"""Explicit live App-auth smoke: AMD64, isolated root-owned key, no DB or crawl.

Unlike CI fixtures this contacts GitHub. Run only with operator approval using
the local development environment and a trusted built/published application image.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import uuid
from pathlib import Path

from app.core.settings import GitHubAppSettings

LABEL = "io.gh-random.auth-smoke"
STAGE = """
import os, sys
value = sys.stdin.buffer.read(65537)
assert 0 < len(value) <= 65536
descriptor = os.open('/run/secrets/github.pem', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(descriptor, 'wb') as out:
    out.write(value)
"""
PROBE = """
import os, platform, stat
import httpx
from app.core.settings import get_github_app_settings
from app.github.app_auth import GitHubAppClient, GITHUB_API_VERSION
assert platform.machine() == 'x86_64' and os.getuid() == 0
settings = get_github_app_settings()
info = settings.github_app_private_key_path.stat()
assert info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
client = GitHubAppClient(settings)
token = None
try:
    token = client.get_installation_access_token()
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': GITHUB_API_VERSION}
    with httpx.Client(timeout=10, trust_env=False) as transport:
        response = transport.get('https://api.github.com/rate_limit', headers=headers)
        assert response.status_code == 200
finally:
    try:
        if token is not None:
            with httpx.Client(timeout=10, trust_env=False) as transport:
                assert transport.delete('https://api.github.com/installation/token', headers={
                    'Authorization': 'Bearer ' + token, 'X-GitHub-Api-Version': GITHUB_API_VERSION
                }).status_code == 204
    finally:
        client.close()
print('AMD64 root-owned key, App token, authenticated HTTPS and token revocation passed.')
"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def docker(arguments: list[str], *, data: bytes | None = None, timeout: int = 90) -> str:
    result = subprocess.run(
        ["docker", *arguments], input=data, capture_output=True, timeout=timeout, check=False
    )
    # Never echo captured container output: exceptions can include credential data.
    require(result.returncode == 0, "Auth smoke Docker operation failed")
    return result.stdout.decode("utf-8").strip()


def smoke(image: str, settings: GitHubAppSettings) -> None:
    require(
        re.fullmatch(
            r"gh-random:[a-z0-9_.-]+|ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[a-f0-9]{64}",
            image,
        )
        is not None,
        "Use a trusted gh-random local tag or immutable GHCR digest",
    )
    require(
        docker(["image", "inspect", image, "--format", "{{.Architecture}}"]) == "amd64",
        "The smoke image must already be available for AMD64",
    )
    key = settings.github_app_private_key_path.resolve(strict=True)
    require(key.is_file() and 0 < key.stat().st_size <= 65536, "Invalid App key file")
    identifier = uuid.uuid4().hex
    volume = "gh-random-auth-" + identifier
    hardened = [
        "--platform",
        "linux/amd64",
        "--user",
        "0:0",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "128",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--tmpfs",
        "/tmp:size=64m,mode=1777",
        "--label",
        f"{LABEL}={identifier}",
    ]
    try:
        docker(["volume", "create", "--label", f"{LABEL}={identifier}", volume])
        # Windows bind mounts do not model Unix 0600 ownership. Stage the key in
        # an ephemeral Docker volume, then mount it read-only in the real probe.
        docker(
            [
                "run",
                "--rm",
                "-i",
                *hardened,
                "--network",
                "none",
                "--mount",
                f"type=volume,source={volume},target=/run/secrets",
                image,
                "python",
                "-c",
                STAGE,
            ],
            data=key.read_bytes(),
        )
        credentials = [
            "--env",
            "GITHUB_APP_CLIENT_ID=" + settings.github_app_client_id,
            "--env",
            "GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github.pem",
        ]
        if settings.github_app_installation_id is not None:
            credentials += [
                "--env",
                f"GITHUB_APP_INSTALLATION_ID={settings.github_app_installation_id}",
            ]
        docker(
            [
                "run",
                "--rm",
                *hardened,
                "--network",
                "bridge",
                "--mount",
                f"type=volume,source={volume},target=/run/secrets,readonly",
                *credentials,
                image,
                "python",
                "-c",
                PROBE,
            ],
        )
    finally:
        # A timed-out Docker client may leave its daemon-owned container alive.
        # Only this invocation's random ownership label can authorize cleanup.
        try:
            children = docker(
                [
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    f"label={LABEL}={identifier}",
                ]
            ).splitlines()
            for child in children:
                require(re.fullmatch(r"[a-f0-9]{64}", child) is not None, "Invalid owned child ID")
                docker(["container", "rm", "--force", child])
            volumes = docker(["volume", "ls", "--quiet", "--filter", f"label={LABEL}={identifier}"])
            if volumes:
                require(volumes == volume, "Ambiguous owned auth volume")
                docker(["volume", "rm", volume])
        except Exception:
            print(
                f"Auth smoke cleanup failed; inspect only owned resource {volume}.", file=sys.stderr
            )
            raise
    print("AMD64 App authentication and token revocation passed; private test volume removed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        require(args.env_file.is_file(), "Explicit private App environment file required")
        settings = GitHubAppSettings(_env_file=args.env_file)  # pyright: ignore[reportCallIssue]
        smoke(args.image, settings)
        return 0
    except Exception as error:
        print(
            f"Live App-auth smoke failed ({type(error).__name__}); no secrets printed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
