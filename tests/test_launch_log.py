"""The launcher's own log (sandbox/launchlog.py, $XDG_STATE_HOME/sandbox-ai/launcher.log): what a start was asked,
what it decided and why, which outside commands ran and how they ended - and never a secret. User, live use: "I press
Start and nothing happens and no information".
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from live_fakes import Host

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def fresh_log(name="launchlog_under_test"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / "launchlog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SANDBOX_AI_DEBUG", raising=False)
    return fresh_log()


def text(tmp_path):
    path = tmp_path / "state" / "sandbox-ai" / "launcher.log"
    return path.read_text() if path.exists() else ""


def test_info_is_written_debug_only_with_the_switch(log, tmp_path, monkeypatch):
    log.info("start", "asked: claude")
    log.debug("background", "begun: something")
    assert "INFO    start: asked: claude" in text(tmp_path) and "begun: something" not in text(tmp_path)
    monkeypatch.setenv("SANDBOX_AI_DEBUG", "1")
    log.debug("background", "begun: something")
    assert "DEBUG   background: begun: something" in text(tmp_path)
    assert oct((tmp_path / "state" / "sandbox-ai" / "launcher.log").stat().st_mode & 0o777) == "0o600"


def test_a_repeated_line_is_written_once_then_counted(log, tmp_path):
    for _ in range(5):
        log.warning("board", "what the start needs is still being prepared")
    log.info("start", "something else")
    lines = text(tmp_path).splitlines()
    assert sum("still being prepared" in line for line in lines) == 1
    assert any("(said 4 more times)" in line for line in lines)


def test_a_state_is_logged_once_per_change(log, tmp_path):
    for state in ("starting", "starting", "starting", "running", "running", "stopped"):
        log.changed(("session", "s1"), state, "session", f"s1: {state}")
    lines = text(tmp_path).splitlines()
    assert [line.rpartition(": ")[2] for line in lines] == ["starting", "running", "stopped"]


def test_the_log_is_rotated(log, tmp_path, monkeypatch):
    monkeypatch.setattr(log, "LIMIT", 2000)
    for number in range(200):
        log.info("start", f"line {number} " + "x" * 50)
    folder = tmp_path / "state" / "sandbox-ai"
    names = sorted(path.name for path in folder.glob("launcher.log*") if not path.name.endswith(".lock"))
    assert names == ["launcher.log", "launcher.log.1", "launcher.log.2", "launcher.log.3"]
    assert all(path.stat().st_size < 2200 for path in folder.glob("launcher.log*"))
    assert "line 199" in text(tmp_path)


def test_no_secret_reaches_a_line(log, tmp_path):
    """A token in an argument, a secret option's value, NAME=VALUE with a secret name, a value given to secret(), a
    URL's password and a subprocess error that repeats its command: none of them reach the log."""
    log.secret("my-own-secret-value")
    argv = ["claude", "--git-token", "plain-value-1", "--password=plain-value-2", "API_TOKEN=plain-value-3",
            "--git-token-file", "/home/u/token-file", "--", f"push with {TOKEN}"]
    shown = log.command_line(argv)
    log.info("start", f"asked: {shown}")
    log.info("hosts", "ssh said: my-own-secret-value and https://user:" + "pw-secret-9" + "@example.org/x")
    log.info("llm", "header Authorization: Bearer plain-value-6-abcdef")
    log.warning("image", "failed", subprocess.TimeoutExpired(["tool", "--token", "plain-value-4"], 30))
    log.ran("hosts", "ssh call", ["ssh", "host", "--api-key", "plain-value-5"], 0.0, returncode=1,
            stderr=f"error: bad credentials {TOKEN}")
    written = text(tmp_path)
    for secret in ("plain-value-1", "plain-value-2", "plain-value-3", "plain-value-4", "plain-value-5", "plain-value-6", TOKEN,
                   "my-own-secret-value", "pw-secret-9"):
        assert secret not in written, (secret, written)
    assert "/home/u/token-file" in written  # a path is no secret
    assert "no answer within 30 s" in written and "exit status 1" in written


