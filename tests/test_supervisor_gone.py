"""A sandbox must never run on without its supervisor and its kill switch (2026-11-16, launcher 0.1.93): the session
supervisor of claude-e7d6edae ended around 09:45 (the host's disk nearly full) while its pod ran on; its cleanup had
removed the run directory - the control socket of approvals, net allow, workspace add and the kill switch (net cut, the
app's emergency stop) - and nobody was told; the terminal that started it never reaped it (a zombie), and `sandbox-ai
list` said only "orphaned". Now it fails closed and loud:

- the launcher whose pod Podman could not remove cuts the sandbox's network (else kills the pod), keeps the run
  directory and says why: launcher.log, its terminal, the desktop, list and doctor (sandbox_outlived);
- the sandbox's own supervisor (runtime.py, PID 1) watches its launcher through a lock in the run directory and cuts
  its own network when the launcher is gone or the directory went (runtime.watch_launcher);
- the terminal that started a session reaps its supervisor and reacts when the sandbox runs on (sessions);
- a monitor's write error (ENOSPC) degrades that monitor with a warning, never the run (runtime.log_event).

Podman and tmux are faked (live_fakes.py); nothing here talks to a real one."""
import importlib.util
import types
import json
from pathlib import Path

import pytest

from live_fakes import Host

ROOT = Path(__file__).resolve().parents[1]
LIVE = {"FAKE_PODMAN_POD_LIVE": "1"}  # (Podman lists the pod running; its removal works again)
NO_SPACE = "Error: removing pod: writing to database: no space left on device"


def written(host):
    path = Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "launcher.log"
    return path.read_text() if path.exists() else ""


@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.add_image("localhost/claude-sandbox:latest")
    return made


