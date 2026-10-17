"""Exercise the production backup timer against local age/Object Storage fixtures."""

from __future__ import annotations

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


def check(args, environment, run, mailbox, alerts_config, monitor_name):
    import grp
    import pwd

    sys.path.insert(0, str(ROOT / "deployment"))
    from install_age import install

    workspace = args.state_dir.parent
    prefix = args.project_name + "-backup"
    unit, timer = prefix + ".service", prefix + ".timer"
    alert = args.project_name + "-alert@" + unit + ".service"
    tools = workspace / "age-tools"
    age = install(tools)
    identity = workspace / "recovery-identity.txt"
    subprocess.run(
        [str(tools / "age-keygen"), "--output", str(identity)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    identity.chmod(0o600)
    key = subprocess.run(
        [str(tools / "age-keygen"), "-y", str(identity)],
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    recipients = workspace / "recipients.txt"
    recipients.write_bytes(key)
    recipients.chmod(0o600)
    # The backup unit gets only the public recipient; hide the recovery identity.
    store, archives = workspace / "fake-objects", workspace / "encrypted"
    store.mkdir(mode=0o700)
    archives.mkdir(mode=0o700)
    executable = workspace / "oci-fixture"
    executable.write_text(
        f"#!{sys.executable}\n" + (ROOT / ".github/scripts/fake_object_store.py").read_text()
    )
    executable.chmod(0o755)
    shutil.copyfile(ROOT / "deployment/crawler.py", workspace / "crawler.py")
    bootstrap = workspace / "installed-backup.py"
    shutil.copyfile(ROOT / "deployment/backup.py", bootstrap)
    config = json.loads((ROOT / "deployment/backup.json.example").read_text())
    config.update(
        archive_dir=str(archives),
        recipients_file=str(recipients),
        age_binary=str(age),
        oci_binary=str(executable),
        timeout_seconds=10,
    )
    config_path = workspace / "backup.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    notification_config = json.loads(alerts_config.read_text())
    notification_config.update(backup_unit=unit, monitor_backups=True)
    alerts_config.write_text(json.dumps(notification_config))
    source = (ROOT / "deployment/systemd/gh-random-backup.service").read_text()
    source = source.replace("User=root", "User=" + pwd.getpwuid(os.getuid()).pw_name)
    source = source.replace("Group=root", "Group=" + grp.getgrgid(os.getgid()).gr_name)
    source = source.replace("gh-random-backup", prefix)
    source = source.replace("gh-random-alert@", args.project_name + "-alert@")
    source = source.replace("RuntimeMaxSec=40min", "RuntimeMaxSec=45s")
    host = (
        environment.get("DOCKER_HOST")
        or run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
        ).stdout.strip()
    )
    source = re.sub(r"(?m)^Environment=DOCKER_HOST=.*$", "Environment=DOCKER_HOST=" + host, source)
    command = [
        sys.executable,
        str(bootstrap),
        "run",
        "--state-dir",
        str(args.state_dir),
        "--env-file",
        str(args.env_file),
        "--config",
        str(config_path),
        "--project-name",
        args.project_name,
    ]
    source = re.sub(r"(?m)^ExecStart=.*$", "ExecStart=" + shlex.join(command), source)
    source = re.sub(
        r"(?m)^ReadWritePaths=.*$", f"ReadWritePaths={args.state_dir} {archives} {store}", source
    )
    source = source.replace(
        "UMask=0077",
        f"UMask=0077\nEnvironment=GH_RANDOM_CI_OBJECT_STORE={store}\nInaccessiblePaths={identity}",
    )
    if environment.get("LD_LIBRARY_PATH"):
        source = source.replace(
            "UMask=0077",
            "UMask=0077\nEnvironment=LD_LIBRARY_PATH=" + environment["LD_LIBRARY_PATH"],
        )
    service_file = workspace / unit
    service_file.write_text(source)
    policy = (ROOT / "deployment/systemd/gh-random-backup.timer").read_text()
    assert "OnCalendar=*-*-* 05:15:00 UTC" in policy and "Persistent=true" in policy
    policy = policy.replace("05:15:00 UTC", "*:*:0/5 UTC").replace("gh-random-backup.service", unit)
    timer_file = workspace / timer
    timer_file.write_text(policy)
    installed = [Path("/run/systemd/system") / unit, Path("/run/systemd/system") / timer]
    record_path = args.state_dir / "backup.json"
    success_path = args.state_dir / "backup-success.json"

    def wait(predicate):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            item = json.loads(record_path.read_text()) if record_path.exists() else {}
            if predicate(item):
                return item
            time.sleep(0.25)
        raise AssertionError("Scheduled backup did not reach expected state")

    try:
        for src, destination in zip((service_file, timer_file), installed, strict=True):
            run(["sudo", "install", "-m", "0644", str(src), str(destination)])
        run(["sudo", "systemd-analyze", "verify", *map(str, installed)])
        run(["sudo", "systemctl", "daemon-reload"])
        (store / "fail-transfer").touch()
        before = len(mailbox.messages)
        run(["sudo", "systemctl", "start", timer])
        failed = wait(lambda item: item.get("status") == "failed")
        assert not success_path.exists()
        mailbox.wait_for_messages(before + 1)
        (store / "fail-transfer").unlink()
        succeeded = wait(
            lambda item: item.get("id") != failed["id"] and item.get("status") == "succeeded"
        )
        run(["sudo", "systemctl", "stop", timer])
        assert succeeded["cleanup_complete"] and succeeded["offsite"]["sha256"]
        assert json.loads(success_path.read_text())["id"] == succeeded["id"]
        run(["sudo", "systemctl", "start", monitor_name])
        mailbox.wait_for_messages(before + 2)
        assert b"recovered" in mailbox.messages[-1]
        assert not list(archives.glob(".work-*"))
        # Recover ciphertext copied back from the independent local object store,
        # not the original local archive, and compare its full inner dump manifest.
        sys.path.insert(0, str(ROOT / "deployment"))
        from backup_archive import unseal
        from database_backup import verify_backup

        remote = store / succeeded["offsite"]["object_name"]
        recovered = unseal(remote, identity, workspace / "offline-recovery", age)
        verify_backup(recovered)
        print(
            "Native backup timer failure/next success, encrypted read-back, "
            "recovery emails and offline decrypt passed.",
            flush=True,
        )
    except BaseException:
        print(
            run(["sudo", "journalctl", "-u", unit, "--no-pager", "-n", "100"], check=False).stdout
        )
        raise
    finally:
        run(["sudo", "systemctl", "stop", timer, unit, alert], check=False)
        run(["sudo", "systemctl", "clean", "--what=state", timer], check=False)
        run(["sudo", "systemctl", "reset-failed", unit, alert], check=False)
        for path in installed:
            run(["sudo", "unlink", str(path)], check=False)
        run(["sudo", "systemctl", "daemon-reload"], check=False)
