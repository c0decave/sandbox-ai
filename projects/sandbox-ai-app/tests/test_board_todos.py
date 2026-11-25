"""The Board items of the user's list of 2026-11-24 (todos.txt, "board:"), offscreen:

1. "kann es sein, dass neues projekt als button fehlt?" - New project… is a button of the Board (first in its row).
2. "wenn ich das item toolchain nutze kommt in der erstsetup KEIN dropdown mit den zur verfügung stehenden toolchains"
   - the first setup of the Toolchains part offers the host's toolchains, as the properties do.
3. "wieso steht da zweimal nebeneinander toolchains ohne ihre registries?" - a check box's row names it once; the
   part lists each toolchain's registries and what the start does about them.
4. "property fenster der items müssen immer groß genug gestartet werden das alles sichtbar ist und immer auch
   zentriert" - the properties' panel as big as its rows need; floating: centred over the window when it opens.
5. "wenn ich zwischen aktiver session z.b. von terminals komme und auf das board gehe, soll dort das passende tab und
   projekt ... angezeigt werden".
6. the agent's intro: the Agent part's check box (--no-intro) and the Settings' default (agent_intro).
7. what a toolchain needs of the network is shown: opened by the start, kept closed, refused by the mode.
8. "Board zeigen" on Sessions (button, right-click menu) and on a terminal tab's menu.
9. the Network part lists, read-only, what the other parts open - from the launcher's reach() in the dry run's plan.
"""
import json
import os
import time
from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QRect, Qt, Signal  # noqa: E402

from sandbox_ai_app import backend, board_follow, dock_fold, guide_view, i18n, terminal, widgets  # noqa: E402
from sandbox_ai_app import board as B  # noqa: E402
from sandbox_ai_app import list_fields as LF  # noqa: E402

from test_pages_follow_host import close, isolated, make_window, sai, wait_for  # noqa: E402,F401
from test_dialog_sizes import screens  # noqa: E402,F401 - the dialogs as the app shows them, measured

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

# The host's store and signed catalog, as `sandbox-ai toolchain list|catalog --json` give them (backend.host_toolchains).
TOOLCHAINS = {"installed": [
    {"name": "go", "version": "1.27.3", "toolchain": "go", "commands": ["go", "gofmt"],
     "hosts": ["proxy.golang.org", "sum.golang.org"], "allow": ["github"], "requires": []},
    {"name": "cc", "version": "0.17.1", "toolchain": "cc", "commands": ["cc"], "hosts": [], "allow": [],
     "requires": []}],
    "catalog": [
    {"name": "rust", "version": "1.99.1", "toolchain": "rust", "commands": ["cargo", "rustc"],
     "hosts": ["crates.io", "static.crates.io"], "allow": [], "requires": ["cc"], "installed": False},
    {"name": "jdk", "version": "21.0.13+9", "toolchain": "jdk", "commands": ["java"],
     "hosts": ["repo.maven.apache.org"], "allow": [], "requires": [], "installed": False},
    {"name": "jdk", "version": "17.0.21+9", "toolchain": "jdk@17", "commands": ["java"],
     "hosts": ["repo.maven.apache.org"], "allow": [], "requires": [], "installed": False}],
    "problem": None}


REAL_VIEW = terminal.TerminalView  # (the window fixture puts a stand-in in its place)


class FakeView(QtWidgets.QWidget):
    """A terminal tab without Chromium: what the Terminals page reads of a TerminalView."""
    exited = Signal(int)

    def __init__(self, argv, web=None, cwd=None, parent=None, env=None):
        super().__init__(parent)
        self.argv, self.tab_title, self.label = list(argv), " ".join(map(str, argv[:3])), None
        self.tab_tag = (env or {}).get(terminal.TAB_TAG)
        self.start_host = self.start_name = self.instance_key = None
        self.view = QtWidgets.QWidget(self)
        self.running = True

    def close_terminal(self):
        self.running = False


CT1 = [{"name": "lab", "id": "lab", "kind": "session", "state": "running", "attachable": True, "run": "claude-lab1",
        "agent": "claude", "workspace": "/srv/lab"}]


@pytest.fixture
def projects(isolated):
    web, api = isolated / "web", isolated / "api"
    web.mkdir()
    api.mkdir()
    return web, api


def local_starts(web, api):
    work = backend.Instance("session", "session:work", "work", run="session-work", session="work", project=str(web),
                            agent="claude", state="running", record={"run": "session-work", "workspace": str(web)})
    task = backend.Instance("session", "session:task", "task", run="session-task", session="task", project=str(api),
                            agent="codex", state="running", record={"run": "session-task", "workspace": str(api)})
    server = backend.Instance("mcp", "mcp:fetch", "fetch", mcp="fetch", state="running")
    return [work, task, server]


@pytest.fixture
def window(qapp, sai, projects, monkeypatch):
    monkeypatch.setattr(terminal, "TerminalView", FakeView)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    monkeypatch.setattr(terminal.chromium, "check", lambda: SimpleNamespace(ok=True, error=None))
    monkeypatch.setattr(sai, "instances", lambda: (local_starts(*projects), []))
    monkeypatch.setattr(sai, "host_toolchains", lambda host="local": json.loads(json.dumps(TOOLCHAINS)))
    sai.answers[("ct1", ("list", "--json"))] = (0, json.dumps(CT1), "")
    shown = make_window(qapp, sai)
    from conftest import read_now
    read_now(shown.sandboxes)
    page = shown.sandboxes
    page.remote["ct1"], page.remote_seen["ct1"] = [backend.listed_instance("ct1", entry) for entry in CT1], time.time()
    page.merge_remote()
    notices = []
    monkeypatch.setattr("sandbox_ai_app.short_notices.short_notice",
                        lambda parent, title, text, **kw: notices.append((title, text)))
    shown.notices = notices
    shown.resize(1280, 820)
    shown.show()
    qapp.processEvents()
    yield shown
    i18n.set_language("en")
    close(qapp, shown)