def runs(host):
    base = Path(host.env["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs"
    return sorted(path for path in base.iterdir() if path.is_dir() and (path / "run.json").exists()) \
        if base.is_dir() else []


# --- the launcher whose pod Podman could not remove ------------------------------------------------------------------


def test_a_pod_podman_cannot_remove_keeps_its_run_directory_is_ended_or_cut_and_said(host):
    """The user's case through the real entry point: the sandbox ends, `podman pod rm` fails for a full disk and the
    pod runs on. Before, the launcher said one line and removed the run directory (approvals, net allow and the kill
    switch lost for good). Now its network is cut, or - here, with no supervisor behind the fake pod to answer - the
    pod is killed; one Podman does not end either keeps its run directory, the reason is recorded and said on the
    terminal, in launcher.log, and by sandbox-ai list."""
    result = host.run("claude", env={"FAKE_PODMAN_RUN": "1:0", "FAKE_PODMAN_POD_RM_FAIL": f"125:{NO_SPACE}"})
    err = result.stderr
    assert "runs on without its supervisor" in err and "no space left on device" in err
    assert "its network could not be cut" in err and "end it now: podman pod rm --force agent-claude-" in err
    assert "Save your work in it, then: sandbox-ai stop" in err
    calls = host.calls()
    assert any(call[:2] == ["pod", "kill"] for call in calls)  # (the cut is not confirmed: the pod is ended instead)
    [kept] = runs(host)  # the run directory stays: sandbox-ai stop finds the run by it
    end = json.loads((kept / "supervisor-ended.json").read_text())
    assert end["by"] == "launcher" and "no space left on device" in end["why"] and "cut" not in end
    assert end["cut_error"] and end["pod"].startswith("agent-claude-")
    record = json.loads((kept / "run.json").read_text())
    assert Path(record["session_dir"]).is_dir()  # the HOME of a run without one of its own: never removed under it
    log = written(host)
    assert "ERROR   pod: the sandbox " in log and "runs on without its supervisor" in log
    assert "start: ended with exit status" in log  # (its end is not recorded in its history: it has not ended)
    # sandbox-ai list says precisely what happened and what to do, not only "orphaned" (and so does --json).
    listed = host.run("list", env=LIVE)
    assert "orphaned" in listed.stdout and f"{record['name']} is orphaned: its supervisor ended at " in listed.stdout
    assert "no space left on device" in listed.stdout and "its network could not be cut" in listed.stdout
    assert f"then: sandbox-ai stop {record['name']}, then: sandbox-ai resume {record['name']}" in listed.stdout
    entry = [e for e in json.loads(host.run("list", "--json", env=LIVE).stdout) if e.get("run") == record["run"]][0]
    assert entry["state"] == "orphaned" and "no space left on device" in entry["orphaned_why"]
    # sandbox-ai stop ends it (its kept run directory names it) and removes what was left
    stopped = host.run("stop", record["name"], env=LIVE)
    assert stopped.returncode == 0, stopped.stderr
    assert any(call[:2] == ["pod", "stop"] for call in host.calls())
    assert runs(host) == [] and not Path(record["session_dir"]).exists()  # (liveness.discard: what was kept for it)


def test_a_pod_that_is_gone_after_all_leaves_nothing_behind(host):
    """Podman's removal failed, but nothing of the pod runs (`pod ps` lists none): the run ends as always."""
    result = host.run("claude", env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_POD_RM_FAIL": "125:Error: odd",
                                     "FAKE_PODMAN_POD_GONE": "1"})
    assert "without its supervisor" not in result.stderr
    assert runs(host) == []


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_supervisor_gone", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the sandbox's own watch of its launcher ---------------------------------------------------------------------


class FakeNetwork:
    """runtime.RuntimeNetwork's set_cut: kept; REFUSES (an exception) for a run without a kill switch."""

    def __init__(self, refuses=None):
        self.cuts, self.refuses = [], refuses

    def set_cut(self, mode, by="self", clear=False):
        if self.refuses:
            raise self.refuses
        self.cuts.append((mode, by))
        return {"cut": mode, "cuts": {by: mode}}


def hold_lock_in_child(directory):
    """A launcher stand-in: another process that holds the run's lock as the launcher does (hold_supervisor_lock)."""
    import subprocess
    import sys
    code = ("import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('held', flush=True)\n"
            "time.sleep(600)\n")
    child = subprocess.Popen([sys.executable, "-I", "-c", code, str(Path(directory) / "supervisor.lock")],
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"
    return child


def test_killing_the_launcher_cuts_the_sandboxs_network_from_inside(tmp_path):
    """The kernel lets go of the launcher's lock whatever ends it (here SIGKILL, as the OOM killer would): the sandbox's
    own supervisor sees it within its interval, cuts the network ("all", on behalf of supervisor-gone), records why in
    the run's directory (list and doctor say it), writes the cut for net list and reports a safeguard. While the
    launcher holds its lock nothing happens."""
    import signal
    import threading
    rt = load("runtime")
    child = hold_lock_in_child(tmp_path)
    try:
        assert rt.launcher_gone(tmp_path) is None
        network, stop = FakeNetwork(), threading.Event()
        found = []
        watch = threading.Thread(target=lambda: found.append(rt.watch_launcher(network, stop, directory=tmp_path,
                                                                                interval=0.05)))
        watch.start()
        watch.join(0.3)
        assert watch.is_alive() and network.cuts == []  # the launcher runs: nothing to do
        child.send_signal(signal.SIGKILL)
        child.wait()
        watch.join(5)
        assert not watch.is_alive() and "the supervisor on the host, has ended" in found[0]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert network.cuts == [("all", "supervisor-gone")]
    end = json.loads((tmp_path / "supervisor-ended.json").read_text())
    assert end["cut"] == "all" and end["by"] == "sandbox" and "has ended" in end["why"]
    assert json.loads((tmp_path / "cut.json").read_text())["mode"] == "all"
    event = json.loads((tmp_path / "safeguard-events.jsonl").read_text().splitlines()[0])
    assert event["kind"] == "guard" and "its network is cut (kill switch all)" in event["why"]
    assert rt.SUPERVISOR_END == load("liveness").SUPERVISOR_END
    assert rt.SUPERVISOR_GONE_BY == load("agent_sandbox").OUTLIVED_BY


def test_a_removed_run_directory_cuts_too_and_without_a_kill_switch_the_run_ends(tmp_path):
    """The user's case seen from inside: /sandbox/control emptied (the launcher's cleanup removed the run directory).
    A run that has no kill switch (or whose cut fails) is ended instead (its agent gets SIGTERM): never uncut."""
    import threading
    rt = load("runtime")
    assert "run directory on the host was removed" in rt.launcher_gone(tmp_path)
    ended = []
    network = FakeNetwork(refuses=ValueError("this sandbox has no kill switch"))
    why = rt.watch_launcher(network, threading.Event(), end=ended.append, directory=tmp_path, interval=0.01)
    assert "removed" in why and len(ended) == 1 and "could not be cut (this sandbox has no kill switch)" in ended[0]
    stopped = threading.Event()
    stopped.set()
    assert rt.watch_launcher(FakeNetwork(), stopped, directory=tmp_path, interval=0.01) is None


def test_the_launcher_holds_its_lock_and_tells_the_sandbox_to_watch_it(tmp_path, monkeypatch):
    """register_run takes the lock (O_CLOEXEC, no child holds it); config.json's "launcher_lock" only where the lock
    reaches the sandbox: Podman on this host's own kernel, not for --podman-url (nor macOS, nor a probe)."""
    s = load("agent_sandbox")
    rt = s.RUNTIME
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "rt").mkdir()
    (tmp_path / "app").mkdir()
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "app")])
    s.validate(a)
    directory = s.register_run(a, "claude-0e7d6eda", tmp_path / "session")
    try:
        assert a.supervisor_lock is not None and rt.launcher_gone(directory) is None
        assert s.launcher_watched(a) is (s.sys.platform == "linux")
        remote = s.parse_args(["claude", "--workspace", str(tmp_path / "app"), "--podman-url", "unix:///x.sock"])
        remote.supervisor_lock = a.supervisor_lock
        assert not s.launcher_watched(remote)
    finally:
        import os
        os.close(a.supervisor_lock)
    assert rt.launcher_gone(directory) == "its launcher, the supervisor on the host, has ended"


