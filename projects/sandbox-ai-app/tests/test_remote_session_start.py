"""A start under a session's name that is taken, and the login of its agent - on this machine and on another host.

User (2026-11-19), a Board start on a remote host (192.168.88.150) that ended in a terminal tab:
  "agent-sandbox: the host has no codex login; log in with `sandbox-ai login codex` ...
   git-logins: https://github.com left out ...
   agent-sandbox: Session vital-wallaby-azalea is running; attach with: sandbox-ai attach vital-wallaby-azalea
   [Exit-Status 1]"
- "the session/terminal is not opened", "the session is not started at all it seems".

End to end: the app's own Board and Start page and its backend, the real ./sandbox-ai of SANDBOX_AI_HOME here and -
through a fake ssh (as the other remote tests have it) - the real ./sandbox-ai of another host with a HOME of its
own; only Podman, tmux and npm are stand-ins (sandbox-ai's tests/live_fakes.py). The sessions these tests meet are
real ones of that host's sandbox-ai: started through its entry point and held where their pod would be made."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, prerequisites, session_start, terminal, terminal_text, widgets  # noqa: E402

HOME = os.environ.get("SANDBOX_AI_HOME")
pytestmark = pytest.mark.skipif(not HOME, reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

# A fake ssh: the command for ct1 runs here, as ct1's user (their HOME, their sandbox-ai on PATH, their Podman).
SSH = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
if "-O" in args or "-G" in args or "--" not in args:
    sys.exit(255)
at = args.index("--")
host = args[at + 1].rpartition("@")[2].split(".")[0]
env = json.load(open(os.path.join(os.environ["FAKE_HOSTS"], host + ".json")))
os.chdir(env["HOME"])
os.execve("/bin/sh", ["sh", "-c", args[at + 2]], env)
'''
LOGIN = {"tokens": {"access_token": "a" * 40, "refresh_token": "r" * 40, "account_id": "acct-1", "id_token": "x"}}


def live_fakes():
    spec = importlib.util.spec_from_file_location("live_fakes_for_remote_start", Path(HOME) / "tests/live_fakes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def at_once(function, *args, done, **kwargs):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        return done(None, exc)
    return done(result, None)


def wait_for(check, timeout=60):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "waited in vain"
        time.sleep(0.05)


class Fleet:
    """This machine (HERE, the app's user) and the host ct1 (THERE, a user of its own), each a live_fakes.Host."""

    def __init__(self, here, there):
        self.here, self.there, self.held, self.relays = here, there, [], []

    def of(self, host):
        return self.there if host == "ct1" else self.here

    def running(self, host, agent, name, *options, ready=False):
        """A session NAME of AGENT that runs on HOST: started through its sandbox-ai, held where its pod is made (its
        supervisor runs, its terminal is not there yet). READY: its terminal's relay is there too, and the command
        that started it has ended (it holds the session's lock no longer)."""
        side = self.of(host)
        side.add_image(f"localhost/{agent}-sandbox:latest")
        hold = side.directory / f"release-{name}"
        stop = side.directory / f"stopped-{name}.json"
        env = {"FAKE_PODMAN_POD_HOLD": str(hold), "FAKE_PODMAN_STOP_AT": str(stop)}
        process = side.popen(agent, *options, *(["--session", name] if "--no-tmux" not in options else ["--name", name]),
                             env=env)
        self.held.append((process, hold))
        wait_for(stop.exists)
        if ready:
            relay = side.home / ".local/state/sandbox-ai/sessions" / name / "tmux/relay/attach.sock"
            relay.parent.mkdir(exist_ok=True)
            short = Path(tempfile.mkdtemp(prefix="relay-"))  # (a test's path is longer than a socket's name may be)
            (short / "d").symlink_to(relay.parent)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(short / "d" / relay.name))
            server.listen(8)
            self.relays.append((server, short))
            wait_for(lambda: process.poll() is not None)
        return process

    def status(self, host, name):
        done = self.of(host).run("session", "status", name, "--json")
        return json.loads(done.stdout) if done.returncode == 0 else None

    def release(self):
        for process, hold in self.held:
            hold.touch()
        for process, _ in self.held:
            try:
                process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                try:  # (bounded too: a process it started may hold its output open - the guard ends that one)
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
                    process.wait(timeout=10)
        for server, short in self.relays:
            server.close()
            shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def fleet(qapp, tmp_path, monkeypatch, real_logins):
    fakes = live_fakes()
    here, there = fakes.Host(tmp_path / "here"), fakes.Host(tmp_path / "ct1")
    (there.bin / "sandbox-ai").write_text(f'#!/bin/sh\nexec "{Path(HOME) / "sandbox-ai"}" "$@"\n')
    (there.bin / "sandbox-ai").chmod(0o755)
    hosts = tmp_path / "hosts"
    hosts.mkdir()
    (hosts / "ct1.json").write_text(json.dumps(there.env))
    (here.bin / "ssh").write_text(SSH.replace("PYTHON", sys.executable, 1))
    (here.bin / "ssh").chmod(0o755)
    for name in ("SANDBOX_AI_NO_AUTO_BUILD", "SANDBOX_AI_MCP_NO_IMAGE_CHECK"):
        monkeypatch.delenv(name, raising=False)
        there.env.pop(name, None)
    for name, value in here.env.items():
        if name.startswith(("FAKE_", "XDG_", "SANDBOX_AI_BUILD")) or name in ("HOME", "PATH"):
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("FAKE_HOSTS", str(hosts))
    monkeypatch.setenv("SANDBOX_AI_SSH_MULTIPLEX", "0")
    config = here.home / ".config/sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    (config / "hosts.json").write_text(json.dumps({"version": 1, "hosts": {"ct1": {"ssh": "me@ct1.example",
                                                                                  "port": 22}}}))
    monkeypatch.chdir(here.workspace)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", None)
    monkeypatch.setattr(widgets, "SHOWN", [])
    for module in (prerequisites, session_start):
        monkeypatch.setattr(module, "background", at_once)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    found = Fleet(here, there)
    yield found
    found.release()


