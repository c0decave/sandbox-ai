"""What the user ran into in live use of the app, end to end: the app's own pages and backend running the real
sandbox-ai of SANDBOX_AI_HOME (its dry runs, builds, listings, catalogs) as subprocesses, with only Podman, tmux and npm
faked on PATH (sandbox-ai's tests/live_fakes.py: a per-user image store, as rootless Podman keeps one). One test per
report, named after it, its error line quoted."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, prerequisites, terminal, widgets  # noqa: E402

HOME = os.environ.get("SANDBOX_AI_HOME")
pytestmark = pytest.mark.skipif(not HOME, reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
# A private preset folder with the Tor presets the user ran (tor-researcher, tor-research-v2): only where it is given.
PRIVATE_PRESETS = Path(os.environ.get("SANDBOX_AI_PRIVATE_PRESETS") or "/nonexistent")


def live_fakes():
    spec = importlib.util.spec_from_file_location("live_fakes_for_app", Path(HOME) / "tests/live_fakes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def at_once(function, *args, done, **kwargs):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        return done(None, exc)
    return done(result, None)


@pytest.fixture
def user(qapp, tmp_path, monkeypatch):
    """The app run by an ordinary user: their HOME, XDG folders and own Podman image store, the fakes on PATH; the
    app's calls of sandbox-ai run the real one."""
    host = live_fakes().Host(tmp_path)
    for name in ("SANDBOX_AI_NO_AUTO_BUILD", "SANDBOX_AI_MCP_NO_IMAGE_CHECK"):
        monkeypatch.delenv(name, raising=False)
    for name, value in host.env.items():
        if name.startswith(("FAKE_", "XDG_", "SANDBOX_AI_BUILD")) or name in ("HOME", "PATH"):
            monkeypatch.setenv(name, value)
    monkeypatch.chdir(host.workspace)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(prerequisites, "background", at_once)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    return host


@pytest.fixture
def board_page(user, monkeypatch):
    from sandbox_ai_app import board_view
    from sandbox_ai_app.board_view import BoardPage
    monkeypatch.setattr(board_view, "background", at_once)
    started = []
    page = BoardPage(backend.SandboxAI(), lambda argv, title, **kw: started.append(argv),
                     lambda: {"board_grid": 20})
    page.started = started
    page.tabs = []
    page.prerequisite_terminal = lambda: (lambda argv, title, ended: page.tabs.append((argv, title, ended)) or True)

    def show(agent, options=()):
        board = B.from_argv(page.sai, agent, list(options), [], workspaces=[str(user.workspace)])
        page.set_board(board, user.workspace)
        page.check()
        return page
    page.show_board = show
    yield page
    page.deleteLater()


def run_tab(argv, env=None):
    """What a terminal tab of the app runs (prerequisites.CHAIN's steps), run here as the app's user."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL, env=env)


# --- A: "Podman failed (exit status 125) before the sandbox started: Error: localhost/qwen-sandbox:latest: image not
# known"; later "The image of qwen (localhost/qwen-sandbox:latest) is not built on this host … sandbox-ai build qwen" --

def test_qwen_image_not_known_the_check_accepts(board_page, user):
    """User: 'The image of qwen (localhost/qwen-sandbox:latest) is not built on this host … sandbox-ai build qwen'
    from the app before a start. The Board's check (the launcher's dry run) and the Start page's accept a start whose
    agent image is not built yet: the start builds it - nothing refuses, nothing is offered, nothing built yet."""
    page = board_page.show_board("qwen")
    assert page.verdict.text() == "The launcher accepts these options.", page.verdict.text()
    assert page.build_button.isHidden() and page.errors() == []
    plan, message = page.sai.dry_run("qwen", ["--workspace", str(user.workspace)])
    assert plan is not None and plan["image"] == "localhost/qwen-sandbox:latest"
    assert "the start builds it first (sandbox-ai build qwen)" in message
    assert user.builds() == [] and user.images() == {}


def test_qwen_image_other_store_build_now_is_offered(board_page, user, monkeypatch):
    """User: 'The image of qwen (...) is not built on this host …' although root's `podman images` listed it. Where the
    launcher still refuses (no build at a start: SANDBOX_AI_NO_AUTO_BUILD), its message names Podman's store and the
    user - and the Board still offers Build now for it, which builds into this user's own store, as this user."""
    monkeypatch.setenv("SANDBOX_AI_NO_AUTO_BUILD", "1")
    page = board_page.show_board("qwen")
    plan, message = page.sai.dry_run("qwen", ["--workspace", str(user.workspace)])
    assert plan is None and f"(Podman's image store: {user.store}, user " in message, message
    assert B.build_offers(message) == [["build", "qwen"]]
    assert not page.build_button.isHidden() and page.build_commands == [["build", "qwen"]]
    page.build_button.click()
    (argv, title, ended), = page.tabs
    steps = json.loads(argv[-1])
    assert [step[0] for step in steps] == ["build qwen"]
    assert steps[0][1][:3] == [page.sai.command()[0], "-I", page.sai.command()[2]], "sandbox-ai itself, no sudo"
    assert not any(word in ("sudo", "pkexec", "su") for word in steps[0][1])
    result = run_tab(steps[0][1])
    assert result.returncode == 0, result.stderr
    assert "localhost/qwen-sandbox:latest" in user.images()


