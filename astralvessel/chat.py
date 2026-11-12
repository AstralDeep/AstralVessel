# A terminal client for one agent: ask streams a single turn; chat keeps a conversation, stores it with history.py
# and can resume it. astralvessel chat and ask run this inside a container on the deployment's front network.

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Any

from .client import VesselClient, transcript_of
from .history import HistoryStore, new_id

OWNER = "local"
URL = "http://vessel:8700"


def _say(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _save(folder: Path, event: dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(event.get("name") or "file")  # the vessel already checked it; never trust a path
    target = folder / name
    stem, dot, ext = name.rpartition(".")
    n = 1
    while target.exists():
        n += 1
        target = folder / (f"{stem}-{n}.{ext}" if dot else f"{name}-{n}")
    target.write_bytes(event["data"])
    return target


def _kept(event: dict[str, Any]) -> dict[str, Any]:
    # What the store keeps of an event: no raw frames and no file bytes.
    return {k: v for k, v in event.items() if k not in ("raw", "data")}


async def turn(client: VesselClient, agent: str, text: str, files: Path, shown: Path,
               history: list[dict[str, Any]] | None = None,
               conversation_id: str | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    streamed = False
    task_id = ""
    stream = client.stream(agent, OWNER, text, history=history, conversation_id=conversation_id)
    try:
        # aclosing ends the HTTP stream at once on a cancel, so the vessel sees the caller leave even before
        # the task id has arrived.
        async with contextlib.aclosing(stream):
            async for event in stream:
                events.append(event)
                kind = event["type"]
                if kind == "task":
                    task_id = event.get("task_id") or ""
                elif kind == "text.delta":
                    streamed = True
                    _say(event.get("content", ""))
                elif kind == "tool.started":
                    _say(f"\n[{event.get('name')}]\n")
                elif kind == "file":
                    saved = _save(files, event)
                    _say(f"\n[saved {shown / saved.relative_to(files.parent)}]\n")
                elif kind == "answer":
                    if not streamed and event.get("text"):
                        _say(event["text"])
                    if event.get("components"):
                        _say(f"\n[{len(event['components'])} interface components]")
                elif kind == "turn.finished":
                    status = event.get("status")
                    _say("\n" if status == "ok" else f"\n[{status}]\n")
                elif kind == "error":
                    _say(f"\n[error: {event.get('detail') or event.get('status')}]\n")
    except asyncio.CancelledError:
        # Ctrl-C while an answer streams: stop the turn on the vessel too, then carry on.
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        if task_id:
            await client.cancel(agent, OWNER, task_id)
        _say("\n[stopped]\n")
    return events


async def ask(agent: str, question: str, chats: Path) -> int:
    client = VesselClient(URL, os.environ["VESSEL_APP_KEY"])
    try:
        events = await turn(client, agent, question, chats / "files" / "ask", Path("chats"))
    finally:
        await client.close()
    finished = [e for e in events if e["type"] == "turn.finished"]
    return 0 if finished and finished[-1].get("status") == "ok" else 1


async def _chat_turn(store: HistoryStore, agent: str, text: str, conversation: str, chats: Path) -> None:
    # One turn on its own event loop, so Ctrl-C at the prompt is a plain interrupt and during an answer a cancel.
    client = VesselClient(URL, os.environ["VESSEL_APP_KEY"])
    try:
        await store.claim(OWNER, conversation, text)
        history = await store.history(OWNER, conversation)
        events = await turn(client, agent, text, chats / "files" / conversation, Path("chats"),
                            history=history, conversation_id=conversation)
    finally:
        await client.close()
    messages, _ = transcript_of(events)
    finished = next((e for e in reversed(events) if e["type"] == "turn.finished"), None)
    if finished and messages:
        await store.record_turn(OWNER, conversation, new_id(), text, _kept(finished), messages,
                                [_kept(e) for e in events])


def chat(agent: str, chats: Path, resume: str | None, list_only: bool) -> int:
    store = HistoryStore(chats / "chats.sqlite")
    if list_only:
        rows = asyncio.run(store.list(OWNER))
        for row in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["updated"]))
            print(f"{row['id']}  {when}  {row['turns']:>3} turns  {row['title']}")
        if not rows:
            print("no saved chats")
        return 0
    if resume and asyncio.run(store.get(OWNER, resume)) is None:
        print(f"no saved chat {resume}; list them with --list")
        return 1
    conversation = resume or new_id()
    print(f"chatting with {agent} (chat {conversation}); /exit or Ctrl-D to leave, Ctrl-C stops an answer")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text in ("/exit", "/quit"):
            break
        if text:
            try:
                asyncio.run(_chat_turn(store, agent, text, conversation, chats))
            except KeyboardInterrupt:
                # Ctrl-C outside the stream (while loading history, or a second press during the cancel).
                print("\n[stopped]")
    print(f"saved; resume with: astralvessel chat {agent} --resume {conversation}")
    return 0


def main(args: argparse.Namespace) -> None:
    chats = Path(os.environ.get("VESSEL_CHATS", "/chats"))
    if args.mode == "ask":
        code = asyncio.run(ask(args.agent, args.question, chats))
    else:
        code = chat(args.agent, chats, args.resume, args.list)
    raise SystemExit(code)
