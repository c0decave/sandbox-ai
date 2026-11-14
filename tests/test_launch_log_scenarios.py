"""launcher.log read as a user debugging a problem reads it, scenario by scenario (logging round 2): the real
./sandbox-ai entry point with Podman, tmux and ssh faked (live_fakes.py) - a start that works, one refused, one Podman
fails, an MCP server that does not come up, a session's life, a slow start, a VPN whose check fails, build all with a
failure, a host SSH cannot reach, approvals nobody answered, net cut and allow, stop --all - each must say from the log
alone what happened and why, once, and never a secret.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types

import pytest

from live_fakes import Host
from test_live_regressions import Relay, wait_for

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
PASTA = 'Error: could not find pasta, the network tool needed by rootless Podman: exec: "pasta": executable file ' \
        'not found in $PATH'


def written(host):
    path = Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "launcher.log"
    return path.read_text() if path.exists() else ""


@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.add_image("localhost/claude-sandbox:latest", "localhost/qwen-sandbox:latest")
    return made


def load(name, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SANDBOX_AI_DEBUG", raising=False)
    spec = importlib.util.spec_from_file_location(f"{name}_for_scenarios", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LOG.forget_states()
    return module


def log_text(tmp_path):
    path = tmp_path / "state" / "sandbox-ai" / "launcher.log"
    return path.read_text() if path.exists() else ""


# --- the log itself -------------------------------------------------------------------------------------------------

def fresh_log(tmp_path, monkeypatch, name):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / "launchlog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_state_is_said_once_across_processes(tmp_path, monkeypatch):
    """The app polls `list --json` every few seconds, each a process of its own: each said "q1: running" again."""
    first, second = fresh_log(tmp_path, monkeypatch, "log_a"), fresh_log(tmp_path, monkeypatch, "log_b")
    assert first.changed(("session", "q1"), "running", "session", "q1: running")
    assert not second.changed(("session", "q1"), "running", "session", "q1: running")
    assert second.state(("session", "q1")) == "running"
    assert second.changed(("session", "q1"), "stopped", "session", "q1: stopped")
    assert first.changed(("session", "q1"), "running", "session", "q1: running")
    assert [line.rpartition(": ")[2] for line in log_text(tmp_path).splitlines()] == ["running", "stopped", "running"]
    assert first.changed(("noted",), True, "noted", None)  # (noted, not said)
    assert second.state(("noted",)) is True and "noted" not in log_text(tmp_path)


def test_the_kept_states_are_bounded(tmp_path, monkeypatch):
    log = fresh_log(tmp_path, monkeypatch, "log_bounded")
    monkeypatch.setattr(log, "STATES_KEPT", 20)
    for number in range(50):
        log.changed(("poll", number), "failed", "x", None)
    kept = json.loads(log.states_path().read_text())
    assert len(kept) == 20 and oct(log.states_path().stat().st_mode & 0o777) == "0o600"


def test_a_long_error_keeps_its_first_and_last_lines(tmp_path, monkeypatch):
    """A session's start error quoted its whole supervisor.log, three times over."""
    log = fresh_log(tmp_path, monkeypatch, "log_long")
    text = "Session failed to start (status 1):\n" + "\n".join(f"agent-sandbox: step {n}" for n in range(40))
    log.error("start", "refused or failed", RuntimeError(text))
    line = log_text(tmp_path)
    assert "RuntimeError: Session failed to start (status 1): | ... | agent-sandbox: step 37 |" in line
    assert "step 39" in line and "step 10" not in line


def test_a_slow_command_is_said_while_it_runs(tmp_path, monkeypatch):
    """A start held by Podman (a lock, a pull) said nothing until the command ended."""
    log = fresh_log(tmp_path, monkeypatch, "log_slow")
    monkeypatch.setattr(log, "SLOW_SECONDS", 0.2)
    log.run("pod", "create the pod agent-x", ["podman", "pod", "create", "--name", "agent-x"],
            runner=lambda argv, **kw: (time.sleep(0.6), types.SimpleNamespace(returncode=0, stderr=""))[1])
    log.run("pod", "quick", ["podman", "version"], runner=lambda argv, **kw: types.SimpleNamespace(returncode=0))
    time.sleep(0.4)
    said = log_text(tmp_path)
    assert "INFO    pod: still running after 0.2 s: create the pod agent-x: podman pod create --name agent-x" in said
    assert "quick" not in said


