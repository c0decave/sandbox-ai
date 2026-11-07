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
    process = subprocess.Popen([program or sys.executable, "-c", code, *argv], executable=sys.executable,
                               stdout=subprocess.PIPE, text=True)
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
