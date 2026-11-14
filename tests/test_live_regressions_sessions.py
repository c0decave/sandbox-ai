"""Regressions from live use: each test quotes what the user reported. Several fixes append here, each in a block of
its own; only Podman (and nothing else) is ever stood in for."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# == A session whose tmux session is gone: "no sessions", yet listed as running (2026-11-14) ===========================
#
# User: attaching to auburn-kakapo-bamboo printed tmux's "no sessions", then "agent-sandbox: detached from
# auburn-kakapo-bamboo; it keeps running. Back: sandbox-ai attach …" (exit 0), while Sandboxes & Sitzungen show it
# as running. The tmux server inside had no session any more (the agent and the shell window ended), but the terminal
# relay - started in the background, outside the guard's loop - kept running, and Firejail keeps its sandbox alive
# for as long as any process in it runs: the run never ended and the relay still answered.

rt = load("sandbox_runtime_live_regressions", "sandbox/runtime.py")
needs_tmux = pytest.mark.skipif(not shutil.which("tmux") or not os.path.exists(rt.SUPERVISOR_PYTHON[0]),
                                reason="needs tmux and /usr/bin/python3")
SESSION = "auburn-kakapo-bamboo"


def tmux_env(tmp_path):
    """An environment whose tmux is a server of this test alone (never the one this test may run inside)."""
    (tmp_path / "tmux-tmp").mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("TMUX")}
    env.update(TMUX_TMPDIR=str(tmp_path / "tmux-tmp"), TERM="xterm-256color", LANG="C.UTF-8")
    return env


def end_tmux(env):
    subprocess.run(["tmux", "kill-server"], env=env, capture_output=True, timeout=20)


def processes_naming(text):
    """PIDs of this user's processes whose arguments contain TEXT (this test's own paths only)."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if os.fsencode(text) in (entry / "cmdline").read_bytes():
                    found.append(int(entry.name))
            except OSError:
                pass
    return [pid for pid in found if pid != os.getpid()]


def end_processes(text):
    for pid in processes_naming(text):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def guard_script(tmp_path, agent, shell="true"):
    """The guard of a session (tmux_command(detached=True)), with the sandbox's paths moved into TMP_PATH: the agent
    and the shell window as given, the real relay of runtime.py."""
    tmux_dir, work = tmp_path / "tmux", tmp_path / "work"
    tmux_dir.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    command = rt.tmux_command(agent, detached=True)
    assert command[:2] == ["sh", "-c"]
    script = command[2].replace(" -c /work ", f" -c {shlex.quote(str(work))} ")
    script = script.replace(f"{shlex.quote(str(work))} bash", f"{shlex.quote(str(work))} {shell}")
    script = script.replace("/sandbox/runtime.py", str(ROOT / "sandbox" / "runtime.py"))
    return ["sh", "-c", script.replace(rt.TMUX_DIR, str(tmux_dir))], tmux_dir


@needs_tmux
def test_the_guard_ends_with_its_tmux_session_and_takes_the_relay_along(tmp_path):
    """User: "no sessions", then "detached from auburn-kakapo-bamboo; it keeps running" - the agent and its shell
    window ended, the relay ran on (and with it the sandbox, under Firejail). Real tmux, real relay: once the tmux
    session is gone the guard ends, the relay no longer answers and no process of it is left; the end is recorded for
    the host (the agent's exit status) and said in the log."""
    env = tmux_env(tmp_path)
    command, tmux_dir = guard_script(tmp_path, ["sh", "-c", "sleep 1; echo cannot start >&2; exit 3"])
    socket_path = str(tmux_dir / "attach.sock")
    try:
        with open(tmp_path / "guard.log", "wb") as log:  # (a file: a relay left behind holds no pipe open)
            guard = subprocess.run(command, env=env, stdout=log, stderr=log, timeout=60)
        said = (tmp_path / "guard.log").read_text()
        assert guard.returncode == 0, said
        deadline = time.monotonic() + 10
        while processes_naming(socket_path) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not processes_naming(socket_path), "the relay ends with the tmux session (else the sandbox runs on)"
        assert rt.attach_probe(socket_path) == 1, "nothing answers on the terminal's socket any more"
        ended = json.loads((tmux_dir / "ended").read_text())
        assert ended["status"] == 3 and isinstance(ended["seconds"], int) and 0 <= ended["seconds"] < 30
        assert "the agent exited with status 3" in said and "the sandbox ends" in said
    finally:
        end_processes(socket_path)
        end_tmux(env)


def test_firejail_ends_the_sandbox_with_the_guard_of_a_session(monkeypatch):
    """User: "... it keeps running" although the tmux session was gone - Firejail keeps a sandbox for as long as any
    process in it runs (an agent's background process, the relay). A session's sandbox ends with its guard
    (--deterministic-shutdown, when this Firejail has it); other starts keep Firejail's default."""
    helps = {"text": "  --deterministic-shutdown - terminate orphan processes.\n  --shell=none\n"}
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, helps["text"], ""))
    assert "--deterministic-shutdown" in rt.firejail_command({}, shutdown=True)
    assert "--deterministic-shutdown" not in rt.firejail_command({})
    helps["text"] = "  --shell=none\n"  # an older Firejail: started as before
    assert "--deterministic-shutdown" not in rt.firejail_command({}, shutdown=True)


