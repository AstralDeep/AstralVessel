"""Installs the pinned kernel Python wheel into the image after checking its digest against kernel-binding.json.
The Dockerfile runs it with the kernel-artifacts build context; without it the executor refuses tickets."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "kernel-binding.json"
ARCHITECTURES = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}


def main(directory: str, allow_missing: bool) -> int:
    manifest = json.loads(MANIFEST.read_text())
    key = f"linux-{ARCHITECTURES.get(platform.machine().lower(), platform.machine())}"
    entry = (manifest["artifacts"].get(key) or {}).get("wheel")
    wheel = Path(directory) / entry["file"] if entry else None
    if wheel is None or not wheel.is_file():
        if allow_missing:
            print(f"no kernel wheel for {key} in {directory}: this image refuses every ticket")
            return 0
        print(f"no kernel wheel for {key} in {directory}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        print(f"digest mismatch for {wheel.name}", file=sys.stderr)
        return 1
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps", str(wheel)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--allow-missing" in sys.argv[2:]))
