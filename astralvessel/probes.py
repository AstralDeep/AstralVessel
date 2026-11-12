# Isolation probes for a running deployment: each claim is checked from inside the containers, for every agent.
# Each script runs in one container and prints a JSON line per check; the host prints PASS, FAIL or SKIP.

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.parse
from dataclasses import dataclass, field

from .deployment import Deployment, size_bytes
from .stack import Stack

PRELUDE = r'''
import glob, hashlib, json, os, socket, subprocess, sys
CFG = json.loads(CFG_JSON)
def out(name, ok, skip=""):
    print(json.dumps({"name": name, "ok": bool(ok), "skip": skip}), flush=True)
def reach(host, port):
    try:
        socket.create_connection((host, port), 3).close()
        return True
    except OSError:
        return False
def refused(fn):
    try:
        fn()
    except OSError:
        return True
    return False
'''

SANDBOX = r'''
out("has no route to the internet", not reach("1.1.1.1", 443))
out("cannot resolve or reach the egress", not reach("egress", 8080))
out("the model host is known to the probes", bool(CFG["model_host"]))
if CFG["model_host"]:
    out("cannot reach the model host directly", not reach(CFG["model_host"], 443))
out("cannot reach the vessel API", not reach("vessel", 8700))
out("cannot reach the vessel's front address", not reach(CFG["front"], 8700))
for other in CFG["others"]:
    out(f"cannot reach {other}", not reach(other, 8710))
if CFG["search"]:
    out("cannot reach the search service", not reach("search", 8080))
dirs = ["/opt", "/home", "/tmp", "/agent", *CFG["mounts"]]
stores = [p for d in dirs for e in ("sqlite", "db") for p in glob.glob(d + "/**/*." + e, recursive=True)]
out("holds no task or conversation store", not stores)
keys = ("VESSEL_APP_KEY", "VESSEL_APP_KEY_SHA256", "VESSEL_MODEL_KEY", "LLM_API_KEY", "VESSEL_UPSTREAM_MODEL_KEY")
out("environment holds no model or app key", not any(k in os.environ for k in keys))
token = os.environ.get("VESSEL_SANDBOX_TOKEN", "")
out("holds its own token, not the master", bool(token) and hashlib.sha256(token.encode()).hexdigest() != CFG["master"])
out("root filesystem is read-only", refused(lambda: open("/opt/probe-write", "w")))
script = "/tmp/probe-noexec.sh"
with open(script, "w") as handle:
    handle.write("#!/bin/sh\necho ran\n")
os.chmod(script, 0o755)
out("scratch space is noexec", refused(lambda: subprocess.run([script], capture_output=True)))
os.unlink(script)
for target in CFG["mounts"]:
    out(f"{target} is mounted read-only", refused(lambda: open(os.path.join(target, ".probe-write"), "x")))
'''

SANDBOX_AFTER = r'''
def cmdline(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""
marks = [b"sleep\x00" + CFG["sleep"][0].encode(), b"sleep\x00" + CFG["sleep"][1].encode()]
strays = [p for p in glob.glob("/proc/[0-9]*/cmdline") if any(m in cmdline(p) for m in marks)]
out("a background process does not outlive its call", not strays)
left = [k for k in CFG["ended"] if os.path.exists("/tmp/vessel-calls/turn-" + k)]
out("a turn's workspace is gone once it ends", not left)
'''

