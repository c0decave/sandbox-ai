"""The Terminals page gives the keyboard to its terminal (the user 2026-11-24: "wenn man auf Terminals umschaltet,
sollte das ausgewählte Terminal-Tab-Fenster direkt aktiv werden. Meist ist es das noch nicht und dann tippt man und
nichts passiert"): shown from another page - by the navigation, Go, a click into its row - and after another tab is
chosen, the selected tab's terminal has the focus at once. Offscreen in the whole window, with real key events: with
the vendored xterm.js what is typed arrives in that tab's command (the stand-in takes no keys: the focus is checked)."""
import os
import shutil
import sys
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore  # noqa: E402
from sandbox_ai_app import backend  # noqa: E402

from conftest import open_tab  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests

HERE = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
# What each tab runs: every key it gets, shown (raw: no line to finish first).
ECHO = [sys.executable, "-c", "import os, sys, tty; tty.setraw(0); print('ready', end='\\r\\n', flush=True)\n"
        "while True:\n    b = os.read(0, 1)\n    sys.stdout.write('[' + b.decode(errors='replace') + ']')\n"
        "    sys.stdout.flush()"]


@pytest.fixture(params=["stand-in", "vendored"])
def web(request, tmp_path, qapp):
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    if request.param == "vendored":
        missing = [name for name in terminal.VENDOR_FILES if not (Path(terminal.WEB) / "vendor" / name).is_file()]
        if missing:  # (terminal.available is the window fixture's "no" here)
            pytest.skip(f"needs xterm.js vendored by scripts/vendor_xterm.py: {', '.join(missing)}")
        return terminal.WEB
    folder = tmp_path / "web"
    (folder / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(HERE.parent / "sandbox_ai_app" / "web" / name, folder / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(HERE / "fake_xterm" / name, folder / "vendor" / name)
    return folder


def js(view, wait, script, timeout=20):
    result = {}
    view.run_js(script, lambda value: result.setdefault("value", value))
    wait(lambda: "value" in result, timeout)
    return result.get("value")


def screen(view, wait):
    return js(view, wait, "window.sandboxTerminal ? window.sandboxTerminal.text() : ''", 5) or ""


def focused_in(qapp, view):
    focus = qapp.focusWidget()
    return focus is not None and (focus is view.view or view.view.isAncestorOf(focus))


def type_into(window, qapp, text):
    from PySide6.QtTest import QTest
    for char in text:
        QTest.keyClick(window.windowHandle(), char)
        qapp.processEvents()


@pytest.fixture
def terminals(window, web, wait, qapp, monkeypatch):  # noqa: F811
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    page = window.terminals
    page.web = web
    window.resize(1200, 800)
    window.show()
    window.activateWindow()
    qapp.processEvents()
    window.show_page("terminals")
    views = [open_tab(page, ECHO, f"t{n}") for n in range(2)]
    assert all(views), "no terminal tab (Chromium's sandbox cannot start here)"
    for view in views:
        assert wait(lambda v=view: v.bridge.ready_seen, 30), "the page never connected"
        assert wait(lambda v=view: "ready" in screen(v, wait), 30)
    yield page, views, web == terminal.WEB
    page.close_all()


def test_the_selected_terminal_has_the_keyboard_when_the_page_is_shown(window, terminals, wait, qapp):  # noqa: F811
    page, views, real = terminals
    page.tabs.setCurrentWidget(views[0])
    for how in ("navigation", "go", "show_page"):
        window.show_page("files")
        window.nav.setFocus()  # (a click into the navigation took the focus there)
        assert wait(lambda: not focused_in(qapp, views[0]), 5)
        if how == "navigation":
            window.nav.setCurrentRow(window.nav_rows["terminals"])
        elif how == "go":
            window.actions["page:terminals"].trigger()
        else:
            window.show_page("terminals")
        assert wait(lambda: focused_in(qapp, views[0]), 10), (how, qapp.focusWidget())
        if real:
            letter = {"navigation": "a", "go": "b", "show_page": "c"}[how]
            type_into(window, qapp, letter)
            assert wait(lambda: f"[{letter}]" in screen(views[0], wait), 10), (how, screen(views[0], wait))


def test_another_tab_chosen_has_the_keyboard_at_once(window, terminals, wait, qapp):  # noqa: F811
    page, views, real = terminals
    page.tabs.setCurrentWidget(views[0])
    assert wait(lambda: focused_in(qapp, views[0]), 10)
    page.tabs.tabBar().setFocus()  # (a click on a tab)
    page.tabs.setCurrentWidget(views[1])
    assert wait(lambda: focused_in(qapp, views[1]), 10), qapp.focusWidget()
    if real:
        type_into(window, qapp, "x")
        assert wait(lambda: "[x]" in screen(views[1], wait), 10), screen(views[1], wait)
        assert "[x]" not in screen(views[0], wait)
    # Alt+1 (the first tab): its terminal has the keyboard, not the tab's frame around it.
    page.show_tab(0)
    assert wait(lambda: focused_in(qapp, views[0]), 10), qapp.focusWidget()
    if real:
        type_into(window, qapp, "y")
        assert wait(lambda: "[y]" in screen(views[0], wait), 10), screen(views[0], wait)
