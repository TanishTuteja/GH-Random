"""CI-only local object store fixture for the fixed OCI CLI put/get contract."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def main() -> int:
    root = Path(os.environ["GH_RANDOM_CI_OBJECT_STORE"]).resolve()
    assert root.is_dir() and root.name == "fake-objects"
    if (root / "fail-transfer").exists():
        return 1
    arguments = sys.argv[1:]
    assert arguments[:2] == ["--auth", "instance_principal"]
    name = arguments[arguments.index("--name") + 1]
    target = root / name
    assert target.resolve().is_relative_to(root)
    file = Path(arguments[arguments.index("--file") + 1])
    operation = arguments[arguments.index("object") + 1]
    if operation == "put":
        assert "--no-overwrite" in arguments and "--verify-checksum" in arguments
        if target.exists():
            print(json.dumps({"output": "already exists"}))
            return 0
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = file.read_bytes()
        with target.open("xb") as output:
            output.write(data)
        target.chmod(0o600)
        print(json.dumps({"etag": hashlib.sha256(data).hexdigest()}))
    else:
        assert operation == "get" and "--if-match" in arguments
        data = target.read_bytes()
        assert hashlib.sha256(data).hexdigest() == arguments[arguments.index("--if-match") + 1]
        with file.open("xb") as output:
            output.write(data)
        file.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