def inspect(qapp, board, kind):
    node = board.board.first(kind) or (board.add_node(kind) or board.board.first(kind))
    board.show_properties(node.id)
    wait_for(qapp, lambda: board.edit_node == node.id and not board.inspect_timer.isActive())
    qapp.processEvents()
    return node


def form_rows(board):
    """[(label text, field widget)] of the inspector's rows."""
    form, rows = board.inspector, []
    for row in range(form.rowCount()):
        label = form.itemAt(row, QtWidgets.QFormLayout.ItemRole.LabelRole)
        field = form.itemAt(row, QtWidgets.QFormLayout.ItemRole.FieldRole)
        rows.append((label.widget().text() if label is not None and label.widget() is not None else "",
                     field.widget() if field is not None else None))
    return rows


# --- 1. New project… -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("language, text", [("en", "New project…"), ("de", "Neues Projekt…")])
def test_the_board_has_a_new_project_button_first_in_its_row(qapp, isolated, sai, monkeypatch, language, text):
    i18n.set_language(language)
    shown = make_window(qapp, sai)
    try:
        board = shown.board
        button = board.new_project_button
        assert button.text() == text and button.toolTip()
        row = button.parentWidget()
        first = min(row.findChildren(QtWidgets.QAbstractButton), key=lambda b: (b.y(), b.x()))
        shown.resize(1280, 820)
        shown.show()
        shown.show_page("board")
        qapp.processEvents()
        first = min((b for b in row.findChildren(QtWidgets.QAbstractButton) if b.isVisibleTo(row)),
                    key=lambda b: (b.y(), b.x()))
        assert first is button, first.text()
        opened = []
        monkeypatch.setattr("sandbox_ai_app.wizard.SandboxWizard.exec", lambda self: opened.append(self) or 0)
        button.click()
        assert len(opened) == 1, "it opens the wizard: a new project step by step"
        menu = board.canvas_menu(board.view.mapToScene(5, 5))
        assert text in [action.text() for action in menu.actions()], "the canvas's menu has it too"
        assert text in [a.text() for a in shown.menuBar().actions()[0].menu().actions()], "and the File menu"
    finally:
        i18n.set_language("en")
        close(qapp, shown)


# --- 2. the first setup of the Toolchains part: the host's toolchains to choose from ----------------------------------

def test_the_first_setup_of_toolchains_offers_the_hosts_toolchains(qapp, window, monkeypatch):
    board = window.board
    window.show_page("board")
    seen = {}

    def answer(dialog):
        dialog.show()
        question, field = dialog.inputs["--toolchain"]
        assert isinstance(field, LF.ListFields), "the same field as the properties: a list to choose from"
        box = field.value_box
        assert wait_for(qapp, lambda: box.count() >= 5), "the store's and the signed catalog's, every version"
        seen["items"] = [(box.itemText(i), box.itemData(i)) for i in range(box.count())]
        assert "Choose from the list" in box.lineEdit().placeholderText()
        for value in ("go", "jdk@17"):
            box.setCurrentIndex(box.findData(value))
            field.suggestion_chosen(box.currentIndex())
            assert field.add()
        seen["note"] = dialog.needs_note.text()
        dialog.inputs["--toolchain-offline"][1].setChecked(True)
        seen["offline"] = dialog.needs_note.text()
        dialog.inputs["--toolchain-offline"][1].setChecked(False)
        box.setEditText("rust")  # typed and not added yet: taken with Done
        box.lineEdit().setModified(True)
        seen["answers"] = dialog.answers()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(guide_view.NodeGuide, "exec", lambda self: answer(self))
    board.add_node("toolchains", guided=True)
    values = dict((value, label) for label, value in seen["items"])
    assert values["go"] == "go 1.27.3 (in the store)"
    assert values["jdk@17"] == "jdk 17.0.21+9 (add it first: sandbox-ai toolchain add jdk@17)", "NAME@VERSION lines"
    assert "rust" in values and "jdk" in values
    assert "go: opened at the start: proxy.golang.org, sum.golang.org, GitHub" in seen["note"]
    assert "jdk@17: opened at the start: repo.maven.apache.org" in seen["note"]
    assert "go: needs proxy.golang.org, sum.golang.org, GitHub - kept closed (you chose so)" in seen["offline"]
    assert seen["answers"]["--toolchain"] == ["go", "jdk@17", "rust"]
    assert board.board.first("toolchains").props["--toolchain"] == ["go", "jdk@17", "rust"]
    assert "--toolchain go --toolchain jdk@17 --toolchain rust" in board.command_text()


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_first_setup_of_toolchains_fits_its_texts_and_is_placed_by_the_rule(qapp, window, screens, monkeypatch,
                                                                                language):
    """The dialog with the list field and the registries below it, as the app shows a dialog (the one rule: big
    enough for everything, nothing cut, centred over the window - test_dialog_sizes' own measure), in both
    languages."""
    i18n.set_language(language)
    board = window.board
    window.show_page("board")
    board.add_node("toolchains", **{"--toolchain": ["go", "rust", "jdk@17"]})
    node = board.board.first("toolchains")
    measured = QtWidgets.QDialog.exec  # (the screens fixture's: shown, measured, cancelled)

    def on_its_question(dialog):
        dialog.show_page(1)  # the toolchains: the field, its chips, what each needs
        field = dialog.inputs["--toolchain"][1]
        assert wait_for(qapp, lambda: field.value_box.count() >= 5 and dialog.needs_note.text())
        return measured(dialog)
    monkeypatch.setattr(guide_view.NodeGuide, "exec", on_its_question)
    board.guide_node(node.id)
    assert screens.shown, "the first setup was shown"
    assert screens.found == [], "\n".join(screens.found)


