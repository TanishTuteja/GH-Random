"""Exercise production containers in an isolated Compose project, then clean up."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def check_ca_roots(context: ssl.SSLContext) -> None:
    assert not any(
        value == "emSign ECC Root CA - G3"
        for certificate in context.get_ca_certs()
        for rdn in certificate["subject"]
        for key, value in rdn
        if key == "commonName"
    ), "A packaged TLS trust store still contains the compromised emSign root"


def smoke_inside_web() -> None:
    """Run through Caddy from the packaged web container, using disposable data."""

    from app.core.settings import get_settings
    from app.db.models import CatalogGeneration, GenerationRepository, Repository
    from app.db.session import create_engine_from_url, create_session_factory

    assert os.environ.get("GH_RANDOM_CONTAINER_SMOKE") == "1", "Not a CI smoke container"
    assert os.getuid() == 10001, "Web must run as the unprivileged application user"
    for package in ("pytest", "ruff", "pyright", "hatchling", "pip", "ensurepip"):
        assert importlib.util.find_spec(package) is None, f"Unexpected dev/build package: {package}"

    import certifi

    assert sys.version_info >= (3, 12, 16), "Use the patched Python 3.12 security release"
    assert tuple(map(int, certifi.__version__.split("."))) >= (2026, 10, 19)
    for context in (
        ssl.create_default_context(),
        ssl.create_default_context(cafile=certifi.where()),
    ):
        check_ca_roots(context)
    # The venv does not expose global site packages: check the base interpreter
    # separately so an unused vulnerable installer cannot hide behind isolation.
    subprocess.run(
        [
            str(Path(sys.base_prefix) / "bin/python"),
            "-c",
            "import importlib.util; "
            "assert all(importlib.util.find_spec(p) is None for p in ('pip', 'ensurepip'))",
        ],
        check=True,
        timeout=10,
    )

    opener = build_opener(ProxyHandler({}))
    host = os.environ.get("GH_RANDOM_SMOKE_HOST", "localhost")
    assert host in {"localhost", "127.0.0.1"}, "Only disposable loopback sites are tested"

    def request(path: str, expected_status: int = 200) -> tuple[bytes, dict[str, str]]:
        try:
            response = opener.open(
                Request(f"http://caddy{path}", headers={"Host": host}), timeout=5
            )
        except HTTPError as error:
            response = error
        with response:
            assert response.status == expected_status, f"{path}: HTTP {response.status}"
            return response.read(), dict(response.headers)

    deadline = time.monotonic() + 45
    while True:
        try:
            body, headers = request("/")
            break
        except (AssertionError, URLError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)

    assert b"<title>GH Random</title>" in body, "Packaged template was not rendered"
    assert headers.get("X-Content-Type-Options") == "nosniff", "Caddy headers were not applied"

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    # Inspect a framework-generated redirect without adding a public debug route.
    # Caddy must overwrite, not trust, spoofed headers from the HTTP client.
    probe = Request(
        "http://caddy/api/random-repository/?gh_random_proxy_probe=1",
        headers={
            "Host": host,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-For": "203.0.113.99",
        },
    )
    try:
        build_opener(ProxyHandler({}), NoRedirect()).open(probe, timeout=5)
    except HTTPError as error:
        with error:
            assert error.code == 307
            assert error.headers["Location"] == (
                f"http://{host}/api/random-repository?gh_random_proxy_probe=1"
            )
    else:
        raise AssertionError("Expected canonical redirect")
    # This private-peer probe establishes that Uvicorn actually consumes trusted
    # proxy scheme headers; the public Caddy probe above verifies sanitization.
    try:
        build_opener(ProxyHandler({}), NoRedirect()).open(
            Request(
                "http://web:8000/api/random-repository/",
                headers={"Host": host, "X-Forwarded-Proto": "https"},
            ),
            timeout=5,
        )
    except HTTPError as error:
        with error:
            assert error.code == 307
            assert error.headers["Location"] == f"https://{host}/api/random-repository"
    else:
        raise AssertionError("Expected trusted-peer HTTPS redirect")
    try:
        opener.open(Request("http://web:8000/", headers={"Host": "evil.example"}), timeout=5)
    except HTTPError as error:
        with error:
            assert error.code == 400
    else:
        raise AssertionError("Web accepted an untrusted Host")
    body, _ = request("/api/random-repository", 404)
    assert json.loads(body)["detail"] == "No catalogued repository matches these filters."

    now = datetime.now(timezone.utc)
    engine = create_engine_from_url(get_settings().database_url)
    try:
        with create_session_factory(engine)() as session, session.begin():
            generation = CatalogGeneration(status="active", created_at=now, activated_at=now)
            session.add(generation)
            session.flush()
            session.add(
                Repository(
                    github_id=1,
                    full_name="ci/smoke-test",
                    name="smoke-test",
                    owner_login="ci",
                    html_url="https://github.com/ci/smoke-test",
                    description="Disposable container smoke-test fixture",
                    language="Python",
                    stargazer_count=42,
                    fork_count=0,
                    open_issues_count=0,
                    is_archived=False,
                    is_fork=False,
                    github_created_at=now,
                    github_updated_at=now,
                    github_pushed_at=now,
                    topics=["smoke-test"],
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            session.flush()
            session.add(
                GenerationRepository(generation_id=generation.id, github_id=1, last_seen_at=now)
            )
    finally:
        engine.dispose()

    body, headers = request("/api/random-repository")
    assert json.loads(body)["full_name"] == "ci/smoke-test"
    assert headers.get("Cache-Control") == "no-store"
    body, _ = request("/?discover=true")
    assert b"ci/smoke-test" in body, "Server-rendered discovery did not read the database"
    for path in ("/docs", "/redoc", "/openapi.json", "/health"):
        request(path, 404)
    print("Packaged web, database discovery, Caddy proxy/headers, and public routes passed.")


def check_backups(run, root: Path, project: str, compose: list[str]) -> None:
    """Use only the disposable stack; never back up/restore a developer database."""
    command = [
        sys.executable,
        "deployment/database_backup.py",
        "--env-file",
        str(root / "deployment/compose.env.example"),
        "--project-name",
        project,
    ]
    target = f"gh_random_restore_ci_{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="gh-random-backup-ci-") as temporary:
        backup = Path(
            run([*command, "backup", "--output-dir", temporary], capture=True).stdout.strip()
        )
        restore = [
            *command,
            "restore",
            "--backup",
            str(backup),
            "--target-database",
            target,
            "--trust-backup",
        ]
        summary = json.loads(run(restore, capture=True).stdout)
        assert summary["database"] == target
        assert (
            summary["repositories"] == summary["memberships"] == summary["active_memberships"] == 1
        )
        assert summary["generations"] == 1
        assert summary["alembic_revision"]
        print("Checking existing-target refusal (expected failure)...", flush=True)
        assert run(restore, check=False).returncode != 0, "Must refuse an existing database"
        # Query both the restored DB and the untouched source through the real app.
        program = """
