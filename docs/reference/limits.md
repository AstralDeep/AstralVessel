# Limits

Fixed limits in this version, in one place. Limits you can configure are in the
[Configuration tables](configuration.md).

## Requests

| Limit | Value |
|---|---|
| Request body | 16,000,000 bytes |
| User message | 100,000 characters |
| History | 2,000 messages, 32 images |
| Attached images | 8 per turn, 5 MB each; PNG, JPEG, GIF, WebP |
| `X-Vessel-User` | 1 to 128 characters: letters, digits, `. _ : @ -` |
| Conversation id (`contextId`) | 1 to 128 characters: letters, digits, `. _ : -` |
| Token `sub` | 1 to 200 printable ASCII characters, no spaces |
| Token clock leeway | 60 seconds |

## Tools

| Limit | Value |
|---|---|
| Process output kept | 1,000,000 bytes |
| Output in `tool.finished` events | 4,000 characters |
| `read` | 400 lines by default, 2,000 at most; lines cut at 4,000 characters |
| `grep` | 100 results by default, 1,000 at most; 400 columns per line |
| `glob` | 200 results by default, 2,000 at most |
| `list` | 500 entries |
| `webfetch` | 4 MB, 30 seconds, 5 redirects |
| `websearch` | 8 results by default, 20 at most |
| Service answer | 1,000,000 bytes |
| Open files per call (0.5.0) | 4,096 |

## Files and components

| Limit | Value |
|---|---|
| File size | 4 MB |
| Files per call | 4 |
| Files per turn | 8, 16 MB in total |
| File name | 100 characters |
| Components per answer | 12, 200,000 bytes of JSON |

## Model and timing

| Limit | Value |
|---|---|
| Model retries | 3, after 1, 2 and 4 seconds, on 429, 500, 502, 503, 504 or a failed connection |
| Model read timeout | 180 seconds without data |
| Streamed text merging | One event per 50 ms or 512 characters |
| Wait for a cancelled turn to finish | 15 seconds |
| Sandbox readiness checks | Every 5 seconds until ready, then every 60 |
| Task store sweep | Hourly |
| Issuer key refresh | At most once a minute, when a token names an unknown key |

## Containers

| Container | Memory | Processes | `/tmp` |
|---|---|---|---|
| vessel | 1 GB | 256 | 64 MB |
| egress | 512 MB | 128 | read-only root, no `/tmp` |
| search | 512 MB | 128 | 32 MB |
| sandbox | `memory` (2 GB) | `pids` (512) | `scratch` (256 MB) |

Egress proxy tunnels close after 16 MB or 60 seconds of silence.
