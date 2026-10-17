"""Run the deployed release's encrypted daily backup worker (stdlib host tool)."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crawler import dispatch, private_json, require


class BackupInterrupted(RuntimeError):
    """Do not let subprocess/selectors silently retry this as EINTR."""


def config_from_file(path: Path, *, local_only: bool = False) -> dict[str, Any]:
    from backup_archive import recipients, tool

    config = private_json(path)
    require(config.get("format") == 1, "Unsupported backup configuration")
    for key in ("archive_dir", "recipients_file", "age_binary"):
        require(
            isinstance(config.get(key), str) and Path(config[key]).is_absolute(),
            "Backup paths must be absolute",
        )
    recipients(Path(config["recipients_file"]))
    tool(Path(config["age_binary"]))
    for key, default, minimum, maximum in (
        ("retention_days", 7, 1, 365),
        ("keep_copies", 2, 2, 365),
        ("timeout_seconds", 900, 1, 1800),
        ("max_archive_bytes", 512 * 1024**2, 1, 2 * 1024**3),
    ):
        config[key] = config.get(key, default)
        require(
            type(config[key]) is int and minimum <= config[key] <= maximum, "Invalid backup policy"
        )
    if not local_only:
        require(
            isinstance(config.get("offsite"), dict),
            "Configure off-site storage before scheduling backups",
        )
        offsite = config["offsite"]
        for key in ("namespace", "bucket"):
            require(
                isinstance(offsite.get(key), str)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", offsite[key]) is not None,
                "Invalid OCI destination",
            )
        require(
            isinstance(offsite.get("prefix"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/", offsite["prefix"]) is not None,
            "Use a dedicated backup object prefix ending in slash",
        )
        tool(Path(config["oci_binary"]))
    return config


def worker(arguments: argparse.Namespace) -> int:
    from backup_archive import prune, seal, upload
    from database_backup import Database
    from deploy import Coordinator, Deployment, atomic_json, deployment_lock, load_release

    config = config_from_file(arguments.config, local_only=arguments.local_only)
    with deployment_lock(arguments.state_dir):
        current = private_json(arguments.state_dir / "current.json")
        root = Path(__file__).resolve().parents[1]
        require(
            root == Path(current["release_dir"]).resolve(), "Release changed during backup dispatch"
        )
        metadata = load_release(root)
        require(
            all(metadata[key] == current[key] for key in ("image", "tag", "commit")),
            "Backup release identity changed",
        )
        arguments.release_dir = root
        arguments.timeout_seconds = config["timeout_seconds"]
        deployment = Deployment(arguments)
        deployment.idle_timeout = 2460  # Entire 40-minute unit budget plus cleanup margin.
        deployment.image_info(metadata["image"])  # Timers never pull or start another stack.
        rendered = deployment.preflight(metadata, check_site=False)
        for role in ("web", "database"):
            identifier = deployment.container(role)
            require(identifier is not None, "Start/deploy the verified stack before backup")
            assert identifier is not None
            info = json.loads(deployment.run(["docker", "inspect", identifier], capture=True))[0]
            require(info["State"]["Running"], "Required backup service is stopped")
            if role == "web":
                require(
                    info["Config"]["Image"] == metadata["image"],
                    "Web differs from verified backup release",
                )
            else:
                live = dict(value.split("=", 1) for value in info["Config"]["Env"])
                require(
                    all(
                        live.get(key) == rendered["services"]["database"]["environment"][key]
                        for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
                    ),
                    "Running database differs from configured credentials",
                )
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "phase": "backup",
            "tag": metadata["tag"],
            "image": metadata["image"],
            "systemd_invocation": os.environ.get("INVOCATION_ID"),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "cleanup_complete": False,
        }
        path = arguments.state_dir / "backup.json"
        atomic_json(path, record)
        try:
            with Coordinator(deployment) as owner:
                owner.request("inspect")
                archive = seal(
                    Database(deployment.compose, timeout=config["timeout_seconds"]),
                    Path(config["archive_dir"]),
                    Path(config["recipients_file"]),
                    Path(config["age_binary"]),
                    timeout=config["timeout_seconds"],
                )
                record.update(archive_dir=str(archive), phase="upload")
                atomic_json(path, record)
                owner.request("inspect")
                if arguments.local_only:
                    record["status"] = "local_only"
                else:
                    record["offsite"] = upload(archive, config)
                    owner.request("inspect")
                    record["phase"] = "retention"
                    record["pruned"] = prune(
                        Path(config["archive_dir"]),
                        retention_days=config["retention_days"],
                        keep=config["keep_copies"],
                    )
                    record["status"] = "succeeded"
            record["cleanup_complete"] = True
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            atomic_json(path, record)
            if record["status"] == "succeeded":
                atomic_json(arguments.state_dir / "backup-success.json", record)
            print(f"Backup {record['id']}: {record['status']}")
            return 0
        except BaseException as error:
            record.update(
                status="failed",
                error_type=type(error).__name__,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            atomic_json(path, record)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "worker"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-name", default="gh-random")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Explicit rehearsal; never promote success watermark",
    )
    args = parser.parse_args()

    def interrupt(signum, frame):
        raise BackupInterrupted("Backup interrupted")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project_name) is not None,
            "Invalid backup project name",
        )
        require(
            all(path.is_absolute() for path in (args.state_dir, args.env_file, args.config)),
            "Use absolute operational paths",
        )
        if args.command == "run":
            return dispatch(args, worker_name="backup.py")
        return worker(args)
    except (Exception, KeyboardInterrupt) as error:
        print(
            f"Backup failed ({type(error).__name__}); inspect private state and journal.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
