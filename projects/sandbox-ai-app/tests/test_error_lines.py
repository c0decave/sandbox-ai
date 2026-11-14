"""0.1.95, from what the user saw: the app showed a refusal of several lines (the launcher's MCP pre-check, one problem
per line) with "\\x0a" where its line breaks were - "...(with its other variables)\\x0a - MCP server whatweb-tor
needs ...". Error texts went through files.printable(), meant for names (an agent's file name must not fake a line),
which escapes line breaks. A message (sandbox-ai's error, its output, an exception) goes through
files.printable_text(): its line breaks and tabs kept, every other control character still escaped."""
import ast
import os
from pathlib import Path

import pytest

from sandbox_ai_app import files

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"
REFUSAL = ("agent-sandbox: 2 problems with the MCP servers of this start (nothing was started or made; fix them, then "
           "start again):\n  - MCP server gowitness-tor needs the variable ALLOW_PRIVATE_TARGETS: set it, or keep it "
           "with sandbox-ai secrets set ALLOW_PRIVATE_TARGETS; a value that is no secret goes into its definition: "
           "sandbox-ai mcp set gowitness-tor --env ALLOW_PRIVATE_TARGETS=VALUE (with its other variables)\n"
           "  - MCP server whatweb-tor needs the variable ALLOW_PRIVATE_TARGETS: set it")
# The file manager's own modules: their texts carry the names of files an agent chose (reviewed apart, 19 passes).
FILE_MANAGER = {"files.py", "files_page.py", "file_editor.py", "file_properties.py", "markdown_view.py", "remote.py"}
# What holds a message rather than a name: an exception, sandbox-ai's error or output, what it said.
MESSAGES = {"exc", "error", "err", "error_", "why", "said", "refusal", "message", "stderr", "out"}


def test_printable_text_keeps_the_lines_and_escapes_the_rest():
    assert files.printable_text(REFUSAL) == REFUSAL
    assert files.printable(REFUSAL).count("\\x0a") == 2, "names: a line break stays visible as such"
    assert files.printable_text("a\n\tb\x1b[31mc‮") == "a\n\tb\\x1b[31mc\\u202e"


def message_calls(tree):
    """printable(X) calls whose X is a message: a name of MESSAGES, str() of one, (err or out), (out + err)..."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "printable"
                and node.args):
            continue
        arg = node.args[0]
        # A field of a record (error.get("kit"), exc.path) is a name, not the message.
        fields = {id(n.value) for n in ast.walk(arg) if isinstance(n, ast.Attribute) and n.attr not in (
            "strip", "removeprefix", "strerror")}
        names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name) and id(n) not in fields}
        if names & MESSAGES:
            yield node.lineno, ast.unparse(node)


def test_no_message_goes_through_the_names_printable():
    """Every error, refusal and output of sandbox-ai the app shows keeps its lines (printable_text); printable() of
    a message would show "\\x0a" again."""
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in FILE_MANAGER:
            continue
        found += [f"{path.name}:{line}: {call}" for line, call in message_calls(ast.parse(path.read_text()))]
    assert found == []


QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402

needs_sandbox_ai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
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


@needs_sandbox_ai
def test_the_boards_verdict_shows_the_launchers_lines(qapp, homes):
    """The path the user saw: the Board's check (a dry run) refused with the MCP pre-check's list. Its one line under
    the canvas says it without "\\x0a"; its tooltip, copy and window of the whole text keep each problem on a line."""
    from sandbox_ai_app.board_view import BoardPage
    page = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    try:
        page.add_node("mcp", name="whatweb-tor")
        page.checking_state = page.board.state()
        page.check_token += 1
        page.checked(page.check_token, (None, REFUSAL + "\x1b[2J"), None, "local")
        line = page.verdict.text()
        assert "\\x0a" not in line and "\n" not in line, "one line under the canvas"
        assert "(with its other variables) - MCP server whatweb-tor needs" in line
        whole = page.verdict.whole_text()
        assert "(with its other variables)\n  - MCP server whatweb-tor needs" in whole and "\\x0a" not in whole
        assert "\\x1b[2J" in whole, "every other control character still escaped"
        assert "\n  - MCP server whatweb-tor" in page.verdict.toolTip() or "whatweb-tor" in page.verdict.toolTip()
    finally:
        page.deleteLater()


def test_an_elided_label_keeps_a_whole_text_of_several_lines(qapp):
    label = widgets.ElidedLabel()
    label.setText("one line", whole="one\nline")
    assert label.text() == "one line" and label.whole_text() == "one\nline"
    label.setText("plain")
    assert label.whole_text() == "plain", "without WHOLE: the text itself"
    label.setText("")
    assert label.whole_text() == "" and label.toolTip() == ""


@needs_sandbox_ai
def test_a_start_the_launcher_refuses_says_its_lines(qapp, homes, monkeypatch):
    """The Start page: a start that cannot be run (an exception of several lines) is a message with its lines."""
    from sandbox_ai_app import ui
    shown = []
    monkeypatch.setattr(ui, "warn", lambda parent, title, text, *a, **kw: shown.append((title, text)))
    window = ui.LaunchPage.__new__(ui.LaunchPage)  # only the start's failure path is exercised

    class Refusing:
        def launch_argv(self, *args, **kwargs):
            raise ValueError(REFUSAL)
    window.sai = Refusing()
    for name, value in (("refused_options", lambda title: False), ("require_workspace", lambda title: True),
                        ("check_prerequisites", lambda then=None, why=None: False), ("host", lambda: ui.LOCAL),
                        ("argv", lambda: []), ("agent_arguments", lambda: []), ("workspace", lambda: str(homes))):
        setattr(window, name, value)
    window.agent = type("Agent", (), {"currentText": lambda self: "claude"})()
    assert ui.LaunchPage.start(window) is False
    assert shown and "\\x0a" not in shown[0][1] and "variables)\n  - MCP server whatweb-tor" in shown[0][1]
