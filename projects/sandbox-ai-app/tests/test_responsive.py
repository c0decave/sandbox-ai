"""The window keeps answering while what a click started runs (user report: "I click install plugin and everything
hangs"). The backend's slow calls are faked to sleep SLOW seconds; a QTimer that ticks every 20 ms must tick at least
every MAX_GAP seconds from the click until what it started has finished, the click itself returns at once, and the
slow call ran on another thread than the GUI's. Kits & plugins (a kit change, Install plugins), MCP add, image
builds, hosts update and install, the LLM servers' Test connection, a copy on the Files page, starts (start, stop,
resume). A long kit change shows a busy bar with Cancel and what sandbox-ai writes as it comes; Cancel ends the
command; a second click starts nothing; the window's status bar says which pages have work running."""
import copy
import os
import shutil
from pathlib import Path
import sys
import threading
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QTimer  # noqa: E402

from sandbox_ai_app import backend, widgets  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures
from test_kits_page import INVENTORY, MARKETPLACES  # noqa: E402

HERE = Path(__file__).resolve().parent
SLOW = 2.0  # seconds a faked slow call takes
MAX_GAP = 0.2  # seconds the event loop may stand still at most


def slow(result, calls=None, seconds=SLOW):
    """A stand-in for a slow backend method: it sleeps, records the thread it ran on, and returns RESULT."""
    calls = [] if calls is None else calls

    def call(*args, **kwargs):
        calls.append(("began", threading.current_thread() is threading.main_thread(), args))
        time.sleep(seconds)
        calls.append(("ended", args))
        return result(*args, **kwargs) if callable(result) else result
    call.calls = calls
    return call


def responsive(qapp, click, finished, calls, timeout=20):
    """CLICK, then the event loop until FINISHED(): it never stands still for MAX_GAP (from the click on, the click
    itself included); the slow call (CALLS) ran off the GUI thread and has ended."""
    stamps = []
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: stamps.append(time.monotonic()))
    timer.start()
    began = time.monotonic()
    click()
    returned = time.monotonic() - began
    while not finished() and time.monotonic() - began < timeout:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    ended = time.monotonic()
    timer.stop()
    points = [began, *stamps, ended]
    gap = max(b - a for a, b in zip(points, points[1:]))
    assert finished(), "what the click started has finished"
    assert calls and calls[0][0] == "began" and calls[0][1] is False, f"the slow call ran on the GUI thread: {calls}"
    assert any(entry[0] == "ended" for entry in calls), "the slow call has ended"
    # (the click may wait in a local event loop that goes on: what counts is that the loop never stands still)
    assert gap < MAX_GAP, f"the event loop stood still for {gap:.2f} s (the click took {returned:.2f} s)"
    assert ended - began >= SLOW * 0.9, "it waited for the slow call (what the click started)"


@pytest.fixture
def kits(window, monkeypatch, wait):
    """The Kits page with the fake inventory loaded."""
    monkeypatch.setattr(window.sai, "has_kits_command", lambda: True)
    monkeypatch.setattr(window.sai, "kits_inventory", lambda: copy.deepcopy(INVENTORY))
    monkeypatch.setattr(window.sai, "has_marketplaces", lambda: True)
    monkeypatch.setattr(window.sai, "marketplaces", lambda: copy.deepcopy(MARKETPLACES))
    window.kits.refresh()
    assert wait(lambda: window.kits.table.rowCount() == 4 and not widgets.busy(window.kits), 10)
    return window.kits


