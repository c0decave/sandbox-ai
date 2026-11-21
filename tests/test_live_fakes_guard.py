"""No process of a test's fake host outlives the test (tests/live_fakes.py, the guard in conftest.py).

Found 2026-11-20: test_mcp_tool_budget.py's real start with --mcp memory --mcp tasks left both servers' relays (and
their loggers) running for hours, four processes a run. That is the product's design - an MCP server outlives the
start it was started for and serves the next ones (README "Local MCP servers"; the failed start is kept, a resume uses
them) - but a test's host has a HOME of its own: nothing ever uses or stops them again. A test stops what it started
(`sandbox-ai mcp stop NAME`); the guard ends what is left by exact PID and fails that test.
"""
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_fakes  # noqa: E402


def detached(*words):
    """A process of its own session (as the launcher's background servers are: ppid 1 once it is gone) whose command
    line names WORDS."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *map(str, words)],
                            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def test_the_processes_naming_a_folder_are_found_and_ended_by_their_pid(tmp_path):
    folder = tmp_path / "host"
    other = tmp_path / "hostess"  # a name that starts with the folder's is another folder
    mine, theirs = detached(folder / "user/.local/state/x.sock"), detached(other)
    try:
        found = live_fakes.naming(folder)
        assert mine.pid in found and str(folder) in found[mine.pid]
        assert theirs.pid not in found
        assert live_fakes.end_processes(found, grace=5) == sorted(found)
        assert mine.wait(10) is not None
        assert theirs.poll() is None, "a process that does not name the folder was ended"
        assert live_fakes.naming(folder) == {}
    finally:
        for process in (mine, theirs):
            if process.poll() is None:
                process.kill()
            process.wait(10)


def test_a_process_that_ends_meanwhile_is_no_leftover(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)", str(tmp_path)])
    try:
        assert live_fakes.outliving(tmp_path, wait=5) == {}
    finally:
        process.wait(10)


def test_the_guard_ends_what_a_host_left_and_says_it(tmp_path):
    host = live_fakes.Host(tmp_path / "host")
    assert host in live_fakes.HOSTS
    left = detached("--internal-mcp-serve", host.home / "server.sock")
    try:
        problems = live_fakes.guard([host], wait=0.2)
        assert len(problems) == 1 and f"PID {left.pid}" in problems[0] and "--internal-mcp-serve" in problems[0]
        assert left.wait(10) is not None
        assert live_fakes.guard([host], wait=0.2) == []
    finally:
        if left.poll() is None:
            left.kill()
        left.wait(10)
        live_fakes.HOSTS.remove(host)


def test_a_host_that_left_nothing_passes_at_once(tmp_path):
    host = live_fakes.Host(tmp_path / "host")
    live_fakes.HOSTS.remove(host)
    begun = time.monotonic()
    assert live_fakes.guard([host]) == [] and time.monotonic() - begun < 2


@pytest.mark.parametrize("text", ["", "x"])
def test_naming_needs_a_folder(text):
    with pytest.raises(ValueError):
        live_fakes.naming(text)
