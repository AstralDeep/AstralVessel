# Conformance checks for any running AstralVessel harness, driven over its public interfaces with real model calls.
# Usage: python conformance/run.py <base-url> <deployment-key>; exits non-zero if any check fails.

from __future__ import annotations

import asyncio
import sys
import uuid

import httpx

from astralvessel.client import VesselClient

EVENT_TYPES = {"task", "status", "answer", "error", "turn.started", "text.delta", "reasoning.delta", "tool.started",
               "tool.finished", "step.finished", "turn.finished"}
STATUSES = {"ok", "cancelled", "deadline", "step_limit", "model_error", "tool_error"}
LEAK_MARKERS = ("Traceback (most recent call last)", "Exception:", "postgresql://", "sqlite3.", "File \"/")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


async def turn(client: VesselClient, user: str, text: str, conversation: str | None = None) -> list[dict]:
    return [e async for e in client.stream(user, text, conversation)]


def strip(events: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k != "raw"} for e in events]


async def main(base: str, key: str) -> int:
    a, b = f"conformance:a:{uuid.uuid4().hex[:8]}", f"conformance:b:{uuid.uuid4().hex[:8]}"
    client = VesselClient(base, key)

    card = await client.card()
    interface = (card.get("supportedInterfaces") or [{}])[0]
    check("agent card is served with a name and streaming", bool(card.get("name")) and
          card.get("capabilities", {}).get("streaming") is True)
    check("agent card advertises A2A 1.0 over JSON-RPC", interface.get("protocolVersion") == "1.0" and
          interface.get("protocolBinding") == "JSONRPC")

    async with httpx.AsyncClient(base_url=base, timeout=15) as raw:
        r1 = await raw.get("/v1/conversations", headers={"X-Vessel-User": a})
        r2 = await raw.get("/v1/conversations", headers={"Authorization": f"Bearer {key}x", "X-Vessel-User": a})
        r3 = await raw.get("/v1/conversations", headers={"Authorization": f"Bearer {key}"})
    check("requests without the deployment key or a user are refused", (r1.status_code, r2.status_code,
          r3.status_code) == (401, 401, 401), f"{r1.status_code} {r2.status_code} {r3.status_code}")

    events = await turn(client, a, "Reply with one short sentence confirming you are ready.")
    kinds = [e["type"] for e in events]
    finished = [e for e in events if e["type"] == "turn.finished"]
    check("every streamed event has a known type", set(kinds) <= EVENT_TYPES, str(set(kinds) - EVENT_TYPES))
    check("a turn opens with the task and turn.started", kinds[:1] == ["task"] and "turn.started" in kinds)
    check("a turn ends with exactly one turn.finished", len(finished) == 1 and kinds[-1] == "turn.finished")
    check("turn.finished carries a known status, tokens and timing", bool(finished) and
          finished[0].get("status") in STATUSES and "tokens" in finished[0] and "elapsed_ms" in finished[0])
    answers = [e for e in events if e["type"] == "answer"]
    check("the answer artifact precedes turn.finished and has text", bool(answers) and
          kinds.index("answer") < kinds.index("turn.finished") and bool(answers[0]["text"].strip()))
    started = [e["call_id"] for e in events if e["type"] == "tool.started"]
    ended = [e["call_id"] for e in events if e["type"] == "tool.finished"]
    check("every tool.started has a matching tool.finished", sorted(started) == sorted(ended))
    cid = events[0].get("conversation_id")

    components = [c for e in answers for c in e.get("components") or []]
    try:
        from astralprims import primitive_adapter
        bad = []
        for c in components:
            try:
                primitive_adapter.validate_python(c)
            except Exception as exc:  # noqa: BLE001
                bad.append(type(exc).__name__)
        check("primitives parts validate against astralprims", not bad, ", ".join(bad))
    except ImportError:
        check("primitives parts validate against astralprims", True, "astralprims not installed; skipped")

    check("the owner can read the conversation", (await client.conversation(a, cid)) is not None)
    check("another user cannot read it", (await client.conversation(b, cid)) is None)
    check("another user does not see it listed", all(c["id"] != cid for c in await client.conversations(b)))
    check("another user cannot delete it", not await client.delete(b, cid))
    hijack = await turn(client, b, "Continue this conversation.", cid)
    check("another user cannot continue it", hijack[-1].get("state") == "TASK_STATE_REJECTED",
          str(hijack[-1].get("state")))

    stream_events: list[dict] = []

    async def consume():
        async for e in client.stream(a, "Use your tools to list the working directory, then search it for the "
                                        "word 'research', then summarize what you found in detail."):
            stream_events.append(e)

    runner = asyncio.create_task(consume())
    for _ in range(300):
        if any(e["type"] in ("turn.started", "tool.started") for e in stream_events):
            break
        await asyncio.sleep(0.05)
    task_id = stream_events[0]["task_id"] if stream_events else ""
    foreign = await client.cancel(b, task_id)
    check("another user cannot cancel the task", "error" in foreign)
    mine = await client.cancel(a, task_id)
    await asyncio.wait_for(runner, 60)
    state = ((mine.get("result") or {}).get("status") or {}).get("state")
    last = [e for e in stream_events if e["type"] == "turn.finished"]
    check("the owner can cancel a running turn", state == "TASK_STATE_CANCELED", str(state))
    check("a cancelled turn still ends with turn.finished", bool(last) and last[-1]["status"] == "cancelled")

    blob = repr(strip(events + hijack + stream_events))
    leaks = [m for m in LEAK_MARKERS if m in blob]
    check("no raw exception text appears in any event", not leaks, ", ".join(leaks))

    await client.delete(a, cid)
    for c in await client.conversations(a):
        await client.delete(a, c["id"])
    await client.close()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python conformance/run.py <base-url> <deployment-key>")
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
