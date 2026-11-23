"""After an update, through the real entry point (sandbox-ai's sandbox-ai-app launcher, offscreen): an older app that
runs makes way for the start of the new one; one too old to be asked is named (version, folder, process) and its window
comes to the front; sandbox-ai-app --version says both versions and where they run from. The user's case (2026-11-18):
the system installation's 0.1.97 ran, the user's own 0.1.99 started - and showed 0.1.97."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

pytest.importorskip("PySide6.QtNetwork", reason="PySide6 is not installed")

from sandbox_ai_app import instance_probe  # noqa: E402
from test_single_instance import OLD_APP, Running, fake_install  # noqa: E402

APP_ROOT = Path(__file__).resolve().parents[1]  # projects/sandbox-ai-app
SAI = Path(os.environ.get("SANDBOX_AI_HOME") or APP_ROOT.parents[1])
LAUNCHER = SAI / "sandbox-ai-app"
CURRENT = instance_probe.__version__

pytestmark = pytest.mark.skipif(not (LAUNCHER.is_file() and (SAI / "sandbox" / "agent_sandbox.py").is_file()),
                                reason="needs sandbox-ai's checkout (SANDBOX_AI_HOME) with its sandbox-ai-app launcher")


@pytest.fixture
def runtime():
    """$XDG_RUNTIME_DIR of the apps started here (the socket's path must be short)."""
    folder = Path(tempfile.mkdtemp(prefix="sie-"))
    folder.chmod(0o700)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def environment(runtime, **extra):
    env = {key: value for key, value in os.environ.items() if key != "SANDBOX_AI_APP_ANOTHER"}
    env.update(QT_QPA_PLATFORM="offscreen", XDG_RUNTIME_DIR=str(runtime), SANDBOX_AI_HOME=str(SAI),
               SANDBOX_AI_APP_PYTHON=sys.executable, SANDBOX_AI_APP_FOREGROUND="1", SANDBOX_AI_APP_NO_INTRO="1",
               SANDBOX_AI_APP_NO_TOUR="1", SANDBOX_AI_APP_NO_WEBENGINE="1", **extra)
    return env


def start(runtime, *args, launcher=LAUNCHER):
    return subprocess.Popen(["bash", str(launcher), *args], env=environment(runtime), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def answers(socket, version, seconds=90):
    """The app that answers on SOCKET within SECONDS has VERSION (via "info")."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        running = instance_probe.ask(socket)
        if running is not None and running.protocol == "info" and running.version == version:
            return running
        time.sleep(0.3)
    return None


def ended(process, seconds=60):
    """(exit status, out, err) of PROCESS within SECONDS; (None, out, err) when it did not end: killed then - with its
    process group when it leads one of its own (a launcher's app, which keeps the output pipes open after the
    launcher itself is gone: reading them to their end waited forever, and the test with it)."""
    try:
        out, err = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        try:
            own_group = os.getpgid(process.pid) == process.pid
        except OSError:
            own_group = False
        if own_group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        try:
            out, err = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # a child elsewhere still holds the pipes: what was read is gone
            process.wait(10)
            out, err = "", f"(did not end within {seconds} s; killed, its output still held open by another process)"
        return None, out, err
    return process.returncode, out, err


def test_sandbox_ai_app_version_says_both_versions_and_folders(runtime):
    code, out, err = ended(start(runtime, "--version"))
    assert code == 0, err
    lines = out.splitlines()
    package = (APP_ROOT / "sandbox_ai_app").resolve()
    assert lines[0] == f"sandbox-ai-app {CURRENT} from {package}"
    assert lines[1] == f"sandbox-ai {CURRENT} from {(SAI / 'sandbox').resolve()}" or \
        lines[1] == f"sandbox-ai {CURRENT} from {SAI / 'sandbox'}", out
    assert not list(runtime.iterdir()), "no app was started (nothing listens)"


def test_an_app_too_old_to_make_way_is_named(runtime):
    old = Running(OLD_APP, fake_install(runtime / "usr-local-lib", "0.1.97"), runtime / instance_probe.NAME)
    try:
        code, _, err = ended(start(runtime))
        assert code == 0, err
        assert "sandbox-ai-app 0.1.97 is still running" in err and f"kill {old.process.pid}" in err, err
        assert f"to use {CURRENT}" in err
        time.sleep(0.5)
    finally:
        out = old.stop()
    assert out == ["asked", "show"], "its window to the front, so it can be quit"


def test_an_older_app_from_another_installation_makes_way(runtime):
    """The running app: a copy of this app as version 0.1.97 in another installation's folder, the real app with its
    window; the start of the current one asks it to quit, it quits the usual way (nothing runs: no question), and the
    new one is the app."""
    tree = runtime / "system" / "versions" / "0.1.97"
    shutil.copytree(APP_ROOT / "sandbox_ai_app", tree / "projects" / "sandbox-ai-app" / "sandbox_ai_app",
                    ignore=shutil.ignore_patterns("__pycache__", "vendor"))
    init = tree / "projects" / "sandbox-ai-app" / "sandbox_ai_app" / "__init__.py"
    init.write_text(init.read_text().replace(f'"{CURRENT}"', '"0.1.97"'))
    shutil.copy2(LAUNCHER, tree / "sandbox-ai-app")
    socket = runtime / instance_probe.NAME
    older = start(runtime, launcher=tree / "sandbox-ai-app")
    newer = None
    try:
        found = answers(socket, "0.1.97")
        assert found is not None, ended(older, 5)
        assert found.path == str((tree / "projects" / "sandbox-ai-app" / "sandbox_ai_app").resolve())
        newer = start(runtime)
        code, _, err = ended(older, 60)
        assert code == 0, err[-3000:]
        now = answers(socket, CURRENT, 90)
        assert now is not None, "the new start never became the app"
        assert now.pid == newer.pid or now.pid in child_pids(newer.pid), now
        assert now.sandbox_ai == CURRENT
    finally:
        for process in (older, newer):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                ended(process, 30)


def child_pids(pid):
    """The launcher execs Python (same PID); kept for a launcher that would not."""
    try:
        return [int(p) for p in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]
    except OSError:
        return []