def test_a_run_logs_its_command_time_and_end(log, tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_AI_DEBUG", "1")
    log.run("image", "is the image there", [sys.executable, "-c", "pass"], check=False)
    log.run("image", "a build", [sys.executable, "-c", "import sys; sys.stderr.write('boom: the end\\n'); "
                                                       "sys.exit(3)"], capture_output=True, text=True, check=False)
    with pytest.raises(FileNotFoundError):
        log.run("hosts", "ssh to a host", ["/nonexistent/ssh", "host"])
    written = text(tmp_path)
    assert "DEBUG   image: is the image there:" in written and ": exit status 0" in written
    assert "WARNING image: a build:" in written and "exit status 3; error output: boom: the end" in written
    assert "WARNING hosts: ssh to a host: /nonexistent/ssh host" in written and "FileNotFoundError" in written


def test_a_line_that_cannot_be_written_fails_nothing(tmp_path, monkeypatch):
    blocked = tmp_path / "file"
    blocked.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))  # not a directory
    log = fresh_log()
    log.error("start", "refused")  # no exception


# --- the launcher -----------------------------------------------------------------------------------------------

@pytest.fixture
def launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SANDBOX_AI_DEBUG", raising=False)
    spec = importlib.util.spec_from_file_location("agent_sandbox_for_log_tests", ROOT / "sandbox" / "agent_sandbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LOG.flush()
    return module


def test_a_background_call_says_what_it_does(launcher, tmp_path, monkeypatch):
    """The app's re-check looped without one useful line ("<locals>.read"): a background call says what it does."""
    monkeypatch.setenv("SANDBOX_AI_DEBUG", "1")
    assert launcher.in_background(lambda: 7, what="whether Podman has the start's own images")() == 7
    with pytest.raises(OSError):
        launcher.in_background(lambda: (_ for _ in ()).throw(OSError("podman gone")), what="the lookup")()
    written = text(tmp_path)
    assert "background: begun: whether Podman has the start's own images" in written
    assert "background: done in" in written and "failed after" in written and "the lookup: OSError: podman gone" \
        in written


def test_a_build_that_cannot_run_is_logged(launcher, tmp_path, monkeypatch):
    """build_missing_image swallowed an OSError (returned False, nothing said why): now it is in the log."""
    monkeypatch.delenv(launcher.AUTO_BUILD_OFF, raising=False)

    def broken(*args, **kwargs):
        raise OSError("no python here")
    monkeypatch.setattr(launcher.subprocess, "run", broken)
    a = types.SimpleNamespace(dry_run=False, podman_url=None, image_variant=None)
    assert launcher.build_missing_image(a, "qwen", "localhost/qwen-sandbox:latest") is False
    written = text(tmp_path)
    assert "the build of localhost/qwen-sandbox:latest could not run: OSError: no python here" in written


def test_a_podman_that_does_not_answer_is_logged(launcher, tmp_path, monkeypatch):
    """`podman image exists` timing out was swallowed ("the start says what Podman says"): now logged."""
    monkeypatch.delenv(launcher.IMAGE_CHECK_OFF, raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/" + name)

    def hanging(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
    monkeypatch.setattr(launcher.subprocess, "run", hanging)
    a = types.SimpleNamespace(backend="podman", image="localhost/qwen-sandbox:latest", agent="qwen", helper=None,
                              browser=None, mcp_socket_dir=None, podman_url=None, dry_run=False)
    assert launcher.start_image_checks(a)() == []
    written = text(tmp_path)
    assert "is the image of qwen there: podman image exists localhost/qwen-sandbox:latest" in written
    assert "TimeoutExpired: no answer within 30 s" in written
    assert "Podman gave no answer for localhost/qwen-sandbox:latest (for qwen)" in written


def test_what_the_launcher_says_reaches_the_log(launcher, tmp_path, capsys):
    with launcher.steady_output():
        print("agent-sandbox: /work is /home/u/project; configuration: none", file=sys.stderr)
        print("agent-sandbox: warning: --gui-wayland gives the agent access", file=sys.stderr)
        print("unrelated output of a tool", file=sys.stderr)
    written = text(tmp_path)
    assert "INFO    said: /work is /home/u/project; configuration: none" in written
    assert "WARNING said: warning: --gui-wayland gives the agent access" in written
    assert "unrelated output" not in written
    assert "agent-sandbox: /work is" in capsys.readouterr().err  # the terminal still gets it, unchanged


def test_debug_lines_reach_standard_error_with_the_switch(launcher, monkeypatch, capsys):
    monkeypatch.setenv("SANDBOX_AI_DEBUG", "1")
    launcher.LOG.to_stderr()
    try:
        launcher.LOG.debug("image", "is the image there: podman image exists x")
    finally:
        launcher.LOG.to_stderr(False)
    assert "agent-sandbox: debug: debug image: is the image there" in capsys.readouterr().err


# --- end to end: the real entry point, Podman faked -----------------------------------------------------------

def test_a_start_refused_for_a_missing_image_logs_which_and_why(tmp_path):
    host = Host(tmp_path)
    result = host.run("qwen", "--", f"use {TOKEN}", env={"FAKE_PODMAN_BUILD_FAIL": "1"})
    assert result.returncode != 0
    written = (Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "launcher.log").read_text()
    assert "start: asked: qwen --" in written
    assert "image: localhost/qwen-sandbox:latest (for qwen) is not on this host" in written
    assert "the build of localhost/qwen-sandbox:latest failed" in written
    assert ("the start is refused: the image of qwen (localhost/qwen-sandbox:latest) is not built on this host"
            in written)
    assert "ERROR   start: refused or failed after" in written and "sandbox-ai build qwen" in written
    assert TOKEN not in written


def test_podman_failing_to_answer_is_logged_as_such(tmp_path):
    host = Host(tmp_path)
    result = host.run("qwen", "--dry-run", env={"FAKE_PODMAN_EXISTS_STATUS": "125"})
    assert result.returncode != 0
    written = (Path(host.env["XDG_STATE_HOME"]) / "sandbox-ai" / "launcher.log").read_text()
    assert "WARNING image: is the image of qwen there: podman image exists localhost/qwen-sandbox:latest" in written
    assert "exit status 125" in written
    assert "the start is refused: Podman could not say whether localhost/qwen-sandbox:latest (for qwen)" in written


# --- images and MCP servers -----------------------------------------------------------------------------------

def load_module(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_log_tests", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_mcp_image_check_that_cannot_run_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", raising=False)
    mcp = load_module("mcp")
    monkeypatch.setattr(mcp.shutil, "which", lambda name: "/usr/bin/" + name)

    def broken(argv, **kwargs):
        raise OSError("podman: permission denied")
    assert mcp.image_present("ghcr.io/example/server@sha256:" + "0" * 64, run=broken) is None
    written = text(tmp_path)
    assert "WARNING mcp: is the server image ghcr.io/example/server@sha256:" in written
    assert "OSError: podman: permission denied" in written


def test_an_image_build_that_fails_is_logged(tmp_path, monkeypatch):
    """sandbox-ai build's extension layers: a layer that fails says which and why (it was only on the terminal)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_DEBUG", "1")
    images = load_module("images")
    missing = types.SimpleNamespace(returncode=125, stdout="", stderr="Error: no such image")
    assert images.inspect("localhost/claude-sandbox:base", run=lambda argv, **kw: missing) is None
    monkeypatch.setattr(images, "inspect", lambda *a, **k: None)
    with pytest.raises(ValueError, match="no base image"):
        images.build_layers("claude", run=lambda argv, **kw: missing)
    written = text(tmp_path)
    assert "inspect the image localhost/claude-sandbox:base: podman " in written
    assert "image inspect --format json localhost/claude-sandbox:base" in written
    assert "localhost/claude-sandbox:base is not there (podman image inspect: exit status 125)" in written


# --- other hosts (SSH), model servers, VPN ----------------------------------------------------------------------

def test_an_unreachable_host_is_logged_once_until_it_answers(tmp_path, monkeypatch):
    """The app polls each host's status: SSH failing is said once (with what ssh said), its return once."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    hosts = load_module("hosts")
    hosts.LOG.forget_states()
    hosts.save({"ct1": hosts.check("ct1", {"ssh": "user@ct1"})})
    answers = [types.SimpleNamespace(returncode=255, stdout="", stderr="ssh: connect to host ct1: No route to host")] \
        * 3 + [types.SimpleNamespace(returncode=0, stdout="[]", stderr=""),
               types.SimpleNamespace(returncode=2, stdout="", stderr=f"error: no such start; token {TOKEN}")]
    for _ in range(len(answers)):
        hosts.run("ct1", ["list", "--json", "--token", "plain-value-7"], runner=lambda argv, **kw: answers.pop(0))
    written = text(tmp_path)
    assert written.count("No route to host") == 1
    assert "WARNING hosts: ct1: sandbox-ai list --json --token [" in written and "exit status 255" in written
    assert "INFO    hosts: ct1 answers again" in written
    assert "exit status 2: error: no such start" in written
    assert "plain-value-7" not in written and TOKEN not in written


def test_a_model_server_that_does_not_answer_is_logged_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    llm = load_module("llm")
    llm.LOG.forget_states()
    endpoint = {"name": "box", "kind": "ollama", "url": "http://192.168.1.20:11434",
                "parsed": llm.parse_url("http://192.168.1.20:11434")}

    class Refusing:
        def open(self, request, timeout=None):
            raise OSError("Connection refused")
    client = llm.Client(endpoint, get=lambda name: None, opener=Refusing())
    for _ in range(3):
        with pytest.raises(ValueError, match="Connection refused"):
            client.json("/api/tags")
    written = text(tmp_path)
    assert written.count("GET http://192.168.1.20:11434/api/tags: no answer after") == 1
    assert "Connection refused" in written


def test_a_vpn_baseline_that_cannot_be_asked_is_logged(tmp_path, monkeypatch):
    """host_baseline on another engine swallowed every failure (the checks compared with nothing, silently)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    vpn = load_module("vpn")
    vpn.launcher = lambda: types.SimpleNamespace(POD_NETWORK="pasta")

    def broken(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 120)
    engine = ["podman", "--" + "remote", "--url", "unix:///x"]
    assert vpn.host_baseline({}, podman=engine, run=broken) == {}
    written = text(tmp_path)
    assert "WARNING vpn: this host's own addresses could not be asked on the other engine" in written
    assert "no answer within 120 s" in written


# --- approvals ------------------------------------------------------------------------------------------------

def test_an_automatic_answer_that_fails_is_logged(tmp_path, monkeypatch):
    """--ask allow: an allow that could not be given, and a deny that could not be written either (that was
    swallowed: the question stayed open with nothing said)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    approvals = load_module("approvals")

    def answer_with(ident, decision, scope, **kwargs):
        raise OSError(f"{decision}: the run's directory is gone")
    result = approvals.auto_answer({"kind": "http", "id": "q1", "host": "example.org"}, {"run": "r1"},
                                   answer_with=answer_with)
    assert result["decision"] == "deny"
    written = text(tmp_path)
    assert "approvals: --ask allow could not allow http question q1: denied: OSError: allow: the run's directory" \
        in written
    assert "the automatic deny of q1 could not be written either: OSError: deny: the run's directory is gone" in written


def test_a_question_that_cannot_be_read_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    approvals = load_module("approvals")
    directory = tmp_path / "run" / "approvals"
    directory.mkdir(parents=True)
    (directory / "1-2-3.json").write_text("{damaged")
    told = []

    class Stop:
        calls = 0

        def wait(self, interval):
            Stop.calls += 1
            return Stop.calls > 1
    approvals.watch(directory, Stop(), notify=lambda *args: told.append(args), interval=0)
    assert told == []
    assert "a question in" in text(tmp_path) and "could not be read (1-2-3.json)" in text(tmp_path)
