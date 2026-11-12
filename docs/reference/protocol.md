# HTTP and A2A protocol

This page lists the vessel's HTTP endpoints, the A2A methods applications use, and the shape of a turn's request and
response. The A2A layer is served by `a2a-sdk` 1.2.2; field names follow A2A 1.0 in camelCase JSON.

## Endpoints

| Method and path | Auth | Returns |
|---|---|---|
| `GET /health` | none | `{"ok": true, "agents": {"<id>": <sandbox ready>}}` |
| `GET /v1/agents` | yes | `{"agents": [{"id", "name", "description", "card_url", "available"}]}` for the caller's agents |
| `GET /agents/<id>/.well-known/agent-card.json` | none | The agent card |
| `GET /agents/<id>/manifest` | yes | The agent's manifest summary: agent fields and id, model name and `max_steps`, tool scopes, mounts, deadline, output settings, primitives schema version |
| `POST /agents/<id>` | yes | A2A JSON-RPC |

"Auth" means an app key with `X-Vessel-User`, or a token from a configured issuer; see
[Identity, apps and issuers](../run/identity.md).

## The agent card

```json
{
  "name": "field-guide",
  "description": "Answers questions about the birds in a small sample field guide.",
  "version": "0.1.0",
  "supportedInterfaces": [{"url": "http://vessel:8700/agents/field-guide", "protocolBinding": "JSONRPC",
                           "protocolVersion": "1.0"}],
  "capabilities": {"streaming": true},
  "defaultInputModes": ["text/plain", "application/vnd.astralvessel.transcript+json"],
  "defaultOutputModes": ["text/markdown", "application/vnd.astralvessel.transcript+json",
                         "application/vnd.astral.primitives+json", "image/png", "text/csv"],
  "skills": [{"id": "field-guide", "name": "field-guide", "description": "...",
              "tags": ["astralvessel", "scope:tools:execute"], "examples": ["..."]}],
  "provider": {"organization": "AstralVessel", "url": "https://github.com/AstralDeep/AstralVessel"}
}
```

`name` and the skill `id` are the agent's deployment id; the skill `name` is the manifest's `name`. The interface URL
is `public_url` plus `/agents/<id>`. Image media types join `defaultInputModes` when the model accepts images; the
primitives type and file types join `defaultOutputModes` when the agent has them. The `scope:` tag is the strongest
scope among the agent's tools.

## A2A methods

| Method | Use |
|---|---|
| `SendStreamingMessage` | Start a turn and stream its events as server-sent events |
| `SendMessage` | Start a turn and get the finished task in one response |
| `GetTask` | Read a task you own |
| `CancelTask` | Cancel a running turn you own |

Send `A2A-Version: 1.0` with each request, and `Accept: text/event-stream` for streaming.

## Sending a turn

```bash
curl -N http://vessel:8700/agents/field-guide \
  -H "Authorization: Bearer $VESSEL_APP_KEY" -H "X-Vessel-User: alice" \
  -H "A2A-Version: 1.0" -H "Accept: text/event-stream" -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": "1", "method": "SendStreamingMessage", "params": {
        "message": {"messageId": "m1", "role": "ROLE_USER", "contextId": "conv-1",
                    "parts": [{"text": "Which birds in the guide hunt fish?"}]},
        "configuration": {"acceptedOutputModes": ["text/markdown",
                          "application/vnd.astralvessel.transcript+json"]}}}'
```

| Part of the message | Meaning |
|---|---|
| A `text` part | The user's message, at most 100,000 characters. |
| A part with `mediaType: application/vnd.astralvessel.transcript+json` | The history, as a [transcript envelope](transcript.md): either `raw` (base64 of the JSON, faster for long histories) or `data` (the envelope as JSON). |
| `raw` parts with an image media type | Attached images: PNG, JPEG, GIF or WebP, at most 8, each at most 5 MB, for agents whose model accepts images. URL parts are refused. |
| A `data` part shaped like `{"method": "tools/call", "arguments": {"question": "..."}}` | A structured skill call, used when there is no text part. The question comes from `question`, `message`, `query`, `text` or `prompt`. |
| `contextId` | The conversation id, 1 to 128 characters from letters, digits and `. _ : -`. Generated when omitted. |
| `configuration.acceptedOutputModes` | List the transcript media type to receive the transcript; list file types to limit which files you receive. |

The whole request body may be at most 16,000,000 bytes.

A turn is rejected (task state `TASK_STATE_REJECTED`, with a message saying why) when the history is malformed or
holds a system message, an image is invalid or the agent does not accept images, there is no text, the text is too
long, or the conversation id is invalid.

## What comes back

A streamed turn produces, in order:

1. A `task` frame (state `TASK_STATE_SUBMITTED`).
2. `statusUpdate` frames in state `TASK_STATE_WORKING`, each carrying one [event](events.md) as a data part of the
   status message, with `metadata["vessel.event"]` set to the event type.
3. `artifactUpdate` frames for the `transcript` artifact (if requested), appended chunk by chunk, the last with
   `lastChunk: true`, and one `file` artifact per returned file.
4. An `artifactUpdate` for the `answer` artifact: a `text/markdown` part, plus a primitives part when components
   were attached.
5. A final `statusUpdate` carrying `turn.finished`, in the task's final state.

| Turn status | Final task state |
|---|---|
| `ok`, `step_limit` | `TASK_STATE_COMPLETED` |
| `cancelled` | `TASK_STATE_CANCELED` |
| `deadline`, `model_error` | `TASK_STATE_FAILED` |

Numbers inside data parts travel as JSON numbers through a protobuf `Struct`, so integers may arrive as `1.0`. The
Python client converts whole floats back to integers.

## Cancelling

```json
{"jsonrpc": "2.0", "id": "2", "method": "CancelTask", "params": {"id": "<task id>"}}
```

The running turn stops, its tool calls and workspace are ended, and it still finishes with a `turn.finished` event
whose status is `cancelled`. Only the task's owner can cancel it.

## What the vessel stores

Each agent has a table in a SQLite task store (`/data/tasks.sqlite` in the `vessel_data` volume). A stored task
keeps its latest status, the user's text and the answer; it never keeps streamed text pieces, the history sent in,
the transcript or files. Tasks are deleted `task_ttl_hours` after their last update. Tasks that were
running when the vessel stopped are marked failed when it starts again.

## Errors

| HTTP status | JSON-RPC code | When |
|---|---|---|
| 401 | -32001 | Missing or invalid key or token, or a missing or invalid `X-Vessel-User` with an app key |
| 403 | -32003 | The caller may not call this agent |
| 503 | -32004 | The agent's sandbox is not ready |
| 413 | | The request body is over 16,000,000 bytes (body `{"error": "request too large"}`) |

Errors from the A2A layer itself, such as invalid parameters or an unknown task, come back as standard JSON-RPC
errors with HTTP 200. The non-JSON-RPC endpoints return `{"error": "..."}` with the same statuses.
