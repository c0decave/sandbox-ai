"""A session that runs without its terminal yet (user report from live use, 2026-11-14): listed as plain "running",
attaching failed. Now Sandboxes & sessions and the Terminals page's Sessions say "starting" with the step its start
is at, while its supervisor still reports progress, and "no terminal (see log)" with the end of its supervisor.log
when it stays without one - its Attach then disabled with that reason (sandbox-ai's sessions.terminal_state)."""
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, terminal  # noqa: E402
from sandbox_ai_app.ui import terminal_of  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests


def session(name, **record):
    return backend.Instance("session", f"session:{name}", name, run=f"codex-{name}", session=name, agent="codex",
                            state="running", record=record)


def test_the_rows_say_starting_and_no_terminal(window, monkeypatch):
    page = window.sandboxes
    starting = session("slow", terminal="starting", terminal_step="starting the MCP server fetch…")
    missing = session("stuck", terminal="missing", terminal_log="Error: the VPN tunnel did not come up")
    ready = session("web", terminal="ready")
    assert [terminal_of(i) for i in (starting, missing, ready)] == ["starting", "missing", None]
    row = page.start_row(starting)
    assert row["cells"][6] == "starting: starting the MCP server fetch…" and row["color"] == "warn"
    assert "Attach waits for it" in row["tip"]
    row = page.start_row(missing)
    assert row["cells"][6] == "no terminal (see log)" and row["color"] == "bad"
    assert "Error: the VPN tunnel did not come up" in row["tip"]
    assert page.start_row(ready)["cells"][6] == "running"
    # Attach of the one without a terminal: disabled, the reason with its log's end.
    monkeypatch.setattr(page, "current", lambda: missing)
    monkeypatch.setattr(page, "several", lambda: False)
    page.update_buttons()
    attach = page.session_buttons["attach"]
    assert not attach.isEnabled() and "Error: the VPN tunnel did not come up" in attach.toolTip()
    assert page.stop_button.isEnabled(), "it can be stopped"
    monkeypatch.setattr(page, "current", lambda: starting)
    page.update_buttons()
    assert page.session_buttons["attach"].isEnabled(), "attach waits for a start that goes on"


def test_the_terminals_sessions_list_says_it_too(window):
    page = window.terminals
    page.entries = terminal.grouped("local", [
        {"name": "slow", "kind": "session", "agent": "codex", "state": "running", "attachable": True,
         "terminal": "starting", "terminal_step": "preparing the kits and plugins…"},
        {"name": "stuck", "kind": "session", "agent": "codex", "state": "running", "attachable": False,
         "attach_why": "no-terminal", "terminal": "missing", "terminal_log": "Error: no relay"}])
    page.shown_rows = None
    page.show_starts()
    states = {page.list.item(r, 0).text(): page.list.item(r, 2).text() for r in range(page.list.rowCount())}
    assert states == {"slow": "starting: preparing the kits and plugins…", "stuck": "no terminal (see log)"}
    row = next(r for r in range(page.list.rowCount()) if page.list.item(r, 0).text() == "stuck")
    assert "Runs without its terminal" in page.list.item(row, 4).text()


def test_the_backend_reads_it_from_the_sessions_files(window, env):
    """SandboxAI.session_terminal: sandbox-ai's sessions.terminal_state, files only."""
    sessions = window.sai.module("sessions")
    if not hasattr(sessions, "terminal_state"):
        pytest.skip("a sandbox-ai without terminal_state")
    directory = sessions.session_dir("stuck")
    (directory / "tmux").mkdir(parents=True)
    sessions.save("stuck", {"agent": "codex", "options": [], "agent_args": [], "backend": "podman",
                            "workspace": "/w", "cwd": "/", "created": "2026-11-01T00:00:00Z",
                            "started": "2026-11-01T00:00:00Z"})
    log = directory / "supervisor.log"
    log.write_text("agent-sandbox: starting the sandbox: its firewall, connection proxy and the agent…\nboom\n")
    os.utime(log, (time.time() - 3600, time.time() - 3600))
    found = window.sai.session_terminal("stuck")
    assert found["terminal"] == "missing" and found["terminal_log"].endswith("boom")
    os.utime(log, None)  # said something just now: still starting, at that step
    found = window.sai.session_terminal("stuck")
    assert found == {"terminal": "starting",
                     "terminal_step": "starting the sandbox: its firewall, connection proxy and the agent…"}
    (directory / "tmux" / "attach.sock").touch()
    assert window.sai.session_terminal("stuck") == {"terminal": "ready"}
    assert json.dumps(found)
