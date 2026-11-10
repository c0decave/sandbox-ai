"""The record of a run (crashguard): a run that ended without quitting is said at the next start, with what was known
of it and the out-of-memory killer's lines where the kernel's log tells; a clean end leaves nothing."""
import json
import os
import stat
import subprocess
import sys

from sandbox_ai_app import applog, crashguard


def test_a_clean_end_leaves_nothing_and_a_live_run_is_no_crash(tmp_path):
    path = tmp_path / "app-run.json"
    guard = crashguard.Guard(path, read_kernel=lambda since: "").start()
    record = json.loads(path.read_text())
    assert record["pid"] == os.getpid() and stat.S_IMODE(path.stat().st_mode) == 0o600
    guard.tick("board")
    assert json.loads(path.read_text())["page"] == "board"
    again = crashguard.Guard(path, read_kernel=lambda since: "").start()
    assert again.last is None, "this process is alive: no unexpected end"
    again.stop()
    assert not path.exists()


def test_a_run_killed_from_outside_is_said_with_the_oom_killers_lines(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(applog, "log", lambda level, where, message: logged.append((level, where, message)))
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    path = tmp_path / "app-run.json"
    path.write_text(json.dumps({"pid": gone.pid, "started": 1000.0, "seen": 1700000000.0, "app": "0.1.85",
                                "memory_mb": 3120, "peak_mb": 3300, "page": "board"}))
    kernel = (f"[123.4] Out of memory: Killed process {gone.pid} (python3) total-vm:9000kB\n"
              f"[123.5] oom_reaper: reaped process {gone.pid + 100000}\n")
    guard = crashguard.Guard(path, read_kernel=lambda since: kernel).start()
    try:
        assert "ended without quitting" in guard.last and "3120 MiB" in guard.last and "board" in guard.last
        assert f"Killed process {gone.pid}" in guard.last and str(gone.pid + 100000) not in guard.last
        assert logged and logged[0][:2] == ("WARNING", "crash")
    finally:
        guard.stop()


def test_without_the_kernels_log_it_says_it_was_ended_from_outside(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, "log", lambda *a: None)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    path = tmp_path / "app-run.json"
    path.write_text(json.dumps({"pid": gone.pid, "started": 1.0}))
    guard = crashguard.Guard(path, read_kernel=lambda since: "").start()
    assert "ended from outside" in guard.last
    guard.stop()


def test_a_crash_is_said_with_what_it_left_in_the_log_also_after_the_start_script_moved_it(tmp_path, monkeypatch):
    """A segfault leaves faulthandler's lines in app.log; the start script moves the last run's log to app.log.1 before
    the next start: the next start quotes them (it said "No Python error was written" without looking)."""
    logged = []
    monkeypatch.setattr(applog, "log", lambda level, where, message: logged.append(message))
    state = tmp_path / "sandbox-ai"
    script = ("import sys; sys.path.insert(0, %r)\n"
              "from sandbox_ai_app import applog, crashguard\n"
              "applog.install()\n"
              "crashguard.Guard(read_kernel=None).start()\n"
              "applog.log('INFO', 'board', 'switching pages')\n"
              "import ctypes; ctypes.string_at(0)\n") % str(__import__("pathlib").Path(applog.__file__).parents[1])
    done = subprocess.run([sys.executable, "-c", script], env={**os.environ, "XDG_STATE_HOME": str(tmp_path)},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    (state / "app.log").rename(state / "app.log.1")  # what sandbox-ai-app (the start script) does at each start
    (state / "app.log").write_text(f"2026-11-10T10:00:00.000+00:00 INFO [start] pid {os.getpid()}, thread x\n")
    guard = crashguard.Guard(state / "app-run.json", read_kernel=None).start()
    try:
        assert "It crashed" in guard.last and "Fatal Python error: Segmentation fault" in guard.last
        assert "Current thread" in guard.last and "No Python error" not in guard.last and guard.last in logged
    finally:
        guard.stop()
    assert crashguard.crash_lines(999999999, [state / "app.log.1"]) == [], "another process's: nothing"


def test_a_damaged_or_linked_record_is_no_crash(tmp_path):
    path = tmp_path / "app-run.json"
    path.write_text("{damaged")
    assert crashguard.Guard(path, read_kernel=None).start().last is None
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"pid": 999999, "started": 1.0}))
    path.unlink()
    path.symlink_to(other)
    assert crashguard.read_record(path) is None


def test_the_memory_warning_is_said_once(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(applog, "log", lambda level, where, message: logged.append(where))
    monkeypatch.setattr(crashguard, "memory_mb", lambda pid=None: crashguard.WARN_MB + 1)
    guard = crashguard.Guard(tmp_path / "r.json", read_kernel=None).start()
    guard.tick("files")
    guard.tick("files")
    assert logged.count("memory") == 1
    guard.stop()
