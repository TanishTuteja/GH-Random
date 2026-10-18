"""Small bounded GitHub.com client; credentials never enter URLs or diagnostics."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from database_backup import check_private

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
MAX_JSON = 4 * 1024 * 1024


class GitHubError(RuntimeError):
    """Safe, deliberately body/header-free request failure."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
            if not all(
                isinstance(config.get(key), int) and config[key] > 0
                for key in ("app_id", "installation_id")
            ):
                raise ValueError("Positive App and installation IDs required")
            private_path(config.get("private_key_file"))
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
            token = private_path(self.config["token_file"]).read_text().strip()
        else:
            if self.cached and self.expires > time.time() + 120:
                return self.cached
            now = int(time.time())

            def encode(value: object) -> str:
                return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

            unsigned = (
                encode({"alg": "RS256", "typ": "JWT"})
                + "."
                + encode({"iat": now - 60, "exp": now + 540, "iss": self.config["app_id"]})
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
                    "permissions": {
                        "contents": "read",
                        "actions": "read",
                        "deployments": "write",
                        "attestations": "read",
                    },
                },
            )
            value = json.loads(response)
            token = value["token"]
            # Refresh conservatively; the provider issues one-hour installation tokens.
            self.cached, self.expires = token, time.time() + 3000
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise GitHubError("Missing or invalid GitHub credential")
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
    if not path.startswith("/") or path.startswith("//") or "#" in path or "\\" in path:
        raise ValueError("Only absolute GitHub API paths are accepted")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "gh-random-operations",
        "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
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
        if not path.startswith(self.base + "/"):
            raise ValueError("Download outside configured repository")
        return request(
            path, self.credential.token(), binary=True, limit=limit, timeout=self.timeout
        )
