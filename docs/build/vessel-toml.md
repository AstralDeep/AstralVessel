# vessel.toml

`vessel.toml` is an agent's manifest: what it is called, which model it uses, which tools it has, and what its
sandbox needs. Unknown keys are refused. The full field list, with types and constraints, is generated from the code
in [Configuration tables](../reference/configuration.md#vesseltoml).

## A complete example

```toml
[agent]
name = "field-guide"
description = "Answers questions about the birds in a small sample field guide."
version = "0.1.0"
examples = ["Which birds in the guide hunt fish?"]

[model]
model = "your-model-name"
temperature = 0.2
max_steps = 20

[tools]
builtin = ["read", "grep", "glob", "list", "bash", "webfetch", "websearch"]
declared = ["tools/species.toml"]

[sandbox]
workdir = "/data"
mounts = [{ source = "guide", target = "/data", read_only = true }]
deadline_seconds = 300
tool_timeout_seconds = 30

[output]
primitives = true
files = ["png", "csv"]
```

## [agent]

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Lowercase letters, digits and hyphens, starting with a letter, up to 63 characters. Shown as the agent card's skill name. |
| `description` | required | One or two sentences. Becomes the agent card's description. |
| `version` | `"0.1.0"` | The agent card's version. |
| `examples` | `[]` | Example questions, shown on the agent card. |

The agent's A2A address and card name come from its `id` in `deployment.toml`, not from `name`.

## [model]

| Key | Default | Meaning |
|---|---|---|
| `model` | required | The model name sent to your endpoint. |
| `endpoint` | `"env:VESSEL_MODEL_URL"` | The OpenAI-compatible base URL. Leave the default: the generated Compose file points it at the egress, and the vessel has no other route out. |
| `api_key` | `"env:VESSEL_MODEL_KEY"` | Leave the default: the generated Compose file gives the vessel a token for the egress, which adds the real key. |
| `temperature` | `0.2` | Sent with every request. |
| `max_steps` | `40` | Model calls per turn, 1 to 200. When reached, the model is asked once more, without tools, to answer with what it has; the turn ends with status `step_limit`. |
| `max_output_tokens` | `8192` | Sent as `max_tokens`; at least 256. |
| `send_reasoning` | `false` | Whether earlier `reasoning_content` from the history is sent back to the model. |
| `images` | `false` | Whether the model accepts images. Turns that attach images, or whose history holds images, are refused unless this is `true`. |

A value of the form `env:NAME` is read from the vessel's environment at each turn.

## [tools]

| Key | Default | Meaning |
|---|---|---|
| `builtin` | `[]` | Any of `read`, `grep`, `glob`, `list`, `bash`, `webfetch`, `websearch`. See [Built-in tools](builtin-tools.md). |
| `declared` | `[]` | Paths to declared tool files, relative to the agent folder. See [Declared tools](declared-tools.md) and [Service tools](service-tools.md). |

An agent with `[output] primitives = true` also gets the `present` tool. See [Rich output](rich-output.md).

## [sandbox]

| Key | Default | Meaning |
|---|---|---|
| `workdir` | `"/workspace"` | The working directory for tool calls, and the base for relative paths. It must exist in the sandbox: set it to one of your mount targets. |
| `mounts` | `[]` | The data the agent needs, as `{ source, target, read_only }`. `target` is the path inside the sandbox; `source` is a label (the deployment chooses the real source). `read_only` must be `true`. |
| `deadline_seconds` | `600` | The whole turn's time limit, model calls included, 5 to 3600. The turn ends with status `deadline`. |
| `tool_timeout_seconds` | `60` | Each tool call's time limit, 1 to 600. A declared tool may set its own. |
| `tool_output_chars` | `20000` | How much of a tool's output the model sees, 1,000 to 200,000 characters. Longer output is cut with a note saying how much was dropped. |
| `dockerfile` | none | A Dockerfile in the agent folder for a custom sandbox image. |

Every mount needs a matching grant in `deployment.toml`, and every grant a matching mount. Writable mounts are not
supported: each turn writes only to its own [workspace](workspaces-and-files.md).

### Extra software in the sandbox

An agent whose tools need more than the base image (Python 3.11, ripgrep, a POSIX shell) can ship a Dockerfile in
its folder. It must start `FROM astralvessel`; `deploy` points that name at the AstralVessel image it uses.

```dockerfile
# agents/charts/sandbox.Dockerfile
FROM astralvessel
USER root
RUN pip install --no-cache-dir matplotlib==3.9.2
USER vessel
```

```toml
[sandbox]
dockerfile = "sandbox.Dockerfile"
```

The base image runs as the unprivileged `vessel` user; switch back to it after installing. The Dockerfile must be
inside the agent folder. Its contents, and every file its `COPY` and `ADD` lines bring in from the folder, are part
of the agent's profile fingerprint, so editing them marks the sandbox stale until you run `deploy` and
`docker compose ... up -d --build`.

## [output]

| Key | Default | Meaning |
|---|---|---|
| `primitives` | `false` | Adds the `present` tool, so answers can carry AstralPrimitives components. See [Rich output](rich-output.md). |
| `files` | `[]` | File types tools may return: any of `png`, `svg`, `csv`, `pdf`. See [Workspaces and files](workspaces-and-files.md). |

## Permission scopes

Each tool carries one of six permission scopes, weakest first: `tools:read`, `tools:search`, `tools:files`,
`tools:write`, `tools:system`, `tools:execute`. The agent card advertises the strongest scope among the agent's
tools as a `scope:` tag, and callers can narrow a turn to some scopes (see
[Identity, apps and issuers](../run/identity.md#narrowing-tools-per-call)).

| Tool | Scope |
|---|---|
| `read`, `glob`, `list` | `tools:files` |
| `grep`, `webfetch`, `websearch` | `tools:search` |
| `bash` | `tools:execute` |
| `present` | `tools:read` |
| Declared tools | their `scope`, default `tools:read` |
