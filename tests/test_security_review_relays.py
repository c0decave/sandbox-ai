"""Security review SAI-14 and SAI-12 (the terminals' relays): the sockets of `sandbox-ai attach` and `sandbox-ai shell`
lie where no process of the sandbox can put another socket under their names, and a relay serves a bounded number
of connections at once.

The supervisor binds both sockets in tmux/relay before Firejail starts (runtime.SealedRelays) and hands the listening
descriptors to the guard (runtime.take_relays); inside Firejail that folder is read-only. The host's clients take the
socket's place from the session's record, which only the host writes, and never fall back to the session's tmux
folder, which the sandbox writes. Real sockets, descriptors, threads and processes; only tmux is a script here."""
import contextlib
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime", "sandbox_runtime_review_relays_test")
st = load("starts", "sandbox_starts_review_relays_test")
sessions = st.module("sessions")
launcher = st.launcher()
bridge = load("bridge", "sandbox_bridge_review_relays_test")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(sessions, "PROC", tmp_path / "proc")
    monkeypatch.setattr(sessions.sys.stdin, "isatty", lambda: False)
    yield
    while SUPERVISORS:  # a hand-over thread a test left waiting (it failed before the guard asked) ends here
        SUPERVISORS.pop().set()


# The hand-overs here are awaited by their condition (the guard has the descriptors, the supervisor's thread has
# ended), never by a fixed time: under a full suite's load (four pytest processes, the app's tests, swap) a process
# can go unscheduled for many seconds, and a hand-over thread that gave up after 20 s removed guard.sock before the
# test looked at it (seen twice in full runs). WAIT only bounds a hang.
WAIT = 120
SUPERVISORS = []


def wait_for(condition, seconds=WAIT):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


def serving(relays, clock=time.monotonic):
    """RELAYS handing over in a thread until the guard took them (or the test ended): no deadline of its own - the
    product's (RELAY_HANDOFF_SECONDS) is the supervisor's wait for Firejail, not a test's; (thread, results)."""
    stop, results = threading.Event(), []
    SUPERVISORS.append(stop)
    thread = threading.Thread(target=lambda: results.append(relays.serve(stop=stop, timeout=math.inf, clock=clock)),
                              daemon=True)
    thread.start()
    return thread, results


def sealed_in(tmp_path, clock=time.monotonic):
    """A supervisor's sealed relays in TMP_PATH/tmux/relay, handing over in a thread; (relays, thread, results)."""
    tmux = tmp_path / "tmux"
    tmux.mkdir(mode=0o700, exist_ok=True)
    relays = rt.SealedRelays(tmux / "relay", sandboxed=False)
    return (relays, *serving(relays, clock))


def ended(thread):
    thread.join(WAIT)
    return not thread.is_alive()


def free_fds(count=2):
    """COUNT descriptor numbers no other object of this process holds, as the guard's targets (take_relays puts the
    listeners in their place). Fixed numbers (210, 211 ...) could be the supervisor's own sockets here, where both
    sides run in one process: its close after the hand-over then closed the guard's listener."""
    return tuple(os.open(os.devnull, os.O_RDONLY) for _ in range(count))


def close_fds(fds):
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


# --- the supervisor's side: bound before the sandbox, published at the hand-over, handed over once ----------------

def test_sealed_relays_are_published_only_when_the_guard_takes_them(tmp_path):
    relays, thread, results = sealed_in(tmp_path)
    directory = tmp_path / "tmux" / "relay"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    # Before the guard asked: no socket under a relay's name (a client waiting for the file does not find it yet).
    assert not (directory / "attach.sock").exists() and not (directory / "shell.sock").exists()
    assert stat.S_ISSOCK((directory / "guard.sock").stat().st_mode)
    targets = free_fds()
    try:
        fds = rt.take_relays(directory / "guard.sock", timeout=WAIT, targets=targets)
        assert ended(thread)
        assert results == [True] and fds == targets
        for name in ("attach", "shell"):
            info = (directory / f"{name}.sock").lstat()
            assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        # Handed over once: the hand-over's socket is gone, and nothing is left under a staging name.
        assert sorted(entry.name for entry in directory.iterdir()) == ["attach.sock", "shell.sock"]
        # The descriptors are the listeners of exactly those names, inherited by what the guard executes.
        assert all(os.get_inheritable(fd) for fd in fds)
        for fd, name in zip(fds, ("attach", "shell")):
            with socket.socket(socket.AF_UNIX) as client, socket.socket(fileno=os.dup(fd)) as listener:
                client.connect(str(directory / f"{name}.sock"))
                listener.settimeout(5)
                accepted, _ = listener.accept()
                accepted.close()
    finally:
        close_fds(targets)


