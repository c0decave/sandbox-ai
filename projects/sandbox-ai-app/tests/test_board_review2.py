"""Board review 2 (user journeys and robustness, docs/reviews/board-review-2.md): each finding's regression test,
through the real entry points - the Board page offscreen and the app's main window with its tabs and panels.

The journeys behind them (a board built from the palette by keyboard, pasted from a clipboard another program filled,
on a host that does not answer, across project tabs and Change folder, typed into while late answers arrive) are
walked at random in test_board_review2_journeys.py; these are the findings, one by one."""
import json
import os
import sys
import threading
import time
import traceback

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QMimeData, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit  # noqa: E402

from sandbox_ai_app import backend, board as B, board_view as BV, i18n, name_fields, widgets  # noqa: E402

from test_projects import close, env, make_window, window  # noqa: E402,F401 - shared fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

HOSTS = [{"name": "lab", "kind": "ssh", "ssh": "me@lab.example"}]
UNREACHABLE = "ssh: connect to host lab.example port 22: Connection timed out"


@pytest.fixture
def raised(monkeypatch):
    """What a slot raised (Qt hands it to sys.excepthook and goes on): a test of a journey fails for any."""
    seen = []
    monkeypatch.setattr(sys, "excepthook",
                        lambda kind, error, trace: seen.append("".join(traceback.format_exception(kind, error, trace))))
    return seen


def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])


def make_page(sai, qapp, **settings):
    view = BV.BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False, **settings})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    return view


def end_page(view):
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    isolated(tmp_path, monkeypatch)
    view = make_page(backend.SandboxAI(), qapp)
    yield view
    end_page(view)


@pytest.fixture
def lab(qapp, tmp_path, monkeypatch):
    """A Board whose host "lab" does not answer: every command there fails as ssh fails (status 255), in a worker -
    one made on the GUI thread is recorded (sai.on_gui)."""
    isolated(tmp_path, monkeypatch)
    sai = backend.SandboxAI()
    sai.ran, sai.on_gui, sai.delay = [], [], 0.0
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [dict(h) for h in HOSTS])

    def host_run(name, *args, **kw):
        sai.ran.append((name, *args))
        if threading.current_thread() is threading.main_thread():
            sai.on_gui.append(args)
        else:
            time.sleep(sai.delay)
        return 255, "", UNREACHABLE
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(BV, "REMOTE_CHECK_DELAY", 20)
    view = make_page(sai, qapp, board_check_delay=20)
    yield view
    sai.delay = 0.0
    end_page(view)


def pump(qapp, rounds=5):
    for _ in range(rounds):
        qapp.processEvents()


def settled(page, wait):
    """The inspector made, and every read it started answered."""
    assert wait(lambda: not page.inspect_timer.isActive(), 10)
    widgets.ThreadCall.wait_all(30)
    assert wait(lambda: not page.inspect_timer.isActive(), 10)
    pump(QApplication.instance())


# -- R2-1: parts pasted from a clipboard another program filled raised in the slot, some half added --

def clip(parts, edges=()):
    mime = QMimeData()
    mime.setData(BV.MIME_PARTS, json.dumps({"format": BV.PARTS_FORMAT, "version": 1, "parts": parts,
                                            "edges": list(edges)}).encode())
    QApplication.clipboard().setMimeData(mime)


FOREIGN = [
    ([{"id": [1], "kind": "mount", "props": {"source": "/a", "dest": "/mnt/a", "mode": "ro"}}], ()),  # id: a list
    ([{"id": 1, "kind": "mount", "props": {"connect": True}}], ()),  # a setting named as Board.add's own argument
    ([{"id": 1, "kind": "mount", "props": {"kind": "x"}}], ()),
    ([{"id": 1, "kind": "mount", "props": {"source": "/a"}}], [[[1], {"a": 2}]]),  # an edge of lists
    ([{"id": 1, "kind": "mcp", "props": {"name": ["x"], "limits": "zz", "env": 5}}], ()),  # a name that is a list
    ([{"id": 1, "kind": "service", "props": {"name": "db", "image": "db", "env": ["x"]}}], ()),
    ([{"id": 1, "kind": "workspace", "props": {"path": 7, "readonly": "x"}}], ()),
    ([{"id": 1, "kind": "limits", "props": {"--memory": ["4g"], "--cpus": {"a": 1}}}], ()),
    ([{"id": 1, "kind": "network", "props": {}}, {"id": 2, "kind": "vpn", "props": {"--vpn": [1, 2]}}], [[2, 1]]),
]


@pytest.mark.parametrize("index", range(len(FOREIGN)))
def test_a_paste_of_what_another_program_put_on_the_clipboard_never_raises(page, qapp, raised, wait, index):
    parts, edges = FOREIGN[index]
    clip(parts, edges)
    before = page.undo.count()
    page.paste(QPointF(100, 100))
    pump(qapp)
    for item in list(page.items.values()):  # every part there now can be shown: on the board and in the inspector
        page.scene.clearSelection()
        item.setSelected(True)
        assert wait(lambda: not page.inspect_timer.isActive(), 10)
        pump(qapp, 2)
    widgets.ThreadCall.wait_all(30)
    pump(qapp)
    assert not raised, raised[0][-3000:]
    assert set(page.items) == {node.id for node in page.board.nodes}, "the scene shows exactly the model's parts"
    assert page.undo.count() <= before + 1, "at most one step of Undo"
    page.preview.toPlainText()
    B.command_line(page.sai, page.board)


def test_a_part_that_cannot_be_read_is_left_out_and_said_the_others_are_pasted(page, qapp, raised):
    clip([{"id": 1, "kind": "mcp", "props": {"name": ["x"]}},
          {"id": 2, "kind": "mount", "props": {"source": "/data", "dest": "/mnt/data", "mode": "ro"}}])
    shown = len(widgets.SHOWN)
    added = page.paste(QPointF(60, 60))
    assert [page.board.node(i).kind for i in added] == ["mount"]
    assert page.board.of_kind("mcp") == []
    said = [text for _title, text, *_ in widgets.SHOWN[shown:]]
    assert any("1" in text and "cannot be read" in text for text in said), said
    page.undo.undo()
    assert page.board.of_kind("mount") == []
    assert not raised, raised[0][-3000:]


