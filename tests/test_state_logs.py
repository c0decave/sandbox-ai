"""How much sandbox-ai's logs take: the file log's reads summed per folder, repeated bodies of the connection log
written once, readers that stop at their budget, the archives of busy folders capped, and the age limit of the
logs (log_days). Review docs/reviews/state-logs-review-1.md."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()


def load(name):
    spec = importlib.util.spec_from_file_location(f"state_logs_{name}", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime")


def run_file_logger(tmp_path, monkeypatch, work, reads=None):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(rt, "LOG_DIR", logs)
    stop = threading.Event()
    kwargs = {} if reads is None else {"reads": reads}
    logger = threading.Thread(target=rt.file_logger, args=([str(tmp_path / "w")], stop), kwargs=kwargs, daemon=True)
    logger.start()
    deadline = time.time() + 5
    while not rt.FILE_LOGGING.is_set() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)
    work()
    time.sleep(0.5)
    stop.set()
    logger.join(5)
    assert not logger.is_alive()
    path = logs / "files.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def make_tree(tmp_path):
    (tmp_path / "w/a").mkdir(parents=True)
    (tmp_path / "w/b").mkdir()
    for index in range(40):
        (tmp_path / f"w/a/m{index}.py").write_text("x")
    (tmp_path / "w/b/one.txt").write_text("y")


def read_all(tmp_path):
    for _ in range(3):
        for index in range(40):
            (tmp_path / f"w/a/m{index}.py").read_text()
    (tmp_path / "w/b/one.txt").read_text()
    (tmp_path / "w/b/new.txt").write_text("z")


def test_reads_are_summed_per_folder_by_default(tmp_path, monkeypatch):
    make_tree(tmp_path)
    records = run_file_logger(tmp_path, monkeypatch, lambda: read_all(tmp_path))
    reads = [r for r in records if r.get("action") == "read"]
    assert reads and all("path" not in r and isinstance(r.get("folder"), str) for r in reads)
    by_folder = {r["folder"]: r for r in reads}
    assert by_folder[str(tmp_path / "w/a")]["reads"] == 120  # every read counted, one record
    assert by_folder[str(tmp_path / "w/a")]["files"] == 40
    assert len(by_folder[str(tmp_path / "w/a")]["names"]) <= rt.FILE_READ_NAMES
    assert by_folder[str(tmp_path / "w/b")]["reads"] == 1
    # changes stay one record per file, as before
    assert ("create", str(tmp_path / "w/b/new.txt")) in {(r.get("action"), r.get("path")) for r in records}


def test_reads_each_file_with_files_mode(tmp_path, monkeypatch):
    make_tree(tmp_path)
    records = run_file_logger(tmp_path, monkeypatch, lambda: read_all(tmp_path), reads="files")
    reads = [r for r in records if r.get("action") == "read"]
    assert len(reads) == 41  # each file once per FILE_REPEAT_SECONDS, as before
    assert all("path" in r for r in reads)


def test_reads_off_logs_no_read(tmp_path, monkeypatch):
    make_tree(tmp_path)
    records = run_file_logger(tmp_path, monkeypatch, lambda: read_all(tmp_path), reads="off")
    assert not [r for r in records if r.get("action") == "read"]
    assert ("create", str(tmp_path / "w/b/new.txt")) in {(r.get("action"), r.get("path")) for r in records}


# -- the connection log: a body logged before in the same part is written as a reference --------------------------

def exchange(number, request_text, answer_text="ok"):
    return {"time": f"2026-11-16T10:00:{number:02d}Z", "connection": number, "event": "http", "method": "POST",
            "url": "https://api.anthropic.com/v1/messages",
            "request": {"headers": [["Host", "api.anthropic.com"]],
                        "body": {"size": 200000, "truncated": True, "text": request_text}},
            "response": {"headers": [], "body": {"size": len(answer_text), "truncated": False, "text": answer_text}}}


def test_a_repeated_body_is_logged_once_then_referenced():
    seen = {}
    prefix = "S" * 65536
    first = rt.logged_once(exchange(1, prefix), seen)
    second = rt.logged_once(exchange(2, prefix, "another answer"), seen)
    assert first["request"]["body"]["text"] == prefix and first["request"]["body"]["digest"]
    body = second["request"]["body"]
    assert "text" not in body and body["size"] == 200000 and body["truncated"] is True
    assert body["same_as"] == {"connection": 1, "time": "2026-11-16T10:00:01Z", "digest": first["request"]["body"]["digest"]}
    assert second["response"]["body"]["text"] == "another answer"  # short and new: as it was


def test_short_or_different_bodies_stay_whole_and_the_record_is_not_changed():
    seen = {}
    original = exchange(1, "x" * 100)
    kept = json.dumps(original)
    assert rt.logged_once(original, seen)["request"]["body"]["text"] == "x" * 100
    assert rt.logged_once(exchange(2, "x" * 100), seen)["request"]["body"]["text"] == "x" * 100  # short: no reference
    assert json.dumps(original) == kept
    assert rt.logged_once(exchange(3, "a" * 5000), seen)["request"]["body"].get("text") == "a" * 5000
    assert rt.logged_once(exchange(4, "b" * 5000), seen)["request"]["body"].get("text") == "b" * 5000


def test_bodies_remembered_are_bounded_and_odd_records_pass():
    seen = {}
    for number in range(rt.LOG_BODY_SEEN + 10):
        rt.logged_once(exchange(number % 60, f"{number:08d}" + "z" * 5000), seen)
    assert len(seen) == rt.LOG_BODY_SEEN
    # the oldest were forgotten: logged whole again
    assert "text" in rt.logged_once(exchange(1, f"{0:08d}" + "z" * 5000), seen)["request"]["body"]
    for odd in ({"event": "tcp"}, {"request": None}, {"request": {"body": None}}, {"response": {"body": "text"}},
                {"request": {"body": {"base64": 5}}}):
        assert rt.logged_once(odd, {}) == odd


def test_append_locked_builds_the_line_under_the_lock_with_the_size(tmp_path):
    path = tmp_path / "log.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        sizes = []
        assert rt.append_locked(fd, lambda size: sizes.append(size) or b"one\n") == 4
        assert rt.append_locked(fd, b"two\n") == 8
        os.ftruncate(fd, 0)  # what the rotation does
        assert rt.append_locked(fd, lambda size: sizes.append(size) or b"three\n") == 6
        assert sizes == [0, 0]
    finally:
        os.close(fd)
    assert path.read_bytes() == b"three\n"


def test_the_proxy_writes_a_repeated_body_once_per_part_of_its_log(tmp_path):
    """Through ProxyServer.log and the run's rotation: each part of the log holds the bodies its references name."""
    log = tmp_path / "connections-20261116-100000-claude-1.jsonl"
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    server = rt.ProxyServer({"allow": {}}, fd, run_dir=tmp_path)
    prefix = "system prompt and tools " * 3000
    try:
        for number in range(1, 4):
            server.log(exchange(number, prefix))
        assert rt.rotate_held_log(log)
        for number in range(4, 6):
            server.log(exchange(number, prefix))
    finally:
        os.close(fd)
    older = [json.loads(line) for line in gzip.decompress((tmp_path / (log.name + ".1.gz")).read_bytes()).splitlines()]
    newer = [json.loads(line) for line in log.read_text().splitlines()]
    for part in (older, newer):
        whole = {r["request"]["body"]["digest"]: r["connection"] for r in part if "text" in r["request"]["body"]}
        assert len(whole) == 1  # the first of the part, whole
        for record in part:
            reference = record["request"]["body"].get("same_as")
            if reference:
                assert whole[reference["digest"]] == reference["connection"]
    assert [r["connection"] for r in newer if "text" in r["request"]["body"]] == [4]
    assert len(prefix) < log.stat().st_size < len(prefix) + 2000  # the second record: a reference, not 72 KB again


