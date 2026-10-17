"""Dispatch the verified release's crawler supervisor; stop only its owned container.

Install this stdlib bootstrap at /usr/local/lib/gh-random/crawler.py. Actual work
executes this script from the verified release, with its matching deployment tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OWNER_LABEL = "io.gh-random.scheduled-crawl"
PROJECT_LABEL = "io.gh-random.project"


class SupervisorInterrupted(RuntimeError):
    """Not OSError/EINTR: selectors must not swallow the shutdown request."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def private_json(path: Path) -> dict[str, Any]:
    # This small check is intentionally self-contained in the installed bootstrap.
    for target in (path.parent, path):
        require(not target.is_symlink(), "Refusing symlinked operational state")
        if os.name == "posix":
            info = target.stat()
            require(
                info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0,
                "Operational state must be private and owned by this user",
            )
    return json.loads(path.read_text(encoding="utf-8"))


def docker(command: list[str], *, timeout: int = 30, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    require(not check or result.returncode == 0, "Docker operation failed")
    return result.stdout.strip() if result.returncode == 0 else ""


def owned_container(record: dict[str, Any]) -> dict[str, Any] | None:
    name = record["container_name"]
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9_-]*-scheduled-[0-9a-f]{32}", name) is not None,
        "Invalid recorded container name",
    )
    # Listing by name distinguishes absence from an unavailable Docker daemon.
    identifiers = docker(
        ["container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"name=^/{name}$"]
    )
    if not identifiers:
        return None
    require(re.fullmatch(r"[0-9a-f]{64}", identifiers) is not None, "Ambiguous recorded container")
    current = json.loads(docker(["inspect", identifiers]))[0]
    labels = current["Config"].get("Labels") or {}
    require(
        labels.get(OWNER_LABEL) == record["id"]
        and labels.get(PROJECT_LABEL) == record["project"]
        and (not record.get("container_id") or record["container_id"] == current["Id"]),
        "Container identity/ownership mismatch; refusing cleanup",
    )
    return current


def cleanup(record: dict[str, Any]) -> None:
    container = owned_container(record)
    if container is None:
        return
    identifier = container["Id"]
    if container["State"]["Running"]:
        # Docker sends TERM, then KILL after the grace period. Never kill merely
        # the docker client and assume its daemon-owned child has stopped.
        docker(["stop", "--time", "30", identifier], timeout=45)
    docker(["rm", identifier])


def dispatch(arguments: argparse.Namespace) -> int:
    current = private_json(arguments.state_dir / "current.json")
    root = Path(current["release_dir"])
    require(root.is_absolute() and root.is_dir(), "Verified release directory is unavailable")
    metadata = json.loads((root / "release.json").read_text(encoding="utf-8"))
    require(
        metadata.get("deployment_format") == 1
        and all(metadata.get(key) == current.get(key) for key in ("image", "tag", "commit")),
        "Verified release pointer/manifest mismatch",
    )
    worker = root / "deployment/crawler.py"
    require(worker.is_file() and not worker.is_symlink(), "Missing/unsafe release supervisor")
    with worker.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    require(
        digest == metadata.get("deployment_files", {}).get("deployment/crawler.py"),
        "Release supervisor hash mismatch",
    )
    os.execv(sys.executable, [sys.executable, str(worker), "worker", *sys.argv[2:]])
    raise AssertionError("exec returned")


def supervise(arguments: argparse.Namespace) -> int:
    # Imported only after dispatch, from the matching verified release directory.
    from deploy import Deployment, atomic_json, deployment_lock, load_release

    require(
        arguments.env_file is not None and arguments.env_file.is_absolute(),
        "An absolute Compose env path is required",
    )
    require(arguments.max_runtime_seconds > 0, "Runtime limit must be positive")
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9_-]*", arguments.project_name) is not None,
        "Invalid Compose project name",
    )
    state = arguments.state_dir
    with deployment_lock(state):
        current = private_json(state / "current.json")
        root = Path(__file__).resolve().parents[1]
        require(root == Path(current["release_dir"]).resolve(), "Release changed during dispatch")
        metadata = load_release(root)
        require(
            all(metadata[key] == current[key] for key in ("image", "tag", "commit")),
            "Verified release identity changed",
        )
        previous_file = state / "crawler.json"
        if previous_file.exists():
            # Holding the kernel lock establishes that the previous supervisor
            # no longer owns this state. Clean only its labelled daemon child.
            cleanup(private_json(previous_file))
        arguments.release_dir = root
        arguments.timeout_seconds = 60
        deployment = Deployment(arguments)
        deployment.image_info(metadata["image"])  # Never pull/start another image from a timer.
        config = deployment.preflight(metadata, check_site=False)
        web = deployment.container("web")
        database = deployment.container("database")
        require(web is not None and database is not None, "Deploy/start the verified stack first")
        assert web is not None and database is not None
        web_info = json.loads(deployment.run(["docker", "inspect", web], capture=True))[0]
        require(
            web_info["State"]["Running"] and web_info["Config"]["Image"] == metadata["image"],
            "Running web differs from the verified release; recover deployment first",
        )
        db_info = json.loads(deployment.run(["docker", "inspect", database], capture=True))[0]
        live = dict(item.split("=", 1) for item in db_info["Config"]["Env"])
        require(
            db_info["State"]["Running"]
            and all(
                live.get(key) == config["services"]["database"]["environment"][key]
                for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
            ),
            "Running database differs from configured credentials",
        )
        identifier = uuid.uuid4().hex
        record = {
            "id": identifier,
            "project": arguments.project_name,
            "container_name": f"{arguments.project_name}-scheduled-{identifier}",
            "container_id": None,
            "status": "launching",
            "image": metadata["image"],
            "tag": metadata["tag"],
            "commit": metadata["commit"],
            "systemd_invocation": os.environ.get("INVOCATION_ID"),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_json(previous_file, record)
        logs = None
        waiter = None
        result = 1
        try:
            print(f"Scheduled crawl {identifier}: starting {metadata['tag']}", flush=True)
            output = deployment.run(
                [
                    *deployment.compose,
                    "--profile",
                    "crawler",
                    "run",
                    "--detach",
                    "--no-deps",
                    "--pull",
                    "never",
                    "--name",
                    record["container_name"],
                    "--label",
                    f"{OWNER_LABEL}={identifier}",
                    "--label",
                    f"{PROJECT_LABEL}={arguments.project_name}",
                    "crawler",
                    "python",
                    "-m",
                    "app.cli",
                    "crawl",
                ],
                capture=True,
            )
            require(
                re.fullmatch(r"[0-9a-f]{64}", output.strip()) is not None,
                "Docker did not report a single crawler container",
            )
            record.update(container_id=output.strip(), status="running")
            atomic_json(previous_file, record)
            logs = subprocess.Popen(["docker", "logs", "--follow", "--timestamps", output.strip()])
            waiter = subprocess.Popen(
                ["docker", "wait", output.strip()],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            try:
                exit_text, _ = waiter.communicate(timeout=arguments.max_runtime_seconds)
                require(
                    waiter.returncode == 0 and exit_text.strip().isdigit(),
                    "Unable to observe the crawler's exit status",
                )
                result = 0 if int(exit_text.strip()) == 0 else 1
                record.update(
                    status="succeeded" if result == 0 else "failed",
                    container_exit_code=int(exit_text.strip()),
                )
            except subprocess.TimeoutExpired:
                record["status"] = "timed_out"
                result = 124
            return result
        except (KeyboardInterrupt, SupervisorInterrupted):
            record["status"] = "interrupted"
            return 143
        except Exception as error:
            record.update(status="failed", error_type=type(error).__name__)
            raise
        finally:
            try:
                cleanup(record)
                record["cleanup_complete"] = True
            except Exception as error:
                record["crawl_status"] = record["status"]
                record["status"] = "cleanup_failed"
                record["error_type"] = type(error).__name__
                record["cleanup_complete"] = False
                # A cleanup failure must never be certified as a successful crawl.
                raise
            finally:
                for process in (logs, waiter):
                    if process is not None:
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                atomic_json(previous_file, record)
                if record["status"] == "succeeded" and record["cleanup_complete"]:
                    atomic_json(state / "crawler-success.json", record)
                print(
                    f"Scheduled crawl {identifier}: {record['status']} "
                    f"cleanup={record['cleanup_complete']}",
                    flush=True,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "worker", "stop"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project-name", default="gh-random")
    parser.add_argument("--max-runtime-seconds", type=int, default=18000)
    arguments = parser.parse_args()
    require(arguments.state_dir.is_absolute(), "State directory must be absolute")

    def interrupt(signum, frame):
        raise SupervisorInterrupted("Crawler supervisor interrupted")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        if arguments.command == "run":
            return dispatch(arguments)
        if arguments.command == "stop":
            path = arguments.state_dir / "crawler.json"
            if path.exists():
                record = private_json(path)
                invocation = os.environ.get("INVOCATION_ID")
                if not invocation or invocation == record.get("systemd_invocation"):
                    cleanup(record)
            return 0
        return supervise(arguments)
    except (Exception, KeyboardInterrupt) as error:
        print(
            f"Scheduled crawler refused/failed ({type(error).__name__}): {error}", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
