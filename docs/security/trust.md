# What it trusts

Isolation holds only as long as the parties below behave. This page lists them and what each one could do if it did
not.

| Trusted party | Trusted to | If it misbehaves |
|---|---|---|
| The operator | Write `deployment.toml`, hold the secrets, choose what each agent may reach | Can grant anything; AstralVessel does not second-guess the deployment file |
| The host kernel | Enforce Landlock, seccomp and namespaces | A kernel bug can break every isolation claim |
| Docker and Compose | Enforce networks, read-only mounts and resource limits | Same as the kernel |
| The application | Name its users honestly in `X-Vessel-User` | Can act as any of its users on the agents it is granted, and see their tasks |
| Token issuers | Sign only tokens for real users, with honest scopes | Can act as any subject on the agents they are granted |
| Service endpoints you grant | Decide what each owner may do with the arguments the model sends | Can return text that steers the model, like any tool output |
| The model provider | Handle prompts and data it receives | Sees every prompt, all history sent, and all tool output |
| Search engines and websites | Nothing beyond answering | See queries and fetches; their content is fenced as untrusted |

## Secrets and where they live

| Secret | Held by | Never reaches |
|---|---|---|
| Model key (`VESSEL_UPSTREAM_MODEL_KEY`) | egress | vessel, sandboxes, search |
| Egress token (derived) | vessel, egress | sandboxes, search |
| Sandbox master (`VESSEL_SANDBOX_TOKEN`) | vessel, `deploy` | sandboxes, egress |
| Per-sandbox token (derived) | vessel, that sandbox | other sandboxes |
| App keys | your application | the vessel holds only digests |
| Service credentials | vessel | sandboxes, the model (redacted from answers) |
| SearXNG secret | search | everything else |

Generated per-container secrets are written to `.generated/` beside the Compose file with mode 600, in a folder of
mode 700 that carries its own `.gitignore`. Never commit `.env`, `compose.generated.yml` or `.generated/`.

## Development mode is not a sandbox

`VESSEL_DEV_UNSANDBOXED=1` makes the vessel run tools in its own process, unconfined, and `VESSEL_UNCONFINED=1`
lets a sandbox start without Landlock. Both exist for tests. Never point a real model at either.
