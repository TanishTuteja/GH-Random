from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from app.core.settings import GitHubAppSettings
from app.github.app_auth import (
    GitHubAppClient,
    GitHubAppConfigurationError,
)

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)


def make_settings(tmp_path, installation_id: int | None = None) -> GitHubAppSettings:
    key_path = tmp_path / "github-app-private-key.pem"
    key_path.write_text("not-a-real-key", encoding="utf-8")
    values: dict[str, object] = {
        "github_app_client_id": "Iv1.example",
        "github_app_private_key_path": key_path,
    }
    if installation_id is not None:
        values["github_app_installation_id"] = installation_id

    return GitHubAppSettings.model_validate(values)


def test_creates_a_short_lived_app_jwt(tmp_path) -> None:
    settings = make_settings(tmp_path)
    client = GitHubAppClient(settings, clock=lambda: NOW)

    try:
        with patch("app.github.app_auth.jwt.encode", return_value="signed-jwt") as encode:
            assert client._create_app_jwt(NOW) == "signed-jwt"

        payload = encode.call_args.args[0]
        assert payload == {
            "iat": int(NOW.timestamp()) - 60,
            "exp": int(NOW.timestamp()) + 9 * 60,
            "iss": "Iv1.example",
        }
        assert encode.call_args.kwargs["algorithm"] == "RS256"
    finally:
        client.close()


def test_resolves_single_installation_and_caches_access_token(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 42}])
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2026-09-29T01:00:00Z",
                },
            )
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubAppClient(make_settings(tmp_path), http_client=http_client, clock=lambda: NOW)

    try:
        with patch("app.github.app_auth.jwt.encode", return_value="app-jwt"):
            assert client.get_installation_access_token() == "installation-token"
            assert client.get_installation_access_token() == "installation-token"

        assert [request.url.path for request in requests] == [
            "/app/installations",
            "/app/installations/42/access_tokens",
        ]
        assert all(request.headers["authorization"] == "Bearer app-jwt" for request in requests)
    finally:
        http_client.close()


def test_uses_configured_installation_without_listing(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/99/access_tokens"
        return httpx.Response(
            201,
            json={
                "token": "installation-token",
                "expires_at": "2026-09-29T01:00:00Z",
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GitHubAppClient(
        make_settings(tmp_path, installation_id=99),
        http_client=http_client,
        clock=lambda: NOW,
    )

    try:
        with patch("app.github.app_auth.jwt.encode", return_value="app-jwt"):
            assert client.get_installation_access_token() == "installation-token"
    finally:
        http_client.close()


def test_rejects_ambiguous_installations_without_an_explicit_id(tmp_path) -> None:
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[{"id": 1}, {"id": 2}])
        )
    )
    client = GitHubAppClient(make_settings(tmp_path), http_client=http_client, clock=lambda: NOW)

    try:
        with patch("app.github.app_auth.jwt.encode", return_value="app-jwt"):
            with pytest.raises(GitHubAppConfigurationError, match="INSTALLATION_ID"):
                client.get_installation_access_token()
    finally:
        http_client.close()
