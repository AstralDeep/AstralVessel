# The operator commands without Docker: writing a deployment from prompts or flags, and the waiting and model checks.

from __future__ import annotations

import argparse
import json
import stat
import subprocess
import tomllib

import pytest

from astralvessel import operate
from astralvessel.deployment import load_deployment


def options(**given) -> argparse.Namespace:
    base = {"deployment": None, "example": False, "agent": None, "mount": None, "model_url": None, "api_key": None,
            "model": None, "timeout": 1.0}
    return argparse.Namespace(**{**base, **given})


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    monkeypatch.setattr(operate, "_docker", lambda *args: "")


def test_without_a_terminal_it_names_every_missing_value():
    with pytest.raises(SystemExit) as stopped:
        operate.gather(options(), interactive=False)
    message = str(stopped.value)
    for flag in ("--agent", "--model-url", "--api-key env:NAME", "--model"):
        assert flag in message


def test_without_a_terminal_an_agents_mounts_are_required(package_dir, tmp_path):
    (package_dir / "vessel.toml").write_text((package_dir / "vessel.toml").read_text().replace(
        "[sandbox]", '[sandbox]\nmounts = [{ source = "notes", target = "/notes" }]'))
    with pytest.raises(SystemExit, match="--mount agent:/notes=FOLDER"):
        operate.gather(options(agent=[str(package_dir)], model_url="https://m.test/v1", model="m",
                               api_key="env:HOME"), interactive=False)


def test_a_key_on_the_command_line_is_refused_and_env_names_must_be_set(monkeypatch):
    with pytest.raises(SystemExit, match="env:NAME"):
        operate.gather(options(api_key="sk-literal"), interactive=False)
    monkeypatch.delenv("UNSET_PROVIDER_KEY", raising=False)
    with pytest.raises(SystemExit, match="not set"):
        operate.gather(options(api_key="env:UNSET_PROVIDER_KEY"), interactive=False)
    with pytest.raises(SystemExit, match="http"):
        operate.gather(options(model_url="ftp://nope"), interactive=False)


def test_flags_write_a_deployment_that_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-from-env")
    answers = operate.gather(options(example=True, model_url="https://m.test/v1", model="my-model",
                                     api_key="env:MY_PROVIDER_KEY"), interactive=False)
    folder = tmp_path / "demo"
    folder.mkdir()
    path = operate.write_deployment(folder, answers, example=True)
    env = folder / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "sk-from-env" not in env.read_text() and "VESSEL_UPSTREAM_MODEL_KEY" not in env.read_text()
    raw = tomllib.loads(path.read_text())
    assert raw["vessel"]["project"] == "demo" and raw["egress"]["upstream_key"] == "env:MY_PROVIDER_KEY"
    assert raw["agents"][0]["package"] == "agents/field-guide" and raw["agents"][0]["model"] == "my-model"
    assert raw["agents"][0]["sandbox"]["mounts"] == [{"target": "/data", "source": "data"}]
    assert (folder / "data" / "species" / "great-blue-heron.md").is_file()
    deployment = load_deployment(path)
    assert deployment.agents["field-guide"].model == "my-model"
    assert ".env" in (folder / ".gitignore").read_text()
    with pytest.raises(SystemExit, match="move the old .env"):
        (folder / "deployment.toml").unlink()
        operate.write_deployment(folder, answers, example=True)


def test_prompts_take_a_hidden_key_and_retry_bad_answers(tmp_path, monkeypatch, package_dir):
    answers = iter(["/no/such/agent", str(package_dir), "not a url", "https://m.test/v1", "m1"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    keys = iter(["", "sk-typed"])
    monkeypatch.setattr(operate.getpass, "getpass", lambda prompt: next(keys))
    got = operate.gather(options(), interactive=True)
    assert got.agents == [package_dir.resolve()] and got.model_url == "https://m.test/v1"
    assert got.api_key == "sk-typed" and got.model == "m1"
    folder = tmp_path / "typed"
    folder.mkdir()
    operate.write_deployment(folder, got, example=False)
    env = (folder / ".env").read_text()
    assert "VESSEL_UPSTREAM_MODEL_KEY=sk-typed" in env and "VESSEL_APP_KEY_SHA256=" in env
    assert load_deployment(folder / "deployment.toml").agents["tester"].model == "m1"


def test_a_free_subnet_and_project_name_are_chosen(monkeypatch, tmp_path):
    def docker(*args):
        if args[:2] == ("network", "ls"):
            return "n1\n"
        if args[:2] == ("network", "inspect"):
            return "172.31.247.0/24 172.17.0.0/16"
        if args[0] == "ps" and "label=com.docker.compose.project=demo" in args:
            return "/somewhere/else\n"
        return ""
    monkeypatch.setattr(operate, "_docker", docker)
    assert str(operate.pick_subnet()) == "172.31.246.0/24"
    assert operate.pick_project(tmp_path / "demo") == "demo-2"
    assert operate.pick_project(tmp_path / "My Stack!") == "my-stack"


class FakeStack:
    def __init__(self, health: list[str], states: str = "vessel running\n"):
        self.health, self.states, self.logged = health, states, []

    def python(self, service, source, interpreter="python", timeout=300):
        out = self.health.pop(0) if self.health else ""
        return subprocess.CompletedProcess([], 0, stdout=out, stderr="")

    def compose(self, *args, **kw):
        return subprocess.CompletedProcess([], 0, stdout=self.states, stderr="")

    def logs(self, service, tail=40):
        self.logged.append(service)
        return "log lines"


def test_waiting_reports_the_agents_that_never_became_ready(tmp_path, monkeypatch, capsys, package_dir):
    monkeypatch.setattr(operate.time, "sleep", lambda s: None)
    deployment = argparse.Namespace(agents={"one": None, "two": None})
    stack = FakeStack([json.dumps({"agents": {"one": True, "two": False}})] * 3)
    clock = iter([0, 0.2, 0.4, 0.6, 2, 3])
    monkeypatch.setattr(operate.time, "monotonic", lambda: next(clock))
    assert operate.wait_ready(stack, deployment, timeout=1) is False
    assert "not ready after 1s: two" in capsys.readouterr().out and stack.logged == ["sandbox-two", "vessel"]
    ready = FakeStack([json.dumps({"agents": {"one": True, "two": True}})])
    monkeypatch.setattr(operate.time, "monotonic", lambda: 0)
    assert operate.wait_ready(ready, deployment, timeout=1) is True
    dead = FakeStack(["not json"], states="vessel running\nsandbox-two exited\n")
    assert operate.wait_ready(dead, deployment, timeout=1) is False and dead.logged == ["sandbox-two"]


def test_the_model_check_fails_on_an_error_or_a_missing_answer():
    agents = {"a": argparse.Namespace(model="m1")}
    deployment = argparse.Namespace(agents=agents)
    good = FakeStack([json.dumps({"model": "m1", "code": 200, "detail": ""})])
    assert operate.check_models(good, deployment) is True
    bad = FakeStack([json.dumps({"model": "m1", "code": 401, "detail": "invalid key"})])
    assert operate.check_models(bad, deployment) is False
    assert operate.check_models(FakeStack([""]), deployment) is False
