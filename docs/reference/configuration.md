# Configuration tables

Every field of `vessel.toml`, declared tool files and `deployment.toml`, with its type, default and constraints.
These tables are generated from AstralVessel's pydantic models when the site is built. Unknown keys are refused in
every table. For what each setting does, see [vessel.toml](../build/vessel-toml.md) and
[deployment.toml](../run/deployment-toml.md).

## vessel.toml

### Manifest

The top level of `vessel.toml`.

<!-- config: astralvessel.manifest.Manifest -->

### AgentSection

`[agent]`

<!-- config: astralvessel.manifest.AgentSection -->

### ModelSection

`[model]`

<!-- config: astralvessel.manifest.ModelSection -->

### ToolsSection

`[tools]`

<!-- config: astralvessel.manifest.ToolsSection -->

### SandboxSection

`[sandbox]`

<!-- config: astralvessel.manifest.SandboxSection -->

### Mount

An entry of `[sandbox] mounts`.

<!-- config: astralvessel.manifest.Mount -->

### OutputSection

`[output]`

<!-- config: astralvessel.manifest.OutputSection -->

## Declared tool files

### DeclaredTool

<!-- config: astralvessel.manifest.DeclaredTool -->

## deployment.toml

### DeploymentFile

The top level of `deployment.toml`.

<!-- config: astralvessel.deployment.DeploymentFile -->

### VesselSection

`[vessel]`

<!-- config: astralvessel.deployment.VesselSection -->

### EgressSection

`[egress]`

<!-- config: astralvessel.deployment.EgressSection -->

### SearchSection

`[search]`

<!-- config: astralvessel.deployment.SearchSection -->

### AgentEntry

`[[agents]]`

<!-- config: astralvessel.deployment.AgentEntry -->

### SandboxProfile

`[agents.sandbox]`

<!-- config: astralvessel.deployment.SandboxProfile -->

### MountGrant

An entry of `[agents.sandbox] mounts`.

<!-- config: astralvessel.deployment.MountGrant -->

### ServiceGrant

`[agents.services.<name>]`

<!-- config: astralvessel.deployment.ServiceGrant -->

### AppEntry

`[[apps]]`

<!-- config: astralvessel.deployment.AppEntry -->

### IssuerEntry

`[[issuers]]`

<!-- config: astralvessel.deployment.IssuerEntry -->