def test_the_record_of_a_supervisors_end_is_written_once_and_read_safely(tmp_path):
    live = load("liveness")
    assert live.record_supervisor_end(tmp_path, "Podman could not remove its pod", "launcher", cut="all", run="r1")
    assert not live.record_supervisor_end(tmp_path, "later and vaguer", "sandbox")  # the first word wins
    end = live.supervisor_end(tmp_path)
    assert end["why"] == "Podman could not remove its pod" and end["cut"] == "all" and end["by"] == "launcher"
    assert live.supervisor_end(tmp_path, run="r2") is None and live.supervisor_end(tmp_path, run="r1")
    text = live.orphan_text("work\x1b[2J", end)
    assert "its supervisor ended at" in text and "network was cut (kill switch all)" in text
    assert "sandbox-ai stop work\\x1b[2J, then: sandbox-ai resume work\\x1b[2J" in text
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / live.SUPERVISOR_END).symlink_to(tmp_path / live.SUPERVISOR_END)
    assert live.supervisor_end(tmp_path / "other") is None  # never through a link
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / live.SUPERVISOR_END).write_text('{"why": 5}')
    assert live.supervisor_end(tmp_path / "bad") is None and live.supervisor_end(None) is None
    assert "is gone while its pod still runs" in live.orphan_text("x")


# --- the terminal that started a session reaps its supervisor and reacts ----------------------------------------


class Ended:
    """A supervisor process (subprocess.Popen) that has ended with STATUS: wait() reaps it."""

    def __init__(self, status, pid=4022643):
        self.status, self.pid, self.waited = status, pid, 0

    def wait(self, timeout=None):
        self.waited += 1
        return self.status


