# Isolation and probes

This page lists the isolation each container and each tool call gets, and the probes that check it on a running
deployment.

## Containers

Every generated container runs with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, and
limits on memory and processes.

| Container | Limits | Can reach |
|---|---|---|
| sandbox | `memory`, `cpus`, `pids` and `scratch` from its profile; `/tmp` is `noexec,nosuid,nodev` | Only its `tools-<agent>` network, where only the vessel's tool client answers |
| vessel | 1 GB, 256 processes, 64 MB `/tmp` | The egress, search and every sandbox; listens only on `front` |
| egress | 512 MB, 128 processes | The outside network |
| search | 512 MB, 128 processes | The egress only |

Each sandbox holds its own token, derived from the master secret, never the master itself, and no model or app key.
The vessel serves an agent only while its sandbox reports that it is confined and runs the deployed profile.

## Each tool call

`grep`, `bash` and declared command tools run as separate processes. Before each one runs, it is confined:

| Mechanism | Effect |
|---|---|
| Landlock (filesystem) | Read and execute only the system directories (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/opt`), the agent folder, the `workdir`, the mounts, and a few devices (`/dev/null`, `/dev/zero`, `/dev/urandom`, `/dev/random`). Write only the turn's workspace. No `/proc`, so no view of other processes. Socket files cannot be created. |
| Landlock (network) | No TCP bind or connect. |
| Landlock (scoping) | No signals to processes outside the call, no abstract Unix sockets outside it. |
| seccomp | No SysV IPC or POSIX message queues (shared by everything running as one user), no IPv4 or IPv6 sockets. From 0.5.0 also no `setsid` or `setpgid`, so every process stays in the call's session. |
| `no_new_privs` | Set before the rules apply. |
| Environment | Only `PATH`, `LANG`, `HOME`, `TMPDIR` and `VESSEL_OUTBOX`. |
| Time and output | The tool's timeout, then the whole process group is killed; at most 1,000,000 bytes of output. |
| Strays | Processes a call leaves behind are killed. |
| 0.5.0 limits | At most 4,096 open files; no file larger than the turn's disk limit; [disk limits](../build/workspaces-and-files.md#disk-limits) per turn and per user. |

If confinement cannot be applied, the call fails with exit code 126 and does not run. A sandbox whose kernel lacks
Landlock ABI 6 refuses to start.

`read`, `glob` and `list` run in the sandbox's server process instead. They are held to the same readable paths and
check where an opened file or folder really points, so links cannot lead them outside.

## Run the probes

`deploy/verify-isolation.sh` probes a running deployment from inside its containers and prints `PASS` or `FAIL` for
each claim. Run it from the deployment folder after `docker compose ... up -d`:

```bash
./verify-isolation.sh
```

It exits non-zero if any probe fails, and CI runs it against the example deployment on every pull request. It
targets the example's agent ids (`field-guide`, `field-guide-reader`) and data; copy and adapt it for your own
deployment.

| Area | Probes |
|---|---|
| Sandbox network | no route to the internet; cannot resolve or reach the egress; cannot reach the model host directly; cannot reach the vessel's API by name or by its front address; cannot reach another agent's sandbox; cannot reach search |
| Sandbox contents | holds no task or conversation store; holds no model or app key; holds its own token, not the master; root filesystem is read-only; scratch space is `noexec`; data is mounted read-only |
| Tool calls | a background process does not outlive its call; a call cannot read another call's scratch; cannot list processes; cannot open TCP connections; cannot create a socket file; cannot use shared memory; cannot open UDP sockets; `glob` cannot climb out of the agent's data |
| Workspaces | calls in one turn share its workspace; a call cannot read another turn's workspace; a turn's workspace is gone once it ends; the sandbox refuses a malformed workspace key; a turn over its disk limit is stopped and emptied (0.5.0) |
| Files | a file written to the outbox reaches the vessel; the outbox refuses other types, big files and links; an outbox swapped for a link exposes nothing |
| Vessel | keeps no conversation store; has no direct route to the internet; reaches the model through the egress; reaches the search service |
| Egress proxy | refuses the cloud metadata address, private addresses and shared (Tailscale) addresses; refuses odd ports; allows a public site on 443 |
| Search and model gate | search has no direct route to the internet; cannot reach the vessel's API; cannot use the model passthrough; the vessel passes the egress with its token |

That is 41 probes, 42 with 0.5.0.

## Conformance checks

`conformance/run.py` checks an agent server from outside, with real model calls: the agent card, authentication,
event order, the transcript, history handling, refusal of a system message in history, owner-scoped `GetTask` and
`CancelTask`, cancellation, and that no raw exception text appears in any event. See the
[Quickstart](../get-started/quickstart.md#7-run-the-conformance-checks-optional).
