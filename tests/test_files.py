# Files from tools: outbox collection and its checks, the sandbox and vessel hops, turn limits, and the client.

from __future__ import annotations

import base64
import os

import httpx
import pytest
from conftest import APP_KEY, text_reply, tool_reply
from test_deploy import landlock

from astralvessel import files as outputs
from astralvessel import sandbox
from astralvessel.client import VesselClient
from astralvessel.deployment import load_deployment
from astralvessel.manifest import load_package
from astralvessel.server import Settings, app_keys, create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
KEY = "d" * 32


def allow_files(package_dir, kinds='["png", "csv"]'):
    manifest = package_dir / "vessel.toml"
    manifest.write_text(manifest.read_text().replace("[output]\n", f"[output]\nfiles = {kinds}\n"))


def test_the_outbox_keeps_only_allowed_valid_files_and_is_emptied(tmp_path):
    box = tmp_path / "outbox"
    box.mkdir()
    (box / "chart.png").write_bytes(PNG)
    (box / "table.csv").write_text("county,rate\nA,1\n")
    (box / "fake.png").write_text("not a png")
    (box / "drawing.svg").write_text("<svg></svg>")
    (box / "notes.txt").write_text("x")
    (box / ".hidden.csv").write_text("x")
    (box / "big.csv").write_bytes(b"a" * (outputs.MAX_FILE_BYTES + 1))
    (box / "link.csv").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("secret")
    (box / "folder.csv").mkdir()
    os.mkfifo(box / "pipe.csv")
    found, notes = outputs.collect(str(box), ["png", "csv"])
    assert [(f.name, f.media_type) for f in found] == [("chart.png", "image/png"), ("table.csv", "text/csv")]
    assert found[0].data == PNG and len(notes) == 8 and os.listdir(box) == []
    text = "\n".join(notes)
    assert "fake.png: not a valid .png" in text and "drawing.svg: this agent may not" in text
    assert "big.csv: larger than 4 MB" in text and "link.csv: not a regular" in text and "secret" not in text[-60:]
    assert (tmp_path / "secret").read_text() == "secret"


def test_the_outbox_returns_at_most_four_files_per_call(tmp_path):
    for index in range(6):
        (tmp_path / f"t{index}.csv").write_text("a,b\n")
    found, notes = outputs.collect(str(tmp_path), ["csv"])
    assert len(found) == outputs.MAX_FILES_PER_CALL and len(notes) == 2 and "at most 4" in notes[0]


def test_a_flooded_outbox_gives_the_model_a_short_summary(tmp_path):
    for index in range(50):
        (tmp_path / f"t{index}.txt").write_text("x")
    found, notes = outputs.collect(str(tmp_path), ["csv"])
    assert found == [] and len(notes) == outputs.MAX_NOTES and notes[-1] == "[41 more entries not attached]"
    assert os.listdir(tmp_path) == []


def test_an_outbox_swapped_for_a_link_reads_and_deletes_nothing_outside(tmp_path):
    victim = tmp_path / "victim"
    (victim / "sub").mkdir(parents=True)
    (victim / "secret.csv").write_text("password,hunter2\n")
    (victim / "keep.txt").write_text("x")
    workspace = tmp_path / "turn"
    workspace.mkdir()
    (workspace / "outbox").symlink_to(victim)
    found, notes = outputs.collect(str(workspace / "outbox"), ["csv"])
    assert found == [] and "not a folder" in notes[0] and not (workspace / "outbox").exists()
    assert sorted(os.listdir(victim)) == ["keep.txt", "secret.csv", "sub"]