def test_a_part_of_this_app_is_still_pasted_with_its_settings_and_connections(page, qapp):
    page.add_node("mount", source="/data", dest="/mnt/data", mode="rw")
    page.add_node("limits")
    page.set_prop(page.board.first("limits").id, "--memory", "4g")
    page.select_all()
    assert page.copy_selected() == 5
    page.undo.setIndex(0)
    assert page.board.of_kind("mount") == [] and page.board.first("limits") is None
    added = page.paste()
    kinds = sorted(page.board.node(i).kind for i in added)
    assert kinds == ["limits", "mount"], "the agent, the proxy and the network are there already"
    assert page.board.first("limits").props == {"--memory": "4g"}
    mount = page.board.of_kind("mount")[0]
    assert mount.props == {"source": "/data", "dest": "/mnt/data", "mode": "rw"} and page.board.connected(mount)


def test_properties_the_app_cannot_show_say_so_and_still_offer_remove(page, qapp, raised, wait, monkeypatch):
    """A part whose settings no check refuses but the inspector cannot show (a draft or a file of another version):
    said in the inspector, with Remove - not an exception in the slot and the rows of the part shown before."""
    page.add_node("mount", source="/data", dest="/mnt/data", mode="ro")
    node = page.board.of_kind("mount")[0]
    rows = page.inspect_rows

    def broken(shown):
        rows(shown)  # (some rows are made, then it fails)
        raise TypeError("QComboBox.setEditText called with wrong argument types")
    monkeypatch.setattr(page, "inspect_rows", lambda shown: broken(shown) if shown.kind == "mount" else rows(shown))
    page.show_properties(node.id)
    assert wait(lambda: not page.inspect_timer.isActive(), 10)
    pump(qapp)
    assert not raised, raised[0][-3000:]
    texts = [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)]
    assert any("cannot be shown" in text for text in texts), texts
    remove = [b for b in page.inspector_holder.findChildren(QtWidgets.QPushButton) if b.text() == "Remove"]
    assert remove, "the part can still be removed"
    remove[0].click()
    assert page.board.node(node.id) is None


# -- R2-2: parts added from the palette (double click, Enter: the keyboard's way) landed on each other --

def overlapping(page):
    rects = {ident: item.sceneBoundingRect() for ident, item in page.items.items()}
    idents = sorted(rects)
    return [(a, b) for index, a in enumerate(idents) for b in idents[index + 1:] if rects[a].intersects(rects[b])]


def test_many_parts_added_from_the_palette_never_cover_each_other(page, qapp):
    for index in range(36):
        page.add_node(("mount", "workspace", "mcp", "service", "bridge", "mount")[index % 6], guided=True)
    for kind, known in B.KINDS.items():
        if known.single and page.board.first(kind) is None:
            page.add_node(kind, guided=True)
    pump(qapp)
    assert len(page.board.nodes) >= 36 + len(B.KINDS) - 6
    assert overlapping(page) == []
    grid = page.grid()
    assert all(node.x % grid == 0 and node.y % grid == 0 for node in page.board.nodes), "on the grid"


def test_the_first_parts_still_go_into_their_kinds_column(page, qapp):
    page.add_node("mount")
    page.add_node("limits")
    page.add_node("mcp")
    mount, limits, mcp = (page.board.of_kind(kind)[0] for kind in ("mount", "limits", "mcp"))
    assert mount.x == limits.x == 40.0 and mcp.x == 40.0 + 2 * 260.0
    assert overlapping(page) == []


# -- R2-3: what was being typed in a property was dropped when a late answer made the inspector anew --

def field_of(page, option):
    field = page.option_fields[option]
    return field if isinstance(field, QLineEdit) else field.findChildren(QLineEdit)[0]


@pytest.mark.parametrize("kind,option", [("limits", "--memory"), ("home", "--state-dir"), ("vpn", "--vpn"),
                                         ("engine", "--image-variant")])
def test_what_is_typed_in_a_field_survives_the_inspector_made_anew(page, qapp, raised, wait, kind, option):
    page.activateWindow()
    page.add_node(kind)
    node = page.board.first(kind)
    page.show_properties(node.id)
    settled(page, wait)
    edit = field_of(page, option)
    edit.setFocus()
    pump(qapp)
    QTest.keyClicks(edit, "abc123")
    steps = page.undo.count()
    page.inspect_timer.start()  # a late answer (the host's model servers, images, sessions, a team's verdict ...)
    settled(page, wait)
    again = field_of(page, option)
    assert again is not edit, "the inspector was made anew"
    assert again.text() == "abc123", "what was typed is still in the field"
    assert again.hasFocus() and again.cursorPosition() == 6
    assert page.undo.count() == steps and option not in page.board.node(node.id).props, "not taken before it is left"
    again.editingFinished.emit()  # leaving the field takes it, as ever
    assert page.board.node(node.id).props[option] == "abc123"
    assert not raised, raised[0][-3000:]


def test_a_field_not_typed_in_follows_the_model_when_the_inspector_is_made_anew(page, qapp, wait):
    """Undo while the keyboard is in a field that was not typed in: the field shows the value undone to."""
    page.activateWindow()
    page.add_node("limits")
    node = page.board.first("limits")
    page.set_prop(node.id, "--memory", "4g")
    page.show_properties(node.id)
    settled(page, wait)
    edit = field_of(page, "--memory")
    edit.setFocus()
    pump(qapp)
    assert edit.text() == "4g"
    page.undo.undo()
    settled(page, wait)
    assert field_of(page, "--memory").text() == ""


