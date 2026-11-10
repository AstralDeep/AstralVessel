# Agent loop tests with a scripted model: tool round trips, unknown tools, limits, cancellation, errors, present.

from __future__ import annotations

import asyncio

from conftest import text_reply, tool_reply

from astralvessel.agent import Turn
from astralvessel.manifest import load_package
from astralvessel.model import ModelClient
from astralvessel.sandbox import LocalRunner


def make_turn(package_dir, model):
    package = load_package(package_dir)
    events = []

    async def emit(item):
        events.append(item)

    client = ModelClient("http://model.test/v1", "k", "test-model", transport=model.transport(), retries=1)
    return Turn(package, client, LocalRunner(package), emit), events


async def run(turn, text="question"):
    return await turn.run([], text, conversation_id="c", turn_id="t")


async def test_tool_round_trip_and_event_order(package_dir, model):
    model.replies = [tool_reply("read", {"path": "wiki/person-ada-lovelace.md"}), text_reply("Ada built it.")]
    turn, events = make_turn(package_dir, model)
    result = await run(turn)
    assert result["status"] == "ok" and result["text"] == "Ada built it." and result["steps"] == 2
    kinds = [e["type"] for e in events]
    assert kinds[0] == "turn.started" and kinds[-1] == "turn.finished" and kinds.count("turn.finished") == 1
    steps = [i for i, k in enumerate(kinds) if k == "step.finished"]
    assert steps[0] < kinds.index("tool.started") < kinds.index("tool.finished") < steps[1]
    finished = next(e for e in events if e["type"] == "tool.finished")
    assert not finished["error"] and "Analytical engine" in finished["output"]
    assert result["tokens"] == {"input": 30, "output": 13}
    second = model.requests[1]["messages"]
    assert second[-1]["role"] == "tool" and "Analytical engine" in second[-1]["content"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "read"


async def test_unlisted_tool_does_not_exist(package_dir, model):
    model.replies = [tool_reply("webfetch", {"url": "http://169.254.169.254/"}), text_reply("ok")]
    turn, events = make_turn(package_dir, model)
    await run(turn)
    finished = next(e for e in events if e["type"] == "tool.finished")
    assert finished["error"] and "does not exist" in finished["output"]
    assert "webfetch" not in [t["function"]["name"] for t in model.requests[0]["tools"]]


async def test_bad_arguments_are_reported_not_raised(package_dir, model):
    model.replies = [tool_reply("read", "{not json"), text_reply("ok")]
    turn, events = make_turn(package_dir, model)
    result = await run(turn)
    assert result["status"] == "ok"
    assert "not a valid JSON object" in next(e for e in events if e["type"] == "tool.finished")["output"]


async def test_step_limit_forces_a_final_answer(package_dir, model):
    model.replies = [tool_reply("list", {}, f"call_{i}") for i in range(4)] + [text_reply("best effort")]
    turn, _ = make_turn(package_dir, model)
    result = await run(turn)
    assert result["status"] == "step_limit" and result["text"] == "best effort"
    assert model.requests[-1].get("tools") is None


async def test_cancel_during_a_tool_closes_the_history(package_dir, model):
    model.replies = [tool_reply("bash", {"command": "sleep 10"}), text_reply("never")]
    turn, events = make_turn(package_dir, model)
    task = asyncio.create_task(run(turn))
    for _ in range(100):
        if any(e["type"] == "tool.started" for e in events):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    result = await task
    assert result["status"] == "cancelled"
    assert events[-1]["type"] == "turn.finished" and events[-1]["status"] == "cancelled"
    roles = [m["role"] for m in turn.new_messages]
    assert roles[-2:] == ["tool", "assistant"]


async def test_cancel_before_the_model_answers(package_dir, model):
    model.delay = 5
    turn, events = make_turn(package_dir, model)
    task = asyncio.create_task(run(turn))
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task
    assert result["status"] == "cancelled" and result["steps"] == 1
    assert turn.new_messages[-1]["content"].startswith("[this turn ended early")


async def test_model_errors_are_generic(package_dir, model):
    model.replies = [500, 500]
    turn, _ = make_turn(package_dir, model)
    result = await run(turn)
    assert result["status"] == "model_error"
    assert "secret-dsn" not in result["error"] and "HTTP 500" in result["error"]


async def test_deadline(package_dir, model, monkeypatch):
    model.delay = 3
    turn, _ = make_turn(package_dir, model)
    monkeypatch.setattr(turn.package.manifest.sandbox, "deadline_seconds", 1)
    result = await run(turn)
    assert result["status"] == "deadline"


async def test_present_attaches_validated_components(package_dir, model):
    bad = {"components": [{"type": "table", "headers": "oops"}]}
    good = {"components": [{"type": "metric", "title": "Pages", "value": "1"}]}
    model.replies = [tool_reply("present", bad, "c1"), tool_reply("present", good, "c2"), text_reply("Done.")]
    turn, events = make_turn(package_dir, model)
    result = await run(turn)
    outputs = [e for e in events if e["type"] == "tool.finished"]
    assert outputs[0]["error"] and "not attached" in outputs[0]["output"]
    assert result["components"][0]["type"] == "metric"


async def test_null_tool_output_fields_do_not_break_the_loop(package_dir, model):
    model.replies = [tool_reply("grep", {"pattern": "Ada", "glob": None, "max_results": None}), text_reply("ok")]
    turn, events = make_turn(package_dir, model)
    result = await run(turn)
    assert result["status"] == "ok"
    assert not next(e for e in events if e["type"] == "tool.finished")["error"]
