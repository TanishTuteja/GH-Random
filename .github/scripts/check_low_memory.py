"""Rehearse a rolling catalog in a disposable, shared 768 MiB Linux cgroup.

Requires a local systemd/cgroup-v2 Docker host and passwordless sudo (CI).
This measures containers, including pg_dump and file cache; host Docker/systemd
and kernel overhead remain outside the budget. It is not an E2 CPU benchmark.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path("/app") if __file__ == "<stdin>" else Path(__file__).resolve().parents[2]
BUDGET = 768 * 1024 * 1024


def inside_seed() -> None:
    """Create representative history using SQL, without loading rows into Python."""
    from sqlalchemy import text

    from app.core.settings import get_settings
    from app.db.session import create_engine_from_url

    assert os.environ.get("GH_RANDOM_MEMORY_REHEARSAL") == "1"
    engine = create_engine_from_url(get_settings().database_url)
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM repositories")) == 0
        connection.execute(
            text("""
                INSERT INTO repositories
                  (github_id, full_name, name, owner_login, html_url, description,
                   language, stargazer_count, fork_count, open_issues_count,
                   is_archived, is_fork, github_created_at, github_updated_at,
                   github_pushed_at, topics, first_seen_at, last_seen_at)
                SELECT n, 'rehearsal/repo-' || n, 'repo-' || n, 'rehearsal',
                       'https://github.com/rehearsal/repo-' || n, repeat('catalog ', 64),
                       'Python', n % 10000, 1, 0, false, false, now(), now(), now(),
                       '["rehearsal"]'::json, now() - interval '1 day',
                       now() - interval '1 day'
                FROM generate_series(1, 55000) AS n
            """)
        )
        for number in range(1, 30):
            # 28 snapshots represent six-hourly crawling over the retained week;
            # the extra expired snapshot exercises deletion and orphan cleanup.
            age_hours = 192 if number == 29 else (28 - number) * 6
            generation = connection.scalar(
                text("""
                    INSERT INTO catalog_generations (status, created_at, activated_at)
                    VALUES (:status, now() - :age * interval '1 hour',
                            now() - :age * interval '1 hour') RETURNING id
                """),
                {"status": "active" if number == 28 else "retired", "age": age_hours},
            )
            connection.execute(
                text("""
                    INSERT INTO generation_repositories
                      (generation_id, github_id, last_seen_at)
                    SELECT :generation, n, now() - interval '1 day'
                    FROM generate_series(1, :count) AS n
                """),
                {"generation": generation, "count": 55000 if number == 29 else 50000},
            )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("ANALYZE")
    engine.dispose()
    print("Seeded 50,000 active repositories, 28 retained snapshots and 5,000 expired orphans.")


def inside_crawl() -> None:
    """Exercise the real full plan; only the GitHub network boundary is synthetic."""
    from datetime import datetime, timezone

    from sqlalchemy import text

    from app.core.settings import get_settings
    from app.crawler.service import CrawlService
    from app.db.session import create_engine_from_url, create_session_factory
    from app.github.search import RepositorySearchPage

    assert os.environ.get("GH_RANDOM_MEMORY_REHEARSAL") == "1"
    engine = create_engine_from_url(get_settings().database_url)
    timestamp = datetime.now(timezone.utc).isoformat()

    class Searcher:
        calls = 0

        def search_repositories(self, query, *, page=1, per_page=100, sort=None, order=None):
            assert page == 1 and per_page == 100
            offset = self.calls * 100
            self.calls += 1
            return RepositorySearchPage(
                total_count=100,
                incomplete_results=False,
                repositories=tuple(
                    {
                        "id": number,
                        "name": f"repo-{number}",
                        "full_name": f"rehearsal/repo-{number}",
                        "owner": {"login": "rehearsal"},
                        "html_url": f"https://github.com/rehearsal/repo-{number}",
                        "description": "Refreshed metadata " * 30,
                        "language": "Python",
                        "stargazers_count": number % 10000,
                        "forks_count": 1,
                        "open_issues_count": 0,
                        "archived": False,
                        "fork": False,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "pushed_at": timestamp,
                        "topics": ["rehearsal"],
                    }
                    for number in range(offset + 1, offset + 101)
                ),
            )

    searcher = Searcher()
    try:
        summary = CrawlService(create_session_factory(engine), searcher).run_full_crawl()
        assert summary.requests_made == searcher.calls == 200
        assert summary.repositories_seen == 20000
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM repositories")) == 50000
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*) FROM generation_repositories AS m
                    JOIN catalog_generations AS g ON g.id = m.generation_id
                    WHERE g.status = 'active'
                """)
                )
                == 50000
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM crawl_runs WHERE status = 'completed'")
                )
                == 1
            )
        print("Full 200-query crawl published; rolling membership and expired history verified.")
    finally:
        engine.dispose()


