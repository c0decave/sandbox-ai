"""Cairn's relay in the agent's container (sandbox/cairn_relay.py) runs under a supervisor of its own now
(`cairn_relay.py supervise`, which runtime.start_cairn_relay starts): before, a relay that died mid-session (killed,
out of memory) was started by nothing again, and every task Cairn handed out failed at once ("the relay ... is not
reachable"), again and again. The supervisor starts it again after a pause (1, 2, 5, 10, 30 s), at most RESTARTS times
within RESTART_WINDOW seconds; beyond that it leaves a note beside the socket (relay.ended) that says why, a worker
says that note, and Cairn's container stops its dispatcher with that message (the board stays). With a real relay
process killed under its supervisor, and the supervisor's and Cairn's loops with stand-ins."""
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = load("cairn_relay_restart_tests", "sandbox/cairn_relay.py")
cairn_rt = load("cairn_runtime_restart_tests", "sandbox/cairn_runtime.py")
runtime = load("runtime_cairn_restart_tests", "sandbox/runtime.py")
FAKE_CLAUDE = "#!/bin/sh\necho \"claude $*\"\n"


class Child:
    def __init__(self, status):
        self.status, self.pid = status, 4242

    def wait(self, timeout=None):
        return self.status

    def poll(self):
        return self.status


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_the_supervisor_starts_a_relay_that_ended_again_with_pauses_and_then_gives_up(tmp_path):
    cfg = {"socket": str(tmp_path / "relay" / "relay.sock"), "workers": {"claude": []}}
    (tmp_path / "relay").mkdir()
    Path(cfg["socket"]).write_text("")  # (a socket the dead relay left)
    clock, started = Clock(), []

    def popen(argv, **kw):
        started.append(argv)
        return Child(-9)  # killed, at once, every time
    assert relay.supervise(cfg, popen=popen, sleep=clock.sleep, clock=clock) == 1
    assert len(started) == relay.RESTARTS + 1
    assert started[0][-3:-1] == [str(Path(relay.__file__).resolve()), "serve"]
    assert json.loads(started[0][-1]) == cfg
    assert clock.slept == list(relay.RESTART_DELAYS[:relay.RESTARTS])
    note = (tmp_path / "relay" / relay.ENDED).read_text()
    assert f"ended {relay.RESTARTS + 1} times" in note and "signal 9" in note and "is not started again" in note
    assert "start the session again" in note
    assert not Path(cfg["socket"]).exists(), "no socket: a worker finds none, and reads the note"
    log = (tmp_path / "relay" / "relay.log").read_text()
    assert "the relay ended (signal 9); started again in 1 s" in log and "is not started again" in log


def test_restarts_long_apart_are_not_counted_together(tmp_path):
    cfg = {"socket": str(tmp_path / "relay.sock"), "workers": {"claude": []}}
    clock, started = Clock(), []

    def popen(argv, **kw):
        started.append(argv)
        if len(started) > 12:
            return Child(0)  # (the run ends: the relay's own end, status 0, ends the supervisor too)
        clock.now += relay.RESTART_WINDOW  # each one ran a long while before it died
        return Child(1)
    assert relay.supervise(cfg, popen=popen, sleep=clock.sleep, clock=clock) == 0
    assert len(started) == 13 and not (tmp_path / relay.ENDED).exists()
    assert set(clock.slept) == {relay.RESTART_DELAYS[0]}, "the pause starts short again"


def test_a_stale_note_of_an_earlier_relay_goes_at_the_start(tmp_path):
    cfg = {"socket": str(tmp_path / "relay.sock"), "workers": {"claude": []}}
    (tmp_path / relay.ENDED).write_text("old")
    assert relay.supervise(cfg, popen=lambda argv, **kw: Child(0), sleep=lambda s: None) == 0
    assert not (tmp_path / relay.ENDED).exists()


def test_a_worker_says_the_note_when_the_relay_is_gone_for_good(tmp_path):
    (tmp_path / relay.ENDED).write_text("the relay to the session's agents ended 6 times ... start the session again")
    err = io.BytesIO()
    assert relay.worker("claude", ["--help"], path=str(tmp_path / "relay.sock"), out=io.BytesIO(), err=err) == 127
    assert b"ended 6 times" in err.getvalue() and b"not reachable" not in err.getvalue()
    (tmp_path / relay.ENDED).write_bytes(b"x" * 100000)  # bounded
    err = io.BytesIO()
    relay.worker("claude", ["--help"], path=str(tmp_path / "relay.sock"), out=io.BytesIO(), err=err)
    assert len(err.getvalue()) < 5000


def test_the_guard_starts_the_supervisor():
    started = []
    runtime.start_cairn_relay({"socket": "/sandbox/cairn/relay.sock", "workers": {"claude": []}}, {"HOME": "/root"},
                              popen=lambda argv, **kw: started.append((argv, kw)))
    [(argv, kw)] = started
    assert argv[3:5] == ["/sandbox/cairn_relay.py", "supervise"] and kw["start_new_session"]


