"""Regressions from live use: each test quotes what the user reported (several fixes append here, each in a block of
its own)."""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

from test_ui import env, read_now, window  # noqa: E402,F401 - the page's fixtures


# == Sandboxes page: stopped sessions only with "Ended" (2026-11-14) ==================================================

def test_a_stopped_session_is_listed_only_with_ended(window, env, monkeypatch, wait):  # noqa: F811
    """User: "warum werden bei Sitzung und Session beendete Sessions angezeigt, obwohl beendete nicht angeklickt
    ist?" - a kept session that is stopped was listed on the Sandboxes page with "Ended" off."""
    page = window.sandboxes
    stopped = backend.Instance("session", "session:old", "old", session="old", state="stopped", host="local")
    running = backend.Instance("session", "session:work", "work", session="work", state="running", host="local")
    orphan = backend.Instance("session", "session:lost", "lost", session="lost", state="orphaned", host="local")
    monkeypatch.setattr(window.sai, "ended_starts", lambda name="local": [])
    monkeypatch.setattr(window.sai, "instances", lambda: ([stopped, running, orphan], []))
    page.show_ended.setChecked(False)
    read_now(page)
    assert widgets.find_row(page.table, "session:work") >= 0 and widgets.find_row(page.table, "session:lost") >= 0
    assert widgets.find_row(page.table, "session:old") < 0, "a stopped session only with Ended checked"
    page.show_ended.setChecked(True)
    read_now(page)
    row = widgets.find_row(page.table, "session:old")
    assert row >= 0
    # With Ended: a stopped session is resumed and deleted from here, and the box's tooltip says so.
    page.table.selectRow(row)
    enabled = [action for action, button in page.session_buttons.items() if button.isEnabled()]
    assert enabled == ["resume", "delete"]
    assert "resume or delete" in page.show_ended.toolTip()


def test_a_session_that_stops_while_selected_stays_until_another_row_is_selected(window, env, monkeypatch,  # noqa: F811
                                                                                 wait):
    """User: "warum werden bei Sitzung und Session beendete Sessions angezeigt, obwohl beendete nicht angeklickt
    ist?" - hidden while Ended is off, but the session the human just stopped must not vanish under the mouse: it
    stays listed and selected, Resume and Delete at hand, until another row is selected."""
    page = window.sandboxes
    registry = {"work": backend.Instance("session", "session:work", "work", session="work", state="running"),
                "other": backend.Instance("session", "session:other", "other", session="other", state="running")}
    monkeypatch.setattr(window.sai, "instances", lambda: (list(registry.values()), []))
    page.show_ended.setChecked(False)
    read_now(page)
    page.table.selectRow(widgets.find_row(page.table, "session:work"))
    registry["work"] = backend.Instance("session", "session:work", "work", session="work", state="stopped")
    read_now(page)  # stopped meanwhile (its Stop, or the agent ended)
    assert page.current() is not None and page.current().key == "session:work"
    assert [a for a, b in page.session_buttons.items() if b.isEnabled()] == ["resume", "delete"]
    page.table.selectRow(widgets.find_row(page.table, "session:other"))
    read_now(page)
    assert widgets.find_row(page.table, "session:work") < 0, "once another row is selected it goes"


# == A session whose tmux session is gone: "no sessions", yet shown as running (2026-11-14) ==========================

