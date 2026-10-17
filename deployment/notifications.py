"""Private, TLS-only SMTP alerts and local missing-success checks (stdlib only).

Install beside the host systemd units. This is not an external uptime monitor:
neither this process nor SMTP can alert while the VM/network is unavailable.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import smtplib
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Any

FIELDS = {
    "id",
    "tag",
    "phase",
    "status",
    "error_type",
    "result",
    "exit_status",
    "invocation",
    "free_bytes",
    "free_percent",
    "free_inode_percent",
    "remaining_days",
}
ISSUE = re.compile(r"[a-z0-9][a-z0-9_.@:-]{0,180}", re.ASCII)
UNIT = re.compile(r"gh-random-[a-zA-Z0-9_.@-]+\.service", re.ASCII)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def private(path: Path, *, directory: bool = False) -> None:
    require(path.is_absolute() and not path.is_symlink(), "Expected an absolute private path")
    require(path.is_dir() if directory else path.is_file(), "Private path is unavailable")
    if os.name == "posix":
        info = path.stat()
        require(
            info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0,
            "Private paths must be owned by this user and inaccessible to others",
        )


def read_json(path: Path) -> dict[str, Any]:
    private(path)
    require(path.stat().st_size <= 65536, "Operational JSON exceeds size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), "Expected an operational JSON object")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as file:
            json.dump(value, file, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def state_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(directory, directory=True)
    path = directory / "alerts.lock"
    require(not path.is_symlink(), "Unsafe alert lock path")
    with os.fdopen(
        os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600), "r+b"
    ) as lock:
        private(path)
        if path.stat().st_size == 0:
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    import msvcrt

                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another notifier owns alert state") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def address(value: object) -> str:
    require(isinstance(value, str), "Expected an email address")
    assert isinstance(value, str)
    require(value.isascii() and not any(c.isspace() for c in value), "Invalid email address")
    parsed = Address(addr_spec=value)
    require(
        bool(parsed.username and parsed.domain) and parsed.addr_spec == value,
        "Invalid email address",
    )
    return value


def positive(value: object, *, maximum: int) -> bool:
    return type(value) is int and 0 < value <= maximum


def hostname(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 253
        and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split(".")
        )
    )


def load_config(path: Path) -> dict[str, Any]:
    config = read_json(path)
    require(config.get("format") == 1, "Unsupported alerts configuration")
    host = config.get("smtp_host")
    require(isinstance(host, str) and bool(host), "SMTP host is required")
    assert isinstance(host, str)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        require(
            isinstance(host, str)
            and len(host) <= 253
            and all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                for part in host.split(".")
            ),
            "Invalid SMTP hostname",
        )
    require(positive(config.get("smtp_port"), maximum=65535), "Invalid SMTP port")
    require(config.get("smtp_security") in ("ssl", "starttls"), "SMTP must use TLS")
    config["from_address"] = address(config.get("from_address"))
    recipients = config.get("to_addresses")
    require(isinstance(recipients, list) and 1 <= len(recipients) <= 5, "Use 1–5 alert recipients")
    assert isinstance(recipients, list)
    config["to_addresses"] = [address(value) for value in recipients]
    for field in ("state_dir", "deployment_state_dir"):
        require(
            isinstance(config.get(field), str) and Path(config[field]).is_absolute(),
            "Invalid state directory",
        )
    for field, default, maximum in (
        ("smtp_timeout_seconds", 10, 30),
        ("repeat_after_hours", 6, 168),
        ("crawl_max_age_hours", 12, 168),
        ("backup_max_age_hours", 36, 720),
    ):
        config[field] = config.get(field, default)
        require(positive(config[field], maximum=maximum), "Invalid alert policy duration")
    require(type(config.get("monitor_backups", False)) is bool, "Invalid backup monitoring switch")
    paths = config.setdefault("disk_paths", [])
    require(
        isinstance(paths, list)
        and len(paths) <= 3
        and all(isinstance(value, str) and Path(value).is_absolute() for value in paths),
        "Use up to three absolute monitored filesystem paths",
    )
    require(len(set(paths)) == len(paths), "Duplicate monitored filesystem path")
    for field, default, maximum in (
        ("disk_min_free_bytes", 2 * 1024**3, 1024**5),
        ("disk_min_free_percent", 10, 99),
        ("disk_min_free_inode_percent", 5, 99),
        ("tls_warn_days", 14, 90),
    ):
        config[field] = config.get(field, default)
        require(positive(config[field], maximum=maximum), "Invalid host monitoring threshold")
    if config.get("tls_host") is not None:
        require(hostname(config["tls_host"]), "Use an exact lowercase TLS hostname, not a URL")
    if config.get("tls_ca_file"):
        private(Path(config["tls_ca_file"]))
    for key, default in (
        ("crawler_unit", "gh-random-crawler.service"),
        ("backup_unit", "gh-random-backup.service"),
        ("monitor_unit", "gh-random-monitor.service"),
    ):
        config[key] = config.get(key, default)
        require(
            isinstance(config[key], str) and UNIT.fullmatch(config[key]) is not None,
            "Invalid monitored unit",
        )
    username = config.get("smtp_username")
    password = config.get("smtp_password_file")
    require(
        bool(username) == bool(password),
        "SMTP username and password file must be configured together",
    )
    if username:
        require(
            isinstance(username, str) and not any(c in username for c in "\r\n"),
            "Invalid SMTP username",
        )
        require(isinstance(password, str), "Invalid SMTP password path")
        assert isinstance(password, str)
        secret = Path(password)
        private(secret)
        require(0 < secret.stat().st_size <= 4096, "Invalid SMTP password file")
        require(bool(secret.read_text(encoding="utf-8").rstrip("\r\n")), "SMTP password is empty")
    if config.get("smtp_ca_file"):
        # Optional explicit trust anchor, never disable hostname/certificate checks.
        private(Path(config["smtp_ca_file"]))
    ssl.create_default_context(cafile=config.get("smtp_ca_file"))
    return config


def send_email(config: dict[str, Any], issue: str, status: str, details: dict[str, str]) -> None:
    message = EmailMessage()
    message["From"] = config["from_address"]
    message["To"] = ", ".join(config["to_addresses"])
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid(domain=config["from_address"].split("@", 1)[1])
    message["Subject"] = f"[GH Random] {status}: {issue}"
    lines = [
        f"GH Random: {issue} — {status}",
        "",
        *[f"{key}: {value}" for key, value in details.items()],
    ]
    lines += [
        "",
        "Inspect private deployment journals and:",
        "journalctl -u gh-random-crawler.service --since today",
        "journalctl -u gh-random-backup.service --since today",
        "journalctl -u gh-random-monitor.service --since today",
        "",
        "No raw logs, database URLs or credential files are included.",
    ]
    message.set_content("\n".join(lines), cte="quoted-printable")
    context = ssl.create_default_context(cafile=config.get("smtp_ca_file"))
    factory = smtplib.SMTP_SSL if config["smtp_security"] == "ssl" else smtplib.SMTP
    options = {"timeout": config["smtp_timeout_seconds"]}
    if config["smtp_security"] == "ssl":
        options["context"] = context
    with factory(config["smtp_host"], config["smtp_port"], **options) as smtp:
        smtp.ehlo()
        if config["smtp_security"] == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if config.get("smtp_username"):
            password = Path(config["smtp_password_file"]).read_text(encoding="utf-8").rstrip("\r\n")
            require(bool(password), "SMTP password is empty")
            smtp.login(config["smtp_username"], password)
        refused = smtp.send_message(message)
        require(not refused, "Some SMTP recipients were refused")


def timestamp(value: object) -> datetime:
    require(isinstance(value, str), "Missing timestamp")
    assert isinstance(value, str)
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "Timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def safe_details(values: dict[str, Any]) -> dict[str, str]:
    return {
        key: re.sub(r"[^A-Za-z0-9 ._:@/+-]", "?", str(value))[:180]
        for key, value in values.items()
        if key in FIELDS
    }


class Reporter:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        sender: Callable[[dict[str, Any], str, str, dict[str, str]], None] = send_email,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config, self.sender, self.now = config, sender, now

    def event(self, issue: str, failed: bool, details: dict[str, Any]) -> bool:
        require(ISSUE.fullmatch(issue) is not None, "Invalid alert issue key")
        directory = Path(self.config["state_dir"])
        with state_lock(directory):
            path = directory / "alerts.json"
            ledger = read_json(path) if path.exists() else {"format": 1, "issues": {}}
            require(
                ledger.get("format") == 1 and isinstance(ledger.get("issues"), dict),
                "Invalid alert ledger",
            )
            previous = ledger["issues"].get(issue, {})
            require(len(ledger["issues"]) < 100 or bool(previous), "Too many alert issues")
            now = self.now()
            notified = bool(previous.get("notified"))
            if not failed and not previous.get("active"):
                return False
            if not failed and not notified:
                ledger["issues"].pop(issue, None)
                atomic_json(path, ledger)
                return False
            if failed and previous.get("active") and previous.get("sent_at"):
                if now - timestamp(previous["sent_at"]) < timedelta(
                    hours=self.config["repeat_after_hours"]
                ):
                    return False
            entry = {
                "active": True,
                "pending_failure": failed,
                "notified": notified,
                "observed_at": now.isoformat(),
                "details": safe_details(details),
            }
            # Preserve pending failures/recoveries on SMTP errors. Mark delivery
            # only after the server accepts every recipient; retries are at-least-once.
            ledger["issues"][issue] = entry
            atomic_json(path, ledger)
            self.sender(self.config, issue, "failure" if failed else "recovered", entry["details"])
            if failed:
                entry.update(notified=True, sent_at=now.isoformat())
            else:
                ledger["issues"].pop(issue, None)
            atomic_json(path, ledger)
            return True


def unit_failure(reporter: Reporter, unit: str) -> None:
    require(UNIT.fullmatch(unit) is not None, "Invalid monitored service unit")
    if os.environ.get("MONITOR_UNIT") == unit:
        # systemd >=251 snapshots the triggering invocation. A fast next run
        # may already have replaced `systemctl show`'s current state.
        fields = {
            key: os.environ.get(variable, "unknown")
            for key, variable in (
                ("Result", "MONITOR_SERVICE_RESULT"),
                ("ExecMainStatus", "MONITOR_EXIT_STATUS"),
                ("InvocationID", "MONITOR_INVOCATION_ID"),
            )
        }
    else:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=Result,ExecMainStatus,InvocationID"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    category = {
        reporter.config["crawler_unit"]: "crawl",
        reporter.config["backup_unit"]: "backup",
    }.get(unit)
    details = {
        "result": fields.get("Result", "unknown"),
        "exit_status": fields.get("ExecMainStatus", "unknown"),
        "invocation": fields.get("InvocationID", "unknown"),
    }
    if category:
        path = Path(reporter.config["deployment_state_dir"]) / (
            "crawler.json" if category == "crawl" else "backup.json"
        )
        if path.exists():
            latest = read_json(path)
            if latest.get("systemd_invocation") == fields.get("InvocationID"):
                details.update(safe_details(latest))
    reporter.event("operation:" + category if category else "unit:" + unit, True, details)


def tls_expiration(host: str, *, port: int = 443, ca_file: str | None = None) -> float:
    """Verified peer/SNI certificate only; parent bounds DNS and the whole probe."""
    context = ssl.create_default_context(cafile=ca_file)
    with socket.create_connection((host, port), timeout=5) as connection:
        with context.wrap_socket(connection, server_hostname=host) as secure:
            certificate = secure.getpeercert()
            require(bool(certificate) and "notAfter" in certificate, "Missing TLS expiration")
            assert certificate is not None
            return ssl.cert_time_to_seconds(str(certificate["notAfter"]))


def monitor_host(reporter: Reporter) -> None:
    """Local early warnings, never a substitute for an outside-VM monitor."""
    config = reporter.config
    for index, value in enumerate(config.get("disk_paths", [])):
        details: dict[str, Any] = {"id": f"filesystem-{index}"}
        try:
            usage = shutil.disk_usage(value)
            free_percent = 100 * usage.free / usage.total
            details.update(free_bytes=usage.free, free_percent=round(free_percent, 2))
            failed = (
                usage.free < config["disk_min_free_bytes"]
                or free_percent < config["disk_min_free_percent"]
            )
            statvfs = getattr(os, "statvfs", None)
            if statvfs is not None:
                counts = statvfs(value)
                if counts.f_files:
                    inodes = 100 * counts.f_favail / counts.f_files
                    details["free_inode_percent"] = round(inodes, 2)
                    failed = failed or inodes < config["disk_min_free_inode_percent"]
            details["status"] = "low_disk_space" if failed else "disk_healthy"
        except (OSError, ZeroDivisionError) as error:
            failed = True
            details.update(status="disk_probe_failed", error_type=type(error).__name__)
        # Deliberately outside the probe's exception handler: mail/state failures
        # must not be mistaken for a disk failure or an accepted delivery.
        reporter.event(f"host:disk-{index}", failed, details)
    host = config.get("tls_host")
    if host:
        command = [sys.executable, str(Path(__file__).resolve()), "probe-tls", "--probe-host", host]
        if config.get("tls_ca_file"):
            command += ["--probe-ca-file", config["tls_ca_file"]]
        details = {}
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
            require(len(result.stdout) <= 100, "Invalid TLS probe output")
            expires = json.loads(result.stdout)
            require(type(expires) in (int, float), "Invalid TLS probe expiration")
            remaining = datetime.fromtimestamp(expires, timezone.utc) - reporter.now()
            details = {"remaining_days": round(remaining.total_seconds() / 86400, 2)}
            failed = remaining <= timedelta(days=config["tls_warn_days"])
            details["status"] = "tls_expiring" if failed else "tls_healthy"
        except (ValueError, OSError, OverflowError, subprocess.SubprocessError) as error:
            failed = True
            details.update(status="tls_probe_failed", error_type=type(error).__name__)
        reporter.event("host:tls", failed, details)


def monitor(reporter: Reporter) -> None:
    directory = Path(reporter.config["state_dir"])
    now = reporter.now()
    with state_lock(directory):
        path = directory / "monitor.json"
        marker = read_json(path) if path.exists() else {"started_at": now.isoformat()}
        atomic_json(path, marker)
    start = timestamp(marker["started_at"])
    state = Path(reporter.config["deployment_state_dir"])
    private(state, directory=True)
    monitor_host(reporter)
    # Preserve the success watermark separately from the latest (possibly failed) run.
    for category, enabled, filename, hours in (
        ("crawl", True, "crawler-success.json", reporter.config["crawl_max_age_hours"]),
        (
            "backup",
            reporter.config.get("monitor_backups", False),
            "backup-success.json",
            reporter.config["backup_max_age_hours"],
        ),
    ):
        if not enabled:
            continue
        success_file = state / filename
        success = read_json(success_file) if success_file.exists() else {}
        if success:
            require(
                success.get("status") == "succeeded" and success.get("cleanup_complete") is True,
                "Invalid success watermark",
            )
        if category == "crawl" and not success and (state / "crawler.json").exists():
            legacy = read_json(state / "crawler.json")
            if legacy.get("status") == "succeeded" and legacy.get("cleanup_complete"):
                success = legacy
        completed = timestamp(success["finished_at"]) if success else start
        require(
            completed <= now + timedelta(minutes=5), "Future success timestamp; check host clock"
        )
        reporter.event(
            "stale:" + category,
            now - completed > timedelta(hours=hours),
            {
                "status": "missing_success",
                "id": success.get("id", "none"),
            },
        )
    for category, filename in (("crawl", "crawler.json"), ("backup", "backup.json")):
        if category == "backup" and not reporter.config.get("monitor_backups", False):
            continue
        path = state / filename
        if not path.exists():
            continue
        latest = read_json(path)
        status = latest.get("status")
        if status not in {"succeeded", "failed", "timed_out", "interrupted", "cleanup_failed"}:
            continue
        if status == "succeeded":
            ledger_file = directory / "alerts.json"
            ledger = read_json(ledger_file) if ledger_file.exists() else {}
            previous = ledger.get("issues", {}).get("operation:" + category, {})
            if previous.get("active") and timestamp(latest["finished_at"]) < timestamp(
                previous["observed_at"]
            ):
                continue  # A preflight failure must not be cleared by an older success.
        reporter.event("operation:" + category, status != "succeeded", latest)
    # Retry durable pending delivery without installing immediate restart loops.
    # Successful delivery has a cooldown; unresolved incidents are reminders.
    ledger_file = directory / "alerts.json"
    ledger = read_json(ledger_file) if ledger_file.exists() else {}
    for issue, entry in ledger.get("issues", {}).items():
        if entry.get("active") and issue != "unit:" + reporter.config["monitor_unit"]:
            reporter.event(issue, entry.get("pending_failure", True), entry["details"])
    reporter.event(
        "unit:" + reporter.config["monitor_unit"], False, {"status": "monitor_succeeded"}
    )
    with state_lock(directory):
        marker["last_completed_at"] = reporter.now().isoformat()
        atomic_json(directory / "monitor.json", marker)


def notify_deployment(config_path: Path | None, record: dict[str, Any], *, failed: bool) -> None:
    """Best effort only; bound DNS/SMTP in a child, never change deployment outcome."""
    if config_path is None:
        return
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "event",
        "--config",
        str(config_path),
        "--issue",
        "operation:deployment",
        "--status",
        "failure" if failed else "recovered",
    ]
    for key, value in safe_details(record).items():
        command += ["--detail", f"{key}={value}"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60, check=False)
        require(result.returncode == 0, "Notifier returned nonzero")
    except Exception:
        print(
            "Email notification failed; deployment outcome is unchanged. "
            "Inspect notifier configuration/service.",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("test", "event", "unit-failure", "monitor", "probe-tls")
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--probe-host")
    parser.add_argument("--probe-ca-file")
    parser.add_argument("--issue")
    parser.add_argument("--status", choices=("failure", "recovered"))
    parser.add_argument("--unit")
    parser.add_argument("--detail", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "probe-tls":
            require(hostname(args.probe_host), "Invalid TLS probe hostname")
            print(json.dumps(tls_expiration(args.probe_host, ca_file=args.probe_ca_file)))
            return 0
        require(args.config is not None, "A private alerts configuration is required")
        config = load_config(args.config)
        reporter = Reporter(config)
        if args.command == "test":
            send_email(config, "delivery-test", "test", {})
        elif args.command == "unit-failure":
            require(bool(args.unit), "Specify the failed unit")
            unit_failure(reporter, args.unit)
        elif args.command == "monitor":
            monitor(reporter)
        else:
            require(bool(args.issue) and bool(args.status), "Specify alert issue and status")
            details = dict(item.split("=", 1) for item in args.detail)
            reporter.event(args.issue, args.status == "failure", details)
        return 0
    except Exception as error:
        # SMTP server errors can echo addresses, credentials or message content.
        print(
            f"Email alert/monitor failed ({type(error).__name__}); "
            "inspect private configuration and service status.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
