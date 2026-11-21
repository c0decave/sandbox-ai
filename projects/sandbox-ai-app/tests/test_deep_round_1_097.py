"""Deep test round 1 of 0.1.97's new features in the app: the Shell button, session bridges (the Bridges box, New
bridge… with HOST:NAME, the Board's Bridge part) and the agents OpenHands, nanobot and Letta Code. Each test is named
after the bug it found, or the feature it drives through the real page where none did."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, bridges as BR, i18n, widgets  # noqa: E402
from test_ui import env, sandboxes_tree, window  # noqa: E402,F401 - the fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def test_shell_on_a_host_whose_sandbox_ai_is_older_than_0_1_97_says_to_update_it(window, monkeypatch, wait):
    """Shell on a start of a host whose sandbox-ai is older than 0.1.97 opened a terminal tab that only showed that
    host's usage line ("invalid choice: 'shell'"), where the web interface says to update the host first. The app
    now says so too, from the host's kept status (no SSH), and opens nothing; a host not known yet is tried."""
    page = sandboxes_tree(window, monkeypatch)
    assert wait(lambda: page.table.rowCount() == 4 and page.remote_reading is None, 15)
    opened, warned = [], []
    monkeypatch.setattr(page, "terminal", lambda argv, title, embedded=None, **k: opened.append(argv))
    monkeypatch.setattr(window.sai, "host_terminal", lambda name, *args: ["ssh", "-tt", name, "sandbox-ai", *args])
    hosts = window.sai.module("hosts")
    versions = {"ct1": "0.1.95"}
    monkeypatch.setattr(hosts, "older_than", lambda name, since, path=None: versions.get(name))
    import sandbox_ai_app.ui as ui
    monkeypatch.setattr(ui, "warn", lambda parent, title, text, *a, **k: warned.append(text))
    page.table.selectRow(widgets.find_row(page.table, backend.remote_key("ct1", "web")))
    page.session("shell")
    assert opened == [] and len(warned) == 1
    assert "0.1.95" in warned[0] and "sandbox-ai hosts update ct1" in warned[0]
    versions.clear()  # updated (or not known yet): the shell is opened
    page.session("shell")
    assert opened == [["ssh", "-tt", "ct1", "sandbox-ai", "shell", "web"]]
    i18n.set_language("de")
    versions["ct1"] = "0.1.90"
    page.session("shell")
    assert "braucht 0.1.97" in warned[-1]


def test_events_of_a_bridge_across_hosts_are_shown_in_german_too():
    """The events of a bridge across hosts name the other host's session as HOST:NAME (and the other half's creation
    says "(across hosts)"): the app's patterns took only plain names, so Monitoring showed these in English in a German
    app, and their reasons too."""
    i18n.set_language("de")
    cases = (
        ("created", "bridge 1234abcd web -> lab1:docs: message,watch",
         "Brücke 1234abcd web -> lab1:docs: message,watch"),
        ("created", "bridge 1234abcd lab1:web -> docs (across hosts): message; back: watch",
         "Brücke 1234abcd lab1:web -> docs: message; " + i18n.tr("back: {caps}", caps="watch")),
        ("message", "bridge 1234abcd: message from web to lab1:docs (12 bytes)",
         "Brücke 1234abcd: Nachricht von web an lab1:docs (12 Bytes)"),
        ("ended", "bridge 1234abcd ended: lab1:docs has ended", "lab1:docs hat geendet"),
    )
    for event, summary, german in cases:
        shown = BR.event_summary({"kind": "bridge", "summary": summary, "details": {"event": event}})
        assert shown != summary and german in shown, (summary, shown)
    assert BR.reason_text("lab1:docs has ended") == "lab1:docs hat geendet"


