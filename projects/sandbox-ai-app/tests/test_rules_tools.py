"""The help beside the ruleset files (--rules FILE) on the Board and the Start page, offscreen: what to enter (a
placeholder, an example in the tooltip), New from example… (a starter written outside every workspace, refused inside
one, then added) and Check (the launcher's own dry run: the rules it reads, or why it refuses a file)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, rules_tools, widgets  # noqa: E402

from test_projects import env  # noqa: E402,F401 - shared fixture

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def tools(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    (tmp_path / "work").mkdir()
    listed = []
    widget = rules_tools.RulesetTools(backend.SandboxAI(), lambda: list(listed), listed.append,
                                      lambda: [str(tmp_path / "work")])
    widget.listed = listed
    yield widget
    widgets.ThreadCall.wait_all(60)
    widget.deleteLater()


def test_the_example_is_a_ruleset_the_launcher_reads(tools, tmp_path, wait):
    path = tools.new_from_example(str(tmp_path / "config" / "github.json"))
    assert path and tools.listed == [path] and os.stat(path).st_mode & 0o077 == 0
    data = json.loads(open(path).read())
    assert data["version"] == 1 and [r["id"] for r in data["http"]] == ["no-gists", "no-issue-writes", "releases"]
    assert "Written" in tools.result.text()
    tools.check()
    assert wait(lambda: tools.result.text().startswith(("✔", "✘")), 90), tools.result.text()
    assert tools.result.text().startswith("✔"), tools.result.text()
    assert "3 HTTP and 3 MCP rules" in tools.result.text()


def test_a_ruleset_inside_a_workspace_is_refused_with_the_reason(tools, tmp_path):
    assert tools.new_from_example(str(tmp_path / "work" / "rules.json")) is None
    assert not (tmp_path / "work" / "rules.json").exists() and tools.listed == []
    title, text, _ = widgets.SHOWN[-1]
    assert "inside the workspace" in text and "~/.config/sandbox-ai/" in text


def test_check_shows_why_the_launcher_refuses_a_file(tools, tmp_path, wait):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "http": [{"id": "x y", "decision": "maybe"}]}))
    tools.listed.append(str(bad))
    tools.check()
    assert wait(lambda: tools.result.text().startswith(("✔", "✘")), 90), tools.result.text()
    assert tools.result.text().startswith("✘") and "id" in tools.result.text()
    assert tools.result.wordWrap()
    tools.listed.clear()
    tools.check()
    assert "Add a ruleset file first" in tools.result.text()


def test_the_field_says_what_to_enter(qapp):
    assert "~/.config/sandbox-ai/github.json" in rules_tools.placeholder()
    tip = rules_tools.tooltip()
    assert "no-gists" in tip and "outside every workspace" in tip


def test_the_board_and_the_start_page_offer_them(qapp, monkeypatch, tmp_path, env):  # noqa: F811
    from test_projects import close, make_window
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    window = make_window(monkeypatch)
    try:
        board = window.board
        board.add_node("approvals", **{"--rules": []})
        board.inspect(board.board.first("approvals"))
        tools = board.rules_tools
        assert isinstance(tools, rules_tools.RulesetTools) and tools.new_button.text() == "New from example…"
        written = tools.new_from_example(str(tmp_path / "cfg" / "github.json"))
        lines = tools.parent().findChild(QtWidgets.QPlainTextEdit)
        assert lines.toPlainText() == written and "~/.config/sandbox-ai/github.json" in lines.placeholderText()
        assert "no-gists" in lines.toolTip()
        field = window.launch.form.widgets["--rules"]
        assert field.field.edit.placeholderText() == rules_tools.placeholder()
        field.tools.new_from_example(str(tmp_path / "cfg" / "start.json"))
        assert window.launch.form.values()["--rules"] == [str(tmp_path / "cfg" / "start.json")]
        window.launch.form.set_values({"--rules": ["/a.json"]})
        assert field.value() == ["/a.json"]
    finally:
        close(window)


def test_check_refuses_a_file_inside_a_workspace_and_all_rules_lists_every_ruleset(tools, tmp_path, wait):
    """Check is sandbox-ai rules check (a file inside a workspace refused as the start refuses it); All rules… is
    sandbox-ai rules show: every ruleset of the start in order, with where it comes from."""
    inside = tmp_path / "work" / "rules.json"
    rules_tools.write_example(str(inside))
    tools.listed.append(str(inside))
    tools.check()
    assert wait(lambda: tools.result.text().startswith(("✔", "✘")), 90), tools.result.text()
    assert tools.result.text().startswith("✘") and "inside the workspace" in tools.result.text()
    tools.listed[:] = [rules_tools.write_example(str(tmp_path / "config" / "github.json"))]
    tools.show_all()
    assert wait(lambda: widgets.SHOWN and widgets.SHOWN[-1][0] == "All rules of this start", 90), tools.result.text()
    text = widgets.SHOWN[-1][1]
    assert f"--rules github.json — from {tmp_path / 'config' / 'github.json'}" in text
    assert "no-gists: deny" in text and "files-deletes: ask" in text
    assert rules_tools.all_rules_text({"rulesets": []}).startswith("This start has no rulesets")