def test_sealed_relays_are_handed_over_once_and_never_to_a_later_process(tmp_path):
    relays, thread, results = sealed_in(tmp_path)
    directory = tmp_path / "tmux" / "relay"
    targets, later = free_fds(), free_fds()
    try:
        assert rt.take_relays(directory / "guard.sock", timeout=WAIT, targets=targets) == targets
        assert ended(thread) and results == [True]
        # A process of the sandbox that asks afterwards (it runs after the guard) finds nothing to ask.
        with pytest.raises(OSError):
            rt.take_relays(directory / "guard.sock", timeout=WAIT, targets=later)
    finally:
        close_fds(targets + later)


def test_a_stalled_test_process_still_finds_the_hand_over_waiting(tmp_path):
    """The flake of the two tests above, made deterministic: the process went unscheduled for longer than a fixed
    deadline between the supervisor's thread starting and the guard asking (here: its clock jumps an hour). The
    test's supervisor waits for the hand-over, not for a time, so guard.sock is still there and the guard gets
    the descriptors."""
    jumped, looked = [0.0], threading.Event()

    def clock():
        if jumped[0]:
            looked.set()
        return time.monotonic() + jumped[0]
    relays, thread, results = sealed_in(tmp_path, clock=clock)
    jumped[0] = 3600.0
    assert looked.wait(WAIT)  # the hand-over thread has seen the hour go by
    directory = tmp_path / "tmux" / "relay"
    assert thread.is_alive() and stat.S_ISSOCK((directory / "guard.sock").stat().st_mode)
    targets = free_fds()
    try:
        assert rt.take_relays(directory / "guard.sock", timeout=WAIT, targets=targets) == targets
        assert ended(thread) and results == [True]
    finally:
        close_fds(targets)


def test_the_guards_targets_are_never_the_supervisors_own_descriptors_here(tmp_path):
    """The other cause of the flake's kind, made deterministic: where the supervisor and the guard are one process
    (these tests), a fixed target number could be the supervisor's own socket - take_relays's dup2 put the guard's
    listener there, and the supervisor's close after the hand-over closed it. free_fds gives numbers nobody holds."""
    relays, thread, results = sealed_in(tmp_path)
    own = {relays.handoff.fileno(), *(listener.fileno() for listener in relays.listeners.values())}
    targets = free_fds()
    try:
        assert not own & set(targets)
        assert rt.take_relays(tmp_path / "tmux" / "relay" / "guard.sock", timeout=WAIT, targets=targets) == targets
        assert ended(thread) and results == [True]  # the supervisor closed its copies ...
        assert all(stat.S_ISSOCK(os.fstat(fd).st_mode) for fd in targets)  # ... and the guard's are still there
    finally:
        close_fds(targets)


def test_sealed_relays_refuse_a_peer_that_is_no_process_of_the_sandbox(tmp_path, monkeypatch, capsys):
    """Another user's process, or one the supervisor's PID namespace does not show, gets nothing; the guard still
    does."""
    tmux = tmp_path / "tmux"
    tmux.mkdir(mode=0o700)
    relays = rt.SealedRelays(tmux / "relay", sandboxed=True)
    peers = iter([(0, os.getuid(), 0), (77, os.getuid() + 1, 0), (os.getpid(), os.getuid(), os.getgid())])
    monkeypatch.setattr(rt.Attribution, "peer", staticmethod(lambda connection: next(peers)))
    thread, results = serving(relays)
    targets = free_fds()
    try:
        for _ in range(2):
            with pytest.raises(OSError):
                rt.take_relays(tmux / "relay" / "guard.sock", timeout=WAIT, targets=targets)
            assert not (tmux / "relay" / "attach.sock").exists()
        assert rt.take_relays(tmux / "relay" / "guard.sock", timeout=WAIT, targets=targets) == targets
    finally:
        close_fds(targets)
    assert ended(thread) and results == [True]
    said = capsys.readouterr().err
    assert "no process of this sandbox" in said and "is not the sandbox's" in said


