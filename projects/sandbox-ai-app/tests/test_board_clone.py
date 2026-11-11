"""Clone on the board (feedback 2026-11-11): this setup as a new project (another folder, its workspace that folder,
not applied until Apply) or as a new session (the next Start, under a new codename)."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, widgets  # noqa: E402

from test_board_view import page  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def test_clone_as_a_new_project(page, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    page.open_project(str(old), quiet=True)
    page.board.first("workspace").props["path"] = str(old)
    page.board.first("network").props["--allow-tcp"] = ["10.0.0.5:22"]
    before = {n.kind: dict(n.props) for n in page.board.nodes}
    assert page.clone_to_project(str(new))
    assert page.key_of(page.directory) == page.key_of(new)
    assert page.board.first("workspace").props["path"] == str(new)
    assert page.board.first("network").props["--allow-tcp"] == ["10.0.0.5:22"]
    assert {n.kind for n in page.board.nodes} == set(before)
    assert page.dirty() and not (new / ".sandbox-ai.json").exists(), "Apply writes it, not the clone"
    page.activate(str(old))
    assert page.board.first("workspace").props["path"] == str(old), "the original stays as it was"


def test_clone_refuses_this_folder_and_a_folder_with_a_project_file(page, tmp_path):
    here, taken = tmp_path / "here", tmp_path / "taken"
    here.mkdir()
    taken.mkdir()
    (taken / ".sandbox-ai.json").write_text("{}")
    page.open_project(str(here), quiet=True)
    assert not page.clone_to_project(str(here))
    assert not page.clone_to_project(str(taken))
    assert any("has a project file already" in str(text) for _title, text, _d in widgets.SHOWN)
    assert page.key_of(page.directory) == page.key_of(here)


def test_clone_as_a_new_session(page):
    controls = page.start_controls
    controls.as_session.setChecked(False)
    controls.name.setText("old-name")
    page.clone_to_session()
    assert controls.as_session.isChecked() and controls.name.text() == ""
    assert controls.suggestion and controls.suggestion in page.notice.text()
    assert [a.text() for a in page.clone_button.menu().actions()] == ["As a new project…", "As a new session"]
