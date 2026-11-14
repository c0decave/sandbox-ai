"""Change folder… on the Board (user wish from live use, 2026-11-14): the board keeps every part and setting and only
switches its project folder - its main workspace, where its project file and draft belong - as one undo step; the
folder checks run again; nothing is written there before Apply. A folder with its own .sandbox-ai.json: asked
whether to load that file (yes: loaded with the usual trust check; no: the board's settings stay, the file is left
alone and Start waits for Apply); without one the folder simply switches."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board_view, state, widgets  # noqa: E402
from test_board_view import page  # noqa: E402,F401 - the board page fixture

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def at_once(monkeypatch):
    def run(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            return done(None, exc)
        return done(result, None)
    monkeypatch.setattr(board_view, "background", run)


def workspace_paths(page):
    return [n.props.get("path") for n in page.board.of_kind("workspace")]


def test_change_folder_keeps_the_board_and_moves_its_folder_as_one_step(page, tmp_path, at_once, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    page.open_project(str(old), quiet=True)
    page.add_node("mcp", name="fetch")
    before_kinds = sorted(n.kind for n in page.board.nodes)
    opened = []
    page.opened.connect(opened.append)
    assert page.change_folder_button.text() == "Change folder…"
    monkeypatch.setattr(state, "run_dialog", lambda dialog: (opened.append(("start", dialog.directory().path())),
                                                            [str(new)])[1])
    assert page.change_folder() is True
    assert ("start", str(old)) in opened, "the current folder preselected"
    assert page.directory == str(new) and workspace_paths(page) == [str(new)]
    assert sorted(n.kind for n in page.board.nodes) == before_kinds, "every part stays"
    assert str(new) in opened, "the window's project follows"
    assert not (new / ".sandbox-ai.json").exists(), "nothing written before Apply"
    assert page.dirty() and page.undo.text(page.undo.index() - 1) == "Change folder"
    assert f"--workspace {new}" in page.preview.toPlainText(), "checked again for the new folder"
    page.undo.undo()
    assert page.directory == str(old) and workspace_paths(page) == [str(old)]
    page.undo.redo()
    assert page.directory == str(new)
    # The same folder again: nothing.
    assert page.change_folder(str(new)) is False


def test_a_folder_with_its_own_project_file_is_asked_about(page, tmp_path, at_once, monkeypatch):
    old, other = tmp_path / "old", tmp_path / "other"
    old.mkdir()
    other.mkdir()
    (other / ".sandbox-ai.json").write_text('{"agent": "codex", "options": ["--allow-pypi"]}\n')
    page.open_project(str(old), quiet=True)
    page.add_node("mcp", name="fetch")
    # Keep this board: the file is left alone, Start waits for Apply.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert page.change_folder(str(other)) is True
    question = next(text for title, text, _ in widgets.SHOWN if title == "Change folder")
    assert "has a project file of its own" in question
    assert page.directory == str(other) and page.board.of_kind("mcp"), "the board's settings stayed"
    assert page.untrusted.get(page.key_of(str(other))), "Start waits for Apply"
    assert (other / ".sandbox-ai.json").read_text().startswith('{"agent": "codex"'), "the file is left as it is"
    # Load it: the folder's own file is the board now (with the usual trust check).
    page.undo.undo()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    loaded = []
    monkeypatch.setattr(page, "open_project", lambda directory=None, quiet=False: loaded.append((directory, quiet)))
    page.change_folder(str(other))
    assert loaded == [(str(other), True)]
