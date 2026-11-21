"""Mounts: you decide (0.1.98). A host path you choose - a workspace, a mount, a state dir, a folder the app creates,
a workspace added to a running sandbox - is given even where it holds a risk (HOME, sandbox-ai's own folders, the
launcher's tree, a kernel tree, XDG_RUNTIME_DIR); the launcher says what it means, and the app shows it: the
Board's verdict accepts with a warning, a folder made says it, a workspace added says it - in the app's language,
the launcher's own words below."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, board as B, i18n, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
HOME_WARNING = ("agent-sandbox: warning: Workspace /home/u is your HOME directory: the agent can change your shell "
                "start files, credentials and sandbox-ai's trust store: everything you can do on this host; read-only "
                "(--ro, a --mount with :ro) is the safer choice, though it still reads all of it (keys, tokens)")


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
    view = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    yield view
    view.deleteLater()


def test_the_launchers_warnings_are_read_from_what_it_said():
    said = f"agent-sandbox: something\n{HOME_WARNING}\n{HOME_WARNING}\nsandbox-ai mcp: warning: MCP server x: y\n"
    found = B.launcher_warnings(said)
    assert found == [HOME_WARNING.split("warning: ", 1)[1], "MCP server x: y"], "each once, without the prefix"
    assert B.launcher_warnings("") == [] and B.launcher_warnings(None) == [] and B.path_warnings([]) == ""


def test_the_warnings_are_introduced_in_the_apps_language():
    try:
        i18n.set_language("de")
        text = B.path_warnings(["Workspace /home/u is your HOME directory"])
        assert text.startswith("Sie entscheiden, was eine Sandbox bekommt") and "nur lesbar" in text
        assert "• Workspace /home/u is your HOME directory" in text
    finally:
        i18n.set_language("en")


def test_the_boards_verdict_accepts_with_the_warning(page):
    """The launcher's dry run accepts a board with HOME as its workspace and warns: the verdict says both (it said
    only "accepts" before, and the warning was lost)."""
    page.checking_state = page.board.state()
    page.check_token += 1
    page.checked(page.check_token, ({"agent": "claude"}, HOME_WARNING), None, "local")
    assert page.verdict.text().startswith("The launcher accepts these options, with a warning:")
    assert "your HOME directory" in page.verdict.whole_text() and "read-only" in page.verdict.whole_text()
    assert page.verdict.property("role") == "notice-warn" and page.launcher_ok
    page.checked(page.check_token, ({"agent": "claude"}, ""), None, "local")
    assert page.verdict.text() == "The launcher accepts these options."
    assert page.verdict.property("role") == "notice-ok"


def test_a_folder_made_says_the_launchers_warning(qapp, homes):
    """The app's Create makes a folder you chose in XDG_RUNTIME_DIR (it refused before): made, and the launcher's
    warning is handed back for the app to show."""
    said = []
    folder = homes / "runtime" / "project"
    assert backend.SandboxAI().create_folders([str(folder)], said) == [str(folder)]
    assert folder.is_dir() and any("XDG_RUNTIME_DIR" in text and "socket" in text for text in said), said


def test_a_workspace_added_with_a_warning_shows_it(qapp, monkeypatch):
    from sandbox_ai_app import ui
    shown, done = [], []
    window = ui.MainWindow.__new__(ui.MainWindow) if hasattr(ui, "MainWindow") else None
    target = window or type("W", (), {})()
    monkeypatch.setattr(ui, "warn", lambda parent, title, text, *a, **kw: shown.append((title, text)))
    target.update_buttons = lambda: done.append("buttons")
    target.load_devices = lambda: done.append("devices")
    target.device_done = lambda title, result, error, draft=None: done.append("plain")
    ui_class = next(cls for cls in vars(ui).values() if isinstance(cls, type) and "workspace_added" in vars(cls))
    ui_class.workspace_added(target, "Add workspace", (0, "added /home/u to web at /workspaces/u (rw)\n",
                                                       HOME_WARNING + "\n"), None)
    (title, text), = shown
    assert title == "Add workspace" and "your HOME directory" in text and "added /home/u" in text
    assert done == ["buttons", "devices"]
    ui_class.workspace_added(target, "Add workspace", (0, "added /srv/p\n", ""), None)
    assert done[-1] == "plain" and len(shown) == 1, "without a warning: as any change"
