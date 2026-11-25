"""Stability round 2 (0.1.95): what a long or busy use of the app met - each test failed before its fix."""
import ast
import os
from pathlib import Path

import pytest

from sandbox_ai_app import backend, widgets
from test_ui import env, window  # noqa: F401 - the window of the UI tests (with its env)

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
APP = Path(__file__).resolve().parents[1] / "sandbox_ai_app"


def settle(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


def close_soon(qapp, kind, how, after=100):
    """HOW(widget) on the shown top-level widget of class KIND a moment later (from inside the loop of its exec())."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    def close():
        shown = [w for w in QApplication.topLevelWidgets() if isinstance(w, kind) and w.isVisible()]
        if not shown:
            QTimer.singleShot(30, close)
            return
        for widget in shown:
            how(widget)
    QTimer.singleShot(after, close)


def test_a_confirmation_goes_once_answered(qapp, monkeypatch):
    """ask_first/confirm (every Remove…, Stop…, Give…): its message box was a child of its page and stayed, closed,
    for as long as the window lived - one more with each question answered."""
    from PySide6.QtWidgets import QMessageBox, QWidget
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    page = QWidget()
    try:
        for _ in range(3):
            close_soon(qapp, QMessageBox, lambda box: box.reject())
            assert widgets.confirm(page, "Remove", "Remove it?", "Remove") is False
        settle(qapp)
        assert page.findChildren(QMessageBox) == []
    finally:
        page.deleteLater()
        settle(qapp)


def test_context_menus_go_once_closed(qapp):
    """The Copy menu of every table and list (copy_menu) and the menu of a label cut short (ElidedLabel): each right
    click left its QMenu behind as a child of the view."""
    from PySide6.QtGui import QStandardItem, QStandardItemModel
    from PySide6.QtWidgets import QMenu, QTableView
    from sandbox_ai_app import copying
    view = QTableView()
    model = QStandardItemModel(1, 1)
    model.setItem(0, 0, QStandardItem("cell"))
    view.setModel(model)
    label = widgets.ElidedLabel()
    label.setText("a text long enough")
    try:
        for _ in range(3):
            close_soon(qapp, QMenu, lambda menu: menu.close())
            copying.copy_menu(view, view.visualRect(model.index(0, 0)).center())
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QContextMenuEvent
        for _ in range(3):
            close_soon(qapp, QMenu, lambda menu: menu.close())
            label.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(1, 1), QPoint(1, 1),
                                                     Qt.KeyboardModifier.NoModifier))
        settle(qapp)
        assert view.findChildren(QMenu) == [] and label.findChildren(QMenu) == []
    finally:
        view.deleteLater()
        label.deleteLater()
        settle(qapp)


def test_the_pages_dialogs_go_once_answered(qapp, window, monkeypatch):  # noqa: F811
    """Add a host, New kit, New marketplace, Pull an image, Start a team: each dialog, made anew each time as a
    child of its page, stayed there closed for as long as the window lived (each about a hundred widgets)."""
    from PySide6.QtWidgets import QDialog
    from sandbox_ai_app import hosts_page, images_page, kits_page, team_page
    w = window
    w.show()
    sites = ((w.hosts, hosts_page.AddHostDialog, w.hosts.add), (w.kits, kits_page.NewKitDialog, w.kits.new_kit),
             (w.images, images_page.PullDialog, w.images.pull_image), (w.team, team_page.StartDialog, w.team.start))
    for _, kind, _ in sites:
        monkeypatch.setattr(kind, "exec", lambda self: QDialog.DialogCode.Rejected)
    for page, kind, opened in sites:
        before = len(page.findChildren(kind))
        for _ in range(3):
            opened()
        settle(qapp)
        assert len(page.findChildren(kind)) == before, kind.__name__


def made_and_run(path):
    """(line, name) of every NAME.exec(...) in PATH whose NAME was made in the same function (a dialog, a message box,
    a menu made anew for this one use) and is not freed there - by widgets.exec_dropped, drop_after_exec,
    deleteLater or WA_DeleteOnClose."""
    text = path.read_text()
    found = []
    for func in ast.walk(ast.parse(text)):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # (A QDrag is deleted by Qt itself once its drag has ended.)
        made = {node.targets[0].id for node in ast.walk(func) if isinstance(node, ast.Assign)
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, (ast.Call, ast.IfExp)) and "QDrag(" not in ast.unparse(node.value)}
        source = ast.get_source_segment(text, func)
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "exec" \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in made - {"loop", "app"}:
                name = node.func.value.id
                if not any(k in source for k in (f"{name}.deleteLater", f"drop_after_exec({name}",
                                                  "WA_DeleteOnClose")):
                    found.append((node.lineno, name))
    return found


def test_every_dialog_and_menu_made_for_one_use_is_freed():
    """The rest of the dialogs, questions and context menus made anew each time (about 50: the Credentials, Hosts,
    Images, Kits, Team, Identities, MCP and Sessions pages, the Board's guide and forms, the presets' forms, the
    copy menus): each exec()'d one stayed a child of its window. Every such exec() frees what it ran now - the file
    manager's own too (its review pass 13)."""
    left = {path.name: found for path in sorted(APP.glob("*.py")) for found in [made_and_run(path)] if found}
    assert left == {}


def test_the_soaks_watchdog_reads_the_gui_threads_stack_without_holding_its_frames():
    """The open segmentation fault of round 1 (a timer of a freed object, inside the event loop, no Python frame):
    the soak's own watchdog thread took the GUI thread's frames (sys._current_frames) during a stall and held them
    until the next one. A frame that outlives its function keeps its locals; dropped in the watchdog thread, it freed
    there what it held last - a dialog's Python object once Qt had deleted the dialog, with the parentless Qt objects
    it kept (a model, a delegate) - and a Qt object deleted outside its thread leaves its QBasicTimers registered
    (Qt 6 stops them silently only in their own thread): their next tick used freed memory. The watchdog reads the
    stack as faulthandler writes it now; no frame object leaves the GUI thread."""
    import inspect
    import io
    import tempfile
    import threading
    import faulthandler
    import soak_app
    assert "_current_frames" not in inspect.getsource(soak_app.Soak.watchdog)
    seen = []

    def look():
        with tempfile.TemporaryFile("w+") as scratch:
            faulthandler.dump_traceback(scratch, all_threads=True)
            scratch.seek(0)
            seen.append(soak_app.main_stack(scratch.read(), f"{threading.main_thread().ident:#018x}"))

    def waiting_here():
        thread = threading.Thread(target=look)
        thread.start()
        thread.join(10)
    waiting_here()
    assert seen and seen[0] and "waiting_here" in seen[0], seen
    assert soak_app.main_stack(io.StringIO("").read(), "0x1") is None
