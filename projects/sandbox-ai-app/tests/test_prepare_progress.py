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
from PySide6 import QtCore  # noqa: E402
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


def test_the_vpn_question_shows_a_profile_name_as_it_is(qapp, monkeypatch):
    """Code review 0.1.95 (trust boundary): the name of a missing VPN profile comes from the board, a preset or a
    project's .sandbox-ai.json - text from outside the app; the question about it was a QMessageBox in Qt's AutoText,
    so a name such as "<b>home</b>" was drawn as markup. It is plain text now."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", None)
    asked = []

    def answer(box, *args):
        asked.append((box.textFormat(), box.text()))
        return 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", answer)
    page = Page(FakeSai("x"))
    assert page.ask_vpn_kind("<b>home</b>", "ct1") is None
    assert asked and asked[0][0] == QtCore.Qt.TextFormat.PlainText and "<b>home</b>" in asked[0][1]
    page.deleteLater()


# --- what is prepared and how far (user report 2026-11-14: "15:19:11 WARNING board: Was der Start braucht, wird noch
# vorbereitet…" - "da sollte stehen was noch vorbereitet wird und Progress") ------------------------------------------

def sandbox_ai_progress():
    """sandbox-ai's own progress.py (what `sandbox-ai build` prints its steps with)."""
    import importlib.util
    import os
    from pathlib import Path
    home = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
    spec = importlib.util.spec_from_file_location("sandbox_ai_progress", home / "sandbox/progress.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_steps_sandbox_ai_build_prints_are_read():
    """The stage and step of a build as sandbox-ai's progress.py says them (its Output of podman's build lines): the
    app reads its own format back, not a guess at it."""
    from sandbox_ai_app import preparing
    progress = sandbox_ai_progress()
    lines = []
    output = progress.Output("building the image localhost/qwen-sandbox:latest",
                             progress.Progress(out=lines.append, prefix="sandbox-ai build"))
    output.line("[2/2] STEP 9/14: RUN apt-get install -y ripgrep")
    found = preparing.parse(lines[-1])
    assert found["stage"] == (2, 2) and found["step"] == (9, 14), lines
    assert found["text"].startswith("RUN apt-get install")
    assert preparing.fraction(found["stage"], found["step"]) == pytest.approx((1 + 8 / 14) / 2)
    output.line("STEP 3/4: COPY x y")
    assert preparing.parse(lines[-1])["stage"] is None and preparing.parse(lines[-1])["step"] == (3, 4)
    assert preparing.parse("sandbox-ai build: qwen -> 0.26.4 (pinned)") is None


def four_items():
    from sandbox_ai_app import preparing as P
    prep = P.Preparation([P.Item("identity", "work"), P.Item("build", "tor", in_tab=True),
                          P.Item("mcp", "exiftool", in_tab=True), P.Item("vpn", "tor-net")], clock_=lambda: 100.0)
    prep.start(0, now=10)
    prep.finish(0, True, now=11)
    prep.start(1, now=100)
    prep.line(1, "sandbox-ai build: building the image localhost/claude-sandbox-tor:latest (step 9/14): RUN x…")
    return prep


def test_the_line_says_what_is_prepared_and_how_far():
    from sandbox_ai_app import i18n
    prep = four_items()
    assert prep.summary(now=172) == ("Preparing (2 of 4): building the image variant tor (step 9/14, 1:12) — next: "
                                     "MCP server exiftool, Tor profile tor-net")
    assert prep.summary(now=172, timed=False) == ("Preparing (2 of 4): building the image variant tor — next: MCP "
                                                  "server exiftool, Tor profile tor-net")
    assert prep.overall() == pytest.approx((1 + 8 / 14) / 4)
    i18n.set_language("de")
    try:
        assert prep.summary(now=172) == ("Vorbereitung (2 von 4): die Image-Variante tor wird gebaut (Schritt 9/14, "
                                         "1:12) — danach: MCP-Server exiftool, Tor-Profil tor-net")
    finally:
        i18n.set_language("en")
    prep.finish(1, False, status=1, now=180)
    assert prep.summary(now=181) == "Not prepared (2 of 4): building the image variant tor failed (exit status 1)."


def test_the_terminal_tab_writes_what_runs_and_how_far_to_its_status_file(page, tmp_path):
    """CHAIN (the tab Preparing the start) passes each step's output through as before and writes what it sees -
    the step that runs, its stage and step - to the status file the app reads."""
    from sandbox_ai_app import preparing as P
    script = tmp_path / "builder"
    script.write_text("#!/bin/sh\necho 'sandbox-ai build: building the image localhost/x:latest (stage 2/2, step "
                      "9/14): RUN make…'\necho '  compiling'\n[ \"$1\" != fail ]\n")
    script.chmod(0o755)
    status = tmp_path / "status.json"
    steps = [["building the image variant tor", [str(script), "ok"]],
             ["adding the MCP server x", [str(script), "fail"]]]
    argv = P.chain_status_argv([sys.executable, "-I", "-c", prerequisites.CHAIN, json.dumps(steps)], status)
    assert json.loads(argv[-1]) == steps, "the steps stay the last argument"
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert done.returncode == 1
    assert "(stage 2/2, step 9/14): RUN make" in done.stdout and "  compiling" in done.stdout, done.stdout
    data = json.loads(status.read_text())
    assert [s["state"] for s in data["steps"]] == ["done", "failed"] and data["steps"][1]["status"] == 1
    assert "step 9/14" in data["steps"][0]["mark"] and data["steps"][0]["last"] == "compiling"
    prep = P.Preparation([P.Item("build", "tor", in_tab=True), P.Item("mcp", "x", in_tab=True)])
    prep.merge(data)
    assert prep.items[0].step == (9, 14) and prep.items[0].stage == (2, 2) and prep.failed() is prep.items[1]


def test_in_a_terminal_its_steps_keep_a_terminal(tmp_path):
    """In the tab (a terminal) each step still runs on a terminal of its own (a pseudo-terminal: its questions, its
    colours, podman's way of printing), and the status file is written all the same."""
    import os
    import pty
    import time
    from sandbox_ai_app import preparing as P
    script = tmp_path / "builder"
    script.write_text("#!/bin/sh\n[ -t 1 ] && echo 'on a terminal'\necho 'x: building (step 2/3): RUN y…'\n")
    script.chmod(0o755)
    status = tmp_path / "status.json"
    argv = P.chain_status_argv([sys.executable, "-I", "-c", prerequisites.CHAIN,
                                json.dumps([["building", [str(script)]]])], status)
    fd, terminal_end = pty.openpty()  # (the tab's terminal)
    process = subprocess.Popen(argv, stdin=terminal_end, stdout=terminal_end, stderr=terminal_end,
                               start_new_session=True)
    os.close(terminal_end)
    out, deadline = b"", time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        out += data
    os.close(fd)
    assert process.wait(timeout=30) == 0, out
    assert b"on a terminal" in out and b"prepared in" in out, out
    step = json.loads(status.read_text())["steps"][0]
    assert step["state"] == "done" and "step 2/3" in step["mark"]


def test_start_again_while_preparing_says_what_runs_as_information(page, monkeypatch):
    """User: 'da sollte stehen was noch vorbereitet wird und Progress' - a second Start while the start is prepared
    says what is being made now and what comes next, as information (not a warning), not the bare "still being
    prepared"."""
    page.sai.host_image_variants = lambda host: {}
    page.sai.host_json = lambda host, *a, **k: [{"target": "tor", "agent": "claude", "variant": True,
                                                 "present": False}]
    page.prerequisite_argv = lambda: ["--image-variant", "tor"]
    started = []
    assert page.check_prerequisites(then=lambda: started.append(1)) is True
    (argv, title, embedded, ended), = page.tabs
    page.notes.clear()
    assert page.check_prerequisites(then=lambda: started.append(2)) is True
    role, text = page.notes[-1]
    assert role == "secondary", page.notes
    assert text.startswith("Still preparing (1 of 1): building the image variant tor"), text
    assert "Still preparing what the start needs" not in text
    page.prerequisite_argv = lambda: []  # (built: nothing is needed any more)
    ended(0)
    assert started == [1]


def test_a_check_that_keeps_coming_back_is_stopped_and_says_what(page, monkeypatch):
    """User log: a stream of `background call='Prerequisites.check_prerequisites.<locals>.read'` every 0.15 s and
    'ich drücke auf Starten und nichts passiert und keine Infos'. Whatever makes the check come back again and again,
    it is stopped after LOOP_LIMIT checks within LOOP_SECONDS, with an error that names what it was about."""
    page.sai.host_image_variants = lambda host: {}
    reads = []
    page.sai.identities = lambda: reads.append(1) or {"work": {}}
    page.prerequisite_argv = lambda: ["--identity", "work"]

    def again():  # (a bug that forgets what was checked and checks again at once, as 0.1.95 did)
        page.prerequisites_done = None
        page.check_prerequisites(then=again)
    page.check_prerequisites(then=again)
    assert len(reads) == prerequisites.LOOP_LIMIT, len(reads)
    role, text = page.notes[-1]
    assert role == "notice-error" and "identity work" in text and "stopped" in text, page.notes
    # Later, a Start of the human's own checks again.
    monkeypatch.setattr(prerequisites.time, "monotonic", lambda: 10 ** 9)
    page.prerequisites_done = None
    ran = []
    page.check_prerequisites(then=lambda: ran.append(1))
    assert ran == [1] and len(reads) == prerequisites.LOOP_LIMIT + 1


def test_every_check_says_what_the_start_needs_what_is_there_and_what_is_missing(page, monkeypatch):
    """'was der Start braucht… ja was braucht er denn???': each check's result is told in plain words at INFO - what
    the start needs, what is there, what is missing - once per change."""
    from sandbox_ai_app import applog
    logged = []
    monkeypatch.setattr(applog, "log", lambda level, where, text: logged.append((level, text)))
    page.sai.host_image_variants = lambda host: {}
    page.sai.identities = lambda: {"work": {}}
    page.sai.host_vpn_profiles = lambda host: []
    page.sai.host_mcp_servers = lambda host: [("fetch",)]
    page.prerequisite_argv = lambda: ["--identity", "work", "--vpn", "home", "--mcp", "fetch"]
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # (the VPN profile is made on the VPN page)
    for _ in range(2):
        page.check_prerequisites(then=lambda: None)
    said = [text for level, text in logged if text.startswith("The start needs")]
    assert len(said) == 1, logged
    assert all(level == "INFO" for level, text in logged if text.startswith("The start needs"))
    assert said[0] == ("The start needs on local: identity work, VPN profile home, MCP server fetch. There: identity "
                       "work, MCP server fetch. Missing: VPN profile home."), said[0]


def test_a_progress_file_nested_deeper_than_python_reads_is_not_read(tmp_path):
    """Stability (the deep-nesting guard): the terminal tab's progress file, damaged into a document nested deeper
    than json reads, raises RecursionError, not ValueError - read() says nothing was read, as for any damaged file,
    instead of raising in the Board's timer."""
    from sandbox_ai_app.preparing import Item, Preparation
    prep = Preparation([Item("image", "claude", in_tab=True)])
    prep.status = tmp_path / "progress.json"
    prep.status.write_text("[" * 200_000)
    assert prep.read() is False
    prep.status.write_text(json.dumps({"steps": []}))
    assert prep.read() is True
