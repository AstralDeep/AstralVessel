# Tests for deployment.toml, the generated Compose file, the transcript checks, and per-call Landlock confinement.

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from astralvessel import confine
from astralvessel.compose import generate, write
from astralvessel.deployment import load_deployment, sandbox_token
from astralvessel.sandbox import LocalRunner, confinement_for
from astralvessel.transcript import TranscriptError, parse_history

landlock = pytest.mark.skipif(confine.abi_version() < confine.REQUIRED_ABI, reason="kernel lacks Landlock ABI 6")


WEB_APP = '[[apps]]\nname = "web"\nkey_sha256 = "env:WEB_KEY_SHA256"\nagents = ["guide"]'


def deployment_text(package, mounts='mounts = [{ target = "/data", source = "env:DATA_DIR" }]', apps=WEB_APP) -> str:
    return f"""
[[agents]]
id = "guide"
package = "{package}"
[agents.sandbox]
{mounts}

{apps}
"""


@pytest.fixture
def data_package(package_dir):
    manifest = package_dir / "vessel.toml"
    manifest.write_text(manifest.read_text().replace(
        "[sandbox]\n", '[sandbox]\nmounts = [{ source = "data", target = "/data", read_only = true }]\n'))
    return package_dir


def test_grants_must_match_what_the_agent_declares(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package, mounts=""))
    with pytest.raises(ValueError, match="does not grant"):
        load_deployment(path)
    path.write_text(deployment_text(data_package, mounts=(
        'mounts = [{ target = "/data", source = "/v" }, { target = "/etc/x", source = "/x" }]')))
    with pytest.raises(ValueError, match="does not declare"):
        load_deployment(path)
    path.write_text(deployment_text(data_package, mounts=(
        'mounts = [{ target = "/data", source = "/v", read_only = false }]')))
    with pytest.raises(ValueError, match="writable mounts"):
        load_deployment(path)
    path.write_text(deployment_text(data_package, apps='[[apps]]\nname = "web"\nkey_sha256 = "x"\nagents = ["nope"]'))
    with pytest.raises(ValueError, match="unknown agents"):
        load_deployment(path)


def test_the_fingerprint_follows_the_sandbox_profile(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))
    before = load_deployment(path).agents["guide"].fingerprint
    path.write_text(deployment_text(data_package).replace('source = "env:DATA_DIR"', 'source = "/other"'))
    assert load_deployment(path).agents["guide"].fingerprint != before
    assert sandbox_token("m" * 16, "a") != sandbox_token("m" * 16, "b")


def test_compose_isolates_each_sandbox_and_keeps_secrets_out(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))
    deployment = load_deployment(path)
    spec = generate(deployment, repo_root=None)
    sandbox = spec["services"]["sandbox-guide"]
    assert sandbox["networks"] == ["tools-guide"] and spec["networks"]["tools-guide"]["internal"] is True
    assert sandbox["read_only"] and sandbox["cap_drop"] == ["ALL"]
    assert sandbox["environment"]["VESSEL_PROFILE_HASH"] == deployment.agents["guide"].fingerprint
    assert "${DATA_DIR:?" in sandbox["volumes"][1] and sandbox["volumes"][1].endswith(":/data:ro")
    vessel = spec["services"]["vessel"]
    assert vessel["environment"]["WEB_KEY_SHA256"].startswith("${WEB_KEY_SHA256:?")
    assert "tools-guide" in vessel["networks"] and "outside" not in vessel["networks"]
    assert spec["services"]["egress"]["networks"] == ["egress", "outside"]
    master = "master-secret-123456"
    out = tmp_path / "compose.generated.yml"
    write(deployment, out, repo_root=None, master_token=master)
    assert master not in out.read_text()
    secret = tmp_path / ".generated" / "sandbox-guide.env"
    assert stat.S_IMODE(os.stat(secret).st_mode) == 0o600
    assert sandbox_token(master, "guide") in secret.read_text()


def test_history_must_be_well_formed_and_never_system():
    ok = parse_history({"version": 1, "messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "assistant", "content": "a", "reasoning_content": "why"}]})
    assert len(ok) == 4
    bad = [
        {"version": 2, "messages": []},
        {"version": 1, "messages": [{"role": "system", "content": "x"}]},
        {"version": 1, "messages": [{"role": "user", "content": "x", "name": "admin"}]},
        {"version": 1, "messages": [{"role": "tool", "tool_call_id": "c9", "content": "x"}]},
        {"version": 1, "messages": [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": {}}}]}]},
        {"version": 1, "messages": [{"role": "assistant", "content": None, "tool_calls": 1.0}]},
        {"version": 1, "messages": [{"role": "assistant", "content": None, "tool_calls": [{"id": ["x"]}]}]},
    ]
    for payload in bad:
        with pytest.raises(TranscriptError):
            parse_history(payload)


@landlock
async def test_concurrent_calls_cannot_see_each_other(package_dir, tmp_path):
    scratch = tmp_path / "calls"
    scratch.mkdir()
    from astralvessel.manifest import load_package
    package = load_package(package_dir)
    runner = LocalRunner(package, confinement_for(package, str(scratch)))
    holder = asyncio.create_task(runner.run("bash", {"command": "echo private > $TMPDIR/note; sleep 3"}))
    await asyncio.sleep(0.5)
    peek, _ = await runner.run("bash", {"command": f"cat {scratch}/*/note; ls {scratch}; ls /proc | head -1"})
    assert "private" not in peek and peek.count("Permission denied") >= 2
    own, failed = await runner.run("bash", {"command": "echo mine > $TMPDIR/x && cat $TMPDIR/x"})
    assert own.startswith("mine") and not failed
    data, _ = await runner.run("bash", {"command": "cat notes/ada-lovelace.md"})
    assert "Analytical engine" in data
    home, _ = await runner.run("bash", {"command": f"cat {os.path.expanduser('~')}/.bashrc"})
    assert "Permission denied" in home or "No such file" in home
    tcp, failed = await runner.run("bash", {"command": "python3 -c \"import socket; socket.create_connection(("
                                                       "'127.0.0.1', 22), 2)\""})
    assert failed and "PermissionError" in tcp
    outside, failed = await runner.run("read", {"path": "/etc/hostname"})
    assert failed and "outside" in outside
    climb, failed = await runner.run("glob", {"pattern": "../*"})
    assert failed and ".." in climb
    sock, failed = await runner.run("bash", {"command": "python3 -c \"import socket,os; socket.socket("
                                                        "socket.AF_UNIX).bind(os.environ['TMPDIR'] + '/s')\""})
    assert failed and "PermissionError" in sock
    shm, failed = await runner.run("bash", {"command": "python3 -c \"import ctypes; l = ctypes.CDLL(None, "
                                                       "use_errno=True); print(l.shmget(0, 4096, 0o1600), "
                                                       "ctypes.get_errno())\""})
    assert shm.split()[:2] == ["-1", "1"]
    udp, failed = await runner.run("bash", {"command": "python3 -c \"import socket; socket.socket(socket.AF_INET, "
                                                       "socket.SOCK_DGRAM)\""})
    assert failed and "PermissionError" in udp
    pair, failed = await runner.run("bash", {"command": "python3 -c \"import socket; socket.socketpair(); "
                                                        "print('ok')\""})
    assert pair.startswith("ok") and not failed
    await holder
    assert list(scratch.iterdir()) == []
