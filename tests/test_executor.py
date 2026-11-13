# The executor: a call runs only with a verified ticket whose action matches the call and the executor's own tool
# definition, and every refusal carries a dotted code and runs nothing.

from __future__ import annotations

import json

import httpx
import pytest
from test_deploy import deployment_text

from astralvessel import sandbox
from astralvessel.deployment import load_deployment
from astralvessel.executor import (
    Replays,
    args_digest,
    catalog,
    definition_hash,
    effect_kind,
    executor_app,
    executor_token,
)
from astralvessel.mesh import Refused, Verified

TOKEN = "e" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SANDBOX_TOKEN = "s" * 16
KEY = "d" * 32


class FakeVerifier:
    def __init__(self):
        self.calls, self.fail, self.mesh_state = [], None, "enrolled"
        self.started = self.stopped = False
        self.digest = None
        self.who = {"mesh": "mesh-1", "device": "device-1", "turn": "turn-1", "agents": ("guide",)}

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def state(self):
        return self.mesh_state

    async def mesh_id(self):
        return "mesh-1"

    async def pairing_offer(self):
        if self.mesh_state != "not_enrolled":
            raise Refused(f"executor.{self.mesh_state}")
        return {"code": "1234 5678", "payload": "PAYLOAD", "expires_ms": 9}

    async def verify(self, ticket_json, action_json, effect):
        self.calls.append((ticket_json, action_json, effect))
        if self.fail:
            raise Refused(self.fail)
        return Verified(**self.who, action_digest=self.digest or f"sha256:{len(self.calls):064d}")


@pytest.fixture
def stack(package_dir, tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(deployment_text(package_dir, mounts="", egress=False))
    deployment = load_deployment(path)
    verifier = FakeVerifier()
    sandbox_app = sandbox.sandbox_app(str(deployment.agents["guide"].package.root), token=SANDBOX_TOKEN,
                                      confine=False)
    runner = sandbox.RemoteRunner("http://sb", SANDBOX_TOKEN, 5, [])
    runner._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox_app), base_url="http://sb",
                                       headers={"Authorization": "Bearer " + SANDBOX_TOKEN})
    app = executor_app(deployment, verifier, {"guide": runner}, TOKEN)
    return deployment, verifier, app


@pytest.fixture
async def client(stack):
    _, _, app = stack
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ex") as http:
            yield http


def entry(deployment, tool):
    return next(t for t in catalog(deployment)["guide"]["tools"] if t["name"] == tool)


def call(deployment, tool="bash", arguments=None, **over):
    arguments_json = json.dumps(arguments if arguments is not None else {"command": "echo ran"})
    action = {"v": 1, "mesh": "mesh-1", "device": "device-1", "turn": "turn-1", "agent": "kernel", "tool": tool,
              "tool_def": entry(deployment, tool)["definition"], "args_digest": args_digest(arguments_json),
              "influence": []}
    body = {"agent": "guide", "tool": tool, "arguments_json": arguments_json, "action_json": json.dumps(action),
            "ticket_json": json.dumps({"v": 1, "capability": "x"}), "workspace": KEY, "owner": "alice"}
    return {**body, **over}


def test_the_catalog_lists_every_tool_with_its_scope_and_definition(stack):
    deployment, _, _ = stack
    listing = catalog(deployment)["guide"]
    assert listing["description"] == "A test agent." and listing["scope"] == "tools:execute"
    tools = {t["name"]: t for t in listing["tools"]}
    assert set(tools) == {"read", "grep", "glob", "list", "bash", "echo"}
    assert tools["bash"]["scope"] == "tools:execute" and tools["echo"]["input_schema"]["type"] == "object"
    assert tools["read"]["definition"] == definition_hash("guide", "read", tools["read"]["description"],
                                                         tools["read"]["input_schema"], {})
    assert len({t["definition"] for t in tools.values()}) == 6
    assert effect_kind("tools:read") == effect_kind("tools:search") == "outward_read"
    assert effect_kind("tools:files") == effect_kind("tools:execute") == "outside_effect"
    assert args_digest("{}") == "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    assert executor_token("m" * 16) != executor_token("n" * 16)


def test_replays_remember_what_ran_and_forget_the_oldest():
    seen = Replays(limit=2)
    assert seen.first_use("a") and not seen.first_use("a") and seen.first_use("b") and seen.first_use("c")
    assert seen.first_use("a")  # evicted, so a very old ticket is judged by its own expiry


