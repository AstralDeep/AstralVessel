# A running deployment as the operator's commands see it: its folder, env file, Compose file and containers.
# up, check, chat, ask and down all drive Docker through this, so they agree on paths and project names.

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

COMPOSE_FILE = "compose.generated.yml"


def load_env_file(path: Path) -> None:
    # Fill only what the environment lacks, so an exported value always wins over the file.
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass
class Stack:
    deployment: Path
    env_file: Path
    compose_file: Path

    @classmethod
    def at(cls, deployment: str | Path = "deployment.toml") -> Stack:
        path = Path(deployment).resolve()
        return cls(path, path.parent / ".env", path.parent / COMPOSE_FILE)

    def load_env(self) -> None:
        if self.env_file.exists():
            load_env_file(self.env_file)

    def compose_command(self) -> list[str]:
        command = ["docker", "compose", "-f", str(self.compose_file)]
        return command + (["--env-file", str(self.env_file)] if self.env_file.exists() else [])

    def compose(self, *args: str, check: bool = True, capture: bool = False,
                stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([*self.compose_command(), *args], check=check, text=True, input=stdin,
                              capture_output=capture)

    def python(self, service: str, source: str, interpreter: str = "python",
               timeout: float = 300) -> subprocess.CompletedProcess:
        # The script goes in on stdin, so nothing it carries shows up in a process listing.
        return subprocess.run([*self.compose_command(), "exec", "-T", service, interpreter, "-"], text=True,
                              input=source, capture_output=True, timeout=timeout, check=False)

    def logs(self, service: str, tail: int = 40) -> str:
        done = self.compose("logs", "--no-color", "--tail", str(tail), service, check=False, capture=True)
        return (done.stdout or "") + (done.stderr or "")

    def container_image(self, service: str) -> str:
        found = self.compose("ps", "-q", service, check=False, capture=True).stdout.strip()
        if not found:
            raise SystemExit(f"the {service} container is not running; start the deployment with astralvessel up")
        return subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", found.splitlines()[0]],
                              check=True, text=True, capture_output=True).stdout.strip()

    def require_compose(self) -> None:
        if not self.compose_file.exists():
            raise SystemExit(f"{self.compose_file} does not exist; run astralvessel up first")
