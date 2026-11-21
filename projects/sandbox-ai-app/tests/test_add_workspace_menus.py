"""A folder for a running session from where the human is (user 2026-11-19): the Files page's right click "Add to a
running session…" (this machine's folder for its sessions, a sandbox-ai host's folder for that host's sessions, another
machine's folder over sshfs for this machine's), and a terminal tab's right click "Add folder…" - both the Sandboxes
page's Add workspace… (asked first there), the path filled in."""
import types

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, files_page, remote, terminal, ui, widgets  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures


def instance(name, run=None, host="local", state="running", kind="session"):
    return backend.Instance(kind, f"{host}:{name}", name, run=run, state=state, host=host)


# -- the terminal tab's Add folder…


def test_a_running_sessions_tab_offers_add_folder():
    asked = []
    page = terminal.TerminalsPage.__new__(terminal.TerminalsPage)
    page.workspace_request = asked.append
    running = instance("work", run="claude-1")
    text, enabled, why, callback = terminal.TerminalsPage.workspace_entry(page, running)
    assert text == "Add folder…" and enabled and why is None
    callback()
    assert asked == [running]
    for gone in (instance("work", run="claude-1", state="paused"), None):
        assert not terminal.TerminalsPage.workspace_entry(page, gone)[1]


def test_add_folder_opens_add_workspace_for_that_session(window, monkeypatch):  # noqa: F811
    called = []
    monkeypatch.setattr(window.sandboxes, "add_workspace_for", lambda host, run, path=None: called.append((host, run, path)))
    assert window.add_folder_to(instance("web", run="claude-abc"))
    assert window.add_folder_to(instance("lab-run", host="lab"))
    assert called == [("local", "claude-abc", None), ("lab", "lab-run", None)]
    monkeypatch.setattr(ui, "inform", lambda *a, **k: called.append("said"))
    assert not window.add_folder_to(instance("web", run="claude-abc", state="stopped"))
    assert called[-1] == "said"


# -- the Files page's Add to a running session…


def pane_of(root, path):
    return types.SimpleNamespace(remote=lambda: getattr(root, "remote", False), root=root,
                                 path_text=lambda: path, path_of_entry=lambda e: f"{path}/{e.name}")


def test_where_a_folder_goes_this_machine_a_host_another_machine(monkeypatch):
    page = files_page.FilesPage.__new__(files_page.FilesPage)
    local = pane_of(types.SimpleNamespace(remote=False), "/home/me/proj")
    entry = types.SimpleNamespace(name="data")
    assert files_page.FilesPage.workspace_source(page, local, entry) == ("local", "/home/me/proj/data")
    assert files_page.FilesPage.workspace_source(page, local) == ("local", "/home/me/proj")
    other = types.SimpleNamespace(remote=True, target=remote.Target("nas", "me", 2222))
    monkeypatch.setattr(files_page.FilesPage, "sandbox_host_name", lambda self, root: None)
    assert files_page.FilesPage.workspace_source(page, pane_of(other, "/srv/x"), entry) == \
        ("local", "sshfs://me@nas:2222/srv/x/data"), "another machine's: over sshfs, for this machine's sessions"
    monkeypatch.setattr(files_page.FilesPage, "sandbox_host_name", lambda self, root: "lab")
    assert files_page.FilesPage.workspace_source(page, pane_of(other, "/srv/x")) == ("lab", "/srv/x"), \
        "a host's folder: for that host's sessions, as its path there"


def test_the_window_offers_the_hosts_running_sessions_and_fills_in_the_path(window, monkeypatch):  # noqa: F811
    called = []
    monkeypatch.setattr(window.sandboxes, "add_workspace_for",
                        lambda host, run, path=None: called.append((host, run, path)))
    monkeypatch.setattr(window.sandboxes, "instances", {
        "a": instance("one", run="claude-1"), "b": instance("two", run="claude-2"),
        "c": instance("old", run="claude-3", state="stopped"), "d": instance("there", host="lab")})
    asked = []

    def get_item(parent, title, label, items, current, editable):
        asked.append(list(items))
        return items[1], True
    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem", staticmethod(get_item))
    assert window.add_workspace_from("local", "/home/me/data")
    assert asked == [["claude-1", "claude-2"]], "only this machine's running sessions"
    assert called == [("local", "claude-2", "/home/me/data")]
    assert window.add_workspace_from("lab", "/srv/x")  # one there: no question
    assert called[-1] == ("lab", "there", "/srv/x") and len(asked) == 1
    said = []
    monkeypatch.setattr(ui, "inform", lambda *a, **k: said.append(a))
    assert not window.add_workspace_from("nowhere", "/x") and said


def test_the_dialog_is_filled_in_with_the_folder(window, monkeypatch):  # noqa: F811
    seen = []

    def exec_dropped(dialog, *a, **k):
        seen.append(dialog.edit.text())
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(widgets, "exec_dropped", exec_dropped)
    window.sandboxes.workspace_dialog("local", "claude-1", [], "/home/me/data")
    assert seen == ["/home/me/data"]