def stuck_session(tmp_path, env, record=None):
    """A session as the user met it: its supervisor runs (a process standing in for it, by its arguments), its relay
    answers, but its tmux server has no session any more ("no sessions")."""
    state = tmp_path / "state"
    directory = state / "sandbox-ai" / "sessions" / SESSION
    (directory / "tmux").mkdir(parents=True)
    (directory / "home").mkdir()
    (directory / "supervisor.log").write_text("agent-sandbox: starting the sandbox\nagent-sandbox: claude is ready\n")
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", str(directory / "tmux")])
    data = {"agent": "claude", "options": [], "agent_args": [], "backend": "bwrap", "workspace": str(tmp_path),
            "cwd": str(tmp_path), "created": "2026-11-14T10:00:00Z", "started": "2026-11-14T10:00:00Z",
            "name": SESSION, "ready": True, "keep": True, "mode": "tmux", "run": "claude-1a2b3c4d",
            "pid": supervisor.pid, **(record or {})}
    (directory / "session.json").write_text(json.dumps(data))
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -s exit-empty off\n")
    subprocess.run(["tmux", "-f", str(conf), "start-server"], env=env, check=True, timeout=20)
    socket_path = str(directory / "tmux" / "attach.sock")
    relay = subprocess.Popen([*rt.SUPERVISOR_PYTHON, str(ROOT / "sandbox" / "runtime.py"), "--internal-attach-relay",
                              socket_path], env=env)
    deadline = time.monotonic() + 15
    while not os.path.exists(socket_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    return directory, supervisor, relay


def sai(tmp_path, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TMUX")}
    env.update(XDG_STATE_HOME=str(tmp_path / "state"), XDG_RUNTIME_DIR=str(tmp_path / "runtime"),
               XDG_CONFIG_HOME=str(tmp_path / "config"), HOME=str(tmp_path / "home"))
    (tmp_path / "runtime").mkdir(mode=0o700, exist_ok=True)
    return subprocess.run([str(ROOT / "sandbox-ai"), *args], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=120)


@needs_tmux
def test_attach_says_the_agent_ended_and_list_reports_it(tmp_path):
    """User: attaching to auburn-kakapo-bamboo printed "no sessions", then "detached from auburn-kakapo-bamboo; it
    keeps running. Back: sandbox-ai attach …" (exit 0), while the lists showed it running. The real `sandbox-ai
    attach`: it says that the agent's tmux session has ended (no "keeps running"), with the end of its log and what
    to do, fails; `sandbox-ai list --json` reports it truthfully (its terminal ended, nothing to attach), and the next
    attach says so at once."""
    env = tmux_env(tmp_path)
    directory, supervisor, relay = stuck_session(tmp_path, env)
    try:
        attached = sai(tmp_path, "attach", SESSION)
        assert "keeps running" not in attached.stderr, attached.stderr
        assert attached.returncode != 0
        assert f"{SESSION} has no agent any more: its tmux session has ended" in attached.stderr
        assert "claude is ready" in attached.stderr, "the end of its log"
        assert f"sandbox-ai stop {SESSION}" in attached.stderr and f"sandbox-ai resume {SESSION}" in attached.stderr
        listed = sai(tmp_path, "list", "--json")
        assert listed.returncode == 0, listed.stderr
        entries = json.loads(listed.stdout)
        entries = entries.get("starts", entries) if isinstance(entries, dict) else entries
        entry = [e for e in entries if e.get("name") == SESSION][0]
        assert entry["state"] == "running" and entry["terminal"] == "ended" and entry["attachable"] is False
        assert entry["attach_why"] == "ended" and "claude is ready" in entry["terminal_log"]
        again = sai(tmp_path, "attach", SESSION)
        assert again.returncode != 0 and "has no agent any more" in again.stderr and "no sessions" not in again.stderr
    finally:
        for process in (relay, supervisor):
            process.kill()
            process.wait()
        end_tmux(env)


@needs_tmux
def test_a_session_whose_agent_exited_at_once_says_why_when_attached(tmp_path):
    """User: "no sessions" ... "it keeps running" - and why had its tmux session ended? The whole chain with the real
    guard (tmux, relay) under a process standing in for the supervisor: the agent exits at once with status 3, the
    shell window closes, the run ends - and `sandbox-ai attach` says that it ended, with the agent's exit status."""
    env = tmux_env(tmp_path)
    state = tmp_path / "state"
    directory = state / "sandbox-ai" / "sessions" / SESSION
    (directory / "home").mkdir(parents=True)
    command, tmux_dir = guard_script(directory, ["sh", "-c", "sleep 1; exit 3"])
    log = open(directory / "supervisor.log", "ab")
    supervisor = subprocess.Popen([sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.call(sys.argv[2:]))",
                                   str(tmux_dir), *command], env=env, stdout=log, stderr=log)
    log.close()
    data = {"agent": "claude", "options": [], "agent_args": [], "backend": "bwrap", "workspace": str(tmp_path),
            "cwd": str(tmp_path), "created": "2026-11-14T10:00:00Z", "started": "2026-11-14T10:00:00Z",
            "name": SESSION, "ready": True, "keep": True, "mode": "tmux", "run": "claude-1a2b3c4d",
            "pid": supervisor.pid}
    (directory / "session.json").write_text(json.dumps(data))
    try:
        assert supervisor.wait(60) == 0, "the run ends with its tmux session"
        assert not processes_naming(str(tmux_dir / "attach.sock"))
        attached = sai(tmp_path, "attach", SESSION)
        assert attached.returncode != 0 and "keeps running" not in attached.stderr
        assert "is stopped" in attached.stderr and "its agent exited with status 3" in attached.stderr
        assert "the agent exited with status 3" in (directory / "supervisor.log").read_text()
    finally:
        supervisor.kill()
        supervisor.wait()
        end_processes(str(tmux_dir / "attach.sock"))
        end_tmux(env)
