"""The system monitor (monitor.py): counters read from a cgroup tree as the kernel shows it (files written by the
test), /proc of real processes, `podman stats` as Podman prints it; samples, rollups, the bounded series files,
warnings with their suggestions, the collector of a run, and what `top` and `stats usage` make of it all."""
import argparse
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_monitor_test", ROOT / "sandbox/monitor.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

GIB = 1 << 30


def pod(root, current=2 * GIB, limit=8 * GIB, usage_usec=0, periods=0, throttled=0, pids=10, pids_max=4096,
        quota="200000 100000", rbytes=0, wbytes=0, oom=0, high=0, psi_io="0.00", procs=(1, 2)):
    """A Podman pod's cgroup as the kernel shows it: the pod's slice, the agent's container scope below it with the
    limits, PSI files."""
    top = root / "user.slice" / "user@1000.service" / "user.slice" / "machine-libpod_pod_abc.slice"
    leaf = top / "libpod-0123.scope" / "container"
    leaf.mkdir(parents=True, exist_ok=True)
    scope = leaf.parent
    (scope / "memory.max").write_text(f"{limit}\n")
    (scope / "cpu.max").write_text(quota + "\n")
    (scope / "pids.max").write_text(f"{pids_max}\n")
    (top / "memory.max").write_text("max\n")
    (top / "memory.current").write_text(f"{current}\n")
    (top / "memory.peak").write_text(f"{current + GIB // 4}\n")
    (top / "memory.stat").write_text(f"anon {current}\nshmem {GIB // 2}\n")
    (top / "memory.events").write_text(f"low 0\nhigh {high}\nmax 0\noom 0\noom_kill {oom}\n")
    (top / "cpu.stat").write_text(f"usage_usec {usage_usec}\nuser_usec 0\nsystem_usec 0\nnr_periods {periods}\n"
                                  f"nr_throttled {throttled}\nthrottled_usec 0\n")
    (top / "io.stat").write_text(f"8:0 rbytes={rbytes} wbytes={wbytes} rios=1 wios=1 dbytes=0 dios=0\n"
                                 f"259:0 rbytes=0 wbytes={wbytes} rios=0 wios=0\n")
    (top / "pids.current").write_text(f"{pids}\n")
    (top / "cgroup.procs").write_text("")
    (leaf / "cgroup.procs").write_text("".join(f"{p}\n" for p in procs))
    for name in ("cpu", "memory", "io"):
        some = psi_io if name == "io" else "1.50"
        text = f"some avg10={some} avg60=0.00 avg300=0.00 total=1\n"
        if name != "cpu":
            text += "full avg10=0.25 avg60=0.00 avg300=0.00 total=1\n"
        (top / f"{name}.pressure").write_text(text)
    return top, leaf


def fake_proc(root, pid=4242, cgroup="/user.slice/user@1000.service/user.slice/machine-libpod_pod_abc.slice/"
                                     "libpod-0123.scope/container", rx=1000, tx=500, netns="net:[1]"):
    proc = root / "proc"
    (proc / str(pid) / "net").mkdir(parents=True, exist_ok=True)
    (proc / str(pid) / "ns").mkdir(exist_ok=True)
    (proc / str(pid) / "cgroup").write_text(f"0::{cgroup}\n")
    (proc / str(pid) / "net" / "dev").write_text(
        "Inter-|   Receive                            |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls\n"
        f"    lo: 99999 1 0 0 0 0 0 0 99999 1 0 0 0 0 0 0\n"
        f"  eth0: {rx} 10 0 0 0 0 0 0 {tx} 5 0 0 0 0 0 0\n")
    link = proc / str(pid) / "ns" / "net"
    if not link.is_symlink():
        link.symlink_to(netns)
    return proc


def test_cgroup_counters_read_the_pods_use_and_the_containers_limits(tmp_path):
    top, leaf = pod(tmp_path / "cg", usage_usec=5_000_000, periods=100, throttled=40, rbytes=4096, wbytes=8192,
                    high=3, psi_io="12.50")
    proc = fake_proc(tmp_path)
    found = m.cgroup_counters(top, leaf, 4242, proc, clock=lambda: 1000.0, own_netns="net:[999]")
    assert found["mem"] == 2 * GIB and found["mem_lim"] == 8 * GIB and found["cpu_lim"] == 2.0
    assert found["pids_lim"] == 4096 and found["peak"] == 2 * GIB + GIB // 4 and found["tmpfs"] == GIB // 2
    assert found["cpu_usec"] == 5_000_000 and found["periods"] == 100 and found["throttled"] == 40
    assert found["rbytes"] == 4096 and found["wbytes"] == 16384  # every device summed
    assert found["rx_bytes"] == 1000 and found["tx_bytes"] == 500  # lo left out
    assert found["psi_io"] == 12.5 and found["psi_io_full"] == 0.25 and found["psi_cpu"] == 1.5
    assert found["high"] == 3 and found["oom"] == 0 and found["procs"] == 2 and found["pids"] == 10
    # The host's own network namespace (a sandbox without one of its own): no network counted, never the host's.
    shared = m.cgroup_counters(top, leaf, 4242, proc, own_netns="net:[1]")
    assert "rx_bytes" not in shared
    assert m.cgroup_counters(tmp_path / "gone", None) is None


def test_cgroup_of_finds_the_pod_or_the_scope_and_never_the_users_slice(tmp_path):
    root = tmp_path / "cg"
    top, leaf = pod(root)
    proc = fake_proc(tmp_path)
    assert m.cgroup_of(4242, proc, root) == (top, leaf)
    scope = root / "user.slice/user@1000.service/app.slice/run-r12.scope"
    scope.mkdir(parents=True)
    (proc / "4243").mkdir()
    (proc / "4243" / "cgroup").write_text("0::/user.slice/user@1000.service/app.slice/run-r12.scope\n")
    assert m.cgroup_of(4243, proc, root) == (scope, scope)
    (proc / "4244").mkdir()
    (proc / "4244" / "cgroup").write_text("0::/user.slice/user@1000.service/session.slice/vte-spawn.scope\n")
    assert m.cgroup_of(4244, proc, root) is None  # the terminal's scope is not the run's
    assert m.cgroup_of(99999, proc, root) is None


