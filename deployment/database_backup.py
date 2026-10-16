"""Back up the Compose PostgreSQL service and restore only to a new database.

Requires Python 3.12 and Docker Compose on the host, not a Python virtualenv.
Archives contain database data: use private storage and trust the source before
restoring. Checksums detect corruption; they do not authenticate the publisher.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

BACKUP_FORMAT = "gh-random-postgresql-backup-v1"
RESTORE_DATABASE = re.compile(r"gh_random_restore_[a-z0-9_]{1,40}", re.ASCII)
BACKUP_NAME = re.compile(r"gh-random-\d{8}T\d{6}Z-[0-9a-f]{12}", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
ROOT = Path(__file__).resolve().parents[1]
AUTH = 'export PGPASSWORD="$POSTGRES_PASSWORD"; '
CONNECTION = '--no-password --host=/var/run/postgresql --port=5432 --username="$POSTGRES_USER"'


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def checksum(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def private_file(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def check_private(path: Path) -> None:
    require(not path.is_symlink(), f"Refusing a symlink: {path}")
    if os.name == "posix":
        require(path.stat().st_uid == os.geteuid(), "Backup paths must belong to the current user")
        require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "Backup paths must be private")


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Database:
    """Use client tools inside the database container, without exposing passwords."""

    def __init__(self, compose: list[str], *, timeout: int = 1800) -> None:
        require(timeout > 0, "Timeout must be positive")
        self.timeout = timeout
        result = subprocess.run(
            [*compose, "ps", "--quiet", "--status", "running", "database"],
            capture_output=True,
            check=True,
            timeout=30,
        )
        self.container = result.stdout.decode().strip()
        require(
            re.fullmatch(r"[0-9a-f]{12,64}", self.container) is not None,
            "Expected exactly one running Compose database container",
        )

    def command(
        self,
        script: str,
        *arguments: str,
        source: BinaryIO | None = None,
        destination: BinaryIO | None = None,
    ) -> bytes:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.container,
                "sh",
                "-eu",
                "-c",
                AUTH + script,
                "sh",
                *arguments,
            ],
            stdin=source if source is not None else subprocess.DEVNULL,
            stdout=destination if destination is not None else subprocess.PIPE,
            # Logs can include SQL or data. Emit only a generic failure to callers.
            stderr=subprocess.PIPE,
            timeout=self.timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"PostgreSQL {script.split()[1]} failed (exit {result.returncode}); "
                "no backup/restore was certified"
            )
        if script.startswith("exec pg_dump ") and result.stderr:
            raise RuntimeError("pg_dump emitted diagnostics; backup was not certified")
        return result.stdout or b""

    def info(self) -> dict[str, object]:
        result = self.command(
            f'exec psql {CONNECTION} --dbname="$POSTGRES_DB" -X -A -t '
            '-v ON_ERROR_STOP=1 --command "$1"',
            "SELECT json_build_object('database',current_database(),"
            "'server_version_num',current_setting('server_version_num')::int)",
        )
        return json.loads(result)

    def dump(self, destination: BinaryIO) -> None:
        self.command(
            f'exec pg_dump {CONNECTION} --dbname="$POSTGRES_DB" --format=custom '
            "--no-privileges --lock-wait-timeout=60s",
            destination=destination,
        )

    def inspect(self, archive: Path) -> None:
        with archive.open("rb") as source:
            self.command("exec pg_restore --list", source=source)

    def restore(self, archive: Path, target: str) -> None:
        self.command(f'exec createdb {CONNECTION} --template=template0 -- "$1"', target)
        # No --clean, --create, DROP, overwrite, or automatic cleanup of databases.
        # Failure leaves a new database for inspection; never retry into it.
        with archive.open("rb") as source:
            self.command(
                f'exec pg_restore {CONNECTION} --dbname="$1" --no-owner --no-privileges '
                "--exit-on-error --single-transaction",
                target,
                source=source,
            )

    def summary(self, target: str) -> dict[str, object]:
        # The application schema must exist for this to be a usable catalog restore.
        result = self.command(
            f'exec psql {CONNECTION} --dbname="$1" -X -A -t -v ON_ERROR_STOP=1 --command "$2"',
            target,
            "SELECT json_build_object("
            "'database',current_database(),"
            "'alembic_revision',(SELECT version_num FROM alembic_version),"
            "'repositories',(SELECT count(*) FROM repositories),"
            "'generations',(SELECT count(*) FROM catalog_generations),"
            "'memberships',(SELECT count(*) FROM generation_repositories),"
            "'crawl_runs',(SELECT count(*) FROM crawl_runs),"
            "'running_crawls',(SELECT count(*) FROM crawl_runs WHERE status='running'),"
            "'active_memberships',(SELECT count(*) FROM generation_repositories gr "
            "JOIN catalog_generations g ON g.id=gr.generation_id WHERE g.status='active'))",
        )
        return json.loads(result)


def backup(database: Database, directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(directory)
    info = database.info()
    name = f"gh-random-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
    partial = directory / f".partial-{name}"
    partial.mkdir(mode=0o700)
    archive = partial / "database.dump"
    with private_file(archive) as destination:
        database.dump(destination)
        destination.flush()
        os.fsync(destination.fileno())
    require(archive.stat().st_size > 5, "Dump is empty")
    with archive.open("rb") as source:
        require(source.read(5) == b"PGDMP", "Expected a PostgreSQL custom-format dump")
    database.inspect(archive)
    manifest = {
        "format": BACKUP_FORMAT,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source_database": info["database"],
        "server_version_num": info["server_version_num"],
        "sha256": checksum(archive),
        "size_bytes": archive.stat().st_size,
    }
    with private_file(partial / "manifest.json") as destination:
        destination.write((json.dumps(manifest, indent=2) + "\n").encode())
        destination.flush()
        os.fsync(destination.fileno())
    complete = directory / name
    require(not complete.exists(), "Backup destination already exists")
    sync_directory(partial)
    partial.rename(complete)
    sync_directory(directory)
    return complete


def verify_backup(directory: Path) -> tuple[Path, dict[str, object]]:
    require(BACKUP_NAME.fullmatch(directory.name) is not None, "Not a completed backup directory")
    check_private(directory)
    archive = directory / "database.dump"
    manifest_path = directory / "manifest.json"
    for path in (archive, manifest_path):
        require(path.is_file(), "Backup files are missing")
        check_private(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(
        isinstance(manifest, dict)
        and {"format", "server_version_num", "sha256", "size_bytes", "source_database"}
        <= manifest.keys(),
        "Invalid backup manifest",
    )
    require(manifest["format"] == BACKUP_FORMAT, "Unsupported backup format")
    require(type(manifest["server_version_num"]) is int, "Invalid server version")
    require(type(manifest["size_bytes"]) is int, "Invalid backup size")
    require(
        isinstance(manifest["sha256"], str) and SHA256.fullmatch(manifest["sha256"]) is not None,
        "Invalid checksum",
    )
    require(archive.stat().st_size == manifest["size_bytes"], "Backup size mismatch")
    require(checksum(archive) == manifest["sha256"], "Backup checksum mismatch")
    return archive, manifest


def restore(database: Database, directory: Path, target: str) -> dict[str, object]:
    require(
        RESTORE_DATABASE.fullmatch(target) is not None, "Use a new gh_random_restore_* database"
    )
    archive, manifest = verify_backup(directory)
    current = database.info()
    require(target != current["database"], "Refusing to restore into the live database")
    require(
        int(str(current["server_version_num"])) // 10000
        == int(str(manifest["server_version_num"])) // 10000,
        "Restore rehearsal requires the same PostgreSQL major version",
    )
    database.inspect(archive)
    database.restore(archive, target)
    summary = database.summary(target)
    require(bool(summary["alembic_revision"]), "Restored database has no Alembic revision")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True, help="Explicit Compose env file")
    parser.add_argument("--compose-file", type=Path, default=ROOT / "compose.production.yaml")
    parser.add_argument("--project-name", default="gh-random")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    commands = parser.add_subparsers(dest="command", required=True)
    dump = commands.add_parser("backup")
    dump.add_argument("--output-dir", type=Path, required=True)
    recovery = commands.add_parser("restore")
    recovery.add_argument("--backup", type=Path, required=True)
    recovery.add_argument("--target-database", required=True)
    recovery.add_argument("--trust-backup", action="store_true", required=True)
    args = parser.parse_args()
    database = Database(
        [
            "docker",
            "compose",
            "--env-file",
            str(args.env_file.resolve()),
            "--file",
            str(args.compose_file.resolve()),
            "--project-name",
            args.project_name,
        ],
        timeout=args.timeout_seconds,
    )
    if args.command == "backup":
        print(backup(database, args.output_dir.absolute()))
    else:
        print(json.dumps(restore(database, args.backup.absolute(), args.target_database), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Database operation failed: {error}", file=sys.stderr)
        print(
            "Partial backups/new restore databases may remain; no live database was dropped.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
