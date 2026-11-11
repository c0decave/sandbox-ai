"""The app under a real window manager (spectrwm on Xvfb, sandbox-ai's tests/devtools/wm_check.py): every top-level
window it maps - the main window and each panel floated out, also one a drag left with Qt's bypass hint - is managed
by the window manager (no override-redirect window drawing over every workspace, WM_STATE set). Skipped without
Xvfb, spectrwm, xwininfo and xprop (the dev image variant has them: sandbox-ai images recipe dev)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
SAI = Path(os.environ.get("SANDBOX_AI_HOME") or HERE.parents[2])
CHECK = SAI / "tests" / "devtools" / "wm_check.py"

pytestmark = pytest.mark.skipif(
    not CHECK.is_file() or not all(shutil.which(tool) for tool in ("Xvfb", "spectrwm", "xwininfo", "xprop")),
    reason="needs Xvfb, spectrwm and x11-utils (the dev image variant), and sandbox-ai's tests/devtools")


def test_every_window_of_the_app_is_managed_by_spectrwm(tmp_path):
    env = {key: value for key, value in os.environ.items() if key not in ("QT_QPA_PLATFORM", "WAYLAND_DISPLAY")}
    env.update(SANDBOX_AI_HOME=str(SAI), SANDBOX_AI_APP_ANOTHER="1", HOME=str(tmp_path),
               XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_STATE_HOME=str(tmp_path / "state"),
               XDG_DATA_HOME=str(tmp_path / "data"), XDG_CACHE_HOME=str(tmp_path / "cache"))
    done = subprocess.run([sys.executable, str(CHECK), "--display", ":96", "--settle", "8",
                           "--screenshot", str(tmp_path / "screen.xwd"), "--",
                           sys.executable, str(HERE / "float_docks_driver.py")],
                          env=env, capture_output=True, text=True, timeout=180)
    assert done.returncode in (0, 1, 3), done.stdout + done.stderr  # 3: no window at all (fails below)
    report = json.loads(done.stdout)
    assert report["program_running"], done.stderr[-3000:]
    assert len(report["windows"]) >= 2, report  # the main window and the floated panels
    assert report["unmanaged"] == [], json.dumps(report["unmanaged"], indent=1)
