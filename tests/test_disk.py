# Disk limits: the deployment settings, the per-file limit on confined calls, and the turn and user limits.

from __future__ import annotations

import hashlib

import httpx
import pytest
from test_deploy import deployment_text, landlock

from astralvessel import sandbox
from astralvessel.compose import generate
from astralvessel.deployment import load_deployment, size_bytes
from astralvessel.manifest import load_package

TOKEN = "t" * 16
AUTH = {"Authorization": "Bearer " + TOKEN}
ALICE, BOB = "a" * 32, "b" * 32


def test_sizes_and_defaults_follow_scratch(package_dir, tmp_path):
    assert (size_bytes("512k"), size_bytes("64m"), size_bytes("2g"), size_bytes("100")) == (
        512 * 1024, 64 * 1024 ** 2, 2 * 1024 ** 3, 100)
    with pytest.raises(ValueError):
        size_bytes("lots")
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(package_dir, mounts='scratch = "100m"'))
    env = generate(load_deployment(path), repo_root=None)["services"]["sandbox-guide"]["environment"]
    assert (env["VESSEL_TURN_DISK"], env["VESSEL_USER_DISK"]) == (str(50 * 1024 ** 2), str(100 * 1024 ** 2))
    path.write_text(deployment_text(package_dir, mounts='scratch = "100m"\nturn_disk = "10m"\nuser_disk = "30m"'))
    env = generate(load_deployment(path), repo_root=None)["services"]["sandbox-guide"]["environment"]
    assert (env["VESSEL_TURN_DISK"], env["VESSEL_USER_DISK"]) == (str(10 * 1024 ** 2), str(30 * 1024 ** 2))
    for bad in ('turn_disk = "40m"\nuser_disk = "20m"', 'user_disk = "200m"', 'turn_disk = "0"'):
        path.write_text(deployment_text(package_dir, mounts=f'scratch = "100m"\n{bad}'))
        with pytest.raises(ValueError, match="turn_disk"):
            load_deployment(path)


async def test_the_vessel_sends_a_digest_of_the_user_never_the_name():
    sent = []

    async def handler(request):
        sent.append(request.read())
        return httpx.Response(200, json={"output": "ok", "error": False})

    runner = sandbox.RemoteRunner("http://sb", TOKEN, 10)
    runner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sb")
    await runner.run("bash", {"command": "true"}, "c" * 32, owner="alice@example.org")
    body = sent[0].decode()
    assert "alice" not in body and hashlib.sha256(b"alice@example.org").hexdigest()[:32] in body


@landlock
async def test_no_single_file_may_outgrow_the_turn_limit(package_dir, tmp_path):
    package = load_package(package_dir)
    scratch = tmp_path / "calls"
    scratch.mkdir()
    confinement = sandbox.confinement_for(package, str(scratch))
    runner = sandbox.LocalRunner(package, sandbox.replace(confinement, max_file_bytes=100_000))
    command = "head -c 200000 /dev/zero > $TMPDIR/big && echo wrote; wc -c < $TMPDIR/big"
    out, _ = await runner.run("bash", {"command": command}, "c" * 32)
    assert "wrote" not in out and "100000" in out
    await runner.end_turn("c" * 32)


def disk_app(package_dir, tmp_path, monkeypatch, turn, user):
    monkeypatch.setenv("VESSEL_TURN_DISK", str(turn))
    monkeypatch.setenv("VESSEL_USER_DISK", str(user))
    scratch = tmp_path / "calls"
    app = sandbox.sandbox_app(str(package_dir), token=TOKEN, confine=True, scratch_root=str(scratch))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb"), scratch


def write(kb, key, owner, name="f"):
    return {"name": "bash", "args": {"command": f"head -c {kb * 1024} /dev/urandom > $TMPDIR/{name}; echo ok"},
            "workspace": key, "owner": owner}


@landlock
async def test_a_turn_over_its_disk_limit_is_stopped_and_its_workspace_emptied(package_dir, tmp_path, monkeypatch):
    client, scratch = disk_app(package_dir, tmp_path, monkeypatch, turn=1024 * 1024, user=4 * 1024 * 1024)
    async with client:
        first = (await client.post("/run", json=write(600, "c" * 32, ALICE, "one"), headers=AUTH)).json()
        assert not first["error"]
        second = (await client.post("/run", json=write(600, "c" * 32, ALICE, "two"), headers=AUTH)).json()
        assert second["error"] and "1 MB disk limit" in second["output"]
        assert not (scratch / f"turn-{'c' * 32}").exists()
        again = (await client.post("/run", json=write(100, "c" * 32, ALICE), headers=AUTH)).json()
        assert not again["error"]  # the turn goes on with an empty workspace


@landlock
async def test_a_users_turns_together_are_held_to_the_user_limit(package_dir, tmp_path, monkeypatch):
    client, _ = disk_app(package_dir, tmp_path, monkeypatch, turn=1024 * 1024, user=1024 * 1024)
    async with client:
        assert not (await client.post("/run", json=write(600, "c" * 32, ALICE), headers=AUTH)).json()["error"]
        other = (await client.post("/run", json=write(600, "e" * 32, BOB), headers=AUTH)).json()
        assert not other["error"]  # another user's files do not count against Alice
        second = (await client.post("/run", json=write(600, "d" * 32, ALICE), headers=AUTH)).json()
        assert second["error"] and "across turns" in second["output"]


async def test_a_turn_stays_with_the_user_who_started_it(package_dir):
    app = sandbox.sandbox_app(str(package_dir), token=TOKEN, confine=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb") as client:
        call = {"name": "bash", "args": {"command": "true"}, "workspace": "c" * 32}
        assert (await client.post("/run", json={**call, "owner": ALICE}, headers=AUTH)).status_code == 200
        assert (await client.post("/run", json={**call, "owner": BOB}, headers=AUTH)).status_code == 403
        assert (await client.post("/run", json={**call, "owner": "../x"}, headers=AUTH)).status_code == 400
