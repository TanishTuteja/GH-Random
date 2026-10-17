"""Offline safety checks; native container CI also rehearses real dump/restore."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO

import pytest


class FakeDatabase:
    def __init__(self) -> None:
        self.source = "gh_random"
        self.version = 170009
        self.dumps_fail = False
        self.inspect_fails = False
        self.inspect_calls = 0
        self.restored: list[str] = []
        self.revision: str | None = "baseline"

    def info(self) -> dict[str, object]:
        return {"database": self.source, "server_version_num": self.version}

    def dump(self, destination: BinaryIO) -> None:
        destination.write(b"PGDMPfake-test-archive")
        if self.dumps_fail:
            raise RuntimeError("dump interrupted")

    def inspect(self, archive: Path) -> None:
        self.inspect_calls += 1
        if self.inspect_fails:
            raise RuntimeError("invalid archive")

    def restore(self, archive: Path, target: str) -> None:
        if target in self.restored:
            raise FileExistsError("database already exists")
        self.restored.append(target)

    def summary(self, target: str) -> dict[str, object]:
        return {"database": target, "alembic_revision": self.revision, "repositories": 1}


@pytest.fixture
def tools() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "deployment/database_backup.py"
    spec = importlib.util.spec_from_file_location("database_backup", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def completed(tools: ModuleType, database: FakeDatabase, tmp_path: Path) -> Path:
    return tools.backup(database, tmp_path / "private-backups")


def test_backup_is_private_checksummed_and_atomically_completed(
    tools: ModuleType, database: FakeDatabase, completed: Path
) -> None:
    archive, manifest = tools.verify_backup(completed)
    assert manifest["source_database"] == "gh_random"
    assert manifest["sha256"] == tools.checksum(archive)
    assert manifest["size_bytes"] == len(b"PGDMPfake-test-archive")
    assert database.inspect_calls == 1
    assert len(list(completed.parent.iterdir())) == 1
    if os.name == "posix":
        assert stat.S_IMODE(completed.stat().st_mode) == 0o700
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE((completed / "manifest.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("failure", ["dumps_fail", "inspect_fails"])
def test_failed_backup_is_never_marked_complete(
    tools: ModuleType, database: FakeDatabase, tmp_path: Path, failure: str
) -> None:
    setattr(database, failure, True)
    root = tmp_path / "backups"
    with pytest.raises(RuntimeError):
        tools.backup(database, root)
    partials = list(root.iterdir())
    assert len(partials) == 1
    assert partials[0].name.startswith(".partial-")
    with pytest.raises(ValueError, match="Not a completed"):
        tools.verify_backup(partials[0])


@pytest.mark.parametrize("content", [b"", b"not a custom archive"])
def test_rejects_empty_or_non_custom_dump(
    tools: ModuleType,
    database: FakeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    monkeypatch.setattr(database, "dump", lambda destination: destination.write(content))
    with pytest.raises(ValueError):
        tools.backup(database, tmp_path / "backups")


@pytest.mark.parametrize("content", [b"short", b"PGDMPfake-test-archivX"])
def test_corruption_is_rejected_before_restore(
    tools: ModuleType, database: FakeDatabase, completed: Path, content: bytes
) -> None:
    (completed / "database.dump").write_bytes(content)
    with pytest.raises(ValueError, match="mismatch"):
        tools.restore(database, completed, "gh_random_restore_test")
    assert database.restored == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "different-format"),
        ("server_version_num", "17"),
        ("server_version_num", True),
        ("sha256", "not-a-hash"),
        ("sha256", None),
        ("size_bytes", "20"),
    ],
)
def test_invalid_manifest_is_rejected(
    tools: ModuleType, completed: Path, field: str, value: object
) -> None:
    path = completed / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        tools.verify_backup(completed)


@pytest.mark.parametrize("manifest", [{}, [], {"format": "something"}])
def test_missing_manifest_fields_are_rejected(
    tools: ModuleType, completed: Path, manifest: object
) -> None:
    (completed / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Invalid backup manifest"):
        tools.verify_backup(completed)


@pytest.mark.parametrize(
    "target", ["gh_random", "postgres", "template1", "gh_random_restore_", "--clean", "x';DROP"]
)
def test_unsafe_restore_names_are_rejected_without_creating_databases(
    tools: ModuleType, database: FakeDatabase, completed: Path, target: str
) -> None:
    with pytest.raises(ValueError, match="Use a new"):
        tools.restore(database, completed, target)
    assert database.restored == []


def test_even_prefixed_live_database_is_not_a_restore_target(
    tools: ModuleType, database: FakeDatabase, completed: Path
) -> None:
    database.source = "gh_random_restore_live"
    with pytest.raises(ValueError, match="live database"):
        tools.restore(database, completed, database.source)
    assert database.restored == []


def test_restore_is_new_database_only_and_reports_schema(
    tools: ModuleType, database: FakeDatabase, completed: Path
) -> None:
    result = tools.restore(database, completed, "gh_random_restore_test")
    assert result["database"] == "gh_random_restore_test"
    assert result["alembic_revision"] == "baseline"
    with pytest.raises(FileExistsError):
        tools.restore(database, completed, "gh_random_restore_test")
    assert database.restored == ["gh_random_restore_test"]


def test_rejects_cross_major_restores(
    tools: ModuleType, database: FakeDatabase, completed: Path
) -> None:
    database.version = 180001
    with pytest.raises(ValueError, match="same PostgreSQL major"):
        tools.restore(database, completed, "gh_random_restore_test")
    assert database.restored == []


def test_missing_schema_revision_does_not_certify_a_restore(
    tools: ModuleType, database: FakeDatabase, completed: Path
) -> None:
    database.revision = None
    with pytest.raises(ValueError, match="no Alembic revision"):
        tools.restore(database, completed, "gh_random_restore_test")
    # Never delete a restore database automatically, even after verification fails.
    assert database.restored == ["gh_random_restore_test"]


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX permission checks; Windows requires private ACLs"
)
def test_refuses_shared_backup_paths(
    tools: ModuleType, database: FakeDatabase, tmp_path: Path
) -> None:
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        tools.backup(database, directory)


@pytest.mark.skipif(os.name != "posix", reason="Creating symlinks requires privileges on Windows")
def test_refuses_symlinked_backup_files(tools: ModuleType, completed: Path) -> None:
    archive = completed / "database.dump"
    original = completed / "original.dump"
    archive.rename(original)
    archive.symlink_to(original)
    with pytest.raises(ValueError, match="symlink"):
        tools.verify_backup(completed)


def test_database_commands_use_container_credentials_and_safe_positional_arguments(
    tools: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"a" * 64 if len(calls) == 1 else b"")

    monkeypatch.setattr(tools.subprocess, "run", run)
    instance = tools.Database(["docker", "compose", "--env-file", "explicit.env"])
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"fake")
    instance.restore(archive, "gh_random_restore_test")
    command = calls[-1][0]
    assert command[:4] == ["docker", "exec", "-i", "a" * 64]
    assert command[-1] == "gh_random_restore_test"
    assert '--dbname="$1"' in command[-3]
    assert "--single-transaction" in command[-3]
    assert "--no-owner" in command[-3]
    assert "--clean" not in " ".join(command)
    assert "$POSTGRES_PASSWORD" in command[-3]  # Only expanded inside the container.
    assert calls[-1][1]["stderr"] == subprocess.PIPE


@pytest.mark.parametrize("output", [b"", b"a" * 64 + b"\n" + b"b" * 64, b"not-a-container"])
def test_requires_exactly_one_running_database(
    tools: ModuleType, monkeypatch: pytest.MonkeyPatch, output: bytes
) -> None:
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, output),
    )
    with pytest.raises(ValueError, match="exactly one"):
        tools.Database(["docker", "compose"])


@pytest.mark.parametrize("returncode", [0, 1])
def test_dump_errors_and_warnings_are_not_certified_or_logged(
    tools: ModuleType, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    calls = 0

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, b"a" * 64)
        return subprocess.CompletedProcess(command, returncode, b"", b"private SQL/data diagnostic")

    monkeypatch.setattr(tools.subprocess, "run", run)
    instance = tools.Database(["docker", "compose"])
    with pytest.raises(RuntimeError) as error:
        instance.dump(io.BytesIO())
    assert "private SQL/data" not in str(error.value)
