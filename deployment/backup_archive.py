"""Seal PostgreSQL snapshots with age and unpack trusted archives offline.

The normal VM needs recipients (public keys) only. Keep recovery identities off
the VM. age authenticates ciphertext, not the sender: trust the backup source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from database_backup import (
    BACKUP_NAME,
    Database,
    backup,
    check_private,
    checksum,
    private_file,
    require,
    sync_directory,
    verify_backup,
)

FORMAT = "gh-random-encrypted-backup-v1"
RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}")


@contextmanager
def work_directory(parent: Path):
    require(
        parent.is_absolute() and parent.resolve() != Path(parent.anchor),
        "Refusing a filesystem root or relative backup directory",
    )
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(parent)
    work = parent / (".work-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    try:
        yield work
    finally:
        # Delete only our exact, private temporary tree, never a computed root.
        require(
            work.resolve().parent == parent.resolve()
            and work.name.startswith(".work-")
            and not work.is_symlink(),
            "Unsafe backup work cleanup",
        )
        require(all(not path.is_symlink() for path in work.rglob("*")), "Unsafe temporary symlink")
        shutil.rmtree(work)


def recipients(path: Path) -> list[str]:
    check_private(path)
    require(
        path.is_absolute() and path.is_file() and path.stat().st_size <= 16384,
        "Invalid recipients file",
    )
    values = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    require(
        1 <= len(values) <= 10 and all(RECIPIENT.fullmatch(value) for value in values),
        "Use 1–10 native age X25519 public recipients, not identities/plugins/SSH keys",
    )
    return values


def tool(path: Path) -> str:
    require(
        path.is_absolute() and path.is_file() and not path.is_symlink(),
        "Tool path must be an absolute regular executable",
    )
    if os.name == "posix":
        info = path.stat()
        require(
            info.st_uid in (0, os.geteuid()) and info.st_mode & 0o022 == 0,
            "Tool must be owned by root/current user and not writable by others",
        )
        require(os.access(path, os.X_OK), "Tool is not executable")
    return str(path)


def seal(
    database: Database, directory: Path, recipient_file: Path, age: Path, *, timeout: int = 900
) -> Path:
    keys = recipients(recipient_file)
    executable = tool(age)
    with work_directory(directory) as work:
        raw = backup(database, work)
        verify_backup(raw)
        tar_path = work / "package.tar"
        with private_file(tar_path) as stream:
            with tarfile.open(fileobj=stream, mode="w") as package:
                for name in ("database.dump", "manifest.json"):
                    package.add(raw / name, arcname=f"{raw.name}/{name}", recursive=False)
        stage = work / "sealed"
        stage.mkdir(mode=0o700)
        ciphertext = stage / "database.tar.age"
        with tar_path.open("rb") as source, private_file(ciphertext) as destination:
            result = subprocess.run(
                [executable, "--encrypt", "--recipients-file", str(recipient_file)],
                stdin=source,
                stdout=destination,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            require(result.returncode == 0, "age encryption failed; no backup certified")
            destination.flush()
            os.fsync(destination.fileno())
        with ciphertext.open("rb") as source:
            require(source.read(22) == b"age-encryption.org/v1\n", "Invalid age ciphertext")
        metadata = {
            "format": FORMAT,
            "id": raw.name,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "sha256": checksum(ciphertext),
            "size_bytes": ciphertext.stat().st_size,
            "recipients_sha256": [hashlib.sha256(value.encode()).hexdigest() for value in keys],
        }
        with private_file(stage / "manifest.json") as output:
            output.write((json.dumps(metadata, indent=2) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        sync_directory(stage)
        target = directory / raw.name
        require(not target.exists(), "Encrypted backup already exists")
        stage.rename(target)
        sync_directory(directory)
        return target


def verify(directory: Path) -> tuple[Path, dict[str, Any]]:
    require(BACKUP_NAME.fullmatch(directory.name) is not None, "Not a managed encrypted backup")
    check_private(directory)
    archive, manifest = directory / "database.tar.age", directory / "manifest.json"
    for path in (archive, manifest):
        require(path.is_file(), "Encrypted backup is incomplete")
        check_private(path)
    require(manifest.stat().st_size <= 65536, "Encrypted manifest exceeds size limit")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    require(
        metadata.get("format") == FORMAT and metadata.get("id") == directory.name,
        "Unsupported/mismatched encrypted backup",
    )
    require(
        archive.stat().st_size == metadata["size_bytes"]
        and checksum(archive) == metadata["sha256"],
        "Encrypted backup checksum mismatch",
    )
    return archive, metadata


def unseal(
    archive: Path,
    identity: Path,
    output: Path,
    age: Path,
    *,
    timeout: int = 900,
    max_bytes: int = 2 * 1024**3,
) -> Path:
    require(archive.is_absolute() and archive.is_file(), "Archive must be an absolute regular file")
    check_private(archive)
    check_private(identity)
    require(
        identity.is_absolute() and identity.is_file(), "Recovery identity must be a private file"
    )
    require(
        max_bytes > 0 and archive.stat().st_size <= max_bytes, "Archive exceeds recovery size limit"
    )
    executable = tool(age)
    with work_directory(output) as work:
        tar_path = work / "package.tar"
        with private_file(tar_path) as destination:
            result = subprocess.run(
                [executable, "--decrypt", "--identity", str(identity), str(archive)],
                stdout=destination,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            require(result.returncode == 0, "age decryption/authentication failed")
        require(
            tar_path.stat().st_size <= max_bytes, "Decrypted archive exceeds recovery size limit"
        )
        with tarfile.open(tar_path, mode="r:") as package:
            members = []
            for member in package:
                members.append(member)
                require(len(members) <= 2, "Unexpected backup members")
            require(
                len(members) == 2 and all(member.isfile() for member in members),
                "Unexpected backup members",
            )
            root = Path(members[0].name).parts[0]
            require(BACKUP_NAME.fullmatch(root) is not None, "Invalid backup root")
            require(
                {m.name for m in members} == {f"{root}/database.dump", f"{root}/manifest.json"},
                "Unsafe archive paths",
            )
            require(
                sum(m.size for m in members) <= max_bytes
                and next(m.size for m in members if m.name.endswith("/manifest.json")) <= 65536,
                "Backup members exceed size limits",
            )
            restored = work / root
            restored.mkdir(mode=0o700)
            for member in members:
                source = package.extractfile(member)
                assert source is not None
                with source, private_file(restored / Path(member.name).name) as destination:
                    shutil.copyfileobj(source, destination)
        verify_backup(restored)
        target = output / root
        require(not target.exists(), "Refusing to overwrite recovered backup")
        restored.rename(target)
        sync_directory(output)
        return target


def upload(directory: Path, config: dict[str, Any], *, runner=subprocess.run) -> dict[str, Any]:
    archive, metadata = verify(directory)
    require(
        metadata["size_bytes"] <= config["max_archive_bytes"],
        "Backup exceeds configured upload ceiling",
    )
    executable = tool(Path(config["oci_binary"]))
    destination = config["offsite"]
    name = destination["prefix"] + directory.name + ".tar.age"
    base = [
        executable,
        "--auth",
        "instance_principal",
        "--connection-timeout",
        "10",
        "--read-timeout",
        "60",
        "--max-retries",
        "2",
        "os",
        "object",
    ]
    location = [
        "--namespace-name",
        destination["namespace"],
        "--bucket-name",
        destination["bucket"],
        "--name",
        name,
    ]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("OCI_")}

    def run(command):
        result = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=config["timeout_seconds"],
            env=environment,
        )
        require(result.returncode == 0, "OCI backup transfer failed; no off-site copy certified")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    put = run(
        [
            *base,
            "put",
            *location,
            "--file",
            str(archive),
            "--no-overwrite",
            "--no-multipart",
            "--verify-checksum",
            "--metadata",
            json.dumps({"gh-random-sha256": metadata["sha256"]}),
        ]
    )
    require(
        isinstance(put.get("etag"), str) and bool(put["etag"]), "OCI did not confirm a new object"
    )
    # A complete download/hash check, not just a successful upload exit status.
    with work_directory(directory.parent) as work:
        downloaded = work / "verified-download.age"
        run([*base, "get", *location, "--file", str(downloaded), "--if-match", put["etag"]])
        require(
            downloaded.is_file()
            and downloaded.stat().st_size == metadata["size_bytes"]
            and checksum(downloaded) == metadata["sha256"],
            "Off-site read-back checksum mismatch",
        )
    receipt = {
        "format": 1,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "object_name": name,
        "bucket": destination["bucket"],
        "namespace": destination["namespace"],
        "etag": put["etag"],
        "sha256": metadata["sha256"],
    }
    from notifications import atomic_json

    atomic_json(directory / "offsite.json", receipt)
    return receipt


def prune(directory: Path, *, retention_days: int = 7, keep: int = 2) -> list[str]:
    require(retention_days >= 1 and keep >= 2, "Keep at least two verified backups")
    check_private(directory)
    candidates = []
    for path in directory.iterdir():
        if not BACKUP_NAME.fullmatch(path.name):
            continue
        # Corrupt/unknown files stop pruning rather than being automatically erased.
        archive, metadata = verify(path)
        receipt_path = path / "offsite.json"
        if not receipt_path.exists():
            continue
        check_private(receipt_path)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        require(
            receipt.get("format") == 1
            and receipt.get("sha256") == metadata["sha256"]
            and isinstance(receipt.get("etag"), str)
            and bool(receipt["etag"])
            and isinstance(receipt.get("object_name"), str)
            and receipt["object_name"].endswith(path.name + ".tar.age")
            and isinstance(receipt.get("verified_at"), str),
            "Invalid off-site receipt",
        )
        require(
            datetime.fromisoformat(receipt["verified_at"]).tzinfo is not None,
            "Off-site receipt must have a timezone",
        )
        require(
            {p.name for p in path.iterdir()}
            == {"database.tar.age", "manifest.json", "offsite.json"},
            "Unknown managed backup files; pruning refused",
        )
        completed = datetime.fromisoformat(metadata["completed_at"])
        require(completed.tzinfo is not None, "Backup time must include zone")
        candidates.append((completed, path))
    candidates.sort(reverse=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = []
    for completed, path in candidates[keep:]:
        if completed >= cutoff:
            continue
        require(
            path.resolve().parent == directory.resolve() and not path.is_symlink(),
            "Unsafe prune target",
        )
        for name in ("database.tar.age", "manifest.json", "offsite.json"):
            (path / name).unlink()
        path.rmdir()
        removed.append(path.name)
    sync_directory(directory)
    return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--age", type=Path, required=True)
    parser.add_argument("--trust-backup", action="store_true", required=True)
    args = parser.parse_args()
    try:
        print(unseal(args.archive, args.identity, args.output_dir, args.age))
    except Exception as error:
        print(
            f"Backup recovery refused ({type(error).__name__}); no database changed.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
