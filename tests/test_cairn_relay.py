"""Cairn's workers are the session's agents (sandbox/cairn_relay.py): the shim Cairn's dispatcher starts as `claude`
or `codex` hands the call over a real Unix socket to the relay, which runs the agent's own CLI - here a stand-in that
records how it is called - and only for the command lines of Cairn's own drivers at the reviewed commit.

The user: "Cairn als eigener Teil im Board, wie ein Service-Container neben dem Agent; sein Board öffnet sich
automatisch im Browser-Tab, die Agents der Session sind seine Worker." and "Cairn natürlich auch mit anderen
Modellen / lokalem Preset supporten." """
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = load("cairn_relay_tests", "sandbox/cairn_relay.py")
UUID = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
FAKE = r'''#!PYTHON
import json, os, re, sys, time
args = sys.argv[1:]
record = {"name": os.path.basename(sys.argv[0]), "args": args, "cwd": os.getcwd(),
          "is_sandbox": os.environ.get("IS_SANDBOX"), "secret": os.environ.get("AGENT_ONLY")}
for path in re.findall(r"\S+/graph\.yaml", " ".join(args)):
    record["graph"] = open(path).read()
with open(os.environ["FAKE_LOG"], "a") as stream:
    stream.write(json.dumps(record) + "\n")
if "SLEEP" in " ".join(args):
    open(os.environ["FAKE_LOG"] + ".pid", "w").write(str(os.getpid()))
    time.sleep(60)
print("answer of " + record["name"])
print("session id: 0199aa11-2233-4455-6677-8899aabbccdd", file=sys.stderr)
sys.exit(3 if "FAIL" in " ".join(args) else 0)
'''


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A relay on a real socket, its workers stand-ins on PATH, the agent's environment with a variable only the
    agent's side has; graph snapshots below the test's own folder."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        (bin_dir / name).write_text(FAKE.replace("PYTHON", sys.executable, 1))
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("AGENT_ONLY", "the agent's login")
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    prompts = tmp_path / "cairn-prompts"
    monkeypatch.setattr(relay, "PROMPTS", re.compile(re.escape(str(prompts)) + r"/[a-z_]{1,32}-[0-9a-f]{12}/graph\.yaml"))
    workspace = tmp_path / "work" / "cairn" / "runs"
    cfg = {"socket": str(tmp_path / "relay" / "relay.sock"), "workspace": str(workspace),
           "workers": {"claude": ["--model", "sonnet"], "codex": ["--profile", "sandbox-ai-llm"]}}
    server = relay.listen(cfg["socket"])
    stop = threading.Event()
    thread = threading.Thread(target=relay.serve, args=(cfg,), kwargs={"server": server, "stop": stop}, daemon=True)
    thread.start()

    def call(agent, *argv, cwd=None):
        out, err = io.BytesIO(), io.BytesIO()
        status = relay.worker(agent, list(argv), path=cfg["socket"], out=out, err=err,
                              cwd=str(cwd or workspace / "p1"))
        return status, out.getvalue().decode(), err.getvalue().decode()

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    yield {"call": call, "calls": calls, "cfg": cfg, "workspace": workspace, "prompts": prompts, "log": log}
    stop.set()
    server.close()


def test_claude_runs_in_the_agent_s_environment_with_sandbox_ai_s_model_in_the_project_folder(world):
    status, out, err = world["call"]("claude", "--session-id", UUID, "--dangerously-skip-permissions", "-p", "--",
                                     "Explore the target")
    assert status == 0 and out == "answer of claude\n" and "session id" in err
    [call] = world["calls"]()
    assert call["args"] == ["--session-id", UUID, "--dangerously-skip-permissions", "-p", "--model", "sonnet", "--",
                            "Explore the target"]
    assert call["cwd"] == str(world["workspace"] / "p1") and call["is_sandbox"] == "1"
    assert call["secret"] == "the agent's login", "the agent's environment, never Cairn's"
    status, _, _ = world["call"]("claude", "-r", UUID, "--dangerously-skip-permissions", "-p", "--", "Conclude")
    assert status == 0 and world["calls"]()[-1]["args"][:2] == ["-r", UUID]


def test_codex_gets_its_profile_after_exec_and_resumes_by_its_session(world):
    skip = "--dangerously-bypass-approvals-and-sandbox"
    status, _, err = world["call"]("codex", "exec", skip, "--", "Explore")
    assert status == 0 and "session id: 0199aa11" in err  # Cairn reads Codex's session from its error output
    status, _, _ = world["call"]("codex", "exec", "resume", "0199aa11-2233-4455-6677-8899aabbccdd", skip, "--", "Go")
    first, second = world["calls"]()
    assert first["args"] == ["exec", "--profile", "sandbox-ai-llm", skip, "--", "Explore"]
    assert second["args"] == ["exec", "--profile", "sandbox-ai-llm", "resume", "0199aa11-2233-4455-6677-8899aabbccdd",
                              skip, "--", "Go"]
    assert first["is_sandbox"] is None, "only Claude Code is told it runs in a sandbox"


