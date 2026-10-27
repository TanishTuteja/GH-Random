"""Small bounded GitHub.com client; credentials never enter URLs or diagnostics."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from database_backup import check_private

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
MAX_JSON = 4 * 1024 * 1024


class GitHubError(RuntimeError):
    """Safe, deliberately body/header-free request failure."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_path(path: str) -> None:
    parsed = urlsplit(path)
    decoded = parsed.path
    for _ in range(4):
        decoded = unquote(decoded)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not decoded.startswith("/")
        or "//" in decoded
        or "\\" in decoded
        or "%" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in path + decoded)
    ):
        raise ValueError("Unsafe GitHub API path")


def valid_token(token: object) -> bool:
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,8192}", token) is not None


def private_path(value: object) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("Credential paths must be absolute")
    path = Path(value)
    check_private(path.parent)
    check_private(path)
    if not path.is_file():
        raise ValueError("Credential must be a private regular file")
    return path


class Credential:
    """Installation JWT is signed by host OpenSSL; no host Python packages needed."""

    def __init__(self, repository: str, config: dict[str, Any]) -> None:
        if REPOSITORY.fullmatch(repository) is None:
            raise ValueError("Invalid repository")
        self.repository = repository
        self.config = config
        self.cached = ""
        self.expires = 0.0
        mode = config.get("type")
        if mode == "app":
            if (
                not isinstance(config.get("client_id"), str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", config["client_id"]) is None
            ):
                raise ValueError("App client ID required for JWT issuer")
            if not all(
                type(config.get(key)) is int and config[key] > 0
                for key in ("app_id", "installation_id")
            ):
                raise ValueError("Positive App and installation IDs required")
            private_path(config.get("private_key_file"))
            permissions = config.get(
                "permissions",
                {
                    "contents": "read",
                    "actions": "read",
                    "deployments": "write",
                    "attestations": "read",
                },
            )
            allowed = {
                "contents": {"read"},
                "actions": {"read"},
                "deployments": {"read", "write"},
                "attestations": {"read"},
            }
            if (
                not isinstance(permissions, dict)
                or not permissions
                or any(
                    key not in allowed or value not in allowed[key]
                    for key, value in permissions.items()
                )
            ):
                raise ValueError("Invalid operations App permissions")
            self.permissions = permissions
        elif mode == "token_file":
            private_path(config.get("token_file"))
        elif mode != "environment":
            raise ValueError("Use app or private token_file credentials")

    @classmethod
    def from_env(cls, repository: str) -> Credential:
        """Only workflow callers use GH_TOKEN; private VM JSON cannot select this mode."""
        return cls(repository, {"type": "environment"})

    def token(self) -> str:
        mode = self.config["type"]
        if mode == "environment":
            token = os.environ.get("GH_TOKEN", "")
        elif mode == "token_file":
            path = private_path(self.config["token_file"])
            if path.stat().st_size > 8192:
                raise GitHubError("Credential file exceeds size limit")
            token = path.read_text().strip()
        else:
            if self.cached and self.expires > time.time() + 120:
                return self.cached
            now = int(time.time())

            def encode(value: object) -> str:
                return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

            unsigned = (
                encode({"alg": "RS256", "typ": "JWT"})
                + "."
                + encode({"iat": now - 60, "exp": now + 540, "iss": self.config["client_id"]})
            )
            signed = subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-sign",
                    str(private_path(self.config["private_key_file"])),
                ],
                input=unsigned.encode(),
                capture_output=True,
                timeout=15,
                check=False,
            )
            if signed.returncode:
                raise GitHubError("App JWT signing failed")
            jwt = unsigned + "." + base64.urlsafe_b64encode(signed.stdout).rstrip(b"=").decode()
            response = request(
                f"/app/installations/{self.config['installation_id']}/access_tokens",
                jwt,
                method="POST",
                payload={
                    "repositories": [self.repository.split("/")[1]],
                    "permissions": self.permissions,
                },
            )
            value = json.loads(response)
            if not isinstance(value, dict):
                raise GitHubError("Invalid installation credential response")
            token = value.get("token")
            expiry = value.get("expires_at")
            if not valid_token(token) or not isinstance(expiry, str):
                raise GitHubError("Invalid installation credential response")
            try:
                instant = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if instant.tzinfo is None or not 120 < instant.timestamp() - time.time() <= 3700:
                    raise ValueError
            except ValueError:
                raise GitHubError("Invalid installation credential expiry") from None
            self.cached, self.expires = token, instant.timestamp()
        if not valid_token(token):
            raise GitHubError("Missing or invalid GitHub credential")
        assert isinstance(token, str)
        return token


