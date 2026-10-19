"""Disposable real pull-to-deploy rehearsal; GitHub/signature oracles are fixtures.

Called only by the existing isolated native ARM64 deployment harness. No GitHub,
SMTP provider, OCI account or existing user database is contacted.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment"))

import pull  # noqa: E402
from install_age import install  # noqa: E402


def check(
    workspace: Path, release: Path, env_file: Path, state: Path, url: str, project: str
) -> None:
    assert os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux"
    original = json.loads((release / "release.json").read_text())
    assert original["platform"] == "linux/arm64"
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
    payload = {"tag": metadata["tag"], "commit": metadata["commit"], "release_id": 8, "app_id": 77}
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
            return {"id": 901, "state": value["state"], "performed_via_github_app": {"id": 77}}

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
