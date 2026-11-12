# The operator's commands: up writes a deployment when there is none, starts it, waits, probes and tests the model;
# check probes, down stops it, and chat and ask reach the vessel from a short-lived container on its front network.

from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .deployment import AGENT_ID, Deployment, EgressSection, load_deployment
from .manifest import load_package
from .probes import run_probes
from .stack import Stack

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEY_VARIABLE = "VESSEL_UPSTREAM_MODEL_KEY"
OWNER = "local"
HEALTH = r'''
import json, os, urllib.request
url = "http://" + os.environ["VESSEL_HOST"] + ":8700/health"
print(urllib.request.urlopen(url, timeout=5).read().decode())
'''
MODEL_CHECK = r'''
import json, os, urllib.error, urllib.request
for model in json.loads(MODELS):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "Reply with OK."}],
                       "max_tokens": 16, "stream": False}).encode()
    request = urllib.request.Request("http://egress:8080/v1/chat/completions", data=body, headers={
        "Authorization": "Bearer " + os.environ["VESSEL_MODEL_KEY"], "Content-Type": "application/json"})
    try:
        code, detail = urllib.request.urlopen(request, timeout=60).status, ""
    except urllib.error.HTTPError as exc:
        code, detail = exc.code, exc.read()[:300].decode(errors="replace")
    except OSError as exc:
        code, detail = 0, type(exc).__name__
    print(json.dumps({"model": model, "code": code, "detail": detail}), flush=True)
'''


def example_root() -> Path:
    # Installed, the example ships inside the package; in a checkout it is the repository's examples folder.
    packaged = Path(__file__).resolve().parent / "example"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parent.parent / "examples"


@dataclass
class Answers:
    agents: list[Path] = field(default_factory=list)
    mounts: dict[tuple[int, str], str] = field(default_factory=dict)  # (agent index, target) -> host folder
    model_url: str = ""
    api_key: str = ""
    model: str = ""


def _ask(prompt: str, secret: bool = False) -> str:
    try:
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except EOFError:
        raise SystemExit("\nstopped: no answer") from None


def _check_url(url: str) -> str | None:
    try:
        EgressSection(upstream_url=url)
    except ValueError:
        return "give an http(s) URL with a host, such as https://api.example.com/v1"
    return None


def _check_key(key: str) -> str | None:
    if key.startswith("env:"):
        name = key[4:]
        if not ENV_NAME.fullmatch(name):
            return f"{key} is not a valid environment variable name"
        if not os.environ.get(name):
            return f"{name} is not set in this shell"
        return None
    return None if key else "the API key is empty"


def _check_agent(path: str) -> str | None:
    try:
        load_package(Path(path).expanduser())
    except FileNotFoundError:
        return f"{path} is not an agent folder (it needs vessel.toml and prompt.md)"
    except (OSError, ValueError) as exc:
        return f"{path}: {exc}".splitlines()[0][:300]
    return None


