# AstralVessel specification, draft 0.1

This document says what a conforming AstralVessel harness must do. The words **must**, **must not**,
**should** and **may** are normative. `conformance/run.py` checks a running harness against it.

## 1. Agent package

An agent package is a directory containing:

| Path | Required | Content |
|---|---|---|
| `vessel.toml` | yes | The manifest (§1.1) |
| `prompt.md` | yes | The agent's briefing, used as the system prompt |
| declared tool files | no | One TOML file per declared tool (§1.2), at paths listed in the manifest |

### 1.1 Manifest

```toml
[agent]                     # required
name = "nexus"              # lowercase letters, digits, hyphens; starts with a letter
description = "..."
version = "0.1.0"
examples = ["..."]          # optional example questions

[model]                     # required
endpoint = "env:VESSEL_MODEL_URL"   # an OpenAI-compatible base URL, or env:NAME
api_key = "env:VESSEL_MODEL_KEY"    # never a literal secret in a shared package
model = "zai-org/GLM-5.3-Flash"
temperature = 0.2
max_steps = 40              # 1 to 200
max_output_tokens = 8192

[tools]
builtin = ["read", "grep", "glob", "list", "bash", "webfetch", "websearch"]   # any subset
declared = ["tools/find_pages.toml"]

[sandbox]
workdir = "/vault"
mounts = [{ source = "vault", target = "/vault", read_only = true }]
deadline_seconds = 600      # wall-clock limit per turn
tool_timeout_seconds = 60
tool_output_chars = 20000   # output beyond this is truncated before the model sees it

[output]
primitives = true           # the agent may attach AstralPrimitives (§4)
```

- Unknown keys **must** be rejected. Unknown built-in tool names **must** be rejected.
- A tool that is not listed **must not** be offered to the model, and a call to it **must** fail with a
  tool error rather than run.
- `env:NAME` values are resolved from the harness environment at run time.

### 1.2 Declared tools

```toml
name = "find_pages"                 # lowercase, digits, underscores; must not shadow a built-in
description = "..."
command = ["python3", "{package}/tools/vault.py", "find_pages"]   # {package} is the package root
input = "json-stdin"                # or "args" (--key=value flags)
timeout_seconds = 30                # optional, overrides the sandbox default
[parameters]                        # JSON Schema for the arguments
type = "object"
```

Declared tool files **must** live inside the package. The command runs in the sandbox (§5) with the
sandbox working directory as its current directory.

## 2. Conversation API: A2A 1.0 profile

A harness **must** serve A2A protocol version 1.0 over JSON-RPC at `POST /a2a`, and an agent card at
`GET /.well-known/agent-card.json` (unauthenticated) with `capabilities.streaming = true`.

| Method | Use |
|---|---|
| `SendStreamingMessage` | Send one user message (text parts) and receive the turn as server-sent events |
| `SendMessage` | The same turn without streaming |
| `CancelTask` | Stop a running turn |
| `GetTask` | Read a task's current state |

- A2A's `contextId` is the conversation. A message with a known `contextId` continues it; a message
  without one starts a new conversation.
- A2A's task is one turn.
- A harness **may** limit message size; the reference harness rejects messages over 100,000 characters,
  request bodies over 1 MB, and conversation ids outside `A-Za-z0-9._:-` (1 to 128 characters).
- Deleting a conversation **must** also remove its tasks, so `GetTask` and `ListTasks` no longer return them.

## 3. Turn events

Within a turn, the harness **must** publish these events, each as a JSON object with a `type` field,
inside an A2A `TaskStatusUpdateEvent` whose status message holds the event as a data part. The status
update's `metadata["vessel.event"]` repeats the type.

| Type | Fields | Notes |
|---|---|---|
| `turn.started` | `conversation_id`, `turn_id` | Exactly once, first |
| `text.delta` | `step`, `content` | Text as the model writes it. Text in a step that ends in tool calls is narration |
| `reasoning.delta` | `step`, `content` | Optional; the model's visible thinking |
| `step.finished` | `step`, `tool_calls`, `tokens` (`input`, `output`), `finish_reason` | Once per model call |
| `tool.started` | `call_id`, `name`, `arguments` | |
| `tool.finished` | `call_id`, `name`, `output`, `error`, `truncated`, `elapsed_ms` | Exactly once per `tool.started` |
| `turn.finished` | `status`, `steps`, `tokens`, `elapsed_ms`, optional `error` | Exactly once, last |

`status` is one of `ok`, `cancelled`, `deadline`, `step_limit`, `model_error`, `tool_error`.

Before the final status, the harness **must** publish one artifact named `answer` containing a text part
(`text/markdown`) with the complete answer, and, when the agent attached any, one primitives part (§4).
The task then ends `COMPLETED` (status `ok` or `step_limit`), `CANCELED` (`cancelled`) or `FAILED`
(anything else); the final status message carries the `turn.finished` event.

A cancel **must** stop the running model call or tool, record the turn, and still emit `turn.finished`.
Errors **must** be reported with generic messages; raw exception text, connection strings, paths of the
harness and secrets **must not** appear in any event, artifact or stored record.

## 4. Rich output: AstralPrimitives

- Every answer **must** be complete as Markdown text alone.
- An answer **may** carry one data part with media type `application/vnd.astral.primitives+json`, whose
  data is `{"components": [...]}`, each component exactly as AstralPrimitives' `to_dict()` produces it,
  and whose part metadata carries `schema_version`, the astralprims version it was validated against.
- The harness **must** validate components before attaching them, and **must** return validation errors
  to the model as a tool error so it can correct them.
- The reference harness offers a `present` tool for this when `[output] primitives = true`.

## 5. Sandbox and egress

- Built-in file and shell tools and declared tools **must** run in a sandbox that has no network route,
  no access to the conversation store, and no model or deployment secrets. The sandbox **should** have a
  read-only root filesystem, size-capped writable scratch space mounted `noexec`, an unprivileged user,
  no Linux capabilities, and memory, CPU and process limits.
- A tool call's processes **must not** outlive the call; background processes it leaves behind are killed
  before the next call runs. Tool output **must** be bounded before it leaves the sandbox.
- While one sandbox serves several users, their calls share its scratch space and can see each other's
  process list. A deployment that needs isolation between users **should** give each conversation its own
  sandbox.
- The harness **must not** hold the model provider's key when an egress service is available; the
  egress service adds it. Web access (`webfetch`, `websearch`) **must** go through a proxy that only
  reaches public addresses on ports 80 and 443, resolves and checks every target, and connects to the
  checked address. Only globally routable addresses count as public: shared address space
  (100.64.0.0/10, used by Tailscale) and other special ranges **must** be refused.

## 6. Apps and owners

- Every request other than the agent card and `GET /health` **must** carry `Authorization: Bearer <deployment key>` and
  `X-Vessel-User: <opaque user id>` (1 to 128 characters of `A-Za-z0-9._:@-`). Missing or invalid
  values get HTTP 401.
- Every conversation and task is bound to the user that created it. Reading, listing, continuing,
  cancelling or deleting another user's conversation or task **must** behave as if it did not exist
  (404, a rejected task, or an A2A task-not-found error).
- The harness **should** store only a hash of the deployment key.

## 7. Conversation management

| Route | Effect |
|---|---|
| `GET /v1/conversations` | The caller's conversations, most recent first |
| `GET /v1/conversations/{id}` | One conversation with its turns, results and recorded events |
| `DELETE /v1/conversations/{id}` | Hard delete |
| `GET /v1/manifest` | A public summary of the agent's manifest |

Idle conversations **should** be deleted after a configurable period (default 24 hours).
