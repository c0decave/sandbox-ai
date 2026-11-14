"""Whole-codebase review round 1 (0.1.95), the desktop app: each fix with the test that failed before it."""
import pytest

from sandbox_ai_app import i18n


@pytest.fixture
def language():
    before = i18n.language()
    yield i18n.set_language
    i18n.set_language(before)


def test_relative_times_read_naturally_in_both_languages(language):
    """The Approvals page said "vor 55 s" (and "3600 s ago" for a question without a time limit); every relative time
    of the app now takes the largest unit that fits, in words a German reader uses."""
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from sandbox_ai_app.widgets import ago
    language("en")
    assert [ago(s) for s in (0, 55, 61, 3599, 7200, 86400, 3 * 86400)] == [
        "0 s ago", "55 s ago", "1 min ago", "59 min ago", "2 h ago", "1 day ago", "3 days ago"]
    assert ago(-5) == "0 s ago" and ago(None) == "?"
    language("de")
    assert [ago(s) for s in (55, 180, 7200, 86400, 3 * 86400)] == [
        "vor 55 Sek.", "vor 3 Min.", "vor 2 Std.", "vor 1 Tag", "vor 3 Tagen"]
    assert i18n.tr("no answer (last seen {ago})", ago=ago(55)) == "keine Antwort (zuletzt gesehen vor 55 Sek.)"
    assert i18n.tr("{state}, last seen {ago}", state="läuft", ago=ago(600)) == "läuft, zuletzt gesehen vor 10 Min."


def test_hosts_page_last_seen_uses_the_same_words(language):
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from sandbox_ai_app import hosts_page
    language("en")
    assert hosts_page.reach_text({"seen": 1000.0, "problem": "x"}, now=1000.0 + 7200) == \
        "no answer (last seen 2 h ago)"


def test_allow_npm_is_a_network_preset_on_the_board():
    """--allow-npm (the coding-<agent> presets have it) was no option of any Board part: it went to the board's hidden
    extra options, so the network part did not show it, the capabilities did not count it, and an offline board with
    it was not said to contradict itself."""
    import os
    from sandbox_ai_app import backend, board as B
    if not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"):
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    sai = backend.SandboxAI()
    board = B.from_argv(sai, "qwen", ["--allow-npm", "--allow-pypi"])
    assert "--allow-npm" not in board.extra and board.first("network").props.get("--allow-npm") is True
    assert "--allow-npm" in B.to_argv(sai, board)
    reach = [c.text for c in B.capabilities(board) if c.area == "reach"]
    assert any("npm" in text for text in reach), reach
    offline = B.from_argv(sai, "claude", ["--allow-npm", "--offline"])
    assert any("Offline contradicts" in p.text and "--allow-npm" in p.text for p in B.problems(sai, offline))