def test_a_session_whose_tmux_session_ended_is_shown_without_an_agent(window, env, monkeypatch, wait):  # noqa: F811
    """User: attaching to auburn-kakapo-bamboo printed tmux's "no sessions", then "detached from auburn-kakapo-bamboo;
    it keeps running" - "while Sandboxes & Sitzungen show it as running". The real read of this machine's sessions
    (sandbox-ai's own modules): a session whose supervisor runs but whose tmux session has ended (its guard recorded
    it, tmux/ended) is shown as running without an agent, in red, with the end of its log; Attach is not offered."""
    import json
    import subprocess
    import sys
    from sandbox_ai_app import terminal
    page = window.sandboxes
    directory = env / "state" / "sandbox-ai" / "sessions" / "auburn-kakapo-bamboo"
    (directory / "tmux").mkdir(parents=True)
    (directory / "home").mkdir()
    (directory / "supervisor.log").write_text("agent-sandbox: the agent exited with status 3 after 1 s, and the shell "
                                              "window closed: the terminal's tmux session has ended; the sandbox ends\n")
    (directory / "tmux" / "ended").write_text('{"status": 3, "seconds": 1}\n')
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", str(directory / "tmux")])
    try:
        (directory / "session.json").write_text(json.dumps({
            "agent": "claude", "options": [], "agent_args": [], "backend": "bwrap", "workspace": str(env),
            "cwd": str(env), "created": "2026-11-14T10:00:00Z", "started": "2026-11-14T10:00:00Z",
            "name": "auburn-kakapo-bamboo", "ready": True, "keep": True, "mode": "tmux", "pid": supervisor.pid}))
        read_now(page)
        row = widgets.find_row(page.table, "session:auburn-kakapo-bamboo")
        assert row >= 0
        page.table.selectRow(row)
        instance = page.current()
        assert instance.state == "running" and instance.record.get("terminal") == "ended"
        state = page.table.item(row, 6).text()
        assert "no agent" in state, state
        for action in ("attach", "attach-app"):
            button = page.session_buttons[action]
            assert not button.isEnabled() and "exited with status 3" in button.toolTip()
        # The Terminals page's list (sandbox-ai list --json's words) says why there is nothing to attach.
        assert "tmux session has ended" in terminal.attach_why({"attachable": False, "attach_why": "ended"})
    finally:
        supervisor.kill()
        supervisor.wait()


# == Terminals: "Not reopened (ended meanwhile)" although the session runs (2026-11-14) ===============================

