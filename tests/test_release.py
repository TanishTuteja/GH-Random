"""Release safety checks without tokens, GitHub writes, Docker, or a database."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlencode

import pytest

SOURCE = "a" * 40
TAG_OBJECT = "b" * 40
DIGEST = "sha256:" + "c" * 64
REPOSITORY = "Owner/Example"


@pytest.fixture
def release_tools() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / ".github/scripts/release.py"
    spec = importlib.util.spec_from_file_location("release_tools", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def responses() -> dict[str, Any]:
    query = urlencode({"head_sha": SOURCE, "event": "push", "branch": "main", "per_page": 100})
    return {
        "git/ref/tags/v0.1.0": {"object": {"type": "tag", "sha": TAG_OBJECT}},
        f"git/tags/{TAG_OBJECT}": {
            "tag": "v0.1.0",
            "verification": {"verified": True},
            "object": {"type": "commit", "sha": SOURCE},
        },
        f"compare/main...{SOURCE}": {"merge_base_commit": {"sha": SOURCE}},
        "releases/1": {"tag_name": "v0.1.0", "draft": False, "prerelease": False},
        "releases/1/assets?per_page=100&page=1": [],
        f"actions/workflows/ci.yaml/runs?{query}": {
            "workflow_runs": [
                {
                    "id": 42,
                    "head_sha": SOURCE,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
        "actions/runs/42/jobs?filter=latest&per_page=100": {
            "total_count": 3,
            "jobs": [
                {"name": name, "conclusion": "success"}
                for name in ("Python checks", "PostgreSQL tests", "Production containers")
            ],
        },
    }


def verify(module: ModuleType, responses: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return module.verify_release(
        responses.__getitem__,
        **{
            "tag": "v0.1.0",
            "sha": SOURCE,
            "repository": REPOSITORY,
            "version": "0.1.0",
            "release_id": 1,
            **overrides,
        },
    )


def test_accepts_verified_tag_and_exact_main_ci(
    release_tools: ModuleType, responses: dict[str, Any]
) -> None:
    metadata = verify(release_tools, responses)
    assert metadata["commit"] == SOURCE
    assert metadata["tag_object"] == TAG_OBJECT
    assert metadata["ci_run_id"] == 42
    assert metadata["image"] == "ghcr.io/owner/example"
    assert metadata["platform"] == "linux/amd64"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tag": "v0.1.0-rc.1"},
        {"tag": "v01.1.0"},
        {"tag": "v0.1.0\nimage=attacker"},
        {"version": "0.2.0"},
        {"sha": "not-a-commit"},
        {"repository": "Owner/Example\nimage=attacker"},
    ],
)
def test_rejects_invalid_identity_before_any_api_call(
    release_tools: ModuleType, overrides: dict[str, str]
) -> None:
    with pytest.raises(ValueError):
        verify(release_tools, {}, **overrides)


@pytest.mark.parametrize(
    ("path", "key", "value"),
    [
        ("git/ref/tags/v0.1.0", "object", {"type": "commit", "sha": SOURCE}),
        (f"git/tags/{TAG_OBJECT}", "verification", {"verified": False}),
        (f"git/tags/{TAG_OBJECT}", "tag", "v0.2.0"),
        (f"git/tags/{TAG_OBJECT}", "object", {"type": "commit", "sha": "d" * 40}),
        (f"git/tags/{TAG_OBJECT}", "object", {"type": "tag", "sha": SOURCE}),
        (f"compare/main...{SOURCE}", "merge_base_commit", {"sha": "d" * 40}),
        ("releases/1", "draft", True),
        ("releases/1", "prerelease", True),
        ("releases/1", "tag_name", "v0.2.0"),
    ],
)
def test_rejects_untrusted_or_changed_release_source(
    release_tools: ModuleType, responses: dict[str, Any], path: str, key: str, value: Any
) -> None:
    responses[path][key] = value
    with pytest.raises(ValueError):
        verify(release_tools, responses)


@pytest.mark.parametrize(
    "name", ["release.json", "SHA256SUMS", "image-digest.txt", "gh-random-v0.1.0-deployment.tar.gz"]
)
def test_never_overwrites_completed_release_assets(
    release_tools: ModuleType, responses: dict[str, Any], name: str
) -> None:
    responses["releases/1/assets?per_page=100&page=1"] = [{"name": name}]
    with pytest.raises(ValueError, match="already has deployment assets"):
        verify(release_tools, responses)


def test_checks_all_asset_pages(release_tools: ModuleType, responses: dict[str, Any]) -> None:
    responses["releases/1/assets?per_page=100&page=1"] = [{"name": "custom.txt"}] * 100
    responses["releases/1/assets?per_page=100&page=2"] = [{"name": "release.json"}]
    with pytest.raises(ValueError, match="already has deployment assets"):
        verify(release_tools, responses)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("conclusion", "cancelled"),
        ("head_sha", "d" * 40),
        ("head_branch", "some-feature"),
        ("event", "pull_request"),
    ],
)
def test_requires_successful_main_push_ci(
    release_tools: ModuleType, responses: dict[str, Any], key: str, value: str
) -> None:
    ci = next(v for v in responses.values() if isinstance(v, dict) and "workflow_runs" in v)
    ci["workflow_runs"][0][key] = value
    with pytest.raises(ValueError):
        verify(release_tools, responses)


def test_does_not_accept_older_success_when_latest_ci_failed(
    release_tools: ModuleType, responses: dict[str, Any]
) -> None:
    ci = next(v for v in responses.values() if isinstance(v, dict) and "workflow_runs" in v)
    ci["workflow_runs"].append({**ci["workflow_runs"][0], "id": 43, "conclusion": "failure"})
    with pytest.raises(ValueError, match="Latest main CI"):
        verify(release_tools, responses)
    ci["workflow_runs"] = []
    with pytest.raises(ValueError, match="No main-push CI"):
        verify(release_tools, responses)


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "failure"])
def test_all_three_named_jobs_must_pass(
    release_tools: ModuleType, responses: dict[str, Any], conclusion: str
) -> None:
    responses["actions/runs/42/jobs?filter=latest&per_page=100"]["jobs"][0]["conclusion"] = (
        conclusion
    )
    with pytest.raises(ValueError, match="Required CI jobs"):
        verify(release_tools, responses)


@pytest.fixture
def bundle_root(release_tools: ModuleType, tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for name in release_tools.DEPLOYMENT_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("example\n", encoding="utf-8")
    (root / "deployment/compose.env.example").write_text(
        "GH_RANDOM_IMAGE=placeholder\nOTHER_SETTING=unchanged\n", encoding="utf-8"
    )
    # These must never appear in the bundle.
    (root / ".env").write_text("secret", encoding="utf-8")
    (root / "deployment/app.env").write_text("secret", encoding="utf-8")
    (root / "deployment/private.pem").write_text("secret", encoding="utf-8")
    return root


def test_bundle_is_allowlisted_digest_pinned_and_checksummed(
    release_tools: ModuleType, responses: dict[str, Any], bundle_root: Path, tmp_path: Path
) -> None:
    output = tmp_path / "release"
    release_tools.bundle_release(bundle_root, output, verify(release_tools, responses), DIGEST)
    image = f"ghcr.io/owner/example@{DIGEST}"
    assert (output / "image-digest.txt").read_text().strip() == image
    metadata = json.loads((output / "release.json").read_text())
    assert metadata["image"] == image
    assert metadata["commit"] == SOURCE
    assert metadata["deployment_format"] == 1
    assert set(metadata["deployment_files"]) == set(release_tools.DEPLOYMENT_FILES)
    with tarfile.open(output / "gh-random-v0.1.0-deployment.tar.gz") as archive:
        assert set(archive.getnames()) == {*release_tools.DEPLOYMENT_FILES, "release.json"}
        template = archive.extractfile("deployment/compose.env.example")
        assert template is not None
        assert template.read().decode() == f"GH_RANDOM_IMAGE={image}\nOTHER_SETTING=unchanged\n"
        bundled_metadata = archive.extractfile("release.json")
        assert bundled_metadata is not None
        assert json.load(bundled_metadata) == metadata
        for name, digest in metadata["deployment_files"].items():
            member = archive.extractfile(name)
            assert member is not None
            assert hashlib.sha256(member.read()).hexdigest() == digest
    checksums = (output / "SHA256SUMS").read_text().splitlines()
    assert len(checksums) == 3
    for line in checksums:
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()


@pytest.mark.parametrize("digest", ["latest", "sha256:invalid", "sha256:" + "c" * 63])
def test_bundle_rejects_non_digest_images(
    release_tools: ModuleType,
    responses: dict[str, Any],
    bundle_root: Path,
    tmp_path: Path,
    digest: str,
) -> None:
    with pytest.raises(ValueError, match="Invalid image digest"):
        release_tools.bundle_release(
            bundle_root, tmp_path / "out", verify(release_tools, responses), digest
        )
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("template", ["NO_IMAGE=value\n", "GH_RANDOM_IMAGE=a\nGH_RANDOM_IMAGE=b\n"])
def test_bundle_rejects_ambiguous_image_configuration(
    release_tools: ModuleType,
    responses: dict[str, Any],
    bundle_root: Path,
    tmp_path: Path,
    template: str,
) -> None:
    (bundle_root / "deployment/compose.env.example").write_text(template, encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one GH_RANDOM_IMAGE"):
        release_tools.bundle_release(
            bundle_root, tmp_path / "out", verify(release_tools, responses), DIGEST
        )


def test_bundle_requires_every_deployment_file(
    release_tools: ModuleType, responses: dict[str, Any], bundle_root: Path, tmp_path: Path
) -> None:
    (bundle_root / "Caddyfile").unlink()
    with pytest.raises(ValueError, match="Missing/unsafe release file"):
        release_tools.bundle_release(
            bundle_root, tmp_path / "out", verify(release_tools, responses), DIGEST
        )


def test_verify_command_writes_only_validated_workflow_outputs(
    release_tools: ModuleType,
    responses: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"release": {"id": 1, "tag_name": "v0.1.0"}}))
    output_path = tmp_path / "github-output"
    metadata_path = tmp_path / "metadata.json"
    monkeypatch.setattr(release_tools, "ROOT", tmp_path)
    monkeypatch.setattr(release_tools, "github_api", lambda repository: responses.__getitem__)
    monkeypatch.setattr(release_tools.subprocess, "check_output", lambda *args, **kwargs: SOURCE)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setattr("sys.argv", ["release.py", "verify", "--metadata", str(metadata_path)])
    release_tools.main()
    assert json.loads(metadata_path.read_text())["commit"] == SOURCE
    assert output_path.read_text().splitlines() == [
        f"commit={SOURCE}",
        "tag=v0.1.0",
        "image=ghcr.io/owner/example",
    ]


def test_bundle_does_not_overwrite_an_existing_output_directory(
    release_tools: ModuleType, responses: dict[str, Any], bundle_root: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(FileExistsError):
        release_tools.bundle_release(bundle_root, output, verify(release_tools, responses), DIGEST)