async def test_a_verified_call_runs_in_the_sandbox_and_returns_its_output(stack, client):
    deployment, verifier, _ = stack
    response = await client.post("/run", json=call(deployment), headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and body["output"].startswith("ran") and not body["error"]
    assert body["files"] == [] and body["ticket"] == {"mesh": "mesh-1", "device": "device-1", "turn": "turn-1"}
    assert [c[2] for c in verifier.calls] == ["outside_effect"]
    read = await client.post("/run", json=call(deployment, "list", {"path": "notes"}, workspace=None), headers=AUTH)
    assert read.status_code == 200 and "ada-lovelace.md" in read.json()["output"]


async def test_a_read_scoped_tool_is_verified_as_an_outward_read(stack, client):
    deployment, verifier, _ = stack
    await client.post("/run", json=call(deployment, "echo", {"word": "hi"}), headers=AUTH)
    assert verifier.calls[-1][2] == "outward_read"


async def test_every_refusal_is_dotted_and_nothing_runs(stack, client):
    deployment, verifier, _ = stack
    marker = "echo ran > /tmp/executor-marker-never"
    for code in ("ticket.expired", "gate.mesh_stopped", "ticket.consent_required", "executor.not_enrolled"):
        verifier.fail = code
        response = await client.post("/run", json=call(deployment, arguments={"command": marker}), headers=AUTH)
        assert response.status_code == 403 and response.json() == {
            "error": True, "code": code, "output": f"refused: {code}"}
    verifier.fail = None
    assert len(verifier.calls) == 4


async def test_the_action_must_match_the_call_and_the_executors_own_tool(stack, client):
    deployment, verifier, _ = stack
    good = call(deployment)
    action = json.loads(good["action_json"])

    def altered(**fields):
        return {**good, "action_json": json.dumps({**action, **fields})}

    cases = [
        (altered(tool="read"), 403, "request.call_mismatch"),
        (altered(args_digest="sha256:" + "0" * 64), 403, "request.call_mismatch"),
        (altered(tool_def="sha256:" + "1" * 64), 403, "gate.tool_def_changed"),
        ({**good, "arguments_json": '{"command":  "echo ran"}'}, 403, "request.call_mismatch"),
        ({**good, "tool": "teleport"}, 404, "executor.tool_unknown"),
        ({**good, "agent": "nobody"}, 404, "executor.tool_unknown"),
    ]
    for body, status, code in cases:
        response = await client.post("/run", json=body, headers=AUTH)
        assert (response.status_code, response.json()["code"]) == (status, code)
    assert verifier.calls == []


async def test_the_verified_claims_must_match_the_action(stack, client):
    deployment, verifier, _ = stack
    for field in ("mesh", "device", "turn"):
        verifier.who = {"mesh": "mesh-1", "device": "device-1", "turn": "turn-1", "agents": ("guide",), field: "other"}
        response = await client.post("/run", json=call(deployment), headers=AUTH)
        assert response.status_code == 403 and response.json()["code"] == "ticket.unauthorized"


async def test_a_ticket_pays_for_one_effect(stack, client):
    deployment, verifier, _ = stack
    verifier.digest = "sha256:" + "f" * 64
    first = await client.post("/run", json=call(deployment), headers=AUTH)
    second = await client.post("/run", json=call(deployment), headers=AUTH)
    assert first.status_code == 200 and second.status_code == 403 and second.json()["code"] == "ticket.replayed"


async def test_malformed_requests_are_refused_before_anything_else(stack, client):
    deployment, verifier, _ = stack
    good = call(deployment)
    bad = [{}, {**good, "arguments_json": "{"}, {**good, "arguments_json": "[]"}, {**good, "action_json": "[]"},
           {**good, "ticket_json": 5}, {**good, "workspace": "../x"}, {**good, "owner": 3}]
    for body in bad:
        response = await client.post("/run", json=body, headers=AUTH)
        assert (response.status_code, response.json()["code"]) == (400, "request.malformed")
    not_json = await client.post("/run", content=b"nope", headers=AUTH)
    assert not_json.status_code == 400 and verifier.calls == []


async def test_callers_need_the_token_on_every_route(stack, client):
    deployment, verifier, _ = stack
    for route, method in (("/tools", "get"), ("/pairing", "get"), ("/run", "post"), ("/end", "post")):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {TOKEN} "}):
            response = await getattr(client, method)(route, headers=headers)
            assert response.status_code == 401
    assert verifier.calls == []


async def test_health_reports_the_sandboxes_and_the_mesh(stack, client, monkeypatch):
    _, verifier, _ = stack
    verifier.mesh_state = "waiting_for_node"
    body = (await client.get("/health")).json()
    assert body == {"ok": True, "agents": {"guide": False}, "mesh": "waiting_for_node"}
    assert verifier.started
    async def confined(self):
        return {"confined": True}

    monkeypatch.setattr(sandbox.RemoteRunner, "health", confined)
    assert (await client.get("/health")).json()["agents"] == {"guide": True}



async def test_the_catalog_and_pairing_offer_are_served_to_the_owners_node(stack, client):
    _, verifier, _ = stack
    listing = (await client.get("/tools", headers=AUTH)).json()
    assert set(listing["agents"]) == {"guide"}
    refused = await client.get("/pairing", headers=AUTH)
    assert refused.status_code == 409 and refused.json()["code"] == "executor.enrolled"
    verifier.mesh_state = "not_enrolled"
    offer = await client.get("/pairing", headers=AUTH)
    assert offer.status_code == 200 and offer.json()["payload"] == "PAYLOAD"


async def test_ending_a_turn_reaches_the_sandbox_and_checks_its_arguments(stack, client):
    deployment, _, _ = stack
    await client.post("/run", json=call(deployment), headers=AUTH)
    assert (await client.post("/end", json={"agent": "guide", "workspace": KEY}, headers=AUTH)).status_code == 200
    for body in ({"agent": "guide", "workspace": "../x"}, {"agent": "nobody", "workspace": KEY}, {"agent": "guide"},
                 ["x"]):
        response = await client.post("/end", json=body, headers=AUTH)
        assert (response.status_code, response.json()["code"]) == (400, "request.malformed")


async def test_long_output_is_clipped_to_the_agents_limit(stack, client):
    deployment, _, _ = stack
    limit = deployment.agents["guide"].package.manifest.sandbox.tool_output_chars
    response = await client.post("/run", json=call(deployment, arguments={"command": f"yes a | head -c {limit * 3}"}),
                                 headers=AUTH)
    output = response.json()["output"]
    assert len(output) <= limit and "truncated" in output


def test_the_stopped_verifier_is_released_with_the_app(stack):
    _, verifier, app = stack
    import asyncio

    async def cycle():
        async with app.router.lifespan_context(app):
            assert verifier.started and not verifier.stopped

    asyncio.run(cycle())
    assert verifier.stopped


async def test_a_ticket_for_another_agent_or_mesh_or_owner_is_refused(stack, client):
    deployment, verifier, _ = stack
    verifier.who["agents"] = ("other-agent",)
    wrong_agent = await client.post("/run", json=call(deployment), headers=AUTH)
    assert wrong_agent.status_code == 403 and wrong_agent.json()["code"] == "request.call_mismatch"
    verifier.who["agents"] = ()
    assert (await client.post("/run", json=call(deployment), headers=AUTH)).status_code == 403
    verifier.who = {"mesh": "mesh-2", "device": "device-1", "turn": "turn-1", "agents": ("guide",)}
    body = call(deployment)
    body["action_json"] = json.dumps({**json.loads(body["action_json"]), "mesh": "mesh-2"})
    wrong_mesh = await client.post("/run", json=body, headers=AUTH)
    assert wrong_mesh.json()["code"] == "ticket.wrong_mesh"
    verifier.who = {"mesh": "mesh-1", "device": "device-1", "turn": "turn-1", "agents": ("guide",)}
    for field, value in (("owner", "bob"), ("workspace", "e" * 32)):
        bound = call(deployment)
        bound["action_json"] = json.dumps({**json.loads(bound["action_json"]), field: value})
        refused = await client.post("/run", json=bound, headers=AUTH)
        assert refused.json()["code"] == "request.call_mismatch"
    good = call(deployment)
    good["action_json"] = json.dumps({**json.loads(good["action_json"]), "owner": "alice", "workspace": KEY})
    assert (await client.post("/run", json=good, headers=AUTH)).status_code == 200


def test_the_definition_hash_is_unique_per_agent(stack):
    deployment, _, _ = stack
    assert definition_hash("a", "bash", "d", {}, {}) != definition_hash("b", "bash", "d", {}, {})