def test_the_devices_question_offers_the_hosts_devices_without_the_gpu_kinds(qapp, window, monkeypatch):
    from sandbox_ai_app import device_picker as DP
    board = window.board
    monkeypatch.setattr(board, "device_suggestions", lambda fill, refresh=False: fill(
        [("ttyUSB0 (serial)", "/dev/ttyUSB0", "Serial ports"), ("NVIDIA", DP.gpu_value("nvidia"), "GPU")], ""))
    node = B.Node("n1", "devices", {})
    dialog = guide_view.NodeGuide(node, [], board)
    field = dialog.inputs["--device"][1]
    assert isinstance(field, LF.ListFields)
    values = [field.value_box.itemData(i) for i in range(field.value_box.count())]
    assert "/dev/ttyUSB0" in values and DP.gpu_value("nvidia") not in values, "the GPU is the part's own question"
    dialog.deleteLater()


# --- 3. a check box names its row once; the registries of each toolchain ------------------------------------------------

def test_a_check_boxs_row_names_it_once(qapp, window):
    board = window.board
    window.show_page("board")
    board.add_node("toolchains", **{"--toolchain": ["go"]})
    inspect(qapp, board, "toolchains")
    rows = form_rows(board)
    boxes = [(label, field) for label, field in rows if isinstance(field, QtWidgets.QCheckBox)]
    assert [field.text() for _, field in boxes] == ["Keep their registries closed"]
    assert all(label == "" for label, _ in boxes), "not the same words twice side by side"
    from sandbox_ai_app import labels
    labels.set_flags_shown(True)
    try:
        board.inspect(board.board.first("toolchains"))
        boxes = [(label, field) for label, field in form_rows(board) if isinstance(field, QtWidgets.QCheckBox)]
        assert boxes and boxes[0][0].startswith("--toolchain-offline"), "with options shown the label is the option"
    finally:
        labels.set_flags_shown(False)
    for kind in ("network", "git", "kits"):  # every part's flags, not only this one's
        inspect(qapp, board, kind)
        for label, field in form_rows(board):
            if isinstance(field, QtWidgets.QCheckBox) and label:
                assert label != field.text(), (kind, label)


def needs_of(qapp, board):
    assert wait_for(qapp, lambda: "Reading" not in board.needs_label.text())
    return board.needs_label.text().splitlines()


def test_the_toolchains_part_lists_each_ones_registries_and_what_the_start_does(qapp, window):
    board = window.board
    window.show_page("board")
    board.add_node("toolchains", **{"--toolchain": ["go", "rust", "cc", "zig"]})
    node = inspect(qapp, board, "toolchains")
    assert [label for label, _ in form_rows(board) if label][:2] == ["Build toolchains", "Registries"]
    assert needs_of(qapp, board) == [
        "go: opened at the start: proxy.golang.org, sum.golang.org, GitHub",
        "rust: opened at the start: crates.io, static.crates.io",
        "cc: needs no registry",
        "zig: not in the store or the signed catalog of this host - what it needs is not known"]
    from sandbox_ai_app.board_view import summary
    assert summary(board.board.node(node.id)) == "go, rust, cc, zig · registries opened"
    assert "registries opened" in " ".join(board.items[node.id].details), "on the part itself"
    # removable: the part's check box keeps them closed (--toolchain-offline)
    box = next(field for _, field in form_rows(board) if isinstance(field, QtWidgets.QCheckBox))
    box.setChecked(True)
    wait_for(qapp, lambda: not board.inspect_timer.isActive())
    assert needs_of(qapp, board)[0] == "go: needs proxy.golang.org, sum.golang.org, GitHub - kept closed (you chose so)"
    assert "--toolchain-offline" in board.command_text()
    assert summary(board.board.node(node.id)).endswith("registries kept closed")
    board.undo_group.undo()
    # a strict mode refuses them: said, not opened
    board.add_node("other", tokens=["--mode", "high"])
    board.show_properties(node.id)
    wait_for(qapp, lambda: board.edit_node == node.id and not board.inspect_timer.isActive())
    assert needs_of(qapp, board)[:2] == [
        "go: needs proxy.golang.org, sum.golang.org, GitHub - refused by mode high",
        "rust: needs crates.io, static.crates.io - refused by mode high"]


