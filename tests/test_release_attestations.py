"""Signed release policy contracts; no real GitHub, registry, keys or credentials."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STABLE = b"gh version 2.102.0 (2026-09-30)\nhttps://github.com/cli/cli/releases/tag/v2.102.0\n"
RUNTIME_VARIABLES = (
    "GH_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)


@pytest.fixture
def module(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "release_attestations", ROOT / "deployment/attestations.py"
    )
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    monkeypatch.setattr(result.tempfile, "tempdir", str(tmp_path))
    return result


@pytest.fixture
def metadata():
    return {
        "repository": "Owner/Repo",
        "commit": "a" * 40,
        "tag": "v0.1.0",
        "digest": "sha256:" + "b" * 64,
        "image": "ghcr.io/owner/repo@sha256:" + "b" * 64,
    }


@pytest.fixture
def archive(tmp_path):
    value = tmp_path / "release-deployment.tar.gz"
    value.write_bytes(b"fixture signed artifact subject")
    return value


@pytest.mark.parametrize(
    "text",
    [
        b"gh version 2.96.0 (2026-07-01)\nhttps://github.com/cli/cli/releases/tag/v2.96.0\n",
        b"gh version 2.97.0-rc1 (2026-07-31)\nhttps://github.com/cli/cli/releases/tag/v2.97.0-rc1\n",
        b"gh version 2.97.0+custom (2026-07-31)\nhttps://github.com/cli/cli/releases/tag/v2.97.0\n",
        b"gh version 02.97.0 (2026-07-31)\nhttps://github.com/cli/cli/releases/tag/v02.97.0\n",
        b"gh version 2.97.0 (2026-07-99)\nhttps://github.com/cli/cli/releases/tag/v2.97.0\n",
        b"gh version 2.97.0 (2026-07-31)\nhttps://evil.test/cli/cli/releases/tag/v2.97.0\n",
        b"gh version 2.97.0 (2026-07-31)\nhttps://github.com/cli/cli/releases/tag/v2.96.0\n",
        b"gh version 2.97.0 (2026-07-31)",
        b"garbage private provider output",
        b"\xff\xfe",
    ],
)
def test_rejects_vulnerable_unstable_vendor_or_malformed_cli_version(module, text):
    with pytest.raises(module.AttestationError):
        module.version(text)


def test_accepts_minimum_and_newer_official_stable_versions(module):
    assert module.version(
        b"gh version 2.97.0 (2026-07-31)\r\nhttps://github.com/cli/cli/releases/tag/v2.97.0\r\n"
    ) == (2, 97, 0)
    assert module.version(STABLE) == (2, 102, 0)


def test_verifies_both_exact_subjects_same_policy_and_never_puts_token_in_args(
    module, metadata, archive, monkeypatch
):
    calls = []

    def run(arguments, environment, **options):
        calls.append((arguments, environment, options))
        return STABLE if arguments[-1] == "--version" else b"verified"

    monkeypatch.setattr(module, "run", run)
    monkeypatch.setenv("GH_HOST", "untrusted.example")
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("GH_TOKEN", "old-token")
    monkeypatch.setenv("SIGSTORE_ROOT_FILE", "/unsafe/root")
    module.verify(metadata, archive, Path("/opt/gh"), token="fixture-secret-token")
    assert len(calls) == 3
    policy = [
        "--repo",
        metadata["repository"],
        "--signer-workflow",
        "Owner/Repo/.github/workflows/release.yaml",
        "--source-ref",
        "refs/tags/v0.1.0",
        "--source-digest",
        "a" * 40,
        "--signer-digest",
        "a" * 40,
        "--deny-self-hosted-runners",
        "--limit",
        "10",
    ]
    assert calls[1][0] == [
        str(Path("/opt/gh")),
        "attestation",
        "verify",
        str(archive.resolve()),
        *policy,
    ]
    assert calls[2][0] == [
        str(Path("/opt/gh")),
        "attestation",
        "verify",
        "oci://" + metadata["image"],
        *policy,
    ]
    assert calls[0][2] == {"timeout": 10, "limit": 4096}
    assert calls[1][2] == {"timeout": 120, "limit": 1024 * 1024}
    for arguments, environment, _ in calls:
        assert "fixture-secret-token" not in str(arguments)
        assert environment["GH_TOKEN"] == "fixture-secret-token"
        assert environment["GH_HOST"] == "github.com"
        assert environment["GH_PROMPT_DISABLED"] == "1"
        assert "GH_DEBUG" not in environment and "SIGSTORE_ROOT_FILE" not in environment


def test_vulnerable_cli_stops_before_any_attestation_request(
    module, metadata, archive, monkeypatch
):
    calls = []

    def run(arguments, *args, **kwargs):
        calls.append(arguments)
        return b"gh version 2.96.0 (2026-07-01)\nhttps://github.com/cli/cli/releases/tag/v2.96.0\n"

    monkeypatch.setattr(module, "run", run)
    with pytest.raises(module.AttestationError, match="2.97.0"):
        module.verify(metadata, archive, Path("gh"), token="fixture")
    assert len(calls) == 1


def test_cli_uses_fresh_writable_private_runtime_and_cleans_up(
    module, metadata, archive, tmp_path, monkeypatch
):
    inherited = tmp_path / "inherited-cli-state"
    inherited.mkdir()
    marker = inherited / "previous-authentication"
    marker.write_text("unrelated-private-credential")
    for variable in RUNTIME_VARIABLES:
        monkeypatch.setenv(variable, str(inherited))

    roots = []
    calls = []

    def run(arguments, environment, **options):
        directories = [Path(environment[variable]) for variable in RUNTIME_VARIABLES]
        parents = {directory.parent for directory in directories}
        assert len(parents) == 1
        root = parents.pop()
        assert root != inherited and root.parent == tmp_path
        if not calls or len(calls) == 3:
            roots.append(root)
            assert all(not list(directory.iterdir()) for directory in directories)
        assert root == roots[-1]
        for directory in [root, *directories]:
            assert directory.is_dir() and not directory.is_symlink()
            assert directory != inherited
            if os.name == "posix":
                assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
        # Emulate the actual CLI's configuration/cache writes, not just env keys.
        for directory in directories:
            assert not (directory / marker.name).exists()
            (directory / "cache-entry").write_text("verification-cache")
        calls.append(arguments)
        return STABLE if arguments[-1] == "--version" else b"verified"

    monkeypatch.setattr(module, "run", run)
    module.verify(metadata, archive, Path("gh"), token="fixture")
    assert len(calls) == 3 and not roots[0].exists()
    module.verify(metadata, archive, Path("gh"), token="fixture")
    assert len(calls) == 6 and len(roots) == 2 and roots[0] != roots[1]
    assert all(not root.exists() for root in roots)
    assert marker.read_text() == "unrelated-private-credential"
    assert list(inherited.iterdir()) == [marker]


@pytest.mark.parametrize("failure_step", [0, 1, 2], ids=["version", "archive", "image"])
def test_cli_runtime_is_removed_after_each_verification_failure(
    module, metadata, archive, monkeypatch, failure_step
):
    roots = []
    calls = []

    def run(arguments, environment, **options):
        directory = Path(environment["GH_CONFIG_DIR"])
        roots.append(directory.parent)
        (directory / "partial-cache").write_text("private-cache-state")
        calls.append(arguments)
        if len(calls) - 1 == failure_step:
            raise module.AttestationError("GitHub CLI verification failed")
        return STABLE if arguments[-1] == "--version" else b"verified"

    monkeypatch.setattr(module, "run", run)
    with pytest.raises(module.AttestationError, match="verification failed"):
        module.verify(metadata, archive, Path("gh"), token="fixture")
    assert len(calls) == failure_step + 1
    assert len(set(roots)) == 1 and not roots[0].exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("repository", "Owner/Repo\nunsafe"),
        ("commit", "a" * 39),
        ("tag", "v0.1.0-rc.1"),
        ("digest", "latest"),
        ("image", "ghcr.io/other/image@sha256:" + "b" * 64),
    ],
)
def test_invalid_metadata_never_executes_cli(module, metadata, archive, monkeypatch, key, value):
    metadata[key] = value
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: pytest.fail("CLI executed"))
    with pytest.raises(module.AttestationError):
        module.verify(metadata, archive, Path("gh"), token="fixture")


def test_missing_archive_or_token_never_executes_cli(module, metadata, archive, monkeypatch):
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: pytest.fail("CLI executed"))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(module.AttestationError):
        module.verify(metadata, archive, Path("gh"))
    with pytest.raises(module.AttestationError):
        module.verify(metadata, archive.with_name("missing"), Path("gh"), token="fixture")


def test_cli_subject_failure_stops_the_pair(module, metadata, archive, monkeypatch):
    calls = []

    def run(arguments, *args, **kwargs):
        calls.append(arguments)
        if arguments[-1] == "--version":
            return STABLE
        raise module.AttestationError("GitHub CLI verification failed")

    monkeypatch.setattr(module, "run", run)
    with pytest.raises(module.AttestationError):
        module.verify(metadata, archive, Path("gh"), token="fixture")
    assert len(calls) == 2


def test_private_output_runtime_and_error_are_bounded(module, capsys):
    assert (
        module.run([sys.executable, "-c", "print('safe',end='')"], {}, timeout=3, limit=100)
        == b"safe"
    )
    with pytest.raises(module.AttestationError):
        module.run(
            [sys.executable, "-c", "import sys; print('private-secret'); sys.exit(2)"],
            {},
            timeout=3,
            limit=100,
        )
    with pytest.raises(module.AttestationError):
        module.run([sys.executable, "-c", "print('x'*100000)"], {}, timeout=3, limit=100)
    start = time.monotonic()
    with pytest.raises(module.AttestationError):
        module.run([sys.executable, "-c", "import time; time.sleep(30)"], {}, timeout=1, limit=100)
    assert time.monotonic() - start < 5
    assert "private-secret" not in str(capsys.readouterr())


def test_cli_rejects_duplicate_metadata_and_redacts_failures(
    module, metadata, archive, tmp_path, monkeypatch, capsys
):
    path = tmp_path / "release.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["attestations", "--metadata", str(path), "--archive", str(archive)]
    )

    def broken(*args, **kwargs):
        raise RuntimeError("private-provider-token")

    monkeypatch.setattr(module, "verify", broken)
    assert module.main() == 1
    assert "private-provider-token" not in str(capsys.readouterr())
    path.write_text('{"repository":"first","repository":"duplicate"}', encoding="utf-8")
    assert module.main() == 1
    assert "duplicate" not in str(capsys.readouterr()).lower()


def test_workflow_verifies_signatures_after_signing_and_before_asset_upload():
    workflow = yaml.load(
        (ROOT / ".github/workflows/release.yaml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["publish"]["steps"]
    signing = next(
        i
        for i, step in enumerate(steps)
        if step["name"] == "Sign the exact deployment archive provenance"
    )
    verify = next(
        i
        for i, step in enumerate(steps)
        if "python deployment/attestations.py" in step.get("run", "")
    )
    upload = next(i for i, step in enumerate(steps) if "gh release upload" in step.get("run", ""))
    assert signing < verify < upload
    assert "continue-on-error" not in steps[verify]
    assert steps[verify]["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "--metadata dist/release/release.json" in steps[verify]["run"]
    assert "gh-random-$RELEASE_TAG-deployment.tar.gz" in steps[verify]["run"]
