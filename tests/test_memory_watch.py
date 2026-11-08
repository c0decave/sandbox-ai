"""The launcher's memory watch against real processes: a cgroup tree as the kernel shows it (files written by the
test, since a sandbox has no cgroupfs of its own), real children that hold memory, real signals."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_sandbox_memory", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)

GIB = 1 << 30


def child(*argv, hold_mib=0, program=None):
    """A real process with ARGV after its code that holds HOLD_MIB of resident memory; PROGRAM is its argv[0] (an
    agent's command line starts with its program: claude, codex, opencode)."""
    code = f"import time; data = bytearray({hold_mib} << 20); [data.__setitem__(i, 1) for i in range(0, len(data), 4096)]; " \
           "print('ready', flush=True); time.sleep(120)"
    # A relocatable Python (setup-python, uv) finds its standard library from argv[0]: with another program name it
    # needs to be told where it lives.
    environment = {**os.environ, "PYTHONHOME": sys.base_prefix} if program else None
    process = subprocess.Popen([program or sys.executable, "-c", code, *argv], executable=sys.executable,
                               stdout=subprocess.PIPE, text=True, env=environment)
    assert process.stdout.readline().strip() == "ready"
    return process


def cgroup(tmp_path, pids, current, limit=8 * GIB, shmem=0, oom_kill=0, leaf=None):
    """A cgroup v2 directory as the kernel shows it; LEAF: some PIDS sit in a child cgroup."""
    group = tmp_path / "cgroup" / "user.slice" / "libpod-abc.scope"
    group.mkdir(parents=True, exist_ok=True)
    (group / "memory.max").write_text(f"{limit}\n")
    (group / "memory.current").write_text(f"{current}\n")
    (group / "memory.stat").write_text(f"anon {current - shmem}\nfile 0\nshmem {shmem}\n")
    (group / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom 0\noom_kill {oom_kill}\n")
    inner = [p for p in pids if leaf and p in leaf]
    (group / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids if p not in inner))
    if inner:
        (group / "container").mkdir(exist_ok=True)
        (group / "container" / "cgroup.procs").write_text("".join(f"{p}\n" for p in inner))
    return group


@pytest.fixture
def processes():
    started = []
    yield started
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait()


def watch(group, protected=(), **options):
    shared = options.pop("session", None)
    return s.MemoryWatch(lambda: (group, set(protected)), "claude-test", session=shared, **options)


def test_nearly_full_ends_the_largest_process_that_is_not_sandbox_ais_or_the_agents(tmp_path, processes):
    supervisor = child("--internal-bootstrap", hold_mib=60)    # sandbox-ai's own: never
    agent = child(hold_mib=200, program="claude")              # the agent, the largest: never
    small = child("worker", hold_mib=20)
    big = child("pytest-worker", hold_mib=120)                 # the one to end
    processes += [supervisor, agent, small, big]
    pids = [p.pid for p in processes]
    group = cgroup(tmp_path, pids, current=int(7.8 * GIB), leaf={small.pid, big.pid})
    notes, log = [], tmp_path / "memory.jsonl"
    w = watch(group, protected={supervisor.pid}, session=tmp_path, log=log, notify=lambda *a: notes.append(a))
    state = w.tick()
    assert state["current"] == int(7.8 * GIB)
    big.wait(5)  # SIGTERM reached it
    assert big.returncode == -signal.SIGTERM
    assert all(p.poll() is None for p in (supervisor, agent, small))
    # The human is told (notification, log), and the agent reads why in its session directory.
    assert notes and "97%" in notes[0][1] and "claude-test" in notes[0][1]
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["event"] for r in records] == ["memory-warning", "memory-relief"]
    assert records[1]["pid"] == big.pid and records[1]["rss"] >= 100 << 20
    shared = json.loads((tmp_path / "memory.json").read_text())
    assert shared["warning"] is True and shared["percent"] == 97.5 and "fewer things" in shared["advice"]
    assert str(big.pid) not in (tmp_path / "memory.json").read_text()  # nothing about the processes
    lines = w.stop()
    assert len(lines) == 1 and "ended python" in lines[0] and f"PID {big.pid}" in lines[0]


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace(tmp_path, processes):
    stubborn = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, "
                                 "signal.SIG_IGN); data = bytearray(50 << 20); print('ready', flush=True); "
                                 "time.sleep(120)"], stdout=subprocess.PIPE, text=True)
    assert stubborn.stdout.readline().strip() == "ready"
    processes.append(stubborn)
    clock = [0.0]
    group = cgroup(tmp_path, [stubborn.pid], current=int(7.9 * GIB))
    w = watch(group, clock=lambda: clock[0], grace=3.0)
    w.tick()
    time.sleep(0.3)
    assert stubborn.poll() is None  # it ignored SIGTERM
    clock[0] = 3.5
    w.tick()
    stubborn.wait(5)
    assert stubborn.returncode == -signal.SIGKILL


