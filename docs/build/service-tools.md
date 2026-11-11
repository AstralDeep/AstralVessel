# Service tools

A service tool is answered by your application over HTTP instead of by the sandbox. The vessel posts the model's
arguments to an endpoint the deployment grants, with credentials the deployment holds, so the sandbox never needs
network access or a secret.

## Declare the tool

```toml
# tools/lookup.toml
name = "lookup"
description = "Look up a record by its id."
service = "records"
path = "/agent-tools/lookup"
scope = "tools:read"

[parameters]
type = "object"
required = ["id"]
[parameters.properties.id]
type = "string"
```

```toml
# vessel.toml
[tools]
declared = ["tools/lookup.toml"]
```

| Key | Meaning |
|---|---|
| `service` | A service name: lowercase letters, digits and hyphens, starting with a letter. |
| `path` | Appended to the granted URL. Must start with `/` and may not contain a `..` segment. |
| `timeout_seconds` | Optional; defaults to the agent's `tool_timeout_seconds`. |

`name`, `description`, `parameters` and `scope` work as for [declared tools](declared-tools.md). `input` has no
effect on a service tool.

## Grant the service

```toml
# deployment.toml
[[agents]]
id = "records-agent"
package = "agents/records"
[agents.services.records]
url = "http://app:8000"
headers = { "X-Internal-Token" = "env:RECORDS_TOKEN" }
```

The services an agent's tools name and the services the deployment grants must match exactly; `deploy` refuses a
missing or an unused grant.

| Key | Meaning |
|---|---|
| `url` | `http://` or `https://`, with no query or fragment, or `env:NAME`. |
| `headers` | Sent with every call. Values may be `env:NAME`. Names starting with `X-Vessel-` are refused. |

`env:` values in `url` and `headers` are passed into the vessel's environment by the generated Compose file and
resolved when the vessel starts.

## The request your application receives

```http
POST /agent-tools/lookup HTTP/1.1
Host: app:8000
Content-Type: application/json
X-Internal-Token: <from RECORDS_TOKEN>
X-Vessel-Owner: web/alice
X-Vessel-Agent: records-agent
X-Vessel-Tool: lookup

{"id": "A-1042"}
```

| Header | Value |
|---|---|
| `X-Vessel-Owner` | The turn's owner: `<app>/<user>` for an app caller, `<issuer>|<subject>` for a token caller |
| `X-Vessel-Agent` | The agent id |
| `X-Vessel-Tool` | The tool name |

The body is the model's arguments, as a JSON object. Use the owner header to decide what this user may see.

## What the model sees

| Your response | The model gets |
|---|---|
| 2xx | The body, as an ordinary result. A JSON body is re-indented. |
| 4xx | The body, marked as an error, so you can explain a bad request to the model. |
| 3xx or 5xx | `<tool> failed (HTTP <status>)`, marked as an error. Redirects are not followed. |
| No answer in time | `<tool>: the service did not answer within N s` |
| Unreachable | `<tool>: the service is unreachable` |
| Body over 1,000,000 bytes | `<tool>: the service's answer was too large` |

Any granted header value of eight or more characters that appears in the response is replaced with `[redacted]`
before the model sees it. An empty body becomes `[HTTP <status>, empty]`.

## Reaching your application

The vessel calls services from its own container, on the networks it is attached to. Put your application on the
deployment's `front` network so the vessel can reach it; see [Connecting an application](../run/applications.md).
