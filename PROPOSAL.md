# AstralVessel: proposal

| | |
|---|---|
| Status | Draft for discussion; version 0.1 of the reference harness and a live demo are built (see README.md) |
| Author | Evan Damron |
| Date | 2026-11-09 |
| Audience | Sam (AstralDeep lead), Evan's collaborators on uk-knowledgegraph and InFocus-Agent, future open-source contributors |
| Background reading | [docs/first-principles.md](docs/first-principles.md) explains every concept here from scratch |

## 1. Summary

AstralVessel is a small, open-source **agent harness** with a written **standard** behind it.
A harness is the program that turns a language model into an agent: it runs the loop of
"ask the model, run the tool it asked for, show it the result, ask again", keeps the
conversation, enforces what the agent may touch, and streams progress to whatever app
is showing it.

Today two of Evan's projects get this from **OpenCode**, a coding assistant that we run
headless and bend into a research agent:

- **uk-knowledgegraph** (research-nexus.ai.uky.edu): answers questions about UK researchers
  by searching a 138,000-page generated wiki.
- **InFocus-Agent** (training.ai.uky.edu/infocus-agent): answers "where could a cancer
  intervention help?" by writing and running R over Cancer InFocus data.

Both projects surround OpenCode with the same hand-built scaffolding: a sandboxed container,
an egress proxy, an HTTP client for OpenCode's private API, a 371-line translator for its event
stream, session cleanup, and a configuration file that mostly switches off the parts of a coding
assistant a research agent must not have.

AstralVessel replaces OpenCode and that scaffolding with one component that is:

- **Lighter**: a Python package plus one container, no Node.js runtime, no coding-assistant
  features to disable, no private API that changes between minor releases.
- **More open**: every interface is a published standard (A2A for conversations, MCP for
  tools, the OpenAI-compatible API for models), described in a spec with conformance tests,
  under the Apache 2.0 license like the rest of Astral.
- **Astral-ready**: it speaks the same protocols as AstralDeep and can emit AstralPrimitives,
  so an agent built for AstralVessel can later run inside AstralDeep, and its answers render
  in Astral's web, Windows, Android, and Apple clients, without being rewritten.

The repository holds both the **spec** (what any conforming harness must do) and a
**reference harness** (Python 3.11) that implements it. uk-knowledgegraph is the first user;
InFocus-Agent the second.

## 2. Background in brief

The full explanation is in [docs/first-principles.md](docs/first-principles.md). The short
version of the vocabulary this proposal uses:

| Term | Meaning here |
|---|---|
| **Language model** | A service that takes text in and returns text out. We use GLM 5.3 Flash on the UK LLM Factory, reached through the OpenAI-compatible chat API. |
| **Tool call** | Instead of answering, the model replies "run `grep` with these arguments". The harness runs it and sends the output back. |
| **Agent** | A model plus a set of tools plus instructions, running in a loop until it produces an answer. |
| **Harness** | The program that runs that loop and everything around it: sessions, permissions, limits, streaming, logging. OpenCode is a harness. AstralVessel is a harness. |
| **Sandbox** | A locked-down container the tools run in, so a model that writes a bad or malicious command cannot reach anything that matters. |
| **Egress proxy** | The sandbox's only door to the outside world. It holds the model API key and allows only approved destinations. |
| **Headless** | Running with no user interface of its own, driven by another program over an API. |
| **Streaming** | Sending progress (text as it is written, each tool call as it starts and ends) while the agent works, instead of one reply at the end. |
| **A2A** | Agent2Agent, an open protocol (Linux Foundation) for one program to hold a conversation with an agent: send a message, receive a stream of status updates and results. AstralDeep already uses it. |
| **MCP** | Model Context Protocol, an open protocol for offering tools to an agent. AstralDeep already uses it. |
| **AstralPrimitives** | Astral's library that describes UI (cards, tables, buttons) as typed JSON. AstralProjection turns that JSON into HTML or native screens. |

## 3. What we built today, and what it costs

### 3.1 The shape both projects share

```
 browser
   │  HTTPS
   ▼
 web app (FastAPI)  ── access gate, chat page, link checking, conversation storage
   │  OpenCode's private HTTP API + one event firehose for every session
   ▼
 OpenCode container  ── Node.js + opencode serve, read-only, no network
   │  bash / read / grep / glob tools run here, over the mounted data
   │  every outbound request goes to ↓
   ▼
 egress container  ── adds the LLM key, forwards only chat completions (and, for nexus,
   │                  a checked forward proxy for web search and fetch)
   ▼
 UK LLM Factory (GLM 5.3 Flash)
```

