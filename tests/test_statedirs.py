"""sandbox-ai gc --usage and the compaction of the logs (statedirs.py): sizes measured without reading a file, never
through a link, never crashing on what cannot be read; ended runs' logs compressed, shared logs split, nothing of a
running start touched. Only temporary trees: never the real state directory."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_statedirs_test", ROOT / "sandbox/statedirs.py")
sd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sd)


@pytest.fixture
def env(tmp_path, monkeypatch):
    values = {"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_DATA_HOME": str(tmp_path / "data"),
              "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
              "XDG_RUNTIME_DIR": str(tmp_path / "runtime")}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "runtime").mkdir()
    return values


def write(path, size=0, text=None, age=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text)
    else:
        path.write_bytes(b"x" * size)
    if age is not None:
        when = time.time() - age
        os.utime(path, (when, when))
    return path


def jsonl(count, run="r1"):
    return "".join(json.dumps({"time": f"2026-11-01T10:00:{i % 60:02d}Z", "run": run, "host": "example.org",
                               "n": i}) + "\n" for i in range(count))


def make_tree(state):
    """A small state directory: a kept start with caches and scratch work, two log directories, an MCP server."""
    home = state / "sessions/claude-big-1234/home"
    write(home / ".cache/pip/wheel", 300_000)
    write(home / ".cache/uv/x", 100_000)
    write(home / ".npm/_cacache/y", 50_000)
    write(home / "scratch/run1/data.bin", 600_000)
    write(home / ".claude/projects/p/t.jsonl", 200_000)
    write(state / "sessions/claude-big-1234/session.json", text="{}")
    write(state / "sessions/claude-big-1234/supervisor.log", 10_000)
    write(state / "sessions/small-otter-moon/home/.bashrc", 100)
    write(state / "sessions/small-otter-moon/session.json", text="{}")
    logs = state / "logs/proj-0123456789ab"
    write(logs / "connections-20261101-100000-run-a.jsonl", text=jsonl(2000, "run-a"), age=7200)
    write(logs / "llm-traffic-20261101-100000-run-a.jsonl", text=jsonl(1000, "run-a"), age=7200)
    write(logs / "connections-20261102-100000-run-live.jsonl", text=jsonl(10, "run-live"), age=7200)
    write(logs / "activity.jsonl", text=jsonl(100))
    write(state / "logs/other-ba9876543210/files.jsonl", 20_000)
    write(state / "mcp/fetch/supervisor.log", 30_000)
    write(state / "history/claude-big-1234.jsonl", text="{}\n")
    write(state / "launcher.log", 2000)
    write(state / "launcher.log.1", 2000)
    write(state / "exposed.json", text="{}")
    return home


def test_usage_parts_and_biggest(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    make_tree(state)
    write(tmp_path / "data/sandbox-ai/mcp-rootfs/a/b", 70_000)
    starts = {"claude-big-1234": {"name": "claude-big", "state": "stopped", "keep": True,
                                  "last_used": "2026-10-01T10:00:00Z"}}
    found = sd.usage(starts, live_runs={"run-live"}, podman=False)
    parts = found["parts"]
    big = parts["starts"]["items"][0]
    assert big["id"] == "claude-big-1234" and big["name"] == "claude-big" and big["state"] == "stopped"
    assert big["home"]["bytes"] >= 1_250_000 and big["bytes"] >= big["home"]["bytes"] + 10_000
    assert [item["path"] for item in big["home"]["biggest"]][:2] == ["scratch", ".cache"]
    assert big["home"]["biggest"][1]["biggest"][0]["path"] == ".cache/pip"
    assert set(big["home"]["caches"]) == {".cache/pip", ".cache/uv", ".npm/_cacache"}
    assert parts["starts"]["count"] == 2
    logs = parts["logs"]
    assert logs["count"] == 2 and logs["items"][0]["name"] == "proj-0123456789ab"
    first = logs["items"][0]
    assert first["runs"] == 2 and "connections" in first["kinds"] and "llm-traffic" in first["kinds"]
    # the live run's log is not compressible; the ended run's two are
    ended = sum(sd.Scan.used(os.stat(p)) for p in Path(first["path"]).glob("*-run-a.jsonl"))
    assert first["compressible"] == ended
    assert parts["mcp"]["items"][0]["name"] == "fetch"
    assert parts["history"]["count"] == 1
    assert {i["name"] for i in parts["launcher_log"]["items"]} == {"launcher.log", "launcher.log.1"}
    assert [i["name"] for i in parts["state_other"]["items"]] == ["exposed.json"]
    assert parts["data"]["items"][0]["name"] == "mcp-rootfs"
    assert found["total"] == sum(part["bytes"] for part in parts.values())
    assert found["scan"]["partial"] is False and found["podman"] is None


def test_usage_never_follows_links_and_survives_unreadable(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    outside = tmp_path / "outside"
    write(outside / "huge", 5_000_000)
    home = state / "sessions/linky-one/home"
    home.mkdir(parents=True)
    (home / "link").symlink_to(outside)
    (state / "logs").mkdir(parents=True)
    (state / "logs/elsewhere").symlink_to(outside)
    locked = home / "locked"
    write(locked / "secret", 1000)
    locked.chmod(0)
    try:
        found = sd.usage({}, podman=False)
    finally:
        locked.chmod(0o700)
    assert found["parts"]["starts"]["items"][0]["bytes"] < 1_000_000
    assert found["parts"]["logs"]["count"] == 0
    if os.geteuid() != 0:
        assert found["scan"]["unreadable"] >= 1


def test_usage_budget_marks_partial(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    for index in range(30):
        write(state / f"sessions/s{index}/home/f", 10)
    found = sd.usage({}, podman=False, scan=sd.Scan(entries=5))
    assert found["scan"]["partial"] is True


def test_usage_empty_and_damaged(env, tmp_path):
    found = sd.usage(None, podman=False)  # nothing there at all
    assert found["total"] == 0 and found["parts"]["starts"]["count"] == 0
    state = tmp_path / "state/sandbox-ai"
    write(state / "sessions", text="not a folder")
    write(state / "logs/p-1/connections-garbage.jsonl", text="\x00\x01")
    assert sd.usage({}, podman=False)["parts"]["logs"]["items"][0]["kinds"]["other"]["files"] == 1


def test_human():
    assert [sd.human(v) for v in (0, 1023, 1024, 5 << 20, 3 << 30, 250 << 30)] == \
        ["0 B", "1023 B", "1.0 KiB", "5.0 MiB", "3.0 GiB", "250 GiB"]


def test_podman_usage(monkeypatch):
    monkeypatch.setattr(sd.shutil, "which", lambda name: "/usr/bin/podman")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["system", "df"]:
            out = json.dumps([{"Type": "Images", "Total": 9, "Active": 3, "RawSize": 9 << 30,
                               "RawReclaimable": 4 << 30, "Size": "9GB", "Reclaimable": "4GB (44%)"},
                              {"Type": "Local Volumes", "Total": 2, "Active": 1, "RawSize": 1 << 20,
                               "RawReclaimable": 0}])
        else:
            out = json.dumps([{"Id": "a" * 64, "Size": 2 << 30, "Labels": {"sandbox-ai.agent-version": "1"}},
                              {"Id": "b" * 64, "Size": 1 << 30, "Labels": {"other": "x"}},
                              {"Id": "c" * 64, "Size": 1 << 30, "Labels": None}])
        return subprocess.CompletedProcess(command, 0, out, "")
    found = sd.podman_usage(run)
    assert found["images"] == {"total": 9, "active": 3, "bytes": 9 << 30, "reclaimable": 4 << 30}
    assert found["volumes"]["bytes"] == 1 << 20
    assert found["dangling"]["total"] == 1 and found["dangling"]["images"][0]["id"] == "a" * 12
    # no answer, or no Podman: None, never a crash
    assert sd.podman_usage(lambda *a, **k: subprocess.CompletedProcess(a, 125, "", "boom")) is None
    monkeypatch.setattr(sd.shutil, "which", lambda name: None)
    assert sd.podman_usage(run) is None


def run_cli(env, *args):
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], capture_output=True, text=True,
                          env={**os.environ, **env, "PATH": "/usr/bin:/bin"}, timeout=120)


def test_gc_usage_end_to_end(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    make_tree(state)
    result = run_cli(env, "gc", "--usage", "--json")
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout)
    assert found["parts"]["logs"]["count"] == 2 and found["parts"]["starts"]["count"] == 2
    assert "would_free" in found and found["would_free"]["compress"]["bytes"] > 0
    text = run_cli(env, "gc", "--usage")
    assert text.returncode == 0, text.stderr
    assert "Starts" in text.stdout and "proj-0123456789ab" in text.stdout and "sandbox-ai gc" in text.stdout
    # --usage measures only: nothing was removed or compressed
    assert (state / "logs/proj-0123456789ab/connections-20261101-100000-run-a.jsonl").exists()
    refused = run_cli(env, "gc", "--usage", "--dry-run")
    assert refused.returncode == 1 and "--usage" in refused.stderr


def summary_rows(lines):
    """The rows of the Summary table of gc --usage's text: [(category, size text, line)]."""
    start = next(i for i, line in enumerate(lines) if line.split()[:2] == ["CATEGORY", "SIZE"])
    rows = []
    for line in lines[start + 1:]:
        if not line.strip() or set(line.strip()) == {"-"}:
            break
        match = re.match(r"\s+(\S.*?)\s{2,}([\d.]+ (?:[KMGT]iB|B))\s", line)
        if match:  # (else the wrapped rest of the row before)
            rows.append((match.group(1), match.group(2), line))
    return rows