def make_board(fleet, monkeypatch, agent, host="ct1", options=()):
    """The Board with a board of AGENT that starts on HOST; .started: what went to a terminal [(argv, title, kw)]."""
    from sandbox_ai_app import board_view
    from sandbox_ai_app.board_view import BoardPage
    monkeypatch.setattr(board_view, "background", at_once)
    started = []
    page = BoardPage(backend.SandboxAI(), lambda argv, title, **kw: started.append((list(argv), title, kw)) or True,
                     lambda: {"board_grid": 20})
    page.started = started
    page.notes = []  # every note the Board showed (its log has them; the label keeps the last)
    show = page.show_notice
    page.show_notice = lambda text, role="secondary": (page.notes.append(text), show(text, role))[1]
    workspace = fleet.of(host).workspace
    # (the agent's image there: what is about sessions is not held up by the question about an image not built)
    fleet.of(host).add_image(f"localhost/{agent}-sandbox:latest")
    board = B.from_argv(page.sai, agent, list(options), [], workspaces=[str(workspace)])
    page.set_board(board, workspace)
    if host != "local":
        page.host_choice.reload()
        assert page.host_choice.choose(host)
    page.check()
    return page


def asked(title):
    return [text for shown, text, _ in widgets.SHOWN if shown == title]


def session_of(argv):
    """The NAME of --session NAME in a start's command line (this machine's argv, or the command ssh hands on)."""
    words = terminal.remote_words(argv[-1]) if os.path.basename(argv[0]) == "ssh" else argv
    return terminal_text.start_session(words)


# --- 1: before the start, the host's sessions ------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["ct1", "local"])
def test_the_same_agents_running_session_is_attached(fleet, monkeypatch, host):
    """Same name, same agent, running: its terminal is opened (attach) - on another host too, where the start used
    to go to a tab and end in 'is running; attach with ...', exit status 1."""
    fleet.running(host, "qwen", "q1")
    page = make_board(fleet, monkeypatch, "qwen", host)
    assert page.start(embedded=True, session="q1") is False  # (not a start: an attach)
    (argv, title, kw), = page.started
    assert argv == page.sai.attach_argv(host, "q1"), argv
    assert ("attach" in argv) if host == "local" else ("attach q1" in argv[-1] and argv[0].endswith("ssh"))
    assert not asked("Session q1"), "nothing to ask: it is that agent's session"
    assert any("q1 runs already: its terminal is opened" in note for note in page.notes), page.notes


@pytest.mark.parametrize("host", ["ct1", "local"])
def test_another_agents_session_is_said_before_anything_runs(fleet, monkeypatch, host):
    """User: 'Session vital-wallaby-azalea is running; attach with: ...' in a tab, exit status 1 - the Board had kept
    its session name and the second Start (another agent) met the first's session. Said before anything runs, with
    what can be done; Cancel (the default) starts nothing."""
    fleet.running(host, "qwen", "q1")
    page = make_board(fleet, monkeypatch, "claude", host)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    assert page.start(embedded=True, session="q1") is False
    text, = asked("Session q1")
    where = "ct1" if host == "ct1" else "this machine"
    assert f"A session named q1 runs already on {where} - with qwen, not claude." in text, text
    assert "It is still starting." in text and "What sandbox-ai sees there: a session of qwen, in tmux" in text, text
    assert page.started == [], "nothing ran"
    assert "Not started: a session named q1 is there already." in page.notes


