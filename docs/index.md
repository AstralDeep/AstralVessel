# AstralVessel

AstralVessel is a small, standalone agent harness. You describe agents as folders, list them in one deployment
file, and `astralvessel deploy` writes a Docker Compose file that runs them. Applications call each agent over
[A2A 1.0](https://a2a-protocol.org/) (JSON-RPC over HTTP), and every tool call the model makes runs in a sandbox
container confined with Landlock and seccomp.

## What you get

- **Agents as folders.** An agent is a manifest (`vessel.toml`), a briefing (`prompt.md`) and optional tools, each a
  small TOML file. See [The agent folder](build/agent-folder.md).
- **One deployment file.** `deployment.toml` lists the agents a vessel serves, grants each one its data, services
  and sandbox limits, and says which applications may call which agents. See
  [deployment.toml](run/deployment-toml.md).
- **Any OpenAI-compatible model.** The model key lives only in the egress container, never in the vessel or a
  sandbox. See [Models and egress](run/models-and-egress.md).
- **A sandbox per agent.** No network, no secrets, a read-only root and read-only data; each call is confined so
  users sharing an agent cannot see each other. See [Isolation and probes](security/isolation.md).
- **Application-owned history.** The vessel keeps only runs in flight. Your application sends the conversation
  with each turn and stores the messages that come back. See [Transcript format](reference/transcript.md).
- **Streaming events.** Every model step and tool call streams as an A2A status update. See
  [Events](reference/events.md).

## Where to start

| If you want to | Read |
|---|---|
| Run the bundled example end to end | [Quickstart](get-started/quickstart.md) |
| Understand the containers and one turn's path | [How it works](get-started/how-it-works.md) |
| Write your own agent | [The agent folder](build/agent-folder.md), then [vessel.toml](build/vessel-toml.md) |
| Call an agent from your application | [Connecting an application](run/applications.md) and [Python client](reference/python.md) |
| Decide whether it is safe for your data | [Threat model](security/threat-model.md) |
| Look up a setting | [Configuration tables](reference/configuration.md) |

## Requirements

- Linux 6.12 or newer with Landlock ABI 6, on x86_64 or arm64. Sandboxes refuse to start without it. macOS and
  Windows through Docker Desktop are **unverified**; see [Platform support](security/platforms.md).
- Docker with Compose.
- Python 3.11 or newer, for the `astralvessel` command.
- An OpenAI-compatible chat completions endpoint and its key.

## Relationship to Astral

AstralVessel speaks the same protocols as [Astral](https://github.com/AstralDeep): A2A for conversations, the
OpenAI-compatible API for models, and [AstralPrimitives](build/rich-output.md) for rich output. Astral can call an
AstralVessel agent as it calls any external A2A agent. AstralVessel is a separate implementation and does not need
an Astral instance.

AstralVessel is licensed under Apache 2.0.