import os
from sqlalchemy.engine import make_url
original_url = os.environ['DATABASE_URL']
os.environ['DATABASE_URL'] = make_url(original_url).set(
    database=os.environ['GH_RANDOM_RESTORE_DATABASE']
).render_as_string(hide_password=False)
from fastapi.testclient import TestClient
from app.main import app, create_app
from app.core.settings import get_settings
from app.db.session import create_engine_from_url, create_session_factory
with TestClient(app) as client:
    result = client.get('/api/random-repository')
    assert result.status_code == 200, result.status_code
    assert result.json()['github_id'] == 1
    assert 'ci/smoke-test' in client.get('/?discover=true').text
os.environ['DATABASE_URL'] = original_url
get_settings.cache_clear()
engine = create_engine_from_url(original_url)
try:
    with TestClient(create_app(session_factory=create_session_factory(engine))) as client:
        result = client.get('/api/random-repository')
        assert result.status_code == 200
        assert result.json()['github_id'] == 1
finally:
    engine.dispose()
print('Restored catalog discovery passed; source catalog remains usable.')
"""
        run(
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
            input_text=program,
        )
        with (backup / "database.dump").open("ab") as archive:
            archive.write(b"deliberate-integrity-test")
        corrupt_target = target + "_bad"
        corrupt = [*restore]
        corrupt[corrupt.index(target)] = corrupt_target
        print("Checking corrupt-backup refusal (expected failure)...", flush=True)
        assert run(corrupt, check=False).returncode != 0, "Must reject a corrupt backup"
        # Confirm rejection happened before CREATE DATABASE, not just during restore.
        result = run(
            [
                *compose,
                "exec",
                "-T",
                "database",
                "sh",
                "-eu",
                "-c",
                'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -X -A -t '
                '-v ON_ERROR_STOP=1 --command "$1"',
                "sh",
                f"SELECT count(*) FROM pg_database WHERE datname='{corrupt_target}'",
            ],
            capture=True,
        )
        assert result.stdout.strip() == "0"
    print("Backup, new-database restore, discovery, and failure guards passed.", flush=True)


def smoke_ownership_inside_web() -> None:
    """Run the packaged crawler with a one-item fake plan and no GitHub calls."""
    from sqlalchemy import select

    from app.core.settings import get_settings
    from app.crawler.planning import build_crawl_search_plan
    from app.crawler.service import CrawlService
    from app.db.models import CrawlRun
    from app.db.operations import CatalogOperationBusy
    from app.db.session import create_engine_from_url, create_session_factory
    from app.github.search import RepositorySearchPage, RepositorySearchSort, SearchOrder

    assert os.environ.get("GH_RANDOM_CONTAINER_SMOKE") == "1", "Not a CI smoke container"
    engine = create_engine_from_url(get_settings().database_url)
    factory = create_session_factory(engine)

    def plan(reference: datetime):
        from app.core.filters import PUSHED_BUCKETS, STAR_BUCKETS, SUPPORTED_LANGUAGES

        return build_crawl_search_plan(
            SUPPORTED_LANGUAGES[:1], STAR_BUCKETS[:1], PUSHED_BUCKETS[:1], reference_time=reference
        )

    class EmptySearcher:
        def __init__(self, interrupt: bool = False) -> None:
            self.interrupt = interrupt

        def search_repositories(
            self,
            query: str,
            *,
            page: int = 1,
            per_page: int = 100,
            sort: RepositorySearchSort | None = None,
            order: SearchOrder | None = None,
        ) -> RepositorySearchPage:
            if self.interrupt:
                raise KeyboardInterrupt()
            try:
                CrawlService(factory, EmptySearcher(), plan_builder=plan).run_full_crawl()
            except CatalogOperationBusy:
                pass
            else:
                raise AssertionError("A second crawler was allowed to overlap")
            return RepositorySearchPage(total_count=0, incomplete_results=False, repositories=())

    try:
        try:
            CrawlService(factory, EmptySearcher(interrupt=True), plan_builder=plan).run_full_crawl()
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("Interrupted crawler unexpectedly completed")
        CrawlService(factory, EmptySearcher(), plan_builder=plan).run_full_crawl()
        with factory() as session:
            statuses = session.scalars(select(CrawlRun.status).order_by(CrawlRun.id)).all()
            assert statuses == ["failed", "completed"]
        print("Packaged crawler exclusion and abandoned-run recovery passed.")
    finally:
        engine.dispose()


def check_containers(existing_image: str | None = None) -> None:
    """Validate and start the real production services with a small CI overlay."""

    root = Path(__file__).resolve().parents[2]
    project = f"gh-random-ci-{uuid.uuid4().hex[:12]}"
    image = existing_image or f"{project}:smoke"
    environment = {
        **os.environ,
        "GH_RANDOM_IMAGE": image,
        "GH_RANDOM_POSTGRES_ENV_FILE": str(root / "deployment/postgres.env.example"),
        "GH_RANDOM_APP_ENV_FILE": str(root / "deployment/app.env.example"),
        "GH_RANDOM_CRAWLER_ENV_FILE": str(root / "deployment/crawler.env.example"),
        "GH_RANDOM_CADDY_ENV_FILE": str(root / "deployment/caddy.env.example"),
        "GITHUB_APP_PRIVATE_KEY_HOST_PATH": str(root / ".github/fixtures/github-app-key.txt"),
    }
    # --env-file avoids loading the developer's local .env. An explicit unique
    # project prevents these tests or their cleanup from sharing production data.
    production = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        "deployment/compose.env.example",
        "-f",
        "compose.production.yaml",
    ]
    compose = [*production, "-f", ".github/compose.ci.yaml"]
    all_roles = [*compose, "--profile", "migrate", "--profile", "crawler"]

    def run(
        command: list[str],
        *,
        input_text: str | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=root,
            env=environment,
            input=input_text,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE if capture else None,
            check=check,
            timeout=900,
        )

    print(f"Disposable container test project: {project}", flush=True)
    succeeded = False
    image_built = False
    try:
        print("Validating production and CI Compose configurations...", flush=True)
        run([*production, "--profile", "migrate", "--profile", "crawler", "config", "--quiet"])
        rendered = json.loads(run([*all_roles, "config", "--format", "json"], capture=True).stdout)
        assert all(not s.get("ports") for s in rendered["services"].values())
        assert rendered["networks"]["backend"]["internal"] is True
        assert rendered["networks"]["proxy"]["internal"] is True
        assert set(rendered["services"]["web"]["networks"]) == {"proxy", "backend"}
        for service in rendered["services"].values():
            assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
            assert service["mem_limit"] and float(service["cpus"]) > 0
            assert service["pids_limit"] > 0

        if existing_image is None:
            print("Building the locked production image...", flush=True)
            run(["docker", "build", "--tag", image, "."])
            image_built = True
        else:
            print(f"Testing the supplied image: {image}", flush=True)
            if run(["docker", "image", "inspect", image], capture=True, check=False).returncode:
                run(["docker", "pull", image])
        caddy_bundle = run(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "cat",
                "caddy",
                "/etc/ssl/certs/ca-certificates.crt",
            ],
            capture=True,
        ).stdout
        caddy_trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        caddy_trust.load_verify_locations(cadata=caddy_bundle)
        check_ca_roots(caddy_trust)
        print(
            "Validating the HTTPS Caddy configuration without requesting certificates...",
            flush=True,
        )
        run(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "--env",
                "GH_RANDOM_DOMAIN=example.com",
                "caddy",
                "caddy",
                "validate",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
            ]
        )
        print("Migrating a fresh PostgreSQL database...", flush=True)
        run([*compose, "up", "--detach", "--wait", "--wait-timeout", "60", "database"])
        run([*compose, "--profile", "migrate", "run", "--rm", "migrate"])
        run([*compose, "--profile", "migrate", "run", "--rm", "migrate", "alembic", "check"])
        print("Checking optional loopback-only pgAdmin...", flush=True)
        run([sys.executable, ".github/scripts/check_pgadmin.py", "--", *compose])
        print(
            "Checking the hardened crawler role without a crawl or GitHub requests...", flush=True
        )
        run(
            [
                *compose,
                "--profile",
                "crawler",
                "run",
                "--rm",
                "--no-deps",
                "crawler",
                "python",
                "-m",
                "app.cli",
                "status",
            ]
        )
        print("Starting web and Caddy for HTTP/database smoke tests...", flush=True)
        run([*compose, "up", "--detach", "--wait", "--wait-timeout", "60", "web", "caddy"])
        for role in ("web", "caddy", "database"):
            container = run([*compose, "ps", "--quiet", role], capture=True).stdout.strip()
            info = json.loads(run(["docker", "inspect", container], capture=True).stdout)[0]
            host = info["HostConfig"]
            assert host["Memory"] > 0 and host["NanoCpus"] > 0 and host["PidsLimit"] > 0
            assert host["LogConfig"]["Config"]["max-size"] == "10m"
            if role != "database":
                assert host["ReadonlyRootfs"] is True
                assert "ALL" in host["CapDrop"]
                assert "no-new-privileges:true" in host["SecurityOpt"]
        run(
            [*compose, "exec", "-T", "web", "python", "-", "--inside"],
            input_text=Path(__file__).read_text(encoding="utf-8"),
        )
        access = run([*compose, "logs", "--no-color", "web"], capture=True).stdout
        probes = [line for line in access.splitlines() if "gh_random_proxy_probe=1" in line]
        assert probes and all("203.0.113.99" not in line for line in probes), (
            "Public client spoofed the access-log address"
        )
        print("Rehearsing backup and restore against the disposable catalog...", flush=True)
        check_backups(run, root, project, compose)
        run([sys.executable, ".github/scripts/check_encrypted_backups.py", "--", *compose])
        run(
            [*compose, "exec", "-T", "web", "python", "-", "--ownership"],
            input_text=Path(__file__).read_text(encoding="utf-8"),
        )
        print("Rehearsing the deployment CLI and failure recovery...", flush=True)
        run(
            [
                sys.executable,
                ".github/scripts/check_deployments.py",
                "--image",
                image,
                *(["--systemd"] if os.environ.get("GITHUB_ACTIONS") == "true" else []),
            ]
        )
        succeeded = True
    finally:
        if not succeeded:
            run([*all_roles, "ps", "--all"], check=False)
            run([*all_roles, "logs", "--no-color", "--tail", "150"], check=False)
        print(f"Cleaning up only {project} and its disposable volumes...", flush=True)
        cleanup = run([*all_roles, "down", "--volumes", "--remove-orphans"], check=False)
        if image_built:
            run(["docker", "image", "rm", image], check=False)
        if succeeded and cleanup.returncode:
            raise RuntimeError(f"Cleanup failed for disposable project {project}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--inside"]:
        smoke_inside_web()
    elif sys.argv[1:] == ["--ownership"]:
        smoke_ownership_inside_web()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--image", help="Test an existing image instead of building one")
        check_containers(parser.parse_args().image)