def test_below_the_limits_nothing_happens_and_warnings_have_hysteresis(tmp_path, processes):
    worker = child("worker", hold_mib=10)
    processes.append(worker)
    notes = []
    group = cgroup(tmp_path, [worker.pid], current=4 * GIB)
    w = watch(group, session=tmp_path, notify=lambda *a: notes.append(a))
    w.tick()
    assert notes == [] and worker.poll() is None and json.loads((tmp_path / "memory.json").read_text())["warning"] is False
    cgroup(tmp_path, [worker.pid], current=int(7.0 * GIB))  # 87.5 %: warned once
    w.tick()
    w.tick()
    assert len(notes) == 1 and worker.poll() is None
    cgroup(tmp_path, [worker.pid], current=int(6.7 * GIB))  # 83.8 %: not low enough to clear (hysteresis)
    w.tick()
    assert w.warned
    cgroup(tmp_path, [worker.pid], current=int(6.0 * GIB))
    w.tick()
    assert not w.warned and json.loads((tmp_path / "memory.json").read_text())["warning"] is False
    cgroup(tmp_path, [worker.pid], current=int(7.0 * GIB))
    w.tick()
    assert len(notes) == 2  # warned again after it had cleared
    assert w.stop() == []


def test_tmpfs_full_and_only_protected_processes_ends_nothing_but_says_so(tmp_path, processes):
    agent = child(hold_mib=10, program="/usr/local/bin/codex")
    processes.append(agent)
    notes = []
    group = cgroup(tmp_path, [agent.pid], current=int(7.9 * GIB), shmem=6 * GIB)
    w = watch(group, notify=lambda *a: notes.append(a))
    w.tick()
    assert agent.poll() is None  # the agent is never ended
    assert "files in its /tmp and HOME: 6.0 GiB" in notes[0][1]
    assert w.stop() == []


def test_after_the_run_the_kernels_oom_kills_are_reported(tmp_path):
    group = cgroup(tmp_path, [], current=2 * GIB, oom_kill=1)
    w = watch(group)
    w.tick()
    cgroup(tmp_path, [], current=int(7.99 * GIB), oom_kill=3)
    w.tick()
    lines = w.stop()
    assert len(lines) == 1 and "the kernel ended 2 processes" in lines[0] and "--memory 16g" in lines[0]


def test_the_watch_waits_for_the_sandbox_and_survives_its_end(tmp_path):
    found = []
    w = s.MemoryWatch(lambda: found[0] if found else None, "claude-test")
    assert w.tick() is None  # not started yet
    group = cgroup(tmp_path, [], current=GIB)
    found.append((group, set()))
    assert w.tick()["current"] == GIB
    for name in ("memory.current", "memory.max"):
        (group / name).unlink()
    assert w.tick() is None  # the cgroup went with the sandbox
    assert w.stop() == []