def test_qwen_image_built_meanwhile_starts(board_page, user, monkeypatch):
    """User: built qwen's image as the user, and the app 'still said not built once (unexplained), later the session
    ran'. The Board's refusal was its check from before the build; Start checks again first and starts."""
    monkeypatch.setenv("SANDBOX_AI_NO_AUTO_BUILD", "1")
    page = board_page.show_board("qwen")
    assert page.errors(), "refused: the image is not there yet"
    user.add_image("localhost/qwen-sandbox:latest")  # built meanwhile, in a terminal
    page.start()
    assert not [text for title, text, _ in widgets.SHOWN if title == "Not started"], widgets.SHOWN
    assert page.started and page.started[-1][-1] != "--dry-run"


def test_variant_name_at_agent_build_now(board_page, user, monkeypatch):
    """A variant whose name two agents have (tor of dsh, tor of claude) is built as NAME@AGENT: Build now takes it."""
    message = ("agent-sandbox: The image of dsh (localhost/dsh-sandbox-tor:latest) is not built on this host (Podman's "
               f"image store: {user.store}, user u) (nothing is pulled at a start): sandbox-ai build --variant tor@dsh")
    assert B.build_offers(message) == [["build", "--variant", "tor@dsh"]]
    remote = ("The image of qwen (localhost/qwen-sandbox:latest) is not built on this host (Podman at "
              "unix:///run/x.sock) (nothing is pulled at a start): sandbox-ai build qwen")
    assert B.build_offers(remote) == [], "another machine's Podman: a build here would not help"


# --- B: "Der Launcher lehnt ab: agent-sandbox: The image of dsh (localhost/dsh-sandbox-tor:latest) is not built on this
# host (nothing is pulled at a start): sandbox-ai build --variant tor" -----------------------------------------------

def old_tor_variant(user):
    live_fakes().old_tor_variant(user)


def test_dsh_tor_variant_refused(board_page, user):
    """User: 'Der Launcher lehnt ab: agent-sandbox: The image of dsh (localhost/dsh-sandbox-tor:latest) is not built on
    this host … sandbox-ai build --variant tor', and the variant was not in the app's image list. The old variant
    (images/tor, dsh) is listed on the Images page with its agent; a Board start asks to build it, builds it in a tab
    as the user into the user's own store (sandbox-ai build --variant tor), and goes on; the check accepts it."""
    old_tor_variant(user)
    from sandbox_ai_app.images_page import ImagesPage
    images = ImagesPage(board_page.sai, lambda *a, **kw: None)
    images.images_listed(*board_page.sai.call("images", "--json"))
    rows = [tuple(images.images.item(row, column).text() for column in range(3))
            for row in range(images.images.rowCount())]
    assert ("tor", "dsh", "not built") in rows, rows
    images.deleteLater()
    page = board_page.show_board("dsh", ["--image-variant", "tor"])
    assert page.verdict.text() == "The launcher accepts these options.", page.verdict.text()
    assert page.start() is False  # preparing first: the variant is built in a tab
    (argv, title, ended), = page.tabs
    steps = json.loads(argv[-1])
    assert steps[0][1][-3:] == ["build", "--variant", "tor"] and "sudo" not in steps[0][1]
    assert run_tab(steps[0][1]).returncode == 0
    assert "localhost/dsh-sandbox-tor:latest" in user.images()
    ended(0)
    assert page.started, "the start goes on once it is built"


# --- the user's private presets tor-researcher and tor-research-v2 ---------------------------------------------------

@pytest.fixture
def tor_user(user):
    if not (PRIVATE_PRESETS / "tor-researcher.json").is_file():
        pytest.skip("SANDBOX_AI_PRIVATE_PRESETS names no folder with the Tor presets")
    assert user.run("presets", "dirs", "add", PRIVATE_PRESETS).returncode == 0
    assert user.run("vpn", "add", "tor-net", "--tor").returncode == 0
    assert user.run("mcp", "add", "mcp:duckduckgo", "--network", "full", "--no-pull").returncode == 0
    image = json.loads((user.home / ".config/sandbox-ai/mcp.json").read_text())["duckduckgo"]["image"]
    user.add_image(image)
    return user


def gallery_preset(sai, name):
    from sandbox_ai_app import presets as P
    found, problems = P.sandbox_ai_presets(sai)
    return next(p for p in found if p.name == name)


@pytest.mark.parametrize("agent", ["claude", "opencode", "dsh", "qwen"])
def test_tor_researcher_needs_no_variant(board_page, tor_user, agent):
    """User (B): the preset tor-researcher's board set --image-variant {variant} (default tor); with dsh the launcher
    refused. Loaded from the private preset folder as the gallery loads it, its board (the agent changed to claude,
    opencode, dsh or qwen) has no variant, and the launcher accepts it."""
    from sandbox_ai_app import presets as P
    preset = gallery_preset(board_page.sai, "tor-researcher")
    names = dict(preset.value_names)
    values = {p.marker: "tor-net" if names.get(p.marker) == "vpn" else "x" if names.get(p.marker) == "question"
              else p.default for p in preset.placeholders}  # as the form fills it in: the defaults, optional ones empty
    board = P.build(board_page.sai, preset, str(tor_user.workspace), agent=agent, values=values)
    board.first("agent").props["agent"] = agent
    argv = B.launcher_argv(board_page.sai, board)
    assert "--image-variant" not in argv and not B.placeholders(board), argv
    plan, message = board_page.sai.dry_run(agent, argv)
    assert plan is not None, message
    assert plan["image"] == f"localhost/{agent}-sandbox:latest"


def test_tor_researcher_starts_once_everything_is_there(board_page, tor_user, monkeypatch):
    """User (B, after the fixes): a start of the tor-researcher board - its VPN profile and MCP server there - must
    start. The app read what the start needs (prerequisites: --vpn, --mcp) and, everything there, remembered the
    wrong thing as done: it read it again and called the start again, without end - nothing ever started."""
    from sandbox_ai_app import board_view
    reads = []
    real = board_page.sai.host_vpn_profiles
    monkeypatch.setattr(board_page.sai, "host_vpn_profiles", lambda host: reads.append(host) or real(host))
    calls = {"n": 0}
    real_background = board_view.background

    def bounded(function, *args, done, **kwargs):  # (an endless loop is a failure here, not a hang)
        calls["n"] += 1
        assert calls["n"] < 50, "the start reads its needs again and again"
        return real_background(function, *args, done=done, **kwargs)
    monkeypatch.setattr(prerequisites, "background", bounded)
    page = board_page.show_board("claude", ["--vpn", "tor-net", "--mcp", "duckduckgo"])
    assert page.verdict.text() == "The launcher accepts these options.", page.verdict.text()
    page.start()
    assert len(page.started) == 1 and len(reads) == 1, (page.started, reads)


def test_tor_research_v2_variant_per_agent(board_page, tor_user):
    """tor-research-v2's crawl-tools variant (tor-research) is one per agent: for dsh, without one, the start says how
    to make it (sandbox-ai images new tor-research --from dsh) and starts nothing; made for dsh and opencode, Start
    asks to build dsh's (tor-research@dsh) and builds it into the user's own store."""
    page = board_page.show_board("dsh", ["--image-variant", "tor-research"])
    assert page.start() is False
    said = "\n".join(text for _, text, _ in widgets.SHOWN)
    assert "Image variant tor-research" in said and "--from dsh" in said, said
    for owner in ("opencode", "dsh"):
        assert tor_user.run("images", "new", "tor-research", "--from", owner).returncode == 0
    widgets.SHOWN.clear()
    page.prerequisites_done = None
    page.check()
    assert page.verdict.text() == "The launcher accepts these options.", page.verdict.text()
    page.start()
    (argv, title, ended), = page.tabs
    steps = json.loads(argv[-1])
    assert steps[0][1][-3:] == ["build", "--variant", "tor-research@dsh"]
    assert run_tab(steps[0][1]).returncode == 0
    assert "localhost/dsh-sandbox-tor-research:latest" in tor_user.images()
    assert "localhost/opencode-sandbox-tor-research:latest" not in tor_user.images()


# --- C: the app started a running session a second time ('is running; attach with…', exit 1) --------------------------

def test_second_start_attaches(qapp, user, monkeypatch):
    """User: the app started a running session a second time: 'is running; attach with…', exit status 1. A start with
    the name of a session the Sandboxes page knows running opens `sandbox-ai session attach NAME` instead."""
    from sandbox_ai_app.ui import MainWindow
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    window = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    try:
        if window.tray is not None:
            window.tray.hide()
            window.tray = None
        opened = []
        monkeypatch.setattr(window.terminals, "open", lambda argv, title, directory=None, done=None: opened.append(argv))
        window.sandboxes.instances = {"session:q1": backend.Instance("session", "session:q1", "q1", session="q1",
                                                                     agent="qwen", state="running")}
        window.in_terminal(window.sai.launch_argv("qwen", [], session="q1"), "qwen (q1)", embedded=True)
        assert opened[-1] == window.sai.command("session", "attach", "q1")
    finally:
        window.shutdown()
        window.close()
        window.deleteLater()


# --- D: "exiftool: needs --mount /work=HOST" when the Board added the MCP server ------------------------------------

def test_exiftool_needs_folder(board_page, user, monkeypatch):
    """User: the Board's adding exiftool failed with 'sandbox-ai mcp: exiftool needs --mount /work=HOST'. Read from
    sandbox-ai's real catalogs, the folder it needs is asked for (the start's workspace offered) and passed as
    --mount; the server is added with it."""
    page = board_page
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "workspace")
    page.prerequisite_argv = lambda: ["--workspace", str(user.workspace), "--mcp", "exiftool"]
    page.prerequisite_terminal = lambda: None
    ran = []
    real = page.sai.host_run
    monkeypatch.setattr(page.sai, "host_run", lambda host, *args, **kw: ran.append(args) or real(host, *args, **kw))
    page.provide({"--mcp": ["exiftool"]}, [], "local", ("local", "{}"), None, "start",
                 context=page.prerequisite_context(), agent="claude")
    assert ("mcp", "add", "exiftool", "--mount", f"/work={user.workspace}") in ran, (ran, widgets.SHOWN)
    question = next(text for title, text, _ in widgets.SHOWN if title == "MCP server exiftool")
    assert "/work" in question


# --- E: "15:19:11 WARNING board: Was der Start braucht, wird noch vorbereitet…" ---------------------------------------

@pytest.fixture
def entries():
    """Every entry of the app's log from now on, as the log window gets them."""
    from sandbox_ai_app import applog
    got = []
    applog.add_listener(got.append)
    yield got
    applog.remove_listener(got.append)


def in_window(page):
    """The Board in a stand-in for the window: its Terminals page with the tab of the preparation, show_page."""
    import types
    holder = QtWidgets.QWidget()
    holder.pages, holder.current = [], []
    holder.show_page = holder.pages.append
    holder.terminals = types.SimpleNamespace(views=lambda: [view for view in holder.views],
                                             tabs=types.SimpleNamespace(setCurrentWidget=holder.current.append))
    holder.views = []
    page.setParent(holder)
    return holder


def test_preparing_says_what_and_how_far(board_page, user, entries):
    """User: '15:19:11 WARNING board: Was der Start braucht, wird noch vorbereitet…' - 'da sollte stehen was noch
    vorbereitet wird und Progress'; then 'was der Start braucht… ja was braucht er denn???' and 'ich drücke auf
    Starten und nichts passiert und keine Infos'. A Board start that needs dsh's variant tor built: the check says
    in plain words what the start needs and what is missing; the Board shows what is prepared, with a bar per item
    and one for the whole, fed from the terminal tab's real `sandbox-ai build` output; Start pressed again says what
    runs, as information and once; a click on the line (on the Board, or in the log window) opens the tab."""
    import types
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from sandbox_ai_app import logview
    old_tor_variant(user)
    page = board_page.show_board("dsh", ["--image-variant", "tor"])
    holder = in_window(page)
    assert page.start() is False
    (argv, title, ended), = page.tabs
    said = [(e["level"], e["message"]) for e in entries if e["where"] == "board"]
    assert ("INFO", "The start needs on local: image variant tor of dsh. Missing: image variant tor of dsh (not "
                    "built).") in said, said
    panel = page.preparing_panel
    assert not panel.isHidden()
    assert panel.line.text() == "Preparing (1 of 1): building the image variant tor", panel.line.text()
    assert [label.text() for label in panel.labels] == ["image variant tor: waiting"]
    # Start again (twice) while it is prepared: what runs, at INFO, once - never the bare "still being prepared".
    del entries[:]
    assert page.start() is False and page.start() is False
    still = [(e["level"], e["message"]) for e in entries if e["where"] == "board"]
    line = ("INFO", "Still preparing (1 of 1): building the image variant tor. The start goes on when it is done.")
    assert still.count(line) == 1, still  # (the Start requests themselves are logged too, since logging round 1)
    assert not [e for e in entries if e["level"] == "WARNING"], entries
    # The tab runs: the real sandbox-ai build (Podman faked) through CHAIN, which writes how far it is.
    result = run_tab(argv)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "(step 1/1)" in result.stdout, result.stdout
    page.preparing_tick()
    item, = page.preparation.items
    assert item.state == "done" and item.step == (1, 1), vars(item)
    assert panel.bars[0].value() == panel.bars[0].maximum() and panel.overall.value() == panel.overall.maximum()
    # Its line opens the terminal tab where it runs: on the Board, and in the log window.
    view = types.SimpleNamespace(argv=list(argv), setFocus=lambda: None)
    holder.views.append(view)
    QTest.mouseClick(panel.line, Qt.MouseButton.LeftButton)
    assert holder.pages == ["terminals"] and holder.current == [view]
    log = logview.LogPanel()
    line = next(logview.line_of(e) for e in entries if e["message"].startswith("Still preparing (1 of 1)"))
    assert log.click_line(line) and holder.current == [view, view]
    log.deleteLater()
    ended(0)
    assert panel.isHidden() and page.preparation is None
    assert page.started, "the start goes on once it is built"
    assert "localhost/dsh-sandbox-tor:latest" in user.images()
    page.setParent(None)
    holder.deleteLater()


