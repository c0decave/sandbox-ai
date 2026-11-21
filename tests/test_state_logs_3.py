"""Review 3 of sandbox-ai's logs (docs/reviews/state-logs-review-3.md): what the first two reviews left open or
missed - the LLM traffic log's deltas across the runs of a session, the readers after a rotation (the approvals'
"blocked" list, the timeline, the canary log's links), redaction of text that is JSON, a rotation cut short by a
crash, the rotation on a full disk."""
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
    spec = importlib.util.spec_from_file_location(f"state_logs_3_{name}", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime")
sd = load("statedirs")
traffic = load("llm_traffic")


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


def lines_of(path):
    data = gzip.decompress(path.read_bytes()) if path.name.endswith(".gz") else path.read_bytes()
    return [json.loads(line) for line in data.splitlines()]


# -- the LLM traffic log: a request written as a delta continues a request of its own run only ----------------------

TOOLS = [{"name": f"Tool{index}", "description": "does things " * 60, "input_schema": {"type": "object"}}
         for index in range(20)]


def claude_request(turns, tag="f", system="You are Claude Code. " * 300):
    """A request as Claude Code sends it (messages first, then system and tools): TURNS tool rounds so far."""
    messages = [{"role": "user", "content": [{"type": "text", "text": f"Read the files one by one ({tag})."}]}]
    for turn in range(turns):
        messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{turn}", "name": "Read",
                                                           "input": {"file_path": f"/w/{tag}{turn}.py"}}]})
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{turn}",
                                                      "content": f"file {tag}{turn}\n" + "line of code\n" * 300}]})
    return {"model": "claude-sonnet-4-5", "messages": messages, "system": [{"type": "text", "text": system}],
            "tools": TOOLS, "max_tokens": 32000, "stream": True}


def traffic_item(document, api="anthropic"):
    return {"time": "2026-11-16T10:00:00.000Z", "source": "provider", "endpoint": None, "host": "api.anthropic.com",
            "url": "https://api.anthropic.com/v1/messages", "api": api, "status": 200, "ms": 900, "first_byte_ms": 80,
            "bytes_out": 100, "bytes_in": 200, "done": {}, "document": document, "answer": rt.LlmAnswer(api)}


def traffic_log(folder, documents, run="run-1", stamp="20261116-100000", session=None, part=None, api="anthropic"):
    """DOCUMENTS through the proxy's LlmTraffic and the supervisor's LlmTrafficFile: the log's path."""
    folder.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    path = folder / f"llm-traffic-{stamp}-{run}.jsonl"
    writer = rt.LlmTrafficFile(read_fd, path, **({"part": part} if part else {}))
    thread = threading.Thread(target=writer.serve, daemon=True)
    thread.start()
    log = rt.LlmTraffic({"mode": "all", "run": run, "record_limit": 16 << 20,
                         **({"session": session} if session else {})}, write_fd)
    for document in documents:
        while True:  # (never a record dropped for a full queue)
            with log.lock:
                if len(log.queue) < rt.LLM_TRAFFIC_QUEUE - 1:
                    break
            time.sleep(0.005)
        assert log.submit(traffic_item(json.loads(json.dumps(document)), api))
    log.close()
    os.close(write_fd)
    thread.join(10)
    return path


def test_the_tail_over_a_sessions_runs_never_joins_a_request_with_another_runs(tmp_path):
    """Each run numbers its requests from 1. The tail over the two runs of a session read the later run whole and of
    the earlier one its last requests - and took the later run's request of that number for the one a delta of the
    earlier run continues: five requests shown with the other conversation's messages, nothing said."""
    first = [claude_request(turns, "a") for turns in range(15)]
    second = [claude_request(turns, "b") for turns in range(20)]
    paths = [traffic_log(tmp_path, first, "run-a", "20261116-100000"),
             traffic_log(tmp_path, second, "run-b", "20261116-110000")]
    tail = traffic.tail_records(paths, 25)
    assert [(r["run"], r["n"]) for r in tail] == [("run-a", n) for n in range(11, 16)] + \
        [("run-b", n) for n in range(1, 21)]
    assert [r["request"] for r in tail] == (first + second)[-25:]
    assert all("delta" not in r for r in tail)
    # the last request of each run alone, and all of both
    assert [r["request"] for r in traffic.tail_records(paths, 21)] == (first + second)[-21:]
    assert [r["request"] for r in traffic.read_records(paths)] == first + second


def test_a_request_whose_start_was_lost_is_not_completed_from_the_run_before(tmp_path):
    """Read in order, the earlier run's requests were still remembered by their numbers when the later run's delta
    asked for one its own log does not hold (a line lost on a full disk, a part removed)."""
    first = [claude_request(turns, "a") for turns in range(5)]
    second = [claude_request(turns, "b") for turns in range(5)]
    paths = [traffic_log(tmp_path, first, "run-a", "20261116-100000"),
             traffic_log(tmp_path, second, "run-b", "20261116-110000")]
    lines = paths[1].read_bytes().splitlines(keepends=True)
    paths[1].write_bytes(b"".join(lines[1:]))  # run-b's first request (whole) is gone
    records = list(traffic.read_records(paths))
    assert [r["request"] for r in records[:5]] == first
    assert records[5]["run"] == "run-b" and records[5]["delta"]["missing"] is True
    assert "(a)" not in json.dumps(records[5]["request"]["messages"])  # (nothing of the other run's conversation)
    for record in traffic.tail_records(paths, 4):  # (each continues the one that misses its start)
        assert record["run"] == "run-b" and record["request"]["messages"][0]["sandbox_ai_omitted"]
        assert "(a)" not in json.dumps(record["request"]["messages"])
    # a delta never continues a later request (a damaged "of")
    expander = traffic.Expander()
    expander.expand({"version": 1, "n": 9, "request": claude_request(3, "x")})
    joined = expander.expand({"version": 1, "n": 4, "delta": {"of": 9, "key": "messages", "kept": 3},
                              "request": {"messages": [{"role": "user", "content": "new"}]}})
    assert joined["delta"]["missing"] is True and len(joined["request"]["messages"]) == 2


