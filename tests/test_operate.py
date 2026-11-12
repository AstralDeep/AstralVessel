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
    assert "VESSEL_UPSTREAM_MODEL_KEY='sk-typed'" in env and "VESSEL_APP_KEY_SHA256=" in env
    assert load_deployment(folder / "deployment.toml").agents["tester"].model == "m1"


def test_a_free_subnet_and_project_name_are_chosen(monkeypatch, tmp_path):
    def docker(*args):
        if args == ("network", "ls", "-q"):
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


def _named(package_dir, tmp_path, name):
    import shutil
    copy = tmp_path / f"pkg-{name}-{len(list(tmp_path.iterdir()))}"
    shutil.copytree(package_dir, copy)
    manifest = copy / "vessel.toml"
    manifest.write_text(manifest.read_text().replace('name = "tester"', f'name = "{name}"'))
    return copy


def test_mount_folders_are_expanded_and_must_exist(package_dir, tmp_path, monkeypatch):
    (package_dir / "vessel.toml").write_text((package_dir / "vessel.toml").read_text().replace(
        "[sandbox]", '[sandbox]\nmounts = [{ source = "notes", target = "/notes" }]'))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes").mkdir()
    base = {"agent": [str(package_dir)], "model_url": "https://m.test/v1", "model": "m", "api_key": "env:HOME"}
    got = operate.gather(options(**base, mount=["agent:/notes=~/notes"]), interactive=False)
    assert got.mounts[(0, "/notes")] == str(tmp_path / "notes")
    with pytest.raises(SystemExit, match="is not a folder"):
        operate.gather(options(**base, mount=["agent:/notes=~/missing"]), interactive=False)


def test_the_example_is_never_copied_over_existing_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_FOR_TEST", "x")
    answers = operate.gather(options(example=True, model_url="https://m.test/v1", model="m",
                                     api_key="env:KEY_FOR_TEST"), interactive=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "mine.md").write_text("mine")
    with pytest.raises(SystemExit, match="already exist"):
        operate.write_deployment(tmp_path, answers, example=True)
    assert [p.name for p in (tmp_path / "data").iterdir()] == ["mine.md"] and not (tmp_path / ".env").exists()


def test_agent_ids_stay_unique_and_the_gitignore_is_extended(package_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_FOR_TEST", "x")
    agents = [_named(package_dir, tmp_path, n) for n in ("x-2", "x", "x")]
    answers = operate.Answers(agents=agents, model_url="https://m.test/v1", api_key="env:KEY_FOR_TEST", model="m")
    folder = tmp_path / "deploy"
    folder.mkdir()
    (folder / ".gitignore").write_text("node_modules/")
    operate.write_deployment(folder, answers, example=False)
    assert list(load_deployment(folder / "deployment.toml").agents) == ["x-2", "x", "x-3"]
    assert (folder / ".gitignore").read_text() == "node_modules/\n.env\n.generated/\ncompose.generated.yml\nchats/\n"


def test_a_key_with_shell_characters_survives_the_env_file(package_dir, tmp_path, monkeypatch):
    from astralvessel.stack import load_env_file
    answers = operate.Answers(agents=[package_dir], model_url="https://m.test/v1", api_key="ab$cd#x y", model="m")
    operate.write_deployment(tmp_path, answers, example=False)
    monkeypatch.delenv("VESSEL_UPSTREAM_MODEL_KEY", raising=False)
    load_env_file(tmp_path / ".env")
    assert operate.os.environ["VESSEL_UPSTREAM_MODEL_KEY"] == "ab$cd#x y"
    assert operate._check_key("it's") and operate._check_key("a\nb")


def test_a_failed_write_leaves_nothing_behind(package_dir, tmp_path, monkeypatch):
    def broken(path):
        raise OSError("disk went away")
    monkeypatch.setattr(operate, "load_deployment", broken)
    answers = operate.Answers(agents=[package_dir], model_url="https://m.test/v1", api_key="k", model="m")
    with pytest.raises(OSError):
        operate.write_deployment(tmp_path, answers, example=False)
    assert not (tmp_path / ".env").exists() and not (tmp_path / "deployment.toml").exists()


def test_a_stopped_deployments_name_is_not_reused(monkeypatch, tmp_path):
    def docker(*args):
        if args[0] == "volume" and "label=com.docker.compose.project=demo" in args:
            return "demo_vessel_data\n"
        return ""
    monkeypatch.setattr(operate, "_docker", docker)
    assert operate.pick_project(tmp_path / "demo") == "demo-2"


def test_provider_errors_are_scrubbed_of_key_like_text():
    body = '{"error": "Incorrect API key sk-abc123***xyz9 provided", "id": "a1b2c3d4e5f6g7h8i9j0k1l2m3"}'
    text = operate._scrub(body)
    assert "sk-abc" not in text and "a1b2c3d4e5f6" not in text and "Incorrect API key" in text


def test_ask_needs_the_app_key_but_not_the_model_key(package_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_FOR_ASK", "sk-only-at-setup")
    answers = operate.Answers(agents=[package_dir], model_url="https://m.test/v1", api_key="env:KEY_FOR_ASK",
                              model="m")
    path = operate.write_deployment(tmp_path, answers, example=False)
    (tmp_path / "compose.generated.yml").write_text("services: {}\n")
    monkeypatch.delenv("KEY_FOR_ASK")
    for name in ("VESSEL_APP_KEY", "VESSEL_APP_KEY_SHA256", "VESSEL_SANDBOX_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "ps"]:
            assert "label=com.docker.compose.oneoff=False" in command
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        if command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "demo:local\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    agent = next(iter(load_deployment(path).agents))
    with pytest.raises(SystemExit) as done:
        operate.client(argparse.Namespace(deployment=str(path), agent=agent, question="hi"), "ask")
    assert done.value.code == 0
    assert not any("compose" in c for c in calls)
    assert calls[-1][:2] == ["docker", "run"] and "demo:local" in calls[-1]