# -- readers: a compressed part is not unpacked once the budget is spent ------------------------------------------

def test_a_compressed_part_beyond_the_budget_is_not_unpacked(tmp_path, monkeypatch):
    logs = load("logs")
    line = b'{"event": "http", "host": "example.org"}\n'
    (tmp_path / "connections-20261116-100000-r.jsonl").write_bytes(line * 1000)
    (tmp_path / "connections-20261116-100000-r.jsonl.1.gz").write_bytes(gzip.compress(line * 500))
    unpacked = []
    tails = logs.jsonltail()
    real = tails.compressed_tail
    monkeypatch.setattr(tails, "compressed_tail", lambda path, *rest: unpacked.append(path) or real(path, *rest))
    budget = logs.new_budget(len(line) * 999 + 10)  # the plain log takes it; less than a line is left
    records = [r for path in logs.connection_logs(tmp_path) for r in logs.iter_records(path, budget)]
    assert len(records) == 999 and unpacked == []
    assert budget["left"] == 0
    assert budget["skipped_bytes"] == len(line) - 10 + len(line) * 500  # the cut of the log, the part unpacked
    budget = logs.new_budget(len(line) * 1500)  # a compressed part cut by the budget spends it too
    (tmp_path / "connections-20261116-100000-r.jsonl.2.gz").write_bytes(gzip.compress(line * 500))
    records = [r for path in logs.connection_logs(tmp_path) for r in logs.iter_records(path, budget)]
    assert len(records) == 1500 and [p.name[-4:] for p in unpacked] == ["1.gz"] and budget["left"] == 0
    (tmp_path / "connections-20261116-100000-r.jsonl.2.gz").unlink()
    unpacked.clear()
    budget = logs.new_budget(len(line) * 1200)  # budget left: the part is read
    records = [r for path in logs.connection_logs(tmp_path) for r in logs.iter_records(path, budget)]
    assert len(records) == 1200 and len(unpacked) == 1