def test_another_agents_session_attach_other_name_or_stop(fleet, monkeypatch):
    fleet.running("ct1", "qwen", "q1", ready=True)
    page = make_board(fleet, monkeypatch, "claude")
    # Attach to it: its terminal, as it is.
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "attach")
    page.start(embedded=True, session="q1")
    assert page.started[-1][0] == page.sai.attach_argv("ct1", "q1")
    # Start under another name: a fresh codename, and the Board's controls show it.
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "other")
    page.start_controls.name.setText("q1")
    page.start(embedded=True, session="q1")
    argv = page.started[-1][0]
    name = session_of(argv)
    assert name and name != "q1" and terminal.start_of_argv(argv) == "claude", argv
    assert page.start_controls.name.text() == name, "the name typed is replaced by the one it started under"
    assert fleet.status("ct1", "q1")["state"] == "running", "the other session is left alone"
    # Stop it and start: asked (start-stop), stopped there, and claude starts under a fresh name (q1 stays qwen's).
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "stop")
    widgets.SHOWN.clear()
    before = len(page.started)
    page.start(embedded=True, session="q1")
    assert asked("Stop") and "Stop q1?" in asked("Stop")[0]
    assert fleet.status("ct1", "q1")["state"] == "stopped"
    assert len(page.started) == before + 1
    assert session_of(page.started[-1][0]) not in (None, "q1", name)


def test_a_paused_session_and_one_in_the_foreground(fleet, monkeypatch):
    """Paused: Continue it (sandbox-ai resume: its terminal), stop, or another name. In the foreground of another
    terminal: no terminal to attach to - stop it, or another name."""
    fleet.running("ct1", "qwen", "q1")
    (fleet.there.home / ".local/state/sandbox-ai/sessions/q1/paused").touch()
    page = make_board(fleet, monkeypatch, "qwen")
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "continue")
    page.start(embedded=True, session="q1")
    assert "A session named q1 is paused on ct1 (qwen)." in asked("Session q1")[0]
    assert page.started[-1][0] == page.sai.resume_argv("ct1", "q1")
    assert [key for key, _ in session_start.choices_for(page.sai.session_seen("ct1", "q1"), "qwen", "x")] == \
        ["continue", "stop", "other"]
    fleet.running("ct1", "qwen", "t1", "--no-tmux")
    seen = page.sai.session_seen("ct1", "t1")
    assert seen["foreground"] and not seen["attachable"] and seen["attach_why"] == "foreground"
    assert [key for key, _ in session_start.choices_for(seen, "qwen", "x")] == ["stop", "other"]
    widgets.SHOWN.clear()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.start(embedded=True, session="t1")
    assert "runs on ct1 in the foreground of another terminal or script" in asked("Session t1")[0]
    assert "it has no terminal to attach to" in asked("Session t1")[0]


def test_a_stopped_session_of_another_agent_is_said_too(fleet, monkeypatch):
    """'Session NAME belongs to qwen, not claude' was the launcher's refusal in a tab: said before, with another name."""
    fleet.running("ct1", "qwen", "q1", ready=True)
    assert fleet.there.run("stop", "q1").returncode == 0
    page = make_board(fleet, monkeypatch, "claude")
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "other")
    page.start(embedded=True, session="q1")
    assert "A stopped session named q1 on ct1 belongs to qwen, not claude" in asked("Session q1")[0]
    assert session_of(page.started[-1][0]) not in (None, "q1")
    # The same agent's stopped session: nothing in the way - the launcher continues it, as before.
    again = make_board(fleet, monkeypatch, "qwen")
    widgets.SHOWN.clear()
    again.start(embedded=True, session="q1")
    assert not asked("Session q1") and session_of(again.started[-1][0]) == "q1"


def test_nothing_of_that_name_starts_as_before(fleet, monkeypatch):
    page = make_board(fleet, monkeypatch, "qwen")
    page.start(embedded=True, session="fresh")  # (it goes on from the host's answer: nothing of that name)
    (argv, title, kw), = page.started
    here = make_board(fleet, monkeypatch, "qwen", "local")
    assert here.start(embedded=True, session="fresh") is True, "this machine: read at once, started at once"
    assert session_of(here.started[-1][0]) == "fresh"
    assert session_of(argv) == "fresh" and terminal.start_of_argv(argv) == "qwen" and not widgets.SHOWN
    assert isinstance(kw.get("ended"), session_start.StartTab), "its end is watched: a refusal is said"


