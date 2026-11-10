# The Vessel HTTP service: A2A 1.0 JSON-RPC with streaming, the agent card, and owner-scoped conversation APIs.
# Apps authenticate with a deployment key and name the end user; every task and conversation is bound to that user.

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import re
from typing import Any

from a2a.auth.user import User
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import TaskUpdater
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Message,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import events as ev
from .agent import Turn
from .manifest import AgentPackage, load_package
from .model import ModelClient, resolve_setting
from .primitives import schema_version
from .sandbox import LocalRunner, RemoteRunner, ToolRunner
from .store import Store

log = logging.getLogger("astralvessel")
USER_HEADER = "x-vessel-user"
USER_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
SCOPE_KEY = "vessel_owner"
STORED_SKIP = {ev.TEXT_DELTA, ev.REASONING_DELTA}


class VesselUser(User):
    def __init__(self, owner: str):
        self._owner = owner

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._owner


class OwnerContextBuilder(DefaultServerCallContextBuilder):
    def build_user(self, request: Request) -> User:
        return VesselUser(request.scope[SCOPE_KEY])


def data_part(payload: Any, media_type: str = "application/json", metadata: dict[str, Any] | None = None) -> Part:
    part = Part(data=ParseDict(payload, Value()), media_type=media_type)
    if metadata:
        part.metadata.update(metadata)
    return part


class Settings:
    def __init__(self, package: AgentPackage, *, app_key_sha256: str, store_path: str, sandbox_url: str | None,
                 sandbox_token: str | None, public_url: str, idle_hours: float):
        self.package = package
        self.app_key_sha256 = app_key_sha256.lower()
        self.store_path = store_path
        self.sandbox_url = sandbox_url
        self.sandbox_token = sandbox_token
        self.public_url = public_url
        self.idle_hours = idle_hours

    @classmethod
    def from_env(cls) -> Settings:
        digest = os.environ.get("VESSEL_APP_KEY_SHA256", "")
        if not digest and os.environ.get("VESSEL_APP_KEY"):
            digest = hashlib.sha256(os.environ.pop("VESSEL_APP_KEY").encode()).hexdigest()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise SystemExit("VESSEL_APP_KEY_SHA256 (or VESSEL_APP_KEY) must be set")
        return cls(load_package(os.environ["VESSEL_PACKAGE"]), app_key_sha256=digest,
                   store_path=os.environ.get("VESSEL_STORE", "./vessel-data/vessel.sqlite"),
                   sandbox_url=os.environ.get("VESSEL_SANDBOX_URL") or None,
                   sandbox_token=os.environ.get("VESSEL_SANDBOX_TOKEN") or None,
                   public_url=os.environ.get("VESSEL_PUBLIC_URL", "http://localhost:8700"),
                   idle_hours=float(os.environ.get("VESSEL_SESSION_TTL_HOURS", "24")))


class VesselExecutor(AgentExecutor):
    def __init__(self, settings: Settings, store: Store, runner: ToolRunner, model_transport=None):
        self.settings = settings
        self.store = store
        self.runner = runner
        self.model_transport = model_transport
        self.running: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    def _model(self) -> ModelClient:
        m = self.settings.package.manifest.model
        return ModelClient(resolve_setting(m.endpoint), resolve_setting(m.api_key), m.model,
                           temperature=m.temperature, max_output_tokens=m.max_output_tokens,
                           transport=self.model_transport)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        owner = context.call_context.user.user_name
        task_id, conversation_id = context.task_id, context.context_id
        updater = TaskUpdater(event_queue, task_id, conversation_id)
        if context.current_task is None:
            await event_queue.enqueue_event(Task(id=task_id, context_id=conversation_id,
                                                 status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                                                 history=[context.message] if context.message else []))
        text = context.get_user_input().strip()
        if not text:
            await updater.reject(self._note(updater, "send a text message"))
            return
        if not await self.store.claim(owner, conversation_id, text):
            await updater.reject(self._note(updater, "unknown conversation"))
            return
        await updater.start_work()
        history = await self.store.history(owner, conversation_id)
        recorded: list[dict[str, Any]] = []

        async def emit(item: dict[str, Any]) -> None:
            if item["type"] not in STORED_SKIP:
                recorded.append(item)
            if item["type"] == ev.TURN_FINISHED:
                return
            message = updater.new_agent_message([data_part(item)], metadata={"vessel.event": item["type"]})
            await updater.update_status(TaskState.TASK_STATE_WORKING, message,
                                        metadata={"vessel.event": item["type"]})

        model = self._model()
        turn = Turn(self.settings.package, model, self.runner, emit)
        finished = asyncio.Event()
        inner = asyncio.create_task(turn.run(history, text, conversation_id=conversation_id, turn_id=task_id))
        self.running[task_id] = (inner, finished)
        try:
            result = await asyncio.shield(inner)
        except asyncio.CancelledError:
            inner.cancel()
            result = await inner
        finally:
            self.running.pop(task_id, None)
            await model.close()
        try:
            await self.store.record_turn(owner, conversation_id, task_id, text, result, turn.new_messages, recorded)
            await self._finish(updater, result)
        finally:
            finished.set()

    async def _finish(self, updater: TaskUpdater, result: dict[str, Any]) -> None:
        parts = [Part(text=result["text"] or "", media_type="text/markdown")]
        if result["components"]:
            parts.append(data_part({"components": result["components"]}, ev.PRIMITIVES_MEDIA_TYPE,
                                   {"schema_version": schema_version()}))
        await updater.add_artifact(parts, name="answer", last_chunk=True)
        summary = {k: v for k, v in result.items() if k not in ("text", "components")}
        message = updater.new_agent_message([data_part({"type": ev.TURN_FINISHED, **summary})],
                                            metadata={"vessel.event": ev.TURN_FINISHED})
        if result["status"] in (ev.STATUS_OK, ev.STATUS_STEP_LIMIT):
            await updater.complete(message)
        elif result["status"] == ev.STATUS_CANCELLED:
            await updater.cancel(message)
        else:
            await updater.failed(message)

    def _note(self, updater: TaskUpdater, text: str) -> Message:
        return updater.new_agent_message([Part(text=text)])

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        entry = self.running.get(context.task_id)
        if entry is None:
            await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()
            return
        inner, finished = entry
        inner.cancel()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(finished.wait(), timeout=15)