def test_a_kit_change_runs_in_the_background_with_a_busy_bar_and_one_at_a_time(qapp, window, kits, monkeypatch):
    page = kits
    window.show_page("kits")
    change = slow((0, "added egm@sandbox-ai", ""))
    monkeypatch.setattr(window.sai, "kit_command", change)
    seen = {}

    def click():
        page.run("add-plugin", "mine", "egm@sandbox-ai", options=["--agent=claude"], title="Add plugin")
        seen["task"] = page.task.isVisibleTo(page) and page.task.cancel_button.isVisibleTo(page)
        seen["buttons"] = not page.plugin_button.isEnabled() and not page.refresh_button.isEnabled()
        seen["status"] = window.busy_label.text()
        page.run("add-plugin", "mine", "other@sandbox-ai", options=["--agent=claude"], title="Add plugin")
        seen["second"] = page.message.text()

    responsive(qapp, click, lambda: not page.task.running, change.calls)
    assert seen["task"] and seen["buttons"], "a busy bar with Cancel; the buttons that change kits are off"
    assert seen["status"] == "Running: Kits & plugins…", "the window says which page works"
    assert seen["second"] == "A change is running: wait for it, or cancel it."
    assert [c[2][:2] for c in change.calls if c[0] == "began"] == [("add-plugin", "mine")], "the second click ran nothing"
    assert "added egm@sandbox-ai" in page.message.text() and window.busy_label.isHidden()


def test_a_kit_change_streams_its_output_and_cancel_ends_it(qapp, window, kits, monkeypatch, wait):
    """The real call() path: a command that writes a line and then hangs (a marketplace clone that does not come):
    its line shows while it runs, Cancel ends it (its process group), and the page says it was cancelled."""
    page = kits
    script = ("import sys, time\nprint('Cloning acme/plugins', flush=True)\n"
              "print('remote: Counting objects', file=sys.stderr, flush=True)\ntime.sleep(60)\n")
    command = window.sai.command
    monkeypatch.setattr(window.sai, "command", lambda *args: [sys.executable, "-c", script]
                        if args[:2] == ("kits", "add-plugin") else command(*args))
    began = time.monotonic()
    page.run("add-plugin", "mine", "egm@sandbox-ai", options=["--agent=claude"], title="Add plugin")
    assert page.task.running and time.monotonic() - began < MAX_GAP
    assert wait(lambda: "Counting objects" in page.task.output.toPlainText(), 20), "what it writes, as it comes"
    assert "Cloning acme/plugins" in page.task.output.toPlainText() and page.task.output.isVisibleTo(page)
    page.task.cancel_button.click()
    assert wait(lambda: not page.task.running, 20) and time.monotonic() - began < 30, "Cancel ended it"
    assert "cancelled" in page.message.text() and page.message.property("role") == "notice-warn"
    assert page.plugin_button.isEnabled(), "the buttons work again"
    assert page.task.output.isVisibleTo(page), "what it wrote stays readable"


def test_the_backend_streams_and_cancels_a_command(tmp_path):
    """SandboxAI.watching: call() hands on what comes as it comes and ends the command (and what it started) once
    cancelled - status backend.CANCELLED."""
    sai = backend.SandboxAI()
    script = tmp_path / "x.py"
    script.write_text("import sys, time\nprint('one', flush=True)\nprint('Ä', file=sys.stderr, flush=True)\n"
                      "time.sleep(60)\n")
    sai.command = lambda *args: [sys.executable, str(script)]
    seen, stop = [], threading.Event()

    def output(text):
        seen.append(text)
        if "Ä" in "".join(seen):
            stop.set()
    began = time.monotonic()
    with sai.watching(output=output, cancelled=stop.is_set):
        status, out, err = sai.call("x", timeout=120)
    assert status == backend.CANCELLED and out == "one\n" and err.startswith("Ä") and err.endswith("cancelled")
    assert time.monotonic() - began < 15
    with sai.watching(output=seen.append, cancelled=lambda: False, errors_only=True):
        sai.command = lambda *args: [sys.executable, "-c", "import sys; print('{}'); print('note', file=sys.stderr)"]
        seen.clear()
        assert sai.call("x")[0] == 0 and "".join(seen) == "note\n", "ERRORS_ONLY: a --json stdout is not shown"
    assert sai.call("x")[0] == 0, "outside watching(): as before"


