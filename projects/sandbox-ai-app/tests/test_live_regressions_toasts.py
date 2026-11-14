"""Regressions from live use: short notices ("toasts") instead of a box with OK for what only says that something is
done. User: '"Geschrieben und vertraut: …/tor-network/.sandbox-ai.json" - so etwas brauchen wir
nicht als extra Box mit OK-Klick. Wenn, dann kurz anzeigen und 1 s später von alleine entfernen. In den Settings Box
komplett deaktivierbar.'"""
import ast
import json
import os
from pathlib import Path
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui  # noqa: E402

from sandbox_ai_app import applog, backend, i18n, short_notices as SN, widgets  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"
needs_checkout = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(autouse=True)
def notices(monkeypatch):
    monkeypatch.setattr(SN, "SHOWN", [])
    SN.set_enabled(True)
    yield SN.SHOWN
    SN.set_enabled(True)


@pytest.fixture
def shown_window(qapp):
    """A visible main window with a focused line edit and a status bar."""
    window = QtWidgets.QMainWindow()
    edit = QtWidgets.QLineEdit()
    window.setCentralWidget(edit)
    window.statusBar().showMessage("")
    window.resize(800, 500)
    window.show()
    window.activateWindow()
    edit.setFocus()
    qapp.processEvents()
    window.edit = edit
    yield window
    window.close()
    window.deleteLater()


def live(widget):
    try:
        return widget.isVisible()
    except RuntimeError:  # deleted: gone
        return False


# == the notice itself =================================================================================================

def test_a_short_notice_shows_near_the_bottom_takes_no_focus_and_goes_by_itself(qapp, shown_window, wait):
    """User: "Wenn, dann kurz anzeigen und 1 s später von alleine entfernen." - a small frame inside the window (no
    window of its own, no box, no OK), the focus stays where it was, about a second later it fades and is gone."""
    focused, active = qapp.focusWidget(), qapp.activeWindow()
    started = time.monotonic()
    notice = SN.short_notice(shown_window.edit, "Project configuration", "Written and trusted: /p/.sandbox-ai.json")
    qapp.processEvents()
    assert notice is not None and notice.isVisible() and not notice.isWindow()
    assert notice.window() is shown_window and notice.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
    assert qapp.focusWidget() is focused and qapp.activeWindow() is active, "a notice never takes the focus"
    assert not [w for w in qapp.topLevelWidgets() if isinstance(w, (QtWidgets.QMessageBox, QtWidgets.QDialog))
                and w.isVisible()], "no box"
    geometry = notice.geometry()
    assert geometry.bottom() <= shown_window.statusBar().geometry().top(), "above the status bar"
    assert geometry.bottom() > shown_window.height() * 2 // 3, "near the bottom"
    assert abs(geometry.center().x() - shown_window.width() // 2) <= 2
    # Screen readers: its title and text as name and description; the text is plain.
    assert notice.accessibleName() == "Project configuration"
    assert notice.accessibleDescription() == "Written and trusted: /p/.sandbox-ai.json"
    assert notice.label.textFormat() == QtCore.Qt.TextFormat.PlainText
    assert 1000 <= SN.lifetime(notice.text) <= 1500
    assert wait(lambda: not live(notice), 5)
    took = time.monotonic() - started
    assert 0.9 <= took <= 1000 / 1000 + (SN.FADE_MS + 900) / 1000, took
    assert qapp.focusWidget() is focused


def test_a_longer_text_stays_a_little_longer_four_seconds_at_most():
    assert SN.lifetime("Forgotten.") == SN.VISIBLE_MS
    assert SN.VISIBLE_MS < SN.lifetime("x" * 120) < SN.LONGEST_MS
    assert SN.lifetime("x" * 5000) == SN.LONGEST_MS == 4000


def test_the_pointer_over_a_notice_keeps_it_a_click_takes_it_away(qapp, shown_window, monkeypatch, wait):
    monkeypatch.setattr(SN, "VISIBLE_MS", 200)
    monkeypatch.setattr(SN, "HOVER_MS", 200)
    notice = SN.short_notice(shown_window, "Images", "Created.")
    qapp.sendEvent(notice, QtGui.QEnterEvent(QtCore.QPointF(2, 2), QtCore.QPointF(2, 2), QtCore.QPointF(2, 2)))
    assert not wait(lambda: not live(notice), 0.8), "kept while the pointer is over it"
    qapp.sendEvent(notice, QtCore.QEvent(QtCore.QEvent.Type.Leave))
    assert wait(lambda: not live(notice), 3)
    clicked = SN.short_notice(shown_window, "Images", "Created.")
    QtGui.QGuiApplication.processEvents()
    from PySide6.QtTest import QTest
    QTest.mouseClick(clicked, QtCore.Qt.MouseButton.LeftButton)
    assert wait(lambda: not live(clicked), 1)


def test_several_notices_stack_and_the_oldest_goes_when_too_many(qapp, shown_window):
    first = [SN.short_notice(shown_window, "T", f"Notice {n}") for n in range(SN.MAX_SHOWN)]
    qapp.processEvents()
    rects = [n.geometry() for n in first]
    assert all(not a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1:]), rects
    assert rects[-1].top() > rects[0].top(), "the newest lowest"
    more = SN.short_notice(shown_window, "T", "One more")
    qapp.processEvents()
    assert not live(first[0]) and live(more) and all(live(n) for n in first[1:])


