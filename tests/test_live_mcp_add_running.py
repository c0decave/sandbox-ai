"""User reports 2026-11-20, a Codex session that ran ~28 h: "sollte mcp server hinzufuegen ... crash" - the tab said
"bright-swallow-paddle has ended (exit status 143)" after the logs could not be written (No space left on device),
while binwalk's container could not start (podman exit status 125); then adding radare2 said "--mount /work=/work:rw:
/work is not there and cannot be created (/ is not a folder you can write)".

End to end through ./sandbox-ai with live_fakes.py (a running session stand-in: its supervisor a process of the test,
its run registered), plus the pieces a full disk touches, each with a stand-in for the full file system.
"""
import errno
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from live_fakes import Host, ROOT

SANDBOX = ROOT / "sandbox"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SANDBOX / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


@pytest.fixture
def session(host):
    """A running session "web" of HOST's user as ./sandbox-ai finds it (its supervisor: a process whose command line
    names the session's tmux folder, as the launcher's does; its run registered with its workspace)."""
    state = host.home / ".local/state/sandbox-ai/sessions/web"
    tmux = state / "tmux"
    for folder in (state / "home", tmux):
        folder.mkdir(parents=True)
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "agent_sandbox.py",
                                   "--tmux-socket-dir", str(tmux)])
    (state / "session.json").write_text(json.dumps({
        "agent": "codex", "options": ["--backend", "podman"], "agent_args": [], "backend": "podman",
        "workspace": str(host.workspace), "cwd": str(host.workspace), "created": "2026-11-19T13:56:00Z",
        "pod": "agent-session-web", "name": "web", "pid": supervisor.pid, "started": "2026-11-19T13:56:01Z"}))
    run = host.runtime / "sandbox-ai/runs/session-web"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run": "session-web", "agent": "codex", "kind": "agent", "name": "web",
                                              "pid": supervisor.pid, "start": "web",
                                              "workspace": str(host.workspace)}))
    yield SimpleNamespace(state=state, supervisor=supervisor, run=run)
    if supervisor.poll() is None:
        supervisor.kill()
    supervisor.wait()


def add_radare2(host, folder=None):
    folder = folder or host.directory / "samples"
    folder.mkdir(exist_ok=True)
    added = host.run("mcp", "add", "hub:radare2", "--no-pull", "--mount", f"/work={folder}:ro")
    assert added.returncode == 0, added.stderr
    return folder


# --- radare2: "--mount /work=/work:rw: /work is not there and cannot be created" ------------------------------------

def test_mcp_add_with_the_sandbox_path_names_this_machine_and_the_session_workspace(host, session):
    """The sandbox's own /work given as the HOST folder (what the agent sees, not a folder of this machine): refused
    with which machine, that HOST is a folder there, and the fix - the running session's workspace on this machine,
    read-only as the catalog wants it. Nothing is added."""
    refused = host.run("mcp", "add", "hub:radare2", "--no-pull", "--mount", "/work=/work/firmware-of-the-sandbox")
    assert refused.returncode != 0
    text = " ".join(refused.stderr.split())
    assert "on this machine (" in text, text
    assert "HOST is a folder of this machine" in text and "inside a sandbox" in text, text
    assert f"web: --mount /work={host.workspace}:ro" in text, text
    assert "Traceback" not in refused.stderr
    config = host.home / ".config/sandbox-ai/mcp.json"
    assert not config.exists() or "radare2" not in json.loads(config.read_text())


def test_mount_host_rw_sandbox_path_that_cannot_be_created(monkeypatch, tmp_path):
    """The user's own line: /work:rw on a host where / is not writable - the same fix, never only "cannot be
    created (/ is not a folder you can write)"."""
    mcp = load("mcp_for_mount_hint", "mcp.py")
    monkeypatch.setattr(mcp, "running_workspaces", lambda: [("bright-swallow-paddle", "/home/u/fw")])
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False if str(self).startswith("/work") else real_exists(self))
    monkeypatch.setattr(mcp.os, "access", lambda path, mode: False)
    with pytest.raises(ValueError) as caught:
        mcp.mount_host("/work=/work:rw", "/work", "rw")
    text = str(caught.value)
    assert "cannot be created" in text and "inside a sandbox" in text
    assert "bright-swallow-paddle: --mount /work=/home/u/fw:ro" in text, text


