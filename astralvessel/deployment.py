# Loads deployment.toml: which agents this vessel serves, each agent's sandbox profile, and which apps may call them.
# The agent package says what it needs; this file is what the operator grants, and the only place grants come from.

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .manifest import AgentPackage, load_package

AGENT_ID = r"^[a-z][a-z0-9-]{0,62}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VesselSection(Strict):
    public_url: str = "http://vessel:8700"
    front_subnet: str = "172.31.247.0/24"
    front_address: str = "172.31.247.10"
    task_ttl_hours: float = Field(default=24, gt=0)


class EgressSection(Strict):
    upstream_url: str = "https://api-llm-factory.ai.uky.edu/v1"
    upstream_key: str = "env:VESSEL_UPSTREAM_MODEL_KEY"


class MountGrant(Strict):
    target: str
    source: str
    read_only: bool = True


class SandboxProfile(Strict):
    mounts: list[MountGrant] = []
    memory: str = "2g"
    cpus: float = Field(default=2, gt=0)
    pids: int = Field(default=512, ge=32)
    scratch: str = "256m"


class AgentEntry(Strict):
    id: str = Field(pattern=AGENT_ID)
    package: str
    sandbox: SandboxProfile = SandboxProfile()


class AppEntry(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    key_sha256: str
    agents: list[str]


class DeploymentFile(Strict):
    vessel: VesselSection = VesselSection()
    egress: EgressSection = EgressSection()
    agents: list[AgentEntry]
    apps: list[AppEntry]

    @model_validator(mode="after")
    def consistent(self) -> DeploymentFile:
        ids = [a.id for a in self.agents]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("agent ids must be present and unique")
        names = [a.name for a in self.apps]
        if len(set(names)) != len(names):
            raise ValueError("app names must be unique")
        for app in self.apps:
            unknown = sorted(set(app.agents) - set(ids))
            if unknown:
                raise ValueError(f"app {app.name} names unknown agents: {', '.join(unknown)}")
        return self


class Agent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    id: str
    package: AgentPackage
    host_package: Path
    profile: SandboxProfile

    @property
    def fingerprint(self) -> str:
        # Everything that shapes the sandbox container; a change means the operator must redeploy it.
        sandbox = self.package.manifest.sandbox.model_dump()
        body = {"id": self.id, "profile": self.profile.model_dump(), "needs": sandbox, "schema": 1}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


class Deployment(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    path: Path
    vessel: VesselSection
    egress: EgressSection
    agents: dict[str, Agent]
    apps: list[AppEntry]


def load_deployment(path: str | Path, packages_root: str | None = None) -> Deployment:
    # packages_root: inside the vessel container, packages are mounted at <root>/<agent id>, not their host paths.
    path = Path(path).resolve()
    raw = DeploymentFile.model_validate(tomllib.loads(path.read_text()))
    agents: dict[str, Agent] = {}
    for entry in raw.agents:
        host = (path.parent / entry.package).resolve()
        package = load_package(Path(packages_root) / entry.id if packages_root else host)
        _check_grants(entry, package)
        agents[entry.id] = Agent(id=entry.id, package=package, host_package=host, profile=entry.sandbox)
    return Deployment(path=path, vessel=raw.vessel, egress=raw.egress, agents=agents, apps=raw.apps)


def _check_grants(entry: AgentEntry, package: AgentPackage) -> None:
    needs = {m.target: m for m in package.manifest.sandbox.mounts}
    grants = {m.target: m for m in entry.sandbox.mounts}
    missing = sorted(set(needs) - set(grants))
    extra = sorted(set(grants) - set(needs))
    if missing:
        raise ValueError(f"agent {entry.id} needs mounts the deployment does not grant: {', '.join(missing)}")
    if extra:
        raise ValueError(f"agent {entry.id} is granted mounts it does not declare: {', '.join(extra)}")
    for target, need in needs.items():
        if need.read_only and not grants[target].read_only:
            raise ValueError(f"agent {entry.id} declares {target} read-only; the deployment may not make it writable")


def resolve(value: str) -> str:
    if value.startswith("env:"):
        resolved = os.environ.get(value[4:], "")
        if not resolved:
            raise ValueError(f"{value[4:]} is not set")
        return resolved
    return value


def sandbox_token(master: str, agent_id: str) -> str:
    # Each sandbox gets its own token, derived from one master secret the vessel holds.
    return hmac.new(master.encode(), f"sandbox:{agent_id}".encode(), hashlib.sha256).hexdigest()