def reopen_page(tmp_path, monkeypatch, listed):
    """The Terminals page with real (offscreen) terminal tabs, a stand-in sandbox-ai whose `list --json` answers what
    LISTED() gives each time it is asked (attach stays, as a real attach does)."""
    import json
    import shutil
    from pathlib import Path
    from sandbox_ai_app import state, terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    here = Path(__file__).resolve().parent
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(here.parent / "sandbox_ai_app" / "web" / name, web / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(here / "fake_xterm" / name, web / "vendor" / name)
    script = tmp_path / "bin" / "sandbox-ai"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexec sleep 60\n")
    script.chmod(0o755)
    sai = backend.SandboxAI()
    asked = []
    monkeypatch.setattr(sai, "command", lambda *args: [str(script), *args])

    def host_run(name, *args, timeout=120):
        asked.append((name, args))
        return 0, json.dumps(listed() if args == ("list", "--json") else {}), ""
    monkeypatch.setattr(sai, "host_run", host_run)
    page = terminal.TerminalsPage(web, sai=sai)
    page.store = state.State(tmp_path / "state.json")
    return page, asked


def listed_start(name, **fields):
    return {"name": name, "id": f"id-{name}", "kind": "session", "agent": "claude", "state": "running",
            "attachable": True, "attach_why": None, "app_tab": None, **fields}


def test_a_running_session_is_never_said_to_have_ended_when_its_tab_is_not_reopened(env, monkeypatch, wait,  # noqa: F811
                                                                                    tmp_path):
    """User: the Terminals page said "Nicht wieder geöffnet (inzwischen beendet): NAME" although the session still
    runs. Only a start that is gone or stopped has ended meanwhile; one that runs is reopened - also when its
    sandbox-ai is older (list --json without "attachable"), when the tab knew it by its id, or once its terminal is
    there (it still started: read again until it is) - or said with its real reason (paused, no terminal)."""
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "REOPEN_RETRY", 0.2, raising=False)
    rounds = []

    def listed():
        rounds.append(True)
        starting = len(rounds) < 3  # its terminal comes on the third read
        old = listed_start("old-version")
        del old["attachable"], old["attach_why"], old["id"]  # (sandbox-ai 0.1.94's list --json)
        return [listed_start("web"), old, listed_start("renamed", id="id-kept"),
                listed_start("slow", attachable=not starting, terminal="starting" if starting else "ready",
                             attach_why="no-terminal" if starting else None),
                listed_start("held", state="paused", attachable=False, attach_why="paused"),
                listed_start("broken", attachable=False, terminal="missing", attach_why="no-terminal",
                             terminal_log="agent-sandbox: podman failed"),
                listed_start("stopped-one", state="stopped", attachable=False)]
    page, asked = reopen_page(tmp_path, monkeypatch, listed)
    page.store.data["terminal-tabs"] = [
        {"host": "local", "name": name, "title": name}
        for name in ("web", "old-version", "slow", "held", "broken", "stopped-one", "gone")] + [
        {"host": "local", "name": "id-kept", "title": "renamed"}]
    done = []
    assert page.reopen(everything=False, done=lambda opened, skipped: done.append((opened, skipped)))
    assert wait(lambda: done, 20)
    opened, skipped = done[0]
    assert skipped == ["stopped-one", "gone"], "only what is stopped or gone ended meanwhile"
    assert set(opened) == {"web", "old-version", "renamed"}
    said = page.hint.text()
    assert "stopped-one, gone" in said and "held" in said and "broken" in said
    assert "slow" not in [name for name in said.split(": ")[-1].split(", ")] or "waiting" in said.lower()
    # The one still starting is reopened once its terminal is there.
    assert wait(lambda: page.tabs.count() == 4, 20), [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert "slow" in [page.tabs.tabText(i) for i in range(page.tabs.count())]
    for name in ("held", "broken"):
        assert name not in [page.tabs.tabText(i) for i in range(page.tabs.count())]
    ended_meanwhile = tr_en("Not reopened (ended meanwhile): {names}", names="x").split(":")[0]
    for line in said.split("\n"):
        if line.startswith(ended_meanwhile):
            assert "held" not in line and "broken" not in line and "slow" not in line
    page.store = None
    page.close_all()
    page.deleteLater()


def tr_en(text, **values):
    from sandbox_ai_app import i18n
    return i18n.tr(text, **values)


# == Tabs of ended sessions close by themselves; failed ones stay, marked (2026-11-14) =================================

def test_a_terminal_tab_of_a_session_that_ended_closes_and_a_failed_one_stays(env, monkeypatch, wait,  # noqa: F811
                                                                             tmp_path):
    """User: "warum sind in dieser Tab-Zeile, die in mehreren Bereichen vorhanden ist, auch Tabs offen von Sessions
    die beendet sind? Das macht doch keinen Sinn." Real terminal tabs and a stand-in sandbox-ai: a session's tab whose
    attach ends normally (detached, the session ended) closes by itself after a moment, without taking the focus; one
    whose start failed (an exit status other than 0, its message in the tab) stays, marked as failed, until the human
    closes it (Ctrl+W); a build's tab that ended stays (its output is read there)."""
    from sandbox_ai_app import terminal
    page, _asked = reopen_page(tmp_path, monkeypatch, lambda: [])
    monkeypatch.setattr(terminal, "AUTO_CLOSE_MS", 300, raising=False)
    script = tmp_path / "bin" / "sandbox-ai"
    script.write_text('#!/bin/sh\ncase "$*" in *fails*) echo "Session failed to start: Podman failed"; exit 1;; '
                      '*) sleep 1; exit 0;; esac\n')
    from conftest import open_tab
    ok = open_tab(page, page.sai.command("attach", "fine"), "fine")
    bad = open_tab(page, page.sai.command("attach", "fails"), "fails")
    build = open_tab(page, ["sh", "-c", "echo built; exit 0"], "build")
    titles = lambda: [page.tabs.tabText(i) for i in range(page.tabs.count())]  # noqa: E731
    assert wait(lambda: not bad.running and not build.running, 15)
    assert wait(lambda: page.tabs.indexOf(ok) < 0, 15), titles()
    assert page.tabs.indexOf(bad) >= 0 and "failed" in page.tabs.tabText(page.tabs.indexOf(bad)), titles()
    assert page.tabs.indexOf(build) >= 0, "a build's tab stays: its output is read there"
    page.tabs.setCurrentWidget(bad)
    assert page.ended(bad) and page.close_key.isEnabled(), "Ctrl+W closes the failed one"
    page.store = None
    page.close_all()
    page.deleteLater()


def test_a_session_tab_of_the_tab_row_closes_when_it_ended_and_stays_when_it_failed(window, env, monkeypatch,  # noqa: F811
                                                                                   wait):
    """User: "warum sind in dieser Tab-Zeile, die in mehreren Bereichen vorhanden ist, auch Tabs offen von Sessions
    die beendet sind? Das macht doch keinen Sinn." The window's tab row: a session that ran and stopped normally (exit
    status 0) loses its tab after a moment; one whose run failed (exit status 1) keeps it, marked as failed; at the
    app's start a kept tab of a session that has ended is not restored."""
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "AUTO_CLOSE_MS", 300, raising=False)
    page = window.sandboxes
    registry = {name: backend.Instance("session", f"session:{name}", name, session=name, state="running",
                                       agent="claude", backend="podman") for name in ("calm", "broken", "keep")}
    monkeypatch.setattr(window.sai, "instances", lambda: (list(registry.values()), []))
    read_now(page)
    for name in ("calm", "broken", "keep"):
        window.open_tab(ui.instance_entry(registry[name]), activate=False)
    window.sync_tabs(page.instances)
    assert all(window.tabs.find(f"session:{n}") >= 0 for n in ("calm", "broken", "keep"))
    registry["calm"] = backend.Instance("session", "session:calm", "calm", session="calm", state="stopped",
                                        record={"status": 0})
    registry["broken"] = backend.Instance("session", "session:broken", "broken", session="broken", state="stopped",
                                          record={"status": 1})
    read_now(page)
    window.sync_tabs(page.instances)
    assert wait(lambda: window.tabs.find("session:calm") < 0, 10), "ended normally: its tab goes"
    index = window.tabs.find("session:broken")
    assert index >= 0 and window.tabs.entry(index)["state"] == "failed"
    assert "failed" in window.tabs.tabToolTip(index).lower() or "fehl" in window.tabs.tabToolTip(index).lower()
    assert window.tabs.find("session:keep") >= 0, "what runs keeps its tab"
    # The next start of the app: the kept tab of a session that ended is not restored.
    window.state.set("tabs", [{"kind": "session", "name": "gone", "id": "gone"},
                              {"kind": "session", "name": "keep", "id": "keep"}])
    window.restoring = True
    for i in reversed(range(window.tabs.count())):
        window.tabs.removeTab(i)
    window.restore_tabs()
    window.sync_tabs(page.instances)
    assert window.tabs.find("session:gone") < 0 and window.tabs.find("session:keep") >= 0