def test_app_does_not_pass_a_sandbox_path_as_the_host_folder(monkeypatch):
    """The app's Board/Start (prerequisites): a start's --mcp-mount whose folder is a sandbox's /work that this machine
    does not have is not handed to `mcp add` (asked for instead); one that is there is."""
    sys.path.insert(0, str(ROOT / "projects/sandbox-ai-app"))
    try:
        from sandbox_ai_app import prerequisites as P
    except ImportError as exc:  # (the app's Qt is not installed for the core tests)
        pytest.skip(f"the app is not importable here: {exc}")
    real = os.path.isdir
    monkeypatch.setattr(P.os.path, "isdir", lambda path: False if str(path).startswith("/work") else real(path))
    argv = ["--mcp", "radare2", "--mcp-mount", "radare2:/work=/work:rw"]
    assert P.start_mounts(argv, "radare2", "local") == []
    assert P.mcp_add_args(argv, "radare2", ["/work=/home/u/fw:ro"], host="local") == (
        "mcp", "add", "radare2", "--mount", "/work=/home/u/fw:ro")
    assert P.start_mounts(["--mcp-mount", "radare2:/work=/tmp:ro"], "radare2", "local") == ["/work=/tmp:ro"]


# --- the session ended with 143 while an MCP server was added -------------------------------------------------------

def test_attach_restart_that_would_fail_leaves_the_session_running(host, session):
    """`mcp attach radare2 web --restart` (the app's Attach with "restart it now"): its image is not there, so the
    new start would be refused. It used to stop the session first and then fail - the session ended (143) for an MCP
    server that could not be added. Now it is checked first: refused with why, the session runs on as it was."""
    add_radare2(host)
    result = host.run("mcp", "attach", "radare2", "web", "--restart", timeout=200)
    assert result.returncode != 0
    assert "not restarted" in result.stderr and "runs on as it was" in result.stderr, result.stderr[-2000:]
    assert "is not there" in result.stderr  # the launcher's own reason: the image
    assert session.supervisor.poll() is None, "the running session was stopped"
    assert "radare2" not in json.loads((session.state / "session.json").read_text())["options"]


def test_resume_restart_that_would_fail_leaves_the_session_running(host, session):
    """`resume web --restart --mcp radare2` (the app's "Restart the agent now" after an attach): checked before the
    stop the same way."""
    add_radare2(host)
    result = host.run("resume", "web", "--restart", "--mcp", "radare2", timeout=200)
    assert result.returncode != 0
    assert "not restarted" in result.stderr and "runs on as it was" in result.stderr, result.stderr[-2000:]
    assert session.supervisor.poll() is None


def test_restart_refused_on_a_full_disk_before_the_stop(host, session, monkeypatch):
    """The restart's file systems (Podman's storage and run directory, the session's folder) nearly full: refused
    before the stop, with the file system, its free bytes and inodes, and what frees space."""
    sessions = load("sessions_for_full_disk", "sessions.py")
    real = os.statvfs

    def full(path):
        found = real(path)
        return os.statvfs_result((found.f_bsize, found.f_frsize, found.f_blocks, 10, 10, found.f_files, 5, 5,
                                  found.f_flag, found.f_namemax))
    monkeypatch.setattr(sessions.os, "statvfs", full)
    monkeypatch.setattr(sessions, "restart_paths", lambda name, data: [str(session.state)])
    with pytest.raises(ValueError) as caught:
        sessions.space_problem_for_restart("web", {"cwd": str(session.state)})
    text = str(caught.value)
    assert "free" in text and "inodes" in text and "sandbox-ai gc --check" in text, text


