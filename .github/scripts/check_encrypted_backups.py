"""Rehearse real encrypted PostgreSQL recovery without any cloud account."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment"))

from backup_archive import seal, unseal, verify  # noqa: E402
from database_backup import Database, private_file, restore  # noqa: E402
from install_age import install  # noqa: E402


def check(compose: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="gh-random-encryption-ci-") as temporary:
        root = Path(temporary)
        age = (
            Path(os.environ["GH_RANDOM_TEST_AGE"])
            if os.environ.get("GH_RANDOM_TEST_AGE")
            else install(root / "tools")
        )
        keygen = age.with_name("age-keygen.exe" if os.name == "nt" else "age-keygen")
        identity = root / "identity.txt"
        subprocess.run(
            [str(keygen), "--output", str(identity)], capture_output=True, check=True, timeout=10
        )
        identity.chmod(0o600)
        key = subprocess.run(
            [str(keygen), "-y", str(identity)], capture_output=True, check=True, timeout=10
        ).stdout
        recipient_file = root / "recipients.txt"
        with private_file(recipient_file) as output:
            output.write(key)
        database = Database(compose)
        encrypted = seal(database, root / "encrypted", recipient_file, age)
        ciphertext, _ = verify(encrypted)
        recovered = unseal(ciphertext, identity, root / "recovered", age)
        target = "gh_random_restore_age_" + uuid.uuid4().hex[:12]
        summary = restore(database, recovered, target)
        assert summary["repositories"] == summary["active_memberships"] == 1
        program = """
import os
from sqlalchemy.engine import make_url
os.environ['DATABASE_URL'] = make_url(os.environ['DATABASE_URL']).set(
    database=os.environ['GH_RANDOM_RESTORE_DATABASE']).render_as_string(hide_password=False)
from app.core.settings import get_settings
from app.main import app
from fastapi.testclient import TestClient
headers = {'Host': get_settings().app_allowed_hosts[0]}
with TestClient(app) as client:
    response = client.get('/api/random-repository', headers=headers)
    assert response.status_code == 200 and response.json()['github_id'] == 1
    assert 'ci/smoke-test' in client.get('/?discover=true', headers=headers).text
"""
        subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "--env",
                f"GH_RANDOM_RESTORE_DATABASE={target}",
                "web",
                "python",
                "-",
            ],
            input=program,
            text=True,
            check=True,
            timeout=30,
        )
        assert not list((root / "encrypted").glob(".work-*"))
        print(
            "Real age encryption, new-database restore and restored page/API discovery passed.",
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compose", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.compose[1:] if args.compose[:1] == ["--"] else args.compose
    assert command[:2] == ["docker", "compose"]
    check(command)
