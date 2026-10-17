"""The timer may certify success only after encryption, read-back and lease cleanup."""

import argparse
import importlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "deployment"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("scheduled_backup_tools", directory / "backup.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deploy = importlib.import_module("deploy")
    archive = importlib.import_module("backup_archive")
    database = importlib.import_module("database_backup")
    state, release = tmp_path / "state", tmp_path / "release"
    state.mkdir(mode=0o700)
    release.mkdir()
    identity = {"image": "immutable-digest", "tag": "v0.1.0", "commit": "a" * 40}
    path = state / "current.json"
    path.write_text(json.dumps({"release_dir": str(release), **identity}))
    path.chmod(0o600)
    args = argparse.Namespace(
        state_dir=state,
        config=tmp_path / "config.json",
        env_file=tmp_path / "compose.env",
        project_name="test",
        local_only=False,
    )
    policy = {
        "timeout_seconds": 10,
        "archive_dir": str(tmp_path / "encrypted"),
        "recipients_file": str(tmp_path / "recipients"),
        "age_binary": str(tmp_path / "age"),
        "retention_days": 7,
        "keep_copies": 2,
    }
    events, behavior = [], {"failure": ""}

    class Deployment:
        def __init__(self, args):
            self.compose = ["docker", "compose"]

        def image_info(self, image):
            assert image == identity["image"]

        def preflight(self, metadata, *, check_site):
            assert not check_site
            return {
                "services": {
                    "database": {
                        "environment": {
                            "POSTGRES_DB": "ci",
                            "POSTGRES_USER": "ci",
                            "POSTGRES_PASSWORD": "ci",
                        }
                    }
                }
            }

        def container(self, service):
            return service

        def run(self, command, **kwargs):
            return json.dumps(
                [
                    {
                        "State": {"Running": True},
                        "Config": {
                            "Image": identity["image"],
                            "Env": ["POSTGRES_DB=ci", "POSTGRES_USER=ci", "POSTGRES_PASSWORD=ci"],
                        },
                    }
                ]
            )

    class Coordinator:
        def __init__(self, deployment):
            pass

        def __enter__(self):
            events.append("acquire")
            return self

        def request(self, command):
            events.append(command)

        def __exit__(self, *args):
            events.append("release")
            if behavior["failure"] == "release":
                raise RuntimeError("cleanup failed")

    def operation(name, result):
        def perform(*args, **kwargs):
            events.append(name)
            if behavior["failure"] == name:
                raise RuntimeError("deliberate failure")
            return result

        return perform

    monkeypatch.setattr(module, "__file__", str(release / "deployment/backup.py"))
    monkeypatch.setattr(module, "config_from_file", lambda *args, **kwargs: policy)
    monkeypatch.setattr(deploy, "load_release", lambda root: identity)
    monkeypatch.setattr(deploy, "Deployment", Deployment)
    monkeypatch.setattr(deploy, "Coordinator", Coordinator)
    monkeypatch.setattr(database, "Database", lambda *args, **kwargs: object())
    monkeypatch.setattr(archive, "seal", operation("encrypt", tmp_path / "encrypted/snapshot"))
    monkeypatch.setattr(archive, "upload", operation("readback", {"sha256": "verified"}))
    monkeypatch.setattr(archive, "prune", operation("prune", []))
    return module, args, events, behavior


def test_success_requires_offsite_readback_and_catalog_cleanup(scenario):
    module, args, events, behavior = scenario
    assert module.worker(args) == 0
    assert (
        events.index("acquire")
        < events.index("encrypt")
        < events.index("readback")
        < events.index("prune")
        < events.index("release")
    )
    latest = json.loads((args.state_dir / "backup-success.json").read_text())
    assert latest["status"] == "succeeded" and latest["cleanup_complete"]


@pytest.mark.parametrize("failure", ["encrypt", "readback", "prune", "release"])
def test_failure_never_advances_success_watermark(scenario, failure):
    module, args, events, behavior = scenario
    watermark = args.state_dir / "backup-success.json"
    watermark.write_text('{"previous":"success"}')
    behavior["failure"] = failure
    with pytest.raises(RuntimeError):
        module.worker(args)
    assert json.loads(watermark.read_text()) == {"previous": "success"}
    assert json.loads((args.state_dir / "backup.json").read_text())["status"] == "failed"
    assert "release" in events


def test_explicit_local_rehearsal_cannot_claim_offsite_success(scenario):
    module, args, events, behavior = scenario
    args.local_only = True
    assert module.worker(args) == 0
    assert "readback" not in events and "prune" not in events
    assert not (args.state_dir / "backup-success.json").exists()
    assert json.loads((args.state_dir / "backup.json").read_text())["status"] == "local_only"


def test_signal_exception_is_not_retryable_eintr(scenario):
    assert not issubclass(scenario[0].BackupInterrupted, OSError)
