# The kernel installer: the image build fails with a clear message unless the pinned wheel is there and matches.

from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install_kernel.py"
BINDING = Path(__file__).resolve().parent.parent / "config" / "kernel-binding.json"


@pytest.fixture
def installer():
    spec = importlib.util.spec_from_file_location("install_kernel", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def binding_for(tmp_path, wheel: bytes, digest: str | None = None) -> tuple[Path, Path]:
    machine = platform.machine().lower()
    key = f"linux-{'aarch64' if machine in ('aarch64', 'arm64') else 'x86_64'}"
    folder = tmp_path / "artifacts"
    folder.mkdir()
    (folder / "k-0.1.0.whl").write_bytes(wheel)
    manifest = tmp_path / "kernel-binding.json"
    manifest.write_text(json.dumps({"python": {"import": "astral_kernel"}, "artifacts": {
        key: {"wheel": {"file": "k-0.1.0.whl", "sha256": digest or hashlib.sha256(wheel).hexdigest()}}}}))
    return folder, manifest


def test_the_shipped_binding_pins_a_digest_for_a_linux_wheel():
    document = json.loads(BINDING.read_text())
    entry = document["artifacts"]["linux-aarch64"]["wheel"]
    assert entry["file"].endswith("manylinux_2_36_aarch64.whl") and len(entry["sha256"]) == 64
    assert document["python"]["import"] == "astral_kernel"


def test_a_wheel_with_the_pinned_digest_is_installed_and_imported(installer, tmp_path, monkeypatch):
    folder, manifest = binding_for(tmp_path, b"wheel")
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kw: ran.append(command))
    assert installer.main(str(folder), manifest) == 0
    assert ran[0][-1] == str(folder / "k-0.1.0.whl") and "--no-deps" in ran[0] and ran[1][-1] == "import astral_kernel"


def test_a_missing_wheel_fails_the_build_and_says_what_to_do(installer, tmp_path, monkeypatch, capsys):
    folder, manifest = binding_for(tmp_path, b"wheel")
    (folder / "k-0.1.0.whl").unlink()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("nothing may be installed"))
    assert installer.main(str(folder), manifest) == 1
    message = capsys.readouterr().err
    assert "needs the pinned kernel wheel k-0.1.0.whl" in message and "kernel_artifacts" in message


def test_a_wheel_with_another_digest_is_refused(installer, tmp_path, monkeypatch, capsys):
    folder, manifest = binding_for(tmp_path, b"wheel", digest="0" * 64)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("nothing may be installed"))
    assert installer.main(str(folder), manifest) == 1
    assert "pins " + "0" * 64 in capsys.readouterr().err


def test_an_architecture_without_a_pinned_wheel_is_refused(installer, tmp_path, monkeypatch, capsys):
    folder, manifest = binding_for(tmp_path, b"wheel")
    monkeypatch.setattr(installer.platform, "machine", lambda: "riscv64")
    assert installer.main(str(folder), manifest) == 1
    assert "pins no kernel wheel for linux-riscv64" in capsys.readouterr().err
