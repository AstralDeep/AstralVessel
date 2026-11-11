# Command line entry points: deploy (write Compose from deployment.toml), serve the vessel, or run one agent's sandbox.
# Containers configure serve and sandbox through the environment, so they need no flags.

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def _load_env_file(path: Path) -> None:
    # Fill only what the environment lacks, so an exported value always wins over the file.
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def deploy(args: argparse.Namespace) -> None:
    from .compose import write
    from .deployment import load_deployment
    deployment_path = Path(args.deployment).resolve()
    env_file = Path(args.env_file) if args.env_file else deployment_path.parent / ".env"
    if env_file.exists():
        _load_env_file(env_file)
    os.environ.pop("VESSEL_JWKS_DIR", None)  # only the vessel container sets it; deploy needs host paths
    master = os.environ.get("VESSEL_SANDBOX_TOKEN", "")
    if len(master) < 16:
        raise SystemExit("set VESSEL_SANDBOX_TOKEN (16+ characters) in the environment or the env file")
    deployment = load_deployment(deployment_path)
    repo = Path(__file__).resolve().parent.parent
    out = Path(args.out).resolve() if args.out else deployment_path.parent / "compose.generated.yml"
    for path in write(deployment, out, repo_root=repo if (repo / "Dockerfile").exists() else None,
                      master_token=master):
        print(f"wrote {path}")
    for agent_id, agent in deployment.agents.items():
        print(f"  agent {agent_id}: sandbox profile {agent.fingerprint}")
    command = ["docker", "compose", "-f", str(out)] + (["--env-file", str(env_file)] if env_file.exists() else [])
    if args.up:
        subprocess.run([*command, "up", "-d", "--build", "--remove-orphans"], check=True)
    else:
        print("start it with: " + " ".join([*command, "up", "-d", "--build"]))


def main() -> None:
    parser = argparse.ArgumentParser(prog="astralvessel")
    sub = parser.add_subparsers(dest="command", required=True)
    dep = sub.add_parser("deploy", help="write a Compose file from deployment.toml")
    dep.add_argument("deployment", nargs="?", default="deployment.toml")
    dep.add_argument("--out", help="Compose file to write (default: compose.generated.yml beside the deployment)")
    dep.add_argument("--env-file", help="environment file to read (default: .env beside the deployment)")
    dep.add_argument("--up", action="store_true", help="also run docker compose up")
    serve = sub.add_parser("serve", help="serve every agent in a deployment")
    serve.add_argument("deployment", nargs="?", default=os.environ.get("VESSEL_DEPLOYMENT"))
    sandbox = sub.add_parser("sandbox", help="run one agent's sandbox tool server")
    sandbox.add_argument("package", nargs="?", default=os.environ.get("VESSEL_PACKAGE"))
    for cmd, port in ((serve, "8700"), (sandbox, "8710")):
        cmd.add_argument("--host", default=os.environ.get("VESSEL_HOST", "127.0.0.1"))
        cmd.add_argument("--port", type=int, default=int(os.environ.get("VESSEL_PORT", port)))
    args = parser.parse_args()
    if args.command == "deploy":
        deploy(args)
        return
    import uvicorn
    if args.command == "serve":
        if not args.deployment:
            parser.error("give a deployment file or set VESSEL_DEPLOYMENT")
        os.environ["VESSEL_DEPLOYMENT"] = args.deployment
        from .server import create_app
        app = create_app()
    else:
        if not args.package:
            parser.error("give an agent package directory or set VESSEL_PACKAGE")
        from .sandbox import sandbox_app
        app = sandbox_app(args.package)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