def test_the_launchers_plan_is_the_truth_about_what_is_opened():
    board = B.Board()
    board.add("agent", agent="claude")
    board.add("toolchains", **{"--toolchain": ["go"]})
    assert B.toolchain_needs(board, TOOLCHAINS)[0]["state"] == "opened"
    plan = {"mode": {"name": "medium"}, "toolchain_hosts": {"opened": ["sum.golang.org"],
                                                           "closed": ["proxy.golang.org", "--allow-github"]}}
    row = B.toolchain_needs(board, TOOLCHAINS, plan)[0]
    assert (row["state"], row["why"]) == ("refused", "policy")
    assert B.toolchain_needs_lines([row]) == [
        "go: needs proxy.golang.org, sum.golang.org, GitHub - refused by the administrator's policy"]
    strict = {"mode": {"name": "paranoid"}, "toolchain_hosts": {"opened": [], "closed": ["proxy.golang.org"]}}
    assert B.toolchain_needs(board, TOOLCHAINS, strict)[0]["why"] == "mode paranoid"
    assert B.toolchain_registries("rust", TOOLCHAINS) == (["crates.io", "static.crates.io"], []), \
        "with what the toolchains it requires need (cc: nothing)"
    assert B.toolchain_registries("nope", TOOLCHAINS) is None
    i18n.set_language("de")
    try:
        assert B.toolchain_needs_lines([row]) == [
            "go: braucht proxy.golang.org, sum.golang.org, GitHub - von der Richtlinie des Administrators abgelehnt"]
    finally:
        i18n.set_language("en")


# --- 4. the properties: big enough for everything, centred when they open floating --------------------------------------

def test_the_docked_properties_are_as_wide_as_their_rows_need(qapp, window):
    board = window.board
    window.show_page("board")
    board.add_node("toolchains", **{"--toolchain": ["go", "rust"]})
    dock, fold = window.docks["inspector"], window.inspector_fold
    fold.sizes["width"] = 260  # as the user left it: narrower than most parts' rows
    inspect(qapp, board, "toolchains")
    wait_for(qapp, lambda: "Reading" not in board.needs_label.text())
    wait_for(qapp, lambda: False, 0.3)
    cap = int(window.workspace.width() * dock_fold.DOCK_SHARE)
    assert dock.width() >= min(dock_fold.least_width(dock), cap) - 2 > 260, dock.width()
    assert not board.inspector_scroll.horizontalScrollBar().isVisible(), "nothing of a row is cut"
    narrow = dock.width()
    inspect(qapp, board, "agent")  # a part with wider rows: wider, within the cap (the canvas keeps its room)
    wait_for(qapp, lambda: False, 0.3)
    assert narrow < dock.width() <= cap + 2
    assert dock.width() >= min(dock_fold.least_width(dock), cap) - 2
    inspect(qapp, board, "toolchains")  # back: as wide as this part needs, not as the widest one seen
    wait_for(qapp, lambda: False, 0.3)
    assert abs(dock.width() - narrow) <= 4, (dock.width(), narrow)
    assert fold.sizes["width"] == 260, "the width the user chose is kept: a part that needs less gets it back"


def test_the_floating_properties_open_centred_and_big_enough(qapp, window):
    board = window.board
    window.setGeometry(40, 30, 700, 560)  # (inside the offscreen screen: there is room to centre a panel over it)
    window.show_page("board")
    board.add_node("toolchains", **{"--toolchain": ["go", "rust"]})
    dock = window.docks["inspector"]
    dock.setFloating(True)
    dock.setGeometry(QRect(5, 5, 200, 150))  # where and how small it was left
    qapp.processEvents()
    window.closed_docks.add("inspector")
    window.update_docks()
    assert not dock.isVisible()
    inspect(qapp, board, "toolchains")
    wait_for(qapp, lambda: False, 0.3)
    assert dock.isVisible() and dock.isFloating()
    need = dock_fold.content_size(dock)
    screen = dock.screen().availableGeometry()
    assert dock.width() >= min(need.width(), int(screen.width() * dock_fold.SCREEN_SHARE)) - 2
    assert dock.height() >= min(need.height(), int(screen.height() * dock_fold.SCREEN_SHARE)) - 2
    assert screen.contains(dock.geometry())
    wanted = dock_fold.float_geometry(dock.size(), dock.size(), window.frameGeometry(), screen)
    assert (dock.geometry().center() - wanted.center()).manhattanLength() <= 4, \
        "centred over the app's window (as far as the screen lets it)"
    assert abs(dock.geometry().center().x() - window.frameGeometry().center().x()) <= 4, (dock.geometry(),
                                                                                         window.frameGeometry())
    # another part while it is open: it grows where it is, never shrinks, and is not moved back to the centre
    dock.move(dock.x() + 30, dock.y() + 20)
    wait_for(qapp, lambda: False, 0.6)  # (past the placement's quiet time: the move is the user's)
    before = dock.geometry()
    inspect(qapp, board, "network")
    wait_for(qapp, lambda: False, 0.3)
    after = dock.geometry()
    assert after.width() >= before.width() and after.height() >= before.height()
    assert screen.contains(after)
    kept = dock_fold.float_geometry(after.size(), after.size(), before, screen)  # (around where the user put it)
    assert (after.center() - kept.center()).manhattanLength() <= 4, "not moved back over the window's centre"


# --- 5. the Board follows the session one comes from -------------------------------------------------------------------

def terminal_tab(window, name):
    terminals = window.terminals
    view = FakeView(["sandbox-ai", "attach", name])
    view.start_name, view.start_host = name, "local"
    terminals.tabs.addTab(view, name)
    terminals.tabs.setCurrentWidget(view)
    return view


def test_coming_from_a_terminal_the_board_shows_its_sessions_tab_and_project(qapp, window, projects):
    web, api = projects
    window.set_project(str(web))
    window.show_page("terminals")
    terminal_tab(window, "task")  # the session of the project api
    window.show_page("board")
    qapp.processEvents()
    assert window.current_page() == "board"
    entry = window.tabs.entry(window.tabs.currentIndex())
    assert entry["key"] == "session:task" and os.path.samefile(entry["path"], api)
    assert os.path.samefile(window.project, api) and os.path.samefile(window.board.directory, api)
    agent = window.board.board.first("agent")
    assert window.board.items[agent.id].isSelected()
    # back to the other session's terminal, then to the Expert view: the same start follows too
    window.show_page("terminals")
    terminal_tab(window, "work")
    window.show_page("launch")
    assert window.tabs.entry(window.tabs.currentIndex())["key"] == "session:work"
    assert os.path.samefile(window.board.directory, web)


