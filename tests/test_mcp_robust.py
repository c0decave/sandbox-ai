"""MCP servers that die before they answer: the reason, early refusals, a check for all (the user, 2026-11-25).

An agent in a session with `--mcp gowitness-tor` (the catalog server gowitness behind a Tor profile) got only
`{"error":{"code":-32000,"message":"sandbox-ai: the MCP server ended before it answered"}}` on initialize, twice, and
could not tell why (it sees no host log) - so it proposed to install gowitness in its own sandbox, around Tor and the
server's isolation. The same host had refused a start earlier because Podman's catatonit is missing there.

Every way a connection can die before initialize, through every path that starts or connects a server:

  - the reason reaches the agent: the relay's error says why in one short line (Podman's error line, the VPN guard's
    refusal, the server's exit status and its last line - redacted, bounded) and "the human sees more: sandbox-ai mcp
    show NAME". A process whose output closed but that is still ending (Podman removing its container, the VPN guard
    its own: longer than the 2 s waited until now - no reason at all was said) is said too;
  - a connection that gets no server (its socket gone, its folders changed, too many at once) is answered with why,
    never only closed;
  - what would end every connection (its image gone, no init for its containers, its rootfs gone) is refused before the
    server starts, with the fix, by a start AND by mcp start, mcp start --for, mcp attach --live and mcp check
    (ensure_running: runnable_problems);
  - mcp list / mcp show (the app, doctor) show the last failure with its time, the fix and the log;
  - `sandbox-ai mcp check [NAME...|--all]` starts each server as a start would, initialize + tools/list, stops it.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "d" * 64


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_mcp_robust_test", "agent_sandbox.py")
m = load("sandbox_mcp_robust_test", "mcp.py")
rt = load("runtime_mcp_robust_test", "runtime.py")


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    for variable, folder in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "cfg"), ("XDG_DATA_HOME", "data"),
                             ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(variable, str(tmp_path / folder))
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", raising=False)
    return tmp_path


def define(servers):
    config = m.config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(servers))


def read_answer(sock, timeout=20):
    sock.settimeout(timeout)
    received = b""
    while not received.endswith(b"\n"):
        data = sock.recv(65536)
        if not data:
            break
        received += data
    return json.loads(received.decode().strip().splitlines()[-1])


# --- the reason reaches the agent ----------------------------------------------------------------------------------

def ended_answer(script, name="gowitness", ended=True):
    """The answer an agent gets to initialize from a server process running SCRIPT (Python), through the relay."""
    process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    ours, theirs = socket.socketpair()
    tail = rt.StderrTail()
    thread = threading.Thread(target=rt.relay_server_stderr, args=(process.stderr, io.StringIO(), tail), daemon=True)
    thread.start()
    gate = rt.McpGateway(None, io.StringIO())
    session = rt.McpConnection(gate, ours, process, ended=(lambda: tail.why(process, thread)) if ended else None,
                               name=name)
    session.client_line(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n')
    session.pump()
    try:
        return read_answer(theirs)["error"]["message"]
    finally:
        process.kill()
        process.wait()


def test_a_crash_at_start_says_its_exit_status_its_last_line_and_what_the_human_runs():
    message = ended_answer("import sys; sys.stderr.write('Traceback ...\\nOSError: [Errno 28] No space left on "
                           "device: /tmp/chromium\\n'); sys.exit(3)")
    assert message.startswith("sandbox-ai: the MCP server ended before it answered (exit status 3: OSError: [Errno 28] "
                              "No space left on device"), message
    assert message.endswith("; the human sees more: sandbox-ai mcp show gowitness"), message


def test_a_process_still_ending_when_its_output_closed_is_said_too(monkeypatch):
    """The gowitness-tor case: the server's container ended (its output closed) but the VPN guard still removes it
    (podman rm) - until now the relay waited 2 s, found it running and said no reason at all."""
    monkeypatch.setattr(rt, "MCP_WHY_SECONDS", 1.0)
    script = ("import os, sys, time; sys.stderr.write('sandbox-ai mcp: vpn: MCP server refused: the VPN container "
              "abc is not running\\n'); sys.stderr.flush(); os.close(1); time.sleep(30)")
    began = time.monotonic()
    message = ended_answer(script)
    assert time.monotonic() - began < 15
    assert "(its output closed (it is still ending): sandbox-ai mcp: vpn: MCP server refused: the VPN container abc " \
           "is not running)" in message, message
    assert "sandbox-ai mcp show gowitness" in message


def test_podman_s_error_line_is_chosen_and_secrets_are_redacted():
    secret = "ghp_" + "B" * 36
    message = ended_answer("import sys; sys.stderr.write('Error: OCI runtime error: crun: the requested cgroup "
                           f"controller `memory` is not available {secret}\\nwarning: something\\n'); sys.exit(126)")
    assert "exit status 126: Error: OCI runtime error: crun: the requested cgroup controller" in message, message
    assert secret not in message and len(message) < 500


def test_an_instance_name_is_shown_as_its_server_s():
    assert rt.mcp_show_hint("gowitness.0a1b2c3d") == "the human sees more: sandbox-ai mcp show gowitness"
    assert rt.mcp_show_hint(None) == "the human sees more: sandbox-ai mcp list"
    assert rt.mcp_show_hint("bad name;rm") == "the human sees more: sandbox-ai mcp list"


def test_the_run_s_relay_says_its_bridge_ended_without_an_answer(tmp_path):
    """The agent's relay in the sandbox reaches the server's socket (McpSocket): a bridge on the host that closes the
    connection without an answer (stopped, crashed) is said so, with the server's name."""
    path = Path(rt.tempfile.mkdtemp(prefix="rb-", dir="/tmp")) / "server.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)

    def bridge():
        connection, _ = listener.accept()
        connection.recv(65536)
        connection.close()
    threading.Thread(target=bridge, daemon=True).start()
    upstream = rt.McpSocket(path)
    ours, theirs = socket.socketpair()
    session = rt.McpConnection(rt.McpGateway({"server": "binwalk", "rules": []}, io.StringIO(), plain=True), ours,
                               upstream)
    session.client_line(b'{"jsonrpc":"2.0","id":4,"method":"initialize","params":{}}\n')
    session.pump()
    answer = read_answer(theirs)
    assert answer["id"] == 4
    assert answer["error"]["message"] == (
        "sandbox-ai: the MCP server ended before it answered (its bridge on the host closed the connection without an "
        "answer: it was stopped or it ended); the human sees more: sandbox-ai mcp show binwalk"), answer
    listener.close()


