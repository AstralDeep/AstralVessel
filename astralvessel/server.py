# The Vessel HTTP service: one A2A 1.0 endpoint and agent card per agent, behind per-app keys.
# Applications own conversation history and send it with each turn; the vessel keeps only runs in flight.

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from a2a.auth.user import User
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.models import create_task_model
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import TaskUpdater
from a2a.server.tasks.database_task_store import DatabaseTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import events as ev
from . import transcript
from .agent import Turn
from .deployment import Agent, AppEntry, Deployment, load_deployment, resolve, sandbox_token
from .model import ModelClient, resolve_setting
from .primitives import schema_version
from .sandbox import LocalRunner, RemoteRunner, ToolRunner

log = logging.getLogger("astralvessel")
USER_HEADER = "x-vessel-user"
USER_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_MESSAGE_CHARS = 100_000
MAX_BODY_BYTES = 16_000_000
SCOPE_KEY = "vessel_owner"
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}


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


@dataclass
class Settings:
    deployment: Deployment
    task_db: str
    sandbox_url_template: str | None = None
    sandbox_master: str | None = None
    app_keys: dict[str, AppEntry] = field(default_factory=dict)
    allow_in_process: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        deployment = load_deployment(os.environ["VESSEL_DEPLOYMENT"], os.environ.get("VESSEL_PACKAGES_ROOT"))
        template = os.environ.get("VESSEL_SANDBOX_URL_TEMPLATE") or None
        master = os.environ.get("VESSEL_SANDBOX_TOKEN") or None
        if template and (not master or len(master) < 16):
            raise SystemExit("VESSEL_SANDBOX_TOKEN (16+ characters) is required with remote sandboxes")
        return cls(deployment, task_db=os.environ.get("VESSEL_TASK_DB", "./vessel-data/tasks.sqlite"),
                   sandbox_url_template=template, sandbox_master=master, app_keys=app_keys(deployment),
                   allow_in_process=os.environ.get("VESSEL_DEV_UNSANDBOXED") == "1")


def app_keys(deployment: Deployment) -> dict[str, AppEntry]:
    keys = {}
    for app in deployment.apps:
        digest = resolve(app.key_sha256).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SystemExit(f"app {app.name}: key_sha256 must be a sha256 hex digest")
        keys[digest] = app
    return keys


class VesselTaskStore(DatabaseTaskStore):
    # Tasks are run state, not history: keep the user's text, drop streamed status messages and the history sent in.
    # Each store gets its own table registry; the SDK's shared one refuses a second table of the same name.

    def __init__(self, engine, *, table_name: str, owner_resolver):
        super().__init__(engine, owner_resolver=owner_resolver)

        class Base(DeclarativeBase):
            pass
        self._base = Base
        self.task_model = create_task_model(table_name, Base)

    async def initialize(self) -> None:
        if not self._initialized:
            async with self.engine.begin() as conn:
                await conn.run_sync(self._base.metadata.create_all)
            self._initialized = True

    async def save(self, task: Task, context) -> None:
        keep = []
        for message in task.history:
            if message.role != Role.ROLE_USER:
                continue
            text = [p for p in message.parts if p.HasField("text")]
            if len(text) != len(message.parts):
                del message.parts[:]
                message.parts.extend(text)
            keep.append(message)
        del task.history[:]
        task.history.extend(keep)
        await super().save(task, context)

    async def recover(self) -> int:
        # Work in flight died with the previous process; mark it failed so nobody waits on it forever.
        await self._ensure_initialized()
        model, failed = self.task_model, 0
        async with self.async_session_maker.begin() as session:
            for row in (await session.execute(select(model.id, model.status))).all():
                if (row.status or {}).get("state") not in TERMINAL:
                    status = MessageToDict(TaskStatus(state=TaskState.TASK_STATE_FAILED))
                    await session.execute(update(model).where(model.id == row.id).values(status=status))
                    failed += 1
        return failed

    async def sweep(self, ttl_hours: float) -> None:
        await self._ensure_initialized()
        cutoff = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=ttl_hours)
        async with self.async_session_maker.begin() as session:
            await session.execute(delete(self.task_model).where(self.task_model.last_updated < cutoff))


