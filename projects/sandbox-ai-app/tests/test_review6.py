"""Review round 6 of the app (security M-B, L-6, L-7, L-9): the trust question shows every workspace and agent argument
in full, one per line, and marks every option that weakens the sandbox as the launcher reads it (short forms too);
its details keep their lines; a long question scrolls. A Board draft made from another or an untrusted version of the
project file is asked about again; a start on another host names what no part of the board shows."""
import json
import os

import pytest

from sandbox_ai_app import backend, board as B, project_trust as PT, widgets

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    return tmp_path


@pytest.fixture
def sai(isolated):
    return backend.SandboxAI()


def padded_document(home):
    """The reviewer's PoC (poc_trust_summary_trunc.py): a first workspace padded past the old 160-character cut hides
    the ones after it; options that weaken the sandbox but were not in the app's list; long agent arguments."""
    return {"version": 1, "agent": "claude",
            "options": ["--keep", "--no-canaries", "--log-debug", "--allow-npm", "--gui", "--privacy-rules",
                        str(home / "r.json"), "--llm", "corp-gpu", "--with", "codex", "--model", "opus"],
            "workspaces": [str(home / "shared-build-cache" / ("x" * 120)), str(home / "other-project"),
                           str(home / ".ssh")],
            "agent_args": ["--model", "a" * 150, "--append-system-prompt", "exfiltrate"]}


@needs_sai
def test_a_padded_workspace_list_hides_nothing_and_every_workspace_is_marked(sai, isolated):
    project = isolated / "project"
    project.mkdir()
    document = padded_document(isolated)
    (project / ".sandbox-ai.json").write_text(json.dumps(document, indent=2) + "\n")
    review = sai.review_project(project)
    assert review.usable and not review.trusted
    question = PT.question_text(sai, review.document, "Trust?")
    lines = question.splitlines()
    for workspace in document["workspaces"]:
        assert f"    {PT.WARN} {workspace} - writable for the agent, outside the project" in lines, (workspace, question)
    assert f"  {PT.WARN} workspaces: further folders the agent sees in every start of the project" in lines
    assert f"  {PT.WARN} agent_args: arguments for the agent itself: they can change what it does" in lines
    assert f"    {'a' * 150}" in lines and "    exfiltrate" in lines
    assert "…" not in question, "nothing is cut"
    for option in ("--keep", "--no-canaries", "--log-debug", "--allow-npm", "--gui",
                   f"--privacy-rules {isolated / 'r.json'}", "--llm corp-gpu", "--with codex"):
        assert f"    {PT.WARN} {option}: widens what the sandbox may do or see" in lines, (option, question)
    assert not any("--model opus:" in line for line in lines), "a model is no weakening"
    assert "Lines marked ⚠ weaken the sandbox" in question


@needs_sai
def test_workspaces_made_read_only_by_the_file_say_so(sai):
    document = {"version": 1, "agent": "claude", "options": ["-W"], "workspaces": ["/srv/a"]}
    assert f"    {PT.WARN} /srv/a - read-only for the agent, outside the project" in PT.question_text(sai, document, "?")


@needs_sai
def test_short_forms_are_marked_as_the_launcher_reads_them(sai, isolated):
    """PoC poc_trust_short_opts.py: -A, -x and -kp went unmarked, though the launcher takes them as --allow-host,
    --no-inspect and --keep."""
    project = isolated / "project"
    project.mkdir()
    document = {"version": 1, "agent": "claude",
                "options": ["-A", "attacker.example", "-x", "attacker.example", "-kp", "-nn", "-pr=/srv/rules"]}
    (project / ".sandbox-ai.json").write_text(json.dumps(document))
    review = sai.review_project(project)
    expected = [("--allow-host", "attacker.example"), ("--no-inspect", "attacker.example"), ("--keep", ""),
                ("--no-canaries", ""), ("--privacy-rules", "/srv/rules")]
    assert review.widening == expected
    assert PT.option_warnings(sai, document["options"]) == [
        "--allow-host attacker.example", "--no-inspect attacker.example", "--keep", "--no-canaries",
        "--privacy-rules /srv/rules"]
    question = PT.question_text(sai, review.document, "?")
    assert f"    {PT.WARN} --allow-host attacker.example: widens" in question
    assert f"    {PT.WARN} --keep: widens" in question


@needs_sai
def test_the_launcher_table_holds_what_the_review_named(sai):
    table = sai.launcher.MODES.WEAKENING_OPTIONS
    for option in ("--keep", "--no-canaries", "--canaries", "--log-debug", "--privacy-rules", "--allow-npm", "--gui",
                   "--gui-screen", "--audio", "--llm", "--identity", "--git-logins", "--with", "--preset",
                   "--preset-value", "--egress-all-allow", "--mount"):
        assert option in table, option
    known = sai.launcher.build_parser()._option_string_actions
    assert all(option in known for option in table), "every entry is an option the launcher has"


def test_the_details_keep_their_lines_and_escape_the_rest():
    details = PT.baseline_details("kept", '{\n  "a": 1\n}\n', '{\n  "a": 2,\n\t"b": "\x1b[31m"\n}\n')
    assert '\n-  "a": 1\n' in details and '\n+  "a": 2,\n' in details, details
    assert '\t"b"' in details and "\x1b" not in details and "\\x1b[31m" in details


