"""Host operations credentials and HTTP boundaries never leak authentication."""

from __future__ import annotations

import importlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest


def headers(**values: str) -> Message:
    message = Message()
    for key, value in values.items():
        message[key] = value
    return message


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


@pytest.mark.parametrize(
    ("path", "media_type"),
    [
        ("/repos/Owner/Repo/actions/artifacts/5/zip", "application/vnd.github+json"),
        (
            "/repos/Owner/Repo/actions/artifacts/5/zip?download=1",
            "application/vnd.github+json",
        ),
        ("/repos/Owner/Repo/releases/assets/1", "application/octet-stream"),
        ("/repos/Owner/Repo/releases/assets/1?name=fixture.zip", "application/octet-stream"),
    ],
)
@pytest.mark.parametrize(
    "location",
    [
        "https://release-assets.githubusercontent.com/test?signed=yes",
        "https://fixture.blob.core.windows.net/test?signed=yes",
    ],
)
def test_download_media_type_and_redirect_drop_api_credential(
    api_tools, monkeypatch, path, media_type, location
):
    requests = []
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("request.json", '{"deployment_id": 42}')
    expected = archive.getvalue()

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 30
            if len(requests) == 1:
                # Match GitHub's content negotiation: an unsupported media type
                # is rejected before the server returns the download redirect.
                if request.get_header("Accept") != media_type:
                    raise HTTPError(request.full_url, 415, "", headers(), None)
                raise HTTPError(
                    request.full_url,
                    302,
                    "",
                    headers(Location=location),
                    None,
                )
            return io.BytesIO(expected)

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Opener())
    actual = api_tools._request(path, "secret", binary=True)
    assert actual == expected
    with zipfile.ZipFile(io.BytesIO(actual)) as package:
        assert json.loads(package.read("request.json")) == {"deployment_id": 42}
    assert requests[0].get_header("Authorization") == "Bearer secret"
    assert requests[1].get_header("Authorization") is None
    assert len(requests) == 2


@pytest.mark.parametrize(
    "location",
    [
        "http://release-assets.githubusercontent.com/x",
        "https://attacker.example/x",
        "https://a.blob.core.windows.net:444/x",
        "https://user:password@release-assets.githubusercontent.com/x",
    ],
)
@pytest.mark.parametrize(
    "path",
    ["/repos/Owner/Repo/actions/artifacts/5/zip", "/repos/Owner/Repo/releases/assets/1"],
)
def test_untrusted_asset_redirect_rejected(api_tools, monkeypatch, location, path):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 302, "", headers(Location=location), None)

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Opener())
    with pytest.raises(api_tools.GitHubError, match="Untrusted"):
        api_tools._request(path, "secret", binary=True)


def test_json_writes_are_bounded_and_errors_redacted(api_tools, monkeypatch):
    class Opener:
        def open(self, request, timeout):
            assert json.loads(request.data) == {"state": "success"}
            raise HTTPError(request.full_url, 403, "SECRET", headers(), io.BytesIO(b"SECRET"))

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Opener())
    with pytest.raises(api_tools.GitHubError, match="HTTP 403") as error:
        api_tools._request(
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
        api_tools._request("/repos/Owner/Repo/x", "secret", limit=2)

    class Broken:
        def open(self, request, timeout):
            raise URLError("secret diagnostic")

    monkeypatch.setattr(api_tools, "build_opener", lambda *args: Broken())
    with pytest.raises(api_tools.GitHubError) as error:
        api_tools._request("/repos/Owner/Repo/x", "secret")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "path",
    [
        "/repos/Owner/Repo/../Other/issues",
        "/repos/Owner/Repo/%2e%2e/Other/issues",
        "/repos/Owner/Repo/%252e%252e/Other/issues",
        "/repos/Owner/Repo/a%2f..%2f..%2fOther",
        "/repos/Owner/Repo/contents\\..\\Other",
        "/repos/Owner/Repo/x\r\nAuthorization:bad",
    ],
)
def test_repository_scope_rejects_url_normalization_traversal(api_tools, path):
    api = api_tools.GitHubAPI("Owner/Repo", api_tools.Credential.from_env("Owner/Repo"))
    with pytest.raises(ValueError):
        api.get(path)
    with pytest.raises(ValueError):
        api.download(path)


