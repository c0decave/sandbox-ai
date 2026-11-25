"""New bridge… clicked for real in the app's window, in every state the Sessions page can be in (2026-11-22: "when I
press New bridge in the app nothing happens"): no session, one, two, sessions only on hosts, a host that is slow or
answers garbage, the sessions still being read, an error in the click, German, after a page switch, from the right
click. Each click shows something - the dialog, or a message that says why not, or a line on the page that says what
runs - and the bridge is checked and made through the real dialog and the real question (no AUTO_ANSWER, no
exec_dropped stand-in).

The sessions are stand-ins here (the backend's instances); tests/test_bridge_e2e_processes.py and the live check
bridge-live (scripts/live_check.sh) make the bridge between real sessions."""
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import backend, bridges_view, i18n, widgets  # noqa: E402
from conftest import read_now  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the fixtures
from test_bridges_app import remote_sessions, sessions  # noqa: E402

pytestmark = pytest.mark.skipif(not __import__("os").environ.get("SANDBOX_AI_HOME") and
                                not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def shown_windows():
    """The app's windows other than the main window that are shown now: dialogs and message boxes."""
    return [w for w in QtWidgets.QApplication.topLevelWidgets()
            if w.isVisible() and isinstance(w, QtWidgets.QDialog)]


def words(widget):
    if isinstance(widget, QtWidgets.QMessageBox):
        return widget.text()
    found = [label.text() for label in widget.findChildren(QtWidgets.QLabel) if label.isVisible()]
    found += [edit.toPlainText() for edit in widget.findChildren(QtWidgets.QPlainTextEdit) if edit.isVisible()]
    return "\n".join(found)


def close_all():
    for dialog in shown_windows():
        dialog.reject()


def click(button, wait, look=None, seconds=1.0):
    """A real mouse click on BUTTON; LOOK(shown) runs while what it opened is open (a dialog's exec() waits in a loop
    of its own), then everything shown is closed. Returns [(type, title, text)] of what was shown."""
    seen = []

    def see():
        shown = shown_windows()
        seen.extend((type(w).__name__, w.windowTitle(), words(w)) for w in shown)
        if look is not None:
            look(shown)
        close_all()
    QtCore.QTimer.singleShot(int(seconds * 1000), see)
    assert button.isVisible(), "the button is shown"
    QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
    wait(lambda: bool(seen), seconds + 5)
    close_all()
    return seen


def page_with(window, monkeypatch, wait, local, hosts=(), host_instances=None, read=True):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)  # real dialogs and questions
    monkeypatch.setattr(window.sai, "instances", lambda: (list(local), []))
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: list(hosts))
    monkeypatch.setattr(window.sai, "host_instances",
                        host_instances or (lambda name, *args, **kwargs: remote_sessions(name)))
    monkeypatch.setattr(window.sai, "host_bridges", lambda name, *args, **kwargs: [])
    monkeypatch.setattr(window.sai, "bridges", lambda include_all=False: [])
    window.show()
    window.show_page("sandboxes")
    page = window.sandboxes
    if read:
        read_now(page)
        if hosts:
            page.remote_names = list(hosts)
    return page


@pytest.fixture
def real_window(window):
    yield window
    close_all()
    widgets.AUTO_ANSWER = True  # the window's teardown asks no real questions


@pytest.mark.parametrize("count", [0, 1])
def test_too_few_sessions_the_click_says_why(real_window, env, monkeypatch, wait, count):
    """Disabled with a tooltip, a click on New bridge… did nothing at all - the user's report. It can be clicked,
    and says how many run and what a bridge needs."""
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:count])
    box = page.bridges_box
    assert box.buttons["new"].isEnabled(), "a click explains instead of nothing"
    seen = click(box.buttons["new"], wait)
    assert [(kind, title) for kind, title, _ in seen] == [("QMessageBox", "New bridge")]
    assert f"{count} run now" in seen[0][2] and "two agent sessions" in seen[0][2]