# --- starts ---------------------------------------------------------------------------------------------------------

def test_a_start_that_works_says_its_pod_and_end(host):
    result = host.run("claude", env={"FAKE_PODMAN_RUN": "0:0"})
    assert result.returncode == 0, result.stderr
    log = written(host)
    assert "start: checked: agent claude" in log and "in this terminal" in log
    assert "pod: the pod agent-claude-" in log and "is created; its sandbox starts" in log
    assert "INFO    pod: the sandbox of agent-claude-" in log and "ended with exit status 0" in log
    assert "pod: removed the pod agent-claude-" in log
    assert "INFO    start: ended with exit status 0" in log


def test_an_agent_that_ends_at_once_is_a_warning(host):
    result = host.run("claude", env={"FAKE_PODMAN_RUN": "1:0"})
    assert result.returncode == 1
    log = written(host)
    assert "WARNING pod: the sandbox of agent-claude-" in log and "ended with exit status 1 after 0 s - at once" in log
    assert "WARNING start: ended with exit status 1" in log


def test_a_refused_option_is_in_the_log(host):
    """argparse's refusal was on the terminal only: the log said 'ended (exit 2)'."""
    result = host.run("claude", "--bogus-option")
    assert result.returncode == 2
    log = written(host)
    assert "WARNING said: sandbox-ai: error: unrecognized arguments: --bogus-option" in log
    assert "WARNING start: ended (exit 2)" in log


