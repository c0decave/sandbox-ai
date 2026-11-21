"""Right-click on a terminal tab -> "Open shell" (user 2026-11-19): a shell in that session's sandbox, under the
agent's rules (`sandbox-ai shell NAME`, on another host over SSH) - as the Sandboxes page's Shell; only while it runs."""
import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, terminal  # noqa: E402


def page_with(asked):
    page = terminal.TerminalsPage.__new__(terminal.TerminalsPage)  # (only the menu's logic)
    page.shell_request = asked.append
    return page


def test_a_running_session_offers_a_shell():
    asked = []
    page = page_with(asked)
    instance = backend.Instance("session", "session:work", "work", project="/p", state="running")
    text, enabled, why, callback = terminal.TerminalsPage.shell_entry(page, instance)
    assert text == "Open shell" and enabled and why is None
    callback()
    assert asked == [instance]


def test_no_shell_for_a_stopped_or_unknown_session():
    page = page_with([])
    stopped = backend.Instance("session", "session:work", "work", project="/p", state="paused")
    for instance in (stopped, None):
        _text, enabled, why, _ = terminal.TerminalsPage.shell_entry(page, instance)
        assert not enabled and why


def test_the_entry_is_in_the_tab_menu_after_the_file_manager():
    import inspect
    source = inspect.getsource(terminal.TerminalsPage.tab_actions)
    assert source.index("self.files_entry(instance)") < source.index("self.shell_entry(instance)")
