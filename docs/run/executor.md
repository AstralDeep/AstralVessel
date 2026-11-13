# The executor

The executor runs a tool only after it has verified a ticket. It sits between the node and the sandboxes and listens only on the `front` network, so no sandbox can reach it.

## The interface

All routes except `/health` need `Authorization: Bearer <executor token>`. The token is derived from `VESSEL_SANDBOX_TOKEN` and written to `.generated/executor.env`.

| Route | Does |
|---|---|
| `GET /health` | Reports each sandbox's confinement and the executor's mesh state. |
| `GET /tools` | Lists each agent's tools with schema, scope and definition hash. |
| `GET /pairing` | Offers the executor's pairing code and payload to the owner. |
| `POST /run` | Verifies a call and runs it. |
| `POST /end` | Ends a turn and removes its workspace. |

`POST /run` takes `agent`, `tool`, `arguments_json`, `action_json`, `ticket_json`, `workspace` and `owner`.

## What it checks, in order

1. The tool exists for that agent. Otherwise `executor.tool_unknown`.
2. The action names the same tool and the digest of exactly these arguments. Otherwise `request.call_mismatch`.
3. The action's `tool_def` equals the executor's own definition hash. Otherwise `gate.tool_def_changed`.
4. When `[owner]` sets a `subject`, the request's `owner` is that owner. Otherwise `executor.owner_mismatch`.
5. The kernel verifies the ticket offline, with the effect kind taken from the tool's scope. Otherwise a `ticket.*` or `gate.*` code.
6. The verified mesh, device and turn equal the action's. Otherwise `ticket.unauthorized`.
7. The ticket's mesh is the executor's own mesh. Otherwise `ticket.wrong_mesh`.
8. This ticket has not run before. Otherwise `ticket.replayed`.

The kernel's action has nine fixed fields and no owner or workspace, so a ticket cannot bind them. The node names them in the request and the node's token vouches for them. The agent is bound by the definition hash. It is the kernel's hash of the tool's name, description, input schema and annotations, with `{"agent": "<id>"}` added to the annotations. A tool published to the mesh with that annotation carries that hash in the ticket.

Every refusal returns HTTP 403 with a dotted `code`, and nothing runs. The replay memory lives in the executor's process and holds the last 8192 tickets.

## Joining the mesh

The executor waits until the node has joined the owner's mesh. Then it opens its own kernel through the node's mailbox and offers pairing. Until the owner approves, `/health` shows `not_enrolled` and every call is refused with `executor.not_enrolled`.

```bash
astralvessel pair --executor
```

The image build fails unless the pinned kernel wheel is present and has the pinned sha256. If the binding still cannot be imported at run time, the executor refuses every ticket and every pairing offer with `executor.no_kernel`. That code is never used for a transport failure, which Deep reports as `executor.unavailable`.

## Known limits

- The executor syncs through the node's mailbox. It needs a route to it on `front`. It has no relay, so it reaches the mailbox only by its direct addresses.
- The replay memory resets when the executor restarts. A ticket's own expiry still applies.
- The node-side adapter that sends calls to `POST /run` is not part of AstralVessel.