def test_restart_that_fails_after_the_stop_is_said(host, session):
    """The checks pass, the new start fails anyway (Podman: no space left on device at its pod): said as what
    happened - stopped to add the server, its start failed with Podman's own line, how to continue - and the end
    is recorded with its reason for the session's terminal (no bare 143)."""
    add_radare2(host)
    host.add_image("ghcr.io/c0decave/axross-mcp-radare2@sha256:cfa49731fd1f5e4df31ede27d8cba8276f469660b23567350453515"
                   "445ba13e3", "localhost/codex-sandbox:latest")
    result = host.run("mcp", "attach", "radare2", "web", "--restart", timeout=200, env={
        "FAKE_PODMAN_RUN": "0:1", "FAKE_PODMAN_POD_CREATE_FAIL": "125:Error: creating pod: mkdir /run/user/1000/containers/x: no space left on "
                                       "device"})
    assert result.returncode != 0
    text = " ".join(result.stderr.split())
    assert "was stopped to start it again with the MCP server radare2" in text, text[-2000:]
    assert "no space left on device" in text and "sandbox-ai resume web" in text, text[-2000:]
    stopped = json.loads((session.state / "stopped.json").read_text())
    assert "a restart to start it again with the MCP server radare2, whose new start failed" in stopped["by"], stopped
    host.run("mcp", "stop", "radare2", timeout=60)  # (the server outlives the start, as designed: the test stops it)


def test_end_message_says_who_stopped_the_session(host, session, monkeypatch, capsys):
    """The tab's end line: "has ended (exit status 143)" alone said nothing; a stop asked for says by what."""
    sessions = load("sessions_for_end_message", "sessions.py")
    monkeypatch.setenv("XDG_STATE_HOME", str(host.home / ".local/state"))
    monkeypatch.setattr(sessions, "session_dir", lambda name: session.state)
    data = json.loads((session.state / "session.json").read_text())
    sessions.record_stop("web", data, "sandbox-ai mcp attach radare2 web --restart (to start it again with the MCP "
                                      "server radare2)")
    monkeypatch.setattr(sessions, "alive", lambda name, data: False)
    monkeypatch.setattr(sessions, "lost", lambda name, data: None)
    monkeypatch.setattr(sessions, "exit_status", lambda name, data: 143)
    monkeypatch.setattr(sessions, "tmux_ended", lambda name: None)
    monkeypatch.setattr(sessions, "prune", lambda name: None)
    assert sessions.after_attach("web") == 143
    said = capsys.readouterr().err
    assert "has ended (exit status 143: stopped by sandbox-ai mcp attach radare2 web --restart" in said, said


# --- a full disk: the run's own messages name the file system --------------------------------------------------------

def test_log_write_failure_names_the_file_system(monkeypatch, capsys, tmp_path):
    """"the disk is full: free space on the host" was said for any ENOSPC - the host's disk was not full. Now the
    file system that refused is named: its path, its type (tmpfs said as such), free bytes and free inodes."""
    runtime = load("runtime_for_enospc", "runtime.py")
    monkeypatch.setattr(runtime, "LOG_DIR", tmp_path)
    monkeypatch.setattr(runtime, "CONTROL_DIR", tmp_path)
    monkeypatch.setattr(runtime, "mount_of", lambda path: ("/sandbox/log", "tmpfs"))
    runtime.LOG_WRITE_FAILED.clear()
    runtime.log_write_failed("processes.jsonl", OSError(errno.ENOSPC, "No space left on device"))
    said = capsys.readouterr().err
    assert "free space on the host" not in said
    assert "tmpfs" in said and "inodes" in said and "free" in said, said


def test_mcp_container_125_says_podmans_line_and_a_log_that_exists(tmp_path):
    """binwalk's container could not start (125): the record says Podman's own error line, names a full file
    system as the likely cause when Podman says "no space left on device", and points to a log that is there (the
    user's `cat .../mcp/binwalk/supervisor.log`: No such file or directory)."""
    mcp = load("mcp_for_watch", "mcp.py")
    watch = mcp.ContainerWatch(tmp_path, "binwalk", "64m", {"memory": "512m", "pids": "128"})
    entry = watch.ended(125, False, said="Error: mounting storage for container 1a2b: mkdir /home/u/.local/share/"
                                         "containers/storage/overlay/x: no space left on device\n")
    assert "no space left on device" in entry["why"] and "full" in entry["why"], entry
    assert "supervisor.log" not in entry["fix"] or (tmp_path / "supervisor.log").exists(), entry
    (tmp_path / "supervisor.log").write_text("x\n")
    entry = mcp.ContainerWatch(tmp_path, "binwalk", "64m", {}).ended(125, False, said="Error: something else\n")
    assert "Error: something else" in entry["why"] and str(tmp_path / "supervisor.log") in entry["fix"], entry


