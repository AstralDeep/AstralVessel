# Events

Every step of a turn streams as an event: a JSON object with a `type`, carried as the data part of an A2A status
update. This page lists each event's fields.

```json
{"result": {"statusUpdate": {"taskId": "bf11...", "contextId": "conv-1", "status": {
  "state": "TASK_STATE_WORKING",
  "message": {"role": "ROLE_AGENT", "metadata": {"vessel.event": "tool.started"},
              "parts": [{"mediaType": "application/json",
                         "data": {"type": "tool.started", "call_id": "call_1", "name": "list", "arguments": {}}}]}}}},
 "id": "1", "jsonrpc": "2.0"}
```

## Event types

| Type | Fields | Sent |
|---|---|---|
| `turn.started` | `conversation_id`, `turn_id` (the task id) | Once, first |
| `text.delta` | `step`, `content` | As the model writes; pieces are merged into one event per 50 ms or 512 characters |
| `reasoning.delta` | `step`, `content` | As the model reasons, merged the same way |
| `step.finished` | `step`, `tool_calls` (count), `tokens` (`input`, `output`), `finish_reason` | After each model call |
| `tool.started` | `call_id`, `name`, `arguments` (the parsed object, or the first 2,000 characters of invalid input) | Before each tool call |
| `tool.finished` | `call_id`, `name`, `output` (first 4,000 characters), `error`, `truncated`, `elapsed_ms` | After each tool call, including stopped ones (`output: "[stopped]"`) |
| `turn.finished` | `status`, `tokens`, `steps`, `elapsed_ms`, `files` (the files attached; an empty list may be left out on the wire, so read a missing key as `[]`), `error` (when any) | Once, last, in the task's final status |

Every `tool.started` has a matching `tool.finished`. Text written in the same model message as a tool call streams
as `text.delta` too; it is narration, and the answer is the final message without tool calls, delivered as the
`answer` artifact.

## Turn statuses

| Status | Meaning |
|---|---|
| `ok` | The model answered |
| `step_limit` | `max_steps` was reached and the model answered without tools |
| `deadline` | The turn ran past `deadline_seconds` |
| `cancelled` | The caller cancelled it |
| `model_error` | The model endpoint failed, or the turn failed unexpectedly; `error` gives a generic reason |
| `tool_error` | Reserved; this version does not produce it |

A tool that fails does not end the turn: the model sees the error and can try again.

## Artifacts

| Artifact `name` | Parts | Sent |
|---|---|---|
| `transcript` | One `application/vnd.astralvessel.transcript+json` part per chunk, holding `{"version": 1, "messages": [...]}`; the last chunk adds `"turn"` | Only when requested in `acceptedOutputModes` |
| `file` | One `raw` part with `mediaType`, `filename` and `metadata.call_id` | Per returned file, for accepted types |
| `answer` | A `text/markdown` part; a primitives part when components were attached | Once, before `turn.finished` |

The transcript's closing `turn` summary has the same fields as `turn.finished` plus `model`, the model name.
