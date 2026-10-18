"""Host operations credentials and HTTP boundaries never leak authentication."""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest


@pytest.fixture
def api_tools(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "deployment"))
    return importlib.import_module("github_api")


def test_repository_scope_and_environment_token(api_tools, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "private-test-value")
    credential = api_tools.Credential.from_env("Owner/Repo")
    assert credential.token() == "private-test-value"
    api = api_tools.GitHubAPI("Owner/Repo", credential)
    with pytest.raises(ValueError, match="outside"):
        api.get("/repos/Other/Repo/contents")
    monkeypatch.setenv("GH_TOKEN", "invalid value")
    with pytest.raises(api_tools.GitHubError) as error:
        credential.token()
    assert "invalid value" not in str(error.value)


def test_private_token_file_revalidated_each_use(api_tools, tmp_path):
    tmp_path.chmod(0o700)
    key = tmp_path / "token"
    key.write_text("test-secret\n")
    key.chmod(0o600)
    credential = api_tools.Credential("Owner/Repo", {"type": "token_file", "token_file": str(key)})
    assert credential.token() == "test-secret"
    key.unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        credential.token()


def test_asset_redirect_drops_api_credential(api_tools, monkeypatch):
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 30
            if len(requests) == 1:
                raise HTTPError(
                    request.full_url,
                    302,
                    "",
                    {"Location": "https://release-assets.githubusercontent.com/test?signed=yes"},
                    None,
                )
            return io.BytesIO(b"archive")

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Opener())
    assert (
        api_tools.request("/repos/Owner/Repo/releases/assets/1", "secret", binary=True)
        == b"archive"
    )
    assert requests[0].get_header("Authorization") == "Bearer secret"
    assert requests[1].get_header("Authorization") is None


@pytest.mark.parametrize(
    "location",
    [
        "http://release-assets.githubusercontent.com/x",
        "https://attacker.example/x",
        "https://a.blob.core.windows.net:444/x",
        "https://user:password@release-assets.githubusercontent.com/x",
    ],
)
def test_untrusted_asset_redirect_rejected(api_tools, monkeypatch, location):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 302, "", {"Location": location}, None)

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Opener())
    with pytest.raises(api_tools.GitHubError, match="Untrusted"):
        api_tools.request("/repos/Owner/Repo/releases/assets/1", "secret", binary=True)


def test_json_writes_are_bounded_and_errors_redacted(api_tools, monkeypatch):
    class Opener:
        def open(self, request, timeout):
            assert json.loads(request.data) == {"state": "success"}
            raise HTTPError(request.full_url, 403, "SECRET", {}, io.BytesIO(b"SECRET"))

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Opener())
    with pytest.raises(api_tools.GitHubError, match="HTTP 403") as error:
        api_tools.request(
            "/repos/Owner/Repo/deployments/1/statuses",
            "SECRET",
            method="POST",
            payload={"state": "success"},
        )
    assert "SECRET" not in str(error.value)


def test_response_limit_and_network_diagnostics(api_tools, monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b"oversized")

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Opener())
    with pytest.raises(api_tools.GitHubError, match="size limit"):
        api_tools.request("/repos/Owner/Repo/x", "secret", limit=2)

    class Broken:
        def open(self, request, timeout):
            raise URLError("secret diagnostic")

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Broken())
    with pytest.raises(api_tools.GitHubError) as error:
        api_tools.request("/repos/Owner/Repo/x", "secret")
    assert "secret" not in str(error.value)
