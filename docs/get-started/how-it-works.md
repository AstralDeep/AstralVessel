# How it works

This page describes the containers `astralvessel deploy` generates, the networks between them, and what happens
during one turn.

## The containers

| Container | Runs | Holds | Networks |
|---|---|---|---|
| `egress` | The model passthrough (port 8080) and a guarded web proxy (port 3128) | The model key | `egress`, `outside` |
| `vessel` | Every agent's A2A endpoint, the agent loop, the web tools and service calls | App key digests, the sandbox master token, a task store of runs in flight | `front`, `egress`, each `tools-<agent>` |
| `sandbox-<agent>` | One agent's file, shell and declared command tools | Its own sandbox token and read-only data | its `tools-<agent>` only |
| `search` | SearXNG, when `[search] provider = "searxng"` (the default) | A generated SearXNG secret | `egress` |

All four are read-only, drop every Linux capability, set `no-new-privileges`, restart unless stopped, and cap their
memory and process counts. Nothing in the running system controls Docker: you run `docker compose` on what
`deploy` writes.

## The networks

| Network | Internal | Who is on it | Why |
|---|---|---|---|
| `front` | yes | the vessel (at `front_address`) and your applications | Applications call agents; the vessel calls service tools |
| `egress` | yes | egress, vessel, search | Model calls, web proxy, search |
| `tools-<agent>` | yes | the vessel and that agent's sandbox | Tool calls over HTTP |
| `outside` | no | egress only | The only route out |

The vessel listens only on its `front` address, so a sandbox, which shares a `tools-<agent>` network with the
vessel, cannot reach the vessel's API. Sandboxes cannot reach each other, the egress, or search. Compose prefixes
every network with the project name, so the default `front` network is `astralvessel_front`.

## The path of one turn

1. **The application sends a message.** It posts an A2A `SendStreamingMessage` (or `SendMessage`) to
   `/agents/<id>` with its app key, an `X-Vessel-User` header naming the end user, the user's text, and the
   conversation so far as a transcript part. A caller with a signed token from a trusted issuer sends the token
   instead. See [Identity, apps and issuers](../run/identity.md).
2. **The vessel checks the caller.** It verifies the key or token, checks the agent is granted to that caller and
   its sandbox is ready, and binds the new task to its owner (`<app>/<user>` or `<issuer>|<subject>`).
3. **The vessel checks the history.** The transcript must be well formed, at most 2,000 messages, and may not hold a
   system message. Images are accepted only if the agent's model accepts them.
4. **The loop runs.** The vessel builds the system prompt from `prompt.md` (read fresh on every turn), sends the
   history and the new message to the model through the egress, and streams text and reasoning back as events.
5. **Tools run where they belong.** When the model asks for a tool:
    - `grep`, `bash` and declared command tools run in the agent's sandbox as separate processes, each confined
      with Landlock and seccomp and sharing the turn's workspace. `read`, `glob` and `list` run in the sandbox's
      server process under the same path limits.
    - `webfetch` and `websearch` run in the vessel and reach the web only through the egress proxy or search.
    - Service tools run in the vessel, which posts the arguments to an endpoint the deployment grants.
    - `present` attaches AstralPrimitives components to the answer.
6. **The loop repeats** until the model answers without calling a tool, the agent's `max_steps` is reached (the
   model is then asked to answer with what it has), the turn's deadline passes, or the caller cancels.
7. **The answer comes back.** The vessel streams the turn's new messages as a transcript artifact (if the caller
   asked for it), any files tools returned, and an `answer` artifact with the Markdown answer. A final
   `turn.finished` event carries the status, token counts and timing.
8. **The workspace is deleted.** The sandbox removes the turn's scratch directory.
9. **The application stores the transcript.** It appends the returned messages to its history and sends them with
   the next turn. The vessel keeps no conversation history.

## What deploy fingerprints

`deploy` computes a profile fingerprint for each agent from its id, its sandbox grants in `deployment.toml`, its
manifest and declared tools as loaded (without the model key), and its sandbox Dockerfile and the files that
Dockerfile copies. The sandbox reports the fingerprint it was started with, and the vessel serves the agent only
while the fingerprint and the loaded manifest match its own. The vessel container is labeled with a hash of the
deployment file and every fingerprint, so `docker compose up -d` recreates it when either changes.

| You changed | Do this |
|---|---|
| `prompt.md` or a `prompt_append` file | Nothing; it is read on every turn |
| A script a tool runs | Nothing; the agent folder is mounted, not copied |
| `vessel.toml`, a tool's `.toml`, `deployment.toml`, or a sandbox Dockerfile | `astralvessel deploy`, then `docker compose ... up -d` (add `--build` for a Dockerfile change) |
