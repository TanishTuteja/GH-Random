"""Adversarial approval/source boundaries; no production credentials or writes."""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import importlib
import io
import json
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def pull_tools(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "deployment"))
    return importlib.import_module("pull")


class API:
    repository = "Owner/Repo"
    base = "/repos/Owner/Repo"

    def __init__(self):
        self.responses = {}
        self.downloads = {}
        self.posts = []
        self.credential = SimpleNamespace(token=lambda: "private-secret")

    def get(self, path):
        return copy.deepcopy(self.responses[path.removeprefix(self.base + "/")])

    def download(self, path, *, limit):
        data = self.downloads[path.removeprefix(self.base + "/")]
        assert len(data) <= limit
        return data

    def post(self, path, payload):
        self.posts.append((path, payload))
        if path.endswith("/statuses"):
            return {
                "id": 100,
                "state": payload["state"],
                "creator": {"id": 777},
                "performed_via_github_app": None,
            }
        return {"id": 100}


@pytest.fixture
def approved(pull_tools):
    api = API()
    sha = "a" * 40
    payload = {
        "format": 1,
        "tag": "v0.1.0",
        "commit": sha,
        "release_id": 8,
        "workflow_sha": sha,
        "run_id": 10,
        "run_attempt": 1,
        "app_id": 77,
        "creator_id": 777,
    }
    request = {
        "id": 100,
        "task": pull_tools.TASK,
        "environment": "production",
        "sha": sha,
        "creator": {"login": "github-actions[bot]"},
        "performed_via_github_app": None,
        # GitHub timestamps have second precision. Windows' datetime.now and
        # time.time can differ slightly within the same subsecond clock tick.
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "payload": payload,
    }
    run = {
        "id": 10,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "path": pull_tools.WORKFLOW,
        "head_sha": sha,
        "repository": {"full_name": api.repository},
        "status": "in_progress",
        "conclusion": None,
    }
    api.responses.update(
        {
            "actions/runs/10/attempts/1": run,
            f"compare/main...{sha}": {"merge_base_commit": {"sha": sha}},
            f"contents/{pull_tools.WORKFLOW}?ref={sha}": {
                "sha": pull_tools.blob_sha(
                    (
                        Path(pull_tools.__file__).resolve().parents[1] / pull_tools.WORKFLOW
                    ).read_bytes()
                )
            },
            "actions/runs/10/attempts/1/jobs?per_page=100": {
                "total_count": 1,
                "jobs": [{"name": pull_tools.QUEUE_JOB, "conclusion": "success"}],
            },
        }
    )
    with io.BytesIO() as stream:
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(
                "request.json", pull_tools.canonical({"deployment_id": 100, "payload": payload})
            )
        raw = stream.getvalue()
    api.downloads["actions/artifacts/5/zip"] = raw
    api.responses["actions/runs/10/artifacts?per_page=100"] = {
        "total_count": 1,
        "artifacts": [
            {
                "id": 5,
                "name": "approved-production-10-1",
                "expired": False,
                "size_in_bytes": len(raw),
                "workflow_run": {"id": 10, "head_sha": sha},
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        ],
    }
    return api, request


def test_request_must_match_run_bound_approved_artifact(pull_tools, approved):
    api, request = approved
    assert pull_tools.verify_request(api, request) == request["payload"]
    request["payload"]["tag"] = "v0.2.0"
    with pytest.raises(ValueError, match="Approval/request mismatch"):
        pull_tools.verify_request(api, request)


def test_optional_actions_app_metadata_cannot_override_creator_or_receipt(pull_tools, approved):
    api, request = approved
    assert request["performed_via_github_app"] is None
    assert pull_tools.verify_request(api, request) == request["payload"]
    request["performed_via_github_app"] = {"slug": "another-app"}
    with pytest.raises(ValueError, match="Actions production request"):
        pull_tools.verify_request(api, request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_branch", "attacker"),
        ("path", ".github/workflows/other.yaml"),
        ("event", "pull_request"),
        ("status", "completed"),
        ("run_attempt", 2),
    ],
)
def test_untrusted_or_canceled_run_rejected(pull_tools, approved, field, value):
    api, request = approved
    api.responses["actions/runs/10/attempts/1"][field] = value
    with pytest.raises(ValueError, match="workflow"):
        pull_tools.verify_request(api, request)


def test_queue_receipt_cannot_be_replayed_for_another_id(pull_tools, approved):
    api, request = approved
    request["id"] = 101
    with pytest.raises(ValueError, match="Approval/request"):
        pull_tools.verify_request(api, request)


