# Quickstart

Four commands take you from nothing to a checked deployment of the example agent and a first answer. Most of the
time goes to pulling the images.

## Before you start

You need a Linux host with kernel 6.12 or newer (Landlock ABI 6), Docker with Compose, and an OpenAI-compatible
model endpoint with an API key.

## 1. Install

=== "uv"

    ```bash
    uv tool install astralvessel
    ```

    uv fetches a suitable Python if the host lacks one, and puts `astralvessel` on your `PATH`.

=== "pipx"

    ```bash
    pipx install astralvessel
    ```

    Needs Python 3.11 or newer.

## 2. Start it

Make an empty folder for the deployment and run `up` there:

```bash
mkdir field-guide && cd field-guide
astralvessel up --example
```

`up` finds no `deployment.toml`, so it asks three questions:

```text
Model endpoint (OpenAI-compatible base URL, such as https://api.example.com/v1):
API key (or env:NAME to read it from the environment; input is hidden):
Model name:
```

Type `env:NAME` instead of the key to keep it out of every file. The variable must then be set whenever you run
`up`, `check`, `chat`, `ask` or `down`. Without `--example`, `up` also asks for your agent folders and for any data
folder each one mounts.

Then it:

1. writes `deployment.toml`, a private `.env` (mode 600) with the key and generated secrets, and a `.gitignore`;
2. copies the example agent into `agents/` and its sample data into `data/`;
3. writes the Compose file and starts the containers;
4. waits for every agent's sandbox to report that it is confined and running the deployed profile;
5. runs the [isolation probes](../security/isolation.md) against every agent;
6. sends one short request to your model through the egress.

It reports success only if every step passes:

```text
PASS  sandbox-field-guide: has no route to the internet
...
PASS  model your-model-name answers through the egress
42 probes passed, 0 failed, 0 skipped

ready: field-guide
  astralvessel chat field-guide
  astralvessel ask field-guide "your question"
```

A wrong key fails the model check, and a sandbox that never becomes ready fails with its logs. The containers keep
running either way, so you can look. Fix the problem and run `astralvessel up` again.

## 3. Chat

```bash
astralvessel chat field-guide
```

Answers stream as they are written, tool calls show as `[name]`, and files a tool returns are saved under `chats/`.
Ctrl-C stops an answer; `/exit` or Ctrl-D leaves. Each chat is saved in `chats/chats.sqlite`:

```bash
astralvessel chat field-guide --list
astralvessel chat field-guide --resume 3f9c...
```

Saving chats is the client's job, not the vessel's: the vessel keeps no conversation history. `chat` is one
application of the [history store](../reference/python.md). Your application can use it the same way, or keep
history its own way.

## 4. Ask once

```bash
astralvessel ask field-guide "Which birds in the guide hunt fish?"
```

One turn, no history. It exits non-zero unless the turn finishes `ok`.

## Stop it

```bash
astralvessel down
```

Add `--volumes` to also delete the vessel's task store. `astralvessel up` starts it again with the same settings.

## Next steps

- [How it works](how-it-works.md): the containers and the path of one turn.
- [The agent folder](../build/agent-folder.md): write your own agent, then run `astralvessel up` in a new folder.
- [Connecting an application](../run/applications.md): call agents from your own service.
- [Manual deployment](../run/manual-deployment.md): each step `up` runs, done by hand.