def test_sealed_relays_never_handed_over_leave_no_socket_behind(tmp_path):
    """The guard never came (Firejail did not start): nothing is published, and nothing that looks like a relay's
    socket is left."""
    tmux = tmp_path / "tmux"
    tmux.mkdir(mode=0o700)
    relays = rt.SealedRelays(tmux / "relay", sandboxed=False)
    stop = threading.Event()
    stop.set()
    assert relays.serve(stop=stop, timeout=5) is False
    assert list((tmux / "relay").iterdir()) == []


def test_sealed_relays_do_not_use_what_is_in_their_place(tmp_path):
    """The relay folder is made by the supervisor: a link there, or another user's folder, is refused; stale sockets
    of a supervisor before are removed."""
    tmux = tmp_path / "tmux"
    tmux.mkdir(mode=0o700)
    (tmp_path / "elsewhere").mkdir()
    (tmux / "relay").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError, match="no folder of the sandbox's own"):
        rt.SealedRelays(tmux / "relay", sandboxed=False)
    (tmux / "relay").unlink()
    (tmux / "relay").mkdir()
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(tmux / "relay" / "attach.sock"))
    stale.close()
    relays = rt.SealedRelays(tmux / "relay", sandboxed=False)
    try:
        assert not (tmux / "relay" / "attach.sock").exists()
    finally:
        relays.handed.set()  # (as handed over: close() leaves the folder as it is)
        relays.close()
    (tmux / "relay" / "sub").mkdir()
    with pytest.raises(OSError, match="holds a folder"):
        rt.SealedRelays(tmux / "relay", sandboxed=False)


# --- the guard's side: its script gives each relay its listener and nobody else ------------------------------------

def test_the_sealed_script_names_the_descriptors_and_closes_them_for_everything_else():
    script = rt.tmux_command(["codex"], detached=True, sealed=(3, 4))[2]
    lines = script.splitlines()
    assert lines[0].endswith("bash 3<&- 4<&- || exit $?")  # tmux, its server and so the agent: neither listener
    assert "/usr/bin/python3 -I -S /sandbox/runtime.py --internal-attach-relay fd:3 4<&- &" in lines
    assert "/usr/bin/python3 -I -S /sandbox/runtime.py --internal-shell-relay fd:4 3<&- &" in lines
    closed = lines.index("exec 3<&- 4<&-")
    # Whatever the script runs from the agent's PATH comes after the listeners are closed for the script itself.
    for word in ("date +%s", "tmux has-session", "sleep 1"):
        assert all(index > closed for index, line in enumerate(lines[1:], 1) if word in line), word
    assert "attach.sock" not in script and "shell.sock" not in script
    # A run that is no session's (no relay folder): the relays bind their own sockets, as before.
    legacy = rt.tmux_command(["codex"], detached=True)[2]
    assert "--internal-attach-relay /sandbox/tmux/attach.sock &" in legacy and "<&-" not in legacy


FAKE_TMUX = """#!/bin/sh
# tmux as the guard's script calls it: the session "starts", is there twice, then is gone.
ls /proc/self/fd | tr '\\n' ' ' >> "$FAKE_STATE/fds-$1"
echo >> "$FAKE_STATE/fds-$1"
case "$1" in
  -u) exit 0 ;;
  has-session) n=$(cat "$FAKE_STATE/asked" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_STATE/asked"
               [ "$n" -lt "${FAKE_ASKS:-40}" ] ;;
  attach) printf 'attached\\r\\n' ;;
esac
"""