def serve(tmp_path, command=None, spawn=None, env=None, name=None, monkeypatch=None):
    """mcp_serve on a short socket path in a thread: (path, thread)."""
    base = Path(rt.tempfile.mkdtemp(prefix="sv-", dir="/tmp"))
    path = base / "s.sock"
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    thread = threading.Thread(target=rt.mcp_serve, args=(str(path), command), daemon=True,
                              kwargs={"cwd": str(tmp_path), "spawn": spawn, "name": name})
    thread.start()
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return path


def ask(path, line=b'{"jsonrpc":"2.0","id":9,"method":"initialize","params":{}}\n'):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall(line)
        return read_answer(client)


def test_a_connection_to_a_server_whose_socket_is_gone_is_answered_with_why(tmp_path, monkeypatch):
    """The bridge's socket folder gone (the server stopped, its folder removed): the run's relay answers the agent
    with why and what the human runs, instead of closing the connection without a word."""
    def gone():
        raise FileNotFoundError(2, "No such file or directory")
    path = serve(tmp_path, spawn=gone, name="radare2", monkeypatch=monkeypatch)
    answer = ask(path)
    assert answer["id"] == 9
    assert answer["error"]["message"] == (
        "sandbox-ai: the MCP server is not running: its socket cannot be reached (No such file or directory); the "
        "human starts it again; the human sees more: sandbox-ai mcp show radare2"), answer