# -- the archives of a folder a run logs into ----------------------------------------------------------------------

sd = load("statedirs")


def aged(path, data, age=7200):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    when = time.time() - age
    os.utime(path, (when, when))
    return path


def test_rotated_activity_parts_of_a_busy_folder_are_capped(tmp_path, monkeypatch):
    """activity.jsonl rotates itself to activity.jsonl.TIME; gc compressed those, but dropped the oldest beyond
    ARCHIVE_LIMIT only after a split - never in a folder a run logs into, the busiest - so they grew without end."""
    busy = tmp_path / "logs/proj"
    data = os.urandom(3000)  # (does not compress)
    for day in range(1, 6):
        aged(busy / f"activity.jsonl.202611{day:02d}-100000", data)
    monkeypatch.setattr(sd, "ARCHIVE_LIMIT", 7000)
    result = sd.compact(tmp_path / "logs", set(), [busy])
    assert len(result["compressed"]) == 5
    left = sorted(p.name for p in busy.iterdir())
    assert left == ["activity.jsonl.20261104-100000.gz", "activity.jsonl.20261105-100000.gz"]  # the newest two fit
    assert sorted(result["dropped"]) == [f"activity.jsonl.202611{day:02d}-100000.gz" for day in (1, 2, 3)]


def test_the_tool_log_rotates_during_the_run(tmp_path):
    log = aged(tmp_path / "tools.jsonl", b'{"tool": "Bash"}\n' * 100, age=0)
    assert "tools.jsonl" in rt.rotate_logs(tmp_path, part=100)
    assert not log.exists() and len(list(tmp_path.glob("tools.jsonl.*.gz"))) == 1


# -- the age limit of the logs (log_days) --------------------------------------------------------------------------

DAY = 86400


