"""Install checksum-pinned official age tools without extracting arbitrary paths."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import tarfile
import zipfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "1.3.2"
PINS = {
    "linux-amd64": "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10",
    "linux-arm64": "6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4",
    "windows-amd64": "f48d8f8f9ebe903ab5027ed067652f2cc1db94bc206976430133b905dcd8e8c7",
}


def install(destination: Path) -> Path:
    architecture = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[
        platform.machine().lower()
    ]
    target = platform.system().lower() + "-" + architecture
    digest = PINS[target]
    suffix = ".exe" if os.name == "nt" else ""
    extension = "zip" if os.name == "nt" else "tar.gz"
    url = f"https://github.com/FiloSottile/age/releases/download/v{VERSION}/age-v{VERSION}-{target}.{extension}"
    with urlopen(url, timeout=60) as response:
        data = response.read(40 * 1024 * 1024 + 1)
    if len(data) > 40 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Official age archive checksum mismatch")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    names = ["age/age" + suffix, "age/age-keygen" + suffix, "age/LICENSE"]
    if extension == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {name: archive.read(name) for name in names}
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            files = {}
            for name in names:
                member = archive.getmember(name)
                if not member.isfile():
                    raise ValueError("Expected regular age tool files")
                source = archive.extractfile(member)
                assert source is not None
                files[name] = source.read()
    for name, value in files.items():
        path = destination / Path(name).name
        if path.exists():
            if path.is_symlink() or path.read_bytes() != value:
                raise ValueError("Refusing to overwrite different age tools")
            continue
        with path.open("xb") as output:
            output.write(value)
        path.chmod(0o755 if path.name != "LICENSE" else 0o644)
    return destination / ("age" + suffix)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    print(install(parser.parse_args().destination.resolve()))