class VesselExecutor(AgentExecutor):
    def __init__(self, agent: Agent, runner: ToolRunner, model_transport=None):
        self.agent = agent
        self.runner = runner
        self.model_transport = model_transport
        self.running: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    def _model(self) -> ModelClient:
        m = self.agent.package.manifest.model
        return ModelClient(resolve_setting(m.endpoint), resolve_setting(m.api_key), m.model,
                           temperature=m.temperature, max_output_tokens=m.max_output_tokens,
                           transport=self.model_transport)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, conversation_id = context.task_id, context.context_id
        updater = TaskUpdater(event_queue, task_id, conversation_id)
        if context.current_task is None:
            await event_queue.enqueue_event(Task(id=task_id, context_id=conversation_id,
                                                 status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                                                 history=[context.message] if context.message else []))
        text = context.get_user_input().strip()
        try:
            history = self._history(context.message)
        except transcript.TranscriptError as exc:
            await updater.reject(self._note(updater, f"invalid history: {exc}"))
            return
        if not text:
            await updater.reject(self._note(updater, "send a text message"))
            return
        if len(text) > MAX_MESSAGE_CHARS:
            await updater.reject(self._note(updater, f"messages are limited to {MAX_MESSAGE_CHARS} characters"))
            return
        if not CONVERSATION_ID.fullmatch(conversation_id or ""):
            await updater.reject(self._note(updater, "invalid conversation id"))
            return
        await updater.start_work()
        artifact_id = f"{task_id}-transcript"
        chunks = [0]

        async def emit(item: dict[str, Any]) -> None:
            if item["type"] == ev.TURN_FINISHED:
                return
            message = updater.new_agent_message([data_part(item)], metadata={"vessel.event": item["type"]})
            await updater.update_status(TaskState.TASK_STATE_WORKING, message,
                                        metadata={"vessel.event": item["type"]})

        async def on_message(message: dict[str, Any]) -> None:
            await updater.add_artifact([data_part(transcript.envelope([message]), transcript.MEDIA_TYPE)],
                                       artifact_id=artifact_id, name="transcript", append=chunks[0] > 0,
                                       last_chunk=False)
            chunks[0] += 1

        model = self._model()
        turn = Turn(self.agent.package, model, self.runner, emit, on_message)
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
            summary = {k: v for k, v in result.items() if k not in ("text", "components")}
            summary["model"] = self.agent.package.manifest.model.model
            await updater.add_artifact([data_part(transcript.envelope([], summary), transcript.MEDIA_TYPE)],
                                       artifact_id=artifact_id, name="transcript", append=chunks[0] > 0,
                                       last_chunk=True)
            await self._finish(updater, result)
        finally:
            finished.set()

    def _history(self, message: Message | None) -> list[dict[str, Any]]:
        for part in message.parts if message else []:
            if part.media_type == transcript.MEDIA_TYPE:
                return transcript.parse_history(MessageToDict(part.data))
        return []

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


def agent_url(deployment: Deployment, agent_id: str) -> str:
    return f"{deployment.vessel.public_url.rstrip('/')}/agents/{agent_id}"


def agent_card(deployment: Deployment, agent: Agent) -> AgentCard:
    meta = agent.package.manifest.agent
    return AgentCard(
        name=agent.id,
        description=meta.description,
        version=meta.version,
        capabilities=AgentCapabilities(streaming=True),
        # Astral reads a skill's permission scope from a scope: tag; an agent carries its strongest tool's scope.
        skills=[AgentSkill(id=agent.id, name=meta.name, description=meta.description,
                           tags=["astralvessel", f"scope:{agent.package.strongest_scope()}"], examples=meta.examples)],
        default_input_modes=["text/plain", transcript.MEDIA_TYPE],
        default_output_modes=["text/markdown", transcript.MEDIA_TYPE] + (
            [ev.PRIMITIVES_MEDIA_TYPE] if agent.package.manifest.output.primitives else []),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=agent_url(deployment, agent.id) + "/a2a",
                                             protocol_version=PROTOCOL_VERSION_1_0)],
        provider=AgentProvider(organization="AstralVessel", url="https://github.com/AstralDeep/AstralVessel"),
    )


@dataclass
class Runtime:
    agent: Agent
    runner: ToolRunner
    card: AgentCard
    tasks: VesselTaskStore
    available: bool = False


def authenticate(settings: Settings, request: Request) -> tuple[AppEntry, str] | None:
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Bearer "):
        return None
    digest = hashlib.sha256(supplied[7:].encode()).hexdigest()
    app = next((a for d, a in settings.app_keys.items() if hmac.compare_digest(d, digest)), None)
    user = request.headers.get(USER_HEADER, "")
    if app is None or not USER_ID.fullmatch(user):
        return None
    return app, user