def test_a_host_bridge_refusing_a_connection_answers_it_and_records_why(tmp_path, monkeypatch):
    """Its folders changed since it started: refused (as before), now answered and recorded for mcp show."""
    folder = tmp_path / "work"
    folder.mkdir()
    info = os.stat(folder)
    records = tmp_path / "safeguards.jsonl"
    mounts = json.dumps([{"path": str(folder), "dev": info.st_dev, "ino": info.st_ino + 1}])
    path = serve(tmp_path, command=[sys.executable, "-c", "pass"], monkeypatch=monkeypatch,
                 env={"SANDBOX_AI_MCP_MOUNTS": mounts, "SANDBOX_AI_MCP_NAME": "binwalk",
                      "SANDBOX_AI_MCP_INSTANCE": "binwalk.12345678", "SANDBOX_AI_MCP_RECORDS": str(records)})
    answer = ask(path)
    message = answer["error"]["message"]
    assert message.startswith("sandbox-ai: the MCP server was not started for this connection (") and \
        "has been replaced since the server started" in message and \
        message.endswith("the human sees more: sandbox-ai mcp show binwalk"), message
    record = json.loads(records.read_text().splitlines()[-1])
    assert record["kind"] == "mcp-server" and record["run"] == "mcp-binwalk.12345678"
    assert "has been replaced" in record["why"] and "sandbox-ai mcp stop binwalk" in record["fix"]


def test_a_server_ending_with_status_0_before_its_answer_is_recorded_by_the_bridge(tmp_path, monkeypatch):
    """A non-zero end is the container watch's to record (with its fix); one that exits 0 or is still ending is the
    bridge's - else mcp show would show nothing of it."""
    records = tmp_path / "safeguards.jsonl"
    monkeypatch.setenv("SANDBOX_AI_MCP_RECORDS", str(records))
    monkeypatch.setenv("SANDBOX_AI_MCP_INSTANCE", "gowitness")
    message = ended_answer("import sys; sys.stderr.write('bye\\n')")
    assert "(exit status 0: bye)" in message
    record = json.loads(records.read_text().splitlines()[-1])
    assert record["what"] == "MCP server gowitness ended before it answered" and record["why"] == "exit status 0: bye"
    records.unlink()
    ended_answer("import sys; sys.exit(5)")
    assert not records.exists()  # (the watch records it, with its fix)


# --- what the watch records, and what mcp show and mcp list say --------------------------------------------------

def test_limits_podman_refuses_are_recorded_with_the_fix(isolated, monkeypatch):
    watch = m.ContainerWatch(isolated / "inst", "gowitness", "768m", {"memory": "2g", "pids": "512"})
    (isolated / "inst").mkdir()
    entry = watch.ended(126, False, "Error: OCI runtime error: crun: the requested cgroup controller `pids` is not "
                                    "available\n")
    assert "the requested cgroup controller `pids` is not available" in entry["why"]
    assert "sandbox-ai doctor" in entry["fix"] and "sandbox-ai mcp check gowitness" in entry["fix"]


def test_mcp_show_says_the_last_failure_its_fix_and_its_log(isolated, capsys):
    define({"gowitness": {"image": f"ghcr.io/c0decave/sandbox-ai-mcp-gowitness@{DIGEST}", "network": "full",
                          "memory": "2g", "pids": 512, "tmp": "768m"}})
    directory = m.state_dir("gowitness")
    directory.mkdir(parents=True)
    m.ContainerWatch(directory, "gowitness", "768m").ended(1, False, "Error: statfs /x: no such file or directory")
    m.show("gowitness")
    said = capsys.readouterr().out
    assert "last failure (" in said and "it ended with exit status 1: Error: statfs /x" in said, said
    assert f"its log: {directory / 'supervisor.log'}" in said
    assert f"  log: {directory / 'supervisor.log'}" in said
    found = next(e for e in m.server_list() if e["name"] == "gowitness")["last_problem"]
    assert found["log"] == str(directory / "supervisor.log") and found["time"]


