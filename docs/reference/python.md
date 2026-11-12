# Python client and history store

The package includes a small A2A client for applications (`astralvessel.client`) and an application-side
conversation store (`astralvessel.history`). Both are optional; any A2A client works.

## VesselClient

```python
import asyncio
from astralvessel.client import VesselClient, transcript_of

async def main():
    client = VesselClient("http://vessel:8700", "app-key")
    events = [e async for e in client.stream("field-guide", "alice", "Which birds hunt fish?")]
    print(next(e["text"] for e in events if e["type"] == "answer"))
    messages, turn = transcript_of(events)   # store these as history
    await client.close()

asyncio.run(main())
```

`VesselClient(base_url, app_key, *, transport=None)` sends `Authorization: Bearer <app_key>` on every request. A
signed token from a configured issuer works in place of the app key. `transport` is an optional
`httpx.AsyncBaseTransport`, useful in tests.

| Method | Returns |
|---|---|
| `stream(agent, user, text, *, history=None, conversation_id=None, images=None)` | An async iterator of event dicts for one turn |
| `cancel(agent, user, task_id)` | The JSON-RPC response to `CancelTask` |
| `rpc(agent, user, method, params)` | Any JSON-RPC call, as the decoded response |
| `agents(user)` | The caller's agents from `/v1/agents`, or `[]` on an error status |
| `manifest(agent, user)` | The agent's `/manifest` |
| `card(agent)` | The agent card |
| `close()` | Closes the HTTP client |

`stream` sends the history as base64 JSON bytes, `images` as `(media type, bytes)` pairs, and asks for markdown, the
transcript, primitives and all four file types. Reads time out after 900 seconds.

### Events from stream

Each item is a dict with a `type`. Every item except `error` from a refused request also has `raw`, the decoded A2A
frame.

| `type` | Keys |
|---|---|
| `task` | `task_id`, `conversation_id`, `state` |
| An [event type](events.md) such as `text.delta` or `turn.finished` | The event's fields, plus `task_id`, `conversation_id` and `state` |
| `status` | `state`, `text`, `task_id`: a status message that is not an event, such as a rejection reason |
| `transcript` | `messages`, `turn`, `last` |
| `file` | `name`, `media_type`, `data` (bytes), `call_id` |
| `answer` | `text`, `components`, `schema_version` |
| `error` | `status` and `detail` for a refused HTTP request; `detail` and `raw` for a JSON-RPC error |

`transcript_of(events)` returns the turn's messages in order and its closing summary (or `None`). `decode(frame)`
turns one A2A JSON frame into these items.

## HistoryStore

A conversation store in one SQLite file, keyed by owner. Every method takes the owner, and a conversation is
visible only to the owner who claimed it.

```python
from astralvessel.history import HistoryStore, new_id

store = HistoryStore("conversations.sqlite")     # or ":memory:"
conversation = new_id()
await store.claim("alice", conversation, "Fish-eating birds")
history = await store.history("alice", conversation)
# ... run the turn with history=history, then:
await store.record_turn("alice", conversation, task_id, question, turn, messages, events=[])
```

| Method | Does |
|---|---|
| `claim(owner, conversation_id, title)` | Creates the conversation, or touches it if this owner has it. Returns `False` if another owner has it. |
| `history(owner, conversation_id)` | Every stored message, in order; `[]` if not this owner's |
| `record_turn(owner, conversation_id, turn_id, user_text, result, messages, events)` | Appends a turn. `result` must have a `status`; `result` and `events` are stored as JSON, so drop `raw` and file bytes first. |
| `list(owner, limit=50)` | The owner's conversations, most recently updated first, with turn counts |
| `get(owner, conversation_id)` | A conversation with its turns, or `None` |
| `delete(owner, conversation_id)` | Deletes it and its turns; returns whether it existed |
| `sweep(idle_seconds)` | Deletes conversations idle longer than this; returns `(owner, id)` pairs |

`new_id()` returns a random 32-character hex id, which is valid as a `contextId`.
