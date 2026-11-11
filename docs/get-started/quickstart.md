# Quickstart

This page takes you from a fresh checkout to a running deployment of the two example agents, a passing isolation
check, and a first streamed answer. It takes about ten minutes, most of it building images.

## Before you start

You need a Linux host with kernel 6.12 or newer (Landlock ABI 6), Docker with Compose, Python 3.11 or newer, git,
and an OpenAI-compatible model endpoint with an API key.

!!! note "Installing from PyPI"
    Releases publish the `astralvessel` package to PyPI and its images to GHCR (see
    [Upgrading and releases](../run/upgrading.md)). No release has been tagged yet, so this quickstart installs
    from a checkout, which builds the images locally.

## 1. Install the command

```bash
git clone https://github.com/AstralDeep/AstralVessel.git
cd AstralVessel
uv venv -p 3.11 && uv pip install -e .
source .venv/bin/activate
astralvessel --help
```

Without uv, `python3 -m venv .venv && .venv/bin/pip install -e .` does the same.

## 2. Fill in the secrets

The example deployment lives in `deploy/`. Create `deploy/.env` with five values: your model endpoint and key, an app
key and its SHA-256, and a master secret for the sandboxes.

```bash
cd deploy
python3 - > .env <<'PY'
import hashlib, secrets
app = secrets.token_urlsafe(32)
print("VESSEL_UPSTREAM_MODEL_URL=https://api.example.com/v1")
print("VESSEL_UPSTREAM_MODEL_KEY=replace-with-your-model-key")
print(f"VESSEL_APP_KEY={app}")
print(f"VESSEL_APP_KEY_SHA256={hashlib.sha256(app.encode()).hexdigest()}")
print(f"VESSEL_SANDBOX_TOKEN={secrets.token_urlsafe(32)}")
PY
chmod 600 .env
```

Then replace the example URL with your provider's OpenAI-compatible base URL (including the `/v1` path) and
`replace-with-your-model-key` with your real key. `deploy/.env` is ignored by git.

| Variable | Used by | What it is |
|---|---|---|
| `VESSEL_UPSTREAM_MODEL_URL` | egress | Your model endpoint's base URL. |
| `VESSEL_UPSTREAM_MODEL_KEY` | egress | Your model provider's API key. Only the egress container receives it. |
| `VESSEL_APP_KEY` | your application | The key the example app sends as `Authorization: Bearer`. The vessel never sees it. |
| `VESSEL_APP_KEY_SHA256` | vessel | The hex SHA-256 of the app key, which the vessel compares against. |
| `VESSEL_SANDBOX_TOKEN` | `astralvessel deploy`, vessel | A master secret of 16 or more characters. Each sandbox gets its own token derived from it. |

## 3. Point it at your model

`deploy/deployment.toml` reads the endpoint and key from the environment:

```toml
[egress]
upstream_url = "env:VESSEL_UPSTREAM_MODEL_URL"
upstream_key = "env:VESSEL_UPSTREAM_MODEL_KEY"
```

`upstream_url` is required; there is no default provider. You can also write the URL into the file directly.
Both example agents name a model in `examples/*/vessel.toml`; set `model` in each to a model your endpoint serves:

```toml
[model]
model = "your-model-name"
```

## 4. Generate and start the deployment

```bash
astralvessel deploy deployment.toml
docker compose -f compose.generated.yml --env-file .env up -d --build
```

`deploy` writes `compose.generated.yml`, plus a `.generated/` folder of per-container secrets beside it, and prints
each agent's sandbox profile fingerprint. Both are ignored by git.

The vessel serves an agent only after its sandbox reports that it is confined and running the deployed profile.
Wait for both agents:

```bash
docker compose -f compose.generated.yml --env-file .env logs vessel | grep 'sandbox is ready'
```

You should see one line for `field-guide` and one for `field-guide-reader`.

## 5. Check the isolation

```bash
./verify-isolation.sh
```

It runs 41 probes from inside the containers (42 with 0.5.0) and ends with `41 passed, 0 failed`. Every probe is
listed in [Isolation and probes](../security/isolation.md). The script targets the example deployment's agent ids.

## 6. Ask a question

The vessel listens only on its internal `front` network and publishes no ports, so run the client from a container
attached to that network. The image built in step 4 already has the client installed.

```bash
set -a; . ./.env; set +a
docker run --rm -i --network astralvessel_front -e VESSEL_APP_KEY astralvessel:local python - <<'PY'
import asyncio, os
from astralvessel.client import VesselClient

async def main():
    client = VesselClient("http://172.31.247.10:8700", os.environ["VESSEL_APP_KEY"])
    async for event in client.stream("field-guide", "alice", "Which birds in the guide hunt fish?"):
        if event["type"] == "tool.started":
            print(f"\n[{event['name']}]")
        elif event["type"] == "answer":
            print("\n" + event["text"])
        elif event["type"] == "turn.finished":
            print(f"\n{event['status']}, {event['steps']} steps")
    await client.close()

asyncio.run(main())
PY
```

`172.31.247.10` is the default `front_address`, and `alice` is the end user your application names. The answer
streams as `text.delta` events too; this script prints only the finished answer.

## 7. Run the conformance checks (optional)

The conformance suite drives an agent over its public interfaces with real model calls, 20 checks per agent:

```bash
for agent in field-guide field-guide-reader; do
  docker run --rm --network astralvessel_front -v "$PWD/../conformance:/conformance:ro" astralvessel:local \
    python /conformance/run.py http://172.31.247.10:8700 "$VESSEL_APP_KEY" $agent
done
```

## Stop it

```bash
docker compose -f compose.generated.yml --env-file .env down
```

Add `-v` to also delete the vessel's task store volume.

## Next steps

- [How it works](how-it-works.md): the containers and the path of one turn.
- [The agent folder](../build/agent-folder.md): write your own agent.
- [Connecting an application](../run/applications.md): call agents from your own service.
