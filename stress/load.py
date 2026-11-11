# Load test for a vessel: many concurrent users, long histories, cancellations and large tool output.
# In-process mode runs a real vessel with a fake streaming model and real per-call confinement; live mode targets a
# running vessel with its real model. Usage: python stress/load.py [--users N] [--turns N] [--live URL KEY AGENT]

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import resource
import socket
import statistics
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from astralvessel.client import VesselClient, transcript_of  # noqa: E402

KEY = "stress-app-key"


def sse(chunks):
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


class FakeModel:
    # Each turn: one tool call (a shell command over the data), then an answer streamed in many small deltas.
    def __init__(self, deltas: int, delay: float):
        self.deltas, self.delay, self.calls = deltas, delay, 0

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            body = json.loads(request.content)
            await asyncio.sleep(self.delay)
            if body["messages"][-1]["role"] == "user":
                command = os.environ.get("STRESS_COMMAND", "grep -ri heron . | head -5; seq 1 2000 | wc -l")
                args = json.dumps({"command": command})
                chunks = [{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": f"c{self.calls}",
                           "type": "function", "function": {"name": "bash", "arguments": args}}]},
                           "finish_reason": "tool_calls"}]}]
            else:
                chunks = [{"choices": [{"index": 0, "delta": {"content": "word "}}]} for _ in range(self.deltas)]
                chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            chunks.append({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": self.deltas}})
            return httpx.Response(200, content=sse(chunks), headers={"content-type": "text/event-stream"})
        return httpx.MockTransport(handler)


async def serve_in_process(args, workdir: Path):
    import uvicorn

    from astralvessel.deployment import load_deployment
    from astralvessel.manifest import load_package
    from astralvessel.sandbox import LocalRunner, confinement_for
    from astralvessel.server import Settings, app_keys, create_app

    data = workdir / "data"
    data.mkdir()
    for i in range(200):
        (data / f"page-{i}.md").write_text(f"# Page {i}\nA heron note {i}.\n" * 20)
    package = workdir / "agent"
    package.mkdir()
    (package / "prompt.md").write_text("You are a stress-test agent.")
    (package / "vessel.toml").write_text(f"""
[agent]
name = "stress"
description = "Stress test agent"
[model]
endpoint = "http://model.test/v1"
api_key = "k"
model = "fake"
max_steps = 6
[tools]
builtin = ["bash", "read", "grep"]
[sandbox]
workdir = "{data}"
deadline_seconds = 120
tool_timeout_seconds = 30
""")
    (workdir / "deployment.toml").write_text(f"""
[egress]
upstream_url = "http://model.test/v1"
[[agents]]
id = "stress"
package = "{package}"
[[apps]]
name = "load"
key_sha256 = "{hashlib.sha256(KEY.encode()).hexdigest()}"
agents = ["stress"]
""")
    deployment = load_deployment(workdir / "deployment.toml")
    scratch = workdir / "calls"
    scratch.mkdir()
    runner = LocalRunner(load_package(package), confinement_for(load_package(package), str(scratch)))
    settings = Settings(deployment, task_db=str(workdir / "tasks.sqlite"), app_keys=app_keys(deployment))
    model = FakeModel(args.deltas, args.model_delay)
    app = create_app(settings, runners={"stress": runner}, model_transport=model.transport())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return f"http://127.0.0.1:{port}", KEY, "stress", server, task, scratch


async def one_user(client, agent, user, turns, results, history_messages):
    history = list(history_messages)
    for n in range(turns):
        started = time.monotonic()
        try:
            events = [e async for e in client.stream(agent, user, f"Turn {n}: find herons.", history=history)]
        except Exception as exc:  # noqa: BLE001
            results.append(("exception", time.monotonic() - started, type(exc).__name__))
            continue
        last = events[-1] if events else {}
        messages, _ = transcript_of(events)
        ok = last.get("type") == "turn.finished" and last.get("status") == "ok" and messages
        results.append(("ok" if ok else f"bad:{last.get('state') or last.get('type')}:{str(last)[:120]}",
                        time.monotonic() - started, ""))
        history += messages


async def cancel_storm(client, agent, count, results):
    async def one(i):
        events, task_id = [], None
        gen = client.stream(agent, f"canceller-{i}", "Start something then get cancelled.")
        async for e in gen:
            events.append(e)
            if e.get("type") == "task":
                task_id = e["task_id"]
            if e.get("type") in ("tool.started", "text.delta") and task_id:
                await client.cancel(agent, f"canceller-{i}", task_id)
        last = events[-1] if events else {}
        results.append("ok" if last.get("type") == "turn.finished" else f"bad:{last}")
    await asyncio.gather(*(one(i) for i in range(count)))


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0


async def main(args):
    with tempfile.TemporaryDirectory(dir=args.workdir) as tmp:
        if args.live:
            base, key, agent = args.live
            server = task = scratch = None
        else:
            base, key, agent, server, task, scratch = await serve_in_process(args, Path(tmp))
        client = VesselClient(base, key)
        long_history = []
        if args.history:
            for i in range(args.history // 2):
                long_history += [{"role": "user", "content": f"Earlier question {i} " + "x" * args.history_chars},
                                 {"role": "assistant", "content": f"Earlier answer {i} " + "y" * args.history_chars}]
        results: list[tuple[str, float, str]] = []
        started = time.monotonic()
        await asyncio.gather(*(one_user(client, agent, f"user-{u}", args.turns, results, long_history)
                               for u in range(args.users)))
        wall = time.monotonic() - started
        cancels: list[str] = []
        if args.cancels:
            await cancel_storm(client, agent, args.cancels, cancels)
        latencies = [r[1] for r in results if r[0] == "ok"]
        failures = [r for r in results if r[0] != "ok"]
        report = {
            "mode": "live" if args.live else "in-process",
            "users": args.users, "turns_each": args.turns, "history_messages": len(long_history),
            "turns_ok": len(latencies), "turns_failed": len(failures),
            "failure_kinds": sorted({f[0].split(":")[0] + ":" + f[0].split(":")[1] if ":" in f[0] else f[0]
                                     for f in failures})[:5],
            "first_failure": failures[0][0][:300] if failures else None,
            "wall_seconds": round(wall, 1), "turns_per_second": round(len(latencies) / wall, 2) if wall else 0,
            "latency_p50_s": round(statistics.median(latencies), 2) if latencies else None,
            "latency_p95_s": round(pct(latencies, 0.95), 2), "latency_max_s": round(max(latencies, default=0), 2),
            "cancels_ok": cancels.count("ok"), "cancels_failed": len(cancels) - cancels.count("ok"),
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        }
        if not args.live:
            report["leftover_scratch_dirs"] = len(list(scratch.iterdir()))
            report["task_db_mb"] = round(os.path.getsize(Path(tmp) / "tasks.sqlite") / 1e6, 1)
        await client.close()
        if server:
            server.should_exit = True
            await task
        print(json.dumps(report, indent=1))
        return 1 if failures or report.get("leftover_scratch_dirs") or report["cancels_failed"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--deltas", type=int, default=200, help="streamed answer chunks per turn (fake model)")
    parser.add_argument("--model-delay", type=float, default=0.05)
    parser.add_argument("--history", type=int, default=0, help="messages of prior history sent with each turn")
    parser.add_argument("--history-chars", type=int, default=500)
    parser.add_argument("--cancels", type=int, default=0)
    parser.add_argument("--workdir", default=None, help="where temporary data goes (default: system temp)")
    parser.add_argument("--live", nargs=3, metavar=("URL", "KEY", "AGENT"))
    sys.exit(asyncio.run(main(parser.parse_args())))