def app_config(tmp_path):
    tmp_path.chmod(0o700)
    key = tmp_path / "key.pem"
    key.write_text("test key fixture")
    key.chmod(0o600)
    return {
        "type": "app",
        "app_id": 77,
        "client_id": "Iv-fixture",
        "installation_id": 8,
        "private_key_file": str(key),
        "permissions": {"deployments": "write"},
    }


def test_app_scopes_expiry_and_client_id_issuer(api_tools, tmp_path, monkeypatch):
    from types import SimpleNamespace

    config = app_config(tmp_path)
    signed = []

    def sign(command, **kwargs):
        signed.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=b"signed")

    def respond(path, jwt, **kwargs):
        assert path == "/app/installations/8/access_tokens"
        assert kwargs["payload"] == {
            "repositories": ["Repo"],
            "permissions": {"deployments": "write"},
        }
        return json.dumps(
            {
                "token": "short-lived-token",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            }
        ).encode()

    monkeypatch.setattr(api_tools.subprocess, "run", sign)
    monkeypatch.setattr(api_tools, "request", respond)
    credential = api_tools.Credential("Owner/Repo", config)
    assert credential.token() == credential.token() == "short-lived-token"
    assert len(signed) == 1
    import base64

    claims = signed[0].decode().split(".")[1]
    assert (
        json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))["iss"]
        == "Iv-fixture"
    )


@pytest.mark.parametrize(
    "value",
    [
        {"token": "secret invalid", "expires_at": "2030-01-01T00:00:00Z"},
        {"token": "secret", "expires_at": "2030-01-01T00:00:00Z"},
        {"token": "secret", "expires_at": "2020-01-01T00:00:00Z"},
        {"token": "secret", "expires_at": "bad"},
        {"token": "secret", "expires_at": "2026-10-18T10:00:00"},
    ],
)
def test_malformed_installation_tokens_never_cached(api_tools, tmp_path, monkeypatch, value):
    from types import SimpleNamespace

    credential = api_tools.Credential("Owner/Repo", app_config(tmp_path))
    monkeypatch.setattr(
        api_tools.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"signature"),
    )
    monkeypatch.setattr(api_tools, "request", lambda *args, **kwargs: json.dumps(value).encode())
    with pytest.raises(api_tools.GitHubError):
        credential.token()
    assert not credential.cached and credential.expires == 0


def test_bool_app_id_and_oversize_token_file_rejected(api_tools, tmp_path):
    config = app_config(tmp_path)
    config["app_id"] = True
    with pytest.raises(ValueError):
        api_tools.Credential("Owner/Repo", config)
    token = tmp_path / "token"
    token.write_text("x" * 8193)
    token.chmod(0o600)
    credential = api_tools.Credential(
        "Owner/Repo", {"type": "token_file", "token_file": str(token)}
    )
    with pytest.raises(api_tools.GitHubError, match="size limit"):
        credential.token()


def test_dns_stall_is_bounded_without_canceling_any_deployment(api_tools, monkeypatch):
    def stalled(command, **kwargs):
        assert command[-1] == "http-worker"
        assert "secret" not in command
        assert kwargs["timeout"] == 45
        raise api_tools.subprocess.TimeoutExpired(command, 45, output=b"secret diagnostic")

    monkeypatch.setattr(api_tools.subprocess, "run", stalled)
    with pytest.raises(api_tools.GitHubError, match="timed out") as error:
        api_tools.request("/repos/Owner/Repo/x", "secret")
    assert "secret" not in str(error.value)
