# Troubleshooting

Symptoms, their likely cause, and the fix. Most problems show up in the vessel's or a sandbox's log:

```bash
docker compose -f compose.generated.yml --env-file .env logs vessel
docker compose -f compose.generated.yml --env-file .env logs sandbox-<agent>
```

## Deploying

| Symptom | Cause and fix |
|---|---|
| `set VESSEL_SANDBOX_TOKEN (16+ characters) in the environment or the env file` | Add a master secret of 16 or more characters to `.env` beside the deployment, or pass `--env-file`. |
| `agent <id> needs mounts the deployment does not grant` / `is granted mounts it does not declare` | The targets in `[agents.sandbox] mounts` must match the agent's `[sandbox] mounts` exactly. |
| `writable mounts (...) are not supported yet` | Set `read_only = true` (the default) on both sides; write to the turn's workspace instead. |
| `services must match its tools; missing ..., unused ...` | Grant exactly the services the agent's service tools name under `[agents.services.<name>]`. |
| `declared tool names collide` | Rename a declared tool that repeats another one or a built-in tool. |
| `unknown builtin tools` | Use only `read`, `grep`, `glob`, `list`, `bash`, `webfetch`, `websearch`. |
| `Extra inputs are not permitted` | A misspelled or unsupported key; check [Configuration tables](reference/configuration.md). |
| `issuer <id>: jwks_file ... is not a file` | The issuer's key file is missing; the path is relative to the deployment file. |
| `Field required` for `egress.upstream_url` | Set `[egress] upstream_url` to your model endpoint; there is no default. |
| `... belongs to project '...'; give each deployment its own folder, or --out` | Two deployments share a folder. See [Several deployments on one host](run/several-deployments.md). |
| `docker compose` says `set NAME in the environment or .env` | An `env:NAME` value has no value; add it to `.env`. |

## Starting

| Symptom | Cause and fix |
|---|---|
| A sandbox exits with `this kernel lacks Landlock ABI 6` | The host kernel is too old or has Landlock disabled. See [Platform support](security/platforms.md). |
| The vessel exits with `key_sha256 must be a sha256 hex digest` | `key_sha256` (or its `env:` value) must be 64 hex characters, the SHA-256 of the key, not the key. |
| The vessel logs `agent <id> sandbox is NOT ready`, and calls get 503 | The sandbox is down, unconfined, or running an old profile. Run `astralvessel deploy`, then `docker compose ... up -d`, and check the sandbox's log. |
| `bash failed: No such file or directory` on every call | The agent's `workdir` does not exist in the sandbox. Set it to a mount target. |
| Every confined call says `the sandbox could not confine this call` | Landlock or seccomp could not be applied, for example on an unsupported architecture. |

## Calling

| Symptom | Cause and fix |
|---|---|
| 401 `authentication required` | Wrong key, a key whose SHA-256 is not in the deployment, a missing or invalid `X-Vessel-User`, or a token that failed verification. |
| 403 `this app may not call this agent` | Add the agent to the app's or issuer's `agents`. |
| 503 `this agent's sandbox is not ready` | See "NOT ready" above. |
| 413 `request too large` | The body is over 16,000,000 bytes; send less history or smaller images. |
| Turn rejected with `invalid request: ...` | The history or an image failed a check; the message names it. See [Transcript format](reference/transcript.md). |
| Turn rejected with `this agent does not accept images` | Set `[model] images = true` if the model accepts images, or drop them. |
| No transcript in the response | Add `application/vnd.astralvessel.transcript+json` to `acceptedOutputModes`. |
| No files in the response | List their media types in `acceptedOutputModes` (or list none), and the type in the agent's `[output] files`. |
| Cannot reach the vessel from the host | It publishes no ports. Call it from a container on the `<project>_front` network. |

## During a turn

| Symptom | Cause and fix |
|---|---|
| `turn.finished` with `model_error` and `model endpoint returned HTTP 4xx` | The endpoint refused the request: check the model name, `upstream_url` and key. The egress and vessel logs have the response detail. |
| `the egress token is missing or wrong` in logs | Run `astralvessel deploy` again so the vessel and egress get matching tokens, then `up -d`. |
| `deadline` status | The turn ran past `deadline_seconds`; raise it or lower `max_steps`. |
| `step_limit` status | The model used all its steps; raise `max_steps` or improve the briefing. |
| A tool result `[timed out after N s]` | Raise `tool_timeout_seconds` or the tool's `timeout_seconds`. |
| `webfetch` returns `blocked or unreachable` | The proxy refused a non-public address or a port other than 80 or 443, or the site is down. |
| Searches fail with engine errors | Engines are rate limiting or blocking; SearXNG suspends a CAPTCHA engine for 10 minutes. With `duckduckgo`, switch to `searxng`. |
| `[stopped: the files went over ... disk limit ...]` | The turn or user went over `turn_disk` or `user_disk`. Raise them, or have the agent delete files it no longer needs. |