def agent_card(settings: Settings) -> AgentCard:
    agent = settings.package.manifest.agent
    return AgentCard(
        name=agent.name,
        description=agent.description,
        version=agent.version,
        capabilities=AgentCapabilities(streaming=True),
        skills=[AgentSkill(id=agent.name, name=agent.name, description=agent.description, tags=["astralvessel"],
                           examples=agent.examples)],
        default_input_modes=["text/plain"],
        default_output_modes=["text/markdown"] + (
            [ev.PRIMITIVES_MEDIA_TYPE] if settings.package.manifest.output.primitives else []),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=settings.public_url.rstrip("/") + "/a2a",
                                             protocol_version=PROTOCOL_VERSION_1_0)],
        provider=AgentProvider(organization="AstralVessel", url="https://github.com/EvanDamron/AstralVessel"),
    )


def authenticate(settings: Settings, request: Request) -> str | None:
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Bearer "):
        return None
    digest = hashlib.sha256(supplied[7:].encode()).hexdigest()
    if not hmac.compare_digest(digest, settings.app_key_sha256):
        return None
    owner = request.headers.get(USER_HEADER, "")
    return owner if USER_ID.fullmatch(owner) else None


def create_app(settings: Settings | None = None, runner: ToolRunner | None = None,
               model_transport=None) -> Starlette:
    settings = settings or Settings.from_env()
    store = Store(settings.store_path)
    package = settings.package
    if runner is None:
        if settings.sandbox_url:
            runner = RemoteRunner(settings.sandbox_url, settings.sandbox_token or "",
                                  package.manifest.sandbox.tool_timeout_seconds)
        else:
            log.warning("no VESSEL_SANDBOX_URL: tools run in-process, which is for development only")
            runner = LocalRunner(package)
    executor = VesselExecutor(settings, store, runner, model_transport)
    card = agent_card(settings)
    handler = DefaultRequestHandler(agent_executor=executor, agent_card=card,
                                    task_store=InMemoryTaskStore(owner_resolver=lambda c: c.user.user_name))

    def unauthorized() -> JSONResponse:
        return JSONResponse({"error": "authentication required"}, status_code=401)

    def gated(endpoint):
        async def wrapper(request: Request):
            owner = authenticate(settings, request)
            if owner is None:
                if request.url.path.startswith("/a2a"):
                    return JSONResponse({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32001, "message": "authentication required"}},
                                        status_code=401)
                return unauthorized()
            request.scope[SCOPE_KEY] = owner
            return await endpoint(request)
        return wrapper

    async def list_conversations(request: Request) -> JSONResponse:
        return JSONResponse({"conversations": await store.list(request.scope[SCOPE_KEY])})

    async def conversation(request: Request) -> JSONResponse:
        owner, cid = request.scope[SCOPE_KEY], request.path_params["cid"]
        if request.method == "DELETE":
            if await store.delete(owner, cid):
                return JSONResponse({"deleted": True})
            return JSONResponse({"error": "not found"}, status_code=404)
        found = await store.get(owner, cid)
        return JSONResponse(found) if found else JSONResponse({"error": "not found"}, status_code=404)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "agent": package.manifest.agent.name})

    async def manifest(_request: Request) -> JSONResponse:
        m = package.manifest
        return JSONResponse({
            "agent": m.agent.model_dump(), "model": {"model": m.model.model, "max_steps": m.model.max_steps},
            "tools": [*m.tools.builtin, *(t.name for t in package.declared), *(["present"] if m.output.primitives else [])],
            "sandbox": {"mounts": [x.model_dump() for x in m.sandbox.mounts],
                        "deadline_seconds": m.sandbox.deadline_seconds},
            "output": m.output.model_dump(), "primitives_schema": schema_version(),
        })

    routes = [Route("/health", health), Route("/v1/manifest", gated(manifest)),
              Route("/v1/conversations", gated(list_conversations)),
              Route("/v1/conversations/{cid}", gated(conversation), methods=["GET", "DELETE"])]
    for route in create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=OwnerContextBuilder()):
        routes.append(Route(route.path, endpoint=gated(route.endpoint), methods=sorted(route.methods or {"POST"})))
    routes.extend(create_agent_card_routes(card))

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async def janitor():
            while True:
                await asyncio.sleep(3600)
                with contextlib.suppress(Exception):
                    await store.sweep(settings.idle_hours * 3600)
        sweeper = asyncio.create_task(janitor())
        try:
            yield
        finally:
            sweeper.cancel()
            await runner.close()

    return Starlette(routes=routes, lifespan=lifespan)