def request(
    path: str,
    token: str,
    *,
    method: str = "GET",
    payload: object = None,
    limit: int = MAX_JSON,
    timeout: int = 30,
    binary: bool = False,
) -> bytes:
    """A killable HTTP-only child bounds DNS/TLS/read time without touching DDL."""
    validate_path(path)
    try:
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "http-worker"],
            input=json.dumps(
                {
                    "path": path,
                    "token": token,
                    "method": method,
                    "payload": payload,
                    "limit": limit,
                    "timeout": timeout,
                    "binary": binary,
                }
            ).encode(),
            capture_output=True,
            timeout=timeout + 15,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        raise GitHubError("GitHub HTTP worker unavailable or timed out") from None
    if child.returncode or len(child.stdout) > limit:
        raise GitHubError("GitHub HTTP worker rejected the request")
    return child.stdout


def _request(
    path: str,
    token: str,
    *,
    method: str = "GET",
    payload: object = None,
    limit: int = MAX_JSON,
    timeout: int = 30,
    binary: bool = False,
) -> bytes:
    validate_path(path)
    # Actions artifact downloads negotiate the normal API media type before
    # redirecting to ZIP bytes. Only release assets require octet-stream to
    # select their content instead of their JSON metadata.
    release_asset = re.fullmatch(
        r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/assets/[0-9]+",
        urlsplit(path).path,
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "gh-random-operations",
        "Accept": (
            "application/octet-stream"
            if binary and release_asset
            else "application/vnd.github+json"
        ),
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request("https://api.github.com" + path, data=data, headers=headers, method=method)
    opener = build_opener(NoRedirect())
    try:
        try:
            response = opener.open(req, timeout=timeout)
        except HTTPError as error:
            if not binary or method != "GET" or error.code not in (301, 302, 303, 307):
                raise
            location = error.headers.get("Location", "")
            target = urlsplit(location)
            host = target.hostname or ""
            if (
                target.scheme != "https"
                or target.username
                or target.password
                or target.port not in (None, 443)
                or not (
                    host.endswith(".githubusercontent.com")
                    or host.endswith(".blob.core.windows.net")
                )
            ):
                raise GitHubError("Untrusted GitHub download redirect") from None
            # Never forward the API credential to signed object-storage URLs.
            response = opener.open(
                Request(location, headers={"User-Agent": headers["User-Agent"]}), timeout=timeout
            )
        with response:
            contents = response.read(limit + 1)
            if len(contents) > limit:
                raise GitHubError("GitHub response exceeds size limit")
            return contents
    except HTTPError as error:
        raise GitHubError(f"GitHub request failed: HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise GitHubError("GitHub request unavailable") from None


class GitHubAPI:
    def __init__(self, repository: str, credential: Credential, *, timeout: int = 30) -> None:
        if REPOSITORY.fullmatch(repository) is None or credential.repository != repository:
            raise ValueError("GitHub credential/repository mismatch")
        if not 1 <= timeout <= 60:
            raise ValueError("Invalid GitHub timeout")
        self.repository, self.credential, self.timeout = repository, credential, timeout
        self.base = f"/repos/{repository}"

    def call(self, path: str, *, method: str = "GET", payload: object = None) -> Any:
        validate_path(path)
        if not path.startswith(self.base + "/"):
            raise ValueError("GitHub path outside configured repository")
        contents = request(
            path, self.credential.token(), method=method, payload=payload, timeout=self.timeout
        )
        return json.loads(contents) if contents else None

    def get(self, path: str) -> Any:
        return self.call(path)

    def post(self, path: str, payload: object) -> Any:
        return self.call(path, method="POST", payload=payload)

    def patch(self, path: str, payload: object) -> Any:
        return self.call(path, method="PATCH", payload=payload)

    def download(self, path: str, *, limit: int = 16 * 1024 * 1024) -> bytes:
        validate_path(path)
        if not path.startswith(self.base + "/"):
            raise ValueError("Download outside configured repository")
        return request(
            path, self.credential.token(), binary=True, limit=limit, timeout=self.timeout
        )


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["http-worker"]:
            raise ValueError("Internal HTTP worker only")
        parameters = json.load(sys.stdin)
        sys.stdout.buffer.write(_request(**parameters))
    except Exception:
        # API bodies, credentials and signed download URLs never become logs.
        raise SystemExit(1) from None