def test_mcp_watch_takes_podmans_error_line(tmp_path):
    """The watch (mcp.py --internal-watch, around each connection's podman run) passes the command's error output on
    and keeps its end for the record."""
    script = tmp_path / "podman-run"
    script.write_text("#!/bin/sh\necho 'Error: creating container storage: no space left on device' >&2\nexit 125\n")
    script.chmod(0o755)
    done = subprocess.run([sys.executable, "-I", str(SANDBOX / "mcp.py"), "--internal-watch", str(tmp_path), "binwalk",
                           "64m", "512m", "128", "--", str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 125
    assert "no space left on device" in done.stderr  # passed on (the server's log)
    records = [json.loads(line) for line in (tmp_path / "mcp-safeguards.jsonl").read_text().splitlines()] \
        if (tmp_path / "mcp-safeguards.jsonl").exists() else \
        [json.loads(line) for path in tmp_path.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert any("no space left on device" in r.get("why", "") for r in records), records


def test_mcp_add_refuses_a_pull_without_room(monkeypatch, tmp_path):
    """mcp add pulls an image: with less room in Podman's storage than the image needs, refused before the pull with
    the free space, the size needed (the catalog's rootfs size when known) and what frees space."""
    mcp = load("mcp_for_pull_room", "mcp.py")
    monkeypatch.setattr(mcp, "podman_storage", lambda: str(tmp_path))
    real = os.statvfs

    def full(path):
        found = real(path)
        return os.statvfs_result((found.f_bsize, 4096, found.f_blocks, 100, 100, found.f_files, found.f_ffree,
                                  found.f_favail, found.f_flag, found.f_namemax))
    monkeypatch.setattr(mcp.os, "statvfs", full)
    with pytest.raises(ValueError) as caught:
        mcp.check_pull_room("binwalk", {"rootfs": {"size": 58873801}})
    text = str(caught.value)
    assert "binwalk is not added" in text and "free" in text and "sandbox-ai gc --check" in text, text
    assert "168 MiB" in text or "MiB" in text


def test_mcp_command_on_a_full_file_system_names_it(tmp_path):
    """`mcp attach ... --live` (or any mcp command) whose write hit ENOSPC: which file system, its room - not a bare
    "[Errno 28] No space left on device"."""
    mcp = load("mcp_for_full_text", "mcp.py")
    text = mcp.full_text(OSError(errno.ENOSPC, "No space left on device", str(tmp_path / "state.json")))
    assert text.startswith(f"; the file system of {tmp_path}") and "inodes" in text and "gc --check" in text
    assert mcp.full_text(OSError(errno.EIO, "Input/output error", str(tmp_path))) == ""
    assert mcp.full_text(ValueError("x")) == ""


# --- TLS: what failed is said, not always "the server certificate is not trusted" ------------------------------------

def test_tls_summary_says_what_failed(tmp_path):
    launcher = load("agent_sandbox_for_tls", "agent_sandbox.py")
    log = tmp_path / "connections.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in (
        {"event": "tls-upstream-failed", "host": "hoi.st",
         "error": "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate "
                  "(_ssl.c:1006)"},
        {"event": "tls-upstream-failed", "host": "tacdb.osmocom.org", "error": "[Errno 104] Connection reset by peer"},
    )) + "\n")
    lines = launcher.connection_summary(log)
    hoi = next(line for line in lines if "hoi.st" in line)
    tacdb = next(line for line in lines if "tacdb.osmocom.org" in line)
    assert "unable to get local issuer certificate" in hoi and "not trusted" in hoi
    assert "not trusted" not in tacdb and "Connection reset by peer" in tacdb
