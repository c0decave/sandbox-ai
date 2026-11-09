"""--ask in words (ask_choice.py): the Start page's chooser, the Board's approvals part with its third choice "Always
allow everything" (--ask allow, sandbox-ai 0.1.80), marked and explained; the security score, the beginner guide,
the wizard (which keeps it), the trust question, the Sandboxes page's mark, and a host too old for it."""
import os
from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import ask_choice as AC, backend, board as B, guide, i18n, project_trust as PT, widgets  # noqa: E402

needs_launcher = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def test_the_choice_speaks_in_values_and_shows_words(qapp):
    box = AC.AskChoice(["on", "off", "allow"], "on")
    assert box.currentText() == "on" and [box.itemText(i) for i in range(box.count())] == ["on", "off", "allow"]
    shown = [box.model().item(i).text() for i in range(box.count())]
    assert shown == ["Ask me (default)", "Refuse without asking", "⚠ Always allow everything"]
    box.setCurrentText("allow")
    assert box.currentText() == "allow" and box.findText("off") == 1
    assert "every question is allowed at once" in box.itemData(2, AC.Qt.ItemDataRole.ToolTipRole)
    assert AC.warned("--ask", "allow") and not AC.warned("--ask", "on") and not AC.warned("--mode", "allow")
    i18n.set_language("de")
    try:
        assert AC.label("allow") == "⚠ Immer alles erlauben" and AC.label("off") == "Ohne Frage ablehnen"
    finally:
        i18n.set_language("en")


def test_the_options_of_a_start_name_it_however_written():
    assert AC.in_options(["--ask", "allow"]) and AC.in_options(["-Q=allow"]) and AC.in_options(["--ask=allow"])
    assert not AC.in_options(["--ask", "on"]) and not AC.in_options(["--", "--ask", "allow"])
    assert AC.allows_all({"ask": "allow"}) and not AC.allows_all({"ask": "on"}) and not AC.allows_all(None)


def test_a_host_too_old_for_allow_is_found_out():
    """`sandbox-ai AGENT --help` there lists --ask's values: one without allow (before 0.1.80) refuses the start by
    name ("--ask allow"), as an option it does not know is refused; a host that lists no values says nothing."""
    old = backend.help_options("  -Q {on,off}, --ask {on,off}\n                        With the proxy, ask\n")
    new = backend.help_options("  -Q {on,off,allow}, --ask {on,off,allow}\n")
    assert {"--ask", "--ask=on", "--ask=off"} <= old and "--ask=allow" not in old and "--ask=allow" in new
    ask = SimpleNamespace(long="--ask", kind="choice", default="on", choices=["on", "off", "allow"],
                          tokens=lambda value: [] if value in ("", "on") else ["--ask", value])
    assert backend.choice_unknown(ask, "allow", old) and not backend.choice_unknown(ask, "allow", new)
    assert not backend.choice_unknown(ask, "allow", {"--ask"}), "no values listed: not known to be too old"
    assert not backend.choice_unknown(ask, "on", old)
    assert backend.unknown_options([ask], ["--ask", "allow", "--", "x"], old) == ["--ask allow"]
    assert backend.unknown_options([ask], ["--ask=allow"], old) == ["--ask allow"]
    assert backend.unknown_options([ask], ["--ask", "allow"], new) == []
    assert backend.dropped_options([ask], {"--ask": "allow"}, old) == ["--ask allow"]
    assert backend.dropped_options([ask], {"--ask": "allow"}, new) == []


def test_the_guide_and_the_trust_question_know_it():
    question = next(q for q in guide.questions("approvals") if q.key == "--ask")
    assert [value for value, _ in question.choices] == ["on", "off", "allow"]
    assert guide.props_of("approvals", {"--ask": "allow"})["--ask"] == "allow"
    assert PT.option_why("--ask allow") == "asks nothing: every question is allowed at once"
    assert PT.option_why("--allow-pypi") == "widens what the sandbox may do or see"


