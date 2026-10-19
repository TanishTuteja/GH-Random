"""Real age round-trips, defensive recovery, off-site contracts and safe retention."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def archives(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "deployment"))
    spec = importlib.util.spec_from_file_location(
        "backup_archive_tools", ROOT / "deployment/backup_archive.py"
    )
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture(scope="session")
def age() -> Path:
    path = Path(
        os.environ.get(
            "GH_RANDOM_TEST_AGE",
            str(ROOT / "build/age-tools" / ("age.exe" if os.name == "nt" else "age")),
        )
    )
    assert path.is_file(), "Run deployment/install_age.py --destination build/age-tools first"
    return path.resolve()


@pytest.fixture
def keys(age: Path, tmp_path: Path):
    identity = tmp_path / "identity.txt"
    keygen = age.with_name("age-keygen.exe" if os.name == "nt" else "age-keygen")
    subprocess.run(
        [str(keygen), "--output", str(identity)], capture_output=True, check=True, timeout=10
    )
    identity.chmod(0o600)
    public = subprocess.run(
        [str(keygen), "-y", str(identity)], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(public + "\n", encoding="utf-8")
    recipients.chmod(0o600)
    return identity, recipients


class FakeDatabase:
    def info(self):
        return {"database": "test_catalog", "server_version_num": 170011}

    def dump(self, destination):
        destination.write(b"PGDMP-private-catalog-fixture")

    def inspect(self, path):
        assert path.read_bytes().startswith(b"PGDMP")


@pytest.fixture
def sealed(archives, age, keys, tmp_path):
    identity, recipients = keys
    directory = tmp_path / "encrypted"
    result = archives.seal(FakeDatabase(), directory, recipients, age)
    return result


def test_real_encryption_and_offline_recovery(archives, age, keys, sealed, tmp_path):
    ciphertext, metadata = archives.verify(sealed)
    assert b"private-catalog-fixture" not in ciphertext.read_bytes()
    assert metadata["recipients_sha256"]
    restored = archives.unseal(ciphertext, keys[0], tmp_path / "recovered", age)
    assert (restored / "database.dump").read_bytes() == b"PGDMP-private-catalog-fixture"
    assert not list(sealed.parent.glob(".work-*"))
    assert not list((tmp_path / "recovered").glob(".work-*"))
    with pytest.raises(ValueError, match="overwrite"):
        archives.unseal(ciphertext, keys[0], tmp_path / "recovered", age)


def test_ciphertext_tampering_and_wrong_key_are_rejected(archives, age, keys, sealed, tmp_path):
    archive, _ = archives.verify(sealed)
    original = archive.read_bytes()
    # Ciphertext is random: replacing with a fixed byte can leave it unchanged.
    archive.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(ValueError, match="checksum"):
        archives.verify(sealed)
    with pytest.raises(ValueError, match="decryption"):
        archives.unseal(archive, keys[0], tmp_path / "recovered", age)
    assert not list((tmp_path / "recovered").iterdir())


@pytest.mark.parametrize(
    "member_name,symlink", [("../../escaped", False), ("safe/database.dump", True)]
)
def test_authenticated_but_unsafe_tar_is_rejected(
    archives, age, keys, tmp_path, member_name, symlink
):
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode="w") as tar:
        for name in (member_name, "safe/manifest.json"):
            member = tarfile.TarInfo(name)
            if symlink:
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                tar.addfile(member)
            else:
                member.size = 1
                tar.addfile(member, io.BytesIO(b"x"))
    archive = tmp_path / "unsafe.age"
    with archives.private_file(archive) as destination:
        subprocess.run(
            [str(age), "-R", str(keys[1])],
            input=package.getvalue(),
            stdout=destination,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
        )
    with pytest.raises(ValueError):
        archives.unseal(archive, keys[0], tmp_path / "recovered", age)
    assert not (tmp_path / "escaped").exists()


def test_private_identity_is_not_accepted_as_public_recipient(archives, keys):
    with pytest.raises(ValueError, match="public recipients"):
        archives.recipients(keys[0])


def test_wrong_recovery_identity_is_rejected(archives, age, sealed, tmp_path):
    foreign = tmp_path / "foreign-identity.txt"
    keygen = age.with_name("age-keygen.exe" if os.name == "nt" else "age-keygen")
    subprocess.run(
        [str(keygen), "--output", str(foreign)], capture_output=True, check=True, timeout=10
    )
    foreign.chmod(0o600)
    with pytest.raises(ValueError, match="decryption"):
        archives.unseal(sealed / "database.tar.age", foreign, tmp_path / "recovered", age)


def test_upload_requires_new_object_and_verified_readback(archives, sealed, age, tmp_path):
    sent = []
    stored = []
    config = {
        "oci_binary": str(age),
        "timeout_seconds": 10,
        "max_archive_bytes": 1024**2,
        "offsite": {"namespace": "test", "bucket": "test", "prefix": "gh-random/"},
    }

    def runner(command, **kwargs):
        sent.append(command)
        assert kwargs["timeout"] == 10
        file = Path(command[command.index("--file") + 1])
        if "put" in command:
            stored.append(file.read_bytes())
            return subprocess.CompletedProcess(command, 0, json.dumps({"etag": "verified-version"}))
        file.write_bytes(stored[0])
        return subprocess.CompletedProcess(command, 0, "")

    receipt = archives.upload(sealed, config, runner=runner)
    assert receipt["sha256"] == archives.verify(sealed)[1]["sha256"]
    assert "--no-overwrite" in sent[0] and "--verify-checksum" in sent[0]
    assert "--if-match" in sent[1] and "instance_principal" in sent[0]
    assert (sealed / "offsite.json").is_file()


@pytest.mark.parametrize("fault", ["no-etag", "bad-download", "timeout"])
def test_failed_upload_never_certifies_offsite_copy(archives, sealed, age, fault):
    config = {
        "oci_binary": str(age),
        "timeout_seconds": 10,
        "max_archive_bytes": 1024**2,
        "offsite": {"namespace": "test", "bucket": "test", "prefix": "gh-random/"},
    }

    def runner(command, **kwargs):
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, 10)
        if "put" in command:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({} if fault == "no-etag" else {"etag": "v1"})
            )
        Path(command[command.index("--file") + 1]).write_bytes(b"corrupt-readback")
        return subprocess.CompletedProcess(command, 0, "")

    with pytest.raises((ValueError, subprocess.TimeoutExpired)):
        archives.upload(sealed, config, runner=runner)
    assert not (sealed / "offsite.json").exists()


def test_retention_keeps_two_verified_copies_and_unverified_archives(archives, age, keys, tmp_path):
    root = tmp_path / "encrypted"
    created = []
    for index in range(4):
        path = archives.seal(FakeDatabase(), root, keys[1], age)
        meta = json.loads((path / "manifest.json").read_text())
        meta["completed_at"] = (datetime.now(timezone.utc) - timedelta(days=10 + index)).isoformat()
        (path / "manifest.json").write_text(json.dumps(meta))
        if index != 3:
            with archives.private_file(path / "offsite.json") as output:
                output.write(
                    json.dumps(
                        {
                            "format": 1,
                            "sha256": meta["sha256"],
                            "verified_at": meta["completed_at"],
                            "etag": "version",
                            "object_name": path.name + ".tar.age",
                        }
                    ).encode()
                )
        created.append(path)
    removed = archives.prune(root)
    assert removed == [created[2].name]
    assert created[0].exists() and created[1].exists() and created[3].exists()
    unrelated = root / "operator-files"
    unrelated.mkdir()
    assert archives.prune(root) == [] and unrelated.exists()
    with pytest.raises(ValueError):
        archives.prune(root, keep=1)


def test_retention_refuses_corrupt_or_unknown_managed_files(archives, sealed):
    (sealed / "manifest.json").write_text("corrupt")
    with pytest.raises(ValueError):
        archives.prune(sealed.parent)