def test_every_notice_goes_to_the_log_and_the_status_bar(qapp, shown_window):
    entries = []
    listener = applog.add_listener(entries.append)
    try:
        SN.short_notice(shown_window, "Login browser", "Forgotten.")
    finally:
        applog.remove_listener(listener)
    assert {"level": "INFO", "where": "notice", "message": "Login browser: Forgotten."}.items() <= entries[-1].items()
    assert shown_window.statusBar().currentMessage() == "Login browser: Forgotten."


def test_with_the_setting_off_nothing_pops_up_but_log_and_status_bar_have_it(qapp, shown_window, notices):
    """User: "In den Settings Box komplett deaktivierbar." """
    SN.set_enabled(False)
    entries = []
    listener = applog.add_listener(entries.append)
    try:
        assert SN.short_notice(shown_window, "Stop all", "Nothing runs that could be stopped.") is None
    finally:
        applog.remove_listener(listener)
    qapp.processEvents()
    assert notices == [] and not shown_window.findChildren(SN.Notice)
    assert entries[-1]["message"] == "Stop all: Nothing runs that could be stopped."
    assert shown_window.statusBar().currentMessage() == "Stop all: Nothing runs that could be stopped."


def test_the_setting_is_in_the_settings_dialog_in_english_and_german():
    assert backend.SETTINGS[SN.SETTING] is True, "on by default"
    assert i18n.TEXTS["Show short notices (toasts)"]["de"] == "Kurze Hinweise anzeigen"


# == the Settings dialog and the main window ===========================================================================

@needs_checkout
def test_the_settings_checkbox_switches_notices_off_at_once_and_is_kept(qapp, monkeypatch, tmp_path):
    from test_ui import window as _window  # noqa: F401
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    try:
        dialog = w.settings_dialog()
        box = dialog.fields[SN.SETTING]
        assert isinstance(box, QtWidgets.QCheckBox) and box.isChecked()
        assert box.text() == "Show short notices (toasts)"
        box.setChecked(False)
        assert w.sai.settings()[SN.SETTING] is False and not SN.enabled()
        box.setChecked(True)
        assert SN.enabled()
        box.setChecked(False)
        dialog.deleteLater()
    finally:
        w.shutdown()
        w.close()
        w.deleteLater()
    SN.set_enabled(True)
    again = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)  # started again: kept
    try:
        assert not SN.enabled()
    finally:
        again.shutdown()
        again.close()
        again.deleteLater()


# == the user's case: Write and trust, from the Start page and the Board =============================================

