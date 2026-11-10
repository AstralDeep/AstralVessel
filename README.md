# AstralVessel

A lightweight, open agent harness and the standard behind it. AstralVessel runs a language-model agent
as a self-contained service: it takes questions over **A2A 1.0**, uses any **OpenAI-compatible**
model, runs the agent's tools in a sealed **sandbox**, reaches the outside world only through an
**egress** door, and answers in Markdown with optional **AstralPrimitives** so Astral's clients can
render the result.

It is the harness and sandbox parts of [Astral](https://github.com/AstralDeep), packaged so an
application can use them without running a full Astral instance.

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
tools, each a small TOML file wrapping a command. The harness serves that folder as one agent.

```
browser ─▶ app ──A2A 1.0──▶ vessel (loop, sessions) ──HTTP──▶ sandbox (tools, read-only data, no network)
          holds the key          │
                                 └──▶ egress ──HTTPS──▶ model (GLM 5.3 on the UK LLM Factory)
                                      adds the model key; guarded proxy for webfetch/websearch
```

- **The app** (here, the demo) holds a deployment key. It names each end user with an `X-Vessel-User`
  header, and every conversation is bound to the user who started it.
- **The vessel** runs the agent loop: ask the model, run the tool it asks for, return the result,
  repeat. It streams every step as A2A events, keeps conversations in SQLite, and enforces step
  limits, deadlines and cancellation.
- **The sandbox** runs the file, shell and declared tools. It is a separate container with no network
  route, no conversation store, no secrets, a read-only root, and the data mounted read-only. A hostile
  command cannot read other users' conversations or the model key, and it cannot reach the harness API.
- **The egress** is the only way out. It adds the model key, so the harness never holds it. Its
  web proxy reaches only globally routable addresses on ports 80 and 443.

## What is here

| Path | What |
|---|---|
| `astralvessel/` | The reference harness: agent loop, model adapter, tools, sandbox runner, SQLite sessions, A2A server, client |
| `egress/` | The egress container: model passthrough that holds the key, and the guarded web proxy |
| `examples/nexus/` | The research nexus agent over the ccts-kg vault, with two declared tools |
| `demo/` | An interactive demo app that sits in front of a Vessel and shows each turn's steps, A2A frames and isolation |
| `deploy/` | Docker Compose deployment, `.env.example`, and `verify-isolation.sh` |
| `conformance/run.py` | Conformance checks: what any harness claiming to follow the standard must pass |
| `tests/` | Unit and service tests with a scripted model |

## Run the demo

```bash
cp deploy/.env.example deploy/.env      # fill in the model key, app key and its sha256, sandbox token, vault path
cd deploy && docker compose up -d --build
./verify-isolation.sh                    # 18 isolation probes from inside the containers
set -a; . ./.env; set +a
docker compose run --rm --no-deps -T -v "$PWD/../conformance:/conformance:ro" demo \
  python /conformance/run.py http://vessel:8700 "$VESSEL_APP_KEY"   # 19 conformance checks
```

The demo listens on `127.0.0.1:8790` and on `DEMO_TAILSCALE_IP:8790`.

## Develop

```bash
uv venv -p 3.11 && uv pip install -e . pytest pytest-asyncio ruff
uv pip install -e ../AstralDeep/components/AstralPrimitives      # optional: validates primitives
.venv/bin/python -m pytest -q
```

Without `VESSEL_SANDBOX_URL` the harness runs tools in its own process. That mode is for tests only:
never point a model at it.

## Status

Version 0.1, built November 2026. Done:

- the harness, the sandbox and egress deployment, and the A2A profile
- owner binding, cancellation, and primitives output rendered by AstralProjection
- the nexus example and the interactive demo
- 39 tests, 18 isolation probes, 19 conformance checks, and an adversarial security pass

Not yet:

- InFocus-Agent on AstralVessel
- MCP tool servers
- a fresh sandbox per conversation (until then, users share one sandbox's scratch space and process list)
- a cap on conversation history length
- running the research nexus's own end-to-end suite against it

For now AstralVessel is an independent project. Whether and how it joins AstralDeep is an open
question for the Astral maintainers.

Licensed under Apache 2.0, like the rest of Astral.