def test_gc_usage_is_a_summary_and_tables_end_to_end(env, tmp_path):
    """gc --usage: a summary table first (category | size | share | reclaimable | how), sorted by size with a
    total; then aligned tables; what gc would free with the command per row and a total; colour only on request."""
    state = tmp_path / "state/sandbox-ai"
    make_tree(state)
    write(state / "sessions/claude-big-1234/session.json", text=json.dumps(
        {"agent": "claude", "options": [], "backend": "bwrap", "keep": True, "created": "2026-10-01T10:00:00Z",
         "workspace": "/srv/app", "cwd": "/", "name": "claude-big"}))
    found = json.loads(run_cli(env, "gc", "--usage", "--json").stdout)
    summary = found["summary"]
    sizes = [row["bytes"] for row in summary["rows"]]
    assert sizes == sorted(sizes, reverse=True) and summary["total"] == sum(sizes)
    assert abs(sum(row["share"] for row in summary["rows"]) - 100) < 0.5
    assert summary["reclaimable"] == sum(row["reclaimable"] for row in summary["rows"])
    assert summary["reclaimable"] == found["would_free"]["total"]["bytes"] > 0
    assert found["would_free"]["total"]["command"] == "sandbox-ai gc --caches --images"
    starts = {row["key"]: row for row in summary["rows"]}["starts"]
    assert starts["reclaimable"] >= 450_000  # the caches of the stopped kept start
    big = found["parts"]["starts"]["items"][0]
    assert big["expires"] and big["expires_why"] == "default"
    text = run_cli(env, "gc", "--usage")
    assert text.returncode == 0, text.stderr
    lines = text.stdout.splitlines()
    assert "\033" not in text.stdout  # not a terminal: no colour
    assert lines[0].startswith("What sandbox-ai keeps here:")
    head = next(line for line in lines if line.split()[:2] == ["CATEGORY", "SIZE"])
    assert head.split() == ["CATEGORY", "SIZE", "SHARE", "RECLAIMABLE", "HOW"]
    rows = summary_rows(lines)
    assert [row[0] for row in rows][:1] == ["Starts (kept HOMEs)"] and len(rows) == len(summary["rows"])
    total = next(line for line in lines if line.lstrip().startswith("Total"))
    assert "100%" in total
    assert any(line.split()[:6] == ["NAME", "STATE", "SIZE", "CACHES", "LAST", "USED"] for line in lines)
    assert any(line.split()[:4] == ["FOLDER", "SIZE", "RUNS", "LAST"] for line in lines)
    free = lines.index(next(line for line in lines if line.startswith("What gc would free")))
    assert lines[free + 1].split()[:3] == ["SIZE", "COMMAND", "WHAT"]
    assert any("sandbox-ai gc --caches " in line and "download caches" in line for line in lines[free:])
    assert any("Total" in line and "sandbox-ai gc --caches --images" in line
               for line in lines[free:])
    # sizes right-aligned: the summary's sizes end in one column
    ends = {line.index(size) + len(size) for _, size, line in rows}
    assert len(ends) == 1 and head.index("SIZE") + 4 in ends
    coloured = run_cli(env, "gc", "--usage", "--color", "always")
    assert coloured.returncode == 0 and "\033[" in coloured.stdout
    plain = subprocess.run([str(ROOT / "sandbox-ai"), "gc", "--usage", "--no-color"], capture_output=True, text=True,
                           env={**os.environ, **env, "PATH": "/usr/bin:/bin", "FORCE_COLOR": "1"}, timeout=120)
    assert plain.returncode == 0 and "\033" not in plain.stdout


# -- compaction ---------------------------------------------------------------------------------------------------