def test_a_watched_agent_is_not_warned_that_it_does_not_see_the_bridge(qapp, tmp_path):
    """New bridge… and the Board's Bridge part warned that a watched agent other than Claude Code "does not see" the
    bridge - the observer preset's own use: it gets no tool of the bridge but info and needs none. Only an agent that
    reaches the other session, or gets an inbox (messages, questions, hints) or both directions, is warned."""
    from sandbox_ai_app.bridges_view import BridgeDialog
    sessions = [{"name": "guard", "agent": "claude", "workspace": str(tmp_path / "g"), "host": None, "run": "r1"},
                {"name": "beta", "agent": "codex", "workspace": str(tmp_path / "b"), "host": None, "run": "r2"}]
    dialog = BridgeDialog(None, sessions, first="guard", second="beta")
    try:
        dialog.watch.setChecked(True)
        assert "beta runs codex" not in dialog.note.text()
        dialog.actions["hint"].setChecked(True)
        assert "beta runs codex" in dialog.note.text(), "a hint goes to its inbox"
        dialog.actions["hint"].setChecked(False)
        dialog.both.setChecked(True)
        assert "beta runs codex" in dialog.note.text()
        dialog.both.setChecked(False)
        dialog.message.setChecked(True)
        assert "beta runs codex" in dialog.note.text()
    finally:
        dialog.deleteLater()
    board = B.Board()
    for kind in ("agent", "proxy", "network"):
        board.add(kind)
    board.add("workspace", path=str(tmp_path))
    board.first("agent").props["agent"] = "codex"
    node = board.add("bridge", peer="guard", direction="in", watch=True, actions=["alert"])
    warned = lambda: [p for p in B.bridge_problems(board) if "reads its MCP servers" in p.text]  # noqa: E731
    assert warned() == [], "watched only: nothing to see"
    node.props["message"] = True
    assert warned(), "an inbox to read"
    node.props.update(message=False, direction="out")
    assert warned(), "it reaches the other session itself"


@pytest.fixture
def user_host(tmp_path, monkeypatch):
    """A user's host (tests/live_fakes.py: the real ./sandbox-ai with only Podman, tmux and npm faked, the three new
    agents' images in the user's store) as the app's own process sees it."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image(*[f"localhost/{agent}-sandbox:latest" for agent in ("openhands", "nanobot", "letta", "opencode2")])
    for key in list(os.environ):
        if key not in host.env and key not in ("SANDBOX_AI_HOME", "LD_LIBRARY_PATH", "QT_QPA_PLATFORM", "TMPDIR"):
            monkeypatch.delenv(key)
    for key, value in host.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(host.workspace)
    return host


@pytest.mark.parametrize("agent, enable, feature, host_name", [
    ("openhands", "--openhands-enable", "model-catalog", "raw.githubusercontent.com"),
    ("nanobot", "--nanobot-enable", "web-search", "duckduckgo.com"),
    ("letta", "--letta-enable", "cloud", "api.letta.com"),
    ("opencode2", "--opencode2-enable", "models-catalog", "models.opencode.ai"),
])
def test_the_boards_new_agents_with_their_features_pass_the_launchers_dry_run(user_host, agent, enable, feature,
                                                                              host_name):
    """End to end where only unit tests were: a Board with OpenHands, nanobot or Letta Code and one of its
    --AGENT-enable features, turned into the launcher's words (to_argv) and checked by the real ./sandbox-ai's dry run
    (the app's check before a start): the feature on with its hosts, telemetry still pinned off; the agent's argument
    the launcher refuses is refused with its reason."""
    sai = backend.SandboxAI()
    board = B.from_argv(sai, agent, [enable, feature])
    argv = B.to_argv(sai, board)
    assert argv.count(enable) == 1
    plan, error = sai.dry_run(agent, [*argv, "--workspace", str(user_host.workspace)])
    assert plan is not None, error
    assert [item["feature"] for item in plan[agent]["enabled"]] == [feature]
    assert host_name in plan["https_hosts"]
    assert plan[agent]["telemetry"]["environment"], "the telemetry opt-outs hold with every feature"
    hostile = {"openhands": ["--", "-H0.0.0.0"], "nanobot": ["--", "-c/tmp/other.json"],
               "letta": ["--", "server", "--listen", "ws://0.0.0.0:1"],
               "opencode2": ["--", "pair", "--remote", "--yes"]}[agent]
    plan, error = sai.dry_run(agent, [*hostile, "--workspace", str(user_host.workspace)])
    assert plan is None and "arguments:" in error, error