@landlock
async def test_a_call_that_replaces_its_outbox_does_not_break_the_next_call(package_dir, tmp_path):
    allow_files(package_dir)
    package = load_package(package_dir)
    scratch = tmp_path / "calls"
    scratch.mkdir()
    runner = sandbox.LocalRunner(package, sandbox.confinement_for(package, str(scratch)))
    out, failed = await runner.run("bash", {"command": "rmdir $VESSEL_OUTBOX && echo x > $VESSEL_OUTBOX"}, KEY)
    assert not failed and "not a folder" in out
    await runner.run("bash", {"command": "rmdir $VESSEL_OUTBOX && ln -s /nowhere $VESSEL_OUTBOX"}, KEY)
    out, failed = await runner.run("bash", {"command": "printf 'a\\n' > $VESSEL_OUTBOX/t.csv && echo ok"}, KEY)
    assert not failed and "ok" in out and [f.name for f in runner.take_files(KEY)] == ["t.csv"]
    await runner.end_turn(KEY)


def test_the_vessel_checks_files_again_when_they_arrive():
    wire = [outputs.ToolFile("chart.png", "image/png", PNG).wire(),
            {"name": "../../x.png", "data": base64.b64encode(PNG).decode()},
            {"name": "evil.png", "data": base64.b64encode(b"<script>").decode()},
            {"name": "t.csv", "data": "not base64!"},
            {"name": "t.pdf", "data": base64.b64encode(b"%PDF-1.7").decode()}, "junk"]
    assert [f.name for f in outputs.from_wire(wire, ["png", "csv"])] == ["chart.png"]
    assert outputs.from_wire({"not": "a list"}, ["png"]) == []


@landlock
async def test_a_confined_call_returns_files_from_its_outbox(package_dir, tmp_path):
    allow_files(package_dir)
    package = load_package(package_dir)
    scratch = tmp_path / "calls"
    scratch.mkdir()
    runner = sandbox.LocalRunner(package, sandbox.confinement_for(package, str(scratch)))
    out, failed = await runner.run("bash", {"command": "printf 'a,b\\n1,2\\n' > $VESSEL_OUTBOX/t.csv; "
                                                       "echo x > $VESSEL_OUTBOX/n.txt; echo done"}, KEY)
    assert not failed and out.startswith("[not attached n.txt") and "done" in out
    [found] = runner.take_files(KEY)
    assert (found.name, found.data) == ("t.csv", b"a,b\n1,2\n") and runner.take_files(KEY) == []
    out, _ = await runner.run("bash", {"command": "ls $VESSEL_OUTBOX | wc -l"}, KEY)
    assert out.startswith("0")
    await runner.end_turn(KEY)
    assert list(scratch.iterdir()) == []


async def test_the_sandbox_server_sends_collected_files(package_dir, monkeypatch):
    allow_files(package_dir)
    app = sandbox.sandbox_app(str(package_dir), token="t" * 16, confine=False)
    original = sandbox.LocalRunner.take_files

    def take(self, workspace):
        return [outputs.ToolFile("chart.png", "image/png", PNG)] if workspace == KEY else original(self, workspace)

    monkeypatch.setattr(sandbox.LocalRunner, "take_files", take)
    remote = sandbox.RemoteRunner("http://sb", "t" * 16, 5, ["png"])
    remote._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb",
                                       headers={"Authorization": "Bearer " + "t" * 16})
    out, failed = await remote.run("bash", {"command": "echo hi"}, KEY)
    assert not failed and [f.data for f in remote.take_files(KEY)] == [PNG]
    await remote.run("bash", {"command": "echo hi"}, KEY)
    await remote.end_turn(KEY)
    assert remote.take_files(KEY) == []
    await remote.close()


class FileRunner(sandbox.LocalRunner):
    def __init__(self, package, count):
        super().__init__(package)
        self.count = count

    async def run(self, name, args, workspace=None, owner=""):
        return "made", False

    def take_files(self, workspace):
        return [outputs.ToolFile(f"chart-{i}.png", "image/png", PNG) for i in range(self.count)]