def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_statedirs_test", ROOT / f"sandbox/{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_compress_roundtrip_and_left_as_it_was(tmp_path):
    log = write(tmp_path / "connections-20261101-100000-a.jsonl", text=jsonl(3000), age=7200)
    log.chmod(0o600)
    before = log.stat()
    data = log.read_bytes()
    saved = sd.compress(log)
    packed = tmp_path / (log.name + ".gz")
    assert not log.exists() and gzip.decompress(packed.read_bytes()) == data
    assert saved > 0 and packed.stat().st_mtime == before.st_mtime and packed.stat().st_mode & 0o777 == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no temporary file left
    # a link is never followed: refused, nothing changed
    target = write(tmp_path / "elsewhere.jsonl", text="{}\n")
    (tmp_path / "connections-20261101-100000-b.jsonl").symlink_to(target)
    with pytest.raises(OSError):
        sd.compress(tmp_path / "connections-20261101-100000-b.jsonl")
    assert target.read_text() == "{}\n" and not (tmp_path / "connections-20261101-100000-b.jsonl.gz").exists()


def test_compress_failure_keeps_the_log(tmp_path, monkeypatch):
    log = write(tmp_path / "connections-20261101-100000-a.jsonl", text=jsonl(100), age=7200)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(sd.os, "fsync", full)
    with pytest.raises(OSError):
        sd.compress(log)
    assert log.exists() and not (tmp_path / (log.name + ".gz")).exists()
    assert [p.name for p in tmp_path.iterdir()] == [log.name]


def test_split_keeps_the_newest_lines(tmp_path):
    lines = [json.dumps({"n": i, "pad": "p" * 50}) + "\n" for i in range(5000)]
    log = write(tmp_path / "files.jsonl", text="".join(lines), age=7200)
    size = log.stat().st_size
    saved = sd.split(log, keep=20_000, when=1_700_000_000)
    kept = log.read_text()
    archive = tmp_path / "files.jsonl.20231114-221320.gz"
    older = gzip.decompress(archive.read_bytes()).decode()
    assert older + kept == "".join(lines)  # nothing lost, nothing twice
    assert kept.startswith("{") and 20_000 <= len(kept) < 20_000 + 100 and saved > 0 and log.stat().st_size < size
    # a second split adds another archive; the oldest go when they are over the limit
    write(log, text=kept + "".join(lines))
    sd.split(log, keep=20_000, when=1_700_100_000)
    archives = sorted(p.name for p in tmp_path.glob("files.jsonl.*.gz"))
    assert len(archives) == 2
    newer = (tmp_path / archives[1]).stat().st_size
    assert sd.drop_archives(tmp_path, "files.jsonl", limit=newer) == [archives[0]]
    assert sd.drop_archives(tmp_path, "files.jsonl", limit=newer) == []
    # small enough: nothing to do
    assert sd.split(write(tmp_path / "dns.jsonl", text="{}\n"), keep=100) == 0


def test_compact(tmp_path, monkeypatch):
    root = tmp_path / "logs"
    proj = root / "proj-1"
    ended = write(proj / "connections-20261101-100000-run-a.jsonl", text=jsonl(500, "run-a"), age=7200)
    traffic = write(proj / "llm-traffic-20261101-100000-run-a.jsonl.1", text=jsonl(500, "run-a"), age=7200)
    live = write(proj / "connections-20261102-100000-run-live.jsonl", text=jsonl(10), age=7200)
    recent = write(proj / "connections-20261103-100000-run-b.jsonl", text=jsonl(10))
    monkeypatch.setattr(sd, "SHARED_LIMIT", 10_000)
    monkeypatch.setattr(sd, "SHARED_KEEP", 2_000)
    shared = write(proj / "files.jsonl", text=jsonl(1000), age=7200)
    busy_shared = write(root / "busy/dns.jsonl", text=jsonl(1000), age=7200)
    write(root / "busy/connections-20261101-100000-run-c.jsonl", text=jsonl(10), age=7200)
    (root / "linked").symlink_to(proj)

    dry = sd.compact(root, {"run-live"}, [root / "busy"], dry_run=True)
    assert sorted(Path(i["path"]).name for i in dry["compressed"]) == [
        "connections-20261101-100000-run-a.jsonl", "connections-20261101-100000-run-c.jsonl",
        "llm-traffic-20261101-100000-run-a.jsonl.1"]
    assert [Path(i["path"]).name for i in dry["split"]] == ["files.jsonl"]
    assert ended.exists() and shared.stat().st_size > 10_000  # a dry run changes nothing

    capped = []
    done = sd.compact(root, {"run-live"}, [root / "busy"], caps=lambda folder: capped.append(folder.name) or [])
    assert not ended.exists() and (proj / (ended.name + ".gz")).exists() and (proj / (traffic.name + ".gz")).exists()
    assert live.exists() and recent.exists() and busy_shared.stat().st_size > 10_000
    assert shared.stat().st_size <= 2_100 and list(proj.glob("files.jsonl.*.gz"))
    assert done["saved"] > 0 and not done["errors"] and sorted(capped) == ["busy", "proj-1"]
    # again: nothing left to do
    assert sd.compact(root, {"run-live"}, [root / "busy"])["compressed"] == []


def test_compact_stops_on_a_full_disk_and_out_of_time(tmp_path, monkeypatch):
    root = tmp_path / "logs"
    for index in range(3):
        write(root / f"p/connections-2026110{index}-100000-r{index}.jsonl", text=jsonl(20), age=7200)

    def full(path, level=3):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(sd, "compress", full)
    result = sd.compact(root)
    assert len(result["errors"]) == 1 and len(list(root.glob("p/*.jsonl"))) == 3
    monkeypatch.undo()
    ticks = iter([0, 0, 100, 100, 100, 100])
    result = sd.compact(root, budget=sd.Budget(10, clock=lambda: next(ticks)))
    assert result["unfinished"] is True and len(result["compressed"]) == 1


def test_compressed_tail_bounds_and_damage(tmp_path):
    tails = load("jsonltail")
    path = tmp_path / "x.jsonl.gz"
    path.write_bytes(gzip.compress(b"".join(b'{"n": %d}\n' % i for i in range(1000)) + b'{"cut'))
    lines, skipped, long = tails.compressed_tail(path, 100)
    assert lines[-1] == b'{"n": 999}\n' and sum(map(len, lines)) <= 100 and skipped > 0 and long == 0
    lines, _, _ = tails.compressed_tail(path, 1 << 20, keep_lines=3)
    assert [json.loads(line)["n"] for line in lines] == [997, 998, 999]
    path.write_bytes(gzip.compress(b'{"n": 1}\n' + b"y" * 5000 + b'\n{"n": 2}\n'))
    lines, _, long = tails.compressed_tail(path, 1 << 20, line_limit=100)
    assert lines == [b'{"n": 1}\n', b'{"n": 2}\n'] and long == 1
    path.write_bytes(gzip.compress(b'{"n": 1}\n{"n": 2}\n' * 1000)[:-30])  # cut short
    lines, _, _ = tails.compressed_tail(path, 1 << 20)
    assert lines and lines[0] == b'{"n": 1}\n'
    assert tails.compressed_tail(tmp_path / "missing.gz", 10) == ([], 0, 0)


def test_readers_read_compressed_logs(tmp_path):
    """What gc compressed is still read: sandbox-ai logs (statistics), report, llm traffic, the canary check."""
    record = {"time": "2026-11-01T10:00:00Z", "event": "http", "host": "api.example.org", "method": "POST",
              "url": "https://api.example.org/v1", "status": 200,
              "model": {"model": "m1", "usage": {"input_tokens": 5}}}
    log = write(tmp_path / "connections-20261101-100000-run-a.jsonl", text=json.dumps(record) + "\n", age=7200)
    traffic = {"version": 1, "time": "2026-11-01T10:00:00Z", "session": "s1", "run": "run-a", "n": 1}
    tlog = write(tmp_path / "llm-traffic-20261101-100000-run-a.jsonl",
                 text="".join(json.dumps({**traffic, "n": i}) + "\n" for i in range(5)), age=7200)
    logs, llm, report, canary = load("logs"), load("llm_traffic"), load("report"), load("canary")
    before = logs.telemetry(tmp_path)
    expected = canary.model_requests(tmp_path, "run-a", ["api.example.org"])
    sd.compress(log)
    sd.compress(tlog)
    assert logs.telemetry(tmp_path)["model_calls"] == before["model_calls"] == {"m1": 1}
    assert [p.name for p in logs.connection_logs(tmp_path)] == [log.name + ".gz"]
    assert report.CONNECTION_LOG.fullmatch(log.name + ".gz").group(2) == "run-a"
    files = llm.run_files(tmp_path)
    assert list(files) == ["run-a"] and files["run-a"]["paths"][0].name.endswith(".gz")
    paths = files["run-a"]["paths"]
    assert [r["n"] for r in llm.read_records(paths)] == [0, 1, 2, 3, 4]
    assert [r["n"] for r in llm.tail_records(paths, 2)] == [3, 4]
    assert llm.first_record(paths[0])["session"] == "s1"
    found = canary.model_requests(tmp_path, "run-a", ["api.example.org"])
    assert len(expected) == 1 and found == [{**expected[0], "log": log.name + ".gz"}]


def test_rotation_counts_compressed_logs(tmp_path):
    launcher = load("agent_sandbox")
    for name in ("connections-20261101-100000-a.jsonl.gz", "connections-20261102-100000-b.jsonl"):
        write(tmp_path / name, 100, age=7200)
    assert launcher.rotate_connection_logs(tmp_path, limit=150) == ["connections-20261101-100000-a.jsonl.gz"]
    for name in ("llm-traffic-20261101-100000-a.jsonl.2.gz", "llm-traffic-20261101-100000-a.jsonl.1",
                 "llm-traffic-20261101-100000-a.jsonl.gz", "llm-traffic-20261102-100000-b.jsonl"):
        write(tmp_path / name, 100, age=7200)
    assert launcher.rotate_traffic_logs(tmp_path, limit=150) == [
        "llm-traffic-20261101-100000-a.jsonl.2.gz", "llm-traffic-20261101-100000-a.jsonl.1",
        "llm-traffic-20261101-100000-a.jsonl.gz"]


def test_gc_compacts_end_to_end(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    make_tree(state)
    logs = state / "logs/proj-0123456789ab"
    record = {"time": "2026-11-01T10:00:00Z", "event": "http", "host": "api.example.org",
              "model": {"model": "m1", "usage": {"input_tokens": 5}}}
    write(logs / "connections-20261101-110000-run-b.jsonl", text=json.dumps(record) + "\n", age=7200)
    dry = run_cli(env, "gc", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr
    planned = json.loads(dry.stdout)["compacted"]
    assert len(planned["compressed"]) == 4 and planned["bytes"] > 0  # every ended run's log (run-live ended too)
    assert not list(logs.glob("*.gz"))
    said = run_cli(env, "gc", "--dry-run")
    assert "would compress 4 log(s) of ended runs" in said.stdout
    done = run_cli(env, "gc")
    assert done.returncode == 0, done.stderr
    assert "compressed 4 log(s) of ended runs" in done.stdout and len(list(logs.glob("*.jsonl.gz"))) == 4
    shown = run_cli(env, "logs", str(logs), "--json")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["telemetry"]["model_calls"] == {"m1": 1}
    kept = run_cli(env, "gc", "--no-compact", "--json")
    assert json.loads(kept.stdout)["compacted"] is None
    refused = run_cli(env, "gc", "--usage", "--caches")
    assert refused.returncode == 1


def test_auto_due(env, tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_AI_AUTO_COMPACT", "1")
    assert sd.auto_due() is False  # nothing kept here yet: nothing to compact
    state = tmp_path / "state/sandbox-ai"
    state.mkdir(parents=True)
    assert sd.auto_due() is True
    stamp = write(state / sd.AUTO_STAMP, text="{}")
    assert sd.auto_due() is False
    os.utime(stamp, (time.time() - 2 * 86400,) * 2)
    assert sd.auto_due() is True
    monkeypatch.setenv("SANDBOX_AI_AUTO_COMPACT", "0")
    assert sd.auto_due() is False


def test_auto_pass_end_to_end(env, tmp_path):
    """The daily pass a start leaves running in the background: the ended runs' logs compressed, noted."""
    state = tmp_path / "state/sandbox-ai"
    make_tree(state)
    result = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/statedirs.py"), "--auto"],
                            capture_output=True, text=True, env={**os.environ, **env}, timeout=120)
    assert result.returncode == 0, result.stderr
    stamp = json.loads((state / sd.AUTO_STAMP).read_text())
    assert stamp["compressed"] == 3 and stamp["saved"] > 0 and stamp["errors"] == []
    assert len(list(state.glob("logs/proj-0123456789ab/*.jsonl.gz"))) == 3
    assert "daily compaction of the logs: 3 compressed" in (state / "launcher.log").read_text()


def test_auto_pass_applies_the_default_expiry_after_its_notice(env, tmp_path):
    """The daily pass removes the stopped kept starts unused for 90 days - no keep_days set - once the default expiry
    was announced 14 days ago; the first pass only announces it."""
    state = tmp_path / "state/sandbox-ai"
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 400 * 86400))
    write(state / "sessions/old-1/home/.bashrc", 10)
    write(state / "sessions/old-1/session.json", text=json.dumps(
        {"agent": "claude", "options": [], "backend": "bwrap", "keep": True, "created": old, "started": old,
         "workspace": "/srv/app", "cwd": "/"}))

    def daily():
        result = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/statedirs.py"), "--auto"],
                                capture_output=True, text=True, env={**os.environ, **env}, timeout=120)
        assert result.returncode == 0, result.stderr
        return json.loads((state / sd.AUTO_STAMP).read_text())
    first = daily()
    assert first["expired"] == [] and first["expiring"] == ["old-1"] and (state / "sessions/old-1").exists()
    notice = json.loads((state / "expiry.json").read_text())
    notice["noticed"] = notice["said"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 15 * 86400))
    (state / "expiry.json").write_text(json.dumps(notice))
    second = daily()
    assert second["expired"] == ["old-1"] and not (state / "sessions/old-1").exists()
    assert "removed the kept start old-1" in (state / "launcher.log").read_text()