def test_old_logs_are_the_files_nothing_was_written_into_for_days(tmp_path):
    logs = tmp_path / "logs"
    proj, busy = logs / "proj-1", logs / "busy-2"
    old = [aged(proj / "files.jsonl.20261001-100000.gz", b"x", 40 * DAY),           # an older part
           aged(proj / "activity.jsonl.20261001-100000", b"x", 40 * DAY),            # rotated, not compressed yet
           aged(proj / "connections-20261001-100000-ended.jsonl.gz", b"x", 40 * DAY),  # a run that ended
           aged(proj / "llm-traffic-20261001-100000-ended.jsonl.2", b"x", 40 * DAY),
           aged(proj / "dns.jsonl", b"x", 40 * DAY),                                  # shared, folder not in use
           aged(busy / "connections-20261001-100000-live.jsonl.3.gz", b"x", 40 * DAY)]  # a live run's older part
    kept = [aged(busy / "connections-20261001-100000-live.jsonl", b"x", 40 * DAY),   # the log a live run writes
            aged(busy / "files.jsonl", b"x", 40 * DAY),                                # shared, folder in use
            aged(proj / "files.jsonl.20261101-100000.gz", b"x", 10 * DAY),            # younger
            aged(proj / "report.html", b"x", 40 * DAY),                                # not a log
            aged(logs / ".proj-3.removing-1/files.jsonl", b"x", 40 * DAY)]             # hidden: being removed
    (proj / "tools.jsonl").symlink_to(old[0])
    found = sd.old_logs(logs, 30, {"live"}, [busy], now=time.time())
    assert sorted(item["path"] for item in found) == sorted(str(p) for p in old)
    assert all(item["last_written"] < sd.iso(time.time() - 30 * DAY) for item in found)
    assert sd.old_logs(logs, 50, {"live"}, [busy]) == []
    assert sd.old_logs(tmp_path / "missing", 30) == []
    # written meanwhile: left; gone meanwhile: passed; the rest removed
    os.utime(old[0])
    old[1].unlink()
    done = sd.remove_old_logs(found, 30)
    assert sorted(done["removed"]) == sorted(str(p) for p in old[2:]) and done["errors"] == []
    assert old[0].exists() and all(p.exists() for p in kept)


def run_cli(env, *args):
    import subprocess
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], capture_output=True, text=True,
                          env={**os.environ, **env, "PATH": "/usr/bin:/bin"}, timeout=120)