def test_the_thread_runs_and_stops(tmp_path):
    group = cgroup(tmp_path, [], current=GIB)
    w = watch(group, interval=0.05, session=tmp_path).start()
    deadline = time.monotonic() + 5
    while not (tmp_path / "memory.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads((tmp_path / "memory.json").read_text())["used_bytes"] == GIB
    assert w.stop() == [] and not w.thread.is_alive()


def supervisor_child(hold_mib):
    """A real process named as the supervisor is (kthreadd, for Firejail) that holds HOLD_MIB."""
    code = (f"open('/proc/self/comm', 'w').write('kthreadd'); data = bytearray({hold_mib} << 20); "
            "[data.__setitem__(i, 1) for i in range(0, len(data), 4096)]; print('ready', flush=True); "
            "import time; time.sleep(120)")
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "ready"
    return process


def test_the_supervisors_memory_is_recorded_and_reported(tmp_path, processes, monkeypatch):
    """The supervisor (named kthreadd) holding more than it should: recorded at each doubling, the human told once at
    SUPERVISOR_WARN, its own records copied into the memory log at the end, and named in the closing lines."""
    monkeypatch.setattr(s, "SUPERVISOR_RECORD", 32 << 20)
    monkeypatch.setattr(s, "SUPERVISOR_WARN", 64 << 20)
    supervisor = supervisor_child(100)
    other = child("worker", hold_mib=10)
    processes += [supervisor, other]
    group = cgroup(tmp_path, [supervisor.pid, other.pid], current=GIB)
    control = tmp_path / "control"
    control.mkdir()
    (control / "supervisor-memory.jsonl").write_text(
        json.dumps({"rss": 300 << 20, "threads": {"proxy-ask": 900}, "event": "forged", "self": 1}) + "\nnot json\n")
    notes, log = [], tmp_path / "memory.jsonl"
    w = watch(group, protected={supervisor.pid}, log=log, notify=lambda *a: notes.append(a), control=lambda: control)
    w.cgroup = group
    pid, rss = w.supervisor_rss()
    assert pid == supervisor.pid and rss >= 100 << 20
    w.watch_supervisor({"current": GIB, "limit": 8 * GIB})
    w.watch_supervisor({"current": GIB, "limit": 8 * GIB})
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["event"] for e in events] == ["supervisor-memory"]  # once per doubling, not at every look
    assert len(notes) == 1 and "supervisor" in notes[0][1] and "claude-test" in notes[0][1]
    lines = w.stop()
    assert any("supervisor held up to" in line and str(log) in line for line in lines)
    detail = [json.loads(line) for line in log.read_text().splitlines()][-1]
    assert detail["event"] == "supervisor-detail" and detail["threads"] == {"proxy-ask": 900}
    assert "self" not in detail and detail["run"] == "claude-test"


def test_no_supervisor_no_records(tmp_path, processes):
    other = child("worker", hold_mib=10)
    processes.append(other)
    group = cgroup(tmp_path, [other.pid], current=GIB)
    w = watch(group, log=tmp_path / "memory.jsonl")
    w.cgroup = group
    assert w.supervisor_rss() is None
    w.watch_supervisor({"current": GIB, "limit": 8 * GIB})
    assert w.supervisor_records() == 0 and w.stop() == [] and not (tmp_path / "memory.jsonl").exists()