def test_the_graph_snapshot_cairn_wrote_in_its_container_reaches_the_worker(world):
    graph = world["prompts"] / "explore-0123456789ab" / "graph.yaml"
    graph.parent.mkdir(parents=True)
    graph.write_text("facts:\n- origin\n")
    found = relay.snapshots(["--", f"Read {graph} first"])
    assert found == {str(graph): "facts:\n- origin\n"}
    status, _, _ = world["call"]("claude", "--session-id", UUID, "--dangerously-skip-permissions", "-p", "--",
                                 f"The graph YAML snapshot is stored in this file:\n\n{graph}\n\nRead it.")
    assert status == 0 and world["calls"]()[-1]["graph"] == "facts:\n- origin\n"


def test_the_relay_writes_the_snapshots_it_is_handed(tmp_path, monkeypatch):
    prompts = tmp_path / "cairn-prompts"
    monkeypatch.setattr(relay, "PROMPTS", re.compile(re.escape(str(prompts)) + r"/[a-z_]{1,32}-[0-9a-f]{12}/graph\.yaml"))
    path = f"{prompts}/reason-00112233aabb/graph.yaml"
    _, cwd, files, _ = relay.command({"agent": "claude", "argv": ["--session-id", UUID, CLAUDE_SKIP, "-p", "--",
                                                                  "x"],
                                      "cwd": f"{tmp_path}/runs/p2", "files": {path: "only in the request"}},
                                     {"claude": []}, f"{tmp_path}/runs")
    relay.prepare(cwd, files)
    assert Path(path).read_text() == "only in the request" and Path(cwd).is_dir()


CLAUDE_SKIP = "--dangerously-skip-permissions"


@pytest.mark.parametrize("argv", [
    ["-p", "--", "x"],  # no session
    ["--session-id", UUID, CLAUDE_SKIP, "-p", "--", "x", "y"],
    ["--session-id", "not-a-uuid", CLAUDE_SKIP, "-p", "--", "x"],
    ["--session-id", UUID, "--mcp-config", "-p", "--", "x"],  # another option where Cairn's flag is
    ["--session-id", UUID, CLAUDE_SKIP, "--print", "--", "x"],
    ["--settings", "/tmp/x.json", "-p", "--", "x"],
    [],
])
def test_only_the_command_lines_of_cairn_s_claudecode_driver(argv):
    with pytest.raises(ValueError, match="claudecode driver"):
        relay.command({"agent": "claude", "argv": argv, "cwd": "/work/cairn/runs/p1"}, {"claude": []},
                      "/work/cairn/runs")


@pytest.mark.parametrize("argv", [
    ["exec", "--", "x"],
    ["exec", "--dangerously-bypass-approvals-and-sandbox", "-c", "x"],
    ["exec", "resume", "--last", "--dangerously-bypass-approvals-and-sandbox", "--", "x"],
    ["exec", "resume", "a b", "--dangerously-bypass-approvals-and-sandbox", "--", "x"],
    ["mcp", "add", "x"],
    ["login"],
])
def test_only_the_command_lines_of_cairn_s_codex_driver(argv):
    with pytest.raises(ValueError, match="codex driver"):
        relay.command({"agent": "codex", "argv": argv, "cwd": "/work/cairn/runs/p1"}, {"codex": []},
                      "/work/cairn/runs")


def test_no_other_program_no_other_folder_no_other_file():
    good = ["--session-id", UUID, CLAUDE_SKIP, "-p", "--", "x"]
    for agent in ("pi", "bash", "/bin/sh", "opencode"):
        with pytest.raises(ValueError, match="no worker"):
            relay.command({"agent": agent, "argv": good, "cwd": "/work/cairn/runs/p1"}, {"claude": []},
                          "/work/cairn/runs")
    with pytest.raises(ValueError, match="no worker"):  # an agent of the session that is not a worker
        relay.command({"agent": "codex", "argv": ["--help"], "cwd": "/"}, {"claude": []}, "/work/cairn/runs")
    for cwd in ("/work", "/root", "/work/cairn/runs/../../../root", "/work/cairn/runs/a/b", "/work/cairn/runs/",
                "/work/cairn/runsx/p1"):
        with pytest.raises(ValueError, match="folder"):
            relay.command({"agent": "claude", "argv": good, "cwd": cwd}, {"claude": []}, "/work/cairn/runs")
    for path in ("/root/.claude/.credentials.json", "/tmp/cairn-prompts/../x/graph.yaml",
                 "/tmp/cairn-prompts/explore-0123456789ab/other.yaml"):
        with pytest.raises(ValueError, match="graph snapshot"):
            relay.command({"agent": "claude", "argv": good, "cwd": "/work/cairn/runs/p1", "files": {path: "x"}},
                          {"claude": []}, "/work/cairn/runs")
    assert relay.command({"agent": "codex", "argv": ["--help"], "cwd": "/"}, {"codex": []}, "/w")[0] == \
        ["codex", "--help"]