# --- refused early, the same checks on every path ----------------------------------------------------------------

IMAGE = f"ghcr.io/c0decave/sandbox-ai-mcp-gowitness@{DIGEST}"


def test_ensure_running_refuses_a_missing_image_with_the_fix_before_anything_starts(isolated, monkeypatch):
    """mcp start --for, mcp attach --live and mcp check start a server through ensure_running, which skipped the start's
    image check: each connection then ended with "ended before it answered" (Podman runs it with --pull=never)."""
    define({"gowitness": {"image": IMAGE, "network": "none"}})
    monkeypatch.setattr(m, "image_present", lambda image, run=None: False)
    started = []
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **kw: started.append(a) or pytest.fail("started"))
    with pytest.raises(ValueError) as caught:
        m.ensure_running("gowitness", s.parse_args(["codex"]))
    assert f"its image {IMAGE} is not there" in str(caught.value)
    assert [fix["commands"] for fix in caught.value.fixes] == [[["podman", "pull", IMAGE]]]
    assert not started


def test_ensure_running_refuses_a_host_without_catatonit_for_an_image_without_an_init(isolated, monkeypatch):
    define({"binwalk": {"image": IMAGE, "network": "none"}})
    monkeypatch.setattr(m, "image_present", lambda image, run=None: True)
    monkeypatch.setenv("SANDBOX_AI_INIT_BINARY", "none")
    monkeypatch.setattr(m.hostinit_module(), "image_entrypoint", lambda image, podman=None, run=None: ["/srv/run"])
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **kw: pytest.fail("started"))
    with pytest.raises(ValueError) as caught:
        m.ensure_running("binwalk", s.parse_args(["codex"]))
    assert "needs an init process" in str(caught.value), caught.value
    # Its image's ENTRYPOINT an init (gowitness: /usr/bin/tini): no catatonit needed.
    monkeypatch.setattr(m.hostinit_module(), "image_entrypoint", lambda image, podman=None, run=None: ["/usr/bin/tini",
                                                                                                         "--", "x"])
    assert m.runnable_problems("binwalk", m.definitions()["binwalk"], s.parse_args(["codex"])) == ([], [])


def test_a_running_server_whose_image_was_removed_is_not_used_again(isolated, monkeypatch):
    define({"gowitness": {"image": IMAGE, "network": "none"}})
    monkeypatch.setattr(m, "status", lambda name: "running")
    monkeypatch.setattr(m, "start_instance", lambda name, a, lent_by=None: ("gowitness", {}, "same", None))
    monkeypatch.setattr(m, "image_present", lambda image, run=None: False)
    with pytest.raises(ValueError, match="is not there"):
        m.ensure_running("gowitness", s.parse_args(["codex"]))


def test_a_gone_rootfs_is_refused_under_bubblewrap(isolated, monkeypatch):
    define({"tiny": {"rootfs": {"sha256": "e" * 64, "path": str(isolated / "gone"), "entrypoint": ["/x"]},
                     "network": "none"}})
    problems, _ = m.runnable_problems("tiny", {"rootfs": {"path": str(isolated / "gone")}},
                                      s.parse_args(["codex", "--backend", "bwrap"]))
    assert problems and "its rootfs is gone" in problems[0]


def test_start_problems_says_the_same_as_ensure_running(isolated, monkeypatch):
    define({"gowitness": {"image": IMAGE, "network": "none"}})
    monkeypatch.setattr(m, "image_present", lambda image, run=None: False)
    fixes = []
    problems = m.start_problems(s.parse_args(["codex"]), ["gowitness"], fixes=fixes)[0]
    with pytest.raises(ValueError) as caught:
        m.ensure_running("gowitness", s.parse_args(["codex"]))
    assert problems == [str(caught.value)]
    assert fixes == caught.value.fixes


