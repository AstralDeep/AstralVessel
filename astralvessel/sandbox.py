# Runs sandbox tools either in-process (development) or in the separate sandbox container over HTTP.
# The sandbox container holds no session store and no secrets, so a hostile command cannot reach either.

from __future__ import annotations

import asyncio
import hmac
import os
import signal
from typing import Any, Protocol

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .manifest import AgentPackage, load_package
from .tools import ACTIVE_SESSIONS, SPAWNING, run_sandbox_tool


class ToolRunner(Protocol):
    async def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]: ...

    async def close(self) -> None: ...


class LocalRunner:
    def __init__(self, package: AgentPackage):
        self.package = package
        self.declared = {t.name: t for t in package.declared}

    async def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        sandbox = self.package.manifest.sandbox
        workdir = os.environ.get("VESSEL_WORKDIR") or sandbox.workdir
        return await run_sandbox_tool(name, args, workdir=workdir, timeout=sandbox.tool_timeout_seconds,
                                      declared=self.declared, package_root=str(self.package.root))

    async def close(self) -> None:
        return None


class RemoteRunner:
    def __init__(self, url: str, token: str, timeout: int):
        self._client = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"},
                                         timeout=httpx.Timeout(timeout + 15, connect=10))

    async def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            response = await self._client.post("/run", json={"name": name, "args": args})
        except httpx.HTTPError:
            return "the sandbox is unreachable", True
        if response.status_code != 200:
            return f"the sandbox refused the call (HTTP {response.status_code})", True
        body = response.json()
        return str(body.get("output", "")), bool(body.get("error"))

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


def sandbox_app(package_path: str | None = None, token: str | None = None) -> Starlette:
    package = load_package(package_path or os.environ["VESSEL_PACKAGE"])
    secret = token or os.environ["VESSEL_SANDBOX_TOKEN"]
    if len(secret) < 16:
        raise ValueError("VESSEL_SANDBOX_TOKEN must be at least 16 characters")
    runner = LocalRunner(package)

    async def run(request: Request) -> JSONResponse:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            return JSONResponse({"error": True, "output": "unauthorized"}, status_code=401)
        body = await request.json()
        name, args = str(body.get("name", "")), body.get("args") or {}
        if not isinstance(args, dict):
            return JSONResponse({"error": True, "output": "arguments must be an object"}, status_code=400)
        reap_strays()
        task = asyncio.create_task(runner.run(name, args))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=0.5)
                if not done and await request.is_disconnected():
                    task.cancel()
                    return JSONResponse({"error": True, "output": "cancelled"}, status_code=499)
            output, error = task.result()
        finally:
            reap_strays()
        return JSONResponse({"output": output, "error": error})

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/run", run, methods=["POST"]), Route("/health", health)])
