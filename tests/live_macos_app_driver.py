"""Settings > Updates of a sandbox-ai.app, driven as a human does it - for tests/test_live_macos.py, run with the
app's own Python and its own code (its sandbox_ai_app and PySide6, set up as Contents/MacOS/sandbox-ai-app does):

    python3 -I -B live_macos_app_driver.py PYSIDE_DIR APP_DIR NEW OLD

Check now, then Install now… (its question answered yes) until the panel reads NEW as installed, then Roll back…
until it reads OLD again. Each step is printed as a JSON line ({"step", ...}); the last line is the result:
{"step": "end", "installed": [after the install, after the rollback], "warnings": [...], "problem": text or None}.
Only what 0.1.154's panel has is used (UpdatesPanel, its buttons, data, widgets.AUTO_ANSWER), so the old app of the
check runs it. The offscreen platform (QT_QPA_PLATFORM): the panel is never shown on the screen.
"""
import json
import sys
import time

pyside, app_dir, NEW, OLD = sys.argv[1:5]
sys.path[0:0] = [app_dir, pyside]

from PySide6.QtWidgets import QApplication  # noqa: E402

qt = QApplication(["sandbox-ai-app"])
from sandbox_ai_app import backend, updates_view, widgets  # noqa: E402

widgets.AUTO_ANSWER = True  # Install now's and Roll back's questions: yes
warnings = []
updates_view.warn = lambda parent, title, text, *rest, **more: warnings.append(f"{title}: {text}")


def say(**fields):
    print(json.dumps(fields, default=str), flush=True)


def wait(condition, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qt.processEvents()
        if condition():
            return True
        time.sleep(0.05)
    qt.processEvents()
    return condition()


def data():
    return panel.data or {}


def end(problem=None):
    say(step="end", installed=installed, warnings=warnings, problem=problem, data=data())
    sys.exit(0)


installed = []
panel = updates_view.UpdatesPanel(backend.SandboxAI())
if not wait(lambda: panel.data is not None, 180):
    end("the panel did not read the update mode (update mode --json) in 180 s")
say(step="read", data=data())
panel.check_button.click()
if not wait(lambda: (data().get("result") or {}).get("available") == NEW, 300):
    end(f"Check now did not find {NEW}")
say(step="checked", data=data())
if not panel.install_button.isEnabled():
    end(f"Install now… is greyed with {NEW} available")
panel.install_button.click()
wait(lambda: data().get("installed") == NEW or warnings, 1900)
installed.append(data().get("installed"))
say(step="installed", data=data(), status=panel.status.text())
if data().get("installed") != NEW:
    end(f"Install now… did not install {NEW}")
if not wait(lambda: panel.rollback_button.isEnabled(), 60):
    end("Roll back… is greyed after the install")
panel.rollback_button.click()
wait(lambda: data().get("installed") == OLD or len(warnings) > 0, 600)
installed.append(data().get("installed"))
say(step="rolled back", data=data(), status=panel.status.text())
end(None if data().get("installed") == OLD else f"Roll back… did not bring back {OLD}")
