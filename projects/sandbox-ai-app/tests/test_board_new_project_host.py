"""The Board's New project… on a host of `sandbox-ai hosts` (todos.txt board, item 10 - the user 2026-11-25: "Wenn wir im
Board 'Neues Projekt…' machen, müssen auch neue Projekte auf verbundenen Remote-Hosts supported werden").

New project… asks where the project is while there is a host: this machine (the wizard, as before) or a host. On a
host its sandbox-ai must read project files (`wizard trust --show`; an older one: refused with the update to do,
nothing made), the folder is chosen from that host's folders or typed, one not there yet is created there after a
yes (over SFTP: one folder, never its parents), and it is opened as that host's project - the host chosen on the
Board, nothing of this machine opened or made. Against the fake SFTP server as the host "lab"
(test_remote_chooser's fixture) and a stand-in for that host's sandbox-ai."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, board_view, widgets  # noqa: E402
from test_remote_chooser import driven, host, rows, select  # noqa: E402,F401
from test_remote_paths import board, sai, shown_project, workspaces  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

WHERE = "New project"
CREATE = "New project on lab"


@pytest.fixture
def lab(sai, monkeypatch):
    """lab's sandbox-ai: it reads project files (wizard trust --show) and has none in a new folder."""
    monkeypatch.setattr(sai, "host_command_options", lambda name, *command, fresh=False:
                        {"--show", "--json"} if (name, command) == ("lab", ("wizard", "trust")) else
                        pytest.fail(f"options of {command} asked on {name}"))
    sai.shown = shown_project(sai, monkeypatch, error="no such file")
    return sai


def where(monkeypatch, host_name):
    monkeypatch.setattr(widgets, "AUTO_CHOICE", host_name)


def questions(title):
    return [text for shown_title, text, _details in widgets.SHOWN if shown_title == title]


def test_without_a_host_new_project_is_the_wizard_at_once(board, sai, monkeypatch):
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"}])
    opened = []
    monkeypatch.setattr(board, "open_wizard", lambda ask=True: opened.append(ask) or "wizard")
    assert board.new_project() == "wizard" and opened == [True]
    assert questions(WHERE) == [], "nothing asked: there is nowhere else"


def test_this_machine_chosen_is_the_wizard_on_this_machine(board, lab, monkeypatch):
    where(monkeypatch, "local")
    opened = []
    monkeypatch.setattr(board, "open_wizard", lambda ask=True: opened.append((ask, board.host())) or "wizard")
    board.new_project_button.click()
    assert questions(WHERE) and "On a host" in questions(WHERE)[0]
    assert opened == [(True, "local")], "the wizard, with this machine chosen on the Board"


def test_a_folder_of_the_host_becomes_that_hosts_project(board, lab, host, monkeypatch, wait):
    where(monkeypatch, "lab")
    seen = driven(monkeypatch, wait, lambda dialog: select(dialog, "proj/") and dialog.chosen())
    assert board.new_project()
    assert wait(lambda: board.remote_project == ("lab", str(host / "proj")), 20), widgets.SHOWN
    assert seen and seen[0].host == "lab" and seen[0].windowTitle() == "Folder of the new project on lab"
    assert workspaces(board) == [str(host / "proj")] and board.host() == "lab"
    assert lab.shown == [("lab", str(host / "proj" / ".sandbox-ai.json"))], "read by lab's sandbox-ai"
    assert questions(CREATE) == [], "it is there: nothing to create"
    assert board.directory is None and board.opened_seen == [], "no project of this machine"


def test_a_folder_not_there_yet_is_created_on_the_host_after_a_yes(board, lab, host, monkeypatch, wait):
    where(monkeypatch, "lab")
    new = host / "fresh"

    def answer(dialog):
        dialog.path.setText(str(new))  # typed: not on lab yet
        dialog.list.clearSelection()
        return dialog.chosen()
    driven(monkeypatch, wait, answer)
    assert board.new_project()
    assert wait(lambda: board.remote_project == ("lab", str(new)), 20), widgets.SHOWN
    assert new.is_dir(), "made on lab (the fake host's file system)"
    assert questions(CREATE) == [f"{new} is not on lab yet. Create the folder there?"]
    assert workspaces(board) == [str(new)] and board.directory is None and board.opened_seen == []


def test_no_folder_is_made_when_the_answer_is_no(board, lab, host, monkeypatch, wait):
    where(monkeypatch, "lab")
    new = host / "not-wanted"
    driven(monkeypatch, wait, lambda dialog: dialog.path.setText(str(new)) or dialog.chosen())
    asked = []
    monkeypatch.setattr(board_view, "confirm", lambda *a, **k: asked.append(a[2]) or False)
    before = board.remote_project
    assert board.new_project()
    assert wait(lambda: asked, 20), widgets.SHOWN
    assert not new.exists() and board.remote_project == before


def test_a_folder_whose_folder_above_is_missing_is_refused(board, lab, host, monkeypatch, wait):
    where(monkeypatch, "lab")
    deep = host / "nowhere" / "deeper"
    driven(monkeypatch, wait, lambda dialog: dialog.path.setText(str(deep)) or dialog.chosen())
    assert board.new_project()
    assert wait(lambda: questions(CREATE), 20)
    assert "is not on lab" in questions(CREATE)[0] and "New folder…" in questions(CREATE)[0]
    assert not (host / "nowhere").exists(), "never its parents"
    assert board.remote_project != ("lab", str(deep))


def test_a_file_is_no_project_folder(board, lab, host, monkeypatch, wait):
    where(monkeypatch, "lab")
    driven(monkeypatch, wait, lambda dialog: dialog.path.setText(str(host / "notes.txt")) or dialog.chosen())
    assert board.new_project()
    assert wait(lambda: questions(CREATE), 20)
    assert "is no folder" in questions(CREATE)[0]
    assert board.remote_project != ("lab", str(host / "notes.txt"))


def test_a_host_too_old_is_refused_with_the_update_before_anything_is_chosen(board, sai, host, monkeypatch, wait):
    where(monkeypatch, "lab")
    monkeypatch.setattr(sai, "host_command_options", lambda name, *command, fresh=False: {"--json"})
    seen = driven(monkeypatch, wait, lambda dialog: pytest.fail("a folder chosen on a host that is too old"))
    assert board.new_project()
    assert wait(lambda: questions(CREATE), 20)
    assert "too old" in questions(CREATE)[0] and "Update the host" in questions(CREATE)[0]
    assert seen == [] and board.remote_project is None
    assert "No new project on lab" in board.notice.text()