@pytest.fixture
def session_host(tmp_path, monkeypatch):
    """sessions.py and its modules over a state directory of their own, Podman faked (a pod that runs)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "rt"))
    (tmp_path / "rt").mkdir()
    st = load("starts")
    sessions = st.module("sessions")
    live, net = sessions.liveness(), sessions.starts().module("net")  # (the ones sessions.py uses)
    pods = {"agent-claude-0e7d6eda": True}
    calls = []

    def podman(cmd, **kwargs):
        args = cmd[1:]
        calls.append(args)
        if args[:2] == ["pod", "ps"]:
            listed = [{"Name": name, "Status": "Running", "InfraId": "i", "Labels": {},
                       "Containers": [{"Id": "i", "Status": "running"}, {"Id": "c", "Status": "running"}]}
                      for name, running in pods.items() if running]
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(listed), stderr="")
        if args[:2] in (["pod", "stop"], ["pod", "rm"]):
            pods[args[-1]] = False
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)
    monkeypatch.setattr(live, "RUNNER", podman)
    live._cache.clear()
    directory = sessions.session_dir("sandbox-ai-coding")
    (directory / "tmux").mkdir(parents=True)
    sessions.save("sandbox-ai-coding", {
        "agent": "claude", "options": [], "agent_args": [], "backend": "podman", "workspace": "/srv/app", "cwd": "/",
        "created": "2026-11-12T09:40:00Z", "started": "2026-11-12T09:40:00Z", "name": "sandbox-ai-coding",
        "keep": True, "mode": "tmux", "ready": True, "run": "claude-0e7d6eda", "pod": "agent-claude-0e7d6eda",
        "pid": 4022643})
    return types.SimpleNamespace(st=st, sessions=sessions, live=live, net=net, pods=pods, calls=calls,
                                 runs=Path(tmp_path / "rt" / "sandbox-ai" / "runs"))


def test_a_supervisor_that_ends_while_its_sandbox_runs_is_reaped_its_network_cut_and_said(session_host):
    """The user's terminal held its session's supervisor as a zombie for days and never said a word. Now it reaps it
    at once; the sandbox still running, its network is cut through its control socket (kept run directory), the
    end is recorded (list says it) and said on the terminal (raw: CR LF) and on the desktop."""
    h = session_host
    run_dir = h.runs / "claude-0e7d6eda"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"run": "claude-0e7d6eda", "pid": 4022643, "net_cut": 1,
                                                  "backend": "podman", "pod": "agent-claude-0e7d6eda",
                                                  "start": "sandbox-ai-coding", "name": "sandbox-ai-coding"}))
    asked, said, told = [], [], []
    h.net.cut_one = lambda record, mode, by: asked.append((record["directory"], mode, by)) or \
        {"cut": mode, "cuts": {by: mode}}
    process = Ended(-9)
    end = h.sessions.reap_supervisor("sandbox-ai-coding", process, "claude-0e7d6eda", out=said.append,
                                     notify=lambda title, text: told.append(text))
    assert process.waited == 1  # reaped: no zombie
    assert asked == [(str(run_dir), "all", "supervisor-gone")]
    assert end["cut"] == "all" and "SIGKILL" in end["why"]
    assert "\r\n" in said[0] and "its supervisor ended (killed by SIGKILL) while its sandbox runs on" in said[0]
    assert "its network is cut (kill switch all)" in said[0] and "sandbox-ai stop sandbox-ai-coding" in said[0]
    assert told and "sandbox-ai-coding" in told[0]
    recorded = h.live.supervisor_end(h.sessions.session_dir("sandbox-ai-coding"), run="claude-0e7d6eda")
    assert recorded["by"] == "terminal" and recorded["cut"] == "all" and recorded["status"] == -9
    assert h.pods["agent-claude-0e7d6eda"] is True  # cut, not stopped: the human saves the work first


def test_a_supervisor_whose_run_directory_is_gone_gets_its_pod_stopped(session_host):
    """No run directory (the control socket went with it): the kill switch cannot reach the sandbox, so its pod is
    stopped - fail closed - and that is said."""
    h = session_host
    said = []
    end = h.sessions.reap_supervisor("sandbox-ai-coding", Ended(1), "claude-0e7d6eda", out=said.append,
                                     notify=lambda title, text: None)
    assert ["pod", "stop", "--time", "30", "agent-claude-0e7d6eda"] in h.calls
    assert h.pods["agent-claude-0e7d6eda"] is False and end["stopped"] is True
    assert "its pod was stopped" in said[0] and "exit status 1" in said[0]


def test_a_supervisor_that_ended_as_it_should_is_reaped_quietly(session_host):
    h = session_host
    said = []
    h.pods["agent-claude-0e7d6eda"] = False  # its cleanup removed the pod
    assert h.sessions.reap_supervisor("sandbox-ai-coding", Ended(0), "claude-0e7d6eda", out=said.append,
                                      notify=lambda title, text: said.append(text)) is None
    h.pods["agent-claude-0e7d6eda"] = True  # a later run of the session: not this one's to judge
    assert h.sessions.reap_supervisor("sandbox-ai-coding", Ended(0), "claude-11111111", out=said.append,
                                      notify=lambda title, text: said.append(text)) is None
    h.st.write_exit("sandbox-ai-coding", "claude-0e7d6eda", 0)  # its end recorded: it ended as it should
    assert h.sessions.reap_supervisor("sandbox-ai-coding", Ended(0), "claude-0e7d6eda", out=said.append,
                                      notify=lambda title, text: said.append(text)) is None
    assert said == [] and not any(call[:2] == ["pod", "stop"] for call in h.calls)


def test_launch_reaps_its_supervisor_in_the_background(session_host):
    """sessions.launch starts the reaper once the terminal is ready (a daemon thread: the attach goes on): a real child
    is reaped when it ends (no zombie); anything else (a test's stand-in) is never waited for."""
    import subprocess
    import sys
    import threading
    h = session_host
    h.pods["agent-claude-0e7d6eda"] = False  # it ended as it should: nothing to say
    before = set(threading.enumerate())
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    h.sessions.watch_supervisor("sandbox-ai-coding", child, "claude-0e7d6eda")
    [reaper] = [thread for thread in threading.enumerate() if thread not in before]
    assert reaper.daemon and reaper.name == "reap sandbox-ai-coding"
    reaper.join(10)
    assert child.returncode == 0  # reaped
    h.sessions.watch_supervisor("sandbox-ai-coding", Ended(0), "claude-0e7d6eda")
    assert set(threading.enumerate()) <= before | {reaper}


def test_a_zombie_launcher_counts_as_gone(tmp_path, monkeypatch):
    """os.kill(pid, 0) succeeds for a zombie: net took the dead supervisor for a running one."""
    net = load("net")
    proc = tmp_path / "proc"
    (proc / "4022643").mkdir(parents=True)
    (proc / "4022643" / "stat").write_text("4022643 (python3) Z 4022607 1 1 0 -1\n")
    monkeypatch.setattr(net.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(net, "PROC", proc)
    assert net.alive(4022643) is False
    (proc / "4022643" / "stat").write_text("4022643 (python3 (x) Z) S 4022607 1 1 0 -1\n")
    assert net.alive(4022643) is True


# --- what list, attach and doctor say of an orphaned start --------------------------------------------------------


def test_a_session_orphaned_by_its_supervisors_end_says_why_in_status_list_and_doctor(session_host):
    """The record the terminal (or the launcher, or the sandbox) kept is what status, list --json and doctor say."""
    h = session_host
    h.live.record_supervisor_end(h.sessions.session_dir("sandbox-ai-coding"), "its supervisor (PID 4022643) ended "
                                 "(exit status 1) while its sandbox ran on", "terminal", run="claude-0e7d6eda",
                                 cut="all")
    data = h.sessions.load("sandbox-ai-coding")
    assert h.sessions.status("sandbox-ai-coding", data) == "orphaned"
    assert h.sessions.orphan_end("sandbox-ai-coding", data)["cut"] == "all"
    entry = {e["id"]: e for e in h.st.entries()}["sandbox-ai-coding"]
    assert entry["state"] == "orphaned"
    why = entry["orphaned_why"]
    assert "its supervisor ended at" in why and "(exit status 1)" in why and "network was cut (kill switch all)" in why
    assert why.endswith("sandbox-ai stop sandbox-ai-coding, then: sandbox-ai resume sandbox-ai-coding")
    lines = []
    h.st.show([entry], out=lines.append, colour=False, flat=True)
    assert lines[-1] == f"sandbox-ai-coding is orphaned: {why}"
    doctor = load("doctor")
    level, title, said, fix = doctor.check_orphans(lambda: [entry])
    assert (level, title) == ("fail", "Orphaned starts") and "sandbox-ai-coding" in said and why in fix
    assert doctor.check_orphans(lambda: [])[0] == "ok"
    assert doctor.check_orphans(lambda: 1 / 0)[0] == "skip"


def test_an_orphan_without_a_record_still_says_what_to_do(session_host):
    h = session_host
    entry = {e["id"]: e for e in h.st.entries()}["sandbox-ai-coding"]
    assert "its supervisor (the launcher on the host) is gone while its pod still runs" in entry["orphaned_why"]
    assert "sandbox-ai stop sandbox-ai-coding" in entry["orphaned_why"]


# --- a monitor's write error (a full disk) degrades that monitor, never the run ----------------------------------


def test_a_full_disk_never_ends_the_file_log_it_says_so_once_and_writes_again(tmp_path, monkeypatch, capsys):
    """files.jsonl of claude-e7d6edae stopped at 09:45 for good, unseen: log_event raised ENOSPC in the file log's
    thread, which ended. Now the record is lost, the warning (disk full) said once - on standard error and as a
    safeguard the launcher shows - and the file log goes on; once the disk has room, it writes again."""
    import threading
    import time
    if not Path("/dev/full").exists():
        pytest.skip("no /dev/full here")
    rt = load("runtime")
    logs, control, watched = tmp_path / "log", tmp_path / "control", tmp_path / "work"
    for path in (logs, control, watched):
        path.mkdir()
    monkeypatch.setattr(rt, "LOG_DIR", logs)
    monkeypatch.setattr(rt, "CONTROL_DIR", control)
    (logs / "files.jsonl").symlink_to("/dev/full")  # every write: ENOSPC, as on the user's host
    stop = threading.Event()
    thread = threading.Thread(target=rt.file_logger, args=([str(watched)], stop), daemon=True)
    thread.start()
    try:
        assert rt.FILE_LOGGING.wait(5)
        for number in range(3):
            (watched / f"full-{number}.txt").write_text("x")
            time.sleep(0.3)
        assert thread.is_alive()  # the file log goes on
        err = capsys.readouterr().err
        # The file system that refused is named with its room (here it has room: /dev/full refuses, as a quota or a
        # tmpfs would) - never "the disk is full: free space on the host" (user report 2026-11-20).
        assert err.count(f"the log files.jsonl cannot be written (No space left on device; the file system of {logs}") \
            == 1 and "free space on the host" not in err
        event = json.loads((control / "safeguard-events.jsonl").read_text().splitlines()[0])
        assert event["kind"] == "guard" and "inodes" in event["why"]
        (logs / "files.jsonl").unlink()  # room again
        (watched / "after.txt").write_text("x")
        for _ in range(50):
            if (logs / "files.jsonl").exists() and "after.txt" in (logs / "files.jsonl").read_text():
                break
            time.sleep(0.1)
        assert "after.txt" in (logs / "files.jsonl").read_text()
        assert "the log files.jsonl is written again" in capsys.readouterr().err
    finally:
        stop.set()
        thread.join(5)
