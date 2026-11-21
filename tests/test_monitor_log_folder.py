"""The system monitor's series lie in the run's log folder, which the sandbox's supervisor can write to (it is mounted
into the sandbox, read-write: the logs come from there). The collector runs in the launcher on the host, as the human:
whatever lies in that folder is data to it - it never reads or writes through a link there, and a FIFO in a series'
place never holds it up (24h review A, 2026-11-17: Series.trim read the series' path and wrote its temporary file
through links, Series.append counted the lines of whatever the path led to and waited on a FIFO for ever - with the
series' lock held, so the launcher's end waited with it)."""
import importlib.util
import json
import os
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_monitor_log_folder_test", ROOT / "sandbox/monitor.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

TIERS = (("monitor", None, 100),)  # 10 samples kept at 10 s: trimmed past 11


def series(folder):
    return m.Series(folder, "claude-abc", {"run": "claude-abc"}, 10, stamp="20261117-100000", tiers=TIERS)


def fill(made, count, start=0):
    for index in range(start, start + count):
        made.write({"t": 1000 + 10 * index, "dt": 10.0, "cpu": 0.5})


def test_a_trim_never_writes_through_a_link_in_the_temporary_files_place(tmp_path):
    logs, outside = tmp_path / "logs", tmp_path / "outside"
    logs.mkdir()
    outside.write_text("the human's file\n")
    made = series(logs)
    path = made.files["monitor"]
    # What the sandbox's side put there: the name the trim writes its new file under, as a link to a file of the human.
    (logs / f".{path.name}.{os.getpid()}").symlink_to(outside)
    fill(made, 13)
    assert outside.read_text() == "the human's file\n"
    lines = path.read_text().splitlines()
    assert not path.is_symlink() and json.loads(lines[0])["header"] == 1
    assert len(lines) - 1 <= made.keep["monitor"] + 2  # trimmed all the same (the newest stay)
    assert json.loads(lines[-1])["t"] == 1000 + 10 * 12


def test_a_trim_never_reads_a_file_the_series_path_was_made_a_link_to(tmp_path):
    logs, secret = tmp_path / "logs", tmp_path / "secret"
    logs.mkdir()
    secret.write_text("".join(f"private line {index}\n" for index in range(50)))
    made = series(logs)
    path = made.files["monitor"]
    fill(made, 5)
    path.unlink()
    path.symlink_to(secret)  # (after the collector opened it last, before it trims)
    made.lines["monitor"] = 100
    made.trim("monitor")
    assert secret.read_text().startswith("private line 0\n") and len(secret.read_text().splitlines()) == 50
    for entry in logs.iterdir():
        if not entry.is_symlink():
            assert "private line" not in entry.read_text(), entry.name
    assert path.is_symlink()  # (left as it was found: nothing of the human's was copied beside it)


def test_the_lines_of_a_link_are_not_counted_and_nothing_is_written_through_it(tmp_path):
    logs, outside = tmp_path / "logs", tmp_path / "outside"
    logs.mkdir()
    outside.write_text("x\n" * 500)
    made = series(logs)
    made.files["monitor"].symlink_to(outside)
    fill(made, 3)
    assert outside.read_text() == "x\n" * 500
    assert [entry.name for entry in logs.iterdir()] == [made.files["monitor"].name]


def test_a_fifo_in_a_series_place_does_not_hold_the_collector_up(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    made = series(logs)
    os.mkfifo(made.files["monitor"])
    done = threading.Event()

    def work():
        fill(made, 2)
        made.close()
        done.set()
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        assert done.wait(5), "the collector waits on a FIFO in its series' place (with the series' lock held)"
    finally:
        if not done.is_set():  # let the writer go, so the test's thread ends
            fd = os.open(made.files["monitor"], os.O_RDWR | os.O_NONBLOCK)
            done.wait(2)
            os.close(fd)


def test_a_live_record_is_never_written_through_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("the human's file\n")
    live = tmp_path / "live" / "claude-abc.json"
    live.parent.mkdir()
    (live.parent / f".{live.name}.{os.getpid()}.{threading.get_ident()}").symlink_to(outside)
    m.write_json(live, {"run": "claude-abc"})
    assert outside.read_text() == "the human's file\n"
    assert json.loads(live.read_text()) == {"run": "claude-abc"}


# --- its readers: a series is data, whatever its lines hold ---------------------------------------------------------

import time  # noqa: E402

import pytest  # noqa: E402


def write_series(folder, run, header, *samples):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"monitor-20261117-100000-{run}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in ({"header": 1, "run": run, "interval": 10, **header}, *samples)))


@pytest.mark.parametrize("header, sample", [
    ({"agent": 5}, {}),                      # a number where a text belongs: groups were sorted by it (str < int)
    ({"name": 7, "workspace": 3, "helper_of": 1, "mcp_name": 2, "kind": 9, "started": 4}, {}),
    ({"mcp_servers": 5}, {}),                # "m" in 5
    ({"mcp_servers": 1.5, "kind": "agent"}, {}),
    ({}, {"dt": 1e300}),                     # seconds without end: int(inf)
    ({}, {"dt": -1e300, "cpu": 1e300, "mem": 1e300, "rd": 1e300}),
    ({"interval": 1e300}, {"n": 1e300}),
])
def test_stats_usage_reads_a_series_with_anything_in_it(tmp_path, header, sample):
    """`sandbox-ai stats usage` (and the app's and the web's System monitor) read every project's series: one line of
    another shape in one of them ended the whole command with a traceback (TypeError, OverflowError)."""
    now = time.time()
    logs = tmp_path / "logs" / "project"
    write_series(logs, "r1", {"name": "n1", "agent": "claude", "workspace": "/w", "kind": "agent", **header},
                 {"t": int(now) - 100, "dt": 10.0, "cpu": 0.5, "mem": 2 << 30, **sample})
    write_series(logs, "r2", {"name": "n2", "agent": "codex", "workspace": "/w2", "kind": "mcp", "mcp_name": "m"},
                 {"t": int(now) - 100, "dt": 10.0})
    for by in ("project", "run", "agent"):
        data = m.usage("1h", by, series=True, now=now, folders=[logs], host_root=tmp_path / "none")
        json.dumps(data)
        assert m.usage_text(data)
        assert {row["run"] for row in data["runs"]} == {"r1", "r2"}
        for row in data["runs"]:
            assert all(row[key] is None or isinstance(row[key], str) for key in ("name", "agent", "workspace",
                                                                                  "mcp_name", "helper_of"))
            assert 0 <= row["seconds"] < 1e12 and 0 <= row["cpu_seconds"] < 1e15


def test_a_record_keeps_only_what_its_keys_may_hold():
    cleaned = m.clean_record({"t": 5, "cpu": 0.5, "agent": 5, "name": "n", "mcp_servers": 7, "mem": 1e300,
                              "dt": -3, "n": 0, "rd": -1e300, "text": ["x"], "oom": True})
    assert cleaned == {"t": 5, "cpu": 0.5, "name": "n"}
