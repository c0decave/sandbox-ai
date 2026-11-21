"""Review 2 of sandbox-ai's logs (docs/reviews/state-logs-review-2.md): what the rotation of a log held open loses,
what a writer killed mid-record does to the next one, the per-folder caps and a live run, the logs' age limit and when
it is announced, mcp-calls.jsonl's rotation, a session's supervisor.log, gc --usage's estimates per kind, and the
LLM traffic log's records as deltas."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()
DAY = 86400


def load(name):
    spec = importlib.util.spec_from_file_location(f"state_logs_2_{name}", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime")
sd = load("statedirs")


def aged(path, data, age=7200):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    when = time.time() - age
    os.utime(path, (when, when))
    return path


def lines_of(path):
    data = gzip.decompress(path.read_bytes()) if path.name.endswith(".gz") else path.read_bytes()
    return [json.loads(line) for line in data.splitlines()]


# -- the connection log's other writer: the run's taint events --------------------------------------------------------

def test_a_taint_event_written_while_the_connection_log_rotates_is_not_lost(tmp_path, monkeypatch):
    """Taint.append wrote into the held connection log without its lock: what it wrote between the rotation's last
    copy and the emptying was lost (70 of 300 records in a stress run with 17 rotations)."""
    log = tmp_path / "connections-20261116-100000-r.jsonl"
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    server = rt.ProxyServer({"allow": {}}, fd, run_dir=tmp_path)
    taint = rt.Taint(log=log)
    try:
        server.log({"connection": 1, "event": "http"})
        real = rt.os.ftruncate
        late = []

        def truncate(target, length):  # the rotation's last step: a taint event comes in just before it
            writer = threading.Thread(target=taint.append, args=({"event": "taint", "what": "late"},))
            writer.start()
            writer.join(0.3)
            late.append(writer)
            return real(target, length)
        monkeypatch.setattr(rt.os, "ftruncate", truncate)
        assert rt.rotate_held_log(log)
        monkeypatch.setattr(rt.os, "ftruncate", real)
        late[0].join(5)
        server.log({"connection": 2, "event": "http"})
    finally:
        os.close(fd)
    records = lines_of(log.with_name(log.name + ".1.gz")) + lines_of(log)
    assert [r.get("connection") or r.get("what") for r in records] == [1, "late", 2]


# -- a record cut by a writer that was killed: the next one starts a line of its own -------------------------------

def test_a_cut_line_does_not_swallow_the_next_record(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    (tmp_path / "files.jsonl").write_bytes(b'{"action": "write", "path": "/w/a"}\n{"action": "wri')  # killed
    assert rt.log_event("files.jsonl", {"action": "write", "path": "/w/b"})
    lines = (tmp_path / "files.jsonl").read_bytes().splitlines()
    assert lines[1] == b'{"action": "wri' and json.loads(lines[2])["path"] == "/w/b"
    # the log held open by the proxy (append_locked), the activity log, the tool log
    held = tmp_path / "held.jsonl"
    held.write_bytes(b'{"cut')
    fd = os.open(held, os.O_RDWR | os.O_APPEND)
    try:
        rt.append_locked(fd, b'{"next": 1}\n')
    finally:
        os.close(fd)
    assert held.read_bytes() == b'{"cut\n{"next": 1}\n'
    activity = load("activity")
    (tmp_path / "activity.jsonl").write_bytes(b'{"kind": "pro')
    assert activity.append(tmp_path / "activity.jsonl", [{"kind": "reply"}])
    assert json.loads((tmp_path / "activity.jsonl").read_bytes().splitlines()[1]) == {"kind": "reply"}
    hooks = rt.HookServer(rt.Ruleset([]), log=tmp_path / "tools.jsonl")
    (tmp_path / "tools.jsonl").write_bytes(b'{"tool": "Ba')
    hooks.write({"tool": "Read"})
    assert json.loads((tmp_path / "tools.jsonl").read_bytes().splitlines()[1])["tool"] == "Read"


# -- mcp-calls.jsonl: rotated while the run lives, its writer goes on in the new file --------------------------------

def test_the_mcp_call_log_rotates_during_the_run_and_its_writer_follows(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    assert "mcp-calls.jsonl" in rt.ROTATED_LOGS
    relays = rt.McpRelays(log=tmp_path / "mcp-calls.jsonl")
    out = relays.stream()
    print(json.dumps({"tool": "one"}), file=out, flush=True)  # as McpGateway writes its records
    assert rt.rotate_logs(tmp_path, part=1) == ["mcp-calls.jsonl"]
    print(json.dumps({"tool": "two"}), file=out, flush=True)
    assert [r["tool"] for r in lines_of(tmp_path / "mcp-calls.jsonl")] == ["two"]
    older = [p for p in tmp_path.iterdir() if p.name.startswith("mcp-calls.jsonl.") and p.name.endswith(".gz")]
    assert len(older) == 1 and [r["tool"] for r in lines_of(older[0])] == ["one"]


def test_two_threads_printing_records_never_mix_their_lines(tmp_path):
    relays = rt.McpRelays(log=tmp_path / "mcp-calls.jsonl")
    out = relays.stream()

    def writer(name):
        for index in range(200):
            print(json.dumps({"tool": name, "i": index, "pad": "x" * 300}), file=out, flush=True)
    threads = [threading.Thread(target=writer, args=(name,)) for name in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(lines_of(tmp_path / "mcp-calls.jsonl")) == 400


def test_a_record_written_into_a_shared_log_just_renamed_away_is_kept(tmp_path, monkeypatch):
    """A writer that opened files.jsonl before the rotation renamed it may write after the part was compressed: that
    record goes into the compressed part too, never lost with the uncompressed one."""
    log = tmp_path / "files.jsonl"
    log.write_bytes(b'{"n": 1}\n')
    early = os.open(log, os.O_WRONLY | os.O_APPEND)
    real = rt.compress_into

    def compress(source, target, end=None):
        done = real(source, target, end)
        os.write(early, b'{"n": 2}\n')  # after its last read
        return done
    monkeypatch.setattr(rt, "compress_into", compress)
    monkeypatch.setattr(rt, "SHARED_ROTATE_GRACE", 0.01)
    try:
        name = rt.rotate_shared_log(log)
    finally:
        os.close(early)
    assert [r["n"] for r in lines_of(tmp_path / name)] == [1, 2]


# -- the per-folder caps never take a live run's logs ----------------------------------------------------------------

def test_the_folder_caps_never_delete_a_live_runs_logs(tmp_path):
    """A run that started earlier and still runs, idle for an hour: the per-folder cap of a new start (or gc) deleted
    its older parts - and the log the proxy still writes, which then grew unseen and was never rotated again."""
    launcher = load("agent_sandbox")
    old = 3 * 3600
    live = [aged(tmp_path / "connections-20261101-100000-live.jsonl", b"x" * 300, old),
            aged(tmp_path / "connections-20261101-100000-live.jsonl.1.gz", b"x" * 300, old),
            aged(tmp_path / "llm-traffic-20261101-100000-live.jsonl", b"x" * 300, old),
            aged(tmp_path / "llm-traffic-20261101-100000-live.jsonl.1", b"x" * 300, old)]
    ended = [aged(tmp_path / "connections-20261102-100000-ended.jsonl.gz", b"x" * 100, old),
             aged(tmp_path / "llm-traffic-20261102-100000-ended.jsonl.gz", b"x" * 100, old)]
    newer = aged(tmp_path / "connections-20261103-100000-new.jsonl", b"x" * 100, old)
    assert launcher.rotate_connection_logs(tmp_path, limit=150, live={"live"}) == [ended[0].name]
    assert launcher.rotate_traffic_logs(tmp_path, limit=50, live={"live"}) == [ended[1].name]
    assert all(path.exists() for path in [*live, newer])
    # without the live runs (an older caller): as before, the oldest first
    assert launcher.rotate_connection_logs(tmp_path, limit=150)[:1] == [live[1].name]


def test_gc_caps_pass_the_runs_that_run(tmp_path, monkeypatch):
    starts = load("starts")
    seen = {}

    class Launcher:
        @staticmethod
        def rotate_connection_logs(folder, live=None):
            seen["connections"] = live
            return []

        @staticmethod
        def rotate_traffic_logs(folder, live=None):
            seen["traffic"] = live
            return []
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state/sandbox-ai/logs/proj").mkdir(parents=True)
    monkeypatch.setattr(starts, "registered_runs", lambda: {"r1": {"log_dir": str(tmp_path / "x")}})
    monkeypatch.setattr(starts, "launcher", lambda: Launcher)
    starts.compact_logs()
    assert seen == {"connections": {"r1"}, "traffic": {"r1"}}


# -- log_days: the daily pass in the background never starts the grace; a start says it ----------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    values = {"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_DATA_HOME": str(tmp_path / "data"),
              "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
              "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "SANDBOX_AI_AUTO_COMPACT": "0"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "runtime").mkdir()
    return values


def run_cli(env, *args, extra=None):
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], capture_output=True, text=True,
                          env={**os.environ, **env, "PATH": "/usr/bin:/bin", **(extra or {})}, timeout=120)


def test_the_daily_pass_alone_never_starts_the_grace_of_log_days(env, tmp_path, monkeypatch):
    """Round 1's daily pass wrote "noticed" in the background: 14 days later the logs went, said to nobody."""
    starts = load("starts")
    monkeypatch.setattr(starts, "registered_runs", lambda: {})
    folder = tmp_path / "state/sandbox-ai/logs/proj-0123456789ab"
    part = aged(folder / "files.jsonl.20261001-100000.gz", gzip.compress(b"{}\n"), 40 * DAY)
    background = starts.expire_logs(shown=False)
    assert background["found"] == 1 and background["removed_from"] is None and background["removed"] == []
    # 20 days later the daily pass still removes nothing: nobody was told
    later = starts.expire_logs(shown=False, now=time.time() + 20 * DAY)
    assert later["removed"] == [] and part.exists()
    # the first that shows it (gc, gc --usage, a start, the app) starts the grace
    shown = starts.expire_logs(dry_run=True)
    assert shown["removed_from"] and starts.seconds(shown["removed_from"]) >= time.time() + 13 * DAY
    assert starts.expire_logs(shown=False, now=time.time() + 15 * DAY)["removed"] == [str(part)]