def test_a_host_that_does_not_answer_starts_all_the_same(fleet, monkeypatch):
    """What is there under the name could not be read (the host did not answer the look): the start goes on - the
    launcher has the last word, and its refusal is said when it comes."""
    page = make_board(fleet, monkeypatch, "qwen")

    def unreachable(host, name):
        raise ValueError("ssh: connect to host ct1 port 22: No route to host")
    monkeypatch.setattr(page.sai, "session_seen", unreachable)
    assert page.start(embedded=True, session="q9") is False  # (it went on from the look's answer)
    assert session_of(page.started[-1][0]) == "q9"


def test_the_boards_codename_is_used_once(fleet, monkeypatch):
    """The cause: the Board offered the codename of its last start again. A start that had waited for a check or a
    preparation went on by itself later, and only a start that returned at once got a new codename - the next Start
    (another agent) met the session of the first."""
    page = make_board(fleet, monkeypatch, "qwen")
    first = page.start_controls.suggestion
    assert first and page.start_controls.name.text() == ""
    page.start(embedded=True, session=first)  # (as a start that went on after its check: not through the controls)
    assert session_of(page.started[-1][0]) == first
    assert page.start_controls.suggestion not in ("", first), "the next start gets another codename"
    # A name typed stays: starting it again is meant (its running session is attached).
    page.start_controls.name.setText("work")
    page.start_controls.start()
    assert page.start_controls.name.text() == "work"


def test_the_start_page_checks_the_hosts_sessions_too(fleet, monkeypatch):
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "background", at_once)
    fleet.running("ct1", "qwen", "q1")
    started = []
    page = ui.LaunchPage(backend.SandboxAI(), lambda argv, title, **kw: started.append(list(argv)) or True)
    page.host_choice.reload()
    assert page.host_choice.choose("ct1")
    page.set_from_argv("claude", ["--workspace", str(fleet.there.workspace)])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    assert page.start(session="q1", embedded=True) is False
    assert "A session named q1 runs already on ct1 - with qwen, not claude." in asked("Session q1")[0]
    assert started == []
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.set_from_argv("qwen", ["--workspace", str(fleet.there.workspace)])
    page.start(session="q1", embedded=True)
    assert started == [page.sai.attach_argv("ct1", "q1")]


def test_what_a_start_does_with_what_is_there():
    def seen(**facts):
        return {"name": "s", "agent": "codex", "state": "running", "foreground": False, "terminal": "ready",
                "attachable": True, "attach_why": None, "login": False, "seen": "", **facts}
    judged = session_start.judged
    assert judged(None, "codex") == "start"
    assert judged(seen(), "codex") == "attach" and judged(seen(terminal="starting"), "codex") == "attach"
    assert judged(seen(), "claude") == "ask" and judged(seen(state="paused"), "codex") == "ask"
    assert judged(seen(foreground=True, attachable=False), "codex") == "ask"
    assert judged(seen(terminal="missing", attachable=False), "codex") == "ask"
    assert judged(seen(terminal="ended", attachable=False), "codex") == "start", "the launcher stops and restarts it"
    assert judged(seen(state="stopped", terminal=None), "codex") == "start"
    assert judged(seen(state="stopped", terminal=None), "claude") == "ask"
    assert judged(seen(state="orphaned"), "codex") == "start" and judged(seen(state="damaged"), "claude") == "start"
    assert [k for k, _ in session_start.choices_for(seen(), "claude", "n")] == ["attach", "stop", "other"]
    assert [k for k, _ in session_start.choices_for(seen(state="stopped", attachable=False), "claude", "n")] == ["other"]
    # What another host says of its start is shown as text, cut, never trusted as more.
    facts = backend.seen_facts({"name": "a\x1b[31mb", "agent": "x" * 99, "state": "weird", "terminal": "?",
                                "attachable": "yes", "login": "yes", "seen": "s" * 5000})
    assert "\x1b" not in facts["name"] and len(facts["agent"]) == 40 and facts["state"] == "damaged"
    assert facts["terminal"] is None and facts["login"] is False and len(facts["seen"]) == 600


# --- 4: the launcher's refusal in the tab, and on the Board -----------------------------------------------------------

class Bar:
    def __init__(self):
        self.said = []

    def ask(self, text, choices, cancelled=None, level="warn"):
        self.said.append((text, [label for label, _ in choices], dict(choices)))

    def say(self, text, seconds=6, level="info"):
        self.said.append((text, [], {}))


class Tab:
    """A terminal tab, as a start's end sees it: the end of what it printed, and the line above it."""

    def __init__(self, text):
        self.text, self.bar = text, Bar()

    def start_tail(self):
        return self.text


