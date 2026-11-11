# Several deployments on one host

Two deployments can share a Docker host if each has its own project name, `front` network and folder. This page
lists what to change.

## Give each deployment its own project and network

```toml
# second/deployment.toml
[vessel]
project = "second"
front_subnet = "172.31.248.0/24"
front_address = "172.31.248.10"
```

| Setting | Why it must differ |
|---|---|
| `project` | Prefixes containers, networks, volumes and locally built images (`second:local` and `second-egress:local` from a checkout, and `second-sandbox-<agent>:local` for an agent with a sandbox Dockerfile). Lowercase letters, digits, `_` and `-`, starting with a letter or digit. |
| `front_subnet`, `front_address` | Two networks cannot use the same subnet. `front_address` must be inside `front_subnet`. |

## Keep each deployment in its own folder

`deploy` writes `compose.generated.yml` and the `.generated/` secrets folder beside the deployment file (or beside
`--out`). If the Compose file there already belongs to another project, `deploy` stops:

```text
.../compose.generated.yml belongs to project 'astralvessel'; give each deployment its own folder, or --out
```

## Reach the second deployment

Applications attach to `second_front` and call `http://172.31.248.10:8700`. The conformance run from the
[Quickstart](../get-started/quickstart.md) becomes:

```bash
docker run --rm --network second_front -v "$PWD/../conformance:/conformance:ro" second:local \
  python /conformance/run.py http://172.31.248.10:8700 "$VESSEL_APP_KEY" field-guide
```

Docker volumes named with `volume:NAME` mount sources are external and not prefixed, so two deployments can mount
the same data volume read-only.