def test_start_held_says_what_is_missing_and_offers_it(board_page, user, monkeypatch):
    """User: 'ich drücke auf Starten und nichts passiert und keine Infos'. A start held because something it needs
    is not made (the build of dsh's variant tor declined) says so on the Board, not only in the log, with the button
    that makes it: Build now builds it in a terminal tab without asking again."""
    old_tor_variant(user)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # (the build is declined)
    page = board_page.show_board("dsh", ["--image-variant", "tor"])
    assert page.start() is False and page.tabs == [] and not page.started
    panel = page.preparing_panel
    assert not panel.isHidden()
    assert panel.line.text() == "Not started: the start needs what is not there yet."
    assert [label.text() for label in panel.labels] == [
        "Image variant tor: not built (sandbox-ai build --variant NAME)."]
    assert [button.text() for button in panel.buttons] == ["Build now"]
    panel.buttons[0].click()
    (argv, title, ended), = page.tabs  # (no question: AUTO_ANSWER still declines one)
    steps = json.loads(argv[-1])
    assert steps[0][1][-3:] == ["build", "--variant", "tor"]
    assert panel.line.text() == "Preparing (1 of 1): building the image variant tor"
    assert run_tab(argv).returncode == 0
    ended(0)
    assert panel.isHidden() and "localhost/dsh-sandbox-tor:latest" in user.images()


def test_an_approval_answer_that_fails_is_a_notice_not_a_box(qapp, tmp_path, monkeypatch):
    """User 2026-11-17: "sandbox-ai approvals: proxy.example.org has no address (on this host: No address associated
    with hostname)" came in a box with OK, after a box that asked first. Now: no question, no box - a short notice in
    the error colour that goes by itself, the text kept in the page's note line (the real page, its real answer path;
    only sandbox-ai's reply is given)."""
    import time
    from sandbox_ai_app import pages, short_notices
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(short_notices, "SHOWN", [])
    run = tmp_path / "runtime" / "sandbox-ai" / "runs" / "claude-1"
    (run / "approvals").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run": "claude-1", "agent": "claude", "pid": os.getpid(),
                                              "workspace": str(tmp_path), "session_dir": str(tmp_path),
                                              "started": "2026-11-17T04:00:00Z", "ask_timeout": 60}))
    (run / "approvals" / "1763352000-1-1.json").write_text(json.dumps({
        "id": "1763352000-1-1", "kind": "dns", "name": "proxy.example.org", "timeout": 60,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    sai = backend.SandboxAI()
    why = ("sandbox-ai approvals: proxy.example.org has no address (on this host: No address associated with "
           "hostname): there is nothing to allow")
    monkeypatch.setattr(sai, "answer", lambda *a, **k: (1, "", why))
    page = pages.ApprovalsPage(sai, interval=0, remote_interval=0)
    try:
        page.open.selectRow(0)
        page.allow_run_button.click()
        deadline = time.monotonic() + 10
        while page.answers and time.monotonic() < deadline:
            qapp.processEvents()
        assert widgets.SHOWN == [], "no box"
        assert short_notices.SHOWN and "has no address" in short_notices.SHOWN[-1][1]
        assert "has no address" in page.message.text()
    finally:
        page.timer.stop()
        page.blocked.timer.stop()
        page.deleteLater()