The domain value lives at the edges: the vault and its helper scripts for nexus, the `cifagent`
R package and the 396-entry intervention library for InFocus, and the briefings that tell the
model how to use them. The middle is generic plumbing we wrote twice.

### 3.2 The plumbing, counted

| Piece | uk-knowledgegraph | InFocus-Agent |
|---|---|---|
| OpenCode HTTP client (`opencode_client.py`) | 226 lines | 226 lines (slightly different copy) |
| Event translator (`opencode_events.py`) | 371 lines | 371 lines (byte-for-byte identical copy) |
| Chat relay router | 540 lines (`wiki_agent.py`) | 260 lines (`chat.py`) |
| Session cleanup | 39 lines (`opencode_janitor.py`) | in the relay |
| Egress service | 322 lines (chat forwarder + checked web proxy) | 72 lines (chat forwarder) |
| OpenCode config and image | 116-line `opencode.json`, 109-line Dockerfile | 77-line `opencode.json`, 67-line Dockerfile |
| Agent briefing | 243 lines (`wiki-agent.md`) | 142 lines (`briefing.md`) |

Roughly **1,000 lines in InFocus and 2,300 in nexus** exist only to host OpenCode safely and talk
to it. A third project would copy them again.

### 3.3 Why OpenCode is an awkward fit

OpenCode is a good coding assistant. We use it as a research-agent engine, which it was not
designed to be. The costs we have hit, all recorded in the two repos and the Atlas:

1. **Most of the configuration is switching things off.** Both `opencode.json` files disable the
   `build`, `plan`, `general` and `explore` agents and the `edit`, `write`, `apply_patch`, `task`,
   `skill`, `question` and `lsp` tools, plus sharing, snapshots and auto-update. Every OpenCode
   release can add a new capability that is on by default and must be found and disabled.
2. **Its API is private and moves.** The HTTP API is OpenCode's internal interface, not a
   standard. The Dockerfiles pin the exact version (1.18.30 in nexus, 1.18.31 in InFocus) because
   "OpenCode's HTTP API changes between minor versions", and the event translator is written to
   ignore unknown fields because "OpenCode adds fields between releases".
3. **One firehose for everything.** OpenCode publishes a single event stream covering every
   session. Each relay subscribes to it, filters for its own session, and translates about two
   dozen event types into the dozen the browser needs. Child-session events are dropped, which is
   one reason subagents were rejected for nexus.
4. **It fights a read-only, offline sandbox.** At startup OpenCode `npm install`s a plugin and
   refreshes a model catalog from the internet. Both images seed those at build time, and nexus
   needs tmpfs mounts for four directories OpenCode insists on writing.
5. **It leaks its coding context.** OpenCode tells the model its working directory and other
   coding-assistant context on every step; the nexus briefing has to work around it, and the relay
   strips stray file links from answers.
6. **A Node.js runtime inside an R or Python image.** InFocus installs Node.js 22 into a
   `rocker/r-ver` image only to run OpenCode.
7. **Permission prompts with nobody to answer them.** OpenCode can pause to ask a human for
   permission. Headless, the relays must auto-reject every prompt.
8. **Metrics fall on the floor.** Elapsed time and the final error code go only to the browser's
   stream; nexus's stored conversations do not record them.
9. **Tools are bash scripts.** Custom capabilities (nexus's `resolve.py`, `person.py`,
   `methods.py`) are scripts the model must remember to invoke through bash, with no declared
   inputs, because OpenCode's own custom-tool mechanism did not fit the sandbox.
10. **No path into Astral.** Nothing OpenCode produces can be rendered by Astral's clients or run
    inside AstralDeep.

None of these are bugs in OpenCode. They are the price of using a product built for a different job.

## 4. What AstralVessel is

AstralVessel has two halves in one repository:

- **The spec** (`spec/`): a short written standard for an *agent package* and for how a harness
  serves it, plus a **conformance suite** of tests any implementation can run against itself.
- **The reference harness** (`astralvessel/`): a Python 3.11 package and container image that
  implements the spec. This is what nexus and InFocus will run.

AstralDeep can later implement the same spec, or simply call a running Vessel. Either way, an
agent package written once runs in both places.

### 4.1 The agent package

An agent is a folder. It contains everything that makes the agent *this* agent, and nothing about
the harness:

```
nexus-agent/
  vessel.toml        the manifest: identity, model, tools, limits
  prompt.md          the briefing (today's wiki-agent.md)
  tools/             optional: declared tools implemented as scripts or Python functions
    resolve.toml     input schema + command for each tool
```

An example manifest, close to what nexus needs:

```toml
[agent]
name = "nexus"
description = "Answers questions about University of Kentucky research from the ccts-kg vault."
version = "1.0.0"

[model]
endpoint = "egress"                 # resolved by the deployment, never a literal key
model = "zai-org/GLM-5.3-Flash"
temperature = 0.2
max_steps = 40

[tools]
builtin = ["read", "grep", "glob", "list", "bash", "webfetch", "websearch"]
declared = ["tools/resolve.toml", "tools/person.toml", "tools/methods.toml"]
mcp = []                            # MCP servers the agent may call, if any

[sandbox]
mounts = [{ source = "vault", target = "/vault", read_only = true }]
memory = "4g"
cpus = 4
pids = 512
deadline_seconds = 600

[output]
primitives = true                   # may emit AstralPrimitives parts (always with text)
```

What changes compared with `opencode.json`: the manifest lists what the agent **has**, not what it
must be prevented from having. A tool not listed does not exist.

**Declared tools** fix the "bash scripts" problem. Each has a name, a description, a JSON Schema for
its inputs, and a command. The model sees `resolve(name: string)` as a real tool with typed
arguments; the harness runs the script inside the sandbox.

### 4.2 The conversation API (A2A with streaming)

Apps talk to a Vessel with **A2A** version 1.0, the same protocol and version AstralDeep already
mounts at `/a2a`. The spec pins a small profile of it:

- `GET /.well-known/agent-card.json` describes the agent (name, description, skills, whether it
  streams).
- `SendStreamingMessage` sends a user message and returns a server-sent event stream for that one turn.
- `CancelTask` stops a running turn.
- A2A's `contextId` is the conversation: send the same `contextId` and the agent remembers.

Within the stream, the profile fixes exactly which events appear and what they carry, so an app
never has to translate a firehose:

| Event | Carries |
|---|---|
| `turn.started` | conversation id, turn id |
| `text.delta` | the next piece of the answer as it is written |
| `reasoning.delta` | the model's visible thinking, when the model provides it (optional for apps to show) |
| `tool.started` | tool name, its arguments |
| `tool.finished` | tool name, a bounded copy of its output, whether it failed, how long it took |
| `step.finished` | tokens in and out for that model call |
| `turn.finished` | the final answer (text, plus optional primitives), total tokens, elapsed milliseconds, and a status code: `ok`, `cancelled`, `deadline`, `step_limit`, `model_error`, or `tool_error` |

These ride inside A2A's standard `TaskStatusUpdateEvent` and `TaskArtifactUpdateEvent` envelopes,
so a generic A2A client still understands the turn, and the vocabulary above is what an app reads
when it wants detail. The same record is written to the session store, which fixes item 8 in §3.3.

### 4.3 Apps and users: who is allowed to ask

A Vessel is not a public service; an app sits in front of it. The app (nexus's FastAPI server, for
example) holds a **deployment API key** and sends it on every request. With each conversation it
also sends an **opaque end-user id**, such as a hash of the CILogon subject. Every conversation is
bound to that user id: a request for someone else's conversation fails exactly as if it did not
exist. This is the owner-binding rule Sam enforces in AstralDeep, applied from day one.

There is no login system inside Vessel. Users sign in to the app however the app chooses (CILogon,
a shared access code, Keycloak). OpenID Connect support can come later without changing the
conversation API.

### 4.4 Sessions

Conversations are stored in SQLite inside the Vessel's data volume: every message, tool call,
token count, timing, and final status. The API offers listing, reading, and deleting a user's
conversations, and a cleanup job deletes conversations idle longer than a configured time (nexus
uses 24 hours today). Deleting really deletes.

SQLite keeps the dependency list at zero extra services. A Postgres option can be added if a
deployment ever needs several Vessel replicas sharing one store.

### 4.5 Tools

The harness ships with the tools both projects use today and nothing else:

| Tool | What it does |
|---|---|
| `read` | Read a file, or a slice of one, with a size cap |
| `grep` | Search file contents (ripgrep) |
| `glob` | Find files by name pattern |
| `list` | List a directory |
| `bash` | Run a shell command with a time limit and bounded output |
| `webfetch` | Fetch a web page through the egress proxy and return it as text |
| `websearch` | Search the web through the egress proxy |

Beyond these, an agent gets **declared tools** from its package (§4.1) and tools from **MCP
servers** the manifest names. AstralDeep's own tools are already available over MCP, so a Vessel
agent could later use them through the same mechanism.

There are **no interactive permission prompts** in version 1. A tool is either in the manifest or
it does not exist; the sandbox, not a dialog box, is the safety boundary. This matches what both
projects do now by auto-rejecting every prompt.

### 4.6 Sandbox and egress

The reference image runs the harness and its tools inside one container with the containment
nexus already proved out:

- read-only root filesystem, writable scratch only in size-capped `tmpfs` with `noexec`
- all Linux capabilities dropped, `no-new-privileges`, an unprivileged user
- process, memory and CPU limits from the manifest
- an internal-only Docker network: the container has no route to the internet, the database, or
  the host

Its only way out is a separate **egress** container, which becomes a standard part of AstralVessel
instead of being copied per project. It forwards model calls (adding the API key itself, so the
sandbox never holds it) and runs the checked forward proxy for `webfetch` and `websearch`: only
ports 80 and 443, every destination resolved and refused if private, loopback, link-local or
otherwise reserved, the connection made to the checked address so DNS rebinding cannot sneak
past, and size and idle limits.

One sandbox per deployment is the version 1 design, which is what both projects run today. A
fresh container per conversation is a later option for agents that write files.

### 4.7 Models

The harness talks to any OpenAI-compatible chat endpoint: the UK LLM Factory, a local vLLM or
Ollama server, or a commercial API. That matters for InFocus, whose 59 partner institutions must
each be able to bring their own model and key. Model quirks (GLM returns its thinking in a
`reasoning_content` field, for example) are handled in one adapter with tests, instead of in an
OpenCode configuration flag nobody can find.

### 4.8 Rich output that Astral can render

This is a firm requirement, not a nice-to-have.

- Every answer has **text** (Markdown). Apps that never render rich output, like nexus today, get a
  complete answer from the text alone.
- An answer **may also** carry an A2A data part with media type
  `application/vnd.astral.primitives+json`. Its content is exactly what AstralPrimitives'
  `.to_dict()` produces (a card, a table, a chart), tagged with the primitives schema version it
  targets.
- The **conformance suite validates** every primitives part against the JSON Schema exported from
  AstralPrimitives' own Pydantic models. A Vessel cannot drift from what AstralDeep and
  AstralProjection accept.
- The **core harness does not depend** on AstralPrimitives. An optional extra,
  `pip install astralvessel[primitives]`, gives agents helpers for building parts.
- A **CI job renders** a sample agent's rich answer through AstralProjection's renderer on every
  change, proving "renders in Astral" rather than assuming it.

### 4.9 The conformance suite

The spec is only useful if it is testable. The suite is a set of pytest tests that take a URL and
a deployment key and check a running harness:

- the agent card is served and well formed
- a turn streams the events of §4.2 in a valid order, ending in exactly one `turn.finished`
- cancelling before the turn starts, during a tool call, and after it finishes all behave
- a second user cannot see, continue, or delete the first user's conversation
- a tool missing from the manifest cannot be called, even if the model asks for it
- the sandbox has no route to the internet or private addresses except through egress
- every primitives part validates against the AstralPrimitives schema
- error responses never contain raw exception text (secrets and personal data stay out)

The reference harness must pass it in CI. AstralDeep can run the same suite against itself the day
it adopts the spec.

## 5. What changes in each project

### 5.1 uk-knowledgegraph (first user)

