"""Hotfix 0.1.96, the second check of the sandbox's own browser (user: "bitte auch den Browser / Browser mit
Tor-Support in-depth prüfen"), the app's part: one test per bug found, named after it.

The real app (MainWindow offscreen, its Sandboxes & sessions page reading a session and its run as sandbox-ai records
them - test_live_regressions_browser.py's fixtures) and, where this machine has them, the browser container's real
display: Xvfb and x11vnc started by sandbox-ai's own runtime.start_gui on the run's socket, as browser_runtime.py
starts them in the container (only the browser binary is missing here; xev stands in as the window that receives
what the Browser tab sends)."""
import importlib.util
import os
from pathlib import Path
import random
import shutil
import subprocess
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from sandbox_ai_app import vnc_view as V  # noqa: E402

from test_ui import window  # noqa: E402,F401 - the page's fixtures
from test_live_regressions_browser import HOME, browser_tabs, env, logged, no_windows_left, sessions  # noqa: E402,F401
from conftest import OPENED_URLS, open_tab, read_now  # noqa: E402
from test_terminal_privacy import bar_text, choices, press, sai, stub_web, terminal  # noqa: E402,F401

DISPLAY_TOOLS = all(shutil.which(tool) for tool in ("Xvfb", "x11vnc", "setpriv", "xev"))
needs_display = pytest.mark.skipif(not (DISPLAY_TOOLS and HOME),
                                   reason="needs SANDBOX_AI_HOME and Xvfb, x11vnc, setpriv, xev (the browser image's)")