@pytest.fixture
def file_vessel(package_dir, model, tmp_path):
    allow_files(package_dir)
    path = tmp_path / "deployment.toml"
    path.write_text('[vessel]\npublic_url = "http://vessel.test"\n[egress]\nupstream_url = "https://models.test/v1"\n'
                    f'[[agents]]\nid = "tester"\npackage = '
                    f'"{package_dir}"\n[[apps]]\nname = "web"\nkey_sha256 = '
                    f'"{__import__("hashlib").sha256(APP_KEY.encode()).hexdigest()}"\nagents = ["tester"]\n')
    deployment = load_deployment(path)
    settings = Settings(deployment, task_db=str(tmp_path / "tasks.sqlite"), app_keys=app_keys(deployment))

    def make(count):
        runner = FileRunner(deployment.agents["tester"].package, count)
        app = create_app(settings, runners={"tester": runner}, model_transport=model.transport())
        return VesselClient("http://vessel.test", APP_KEY, transport=httpx.ASGITransport(app=app))
    return make


async def test_files_stream_to_the_caller_within_the_turn_limit(file_vessel, model):
    model.replies = [tool_reply("bash", {"command": "plot"}), tool_reply("bash", {"command": "plot"}, "call_2"),
                     text_reply("Here are the charts.")]
    client = file_vessel(5)
    events = [e async for e in client.stream("tester", "alice", "chart it")]
    got = [e for e in events if e["type"] == "file"]
    assert len(got) == outputs.MAX_FILES_PER_TURN and all(e["data"] == PNG for e in got)
    assert got[0]["media_type"] == "image/png" and got[0]["call_id"] == "call_1" and got[-1]["call_id"] == "call_2"
    second = [e for e in events if e["type"] == "tool.finished"][1]["output"]
    assert second.count("[attached") == 3 and second.count("all the files it may") == 2
    finished = next(e for e in events if e["type"] == "turn.finished")
    assert len(finished["files"]) == 8 and finished["files"][0] == {"name": "chart-0.png", "media_type": "image/png",
                                                                    "bytes": len(PNG)}
    card = await client.card("tester")
    assert {"image/png", "text/csv"} <= set(card["defaultOutputModes"])


async def test_a_caller_that_does_not_accept_a_file_type_does_not_get_it(file_vessel, model):
    model.replies = [tool_reply("bash", {"command": "plot"}), text_reply("done")]
    client = file_vessel(1)
    body = {"jsonrpc": "2.0", "id": "1", "method": "SendStreamingMessage", "params": {
        "message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "chart it"}]},
        "configuration": {"acceptedOutputModes": ["text/markdown"]}}}
    headers = {"X-Vessel-User": "alice", "Accept": "text/event-stream", "A2A-Version": "1.0"}
    async with client._client.stream("POST", "/agents/tester", headers=headers, json=body) as response:
        text = (await response.aread()).decode()
    assert "turn.finished" in text and '"name": "file"' not in text and '"name":"file"' not in text


async def test_the_model_sees_attached_files_even_when_the_output_is_clipped(package_dir, model):
    from astralvessel.agent import Turn
    from astralvessel.model import ModelClient
    allow_files(package_dir)
    package = load_package(package_dir)
    runner = FileRunner(package, 1)

    async def flood(name, args, workspace=None, owner=""):
        return "x" * (package.manifest.sandbox.tool_output_chars * 2), False

    runner.run = flood
    model.replies = [tool_reply("bash", {"command": "plot"}), text_reply("done")]
    events = []

    async def emit(item):
        events.append(item)

    client = ModelClient("http://model.test/v1", "k", "test-model", transport=model.transport(), retries=1)
    await Turn(package, client, runner, emit).run([], "chart", conversation_id="c", turn_id="t")
    tool_message = model.requests[1]["messages"][-1]["content"]
    assert "output truncated" in tool_message and tool_message.endswith("to the answer]")
    assert [e["name"] for e in events if e["type"] == "file.added"] == ["chart-0.png"]



async def test_a_turn_without_files_reports_an_empty_files_list(file_vessel, model):
    model.replies = [text_reply("No charts needed.")]
    client = file_vessel(0)
    events = [e async for e in client.stream("tester", "alice", "just answer")]
    finished = next(e for e in events if e["type"] == "turn.finished")
    assert finished["files"] == []
    stored = await client.rpc("tester", "alice", "GetTask", {"id": finished["task_id"]})
    assert stored["result"]["status"]["message"]["parts"][0]["data"]["files"] == []