# --- mcp check --------------------------------------------------------------------------------------------------

def test_check_reports_ok_the_reason_and_the_fix_and_stops_what_it_started(isolated, monkeypatch):
    define({"good": {"image": IMAGE, "network": "none"}, "bad": {"image": IMAGE.replace("gowitness", "x"),
                                                                    "network": "none", "memory": "512m"}})
    monkeypatch.setattr(m, "image_present", lambda image, run=None: "x@" not in image)
    monkeypatch.setattr(m, "below_recommended", lambda d: {"memory": "2g"} if "x@" in d.get("image", "") else {})
    started, stopped = [], []

    def ensure(name, a, vpn=None):
        started.append((name, a.mcp_workspace, vpn))
        m.runnable_problems(name, m.definitions()[name], a)  # (the real ones would refuse "bad" first)
        return "started"
    monkeypatch.setattr(m, "ensure_running", ensure)
    monkeypatch.setattr(m, "stop", lambda name, timeout=30: stopped.append(name) or 0)
    rows = m.check_servers(["good", "bad"], out=lambda text: None, shake=lambda sock, timeout: (["screenshot"], None))
    rows.append(m.check_server("helper-x", {"helper-x": {"helper": "claude"}}))  # (a start's --helper)
    by = {row["server"]: row for row in rows}
    assert by["good"]["result"] == "ok" and by["good"]["tools"] == 1 and by["good"]["started"]
    assert by["bad"]["result"] == "failed" and "is not there" in by["bad"]["reason"]
    assert by["bad"]["fix"] == f"podman pull {IMAGE.replace('gowitness', 'x')}"
    assert by["bad"]["note"] == "below the limits its catalog recommends (2g): sandbox-ai mcp set bad --recommended"
    assert by["helper-x"]["result"] == "skipped"
    assert started == [("good", str(m.check_workspace()), None)] and stopped == ["good"]
    assert "FAILED" in m.check_line(by["bad"]) and "fix: podman pull" in m.check_line(by["bad"])


def test_check_says_a_handshake_that_failed_with_the_watch_s_fix(isolated, monkeypatch):
    define({"good": {"image": IMAGE, "network": "none"}})
    monkeypatch.setattr(m, "image_present", lambda image, run=None: True)
    monkeypatch.setattr(m, "ensure_running", lambda name, a, vpn=None: "running")
    monkeypatch.setattr(m, "stop", lambda name, timeout=30: pytest.fail("one that ran is left running"))
    directory = m.state_dir("good")
    directory.mkdir(parents=True)
    m.ContainerWatch(directory, "good", "64m").ended(137, False, "")
    row = m.check_servers(["good"], out=lambda text: None,
                          shake=lambda sock, timeout: (None, "the MCP server ended before it answered (exit status "
                                                                "137, nothing on its error output); the human sees more:"
                                                                " sandbox-ai mcp show good"))[0]
    assert row["result"] == "failed" and "exit status 137" in row["reason"]
    assert "sandbox-ai mcp set good --memory" in row["fix"]


def test_check_cli_needs_names_or_all(isolated):
    with pytest.raises(ValueError, match="--all"):
        m.cli(["check"])
    with pytest.raises(ValueError, match="--all"):
        m.cli(["check", "x", "--all"])
    with pytest.raises(ValueError, match="Unknown MCP server: x"):
        m.cli(["check", "x"])


# --- end to end: ./sandbox-ai with Podman stood in for ----------------------------------------------------------

