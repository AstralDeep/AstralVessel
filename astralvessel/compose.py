# Turns deployment.toml into a Docker Compose file: egress, the vessel, and one confined sandbox per agent.
# Nothing in the running system touches Docker; the operator runs `docker compose up` on what this writes.

from __future__ import annotations

import hashlib
import json
import os
import secrets as tokens
from pathlib import Path
from typing import Any

from .deployment import Deployment, egress_token, sandbox_token

IMAGE = "astralvessel:local"
SANDBOX_PORT = 8710
SEARXNG_IMAGE = "searxng/searxng:2026.10.25-54ca1d9cf"
# JSON answers only, no rate limiter or image proxy (one private caller), every request through the egress, and
# more web engines than the default three, so one engine's CAPTCHA or rate limit leaves the others answering.
SEARXNG_SETTINGS = """use_default_settings: true
general:
  instance_name: "astralvessel-search"
search:
  formats: [json]
  suspended_times:
    SearxEngineCaptcha: 600
server:
  limiter: false
  image_proxy: false
  public_instance: false
outgoing:
  request_timeout: 6.0
  proxies:
    all://:
      - http://egress:3128
engines:
  - {name: bing, disabled: false}
  - {name: yahoo, disabled: false}
  - {name: mojeek, disabled: false, inactive: false}
  - {name: startpage, disabled: false, inactive: false}
"""
HARDENED: dict[str, Any] = {
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "restart": "unless-stopped",
    "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
}


def _env_ref(value: str) -> str:
    # "env:NAME" stays a reference that Compose fills from the operator's environment, never a copied secret.
    if value.startswith("env:"):
        name = value[4:]
        return f"${{{name}:?set {name} in the environment or .env}}"
    return value


def _source(deployment: Deployment, source: str) -> str:
    # "volume:NAME" is an existing Docker volume (for data another stack writes); a relative path is relative to
    # the deployment file, like an agent's package path.
    if source.startswith("volume:"):
        return source[7:]
    if source.startswith("env:") or os.path.isabs(source):
        return _env_ref(source)
    return str((deployment.path.parent / source).resolve())


def _image(repo_root: Path | None, dockerfile: str = "Dockerfile", tag: str = IMAGE) -> dict[str, Any]:
    if repo_root is None:
        return {"image": tag}
    return {"image": tag, "build": {"context": str(repo_root), "dockerfile": dockerfile}}