def test_coming_from_sessions_the_board_shows_the_selected_one_and_nothing_else_changes_it(qapp, window, projects):
    web, api = projects
    window.set_project(str(web))
    window.show_page("sandboxes")
    assert window.show_instance("session:task")
    window.show_page("board")
    assert window.tabs.entry(window.tabs.currentIndex())["key"] == "session:task"
    assert os.path.samefile(window.board.directory, api)
    # from a page without a session (Help), or with an MCP server's row selected: the Board stays as it is
    window.set_project(str(web))
    window.show_page("help")
    window.show_page("board")
    assert os.path.samefile(window.board.directory, web)
    window.show_page("sandboxes")
    assert window.show_instance("mcp:fetch")
    window.show_page("board")
    assert os.path.samefile(window.board.directory, web)
    assert window.notices == [], "following says nothing and asks nothing"


def test_a_board_with_changes_is_kept_when_the_board_follows_another_session(qapp, window, projects):
    web, api = projects
    window.set_project(str(web))
    window.show_page("board")
    window.board.add_node("limits", **{"--memory": "4g"})
    window.show_page("terminals")
    terminal_tab(window, "task")
    window.show_page("board")
    assert os.path.samefile(window.board.directory, api) and window.board.board.first("limits") is None
    window.set_project(str(web))
    assert window.board.board.first("limits").props["--memory"] == "4g", "its unapplied change is still there"


# --- 6. the agent's intro: the Agent part's check box and the Settings' default -----------------------------------------

def intro_box(qapp, board):
    inspect(qapp, board, "agent")
    return board.intro_box


def test_the_agent_part_has_the_intro_check_box_on_and_clearing_it_is_no_intro(qapp, window):
    board = window.board
    window.show_page("board")
    box = intro_box(qapp, board)
    assert box.isChecked() and box.text() == "Tell the agent what its sandbox has"
    assert "INTRO.md" in box.toolTip() and "--no-intro" not in board.command_text()
    box.setChecked(False)
    wait_for(qapp, lambda: not board.inspect_timer.isActive())
    assert "--no-intro" in board.command_text()
    assert not intro_box(qapp, board).isChecked()
    board.undo_group.undo()
    assert "--no-intro" not in board.command_text(), "one step of Undo"
    # a command line with --no-intro put on the board shows it cleared (the same start in both views)
    board.take_start("claude", ["--no-intro"])
    assert not intro_box(qapp, board).isChecked()
    assert not any(isinstance(field, QtWidgets.QCheckBox) and "Do not tell" in field.text()
                   for _, field in form_rows(board)), "one check box, not the option a second time"


def test_with_the_default_off_ticking_it_is_intro(qapp, window):
    board = window.board
    window.sai.save_sandbox_default("agent_intro", False)
    window.show_page("board")
    box = intro_box(qapp, board)
    assert not box.isChecked()
    box.setChecked(True)
    wait_for(qapp, lambda: not board.inspect_timer.isActive())
    assert "--intro" in board.command_text() and "--no-intro" not in board.command_text()
    intro_box(qapp, board).setChecked(False)
    wait_for(qapp, lambda: not board.inspect_timer.isActive())
    assert "intro" not in board.command_text(), "back to the default: no option at all"


def test_settings_keep_the_default_for_every_start(qapp, window):
    dialog = window.settings_dialog()
    try:
        box = dialog.agent_intro
        assert box.isChecked() and box.isEnabled()
        box.setChecked(False)
        assert window.sai.sandbox_defaults()[0].get("agent_intro") is False
        box.setChecked(True)
        assert "agent_intro" not in window.sai.sandbox_defaults()[0], "on is sandbox-ai's own default: unset"
        with pytest.raises(ValueError, match="agent_intro is false, or unset"):
            window.sai.save_sandbox_default("agent_intro", "no")
    finally:
        dialog.deleteLater()


def test_the_launcher_takes_what_the_board_writes(window):
    """The board's start with the box cleared is a start the launcher plans without an intro (its own dry run)."""
    board = window.board
    board.take_start("claude", ["--no-intro"])
    plan, message = window.sai.host_dry_run("local", "claude", B.check_argv(window.sai, board.board))
    assert plan is not None, message
    assert plan["intro"] is None
    board.undo_group.undo()
    plan, message = window.sai.host_dry_run("local", "claude", B.check_argv(window.sai, board.board))
    assert plan is not None and plan["intro"]["file"] == "/sandbox/session/INTRO.md", message


# --- 7. what the Board adds is in the launcher's plan of its start (a dry run of the board's own command line) ----------

def plan_of_board(window):
    board = window.board
    agent = board.board.first("agent").props.get("agent", "claude")
    plan, message = window.sai.host_dry_run("local", agent, B.check_argv(window.sai, board.board))
    assert plan is not None, message
    return plan


def test_folders_added_on_the_board_are_mounted_with_the_chosen_mode(window, projects, isolated):
    web, api = projects
    data = isolated / "datasets"
    data.mkdir()
    board = window.board
    window.set_project(str(web))
    board.add_node("workspace", path=str(api), readonly=False)
    board.add_node("mount", source=str(data), dest="/mnt/data", mode="ro")
    plan = plan_of_board(window)
    assert [entry["sandbox"] for entry in plan["workspaces"]] == ["/work", "/workspaces/api"]
    assert plan["mounts"] == [f"{data}:/mnt/data:ro"] and plan["workspace_mode"] == "rw"
    text = plan["intro"]["text"]
    assert "- `/workspaces/api` - a further project folder, read-write" in text
    assert "- `/mnt/data` - an extra folder or file, read-only" in text and str(isolated) not in text