# == The tabs' close button lies inside its tab (2026-11-14) ==========================================================

GAP = 3  # px at least between a tab's [x] and its right edge


@pytest.mark.parametrize("large", [False, True])
def test_the_close_button_of_every_tab_lies_inside_its_tab_in_every_theme(qapp, large):
    """User: the close button [x] of the tabs in the shared tab row always sticks out a little beyond the tab's edge.
    In every theme, with the usual and a large font: the [x] of the window's tab row (ProjectTabs) and of the
    Terminals page's tabs (Qt's own close button) lies wholly inside its tab."""
    from PySide6 import QtCore, QtGui, QtWidgets
    from sandbox_ai_app import theme, ui
    font = QtGui.QFont(qapp.font())
    found = []
    try:
        if large:
            bigger = QtGui.QFont(font)
            bigger.setPointSizeF((font.pointSizeF() if font.pointSizeF() > 0 else 10) * 1.6)
            qapp.setFont(bigger)
        for mode in theme.THEMES:
            theme.apply(mode, qapp)
            row = ui.ProjectTabs()
            for name in ("auburn-kakapo-bamboo", "web", "a"):
                row.add({"key": f"session:{name}", "kind": "session", "name": name, "agent": "claude",
                         "backend": "podman", "state": "running"})
            row.add({"key": "project:/srv/app", "kind": "project", "path": "/srv/app"})
            from sandbox_ai_app import widgets as shared
            terminals = QtWidgets.QTabWidget()
            shared.inset_close_tabs(terminals)  # as the Terminals page and the browser make theirs
            terminals.setTabsClosable(True)
            for name in ("claude (work)", "build"):
                terminals.addTab(QtWidgets.QWidget(), name)
            for bar in (row, terminals.tabBar()):
                holder = bar if bar is row else terminals
                holder.resize(900, 80)
                holder.show()
                for _ in range(3):
                    qapp.processEvents()
                for index in range(bar.count()):
                    button = bar.tabButton(index, QtWidgets.QTabBar.ButtonPosition.RightSide)
                    if button is None or not button.isVisible():
                        continue
                    shown = button.findChild(QtWidgets.QToolButton, "tabClose") or button  # (the [x] itself)
                    # Inside the tab's frame, a few pixels from its right edge (its border is drawn there).
                    tab = bar.tabRect(index).adjusted(1, 1, -GAP, -1)
                    where = QtCore.QRect(shown.mapTo(bar, QtCore.QPoint(0, 0)), shown.size())
                    if not tab.contains(where):
                        found.append(f"{mode}{' large' if large else ''} {type(bar).__name__} tab {index}: the "
                                     f"button {where} beyond the tab {tab}")
                holder.close()
            row.deleteLater()
            terminals.deleteLater()
    finally:
        qapp.setFont(font)
        theme.apply("system", qapp)
    assert found == [], "\n".join(found)


# == The Rename question and its message can be read (2026-11-14) ====================================================

@pytest.mark.parametrize("language", ["en", "de"])
def test_the_rename_question_and_its_message_are_wide_enough_for_their_title(window, env, monkeypatch, wait,  # noqa: F811
                                                                             language):
    """User: "box msg in Sitzung & Session umbenennen ist zu klein. Bitte auch alle anderen Boxen prüfen ob sie korrekt
    dargestellt werden." The Sandboxes page's Rename… as the app runs it: the question (Qt's input dialog) and the
    message with the outcome were as narrow as their text, and the window manager cut their title "Rename
    auburn-kakapo-bamboo". Both are now as wide as the title needs (in bold, with a title bar's buttons beside it)
    and show their text whole (tests/test_dialog_sizes.py checks every other dialog)."""
    from PySide6 import QtGui, QtWidgets
    from sandbox_ai_app import i18n
    i18n.set_language(language)
    for cls in (QtWidgets.QDialog, QtWidgets.QMessageBox):  # (taken back after the test)
        monkeypatch.setattr(cls, "showEvent", cls.showEvent)
    widgets.fitting_dialogs()  # as main.py installs it
    page = window.sandboxes
    name = "auburn-kakapo-bamboo"
    session = backend.Instance("session", f"session:{name}", name, session=name, state="running")
    monkeypatch.setattr(window.sai, "instances", lambda: ([session], []))
    monkeypatch.setattr(window.sai, "host_run", lambda host, *args, timeout=120: (0, f"{name} is now {name}-2\n", ""))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    read_now(page)
    page.table.selectRow(widgets.find_row(page.table, f"session:{name}"))
    shown = []

    def bold_width(dialog):
        font = QtGui.QFont(dialog.font())
        font.setBold(True)
        return QtGui.QFontMetrics(font).horizontalAdvance(dialog.windowTitle()) + 100

    def answered(dialog, *args):  # the question, shown as the app shows it, answered with a new name
        dialog.show()
        QtWidgets.QApplication.processEvents()
        shown.append((dialog.windowTitle(), dialog.width(), bold_width(dialog)))
        dialog.setTextValue(f"{name}-2")
        dialog.accept()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(QtWidgets.QDialog, "exec", answered)
    page.rename()
    boxes = []
    assert wait(lambda: [w for w in QtWidgets.QApplication.topLevelWidgets()
                         if isinstance(w, QtWidgets.QMessageBox) and w.isVisible() and name in w.windowTitle()], 15)
    for box in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(box, QtWidgets.QMessageBox) and box.isVisible() and name in box.windowTitle():
            QtWidgets.QApplication.processEvents()
            boxes.append((box.windowTitle(), box.width(), bold_width(box)))
            label = box.findChild(QtWidgets.QLabel, "qt_msgbox_label")
            assert label.width() >= label.sizeHint().width() or label.wordWrap(), "its text whole"
            box.close()
    assert shown and boxes
    for title, width, needed in shown + boxes:
        assert width >= needed, f"{title!r}: {width} px wide, its title needs {needed}"