def test_two_sessions_the_dialog_opens_in_front(real_window, env, monkeypatch, wait):
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:2])
    looked = {}

    def look(shown):
        dialog = next(w for w in shown if isinstance(w, bridges_view.BridgeDialog))
        looked["geometry"] = dialog.frameGeometry()
        looked["screen"] = dialog.screen().availableGeometry() if dialog.screen() else None
        looked["a"], looked["b"] = dialog.a.currentData(), dialog.b.currentData()
    seen = click(page.bridges_box.buttons["new"], wait, look)
    assert [(kind, title) for kind, title, _ in seen] == [("BridgeDialog", "New bridge")]
    assert (looked["a"], looked["b"]) == ("alpha", "beta")
    assert looked["screen"] is None or looked["screen"].intersects(looked["geometry"]), "on the screen"


def test_while_the_sessions_are_read_the_click_waits_for_them(real_window, env, monkeypatch, wait):
    """Before the page's first read came back New bridge… was disabled ("0 run now", which was not true either). The
    click says that the sessions are being read, on the page, and the dialog opens once they are."""
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:2], read=False)
    page.loaded = False
    page.instances = {}
    box = page.bridges_box
    box.update_buttons()
    assert box.buttons["new"].isEnabled()
    QTest.mouseClick(box.buttons["new"], QtCore.Qt.MouseButton.LeftButton)
    assert box.status.isVisible() and "being read" in box.status.text()
    assert not shown_windows()
    seen = []
    QtCore.QTimer.singleShot(1500, lambda: seen.extend(shown_windows()) or close_all())
    read_now(page)
    assert wait(lambda: bool(seen), 10)
    assert isinstance(seen[0], bridges_view.BridgeDialog) and not box.status.isVisible()


def test_sessions_only_on_a_host_the_dialog_offers_them(real_window, env, monkeypatch, wait):
    page = page_with(real_window, monkeypatch, wait, [], hosts=["host1"])
    looked = {}

    def look(shown):
        dialog = next(w for w in shown if isinstance(w, bridges_view.BridgeDialog))
        wait(lambda: dialog.a.count() >= 2, 10)
        looked["names"] = [dialog.a.itemData(i) for i in range(dialog.a.count())]
    seen = click(page.bridges_box.buttons["new"], wait, look)
    assert seen and seen[0][0] == "BridgeDialog" and looked["names"] == ["host1:api", "host1:web"]


def test_a_slow_host_holds_up_nothing_and_is_said(real_window, env, monkeypatch, wait):
    release = threading.Event()

    def slow(name, *args, **kwargs):
        release.wait(30)
        return remote_sessions(name)
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:1], hosts=["host1"], host_instances=slow)
    looked = {}

    def look(shown):
        dialog = next(w for w in shown if isinstance(w, bridges_view.BridgeDialog))
        looked["note"] = dialog.hosts_note.text() if dialog.hosts_note.isVisible() else ""
    began = time.monotonic()
    seen = click(page.bridges_box.buttons["new"], wait, look, seconds=1.0)
    release.set()
    assert seen and seen[0][0] == "BridgeDialog" and time.monotonic() - began < 6
    assert "Asking your hosts for their running sessions: host1" in looked["note"]


@pytest.mark.parametrize("garbage", [{"not": "a list"}, 5, "text", [1, None, "x"],
                                     [{"name": "web", "kind": "session", "state": "running",
                                       "workspace": {"a": 1}, "agent": ["x"], "run": 7}]])
def test_a_host_that_answers_garbage_is_said_and_never_asked_for_ever(real_window, env, monkeypatch, wait, garbage):
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:1], hosts=["host1"])
    monkeypatch.setattr(real_window.sai, "host_json", lambda name, *args, **kwargs: garbage)
    monkeypatch.setattr(real_window.sai, "host_instances", backend.SandboxAI.host_instances.__get__(real_window.sai))
    looked = {}

    def look(shown):
        dialog = next(w for w in shown if isinstance(w, bridges_view.BridgeDialog))
        wait(lambda: not dialog.asking, 10)
        looked["asking"] = set(dialog.asking)
        looked["values"] = [dialog.a.itemData(i) for i in range(dialog.a.count())]
        for name in looked["values"]:  # whatever it listed can be chosen and read
            dialog.b.setCurrentIndex(dialog.b.findData(name))
            assert isinstance(dialog.peer_workspace(), str)
    seen = click(page.bridges_box.buttons["new"], wait, look, seconds=1.0)
    assert seen and seen[0][0] == "BridgeDialog" and looked["asking"] == set()
    assert all(isinstance(name, str) for name in looked["values"])


