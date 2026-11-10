# The transcript format: OpenAI chat messages in a small versioned envelope, sent in as history and streamed back out.
# Applications own conversation history; the vessel checks what it is given and never accepts a system message.

from __future__ import annotations

from typing import Any

MEDIA_TYPE = "application/vnd.astralvessel.transcript+json"
VERSION = 1
MAX_MESSAGES = 2000
ROLE_KEYS = {
    "user": {"role", "content"},
    "assistant": {"role", "content", "tool_calls", "reasoning_content"},
    "tool": {"role", "tool_call_id", "content"},
}


class TranscriptError(ValueError):
    pass


def envelope(messages: list[dict[str, Any]], turn: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"version": VERSION, "messages": messages}
    if turn is not None:
        body["turn"] = turn
    return body


def parse_history(payload: Any) -> list[dict[str, Any]]:
    try:
        return _parse(payload)
    except TranscriptError:
        raise
    except Exception:  # noqa: BLE001  any other shape problem is still a refusal, never a crash
        raise TranscriptError("the history is malformed") from None


def _parse(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("version") != VERSION:
        raise TranscriptError(f"history must be a transcript envelope with version {VERSION}")
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise TranscriptError(f"history must hold a list of at most {MAX_MESSAGES} messages")
    pending: set[str] = set()
    checked = []
    for index, message in enumerate(messages):
        where = f"message {index}"
        if not isinstance(message, dict) or message.get("role") not in ROLE_KEYS:
            raise TranscriptError(f"{where} needs a role of user, assistant or tool")
        role = message["role"]
        if set(message) - ROLE_KEYS[role]:
            raise TranscriptError(f"{where} has fields a {role} message may not carry")
        if role != "tool" and pending:
            raise TranscriptError(f"{where} comes before every tool call was answered")
        if role == "tool":
            call = message.get("tool_call_id")
            if not isinstance(call, str) or call not in pending or not isinstance(message.get("content"), str):
                raise TranscriptError(f"{where} answers no open tool call")
            pending.discard(call)
        elif role == "user":
            _check_user_content(message.get("content"), where)
        else:
            _check_assistant(message, where, pending)
        checked.append(dict(message))
    if pending:
        raise TranscriptError("the history ends with unanswered tool calls")
    return checked


IMAGE_URL = ("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/gif;base64,", "data:image/webp;base64,")
MAX_IMAGE_URL_CHARS = 7_000_000


def _check_user_content(content: Any, where: str) -> None:
    # Text, or OpenAI content parts: text and inline images (data: URLs only, never a link the model would fetch).
    if isinstance(content, str):
        return
    if not isinstance(content, list) or not content:
        raise TranscriptError(f"{where} needs text content or a list of content parts")
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text" and set(part) == {"type", "text"} and isinstance(part["text"], str):
            continue
        image = part.get("image_url") if kind == "image_url" and set(part) == {"type", "image_url"} else None
        url = image.get("url") if isinstance(image, dict) and set(image) <= {"url", "detail"} else None
        if not isinstance(url, str) or not url.startswith(IMAGE_URL) or len(url) > MAX_IMAGE_URL_CHARS:
            raise TranscriptError(f"{where} has a content part that is not text or an inline image")


def _check_assistant(message: dict[str, Any], where: str, pending: set[str]) -> None:
    if message.get("content") is not None and not isinstance(message["content"], str):
        raise TranscriptError(f"{where} content must be text or null")
    if "reasoning_content" in message and not isinstance(message["reasoning_content"], str):
        raise TranscriptError(f"{where} reasoning_content must be text")
    calls = message.get("tool_calls")
    if calls is not None and not isinstance(calls, list):
        raise TranscriptError(f"{where} tool_calls must be a list")
    for call in calls or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if (not isinstance(fn, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str)
                or not isinstance(fn.get("name"), str) or not isinstance(fn.get("arguments"), str)
                or set(call) - {"id", "type", "function"} or set(fn) - {"name", "arguments"}):
            raise TranscriptError(f"{where} has a malformed tool call")
        pending.add(call["id"])


def for_model(messages: list[dict[str, Any]], send_reasoning: bool) -> list[dict[str, Any]]:
    if send_reasoning:
        return messages
    return [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]