def gather(args: argparse.Namespace, interactive: bool) -> Answers:
    answers = Answers(model_url=args.model_url or "", api_key=args.api_key or "", model=args.model or "")
    if args.api_key and not args.api_key.startswith("env:"):
        raise SystemExit("--api-key takes env:NAME, so the key never appears in a command line; "
                         "or leave it out and type it at the prompt")
    for given, check in ((answers.model_url, _check_url), (answers.api_key, _check_key)):
        if given and (problem := check(given)):
            raise SystemExit(problem)
    if args.example:
        answers.agents = [example_root() / "field-guide"]
        answers.mounts[(0, "/data")] = str(example_root() / "sample-data")
    for path in args.agent or []:
        if problem := _check_agent(path):
            raise SystemExit(problem)
        answers.agents.append(Path(path).expanduser().resolve())
    for grant in args.mount or []:
        match = re.fullmatch(r"([^:]+):(/[^=]*)=(.+)", grant)
        if not match:
            raise SystemExit(f"--mount {grant}: use AGENT_FOLDER:/target=/host/folder")
        index = next((i for i, a in enumerate(answers.agents)
                      if a.name == match[1] or str(a) == str(Path(match[1]).expanduser().resolve())), None)
        if index is None:
            raise SystemExit(f"--mount {grant}: {match[1]} is not one of the agents")
        answers.mounts[(index, match[2])] = match[3]
    if not interactive:
        missing = [flag for flag, have in (("--agent (or --example)", answers.agents), ("--model-url",
                   answers.model_url), ("--api-key env:NAME", answers.api_key), ("--model", answers.model)) if not have]
        missing += [f"--mount {a.name}:{m.target}=FOLDER" for i, a in enumerate(answers.agents)
                    for m in load_package(a).manifest.sandbox.mounts if (i, m.target) not in answers.mounts]
        if missing:
            raise SystemExit("no deployment.toml here and no terminal to ask on; pass " + ", ".join(missing))
        return answers
    print("No deployment.toml here, so let's write one. Press Ctrl-C to stop.\n")
    while not answers.agents:
        for part in _ask("Agent folders (comma-separated): ").split(","):
            if not part.strip():
                continue
            if problem := _check_agent(part.strip()):
                print("  " + problem)
                answers.agents = []
                break
            answers.agents.append(Path(part.strip()).expanduser().resolve())
    for index, agent in enumerate(answers.agents):
        for mount in load_package(agent).manifest.sandbox.mounts:
            while (index, mount.target) not in answers.mounts:
                folder = _ask(f"Folder to mount read-only at {mount.target} for {agent.name}: ")
                if Path(folder).expanduser().is_dir():
                    answers.mounts[(index, mount.target)] = folder
                else:
                    print(f"  {folder} is not a folder")
    while not answers.model_url:
        url = _ask("Model endpoint (OpenAI-compatible base URL, such as https://api.example.com/v1): ")
        if problem := _check_url(url):
            print("  " + problem)
        else:
            answers.model_url = url
    while not answers.api_key:
        key = _ask("API key (or env:NAME to read it from the environment; input is hidden): ", secret=True)
        if problem := _check_key(key):
            print("  " + problem)
        else:
            answers.api_key = key
    while not answers.model:
        answers.model = _ask("Model name: ")
    return answers


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    return slug[:63] or "astralvessel"


def _docker(*args: str) -> str:
    try:
        return subprocess.run(["docker", *args], check=True, text=True, capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def pick_project(folder: Path) -> str:
    # Another running deployment may already use this folder's name; never join or replace it.
    base = _slug(folder.name)
    for n in range(1, 50):
        name = base if n == 1 else f"{base[:60]}-{n}"
        dirs = set(_docker("ps", "-a", "--filter", f"label=com.docker.compose.project={name}", "--format",
                           '{{.Label "com.docker.compose.project.working_dir"}}').split())
        if not dirs or dirs == {str(folder)}:
            return name
    raise SystemExit("could not find a free project name; set [vessel] project yourself")


def pick_subnet() -> ipaddress.IPv4Network:
    # Docker refuses a network that overlaps another, so take the first free /24 in 172.31.0.0/16.
    ids = _docker("network", "ls", "-q").split()
    taken = []
    if ids:
        for text in _docker("network", "inspect", "-f", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids).split():
            try:
                taken.append(ipaddress.ip_network(text, strict=False))
            except ValueError:
                continue
    for third in range(247, 0, -1):
        net = ipaddress.ip_network(f"172.31.{third}.0/24")
        if not any(net.overlaps(other) for other in taken if other.version == 4):
            return net
    raise SystemExit("no free 172.31.x.0/24 subnet; set [vessel] front_subnet yourself")


def _toml(value: str) -> str:
    return json.dumps(value)


def _relative(path: Path, folder: Path) -> str:
    try:
        return str(path.resolve().relative_to(folder))
    except ValueError:
        return str(path.resolve())


def write_deployment(folder: Path, answers: Answers, *, example: bool) -> Path:
    deployment, env = folder / "deployment.toml", folder / ".env"
    if env.exists():
        raise SystemExit(f"{env} exists but deployment.toml does not; move the old .env away first")
    agents = list(answers.agents)
    mounts = dict(answers.mounts)
    if example:
        # Copy the example in, so the deployment folder holds everything it runs.
        shutil.copytree(agents[0], folder / "agents" / "field-guide", dirs_exist_ok=True)
        shutil.copytree(mounts[(0, "/data")], folder / "data", dirs_exist_ok=True)
        agents[0], mounts[(0, "/data")] = folder / "agents" / "field-guide", str(folder / "data")
    subnet = pick_subnet()
    lines = ["# Written by astralvessel up. Edit it and run astralvessel up again to apply a change.", "",
             "[vessel]", f"project = {_toml(pick_project(folder))}", f"front_subnet = {_toml(str(subnet))}",
             f"front_address = {_toml(str(subnet.network_address + 10))}", "",
             "[egress]", f"upstream_url = {_toml(answers.model_url)}"]
    key_ref = answers.api_key if answers.api_key.startswith("env:") else f"env:{KEY_VARIABLE}"
    lines += [f"upstream_key = {_toml(key_ref)}", ""]
    ids: list[str] = []
    for index, agent in enumerate(agents):
        package = load_package(agent)
        base = package.manifest.agent.name if re.fullmatch(AGENT_ID, package.manifest.agent.name) else "agent"
        agent_id = base
        while agent_id in ids:
            agent_id = f"{base[:58]}-{len(ids) + 1}"
        ids.append(agent_id)
        lines += ["[[agents]]", f"id = {_toml(agent_id)}", f"package = {_toml(_relative(agent, folder))}",
                  f"model = {_toml(answers.model)}"]
        grants = []
        for mount in package.manifest.sandbox.mounts:
            source = _relative(Path(mounts[(index, mount.target)]), folder)
            grants.append(f"{{ target = {_toml(mount.target)}, source = {_toml(source)} }}")
        if grants:
            lines += ["[agents.sandbox]", f"mounts = [{', '.join(grants)}]"]
        lines.append("")
    lines += ["[[apps]]", 'name = "cli"', 'key_sha256 = "env:VESSEL_APP_KEY_SHA256"',
              f"agents = [{', '.join(_toml(i) for i in ids)}]", ""]
    app_key = secrets.token_urlsafe(32)
    values = {"VESSEL_APP_KEY": app_key, "VESSEL_APP_KEY_SHA256": hashlib.sha256(app_key.encode()).hexdigest(),
              "VESSEL_SANDBOX_TOKEN": secrets.token_urlsafe(32)}
    if not answers.api_key.startswith("env:"):
        values[KEY_VARIABLE] = answers.api_key
    text = "".join(f"{k}={v}\n" for k, v in values.items())
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("# Secrets for this deployment; keep this file private.\n" + text)
    gitignore = folder / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".env\n.generated/\ncompose.generated.yml\nchats/\n")
    deployment.write_text("\n".join(lines))
    try:
        load_deployment(deployment)
    except ValueError:
        deployment.unlink()
        env.unlink()
        raise
    print(f"wrote {deployment} and {env} (private)")
    return deployment