def test_a_toolchain_added_on_the_board_is_planned_with_its_registries(window, projects, isolated, monkeypatch):
    import sys
    sys.path.insert(0, str(backend.Path(os.environ["SANDBOX_AI_HOME"]) / "tests"))
    import toolchain_fakes as TF
    TF.install(isolated / "toolchain-catalog", ["go"])  # (into this test's store: XDG_DATA_HOME)
    try:
        board = window.board
        window.set_project(str(projects[0]))
        board.add_node("toolchains", **{"--toolchain": ["go"]})
        plan = plan_of_board(window)
        assert plan["toolchains"][0]["sandbox"] == "/opt/toolchains/go" and plan["toolchains"][0]["mode"] == "ro"
        assert plan["toolchain_hosts"]["opened"] == ["proxy.golang.org", "sum.golang.org", "--allow-github"]
        assert {"proxy.golang.org", "sum.golang.org"} <= set(plan["https_hosts"]), "the rule it needs: set by itself"
        board.add_node("other", tokens=["--mode", "high"])
        strict = plan_of_board(window)
        assert strict["toolchain_hosts"]["opened"] == [] and "proxy.golang.org" not in strict["https_hosts"]
        rows = B.toolchain_needs(board.board, TOOLCHAINS, strict)
        assert B.toolchain_needs_lines(rows) == [
            "go: needs proxy.golang.org, sum.golang.org, GitHub - refused by mode high"]
    finally:
        TF.writable(isolated)


def test_an_mcp_server_added_on_the_board_is_in_the_plan_and_the_agents_intro(window, projects):
    board = window.board
    window.set_project(str(projects[0]))
    board.add_node("mcp", name="demo")
    plan = plan_of_board(window)
    assert [entry["name"] for entry in plan["mcp"]] == ["demo"]
    assert "## MCP servers" in plan["intro"]["text"] and "\n- demo\n" in plan["intro"]["text"]


def test_a_device_added_on_the_board_is_in_the_plan(window, projects):
    board = window.board
    window.set_project(str(projects[0]))
    board.add_node("devices", **{"--device": ["/dev/null"]})
    plan, message = window.sai.host_dry_run("local", "claude", B.check_argv(window.sai, board.board))
    if plan is None:  # (a host whose launcher refuses /dev/null as a device: the refusal names it, nothing is hidden)
        assert "/dev/null" in message
        return
    assert plan["devices"] == [{"path": "/dev/null", "kind": "node"}]
    assert "- `/dev/null`" in plan["intro"]["text"]


# --- 9. the Network part lists, read-only, what the other parts open (the launcher's own account: the plan's "reach") ----

REACH = [  # as agent_sandbox.reach() gives it (the dry run's plan "reach", run.json "reach")
    {"kind": "preset", "what": "claude", "source": "provider", "detail": "claude", "hosts": ["api.anthropic.com"]},
    {"kind": "https", "what": "example.org", "source": "option"},
    {"kind": "preset", "what": "--allow-github", "source": "toolchain", "detail": "go, rust",
     "hosts": ["github.com", "api.github.com"]},
    {"kind": "https", "what": "proxy.golang.org", "source": "toolchain", "detail": "go, rust"},
    {"kind": "https", "what": "crates.io", "source": "toolchain", "detail": "go, rust"},
    {"kind": "https", "what": "git.example.org", "source": "git-login"},
    {"kind": "https", "what": "api.example.com", "source": "api-key"},
    {"kind": "tcp", "what": "192.0.2.10:11434", "source": "llm", "detail": "lab"},
    {"kind": "https", "what": "telegram.org", "source": "feature"},
    {"kind": "https", "what": "registry.npmjs.org", "source": "publish"},
    {"kind": "https", "what": "kept.example", "source": "approved", "scope": "every start of the project"}]


def test_what_other_parts_open_is_taken_from_the_plans_reach_and_nothing_else():
    board = B.Board()
    board.add("agent", agent="claude")
    board.add("toolchains", **{"--toolchain": ["go", "rust"]})
    assert B.opened_by_parts(board, None) is None, "no check yet"
    assert B.opened_by_parts(board, {"mode": {"name": "medium"}}) is None, "a sandbox-ai that does not say it"
    found = B.opened_by_parts(board, {"reach": REACH, "mode": {"name": "medium"},
                                      "toolchain_hosts": {"opened": ["proxy.golang.org"], "closed": []}})
    assert [entry["source"] for entry in found["opened"]] == [
        "toolchain", "toolchain", "toolchain", "git-login", "api-key", "llm", "feature", "publish"], \
        "another part's: not the provider, the start's own options or an approval"
    assert found["closed"] == [] and found["why"] == ""
    assert set(entry["source"] for entry in found["opened"]) == set(B.REACH_PARTS)
    strict = B.opened_by_parts(board, {"reach": REACH[:2], "mode": {"name": "high"}, "toolchain_hosts": {
        "opened": [], "closed": ["proxy.golang.org", "--allow-github"]}})
    assert strict == {"opened": [], "closed": ["proxy.golang.org", "--allow-github"], "why": "mode high"}
    board.first("toolchains").props["--toolchain-offline"] = True
    chosen = B.opened_by_parts(board, {"reach": REACH[:2], "mode": {"name": "medium"}, "toolchain_hosts": {
        "opened": [], "closed": ["proxy.golang.org"]}})
    assert chosen["why"] == "offline"
    board.first("toolchains").props.pop("--toolchain-offline")
    policy = B.opened_by_parts(board, {"reach": REACH[:2], "mode": {"name": "medium"}, "toolchain_hosts": {
        "opened": [], "closed": ["proxy.golang.org"]}})
    assert policy["why"] == "policy", "neither the mode nor the board: the administrator's policy"


