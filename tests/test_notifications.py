"""Exercise real TLS on loopback; these tests never contact an email provider."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import smtplib
import ssl
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from pathlib import Path
from types import ModuleType

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def alerts() -> ModuleType:
    return load("notifications_tools", ROOT / "deployment/notifications.py")


@pytest.fixture
def smtp() -> ModuleType:
    return load("smtp_fixture", ROOT / ".github/scripts/smtp_fixture.py")


def private_text(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def config(tmp_path: Path) -> dict:
    state = tmp_path / "deployments"
    state.mkdir(mode=0o700)
    return {
        "format": 1,
        "smtp_host": "127.0.0.1",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "from_address": "alerts@example.com",
        "to_addresses": ["operator@example.com"],
        "state_dir": str(tmp_path / "alerts"),
        "deployment_state_dir": str(state),
        "smtp_timeout_seconds": 2,
        "repeat_after_hours": 6,
        "crawl_max_age_hours": 12,
        "backup_max_age_hours": 36,
        "crawler_unit": "gh-random-crawler.service",
        "backup_unit": "gh-random-backup.service",
        "monitor_unit": "gh-random-monitor.service",
    }


@pytest.fixture
def certificate(tmp_path: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path = private_text(
        tmp_path / "test-key.pem",
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )
    cert_path = private_text(
        tmp_path / "test-cert.pem", cert.public_bytes(serialization.Encoding.PEM).decode()
    )
    return cert_path, key_path


@pytest.mark.parametrize("mode", ["ssl", "starttls"])
def test_real_tls_smtp_delivery_and_no_secret_leak(
    alerts, smtp, config, certificate, tmp_path, mode
):
    cert, key = certificate
    secret = private_text(tmp_path / "password", "ci-only-password\n")
    with smtp.smtp_server(cert, key, mode=mode) as server:
        config.update(
            smtp_port=server.server_address[1],
            smtp_security=mode,
            smtp_ca_file=str(cert),
            smtp_username="ci-only-user",
            smtp_password_file=str(secret),
        )
        path = private_text(tmp_path / "config.json", json.dumps(config))
        reporter = alerts.Reporter(alerts.load_config(path))
        assert reporter.event(
            "operation:crawl",
            True,
            {"id": "run-1", "DATABASE_URL": "SECRET-URL", "password": "ci-only-password"},
        )
        server.wait_for_messages(1)
        message = message_from_bytes(server.messages[0])
        payload = message.get_payload(decode=True)
        assert isinstance(payload, bytes)
        body = payload.decode()
        assert "run-1" in body and "journalctl" in body
        assert "SECRET-URL" not in body and "ci-only-password" not in body
        assert "failure" in message["Subject"]


def test_untrusted_certificate_is_rejected(alerts, smtp, config, certificate):
    cert, key = certificate
    with smtp.smtp_server(cert, key) as server:
        config["smtp_port"] = server.server_address[1]
        with pytest.raises(ssl.SSLCertVerificationError):
            alerts.send_email(config, "test", "test", {})
        assert not server.messages


def test_starttls_never_falls_back_to_plaintext(alerts, smtp, config, certificate):
    cert, key = certificate
    with smtp.smtp_server(cert, key, mode="no-starttls") as server:
        config.update(
            smtp_port=server.server_address[1], smtp_security="starttls", smtp_ca_file=str(cert)
        )
        with pytest.raises(smtplib.SMTPNotSupportedError):
            alerts.send_email(config, "test", "test", {})
        assert not server.messages


def test_recipient_refusal_does_not_record_delivery(alerts, smtp, config, certificate):
    cert, key = certificate
    with smtp.smtp_server(cert, key, refuse=True) as server:
        config.update(smtp_port=server.server_address[1], smtp_ca_file=str(cert))
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            alerts.Reporter(config).event("operation:crawl", True, {})
    ledger = alerts.read_json(Path(config["state_dir"]) / "alerts.json")
    assert not ledger["issues"]["operation:crawl"].get("sent_at")


@pytest.mark.parametrize(
    "field,value",
    [
        ("smtp_security", "none"),
        ("smtp_port", 0),
        ("smtp_port", True),
        ("smtp_host", "https://smtp.example.com"),
        ("from_address", "a@example.com\r\nBcc:x@example.com"),
        ("to_addresses", []),
        ("state_dir", "relative"),
        ("repeat_after_hours", 0),
        ("smtp_username", "orphaned-user"),
        ("crawler_unit", "--evil.service"),
    ],
)
def test_invalid_configuration_fails_closed(alerts, config, tmp_path, field, value):
    config[field] = value
    path = private_text(tmp_path / "config.json", json.dumps(config))
    with pytest.raises((ValueError, TypeError)):
        alerts.load_config(path)


def test_config_permission_and_symlink_guards(alerts, config, tmp_path):
    path = private_text(tmp_path / "config.json", json.dumps(config))
    assert alerts.load_config(path)["smtp_host"] == "127.0.0.1"
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(ValueError, match="inaccessible"):
            alerts.load_config(path)
        path.chmod(0o600)
        link = tmp_path / "link.json"
        link.symlink_to(path)
        with pytest.raises(ValueError, match="private path"):
            alerts.load_config(link)


def test_cooldown_recovery_and_recurrence(alerts, config):
    sent = []
    clock = [NOW]
    reporter = alerts.Reporter(
        config, sender=lambda *args: sent.append(args[1:]), now=lambda: clock[0]
    )
    assert reporter.event("operation:crawl", True, {"id": "first"})
    assert not reporter.event("operation:crawl", True, {"id": "duplicate"})
    clock[0] += timedelta(hours=6)
    assert reporter.event("operation:crawl", True, {"id": "reminder"})
    assert reporter.event("operation:crawl", False, {"id": "success"})
    assert not reporter.event("operation:crawl", False, {})
    assert reporter.event("operation:crawl", True, {"id": "new-incident"})
    assert [item[1] for item in sent] == ["failure", "failure", "recovered", "failure"]


def test_smtp_outage_preserves_failure_and_recovery_for_retry(alerts, config):
    sent = []
    broken = [True]

    def sender(*args):
        if broken[0]:
            raise OSError("deliberate SMTP outage")
        sent.append(args[2])

    reporter = alerts.Reporter(config, sender=sender, now=lambda: NOW)
    with pytest.raises(OSError):
        reporter.event("operation:deployment", True, {"id": "deployment-1"})
    broken[0] = False
    assert reporter.event("operation:deployment", True, {})
    broken[0] = True
    with pytest.raises(OSError):
        reporter.event("operation:deployment", False, {})
    broken[0] = False
    assert reporter.event("operation:deployment", False, {})
    assert sent == ["failure", "recovered"]


def test_hourly_monitor_covers_no_first_success_staleness_and_recovery(alerts, config):
    sent = []
    clock = [NOW]
    reporter = alerts.Reporter(
        config, sender=lambda *args: sent.append(args[1:]), now=lambda: clock[0]
    )
    alerts.monitor(reporter)
    assert not sent
    clock[0] += timedelta(hours=13)
    alerts.monitor(reporter)
    assert sent[-1][:2] == ("stale:crawl", "failure")
    state = Path(config["deployment_state_dir"])
    alerts.atomic_json(
        state / "crawler-success.json",
        {
            "status": "succeeded",
            "cleanup_complete": True,
            "id": "new-success",
            "finished_at": clock[0].isoformat(),
        },
    )
    alerts.monitor(reporter)
    assert sent[-1][:2] == ("stale:crawl", "recovered")


def test_new_preflight_failure_is_not_cleared_by_old_success(alerts, config):
    sent = []
    reporter = alerts.Reporter(config, sender=lambda *args: sent.append(args[1:]), now=lambda: NOW)
    state = Path(config["deployment_state_dir"])
    alerts.atomic_json(
        state / "crawler.json",
        {
            "status": "succeeded",
            "cleanup_complete": True,
            "id": "older-run",
            "finished_at": (NOW - timedelta(hours=1)).isoformat(),
        },
    )
    reporter.event("operation:crawl", True, {"result": "preflight-failed"})
    alerts.monitor(reporter)
    assert [item[1] for item in sent] == ["failure"]
    alerts.atomic_json(
        state / "crawler.json",
        {
            "status": "succeeded",
            "cleanup_complete": True,
            "id": "next-run",
            "finished_at": NOW.isoformat(),
        },
    )
    alerts.monitor(reporter)
    assert sent[-1][:2] == ("operation:crawl", "recovered")


def test_monitor_retries_pending_delivery_after_outage(alerts, config):
    failed = alerts.Reporter(
        config, sender=lambda *args: (_ for _ in ()).throw(OSError("offline")), now=lambda: NOW
    )
    with pytest.raises(OSError):
        failed.event("operation:deployment", True, {"id": "deploy-1"})
    sent = []
    alerts.monitor(
        alerts.Reporter(config, sender=lambda *args: sent.append(args[1:]), now=lambda: NOW)
    )
    assert sent[0][:2] == ("operation:deployment", "failure")


def test_successful_operation_without_delivered_failure_does_not_email_recovery(alerts, config):
    broken = alerts.Reporter(config, sender=lambda *args: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(OSError):
        broken.event("operation:crawl", True, {})
    sent = []
    good = alerts.Reporter(config, sender=lambda *args: sent.append(args))
    assert not good.event("operation:crawl", False, {})
    assert not sent


def test_backup_monitoring_is_explicit_and_has_its_own_age_policy(alerts, config):
    config["monitor_backups"] = True
    sent = []
    clock = [NOW]
    reporter = alerts.Reporter(
        config, sender=lambda *args: sent.append(args[1:]), now=lambda: clock[0]
    )
    alerts.monitor(reporter)
    clock[0] += timedelta(hours=37)
    alerts.monitor(reporter)
    assert ("stale:backup", "failure") in [item[:2] for item in sent]


def test_corrupt_or_future_success_watermark_is_not_certified(alerts, config):
    state = Path(config["deployment_state_dir"])
    alerts.atomic_json(
        state / "crawler-success.json",
        {
            "status": "succeeded",
            "cleanup_complete": True,
            "finished_at": (NOW + timedelta(days=1)).isoformat(),
        },
    )
    reporter = alerts.Reporter(config, sender=lambda *args: None, now=lambda: NOW)
    with pytest.raises(ValueError, match="Future success timestamp"):
        alerts.monitor(reporter)


def test_alert_lock_excludes_a_second_notifier(alerts, config, monkeypatch):
    clock = iter([0.0, 10.0])
    path = Path(config["state_dir"])
    with alerts.state_lock(path):
        monkeypatch.setattr(alerts.time, "monotonic", lambda: next(clock))
        with pytest.raises(RuntimeError, match="Another notifier"):
            with alerts.state_lock(path):
                pytest.fail("Second notifier acquired the same lock")


def test_unit_failure_uses_only_safe_systemd_fields(alerts, config, monkeypatch):
    sent = []
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, "Result=timeout\nExecMainStatus=9\nInvocationID=abc\nSECRET=hidden\n"
        )

    monkeypatch.setattr(alerts.subprocess, "run", run)
    reporter = alerts.Reporter(config, sender=lambda *args: sent.append(args[1:]))
    alerts.unit_failure(reporter, "gh-random-crawler.service")
    assert sent[0][0] == "operation:crawl" and sent[0][2]["result"] == "timeout"
    assert "hidden" not in str(sent)
    assert calls[0][0] == "systemctl"
    with pytest.raises(ValueError):
        alerts.unit_failure(reporter, "--unsafe")


def test_notification_child_is_bounded_and_cannot_change_deploy_outcome(
    alerts, monkeypatch, capsys
):
    def run(command, **kwargs):
        assert kwargs["timeout"] == 60
        assert "SECRET" not in str(command)
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(alerts.subprocess, "run", run)
    alerts.notify_deployment(
        Path("/private/config.json"), {"id": "safe-id", "password": "SECRET"}, failed=True
    )
    assert "outcome is unchanged" in capsys.readouterr().err


def test_systemd_event_uses_triggering_invocation_not_a_newer_run(alerts, config, monkeypatch):
    for key, value in {
        "MONITOR_UNIT": "gh-random-crawler.service",
        "MONITOR_SERVICE_RESULT": "timeout",
        "MONITOR_EXIT_STATUS": "KILL",
        "MONITOR_INVOCATION_ID": "original-invocation",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        alerts.subprocess, "run", lambda *args, **kwargs: pytest.fail("Queried mutable unit state")
    )
    sent = []
    alerts.unit_failure(
        alerts.Reporter(config, sender=lambda *args: sent.append(args[3])),
        "gh-random-crawler.service",
    )
    assert sent[0]["invocation"] == "original-invocation" and sent[0]["result"] == "timeout"


def test_deployment_cli_failure_sends_metadata_through_bounded_tls_child(
    smtp, config, certificate, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(ROOT / "deployment"))
    deploy = load("alert_deploy_cli", ROOT / "deployment/deploy.py")
    cert, key = certificate
    with smtp.smtp_server(cert, key) as server:
        config.update(smtp_port=server.server_address[1], smtp_ca_file=str(cert))
        path = private_text(tmp_path / "config.json", json.dumps(config))

        class FailedDeployment:
            def __init__(self, args):
                self.attempt_record = {"id": "deploy-42", "phase": "backup", "tag": "v0.1.0"}

            def execute(self):
                raise RuntimeError("deliberate failure; PRIVATE-DIAGNOSTIC")

        monkeypatch.setattr(deploy, "Deployment", FailedDeployment)
        monkeypatch.setattr(deploy.signal, "signal", lambda *args: None)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "deploy.py",
                "apply",
                "--env-file",
                str(tmp_path / "compose.env"),
                "--state-dir",
                config["deployment_state_dir"],
                "--backup-dir",
                str(tmp_path / "backups"),
                "--url",
                "https://example.com",
                "--alerts-config",
                str(path),
            ],
        )
        with pytest.raises(SystemExit) as error:
            deploy.main()
        assert error.value.code == 1
        server.wait_for_messages(1)
        payload = message_from_bytes(server.messages[0]).get_payload(decode=True)
        assert isinstance(payload, bytes)
        assert b"deploy-42" in payload and b"backup" in payload
        assert b"PRIVATE-DIAGNOSTIC" not in payload


def test_cli_redacts_provider_errors(alerts, config, tmp_path, monkeypatch, capsys):
    path = private_text(tmp_path / "config.json", json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["notifications.py", "test", "--config", str(path)])
    monkeypatch.setattr(
        alerts,
        "send_email",
        lambda *args: (_ for _ in ()).throw(RuntimeError("SECRET provider reply")),
    )
    assert alerts.main() == 1
    assert "SECRET" not in capsys.readouterr().err
