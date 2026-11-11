# Identity, apps and issuers

Every request to an agent names a caller and an end user, and every task belongs to that owner. This page covers
the two ways to authenticate, how owners are formed, and how a caller can narrow the tools a turn may use.

## App keys

An application holds a random key; the deployment holds only its SHA-256.

```toml
[[apps]]
name = "web"
key_sha256 = "env:WEB_APP_KEY_SHA256"
agents = ["field-guide", "field-guide-reader"]
```

Make a key and its digest:

```bash
python3 -c 'import hashlib, secrets; k = secrets.token_urlsafe(32); print(k); print(hashlib.sha256(k.encode()).hexdigest())'
```

Each request sends the key and names the end user:

```http
Authorization: Bearer <app key>
X-Vessel-User: alice
```

`X-Vessel-User` is required with an app key: 1 to 128 characters from letters, digits and `. _ : @ -`. The owner is
`<app name>/<user>`, for example `web/alice`. The vessel trusts the application to name its users honestly.

## Token issuers

An orchestrator with an identity provider can send each user's signed token instead of an app key and user header.

```toml
[[issuers]]
id = "astral"
issuer = "https://iam.example.edu/realms/Astral"
audience = "research-vessel"
jwks_url = "https://iam.example.edu/realms/Astral/protocol/openid-connect/certs"
authorized_parties = ["astral-orchestrator"]
agents = ["field-guide"]
```

| Key | Default | Meaning |
|---|---|---|
| `id` | required | Lowercase name; the owner is `<id>|<sub>`, so two issuers never name the same owner. |
| `issuer` | required | Must equal the token's `iss`. |
| `audience` | required | Must be in the token's `aud`. |
| `jwks_url` | | The issuer's signing keys, `https://` only, fetched through the egress proxy. |
| `jwks_file` | | A JWKS file, relative to the deployment file. Set exactly one of `jwks_url` or `jwks_file`. |
| `authorized_parties` | `[]` | If set, the token's `azp` must be one of these. |
| `algorithms` | `["RS256", "ES256", "EdDSA"]` | Allowed: `RS256`, `RS384`, `RS512`, `PS256`, `ES256`, `ES384`, `EdDSA`. |
| `agents` | required | The agent ids this issuer's users may call. |

A bearer value is treated as a token when the deployment has issuers and the value looks like a JWT (three
dot-separated parts starting `ey`); otherwise it is checked as an app key. A token passes only if:

- its `iss` names a configured issuer, and its header `alg` is in that issuer's `algorithms`;
- it is signed by one of the issuer's signing keys (keys marked for encryption are ignored);
- it carries `exp`, `iat`, `iss`, `aud` and `sub`, is unexpired (60 seconds of leeway), and its audience matches;
- `sub` is 1 to 200 printable ASCII characters without spaces;
- `azp` matches, when `authorized_parties` is set.

Shared-secret (`HS256`) and unsigned tokens are always refused. When a token names a key the vessel does not have,
it refetches the key set, at most once a minute; if a fetch fails or returns no usable keys, it keeps the keys it had.

!!! warning "jwks_file in the generated Compose file"
    The generated Compose file mounts only `deployment.toml` into the vessel, not files beside it, so a `jwks_file`
    is not readable there and tokens from that issuer are refused. Use `jwks_url` with the generated deployment.

## What a caller may reach

| Request | App key | Token |
|---|---|---|
| `GET /health` | not needed | not needed |
| `GET /agents/<id>/.well-known/agent-card.json` | not needed | not needed |
| `GET /v1/agents` | lists the app's agents | lists the issuer's agents |
| `POST /agents/<id>`, `GET /agents/<id>/manifest` | only the app's agents | only the issuer's agents |

A missing or wrong credential gets 401; an agent the caller is not granted gets 403. See
[HTTP and A2A protocol](../reference/protocol.md#errors).

## Tasks belong to their owner

Every task is bound to the owner who started it. Another owner, even another user of the same app, cannot read it
with `GetTask` or cancel it with `CancelTask`. Service tools receive the owner in `X-Vessel-Owner`.

## Narrowing tools per call

A caller can remove tools from one turn by permission scope. It can never add tools the agent lacks.

| Caller | How | Effect |
|---|---|---|
| App | `X-Vessel-Scopes: tools:read tools:search` | Only tools with a listed scope exist for this turn |
| App | `X-Vessel-Scopes` present but empty, or with no `tools:` entries | No tools |
| App | No `X-Vessel-Scopes` header | All of the agent's tools |
| Token | `scope` (space-separated) or `scp` (string or list) with `tools:` entries | Only tools with a listed scope |
| Token | No `tools:` entries in its scopes | All of the agent's tools |

Entries that start with `tools:` but are not one of the six known scopes count as narrowing to nothing, so a typo
leaves fewer tools, never more. The scopes are listed in [vessel.toml](../build/vessel-toml.md#permission-scopes).
