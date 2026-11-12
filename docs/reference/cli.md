# CLI

You run `up`, `check`, `chat`, `ask` and `down`, or `deploy` for a [manual deployment](../run/manual-deployment.md).
The containers run `serve`, `sandbox` and `client`.

```text
usage: astralvessel [-h] {up,check,chat,ask,down,client,deploy,serve,sandbox} ...
```

Every command that takes a deployment uses `deployment.toml` in the current folder unless you name one, and reads
`.env` beside it, filling only variables not already set in the environment.

## astralvessel up

Starts a deployment and checks it. With no deployment file, it first writes one.

```text
astralvessel up [deployment] [--example] [--agent FOLDER] [--mount AGENT:/TARGET=FOLDER]
                [--model-url URL] [--api-key env:NAME] [--model NAME] [--timeout SECONDS]
```

| Argument | Meaning |
|---|---|
| `deployment` | The deployment file. If you name one, it must exist. |
| `--example` | Set up the example field guide agent, copied into `agents/` with its data in `data/`. |
| `--agent` | An agent folder; repeat for several. |
| `--mount` | A host folder for a mount an agent declares, such as `my-agent:/data=/srv/data`. |
| `--model-url` | The provider's OpenAI-compatible base URL. |
| `--api-key` | `env:NAME` only, so a key never appears in a command line. Leave it out to type the key at the prompt. |
| `--model` | The model every agent uses, written as each agent's `model`. |
| `--timeout` | Seconds to wait for the sandboxes; 300 by default. |

Setup options apply only when there is no deployment file yet; with one, edit the file instead.

Writing a deployment:

1. Asks for anything the options did not give: agent folders, mount folders, model URL, key and model name. With no
   terminal attached it never asks; it stops and names the missing options.
2. Picks a project name from the folder's name (adding a number if another folder's deployment already uses it) and
   a free `172.31.x.0/24` front subnet.
3. Writes `.env` (mode 600) with a generated app key, its SHA-256 and the sandbox master secret, plus the model key
   unless you gave `env:NAME`. It refuses to overwrite an existing `.env`.
4. Writes `deployment.toml`, with one app, `cli`, allowed to call every agent, and a `.gitignore`.

Starting:

1. Writes the Compose file, as `deploy` does, and runs `docker compose up -d --build --remove-orphans`.
2. Waits until every agent's sandbox reports it is confined and running the deployed profile. A container that
   exits, or an agent still not ready at the timeout, stops it with that container's logs.
3. Runs every isolation probe, as `check` does.
4. Sends each model one short chat request through the egress, as the vessel does.

It exits non-zero if any step fails, and leaves the containers running so you can look.

## astralvessel check

```text
astralvessel check [deployment]
```

Runs the [isolation probes](../security/isolation.md) against a running deployment: every agent's sandbox and calls,
the vessel, the egress proxy and search. Prints `PASS`, `FAIL` or `SKIP` for each and exits non-zero if any fails.

## astralvessel chat

```text
astralvessel chat AGENT [--resume ID] [--list] [--deployment FILE]
```

A conversation with one agent in the terminal. Each turn sends the chat's history and stores the transcript that
comes back in `chats/chats.sqlite`, through the [history store](python.md). Files a tool returns are saved in
`chats/files/<chat id>/`. Ctrl-C stops the current answer, on the vessel too; `/exit` or Ctrl-D leaves.

| Option | Meaning |
|---|---|
| `--resume ID` | Continue a saved chat. |
| `--list` | List saved chats, newest first. |

## astralvessel ask

```text
astralvessel ask AGENT QUESTION [--deployment FILE]
```

One turn with no history; files go to `chats/files/ask/`. Exits non-zero unless the turn finishes `ok`.

`chat` and `ask` run the client in a short-lived container on the deployment's internal `front` network, because the
vessel publishes no ports. The container uses the vessel's own image, runs as your user with a read-only root, no
capabilities and at most 64 processes, gets `VESSEL_APP_KEY` from the environment, and mounts only `chats/`.

## astralvessel down

```text
astralvessel down [deployment] [--volumes]
```

Runs `docker compose down`. `--volumes` also deletes the vessel's task store.

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

## astralvessel client

```text
astralvessel client {chat,ask} AGENT [QUESTION] [--resume ID] [--list]
```

The client `chat` and `ask` start inside their container. It calls `http://vessel:8700` with `VESSEL_APP_KEY` and
keeps chats in `$VESSEL_CHATS` (default `/chats`).

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
| `python stress/load.py [--users N] [--turns N] [--history N] [--cancels N]` | Load test with a fake model; `--live URL KEY AGENT` targets a running vessel |
