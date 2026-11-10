#!/usr/bin/env bash
# Probes a running deployment from inside its containers and prints PASS or FAIL for each isolation claim.
# Run from deploy/ after `astralvessel deploy` and `docker compose ... up -d` (see the README).
set -u
pass=0; fail=0
dc() { docker compose -f compose.generated.yml -f compose.demo.yml --env-file .env "$@"; }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $name"; pass=$((pass+1)); else echo "FAIL  $name"; fail=$((fail+1)); fi; }
py() { local svc="$1"; shift; dc exec -T "$svc" python -c "$@"; }

CONNECT='import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2])))==0 else 1)'
reach() { dc exec -T "$1" python -c "$CONNECT" "$2" "$3"; }
not_reach() { ! reach "$@"; }
SB=sandbox-nexus

check "sandbox has no route to the internet"            not_reach $SB 1.1.1.1 443
check "sandbox cannot resolve or reach egress"          not_reach $SB egress 8080
check "sandbox cannot reach the model host directly"    not_reach $SB api-llm-factory.ai.uky.edu 443
check "sandbox cannot reach the harness API"            not_reach $SB vessel 8700
check "sandbox cannot reach the harness's front address" not_reach $SB 172.31.247.10 8700
check "one agent's sandbox cannot reach another's"      not_reach $SB sandbox-vault-reader 8710
check "sandbox holds no task or conversation store"     py $SB 'import glob,sys; sys.exit(1 if glob.glob("/data/*") or [p for d in ("/opt","/home","/tmp","/agent") for p in glob.glob(d + "/**/*.sqlite", recursive=True)] else 0)'
check "sandbox environment holds no model or app key"   py $SB 'import os,sys; sys.exit(1 if any(k in os.environ for k in ("VESSEL_APP_KEY","VESSEL_APP_KEY_SHA256","VESSEL_MODEL_KEY","LLM_API_KEY")) else 0)'
check "sandbox holds its own token, not the master"     py $SB "import os,sys; sys.exit(1 if os.environ['VESSEL_SANDBOX_TOKEN'] == '$(. ./.env; echo "$VESSEL_SANDBOX_TOKEN")' else 0)"
check "sandbox root filesystem is read-only"            py $SB 'import sys
try:
    open("/opt/x","w"); sys.exit(1)
except OSError: sys.exit(0)'
check "sandbox scratch space is noexec"                 dc exec -T $SB sh -c '! (printf "#!/bin/sh\necho ran" > /tmp/x.sh && chmod +x /tmp/x.sh && /tmp/x.sh)'
check "vault is mounted read-only"                      py $SB 'import sys
try:
    open("/vault/index.md","a"); sys.exit(1)
except OSError: sys.exit(0)'

CALL='import asyncio,os,sys
from astralvessel.deployment import sandbox_token
from astralvessel.sandbox import RemoteRunner
async def go():
    r = RemoteRunner(os.environ["VESSEL_SANDBOX_URL_TEMPLATE"].format(id="nexus"),
                     sandbox_token(os.environ["VESSEL_SANDBOX_TOKEN"], "nexus"), 30)
    sys.exit(0 if await PROBE(r) else 1)
'
probe() { py vessel "${CALL/PROBE/probe}
async def probe(r):
$1
asyncio.run(go())"; }
no_strays() {
  probe '
    await r.run("bash", {"command": "setsid sleep 600 >/dev/null 2>&1 & nohup sleep 601 >/dev/null 2>&1 &"})
    await r.run("bash", {"command": "true"})
    return True' || return 1
  # Calls cannot list processes, so look from the sandbox container itself.
  py $SB 'import glob,sys; sys.exit(1 if any(b"sleep\x0060" in open(p,"rb").read() for p in glob.glob("/proc/[0-9]*/cmdline")) else 0)'
}
check "a background process does not outlive its call" no_strays
check "a call cannot read another call's scratch"      probe '
    holder = asyncio.create_task(r.run("bash", {"command": "echo private-note > $TMPDIR/note; sleep 4"}))
    await asyncio.sleep(1)
    out, _ = await r.run("bash", {"command": "cat /tmp/vessel-calls/*/note; ls /tmp/vessel-calls"})
    await holder
    return "private-note" not in out and "Permission denied" in out'
check "a call cannot list processes"                   probe '
    out, _ = await r.run("bash", {"command": "ls /proc"})
    return "Permission denied" in out'
check "a call cannot open TCP connections"             probe '
    out, failed = await r.run("bash", {"command": "python3 -c \"import socket; socket.create_connection((\x27127.0.0.1\x27, 8710), 2)\""})
    return failed and "PermissionError" in out'
check "the vessel keeps no conversation store"         py vessel 'import glob,sqlite3,sys
names = [r[0] for f in glob.glob("/data/*.sqlite") for r in sqlite3.connect(f).execute("select name from sqlite_master where type=\"table\"")]
sys.exit(0 if names and all(n.startswith("tasks_") for n in names) else 1)'
check "harness has no direct route to the internet"     not_reach vessel 1.1.1.1 443
check "harness reaches the model through egress"        reach vessel egress 8080
PROXY='import socket,sys; s=socket.create_connection(("egress",3128),5); s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (sys.argv[1],sys.argv[1])).encode()); r=s.recv(200).decode(); print(r.split("\r\n")[0]); sys.exit(0 if " 200" in r.split("\r\n")[0] else 1)'
proxy() { dc exec -T vessel python -c "$PROXY" "$1"; }
refused() { ! proxy "$1"; }
check "egress proxy refuses the cloud metadata address" refused 169.254.169.254:443
check "egress proxy refuses private addresses"          refused 10.0.0.1:443
check "egress proxy refuses Tailscale addresses"        refused 100.100.100.100:443
check "egress proxy refuses odd ports"                  refused example.com:22
check "egress proxy allows a public site on 443"        proxy example.com:443
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
