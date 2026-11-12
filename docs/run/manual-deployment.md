# Manual deployment

`astralvessel up` runs these steps for you. Do them by hand when you write `deployment.toml` yourself, keep secrets
somewhere other than `.env`, or script the deployment. The example here is the repository's `deploy/` folder, which
serves both example agents.

## 1. Fill in the secrets

Create `.env` beside `deployment.toml` with five values: your model endpoint and key, an app key and its SHA-256,
and a master secret for the sandboxes.

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

Then put in your provider's base URL (with the `/v1` path) and your real key.

| Variable | Used by | What it is |
|---|---|---|
| `VESSEL_UPSTREAM_MODEL_URL` | egress | Your model endpoint's base URL. |
| `VESSEL_UPSTREAM_MODEL_KEY` | egress | Your model provider's API key. Only the egress container receives it. |
| `VESSEL_APP_KEY` | your application | The key the app sends as `Authorization: Bearer`. The vessel never sees it. |
| `VESSEL_APP_KEY_SHA256` | vessel | The hex SHA-256 of the app key, which the vessel compares against. |
| `VESSEL_SANDBOX_TOKEN` | `astralvessel deploy`, vessel | A master secret of 16 or more characters. Each sandbox gets its own token derived from it. |

## 2. Point it at your model

`[egress]` names the endpoint and key, here read from the environment:

```toml
[egress]
upstream_url = "env:VESSEL_UPSTREAM_MODEL_URL"
upstream_key = "env:VESSEL_UPSTREAM_MODEL_KEY"
```

Set the model per agent in `deployment.toml`, which overrides the one the agent folder names:

```toml
[[agents]]
id = "field-guide"
package = "../examples/field-guide"
model = "your-model-name"
```

## 3. Generate and start

```bash
astralvessel deploy deployment.toml
docker compose -f compose.generated.yml --env-file .env up -d --build
```

`deploy` writes `compose.generated.yml`, plus a `.generated/` folder of per-container secrets beside it, and prints
each agent's sandbox profile fingerprint. See [CLI](../reference/cli.md#astralvessel-deploy).

The vessel serves an agent only after its sandbox reports that it is confined and running the deployed profile. Its
log says so for each agent:

```bash
docker compose -f compose.generated.yml --env-file .env logs vessel | grep 'sandbox is ready'
```

## 4. Check the isolation

```bash
astralvessel check
```

It probes every agent from inside the containers and exits non-zero if any probe fails. See
[Isolation and probes](../security/isolation.md).

## 5. Ask a question from your own code

The vessel listens only on its internal `front` network and publishes no ports, so a client runs in a container
attached to that network (`<project>_front`). The vessel image already has the client installed.

```bash
set -a; . ./.env; set +a
docker run --rm -i --network astralvessel_front -e VESSEL_APP_KEY astralvessel:local python - <<'PY'
import asyncio, os
from astralvessel.client import VesselClient

async def main():
    client = VesselClient("http://vessel:8700", os.environ["VESSEL_APP_KEY"])
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

Containers on the `front` network reach the vessel by its service name, `vessel`. `alice` is the user ID your
application sends with each turn. The script prints the finished answer; the text also arrives word by word as
`text.delta` events. `astralvessel chat` and `ask` work the same way against any deployment folder.

## Stop it

```bash
docker compose -f compose.generated.yml --env-file .env down
```

Add `-v` to also delete the vessel's task store volume.
