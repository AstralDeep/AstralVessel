# Tool definitions shown to the model and the file and shell tools that run inside the sandbox.
# run_tool executes one call with time and output limits and never raises for tool-level failures.

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .manifest import DeclaredTool

SANDBOX_TOOLS = ("read", "grep", "glob", "list", "bash")
WEB_TOOLS = ("webfetch", "websearch")
SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
            "HOME": "/tmp", "TMPDIR": "/tmp"}

BUILTIN_SCHEMAS: dict[str, dict[str, Any]] = {
    "read": {
        "description": "Read a text file. Returns numbered lines. Use offset and limit for large files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path, or relative to the working directory"},
            "offset": {"type": "integer", "description": "First line to return, starting at 1"},
            "limit": {"type": "integer", "description": "Number of lines to return (default 400)"},
        }, "required": ["path"]},
    },
    "grep": {
        "description": "Search file contents with a regular expression (ripgrep syntax). "
                       "Returns matching lines as path:line:text.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory to search (default: working directory)"},
            "glob": {"type": "string", "description": "Only search files matching this glob, e.g. *.md"},
            "ignore_case": {"type": "boolean"},
            "max_results": {"type": "integer", "description": "Default 100"},
        }, "required": ["pattern"]},
    },
    "glob": {
        "description": "Find files whose path matches a glob pattern such as wiki/person-*.md.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Directory to search from (default: working directory)"},
            "max_results": {"type": "integer", "description": "Default 200"},
        }, "required": ["pattern"]},
    },
    "list": {
        "description": "List the entries of a directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory (default: working directory)"},
        }},
    },
    "bash": {
        "description": "Run a shell command in the sandbox and return its combined output and exit code.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
        }, "required": ["command"]},
    },
    "webfetch": {
        "description": "Fetch a public web page and return its readable text.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "http or https URL"},
        }, "required": ["url"]},
    },
    "websearch": {
        "description": "Search the web. Returns titles, links and snippets.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "Default 8"},
        }, "required": ["query"]},
    },
}


def tool_spec(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: limit - 200]
    return head + f"\n\n[output truncated: {len(text) - len(head)} more characters]", True


def _resolve(path: str | None, workdir: str) -> Path:
    base = Path(workdir)
    if not path:
        return base
    candidate = Path(path)
    return candidate if candidate.is_absolute() else base / candidate


async def _proc(argv: list[str], *, cwd: str, timeout: int, shell: bool = False,
                stdin: bytes | None = None) -> tuple[int, str]:
    if shell:
        proc = await asyncio.create_subprocess_shell(
            argv[0], cwd=cwd, env=SAFE_ENV, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    else:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=SAFE_ENV, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 124, f"[timed out after {timeout} s]"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def _read(args: dict[str, Any], workdir: str) -> tuple[str, bool]:
    path = _resolve(args.get("path"), workdir)
    if not path.is_file():
        return f"not a file: {path}", True
    offset = max(int(args.get("offset") or 1), 1)
    limit = min(max(int(args.get("limit") or 400), 1), 2000)
    lines = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if number < offset:
                continue
            if number >= offset + limit:
                lines.append(f"[more lines follow; continue with offset={number}]")
                break
            lines.append(f"{number:>6}\t{line.rstrip()}")
    return "\n".join(lines) or "[empty]", False


async def _grep(args: dict[str, Any], workdir: str, timeout: int) -> tuple[str, bool]:
    target = str(_resolve(args.get("path"), workdir))
    cap = min(max(int(args.get("max_results") or 100), 1), 1000)
    if shutil.which("rg"):
        argv = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns=400"]
        if args.get("ignore_case"):
            argv.append("-i")
        if args.get("glob"):
            argv += ["--glob", str(args["glob"])]
        argv += ["--", str(args["pattern"]), target]
    else:
        argv = ["grep", "-rnE"] + (["-i"] if args.get("ignore_case") else [])
        if args.get("glob"):
            argv.append(f"--include={args['glob']}")
        argv += ["--", str(args["pattern"]), target]
    code, out = await _proc(argv, cwd=workdir, timeout=timeout)
    if code == 1 and not out.strip():
        return "no matches", False
    if code not in (0, 1):
        return out.strip() or f"search failed with exit code {code}", True
    lines = out.splitlines()
    tail = f"\n[{len(lines) - cap} more matches not shown]" if len(lines) > cap else ""
    return "\n".join(lines[:cap]) + tail, False


async def _glob(args: dict[str, Any], workdir: str) -> tuple[str, bool]:
    base = _resolve(args.get("path"), workdir)
    cap = min(max(int(args.get("max_results") or 200), 1), 2000)
    pattern = str(args["pattern"])
    found = []
    for match in base.glob(pattern):
        found.append(str(match))
        if len(found) > cap:
            break
    if not found and "/" not in pattern:
        found = [str(p) for p in base.rglob("*") if fnmatch.fnmatch(p.name, pattern)][: cap + 1]
    more = f"\n[more than {cap} matches; narrow the pattern]" if len(found) > cap else ""
    return ("\n".join(sorted(found[:cap])) or "no matches") + more, False


async def _list(args: dict[str, Any], workdir: str) -> tuple[str, bool]:
    path = _resolve(args.get("path"), workdir)
    if not path.is_dir():
        return f"not a directory: {path}", True
    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    shown = [p.name + ("/" if p.is_dir() else "") for p in entries[:500]]
    more = f"\n[{len(entries) - 500} more entries]" if len(entries) > 500 else ""
    return "\n".join(shown) + more or "[empty directory]", False


async def _bash(args: dict[str, Any], workdir: str, timeout: int) -> tuple[str, bool]:
    code, out = await _proc([str(args["command"])], cwd=workdir, timeout=timeout, shell=True)
    return f"{out.rstrip()}\n[exit code {code}]".lstrip(), code != 0


async def _declared(tool: DeclaredTool, args: dict[str, Any], workdir: str, package_root: str,
                    timeout: int) -> tuple[str, bool]:
    argv = [part.replace("{package}", package_root) for part in tool.command]
    stdin = json.dumps(args).encode()
    if tool.input == "args":
        argv += [f"--{key}={value}" for key, value in args.items()]
        stdin = b""
    code, out = await _proc(argv, cwd=workdir, timeout=tool.timeout_seconds or timeout, stdin=stdin)
    return out.rstrip() or f"[exit code {code}]", code != 0


async def run_sandbox_tool(name: str, args: dict[str, Any], *, workdir: str, timeout: int,
                           declared: dict[str, DeclaredTool], package_root: str) -> tuple[str, bool]:
    try:
        if name == "read":
            return await _read(args, workdir)
        if name == "grep":
            return await _grep(args, workdir, timeout)
        if name == "glob":
            return await _glob(args, workdir)
        if name == "list":
            return await _list(args, workdir)
        if name == "bash":
            return await _bash(args, workdir, timeout)
        if name in declared:
            return await _declared(declared[name], args, workdir, package_root, timeout)
    except (KeyError, ValueError, TypeError) as exc:
        return f"invalid arguments for {name}: {type(exc).__name__}", True
    except OSError as exc:
        return f"{name} failed: {exc.strerror or type(exc).__name__}", True
    return f"unknown tool {name}", True