CALLS = r'''
import asyncio
from astralvessel.deployment import sandbox_token
from astralvessel.sandbox import RemoteRunner
A, B, C, D, E, F = CFG["keys"]
AGENT = CFG["agent"]

async def main():
    r = RemoteRunner(os.environ["VESSEL_SANDBOX_URL_TEMPLATE"].format(id=AGENT),
                     sandbox_token(os.environ["VESSEL_SANDBOX_TOKEN"], AGENT), 30, CFG["types"])
    async def run(name, args, key=None, owner=""):
        return await r.run(name, args, key, owner=owner)

    s1, s2 = CFG["sleep"]
    await run("bash", {"command": f"setsid sleep {s1} >/dev/null 2>&1 & nohup sleep {s2} >/dev/null 2>&1 &"})
    await run("bash", {"command": "true"})

    holder = asyncio.create_task(run("bash", {"command": "echo private-note > $TMPDIR/note; sleep 4"}))
    await asyncio.sleep(1)
    got, _ = await run("bash", {"command": "cat /tmp/vessel-calls/*/note; ls /tmp/vessel-calls"})
    await holder
    out("a call cannot read another call's scratch", "private-note" not in got and "Permission denied" in got)

    got, _ = await run("bash", {"command": "ls /proc"})
    out("a call cannot list processes", "Permission denied" in got)
    got, failed = await run("bash", {"command": "python3 -c \"import socket; "
                                     "socket.create_connection(('127.0.0.1', 8710), 2)\""})
    out("a call cannot open TCP connections", failed and "PermissionError" in got)
    got, failed = await run("bash", {"command": "python3 -c \"import socket,os; "
                                     "socket.socket(socket.AF_UNIX).bind(os.environ['TMPDIR'] + '/s')\""})
    out("a call cannot create a socket file", failed and "PermissionError" in got)
    got, _ = await run("bash", {"command": "python3 -c \"import ctypes; l = ctypes.CDLL(None, use_errno=True); "
                                "print(l.shmget(0, 4096, 0o1600), ctypes.get_errno())\""})
    out("a call cannot use shared memory", got.split()[:2] == ["-1", "1"])
    got, failed = await run("bash", {"command": "python3 -c \"import socket; "
                                     "socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\""})
    out("a call cannot open UDP sockets", failed and "PermissionError" in got)
    _, failed = await run("glob", {"pattern": "../*"})
    got, _ = await run("glob", {"pattern": "*", "path": "/tmp"})
    out("glob cannot climb out of the agent's data", failed and "outside" in got)

    await run("bash", {"command": "echo kept-note > $TMPDIR/note"}, A)
    got, failed = await run("bash", {"command": "cat $TMPDIR/note"}, A)
    out("calls in one turn share its workspace", got.startswith("kept-note") and not failed)
    got, _ = await run("bash", {"command": f"cat /tmp/vessel-calls/turn-{A}/note; ls /tmp/vessel-calls"}, B)
    got2, failed = await run("read", {"path": f"/tmp/vessel-calls/turn-{A}/note"}, B)
    out("a call cannot read another turn's workspace",
        "kept-note" not in got and got.count("Permission denied") >= 2 and failed and "kept-note" not in got2)
    await r.end_turn(A)
    await r.end_turn(B)

    got, failed = await run("bash", {"command": "printf 'a,b\\n1,2\\n' > $VESSEL_OUTBOX/t.csv"}, C)
    files = r.take_files(C)
    await r.end_turn(C)
    if "csv" in CFG["types"]:
        out("a file a call writes to its outbox reaches the vessel",
            not failed and [(f.name, f.data) for f in files] == [("t.csv", b"a,b\n1,2\n")])
    else:
        out("a file type the agent does not allow is not attached", files == [] and "may not return" in got)
    cmd = ("head -c 5000000 /dev/zero | tr '\\0' a > $VESSEL_OUTBOX/big.csv; echo x > $VESSEL_OUTBOX/run.sh; "
           "ln -s /etc/passwd $VESSEL_OUTBOX/p.csv; printf x > $VESSEL_OUTBOX/fake.png")
    got, _ = await run("bash", {"command": cmd}, D)
    files = r.take_files(D)
    await r.end_turn(D)
    out("the outbox refuses other types, big files and links",
        files == [] and got.count("[not attached") == 4 and "root:" not in got)
    got, _ = await run("bash", {"command": "rmdir $VESSEL_OUTBOX && ln -s /etc $VESSEL_OUTBOX"}, E)
    files = r.take_files(E)
    await r.end_turn(E)
    out("an outbox swapped for a link exposes nothing", files == [] and "not a folder" in got)

    each = CFG["turn_bytes"] * 6 // 10
    if CFG["turn_bytes"] * 5 // 4 + (1 << 20) > CFG["scratch_bytes"]:
        out("a turn over its disk limit is stopped and emptied", True, "turn_disk is too close to scratch to test")
    else:
        cmd = f"head -c {each} /dev/zero > $TMPDIR/a; head -c {each} /dev/zero > $TMPDIR/b; sleep 3"
        got, failed = await run("bash", {"command": cmd}, F, owner="probe-user")
        left, _ = await run("bash", {"command": "ls $TMPDIR | wc -l"}, F, owner="probe-user")
        out("a turn over its disk limit is stopped and emptied",
            failed and "disk limit" in got and left.startswith("1"))
    await r.end_turn(F)

    got, failed = await run("bash", {"command": "true"}, "../../etc")
    out("the sandbox refuses a malformed workspace key", failed and "HTTP 400" in got)
    await r.close()

asyncio.run(main())
'''

