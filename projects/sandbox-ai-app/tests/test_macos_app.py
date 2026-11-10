"""macOS support, stage 5 (TODO.md): the app on a Mac, simulated on Linux - login through a LaunchAgent, terminal
windows of Terminal or iTerm through AppleScript, folders checked with F_GETPATH, processes through sandbox-ai's
procinfo.py (no /proc), notifications of an unbundled Python through osascript. Nothing is started: every program
is a recorder, and sys.platform is "darwin" only inside each test."""
import importlib.util
import os
from pathlib import Path
import plistlib
import shutil
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sandbox_ai_app import backend, files, terminal  # noqa: E402


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def test_login_through_a_launch_agent(mac, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(backend.subprocess, "run", lambda argv, **k: calls.append(argv))
    assert not backend.autostart_enabled()
    path = backend.set_autostart(True, ["/Applications/x y/sandbox-ai-app", "--hidden"])
    assert path == tmp_path / "Library" / "LaunchAgents" / "org.sandbox-ai.app.plist" and backend.autostart_enabled()
    agent = plistlib.loads(path.read_bytes())
    assert agent["ProgramArguments"] == ["/Applications/x y/sandbox-ai-app", "--hidden"]  # a list: no shell, no quoting
    assert agent["Label"] == "org.sandbox-ai.app" and agent["RunAtLoad"] and agent["LimitLoadToSessionType"] == "Aqua"
    assert calls == []  # read by launchd at the next login; not started a second time now
    with pytest.raises(ValueError, match="control character"):
        backend.set_autostart(True, ["/opt/app\n<key>evil</key>", "--hidden"])
    backend.set_autostart(False)
    assert not path.exists() and calls == [["/bin/launchctl", "bootout", f"gui/{os.getuid()}/org.sandbox-ai.app"]]
    # The app that LaunchAgent started: bootout would end it (and its answers to the sandboxes); the file goes only.
    backend.set_autostart(True, ["/Applications/sandbox-ai.app/x", "--hidden"])
    monkeypatch.setenv("XPC_SERVICE_NAME", "org.sandbox-ai.app")
    backend.set_autostart(False)
    assert not path.exists() and len(calls) == 1


def test_terminal_windows_through_applescript(mac, monkeypatch):
    sai = SimpleNamespace()
    monkeypatch.delenv("SANDBOX_AI_TERMINAL", raising=False)
    argv = backend.SandboxAI.in_terminal(sai, ["sandbox-ai", "attach", 'x"; do shell script "id'])
    assert argv[0] == "/usr/bin/osascript" and 'tell application "Terminal"' in argv
    # The login shell (zsh, fish, tcsh) only types `exec /bin/sh FILE`: the command waits in that private file;
    # "--" ends osascript's options before the data.
    assert argv[-2] == "--" and argv[-1].startswith("exec /bin/sh /")
    path = Path(argv[-1].split(" ", 2)[2])
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    lines = path.read_text().splitlines()
    assert lines[0] == 'rm -f -- "$0"; rmdir -- "${0%/*}" 2>/dev/null'
    shutil.rmtree(path.parent)
    script = lines[1]
    assert script.startswith("sandbox-ai attach ") and "'x\"; do shell script \"id'" in script  # shell-quoted data
    assert all('do shell script "id' not in line for line in argv[:-1])  # never part of the AppleScript
    monkeypatch.setenv("SANDBOX_AI_TERMINAL", "iTerm")
    argv = backend.SandboxAI.in_terminal(sai, ["sandbox-ai", "doctor"])
    assert 'tell application "iTerm"' in argv and argv[-1].startswith("sandbox-ai doctor;")


class Info:
    """sandbox-ai's procinfo.py as on a Mac: a small process table."""

    def __init__(self, uid):
        self.uid = uid
        self.processes = {
            10: (1, 10, "S", [b"/usr/bin/python3", b"/x/sandbox/agent_sandbox.py", b"claude", b"--workspace", b"/w", b""]),
            11: (10, 11, "Z", [b"podman", b""]),
            12: (1, 12, "S", [b"/usr/bin/python3", b"/x/sandbox/agent_sandbox.py", b"codex", b"--dry-run", b""]),
        }

    def table(self):
        return {pid: entry[:3] for pid, entry in self.processes.items()}

    def pids(self):
        return sorted(self.processes)

    def owner(self, pid):
        return self.uid if pid in self.processes else None

    def cmdline(self, pid):
        return self.processes[pid][3] if pid in self.processes else None

    def cwd(self, pid):
        return "/Users/ada/app" if pid in self.processes else None

    def started_epoch(self, pid):
        return 1_700_000_000.5 if pid in self.processes else None


def test_processes_through_procinfo(mac, tmp_path):
    info = Info(os.getuid())
    assert backend.group_alive(10, info=info) and not backend.group_alive(11, info=info)  # 11: a zombie
    assert backend.process_started(10, info=info) == "2023-11-14T22:13:20Z"
    assert backend.process_started(99, info=info) is None
    found = backend.launcher_processes(("claude", "codex"), info=info)
    assert found == [(10, "claude", ["--workspace", "/w"], "/Users/ada/app")]  # the dry run left out
    assert backend.launcher_processes(info=Info(os.getuid() + 1)) == []  # another user's
    # a fake /proc of a test is read as /proc, whatever the platform
    (tmp_path / "77").mkdir()
    (tmp_path / "77" / "stat").write_text("77 (x) S 1 77 77 0 -1\n")
    assert backend.group_alive(77, tmp_path, info=info)


def test_linux_reads_proc_as_before(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert backend.mac_processes(Path("/proc"), Info(0)) is None
    assert backend.group_alive(os.getpgid(0), info=Info(0))


def test_folders_are_followed_with_f_getpath(mac, monkeypatch, tmp_path):
    seen = []

    def fcntl_(fd, command, buffer):
        seen.append(command)
        return os.fsencode(str(tmp_path)).ljust(1024, b"\0")
    import fcntl
    monkeypatch.setattr(fcntl, "fcntl", fcntl_)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert files.fd_location(fd) == str(tmp_path) and seen == [getattr(fcntl, "F_GETPATH", 50)]
    finally:
        os.close(fd)
    root = files.Root(tmp_path)
    assert root.path == str(tmp_path)


def test_linux_fd_location_reads_proc(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert files.fd_location(fd) == str(tmp_path)
    finally:
        os.close(fd)


def test_an_interactive_start_detaches_on_a_mac_with_a_new_sandbox_ai(mac):
    argv = ["/x/sandbox-ai", "claude", "--workspace", "/w"]
    assert terminal.detaches(argv, SimpleNamespace(on_machine=lambda a: True, interactive_args=lambda *a: True))
    assert not terminal.detaches(argv, SimpleNamespace(interactive_args=lambda *a: True))  # older: no tmux on a Mac


def test_notifications_of_an_unbundled_python_go_through_osascript(mac):
    from sandbox_ai_app import notifications
    assert notifications.mac_unbundled("/usr/local/bin/python3")
    assert not notifications.mac_unbundled("/Applications/sandbox-ai.app/Contents/MacOS/python3")
    started = []
    notifications.mac_notification("sandbox-ai", 'a "quote" & stuff', popen=lambda argv, **k: started.append(argv))
    argv = started[0]
    assert argv[:2] == ["/usr/bin/osascript", "-e"] and argv[-2:] == ["sandbox-ai", 'a "quote" & stuff']
    assert " ".join(argv[2:7:2]) == " ".join(notifications.OSASCRIPT_NOTIFICATION)