def test_an_error_in_the_click_is_shown_not_only_logged(real_window, env, monkeypatch, wait):
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:2])

    def broken(*args, **kwargs):
        raise RuntimeError("the dialog could not be made")
    monkeypatch.setattr(bridges_view, "BridgeDialog", broken)
    seen = click(page.bridges_box.buttons["new"], wait)
    assert [(kind, title) for kind, title, _ in seen] == [("QMessageBox", "New bridge")]
    assert "the dialog could not be made" in seen[0][2]


def test_in_german_too(real_window, env, monkeypatch, wait):
    i18n.set_language("de")
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:1])
    seen = click(page.bridges_box.buttons["new"], wait)
    assert [(kind, title) for kind, title, _ in seen] == [("QMessageBox", "Neue Brücke")]
    assert "jetzt laufen 1" in seen[0][2]
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:2])
    seen = click(page.bridges_box.buttons["new"], wait)
    assert seen[0][:2] == ("BridgeDialog", "Neue Brücke")


def test_after_a_page_switch(real_window, env, monkeypatch, wait):
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:2])
    real_window.show_page("approvals")
    real_window.show_page("sandboxes")
    seen = click(page.bridges_box.buttons["new"], wait)
    assert seen and seen[0][0] == "BridgeDialog"


def test_from_the_right_click_of_the_bridges(real_window, env, monkeypatch, wait):
    """The right click on the list of bridges offers the buttons' commands (widgets.list_menu): New bridge… there
    opens the dialog as the button does."""
    from test_bridges_app import ROWS
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:2])
    monkeypatch.setattr(real_window.sai, "bridges", lambda include_all=False: [dict(r) for r in ROWS])
    box = page.bridges_box
    box.refresh()
    assert wait(lambda: box.table.rowCount() == 2 and box.table.isVisible(), 10)
    menu = widgets.menu_of(box.table)
    action = next(a for a in menu.actions() if a.text() == "New bridge…")
    assert action.isEnabled()
    seen = []
    QtCore.QTimer.singleShot(1000, lambda: seen.extend(shown_windows()) or close_all())
    action.trigger()
    assert wait(lambda: bool(seen), 10)
    assert isinstance(seen[0], bridges_view.BridgeDialog)
    menu.deleteLater()


def test_the_whole_way_through_the_real_dialog_and_question(real_window, env, monkeypatch, wait):
    """Filled in, Check and create… clicked: the check is said on the page while the launcher's dry run runs, its
    warning is asked about in a real question, Create bridge clicked, the bridge made and said."""
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:2])
    box = page.bridges_box
    checking, plans, added = threading.Event(), [], []

    def plan(args):
        plans.append(args)
        checking.wait(20)
        return {"id": None, "state": "planned", "dry_run": True, "a": "alpha", "b": "beta", "grants": {},
                "warnings": ["data can flow from alpha (more restricted) to beta, which reaches what alpha cannot: "
                             "host example.org"]}
    monkeypatch.setattr(real_window.sai, "bridge_plan", plan)
    adding = threading.Event()

    def add(args):
        added.append(args)
        adding.wait(20)
        return {"id": "abcdef01", "state": "active", "a": "alpha", "b": "beta", "notes": []}
    monkeypatch.setattr(real_window.sai, "bridge_add", add)
    steps = {}

    def fill():
        dialog = next(w for w in shown_windows() if isinstance(w, bridges_view.BridgeDialog))
        QTest.mouseClick(dialog.message, QtCore.Qt.MouseButton.LeftButton)
        dialog.read.edit.setPlainText("src")
        QTest.mouseClick(dialog.ok, QtCore.Qt.MouseButton.LeftButton)
        steps["filled"] = True
    QtCore.QTimer.singleShot(800, fill)
    QTest.mouseClick(box.buttons["new"], QtCore.Qt.MouseButton.LeftButton)
    assert wait(lambda: steps.get("filled") and plans, 10)
    assert wait(lambda: box.status.isVisible() and "Checking" in box.status.text(), 5), \
        "the dry run is said on the page while it runs"
    asked = {}

    def answer():  # (the question waits in a loop of its own: answered from the event loop, as a human does)
        boxes = [w for w in shown_windows() if isinstance(w, QtWidgets.QMessageBox)]
        if not boxes:
            QtCore.QTimer.singleShot(50, answer)
            return
        question = boxes[-1]
        asked["title"], asked["text"] = question.windowTitle(), question.text()
        create = next(b for b in question.buttons() if b.text() == "Create bridge")
        QTest.mouseClick(create, QtCore.Qt.MouseButton.LeftButton)
    QtCore.QTimer.singleShot(50, answer)
    checking.set()
    assert wait(lambda: "text" in asked, 10)
    assert asked["title"] == "New bridge" and "Create this bridge?" in asked["text"]
    assert "data can flow from alpha" in asked["text"]
    assert wait(lambda: added and box.status.isVisible() and "Making the bridge alpha → beta" in box.status.text(),
                10), "the add is said on the page while it runs"
    adding.set()
    assert wait(lambda: added and any(isinstance(w, QtWidgets.QMessageBox) and "is active" in w.text()
                                      for w in shown_windows()), 10)
    assert added == [["bridge", "add", "alpha", "beta", "--allow", "message,files:ro:src", "--ask=first"]]
    assert wait(lambda: not box.status.isVisible(), 5)


