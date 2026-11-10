# End-to-end tests of the Vessel service in process: A2A streaming, authentication, owner binding and cancellation.

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest
from conftest import APP_KEY, text_reply, tool_reply

from astralvessel.client import VesselClient
from astralvessel.manifest import load_package
from astralvessel.server import Settings, create_app


@pytest.fixture
def vessel(package_dir, model, tmp_path):
    settings = Settings(load_package(package_dir), app_key_sha256=hashlib.sha256(APP_KEY.encode()).hexdigest(),
                        store_path=str(tmp_path / "v.sqlite"), sandbox_url=None, sandbox_token=None,
                        public_url="http://vessel.test", idle_hours=24)
    app = create_app(settings, model_transport=model.transport())
    transport = httpx.ASGITransport(app=app)
    return VesselClient("http://vessel.test", APP_KEY, transport=transport), transport


@pytest.fixture
async def live_vessel(package_dir, model, tmp_path):
    import socket

    import uvicorn
    settings = Settings(load_package(package_dir), app_key_sha256=hashlib.sha256(APP_KEY.encode()).hexdigest(),
                        store_path=str(tmp_path / "live.sqlite"), sandbox_url=None, sandbox_token=None,
                        public_url="http://127.0.0.1", idle_hours=24)
    app = create_app(settings, model_transport=model.transport())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    client = VesselClient(f"http://127.0.0.1:{port}", APP_KEY)
    yield client
    await client.close()
    server.should_exit = True
    await serving


async def collect(client, user, text, conversation=None):
    return [e async for e in client.stream(user, text, conversation)]


async def test_agent_card_is_public_and_advertises_a2a_1_0(vessel):
    client, _ = vessel
    card = await client.card()
    assert card["name"] == "tester" and card["capabilities"]["streaming"] is True
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert "application/vnd.astral.primitives+json" in card["defaultOutputModes"]


async def test_requests_without_the_deployment_key_or_user_are_refused(vessel):
    _, transport = vessel
    async with httpx.AsyncClient(transport=transport, base_url="http://vessel.test") as raw:
        assert (await raw.get("/v1/conversations", headers={"X-Vessel-User": "alice"})).status_code == 401
        bad = {"Authorization": "Bearer wrong", "X-Vessel-User": "alice"}
        assert (await raw.get("/v1/conversations", headers=bad)).status_code == 401
        no_user = {"Authorization": f"Bearer {APP_KEY}"}
        assert (await raw.get("/v1/conversations", headers=no_user)).status_code == 401
        odd_user = {"Authorization": f"Bearer {APP_KEY}", "X-Vessel-User": "a b/../c"}
        assert (await raw.get("/v1/conversations", headers=odd_user)).status_code == 401
        rpc = await raw.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}})
        assert rpc.status_code == 401


async def test_a_turn_streams_events_and_a_rich_answer(vessel, model):
    client, _ = vessel
    model.replies = [tool_reply("present", {"components": [{"type": "metric", "title": "Pages", "value": "1"}]}),
                     text_reply("There is one page.")]
    events = await collect(client, "alice", "How many pages?")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "task" and "turn.started" in kinds and kinds[-1] == "turn.finished"
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["text"] == "There is one page." and answer["components"][0]["value"] == "1"
    finished = events[-1]
    assert finished["status"] == "ok" and finished["state"] == "TASK_STATE_COMPLETED" and finished["steps"] == 2


async def test_conversations_continue_and_stay_with_their_owner(vessel, model):
    client, _ = vessel
    model.replies = [text_reply("first"), text_reply("second")]
    first = await collect(client, "alice", "Remember the word heron.")
    cid = first[0]["conversation_id"]
    await collect(client, "alice", "What was the word?", cid)
    assert any(m["content"] == "Remember the word heron." for m in model.requests[1]["messages"])
    assert (await client.conversation("alice", cid))["turns"][1]["result"]["text"] == "second"
    assert await client.conversation("bob", cid) is None
    assert await client.conversations("bob") == []
    assert not await client.delete("bob", cid)
    hijack = await collect(client, "bob", "Continue alice's chat", cid)
    assert hijack[-1]["state"] == "TASK_STATE_REJECTED"
    assert len(model.requests) == 2
    assert await client.delete("alice", cid)
    assert await client.conversation("alice", cid) is None


async def test_cancel_task_stops_a_running_turn_and_is_owner_scoped(live_vessel, model):
    client = live_vessel
    model.replies = [tool_reply("bash", {"command": "sleep 10"}), text_reply("never")]
    events: list[dict] = []

    async def consume():
        async for e in client.stream("alice", "Run something slow"):
            events.append(e)

    runner = asyncio.create_task(consume())
    for _ in range(200):
        if any(e["type"] == "tool.started" for e in events):
            break
        await asyncio.sleep(0.02)
    task_id = events[0]["task_id"]
    assert "error" in await client.cancel("bob", task_id)
    result = await client.cancel("alice", task_id)
    assert result["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    await asyncio.wait_for(runner, 15)
    finished = [e for e in events if e["type"] == "turn.finished"]
    assert finished and finished[-1]["status"] == "cancelled"


async def test_model_failure_surfaces_a_generic_error(vessel, model):
    client, _ = vessel
    model.replies = [500, 500, 500, 500]
    events = await collect(client, "alice", "Hello?")
    finished = events[-1]
    assert finished["state"] == "TASK_STATE_FAILED" and finished["status"] == "model_error"
    assert "secret-dsn" not in str(events)


async def test_empty_message_is_rejected(vessel):
    client, _ = vessel
    events = await collect(client, "alice", "   ")
    assert events[-1]["state"] == "TASK_STATE_REJECTED"