def test_a_text_taken_with_enter_or_typed_and_deleted_follows_undo(page, qapp, wait):
    """What was typed and then taken (Enter) is the model's: Undo of it shows in the field the keyboard is still in -
    not the text typed, to be taken again when the field is left. The same for a text typed and deleted again."""
    page.activateWindow()
    page.add_node("limits")
    node = page.board.first("limits")
    page.show_properties(node.id)
    settled(page, wait)
    edit = field_of(page, "--memory")
    edit.setFocus()
    pump(qapp)
    QTest.keyClicks(edit, "4g")
    QTest.keyClick(edit, Qt.Key.Key_Return)  # taken; the keyboard stays in the field
    settled(page, wait)
    assert page.board.first("limits").props["--memory"] == "4g"
    assert field_of(page, "--memory").hasFocus()
    page.undo.undo()  # (the toolbar's Undo: the keyboard stays where it is)
    settled(page, wait)
    assert "--memory" not in page.board.first("limits").props
    assert field_of(page, "--memory").text() == "", "the field shows the value undone to"
    field_of(page, "--memory").editingFinished.emit()  # leaving it takes nothing back
    assert "--memory" not in page.board.first("limits").props
    # typed and deleted again, then the model's value of this field changes: the field follows it
    edit = field_of(page, "--memory")
    QTest.keyClicks(edit, "x")
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    page.undo.redo()
    settled(page, wait)
    assert field_of(page, "--memory").text() == "4g"


def test_a_list_typed_a_moment_ago_is_taken_when_the_inspector_is_made_anew(page, qapp, wait):
    # (a list of one value per line: the approvals' hidden files; the network's lists are one field of kinds since
    # 2026-11-19, whose value typed survives the rows made anew: test_list_fields)
    page.activateWindow()
    page.add_node("approvals")
    node = page.board.first("approvals")
    page.show_properties(node.id)
    settled(page, wait)
    lines = page.option_fields["--hide"].findChildren(QPlainTextEdit)[0]
    lines.setFocus()
    QTest.keyClicks(lines, ".env")
    assert lines.timer.isActive(), "taken 600 ms after the typing stops"
    page.inspect_timer.start()  # a late answer before that
    settled(page, wait)
    assert page.board.first("approvals").props.get("--hide") == [".env"]
    assert page.option_fields["--hide"].findChildren(QPlainTextEdit)[0].toPlainText().strip() == ".env"


# -- R2-4, R2-5: a host that does not answer --

def test_the_note_of_a_host_whose_options_cannot_be_read_does_not_grow(lab, qapp, wait, monkeypatch):
    page = lab
    monkeypatch.setattr(BV, "HOST_RETRY", 0)  # (asked again at every check, as after a minute)
    assert page.host_choice.choose("lab")
    assert wait(lambda: page.host_note.text().count("could not be read") == 1, 20), page.host_note.text()
    for index in range(3):
        failed = dict(page.host_failed)
        page.set_prop(page.board.first("network").id, "--allow-host", [f"h{index}.example"])
        assert wait(lambda: page.host_failed != failed and not page.host_reading, 20), "read again, failed again"
        pump(qapp)
    assert page.host_note.text().count("could not be read") == 1, page.host_note.text()
    assert page.sai.on_gui == []


def test_a_host_that_does_not_answer_is_not_a_launcher_that_refuses(lab, qapp, wait):
    page = lab
    assert page.host_choice.choose("lab")
    assert wait(lambda: "Connection timed out" in page.verdict.text(), 20), page.verdict.text()
    assert "lab does not answer" in page.verdict.text() and "refuses" not in page.verdict.text()
    assert page.launcher_ok is False
    # Dry run says the same, not "The launcher refused these options"
    shown = len(widgets.SHOWN)
    page.dry_run()
    assert wait(lambda: len(widgets.SHOWN) > shown, 20)
    title, text, *_ = widgets.SHOWN[-1]
    assert "lab does not answer" in title and "Connection timed out" in text
    # ... and Start does not open a terminal that only fails: said, after one more try
    page.add_node("workspace", path="/srv/project")
    assert wait(lambda: "does not answer" in page.verdict.text() and not page.check_running
                and not page.check_timer.isActive(), 20)
    started = []
    page.terminal = lambda *a, **k: started.append(a)
    shown = len(widgets.SHOWN)
    page.start()
    assert wait(lambda: len(widgets.SHOWN) > shown, 20)
    assert widgets.SHOWN[-1][0] == "Not started" and "lab does not answer" in widgets.SHOWN[-1][1]
    assert started == [] and page.sai.on_gui == []


def test_the_launchers_own_refusal_on_a_host_is_still_a_refusal(lab, qapp, wait, monkeypatch):
    page = lab
    monkeypatch.setattr(page.sai, "host_run", lambda name, *args, **kw: (
        (2, "", "agent-sandbox: --offline contradicts --allow-pypi") if "--dry-run" in args else (1, "", "no")))
    assert page.host_choice.choose("lab")
    assert wait(lambda: "contradicts" in page.verdict.text(), 20), page.verdict.text()
    assert page.verdict.text().startswith("The launcher refuses:")


def test_host_dry_run_tells_no_answer_from_a_refusal(lab):
    sai = lab.sai
    with pytest.raises(backend.HostUnreachable) as caught:
        sai.host_dry_run("lab", "claude", [])
    assert "Connection timed out" in str(caught.value) and isinstance(caught.value, ValueError)


# -- R2-9: names that could not be read were asked for again at every rebuild of the inspector --

