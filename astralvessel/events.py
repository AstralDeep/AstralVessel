# The event vocabulary of one turn, shared by the loop, the A2A server and the session store.
# Each event is a plain dict with a "type" key so it serializes the same way everywhere.

from __future__ import annotations

from typing import Any

TURN_STARTED = "turn.started"
TEXT_DELTA = "text.delta"
REASONING_DELTA = "reasoning.delta"
TOOL_STARTED = "tool.started"
TOOL_FINISHED = "tool.finished"
STEP_FINISHED = "step.finished"
TURN_FINISHED = "turn.finished"
# Carries a tool's file as bytes; the server sends it as an artifact, never as a status message.
FILE_ADDED = "file.added"

EVENT_TYPES = (
    TURN_STARTED, TEXT_DELTA, REASONING_DELTA, TOOL_STARTED, TOOL_FINISHED, STEP_FINISHED, TURN_FINISHED,
    FILE_ADDED,
)

STATUS_OK = "ok"
STATUS_CANCELLED = "cancelled"
STATUS_DEADLINE = "deadline"
STATUS_STEP_LIMIT = "step_limit"
STATUS_MODEL_ERROR = "model_error"
STATUS_TOOL_ERROR = "tool_error"

STATUSES = (
    STATUS_OK, STATUS_CANCELLED, STATUS_DEADLINE, STATUS_STEP_LIMIT, STATUS_MODEL_ERROR, STATUS_TOOL_ERROR,
)

PRIMITIVES_MEDIA_TYPE = "application/vnd.astral.primitives+json"


def event(kind: str, **fields: Any) -> dict[str, Any]:
    if kind not in EVENT_TYPES:
        raise ValueError(f"unknown event type {kind}")
    return {"type": kind, **fields}
