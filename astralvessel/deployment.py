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


class ServiceGrant(Strict):
    # An application endpoint the vessel calls for the agent's service tools; values may be "env:NAME".
    url: str
    headers: dict[str, str] = {}

    @model_validator(mode="after")
    def plain(self) -> ServiceGrant:
        if any(name.lower().startswith("x-vessel-") for name in self.headers):
            raise ValueError("X-Vessel-* headers are set by the vessel and may not be granted")
        if not self.url.startswith("env:"):
            check_service_url(self.url)
        return self


def check_service_url(url: str) -> str:
    # The tool's path is appended, so a query or fragment here would swallow it.
    if not url.startswith(("http://", "https://")) or "?" in url or "#" in url:
        raise ValueError(f"service url {url!r} must be http(s) with no query or fragment")
    return url


class AgentEntry(Strict):
    id: str = Field(pattern=AGENT_ID)
    package: str
    sandbox: SandboxProfile = SandboxProfile()
    services: dict[str, ServiceGrant] = {}
    # Extra briefing files from the package, appended after prompt.md: one package can serve as several agents.
    prompt_append: list[str] = []


class AppEntry(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    key_sha256: str
    agents: list[str]


ASYMMETRIC_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA")


class IssuerEntry(Strict):
    # A token issuer callers may authenticate with instead of an app key (for example an Astral deployment's
    # Keycloak). Owners are "<id>|<sub>", so two issuers can never name the same owner.
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    issuer: str
    audience: str
    jwks_url: str | None = None
    jwks_file: str | None = None
    authorized_parties: list[str] = []
    algorithms: list[str] = ["RS256", "ES256", "EdDSA"]
    agents: list[str]

    @model_validator(mode="after")
    def keys(self) -> IssuerEntry:
        if (self.jwks_url is None) == (self.jwks_file is None):
            raise ValueError(f"issuer {self.id} needs exactly one of jwks_url or jwks_file")
        if self.jwks_url and not self.jwks_url.startswith("https://"):
            raise ValueError(f"issuer {self.id}: jwks_url must be https")
        unknown = sorted(set(self.algorithms) - set(ASYMMETRIC_ALGORITHMS))
        if unknown or not self.algorithms:
            raise ValueError(f"issuer {self.id}: only asymmetric algorithms are accepted, not {unknown}")
        return self


class DeploymentFile(Strict):
    vessel: VesselSection = VesselSection()
    egress: EgressSection = EgressSection()
    agents: list[AgentEntry]
    apps: list[AppEntry] = []
    issuers: list[IssuerEntry] = []

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
        issuer_ids = [i.id for i in self.issuers]
        if len(set(issuer_ids)) != len(issuer_ids):
            raise ValueError("issuer ids must be unique")
        for issuer in self.issuers:
            unknown = sorted(set(issuer.agents) - set(ids))
            if unknown:
                raise ValueError(f"issuer {issuer.id} names unknown agents: {', '.join(unknown)}")
        if not self.apps and not self.issuers:
            raise ValueError("a deployment needs at least one app or issuer")
        return self


class Agent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    id: str
    package: AgentPackage
    host_package: Path
    profile: SandboxProfile
    services: dict[str, ServiceGrant] = {}
    prompt_append: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        # Everything that shapes the sandbox container; a change means the operator must redeploy it.
        sandbox = self.package.manifest.sandbox.model_dump()
        dockerfile = self.package.manifest.sandbox.dockerfile
        image = build_inputs_hash(self.package.root, dockerfile) if dockerfile else None
        body = {"id": self.id, "profile": self.profile.model_dump(), "needs": sandbox, "image": image, "schema": 1}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


class Deployment(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    path: Path
    vessel: VesselSection
    egress: EgressSection
    agents: dict[str, Agent]
    apps: list[AppEntry]
    issuers: list[IssuerEntry] = []


def load_deployment(path: str | Path, packages_root: str | None = None) -> Deployment:
    # packages_root: inside the vessel container, packages are mounted at <root>/<agent id>, not their host paths.
    path = Path(path).resolve()
    raw = DeploymentFile.model_validate(tomllib.loads(path.read_text()))
    agents: dict[str, Agent] = {}
    for entry in raw.agents:
        host = (path.parent / entry.package).resolve()
        package = load_package(Path(packages_root) / entry.id if packages_root else host)
        _check_grants(entry, package)
        for name in entry.prompt_append:
            target = (package.root / name).resolve()
            if not target.is_file() or package.root not in target.parents:
                raise ValueError(f"agent {entry.id}: prompt_append {name} must be a file inside the package")
        agents[entry.id] = Agent(id=entry.id, package=package, host_package=host, profile=entry.sandbox,
                                 services=entry.services, prompt_append=tuple(entry.prompt_append))
    issuers = [i.model_copy(update={"jwks_file": str((path.parent / i.jwks_file).resolve())}) if i.jwks_file else i
               for i in raw.issuers]
    return Deployment(path=path, vessel=raw.vessel, egress=raw.egress, agents=agents, apps=raw.apps,
                      issuers=issuers)


def _check_grants(entry: AgentEntry, package: AgentPackage) -> None:
    missing_services = sorted(package.services() - set(entry.services))
    extra_services = sorted(set(entry.services) - package.services())
    if missing_services or extra_services:
        raise ValueError(f"agent {entry.id}: services must match its tools; missing "
                         f"{', '.join(missing_services) or 'none'}, unused {', '.join(extra_services) or 'none'}")
    needs = {m.target: m for m in package.manifest.sandbox.mounts}
    grants = {m.target: m for m in entry.sandbox.mounts}
    missing = sorted(set(needs) - set(grants))
    extra = sorted(set(grants) - set(needs))
    if missing:
        raise ValueError(f"agent {entry.id} needs mounts the deployment does not grant: {', '.join(missing)}")
    if extra:
        raise ValueError(f"agent {entry.id} is granted mounts it does not declare: {', '.join(extra)}")
    for target, need in needs.items():
        # Every sandbox can serve many users at once, and a writable mount would be shared between them.
        if not need.read_only or not grants[target].read_only:
            raise ValueError(f"agent {entry.id}: writable mounts ({target}) are not supported yet; mounts are "
                             "read-only and each call writes only its own scratch directory")


def build_inputs_hash(root: Path, dockerfile: str) -> str:
    # The Dockerfile plus every package file its COPY and ADD lines bring in, so editing one marks the sandbox stale.
    digest = hashlib.sha256((root / dockerfile).read_bytes())
    for line in (root / dockerfile).read_text().splitlines():
        words = line.split()
        if not words or words[0].upper() not in ("COPY", "ADD"):
            continue
        sources = [w for w in words[1:-1] if not w.startswith("--")]
        for source in sources:
            for path in sorted(root.glob(source.lstrip("/"))):
                files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
                for file in files:
                    if root in file.resolve().parents:
                        digest.update(str(file.relative_to(root)).encode() + b"\0" + file.read_bytes())
    return digest.hexdigest()


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
