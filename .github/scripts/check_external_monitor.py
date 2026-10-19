"""Trusted scheduled-workflow entry point; never interpolate configuration into shell."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment"))
spec = importlib.util.spec_from_file_location(
    "external_monitor", ROOT / "deployment/external_monitor.py"
)
assert spec is not None and spec.loader is not None
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def main() -> int:
    try:
        config = {
            "repository": os.environ["GITHUB_REPOSITORY"],
            "origin": os.environ["GH_MONITOR_ORIGIN"],
            "deployment_id": int(os.environ["GH_MONITOR_DEPLOYMENT"]),
            "telemetry_creator_id": int(os.environ["GH_MONITOR_CREATOR"]),
            "issue_number": int(os.environ["GH_MONITOR_ISSUE"]),
        }
        with tempfile.TemporaryDirectory(prefix="gh-random-external-") as temporary:
            path = Path(temporary) / "config.json"
            monitor.notifications.atomic_json(path, config)
            sys.argv = ["external_monitor.py", "check", "--config", str(path)]
            return monitor.main()
    except Exception as error:
        print(
            f"Enabled external monitoring is misconfigured ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