def network_reach(qapp, board, lines=None):
    """The Network part's list once the launcher checked the board as it is now (its lines)."""
    assert wait_for(qapp, lambda: board.plan_found[0] == board.board.state() and board.plan_found[1] is not None
                    and "in a moment" not in board.reach_label.text(), 60), board.reach_label.text()
    return board.reach_label.text().splitlines()


def test_the_network_part_lists_what_the_toolchains_open_and_closes_it_from_there(qapp, window, projects, isolated):
    import sys
    sys.path.insert(0, str(backend.Path(os.environ["SANDBOX_AI_HOME"]) / "tests"))
    import toolchain_fakes as TF
    TF.install(isolated / "toolchain-catalog", ["go"])  # (the launcher's own store of this test: its real dry run)
    try:
        board = window.board
        window.set_project(str(projects[0]))
        window.show_page("board")
        inspect(qapp, board, "network")
        assert network_reach(qapp, board) == ["Nothing: no other part opens a destination."]
        assert not board.reach_button.isVisibleTo(board.side)
        board.add_node("toolchains", **{"--toolchain": ["go"]})
        inspect(qapp, board, "network")
        assert network_reach(qapp, board) == [
            "GitHub, proxy.golang.org, sum.golang.org - opened by a toolchain's registries (go)"]
        labels = [label for label, _ in form_rows(board) if label]
        assert labels[-1] == "Opened by other parts", labels
        # removable where the rule allows it: the Toolchains part's own switch, one step of Undo
        assert board.reach_button.isVisibleTo(board.side)
        assert board.reach_button.text() == "Keep the toolchains' registries closed"
        board.reach_button.click()
        wait_for(qapp, lambda: not board.inspect_timer.isActive())
        assert "--toolchain-offline" in board.command_text()
        assert network_reach(qapp, board) == [
            "proxy.golang.org, sum.golang.org, GitHub - the toolchains need them; kept closed (the Toolchains part "
            "says so, or the board is offline)"]
        assert board.reach_button.text() == "Open the toolchains' registries"
        board.reach_button.click()
        wait_for(qapp, lambda: not board.inspect_timer.isActive())
        assert "--toolchain-offline" not in board.command_text()
        assert network_reach(qapp, board)[0].startswith("GitHub, proxy.golang.org, sum.golang.org - opened by")
        # a strict mode refuses them: said, nothing to open from here
        board.add_node("other", tokens=["--mode", "high"])
        inspect(qapp, board, "network")
        assert network_reach(qapp, board) == [
            "proxy.golang.org, sum.golang.org, GitHub - the toolchains need them; refused by mode high"]
        assert not board.reach_button.isVisibleTo(board.side)
        i18n.set_language("de")
        board.inspect(board.board.first("network"))
        assert board.reach_label.text() == ("proxy.golang.org, sum.golang.org, GitHub - die Toolchains brauchen sie; "
                                            "abgelehnt durch mode high")
    finally:
        i18n.set_language("en")
        TF.writable(isolated)


def test_the_network_part_words_every_source_as_approvals_does(qapp, window):
    """A plan with every source another part can be (the launcher's reach()): one line per source, worded with the
    Approvals page's own words (approvals_sections.REACH_SOURCES), the provider's and the options' own left out."""
    board = window.board
    board.add_node("toolchains", **{"--toolchain": ["go", "rust"]})
    board.plan_found = (board.board.state(), {"reach": REACH, "mode": {"name": "medium"},
                                             "toolchain_hosts": {"opened": [], "closed": []}})
    lines, removable = board.reach_text()
    assert lines == [
        "GitHub, proxy.golang.org, crates.io - opened by a toolchain's registries (go, rust)",
        "git.example.org - opened by a Git login",
        "api.example.com - opened by an API key",
        "192.0.2.10:11434 - opened by --llm (lab)",
        "telegram.org - opened by an agent feature",
        "registry.npmjs.org - opened by a publish login"] and removable
    board.plan_found = (board.board.state(), {"mode": {"name": "medium"}})  # a launcher that does not say it
    assert board.reach_text() == (["Not known: the sandbox-ai that checks this board does not say what a start may "
                                   "reach (0.1.245 and newer do)."], False)
    board.plan_found = ("another state", None)
    assert board.reach_text()[0] == ["The launcher's check of the board says it in a moment…"]


# --- 8. Show on the Board: Sessions (button, right-click menu) and a terminal tab's menu -------------------------------------

SHOW = "Show on the Board"


