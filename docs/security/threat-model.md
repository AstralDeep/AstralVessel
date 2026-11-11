# Threat model

This page says what AstralVessel assumes an attacker can do, what it protects against that, and what it leaves out
of scope. Read it before giving an agent data or tools.

## The assumption

The model, and everything the model reads (web pages, data files, tool output, service answers), may be hostile. A
prompt injection can make the agent try anything its tools allow, as often as its step limit allows. AstralVessel
is designed so that the worst a hijacked agent can do is bounded by what the deployment grants it.

## What it protects

| Asset | Protection |
|---|---|
| The model key | Held only by the egress. The vessel holds a derived token that only the egress accepts; sandboxes hold nothing. |
| App keys | The vessel holds only their SHA-256 digests. |
| Conversation history | Kept by the application. The vessel's task store keeps the user's text, status and the answer, never the history sent in, the transcript or files. |
| Agent data | Mounted read-only. Writable mounts are not supported. |
| Other users of the same agent | Each tool call is confined to its own turn's workspace and cannot see, signal or talk to other calls. See [Isolation and probes](isolation.md). |
| Other users' tasks | Every task is bound to its owner; others cannot read or cancel it. |
| Other agents | Each app or issuer reaches only the agents it is granted. Each agent has its own sandbox and network. |
| The host network and cloud metadata | Agent-driven web traffic reaches only globally routable addresses on ports 80 and 443. |
| Service credentials | Held by the vessel, added to service calls, and redacted from service answers before the model sees them. |
| Internal errors | The model and callers get generic messages; raw exception text, which can carry connection strings or paths, goes only to the operator's logs. |
| The system prompt | Fixed by the operator; history containing a system message is refused. |

## Out of scope

- **Kernel and container escapes.** Isolation rests on the host kernel's Landlock and seccomp and on Docker.
- **What the agent says.** An agent can pass anything it may read to its user.
- **Data leaving through web tools.** With `webfetch` or `websearch`, an agent can put anything it may read into a
  URL or query sent to any public site. Give web tools only to agents whose data may leave.
- **Data leaving through service tools.** The model controls the request body your service receives; your service
  must treat it as untrusted and decide what the owner may do.
- **Denial of service by a granted caller.** Limits on steps, time, output, memory, processes and disk bound one
  turn, but a caller with a valid key can start many turns.
- **The model provider and search engines.** They see what is sent to them.

See [What it trusts](trust.md) for the parties AstralVessel relies on.
