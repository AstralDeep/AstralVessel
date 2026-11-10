# Unit tests for the manifest loader, tools, primitives validation, the store's owner binding, and the A2A decoder.

from __future__ import annotations

import asyncio

import pytest

from astralvessel.client import decode, integral
from astralvessel.manifest import load_package
from astralvessel.primitives import validate_components
from astralvessel.sandbox import LocalRunner
from astralvessel.store import Store
from astralvessel.tools import clip


def test_example_nexus_package_loads():
    package = load_package("examples/nexus")
    assert package.manifest.agent.name == "nexus"
    assert {t.name for t in package.declared} == {"find_pages", "profile"}
    assert package.manifest.output.primitives


def test_unknown_builtin_is_rejected(package_dir):
    text = (package_dir / "vessel.toml").read_text().replace('"bash"]', '"bash", "teleport"]')
    (package_dir / "vessel.toml").write_text(text)
    with pytest.raises(ValueError, match="teleport"):
        load_package(package_dir)


def test_declared_tool_outside_package_is_rejected(package_dir, tmp_path):
    (tmp_path / "evil.toml").write_text('name = "evil"\ndescription = "x"\ncommand = ["true"]\n')
    text = (package_dir / "vessel.toml").read_text().replace('"tools/echo.toml"', '"../evil.toml"')
    (package_dir / "vessel.toml").write_text(text)
    with pytest.raises(ValueError, match="outside the package"):
        load_package(package_dir)


def test_declared_tool_cannot_shadow_a_builtin(package_dir):
    (package_dir / "tools" / "echo.toml").write_text('name = "bash"\ndescription = "x"\ncommand = ["true"]\n')
    with pytest.raises(ValueError, match="collide"):
        load_package(package_dir)


async def test_sandbox_tools_run_with_limits(package_dir):
    runner = LocalRunner(load_package(package_dir))
    out, err = await runner.run("read", {"path": "wiki/person-ada-lovelace.md"})
    assert not err and "Analytical engine" in out
    out, err = await runner.run("grep", {"pattern": "Analytical", "glob": "*.md"})
    assert not err and "person-ada-lovelace.md" in out
    out, err = await runner.run("grep", {"pattern": "nothing-matches-this"})
    assert out == "no matches" and not err
    out, err = await runner.run("glob", {"pattern": "wiki/person-*.md"})
    assert "person-ada-lovelace.md" in out
    out, err = await runner.run("list", {"path": "wiki"})
    assert out.strip() == "person-ada-lovelace.md"
    out, err = await runner.run("bash", {"command": "echo hi && exit 3"})
    assert err and "hi" in out and "exit code 3" in out
    out, err = await runner.run("bash", {"command": "sleep 30"})
    assert err and "timed out" in out
    out, err = await runner.run("echo", {"word": "vessel"})
    assert not err and '"word": "vessel"' in out
    out, err = await runner.run("read", {"path": "missing.md"})
    assert err


async def test_bash_does_not_inherit_the_harness_environment(package_dir, monkeypatch):
    monkeypatch.setenv("VESSEL_SECRET_PROBE", "should-not-leak")
    runner = LocalRunner(load_package(package_dir))
    out, _ = await runner.run("bash", {"command": "env"})
    assert "should-not-leak" not in out


def test_clip_marks_truncation():
    text, truncated = clip("x" * 5000, 1000)
    assert truncated and len(text) < 1100 and "truncated" in text
    assert clip("short", 1000) == ("short", False)


def test_primitives_validate_against_astralprims():
    good = validate_components([{"type": "table", "headers": ["a"], "rows": [[1]]},
                                {"type": "metric", "title": "People", "value": "3"}])
    assert good[0]["type"] == "table" and good[1]["type"] == "metric"
    with pytest.raises(ValueError, match="component 0"):
        validate_components([{"type": "table", "headers": "not-a-list"}])
    with pytest.raises(ValueError):
        validate_components([{"type": "no_such_primitive"}])
    with pytest.raises(ValueError):
        validate_components([])
    with pytest.raises(ValueError, match="at most"):
        validate_components([{"type": "text", "content": "x"}] * 13)


async def test_store_binds_every_conversation_to_its_owner(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    assert await store.claim("alice", "c1", "First question")
    assert not await store.claim("bob", "c1", "Hijack")
    result = {"status": "ok", "text": "answer"}
    await store.record_turn("alice", "c1", "t1", "q", result, [{"role": "user", "content": "q"}], [])
    await store.record_turn("bob", "c1", "t2", "q", result, [{"role": "user", "content": "evil"}], [])
    assert len((await store.get("alice", "c1"))["turns"]) == 1
    assert await store.get("bob", "c1") is None
    assert await store.history("bob", "c1") == []
    assert [c["id"] for c in await store.list("alice")] == ["c1"] and await store.list("bob") == []
    assert not await store.delete("bob", "c1")
    assert await store.delete("alice", "c1")
    assert await store.get("alice", "c1") is None


async def test_store_history_drops_reasoning_and_sweeps_idle(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    await store.claim("alice", "c1", "q")
    await store.record_turn("alice", "c1", "t1", "q", {"status": "ok"},
                            [{"role": "assistant", "content": "a", "reasoning_content": "secret thoughts"}], [])
    assert "reasoning_content" not in (await store.history("alice", "c1"))[0]
    await asyncio.sleep(0.01)
    assert await store.sweep(0) == 1


def test_decoder_reads_vessel_events_and_answers():
    frame = {"result": {"statusUpdate": {"taskId": "t", "contextId": "c", "status": {"state": "TASK_STATE_WORKING",
             "message": {"parts": [{"data": {"type": "step.finished", "step": 1.0, "tokens": {"input": 3.0}}}]}}}}}
    event = decode(frame)[0]
    assert event["type"] == "step.finished" and event["step"] == 1 and isinstance(event["step"], int)
    answer = decode({"result": {"artifactUpdate": {"artifact": {"parts": [
        {"text": "hello", "mediaType": "text/markdown"},
        {"data": {"components": [{"type": "metric", "title": "x", "value": "1"}]},
         "mediaType": "application/vnd.astral.primitives+json", "metadata": {"schema_version": "0.4.0"}}]}}}})[0]
    assert answer["text"] == "hello" and answer["components"][0]["type"] == "metric"
    assert answer["schema_version"] == "0.4.0"
    assert integral([1.0, 1.5, {"a": 2.0}]) == [1, 1.5, {"a": 2}]
