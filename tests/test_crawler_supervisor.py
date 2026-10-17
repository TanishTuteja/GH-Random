"""Offline supervisor guards; real Docker and native systemd run in container CI."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def supervisor(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    directory = Path(__file__).resolve().parents[1] / "deployment"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("crawler_supervisor", directory / "crawler.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def worker(supervisor: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    deploy = importlib.import_module("deploy")

    root = tmp_path / "release"
    root.mkdir()
    state = tmp_path / "state"
    image = "ghcr.io/ci/repo@sha256:" + "a" * 64
    metadata = {"image": image, "tag": "v0.1.0", "commit": "b" * 40}
    private_json(state / "current.json", {**metadata, "release_dir": str(root)})
    arguments = argparse.Namespace(
        state_dir=state,
        env_file=tmp_path / "compose.env",
        project_name="test",
        max_runtime_seconds=5,
    )
    commands = []
    cleanup = []
    behavior = {"exit": "0", "error": "", "cleanup_error": False, "runtime_image": image}

    class FakeDeployment:
        def __init__(self, arguments):
            self.compose = ["docker", "compose"]

        def image_info(self, image):
            return {"Id": "image-id"}

        def preflight(self, supplied, *, check_site=True):
            assert supplied == metadata and check_site is False
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
            commands.append(command)
            if command[:2] == ["docker", "inspect"]:
                return json.dumps(
                    [
                        {
                            "State": {"Running": True},
                            "Config": {
                                "Image": behavior["runtime_image"],
                                "Env": [
                                    "POSTGRES_DB=ci",
                                    "POSTGRES_USER=ci",
                                    "POSTGRES_PASSWORD=ci",
                                ],
                            },
                        }
                    ]
                )
            return "c" * 64 + "\n"

    class Process:
        returncode = 0

        def communicate(self, *, timeout):
            assert timeout == arguments.max_runtime_seconds
            if behavior["error"] == "timeout":
                raise subprocess.TimeoutExpired("docker wait", timeout)
            if behavior["error"] == "interrupt":
                raise supervisor.SupervisorInterrupted("interrupted")
            return behavior["exit"] + "\n", ""

        def wait(self, *, timeout):
            return 0

    def stop(record):
        cleanup.append(record.copy())
        if behavior["cleanup_error"]:
            raise RuntimeError("cleanup failure")

    monkeypatch.setattr(supervisor, "__file__", str(root / "deployment/crawler.py"))
    monkeypatch.setattr(deploy, "Deployment", FakeDeployment)
    monkeypatch.setattr(deploy, "load_release", lambda supplied: metadata)
    monkeypatch.setattr(supervisor, "cleanup", stop)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: Process())
    return arguments, behavior, commands, cleanup


@pytest.mark.parametrize("exit_code, expected, status", [("0", 0, "succeeded"), ("7", 1, "failed")])
def test_tracks_real_container_exit_and_launches_normal_crawl(
    supervisor: ModuleType, worker, exit_code: str, expected: int, status: str
) -> None:
    args, behavior, commands, cleanup = worker
    behavior["exit"] = exit_code
    assert supervisor.supervise(args) == expected
    record = json.loads((args.state_dir / "crawler.json").read_text())
    assert record["status"] == status and record["cleanup_complete"] is True
    assert record["container_exit_code"] == int(exit_code)
    launch = commands[-1]
    assert launch[-5:] == ["crawler", "python", "-m", "app.cli", "crawl"]
    assert "--resume" not in launch and "--no-deps" in launch and "--rm" not in launch
    assert launch[launch.index("--pull") + 1] == "never"
    assert len(cleanup) == 1 and cleanup[0]["container_id"] == "c" * 64
    success = args.state_dir / "crawler-success.json"
    assert success.exists() == (expected == 0)
    if expected == 0:
        assert json.loads(success.read_text()) == record


def test_later_failure_preserves_last_success_watermark(supervisor: ModuleType, worker) -> None:
    args, behavior, commands, cleanup = worker
    assert supervisor.supervise(args) == 0
    success = (args.state_dir / "crawler-success.json").read_text()
    behavior["exit"] = "1"
    assert supervisor.supervise(args) == 1
    assert (args.state_dir / "crawler-success.json").read_text() == success


@pytest.mark.parametrize(
    "error, expected, status",
    [
        ("timeout", 124, "timed_out"),
        ("interrupt", 143, "interrupted"),
    ],
)
def test_timeout_and_signal_stop_daemon_owned_child(
    supervisor: ModuleType, worker, error: str, expected: int, status: str
) -> None:
    args, behavior, commands, cleanup = worker
    behavior["error"] = error
    assert supervisor.supervise(args) == expected
    record = json.loads((args.state_dir / "crawler.json").read_text())
    assert record["status"] == status and record["cleanup_complete"] is True
    assert cleanup[0]["id"] == record["id"]


def test_cleanup_failure_cannot_report_success(supervisor: ModuleType, worker) -> None:
    args, behavior, commands, cleanup = worker
    behavior["cleanup_error"] = True
    with pytest.raises(RuntimeError, match="cleanup failure"):
        supervisor.supervise(args)
    record = json.loads((args.state_dir / "crawler.json").read_text())
    assert record["status"] == "cleanup_failed"
    assert record["crawl_status"] == "succeeded" and record["cleanup_complete"] is False


def test_failed_deploy_runtime_mismatch_refuses_old_crawler(supervisor: ModuleType, worker) -> None:
    args, behavior, commands, cleanup = worker
    behavior["runtime_image"] = "new-but-not-verified-image"
    with pytest.raises(ValueError, match="recover deployment"):
        supervisor.supervise(args)
    assert not (args.state_dir / "crawler.json").exists() and cleanup == []
    assert not any("run" in command for command in commands)


def test_orphan_cleanup_precedes_new_child(supervisor: ModuleType, worker) -> None:
    args, behavior, commands, cleanup = worker
    private_json(args.state_dir / "crawler.json", {"id": "old"})
    supervisor.supervise(args)
    assert cleanup[0] == {"id": "old"}
    assert len(cleanup) == 2 and cleanup[1]["id"] != "old"


def test_stop_post_will_not_kill_a_successor_invocation(
    supervisor: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    private_json(state / "crawler.json", {"systemd_invocation": "new-invocation"})
    monkeypatch.setenv("INVOCATION_ID", "old-invocation")
    monkeypatch.setattr(sys, "argv", ["crawler.py", "stop", "--state-dir", str(state)])
    monkeypatch.setattr(supervisor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(supervisor, "cleanup", lambda value: pytest.fail("killed successor"))
    assert supervisor.main() == 0


def test_signal_exception_cannot_be_swallowed_as_retryable_io(supervisor: ModuleType) -> None:
    assert not issubclass(supervisor.SupervisorInterrupted, OSError)


@pytest.mark.parametrize("matching", [True, False])
@pytest.mark.parametrize("worker_name", ["crawler.py", "backup.py"])
def test_bootstrap_executes_only_matching_verified_worker(
    supervisor: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matching: bool,
    worker_name: str,
) -> None:
    state = tmp_path / "state"
    release = tmp_path / "release"
    (release / "deployment").mkdir(parents=True)
    worker = release / "deployment" / worker_name
    worker.write_text("safe fixture")
    metadata = {
        "image": "immutable",
        "tag": "v0.1.0",
        "commit": "a" * 40,
        "deployment_format": 1,
        "deployment_files": {
            "deployment/" + worker_name: hashlib.sha256(worker.read_bytes()).hexdigest(),
        },
    }
    (release / "release.json").write_text(json.dumps(metadata))
    private_json(
        state / "current.json",
        {"release_dir": str(release), **{key: metadata[key] for key in ("image", "tag", "commit")}},
    )
    if not matching:
        worker.write_text("tampered")
    arguments = argparse.Namespace(state_dir=state)
    monkeypatch.setattr(sys, "argv", ["crawler.py", "run", "--state-dir", str(state)])

    class ExecCalled(Exception):
        pass

    def execute(binary, argv):
        assert binary == sys.executable and argv[1:3] == [str(worker), "worker"]
        raise ExecCalled()

    monkeypatch.setattr(supervisor.os, "execv", execute)
    with pytest.raises(ExecCalled if matching else ValueError):
        supervisor.dispatch(arguments, worker_name=worker_name)


@pytest.mark.parametrize("correct", [True, False])
def test_cleanup_checks_labels_and_exact_container_identity(
    supervisor: ModuleType, monkeypatch: pytest.MonkeyPatch, correct: bool
) -> None:
    commands = []
    record = {
        "id": "a" * 32,
        "project": "test",
        "container_id": "b" * 64,
        "container_name": "test-scheduled-" + "a" * 32,
    }

    def docker(command, **kwargs):
        commands.append(command)
        if command[0] == "container":
            return "b" * 64
        if command[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": "b" * 64,
                        "State": {"Running": True},
                        "Config": {
                            "Labels": {
                                supervisor.OWNER_LABEL: record["id"] if correct else "other-owner",
                                supervisor.PROJECT_LABEL: "test",
                            },
                        },
                    }
                ]
            )
        return ""

    monkeypatch.setattr(supervisor, "docker", docker)
    if correct:
        supervisor.cleanup(record)
        assert commands[-2:] == [["stop", "--time", "30", "b" * 64], ["rm", "b" * 64]]
    else:
        with pytest.raises(ValueError, match="ownership mismatch"):
            supervisor.cleanup(record)
        assert not any(command[0] in ("stop", "rm", "kill") for command in commands)
