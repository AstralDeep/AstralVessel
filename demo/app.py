# The demo app in front of a Vessel: it holds the app key, keeps each persona's conversations, and relays turns.
# It renders primitives with AstralProjection's own web renderer, so the preview is exactly what Astral would show.

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from astralvessel.client import VesselClient, transcript_of
from astralvessel.history import HistoryStore, new_id

HERE = Path(__file__).parent
PERSONAS = {
    "alice": {"id": "demo:alice", "name": "Alice", "role": "Research administrator"},
    "bob": {"id": "demo:bob", "name": "Bob", "role": "Faculty member"},
}
AGENT = os.environ.get("DEMO_AGENT", "nexus")
SKIPPED = {"text.delta", "reasoning.delta", "transcript"}


def projection_static() -> Path | None:
    try:
        import webrender
    except ImportError:
        return None
    return Path(webrender.__file__).parent / "static"


def render_primitives(components: list[dict[str, Any]]) -> str:
    from webrender.registry import render_for_target
    return render_for_target("web", components)


def create_app():
    client = VesselClient(os.environ.get("VESSEL_URL", "http://vessel:8700"), os.environ["VESSEL_APP_KEY"])
    history = HistoryStore(os.environ.get("DEMO_HISTORY", "./demo-data/history.sqlite"))

    def persona(value: Any) -> dict[str, str] | None:
        return PERSONAS.get(str(value or ""))

    async def index(_request: Request) -> FileResponse:
        # Images only from this page, so model-written Markdown cannot send data out through an image URL.
        return FileResponse(HERE / "static" / "index.html",
                            headers={"Cache-Control": "no-store", "Content-Security-Policy": "img-src 'self' data:"})

    async def meta(_request: Request) -> JSONResponse:
        card = await client.card(AGENT)
        manifest = await client.manifest(AGENT, PERSONAS["alice"]["id"])
        manifest["tools"] = list(manifest.get("tools") or {})
        return JSONResponse({"card": card, "manifest": manifest, "personas": PERSONAS,
                             "agents": await client.agents(PERSONAS["alice"]["id"]),
                             "projection": projection_static() is not None})

    async def conversations(request: Request) -> JSONResponse:
        who = persona(request.query_params.get("persona"))
        if who is None:
            return JSONResponse({"error": "unknown persona"}, status_code=400)
        return JSONResponse({"conversations": await history.list(who["id"])})

    async def conversation(request: Request) -> JSONResponse:
        who = persona(request.query_params.get("persona"))
        if who is None:
            return JSONResponse({"error": "unknown persona"}, status_code=400)
        cid = request.path_params["cid"]
        if request.method == "DELETE":
            ok = await history.delete(who["id"], cid)
            return JSONResponse({"deleted": ok}, status_code=200 if ok else 404)
        found = await history.get(who["id"], cid)
        return JSONResponse(found) if found else JSONResponse({"error": "not found"}, status_code=404)

    async def chat(request: Request) -> StreamingResponse | JSONResponse:
        body = await request.json()
        who = persona(body.get("persona"))
        text = str(body.get("text") or "").strip()
        if who is None or not text or len(text) > 4000:
            return JSONResponse({"error": "a persona and a question of at most 4000 characters are required"},
                                status_code=400)

        owner = who["id"]
        cid = str(body.get("conversation_id") or new_id())
        if not await history.claim(owner, cid, text):
            return JSONResponse({"error": "unknown conversation"}, status_code=404)
        past = await history.history(owner, cid)

        async def relay():
            # The app owns history: send it with the turn, then store the transcript messages that come back.
            events, recorded, answer, finished, task_id = [], [], {}, {}, ""
            async for item in client.stream(AGENT, owner, text, history=past, conversation_id=cid):
                events.append(item)
                kind = item.get("type")
                task_id = task_id or item.get("task_id") or ""
                if kind == "answer":
                    answer = item
                elif kind == "turn.finished":
                    finished = item
                if kind not in SKIPPED:
                    recorded.append({k: v for k, v in item.items() if k != "raw"})
                if kind != "transcript":
                    yield f"data: {json.dumps(item)}\n\n"
            messages, _turn = transcript_of(events)
            if messages:
                result = {k: v for k, v in finished.items() if k not in ("type", "raw", "task_id", "conversation_id",
                                                                          "state")}
                result.update(text=answer.get("text", ""), components=answer.get("components", []))
                result.setdefault("status", "model_error")
                await history.record_turn(owner, cid, task_id or new_id(), text, result, messages, recorded)

        return StreamingResponse(relay(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def cancel(request: Request) -> JSONResponse:
        body = await request.json()
        who = persona(body.get("persona"))
        if who is None or not body.get("task_id"):
            return JSONResponse({"error": "persona and task_id are required"}, status_code=400)
        return JSONResponse(await client.cancel(AGENT, who["id"], str(body["task_id"])))

    async def render(request: Request) -> JSONResponse:
        body = await request.json()
        components = body.get("components")
        if not isinstance(components, list) or projection_static() is None:
            return JSONResponse({"error": "nothing to render"}, status_code=400)
        return JSONResponse({"html": render_primitives(components)})

    astral = projection_static()
    routes = [] if astral is None else [Mount("/static/fonts", StaticFiles(directory=astral / "fonts"), name="fonts"),
                                        Mount("/astral", StaticFiles(directory=astral), name="astral")]
    routes += [
        Route("/", index),
        Route("/api/meta", meta),
        Route("/api/conversations", conversations),
        Route("/api/conversations/{cid}", conversation, methods=["GET", "DELETE"]),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/cancel", cancel, methods=["POST"]),
        Route("/api/render", render, methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    return CrossOriginAssets(Starlette(routes=routes))


class CrossOriginAssets:
    # The sandboxed preview frame has an opaque origin, so its font and script loads are cross-origin
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(("/astral/", "/static/fonts/")):
            return await self.app(scope, receive, send)

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"access-control-allow-origin", b"*")]
            await send(message)

        return await self.app(scope, receive, send_with_cors)


app = create_app() if os.environ.get("VESSEL_APP_KEY") else None
