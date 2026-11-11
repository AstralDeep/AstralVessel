# deployment.toml

`deployment.toml` is the operator's file. It lists the agents one vessel serves, grants each one its data, services
and sandbox limits, and says who may call which agent. It is the only place access is granted. The full field list
is generated from the code in [Configuration tables](../reference/configuration.md#deploymenttoml).

## A complete example

```toml
[vessel]
project = "astralvessel"
public_url = "http://vessel:8700"
front_subnet = "172.31.247.0/24"
front_address = "172.31.247.10"
task_ttl_hours = 24

[egress]
upstream_url = "env:VESSEL_UPSTREAM_MODEL_URL"
upstream_key = "env:VESSEL_UPSTREAM_MODEL_KEY"

[search]
provider = "searxng"

[[agents]]
id = "field-guide"
package = "../examples/field-guide"
[agents.sandbox]
memory = "1g"
cpus = 2
pids = 256
scratch = "64m"
mounts = [{ target = "/data", source = "../examples/sample-data" }]

[[apps]]
name = "example"
key_sha256 = "env:VESSEL_APP_KEY_SHA256"
agents = ["field-guide"]
```

Relative paths (`package`, mount sources, `jwks_file`) are relative to the deployment file. Unknown keys are refused.

## [vessel]

| Key | Default | Meaning |
|---|---|---|
| `project` | `"astralvessel"` | The Compose project name. It prefixes containers, networks, volumes and locally built images. See [Several deployments on one host](several-deployments.md). |
| `public_url` | `"http://vessel:8700"` | The base URL written into agent cards and the agent list. Set it to the address your callers use. |
| `front_subnet` | `"172.31.247.0/24"` | The subnet of the internal `front` network. |
| `front_address` | `"172.31.247.10"` | The vessel's fixed address on `front`; it listens only there, on port 8700. |
| `task_ttl_hours` | `24` | Task records not updated for this long are deleted, checked hourly. |

## [egress]

| Key | Default | Meaning |
|---|---|---|
| `upstream_url` | **required** | Your OpenAI-compatible base URL, such as `https://api.example.com/v1`, or `env:NAME`. |
| `upstream_key` | `"env:VESSEL_UPSTREAM_MODEL_KEY"` | The model key, normally as `env:NAME`. |

See [Models and egress](models-and-egress.md).

## [search]

| Key | Default | Meaning |
|---|---|---|
| `provider` | `"searxng"` | `searxng` runs a private SearXNG container; `duckduckgo` scrapes DuckDuckGo's HTML page. See [Search](search.md). |

## [[agents]]

| Key | Default | Meaning |
|---|---|---|
| `id` | required | Lowercase letters, digits and hyphens, starting with a letter. The agent's address is `/agents/<id>`. |
| `package` | required | The agent folder. |
| `prompt_append` | `[]` | Extra briefing files from the folder, appended after `prompt.md`. See [Prompts](../build/prompts.md). |
| `sandbox` | defaults below | The sandbox profile. |
| `services` | `{}` | Service grants, one per service the agent's tools name. See [Service tools](../build/service-tools.md). |

### [agents.sandbox]

| Key | Default | Meaning |
|---|---|---|
| `mounts` | `[]` | `{ target, source, read_only }` grants, one for each mount the agent declares. |
| `memory` | `"2g"` | The container's memory limit. |
| `cpus` | `2` | CPU limit; greater than 0. |
| `pids` | `512` | Process limit; at least 32. |
| `scratch` | `"256m"` | Size of the sandbox's `/tmp`, which holds every turn's workspace. |
| `turn_disk` | a quarter of `scratch` | 0.5.0. Disk one turn may use. See [Disk limits](../build/workspaces-and-files.md#disk-limits). |
| `user_disk` | half of `scratch` | 0.5.0. Disk one user's live turns may use together. |

A mount `source` can be:

| Form | Meaning |
|---|---|
| A relative path | Relative to the deployment file |
| An absolute path | Used as is |
| `env:NAME` | Filled from the environment by Compose |
| `volume:NAME` | An existing Docker volume, for data another stack writes; declared `external` in the Compose file |

Every grant must be read-only and its `target` must match a mount the agent declares, and the agent's declared
mounts must all be granted.

## [[apps]]

| Key | Meaning |
|---|---|
| `name` | Lowercase letters, digits and hyphens, starting with a letter; unique. Appears in task owners as `<name>/<user>`. |
| `key_sha256` | The hex SHA-256 of the app's key, or `env:NAME`. The vessel never holds the key itself. |
| `agents` | The agent ids this app may call. |

## [[issuers]]

Token issuers callers may authenticate with instead of an app key. See
[Identity, apps and issuers](identity.md#token-issuers).

A deployment needs at least one app or issuer.

## env: values

Settings marked as accepting `env:NAME` stay references in the generated Compose file (`${NAME:?...}`), so secrets
are never copied into it. Compose fills them from the environment or from the `--env-file` you pass, and refuses to
start if one is unset.

## Check a file

```bash
python -c 'from astralvessel.deployment import load_deployment; d = load_deployment("deployment.toml"); print(list(d.agents))'
```

This loads every agent folder and checks every grant, as `deploy` does.