def test_llm_traffic_of_a_session_with_two_runs_end_to_end(tmp_path, env):
    """Through ./sandbox-ai: `llm traffic --session NAME --tail N --json` over both runs of a session."""
    logs = tmp_path / "logs"
    first = [claude_request(turns, "a") for turns in range(15)]
    second = [claude_request(turns, "b") for turns in range(20)]
    traffic_log(logs, first, "run-a", "20261116-100000", session="work")
    traffic_log(logs, second, "run-b", "20261116-110000", session="work")
    session = Path(env["XDG_STATE_HOME"]) / "sandbox-ai/sessions/work"
    session.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({
        "name": "work", "agent": "claude", "options": ["--log-dir", str(logs)], "agent_args": [],
        "cwd": str(tmp_path), "run": "run-b"}))
    done = run_cli(env, "llm", "traffic", "--session", "work", "--tail", "25", "--json")
    assert done.returncode == 0, done.stderr
    shown = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r["request"] for r in shown] == (first + second)[-25:]


# -- deltas of a conversation that does not only grow -----------------------------------------------------------------

def chat_request(messages):
    return {"model": "qwen3.6", "messages": messages, "tools": TOOLS, "stream": True}


def tool_round(index, size=3000):
    return [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{index}", "type": "function",
                                                                    "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{index}", "content": f"file {index}\n" + f"line {index}\n" * (size // 8)}]


def moving_conversations():
    """{what: [request]}: a harness that drops its oldest round at a small context, one that clears old tool results
    one by one, one whose first message changes with every request."""
    system = {"role": "system", "content": "You are a coding agent. " * 300}
    window, cleared, clock = [], [], []
    messages = [system, {"role": "user", "content": "go"}]
    for index in range(40):
        kept = messages[:2] + messages[2:][-16:]
        window.append(chat_request(json.loads(json.dumps(kept))))
        edited = json.loads(json.dumps(messages))
        for message in [m for m in edited if m["role"] == "tool"][:-4]:
            message["content"] = "[old tool result cleared]"
        cleared.append(chat_request(edited))
        timed = json.loads(json.dumps(messages))
        timed[0]["content"] += f" It is 10:{index:02d}."
        clock.append(chat_request(timed))
        messages += tool_round(index)
    return {"window": window, "cleared": cleared, "clock": clock}


def test_a_request_whose_items_moved_leaves_out_those_its_base_holds(tmp_path):
    """Only a common beginning was left out: a harness that drops its oldest messages (a local model's small context),
    clears old tool results, or changes its first message with every request wrote nearly every request whole
    (measured x1.0-2.1 against x9-17 now)."""
    for what, documents in moving_conversations().items():
        path = traffic_log(tmp_path / what, documents, api="chat")
        whole = sum(len(json.dumps(document, separators=(",", ":"))) for document in documents)
        assert path.stat().st_size * 4 < whole, (what, path.stat().st_size, whole)
        records = lines_of(path)
        assert any(isinstance(r.get("delta", {}).get("items"), list) for r in records), what
        assert [r["request"] for r in traffic.read_records([path])] == documents, what
        assert [r["request"] for r in traffic.tail_records([path], 3)] == documents[-3:], what


def test_the_layout_of_a_delta_happy_sad_edge():
    deltas = rt.TrafficDeltas()
    first = {"n": 1, "api": "chat", "request": chat_request(
        [{"role": "system", "content": "s" * 400}, {"role": "user", "content": "go"}, *tool_round(0), *tool_round(1)])}
    deltas.written(1, deltas.encode(first)[1], 10)
    # the oldest round dropped, a new one added: the first two, then the base's items 4-5, then two new ones
    second = {"n": 2, "api": "chat", "request": chat_request(
        [*first["request"]["messages"][:2], *tool_round(1), *tool_round(2)])}
    written, _ = deltas.encode(second)
    assert written["delta"]["kept"] == 2 and written["delta"]["items"] == [[4, 2], 2]
    assert written["request"]["messages"] == tool_round(2)
    expander = traffic.Expander()
    expander.expand(first)
    assert expander.expand(written)["request"] == second["request"]
    # a short item (below LLM_DELTA_ITEM) is written again, never a reference; a plain continuation has no layout
    assert rt.LLM_DELTA_ITEM > len(json.dumps({"role": "user", "content": "go"}))
    third = {"n": 3, "api": "chat", "request": chat_request([*first["request"]["messages"], *tool_round(5)])}
    assert "items" not in deltas.encode(third)[0]["delta"]
    # the reader: a layout that does not fit the record or the base (damaged) is not followed - the items as written
    laid_out = traffic.Expander.laid_out
    earlier, items = ["a", "b", "c"], ["x", "y"]
    assert laid_out([[1, 2], 2], earlier, items, {}) == ["b", "c", "x", "y"]
    assert laid_out([1, [0, 1], 1], earlier, items, {}) == ["x", "a", "y"]
    assert laid_out(None, earlier, items, {}) == items and laid_out([], earlier, [], {}) == []
    for damaged in ([[2, 5], 2], [[-1, 1], 2], [1], [3], ["x"], [[0, 1, 2]], [True, 1], [[0, True], 2]):
        assert laid_out(damaged, earlier, items, {}) == items, damaged


# -- the timeline after it rotated: --since, the tail, a run's records ----------------------------------------------

act = load("activity")


def timeline(folder, now=None):
    """A log folder whose timeline rotated twice: the oldest part compressed (as gc leaves it), the next one raw, the
    current file with two records. Ten records each of the runs old-1 (9 days ago), mid-1 (3 days ago); now-1 today."""
    now = time.time() if now is None else now
    folder.mkdir(parents=True, exist_ok=True)

    def records(run, age, count):
        return [act.finalize(act.raw("command", f"{run} step {index}", act.stamp(now - age + index), {},
                                     key=f"k{index}", source="claude"), run, None, "claude", act.Redactor())
                for index in range(count)]

    def write(name, items, age, packed=False):
        data = "".join(json.dumps(item) + "\n" for item in items).encode()
        path = folder / name
        path.write_bytes(gzip.compress(data) if packed else data)
        os.utime(path, (now - age, now - age))
    stamp = lambda age: time.strftime("%Y%m%d-%H%M%S", time.gmtime(now - age))  # noqa: E731
    write(f"activity.jsonl.{stamp(8 * DAY)}.gz", records("old-1", 9 * DAY, 10), 8 * DAY, packed=True)
    write(f"activity.jsonl.{stamp(2 * DAY)}", records("mid-1", 3 * DAY, 10), 2 * DAY)
    write("activity.jsonl", records("now-1", 60, 2), 0)
    return folder


def shown(argv):
    out = []
    assert act.cli([*argv, "--json"], out=out.append) == 0
    return [json.loads(line)["summary"] for line in out]


def test_since_reads_the_older_parts_of_the_timeline_that_reach_back_to_it(tmp_path):
    """`sandbox-ai activity --since 5d` read the current file only: at a busy project's rate that is the last four
    days (256 MiB), and the answer was silently shorter than asked."""
    logs = timeline(tmp_path / "logs")
    assert shown(["-l", str(logs), "--since", "5d"]) == [f"mid-1 step {n}" for n in range(10)] + \
        ["now-1 step 0", "now-1 step 1"]
    assert len(shown(["-l", str(logs), "--since", "30d"])) == 22
    assert shown(["-l", str(logs), "--since", "1h"]) == ["now-1 step 0", "now-1 step 1"]
    assert shown(["-l", str(logs), "--since", "30d", "--grep", "old-1 step 7"]) == ["old-1 step 7"]
    assert shown(["-l", str(logs), "--since", "30d", "--tail", "13"])[0] == "old-1 step 9"


def test_the_tail_goes_on_into_the_older_parts_after_a_rotation(tmp_path, monkeypatch):
    """Just after the timeline rotated its current file is nearly empty: the app's Activity page (the last 5000
    events) showed two events of a run that had made thousands."""
    logs = timeline(tmp_path / "logs")
    assert shown(["-l", str(logs), "--tail", "5"]) == ["mid-1 step 7", "mid-1 step 8", "mid-1 step 9", "now-1 step 0",
                                                      "now-1 step 1"]
    assert shown(["-l", str(logs), "--tail", "2"]) == ["now-1 step 0", "now-1 step 1"]
    # never further back than the newest older part without --since (a filter that matches little reads no month)
    assert len(shown(["-l", str(logs), "--tail", "500"])) == 12
    assert shown(["-l", str(logs), "--tail", "3", "--kind", "command", "--grep", "old-1"]) == []
    assert shown(["-l", str(logs), "--tail", "3", "--kind", "command", "--grep", "old-1", "--since", "30d"]) == [
        f"old-1 step {n}" for n in (7, 8, 9)]
    monkeypatch.setattr(act, "TAIL_BACK", 10)  # a current file of its usual size: the tail is what it holds
    assert shown(["-l", str(logs), "--tail", "5"]) == ["now-1 step 0", "now-1 step 1"]
    monkeypatch.undo()
    # a run's records wherever they are (one pass over each part, only the lines that name it parsed), and its tail
    assert shown(["-l", str(logs), "old-1"]) == [f"old-1 step {n}" for n in range(10)]
    assert shown(["-l", str(logs), "mid-1", "--tail", "2"]) == ["mid-1 step 8", "mid-1 step 9"]


def test_everything_names_the_older_parts_it_did_not_read(tmp_path, capsys):
    """Without --since, --tail or a run the current file is shown (as before); that older parts exist is said."""
    logs = timeline(tmp_path / "logs")
    assert shown(["-l", str(logs)]) == ["now-1 step 0", "now-1 step 1"]
    assert "2 older parts" in capsys.readouterr().err
    (logs / "activity.jsonl").unlink()  # only older parts left (the current one removed by its age)
    assert len(shown(["-l", str(logs), "--tail", "50"])) == 10 and len(shown(["-l", str(logs), "--since", "30d"])) == 20


def test_the_timeline_since_end_to_end(tmp_path, env):
    logs = timeline(tmp_path / "logs")
    done = run_cli(env, "activity", "-l", str(logs), "--since", "5d", "--json")
    assert done.returncode == 0, done.stderr
    assert [json.loads(line)["run"] for line in done.stdout.splitlines()] == ["mid-1"] * 10 + ["now-1"] * 2
    done = run_cli(env, "activity", "-l", str(logs), "--tail", "4", "--json")
    assert [json.loads(line)["run"] for line in done.stdout.splitlines()] == ["mid-1"] * 2 + ["now-1"] * 2


def test_which_parts_name_a_run_is_remembered_beside_them(tmp_path):
    """A run's records were looked for in every older part, every time (11 s over 30 days of a busy project): what
    each part names is found once and kept in .activity-parts.json; then only the parts that hold the run are read."""
    logs = timeline(tmp_path / "logs")
    folder_time = os.stat(logs).st_mtime_ns
    parts = act.older_parts(logs)
    assert [p.name for p in act.parts_with(logs, parts, "mid-1")] == [parts[1].name]
    index = json.loads((logs / act.PARTS_INDEX).read_text())
    assert sorted(index["parts"].values()) == [["mid-1"], ["old-1"]]
    assert os.stat(logs).st_mtime_ns == folder_time  # (reading never makes the folder look written: gc's age)
    assert os.stat(logs / act.PARTS_INDEX).st_mtime <= time.time() - 2 * DAY + 5
    assert os.stat(logs / act.PARTS_INDEX).st_mode & 0o777 == 0o600
    # the part that does not name the run is not opened again
    opened = []
    real = act.part_stream
    act.part_stream = lambda path: opened.append(Path(path).name) or real(path)
    try:
        assert shown(["-l", str(logs), "old-1", "--tail", "3"]) == [f"old-1 step {n}" for n in (7, 8, 9)]
    finally:
        act.part_stream = real
    assert opened == [parts[0].name]
    assert act.parts_with(logs, parts, "nobody") == []
    # sad: a damaged index is made again; one that cannot be written is done without; a part that is gone is dropped
    (logs / act.PARTS_INDEX).write_text("{not json")
    assert [p.name for p in act.parts_with(logs, parts, "old-1")] == [parts[0].name]
    assert json.loads((logs / act.PARTS_INDEX).read_text())["version"] == 1
    (logs / act.PARTS_INDEX).write_text(json.dumps({"parts": {"20200101-000000": ["gone"], "x": 5}}))
    act.parts_with(logs, parts, "old-1")
    assert "20200101-000000" not in json.loads((logs / act.PARTS_INDEX).read_text())["parts"]
    (logs / act.PARTS_INDEX).unlink()
    os.chmod(logs, 0o500)
    try:
        assert [p.name for p in act.parts_with(logs, parts, "mid-1")] == [parts[1].name]
        assert not (logs / act.PARTS_INDEX).exists() or os.geteuid() == 0
    finally:
        os.chmod(logs, 0o700)
    # edge: a part that cannot be read whole stays in (never hidden by the index); the compressed part of a raw one
    # keeps its entry (the same TIME)
    broken = logs / "activity.jsonl.20200202-000000.gz"
    broken.write_bytes(b"\x1f\x8b not gzip")
    assert broken in act.parts_with(logs, act.older_parts(logs), "mid-1")
    broken.unlink()
    assert act.part_names(parts[1]) == {"mid-1"} and act.part_names(logs / "absent") is None
    packed = parts[1].with_name(parts[1].name + ".gz")
    packed.write_bytes(gzip.compress(parts[1].read_bytes()))
    parts[1].unlink()
    assert shown(["-l", str(logs), "mid-1", "--tail", "1"]) == ["mid-1 step 9"]


# -- the approvals' "blocked" list after a log rotated -----------------------------------------------------------------

ap = load("approvals")


def blocked_logs(tmp_path, monkeypatch):
    """A run's logs with a refusal in each, all rotated as the run rotates them (the connection log held open and
    emptied in place, the shared logs renamed away), then one newer refusal in the connection log."""
    logs = tmp_path / "logs"
    logs.mkdir()
    connections = logs / "connections-20261116-100000-claude-1.jsonl"
    lines = {
        connections.name: {"time": "2026-11-16T10:00:10Z", "event": "denied", "destination": "198.51.100.3:443",
                           "reason": "not allowed by the network options"},
        "egress-denied.jsonl": {"time": "2026-11-16T10:00:11Z", "host": "pypi.org", "reason": "not in allowed hosts",
                                "sandbox": {"run": "claude-1"}},
        "tools.jsonl": {"time": "2026-11-16T10:00:12Z", "agent": "claude", "tool": "Bash", "decision": "deny",
                        "rule": "no-rm", "ruleset": "rules.json", "reason": "denied by rule no-rm"},
        "mcp-calls.jsonl": {"time": "2026-11-16T10:00:13Z", "server": "files", "tool": "delete_file",
                            "arguments": "{}", "decision": "deny", "rule": "deletes", "ruleset": "policy",
                            "reason": "the policy refuses deletes"}}
    for name, record in lines.items():
        (logs / name).write_text(json.dumps(record) + "\n")
    monkeypatch.setattr(rt, "SHARED_ROTATE_GRACE", 0)
    assert sorted(rt.rotate_logs(logs, held=connections.name, part=10)) == sorted(lines)
    assert connections.stat().st_size == 0 and not (logs / "tools.jsonl").exists()
    with open(connections, "a") as stream:
        stream.write(json.dumps({"time": "2026-11-16T10:05:00Z", "event": "denied", "destination": "203.0.113.9:22",
                                 "reason": "not allowed by the network options"}) + "\n")
    return logs


def test_the_blocked_list_keeps_what_was_refused_before_a_log_rotated(tmp_path, monkeypatch):
    """The list read the current part of each log only: the moment the run rotated its connection log (128 MiB) or a
    shared log was renamed away, every refusal in it left the list of the run that still runs."""
    logs = blocked_logs(tmp_path, monkeypatch)
    record = {"run": "claude-1", "directory": str(tmp_path / "run"), "log_dir": str(logs),
              "started": "2026-11-16T10:00:00Z"}
    found = ap.blocked_of_run("claude-1", record)
    assert sorted(entry["what"] for entry in found) == [
        "MCP tool files/delete_file with {}", "claude tool Bash", "connection to 198.51.100.3:443",
        "connection to 203.0.113.9:22", "lookup of pypi.org"]
    # bounded as before: the newest TAIL bytes of a log and its parts together, the current part first
    short = [entry["what"] for entry in ap.blocked_of_run("claude-1", record, tail=150)]
    assert "connection to 203.0.113.9:22" in short and "connection to 198.51.100.3:443" not in short
    # sad and edge: no older parts, a part that is damaged or a link, a part before the run's start
    path = logs / "egress-denied.jsonl"
    assert ap.tail_records(logs / "absent.jsonl", older=True) == []
    part = ap.older_parts(path)[0]
    assert part.name.startswith("egress-denied.jsonl.") and part.name.endswith(".gz")
    assert [r["host"] for r in ap.tail_records(path, older=True)] == ["pypi.org"] and ap.tail_records(path) == []
    assert ap.tail_records(path, older=True, stop=lambda r: r["time"] < "2026-11-16T11:00:00Z") == []
    part.write_bytes(b"\x1f\x8b damaged")
    assert ap.tail_records(path, older=True) == []
    part.unlink()
    part.symlink_to(logs / "tools.jsonl")
    assert ap.part_tail(part, 1 << 20) == [] and ap.part_tail(logs / "none.gz", 1 << 20) == []
    raw = logs / "dns.jsonl.20261116-100000"  # a part the disk took no compressed copy of
    raw.write_text('{"n": 1}\n{"n": 2}\n{"n": 3}\n')
    assert ap.part_tail(raw, 1 << 20) == [b'{"n": 3}', b'{"n": 2}', b'{"n": 1}']
    assert ap.part_tail(raw, 10) == [b'{"n": 3}'] and ap.part_tail(raw, 0) == []
    assert [p.name for p in ap.older_parts(logs / "connections-20261116-100000-claude-1.jsonl")] == [
        "connections-20261116-100000-claude-1.jsonl.1.gz"]
    raw.with_name(raw.name + ".gz").write_bytes(gzip.compress(raw.read_bytes()))  # both ways: read once
    assert ap.older_parts(logs / "dns.jsonl") == [raw]


def test_the_blocked_list_after_a_rotation_end_to_end(tmp_path, env, monkeypatch):
    """Through ./sandbox-ai: `approvals blocked RUN --json` of a registered run whose logs rotated."""
    logs = blocked_logs(tmp_path, monkeypatch)
    directory = Path(env["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs" / "claude-1"
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({
        "run": "claude-1", "agent": "claude", "pid": os.getpid(), "directory": str(directory), "log_dir": str(logs),
        "started": "2026-11-16T10:00:00Z", "name": "claude-1"}))
    done = run_cli(env, "approvals", "blocked", "claude-1", "--json")
    assert done.returncode == 0, done.stderr
    assert sorted(entry["what"] for entry in json.loads(done.stdout)) == [
        "MCP tool files/delete_file with {}", "claude tool Bash", "connection to 198.51.100.3:443",
        "connection to 203.0.113.9:22", "lookup of pypi.org"]


# -- redaction: a file of assignments the agent read, and text that is JSON -----------------------------------------

ENV_FILE = ("DB_HOST=db.example\nDB_PASSWORD=hunter2hunter2\nPASSWORD=correct-horse\nAWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI\n"
            "OPENAI_API_KEY=abcdef0123456789\nexport STRIPE_SECRET=\"quoted value\"\nMY_PASSWD='single quoted'\n"
            "max_retries=3\nDEBUG=true\n")
ENV_SECRETS = ("hunter2hunter2", "correct-horse", "wJalrXUtnFEMI", "abcdef0123456789", "quoted value", "single quoted")


def test_assignments_of_secrets_are_redacted_in_every_log_also_inside_json():
    """`password=` was redacted only as a whole word (a form's field): DB_PASSWORD=..., AWS_SECRET_ACCESS_KEY=... of an
    .env file the agent read stayed in the connection log and the LLM traffic log for as long as those are kept - and
    in a request's body, which is JSON, even PASSWORD=... did (after "\\n" there is no word boundary)."""
    redacted = rt.redact_text(ENV_FILE)
    assert not any(secret in redacted for secret in ENV_SECRETS), redacted
    assert "DB_HOST=db.example" in redacted and "max_retries=3" in redacted and "DEBUG=true" in redacted
    assert "DB_PASSWORD=[redacted]\n" in redacted and "MY_PASSWD=[redacted]\n" in redacted  # (the line's end stays)
    # the same file inside a model request (the connection log keeps the body's text, which is JSON)
    body = json.dumps({"messages": [{"role": "user", "content": [{"type": "tool_result", "content": ENV_FILE}]}]})
    record = rt.body_record(body.encode(), len(body), [("content-type", "application/json")], 1 << 16)
    assert not any(secret in record["text"] for secret in ENV_SECRETS), record["text"]
    assert json.loads(record["text"])["messages"][0]["content"][0]["content"].count("[redacted]") == 6  # still JSON
    assert "db.example" in record["text"] and "max_retries=3" in record["text"]
    # a long body (searched only where a match can begin): the same
    long_body = json.dumps({"content": "x" * 5000 + "\n" + ENV_FILE + "y" * 5000})
    assert len(long_body) > rt.REDACT_ANCHORED
    assert rt.redact_text(long_body) == rt.redact_text(long_body, anchored=False)
    assert not any(secret in rt.redact_text(long_body) for secret in ENV_SECRETS)
    # the LLM traffic log (each string on its own) and a process's command line
    assert not any(secret in json.dumps(rt.llm_redacted({"content": ENV_FILE})) for secret in ENV_SECRETS)
    assert rt.redact_text("env PGPASSWORD=s3cr3tpw psql -h db") == "env PGPASSWORD=[redacted] psql -h db"
    # edge: nothing without a value, a name that only resembles one, what was redacted before stays so
    assert rt.redact_text("PASSWORD= next") == "PASSWORD= next" and rt.redact_text("a==b") == "a==b"
    for cut in ('API_KEY="abcdef0123', "API_KEY='abcdef0123", 'x\\nAPI_KEY=\\"abcdef0123'):  # (a body cut in the value)
        assert "abcdef0123" not in rt.redact_text(cut), cut
    assert rt.redact_text("monkey=1 keyboard=us author=me") == "monkey=1 keyboard=us author=me"
    assert rt.redact_text("grant_type=x&code=abc&client_secret=s") == "grant_type=x&code=[redacted]&client_secret=[redacted]"
    assert rt.redact_text('say \\"--password-store=basic\\" twice') == 'say \\"--password-store=[redacted]\\" twice'


def test_mcp_arguments_and_a_questions_body_are_redacted_as_values_not_as_json_text():
    """They were made JSON text first and then redacted as text: `password=` after a line break was missed (in JSON
    the break is "\\n": no word boundary before the name), and an escaped quote after a value lost its backslash."""
    arguments = {"path": "/w/.env", "content": "HOST=x\npassword=hunter2hunter2\nnext line",
                 "note": 'run it with "--password-store=basic" please', "api_key": "abcdef0123456789abcdef"}
    text = rt.McpGateway.arguments(arguments)
    assert "hunter2hunter2" not in text and "abcdef0123456789abcdef" not in text
    shown = json.loads(text)  # (still the JSON of the arguments)
    assert shown["api_key"] == "[redacted]" and shown["path"] == "/w/.env" and "next line" in shown["content"]
    assert shown["note"].endswith(" please") and "basic" not in shown["note"]
    # sad and edge: values that are no JSON types, deep nesting, a long text is still cut
    assert rt.McpGateway.arguments({"when": object}) .startswith('{"when": "<class')
    assert len(rt.McpGateway.arguments({"content": "x " * 5000})) <= 2003
    assert rt.McpGateway.arguments(None) == "null" and rt.McpGateway.arguments([]) == "[]"


# -- the canary log's links into the timeline after the connection log rotated -----------------------------------

canary = load("canary")


def test_a_canary_requests_timeline_link_holds_after_the_connection_log_rotated(tmp_path):
    """The timeline's collector gives a record of the connection log's second part the key "1.OFFSET" (LogFile); the
    canary log looked for "OFFSET" in every part: after the first rotation no request it showed was linked to its
    event in the timeline any more - or it was linked to another request, one that began at that offset in the first
    part."""
    name = "connections-20261116-100000-claude-1.jsonl"
    log = tmp_path / name
    held = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)  # the proxy's descriptor
    collector = act.Connections(tmp_path, name)
    made = {}

    def request(host):
        os.write(held, (json.dumps({"time": act.stamp(time.time()), "event": "http", "method": "POST",
                                    "url": f"https://{host}/v1/messages", "host": host, "status": 200}) + "\n").encode())
        for event in collector.poll([1 << 20], time.time()):
            record = act.finalize(event, "claude-1", None, "claude", act.Redactor())
            made[record["details"]["target"]] = record["id"]
    try:
        request("the-first.example")  # (each part shorter than the one before: the collector sees the emptying)
        assert rt.rotate_held_log(log)
        request("second.example")
        assert rt.rotate_held_log(log)
        request("3rd.example")
    finally:
        os.close(held)
    assert len(made) == 3 and len(set(made.values())) == 3
    found = canary.model_requests(tmp_path, "claude-1", {"the-first.example", "second.example", "3rd.example"})
    assert {q["host"]: q["activity_id"] for q in found} == made
    assert [(q["log"], q["offset"]) for q in found] == [(name + ".2.gz", 0), (name + ".1.gz", 0), (name, 0)]
    # the log gc compressed once the run ended is the last part still
    sd.compress(log)
    found = canary.model_requests(tmp_path, "claude-1", {"the-first.example", "second.example", "3rd.example"})
    assert {q["host"]: q["activity_id"] for q in found} == made


# -- a rotation cut short (the supervisor killed, the run ended in the middle of it) --------------------------------

def test_a_rotation_killed_before_it_emptied_the_log_leaves_no_duplicates_after_gc(tmp_path, monkeypatch):
    """rotate_held_log puts the compressed part in place, then empties the log: killed in between (the container
    stopped), the part's records were in both - every reader counted them twice, for as long as the logs are kept."""
    logs = tmp_path / "logs/p-1"
    logs.mkdir(parents=True)
    log = logs / "connections-20261116-100000-claude-1.jsonl"
    first = b"".join(b'{"connection": %d, "event": "http"}\n' % n for n in range(1, 4))
    log.write_bytes(first)
    assert rt.rotate_held_log(log)                      # a rotation that finished: NAME.1.gz
    second = b"".join(b'{"connection": %d, "event": "http"}\n' % n for n in range(4, 9))
    log.write_bytes(second)

    def killed(fd, length):
        raise KeyboardInterrupt
    monkeypatch.setattr(rt.os, "ftruncate", killed)
    with pytest.raises(KeyboardInterrupt):
        rt.rotate_held_log(log)                         # the part is in place, the log not emptied
    monkeypatch.undo()
    with open(log, "ab") as stream:                     # (the proxy wrote one more record before it ended)
        stream.write(b'{"connection": 9, "event": "http"}\n')
    names = sorted(p.name for p in logs.iterdir())
    assert names == [log.name, log.name + ".1.gz", log.name + ".2.gz"]
    assert gzip.decompress((logs / names[1]).read_bytes()) == second
    for path in logs.iterdir():
        os.utime(path, (time.time() - 7200,) * 2)
    result = sd.compact(tmp_path / "logs")
    assert not result["errors"]
    names = sorted(p.name for p in logs.iterdir())
    assert names == [log.name + ".1.gz", log.name + ".gz"]  # the duplicate gone, the older part back at its number
    assert gzip.decompress((logs / names[0]).read_bytes()) == first
    assert [r["connection"] for r in lines_of(logs / names[1])] == [4, 5, 6, 7, 8, 9]
    assert log.name + ".1.gz" in " ".join(result["dropped"])


def test_cut_rotation_happy_sad_edge(tmp_path):
    log = tmp_path / "connections-20261116-100000-r.jsonl"
    part = tmp_path / (log.name + ".1.gz")
    # sad: no part, a part that is not the log's beginning (a rotation that finished), a damaged part, a link
    log.write_bytes(b'{"n": 2}\n')
    assert sd.undo_cut_rotation(log) is None
    part.write_bytes(gzip.compress(b'{"n": 1}\n'))
    assert sd.undo_cut_rotation(log) is None and part.exists()
    part.write_bytes(b"\x1f\x8b damaged")
    assert sd.undo_cut_rotation(log) is None and part.exists()
    part.unlink()
    part.symlink_to(log)
    assert sd.undo_cut_rotation(log) is None and part.is_symlink()
    part.unlink()
    # edge: an empty log or an empty part is never "the same"; the log exactly the part (nothing written after)
    part.write_bytes(gzip.compress(b""))
    assert sd.undo_cut_rotation(log) is None
    part.write_bytes(gzip.compress(b'{"n": 2}\n'))
    (tmp_path / (log.name + ".2.gz")).write_bytes(gzip.compress(b'{"n": 1}\n'))
    (tmp_path / (log.name + ".4.gz")).write_bytes(gzip.compress(b'{"n": 0}\n'))
    assert sd.undo_cut_rotation(log) == part.name
    assert sorted(p.name for p in tmp_path.iterdir()) == [log.name, log.name + ".1.gz", log.name + ".3.gz"]
    assert sd.packed_prefix(log, tmp_path / "absent.gz") is None
    assert sd.packed_prefix(log, tmp_path / (log.name + ".3.gz")) is None


def test_a_part_left_both_raw_and_compressed_is_finished_by_gc(tmp_path, monkeypatch):
    """rotate_shared_log compresses the renamed log, waits 0.2 s for a late writer, then removes it: a run that ends in
    that moment left files.jsonl.TIME beside files.jsonl.TIME.gz - gc skipped the pair for good ("never overwritten"):
    128 MiB uncompressed until the logs' age limit, and the part twice for whoever reads both."""
    logs = tmp_path / "logs/p-1"
    logs.mkdir(parents=True)
    log = logs / "files.jsonl"
    log.write_bytes(b'{"n": 1}\n{"n": 2}\n')

    def killed(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(rt.time, "sleep", killed)
    with pytest.raises(KeyboardInterrupt):
        rt.rotate_shared_log(log, when=1_800_000_000)
    monkeypatch.undo()
    raw = logs / "files.jsonl.20270115-080000"
    packed = logs / (raw.name + ".gz")
    assert raw.exists() and packed.exists()
    with open(raw, "ab") as stream:
        stream.write(b'{"n": 3}\n')                     # a late writer's record, after the part was compressed
    traffic_part = logs / "llm-traffic-20261116-100000-r.jsonl.1"
    traffic_part.write_bytes(b'{"version":1,"n":1}\n')  # (LlmTrafficFile.pack killed before it removed the part)
    traffic_part.with_name(traffic_part.name + ".gz").write_bytes(gzip.compress(traffic_part.read_bytes()))
    other = logs / "dns.jsonl.20270115-080000"          # another content under the compressed name: both stay
    other.write_bytes(b'{"n": 7}\n')
    other.with_name(other.name + ".gz").write_bytes(gzip.compress(b'{"n": 8}\n'))
    for path in logs.iterdir():
        os.utime(path, (time.time() - 7200,) * 2)
    result = sd.compact(tmp_path / "logs", busy_dirs=[logs])
    assert sorted(p.name for p in logs.iterdir()) == [other.name, other.name + ".gz", packed.name,
                                                      traffic_part.name + ".gz"]
    assert [r["n"] for r in lines_of(packed)] == [1, 2, 3]
    assert result["saved"] > 0 and not result["errors"]
    # contract of finish_part: equal, a prefix (the rest appended), different, nothing there
    assert sd.finish_part(other, other.with_name(other.name + ".gz")) is False and other.exists()
    assert sd.finish_part(logs / "absent", packed) is False


# -- the rotation on a full disk ---------------------------------------------------------------------------------------

def test_a_rotation_that_fails_leaves_the_older_parts_where_they_are(tmp_path, monkeypatch):
    """rotate_held_log moved every older part up by one before it compressed the log: on a full disk the compression
    failed, a minute later the next try moved them again - NAME.1.gz became NAME.2.gz, NAME.3.gz ... and at 999 the
    parts were deleted: after 17 hours of a full disk a run had lost all its older parts (and every key the timeline
    and the canary log count by had moved)."""
    import errno
    log = tmp_path / "connections-20261116-100000-r.jsonl"
    log.write_bytes(b'{"n": 1}\n')
    assert rt.rotate_held_log(log)
    log.write_bytes(b'{"n": 2}\n')
    assert rt.rotate_held_log(log)
    log.write_bytes(b'{"n": 3}\n')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert sorted(before) == [log.name, log.name + ".1.gz", log.name + ".2.gz"]
    real = rt.gzip.GzipFile

    class Full(real):
        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(rt.gzip, "GzipFile", Full)
    for _ in range(3):  # (the rotator tries again every minute)
        assert rt.rotate_logs(tmp_path, held=log.name, part=1) == []
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before  # nothing moved, no temporary file left
    monkeypatch.setattr(rt.gzip, "GzipFile", real)
    assert rt.rotate_logs(tmp_path, held=log.name, part=1) == [log.name]  # space again: the rotation as it should be
    assert [gzip.decompress((tmp_path / f"{log.name}.{n}.gz").read_bytes()) for n in (3, 2, 1)] == [
        b'{"n": 1}\n', b'{"n": 2}\n', b'{"n": 3}\n'] and log.read_bytes() == b""


# -- two runs of one project rotate a shared log in the same second --------------------------------------------------

def test_a_second_rotation_of_the_same_second_never_replaces_the_first_ones_part(tmp_path, monkeypatch):
    """Both runs' rotators see the log over its limit. The second looked whether the part of that second exists and
    then renamed - a rename replaces: had the first run renamed in between (and a writer started the new log), the
    second put the new, small log over the first run's part of 128 MiB."""
    log = tmp_path / "files.jsonl"
    part = tmp_path / "files.jsonl.20270115-080000"
    part.write_bytes(b"the first run's part, being compressed\n")
    log.write_bytes(b'{"n": "new"}\n')
    monkeypatch.setattr(rt.Path, "exists", lambda self: False)  # (the look came before the first run's rename)
    assert rt.rotate_shared_log(log, when=1_800_000_000) is None
    monkeypatch.undo()
    assert part.read_bytes() == b"the first run's part, being compressed\n" and log.read_bytes() == b'{"n": "new"}\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == [log.name, part.name]
    # the timeline's own rotation (the launcher's), the same way
    timeline_log = tmp_path / "activity.jsonl"
    timeline_log.write_bytes(b'{"kind": "x"}\n' * 4)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    for second in range(3):  # (whichever second the append falls into)
        (tmp_path / f"activity.jsonl.{time.strftime('%Y%m%d-%H%M%S', time.gmtime(time.time() + second))}").write_bytes(
            b"another run's part\n")
    monkeypatch.setattr(act.Path, "exists", lambda self: False)
    assert act.append(timeline_log, [{"kind": "y"}], limit=20) is False
    monkeypatch.undo()
    assert (tmp_path / f"activity.jsonl.{stamp}").read_bytes() == b"another run's part\n"
    assert timeline_log.read_bytes() == b'{"kind": "x"}\n' * 4


def test_a_split_part_keeps_the_time_of_its_log(tmp_path):
    """The part gc splits off a shared log was dated now: the age limit kept it for 30 days from the day gc ran, not
    from the day its records were written."""
    log = tmp_path / "files.jsonl"
    log.write_bytes(b'{"n": 1}\n' * 1000)
    when = time.time() - 20 * DAY
    os.utime(log, (when, when))
    assert sd.split(log, keep=1000) > 0
    part = next(p for p in tmp_path.iterdir() if p.name.endswith(".gz"))
    assert abs(part.stat().st_mtime - when) < 2 and abs(log.stat().st_mtime - when) < 2


# -- end to end through ./sandbox-ai ------------------------------------------------------------------------------------

def test_an_env_file_in_a_conversation_is_redacted_in_the_llm_traffic_log_end_to_end(tmp_path, env):
    """The proxy's LlmTraffic and the supervisor's file, then `sandbox-ai llm traffic --json`: no secret of the .env
    file the agent read, the rest of it there."""
    request = claude_request(1)
    request["messages"][-1]["content"][0]["content"] = ENV_FILE
    traffic_log(tmp_path / "logs", [request, {**request, "messages": request["messages"] + [
        {"role": "assistant", "content": [{"type": "text", "text": "I see DB_PASSWORD=hunter2hunter2 there."}]}]}])
    assert not any(secret.encode() in path.read_bytes() for path in (tmp_path / "logs").iterdir()
                   for secret in ENV_SECRETS)
    done = run_cli(env, "llm", "traffic", "--log-dir", str(tmp_path / "logs"), "--json")
    assert done.returncode == 0, done.stderr
    assert not any(secret in done.stdout for secret in ENV_SECRETS)
    records = [json.loads(line) for line in done.stdout.splitlines()]
    assert len(records) == 2 and "DB_HOST=db.example" in records[1]["request"]["messages"][2]["content"][0]["content"]
    assert records[1]["request"]["messages"][3]["content"][0]["text"] == "I see DB_PASSWORD=[redacted] there."


def test_gc_finishes_cut_rotations_end_to_end(tmp_path, env, monkeypatch):
    """`sandbox-ai gc` over a state directory with a connection log whose rotation never emptied it and a part left
    both raw and compressed."""
    logs = Path(env["XDG_STATE_HOME"]) / "sandbox-ai/logs/project-0123abcd"
    logs.mkdir(parents=True)
    log = logs / "connections-20261116-100000-claude-1.jsonl"
    records = b"".join(b'{"connection": %d, "event": "http"}\n' % n for n in range(1, 200))
    log.write_bytes(records)

    def killed(fd, length):
        raise KeyboardInterrupt
    monkeypatch.setattr(rt.os, "ftruncate", killed)
    with pytest.raises(KeyboardInterrupt):
        rt.rotate_held_log(log)
    monkeypatch.undo()
    raw = logs / "files.jsonl.20261116-100500"
    raw.write_bytes(b'{"action": "write", "path": "/work/a"}\n' * 50)
    raw.with_name(raw.name + ".gz").write_bytes(gzip.compress(raw.read_bytes()))
    for path in logs.iterdir():
        os.utime(path, (time.time() - 7200,) * 2)
    done = run_cli(env, "gc", "--json")
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in logs.iterdir()) == [log.name + ".gz", raw.name + ".gz"]
    assert gzip.decompress((logs / (log.name + ".gz")).read_bytes()) == records
    done = run_cli(env, "logs", str(logs), "--json")  # every record once for the statistics
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["telemetry"]["requests"] == 199


def test_an_assigned_secret_is_redacted_across_command_separators():
    """Backlog A (security review 1, finding 8): an assigned secret's value ended at `;`, `&`, `|` or `)`:
    `DB_PASSWORD=p@ss;word123` left `;word123` in the log. The value now runs to the next white space or quote."""
    for text, secret in (("DB_PASSWORD=p@ss;word123", ";word123"), ("API_KEY=ab&cd&&ef", "cd"),
                         ("export TOKEN=x|y|zz9 && run", "zz9"), ("(SECRET=a)b)", "b)"),
                         ("DB_PASSWORD=p@ss;word123\nNEXT=1", "word123")):
        redacted = rt.redact_text(text)
        assert secret not in redacted and "[redacted]" in redacted, (text, redacted)
    assert rt.redact_text("DB_PASSWORD=p@ss;word123 echo done") == "DB_PASSWORD=[redacted] echo done"
    assert rt.redact_text("DB_PASSWORD=p@ss;word123\nNEXT=1") == "DB_PASSWORD=[redacted]\nNEXT=1", "the line's end"
    assert rt.redact_text('x DB_PASSWORD=p;w "quoted"') == 'x DB_PASSWORD=[redacted] "quoted"'
    # a query string's field keeps its own end (`&` before the next NAME=), as a form does
    assert rt.redact_text("https://x.example/a?api_key=SECRET&q=x") == "https://x.example/a?api_key=[redacted]&q=x"
    assert rt.redact_text("grant_type=x&code=abc&client_secret=s") == \
        "grant_type=x&code=[redacted]&client_secret=[redacted]"