def test_the_live_checks_app_driver_makes_the_bridge(real_window, env, monkeypatch, wait, qapp):
    """tests/live_bridge_app_driver.py (the app's part of the live check bridge-live) on this window: what it runs
    against two real sessions there runs here against stand-ins - the dialog opened, both chosen, the real question
    answered, the bridge's id read from the app's message."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "live_bridge_app_driver", Path(__file__).resolve().parents[3] / "tests" / "live_bridge_app_driver.py")
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    page_with(real_window, monkeypatch, wait, sessions(env)[:3])
    plans, added = [], []
    monkeypatch.setattr(real_window.sai, "bridge_plan", lambda args: plans.append(args) or {
        "id": None, "state": "planned", "dry_run": True, "a": "guard", "b": "alpha", "grants": {}, "warnings": []})
    monkeypatch.setattr(real_window.sai, "bridge_add", lambda args: added.append(args) or {
        "id": "0a1b2c3d", "state": "active", "a": "guard", "b": "alpha", "notes": []})
    steps = []
    result = driver.drive(qapp, real_window, "guard", "alpha", lambda **fields: steps.append(fields), seconds=10)
    assert result["problem"] is None and result["opened"] and result["id"] == "0a1b2c3d", result
    assert added == [["bridge", "add", "guard", "alpha", "--allow", "message", "--ask=first"]]
    assert [step["step"] for step in steps] == ["listed", "dialog", "question"]
    nobody = driver.drive(qapp, real_window, "guard", "nobody", seconds=1)
    assert nobody["problem"].startswith("the Sessions page did not list both sessions")


def test_with_no_bridge_the_right_click_on_its_line_offers_new_bridge(real_window, env, monkeypatch, wait):
    """With no bridge the list is hidden; a right click on the line that says so offered nothing."""
    page = page_with(real_window, monkeypatch, wait, sessions(env)[:2])
    box = page.bridges_box
    assert box.empty.isVisible() and not box.table.isVisible()
    menus, original = [], widgets.exec_dropped
    monkeypatch.setattr(widgets, "exec_dropped", lambda menu, *args: menus.append(menu))
    box.empty.customContextMenuRequested.emit(QtCore.QPoint(2, 2))
    monkeypatch.setattr(widgets, "exec_dropped", original)  # (the dialog's own exec below is the real one)
    assert len(menus) == 1
    action = next(a for a in menus[0].actions() if a.text() == "New bridge…")
    assert action.isEnabled()
    seen = []
    QtCore.QTimer.singleShot(1000, lambda: seen.extend(shown_windows()) or close_all())
    action.trigger()
    assert wait(lambda: bool(seen), 10)
    assert isinstance(seen[0], bridges_view.BridgeDialog)
    menus[0].deleteLater()