def test_names_that_could_not_be_read_are_not_asked_for_again_at_once(qapp, wait, monkeypatch):
    class Sai:
        def __init__(self):
            self.asked = 0

        def host_vpn_profiles(self, host):
            self.asked += 1
            raise ValueError("podman is not installed here")
    sai = Sai()
    answers = []
    for _ in range(5):  # the inspector made anew five times: a field of the VPN's name each time
        name_fields.names_of(sai, "vpn", "local", lambda names, problem: answers.append((names, problem)))
        assert wait(lambda: len(answers) == _ + 1, 10)
    assert sai.asked == 1, "one read; its failure is kept for a moment"
    assert all(names is None and "podman is not installed" in problem for names, problem in answers)
    # the list opened (the human asks): read again once the failure is older than a moment
    monkeypatch.setattr(name_fields.time, "monotonic", lambda real=time.monotonic: real() + name_fields.OPEN_SECONDS + 1)
    name_fields.names_of(sai, "vpn", "local", lambda names, problem: answers.append((names, problem)),
                         keep=name_fields.OPEN_SECONDS)
    assert wait(lambda: len(answers) == 6, 10) and sai.asked == 2
    # ... and a change of the names (one added on its page) reads them at once
    name_fields.forget("vpn", "local")
    name_fields.names_of(sai, "vpn", "local", lambda names, problem: answers.append((names, problem)))
    assert wait(lambda: len(answers) == 7, 10) and sai.asked == 3


# -- R2-10: Change folder left the board kept for the old folder sharing its Undo with the one shown --

def project(env, name, document=None):  # noqa: F811
    folder = env / name
    folder.mkdir(exist_ok=True)
    if document is not None:
        (folder / ".sandbox-ai.json").write_text(json.dumps(document))
    return str(folder)


def change_folder(board, folder, wait):
    assert board.change_folder(folder)
    assert wait(lambda: board.key_of(board.directory) == board.key_of(folder), 10)


def test_change_folder_then_the_tabs_never_leaves_a_board_without_its_undo(window, qapp, env, raised, wait):  # noqa: F811
    one, two = project(env, "one"), project(env, "two")
    board = window.board
    window.show()
    window.show_page("board")
    assert window.set_project(one)
    board.add_node("limits")
    assert window.set_project(two)
    assert window.set_project(one) and board.board.first("limits") is not None
    change_folder(board, two, wait)  # the board of one (limits) is on two now
    pump(qapp)
    assert board.board.first("limits") is not None and board.key_of(board.directory) == board.key_of(two)
    assert window.set_project(one)  # the tab of one: its board is read from its file again, nothing of the old stash
    pump(qapp)
    assert board.key_of(board.directory) == board.key_of(one)
    assert board.board.first("limits") is None, "the board went to two with its changes"
    assert window.set_project(two)
    pump(qapp)
    assert board.board.first("limits") is not None, "two shows the board that was moved there"
    assert board.open_project(two, quiet=True)  # read again from its folder
    assert window.set_project(one)
    assert window.set_project(two)
    pump(qapp)
    stacks = [session["stack"] for session in board.sessions.values()]
    assert all(BV.alive(stack) for stack in stacks), "no kept board with a deleted Undo"
    assert len({id(stack) for stack in stacks}) == len(stacks), "one Undo per kept board"
    board.add_node("limits")  # ... and the board works
    board.undo.undo()
    assert not raised, raised[0][-3000:]


def test_change_folder_onto_a_folder_with_unapplied_changes_asks_first(window, qapp, env, wait, monkeypatch):  # noqa: F811
    one, two = project(env, "one"), project(env, "two")
    board = window.board
    assert window.set_project(two)
    board.add_node("limits")  # two's own changes, not applied
    assert window.set_project(one)
    asked = []
    monkeypatch.setattr(BV, "ask_first", lambda parent, action, title, text, *a, **k: asked.append((action, text))
                        or False)
    assert board.change_folder(two) is False
    pump(qapp)
    assert asked and asked[0][0] == "board-discard" and "two" in asked[0][1]
    assert board.key_of(board.directory) == board.key_of(one), "kept: nothing moved"
    assert board.dirty_for(two), "two's changes are still there"


def test_a_kept_board_whose_undo_is_gone_is_read_again_instead_of_raising(window, qapp, env, raised):  # noqa: F811
    """Whatever left a kept board with a deleted Undo: its tab reads the project again - no exception at every
    switch to it."""
    one, two = project(env, "one"), project(env, "two")
    board = window.board
    assert window.set_project(one)
    assert window.set_project(two)
    stale = board.sessions[board.key_of(one)]["stack"]
    board.undo_group.removeStack(stale)
    stale.deleteLater()
    QApplication.sendPostedEvents(None, 52)  # QEvent.DeferredDelete
    assert window.set_project(one)
    pump(qapp)
    assert board.key_of(board.directory) == board.key_of(one) and BV.alive(board.undo)
    assert not raised, raised[0][-3000:]


def test_change_folders_late_answer_never_moves_another_projects_board(window, qapp, env, wait, monkeypatch):  # noqa: F811
    """The look whether the folder has a project file runs in the background (a disk that hangs: up to 30 s): its
    answer is for the board that asked, not for the one of the tab shown by then."""
    one, two, three = project(env, "one"), project(env, "two"), project(env, "three")
    board = window.board
    assert window.set_project(one)
    release = threading.Event()
    real = os.path.isfile
    monkeypatch.setattr(BV.os.path, "isfile", lambda path: (release.wait(20), real(path))[1]
                        if threading.current_thread() is not threading.main_thread() else real(path))
    assert board.change_folder(three)
    assert window.set_project(two)  # another tab meanwhile
    release.set()
    widgets.ThreadCall.wait_all(30)
    pump(qapp)
    assert board.key_of(board.directory) == board.key_of(two), "two's board stays on two"
    assert not board.dirty(), "nothing was changed on it"
    assert window.set_project(one)
    assert board.key_of(board.directory) == board.key_of(one)


# -- R2-11: a theme change queued for a Board that went ran on the deleted page --

def test_a_restyle_queued_for_a_board_that_went_does_not_run(qapp, tmp_path, monkeypatch, raised):
    from PySide6.QtCore import QEvent
    isolated(tmp_path, monkeypatch)
    view = make_page(backend.SandboxAI(), qapp)
    qapp.sendEvent(view, QEvent(QEvent.Type.PaletteChange))  # a new theme: its restyle is queued
    assert view.restyle_queued
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for _ in range(5):
        qapp.processEvents()
    assert not raised, raised[0][-3000:]


