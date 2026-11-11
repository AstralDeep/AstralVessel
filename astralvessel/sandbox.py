# Runs sandbox tools either in-process (development) or in the separate sandbox container over HTTP.
# The sandbox container holds no session store and no secrets, so a hostile command cannot reach either.

from __future__ import annotations

import asyncio
import hmac
import os
import signal
import time
from typing import Any, Protocol

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import confine as landlock
from .manifest import AgentPackage, load_package
from .tools import (
    ACTIVE_SESSIONS,
    SPAWNING,
    WORKSPACE_KEY,
    Confinement,
    end_workspace,
    run_sandbox_tool,
    sweep_workspaces,
)

SCRATCH_ROOT = "/tmp/vessel-calls"


class ToolRunner(Protocol):
    # workspace is the turn's random key: calls that pass the same key share one scratch directory.
    async def run(self, name: str, args: dict[str, Any], workspace: str | None = None) -> tuple[str, bool]: ...

    async def end_turn(self, workspace: str) -> None: ...

    async def close(self) -> None: ...


def confinement_for(package: AgentPackage, scratch_root: str = SCRATCH_ROOT) -> Confinement:
    sandbox = package.manifest.sandbox
    readable = (sandbox.workdir, str(package.root), *(m.target for m in sandbox.mounts))
    return Confinement(readable=tuple(dict.fromkeys(readable)), scratch_root=scratch_root)


class LocalRunner:
    def __init__(self, package: AgentPackage, confinement: Confinement | None = None):
        self.package = package
        self.declared = {t.name: t for t in package.declared if t.command}  # service tools never run here
        self.confinement = confinement

    async def run(self, name: str, args: dict[str, Any], workspace: str | None = None) -> tuple[str, bool]:
        sandbox = self.package.manifest.sandbox
        workdir = os.environ.get("VESSEL_WORKDIR") or sandbox.workdir
        return await run_sandbox_tool(name, args, workdir=workdir, timeout=sandbox.tool_timeout_seconds,
                                      declared=self.declared, package_root=str(self.package.root),
                                      confinement=self.confinement, workspace=workspace)

    async def end_turn(self, workspace: str) -> None:
        await asyncio.to_thread(end_workspace, self.confinement, workspace)

    async def close(self) -> None:
        return None


class RemoteRunner:
    def __init__(self, url: str, token: str, timeout: int):
        self._client = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"},
                                         timeout=httpx.Timeout(timeout + 15, connect=10))

    async def health(self) -> dict[str, Any] | None:
        try:
            response = await self._client.get("/health", timeout=5)
        except httpx.HTTPError:
            return None
        return response.json() if response.status_code == 200 else None

    async def run(self, name: str, args: dict[str, Any], workspace: str | None = None) -> tuple[str, bool]:
        try:
            response = await self._client.post("/run", json={"name": name, "args": args, "workspace": workspace})
        except httpx.HTTPError:
            return "the sandbox is unreachable", True
        if response.status_code != 200:
            return f"the sandbox refused the call (HTTP {response.status_code})", True
        body = response.json()
        return str(body.get("output", "")), bool(body.get("error"))

    async def end_turn(self, workspace: str) -> None:
        try:
            await self._client.post("/end", json={"workspace": workspace}, timeout=10)
        except httpx.HTTPError:
            pass  # the sandbox sweeps workspaces older than any turn could run

    async def close(self) -> None:
        await self._client.aclose()


