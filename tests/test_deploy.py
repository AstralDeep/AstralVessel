# Tests for deployment.toml, the generated Compose file, the transcript checks, and per-call Landlock confinement.

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from importlib.metadata import version

import pytest

from astralvessel import confine
from astralvessel.compose import generate, write
from astralvessel.deployment import egress_token, load_deployment, sandbox_token
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


def test_an_edited_manifest_tool_or_deployment_recreates_what_loads_it(data_package, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(data_package))

    def services():
        spec = generate(load_deployment(path), repo_root=None)["services"]
        sandbox = spec["sandbox-guide"]["environment"]["VESSEL_PROFILE_HASH"]
        return spec["vessel"]["labels"]["astralvessel.inputs"], sandbox

    first = services()
    # The vessel loads the package from its own mount; the same files must give the same fingerprint there.
    mount = tmp_path / "mount"
    shutil.copytree(data_package, mount / "guide")
    assert load_deployment(path, packages_root=str(mount)).agents["guide"].fingerprint == first[1]
    (data_package / "prompt.md").write_text("A new briefing, read on the next turn.")
    (data_package / "notes.md~").write_text("an editor backup")
    assert services() == first
    tool = data_package / "tools" / "echo.toml"
    tool.write_text(tool.read_text().replace("Echo the input", "Echo the input back"))
    edited = services()
    assert edited[0] != first[0] and edited[1] != first[1]
    (data_package / "tools" / "rank.toml").write_text(tool.read_text().replace('name = "echo"', 'name = "rank"'))
    manifest = data_package / "vessel.toml"
    manifest.write_text(manifest.read_text().replace('"tools/echo.toml"]', '"tools/echo.toml", "tools/rank.toml"]'))
    added = services()
    assert added[0] != edited[0] and added[1] != edited[1]
    path.write_text(path.read_text() + "\n# comment\n")
    redeployed = services()
    assert redeployed[0] != added[0] and redeployed[1] == added[1]


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
    search = spec["services"]["search"]
    assert search["networks"] == ["egress"] and "ports" not in search and search["read_only"]
    assert search["cap_drop"] == ["ALL"] and vessel["environment"]["VESSEL_SEARCH_URL"] == "http://search:8080"
    assert "search" in vessel["depends_on"]
    released = version("astralvessel")
    assert vessel["image"] == f"ghcr.io/astraldeep/astralvessel:{released}" and "build" not in vessel
    assert spec["services"]["egress"]["image"] == f"ghcr.io/astraldeep/astralvessel-egress:{released}"
    built = generate(deployment, repo_root=tmp_path)["services"]
    assert built["vessel"]["image"] == "astralvessel:local" and built["vessel"]["build"]["context"] == str(tmp_path)
    assert built["egress"]["build"]["context"] == str(tmp_path / "egress")
    assert "VESSEL_MODEL_KEY" not in vessel["environment"] and vessel["env_file"] == [".generated/vessel.env"]
    assert spec["services"]["egress"]["env_file"] == [".generated/egress.env"]
    assert search["env_file"] == [".generated/search.env"]
    shared = 'mounts = [{ target = "/data", source = "volume:shared-corpus" }]'
    path.write_text(deployment_text(data_package, mounts=shared))
    named = generate(load_deployment(path), repo_root=None)
    assert named["services"]["sandbox-guide"]["volumes"][1] == "shared-corpus:/data:ro"
    assert named["volumes"]["shared-corpus"] == {"external": True}
    master = "master-secret-123456"
    out = tmp_path / "compose.generated.yml"
    write(deployment, out, repo_root=None, master_token=master)
    assert master not in out.read_text()
    secret = tmp_path / ".generated" / "sandbox-guide.env"
    assert stat.S_IMODE(os.stat(secret).st_mode) == 0o600
    assert sandbox_token(master, "guide") in secret.read_text()
    token = egress_token(master)
    assert token not in (sandbox_token(master, "guide"), sandbox_token(master, "egress:model")) and token != master
    for name, line in (("egress", "EGRESS_TOKEN"), ("vessel", "VESSEL_MODEL_KEY")):
        held = tmp_path / ".generated" / f"{name}.env"
        assert stat.S_IMODE(os.stat(held).st_mode) == 0o600 and held.read_text() == f"{line}={token}\n"
    assert token not in out.read_text()
    key = tmp_path / ".generated" / "search.env"
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600 and key.read_text().startswith("SEARXNG_SECRET=")
    first = key.read_text()
    write(deployment, out, repo_root=None, master_token=master)
    assert key.read_text() == first and first.split("=")[1].strip() not in out.read_text()
    assert "http://egress:3128" in (tmp_path / ".generated" / "searxng.yml").read_text()
    path.write_text('[search]\nprovider = "duckduckgo"\n' + deployment_text(data_package))
    plain = generate(load_deployment(path), repo_root=None)
    assert "search" not in plain["services"] and "VESSEL_SEARCH_URL" not in plain["services"]["vessel"]["environment"]


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


