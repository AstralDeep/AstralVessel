# An application-side conversation store in one SQLite file, keyed by the owning user; the vessel itself keeps none.
# Apps send history() with each turn and record_turn() the transcript messages that come back.

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(owner, updated);
CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, created REAL NOT NULL, user_text TEXT NOT NULL, status TEXT NOT NULL,
  result TEXT NOT NULL, messages TEXT NOT NULL, events TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id, seq);
"""


class HistoryStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        self._lock = asyncio.Lock()

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def _owned(self, owner: str, conversation_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM conversations WHERE id = ? AND owner = ?",
                                (conversation_id, owner)).fetchone()

    async def claim(self, owner: str, conversation_id: str, title: str) -> bool:
        def work() -> bool:
            row = self._db.execute("SELECT owner FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            now = time.time()
            if row is not None:
                if row["owner"] != owner:
                    return False
                # Touch it now, so the idle sweep cannot delete a conversation while its turn runs.
                self._db.execute("UPDATE conversations SET updated = ? WHERE id = ?", (now, conversation_id))
                return True
            self._db.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                             (conversation_id, owner, title[:120] or "Conversation", now, now))
            return True
        return await self._run(work)

    async def history(self, owner: str, conversation_id: str) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            if self._owned(owner, conversation_id) is None:
                return []
            messages: list[dict[str, Any]] = []
            for row in self._db.execute("SELECT messages FROM turns WHERE conversation_id = ? ORDER BY seq",
                                        (conversation_id,)):
                messages.extend(json.loads(row["messages"]))
            return messages
        return await self._run(work)

    async def record_turn(self, owner: str, conversation_id: str, turn_id: str, user_text: str,
                          result: dict[str, Any], messages: list[dict[str, Any]],
                          events: list[dict[str, Any]]) -> None:
        def work() -> None:
            if self._owned(owner, conversation_id) is None:
                return
            seq = self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE conversation_id = ?",
                                   (conversation_id,)).fetchone()[0]
            self._db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (turn_id, conversation_id, seq, time.time(), user_text, result["status"],
                              json.dumps(result), json.dumps(messages), json.dumps(events)))
            self._db.execute("UPDATE conversations SET updated = ? WHERE id = ?", (time.time(), conversation_id))
        await self._run(work)

    async def list(self, owner: str, limit: int = 50) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = c.id) AS turns "
                "FROM conversations c WHERE owner = ? ORDER BY updated DESC LIMIT ?", (owner, limit))
            return [{"id": r["id"], "title": r["title"], "created": r["created"], "updated": r["updated"],
                     "turns": r["turns"]} for r in rows]
        return await self._run(work)

    async def get(self, owner: str, conversation_id: str) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            row = self._owned(owner, conversation_id)
            if row is None:
                return None
            turns = [{"id": t["id"], "created": t["created"], "user_text": t["user_text"], "status": t["status"],
                      "result": json.loads(t["result"]), "events": json.loads(t["events"])}
                     for t in self._db.execute("SELECT * FROM turns WHERE conversation_id = ? ORDER BY seq",
                                               (conversation_id,))]
            return {"id": row["id"], "title": row["title"], "created": row["created"], "updated": row["updated"],
                    "turns": turns}
        return await self._run(work)

    async def delete(self, owner: str, conversation_id: str) -> bool:
        def work() -> bool:
            cur = self._db.execute("DELETE FROM conversations WHERE id = ? AND owner = ?", (conversation_id, owner))
            return cur.rowcount > 0
        return await self._run(work)

    async def sweep(self, idle_seconds: float) -> list[tuple[str, str]]:
        def work() -> list[tuple[str, str]]:
            rows = self._db.execute("DELETE FROM conversations WHERE updated < ? RETURNING owner, id",
                                    (time.time() - idle_seconds,)).fetchall()
            return [(r["owner"], r["id"]) for r in rows]
        return await self._run(work)


def new_id() -> str:
    return uuid.uuid4().hex