# -- R2-6: the board of no project folder (a new installation's first board, another host's board) was not kept --

def restart(window, monkeypatch):
    """Close WINDOW as the app does (its state is written) and start a new one."""
    window.shutdown()
    return make_window(monkeypatch)


def crashed(window, monkeypatch, wait):
    """WINDOW's process dies (nothing of its shutdown runs; what the state's timer wrote is all there is), and the app
    is started again."""
    assert wait(lambda: not window.state.timer.isActive(), 10)  # (the usual delay after the last change)
    assert window.state.flush()  # ... its write on the disk (the writer thread's, awaited here)
    window.state.flush = lambda *a, **k: True  # the dying process writes nothing more
    window.commit_edits = lambda: None
    return make_window(monkeypatch)


def with_lab(monkeypatch):
    """Every backend made from now on knows the host "lab" (which does not answer)."""
    monkeypatch.setattr(backend.SandboxAI, "hosts_supported", lambda self: True)
    monkeypatch.setattr(backend.SandboxAI, "host_entries", lambda self: [dict(h) for h in HOSTS])
    monkeypatch.setattr(backend.SandboxAI, "host_run", lambda self, name, *args, **kw: (255, "", UNREACHABLE))


@pytest.mark.parametrize("how", ["closed", "crashed"])
def test_a_board_without_a_project_folder_comes_back_with_its_undo(qapp, env, monkeypatch, wait, raised, how):  # noqa: F811
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    first = make_window(monkeypatch)
    first.show_page("board")
    board = first.board
    assert board.directory is None and first.project is None, "a new installation: no project yet"
    board.add_node("mount", source="/data", dest="/mnt/data", mode="ro")
    board.add_node("limits")
    board.set_prop(board.board.first("limits").id, "--memory", "4g")
    again = restart(first, monkeypatch) if how == "closed" else crashed(first, monkeypatch, wait)
    try:
        again.show_page("board")
        board = again.board
        assert board.directory is None
        assert board.board.first("limits") is not None and board.board.first("limits").props == {"--memory": "4g"}
        assert [n.props["source"] for n in board.board.of_kind("mount")] == ["/data"]
        assert board.dirty() and board.undo.canUndo()
        board.undo.undo()
        assert "--memory" not in board.board.first("limits").props, "its steps of Undo came back with it"
        board.undo.setIndex(0)
        assert board.board.first("limits") is None and board.board.of_kind("mount") == []
        assert not raised, raised[0][-3000:]
    finally:
        close(again)
        if how == "crashed":
            close(first)
    third = make_window(monkeypatch)
    try:  # undone to nothing and closed: no draft is kept any more
        assert third.board.board.first("limits") is None and not third.board.dirty()
    finally:
        close(third)


