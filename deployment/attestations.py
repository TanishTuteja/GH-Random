"""Shared, fail-closed verification of signed release archive and image provenance.

Use a reviewed official GitHub CLI installed through the operator's normal trusted
package process. A version string is a compatibility/security floor, not proof of
binary authenticity. Never expose CLI output: failures may include private data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

MINIMUM_VERSION = (2, 97, 0)
VERSION = re.compile(
    r"gh version ((0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)) "
    r"\((\d{4}-\d{2}-\d{2})\)",
    re.ASCII,
)
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)


class AttestationError(RuntimeError):
    """Safe, body/header/token-free verification failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AttestationError(message)


def run(arguments: list[str], environment: dict[str, str], *, timeout: int, limit: int) -> bytes:
    """Bound complete CLI runtime and private output, including DNS stalls."""
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            with subprocess.Popen(
                arguments, stdout=output, stderr=errors, env=environment
            ) as child:
                deadline = time.monotonic() + timeout
                while child.poll() is None:
                    if output.tell() + errors.tell() > limit or time.monotonic() >= deadline:
                        child.kill()
                        child.wait(timeout=10)
                        raise AttestationError("GitHub CLI exceeded its time/output limit")
                    time.sleep(0.05)
                require(child.returncode == 0, "GitHub CLI verification failed")
            require(output.tell() + errors.tell() <= limit, "GitHub CLI output exceeds limit")
            output.seek(0)
            return output.read(limit + 1)
    except (OSError, subprocess.SubprocessError):
        raise AttestationError("GitHub CLI is unavailable or interrupted") from None


def version(output: bytes) -> tuple[int, int, int]:
    """Reject unreviewed dev/prerelease/vendor formats, not just missing flags."""
    try:
        lines = output.decode("utf-8").strip().splitlines()
        require(len(lines) == 2, "Unrecognized official GitHub CLI version format")
        match = VERSION.fullmatch(lines[0])
        require(match is not None, "A stable official GitHub CLI version is required")
        assert match is not None
        date.fromisoformat(match[5])
        require(
            lines[1] == "https://github.com/cli/cli/releases/tag/v" + match[1],
            "Unrecognized official GitHub CLI release identity",
        )
        result = (int(match[2]), int(match[3]), int(match[4]))
        # GHSA-mm27-mwq9-fr5g: <=2.96.0 interpolates signer-workflow as regex.
        require(result >= MINIMUM_VERSION, "GitHub CLI 2.97.0 or newer is required")
        return result
    except (UnicodeError, ValueError):
        raise AttestationError("Unrecognized official GitHub CLI version") from None


def validate(metadata: dict[str, Any]) -> None:
    repository, commit, tag, digest = (
        metadata.get(key) for key in ("repository", "commit", "tag", "digest")
    )
    require(
        isinstance(repository, str) and REPOSITORY.fullmatch(repository) is not None,
        "Invalid attestation repository",
    )
    require(
        isinstance(commit, str) and COMMIT.fullmatch(commit) is not None,
        "Invalid attestation source commit",
    )
    require(isinstance(tag, str) and TAG.fullmatch(tag) is not None, "Invalid stable release tag")
    require(
        isinstance(digest, str) and DIGEST.fullmatch(digest) is not None,
        "Invalid attestation image digest",
    )
    assert isinstance(repository, str)
    require(
        metadata.get("image") == f"ghcr.io/{repository.lower()}@{digest}",
        "Use the exact repository's immutable image digest",
    )


def verify(metadata: dict[str, Any], archive: Path, gh: Path, *, token: str | None = None) -> None:
    """Enforce identical signer/source policy before publication or VM execution."""
    validate(metadata)
    require(archive.is_file() and not archive.is_symlink(), "Release archive is unavailable")
    selected = token if token is not None else os.environ.get("GH_TOKEN", "")
    require(
        isinstance(selected, str)
        and 0 < len(selected) <= 16384
        and not any(character.isspace() for character in selected),
        "A private GitHub API credential is required",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GH_", "GITHUB_", "SIGSTORE_"))
    }
    environment.update(GH_TOKEN=selected, GH_HOST="github.com", GH_PROMPT_DISABLED="1")
    policy = [
        "--repo",
        metadata["repository"],
        "--signer-workflow",
        metadata["repository"] + "/.github/workflows/release.yaml",
        "--source-ref",
        "refs/tags/" + metadata["tag"],
        "--source-digest",
        metadata["commit"],
        "--signer-digest",
        metadata["commit"],
        "--deny-self-hosted-runners",
        "--limit",
        "10",
    ]
    # Never depend on the operator's home, cached credentials or writable
    # host configuration. This also works with systemd ProtectHome=true and
    # ProtectSystem=strict: the unit's private temporary directory is writable.
    with tempfile.TemporaryDirectory(prefix="gh-random-attestation-") as temporary:
        runtime = Path(temporary)
        for variable in (
            "GH_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ):
            directory = runtime / variable.lower()
            directory.mkdir(mode=0o700)
            environment[variable] = str(directory)
        version(run([str(gh), "--version"], environment, timeout=10, limit=4096))
        for target in (str(archive.resolve()), "oci://" + metadata["image"]):
            run(
                [str(gh), "attestation", "verify", target, *policy],
                environment,
                timeout=120,
                limit=1024 * 1024,
            )


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "Duplicate release metadata key")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--gh", type=Path, default=Path("gh"))
    args = parser.parse_args()
    try:
        require(
            args.metadata.is_file()
            and not args.metadata.is_symlink()
            and args.metadata.stat().st_size <= 65536,
            "Release metadata is unavailable or oversized",
        )
        metadata = json.loads(args.metadata.read_bytes(), object_pairs_hook=unique)
        require(isinstance(metadata, dict), "Expected release metadata object")
        verify(metadata, args.archive, args.gh)
        print("Signed archive and image provenance verified against exact release identity.")
        return 0
    except Exception as error:
        print(
            f"Signed release verification failed ({type(error).__name__}); "
            "CLI output and private credentials were not printed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