def test_sample_makes_rates_and_shares():
    before = {"time": 100.0, "cpu_usec": 0, "periods": 0, "throttled": 0, "rbytes": 0, "wbytes": 0, "rx_bytes": 0,
              "tx_bytes": 0, "oom": 1, "high": 0}
    after = {"time": 110.0, "cpu_usec": 15_000_000, "periods": 100, "throttled": 60, "rbytes": 10240,
             "wbytes": 20480, "rx_bytes": 1000, "tx_bytes": 2000, "oom": 2, "high": 5, "mem": GIB, "mem_lim": 4 * GIB,
             "cpu_lim": 2.0, "pids": 7, "pids_lim": 0}
    s = m.sample(before, after, host_cpus=8)
    assert s["cpu"] == 1.5 and s["cpu_pct"] == 75.0 and s["thr"] == 60.0 and s["dt"] == 10.0
    assert s["rd"] == 1024 and s["wr"] == 2048 and s["rx"] == 100 and s["tx"] == 200
    assert s["oom"] == 1 and s["high"] == 5 and s["mem_pct"] == 25.0 and "pids_lim" not in s
    no_limit = m.sample(before, {**after, "cpu_lim": 0}, host_cpus=8)
    assert no_limit["cpu_pct"] == 18.8  # of the host's 8 CPUs
    assert m.sample(None, after) is None and m.sample(after, after) is None
    # A counter that went back (a container restarted in the pod): never a negative rate.
    assert m.sample({**before, "rbytes": 10 ** 9}, after)["rd"] == 0


def test_podman_stats_raw_and_printed():
    raw = "\n".join(json.dumps(row) for row in [
        {"Name": "agent-claude-1234-agent", "CPUNano": 3_000_000_000, "MemUsage": GIB, "MemLimit": 8 * GIB,
         "NetInput": 100, "NetOutput": 50, "BlockInput": 10, "BlockOutput": 20, "PIDs": 7},
        {"Name": "agent-claude-1234-service-db", "CPUNano": 1_000_000_000, "MemUsage": GIB, "MemLimit": 2 * GIB,
         "NetInput": 1, "NetOutput": 1, "BlockInput": 0, "BlockOutput": 0, "PIDs": 3},
        {"Name": "agent-codex-9999-agent", "CPUNano": 9, "MemUsage": 9}])
    found = m.stats_counters(raw, "agent-claude-1234-", clock=lambda: 5.0)
    assert found["cpu_usec"] == 4_000_000 and found["mem"] == 2 * GIB and found["mem_lim"] == 8 * GIB
    assert found["rx_bytes"] == 101 and found["wbytes"] == 20 and found["pids"] == 10 and found["source"] == "podman"
    printed = json.dumps([{"name": "agent-claude-1234-agent", "cpu_percent": "150.5%", "mem_usage": "1.5GB / 8GB",
                           "net_io": "1.2kB / 648B", "block_io": "4MiB / 0B", "pids": "12"}])
    found = m.stats_counters(printed, "agent-claude-1234-")
    assert found["cpu_now"] == 1.505 and found["mem"] == 1_500_000_000 and found["rx_bytes"] == 1200
    assert found["tx_bytes"] == 648 and found["rbytes"] == 4 << 20 and found["pids"] == 12
    assert m.sample({"time": 0.0}, {**found, "time": 30.0})["cpu"] == 1.505
    assert m.stats_counters(raw, "agent-gone-") is None and m.stats_counters("not json", "x") is None


def test_rollups_average_by_time_and_keep_the_peaks():
    rollup = m.Rollup(60)
    assert rollup.add({"t": 0, "dt": 10, "cpu": 1.0, "mem": 100, "oom": 1, "mem_lim": 1000}) is None
    assert rollup.add({"t": 10, "dt": 30, "cpu": 3.0, "mem": 300, "oom": 0, "mem_lim": 1000}) is None
    done = rollup.add({"t": 60, "dt": 10, "cpu": 0.0, "mem": 50})
    assert done["t"] == 0 and done["n"] == 2 and done["cpu"] == 2.5 and done["cpu_max"] == 3.0
    assert done["mem_max"] == 300 and done["oom"] == 1 and done["mem_lim"] == 1000 and done["dt"] == 40
    hour = m.Rollup(3600)
    hour.add(done)
    hour.add({**done, "t": 60, "cpu": 0.5, "cpu_max": 9.0})
    whole = hour.flush()
    assert whole["n"] == 4 and whole["cpu_max"] == 9.0 and whole["cpu"] == 1.5 and whole["oom"] == 2


def test_series_files_are_bounded_and_rolled_up(tmp_path):
    tiers = (("monitor", None, 100), ("monitor-1m", 60, 600), ("monitor-1h", 3600, 7200))
    series = m.Series(tmp_path, "claude-abc", {"run": "claude-abc", "workspace": "/w"}, 10, stamp="20261117-100000",
                      tiers=tiers)
    for index in range(200):
        series.write({"t": index * 10, "dt": 10, "cpu": 1.0, "mem": GIB})
    series.event({"event": "warning", "t": 1990, "kind": "memory", "text": "x"})
    series.close()
    raw = (tmp_path / "monitor-20261117-100000-claude-abc.jsonl").read_text().splitlines()
    assert json.loads(raw[0])["header"] == 1 and json.loads(raw[0])["interval"] == 10
    assert len(raw) - 1 <= 10 * m.TRIM_SLACK + 1  # 100 s of 10 s samples kept (+ the warning)
    assert json.loads(raw[-1])["event"] == "warning"
    minutes = [json.loads(line) for line in (tmp_path / "monitor-1m-20261117-100000-claude-abc.jsonl").read_text()
               .splitlines()[1:]]
    assert len([entry for entry in minutes if "event" not in entry]) <= 10 * m.TRIM_SLACK + 1
    hours = [json.loads(line) for line in (tmp_path / "monitor-1h-20261117-100000-claude-abc.jsonl").read_text()
             .splitlines()[1:]]
    assert hours[-1]["t"] == 0 and hours[-1]["cpu"] == 1.0  # the hour begun, written at close
    header, samples, events = m.read_series(tmp_path / "monitor-20261117-100000-claude-abc.jsonl")
    assert header["workspace"] == "/w" and samples[-1]["t"] == 1990 and events[0]["kind"] == "memory"


def feed(assessor, samples):
    started, ended = [], []
    for entry in samples:
        new, gone = assessor.feed(entry)
        started += new
        ended += gone
    return started, ended