def test_a_project_opened_after_the_restart_is_shown_not_the_board_that_came_back(qapp, env, monkeypatch, wait):  # noqa: F811
    """The board of no folder that came back is shown again after the window's own tab only: a project the human
    opens later is shown and stays shown."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    first = make_window(monkeypatch)
    first.show_page("board")
    first.board.add_node("limits")
    again = restart(first, monkeypatch)
    try:
        again.show_page("board")
        board = again.board
        pump(qapp, 10)
        assert board.directory is None and board.board.first("limits") is not None
        folder = project(env, "later")
        assert again.set_project(folder)
        pump(qapp, 10)
        assert board.key_of(board.directory) == board.key_of(folder), "the project opened stays shown"
        assert board.board.first("limits") is None and board.unnamed_kept()
    finally:
        close(again)


def test_a_board_of_another_hosts_folder_comes_back_shown_as_it_was_left(qapp, env, monkeypatch, wait, raised):  # noqa: F811
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    with_lab(monkeypatch)
    folder = project(env, "here")
    first = make_window(monkeypatch)
    first.show_page("board")
    assert first.set_project(folder)
    board = first.board
    board.host_choice.reload()
    assert board.host_choice.choose("lab")
    assert board.clone_on_host("lab", "/srv/project")  # the board on lab's folder: no project of this machine
    board.add_node("limits")
    assert board.directory is None and board.remote_project == ("lab", "/srv/project")
    again = restart(first, monkeypatch)
    try:
        again.show_page("board")
        board = again.board
        assert wait(lambda: board.directory is None and board.remote_project == ("lab", "/srv/project"), 10), \
            (board.directory, board.remote_project)
        assert board.host() == "lab" and board.board.first("limits") is not None and board.dirty()
        assert [n.props["path"] for n in board.board.of_kind("workspace")] == ["/srv/project"]
        assert again.project == os.path.realpath(folder), "the project's tab is still there"
        assert again.set_project(folder) and board.key_of(board.directory) == board.key_of(folder)
        assert not raised, raised[0][-3000:]
    finally:
        close(again)


def test_a_board_kept_without_a_folder_can_be_shown_again(window, qapp, env, monkeypatch):  # noqa: F811
    """With a project's tab chosen the board of no folder was kept and never shown again: "Keep them" (asked when
    another one would replace it) kept what nobody could reach. The canvas menu shows it, and so does "Keep them"."""
    folder = project(env, "here")
    board = window.board
    board.add_node("limits")  # the board of no folder, changed
    assert window.set_project(folder)
    assert board.key_of(board.directory) == board.key_of(folder) and board.board.first("limits") is None
    menu = board.canvas_menu(QPointF(-5000, -5000))
    action = next(a for a in menu.actions() if a.text() == "Show the board without a project folder")
    assert action.isVisible() and action.isEnabled()
    action.trigger()
    assert board.directory is None and board.board.first("limits") is not None and board.dirty()
    menu = board.canvas_menu(QPointF(-5000, -5000))
    assert "Show the board without a project folder" not in [a.text() for a in menu.actions()], "it is shown"
    # ... and "Keep them" shows what it keeps
    assert window.set_project(folder)
    monkeypatch.setattr(BV, "ask_first", lambda *a, **k: False)
    assert board.may_discard_unnamed() is False
    assert board.directory is None and board.board.first("limits") is not None


def test_the_board_without_a_folder_can_be_discarded_and_stays_so(qapp, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    first = make_window(monkeypatch)
    first.show_page("board")
    board = first.board
    board.add_node("limits")
    menu = board.canvas_menu(QPointF(-5000, -5000))
    discard = next(a for a in menu.actions() if a.text() == "Discard the changes not applied")
    assert discard.isEnabled()
    discard.trigger()  # (asked first; the tests answer yes)
    assert board.board.first("limits") is None and not board.dirty() and board.directory is None
    again = restart(first, monkeypatch)
    try:
        assert again.board.board.first("limits") is None and not again.board.dirty()
    finally:
        close(again)


# -- R2-12: a board made from a preset (or an import) was no change: gone with the next start of the app --

def test_a_presets_board_is_a_change_not_applied_and_survives_a_restart(qapp, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    folder = project(env, "locked-down")
    first = make_window(monkeypatch)
    first.show_page("board")
    assert first.set_project(folder)
    first.board.new_from_preset("locked")
    assert "--offline" in first.board.preview.toPlainText()
    assert first.board.dirty(), "not written to the project's file: a change not applied"
    again = restart(first, monkeypatch)
    try:
        again.show_page("board")
        board = again.board
        assert board.key_of(board.directory) == board.key_of(folder)
        assert "--offline" in board.preview.toPlainText(), "the locked board, not the default one"
        assert board.dirty()
        board.undo.undo()
        assert "--offline" not in board.preview.toPlainText(), "Undo goes back to the project's file"
    finally:
        close(again)


# -- R2-8: the keyboard alone could not choose a part: no way to its properties, its menu or its place --

def chosen(board):
    return [board.board.node(i.node_id).kind for i in board.scene.selectedItems() if isinstance(i, BV.NodeItem)]


def test_the_arrow_keys_choose_parts_and_enter_opens_their_properties(window, qapp, wait, raised):  # noqa: F811
    window.resize(1280, 800)
    window.show()
    window.show_page("board")
    window.activateWindow()
    board = window.board
    board.arrange()
    pump(qapp)
    view = board.view
    view.setFocus()
    assert chosen(board) == []
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert chosen(board) == ["agent"], "nothing chosen: the agent first"
    QTest.keyClick(view, Qt.Key.Key_Right)
    assert chosen(board) == ["proxy"]
    QTest.keyClick(view, Qt.Key.Key_Right)
    assert chosen(board) == ["network"]
    QTest.keyClick(view, Qt.Key.Key_Right)
    assert chosen(board) == ["network"], "none further that way: it stays"
    QTest.keyClick(view, Qt.Key.Key_Left)
    assert chosen(board) == ["proxy"]
    # Enter: its properties, the keyboard in them
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert wait(lambda: board.inspector_holder.isAncestorOf(QApplication.focusWidget() or view), 10), \
        QApplication.focusWidget()
    assert "proxy" in board.inspector_title.text().lower()
    assert not window.inspector_fold.is_folded() and window.docks["inspector"].isVisible()
    # OK: the panel folds, the keyboard is back on the canvas with the part still chosen
    board.edit_box.buttons()[0].click()
    pump(qapp)
    assert QApplication.focusWidget() is view and chosen(board) == ["proxy"]
    # Ctrl+A chooses every part
    QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert sorted(chosen(board)) == ["agent", "network", "proxy"]
    assert not raised, raised[0][-3000:]


def test_enter_reaches_the_properties_when_they_float_and_ok_comes_back(window, qapp, wait, monkeypatch):  # noqa: F811
    """The properties in a window of their own (floated): Enter on the board makes that window the active one with
    the keyboard in it, OK the app's window again with the keyboard on the board."""
    window.resize(1280, 800)
    window.show()
    window.show_page("board")
    window.activateWindow()
    board = window.board
    dock = window.docks["inspector"]
    board.show_properties(board.board.first("agent").id)
    pump(qapp, 5)
    dock.setFloating(True)
    pump(qapp, 5)
    assert dock.isFloating() and dock.isVisible()
    activated = []
    monkeypatch.setattr(type(dock), "activateWindow", lambda self: activated.append("panel"), raising=False)
    monkeypatch.setattr(type(window), "activateWindow", lambda self: activated.append("window"), raising=False)
    board.view.setFocus()
    QTest.keyClick(board.view, Qt.Key.Key_Right)  # the proxy
    QTest.keyClick(board.view, Qt.Key.Key_Return)
    assert wait(lambda: "panel" in activated, 10), activated
    assert "proxy" in board.inspector_title.text().lower()
    field = next(w for w in board.inspector_holder.findChildren(QtWidgets.QCheckBox) if w.isVisible())
    field.setFocus()  # (offscreen no window is activated for real: the keyboard put where it would be)
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: field))
    board.edit_box.buttons()[0].click()
    assert activated[-1] == "window", activated


def test_every_part_of_a_large_board_is_reached_with_the_arrow_keys(page, qapp):
    for kind, known in B.KINDS.items():
        if page.board.first(kind) is None or not known.single:
            page.add_node(kind)
    page.add_node("mount")
    page.add_node("mcp")
    page.arrange()
    pump(qapp)
    seen, frontier = set(), [page.board.first("agent").id]
    while frontier:  # every part reachable from the agent by some sequence of arrow keys
        ident = frontier.pop()
        if ident in seen:
            continue
        seen.add(ident)
        for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            page.scene.clearSelection()
            page.items[ident].setSelected(True)
            node = page.select_towards(*step)
            if node is not None and node.id not in seen:
                frontier.append(node.id)
    assert seen == set(page.items), [page.board.node(i).kind for i in set(page.items) - seen]


