"""Install the reviewed official ORAS CLI, verifying its pinned archive hash."""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import tarfile
import zipfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "1.3.4"
# Official release assets, reviewed against GitHub's release API SHA-256 values.
PINS = {
    "linux_amd64": "f27adb935022d94df8dc77719c322dda592c78a0d57a6f7dcdd8d900b248c454",
    "linux_arm64": "15702c6e3a4a56a8bd8ac5c17efdbcab56d9bada661ccbcf017f5b10c1d89399",
    "windows_amd64": "ffdb6aa40267686b5d507da1f21a57fc502a9a7c86b90c54557d335644c99dbd",
}
LIMIT = 40 * 1024 * 1024


def install(destination: Path) -> Path:
    architecture = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[
        platform.machine().lower()
    ]
    target = platform.system().lower() + "_" + architecture
    expected = PINS[target]
    windows = target.startswith("windows_")
    extension = "zip" if windows else "tar.gz"
    executable = "oras.exe" if windows else "oras"
    url = (
        f"https://github.com/oras-project/oras/releases/download/v{VERSION}/"
        f"oras_{VERSION}_{target}.{extension}"
    )
    with urlopen(url, timeout=60) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Official ORAS archive checksum mismatch")
    # No extractall: only the two reviewed regular files can become local files.
    files = {}
    if windows:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in (executable, "LICENSE"):
                info = archive.getinfo(name)
                if info.is_dir() or info.file_size > LIMIT:
                    raise ValueError("Unsafe ORAS archive member")
                files[name] = archive.read(name)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for name in (executable, "LICENSE"):
                member = archive.getmember(name)
                if not member.isfile() or member.size > LIMIT:
                    raise ValueError("Unsafe ORAS archive member")
                source = archive.extractfile(member)
                assert source is not None
                files[name] = source.read(LIMIT + 1)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("Unsafe ORAS destination")
    for name, content in files.items():
        path = destination / name
        if path.is_symlink():
            raise ValueError("Unsafe ORAS destination member")
        if path.exists():
            if not path.is_file() or path.read_bytes() != content:
                raise ValueError("Refusing to overwrite different ORAS tools")
        else:
            with path.open("xb") as output:
                output.write(content)
        path.chmod(0o755 if name == executable else 0o644)
    return destination / executable


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    print(install(parser.parse_args().destination.absolute()))
