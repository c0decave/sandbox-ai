"""The navigation's order under Running (user 2026-11-18): Sandboxes & sessions, Terminals, Approvals - the
terminals right after the sessions they belong to; Ctrl+3, Ctrl+4 and Ctrl+5 open them in that order."""
import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import ui  # noqa: E402


def test_running_lists_sessions_then_terminals_then_approvals():
    running = dict(ui.SECTIONS)["Running"]
    assert [key for key, _ in running] == ["sandboxes", "terminals", "approvals"]
    assert ui.PAGES[2:5] == (("sandboxes", "Sandboxes & sessions"), ("terminals", "Terminals"),
                             ("approvals", "Approvals")), "Ctrl+3 ... Ctrl+5 follow the order"
