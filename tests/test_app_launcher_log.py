"""The app's start script (sandbox-ai-app) started from a terminal: the app goes into the background with its output in
app.log; the logs of the five starts before are kept (a crash's stacks survive a few restarts), and a start while the
app runs moves nothing (that start only brings the running app's window to the front)."""
import json
import os
from pathlib import Path
import pty
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def fake_python(tmp_path):
    """A python3 that has PySide6 (for the script's checks) and, as the app, writes one line and ends."""
    script = tmp_path / "python3"
    script.write_text("#!/bin/sh\ncase \"$*\" in *'import PySide6'*) exit 0;; esac\necho the-app-ran\nexit 0\n")
    script.chmod(0o755)
    return script


def start(tmp_path, env):
    master, slave = pty.openpty()
    try:
        run = subprocess.run([str(ROOT / "sandbox-ai-app")], stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                             env=env, timeout=60, text=True)
    finally:
        os.close(master)
        os.close(slave)
    return run


def wait_for(path, text):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if path.exists() and text in path.read_text():
            return True
        time.sleep(0.05)
    return False


def test_five_logs_are_kept_and_a_running_app_keeps_its_log(tmp_path):
    state = tmp_path / "state" / "sandbox-ai"
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "SANDBOX_AI_APP_PYTHON": str(fake_python(tmp_path)),
           "SANDBOX_AI_APP_NO_WEBENGINE": "1", "HOME": str(tmp_path)}
    env.pop("SANDBOX_AI_APP_FOREGROUND", None)
    for number in range(7):
        assert start(tmp_path, env).returncode == 0
        assert wait_for(state / "app.log", "the-app-ran")
        (state / "app.log").write_text(f"start {number}\n")
    logs = sorted(p.name for p in state.glob("app.log*"))
    assert logs == ["app.log", "app.log.1", "app.log.2", "app.log.3", "app.log.4", "app.log.5"]
    assert (state / "app.log.1").read_text() == "start 5\n" and (state / "app.log.5").read_text() == "start 1\n"
    # The app runs (its record names a live process): nothing is moved, the start's line is added to its log.
    (state / "app-run.json").write_text(json.dumps({"pid": os.getpid(), "started": 1.0}))
    before = {p.name: p.read_text() for p in state.glob("app.log*")}
    assert start(tmp_path, env).returncode == 0
    assert wait_for(state / "app.log", "the-app-ran")
    after = {p.name: p.read_text() for p in state.glob("app.log*")}
    assert after["app.log"].startswith("start 6\n") and {k: v for k, v in after.items() if k != "app.log"} == \
        {k: v for k, v in before.items() if k != "app.log"}