@pytest.fixture
def stub_web(tmp_path):
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(HERE.parent / "sandbox_ai_app" / "web" / name, web / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(HERE / "fake_xterm" / name, web / "vendor" / name)
    return web


@pytest.mark.parametrize("action", ["install plugins", "image build", "start"])
def test_what_runs_in_a_terminal_tab_leaves_the_window_free(qapp, window, env, monkeypatch, stub_web, wait, action):
    """Install plugins, an image build and a start run in a terminal tab of the app (their own process, its output
    as it comes): opening it keeps the window answering."""
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    window.terminals.web = str(stub_web)
    sleeper = [sys.executable, "-c", f"import time; print('working', flush=True); time.sleep({SLOW})"]
    monkeypatch.setattr(window.sai, "plugins_argv", lambda **kw: sleeper)
    monkeypatch.setattr(window.sai, "launch_argv", lambda *a, **kw: sleeper)
    command = window.sai.command
    monkeypatch.setattr(window.sai, "command", lambda *args: sleeper if args[:1] == ("build",) else command(*args))
    window.sai.save_setting("terminal", "app")
    window.terminals.open([sys.executable, "-c", "pass"], "warm up")  # the first web view (Chromium) once
    before = window.terminals.tabs.count()
    ended = []
    if action == "install plugins":
        click = lambda: window.kits.install(False)  # noqa: E731
    elif action == "image build":
        click = window.images.build_all
    else:
        window.set_project(project(env, "p"))
        click = lambda: window.launch.start(embedded=True)  # noqa: E731
    calls = [("began", False, ()), ("ended", ())]  # the command runs in its own process

    def finished():
        views = window.terminals.views()[before:]
        if views and not views[-1].running and not ended:
            ended.append(True)
        return bool(ended)
    responsive(qapp, click, finished, calls)
    assert window.terminals.tabs.count() == before + 1


def test_mcp_add_runs_in_the_background(qapp, window, monkeypatch):
    from sandbox_ai_app import ui
    page = window.mcp
    added = slow((0, "added demo", ""))
    monkeypatch.setattr(window.sai, "host_run", lambda host, *args, timeout=120, input=None:
                        added(host, *args) if args[:2] == ("mcp", "add") else (0, "[]", ""))
    page.entries = {"mcp:demo": {"name": "demo", "network": "none", "title": "Demo", "image": "x"}}
    widgets.fill(page.catalog, [["mcp:demo", "none", "Demo", "yes", "signed"][:page.catalog.columnCount()]])
    page.catalog.selectRow(0)
    page.catalog_of = (ui.LOCAL, "")
    monkeypatch.setattr(ui.AddServerDialog, "exec", lambda dialog: QtWidgets.QDialog.DialogCode.Accepted)
    responsive(qapp, page.add, lambda: added.calls and not widgets.busy(page), added.calls)
    assert added.calls[0][2][1:3] == ("mcp", "add")


@pytest.fixture
def host_page(window, monkeypatch):
    sai = window.sai
    if not sai.hosts_supported() or not sai.host_update_supported():
        pytest.skip("this sandbox-ai cannot update hosts")
    sai.host_add("ct1", "admin@192.0.2.10", 22, "", "")
    monkeypatch.setattr(sai, "host_statuses", lambda refresh=False, cached=True: {
        "ct1": {"version": "0.1.60", "available": "0.1.66", "update": True, "kind": "user"}})
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120, input=None: (0, "{}", ""))
    page = window.hosts
    page.load_hosts()
    page.table.clearSelection()
    page.table.selectRow(widgets.find_row(page.table, "ct1"))
    return page


