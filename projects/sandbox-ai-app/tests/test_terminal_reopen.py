"""Terminals after a restart of the app (user report 2026-11-12: "the sessions keep running in tmux, but the app's
terminal tabs are gone - wouldn't it make sense to show them again?"): the tabs of running starts are kept in the
app's state in their order; at the next start a tab is attached again to each of them - and, with the setting
reopen_terminals (on by default), to every other session in tmux that runs here and on the hosts whose terminals
were open. Nothing is ever started (only `sandbox-ai attach NAME`), a session that ended meanwhile is left out and
said. A start in a tab gets SANDBOX_AI_APP_TAB, which sandbox-ai records (list --json "app_tab"): that is how its
tab is found again. Real terminal tabs (offscreen QtWebEngine with a stand-in xterm.js) and a stand-in sandbox-ai."""
import json
from pathlib import Path
import shutil

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from conftest import open_tab  # noqa: E402
from sandbox_ai_app import backend, state, widgets  # noqa: E402

HERE = Path(__file__).resolve().parent


@pytest.fixture
def terminal(qapp, monkeypatch):
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    return terminal


@pytest.fixture
def stub_web(tmp_path):
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(HERE.parent / "sandbox_ai_app" / "web" / name, web / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(HERE / "fake_xterm" / name, web / "vendor" / name)
    return web


@pytest.fixture
def sai(tmp_path, monkeypatch):
    """sandbox-ai as a stand-in: its commands run a script named sandbox-ai that writes what it was asked (and the
    tag of its tab) and stays (as attach does); `list --json` says what LISTED holds."""
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    script = tmp_path / "bin" / "sandbox-ai"
    script.parent.mkdir()
    log = tmp_path / "asked.log"
    script.write_text(f"#!/bin/sh\necho \"$* tab=$SANDBOX_AI_APP_TAB\" >> {log}\nexec sleep 60\n")
    script.chmod(0o755)
    sai = backend.SandboxAI()
    sai.listed, sai.asked, sai.log = [], [], log
    monkeypatch.setattr(sai, "command", lambda *args: [str(script), *args])

    def host_run(name, *args, timeout=120):
        sai.asked.append((name, args))
        return 0, json.dumps(sai.listed if args == ("list", "--json") else {}), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    return sai


def running(name, tag=None, **fields):
    return {"name": name, "id": f"id-{name}", "kind": "session", "agent": "claude", "state": "running",
            "attachable": True, "attach_why": None, "app_tab": tag, **fields}


def attached(page):
    """(title, the start the tab attaches to) of every tab, in order."""
    from sandbox_ai_app import terminal
    return [(page.tabs.tabText(i), terminal.attach_key(page.tabs.widget(i).argv)) for i in range(page.tabs.count())]


def test_the_tabs_of_running_starts_are_kept_and_attached_again_after_a_restart(terminal, stub_web, sai, tmp_path,
                                                                                 wait):
    store = state.State(tmp_path / "state.json")
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = store
    # The app's tabs: a start (a session in tmux: its tab gets a tag), an attached start, and a build (ends with its
    # tab: not kept).
    start = open_tab(page, sai.command("claude", "--workspace", "/srv/app"), "claude: app")
    open_tab(page, sai.command("attach", "web"), "web")
    open_tab(page, ["sh", "-c", "sleep 60"], "build")
    assert start.tab_tag and len(start.tab_tag) == 16
    assert wait(lambda: sai.log.exists() and f"tab={start.tab_tag}" in sai.log.read_text(), 10), \
        "the start in the tab gets the tag sandbox-ai records"
    kept = store.data["terminal-tabs"]
    assert kept == [{"host": "local", "tag": start.tab_tag, "title": "claude: app"},
                    {"host": "local", "name": "web", "title": "web"}]
    # The app ends: its tabs end with it, the kept list stays (MainWindow.shutdown lets go of the store first).
    page.store = None
    page.close_all()
    page.deleteLater()
    store.flush()
    assert json.loads((tmp_path / "state.json").read_text())["terminal-tabs"] == kept

    # The next start of the app: brisk-otter is the session the first tab started; web still runs; idle runs too
    # (opened with the setting on, after the kept ones); a paused one, a task and an MCP server have nothing to attach.
    sai.listed = [running("idle"), running("web"), running("brisk-otter", tag=start.tab_tag),
                  running("held", state="paused", attachable=False),
                  running("task", kind="run", attachable=False), {"name": "mcp-x", "kind": "mcp", "state": "running"}]
    sai.log.unlink()
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = state.State(tmp_path / "state.json")
    done = []
    assert page.reopen(everything=True, done=lambda opened, skipped: done.append((opened, skipped)))
    assert wait(lambda: done and page.tabs.count() == 3, 20)
    assert done == [(["brisk-otter", "web", "idle"], [])]
    assert attached(page) == [("brisk-otter", ("here", "brisk-otter")), ("web", ("here", "web")),
                              ("idle", ("here", "idle"))], "in the previous order, then the others"
    assert wait(lambda: sai.log.exists() and len(sai.log.read_text().splitlines()) == 3, 10)
    assert all(line.startswith("attach ") for line in sai.log.read_text().splitlines()), "only attached, never started"
    assert sai.asked == [("local", ("list", "--json"))]
    # Kept again, now by name.
    assert wait(lambda: [e.get("name") for e in page.store.data["terminal-tabs"]] == ["brisk-otter", "web", "idle"], 5)
    page.store = None
    page.close_all()
    page.deleteLater()


def test_a_session_that_ended_meanwhile_is_left_out_and_said(terminal, stub_web, sai, tmp_path, wait):
    store = state.State(tmp_path / "state.json")
    store.data["terminal-tabs"] = [{"host": "local", "name": "gone", "title": "gone"},
                                   {"host": "local", "tag": "0123456789abcdef", "title": "claude: old"},
                                   {"host": "local", "name": "web", "title": "web"}]
    sai.listed = [running("web"), running("idle"), running("stopped-one", state="stopped", attachable=False)]
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = store
    done = []
    # The setting off: only the kept ones.
    assert page.reopen(everything=False, done=lambda opened, skipped: done.append((opened, skipped)))
    assert wait(lambda: done and page.tabs.count() == 1, 20)
    assert done == [(["web"], ["gone", "claude: old"])]
    assert attached(page) == [("web", ("here", "web"))]
    assert "Not reopened (ended meanwhile): gone, claude: old" in page.hint.text() and page.hint.isVisibleTo(page)
    page.store = None
    page.close_all()
    page.deleteLater()


def test_the_setting_decides_and_nothing_kept_reads_nothing(terminal, stub_web, sai, tmp_path, monkeypatch, wait):
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = state.State(tmp_path / "state.json")
    assert backend.setting("reopen_terminals") is True, "on by default"
    sai.listed = [running("idle")]
    done = []
    assert page.reopen(done=lambda opened, skipped: done.append(opened))  # the setting: every session here
    assert wait(lambda: done == [["idle"]] and page.tabs.count() == 1, 20)
    page.store.data["terminal-tabs"] = []
    page.close_all()
    monkeypatch.setitem(backend.SETTINGS, "reopen_terminals", False)
    sai.asked.clear()
    assert page.reopen() is False and sai.asked == [], "off and nothing kept: nothing is read, nothing opens"
    page.store = None
    page.deleteLater()


def test_a_host_that_does_not_answer_says_nothing_of_its_sessions(terminal, stub_web, sai, tmp_path, monkeypatch,
                                                                  wait):
    store = state.State(tmp_path / "state.json")
    store.data["terminal-tabs"] = [{"host": "ct1", "name": "far", "title": "ct1: far"},
                                   {"host": "local", "name": "web", "title": "web"}]
    sai.listed = [running("web")]

    def host_run(name, *args, timeout=120):
        if name == "ct1":
            return 255, "", "ssh: connect to host ct1: Connection refused"
        return 0, json.dumps(sai.listed), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    page = terminal.TerminalsPage(stub_web, sai=sai)
    page.store = store
    done = []
    page.reopen(everything=False, done=lambda opened, skipped: done.append((opened, skipped)))
    assert wait(lambda: done, 20)
    assert done == [(["web"], [])]
    page.store = None
    page.close_all()
    page.deleteLater()


def test_the_window_keeps_the_list_at_its_end_and_lets_go_of_it(window, monkeypatch):  # noqa: F811
    """MainWindow: the Terminals page gets the app's state when it is made; at the end the list is written while
    the tabs still run, and the tabs that end with the app change it no more."""
    page = window.terminals
    assert page.store is window.state
    monkeypatch.setattr(page, "remembered", lambda: [{"host": "local", "name": "web", "title": "web"}])
    window.shutdown()
    assert window.state.data["terminal-tabs"] == [{"host": "local", "name": "web", "title": "web"}]
    assert page.store is None
    monkeypatch.setattr(page, "remembered", lambda: [])
    page.save_tabs()  # (a tab that ends now)
    assert window.state.data["terminal-tabs"] == [{"host": "local", "name": "web", "title": "web"}]


from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests
