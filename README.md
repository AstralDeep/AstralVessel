# AstralVessel

A lightweight, open agent server and the spec behind it. AstralVessel is a server for agents that
sits between an application and its model provider: it takes questions over **A2A 1.0**, uses any
**OpenAI-compatible** model, runs each agent's tools in its own sealed **sandbox**, reaches the outside
world only through an **egress** door, and answers in Markdown with optional **AstralPrimitives** so
Astral's clients can render the result.

It is built on the same design as [Astral](https://github.com/AstralDeep)'s harness and sandbox, as a
separate implementation an application can use without running a full Astral instance. It is not code
taken from AstralDeep.

**Documentation:** <https://astraldeep.github.io/AstralVessel/>

![Diagram: on the left, a full Astral instance with its web, Windows, Android and Apple clients, accounts and sign-in, an agent catalog of many agents, and a highlighted harness and sandbox. On the right, one app such as a team's web app, using AstralVessel for its agents: an agent folder (vessel.toml, prompt.md, tools/) plus a harness, sandbox and egress. A solid arrow from left to right reads "used on its own"; a dashed arrow back reads "could slot back in later".](https://raw.githubusercontent.com/AstralDeep/AstralVessel/main/assets/lifted-out.svg)

## Why it exists

Astral is a centralized platform: one deployment serves many users, many agents, and Astral's own web,
Windows, Android and Apple clients. Some applications need only a small piece of that: a few agents
answering questions or doing work inside one app, without Astral's accounts, agent catalog or client
apps.

Without AstralVessel, such an app tends to borrow its agent loop from a coding assistant run headless
(OpenCode is the usual choice) and surround it with hand-built scaffolding:

- a sandboxed container
- an egress proxy
- an HTTP client for the assistant's private API
- a translator for its event stream
- session cleanup
- configuration that switches off the coding features the agent must not have

Each app rebuilds that glue, it breaks when the assistant changes its private API, and it still runs a
coding tool to do something else.

AstralVessel replaces the borrowed assistant and the scaffolding with one component that is:

- **Lighter.** It is a Python package and a few containers. It has no coding-assistant features to
  switch off and no private API that shifts between releases.
- **Open.** Every interface is a published standard: A2A for conversations, the OpenAI-compatible API
  for models, and AstralPrimitives for rich output. The code is under Apache 2.0 like the rest of Astral.
- **Astral-ready.** It speaks the same protocols as AstralDeep, so AstralDeep can call an AstralVessel
  agent the way it calls any external A2A agent, and an agent built for AstralVessel could move into
  Astral without being rewritten.

AstralVessel's spec covers an agent server as seen from outside: A2A in, events and transcripts out,
sandboxes behind it. AstralDeep's own harness contract is a different layer, the interface its
orchestrator uses for its internal model loop; AstralVessel does not implement or replace it.

## How it works

An agent is a folder: a manifest (`vessel.toml`), a briefing (`prompt.md`), and optional declared
tools, each a small TOML file. A deployment file (`deployment.toml`) lists the agents one vessel
serves, the sandbox and services each one gets, and which applications may call which agents. The
agent declares what it needs; the deployment file is the only place access is granted.

![Diagram of who holds what. The app holds its key and its users' conversation history, names each user with X-Vessel-User, and is also on an outside network for its users' browsers. It calls the vessel over A2A 1.0. The vessel serves every agent and runs the agent loop; it holds only runs in flight, never conversation history or the model key. It calls each agent's sandbox over /run (HTTP); there is one sandbox per agent, which runs the tools on read-only data and never has network, secrets, history, or a route to the vessel's API. The vessel reaches the egress over the OpenAI-compatible API, which is also the web proxy for webfetch and websearch. The egress holds the model key, reaches only public addresses on ports 80 and 443, and calls the model over HTTPS: any OpenAI-compatible model the operator configures. Everything except the egress's outbound link and the app's browser-facing side sits on internal Docker networks with no route out.](https://raw.githubusercontent.com/AstralDeep/AstralVessel/main/assets/who-holds-what.svg)

- **The app** authenticates with its own key, which the deployment limits to certain agents, and names
  each end user with an `X-Vessel-User` header. An orchestrator with an identity provider (such as
  AstralDeep's Keycloak) can instead send each user's signed token: the vessel checks its issuer,
  audience, expiry, authorized party and signature against the issuers the deployment trusts. Every
  task is bound to its owner: the app and user, or the issuer and the token's subject. The app owns conversation
  history: it sends the history with each turn and stores the messages that come back. It may attach
  images for agents whose model accepts them.
- **The vessel** serves every agent in the deployment, each with its own A2A address and agent card.
  It runs the agent loop (ask the model, run the tool it asks for, return the result, repeat), streams
  every step as A2A events, and streams the turn's messages as a transcript in OpenAI chat format. It
  keeps only runs in flight, and enforces step limits, deadlines and cancellation. Agent briefings are
  fixed at setup: history from the app may never carry a system message.
- **Each agent's sandbox** is a separate container that runs its file, shell and command tools. It has
  no network route, no secrets, no history, a read-only root, and its data mounted read-only. Inside
  it, every tool call runs under Landlock and a seccomp filter. The calls of one turn share a scratch
  directory (the turn's workspace, `$TMPDIR`), so a tool can save an intermediate result for the next
  call; it is deleted when the turn ends. Disk is limited per turn and per user (`turn_disk` and
  `user_disk` in the agent's `[agents.sandbox]`, by default a quarter and half of `scratch`): no file may
  outgrow the turn's limit, each file or folder also counts 1 KB, files deleted but still open count
  until closed, and a turn that goes over either limit has its calls stopped and its workspace emptied.
  The sandbox checks usage twice a second, so a fast writer can briefly overshoot; `scratch` stays the
  hard ceiling. A call cannot start a new session or process group (`setsid`, `setpgid`), so everything
  it starts ends with it, and it may hold at most 4096 open files. A call cannot read another turn's files, see other
  processes, signal them, make socket files, use shared memory or message queues, or open network
  sockets. Users can share an agent without seeing each other. An agent that needs extra software ships a Dockerfile built on the AstralVessel image.
- **Service tools** call the application itself: the vessel sends the model's arguments to an endpoint
  the deployment grants, with credentials the deployment holds, so the sandbox never needs network
  access or a secret. The application learns who asked through `X-Vessel-Owner` (the app and its user,
  or the issuer and subject), `X-Vessel-Agent` and `X-Vessel-Tool` headers.
- **The egress** is the only way out. It adds the model key, so the harness never holds it, and
  forwards model calls only for the vessel, which holds a token derived at deploy time (so an agent's
  model `api_key` stays `env:VESSEL_MODEL_KEY`, the default). Its
  web proxy reaches only globally routable addresses on ports 80 and 443.
- **Web search** runs on a SearXNG container the deployment starts beside the egress: no account or
  key, several engines at once so one engine's CAPTCHA or rate limit leaves the others answering, and
  all of its traffic through the egress. Only the vessel can reach it. A failed or refused search
  reaches the model as an error, never as "no results". `[search] provider = "duckduckgo"` in
  `deployment.toml` scrapes DuckDuckGo instead, with no extra container, but DuckDuckGo challenges
  repeated queries.

Nothing in the running system controls Docker. `astralvessel deploy` turns `deployment.toml` into a
Compose file, the operator starts it, and the vessel serves an agent only once its sandbox reports that
it is confined and was started from the current version of its sandbox profile, its manifest and its
tools (a profile fingerprint set at deploy time, so an edit that was not redeployed is caught). Deploy labels the vessel
with the deployment file and those fingerprints, so `up -d` recreates it whenever they change. Prompt
files are read on every turn and need no redeploy.
Applications join the vessel's `front` network to reach it, and to receive its service calls.

## What it protects, and what it trusts

AstralVessel assumes the model, and everything the model reads (web pages, data, tool output), may be
hostile: a prompt injection can make the agent try anything its tools allow. Against that it keeps:

- the model key in the egress alone, and app keys as hashes in the vessel;
- conversation history with the application, never in the vessel or a sandbox;
- data read-only, tool calls with no network (a sandbox's only link is to the vessel), and each call out
  of other calls' files and processes;
- each user's tasks bound to their owner, and each app limited to the agents it was granted;
- agent-driven web traffic to public addresses on ports 80 and 443 only (the model endpoint is the
  operator's choice).

It trusts the operator and the host's kernel and Docker, the application to name its users honestly (or
the token issuers the deployment lists), and the model provider and search engines with what is sent to
them. Out of scope: kernel or container escapes, and what the agent says in its answer. An agent can
pass anything it may read to its user, and with web tools it can send it to any public site, so give web
tools only to agents whose data may leave.

## Calling an agent

Each agent has an A2A endpoint at `/agents/<id>` and a public card at
`/agents/<id>/.well-known/agent-card.json`. Two ways to ask:

- **A conversation turn:** a text message, with the history as an
  `application/vnd.astralvessel.transcript+json` part. A caller that lists that media type in
  `acceptedOutputModes` gets the turn's messages streamed back to store; the bundled client does.
- **A structured skill call,** the shape AstralDeep uses for external agents: a data part
  `{"method": "tools/call", "name": "<id>", "arguments": {"question": "..."}}`, usually with a
  non-streaming `SendMessage`. The answer comes back as the task's `answer` artifact, with any
  AstralPrimitives components under `_ui_components`, which AstralDeep renders.

A token's `tools:*` scopes, or an `X-Vessel-Scopes` header from an app, narrow the tools an agent may
use on that call. They can only remove tools, never add them.

```toml
# deployment.toml: trust an identity provider's tokens for some agents
[[issuers]]
id = "astral"
issuer = "https://iam.example.edu/realms/Astral"
audience = "research-vessel"
jwks_url = "https://iam.example.edu/realms/Astral/protocol/openid-connect/certs"
authorized_parties = ["astral-orchestrator"]
agents = ["records-agent"]
```

Only asymmetric signatures are accepted (RSA, ECDSA, EdDSA); shared-secret and unsigned tokens are
refused. Keys are fetched through the egress and refreshed when a token names a new one.

## Declaring tools

```toml
# tools/species.toml: a command run in the sandbox, given the arguments as JSON on stdin
name = "species"
description = "List every species in the guide as JSON."
command = ["python3", "{package}/tools/species.py"]
scope = "tools:read"

# tools/lookup.toml: a service tool, answered by the application over HTTP
name = "lookup"
description = "Look a record up."
service = "records"
path = "/agent-tools/lookup"
```

```toml
# deployment.toml: the operator grants the service and its credentials
[egress]
upstream_url = "env:VESSEL_UPSTREAM_MODEL_URL"   # required: any OpenAI-compatible endpoint

[[agents]]
id = "records-agent"
package = "agents/records"
[agents.services.records]
url = "http://app:8000"
headers = { "X-Internal-Token" = "env:RECORDS_TOKEN" }
```

Each tool has one of Astral's six permission scopes, and an agent's card carries the strongest of its
tools' scopes as a `scope:` tag Astral reads.

### Files from tools

An agent that lists file types in its manifest (`[output] files = ["png", "svg", "csv", "pdf"]`, any
of these) can return files. A sandbox call writes them into `$VESSEL_OUTBOX`. After the call, the
sandbox takes every entry out of the outbox and keeps regular files with a listed extension, a plain
name and valid content, up to 4 MB each and 4 per call. A turn returns at most 8 files and 16 MB. The
model is told which files were attached and why any others were refused.

Each file streams to the caller as an A2A artifact named `file` (raw bytes, `mediaType`, `filename`,
and the tool's `call_id` in its metadata). Callers that list `acceptedOutputModes` get only the types
they list. The bundled client yields them as `file` events. Like the transcript, files go out on the
live stream only, and the app stores what it wants to keep. An SVG can carry script, so show it as
an `<img>` or a download, never inline.

## What is here

| Path | What |
|---|---|
| `astralvessel/` | The harness: agent loop, model adapter, tools, confinement, sandbox server, service client, A2A server, deployment loader, Compose generator, client |
| `astralvessel/history.py` | An application-side conversation store, for apps that want one |
| `egress/` | The egress container: model passthrough that holds the key, and the guarded web proxy |
| `examples/field-guide/` | An example agent over a small sample field guide, with a declared command tool |
| `examples/field-guide-reader/` | A second, read-only example agent over the same data, with no shell or web access |
| `deploy/` | The example `deployment.toml` and `.env.example`, for a manual deployment of both example agents |
| `stress/load.py` | A load test: many concurrent users, long histories, cancellations and flooding tool output |
| `tests/` | Unit and service tests with a scripted model |

## Quickstart

```bash
uv tool install astralvessel              # or: pipx install astralvessel
mkdir field-guide && cd field-guide
astralvessel up --example                 # asks for your model URL, key and model; starts and checks it
astralvessel chat field-guide             # saves chats in chats/ and can resume them
astralvessel ask field-guide "Which birds hunt fish?"
```

`up` writes `deployment.toml` and a private `.env`, starts the containers, waits until every sandbox
reports it is confined, runs the isolation probes against every agent, and tests the model key. An
installed copy runs the images released with it, `ghcr.io/astraldeep/astralvessel` and
`ghcr.io/astraldeep/astralvessel-egress` (amd64 and arm64); a checkout builds them from source.

Sandboxes need a Linux kernel with Landlock ABI 6 or newer (6.12+); without it they refuse to start.
To do each step yourself, see [Manual deployment](https://astraldeep.github.io/AstralVessel/run/manual-deployment/).

## Develop

```bash
uv venv -p 3.11 && uv pip install -e . pytest pytest-asyncio ruff
uv pip install astralprims                                       # optional: validates primitives
.venv/bin/python -m pytest -q
.venv/bin/python stress/load.py --users 50 --turns 3 --history 1000 --cancels 20
```

The vessel refuses to start without sandboxes unless `VESSEL_DEV_UNSANDBOXED=1`, which runs tools in
its own process, unconfined. That mode is for tests only: never point a model at it.

## Contributing

- Work on a branch named for the change and open a pull request against `main`. CI (lint, tests on
  Python 3.11 and 3.12, and isolation probes on the running example) must pass, and a maintainer
  reviews before it merges.
- Before pushing, run `ruff check .` and `.venv/bin/python -m pytest -q`. Add a test with each fix
  or feature.
- AstralVessel stays application-neutral: no code, examples, prompts or docs about a particular
  application that uses it. An application's own agents live in that application's repo.
- Never commit secrets, `.env` files, or generated Compose output (`deploy/compose.generated.yml`,
  `deploy/.generated/`).
- CI also starts the example deployment and runs `astralvessel check` against it.
- Releases: bump `version` (X.Y.Z) in `pyproject.toml` on `main`, run the Release workflow by hand
  as a dry run, then push the tag `v<version>` on `main`. It publishes the images, waits for a
  maintainer's approval of the `pypi` environment, checks the images are public, then publishes the
  package.
- The documentation lives in `docs/` (built with MkDocs). Update it, and this README where it applies, in the same
  pull request when behavior changes.

## Status

Version 0.5.0, November 2026, the first release: `pip install astralvessel` and images at
`ghcr.io/astraldeep/astralvessel`. Done:

- one vessel serving many agents, each with its own sandbox, card and A2A address
- `deployment.toml`, per-app keys, and Compose generated by `astralvessel deploy`
- per-call confinement inside each sandbox (Landlock and seccomp)
- a workspace per turn that its tool calls share, deleted when the turn ends
- files from tools (PNG, SVG, CSV, PDF), checked twice and streamed as A2A artifacts
- application-owned history, with transcripts streamed in OpenAI chat format
- service tools, image input, and per-agent sandbox images
- verified-token callers (issuer allowlist, audience, owners keyed by issuer and subject), per-call
  scope narrowing, and structured skill calls in the shape AstralDeep sends
- fetched web content fenced and labeled as untrusted before the model reads it
- web search through a private SearXNG container, with failures reported as errors
- a task store that survives restarts, owner binding, cancellation, and AstralPrimitives output
- unit and service tests, isolation probes, a load test, and an independent security review
- CI on every pull request: lint, tests, image builds, and isolation probes on a running deployment
- releases from a version tag: multi-architecture images and the PyPI package

Load test, in one process on one core with a fake model (the load generator shares that core): 50
concurrent users, three turns each, every turn sending a 1,000-message history, running a confined
shell command and streaming 1,000 pieces of text, plus 20 cancellations mid-turn. No failures, a median
turn of about 6 seconds, peak memory about 380 MB, and no scratch left behind.

Not yet:

- jobs: submitting without holding a stream open, completion callbacks, structured results
- pausing a turn for approval or a policy check before a tool runs
- usage and audit export
- an admin API and a launcher that starts sandboxes at runtime
- writable mounts, stronger isolation tiers (gVisor, microVMs), and MCP tool servers

For now AstralVessel is an independent project. Whether and how it joins AstralDeep is an open
question for the Astral maintainers.

Licensed under Apache 2.0, like the rest of Astral.