def test_the_watch_throttles_before_the_limit_and_follows_a_new_one(tmp_path, processes):
    """memory.high at MEMORY_HIGH of the limit: the kernel slows the sandbox down there instead of reaching
    memory.max, where its OOM killer would end a process; set again when `sandbox-ai limits --memory` changes it."""
    agent = child(hold_mib=10, program="claude")
    processes.append(agent)
    group = cgroup(tmp_path, [agent.pid], current=1 * GIB)
    w = watch(group, log=tmp_path / "memory.jsonl")
    w.tick()
    assert int((group / "memory.high").read_text()) == int(8 * GIB * s.MEMORY_HIGH)
    (group / "memory.max").write_text(f"{16 * GIB}\n")
    w.tick()
    assert int((group / "memory.high").read_text()) == int(16 * GIB * s.MEMORY_HIGH)
    assert not (tmp_path / "memory.jsonl").exists()  # nothing to report when it works
    (group / "memory.high").unlink()
    (group / "memory.high").mkdir()  # cannot be written (as where the cgroup is not delegated)
    (group / "memory.max").write_text(f"{4 * GIB}\n")
    w.tick()
    records = [json.loads(line) for line in (tmp_path / "memory.jsonl").read_text().splitlines()]
    assert records[-1]["event"] == "memory-high" and records[-1]["error"]


def test_a_sandbox_stalled_below_the_limit_is_relieved(tmp_path, processes):
    """Held at memory.high a sandbox stalls instead of dying: when its processes stall on memory (PSI), the largest
    one that is neither sandbox-ai's nor the agent's ends, as at MEMORY_ACT."""
    agent = child(hold_mib=10, program="claude")
    build = child("cc1plus", hold_mib=60)
    processes += [agent, build]
    group = cgroup(tmp_path, [agent.pid, build.pid], current=int(8 * GIB * 0.93))
    (group / "memory.pressure").write_text("some avg10=2.00 avg60=1.00 avg300=0.50 total=100\n"
                                           "full avg10=1.00 avg60=0.50 avg300=0.10 total=50\n")
    w = watch(group, log=tmp_path / "memory.jsonl")
    w.tick()
    assert build.poll() is None  # a little pressure: nothing ends
    (group / "memory.pressure").write_text("some avg10=80.00 avg60=40.00 avg300=9.00 total=900\n"
                                           "full avg10=35.50 avg60=12.00 avg300=3.00 total=500\n")
    w.tick()
    build.wait(5)
    assert build.returncode == -signal.SIGTERM and agent.poll() is None
    events = [json.loads(line)["event"] for line in (tmp_path / "memory.jsonl").read_text().splitlines()]
    assert "memory-stall" in events and "memory-relief" in events
    assert s.memory_stall(tmp_path) is None  # no PSI: no stall known


def test_a_lifted_limit_lifts_the_throttle(tmp_path, processes):
    agent = child(hold_mib=10, program="claude")
    processes.append(agent)
    group = cgroup(tmp_path, [agent.pid], current=1 * GIB)
    w = watch(group)
    w.tick()
    assert int((group / "memory.high").read_text()) == int(8 * GIB * s.MEMORY_HIGH)
    (group / "memory.max").write_text("max\n")  # sandbox-ai limits --memory none
    assert w.tick() is None
    assert (group / "memory.high").read_text().strip() == "max"


def test_after_a_relief_a_stall_alone_waits_before_the_next(tmp_path, processes):
    """PSI's avg10 decays over ten seconds: right after ending a process the stall still shows, and must not end a
    second one before the first one's memory is back."""
    agent = child(hold_mib=10, program="claude")
    first, second = child("cc1plus", hold_mib=60), child("ld", hold_mib=40)
    processes += [agent, first, second]
    group = cgroup(tmp_path, [agent.pid, first.pid, second.pid], current=int(8 * GIB * 0.93))
    (group / "memory.pressure").write_text("some avg10=80.00 avg60=1 avg300=1 total=9\nfull avg10=40.00 avg60=1 "
                                           "avg300=1 total=9\n")
    now = [1000.0]
    w = watch(group, clock=lambda: now[0])
    w.tick()
    first.wait(5)
    now[0] += 2
    w.tick()
    assert second.poll() is None, "the stall still shows the first one: wait"
    now[0] += s.MEMORY_STALL_PAUSE
    w.tick()
    second.wait(5)
    assert second.returncode == -signal.SIGTERM
