"""Test the host supervisor and, only in Linux CI, real temporary systemd units."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def check(arguments: argparse.Namespace) -> None:
    state = arguments.state_dir
    workspace = state.parent
    record_file = state / "crawler.json"
    bootstrap = workspace / "installed-crawler.py"
    shutil.copyfile(ROOT / "deployment/crawler.py", bootstrap)
    crawler_template = arguments.crawler_env.read_text(encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if key != "INVOCATION_ID"}
    launcher = [
        sys.executable,
        str(bootstrap),
        "run",
        "--state-dir",
        str(state),
        "--env-file",
        str(arguments.env_file),
        "--project-name",
        arguments.project_name,
    ]
    if os.name == "nt":
        # The installed systemd bootstrap uses POSIX exec semantics. Exercise
        # the exact release worker locally; Linux CI also tests real dispatch.
        launcher[1:3] = [str(arguments.release_dir / "deployment/crawler.py"), "worker"]
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(arguments.env_file),
        "-f",
        str(arguments.release_dir / "compose.production.yaml"),
        "-p",
        arguments.project_name,
    ]

    def run(command, *, check=True, capture=True, timeout=90):
        result = subprocess.run(
            command,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
            timeout=timeout,
        )
        if check and result.returncode:
            raise AssertionError(f"Command failed: {command[0]}\n{result.stdout}\n{result.stderr}")
        return result

    def mode(value):
        arguments.crawler_env.write_text(
            crawler_template + f"GH_RANDOM_CI_CRAWL_MODE={value}\n", encoding="utf-8"
        )

    def record():
        return json.loads(record_file.read_text(encoding="utf-8")) if record_file.exists() else {}

    def wait_for(predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = record()
            if predicate(value):
                return value
            time.sleep(0.25)
        raise AssertionError(f"Scheduler condition not met: {record()}")

    def no_child(value):
        result = run(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=io.gh-random.scheduled-crawl={value['id']}",
            ]
        )
        assert not result.stdout.strip(), result.stdout

    def normal(expected, *, runtime=30):
        result = run([*launcher, "--max-runtime-seconds", str(runtime)], check=False)
        print(result.stdout, result.stderr, flush=True)
        assert result.returncode == expected
        value = record()
        assert value["cleanup_complete"] is True
        no_child(value)
        return value

    print("Checking ordinary crawl failure and next normal scheduled run...", flush=True)
    mode("fail")
    assert normal(1)["status"] == "failed"
    mode("success")
    assert normal(0)["status"] == "succeeded"
    print("Checking timeout, shared deployment lock and managed orphan recovery...", flush=True)
    mode("hang")
    assert normal(124, runtime=2)["status"] == "timed_out"
    mode("success")
    assert normal(0)["status"] == "succeeded"
    mode("hang")
    old_id = record()["id"]
    with (workspace / "supervisor.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [*launcher, "--max-runtime-seconds", "60"], env=environment, stdout=log, stderr=log
        )
        try:
            active = wait_for(
                lambda item: item.get("id") != old_id and item.get("status") == "running"
            )
            contender = run([*launcher], check=False)
            assert contender.returncode != 0 and record()["id"] == active["id"]
            deployment = run(
                [
                    sys.executable,
                    str(arguments.release_dir / "deployment/deploy.py"),
                    "apply",
                    "--env-file",
                    str(arguments.env_file),
                    "--state-dir",
                    str(state),
                    "--backup-dir",
                    str(workspace / "backups"),
                    "--url",
                    "https://127.0.0.1",
                    "--project-name",
                    arguments.project_name,
                ],
                check=False,
            )
            assert deployment.returncode != 0 and "Another deployment" in deployment.stderr
            # Kill the host wrapper, deliberately leaving its daemon-owned child.
            process.kill()
            process.wait(timeout=10)
            assert (
                run(
                    ["docker", "inspect", active["container_id"], "--format", "{{.State.Running}}"]
                ).stdout.strip()
                == "true"
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    mode("success")
    assert normal(0)["status"] == "succeeded"
    no_child(active)

    if arguments.systemd:
        native_units(arguments, launcher, bootstrap, environment, mode, record, wait_for, no_child)

    # The real service must recover interrupted CrawlRuns before starting fresh.
    program = """
from sqlalchemy import select
from app.core.settings import get_settings
from app.db.session import create_engine_from_url, create_session_factory
from app.db.models import CrawlRun
engine = create_engine_from_url(get_settings().database_url)
with create_session_factory(engine)() as session:
    runs = session.scalars(select(CrawlRun).order_by(CrawlRun.id)).all()
    assert len(runs) >= 5
    assert all(run.status != 'running' and run.resume_count == 0 for run in runs)
    assert any(run.status == 'failed' for run in runs)
    assert runs[-1].status == 'completed'
