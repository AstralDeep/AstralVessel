"""Installs the pinned kernel Python wheel into the image after checking its digest against kernel-binding.json.
The Dockerfile runs it with the kernel-artifacts build context and fails the build if the wheel is absent or differs."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "kernel-binding.json"
ARCHITECTURES = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}
HINT = "set [executor] kernel_artifacts in deployment.toml, or pass --build-context kernel-artifacts=<folder>"


def main(directory: str, binding: Path = MANIFEST) -> int:
    manifest = json.loads(binding.read_text())
    machine = platform.machine().lower()
    key = f"linux-{ARCHITECTURES.get(machine, machine)}"
    entry = (manifest["artifacts"].get(key) or {}).get("wheel")
    if entry is None:
        print(f"kernel-binding.json pins no kernel wheel for {key}, so this image cannot be built", file=sys.stderr)
        return 1
    wheel = Path(directory) / entry["file"]
    if not wheel.is_file():
        print(f"the executor image needs the pinned kernel wheel {entry['file']} in {directory}; {HINT}",
              file=sys.stderr)
        return 1
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        print(f"{wheel.name} has sha256 {digest}, but kernel-binding.json pins {entry['sha256']}", file=sys.stderr)
        return 1
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps", str(wheel)], check=True)
    subprocess.run([sys.executable, "-c", f"import {manifest['python']['import']}"], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
