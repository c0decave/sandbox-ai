"""sandbox-ai usage: what a running sandbox uses, from a cgroup tree as the kernel shows it (files written by the
test) and real processes in /proc; the launcher's resources.json where the cgroup cannot be read."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_usage_test", ROOT / "sandbox/usage.py")
u = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(u)

GIB = 1 << 30


def cgroup(root, current=2 * GIB, limit=8 * GIB, usage_usec=0, periods=0, throttled=0, pids=10, pids_max=4096,
           quota="200000 100000", procs=()):
    group = root / "user.slice" / "user@1000.service" / "app.slice" / "libpod-pod.slice"
    (group / "container").mkdir(parents=True, exist_ok=True)
    (group / "memory.max").write_text(f"{limit}\n")
    (group / "memory.current").write_text(f"{current}\n")
    (group / "memory.stat").write_text(f"anon {current}\nshmem {GIB // 2}\n")
    (group / "memory.events").write_text("oom_kill 0\n")
    (group / "cpu.stat").write_text(f"usage_usec {usage_usec}\nnr_periods {periods}\nnr_throttled {throttled}\n"
                                    "throttled_usec 2500000\n")
    (group / "cpu.max").write_text(quota + "\n")
    (group / "pids.current").write_text(f"{pids}\n")
    (group / "pids.max").write_text(f"{pids_max}\n")
    (group / "cgroup.procs").write_text("")
    (group / "container" / "cgroup.procs").write_text("".join(f"{p}\n" for p in procs))
    return group


@pytest.fixture
def busy():
    """A real process that uses a CPU while the test measures."""
    process = subprocess.Popen([sys.executable, "-c", "import time\nprint('ready', flush=True)\n"
                                "end = time.time() + 60\nwhile time.time() < end: pass"], stdout=subprocess.PIPE,
                               text=True)
    assert process.stdout.readline().strip() == "ready"
    yield process
    process.kill()
    process.wait()


def record(**extra):
    return {"run": "claude-abc", "name": "web", "agent": "claude", "backend": "podman", "workspace": "/w",
            "session_dir": "/nonexistent", **extra}


def test_measures_cpu_memory_processes_and_the_top_ones(tmp_path, busy):
    root = tmp_path / "cg"
    group = cgroup(root, procs=[busy.pid])
    relative = "/" + str((group / "container").relative_to(root))
    clock = [100.0]

    def sleep(seconds):
        # Over 2 s: 3 CPUs of usage, 60 of 100 periods held back.
        clock[0] += 2.0
        cgroup(root, usage_usec=3_000_000 * 2, periods=100, throttled=60, pids=12, procs=[busy.pid])
        import time
        time.sleep(0.3)  # the real process uses CPU meanwhile
    [entry] = u.measure([record(mode={"name": "high"})], 2.0, finder=lambda r: {"cgroup": relative, "pids": []},
                        root=root, sleep=sleep, clock=lambda: clock[0], host_cpus=8)
    assert entry["source"] == "cgroup" and entry["cgroup"] == str(group)  # the scope with the limits, not the leaf
    assert entry["cpu"]["used_cpus"] == 3.0 and entry["cpu"]["limit_cpus"] == 2.0 and entry["cpu"]["percent"] == 150.0
    assert entry["cpu"]["throttled_percent"] == 60.0 and entry["cpu"]["throttled_seconds"] == 2.5
    assert entry["memory"] == {"used_bytes": 2 * GIB, "limit_bytes": 8 * GIB, "percent": 25.0,
                               "tmpfs_bytes": GIB // 2, "oom_kills": 0}
    assert entry["pids"] == {"current": 12, "limit": 4096, "percent": 0.3}
    assert entry["warnings"] == ["cpu"]
    [top] = entry["top"]
    assert top["pid"] == busy.pid and top["name"].startswith("python") and top["rss_bytes"] > 0
    assert top["threads"] >= 1 and top["cpu_percent"] >= 0
    assert entry["caps"]["mode"] == "high" and entry["caps"]["memory"]  # the strict mode caps the limits
    text = "\n".join(u.lines(entry))
    assert "web (claude-abc)" in text and "3 (of 2 CPUs; 150 %), held back in 60 % of the periods" in text
    assert "2.0 GiB of 8.0 GiB (25 %), files in /tmp and HOME 0.5 GiB" in text and "12 of 4096" in text
    assert "the CPU limit holds it back" in text and str(busy.pid) in text
    assert "sandbox-ai limits claude-abc" in text and "security mode high: at most memory 4g" in text


def test_without_limits_the_host_cpus_are_the_measure(tmp_path):
    root = tmp_path / "cg"
    group = cgroup(root, quota="max 100000", pids_max="max")
    (group / "memory.max").write_text("max\n")
    (group / "pids.max").write_text("50\n")  # the only limit: still the scope that carries the run
    relative = "/" + str(group.relative_to(root))
    clock = [0.0]

    def sleep(seconds):
        clock[0] += 1.0
        (group / "cpu.stat").write_text("usage_usec 2000000\nnr_periods 0\nnr_throttled 0\n")
    [entry] = u.measure([record()], finder=lambda r: {"cgroup": relative, "pids": []}, root=root, sleep=sleep,
                        clock=lambda: clock[0], host_cpus=8)
    assert entry["cpu"]["limit_cpus"] is None and entry["cpu"]["percent"] == 25.0
    assert entry["memory"]["limit_bytes"] is None and entry["memory"]["percent"] is None
    assert entry["warnings"] == [] and entry["caps"] == {"mode": None, "memory": None, "cpus": None, "pids": None}
    text = "\n".join(u.lines(entry))
    assert "no limit, 8 on the host" in text and "(no limit)" in text and "security mode" not in text


def test_where_the_cgroup_cannot_be_read_the_launchers_record_is_shown(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    slept = []
    [entry] = u.measure([record(session_dir=str(session))], finder=lambda r: None, sleep=slept.append)
    assert entry["source"] is None and slept == []  # nothing measured: no waiting either
    assert "no usage known" in "\n".join(u.lines(entry))
    (session / "resources.json").write_text(json.dumps({
        "memory": {"used_bytes": 7 * GIB, "limit_bytes": 8 * GIB, "percent": 87.5, "tmpfs_bytes": 0},
        "cpu": {"used_cpus": 1.0, "limit_cpus": 4, "host_cpus": 8, "percent": 25.0, "throttled_percent": 0},
        "pids": {"current": 3, "limit": 100, "percent": 3.0}, "updated": "2026-11-07T10:00:00Z"}))
    [entry] = u.measure([record(session_dir=str(session))], finder=lambda r: None, sleep=slept.append)
    assert entry["source"] == "watch" and entry["warnings"] == ["memory"] and entry["memory"]["percent"] == 87.5
    assert "the launcher's last record, 2026-11-07T10:00:00Z" in "\n".join(u.lines(entry))


def test_process_times_reads_names_with_spaces_and_skips_the_gone(tmp_path):
    proc = tmp_path / "proc"
    (proc / "5").mkdir(parents=True)
    (proc / "5" / "stat").write_text("5 (tmux: server) S 1 5 5 0 -1 4194560 1 0 0 0 30 12 0 0 20 0 1 0 1 1 1\n")
    (proc / "5" / "status").write_text("Name:\ttmux: server\nVmRSS:\t2048 kB\nThreads:\t3\n")
    assert u.process_times([5, 6], proc) == {5: ("tmux: server", 42, 2 << 20, 3)}


def test_the_command_line(tmp_path, monkeypatch, capsys):
    root = tmp_path / "cg"
    group = cgroup(root)
    runs = {"claude-abc": record()}
    net = type("Net", (), {"running": staticmethod(lambda: runs),
                           "choose": staticmethod(lambda r, name, **k: r[name] if name else next(iter(r.values())))})
    monkeypatch.setitem(u._modules, "net", net)
    monkeypatch.delenv("SANDBOX_AI_RUN", raising=False)
    monkeypatch.setattr(u, "look", lambda record, *a, **k: (group, []))
    out = []
    assert u.main(["claude-abc", "--json", "--interval", "0.1"], out=out.append, sleep=lambda s: None) == 0
    data = json.loads(out[0])
    assert data["run"] == "claude-abc" and data["memory"]["used_bytes"] == 2 * GIB
    out.clear()
    assert u.main(["--all", "--json"], out=out.append, sleep=lambda s: None) == 0
    assert [entry["run"] for entry in json.loads(out[0])] == ["claude-abc"]
    out.clear()
    assert u.main([], out=out.append, sleep=lambda s: None) == 0 and "memory     2.0 GiB of 8.0 GiB" in out[0]
    # --follow: one JSON line per look, until Ctrl-C.
    looks = []

    def interrupt(seconds):
        looks.append(seconds)
        if len(looks) == 3:
            raise KeyboardInterrupt
    out.clear()
    assert u.main(["--follow", "--json"], out=out.append, sleep=interrupt) == 0
    assert len(out) == 2 and all(json.loads(line)["run"] == "claude-abc" for line in out)
    with pytest.raises(SystemExit):
        u.main(["--interval", "0"])
    with pytest.raises(SystemExit):
        u.main(["claude-abc", "--all"])
    runs.clear()
    net.choose = staticmethod(lambda r, name, **k: (_ for _ in ()).throw(ValueError("no sandbox is running")))
    assert u.main([]) == 1 and "no sandbox is running" in capsys.readouterr().err


def test_main_knows_the_command():
    spec = importlib.util.spec_from_file_location("sandbox_main_usage_test", ROOT / "sandbox/main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    assert main.PYTHON_COMMANDS["usage"] == ("usage.py",) and "sandbox-ai usage" in main.__doc__
