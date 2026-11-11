# Streams one OpenAI-compatible chat completion, yielding text, reasoning and the finished tool calls.
# Model quirks live here: GLM returns thinking in reasoning_content and splits tool-call arguments across chunks.

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("astralvessel")

RETRYABLE = {429, 500, 502, 503, 504}


class ModelError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class StepResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


def resolve_setting(value: str) -> str:
    if value.startswith("env:"):
        name = value[4:]
        resolved = os.environ.get(name, "")
        if not resolved:
            raise ModelError(f"model setting {name} is not configured")
        return resolved
    return value


class ModelClient:
    def __init__(self, endpoint: str, api_key: str, model: str, *, temperature: float = 0.2,
                 max_output_tokens: int = 8192, transport: httpx.AsyncBaseTransport | None = None,
                 retries: int = 3):
        self.url = endpoint.rstrip("/") + "/chat/completions"
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self._client = httpx.AsyncClient(
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=15, read=180, write=30, pool=15),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
                     ) -> AsyncIterator[tuple[str, Any]]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        if tools:
            body["tools"] = tools
        for attempt in range(self.retries + 1):
            started = False
            try:
                async with self._client.stream("POST", self.url, json=body) as response:
                    if response.status_code != 200:
                        if response.status_code in RETRYABLE and attempt < self.retries:
                            await response.aread()
                            await asyncio.sleep(min(2 ** attempt, 8))
                            continue
                        body = (await response.aread())[:500].decode("utf-8", "replace")
                        # Logged for the operator only; the caller sees the status alone.
                        log.warning("model endpoint returned HTTP %s: %s", response.status_code, body)
                        raise ModelError(f"model endpoint returned HTTP {response.status_code}")
                    result = StepResult()
                    calls: dict[int, dict[str, str]] = {}
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload, parse_constant=_no_constants)
                        except ValueError:
                            continue
                        started = True
                        if chunk.get("usage"):
                            result.usage = {
                                "input": int(chunk["usage"].get("prompt_tokens") or 0),
                                "output": int(chunk["usage"].get("completion_tokens") or 0),
                            }
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            thinking = _text(delta.get("reasoning_content") or delta.get("reasoning"))
                            if thinking:
                                result.reasoning += thinking
                                yield "reasoning", thinking
                            content = _text(delta.get("content"))
                            if content:
                                result.text += content
                                yield "text", content
                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(_index(tc.get("index")),
                                                        {"id": "", "name": "", "arguments": ""})
                                slot["id"] = tc.get("id") or slot["id"]
                                fn = tc.get("function") or {}
                                slot["name"] += _text(fn.get("name"))
                                slot["arguments"] += _text(fn.get("arguments"))
                            if choice.get("finish_reason"):
                                result.finish_reason = choice["finish_reason"]
                    result.tool_calls = [
                        ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=c["arguments"] or "{}")
                        for i, c in sorted(calls.items()) if c["name"]
                    ]
                    yield "done", result
                    return
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if started or attempt >= self.retries:
                    raise ModelError(f"model endpoint unreachable ({type(exc).__name__})") from None
                await asyncio.sleep(min(2 ** attempt, 8))
        raise ModelError("model endpoint kept failing")


def _no_constants(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def _text(value: Any) -> str:
    # Model text can carry lone surrogates, which cannot be encoded later; replace them.
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", "replace").decode("utf-8")


def _index(value: Any) -> int:
    return value if isinstance(value, int) and 0 <= value < 64 else 0
