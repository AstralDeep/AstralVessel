# Loads an agent package: vessel.toml, prompt.md and declared tools.
# The manifest lists what an agent has; anything not listed does not exist.

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BUILTIN_TOOLS = ("read", "grep", "glob", "list", "bash", "webfetch", "websearch")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSection(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    description: str
    version: str = "0.1.0"
    examples: list[str] = []


class ModelSection(Strict):
    endpoint: str = "env:VESSEL_MODEL_URL"
    model: str
    api_key: str = "env:VESSEL_MODEL_KEY"
    temperature: float = 0.2
    max_steps: int = Field(default=40, ge=1, le=200)
    max_output_tokens: int = Field(default=8192, ge=256)


class Mount(Strict):
    source: str
    target: str
    read_only: bool = True


class SandboxSection(Strict):
    workdir: str = "/workspace"
    mounts: list[Mount] = []
    deadline_seconds: int = Field(default=600, ge=5, le=3600)
    tool_timeout_seconds: int = Field(default=60, ge=1, le=600)
    tool_output_chars: int = Field(default=20000, ge=1000, le=200000)


class ToolsSection(Strict):
    builtin: list[str] = []
    declared: list[str] = []

    @field_validator("builtin")
    @classmethod
    def known_builtins(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(BUILTIN_TOOLS))
        if unknown:
            raise ValueError(f"unknown builtin tools: {', '.join(unknown)}")
        return value


class OutputSection(Strict):
    primitives: bool = False


class Manifest(Strict):
    agent: AgentSection
    model: ModelSection
    tools: ToolsSection = ToolsSection()
    sandbox: SandboxSection = SandboxSection()
    output: OutputSection = OutputSection()


class DeclaredTool(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    description: str
    command: list[str]
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    timeout_seconds: int | None = None
    input: Literal["json-stdin", "args"] = "json-stdin"


class AgentPackage(BaseModel):
    root: Path
    manifest: Manifest
    prompt: str
    declared: list[DeclaredTool]


def load_package(path: str | Path) -> AgentPackage:
    root = Path(path).resolve()
    manifest = Manifest.model_validate(tomllib.loads((root / "vessel.toml").read_text()))
    prompt = (root / "prompt.md").read_text()
    declared = []
    for rel in manifest.tools.declared:
        tool_path = (root / rel).resolve()
        if root not in tool_path.parents:
            raise ValueError(f"declared tool {rel} is outside the package")
        declared.append(DeclaredTool.model_validate(tomllib.loads(tool_path.read_text())))
    names = [t.name for t in declared]
    clash = (set(names) & set(BUILTIN_TOOLS)) | {n for n in names if names.count(n) > 1}
    if clash:
        raise ValueError(f"declared tool names collide: {', '.join(sorted(clash))}")
    return AgentPackage(root=root, manifest=manifest, prompt=prompt, declared=declared)