def test_warnings_start_once_end_and_suggest_the_fix():
    a = m.Assessor("claude-abc", "web", host_cpus=8)
    base = {"mem_lim": 8 * GIB, "cpu_lim": 2.0, "pids_lim": 100}
    started, _ = feed(a, [{**base, "t": 0, "mem": int(7.8 * GIB), "high": 3, "cpu": 1.0, "thr": 10, "pids": 5}])
    [memory] = started
    assert memory["kind"] == "memory" and memory["argv"] == ["limits", "claude-abc", "--memory", "12g"]
    assert memory["advice"] == "raise --memory to 12g: peak 7.8 of 8.0 GiB with 3 throttling events"
    assert feed(a, [{**base, "t": 10, "mem": int(7.9 * GIB), "cpu": 1.0, "pids": 5}]) == ([], [])  # once
    _, ended = feed(a, [{**base, "t": 20, "mem": 4 * GIB, "cpu": 1.0, "pids": 5}])
    assert [w["kind"] for w in ended] == ["memory"]
    # The CPU limit holding it back for two minutes.
    held = [{**base, "t": 30 + 10 * i, "mem": GIB, "cpu": 2.0, "thr": 74.0, "pids": 5} for i in range(13)]
    started, _ = feed(a, held)
    [cpu] = started
    assert cpu["argv"] == ["limits", "claude-abc", "--cpus", "4"] and "74 % of the periods for 2 min" in cpu["advice"]
    # An OOM kill: at once, an error.
    started, _ = feed(a, [{**base, "t": 200, "mem": GIB, "oom": 2, "cpu": 0.5, "pids": 5}])
    assert [(w["kind"], w["level"]) for w in started] == [("oom", "error")] and "2 processes" in started[0]["text"]
    # Processes near the limit.
    started, _ = feed(a, [{**base, "t": 210, "mem": GIB, "cpu": 0.5, "pids": 95}])
    assert started[0]["kind"] == "pids" and started[0]["argv"] == ["limits", "claude-abc", "--pids", "1024"]


