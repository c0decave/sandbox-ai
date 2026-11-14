"""Stability round 1 (0.1.95): what a long or busy use of the app met - each test failed before its fix."""
import json
import os

import pytest

from sandbox_ai_app import backend
from test_ui import window  # noqa: F401 - the window of the UI tests (with this file's env)

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def test_a_damaged_run_record_does_not_stop_the_scan_of_the_questions(env):
    """live_runs (the Approvals page's scan, every second, on the GUI thread too): one run.json nested deeper than
    Python reads raised RecursionError there - every scan failed, so no question of any run was shown - and one with a
    PID beyond any number raised OverflowError (sandbox-ai's net.alive)."""
    runs = env / "runtime" / "sandbox-ai" / "runs"
    for name, text in (("deep", "[" * 100000 + "]" * 100000), ("huge", json.dumps({"run": "huge", "pid": 10 ** 30})),
                       ("live", json.dumps({"run": "live", "pid": os.getpid()}))):
        (runs / name / "approvals").mkdir(parents=True)
        (runs / name / "run.json").write_text(text)
    sai = backend.SandboxAI()
    found = sai.live_runs()
    assert "live" in found and "deep" not in found
    question = {"id": "1700000000-1-1", "time": "2026-11-03T10:00:00Z", "timeout": 600, "kind": "dns",
                "name": "a.example"}
    (runs / "live" / "approvals" / "1700000000-1-1.json").write_text(json.dumps(question))
    assert [q.get("id") for _, _, q in sai.questions()] == ["1700000000-1-1"]


def close_soon(qapp, kind, after=150):
    """Reject the shown dialog of class KIND a moment later (from inside the loop of its exec())."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    def close():
        shown = [w for w in QApplication.topLevelWidgets() if isinstance(w, kind) and w.isVisible()]
        if not shown:
            QTimer.singleShot(50, close)
            return
        for dialog in shown:
            dialog.reject()
    QTimer.singleShot(after, close)


def test_dialogs_made_anew_for_each_use_go_once_answered(qapp, window):
    """Settings…, the preset gallery (the Board's and the Start page's), the wizard and Save as preset are made anew each time, as
    children of their window: closed, each stayed for as long as the window lived - Settings alone about 400 widgets,
    1500 Qt objects and 4 MB each time it was opened (a soak: +21 top-level windows a round)."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from sandbox_ai_app.preset_gallery import PresetGallery, SavePresetDialog
    from sandbox_ai_app.settings import SettingsDialog
    from sandbox_ai_app.wizard import SandboxWizard
    w = window
    w.show()
    kinds = (SettingsDialog, PresetGallery, SandboxWizard, SavePresetDialog)

    def alive():
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        return {kind.__name__: sum(isinstance(x, kind) for x in QApplication.allWidgets()) for kind in kinds}
    before = alive()
    for _ in range(3):
        close_soon(qapp, SettingsDialog)
        w.open_settings()
        close_soon(qapp, PresetGallery)
        w.board.open_gallery()
        close_soon(qapp, SandboxWizard)
        w.board.open_wizard(ask=False)
        close_soon(qapp, PresetGallery)
        w.launch.open_preset_gallery()
        close_soon(qapp, SavePresetDialog)
        w.board.save_as_preset()
    assert alive() == before