def create_app(settings: Settings | None = None, runners: dict[str, ToolRunner] | None = None,
               model_transport=None) -> Starlette:
    settings = settings or Settings.from_env()
    deployment = settings.deployment
    os.makedirs(os.path.dirname(os.path.abspath(settings.task_db)), exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{settings.task_db}")
    runtimes: dict[str, Runtime] = {}
    routes: list[Route] = []

    def refuse(rpc: bool, status: int, code: int, message: str) -> JSONResponse:
        if rpc:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}},
                                status_code=status)
        return JSONResponse({"error": message}, status_code=status)

    def gated(endpoint, agent_id: str | None = None):
        async def wrapper(request: Request):
            rpc = request.url.path.endswith("/a2a")
            found = authenticate(settings, request)
            if found is None:
                return refuse(rpc, 401, -32001, "authentication required")
            app, user = found
            if agent_id is not None and agent_id not in app.agents:
                return refuse(rpc, 403, -32003, "this app may not call this agent")
            if agent_id is not None and not runtimes[agent_id].available:
                return refuse(rpc, 503, -32004, "this agent's sandbox is not ready")
            # Count what actually arrives: a chunked body carries no Content-Length to check.
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return JSONResponse({"error": "request too large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
            request.scope[SCOPE_KEY] = f"{app.name}/{user}"
            request.scope["vessel_app"] = app
            return await endpoint(request)
        return wrapper

    for agent_id, agent in deployment.agents.items():
        if runners and agent_id in runners:
            runner = runners[agent_id]
        elif settings.sandbox_url_template:
            sandbox = agent.package.manifest.sandbox
            longest = max([sandbox.tool_timeout_seconds] + [t.timeout_seconds or 0 for t in agent.package.declared])
            runner = RemoteRunner(settings.sandbox_url_template.format(id=agent_id),
                                  sandbox_token(settings.sandbox_master or "", agent_id), longest)
        elif settings.allow_in_process:
            log.warning("agent %s: tools run unconfined in the vessel process (development only)", agent_id)
            runner = LocalRunner(agent.package)
        else:
            raise SystemExit("set VESSEL_SANDBOX_URL_TEMPLATE, or VESSEL_DEV_UNSANDBOXED=1 for local development")
        tasks = VesselTaskStore(engine, table_name=f"tasks_{agent_id.replace('-', '_')}",
                                owner_resolver=lambda c: c.user.user_name)
        card = agent_card(deployment, agent)
        runtime = Runtime(agent, runner, card, tasks, available=not isinstance(runner, RemoteRunner))
        runtimes[agent_id] = runtime
        handler = DefaultRequestHandler(agent_executor=VesselExecutor(agent, runner, model_transport),
                                        agent_card=card, task_store=tasks)
        base = f"/agents/{agent_id}"
        for route in create_jsonrpc_routes(handler, rpc_url=f"{base}/a2a", context_builder=OwnerContextBuilder()):
            routes.append(Route(route.path, endpoint=gated(route.endpoint, agent_id),
                                methods=sorted(route.methods or {"POST"})))
        routes.extend(create_agent_card_routes(card, card_url=f"{base}/.well-known/agent-card.json"))
        routes.append(Route(f"{base}/manifest", gated(manifest_endpoint(agent), agent_id)))

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "agents": {i: r.available for i, r in runtimes.items()}})

    async def list_agents(request: Request) -> JSONResponse:
        app: AppEntry = request.scope["vessel_app"]
        return JSONResponse({"agents": [
            {"id": i, "name": r.agent.package.manifest.agent.name, "description": r.card.description,
             "card_url": agent_url(deployment, i) + "/.well-known/agent-card.json", "available": r.available}
            for i, r in runtimes.items() if i in app.agents]})

    routes += [Route("/health", health), Route("/v1/agents", gated(list_agents))]

    async def watch_sandboxes() -> None:
        # Serve an agent only while its sandbox runs, confined, with the profile this deployment expects.
        while True:
            for runtime in runtimes.values():
                if isinstance(runtime.runner, RemoteRunner):
                    health = await runtime.runner.health()
                    ready = bool(health and health.get("profile") == runtime.agent.fingerprint
                                 and health.get("confined"))
                    if ready != runtime.available:
                        log.warning("agent %s sandbox is %s", runtime.agent.id, "ready" if ready else "NOT ready")
                    runtime.available = ready
            await asyncio.sleep(60 if all(r.available for r in runtimes.values()) else 5)

    async def janitor() -> None:
        while True:
            with contextlib.suppress(Exception):
                for runtime in runtimes.values():
                    await runtime.tasks.sweep(deployment.vessel.task_ttl_hours)
            await asyncio.sleep(3600)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        for runtime in runtimes.values():
            if await runtime.tasks.recover():
                log.warning("agent %s: marked unfinished tasks from a previous run as failed", runtime.agent.id)
        background = [asyncio.create_task(watch_sandboxes()), asyncio.create_task(janitor())]
        try:
            yield
        finally:
            for job in background:
                job.cancel()
            for runtime in runtimes.values():
                await runtime.runner.close()
            await engine.dispose()

    return Starlette(routes=routes, lifespan=lifespan)


def manifest_endpoint(agent: Agent):
    async def manifest(_request: Request) -> JSONResponse:
        m = agent.package.manifest
        return JSONResponse({
            "agent": {**m.agent.model_dump(), "id": agent.id},
            "model": {"model": m.model.model, "max_steps": m.model.max_steps},
            "tools": agent.package.tool_scopes(),
            "sandbox": {"mounts": [x.model_dump() for x in m.sandbox.mounts],
                        "deadline_seconds": m.sandbox.deadline_seconds},
            "output": m.output.model_dump(), "primitives_schema": schema_version(),
        })
    return manifest
