"""0.1.95, from what the user hit: a board whose project folder did not exist yet started into "[Errno 2] No such file
or directory: '.../c0de/tor-network-another'" (the terminal was opened in it). The Board's check says
the folder does not exist and will be created, with Create now; Start and Apply create it (with its parents) after a
short question; it is refused only where a start would refuse the workspace - with the reason, never an errno."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, board as B, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    return tmp_path


@pytest.fixture
def page(qapp, homes):
    from sandbox_ai_app.board_view import BoardPage
    started = []
    view = BoardPage(backend.SandboxAI(), lambda *a, **kw: started.append((a, kw)), lambda: {"board_grid": 20})
    view.started = started
    yield view
    view.deleteLater()


def workspace(page, path):
    node = page.add_node("workspace", path=str(path)) or page.board.of_kind("workspace")[0]
    page.set_prop(node.id if hasattr(node, "id") else node, "path", str(path))
    page.refresh()
    return page.board.of_kind("workspace")[0]


def findings(page):
    return [page.findings.item(i).text() for i in range(page.findings.count())]


def test_missing_workspaces(homes):
    board = B.Board()
    board.add("agent", agent="claude")
    node = board.add("workspace", path=str(homes / "c0de" / "tor-network-another"))
    assert B.missing_workspaces(board) == [(node.id, str(homes / "c0de" / "tor-network-another"))]
    node.props["path"] = str(homes)
    assert B.missing_workspaces(board) == []
    node.props["path"] = ""
    assert B.missing_workspaces(board) == [], "no folder yet: the board's own check says so"
    (homes / "file").write_text("x")
    node.props["path"] = str(homes / "file")
    assert B.missing_workspaces(board) == [], "something is there: the launcher says why it is refused"


def test_the_check_says_it_will_be_created_and_create_now_makes_it(page, homes, wait):
    folder = homes / "c0de" / "tor-network-another"
    node = workspace(page, folder)
    text = f"! The folder {folder} does not exist: it will be created (Create now, or at Start or Apply)."
    assert text in findings(page)
    assert not page.create_button.isHidden()
    assert not [p for p in page.errors() if "does not exist" in p.text], "a warning: it does not stop the start"
    page.create_button.click()
    assert wait(lambda: folder.is_dir())
    assert wait(lambda: page.create_button.isHidden()) and text not in findings(page)
    assert node.id  # (the part stays as it was)


def test_start_creates_it_after_a_yes_and_goes_on(page, homes, wait, monkeypatch):
    folder = homes / "projects" / "new"
    workspace(page, folder)
    went_on = []
    monkeypatch.setattr(page, "check_prerequisites", lambda then=None, why=None: went_on.append(True) or True)
    assert page.start() is False  # asked, then made in the background
    assert any(title == "Create the folder" and str(folder) in text for title, text, _ in widgets.SHOWN)
    assert wait(lambda: went_on), "the start went on once the folder was made"
    assert folder.is_dir()


def test_start_without_a_yes_makes_nothing(page, homes, monkeypatch):
    folder = homes / "projects" / "declined"
    workspace(page, folder)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    from sandbox_ai_app import board_view
    monkeypatch.setattr(board_view, "confirm", lambda *a, **kw: False)
    assert page.start() is False and not folder.exists() and page.started == []


def test_a_folder_a_start_would_refuse_is_refused_with_the_reason(page, homes, wait, monkeypatch):
    from sandbox_ai_app import board_view
    said = []
    monkeypatch.setattr(board_view, "warn", lambda parent, title, text, *a, **kw: said.append((title, str(text))))
    folder = homes / "runtime" / "inside"  # XDG_RUNTIME_DIR: the sandbox's sockets and approvals live there
    workspace(page, folder)
    page.create_missing()
    assert wait(lambda: said)
    title, text = said[0]
    assert title == "Not created" and "XDG_RUNTIME_DIR" in text and "Errno" not in text
    assert not folder.exists()


def test_the_start_page_creates_its_workspace_after_a_yes(qapp, homes, wait, monkeypatch):
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "background", lambda function, *args, done, **kw: done(function(*args), None))
    folder = homes / "start-page" / "work"
    window = ui.LaunchPage.__new__(ui.LaunchPage)  # only the folder's part of the start is exercised
    window.sai = backend.SandboxAI()
    window.host = lambda: ui.LOCAL
    window.workspace = lambda: str(folder)
    went_on = []
    assert ui.LaunchPage.folder_made(window, lambda: went_on.append(True)) is False
    assert wait(lambda: went_on) and folder.is_dir()
    assert ui.LaunchPage.folder_made(window, lambda: went_on.append(False)) is True, "there now: on at once"
