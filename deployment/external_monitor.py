"""Outside-VM checks and safe GitHub telemetry; no application routes or dependencies."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

try:
    from . import notifications
except ImportError:
    import notifications  # type: ignore[no-redef]

ENVIRONMENT = "gh-random-monitoring"
TITLE = "[Operations] External monitor state"
MARKER = "<!-- gh-random-monitor:v1 -->\n"
CODES = {"site", "telemetry", "host", "crawl", "backup"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def integer(value: object, *, zero: bool = False) -> bool:
    return type(value) is int and (0 if zero else 1) <= value < 10**12


def epoch(value: object, now: int) -> int:
    require(integer(value, zero=True), "Invalid telemetry timestamp")
    assert isinstance(value, int)
    require(value <= now + 300, "Future telemetry timestamp")
    return value


def telemetry(value: object, now: int) -> dict[str, int]:
    require(isinstance(value, dict), "Invalid telemetry")
    assert isinstance(value, dict)
    require(
        set(value) == {"v", "h", "c", "b"} and type(value["v"]) is int and value["v"] == 1,
        "Invalid telemetry schema",
    )
    return {"v": 1, **{key: epoch(value[key], now) for key in ("h", "c", "b")}}


def prefix(api: Any) -> str:
    return f"/repos/{api.repository}"


def anchor(api: Any, deployment: int, creator: int) -> dict[str, Any]:
    require(integer(deployment) and integer(creator), "Invalid monitoring identity")
    value = api.get(f"{prefix(api)}/deployments/{deployment}")
    require(
        isinstance(value, dict)
        and value.get("id") == deployment
        and value.get("environment") == ENVIRONMENT
        and value.get("production_environment") is False
        and value.get("creator", {}).get("id") == creator,
        "Monitoring anchor does not belong to the expected telemetry identity",
    )
    return value


def latest(api: Any, deployment: int, creator: int, now: int) -> dict[str, int] | None:
    statuses = api.get(f"{prefix(api)}/deployments/{deployment}/statuses?per_page=1")
    require(isinstance(statuses, list) and len(statuses) <= 1, "Invalid telemetry response")
    if not statuses:
        return None
    value = statuses[0]
    require(
        isinstance(value, dict)
        and value.get("creator", {}).get("id") == creator
        and value.get("state") == "success"
        and value.get("environment") == ENVIRONMENT,
        "Unexpected telemetry author/status; do not fall back to an older heartbeat",
    )
    created = notifications.timestamp(value["created_at"])
    require(created.timestamp() <= now + 300, "Future GitHub telemetry creation timestamp")
    description = value.get("description")
    require(
        isinstance(description, str) and len(description) <= 140, "Invalid telemetry description"
    )
    assert isinstance(description, str)
    return telemetry(json.loads(description), now)


def local_success(directory: Path, filename: str, now: int) -> int:
    path = directory / filename
    if not path.exists():
        return 0
    value = notifications.read_json(path)
    require(
        value.get("status") == "succeeded" and value.get("cleanup_complete") is True,
        "Invalid operational success watermark",
    )
    result = int(notifications.timestamp(value["finished_at"]).timestamp())
    return epoch(result, now)


def publish(api: Any, config: dict[str, Any], now: int) -> dict[str, int]:
    """Publish only proven success watermarks, never last-run errors or logs."""
    deployment, creator = config["deployment_id"], config["telemetry_creator_id"]
    anchor(api, deployment, creator)
    previous = latest(api, deployment, creator, now) or {"v": 1, "h": 0, "c": 0, "b": 0}
    state, alerts = Path(config["deployment_state_dir"]), Path(config["alerts_state_dir"])
    notifications.private(state, directory=True)
    notifications.private(alerts, directory=True)
    marker = notifications.read_json(alerts / "monitor.json")
    host = epoch(int(notifications.timestamp(marker["last_completed_at"]).timestamp()), now)
    current = {
        "v": 1,
        "h": host,
        "c": local_success(state, "crawler-success.json", now),
        "b": local_success(state, "backup-success.json", now),
    }
    # A failed operation, missing local file, rollback, or recovered VM must not
    # erase the last proven success visible to the independent checker.
    current = {key: max(current[key], previous[key]) for key in current}
    body = json.dumps(current, separators=(",", ":"), sort_keys=True)
    require(len(body) <= 140, "Telemetry exceeds GitHub status limit")
    result = api.post(
        f"{prefix(api)}/deployments/{deployment}/statuses",
        {
            "state": "success",
            "description": body,
            "environment": ENVIRONMENT,
            "auto_inactive": False,
        },
    )
    require(
        isinstance(result, dict) and integer(result.get("id")), "Telemetry was not acknowledged"
    )
    return current


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def public_origin(value: str) -> str:
    parsed = urlsplit(value)
    require(
        parsed.scheme == "https"
        and parsed.hostname is not None
        and notifications.hostname(parsed.hostname)
        and "." in parsed.hostname
        and re.fullmatch(r"[0-9.]+", parsed.hostname) is None
        and parsed.netloc == parsed.hostname
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment,
        "Use a canonical public HTTPS origin, without port, credentials, path or query",
    )
    return f"https://{parsed.hostname}"


def public_addresses(host: str) -> None:
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    require(bool(addresses), "No public site addresses")
    require(
        all(ipaddress.ip_address(value[4][0]).is_global for value in addresses),
        "The monitored origin must not resolve to a private/local address",
    )


def read_https(url: str, *, ca_file: str | None = None) -> tuple[str, bytes]:
    """TLS verified, redirects refused, no auth/cookies, bounded response/request."""
    opener = build_opener(
        NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=ca_file))
    )
    request = Request(
        url, headers={"User-Agent": "GH-Random-external-monitor/1", "Cache-Control": "no-cache"}
    )
    with opener.open(request, timeout=10) as response:
        require(response.status == 200, "Unexpected site response")
        data = response.read(1024 * 1024 + 1)
        require(len(data) <= 1024 * 1024, "Site response exceeds limit")
        return response.headers.get_content_type(), data


def probe_site(origin: str) -> None:
    origin = public_origin(origin)
    host = urlsplit(origin).hostname
    assert host is not None
    public_addresses(host)
    content_type, page = read_https(origin + "/")
    require(content_type == "text/html" and b"GH Random" in page, "Unexpected homepage")
    content_type, data = read_https(
        origin + "/api/random-repository?language=any&star_bucket=any&pushed_bucket=any"
    )
    require(content_type == "application/json", "Discovery did not return JSON")
    value = json.loads(data)
    require(
        isinstance(value, dict)
        and integer(value.get("github_id"))
        and isinstance(value.get("full_name"), str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["full_name"]) is not None
        and value.get("html_url") == "https://github.com/" + value["full_name"],
        "Discovery has no active catalog result",
    )


def checks(api: Any, config: dict[str, Any], now: int) -> list[str]:
    failures: list[str] = []
    try:
        probe_site(config["origin"])
    except Exception:
        failures.append("site")
    try:
        anchor(api, config["deployment_id"], config["telemetry_creator_id"])
        value = latest(api, config["deployment_id"], config["telemetry_creator_id"], now)
        require(value is not None, "No telemetry has been published")
        assert value is not None
        for code, key, seconds in (
            ("host", "h", 3 * 3600),
            ("crawl", "c", 12 * 3600),
            ("backup", "b", 36 * 3600),
        ):
            if not value[key] or now - value[key] > seconds:
                failures.append(code)
    except Exception:
        failures.append("telemetry")
    return sorted(failures)


def read_ledger(issue: dict[str, Any], now: int) -> dict[str, Any]:
    require(
        issue.get("title") == TITLE and "pull_request" not in issue, "Unexpected monitoring issue"
    )
    body = issue.get("body")
    require(
        isinstance(body, str) and len(body) <= 16384 and body.startswith(MARKER),
        "Invalid monitoring ledger",
    )
    assert isinstance(body, str)
    ledger = json.loads(body[len(MARKER) :])
    require(
        isinstance(ledger, dict)
        and set(ledger) == {"v", "active", "notified", "sent_at", "pending"},
        "Invalid incident ledger",
    )
    require(
        type(ledger["v"]) is int
        and ledger["v"] == 1
        and type(ledger["notified"]) is bool
        and type(ledger["pending"]) is bool,
        "Invalid incident schema",
    )
    require(
        isinstance(ledger["active"], list)
        and len(ledger["active"]) <= 5
        and all(code in CODES for code in ledger["active"]),
        "Invalid incident codes",
    )
    epoch(ledger["sent_at"], now)
    return ledger


def write_ledger(api: Any, issue: int, ledger: dict[str, Any]) -> None:
    response = api.patch(
        f"{prefix(api)}/issues/{issue}",
        {
            "body": MARKER + json.dumps(ledger, sort_keys=True, separators=(",", ":")),
            "state": "open",
        },
    )
    require(
        isinstance(response, dict) and response.get("number") == issue,
        "Incident state was not acknowledged",
    )


def report(api: Any, config: dict[str, Any], failures: list[str], now: int, sender: Any) -> bool:
    """Durable at-least-once SMTP: persist pending before send, acknowledge after."""
    issue = config["issue_number"]
    require(integer(issue), "A positive monitoring issue number is required")
    require(
        failures == sorted(set(failures)) and all(code in CODES for code in failures),
        "Only fixed monitoring failure codes may be reported",
    )
    ledger = read_ledger(api.get(f"{prefix(api)}/issues/{issue}"), now)
    failed = bool(failures)
    due = (
        failed
        and (
            ledger["pending"]
            or not ledger["notified"]
            or failures != ledger["active"]
            or now - ledger["sent_at"] >= 21600
        )
    ) or (not failed and ledger["notified"])
    if not due:
        if ledger["active"] != failures or ledger["pending"]:
            ledger["active"] = failures
            ledger["pending"] = False
            write_ledger(api, issue, ledger)
        return False
    ledger["active"] = failures
    ledger["pending"] = True
    write_ledger(api, issue, ledger)
    sender(
        config["smtp"],
        "external-monitor",
        "failure" if failed else "recovered",
        {"status": ", ".join(failures) if failed else "all_checks_healthy"},
    )
    ledger.update(notified=failed, sent_at=now, pending=False)
    write_ledger(api, issue, ledger)
    # The issue supplies an independent, durable human-visible event trail;
    # comments contain fixed codes only, never response bodies/logs/addresses.
    api.post(
        f"{prefix(api)}/issues/{issue}/comments",
        {
            "body": (
                f"External monitor {'failure' if failed else 'recovered'}: "
                f"{', '.join(failures) if failed else 'all checks healthy'}. "
                "SMTP accepted; inbox delivery still depends on the provider."
            )
        },
    )
    return True


def smtp_from_environment(directory: Path) -> dict[str, Any]:
    raw = os.environ.get("GH_MONITOR_SMTP_CONFIG", "")
    require(
        bool(raw) and len(raw) <= 16384, "Enabled monitoring requires private SMTP configuration"
    )
    value = json.loads(raw)
    require(isinstance(value, dict), "Invalid external SMTP configuration")
    allowed = {
        "smtp_host",
        "smtp_port",
        "smtp_security",
        "smtp_username",
        "from_address",
        "to_addresses",
        "smtp_timeout_seconds",
    }
    require(not set(value) - allowed, "Unexpected external SMTP field")
    value.update(
        format=1,
        state_dir=str(directory / "alerts"),
        deployment_state_dir=str(directory / "deployments"),
    )
    if value.get("smtp_username"):
        password = os.environ.get("GH_MONITOR_SMTP_PASSWORD", "")
        require(bool(password) and len(password) <= 4096, "External SMTP password is required")
        path = directory / "smtp-password"
        path.write_text(password, encoding="utf-8")
        path.chmod(0o600)
        value["smtp_password_file"] = str(path)
    path = directory / "smtp.json"
    notifications.atomic_json(path, value)
    return notifications.load_config(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("publish", "check", "create-anchor", "create-issue"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        from github_api import Credential, GitHubAPI  # type: ignore[import-not-found]

        config = notifications.read_json(args.config)
        repository = config["repository"]
        require(isinstance(repository, str), "Invalid monitoring repository")
        if args.command in {"check", "create-issue"}:
            credential = Credential.from_env(repository)
        else:
            require(
                config.get("credential", {}).get("type") == "app",
                "Telemetry requires the dedicated deployment App, not crawler/PAT credentials",
            )
            options = {**config["credential"], "permissions": {"deployments": "write"}}
            credential = Credential(repository, options)
        api = GitHubAPI(repository, credential, timeout=15)
        now = int(datetime.now(timezone.utc).timestamp())
        if args.command == "publish":
            publish(api, config, now)
        elif args.command == "create-anchor":
            value = api.post(
                f"{prefix(api)}/deployments",
                {
                    "ref": "main",
                    "task": "monitor",
                    "auto_merge": False,
                    "required_contexts": [],
                    "environment": ENVIRONMENT,
                    "description": "Operational success telemetry; not an application deployment",
                    "transient_environment": False,
                    "production_environment": False,
                },
            )
            print(
                json.dumps(
                    {"deployment_id": value["id"], "telemetry_creator_id": value["creator"]["id"]}
                )
            )
        elif args.command == "create-issue":
            ledger = {"v": 1, "active": [], "notified": False, "sent_at": 0, "pending": False}
            value = api.post(
                f"{prefix(api)}/issues", {"title": TITLE, "body": MARKER + json.dumps(ledger)}
            )
            print(json.dumps({"issue_number": value["number"]}))
        else:
            require(integer(config.get("issue_number")), "A durable monitoring issue is required")
            public_origin(config["origin"])
            failures = checks(api, config, now)
            with tempfile.TemporaryDirectory(prefix="gh-random-monitor-") as temporary:
                config["smtp"] = smtp_from_environment(Path(temporary))
                report(api, config, failures, now, notifications.send_email)
            print("External monitor: " + (", ".join(failures) if failures else "healthy"))
            return 1 if failures else 0
        return 0
    except Exception as error:
        print(
            f"External monitoring failed ({type(error).__name__}); "
            "inspect private configuration and workflow permissions.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
