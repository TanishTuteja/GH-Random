"""Disposable real pull-to-deploy rehearsal; GitHub/signature oracles are fixtures.

Called only by the existing isolated native AMD64 deployment harness. No GitHub,
SMTP provider, OCI account or existing user database is contacted.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment"))

import pull  # noqa: E402
from install_age import install  # noqa: E402


def check_alert_sandbox(workspace: Path, project: str) -> None:
    """Exercise notifier and attestation runtime writes inside the puller sandbox."""
    source = (ROOT / "deployment/systemd/gh-random-pull.service").read_text()
    settings = dict(
        line.split("=", 1)
        for line in source.splitlines()
        if line.startswith(("ProtectSystem=", "ProtectHome=", "PrivateTmp=", "ReadWritePaths="))
    )
    assert settings["ProtectSystem"] == "strict"
    assert settings["ProtectHome"] == settings["PrivateTmp"] == "true"
    directory = workspace / "pull-alert-sandbox"
    directory.mkdir(mode=0o700)

    def fixture_path(original: str) -> Path:
        return directory / original.lstrip("/")

    writable = [fixture_path(value) for value in settings["ReadWritePaths"].split()]
    alerts = fixture_path("/var/lib/gh-random/alerts")
    blocked = directory / "unrelated"
    for path in [*writable, alerts, blocked]:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "deployment/notifications.py", directory / "notifications.py")
    shutil.copyfile(ROOT / "deployment/attestations.py", directory / "attestations.py")
    # Model the CLI's real config/cache writes, not signature cryptography or
    # remote GitHub responses. The live operator test verifies actual signatures.
    gh = directory / "gh-fixture"
    gh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/python3
            import os
            import sys
            from pathlib import Path

            fallback = Path.home() / ".config" / "gh"
            for variable in (
                "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_DATA_HOME", "XDG_STATE_HOME",
            ):
                location = Path(os.environ.get(variable, str(fallback)))
                location.mkdir(parents=True, exist_ok=True)
                (location / "cli-cache-fixture").write_text("private fixture state")
            if sys.argv[1:] == ["--version"]:
                print("gh version 2.102.0 (2026-09-30)")
                print("https://github.com/cli/cli/releases/tag/v2.102.0")
            else:
                assert sys.argv[1:3] == ["attestation", "verify"]
                print("fixture verified")
            """
        )
    )
    gh.chmod(0o700)
    archive = directory / "subject.tar.gz"
    archive.write_bytes(b"fixture subject; no real release or credential")
    probe = directory / "probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""\
            import errno
            import json
            import os
            from pathlib import Path
            import attestations
            from notifications import Reporter

            # A positive result must not come from disabling the read-only sandbox.
            try:
                Path({str(blocked / "must-stay-readonly")!r}).write_text("forbidden")
            except OSError as error:
                assert error.errno == errno.EROFS, error
            else:
                raise AssertionError("Unrelated state is writable inside the puller sandbox")

            state = Path({str(alerts)!r})
            deliveries = []
            reporter = Reporter(
                {{"state_dir": str(state), "repeat_after_hours": 6}},
                sender=lambda config, issue, status, details: deliveries.append(status),
            )
            issue = "unit:gh-random-pull.service"
            assert reporter.event(issue, True, {{}})
            assert reporter.event(issue, False, {{}})
            assert deliveries == ["failure", "recovered"]
            assert json.loads((state / "alerts.json").read_text())["issues"] == {{}}
            assert (state / "alerts.lock").is_file()
            print("Puller sandbox permits alert locks and failure/recovery ledger writes.")

            # The verifier must create its own private runtime instead of
            # inheriting poisoned read-only config/cache paths or using HOME.
            for variable in (
                "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_DATA_HOME", "XDG_STATE_HOME",
            ):
                os.environ[variable] = str(Path({str(blocked)!r}) / variable)
            metadata = {{
                "repository": "Owner/Repo",
                "commit": "a" * 40,
                "tag": "v0.1.0",
                "digest": "sha256:" + "b" * 64,
                "image": "ghcr.io/owner/repo@sha256:" + "b" * 64,
            }}
            calls = []
            actual_run = attestations.run

            def observe(arguments, environment, **options):
                calls.append(arguments)
                return actual_run(arguments, environment, **options)

            attestations.run = observe
            attestations.verify(
                metadata, Path({str(archive)!r}), Path({str(gh)!r}), token="fixture-token"
            )
            assert len(calls) == 3
            assert calls[1][3] == str(Path({str(archive)!r}).resolve())
            assert calls[2][3] == "oci://" + metadata["image"]
            assert not list(Path({str(blocked)!r}).iterdir())
            print("Attestation CLI config/cache writes pass with protected HOME and filesystem.")
            """
        )
    )
    unit = project + "-pull-alert-sandbox.service"
    try:
        subprocess.run(
            [
                "sudo",
                "systemd-run",
                "--unit=" + unit,
                "--wait",
                "--pipe",
                "--collect",
                "--uid=" + str(os.getuid()),
                "--gid=" + str(os.getgid()),
                "--property=Type=exec",
                "--property=RuntimeMaxSec=30s",
                "--property=TimeoutStopSec=5s",
                "--property=UMask=0077",
                "--property=ProtectSystem=" + settings["ProtectSystem"],
                "--property=ProtectHome=" + settings["ProtectHome"],
                "--property=PrivateTmp=" + settings["PrivateTmp"],
                "--property=ReadWritePaths=" + " ".join(map(str, writable)),
                "/usr/bin/python3",
                "-B",
                str(probe),
            ],
            check=True,
            timeout=60,
        )
    finally:
        subprocess.run(
            ["sudo", "systemctl", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )


def check(
    workspace: Path, release: Path, env_file: Path, state: Path, url: str, project: str
) -> None:
    assert os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux"
    check_alert_sandbox(workspace, project)
    original = json.loads((release / "release.json").read_text())
    assert original["platform"] == "linux/amd64"
    directory = workspace / "pull-rehearsal"
    directory.mkdir(mode=0o700)

    def private(name, contents):
        path = directory / name
        path.write_text(contents)
        path.chmod(0o600)
        return path

    age = install(directory / "age-tools")
    identity = directory / "identity"
    subprocess.run(
        [str(age.with_name("age-keygen")), "--output", str(identity)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    identity.chmod(0o600)
    recipient = subprocess.run(
        [str(age.with_name("age-keygen")), "-y", str(identity)],
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout.decode()
    recipients = private("recipients", recipient)
    object_store = directory / "fake-objects"
    object_store.mkdir(mode=0o700)
    oci = private(
        "oci",
        f"#!{sys.executable}\nimport os, runpy\n"
        f"os.environ['GH_RANDOM_CI_OBJECT_STORE']={str(object_store)!r}\n"
        f"runpy.run_path({str(ROOT / '.github/scripts/fake_object_store.py')!r}, "
        "run_name='__main__')\n",
    )
    oci.chmod(0o700)
    backups = directory / "backups"
    backup_config = private(
        "backup.json",
        json.dumps(
            {
                "format": 1,
                "archive_dir": str(backups),
                "age_binary": str(age),
                "recipients_file": str(recipients),
                "oci_binary": str(oci),
                "timeout_seconds": 120,
                "offsite": {"namespace": "fixture", "bucket": "fixture", "prefix": "pull-fixture/"},
            }
        ),
    )
    alerts = private(
        "alerts.json",
        json.dumps(
            {
                "format": 1,
                "smtp_host": "smtp.invalid",
                "smtp_port": 465,
                "smtp_security": "ssl",
                "from_address": "ci@example.com",
                "to_addresses": ["operator@example.com"],
                "state_dir": str(directory / "alerts"),
                "deployment_state_dir": str(state),
            }
        ),
    )
    config = {
        "state_dir": str(state),
        "release_dir": str(directory / "releases"),
        "env_file": str(env_file),
        "backup_dir": str(backups),
        "backup_config": str(backup_config),
        "alerts_config": str(alerts),
        "project_name": project,
        "url": url,
        "gh_binary": "/not-used-in-fixture",
        "credential": {"app_id": 77},
        "creator_id": 777,
    }
    source_files = {name: (release / name).read_bytes() for name in original["deployment_files"]}
    metadata = {**original, "tag_object": "b" * 40, "release_id": 8}
    archive_files = {**source_files, "release.json": pull.canonical(metadata)}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in archive_files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    raw = stream.getvalue()
    payload = {
        "tag": metadata["tag"],
        "commit": metadata["commit"],
        "release_id": 8,
        "app_id": 77,
        "creator_id": 777,
    }
    deployment = {"id": 90001, "payload": payload}

    class API:
        repository = original["repository"]
        base = "/repos/" + repository

        def __init__(self):
            self.posts = []

        def get(self, path):
            if path.endswith("/releases/8"):
                return {"id": 8}
            if path.endswith("/releases/8/assets?per_page=100"):
                return [
                    {
                        "id": 9,
                        "name": f"gh-random-{metadata['tag']}-deployment.tar.gz",
                        "size": len(raw),
                        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    }
                ]
            assert path.endswith("/deployments/90001")
            return deployment

        def download(self, path, *, limit):
            assert path.endswith("/releases/assets/9") and len(raw) <= limit
            return raw

        def post(self, path, value):
            assert path.endswith("/deployments/90001/statuses")
            self.posts.append(value)
            return {
                "id": 901,
                "state": value["state"],
                "creator": {"id": 777},
                "performed_via_github_app": None,
            }

    api = API()
    actual_run = subprocess.run
    calls = []

    def delegate(command, **kwargs):
        assert command[1].endswith("/deployment/deploy.py") and command[2] == "apply"
        calls.append(command)
        # Only the existing disposable loopback stack uses this explicit flag.
        # Production load_config rejects this HTTP origin and offers no bypass.
        return actual_run([*command, "--allow-loopback-http"], **kwargs)

    source = {
        "commit": metadata["commit"],
        "tag_object": metadata["tag_object"],
        "release": {"id": 8},
        "files": {name: pull.blob_sha(data) for name, data in source_files.items()},
    }
    with (
        patch.object(pull, "verify_request", return_value=payload),
        patch.object(pull, "source_release", return_value=source),
        patch.object(pull, "attest") as verify_attest,
        patch.object(pull.subprocess, "run", side_effect=delegate),
    ):
        pull.execute(api, config, deployment)
        verify_attest.assert_called_once()
        assert [post["state"] for post in api.posts] == ["in_progress", "success"]
        assert len(calls) == 1
        before = (state / "current.json").read_bytes()
        pull.execute(api, config, deployment)
        assert len(calls) == 1 and (state / "current.json").read_bytes() == before
    current = json.loads(before)
    assert current["previous"] and current["image"] == metadata["image"]
    assert len(list(object_store.rglob("*.age"))) == 1
    assert len(list(backups.glob("gh-random-*/offsite.json"))) == 1
    assert not list(backups.glob(".work-*"))
    print(
        "Approved-pull worker executed real encrypted pre-DDL backup, migration, "
        "web/proxy smoke; durable replay fence and previous release preserved.",
        flush=True,
    )