def test_clear_caches(tmp_path):
    home = tmp_path / "home"
    write(home / ".cache/pip/a", 1000)
    write(home / ".npm/_cacache/b", 1000)
    write(home / ".claude/projects/t.jsonl", 1000)
    write(home / ".cache/ms-playwright/chromium", 1000)  # a browser download: not a cache gc empties
    outside = write(tmp_path / "outside/keep", 1000)
    (home / ".cache/uv").symlink_to(outside.parent)
    planned = sd.clear_caches([home], dry_run=True)
    assert sorted(item["path"] for item in planned) == [".cache/pip", ".npm/_cacache"]
    assert (home / ".cache/pip/a").exists()
    done = sd.clear_caches([home, tmp_path / "missing"])
    assert sorted(item["path"] for item in done) == [".cache/pip", ".npm/_cacache"]
    assert not (home / ".cache/pip").exists() and (home / ".claude/projects/t.jsonl").exists()
    assert (home / ".cache/ms-playwright/chromium").exists() and outside.exists()


def test_gc_caches_and_images_end_to_end(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    home = make_tree(state)
    write(state / "sessions/claude-big-1234/session.json", text=json.dumps(
        {"agent": "claude", "options": [], "backend": "bwrap", "keep": True, "created": "2026-10-01T10:00:00Z",
         "workspace": "/srv/app", "cwd": "/"}))
    dry = run_cli(env, "gc", "--caches", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr
    planned = json.loads(dry.stdout)["caches"]
    assert sorted(item["path"] for item in planned) == [".cache/pip", ".cache/uv", ".npm/_cacache"]
    assert (home / ".cache/pip/wheel").exists()
    done = run_cli(env, "gc", "--caches", "--no-compact")
    assert done.returncode == 0, done.stderr
    assert "removed the download caches in the kept HOMEs" in done.stdout
    assert not (home / ".cache/pip").exists() and (home / "scratch/run1/data.bin").exists()
    assert (home / ".claude/projects/p/t.jsonl").exists()
    # without Podman: no image is named, nothing fails
    images = run_cli(env, "gc", "--images", "--no-compact", "--json")
    assert images.returncode == 0 and json.loads(images.stdout)["images"] == []


def test_remove_images():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        failed = command[-1].startswith("b")
        return subprocess.CompletedProcess(command, 2 if failed else 0, "", "image is in use by a container")
    removed, stayed = sd.remove_images(["a" * 12, "b" * 12, "not an id"], run)
    assert removed == ["a" * 12] and list(stayed) == ["b" * 12] and "in use" in stayed["b" * 12]
    assert calls == [["podman", "image", "rm", "a" * 12], ["podman", "image", "rm", "b" * 12]]  # never forced


# -- what a long run rotates while it runs (runtime.rotate_logs), and the readers of the parts -----------------------

def test_the_readers_take_a_runs_rotated_parts_newest_first(tmp_path):
    logs, report, canary = load("logs"), load("report"), load("canary")
    base = "connections-20261112-094036-claude-e7d6"
    record = {"time": "2026-11-12T10:00:00Z", "event": "http", "host": "api.example.org", "method": "POST",
              "url": "https://api.example.org/v1", "status": 200, "model": {"model": "m1", "usage": {"x": 1}}}
    for name, count in ((f"{base}.jsonl.2.gz", 1), (f"{base}.jsonl.1.gz", 2), (f"{base}.jsonl", 3)):
        data = (json.dumps(record) + "\n").encode() * count
        (tmp_path / name).write_bytes(gzip.compress(data) if name.endswith(".gz") else data)
    write(tmp_path / "connections-20261111-212012-claude-db35.jsonl.gz", text="")
    assert [p.name for p in logs.connection_logs(tmp_path)] == [
        f"{base}.jsonl", f"{base}.jsonl.1.gz", f"{base}.jsonl.2.gz", "connections-20261111-212012-claude-db35.jsonl.gz"]
    assert logs.telemetry(tmp_path)["model_calls"] == {"m1": 6}  # every part read
    assert report.CONNECTION_LOG.fullmatch(f"{base}.jsonl.2.gz").group(2) == "claude-e7d6"
    assert len(canary.model_requests(tmp_path, "claude-e7d6", ["api.example.org"])) == 6
    launcher = load("agent_sandbox")
    for path in tmp_path.iterdir():
        os.utime(path, (time.time() - 7200,) * 2)
    removed = launcher.rotate_connection_logs(tmp_path, limit=1)
    assert removed[:3] == ["connections-20261111-212012-claude-db35.jsonl.gz", f"{base}.jsonl.2.gz",
                           f"{base}.jsonl.1.gz"]  # the oldest run first, then a run's oldest part


def test_gc_compresses_the_parts_a_run_left_uncompressed(tmp_path):
    root = tmp_path / "logs"
    raw = write(root / "p/activity.jsonl.20261112-094036", text=jsonl(100), age=7200)
    assert sd.log_kind(raw.name) == "older parts"
    done = sd.compact(root)
    assert [Path(item["path"]).name for item in done["compressed"]] == [raw.name]
    assert (root / "p" / (raw.name + ".gz")).exists() and not raw.exists()
    assert sd.log_kind(raw.name + ".gz") == "older parts (compressed)"


def test_activity_rotates_instead_of_stopping(tmp_path, monkeypatch):
    activity = load("activity")
    path = tmp_path / "activity.jsonl"
    records = [{"id": str(i), "summary": "x" * 50} for i in range(10)]
    assert activity.append(path, records[:5], limit=400) is True
    first = path.read_text()
    assert activity.append(path, records[5:], limit=400) is True  # over the limit: the older part renamed away
    older = [p for p in tmp_path.iterdir() if p.name.startswith("activity.jsonl.")]
    assert len(older) == 1 and older[0].read_text() == first and sd.ARCHIVE_RAW.fullmatch(older[0].name)
    assert [json.loads(line)["id"] for line in path.read_text().splitlines()] == ["5", "6", "7", "8", "9"]
    assert "log-full" not in path.read_text()


def test_a_followed_log_rotated_during_the_run_keeps_its_keys_apart(tmp_path):
    activity = load("activity")
    log = tmp_path / "connections-x.jsonl"
    log.write_text('{"n": 1}\n{"n": 2}\n')
    follower = activity.LogFile(tmp_path, log.name, from_start=True)
    budget = [1 << 20]
    assert [offset for offset, _ in follower.records(budget)] == [0, 9]
    os.truncate(log, 0)  # emptied in place (runtime.rotate_held_log)
    with open(log, "a") as stream:
        stream.write('{"n": 3}\n')
    assert [(offset, r["n"]) for offset, r in follower.records([1 << 20])] == [("1.0", 3)]
    log.rename(tmp_path / "older")  # renamed away (runtime.rotate_shared_log)
    log.write_text('{"n": 4}\n{"n": 5}\n')
    assert [(offset, r["n"]) for offset, r in follower.records([1 << 20])] == [("2.0", 4), ("2.9", 5)]
    with open(log, "a") as stream:
        stream.write('{"n": 6}\n')
    assert [offset for offset, _ in follower.records([1 << 20])] == ["2.18"]


# -- log folders of projects that are gone, or not used for long (gc --logs-older-than) ----------------------------

def age_tree(folder, days):
    when = time.time() - days * 86400
    for path in [*Path(folder).rglob("*"), Path(folder)]:
        os.utime(path, (when, when), follow_symlinks=False)


def test_old_log_folders_by_their_newest_write(tmp_path):
    root = tmp_path / "logs"
    write(root / "gone-111111111111/activity.jsonl", 5000)
    write(root / "gone-111111111111/sub/files.jsonl", 3000)
    age_tree(root / "gone-111111111111", 200)
    write(root / "recent-222222222222/dns.jsonl", 100)
    age_tree(root / "recent-222222222222", 200)
    write(root / "recent-222222222222/new.jsonl", 100)  # one new write: the folder is in use
    write(root / "busy-333333333333/activity.jsonl", 100)
    age_tree(root / "busy-333333333333", 200)
    (root / "link-444444444444").symlink_to(root / "gone-111111111111")
    found = sd.old_log_folders(root, 90, busy_dirs=[str(root / "busy-333333333333")])
    assert [item["name"] for item in found] == ["gone-111111111111"]
    assert found[0]["bytes"] >= 8000 and found[0]["last_written"] < sd.iso(time.time() - 199 * 86400)
    assert sd.old_log_folders(root, 365) == []


def test_remove_log_folder_puts_back_one_a_run_took_meanwhile(tmp_path):
    root = tmp_path / "logs"
    write(root / "a-1/activity.jsonl", 100)
    write(root / "b-2/activity.jsonl", 100)
    assert sd.remove_log_folder(root / "a-1", busy=lambda: []) is True
    assert not (root / "a-1").exists() and list(root.iterdir()) == [root / "b-2"]
    assert sd.remove_log_folder(root / "b-2", busy=lambda: [str(root / "b-2")]) is False
    assert (root / "b-2/activity.jsonl").exists() and list(root.iterdir()) == [root / "b-2"]


def test_gc_logs_older_than_end_to_end(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    make_tree(state)
    gone = state / "logs/gone-555555555555"
    write(gone / "activity.jsonl", text=jsonl(50))
    age_tree(gone, 120)
    age_tree(state / "logs/other-ba9876543210", 40)
    usage = json.loads(run_cli(env, "gc", "--usage", "--json").stdout)["would_free"]["logs_older_than"]
    assert {entry["days"]: entry["count"] for entry in usage}[90] == 1
    assert {entry["days"]: entry["count"] for entry in usage}[30] == 2
    said = run_cli(env, "gc", "--usage").stdout
    assert re.search(r"sandbox-ai gc --logs-older-than 90\s+or: 1 log folder\(s\)", said)
    dry = run_cli(env, "gc", "--logs-older-than", "90", "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr
    assert [item["name"] for item in json.loads(dry.stdout)["log_folders"]] == ["gone-555555555555"]
    assert gone.exists()
    said = run_cli(env, "gc", "--logs-older-than", "90", "--dry-run", "--no-compact")
    assert "would remove the log folder gone-555555555555" in said.stdout
    plain = run_cli(env, "gc", "--no-compact", "--json")  # without the option: no log folder goes
    assert json.loads(plain.stdout)["log_folders"] == [] and gone.exists()
    done = run_cli(env, "gc", "--logs-older-than", "90", "--no-compact")
    assert done.returncode == 0, done.stderr
    assert "removed the log folder gone-555555555555" in done.stdout
    assert not gone.exists() and (state / "logs/other-ba9876543210").exists()
    assert (state / "logs/proj-0123456789ab").exists()
    refused = run_cli(env, "gc", "--logs-older-than", "0")
    assert refused.returncode == 1 and "--logs-older-than" in refused.stderr


def test_gc_never_removes_the_log_folder_a_registered_run_logs_into(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    folder = state / "logs/live-666666666666"
    write(folder / "activity.jsonl", 100)
    age_tree(folder, 400)
    run_dir = tmp_path / "runtime/sandbox-ai/runs/run-live"
    write(run_dir / "run.json", text=json.dumps({"run": "run-live", "log_dir": str(folder), "pid": os.getpid(),
                                                 "started": "2026-01-01T00:00:00Z"}))
    done = run_cli(env, "gc", "--logs-older-than", "30", "--no-compact", "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["log_folders"] == [] and folder.exists()


# -- the MCP servers' logs, the web interface's audit log, the MCP root file systems ------------------------------

def unpacked(folder, name):
    """Every line of the log NAME in FOLDER, its compressed older parts first (oldest first)."""
    lines = []
    for part in sorted(Path(folder).glob(f"{name}.*.gz")):
        lines += gzip.decompress(part.read_bytes()).decode().splitlines()
    path = Path(folder) / name
    return lines + (path.read_text().splitlines() if path.exists() else [])


def test_rotate_renames_compresses_and_caps(tmp_path):
    log = write(tmp_path / "supervisor.log", text="".join(f"line {i}\n" for i in range(500)))
    assert sd.log_kind("supervisor.log.20261101-100000.gz") == "older parts (compressed)"
    old = write(tmp_path / "supervisor.log.20200101-000000.gz", 5000)
    name = sd.rotate(log, limit=4000, when=time.time())
    assert name.startswith("supervisor.log.") and name.endswith(".gz") and not log.exists()
    assert gzip.decompress((tmp_path / name).read_bytes()).decode().splitlines()[-1] == "line 499"
    assert not old.exists(), "the oldest part beyond the limit went"
    assert sd.rotate(tmp_path / "missing.log") is None


def test_an_mcp_servers_logs_rotate_while_it_writes(tmp_path, monkeypatch):
    mcp = load("mcp")
    monkeypatch.setattr(mcp, "MCP_LOG_PART", 4000)
    folder = tmp_path / "mcp/fetch"
    write(folder / "supervisor.log", text="before\n" * 1000)  # larger than the part already: rotated first
    (folder / "approvals").mkdir()
    import io
    lines = [f"server output {i}\n".encode() for i in range(600)]
    calls = [b"sandbox-ai-mcp-call " + json.dumps({"tool": "t", "n": i}).encode() + b"\n" for i in range(300)]
    data = b"".join(x for pair in zip(lines[:300], calls) for x in pair) + b"".join(lines[300:])
    clock = iter(range(10 ** 6))
    monkeypatch.setattr(mcp, "rotation_time", lambda: 1_700_000_000 + next(clock))
    mcp.split_log(folder, io.BytesIO(data))
    assert (folder / "supervisor.log").stat().st_size <= 4000 + 100
    assert unpacked(folder, "supervisor.log") == ["before"] * 1000 + [line.decode().strip() for line in lines]
    assert [json.loads(line)["n"] for line in unpacked(folder, "mcp-calls.jsonl")] == list(range(300))
    assert len(list(folder.glob("mcp-calls.jsonl.*.gz"))) >= 2


def test_gc_compacts_the_logs_of_mcp_servers_that_do_not_run(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    idle, busy = state / "mcp/idle", state / "mcp/busy"
    write(idle / "supervisor.log", text="x\n" * 40_000, age=7200)
    write(idle / "mcp-calls.jsonl.20261101-100000", text=jsonl(100), age=7200)
    write(idle / "approvals/1-2-3.json", text="{}", age=40 * 86400)
    write(idle / "approvals/1-2-3.done", text="{}", age=40 * 86400)
    write(idle / "approvals/4-5-6.json", text="{}", age=40 * 86400)  # never decided: left
    write(idle / "approvals/7-8-9.json", text="{}")
    write(idle / "approvals/7-8-9.done", text="{}")
    write(busy / "supervisor.log", text="x\n" * 40_000, age=7200)
    import fcntl
    holder = os.open(write(busy / ".log.lock", 0), os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_SH)  # its log reader runs (mcp.split_log holds this)
    env = {**env, "SANDBOX_AI_MCP_LOG_PART": "50000"}
    try:
        dry = run_cli(env, "gc", "--dry-run", "--json")
        assert dry.returncode == 0, dry.stderr
        planned = json.loads(dry.stdout)["mcp_logs"]
        assert sorted(Path(p).name for p in planned["rotated"]) == ["supervisor.log"]
        assert (idle / "supervisor.log").exists()
        done = run_cli(env, "gc")
        assert done.returncode == 0, done.stderr
        assert "of the MCP servers' logs" in done.stdout
    finally:
        os.close(holder)
    assert not (idle / "supervisor.log").exists() and len(list(idle.glob("supervisor.log.*.gz"))) == 1
    assert list(idle.glob("mcp-calls.jsonl.*.gz")) and not (idle / "mcp-calls.jsonl.20261101-100000").exists()
    assert sorted(p.name for p in (idle / "approvals").iterdir()) == ["4-5-6.json", "7-8-9.done", "7-8-9.json"]
    assert (busy / "supervisor.log").stat().st_size == 80_000 and not list(busy.glob("*.gz"))


def test_the_web_audit_log_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    web = load("webui")
    monkeypatch.setattr(web, "AUDIT_PART", 3000)
    monkeypatch.setattr(web, "AUDIT_ARCHIVE", 1 << 20)
    clock = iter(range(10 ** 6))
    monkeypatch.setattr(web, "rotation_time", lambda: 1_700_000_000 + next(clock))
    for number in range(400):
        web.audit("action", n=number)
    folder = web.state_dir()
    assert (folder / "audit.jsonl").stat().st_size <= 3000 + 200
    assert [json.loads(line)["n"] for line in unpacked(folder, "audit.jsonl")] == list(range(400))


def test_gc_removes_mcp_root_file_systems_no_server_uses(env, tmp_path):
    store = tmp_path / "data/sandbox-ai/mcp-rootfs"
    used, unused, fresh, named = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
    for sha in (used, unused, fresh, named):
        write(store / sha / "usr/bin/server", 1000)
        (store / sha / "usr").chmod(0o555)  # read-only, as unpacked
    for sha in (used, unused, named):
        age_tree(store / sha, 30)
    write(tmp_path / "config/sandbox-ai/mcp.json", text=json.dumps(
        {"fetch": {"rootfs": {"path": str(store / used), "sha256": used}}}))
    write(tmp_path / "state/sandbox-ai/mcp/other/mcp.json", text=json.dumps({"rootfs": str(store / named)}))
    dry = run_cli(env, "gc", "--dry-run", "--no-compact", "--json")
    assert dry.returncode == 0, dry.stderr
    assert [item["sha256"] for item in json.loads(dry.stdout)["rootfs"]] == [unused] and (store / unused).exists()
    usage = json.loads(run_cli(env, "gc", "--usage", "--json").stdout)["would_free"]["rootfs"]
    assert usage["count"] == 1 and usage["bytes"] >= 1000
    done = run_cli(env, "gc", "--no-compact")
    assert done.returncode == 0, done.stderr
    assert "MCP root file system" in done.stdout
    assert sorted(p.name for p in store.iterdir()) == sorted([used, fresh, named])
    write(tmp_path / "config/sandbox-ai/mcp.json", text="{broken")
    broken = run_cli(env, "gc", "--no-compact", "--json")  # which servers there are cannot be told: none goes
    assert json.loads(broken.stdout)["rootfs"] == []


# -- what is in a HOME, by kind (gc --usage --detail) -------------------------------------------------------------

def detail_home(home):
    write(home / ".cache/pip/wheel", 300_000)
    write(home / ".cache/ms-playwright/chromium/chrome", 200_000)
    write(home / ".cache/fontconfig/x", 1000)
    write(home / ".cargo/registry/index/a", 100_000)
    write(home / ".cargo/bin/rg", 50_000)
    write(home / ".rustup/toolchains/stable/lib", 400_000)
    write(home / ".bun/install/cache/x", 20_000)
    write(home / ".claude/projects/p/t.jsonl", 200_000)
    write(home / ".claude/settings.json", text="{}")
    write(home / ".codex/sessions/2026/11/01/r.jsonl", 70_000)
    write(home / "scratch/app/node_modules/left-pad/index.js", 150_000)
    write(home / "scratch/rust/target/CACHEDIR.TAG", text="Signature: 8a477f597d28d172789f06886806bc55")
    write(home / "scratch/rust/target/debug/big", 250_000)
    write(home / "scratch/py/.venv/pyvenv.cfg", text="home = /usr/bin")
    write(home / "scratch/py/.venv/lib/x", 60_000)
    write(home / "scratch/notavenv/venv/data", 30_000)  # no pyvenv.cfg: the agent's own folder
    write(home / "scratch/data.bin", 500_000)
    write(home / "tmp/copy", 40_000)
    write(home / ".bashrc", 100)


def test_home_detail_by_kind(tmp_path):
    home = tmp_path / "home"
    detail_home(home)
    found = sd.home_detail(sd.Scan(), home)
    assert found["bytes"] == sd.Scan().size(home)  # the same as a plain measurement
    assert sum(found["kinds"].values()) == found["bytes"]  # every byte counted once
    assert list(found["kinds"]) == ["caches", "toolchains", "agent", "build", "other"]
    items = {item["path"]: item for item in found["items"]}
    assert items[".cache/pip"]["kind"] == "caches" and items[".cache/pip"]["what"] == "pip"
    assert items[".cache/ms-playwright"]["what"] == "Playwright browsers"
    assert items[".cache"]["what"] == ".cache (other)" and items[".cache"]["bytes"] < 20_000
    assert items[".cargo/registry"]["kind"] == "caches" and items[".cargo"]["kind"] == "toolchains"
    assert items[".bun/install/cache"]["kind"] == "caches"
    assert items[".claude/projects"]["what"] == "Claude transcripts" and items[".claude"]["kind"] == "agent"
    assert items[".codex/sessions"]["what"] == "Codex transcripts"
    assert items["scratch/app/node_modules"]["kind"] == "build"
    assert items["scratch/rust/target"]["what"] == "Rust target"
    assert items["scratch/py/.venv"]["what"] == "Python virtualenv"
    assert items["tmp"]["what"] == "temp"
    assert "scratch/notavenv/venv" not in items
    other = {entry["path"]: entry["bytes"] for entry in found["other"]}
    assert other["scratch"] >= 530_000 and other["scratch"] < 600_000  # its build output is not in it
    assert "(files directly in HOME)" in other
    assert [entry["path"] for entry in found["biggest"]][:2] == ["scratch", ".cache"]
    assert set(found["caches"]) == {".cache/pip", ".bun/install/cache"}
    assert [item["bytes"] for item in found["items"]] == sorted((i["bytes"] for i in found["items"]), reverse=True)


def test_home_detail_never_follows_links_and_survives_the_unreadable(tmp_path):
    home = tmp_path / "home"
    write(home / "scratch/a", 1000)
    outside = write(tmp_path / "outside/huge", 3_000_000).parent
    (home / ".cache").symlink_to(outside)  # a known path that is a link: counted as a link
    (home / "scratch/link").symlink_to(outside)
    linked = write(home / "scratch/hard1", 100_000)
    os.link(linked, home / "scratch/hard2")  # a hard link: once
    locked = write(home / ".claude/projects/p/t.jsonl", 1000).parent
    locked.chmod(0)
    try:
        found = sd.home_detail(sd.Scan(), home)
    finally:
        locked.chmod(0o700)
    assert found["bytes"] < 300_000 and sum(found["kinds"].values()) == found["bytes"]
    if os.geteuid() != 0:
        assert sd.home_detail(sd.Scan(), home)["bytes"] > 0
    # no HOME, a HOME that is a file or a link: nothing, never a crash
    assert sd.home_detail(sd.Scan(), tmp_path / "missing")["bytes"] == 0
    assert sd.home_detail(sd.Scan(), write(tmp_path / "file", 10))["bytes"] == 0
    (tmp_path / "linkhome").symlink_to(home)
    assert sd.home_detail(sd.Scan(), tmp_path / "linkhome")["bytes"] == 0


def test_home_detail_within_its_budget(tmp_path):
    home = tmp_path / "home"
    for index in range(40):
        write(home / f"scratch/d{index}/f", 10)
    scan = sd.Scan(entries=10)
    found = sd.home_detail(scan, home)
    assert scan.partial is True and sum(found["kinds"].values()) == found["bytes"]
    clock = iter([0, 0] + [1000] * 1000)
    late = sd.Scan(seconds=5, clock=lambda: next(clock))
    sd.home_detail(late, home)
    assert late.partial is True


def kept(state, key, name, home_tree=None):
    write(state / f"sessions/{key}/session.json", text=json.dumps(
        {"agent": "claude", "options": [], "backend": "bwrap", "keep": True, "created": "2026-10-01T10:00:00Z",
         "workspace": "/srv/app", "cwd": "/", "name": name}))
    if home_tree:
        home_tree(state / f"sessions/{key}/home")
    return state / f"sessions/{key}/home"


def test_gc_usage_detail_end_to_end(env, tmp_path):
    """gc --usage --detail: every kept HOME by kind in one table with the total; --detail NAME one HOME in depth;
    read under the start's lock (a start a command holds is named busy, not read); nothing is changed."""
    state = tmp_path / "state/sandbox-ai"
    home = kept(state, "claude-big-1234", "claude-big", detail_home)
    kept(state, "claude-small-1234", "claude-small", lambda h: write(h / ".claude/projects/p/t.jsonl", 5000))
    found = json.loads(run_cli(env, "gc", "--usage", "--detail", "--json").stdout)
    assert [item["name"] for item in found["starts"]] == ["claude-big", "claude-small"]
    assert found["kinds"]["caches"] == sum(item["kinds"].get("caches", 0) for item in found["starts"])
    big = found["starts"][0]
    assert sum(big["kinds"].values()) == big["bytes"] and big["busy"] is None
    text = run_cli(env, "gc", "--usage", "--detail")
    assert text.returncode == 0, text.stderr
    head = next(line for line in text.stdout.splitlines() if line.split()[:3] == ["NAME", "STATE", "SIZE"])
    assert head.split()[3:] == ["CACHES", "TOOLCHAINS", "AGENT", "BUILD", "OTHER", "BIGGEST"]
    assert any(line.lstrip().startswith("Total") for line in text.stdout.splitlines())
    one = run_cli(env, "gc", "--usage", "--detail", "claude-big")
    assert one.returncode == 0, one.stderr
    assert "The HOME of claude-big" in one.stdout and "Claude transcripts" in one.stdout
    assert "scratch/app/node_modules" in one.stdout and "Rust target" in one.stdout
    # by id too; an unknown name is refused; --detail without --usage too
    assert run_cli(env, "gc", "--usage", "--detail", "claude-big-1234").returncode == 0
    missing = run_cli(env, "gc", "--usage", "--detail", "nope")
    assert missing.returncode == 1 and "no start named nope" in missing.stderr
    assert run_cli(env, "gc", "--detail").returncode == 1
    # a start another command holds: named busy, not read
    import fcntl
    with open(state / "sessions/claude-big-1234/.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        busy = json.loads(run_cli(env, "gc", "--usage", "--detail", "claude-big", "--json").stdout)
    assert busy["starts"][0]["busy"] and busy["starts"][0]["bytes"] == 0
    # nothing changed
    assert (home / ".cache/pip/wheel").exists() and (home / "scratch/app/node_modules/left-pad/index.js").exists()
    # the HOMEs' kinds are in gc --usage too
    usage = json.loads(run_cli(env, "gc", "--usage", "--json").stdout)
    first = usage["parts"]["starts"]["items"][0]
    assert first["home"]["kinds"]["caches"] > 0 and "--detail" in run_cli(env, "gc", "--usage").stdout


def test_image_groups(monkeypatch):
    """Podman's images by what they are to sandbox-ai; sizes from system df -v (what removing one frees) where Podman
    says them, else their own; nothing breaks on what Podman does not say."""
    monkeypatch.setattr(sd.shutil, "which", lambda name: "/usr/bin/podman")
    ours = {"sandbox-ai.agent-version": "1"}
    rows = [{"Id": "a" * 64, "Names": ["localhost/claude-sandbox:latest"], "Labels": ours, "Size": 30},
            {"Id": "b" * 64, "RepoTags": ["localhost/claude-sandbox:base"], "Labels": ours, "Size": 20},
            {"Id": "c" * 64, "Names": ["<none>:<none>"], "Labels": ours, "Size": 10},
            {"Id": "d" * 64, "Names": None, "Labels": None, "Size": 5, "ParentId": "e" * 64},
            {"Id": "e" * 64, "Names": [], "Labels": {}, "Size": 7},
            {"Id": "f" * 64, "Names": ["localhost/x"], "Labels": ours, "Size": 1, "Containers": 2},
            {"Id": "a" * 64, "Names": ["again"]}, {"Id": "not hex"}, "junk", {"Names": ["no id"]}]
    answers = {"images": json.dumps(rows), "df": json.dumps({"Images": [{"ImageID": "a" * 12, "UniqueSize": 3},
                                                                         {"ImageID": "zz", "UniqueSize": "big"}]})}

    def run(command, **kwargs):
        key = "images" if command[1] == "images" else "df"
        return subprocess.CompletedProcess(command, 0, answers[key], "")
    found = sd.image_groups({"localhost/claude-sandbox:latest"}, run)
    groups = found["groups"]
    assert groups["sandbox_used"]["count"] == 2 and groups["sandbox_used"]["bytes"] == 3 + 20  # :base of a used one
    assert groups["sandbox_unnamed"]["images"][0]["id"] == "c" * 12
    assert groups["other_unnamed"]["count"] == 1 and groups["intermediate"]["images"][0]["id"] == "e" * 12
    assert groups["in_use"]["count"] == 1 and found["count"] == 6 and found["unique_sizes"] is True
    assert sd.image_groups(set(), run)["groups"]["sandbox_unused"]["count"] == 2  # no kept start: unused
    # system df -v not answered: their own sizes; images not answered: None
    answers["df"] = "not json"
    assert sd.image_groups(set(), run)["unique_sizes"] is False
    answers["images"] = "{}"
    assert sd.image_groups(set(), run) is None
    monkeypatch.setattr(sd.shutil, "which", lambda name: None)
    assert sd.image_groups(set(), run) is None


def test_remove_image_refs_never_forces_nor_takes_options():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 2 if "used" in command[-1] else 0, "", "in use by a container")
    removed, stayed = sd.remove_image_refs(["localhost/old:latest", "--all", "-f", "a;rm", "used:1", "c" * 12], run)
    assert removed == ["localhost/old:latest", "c" * 12] and list(stayed) == ["used:1"]
    assert all(command[:3] == ["podman", "image", "rm"] and len(command) == 4 for command in calls)