def test_cairn_stops_its_dispatcher_when_the_relay_is_gone_for_good(tmp_path, monkeypatch):
    mount = tmp_path / "mount"
    mount.mkdir()
    socket_path = tmp_path / "relay" / "relay.sock"
    socket_path.parent.mkdir()
    cfg = {"port": 18998, "data": str(tmp_path / "data"), "workers": ["claude"], "socket": str(socket_path),
           "project": None, "dispatch": {"server": "x", "runtime": {"execution": "local"},
                                         "local": {"workspace_root": str(tmp_path / "runs")}, "workers": []}}
    (mount / "config.json").write_text(json.dumps(cfg))
    for name, value in (("BIN", tmp_path / "bin"), ("HOME", tmp_path / "home"), ("DISPATCH", tmp_path / "d.yaml")):
        monkeypatch.setattr(cairn_rt, name, value)
    monkeypatch.setattr(cairn_rt.signal, "signal", lambda *a: None)
    monkeypatch.setattr(cairn_rt, "port_open", lambda port: True)
    monkeypatch.setattr(cairn_rt, "relay_ready", lambda path: True)
    said = []
    monkeypatch.setattr(cairn_rt, "say", said.append)
    started, ticks = [], []

    class Proc:
        def __init__(self, argv):
            self.argv, self.returncode, self.stdout, self.terminated = argv, None, io.StringIO(""), False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated, self.returncode = True, -15

        def wait(self, timeout=None):
            if "serve" in self.argv:
                self.returncode = 0  # (the board stays until the run ends)
            return self.returncode

        def kill(self):
            self.returncode = -9

    def popen(argv, **kw):
        started.append(Proc(argv))
        return started[-1]

    def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 3:  # the relay's supervisor gave up meanwhile
            (socket_path.parent / relay.ENDED).write_text("the relay ... is not started again: start the session "
                                                          "again")
        assert len(ticks) < 50, said
    assert cairn_rt.main(mount=mount, popen=popen, sleep=sleep) == 1
    dispatchers = [p for p in started if "dispatch" in p.argv]
    assert len(dispatchers) == 1 and dispatchers[0].terminated, "stopped, and not started again"
    text = "\n".join(said)
    assert "is not started again" in text and "Cairn's dispatcher is stopped" in text and "the board stays" in text


# --- a real relay process, killed under its supervisor ---------------------------------------------------------------

def children(pid):
    found = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        found += [int(p) for p in (task / "children").read_text().split()]
    return found


@pytest.mark.skipif(not Path("/proc/self/task").exists(), reason="needs /proc (Linux)")
def test_a_relay_killed_mid_session_answers_again(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(FAKE_CLAUDE)
    (bin_dir / "claude").chmod(0o755)
    cfg = {"socket": str(tmp_path / "relay" / "relay.sock"), "workspace": str(tmp_path / "runs"),
           "workers": {"claude": []}}
    env = dict(os.environ, PATH=f"{bin_dir}:/usr/bin:/bin")
    supervisor = subprocess.Popen([sys.executable, "-I", str(ROOT / "sandbox/cairn_relay.py"), "supervise",
                                   json.dumps(cfg)], env=env, stdin=subprocess.DEVNULL, start_new_session=True)

    def call():
        out, err = io.BytesIO(), io.BytesIO()
        status = relay.worker("claude", ["--help"], path=cfg["socket"], out=out, err=err, cwd=str(tmp_path))
        return status, out.getvalue().decode(), err.getvalue().decode()

    def answered(seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if Path(cfg["socket"]).exists() and call()[0] == 0:
                return True
            time.sleep(0.1)
        return False
    try:
        assert answered(), "the relay came up"
        [first] = children(supervisor.pid)
        os.kill(first, signal.SIGKILL)  # (the relay this test's supervisor started)
        time.sleep(0.3)
        assert call()[0] != 0, "while it is down a task fails"
        assert answered(), "started again"
        [second] = children(supervisor.pid)
        assert second != first and "claude --help" in call()[1]
        log = (tmp_path / "relay" / "relay.log").read_text()
        assert "the relay ended (signal 9); started again in 1 s" in log
        left = children(supervisor.pid)
    finally:
        supervisor.terminate()
        supervisor.wait(10)
    # SIGTERM (the run ends) ends the relay with its supervisor.
    deadline = time.monotonic() + 10
    while any(gone_not(pid) for pid in left) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(gone_not(pid) for pid in left)


def gone_not(pid):
    """Whether PID still runs (a zombie, its parent gone, is reaped by init: not running)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][:1] not in ("Z", "X")
    except (OSError, IndexError):
        return False