def test_a_round_one_notice_without_said_waits_for_its_grace_again(env, tmp_path, monkeypatch):
    starts = load("starts")
    monkeypatch.setattr(starts, "registered_runs", lambda: {})
    part = aged(tmp_path / "state/sandbox-ai/logs/proj/dns.jsonl", b"{}\n", 40 * DAY)
    (tmp_path / "state/sandbox-ai/log-days.json").write_text(json.dumps({"noticed": sd.iso(time.time() - 20 * DAY)}))
    assert starts.expire_logs(shown=False)["removed"] == [] and part.exists()


def test_a_start_says_the_age_limit_of_the_logs_once_a_day(env, tmp_path, monkeypatch):
    starts = load("starts")
    monkeypatch.setattr(starts, "registered_runs", lambda: {})
    aged(tmp_path / "state/sandbox-ai/logs/proj/dns.jsonl", b"{}\n", 40 * DAY)
    assert starts.log_days_notice() is None  # nothing found yet
    starts.expire_logs(shown=False)  # the daily pass found some
    text = starts.log_days_notice()
    assert "older than 30 days" in text and "sandbox-ai defaults set log_days 0" in text and "UTC on" in text
    assert starts.log_days_notice() is None  # once a day
    assert starts.log_days_notice(when=time.time() + DAY + 60) is not None
    assert starts.log_days_notice(when=time.time() + 15 * DAY) is None  # the grace over: gc removes them now


