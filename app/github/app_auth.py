"""GitHub App JWT and installation-token authentication."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

import httpx
import jwt

from app.core.settings import GitHubAppSettings

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
APP_JWT_LIFETIME = timedelta(minutes=9)
TOKEN_REFRESH_WINDOW = timedelta(minutes=1)


class GitHubAppAuthenticationError(RuntimeError):
    """GitHub rejected an App-authentication request or its response was invalid."""


class GitHubAppConfigurationError(GitHubAppAuthenticationError):
    """The local GitHub App configuration cannot be used safely."""


@dataclass(frozen=True)
class _InstallationAccessToken:
    value: str
    expires_at: datetime

    def is_usable(self, now: datetime) -> bool:
        return self.expires_at - TOKEN_REFRESH_WINDOW > now


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GitHubAppClient:
    """Create and cache GitHub App installation access tokens."""

    def __init__(
        self,
        settings: GitHubAppSettings,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] = _utc_now,
        api_base_url: str = GITHUB_API_BASE_URL,
    ) -> None:
        self._settings = settings
        self._http_client = http_client or httpx.Client(timeout=10.0)
        self._owns_http_client = http_client is None
        self._clock = clock
        self._api_base_url = api_base_url.rstrip("/")
        self._installation_id = settings.github_app_installation_id
        self._cached_token: _InstallationAccessToken | None = None
        self._lock = Lock()

    def close(self) -> None:
        """Close the internally created HTTP client, if any."""

        if self._owns_http_client:
            self._http_client.close()

    def get_installation_access_token(self) -> str:
        """Return a cached token, or obtain a fresh one from GitHub."""

        with self._lock:
            now = self._clock()
            if self._cached_token is not None and self._cached_token.is_usable(now):
                return self._cached_token.value

            app_jwt = self._create_app_jwt(now)
            installation_id = self._installation_id or self._resolve_installation_id(app_jwt)
            self._installation_id = installation_id
            token_data = self._request_json(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                app_jwt,
                body={},
            )
            token = self._parse_access_token(token_data, now)
            self._cached_token = token
            return token.value

    def _create_app_jwt(self, now: datetime) -> str:
        if now.tzinfo is None:
            raise GitHubAppConfigurationError("The GitHub App clock must return an aware datetime.")

        try:
            private_key = self._settings.github_app_private_key_path.read_text(encoding="utf-8")
        except OSError as error:
            raise GitHubAppConfigurationError(
                "The configured GitHub App private-key file could not be read."
            ) from error

        issued_at = int(now.timestamp()) - 60
        expires_at = int((now + APP_JWT_LIFETIME).timestamp())
        return jwt.encode(
            {
                "iat": issued_at,
                "exp": expires_at,
                "iss": self._settings.github_app_client_id,
            },
            private_key,
            algorithm="RS256",
        )

    def _resolve_installation_id(self, app_jwt: str) -> int:
        installations = self._request_json("GET", "/app/installations", app_jwt)

        if not isinstance(installations, list) or len(installations) != 1:
            raise GitHubAppConfigurationError(
                "Set GITHUB_APP_INSTALLATION_ID when the App does not have exactly one "
                "installation."
            )

        installation = installations[0]
        if not isinstance(installation, Mapping):
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid App-installations response."
            )

        installation_id = installation.get("id")
        if not isinstance(installation_id, int) or isinstance(installation_id, bool):
            raise GitHubAppAuthenticationError(
                "GitHub returned an installation without a numeric ID."
            )

        return installation_id

    def _parse_access_token(
        self,
        token_data: object,
        now: datetime,
    ) -> _InstallationAccessToken:
        if not isinstance(token_data, Mapping):
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid installation-token response."
            )

        token = token_data.get("token")
        expires_at_value = token_data.get("expires_at")
        if not isinstance(token, str) or not isinstance(expires_at_value, str):
            raise GitHubAppAuthenticationError(
                "GitHub's installation-token response omitted its token or expiration."
            )

        try:
            expires_at = datetime.fromisoformat(expires_at_value.replace("Z", "+00:00"))
        except ValueError as error:
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid installation-token expiration."
            ) from error

        if expires_at.tzinfo is None or expires_at <= now:
            raise GitHubAppAuthenticationError(
                "GitHub returned an installation token that is already expired."
            )

        return _InstallationAccessToken(value=token, expires_at=expires_at)

    def _request_json(
        self,
        method: str,
        path: str,
        app_jwt: str,
        *,
        body: Mapping[str, object] | None = None,
    ) -> object:
        try:
            response = self._http_client.request(
                method,
                f"{self._api_base_url}{path}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
                json=body,
            )
        except httpx.HTTPError as error:
            raise GitHubAppAuthenticationError(
                "The request to GitHub for an installation token failed."
            ) from error

        if response.is_error:
            raise GitHubAppAuthenticationError(
                f"GitHub App authentication failed with HTTP status {response.status_code}."
            )

        try:
            return response.json()
        except ValueError as error:
            raise GitHubAppAuthenticationError(
                "GitHub returned a non-JSON response during App authentication."
            ) from error