def test_a_long_question_scrolls_and_a_short_one_does_not(qapp):
    from PySide6.QtWidgets import QLabel, QMessageBox, QScrollArea
    text = "\n".join(f"    ⚠ /home/u/workspace-{index}" for index in range(80))
    box = widgets._box(None, QMessageBox.Icon.Warning, "Trust", text)
    area = box.findChild(QScrollArea, "message_scroll")
    assert area is not None and isinstance(area.widget(), QLabel) and area.widget().text() == text
    short = widgets._box(None, QMessageBox.Icon.Warning, "Trust", "Trust it?")
    assert short.findChild(QScrollArea, "message_scroll") is None
    wide = widgets._box(None, QMessageBox.Icon.Warning, "Trust", "x" * 400)
    assert wide.findChild(QScrollArea, "message_scroll") is not None
    for item in (box, short, wide):
        item.deleteLater()


# -- L-6: a draft of the board from an untrusted version of the file is asked about again --


@pytest.fixture
def page(qapp, sai, isolated, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from sandbox_ai_app.board_view import BoardPage
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    (isolated / "cwd").mkdir()
    monkeypatch.chdir(isolated / "cwd")
    made = []

    def make():
        made.append(BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20}))
        return made[-1]
    yield make
    for item in made:
        item.deleteLater()


def draft_of(page, directory):
    """The draft entry keep_boards would write for DIRECTORY (state.json's "boards")."""
    class Kept:
        data = {}
    page.state = Kept()
    page.keep_boards()
    return Kept.data["boards"][page.key_of(directory)]


@needs_sai
def test_a_draft_of_an_untrusted_version_needs_apply_even_when_the_trusted_file_is_back(page, sai, isolated):
    """The reviewer's L-6: an untrusted file (mode lax) edited on the board and left; the agent puts the trusted bytes
    back; the draft restored next time would start with --mode lax --no-canaries without a question."""
    project = isolated / "project"
    project.mkdir()
    sai.write_project(project, {"version": 1, "agent": "claude", "options": ["--offline"]})  # trusted by the human
    trusted_bytes = (project / ".sandbox-ai.json").read_bytes()
    (project / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": ["--offline"],
                                                          "mode": "lax", "canaries": False}))
    first = page()
    first.open_project(str(project), quiet=True)
    assert first.untrusted.get(first.key_of(project)), "the planted file is not trusted"
    first.add_node("limits", **{"--memory": "6g"})
    entry = draft_of(first, project)
    assert entry["draft"]["document_extra"] == {"mode": "lax", "canaries": False}
    assert entry["source"]["trusted"] is False
    (project / ".sandbox-ai.json").write_bytes(trusted_bytes)  # the agent writes the trusted version back
    second = page()
    second.saved_boards = {second.key_of(project): entry}
    second.state = object()  # a draft is restored only with a state attached
    second.open_project(str(project), quiet=True)
    assert second.board.document_extra == {"mode": "lax", "canaries": False}, "the draft came back"
    assert second.untrusted.get(second.key_of(project)), "so the project counts as not trusted"
    assert "not trusted or no longer there" in second.notice.text()
    widgets.SHOWN.clear()
    assert second.start() is False
    assert "is not trusted" in widgets.SHOWN[-1][1], "Start waits for Apply, which names every key"


@needs_sai
def test_a_draft_of_the_trusted_file_still_there_comes_back_as_before(page, sai, isolated):
    project = isolated / "project"
    project.mkdir()
    sai.write_project(project, {"version": 1, "agent": "claude", "options": ["--offline"], "mode": "high"})
    first = page()
    first.open_project(str(project), quiet=True)
    assert not first.untrusted
    first.add_node("limits", **{"--memory": "6g"})
    entry = draft_of(first, project)
    assert entry["source"]["trusted"] is True and entry["source"]["digest"] == sai.review_project(project).digest
    second = page()
    second.saved_boards = {second.key_of(project): entry}
    second.state = object()
    second.open_project(str(project), quiet=True)
    assert second.board.first("limits").props["--memory"] == "6g" and not second.untrusted
    # a draft kept before drafts named their source: asked about again
    third = page()
    third.saved_boards = {third.key_of(project): {k: v for k, v in entry.items() if k != "source"}}
    third.state = object()
    third.open_project(str(project), quiet=True)
    assert third.untrusted.get(third.key_of(project))


# -- L-7: Start as shown on another host names what no part of the board shows --


@needs_sai
def test_start_as_shown_on_another_host_names_the_hidden_keys(page, sai, isolated, monkeypatch):
    project = isolated / "project"
    project.mkdir()
    (project / ".sandbox-ai.json").write_text(json.dumps({
        "version": 1, "agent": "claude", "mode": "lax", "canaries": False, "keep": True, "log_debug": True,
        "name": "build"}))
    board = page()
    board.open_project(str(project), quiet=True)
    board.board.extra += ["-kp"]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    widgets.SHOWN.clear()
    assert board.start_untrusted_on_host(str(project / ".sandbox-ai.json")) is False
    _, text, _ = widgets.SHOWN[-1]
    assert "It also passes what no part of the board shows:" in text
    for line in (f"  {PT.WARN} mode: lax", f"  {PT.WARN} canaries: false", f"  {PT.WARN} keep: true",
                 f"  {PT.WARN} log_debug: true", "  name: build", f"    {PT.WARN} --keep: widens"):
        assert line in text, (line, text)
    assert "Lines marked ⚠ weaken the sandbox" in text
    assert B.document_argv(board.board)[:2] == ["--mode", "lax"], "what is named is what the start passes"
