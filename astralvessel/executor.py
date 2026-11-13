# The executor in front of the sandboxes: it verifies a ticket and the action it covers before a sandbox runs a tool.
# The node's tool adapter calls POST /run on it; it speaks to each sandbox over HTTP with that sandbox's own token.

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .deployment import Deployment, load_deployment, sandbox_token
from .manifest import BUILTIN_SCOPES
from .mesh import Refused, Verified
from .sandbox import RemoteRunner
from .tools import BUILTIN_SCHEMAS, WORKSPACE_KEY, clip

READ_SCOPES = frozenset({"tools:read", "tools:search"})
SANDBOX_PORT = 8710
SEEN_LIMIT = 8192


class Verifier(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def verify(self, ticket_json: str, action_json: str, effect: str) -> Verified: ...

    async def state(self) -> str: ...

    async def mesh_id(self) -> str | None: ...

    async def pairing_offer(self) -> dict[str, Any]: ...


def definition_hash(agent: str, name: str, description: str, input_schema: Any, annotations: Any) -> str:
    # The kernel hashes a published tool as name, description, input schema and annotations, so putting the agent id in
    # the annotations makes this the hash a device's ticket carries for a tool published with that annotation.
    document = {"annotations": {**annotations, "agent": agent}, "description": description,
                "input_schema": input_schema, "name": name}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def args_digest(arguments_json: str) -> str:
    return "sha256:" + hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()


def executor_token(master: str) -> str:
    # The node's token for this executor, derived from the master secret so no two tokens in the deployment match.
    return hmac.new(master.encode(), b"executor:run", hashlib.sha256).hexdigest()


def catalog(deployment: Deployment) -> dict[str, dict[str, Any]]:
    # What each agent offers: the tools the manifest lists, each with its schema, scope and definition hash.
    found: dict[str, dict[str, Any]] = {}
    for agent_id, agent in deployment.agents.items():
        tools: dict[str, dict[str, Any]] = {}
        for name in agent.package.manifest.tools.builtin:
            schema = BUILTIN_SCHEMAS[name]
            tools[name] = {"description": schema["description"], "input_schema": schema["parameters"],
                           "scope": BUILTIN_SCOPES[name]}
        for tool in agent.package.declared:
            tools[tool.name] = {"description": tool.description, "input_schema": tool.parameters, "scope": tool.scope}
        for name, tool in tools.items():
            tool["name"] = name
            tool["definition"] = definition_hash(agent_id, name, tool["description"], tool["input_schema"], {})
        found[agent_id] = {"description": agent.package.manifest.agent.description,
                           "scope": agent.package.strongest_scope(), "tools": list(tools.values())}
    return found


def effect_kind(scope: str) -> str:
    return "outward_read" if scope in READ_SCOPES else "outside_effect"


def _refusal(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": True, "code": code, "output": f"refused: {code}"}, status_code=status)


class Replays:
    # A ticket pays for one effect. The kernel verifies each ticket afresh, so the executor remembers which it ran.
    def __init__(self, limit: int = SEEN_LIMIT):
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._limit = limit

    def first_use(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > self._limit:
            self._seen.popitem(last=False)
        return True


def executor_app(deployment: Deployment, verifier: Verifier, runners: dict[str, RemoteRunner], token: str) -> Starlette:
    tools = catalog(deployment)
    replays = Replays()

    def authorized(request: Request) -> bool:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(supplied.encode(), token.encode())

    async def health(_request: Request) -> JSONResponse:
        ready = {agent_id: ((await runner.health()) or {}).get("confined") is True
                 for agent_id, runner in runners.items()}
        return JSONResponse({"ok": True, "agents": ready, "mesh": await verifier.state()})

    async def listing(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        return JSONResponse({"agents": tools})

    async def pairing(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        try:
            return JSONResponse(await verifier.pairing_offer())
        except Refused as exc:
            return _refusal(exc.code, 409)

    async def run(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            agent_id, name = body["agent"], body["tool"]
            arguments_json, ticket_json, action_json = body["arguments_json"], body["ticket_json"], body["action_json"]
            workspace, owner = body.get("workspace"), body.get("owner") or ""
            if not all(isinstance(v, str) for v in (agent_id, name, arguments_json, ticket_json, action_json, owner)):
                raise ValueError
            arguments, action = json.loads(arguments_json), json.loads(action_json)
            if not isinstance(arguments, dict) or not isinstance(action, dict):
                raise ValueError
            if workspace is not None and not (isinstance(workspace, str) and WORKSPACE_KEY.fullmatch(workspace)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return _refusal("request.malformed", 400)
        entry = next((t for t in tools.get(agent_id, {}).get("tools", []) if t["name"] == name), None)
        if entry is None:
            return _refusal("executor.tool_unknown", 404)
        if action.get("tool") != name or action.get("args_digest") != args_digest(arguments_json):
            return _refusal("request.call_mismatch", 403)
        if action.get("tool_def") != entry["definition"]:
            return _refusal("gate.tool_def_changed", 403)
        if deployment.owner.owner_id not in (None, owner):
            return _refusal("executor.owner_mismatch", 403)
        try:
            verified = await verifier.verify(ticket_json, action_json, effect_kind(entry["scope"]))
        except Refused as exc:
            return _refusal(exc.code, 403)
        if verified.mesh != action.get("mesh") or verified.turn != action.get("turn") \
                or verified.device != action.get("device"):
            return _refusal("ticket.unauthorized", 403)
        if verified.mesh != await verifier.mesh_id():
            return _refusal("ticket.wrong_mesh", 403)
        if not replays.first_use(verified.action_digest):
            return _refusal("ticket.replayed", 403)
        output, error = await runners[agent_id].run(name, arguments, workspace, owner=owner)
        limit = deployment.agents[agent_id].package.manifest.sandbox.tool_output_chars
        files = [f.wire() for f in runners[agent_id].take_files(workspace)] if workspace else []
        return JSONResponse({"output": clip(output, limit)[0], "error": error, "files": files,
                             "ticket": {"mesh": verified.mesh, "device": verified.device, "turn": verified.turn}})

    async def end(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            agent_id, workspace = body["agent"], body["workspace"]
            if agent_id not in runners or not (isinstance(workspace, str) and WORKSPACE_KEY.fullmatch(workspace)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return _refusal("request.malformed", 400)
        await runners[agent_id].end_turn(workspace)
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        await verifier.start()
        try:
            yield
        finally:
            await verifier.stop()
            await asyncio.gather(*(r.close() for r in runners.values()))

    return Starlette(routes=[Route("/health", health), Route("/tools", listing), Route("/pairing", pairing),
                             Route("/run", run, methods=["POST"]), Route("/end", end, methods=["POST"])],
                     lifespan=lifespan)


def build(deployment: Deployment | None = None) -> Starlette:
    from .mesh import KernelVerifier
    deployment = deployment or load_deployment(os.environ["VESSEL_DEPLOYMENT"],
                                               os.environ.get("VESSEL_PACKAGES_ROOT"))
    master = os.environ["VESSEL_SANDBOX_TOKEN"]
    template = os.environ.get("VESSEL_SANDBOX_URL_TEMPLATE", f"http://sandbox-{{id}}:{SANDBOX_PORT}")
    runners = {agent_id: RemoteRunner(template.format(id=agent_id), sandbox_token(master, agent_id),
                                      agent.package.manifest.sandbox.tool_timeout_seconds,
                                      list(agent.package.manifest.output.files))
               for agent_id, agent in deployment.agents.items()}
    node_url = os.environ.get("VESSEL_NODE_URL", "http://node:8001").rstrip("/")

    async def mailbox() -> str | None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                document = (await client.get(f"{node_url}/api/node/descriptor")).json()
            return (document.get("mailbox") or {}).get("address")
        except (httpx.HTTPError, ValueError):
            return None

    verifier = KernelVerifier(Path(os.environ.get("VESSEL_EXECUTOR_DIR", "/executor")), mailbox=mailbox)
    return executor_app(deployment, verifier, runners, os.environ["VESSEL_EXECUTOR_TOKEN"])

