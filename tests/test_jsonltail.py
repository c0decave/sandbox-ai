"""Reading the newest records of a JSON-lines log from its end (activity, safeguards): the same records as reading
the whole file, in any chunk size; a line still being written and an overlong line are not taken."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "sandbox"


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_test_jsonltail", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tails = load("jsonltail")


def parse(line):
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


@pytest.mark.parametrize("chunk", [1, 3, 7, 64, 1 << 20])
def test_the_newest_records_are_those_of_a_whole_read(tmp_path, chunk):
    path = tmp_path / "log.jsonl"
    records = [{"n": n, "kind": "a" if n % 3 else "b", "pad": "x" * (n % 11)} for n in range(60)]
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records) + b"damaged\n" + b'{"n": 99')
    fd = os.open(path, os.O_RDONLY)
    try:
        end = tails.complete_end(fd, os.fstat(fd).st_size, chunk)
        assert end == path.stat().st_size - len(b'{"n": 99')  # the line still being written is left out
        assert tails.newest(fd, end, parse, None, 5, chunk) == records[-5:]
        keep = lambda r: r["kind"] == "b"  # noqa: E731
        assert tails.newest(fd, end, parse, keep, 4, chunk) == [r for r in records if keep(r)][-4:]
        assert tails.newest(fd, end, parse, None, 1000, chunk) == records  # fewer than asked: all of them
    finally:
        os.close(fd)


def test_an_overlong_line_is_skipped_whole(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    path.write_bytes(b'{"n": 1}\n' + json.dumps({"n": 2, "big": "y" * 5000}).encode() + b'\n{"n": 3}\n')
    fd = os.open(path, os.O_RDONLY)
    try:
        lines = list(tails.lines_from_end(fd, path.stat().st_size, chunk=64, line_limit=1000))
    finally:
        os.close(fd)
    assert lines == [b'{"n": 3}', b'{"n": 1}']


def test_activity_tail_reads_from_the_end_and_follows_from_there(tmp_path):
    activity = load("activity")
    path = tmp_path / "activity.jsonl"
    records = [{"kind": "prompt", "run": f"r{n % 2}", "time": f"2026-11-11T10:00:{n:02d}Z"} for n in range(40)]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    position = activity.Follow()
    got = activity.read_records(path, lambda r: r["run"] == "r1", 3, position)
    assert got == [r for r in records if r["run"] == "r1"][-3:]
    assert position.offset == path.stat().st_size and position.inode == path.stat().st_ino
    assert [r["run"] for r in activity.runs_in(path)] == ["r0", "r1"]