def generate(deployment: Deployment, *, repo_root: Path | None) -> dict[str, Any]:
    vessel_env: dict[str, str] = {
        "VESSEL_DEPLOYMENT": "/deploy/deployment.toml",
        "VESSEL_PACKAGES_ROOT": "/agents",
        "VESSEL_MODEL_URL": "http://egress:8080/v1",
        "VESSEL_WEB_PROXY": "http://egress:3128",
        "VESSEL_SANDBOX_URL_TEMPLATE": f"http://sandbox-{{id}}:{SANDBOX_PORT}",
        "VESSEL_SANDBOX_TOKEN": _env_ref("env:VESSEL_SANDBOX_TOKEN"),
        "VESSEL_TASK_DB": "/data/tasks.sqlite",
        # Listen only on the front network, so no sandbox (each shares a tools network with the vessel) can reach it.
        "VESSEL_HOST": deployment.vessel.front_address,
    }
    secrets = [app.key_sha256 for app in deployment.apps] + [
        value for agent in deployment.agents.values() for grant in agent.services.values()
        for value in (grant.url, *grant.headers.values())]
    for value in secrets:
        if value.startswith("env:"):
            vessel_env[value[4:]] = _env_ref(value)
    services: dict[str, Any] = {
        "egress": {**HARDENED, **_image(repo_root and repo_root / "egress", tag="astralvessel-egress:local"),
                   "environment": {"LLM_BASE_URL": deployment.egress.upstream_url,
                                   "LLM_API_KEY": _env_ref(deployment.egress.upstream_key)},
                   "env_file": [".generated/egress.env"],
                   "networks": ["egress", "outside"], "mem_limit": "512m", "pids_limit": 128},
    }
    networks: dict[str, Any] = {
        "egress": {"internal": True},
        "front": {"internal": True, "ipam": {"config": [{"subnet": deployment.vessel.front_subnet}]}},
        "outside": {},
    }
    if deployment.search.provider == "searxng":
        # Only on the egress network: the vessel can ask it, no sandbox can, and it reaches the web through the egress.
        vessel_env["VESSEL_SEARCH_URL"] = "http://search:8080"
        services["search"] = {
            **HARDENED, "image": SEARXNG_IMAGE, "user": "977:977",
            "env_file": [".generated/search.env"],
            "volumes": ["./.generated/searxng.yml:/etc/searxng/settings.yml:ro"],
            "tmpfs": ["/tmp:size=32m,noexec,nosuid,nodev,mode=1777",
                      "/etc/searxng:size=1m,noexec,nosuid,nodev,uid=977,gid=977",
                      "/var/cache/searxng:size=32m,noexec,nosuid,nodev,uid=977,gid=977"],
            "networks": ["egress"], "mem_limit": "512m", "pids_limit": 128, "depends_on": ["egress"],
        }
    vessel_volumes = [f"{deployment.path}:/deploy/deployment.toml:ro", "vessel_data:/data"]
    vessel_networks: dict[str, Any] = {"front": {"ipv4_address": deployment.vessel.front_address}, "egress": {}}
    for agent_id, agent in deployment.agents.items():
        tools = f"tools-{agent_id}"
        networks[tools] = {"internal": True}
        vessel_networks[tools] = {}
        vessel_volumes.append(f"{agent.host_package}:/agents/{agent_id}:ro")
        mounts = [f"{agent.host_package}:/agent:ro"] + [
            f"{_source(deployment, m.source)}:{m.target}:{'ro' if m.read_only else 'rw'}" for m in agent.profile.mounts]
        dockerfile = agent.package.manifest.sandbox.dockerfile
        if dockerfile:
            # The agent's own image, built FROM the AstralVessel image (named "astralvessel" in its Dockerfile).
            base = "service:vessel" if repo_root else f"docker-image://{IMAGE}"
            image = {"image": f"astralvessel-sandbox-{agent_id}:local",
                     "build": {"context": str(agent.host_package), "dockerfile": dockerfile,
                               "additional_contexts": {"astralvessel": base}}}
        else:
            image = _image(repo_root)
        services[f"sandbox-{agent_id}"] = {
            **HARDENED, **image,
            "command": ["astralvessel", "sandbox"],
            "environment": {"VESSEL_PACKAGE": "/agent", "VESSEL_PORT": str(SANDBOX_PORT),
                            "VESSEL_PROFILE_HASH": agent.fingerprint,
                            "VESSEL_TURN_DISK": str(agent.profile.turn_bytes),
                            "VESSEL_USER_DISK": str(agent.profile.user_bytes)},
            "env_file": [f".generated/sandbox-{agent_id}.env"],
            "volumes": mounts,
            "tmpfs": [f"/tmp:size={agent.profile.scratch},noexec,nosuid,nodev,mode=1777"],
            "networks": [tools],
            "mem_limit": agent.profile.memory, "cpus": agent.profile.cpus, "pids_limit": agent.profile.pids,
            "labels": {"astralvessel.agent": agent_id, "astralvessel.profile": agent.fingerprint},
        }
    services["vessel"] = {
        **HARDENED, **_image(repo_root),
        "environment": vessel_env,
        "env_file": [".generated/vessel.env"],
        "volumes": vessel_volumes,
        "tmpfs": ["/tmp:size=64m,noexec,nosuid,nodev,mode=1777"],
        "networks": vessel_networks,
        # The vessel reads the deployment file and every package at start, so a change to either must recreate it.
        "labels": {"astralvessel.inputs": inputs_hash(deployment)},
        "mem_limit": "1g", "pids_limit": 256,
        "depends_on": ["egress", *(["search"] if "search" in services else []),
                       *(f"sandbox-{a}" for a in deployment.agents)],
    }
    volumes: dict[str, Any] = {"vessel_data": {}}
    for agent in deployment.agents.values():
        for mount in agent.profile.mounts:
            if mount.source.startswith("volume:"):
                volumes[mount.source[7:]] = {"external": True}
    return {"name": "astralvessel", "services": services, "networks": networks, "volumes": volumes}


def inputs_hash(deployment: Deployment) -> str:
    body = {"deployment": hashlib.sha256(deployment.path.read_bytes()).hexdigest(),
            "agents": {agent_id: agent.fingerprint for agent_id, agent in deployment.agents.items()}}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def write(deployment: Deployment, out: Path, *, repo_root: Path | None, master_token: str) -> list[Path]:
    secrets = out.parent / ".generated"
    secrets.mkdir(mode=0o700, exist_ok=True)
    (secrets / ".gitignore").write_text("*\n")
    written = [out]
    for name, line in (("egress", "EGRESS_TOKEN"), ("vessel", "VESSEL_MODEL_KEY")):
        path = secrets / f"{name}.env"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{line}={egress_token(master_token)}\n")
        written.append(path)
    for agent_id in deployment.agents:
        path = secrets / f"sandbox-{agent_id}.env"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(f"VESSEL_SANDBOX_TOKEN={sandbox_token(master_token, agent_id)}\n")
        written.append(path)
    if deployment.search.provider == "searxng":
        settings = secrets / "searxng.yml"
        settings.write_text(SEARXNG_SETTINGS)
        settings.chmod(0o644)  # read by the search container's own user
        env = secrets / "search.env"
        if not env.exists():  # keep the key across redeploys so the container is not recreated for nothing
            fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(f"SEARXNG_SECRET={tokens.token_hex(32)}\n")
        written += [settings, env]
    header = ("# Generated by `astralvessel deploy` from " + str(deployment.path) + ". Do not edit; edit the\n"
              "# deployment file and run deploy again. Each sandbox is labeled with its profile fingerprint.\n")
    out.write_text(header + to_yaml(generate(deployment, repo_root=repo_root)))
    return written


def to_yaml(value: Any, indent: int = 0) -> str:
    # A small emitter for the plain dicts, lists and scalars above; every scalar is JSON-quoted, which YAML accepts.
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}\n"
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:\n{to_yaml(item, indent + 1)}")
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}\n")
        return "".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                body = to_yaml(item, indent + 1)
                lines.append(f"{pad}- {body.lstrip()}")
            else:
                lines.append(f"{pad}- {_scalar(item)}\n")
        return "".join(lines)
    return f"{pad}{_scalar(value)}\n"


def _scalar(value: Any) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return json.dumps(value)
