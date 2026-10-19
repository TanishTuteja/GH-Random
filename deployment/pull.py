"""Outbound-only approved release puller. No SSH listener or Actions runner on the VM."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from database_backup import check_private, private_file, sync_directory
from deploy import CRITICAL_FILES, TAG, atomic_json, deployment_lock, require, validate_url
from github_api import REPOSITORY, Credential, GitHubAPI

TASK = "gh-random:deploy"
WORKFLOW = ".github/workflows/deploy.yaml"
QUEUE_JOB = "Queue approved deployment"
REQUIRED_JOBS = {"Python checks", "PostgreSQL tests", "Production containers"}
MAX_ARCHIVE = 16 * 1024 * 1024


class RequestNotReady(RuntimeError):
    """The trusted queue job/artifact has not finished propagating yet."""


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def get(api: GitHubAPI, path: str) -> Any:
    return api.get(api.base + "/" + path)


def timestamp(value: str) -> float:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "An aware GitHub timestamp is required")
    return result.timestamp()


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def blob(api: GitHubAPI, sha: str) -> bytes:
    require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "Invalid Git blob")
    value = get(api, f"git/blobs/{sha}")
    require(value["encoding"] == "base64" and value["size"] <= MAX_ARCHIVE, "Invalid Git blob")
    data = base64.b64decode(value["content"])
    require(blob_sha(data) == sha, "Git blob content mismatch")
    return data


def source_release(api: GitHubAPI, tag: str) -> dict[str, Any]:
    """Recheck signed stable tag, main ancestry/version and latest exact-commit CI."""
    require(TAG.fullmatch(tag) is not None, "Stable signed version required")
    ref = get(api, f"git/ref/tags/{tag}")["object"]
    require(ref["type"] == "tag", "Annotated signed tag required")
    signed = get(api, f"git/tags/{ref['sha']}")
    require(
        signed["tag"] == tag
        and signed["verification"]["verified"] is True
        and signed["object"]["type"] == "commit",
        "Unverified release tag",
    )
    sha = signed["object"]["sha"]
    require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "Invalid source commit")
    require(
        get(api, f"compare/main...{sha}")["merge_base_commit"]["sha"] == sha, "Release not on main"
    )
    tree = get(api, f"git/trees/{sha}?recursive=1")
    require(not tree.get("truncated") and len(tree["tree"]) <= 2000, "Source tree too large")
    files = {
        item["path"]: item["sha"]
        for item in tree["tree"]
        if item["type"] == "blob" and item["mode"] == "100644"
    }
    version = tomllib.loads(blob(api, files["pyproject.toml"]).decode())["project"]["version"]
    require(version == tag[1:], "Release source version mismatch")
    runs = get(
        api, f"actions/workflows/ci.yaml/runs?head_sha={sha}&event=push&branch=main&per_page=100"
    )["workflow_runs"]
    require(bool(runs), "No source main CI")
    run = max(runs, key=lambda item: item["id"])
    require(
        run["head_sha"] == sha
        and run["event"] == "push"
        and run["head_branch"] == "main"
        and run["status"] == "completed"
        and run["conclusion"] == "success",
        "Latest source CI not successful",
    )
    jobs = get(api, f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
    require(
        jobs["total_count"] <= 100
        and REQUIRED_JOBS
        <= {item["name"] for item in jobs["jobs"] if item["conclusion"] == "success"},
        "Required source checks missing",
    )
    release = get(api, f"releases/tags/{tag}")
    require(
        not release["draft"] and not release["prerelease"] and release["tag_name"] == tag,
        "Unpublished stable release",
    )
    return {"commit": sha, "tag_object": ref["sha"], "release": release, "files": files}


def verify_request(
    api: GitHubAPI, deployment: dict[str, Any], *, max_age: int = 1800
) -> dict[str, Any]:
    """A payload is NOT authority: bind it to the approved job's immutable artifact."""
    require(
        deployment["task"] == TASK
        and deployment["environment"] == "production"
        and deployment["creator"]["login"] == "github-actions[bot]"
        and deployment["performed_via_github_app"]["slug"] == "github-actions",
        "Not an Actions production request",
    )
    payload = deployment["payload"]
    require(isinstance(payload, dict) and payload.get("format") == 1, "Unknown request format")
    require(
        0 <= time.time() - timestamp(deployment["created_at"]) <= max_age, "Expired/future request"
    )
    run_id, attempt = payload["run_id"], payload["run_attempt"]
    require(
        type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0,
        "Invalid request run",
    )
    run = get(api, f"actions/runs/{run_id}/attempts/{attempt}")
    require(
        run["event"] == "workflow_dispatch"
        and run["head_branch"] == "main"
        and run["path"] == WORKFLOW
        and run["head_sha"] == payload["workflow_sha"]
        and run["run_attempt"] == attempt
        and run["repository"]["full_name"] == api.repository
        and run["status"] == "in_progress"
        and run.get("conclusion") is None,
        "Request workflow is canceled, finished, or untrusted",
    )
    require(
        get(api, f"compare/main...{run['head_sha']}")["merge_base_commit"]["sha"]
        == run["head_sha"],
        "Workflow not on main",
    )
    # Bind to the *installed* operator-reviewed workflow, not any job with this name.
    workflow = get(api, f"contents/{WORKFLOW}?ref={run['head_sha']}")
    expected = Path(__file__).resolve().parents[1] / WORKFLOW
    require(
        workflow["sha"] == blob_sha(expected.read_bytes()),
        "Workflow changed: upgrade puller explicitly",
    )
    jobs = get(api, f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    require(jobs["total_count"] <= 100, "Too many request jobs")
    matching = [item for item in jobs["jobs"] if item["name"] == QUEUE_JOB]
    if not matching or matching[0]["conclusion"] is None:
        raise RequestNotReady("Approval job not complete")
    require(len(matching) == 1 and matching[0]["conclusion"] == "success", "Approval job failed")
    artifacts = get(api, f"actions/runs/{run_id}/artifacts?per_page=100")
    require(artifacts["total_count"] <= 100, "Too many request artifacts")
    matching = [
        item
        for item in artifacts["artifacts"]
        if item["name"] == f"approved-production-{run_id}-{attempt}"
    ]
    if not matching:
        raise RequestNotReady("Approval artifact unavailable")
    require(len(matching) == 1, "Duplicate approval artifact")
    artifact = matching[0]
    require(
        not artifact["expired"]
        and artifact["size_in_bytes"] <= 65536
        and artifact["workflow_run"]["id"] == run_id
        and artifact["workflow_run"]["head_sha"] == run["head_sha"],
        "Approval artifact/run mismatch",
    )
    raw = api.download(api.base + f"/actions/artifacts/{artifact['id']}/zip", limit=65536)
    require(
        artifact["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest(),
        "Artifact digest mismatch",
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == ["request.json"], "Unexpected approval artifact files")
        info = archive.infolist()[0]
        require(info.file_size <= 16384 and not info.is_dir(), "Oversize request artifact")
        approved = json.loads(archive.read(info))
    require(
        approved == {"deployment_id": deployment["id"], "payload": payload},
        "Approval/request mismatch",
    )
    require(deployment["sha"] == payload["commit"], "Deployment/source mismatch")
    return payload


def verify_bundle(
    api: GitHubAPI, payload: dict[str, Any], raw: bytes
) -> tuple[dict[str, Any], dict[str, bytes]]:
    require(len(raw) <= MAX_ARCHIVE, "Compressed deployment archive too large")
    # Tarfile processes PAX/GNU extension headers before yielding a member.
    # Bound the ENTIRE expanded stream, including those headers, first.
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        expanded = compressed.read(MAX_ARCHIVE + 1)
    require(len(expanded) <= MAX_ARCHIVE, "Deployment archive expanded too large")
    source = source_release(api, payload["tag"])
    require(
        source["commit"] == payload["commit"] and source["release"]["id"] == payload["release_id"],
        "Release request moved",
    )
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            require(
                member.isfile()
                and not path.is_absolute()
                and ".." not in path.parts
                and str(path) == member.name
                and "\\" not in member.name
                and member.name not in files
                and len(files) < 200
                and member.size <= MAX_ARCHIVE,
                "Unsafe deployment archive member",
            )
            total += member.size
            require(total <= MAX_ARCHIVE, "Deployment archive expanded too large")
            stream = archive.extractfile(member)
            require(stream is not None, "Missing archive content")
            assert stream is not None
            files[member.name] = stream.read()
    metadata = json.loads(files.pop("release.json"))
    require(
        metadata["deployment_format"] == 1
        and metadata["repository"] == api.repository
        and metadata["tag"] == payload["tag"]
        and metadata["commit"] == source["commit"]
        and metadata["tag_object"] == source["tag_object"]
        and metadata["release_id"] == payload["release_id"]
        and metadata["platform"] == "linux/arm64"
        and re.fullmatch(r"sha256:[0-9a-f]{64}", metadata["digest"]) is not None
        and metadata["image"] == f"ghcr.io/{api.repository.lower()}@{metadata['digest']}",
        "Deployment release metadata mismatch",
    )
    require(
        set(files) == set(metadata["deployment_files"]) and CRITICAL_FILES <= files.keys(),
        "Incomplete deployment bundle",
    )
    for name, data in files.items():
        require(
            hashlib.sha256(data).hexdigest() == metadata["deployment_files"][name],
            "Deployment hash mismatch",
        )
        if name == "deployment/compose.env.example":
            original = blob(api, source["files"][name]).decode().replace("\r\n", "\n")
            transformed, count = re.subn(
                r"(?m)^GH_RANDOM_IMAGE=.*$", f"GH_RANDOM_IMAGE={metadata['image']}", original
            )
            require(
                count == 1 and data == transformed.encode(),
                "Unexpected image template transformation",
            )
        else:
            require(
                name in source["files"] and blob_sha(data) == source["files"][name],
                "Bundle differs from signed source",
            )
    files["release.json"] = canonical(metadata)
    return metadata, files


def attest(api: GitHubAPI, metadata: dict[str, Any], archive: Path, gh: Path) -> None:
    """GitHub CLI verifies signed certificate identities, not self-asserted predicates."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GH_", "GITHUB_", "SIGSTORE_"))
    }
    environment["GH_TOKEN"] = api.credential.token()
    policy = [
        "--repo",
        api.repository,
        "--signer-workflow",
        api.repository + "/.github/workflows/release.yaml",
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
    for target in (str(archive), "oci://" + metadata["image"]):
        result = subprocess.run(
            [str(gh), "attestation", "verify", target, *policy],
            env=environment,
            capture_output=True,
            timeout=120,
            check=False,
        )
        require(result.returncode == 0, "Signed release attestation verification failed")


def status(api: GitHubAPI, identifier: int, state: str, description: str, *, url: str = "") -> None:
    value: dict[str, Any] = {"state": state, "description": description, "auto_inactive": False}
    if url:
        value["environment_url"] = url
    api.post(api.base + f"/deployments/{identifier}/statuses", value)


def load_config(path: Path) -> dict[str, Any]:
    check_private(path.parent)
    check_private(path)
    config = json.loads(path.read_text())
    require(REPOSITORY.fullmatch(config.get("repository", "")) is not None, "Invalid repository")
    require(config.get("credential", {}).get("type") == "app", "Dedicated deployment App required")
    for key in (
        "state_dir",
        "release_dir",
        "env_file",
        "backup_dir",
        "backup_config",
        "alerts_config",
        "gh_binary",
    ):
        require(
            isinstance(config.get(key), str) and Path(config[key]).is_absolute(),
            "Absolute deployment paths required",
        )
    for key in ("env_file", "backup_config", "alerts_config"):
        check_private(Path(config[key]).parent)
        check_private(Path(config[key]))
    gh = Path(config["gh_binary"])
    require(
        gh.is_file() and not gh.is_symlink() and os.access(gh, os.X_OK), "GitHub CLI unavailable"
    )
    if os.name == "posix":
        require(
            gh.stat().st_uid in (0, os.geteuid()) and gh.stat().st_mode & 0o022 == 0,
            "Unsafe GitHub CLI permissions",
        )
    validate_url(config["url"], allow_loopback_http=False)
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9_-]*", config["project_name"]) is not None, "Invalid project"
    )
    return config


def execute(api: GitHubAPI, config: dict[str, Any], deployment: dict[str, Any]) -> None:
    state = Path(config["state_dir"])
    journal = state / f"github-deployment-{deployment['id']}.json"
    # Durable outbox + at-most-once execution. Never retry a migration after a crash.
    if journal.exists():
        check_private(journal)
        record = json.loads(journal.read_text())
        if record["state"] == "in_progress":
            record.update(
                state="error",
                description="Puller interrupted; inspect VM journal before a new request",
            )
            atomic_json(journal, record)
        status(api, deployment["id"], record["state"], record["description"], url=config["url"])
        return
    payload = verify_request(api, deployment)
    require(
        payload.get("app_id") == config["credential"]["app_id"], "Request targets another VM App"
    )
    release = get(api, f"releases/{payload['release_id']}")
    assets = get(api, f"releases/{release['id']}/assets?per_page=100")
    matching = [
        asset
        for asset in assets
        if asset["name"] == f"gh-random-{payload['tag']}-deployment.tar.gz"
    ]
    require(
        len(matching) == 1 and matching[0]["size"] <= MAX_ARCHIVE,
        "Missing release deployment archive",
    )
    asset = matching[0]
    raw = api.download(api.base + f"/releases/assets/{asset['id']}", limit=MAX_ARCHIVE)
    require(
        asset["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest(),
        "Release asset digest mismatch",
    )
    metadata, files = verify_bundle(api, payload, raw)
    releases = Path(config["release_dir"])
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(releases)
    root = releases / f"{metadata['tag']}-{metadata['commit']}-{deployment['id']}"
    require(not root.exists(), "Release path already exists; inspect before new request")
    root.mkdir(mode=0o700)
    archive_path = root / "verified-deployment.tar.gz"
    with private_file(archive_path) as output:
        output.write(raw)
    attest(api, metadata, archive_path, Path(config["gh_binary"]))
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with private_file(target) as output:
            output.write(data)
        # This public config is bind-mounted into Caddy's unprivileged UID.
        # Keep executable deployment tools/private examples restricted; Docker
        # resolves the mode-0700 parent directory as the trusted host daemon.
        if name == "Caddyfile":
            target.chmod(0o644)
    sync_directory(root)
    # Approval can be canceled while downloads/crypto run. Recheck at the last safe boundary.
    verify_request(api, get(api, f"deployments/{deployment['id']}"))
    command = [
        sys.executable,
        str(root / "deployment/deploy.py"),
        "apply",
        "--release-dir",
        str(root),
        "--env-file",
        config["env_file"],
        "--state-dir",
        config["state_dir"],
        "--backup-dir",
        config["backup_dir"],
        "--backup-config",
        config["backup_config"],
        "--alerts-config",
        config["alerts_config"],
        "--project-name",
        config["project_name"],
        "--url",
        config["url"],
    ]
    if not (state / "current.json").exists():
        require(
            config.get("allow_initial_bootstrap") is True,
            "Initial bootstrap requires explicit VM opt-in",
        )
        command.append("--bootstrap")
    record = {
        "state": "in_progress",
        "description": "VM applying verified release; cancellation will not interrupt migrations",
        "tag": payload["tag"],
    }
    atomic_json(journal, record)
    status(api, deployment["id"], record["state"], record["description"], url=config["url"])
    # No subprocess timeout and no remote cancellation: deploy.py owns bounded
    # database/session lifecycle. systemd stop is an explicit host operation.
    result = subprocess.run(command, check=False)
    record.update(
        state="success" if result.returncode == 0 else "failure",
        description="VM deployment and HTTPS smoke tests succeeded"
        if result.returncode == 0
        else "VM deployment failed; inspect journal; no automatic rollback",
    )
    atomic_json(journal, record)
    status(api, deployment["id"], record["state"], record["description"], url=config["url"])


def poll(config: dict[str, Any]) -> None:
    api = GitHubAPI(config["repository"], Credential(config["repository"], config["credential"]))
    state = Path(config["state_dir"])
    # Separate queue lock: deploy.py owns shared deployment/crawler/backup lock.
    queue_state = state / "github-puller"
    with deployment_lock(queue_state):
        check_private(state)
        processed = [
            int(path.stem.removeprefix("github-deployment-"))
            for path in state.glob("github-deployment-*.json")
            if re.fullmatch(r"github-deployment-[0-9]+", path.stem)
        ]
        latest = max(processed, default=0)
        requests = get(
            api, "deployments?task=gh-random%3Adeploy&environment=production&per_page=20"
        )
        for deployment in sorted(requests, key=lambda item: item["id"], reverse=True):
            statuses = get(api, f"deployments/{deployment['id']}/statuses?per_page=1")
            journal = state / f"github-deployment-{deployment['id']}.json"
            if journal.exists():
                record = json.loads(journal.read_text())
                if (
                    not statuses
                    or statuses[0]["state"] != record["state"]
                    or statuses[0].get("performed_via_github_app", {}).get("id")
                    != config["credential"]["app_id"]
                    or record["state"] == "in_progress"
                ):
                    execute(api, config, deployment)
                continue
            if deployment["id"] < latest:
                record = {
                    "state": "inactive",
                    "description": "Superseded; dispatch again explicitly if needed",
                }
                atomic_json(journal, record)
                status(api, deployment["id"], record["state"], record["description"])
                continue
            if statuses and statuses[0]["state"] not in ("queued", "pending"):
                continue
            try:
                execute(api, config, deployment)
            except RequestNotReady:
                pass
            except ValueError:
                record = {
                    "state": "error",
                    "description": "Request rejected by VM verification; no deployment started",
                }
                atomic_json(journal, record)
                status(api, deployment["id"], record["state"], record["description"])
                raise RuntimeError("Deployment request rejected; inspect GitHub status") from None
            except Exception as error:
                # Not-ready requests may become verifiable next tick. No root
                # script executes until verification completes; no payload logged.
                print(
                    f"Request {deployment['id']} not applied ({type(error).__name__})",
                    file=sys.stderr,
                )
                raise
            # At most one request per poll, newest first. No obsolete queued rollback.
            break


def queue(api: GitHubAPI, tag: str, output: Path) -> int:
    require(
        os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
        "Dispatch on main only",
    )
    source = source_release(api, tag)
    app_id = int(os.environ.get("PRODUCTION_DEPLOYMENT_APP_ID", "0"))
    require(app_id > 0, "Configure the dedicated VM App ID before queueing production")
    payload = {
        "format": 1,
        "tag": tag,
        "commit": source["commit"],
        "release_id": source["release"]["id"],
        "workflow_sha": os.environ["GITHUB_SHA"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "app_id": app_id,
    }
    deployment = api.post(
        api.base + "/deployments",
        {
            "ref": source["commit"],
            "task": TASK,
            "environment": "production",
            "auto_merge": False,
            "required_contexts": [],
            "production_environment": True,
            "transient_environment": False,
            "description": "Approved release waiting for outbound VM puller",
            "payload": payload,
        },
    )
    identifier = deployment["id"]
    output.write_bytes(canonical({"deployment_id": identifier, "payload": payload}))
    status(api, identifier, "queued", "Approved request queued; NOT yet deployed")
    return identifier


def wait(api: GitHubAPI, identifier: int, *, seconds: int = 7200) -> None:
    app_id = int(os.environ.get("PRODUCTION_DEPLOYMENT_APP_ID", "0"))
    require(app_id > 0, "Expected VM App ID required")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        statuses = get(api, f"deployments/{identifier}/statuses?per_page=1")
        if statuses:
            if statuses[0]["state"] in {"queued", "pending"}:
                time.sleep(30)
                continue
            require(
                statuses[0].get("performed_via_github_app", {}).get("id") == app_id,
                "Status is not from the dedicated VM App",
            )
            state = statuses[0]["state"]
            if state == "success":
                return
            require(state not in {"failure", "error", "inactive"}, "VM deployment did not succeed")
        time.sleep(30)
    raise RuntimeError(
        "No verified VM success before observation timeout; request was NOT canceled"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("poll", "queue", "wait"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--deployment-id", type=int)
    args = parser.parse_args()
    try:
        if args.command == "poll":
            require(args.config is not None, "Private configuration required")
            config = load_config(args.config)
            poll(config)
            # Clear only the service incident, never a failed deployment record.
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("notifications.py")),
                    "event",
                    "--config",
                    config["alerts_config"],
                    "--issue",
                    "unit:gh-random-pull.service",
                    "--status",
                    "recovered",
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
            require(result.returncode == 0, "Puller recovery notification failed")
        else:
            repository = os.environ["GITHUB_REPOSITORY"]
            api = GitHubAPI(repository, Credential.from_env(repository))
            if args.command == "queue":
                require(args.tag is not None and args.output is not None, "Tag and output required")
                assert args.tag is not None and args.output is not None
                identifier = queue(api, args.tag, args.output)
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"deployment_id={identifier}\n")
                print(f"Queued deployment {identifier}; not yet deployed")
            else:
                require(
                    args.deployment_id is not None and args.deployment_id > 0,
                    "Deployment ID required",
                )
                assert args.deployment_id is not None
                wait(api, args.deployment_id)
                print("VM reports verified deployment success")
    except Exception as error:
        print(f"Deployment automation failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
