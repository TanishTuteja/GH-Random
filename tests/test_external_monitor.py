"""Independent monitoring contracts; no real GitHub, public site or mail provider."""

from __future__ import annotations

import importlib.util
import json
import ssl
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NOW = 1792324800


@pytest.fixture
def module():
    # Host scripts are siblings, not an installed application package.
    sys.path.insert(0, str(ROOT / "deployment"))
    try:
        spec = importlib.util.spec_from_file_location(
            "external_monitor_test", ROOT / "deployment/external_monitor.py"
        )
        assert spec is not None and spec.loader is not None
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        yield result
    finally:
        sys.path.remove(str(ROOT / "deployment"))


class API:
    repository = "example/project"

    def __init__(self, module):
        self.module = module
        self.statuses: list = []
        self.calls: list = []
        self.deployment = {
            "id": 10,
            "environment": module.ENVIRONMENT,
            "production_environment": False,
            "creator": {"id": 20},
        }
        self.issue = {
            "number": 30,
            "title": module.TITLE,
            "body": module.MARKER
            + json.dumps({"v": 1, "active": [], "notified": False, "pending": False, "sent_at": 0}),
        }

    def get(self, path):
        if path.endswith("/statuses?per_page=1"):
            return self.statuses
        if path.endswith("/deployments/10"):
            return self.deployment
        if path.endswith("/issues/30"):
            return self.issue
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append((path, body))
        return {"id": 100}

    def patch(self, path, body):
        assert path.endswith("/issues/30")
        self.issue.update(body)
        return self.issue

    def heartbeat(self, **overrides):
        value = {"v": 1, "h": NOW, "c": NOW, "b": NOW, **overrides}
        self.statuses = [
            {
                "state": "success",
                "environment": self.module.ENVIRONMENT,
                "description": json.dumps(value),
                "creator": {"id": 20},
                "created_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            }
        ]


@pytest.fixture
def api(module):
    return API(module)


@pytest.fixture
def config():
    return {
        "deployment_id": 10,
        "telemetry_creator_id": 20,
        "issue_number": 30,
        "origin": "https://example.com",
        "smtp": {},
    }


def private_json(path: Path, value: Any):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def test_publish_preserves_watermarks_after_failure_and_local_loss(module, api, config, tmp_path):
    state, alerts = tmp_path / "deployments", tmp_path / "alerts"
    state.mkdir(mode=0o700)
    alerts.mkdir(mode=0o700)
    config.update(deployment_state_dir=str(state), alerts_state_dir=str(alerts))
    private_json(
        alerts / "monitor.json",
        {"last_completed_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat()},
    )
    private_json(state / "crawler.json", {"status": "failed", "raw_secret": "never published"})
    api.heartbeat(c=NOW - 1, b=NOW - 2, h=NOW - 3)
    result = module.publish(api, config, NOW)
    assert result == {"v": 1, "h": NOW, "c": NOW - 1, "b": NOW - 2}
    assert len(api.calls[-1][1]["description"]) <= 140
    assert "never" not in str(api.calls)
    assert api.calls[-1][1]["auto_inactive"] is False


def test_unproven_or_future_local_success_refused(module, api, config, tmp_path):
    state, alerts = tmp_path / "deployments", tmp_path / "alerts"
    state.mkdir(mode=0o700)
    alerts.mkdir(mode=0o700)
    config.update(deployment_state_dir=str(state), alerts_state_dir=str(alerts))
    private_json(
        alerts / "monitor.json",
        {"last_completed_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat()},
    )
    for value in (
        {"status": "failed", "cleanup_complete": True},
        {
            "status": "succeeded",
            "cleanup_complete": True,
            "finished_at": datetime.fromtimestamp(NOW + 301, timezone.utc).isoformat(),
        },
    ):
        private_json(state / "backup-success.json", value)
        with pytest.raises(ValueError):
            module.publish(api, config, NOW)
    assert not api.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "failure"),
        ("creator", {"id": 99}),
        ("environment", "production"),
        ("description", '{"v":1}'),
    ],
)
def test_untrusted_latest_status_never_falls_back(module, api, field, value):
    api.heartbeat()
    api.statuses[0][field] = value
    with pytest.raises(ValueError):
        module.latest(api, 10, 20, NOW)


@pytest.mark.parametrize("bad", [True, -1, 1.1, "123", NOW + 301])
def test_bad_epoch(module, bad):
    with pytest.raises(ValueError):
        module.telemetry({"v": 1, "h": bad, "c": NOW, "b": NOW}, NOW)


