# AstralVessel

A lightweight, open agent harness and the standard behind it. AstralVessel is a server for agents that
sits between an application and its model provider: it takes questions over **A2A 1.0**, uses any
**OpenAI-compatible** model, runs each agent's tools in its own sealed **sandbox**, reaches the outside
world only through an **egress** door, and answers in Markdown with optional **AstralPrimitives** so
Astral's clients can render the result.

It is the harness and sandbox parts of [Astral](https://github.com/AstralDeep), packaged so an
application can use them without running a full Astral instance.

![Diagram: on the left, a full Astral instance with its web, Windows, Android and Apple clients, accounts and sign-in, an agent catalog of many agents, and a highlighted harness and sandbox. On the right, one app such as a team's web app, using AstralVessel for its agents: an agent folder (vessel.toml, prompt.md, tools/) plus a harness, sandbox and egress. A solid arrow from left to right reads "used on its own"; a dashed arrow back reads "could slot back in later".](assets/lifted-out.svg)

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
  for models, and AstralPrimitives for rich output. A conformance suite checks any harness against
  them, and the code is under Apache 2.0 like the rest of Astral.
- **Astral-ready.** It speaks the same protocols as AstralDeep. Eventually it could slot in as the
  harness and sandbox AstralDeep uses, and an agent built for AstralVessel could move into Astral
  without being rewritten.

## How it works

An agent is a folder: a manifest (`vessel.toml`), a briefing (`prompt.md`), and optional declared
tools, each a small TOML file. A deployment file (`deployment.toml`) lists the agents one vessel
serves, the sandbox and services each one gets, and which applications may call which agents. The
agent declares what it needs; the deployment file is the only place access is granted.

![Diagram of who holds what. The app holds its key and its users' conversation history, names each user with X-Vessel-User, and is also on an outside network for its users' browsers. It calls the vessel over A2A 1.0. The vessel serves every agent and runs the agent loop; it holds only runs in flight, never conversation history or the model key. It calls each agent's sandbox over /run (HTTP); there is one sandbox per agent, which runs the tools on read-only data and never has network, secrets, history, or a route to the vessel's API. The vessel reaches the egress over the OpenAI-compatible API, which is also the web proxy for webfetch and websearch. The egress holds the model key, reaches only public addresses on ports 80 and 443, and calls the model over HTTPS: GLM 5.3 on the UK LLM Factory, or any OpenAI-compatible model. Everything except the egress's outbound link and the app's browser-facing side sits on internal Docker networks with no route out.](assets/who-holds-what.svg)

- **The app** holds its own key, which the deployment limits to certain agents. It names each end user
  with an `X-Vessel-User` header; every task is bound to that app and user. The app owns conversation
  history: it sends the history with each turn and stores the messages that come back. It may attach
  images for agents whose model accepts them.
- **The vessel** serves every agent in the deployment, each with its own A2A address and agent card.
  It runs the agent loop (ask the model, run the tool it asks for, return the result, repeat), streams
  every step as A2A events, and streams the turn's messages as a transcript in OpenAI chat format. It
  keeps only runs in flight, and enforces step limits, deadlines and cancellation. Agent briefings are
  fixed at setup: history from the app may never carry a system message.
- **Each agent's sandbox** is a separate container that runs its file, shell and command tools. It has
  no network route, no secrets, no history, a read-only root, and its data mounted read-only. Inside
  it, every tool call runs under Landlock and a seccomp filter with its own scratch directory: a call
  cannot read another call's files, see other processes, signal them, make socket files, use shared
  memory or message queues, or open network sockets. Users can share an agent without seeing each
  other. An agent that needs extra software ships a Dockerfile built on the AstralVessel image.
- **Service tools** call the application itself: the vessel sends the model's arguments to an endpoint
  the deployment grants, with credentials the deployment holds, so the sandbox never needs network
  access or a secret. The application learns which app, user and agent asked through `X-Vessel-*`
  headers.
- **The egress** is the only way out. It adds the model key, so the harness never holds it. Its
  web proxy reaches only globally routable addresses on ports 80 and 443.

Nothing in the running system controls Docker. `astralvessel deploy` turns `deployment.toml` into a
Compose file, the operator starts it, and the vessel serves an agent only once its sandbox reports that
it is confined and was started from the current deployment file (a profile fingerprint set at deploy
time, so an edited deployment that was not redeployed is caught). Applications join the vessel's
`front` network to reach it, and to receive its service calls.

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
[[agents]]
id = "records-agent"
package = "agents/records"
[agents.services.records]
url = "http://app:8000"
headers = { "X-Internal-Token" = "env:RECORDS_TOKEN" }
```

Each tool has one of Astral's six permission scopes, and an agent's card carries the strongest of its
tools' scopes as a `scope:` tag Astral reads.

## What is here

| Path | What |
|---|---|
| `astralvessel/` | The harness: agent loop, model adapter, tools, confinement, sandbox server, service client, A2A server, deployment loader, Compose generator, client |
| `astralvessel/history.py` | An application-side conversation store, for apps that want one |
| `egress/` | The egress container: model passthrough that holds the key, and the guarded web proxy |
| `examples/field-guide/` | An example agent over a small sample field guide, with a declared command tool |
| `examples/field-guide-reader/` | A second, read-only example agent over the same data, with no shell or web access |
| `deploy/` | The example `deployment.toml`, `.env.example`, and `verify-isolation.sh` |
| `conformance/run.py` | Conformance checks: what any harness claiming to follow the standard must pass |
| `stress/load.py` | A load test: many concurrent users, long histories, cancellations and flooding tool output |
| `tests/` | Unit and service tests with a scripted model |

## Run the example

```bash
cp deploy/.env.example deploy/.env      # fill in the model key, app key and its sha256, and the sandbox token
cd deploy
astralvessel deploy deployment.toml      # (installed as in Develop) writes compose.generated.yml and sandbox tokens
docker compose -f compose.generated.yml --env-file .env up -d --build
./verify-isolation.sh                    # 28 isolation probes from inside the containers
set -a; . ./.env; set +a
for agent in field-guide field-guide-reader; do
  docker run --rm --network astralvessel_front -v "$PWD/../conformance:/conformance:ro" astralvessel:local \
    python /conformance/run.py http://172.31.247.10:8700 "$VESSEL_APP_KEY" $agent
done                                     # 20 conformance checks per agent
```

Sandboxes need a Linux kernel with Landlock ABI 6 or newer (6.12+); without it they refuse to start.

## Develop

```bash
uv venv -p 3.11 && uv pip install -e . pytest pytest-asyncio ruff
uv pip install -e ../AstralDeep/components/AstralPrimitives      # optional: validates primitives
.venv/bin/python -m pytest -q
.venv/bin/python stress/load.py --users 50 --turns 3 --history 1000 --cancels 20
```

The vessel refuses to start without sandboxes unless `VESSEL_DEV_UNSANDBOXED=1`, which runs tools in
its own process, unconfined. That mode is for tests only: never point a model at it.

## Status

Version 0.3, November 2026. Done:

- one vessel serving many agents, each with its own sandbox, card and A2A address
- `deployment.toml`, per-app keys, and Compose generated by `astralvessel deploy`
- per-call confinement inside each sandbox (Landlock and seccomp)
- application-owned history, with transcripts streamed in OpenAI chat format
- service tools, image input, and per-agent sandbox images
- a task store that survives restarts, owner binding, cancellation, and AstralPrimitives output
- unit and service tests, isolation probes, conformance checks, a load test, and an independent
  security review

Load test, in one process on one core with a fake model (the load generator shares that core): 50
concurrent users, three turns each, every turn sending a 1,000-message history, running a confined
shell command and streaming 1,000 pieces of text, plus 20 cancellations mid-turn. No failures, a median
turn of about 6 seconds, peak memory about 380 MB, and no scratch left behind.

Not yet:

- jobs: submitting without holding a stream open, completion callbacks, structured results
- pausing a turn for approval or a policy check before a tool runs
- files from tools, usage and audit export, signed user identity
- an admin API and a launcher that starts sandboxes at runtime
- writable mounts, stronger isolation tiers (gVisor, microVMs), and MCP tool servers

For now AstralVessel is an independent project. Whether and how it joins AstralDeep is an open
question for the Astral maintainers.

Licensed under Apache 2.0, like the rest of Astral.
