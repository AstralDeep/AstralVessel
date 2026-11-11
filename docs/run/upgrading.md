# Upgrading and releases

How you upgrade depends on whether `astralvessel` came from PyPI or from a checkout, because that decides which
images `deploy` writes into the Compose file.

## Which images deploy uses

| Installed from | Images in the Compose file |
|---|---|
| PyPI (`pip install astralvessel`) | The released `ghcr.io/astraldeep/astralvessel:<version>` and `ghcr.io/astraldeep/astralvessel-egress:<version>`, where `<version>` is the installed package's version |
| A checkout (`pip install -e .`) | Built locally from the checkout, tagged `<project>:local` and `<project>-egress:local` |

`deploy` uses the checkout's Dockerfile when it finds one beside the package, so an editable install always builds.
An agent with a [sandbox Dockerfile](../build/vessel-toml.md#extra-software-in-the-sandbox) is always built locally,
`FROM` whichever AstralVessel image the deployment uses.

## Upgrade an installed copy

```bash
pip install --upgrade astralvessel
astralvessel deploy deployment.toml
docker compose -f compose.generated.yml --env-file .env up -d
```

The new Compose file names the new image tags, so `up -d` pulls and recreates the containers.

## Upgrade a checkout

```bash
git pull --ff-only
astralvessel deploy deployment.toml
docker compose -f compose.generated.yml --env-file .env up -d --build
```

Run `deploy` after every upgrade: fingerprints and generated settings can change between versions, and a sandbox
whose fingerprint does not match is not served.

## What survives an upgrade

| Kept | Where |
|---|---|
| Task records | The `<project>_vessel_data` volume. Tasks that were running when the vessel stopped are marked failed on restart. |
| Per-container secrets | Regenerated from `VESSEL_SANDBOX_TOKEN` on each `deploy`; the SearXNG secret is kept. |
| Conversations | In your application; the vessel holds none. |

## How releases are made

Releases come from a version tag on `main`:

1. Set `version` (plain `X.Y.Z`) in `pyproject.toml`.
2. Run the Release workflow by hand. That is a dry run: it builds both images for amd64 and arm64 and the package,
   and pushes nothing.
3. Push the tag `v<version>`. The workflow checks the tag matches the version, pushes both images to GHCR tagged
   with the version and `latest`, waits for a maintainer to approve the `pypi` environment, checks the images can
   be pulled without signing in, and publishes the package to PyPI through trusted publishing.

No release has been tagged yet; until one is, install from a checkout.
