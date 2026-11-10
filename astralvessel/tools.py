# Tool definitions shown to the model and the file and shell tools that run inside the sandbox.
# run_tool executes one call with time and output limits and never raises for tool-level failures.

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import confine as landlock
from .manifest import DeclaredTool

SANDBOX_TOOLS = ("read", "grep", "glob", "list", "bash")
WEB_TOOLS = ("webfetch", "websearch")
SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
            "HOME": "/tmp", "TMPDIR": "/tmp"}
# Session ids of tool processes still running; the sandbox server kills every other process it finds.
ACTIVE_SESSIONS: set[int] = set()
SPAWNING = [0]
MAX_OUTPUT_BYTES = 1_000_000
MAX_LINE_CHARS = 4000


@dataclass(frozen=True)
class Confinement:
    # Each call gets a fresh scratch directory under scratch_root and may read only these roots (plus the system).
    readable: tuple[str, ...]
    scratch_root: str

    def allows(self, path: Path) -> bool:
        real = os.path.realpath(path)
        return any(real == r or real.startswith(r.rstrip("/") + "/") for r in map(os.path.realpath, self.readable))

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


async def _spawn(argv: list[str], *, cwd: str, env: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)


async def _proc(argv: list[str], *, cwd: str, timeout: int, shell: bool = False,
                stdin: bytes | None = None, confinement: Confinement | None = None) -> tuple[int, str]:
    if shell:
        argv = ["/bin/sh", "-c", argv[0]]
    env, scratch = SAFE_ENV, None
    if confinement is not None:
        scratch = tempfile.mkdtemp(prefix="call-", dir=confinement.scratch_root)
        env = {**SAFE_ENV, "HOME": scratch, "TMPDIR": scratch}
        flags = [f for root in confinement.readable for f in ("--read", root)]
        argv = [sys.executable, "-I", "-S", landlock.__file__, "--scratch", scratch, *flags, "--", *argv]
    try:
        return await _supervise(argv, cwd=cwd, env=env, timeout=timeout, stdin=stdin)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


async def _supervise(argv: list[str], *, cwd: str, env: dict[str, str], timeout: int,
                     stdin: bytes | None) -> tuple[int, str]:
    SPAWNING[0] += 1
    try:
        proc = await _spawn(argv, cwd=cwd, env=env)
    finally:
        SPAWNING[0] -= 1
    ACTIVE_SESSIONS.add(proc.pid)
    try:
        out = await asyncio.wait_for(_collect(proc, stdin), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 124, f"[timed out after {timeout} s]"
    finally:
        ACTIVE_SESSIONS.discard(proc.pid)
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def _collect(proc: asyncio.subprocess.Process, stdin: bytes | None) -> bytes:
    # Keep at most MAX_OUTPUT_BYTES; a command that prints more is stopped, so a flood cannot exhaust memory.
    if stdin:
        proc.stdin.write(stdin)
        with contextlib.suppress(ConnectionError):
            await proc.stdin.drain()
    proc.stdin.close()
    chunks, size = [], 0
    while chunk := await proc.stdout.read(65536):
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_OUTPUT_BYTES:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 9)
            chunks.append(b"\n[output stopped at %d bytes]" % MAX_OUTPUT_BYTES)
            break
    await proc.wait()
    return b"".join(chunks)


async def _read(args: dict[str, Any], workdir: str) -> tuple[str, bool]:
    path = _resolve(args.get("path"), workdir)
    if not path.is_file():
        return f"not a file: {path}", True
    offset = max(int(args.get("offset") or 1), 1)
    limit = min(max(int(args.get("limit") or 400), 1), 2000)

    def scan() -> list[str]:
        lines = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            number = 0
            # readline with a size bound keeps one enormous line from filling memory.
            while line := handle.readline(MAX_LINE_CHARS):
                if not line.endswith("\n") and len(line) == MAX_LINE_CHARS:
                    while (rest := handle.readline(MAX_LINE_CHARS)) and not rest.endswith("\n"):
                        pass  # skip the rest of the over-long line in bounded pieces
                    line += " [line truncated]"
                number += 1
                if number < offset:
                    continue
                if number >= offset + limit:
                    lines.append(f"[more lines follow; continue with offset={number}]")
                    break
                lines.append(f"{number:>6}\t{line.rstrip()}")
        return lines

    return "\n".join(await asyncio.to_thread(scan)) or "[empty]", False