def test_external_checks_site_and_each_success_independently(module, api, config, monkeypatch):
    monkeypatch.setattr(module, "probe_site", lambda origin: None)
    api.heartbeat()
    assert module.checks(api, config, NOW) == []
    api.heartbeat(h=NOW - 10801, c=NOW - 43201, b=NOW - 129601)
    assert module.checks(api, config, NOW) == ["backup", "crawl", "host"]
    monkeypatch.setattr(
        module, "probe_site", lambda origin: (_ for _ in ()).throw(OSError("private DNS info"))
    )
    api.statuses = []
    assert module.checks(api, config, NOW) == ["site", "telemetry"]


def test_incident_delivery_retry_cooldown_reminder_and_recovery(module, api, config):
    sent = []

    def sender(*args):
        sent.append(args)

    assert not module.report(api, config, [], NOW, sender)
    assert module.report(api, config, ["site"], NOW, sender)
    assert not module.report(api, config, ["site"], NOW + 10, sender)
    assert module.report(api, config, ["site"], NOW + 21600, sender)
    assert module.report(api, config, [], NOW + 21601, sender)
    assert not module.report(api, config, [], NOW + 21602, sender)
    assert [args[2] for args in sent] == ["failure", "failure", "recovered"]


def test_changed_failure_email_is_retried_even_during_cooldown(module, api, config):
    module.report(api, config, ["site"], NOW, lambda *args: None)

    def broken(*args):
        raise OSError("private mailer output")

    with pytest.raises(OSError):
        module.report(api, config, ["crawl", "site"], NOW + 10, broken)
    assert module.read_ledger(api.issue, NOW + 10)["pending"] is True
    sent = []
    assert module.report(api, config, ["crawl", "site"], NOW + 11, lambda *args: sent.append(args))
    assert len(sent) == 1
    assert not module.read_ledger(api.issue, NOW + 11)["pending"]


def test_unsent_failure_recovers_silently(module, api, config):
    def broken(*args):
        raise OSError()

    with pytest.raises(OSError):
        module.report(api, config, ["site"], NOW, broken)
    assert not module.report(api, config, [], NOW + 1, broken)
    assert not module.read_ledger(api.issue, NOW + 1)["pending"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://example.com:443",
        "https://user@example.com",
        "https://example.com/path",
        "https://example.com?x=1",
    ],
)
def test_origin_is_public_canonical_https(module, url):
    with pytest.raises(ValueError):
        module.public_origin(url)


def test_probe_requires_real_catalog_result_and_never_follows_page_redirects(module, monkeypatch):
    monkeypatch.setattr(module, "public_addresses", lambda host: None)
    calls = []

    def read(url):
        calls.append(url)
        return (
            ("text/html", b"GH Random")
            if url.endswith("/")
            else (
                "application/json",
                b'{"github_id":1,"full_name":"a/b","html_url":"https://github.com/a/b"}',
            )
        )

    monkeypatch.setattr(module, "read_https", read)
    module.probe_site("https://example.com")
    assert len(calls) == 2 and "language=any" in calls[-1]
    monkeypatch.setattr(module, "read_https", lambda url: ("text/html", b"fake"))
    with pytest.raises(ValueError):
        module.probe_site("https://example.com")


def test_enabled_monitor_missing_smtp_fails_closed(module, tmp_path, monkeypatch):
    monkeypatch.delenv("GH_MONITOR_SMTP_CONFIG", raising=False)
    with pytest.raises(ValueError):
        module.smtp_from_environment(tmp_path)


def test_publisher_requests_only_deployments_permission(module, api, tmp_path, monkeypatch):
    captured = []

    class Credential:
        def __init__(self, repository, options):
            captured.append((repository, options))

    monkeypatch.setitem(
        sys.modules,
        "github_api",
        SimpleNamespace(Credential=Credential, GitHubAPI=lambda *args, **kwargs: api),
    )
    path = tmp_path / "config.json"
    private_json(
        path,
        {
            "repository": api.repository,
            "credential": {
                "type": "app",
                "app_id": 1,
                "client_id": "Iv1.fixture",
                "installation_id": 2,
                "private_key_file": str(tmp_path / "unused-key"),
                "permissions": {"contents": "read", "deployments": "write"},
            },
        },
    )
    monkeypatch.setattr(sys, "argv", ["external_monitor.py", "publish", "--config", str(path)])
    monkeypatch.setattr(module, "publish", lambda *args: None)
    assert module.main() == 0
    assert captured[0][1]["permissions"] == {"deployments": "write"}
    assert captured[0][1]["client_id"] == "Iv1.fixture"
    private_json(path, {"repository": api.repository, "credential": {"type": "token_file"}})
    assert module.main() == 1