# rootless Podman: an image "...-ok" runs a small MCP server; "...-crash" ends at once with a line about its full
# /tmp; "...-limits" is refused by crun for its limits (as on a host whose cgroups are not delegated). Everything else
# goes to live_fakes' Podman.
STAND_IN = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STAND_IN_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
image = next((a for a in args if a.startswith("ghcr.io/")), None)
if args[:1] == ["run"] and image and "--pod" not in args:
    if "-crash@" in image:
        print("Traceback (most recent call last):", file=sys.stderr)
        print("OSError: [Errno 28] No space left on device: '/tmp/chromium-profile'", file=sys.stderr)
        sys.exit(1)
    if "-limits@" in image:
        print("Error: OCI runtime error: crun: the requested cgroup controller `pids` is not available", file=sys.stderr)
        sys.exit(126)
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "ok"}}
        elif message.get("method") == "tools/list":
            result = {"tools": [{"name": "screenshot", "inputSchema": {"type": "object"}},
                                {"name": "report", "inputSchema": {"type": "object"}}]}
        elif "id" in message:
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    sys.exit(0)
os.execv(os.environ["STAND_IN_REAL"], [os.environ["STAND_IN_REAL"], *args])
'''


def image(kind):
    return f"ghcr.io/c0decave/sandbox-ai-mcp-{kind}@{DIGEST}"


@pytest.fixture()
def host(tmp_path):
    found = Host(tmp_path / "h")
    real = found.bin / "podman-real"
    (found.bin / "podman").rename(real)
    (found.bin / "podman").write_text(STAND_IN.replace("PYTHON", sys.executable, 1))
    (found.bin / "podman").chmod(0o755)
    found.env.update(STAND_IN_LOG=str(found.directory / "stand-in.log"), STAND_IN_REAL=str(real))
    found.env.pop("SANDBOX_AI_MCP_NO_IMAGE_CHECK", None)
    found.add_image(image("ok"), image("crash"), image("limits"))
    config = found.home / ".config/sandbox-ai/mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({kind: {"image": image(kind), "network": "none"}
                                  for kind in ("ok", "crash", "limits", "gone")}))
    yield found
    state = found.home / ".local/state/sandbox-ai/mcp"
    for directory in state.iterdir() if state.is_dir() else ():
        found.run("mcp", "stop", directory.name, timeout=60)


def test_e2e_mcp_check_all(host):
    done = host.run("mcp", "check", "--all", "--json", "--timeout", "60", timeout=600)
    rows = {row["server"]: row for row in json.loads(done.stdout)}
    assert done.returncode == 1, done.stderr[-3000:]
    assert rows["ok"]["result"] == "ok" and rows["ok"]["tools"] == 2, rows["ok"]
    assert rows["ok"]["tool_names"] == ["screenshot", "report"]
    crash = rows["crash"]
    assert crash["result"] == "failed"
    assert crash["reason"].startswith("the MCP server ended before it answered (exit status 1: OSError: [Errno 28] "
                                      "No space left on device"), crash
    assert crash["reason"].endswith("the human sees more: sandbox-ai mcp show crash")
    assert "read the server's log" in crash["fix"], crash
    limits = rows["limits"]
    assert "the requested cgroup controller `pids` is not available" in limits["reason"], limits
    assert "sandbox-ai doctor" in limits["fix"], limits
    gone = rows["gone"]
    assert "its image " + image("gone") + " is not there" in gone["reason"] and \
        gone["fix"] == f"podman pull {image('gone')}", gone
    # Nothing it started runs on; nothing was started for the server whose image is gone.
    assert not any(path.name.startswith("gone") for path in (host.home / ".local/state/sandbox-ai/mcp").iterdir())
    listed = host.run("mcp", "list", "--json")
    assert all(entry["status"] == "stopped" for entry in json.loads(listed.stdout)), listed.stdout
    # The last failure is there for the human: mcp show and mcp list.
    shown = host.run("mcp", "show", "crash")
    assert "last failure (" in shown.stdout and "No space left on device" in shown.stdout, shown.stdout
    assert "its log: " in shown.stdout and "supervisor.log" in shown.stdout


def test_e2e_mcp_check_says_each_line(host):
    done = host.run("mcp", "check", "ok", "gone", "--timeout", "60", timeout=600)
    assert done.returncode == 1
    assert "ok: ok - 2 tools" in done.stdout, done.stdout
    assert "gone: FAILED - MCP server gone: its image" in done.stdout and "fix: podman pull" in done.stdout
    assert "1 of 2 ok; failed: gone" in done.stdout


def test_e2e_mcp_start_refuses_without_catatonit_and_without_the_image(host):
    done = host.run("mcp", "start", "ok", env={"SANDBOX_AI_INIT_BINARY": "none"})
    assert done.returncode == 1 and "needs an init process" in done.stderr, done.stderr
    done = host.run("mcp", "start", "gone")
    assert done.returncode == 1 and "is not there" in done.stderr, done.stderr
    assert not (host.home / ".local/state/sandbox-ai/mcp/gone").exists()


def test_e2e_an_agent_s_connection_to_a_crashing_server_says_why(host):
    """The connection as an agent's relay makes it (runtime.py's bridge), to a server mcp start started."""
    done = host.run("mcp", "start", "crash", timeout=120)
    assert done.returncode == 0, done.stderr
    sock = host.home / ".local/state/sandbox-ai/mcp/crash/sock/server.sock"
    tools, why = m.handshake(sock, timeout=60)
    assert tools is None and "exit status 1: OSError: [Errno 28] No space left on device" in why, why
    assert why.endswith("the human sees more: sandbox-ai mcp show crash")


def test_behind_tor_too_the_missing_image_is_refused_before_its_tor_container(isolated, monkeypatch):
    """The user's gowitness-tor: a catalog server with a Tor profile of its own (the instance's own Tor container
    NAME-VPN) and one put behind Tor by mcp start --vpn: refused before any container is started."""
    define({"gowitness-tor": {"image": IMAGE, "network": "full", "vpn": "tor1", "memory": "2g", "pids": 512,
                              "tmp": "768m"}})
    vpn = m.launcher().load_vpn_module()
    monkeypatch.setattr(vpn, "load_profile", lambda name: {"name": name, "type": "tor"})
    monkeypatch.setattr(m.launcher(), "load_vpn_module", lambda: vpn)
    monkeypatch.setattr(m, "image_present", lambda image, run=None: False)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **kw: pytest.fail("started"))
    for given in (None, "tor1"):
        with pytest.raises(ValueError) as caught:
            m.ensure_running("gowitness-tor", s.parse_args(["codex"]), given)
        assert f"MCP server gowitness-tor: its image {IMAGE} is not there" in str(caught.value)
        assert caught.value.fixes[0]["commands"] == [["podman", "pull", IMAGE]]


def test_doctor_says_a_recent_failure_and_limits_below_the_catalog_s():
    doctor = load("doctor_mcp_robust_test", "doctor.py")
    now = time.time()
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30 * 86400))
    problem = {"time": recent, "what": "MCP server gowitness's process ended while it was used",
               "why": "podman could not start its container (exit status 126): Error: crun: ...",
               "fix": "sandbox-ai doctor shows the cgroup delegation", "log": "/x/supervisor.log"}
    servers = [{"name": "gowitness", "last_problem": problem, "below_recommended": {}},
               {"name": "jadx", "last_problem": {**problem, "time": old}, "below_recommended": {"memory": "2g"}},
               {"name": "time", "last_problem": None, "below_recommended": {}}]
    state, name, detail, fix = doctor.check_mcp_servers(servers, now=now)
    assert (state, name) == ("warn", "MCP servers")
    assert "gowitness: " + recent + ": podman could not start its container" in detail
    assert "jadx: below the limits its catalog recommends (2g)" in detail and "jadx: " + old not in detail
    assert "(its log: /x/supervisor.log)" in fix and "sandbox-ai mcp set jadx --recommended" in fix
    assert fix.endswith("then: sandbox-ai mcp check gowitness")
    assert doctor.check_mcp_servers(servers[2:], now=now)[0] == "ok"
    assert doctor.check_mcp_servers([], now=now)[0] == "skip"


# --- item 7: the limits a catalog recommends (rootfs.json's "limits") reach the definition ---------------------------

GOWITNESS = {"id": "mcp:gowitness", "name": "gowitness", "title": "Web screenshots (gowitness, Chromium)", "kind": "oci",
             "image": "ghcr.io/c0decave/sandbox-ai-mcp-gowitness", "digest": DIGEST, "network": "full",
             "tor": {"support": "yes"}}


def test_e2e_mcp_add_applies_the_catalog_s_limits_also_as_another_name_behind_tor_and_set_recommended_restores(
        host, tmp_path):
    """The user's gowitness-tor: `mcp add gowitness --as gowitness-tor --vpn PROFILE` gets the 2g, 512 processes and
    /tmp of 768m its catalog's rootfs.json recommends (said so); `mcp set --recommended` gives them back after a lower
    value was set."""
    catalog = tmp_path / "cat" / "catalog.json"
    catalog.parent.mkdir()
    catalog.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [GOWITNESS]}))
    (catalog.parent / "rootfs.json").write_text(json.dumps({"version": 1, "rootfs": {}, "limits": {
        "gowitness": {"memory": "2g", "pids": 512, "tmp": "768m"}}}))
    host.add_image(f"{GOWITNESS['image']}@{DIGEST}")
    assert host.run("vpn", "add", "tor1", "--tor").returncode == 0
    for local, extra in (("gowitness", []), ("gowitness-tor", ["--as", "gowitness-tor", "--vpn", "tor1"])):
        added = host.run("mcp", "add", "gowitness", "--catalog", str(catalog), "--no-pull", "--unsigned", *extra,
                         timeout=120)
        assert added.returncode == 0, added.stderr
        said = added.stdout + added.stderr
        assert f"{local} gets the limits its catalog entry recommends: 2g, 512 processes, /tmp 768m" in said, said
        definition = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())[local]
        assert (definition["memory"], definition["pids"], definition["tmp"]) == ("2g", 512, "768m"), definition
    assert json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["gowitness-tor"]["vpn"] == "tor1"
    assert host.run("mcp", "set", "gowitness-tor", "--memory", "512m", "--tmp", "64m").returncode == 0
    listed = host.run("mcp", "list")
    assert "below the limits its catalog recommends" in listed.stdout and \
        "sandbox-ai mcp set gowitness-tor --recommended" in listed.stdout, listed.stdout
    restored = host.run("mcp", "set", "gowitness-tor", "--recommended")
    assert restored.returncode == 0, restored.stderr
    definition = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["gowitness-tor"]
    assert (definition["memory"], definition["pids"], definition["tmp"], definition["vpn"]) == ("2g", 512, "768m",
                                                                                                 "tor1"), definition


def test_a_vpn_profile_that_is_gone_is_refused_before_anything_starts(isolated, monkeypatch):
    """A start's own check says "its VPN: ..." (start_problems); mcp start --for and attach --live started its VPN
    container instead, which never came up."""
    define({"gowitness-tor": {"image": IMAGE, "network": "full", "vpn": "gone1"}})
    monkeypatch.setattr(m, "image_present", lambda image, run=None: True)
    monkeypatch.setattr(m, "image_init", lambda name, definition, found=None: True)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **kw: pytest.fail("started"))
    with pytest.raises(ValueError) as caught:
        m.ensure_running("gowitness-tor", s.parse_args(["codex"]))
    assert str(caught.value).startswith("MCP server gowitness-tor: its VPN: "), caught.value
    assert "gone1" in str(caught.value)
