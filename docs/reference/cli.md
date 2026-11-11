# CLI

The `astralvessel` command has three subcommands. You run `deploy`; the containers run `serve` and `sandbox`.

```text
usage: astralvessel [-h] {deploy,serve,sandbox} ...
```

## astralvessel deploy

Writes a Compose file and its secrets from a deployment file.

```text
astralvessel deploy [deployment] [--out OUT] [--env-file ENV_FILE] [--up]
```

| Argument | Default | Meaning |
|---|---|---|
| `deployment` | `deployment.toml` | The deployment file |
| `--out` | `compose.generated.yml` beside the deployment | The Compose file to write; `.generated/` goes beside it |
| `--env-file` | `.env` beside the deployment | Read for `VESSEL_SANDBOX_TOKEN`, and passed to `docker compose` |
| `--up` | off | Also run `docker compose ... up -d --build --remove-orphans` |

What it does:

1. Reads the env file, if it exists, filling only variables not already set in the environment.
2. Requires `VESSEL_SANDBOX_TOKEN` of at least 16 characters.
3. Loads and checks the deployment and every agent folder.
4. Refuses to overwrite a Compose file that belongs to a different `project`.
5. Writes the Compose file and, in `.generated/`, the egress and vessel tokens, one token file per sandbox, and the
   SearXNG settings and secret (when search uses SearXNG).
6. Prints each file written, each agent's profile fingerprint, and the `docker compose` command to start it.

```text
$ astralvessel deploy deployment.toml
wrote /srv/vessel/compose.generated.yml
wrote /srv/vessel/.generated/egress.env
wrote /srv/vessel/.generated/vessel.env
wrote /srv/vessel/.generated/sandbox-field-guide.env
wrote /srv/vessel/.generated/sandbox-field-guide-reader.env
wrote /srv/vessel/.generated/searxng.yml
wrote /srv/vessel/.generated/search.env
  agent field-guide: sandbox profile 9f2ca421f9354a86
  agent field-guide-reader: sandbox profile 9b7ce11de4212579
start it with: docker compose -f /srv/vessel/compose.generated.yml --env-file /srv/vessel/.env up -d --build
```

Run it again after any change to `deployment.toml`, a `vessel.toml`, a declared tool file, or a sandbox Dockerfile.

## astralvessel serve

Serves every agent in a deployment. The vessel container runs it with its settings in the environment.

```text
astralvessel serve [deployment] [--host HOST] [--port PORT]
```

| Argument | Default |
|---|---|
| `deployment` | `$VESSEL_DEPLOYMENT` |
| `--host` | `$VESSEL_HOST`, else `127.0.0.1` |
| `--port` | `$VESSEL_PORT`, else `8700` |

It refuses to start without `VESSEL_SANDBOX_URL_TEMPLATE` (remote sandboxes) unless `VESSEL_DEV_UNSANDBOXED=1`.
See [Environment variables](environment.md).

## astralvessel sandbox

Runs one agent's sandbox tool server. Each sandbox container runs it.

```text
astralvessel sandbox [package] [--host HOST] [--port PORT]
```

| Argument | Default |
|---|---|
| `package` | `$VESSEL_PACKAGE` |
| `--host` | `$VESSEL_HOST`, else `127.0.0.1` |
| `--port` | `$VESSEL_PORT`, else `8710` |

It requires `VESSEL_SANDBOX_TOKEN` (16 or more characters) and refuses to start without Landlock ABI 6 unless
`VESSEL_UNCONFINED=1`.

## Other entry points

| Command | Use |
|---|---|
| `python conformance/run.py <base-url> <app-key> <agent-id>` | Conformance checks against a running agent; exits non-zero on failure |
| `deploy/verify-isolation.sh` | Isolation probes against the running example deployment |
| `python stress/load.py [--users N] [--turns N] [--history N] [--cancels N]` | Load test with a fake model; `--live URL KEY AGENT` targets a running vessel |