def test_a_refused_call_is_answered_and_runs_nothing(world):
    status, out, err = world["call"]("claude", "--dangerously-skip-permissions", "-c", "rm -rf /work")
    assert status == relay.REFUSED and out == "" and "refused" in err and world["calls"]() == []
    status, _, err = world["call"]("claude", "--session-id", UUID, CLAUDE_SKIP, "-p", "--", "x", cwd="/root")
    assert status == relay.REFUSED and "folder" in err and world["calls"]() == []


def test_a_symbolic_link_does_not_lead_the_worker_elsewhere(world, tmp_path):
    world["workspace"].mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (world["workspace"] / "p9").symlink_to(tmp_path / "elsewhere")
    status, _, err = world["call"]("claude", "--session-id", UUID, CLAUDE_SKIP, "-p", "--", "x",
                                   cwd=world["workspace"] / "p9")
    assert status == 127 and "symbolic link" in err and world["calls"]() == []


def test_the_worker_gone_ends_the_agent_s_process_group(world):
    """Cairn's timeout kills its `claude`: the agent's CLI in the agent's container must not run on, spending."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(world["cfg"]["socket"])
    request = {"agent": "claude", "argv": ["--session-id", UUID, CLAUDE_SKIP, "-p", "--", "SLEEP"],
               "cwd": str(world["workspace"] / "p1"), "files": {}}
    client.sendall(relay.frame(b"q", json.dumps(request).encode()))
    pid_file = Path(str(world["log"]) + ".pid")
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            break
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    client.close()
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("the worker's CLI ran on after the worker went")


def test_failing_tasks_pause_the_workers_and_a_success_ends_the_pause():
    now = [0.0]
    guard = relay.Guard(clock=lambda: now[0], failures=3, pause=60, limit=900)
    for _ in range(2):
        guard.done(1)
    assert guard.paused() == 0
    guard.done(1)
    assert guard.paused() == 60
    now[0] = 61
    assert guard.paused() == 0
    for _ in range(3):
        guard.done(1)
    assert guard.paused() == 120, "each pause doubles"
    now[0] = 1000
    guard.done(0)
    for _ in range(3):
        guard.done(1)
    assert guard.paused() == 60, "a success starts again from the first pause"
    for _ in range(30):
        for _ in range(3):
            guard.done(1)
    assert guard.paused() == 900


def test_the_relay_answers_a_paused_task_without_running_it(world, monkeypatch):
    for _ in range(relay.FAILURES):
        status, _, _ = world["call"]("claude", "--session-id", UUID, CLAUDE_SKIP, "-p", "--", "FAIL")
        assert status == 3
    before = len(world["calls"]())
    status, _, err = world["call"]("claude", "--session-id", UUID, CLAUDE_SKIP, "-p", "--", "again")
    assert status == relay.PAUSED and "paused" in err and "stop the project" in err
    assert len(world["calls"]()) == before
    status, out, _ = world["call"]("claude", "--help")
    assert status == 0 and out, "--help (Cairn's check at its start) is no task"


def test_the_socket_is_the_owner_s_only(world):
    assert os.stat(world["cfg"]["socket"]).st_mode & 0o777 == 0o600


def test_no_relay_says_so():
    err = io.BytesIO()
    assert relay.worker("claude", ["--help"], path="/nonexistent/relay.sock", out=io.BytesIO(), err=err) == 127
    assert b"not reachable" in err.getvalue()


# --- analysis round 1 (security): the snapshots the relay writes are bounded ---------------------------------------

def snapshot_world(tmp_path, monkeypatch):
    prompts = tmp_path / "cairn-prompts"
    monkeypatch.setattr(relay, "PROMPTS", re.compile(re.escape(str(prompts)) + r"/[a-z_]{1,32}-[0-9a-f]{12}/graph\.yaml"))

    def write(number, content="facts: []\n", when=None):
        path = f"{prompts}/explore-{number:012x}/graph.yaml"
        relay.prepare(str(tmp_path / "runs" / "p1"), {path: content})
        if when is not None:
            os.utime(path, (when, when))
        return Path(path)
    return prompts, write


def test_the_relay_keeps_only_the_newest_snapshots_in_the_agent_s_tmp(tmp_path, monkeypatch):
    """Regression: every task's graph snapshot (up to 8 files of 4 MiB a call) was written into the agent's /tmp - a
    tmpfs, memory - and never removed: Cairn hands a failing task out again without a limit, so a long session (or a
    Cairn container gone wrong) filled it, and from then on no worker and nothing else in the sandbox could write
    there. Now the relay keeps the newest KEPT snapshots, KEPT_BYTES at most, and removes the older ones it wrote."""
    prompts, write = snapshot_world(tmp_path, monkeypatch)
    monkeypatch.setattr(relay, "KEPT", 5)
    written = [write(number, when=1_700_000_000 + number) for number in range(12)]
    kept = sorted(path.parent.name for path in prompts.glob("*/graph.yaml"))
    assert kept == sorted(path.parent.name for path in written[-5:]), "the newest five, the older ones gone"
    assert sorted(entry.name for entry in prompts.iterdir()) == kept, "their folders too"


def test_the_snapshots_are_bounded_in_bytes_and_a_running_call_s_are_kept(tmp_path, monkeypatch):
    prompts, write = snapshot_world(tmp_path, monkeypatch)
    monkeypatch.setattr(relay, "KEPT_BYTES", 3000)
    old = write(1, "x" * 1000, when=1_700_000_001)
    with relay.BUSY_LOCK:
        relay.BUSY[str(old.parent)] += 1  # (a worker that still runs with it: handle() holds it so)
    try:
        for number in range(2, 8):
            write(number, "y" * 1000, when=1_700_000_000 + number)
        left = sorted(path.parent.name for path in prompts.glob("*/graph.yaml"))
        assert old.parent.name in left, "a running call's snapshot is never removed"
        assert sum(path.stat().st_size for path in prompts.glob("*/graph.yaml")) <= 3000 + 1000
        assert f"explore-{7:012x}" in left and f"explore-{2:012x}" not in left
    finally:
        with relay.BUSY_LOCK:
            relay.BUSY[str(old.parent)] -= 1


def test_pruning_touches_only_snapshots_and_follows_no_link(tmp_path, monkeypatch):
    prompts, write = snapshot_world(tmp_path, monkeypatch)
    monkeypatch.setattr(relay, "KEPT", 1)
    write(1, when=1_700_000_001)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "graph.yaml").write_text("the agent's own file")
    (prompts / "linked-0123456789ab").symlink_to(outside)  # a link named like a snapshot's folder
    (prompts / "notes.txt").write_text("not a snapshot")
    other = prompts / "explore-00000000000a"
    other.mkdir()
    (other / "graph.yaml").write_text("old")
    (other / "more.txt").write_text("something else beside it")
    os.utime(other / "graph.yaml", (1_600_000_000, 1_600_000_000))
    write(2, when=1_700_000_002)
    assert (outside / "graph.yaml").read_text() == "the agent's own file"
    assert (prompts / "linked-0123456789ab").is_symlink() and (prompts / "notes.txt").exists()
    assert not (other / "graph.yaml").exists() and (other / "more.txt").exists(), "only the snapshot goes"
    assert not (prompts / f"explore-{1:012x}").exists() and (prompts / f"explore-{2:012x}" / "graph.yaml").exists()
    # A snapshots folder that is a link itself is never pruned through (prepare refuses it already).
    assert relay.prune(str(prompts / "linked-0123456789ab")) == 0


def test_a_call_holds_its_snapshots_while_its_worker_runs_and_lets_them_go_after(world):
    graph = world["prompts"] / "explore-0123456789ab" / "graph.yaml"
    graph.parent.mkdir(parents=True)
    graph.write_text("facts:\n- origin\n")
    seen = []
    real = relay.pump

    def pump(connection, child, *args, **kwargs):
        with relay.BUSY_LOCK:
            seen.append(relay.BUSY[str(graph.parent)])
        return real(connection, child, *args, **kwargs)
    relay.pump = pump
    try:
        status, _, _ = world["call"]("claude", "--session-id", UUID, "--dangerously-skip-permissions", "-p", "--",
                                     f"Read {graph} first")
    finally:
        relay.pump = real
    assert status == 0 and seen == [1]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and relay.BUSY[str(graph.parent)]:
        time.sleep(0.05)
    assert relay.BUSY[str(graph.parent)] == 0
