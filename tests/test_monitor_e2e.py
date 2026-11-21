"""End to end: the real ./sandbox-ai starts a run (Podman faked: its sandbox runs a few seconds, `podman stats` gives
growing counters), its launcher's collector records it; `sandbox-ai top` shows it while it runs, `sandbox-ai stats
usage` afterwards, with its project, and a memory warning lands in its safeguard records and its activity."""
import json
from pathlib import Path
import time

from live_fakes import Host

GIB = 1 << 30


def test_a_run_is_recorded_shown_live_and_summed_up(tmp_path):
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    stats = tmp_path / "stats.json"
    # 0.9 CPUs a second (at three seconds between calls), 7.6 of 8 GiB: a memory warning.
    stats.write_text(json.dumps({"CPUNano": 2_700_000_000, "MemUsage": int(7.6 * GIB), "MemLimit": 8 * GIB,
                                 "NetInput": 3000, "NetOutput": 1500, "BlockInput": 0, "BlockOutput": 6000,
                                 "PIDs": 12}))
    env = {"FAKE_PODMAN_RUN": "0:9", "FAKE_PODMAN_STATS": str(stats), "SANDBOX_AI_MONITOR_INTERVAL": "1"}
    process = host.popen("claude", "--no-tmux", env=env)
    try:
        live = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and process.poll() is None:
            shown = host.run("top", "--json", "--no-measure", env=env)
            assert shown.returncode == 0, shown.stderr
            data = json.loads(shown.stdout)
            live = next((row for row in data["runs"] if row["sample"].get("cpu") is not None), None)
            if live is not None and live["warnings"]:
                break
            time.sleep(0.5)
        assert live is not None, "top never showed the run's sample"
        assert live["source"] == "podman" and live["agent"] == "claude"
        assert live["project"] == str(host.workspace) and live["sample"]["mem_lim"] == 8 * GIB
        assert [w["kind"] for w in live["warnings"]] == ["memory"]
        assert live["warnings"][0]["argv"][:2] == ["limits", live["run"]]
        table = host.run("top", "--no-measure", env=env)
        assert "RUN" in table.stdout and live["name"][:24] in table.stdout and "raise --memory to 12g" in table.stdout
    finally:
        out, err = process.communicate(timeout=120)
    assert process.returncode == 0, err
    done = host.run("stats", "usage", "--since", "1h", "--json", "--series")
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    [run] = data["runs"]
    assert run["agent"] == "claude" and run["project"] == str(host.workspace)
    assert 0.8 <= run["cpu_avg"] <= 1.0 and run["mem_max"] == int(7.6 * GIB) and run["series"]
    assert run["written"] > 0 and run["received"] > 0
    assert [w["kind"] for w in run["warnings"]] == ["memory"]
    [group] = data["groups"]
    assert group["key"] == str(host.workspace) and group["warnings"] == 1
    assert data["collector"]["percent_of_a_cpu"] is None or data["collector"]["percent_of_a_cpu"] < 0.5
    # Its files: in the project's log folder, the three tiers; the live record gone with the run.
    logs = Path(host.home) / ".local/state/sandbox-ai/logs"
    names = sorted(path.name.split("-2")[0] for path in logs.glob("*/monitor*.jsonl"))
    assert names == ["monitor", "monitor-1h", "monitor-1m"]
    assert not list((Path(host.home) / ".local/state/sandbox-ai/monitor/live").glob("claude-*.json"))
    # The warning in the run's safeguard records and its activity.
    safeguards = [json.loads(line) for line in next(logs.glob("*/safeguards.jsonl")).read_text().splitlines()]
    assert any(entry["kind"] == "resources" and "raise --memory to 12g" in entry["fix"] for entry in safeguards)
    activity = [json.loads(line) for line in next(logs.glob("*/activity.jsonl")).read_text().splitlines()]
    assert any(entry["kind"] == "resources" and entry["details"]["warning"] == "memory" for entry in activity)
    text = host.run("stats", "usage", "--since", "1h")
    assert text.returncode == 0 and str(host.workspace) in text.stdout and "warnings:" in text.stdout