def test_the_sealed_script_runs_the_relays_on_the_handed_sockets(tmp_path, monkeypatch):
    """The guard's script as it runs (tmux a script that records its descriptors): the relays answer on the sockets
    the supervisor bound, and neither tmux (the agent's ancestor) nor the script's other commands hold a listener."""
    relays, thread, results = sealed_in(tmp_path)
    directory = tmp_path / "tmux" / "relay"
    state, tools = tmp_path / "state-fake", tmp_path / "bin"
    state.mkdir()
    tools.mkdir()
    (tools / "tmux").write_text(FAKE_TMUX)
    (tools / "tmux").chmod(0o755)
    monkeypatch.setattr(rt, "AGENT_EXIT", str(tmp_path / "tmux" / "agent-exit"))
    monkeypatch.setattr(rt, "TMUX_ENDED", str(tmp_path / "tmux" / "ended"))
    script = rt.tmux_command(["true"], detached=True, sealed=(3, 4))[2]
    script = script.replace("/sandbox/runtime.py", str(ROOT / "sandbox" / "runtime.py"))
    fds = rt.take_relays(directory / "guard.sock", timeout=WAIT, targets=free_fds())
    assert ended(thread) and results == [True]

    def place():  # the guard's descriptors 3 and 4, as take_relays leaves them (by way of numbers above both)
        high = [fcntl.fcntl(fd, fcntl.F_DUPFD, 64) for fd in fds]
        os.dup2(high[0], 3, inheritable=True)
        os.dup2(high[1], 4, inheritable=True)
    process = subprocess.Popen(["sh", "-c", script], cwd=tmp_path, preexec_fn=place, pass_fds=(3, 4),
                               env={"PATH": f"{tools}:/usr/bin:/bin", "FAKE_STATE": str(state), "FAKE_ASKS": "40"},
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    close_fds(fds)
    try:
        # `sandbox-ai shell`: the relay on the handed socket runs the command.
        out = []
        client = threading.Thread(target=lambda: out.append(shell_through(directory / "shell.sock", ["echo", "hi"])),
                                  daemon=True)
        client.start()
        client.join(30)
        assert out == [(0, b"hi\n")]
        # `sandbox-ai attach`: the relay on the handed socket starts its tmux client.
        with socket.socket(socket.AF_UNIX) as attach:
            attach.settimeout(20)
            attach.connect(str(directory / "attach.sock"))
            attach.sendall(rt.attach_frame(b"r", struct.pack("!HH", 24, 80)))
            said = b""
            while b"attached" not in said:
                chunk = attach.recv(4096)
                if not chunk:
                    break
                said += chunk
        assert b"attached" in said
    finally:
        (state / "asked").write_text("1000")  # the session is "gone": the script ends its relays and itself
        try:
            process.wait(30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    assert process.returncode == 0, process.stderr.read()
    # No process but the two relays ever had a listener: tmux (new-session: its server, the agent's ancestor),
    # has-session and the attach relay's own tmux client saw descriptors 0-2 and ls's own only.
    for record in ("fds--u", "fds-has-session", "fds-attach"):
        for line in (state / record).read_text().splitlines():
            held = {int(fd) for fd in line.split()} - {0, 1, 2}
            assert len(held) <= 1, (record, line)  # (only ls's own descriptor of /proc/self/fd)
    assert json.loads((tmp_path / "tmux" / "ended").read_text()) == {"status": None, "seconds": None}


def shell_through(path, argv):
    """(status, output) of ARGV run through the shell relay at PATH, as runtime.shell_client does it on pipes."""
    read_out, write_out = os.pipe()
    read_in, write_in = os.pipe()
    os.close(write_in)
    try:
        status = rt.shell_client(str(path), argv, tty=False, stdin=read_in, stdout=write_out, stderr=write_out)
    finally:
        os.close(write_out)
        os.close(read_in)
    with os.fdopen(read_out, "rb") as stream:
        return status, stream.read()


def test_the_guard_does_not_start_a_session_whose_relays_it_cannot_take(tmp_path, monkeypatch, capsys):
    """Fail closed: the relay folder is there (the supervisor sealed it) and the hand-over fails - the guard ends
    before anything of the agent runs, rather than publish the relays where the sandbox can write."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"probe": False, "env": {}, "command": ["true"], "tmux": "detached"}))
    (tmp_path / "relay").mkdir()
    monkeypatch.setattr(rt, "SESSION_CONFIG", config)
    monkeypatch.setattr(rt, "RELAY_DIR", str(tmp_path / "relay"))
    monkeypatch.setattr(rt, "namespace_state", lambda: {})
    monkeypatch.setattr(rt, "verify_inner", lambda before, current: None)
    monkeypatch.setattr(rt, "take_relays", lambda: (_ for _ in ()).throw(OSError(111, "Connection refused")))
    executed = []
    monkeypatch.setattr(rt.os, "execvpe", lambda *args: executed.append(args))
    monkeypatch.setattr(rt.os, "chdir", lambda path: None)
    assert rt.inner_guard({}) == 1 and executed == []
    assert "could not be taken over from the supervisor" in capsys.readouterr().err


def test_firejail_keeps_the_relay_folder_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "RELAY_DIR", str(tmp_path / "relay"))
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "--shell=none\n", ""))
    assert f"--read-only={tmp_path / 'relay'}" not in rt.firejail_command({})  # (no session: no such folder)
    (tmp_path / "relay").mkdir()
    command = rt.firejail_command({})
    assert f"--read-only={tmp_path / 'relay'}" in command
    assert command.index(f"--read-only={tmp_path / 'relay'}") < command.index("/sandbox/runtime.py")


# --- the relays serve a bounded number of connections (SAI-12) -----------------------------------------------------

def test_a_relay_refuses_connections_beyond_its_limit_before_serving_them(tmp_path):
    path = tmp_path / "relay.sock"
    serving, release = [], threading.Event()

    def handler(connection):
        serving.append(connection)
        release.wait(20)
        connection.close()
    threading.Thread(target=rt.serve_relay, args=(str(path), handler), kwargs={"at_once": 3}, daemon=True).start()
    assert wait_for(path.exists)
    held = []
    for _ in range(3):
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(path))
        held.append(client)
    assert wait_for(lambda: len(serving) == 3)
    extra = socket.socket(socket.AF_UNIX)
    extra.settimeout(5)
    extra.connect(str(path))
    assert extra.recv(1) == b""  # closed at once: no thread, no pty, no process was made for it
    extra.close()
    assert len(serving) == 3
    release.set()
    for client in held:
        client.close()
    # A slot is free again once a connection ended.
    assert wait_for(lambda: one_more(path, serving, 4))


def one_more(path, serving, expected):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        try:
            client.connect(str(path))
        except OSError:
            return False
        return wait_for(lambda: len(serving) >= expected, 1)


def test_the_relays_limits_are_set():
    assert rt.RELAY_AT_ONCE == {"attach": 16, "shell": 32}


# --- the host's side: the socket's place is the session's record's word ---------------------------------------------

def make_session(key, tmp_path, relay=sessions.SEALED_RELAYS, pid=41):
    directory = sessions.session_dir(key)
    for path in (directory, directory / "home", directory / "tmux"):
        path.mkdir(parents=True, exist_ok=True)
    data = {"agent": "codex", "options": ["--backend", "podman"], "agent_args": [], "backend": "podman",
            "workspace": "/srv/app", "cwd": "/", "created": "2026-11-01T10:00:00Z", "pod": f"agent-session-{key}",
            "name": key, "mode": "tmux", "pid": pid, "started": "2026-11-02T09:30:00Z",
            **({"shell_relay": relay} if relay else {})}
    entry = tmp_path / "proc" / str(pid)
    entry.mkdir(exist_ok=True)
    (entry / "cmdline").write_bytes(b"\0".join(os.fsencode(str(part)) for part in (
        "python3", "agent_sandbox.py", "--tmux-socket-dir", directory / "tmux")) + b"\0")
    (entry / "stat").write_text(f"{pid} (python3) S 1 1 1 0 -1\n")
    sessions.save(key, data)
    return directory, data


def test_the_relays_place_is_read_from_the_sessions_record(tmp_path):
    directory, data = make_session("new", tmp_path)
    assert sessions.sealed_relays(data)
    assert sessions.relay_socket(directory, data) == directory / "tmux" / "relay" / "attach.sock"
    assert sessions.relay_socket(directory, data, "shell") == directory / "tmux" / "relay" / "shell.sock"
    assert sessions.relay_inside(data) == "/sandbox/tmux/relay/attach.sock" == rt.RELAY_ATTACH
    assert sessions.relay_inside(data, "shell") == "/sandbox/tmux/relay/shell.sock" == rt.RELAY_SHELL
    # A start of a sandbox-ai before 0.1.117 that still runs: where its relays made them.
    for made in (1, None, True, "2"):
        old = {**data, "shell_relay": made}
        assert not sessions.sealed_relays(old)
        assert sessions.relay_socket(directory, old, "shell") == directory / "tmux" / "shell.sock"
        assert sessions.relay_inside(old) == "/sandbox/tmux/attach.sock"


class Impostor:
    """What a process of the sandbox binds in the session's tmux folder, which it can write: it records what a client
    sends and answers with ANSWER."""

    def __init__(self, path, answer=b""):
        self.server = rt.listen_unix(str(path))
        self.answer, self.requests = answer, []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                with contextlib.suppress(OSError):
                    self.requests.append(connection.recv(65536))
                    connection.sendall(self.answer)


def test_an_impostor_in_the_tmux_folder_gets_nothing_of_a_sealed_session(tmp_path, monkeypatch, capsys):
    """SAI-14: a process of the sandbox ended the relays and bound its own sockets under the names a client used to
    connect to (tmux/attach.sock, tmux/shell.sock). The clients of a sealed session connect in tmux/relay only: the
    impostor receives no command, no key, no window size, and nothing it sends is shown."""
    directory, data = make_session("web", tmp_path)
    (directory / "tmux" / "relay").mkdir()
    for name in ("attach.sock", "shell.sock"):  # the relays the process ended: their sockets stay, nobody listens
        dead = socket.socket(socket.AF_UNIX)
        dead.bind(str(directory / "tmux" / "relay" / name))
        dead.close()
    fake_shell = Impostor(directory / "tmux" / "shell.sock", b"o" + struct.pack("!I", 5) + b"owned"
                          + b"x" + struct.pack("!I", 4) + struct.pack("!i", 0))
    fake_attach = Impostor(directory / "tmux" / "attach.sock", b"\x1b]52;c;b3duZWQ=\x07owned")
    monkeypatch.setattr(sessions, "activity_event", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="relay/shell.sock"):
        sessions.shell("web", ["cat", "/etc/hostname"], tty=False)
    with pytest.raises(RuntimeError, match="relay/attach.sock"):
        sessions.attach("web")
    assert not sessions.terminal_listening("web", data)
    assert fake_shell.requests == [] and fake_attach.requests == []
    assert "owned" not in capsys.readouterr().out
    # The bridge's snapshot of a peer's terminal goes the same way.
    assert bridge.terminal_socket({"start": "web"}) == directory / "tmux" / "relay" / "shell.sock"
    assert not bridge.older_start({"start": "web"})


def test_a_sealed_session_is_served_in_its_relay_folder(tmp_path, monkeypatch):
    directory, data = make_session("web", tmp_path)
    (directory / "tmux" / "relay").mkdir()
    work = tmp_path / "work"
    work.mkdir()
    threading.Thread(target=rt.shell_relay, args=(str(directory / "tmux" / "relay" / "shell.sock"), str(work)),
                     daemon=True).start()
    assert wait_for((directory / "tmux" / "relay" / "shell.sock").exists)
    monkeypatch.setattr(sessions, "activity_event", lambda *a, **k: None)
    assert sessions.shell("web", ["sh", "-c", "exit 7"], tty=False) == 7
    assert bridge.terminal_socket({"start": "web"}) == directory / "tmux" / "relay" / "shell.sock"


def test_a_session_started_before_the_sealed_relays_still_attaches_where_it_was_made(tmp_path, monkeypatch):
    """A sandbox started by an older sandbox-ai keeps running across the update: its relays are in its tmux folder,
    and its record (no shell_relay 2) says so."""
    directory, data = make_session("old", tmp_path, relay=1)
    work = tmp_path / "work"
    work.mkdir()
    threading.Thread(target=rt.shell_relay, args=(str(directory / "tmux" / "shell.sock"), str(work)),
                     daemon=True).start()
    assert wait_for((directory / "tmux" / "shell.sock").exists)
    (directory / "tmux" / "attach.sock").touch()
    monkeypatch.setattr(sessions, "activity_event", lambda *a, **k: None)
    assert sessions.shell("old", ["sh", "-c", "exit 5"], tty=False) == 5
    assert sessions.terminal_state("old", data) == ("ready", "")


def test_a_new_session_is_recorded_with_sealed_relays_and_waited_for_there(tmp_path, monkeypatch):
    """The start's record names the sealed relays, and the start waits for the socket in tmux/relay: one that
    appears in the tmux folder (the sandbox's to write) does not make it ready."""
    directory = tmp_path / "s"
    (directory / "tmux").mkdir(parents=True)
    (directory / "supervisor.log").write_text("boom: image missing\n")
    (directory / "tmux" / "attach.sock").touch()  # (what a process of the sandbox could put there)
    sealed = {"shell_relay": sessions.SEALED_RELAYS}
    ended = SimpleNamespace(poll=lambda: 1, returncode=1)
    with pytest.raises(RuntimeError, match="image missing"):
        sessions.wait_ready(directory, ended, data=sealed)
    (directory / "tmux" / "relay").mkdir()
    (directory / "tmux" / "relay" / "attach.sock").touch()
    sessions.wait_ready(directory, ended, data=sealed)