def test_a_refused_start_is_said_plainly_in_its_tab_and_on_the_board(fleet, monkeypatch):
    """User: the tab ended with the launcher's lines and '[Exit-Status 1]', nothing else. The launcher's own refusal
    - read from the end of the tab, here the real one of ct1's sandbox-ai - is said in the line above the terminal
    and on the Board, with the same buttons as before a start."""
    fleet.running("ct1", "qwen", "q1")
    fleet.there.add_image("localhost/claude-sandbox:latest")
    refused = fleet.there.run("claude", "--session", "q1")
    assert refused.returncode == 1
    tab = Tab(refused.stderr + "\n[sandbox-ai: exit status 1; close the tab with ×]\n")
    said = terminal_text.refusal(tab.start_tail())
    assert said.startswith("Session q1 is running with qwen, not claude; attach with: sandbox-ai attach q1"), said
    assert terminal_text.session_refusal(said) == "q1"
    page = make_board(fleet, monkeypatch, "claude")
    shown = []
    page.show_missing = lambda text, rows, role="notice-warn": shown.append((text, rows))
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "attach")
    page.start_ended(1, tab, "ct1", "claude", "q1", True, lambda name: page.started.append(("again", name)))
    text, labels, actions = tab.bar.said[-1]
    assert text.startswith("ct1: Not started: Session q1 is running with qwen, not claude"), text
    assert "A session named q1 runs already on ct1 - with qwen, not claude." in text
    assert labels[:2] == ["Attach to it", "Stop it and start claude as " + labels[1].rsplit(" ", 1)[1]], labels
    assert labels[2].startswith("Start as ") and labels[3] == "Cancel"
    (first, rows), = shown
    assert first.startswith("ct1: Not started: Session q1 is running with qwen, not claude")
    assert [label for label, _ in rows[0][1]] == labels[:3], "the Board's note has the same buttons"
    actions["Attach to it"]()
    assert page.started[-1][0] == page.sai.attach_argv("ct1", "q1")
    actions[labels[2]]()
    assert page.started[-1] == ("again", labels[2][len("Start as "):])


def test_a_start_that_failed_otherwise_says_the_launchers_last_words(fleet, monkeypatch):
    page = make_board(fleet, monkeypatch, "qwen")
    tab = Tab("agent-sandbox: note: something\nagent-sandbox: \x1b[31mUnknown MCP server: nope; defined: none\x1b[0m\n"
              "\n[sandbox-ai: exit status 1; close the tab with ×]\n")
    assert terminal_text.refusal(terminal_text.ESCAPES.sub("", tab.text)) == "Unknown MCP server: nope; defined: none"
    page.start_ended(1, tab, "local", "qwen", "s1", True, lambda name: None)
    text, labels, _ = tab.bar.said[-1]
    assert text.startswith("Not started: ") and "Unknown MCP server: nope" in text and labels == []
    quiet = Tab("")
    page.start_ended(0, quiet, "local", "qwen", "s1", True, lambda name: None)
    assert quiet.bar.said == [], "a start that ended well: nothing to say"
    silent = Tab("some output without the launcher's words\n")
    page.start_ended(3, silent, "local", "qwen", None, True, lambda name: None)
    assert silent.bar.said[-1][0] == "The start ended with an error (exit status 3): its terminal shows why."


def test_refusals_of_older_launchers_are_read_too():
    for text, name in (("Session work is running; attach with: sandbox-ai attach work", "work"),
                       ("Session work is paused; continue it with: sandbox-ai resume work", "work"),
                       ("Session work belongs to claude, not codex", "work"),
                       ("Session w.2 is being started or stopped by another command; try again in a moment", "w.2")):
        assert terminal_text.session_refusal(text) == name, text
    assert terminal_text.session_refusal("Unknown MCP server: Session") is None
    assert terminal_text.start_agent(["python3", "-I", "/x/main.py", "codex", "--session", "a"]) == "codex"
    assert terminal_text.start_agent(["sandbox-ai", "codex", "--dry-run"]) is None
    assert terminal_text.start_agent(["sandbox-ai", "login", "codex"]) is None
    assert terminal_text.start_session(["sandbox-ai", "codex", "--session", "a", "--", "--session", "b"]) == "a"
    tail = terminal_text.OutputTail(limit=100, keep=10)
    tail.feed(b"x" * 60)
    assert not tail.done
    tail.feed(b"y" * 60)
    assert tail.done and tail.text() == "y" * 10


# --- 3: the agent's login on the start's host -------------------------------------------------------------------------

