# Tests for service tools (application endpoints the vessel calls), image input, and per-agent sandbox images.

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
from conftest import APP_KEY, text_reply, tool_reply

from astralvessel.client import VesselClient
from astralvessel.compose import generate
from astralvessel.deployment import load_deployment
from astralvessel.server import Settings, app_keys, create_app
from astralvessel.transcript import TranscriptError, parse_history

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def service_package(package_dir):
    (package_dir / "tools" / "lookup.toml").write_text(
        'name = "lookup"\ndescription = "Look a record up in the app"\nservice = "records"\npath = "/tools/lookup"\n'
        '[parameters]\ntype = "object"\n[parameters.properties.q]\ntype = "string"\n')
    manifest = package_dir / "vessel.toml"
    text = manifest.read_text().replace('declared = ["tools/echo.toml"]',
                                        'declared = ["tools/echo.toml", "tools/lookup.toml"]')
    manifest.write_text(text.replace('max_steps = 4', 'max_steps = 4\nimages = true'))
    return package_dir


def deployment(path, package, services='[agents.services.records]\nurl = "http://records.test/api"\n'
               'headers = { "X-Token" = "env:RECORDS_TOKEN" }\n'):
    path.write_text(f"""
[vessel]
public_url = "http://vessel.test"
[egress]
upstream_url = "https://models.test/v1"
[[agents]]
id = "tester"
package = "{package}"
{services}
[[apps]]
name = "web"
key_sha256 = "{hashlib.sha256(APP_KEY.encode()).hexdigest()}"
agents = ["tester"]
""")
    return path


@pytest.fixture
def served(service_package, model, tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDS_TOKEN", "s3cret-token")
    calls: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return replies.pop(0) if replies else httpx.Response(200, json={"found": "Ada Lovelace"})

    dep = load_deployment(deployment(tmp_path / "deployment.toml", service_package))
    settings = Settings(dep, task_db=str(tmp_path / "t.sqlite"), app_keys=app_keys(dep), allow_in_process=True)
    app = create_app(settings, model_transport=model.transport(), service_transport=httpx.MockTransport(handler))
    return VesselClient("http://vessel.test", APP_KEY, transport=httpx.ASGITransport(app=app)), calls, replies


async def run(client, text, **kw):
    return [e async for e in client.stream("tester", "alice", text, **kw)]


async def test_a_service_tool_is_called_by_the_vessel_with_its_credentials(served, model):
    client, calls, _ = served
    model.replies = [tool_reply("lookup", {"q": "ada"}), text_reply("Found her.")]
    events = await run(client, "Who is ada?")
    request = calls[0]
    assert str(request.url) == "http://records.test/api/tools/lookup" and json.loads(request.content) == {"q": "ada"}
    assert request.headers["x-token"] == "s3cret-token" and request.headers["x-vessel-owner"] == "web/alice"
    assert request.headers["x-vessel-tool"] == "lookup"
    finished = next(e for e in events if e["type"] == "tool.finished")
    assert "Ada Lovelace" in finished["output"] and not finished["error"]
    assert "s3cret-token" not in json.dumps(model.requests)


async def test_service_errors_reach_the_model_without_internals(served, model):
    client, _, replies = served
    replies += [httpx.Response(404, text="no record matches 'zed'"), httpx.Response(500, text="Traceback secret-dsn")]
    model.replies = [tool_reply("lookup", {"q": "zed"}), tool_reply("lookup", {"q": "x"}, "call_2"), text_reply("ok")]
    events = await run(client, "Find zed")
    outputs = [e for e in events if e["type"] == "tool.finished"]
    assert outputs[0]["error"] and "no record matches" in outputs[0]["output"]
    assert outputs[1]["error"] and "secret-dsn" not in outputs[1]["output"] and "HTTP 500" in outputs[1]["output"]


def test_service_grants_must_match_the_tools(service_package, tmp_path):
    with pytest.raises(ValueError, match="missing records"):
        load_deployment(deployment(tmp_path / "d.toml", service_package, services=""))
    with pytest.raises(ValueError, match="needs exactly one of command or service"):
        (service_package / "tools" / "lookup.toml").write_text(
            'name = "lookup"\ndescription = "x"\nservice = "records"\npath = "/x"\ncommand = ["true"]\n')
        load_deployment(deployment(tmp_path / "d.toml", service_package))


async def test_images_reach_the_model_as_content_parts(served, model):
    client, _, _ = served
    model.replies = [text_reply("A blank image.")]
    events = await run(client, "What is this?", images=[("image/png", PNG)])
    assert events[-1]["state"] == "TASK_STATE_COMPLETED"
    user = model.requests[0]["messages"][-1]
    assert user["content"][0] == {"type": "text", "text": "What is this?"}
    assert user["content"][1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()


async def test_bad_or_unwanted_images_are_refused(served, package_dir, model, tmp_path):
    client, _, _ = served
    lying = await run(client, "x", images=[("image/png", b"GIF89a" + b"\x00" * 10)])
    pdf = await run(client, "x", images=[("application/pdf", b"%PDF-1.4")])
    many = await run(client, "x", images=[("image/png", PNG)] * 9)
    assert all(e[-1]["state"] == "TASK_STATE_REJECTED" for e in (lying, pdf, many))
    assert model.requests == []


PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


def history_with(image: dict) -> dict:
    return {"version": 1, "messages": [{"role": "user", "content": [{"type": "text", "text": "q"}, image]},
                                       {"role": "assistant", "content": "a"}]}


def test_history_images_get_the_same_checks_as_attachments():
    assert len(parse_history(history_with({"type": "image_url", "image_url": {"url": PNG_URL}}))) == 2
    bad = [{"url": "https://example.com/x.png"}, {"url": "data:image/png;base64,NOT_BASE64!!"},
           {"url": "data:image/png;base64," + base64.b64encode(b"GIF89a....").decode()},
           {"url": PNG_URL, "detail": {"evil": [1, 2]}}]
    for image in bad:
        with pytest.raises(TranscriptError):
            parse_history(history_with({"type": "image_url", "image_url": image}))
    many = {"version": 1, "messages": [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG_URL}}] * 33},
        {"role": "assistant", "content": "a"}]}
    with pytest.raises(TranscriptError, match="at most"):
        parse_history(many)


