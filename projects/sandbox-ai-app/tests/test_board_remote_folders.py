"""The Board's folder and file actions with another host chosen at its bottom (the user's report 2026-11-18: "Change
folder…" still opened this machine's file dialog and resolved the folder here). Each chooses from that host's folders
(remote_chooser), keeps the path as it is there (never resolved or looked at here), reads that host's project file
there (`wizard trust --show`, in the background) and writes nothing on this machine for it. End to end against the
fake SFTP server as the host "lab" (test_remote_chooser's fixture)."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPointF  # noqa: E402

from sandbox_ai_app import backend, presets as PR, remote_chooser as RC, state, widgets  # noqa: E402
from test_remote_chooser import driven, failing, host  # noqa: E402,F401
from test_remote_paths import board, sai, shown_project, workspaces  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def on_host(board, host, monkeypatch):
    """The board of lab's folder proj (as Open project folder… left it), with a setting of its own; this machine's
    file dialog and this machine's project files are never asked."""
    monkeypatch.setattr(state, "run_dialog", lambda dialog: pytest.fail("this machine's file dialog for a host"))
    monkeypatch.setattr(board.sai, "write_project", lambda *a, **k: pytest.fail("a project file written here"))
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    agent = board.board.first("agent")
    agent.props["--env"] = ["KEEP=1"]
    board.board.add("workspace", path=str(host / "proj"))
    board.rebuild()
    board.remote_project = ("lab", str(host / "proj"))
    board.show_folder()
    return board


def change_button(board):
    return next(b for b in board.findChildren(QtWidgets.QPushButton) if b.text() == "Change folder…")


def test_change_folder_on_a_host_chooses_there_and_keeps_the_board(on_host, host, monkeypatch, wait):
    board = on_host
    asked = shown_project(board.sai, monkeypatch, {"agent": "codex", "options": []}, trusted=True)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # "Keep this board"
    starts = []

    def answer(dialog):
        starts.append(dialog.folder)
        assert dialog.host == "lab"
        dialog.path.setText(str(host / "other"))  # (typed: the list shows the board's folder, where it started)
        return dialog.chosen()
    driven(monkeypatch, wait, answer)
    change_button(board).click()
    assert wait(lambda: board.remote_project == ("lab", str(host / "other")), 20), board.remote_project
    assert starts == [str(host / "proj")], "it starts in the board's folder there"
    assert asked == [("lab", str(host / "other" / ".sandbox-ai.json"))], "its project file looked for on lab"
    assert any("project file of its own" in text for _title, text, _ in widgets.SHOWN), "asked: load it or keep"
    assert workspaces(board) == [str(host / "other")]
    assert board.board.first("agent").props.get("agent") == "claude" and \
        board.board.first("agent").props.get("--env") == ["KEEP=1"], "the board's settings stay"
    assert board.directory is None and board.opened_seen == [], "no project of this machine"
    assert "lab" in board.folder.text() and str(host / "other") in board.folder.toolTip()
    board.undo.undo()  # one step
    assert workspaces(board) == [str(host / "proj")] and board.remote_project == ("lab", str(host / "proj"))
    board.undo.redo()
    assert workspaces(board) == [str(host / "other")] and board.remote_project == ("lab", str(host / "other"))


def test_change_folder_on_a_host_loads_its_project_file_when_asked(on_host, host, monkeypatch, wait):
    board = on_host
    shown_project(board.sai, monkeypatch, {"agent": "codex", "options": []}, trusted=True)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # "Load it"
    assert board.change_folder(str(host / "other"))
    assert wait(lambda: board.board.first("agent").props.get("agent") == "codex", 20)
    assert board.remote_project == ("lab", str(host / "other")) and workspaces(board) == [str(host / "other")]
    assert board.opened_seen == []


def test_a_hosts_folder_without_a_project_file_is_taken_without_a_question(on_host, host, monkeypatch, wait):
    board = on_host
    shown_project(board.sai, monkeypatch, error="no such file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    assert board.change_folder(str(host / "other"))
    assert wait(lambda: board.remote_project == ("lab", str(host / "other")), 20)
    assert not any("project file of its own" in text for _title, text, _ in widgets.SHOWN)
    assert board.board.first("agent").props.get("--env") == ["KEEP=1"]


def test_an_unreachable_host_is_said_and_nothing_changes(on_host, host, monkeypatch, wait):
    board = on_host
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)

    def unreachable(name, path):
        raise RuntimeError("ssh: connect to host lab.example port 22: Connection refused")
    monkeypatch.setattr(board.sai, "host_project_show", unreachable)
    assert board.change_folder(str(host / "other"))
    assert wait(lambda: any("Connection refused" in text for _title, text, _ in widgets.SHOWN), 20)
    assert workspaces(board) == [str(host / "proj")] and board.remote_project == ("lab", str(host / "proj"))
    assert board.undo.count() == 0


def test_a_typed_path_of_a_host_is_kept_as_it_is(on_host, host, tmp_path, monkeypatch, wait):
    board = on_host
    monkeypatch.setattr(RC, "SSH", failing("ssh: connect to host lab.example port 22: Connection refused", "",
                                           tmp_path))
    asked = shown_project(board.sai, monkeypatch, error="no such file")

    def answer(dialog):
        assert wait(lambda: "type the path" in dialog.status.text(), 20)
        dialog.path.setText("/srv/../srv/app")
        return dialog.chosen()
    driven(monkeypatch, wait, answer, listed=False)
    assert board.change_folder()
    assert wait(lambda: workspaces(board) == ["/srv/../srv/app"], 20), "as typed: never resolved here"
    assert asked == [("lab", "/srv/../srv/app/.sandbox-ai.json")]


def test_save_on_a_host_never_writes_a_project_file_here(on_host, monkeypatch):
    """Save (Ctrl+S) went to Apply, which wrote the host's folder's .sandbox-ai.json on this machine."""
    board = on_host
    saved = []
    monkeypatch.setattr(board, "save_as_preset", lambda choices=None: saved.append(True) or None)
    board.save_board()
    assert saved == [True], "a board of a host is kept as a preset of yours"
    assert board.apply() is False
    assert "on lab the board starts as it is" in widgets.SHOWN[-1][1]


def test_a_project_of_this_computer_dropped_on_a_board_of_a_host_is_refused(on_host, tmp_path):
    board = on_host
    project = tmp_path / "here"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text("{}")
    board.project_request = lambda directory: pytest.fail("this computer's project opened for a board of a host")
    board.choose_drop = lambda choices, point=None: "project"
    assert board.drop_paths([str(project)], QPointF(10, 10)) is None
    assert "this computer's, but the board starts on lab" in widgets.SHOWN[-1][1]


def test_a_preset_of_a_hosts_board_says_whose_paths_it_holds(on_host, host):
    board = on_host
    preset = PR.from_board(board.sai, board.board, "On lab", keep=True, host=board.host())
    note = " ".join(preset.notes)
    assert "paths of lab" in note and "this computer" not in note
    here = PR.from_board(board.sai, board.board, "Here", keep=True)
    assert "paths of this computer" in " ".join(here.notes)