| Today | With AstralVessel |
|---|---|
| `opencode/` image with Node.js and OpenCode | `nexus-agent/` package: `vessel.toml`, `prompt.md` (today's `wiki-agent.md`), declared tools wrapping `resolve.py`, `person.py`, `methods.py` |
| `opencode_client.py`, `opencode_events.py`, `opencode_janitor.py` | Deleted. The relay becomes a thin A2A client. |
| `wiki_agent.py` (540 lines) | Keeps what is nexus-specific: access control, attachments, link rewriting, storage policy. The OpenCode-specific half goes away. |
| `egress/` | Replaced by AstralVessel's standard egress container |
| Stored transcripts lack timing and error codes | Every turn records both |

**Done means:** nexus serves research-nexus.ai.uky.edu from AstralVessel and passes its existing
end-to-end suite (`scripts/e2e/site_e2e.sh`, 44 of 45 at the last full run; the one failure is a
briefing issue that will be judged the same way).

### 5.2 InFocus-Agent (second user)

| Today | With AstralVessel |
|---|---|
| `rocker/r-ver` image plus Node.js plus OpenCode | `rocker/r-ver` image plus the AstralVessel Python package; no Node.js |
| `opencode.json` disabling seven tools and four agents | `vessel.toml` listing `read`, `grep`, `glob`, `list`, `bash` |
| R runs through bash | Unchanged; `cifagent` functions can also become declared tools later |
| Link checking against the intervention library in the web app | Unchanged (it is InFocus's own rule) |

**Done means:** the smoke tests (`scripts/e2e/smoke.py`, Todd's two questions) pass against the
live site with numbers traced to R output and at least three library links.

This also serves Todd's portability requirement: a partner institution runs the InFocus package in
the AstralVessel container and points it at its own model endpoint.

## 6. Why lighter and more open

| | OpenCode as we use it | AstralVessel | AstralDeep |
|---|---|---|---|
| What it is | A coding assistant run headless | A research-agent harness and standard | A clinical multi-agent platform |
| Runtime | Node.js | Python 3.11 | Python 3.11 + PostgreSQL + Keycloak |
| Services to deploy | OpenCode + our relay + our egress | Vessel + egress | Orchestrator, PostgreSQL, Keycloak, optional voice, LETS |
| App-facing interface | Private HTTP API, pinned per version | A2A (open standard) | Server-driven UI WebSocket; A2A/MCP for tools only today |
| Tools interface | Built-ins + bash scripts | Built-ins + declared tools + MCP | MCP |
| Configuration style | Disable what you must not have | List what you have | Feature flags per subsystem |
| Rendering in Astral clients | No | Yes, through primitives parts | Native |
| Spec and conformance tests | No | Yes | Has its own contracts |

AstralVessel is not a smaller AstralDeep. AstralDeep's owner isolation, clinical data gates, and
durable state are the right weight for a clinical platform and the wrong weight for a research
wiki. Vessel takes the parts of the Astral design an app-embedded agent needs (owner binding,
open protocols, primitives) and leaves the rest.

## 7. Path into AstralDeep

Two routes stay open, and neither requires a decision now:

1. **Deep calls a Vessel.** AstralDeep's existing A2A support forwards a conversation to a running
   Vessel and renders the primitives parts it gets back through AstralProjection. The Vessel is
   then one more agent behind Deep, like a remote agent.
2. **Deep loads Vessel packages.** AstralDeep reads `vessel.toml` packages and runs them under its
   own harness, with its own permission and audit gates, after passing the conformance suite.

Either route is a single, reviewable integration PR against a harness that is already proven in
production, which is a smaller ask of Sam than redesigning Deep's core up front.

## 8. Not in version 1

- A user interface. Apps bring their own.
- Multi-tenant hosting (one Vessel serving unrelated organizations).
- Subagents and agents delegating to agents.
- Generating agent code from a description (AstralDeep already does this for personal agents).
- Clinical data and PHI handling. That stays AstralDeep's job.
- Interactive permission prompts.

## 9. Plan

| Milestone | Delivers | Verified by |
|---|---|---|
| M0 | This proposal agreed; repo home decided | Sam and Evan sign off |
| M1 | Spec draft: package format, A2A profile, event vocabulary, primitives part | Spec reviewed; conformance suite skeleton runs |
| M2 | Reference harness: loop, built-in tools, OpenAI-compatible model adapter, SQLite sessions | Unit tests; a sample agent answers over A2A |
| M3 | Sandbox image and standard egress container | Conformance network and isolation tests pass |
| M4 | Primitives output and the AstralProjection render test | CI renders a sample rich answer |
| M5 | nexus on AstralVessel in a staging stack on bigrambro | `site_e2e.sh` passes against staging |
| M6 | nexus live on AstralVessel; OpenCode stack retired after a week of parallel running | Live e2e passes; rollback documented |
| M7 | InFocus-Agent on AstralVessel | `smoke.py` passes live |

## 10. Risks and open questions

- **We own the harness now.** OpenCode's maintainers fix bugs we never see; with Vessel those are
  ours. The mitigation is scope: Vessel does one job, with no editor, no UI, and no coding features,
  and the conformance suite catches regressions.
- **Answer quality must not drop.** OpenCode's prompts and loop details have been tuned by many
  users. Nexus's end-to-end suite and InFocus's smoke tests are the bar; if Vessel answers worse on
  them, it does not ship.
- **Repo home.** Evan's GitHub account cannot create repositories in the AstralDeep organization.
  Either Sam creates `AstralDeep/AstralVessel` (private until the proposal settles), or it starts
  in Kentucky-Open-Science and transfers later.
- **Sam's buy-in on the primitives contract.** The primitives media type and schema export touch
  AstralPrimitives. That needs Sam's agreement before M4.
- **Collaborators.** uk-knowledgegraph and InFocus-Agent have other contributors (Mitch, Caroline,
  Todd). The switch in each needs their agreement as well as Evan's.
- **A2A versions.** AstralDeep speaks A2A 1.0 through the `a2a-sdk` 1.x line, with 0.3
  compatibility for older peers. The profile targets 1.0 so route 1 in §7 works without translation.