@landlock
async def test_calls_in_one_turn_share_a_workspace_that_other_turns_cannot_see(package_dir, tmp_path):
    scratch = tmp_path / "calls"
    scratch.mkdir()
    from astralvessel.manifest import load_package
    package = load_package(package_dir)
    runner = LocalRunner(package, confinement_for(package, str(scratch)))
    turn_a, turn_b = "a" * 32, "b" * 32
    _, failed = await runner.run("bash", {"command": "echo kept > $TMPDIR/table.rds"}, turn_a)
    assert not failed
    later, failed = await runner.run("bash", {"command": "cat $TMPDIR/table.rds"}, turn_a)
    assert later.startswith("kept") and not failed
    read, failed = await runner.run("read", {"path": str(scratch / f"turn-{turn_a}" / "table.rds")}, turn_a)
    assert "kept" in read and not failed
    other, _ = await runner.run("bash", {"command": f"cat {scratch}/turn-{turn_a}/table.rds; ls {scratch}"}, turn_b)
    assert "kept" not in other and other.count("Permission denied") >= 2
    other, failed = await runner.run("read", {"path": str(scratch / f"turn-{turn_a}" / "table.rds")}, turn_b)
    assert failed and "outside" in other
    unkeyed, _ = await runner.run("bash", {"command": f"cat {scratch}/turn-{turn_a}/table.rds"})
    assert "kept" not in unkeyed
    await runner.end_turn(turn_a)
    assert sorted(p.name for p in scratch.iterdir()) == [f"turn-{turn_b}"]
    await runner.end_turn(turn_b)
    assert list(scratch.iterdir()) == []
    _, failed = await runner.run("bash", {"command": "true"}, "../escape" + "0" * 23)
    assert failed and list(scratch.iterdir()) == []


def test_stale_workspaces_are_swept(tmp_path):
    from astralvessel.tools import Confinement, sweep_workspaces
    confinement = Confinement(readable=(), scratch_root=str(tmp_path))
    old, fresh, other = tmp_path / ("turn-" + "a" * 32), tmp_path / ("turn-" + "b" * 32), tmp_path / "call-x"
    for path in (old, fresh, other):
        path.mkdir()
    os.utime(old, (4102444800, 4102444800))  # a call can set its workspace's times; the sweep ignores them
    sweep_workspaces(confinement, {"b" * 32})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["call-x", fresh.name]


@pytest.mark.parametrize("tool", ["read", "list", "glob"])
def test_in_server_tools_check_where_the_opened_path_really_points(tmp_path, tool):
    # A call can swap a link after the path check; the tools check the opened descriptor too.
    from astralvessel.tools import Confinement, _glob, _list, _read
    data, outside = tmp_path / "data", tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (data / "link").symlink_to(outside / "secret.txt" if tool == "read" else outside)
    confinement = Confinement(readable=(str(data),), scratch_root=str(tmp_path / "calls"))
    call = {"read": lambda: _read({"path": "link"}, str(data), confinement),
            "list": lambda: _list({"path": "link"}, str(data), confinement),
            "glob": lambda: _glob({"pattern": "*", "path": "link"}, str(data), 5, confinement)}[tool]
    with pytest.raises(PermissionError):
        asyncio.run(call())
