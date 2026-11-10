# Shared fixtures: a scripted OpenAI-compatible model, a tiny agent package, and an in-process Vessel app.
# The scripted model replies from a queue so every test controls exactly what the agent sees.

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

APP_KEY = "test-app-key"


def sse(chunks: list[dict[str, Any]]) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def text_reply(text: str, usage: tuple[int, int] = (10, 5)) -> list[dict[str, Any]]:
    half = len(text) // 2
    return [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"index": 0, "delta": {"content": text[:half]}}]},
        {"choices": [{"index": 0, "delta": {"content": text[half:]}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}},
    ]


def tool_reply(name: str, arguments: dict[str, Any] | str, call_id: str = "call_1") -> list[dict[str, Any]]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return [
        {"choices": [{"index": 0, "delta": {"content": "Looking. "}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": raw[:5]}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": raw[5:]}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 8}},
    ]


class ScriptedModel:
    def __init__(self, replies: list[Any] | None = None):
        self.replies = list(replies or [])
        self.requests: list[dict[str, Any]] = []
        self.delay = 0.0

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            if self.delay:
                await asyncio.sleep(self.delay)
            reply = self.replies.pop(0) if self.replies else text_reply("default answer")
            if isinstance(reply, int):
                return httpx.Response(reply, json={"error": "upstream detail with secret-dsn://user:pw@db"})
            return httpx.Response(200, content=sse(reply), headers={"content-type": "text/event-stream"})
        return httpx.MockTransport(handler)


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    (root / "tools").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "notes").mkdir(parents=True)
    (work / "notes" / "ada-lovelace.md").write_text("---\ntitle: Ada Lovelace\n---\n# Ada\nAnalytical engine.\n")
    (root / "prompt.md").write_text("You are a test agent.")
    (root / "tools" / "echo.toml").write_text(
        'name = "echo"\ndescription = "Echo the input"\ncommand = ["python3", "-c", '
        '"import sys,json; print(json.dumps(json.load(sys.stdin)))"]\n'
        '[parameters]\ntype = "object"\n[parameters.properties.word]\ntype = "string"\n')
    (root / "vessel.toml").write_text(f"""
[agent]
name = "tester"
description = "A test agent."
examples = ["hello"]

[model]
endpoint = "http://model.test/v1"
api_key = "model-key"
model = "test-model"
max_steps = 4

[tools]
builtin = ["read", "grep", "glob", "list", "bash"]
declared = ["tools/echo.toml"]

[sandbox]
workdir = "{work}"
deadline_seconds = 20
tool_timeout_seconds = 5

[output]
primitives = true
""")
    return root
