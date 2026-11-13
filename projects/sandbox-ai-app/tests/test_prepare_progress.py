"""Preparing a start in a terminal tab (user report 2026-11-12: "starting a clone of a session takes forever,
probably because something is built - that should be seen in the terminal"): what a start needs that takes minutes -
an image variant built, MCP servers added (their images pulled) - runs in a terminal tab of the app, each step said
with its number, its output as it comes (sandbox-ai's own progress lines) and how long it took; the start goes on at
its end. Without terminal tabs it runs in the background as before (the log window has its output)."""
import json
import subprocess
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import prerequisites, terminal, widgets  # noqa: E402


class FakeSai:
    def __init__(self, script, terminal_setting="app"):
        self.script, self.terminal_setting, self.ran = script, terminal_setting, []

    def settings(self):
        return {"terminal": self.terminal_setting}

    def command(self, *args):
        return [str(self.script), *args]

    def host_terminal(self, host, *args):
        return ["ssh", "-tt", host, "sandbox-ai", *args]

    def module(self, name):
        return type("Images", (), {"RECIPES": {"tor": {}}})

    def host_run(self, host, *args, timeout=None):
        self.ran.append((host, args))
        return 0, "done", ""


class Page(QtWidgets.QWidget, prerequisites.Prerequisites):
    def __init__(self, sai):
        super().__init__()
        self.sai, self.tabs, self.notes = sai, [], []

    def prerequisite_agent(self):
        return "claude"

    def prerequisite_argv(self):
        return []  # (what was made is there now: nothing more is needed)

    def host(self):
        return "local"

    def show_notice(self, text, role):
        self.notes.append((role, text))

    def in_terminal(self, argv, title, embedded=None, ended=None):
        self.tabs.append((argv, title, embedded, ended))
        return True


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))

    def at_once(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            return done(None, exc)
        return done(result, None)
    monkeypatch.setattr(prerequisites, "background", at_once)
    script = tmp_path / "sandbox-ai"
    script.write_text("#!/bin/sh\necho \"agent-sandbox: $* working\"\n[ \"$1\" != fail ]\n")
    script.chmod(0o755)
    view = Page(FakeSai(script))
    yield view
    view.deleteLater()


def test_the_slow_part_runs_in_a_terminal_tab_and_the_start_goes_on_at_its_end(page):
    started = []
    page.provide({"--mcp": ["fetch"], "--identity": ["work"]}, ["tor"], "local", ("local", "{}"),
                 lambda: started.append(True), "start", context="local", agent="claude")
    # The quick part (an identity) here, the builds and MCP servers in the tab - not in the background.
    assert [args[:2] for _, args in page.sai.ran] == [("identities", "add")]
    assert len(page.tabs) == 1 and started == []
    argv, title, embedded, ended = page.tabs[0]
    assert title == "Preparing the start" and embedded is True and argv[:3] == [sys.executable, "-I", "-c"]
    steps = json.loads(argv[-1])
    assert steps == [["building the image variant tor", [str(page.sai.script), "build", "--variant", "tor"]],
                     ["adding the MCP server fetch (its image is pulled)", [str(page.sai.script), "mcp", "add",
                                                                             "fetch"]]]
    # What the tab shows: each step numbered, the command's own lines, how long it took, the end.
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    lines = done.stdout.splitlines()
    assert done.returncode == 0
    assert lines[0] == "agent-sandbox: preparing the start (1/2): building the image variant tor…"
    assert lines[1] == "agent-sandbox: build --variant tor working"
    assert lines[2].startswith("agent-sandbox: building the image variant tor: done in ")
    assert lines[3] == "agent-sandbox: preparing the start (2/2): adding the MCP server fetch (its image is pulled)…"
    assert lines[-1].startswith("agent-sandbox: prepared in ") and lines[-1].endswith("; the start goes on")
    ended(0)
    assert started == [True], "the start goes on once the tab says it is prepared"
    assert ("notice-ok", "Made for the start: identity work, built tor, MCP server fetch.") in page.notes


def test_a_failed_step_stops_there_and_the_start_waits(page, monkeypatch):
    started = []
    monkeypatch.setattr(page.sai, "command", lambda *args: [str(page.sai.script), "fail", *args])
    page.provide({"--mcp": ["fetch"]}, ["tor"], "local", ("local", "{}"), lambda: started.append(True), "start",
                 context="local", agent="claude")
    argv, _, _, ended = page.tabs[0]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "building the image variant tor failed (exit status 1)" in done.stdout
    assert "(2/2)" not in done.stdout, "nothing after the step that failed"
    ended(done.returncode)
    assert started == [] and ("notice-error", "Not everything could be made for the start.") in page.notes


def test_without_terminal_tabs_it_runs_in_the_background_as_before(page, monkeypatch):
    monkeypatch.setattr(page.sai, "terminal_setting", "window")
    started = []
    page.provide({"--mcp": ["fetch"]}, ["tor"], "local", ("local", "{}"), lambda: started.append(True), "start",
                 context="local", agent="claude")
    assert page.tabs == [] and [args[:2] for _, args in page.sai.ran] == [("build", "--variant"), ("mcp", "add")]
    assert started == [True]


def test_closing_the_tab_before_its_end_tells_the_end_and_frees_the_start(qapp, tmp_path, wait, monkeypatch):
    """The tab Preparing the start closed while its steps ran (a build that hangs, a change of mind): its end was never
    told - the tab's exit signal went with the tab - so the start stayed "being prepared" and every later Start said
    "Still preparing what the start needs…" until the app was restarted. ENDED gets the command's exit status from its
    terminal bridge, which outlives the tab until the command is gone."""
    import shutil
    import types
    from pathlib import Path
    from sandbox_ai_app import terminal as T, ui
    if T.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {T.MISSING}")
    monkeypatch.setattr(T, "available", lambda web=None: (True, ""))
    here = Path(__file__).resolve().parent
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(here.parent / "sandbox_ai_app" / "web" / name, web / name)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(here / "fake_xterm" / name, web / "vendor" / name)
    terminals = T.TerminalsPage(web)
    window = types.SimpleNamespace(sai=types.SimpleNamespace(settings=lambda: {"terminal": "app"}),
                                   terminals=terminals, show_page=lambda key: None, own_starts=[], project=None)
    told, views = [], []
    original = terminals.make_tab
    monkeypatch.setattr(terminals, "make_tab", lambda *a, **k: views.append(original(*a, **k)) or views[-1])
    assert ui.MainWindow.in_terminal(window, ["sh", "-c", "sleep 60"], "Preparing the start", embedded=True,
                                     ended=told.append)
    assert wait(lambda: views, 30), "the tab never came"
    views[0].bridge.kill_after = 300
    terminals.close_tab(0, ask=False)
    views.clear()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # the tab goes, as the app's loop deletes it
    assert wait(lambda: told, 10), "the end of the closed tab's command was never told"
    assert told[0] != 0
    terminals.deleteLater()
