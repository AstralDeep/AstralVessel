# A small A2A 1.0 JSON-RPC client for apps in front of a Vessel: list agents, stream a turn with its history, cancel.
# It yields Vessel events decoded from the A2A status and artifact updates, plus the raw frames for inspection.

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

PRIMITIVES_MEDIA_TYPE = "application/vnd.astral.primitives+json"
TRANSCRIPT_MEDIA_TYPE = "application/vnd.astralvessel.transcript+json"


class VesselClient:
    def __init__(self, base_url: str, app_key: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, transport=transport,
                                         headers={"Authorization": f"Bearer {app_key}"},
                                         timeout=httpx.Timeout(30, read=900))

    async def close(self) -> None:
        await self._client.aclose()

    def _user(self, user: str) -> dict[str, str]:
        return {"X-Vessel-User": user}

    async def rpc(self, agent: str, user: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(f"/agents/{agent}", headers={**self._user(user), "A2A-Version": "1.0"},
                                           json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method,
                                                 "params": params})
        return response.json()

    async def stream(self, agent: str, user: str, text: str, *, history: list[dict[str, Any]] | None = None,
                     conversation_id: str | None = None, images: list[tuple[str, bytes]] | None = None
                     ) -> AsyncIterator[dict[str, Any]]:
        # images: (media type, bytes) pairs, for agents whose model accepts images.
        parts: list[dict[str, Any]] = [{"text": text}]
        for index, (media_type, raw) in enumerate(images or []):
            parts.append({"raw": base64.b64encode(raw).decode(), "mediaType": media_type,
                          "filename": f"image-{index + 1}"})
        if history:
            # As JSON bytes: the A2A layer validates a structured data part field by field, slowly when it is long.
            body = json.dumps({"version": 1, "messages": history}).encode()
            parts.append({"raw": base64.b64encode(body).decode(), "mediaType": TRANSCRIPT_MEDIA_TYPE,
                          "filename": "history.json"})
        message: dict[str, Any] = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": parts}
        if conversation_id:
            message["contextId"] = conversation_id
        # Asking for the transcript media type is what makes the vessel stream the turn's messages back.
        accepted = ["text/markdown", TRANSCRIPT_MEDIA_TYPE, PRIMITIVES_MEDIA_TYPE]
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "SendStreamingMessage",
                "params": {"message": message, "configuration": {"acceptedOutputModes": accepted}}}
        headers = {**self._user(user), "Accept": "text/event-stream", "A2A-Version": "1.0"}
        async with self._client.stream("POST", f"/agents/{agent}", headers=headers, json=body) as response:
            if response.status_code != 200:
                await response.aread()
                yield {"type": "error", "status": response.status_code, "detail": response.text[:500]}
                return
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    frame = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                for item in decode(frame):
                    yield item

    async def cancel(self, agent: str, user: str, task_id: str) -> dict[str, Any]:
        return await self.rpc(agent, user, "CancelTask", {"id": task_id})

    async def agents(self, user: str) -> list[dict[str, Any]]:
        response = await self._client.get("/v1/agents", headers=self._user(user))
        return response.json().get("agents", []) if response.status_code == 200 else []

    async def manifest(self, agent: str, user: str) -> dict[str, Any]:
        return (await self._client.get(f"/agents/{agent}/manifest", headers=self._user(user))).json()

    async def card(self, agent: str) -> dict[str, Any]:
        return (await self._client.get(f"/agents/{agent}/.well-known/agent-card.json")).json()


def transcript_of(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    # The messages a turn added, in order, and its closing summary; store the messages to send as later history.
    messages: list[dict[str, Any]] = []
    turn = None
    for item in events:
        if item.get("type") == "transcript":
            messages.extend(item["messages"])
            turn = item.get("turn") or turn
    return messages, turn


def integral(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: integral(v) for k, v in value.items()}
    if isinstance(value, list):
        return [integral(v) for v in value]
    return value


def _data(part: dict[str, Any]) -> Any:
    return integral(part.get("data"))


def decode(frame: dict[str, Any]) -> list[dict[str, Any]]:
    if "error" in frame:
        return [{"type": "error", "detail": frame["error"].get("message", "error"), "raw": frame}]
    result = frame.get("result") or {}
    out: list[dict[str, Any]] = []
    if "task" in result:
        task = result["task"]
        out.append({"type": "task", "task_id": task.get("id"), "conversation_id": task.get("contextId"),
                    "state": (task.get("status") or {}).get("state"), "raw": frame})
    if "statusUpdate" in result:
        update = result["statusUpdate"]
        status = update.get("status") or {}
        parts = (status.get("message") or {}).get("parts") or []
        vessel = next((_data(p) for p in parts if isinstance(_data(p), dict) and "type" in _data(p)), None)
        if vessel:
            out.append({**vessel, "task_id": update.get("taskId"), "conversation_id": update.get("contextId"),
                        "state": status.get("state"), "raw": frame})
        else:
            text = " ".join(p.get("text", "") for p in parts if "text" in p)
            out.append({"type": "status", "state": status.get("state"), "text": text,
                        "task_id": update.get("taskId"), "raw": frame})
    if "artifactUpdate" in result and (result["artifactUpdate"].get("artifact") or {}).get("name") == "transcript":
        artifact = result["artifactUpdate"]["artifact"]
        for part in artifact.get("parts") or []:
            if part.get("mediaType") == TRANSCRIPT_MEDIA_TYPE:
                body = _data(part) or {}
                out.append({"type": "transcript", "messages": body.get("messages", []), "turn": body.get("turn"),
                            "last": bool(result["artifactUpdate"].get("lastChunk")), "raw": frame})
    elif "artifactUpdate" in result:
        artifact = (result["artifactUpdate"].get("artifact") or {})
        text, components, version = "", [], None
        for part in artifact.get("parts") or []:
            if "text" in part:
                text += part["text"]
            elif part.get("mediaType") == PRIMITIVES_MEDIA_TYPE:
                body = _data(part) or {}
                components = body.get("_ui_components") or body.get("components") or []
                version = (part.get("metadata") or {}).get("schema_version")
        out.append({"type": "answer", "text": text, "components": components, "schema_version": version,
                    "raw": frame})
    return out
