"""Right-click on a terminal tab -> "File manager" (user 2026-11-18): the Files page opens on the session's folder -
its workspace on this machine, or that folder among a host's sandboxes (over SSH) for a start on another host."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, files_page  # noqa: E402
from test_files_page import settle, setup  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME")


def test_show_start_on_this_machine_lists_the_workspace(setup, wait):
    page, ws, host, _ = setup
    page.left.go_path(str(host))
    settle(page.left)
    page.show_start("local", str(ws))
    assert wait(lambda: page.left.root is not None and os.path.realpath(str(page.left.root.path)) == str(ws), 10)


def test_show_start_opens_the_tabs_folder_also_when_the_sandboxes_list_does_not_name_it(setup, wait, tmp_path):
    """The user (2026-11-24): a tab's File manager did not open the tab's folder. A folder the sandboxes' list does
    not name (a session whose record names no workspace) showed the list's first entry - another sandbox's workspace."""
    page, _ws, host, _ = setup
    other = tmp_path / "other-ws"
    other.mkdir()
    (other / "x.txt").write_text("x")
    page.left.go_path(str(host))
    settle(page.left)
    page.show_start("local", str(other))
    assert wait(lambda: page.left.root is not None and os.path.realpath(str(page.left.root.path)) ==
                str(other.resolve()), 10)


def test_show_start_through_a_link_to_the_workspace(setup, wait, tmp_path):
    page, ws, host, _ = setup
    link = tmp_path / "linked"
    link.symlink_to(ws)
    page.left.go_path(str(host))
    settle(page.left)
    page.show_start("local", str(link))
    assert wait(lambda: page.left.root is not None and os.path.realpath(str(page.left.root.path)) == str(ws), 10)


def test_show_start_on_a_host_selects_that_sandbox_folder_once_the_host_listed_it(setup, monkeypatch):
    page, _ws, _host, _ = setup
    shown = []
    monkeypatch.setattr(page.left, "show_places", lambda select=None: shown.append(select))
    read = []
    monkeypatch.setattr(page, "read_host_folders", lambda: read.append(True))
    page.show_start("lab", "/home/me/proj")
    assert read and not [s for s in shown if s], "asked the host first"
    page.host_tokens["lab"] = "t"
    page.host_folders["lab"] = ("reading", [], None)
    page.host_folders_read("lab", "t", [("run", "Running sandbox x: workspace", "/home/me/proj")], None)
    assert files_page.host_folder("lab", "/home/me/proj") in shown
    # Listed already: selected at once.
    shown.clear()
    page.show_start("lab", "/home/me/proj")
    assert shown == [files_page.host_folder("lab", "/home/me/proj")]


def test_a_host_that_does_not_list_it_says_so(setup, monkeypatch):
    page, _ws, _host, _ = setup
    monkeypatch.setattr(page, "read_host_folders", lambda: None)
    page.show_start("lab", "/home/me/gone")
    page.host_tokens["lab"] = "t"
    page.host_folders["lab"] = ("reading", [], None)
    page.host_folders_read("lab", "t", [("run", "Running sandbox y: workspace", "/home/me/other")], None)
    assert "/home/me/gone" in page.message.text()


def test_the_terminals_tab_menu_offers_the_file_manager():
    from sandbox_ai_app import terminal
    asked = []

    class View:
        pass

    page = terminal.TerminalsPage.__new__(terminal.TerminalsPage)  # (only the menu's logic)
    instance = backend.Instance("session", "session:work", "work", project="/p", record={"workspace": "/p/ws"})
    page.files_request = asked.append
    entries = terminal.TerminalsPage.files_entry(page, instance)
    text, enabled, why, callback = entries
    assert text == "File manager" and enabled and why is None
    callback()
    assert asked == [instance]
    text, enabled, why, _ = terminal.TerminalsPage.files_entry(page, None)
    assert not enabled and why
