"""Start only opt-in pgAdmin against a caller-owned disposable Compose database."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def check(compose: list[str]) -> None:
    project = compose[compose.index("--project-name") + 1]
    assert re.fullmatch(r"gh-random-ci-[0-9a-f]{12}", project), "Disposable CI project required"
    with tempfile.TemporaryDirectory(prefix="gh-random-admin-") as directory:
        password = Path(directory) / "password.txt"
        password.write_text("disposable-pgadmin-fixture-password", encoding="utf-8")
        password.chmod(0o444)  # Readable by container UID 5050; parent is private.
        environment = {
            **os.environ,
            "GH_RANDOM_PGADMIN_EMAIL": "admin@example.com",
            "GH_RANDOM_PGADMIN_PASSWORD_FILE": str(password),
            "GH_RANDOM_PGADMIN_PORT": "0",  # Random loopback-only test port.
        }
        command = [*compose, "-f", "compose.pgadmin.yaml", "--profile", "admin"]

        def run(arguments: list[str], *, check: bool = True) -> str:
            return subprocess.run(
                arguments,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=check,
                timeout=300,
            ).stdout.strip()

        try:
            rendered = json.loads(run([*command, "config", "--format", "json"]))
            admin = rendered["services"]["pgadmin"]
            assert admin["profiles"] == ["admin"]
            assert set(admin["networks"]) == {"backend", "admin_access"}
            assert rendered["networks"]["backend"]["internal"] is True
            assert admin["ports"][0]["host_ip"] == "127.0.0.1"
            assert "PGADMIN_DEFAULT_PASSWORD" not in admin["environment"]
            assert admin["restart"] == "no"
            run([*command, "up", "--detach", "--no-deps", "pgadmin"])
            container = run([*command, "ps", "--quiet", "pgadmin"])
            info = json.loads(run(["docker", "inspect", container]))[0]
            assert info["Config"]["User"] == "5050:5050"
            host = info["HostConfig"]
            assert "ALL" in host["CapDrop"] and "no-new-privileges:true" in host["SecurityOpt"]
            assert host["Memory"] > 0 and host["NanoCpus"] > 0 and host["PidsLimit"] > 0
            assert host["LogConfig"]["Config"] == {"max-size": "10m", "max-file": "3"}
            bindings = info["NetworkSettings"]["Ports"]["5050/tcp"]
            assert len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1", bindings
            url = f"http://127.0.0.1:{int(bindings[0]['HostPort'])}/login"
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 90
            while True:
                try:
                    with opener.open(url, timeout=5) as response:
                        assert response.status == 200 and b"pgAdmin" in response.read()
                    break
                except (URLError, OSError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)
            # Real database DNS/connectivity without a password in host arguments.
            probe = "import socket; socket.create_connection(('database', 5432), timeout=5).close()"
            run([*command, "exec", "-T", "pgadmin", "/venv/bin/python", "-c", probe])
            print(
                "Private pgAdmin login, non-root runtime and database connectivity passed.",
                flush=True,
            )
        except BaseException:
            print(run([*command, "logs", "--tail", "100", "pgadmin"], check=False))
            raise
        finally:
            run([*command, "rm", "--stop", "--force", "pgadmin"])
            for kind, name in (("volume", "pgadmin_data"), ("network", "admin_access")):
                resource = project + "_" + name
                if run(["docker", kind, "ls", "--quiet", "--filter", "name=" + resource]):
                    run(["docker", kind, "rm", resource])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compose", nargs=argparse.REMAINDER)
    arguments = parser.parse_args().compose
    check(arguments[1:] if arguments[:1] == ["--"] else arguments)
