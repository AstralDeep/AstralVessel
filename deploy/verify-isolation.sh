#!/usr/bin/env bash
# Probes a running deployment from inside its containers and prints PASS or FAIL for each isolation claim.
# Run from deploy/ after `docker compose up -d`.
set -u
pass=0; fail=0
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $name"; pass=$((pass+1)); else echo "FAIL  $name"; fail=$((fail+1)); fi; }
py() { local svc="$1"; shift; docker compose exec -T "$svc" python -c "$1"; }

CONNECT='import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2])))==0 else 1)'
reach() { docker compose exec -T "$1" python -c "$CONNECT" "$2" "$3"; }
not_reach() { ! reach "$@"; }

check "sandbox has no route to the internet"            not_reach sandbox 1.1.1.1 443
check "sandbox cannot resolve or reach egress"          not_reach sandbox egress 8080
check "sandbox cannot reach the model host directly"    not_reach sandbox api-llm-factory.ai.uky.edu 443
check "sandbox has no session store"                    py sandbox 'import glob,sys; sys.exit(1 if glob.glob("/data/*") or [p for d in ("/opt","/home","/tmp","/agent","/data") for p in glob.glob(d + "/**/*.sqlite", recursive=True)] else 0)'
check "sandbox environment holds no model or app key"   py sandbox 'import os,sys; sys.exit(1 if any(k in os.environ for k in ("VESSEL_APP_KEY","VESSEL_APP_KEY_SHA256","VESSEL_MODEL_KEY","LLM_API_KEY")) else 0)'
check "sandbox root filesystem is read-only"            py sandbox 'import sys
try:
    open("/opt/x","w"); sys.exit(1)
except OSError: sys.exit(0)'
check "sandbox scratch space is noexec"                 docker compose exec -T sandbox sh -c '! (printf "#!/bin/sh\necho ran" > /tmp/x.sh && chmod +x /tmp/x.sh && /tmp/x.sh)'
check "vault is mounted read-only"                      py sandbox 'import sys
try:
    open("/vault/index.md","a"); sys.exit(1)
except OSError: sys.exit(0)'
check "sandbox cannot reach the harness API"           not_reach sandbox vessel 8700
check "sandbox cannot reach the harness's front address" not_reach sandbox 172.31.247.10 8700
STRAY='import asyncio,os,sys
from astralvessel.sandbox import RemoteRunner
async def go():
    r = RemoteRunner(os.environ["VESSEL_SANDBOX_URL"], os.environ["VESSEL_SANDBOX_TOKEN"], 30)
    await r.run("bash", {"command": "setsid sleep 600 >/dev/null 2>&1 & nohup sleep 601 >/dev/null 2>&1 &"})
    out, _ = await r.run("bash", {"command": "ps -eo args 2>/dev/null || cat /proc/[0-9]*/cmdline | tr \"\\0\" \" \""})
    sys.exit(1 if "sleep 60" in out else 0)
asyncio.run(go())'
check "a background process does not outlive its call"  py vessel "$STRAY"
check "harness has no direct route to the internet"     not_reach vessel 1.1.1.1 443
check "harness reaches the model through egress"        reach vessel egress 8080
PROXY='import socket,sys; s=socket.create_connection(("egress",3128),5); s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (sys.argv[1],sys.argv[1])).encode()); r=s.recv(200).decode(); print(r.split("\r\n")[0]); sys.exit(0 if " 200" in r.split("\r\n")[0] else 1)'
proxy() { docker compose exec -T vessel python -c "$PROXY" "$1"; }
refused() { ! proxy "$1"; }
check "egress proxy refuses the cloud metadata address" refused 169.254.169.254:443
check "egress proxy refuses private addresses"          refused 10.0.0.1:443
check "egress proxy refuses Tailscale addresses"        refused 100.100.100.100:443
check "egress proxy refuses odd ports"                  refused example.com:22
check "egress proxy allows a public site on 443"        proxy example.com:443
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
