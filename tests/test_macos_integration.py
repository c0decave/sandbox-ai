"""macOS support, stages 3 and 4 (TODO.md): the host integration on a Mac, simulated on Linux - the browser opened
with open(1), notifications through osascript (text as arguments, never in the script), secrets in the Keychain
(the value never on a command line), Claude's Keychain login named, ~/Library private and compared without case,
the sandbox's xdg-open through request files, peer credentials without SO_PEERCRED, the team board posted in the VM
and MCP servers' sockets in Podman volumes. Every outside program is a recorder: nothing is opened or shown."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


approvals = load("approvals", "approvals_macos_test")
ss = load("secret_store", "secret_store_macos_test")
team = load("team", "team_macos_test")
tb = load("team_board", "team_board_macos_test")
mcp = load("mcp", "mcp_macos_test")
s = mcp.launcher()
rt = s.RUNTIME


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


# --- stage 3: the desktop -------------------------------------------------------------------------------------

def test_the_browser_opens_with_open_on_a_mac(mac, monkeypatch):
    opened = []
    monkeypatch.setattr(approvals.shutil, "which", lambda program: program if program == "/usr/bin/open" else None)
    monkeypatch.setattr(approvals.subprocess, "Popen", lambda argv, **k: opened.append(argv))
    approvals.open_browser("https://example.org/login")
    assert opened == [["/usr/bin/open", "https://example.org/login"]]


def test_notifications_through_osascript_take_the_text_as_arguments(mac, monkeypatch):
    calls = []
    monkeypatch.setattr(approvals.subprocess, "run", lambda argv, **k: calls.append(argv))
    evil = 'x" & (do shell script "touch /tmp/pwned") & "'
    approvals.desktop_notification("sandbox-ai: a question", evil + "\x1b[2J")
    argv = calls[0]
    assert argv[0] == "/usr/bin/osascript" and argv[1::2][:3] == ["-e", "-e", "-e"]
    script = " ".join(argv[2:7:2])
    assert script == " ".join(approvals.OSASCRIPT_NOTIFICATION) and "touch" not in script
    assert argv[-2:] == ["sandbox-ai: a question", evil + "?[2J"]  # data, never part of the script


# --- stage 3: the Keychain ------------------------------------------------------------------------------------

class Security:
    """security(1) as a recorder with a Keychain of its own."""

    def __init__(self):
        self.items, self.calls = {}, []

    def __call__(self, argv, input=None, **kwargs):
        self.calls.append((argv, input))
        if argv[1:] == ["-i"]:
            words = input.split()
            assert words[:2] == ["add-generic-password", "-U"]
            options = dict(zip(words[2::2], words[3::2]))
            self.items[options["-a"]] = bytes.fromhex(options["-X"]).decode()
            return subprocess.CompletedProcess(argv, 0, "", "")
        options = dict(zip(argv[2::2], argv[3::2]))
        if argv[1] == "find-generic-password":
            value = self.items.get(options.get("-a"))
            return subprocess.CompletedProcess(argv, 0 if value is not None else 44, (value or "") + "\n", "")
        if argv[1] == "delete-generic-password":
            return subprocess.CompletedProcess(argv, 0 if self.items.pop(options["-a"], None) is not None else 44, "", "")
        if argv[1] == "dump-keychain":
            out = "".join(f'keychain: "/Users/ada/Library/Keychains/login.keychain-db"\nclass: "genp"\nattributes:\n'
                          f'    "acct"<blob>="{name}"\n    "svce"<blob>="sandbox-ai"\n' for name in self.items)
            out += 'keychain: "/x"\nclass: "genp"\nattributes:\n    "acct"<blob>="ada"\n    "svce"<blob>="other"\n'
            return subprocess.CompletedProcess(argv, 0, out, "")
        raise AssertionError(argv)


def test_secrets_go_to_the_keychain_never_through_the_command_line(mac, monkeypatch):
    security = Security()
    store = ss.KeychainStore(security)
    store.set("GH_PAT", "ghp_secret value")
    assert store.get("GH_PAT") == "ghp_secret value" and store.names() == ["GH_PAT"]
    for argv, data in security.calls:
        assert not any("ghp_secret" in arg or "ghp_secret".encode().hex() in arg for arg in argv)
    assert "ghp_secret".encode().hex() in security.calls[0][1]  # on stdin, as hex
    assert store.delete("GH_PAT") and not store.delete("GH_PAT") and store.get("GH_PAT") is None
    with pytest.raises(ValueError):
        store.set("a b", "x")
    monkeypatch.delenv("SANDBOX_AI_SECRETS", raising=False)
    monkeypatch.setattr(ss, "keychain_usable", lambda: True)
    assert isinstance(ss.store(), ss.KeychainStore)
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    assert isinstance(ss.store(), ss.FileStore)


def test_a_keychain_that_refuses_says_so(mac):
    def refusing(argv, input=None, **kwargs):
        return subprocess.CompletedProcess(argv, 0 if argv[1:] == ["-i"] else 44, "",
                                           "security: SecKeychainItemCreateFromContent: User interaction is not allowed."
                                           if argv[1:] == ["-i"] else "")
    with pytest.raises(ValueError, match="Keychain refused.*SANDBOX_AI_SECRETS=file"):
        ss.KeychainStore(refusing).set("TOKEN", "v")


def test_claudes_keychain_login_is_named_not_read(mac, monkeypatch, capsys):
    calls = []
    assert s.claude_keychain_login(lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0))
    assert calls == [["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials"]]  # no -w
    assert not s.claude_keychain_login(lambda argv, **k: SimpleNamespace(returncode=44))
    monkeypatch.setattr(s, "login_state", lambda agent: ("host", "missing"))
    monkeypatch.setattr(s, "claude_keychain_login", lambda runner=None: True)
    s.check_login(s.parse_args(["claude"]))
    assert "macOS Keychain" in capsys.readouterr().err
    doctor = load("doctor", "doctor_macos_login_test")
    monkeypatch.setattr(doctor, "launcher", lambda: s)
    state, title, detail, hint = doctor.check_login("claude")
    assert state == "warn" and "Keychain" in detail and hint == "sandbox-ai login claude"
    monkeypatch.setattr(sys, "platform", "linux")
    assert not load("agent_sandbox", "agent_sandbox_linux_keychain").claude_keychain_login(
        lambda argv, **k: pytest.fail("no security(1) on Linux"))


def test_check_login_points_to_sandbox_ai_login(mac, monkeypatch, capsys):
    monkeypatch.setattr(s, "login_state", lambda agent: ("host", "missing"))
    monkeypatch.setattr(s, "claude_keychain_login", lambda runner=None: True)
    s.check_login(s.parse_args(["claude"]))
    err = capsys.readouterr().err
    assert "Keychain" in err and "sandbox-ai login claude" in err and "or on the host" not in err


# --- stage 3: private locations ------------------------------------------------------------------------------

def test_library_is_private_and_compared_without_case_on_a_mac(mac, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    fixed, _ = s.private_locations()
    for name in ("LaunchAgents", "Keychains", "Application Support", "Preferences"):
        assert tmp_path / "Library" / name in fixed
    said = []
    assert s.check_private_location(tmp_path / "library" / "launchagents", "--mount", said.append)
    assert "Library/LaunchAgents" in said[0] and "library/launchagents" in said[0]
    assert s.check_private_location(tmp_path / ".SSH", "--workspace", said.append)  # ~/.ssh as APFS sees it
    (tmp_path / ".BashRC").write_text("")
    assert "shell or session start file" in s.check_private_location(tmp_path / ".BashRC", "--mount", said.append)
    with pytest.raises(ValueError, match="sandbox-ai's own launcher"):
        s.check_launcher_location(Path(str(s.HERE).upper()), "--workspace")
    monkeypatch.setattr(sys, "platform", "linux")
    assert not any("Library" in str(path) for path in s.private_locations()[0])
    assert s.check_private_location(tmp_path / ".SSH", "--workspace", said.append) is None  # Linux: another folder


# --- stage 2/3: the sandbox's xdg-open through request files ----------------------------------------------------

def test_the_sandboxs_browser_requests_cross_as_files(tmp_path, monkeypatch):
    """The supervisor's browser_relay (in the VM) and the host's serve_browser_files meet in the control directory;
    the sandbox's xdg-open gets the host's answer as on Linux."""
    requests, approvals_dir = tmp_path / "control" / "browser", tmp_path / "control" / "approvals"
    approvals_dir.mkdir(parents=True)
    sock = Path("/tmp") / f"open-{os.getpid()}-{time.monotonic_ns()}.sock"
    stop, told = threading.Event(), []
    host = threading.Thread(target=approvals.serve_browser_files, args=(requests, approvals_dir, stop),
                            kwargs={"notify": lambda title, text: told.append(text), "interval": 0.05}, daemon=True)
    vm = threading.Thread(target=rt.browser_relay, args=(str(sock), requests, stop), kwargs={"wait": 5}, daemon=True)
    host.start()
    vm.start()
    try:
        deadline = time.monotonic() + 5
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        out = io.StringIO()
        assert rt.open_request(["https://example.org/device"], socket_path=str(sock), out=out) == 0
        assert "asked the human" in out.getvalue() and "https://example.org/device" in told[0]
        questions = list(approvals_dir.glob("*.json"))
        assert len(questions) == 1 and json.loads(questions[0].read_text())["url"] == "https://example.org/device"
        out = io.StringIO()
        assert rt.open_request(["file:///etc/passwd"], socket_path=str(sock), out=out) == 1
        with socket.socket(socket.AF_UNIX) as client:  # what the agent sends by hand is only a request
            client.connect(str(sock))
            client.sendall(json.dumps({"url": "javascript:alert(1)"}).encode() + b"\n")
            assert not json.loads(client.makefile().readline())["ok"]
        assert not list(requests.glob("*.request")) and not list(requests.glob("*.reply"))
    finally:
        stop.set()
        host.join(5)
        vm.join(5)
        sock.unlink(missing_ok=True)


def test_a_host_that_does_not_answer_is_said(tmp_path):
    left, right = socket.socketpair()
    right.sendall(b'{"url": "https://example.org"}\n')
    rt.browser_forward(left, tmp_path, 1, wait=0.3)
    assert "did not take the request" in right.recv(4096).decode() and not list(tmp_path.glob("*.request"))


# --- stage 4: peer credentials -----------------------------------------------------------------------------------

def test_peer_credentials_without_so_peercred(mac, monkeypatch):
    import struct
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    xucred = struct.pack("IIhxx16I", 0, 501, 1, *([20] + [0] * 15))

    class Connection:
        def getsockopt(self, level, name, size):
            assert level == 0
            return struct.pack("i", 4242) if name == 2 else xucred[:size]
    assert rt.Attribution.peer(Connection()) == (4242, 501, 20)
    monkeypatch.setattr(rt.Attribution, "process", lambda self, pid: None)
    gateway = rt.McpGateway({"server": "demo"}, out=io.StringIO(), plain=True)
    assert gateway.client(Connection()) == {"process": {"pid": 4242, "uid": 501}}
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(OSError):
        rt.Attribution.peer(Connection())  # caught as before: "no peer credentials"


# --- stage 4: the team board ------------------------------------------------------------------------------------

def test_a_task_is_posted_in_the_vm_where_the_lock_holds(mac, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    team.start("app", ["claude", "codex"], tmp_path, [], lambda line: None,
               lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    real = team.load_module

    def loader(name, file):
        module = real(name, file)
        if file == "mcp.py":
            module.status = lambda server: "running"
        return module
    monkeypatch.setattr(team, "load_module", loader)
    calls, lines = [], []

    def podman(argv, input=None, **kwargs):
        """podman exec: team_board.py --post for real, on the board's directory."""
        calls.append(argv)
        out = io.StringIO()
        args = argv[argv.index("--members"):]
        args[args.index("--dir") + 1] = str(team.teams_dir() / "app" / "board")
        code = tb.main(args, stdin=io.StringIO(input), out=out)
        return subprocess.CompletedProcess(argv, code, out.getvalue(), "")
    assert team.post("app", "codex", "look at the logs", lines.append, run=podman) == 0
    assert calls[0][:5] == ["podman", "exec", "--interactive", "--user=0:0", "agent-mcp-team-app-agent"]
    assert lines == ["sandbox-ai team: task #1 posted for codex"]
    board = tb.read(team.teams_dir() / "app" / "board")
    assert board["tasks"][0]["task"] == "look at the logs" and board["tasks"][0]["from"] == "human"
    with pytest.raises(ValueError, match="did not take the task"):
        team.post("app", "codex", "x", run=lambda argv, **k: subprocess.CompletedProcess(argv, 125, "", "Error: no "
                                                                                       "such container"))


