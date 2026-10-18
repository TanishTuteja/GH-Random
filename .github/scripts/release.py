"""Fail-closed release checks and an allowlisted, digest-pinned deployment bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_JOBS = {"Python checks", "PostgreSQL tests", "Production containers"}
DEPLOYMENT_FILES = (
    ".github/workflows/deploy.yaml",
    "compose.production.yaml",
    "compose.pgadmin.yaml",
    "deployment/pgadmin.env.example",
    "deployment/PGADMIN.md",
    "Caddyfile",
    "deployment/compose.env.example",
    "deployment/postgres.env.example",
    "deployment/app.env.example",
    "deployment/crawler.env.example",
    "deployment/caddy.env.example",
    "deployment/database_backup.py",
    "deployment/deploy.py",
    "deployment/github_api.py",
    "deployment/external_monitor.py",
    "deployment/telemetry.json.example",
    "deployment/MONITORING.md",
    "deployment/systemd/gh-random-telemetry.service",
    "deployment/systemd/gh-random-telemetry.timer",
    "deployment/pull.py",
    "deployment/pull.json.example",
    "deployment/AUTOMATION.md",
    "deployment/systemd/gh-random-pull.service",
    "deployment/systemd/gh-random-pull.timer",
    "deployment/crawler.py",
    "deployment/notifications.py",
    "deployment/backup.py",
    "deployment/backup_archive.py",
    "deployment/backup.json.example",
    "deployment/install_age.py",
    "deployment/BACKUPS.md",
    "deployment/systemd/gh-random-backup.service",
    "deployment/systemd/gh-random-backup.timer",
    "deployment/OPERATIONS.md",
    "deployment/alerts.json.example",
    "deployment/systemd/gh-random-alert@.service",
    "deployment/systemd/gh-random-monitor.service",
    "deployment/systemd/gh-random-monitor.timer",
    "deployment/systemd/gh-random-crawler.service",
    "deployment/systemd/gh-random-crawler.timer",
    "deployment/systemd/journald-limits.conf.example",
    "README.md",
    "LICENSE",
)
# Stable versions only for now; prereleases need an explicit policy first.
VERSION = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)
SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
Api = Callable[[str], Any]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_identity(tag: str, sha: str, repository: str, version: str) -> None:
    require(VERSION.fullmatch(tag) is not None, "Use a stable vMAJOR.MINOR.PATCH tag")
    require(tag[1:] == version, "Tag does not match pyproject.toml version")
    require(SHA.fullmatch(sha) is not None, "Invalid source commit")
    require(REPOSITORY.fullmatch(repository) is not None, "Invalid repository")


def github_api(repository: str) -> Api:
    token = os.environ["GH_TOKEN"]

    def get(path: str) -> Any:
        # Only fixed GitHub API paths constructed here; never follow API-provided URLs.
        request = Request(
            f"https://api.github.com/repos/{repository}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "gh-random-release",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            # Do not dump authenticated request headers or arbitrary response bodies.
            raise RuntimeError(f"GitHub release check failed: HTTP {error.code}") from None

    return get


def verify_release(
    api: Api, *, tag: str, sha: str, repository: str, version: str, release_id: int
) -> dict[str, Any]:
    validate_identity(tag, sha, repository, version)
    ref = api(f"git/ref/tags/{tag}")
    require(ref["object"]["type"] == "tag", "Release tag must be annotated and signed")
    tag_object = api(f"git/tags/{ref['object']['sha']}")
    require(tag_object["tag"] == tag, "Unexpected tag object")
    verification = tag_object["verification"]
    require(verification["verified"] is True, "GitHub must verify the release tag signature")
    require(tag_object["object"]["type"] == "commit", "Tag must point directly to a commit")
    require(tag_object["object"]["sha"] == sha, "Tag moved or checkout does not match tag")

    comparison = api(f"compare/main...{sha}")
    require(comparison["merge_base_commit"]["sha"] == sha, "Source must belong to main")
    release = api(f"releases/{release_id}")
    require(release["tag_name"] == tag, "Release tag mismatch")
    require(not release["draft"] and not release["prerelease"], "Use a published stable release")
    # Never replace a completed release, including on an accidental workflow rerun.
    for page in range(1, 101):
        assets = api(f"releases/{release_id}/assets?per_page=100&page={page}")
        for asset in assets:
            name = asset["name"]
            require(
                name not in {"release.json", "image-digest.txt", "SHA256SUMS"}
                and not name.endswith("-deployment.tar.gz"),
                "Release already has deployment assets; do not overwrite a released version",
            )
        if len(assets) < 100:
            break
    else:
        raise ValueError("Too many release assets to verify safely")

    query = urlencode({"head_sha": sha, "event": "push", "branch": "main", "per_page": 100})
    runs = api(f"actions/workflows/ci.yaml/runs?{query}")["workflow_runs"]
    require(bool(runs), "No main-push CI run for this exact source commit")
    run = max(runs, key=lambda item: item["id"])
    require(
        run["head_sha"] == sha and run["event"] == "push" and run["head_branch"] == "main",
        "Unexpected CI source",
    )
    require(
        run["status"] == "completed" and run["conclusion"] == "success",
        "Latest main CI must finish successfully before publishing the release",
    )
    jobs = api(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
    require(jobs["total_count"] <= 100, "Too many CI jobs to verify safely")
    passed = {job["name"] for job in jobs["jobs"] if job["conclusion"] == "success"}
    require(REQUIRED_JOBS <= passed, "Required CI jobs did not all pass")
    return {
        "tag": tag,
        "commit": sha,
        "tag_object": ref["object"]["sha"],
        "repository": repository,
        "image": f"ghcr.io/{repository.lower()}",
        "ci_run_id": run["id"],
        "release_id": release_id,
        "platform": "linux/arm64",
    }


def bundle_release(root: Path, output: Path, metadata: dict[str, Any], digest: str) -> None:
    validate_identity(
        metadata["tag"], metadata["commit"], metadata["repository"], metadata["tag"][1:]
    )
    require(DIGEST.fullmatch(digest) is not None, "Invalid image digest")
    image = f"ghcr.io/{metadata['repository'].lower()}@{digest}"
    metadata = {**metadata, "image": image, "digest": digest}
    # Explicit allowlist: never glob deployment files, .env files, or private keys.
    files: dict[str, bytes] = {}
    for name in DEPLOYMENT_FILES:
        path = root / name
        require(path.is_file() and not path.is_symlink(), f"Missing/unsafe release file: {name}")
        require(path.resolve().is_relative_to(root.resolve()), "File escapes release checkout")
        files[name] = path.read_bytes()
    compose = files["deployment/compose.env.example"].decode("utf-8").replace("\r\n", "\n")
    compose, count = re.subn(r"(?m)^GH_RANDOM_IMAGE=.*$", f"GH_RANDOM_IMAGE={image}", compose)
    require(count == 1, "Expected exactly one GH_RANDOM_IMAGE in the deployment template")
    files["deployment/compose.env.example"] = compose.encode("utf-8")
    metadata["deployment_format"] = 1
    metadata["deployment_files"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()
    }
    files["release.json"] = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
    output.mkdir(parents=True, exist_ok=False)
    (output / "release.json").write_bytes(files["release.json"])
    (output / "image-digest.txt").write_text(image + "\n", encoding="utf-8")
    with tarfile.open(output / f"gh-random-{metadata['tag']}-deployment.tar.gz", "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    checksums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(output.iterdir())
    )
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify", "bundle"])
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--digest")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=ROOT).strip()
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "version"
        ]
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event_file:
            event = json.load(event_file)
        repository = os.environ["GITHUB_REPOSITORY"]
        metadata = verify_release(
            github_api(repository),
            tag=event["release"]["tag_name"],
            sha=sha,
            repository=repository,
            version=version,
            release_id=event["release"]["id"],
        )
        args.metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key in ("commit", "tag", "image"):
                output.write(f"{key}={metadata[key]}\n")
        print(f"Verified {metadata['tag']} at {sha}, CI run {metadata['ci_run_id']}")
    else:
        require(args.output is not None and args.digest is not None, "Supply --output and --digest")
        bundle_release(
            ROOT,
            args.output,
            json.loads(args.metadata.read_text(encoding="utf-8")),
            args.digest,
        )


if __name__ == "__main__":
    main()
