# Agent loop tests with a scripted model: tool round trips, unknown tools, limits, cancellation, errors, present.

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from conftest import text_reply, tool_reply

from astralvessel.agent import Turn
from astralvessel.manifest import load_package
from astralvessel.model import ModelClient, ModelError
from astralvessel.sandbox import LocalRunner
from astralvessel.transcript import for_model


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
    model.replies = [tool_reply("read", {"path": "notes/ada-lovelace.md"}), text_reply("Ada built it.")]
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
    model.replies = [tool_reply("read", '{"path": notes/ada-lovelace.md}'),
                     tool_reply("read", {"path": "notes/ada-lovelace.md"}, "call_2"), text_reply("ok")]
    turn, events = make_turn(package_dir, model)
    result = await run(turn)
    assert result["status"] == "ok" and result["steps"] == 3
    assert "Analytical engine" in [e for e in events if e["type"] == "tool.finished"][1]["output"]
    assert "not a valid JSON object" in next(e for e in events if e["type"] == "tool.finished")["output"]
    # Model endpoints refuse a request that replays the broken arguments, so the replay carries "{}".
    replayed = model.requests[1]["messages"][-2]
    assert replayed["tool_calls"][0]["function"]["arguments"] == "{}"
    assert "not a valid JSON object" in model.requests[1]["messages"][-1]["content"]
    assert turn.new_messages[1]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_history_with_broken_arguments_is_replayable():
    def call(arguments):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "read", "arguments": arguments}}]}
    broken = ['{"names": Ada, Grace}', "[1, 2]", '{"x": NaN}', " ", "[" * 100000]
    history = [call(a) for a in broken] + [call('{"path": "a.md"}')]
    sent = for_model(history, send_reasoning=False)
    assert [m["tool_calls"][0]["function"]["arguments"] for m in sent] == ["{}"] * 5 + ['{"path": "a.md"}']
    assert history[0]["tool_calls"][0]["function"]["arguments"] == '{"names": Ada, Grace}'


async def test_a_refused_model_request_is_logged_for_the_operator(caplog):
    def handler(request):
        return httpx.Response(400, text='{"error": "arguments is not JSON"}\nFORGED log line')

    client = ModelClient("http://model.test/v1", "k", "test-model", transport=httpx.MockTransport(handler), retries=0)
    with caplog.at_level(logging.WARNING, logger="astralvessel"), pytest.raises(ModelError) as raised:
        async for _ in client.stream([{"role": "user", "content": "hi"}], []):
            pass
    assert str(raised.value) == "model endpoint returned HTTP 400"
    assert "arguments is not JSON" in caplog.text and "\\nFORGED" in caplog.text and "\nFORGED" not in caplog.text


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
    kinds = [e["type"] for e in events]
    assert kinds.count("tool.started") == kinds.count("tool.finished") == 1


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


async def test_non_finite_numbers_in_arguments_are_refused(package_dir, model):
    model.replies = [tool_reply("read", '{"path": "x", "offset": NaN}'), text_reply("ok")]
    turn, events = make_turn(package_dir, model)
    result = await run(turn)
    finished = next(e for e in events if e["type"] == "tool.finished")
    assert result["status"] == "ok" and finished["error"] and "valid JSON" in finished["output"]


async def test_lone_surrogates_from_the_model_are_replaced(package_dir, model):
    model.replies = [text_reply("bad \ud800 text")]
    turn, events = make_turn(package_dir, model)
    result = await run(turn)
    assert result["text"] == "bad ? text"
    json.dumps(events).encode("utf-8")


async def test_a_turn_passes_one_workspace_to_its_calls_and_ends_it(package_dir, model):
    model.replies = [tool_reply("bash", {"command": "true"}), tool_reply("bash", {"command": "true"}, "call_2"),
                     text_reply("ok")]
    turn, _ = make_turn(package_dir, model)
    seen, ended = [], []

    async def record(name, args, workspace=None):
        seen.append(workspace)
        return "done", False

    async def end(workspace):
        ended.append(workspace)
        raise RuntimeError("sandbox gone")

    turn.runner.run, turn.runner.end_turn = record, end
    other, _ = make_turn(package_dir, model)
    result = await run(turn)
    assert result["status"] == "ok" and seen == [turn.workspace] * 2 and ended == [turn.workspace]
    assert len(turn.workspace) == 32 and other.workspace != turn.workspace


async def test_a_turn_without_sandbox_calls_does_not_call_the_sandbox_to_end(package_dir, model):
    model.replies = [text_reply("no tools needed")]
    turn, _ = make_turn(package_dir, model)
    ended = []

    async def end(workspace):
        ended.append(workspace)

    turn.runner.end_turn = end
    assert (await run(turn))["status"] == "ok" and ended == []


async def test_a_second_cancel_while_ending_still_finishes_the_turn(package_dir, model):
    model.replies = [tool_reply("bash", {"command": "sleep 30"}), text_reply("never")]
    turn, events = make_turn(package_dir, model)
    started, ended = asyncio.Event(), []

    async def end(workspace):
        started.set()
        await asyncio.sleep(0.5)
        ended.append(workspace)

    turn.runner.end_turn = end
    task = asyncio.create_task(run(turn))
    await asyncio.sleep(1)
    task.cancel()
    await started.wait()
    task.cancel()
    result = await task
    assert result["status"] == "cancelled" and events[-1]["type"] == "turn.finished"
    await asyncio.sleep(0.7)
    assert ended == [turn.workspace]


async def test_a_cancelled_turn_still_ends_its_workspace(package_dir, model):
    model.replies = [tool_reply("bash", {"command": "sleep 30"}), text_reply("never")]
    turn, _ = make_turn(package_dir, model)
    ended = []

    async def end(workspace):
        ended.append(workspace)

    turn.runner.end_turn = end
    task = asyncio.create_task(run(turn))
    await asyncio.sleep(1)
    task.cancel()
    result = await task
    assert result["status"] == "cancelled" and ended == [turn.workspace]


async def test_a_crashing_tool_reports_a_generic_error(package_dir, model, monkeypatch):
    model.replies = [tool_reply("bash", {"command": "true"}), text_reply("ok")]
    turn, events = make_turn(package_dir, model)

    async def explode(*_a):
        raise RuntimeError("secret-dsn://user:pw@db")

    monkeypatch.setattr(turn.runner, "run", explode)
    result = await run(turn)
    finished = next(e for e in events if e["type"] == "tool.finished")
    assert result["status"] == "ok" and finished["output"] == "bash failed"
    assert "secret-dsn" not in json.dumps(events)


async def test_an_unexpected_failure_still_finishes_the_turn(package_dir, model, monkeypatch):
    turn, events = make_turn(package_dir, model)

    async def explode(*_a):
        raise RuntimeError("secret-dsn://user:pw@db")

    monkeypatch.setattr(turn, "_loop", explode)
    result = await run(turn)
    assert result["status"] == "model_error" and events[-1]["type"] == "turn.finished"
    assert "secret-dsn" not in json.dumps(events)