def test_team_board_post_mode(tmp_path):
    out = io.StringIO()
    assert tb.main(["--members", "claude,codex", "--dir", str(tmp_path), "--post", "codex"],
                   stdin=io.StringIO("do it"), out=out) == 0
    assert json.loads(out.getvalue()) == {"id": 1}
    assert tb.main(["--members", "claude,codex", "--dir", str(tmp_path), "--post", "nobody"],
                   stdin=io.StringIO("x"), out=io.StringIO()) == 2


# --- stage 4: MCP servers -----------------------------------------------------------------------------------------

def test_mcp_sockets_lie_in_podman_volumes_on_a_mac(mac, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(s, "podman_machine", lambda runner=None: {})
    volume = s.mcp_volume(s.mcp_socket_dir("files"))
    assert volume.startswith("sandbox-ai-mcp-files-") and len(volume) == len("sandbox-ai-mcp-files-") + 12
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "other"))
    assert s.mcp_volume(s.mcp_socket_dir("files")) != volume  # another state root, another volume
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    a = s.parse_args(["claude", "--workspace", str(tmp_path), "--mcp", "files"])
    a.run_id = "claude-1"
    _, run = s.pod_commands(a, "agent-claude-1", tmp_path, tmp_path)
    assert f"type=volume,src={volume},dst={s.MCP_UPSTREAM_DIR}/files,ro" in run
    server = s.parse_args(["codex", "--workspace", str(tmp_path), "--mcp-socket-dir", str(s.mcp_socket_dir("files")),
                           "--pod-name", "agent-mcp-files", "--binary", "python3"])
    server.run_id = "mcp-files"
    _, run = s.pod_commands(server, "agent-mcp-files", tmp_path, tmp_path)
    assert f"type=volume,src={volume},dst={s.MCP_SERVE_DIR}" in run
    assert not any(str(s.mcp_socket_dir("files")) in arg for arg in run)
    monkeypatch.setattr(sys, "platform", "linux")
    _, run = s.pod_commands(a, "agent-claude-1", tmp_path, tmp_path)
    assert not any("type=volume" in arg for arg in run)


def test_mcp_readiness_is_asked_in_the_vm(mac):
    calls = []
    a = SimpleNamespace(backend="podman", podman_url=None)
    probe = mcp.machine_ready("files", a, runner=lambda argv, **k: calls.append(argv) or
                              SimpleNamespace(returncode=0))["ready"]
    assert probe() and calls[0][3] == "agent-mcp-files-agent"
    assert calls[0][-2:] == ["--internal-attach-probe", f"{s.MCP_SERVE_DIR}/server.sock"]
    assert mcp.machine_ready("files", SimpleNamespace(backend="bwrap", podman_url=None)) == {}


def test_catalog_servers_are_refused_on_a_mac(mac, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(mcp, "definitions", lambda document=None: {"hub": {"image": "ghcr.io/x/y@sha256:" + "0" * 64,
                                                                           "network": "none"}})
    monkeypatch.setattr(mcp.launcher(), "mcp_gateway", lambda a, name, approvals: None)
    a = s.parse_args(["claude", "--workspace", str(tmp_path)])
    with pytest.raises(ValueError, match="not supported yet"):
        mcp.ensure_running_locked("hub", a)