def test_idle_holding_memory_and_io_pressure():
    a = m.Assessor("claude-abc", "web")
    idle = [{"t": i * 60, "cpu": 0.005, "mem": 3 * GIB + GIB // 5} for i in range(31)]
    started, _ = feed(a, idle)
    [warning] = started
    assert warning["kind"] == "idle" and warning["argv"] == ["stop", "web"]
    assert warning["advice"] == "stop idle run web: 3.2 GiB for 30 min; it is not kept: what runs in it ends for good"
    _, ended = feed(a, [{"t": 2000, "cpu": 0.5, "mem": 3 * GIB}])
    assert [w["kind"] for w in ended] == ["idle"]
    io = [{"t": 3000 + i * 10, "cpu": 0.5, "mem": GIB, "psi_io": 55.0, "psi_io_full": 30.0} for i in range(7)]
    started, _ = feed(a, io)
    assert [w["kind"] for w in started] == ["io"] and "55 % of the time" in started[0]["text"]


def test_host_warnings():
    h = m.HostAssessor()
    low = [{"t": i * 10, "mem_total": 16 * GIB, "mem_avail": GIB, "swap_total": 8 * GIB, "swap_used": 6 * GIB,
            "swap_in": 200.0} for i in range(7)]
    started, _ = feed(h, low)
    assert sorted(w["kind"] for w in started) == ["host-memory", "host-swap"]
    _, ended = feed(h, [{"t": 100, "mem_total": 16 * GIB, "mem_avail": 8 * GIB, "swap_total": 8 * GIB,
                         "swap_used": 6 * GIB, "swap_in": 0.0}])
    assert sorted(w["kind"] for w in ended) == ["host-memory", "host-swap"]


def test_host_counters_from_proc(tmp_path):
    proc = tmp_path / "proc"
    (proc / "pressure").mkdir(parents=True)
    (proc / "stat").write_text("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 1 1 1\n")
    (proc / "loadavg").write_text("1.25 0.50 0.25 1/100 1234\n")
    (proc / "meminfo").write_text("MemTotal: 16000 kB\nMemFree: 1000 kB\nMemAvailable: 4000 kB\nSwapTotal: 2000 kB\n"
                                  "SwapFree: 500 kB\n")
    (proc / "vmstat").write_text("pswpin 10\npswpout 20\n")
    (proc / "diskstats").write_text("   8       0 sda 1 0 100 0 2 0 200 0 0 0 0\n"
                                    "   8       1 sda1 1 0 100 0 2 0 200 0 0 0 0\n   7 0 loop0 1 0 999 0 1 0 999 0\n")
    (proc / "pressure" / "io").write_text("some avg10=3.00 avg60=0 avg300=0 total=0\n")
    blocks = tmp_path / "block"
    for name in ("sda", "loop0"):
        (blocks / name).mkdir(parents=True)
    before = m.host_counters(proc, clock=lambda: 0.0, sys_block=blocks)
    assert before["mem_total"] == 16000 * 1024 and before["mem_avail"] == 4000 * 1024
    assert before["swap_used"] == 1500 * 1024 and before["load1"] == 1.25 and before["psi_io"] == 3.0
    assert before["rbytes"] == 100 * 512 and before["wbytes"] == 200 * 512  # the disk, not its partition nor loop0
    (proc / "stat").write_text("cpu  200 0 200 750 100 0 0 0 0 0\n")
    (proc / "vmstat").write_text("pswpin 110\npswpout 20\n")
    s = m.host_sample(before, m.host_counters(proc, clock=lambda: 10.0, sys_block=blocks))
    assert s["cpu_pct"] == 80.0 and s["swap_in"] == 10.0 and s["dt"] == 10.0
    assert m.host_counters(tmp_path / "none") is None


def test_tree_counters_measure_a_real_process_tree(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time\nx = bytearray(20 << 20)\nprint('ready', "
                              "flush=True)\ntime.sleep(30)"], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        found = m.tree_counters(os.getpid())
        assert found["source"] == "tree" and found["procs"] >= 2 and found["mem"] > 20 << 20
        assert found["pids"] >= 2 and found["cpu_usec"] > 0
        assert child.pid in m.descendants(os.getpid())
    finally:
        child.kill()
        child.wait()
    assert m.tree_counters(2 ** 22 + 12345) is None


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_recorder_writes_series_live_record_and_warns(tmp_path):
    root = tmp_path / "cg"
    top, leaf = pod(root, current=int(7.5 * GIB), usage_usec=0)
    proc = fake_proc(tmp_path)
    clock, said = Clock(), []
    host_values = iter(range(1000))

    def host():
        value = next(host_values)
        return {"time": clock(), "cpu_total": value * 100, "cpu_idle": value * 50, "mem_total": 16 * GIB,
                "mem_avail": 8 * GIB, "cpus": 8}
    recorder = m.Recorder({"run": "claude-abc", "name": "web", "agent": "claude", "workspace": "/w", "kind": "agent"},
                          lambda: m.cgroup_counters(top, leaf, 4242, proc, clock=clock, own_netns="x"),
                          tmp_path / "logs", interval=10, warn=lambda w, started: said.append((w["kind"], started)),
                          host=host, root=tmp_path / "monitor", host_cpus=8, clock=clock)
    for step in range(1, 8):
        clock.now += 10
        pod(root, current=int(7.5 * GIB) + step * (GIB // 10), usage_usec=step * 10_000_000)
        recorder.tick()
    live = m.live_record("claude-abc", tmp_path / "monitor", now=clock.now)
    assert live["sample"]["cpu"] == 1.0 and live["source"] == "cgroup" and len(live["spark"]) == 6
    assert [w["kind"] for w in live["warnings"]] == ["memory"] and said == [("memory", True)]
    assert live["warnings"][0]["argv"][:3] == ["limits", "claude-abc", "--memory"]
    shown = m.live_record("host", tmp_path / "monitor", now=clock.now)
    assert shown["sample"]["cpu_pct"] == 50.0
    recorder.stop()
    assert not (tmp_path / "monitor" / "live" / "claude-abc.json").exists()
    files = m.series_files([tmp_path / "logs"])["claude-abc"]
    assert set(files) == {"monitor", "monitor-1m", "monitor-1h"}
    _, samples, events = m.read_run(files, 0, "monitor")
    assert len(samples) == 6 and events[0]["kind"] == "memory"
    # A second recorder of another run: the first holds no lock any more, the host is written by this one.
    other = m.Recorder({"run": "codex-1"}, lambda: None, tmp_path / "logs", host=host, root=tmp_path / "monitor",
                       clock=clock)
    other.tick()
    assert other.host_lock.fd is not None
    other.stop()


def test_a_warning_that_comes_back_soon_is_recorded_not_said_again(tmp_path):
    clock, said = Clock(), []
    recorder = m.Recorder({"run": "r1", "name": "web"}, lambda: None, tmp_path, warn=lambda w, s: said.append(s),
                          root=tmp_path / "mon", clock=clock)
    warning = {"kind": "memory", "since": 1, "text": "t", "advice": "a", "values": {}}
    recorder.say(warning, True)
    recorder.say(warning, False)
    clock.now += 60
    recorder.say(warning, True)
    assert said == [True, False]
    clock.now += m.RENOTIFY
    recorder.say(warning, True)
    assert said == [True, False, True]


def write_run(folder, run, header, samples, kind="monitor", gz=False, stamp="20261117-100000"):
    folder.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"header": 1, "run": run, "interval": 10, **header})] + [json.dumps(s) for s in samples]
    path = folder / (f"{kind}-{stamp}-{run}.jsonl" + (".gz" if gz else ""))
    data = ("\n".join(lines) + "\n").encode()
    path.write_bytes(gzip.compress(data) if gz else data)
    last = max([s["t"] for s in samples] or [time.time()])
    os.utime(path, (last, last))  # (written last when its newest sample was)
    return path


def test_stats_usage_groups_by_project_attributes_mcp_servers_and_finds_idle_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    now = 1_800_000_000
    logs = tmp_path / "state" / "sandbox-ai" / "logs"
    busy = [{"t": now - 3000 + i * 10, "dt": 10, "cpu": 2.0, "mem": 2 * GIB, "rd": 1000, "wr": 2000, "rx": 10,
             "tx": 20, "pids": 50} for i in range(100)]
    write_run(logs / "web-1", "claude-aaa", {"name": "web", "agent": "claude", "workspace": "/p/web",
                                             "mcp_servers": ["github"]}, busy + [
        {"event": "warning", "t": now - 100, "kind": "memory", "text": "near", "advice": "raise"}])
    idle = [{"t": now - 2400 + i * 60, "dt": 60, "cpu": 0.001, "mem": 3 * GIB} for i in range(40)]
    write_run(logs / "api-2", "codex-bbb", {"name": "api", "agent": "codex", "workspace": "/p/api"}, idle,
              gz=True)  # compressed after its run ended: read as it is
    write_run(tmp_path / "state" / "sandbox-ai" / "monitor" / "runs", "mcp-github", {
        "name": "github", "kind": "mcp", "mcp_name": "github", "workspace": "/srv"},
        [{"t": now - 3000 + i * 10, "dt": 10, "cpu": 0.5, "mem": GIB} for i in range(100)])
    old = write_run(logs / "web-1", "claude-old", {"name": "old", "workspace": "/p/web"},
                    [{"t": now - 30 * 86400, "dt": 10, "cpu": 9.0}], stamp="20261001-000000")
    os.utime(old, (now - 30 * 86400, now - 30 * 86400))
    data = m.usage("2h", "project", now=now, series=True)
    keys = {group["key"]: group for group in data["groups"]}
    assert set(keys) == {"/p/web", "/p/api"}  # the MCP server counts for the start it serves
    web = keys["/p/web"]
    assert web["runs"] == 2 and web["cpu_seconds"] == 2500.0 and web["read"] == 1000 * 1000
    assert web["warnings"] == 1 and web["series"]
    runs = {row["run"]: row for row in data["runs"]}
    assert runs["mcp-github"]["parent"] == "web" and runs["mcp-github"]["project"] == "/p/web"
    assert "claude-old" not in runs  # outside the window
    assert data["idle"][0]["name"] == "api" and data["idle"][0]["memory"] == 3 * GIB
    assert data["top"][0]["name"] == "web" and data["warnings"][0]["name"] == "web"
    by_agent = m.usage("2h", "agent", now=now)
    assert {group["key"] for group in by_agent["groups"]} == {"claude", "codex", "(none)"}
    only = m.usage("2h", "run", project="/p/api", now=now)
    assert [row["run"] for row in only["runs"]] == ["codex-bbb"]
    assert "web" in m.usage_text(data) and "idle while holding memory" in m.usage_text(data)
    with pytest.raises(ValueError):
        m.usage("soon")


def test_the_reader_takes_files_as_untrusted_data(tmp_path):
    path = tmp_path / "monitor-20261117-100000-r.jsonl"
    path.write_text(json.dumps({"header": 1, "run": "r", "name": "\x1b[31mred", "workspace": ["x"]}) + "\n"
                    + "not json\n" + json.dumps({"t": 5, "cpu": "1e309", "mem": True, "rd": float("nan")}) + "\n"
                    + "x" * (m.LINE_LIMIT + 10) + "\n" + json.dumps([1, 2]) + "\n" + json.dumps({"t": 6, "cpu": 1})
                    + "\n")
    header, samples, _ = m.read_series(path)
    assert header["name"] == "?[31mred" and "workspace" not in header
    assert samples == [{"t": 5}, {"t": 6, "cpu": 1}]
    live = tmp_path / "live"
    live.mkdir()
    (live / "r.json").write_text(json.dumps({"updated": 100, "interval": 10, "sample": {"cpu": "x", "mem": 5},
                                             "warnings": [{"kind": "bogus"}, {"kind": "memory", "argv": [1]}],
                                             "spark": [[1, 2, 3, 4, 5], [1], "x"]}))
    found = m.live_record("r", tmp_path, now=110)
    assert found["sample"] == {"mem": 5} and [w["kind"] for w in found["warnings"]] == ["memory"]
    assert found["warnings"][0]["argv"] is None and found["spark"] == [[1, 2, 3, 4, 5]]
    assert m.live_record("r", tmp_path, now=200) is None  # stale


def test_top_rows_use_the_live_records_and_attribute_parents(tmp_path):
    root = tmp_path / "monitor"
    now = time.time()
    m.write_json(root / "live" / "claude-a.json", {"updated": now, "interval": 10, "source": "cgroup",
                                                    "sample": {"cpu": 0.5, "mem": GIB, "mem_lim": 8 * GIB},
                                                    "warnings": [], "spark": [[1, 0.5, GIB, 0, 0]]})
    m.write_json(root / "live" / "mcp-git.json", {"updated": now, "interval": 10, "sample": {"cpu": 1.5, "mem": 1},
                                                   "warnings": [{"kind": "cpu", "text": "held", "advice": "raise",
                                                                 "argv": ["limits", "mcp-git", "--cpus", "2"]}]})
    m.write_json(root / "live" / "host.json", {"updated": now, "interval": 10,
                                                "sample": {"cpu_pct": 20.0, "mem_total": 16 * GIB}})
    records = [{"run": "claude-a", "name": "web", "agent": "claude", "workspace": "/p/web", "mcp_servers": ["git"]},
               {"run": "mcp-git", "name": "git", "kind": "mcp", "mcp_name": "git", "workspace": "/srv"}]
    rows, host = m.top_rows(records, root=root, measure=False)
    by = {row["run"]: row for row in rows}
    assert by["mcp-git"]["parent"] == "web" and by["mcp-git"]["project"] == "/p/web"
    assert by["claude-a"]["spark"] == [[1, 0.5, GIB, 0, 0]] and host["sample"]["cpu_pct"] == 20.0
    text = m.top_table(sorted(rows, key=lambda r: -(r["sample"].get("cpu") or 0)), host)
    assert text.splitlines()[0].startswith("host: CPU 20 %") and text.splitlines()[2].startswith("git <web")
    assert "mcp-git: raise" not in text and "git: raise" in text


def test_top_rows_say_each_runs_own_interval(tmp_path):
    """Review 24h B: monitor_interval (defaults, SANDBOX_AI_MONITOR_INTERVAL) changes how often a run is sampled;
    the app's "sampled every N s" said 10 whatever it was. Each row carries its recorder's interval."""
    root = tmp_path / "monitor"
    m.write_json(root / "live" / "claude-a.json", {"updated": time.time(), "interval": 30, "sample": {"cpu": 0.5}})
    rows, _ = m.top_rows([{"run": "claude-a", "name": "web"}, {"run": "codex-b", "name": "api"}], root=root,
                         measure=False)
    assert [row.get("interval") for row in rows] == [30, None]


def test_interval_setting():
    assert m.interval_setting({}, {}) == 10.0
    assert m.interval_setting({"SANDBOX_AI_MONITOR_INTERVAL": "2"}, {"monitor_interval": 30}) == 2.0
    assert m.interval_setting({}, {"monitor_interval": 30}) == 30.0
    assert m.interval_setting({}, {"monitor_interval": 0}) == 0.0  # recording off
    assert m.interval_setting({"SANDBOX_AI_MONITOR_INTERVAL": "0.01"}, {"monitor_interval": "x"}) == 10.0


def test_prune_keeps_runs_within_the_budget(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    now = time.time()
    for index in range(5):
        path = runs / f"monitor-20261117-10000{index}-r{index}.jsonl"
        path.write_bytes(b"x" * 1000)
        os.utime(path, (now - index * 3600, now - index * 3600))
    gone = runs / "monitor-20261001-000000-gone.jsonl"
    gone.write_text("x")
    os.utime(gone, (now - 40 * 86400, now - 40 * 86400))
    removed = m.prune(tmp_path, days=30, now=now, budget=2500)
    assert str(gone) in removed and sorted(p.name for p in runs.iterdir()) == [
        "monitor-20261117-100000-r0.jsonl", "monitor-20261117-100001-r1.jsonl"]


def test_main_knows_the_commands():
    spec = importlib.util.spec_from_file_location("sandbox_main_monitor_test", ROOT / "sandbox/main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    assert main.PYTHON_COMMANDS["top"] == ("monitor.py", "top")
    assert main.PYTHON_COMMANDS["stats"] == ("monitor.py", "stats")
    assert "\n  top " in main.help_text() and "\n  stats " in main.help_text()


def test_gc_compresses_an_ended_runs_series_and_log_days_removes_it(tmp_path):
    """The series follow the rules of a run's logs: compressed once the run ended (read as they are), removed by
    the age limit of the logs - never those of a run that runs."""
    spec = importlib.util.spec_from_file_location("sandbox_statedirs_monitor_test", ROOT / "sandbox/statedirs.py")
    statedirs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(statedirs)
    folder = tmp_path / "logs" / "web-1"
    ended = write_run(folder, "claude-old", {"workspace": "/p/web"}, [{"t": 100, "dt": 10, "cpu": 1.0}])
    minutes = write_run(folder, "claude-old", {}, [{"t": 60, "n": 6, "cpu": 1.0}], kind="monitor-1m")
    running = write_run(folder, "claude-new", {}, [{"t": 100, "dt": 10, "cpu": 1.0}])
    hour_ago = time.time() - 2 * 3600
    for path in (ended, minutes, running):
        os.utime(path, (hour_ago, hour_ago))
    assert statedirs.log_kind(ended.name) == "monitor" and statedirs.log_kind(minutes.name) == "monitor-1m"
    result = statedirs.compact(tmp_path / "logs", live_runs={"claude-new"})
    assert sorted(Path(item["path"]).name for item in result["compressed"]) == sorted([ended.name, minutes.name])
    assert running.exists() and not ended.exists()
    _, samples, _ = m.read_series(Path(str(ended) + ".gz"))
    assert samples == [{"t": 100, "dt": 10, "cpu": 1.0}]
    files = m.series_files([folder])
    assert set(files) == {"claude-old", "claude-new"} and files["claude-old"]["monitor"][0].name.endswith(".gz")
    old = time.time() - 40 * 86400
    for path in folder.iterdir():
        os.utime(path, (old, old))
    found = {Path(item["path"]).name for item in statedirs.old_logs(tmp_path / "logs", 30, {"claude-new"})}
    assert ended.name + ".gz" in found and running.name not in found


def test_a_window_parses_only_its_own_samples_and_the_finer_tiers_tail(tmp_path, monkeypatch):
    """Review 24h B: the app's System monitor asks `stats usage --series` every minute while shown (the web on every
    window change). A day of raw samples is 8640 lines a run: the 24 h window (minute tier) parsed every raw line
    too, only to take the minute begun, and the 1 h window parsed the whole day. Lines before the window (or before
    the coarser tier's end) are skipped without parsing; the result stays the same, events included."""
    series = m.Series(tmp_path, "claude-abc", {"run": "claude-abc", "name": "web"}, 10)
    start = 1_800_000_000 - 1_800_000_000 % 3600
    for i in range(2160):  # six hours of samples at 10 s
        series.write({"t": start + i * 10, "dt": 10.0, "cpu": 0.5, "mem": GIB})
        if i == 100:
            series.event({"event": "warning", "t": start + i * 10, "kind": "memory", "text": "early"})
    files = m.series_files([tmp_path])["claude-abc"]
    now = start + 2160 * 10
    expected = m.read_run(files, now - 6 * 3600, "monitor-1m")
    parsed = []
    real = m.json.loads
    monkeypatch.setattr(m.json, "loads", lambda text, *a, **k: parsed.append(1) or real(text, *a, **k))
    header, samples, events = m.read_run(files, now - 6 * 3600, "monitor-1m")
    assert (header, samples, events) == expected
    assert [e["text"] for e in events] == ["early"], "an event early in the window is kept"
    assert len(parsed) < 360 + 60 + 20, f"{len(parsed)} lines parsed: the raw tier's whole day again"
    parsed.clear()
    _, hour, _ = m.read_run(files, now - 3600, "monitor")
    assert len(hour) == 360 and len(parsed) < 360 + 20, len(parsed)


def test_the_runs_cgroup_is_podmans_own_never_one_made_below_it():
    """Backlog A (review A, 8): the first libpod-* met walking up counted as the run's: a cgroup named libpod-x made
    below the container's would have been measured alone. Now only Podman's own forms count - a container scope
    libpod-<hex>.scope under its pod's slice (the pod is measured) or one inside no other - innermost first, so a
    sandbox-ai inside a sandbox still measures its own pod."""
    pod_slice = Path("/sys/fs/cgroup/user.slice/user@1000.service/user.slice/machine-libpod_pod_0abc.slice")
    container = pod_slice / "libpod-0123.scope"
    assert m.sandbox_top(container / "container") == pod_slice
    assert m.sandbox_top(container / "container" / "libpod-x" / "p") == pod_slice, "not Podman's form"
    assert m.sandbox_top(container / "container" / "libpod-dead.scope" / "p") == pod_slice, "made below it"
    inner = container / "container" / "machine-libpod_pod_0bbb.slice"
    assert m.sandbox_top(inner / "libpod-0ccc.scope" / "container") == inner, "a sandbox-ai inside a sandbox"
    alone = Path("/sys/fs/cgroup/user.slice/libpod-0dd.scope")
    assert m.sandbox_top(alone / "container") == alone and m.sandbox_top(alone / "libpod-0ee.scope") == alone
    assert m.sandbox_top(Path("/sys/fs/cgroup/user.slice/libpod-conmon-0ff.scope")) is None
    scope = Path("/sys/fs/cgroup/user.slice/run-r1234.scope")
    assert m.sandbox_top(scope / "x") == scope


def test_tree_counters_count_the_cpu_of_children_that_ended(tmp_path):
    """Backlog A (review B): only the root's waited-for children counted; a child of the tree that waited for its
    own (a shell running short commands) took their CPU with it when they ended. Every process's cutime and cstime
    count now (each ended process is in exactly one living parent's, or the root's)."""
    proc = tmp_path / "proc"

    def process(pid, ppid, utime, stime, cutime, cstime):
        (proc / str(pid)).mkdir(parents=True)
        fields = ["S", str(ppid)] + ["0"] * 9 + [str(utime), str(stime), str(cutime), str(cstime)] + \
            ["0", "0", "1", "0", "0", "0", "10"] + ["0"] * 20
        (proc / str(pid) / "stat").write_text(f"{pid} (x) " + " ".join(fields) + "\n")
    process(10, 1, 100, 0, 50, 0)
    process(11, 10, 100, 0, 300, 0)  # a shell whose commands ended: 300 ticks of theirs
    found = m.tree_counters(10, proc=proc)
    tick = os.sysconf("SC_CLK_TCK")
    assert found["cpu_usec"] == (100 + 50 + 100 + 300) * 1_000_000 // tick


def test_recorder_stop_does_not_wait_for_a_slow_look(tmp_path):
    """Backlog A (review B): stop() joined the collector's thread for up to interval + 5 s - a look inside a slow
    `podman stats` (30 s) delayed the launcher's end. Now it waits STOP_WAIT at most; the look that comes back
    later writes nothing (the series is closed)."""
    import threading
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(30)
        return {"time": time.time(), "source": "cgroup", "mem": 1, "cpu_usec": 1}
    recorder = m.Recorder({"run": "slow-1", "name": "w"}, slow, tmp_path / "logs", interval=30,
                          root=tmp_path / "mon").start()
    assert entered.wait(10)
    begun = time.monotonic()
    recorder.stop()
    assert time.monotonic() - begun < m.STOP_WAIT + 1.5
    release.set()
    recorder.thread.join(10)
    assert not recorder.thread.is_alive() and recorder.latest is None, "nothing recorded after the stop"
    assert not (tmp_path / "mon" / "live" / "slow-1.json").exists()


@pytest.mark.parametrize("command", ["top", "stats"])
def test_abbreviated_options_are_resolved_here_before_the_host_is_asked(command, monkeypatch):
    """Backlog A (review A, 11): argparse took `top --hos HOST` for --host, but only --host spelled out was taken out
    of what went to the host: the host was asked `top --hos HOST` and looked for a host of that name among its own.
    Abbreviations are now resolved here (as argparse took them) before the host's argv is built."""
    asked = []
    monkeypatch.setattr(m, "on_host", lambda host, argv, out=print, err=None: asked.append((host, argv)) or 0)
    main = m.top_main if command == "top" else m.stats_main
    sub = [] if command == "top" else ["usage"]
    assert main([*sub, "--hos", "ct1", "--js"], out=lambda line: None) == 0
    assert asked[-1] == ("ct1", [command, *sub, "--json"])
    assert main([*sub, "--host=ct1", "--json"], out=lambda line: None) == 0
    assert asked[-1] == ("ct1", [command, *sub, "--json"])
    assert main([*sub, "--hos=ct1", "--proj", "/w"], out=lambda line: None) == 0
    assert asked[-1] == ("ct1", [command, *sub, "--project", "/w"])
    assert m.resolved_options(argparse.ArgumentParser(), ["--", "--hos"]) == ["--", "--hos"]


def test_the_idle_warning_suggests_what_fits_the_run():
    """Backlog A (review B): an MCP server idle for 30 minutes with a gigabyte was told "stop" - stopping it breaks
    the sandboxes that use it, and it is idle between calls by nature. Each case says its own: a kept session is
    stopped and kept (resume starts it again), a run that is not kept ends for good, an MCP server only with `mcp
    stop NAME` when nothing uses it (no one-click command), a helper ends with its session (none either)."""
    idle = [{"t": i * 60, "cpu": 0.005, "mem": 3 * GIB + GIB // 5} for i in range(31)]
    cases = {
        "session": (m.Assessor("claude-abc", "web", role="session"), ["stop", "web"],
                    "stop idle session web: 3.2 GiB for 30 min; it is kept: sandbox-ai resume web starts it again"),
        "run": (m.Assessor("claude-abc", "web", role="run"), ["stop", "web"],
                "stop idle run web: 3.2 GiB for 30 min; it is not kept: what runs in it ends for good"),
        "mcp": (m.Assessor("mcp-fetch-1", "fetch", role="mcp", owner="fetch"), None,
                "an MCP server waits for calls between them: if no running sandbox uses it any more, "
                "sandbox-ai mcp stop fetch (it ends it for every sandbox that uses it)"),
        "helper": (m.Assessor("codex-h1", "codex-h1", role="helper", owner="web"), None,
                   "a helper waits for its session's tasks between them: it ends with web (sandbox-ai stop web)"),
    }
    for role, (assessor, argv, advice) in cases.items():
        [warning] = feed(assessor, idle)[0]
        assert warning["argv"] == argv and warning["advice"] == advice, role
        assert warning["values"]["role"] == role
    assert m.idle_role({"kind": "mcp", "mcp_name": "fetch"}) == ("mcp", "fetch")
    assert m.idle_role({"kind": "agent", "helper_of": "web"}) == ("helper", "web")
    assert m.idle_role({"kind": "agent", "kept": True}) == ("session", None)
    assert m.idle_role({"kind": "agent"}) == ("run", None)


def test_the_host_counts_its_network_and_its_processes(tmp_path):
    """User 2026-11-20 (charts for CPU, memory, IO, network, processes - of the host too): the host's series has its
    network (its own namespace's /proc/net/dev, every interface but lo) and its processes and threads (the 4th field
    of /proc/loadavg)."""
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "stat").write_text("cpu  100 0 100 700 100 0 0 0 0 0\n")
    (proc / "loadavg").write_text("1.25 0.50 0.25 3/812 1234\n")
    dev = ("Inter-|   Receive |  Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes\n"
           "    lo: 99999 1 0 0 0 0 0 0 99999 1 0 0 0 0 0 0\n  eth0: {rx} 10 0 0 0 0 0 0 {tx} 5 0 0 0 0 0 0\n"
           "  wlan0: 100 1 0 0 0 0 0 0 50 1 0 0 0 0 0 0\n")
    (proc / "net" / "dev").write_text(dev.format(rx=1000, tx=500))
    before = m.host_counters(proc, clock=lambda: 0.0, sys_block=tmp_path / "none")
    assert before["rx_bytes"] == 1100 and before["tx_bytes"] == 550 and before["pids"] == 812
    (proc / "net" / "dev").write_text(dev.format(rx=21000, tx=10500))
    after = m.host_counters(proc, clock=lambda: 10.0, sys_block=tmp_path / "none")
    s = m.host_sample(before, after)
    assert s["rx"] == 2000 and s["tx"] == 1000 and s["pids"] == 812
    (proc / "net" / "dev").unlink()
    assert "rx_bytes" not in m.host_counters(proc, clock=lambda: 20.0, sys_block=tmp_path / "none")


# What a Mac prints (macOS 15: `netstat -ibn`, `sysctl -n ...`, `vm_stat`): one <Link#N> row per interface with its
# byte counters (an interface without an address has that column empty; a down one a * after its name), then a row
# per address with the same counters again - counted once.
MAC_NETSTAT = """\
Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                        982345     0  412345678   982345     0  412345678     0
lo0        16384 127           127.0.0.1         982345     -  412345678   982345     -  412345678     -
lo0        16384 ::1/128     ::1                 982345     -  412345678   982345     -  412345678     -
gif0*      1280  <Link#2>                             0     0          0        0     0          0     0
en0        1500  <Link#6>    a4:83:e7:12:34:56  5123456     0 {rx}  2345678     0 {tx}     0
en0        1500  fe80::1c2b: fe80:6::1c2b:3a4f  5123456     -  {rx}  2345678     -  {tx}     -
en0        1500  192.168.1     192.168.1.20      5123456     -  {rx}  2345678     -  {tx}     -
utun0      1380  <Link#14>                          120     0       9000      130     0       8000     0
bridge100  1500  <Link#20>   be:d0:74:aa:bb:64     4000     0     300000     5000     0     200000     0
"""


def mac_run(answers, calls):
    """subprocess.run as on a Mac: ANSWERS {command words: stdout}; a command not there fails as a missing tool."""
    def run(argv, **_kwargs):
        calls.append(tuple(argv))
        if tuple(argv) not in answers:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 0, answers[tuple(argv)], "")
    return run


def mac_answers(rx, tx, threads="2481\n"):
    return {("sysctl", "-n", "hw.memsize"): "34359738368\n",
            ("vm_stat",): "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100000.\n"
                          "Pages inactive: 50000.\nPages speculative: 10000.\n",
            ("sysctl", "-n", "vm.swapusage"): "total = 2048.00M  used = 512.00M  free = 1536.00M  (encrypted)\n",
            ("netstat", "-ibn"): MAC_NETSTAT.format(rx=rx, tx=tx),
            ("sysctl", "-n", "kern.num_threads"): threads}


def test_the_mac_host_counts_its_network_and_its_processes():
    """Follow-up of 0.1.174 (2026-11-20): on macOS the host's network and processes were "not recorded" (no /proc;
    Podman's VM is not this Mac). The Mac's counters have them now, from what macOS ships: `netstat -ibn` (each
    interface's <Link#> row once, every one but lo0, as /proc/net/dev but lo on Linux) and `sysctl -n
    kern.num_threads` (the threads of every process, as the 4th field of Linux's /proc/loadavg) - two short calls
    per look, nothing imported."""
    calls = []
    before = m.host_counters_mac(run=mac_run(mac_answers(1_000_000, 400_000), calls), clock=lambda: 0.0)
    assert before["rx_bytes"] == 1_000_000 + 9000 + 300000 and before["tx_bytes"] == 400_000 + 8000 + 200000
    assert before["pids"] == 2481
    assert before["mem_total"] == 32 * GIB and before["mem_avail"] == 16384 * 160000
    after = m.host_counters_mac(run=mac_run(mac_answers(1_200_000, 500_000, "2500\n"), calls), clock=lambda: 10.0)
    s = m.host_sample(before, after)
    assert s["rx"] == 20000 and s["tx"] == 10000 and s["pids"] == 2500
    assert ("netstat", "-ibn") in calls and ("sysctl", "-n", "kern.num_threads") in calls


def test_the_mac_host_without_netstat_or_the_thread_count_records_the_rest():
    """A Mac whose netstat or sysctl does not answer as expected: those counters are left out (the charts then say
    "not recorded"), never a made-up zero; memory and swap as before."""
    answers = mac_answers(1, 1, threads="sysctl: unknown oid 'kern.num_threads'\n")
    del answers[("netstat", "-ibn")]
    found = m.host_counters_mac(run=mac_run(answers, []), clock=lambda: 0.0)
    assert "rx_bytes" not in found and "tx_bytes" not in found and "pids" not in found
    assert found["mem_total"] == 32 * GIB and found["swap_used"] == 512 << 20
    # garbage instead of a table: nothing counted
    answers[("netstat", "-ibn")] = "netstat: sysctl: Operation not permitted\n"
    assert "rx_bytes" not in m.host_counters_mac(run=mac_run(answers, []), clock=lambda: 0.0)
    assert m.mac_net_bytes("Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\n") is None


def test_the_series_detail_has_every_metric_bounded_with_its_gaps(tmp_path, monkeypatch):
    """The app's and the web's charts (user 2026-11-20): `stats usage --series` gives each run and the host a
    "detail" besides the series - per time slot (aligned to the window, the same for every run: they add up) its
    CPUs and limit, memory at its highest and its limit, read and written, received and sent, processes and their
    limit; the host's CPU busy, memory free of its total, swap, disk, network, processes. At most SERIES_POINTS slots;
    a slot without a sample is left out (a gap the charts show); a minute's or an hour's record gives its peaks."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    now = 1_800_000_000
    logs = tmp_path / "state" / "sandbox-ai" / "logs"
    samples = [{"t": now - 3600 + i * 10, "dt": 10, "cpu": 1.0 + (i % 2), "cpu_pct": 50.0, "cpu_lim": 4,
                "mem": GIB * (1 + i % 3), "mem_lim": 8 * GIB, "rd": 100, "wr": 200, "rx": 10, "tx": 20,
                "pids": 40 + i % 5, "pids_lim": 4096} for i in range(360) if not 100 <= i < 160]  # ten minutes off
    write_run(logs / "web-1", "claude-aaa", {"name": "web", "workspace": "/p/web"}, samples)
    host_root = tmp_path / "state" / "sandbox-ai" / "monitor"
    write_run(host_root, "host", {"kind": "host"},
              [{"t": now - 3600 + i * 10, "dt": 10, "cpu_pct": 20.0, "mem_total": 32 * GIB, "mem_avail": 20 * GIB,
                "swap_total": 4 * GIB, "swap_used": GIB, "rd": 5, "wr": 6, "rx": 7, "tx": 8, "pids": 800,
                "load1": 1.5, "cpus": 8} for i in range(360)], stamp=m.HOST_STAMP)
    data = m.usage("1h", "run", now=now, series=True)
    [run] = data["runs"]
    detail = run["detail"]
    assert set(detail) == {"t", *m.DETAIL_KEYS}
    count = len(detail["t"])
    assert 0 < count <= m.SERIES_POINTS and all(len(values) == count for values in detail.values())
    assert detail["t"] == sorted(detail["t"])
    steps = [b - a for a, b in zip(detail["t"], detail["t"][1:])]
    assert max(steps) >= 600, "the ten minutes without a sample are a gap"
    assert detail["mem_lim"][0] == 8 * GIB and detail["pids_lim"][0] == 4096 and detail["cpu_lim"][0] == 4
    assert max(detail["mem"]) == 3 * GIB, "memory at its highest in a slot"
    assert 1.0 <= detail["cpu"][0] <= 2.0 and detail["rd"][0] == 100 and detail["tx"][0] == 20
    host = data["host"]["detail"]
    assert set(host) == {"t", *m.HOST_DETAIL_KEYS}
    assert host["mem_avail"][0] == 20 * GIB and host["rx"][0] == 7 and host["pids"][0] == 800
    assert data["runs"][0]["series"] and data["host"]["series"], "the series stay as they were"
    # A window's slots are aligned: two runs' slots are the same times.
    write_run(logs / "api-2", "codex-bbb", {"name": "api", "workspace": "/p/api"},
              [{"t": now - 3600 + i * 10 + 3, "dt": 10, "cpu": 0.5, "mem": GIB} for i in range(360)])
    both = {row["run"]: row["detail"]["t"] for row in m.usage("1h", "run", now=now, series=True)["runs"]}
    assert set(both["claude-aaa"]) <= set(both["codex-bbb"])
    # A minute tier's record: its peak (mem_max) is what the chart shows.
    rolled = m.detail([{"t": now - 60, "dt": 60, "mem": GIB, "mem_max": 2 * GIB, "pids": 5, "pids_max": 9}],
                      m.DETAIL_KEYS, now - 86400, now)
    assert rolled["mem"] == [2 * GIB] and rolled["pids"] == [9] and rolled["cpu"] == [None]
    assert m.detail([], m.DETAIL_KEYS, now - 60, now) == {"t": [], **{key: [] for key in m.DETAIL_KEYS}}
