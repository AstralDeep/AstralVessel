# AstralVessel

A lightweight, open agent harness and the standard behind it. AstralVessel is a server for agents that
sits between an application and its model provider: it takes questions over **A2A 1.0**, uses any
**OpenAI-compatible** model, runs each agent's tools in its own sealed **sandbox**, reaches the outside
world only through an **egress** door, and answers in Markdown with optional **AstralPrimitives** so
Astral's clients can render the result.

It is the harness and sandbox parts of [Astral](https://github.com/AstralDeep), packaged so an
application can use them without running a full Astral instance.

![Diagram: on the left, a full Astral instance with its web, Windows, Android and Apple clients, accounts and sign-in, an agent catalog of many agents, and a highlighted harness and sandbox. On the right, one app such as the research nexus, using AstralVessel: one agent folder (vessel.toml, prompt.md, tools/) plus a harness, sandbox and egress. A solid arrow from left to right reads "used on its own"; a dashed arrow back reads "could slot back in later".](assets/lifted-out.svg)

## Why it exists

Astral is a centralized platform: one deployment serves many users, many agents, and Astral's own web,
Windows, Android and Apple clients. Some applications need only a small piece of that: one agent,
answering questions inside one app. The research nexus (uk-knowledgegraph) is the clearest case. It
needs an agent that searches a generated wiki of University of Kentucky research and answers questions
on the site. It does not need Astral's accounts, agent catalog, or client apps.

Today that kind of app gets its agent loop from **OpenCode**, a coding assistant run headless and bent
into a research agent. Two projects do this, uk-knowledgegraph and InFocus-Agent, and both surround
OpenCode with the same hand-built scaffolding:

- a sandboxed container
- an egress proxy
- an HTTP client for OpenCode's private API
- a 371-line translator for its event stream, identical in both repos
- session cleanup
- configuration that switches off the coding features a research agent must not have

That is about 1,000 lines of glue in InFocus-Agent and 2,300 in uk-knowledgegraph. It breaks when
OpenCode changes its private API, and it still runs a Node.js coding tool to answer research questions.

AstralVessel replaces OpenCode and that scaffolding with one component that is:

- **Lighter.** It is a Python package and a container. It has no coding-assistant features to switch
  off and no private API that shifts between releases.
- **Open.** Every interface is a published standard: A2A for conversations, the OpenAI-compatible API
  for models, and AstralPrimitives for rich output. A conformance suite checks any harness against
  them, and the code is under Apache 2.0 like the rest of Astral.
- **Astral-ready.** It speaks the same protocols as AstralDeep. Eventually it could slot in as the
  harness and sandbox AstralDeep uses, and an agent built for AstralVessel could move into Astral
  without being rewritten. A small, self-contained harness could also help run agents closer to
  Astral's clients when that makes sense.

## How it works

An agent is a folder: a manifest (`vessel.toml`), a briefing (`prompt.md`), and optional declared
tools, each a small TOML file wrapping a command. A deployment file (`deployment.toml`) lists the
agents one vessel serves, the sandbox each one gets, and which applications may call which agents.
The agent declares what it needs; the deployment file is the only place access is granted.

![Diagram of who holds what. The app holds its key and its users' conversation history, names each user with X-Vessel-User, and is also on an outside network for its users' browsers. It calls the vessel over A2A 1.0. The vessel serves every agent and runs the agent loop; it holds only runs in flight, never conversation history or the model key. It calls each agent's sandbox over /run (HTTP); there is one sandbox per agent, which runs the tools on read-only data and never has network, secrets, history, or a route to the vessel's API. The vessel reaches the egress over the OpenAI-compatible API, which is also the web proxy for webfetch and websearch. The egress holds the model key, reaches only public addresses on ports 80 and 443, and calls the model over HTTPS: GLM 5.3 on the UK LLM Factory, or any OpenAI-compatible model. Everything except the egress's outbound link and the app's browser-facing side sits on internal Docker networks with no route out.](assets/who-holds-what.svg)

- **The app** holds its own key, which the deployment limits to certain agents. It names each end user
  with an `X-Vessel-User` header; every task is bound to that app and user. The app owns conversation
  history: it sends the history with each turn and stores the messages that come back.
- **The vessel** serves every agent in the deployment, each with its own A2A address and agent card.
  It runs the agent loop (ask the model, run the tool it asks for, return the result, repeat), streams
  every step as A2A events, and streams the turn's messages as a transcript in OpenAI chat format. It
  keeps only runs in flight, and enforces step limits, deadlines and cancellation. Agent briefings are
  fixed at setup: history from the app may never carry a system message.
- **Each agent's sandbox** is a separate container that runs its file, shell and declared tools. It has
  no network route, no secrets, no history, a read-only root, and its data mounted read-only. Inside
  it, every tool call runs under Landlock with its own scratch directory: a call cannot read another
  call's files, see other processes, signal them, or open network connections. Users can share an
  agent without seeing each other.
- **The egress** is the only way out. It adds the model key, so the harness never holds it. Its
  web proxy reaches only globally routable addresses on ports 80 and 443.

Nothing in the running system controls Docker. `astralvessel deploy` turns `deployment.toml` into a
Compose file, the operator starts it, and the vessel serves an agent only once its sandbox reports the
profile the deployment expects.

## What is here

| Path | What |
|---|---|
| `astralvessel/` | The harness: agent loop, model adapter, tools, Landlock confinement, sandbox server, A2A server, deployment loader, Compose generator, client |
| `astralvessel/history.py` | An application-side conversation store, for apps that want one (the demo uses it) |
| `egress/` | The egress container: model passthrough that holds the key, and the guarded web proxy |
| `examples/nexus/` | The research nexus agent over the ccts-kg vault, with two declared tools |
| `examples/vault-reader/` | A second, read-only agent over the same vault, with no shell or web access |
| `demo/` | An interactive demo app that keeps its personas' history and shows each turn's steps and A2A frames |
| `deploy/` | The demo `deployment.toml`, the demo app's Compose overlay, `.env.example`, and `verify-isolation.sh` |
| `conformance/run.py` | Conformance checks: what any harness claiming to follow the standard must pass |
| `tests/` | Unit and service tests with a scripted model |

## Run the demo

```bash
cp deploy/.env.example deploy/.env      # fill in the model key, app key and its sha256, sandbox token, vault path
cd deploy
astralvessel deploy deployment.toml      # (installed as in Develop) writes compose.generated.yml and sandbox tokens
docker compose -f compose.generated.yml -f compose.demo.yml --env-file .env up -d --build
./verify-isolation.sh                    # 24 isolation probes from inside the containers
set -a; . ./.env; set +a
for agent in nexus vault-reader; do
  docker compose -f compose.generated.yml -f compose.demo.yml --env-file .env run --rm --no-deps -T \
    -v "$PWD/../conformance:/conformance:ro" demo python /conformance/run.py http://vessel:8700 "$VESSEL_APP_KEY" $agent
done                                     # 20 conformance checks per agent
```

The demo listens on `127.0.0.1:8790` and on `DEMO_TAILSCALE_IP:8790`. Sandboxes need a Linux kernel
with Landlock ABI 6 or newer (6.12+); without it they refuse to start.

## Develop

```bash
uv venv -p 3.11 && uv pip install -e . pytest pytest-asyncio ruff
uv pip install -e ../AstralDeep/components/AstralPrimitives      # optional: validates primitives
.venv/bin/python -m pytest -q
```

Without `VESSEL_SANDBOX_URL_TEMPLATE` the vessel runs tools in its own process, unconfined. That mode
is for tests only: never point a model at it.

## Status

Version 0.2, November 2026. Done:

- one vessel serving many agents, each with its own sandbox, card and A2A address
- `deployment.toml`, per-app keys, and Compose generated by `astralvessel deploy`
- per-call Landlock confinement inside each sandbox
- application-owned history, with transcripts streamed in OpenAI chat format
- a task store that survives restarts, owner binding, cancellation, and AstralPrimitives output
- 46 tests, 24 isolation probes, 20 conformance checks per agent

Not yet:

- jobs: submitting without holding a stream open, completion callbacks, structured results
- pausing a turn for approval or a policy check before a tool runs
- file input and output, usage and audit export, signed user identity
- an admin API and a launcher that starts sandboxes at runtime
- stronger isolation tiers (gVisor, microVMs), and MCP tool servers
- InFocus-Agent on AstralVessel, and the research nexus's own end-to-end suite against it

For now AstralVessel is an independent project. Whether and how it joins AstralDeep is an open
question for the Astral maintainers.

Licensed under Apache 2.0, like the rest of Astral.
