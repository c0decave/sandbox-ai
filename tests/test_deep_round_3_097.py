"""Deep test round 3 of 0.1.96 (chaos, concurrency and hostile input): `sandbox-ai shell`, the session bridge (on one
host and across hosts), and the agents OpenHands, nanobot and Letta Code, cut by failure mode - many at once,
interruption (SIGKILL of the broker, the stub, the add), resource exhaustion, hostile or damaged input, time.

Each test is named after the bug it showed first (a failing test before its fix). Real processes wherever a sandbox
allows them: the shell relay and client through runtime.py's entry points, the bridge's broker as the process `bridge
add` starts, the supervisors' control sockets and MCP relays of test_bridge.py's world.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = str(ROOT / "sandbox" / "runtime.py")


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as stream:
            return stream.read().split(") ")[1].startswith("Z")
    except OSError:
        return True


def peak_memory(pid):
    """The highest resident memory of process PID so far, in bytes (VmHWM)."""
    with open(f"/proc/{pid}/status") as stream:
        for line in stream:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    return 0


# --- sandbox-ai shell -----------------------------------------------------------------------------------------------

@pytest.fixture
def shell_relay(tmp_path):
    """runtime.py --internal-shell-relay as the sandbox's guard starts it (a process of its own); (socket, process)."""
    work = tmp_path / "work"
    work.mkdir()
    path = tmp_path / "shell.sock"
    server = subprocess.Popen([sys.executable, "-I", "-S", RUNTIME, "--internal-shell-relay", str(path)], cwd=work)
    assert wait_for(path.exists, 10)
    yield str(path), server
    server.kill()
    server.wait()