def test_expired_request_and_changed_installed_workflow_rejected(pull_tools, approved):
    api, request = approved
    request["created_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="Expired"):
        pull_tools.verify_request(api, request)
    request["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    api.responses[f"contents/{pull_tools.WORKFLOW}?ref={request['sha']}"]["sha"] = "0" * 40
    with pytest.raises(ValueError, match="Workflow changed"):
        pull_tools.verify_request(api, request)


def test_incomplete_queue_job_is_retryable_without_claim(pull_tools, approved):
    api, request = approved
    api.responses["actions/runs/10/attempts/1/jobs?per_page=100"]["jobs"][0]["conclusion"] = None
    with pytest.raises(pull_tools.RequestNotReady):
        pull_tools.verify_request(api, request)


def test_tampered_receipt_digest_rejected(pull_tools, approved):
    api, request = approved
    api.downloads["actions/artifacts/5/zip"] += b"bad"
    with pytest.raises(ValueError, match="digest"):
        pull_tools.verify_request(api, request)


@pytest.fixture
def published(pull_tools, approved):
    api, request = approved
    sha = request["sha"]
    source_files = {name: b"signed source file\n" for name in pull_tools.CRITICAL_FILES}
    source_files["pyproject.toml"] = b'[project]\nversion = "0.1.0"\n'
    source_files["deployment/compose.env.example"] = b"GH_RANDOM_IMAGE=example\n"
    hashes = {name: pull_tools.blob_sha(data) for name, data in source_files.items()}
    for name, data in source_files.items():
        api.responses[f"git/blobs/{hashes[name]}"] = {
            "encoding": "base64",
            "content": base64.b64encode(data).decode(),
            "size": len(data),
        }
    api.responses.update(
        {
            "git/ref/tags/v0.1.0": {"object": {"type": "tag", "sha": "b" * 40}},
            "git/tags/" + "b" * 40: {
                "tag": "v0.1.0",
                "verification": {"verified": True},
                "object": {"type": "commit", "sha": sha},
            },
            f"git/trees/{sha}?recursive=1": {
                "truncated": False,
                "tree": [
                    {"path": name, "type": "blob", "mode": "100644", "sha": digest}
                    for name, digest in hashes.items()
                ],
            },
            f"actions/workflows/ci.yaml/runs?head_sha={sha}&event=push&branch=main&per_page=100": {
                "workflow_runs": [
                    {
                        "id": 20,
                        "head_sha": sha,
                        "head_branch": "main",
                        "event": "push",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
            "actions/runs/20/jobs?filter=latest&per_page=100": {
                "total_count": 3,
                "jobs": [
                    {"name": name, "conclusion": "success"} for name in pull_tools.REQUIRED_JOBS
                ],
            },
            "releases/tags/v0.1.0": {
                "id": 8,
                "tag_name": "v0.1.0",
                "draft": False,
                "prerelease": False,
            },
            f"actions/workflows/release.yaml/runs?head_sha={sha}&event=release&per_page=100": {
                "workflow_runs": [
                    {
                        "id": 30,
                        "head_sha": sha,
                        "head_branch": "v0.1.0",
                        "event": "release",
                        "path": ".github/workflows/release.yaml",
                        "repository": {"full_name": api.repository},
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            },
            "actions/runs/30/jobs?filter=latest&per_page=100": {
                "total_count": 2,
                "jobs": [
                    {"name": name, "conclusion": "success"}
                    for name in ("Verify release source", "Publish AMD64 release")
                ],
            },
            "releases/8/assets?per_page=100": [
                {
                    "name": name,
                    "state": "uploaded",
                    "id": index + 1,
                    "size": 100,
                    "digest": "sha256:" + "c" * 64,
                }
                for index, name in enumerate(
                    (
                        "release.json",
                        "image-digest.txt",
                        "SHA256SUMS",
                        "gh-random-v0.1.0-deployment.tar.gz",
                    )
                )
            ],
        }
    )
    digest = "sha256:" + "c" * 64
    image = "ghcr.io/owner/repo@" + digest
    bundled = {name: data for name, data in source_files.items() if name != "pyproject.toml"}
    bundled["deployment/compose.env.example"] = f"GH_RANDOM_IMAGE={image}\n".encode()
    metadata = {
        "deployment_format": 1,
        "repository": api.repository,
        "tag": "v0.1.0",
        "commit": sha,
        "tag_object": "b" * 40,
        "release_id": 8,
        "platform": "linux/amd64",
        "digest": digest,
        "image": image,
        "deployment_files": {
            name: hashlib.sha256(data).hexdigest() for name, data in bundled.items()
        },
    }
    bundled["release.json"] = pull_tools.canonical(metadata)
    return api, request, bundled


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as output:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            output.addfile(member, io.BytesIO(data))
    return stream.getvalue()


@pytest.mark.parametrize(
    "platform", ["linux/arm64", "linux/386", "windows/amd64", "linux/amd64/v3"]
)
def test_pull_worker_rejects_bundles_for_other_platforms(pull_tools, published, platform):
    api, request, files = published
    metadata = json.loads(files["release.json"])
    metadata["platform"] = platform
    files["release.json"] = pull_tools.canonical(metadata)
    with pytest.raises(ValueError, match="release metadata mismatch"):
        pull_tools.verify_bundle(api, request["payload"], archive(files))


def test_bundle_independently_matches_signed_source_not_manifest(pull_tools, published):
    api, request, files = published
    metadata, verified = pull_tools.verify_bundle(api, request["payload"], archive(files))
    assert verified.keys() == files.keys()
    assert metadata["image"].startswith("ghcr.io/owner/repo@sha256:")
    files["deployment/deploy.py"] = b"attacker root script"
    forged = json.loads(files["release.json"])
    forged["deployment_files"]["deployment/deploy.py"] = hashlib.sha256(
        files["deployment/deploy.py"]
    ).hexdigest()
    files["release.json"] = pull_tools.canonical(forged)
    with pytest.raises(ValueError, match="signed source"):
        pull_tools.verify_bundle(api, request["payload"], archive(files))


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "deployment/../../escape", "deployment\\escape"]
)
def test_archive_never_extracts_unsafe_paths(pull_tools, published, path):
    api, request, files = published
    files[path] = b"bad"
    with pytest.raises(ValueError, match="Unsafe"):
        pull_tools.verify_bundle(api, request["payload"], archive(files))


def test_pax_gzip_bomb_is_bounded_before_tar_parser(pull_tools, published, monkeypatch):
    api, request, _ = published
    member = tarfile.TarInfo("pax-header")
    member.type = tarfile.XHDTYPE
    member.size = 64 * 1024
    raw = gzip.compress(member.tobuf() + b"x" * member.size)
    monkeypatch.setattr(pull_tools, "MAX_ARCHIVE", 4096)
    monkeypatch.setattr(
        pull_tools.tarfile,
        "open",
        lambda *args, **kwargs: pytest.fail("Tar parsed before decompression cap"),
    )
    assert len(raw) < 4096
    with pytest.raises(ValueError, match="expanded too large"):
        pull_tools.verify_bundle(api, request["payload"], raw)


@pytest.mark.parametrize(
    "field,value",
    [("verification", {"verified": False}), ("object", {"type": "commit", "sha": "d" * 40})],
)
def test_signed_source_changes_rejected(pull_tools, published, field, value):
    api, request, files = published
    api.responses["git/tags/" + "b" * 40][field] = value
    with pytest.raises((ValueError, KeyError)):
        pull_tools.verify_bundle(api, request["payload"], archive(files))


@pytest.mark.parametrize(
    "status,conclusion",
    [("in_progress", None), ("completed", "failure"), ("completed", "cancelled")],
)
def test_published_release_is_not_ready_until_exact_release_workflow_succeeds(
    pull_tools, published, status, conclusion
):
    api, request, _ = published
    path = (
        f"actions/workflows/release.yaml/runs?head_sha={request['sha']}&event=release&per_page=100"
    )
    api.responses[path]["workflow_runs"][0].update(status=status, conclusion=conclusion)
    with pytest.raises(ValueError, match="release workflow"):
        pull_tools.source_release(api, "v0.1.0")


@pytest.mark.parametrize(
    "name", ["release.json", "image-digest.txt", "SHA256SUMS", "gh-random-v0.1.0-deployment.tar.gz"]
)
def test_partial_release_asset_upload_is_not_deployable(pull_tools, published, name):
    api, _, _ = published
    api.responses["releases/8/assets?per_page=100"] = [
        item for item in api.responses["releases/8/assets?per_page=100"] if item["name"] != name
    ]
    with pytest.raises(ValueError, match="incomplete"):
        pull_tools.source_release(api, "v0.1.0")


def test_latest_failed_release_attempt_cannot_use_old_success(pull_tools, published):
    api, request, _ = published
    path = (
        f"actions/workflows/release.yaml/runs?head_sha={request['sha']}&event=release&per_page=100"
    )
    old = api.responses[path]["workflow_runs"][0]
    api.responses[path]["workflow_runs"].append(
        {**old, "id": 31, "status": "completed", "conclusion": "failure"}
    )
    with pytest.raises(ValueError, match="release workflow"):
        pull_tools.source_release(api, "v0.1.0")


def test_other_tag_run_and_missing_release_jobs_are_rejected(pull_tools, published):
    api, request, _ = published
    path = (
        f"actions/workflows/release.yaml/runs?head_sha={request['sha']}&event=release&per_page=100"
    )
    api.responses[path]["workflow_runs"][0]["head_branch"] = "v0.2.0"
    with pytest.raises(ValueError, match="exact release tag"):
        pull_tools.source_release(api, "v0.1.0")
    api.responses[path]["workflow_runs"][0]["head_branch"] = "v0.1.0"
    api.responses["actions/runs/30/jobs?filter=latest&per_page=100"]["jobs"][1]["conclusion"] = (
        "skipped"
    )
    with pytest.raises(ValueError, match="release jobs"):
        pull_tools.source_release(api, "v0.1.0")


def test_claimed_interrupted_request_is_not_reexecuted(pull_tools, approved, tmp_path, monkeypatch):
    api, request = approved
    tmp_path.chmod(0o700)
    journal = tmp_path / "github-deployment-100.json"
    pull_tools.atomic_json(journal, {"state": "in_progress", "description": "started"})
    monkeypatch.setattr(
        pull_tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("Migration replayed")
    )
    pull_tools.execute(
        api,
        {
            "state_dir": str(tmp_path),
            "url": "https://example.com",
            "credential": {"app_id": 77},
            "creator_id": 777,
        },
        request,
    )
    assert json.loads(journal.read_text())["state"] == "error"
    assert api.posts[-1][1]["state"] == "error"


def test_puller_uses_shared_patched_attestation_policy(pull_tools, tmp_path, monkeypatch):
    api = API()
    api.credential = SimpleNamespace(token=lambda: "private-secret")
    calls = []

    def verify(metadata, archive, gh, *, token):
        calls.append((metadata, archive, gh, token))

    monkeypatch.setattr(pull_tools, "verify_attestations", verify)
    metadata = {
        "repository": api.repository,
        "image": "ghcr.io/owner/repo@sha256:" + "c" * 64,
        "digest": "sha256:" + "c" * 64,
        "tag": "v0.1.0",
        "commit": "a" * 40,
    }
    archive = tmp_path / "archive"
    pull_tools.attest(api, metadata, archive, Path("gh"))
    assert calls == [(metadata, archive, Path("gh"), "private-secret")]
    with pytest.raises(ValueError, match="repository mismatch"):
        pull_tools.attest(api, {**metadata, "repository": "Other/Repo"}, archive, Path("gh"))
    assert len(calls) == 1


def test_observer_rejects_forged_success(pull_tools, monkeypatch):
    api = API()
    monkeypatch.setenv("PRODUCTION_DEPLOYMENT_APP_ID", "77")
    monkeypatch.setenv("PRODUCTION_DEPLOYMENT_CREATOR_ID", "777")
    api.responses["deployments/100/statuses?per_page=1"] = [
        {"state": "success", "creator": {"id": 888}, "performed_via_github_app": None}
    ]
    with pytest.raises(ValueError, match="dedicated VM App"):
        pull_tools.wait(api, 100, seconds=1)


def test_observer_accepts_realistic_null_app_metadata_only_for_expected_bot(
    pull_tools, monkeypatch
):
    api = API()
    monkeypatch.setenv("PRODUCTION_DEPLOYMENT_APP_ID", "77")
    monkeypatch.setenv("PRODUCTION_DEPLOYMENT_CREATOR_ID", "777")
    api.responses["deployments/100/statuses?per_page=1"] = [
        {"state": "success", "creator": {"id": 777}, "performed_via_github_app": None}
    ]
    pull_tools.wait(api, 100, seconds=1)
    api.responses["deployments/100/statuses?per_page=1"][0]["performed_via_github_app"] = {"id": 88}
    with pytest.raises(ValueError, match="dedicated VM App"):
        pull_tools.wait(api, 100, seconds=1)
    api.responses["deployments/100/statuses?per_page=1"][0].update(
        creator={"id": 888}, performed_via_github_app={"id": 77}
    )
    with pytest.raises(ValueError, match="dedicated VM App"):
        pull_tools.wait(api, 100, seconds=1)


def test_old_queued_request_never_redeploys_after_newer_request(
    pull_tools, approved, tmp_path, monkeypatch
):
    api, request = approved
    tmp_path.chmod(0o700)
    pull_tools.atomic_json(
        tmp_path / "github-deployment-200.json",
        {
            "claimed": True,
            "state": "success",
            "description": "done",
            "reported": {
                "state": "success",
                "description": "done",
                "app_id": 77,
                "creator_id": 777,
                "status_id": 200,
            },
        },
    )
    api.responses["deployments?task=gh-random%3Adeploy&environment=production&per_page=20"] = [
        request
    ]
    api.responses["deployments/100/statuses?per_page=1"] = [{"state": "queued"}]
    monkeypatch.setattr(pull_tools, "GitHubAPI", lambda *args, **kwargs: api)
    monkeypatch.setattr(pull_tools, "Credential", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pull_tools, "execute", lambda *args: pytest.fail("Old queued release executed")
    )
    pull_tools.poll(
        {
            "repository": api.repository,
            "credential": {"app_id": 77},
            "creator_id": 777,
            "state_dir": str(tmp_path),
            "url": "https://example.com",
        }
    )
    assert api.posts[-1][1]["state"] == "inactive"


@pytest.mark.parametrize("cancel_last", [False, True])
def test_cancel_rechecked_before_claim_and_terminal_delivery_never_replays(
    pull_tools, published, tmp_path, monkeypatch, cancel_last
):
    api, request, files = published
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    raw = archive(files)
    api.responses["releases/8"] = {"id": 8}
    api.responses["releases/8/assets?per_page=100"] = [
        {
            "name": "gh-random-v0.1.0-deployment.tar.gz",
            "id": 9,
            "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "state": "uploaded",
        }
    ] + [
        {
            "name": name,
            "state": "uploaded",
            "id": index + 10,
            "size": 100,
            "digest": "sha256:" + "c" * 64,
        }
        for index, name in enumerate(("release.json", "image-digest.txt", "SHA256SUMS"))
    ]
    api.responses["deployments/100"] = request
    api.downloads["releases/assets/9"] = raw
    config = {
        "state_dir": str(state),
        "release_dir": str(tmp_path / "releases"),
        "env_file": str(tmp_path / "compose.env"),
        "backup_dir": str(tmp_path / "backups"),
        "backup_config": str(tmp_path / "backup.json"),
        "alerts_config": str(tmp_path / "alerts.json"),
        "gh_binary": "gh",
        "credential": {"app_id": 77},
        "creator_id": 777,
        "project_name": "fixture",
        "url": "https://example.com",
        "allow_initial_bootstrap": True,
    }
    monkeypatch.setattr(pull_tools, "attest", lambda *args: None)
    original_verify = pull_tools.verify_request
    count = 0

    def verify(*args, **kwargs):
        nonlocal count
        count += 1
        if cancel_last and count == 2:
            raise ValueError("Canceled before claim")
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(pull_tools, "verify_request", verify)
    executed = []
    monkeypatch.setattr(
        pull_tools.subprocess,
        "run",
        lambda command, **kwargs: executed.append(command) or SimpleNamespace(returncode=0),
    )
    if cancel_last:
        with pytest.raises(ValueError, match="Canceled before claim"):
            pull_tools.execute(api, config, request)
        assert not executed and not (state / "github-deployment-100.json").exists()
    else:
        pull_tools.execute(api, config, request)
        assert len(executed) == 1 and api.posts[-1][1]["state"] == "success"
        extracted = next((tmp_path / "releases").iterdir())
        assert (extracted / "Caddyfile").stat().st_mode & 0o004
        if pull_tools.os.name == "posix":
            assert extracted.stat().st_mode & 0o077 == 0
            assert (extracted / "deployment/deploy.py").stat().st_mode & 0o077 == 0
        assert "--backup-config" in executed[0] and "--alerts-config" in executed[0]
        pull_tools.execute(api, config, request)
        assert len(executed) == 1


def poll_fixture(tools, api, directory, monkeypatch):
    directory.chmod(0o700)
    monkeypatch.setattr(tools, "GitHubAPI", lambda *args, **kwargs: api)
    monkeypatch.setattr(tools, "Credential", lambda *args, **kwargs: None)
    return {
        "repository": api.repository,
        "credential": {"app_id": 77},
        "creator_id": 777,
        "state_dir": str(directory),
        "url": "https://example.com",
    }


def test_pending_local_outbox_drains_beyond_remote_page_without_resending(
    pull_tools, tmp_path, monkeypatch
):
    api = API()
    config = poll_fixture(pull_tools, api, tmp_path, monkeypatch)
    api.responses["deployments?task=gh-random%3Adeploy&environment=production&per_page=20"] = []
    for identifier in range(1, 23):
        record = {"claimed": True, "state": "success", "description": "done"}
        if identifier != 1:
            record["reported"] = {
                "state": "success",
                "description": "done",
                "app_id": 77,
                "creator_id": 777,
                "status_id": identifier,
            }
        pull_tools.atomic_json(tmp_path / f"github-deployment-{identifier}.json", record)
    monkeypatch.setattr(
        pull_tools.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Root deployment replayed"),
    )
    pull_tools.poll(config)
    assert len(api.posts) == 1 and api.posts[0][0].endswith("/deployments/1/statuses")
    journal = json.loads((tmp_path / "github-deployment-1.json").read_text())
    assert journal["reported"]["app_id"] == 77 and journal["reported"]["state"] == "success"
    pull_tools.poll(config)
    assert len(api.posts) == 1


def test_unrelated_same_state_is_not_a_status_acknowledgment(pull_tools, tmp_path, monkeypatch):
    api = API()
    config = poll_fixture(pull_tools, api, tmp_path, monkeypatch)
    journal = tmp_path / "github-deployment-1.json"
    record = {"claimed": True, "state": "success", "description": "done"}
    pull_tools.atomic_json(journal, record)
    monkeypatch.setattr(
        api,
        "post",
        lambda *args: {
            "id": 1,
            "state": "success",
            "creator": {"id": 888},
            "performed_via_github_app": None,
        },
    )
    with pytest.raises(ValueError, match="dedicated VM App"):
        pull_tools.report_record(api, config, 1, journal, record)
    assert "reported" not in json.loads(journal.read_text())


def test_status_decode_failure_does_not_clobber_success_journal(
    pull_tools, approved, tmp_path, monkeypatch
):
    api, request = approved
    config = poll_fixture(pull_tools, api, tmp_path, monkeypatch)
    api.responses["deployments?task=gh-random%3Adeploy&environment=production&per_page=20"] = [
        request
    ]
    api.responses["deployments/100/statuses?per_page=1"] = [{"state": "queued"}]
    original = pull_tools.execute
    journal = tmp_path / "github-deployment-100.json"

    def completed_then_bad_response(*args, **kwargs):
        pull_tools.atomic_json(
            journal,
            {"claimed": True, "state": "success", "description": "real execution succeeded"},
        )
        raise json.JSONDecodeError("bad status response", "", 0)

    monkeypatch.setattr(pull_tools, "execute", completed_then_bad_response)
    with pytest.raises(json.JSONDecodeError):
        pull_tools.poll(config)
    assert json.loads(journal.read_text())["state"] == "success"
    monkeypatch.setattr(pull_tools, "execute", original)
    api.responses["deployments/100/statuses?per_page=1"] = [
        {"state": "success", "creator": {"id": 777}, "performed_via_github_app": None}
    ]
    monkeypatch.setattr(
        pull_tools.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Root deployment replayed"),
    )
    pull_tools.poll(config)
    assert json.loads(journal.read_text())["reported"]["state"] == "success"


def test_rejected_newer_request_cannot_supersede_approved_request(
    pull_tools, approved, tmp_path, monkeypatch
):
    api, request = approved
    config = poll_fixture(pull_tools, api, tmp_path, monkeypatch)
    pull_tools.atomic_json(
        tmp_path / "github-deployment-200.json",
        {
            "state": "error",
            "description": "unapproved request rejected",
            "reported": {
                "state": "error",
                "description": "unapproved request rejected",
                "app_id": 77,
                "creator_id": 777,
                "status_id": 200,
            },
        },
    )
    api.responses["deployments?task=gh-random%3Adeploy&environment=production&per_page=20"] = [
        request
    ]
    api.responses["deployments/100/statuses?per_page=1"] = [{"state": "queued"}]
    executed = []
    monkeypatch.setattr(
        pull_tools, "execute", lambda api, cfg, value, **kwargs: executed.append(value["id"])
    )
    pull_tools.poll(config)
    assert executed == [100] and not api.posts