@needs_checkout
def test_board_apply_says_written_and_trusted_as_a_notice_not_a_box(qapp, tmp_path, monkeypatch, wait):
    """User: '"Geschrieben und vertraut: …/.sandbox-ai.json" - so etwas brauchen wir nicht als extra Box mit
    OK-Klick.' - the real Apply of the Board: the question (Write and trust) stays, the outcome is a notice."""
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.board_view import BoardPage
    page = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20})
    page.resize(900, 600)
    page.show()
    try:
        project = tmp_path / "tor-network"
        project.mkdir()
        assert page.open_project(str(project))
        page.set_prop(page.board.first("network").id, "--allow-pypi", True)
        assert page.apply()
        assert wait(lambda: not widgets.busy(page), 60)
        assert page.sai.review_project(project).trusted
        path = str(project / ".sandbox-ai.json")
        assert [title for title, _, _ in widgets.SHOWN] == ["Write and trust"], "the question only, no box after it"
        assert SN.SHOWN[-1] == ("Project configuration", f"Written and trusted: {path}")
        # (opened first: the folder has no project file yet - said as a notice too, no box)
        assert SN.SHOWN[0] == ("Board", f"{project} has no .sandbox-ai.json yet: Apply writes one.")
        notice = next(n for n in page.findChildren(SN.Notice) if n.text == f"Written and trusted: {path}")
        assert notice.isVisible()
        assert wait(lambda: not live(notice), 5), "gone by itself"
    finally:
        page.hide()
        page.deleteLater()


@needs_checkout
def test_start_page_write_and_trust_says_it_as_a_notice_in_german_too(qapp, tmp_path, monkeypatch, wait):
    from test_ui import window as _w  # noqa: F401 - (the Start page's fixture is made by hand below)
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    w.show()
    try:
        i18n.set_language("de")
        project = tmp_path / "tor-network"
        project.mkdir()
        page = w.launch
        page.form.widgets["--workspace"].set_value([str(project)])
        page.write_project()
        assert wait(lambda: (project / ".sandbox-ai.json").is_file() and not widgets.busy(page), 60)
        assert wait(lambda: SN.SHOWN, 10)
        path = str(project / ".sandbox-ai.json")
        assert SN.SHOWN[-1] == ("Projektkonfiguration", f"Geschrieben und vertraut: {path}"), SN.SHOWN
        assert [title for title, _, _ in widgets.SHOWN] == [i18n.tr("Write and trust")], widgets.SHOWN
        notice = w.findChildren(SN.Notice)[-1]
        assert notice.window() is w and notice.isVisible()
        assert w.statusBar().currentMessage() == f"Projektkonfiguration: Geschrieben und vertraut: {path}"
    finally:
        i18n.set_language("en")
        w.shutdown()
        w.close()
        w.deleteLater()


# == every pure confirmation is a notice; what needs reading stays a box ===============================================

# The texts (tr keys) that only say that something is done or how it is: said as short notices, never in a box.
CONVERTED = {
    "Written and trusted: {path}",
    "Trusted: {path}. The form starts in that folder; the launcher applies the file's options itself.",
    "Trusted on {host}: {path}",
    "Written: {path}",
    "Forgotten.",
    "Created.",
    "Nothing to remove.",
    "Removed:",
    "Nothing runs that could be stopped.",
    "No device was added to {run} during its run (devices given at the start stay).",
    "No workspace was added to {run} during its run (those of the start stay).",
    "{start} has no MCP server in its options.",
    "Imported as your preset {name}: it is under My presets in the gallery.",
    "{path} uses the kit {name} already.",
    "The kit has no skills, plugins or hooks.",
    "The language changes when the app is started again.",
}
DIALOGS = {"inform", "warn", "information"}


def calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            yield name, node


