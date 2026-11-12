# Connecting an application

Your application reaches the vessel over the deployment's internal `front` network, and the vessel reaches your
application the same way for service tools. This page shows how to attach an application and what it must do on
each turn.

## Attach to the front network

The generated Compose file publishes no ports. Attach your application's container to the `front` network, which
Compose names `<project>_front` (`astralvessel_front` by default):

```yaml
# your application's compose.yml
services:
  app:
    image: my-app:latest
    environment:
      VESSEL_URL: http://vessel:8700
    networks: [default, vessel]

networks:
  vessel:
    external: true
    name: astralvessel_front
```

The vessel listens at `front_address` on port 8700, and containers on `front` reach it by name as
`http://vessel:8700`. The `front` network is internal, so it gives no route out;
keep your application on its own network too for anything else it needs. A service grant's `url` names your
application as the vessel would reach it on this network.

Set `[vessel] public_url` to the base URL your application uses, so agent cards and `/v1/agents` show reachable
addresses.

## What your application does on each turn

1. **Authenticate** with its app key and name the user in `X-Vessel-User` (or send the user's token).
2. **Send the history.** The vessel keeps no conversations. Send the messages stored from earlier turns as a
   transcript part, and a stable conversation id as the message's `contextId`.
3. **Ask for the transcript.** List `application/vnd.astralvessel.transcript+json` in `acceptedOutputModes` to get
   the turn's new messages back.
4. **Store the new messages** and send them with the next turn.
5. **Store files** from `file` artifacts if you want to keep them; the vessel does not.

The bundled [Python client](../reference/python.md) does steps 1 to 3, and its history store does step 4:

```python
from astralvessel.client import VesselClient, transcript_of
from astralvessel.history import HistoryStore, new_id

client = VesselClient("http://vessel:8700", app_key)
store = HistoryStore("conversations.sqlite")

async def ask(user: str, conversation_id: str, question: str) -> str:
    await store.claim(user, conversation_id, question[:60])
    history = await store.history(user, conversation_id)
    events = [e async for e in client.stream("field-guide", user, question, history=history,
                                             conversation_id=conversation_id)]
    messages, turn = transcript_of(events)
    task_id = next(e["task_id"] for e in events if e["type"] == "task")
    await store.record_turn(user, conversation_id, task_id, question, turn or {"status": "unknown"}, messages, [])
    return next((e["text"] for e in events if e["type"] == "answer"), "")
```

Start a conversation with `conversation_id = new_id()`. To call agents from another language, see
[HTTP and A2A protocol](../reference/protocol.md).

## Calling from an orchestrator

An orchestrator that wants only the answer can send a non-streaming `SendMessage` with a structured skill call, the
shape Astral uses for external agents:

```json
{"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": {
  "messageId": "m1", "role": "ROLE_USER",
  "parts": [{"data": {"method": "tools/call", "name": "field-guide",
                      "arguments": {"question": "Which birds hunt fish?"}}}]}}}
```

The question is read from the first of `question`, `message`, `query`, `text` or `prompt` in `arguments`. The
completed task's `answer` artifact carries the Markdown answer and any components under `_ui_components`. Without
the transcript media type in `acceptedOutputModes`, no transcript is streamed.

## Readiness

`GET /health` (no credentials) returns whether each agent's sandbox is ready:

```json
{"ok": true, "agents": {"field-guide": true, "field-guide-reader": true}}
```

Calls to an agent whose sandbox is not ready get 503. The vessel checks sandboxes every 5 seconds until all are
ready, then every 60 seconds.
