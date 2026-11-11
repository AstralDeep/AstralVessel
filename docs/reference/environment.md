# Environment variables

The generated Compose file sets these for you. You need them only to run `serve` or `sandbox` by hand, or to
understand what a container was given.

## Read by astralvessel deploy

| Variable | Meaning |
|---|---|
| `VESSEL_SANDBOX_TOKEN` | Master secret, 16 or more characters. Read from the environment or the env file. |

Every variable named by an `env:NAME` value in `deployment.toml` must be set when you run `docker compose`, which
fills it from the environment or `--env-file`.

## The vessel (`astralvessel serve`)

| Variable | Set by Compose to | Meaning |
|---|---|---|
| `VESSEL_DEPLOYMENT` | `/deploy/deployment.toml` | The deployment file |
| `VESSEL_PACKAGES_ROOT` | `/agents` | Agent folders are read from `<root>/<agent id>` instead of their host paths |
| `VESSEL_MODEL_URL` | `http://egress:8080/v1` | Default for agents' `[model] endpoint` |
| `VESSEL_MODEL_KEY` | the derived egress token (from `.generated/vessel.env`) | Default for agents' `[model] api_key` |
| `VESSEL_WEB_PROXY` | `http://egress:3128` | Proxy for `webfetch`, DuckDuckGo and issuer key fetches |
| `VESSEL_SEARCH_URL` | `http://search:8080` | SearXNG base URL; unset with `provider = "duckduckgo"` |
| `VESSEL_SANDBOX_URL_TEMPLATE` | `http://sandbox-{id}:8710` | Where each agent's sandbox listens |
| `VESSEL_SANDBOX_TOKEN` | the master secret | Each sandbox's token is derived from it |
| `VESSEL_TASK_DB` | `/data/tasks.sqlite` | The task store; default `./vessel-data/tasks.sqlite` |
| `VESSEL_HOST` | `front_address` | Listen address; default `127.0.0.1` |
| `VESSEL_PORT` | (unset) | Listen port; default `8700` |
| `VESSEL_DEV_UNSANDBOXED` | (unset) | `1` runs tools unconfined in the vessel process when no sandbox template is set. Tests only. |
| Each `env:` name used by `key_sha256` or a service grant | `${NAME}` | Resolved when the vessel starts |

## A sandbox (`astralvessel sandbox`)

| Variable | Set by Compose to | Meaning |
|---|---|---|
| `VESSEL_PACKAGE` | `/agent` | The agent folder |
| `VESSEL_PORT` | `8710` | Listen port |
| `VESSEL_SANDBOX_TOKEN` | the derived per-sandbox token (from `.generated/sandbox-<id>.env`) | Required, 16 or more characters |
| `VESSEL_PROFILE_HASH` | the agent's fingerprint | Reported on `/health` |
| `VESSEL_TURN_DISK`, `VESSEL_USER_DISK` | `turn_disk` and `user_disk` in bytes | 0.5.0 disk limits |
| `VESSEL_UNCONFINED` | (unset) | `1` starts without Landlock. Development only. |

## The egress

| Variable | Set by Compose to |
|---|---|
| `LLM_BASE_URL` | `[egress] upstream_url` |
| `LLM_API_KEY` | `[egress] upstream_key` |
| `EGRESS_TOKEN` | the derived egress token (from `.generated/egress.env`) |

Without `EGRESS_TOKEN` the egress refuses every model call.

## Inside a tool call

| Variable | Value |
|---|---|
| `PATH` | `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` |
| `LANG` | `C.UTF-8` |
| `HOME`, `TMPDIR` | The turn's workspace |
| `VESSEL_OUTBOX` | The outbox folder in the workspace |