engine.dispose()
print('Normal successor recovered abandoned rows without --resume.')
"""
    result = subprocess.run(
        [*compose, "exec", "-T", "web", "python", "-"],
        input=program,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=True,
    )
    print(result.stdout, flush=True)
    print("Scheduled crawler exits, timeout, exclusion and orphan recovery passed.", flush=True)


def native_units(args, launcher, bootstrap, environment, mode, record, wait_for, no_child):
    import grp
    import pwd

    assert os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux"
    assert re.fullmatch(r"gh-random-deploy-ci-[0-9a-f]{12}", args.project_name)
    unit_name = args.project_name + ".service"
    timer_name = args.project_name + ".timer"
    state = args.state_dir
    workspace = state.parent

    def run(command, *, check=True):
        result = subprocess.run(
            command,
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=120,
            check=False,
        )
        if check and result.returncode:
            raise AssertionError(f"Native unit check failed: {result.stdout}\n{result.stderr}")
        return result

    source = (ROOT / "deployment/systemd/gh-random-crawler.service").read_text()
    source = source.replace("User=root", f"User={pwd.getpwuid(os.getuid()).pw_name}")
    source = source.replace("Group=root", f"Group={grp.getgrgid(os.getgid()).gr_name}")
    source = source.replace("gh-random-crawler", args.project_name)
    if environment.get("LD_LIBRARY_PATH"):
        # setup-python's toolcache interpreter may need its shared-library path;
        # systemd does not inherit the caller's shell environment automatically.
        source = source.replace(
            "UMask=0077",
            "UMask=0077\nEnvironment=LD_LIBRARY_PATH=" + environment["LD_LIBRARY_PATH"],
        )
    host = (
        environment.get("DOCKER_HOST")
        or run(
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
            ]
        ).stdout.strip()
    )
    source = re.sub(r"(?m)^Environment=DOCKER_HOST=.*$", f"Environment=DOCKER_HOST={host}", source)
    source = re.sub(r"(?m)^ExecStart=.*$", "ExecStart=" + shlex.join(launcher), source)
    stop = [sys.executable, str(bootstrap), "stop", "--state-dir", str(state)]
    source = re.sub(r"(?m)^ExecStopPost=.*$", "ExecStopPost=" + shlex.join(stop), source)
    source = source.replace("RuntimeMaxSec=5h5min", "RuntimeMaxSec=20s")
    source = re.sub(r"(?m)^ReadWritePaths=.*$", f"ReadWritePaths={state}", source)
    source = source.replace("[Unit]\n", "[Unit]\nStartLimitIntervalSec=0\n", 1)
    service_file = workspace / unit_name
    service_file.write_text(source, encoding="utf-8")
    timer_source = (ROOT / "deployment/systemd/gh-random-crawler.timer").read_text()
    assert "OnCalendar=*-*-* 00,06,12,18:00:00 UTC" in timer_source
    assert "Persistent=true" in timer_source
    print(
        run(
            ["systemd-analyze", "calendar", "--iterations=4", "*-*-* 00,06,12,18:00:00 UTC"]
        ).stdout,
        flush=True,
    )
    timer_source = timer_source.replace("gh-random-crawler.service", unit_name)
    # Scale only the clock for CI. Never enable the actual production schedule.
    timer_source = timer_source.replace("00,06,12,18:00:00 UTC", "*:*:0/5 UTC")
    timer_file = workspace / timer_name
    timer_file.write_text(timer_source, encoding="utf-8")
    installed = [Path("/run/systemd/system") / unit_name, Path("/run/systemd/system") / timer_name]
    try:
        for src, dest in zip((service_file, timer_file), installed, strict=True):
            run(["sudo", "install", "-m", "0644", str(src), str(dest)])
        run(["sudo", "systemd-analyze", "verify", str(installed[0]), str(installed[1])])
        run(["sudo", "systemctl", "daemon-reload"])
        print("Checking real timer failure followed by a successful next tick...", flush=True)
        old = record()["id"]
        mode("fail")
        run(["sudo", "systemctl", "start", timer_name])
        failed = wait_for(
            lambda item: (
                item.get("id") != old
                and item.get("status") == "failed"
                and item.get("cleanup_complete")
            )
        )
        mode("success")
        succeeded = wait_for(
            lambda item: (
                item.get("id") != failed["id"]
                and item.get("status") == "succeeded"
                and item.get("cleanup_complete")
            )
        )
        run(["sudo", "systemctl", "stop", timer_name])
        no_child(succeeded)

        for operation in ("stop", "kill", "watchdog"):
            print(f"Checking real systemd {operation} container cleanup...", flush=True)
            previous = record()["id"]
            mode("hang")
            run(["sudo", "systemctl", "start", "--no-block", unit_name])
            active = wait_for(
                lambda item: item.get("id") != previous and item.get("status") == "running"
            )
            if operation == "stop":
                stop_started = time.monotonic()
                run(["sudo", "systemctl", "stop", unit_name])
                assert time.monotonic() - stop_started < 15, "SIGTERM cleanup was not prompt"
            elif operation == "kill":
                run(["sudo", "systemctl", "kill", "--kill-whom=main", "--signal=KILL", unit_name])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = run(
                    ["sudo", "systemctl", "show", "--property=ActiveState", "--value", unit_name]
                )
                if status.stdout.strip() in ("failed", "inactive"):
                    break
                time.sleep(0.25)
            else:
                raise AssertionError("Native service failed to finish")
            no_child(active)
        mode("success")
        run(["sudo", "systemctl", "start", unit_name])
        wait_for(lambda item: item.get("status") == "succeeded" and item.get("cleanup_complete"))
        print("Native systemd timer retry, stop, SIGKILL fallback and watchdog passed.", flush=True)
    except BaseException:
        print(
            run(
                ["sudo", "journalctl", "-u", unit_name, "--no-pager", "-n", "150"], check=False
            ).stdout
        )
        raise
    finally:
        run(["sudo", "systemctl", "stop", timer_name, unit_name], check=False)
        run(["sudo", "systemctl", "clean", "--what=state", timer_name], check=False)
        run(["sudo", "systemctl", "reset-failed", unit_name], check=False)
        for path in installed:
            run(["sudo", "unlink", str(path)], check=False)
        run(["sudo", "systemctl", "daemon-reload"], check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("release-dir", "state-dir", "env-file", "crawler-env"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--systemd", action="store_true")
    check(parser.parse_args())