def test_a_start_prints_the_log_days_notice_end_to_end(tmp_path):
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    state = Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai"
    aged(state / "logs/proj-0123456789ab/dns.jsonl", b"{}\n", 40 * DAY)
    (state / "log-days.json").write_text(json.dumps({"noticed": sd.iso(time.time() - DAY)}))
    done = host.run("claude", "--no-tmux", "--no-auth", env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    said = " ".join(done.stderr.split())
    assert "logs older than 30 days" in said and "sandbox-ai defaults set log_days 0" in said
    assert "said" in json.loads((state / "log-days.json").read_text())
    again = host.run("claude", "--no-tmux", "--no-auth", env={"FAKE_PODMAN_RUN": "0:0"})
    assert "logs older than 30 days" not in " ".join(again.stderr.split())


# -- a session's supervisor.log: its newest part kept at a start ----------------------------------------------------

def test_a_long_supervisor_log_keeps_its_newest_part(tmp_path):
    sessions = load("sessions")
    log = tmp_path / "supervisor.log"
    log.write_bytes(b"".join(b"agent-sandbox: line %07d\n" % index for index in range(400_000)))  # ~10 MB
    before = log.stat().st_size
    removed = sessions.trim_log(log, keep=1 << 20, limit=4 << 20)
    data = log.read_bytes()
    assert removed > before - (1 << 20) - 100 and len(data) <= (1 << 20) + 200
    first, second = data.split(b"\n", 2)[:2]
    assert b"older output removed" in first and second.startswith(b"agent-sandbox: line ")
    assert data.endswith(b"line 0399999\n")
    assert sessions.trim_log(log, keep=1 << 20, limit=4 << 20) == 0  # small enough: as it is
    assert sessions.trim_log(tmp_path / "missing.log") == 0


# -- gc --usage: what compressing saves, estimated per kind from a sample --------------------------------------------

def test_gc_usage_estimates_compression_per_kind_end_to_end(env, tmp_path):
    folder = tmp_path / "state/sandbox-ai/logs/proj-0123456789ab"
    import random
    rng = random.Random(1)
    # an ended run's log of downloads' digests (random: barely shrinks) and a rotated part of a shared log that shrinks
    aged(folder / "connections-20261101-100000-ended.jsonl", b"".join(
        json.dumps({"event": "http", "sha256": rng.randbytes(400).hex()}).encode() + b"\n" for _ in range(3000)), 2 * DAY)
    aged(folder / "processes.jsonl.20261101-100000", b"".join(
        json.dumps({"pid": i, "command": "python -m pytest -q tests/test_x.py"}).encode() + b"\n" for i in range(30000)),
        2 * DAY)
    usage = json.loads(run_cli(env, "gc", "--usage", "--json").stdout)["would_free"]["compress"]
    kinds = usage["kinds"]
    assert set(kinds) == {"connections", "older parts"}
    assert kinds["connections"]["ratio"] < 2.5 < 8 < kinds["older parts"]["ratio"]
    before = sum(p.stat().st_blocks * 512 for p in folder.iterdir())
    done = run_cli(env, "gc")
    assert done.returncode == 0, done.stderr
    after = sum(p.stat().st_blocks * 512 for p in folder.iterdir())
    assert abs((before - after) - usage["saves"]) < 0.15 * (before - after)
    text = " ".join(run_cli(env, "gc", "--usage").stdout.split())
    assert "quarter" not in text


# -- the LLM traffic log: a request that continues an earlier one written as a delta, put together by its readers ----

traffic = load("llm_traffic")
CLAUDE_TOOLS = [{"name": f"Tool{index}", "description": "does things " * 60, "input_schema": {"type": "object"}}
                for index in range(20)]


def claude_request(turns, system="You are Claude Code. " * 300):
    """A request as Claude Code sends it (messages first, then system and tools): TURNS tool rounds so far."""
    messages = [{"role": "user", "content": [{"type": "text", "text": "Read the files one by one."}]}]
    for turn in range(turns):
        messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{turn}", "name": "Read",
                                                           "input": {"file_path": f"/w/f{turn}.py"}}]})
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{turn}",
                                                      "content": f"file {turn}\n" + "line of code\n" * 300}]})
    return {"model": "claude-sonnet-4-5", "messages": messages, "system": [{"type": "text", "text": system}],
            "tools": CLAUDE_TOOLS, "max_tokens": 32000, "stream": True}


