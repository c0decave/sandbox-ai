"""Copy relative path and Copy path in the sandbox (files_page.py, backend.sandbox_paths): where the launcher puts a
sandbox's workspaces (/work, /workspaces/NAME) and HOME (/root), the forms of a path in either pane, one path a line,
quoted for the shell, the menu and Ctrl+Shift+C. Everything the tests change is below tmp_path."""
import json
import os

import pytest

from sandbox_ai_app import backend
from test_backend import fake_run, sai  # noqa: F401


def test_workspaces_land_where_the_launcher_mounts_them(tmp_path):
    a, b, c, d = (tmp_path / name for name in ("proj", "lib", "x/lib", "named"))
    targets = backend.sandbox_targets([a, b, c, d], {os.path.realpath(d): "docs"})
    assert targets == [(str(a), "/work"), (str(b), "/workspaces/lib"), (str(c), "/workspaces/lib-2"),
                       (str(d), "/workspaces/docs")]
    assert backend.sandbox_targets([]) == [] and backend.sandbox_targets([None, a]) == [(str(a), "/work")]
    paths = backend.paths_from_options(None, ["-w", "/p,/q", "--workspace-name", "docs=/q"], "/base")
    assert paths["workspace_names"] == {"/q": "docs"}


def test_sandbox_paths_of_runs_and_sessions(sai, tmp_path):  # noqa: F811
    fake_run(tmp_path, workspace=tmp_path / "project", session=None)
    session = tmp_path / "state" / "sandbox-ai" / "sessions" / "work"
    (session / "home").mkdir(parents=True)
    for name in ("app", "lib", "docs"):
        (tmp_path / name).mkdir()
    (session / "session.json").write_text(json.dumps({
        "agent": "codex", "backend": "podman", "cwd": str(tmp_path), "created": "2026-11-03T10:00:00Z",
        "options": ["-w", f"{tmp_path / 'app'},{tmp_path / 'lib'},{tmp_path / 'docs'}",
                    "--workspace-name", f"manual={tmp_path / 'docs'}"],
        "workspace": str(tmp_path / "app")}))
    found = sai.sandbox_paths()
    assert found[os.path.realpath(tmp_path / "project")] == "/work"
    assert found[os.path.realpath(tmp_path / "app")] == "/work"
    assert found[os.path.realpath(tmp_path / "lib")] == "/workspaces/lib"
    assert found[os.path.realpath(tmp_path / "docs")] == "/workspaces/manual"
    assert found[os.path.realpath(session / "home")] == "/root"


# -- the page --

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from test_files_page import press, select, settle, setup  # noqa: E402,F401


def clipboard():
    return QGuiApplication.clipboard().text()


def test_relative_and_in_sandbox_paths_in_both_panes(setup, wait):
    page, ws, host, _ = setup
    (ws / "sub" / "my file's.txt").write_text("x")
    page.sandbox_paths = {str(ws): "/work"}
    left = page.left
    left.go("sub")
    select(left, "b.txt", "my file's.txt")
    assert page.copy_path(left, form="relative") == "sub/b.txt\nsub/my file's.txt", "one path a line, plain"
    assert clipboard() == "sub/b.txt\nsub/my file's.txt"
    assert page.copy_path(left, form="sandbox") == "/work/sub/b.txt\n/work/sub/my file's.txt"
    assert page.copy_path(left, form="sandbox", quoted=True) == "/work/sub/b.txt\n'/work/sub/my file'\"'\"'s.txt'"
    assert page.copy_path(left) == f"{ws}/sub/b.txt\n{ws}/sub/my file's.txt"
    left.table.clearSelection()
    assert page.copy_path(left, form="relative") == "sub" and page.copy_path(left, form="sandbox") == "/work/sub"
    # A HOME: relative to it, /root in the sandbox.
    page.sandbox_paths = {str(ws): "/root"}
    assert page.copy_path(left, [left.by_name["b.txt"]], "sandbox") == "/root/sub/b.txt"
    # Not a running sandbox's folder (a chosen one): relative to the pane's folder, no path in the sandbox.
    page.sandbox_paths = {}
    assert page.copy_path(left, [left.by_name["b.txt"]], "relative") == "sub/b.txt"
    assert page.copy_path(left, [left.by_name["b.txt"]], "sandbox") is None and "no path in the sandbox" in \
        page.message.text()
    # The host pane: a folder that lies in a running sandbox's workspace has its sandbox path too.
    page.sandbox_paths = {str(ws): "/work"}
    right = page.right
    right.go_path(str(ws / "sub"))
    settle(right)
    select(right, "b.txt")
    assert page.copy_path(right, form="sandbox") == "/work/sub/b.txt" and page.copy_path(right, form="relative") == \
        "sub/b.txt"
    right.go_path(str(host))
    settle(right)
    (host / "h.txt").write_text("h")
    right.list()
    select(right, "h.txt")
    assert page.copy_path(right, form="relative") == "h.txt", "out of every sandbox: relative to the folder shown"
    assert page.copy_path(right, form="sandbox") is None


def test_the_menu_and_ctrl_shift_c(setup, wait):
    page, ws, host, _ = setup
    page.sandbox_paths = {str(ws): "/work"}
    left = page.left
    left.go("sub")
    select(left, "b.txt")
    menu = page.menu(left)
    actions = {action.text(): action for action in menu.actions() if action.text()}
    assert actions["Copy relative path"].shortcut().toString() == "Ctrl+Shift+C"
    assert actions["Copy path in the sandbox"].isEnabled()
    quoted = actions["Copy quoted for the shell"].menu()
    assert [a.text() for a in quoted.actions()] == ["Path", "Relative path", "Path in the sandbox"]
    actions["Copy path in the sandbox"].trigger()
    assert clipboard() == "/work/sub/b.txt"
    quoted.actions()[1].trigger()
    assert clipboard() == "sub/b.txt"
    QGuiApplication.clipboard().setText("")
    press(left, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert clipboard() == "sub/b.txt", "Ctrl+Shift+C: the relative path"
    assert page.is_key(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert not page.is_key(Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier), \
        "Ctrl+Shift+X stays the window's emergency stop"
    page.sandbox_paths = {}
    menu = page.menu(left)
    actions = {action.text(): action for action in menu.actions() if action.text()}
    assert not actions["Copy path in the sandbox"].isEnabled()