def write_compose(stack: Stack, out: Path | None = None) -> tuple[Deployment, Path]:
    from .compose import write
    stack.load_env()
    os.environ.pop("VESSEL_JWKS_DIR", None)  # only the vessel container sets it; deploy needs host paths
    master = os.environ.get("VESSEL_SANDBOX_TOKEN", "")
    if len(master) < 16:
        raise SystemExit("set VESSEL_SANDBOX_TOKEN (16+ characters) in the environment or the env file")
    deployment = load_deployment(stack.deployment)
    repo = Path(__file__).resolve().parent.parent
    target = out or stack.compose_file
    for path in write(deployment, target, repo_root=repo if (repo / "Dockerfile").exists() else None,
                      master_token=master):
        print(f"wrote {path}")
    for agent_id, agent in deployment.agents.items():
        print(f"  agent {agent_id}: sandbox profile {agent.fingerprint}")
    return deployment, target


def _missing_env(deployment: Deployment) -> list[str]:
    refs = [deployment.egress.upstream_url, deployment.egress.upstream_key, *(a.key_sha256 for a in deployment.apps)]
    return [r[4:] for r in refs if r.startswith("env:") and not os.environ.get(r[4:])]


def wait_ready(stack: Stack, deployment: Deployment, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    agents: dict[str, bool] = {}
    print("waiting for every agent's sandbox to report it is confined", end="", flush=True)
    while time.monotonic() < deadline:
        done = stack.python("vessel", HEALTH, timeout=20)
        try:
            agents = json.loads(done.stdout).get("agents", {})
        except (json.JSONDecodeError, AttributeError):
            agents = {}
        if agents and all(agents.get(a) for a in deployment.agents):
            print(" ready")
            return True
        states = stack.compose("ps", "-a", "--format", "{{.Service}} {{.State}}", check=False, capture=True).stdout
        dead = [line.split()[0] for line in states.splitlines() if line.split()[1:2] in (["exited"], ["dead"])]
        if dead:
            print(f"\nstopped: {', '.join(dead)} exited")
            for service in dead:
                print(f"--- {service} ---\n{stack.logs(service)}")
            return False
        print(".", end="", flush=True)
        time.sleep(3)
    late = [a for a in deployment.agents if not agents.get(a)]
    print(f"\nnot ready after {timeout:.0f}s: {', '.join(late)}")
    for agent_id in late:
        print(f"--- sandbox-{agent_id} ---\n{stack.logs(f'sandbox-{agent_id}')}")
    print(f"--- vessel ---\n{stack.logs('vessel')}")
    return False


def check_models(stack: Stack, deployment: Deployment) -> bool:
    models = sorted({a.model for a in deployment.agents.values()})
    done = stack.python("vessel", f"MODELS = {json.dumps(json.dumps(models))}\n" + MODEL_CHECK, timeout=200)
    ok = done.returncode == 0
    seen = 0
    for raw in done.stdout.splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        seen += 1
        if line["code"] == 200:
            print(f"PASS  model {line['model']} answers through the egress")
        else:
            ok = False
            print(f"FAIL  model {line['model']}: HTTP {line['code']} {line['detail']}".rstrip())
    return ok and seen == len(models)


def up(args: argparse.Namespace) -> None:
    stack = Stack.at(args.deployment or "deployment.toml")
    if not stack.deployment.exists():
        if args.deployment:
            raise SystemExit(f"{stack.deployment} does not exist")
        answers = gather(args, interactive=sys.stdin.isatty() and sys.stdout.isatty())
        write_deployment(stack.deployment.parent, answers, example=args.example)
    elif args.example or args.agent or args.model_url or args.api_key or args.model or args.mount:
        raise SystemExit(f"{stack.deployment} exists; edit it instead of passing setup options")
    deployment, _ = write_compose(stack)
    if missing := _missing_env(deployment):
        raise SystemExit(f"set {', '.join(missing)} in the environment or {stack.env_file}")
    stack.compose("up", "-d", "--build", "--remove-orphans")
    if not wait_ready(stack, deployment, args.timeout):
        raise SystemExit("the deployment is running but not ready; fix the problem above and run astralvessel up")
    report = run_probes(stack, deployment, os.environ["VESSEL_SANDBOX_TOKEN"])
    models_ok = check_models(stack, deployment)
    print(f"{report.passed} probes passed, {len(report.failed)} failed, {report.skipped} skipped")
    if report.failed or not models_ok:
        raise SystemExit("the deployment is running, but a check failed; it is not ready for use")
    first = next(iter(deployment.agents))
    print(f"\nready: {', '.join(deployment.agents)}\n  astralvessel chat {first}\n"
          f"  astralvessel ask {first} \"your question\"")


def check(args: argparse.Namespace) -> None:
    stack = Stack.at(args.deployment or "deployment.toml")
    stack.require_compose()
    stack.load_env()
    os.environ.pop("VESSEL_JWKS_DIR", None)
    deployment = load_deployment(stack.deployment)
    report = run_probes(stack, deployment, os.environ.get("VESSEL_SANDBOX_TOKEN", ""))
    print(f"{report.passed} passed, {len(report.failed)} failed, {report.skipped} skipped")
    if report.failed:
        raise SystemExit(1)


def down(args: argparse.Namespace) -> None:
    stack = Stack.at(args.deployment or "deployment.toml")
    stack.require_compose()
    stack.compose("down", *(["-v"] if args.volumes else []))


def client(args: argparse.Namespace, mode: str) -> None:
    # The vessel publishes no ports, so the client runs in a throwaway container on its internal front network.
    stack = Stack.at(args.deployment or "deployment.toml")
    stack.require_compose()
    stack.load_env()
    os.environ.pop("VESSEL_JWKS_DIR", None)
    if not os.environ.get("VESSEL_APP_KEY"):
        raise SystemExit(f"set VESSEL_APP_KEY in the environment or {stack.env_file}")
    deployment = load_deployment(stack.deployment)
    if args.agent not in deployment.agents:
        raise SystemExit(f"no agent {args.agent}; this deployment has {', '.join(deployment.agents)}")
    image = stack.container_image("vessel")
    chats = stack.deployment.parent / "chats"
    chats.mkdir(mode=0o700, exist_ok=True)
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    command = ["docker", "run", "--rm", "-i", *(["-t"] if tty else []),
               "--network", f"{deployment.vessel.project}_front", "--user", f"{os.getuid()}:{os.getgid()}",
               "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--pids-limit", "64", "-e", "VESSEL_APP_KEY", "-e", "HOME=/tmp", "-v", f"{chats}:/chats",
               image, "astralvessel", "client", mode, args.agent]
    if mode == "ask":
        command.append(args.question)
    else:
        command += [*(["--resume", args.resume] if args.resume else []), *(["--list"] if args.list else [])]
    raise SystemExit(subprocess.run(command, check=False).returncode)