def with_login_terminal(page):
    """The page's logins go to a terminal that the test ends: .logins [(argv, title, ended)]."""
    page.logins = []
    page.login_terminal = lambda: (lambda argv, title, ended: page.logins.append((argv, title, ended)) or True)
    return page


def log_in(fleet, host, agent="codex"):
    """What `sandbox-ai login codex` in that terminal ends with: the login in that host's store (made by its real
    sandbox-ai login, whose sandbox - the fake Podman - writes Codex's auth.json)."""
    side = fleet.of(host)
    side.add_image(f"localhost/{agent}-sandbox:latest")
    done = side.run("login", agent, env={"FAKE_PODMAN_RUN": "0", "FAKE_PODMAN_WRITE_HOME": json.dumps(
        {".codex/auth.json": json.dumps(LOGIN)})})
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("host", ["ct1", "local"])
def test_a_start_without_the_agents_login_asks_to_log_in_first(fleet, monkeypatch, host):
    """User: 'agent-sandbox: the host has no codex login; log in with `sandbox-ai login codex`' - in the tab of a start
    that then ran without one. Asked first: 'Log in now' runs sandbox-ai login codex on that host in a terminal, and
    the start goes on when it ends with a login."""
    page = with_login_terminal(make_board(fleet, monkeypatch, "codex", host))
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "login")
    assert page.start(embedded=True, session="c1") is False
    where = "ct1" if host == "ct1" else "this machine"
    question, = asked("Log in to codex")
    assert f"There is no login for codex on {where}" in question and f"Log in to codex on {where} now?" in question
    (argv, title, ended), = page.logins
    assert argv == page.sai.login_argv(host, "codex") and terminal.login_of(argv) == "codex"
    assert page.started == [], "the start waits for the login"
    log_in(fleet, host)
    ended(0)
    assert len(page.logins) == 1, "it has a login now: not asked again"
    assert session_of(page.started[-1][0]) == "c1" and terminal.start_of_argv(page.started[-1][0]) == "codex"
    # The next start: the host has the login, nothing is asked.
    widgets.SHOWN.clear()
    page.start(embedded=True, session="c2")
    assert not asked("Log in to codex") and session_of(page.started[-1][0]) == "c2"


def test_start_without_a_login_is_said_and_not_asked_again(fleet, monkeypatch):
    page = with_login_terminal(make_board(fleet, monkeypatch, "codex"))
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "without")
    page.start(embedded=True, session="c1")
    assert asked("Log in to codex") and page.logins == []
    assert session_of(page.started[-1][0]) == "c1"
    assert any(note.startswith("Started without a login of codex on ct1") for note in page.notes), page.notes
    widgets.SHOWN.clear()
    page.start(embedded=True, session="c2")
    assert not asked("Log in to codex"), "chosen once: not asked again while the app runs"
    assert session_of(page.started[-1][0]) == "c2"


def test_a_login_that_ends_without_one_or_is_cancelled_starts_nothing(fleet, monkeypatch):
    page = with_login_terminal(make_board(fleet, monkeypatch, "codex"))
    shown = []
    page.show_missing = lambda text, rows, role="notice-warn": shown.append((text, rows))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    assert page.start(embedded=True, session="c1") is False
    assert page.started == [] and page.logins == []
    assert shown[-1][1][0][0] == "There is no login for codex on ct1."
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "login")
    page.start(embedded=True, session="c1")
    page.logins[-1][2](1)  # (the login's terminal ended with an error: closed, or refused)
    assert page.started == []
    assert "The login of codex on ct1 did not end with a login (exit status 1): not started." in shown[-1][1][0][0]


def test_a_login_configured_otherwise_is_not_asked_for(fleet, monkeypatch):
    needs = prerequisites.login_needs
    assert needs("codex", ["--workspace", "/w"]) == ["codex"]
    assert needs("codex", ["--with", "claude,goose", "--with=hermes"]) == ["codex", "claude", "hermes"]
    assert needs("goose", []) == [] and needs("dsh", []) == []
    for option in ("--no-auth", "--copy-auth", "--auth-file=/x", "--auth-from-session", "--llm", "--api-key",
                   "--offline", "--local-config", "--import-codex-auth"):
        assert needs("codex", ["--workspace", "/w", option, "x"]) == [], option
    assert needs("codex", ["--with", "claude"], skipped={"codex"}) == ["claude"]
    assert needs("claude", ["--secret-env", "ANTHROPIC_API_KEY=anthropic"]) == [], "a key handed in as a variable"
    assert needs("claude", ["--env=CLAUDE_CODE_OAUTH_TOKEN=x"]) == [] and needs("claude", ["--env", "LANG=C"]) == ["claude"]
    # A start with a key: nothing is asked, it starts.
    key = fleet.there.directory / "key.txt"
    page = with_login_terminal(make_board(fleet, monkeypatch, "codex", options=["--no-auth"]))
    page.start(embedded=True, session="c1")
    assert not asked("Log in to codex") and session_of(page.started[-1][0]) == "c1", (widgets.SHOWN, key)


