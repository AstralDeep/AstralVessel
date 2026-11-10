# Command line entry points: serve an agent package, or run the sandbox tool server beside it.
# Settings come from the environment so containers configure both without flags.

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="astralvessel")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "sandbox"):
        cmd = sub.add_parser(name)
        cmd.add_argument("package", nargs="?", default=os.environ.get("VESSEL_PACKAGE"))
        cmd.add_argument("--host", default=os.environ.get("VESSEL_HOST", "127.0.0.1"))
        cmd.add_argument("--port", type=int, default=int(os.environ.get("VESSEL_PORT", "8700")))
    args = parser.parse_args()
    if not args.package:
        parser.error("give an agent package directory or set VESSEL_PACKAGE")
    os.environ["VESSEL_PACKAGE"] = args.package
    if args.command == "serve":
        from .server import create_app
        app = create_app()
    else:
        from .sandbox import sandbox_app
        app = sandbox_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