def test_a_host_update_runs_in_the_background_one_at_a_time(qapp, window, host_page, monkeypatch):
    page = host_page
    update = slow(lambda name, sudo=None, from_here=False: {"name": name, "before": "0.1.60", "after": "0.1.66",
                                                            "changed": True, "error": None})
    monkeypatch.setattr(window.sai, "host_update", update)
    seen = {}

    def click():
        page.update_host()
        seen["task"] = page.task.isVisibleTo(page) and "Updating ct1" in page.task.label.text()
        page.update_all()
        seen["second"] = page.status.text()
    responsive(qapp, click, lambda: not page.task.running, update.calls)
    assert seen["task"] and seen["second"] == "An update or installation is running: wait for it, or cancel it."
    assert len([c for c in update.calls if c[0] == "began"]) == 1
    assert "ct1: 0.1.60 -> 0.1.66" in page.updates.toPlainText()


def test_a_host_install_runs_in_the_background(qapp, window, host_page, monkeypatch):
    from sandbox_ai_app import hosts_page
    page = host_page
    if not window.sai.host_install_supported():
        pytest.skip("this sandbox-ai cannot install on hosts")
    install = slow(lambda name, **values: {"name": name, "before": None, "after": "0.1.78", "changed": True,
                                           "error": None})
    monkeypatch.setattr(window.sai, "host_install", install)
    monkeypatch.setattr(hosts_page.InstallDialog, "exec", lambda dialog: QtWidgets.QDialog.DialogCode.Accepted)
    responsive(qapp, page.install, lambda: install.calls and not page.task.running, install.calls)
    assert "ct1" in page.updates.toPlainText()


def test_the_llm_test_connection_runs_in_the_background(qapp, window):
    from sandbox_ai_app import llm_page
    run = slow((0, "[]", ""))
    page = window.llm
    dialog = llm_page.LlmDialog(page, "local", run=run)
    dialog.name.setText("box")
    dialog.url.setText("http://192.168.1.30:1234")
    responsive(qapp, dialog.test_button.click, lambda: run.calls and dialog.test_button.isEnabled(), run.calls)
    assert dialog.test_result.text() != "Asking the server…"
    dialog.deleteLater()


def test_a_copy_on_the_files_page_runs_in_the_background(qapp, tmp_path, monkeypatch, wait):
    from sandbox_ai_app import files, files_page
    from test_files_page import FakeSai
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    ws, host = tmp_path / "ws", tmp_path / "host"
    ws.mkdir()
    host.mkdir()
    (ws / "a.txt").write_text("alpha")
    page = files_page.FilesPage(FakeSai([("run", "claude-abc", str(ws.resolve()))]))
    assert wait(lambda: page.roots.count() > 0 and not page.roots_reading)
    page.right.go_path(str(host))
    copied = slow(files.copy_entry)
    monkeypatch.setattr(files_page.files, "copy_entry", copied)
    widgets.select_keys(page.left.table, ["a.txt"])
    responsive(qapp, lambda: page.transfer(page.left, page.left.chosen(), page.right),
               lambda: copied.calls and not page.task.running, copied.calls)
    assert (host / "a.txt").read_text() == "alpha"
    page.deleteLater()


def instance(kind, name, **values):
    return backend.Instance(kind, f"{kind}:{name}", name, **values)


def test_stop_runs_in_the_background(qapp, window, monkeypatch):
    stop = slow((0, "", ""))
    monkeypatch.setattr(window.sai, "stop_run", stop)
    page = window.sandboxes
    responsive(qapp, lambda: page.stop(instance("run", "claude-x", run="claude-x", state="running")),
               lambda: stop.calls and not widgets.busy(page), stop.calls)


def test_resume_looks_at_the_home_in_the_background(qapp, window, monkeypatch):
    review = slow({"review": {"changes": [], "complete": True}})
    monkeypatch.setattr(window.sai, "review_home", review)
    went = []
    page = window.sandboxes
    responsive(qapp, lambda: page.resume_checked(instance("session", "work", session="work", state="stopped"),
                                                 lambda: went.append(True)),
               lambda: bool(went), review.calls)