async def test_history_images_need_an_image_agent_and_deep_nesting_is_refused(package_dir, model, tmp_path):
    dep = load_deployment(deployment(tmp_path / "d.toml", package_dir, services=""))
    settings = Settings(dep, task_db=str(tmp_path / "t.sqlite"), app_keys=app_keys(dep), allow_in_process=True)
    client = VesselClient("http://vessel.test", APP_KEY, transport=httpx.ASGITransport(
        app=create_app(settings, model_transport=model.transport())))
    history = history_with({"type": "image_url", "image_url": {"url": PNG_URL}})["messages"]
    events = [e async for e in client.stream("tester", "u", "hi", history=history)]
    assert events[-1]["state"] == "TASK_STATE_REJECTED" and model.requests == []
    nested = "[" * 100_000 + "]" * 100_000
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "messageId": "m1", "role": "ROLE_USER", "parts": [
            {"text": "hi"}, {"raw": base64.b64encode(nested.encode()).decode(),
                             "mediaType": "application/vnd.astralvessel.transcript+json"}]}}}
    headers = {"Authorization": f"Bearer {APP_KEY}", "X-Vessel-User": "u", "A2A-Version": "1.0"}
    reply = (await client._client.post("/agents/tester", headers=headers, json=body)).json()
    assert reply["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert "recursion" not in json.dumps(reply)


def test_service_grants_refuse_vessel_headers_and_url_queries(service_package, tmp_path):
    for services in ('[agents.services.records]\nurl = "http://r.test"\nheaders = { "x-vessel-owner" = "spoof" }\n',
                     '[agents.services.records]\nurl = "http://r.test/api?k=1"\n'):
        with pytest.raises(ValueError):
            load_deployment(deployment(tmp_path / "d.toml", service_package, services=services))


async def test_echoed_credentials_are_redacted_and_transcripts_are_not_stored(served, model, tmp_path):
    client, _, replies = served
    replies.append(httpx.Response(401, text="bad token s3cret-token for you"))
    model.replies = [tool_reply("lookup", {"q": "x"}), text_reply("done")]
    events = await run(client, "go")
    assert "s3cret-token" not in json.dumps(events) and "[redacted]" in json.dumps(events)
    import sqlite3
    rows = sqlite3.connect(tmp_path / "t.sqlite").execute("select artifacts from tasks_tester").fetchall()
    assert rows and all("transcript" not in (r[0] or "") for r in rows)


def test_an_agent_can_ship_its_own_sandbox_image(package_dir, tmp_path):
    (package_dir / "sandbox.Dockerfile").write_text("FROM astralvessel\nRUN true\n")
    manifest = package_dir / "vessel.toml"
    manifest.write_text(manifest.read_text().replace("[sandbox]\n", '[sandbox]\ndockerfile = "sandbox.Dockerfile"\n'))
    dep = load_deployment(deployment(tmp_path / "d.toml", package_dir, services=""))
    before = dep.agents["tester"].fingerprint
    build = generate(dep, repo_root=tmp_path)["services"]["sandbox-tester"]["build"]
    assert build["dockerfile"] == "sandbox.Dockerfile"
    assert build["additional_contexts"] == {"astralvessel": "service:vessel"}
    (package_dir / "sandbox.Dockerfile").write_text("FROM astralvessel\nCOPY tools/ /opt/tools/\n")
    copied = load_deployment(tmp_path / "d.toml").agents["tester"].fingerprint
    assert copied != before
    (package_dir / "tools" / "echo.toml").write_text((package_dir / "tools" / "echo.toml").read_text() + "\n")
    assert load_deployment(tmp_path / "d.toml").agents["tester"].fingerprint != copied
    (package_dir / "vessel.toml").write_text(manifest.read_text().replace('"sandbox.Dockerfile"', '"../x.Dockerfile"'))
    with pytest.raises(ValueError, match="inside the package"):
        load_deployment(tmp_path / "d.toml")


async def test_streamed_pieces_are_coalesced_without_reordering():
    from astralvessel.server import DeltaCoalescer
    sent = []

    async def send(item):
        sent.append(item)
    stream = DeltaCoalescer(send)
    for word in ("a", "b", "c"):
        await stream.add({"type": "text.delta", "step": 1, "content": word})
    await stream.add({"type": "tool.started", "call_id": "x"})
    await stream.add({"type": "reasoning.delta", "step": 2, "content": "r"})
    await stream.add({"type": "text.delta", "step": 2, "content": "d" * 600})
    await stream.flush()
    assert [(e["type"], e.get("content")) for e in sent] == [
        ("text.delta", "abc"), ("tool.started", None), ("reasoning.delta", "r"), ("text.delta", "d" * 600)]


async def test_task_store_failures_never_carry_database_text(tmp_path, monkeypatch):
    from a2a.server.tasks.database_task_store import DatabaseTaskStore
    from a2a.types import Task, TaskState, TaskStatus
    from sqlalchemy.ext.asyncio import create_async_engine

    from astralvessel.server import TaskStoreError, VesselTaskStore

    async def broken(*_args, **_kw):
        raise RuntimeError("(sqlite3.OperationalError) database is locked [SQL: UPDATE secret_table]")
    monkeypatch.setattr(DatabaseTaskStore, "save", broken)
    monkeypatch.setattr(DatabaseTaskStore, "get", broken)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite'}")
    store = VesselTaskStore(engine, table_name="tasks_x", owner_resolver=lambda c: "o")
    with pytest.raises(TaskStoreError) as raised:
        await store.save(Task(id="t", context_id="c", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED)), None)
    assert "SQL" not in str(raised.value)
    with pytest.raises(TaskStoreError):
        await store.get("t", None)
    await engine.dispose()


