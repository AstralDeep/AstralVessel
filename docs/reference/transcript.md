# Transcript format

A transcript is conversation history in OpenAI chat format, wrapped in a small versioned envelope. Applications send
it in with each turn and receive the turn's new messages back in the same format.

Media type: `application/vnd.astralvessel.transcript+json`

## The envelope

```json
{
  "version": 1,
  "messages": [
    {"role": "user", "content": "What is here?"},
    {"role": "assistant", "content": "Looking. ",
     "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "list", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "species/"},
    {"role": "assistant", "content": "There is one folder, species/.", "reasoning_content": "..."}
  ]
}
```

## Messages

| Role | Allowed fields | Rules |
|---|---|---|
| `user` | `role`, `content` | `content` is a string, or a non-empty list of `{"type": "text", "text": ...}` and `{"type": "image_url", "image_url": {"url": ..., "detail": ...}}` parts |
| `assistant` | `role`, `content`, `tool_calls`, `reasoning_content` | `content` is a string or null; each tool call has exactly `id`, `type: "function"` and `function` with string `name` and `arguments` |
| `tool` | `role`, `tool_call_id`, `content` | Must answer an open tool call from the preceding assistant message; `content` is a string |

`system` messages are refused: the briefing is fixed by the operator.

The vessel also checks that:

- the envelope's `version` is `1` and it holds at most 2,000 messages;
- no other field appears on a message;
- every tool call is answered before the next user or assistant message, and the history does not end with an
  unanswered call;
- images are inline `data:` URLs of PNG, JPEG, GIF or WebP, real base64 with the right leading bytes, at most 5 MB
  each and 32 in the whole history, and only for agents whose model accepts images;
- `detail`, when present, is `auto`, `low` or `high`.

A history that fails a check rejects the turn with a message naming the problem.

## What comes back

With the transcript media type in `acceptedOutputModes`, the `transcript` artifact streams each completed message of
the turn, in order: the user's message first, then assistant messages, tool results, and the final answer. Each
message is sent once, as soon as it is complete, so a dropped stream still leaves a usable prefix. The last chunk
adds a summary:

```json
{"version": 1, "messages": [],
 "turn": {"status": "ok", "steps": 2, "tokens": {"input": 30, "output": 13}, "elapsed_ms": 3, "model": "your-model-name"}}
```

Append the messages to your stored history and send them with the next turn.

| Situation | What the transcript holds |
|---|---|
| The turn ended before the model answered | A closing assistant message `[this turn ended early: <status>]` |
| A tool call never ran | A tool result `[not run: the turn ended first]` |
| The model sent tool arguments that are not JSON | The call's `arguments` are stored as `{}`, so the history stays replayable; the error result stays |

`reasoning_content` is kept in the transcript; it is sent back to the model on later turns only when the agent sets
`send_reasoning = true`.