def test_external_failure_and_recovery_are_delivered_over_real_tls(module, api, config, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "external_mail_fixtures", ROOT / "tests/test_notifications.py"
    )
    assert spec is not None and spec.loader is not None
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    cert, key = fixtures.certificate.__wrapped__(tmp_path)
    smtp_spec = importlib.util.spec_from_file_location(
        "external_smtp_fixture", ROOT / ".github/scripts/smtp_fixture.py"
    )
    assert smtp_spec is not None and smtp_spec.loader is not None
    smtp = importlib.util.module_from_spec(smtp_spec)
    smtp_spec.loader.exec_module(smtp)
    with smtp.smtp_server(cert, key) as server:
        config["smtp"] = {
            "smtp_host": "127.0.0.1",
            "smtp_port": server.server_address[1],
            "smtp_security": "ssl",
            "smtp_timeout_seconds": 2,
            "smtp_ca_file": str(cert),
            "from_address": "alerts@example.com",
            "to_addresses": ["operator@example.com"],
        }
        assert module.report(api, config, ["backup", "site"], NOW, module.notifications.send_email)
        assert module.report(api, config, [], NOW + 1, module.notifications.send_email)
        server.wait_for_messages(2)
        assert b"failure: external-monitor" in server.messages[0]
        assert b"recovered: external-monitor" in server.messages[1]
        assert not module.read_ledger(api.issue, NOW + 1)["notified"]


def test_private_address_and_missing_catalog_are_failures(module, monkeypatch):
    monkeypatch.setattr(
        module.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )
    with pytest.raises(ValueError):
        module.public_addresses("example.com")
    monkeypatch.setattr(module, "public_addresses", lambda host: None)

    def read(url):
        return (
            ("text/html", b"GH Random")
            if url.endswith("/")
            else (
                "application/json",
                b'{"detail":"No catalogued repository matches these filters."}',
            )
        )

    monkeypatch.setattr(module, "read_https", read)
    with pytest.raises(ValueError):
        module.probe_site("https://example.com")


def test_https_probe_real_tls_bounded_response_and_redirect_rejection(module, tmp_path):
    # Reuse the actual loopback TLS certificate fixture builder, not a fake SSL context.
    spec = importlib.util.spec_from_file_location(
        "notification_tests", ROOT / "tests/test_notifications.py"
    )
    assert spec is not None and spec.loader is not None
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    cert, key = fixtures.certificate.__wrapped__(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302 if self.path == "/redirect" else 200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Location", "https://example.com/private")
            self.end_headers()
            self.wfile.write(b"x" * (1024 * 1024 + 1) if self.path == "/big" else b"GH Random")

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"https://localhost:{server.server_port}"
        assert module.read_https(origin, ca_file=str(cert)) == ("text/html", b"GH Random")
        with pytest.raises(HTTPError) as error:
            module.read_https(origin + "/redirect", ca_file=str(cert))
        assert error.value.code == 302
        with pytest.raises(URLError) as untrusted:
            module.read_https(origin)  # Untrusted certificate remains rejected.
        assert isinstance(untrusted.value.reason, ssl.SSLCertVerificationError)
        with pytest.raises(ValueError, match="response exceeds limit"):
            module.read_https(origin + "/big", ca_file=str(cert))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_independent_workflow_is_trusted_serialized_and_least_privilege():
    workflow = yaml.load(
        (ROOT / ".github/workflows/monitor.yaml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    job = workflow["jobs"]["monitor"]
    assert job["permissions"] == {"contents": "read", "deployments": "read", "issues": "write"}
    assert (
        job["if"] == "${{ github.ref == 'refs/heads/main' && vars.MONITORING_ENABLED == 'true' }}"
    )
    assert job["environment"] == {"name": "monitoring", "deployment": "false"}
    assert job["timeout-minutes"] == "5"
    assert job["steps"][0]["with"] == {"ref": "${{ github.sha }}", "persist-credentials": "false"}
    for step in job["steps"]:
        if "uses" in step:
            assert len(step["uses"].rsplit("@", 1)[1]) == 40


def test_telemetry_unit_has_no_docker_or_write_access_and_runs_after_local_monitor():
    unit = (ROOT / "deployment/systemd/gh-random-telemetry.service").read_text()
    timer = (ROOT / "deployment/systemd/gh-random-telemetry.timer").read_text()
    assert "ProtectSystem=strict" in unit and "NoNewPrivileges=true" in unit
    assert "ReadWritePaths=" not in unit
    assert "-/run/docker.sock" in unit and "-/etc/gh-random/smtp-password" in unit
    assert "TimeoutStartSec=120s" in unit
    assert "/usr/local/lib/gh-random/external_monitor.py publish" in unit
    assert "OnCalendar=*-*-* *:20:00 UTC" in timer and "Persistent=true" in timer