def traffic_item(document, api="anthropic"):
    return {"time": "2026-11-16T10:00:00.000Z", "source": "provider", "endpoint": None, "host": "api.anthropic.com",
            "url": "https://api.anthropic.com/v1/messages", "api": api, "status": 200, "ms": 900, "first_byte_ms": 80,
            "bytes_out": 100, "bytes_in": 200, "done": {}, "document": document, "answer": rt.LlmAnswer(api)}


def traffic_log(tmp_path, documents, monkeypatch=None, deltas=True, part=None, api="anthropic"):
    """DOCUMENTS through the proxy's LlmTraffic and the supervisor's LlmTrafficFile: the log's path."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    path = tmp_path / "llm-traffic-20261116-100000-run-1.jsonl"
    writer = rt.LlmTrafficFile(read_fd, path, **({"part": part} if part else {}))
    thread = threading.Thread(target=writer.serve, daemon=True)
    thread.start()
    log = rt.LlmTraffic({"mode": "all", "run": "run-1", "record_limit": 16 << 20}, write_fd)
    if not deltas:
        log.deltas = None
    for document in documents:
        while True:  # (never a record dropped for a full queue: "lost_before" would differ)
            with log.lock:
                if len(log.queue) < rt.LLM_TRAFFIC_QUEUE - 1:
                    break
            time.sleep(0.005)
        assert log.submit(traffic_item(json.loads(json.dumps(document)), api))
    log.close()
    os.close(write_fd)
    thread.join(10)  # (serve() ends once the parts it rotated are compressed)
    return path


def test_a_request_that_continues_another_is_written_as_what_is_new():
    deltas = rt.TrafficDeltas()
    first = {"n": 1, "api": "anthropic", "digests": ["s", "a"], "request": claude_request(1)}
    written, memory = deltas.encode(first)
    assert written is first and memory["depth"] == 0
    deltas.written(1, memory, 100)
    second = {"n": 2, "api": "anthropic", "digests": ["s", "a", "b", "c"], "request": claude_request(2)}
    written, memory = deltas.encode(second)
    assert written["delta"] == {"of": 1, "key": "messages", "kept": 3, "same": ["system", "tools"], "digests": 2,
                                "order": ["model", "messages", "system", "tools", "max_tokens", "stream"]}
    assert written["request"]["messages"] == second["request"]["messages"][3:] and written["digests"] == ["b", "c"]
    assert "system" not in written["request"] and written["request"]["model"] == "claude-sonnet-4-5"
    assert list(written) == ["n", "api", "delta", "digests", "request"] and second["request"]["system"]  # unchanged
    expander = traffic.Expander()
    assert expander.expand(first) == first and expander.expand(written) == second
    # a record not written (forgotten), another API, a chain at its end, the bytes after which all start again
    deltas.forget(1)
    assert deltas.encode(second)[0] is second
    deltas = rt.TrafficDeltas(chain=2)  # a chain of deltas is at most 1 long here: the third continues the first
    for number in (1, 2, 3):
        record = {"n": number, "api": "anthropic", "request": claude_request(number)}
        written, memory = deltas.encode(record)
        deltas.written(number, memory, 10)
        assert written.get("delta", {}).get("of") == (None, 1, 1)[number - 1]
    assert "delta" not in deltas.encode({"n": 4, "api": "chat", "request": claude_request(4)})[0]
    deltas = rt.TrafficDeltas(reset=50)
    written, memory = deltas.encode({"n": 1, "api": "anthropic", "request": claude_request(1)})
    deltas.written(1, memory, 60)
    assert "delta" not in deltas.encode({"n": 2, "api": "anthropic", "request": claude_request(2)})[0]


def test_deltas_end_to_end_through_the_proxy_and_the_supervisor(tmp_path):
    documents = [claude_request(turns) for turns in range(40)]
    whole = traffic_log(tmp_path / "whole", documents, deltas=False)
    small = traffic_log(tmp_path / "small", documents)
    assert small.stat().st_size * 8 < whole.stat().st_size  # ~ the square of the conversation's length before
    for expected, record in zip(documents, traffic.read_records([small])):
        assert record["request"] == expected and "delta" not in record
    plain = list(traffic.read_records([whole]))
    assert [r["digests"] for r in traffic.read_records([small])] == [r["digests"] for r in plain]
    # every way of reading: the CLI as JSON, one request, the tail, a summary - the same as of the whole records
    for argv in (["--json"], ["--request", "17", "--json"], ["--tail", "3", "--json"], ["--summary", "--json"],
                 ["--request", "30", "--full"], ["--grep", "f38.py", "--summary", "--json"]):
        outputs = []
        for folder in ("whole", "small"):
            lines = []
            traffic.cli(["--log-dir", str(tmp_path / folder), *argv], out=lines.append)
            outputs.append("\n".join(lines))
        assert outputs[0] and outputs[0] == outputs[1], argv  # (pytest's diff of two 4 MB texts takes minutes)
    # --grep finds the requests whose line holds the text: with deltas, where it is new (not every later request)
    found = []
    traffic.cli(["--log-dir", str(tmp_path / "small"), "--grep", "f10.py", "--summary", "--json"], out=found.append)
    assert [json.loads(line)["n"] for line in found] == [12]


def test_a_delta_whose_request_is_not_in_the_log_says_so(tmp_path):
    documents = [claude_request(turns) for turns in range(3)]
    path = traffic_log(tmp_path, documents)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[1:]))  # the first (whole) record gone: a part removed
    records = list(traffic.read_records([path]))
    assert records[0]["delta"]["missing"] is True
    assert "1 earlier items" in records[0]["request"]["messages"][0]["sandbox_ai_omitted"]
    assert records[1]["request"]["messages"][0]["sandbox_ai_omitted"]  # (it continues one that misses its start)


def test_deltas_across_rotated_and_compressed_parts_tail_and_follow(tmp_path):
    documents = [claude_request(turns) for turns in range(30)]
    path = traffic_log(tmp_path, documents, part=40_000)
    parts = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("llm-traffic"))
    assert any(name.endswith(".gz") for name in parts) and not any(name.endswith((".1", ".2")) for name in parts)
    files = traffic.run_files(tmp_path)["run-1"]["paths"]
    kept = list(traffic.read_records(files))
    first = kept[0]["n"]
    assert [r["request"] for r in kept] == documents[first - 1:]
    tail = traffic.tail_records(files, 2)
    assert [r["request"] for r in tail] == documents[-2:]
    assert all("delta" not in r for r in tail)
    follow = traffic.Follow(tmp_path, ["run-1"])
    follow.mark()
    expander = traffic.Expander(traffic.finder(files))
    for record in traffic.tail_records(files, 1, expander):
        pass
    with open(path, "ab") as stream:  # the next request, as a delta of the last one
        deltas = rt.TrafficDeltas()
        last = kept[-1]
        deltas.written(last["n"], deltas.encode(last)[1], 10)
        record = {**last, "n": last["n"] + 1, "digests": last["digests"] + ["x"],
                  "request": claude_request(30)}
        stream.write(json.dumps(deltas.encode(record)[0], separators=(",", ":")).encode() + b"\n")
    new = [expander.expand(r) for r in follow.read()]
    assert len(new) == 1 and new[0]["request"] == claude_request(30)
    follow.close()


def test_llm_traffic_deltas_end_to_end_through_the_entry_point(tmp_path, env):
    documents = [claude_request(turns) for turns in range(12)]
    traffic_log(tmp_path / "logs", documents)
    done = run_cli(env, "llm", "traffic", "--log-dir", str(tmp_path / "logs"), "--request", "12", "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["record"]["request"] == documents[-1]


def test_redaction_never_breaks_a_record_of_the_llm_traffic_log(tmp_path):
    """The line was redacted as text: "--password-store=basic\\"" (a file the agent read, in every later request of
    its conversation) lost the backslash of its escaped quote - the line was no JSON any more, skipped by every reader:
    320 of 410 requests of a measured Claude Code run. And the record's own "session" (the start's name) was
    redacted as a secret's key."""
    key = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0"
    secret_file = 'args = ["--no-sandbox", "--password-store=basic", "--user-data-dir=/x"]\nexport password=hunter2\\'
    document = claude_request(1)
    document["messages"].append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t9", "content": json.dumps(secret_file) + f" key {key}"},
        {"type": "text", "text": '{"api_key": "hunter2-pass", "note": "x"}'}]})
    read_fd, write_fd = os.pipe()
    path = tmp_path / "llm-traffic-20261116-100000-run-1.jsonl"
    writer = rt.LlmTrafficFile(read_fd, path)
    thread = threading.Thread(target=writer.serve, daemon=True)
    thread.start()
    log = rt.LlmTraffic({"mode": "all", "run": "run-1", "session": "my-session"}, write_fd, lambda: ("A1b2C3d4E5f6",))
    assert log.submit(traffic_item(document))
    assert log.submit(traffic_item({**document, "messages": document["messages"] + [{"role": "user", "content": "go"}]}))
    log.close()
    os.close(write_fd)
    thread.join(10)
    text = path.read_text()
    records = [json.loads(line) for line in text.splitlines()]  # every line JSON
    assert len(records) == 2 and records[0]["session"] == "my-session"
    assert "hunter2" not in text and key not in text and "A1b2C3d4E5f6" not in text and "basic" not in text
    whole = list(traffic.read_records([path]))
    assert len(whole) == 2 and whole[1]["request"]["messages"][-1] == {"role": "user", "content": "go"}
    assert "--password-store=[redacted]" in whole[1]["request"]["messages"][-2]["content"][0]["content"]