def test_sessions_has_show_on_board_as_a_button_and_first_in_the_menu(qapp, window, projects):
    web, api = projects
    window.set_project(str(web))
    page = window.sandboxes
    window.show_page("sandboxes")
    page.table.clearSelection()
    page.table.setCurrentItem(None)
    page.update_buttons()
    assert not page.board_button.isEnabled() and "Select one session" in page.board_button.toolTip()
    assert window.show_instance("mcp:fetch")
    assert not page.board_button.isEnabled() and "MCP server" in page.board_button.toolTip()
    assert window.show_instance("session:task")
    assert page.board_button.isEnabled() and page.board_button.label == SHOW
    assert "Show the Board with this session's project" in page.board_button.toolTip()
    entries = widgets.menu_entries(widgets.menu_of(page.table))
    assert entries[0] == (SHOW, True), "the first entry of the right-click menu, before Attach"
    menu = widgets.menu_of(page.table)
    next(a for a in menu.actions() if a.text() == SHOW).trigger()
    qapp.processEvents()
    assert window.current_page() == "board"
    assert window.tabs.entry(window.tabs.currentIndex())["key"] == "session:task"
    assert os.path.samefile(window.board.directory, api)
    assert window.board.items[window.board.board.first("agent").id].isSelected(), "the session's Agent part"
    # the button does the same for another row
    window.show_page("sandboxes")
    assert window.show_instance("session:work")
    page.board_button.click()
    assert window.current_page() == "board" and os.path.samefile(window.board.directory, web)


def test_a_terminal_tabs_menu_has_show_on_board(qapp, window, projects):
    web, api = projects
    window.set_project(str(web))
    window.show_page("terminals")
    terminals = window.terminals
    task = terminal_tab(window, "task")
    work = terminal_tab(window, "work")  # the current tab; the menu is opened on the other one
    build = FakeView(["sandbox-ai", "build", "claude"])
    terminals.tabs.addTab(build, "build")
    actions = {entry[0]: entry for entry in terminals.tab_actions(terminals.tabs.indexOf(task)) if entry
               and isinstance(entry, tuple)}
    text, enabled, why, act = actions[SHOW]
    assert enabled and why is None
    act()
    qapp.processEvents()
    assert window.current_page() == "board"
    assert window.tabs.entry(window.tabs.currentIndex())["key"] == "session:task", \
        "the tab it was asked for, not the current terminal tab's"
    assert os.path.samefile(window.board.directory, api)
    blocked = {entry[0]: entry for entry in terminals.tab_actions(terminals.tabs.indexOf(build)) if entry
               and isinstance(entry, tuple)}[SHOW]
    assert not blocked[1] and blocked[2], "a build's tab says why not"
    # the terminal's own right-click menu (FakeView has none): the real view's entries
    # the terminal's own right-click menu: the real view's entries (no Chromium here: a stand-in for the tab `work`)
    page = SimpleNamespace(board_request=terminals.board_request, board_entry=terminals.board_entry,
                           instance_of=lambda _view: terminals.instance_of(work))
    stand = SimpleNamespace(terminals_page=lambda: page, run_js=lambda *a: None, helper=None)
    entries = REAL_VIEW.menu_actions(stand, {})
    assert (SHOW, True) in [(text, enabled) for text, enabled, _ in entries]


def test_show_on_board_builds_the_board_from_the_sessions_start_when_the_project_has_none(qapp, window, projects,
                                                                                         monkeypatch):
    web, api = projects
    monkeypatch.setattr(window.sai, "session_start", lambda ident: (
        "codex", ["--allow-pypi", "--memory", "6g"], []) if ident == "task" else None)
    window.show_page("sandboxes")
    assert window.show_instance("session:task")
    window.sandboxes.board_button.click()
    board = window.board
    assert board.board.first("agent").props.get("agent") == "codex"
    assert board.board.first("limits").props["--memory"] == "6g", "what the session was started with"
    assert board.board.first("network").props.get("--allow-pypi") is True
    assert not board.dirty(), "its base, not a change to apply"


def test_show_on_board_for_a_session_of_another_host(qapp, window, monkeypatch):
    board = window.board
    read = []
    monkeypatch.setattr(window.sai, "host_project_show", lambda host, path: read.append((host, path)) or {
        "document": {"version": 1, "agent": "claude", "options": ["--allow-github"]}, "trusted": True, "digest": "d"})
    page = window.sandboxes
    window.show_page("sandboxes")
    lab = next(i for i in page.instances.values() if getattr(i, "host", "local") == "ct1")
    assert window.show_instance(lab.key)
    assert page.board_button.isEnabled()
    page.board_button.click()
    assert window.current_page() == "board" and board.host() == "ct1", "that host chosen beside Apply"
    assert wait_for(qapp, lambda: board.remote_project == ("ct1", "/srv/lab")), board.remote_project
    assert read and read[0][0] == "ct1" and read[0][1].startswith("/srv/lab/")
    assert board.board.first("network").props.get("--allow-github") is True
    assert wait_for(qapp, lambda: board.items[board.board.first("agent").id].isSelected())


def test_a_session_whose_folder_is_gone_says_so(qapp, window, projects):
    web, api = projects
    api.rmdir()
    window.set_project(str(web))
    window.show_page("sandboxes")
    assert window.show_instance("session:task")
    window.sandboxes.board_button.click()
    assert window.notices and window.notices[-1][0] == SHOW and "not there any more" in window.notices[-1][1]
    assert window.current_page() == "sandboxes" and os.path.samefile(window.board.directory, web)


@pytest.mark.parametrize("language, text", [("en", "Show on the Board"), ("de", "Auf dem Board zeigen")])
def test_show_on_board_in_both_languages(qapp, isolated, sai, language, text):
    i18n.set_language(language)
    shown = make_window(qapp, sai)
    try:
        assert shown.sandboxes.board_button.label == text
        assert any(entry and isinstance(entry, tuple) and entry[0] == text
                   for entry in [shown.terminals.board_entry(None)])
        assert board_follow.why_not(None)
    finally:
        i18n.set_language("en")
        close(qapp, shown)