def test_an_untrusted_project_file_says_which_and_why(host):
    (host.workspace / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude"}))
    result = host.run("claude", "--dry-run")
    assert result.returncode != 0
    log = written(host)
    assert "ERROR   start: refused or failed after" in log
    assert f"Project configuration {host.workspace.resolve() / '.sandbox-ai.json'} is not trusted" in log


def test_podman_failing_at_the_pod_says_podmans_reason_and_the_help(host):
    """`pod create` failing (exit 125, pasta missing) left only "exit status 125" in the log, with a wrong guess (a
    pod of that name someone else's)."""
    result = host.run("claude", env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_POD_CREATE_FAIL": f"125:{PASTA}"})
    assert result.returncode == 1
    assert PASTA in result.stderr  # the terminal still has Podman's own message
    log = written(host)
    assert f"exit status 125; error output: {PASTA}" in log
    assert "Podman did not create the pod agent-claude-" in log and \
        "-> the network needs pasta: install the package passt" in log
    assert "someone else's" not in log


def test_podman_failing_at_the_lookup_says_why(host):
    result = host.run("claude", "--allow-domain", "example.org",
                      env={"FAKE_PODMAN_LOOKUP_FAIL": "125:Error: could not find pasta: executable file not found"})
    assert result.returncode == 1
    log = written(host)
    assert "ERROR   start: refused or failed after" in log and "Podman failed (exit status 125) before the sandbox " \
        "started: Error: could not find pasta" in log and "-> the network needs pasta" in log


def test_an_mcp_server_that_does_not_come_up_says_which_and_why_once(host):
    """The demo server's sandbox needs the codex image, which is not there: its own start says which image, the
    start says which server, and the long error is not repeated in every line."""
    host.add_image("localhost/mcp-demo:latest")
    result = host.run("claude", "--mcp", "demo", env={"FAKE_PODMAN_RUN": "0:0"})
    assert result.returncode == 1
    log = written(host)
    assert "start: checked: agent codex" in log and "an MCP server's sandbox, name agent-mcp-demo" in log
    assert "WARNING pod: the start is refused: the image localhost/codex-sandbox:latest of codex is not on this host" \
        in log
    assert "mcp: demo: did not come up after" in log
    assert "WARNING mcp: the MCP server demo did not come up for the start (RuntimeError, said above)" in log
    assert log.count("MCP server sandbox failed to start") == 2  # mcp.py's line and the start's end


# --- sessions -------------------------------------------------------------------------------------------------------

@pytest.fixture
def session(host):
    """The session q1 of qwen, running (its sandbox runs 60 s; its terminal relay is there): (host, env)."""
    env = {"FAKE_PODMAN_RUN": "0:60"}
    socket_path = host.home / ".local/state/sandbox-ai/sessions/q1/tmux/attach.sock"
    start = host.popen("qwen", "--session", "q1", env=env)
    wait_for(lambda: any(call[:2] == ["pod", "create"] for call in host.calls()))  # (its tmux folder made anew)
    relay = Relay(socket_path)
    _, err = start.communicate(timeout=120)
    assert start.returncode == 0, err
    wait_for(lambda: "q1" in host.run("net", "list", env=env).stdout)  # (its run registered)
    yield host, env
    host.run("stop", "q1", env=env)
    relay.close()


def test_a_session_life_is_in_the_log(session):
    host, env = session
    for _ in range(3):
        assert host.run("list", "--json", env=env).returncode == 0
    assert host.run("attach", "q1", env=env).returncode == 0
    assert host.run("stop", "q1", env=env).returncode == 0
    assert host.run("delete", "q1", "--yes", env=env).returncode == 0
    log = written(host)
    # the start: the parent said what its supervisor said, which logged it itself - once
    assert log.count("said: starting the sandbox: its firewall, connection proxy and the agent") == 1
    assert "the supervisor of a session (its terminal: tmux), name q1" in log
    assert "session: q1: its terminal is ready after" in log
    # three polls: its state once
    assert log.count("session: q1: running: its supervisor") == 1
    assert "command: asked: sandbox-ai list" not in log  # (a look: DEBUG)
    # attach and its detach, stop, delete: asked and how they ended
    assert "INFO    command: asked: sandbox-ai attach q1" in log
    assert "command: sandbox-ai attach q1: done after" in log and "detached from q1; it keeps running" in log
    assert "session: q1: stop: SIGTERM to its supervisor" in log
    assert "ended by SIGTERM: stopped" in log
    assert "command: sandbox-ai stop q1: done after" in log and "session q1 stopped" in log
    assert "command: sandbox-ai delete q1 --yes: done after" in log and "session q1 deleted" in log


def test_a_kill_switch_on_a_starting_session_says_it_waits(session):
    host, env = session
    result = host.run("net", "cut", "q1", "remote", env=env)
    assert result.returncode == 0, result.stderr
    log = written(host)
    assert "INFO    command: asked: sandbox-ai net cut q1 remote" in log
    assert "net: kill switch remote for q1 (run qwen-" in log and "waiting: q1 is starting" in log
    refused = host.run("net", "allow", "q1", "-A", "example.org", "-y", env=env)
    assert refused.returncode == 1
    assert "WARNING command: sandbox-ai net allow q1 -A example.org -y: exit status 1 after" in written(host)
    assert "the sandbox does not answer" in written(host)


def test_mcp_attach_and_detach_say_their_result(session):
    host, env = session
    assert host.run("mcp", "attach", "demo", "q1", "--live", env=env).returncode == 1  # (its relay: none here)
    assert host.run("mcp", "attach", "demo", "q1", env=env).returncode == 0
    assert host.run("mcp", "detach", "demo", "q1", env=env).returncode == 0
    log = written(host)
    assert "WARNING command: sandbox-ai mcp attach demo q1 --live: exit status 1 after" in log and \
        "the sandbox does not answer" in log
    assert "INFO    mcp: attach demo to q1: after a restart" in log
    assert "INFO    mcp: detach demo from q1: next start" in log


def test_a_slow_session_start_says_what_it_waits_for(tmp_path, monkeypatch):
    sessions = load("sessions", tmp_path, monkeypatch)
    directory = tmp_path / "s1"
    (directory / "tmux").mkdir(parents=True)
    (directory / "supervisor.log").write_text("agent-sandbox: starting the MCP server fetch\n")
    now = [0.0]

    def clock():
        return now[0]
    ready_after = [0]

    def ready():
        ready_after[0] += 1
        now[0] += 5
        return ready_after[0] > 10  # 50 s

    monkeypatch.setattr(sessions.time, "sleep", lambda seconds: None)
    process = types.SimpleNamespace(pid=4242, poll=lambda: None, returncode=None)
    sessions.wait_ready(directory, process, ready=ready, follow=0, out=lambda text: None, clock=clock, bar=False)
    said = log_text(tmp_path)
    assert "session: s1: still starting after 15 s: waiting for its terminal (attach.sock); its supervisor " \
        "(PID 4242) said last: starting the MCP server fetch" in said
    assert "still starting after 30 s" in said and said.count("still starting after") == 2  # 15, 30 (60: ready)


# --- VPN ------------------------------------------------------------------------------------------------------------

def vpn_profile():
    return {"name": "tor1", "type": "tor"}


def test_a_vpn_whose_check_fails_says_which_check(tmp_path, monkeypatch):
    vpn = load("vpn", tmp_path, monkeypatch)

    class Tunnel:
        def __init__(self, command, data, say):
            self.closed = False

        def wait_ready(self, timeout):
            return {"tunnel": {"endpoints": ["192.0.2.9"]}, "checks": {}}

        def close(self):
            self.closed = True
    monkeypatch.setattr(vpn, "Tunnel", Tunnel)
    monkeypatch.setattr(vpn, "payload", lambda *args: {"checks": []})
    monkeypatch.setattr(vpn, "baseline_of", lambda checks, podman: {})
    monkeypatch.setattr(vpn, "ready_timeout", lambda profile: 30)
    monkeypatch.setattr(vpn, "report", lambda *args: None)
    monkeypatch.setattr(vpn, "evaluate", lambda *args: {
        "failures": ["the address seen inside (203.0.113.7) is this host's own"], "warnings": [], "notes": []})
    with pytest.raises(ValueError, match="the checks failed"):
        vpn.bring_up(vpn_profile(), ["podman", "run", "--env", f"TOKEN={TOKEN}", "x"], [], say=lambda text: None,
                     run="claude-1")
    said = log_text(tmp_path)
    assert "INFO    vpn: the VPN tor1 (tor, run claude-1): its container starts (serve); waiting up to 30 s" in said
    assert "vpn: the VPN tor1 (tor, run claude-1): its tunnel is up after" in said
    assert "ERROR   vpn: the VPN tor1 (tor, run claude-1): the checks failed, the start is stopped: the address " \
        "seen inside (203.0.113.7) is this host's own" in said
    assert "did not come up" not in said and TOKEN not in said


def test_a_vpn_that_does_not_come_up_says_why(tmp_path, monkeypatch):
    vpn = load("vpn", tmp_path, monkeypatch)

    class Tunnel:
        def __init__(self, command, data, say):
            pass

        def wait_ready(self, timeout):
            raise ValueError("the VPN container ended before the tunnel was up: tor: bootstrap failed")

        def close(self):
            pass
    monkeypatch.setattr(vpn, "Tunnel", Tunnel)
    monkeypatch.setattr(vpn, "payload", lambda *args: {"checks": []})
    monkeypatch.setattr(vpn, "baseline_of", lambda checks, podman: {})
    with pytest.raises(ValueError):
        vpn.bring_up(vpn_profile(), ["podman", "run", "x"], [], say=lambda text: None)
    assert "WARNING vpn: the VPN tor1 (tor): did not come up after" in log_text(tmp_path)
    assert "ValueError: the VPN container ended before the tunnel was up: tor: bootstrap failed" in log_text(tmp_path)


# --- builds, hosts --------------------------------------------------------------------------------------------------

def test_build_all_with_one_failure_says_each_build_and_its_end(tmp_path):
    host = Host(tmp_path)
    result = host.run("build", "all", env={"FAKE_PODMAN_BUILD_FAIL": "localhost/team-sandbox"}, timeout=900)
    assert result.returncode == 1
    log = written(host)
    assert "INFO    command: asked: sandbox-ai build all" in log
    # every build its end (team, Cairn, Tor and the browser were `exec podman build`: no end at all)
    assert log.count("build: asked: sandbox-ai build ") == log.count("build: ended with exit status") >= 12
    lines = log.splitlines()
    team = next(n for n, line in enumerate(lines) if line.endswith("build: asked: sandbox-ai build team"))
    assert "WARNING build: ended with exit status 1" in lines[team + 1] and "on the terminal" in lines[team + 1]
    assert "WARNING command: sandbox-ai build all: exit status 1 after" in log and "failed: team" in log


def test_a_host_ssh_cannot_reach_says_so_when_asked(tmp_path):
    host = Host(tmp_path)
    assert host.run("hosts", "add", "ct1", "user@ct1").returncode == 0
    assert host.run("hosts", "run", "ct1", "list").returncode == 255
    assert host.run("hosts", "run", "ct1", "--", "claude", "--git-token", TOKEN).returncode == 255
    assert host.run("hosts", "test", "ct1").returncode == 1
    log = written(host)
    assert "WARNING hosts: ct1: sandbox-ai list (hosts run, " in log and "exit status 255: SSH did not reach ct1 " \
        "(ssh user@ct1:22" in log
    assert log.count("SSH did not reach ct1") == 2  # asked twice: said twice (not once per change, as a poll)
    assert "WARNING hosts: test ct1: ssh failed; ssh: connect to host ct1 port 22: No route to host" in log
    assert "WARNING command: sandbox-ai hosts test ct1: exit status 1" in log
    assert TOKEN not in log


# --- approvals, net, stop --all, liveness ---------------------------------------------------------------------------

def test_a_question_nobody_answered_is_logged(tmp_path, monkeypatch):
    approvals = load("approvals", tmp_path, monkeypatch)
    directory = tmp_path / "run" / "approvals"
    directory.mkdir(parents=True)
    asking = {"1-2-3.json": "connect to example.org:443", "4-5-6.json": "push to origin",
              "7-8-9.json": "a lookup of x.org"}
    (directory / "1-2-3.done").write_text(json.dumps({"decision": "deny", "scope": "once",
                                                      "reason": "no answer within 120 s"}))
    (directory / "4-5-6.done").write_text(json.dumps({"decision": "deny", "scope": "once",
                                                      "reason": "could not be applied: nft failed"}))
    (directory / "7-8-9.json").write_text("{}")
    approvals.settled(directory, asking, "claude-1")
    said = log_text(tmp_path)
    assert "INFO    approvals: claude-1: question 1-2-3 refused: no answer within 120 s (nobody answered): " \
        "connect to example.org:443" in said
    assert "WARNING approvals: claude-1: question 4-5-6 was allowed, but refused: could not be applied: nft failed" \
        in said
    assert list(asking) == ["7-8-9.json"]  # still open


def test_a_kill_switch_says_each_runs_result(tmp_path, monkeypatch):
    net = load("net", tmp_path, monkeypatch)
    runs = {"claude-1": {"run": "claude-1", "name": "s1", "net_cut": True, "directory": str(tmp_path)},
            "codex-2": {"run": "codex-2", "name": "s2", "net_cut": True, "directory": str(tmp_path)}}

    def cut_one(record, mode, by, clear=False, timeout=None):
        if record["run"] == "codex-2":
            raise ValueError("its supervisor did not confirm the kill switch all")
        return {"cut": mode, "cuts": {name: mode for name in by}}
    monkeypatch.setattr(net, "cut_one", cut_one)
    monkeypatch.setattr(net, "related_runs", lambda runs, entry: [])
    monkeypatch.setattr(net, "running", lambda *args: runs)
    assert net.cut_runs(runs, None, "all", out=lambda text: None, every=True) == 1
    said = log_text(tmp_path)
    assert "INFO    net: kill switch all for s1 (run claude-1): confirmed by its supervisor; it holds now: all" in said
    assert "WARNING net: kill switch all for s2 (run codex-2) NOT set: its supervisor did not confirm" in said


def test_stop_all_says_what_it_stops_and_how_it_ended(tmp_path, monkeypatch):
    starts = load("starts", tmp_path, monkeypatch)
    monkeypatch.setattr(starts, "entries", lambda: [{"name": "s1", "kind": "session", "state": "running"},
                                                    {"name": "r2", "kind": "run", "state": "paused"},
                                                    {"name": "m", "kind": "mcp", "state": "running"}])

    def act(command, name, out, wait=None):
        if name == "r2":
            raise ValueError("r2 has ended meanwhile")
    monkeypatch.setattr(starts, "act", act)
    assert starts.stop_all(out=lambda text: None, yes=True, inside=False) == 1
    said = log_text(tmp_path)
    assert "INFO    starts: stop --all: 2 to stop: s1 (running), r2 (paused)" in said
    assert "WARNING starts: stop --all: r2 was not stopped: ValueError: r2 has ended meanwhile" in said
    assert "WARNING starts: stop --all: stopped 1 of 2; not stopped: r2" in said


def test_an_empty_pod_listing_says_why(tmp_path, monkeypatch):
    """`podman pod ps` ending well with no list was logged as "does not answer ...: " - with no reason."""
    liveness = load("liveness", tmp_path, monkeypatch)
    answers = [types.SimpleNamespace(returncode=0, stdout="", stderr=""),
               types.SimpleNamespace(returncode=0, stdout="[]", stderr=""),
               types.SimpleNamespace(returncode=0, stdout="[]", stderr="")]
    monkeypatch.setattr(liveness, "RUNNER", lambda argv, **kw: answers.pop(0))
    for _ in range(3):
        liveness.pods(fresh=True)
    said = log_text(tmp_path)
    assert "WARNING liveness: Podman does not answer podman pod ps (whether a run's pod still runs stays unknown): " \
        "exit status 0, but no list of pods: no output" in said
    assert said.count("Podman answers podman pod ps again") == 1


def test_a_first_answer_of_podman_is_not_said_again(tmp_path, monkeypatch):
    """Each poll's process said "Podman answers podman pod ps again" although it never failed."""
    liveness = load("liveness", tmp_path, monkeypatch)
    monkeypatch.setattr(liveness, "RUNNER", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="[]",
                                                                                     stderr=""))
    liveness.pods(fresh=True)
    assert "answers podman pod ps again" not in log_text(tmp_path)


# --- secrets and floods ---------------------------------------------------------------------------------------------

def test_debug_mode_leaks_no_secret(host, tmp_path):
    """SANDBOX_AI_DEBUG=1 adds every outside command: a token in the agent's arguments, in a secret option and in the
    environment given to the sandbox reaches neither launcher.log nor what the launcher says."""
    env = {"FAKE_PODMAN_RUN": "0:0", "SANDBOX_AI_DEBUG": "1", "MY_API_TOKEN": TOKEN}
    result = host.run("claude", "--env", "MY_API_TOKEN", "--", f"use {TOKEN}", env=env)
    assert result.returncode == 0, result.stderr
    assert host.run("net", "allow", "nothing-runs", "--git-token-env", f"https://github.com={TOKEN}",
                    env=env).returncode != 0
    log = written(host)
    assert "DEBUG" in log and "agent-sandbox: debug:" in result.stderr
    assert TOKEN not in log and TOKEN not in result.stderr


def test_polls_do_not_flood_the_log(host):
    """What the app polls (list --json, approvals list --json, net list) writes nothing while nothing changes."""
    for _ in range(5):
        for args in (("list", "--json"), ("approvals", "list", "--json"), ("net", "list", "--json"), ("mcp", "list")):
            assert host.run(*args).returncode == 0
    assert written(host) == ""