def feed(pipe, total, chunk=1 << 20):
    """Write TOTAL bytes into PIPE (a file object), then close it; never raises (the reader may go)."""
    block = b"y\n" * (chunk // 2)
    try:
        while total > 0:
            pipe.write(block[:total])
            total -= min(total, len(block))
    except (BrokenPipeError, ValueError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def test_shell_input_for_a_command_that_does_not_read_it_is_not_held_without_bound(shell_relay):
    """`yes | sandbox-ai shell NAME -n -- sleep 60`: the relay inside the sandbox held every byte of the input the
    command had not read yet (64 MiB of input: 64 MiB more of the relay's memory, until the sandbox's limit). It holds
    at most SHELL_PENDING_LIMIT now; the rest waits on the client's side, and nothing is lost."""
    path, server = shell_relay
    total = 64 << 20
    before = peak_memory(server.pid)  # (runtime.py itself: some 50 MiB)
    client = subprocess.Popen([sys.executable, "-I", "-S", RUNTIME, "--internal-shell-client", path, "pipe",
                               "sh", "-c", "sleep 3; wc -c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    writer = threading.Thread(target=feed, args=(client.stdin, total), daemon=True)
    writer.start()
    out = client.stdout.read()
    client.wait(60)
    writer.join(10)
    assert client.returncode == 0 and int(out.split()[0]) == total, "every byte reached the command"
    held = peak_memory(server.pid) - before
    assert held < 16 << 20, f"the relay held {held >> 20} MiB more"


def test_shell_whose_input_is_held_still_ends_with_its_client(shell_relay, tmp_path):
    """The relay reads no more input while it holds its limit: the client going away (the terminal closed, kill -9)
    must still end what the shell started."""
    path, server = shell_relay
    marker = tmp_path / "work" / "pid"
    client = subprocess.Popen([sys.executable, "-I", "-S", RUNTIME, "--internal-shell-client", path, "pipe",
                               "sh", "-c", f"echo $$ > {shlex.quote(str(marker))}; exec sleep 300"],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    threading.Thread(target=feed, args=(client.stdin, 32 << 20), daemon=True).start()
    assert wait_for(lambda: marker.exists() and marker.read_text().strip(), 10)
    pid = int(marker.read_text())
    time.sleep(1)  # the relay holds its limit by now
    client.kill()
    client.wait()
    assert wait_for(lambda: gone(pid), 15), "the shell's command outlived its client"


def test_shell_echoing_a_large_input_does_not_deadlock(shell_relay):
    """Input and output at once in quantity (`cat big | sandbox-ai shell NAME -n -- cat`): neither side waits on the
    other for ever (the client sends without blocking and reads the relay's output meanwhile)."""
    path, server = shell_relay
    total = 48 << 20
    client = subprocess.Popen([sys.executable, "-I", "-S", RUNTIME, "--internal-shell-client", path, "pipe", "cat"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    threading.Thread(target=feed, args=(client.stdin, total), daemon=True).start()
    received = 0
    while True:
        chunk = client.stdout.read1(1 << 20) if hasattr(client.stdout, "read1") else client.stdout.read(1 << 20)
        if not chunk:
            break
        received += len(chunk)
    assert client.wait(60) == 0 and received == total


def test_shell_twenty_at_once_each_get_their_own_output_and_status(shell_relay):
    """(Missing; it passed.) Twenty `sandbox-ai shell NAME -n -- ...` at the same moment through one relay: each gets
    exactly its own output, error output and exit status, with input of its own."""
    path, _ = shell_relay
    clients = []
    for number in range(20):
        clients.append(subprocess.Popen(
            [sys.executable, "-I", "-S", RUNTIME, "--internal-shell-client", path, "pipe", "sh", "-c",
             f"read line; echo \"$line {number}\"; echo err{number} >&2; exit {number}"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    for number, client in enumerate(clients):
        out, err = client.communicate(f"in{number}\n".encode(), timeout=60)
        assert (client.returncode, out, err) == (number, f"in{number} {number}\n".encode(), f"err{number}\n".encode())


def shell_world(tmp_path, monkeypatch):
    """A running start "web" of test_shell.py's kind whose shell relay serves for real; test_shell's module."""
    import test_shell as ts
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for folder in ("runtime", "proc"):
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(ts.sessions, "PROC", tmp_path / "proc")
    monkeypatch.setattr(ts.st.module("net"), "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(ts.sessions.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(ts.sessions, "activity_event", lambda *args, **kwargs: None)
    ts.make_start("web", tmp_path, pid=41, sockets=("attach.sock",))
    ts.relay(tmp_path / "state" / "sandbox-ai" / "sessions" / "web" / "tmux")
    return ts


def test_shell_command_line_too_long_for_the_relay_is_said_as_such(tmp_path, monkeypatch, capfd):
    """`sandbox-ai shell NAME -- COMMAND` with a command line of more than 256 KiB (a long argument list built by a
    script): the relay closed the connection without a word and the shell said "ended without its exit status (the
    sandbox ended, or its shell relay went)" - neither of which had happened. It is refused before anything is sent
    now, with the limit and what to do instead."""
    ts = shell_world(tmp_path, monkeypatch)
    words = ["x" * 100000] * 3
    assert ts.st.main(["shell", "web", "-n", "--", "echo", *words]) != 0
    err = capfd.readouterr().err
    assert "at most 262144 bytes" in err and "standard input" in err and "relay went" not in err, err
    status = ts.st.main(["shell", "web", "-n", "--", "sh", "-c", "exit 4"])
    assert status == 4, "the relay serves on"


def test_shell_in_a_session_paused_under_it_says_so_instead_of_hanging_silently(tmp_path, monkeypatch, capfd):
    """`sandbox-ai pause NAME` while a shell is open in NAME froze its relay: the shell's terminal waited without a
    word (keys went nowhere, Ctrl-C too) until the session was resumed. It says now that the session is paused and
    how to go on; the shell itself goes on after the resume."""
    import test_shell as ts
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for folder in ("runtime", "proc"):
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(ts.sessions, "PROC", tmp_path / "proc")
    monkeypatch.setattr(ts.st.module("net"), "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(ts.sessions.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(ts.sessions, "activity_event", lambda *args, **kwargs: None)
    ts.make_start("web", tmp_path, pid=41, sockets=("attach.sock",))
    directory = tmp_path / "state" / "sandbox-ai" / "sessions" / "web"
    ts.relay(directory / "tmux")

    def pause_then_resume():  # (as sandbox-ai pause and resume mark it; the relay itself is not frozen here)
        time.sleep(0.5)
        (directory / "paused").touch()
        time.sleep(2.5)
        (directory / "paused").unlink()
    threading.Thread(target=pause_then_resume, daemon=True).start()
    status = ts.st.main(["shell", "web", "-n", "--", "sh", "-c", "sleep 5; echo done"])
    out, err = capfd.readouterr()
    assert status == 0 and "done" in out
    assert err.count("web is paused: this shell waits until it is resumed (sandbox-ai resume web") == 1, err


# --- the session bridge on one host: interruption and concurrency -------------------------------------------------

from test_bridge import world, call, text, tools, b, ap  # noqa: E402,F401 - the world of test_bridge.py
from test_bridge_remote import two, remote_state  # noqa: E402,F401 - the other host (its fixture)


def dead_pid():
    """The number of a process that has ended (and was reaped)."""
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def kill_broker(world, ident):
    """What SIGKILL does to a bridge's broker: it stops serving at once and closes nothing (its relays stay attached
    in both runs, its record says it lives), and its process is gone."""
    broker = world.broker(ident)
    broker.closed = True  # (close() does nothing from here on: a killed process runs no cleanup)
    broker.stopping.set()
    for listener in list(broker.listeners.values()):
        listener.close()
    b.change(ident, lambda current: current.update(broker_pid=dead_pid(), broker_start=None))


def test_bridge_whose_broker_was_killed_leaves_nothing_after_revoke_all(world, capsys):
    """kill -9 of a bridge's broker (or the machine's OOM killer) left its MCP server attached in both runs and in
    Claude Code's ~/.claude.json until the runs ended; `bridge list` no longer listed it and `bridge revoke --all`
    passed it over. revoke --all closes what such a broker left now, and says so."""
    alpha, beta = world.make("alpha"), world.make("beta")
    ident = world.add("alpha", "beta", "message")["id"]
    kill_broker(world, ident)
    assert b.listing(True)[0]["state"] == "ended"
    assert alpha.relays.names() == ["bridge-beta"] and beta.relays.names() == ["bridge-alpha"]  # what it left
    assert b.main(["revoke", "--all"]) == 0
    assert alpha.relays.names() == [] and beta.relays.names() == [], "revoke --all closes what a killed broker left"
    assert "mcpServers" not in json.loads((alpha.home / ".claude.json").read_text())
    assert ident in capsys.readouterr().out
    assert b.load(ident)["state"] in ("ended", "revoked")


def test_bridge_add_after_a_killed_broker_closes_what_it_left(world):
    """A new bridge of the same two sessions after a killed broker: the old one's relays are closed first, so the new
    one gets the peer's own name (bridge-beta, not bridge-ID beside a dead bridge-beta)."""
    alpha, beta = world.make("alpha"), world.make("beta")
    ident = world.add("alpha", "beta", "message")["id"]
    kill_broker(world, ident)
    data = world.add("alpha", "beta", "message")
    assert data["id"] != ident and data["a"]["server"] == "bridge-beta"
    assert alpha.relays.names() == ["bridge-beta"] and beta.relays.names() == ["bridge-alpha"]
    assert "broker" in b.load(ident)["reason"]


def test_bridge_add_killed_while_it_starts_does_not_block_the_sessions_for_ever(world):
    """`bridge add` killed (Ctrl-C twice, kill -9, the terminal closed) after it wrote the bridge's record and before
    its broker ran: the record said "starting" for ever, `bridge list` listed it, and every later `bridge add` of the
    same two sessions was refused as bridged already."""
    world.make("alpha"), world.make("beta")
    child = os.fork()
    if child == 0:  # the add, in a process of its own; its broker never comes
        try:
            b.add("alpha", "beta", "message", ask="never", out=lambda line: None, spawn=lambda ident: time.sleep(600))
        finally:
            os._exit(0)
    try:
        assert wait_for(lambda: any(b.load(ident).get("state") == "starting" for ident in b.all_ids()), 20)
    finally:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
    assert b.listing() == [], "a start nobody makes any more is no bridge"
    assert world.add("alpha", "beta", "message")["state"] == "active"


def test_two_bridge_adds_of_the_same_sessions_at_once_make_one_bridge(world, monkeypatch):
    """Two `bridge add alpha beta` at the same moment (two terminals, the app's dialog and a script): each looked for a
    bridge of the two before either had written its record, and both were made."""
    world.make("alpha"), world.make("beta")
    real, entered = b.server_name, threading.Event()

    def slow(peer, ident):  # (between the look for another bridge and the record: where the other add came in)
        entered.set()
        time.sleep(0.5)
        return real(peer, ident)
    monkeypatch.setattr(b, "server_name", slow)
    results = []

    def add():
        try:
            results.append(world.add("alpha", "beta", "message")["state"])
        except ValueError as exc:
            results.append(str(exc))
    threads = [threading.Thread(target=add, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(results, key=len)[0] == "active" and "bridged already" in sorted(results, key=len)[1], results
    assert len(b.listing()) == 1


def test_bridge_write_answered_twice_or_late_writes_once_or_not_at_all(world, monkeypatch):
    """(A test that was missing; it passed.) The app and `approvals deny` answering one write question at once, and an
    answer that comes after the question timed out: the first answer decides, the second is told there is no open
    question, and a late allow writes nothing."""
    alpha, beta = world.make("alpha"), world.make("beta")
    (beta.folder / "notes.txt").write_text("one\n")
    world.add("alpha", "beta", "files:rw:.", ask="never")
    result = {}
    waiting = threading.Thread(target=lambda: result.update(reply=call(alpha, "bridge-beta", "write_file", {
        "path": "notes.txt", "content": "two\n"})), daemon=True)
    waiting.start()
    assert wait_for(lambda: ap.questions(ap.bridge_runs()), 10)
    ident = ap.questions(ap.bridge_runs())[0][2]["id"]
    ap.answer(ident, "allow", "once", runs=ap.bridge_runs(), out=lambda line: None)
    with pytest.raises(ValueError, match="no open question"):
        ap.answer(ident, "deny", "once", runs=ap.bridge_runs(), out=lambda line: None)
    waiting.join(15)
    assert not result["reply"]["isError"] and (beta.folder / "notes.txt").read_text() == "two\n"
    # Late: the question timed out (denied); an allow after it is refused, and nothing is written.
    monkeypatch.setattr(b, "WRITE_QUESTION_SECONDS", 1)
    late = call(alpha, "bridge-beta", "write_file", {"path": "notes.txt", "content": "three\n"})
    assert late["isError"] and "no answer within 1 s" in text(late)
    timed_out = [path.stem for path in b.state_root().glob("*/approvals/*.json")
                 if path.with_suffix(".done").exists() and not path.with_suffix(".answer").exists()]
    assert len(timed_out) == 1
    for question_id in timed_out:
        with pytest.raises(ValueError, match="no open question"):
            ap.answer(question_id, "allow", "once", runs=ap.bridge_runs(), out=lambda line: None)
    assert (beta.folder / "notes.txt").read_text() == "two\n"


def test_bridge_until_holds_across_clock_jumps(world):
    """(Missing; it passed.) --until is wall-clock time: a clock set back does not end a bridge early, one set
    forward past --until ends it at the next look; the rate limit counts on a clock that never jumps."""
    world.make("alpha"), world.make("beta")
    data = world.add("alpha", "beta", "message", until="10m", limits=["rate=6"])
    broker = world.broker(data["id"])
    broker.clock = lambda: time.time() - 86400 * 365  # set back a year
    time.sleep(0.5)
    assert b.load(data["id"])["state"] == "active"
    alpha = world.runs["alpha"]
    sent = [call(alpha, "bridge-beta", "send_message", {"text": str(n)}) for n in range(8)]
    assert sum(1 for reply in sent if reply["isError"] and "calls a minute" in text(reply)) >= 2
    broker.clock = lambda: time.time() + 3600  # forward past --until
    assert wait_for(lambda: b.load(data["id"])["state"] == "ended", 10)
    assert "time ran out" in b.load(data["id"])["reason"]


# --- damaged bridge records -----------------------------------------------------------------------------------------

import live_fakes  # noqa: E402

DAMAGED = {"11111111": "[]",  # valid JSON of another shape
           "22222222": json.dumps({"id": "22222222", "state": "ended"}),  # parts missing
           "33333333": json.dumps({"id": "33333333", "state": "ended", "a": "x", "b": None, "grants": 5}),
           "44444444": ""}  # cut to nothing (a crash before the disk had it)


def plant_damaged(root):
    for ident, content in DAMAGED.items():
        (root / ident / "approvals").mkdir(parents=True)
        (root / ident / "bridge.json").write_text(content)
        (root / ident / "approvals" / f"{ident[0]}-1-1.json").write_text(json.dumps({"id": f"{ident[0]}-1-1",
                                                                                      "kind": "bridge"}))


def test_damaged_bridge_record_does_not_break_bridge_list_show_or_approvals(tmp_path):
    """One damaged bridge record (valid JSON of another shape, parts missing) made `bridge list`, `list --all` and
    `revoke --all` end in a traceback, and `sandbox-ai approvals list` too - so no question of any sandbox could be
    listed or answered any more. Each command says now which record is damaged and what to do."""
    host = live_fakes.Host(tmp_path / "host")
    plant_damaged(host.home / ".local/state/sandbox-ai/bridges")
    for args in (["bridge", "list"], ["bridge", "list", "--all"], ["bridge", "list", "--all", "--json"]):
        done = host.run(*args, timeout=120)
        assert done.returncode == 0 and "Traceback" not in done.stderr, done.stderr
        assert "33333333: its record is damaged" in done.stderr and "remove its folder" in done.stderr
    assert json.loads(host.run("bridge", "list", "--all", "--json", timeout=120).stdout) == []
    done = host.run("bridge", "show", "22222222", timeout=120)
    assert done.returncode == 1 and "Traceback" not in done.stderr and "record is damaged" in done.stderr
    done = host.run("approvals", "list", "--json", timeout=120)
    assert done.returncode == 0 and "Traceback" not in done.stderr, done.stderr
    assert json.loads(done.stdout) == [], "a damaged bridge's questions wait for no broker"
    # The parts missing in a record of a broker that runs: its questions are listed (it may still ask), no crash.
    alive = host.home / ".local/state/sandbox-ai/bridges/55555555"
    (alive / "approvals").mkdir(parents=True)
    (alive / "bridge.json").write_text(json.dumps({"id": "55555555", "state": "active", "broker_pid": os.getpid()}))
    done = host.run("approvals", "list", "--json", timeout=120)
    assert done.returncode == 0 and "Traceback" not in done.stderr, done.stderr


def test_bridge_across_hosts_ends_on_the_other_host_when_the_connection_goes_silent(two, world, monkeypatch):
    """An SSH connection that drops without a word (a laptop that sleeps, Wi-Fi that changes): this machine's ssh
    gives up after its keepalives, but sshd on the other host keeps the stub's end open (for hours without
    ClientAliveInterval), so the other host's half stayed "active" and its session's agent kept the bridge's server.
    The halves greet each other now and then; a half that hears nothing from the other for LINK_IDLE ends it."""
    for module in (two.here, two.there):
        monkeypatch.setattr(module, "PING_INTERVAL", 0.3)
        monkeypatch.setattr(module, "LINK_IDLE", 2.0)
    alpha, beta = world.make("alpha"), world.make("beta")
    ident = world.add("alpha", "host1:beta", "message", both=True)["id"]
    time.sleep(3)  # idle, but the greetings keep it: no false end
    assert b.load(ident)["state"] == "active" and remote_state(two, ident)["state"] == "active"
    local = world.broker(ident)
    local.closed = True  # this machine's half goes silent: nothing sent, nothing read, the connection not closed
    local.stopping.set()
    local.link.closed.set()
    assert wait_for(lambda: remote_state(two, ident)["state"] == "ended", 15)
    assert "nothing" in remote_state(two, ident)["reason"] and "heard" in remote_state(two, ident)["reason"]
    assert wait_for(lambda: beta.relays.names() == [], 15), "the other host's session lost the bridge's server"


def test_bridge_across_hosts_ends_here_when_the_other_half_goes_silent(two, world, monkeypatch):
    """The other way: the stub stopped (SIGSTOP, a host that hangs while its sshd still answers keepalives) - this
    machine's half ends the bridge instead of waiting with every call until its timeout."""
    for module in (two.here, two.there):
        monkeypatch.setattr(module, "PING_INTERVAL", 0.3)
        monkeypatch.setattr(module, "LINK_IDLE", 2.0)
    alpha, beta = world.make("alpha"), world.make("beta")
    ident = world.add("alpha", "host1:beta", "message")["id"]
    stub = two.stubs[0]
    stub.closed = True
    stub.check = lambda: None  # (frozen: its loop goes on, says nothing and ends nothing)
    stub.link.closed.set()
    assert wait_for(lambda: b.load(ident)["state"] == "ended", 15)
    assert "heard" in b.load(ident)["reason"]
    assert wait_for(lambda: alpha.relays.names() == [], 15)


def in_child_with_a_full_disk(work, size=4096):
    """WORK() in a process of its own whose files cannot grow beyond SIZE bytes (RLIMIT_FSIZE, SIGXFSZ ignored: a
    write fails as on a full disk); its exit status: 0 when WORK raised OSError, 1 when it did not, 2 otherwise."""
    import resource
    child = os.fork()
    if child == 0:
        status = 2
        try:
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
            work()
            status = 1
        except OSError:
            status = 0
        finally:
            os._exit(status)
    return os.waitstatus_to_exitcode(os.waitpid(child, 0)[1])


def test_bridge_write_killed_halfway_leaves_nothing_in_the_peers_workspace(tmp_path):
    """kill -9 of the broker in the middle of an allowed write (files:rw) left its cut temporary file
    (.NAME.bridge-XXXX.tmp) in the peer's workspace for ever. The content is written to a file without a name now
    (O_TMPFILE), which gets one only once it is complete, just before it replaces the old file."""
    root = tmp_path / "ws"
    (root / "docs").mkdir(parents=True)
    (root / "docs/a.txt").write_text("old\n")
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"workspace_checks": {}}))
    space = b.Workspace({"workspace": str(root), "directory": str(run)}, [{"path": "docs", "mode": "rw"}])
    child = os.fork()
    if child == 0:  # the broker, killed halfway through its write of the content
        try:
            real = os.write

            def halfway(fd, data):
                real(fd, bytes(data[:len(data) // 2]))
                os.kill(os.getpid(), signal.SIGKILL)
            os.write = halfway
            space.write(["docs", "a.txt"], b"new content\n" * 1000, b.digest(b"old\n"))
        finally:
            os._exit(3)
    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert sorted(path.name for path in (root / "docs").iterdir()) == ["a.txt"], "nothing left beside it"
    assert (root / "docs/a.txt").read_text() == "old\n"
    space.write(["docs", "a.txt"], b"new\n", b.digest(b"old\n"))  # (and a write that is not killed still writes)
    assert (root / "docs/a.txt").read_text() == "new\n" and len(list((root / "docs").iterdir())) == 1


def test_bridge_record_written_on_a_full_disk_leaves_no_temporary_file(tmp_path):
    """A bridge's record, a question to the human or an exposure written when the disk is full: the write failed (as
    it should), but its temporary file (.bridge.json.PID.THREAD, cut) stayed in the human's state folder for ever."""
    folder = tmp_path / "bridge"
    folder.mkdir()
    (folder / "bridge.json").write_text('{"before": true}')
    assert in_child_with_a_full_disk(lambda: b.write_private(folder / "bridge.json", {"x": "y" * 100000})) == 0
    assert sorted(path.name for path in folder.iterdir()) == ["bridge.json"], "no temporary file left"
    assert json.loads((folder / "bridge.json").read_text()) == {"before": True}, "the record as it was"


# --- the agents' own files, crafted by the agent ----------------------------------------------------------------------

nanobot = load("nanobot", "sandbox_nanobot_deep3_test")


def pin_nanobot(tmp_path, content, limit=None):
    """The image's start of nanobot: /etc/nanobot/pin_config.py over ~/.nanobot/config.json holding CONTENT (what the
    agent wrote there). (exit status, stderr, the files in ~/.nanobot)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    pin, settings = tmp_path / "pin_config.py", tmp_path / "sandbox-ai.json"
    pin.write_text(nanobot.PIN_SCRIPT)
    settings.write_text(json.dumps(nanobot.BASE_CONFIG))
    config = tmp_path / "home" / ".nanobot" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(content)

    def limited():
        import resource
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    done = subprocess.run([sys.executable, "-I", str(pin), str(settings), str(config)], capture_output=True,
                          text=True, timeout=60, preexec_fn=limited if limit else None)
    return done.returncode, done.stderr, sorted(path.name for path in config.parent.iterdir())


def test_nanobot_config_the_agent_made_unwritable_does_not_crash_its_start_or_leave_files(tmp_path):
    """nanobot's ~/.nanobot/config.json is the agent's to write. A lone surrogate escape in it ("\\ud800") made the
    start's pin script end in a traceback (nanobot never started again) and left a .config.XXXX file at every start;
    JSON nested deeper than Python reads, the same traceback. The surrogate is kept as its escape now, a file that
    cannot be read is said in one line, and nothing is left behind."""
    status, said, files = pin_nanobot(tmp_path / "surrogate", '{"agents": {"note": "\\ud800"}, '
                                                              '"gateway": {"host": "0.0.0.0"}}')
    assert status == 0 and "Traceback" not in said and files == ["config.json"], said
    written = json.loads((tmp_path / "surrogate/home/.nanobot/config.json").read_text())
    assert written["gateway"]["host"] == "127.0.0.1" and written["agents"]["note"] == "\ud800"
    status, said, files = pin_nanobot(tmp_path / "deep", "[" * 100000 + "]" * 100000)
    assert status == 1 and "Traceback" not in said and "nanobot is not started" in said and files == ["config.json"]


def test_nanobot_config_on_a_full_disk_leaves_no_temporary_file(tmp_path):
    status, said, files = pin_nanobot(tmp_path, json.dumps({"agents": {"note": "x" * 20000}}), limit=8192)
    assert status == 1 and "Traceback" not in said and "could not be written" in said, said
    assert files == ["config.json"], "no cut .config.XXXX beside it"


def test_bridge_across_hosts_suspend_resume_flood_from_the_other_half_does_not_flood_the_human(two, world):
    """The other host's half (buggy, or its host taken over) sent "suspended" and "resume" in turn, well within the
    frame rate: every suspension here was a desktop notification, an event in the session's activity and a line in
    the bridge's log, a hundred a second without end, and every resume lifted the suspension here without the human.
    More than a few changes of state a minute from the other half end the bridge now, as a flood of frames does."""
    from test_bridge_remote import hostile
    alpha = world.make("alpha")
    done = {}

    def script(peer):
        for number in range(200):
            peer.send({"t": "ev", "op": "state", "body": {"state": "suspended", "reason": f"cut {number}"}})
            peer.send({"t": "ev", "op": "resume", "body": {}})
            time.sleep(0.01)
        done["sent"] = True
    two.script["run"] = hostile(script)
    data = world.add("alpha", "host1:beta", "message", both=True)
    assert wait_for(lambda: done.get("sent") or b.load(data["id"])["state"] == "ended", 30)
    assert wait_for(lambda: b.load(data["id"])["state"] == "ended", 15), b.load(data["id"])["state"]
    assert "changes of state" in b.load(data["id"])["reason"]
    suspended = [note for note in world.notes if "suspended" in note[0]]
    assert len(suspended) <= 10, f"{len(suspended)} notifications"
    assert wait_for(lambda: alpha.relays.names() == [], 30)  # (a loaded machine: the full suites beside it)


def test_bridge_stub_killed_on_the_other_host_leaves_nothing_there(two, world):
    """kill -9 of `sandbox-ai bridge-stub` on the other host: this machine's half ends (its connection ended), but on
    the other host the bridge's server stayed attached to its session, and a new bridge to that session was named
    bridge-ID beside the dead one. A new bridge closes what the killed half left there first, and the other host's
    `bridge revoke --all` does too."""
    alpha, beta = world.make("alpha"), world.make("beta")
    ident = world.add("alpha", "host1:beta", "message")["id"]
    stub = two.stubs[0]
    stub.closed = True  # killed: it closes nothing
    stub.stopping.set()
    two.rb.change(ident, lambda current: current.update(broker_pid=dead_pid(), broker_start=None))
    assert wait_for(lambda: b.load(ident)["state"] == "ended", 15)
    assert beta.relays.names() == ["bridge-laptop-alpha"]  # what the killed half left there
    again = world.add("alpha", "host1:beta", "message")
    assert again["b"]["server"] == "bridge-laptop-alpha" and beta.relays.names() == ["bridge-laptop-alpha"]
    assert "broker" in two.rb.load(ident)["reason"]


def test_bridge_end_is_not_held_by_a_write_to_a_half_that_reads_nothing(tmp_path):
    """The other half stopped reading (frozen, its host hung while ssh still answered) while a large frame was being
    sent to it: the end of this half (revoke, the silence noticed) closed ssh's input, which waited for that write for
    ever - and only after it would this half's sides be detached."""
    r = b.remote()
    ssh = subprocess.Popen(["sleep", "600"], stdin=subprocess.PIPE)  # reads nothing, as a frozen other half
    link = r.Link(ssh.stdout or open(os.devnull, "rb"), ssh.stdin, "host1")
    stuck = threading.Thread(target=lambda: link.event("alert", {"text": "x" * (3 << 20)}), daemon=True)
    stuck.start()
    time.sleep(0.5)
    assert stuck.is_alive(), "the write waits (the pipe is full)"
    broker = type("Half", (), {"link": link, "process": ssh})()
    link.closed.set()  # (as the silence or a revoke has it: nothing more is said)
    started = time.monotonic()
    r.close_link(broker, "revoked")
    assert time.monotonic() - started < 20, "the end of this half was held by the stuck write"
    assert ssh.poll() is not None, "ssh was ended"


letta = load("letta", "sandbox_letta_deep3_test")


def test_letta_settings_with_mcp_servers_of_another_form_are_left_as_written(tmp_path):
    """Letta Code's ~/.letta/settings.json is the agent's to write. An agent whose mcpServers was a string or an
    object came out of sandbox-ai's start as a list of its characters or keys with the --mcp servers after them (the
    agent's own servers lost). Such an agent is left as written now, and the start says so."""
    script = tmp_path / "start.py"
    config = tmp_path / "start.json"
    config.write_text(json.dumps({"servers": [{"name": "bridge-x", "command": "python3", "args": []}]}))
    script.write_text(letta.START_SCRIPT.replace('"/etc/letta/sandbox-ai-start.json"', repr(str(config))))
    fake = tmp_path / "letta"
    fake.write_text("#!/bin/sh\necho LETTA \"$@\"\n")
    fake.chmod(0o755)
    home = tmp_path / "home"
    (home / ".letta").mkdir(parents=True)
    agents = [{"baseUrl": "local:1", "mcpServers": "abc"}, {"baseUrl": "local:2", "mcpServers": {"mine": {}}},
              {"baseUrl": "local:3"}]
    (home / ".letta/settings.json").write_text(json.dumps({"agents": agents}))
    done = subprocess.run([sys.executable, "-I", str(script), str(fake)], capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=60)
    assert done.returncode == 0 and done.stdout.startswith("LETTA"), done.stderr
    written = json.loads((home / ".letta/settings.json").read_text())["agents"]
    assert written[0]["mcpServers"] == "abc" and written[1]["mcpServers"] == {"mine": {}}
    assert written[2]["mcpServers"] == [{"name": "bridge-x", "command": "python3", "args": []}]
    assert done.stderr.count("is no list") == 2


def test_damaged_bridge_record_does_not_stop_revoke_all(world, capsys):
    world.make("alpha"), world.make("beta")
    ident = world.add("alpha", "beta", "message")["id"]
    plant_damaged(b.state_root())
    assert b.main(["revoke", "--all"]) == 0
    assert b.load(ident)["state"] == "revoked"
    assert "Traceback" not in capsys.readouterr().err
