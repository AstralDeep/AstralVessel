# AstralVessel

A lightweight, open agent harness and the standard behind it. An AstralVessel agent is a folder (a
manifest, a briefing, declared tools) served over **A2A 1.0**, using any **OpenAI-compatible** model,
with its tools in a sealed **sandbox** and the model reached only through an **egress** door. Answers
are Markdown plus optional **AstralPrimitives**, so they render in Astral's clients, and the same
package is meant to run inside AstralDeep later.

It replaces the OpenCode-plus-scaffolding setup in uk-knowledgegraph and InFocus-Agent. Why, and the
plan: [PROPOSAL.md](PROPOSAL.md). Every concept from scratch: [docs/first-principles.md](docs/first-principles.md).
The normative standard: [spec/SPEC.md](spec/SPEC.md).

## What is here

| Path | What |
|---|---|
| `astralvessel/` | The reference harness: agent loop, model adapter, tools, sandbox runner, SQLite sessions, A2A server, client |
| `egress/` | The egress container: model passthrough that holds the key, and the guarded web proxy (from uk-knowledgegraph) |
| `examples/nexus/` | The nexus research agent over the ccts-kg vault, with two declared tools |
| `demo/` | The interactive demo app that sits in front of a Vessel, in the house design |
| `deploy/` | Docker Compose deployment, `.env.example`, and `verify-isolation.sh` |
| `conformance/run.py` | Conformance checks for any running harness |
| `spec/SPEC.md` | The standard |
| `tests/` | Unit and service tests with a scripted model |

## Architecture

```
browser ─▶ demo app ──A2A 1.0──▶ vessel (loop, sessions) ──HTTP──▶ sandbox (tools, read-only data, no network)
            holds the key            │
                                     └──▶ egress ──HTTPS──▶ model (GLM 5.3 on the UK LLM Factory)
                                          adds the model key; guarded proxy for webfetch/websearch
```

The harness and sandbox are on internal Docker networks with no route out. The sandbox holds no
conversation store and no secrets, so a hostile command cannot read other users' conversations or
the model key. The harness stores only a SHA-256 of the app's deployment key.

## Run the demo

```bash
cp deploy/.env.example deploy/.env      # fill in the model key, app key and its sha256, sandbox token, vault path
cd deploy && docker compose up -d --build
./verify-isolation.sh                    # 15 isolation probes from inside the containers
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

Version 0.1, built 2026-11-09 to 10. Done: the harness, the sandbox and egress deployment, the A2A
profile, owner binding, cancellation, primitives output rendered by AstralProjection, the nexus
example, 28 tests, 15 isolation probes, 19 conformance checks. Not yet: InFocus-Agent on Vessel, MCP
tool servers, a fresh sandbox per conversation, and running nexus's own end-to-end suite against it.

Licensed under Apache 2.0, like the rest of Astral.
