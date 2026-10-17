"""Deploy a trusted, extracted release; never downgrade schema or delete volumes."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from database_backup import Database, backup, check_private, checksum, private_file, sync_directory

ROOT = Path(__file__).resolve().parents[1]
CRITICAL_FILES = {
    "compose.production.yaml",
    "Caddyfile",
    "deployment/deploy.py",
    "deployment/database_backup.py",
}
IMAGE = re.compile(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    with private_file(temporary) as output:
        output.write((json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


@contextmanager
def deployment_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(directory)
    path = directory / "deploy.lock"
    require(not path.is_symlink(), "Unsafe deployment lock path")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        check_private(path)
        if path.stat().st_size == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another deployment owns this state directory") from None
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    # Never unlink a lock file: a contender could be locking its old inode.


def load_release(root: Path) -> dict[str, Any]:
    metadata = json.loads((root / "release.json").read_text(encoding="utf-8"))
    require(metadata.get("deployment_format") == 1, "Unsupported/missing deployment manifest")
    require(TAG.fullmatch(metadata.get("tag", "")) is not None, "Invalid release version")
    require(SHA.fullmatch(metadata.get("commit", "")) is not None, "Invalid release commit")
    require(
        IMAGE.fullmatch(metadata.get("image", "")) is not None,
        "An immutable GHCR digest is required",
    )
    require(
        metadata["image"] == f"ghcr.io/{metadata['repository'].lower()}@{metadata['digest']}",
        "Image/repository/digest mismatch",
    )
    files = metadata.get("deployment_files", {})
    require(isinstance(files, dict) and CRITICAL_FILES <= files.keys(), "Missing deployment files")
    for name, digest in files.items():
        path = root / name
        require(
            not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and not path.is_symlink()
            and path.is_file()
            and path.resolve().is_relative_to(root.resolve()),
            "Unsafe release file path",
        )
        require(checksum(path) == digest, f"Release file changed: {name}")
    return metadata


def update_image_setting(path: Path, image: str) -> None:
    check_private(path)
    contents = path.read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^GH_RANDOM_IMAGE=.*$", f"GH_RANDOM_IMAGE={image}", contents)
    require(count == 1, "Compose env must contain exactly one GH_RANDOM_IMAGE line")
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    with private_file(temporary) as output:
        output.write(updated.encode())
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Deployment smoke test must not redirect")


def validate_url(url: str, *, allow_loopback_http: bool) -> None:
    parsed = urlsplit(url)
    require(
        bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment,
        "Provide the site's origin URL, without credentials, query, or extra path",
    )
    if parsed.scheme == "https":
        return
    loopback = False
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        pass
    require(
        allow_loopback_http and parsed.scheme == "http" and loopback,
        "HTTPS is required; HTTP is only allowed explicitly for loopback rehearsals",
    )


def smoke(url: str, timeout: int) -> None:
    opener = build_opener(NoRedirect())
    deadline = time.monotonic() + timeout
    while True:
        try:
            for suffix in ("", "?discover=true"):
                with opener.open(
                    url.rstrip("/") + "/" + suffix, timeout=min(timeout, 10)
                ) as response:
                    require(response.status == 200, "Web smoke test did not return HTTP 200")
                    require(
                        "text/html" in response.headers.get("Content-Type", "")
                        and b"<html" in response.read(2_000_000).lower(),
                        "Web smoke test did not return the rendered page",
                    )
            return
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Public page/database discovery smoke test failed") from None
            time.sleep(0.5)


class Coordinator:
    def __init__(self, deployment: Deployment) -> None:
        self.deployment = deployment
        self.name = f"gh-random-deploy-{uuid.uuid4().hex[:12]}"
        self.responses: queue.Queue[str | None] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None

    def receive(self) -> dict[str, Any]:
        try:
            line = self.responses.get(timeout=self.deployment.timeout + 30)
        except queue.Empty:
            raise TimeoutError("Deployment coordinator timed out") from None
        if line is None:
            raise RuntimeError("Deployment coordinator exited unexpectedly")
        result = json.loads(line)
        require(
            result.get("ok") is True,
            f"Coordinator refused operation: {result.get('error', 'unknown')}",
        )
        return result

    def request(self, command: str) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Coordinator is not running")
        self.process.stdin.write(json.dumps({"command": command}) + "\n")
        self.process.stdin.flush()
        return self.receive()

    def __enter__(self) -> Coordinator:
        self.process = subprocess.Popen(
            [
                *self.deployment.compose,
                "--profile",
                "migrate",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "--name",
                self.name,
                "-T",
                "migrate",
                "python",
                "-m",
                "app.deployment",
                "--timeout-seconds",
                str(self.deployment.timeout),
            ],
            cwd=self.deployment.root,
            env=self.deployment.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.deployment.log,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        def read() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                self.responses.put(line)
            self.responses.put(None)

        threading.Thread(target=read, daemon=True).start()
        try:
            hello = self.receive()
            require(hello.get("protocol") == 1, "Unsupported deployment coordinator protocol")
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        # Killing docker's client alone does not guarantee the container stopped.
        try:
            self.deployment.run(["docker", "rm", "--force", self.name], check=False)
        finally:
            if self.process is not None:
                if self.process.stdin is not None:
                    try:
                        self.process.stdin.close()
                    except OSError:
                        pass
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)

    def __exit__(self, kind, error, traceback) -> None:
        try:
            if kind is None:
                self.request("finish")
                assert self.process is not None
                self.process.wait(timeout=10)
                require(self.process.returncode == 0, "Deployment coordinator failed on exit")
        finally:
            self.stop()


class Deployment:
    def __init__(self, arguments: argparse.Namespace) -> None:
        self.args = arguments
        self.root = arguments.release_dir.resolve()
        self.timeout: int = arguments.timeout_seconds
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GH_RANDOM_", "COMPOSE_"))
            and key not in {"CADDY_IMAGE", "POSTGRES_IMAGE"}
        }
        self.log: Any = None
        self.compose: list[str] = []

    def run(self, command: list[str], *, capture: bool = False, check: bool = True) -> str:
        result = subprocess.run(
            command,
            cwd=self.root,
            env=self.environment,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE if capture else self.log,
            stderr=self.log,
            timeout=self.timeout + 30,
            check=False,
        )
        require(
            not check or result.returncode == 0,
            "Deployment command failed; inspect the private log",
        )
        return result.stdout or ""

    def container(self, service: str) -> str | None:
        output = self.run([*self.compose, "ps", "--all", "--quiet", service], capture=True).strip()
        require(
            not output or re.fullmatch(r"[0-9a-f]{12,64}", output) is not None,
            "Ambiguous service container",
        )
        return output or None

    def image_info(self, image: str) -> dict[str, Any]:
        return json.loads(self.run(["docker", "image", "inspect", image], capture=True))[0]

    def validate_proxy(self, config: dict[str, Any]) -> None:
        image = config["services"]["caddy"]["image"]
        container = self.container("caddy")
        if container:
            current = json.loads(self.run(["docker", "inspect", container], capture=True))[0]
            require(
                current["Config"]["Image"] == image
                and current["Image"] == self.image_info(image)["Id"],
                "Proxy image changes require separate maintenance",
            )
        else:
            try:
                self.image_info(image)
            except ValueError:
                self.run([*self.compose, "pull", "caddy"])
        self.run(
            [
                *self.compose,
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "-T",
                "caddy",
                "caddy",
                "validate",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
            ]
        )

    def preflight(self, metadata: dict[str, Any]) -> dict[str, Any]:
        env_file: Path = self.args.env_file
        check_private(env_file)
        require(
            not env_file.resolve().is_relative_to(self.root),
            "Keep live configuration outside release files",
        )
        contents = env_file.read_text(encoding="utf-8")
        require(
            len(re.findall(r"(?m)^GH_RANDOM_IMAGE=", contents)) == 1, "Expected one image setting"
        )
        self.environment["GH_RANDOM_IMAGE"] = metadata["image"]
        self.compose = [
            "docker",
            "compose",
            "--env-file",
            str(env_file.resolve()),
            "--file",
            str(self.root / "compose.production.yaml"),
            "--project-name",
            self.args.project_name,
        ]
        config = json.loads(
            self.run(
                [
                    *self.compose,
                    "--profile",
                    "migrate",
                    "--profile",
                    "crawler",
                    "config",
                    "--format",
                    "json",
                ],
                capture=True,
            )
        )
        services = config["services"]
        for role in ("web", "migrate", "crawler"):
            require(
                services[role]["image"] == metadata["image"],
                "Application roles must use the release digest",
            )
        db = services["database"]["environment"]
        for role in ("web", "migrate", "crawler"):
            url = urlsplit(services[role]["environment"]["DATABASE_URL"])
            require(
                url.scheme == "postgresql+psycopg"
                and url.hostname == "database"
                and url.port == 5432
                and unquote(url.path.lstrip("/")) == db["POSTGRES_DB"]
                and unquote(url.username or "") == db["POSTGRES_USER"]
                and unquote(url.password or "") == db["POSTGRES_PASSWORD"],
                "Application/database credentials or hostname disagree",
            )
        for key in (
            "GH_RANDOM_POSTGRES_ENV_FILE",
            "GH_RANDOM_APP_ENV_FILE",
            "GH_RANDOM_CRAWLER_ENV_FILE",
            "GH_RANDOM_CADDY_ENV_FILE",
            "GITHUB_APP_PRIVATE_KEY_HOST_PATH",
        ):
            # Values resolved by Compose are reflected in env_file/mount paths;
            # inspect the explicit dotenv paths without evaluating shell syntax.
            matches = re.findall(rf"(?m)^{key}=(.+)$", contents)
            require(len(matches) == 1, f"Expected one explicit configuration path: {key}")
            path = Path(matches[0].strip().strip('"').strip("'"))
            require(
                path.is_absolute()
                and path.is_file()
                and not path.resolve().is_relative_to(self.root),
                "Configuration paths must be absolute regular files outside the release",
            )
            check_private(path)
        site = services["caddy"]["environment"]["GH_RANDOM_DOMAIN"]
        domain = urlsplit(site if "://" in site else "https://" + site).hostname
        require(
            domain == urlsplit(self.args.url).hostname,
            "Smoke URL must match the configured site hostname",
        )
        try:
            image = self.image_info(metadata["image"])
        except (ValueError, json.JSONDecodeError):
            self.run(["docker", "pull", metadata["image"]])
            image = self.image_info(metadata["image"])
        labels = image["Config"].get("Labels") or {}
        require(
            labels.get("org.opencontainers.image.source")
            == f"https://github.com/{metadata['repository']}"
            and labels.get("org.opencontainers.image.revision") == metadata["commit"]
            and labels.get("org.opencontainers.image.version") == metadata["tag"],
            "Image source/revision/version labels do not match the release",
        )
        host_arch = self.run(
            ["docker", "info", "--format", "{{.Architecture}}"], capture=True
        ).strip()
        host_arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(host_arch, host_arch)
        require(
            image["Os"] == "linux" and image["Architecture"] == host_arch,
            "Image/host architecture mismatch",
        )
        require(metadata.get("platform") == f"linux/{host_arch}", "Release platform mismatch")
        return config

    def execute(self) -> dict[str, Any]:
        require(
            self.timeout > 0 and self.args.smoke_timeout_seconds > 0, "Timeouts must be positive"
        )
        require(
            all(
                path.is_absolute()
                for path in (self.args.env_file, self.args.state_dir, self.args.backup_dir)
            ),
            "Configuration, state, and backup paths must be absolute",
        )
        validate_url(self.args.url, allow_loopback_http=self.args.allow_loopback_http)
        state: Path = self.args.state_dir
        with deployment_lock(state):
            current_file = state / "current.json"
            if current_file.exists():
                check_private(current_file)
            previous = json.loads(current_file.read_text()) if current_file.exists() else None
            if self.args.command == "rollback":
                require(
                    self.args.confirm_schema_compatible,
                    "Rollback requires explicit compatibility acknowledgement",
                )
                require(
                    previous is not None and previous.get("previous"),
                    "No successful previous release recorded",
                )
                assert previous is not None
                self.root = Path(previous["previous"]["release_dir"]).resolve()
            metadata = load_release(self.root)
            identifier = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
            )
            record: dict[str, Any] = {
                "id": identifier,
                "status": "running",
                "phase": "preflight",
                "command": self.args.command,
                "tag": metadata["tag"],
                "commit": metadata["commit"],
                "image": metadata["image"],
                "release_dir": str(self.root),
                "previous": previous,
            }
            record_file = state / f"{identifier}.json"
            with private_file(state / f"{identifier}.log") as self.log:

                def phase(name: str) -> None:
                    record["phase"] = name
                    atomic_json(record_file, record)
                    print(f"Deployment {identifier}: {name}", flush=True)

                try:
                    phase("preflight")
                    config = self.preflight(metadata)
                    self.validate_proxy(config)
                    database = self.container("database")
                    if database is None:
                        require(
                            self.args.bootstrap and previous is None,
                            "Database is missing; explicit first-deployment --bootstrap required",
                        )
                        self.run([*self.compose, "pull", "database"])
                        self.run(
                            [*self.compose, "up", "--detach", "--no-recreate", "--wait", "database"]
                        )
                    else:
                        existing = json.loads(
                            self.run(["docker", "inspect", database], capture=True)
                        )[0]
                        require(
                            existing["State"]["Running"],
                            "Start the existing database deliberately before deployment",
                        )
                        require(
                            existing["Config"]["Image"] == config["services"]["database"]["image"],
                            "Database image changes require separate maintenance",
                        )
                        require(
                            existing["Image"] == self.image_info(existing["Config"]["Image"])["Id"],
                            "Database image tag changed; review separately",
                        )
                        live_env = dict(item.split("=", 1) for item in existing["Config"]["Env"])
                        require(
                            all(
                                live_env.get(key)
                                == config["services"]["database"]["environment"][key]
                                for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
                            ),
                            "Existing database configuration differs; review it separately",
                        )
                    web = self.container("web")
                    record["previous_runtime_image"] = (
                        json.loads(self.run(["docker", "inspect", web], capture=True))[0]["Config"][
                            "Image"
                        ]
                        if web
                        else None
                    )
                    coordinator = Coordinator(self)
                    record["coordinator_container"] = coordinator.name
                    phase("acquire_catalog")
                    with coordinator:
                        record["schema_before"] = coordinator.request("inspect")
                        if self.args.command == "rollback":
                            require(
                                record["schema_before"]["revision"]
                                == record["schema_before"]["head"],
                                "Schema-changing rollback refused; use a reviewed recovery plan",
                            )
                        phase("backup")
                        record["backup"] = str(
                            backup(
                                Database(self.compose, timeout=self.timeout), self.args.backup_dir
                            )
                        )
                        phase("migrate")
                        if self.args.command == "apply":
                            record["schema_after"] = coordinator.request("migrate")
                        else:
                            record["schema_after"] = coordinator.request("inspect")
                        require(
                            record["schema_after"]["revision"] == record["schema_after"]["head"],
                            "Schema did not reach the release head",
                        )
                        phase("restart_web")
                        coordinator.request("inspect")
                        self.run(
                            [
                                *self.compose,
                                "up",
                                "--detach",
                                "--no-deps",
                                "--pull",
                                "never",
                                "--force-recreate",
                                "--wait",
                                "web",
                            ]
                        )
                        phase("restart_proxy")
                        coordinator.request("inspect")
                        self.run(
                            [
                                *self.compose,
                                "up",
                                "--detach",
                                "--no-deps",
                                "--pull",
                                "never",
                                "--force-recreate",
                                "--wait",
                                "caddy",
                            ]
                        )
                        phase("smoke")
                        smoke(self.args.url, self.args.smoke_timeout_seconds)
                        coordinator.request("inspect")
                        phase("record_success")
                        update_image_setting(self.args.env_file, metadata["image"])
                        successful = {
                            key: record[key]
                            for key in ("id", "tag", "commit", "image", "release_dir")
                        }
                        successful["previous"] = (
                            {
                                key: previous[key]
                                for key in ("id", "tag", "commit", "image", "release_dir")
                            }
                            if previous
                            else None
                        )
                        atomic_json(current_file, successful)
                        phase("release_catalog")
                    record.update(status="success", phase="complete")
                    atomic_json(record_file, record)
                    return record
                except BaseException as error:
                    record.update(status="failed", error_type=type(error).__name__)
                    atomic_json(record_file, record)
                    raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["apply", "rollback"])
    parser.add_argument("--release-dir", type=Path, default=ROOT)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project-name", default="gh-random")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--smoke-timeout-seconds", type=int, default=60)
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--allow-loopback-http", action="store_true")
    parser.add_argument("--confirm-schema-compatible", action="store_true")
    arguments = parser.parse_args()

    def interrupt(signum, frame) -> None:
        raise InterruptedError("Deployment interrupted")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        result = Deployment(arguments).execute()
        print(f"Deployment successful: {result['tag']} {result['image']}")
    except (Exception, KeyboardInterrupt) as error:
        print(f"Deployment failed ({type(error).__name__}): {error}", file=sys.stderr)
        print(
            "No schema downgrade, data restore, or automatic rollback was attempted. "
            "Inspect the private journal/log.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
