# Command line entry points: up, check, chat, ask and down for operators; deploy writes Compose from a deployment file;
# serve and sandbox run inside the containers, configured through the environment.

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def deploy(args: argparse.Namespace) -> None:
    from .operate import write_compose
    from .stack import Stack
    stack = Stack.at(args.deployment)
    if args.env_file:
        stack.env_file = Path(args.env_file)
    deployment, out = write_compose(stack, Path(args.out).resolve() if args.out else None)
    stack.compose_file = out
    command = stack.compose_command()
    if args.up:
        subprocess.run([*command, "up", "-d", "--build", "--remove-orphans"], check=True)
    else:
        print("start it with: " + " ".join([*command, "up", "-d", "--build"]))


def main() -> None:
    parser = argparse.ArgumentParser(prog="astralvessel")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="start a deployment, writing one first if there is none; then check it")
    up.add_argument("deployment", nargs="?", help="deployment file (default: deployment.toml here)")
    up.add_argument("--example", action="store_true", help="set up the example field guide agent")
    up.add_argument("--agent", action="append", metavar="FOLDER", help="an agent folder (repeatable)")
    up.add_argument("--mount", action="append", metavar="AGENT:/TARGET=FOLDER", help="a folder an agent needs")
    up.add_argument("--model-url", help="the model provider's OpenAI-compatible base URL")
    up.add_argument("--api-key", metavar="env:NAME", help="read the provider's key from this variable")
    up.add_argument("--model", help="the model every agent uses")
    up.add_argument("--timeout", type=float, default=300, help="seconds to wait for the sandboxes (default 300)")
    check = sub.add_parser("check", help="run the isolation probes against every agent")
    check.add_argument("deployment", nargs="?")
    chat = sub.add_parser("chat", help="chat with an agent; chats are saved in chats/ and can be resumed")
    chat.add_argument("agent")
    chat.add_argument("--resume", metavar="ID", help="continue a saved chat")
    chat.add_argument("--list", action="store_true", help="list saved chats")
    chat.add_argument("--deployment")
    ask = sub.add_parser("ask", help="ask an agent one question, with no history")
    ask.add_argument("agent")
    ask.add_argument("question")
    ask.add_argument("--deployment")
    down = sub.add_parser("down", help="stop the deployment")
    down.add_argument("deployment", nargs="?")
    down.add_argument("--volumes", action="store_true", help="also delete the task store volume")
    inner = sub.add_parser("client", help="the chat and ask client, run inside a container on the front network")
    inner.add_argument("mode", choices=["chat", "ask"])
    inner.add_argument("agent")
    inner.add_argument("question", nargs="?")
    inner.add_argument("--resume")
    inner.add_argument("--list", action="store_true")
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
    if args.command in ("up", "check", "down", "chat", "ask"):
        from . import operate
        if args.command in ("chat", "ask"):
            operate.client(args, args.command)
        else:
            getattr(operate, args.command)(args)
        return
    if args.command == "client":
        if args.mode == "ask" and not args.question:
            parser.error("ask needs a question")
        from .chat import main as client_main
        client_main(args)
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
