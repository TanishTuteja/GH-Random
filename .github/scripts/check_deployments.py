"""Rehearse the actual deploy CLI with synthetic releases and disposable data.

Requires Docker's containerd image store for locally built digest references.
Nothing is pushed to a registry; no GitHub App or production secrets are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]


def rehearse(base_image: str) -> None:
    project = f"gh-random-deploy-ci-{uuid.uuid4().hex[:12]}"
    repository = f"ci/{project}"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GH_RANDOM_", "COMPOSE_"))
        and key not in {"POSTGRES_IMAGE", "CADDY_IMAGE"}
    }

    def run(command, *, capture=False, check=True, input_text=None):
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            input=input_text,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            timeout=900,
            check=check,
        )

    images = []
    compose = []
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        workspace = Path(temporary)
        workspace.chmod(0o700)
        config = workspace / "config"
        config.mkdir(mode=0o700)

        def private(name, contents):
            path = config / name
            path.write_text(contents, encoding="utf-8")
            path.chmod(0o600)
            return path

        app_env = private(
            "app.env",
            "DATABASE_URL=postgresql+psycopg://deploy_ci:ci-only-password@database:5432/deploy_ci\n",
        )
        postgres_env = private(
            "postgres.env",
            "POSTGRES_DB=deploy_ci\nPOSTGRES_USER=deploy_ci\nPOSTGRES_PASSWORD=ci-only-password\n",
        )
        crawler_env = private(
            "crawler.env",
            app_env.read_text()
            + "GITHUB_APP_CLIENT_ID=ci-fixture\nGITHUB_APP_INSTALLATION_ID=1\n"
            + "GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app-private-key.pem\n",
        )
        caddy_env = private(
            "caddy.env", "GH_RANDOM_DOMAIN=http://127.0.0.1\nCADDY_EMAIL=ci@example.com\n"
        )
        key = private("key.pem", "Not a private key. Never authenticate in this rehearsal.\n")
        env_file = private(
            "compose.env",
            "GH_RANDOM_IMAGE=not-yet-deployed\n"
            + f"GH_RANDOM_POSTGRES_ENV_FILE={postgres_env.as_posix()}\n"
            + f"GH_RANDOM_APP_ENV_FILE={app_env.as_posix()}\n"
            + f"GH_RANDOM_CRAWLER_ENV_FILE={crawler_env.as_posix()}\n"
            + f"GH_RANDOM_CADDY_ENV_FILE={caddy_env.as_posix()}\n"
            + f"GITHUB_APP_PRIVATE_KEY_HOST_PATH={key.as_posix()}\n",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        releases = []
        try:
            for index in range(3):
                tag = f"v0.1.{index}"
                commit = str(index + 1) * 40
                name = f"ghcr.io/{repository}:{tag}"
                build = workspace / f"build-{index}"
                build.mkdir()
                dockerfile = "ARG BASE=python:3.12-slim\nFROM ${BASE}\n"
                if index == 2:
                    dockerfile += "COPY bad_migration.py /app/alembic/versions/bad_migration.py\n"
                    (build / "bad_migration.py").write_text(
                        "from alembic import op\n"
                        'revision = "ci_bad_migration"\n'
                        'down_revision = "20260930_01"\n'
                        "branch_labels = depends_on = None\n"
                        "def upgrade():\n"
                        '    op.execute("CREATE TABLE ci_must_rollback (id integer)")\n'
                        '    raise RuntimeError("deliberate CI migration failure")\n'
                        "def downgrade():\n"
                        '    raise RuntimeError("never downgrade in CI")\n',
                        encoding="utf-8",
                    )
                (build / "Dockerfile").write_text(dockerfile, encoding="utf-8")
                images.append(name)
                run(
                    [
                        "docker",
                        "build",
                        "--build-arg",
                        f"BASE={base_image}",
                        "--label",
                        f"org.opencontainers.image.source=https://github.com/{repository}",
                        "--label",
                        f"org.opencontainers.image.revision={commit}",
                        "--label",
                        f"org.opencontainers.image.version={tag}",
                        "--tag",
                        name,
                        str(build),
                    ]
                )
                info = json.loads(run(["docker", "image", "inspect", name], capture=True).stdout)[0]
                digest_ref = next(
                    ref for ref in info["RepoDigests"] if ref.startswith(f"ghcr.io/{repository}@")
                )
                release = workspace / tag
                (release / "deployment").mkdir(parents=True)
                for filename in (
                    "compose.production.yaml",
                    "Caddyfile",
                    "deployment/deploy.py",
                    "deployment/database_backup.py",
                ):
                    shutil.copyfile(ROOT / filename, release / filename)
                composition = (release / "compose.production.yaml").read_text()
                composition = composition.replace(
                    '      - "80:80"\n      - "443:443"\n      - "443:443/udp"',
                    f'      - "127.0.0.1:{port}:80"',
                )
                assert f"127.0.0.1:{port}:80" in composition
                (release / "compose.production.yaml").write_text(composition, encoding="utf-8")
                metadata = {
                    "deployment_format": 1,
                    "tag": tag,
                    "commit": commit,
                    "repository": repository,
                    "image": digest_ref,
                    "digest": digest_ref.split("@", 1)[1],
                    "platform": f"linux/{info['Architecture']}",
                    "deployment_files": {
                        path.relative_to(release).as_posix(): hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest()
                        for path in release.rglob("*")
                        if path.is_file()
                    },
                }
                (release / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
                releases.append((release, metadata))

            state = workspace / "state"
            common = [
                "--env-file",
                str(env_file),
                "--state-dir",
                str(state),
                "--backup-dir",
                str(workspace / "backups"),
                "--url",
                url,
                "--allow-loopback-http",
                "--project-name",
                project,
                "--timeout-seconds",
                "120",
                "--smoke-timeout-seconds",
                "10",
            ]

            def deploy(index, *, expected=True, extra=()):
                release = releases[index][0]
                command = [
                    sys.executable,
                    str(release / "deployment/deploy.py"),
                    "apply",
                    "--release-dir",
                    str(release),
                    *common,
                    *extra,
                ]
                result = run(command, capture=True, check=False)
                print(result.stdout, flush=True)
                assert (result.returncode == 0) == expected, result.stdout

            compose = [
                "docker",
                "compose",
                "--env-file",
                str(env_file),
                "--file",
                str(releases[0][0] / "compose.production.yaml"),
                "--project-name",
                project,
            ]
            print(f"Rehearsing bootstrap in {project}...", flush=True)
            deploy(0, extra=["--bootstrap"])
            # Reuse the real app fixture through the packaged web, not raw SQL-only assertions.
            fixture = (ROOT / ".github/scripts/check_containers.py").read_text()
            fixture = fixture.replace(
                'headers={"Host": "localhost"}', 'headers={"Host": "127.0.0.1"}'
            )
            run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "--env",
                    "GH_RANDOM_CONTAINER_SMOKE=1",
                    "web",
                    "python",
                    "-",
                    "--inside",
                ],
                input_text=fixture,
            )
            print("Checking deployment/crawler migration exclusion and EOF cleanup...", flush=True)
            coordinator_name = project + "-lease"
            agent_command = [
                *compose,
                "--profile",
                "migrate",
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "--name",
                coordinator_name,
                "-T",
                "migrate",
                "python",
                "-m",
                "app.deployment",
                "--timeout-seconds",
                "60",
            ]
            with (workspace / "coordinator.log").open("w", encoding="utf-8") as diagnostics:
                agent = subprocess.Popen(
                    agent_command,
                    cwd=ROOT,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=diagnostics,
                    text=True,
                    encoding="utf-8",
                )
                try:
                    assert agent.stdin is not None and agent.stdout is not None
                    assert json.loads(agent.stdout.readline())["protocol"] == 1
                    agent.stdin.write('{"command":"migrate"}\n')
                    agent.stdin.flush()
                    assert json.loads(agent.stdout.readline())["revision"] == "20260930_01"
                    contender = run(
                        [
                            *compose,
                            "--profile",
                            "migrate",
                            "run",
                            "--rm",
                            "--no-deps",
                            "--pull",
                            "never",
                            "-T",
                            "migrate",
                            "alembic",
                            "check",
                        ],
                        capture=True,
                        check=False,
                    )
                    assert contender.returncode != 0 and "CatalogOperationBusy" in contender.stdout
                    deploy(1, expected=False)
                    # Closing the controller pipe, without 'finish', must release the DB lease.
                    agent.stdin.close()
                    assert agent.wait(timeout=15) == 0
                finally:
                    run(["docker", "rm", "--force", coordinator_name], check=False)
                    if agent.poll() is None:
                        agent.kill()
                        agent.wait(timeout=10)
                    if agent.stdout is not None:
                        agent.stdout.close()
            print("Checking idle controller expiry...", flush=True)
            idle = subprocess.Popen(
                [*agent_command[:-1], "1"],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
            try:
                assert idle.stdout is not None and idle.stdin is not None
                assert json.loads(idle.stdout.readline())["protocol"] == 1
                assert json.loads(idle.stdout.readline())["error"] == "TimeoutError"
                assert idle.wait(timeout=15) != 0
            finally:
                run(["docker", "rm", "--force", coordinator_name], check=False)
                if idle.poll() is None:
                    idle.kill()
                    idle.wait(timeout=10)
                if idle.stdin is not None:
                    idle.stdin.close()
                if idle.stdout is not None:
                    idle.stdout.close()
            print("Rehearsing successful redeploy and unchanged catalog...", flush=True)
            deploy(1)
            current = json.loads((state / "current.json").read_text())
            assert current["image"] == releases[1][1]["image"]
            assert current["previous"]["image"] == releases[0][1]["image"]
            print("Rehearsing a transactional migration failure...", flush=True)
            deploy(2, expected=False)
            assert json.loads((state / "current.json").read_text()) == current
            sql = "SELECT to_regclass('public.ci_must_rollback') IS NULL"
            result = run(
                [
                    *compose,
                    "exec",
                    "-T",
                    "database",
                    "psql",
                    "-U",
                    "deploy_ci",
                    "-d",
                    "deploy_ci",
                    "-At",
                    "-c",
                    sql,
                ],
                capture=True,
            )
            assert result.stdout.strip() == "t", result.stdout
            print("Rehearsing smoke-test failure without implicit rollback...", flush=True)
            deploy(
                0,
                expected=False,
                extra=["--url", "http://127.0.0.1:1", "--smoke-timeout-seconds", "1"],
            )
            assert json.loads((state / "current.json").read_text()) == current
            print("Rehearsing explicit same-schema previous-release rollback...", flush=True)
            result = run(
                [
                    sys.executable,
                    str(releases[1][0] / "deployment/deploy.py"),
                    "rollback",
                    *common,
                    "--confirm-schema-compatible",
                ],
                capture=True,
            )
            print(result.stdout, flush=True)
            assert (
                json.loads((state / "current.json").read_text())["image"] == releases[0][1]["image"]
            )
            with urlopen(url + "/api/random-repository", timeout=10) as response:
                assert json.load(response)["full_name"] == "ci/smoke-test"
            records = [
                json.loads(p.read_text()) for p in state.glob("*.json") if p.name != "current.json"
            ]
            assert sorted(r["phase"] for r in records if r["status"] == "failed") == [
                "acquire_catalog",
                "migrate",
                "smoke",
            ]
            assert len(list((workspace / "backups").glob("gh-random-*"))) == 5
            print(
                "Deploy bootstrap/redeploy, failed DDL/smoke, rollback, backups "
                "and data preservation passed.",
                flush=True,
            )
        except BaseException:
            # These logs contain only disposable fixture values, never real configuration.
            for log in sorted((workspace / "state").glob("*.log")):
                print(log.name, log.read_text(encoding="utf-8", errors="replace")[-16000:])
            raise
        finally:
            if compose:
                run(
                    [
                        *compose,
                        "--profile",
                        "migrate",
                        "--profile",
                        "crawler",
                        "down",
                        "--volumes",
                        "--remove-orphans",
                    ],
                    check=False,
                )
            for image in reversed(images):
                run(["docker", "image", "rm", image], check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Existing local production image")
    rehearse(parser.parse_args().image)