def test_alt_and_an_arrow_key_move_the_chosen_parts_as_one_step_of_undo(page, qapp):
    page.activateWindow()
    view = page.view
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Down)
    agent = page.board.first("agent")
    x, y, steps = agent.x, agent.y, page.undo.count()
    QTest.keyClick(view, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    agent = page.board.first("agent")
    assert (agent.x, agent.y) == (x, y + page.grid()) and page.undo.count() == steps + 1
    QTest.keyClick(view, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
    assert page.board.first("agent").x == x + page.grid()
    page.undo.undo()
    page.undo.undo()
    agent = page.board.first("agent")
    assert (agent.x, agent.y) == (x, y)
    assert page.items[agent.id].pos().x() == x and page.items[agent.id].pos().y() == y


def submenu(menu, title):
    return next((a.menu() for a in menu.actions() if a.menu() is not None and a.text() == title), None)


def test_a_parts_menu_connects_and_disconnects_it_without_the_mouse(page, qapp):
    page.add_node("mount", source="/data", dest="/mnt/data", mode="ro")
    mount, agent = page.board.of_kind("mount")[0], page.board.first("agent")
    assert (mount.id, agent.id) in page.board.edges
    point = page.items[mount.id].sceneBoundingRect().center()
    menu = page.canvas_menu(point)
    assert submenu(menu, "Connect with") is None, "connected with all it can be"
    disconnect = submenu(menu, "Disconnect from")
    assert [a.text() for a in disconnect.actions()] == ["Agent: claude"]
    disconnect.actions()[0].trigger()
    assert (mount.id, agent.id) not in page.board.edges and not page.board.connected(page.board.of_kind("mount")[0])
    menu = page.canvas_menu(point)
    assert submenu(menu, "Disconnect from") is None
    connect = submenu(menu, "Connect with")
    assert [a.text() for a in connect.actions()] == ["Agent: claude"]
    connect.actions()[0].trigger()
    assert (mount.id, agent.id) in page.board.edges
    page.undo.undo()
    assert (mount.id, agent.id) not in page.board.edges, "one step of Undo each"
    # the agent's menu names every part it can be connected with, from its side
    menu = page.canvas_menu(page.items[agent.id].sceneBoundingRect().center())
    assert "Extra folder: /data" in [a.text() for a in submenu(menu, "Connect with").actions()]
    assert "Connection proxy" in [a.text() for a in submenu(menu, "Disconnect from").actions()]


# -- R2-14: the agent's guide on a board with a model server kept the model answered and dropped the other answers --

def test_the_agents_guide_keeps_every_answer_when_it_also_changes_the_servers_model(page, qapp, raised):
    from sandbox_ai_app import guide, guide_view
    page.llm_servers = [{"name": "srv", "kind": "vllm", "url": "http://10.0.0.5:8000", "models": ["m1", "m2"]}]
    page.choose_llm_server({"name": "srv", "model": "", "only": False})
    assert page.board.first("llm").props["--llm"] == "srv"
    agent = page.board.first("agent")
    answers = {question.key: "" for question in guide.questions("agent")}
    answers.update({"agent": "claude", "model": "srv:m2", "--dangerously-skip-permissions": True})
    steps = page.undo.count()
    guide_view.apply(page, agent.id, answers)
    assert page.board.first("llm").props["--llm"] == "srv:m2", "the server's model answered"
    assert page.board.first("agent").props.get("--dangerously-skip-permissions") is True, \
        "... and the other answers of the same guide"
    assert "--dangerously-skip-permissions" in page.preview.toPlainText()
    assert page.undo.count() == steps + 2
    page.undo.undo()
    assert "--dangerously-skip-permissions" not in page.board.first("agent").props
    assert page.board.first("llm").props["--llm"] == "srv:m2"
    assert not raised, raised[0][-3000:]


# -- R2-15: the model and MCP servers of a host that did not answer read as "none" until another host was chosen --

def test_a_hosts_servers_that_could_not_be_read_are_said_and_asked_for_again(lab, qapp, wait, monkeypatch):
    page, sai = lab, lab.sai
    assert page.host_choice.choose("lab")
    agent = page.board.first("agent")
    page.show_properties(agent.id)
    settled(page, wait)

    def notes():
        return [w.text() for w in page.inspector_holder.findChildren(QtWidgets.QLabel)
                if "model servers" in w.text()]
    assert any("lab could not be read" in text and "Connection timed out" in text for text in notes()), notes()
    asked = len([ran for ran in sai.ran if ran[1:3] == ("llm", "list")])
    page.inspect_timer.start()  # the part looked at again a moment later: not asked again at once
    settled(page, wait)
    assert len([ran for ran in sai.ran if ran[1:3] == ("llm", "list")]) == asked
    # ... but after a while, and then the host answers
    servers = [{"name": "gpu", "kind": "vllm", "url": "http://10.0.0.5:8000"}]
    monkeypatch.setattr(sai, "host_run", lambda name, *args, **kw: (
        (0, json.dumps(servers), "") if args[:2] == ("llm", "list") else (255, "", UNREACHABLE)))
    monkeypatch.setattr(BV, "HOST_RETRY", 0)
    page.inspect_timer.start()
    settled(page, wait)
    assert wait(lambda: page.llm_servers and page.llm_servers[0].get("name") == "gpu", 20), page.llm_servers
    settled(page, wait)
    assert not any("could not be read" in text for text in notes()), notes()
    assert page.llm_combo.findData("gpu") >= 0, "offered as the agent's model server"
    # the MCP servers of the host: the same
    page.add_node("mcp")
    settled(page, wait)
    assert page.mcp_known.get("lab") == [] and "lab" in page.mcp_failed
    listed = [{"name": "files", "status": "ok", "serves": [], "image": "x"}]
    monkeypatch.setattr(sai, "host_run", lambda name, *args, **kw: (
        (0, json.dumps(listed), "") if args[:2] == ("mcp", "list") else (255, "", UNREACHABLE)))
    page.inspect_timer.start()
    assert wait(lambda: page.mcp_known.get("lab") == ["files"], 20), page.mcp_known
    assert sai.on_gui == []


# -- R2-16: an imported board's Bridge and Team parts were taken from the bundle's state as they came --

def imported(tmp_path, nodes, edges=()):
    """A board file as `sandbox-ai board import` hands it on, its state (the bundle's) holding NODES."""
    state = {"nodes": [{"id": 1, "kind": "agent", "props": {"agent": "claude"}, "x": 300.0, "y": 40.0}, *nodes],
             "edges": [list(edge) for edge in edges], "extra": [], "document_extra": {}}
    return {"schema": "sandbox-ai-board", "version": 1, "name": "elsewhere", "agent": "claude",
            "options": ["--workspace", str(tmp_path / "ws"), "--allow-pypi"], "agent_args": [], "state": state}


def test_an_imported_boards_own_parts_come_with_their_places_and_connections(page, tmp_path):
    (tmp_path / "ws").mkdir()
    document = imported(tmp_path, [
        {"id": 7, "kind": "bridge", "props": {"peer": "other", "direction": "out", "message": True},
         "x": 560.0, "y": 300.0}], edges=[(1, 7)])
    assert page.show_imported(document, "local")
    bridge = page.board.of_kind("bridge")[0]
    assert bridge.props["peer"] == "other" and (bridge.x, bridge.y) == (560.0, 300.0)
    assert page.board.connected(bridge)
    assert "--allow-pypi" in page.preview.toPlainText()
    assert page.dirty(), "not applied: no project file says this board"


FOREIGN_STATES = [
    [{"id": 7, "kind": "team", "props": {"name": "t", "lead": "claude", "members": ["claude", "codex"]},
      "x": "12", "y": None}],  # a place that is no number
    [{"id": 7, "kind": "bridge", "props": {"connect": True, "peer": ["x"]}, "x": 0, "y": 0}],  # Board.add's argument
    [{"id": 7, "kind": "bridge", "props": {"kind": "agent", "direction": 4}, "x": 0, "y": 0}],
    [{"id": 7, "kind": "bridge", "props": {"peer": {"a": 1}, "read": 5, "limits": "x"}, "x": 0, "y": 0}],
    [{"id": 7, "kind": "team", "props": {"name": 5, "lead": [], "members": "abc"}, "x": 1e300, "y": float("inf")}],
    [{"id": [7], "kind": "bridge", "props": {"peer": "other"}, "x": 0, "y": 0}],  # an id that is a list
    [{"id": 7, "kind": "bridge", "props": ["peer"], "x": 0, "y": 0}],  # settings that are no object
    [{"id": 7, "kind": "nonsense", "props": {}, "x": 0, "y": 0}],
]


@pytest.mark.parametrize("index", range(len(FOREIGN_STATES)))
def test_an_imported_board_with_a_state_made_elsewhere_is_shown_without_what_cannot_be_used(page, qapp, raised, wait,
                                                                                         tmp_path, index):
    (tmp_path / "ws").mkdir(exist_ok=True)
    document = json.loads(json.dumps(imported(tmp_path, FOREIGN_STATES[index], edges=[(1, 7)]),
                                     default=lambda value: None).replace("Infinity", "1e999"))
    page.show_imported(document, "local")
    pump(qapp)
    for item in list(page.items.values()):  # every part shown can be looked at
        page.scene.clearSelection()
        item.setSelected(True)
        assert wait(lambda: not page.inspect_timer.isActive(), 10)
        pump(qapp, 2)
    widgets.ThreadCall.wait_all(30)
    pump(qapp)
    assert not raised, raised[0][-3000:]
    assert set(page.items) == {node.id for node in page.board.nodes}
    assert "--allow-pypi" in page.preview.toPlainText(), "the board's start is shown: it came from the launcher's options"
    assert [node.kind for node in page.board.nodes if node.kind in ("bridge", "team")] == [], "left out"


# -- R2-22: "New from preset…" held the window: every card of the gallery measured seven times --

def test_a_gallery_card_is_measured_once_until_something_in_it_changes(page, qapp):
    """The gallery's flow layouts ask every card for its size again and again while the gallery is made (2226 times
    for 106 presets, a third of the time the click held the window): a card measures itself once, and anew when a
    text, the font or the style of something in it changes."""
    from sandbox_ai_app import preset_gallery, presets as PR
    preset = next(p for p in PR.catalogue(page.sai, "local").presets if p.description)
    board = PR.build(page.sai, preset, None, agent="claude")
    card = preset_gallery.PresetCard(preset, board, 80)
    holder = QtWidgets.QWidget()
    preset_gallery.FlowLayout(holder, spacing=10).addWidget(card)
    holder.resize(800, 600)
    holder.show()
    pump(qapp)
    measured = []
    layout = card.layout()
    real = type(card).sizeHint
    first = card.sizeHint()
    assert card.measured is not None and first.height() >= layout.totalHeightForWidth(card.minimumWidth())
    kept = card.measured
    for _ in range(20):
        measured.append(card.sizeHint())
    assert card.measured is kept and all(size == first for size in measured), "not measured again"
    assert real(card) == first
    # a longer description: Qt asks the card for a new layout, and it measures itself anew - taller
    description = next(label for label in card.findChildren(QtWidgets.QLabel) if label.wordWrap()
                       and label.text() == preset.text(preset.description))
    description.setText(" ".join(["a longer description of this preset"] * 30))
    pump(qapp)
    assert card.sizeHint().height() > first.height(), "the card grows with its text"
    assert card.sizeHint().height() >= layout.totalHeightForWidth(card.minimumWidth()), "nothing of it is cut"
    holder.close()
    holder.deleteLater()