def check(image: str | None = None) -> None:
    if sys.platform != "linux" or not Path("/run/systemd/system").is_dir():
        raise RuntimeError("Low-memory rehearsal requires a local Linux systemd Docker host")
    identity = "ghrandommemory" + uuid.uuid4().hex[:12]
    project = "gh-random-memory-" + uuid.uuid4().hex[:12]
    crawler_name = project + "-crawler"
    slice_name = identity + ".slice"
    unit = Path("/run/systemd/system") / slice_name
    built = image is None
    image = image or project + ":smoke"
    environment = {
        **os.environ,
        "GH_RANDOM_IMAGE": image,
        "GH_RANDOM_POSTGRES_ENV_FILE": str(ROOT / "deployment/postgres.env.example"),
        "GH_RANDOM_APP_ENV_FILE": str(ROOT / "deployment/app.env.example"),
        "GH_RANDOM_CRAWLER_ENV_FILE": str(ROOT / "deployment/crawler.env.example"),
        "GH_RANDOM_CADDY_ENV_FILE": str(ROOT / "deployment/caddy.env.example"),
        "GITHUB_APP_PRIVATE_KEY_HOST_PATH": str(ROOT / ".github/fixtures/github-app-key.txt"),
    }

    def run(command, *, capture=False, check=True, input_text=None, timeout=600):
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            encoding="utf-8",
            input=input_text,
            stdout=subprocess.PIPE if capture else None,
            check=check,
            timeout=timeout,
        )

    info = run(["docker", "info", "--format", "{{.CgroupDriver}} {{.CgroupVersion}}"], capture=True)
    assert info.stdout.strip() == "systemd 2", "Expected local systemd/cgroup-v2 Docker"
    print(f"Disposable low-memory project: {project}; shared budget: 768 MiB, no swap.", flush=True)
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        folder = Path(temporary)
        source_unit = folder / slice_name
        source_unit.write_text(
            "[Unit]\nDescription=Disposable GH Random memory rehearsal\n"
            "[Slice]\nMemoryAccounting=yes\nMemoryMax=768M\nMemorySwapMax=0\n",
            encoding="utf-8",
        )
        override = folder / "memory.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        role: {
                            "cgroup_parent": slice_name,
                            **(
                                {"ports": ["127.0.0.1::80"]}
                                if role == "caddy"
                                else {"environment": {"GH_RANDOM_MEMORY_REHEARSAL": "1"}}
                            ),
                        }
                        for role in ("database", "web", "crawler", "migrate", "caddy")
                    }
                }
            ),
            encoding="utf-8",
        )
        compose = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            "deployment/compose.env.example",
            "-f",
            "compose.production.yaml",
            "-f",
            ".github/compose.ci.yaml",
            "-f",
            str(override),
        ]
        all_roles = [*compose, "--profile", "migrate", "--profile", "crawler"]
        unit_installed = False
        crawl = None
        stop = threading.Event()
        latencies: list[float] = []
        succeeded = False
        try:
            if built:
                run(["docker", "build", "--tag", image, "."])
            run(["sudo", "install", "-m", "0644", str(source_unit), str(unit)])
            unit_installed = True
            run(["sudo", "systemctl", "daemon-reload"])
            run(["sudo", "systemctl", "start", slice_name])
            group = run(
                ["systemctl", "show", slice_name, "--property=ControlGroup", "--value"],
                capture=True,
            ).stdout.strip()
            assert group.startswith("/") and group.endswith("/" + slice_name)
            cgroup = Path("/sys/fs/cgroup") / group.lstrip("/")
            assert int((cgroup / "memory.max").read_text()) == BUDGET
            assert (cgroup / "memory.swap.max").read_text().strip() == "0"
            run([*compose, "up", "-d", "--wait", "--wait-timeout", "60", "database"])
            run([*compose, "--profile", "migrate", "run", "--rm", "migrate"])
            program = Path(__file__).read_text(encoding="utf-8")
            run([*compose, "exec", "-T", "database", "sh", "-c", "echo 'Database ready'"])
            run(
                [
                    *compose,
                    "--profile",
                    "migrate",
                    "run",
                    "--rm",
                    "-T",
                    "migrate",
                    "python",
                    "-",
                    "--seed",
                ],
                input_text=program,
            )
            run([*compose, "up", "-d", "--wait", "--wait-timeout", "60", "web", "caddy"])
            services = {}
            for role in ("database", "web", "caddy"):
                container = run([*compose, "ps", "-q", role], capture=True).stdout.strip()
                details = json.loads(run(["docker", "inspect", container], capture=True).stdout)[0]
                assert details["HostConfig"]["CgroupParent"] == slice_name
                services[role] = container
                if role == "caddy":
                    port = details["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"]
            address = f"http://127.0.0.1:{port}"
            # Compose's service_started condition is not HTTP readiness. Allow
            # startup here; every request during the measured load must succeed.
            deadline = time.monotonic() + 45
            while True:
                try:
                    with build_opener(ProxyHandler({})).open(
                        Request(address + "/", headers={"Host": "localhost"}), timeout=5
                    ) as response:
                        assert response.status == 200
                    break
                except (URLError, OSError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)

            def traffic():
                opener = build_opener(ProxyHandler({}))
                while not stop.is_set():
                    start = time.monotonic()
                    with opener.open(
                        Request(address + "/api/random-repository", headers={"Host": "localhost"}),
                        timeout=10,
                    ) as response:
                        assert response.status == 200
                        assert json.load(response)["full_name"].startswith("rehearsal/")
                    latencies.append(time.monotonic() - start)
                    stop.wait(0.25)

            with ThreadPoolExecutor(max_workers=2) as executor:
                requests = [executor.submit(traffic) for _ in range(2)]
                crawl = subprocess.Popen(
                    [
                        *compose,
                        "--profile",
                        "crawler",
                        "run",
                        "--name",
                        crawler_name,
                        "--rm",
                        "-T",
                        "crawler",
                        "python",
                        "-",
                        "--crawl",
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdin=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                try:
                    assert crawl.stdin is not None
                    crawl.stdin.write(program)
                    crawl.stdin.close()
                    # Confirm the one-off crawler is in the same constrained
                    # group; it must not escape merely because it uses `run`.
                    deadline = time.monotonic() + 30
                    while True:
                        inspection = subprocess.run(
                            ["docker", "inspect", crawler_name],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            timeout=10,
                        )
                        if inspection.returncode == 0:
                            details = json.loads(inspection.stdout)[0]
                            assert details["HostConfig"]["CgroupParent"] == slice_name
                            break
                        assert crawl.poll() is None, "Crawler exited before its group was verified"
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Crawler container was not created in time")
                        time.sleep(0.1)
                    # Use the normal backup CLI, targeting only this uniquely named
                    # project; pg_dump runs inside the budgeted database container.
                    run(
                        [
                            sys.executable,
                            "deployment/database_backup.py",
                            "--env-file",
                            str(ROOT / "deployment/compose.env.example"),
                            "--project-name",
                            project,
                            "backup",
                            "--output-dir",
                            str(folder / "backups"),
                        ],
                    )
                    assert crawl.wait(timeout=600) == 0, "Budgeted full crawl failed"
                finally:
                    stop.set()
                for task in requests:
                    task.result()
            for role, container in services.items():
                details = json.loads(run(["docker", "inspect", container], capture=True).stdout)[0]
                assert details["State"]["Running"] and not details["State"]["OOMKilled"], role
                assert details["RestartCount"] == 0, role
            events = dict(
                line.split() for line in (cgroup / "memory.events").read_text().splitlines()
            )
            assert int(events["oom"]) == int(events["oom_kill"]) == 0, events
            peak = int((cgroup / "memory.peak").read_text())
            assert 0 < peak <= BUDGET
            assert len(latencies) >= 20, "Insufficient concurrent discovery traffic"
            print(
                json.dumps(
                    {
                        "budget_mib": 768,
                        "peak_mib": round(peak / 1024 / 1024, 1),
                        "requests": len(latencies),
                        "http_p95_seconds": round(sorted(latencies)[int(len(latencies) * 0.95)], 3),
                        "http_max_seconds": round(max(latencies), 3),
                        "memory_events": events,
                        "active_repositories": 50000,
                        "initial_memberships": 1455000,
                        "crawl_combinations": 200,
                        "synthetic_repositories_refreshed": 20000,
                        "expired_orphans_removed": 5000,
                    }
                ),
                flush=True,
            )
            succeeded = True
        finally:
            stop.set()
            if crawl is not None and crawl.poll() is None:
                crawl.terminate()
                crawl.wait(timeout=15)
            if not succeeded:
                run([*all_roles, "logs", "--no-color", "--tail", "80"], check=False)
            # Terminating a Compose client does not necessarily stop its one-off
            # container. Remove only one-offs labelled with this unique project.
            one_offs = run(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--filter",
                    "label=com.docker.compose.oneoff=True",
                ],
                capture=True,
            ).stdout.split()
            if one_offs:
                run(["docker", "rm", "--force", *one_offs])
            cleanup = run([*all_roles, "down", "--volumes", "--remove-orphans"], check=False)
            if unit_installed:
                run(["sudo", "systemctl", "stop", slice_name], check=False)
                run(["sudo", "rm", "--", str(unit)])
                run(["sudo", "systemctl", "daemon-reload"])
            if built:
                run(["docker", "image", "rm", image], check=False)
            if succeeded and cleanup.returncode:
                raise RuntimeError("Disposable memory rehearsal cleanup failed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--seed"]:
        inside_seed()
    elif sys.argv[1:] == ["--crawl"]:
        inside_crawl()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--image", help="Use an already built image")
        check(parser.parse_args().image)
