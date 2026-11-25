"""The menu Install (todos 2026-11-24): a checklist of what starts need - images, MCP servers, toolchains, what presets
need - on this machine or a host, with state and size, All / None, and Install…, which runs `sandbox-ai install
ITEM...` there in a terminal tab (each item's own command and checks; a pending MCP server's build asked there)."""
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from sandbox_ai_app import backend, install_view as IV, widgets  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the isolated home and the window of the UI tests

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

LISTED = {"items": [
    {"id": "image:claude", "kind": "images", "name": "claude", "title": "Claude Code's image", "installed": True,
     "size": 2_000_000_000, "note": "", "pending": False, "all": True, "command": ["build", "claude"]},
    {"id": "image:codex", "kind": "images", "name": "codex", "title": "Codex's image", "installed": False,
     "size": None, "note": "a build of several minutes", "pending": False, "all": True, "command": ["build", "codex"]},
    {"id": "mcp:duckduckgo", "kind": "mcp", "name": "duckduckgo", "title": "DuckDuckGo", "installed": False,
     "size": None, "note": "an image pulled by its digest", "pending": False, "all": True, "command": []},
    {"id": "mcp:hub:nmap", "kind": "mcp", "name": "hub:nmap", "title": "Nmap", "installed": False, "size": None,
     "note": "pending", "pending": True, "all": False, "command": []},
    {"id": "mcp:brave-search", "kind": "mcp", "name": "brave-search", "title": "Brave", "installed": False,
     "size": None, "note": "needs BRAVE_API_KEY", "pending": False, "all": False, "command": []},
    {"id": "toolchain:rust", "kind": "toolchains", "name": "rust", "title": "Rust 1.99.1", "installed": False,
     "size": 150_935_140, "note": "a download", "pending": False, "all": True, "command": []},
    {"id": "preset:rust", "kind": "presets", "name": "rust", "title": "Rust", "installed": False,
     "size": 150_935_140, "note": "", "pending": False, "all": True, "command": [],
     "needs": ["image:claude", "toolchain:rust"]}], "problems": ["MCP catalogs: one catalog not read"]}


class FakeSai:
    def __init__(self, answer=None):
        self.asked, self.answer = [], answer or (0, json.dumps(LISTED), "")

    def host_run(self, host, *args, timeout=120):
        self.asked.append((host, *args))
        return self.answer

    def command(self, *args):
        return ["python3", "-I", "main.py", *args]

    def host_terminal(self, host, *args):
        return ["ssh", "-tt", host, "sandbox-ai", *args]

    def hosts_supported(self):
        return True

    def host_entries(self):
        return [{"name": "build-box", "kind": "ssh", "ssh": "me@build.example.org"}]


def wait_for(qapp, condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def made(qapp, sai=None, **kwargs):
    ran = []
    dialog = IV.InstallDialog(None, sai or FakeSai(), lambda argv, title, ended: ran.append((argv, title, ended)),
                              **kwargs)
    assert wait_for(qapp, lambda: dialog.reading is None and dialog.rows)
    return dialog, ran


def texts(dialog):
    found = {}
    for item, row in dialog.items():
        found[row["id"]] = [item.text(c) for c in range(4)] + [bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)]
    return found


def test_the_checklist_shows_state_size_and_what_presets_need(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    dialog, _ = made(qapp)
    rows = texts(dialog)
    assert rows["image:claude"][1:3] == ["installed", "2.0 GB"] and rows["image:claude"][4] is False
    assert rows["image:codex"][1] == "not installed" and rows["image:codex"][4] is True
    assert rows["mcp:hub:nmap"][1] == "pending (built after a question)"
    assert rows["toolchain:rust"][2] == "150.9 MB"
    assert rows["preset:rust"][3] == "needs image:claude, toolchain:rust"
    groups = [dialog.tree.topLevelItem(i).text(0) for i in range(dialog.tree.topLevelItemCount())]
    assert groups == ["Agent images", "MCP servers", "Toolchains", "What presets need"]
    assert dialog.tree.topLevelItem(0).text(1) == "1 installed of 2"
    assert "one catalog not read" in dialog.status.text()
    assert not dialog.install_button.isEnabled() and dialog.install_button.text() == "Install…"
    dialog.deleteLater()


def test_all_none_and_install_run_sandbox_ai_install_in_a_terminal(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    dialog, ran = made(qapp)
    dialog.all_button.click()
    assert dialog.chosen() == ["image:codex", "mcp:duckduckgo", "toolchain:rust"], \
        "All: what --all takes - never a pending server, one that needs a secret, a preset as such"
    assert dialog.install_button.text() == "Install 3…"
    dialog.none_button.click()
    assert dialog.chosen() == [] and not dialog.install_button.isEnabled()
    for item, row in dialog.items():
        if row["id"] in ("mcp:hub:nmap", "preset:rust"):
            item.setCheckState(0, Qt.CheckState.Checked)
    dialog.install_button.click()
    (argv, title, ended), = ran
    assert argv == ["python3", "-I", "main.py", "install", "mcp:hub:nmap", "preset:rust"]
    assert title == "Install on local (this machine)"
    question = widgets.SHOWN[-1][1]
    assert "• mcp:hub:nmap" in question and "only after its own question in the terminal" in question
    dialog.deleteLater()


def test_on_a_host_it_reads_and_installs_there(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    sai = FakeSai()
    dialog, ran = made(qapp, sai, host="build-box", every=True)
    assert sai.asked[0] == ("build-box", "install", "--list", "--json")
    assert dialog.chosen() == ["image:codex", "mcp:duckduckgo", "toolchain:rust"], "Everything signed…: ticked"
    dialog.install_button.click()
    assert ran[0][0] == ["ssh", "-tt", "build-box", "sandbox-ai", "install", "image:codex", "mcp:duckduckgo",
                         "toolchain:rust"]
    dialog.deleteLater()


def test_a_host_without_the_command_says_so(qapp, monkeypatch):
    sai = FakeSai(answer=(2, "", "sandbox-ai: argument COMMAND: invalid choice: 'install'"))
    dialog = IV.InstallDialog(None, sai, lambda *a: None, host="build-box")
    assert wait_for(qapp, lambda: dialog.reading is None)
    assert "has no install command yet: update it" in dialog.status.text() and dialog.rows == []
    dialog.deleteLater()


def test_the_menu_bar_has_install_with_its_entries(window):  # noqa: F811
    menus = {action.text(): action.menu() for action in window.menuBar().actions() if action.menu() is not None}
    assert "&Install" in menus
    entries = [a.text() for a in menus["&Install"].actions() if not a.isSeparator()]
    assert entries == ["Install…", "Agent images…", "MCP servers…", "Toolchains…", "What presets need…",
                       "Everything signed…"]
    window.actions["install:toolchains"].trigger()
    dialog = window.install_dialog
    assert isinstance(dialog, IV.InstallDialog) and dialog.kind == "toolchains" and dialog.isVisible()
    dialog.close()
