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

Applications attach to `second_front` and call `http://vessel:8700`. An application attached to more than one
deployment's `front` network should use the container name instead, such as `http://second-vessel-1:8700`, because
`vessel` would match both. `astralvessel chat` and `ask` attach to the right network themselves.

`astralvessel up` picks a free project name and front subnet when it writes a deployment, so deployments it creates
in separate folders never collide.

Docker volumes named with `volume:NAME` mount sources are external and not prefixed, so two deployments can mount
the same data volume read-only.