def sandbox_runtime():
    spec = importlib.util.spec_from_file_location("sandbox_runtime_for_app_hotfix", Path(HOME) / "sandbox/runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_display():
    for _ in range(200):
        number = random.randint(150, 900)
        if not Path(f"/tmp/.X11-unix/X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():
            return f":{number}"
    pytest.skip("no free display number")


class RealDisplay:
    """The browser container's display on SOCKET (RUN.browser-vnc/vnc.sock): runtime.start_gui's Xvfb and x11vnc,
    and xev's window at its top left (the stand-in for the browser's), whose events go to EVENTS."""

    def __init__(self, socket_path, events):
        self.runtime = sandbox_runtime()
        self.socket, self.events = Path(socket_path), Path(events)
        self.socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket.parent.chmod(0o700)
        self.runtime.VNC_DIR = self.socket.parent
        self.kept_drop = None
        self.display, self.processes, self.xev = free_display(), [], None
        self.start()

    def start(self):
        if os.geteuid() != 0 and self.kept_drop is None:
            # The browser container's runtime runs as root and drops every capability (setpriv); a test as a user
            # cannot (setpriv's 127, CI) and has none to drop: the display itself is what this test is about.
            self.kept_drop, self.runtime.DROP_ALL = self.runtime.DROP_ALL, []
        self.processes = self.runtime.start_gui({"display": self.display, "screen": "320x200x24", "vnc": True,
                                                 "private": True, "selection": True})
        stream = self.events.open("ab")
        self.xev = subprocess.Popen(["xev", "-display", self.display, "-geometry", "200x150+0+0", "-event",
                                     "keyboard", "-event", "button"], stdout=stream, stderr=subprocess.DEVNULL)
        stream.close()
        deadline = time.monotonic() + 10
        while "MapNotify" not in self.text() and time.monotonic() < deadline:  # (no event of that kind is asked)
            if subprocess.run(["xwininfo", "-display", self.display, "-name", "Event Tester"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(0.1)

    def text(self):
        return self.events.read_text(errors="replace") if self.events.exists() else ""

    def stop(self):
        if getattr(self, "kept_drop", None) is not None:
            self.runtime.DROP_ALL, self.kept_drop = self.kept_drop, None
        for process in [self.xev, *reversed(self.processes)]:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        for stale in (Path(f"/tmp/.X11-unix/X{self.display[1:]}"), Path(f"/tmp/.X{self.display[1:]}-lock")):
            if stale.exists():
                stale.unlink()

    def crash(self):
        """x11vnc ends (killed): the browser's runtime starts the display again on the same socket."""
        self.processes[1].kill()
        self.processes[1].wait()
        self.stop()
        self.start()


@pytest.fixture
def real_display(tmp_path):
    made = []

    def make(path):
        made.append(RealDisplay(path, tmp_path / f"xev-{len(made)}.txt"))
        return made[-1]
    yield make
    for display in made:
        display.stop()


def opened(window, page, session, wait):  # noqa: F811
    read_now(page)
    page.open_browser(page.instances[f"session:{session.name}"])
    tabs = browser_tabs(window)
    assert [text for _, text, _ in tabs] == [f"Browser: {session.name}"], tabs
    panel = tabs[0][2]
    assert wait(lambda: "Connected" in panel.status.text(), 15), panel.status.text()
    return panel


def click_and_type(panel, qapp):
    view = panel.view
    view.resize(640, 400)  # the display (320x200) scaled by two
    where = QPointF(view.target().x() + 100, view.target().y() + 100)  # the display's (50, 50): xev's window
    for kind, buttons in ((QMouseEvent.Type.MouseMove, Qt.MouseButton.NoButton),
                          (QMouseEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                          (QMouseEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
        button = Qt.MouseButton.LeftButton if kind != QMouseEvent.Type.MouseMove else Qt.MouseButton.NoButton
        QtWidgets.QApplication.sendEvent(view, QMouseEvent(kind, where, where, button, buttons,
                                                           Qt.KeyboardModifier.NoModifier))
    for kind in (QKeyEvent.Type.KeyPress, QKeyEvent.Type.KeyRelease):
        QtWidgets.QApplication.sendEvent(view, QKeyEvent(kind, Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier, "q"))
    qapp.processEvents()


# == The Browser tab with the real display ==========================================================================

@needs_display
def test_the_browser_tab_shows_the_real_display_and_its_mouse_and_keys_reach_the_browser(window, env, sessions,  # noqa: F811
                                                                                        real_display, wait, qapp):
    """Executed end to end with the display the browser's container runs (the earlier checks used a stand-in RFB
    server): the tab connects to the real x11vnc on the run's socket, shows its picture (xev's white window at the
    top left where the browser's would be) and what you click and type there arrives in that window."""
    session = sessions()
    display = real_display(session.vnc)
    panel = opened(window, window.sandboxes, session, wait)
    image = panel.client.image
    assert (image.width(), image.height()) == (320, 200)
    assert wait(lambda: panel.client.image.pixelColor(100, 70).name() == "#ffffff", 10), "xev's window is shown"
    click_and_type(panel, qapp)
    assert wait(lambda: "ButtonPress" in display.text() and "keysym 0x71, q" in display.text(), 10), display.text()


# == A display that ended and came back: the tab stayed "closed" ===================================================

@needs_display
def test_the_browser_tab_connects_again_by_itself_when_its_display_comes_back(window, env, sessions,  # noqa: F811
                                                                             real_display, wait, qapp, logged):
    """When the browser container's x11vnc (or Xvfb) ended, its runtime starts the display again on the same socket
    (browser_runtime.py) - but the tab only said "The browser's display was closed (the run ended?)" and stayed so
    until Reconnect was clicked. Now, while its start runs, it connects again by itself and says so meanwhile."""
    session = sessions()
    display = real_display(session.vnc)
    panel = opened(window, window.sandboxes, session, wait)
    display.processes[1].kill()
    display.processes[1].wait()
    assert wait(lambda: "Connected" not in panel.status.text(), 10)
    assert wait(lambda: "again" in panel.status.text(), 5), panel.status.text()
    display.stop()
    display.start()
    assert wait(lambda: "Connected" in panel.status.text(), 20), panel.status.text()
    click_and_type(panel, qapp)
    assert wait(lambda: "keysym 0x71, q" in display.text(), 10), "the new display gets what is typed"
    said = [e["message"] for e in logged if e["where"] == "browser"]
    assert sum("connected to the browser's display" in line for line in said) == 2, said


# == A web UI in the sandbox (DSH's, Cairn's): its link in the terminal opened on this computer =====================

DSH_URL = "http://127.0.0.1:3080/?token=4f9c2d1e8b7a6f5e"


def test_a_sandbox_address_from_its_terminal_is_never_opened_on_this_computer(terminal, stub_web, sai, qapp):
    """`sandbox-ai dsh` serves DeepSeek Harness' web UI on the sandbox's loopback and prints its URL with a token
    (http://127.0.0.1:3080/?token=...); Cairn's server is http://127.0.0.1:8000. A click on that link in the run's
    terminal tab opened it in this computer's browser - this computer's 127.0.0.1, not the sandbox's: nothing there,
    or another program (of any user of this machine) that listens on that port and gets the token. A loopback address
    from a sandbox's tab is now never opened here: the bar says why, with Copy link (and, for a start with a browser
    of its own, Open in the sandbox's browser: below). A tab that is not a sandbox's (sandbox-ai web on this
    computer) opens its loopback links as before."""
    page = terminal.TerminalsPage(stub_web, sai=sai)
    run = open_tab(page, sai.command("dsh"), "dsh: app")
    attached = open_tab(page, sai.command("attach", "pentest"), "pentest")
    remote = open_tab(page, ["ssh", "-t", "box", "exec sandbox-ai attach far"], "far")
    here = open_tab(page, sai.command("web"), "web interface")
    try:
        sai.runs_now = {"dsh-1": {"run": "dsh-1", "app_tab": run.tab_tag},
                        "claude-2": {"run": "claude-2", "session": "pentest", "name": "pentest"}}
        remote.start_host, remote.start_name = "box", "far"  # (a start on a host of sandbox-ai hosts, over SSH)
        before = len(OPENED_URLS)
        for view, url in ((run, DSH_URL), (attached, "http://localhost:8000/board"), (remote, "http://[::1]:3080/")):
            view.open_link(url)
            assert len(OPENED_URLS) == before, f"{url} opened on this computer"
            assert "address of the sandbox" in bar_text(view) and "this computer" in bar_text(view), bar_text(view)
            assert choices(view) == ["Copy link", "Cancel"], choices(view)
        press(run, "Copy link")
        assert QtWidgets.QApplication.clipboard().text() == DSH_URL
        run.open_link("https://example.org/docs")  # not the sandbox's own address: as before
        assert OPENED_URLS[before].toString() == "https://example.org/docs"
        here.open_link("http://127.0.0.1:8384/?token=x")  # sandbox-ai web on this computer: its address is here
        assert OPENED_URLS[before + 1].toString() == "http://127.0.0.1:8384/?token=x"
    finally:
        page.close_all()
        page.deleteLater()


@needs_display
def test_a_sandbox_address_opens_in_the_sandbox_browser(window, env, sessions, real_display, wait, qapp,  # noqa: F811
                                                        logged):
    """The start has a browser of its own (--browser): the bar offers Open in the sandbox's browser, which opens
    its Browser tab and types the address there (Ctrl+L, the URL, Enter: Chromium's and Firefox's address bar) - the
    browser that shares the sandbox's loopback reaches DSH's and Cairn's web UIs. With the real display: the keys
    arrive in the window that has the focus there."""
    from test_live_regressions_browser import terminal_tab
    session = sessions()
    display = real_display(session.vnc)
    read_now(window.sandboxes)
    view = terminal_tab(window, env, session.name)
    before = len(OPENED_URLS)
    view.open_link(DSH_URL)
    assert len(OPENED_URLS) == before
    assert [button.text() for button in view.bar.buttons] == ["Open in the sandbox's browser", "Copy link", "Cancel"]
    panel_view = window.terminals.browsers
    assert view.bar.choose("Open in the sandbox's browser")
    tabs = browser_tabs(window)
    assert [text for _, text, _ in tabs] == [f"Browser: {session.name}"], tabs
    panel = tabs[0][2]
    assert wait(lambda: "Connected" in panel.status.text(), 15), panel.status.text()
    panel.view.resize(640, 400)
    where = QPointF(panel.view.target().x() + 100, panel.view.target().y() + 100)
    QtWidgets.QApplication.sendEvent(panel.view, QMouseEvent(QMouseEvent.Type.MouseMove, where, where,
                                                             Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                                             Qt.KeyboardModifier.NoModifier))
    assert wait(lambda: "keysym 0xff0d, Return" in display.text(), 10), display.text()[-2000:]
    typed = display.text()
    assert "keysym 0xffe3, Control_L" in typed and "keysym 0x6c, l" in typed
    for keysym in ("0x3a, colon", "0x3f, question", "0x3d, equal", "0x34, 4", "0x66, f"):
        assert f"keysym {keysym}" in typed, keysym
    said = [e["message"] for e in logged if e["where"] in ("browser", "terminal")]
    assert not any("4f9c2d1e8b7a6f5e" in line for line in said), "the token stays out of app.log"
    assert panel_view.find(f"session:{session.name}") is panel


# == Verified (no bug): a resumed session's new display ==============================================================

@needs_display
def test_the_browser_tab_follows_a_resumed_session_to_its_new_display(window, env, sessions, real_display, wait,  # noqa: F811
                                                                     qapp):
    """sandbox-ai resume: the session runs again with a new run, whose browser has a display at a new socket
    (NEW_RUN.browser-vnc/vnc.sock). The open tab follows it at the next read of Sandboxes & sessions and what is
    typed reaches the new display - executed with the real displays."""
    import json
    import shutil as _shutil
    from test_live_regressions_browser import browser_module
    session = sessions()
    first = real_display(session.vnc)
    panel = opened(window, window.sandboxes, session, wait)
    first.stop()
    _shutil.rmtree(session.run_dir)
    run = "claude-0f0f0f0f"
    session.run_dir = session.run_dir.with_name(run)
    (session.run_dir / "approvals").mkdir(parents=True)
    session.run_dir.chmod(0o700)
    a = __import__("types").SimpleNamespace(browser="chromium", browser_control=None, browser_profile=None,
                                            browser_container={"name": "browser"}, control_dir=session.run_dir,
                                            run_id=run)
    session.record = dict(session.record, run=run, browser=browser_module().record(a, session.record["pod"]))
    (session.run_dir / "run.json").write_text(json.dumps(session.record))
    second = real_display(session.vnc)
    read_now(window.sandboxes)
    assert panel.path == str(session.vnc)
    assert wait(lambda: "Connected" in panel.status.text(), 15), panel.status.text()
    assert wait(lambda: panel.client.image.pixelColor(100, 70).name() == "#ffffff", 10), "the new display is shown"
    click_and_type(panel, qapp)
    assert wait(lambda: "keysym 0x71, q" in second.text(), 10), second.text()[-1000:]
    assert [text for _, text, _ in browser_tabs(window)] == [f"Browser: {session.name}"]