async def _grep(args: dict[str, Any], workdir: str, timeout: int,
                confinement: Confinement | None) -> tuple[str, bool]:
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
    code, out = await _proc(argv, cwd=workdir, timeout=timeout, confinement=confinement)
    if code == 1 and not out.strip():
        return "no matches", False
    if code not in (0, 1):
        return out.strip() or f"search failed with exit code {code}", True
    lines = out.splitlines()
    tail = f"\n[{len(lines) - cap} more matches not shown]" if len(lines) > cap else ""
    return "\n".join(lines[:cap]) + tail, False


async def _glob(args: dict[str, Any], workdir: str, timeout: int) -> tuple[str, bool]:
    base = _resolve(args.get("path"), workdir)
    cap = min(max(int(args.get("max_results") or 200), 1), 2000)
    pattern = str(args["pattern"])
    if pattern.startswith("/"):
        return "the pattern must be relative; pass the directory as path", True
    # The walk runs in a thread with a time budget so a huge tree cannot stall every other call.
    deadline = time.monotonic() + timeout

    def walk() -> tuple[list[str], bool]:
        found: list[str] = []
        # A bare name pattern such as "*.md" searches the whole tree.
        for match in base.rglob(pattern) if "/" not in pattern else base.glob(pattern):
            if time.monotonic() > deadline:
                return found, True
            found.append(str(match))
            if len(found) > cap:
                break
        return found, False

    found, timed_out = await asyncio.to_thread(walk)
    more = f"\n[more than {cap} matches; narrow the pattern]" if len(found) > cap else ""
    if timed_out:
        more += f"\n[stopped after {timeout} s; narrow the pattern or path]"
    return ("\n".join(sorted(found[:cap])) or "no matches") + more, False


async def _list(args: dict[str, Any], workdir: str) -> tuple[str, bool]:
    path = _resolve(args.get("path"), workdir)
    if not path.is_dir():
        return f"not a directory: {path}", True
    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    shown = [p.name + ("/" if p.is_dir() else "") for p in entries[:500]]
    more = f"\n[{len(entries) - 500} more entries]" if len(entries) > 500 else ""
    return "\n".join(shown) + more or "[empty directory]", False


async def _bash(args: dict[str, Any], workdir: str, timeout: int,
                confinement: Confinement | None) -> tuple[str, bool]:
    code, out = await _proc([str(args["command"])], cwd=workdir, timeout=timeout, shell=True,
                            confinement=confinement)
    return f"{out.rstrip()}\n[exit code {code}]".lstrip(), code != 0


async def _declared(tool: DeclaredTool, args: dict[str, Any], workdir: str, package_root: str,
                    timeout: int, confinement: Confinement | None) -> tuple[str, bool]:
    argv = [part.replace("{package}", package_root) for part in tool.command]
    stdin = json.dumps(args).encode()
    if tool.input == "args":
        # Only the declared parameters become flags, so the model cannot add flags of its own.
        allowed = set((tool.parameters or {}).get("properties") or {})
        unknown = sorted(k for k in args if k not in allowed)
        if unknown:
            return f"unknown arguments for {tool.name}: {', '.join(unknown)[:200]}", True
        argv += [f"--{key}={value}" for key, value in args.items()]
        stdin = b""
    code, out = await _proc(argv, cwd=workdir, timeout=tool.timeout_seconds or timeout, stdin=stdin,
                            confinement=confinement)
    return out.rstrip() or f"[exit code {code}]", code != 0


async def run_sandbox_tool(name: str, args: dict[str, Any], *, workdir: str, timeout: int,
                           declared: dict[str, DeclaredTool], package_root: str,
                           confinement: Confinement | None = None) -> tuple[str, bool]:
    try:
        if name in ("read", "glob", "list") and confinement is not None:
            # These run inside the sandbox server itself, so they get the same path limits a confined call has.
            target = _resolve(args.get("path"), workdir)
            if not confinement.allows(target):
                return f"{name}: {target} is outside the agent's data", True
        if name == "read":
            return await _read(args, workdir)
        if name == "grep":
            return await _grep(args, workdir, timeout, confinement)
        if name == "glob":
            return await _glob(args, workdir, timeout)
        if name == "list":
            return await _list(args, workdir)
        if name == "bash":
            return await _bash(args, workdir, timeout, confinement)
        if name in declared:
            return await _declared(declared[name], args, workdir, package_root, timeout, confinement)
    except (KeyError, ValueError, TypeError) as exc:
        return f"invalid arguments for {name}: {type(exc).__name__}", True
    except OSError as exc:
        return f"{name} failed: {exc.strerror or type(exc).__name__}", True
    except Exception:  # noqa: BLE001  any other failure becomes a generic tool error, never raw text
        return f"{name} failed", True
    return f"unknown tool {name}", True