@pytest.fixture
def env(tmp_path, monkeypatch):
    values = {"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_DATA_HOME": str(tmp_path / "data"),
              "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
              "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "SANDBOX_AI_AUTO_COMPACT": "0"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "runtime").mkdir()
    return values


def test_gc_removes_logs_older_than_log_days_end_to_end(env, tmp_path):
    state = tmp_path / "state/sandbox-ai"
    folder = state / "logs/proj-0123456789ab"
    part = aged(folder / "files.jsonl.20261001-100000.gz", gzip.compress(b"{}\n" * 1000), 40 * DAY)
    young = aged(folder / "files.jsonl.20261110-100000.gz", gzip.compress(b"{}\n"), 5 * DAY)
    # the default (no log_days): the first gc says it and from when; nothing goes yet
    first = run_cli(env, "gc")
    assert first.returncode == 0, first.stderr
    assert "UTC on, gc and its daily pass remove 1 log file(s) older than 30 days" in first.stdout
    assert "sandbox-ai defaults set log_days 0" in first.stdout and part.exists()
    usage = json.loads(run_cli(env, "gc", "--usage", "--json").stdout)["would_free"]["log_days"]
    assert usage["origin"] == "default" and usage["found"] == 1 and usage["removed_from"]
    assert "older than 30 days (the default age limit; from" in " ".join(run_cli(env, "gc", "--usage").stdout.split())
    # its grace over: gc removes it
    notice = state / "log-days.json"
    # (said 15 days ago: gc, gc --usage or a start showed it - review 2: never the daily pass alone)
    notice.write_text(json.dumps({"noticed": sd.iso(time.time() - 15 * DAY), "said": sd.iso(time.time() - 15 * DAY)}))
    dry = run_cli(env, "gc", "--dry-run")
    assert "would remove 1 log file(s) older than 30 days" in dry.stdout and part.exists()
    done = run_cli(env, "gc")
    assert "removed 1 log file(s) older than 30 days" in done.stdout
    assert not part.exists() and young.exists()
    # log_days set: at once, also without a notice; 0: never
    notice.unlink()
    assert run_cli(env, "defaults", "set", "log_days", "3").returncode == 0
    done = json.loads(run_cli(env, "gc", "--json").stdout)["log_days"]
    assert done["origin"] == "log_days" and done["removed"] == [str(young)] and not young.exists()
    aged(folder / "dns.jsonl", b"{}\n", 400 * DAY)
    assert run_cli(env, "defaults", "set", "log_days", "0").returncode == 0
    assert json.loads(run_cli(env, "gc", "--json").stdout)["log_days"]["origin"] == "off"
    assert (folder / "dns.jsonl").exists()
    refused = run_cli(env, "defaults", "set", "log_days", "-1")
    assert refused.returncode != 0 and "log_days" in refused.stderr


def test_the_file_reads_setting_is_checked_end_to_end(env):
    assert run_cli(env, "defaults", "set", "file_reads", "files").returncode == 0
    refused = run_cli(env, "defaults", "set", "file_reads", "each")
    assert refused.returncode != 0 and "file_reads is one of folders, files, off" in refused.stderr


# -- binary answers (downloads): their size, digest and first bytes, unless connection_bodies is all -------------

def download(number, data):
    import base64
    return {"time": "2026-11-16T10:00:00Z", "connection": number, "event": "http", "method": "GET",
            "url": "https://files.pythonhosted.org/x.whl",
            "request": {"headers": [], "body": None},
            "response": {"headers": [["Content-Type", "application/octet-stream"]],
                         "body": {"size": 3_000_000, "truncated": True, "base64": base64.b64encode(data).decode()}}}


def test_a_binary_answer_keeps_its_digest_and_first_bytes():
    import base64
    import hashlib
    data = os.urandom(65536)
    written = rt.binary_answer(download(1, data))
    body = written["response"]["body"]
    assert "base64" not in body and body["size"] == 3_000_000 and body["truncated"] is True
    assert body["binary"] == {"logged": 65536, "sha256": hashlib.sha256(data).hexdigest(),
                              "head": base64.b64encode(data[:rt.BINARY_HEAD]).decode()}
    upload = download(2, data)
    upload["request"], upload["response"] = upload["response"], {"headers": [], "body": None}
    assert rt.binary_answer(upload) is upload  # what was sent stays whole (up to 64 KiB)
    text = {"response": {"body": {"size": 3, "text": "abc"}}}
    assert rt.binary_answer(text) is text
    broken = {"response": {"body": {"size": 3, "base64": "%%%"}}}
    assert rt.binary_answer(broken) is broken


def test_the_proxy_logs_binary_answers_by_digest_unless_asked_for_all(tmp_path):
    data = os.urandom(65536)
    for setting, whole in ((None, False), ("text", False), ("all", True)):
        log = tmp_path / f"connections-{setting}.jsonl"
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        server = rt.ProxyServer({"allow": {}, **({"connection_bodies": setting} if setting else {})}, fd,
                                run_dir=tmp_path)
        try:
            server.log(download(1, data))
        finally:
            os.close(fd)
        body = json.loads(log.read_text())["response"]["body"]
        assert ("base64" in body) is whole and ("binary" in body) is not whole


def test_the_settings_reach_the_sandbox_end_to_end(tmp_path):
    """The real ./sandbox-ai (Podman faked, tests/live_fakes.py): file_reads and connection_bodies in your defaults
    are what the run's supervisor and proxy are given; without them reads are summed and downloads digested."""
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    logs = tmp_path / "logs"

    def started(name):
        kept = tmp_path / name
        done = host.run("claude", "--no-tmux", "--no-auth", "-D", "--log-dir", str(logs),
                        env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
        assert done.returncode == 0, done.stderr
        return json.loads((kept / "session/config.json").read_text())
    def plan():
        done = host.run("claude", "--dry-run", "--log-dir", str(logs))
        assert done.returncode == 0, done.stderr
        found = json.loads(done.stdout)
        return found["proxy"]["connection_bodies"], found["file_log"]
    plain = started("plain")
    assert plain["file_reads"] == "folders" and plain["watch"]  # (an explicit --log-dir: files are watched)
    assert plan() == ("text", {"watched": True, "reads": "folders"})
    for key, value in (("file_reads", "files"), ("connection_bodies", "all")):
        assert host.run("defaults", "set", key, value).returncode == 0
    assert started("chosen")["file_reads"] == "files"
    assert plan() == ("all", {"watched": True, "reads": "files"})


def test_a_window_of_many_folders_is_merged_into_their_parents():
    """A recursive grep reads every folder of a tree in one window: one record per folder was 231 records every 30 s
    for this repository alone. Beyond FILE_READ_RECORDS the deepest folders are merged into their parents; every read
    is still counted."""
    reads = rt.FileReads()
    for top in range(3):
        for sub in range(40):
            for name in ("a.py", "b.py"):
                reads.add(f"/work/src/pkg{top}/mod{sub}/{name}")
    reads.add("/work/README.md")
    written = []
    reads.flush(written.append)
    assert len(written) <= rt.FILE_READ_RECORDS
    assert sum(r["reads"] for r in written) == 241 and sum(r["files"] for r in written) == 241
    by_folder = {r["folder"]: r for r in written}
    assert by_folder["/work/src/pkg0"]["folders"] == 40 and by_folder["/work/src/pkg0"]["reads"] == 80
    assert by_folder["/work"]["reads"] == 1 and by_folder["/work"].get("folders", 1) == 1
    few = rt.FileReads()
    few.add("/a/b/c/d.txt")
    few.flush(written.append)
    assert written[-1] == {"action": "read", "folder": "/a/b/c", "reads": 1, "files": 1, "names": ["d.txt"]}


def test_the_names_a_window_tells_apart_are_bounded(monkeypatch):
    monkeypatch.setattr(rt, "FILE_READ_KNOWN", 10)
    reads = rt.FileReads()
    for index in range(30):
        reads.add(f"/w/a/f{index}")
    reads.add("/w/a/f0")  # told apart: the same file
    reads.add("/w/a/f25")  # beyond the bound: counted as another
    assert reads.known == 10 and sum(len(e["names"]) for e in reads.folders.values()) == 10
    written = []
    reads.flush(written.append)
    assert written == [{"action": "read", "folder": "/w/a", "reads": 32, "files": 31,
                        "names": ["f0", "f1", "f2", "f3", "f4"]}]
    assert reads.known == 0 and reads.folders == {}


def test_spend_logs_stay_while_a_months_limit_counts_them(tmp_path):
    """Review 3x-2: a month's spend limit (sandbox-ai spend limit set --month) counts what every run spent since the
    month began, from the runs' spend logs - which went with the other logs after log_days (7 days, say: a month's
    limit then saw a week). A spend log written last in this month stays; once its month is over, log_days holds."""
    import datetime
    logs = tmp_path / "logs" / "proj-1"
    now = datetime.datetime(2026, 11, 20, 12, 0).timestamp()  # (local time)
    written = {"this-month": datetime.datetime(2026, 11, 2, 9, 0).timestamp(),
               "last-month": datetime.datetime(2026, 10, 25, 9, 0).timestamp()}
    paths = {}
    for when, at in written.items():
        for kind in ("spend", "connections"):
            path = logs / f"{kind}-20261001-100000-{when}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            os.utime(path, (at, at))
            paths[kind, when] = str(path)
    found = {item["path"] for item in sd.old_logs(tmp_path / "logs", 7, now=now)}
    assert found == {paths["spend", "last-month"], paths["connections", "last-month"],
                     paths["connections", "this-month"]}