def keys_in(node):
    """Every tr("…") key and plain string inside NODE."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            yield inner.value


def test_no_pure_confirmation_is_a_box_any_more_each_is_a_short_notice():
    in_boxes, in_notices = {}, set()
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for name, node in calls(tree):
            args = [*node.args, *(k.value for k in node.keywords)]
            texts = {text for arg in args for text in keys_in(arg)}
            if name in DIALOGS:
                for text in texts & CONVERTED:
                    in_boxes.setdefault(text, []).append(f"{path.name}:{node.lineno}")
            elif name == "short_notice":
                in_notices |= texts
    assert not in_boxes, f"still a box with OK: {in_boxes}"
    assert CONVERTED <= in_notices, CONVERTED - in_notices


def test_the_command_outcomes_that_only_report_are_notices():
    """The success branch of ui.report() (device removed, limits set, mcp add), the attach and detach of an MCP
    server, Restore HOME without skips, a server's properties, mcp start/stop, Remove preset directory: notices."""
    source = (PACKAGE / "ui.py").read_text()
    report = source[source.index("def report(parent, title, result, error):"):]
    report = report[:report.index("\n\n\n")]
    assert "short_notice(parent, title, (out + err).strip()" in report and "warn(parent, title" in report
    for marker in ("def live_mcp_done(self, host, start, title, result, error):", "def detached(self, host, title,",
                   "def restored(self, title, result, error):", "def attached(self, name, result, error):",
                   "def server_done(self, action, result, error, quiet=True):"):
        body = source[source.index(marker):]
        body = body[:body.index("\n    def ")]
        assert "short_notice" in body, marker
    settings = (PACKAGE / "settings.py").read_text()
    done = settings[settings.index("    def done(self, result, error):"):]
    assert "short_notice(self, tr(\"Preset directories\"), text" in done[:done.index("\n    def ")]
    reports = (PACKAGE / "reports.py").read_text()
    written = reports[reports.index('text = tr("Written to {path}.", path=printable(result))'):]
    assert "short_notice(parent, title, text)" in written[:300], "a report written: a notice"


@needs_checkout
def test_converted_call_sites_show_a_notice_instead_of_a_box(qapp, tmp_path, monkeypatch, wait):
    """A sample of the real call sites, run: each says its outcome as a notice, none opens a box."""
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import login_browser, presets as PR, terminal, ui
    from sandbox_ai_app.preset_gallery import PresetGallery
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    w = ui.MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    w.show()
    boxes = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, lambda *a, **k: boxes.append(a))
    try:
        # Stop all with nothing running
        monkeypatch.setattr(w.sandboxes, "stop_all_targets", lambda: {})
        assert w.sandboxes.stop_all() is False
        assert SN.SHOWN[-1] == ("Stop all", "Nothing runs that could be stopped.")
        # Settings: the login browser forgotten (asked first: the question stays)
        dialog = w.settings_dialog()
        monkeypatch.setattr(login_browser, "forget", lambda: None)
        dialog.forget_login_browser()
        assert SN.SHOWN[-1] == ("Login browser", "Forgotten.")
        dialog.deleteLater()
        # ui.report(): a command that worked is a notice; one that failed stays a box
        ui.report(w, "Limits", (0, "Limits of claude-abc set.\n", ""), None)
        assert SN.SHOWN[-1] == ("Limits", "Limits of claude-abc set.")
        ui.report(w, "Limits", (1, "", "refused"), None)
        assert widgets.SHOWN[-1] == ("Limits", "refused", None)
        # Export of a preset
        gallery = PresetGallery(w.sai, parent=w)
        gallery.selected = type("Card", (), {"preset": PR.find("locked")})()
        target = tmp_path / "locked.json"
        assert gallery.export_preset(str(target)) == str(target)
        assert SN.SHOWN[-1] == ("Export the preset", f"Written: {target}")
        titles = [title for title, _, _ in widgets.SHOWN]
        assert titles == ["Login browser", "Limits"], widgets.SHOWN  # the question to forget, the failure
        assert not any(title.startswith("Export") for title in titles) and not boxes
    finally:
        w.shutdown()
        w.close()
        w.deleteLater()


@needs_checkout
def test_a_board_drop_of_a_preset_file_says_imported_as_a_notice(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app import presets
    from sandbox_ai_app.board_view import BoardPage
    page = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20})
    try:
        path = tmp_path / "offline.json"
        presets.export(page.sai, presets.find("locked"), str(path))
        monkeypatch.setattr(page, "choose_drop", lambda choices, point=None: "preset")
        assert page.drop_paths([str(path)], QtCore.QPointF(0, 0)) == "preset"
        assert widgets.SHOWN == [] and "Imported as your preset" in SN.SHOWN[-1][1]
        json.loads(path.read_text())
    finally:
        page.deleteLater()