def reap_strays() -> None:
    # Background processes a call left behind (nohup, setsid, a daemon) must not outlive it: they could
    # watch later calls made for other users. Only the sandbox server runs this, inside its own container.
    me = os.getpid()
    if me != 1 or SPAWNING[0]:
        return  # not the container's init process (never reap a developer's machine), or a call is starting
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        pid = int(entry)
        try:
            if os.getsid(pid) not in ACTIVE_SESSIONS:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def sandbox_app(package_path: str | None = None, token: str | None = None, *, confine: bool | None = None,
                scratch_root: str = SCRATCH_ROOT) -> Starlette:
    package = load_package(package_path or os.environ["VESSEL_PACKAGE"])
    secret = token or os.environ["VESSEL_SANDBOX_TOKEN"]
    if len(secret) < 16:
        raise ValueError("VESSEL_SANDBOX_TOKEN must be at least 16 characters")
    if confine is None:
        confine = os.environ.get("VESSEL_UNCONFINED") != "1"
    confinement = None
    if confine:
        # Without Landlock, concurrent calls for different users could see each other; refuse to start.
        if landlock.abi_version() < landlock.REQUIRED_ABI:
            raise SystemExit(f"this kernel lacks Landlock ABI {landlock.REQUIRED_ABI}; per-call isolation is "
                             "unavailable (set VESSEL_UNCONFINED=1 only for development)")
        os.makedirs(scratch_root, mode=0o711, exist_ok=True)
        confinement = confinement_for(package, scratch_root)
    runner = LocalRunner(package, confinement)
    profile = os.environ.get("VESSEL_PROFILE_HASH", "")
    stale_after = package.manifest.sandbox.deadline_seconds + 60
    started: dict[str, float] = {}  # live turns and when each was first seen
    running: dict[str, set[asyncio.Task]] = {}
    ended: dict[str, None] = {}  # recently ended keys, oldest first; a late call for one is refused

    def authorized(request: Request) -> bool:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(supplied.encode(), secret.encode())

    def workspace_of(body: dict[str, Any]) -> str | None:
        workspace = body.get("workspace") if isinstance(body, dict) else ""
        if workspace is not None and not (isinstance(workspace, str) and WORKSPACE_KEY.fullmatch(workspace)):
            raise ValueError("invalid workspace")
        return workspace

    async def run(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        body = await request.json()
        name, args = str(body.get("name", "")), body.get("args") or {}
        if not isinstance(args, dict):
            return JSONResponse({"error": True, "output": "arguments must be an object"}, status_code=400)
        try:
            workspace = workspace_of(body)
        except ValueError:
            return JSONResponse({"error": True, "output": "invalid workspace"}, status_code=400)
        if workspace in ended:
            return JSONResponse({"error": True, "output": "this turn has ended"}, status_code=410)
        now = time.monotonic()
        if workspace:
            started.setdefault(workspace, now)
        for key in [k for k, t in started.items() if now - t > stale_after]:
            del started[key]
        await asyncio.to_thread(sweep_workspaces, confinement, set(started))
        reap_strays()
        task = asyncio.create_task(runner.run(name, args, workspace))
        if workspace:
            running.setdefault(workspace, set()).add(task)
            task.add_done_callback(lambda t: running.get(workspace, set()).discard(t))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=0.5)
                if not done and await request.is_disconnected():
                    task.cancel()
                    return JSONResponse({"error": True, "output": "cancelled"}, status_code=499)
            if task.cancelled():
                return JSONResponse({"error": True, "output": "this turn has ended"}, status_code=410)
            output, error = task.result()
        finally:
            reap_strays()
        return JSONResponse({"output": output, "error": error})

    async def end(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        try:
            workspace = workspace_of(await request.json())
        except ValueError:
            workspace = None
        if workspace is None:
            return JSONResponse({"error": True, "output": "invalid workspace"}, status_code=400)
        ended[workspace] = None
        while len(ended) > 4096:
            del ended[next(iter(ended))]
        started.pop(workspace, None)
        # Stop the turn's calls still in flight first, so none writes into or recreates the workspace after it.
        tasks = running.pop(workspace, set())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.end_turn(workspace)
        return JSONResponse({"ok": True})

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "agent": package.manifest.agent.name, "profile": profile,
                             "loaded": package.loaded_digest(), "confined": confinement is not None})

    return Starlette(routes=[Route("/run", run, methods=["POST"]), Route("/end", end, methods=["POST"]),
                             Route("/health", health)])