VESSEL = r'''
import sqlite3, urllib.error, urllib.request
names = [row[0] for f in glob.glob("/data/*.sqlite")
         for row in sqlite3.connect(f).execute("select name from sqlite_master where type='table'")]
out("the vessel keeps no conversation store", bool(names) and all(n.startswith("tasks_") for n in names))
out("the vessel has no direct route to the internet", not reach("1.1.1.1", 443))
out("the vessel reaches the model through the egress", reach("egress", 8080))
def proxy(target):
    try:
        s = socket.create_connection(("egress", 3128), 5)
        s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
        line = s.recv(200).decode(errors="replace").split("\r\n")[0]
        s.close()
        return " 200" in line
    except OSError:
        return False
out("the egress proxy refuses the cloud metadata address", not proxy("169.254.169.254:443"))
out("the egress proxy refuses private addresses", not proxy("10.0.0.1:443"))
out("the egress proxy refuses Tailscale addresses", not proxy("100.100.100.100:443"))
out("the egress proxy refuses odd ports", not proxy("example.com:22"))
out("the egress proxy allows a public site on 443", proxy("example.com:443"))
if CFG["search"]:
    out("the vessel reaches the search service", reach("search", 8080))
request = urllib.request.Request("http://egress:8080/v1/models",
                                 headers={"Authorization": "Bearer " + os.environ["VESSEL_MODEL_KEY"]})
try:
    code = urllib.request.urlopen(request, timeout=10).status
except urllib.error.HTTPError as exc:
    code = exc.code
except OSError:
    code = 0
out("the vessel passes the egress with its token", code not in (0, 403))
'''

SEARCH = r'''
import urllib.error, urllib.request
out("search has no direct route to the internet", not reach("1.1.1.1", 443))
out("search cannot reach the vessel API", not reach(CFG["front"], 8700))
request = urllib.request.Request("http://egress:8080/v1/models", headers={"Authorization": "Bearer guess"})
try:
    code, body = urllib.request.urlopen(request, timeout=10).status, b""
except urllib.error.HTTPError as exc:
    code, body = exc.code, exc.read()
except OSError:
    code, body = 0, b""
out("search cannot use the model passthrough", code == 403 and b"egress token" in body)
'''

SEARCH_PYTHON = "/usr/local/searxng/.venv/bin/python"


@dataclass
class Report:
    passed: int = 0
    failed: list[str] = field(default_factory=list)
    skipped: int = 0

    def add(self, label: str, line: dict) -> None:
        name = f"{label}: {line['name']}"
        if line.get("skip"):
            self.skipped += 1
            print(f"SKIP  {name} ({line['skip']})")
        elif line.get("ok"):
            self.passed += 1
            print(f"PASS  {name}")
        else:
            self.failed.append(name)
            print(f"FAIL  {name}")


def script(body: str, config: dict) -> str:
    return f"CFG_JSON = {json.dumps(json.dumps(config))}\n" + PRELUDE + body


def _run(stack: Stack, report: Report, label: str, service: str, body: str, config: dict,
         interpreter: str = "python") -> None:
    done = stack.python(service, script(body, config), interpreter)
    lines = []
    for raw in done.stdout.splitlines():
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    for line in lines:
        report.add(label, line)
    if done.returncode != 0 or not lines:
        # A script that died part way is a failure even if every check it reached passed.
        report.add(label, {"name": "probe script finished", "ok": False})


def model_host(stack: Stack) -> str:
    done = stack.python("egress", "import os; print(os.environ.get('LLM_BASE_URL', ''))")
    return urllib.parse.urlsplit(done.stdout.strip()).hostname or ""


def run_probes(stack: Stack, deployment: Deployment, master: str) -> Report:
    report = Report()
    search = deployment.search.provider == "searxng"
    front = deployment.vessel.front_address
    host = model_host(stack)
    ids = list(deployment.agents)
    sleeps = [str(6000 + secrets.randbelow(999)), str(7000 + secrets.randbelow(999))]
    for agent_id, agent in deployment.agents.items():
        service = f"sandbox-{agent_id}"
        keys = [secrets.token_hex(16) for _ in range(6)]
        mounts = [m.target for m in agent.profile.mounts]
        _run(stack, report, service, service, SANDBOX, {
            "model_host": host, "front": front, "search": search, "mounts": mounts,
            "others": [f"sandbox-{other}" for other in ids if other != agent_id],
            "master": hashlib.sha256(master.encode()).hexdigest()})
        _run(stack, report, f"{agent_id} calls", "vessel", CALLS, {
            "agent": agent_id, "keys": keys, "sleep": sleeps, "types": agent.package.manifest.output.files,
            "turn_bytes": agent.profile.turn_bytes, "scratch_bytes": size_bytes(agent.profile.scratch)})
        _run(stack, report, service, service, SANDBOX_AFTER, {"sleep": sleeps, "ended": keys[:2]})
    _run(stack, report, "vessel", "vessel", VESSEL, {"search": search})
    if search:
        _run(stack, report, "search", "search", SEARCH, {"front": front}, SEARCH_PYTHON)
    return report