def test_login_status_of_a_host_and_of_an_older_one(fleet, monkeypatch):
    sai = backend.SandboxAI()
    found = sai.host_logins("ct1")
    assert found["codex"] == {"where": "host", "problem": "missing"} and "claude" in found
    assert sai.host_logged_in("ct1", ["codex", "claude"]) == set()
    log_in(fleet, "ct1")
    assert sai.host_logins("ct1")["codex"] == {"where": "store", "problem": None}
    assert sai.host_logged_in("ct1", ["codex", "claude"]) == {"codex"}
    # A sandbox-ai there that has no --status --json (before 0.1.149): its lines are read.
    real = sai.host_run

    def older(host, *args, **kw):
        if args[:3] == ("login", "--status", "--json"):
            return 2, "", "usage: sandbox-ai login\nsandbox-ai login: error: --json goes with --status --sessions"
        return real(host, *args, **kw)
    monkeypatch.setattr(sai, "host_run", older)
    assert sai.host_logins("ct1")["codex"] == {"where": "store", "problem": None}
    assert sai.host_logins("ct1")["claude"]["problem"] == "missing"


def test_what_is_there_under_a_name_on_a_host_and_on_an_older_one(fleet, monkeypatch):
    sai = backend.SandboxAI()
    assert sai.session_seen("ct1", "nothing") is None
    fleet.running("ct1", "qwen", "q1")
    seen = sai.session_seen("ct1", "q1")
    assert (seen["agent"], seen["state"], seen["terminal"], seen["foreground"]) == ("qwen", "running", "starting", False)
    real = sai.host_run

    def older(host, *args, **kw):
        if args[:2] == ("session", "status"):
            return 2, "", "usage: sandbox-ai session ...\nsandbox-ai session: error: argument command: invalid choice: 'status'"
        return real(host, *args, **kw)
    monkeypatch.setattr(sai, "host_run", older)
    seen = sai.session_seen("ct1", "q1")
    assert (seen["agent"], seen["state"], seen["terminal"]) == ("qwen", "running", "starting")
    assert sai.session_seen("ct1", "nothing") is None
    with pytest.raises(ValueError):
        sai.session_seen("ct1", "--evil")


# --- the whole window: the Board in MainWindow, its reads in their threads, the tabs it would open -------------------

@pytest.fixture
def window(fleet, monkeypatch, qapp):
    """The app's main window as the user has it (offscreen), its reads in real threads; the terminal tabs it opens are
    noted instead of made ([(argv, title)])."""
    from sandbox_ai_app import session_start as S
    from sandbox_ai_app.ui import MainWindow
    from sandbox_ai_app.widgets import background
    for module in (prerequisites, S):  # (the real thing here: a thread, its answer back in the window's thread)
        monkeypatch.setattr(module, "background", background)
    made = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if made.tray is not None:
        made.tray.hide()
        made.tray = None
    made.opened, made.opened_tabs = [], []

    def open_tab(argv, title, directory=None, done=None):
        made.opened.append((list(argv), title))
        tab = WindowTab()
        made.opened_tabs.append(tab)
        if done is not None:
            done(tab)
    monkeypatch.setattr(made.terminals, "open", open_tab)
    yield made
    made.shutdown()
    made.close()
    made.deleteLater()


class Ended:
    """A tab's "its command ended" signal, as the window connects to it."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, status):
        for slot in list(self.slots):
            slot(status)


class WindowTab:
    """A terminal tab the window opened: its command's end (bridge.exited), the line above it, what it printed."""

    def __init__(self):
        self.bridge, self.bar, self.text = type("Bridge", (), {"exited": Ended()})(), Bar(), ""

    def start_tail(self):
        return self.text


def board_of(window, fleet, agent, host="ct1", options=("--no-auth",)):
    page = window.board
    workspace = fleet.of(host).workspace
    page.set_board(B.from_argv(page.sai, agent, list(options), [], workspaces=[str(workspace)]), workspace)
    page.host_choice.reload()
    assert page.host_choice.choose(host)
    return page


