"""Auth-smoke orchestration tests never read real credentials or contact GitHub."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from app.core.settings import GitHubAppSettings


def test_hardened_auth_role_has_only_app_credentials_and_always_cleans_owned_resources(
    tmp_path, monkeypatch
):
    source = Path(__file__).resolve().parents[1] / ".github/scripts/check_github_auth.py"
    spec = importlib.util.spec_from_file_location("github_auth_smoke", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = tmp_path / "fixture.pem"
    key.write_bytes(b"not-a-real-key")
    settings = GitHubAppSettings(
        github_app_client_id="test-client",
        github_app_installation_id=123,
        github_app_private_key_path=key,
    )
    calls = []
    volume = []

    def docker(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ["image", "inspect"]:
            return "amd64"
        if command[:2] == ["volume", "create"]:
            volume.append(command[-1])
        if command[:2] == ["volume", "ls"]:
            return volume[0]
        if command[:1] == ["run"] and command[-1] == module.PROBE:
            assert "--read-only" in command and "readonly" in " ".join(command)
            assert "DATABASE_URL" not in " ".join(command) and "--env-file" not in command
            assert "--user" in command and "0:0" in command and "ALL" in command
            assert "--network" in command and "bridge" in command
            raise RuntimeError("Deliberate mocked probe failure")
        return ""

    monkeypatch.setattr(module, "docker", docker)
    with pytest.raises(RuntimeError, match="mocked probe"):
        module.smoke("gh-random:auth-fixture", settings)
    assert calls[-1][0] == ["volume", "rm", volume[0]]
    stage = next(item for item in calls if item[0][-1] == module.STAGE)
    assert stage[1]["data"] == b"not-a-real-key" and "none" in stage[0]


def test_docker_failure_does_not_echo_captured_secrets(monkeypatch):
    source = Path(__file__).resolve().parents[1] / ".github/scripts/check_github_auth.py"
    spec = importlib.util.spec_from_file_location("redacted_auth_smoke", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, b"SECRET", b"SECRET"),
    )
    with pytest.raises(ValueError) as error:
        module.docker(["test"])
    assert "SECRET" not in str(error.value)