def test_summed_reads_count_as_the_files_read_as_before(tmp_path):
    """Round 1 counted a folder's summed record by its "reads" (every close): the same loop's statistics were 120x
    what the per-file log said before (330716 vs 2734 in 60 s). A read counts once per file and window, as before."""
    logs = load("logs")
    lines = [{"action": "read", "path": "/w/a/x.py"}, {"action": "read", "path": "/w/a/y.py"},  # before
             {"action": "read", "folder": "/w/a", "reads": 500, "files": 2, "names": ["x.py", "y.py"]},  # now
             {"action": "read", "reads": 7, "note": "in more than 4096 folders"}, {"action": "write", "path": "/w/a/x.py"}]
    (tmp_path / "files.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    summary = logs.summarize(tmp_path)
    assert summary["actions"] == {"read": 2 + 2 + 7, "write": 1} and summary["file_events"] == 12
    assert dict(summary["busiest_directories"])["/w/a"] == 2 + 2 + 1


def test_reads_beyond_the_folders_a_window_tells_apart_keep_where_they_were(monkeypatch):
    """A window with more folders than FILE_READ_FOLDERS (a node_modules tree): 95904 of 100000 reads were one record
    without a folder - the busiest folders never named them. Now summed below their first two folders."""
    monkeypatch.setattr(rt, "FILE_READ_FOLDERS", 100)
    reads = rt.FileReads()
    for index in range(1000):
        reads.add(f"/work/node_modules/pkg{index}/index.js")
    reads.add("/tmp/x/y/z.txt")
    for index in range(rt.FILE_READ_OVERFLOW + 5):
        reads.add(f"/other{index}/a/b.txt")
    out = []
    reads.flush(out.append)
    beyond = {r.get("folder"): r["reads"] for r in out if r.get("note")}
    assert beyond["/work/node_modules"] == 900 and beyond["/tmp/x"] == 1
    assert beyond[None] == 7 and len(beyond) == rt.FILE_READ_OVERFLOW + 1  # (two of the 64 taken above)
    assert sum(r["reads"] for r in out) == 1001 + rt.FILE_READ_OVERFLOW + 5


def test_a_start_never_caps_away_a_running_runs_connection_log_end_to_end(tmp_path):
    """The real ./sandbox-ai start (Podman faked): over 1 GiB of connection logs in the folder, a run that started
    earlier and still runs (registered, idle for hours) keeps its log and its older parts; an ended run's go."""
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    logs = tmp_path / "logs"
    logs.mkdir()

    def sparse(name, size):
        path = logs / name
        with open(path, "wb") as stream:
            stream.truncate(size)  # (no disk taken: the caps count the size)
        os.utime(path, (time.time() - 5 * 3600,) * 2)
        return path
    live = [sparse("connections-20261101-100000-live1.jsonl", 700 << 20),
            sparse("connections-20261101-100000-live1.jsonl.1.gz", 700 << 20)]
    ended = sparse("connections-20261102-100000-old1.jsonl.gz", 1100 << 20)
    kept = sparse("connections-20261103-100000-old2.jsonl.gz", 100 << 20)
    runs = Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai/runs/live1"
    runs.mkdir(parents=True)
    (runs / "run.json").write_text(json.dumps({"run": "live1", "pid": os.getpid(), "log_dir": str(logs),
                                               "agent": "claude", "name": "earlier"}))
    done = host.run("claude", "--no-tmux", "--no-auth", "--log-dir", str(logs), env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    assert "deleted the old connection log connections-20261102-100000-old1.jsonl.gz" in done.stderr
    assert all(path.exists() for path in [*live, kept]) and not ended.exists()


def test_the_tail_of_a_compressed_log_of_deltas_reads_back_as_far_as_needed(tmp_path):
    documents = [claude_request(turns) for turns in range(20)]
    path = traffic_log(tmp_path, documents)
    packed = path.with_name(path.name + ".gz")  # as gc compresses an ended run's log
    packed.write_bytes(gzip.compress(path.read_bytes()))
    path.unlink()
    assert [r["request"] for r in traffic.tail_records([packed], 3)] == documents[-3:]
    whole = tmp_path / "whole"
    plain = traffic_log(whole, documents, deltas=False)
    plain.with_name(plain.name + ".gz").write_bytes(gzip.compress(plain.read_bytes()))
    plain.unlink()
    assert [r["request"] for r in traffic.tail_records([plain.with_name(plain.name + ".gz")], 3)] == documents[-3:]


def test_the_trim_of_supervisor_log_is_set_by_the_environment():
    sessions = load("sessions")
    assert sessions.supervisor_log_limits({}) == (8 << 20, 4 << 20)
    assert sessions.supervisor_log_limits({"SANDBOX_AI_SUPERVISOR_LOG_MIB": "64"}) == (64 << 20, 32 << 20)
    assert sessions.supervisor_log_limits({"SANDBOX_AI_SUPERVISOR_LOG_MIB": "0"}) == (None, None)
    assert sessions.supervisor_log_limits({"SANDBOX_AI_SUPERVISOR_LOG_MIB": "lots"}) == (8 << 20, 4 << 20)
    assert sessions.trim_log("/nonexistent", None, None) == 0