def test_window_board_start_on_a_host_opens_the_running_sessions_terminal(fleet, window, wait):
    """The report, in the whole window: the Board, a host, Start with the name of a session that runs there. The
    tab that opens is the session's terminal (ssh ... sandbox-ai attach NAME) - not a second start."""
    fleet.running("ct1", "qwen", "q1")
    page = board_of(window, fleet, "qwen")
    page.start_controls.name.setText("q1")
    page.start_controls.start()
    assert wait(lambda: window.opened, 60), [text for _, text, _ in widgets.SHOWN]
    (argv, title), = window.opened
    assert argv == window.sai.attach_argv("ct1", "q1") and title == "ct1: session q1"
    assert "q1 runs already: its terminal is opened" in window.statusBar().currentMessage()


def test_window_board_start_of_another_agent_asks_first(fleet, window, wait, monkeypatch):
    fleet.running("ct1", "qwen", "q1")
    page = board_of(window, fleet, "claude")
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "other")
    page.start_controls.name.setText("q1")
    page.start_controls.start()
    assert wait(lambda: window.opened, 60), [text for _, text, _ in widgets.SHOWN]
    assert "A session named q1 runs already on ct1 - with qwen, not claude." in asked("Session q1")[0]
    (argv, title), = window.opened
    name = session_of(argv)
    assert terminal.start_of_argv(argv) == "claude" and name not in (None, "q1")
    assert page.start_controls.name.text() == name and title == f"ct1: claude ({name})"


def test_window_uses_the_sandboxes_pages_fresh_look(fleet, window, monkeypatch):
    """The Sessions page's instances of that host, when read a moment ago, answer at once; older, the host is asked."""
    fleet.running("ct1", "qwen", "q1")
    sandboxes = window.sandboxes
    sandboxes.remote["ct1"] = window.sai.host_instances("ct1")
    sandboxes.remote_seen["ct1"] = time.time()
    assert [i.name for i in window.fresh_starts("ct1", 5)] == ["q1"]
    page = board_of(window, fleet, "qwen")
    asked_host = []
    monkeypatch.setattr(page.sai, "session_seen", lambda host, name: asked_host.append(name))
    assert page.session_in_the_way("q1", True, lambda name: None) is True
    assert window.opened[-1][0] == window.sai.attach_argv("ct1", "q1") and asked_host == [], "no second read"
    sandboxes.remote_seen["ct1"] = time.time() - 60
    assert window.fresh_starts("ct1", 5) is None and window.fresh_starts("local", 5) is None
    sandboxes.remote_seen["ct1"] = time.time()
    sandboxes.remote_problems["ct1"] = "ssh: timed out"
    assert window.fresh_starts("ct1", 5) is None, "a host that did not answer its last look is asked itself"


def test_window_login_first_then_the_start_goes_on(fleet, window, wait, monkeypatch):
    """The whole way in the window: a Codex start on ct1, which has no Codex login - asked, Log in now, the login's
    tab (ssh ... sandbox-ai login codex), and when it ended with a login the start's own tab opens."""
    page = board_of(window, fleet, "codex", options=())
    monkeypatch.setattr(widgets, "AUTO_CHOICE", "login")
    page.start_controls.name.setText("c1")
    page.start_controls.start()
    assert wait(lambda: window.opened, 90), [text for _, text, _ in widgets.SHOWN]
    (argv, title), = window.opened
    assert argv == window.sai.login_argv("ct1", "codex") and title == "ct1: Log in to codex"
    assert "There is no login for codex on ct1" in asked("Log in to codex")[0]
    log_in(fleet, "ct1")
    window.opened_tabs[0].bridge.exited.emit(0)
    assert wait(lambda: len(window.opened) == 2, 90), [text for _, text, _ in widgets.SHOWN]
    argv, title = window.opened[1]
    assert terminal.start_of_argv(argv) == "codex" and session_of(argv) == "c1" and title == "ct1: codex (c1)"
    assert len(asked("Log in to codex")) == 1


def test_window_a_refused_start_is_said_in_its_tab(fleet, window, wait):
    """The launcher refused the start in its tab after all (the session came between the look and the start): its
    words are said in the line above that terminal, with the buttons - not only an exit status."""
    page = board_of(window, fleet, "claude")
    page.start_controls.name.setText("q1")
    page.start_controls.start()
    assert wait(lambda: window.opened, 60)
    fleet.running("ct1", "qwen", "q1")  # (meanwhile, from elsewhere)
    fleet.there.add_image("localhost/claude-sandbox:latest")
    refused = fleet.there.run("claude", "--session", "q1")
    tab = window.opened_tabs[0]
    tab.text = refused.stderr
    tab.bridge.exited.emit(refused.returncode)
    assert wait(lambda: tab.bar.said, 60)
    text, labels, _ = tab.bar.said[-1]
    assert text.startswith("ct1: Not started: Session q1 is running with qwen, not claude"), text
    assert labels[0] == "Attach to it" and labels[-1] == "Cancel" and len(labels) == 4