async def test_briefings_reload_and_one_package_serves_two_agents(package_dir, model, tmp_path):
    (package_dir / "extra.md").write_text("Also follow the extra protocol.")
    path = tmp_path / "d.toml"
    deployment(path, package_dir, services="")
    path.write_text(path.read_text().replace('agents = ["tester"]', 'agents = ["tester", "tester-extra"]') + f"""
[[agents]]
id = "tester-extra"
package = "{package_dir}"
prompt_append = ["extra.md"]
""")
    dep = load_deployment(path)
    settings = Settings(dep, task_db=str(tmp_path / "t.sqlite"), app_keys=app_keys(dep), allow_in_process=True)
    client = VesselClient("http://vessel.test", APP_KEY, transport=httpx.ASGITransport(
        app=create_app(settings, model_transport=model.transport())))
    model.replies = [text_reply("a"), text_reply("b"), text_reply("c")]
    [e async for e in client.stream("tester", "u", "hi")]
    (package_dir / "prompt.md").write_text("You are an edited test agent.")
    [e async for e in client.stream("tester", "u", "hi")]
    [e async for e in client.stream("tester-extra", "u", "hi")]
    systems = [r["messages"][0]["content"] for r in model.requests]
    assert systems[0].startswith("You are a test agent.") and systems[1].startswith("You are an edited test agent.")
    assert "extra protocol" in systems[2] and "extra protocol" not in systems[1]
    path.write_text(path.read_text().replace('["extra.md"]', '["../escape.md"]'))
    with pytest.raises(ValueError, match="inside the package"):
        load_deployment(path)
