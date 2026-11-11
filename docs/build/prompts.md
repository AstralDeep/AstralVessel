# Prompts

`prompt.md` is the agent's briefing. This page shows how the vessel turns it into the system prompt, and when edits
take effect.

## How the system prompt is built

On every turn the vessel builds one system message from:

1. `prompt.md`, read from the agent folder.
2. Each file in the agent's `prompt_append` list in `deployment.toml`, in order, separated by blank lines.
3. An `## Environment` section the vessel adds:

```text
## Environment
Today is 2026-11-11.
Tool commands run in /data.
Mounted data:
- /data (read-only)
Files a tool call writes under $TMPDIR are kept for later tool calls in this turn and deleted when the turn ends.
Text you write in the same message as a tool call is shown as brief narration, not as your answer. Your final message, with no tool calls, is the answer: write it in full there.
```

The `Mounted data` lines appear only when the agent declares mounts. An agent with `[output] primitives = true` also
gets a line asking it to call `present` once before its final message and to write the complete answer in Markdown
anyway.

Tool descriptions and parameter schemas reach the model as OpenAI tool definitions, not as prompt text.

## Edits apply on the next turn

`prompt.md` and `prompt_append` files are read on every turn, so an edit needs no redeploy or restart. If
`prompt.md` cannot be read at turn time, the vessel uses the copy it read at startup. A `prompt_append` file that
has disappeared since the deployment loaded is skipped.

## History cannot replace the briefing

The briefing is fixed by the operator. Conversation history sent by an application may not contain a `system`
message; a turn whose history does is rejected. See [Transcript format](../reference/transcript.md).

## Writing a briefing

The model sees only the briefing, the environment section, its tools and the conversation. Some points that follow
from how the loop works:

- **Say where the data is.** The environment section names the mount paths but not what is in them.
- **Name the tools you expect it to use.** For example, the example `field-guide` agent says "Use the `species`
  tool to get every species' facts at once, and read pages for details."
- **Say what to do when the data is silent.** Answers come only from what the model reads.
- **Put the answer in the final message.** The vessel treats the last message without tool calls as the answer;
  text written beside a tool call streams as narration.
- **Treat web text as data.** Pages fetched by `webfetch` and results from `websearch` reach the model fenced in a
  `<web_content>` block labeled as untrusted, but a briefing that says the same thing helps.