@needs_launcher
def test_the_board_scores_it_and_shows_it(qapp):
    sai = backend.SandboxAI()
    board = B.from_argv(sai, "claude", ["--ask", "allow"])
    assert board.first("approvals").props["--ask"] == "allow" and B.values(board, sai)[1]["--ask"] == "allow"
    score, badges = B.badges(board, {})
    found = [b for b in badges if "every question is allowed at once" in b.text]
    assert found and found[0].level == "bad" and found[0].cost == 30
    assert score < B.badges(B.from_argv(sai, "claude", []), {})[0]
    assert not [b for b in B.badges(B.from_argv(sai, "claude", ["--ask", "on"]), {})[1] if "allowed at once" in b.text]


@needs_launcher
def test_the_inspector_offers_always_allow_everything_and_explains_it(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.board_view import BoardPage, summary
    page = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20})
    try:
        page.add_node("approvals")
        node = page.board.first("approvals")
        page.inspect(node)
        box = next(c for c in page.inspector_holder.findChildren(QtWidgets.QComboBox) if isinstance(c, AC.AskChoice))
        note = next(label for label in page.inspector_holder.findChildren(QtWidgets.QLabel)
                    if "every question is allowed at once" in label.text())
        assert box.currentText() == "on" and note.isHidden()
        box.setCurrentIndex(box.findText("allow"))
        assert page.board.first("approvals").props["--ask"] == "allow"
        assert "--ask allow" in page.preview.toPlainText()
        assert summary(page.board.first("approvals")) == "⚠ Always allow everything"
        page.inspect(page.board.first("approvals"))
        note = next(label for label in page.inspector_holder.findChildren(QtWidgets.QLabel)
                    if "every question is allowed at once" in label.text())
        assert not note.isHidden() and note.property("role") == "notice-warn"
        page.forbidden = {"--ask": {"allow"}}
        page.inspect(page.board.first("approvals"))
        box = next(c for c in page.inspector_holder.findChildren(QtWidgets.QComboBox) if isinstance(c, AC.AskChoice))
        assert not box.model().item(box.findText("allow")).isEnabled()
    finally:
        page.deleteLater()


def test_the_sandboxes_page_marks_a_start_that_asks_nothing(qapp):
    from sandbox_ai_app import ui
    stub = SimpleNamespace(stale=lambda i: None, is_asking=lambda i: False, mine=lambda i: False, remote_seen={})
    start = backend.Instance("run", "run:web", "web", run="web", project="/w", agent="claude", backend="podman",
                             started="", state="running", proxy=True, record={"ask": "allow"})
    row = ui.SandboxesPage.start_row(stub, start)
    assert "⚠ asks nothing, allows every question" in row["cells"][6] and row["color"] == "warn"
    assert "every question is allowed at once" in row["tip"]
    start.record = {"ask": "on"}
    assert "asks nothing" not in ui.SandboxesPage.start_row(stub, start)["cells"][6]


@needs_launcher
def test_the_start_page_offers_it_marked(qapp):
    from sandbox_ai_app import ui
    sai = backend.SandboxAI()
    form = ui.OptionsForm(sai.options())
    try:
        box = form.widgets["--ask"]
        assert isinstance(box, AC.AskChoice) and box.currentText() == "on"
        assert form.values()["--ask"] == "on" and "--ask" not in backend.argv_from_values(sai.options(), form.values())
        box.setCurrentText("allow")
        form.mark_dangerous()
        assert form.values()["--ask"] == "allow"
        assert ["--ask", "allow"] == [t for t in backend.argv_from_values(sai.options(), form.values())
                                      if t in ("--ask", "allow")]
        assert form.labels["--ask"].title.property("role") == "notice-warn"
        form.apply_policy({"--ask": {"allow"}})
        assert not box.model().item(box.findText("allow")).isEnabled()
    finally:
        form.deleteLater()
