"""The navigation's order (user 2026-11-18: the terminals right after the sessions they belong to, then the approvals;
Ctrl+3, Ctrl+4 and Ctrl+5 open them in that order) and its names (user 2026-11-24: "Board/Start sollte unter Laufend
und Laufend umbenannt werden in Projekt", "Es heißt nicht mehr Start sondern Board", and "Sandboxen und Sitzungen
sollten nur noch Sitzungen im Deutschen und Sessions im Englischen heißen")."""
import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import ui  # noqa: E402
from sandbox_ai_app.i18n import set_language, tr  # noqa: E402


def test_project_lists_the_board_then_sessions_terminals_and_approvals():
    sections = dict(ui.SECTIONS)
    assert "Running" not in sections and list(sections)[0] == "Project"
    assert [key for key, _ in sections["Project"]] == ["launch", "board", "sandboxes", "terminals", "approvals"]
    assert ui.PAGES[0] == ("launch", "Board"), "the navigation's row of the page is Board, no more Start"
    assert ui.PAGES[2:5] == (("sandboxes", "Sessions"), ("terminals", "Terminals"),
                             ("approvals", "Approvals")), "Ctrl+3 ... Ctrl+5 follow the order"


def test_the_names_in_german():
    try:
        set_language("de")
        assert tr("Project") == "Projekt" and tr("Board") == "Board" and tr("Sessions") == "Sitzungen"
    finally:
        set_language("en")


def test_the_navigation_shows_them(window):  # noqa: F811
    nav = window.nav
    texts = [nav.item(row).text().strip() for row in range(nav.count()) if not nav.isRowHidden(row)]
    start = texts.index("PROJECT")  # (a section's header in capitals)
    assert texts[start:start + 5] == ["PROJECT", "Board", "Sessions", "Terminals", "Approvals"], texts
    assert "Start" not in texts and "RUNNING" not in texts and "Sandboxes & sessions" not in texts
    go = {action.text() for action in window.actions.values()}
    assert {"Board", "Board: Board view", "Sessions"} <= go, go


from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests
