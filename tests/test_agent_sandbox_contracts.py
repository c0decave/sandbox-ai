"""Happy/sad/edge contracts for every launcher function.

Only OS/process boundaries are mocked. Filesystem copies, pipes, validation,
configuration serialization and security decisions use the implementation.
The separate live suite proves the actual namespace and model boundaries.
"""
import argparse
import ast
import base64
import contextlib
import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import stat
import struct
import subprocess
import sys
import ssl
import threading
import time
import tempfile
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_contracts", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)


class Both:
    """Patch target for names the launcher re-exports from runtime.py: sets them in both modules.

    __dict__ is the launcher's, so pytest's monkeypatch records and restores the real old value."""
    __slots__ = ("module",)

    def __init__(self, module):
        object.__setattr__(self, "module", module)

    @property
    def __dict__(self):
        return vars(self.module)

    def modules(self):
        return (self.module, self.module.RUNTIME)

    def __getattr__(self, name):
        return getattr(self.module, name)

    def __setattr__(self, name, value):
        for module in self.modules():
            setattr(module, name, value)

    def __delattr__(self, name):
        for module in self.modules():
            if name in vars(module):
                delattr(module, name)


sr = Both(s)
SPEC.loader.exec_module(s)


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("HOME", str(host))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                 "XDG_CONFIG_HOME", "TERM"):
        monkeypatch.delenv(name, raising=False)


def states():
    before = {"mnt": "mnt:[10]", "pid": "pid:[11]"}
    current = dict(mnt="mnt:[12]", pid="pid:[13]", CapEff="0", CapPrm="0",
                   CapBnd="0", CapInh="0", CapAmb="0", NoNewPrivs="1", Seccomp="2")
    return before, current


def test_parse_args(scenario):
    if scenario == "sad":
        with pytest.raises(SystemExit) as exc:
            s.parse_args(["codex", "--unknown-wrapper-option"])
        assert exc.value.code == 2
    else:
        tail = ["exec", "--", "--sandbox=literal-prompt"] if scenario == "edge" else ["exec", "task"]
        args = s.parse_args(["codex", "--", *tail])
        assert args.agent_args == tail
        assert args.backend == "podman"


def test_allowed_hosts(scenario):
    options = {"happy": [], "sad": ["--no-provider-defaults"],
               "edge": ["--allow-host", "opencode.ai", "--allow-host", "opencode.ai"]}[scenario]
    answer = s.allowed_hosts(s.parse_args(["opencode", *options]))
    if scenario == "sad":
        assert answer == []
    else:
        assert answer.count("opencode.ai") == 1
        assert answer == ["opencode.ai"]


def test_safe_dest(scenario):
    if scenario == "sad":
        for dest in ("/work/../etc", "/work//file", "/work/./file", "/etc", "relative"):
            with pytest.raises(ValueError):
                s.safe_dest(dest)
    else:
        value = "/mnt" if scenario == "edge" else "/work/nested/file"
        assert s.safe_dest(value) == value


def test_parse_mount(scenario, tmp_path):
    if scenario == "sad":
        for mount in ("missing-separators", f"{tmp_path}/missing:/mnt/data:rw", f"{tmp_path}:/mnt/data:bad"):
            with pytest.raises(ValueError):
                s.parse_mount(mount)
    else:
        path = tmp_path / ("colon: and spaces" if scenario == "edge" else "reference")
        path.write_text("unchanged")
        assert s.parse_mount(f"{path}:/mnt/reference:ro") == (str(path), "/mnt/reference", "ro")
        assert path.read_text() == "unchanged"


def test_validate(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        cases = [["--network", "research", "--ip", "2001:db8::1"],
                 ["--network", "research", "--ip6", "192.0.2.1"],
                 ["--runtime", "/usr/bin"], ["--env", "INVALID=VALUE"],
                 ["--env", "UNSET_CONTRACT_VARIABLE"], ["--allow-host", "a..example.org"],
                 ["--offline", "--dns"], ["--offline", "--egress-all-allow"], ["--offline", "--nameserver", "1.1.1.1"],
                 ["--nameserver", "dns.example"], ["--nameserver", "1.1.1.1; accept"], ["--nameserver", "10.0.0.0/8"],
                 ["--allow-host-port", "3000", "--offline"], ["--allow-host-port", "3000", "--backend", "bwrap"],
                 ["--allow-host-port", "3000", "--network", "research"], ["--allow-host-port", "3000,22"],
                 ["--git-token-env", "https://github.com=UNSET_CONTRACT_VARIABLE"],
                 ["--git-token-env", "https://github.com=PATH", "--offline"],
                 ["--git-credentials", "/nonexistent/git-credentials"],
                 ["--git-token-env", "http://localhost:3000=UNSET_CONTRACT_VARIABLE"],
                 ["--git-token-env", "http://localhost:3000=PATH", "--backend", "bwrap"],
                 ["--git-token-env", "https://git.example.org=PATH", "--offline"],
                 ["--device", "/dev/console"], ["--device", "/dev/null"],
                 ["--gpu", "dri", "--podman-url", "unix:///example/podman.sock"]]
        monkeypatch.delenv("UNSET_CONTRACT_VARIABLE", raising=False)
        for args in cases:
            with pytest.raises(ValueError):
                s.validate(s.parse_args(["codex", *args]))
    else:
        workspace = tmp_path / ("new project" if scenario == "edge" else "work")
        args = s.parse_args(["codex", "--workspace", str(workspace), "--allow", "2001:db8::/32"])
        s.validate(args)
        assert args.workspace == str(workspace)
        assert not workspace.exists()


def test_prepare_workspace(scenario, tmp_path):
    path = tmp_path / "parent/work"
    if scenario == "sad":
        path.parent.mkdir()
        path.write_text("preserve")
        with pytest.raises(ValueError):
            s.prepare_workspace(path)
        assert path.read_text() == "preserve"
    else:
        s.prepare_workspace(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        (path / "marker").write_text("preserve")
        if scenario == "edge":
            s.prepare_workspace(path)
        assert (path / "marker").read_text() == "preserve"
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_resolve_hosts(scenario, monkeypatch):
    if scenario == "sad":
        monkeypatch.setattr(s.socket, "getaddrinfo", lambda *a, **kw: [])
        with pytest.raises(ValueError, match="No addresses"):
            s.resolve_hosts(["example.org"])
    else:
        values = ["203.0.113.1"] if scenario == "happy" else ["2001:db8::1", "203.0.113.1", "203.0.113.1"]
        monkeypatch.setattr(s.socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", (v, 443)) for v in values])
        assert s.resolve_hosts(["example.org"]) == {"example.org": sorted(set(values))}


def test_hosts_file(scenario):
    # Negative case: an empty permission list must not imply general DNS.
    values = {} if scenario == "sad" else {"example.org": ["203.0.113.1"]}
    if scenario == "edge":
        values["example.org"].append("2001:db8::1")
    text = s.hosts_file(values)
    assert text.startswith("127.0.0.1 localhost\n::1 localhost\n")
    assert "nameserver" not in text
    assert text.count(" example.org\n") == len(values.get("example.org", []))


def test_podman_command(scenario):
    options = ["--podman-url", "unix:///example/podman.sock"] if scenario == "edge" else []
    arg = "$(touch MUST_NOT_RUN)" if scenario == "sad" else "ps"
    cmd = s.podman_command(s.parse_args(["codex", *options]), "pod", arg)
    assert cmd[-2:] == ["pod", arg]
    assert ("--remote" in cmd) == (scenario == "edge")
    assert cmd[0] == "podman"


def test_resolve_pod_hosts(scenario, monkeypatch):
    run = Mock(return_value=Mock(stdout='{"example.org":["203.0.113.1","203.0.113.1"]}'))
    monkeypatch.setattr(s.subprocess, "run", run)
    args = s.parse_args(["codex"])
    if scenario == "sad":
        run.side_effect = subprocess.TimeoutExpired("podman", 45)
        with pytest.raises(subprocess.TimeoutExpired):
            s.resolve_pod_hosts(args, ["example.org"])
    elif scenario == "edge":
        assert s.resolve_pod_hosts(args, []) == {}
        run.assert_not_called()
    else:
        assert s.resolve_pod_hosts(args, ["example.org"]) == {"example.org": ["203.0.113.1"]}
        assert run.call_args.kwargs["timeout"] == 45


def test_firewall_rules(scenario):
    if scenario == "sad":
        with pytest.raises(ValueError):
            s.firewall_rules(["203.0.113.1; accept"], [])
    else:
        rules = s.firewall_rules(["192.0.2.5/24"] if scenario == "happy" else [],
                                 ["2001:db8::1", "2001:db8::1"] if scenario == "happy" else [])
        assert rules.count("policy drop") == 3
        assert rules.count("ip6 daddr 2001:db8::1 tcp dport 443 accept") == (1 if scenario == "happy" else 0)
        assert "udp dport 53 accept" not in rules


def test_copy_private(scenario, tmp_path):
    source, home = tmp_path / "source", tmp_path / "home"
    home.mkdir()
    if scenario == "sad":
        source.mkdir()
        with pytest.raises(ValueError, match="regular file"):
            s.copy_private(source, home, "target")
        assert not (home / "target").exists()
    else:
        source.write_bytes(b"" if scenario == "edge" else b"fixture-data")
        s.copy_private(source, home, "a/b/c")
        assert (home / "a/b/c").read_bytes() == source.read_bytes()
        assert stat.S_IMODE((home / "a/b/c").stat().st_mode) == 0o600
        assert stat.S_IMODE((home / "a").stat().st_mode) == 0o700


def test_auth_sources(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
    if scenario == "sad":
        assert not s.auth_sources("codex")[0][0].exists()
    elif scenario == "edge":
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "custom-data"))
        assert s.auth_sources("opencode")[0][0] == tmp_path / "custom-data/opencode/auth.json"
    else:
        assert s.auth_sources("codex")[0] == (tmp_path / "custom-codex/auth.json", ".codex/auth.json")


def test_auth_mode(scenario):
    options = {"happy": ["--copy-auth"], "sad": ["--no-auth"],
               "edge": ["--copy", "/example/metadata:.claude.json"]}[scenario]
    assert s.auth_mode(s.parse_args(["claude", *options])) == {"happy": "copy", "sad": "none", "edge": "manual"}[scenario]


def test_prepare_home(scenario, tmp_path):
    home = tmp_path / "sandbox-home"
    home.mkdir()
    if scenario == "sad":
        with pytest.raises(ValueError, match="Copy syntax"):
            s.prepare_home(s.parse_args(["codex", "--no-auth", "--copy", "invalid"]), home)
    elif scenario == "edge":
        source = tmp_path / "config.jsonc"
        source.write_text('{"model":"fixture"}')
        args = s.parse_args(["opencode", "--no-auth", "--local-config", str(source)])
        s.prepare_home(args, home)
        assert (home / ".config/opencode/opencode.jsonc").read_text() == '{"model":"fixture"}'
        # A reused HOME follows the host's config, even if the agent swapped its copy for a FIFO.
        source.write_text('{"model":"changed"}')
        (home / ".config/opencode/opencode.jsonc").unlink()
        os.mkfifo(home / ".config/opencode/opencode.jsonc")
        s.prepare_home(args, home)
        assert (home / ".config/opencode/opencode.jsonc").read_text() == '{"model":"changed"}'
    else:
        s.prepare_home(s.parse_args(["codex", "--no-auth"]), home)
        assert (home / ".codex").is_dir()
        assert not (home / ".codex/auth.json").exists()


def test_opencode_auth_from_codex(scenario, tmp_path):
    exp = {"happy": 2000000000, "sad": True, "edge": 1}[scenario]
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    source = tmp_path / "fixture.json"
    source.write_text(json.dumps({"tokens": {"access_token": "fixture." + payload + ".signature",
                                            "refresh_token": "fixture-refresh", "account_id": "fixture-account"}}))
    if scenario == "sad":
        with pytest.raises(ValueError, match="JWT expiry"):
            s.opencode_auth_from_codex(source)
    else:
        assert s.opencode_auth_from_codex(source)["openai"]["expires"] == exp * 1000


def test_namespace_state(scenario, monkeypatch):
    if scenario == "sad":
        monkeypatch.setattr(s.os, "readlink", Mock(side_effect=PermissionError("proc denied")))
        with pytest.raises(PermissionError):
            s.namespace_state()
    else:
        monkeypatch.setattr(s.os, "readlink", lambda p: "ns:" + p.rsplit("/", 1)[1])
        lines = "Name:\tignored\nCapEff:\t00000000\nNoNewPrivs:\t1\nSeccomp:\t2\n"
        if scenario == "edge":
            lines += "Unknown: ignored\n"
        monkeypatch.setattr(s.Path, "read_text", lambda *a, **kw: lines)
        result = s.namespace_state()
        assert result == dict(mnt="ns:mnt", pid="ns:pid", CapEff="00000000", NoNewPrivs="1", Seccomp="2")


def test_verify_inner(scenario):
    before, current = states()
    if scenario == "sad":
        for key in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb"):
            with pytest.raises(RuntimeError, match="capabilities"):
                s.verify_inner(before, dict(current, **{key: "8000000000000000"}))
    elif scenario == "edge":
        for key in ("mnt", "pid"):
            with pytest.raises(RuntimeError, match="namespaces"):
                s.verify_inner(before, dict(current, **{key: before[key]}))
    else:
        s.verify_inner(before, current)


def test_wait_child(scenario, monkeypatch):
    handlers, restored = {}, []
    def register(sig, handler):
        if callable(handler):
            handlers[sig] = handler
        else:
            restored.append((sig, handler))
        return "previous-handler"
    monkeypatch.setattr(s.signal, "signal", register)
    child = Mock()
    child.poll.return_value = 0 if scenario == "edge" else None
    def wait():
        for sig, handler in handlers.items():
            handler(sig, None)
        if scenario == "sad":
            raise OSError("wait failed")
        return 7
    child.wait.side_effect = wait
    if scenario == "sad":
        with pytest.raises(OSError, match="wait failed"):
            s.wait_child(child)
    else:
        assert s.wait_child(child) == 7
    assert len(restored) == 3
    assert child.send_signal.call_count == (0 if scenario == "edge" else 3)
    if scenario != "edge":
        assert [call.args[0] for call in child.send_signal.call_args_list] == [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]


def test_inner_bootstrap(scenario, monkeypatch):
    monkeypatch.setattr(s.os, "getpid", lambda: 1)
    libc = Mock()
    libc.prctl.return_value = 0
    monkeypatch.setattr(s.RUNTIME.ctypes, "CDLL", lambda *a, **kw: libc)
    run, popen = Mock(), Mock()
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    monkeypatch.setattr(sr, "namespace_state", lambda: states()[0])
    monkeypatch.setattr(sr, "firejail_command", lambda state: ["firejail-fixture"])
    monkeypatch.setattr(sr, "wait_child", lambda child: 0)
    if scenario == "sad":
        run.side_effect = subprocess.CalledProcessError(1, "nft")
        with pytest.raises(subprocess.CalledProcessError):
            s.inner_bootstrap()
        popen.assert_not_called()
    elif scenario == "edge":
        monkeypatch.setattr(s.os, "getpid", lambda: 2)
        with pytest.raises(RuntimeError, match="PID 1"):
            s.inner_bootstrap()
        run.assert_not_called()
        popen.assert_not_called()
    else:
        assert s.inner_bootstrap() == 0
        assert run.call_args_list[0].args[0] == ["nft", "-f", "/sandbox/session/firewall.nft"]
        assert run.call_args_list[1].args[0][:3] == ["nft", "list", "table"]
        assert popen.call_args.kwargs["close_fds"] is True


def test_firejail_command(scenario, monkeypatch):
    run = Mock(return_value=Mock(stdout="--shell=program" if scenario == "edge" else "--seccomp"))
    monkeypatch.setattr(s.subprocess, "run", run)
    if scenario == "sad":
        run.side_effect = FileNotFoundError("firejail")
        with pytest.raises(FileNotFoundError):
            s.firejail_command(states()[0])
    else:
        cmd = s.firejail_command(states()[0])
        assert ("--shell=none" in cmd) == (scenario == "edge")
        assert "--caps.drop=all" in cmd and "--noprofile" in cmd
        assert json.loads(cmd[-1]) == states()[0]


def test_base_env(scenario, monkeypatch):
    monkeypatch.setenv("HOST_SECRET_FIXTURE", "must-not-leak")
    if scenario == "edge":
        monkeypatch.setenv("TERM", "xterm-fixture")
    env = s.base_env()
    assert env["HOME"] == "/root" and env["CODEX_HOME"] == "/root/.codex"
    assert "HOST_SECRET_FIXTURE" not in env
    assert env["TERM"] == ("xterm-fixture" if scenario == "edge" else "xterm-256color")
    if scenario == "sad":
        monkeypatch.setenv("HOME", "/untrusted-home")
        assert s.base_env()["HOME"] == "/root"


def test_agent_environment(scenario, monkeypatch):
    monkeypatch.setenv("CONTRACT_VALUE", "fixture-value")
    options = ["--env", "CONTRACT_VALUE"]
    if scenario == "edge":
        options += ["--local-config", "/example/config.jsonc"]
    args = s.parse_args(["opencode", *options])
    if scenario == "sad":
        monkeypatch.delenv("CONTRACT_VALUE")
        with pytest.raises(KeyError):
            s.agent_environment(args)
    else:
        env = s.agent_environment(args)
        assert env["CONTRACT_VALUE"] == "fixture-value"
        assert (env.get("OPENCODE_DISABLE_PROJECT_CONFIG") == "1") == (scenario == "edge")


def test_agent_command(scenario):
    if scenario == "sad":
        args = s.parse_args(["codex", "--", "exec", "--sandbox=read-only", "task"])
        assert s.agent_command(args, "codex") == ["codex", *args.agent_args]
    else:
        tail = ["exec", "--", "--sandbox=literal-task"] if scenario == "edge" else ["exec", "task"]
        args = s.parse_args(["codex", "--", *tail])
        assert s.agent_command(args, "codex") == ["codex", "--sandbox", "danger-full-access", *tail]


def test_inner_guard(scenario, monkeypatch, capsys):
    before, current = states()
    monkeypatch.setattr(sr, "namespace_state", lambda: current)
    config = {"probe": scenario == "edge", "env": {"CONTRACT_VALUE": "fixture"}, "command": ["agent", "task"]}
    read = Mock(return_value=json.dumps(config))
    monkeypatch.setattr(s.Path, "read_text", read)
    chdir, execute = Mock(), Mock()
    monkeypatch.setattr(s.os, "chdir", chdir)
    monkeypatch.setattr(s.os, "execvpe", execute)
    if scenario == "sad":
        current["CapEff"] = "1"
        with pytest.raises(RuntimeError):
            s.inner_guard(before)
        read.assert_not_called()
        execute.assert_not_called()
    elif scenario == "edge":
        assert s.inner_guard(before) == 0
        assert json.loads(capsys.readouterr().out)["verified"] is True
        execute.assert_not_called()
        chdir.assert_not_called()
    else:
        s.inner_guard(before)
        chdir.assert_called_once_with("/work")
        cmd, argv, env = execute.call_args.args
        assert cmd == "agent" and argv == ["agent", "task"]
        assert env["CONTRACT_VALUE"] == "fixture" and env["HOME"] == "/root"
        config["tmux"] = True
        read.return_value = json.dumps(config)
        s.inner_guard(before)
        cmd, argv, env = execute.call_args.args
        assert cmd == "tmux" and argv == s.tmux_command(["agent", "task"])


def test_mount_option(scenario):
    if scenario == "sad":
        for source in ("/tmp/a,b", "/tmp/a\nb", "/tmp/a\rb"):
            with pytest.raises(ValueError):
                s.mount_option(source, "/work", "rw")
    else:
        mode = "ro" if scenario == "edge" else "rw"
        assert s.mount_option("/tmp/space dir", "/work", mode) == ["--mount", f"type=bind,src=/tmp/space dir,dst=/work,{mode},nodev"]


def test_pod_commands(scenario, tmp_path, monkeypatch):
    options = ["--offline"] if scenario == "sad" else []
    if scenario == "edge":
        options = ["--network", "research", "--ip", "192.0.2.2", "--ip6", "2001:db8::2", "--workspace-ro"]
    args = s.parse_args(["codex", "--workspace", str(tmp_path), *options])
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: scenario == "edge")
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: scenario == "edge")
    create, run = s.pod_commands(args, "owned-fixture", tmp_path / "session", tmp_path / "home")
    assert create[create.index("--network") + 1] == ("none" if scenario == "sad" else "research" if scenario == "edge" else s.POD_NETWORK)
    if scenario == "happy":
        forwarded = s.parse_args(["codex", "--workspace", str(tmp_path), "--allow-host-port", "3000"])
        create, _ = s.pod_commands(forwarded, "owned-fixture", tmp_path / "session", tmp_path / "home")
        assert create[create.index("--network") + 1] == s.pod_network(forwarded)
        assert "-T,3000" in create[create.index("--network") + 1]
    assert ("--tty" in run) == (scenario == "edge")
    assert not any(v.startswith("--publish") for v in run)
    assert "--label" not in create
    if scenario == "happy":
        # Session pods carry their session's tmux directory, so a stale one can be recognised.
        session = s.parse_args(["codex", "--workspace", str(tmp_path), "--pod-name", "agent-session-x",
                                "--tmux-socket-dir", str(tmp_path / "sess" / "tmux")])
        create, _ = s.pod_commands(session, "agent-session-x", tmp_path / "session", tmp_path / "home")
        assert create[create.index("--label") + 1] == f"{s.SESSION_LABEL}={(tmp_path / 'sess' / 'tmux').resolve()}"
    assert f"type=bind,src={tmp_path},dst=/work,{'ro' if scenario == 'edge' else 'rw'},nodev" in run
    assert "--device" not in run
    if scenario == "happy":
        # Single devices and CDI names through --device; directories as bind mounts, so replugged devices show.
        monkeypatch.setattr(s, "devices", lambda a: [("/dev/ttyACM0", "node"), ("/dev/bus/usb", "dir"),
                                                     ("nvidia.com/gpu=all", "cdi")])
        _, run = s.pod_commands(args, "owned-fixture", tmp_path / "session", tmp_path / "home")
        assert [run[i + 1] for i, v in enumerate(run) if v == "--device"] == ["/dev/ttyACM0", "nvidia.com/gpu=all"]
        assert "type=bind,src=/dev/bus/usb,dst=/dev/bus/usb,rw,dev" in run


def test_run_pod(scenario, tmp_path, monkeypatch):
    args = s.parse_args(["codex", "--offline"])
    run, popen, wait = Mock(), Mock(), Mock(return_value=9 if scenario == "edge" else 0)
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    monkeypatch.setattr(sr, "wait_child", wait)
    if scenario == "sad":
        popen.side_effect = OSError("spawn failed")
        with pytest.raises(OSError, match="spawn failed"):
            s.run_pod(args, tmp_path, tmp_path)
    else:
        assert s.run_pod(args, tmp_path, tmp_path) == (9 if scenario == "edge" else 0)
    cleanup = run.call_args_list[-1].args[0]
    assert cleanup[:4] == ["podman", "pod", "rm", "--force"]
    assert cleanup[-1].startswith("agent-codex-")


def test_bwrap_command(scenario, tmp_path, monkeypatch):
    args = s.parse_args(["codex", "--backend", "bwrap", "--workspace", str(tmp_path)])
    if scenario == "sad":
        args.runtime = [str(tmp_path)]
        with pytest.raises(ValueError, match="runtime"):
            s.bwrap_command(args, tmp_path, tmp_path)
    else:
        args.workspace_ro = scenario == "edge"
        cmd = s.bwrap_command(args, tmp_path, tmp_path)
        assert "--share-net" not in cmd and "--clearenv" in cmd
        position = cmd.index("/work")
        assert cmd[position - 2] == ("--ro-bind" if scenario == "edge" else "--bind")
        assert "--dev-bind" not in cmd
        monkeypatch.setattr(s, "devices", lambda a: [("/dev/kvm", "node"), ("/dev/dri", "dir")])
        cmd = s.bwrap_command(args, tmp_path, tmp_path)
        assert [cmd[i + 1:i + 3] for i, v in enumerate(cmd) if v == "--dev-bind"] == [["/dev/kvm", "/dev/kvm"],
                                                                                     ["/dev/dri", "/dev/dri"]]
        assert cmd.index("--dev-bind") > cmd.index("--dev")  # on top of the sandbox's own /dev
        monkeypatch.setattr(s, "devices", lambda a: [("nvidia.com/gpu=all", "cdi")])
        with pytest.raises(ValueError, match="CDI"):
            s.bwrap_command(args, tmp_path, tmp_path)


def test_read_ready(scenario):
    read_fd, write_fd = os.pipe()
    child = Mock(returncode=3)
    child.poll.return_value = None
    try:
        if scenario == "sad":
            os.close(write_fd)
            write_fd = -1
            with pytest.raises(RuntimeError, match="exited early"):
                s.read_ready(read_fd, child, timeout=0.2)
        else:
            value = b'1' if scenario == "edge" else b'{"child-pid":12345}'
            os.write(write_fd, value)
            assert s.read_ready(read_fd, child, timeout=0.2) == (1 if scenario == "edge" else {"child-pid": 12345})
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_stop_process(scenario):
    process = Mock()
    process.poll.return_value = 0 if scenario == "edge" else None
    if scenario == "happy":
        # The proxy: its stdin closes, it exits; no signal (it runs as another user, without CAP_KILL).
        s.stop_process(process)
        assert process.stdin.close.call_count == 1 and process.terminate.call_count == 0
        # Still running after that and not ours to signal: left to the end of the PID namespace.
        other = Mock()
        other.poll.return_value = None
        other.wait.side_effect = subprocess.TimeoutExpired("fixture", 2)
        other.terminate.side_effect = PermissionError(1, "Operation not permitted")
        s.stop_process(other)
        assert other.kill.call_count == 0
    elif scenario == "sad":
        process.stdin = None
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), 0]
        s.stop_process(process)
        assert process.terminate.call_count == 1 and process.kill.call_count == 1
    else:
        s.stop_process(process)
        assert process.terminate.call_count == 0 and process.kill.call_count == 0
    s.stop_process(None)


def test_exit_on_eof(scenario):
    import io
    exits = []
    stream = {"happy": io.BytesIO(b"x" * 10000), "sad": Mock(read=Mock(side_effect=OSError(9, "bad"))),
              "edge": io.BytesIO(b"")}[scenario]
    rt.exit_on_eof(stream, exit=exits.append)
    assert exits == [0]


def test_subordinate_range(scenario, monkeypatch):
    monkeypatch.setattr(s.pwd, "getpwuid", lambda uid: Mock(pw_name="fixture-user"))
    monkeypatch.setattr(s.os, "getuid", lambda: 1234)
    entries = {"happy": "fixture-user:100000:65536\n", "sad": "fixture-user:100000:65535\n",
               "edge": "invalid\nother:200000:65536\n1234:300000:65536\n"}[scenario]
    monkeypatch.setattr(s.Path, "read_text", lambda *a, **kw: entries)
    if scenario == "sad":
        with pytest.raises(ValueError, match="65536"):
            s.subordinate_range("uid")
    else:
        assert s.subordinate_range("uid") == (300000 if scenario == "edge" else 100000, 65536)


def test_configure_user_maps(scenario, monkeypatch):
    monkeypatch.setattr(s, "subordinate_range", lambda kind: (100000, 65536))
    run = Mock()
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.Path, "read_text", lambda *a, **kw: "allow\n" if scenario != "edge" else "deny\n")
    if scenario == "sad":
        run.side_effect = subprocess.CalledProcessError(1, "newuidmap")
        with pytest.raises(subprocess.CalledProcessError):
            s.configure_user_maps(12345)
        assert run.call_count == 1
    elif scenario == "edge":
        with pytest.raises(RuntimeError, match="setgroups"):
            s.configure_user_maps(12345)
    else:
        s.configure_user_maps(12345)
        assert [c.args[0][0] for c in run.call_args_list] == ["newuidmap", "newgidmap"]


def test_run_bwrap(scenario, tmp_path, monkeypatch):
    args = s.parse_args(["codex", "--backend", "bwrap", *(["--offline"] if scenario == "edge" else [])])
    monkeypatch.setattr(s, "bwrap_command", lambda *a: ["bwrap-fixture"])
    children = [Mock(), Mock()]
    popen = Mock(side_effect=children)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    ready = Mock(side_effect=[{"child-pid": 12345}, True])
    monkeypatch.setattr(s, "read_ready", ready)
    maps = Mock(side_effect=RuntimeError("mapping failed") if scenario == "sad" else None)
    monkeypatch.setattr(s, "configure_user_maps", maps)
    monkeypatch.setattr(sr, "wait_child", lambda child: 0)
    stop = Mock()
    monkeypatch.setattr(sr, "stop_process", stop)
    write = Mock()
    monkeypatch.setattr(s.os, "write", write)
    real_pipe, descriptors = os.pipe, []
    def pipe():
        pair = real_pipe()
        descriptors.extend(pair)
        return pair
    monkeypatch.setattr(s.os, "pipe", pipe)
    if scenario == "sad":
        with pytest.raises(RuntimeError, match="mapping failed"):
            s.run_bwrap(args, tmp_path, tmp_path)
        write.assert_not_called()
        assert popen.call_count == 1
    else:
        assert s.run_bwrap(args, tmp_path, tmp_path) == 0
        assert popen.call_count == (1 if scenario == "edge" else 2)
        assert write.call_count == 2
    assert stop.call_count == 2
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_main(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    runner = Mock(return_value=0)
    monkeypatch.setattr(s, "run_pod", runner)
    options = ["codex", "--workspace", str(tmp_path / "work"), "--offline", "--no-auth"]
    if scenario == "sad":
        monkeypatch.setattr(s.os, "geteuid", lambda: 0)
        with pytest.raises(ValueError, match="ordinary host user"):
            s.main(options)
        runner.assert_not_called()
        assert not (tmp_path / "work").exists()
    else:
        seen = {}
        def inspect(args, session, home):
            seen.update(config=json.loads((session / "config.json").read_text()), home=home, session=session)
            assert (session / "config.json").stat().st_mode & 0o777 == 0o600
            return 0
        runner.side_effect = inspect
        if scenario == "edge":
            options += ["--state-dir", str(tmp_path / "persistent")]
        assert s.main(options) == 0
        assert seen["config"]["command"][:3] == ["codex", "--sandbox", "danger-full-access"]
        assert not seen["session"].exists()
        assert seen["home"].exists() == (scenario == "edge")


def test_regression_later_parent_mount_hides_previous_child(tmp_path):
    """The most recent parent bind determines the visible destination tree."""
    workspace, parent, child = [tmp_path / p for p in ("work", "parent", "child")]
    for path in (workspace, parent, child):
        path.mkdir()
    (parent / "child").symlink_to("/sandbox", target_is_directory=True)
    args = s.parse_args(["codex", "--workspace", str(workspace),
                         "--mount", f"{child}:/mnt/data/child:rw",
                         "--mount", f"{parent}:/mnt/data:rw",
                         "--mount", f"{workspace}:/mnt/data/child/session:rw"])
    with pytest.raises(ValueError, match="symlink"):
        s.validate(args)


def test_regression_partial_pipe_allocation_closes_owned_descriptors(tmp_path, monkeypatch):
    real_pipe = os.pipe
    allocated = []
    def pipe():
        if allocated:
            raise OSError("fixture pipe allocation failure")
        pair = real_pipe()
        allocated.extend(pair)
        return pair
    monkeypatch.setattr(s.os, "pipe", pipe)
    try:
        with pytest.raises(OSError, match="pipe allocation"):
            s.run_bwrap(s.parse_args(["codex", "--backend", "bwrap"]), tmp_path, tmp_path)
        for fd in allocated:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        for fd in allocated:
            try:
                os.close(fd)
            except OSError:
                pass


class StopLoop(BaseException):
    """Ends an endless logger loop from a mocked boundary."""


def claude_credentials(path, expires, refresh="refresh-fixture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "access-fixture", "refreshToken": refresh,
                                                  "expiresAt": expires, "scopes": ["user:inference"]}}))
    return path


def test_host_networks(scenario):
    hosts = {"happy": ["api.anthropic.com"], "sad": ["api.openai.com"], "edge": []}[scenario]
    assert s.host_networks(hosts) == (["160.79.104.0/23", "2607:6bc0::/48"] if scenario == "happy" else [])


def test_log_event(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / ("missing" if scenario == "sad" else "log"))
    if scenario == "sad":
        with pytest.raises(OSError):
            s.log_event("files.jsonl", {"action": "read"})
        return
    (tmp_path / "log").mkdir()
    for _ in range(2 if scenario == "edge" else 1):
        s.log_event("files.jsonl", {"path": "/work/\u00e4 \"quoted\""})
    lines = [json.loads(line) for line in (tmp_path / "log/files.jsonl").read_text().splitlines()]
    assert len(lines) == (2 if scenario == "edge" else 1)
    assert lines[0]["path"] == "/work/\u00e4 \"quoted\"" and lines[0]["time"].endswith("Z")


def test_start_loggers(scenario, tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    monkeypatch.setattr(s.RUNTIME, "SESSION_CONFIG", config)
    started = []
    monkeypatch.setattr(s.RUNTIME.threading, "Thread", lambda target, args, daemon: Mock(start=lambda: started.append((target, args))))
    if scenario == "sad":
        with pytest.raises(OSError):
            s.start_loggers()
        assert started == []
        return
    config.write_text(json.dumps({"pinned": ["api.openai.com"] if scenario == "happy" else [], "watch": ["/work"],
                                  "env": {"API_KEY": "value-of-the-key", "PATH": "/usr/bin"},
                                  "secret_names": ["API_KEY", "MISSING"] if scenario == "happy" else []}))
    stop = s.start_loggers()
    assert not stop.is_set() and stop.seen == set() and len(stop.threads) == 4
    targets = {target.__name__: args for target, args in started}
    assert targets["dns_logger"] == ({"api.openai.com"} if scenario == "happy" else set(), 53, stop, None, ())
    assert targets["file_logger"] == (["/work"], stop)
    # The process log redacts the values of --env and --secret-env variables by value.
    assert "denied_loop" in targets and targets["process_logger"] == (
        stop, 0.5, Path("/proc"), ["value-of-the-key"] if scenario == "happy" else [])


def test_poll_denied(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path)
    elements = {"happy": [{"concat": ["198.51.100.7", "tcp", 80]}],
                "edge": [{"elem": {"val": {"concat": ["2001:db8::9", "udp", 123]}, "expires": 1}},
                         {"concat": ["198.51.100.7", "tcp", 80]}]}.get(scenario, [])
    output = json.dumps({"nftables": [{"metainfo": {}}, {"set": {"name": "denied4", "elem": elements}}]})
    run = Mock(return_value=Mock(returncode=1 if scenario == "sad" else 0, stdout=output))
    monkeypatch.setattr(s.subprocess, "run", run)
    seen = {("198.51.100.7", "tcp", 80)} if scenario == "edge" else set()
    s.poll_denied(seen)
    s.poll_denied(seen)
    log = tmp_path / "egress-denied.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert [r.get("address") for r in records] == {"happy": ["198.51.100.7"], "sad": [], "edge": ["2001:db8::9"]}[scenario]
    assert run.call_args_list[0].args[0] == ["nft", "-j", "list", "set", "inet", "agent_sandbox", "denied4"]


def test_dns_reply(scenario):
    question = (b"\x07example\x03org\x00" if scenario != "edge" else b"\x03API\x07Example\x03COM\x00") + b"\x00\x01\x00\x01"
    query = b"\xbe\xef" + (b"\x01\x00" if scenario == "happy" else b"\x00\x00") + b"\x00\x01\x00\x00\x00\x00\x00\x00" + question
    if scenario == "sad":
        with pytest.raises(struct_error()):
            s.dns_reply(b"\x00")
        return
    name, reply = s.dns_reply(query)
    assert name == ("example.org" if scenario == "happy" else "api.example.com")
    assert reply[:2] == b"\xbe\xef" and reply[12:] == question
    flags = int.from_bytes(reply[2:4], "big")
    assert flags & 0xF == 3 and flags & 0x8000 and bool(flags & 0x0100) == (scenario == "happy")


def struct_error():
    import struct
    return struct.error


def test_dns_logger(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path)
    name = {"happy": b"\x04evil\x07example\x00", "edge": b"\x03api\x06openai\x03com\x00"}.get(scenario, b"")
    packet = b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + name + b"\x00\x01\x00\x01"
    sock = Mock()
    sock.recvfrom.side_effect = [(b"\x01" if scenario == "sad" else packet, ("127.0.0.1", 5353)), StopLoop()]
    monkeypatch.setattr(s.socket, "socket", lambda *a: sock)
    with pytest.raises(StopLoop):
        s.dns_logger({"api.openai.com"})
    sock.bind.assert_called_once_with(("127.0.0.1", 53))
    log = tmp_path / "egress-denied.jsonl"
    hosts = [json.loads(line)["host"] for line in log.read_text().splitlines()] if log.exists() else []
    assert hosts == (["evil.example"] if scenario == "happy" else [])
    assert sock.sendto.called == (scenario != "sad")


def test_file_logger(scenario, tmp_path, monkeypatch):
    import struct
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / "log")
    (tmp_path / "log").mkdir()
    root = tmp_path / "work"
    (root / "sub").mkdir(parents=True)
    libc = Mock()
    libc.inotify_init1.return_value = -1 if scenario == "sad" else 7
    watched = []
    libc.inotify_add_watch.side_effect = lambda fd, path, mask: watched.append(os.fsdecode(path)) or len(watched)
    monkeypatch.setattr(s.RUNTIME.ctypes, "CDLL", lambda *a, **kw: libc)
    def event(wd, mask, name):
        raw = name.encode() + b"\0" * (16 - len(name))
        return struct.pack("iIII", wd, mask, 0, len(raw)) + raw
    reads = {"happy": [event(1, 0x8, "a.txt") + event(1, 0x10, "a.txt") + event(1, 0x8, "a.txt")],
             "edge": [event(1, 0x40000100, "newdir") + event(1, 0x40000010, "sub") + event(1, 0x4000, "")]}
    monkeypatch.setattr(s.os, "read", Mock(side_effect=[*reads.get(scenario, []), StopLoop()]))
    if scenario == "edge":
        (root / "newdir").mkdir()
    if scenario == "sad":
        s.file_logger([str(root)])
    else:
        with pytest.raises(StopLoop):
            s.file_logger([str(root)])
    records = [json.loads(line) for line in (tmp_path / "log/files.jsonl").read_text().splitlines()]
    if scenario == "happy":
        assert sorted(watched) == [str(root), str(root / "sub")]
        assert [(r["action"], r["path"]) for r in records] == [("write", str(root / "a.txt")), ("read", str(root / "a.txt"))]
    elif scenario == "edge":
        assert str(root / "newdir") in watched
        assert [r.get("action") for r in records] == ["create", None]
        assert "overflow" in records[1]["error"]
    else:
        assert records == [{"time": records[0]["time"], "error": "inotify unavailable"}]


def test_oauth_fields(scenario):
    oauth = {"happy": {"accessToken": "a", "expiresAt": 5, "scopes": ["x"]},
             "sad": {"accessToken": 1, "expiresAt": True, "scopes": ["x", 2], "evil": "y"},
             "edge": {"expiresAt": 1.5, "refreshTokenExpiresAt": 0, "subscriptionType": "max"}}[scenario]
    fields = {**s.OAUTH_TOKEN_FIELDS, **s.OAUTH_INFO_FIELDS}
    expected = {"happy": oauth, "sad": {}, "edge": oauth}[scenario]
    assert s.oauth_fields(oauth, fields) == expected


def test_claude_oauth(scenario, tmp_path):
    path = tmp_path / "credentials.json"
    if scenario == "sad":
        path.write_text('{"claudeAiOauth": {"accessToken": "", "refreshToken": "x", "expiresAt": 1}}')
        assert s.claude_oauth(path) is None
        assert s.claude_oauth(tmp_path / "missing.json") is None
    else:
        claude_credentials(path, 2.5e12 if scenario == "edge" else 2000)
        assert s.claude_oauth(path)["refreshToken"] == "refresh-fixture"


def test_sync_claude_credentials(scenario, tmp_path):
    source = claude_credentials(tmp_path / "sandbox/.credentials.json", 3000, "rotated")
    target = tmp_path / "host/.credentials.json"
    if scenario == "sad":
        claude_credentials(target, 4000, "host-newer")
        assert s.sync_claude_credentials(source, target) is False
        assert s.claude_oauth(target)["refreshToken"] == "host-newer"
        assert s.sync_claude_credentials(tmp_path / "missing.json", target) is False
    else:
        claude_credentials(target, 1000, "stale")
        if scenario == "edge":
            document = json.loads(target.read_text())
            target.write_text(json.dumps({**document, "mcpOAuth": {"keep": True}}))
        assert s.sync_claude_credentials(source, target) is True
        document = json.loads(target.read_text())
        assert document["claudeAiOauth"]["refreshToken"] == "rotated"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert (document.get("mcpOAuth") == {"keep": True}) == (scenario == "edge")
        assert sorted(p.name for p in target.parent.iterdir()) == [".credentials.json"]


def test_claude_credential_pair(scenario, tmp_path, monkeypatch):
    home = tmp_path / "home"
    if scenario == "sad":
        assert s.claude_credential_pair(s.parse_args(["claude", "--no-auth"]), home) is None
        assert s.claude_credential_pair(s.parse_args(["codex"]), home) is None
    else:
        options = ["claude"] + (["--copy-auth"] if scenario == "edge" else [])
        if scenario == "edge":
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
        host, sandbox = s.claude_credential_pair(s.parse_args(options), home)
        expected = tmp_path / "custom" if scenario == "edge" else tmp_path / "host/.claude"
        assert host == expected / ".credentials.json"
        assert sandbox == home / ".claude/.credentials.json"


def test_nameservers(scenario):
    if scenario == "sad":
        assert s.nameservers(s.parse_args(["codex"])) == []
        assert s.nameservers(s.parse_args(["codex", "--offline"])) == []
    elif scenario == "happy":
        assert s.nameservers(s.parse_args(["codex", "--dns"])) == ["1.1.1.1", "8.8.8.8"]
        assert s.nameservers(s.parse_args(["codex", "--egress-all-allow"])) == ["1.1.1.1", "8.8.8.8"]
    else:
        args = s.parse_args(["codex", "--nameserver", "9.9.9.9", "--nameserver", "2620:fe::fe", "--nameserver", "9.9.9.9"])
        assert s.nameservers(args) == ["9.9.9.9", "2620:fe::fe"]


def test_resolver_files(scenario):
    options = {"happy": ["--dns"], "sad": [], "edge": ["--log-dir", "/tmp/contract-logs"]}[scenario]
    resolv, nsswitch = s.resolver_files(s.parse_args(["codex", *options]))
    if scenario == "happy":
        assert resolv == "nameserver 1.1.1.1\nnameserver 8.8.8.8\n"
    elif scenario == "edge":
        assert resolv == "nameserver 127.0.0.1\n"
    else:
        assert "nameserver" not in resolv
    assert nsswitch.endswith("hosts: files dns\n" if scenario != "sad" else "hosts: files\n")


def test_workspace_targets(scenario, tmp_path):
    main, lib = tmp_path / "main", tmp_path / "lib"
    if scenario == "sad":
        args = s.parse_args(["codex", "--workspace", str(main)])
        assert s.workspace_targets(args) == [(str(main), "/work")]
    elif scenario == "happy":
        args = s.parse_args(["codex", "--workspace", f"{main},{lib}"])
        assert s.workspace_targets(args) == [(str(main), "/work"), (str(lib), "/workspaces/lib")]
    else:
        other = tmp_path / "other" / "lib"
        args = s.parse_args(["codex", "--workspace", str(main), "--workspace", f"{lib},{other}", "--workspace", str(tmp_path / "x y")])
        assert s.workspace_targets(args) == [(str(main), "/work"), (str(lib), "/workspaces/lib"),
                                             (str(other), "/workspaces/lib-2"), (str(tmp_path / "x y"), "/workspaces/x y")]


def test_check_private_dir(scenario, tmp_path):
    work = tmp_path / "work"
    args = s.parse_args(["codex", "--workspace", f"{work},{tmp_path / 'lib'}"])
    if scenario == "sad":
        for inside in (work / "logs", tmp_path / "lib", tmp_path):
            with pytest.raises(ValueError, match="--gui-vnc must lie outside"):
                s.check_private_dir(args, inside, "--gui-vnc")
    elif scenario == "edge":
        with pytest.raises(ValueError, match="control characters"):
            s.check_private_dir(args, tmp_path / "a,b", "--log-dir")
    else:
        s.check_private_dir(args, tmp_path / "private", "--log-dir")


def test_wayland_socket(scenario, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    if scenario == "sad":
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        with pytest.raises(ValueError, match="WAYLAND_DISPLAY"):
            s.wayland_socket()
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-9")
        (runtime / "wayland-9").write_text("not a socket")
        with pytest.raises(ValueError, match="not found"):
            s.wayland_socket()
        return
    server = socket.socket(socket.AF_UNIX)
    path = runtime / "wayland-1" if scenario == "happy" else tmp_path / "abs-wayland"
    server.bind(str(path))
    try:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1" if scenario == "happy" else str(path))
        assert s.wayland_socket() == path
    finally:
        server.close()


def test_gui_config(scenario, tmp_path):
    options = {"happy": ["--gui"], "sad": [], "edge": ["--gui-vnc", str(tmp_path / "vnc"), "--gui-screen", "1280x720x16"]}[scenario]
    config = s.gui_config(s.parse_args(["codex", *options]))
    if scenario == "sad":
        assert config is None
    else:
        assert config == {"display": ":99", "screen": "1920x1080x24" if scenario == "happy" else "1280x720x16",
                          "vnc": scenario == "edge"}


def test_start_gui(scenario, tmp_path, monkeypatch):
    socket_path = tmp_path / "X99"
    monkeypatch.setattr(sr, "x_socket", lambda display: socket_path)
    started = []
    def popen(cmd, **kwargs):
        started.append(cmd)
        if cmd[-1] != "-quiet" and scenario != "sad":
            socket_path.write_text("")
        return Mock(poll=Mock(return_value=1 if scenario == "sad" else None))
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    stopped = []
    monkeypatch.setattr(sr, "stop_process", stopped.append)
    monkeypatch.setattr(s.time, "sleep", lambda _: None)
    waited = []
    monkeypatch.setattr(rt, "wait_for_unix_listener", lambda path, process, errors, name: waited.append((str(path), name)))
    config = {"display": ":99", "screen": "1920x1080x24", "vnc": scenario == "edge"}
    if scenario == "sad":
        with pytest.raises(RuntimeError, match="Xvfb exited"):
            s.start_gui(config)
        assert len(stopped) == 1
        return
    processes = s.start_gui(config)
    assert len(processes) == (2 if scenario == "edge" else 1)
    drop = ["setpriv", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "--"]
    assert started[0] == drop + ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"]
    if scenario == "edge":
        assert started[1] == drop + ["x11vnc", "-display", ":99", "-unixsock", "/sandbox/vnc/vnc.sock",
                                     "-rfbport", "0", "-forever", "-shared", "-nopw", "-quiet", "-nosel"]
        assert waited == [("/sandbox/vnc/vnc.sock", "x11vnc")]  # started only once it listens


def test_x_socket(scenario):
    display = {"happy": ":99", "sad": ":0", "edge": ":1234"}[scenario]
    assert s.x_socket(display) == Path("/tmp/.X11-unix/X" + display[1:])


def test_auth_plan(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(s, "auth_sources", lambda name: [(tmp_path / "primary.json", s.AUTH_TARGETS[name]),
                                                         (tmp_path / "meta.json", ".claude.json")])
    if scenario == "sad":
        monkeypatch.setattr(s, "auth_sources", Mock(side_effect=AssertionError("no discovery")))
        assert s.auth_plan(s.parse_args(["claude"])).startswith("automatic")
        assert s.auth_plan(s.parse_args(["claude", "--no-auth"])) == []
    elif scenario == "happy":
        assert s.auth_plan(s.parse_args(["claude", "--copy-auth"])) == [
            {"source": str(tmp_path / "primary.json"), "target": "/root/.claude/.credentials.json"}]
        (tmp_path / "meta.json").write_text("{}")
        assert len(s.auth_plan(s.parse_args(["claude", "--copy-auth"]))) == 2
    else:
        plan = s.auth_plan(s.parse_args(["opencode", "--copy", f"{tmp_path}/x.json:./.local/share/opencode/auth.json",
                                         "--copy", f"{tmp_path}/y:.config/other"]))
        assert plan == [{"source": f"{tmp_path}/x.json", "target": "/root/.local/share/opencode/auth.json"}]
        assert s.auth_plan(s.parse_args(["opencode", "--import-codex-auth", f"{tmp_path}/c.json"])) == [
            {"source": f"{tmp_path}/c.json", "target": "/root/.local/share/opencode/auth.json"}]


def test_tmux_command(scenario):
    import shlex
    command = {"happy": ["claude"], "sad": ["codex", "exec", "a b; rm -rf / $(x) && echo x"], "edge": ["opencode", "run", "it's"]}[scenario]
    cmd = s.tmux_command(command)
    assert cmd[:8] == ["tmux", "-u", "new-session", "-s", "sandbox", "-n", "agent", "-c"]
    assert shlex.split(cmd[9]) == command
    assert cmd[10:] == [";", "new-window", "-d", "-n", "shell", "-c", "/work", "bash"]
    # Detached sessions: the guard keeps running while the tmux session exists.
    detached = s.tmux_command(command, detached=True)
    assert detached[:2] == ["sh", "-c"]
    tmux_part, _, loop = detached[2].rpartition(" && ")
    assert shlex.split(tmux_part)[:4] == ["tmux", "-u", "new-session", "-d"]
    assert shlex.split(shlex.split(tmux_part)[10]) == command
    assert loop == "while tmux has-session -t sandbox 2>/dev/null; do sleep 1; done"
    # The relay that terminals attach through starts inside the same confinement.
    assert "--internal-attach-relay /sandbox/tmux/attach.sock" in tmux_part


def test_claude_token_status(scenario, tmp_path):
    path = tmp_path / "credentials.json"
    expires = {"happy": time.time() + 3600, "sad": 0, "edge": time.time() - 60}[scenario] * 1000
    claude_credentials(path, expires)
    status = s.claude_token_status(path)
    assert "access-fixture" not in status and "refresh-fixture" not in status
    assert status.startswith({"happy": "Claude access token valid", "sad": "Claude access token expired",
                              "edge": "Claude access token expired"}[scenario])


def test_load_sessions_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_sessions_module()
    else:
        module = s.load_sessions_module()
        assert callable(module.start) and callable(module.cli)
        if scenario == "edge":
            assert s.load_sessions_module() is not module


def test_tcp_targets(scenario):
    values = {"happy": ["10.0.0.5:11434"], "sad": ["10.0.0.5:99999"], "edge": ["[fe80::1]:1", "10.0.0.5:65535"]}[scenario]
    args = s.parse_args(["opencode", *[x for v in values for x in ("--allow-tcp", v)]])
    if scenario == "sad":
        with pytest.raises(ValueError, match="range"):
            s.tcp_targets(args)
    else:
        expected = {"happy": [("10.0.0.5", 11434)], "edge": [("fe80::1", 1), ("10.0.0.5", 65535)]}[scenario]
        assert s.tcp_targets(args) == expected


def test_host_ports(scenario):
    values = {"happy": ["3000"], "sad": ["0"], "edge": ["8080", "3000", "8080", "65535"]}[scenario]
    args = s.parse_args(["claude", *[x for v in values for x in ("--allow-host-port", v)]])
    if scenario == "sad":
        for bad in ["0", "65536", "-1", "3000,22", "3000:22", " 22", "２２", "1e3"]:
            args.allow_host_port = [bad]
            with pytest.raises(ValueError, match="allow-host-port"):
                s.host_ports(args)
    else:
        assert s.host_ports(args) == {"happy": [3000], "edge": [3000, 8080, 65535]}[scenario]


def test_pod_network(scenario):
    options = {"happy": ["--allow-host-port", "3000"], "sad": ["--offline"],
               "edge": ["--allow-host-port", "8080", "--allow-host-port", "3000"]}[scenario]
    network = s.pod_network(s.parse_args(["claude", *options]))
    if scenario == "sad":
        assert network == "none"
        assert s.pod_network(s.parse_args(["claude", "--network", "research"])) == "research"
        # No host ports: the default stays fully closed towards the host.
        assert s.pod_network(s.parse_args(["claude"])) == s.POD_NETWORK
        return
    expected = {"happy": "-T,3000", "edge": "-T,3000,-T,8080"}[scenario]
    assert network == f"pasta:--no-map-gw,-t,none,-u,none,{expected},-U,none"
    # Only namespace-to-host TCP forwards change; inbound and UDP stay off.
    assert "-t,none" in network and "-u,none" in network and "-U,none" in network and "-T,none" not in network


TOKEN = "github_pat_" + "A1b2" * 10


def test_github_token(scenario, monkeypatch):
    """A token for https://github.com opens the GitHub preset and becomes GH_TOKEN; the former
    --github-token-env/-file aliases are gone."""
    monkeypatch.setenv("CONTRACT_GH", TOKEN)
    if scenario == "happy":
        args = s.parse_args(["claude", "--git-token-env", "https://github.com=CONTRACT_GH"])
        assert "github" in args.presets
        assert args.allow_host == []  # the preset covers github.com, api.github.com and the download hosts
        env = s.git_environment(args)
        assert env["GH_TOKEN"] == TOKEN == env["SANDBOX_GIT_TOKEN_0"]
    elif scenario == "sad":
        for removed in (["--github-token-env", "CONTRACT_GH"], ["--github-token-file", "/x"]):
            with pytest.raises(SystemExit):
                s.parse_args(["claude", *removed])
        assert "--github-token" not in s.build_parser().format_help()
    else:
        # The same URL with and without a trailing slash counts once.
        args = s.parse_args(["claude", "--git-token-env", "https://github.com=CONTRACT_GH",
                             "--git-token-env", "https://github.com/=CONTRACT_GH"])
        assert len(s.git_tokens(args)) == 1


def test_network_profiles(scenario):
    if scenario == "happy":
        args = s.parse_args(["claude", "--profile", "development"])
        assert args.presets == ["pypi", "github"] and args.frontier
        assert "pypi.org" in s.allowed_hosts(args) and "api.openai.com" in s.allowed_hosts(args)
    elif scenario == "sad":
        # offline cannot be combined with destinations
        with pytest.raises(ValueError, match="offline"):
            s.validate(s.parse_args(["claude", "--no-auth", "--profile", "offline", "--profile", "development"]))
        # Git tokens need their server: a clear message instead of a generic conflict.
        with pytest.raises(ValueError, match="Git tokens need network"):
            s.validate(s.parse_args(["claude", "--no-auth", "--profile", "offline",
                                     "--git-token-env", "https://github.com=PATH"]))
    else:
        args = s.parse_args(["claude", "--profile", "egress-all", "--allow-pypi", "--profile", "development"])
        assert args.egress_all_allow and args.presets == ["pypi", "github"] and s.nameservers(args)


GIT_TOKEN = "0123456789abcdef" * 2 + "01234567"


def test_git_token_credential(scenario):
    if scenario == "happy":
        assert s.git_token_credential("http://alice@localhost:3000=GITEA") == {
            "url": "http://localhost:3000", "user": "alice", "host": "localhost", "port": 3000,
            "loopback": True, "env": "GITEA"}
        assert s.git_token_credential("https://Git.Example.org:443/=T")["url"] == "https://git.example.org"
    elif scenario == "sad":
        # Plain http leaves the host only on loopback; paths, quotes in the user and bad names are refused.
        for bad in ["http://git.example.org=T", "http://localhost:3000/owner=T", "http://localhost:3000",
                    "http://a'b@localhost:3000=T", "ssh://localhost:22=T", "http://localhost:70000=T",
                    "https://a..example.org=T", "http://localhost:3000=T=x"]:
            with pytest.raises(ValueError):
                s.git_token_credential(bad)
    else:
        credential = s.git_token_credential("https://[::1]:8443=T")
        assert credential["loopback"] and credential["port"] == 8443 and credential["user"] == "x-access-token"


def test_git_tokens(scenario, monkeypatch):
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    if scenario == "happy":
        args = s.parse_args(["claude", "--git-token-env", "http://localhost:3000=CONTRACT_GIT",
                             "--git-token-env", "https://git.example.org=CONTRACT_GIT"])
        # Loopback is reached through pasta's host forward, a remote host through its pinned name.
        assert args.allow_host_port == ["3000"] and args.allow_host == ["git.example.org"]
        assert [(c["url"], token) for c, token in s.git_tokens(args)] == [
            ("http://localhost:3000", GIT_TOKEN), ("https://git.example.org", GIT_TOKEN)]
        # The same entry from the command line and the project file counts once.
        twice = s.parse_args(["claude", "--git-token-env", "http://localhost:3000=CONTRACT_GIT",
                              "--git-token-env", "http://localhost:3000/=CONTRACT_GIT"])
        assert len(s.git_tokens(twice)) == 1
    elif scenario == "sad":
        monkeypatch.delenv("UNSET_CONTRACT_VARIABLE", raising=False)
        for options in [["http://localhost:3000=UNSET_CONTRACT_VARIABLE"],
                        ["http://localhost:3000=CONTRACT_GIT", "http://localhost:3000/=OTHER_GIT"]]:
            with pytest.raises(ValueError):
                s.git_tokens(s.parse_args(["claude", *(f"--git-token-env={o}" for o in options)]))
        with pytest.raises(ValueError, match="loopback"):
            s.git_tokens(s.parse_args(["claude", "--backend", "bwrap", "--git-token-env",
                                       "http://localhost:3000=CONTRACT_GIT"]))
    else:
        # Tokens that could add credential protocol fields are refused, and never echoed.
        for bad in [GIT_TOKEN + "\npassword=evil", GIT_TOKEN + " x", "short", "x" * 600]:
            monkeypatch.setenv("CONTRACT_GIT", bad)
            with pytest.raises(ValueError) as error:
                s.git_tokens(s.parse_args(["claude", "--git-token-env", "http://localhost:3000=CONTRACT_GIT"]))
            assert bad not in str(error.value)


def test_git_token_values(scenario):
    options = {"happy": ["--git-token-env", "http://localhost:3000=A", "--git-token-file", "https://h.example=/t"],
               "sad": [], "edge": ["--git-token-file", "http://localhost:3000=/x=y"]}[scenario]
    expected = {"happy": [("env", "http://localhost:3000=A"), ("file", "https://h.example=/t")], "sad": [],
                "edge": [("file", "http://localhost:3000=/x=y")]}[scenario]
    assert s.git_token_values(s.parse_args(["claude", *options])) == expected


def test_read_token_file(scenario, tmp_path):
    path = tmp_path / "token"
    if scenario == "happy":
        path.write_text(GIT_TOKEN + "\n")
        path.chmod(0o600)
        assert s.read_token_file(path, s.GIT_TOKEN, "--git-token-file") == GIT_TOKEN
    elif scenario == "sad":
        # Readable by others, a symlink, a directory, or not a token: refused without showing the content.
        path.write_text(GIT_TOKEN)
        path.chmod(0o644)
        with pytest.raises(ValueError, match="chmod 600"):
            s.read_token_file(path, s.GIT_TOKEN, "--git-token-file")
        (tmp_path / "link").symlink_to(path)
        with pytest.raises(ValueError, match="symlink"):
            s.read_token_file(tmp_path / "link", s.GIT_TOKEN, "--git-token-file")
        with pytest.raises(ValueError, match="not a regular file"):
            s.read_token_file(tmp_path, s.GIT_TOKEN, "--git-token-file")
        secret = tmp_path / "secret"
        # Assembled at run time, so that secret scanners do not take the fixture for a real key.
        secret.write_text("-----BEGIN " + "PRIVATE KEY-----\nabc\n")
        secret.chmod(0o600)
        with pytest.raises(ValueError) as error:
            s.read_token_file(secret, s.GIT_TOKEN, "--git-token-file")
        assert "PRIVATE" not in str(error.value)
    else:
        with pytest.raises(ValueError, match="cannot read"):
            s.read_token_file(tmp_path / "missing", s.GIT_TOKEN, "--git-token-file")
        path.write_bytes(b"x" * 5000)
        path.chmod(0o600)
        with pytest.raises(ValueError, match="does not hold a token"):
            s.read_token_file(path, s.GIT_TOKEN, "--git-token-file")


def test_token_files_in_launcher(scenario, tmp_path, monkeypatch):
    token = tmp_path / "private" / "t"
    token.parent.mkdir()
    token.write_text(GIT_TOKEN + "\n")
    token.chmod(0o600)
    work = tmp_path / "work"
    if scenario == "happy":
        gh = tmp_path / "private" / "gh"
        gh.write_text(TOKEN)
        gh.chmod(0o600)
        args = s.parse_args(["claude", "--workspace", str(work), "--git-token-file", f"http://localhost:3000={token}",
                             "--git-token-file", f"https://github.com={gh}"])
        s.validate(args)
        assert args.allow_host_port == ["3000"] and "github" in args.presets
        env = s.git_environment(args)
        assert env["SANDBOX_GIT_TOKEN_0"] == GIT_TOKEN and env["GH_TOKEN"] == TOKEN
    elif scenario == "sad":
        # A token file inside the workspace could be swapped by the agent for any host file.
        inside = work / "t"
        work.mkdir()
        inside.write_text(GIT_TOKEN)
        inside.chmod(0o600)
        with pytest.raises(ValueError, match="agent can change"):
            s.validate(s.parse_args(["claude", "--workspace", str(work),
                                     "--git-token-file", f"http://localhost:3000={inside}"]))
        monkeypatch.setenv("CONTRACT_GH", TOKEN)
        with pytest.raises(ValueError, match="given twice"):
            s.validate(s.parse_args(["claude", "--workspace", str(work),
                                     "--git-token-env", "https://github.com=CONTRACT_GH",
                                     "--git-token-file", f"https://github.com={token}"]))
    else:
        # The same URL from the command line and the project file, once as a file and once as a variable: refused.
        monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
        with pytest.raises(ValueError, match="given twice"):
            s.git_tokens(s.parse_args(["claude", "--git-token-file", f"http://localhost:3000={token}",
                                       "--git-token-env", "http://localhost:3000=CONTRACT_GIT"]))


def test_git_credential_helper(scenario):
    # Users from --git-credentials may hold any printable character: they reach the helper as a variable.
    user = {"happy": "alice", "sad": "x-access-token", "edge": "first.last@example.org'; echo pwned"}[scenario]
    helper = s.git_credential_helper("SANDBOX_GIT_USER_0", "SANDBOX_GIT_TOKEN_0")
    assert helper.startswith("!f() {") and '"$SANDBOX_GIT_USER_0"' in helper and '"$SANDBOX_GIT_TOKEN_0"' in helper
    if not shutil.which("sh"):
        pytest.skip("sh not installed")
    run = subprocess.run(["sh", "-c", helper[1:] + ' "$@"', "helper", "get" if scenario != "sad" else "store"],
                         env={"SANDBOX_GIT_TOKEN_0": GIT_TOKEN, "SANDBOX_GIT_USER_0": user},
                         capture_output=True, text=True)
    expected = "" if scenario == "sad" else f"username={user}\npassword={GIT_TOKEN}\n"
    assert run.stdout == expected


def test_read_private_file(scenario, tmp_path):
    path = tmp_path / "private"
    path.write_bytes(b"line one\nline two\n")
    path.chmod(0o600)
    if scenario == "happy":
        assert s.read_private_file(path, "--git-credentials", 100) == b"line one\nline two\n"
    elif scenario == "sad":
        path.chmod(0o640)
        with pytest.raises(ValueError, match="chmod 600") as error:
            s.read_private_file(path, "--git-credentials", 100)
        assert "line" not in str(error.value)
        (tmp_path / "link").symlink_to(path)
        with pytest.raises(ValueError, match="symlink"):
            s.read_private_file(tmp_path / "link", "--git-credentials", 100)
    else:
        # A longer file shows as LIMIT + 1 bytes, never more.
        assert s.read_private_file(path, "--git-credentials", 4) == b"line "


def test_git_server(scenario):
    if scenario == "happy":
        assert s.git_server("http://localhost:3000", "--x") == {
            "url": "http://localhost:3000", "user": None, "host": "localhost", "port": 3000, "loopback": True}
        assert s.git_server("https://alice@Git.Example.org:443/", "--x")["url"] == "https://git.example.org"
    elif scenario == "sad":
        for bad in ["http://git.example.org", "https://git.example.org/owner", "ftp://localhost", "https://a_b.org"]:
            with pytest.raises(ValueError, match="--x"):
                s.git_server(bad, "--x")
    else:
        server = s.git_server("https://[::1]:8443", "--x")
        assert server["loopback"] and server["port"] == 8443 and server["url"] == "https://[::1]:8443"


def test_loopback_reachable(scenario):
    options = {"happy": [["--podman-url", "unix:///run/podman.sock"], []],
               "sad": [["--backend", "bwrap"], ["--network", "research"], ["--offline"]],
               "edge": [["--profile", "offline"]]}[scenario]
    expected = scenario == "happy"
    assert [s.loopback_reachable(s.parse_args(["claude", *o])) for o in options] == [expected] * len(options)


GIT_LOGIN = "p@ss w'rd\"$(id);\\%"  # every character the credential store percent-encodes and a shell would expand


def credential_line(user, password, server):
    quote = __import__("urllib.parse").parse.quote
    return f"{server.split('://')[0]}://{quote(user, safe='')}:{quote(password, safe='')}@{server.split('://')[1]}"


def test_git_credentials_line(scenario):
    if scenario == "happy":
        server, user, password = s.git_credentials_line(credential_line("alice", GIT_LOGIN, "http://localhost:3000"))
        assert (server["url"], user, password) == ("http://localhost:3000", "alice", GIT_LOGIN)
        # A path, as git stores it with credential.useHttpPath, does not narrow the server.
        server, user, _ = s.git_credentials_line("https://me%40example.org:tok@GitHub.com/owner/repo.git")
        assert server["url"] == "https://github.com" and user == "me@example.org"
    elif scenario == "sad":
        cases = {"no-url": "not a login", "ssh://u:secret@host": "not a login",
                 "https://u@host": "not a login", "https://:secret@host": "empty",
                 "https://u:se%0Acret@host": "control characters", "https://u:%FFsecret@host": "UTF-8",
                 "http://u:secret@git.example.org": "unencrypted"}
        for line, reason in cases.items():
            with pytest.raises(ValueError, match=reason) as error:
                s.git_credentials_line(line)
            assert "secret" not in str(error.value)
    else:
        server, user, password = s.git_credentials_line("http://u:%3A%40%2F@[::1]:3000")
        assert server["loopback"] and server["url"] == "http://[::1]:3000" and password == ":@/"
        with pytest.raises(ValueError, match="too long"):
            s.git_credentials_line("https://u:" + "x" * 5000 + "@host")


def credentials_file(directory, *lines, newline="\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "git-credentials"
    path.write_text(newline.join(lines) + newline)
    path.chmod(0o600)
    return path


def test_git_credentials_file(scenario, tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    if scenario == "happy":
        path = credentials_file(tmp_path / "private",
                                credential_line("x-access-token", TOKEN, "https://github.com"),
                                credential_line("alice", GIT_TOKEN, "http://localhost:3000"),
                                credential_line("me@example.org", GIT_LOGIN, "https://git.example.org"))
        args = s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(path)])
        s.validate(args)
        # Each server is reached as with --git-token-*: loopback forward, GitHub preset, pinned name.
        assert args.allow_host_port == ["3000"] and "github" in args.presets and args.allow_host == ["git.example.org"]
        entries, notes = s.git_credentials_file(args)
        assert notes == [] and [(c["url"], c["user"], c["line"], password) for c, password in entries] == [
            ("https://github.com", "x-access-token", 1, TOKEN), ("http://localhost:3000", "alice", 2, GIT_TOKEN),
            ("https://git.example.org", "me@example.org", 3, GIT_LOGIN)]
        assert s.git_tokens(args) == entries
        env = s.git_environment(args)
        assert env["GH_TOKEN"] == TOKEN and env["SANDBOX_GIT_USER_2"] == "me@example.org"
        # Logins reach only their own variables, never the git configuration.
        config = {k: v for k, v in env.items() if k.startswith("GIT_CONFIG_")}
        assert not any(secret in json.dumps(config) for secret in (TOKEN, GIT_TOKEN, GIT_LOGIN, "me@example.org"))
    elif scenario == "sad":
        # Nothing usable: refused, with the line numbers and reasons but no secret.
        path = credentials_file(tmp_path / "private", "https://u:secret1@host.example:99999",
                                "http://u:secret2@git.example.org", "garbage-secret3")
        with pytest.raises(ValueError, match="no login the sandbox can use") as error:
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(path)]))
        assert "line 1" in str(error.value) and "line 3" in str(error.value) and "unencrypted" in str(error.value)
        assert "secret" not in str(error.value)
        # A loopback server needs the Podman default network.
        local = credentials_file(tmp_path / "local", credential_line("u", GIT_TOKEN, "http://localhost:3000"))
        with pytest.raises(ValueError, match="loopback"):
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--backend", "bwrap",
                                     "--git-credentials", str(local)]))
        with pytest.raises(ValueError, match="Git tokens need network"):
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--offline", "--git-credentials", str(local)]))
        # Inside a workspace the agent could swap the file; readable by others it is not private.
        inside = credentials_file(work, credential_line("u", GIT_TOKEN, "https://github.com"))
        with pytest.raises(ValueError, match="agent can change"):
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(inside)]))
        local.chmod(0o644)
        with pytest.raises(ValueError, match="chmod 600"):
            s.validate(s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(local)]))
        # The MCP server sandboxes never get Git logins.
        assert "--git-credentials" in load_module("sandbox_mcp_contracts", "sandbox/mcp.py").forbidden()
    else:
        # CRLF, blank and malformed lines; the first line per server wins; a token option wins over the file;
        # notes name lines only, and the launcher prints them.
        monkeypatch.setenv("CONTRACT_GH", TOKEN)
        path = credentials_file(tmp_path / "private", "", "broken-secret",
                                credential_line("first", GIT_TOKEN, "http://localhost:3000"),
                                credential_line("second", "secret-second", "http://localhost:3000/other/repo"),
                                credential_line("x", "secret-gh", "https://github.com"), newline="\r\n")
        args = s.parse_args(["claude", "--workspace", str(work), "--git-credentials", str(path),
                             "--git-token-env", "https://github.com=CONTRACT_GH"])
        entries, notes = s.git_credentials_file(args)
        assert [(c["user"], password) for c, password in entries] == [("first", GIT_TOKEN)]
        assert [note.split(" skipped: ")[0].rsplit(" ", 1)[1] for note in notes] == ["2", "4", "5"]
        assert "earlier line" in notes[1] and "--git-token-*" in notes[2]
        assert not any("secret" in note or "second" in note for note in notes)
        assert [(c["url"], token) for c, token in s.git_tokens(args)] == [
            ("https://github.com", TOKEN), ("http://localhost:3000", GIT_TOKEN)]
        monkeypatch.setattr(s, "load_project_config", lambda a: (None, None))
        monkeypatch.setattr(s, "check_login", lambda a: None)
        assert s.launch(["claude", "--workspace", str(work), "--no-auth", "--dry-run", "--git-credentials", str(path),
                         "--git-token-env", "https://github.com=CONTRACT_GH"]) in (0, None)
        out, err = capsys.readouterr()
        plan = json.loads(out)
        assert plan["git_credentials"] == [
            {"url": "https://github.com", "user": "x-access-token", "env": "CONTRACT_GH"},
            {"url": "http://localhost:3000", "user": "first", "credentials": str(path), "line": 3}]
        assert plan["host_ports"] == [3000] and f"{path} line 4 skipped" in err
        assert not any(secret in out + err for secret in (TOKEN, GIT_TOKEN, "secret-second", "secret-gh", "broken-secret"))


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_git_credentials_real_git(tmp_path):
    """Real git against a Git server on loopback that demands the login (HTTP Basic, git-http-backend):
    with only the launcher's GIT_CONFIG_* variables and a planted helper in HOME, clone and push work
    with the right login and fail with a wrong one."""
    import base64 as b64
    import http.server
    import threading
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed")
    backend = Path(subprocess.run([git, "--exec-path"], capture_output=True, text=True).stdout.strip()) / "git-http-backend"
    if not backend.exists():
        pytest.skip("git-http-backend not installed")
    root = tmp_path / "srv"
    subprocess.run([git, "init", "--bare", "-q", "-b", "main", str(root / "repo.git")], check=True)
    expected = "Basic " + b64.b64encode(f"me@example.org:{GIT_LOGIN}".encode()).decode()
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_backend(self):
            seen.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            env = {"GIT_PROJECT_ROOT": str(root), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path,
                   "QUERY_STRING": query, "REQUEST_METHOD": self.command, "REMOTE_USER": "me",
                   "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                   "PATH": os.environ["PATH"]}
            if self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
            out = subprocess.run([str(backend)], input=body, env=env, capture_output=True).stdout
            head, _, payload = out.partition(b"\r\n\r\n")
            status = 200
            headers = []
            for line in head.decode().split("\r\n"):
                name, _, value = line.partition(": ")
                if name.lower() == "status":
                    status = int(value.split()[0])
                elif name:
                    headers.append((name, value))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = handle_backend

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        def attempt(password, name):
            path = credentials_file(tmp_path / name, credential_line("me@example.org", password,
                                                                     f"http://localhost:{port}"))
            args = s.parse_args(["claude", "--workspace", str(tmp_path / "work"), "--git-credentials", str(path)])
            s.validate(args)
            assert args.allow_host_port == [str(port)]
            home = tmp_path / f"home-{name}"
            home.mkdir()
            # A helper the agent planted in the sandbox HOME is dropped for this server.
            (home / ".gitconfig").write_text("[credential]\n\thelper = !echo username=evil; echo password=planted #\n")
            env = {"HOME": str(home), "PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0",
                   "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@example.org", "GIT_COMMITTER_NAME": "A",
                   "GIT_COMMITTER_EMAIL": "a@example.org", **s.git_environment(args)}
            clone = home / "clone"
            url = f"http://localhost:{port}/repo.git"
            result = subprocess.run([git, "clone", "-q", url, str(clone)], env=env, capture_output=True, text=True)
            if result.returncode:
                return result
            (clone / "file.txt").write_text(name)
            subprocess.run([git, "-C", str(clone), "add", "file.txt"], env=env, check=True)
            subprocess.run([git, "-C", str(clone), "commit", "-qm", name], env=env, check=True)
            return subprocess.run([git, "-C", str(clone), "push", "-q", "origin", "HEAD:main"],
                                  env=env, capture_output=True, text=True)

        good = attempt(GIT_LOGIN, "good")
        assert good.returncode == 0, good.stderr
        log = subprocess.run([git, "--git-dir", str(root / "repo.git"), "log", "--format=%s", "main"],
                             capture_output=True, text=True).stdout
        assert log.split() == ["good"]
        assert expected in seen and not any("planted" in b64.b64decode(h.split()[1]).decode()
                                            for h in seen if h)
        bad = attempt("wrong", "bad")
        assert bad.returncode != 0 and "Authentication failed" in bad.stderr
    finally:
        server.shutdown()


def test_host_git_identity(scenario, monkeypatch):
    answers = {"happy": {"user.name": "Ada Lovelace\n", "user.email": "ada@example.org\n"},
               "sad": {"user.name": "Ada\n"},
               "edge": {"user.name": "Ada\x1b]0;pwn\x07\n", "user.email": "ada@example.org\n"}}[scenario]

    def run(cmd, **kwargs):
        # Global scope only, from HOME, without GIT_* overrides: never the workspace's .git/config.
        assert cmd[:4] == ["git", "config", "--global", "--get"]
        assert kwargs["cwd"] == Path.home() and not any(k.startswith("GIT_") for k in kwargs["env"])
        value = answers.get(cmd[4])
        return Mock(returncode=0 if value else 1, stdout=value or "")
    monkeypatch.setattr(s.subprocess, "run", run)
    if scenario == "happy":
        assert s.host_git_identity() == ("Ada Lovelace", "ada@example.org")
    else:
        # Missing values and control characters (terminal escapes) are refused.
        with pytest.raises(ValueError, match="user.email" if scenario == "sad" else "user.name"):
            s.host_git_identity()


def test_git_environment(scenario, monkeypatch, tmp_path):
    monkeypatch.setenv("CONTRACT_GH", TOKEN)
    monkeypatch.setattr(s, "host_git_identity", lambda: ("Ada", "ada@example.org"))
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    options = {"happy": ["--git-identity", "--git-token-env", "https://github.com=CONTRACT_GH"], "sad": [],
               "edge": ["--git-token-env", "https://github.com=CONTRACT_GH",
                        "--git-token-env", "http://alice@localhost:3000=CONTRACT_GIT"]}[scenario]
    env = s.git_environment(s.parse_args(["claude", *options]))
    if scenario == "sad":
        # Only the privacy hooks, unless they are switched off.
        assert env == {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                       "GIT_CONFIG_VALUE_0": s.PRIVACY_HOOKS}
        assert s.git_environment(s.parse_args(["claude", "--privacy-check", "off"])) == {}
        return
    count = int(env["GIT_CONFIG_COUNT"])
    assert (env[f"GIT_CONFIG_KEY_{count - 1}"], env[f"GIT_CONFIG_VALUE_{count - 1}"]) == \
        ("core.hooksPath", s.PRIVACY_HOOKS)
    if scenario == "happy":
        assert env["GIT_CONFIG_KEY_0"] == "user.name" and env["GIT_CONFIG_VALUE_0"] == "Ada"
        assert env["GIT_CONFIG_KEY_1"] == "user.email" and env["GIT_CONFIG_VALUE_1"] == "ada@example.org"
    assert env["GH_TOKEN"] == TOKEN == env["SANDBOX_GIT_TOKEN_0"]
    assert TOKEN not in json.dumps({k: v for k, v in env.items() if k not in ("GH_TOKEN", "SANDBOX_GIT_TOKEN_0")})
    if not shutil.which("git"):
        pytest.skip("git not installed")
    # Real git: a helper the agent planted in ~/.gitconfig is dropped for github.com,
    # the token answers only for https://github.com and nothing else.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[credential]\n\thelper = !echo password=planted #\n")
    run_env = {"HOME": str(home), "PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0", **env}

    def fill(url):
        return subprocess.run(["git", "credential", "fill"], input=f"url={url}\n\n", env=run_env,
                              capture_output=True, text=True).stdout
    answer = fill("https://github.com/owner/repo.git")
    assert f"password={TOKEN}" in answer and "username=x-access-token" in answer and "planted" not in answer
    for other in ["https://github.com.evil.example/x.git", "http://github.com/x.git", "https://gitlab.com/x.git"]:
        assert TOKEN not in fill(other)
    local = fill("http://localhost:3000/alice/sandbox-ai.git")
    if scenario == "edge":
        assert env["SANDBOX_GIT_TOKEN_1"] == GIT_TOKEN and GIT_TOKEN not in json.dumps(
            {k: v for k, v in env.items() if k != "SANDBOX_GIT_TOKEN_1"})
        assert f"password={GIT_TOKEN}" in local and "username=alice" in local and "planted" not in local
        for other in ["http://localhost:3001/x.git", "http://127.0.0.1:3000/x.git", "https://localhost:3000/x.git"]:
            assert GIT_TOKEN not in fill(other)
    else:
        assert GIT_TOKEN not in local


def test_config_store(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert s.config_store() == Path.home() / ".config/sandbox-ai/trusted.json"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ("x" if scenario == "happy" else "with space")))
        assert s.config_store().parent.parent == tmp_path / ("x" if scenario == "happy" else "with space")


def test_trusted_configs(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = tmp_path / "sandbox-ai/trusted.json"
    if scenario != "sad":
        store.parent.mkdir(parents=True)
        store.write_text('{"/p/.sandbox-ai.json": "abc"}' if scenario == "happy" else "not json")
    assert s.trusted_configs() == ({"/p/.sandbox-ai.json": "abc"} if scenario == "happy" else {})


def test_trust_config(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / ".sandbox-ai.json"
    if scenario == "sad":
        with pytest.raises(OSError):
            s.trust_config(path)
        return
    path.write_text("{}")
    s.trust_config(path)
    if scenario == "edge":
        path.write_text('{"changed": 1}')
        s.trust_config(path)
    import hashlib
    assert s.trusted_configs() == {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}
    assert (tmp_path / "cfg/sandbox-ai").stat().st_mode & 0o777 == 0o700


def test_load_project_config(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path.with_name(tmp_path.name + "-cfg")))  # outside the workspace
    path = tmp_path / ".sandbox-ai.json"
    args = s.parse_args(["codex", "--workspace", str(tmp_path)])
    if scenario == "sad":
        assert s.load_project_config(args) == (None, None)
        with pytest.raises(ValueError, match="not found"):
            s.load_project_config(s.parse_args(["codex", "--config", str(tmp_path / "missing.json")]))
        return
    document = {"version": 1, "agent": "codex", "options": ["--dns"]}
    path.write_text(json.dumps(document))
    s.trust_config(path)
    if scenario == "edge":
        assert s.load_project_config(s.parse_args(["codex", "--workspace", str(tmp_path), "--no-config"])) == (None, None)
    else:
        assert s.load_project_config(args) == (path, document)


def test_merge_config(scenario):
    document = {"options": ["--dns", "--log-dir", "/l"], "agent_args": ["run"], "workspaces": ["/extra"]}
    argv = {"happy": ["codex"], "sad": ["codex", "--workspace", "/main", "--", "own"],
            "edge": ["codex", "--workspace=/main,/extra", "--log-dir", "/mine"]}[scenario]
    args = s.parse_args(argv)
    merged = s.merge_config(argv, args, document)
    expected_workspaces = {"happy": f"{os.getcwd()},/extra", "sad": "/main,/extra", "edge": "/main,/extra"}[scenario]
    assert merged[merged.index("--workspace") + 1] == expected_workspaces
    assert merged[merged.index("--") + 1:] == (["own"] if scenario == "sad" else ["run"])
    final = s.parse_args(merged)
    assert final.log_dir == Path("/mine" if scenario == "edge" else "/l")
    assert final.workspaces[0] == ("/main" if scenario != "happy" else os.getcwd())


def test_claude_default_settings(scenario, tmp_path):
    home = tmp_path / "home"
    settings = home / ".claude/settings.json"
    if scenario == "sad":
        settings.parent.mkdir(parents=True)
        settings.write_text("{broken")
        s.claude_default_settings(home)
        assert settings.read_text() == "{broken"
        return
    if scenario == "edge":
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "sonnet", "theme": "dark",
                                        "modelSettings": {"claude-opus-5-5": {"effortLevel": "max"}}}))
    s.claude_default_settings(home)
    document = json.loads(settings.read_text())
    if scenario == "happy":
        assert document == {"model": "claude-opus-5-5", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
        assert settings.stat().st_mode & 0o777 == 0o600
    else:
        # Existing choices win; only missing keys are added.
        assert document["model"] == "sonnet" and document["theme"] == "dark"
        assert document["modelSettings"]["claude-opus-5-5"]["effortLevel"] == "max"


def test_update_json_defaults(scenario, tmp_path):
    home = tmp_path / "home"
    target = home / "sub/state.json"
    target.parent.mkdir(parents=True)

    def add_k(document):
        changed = "k" not in document
        document.setdefault("k", 1)
        return changed

    if scenario == "sad":
        # A planted symlink is refused, never followed or replaced.
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        target.symlink_to(outside)
        with pytest.raises(ValueError):
            s.update_json_defaults(home, "sub/state.json", add_k)
        assert outside.read_text() == "{}" and target.is_symlink()
        return
    if scenario == "edge":
        target.write_text("[1, 2]")  # Valid JSON but not an object: untouched, with a reason.
        assert "JSON object" in s.update_json_defaults(home, "sub/state.json", add_k)
        assert target.read_text() == "[1, 2]"
        big = json.dumps({"pad": "x" * 64})
        target.write_text(big)  # Over the given limit: refused, file kept, reason names the limit.
        assert "16 bytes" in s.update_json_defaults(home, "sub/state.json", add_k, limit=16)
        assert target.read_text() == big
        # Nothing to add: the bytes and the inode stay, so concurrent writers keep their data.
        original = '{"k": 2,   "x": "\\u00e4"}'
        target.write_text(original)
        inode = target.stat().st_ino
        assert s.update_json_defaults(home, "sub/state.json", add_k) is None
        assert target.read_text() == original and target.stat().st_ino == inode
        return
    assert s.update_json_defaults(home, "sub/state.json", add_k) is None
    assert json.loads(target.read_text()) == {"k": 1}
    assert target.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]
    # Non-ASCII text is kept as written, not turned into escapes.
    target.write_text(json.dumps({"projects": {"/home/jörg": {}}}, ensure_ascii=False), encoding="utf-8")
    assert s.update_json_defaults(home, "sub/state.json", add_k) is None
    assert "/home/jörg" in target.read_text(encoding="utf-8")


def test_claude_default_state(scenario, tmp_path):
    home = tmp_path / "home"
    state = home / ".claude.json"
    credentials = home / ".claude/.credentials.json"
    credentials.parent.mkdir(parents=True)
    oauth = {"accessToken": "a", "refreshToken": "r", "expiresAt": int(time.time() * 1000) + 3600_000}
    if scenario == "sad":
        # No usable login: leave onboarding (and its login step) to Claude.
        credentials.write_text("{}")
        s.claude_default_state(home)
        assert not state.exists()
        # A planted ~/.claude symlink is refused before anything is read through it.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
        shutil.rmtree(home / ".claude")
        (home / ".claude").symlink_to(outside)
        with pytest.raises(ValueError):
            s.claude_default_state(home)
        assert not state.exists()
        return
    if scenario == "edge":
        # A refresh token past its expiry cannot log in: keep onboarding's login step.
        expired = {**oauth, "refreshTokenExpiresAt": int(time.time() * 1000) - 1000}
        credentials.write_text(json.dumps({"claudeAiOauth": expired}))
        s.claude_default_state(home)
        assert not state.exists()
    credentials.write_text(json.dumps({"claudeAiOauth": oauth}))
    if scenario == "edge":
        # Real host files grow past CONFIG_LIMIT through project history.
        projects = {f"/p{i}": {"history": ["x" * 100] * 10} for i in range(s.CONFIG_LIMIT // 1000 + 1)}
        state.write_text(json.dumps({"theme": "light", "hasCompletedOnboarding": False,
                                     "oauthAccount": {"emailAddress": "x"}, "projects": projects}))
        assert state.stat().st_size > s.CONFIG_LIMIT
    s.claude_default_state(home)
    document = json.loads(state.read_text())
    if scenario == "happy":
        assert document == {"hasCompletedOnboarding": True, "theme": "dark"}
        assert state.stat().st_mode & 0o777 == 0o600
    else:
        assert document["hasCompletedOnboarding"] is True and document["theme"] == "light"
        assert document["oauthAccount"] == {"emailAddress": "x"} and document["projects"] == projects


def test_printable(scenario):
    text = {"happy": "plain ä text\n\tok", "sad": "\x1b]52;c;cGF5\x07\r", "edge": "a‮b\x9bc"}[scenario]
    expected = {"happy": text, "sad": "\\x1b]52;c;cGF5\\x07\\x0d", "edge": "a\\u202eb\\x9bc"}[scenario]
    assert s.printable(text) == expected


def exposure_args(tmp_path, *options):
    for name in ("work", "rw", "ro", "state", "outside"):
        (tmp_path / name).mkdir(exist_ok=True)
    return s.parse_args(["opencode", "--workspace", str(tmp_path / "work"), "--mount", f"{tmp_path / 'rw'}:/mnt/rw:rw",
                         "--mount", f"{tmp_path / 'ro'}:/mnt/ro:ro", "--state-dir", str(tmp_path / "state"), *options])


def test_exposed_trees(scenario, tmp_path):
    options = {"happy": [], "sad": [], "edge": []}[scenario]
    args = exposure_args(tmp_path, *options)
    if scenario == "sad":
        args.state_dir = None
    trees = s.exposed_trees(args)
    expected = [tmp_path / "work", tmp_path / "rw"] + ([] if scenario == "sad" else [tmp_path / "state"])
    assert trees == expected  # read-only mounts are not writable by the agent
    assert tmp_path / "ro" not in trees


def test_check_host_source(scenario, tmp_path):
    args = exposure_args(tmp_path)
    (tmp_path / "outside/config.json").write_text("{}")
    if scenario == "happy":
        s.check_host_source(args, tmp_path / "outside/config.json", "--local-config")
        return
    if scenario == "sad":
        # Inside the workspace, the rw mount or the persistent HOME: refused even before it exists.
        for path in [tmp_path / "work/cfg.json", tmp_path / "rw/x", tmp_path / "state/.ssh/id",
                     tmp_path / "outside/../work/cfg.json"]:
            with pytest.raises(ValueError, match="agent can change"):
                s.check_host_source(args, path, "--copy")
        return
    # A host symlink outside that leads into the workspace is refused by its target.
    (tmp_path / "outside/link.json").symlink_to(tmp_path / "work/cfg.json")
    with pytest.raises(ValueError, match="agent can change"):
        s.check_host_source(args, tmp_path / "outside/link.json", "--auth-file")


def test_check_host_sources(scenario, tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/auth.json").write_text("{}")
    if scenario == "happy":
        s.validate(exposure_args(tmp_path, "--auth-file", str(tmp_path / "outside/auth.json")))
        return
    if scenario == "sad":
        (tmp_path / "work").mkdir()
        (tmp_path / "work/data").mkdir()
        cases = [["--local-config", str(tmp_path / "work/x.json")],
                 ["--copy", f"{tmp_path / 'work/x.json'}:.config/x"],
                 ["--mount", f"{tmp_path / 'work/data'}:/mnt/data:ro"],
                 ["--mount", f"{tmp_path / 'rw'}:/mnt/again:ro"]]
        (tmp_path / "work/x.json").write_text("{}")
        for options in cases:
            with pytest.raises(ValueError, match="agent can change"):
                s.validate(exposure_args(tmp_path, *options))
        return
    # The persistent HOME must not lie in a workspace, where it could be committed or swapped.
    (tmp_path / "work").mkdir()
    (tmp_path / "work/.state").mkdir(mode=0o700)
    with pytest.raises(ValueError, match="agent can change"):
        s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "work"),
                                 "--state-dir", str(tmp_path / "work/.state")]))


def test_check_tiocsti(scenario, tmp_path, monkeypatch):
    sysctl = tmp_path / "legacy_tiocsti"
    monkeypatch.setattr(s, "TIOCSTI_SYSCTL", sysctl)
    if scenario == "happy":
        sysctl.write_text("0\n")
        s.check_tiocsti()
        return
    if scenario == "sad":
        sysctl.write_text("1\n")
    # edge: no sysctl at all means a kernel before 6.2, which always allows TIOCSTI.
    with pytest.raises(ValueError, match="TIOCSTI"):
        s.check_tiocsti()


def test_claude_state_subset(scenario, tmp_path):
    source = tmp_path / ".claude.json"
    if scenario == "sad":
        source.write_text("[1]")
        assert s.claude_state_subset(source) is None
        source.write_text("{broken")
        assert s.claude_state_subset(source) is None
        return
    document = {"oauthAccount": {"emailAddress": "a@example.org"}, "userID": "u", "theme": "dark",
                "mcpServers": {"db": {"env": {"TOKEN": "secret"}}}, "projects": {"/home/me/x": {}}}
    source.write_text(json.dumps(document))
    if scenario == "edge":
        # A host file managed as a symlink (dotfiles) is followed on the host side.
        real = tmp_path / "dotfiles.json"
        source.rename(real)
        source.symlink_to(real)
    subset = json.loads(s.claude_state_subset(source))
    assert subset == {"oauthAccount": {"emailAddress": "a@example.org"}, "userID": "u", "theme": "dark"}


def test_copy_auth_file(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"oauthAccount": {"x": 1}, "mcpServers": {"a": {}}}))
    if scenario == "happy":
        s.copy_auth_file(source, home, ".codex/auth.json")
        assert (home / ".codex/auth.json").read_bytes() == source.read_bytes()
    elif scenario == "sad":
        source.write_text("{broken")
        assert s.copy_auth_file(source, home, ".claude.json") is None
        assert not (home / ".claude.json").exists()
    else:
        s.copy_auth_file(source, home, ".claude.json")
        assert json.loads((home / ".claude.json").read_text()) == {"oauthAccount": {"x": 1}}
        assert (home / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_replace_private(scenario, tmp_path):
    home = tmp_path / "home"
    target = home / ".config/app/config.json"
    if scenario == "sad":
        target.parent.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.write_text("keep")
        target.symlink_to(outside)
        with pytest.raises(ValueError):
            s.replace_private(b"new", home, ".config/app/config.json")
        assert outside.read_text() == "keep"
        return
    home.mkdir()
    assert s.replace_private(b"one", home, ".config/app/config.json") is True
    assert target.read_bytes() == b"one" and target.stat().st_mode & 0o777 == 0o600
    if scenario == "edge":
        inode = target.stat().st_ino
        assert s.replace_private(b"one", home, ".config/app/config.json") is False
        assert target.stat().st_ino == inode
        target.unlink()
        os.mkfifo(target)  # A planted FIFO is replaced without being opened.
        assert s.replace_private(b"two", home, ".config/app/config.json") is True
        assert target.read_bytes() == b"two"


def test_timestamp_ms(scenario):
    values = {"happy": [0, 1, time.time() * 1000, 2 ** 53 - 1],
              "sad": [-1, 2 ** 53, 10 ** 400, float("inf"), float("nan"), True, "1", None],
              "edge": [0.5, 1e15]}[scenario]
    assert all(bool(s.timestamp_ms(v)) == (scenario != "sad") for v in values)


def test_credential_digest(scenario, tmp_path):
    path = tmp_path / "credentials.json"
    if scenario == "sad":
        assert s.credential_digest(path) is None
        return
    path.write_text("a" if scenario == "happy" else "")
    digest = s.credential_digest(path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("b")
    assert s.credential_digest(path) != digest


def test_claude_writeback_state(scenario, tmp_path):
    host, sandbox = tmp_path / "host.json", tmp_path / "home/.claude/.credentials.json"
    soon = (time.time() + 3600) * 1000
    if scenario != "sad":
        claude_credentials(host, soon)
        claude_credentials(sandbox, soon)
    state = s.claude_writeback_state((host, sandbox))
    if scenario == "sad":
        assert state == {"host": None, "host_login": False, "tokens": None}
        return
    assert state["host"] == s.oauth_token_digest(s.claude_oauth(host)) and state["host_login"] is True
    assert state["tokens"] == {"accessToken": "access-fixture", "refreshToken": "refresh-fixture", "expiresAt": soon}
    if scenario == "edge":
        assert "access-fixture" not in state["host"]


def test_claude_writeback(scenario, tmp_path):
    home = tmp_path / "home"
    host, sandbox = tmp_path / "host/.claude/.credentials.json", home / ".claude/.credentials.json"
    soon, later = (time.time() + 3600) * 1000, (time.time() + 7200) * 1000
    claude_credentials(sandbox, soon)
    if scenario == "sad":
        # No host login before: the sandbox never creates one; the agent's tokens stay inside.
        before = s.claude_writeback_state((host, sandbox))
        claude_credentials(sandbox, later, refresh="forged")
        assert s.claude_writeback(home, (host, sandbox), before) is None
        assert not host.exists()
        # Review finding: a host file without a login (e.g. only MCP OAuth tokens) must not receive one either.
        host.parent.mkdir(parents=True)
        host.write_text(json.dumps({"mcpOAuth": {"srv": {"accessToken": "m"}}}))
        before = s.claude_writeback_state((host, sandbox))
        claude_credentials(sandbox, later, refresh="forged-again")
        assert s.claude_writeback(home, (host, sandbox), before) is None
        assert "claudeAiOauth" not in json.loads(host.read_text())
        return
    claude_credentials(host, soon)
    before = s.claude_writeback_state((host, sandbox))
    if scenario == "happy":
        assert s.claude_writeback(home, (host, sandbox), before) is None  # nothing refreshed, nothing written
    # Regression: Claude on the host rewrote its file for another reason (an MCP OAuth token) while the
    # sandbox refreshed the login. The login itself is unchanged, so the rotated tokens must still go back;
    # dropping them left the host with a used-up refresh token and the next start with "Login expired".
    claude_credentials(sandbox, later, refresh="rotated")
    document = json.loads(host.read_text())
    document["mcpOAuth"] = {"srv": {"accessToken": "m"}}
    host.write_text(json.dumps(document))
    if scenario == "happy":
        assert "written back" in s.claude_writeback(home, (host, sandbox), before)
        written = json.loads(host.read_text())
        assert written["claudeAiOauth"]["refreshToken"] == "rotated" and written["mcpOAuth"]["srv"]["accessToken"] == "m"
        return
    # edge: the user logged in again on the host during the run; the host's login wins.
    claude_credentials(sandbox, later, refresh="rotated")
    claude_credentials(host, soon, refresh="host-relogin")
    assert "changed during the run; kept that one" in s.claude_writeback(home, (host, sandbox), before)
    assert json.loads(host.read_text())["claudeAiOauth"]["refreshToken"] == "host-relogin"
    # A symlinked ~/.claude in the sandbox is refused before anything is read through it.
    target = tmp_path / "elsewhere"
    target.mkdir()
    shutil.rmtree(home / ".claude")
    (home / ".claude").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        s.claude_writeback(home, (host, sandbox), before)


def test_load_mcp_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_mcp_module()
    else:
        module = s.load_mcp_module()
        assert callable(module.ensure_running) and "demo" in module.BUILTIN
        if scenario == "edge":
            assert s.load_mcp_module() is not module


def test_mcp_socket_dir(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert s.mcp_socket_dir("x") == Path.home() / ".local/state/sandbox-ai/mcp/x/sock"
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ("s" if scenario == "happy" else "with space")))
        name = "demo" if scenario == "happy" else "a_b-1"
        assert s.mcp_socket_dir(name) == tmp_path / ("s" if scenario == "happy" else "with space") / f"sandbox-ai/mcp/{name}/sock"


def test_mcp_bridge_args(scenario):
    name = {"happy": "demo", "sad": "x", "edge": "a_b-1"}[scenario]
    assert s.mcp_bridge_args(name) == ["-I", "/sandbox/runtime.py", "--internal-mcp-bridge",
                                       f"/sandbox/mcp/{name}/server.sock"]


def test_mcp_agent_args(scenario):
    if scenario == "sad":
        assert s.mcp_agent_args(s.parse_args(["claude"])) == []
        assert s.mcp_agent_args(s.parse_args(["opencode", "--mcp", "demo"])) == []
    elif scenario == "happy":
        args = s.mcp_agent_args(s.parse_args(["claude", "--mcp", "demo"]))
        assert args[0] == "--mcp-config" and "demo" in json.loads(args[1])["mcpServers"]
    else:
        args = s.mcp_agent_args(s.parse_args(["codex", "--mcp", "a", "--mcp", "a", "--mcp", "b"]))
        assert args.count("-c") == 4 and 'mcp_servers.b.command="python3"' in args


def test_mcp_serve(scenario, tmp_path):
    import threading, time as clock
    sock = tmp_path / "s.sock"
    if scenario == "sad":
        with pytest.raises(OSError):
            s.mcp_serve(str(tmp_path / "missing-dir" / "s.sock"), ["cat"])
        return
    if scenario == "edge":
        sock.write_text("stale")  # A stale socket file is replaced.
    command = ["cat"] if scenario == "happy" else ["/nonexistent/server"]
    threading.Thread(target=s.mcp_serve, args=(str(sock), command), kwargs={"cwd": str(tmp_path)}, daemon=True).start()
    for _ in range(100):
        if sock.exists() and sock.is_socket():
            break
        clock.sleep(0.05)
    def exchange():
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(10)
        client.connect(str(sock))
        client.sendall(b"echo\n")
        client.shutdown(socket.SHUT_WR)
        received = b""
        try:
            while chunk := client.recv(100):
                received += chunk
        except ConnectionResetError:
            pass  # a failed server closes with unread input
        return received
    assert exchange() == (b"echo\n" if scenario == "happy" else b"")
    # One failing or finished instance never takes the listener down.
    assert exchange() == (b"echo\n" if scenario == "happy" else b"")


def test_mcp_bridge(scenario, tmp_path, monkeypatch):
    if scenario == "sad":
        with pytest.raises(RuntimeError, match="unavailable"):
            s.mcp_bridge(str(tmp_path / "absent.sock"))
        return
    import threading
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(tmp_path / "b.sock"))
    server.listen(1)
    payload = b"x" * (300000 if scenario == "edge" else 5)
    def answer():
        connection, _ = server.accept()
        with connection:
            while connection.recv(65536):
                pass
            connection.sendall(payload)
    threading.Thread(target=answer, daemon=True).start()
    read_r, read_w = os.pipe()
    write_r, write_w = os.pipe()
    os.write(read_w, b"request")
    os.close(read_w)
    saved = os.dup(0), os.dup(1)
    os.dup2(read_r, 0)
    os.dup2(write_w, 1)
    received = []
    reader = threading.Thread(target=lambda: received.append(b"".join(iter(lambda: os.read(write_r, 65536), b""))))
    reader.start()
    try:
        assert s.mcp_bridge(str(tmp_path / "b.sock")) == 0
    finally:
        os.dup2(saved[0], 0)
        os.dup2(saved[1], 1)
        os.close(write_w)
        reader.join(10)
    assert received == [payload]


def test_config_key(scenario, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    if scenario == "happy":
        assert s.config_key(real / ".sandbox-ai.json") == real / ".sandbox-ai.json"
    elif scenario == "sad":
        # The file name itself is never followed, even when it is a link.
        (real / "target.json").write_text("{}")
        (real / ".sandbox-ai.json").symlink_to(real / "target.json")
        assert s.config_key(real / ".sandbox-ai.json").name == ".sandbox-ai.json"
    else:
        (tmp_path / "alias").symlink_to(real)
        assert s.config_key(tmp_path / "alias" / ".sandbox-ai.json") == real / ".sandbox-ai.json"


def test_read_config_bytes(scenario, tmp_path):
    path = tmp_path / "c.json"
    if scenario == "happy":
        path.write_bytes(b'{"a": 1}')
        assert s.read_config_bytes(path) == b'{"a": 1}'
    elif scenario == "sad":
        (tmp_path / "t.json").write_text("{}")
        path.symlink_to(tmp_path / "t.json")
        with pytest.raises(ValueError, match="symlink"):
            s.read_config_bytes(path)
        os.mkfifo(tmp_path / "fifo")
        with pytest.raises(ValueError, match="regular file"):
            s.read_config_bytes(tmp_path / "fifo")
    else:
        path.write_bytes(b"x" * (s.CONFIG_LIMIT + 1))
        with pytest.raises(ValueError, match="larger"):
            s.read_config_bytes(path)


def test_attach_frame(scenario):
    import struct as st
    kind, payload = {"happy": (b"d", b"ls\r"), "sad": (b"r", st.pack("!HH", 40, 120)), "edge": (b"d", b"")}[scenario]
    frame = s.attach_frame(kind, payload)
    assert frame[:1] == kind and st.unpack("!I", frame[1:5])[0] == len(payload) and frame[5:] == payload


def test_read_frames(scenario):
    import struct as st
    data = s.attach_frame(b"d", b"abc") + s.attach_frame(b"r", st.pack("!HH", 30, 100))
    if scenario == "happy":
        frames, rest = s.read_frames(data)
        assert frames == [(b"d", b"abc"), (b"r", st.pack("!HH", 30, 100))] and rest == b""
    elif scenario == "sad":
        frames, rest = s.read_frames(data[:10])  # an incomplete second header stays buffered
        assert frames == [(b"d", b"abc")] and rest == data[8:10]
    else:
        frames, rest = s.read_frames(data[:3])
        assert frames == [] and rest == data[:3]


def relay_round_trip(tmp_path, child_script, frames):
    import threading, time as clock
    sock = tmp_path / "attach.sock"
    command = [sys.executable, "-c", child_script]
    threading.Thread(target=s.attach_relay, args=(str(sock), command), daemon=True).start()
    for _ in range(200):
        if sock.exists():
            break
        clock.sleep(0.02)
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(15)
    client.connect(str(sock))
    for frame in frames:
        client.sendall(frame)
    received = b""
    try:
        while chunk := client.recv(4096):
            received += chunk
    except ConnectionResetError:
        pass
    return received


def test_attach_relay(scenario, tmp_path):
    import struct as st
    script = ("import os,sys; r,c=os.get_terminal_size(0).lines,os.get_terminal_size(0).columns; "
              "print('SIZE',r,c,flush=True); line=sys.stdin.readline(); print('GOT',line.strip(),flush=True)")
    if scenario == "sad":
        out = relay_round_trip(tmp_path, "import sys; sys.exit(3)", [])
        assert out == b""  # the attach client ended: connection closed, relay keeps serving
        return
    size = (30, 100) if scenario == "happy" else (12, 345)
    out = relay_round_trip(tmp_path, script, [s.attach_frame(b"r", st.pack("!HH", *size)),
                                              s.attach_frame(b"d", b"hello\r")])
    assert f"SIZE {size[0]} {size[1]}".encode() in out and b"GOT hello" in out


def test_attach_client(scenario, tmp_path):
    import threading
    sock = tmp_path / "c.sock"
    if scenario == "sad":
        with pytest.raises(RuntimeError, match="not reachable"):
            s.attach_client(str(sock), os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY))
        # The sandbox swaps attach.sock for a symlink to another host socket of the user:
        # the client refuses instead of sending keystrokes there.
        victim = socket.socket(socket.AF_UNIX)
        victim.bind(str(tmp_path / "victim.sock"))
        victim.listen(1)
        victim.settimeout(0.5)
        sock.symlink_to(tmp_path / "victim.sock")
        with pytest.raises(RuntimeError, match="not reachable"):
            s.attach_client(str(sock), os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY))
        with pytest.raises(OSError):
            victim.accept()
        sock.unlink()
        (tmp_path / "plain").write_text("")  # not a socket at all
        with pytest.raises(RuntimeError, match="not a socket"):
            s.attach_client(str(tmp_path / "plain"), os.open(os.devnull, os.O_RDONLY), os.open(os.devnull, os.O_WRONLY))
        return
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(sock))
    server.listen(1)
    got = []
    # A hostile relay sends a tmux-style exec request: the client only ever writes bytes out.
    hostile = b"\x00\x00\x00\x0cMSG_EXEC rm -rf ~" if scenario == "edge" else b"screen output"
    def serve():
        connection, _ = server.accept()
        with connection:
            data = b""
            while b"typed" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
            got.append(data)
            connection.sendall(hostile)
    threading.Thread(target=serve, daemon=True).start()
    stdin_r, stdin_w = os.pipe()
    os.write(stdin_w, b"typed")
    os.close(stdin_w)
    out_r, out_w = os.pipe()
    assert s.attach_client(str(sock), stdin_r, out_w) == 0
    os.close(out_w)
    assert os.read(out_r, 4096) == hostile
    frames, _ = s.read_frames(got[0])
    assert (b"d", b"typed") in frames


def test_check_home_path(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if scenario == "happy":
        assert s.check_home_path(home, home / ".claude/x.json") == home / ".claude/x.json"
    elif scenario == "sad":
        (tmp_path / "outside").mkdir()
        (home / "link").symlink_to(tmp_path / "outside")
        with pytest.raises(ValueError, match="symlink"):
            s.check_home_path(home, home / "link/x.json")
    else:
        with pytest.raises(ValueError, match="outside"):
            s.check_home_path(home, tmp_path / "elsewhere.json")


def test_save_trusted(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    data = {"happy": {"/p/.sandbox-ai.json": "ab"}, "sad": {}, "edge": {"/ü ñ/x": "cd"}}[scenario]
    s.save_trusted(data)
    assert s.trusted_configs() == data
    assert (tmp_path / "cfg/sandbox-ai").stat().st_mode & 0o777 == 0o700


def test_forget_config(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / ".sandbox-ai.json"
    path.write_text("{}")
    if scenario == "sad":
        assert s.forget_config(path) is False
        return
    s.trust_config(path)
    if scenario == "edge":
        path.unlink()
    assert s.forget_config(path) is True and s.trusted_configs() == {}


def test_listen_unix(scenario, tmp_path):
    path = tmp_path / "l.sock"
    if scenario == "sad":
        with pytest.raises(OSError):
            s.listen_unix(tmp_path / "missing" / "l.sock")
        return
    if scenario == "edge":
        path.write_text("stale")
    server = s.listen_unix(path)
    try:
        assert path.is_socket() and not [p for p in tmp_path.iterdir() if p.name.startswith(".l.sock")]
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(path))  # accepted at once: listening before the name appeared
        client.close()
    finally:
        server.close()


def test_relay_terminal(scenario, tmp_path):
    import struct as st, threading
    left, right = socket.socketpair()
    if scenario == "sad":
        right.close()  # the client vanishes before sending a size: nothing is started
        s.relay_terminal(left, ["/nonexistent"])
        return
    command = ([sys.executable, "-c", "import os; print(os.get_terminal_size(0).columns, flush=True)"]
               if scenario == "happy" else ["/nonexistent/attach"])
    thread = threading.Thread(target=s.relay_terminal, args=(left, command), daemon=True)
    thread.start()
    right.sendall(s.attach_frame(b"r", st.pack("!HH", 20, 77)))
    right.settimeout(10)
    received = b""
    try:
        while chunk := right.recv(4096):
            received += chunk
    except (ConnectionResetError, socket.timeout):
        pass
    thread.join(10)
    assert not thread.is_alive()
    assert (b"77" in received) if scenario == "happy" else received == b""


def test_terminate(scenario):
    signum = {"happy": signal.SIGTERM, "sad": signal.SIGHUP, "edge": signal.SIGINT}[scenario]
    with pytest.raises(SystemExit) as raised:
        s.terminate(signum, None)
    assert raised.value.code == 128 + signum


def test_launch(scenario, tmp_path, monkeypatch, capsys):
    if scenario == "sad":
        with pytest.raises(ValueError):
            s.launch(["codex", "--workspace", str(tmp_path), "--offline", "--dns", "--dry-run"])
    else:
        options = ["--dry-run"] + (["--gui"] if scenario == "edge" else [])
        assert s.launch(["codex", "--workspace", str(tmp_path / "w"), *options]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["agent"] == "codex" and (plan["gui"] is not None) == (scenario == "edge")


def test_check_workspace_scope(scenario, tmp_path, monkeypatch):
    """Review finding: HOME, /, the temp directory or the launcher's own checkout must not become the workspace."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    if scenario == "happy":
        s.check_workspace_scope(tmp_path / "home" / "src" / "app")
    elif scenario == "sad":
        for risky in [Path("/"), tmp_path / "home", s.HERE, s.HERE.parent, Path(tempfile.gettempdir())]:
            with pytest.raises(ValueError, match="Workspace"):
                s.check_workspace_scope(risky.resolve())
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
        (tmp_path / "run").mkdir()
        with pytest.raises(ValueError, match="XDG_RUNTIME_DIR"):
            s.check_workspace_scope(tmp_path / "run")
        s.check_workspace_scope(tmp_path / "run-not")


def test_auth_source_candidates(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)
    store = tmp_path / ".local/share/sandbox-ai/auth"
    if scenario == "happy":
        assert s.auth_source_candidates("claude") == [store / "claude/.claude/.credentials.json",
                                                      store / "claude/.claude.json",
                                                      tmp_path / ".claude/.credentials.json",
                                                      tmp_path / ".claude/.claude.json", tmp_path / ".claude.json"]
        # The login store feeds every sandbox: a workspace that contains it is refused.
        (tmp_path / ".local/share").mkdir(parents=True)
        with pytest.raises(ValueError, match="agent can change|lies in "):
            s.validate(s.parse_args(["claude", "--workspace", str(tmp_path / ".local")]))
    elif scenario == "sad":
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "work/.codex"))
        assert s.auth_source_candidates("codex") == [store / "codex/.codex/auth.json", tmp_path / "work/.codex/auth.json"]
        # A credential directory inside the workspace is refused before anything is read.
        with pytest.raises(ValueError, match="agent can change|lies in "):
            s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "work")]))
    else:
        # Set but empty counts as unset (XDG): never a path relative to the current directory.
        monkeypatch.setenv("XDG_DATA_HOME", "")
        monkeypatch.setenv("CODEX_HOME", "")
        assert all(path.is_absolute() for agent in ("codex", "opencode")
                   for path in s.auth_source_candidates(agent) + [src for src, _ in s.auth_sources(agent)])
        monkeypatch.delenv("CODEX_HOME")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        assert s.auth_source_candidates("opencode") == [tmp_path / "data/sandbox-ai/auth/opencode/.local/share/opencode/auth.json",
                                                        tmp_path / "data/opencode/auth.json"]


def test_opencode_catalog(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    if scenario == "sad":
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        assert s.opencode_catalog() == tmp_path / ".cache/opencode/models.json"
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
        assert s.opencode_catalog() == tmp_path / "c/opencode/models.json"


def test_has_controlling_terminal(scenario, monkeypatch):
    if scenario == "sad":
        monkeypatch.setattr(s.os, "open", Mock(side_effect=OSError(6, "No such device or address")))
        assert s.has_controlling_terminal() is False
    elif scenario == "happy":
        monkeypatch.setattr(s.os, "open", Mock(return_value=99))
        monkeypatch.setattr(s.os, "close", Mock())
        assert s.has_controlling_terminal() is True
    else:
        # bwrap with every standard stream redirected still checks TIOCSTI when a controlling terminal exists.
        monkeypatch.setattr(s, "has_controlling_terminal", lambda: True)
        monkeypatch.setattr(s, "check_tiocsti", Mock(side_effect=ValueError("TIOCSTI")))
        monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(s.os, "isatty", lambda fd: False)
        with pytest.raises(ValueError, match="TIOCSTI"):
            s.main(["codex", "--backend", "bwrap", "--offline", "--binary", "/usr/bin/true"])


def test_pod_owner(scenario, tmp_path):
    options = {"happy": ["--pod-name", "agent-session-x", "--tmux-socket-dir", str(tmp_path / "t")],
               "sad": ["--tmux-socket-dir", str(tmp_path / "t")],
               "edge": ["--pod-name", "agent-mcp-x", "--mcp-socket-dir", str(tmp_path / "m")]}[scenario]
    owner = s.pod_owner(s.parse_args(["codex", *options]))
    assert owner == {"happy": str((tmp_path / "t").resolve()), "sad": None, "edge": str((tmp_path / "m").resolve())}[scenario]


def test_remove_stale_pod(scenario, monkeypatch):
    calls = []
    labels = {"happy": {s.SESSION_LABEL: "/own"}, "sad": {s.SESSION_LABEL: "/other"}, "edge": None}[scenario]
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pod", "inspect"]:
            return Mock(returncode=0 if labels is not None else 125, stdout=json.dumps(labels))
        return Mock(returncode=0)
    monkeypatch.setattr(s.subprocess, "run", run)
    engine = Mock(podman_url=None)
    if scenario == "happy":
        assert s.remove_stale_pod(engine, "agent-mcp-x", "/own") is True
        assert calls[-1] == ["podman", "pod", "rm", "--force", "agent-mcp-x"]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="not created here"):
            s.remove_stale_pod(engine, "agent-mcp-x", "/own")
        assert len(calls) == 1
    else:
        assert s.remove_stale_pod(engine, "agent-mcp-x", "/own") is False


def test_relative_paths(scenario):
    options = {"happy": ["--mount", "/a:/mnt/a:ro", "--state-dir=~/s", "--git-token-file", "http://x=/t"],
               "sad": ["--mount", "data:/mnt/d:rw", "--local-config=c.json", "--git-token-file", "https://h=t",
                       "--copy", "f:.x"],
               "edge": ["--dns", "--allow-tcp", "1.2.3.4:5", "--workspace", "."]}[scenario]
    expected = {"happy": [], "sad": ["--mount data:/mnt/d:rw", "--local-config c.json", "--git-token-file https://h=t",
                                     "--copy f:.x"], "edge": ["--workspace ."]}[scenario]
    assert s.relative_paths(options) == expected


def test_review_workspace_inside_rw_mount_or_state_dir_is_refused(scenario, tmp_path):
    """Review finding: the agent could swap a directory on the way to the workspace for a symlink."""
    data = tmp_path / "data"
    workspace = data / "a" / "b" / "proj"
    workspace.mkdir(parents=True)
    options = {"happy": ["--mount", f"{tmp_path / 'other'}:/mnt/o:rw"],
               "sad": ["--mount", f"{data}:/mnt/data:rw"], "edge": ["--state-dir", str(data)]}[scenario]
    (tmp_path / "other").mkdir()
    args = s.parse_args(["codex", "--workspace", str(workspace), "--no-auth", *options])
    if scenario == "happy":
        s.validate(args)
    else:
        with pytest.raises(ValueError, match="agent can change"):
            s.validate(args)


def test_review_allow_refuses_scope_ids(scenario):
    value = {"happy": "2001:db8::/32", "sad": "fe80::1%x y", "edge": "fe80::1%eth0"}[scenario]
    args = s.parse_args(["codex", "--no-auth", "--allow", value])
    if scenario == "happy":
        s.validate(args)
    else:
        with pytest.raises(ValueError, match="scope ID"):
            s.validate(args)


def test_default_log_dir(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    a, b = tmp_path / "x" / "app", tmp_path / "y" / "app"
    if scenario == "happy":
        assert s.default_log_dir(a).parent == tmp_path / "state/sandbox-ai/logs"
        assert s.default_log_dir(a).name.startswith("app-")
    elif scenario == "sad":
        # Same directory name, different projects: different log directories.
        assert s.default_log_dir(a) != s.default_log_dir(b)
    else:
        assert s.default_log_dir(Path("/")).name.startswith("root-")


def test_load_runtime(scenario):
    """The launcher and the in-sandbox runtime share helpers; the runtime never loads the launcher."""
    runtime = s.load_runtime()
    if scenario == "happy":
        assert runtime.base_env().keys() == s.base_env().keys() and s.LOG_DIR == runtime.LOG_DIR
    elif scenario == "sad":
        source = (ROOT / "sandbox/runtime.py").read_text()
        code = source.split('"""', 2)[2]
        # Standalone inside the sandbox: it never loads the launcher or any other sibling.
        assert "agent_sandbox.py" not in code and "spec_from_file_location" not in code
    else:
        assert runtime is not s.RUNTIME and runtime.ATTACH_SOCKET == s.ATTACH_SOCKET


def test_contract_manifest_covers_every_launcher_function():
    """New production functions must get the same three contract scenarios."""
    functions = set()
    for source in ("sandbox/agent_sandbox.py", "sandbox/runtime.py"):
        tree = ast.parse((ROOT / source).read_text())
        functions |= {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for function in sorted(functions):
        contract = globals().get("test_" + function)
        assert callable(contract), "Missing happy/sad/edge contract for " + function
        assert "scenario" in contract.__code__.co_varnames[:contract.__code__.co_argcount]


# --- tmux by default, login store, write-back by token digest -------------------------------------


def test_build_parser(scenario):
    parser = s.build_parser()
    actions = parser._option_string_actions
    if scenario == "happy":
        assert actions["--tmux"].nargs == 0 and actions["--mount"].nargs is None
    elif scenario == "sad":
        with pytest.raises(SystemExit):
            parser.parse_args(["codex", "--tmu"])  # no abbreviations
    else:
        assert parser.parse_args(["codex", "--tmux", "--no-tmux"]).tmux is False  # the last one wins
        assert parser.parse_args(["codex"]).tmux is None


def test_resolve_tmux(scenario, monkeypatch):
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: True, raising=False)
    if scenario == "happy":
        assert s.resolve_tmux(s.parse_args(["claude"])) is True
        assert s.resolve_tmux(s.parse_args(["claude", "--", "--model", "opus"])) is True
        assert s.resolve_tmux(s.parse_args(["claude", "--no-tmux"])) is False
    elif scenario == "sad":
        monkeypatch.setattr(s.sys.stdin, "isatty", lambda: False, raising=False)
        assert s.resolve_tmux(s.parse_args(["claude"])) is False
        assert s.resolve_tmux(s.parse_args(["claude", "--tmux"])) is True  # insisted on: checked later
    else:
        assert s.resolve_tmux(s.parse_args(["claude", "--", "-p", "task"])) is False
        assert s.resolve_tmux(s.parse_args(["claude", "--", "mcp", "list"])) is False
        assert s.resolve_tmux(s.parse_args(["claude", "--binary", "python3"])) is False
        assert s.resolve_tmux(s.parse_args(["codex", "--probe"])) is False
        assert s.resolve_tmux(s.parse_args(["codex", "--session", "x"])) is False
        monkeypatch.setattr(s.shutil, "which", lambda name: None)
        assert s.resolve_tmux(s.parse_args(["codex", "--backend", "bwrap"])) is False


def test_auth_store(scenario, monkeypatch, tmp_path):
    if scenario == "happy":
        assert s.auth_store("claude") == Path.home() / ".local/share/sandbox-ai/auth/claude"
    elif scenario == "sad":
        monkeypatch.setenv("XDG_DATA_HOME", "")
        assert s.auth_store() == Path.home() / ".local/share/sandbox-ai/auth"
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
        assert s.auth_store("codex") == tmp_path / "d/sandbox-ai/auth/codex"


def test_store_login(scenario, tmp_path):
    store = s.auth_store("claude")
    if scenario == "happy":
        claude_credentials(store / ".claude/.credentials.json", 4102444800000)
        assert s.store_login("claude")
        (s.auth_store("codex") / ".codex").mkdir(parents=True)
        (s.auth_store("codex") / ".codex/auth.json").write_text('{"OPENAI_API_KEY": "sk"}')
        assert s.store_login("codex")
        # Without the agent's structure it is no login, and the host's is used instead.
        (s.auth_store("codex") / ".codex/auth.json").write_text('{"x": 1}')
        assert not s.store_login("codex")
    elif scenario == "sad":
        assert not s.store_login("claude") and not s.store_login("opencode")
        (store / ".claude").mkdir(parents=True)
        (store / ".claude/.credentials.json").write_text('{"other": 1}')
        assert not s.store_login("claude")
    else:
        # A link in the store is never followed.
        claude_credentials(tmp_path / "elsewhere.json", 4102444800000)
        (store / ".claude").mkdir(parents=True)
        (store / ".claude/.credentials.json").symlink_to(tmp_path / "elsewhere.json")
        assert not s.store_login("claude")


def test_store_sources(scenario):
    store = s.auth_store({"happy": "claude", "sad": "codex", "edge": "opencode"}[scenario])
    expected = {"happy": [(store / ".claude/.credentials.json", ".claude/.credentials.json"),
                          (store / ".claude.json", ".claude.json")],
                "sad": [(store / ".codex/auth.json", ".codex/auth.json")],
                "edge": [(store / ".local/share/opencode/auth.json", ".local/share/opencode/auth.json")]}[scenario]
    assert s.store_sources(store.name) == expected


def test_oauth_token_digest(scenario):
    login = {"accessToken": "a", "refreshToken": "r", "expiresAt": 1}
    if scenario == "happy":
        digest = s.oauth_token_digest(login)
        assert len(digest) == 64 and "a" * 2 not in digest[:0]
        # Fields other than the tokens do not count.
        assert s.oauth_token_digest({**login, "scopes": ["x"]}) == digest
    elif scenario == "sad":
        assert s.oauth_token_digest(None) is None
    else:
        assert s.oauth_token_digest(login) != s.oauth_token_digest({**login, "refreshToken": "rotated"})


def test_claude_recover_writeback(scenario, tmp_path):
    home = tmp_path / "home"
    source, sandbox = tmp_path / "host/.credentials.json", home / ".claude/.credentials.json"
    soon, later = (time.time() + 3600) * 1000, (time.time() + 7200) * 1000
    claude_credentials(source, soon)
    claude_credentials(sandbox, soon)
    s.remember_sync("claude", home, s.oauth_token_digest(s.claude_oauth(source)))
    if scenario == "happy":
        # The earlier run refreshed (rotating the refresh token) and was killed before writing back.
        claude_credentials(sandbox, later, refresh="rotated")
        assert s.claude_recover_writeback(home, (source, sandbox)) is True
        assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "rotated"
    elif scenario == "sad":
        # The host logged in again since: its login wins, nothing is written back.
        claude_credentials(sandbox, later, refresh="rotated")
        claude_credentials(source, soon, refresh="host-relogin")
        assert s.claude_recover_writeback(home, (source, sandbox)) is False
        assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "host-relogin"
    else:
        # Nothing refreshed, or tokens claiming an implausible lifetime: nothing written.
        assert s.claude_recover_writeback(home, (source, sandbox)) is False
        claude_credentials(sandbox, (time.time() + 365 * 86400) * 1000, refresh="forged")
        assert s.claude_recover_writeback(home, (source, sandbox)) is False
        assert json.loads(source.read_text())["claudeAiOauth"]["refreshToken"] == "refresh-fixture"


def test_sandbox_digest(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    if scenario == "happy":
        (home / ".codex/auth.json").write_text("{}")
        assert s.sandbox_digest(home, ".codex/auth.json") == hashlib.sha256(b"{}").hexdigest()
    elif scenario == "sad":
        assert s.sandbox_digest(home, ".codex/auth.json") is None
    else:
        (tmp_path / "x").write_text("{}")
        (home / ".codex/auth.json").symlink_to(tmp_path / "x")
        assert s.sandbox_digest(home, ".codex/auth.json") is None


def test_write_private_file(scenario, tmp_path):
    path = tmp_path / "f.json"
    if scenario == "sad":
        path.symlink_to(tmp_path / "target")
        with pytest.raises(ValueError, match="symlink"):
            s.write_private_file(path, b"x")
        assert not (tmp_path / "target").exists()
        return
    if scenario == "edge":
        path.write_text("old")
        path.chmod(0o644)
    s.write_private_file(path, b"new")
    assert path.read_bytes() == b"new" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".auth-*"))  # no temporary file left


def test_login_state(scenario, monkeypatch):
    host = Path.home() / ".claude/.credentials.json"
    if scenario == "happy":
        claude_credentials(host, 4102444800000)
        assert s.login_state("claude") == ("host", None)
        claude_credentials(s.auth_store("claude") / ".claude/.credentials.json", 4102444800000)
        assert s.login_state("claude") == ("store", None)
    elif scenario == "sad":
        assert s.login_state("claude") == ("host", "missing") and s.login_state("codex") == ("host", "missing")
        store_codex_login()
        assert s.login_state("codex") == ("store", None)
    else:
        host.parent.mkdir(parents=True)
        login = {"accessToken": "a", "refreshToken": "r", "expiresAt": 1000, "refreshTokenExpiresAt": 2000}
        host.write_text(json.dumps({"claudeAiOauth": login}))
        assert s.login_state("claude") == ("host", "expired")


def test_check_login(scenario, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: True, raising=False)
    host = Path.home() / ".claude/.credentials.json"
    login = Mock(return_value=0)
    monkeypatch.setattr(s, "load_login_module", lambda: Mock(login=login))
    if scenario == "happy":
        claude_credentials(host, 4102444800000)
        s.check_login(s.parse_args(["claude"]))
        assert capsys.readouterr().err == "" and not login.called
        # Missing: a hint only, no question.
        s.check_login(s.parse_args(["codex"]))
        assert "sandbox-ai login codex" in capsys.readouterr().err and not login.called
        return
    host.parent.mkdir(parents=True)
    expired = {"accessToken": "a", "refreshToken": "r", "expiresAt": 1000, "refreshTokenExpiresAt": 2000}
    host.write_text(json.dumps({"claudeAiOauth": expired}))
    if scenario == "sad":
        # Expired in a terminal: the user is asked and the login runs inside a sandbox.
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        s.check_login(s.parse_args(["claude", "--backend", "bwrap"]))
        assert login.call_args[0] == ("claude", ["--backend", "bwrap"])
        login.return_value = 1
        with pytest.raises(ValueError, match="Login failed"):
            s.check_login(s.parse_args(["claude"]))
    else:
        # No question without a terminal, for --no-auth, dry runs or a HOME that has a login.
        monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("asked")))
        for args in (["claude", "--no-auth"], ["claude", "--dry-run"]):
            s.check_login(s.parse_args(args))
        state = tmp_path / "state"
        claude_credentials(state / ".claude/.credentials.json", 4102444800000)
        s.check_login(s.parse_args(["claude", "--state-dir", str(state)]))
        monkeypatch.setattr(s.sys.stdin, "isatty", lambda: False, raising=False)
        s.check_login(s.parse_args(["claude"]))
        assert "expired claude login" in capsys.readouterr().err and not login.called


def test_load_login_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_login_module()
    else:
        module = s.load_login_module()
        assert callable(module.login) and module.LOGIN_ARGS["claude"] == ["auth", "login"]
        if scenario == "edge":
            assert s.load_login_module() is not module


def test_interactive_args(scenario):
    if scenario == "happy":
        assert s.interactive_args("claude", []) and s.interactive_args("claude", ["--model", "opus", "--continue"])
        assert s.interactive_args("codex", ["-m", "gpt-5", "--search"])
    elif scenario == "sad":
        for agent, args in (("claude", ["-p", "x"]), ("claude", ["--version"]), ("claude", ["mcp", "list"]),
                            ("codex", ["exec", "x"]), ("codex", ["login", "status"]), ("opencode", ["run", "x"]),
                            ("opencode", ["models"])):
            assert not s.interactive_args(agent, args), (agent, args)
    else:
        # An option we do not know, followed by a word: that word may be a subcommand; no tmux.
        assert not s.interactive_args("claude", ["--unknown", "value"])
        assert not s.interactive_args("claude", ["first message"])


def store_codex_login(refresh="r1", account="acct-1", **extra):
    path = s.auth_store("codex") / ".codex/auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"OPENAI_API_KEY": None, "tokens": {"id_token": "i", "access_token": "a",
                                                                  "refresh_token": refresh, "account_id": account},
                                "last_refresh": "t0", **extra}))
    return path


def test_login_document_ok(scenario):
    if scenario == "happy":
        assert s.login_document_ok("codex", {"tokens": {"access_token": "a", "refresh_token": "r", "account_id": "x"}})
        assert s.login_document_ok("codex", {"OPENAI_API_KEY": "sk-1"})
        assert s.login_document_ok("opencode", {"anthropic": {"type": "oauth", "refresh": "r"}})
    elif scenario == "sad":
        for agent, document in (("codex", {"x": 1}), ("codex", []), ("opencode", {}), ("opencode", {"a": "b"}),
                                ("codex", {"tokens": {"refresh_token": "r"}})):
            assert not s.login_document_ok(agent, document)
    else:
        assert not s.login_document_ok("codex", {"OPENAI_API_KEY": ""})


def test_stored_document(scenario, tmp_path):
    path = tmp_path / "auth.json"
    if scenario == "happy":
        path.write_text('{"OPENAI_API_KEY": "sk"}')
        assert s.stored_document("codex", path) == {"OPENAI_API_KEY": "sk"}
    elif scenario == "sad":
        path.write_text("{broken")
        assert s.stored_document("codex", path) is None
        assert s.stored_document("codex", tmp_path / "missing") is None
    else:
        (tmp_path / "real").write_text('{"OPENAI_API_KEY": "sk"}')
        path.symlink_to(tmp_path / "real")
        assert s.stored_document("codex", path) is None


def test_merge_refreshed(scenario):
    stored = {"OPENAI_API_KEY": None, "last_refresh": "t0",
              "tokens": {"id_token": "i", "access_token": "a", "refresh_token": "r1", "account_id": "acct"}}
    if scenario == "happy":
        sandbox = {"OPENAI_API_KEY": None, "last_refresh": "t1",
                   "tokens": {"id_token": "i2", "access_token": "a2", "refresh_token": "r2", "account_id": "acct"}}
        merged = s.merge_refreshed("codex", stored, sandbox)
        assert merged["tokens"]["refresh_token"] == "r2" and merged["last_refresh"] == "t1"
        oc = {"anthropic": {"type": "oauth", "refresh": "r1", "access": "a1", "expires": 1, "accountId": "x"}}
        new = {"anthropic": {"type": "oauth", "refresh": "r2", "access": "a2", "expires": 2, "accountId": "x"}}
        assert s.merge_refreshed("opencode", oc, new)["anthropic"]["refresh"] == "r2"
    elif scenario == "sad":
        # Review finding: the agent must not plant an API key or another account for every later sandbox.
        planted = {"OPENAI_API_KEY": "sk-attacker", "x": 1,
                   "tokens": {"access_token": "evil", "refresh_token": "evil", "account_id": "attacker"}}
        assert s.merge_refreshed("codex", stored, planted) is None
        assert s.merge_refreshed("codex", stored, {"OPENAI_API_KEY": "sk-attacker", "tokens": stored["tokens"]}) is None
        oc = {"openai": {"type": "api", "key": "sk-real"}}
        assert s.merge_refreshed("opencode", oc, {"openai": {"type": "api", "key": "sk-evil"},
                                                  "evil": {"type": "api", "key": "x"}}) is None
        assert s.merge_refreshed("codex", stored, "not a dict") is None
    else:
        assert s.merge_refreshed("codex", stored, json.loads(json.dumps(stored))) is None  # nothing new
        assert s.merge_refreshed("codex", {"OPENAI_API_KEY": "sk"}, {"OPENAI_API_KEY": "sk2"}) is None


def test_login_sync_store(scenario, monkeypatch, tmp_path):
    if scenario == "edge":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        assert s.login_sync_store() == tmp_path / "st/sandbox-ai/login-sync.json"
    else:
        assert s.login_sync_store() == Path.home() / ".local/state/sandbox-ai/login-sync.json"


def test_load_login_sync(scenario):
    store = s.login_sync_store()
    if scenario == "sad":
        assert s.load_login_sync() == {}
        return
    store.parent.mkdir(parents=True)
    store.write_text('{"claude:/x": "d"}' if scenario == "happy" else "[1]")
    assert s.load_login_sync() == ({"claude:/x": "d"} if scenario == "happy" else {})


def test_sync_key(scenario, tmp_path):
    home = tmp_path / {"happy": "h", "sad": "x/../h", "edge": "a:b"}[scenario]
    assert s.sync_key("codex", home) == f"codex:{home.resolve()}"


def test_remember_sync(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    if scenario == "sad":
        s.remember_sync("claude", home, None)  # nothing to forget: no file is created
        assert not s.login_sync_store().exists()
        return
    s.remember_sync("claude", home, "d1")
    assert s.load_login_sync() == {s.sync_key("claude", home): "d1"}
    if scenario == "happy":
        s.remember_sync("claude", home, None)
        assert s.load_login_sync() == {}
    else:
        # Entries of HOMEs that no longer exist are dropped.
        shutil.rmtree(home)
        other = tmp_path / "other"
        other.mkdir()
        s.remember_sync("codex", other, "d2")
        assert s.load_login_sync() == {s.sync_key("codex", other): "d2"}


def test_claude_recover_writeback_never_after_an_unshared_run(tmp_path):
    """Review finding: a --no-auth run on the HOME (another account) must not be taken for a missed
    write-back on the next start."""
    home, host = tmp_path / "home", Path.home() / ".claude/.credentials.json"
    soon = (time.time() + 3600) * 1000
    claude_credentials(host, soon)
    a = s.parse_args(["claude", "--state-dir", str(home)])
    home.mkdir(mode=0o700)
    s.prepare_home(a, home)
    assert s.sync_key("claude", home) in s.load_login_sync()
    s.prepare_home(s.parse_args(["claude", "--no-auth", "--state-dir", str(home)]), home)
    assert s.sync_key("claude", home) not in s.load_login_sync()
    claude_credentials(home / ".claude/.credentials.json", soon + 3600_000, refresh="other-account")
    s.prepare_home(a, home)
    assert json.loads(host.read_text())["claudeAiOauth"]["refreshToken"] == "refresh-fixture"


def test_store_writeback_state(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    a = s.parse_args(["codex"])
    if scenario == "sad":
        assert s.store_writeback_state(a, home) is None  # no stored login
        return
    source = store_codex_login()
    if scenario == "happy":
        shutil.copy(source, home / ".codex/auth.json")
        before = s.store_writeback_state(a, home)
        assert before["source"] == source and before["store"] == before["sandbox"]
        assert s.store_writeback_state(s.parse_args(["codex", "--no-auth"]), home) is None
    else:
        # A persistent HOME with a login of its own never writes it into the store.
        (home / ".codex/auth.json").write_text('{"tokens": {"refresh_token": "own"}}')
        assert s.store_writeback_state(a, home) is None


def test_store_writeback(scenario, tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    source = store_codex_login()
    shutil.copy(source, home / ".codex/auth.json")
    a = s.parse_args(["codex"])
    before = s.store_writeback_state(a, home)
    refreshed = json.loads(source.read_text())
    refreshed["tokens"]["refresh_token"] = "r2"
    if scenario == "happy":
        assert s.store_writeback(home, a, before) is None  # unchanged
        (home / ".codex/auth.json").write_text(json.dumps(refreshed))
        assert "written back" in s.store_writeback(home, a, before)
        assert json.loads(source.read_text())["tokens"]["refresh_token"] == "r2"
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
    elif scenario == "sad":
        # Review finding: a planted API key or another account never reaches the store.
        (home / ".codex/auth.json").write_text('{"OPENAI_API_KEY": "sk-attacker", "x": 1}')
        assert s.store_writeback(home, a, before) is None
        assert "sk-attacker" not in source.read_text() and s.store_login("codex")
        store_codex_login(refresh="other-run")
        (home / ".codex/auth.json").write_text(json.dumps(refreshed))
        assert "kept the stored one" in s.store_writeback(home, a, before)
        assert "other-run" in source.read_text()
    else:
        shutil.rmtree(home / ".codex")
        (home / ".codex").symlink_to(tmp_path)
        with pytest.raises(ValueError, match="symlink"):
            s.store_writeback(home, a, before)


def test_sync_store_login(scenario, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    a = s.parse_args(["codex", "--state-dir", str(home)])
    if scenario == "sad":
        s.sync_store_login(a, home)  # no stored login: nothing recorded
        assert s.load_login_sync() == {}
        return
    store_codex_login()
    s.prepare_home(a, home)  # copied from the store and recorded
    assert s.load_login_sync()[s.sync_key("codex", home)] == s.credential_digest(s.auth_store("codex") / ".codex/auth.json")
    if scenario == "happy":
        # Another sandbox refreshed into the store: this HOME's tokens are used up, take over the new ones.
        store_codex_login(refresh="r2")
        s.sync_store_login(a, home)
        assert json.loads((home / ".codex/auth.json").read_text())["tokens"]["refresh_token"] == "r2"
        assert "took over the newer stored login" in capsys.readouterr().err
    else:
        # The HOME changed its login itself: kept, and no longer followed.
        (home / ".codex/auth.json").write_text('{"OPENAI_API_KEY": "sk-own"}')
        store_codex_login(refresh="r2")
        s.sync_store_login(a, home)
        assert "sk-own" in (home / ".codex/auth.json").read_text()
        assert s.sync_key("codex", home) not in s.load_login_sync()


def test_check_login_prompt_keys(scenario, monkeypatch, capsys):
    """Review finding: Ctrl-D or Ctrl-C at the question gave a traceback; commands that print and exit
    are never asked."""
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: True, raising=False)
    host = Path.home() / ".claude/.credentials.json"
    host.parent.mkdir(parents=True)
    host.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 1000,
                                                  "refreshTokenExpiresAt": 2000}}))
    login = Mock(return_value=0)
    monkeypatch.setattr(s, "load_login_module", lambda: Mock(login=login))
    if scenario == "happy":
        monkeypatch.setattr("builtins.input", Mock(side_effect=EOFError))
        s.check_login(s.parse_args(["claude"]))  # Ctrl-D: no, start anyway with the hint
        assert "sandbox-ai login claude" in capsys.readouterr().err
    elif scenario == "sad":
        monkeypatch.setattr("builtins.input", Mock(side_effect=KeyboardInterrupt))
        with pytest.raises(SystemExit) as exc:
            s.check_login(s.parse_args(["claude"]))
        assert exc.value.code == 130
    else:
        monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("asked")))
        s.check_login(s.parse_args(["claude", "--", "-p", "task"]))
    assert not login.called


# --- kits, project memory and plugins ---------------------------------------------------------------

def make_kit(root, files):
    """Files below ROOT from {relative path: text}; returns ROOT."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def kit_args(tmp_path, agent="claude", *options):
    a = s.parse_args([agent, "--workspace", str(tmp_path / "work"), "--no-auth", *options])
    s.validate(a)
    return a


def data_home():
    return Path(os.environ["HOME"]) / ".local/share/sandbox-ai"


def test_load_kits_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_kits_module()
    else:
        module = s.load_kits_module()
        assert module.LAYOUT["claude"]["skills"] == ".claude/skills" and callable(module.scan)
        if scenario == "edge":
            assert s.load_kits_module() is not module


def test_load_privacy_module(scenario, monkeypatch, tmp_path):
    if scenario == "sad":
        monkeypatch.setattr(s, "HERE", tmp_path)
        with pytest.raises(FileNotFoundError):
            s.load_privacy_module()
    else:
        module = s.load_privacy_module()
        assert callable(module.scan_repository) and module.builtin_rules().patterns
        if scenario == "edge":
            assert s.load_privacy_module() is not module


def test_privacy_hooks_apply(scenario, tmp_path):
    if scenario == "happy":
        assert s.privacy_hooks_apply(s.parse_args(["claude", "--workspace", str(tmp_path)]))
    elif scenario == "sad":
        assert not s.privacy_hooks_apply(s.parse_args(["claude", "--workspace", str(tmp_path),
                                                       "--privacy-check", "off"]))
    else:
        # Probes and MCP servers commit nothing.
        assert not s.privacy_hooks_apply(s.parse_args(["claude", "--workspace", str(tmp_path), "--probe"]))
        assert s.privacy_hooks_apply(s.parse_args(["claude", "--workspace", str(tmp_path),
                                                   "--privacy-check", "always"]))


def test_privacy_rules_dir(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    if scenario == "happy":
        rules = tmp_path / "rules"
        args = s.parse_args(["claude", "--workspace", str(tmp_path / "w"), "--privacy-rules", str(rules)])
        assert s.privacy_rules_dir(args) == rules
    elif scenario == "sad":
        assert s.privacy_rules_dir(s.parse_args(["claude", "--workspace", str(tmp_path / "w")])) is None
    else:
        default = tmp_path / "config" / "sandbox-ai" / "privacy"
        default.mkdir(parents=True)
        assert s.privacy_rules_dir(s.parse_args(["claude", "--workspace", str(tmp_path / "w")])) == default
        # Rules the agent could change or read would be no rules: refused inside a workspace.
        args = s.parse_args(["claude", "--workspace", str(tmp_path / "w"), "--privacy-rules",
                             str(tmp_path / "w" / "r")])
        with pytest.raises(ValueError, match="--privacy-rules"):
            s.check_host_sources(args)


def test_privacy_context(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    options = {"happy": ["--allow-tcp", "192.168.50.10:1234", "--git-token-env",
                         "http://alice@localhost:3000=CONTRACT_GIT"],
               "sad": [], "edge": ["--allow-tcp", "[fd00::5]:11434"]}[scenario]
    context = s.privacy_context(s.parse_args(["claude", "--workspace", str(tmp_path), *options]))
    assert str(Path.home()) in context["homes"]
    if scenario == "happy":
        assert context["addresses"] == ["192.168.50.10"]
        assert context["servers"] == [{"host": "localhost", "port": 3000, "user": "alice"}]
        assert context["tokens"] == [GIT_TOKEN]
    elif scenario == "sad":
        assert context["addresses"] == context["servers"] == context["tokens"] == []
    else:
        assert context["addresses"] == ["fd00::5"]


def test_privacy_workspaces(scenario, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    if scenario == "happy":
        args = s.parse_args(["claude", "--workspace", f"{first},{second}"])
        assert s.privacy_workspaces(args) == [(first, "first"), (second, "second")]
    elif scenario == "sad":
        # A read-only workspace cannot have changed.
        assert s.privacy_workspaces(s.parse_args(["claude", "--workspace", str(first), "--workspace-ro"])) == []
    else:
        (tmp_path / "link").symlink_to(first)
        assert s.privacy_workspaces(s.parse_args(["claude", "--workspace", str(tmp_path / "link")])) == \
            [(first.resolve(), "first")]


def test_prepare_privacy(scenario, tmp_path, monkeypatch):
    workspace, session = tmp_path / "proj", tmp_path / "session"
    workspace.mkdir()
    session.mkdir()
    rules = tmp_path / "rules"
    rules.mkdir()
    options = ["claude", "--workspace", str(workspace), "--privacy-rules", str(rules)]
    if scenario == "sad":
        (rules / "proj.rules.json").write_text('{"pattern": [{"id": "x", "regex": "(", "severity": "block", '
                                               '"reason": "r"}]}')
        with pytest.raises(ValueError, match="uncompilable"):
            s.prepare_privacy(s.parse_args(options), session)
        assert s.prepare_privacy(s.parse_args([*options, "--privacy-check", "off"]), session) is None
        return
    (rules / "global.rules.json").write_text(json.dumps({"pattern": [
        {"id": "codename", "regex": r"\bbluefin\b", "severity": "block", "reason": "internal codename"}]}))
    monkeypatch.setenv("CONTRACT_GIT", GIT_TOKEN)
    check = s.prepare_privacy(s.parse_args([*options, "--git-token-env", "http://alice@localhost:3000=CONTRACT_GIT"]),
                              session)
    hook = json.loads((session / "privacy" / "hook.json").read_text())
    # Your own rules and the tokens stay on the host; the agent's hooks know the rest.
    assert "bluefin" not in json.dumps(hook) and GIT_TOKEN not in json.dumps(hook)
    assert {p["id"] for p in hook["rules"]["pattern"]} >= {"host-home", "own-git-account"}
    assert hook["own_hosts"] == ["localhost"]
    assert (session / "privacy" / "privacy.py").read_bytes() == (ROOT / "sandbox/privacy.py").read_bytes()
    if scenario == "edge" and shutil.which("git"):
        # After the run: a workspace with a public remote and an unpushed secret is reported.
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/o/r.git"],
                       check=True)
        (workspace / "notes.txt").write_text("project bluefin\n")
        printed = []
        monkeypatch.setattr(sys, "stderr", Mock(write=printed.append))
        check()
        text = "".join(printed)
        assert "1 blocking" in text and "notes.txt:1 (untracked) [codename]" in text


def test_kits_apply(scenario, tmp_path):
    if scenario == "happy":
        assert s.kits_apply(s.parse_args(["claude", "--workspace", str(tmp_path)]))
    elif scenario == "sad":
        assert not s.kits_apply(s.parse_args(["claude", "--workspace", str(tmp_path), "--probe"]))
    else:
        # MCP server sandboxes run a server, not an agent: no skills, memory or plugins.
        assert not s.kits_apply(s.parse_args(["codex", "--workspace", str(tmp_path),
                                              "--mcp-socket-dir", str(tmp_path / "sock")]))


def test_kit_managed_paths(scenario, tmp_path):
    from pathlib import PurePosixPath as P
    if scenario == "happy":
        paths = s.kit_managed_paths(s.parse_args(["claude", "--workspace", str(tmp_path)]))
        assert {P(".claude/skills"), P(".claude/CLAUDE.md"), P(".claude/projects/-work/memory"),
                P(".claude/plugins/cache")} <= set(paths)
    elif scenario == "sad":
        assert s.kit_managed_paths(s.parse_args(["codex", "--workspace", str(tmp_path), "--no-default-kit",
                                                 "--no-memory", "--no-plugins"])) == []
        assert s.kit_managed_paths(s.parse_args(["codex", "--workspace", str(tmp_path), "--probe"])) == []
    else:
        # Without the default kit, a --kit still fills the agent's directories; memory alone otherwise.
        assert P(".agents/skills") in s.kit_managed_paths(
            s.parse_args(["codex", "--workspace", str(tmp_path), "--no-default-kit", "--kit", "x"]))
        assert s.kit_managed_paths(s.parse_args(["opencode", "--workspace", str(tmp_path), "--no-default-kit"])) \
            == [P(".config/opencode/AGENTS.md")]


def test_kit_plan(scenario, tmp_path):
    make_kit(data_home() / "kits/review/claude", {"skills/review/SKILL.md": "---\nname: review\n---\n",
                                                   "commands/check.md": "check"})
    if scenario == "happy":
        plan = s.kit_plan(kit_args(tmp_path, "claude", "--kit", "review"))
        assert plan["entries"] == {"skills": ["review", "sandbox-ai"], "commands": ["check.md"]}
        assert plan["memory"].startswith(str(Path(os.environ["HOME"]) / ".local/state/sandbox-ai/memory/work-"))
        assert any("created with examples on the first start" in note for note in plan["notes"])
        assert not (data_home() / "home").exists()  # planning changes nothing
    elif scenario == "sad":
        with pytest.raises(ValueError, match="--kit missing: .* does not exist .*kits for claude: review"):
            s.kit_plan(kit_args(tmp_path, "claude", "--kit", "missing"))
        assert s.kit_plan(kit_args(tmp_path, "claude", "--probe")) is None
    else:
        make_kit(data_home() / "kits/other/claude", {"skills/review/SKILL.md": "x"})
        with pytest.raises(ValueError, match="skill 'review' comes from both kit review and kit other"):
            s.kit_plan(kit_args(tmp_path, "claude", "--kit", "review", "--kit", "other"))
        plan = s.kit_plan(kit_args(tmp_path, "claude", "--kit", "review", "--no-memory", "--no-plugins"))
        assert plan["memory"] is None and "plugins" not in plan


def test_ensure_mount_point(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    if scenario == "happy":
        s.ensure_mount_point(home, ".claude/skills")
        assert (home / ".claude/skills").is_dir()
    elif scenario == "sad":
        (tmp_path / "elsewhere").mkdir()
        (home / ".claude").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(ValueError, match="symlink"):
            s.ensure_mount_point(home, ".claude/skills")
        assert not (tmp_path / "elsewhere/skills").exists()
    else:
        (home / ".codex").mkdir()
        (home / ".codex/prompts").write_text("a file the agent left")
        with pytest.raises(ValueError, match="not a directory"):
            s.ensure_mount_point(home, ".codex/prompts")
        s.ensure_mount_point(home, ".codex")  # existing directory: fine


def plugin_store_fixture(installed=("sandcastle@sandbox-ai",), known=("sandbox-ai",)):
    store = data_home() / "plugins/claude/.claude/plugins"
    (store / "cache/sandbox-ai/sandcastle/1.0.0").mkdir(parents=True)
    (store / "marketplaces").mkdir()
    (store / "known_marketplaces.json").write_text(json.dumps(
        {name: {"source": {"source": "directory", "path": "/sandbox/marketplace"},
                "installLocation": "/sandbox/marketplace"} for name in known}))
    (store / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        plugin: [{"scope": "user", "installPath": "/root/.claude/plugins/cache/x", "version": "1.0.0"}]
        for plugin in installed}}))
    return store


def test_claude_plugins(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marketplace = {"sandbox-ai": {"source": "directory", "path": "/sandbox/marketplace"}}
    notes = []
    if scenario == "happy":
        store = plugin_store_fixture(installed=("sandcastle@sandbox-ai", "other@sandbox-ai"))
        mounts = s.claude_plugins(home, marketplace, ["sandcastle@sandbox-ai"], notes)
        assert mounts == [(store / "cache", "/root/.claude/plugins/cache", "ro"),
                          (store / "marketplaces", "/root/.claude/plugins/marketplaces", "ro")]
        settings = json.loads((home / ".claude/settings.json").read_text())
        # Installed for another kit, so off in this run.
        assert settings["enabledPlugins"] == {"sandcastle@sandbox-ai": True, "other@sandbox-ai": False}
        assert settings["extraKnownMarketplaces"] == {"sandbox-ai": {"source": marketplace["sandbox-ai"]}}
        known = json.loads((home / ".claude/plugins/known_marketplaces.json").read_text())
        assert known["sandbox-ai"]["autoUpdate"] is False
        assert notes == []
    elif scenario == "sad":
        # Nothing installed yet: nothing mounted or enabled, and a hint how to install.
        assert s.claude_plugins(home, marketplace, ["sandcastle@sandbox-ai"], notes) == []
        assert "sandbox-ai plugins claude" in notes[0] and "sandcastle@sandbox-ai" in notes[0]
        assert json.loads((home / ".claude/settings.json").read_text())["enabledPlugins"] == {}
    else:
        plugin_store_fixture()
        # A reused HOME whose settings are not JSON is left alone, with a note.
        (home / ".claude").mkdir()
        (home / ".claude/settings.json").write_text("{broken")
        s.claude_plugins(home, marketplace, ["sandcastle@sandbox-ai"], notes)
        assert (home / ".claude/settings.json").read_text() == "{broken"
        assert any(note.startswith("plugins not enabled") for note in notes)


def test_prepare_kits(scenario, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    if scenario == "happy":
        a = kit_args(tmp_path, "claude")
        mounts, notes, memory = s.prepare_kits(a, home, tmp_path / "staging")
        # First start: the default kit is created from the templates, its skill mounted read-only.
        assert (data_home() / "home/claude/skills/sandbox-example/SKILL.md").is_file()
        assert ((tmp_path / "staging/skills"), "/root/.claude/skills", "ro") in mounts
        assert (s.load_kits_module().MARKETPLACE, "/sandbox/marketplace", "ro") in mounts
        assert (tmp_path / "staging/skills/sandbox-example/SKILL.md").is_file()
        assert (home / ".claude/skills").is_dir()  # the mount point
        # The memory template was taken once into the new project memory and the HOME.
        assert (home / ".claude/CLAUDE.md").read_text().startswith("# Project notes")
        assert set(memory) == {".claude/CLAUDE.md"}
        assert any(note.startswith("created the default kit") for note in notes)
        assert any(note.startswith("new project memory") for note in notes)
    elif scenario == "sad":
        make_kit(data_home() / "kits/bad/claude", {"marketplaces.json": json.dumps(
            {"marketplaces": {"x": {"source": {"source": "directory", "path": "/etc"}}}})})
        a = kit_args(tmp_path, "claude", "--kit", "bad")
        with pytest.raises(ValueError, match="directory source can only be sandbox-ai's own"):
            s.prepare_kits(a, home, tmp_path / "staging")
        assert s.prepare_kits(kit_args(tmp_path, "claude", "--probe"), home, tmp_path / "s2") == ([], [], None)
    else:
        # OpenCode: npm plugins go into OPENCODE_CONFIG_CONTENT, with a note about the npm registry.
        make_kit(data_home() / "home/opencode", {"marketplaces.json": json.dumps({"plugins": ["opencode-foo@1.2.3"]})})
        a = kit_args(tmp_path, "opencode", "--no-memory")
        mounts, notes, memory = s.prepare_kits(a, home, tmp_path / "staging")
        assert [dest for _, dest, _ in mounts] == ["/root/.config/opencode/skills"] and memory is None
        assert json.loads(s.agent_environment(a)["OPENCODE_CONFIG_CONTENT"]) == {"plugin": ["opencode-foo@1.2.3"]}
        assert any("registry.npmjs.org" in note for note in notes)


def test_save_memory(scenario, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    a = kit_args(tmp_path, "claude", "--no-default-kit")
    manifest, _, store, _ = s.load_kits_module().memory_prepare("claude", a.workspaces[0], home)
    memory = home / ".claude/projects/-work/memory"
    if scenario == "happy":
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text("- builds with make")
        s.save_memory(a, home, manifest)
        assert (store / ".claude/projects/-work/memory/MEMORY.md").read_text() == "- builds with make"
        assert "1 file(s) kept" in capsys.readouterr().err
    elif scenario == "sad":
        memory.mkdir(parents=True)
        (memory / "leak").symlink_to("/etc/passwd")
        s.save_memory(a, home, manifest)
        assert not (store / ".claude/projects/-work/memory/leak").exists()
        assert "leak skipped: symlink" in capsys.readouterr().err
    else:
        # Nothing changed: nothing written, nothing printed.
        s.save_memory(a, home, manifest)
        assert capsys.readouterr().err == ""
        assert sorted(p.name for p in store.rglob("*")) == []
        assert not list(store.parent.glob(".pending-*"))  # the run is recorded as saved


# --- host devices (--device, --gpu) ---------------------------------------------------------------


@pytest.fixture
def fake_dev(tmp_path, monkeypatch):
    """A /dev of regular files that device_mode presents as devices: "sda*" as block devices,
    everything else as character devices."""
    dev = tmp_path / "dev"
    for name in ("ttyACM0", "kvm", "kfd", "video0", "console", "null", "hidraw0", "sda", "nvidia0", "nvidiactl",
                 "nvidia-uvm", "bus/usb/001/004", "dri/renderD128", "snd/pcmC0D0c", "input/event0"):
        (dev / name).parent.mkdir(parents=True, exist_ok=True)
        (dev / name).write_text("")
        (dev / name).chmod(0o666)
    (dev / "serial/by-id").mkdir(parents=True)
    (dev / "serial/by-id/usb-Board").symlink_to("../../ttyACM0")
    (dev / "escape").symlink_to(tmp_path)
    real_mode = s.device_mode

    def mode(path):
        path = Path(path)
        if path.is_dir():
            return stat.S_IFDIR | 0o755
        if path.name.startswith("sda"):
            return stat.S_IFBLK | 0o660
        if dev in path.parents:
            return stat.S_IFCHR | 0o666
        return real_mode(path)

    monkeypatch.setattr(s, "DEV", dev)
    monkeypatch.setattr(s, "device_mode", mode)
    monkeypatch.setattr(s, "CDI_DIRS", (tmp_path / "cdi",))
    return dev


def test_device_mode(scenario, tmp_path):
    if scenario == "happy":
        assert stat.S_ISCHR(s.device_mode("/dev/null"))
    elif scenario == "sad":
        with pytest.raises(OSError):
            s.device_mode(tmp_path / "missing")
    else:
        assert stat.S_ISDIR(s.device_mode(tmp_path))


def test_parse_device(scenario, fake_dev, tmp_path):
    if scenario == "happy":
        assert s.parse_device(str(fake_dev / "ttyACM0")) == (str(fake_dev / "ttyACM0"), "node")
        # Symlinks are resolved: the agent sees the device under its real name.
        assert s.parse_device(str(fake_dev / "serial/by-id/usb-Board")) == (str(fake_dev / "ttyACM0"), "node")
        assert s.parse_device(str(fake_dev / "bus/usb")) == (str(fake_dev / "bus/usb"), "dir")
        assert s.parse_device(str(fake_dev / "bus/usb/001")) == (str(fake_dev / "bus/usb/001"), "dir")
    elif scenario == "sad":
        cases = {"console": "consoles", "null": "already", "hidraw0": "keyboards", "input/event0": "keyboards",
                 "sda": "block devices", "serial": "directories", "escape": "not to a device",
                 "missing": "No such file"}
        for name, message in cases.items():
            with pytest.raises(ValueError, match=message):
                s.parse_device(str(fake_dev / name))
        for value in ("", "relative/ttyACM0", "/etc/passwd", f"{fake_dev}/ttyACM0:rw", f"{fake_dev}/a,b"):
            with pytest.raises(ValueError, match="--device expects"):
                s.parse_device(value)
    else:
        # The option name in messages follows the caller; a file outside /dev is no device.
        (fake_dev / "plain").write_text("")
        with pytest.raises(ValueError, match="--gpu dri"):
            s.parse_device(str(fake_dev / "console"), "--gpu dri")
        assert s.parse_device(str(fake_dev / "dri")) == (str(fake_dev / "dri"), "dir")


def test_nvidia_cdi_available(scenario, fake_dev, tmp_path):
    cdi = tmp_path / "cdi"
    if scenario == "happy":
        cdi.mkdir()
        (cdi / "nvidia.yaml").write_text("cdiVersion: 0.5.0\nkind: nvidia.com/gpu\n")
        assert s.nvidia_cdi_available()
    elif scenario == "sad":
        assert not s.nvidia_cdi_available()  # no directory at all
    else:
        cdi.mkdir()
        (cdi / "notes.txt").write_text("nvidia.com/gpu")  # not a specification
        (cdi / "other.json").write_text('{"kind": "vendor.example/fpga"}')
        assert not s.nvidia_cdi_available()


def test_gpu_devices(scenario, fake_dev, tmp_path):
    if scenario == "happy":
        assert s.gpu_devices(s.parse_args(["codex"])) == []
        assert s.gpu_devices(s.parse_args(["codex", "--gpu", "dri"])) == [(str(fake_dev / "dri"), "dir")]
        assert s.gpu_devices(s.parse_args(["codex", "--gpu", "rocm"])) == [(str(fake_dev / "dri"), "dir"),
                                                                          (str(fake_dev / "kfd"), "node")]
        (tmp_path / "cdi").mkdir()
        (tmp_path / "cdi" / "nvidia.yaml").write_text("kind: nvidia.com/gpu\n")
        assert s.gpu_devices(s.parse_args(["codex", "--gpu", "nvidia"])) == [("nvidia.com/gpu=all", "cdi")]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="nvidia-ctk cdi generate"):
            s.gpu_devices(s.parse_args(["codex", "--gpu", "nvidia"]))
        (fake_dev / "kfd").unlink()
        with pytest.raises(ValueError, match="kfd"):
            s.gpu_devices(s.parse_args(["codex", "--gpu", "rocm"]))
        for name in ("nvidia0", "nvidiactl", "nvidia-uvm"):
            (fake_dev / name).unlink()
        with pytest.raises(ValueError, match="NVIDIA driver"):
            s.gpu_devices(s.parse_args(["codex", "--backend", "bwrap", "--gpu", "nvidia"]))
    else:
        # bwrap gets the device nodes (the host's /usr brings the libraries); a remote Podman its own CDI.
        assert s.gpu_devices(s.parse_args(["codex", "--backend", "bwrap", "--gpu", "nvidia"])) == [
            (str(fake_dev / name), "node") for name in ("nvidia-uvm", "nvidia0", "nvidiactl")]
        remote = s.parse_args(["codex", "--gpu", "nvidia", "--podman-url", "unix:///example/podman.sock"])
        assert s.gpu_devices(remote) == [("nvidia.com/gpu=all", "cdi")]


def test_devices(scenario, fake_dev):
    if scenario == "happy":
        a = s.parse_args(["codex", "--device", str(fake_dev / "ttyACM0"), "--device", str(fake_dev / "kvm")])
        assert s.devices(a) == [(str(fake_dev / "ttyACM0"), "node"), (str(fake_dev / "kvm"), "node")]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="consoles"):
            s.devices(s.parse_args(["codex", "--device", str(fake_dev / "kvm"), "--device", str(fake_dev / "console")]))
    else:
        # Repeats and devices inside a directory passed whole are dropped.
        a = s.parse_args(["codex", "--device", str(fake_dev / "serial/by-id/usb-Board"), "--device",
                          str(fake_dev / "ttyACM0"), "--device", str(fake_dev / "dri/renderD128"), "--gpu", "dri"])
        assert s.devices(a) == [(str(fake_dev / "ttyACM0"), "node"), (str(fake_dev / "dri"), "dir")]


def acl(*entries):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


def test_device_access(scenario, tmp_path, monkeypatch):
    device = tmp_path / "ttyACM0"
    device.write_text("")
    uid = os.getuid()

    def getxattr(path, name, value=None):
        if value is None:
            raise OSError(61, "No data available")
        return value

    if scenario == "happy":
        device.chmod(0o600)
        assert s.device_access(device)
        # udev's uaccess: a named ACL entry for this user, limited by the mask.
        device.chmod(0o000)
        monkeypatch.setattr(os, "getxattr", lambda path, name: acl((1, 0, 0xFFFFFFFF), (2, 6, uid), (4, 0, 0xFFFFFFFF),
                                                                  (0x10, 6, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)))
        monkeypatch.setattr(os, "stat", lambda path: Mock(st_uid=uid + 1, st_gid=os.getgid() + 1, st_mode=0o20000))
        assert s.device_access(device)
    elif scenario == "sad":
        # Only the group could open it, and the sandbox keeps no supplementary groups.
        monkeypatch.setattr(os, "getxattr", lambda path, name: getxattr(path, name))
        monkeypatch.setattr(os, "stat", lambda path: Mock(st_uid=uid + 1, st_gid=os.getgid() + 1, st_mode=0o20660))
        assert not s.device_access(device)
        # A named entry masked down to read only.
        monkeypatch.setattr(os, "getxattr", lambda path, name: acl((1, 6, 0xFFFFFFFF), (2, 6, uid), (4, 0, 0xFFFFFFFF),
                                                                  (0x10, 4, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)))
        assert not s.device_access(device)
    else:
        # Others may: the device is open to everyone; an ACL in an unknown version counts as none.
        monkeypatch.setattr(os, "getxattr", lambda path, name: struct.pack("<I", 9))
        monkeypatch.setattr(os, "stat", lambda path: Mock(st_uid=uid + 1, st_gid=os.getgid() + 1, st_mode=0o20666))
        assert s.device_access(device)


def test_group_name(scenario):
    if scenario == "happy":
        assert s.group_name(0) == "root"
    elif scenario == "sad":
        assert s.group_name(2 ** 31 - 7) == str(2 ** 31 - 7)
    else:
        assert s.group_name(os.getgid()) == s.grp.getgrgid(os.getgid()).gr_name


def test_device_notes(scenario, fake_dev, monkeypatch):
    if scenario == "happy":
        notes = s.device_notes(s.parse_args(["codex", "--device", str(fake_dev / "ttyACM0"), "--device",
                                             str(fake_dev / "bus/usb")]))
        assert notes[0] == (f"devices for the agent (their drivers run in your host's kernel): {fake_dev}/ttyACM0, "
                            f"{fake_dev}/bus/usb (directory: devices plugged in later appear too)")
        assert len(notes) == 2 and "security keys" in notes[1]
        assert s.device_notes(s.parse_args(["codex"])) == []
    elif scenario == "sad":
        monkeypatch.setattr(s, "device_access", lambda path: False)
        notes = s.device_notes(s.parse_args(["codex", "--device", str(fake_dev / "ttyACM0"), "--device",
                                             str(fake_dev / "dri")]))
        assert "cannot open" in notes[1] and "ttyACM0" in notes[1] and "only through group" in notes[1]
        assert "any device in" in notes[2] and "udev rule" in notes[2]
    else:
        notes = s.device_notes(s.parse_args(["codex", "--device", str(fake_dev / "video0"), "--device",
                                             str(fake_dev / "snd")]))
        assert any("camera" in note for note in notes) and any("microphones" in note for note in notes)


# --- workspaces trusted without asking -------------------------------------------------------------


def test_claude_trusted_workspaces(scenario, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    a = s.parse_args(["claude", "--workspace", f"{tmp_path / 'app'},{tmp_path / 'lib'}"])
    if scenario == "happy":
        s.claude_trusted_workspaces(a, home)
        projects = json.loads((home / ".claude.json").read_text())["projects"]
        assert projects == {"/work": {"hasTrustDialogAccepted": True},
                            "/workspaces/lib": {"hasTrustDialogAccepted": True}}
    elif scenario == "sad":
        (home / ".claude.json").write_text("{not json")
        s.claude_trusted_workspaces(a, home)
        assert (home / ".claude.json").read_text() == "{not json"
        assert "may ask whether you trust" in capsys.readouterr().err
    else:
        # Other keys of a project and other projects stay; a second run changes nothing.
        (home / ".claude.json").write_text(json.dumps({"projects": {"/work": {"history": [1]}, "/old": {}}}))
        s.claude_trusted_workspaces(a, home)
        before = (home / ".claude.json").stat().st_mtime_ns
        s.claude_trusted_workspaces(a, home)
        projects = json.loads((home / ".claude.json").read_text())["projects"]
        assert projects["/work"] == {"history": [1], "hasTrustDialogAccepted": True} and projects["/old"] == {}
        assert (home / ".claude.json").stat().st_mtime_ns == before


def test_codex_trusted_workspaces(scenario, tmp_path, capsys):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    a = s.parse_args(["codex", "--workspace", f"{tmp_path / 'app'},{tmp_path / 'lib'}"])
    config = home / ".codex/config.toml"
    if scenario == "happy":
        s.codex_trusted_workspaces(a, home)
        assert config.read_text() == ('[projects."/work"]\ntrust_level = "trusted"\n\n'
                                      '[projects."/workspaces/lib"]\ntrust_level = "trusted"\n')
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
    elif scenario == "sad":
        config.symlink_to(tmp_path / "elsewhere.toml")
        with pytest.raises(ValueError, match="symlink"):
            s.codex_trusted_workspaces(a, home)
        assert not (tmp_path / "elsewhere.toml").exists()
    else:
        # Existing settings and an explicit decision for a workspace are kept.
        config.write_text('model = "gpt-5"\n\n[projects.\'/work\']\ntrust_level = "untrusted"\n')
        s.codex_trusted_workspaces(a, home)
        assert config.read_text() == ('model = "gpt-5"\n\n[projects.\'/work\']\ntrust_level = "untrusted"\n\n'
                                      '[projects."/workspaces/lib"]\ntrust_level = "trusted"\n')
        s.codex_trusted_workspaces(a, home)
        assert config.read_text().count("/workspaces/lib") == 1


# --- connection proxy (runtime.py) and its launcher side -------------------------------------------

rt = s.RUNTIME
needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
OID_A, OID_B = "a" * 40, "b" * 40


def client_hello(name):
    """A real ClientHello with SNI NAME, taken from Python's TLS client."""
    context = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def test_run_openssl(scenario, tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    if scenario == "happy":
        rt.run_openssl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out",
                        str(tmp_path / "k.pem")])
        assert (tmp_path / "k.pem").read_text().startswith("-----BEGIN PRIVATE KEY")
    elif scenario == "sad":
        with pytest.raises(RuntimeError, match="openssl genpkey failed"):
            rt.run_openssl(["genpkey", "-algorithm", "NOPE"])
    else:
        # The configuration's $ENV::SAN comes from the san argument.
        (tmp_path / "c.cnf").write_text("x = $ENV::SAN\n[req]\ndistinguished_name=dn\n[dn]\n")
        rt.run_openssl(["req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout",
                        str(tmp_path / "k"), "-subj", "/CN=x", "-config", str(tmp_path / "c.cnf"), "-out",
                        str(tmp_path / "r")], san="DNS:x")


@needs_openssl
def test_create_ca(scenario, tmp_path):
    bundle = tmp_path / "system.crt"
    bundle.write_bytes(b"-----BEGIN CERTIFICATE-----\nSYSTEM\n-----END CERTIFICATE-----\n")
    if scenario == "happy":
        rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=(str(tmp_path / "missing"), str(bundle)))
        ca = (tmp_path / "ca" / "ca.pem").read_bytes()
        assert (tmp_path / "ca" / "bundle.pem").read_bytes() == bundle.read_bytes() + ca
        assert (tmp_path / "run" / "system.pem").read_bytes() == bundle.read_bytes()
        assert stat.S_IMODE((tmp_path / "run").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "ca" / "bundle.pem").stat().st_mode) == 0o644
        text = subprocess.run(["openssl", "x509", "-in", str(tmp_path / "ca" / "ca.pem"), "-noout", "-text"],
                              capture_output=True, text=True).stdout
        assert "CA:TRUE, pathlen:0" in text and "this run only" in text
    elif scenario == "sad":
        (tmp_path / "run").write_text("a file")
        with pytest.raises(OSError):
            rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=())
    else:
        # Without a system bundle the agent's bundle is the session CA alone.
        rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=())
        assert (tmp_path / "ca" / "bundle.pem").read_bytes() == (tmp_path / "ca" / "ca.pem").read_bytes()


@needs_openssl
def test_server_certificate(scenario, tmp_path):
    rt.create_ca(tmp_path / "run", tmp_path / "ca", bundles=())
    run = tmp_path / "run"
    if scenario == "happy":
        path = rt.server_certificate(run, "API.Example.org.")
        assert path.name == "api.example.org.pem"
        assert rt.server_certificate(run, "api.example.org") == path  # made once
        verify = subprocess.run(["openssl", "verify", "-CAfile", str(tmp_path / "ca" / "ca.pem"), str(path)],
                                capture_output=True, text=True)
        assert verify.returncode == 0
    elif scenario == "sad":
        for name in ("../../etc/passwd", "a b", "x" * 300, "-subj"):
            with pytest.raises(ValueError, match="not a host name"):
                rt.server_certificate(run, name)
    else:
        path = rt.server_certificate(run, "2001:db8::1")
        text = subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-ext", "subjectAltName"],
                              capture_output=True, text=True).stdout
        assert "IP Address:2001:DB8:0:0:0:0:0:1" in text.upper().replace("IP ADDRESS", "IP Address")


def test_client_hello_sni(scenario):
    hello = client_hello("api.example.org")
    if scenario == "happy":
        assert rt.client_hello_sni(hello) == ("tls", "api.example.org")
    elif scenario == "sad":
        assert rt.client_hello_sni(b"GET / HTTP/1.1\r\n") == ("other", None)
        assert rt.client_hello_sni(b"SSH-2.0-x") == ("other", None)
        assert rt.client_hello_sni(b"\x16\x03\x01\x00\x05\x02\x00\x00\x01\x00") == ("tls", None)  # not a ClientHello
    else:
        assert rt.client_hello_sni(hello[:3]) == ("partial", None)
        assert rt.client_hello_sni(hello[:-1]) == ("partial", None)
        assert rt.client_hello_sni(b"") == ("other", None)


def test_original_destination(scenario):
    if scenario == "happy":
        sock = Mock(family=socket.AF_INET)
        sock.getsockopt.return_value = struct.pack("!HH4s8x", socket.AF_INET, 443, socket.inet_aton("192.0.2.7"))
        assert rt.original_destination(sock) == ("192.0.2.7", 443)
        assert sock.getsockopt.call_args.args == (socket.SOL_IP, 80, 16)
    elif scenario == "sad":
        sock = Mock(family=socket.AF_INET)
        sock.getsockopt.side_effect = OSError(2, "No such file or directory")  # not redirected
        with pytest.raises(OSError):
            rt.original_destination(sock)
    else:
        sock = Mock(family=socket.AF_INET6)
        sock.getsockopt.return_value = (struct.pack("!HHI", socket.AF_INET6, 8443, 0)
                                        + socket.inet_pton(socket.AF_INET6, "2001:db8::5") + b"\0" * 4)
        assert rt.original_destination(sock) == ("2001:db8::5", 8443)


def test_allowed_destination(scenario):
    allow = {"nets": ["192.0.2.0/24"], "https": ["198.51.100.4/32"], "tcp": [["203.0.113.9", 5432]],
             "loopback": [3000]}
    if scenario == "happy":
        assert rt.allowed_destination(allow, "192.0.2.200", 22)
        assert rt.allowed_destination(allow, "198.51.100.4", 443)
        assert rt.allowed_destination(allow, "203.0.113.9", 5432)
        assert rt.allowed_destination(allow, "127.0.0.1", 3000)
    elif scenario == "sad":
        assert not rt.allowed_destination(allow, "198.51.100.4", 80)
        assert not rt.allowed_destination(allow, "203.0.113.9", 5433)
        assert not rt.allowed_destination(allow, "127.0.0.1", 15001)  # the proxy itself
        assert not rt.allowed_destination({}, "192.0.2.1", 443)
    else:
        # Everything outside, but loopback only where forwarded.
        assert rt.allowed_destination({"all": True}, "2001:db8::1", 1)
        assert not rt.allowed_destination({"all": True}, "::1", 22)


def test_redact_headers(scenario):
    if scenario == "happy":
        assert rt.redact_headers([["Authorization", "Bearer x"], ["X-Api-Key", "k"], ["Accept", "*/*"]]) == [
            ["Authorization", "[redacted]"], ["X-Api-Key", "[redacted]"], ["Accept", "*/*"]]
    elif scenario == "sad":
        assert rt.redact_headers([["Cookie", "a=b"], ["Set-Cookie", "s=1"], ["Proxy-Authorization", "x"]]) == [
            ["Cookie", "[redacted]"], ["Set-Cookie", "[redacted]"], ["Proxy-Authorization", "[redacted]"]]
    else:
        # Other headers keep their value, minus anything that looks like a token.
        assert rt.redact_headers([["Referer", "https://x/?access_token=abc"]]) == [
            ["Referer", "https://x/?access_token=[redacted]"]]
        assert rt.redact_headers([]) == []


def test_redact_text(scenario):
    if scenario == "happy":
        text = '{"refreshToken": "r-1", "api_key": "k", "model": "m"}'
        assert rt.redact_text(text) == '{"refreshToken": "[redacted]", "api_key": "[redacted]", "model": "m"}'
        assert rt.redact_text("grant_type=x&code=abc&client_secret=s") == "grant_type=x&code=[redacted]&client_secret=[redacted]"
    elif scenario == "sad":
        token = "ghp_" + "Z" * 36
        assert token not in rt.redact_text(f"push with {token} now")
        key = "sk-" + "ant-api03-" + "abcdefghijklmnop"  # assembled, so that no scanner takes the file for a leak
        assert key not in rt.redact_text(f"key {key}")
        assert rt.redact_text("Authorization: Bearer abcdefghijkl") == "Authorization: Bearer [redacted]"
    else:
        assert rt.redact_text("") == "" and rt.redact_text("plain words") == "plain words"
        assert rt.redact_text('{"password": "a\\"b"}') == '{"password": "[redacted]"}'


def test_body_record(scenario):
    if scenario == "happy":
        assert rt.body_record(b'{"a": 1}', 8, [], 100) == {"size": 8, "truncated": False, "text": '{"a": 1}'}
        packed = gzip.compress(b"hello world")
        assert rt.body_record(packed, len(packed), [["Content-Encoding", "gzip"]], 100)["text"] == "hello world"
    elif scenario == "sad":
        record = rt.body_record(b"\x00\x01\xff", 3, [], 100)
        assert record["base64"] == base64.b64encode(b"\x00\x01\xff").decode()
        assert rt.body_record(b"xx", 2, [["Content-Encoding", "br"]], 100)["encoding"] == "br"
        assert rt.body_record(b"", 0, [], 100) is None
    else:
        # Cut in the middle of a character: the text ends before it; the size is the whole body's.
        record = rt.body_record("aé".encode()[:2], 3, [], 2)
        assert record == {"size": 3, "truncated": True, "text": "a"}


def test_pkt_lines(scenario):
    if scenario == "happy":
        assert rt.pkt_lines(b"0009hello0000PACK") == ([b"hello"], True)
    elif scenario == "sad":
        with pytest.raises(ValueError):
            rt.pkt_lines(b"zzzzhello")
        with pytest.raises(ValueError):
            rt.pkt_lines(b"0002")
    else:
        assert rt.pkt_lines(b"0009hel") == ([], False)
        assert rt.pkt_lines(b"0009hello") == ([b"hello"], False)
        assert rt.pkt_lines(b"0000") == ([], True)


def test_push_commands(scenario):
    first = f"{OID_A} {OID_B} refs/heads/main\0report-status side-band-64k\n".encode()
    if scenario == "happy":
        assert rt.push_commands([first, f"{'0' * 40} {OID_A} refs/tags/v1\n".encode()]) == (
            [(OID_A, OID_B, "refs/heads/main"), ("0" * 40, OID_A, "refs/tags/v1")], {"report-status", "side-band-64k"})
    elif scenario == "sad":
        for lines in ([], [b"push-cert\0report-status\n"], [f"{OID_A} xyz refs/heads/main\n".encode()]):
            with pytest.raises(ValueError):
                rt.push_commands(lines)
    else:
        assert rt.push_commands([b"shallow " + OID_A.encode(), first])[0] == [(OID_A, OID_B, "refs/heads/main")]


def test_advertised_refs(scenario):
    if scenario == "happy":
        body = (rt.pkt(b"# service=git-receive-pack\n") + b"0000" + rt.pkt(f"{OID_A} refs/heads/main\0caps\n".encode())
                + rt.pkt(f"{OID_B} refs/tags/v1\n".encode()) + b"0000")
        assert rt.advertised_refs(body) == {OID_A, OID_B}
    elif scenario == "sad":
        assert rt.advertised_refs(b"<html>not git</html>") == set()
    else:
        # An empty repository advertises the zero id.
        empty = rt.pkt(f"{'0' * 40} capabilities^{{}}\0report-status\n".encode()) + b"0000"
        assert rt.advertised_refs(empty) == set()


def test_pkt(scenario):
    if scenario == "happy":
        assert rt.pkt(b"hello") == b"0009hello"
    elif scenario == "sad":
        assert rt.pkt(b"") == b"0004"
    else:
        assert rt.pkt(b"x" * 65516)[:4] == b"fff0"


def test_push_rejection(scenario):
    commands = [(OID_A, OID_B, "refs/heads/main")]
    if scenario == "happy":
        response = rt.push_rejection(commands, "line one\nline two", {"report-status", "side-band-64k"})
        head, _, body = response.partition(b"\r\n\r\n")
        assert b"application/x-git-receive-pack-result" in head and b"Content-Length: %d" % len(body) in head
        assert b"\x02sandbox-ai: line one\n" in body and b"\x02sandbox-ai: line two\n" in body
        assert b"ng refs/heads/main refused by the sandbox-ai privacy check" in body and body.endswith(b"0000")
    elif scenario == "sad":
        body = rt.push_rejection(commands, "why", {"report-status"}).partition(b"\r\n\r\n")[2]
        assert body.startswith(rt.pkt(b"unpack ok\n")) and b"why" not in body  # no side band for messages
    else:
        body = rt.push_rejection(commands * 3, "m", {"side-band"}).partition(b"\r\n\r\n")[2]
        assert body.count(b"ng refs/heads/main") == 3


def test_parse_head(scenario):
    if scenario == "happy":
        assert rt.parse_head(b"GET /x HTTP/1.1\r\nHost: a\r\nX-Y:  z \r\n\r\n") == ("GET /x HTTP/1.1",
                                                                                 [["Host", "a"], ["X-Y", "z"]])
    elif scenario == "sad":
        for head in (b"GET / HTTP/1.1\r\nno colon\r\n\r\n", b"GET / HTTP/1.1\r\n Folded: x\r\n\r\n"):
            with pytest.raises(ValueError):
                rt.parse_head(head)
    else:
        assert rt.parse_head(b"HTTP/1.1 204 No Content\r\n\r\n") == ("HTTP/1.1 204 No Content", [])


def test_header(scenario):
    headers = [["Content-Length", "1"], ["X", "a"], ["x", "b"]]
    if scenario == "happy":
        assert rt.header(headers, "content-length") == "1"
    elif scenario == "sad":
        assert rt.header(headers, "missing") is None
    else:
        assert rt.header(headers, "x") == "b"  # the last one


def test_send_all(scenario):
    left, right = socket.socketpair()
    try:
        if scenario == "happy":
            rt.send_all(left, b"x" * 100000)
            data = b""
            while len(data) < 100000:
                data += right.recv(65536)
            assert data == b"x" * 100000
        elif scenario == "sad":
            right.close()
            with pytest.raises(OSError):
                for _ in range(100):
                    rt.send_all(left, b"x" * 65536)
        else:
            # A non-blocking socket with a full buffer waits instead of failing.
            left.setblocking(False)
            reader = threading.Thread(target=lambda: [right.recv(1 << 20) for _ in range(40)], daemon=True)
            reader.start()
            rt.send_all(left, b"y" * (2 << 20))
    finally:
        left.close()
        right.close()


def test_relay(scenario):
    a1, a2 = socket.socketpair()
    b1, b2 = socket.socketpair()
    counts = [0, 0]
    thread = threading.Thread(target=rt.relay, args=(a2, b1, counts), kwargs={"grace": 1}, daemon=True)
    thread.start()
    try:
        if scenario == "happy":
            a1.sendall(b"ping")
            assert b2.recv(10) == b"ping"
            b2.sendall(b"pong!")
            assert a1.recv(10) == b"pong!"
            a1.close()
            b2.close()
            thread.join(5)
            assert counts == [4, 5] and not thread.is_alive()
        elif scenario == "sad":
            # One side ends, the other stays quiet: the relay stops after the grace time.
            a1.close()
            thread.join(5)
            assert not thread.is_alive()
        else:
            # Half-close travels: the far side sees the end and still answers.
            a1.shutdown(socket.SHUT_WR)
            assert b2.recv(10) == b""
            b2.sendall(b"late")
            assert a1.recv(10) == b"late"
            b2.close()
            thread.join(5)
            assert counts == [0, 4]
    finally:
        for sock in (a1, b2, a2, b1):
            sock.close()


def test_request_framing(scenario):
    if scenario == "happy":
        assert rt.request_framing([["Content-Length", "12"]]) == ("length", 12)
        assert rt.request_framing([["Transfer-Encoding", "Chunked "]]) == ("chunked",)
    elif scenario == "sad":
        # Anything a server could frame differently is refused (request smuggling).
        for headers in ([["Content-Length", "-1"]], [["Transfer-Encoding", "gzip, chunked"]],
                        [["Transfer-Encoding", "chunked"], ["Content-Length", "5"]],
                        [["Content-Length", "5"], ["Content-Length", "6"]], [["Transfer-Encoding", "xchunked"]]):
            with pytest.raises(ValueError):
                rt.request_framing(headers)
    else:
        assert rt.request_framing([]) is None and rt.request_framing([["Content-Length", "0"]]) is None


def test_response_framing(scenario):
    if scenario == "happy":
        assert rt.response_framing("GET", 200, [["Content-Length", "3"]]) == ("length", 3)
        assert rt.response_framing("GET", 200, [["Transfer-Encoding", "chunked"]]) == ("chunked",)
    elif scenario == "sad":
        assert rt.response_framing("GET", 200, []) == ("close",)
    else:
        for method, status in (("HEAD", 200), ("GET", 204), ("GET", 304), ("GET", 101)):
            assert rt.response_framing(method, status, [["Content-Length", "9"]]) is None


def test_host_matches(scenario):
    if scenario == "happy":
        assert rt.host_matches("API.example.org.", ["api.example.org"])
        assert rt.host_matches("a.b.example.org", ["*.example.org"])
    elif scenario == "sad":
        assert not rt.host_matches("example.org", ["*.example.org"])
        assert not rt.host_matches("evilexample.org", ["*.example.org"])
    else:
        assert not rt.host_matches(None, ["*.example.org"]) and not rt.host_matches("x", [])


def test_drop_to_proxy_user(scenario, monkeypatch):
    calls = []
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(rt.os, name, lambda *args, n=name: calls.append((n, args)))
    libc = Mock()
    libc.prctl.return_value = -1 if scenario == "sad" else 0
    monkeypatch.setattr(rt.ctypes, "CDLL", lambda *a, **kw: libc)
    if scenario == "sad":
        with pytest.raises(OSError, match="no_new_privs"):
            rt.drop_to_proxy_user()
    else:
        rt.drop_to_proxy_user()
        # Groups first, then the group, the user last (afterwards nothing could be changed).
        assert calls == [("setgroups", ([],)), ("setresgid", (1, 1, 1)), ("setresuid", (1, 1, 1))]
        assert libc.prctl.call_args.args[:2] == (38, 1)


def test_proxy_listeners(scenario):
    if scenario == "happy":
        listeners = rt.proxy_listeners(0)
        try:
            assert listeners[0].getsockname()[0] == "127.0.0.1"
        finally:
            for sock in listeners:
                sock.close()
    elif scenario == "sad":
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        try:
            with pytest.raises(OSError):
                rt.proxy_listeners(taken.getsockname()[1])
        finally:
            taken.close()
    else:
        listeners = rt.proxy_listeners(0)
        try:
            assert all(sock.type == socket.SOCK_STREAM for sock in listeners) and 1 <= len(listeners) <= 2
        finally:
            for sock in listeners:
                sock.close()


@needs_openssl
def test_proxy_main(scenario, tmp_path, monkeypatch):
    proxy_dir, run = tmp_path / "proxy", tmp_path / "run"
    proxy_dir.mkdir()
    (proxy_dir / "proxy.json").write_text(json.dumps({"port": 0, "allow": {"all": True},
                                                      "push_check": scenario == "edge"}))
    monkeypatch.setattr(rt, "PROXY_DIR", proxy_dir)
    monkeypatch.setattr(rt, "PROXY_RUN", run)
    monkeypatch.setattr(rt, "CA_DIR", tmp_path / "ca")
    chowned, popen, real_popen = [], Mock(), subprocess.Popen
    monkeypatch.setattr(rt.os, "chown", lambda path, uid, gid: chowned.append((str(path), uid, gid)))
    # openssl really runs (through subprocess.run); only the checker process is recorded.
    monkeypatch.setattr(rt.subprocess, "Popen",
                        lambda command, *a, **kw: real_popen(command, *a, **kw) if command[0] == "openssl"
                        else popen(command, *a, **kw))
    monkeypatch.setattr(rt, "drop_to_proxy_user", Mock(side_effect=OSError("drop") if scenario == "sad" else None))
    monkeypatch.setattr(rt.ProxyServer, "wait", lambda self: 0)
    served = Mock()
    monkeypatch.setattr(rt.ProxyServer, "serve", served)
    ready_r, ready_w = os.pipe()
    log = os.open(tmp_path / "log", os.O_WRONLY | os.O_CREAT)
    try:
        if scenario == "sad":
            with pytest.raises(OSError, match="drop"):
                rt.proxy_main(log, ready_w)
            assert not served.called  # nothing is served with root's rights
            return
        assert rt.proxy_main(log, ready_w) == 0
        assert os.read(ready_r, 1) == b"1"
        assert (str(run), 1, 1) in chowned and (str(run / "ca.key"), 1, 1) in chowned
        assert json.loads((run / "allow.json").read_text()) == {"all": True}
        if scenario == "edge":
            command = popen.call_args.args[0]
            assert command[:len(rt.DROP_ALL)] == rt.DROP_ALL and command[-2:] == ["serve", str(proxy_dir / "push.json")]
            assert popen.call_args.kwargs["env"]["HOME"] == "/nonexistent"
        else:
            popen.assert_not_called()
    finally:
        os.close(ready_r)
        os.close(log)
        with contextlib.suppress(OSError):
            os.close(ready_w)
        for sock in served.call_args.args[0] if served.called else []:
            sock.close()


def test_start_proxy(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    trusted = Mock()
    monkeypatch.setattr(rt, "trust_session_ca", trusted)
    started = []

    def popen(command, pass_fds, **kwargs):
        log_fd, ready_fd = pass_fds
        started.append(command)
        process = Mock()
        process.poll.return_value = None if scenario != "sad" else 1
        if scenario == "happy":
            os.write(ready_fd, b"1")
        elif scenario == "edge":
            os.write(ready_fd, b"x")
        return process
    monkeypatch.setattr(rt.subprocess, "Popen", popen)
    monkeypatch.setattr(rt, "stop_process", Mock())
    if scenario == "happy":
        rt.start_proxy({"log": "connections-x.jsonl"})
        assert started[0][2:4] == ["/sandbox/runtime.py", "--internal-proxy"]
        assert (tmp_path / "connections-x.jsonl").exists() and trusted.called
        assert stat.S_IMODE((tmp_path / "connections-x.jsonl").stat().st_mode) == 0o600
    else:
        with pytest.raises(RuntimeError, match="did not start"):
            rt.start_proxy({"log": "connections-x.jsonl"})
        assert not trusted.called and rt.stop_process.called


def test_trust_session_ca(scenario, tmp_path):
    bundle = tmp_path / "certs" / "ca-certificates.crt"
    bundle.parent.mkdir()
    bundle.write_text("system")
    (tmp_path / "cert.pem").symlink_to(bundle)
    mounts = []
    if scenario == "happy":
        done = rt.trust_session_ca((str(bundle), str(tmp_path / "cert.pem")), mount=lambda s, t: mounts.append((s, t)))
        # A link to the same file is bound once.
        assert done == [str(bundle)] and mounts == [(str(rt.CA_DIR / "bundle.pem"), str(bundle))]
    elif scenario == "sad":
        def fail(source, target):
            raise OSError(1, "no")
        with pytest.raises(OSError):
            rt.trust_session_ca((str(bundle),), mount=fail)
    else:
        assert rt.trust_session_ca((str(tmp_path / "missing"),), mount=lambda s, t: mounts.append(t)) == []
        assert mounts == []


def test_runtime_rules(scenario):
    if scenario == "happy":
        script = rt.runtime_rules({"nets": {"192.0.2.0/24"}, "https": {"198.51.100.4/32"},
                                   "tcp": {("203.0.113.9", 5432)}})
        assert script.splitlines() == [
            "flush chain inet agent_sandbox runtime",
            "add rule inet agent_sandbox runtime ip daddr 192.0.2.0/24 accept",
            "add rule inet agent_sandbox runtime ip daddr 198.51.100.4/32 tcp dport 443 accept",
            "add rule inet agent_sandbox runtime ip daddr 203.0.113.9 tcp dport 5432 accept"]
    elif scenario == "sad":
        with pytest.raises(ValueError):
            rt.runtime_rules({"nets": {"not-an-address"}, "https": set(), "tcp": set()})
    else:
        assert rt.runtime_rules({"nets": set(), "https": {"2001:db8::/32"}, "tcp": set()}).splitlines()[1] == \
            "add rule inet agent_sandbox runtime ip6 daddr 2001:db8::/32 tcp dport 443 accept"


def test_runtime_change(scenario):
    empty = {"nets": set(), "https": set(), "tcp": set()}
    if scenario == "happy":
        state = rt.runtime_change(empty, {"op": "allow", "https": ["192.0.2.7"], "tcp": [["192.0.2.8", 22]]})
        assert state == {"nets": set(), "https": {"192.0.2.7/32"}, "tcp": {("192.0.2.8", 22)}}
        assert rt.runtime_change(state, {"op": "revoke", "https": ["192.0.2.7/32"]})["https"] == set()
    elif scenario == "sad":
        for request in ({"op": "drop"}, {"op": "allow", "nets": ["192.0.2.1; flush ruleset"]},
                        {"op": "allow", "tcp": [["192.0.2.1", 0]]}, {"op": "allow", "tcp": [["192.0.2.1", True]]},
                        {"op": "allow", "tcp": [["192.0.2.1", "22"]]}, {"op": "allow", "nets": ["fe80::1%eth0"]}):
            with pytest.raises(ValueError):
                rt.runtime_change(empty, request)
    else:
        # Revoking what was never allowed changes nothing; networks are normalised.
        assert rt.runtime_change(empty, {"op": "revoke", "nets": ["10.0.0.0/8"]}) == empty
        assert rt.runtime_change(empty, {"op": "allow", "nets": ["10.1.2.3/8"]})["nets"] == {"10.0.0.0/8"}


def test_merged_allow(scenario):
    base = {"all": False, "nets": ["192.0.2.0/24"], "https": ["198.51.100.1/32"], "tcp": [["203.0.113.1", 22]],
            "loopback": [3000]}
    empty = {"nets": set(), "https": set(), "tcp": set()}
    if scenario == "happy":
        merged = rt.merged_allow(base, {"nets": set(), "https": {"198.51.100.2/32"}, "tcp": {("203.0.113.2", 5432)}})
        assert merged["https"] == ["198.51.100.1/32", "198.51.100.2/32"]
        assert merged["tcp"] == [["203.0.113.1", 22], ["203.0.113.2", 5432]] and merged["loopback"] == [3000]
    elif scenario == "sad":
        assert rt.merged_allow({}, empty) == {"nets": [], "https": [], "tcp": [], "ssh": [], "ssh_tcp": []}
    else:
        assert rt.merged_allow(base, empty) == {**base, "ssh": [], "ssh_tcp": []} and base["nets"] == ["192.0.2.0/24"]
        # What `net allow` opened explicitly may also take SSH, as --allow and --allow-tcp at the start: a
        # range on any port, an IP:PORT on that port only.
        merged = rt.merged_allow(base, {"nets": {"10.0.0.0/8"}, "https": set(), "tcp": {("192.0.2.9", 22)}})
        assert merged["ssh"] == ["10.0.0.0/8"] and merged["ssh_tcp"] == [["192.0.2.9", 22]]


def test_control_server(scenario, tmp_path):
    socket_path, allow = tmp_path / "control.sock", tmp_path / "allow.json"
    applied, stop = [], threading.Event()
    apply = (lambda script: applied.append(script)) if scenario != "sad" else Mock(
        side_effect=subprocess.CalledProcessError(1, "nft"))
    network = rt.RuntimeNetwork({"loopback": [3000]}, allow if scenario != "edge" else None, apply)
    thread = threading.Thread(target=rt.control_server, args=(socket_path, network, stop), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    def ask(request):
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(socket_path))
            client.sendall(json.dumps(request).encode() + b"\n")
            return json.loads(client.makefile().readline())
    try:
        if scenario == "happy":
            answer = ask({"op": "allow", "https": ["192.0.2.7"]})
            assert answer == {"ok": True, "nets": [], "https": ["192.0.2.7/32"], "tcp": []}
            assert "tcp dport 443 accept" in applied[-1]
            assert json.loads(allow.read_text())["https"] == ["192.0.2.7/32"]
            assert ask({"op": "show"})["https"] == ["192.0.2.7/32"]
        elif scenario == "sad":
            # The firewall refused: nothing is recorded as allowed.
            answer = ask({"op": "allow", "https": ["192.0.2.7"]})
            assert not answer["ok"] and ask({"op": "show"})["https"] == [] and not allow.exists()
            assert not ask({"op": "allow", "nets": ["x"]})["ok"]
        else:
            assert ask({"op": "allow", "tcp": [["192.0.2.9", 22]]})["tcp"] == [["192.0.2.9", 22]]
            assert ask({"op": "revoke", "tcp": [["192.0.2.9", 22]]})["tcp"] == []
            assert applied[-1] == "flush chain inet agent_sandbox runtime\n"
    finally:
        stop.set()
        thread.join(5)


def test_proxy_enabled(scenario):
    if scenario == "happy":
        assert s.proxy_enabled(s.parse_args(["codex"]))
    elif scenario == "sad":
        assert not s.proxy_enabled(s.parse_args(["codex", "--no-proxy"]))
    else:
        assert not s.proxy_enabled(s.parse_args(["codex", "--offline"]))


def test_proxy_allow(scenario):
    hosts = {"api.openai.com": ["192.0.2.10"]}
    if scenario == "happy":
        a = s.parse_args(["codex", "--allow", "198.51.100.0/24", "--allow-tcp", "203.0.113.5:5432"])
        allow = s.proxy_allow(a, hosts)
        assert allow == {"all": False, "nets": ["198.51.100.0/24"], "https": ["192.0.2.10/32"],
                         "tcp": [["203.0.113.5", 5432]], "loopback": []}
    elif scenario == "sad":
        assert s.proxy_allow(s.parse_args(["codex"]), {}) == {"all": False, "nets": [], "https": [], "tcp": [],
                                                              "loopback": []}
    else:
        allow = s.proxy_allow(s.parse_args(["codex", "--egress-all-allow", "--allow-host-port", "3000"]), hosts)
        assert allow["all"] and allow["loopback"] == [3000]
        assert ["1.1.1.1", 53] in allow["tcp"]  # resolvers over TCP


def test_proxy_config(scenario):
    hosts = {"github.com": ["192.0.2.20"], "api.github.com": ["192.0.2.21"], "pypi.org": ["192.0.2.30"]}
    if scenario == "happy":
        config = s.proxy_config(s.parse_args(["codex", "--allow-tcp", "203.0.113.5:22"]), hosts, "connections-x.jsonl")
        assert config["ssh"] == {"block": True, "allowed": [], "allowed_tcp": [["203.0.113.5", 22]],
                                 "public_git": ["192.0.2.20", "192.0.2.21"]}
        assert config["push_check"] and config["log"] == "connections-x.jsonl" and config["body_limit"] == 65536
    elif scenario == "sad":
        config = s.proxy_config(s.parse_args(["codex", "--privacy-check", "off"]), hosts, "l")
        assert not config["ssh"]["block"] and not config["push_check"]
    else:
        config = s.proxy_config(s.parse_args(["codex", "--no-inspect", "Login.Example.org"]), {}, "l")
        assert config["no_inspect"] == ["login.example.org"] and config["port"] == s.PROXY_PORT
        # Approved "always" (remembered): reachable, but never over SSH, unlike --allow-tcp.
        a = s.parse_args(["codex", "--allow-tcp", "203.0.113.5:22"])
        a.allow_tcp.append("198.51.100.9:22")
        a.approved_tcp = ["198.51.100.9:22"]
        config = s.proxy_config(a, {}, "l")
        assert config["ssh"]["allowed_tcp"] == [["203.0.113.5", 22]]
        assert ["198.51.100.9", 22] in config["allow"]["tcp"]


def test_write_proxy_files(scenario, tmp_path):
    work = tmp_path / "app"
    work.mkdir()
    out = tmp_path / "proxy"
    out.mkdir()
    if scenario == "happy":
        a = s.parse_args(["codex", "--workspace", str(work)])
        s.write_proxy_files(a, out, {}, "connections-x.jsonl")
        assert sorted(p.name for p in out.iterdir()) == ["privacy.py", "proxy.json", "push.json"]
        push = json.loads((out / "push.json").read_text())
        assert push["mode"] == "public" and push["workspaces"][0]["path"] == "/work"
        assert any(p["id"] for p in push["workspaces"][0]["rules"]["pattern"])
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in out.iterdir())
    elif scenario == "sad":
        rules = tmp_path / "rules"
        rules.mkdir()
        (rules / "global.rules.json").write_text("{broken")
        with pytest.raises(ValueError, match="--privacy-rules"):
            s.write_proxy_files(s.parse_args(["codex", "--workspace", str(work), "--privacy-rules", str(rules)]),
                                out, {}, "l")
    else:
        s.write_proxy_files(s.parse_args(["codex", "--workspace", str(work), "--privacy-check", "off"]), out, {}, "l")
        assert [p.name for p in out.iterdir()] == ["proxy.json"]


def test_rotate_connection_logs(scenario, tmp_path):
    for index in range(3):
        (tmp_path / f"connections-2026110{index}-x.jsonl").write_bytes(b"x" * 100)
        os.utime(tmp_path / f"connections-2026110{index}-x.jsonl", (time.time() - 7200, time.time() - 7200))
    (tmp_path / "files.jsonl").write_bytes(b"y" * 1000)
    if scenario == "happy":
        assert s.rotate_connection_logs(tmp_path, limit=150) == ["connections-20261100-x.jsonl",
                                                                  "connections-20261101-x.jsonl"]
        assert (tmp_path / "files.jsonl").exists() and (tmp_path / "connections-20261102-x.jsonl").exists()
    elif scenario == "sad":
        assert s.rotate_connection_logs(tmp_path / "missing") == []
    else:
        assert s.rotate_connection_logs(tmp_path, limit=300) == []
        # A log written within the hour may belong to a sandbox that still runs: kept.
        os.utime(tmp_path / "connections-20261100-x.jsonl", None)
        assert s.rotate_connection_logs(tmp_path, limit=150) == ["connections-20261101-x.jsonl",
                                                                  "connections-20261102-x.jsonl"]


def test_connection_summary(scenario, tmp_path):
    log = tmp_path / "connections.jsonl"
    if scenario == "happy":
        log.write_text("\n".join(json.dumps(r) for r in [
            {"event": "http", "url": "https://a/"}, {"event": "denied"}, {"event": "tcp"},
            {"event": "http", "push": {"ok": False, "refs": ["refs/heads/main"]}},
            {"event": "tls-refused", "host": "pinned.example.org"}]) + "\nnot json\n")
        lines = s.connection_summary(log)
        assert lines[0].startswith("proxy: 2 HTTP request(s), 1 other connection(s), 1 refused")
        assert "refs/heads/main" in lines[1] and "--no-inspect pinned.example.org" in lines[2]
    elif scenario == "sad":
        assert s.connection_summary(tmp_path / "missing") == []
    else:
        log.write_text("")
        assert s.connection_summary(log) == [f"proxy: 0 HTTP request(s), 0 other connection(s), 0 refused; log: {log}"]


def test_runs_dir(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert s.runs_dir() == tmp_path / "sandbox-ai" / "runs"
    elif scenario == "sad":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        assert s.runs_dir() == tmp_path / "state" / "sandbox-ai" / "runs"
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert s.runs_dir() == Path.home() / ".local/state/sandbox-ai/runs"


def test_register_run(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "app")])
    s.validate(a)
    if scenario == "happy":
        directory = s.register_run(a, "claude-1234abcd", tmp_path / "session")
        record = json.loads((directory / "run.json").read_text())
        assert record["run"] == "claude-1234abcd" and record["workspace"] == str(tmp_path / "app")
        assert record["session_dir"] == str(tmp_path / "session") and record["proxy"]
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    elif scenario == "sad":
        # A run of that name that still lives (another process) stops the start; a dead one is replaced.
        directory = s.register_run(a, "session-x", tmp_path / "session")
        record = json.loads((directory / "run.json").read_text())
        (directory / "run.json").write_text(json.dumps({**record, "pid": os.getppid()}))
        with pytest.raises(ValueError, match="already running"):
            s.register_run(a, "session-x", tmp_path / "session")
        (directory / "run.json").write_text(json.dumps({**record, "pid": 2 ** 22 + 9}))
        (directory / "control.sock").write_text("stale")
        assert s.register_run(a, "session-x", tmp_path / "other") == directory and not (directory / "control.sock").exists()
    else:
        assert json.loads((s.register_run(a, "x", tmp_path) / "run.json").read_text())["session"] is None


def test_first_bytes_state(scenario):
    hello = client_hello("example.org")
    if scenario == "happy":
        for data in (hello, b"GET / HTTP/1.1\r\n", b"SSH-2.0-x", b"\x00\x01binary"):
            assert rt.first_bytes_state(data) == "complete", data
    elif scenario == "sad":
        # Pieces that would change meaning with the next byte: waited for, never classified early.
        for data in (b"", hello[:10], b"S", b"SS", b"SSH", b"POS", b"PRI"):
            assert rt.first_bytes_state(data) == "more", data
    else:
        assert rt.first_bytes_state(b"POSTXXXXXXX") == "complete"  # longer than any method: not HTTP
        assert rt.first_bytes_state(b"SS1") == "complete"  # (SSX could still begin a method)


def test_review_regressions_launcher(tmp_path, monkeypatch):
    """Findings of the review before 0.1.20, each kept closed."""
    # --mount never of device or kernel trees (Podman would keep device nodes usable).
    for source in ("/dev", "/proc/self", "/sys"):
        if Path(source).exists():
            with pytest.raises(ValueError, match="device and kernel trees"):
                s.parse_mount(f"{source}:/mnt/x:ro")
    # --no-inspect neither for public Git hosts nor for names below them.
    for value in ("github.com", "*.github.com", "www.github.com", "*.com"):
        with pytest.raises(ValueError, match="pass unchecked"):
            s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "w"), "--no-inspect", value]))
    s.validate(s.parse_args(["codex", "--workspace", str(tmp_path / "w"), "--no-inspect", "*.github.com",
                             "--privacy-check", "off"]))


@pytest.mark.parametrize("existing", [
    '[projects]\n"/work" = { trust_level = "untrusted" }\n',
    'projects."/work".trust_level = "untrusted"\n',
    '[ projects."/work" ]\ntrust_level = "untrusted"\n',
    'projects = { "/other" = { trust_level = "trusted" } }\n',
    'not = valid = toml\n',
])
def test_codex_trust_never_breaks_config_toml(tmp_path, existing):
    toml = s.load_privacy_module()._toml_module()
    if toml is None:
        pytest.skip("no TOML parser (Python < 3.11 without tomli)")
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(existing)
    s.codex_trusted_workspaces(s.parse_args(["codex", "--workspace", str(tmp_path / "app")]), home)
    after = (home / ".codex/config.toml").read_text()
    if existing.startswith("not"):
        assert after == existing
    else:
        document = toml.loads(after)  # still valid
        assert "/work" in document["projects"] or after == existing
        if "untrusted" in existing:
            assert after == existing  # the decision stored there is kept


def test_new_device_refusals(fake_dev):
    for name in ("fb0", "usbmon0", "snapshot", "iommu", "usb/hiddev0"):
        (fake_dev / name).parent.mkdir(parents=True, exist_ok=True)
        (fake_dev / name).write_text("")
        with pytest.raises(ValueError, match="refused"):
            s.parse_device(str(fake_dev / name))


def test_long_option(scenario):
    actions = s.build_parser()._option_string_actions
    if scenario == "happy":
        assert s.long_option(actions["-D"]) == "--dangerously-skip-permissions"
    elif scenario == "sad":
        assert s.long_option(actions["-h"]) == "--help"
    else:
        assert s.long_option(actions["-gl"]) == "--allow-gitlab"


def test_canonical_options(scenario):
    if scenario == "happy":
        assert s.canonical_options(["claude", "-Dn", "-w/tmp/a", "-gl", "-S", "/x", "-V"]) == [
            "claude", "--dangerously-skip-permissions", "--dry-run", "--workspace", "/tmp/a", "--allow-gitlab",
            "--state-dir", "/x", "--version"]
    elif scenario == "sad":
        # Values are never read as options; unknown tokens stay as they are for argparse to refuse.
        assert s.canonical_options(["--env", "-D", "-e", "-S", "-jz", "--", "-D"]) == [
            "--env", "-D", "--env", "-S", "-jz", "--", "-D"]
    else:
        assert s.canonical_options(["--mount=a:/mnt/a:ro", "-d/dev/kvm", "-x", "*.example.org"]) == [
            "--mount=a:/mnt/a:ro", "--device", "/dev/kvm", "--no-inspect", "*.example.org"]


def test_short_forms_cover_every_option_and_reach_every_check(tmp_path):
    parser = s.build_parser()
    visible = [a for a in parser._actions if a.option_strings and a.help != argparse.SUPPRESS]
    assert all(any(not o.startswith("--") for o in a.option_strings) for a in visible)
    shorts = [o for a in parser._actions for o in a.option_strings if not o.startswith("--")]
    assert len(shorts) == len(set(shorts))
    # A short form in a project file is checked like the long one.
    work = tmp_path / "w"
    work.mkdir()
    config = work / s.CONFIG_NAME
    config.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["-S/tmp/x"]}))
    s.trust_config(config)
    _, document = s.load_project_config(s.parse_args(["codex", "--workspace", str(work)]))
    assert document["options"] == ["--state-dir", "/tmp/x"]
    config.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["-s", "x"]}))  # --session: forbidden
    s.trust_config(config)
    with pytest.raises(ValueError, match="not allowed in a project configuration"):
        s.load_project_config(s.parse_args(["codex", "--workspace", str(work)]))
    mcp = s.load_mcp_module()
    with pytest.raises(ValueError, match="not allowed for an MCP server"):
        mcp.validated({"x": {"command": ["true"], "options": ["-S/tmp/state"]}}, "test")


def test_short_forms_like_argparse_reads_them():
    assert s.canonical_options(["-Dw", "/tmp/a", "-w=/tmp/b", "-pr=/rules", "-ne", "A"]) == [
        "--dangerously-skip-permissions", "--workspace", "/tmp/a", "--workspace", "/tmp/b", "--privacy-rules",
        "/rules", "--dry-run", "--env", "A"]
    wizard = s.load_wizard_module() if hasattr(s, "load_wizard_module") else None
    spec = importlib.util.spec_from_file_location("wizard_short", ROOT / "sandbox/wizard.py")
    wizard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wizard)
    with pytest.raises(ValueError, match="Not allowed in a project configuration"):
        wizard.check_free_options("codex", "-s name")
    assert wizard.check_free_options("codex", "-g -A example.org") == ["--allow-github", "--allow-host", "example.org"]


# --- resource limits ------------------------------------------------------------------------------


def test_resource_limits(scenario, monkeypatch):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    if scenario == "happy":
        assert s.resource_limits(s.parse_args(["codex"])) == {"memory": "8g", "cpus": "4", "pids": 4096}
        assert s.resource_limits(s.parse_args(["codex", "--memory", "2G", "--cpus", "1.5", "--pids", "512"])) == {
            "memory": "2g", "cpus": "1.5", "pids": 512}
    elif scenario == "sad":
        for args in (["--memory", "lots"], ["--memory", "0g"], ["--cpus", "0"], ["--cpus", "-1"], ["--cpus", "99"],
                     ["--pids", "3"], ["--pids", "x"]):
            with pytest.raises(ValueError):
                s.resource_limits(s.parse_args(["codex", *args]))
    else:
        assert s.resource_limits(s.parse_args(["codex", "--memory", "none", "--cpus", "none", "--pids", "none"])) == {}
        monkeypatch.setattr(s.os, "cpu_count", lambda: 1)
        assert s.resource_limits(s.parse_args(["codex"]))["cpus"] == "1"  # at least one CPU


def test_delegated_controllers(scenario, tmp_path):
    if scenario == "happy":
        (tmp_path / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
        assert s.delegated_controllers(tmp_path / "cgroup.controllers") == {"cpuset", "cpu", "io", "memory", "pids"}
    elif scenario == "sad":
        assert s.delegated_controllers(tmp_path / "missing") is None  # unknown: limits are passed on
    else:
        (tmp_path / "cgroup.controllers").write_text("")
        assert s.delegated_controllers(tmp_path / "cgroup.controllers") == set()


def test_enforced_limits(scenario, monkeypatch):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(s, "user_manager_running", lambda: True)
    a = s.parse_args(["codex"])
    if scenario == "happy":
        monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"cpu", "memory", "pids"})
        assert s.enforced_limits(a) == ({"memory": "8g", "cpus": "4", "pids": 4096}, [])
    elif scenario == "sad":
        # cpu not delegated to the user (older systemd): that limit is left out, with a note.
        monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"memory", "pids"})
        limits, notes = s.enforced_limits(a)
        assert limits == {"memory": "8g", "pids": 4096} and "cpu" in notes[0] and "--cpus" in notes[0]
    else:
        monkeypatch.setattr(s, "delegated_controllers", lambda path=None: None)
        assert s.enforced_limits(a)[0] == {"memory": "8g", "cpus": "4", "pids": 4096}
        remote = s.parse_args(["codex", "--podman-url", "unix:///x.sock"])
        monkeypatch.setattr(s, "delegated_controllers", lambda path=None: set())
        assert s.enforced_limits(remote)[0] == {"memory": "8g", "cpus": "4", "pids": 4096}  # remote: not ours to check
        # No systemd user session: rootless Podman uses cgroupfs and cannot set limits at all.
        monkeypatch.setattr(s, "user_manager_running", lambda: False)
        limits, notes = s.enforced_limits(a)
        assert limits == {} and "linger" in notes[0]


def test_limits_prefix(scenario, monkeypatch):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(s, "user_manager_running", lambda: True)
    monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"cpu", "memory", "pids"})
    a = s.parse_args(["codex", "--backend", "bwrap"])
    if scenario == "happy":
        monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(s, "user_manager_running", lambda: True)
        assert s.limits_prefix(a) == ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=8G",
                                      "-p", "MemorySwapMax=8G", "-p", "CPUQuota=400%", "-p", "TasksMax=4096", "--"]
    elif scenario == "sad":
        monkeypatch.setattr(s.shutil, "which", lambda name: None)
        assert s.limits_prefix(a) == []
    else:
        assert s.limits_prefix(s.parse_args(["codex", "--backend", "bwrap", "--memory", "none", "--cpus", "none",
                                             "--pids", "none"])) == []


def test_user_manager_running(scenario, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(s, "Path", lambda value: seen.append(value) or (tmp_path / "private" if scenario == "happy"
                                                                       else tmp_path / "missing"))
    if scenario == "happy":
        (tmp_path / "private").write_text("")
        assert s.user_manager_running()
    elif scenario == "sad":
        assert not s.user_manager_running()
    else:
        s.user_manager_running()
        assert seen == [f"/run/user/{os.getuid()}/bus"]


def test_podman_run_carries_the_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(s.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(s, "user_manager_running", lambda: True)
    monkeypatch.setattr(s, "delegated_controllers", lambda path=None: {"memory", "pids"})
    _, run = s.pod_commands(s.parse_args(["codex", "--workspace", str(tmp_path), "--memory", "2g"]), "p",
                            tmp_path / "session", tmp_path / "home")
    assert run[run.index("--memory") + 1] == "2g" and run[run.index("--pids-limit") + 1] == "4096"
    assert "--cpus" not in run  # cpu not delegated: left out, not failing the start


def test_user_bus_environment(scenario, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/elsewhere")
    env = s.user_bus_environment()
    if scenario == "happy":
        assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    elif scenario == "sad":
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path=/run/user/{os.getuid()}/bus"
    else:
        assert env["PATH"] == os.environ["PATH"] and os.environ["XDG_RUNTIME_DIR"] == "/tmp/elsewhere"


# --- approvals ------------------------------------------------------------------------------------


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def answer_file(directory, decision, scope="once", **extra):
    """Answer the first open question in DIRECTORY like sandbox-ai approvals does."""
    wait_for(lambda: list(directory.glob("*.json")))
    question = json.loads(sorted(directory.glob("*.json"))[0].read_text())
    (directory / f"{question['id']}.answer").write_text(json.dumps({"decision": decision, "scope": scope, **extra}))
    return question


def test_approval_broker_and_runtime_network(tmp_path):
    rules = []
    network = rt.RuntimeNetwork({"https": []}, tmp_path / "allow.json", rules.append)
    broker = rt.ApprovalBroker(tmp_path / "approvals", network, timeout=3, poll=0.02)
    results = []
    thread = threading.Thread(target=lambda: results.append(broker.ask("connect", {
        "destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443, "host": "api.example.org"})))
    thread.start()
    question = answer_file(tmp_path / "approvals", "allow", "run")
    thread.join(5)
    assert question["kind"] == "connect" and question["host"] == "api.example.org" and question["timeout"] == 3
    assert results[0]["decision"] == "allow" and results[0]["scope"] == "run"
    # Applied to the firewall and the proxy's allow-list before the answer; remembered for the run.
    assert "ip daddr 192.0.2.7 tcp dport 8443 accept" in rules[-1]
    assert json.loads((tmp_path / "allow.json").read_text())["tcp"] == [["192.0.2.7", 8443]]
    assert broker.ask("connect", {"destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443})["scope"] == "run"
    assert json.loads((tmp_path / "approvals" / f"{question['id']}.done").read_text())["decision"] == "allow"
    assert stat.S_IMODE((tmp_path / "approvals" / f"{question['id']}.json").stat().st_mode) == 0o600


def test_approval_broker_once_timeout_and_dns(tmp_path, monkeypatch):
    rules = []
    network = rt.RuntimeNetwork({}, None, rules.append)
    timers = []
    monkeypatch.setattr(network, "allow_once", lambda address, port, timer=None: (
        rt.RuntimeNetwork.allow_once(network, address, port, timer=lambda s, f: timers.append(f) or Mock())))
    broker = rt.ApprovalBroker(tmp_path / "a", network, timeout=1, poll=0.02)
    results = []
    thread = threading.Thread(target=lambda: results.append(broker.ask("connect", {
        "destination": "198.51.100.1:22", "address": "198.51.100.1", "port": 22})))
    thread.start()
    answer_file(tmp_path / "a", "allow", "once")
    thread.join(5)
    assert results[0]["decision"] == "allow" and "198.51.100.1 tcp dport 22" in rules[-1]
    timers[0]()  # the once-rule expires
    assert "198.51.100.1" not in rules[-1]
    # Unanswered: refused after the timeout and not asked again during the run.
    started = time.monotonic()
    denied = broker.ask("connect", {"destination": "203.0.113.9:443", "address": "203.0.113.9", "port": 443})
    assert denied["decision"] == "deny" and "no answer" in denied["reason"] and time.monotonic() - started < 4
    assert broker.ask("connect", {"destination": "203.0.113.9:443", "address": "203.0.113.9", "port": 443}) is denied or \
        broker.ask("connect", {"destination": "203.0.113.9:443", "address": "203.0.113.9", "port": 443})["decision"] == "deny"
    # A lookup is filed without waiting; its answer brings the addresses the host resolved.
    assert broker.ask("dns", {"name": "pypi.org"}, wait=False) is None
    files = [p for p in (tmp_path / "a").glob("*.json") if json.loads(p.read_text())["kind"] == "dns"]
    (tmp_path / "a" / f"{files[0].stem}.answer").write_text(json.dumps(
        {"decision": "allow", "scope": "run", "addresses": ["151.101.0.223", "not-an-address"]}))
    wait_for(lambda: (tmp_path / "a" / f"{files[0].stem}.done").exists())
    done = json.loads((tmp_path / "a" / f"{files[0].stem}.done").read_text())
    assert done["decision"] == "deny" and "could not be applied" in done["reason"]  # a bad address refuses all


def test_serve_asks(scenario, tmp_path):
    proxy_end, supervisor_end = socket.socketpair()
    if scenario == "edge":
        # Lines that are not questions are skipped; the channel keeps working.
        proxy_end.sendall(b"not json\n[1, 2]\n")
    asked = []

    class Broker:
        def ask(self, kind, details):
            asked.append((kind, details))
            return {"decision": "allow" if details.get("port") == 443 else "deny", "scope": "run", "reason": "answered"}
    threading.Thread(target=rt.serve_asks, args=(supervisor_end, Broker()), daemon=True).start()
    channel = rt.AskChannel(proxy_end, timeout=5)
    results = {}
    threads = [threading.Thread(target=lambda p=p: results.__setitem__(p, channel("connect", {"port": p})))
               for p in (443, 22)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert results[443]["decision"] == "allow" and results[22]["decision"] == "deny"
    assert sorted(d["port"] for _, d in asked) == [22, 443]
    if scenario == "sad":
        assert channel("other", {"port": 443})["decision"] == "deny"  # an unknown kind is never allowed
        assert len(asked) == 2  # and never reaches the broker
    proxy_end.close()


def test_load_approvals_module(scenario):
    module = s.load_approvals_module()
    if scenario == "happy":
        assert callable(module.answer) and callable(module.watch)
    elif scenario == "sad":
        assert module.ID.fullmatch("../x") is None
    else:
        assert module is not s.load_approvals_module()  # loaded fresh, like the other helpers


def test_asking(scenario):
    if scenario == "happy":
        assert s.asking(s.parse_args(["codex"]))
    elif scenario == "sad":
        assert not s.asking(s.parse_args(["codex", "--ask", "off"])) and not s.asking(s.parse_args(["codex", "-X"]))
    else:
        assert not s.asking(s.parse_args(["codex", "--probe"])) and not s.asking(s.parse_args(["codex", "--offline"]))


def test_apply_remembered_approvals(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    work = tmp_path / "work"
    work.mkdir()
    approvals = s.load_approvals_module()
    a = s.parse_args(["codex", "--workspace", str(work)])
    if scenario == "happy":
        approvals.remember(work, host="pypi.org")
        approvals.remember(work, tcp="192.0.2.5:5432")
        notes = s.apply_remembered_approvals(a)
        assert a.allow_host == ["pypi.org"] and a.allow_tcp == ["192.0.2.5:5432"] and "approved" in notes[0]
        s.validate(a)
    elif scenario == "sad":
        assert s.apply_remembered_approvals(a) == [] and a.allow_host == []
    else:
        # Another project's approvals do not apply; damaged entries are ignored.
        approvals.remember(tmp_path / "other", host="pypi.org")
        (tmp_path / "config/sandbox-ai/approved.json").write_text(json.dumps(
            {str(work.resolve()): {"hosts": ["bad host;"], "tcp": ["x"]}, **json.loads(
                (tmp_path / "config/sandbox-ai/approved.json").read_text())}))
        assert s.apply_remembered_approvals(a) == [] and a.allow_host == []


# --- telemetry: model usage, credentials, processes, MCP tool calls -------------------------------


def test_usage_scanner_reads_streams_and_bodies():
    anthropic = rt.UsageScanner()
    for chunk in (b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5",'
                  b'"usage":{"input_tokens":1200,"cache_read_input_tokens":800,"output_tokens":1}}}\n\n',
                  b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_t',
                  b'okens":345}}\n\n'):
        anthropic.feed(chunk)
    assert anthropic.result() == {"model": "claude-opus-5-5", "usage": {
        "input_tokens": 1200, "cache_read_input_tokens": 800, "output_tokens": 345}}
    openai = rt.UsageScanner()
    openai.feed(json.dumps({"model": "gpt-5.1", "usage": {"input_tokens": 50, "output_tokens": 7,
                                                           "total_tokens": 57}}).encode())
    assert openai.result() == {"model": "gpt-5.1", "usage": {"input_tokens": 50, "output_tokens": 7, "total_tokens": 57}}
    assert rt.UsageScanner().result() is None
    noisy = rt.UsageScanner()
    noisy.feed(b"data: [DONE]\n\ndata: {not json}\n\n" + b"x" * 100)
    assert noisy.result() is None


def test_is_model_request(scenario):
    if scenario == "happy":
        assert rt.is_model_request("/v1/messages?beta=true") and rt.is_model_request("/v1/responses")
    elif scenario == "sad":
        assert not rt.is_model_request("/v1/models") and not rt.is_model_request("/simple/httpx/")
    else:
        assert rt.is_model_request("/v1/chat/completions") and rt.is_model_request("/backend-api/codex/responses")


def test_credential_headers(scenario):
    if scenario == "happy":
        assert rt.credential_headers([["Authorization", "Bearer x"], ["x-api-key", "k"], ["Accept", "*/*"]]) == [
            "authorization", "x-api-key"]
    elif scenario == "sad":
        assert rt.credential_headers([]) == []
    else:
        assert rt.credential_headers([["Cookie", "a"], ["cookie", "b"]]) == ["cookie"]


def test_tool_call_record(scenario):
    if scenario == "happy":
        record = rt.tool_call_record(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "web_search", "arguments": {"query": "x", "api_key": "secret-value"}}}).encode())
        assert record["tool"] == "web_search" and '"api_key": "[redacted]"' in record["arguments"]
    elif scenario == "sad":
        assert rt.tool_call_record(b"not json") is None
        assert rt.tool_call_record(b'{"method": "tools/list"}') is None
    else:
        record = rt.tool_call_record(json.dumps({"method": "tools/call", "params": {
            "name": "x", "arguments": {f"text{i}": "y" * 5000 for i in range(10)}}}).encode())
        assert len(record["arguments"]) == 2003 and record["arguments"].endswith("...")


def test_process_logger(scenario, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "LOG_DIR", tmp_path / "log")
    (tmp_path / "log").mkdir()
    proc = tmp_path / "proc"

    def process(pid, argv, start="100", ppid=1):
        (proc / str(pid)).mkdir(parents=True, exist_ok=True)
        (proc / str(pid) / "stat").write_text(f"{pid} (x y) S {ppid} " + " ".join(["0"] * 16) + f" {start} 0 0")
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    process(7, [b"git", b"push", b"https://x-access-token:ghp_" + b"A" * 36 + b"@github.com/a/b"])
    if scenario == "edge":
        (proc / "self").mkdir(parents=True)  # not a process
        process(8, [])  # a kernel thread: no command line

    class Stop:
        calls = 0

        def wait(self, interval):
            Stop.calls += 1
            if Stop.calls == 2 and scenario == "happy":
                process(9, [b"python3", b"-c", b"pass"], ppid=7)
            return Stop.calls > 3
    rt.process_logger(Stop(), proc=proc)
    records = [json.loads(line) for line in (tmp_path / "log/processes.jsonl").read_text().splitlines()]
    if scenario == "sad":
        assert len(records) == 1 and "ghp_" not in records[0]["command"]  # once, redacted
    elif scenario == "happy":
        assert [(r["pid"], r["ppid"]) for r in records] == [(7, 1), (9, 7)]
    else:
        assert [r["pid"] for r in records] == [7]


def test_load_secret_store_module(scenario):
    module = s.load_secret_store_module()
    if scenario == "happy":
        assert module.check_name("GH_PAT") == "GH_PAT"
    elif scenario == "sad":
        with pytest.raises(ValueError):
            module.check_name("../etc")
    else:
        assert module.NAME.fullmatch("_x") and not module.NAME.fullmatch("x" * 65)


def test_secret_environment(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    store = s.load_secret_store_module().FileStore()
    store.set("KEY", "value")
    if scenario == "happy":
        assert s.secret_environment(s.parse_args(["codex", "--secret-env", "KEY", "--secret-env", "OTHER=KEY"])) == {
            "KEY": "value", "OTHER": "value"}
    elif scenario == "sad":
        with pytest.raises(ValueError, match="no secret"):
            s.secret_environment(s.parse_args(["codex", "--secret-env", "NOPE"]))
    else:
        assert s.secret_environment(s.parse_args(["codex"])) == {}


def test_open_request(scenario, tmp_path):
    import io
    out = io.StringIO()
    if scenario == "happy":
        short = Path(tempfile.mkdtemp(prefix="o-", dir="/tmp"))
        server = rt.listen_unix(short / "s.sock")

        def answer():
            connection, _ = server.accept()
            with connection:
                assert json.loads(connection.makefile("r").readline()) == {"url": "https://example.org/x"}
                connection.sendall(b'{"ok": true, "message": "asked"}\n')
        thread = threading.Thread(target=answer)
        thread.start()
        assert rt.open_request(["--new-window", "https://example.org/x"], str(short / "s.sock"), out) == 0
        thread.join(5)
        assert out.getvalue().strip() == "asked"
    elif scenario == "sad":
        assert rt.open_request(["https://example.org"], str(tmp_path / "missing.sock"), out) == 1
        assert "open it yourself" in out.getvalue()
    else:
        assert rt.open_request(["https://a", "https://b"], "/x", out) == 1
        assert rt.open_request(["ftp://example.org"], "/x", out) == 1


def test_write_browser_shims(scenario, tmp_path):
    directory = tmp_path / "bin"
    if scenario == "sad":
        directory.write_text("in the way")
        with pytest.raises(OSError):
            s.write_browser_shims(directory)
        return
    s.write_browser_shims(directory)
    if scenario == "happy":
        assert sorted(p.name for p in directory.iterdir()) == sorted(s.BROWSER_SHIMS)
        assert (directory / "xdg-open").read_text().endswith('--internal-open "$@"\n')
    else:
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o755 for p in directory.iterdir())
        s.write_browser_shims(directory)  # again: unchanged


def test_browser_bindings_and_environment(tmp_path):
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "w")])
    a.browser_dir = tmp_path / "run.browser"
    _, run = s.pod_commands(a, "p", tmp_path / "session", tmp_path / "home")
    assert f"type=bind,src={a.browser_dir},dst=/sandbox/browser,ro,nodev" in run
    cmd = s.bwrap_command(a, tmp_path, tmp_path)
    assert ["--ro-bind", str(a.browser_dir), "/sandbox/browser"] == cmd[cmd.index(str(a.browser_dir)) - 1:cmd.index(str(a.browser_dir)) + 2]
    env = s.agent_environment(a)
    assert env["BROWSER"] == "/sandbox/session/bin/xdg-open" and env["PATH"].startswith("/sandbox/session/bin:")
    assert "BROWSER" not in s.agent_environment(s.parse_args(["claude", "--ask", "off"]))


def test_question_name(scenario):
    if scenario == "happy":
        assert rt.question_name("API.Example.org.") == "api.example.org"
        assert rt.question_name("xn--bcher-kva.example") == "xn--bcher-kva.example"
    elif scenario == "sad":
        for bad in ("pypi.org\n1-1-1  x", "a b", "evil\x1b[8m.org", "b\u00fccher.example", "", "-x.org", "a..b"):
            assert rt.question_name(bad) is None, bad
    else:
        assert rt.question_name(None) is None and rt.question_name("a" * 254) is None
        assert rt.question_name("localhost") == "localhost"


def test_printable_text(scenario):
    if scenario == "happy":
        assert rt.printable_text("https://github.com/a/b") == "https://github.com/a/b"
    elif scenario == "sad":
        assert rt.printable_text("x\n\x1b[31m\u202e") == "x%0A%1B[31m%E2%80%AE"
        assert rt.printable_text("100%") == "100%25"  # unambiguous
    else:
        assert rt.printable_text("a" * 10, limit=4) == "aaaa" and rt.printable_text("") == ""


def test_write_new(scenario, tmp_path):
    target = tmp_path / "allow.json"
    if scenario == "happy":
        rt.write_new(target, "{}", 0o644)
        assert target.read_text() == "{}" and stat.S_IMODE(target.stat().st_mode) == 0o644
        rt.write_new(target, "[]", 0o600)
        assert target.read_text() == "[]" and stat.S_IMODE(target.stat().st_mode) == 0o600
    elif scenario == "sad":
        # A planted symlink at any temporary name is removed, never followed.
        victim = tmp_path / "victim"
        victim.write_text("keep")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}")
        temporary.symlink_to(victim)
        rt.write_new(target, "new", 0o644)
        assert victim.read_text() == "keep" and target.read_text() == "new" and not target.is_symlink()
    else:
        with pytest.raises(OSError):
            rt.write_new(tmp_path / "missing" / "x.json", "{}", 0o644)
        assert not (tmp_path / "missing").exists()


def test_review_findings_after_0_1_20(tmp_path, monkeypatch):
    """Findings of the review of approvals, secrets and browser requests, each kept closed."""
    rules = []
    network = rt.RuntimeNetwork({}, tmp_path / "allow.json", rules.append)
    # Approved destinations never take SSH; what the human allows with sandbox-ai net does.
    network.change({"op": "allow", "tcp": [["192.0.2.9", 443]]}, ssh=False)
    assert json.loads((tmp_path / "allow.json").read_text())["ssh_tcp"] == []
    network.change({"op": "allow", "tcp": [["192.0.2.9", 443]]})
    assert json.loads((tmp_path / "allow.json").read_text())["ssh_tcp"] == [["192.0.2.9", 443]]
    network.change({"op": "allow", "tcp": [["192.0.2.10", 22]]}, ssh=False)
    assert json.loads((tmp_path / "allow.json").read_text())["ssh_tcp"] == [["192.0.2.9", 443]]
    # IDs carry a random part: two sandboxes asking in the same second do not collide.
    one = rt.ApprovalBroker(tmp_path / "a1", network, timeout=0.2, poll=0.02)
    two = rt.ApprovalBroker(tmp_path / "a2", network, timeout=0.2, poll=0.02)
    one.ask("dns", {"name": "a.example"}, wait=False)
    two.ask("dns", {"name": "a.example"}, wait=False)
    ids = [json.loads(next((tmp_path / d).glob("*.json")).read_text())["id"] for d in ("a1", "a2")]
    assert ids[0] != ids[1]
    # A deny counts for the run (no re-asking to wear the human down); "once" may be asked again.
    broker = rt.ApprovalBroker(tmp_path / "a3", network, timeout=3, poll=0.02)
    details = {"destination": "198.51.100.1:443", "address": "198.51.100.1", "port": 443}
    results = []
    thread = threading.Thread(target=lambda: results.append(broker.ask("connect", details)))
    thread.start()
    answer_file(tmp_path / "a3", "deny", "run")
    thread.join(5)
    assert results[0]["decision"] == "deny"
    assert broker.ask("connect", details)["decision"] == "deny" and len(list((tmp_path / "a3").glob("*.json"))) == 1
    # Open questions are capped; beyond that the agent is refused without asking.
    capped = rt.ApprovalBroker(tmp_path / "a4", network, timeout=5, poll=0.05)
    for n in range(rt.OPEN_QUESTIONS):
        capped.ask("dns", {"name": f"n{n}.example"}, wait=False)
    assert capped.ask("dns", {"name": "more.example"}, wait=False)["reason"] == "too many questions"
    assert len(list((tmp_path / "a4").glob("*.json"))) == rt.OPEN_QUESTIONS
    # A push answer covers the same warnings only; "always" is the run at most.
    assert rt.ApprovalBroker.key("push", {"url": "u", "message": "a"}) != rt.ApprovalBroker.key("push", {"url": "u", "message": "b"})
    # A name the agent controls opens only public addresses.
    broker._apply("dns", {"name": "x.example"}, {"addresses": ["10.0.0.5", "127.0.0.1", "151.101.0.223"], "scope": "run"})
    assert json.loads((tmp_path / "allow.json").read_text())["https"] == ["151.101.0.223/32"]
    # Command lines: URL passwords, secret-named flags, curl -u.
    for text in ("git clone https://oauth2:s3cr3t-value@git.example/x", "tool --api-key s3cr3t-value",
                 "tool --password=s3cr3t-value", "curl -u admin:s3cr3t-value https://x"):
        assert "s3cr3t-value" not in rt.redact_text(text), text
    assert rt.redact_text("ls -la --color=auto /work") == "ls -la --color=auto /work"


def test_remembered_approvals_inside_a_workspace_are_not_read(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / ".config/sandbox-ai").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(work / ".config"))
    (work / ".config/sandbox-ai/approved.json").write_text(json.dumps({str(work.resolve()): {"hosts": ["evil.example"]}}))
    a = s.parse_args(["claude", "--workspace", str(work)])
    notes = s.apply_remembered_approvals(a)
    assert "evil.example" not in a.allow_host and "not read" in notes[0]


def test_firewall_accepts_what_it_redirects_to_the_proxy():
    """Found by the e2e run on a real host: after the NAT redirect the filter hook still sees the original
    output interface, so the redirected connection must be accepted by destination, before any drop."""
    rules = s.firewall_rules([], ["192.0.2.1"], proxy={"port": 15001, "loopback": []})
    output = rules.split(" chain output ")[1].split("\n }")[0].splitlines()
    accepts = [i for i, line in enumerate(output) if "ct status dnat" in line]
    assert [output[i].strip() for i in accepts] == ["ct status dnat ip daddr 127.0.0.1 tcp dport 15001 accept",
                                                    "ct status dnat ip6 daddr ::1 tcp dport 15001 accept"]
    assert accepts[-1] < next(i for i, line in enumerate(output) if "jump runtime" in line)
    assert "ct status dnat" not in s.firewall_rules([], ["192.0.2.1"])  # without the proxy: no such door


def test_stdin_is_pipe(scenario, tmp_path):
    if scenario == "happy":
        r, w = os.pipe()
        try:
            assert rt.stdin_is_pipe(r)
        finally:
            os.close(r), os.close(w)
    elif scenario == "sad":
        with open(tmp_path / "f", "w") as stream:
            assert not rt.stdin_is_pipe(stream.fileno())  # /dev/null, a file, a terminal: never exits on its own
    else:
        assert not rt.stdin_is_pipe(987654)


def test_wait_for_unix_listener(scenario, tmp_path):
    import tempfile as tempfile_module
    path = tmp_path / "s.sock"
    errors = tempfile_module.TemporaryFile()
    if scenario == "happy":
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(1)
        rt.wait_for_unix_listener(path, Mock(poll=Mock(return_value=None)), errors, "x11vnc", timeout=5)
        server.close()
    elif scenario == "sad":
        errors.write(b"x11vnc: cannot open display\n")
        with pytest.raises(RuntimeError, match="exited with status 1: x11vnc: cannot open display"):
            rt.wait_for_unix_listener(path, Mock(poll=Mock(return_value=1)), errors, "x11vnc", timeout=5)
    else:
        bound = socket.socket(socket.AF_UNIX)
        bound.bind(str(path))  # the file is there, nobody listens yet
        with pytest.raises(RuntimeError, match="did not listen"):
            rt.wait_for_unix_listener(path, Mock(poll=Mock(return_value=None)), errors, "x11vnc", timeout=0.5)
        bound.close()


# --- state an earlier run may have left: symlinked workspaces, private directories ------------------


def test_logical_cwd(scenario, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.chdir(real)
    if scenario == "happy":  # the shell's name for the directory, symlink kept
        monkeypatch.setenv("PWD", str(link))
        assert s.logical_cwd() == str(link)
    elif scenario == "sad":  # a stale or foreign PWD is ignored
        monkeypatch.setenv("PWD", str(tmp_path))
        assert s.logical_cwd() == str(real)
    else:
        monkeypatch.setenv("PWD", "relative/path")
        assert s.logical_cwd() == str(real)


def test_planted_symlink(scenario, tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    planted = project / "frontend"
    planted.symlink_to(target)
    if scenario == "happy":  # a link inside a tree a sandbox could write
        assert s.planted_symlink(planted / "x", [project]) == planted
    elif scenario == "sad":  # your own link elsewhere (~/src -> /data/src) is fine
        assert s.planted_symlink(planted / "x", [tmp_path / "other"]) is None
    else:
        assert s.planted_symlink(project / "src", [project]) is None  # no link on the way


def test_check_planted(scenario, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    (project / "shared").symlink_to(config)
    if scenario == "happy":
        with pytest.raises(ValueError, match="goes through the symlink .*shared.*give the real path: .*config"):
            s.check_planted(project / "shared", "--mount", [project])
    elif scenario == "sad":
        s.check_planted(config, "--mount", [project])
    else:
        s.check_planted(str(project / "shared"), "--mount", [])  # no registry yet: nothing to compare


def test_exposed_registry(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert s.exposed_registry() == tmp_path / "sandbox-ai" / "exposed.json"
    elif scenario == "sad":
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert s.exposed_registry().parts[-3:] == ("state", "sandbox-ai", "exposed.json")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", "")
        assert s.exposed_registry().name == "exposed.json"


def test_exposed_before(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = tmp_path / "sandbox-ai" / "exposed.json"
    path.parent.mkdir()
    if scenario == "happy":
        path.write_text(json.dumps(["/home/u/proj", "relative", 3]))
        assert s.exposed_before() == [Path("/home/u/proj")]
    elif scenario == "sad":
        path.write_text("not json")
        assert s.exposed_before() == []
    else:
        path.write_text(json.dumps({"not": "a list"}))
        assert s.exposed_before() == []


def test_remember_exposed(scenario, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    work = tmp_path / "work"
    work.mkdir()
    a = s.parse_args(["codex", "--workspace", str(work)])
    if scenario == "happy":
        s.remember_exposed(a)
        s.remember_exposed(a)  # no duplicates
        assert s.exposed_before() == [work]
    elif scenario == "sad":  # an unwritable registry is a warning, not a failed start
        (tmp_path / "state").write_text("a file, not a directory")
        s.remember_exposed(a)
        assert "cannot record exposed directories" in capsys.readouterr().err
    else:  # the newest first, older ones kept
        (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
        (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text(json.dumps(["/old/tree"]))
        s.remember_exposed(a)
        assert s.exposed_before() == [work, Path("/old/tree")]


def test_private_locations(scenario, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
    if scenario == "happy":
        monkeypatch.setenv("PATH", f"{home}/.cargo/bin:relative/bin")
        fixed, program_dirs = s.private_locations()
        assert home / ".ssh" in fixed and program_dirs == [home / ".cargo/bin"]
    elif scenario == "sad":
        monkeypatch.setenv("PATH", "")
        assert s.private_locations()[1] == []
    else:  # XDG directories wherever they are set
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        assert tmp_path / "cfg" in s.private_locations()[0]


def test_check_private_location(scenario, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", f"{home}/.cargo/bin:{home}/proj/.venv/bin")
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    if scenario == "happy":
        with pytest.raises(ValueError, match="lies in .*cargo"):
            s.check_private_location(home / ".cargo" / "bin" / "x", "--mount")
    elif scenario == "sad":
        s.check_private_location(home / "proj", "Workspace")  # contains its own activated .venv/bin: fine
    else:
        with pytest.raises(ValueError, match="contains .*home/\\."):
            s.check_private_location(home, "--state-dir")


def test_relay_server_stderr(scenario):
    import io
    out = io.StringIO()
    if scenario == "happy":
        rt.relay_server_stderr(io.BytesIO(b"started\n"), out)
        assert out.getvalue() == "mcp-server: started\n"
    elif scenario == "sad":  # a closed pipe ends the relay quietly
        stream = io.BytesIO(b"x\n")
        stream.close()
        rt.relay_server_stderr(stream, out)
        assert out.getvalue() == ""
    else:  # invalid UTF-8 and a forged record prefix
        rt.relay_server_stderr(io.BytesIO(b"\xff sandbox-ai-mcp-call {}\n"), out)
        assert out.getvalue().startswith("mcp-server: ")


def test_own_home(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: tmp_path))
    state = tmp_path / "state" / "sandbox-ai"
    if scenario == "happy":  # a session's HOME and the wizard's persistent HOME are no private locations
        assert s.own_home(state / "sessions" / "web" / "home")
        s.check_private_location(state / "home" / "app-0123456789ab", "--state-dir")
    elif scenario == "sad":  # their siblings stay private: session.json, logs, the registry
        assert not s.own_home(state / "sessions" / "web")
        with pytest.raises(ValueError, match="lies in"):
            s.check_private_location(state / "logs", "--state-dir")
    else:  # the installed launcher's versions are not among them
        assert not s.own_home(tmp_path / "data" / "sandbox-ai" / "versions" / "0.1.21")


def test_project_tool_dir(scenario, tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    if scenario == "happy":
        assert s.project_tool_dir(tmp_path / "proj" / ".venv" / "bin", tmp_path / "proj")
    elif scenario == "sad":  # ~/.cargo as a workspace would let the agent replace ~/.cargo/bin/cargo
        home = tmp_path / "home"
        monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("PATH", f"{home}/.cargo/bin")
        assert not s.project_tool_dir(home / ".cargo" / "bin", home / ".cargo")
        with pytest.raises(ValueError, match="contains .*cargo"):
            s.check_private_location(home / ".cargo", "Workspace")
    else:
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "proj" / "env"))
        assert s.project_tool_dir(tmp_path / "proj" / "env" / "bin", tmp_path / "proj")


def test_seed_exposed(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "cfg" / "sandbox-ai").mkdir(parents=True)
    if scenario == "happy":  # projects of approvals, trusted configurations, sessions and MCP definitions
        (tmp_path / "cfg" / "sandbox-ai" / "approved.json").write_text(json.dumps({"/p/one": {}}))
        (tmp_path / "cfg" / "sandbox-ai" / "trusted.json").write_text(json.dumps({"/p/two/.sandbox-ai.json": "x"}))
        (tmp_path / "cfg" / "sandbox-ai" / "mcp.json").write_text(json.dumps({"bw": {
            "image": "x", "mounts": [{"host": "/p/out", "target": "/out", "mode": "rw"}]}}))
        session = tmp_path / "state" / "sandbox-ai" / "sessions" / "web"
        session.mkdir(parents=True)
        (session / "session.json").write_text(json.dumps({"workspace": "/p/three"}))
        assert s.seed_exposed() == [Path("/p/one"), Path("/p/two"), Path("/p/three"), Path("/p/out")]
        assert s.exposed_before() == s.seed_exposed()  # used until the registry is written
    elif scenario == "sad":
        (tmp_path / "cfg" / "sandbox-ai" / "approved.json").write_text("broken")
        assert s.seed_exposed() == []
    else:
        (tmp_path / "cfg" / "sandbox-ai" / "approved.json").write_text(json.dumps({"relative": {}}))
        assert s.seed_exposed() == []


def test_shortened(scenario):
    if scenario == "happy":  # long secrets keep their closing quote, so the redaction still matches
        record = rt.tool_call_record(json.dumps({"method": "tools/call", "params": {
            "name": "x", "arguments": {"client_secret": "ya29." + "A" * 9000}}}).encode())
        assert "AAAAAAAAAA" not in record["arguments"]
    elif scenario == "sad":
        assert rt.shortened({"k": ["v"] * 500})["k"][-1] == "..."
    else:
        assert rt.shortened(7) == 7 and rt.shortened("x" * 501).endswith("...")


def test_private_directories_are_no_workspaces(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "systemd").mkdir(parents=True)
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(ValueError, match="lies in .*\\.config"):
        s.check_workspace_scope(home / ".config" / "systemd")
    s.check_workspace_scope(home / "src" / "app")  # a project directory in HOME is fine


def test_overlapping_once_approvals_keep_the_target_open_until_the_last_ends():
    rules, timers = [], []
    network = rt.RuntimeNetwork({}, None, rules.append)
    for _ in range(2):
        network.allow_once("198.51.100.1", 443, timer=lambda seconds, function: timers.append(function) or Mock())
    timers[0]()
    assert "198.51.100.1" in rules[-1]  # the second approval is still running
    timers[1]()
    assert "198.51.100.1" not in rules[-1]


def test_mounts_changed(scenario, tmp_path):
    folder = tmp_path / "out"
    folder.mkdir()
    info = os.stat(folder)
    record = [{"path": str(folder), "dev": info.st_dev, "ino": info.st_ino}]
    if scenario == "happy":
        assert rt.mounts_changed(record) is None and rt.mounts_changed([]) is None
    elif scenario == "sad":  # swapped for a link to another folder while the server ran
        other = tmp_path / "config"
        other.mkdir()
        folder.rmdir()
        folder.symlink_to(other)
        assert "has become a symlink" in rt.mounts_changed(record)
    else:  # replaced by another directory of the same name
        folder.rmdir()
        folder.mkdir()
        (tmp_path / "keep").mkdir()  # the old inode must not come back
        assert "replaced" in rt.mounts_changed(record) or os.stat(folder).st_ino == info.st_ino


def test_image_mount_hosts(scenario, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "sandbox-ai").mkdir()
    path = tmp_path / "sandbox-ai" / "mcp.json"
    if scenario == "happy":
        path.write_text(json.dumps({"bw": {"image": "x", "mounts": [{"host": str(tmp_path / "out"), "target": "/o"}]}}))
        assert s.image_mount_hosts() == [(tmp_path / "out").resolve()]
    elif scenario == "sad":
        assert s.image_mount_hosts() == []  # no mcp.json
    else:
        path.write_text(json.dumps({"bw": {"mounts": ["not a dict"]}, "x": []}))
        assert s.image_mount_hosts() == []
        # Image servers of the trusted project file count too.
        project = {"mcp_servers": {"p": {"image": "x", "mounts": [{"host": str(tmp_path / "p"), "target": "/o"}]}}}
        assert s.image_mount_hosts(project) == [(tmp_path / "p").resolve()]


def test_workspace_holding_an_image_mount_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    work = tmp_path / "data"
    (work / "extracted").mkdir(parents=True)
    (tmp_path / "cfg" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "cfg" / "sandbox-ai" / "mcp.json").write_text(json.dumps(
        {"bw": {"image": "x", "mounts": [{"host": str(work / "extracted"), "target": "/out", "mode": "rw"}]}}))
    with pytest.raises(ValueError, match="an MCP image server mounts"):
        s.validate(s.parse_args(["codex", "--workspace", str(work), "--no-config"]))
    s.validate(s.parse_args(["codex", "--workspace", str(work / "extracted"), "--no-config"]))  # the folder itself


def test_tool_dirs_state(scenario, tmp_path, monkeypatch):
    work = tmp_path / "proj"
    tools = work / ".venv" / "bin"
    tools.mkdir(parents=True)
    (tools / "python").write_text("x")
    a = argparse.Namespace(workspaces=[str(work)])
    if scenario == "happy":
        monkeypatch.setenv("PATH", f"{tools}:/usr/bin")
        assert list(s.tool_dirs_state(a)) == [str(tools)] and "python" in s.tool_dirs_state(a)[str(tools)]
    elif scenario == "sad":
        monkeypatch.setenv("PATH", "/usr/bin:relative")
        assert s.tool_dirs_state(a) == {}
    else:
        monkeypatch.setenv("PATH", str(tmp_path / "missing"))
        assert s.tool_dirs_state(a) == {}


def test_tool_dirs_changed(scenario, tmp_path, monkeypatch):
    work = tmp_path / "proj"
    tools = work / "node_modules" / ".bin"
    tools.mkdir(parents=True)
    (tools / "eslint").write_text("x")
    monkeypatch.setenv("PATH", str(tools))
    before = s.tool_dirs_state(argparse.Namespace(workspaces=[str(work)]))
    if scenario == "happy":  # the agent put a git where your shell finds it first
        (tools / "git").write_text("#!/bin/sh\n")
        assert "git" in s.tool_dirs_changed(before)[0] and "on your PATH" in s.tool_dirs_changed(before)[0]
    elif scenario == "sad":
        assert s.tool_dirs_changed(before) == []
    else:  # same size, mtime reset: the ctime still tells
        stat_before = os.stat(tools / "eslint")
        time.sleep(0.01)
        (tools / "eslint").write_text("y")
        os.utime(tools / "eslint", ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        assert "eslint" in s.tool_dirs_changed(before)[0]


# --- the administrator's policy -------------------------------------------------------------------


def policy_file(tmp_path, document, monkeypatch, owner=0, mode=0o644):
    """A policy file as root would own it (os.lstat/os.stat report OWNER and MODE)."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    real_lstat, real_stat = os.lstat, os.stat

    def fake(function):
        def call(target, *args, **kwargs):
            info = function(target, *args, **kwargs)
            if Path(target) in (path, path.parent):
                values = list(info)
                values[stat.ST_UID], values[stat.ST_MODE] = owner, (info.st_mode & ~0o7777) | mode
                return os.stat_result(values)
            return info
        return call
    monkeypatch.setattr(s.os, "lstat", fake(real_lstat))
    monkeypatch.setattr(s.os, "stat", fake(real_stat))
    return path


def test_load_policy(scenario, tmp_path, monkeypatch):
    if scenario == "happy":
        path = policy_file(tmp_path, {"version": 1, "forbid": ["--egress-all-allow", "--ask=off"],
                                      "max_approval_scope": "run"}, monkeypatch)
        assert s.load_policy(path)["max_approval_scope"] == "run"
        assert s.load_policy(tmp_path / "missing.json") == {}  # no policy: nothing changes
    elif scenario == "sad":  # a file the user could edit is no policy: every start stops
        path = policy_file(tmp_path, {"version": 1}, monkeypatch, owner=os.getuid() or 1000)
        with pytest.raises(ValueError, match="owned by root"):
            s.load_policy(path)
    else:
        path = policy_file(tmp_path, {"version": 1, "forbid": ["--no-such-option"]}, monkeypatch)
        with pytest.raises(ValueError, match="unknown option"):
            s.load_policy(path)
        path = policy_file(tmp_path, {"version": 1}, monkeypatch, mode=0o666)
        with pytest.raises(ValueError, match="writable by root only"):
            s.load_policy(path)


def test_check_policy(scenario):
    policy = {"version": 1, "forbid": ["--egress-all-allow", "--privacy-check=off", "--no-proxy"],
              "backends": ["podman"], "max_approval_scope": "once", "message": "ask IT"}
    if scenario == "happy":
        a = s.parse_args(["codex", "--allow-pypi"])
        s.check_policy(a, policy)
        assert a.max_approval_scope == "once"
    elif scenario == "sad":
        with pytest.raises(ValueError, match="--privacy-check=off is not allowed.*ask IT"):
            s.check_policy(s.parse_args(["codex", "-pc", "off"]), policy)  # a short form counts too
        with pytest.raises(ValueError, match="backend bwrap"):
            s.check_policy(s.parse_args(["codex", "--backend", "bwrap"]), policy)
    else:  # whatever sets the option: a profile that implies --egress-all-allow is refused as well
        a = s.parse_args(["codex"])
        a.egress_all_allow = True
        with pytest.raises(ValueError, match="--egress-all-allow"):
            s.check_policy(a, policy)
        empty = s.parse_args(["codex"])
        s.check_policy(empty, {})
        assert empty.max_approval_scope == "always"


def test_policy_caps_the_scope_of_answers(tmp_path):
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda rules: None), max_scope="run")
    (tmp_path / "a" / "1-1-1.answer").write_text(json.dumps({"decision": "allow", "scope": "always"}))
    assert broker._read_answer("1-1-1")["scope"] == "run"


def test_http_rules(scenario):
    if scenario == "happy":
        a = s.parse_args(["codex", "--allow-http", "api.github.com GET,HEAD /repos/**", "-ah", "*.example.org * /"])
        assert s.http_rules(a) == [{"host": "api.github.com", "methods": ["GET", "HEAD"], "path": "/repos/**"},
                                   {"host": "*.example.org", "methods": ["*"], "path": "/"}]
    elif scenario == "sad":
        with pytest.raises(ValueError, match="HOST METHODS PATH"):
            s.http_rules(s.parse_args(["codex", "--allow-http", "api.github.com get /x"]))
    else:  # rules for a host whose TLS passes unopened could never hold
        with pytest.raises(ValueError, match="--no-inspect"):
            s.http_rules(s.parse_args(["codex", "--allow-http", "api.github.com GET /", "--no-inspect", "*.github.com"]))


def test_host_matches_any(scenario):
    if scenario == "happy":
        assert s.host_matches_any("api.github.com", "*.github.com") and s.host_matches_any("a.b", "a.b")
    elif scenario == "sad":
        assert not s.host_matches_any("github.com.evil.example", "github.com")
    else:
        assert s.host_matches_any("*.github.com", "github.com")


def test_proxy_config_carries_http_rules_with_pinned_addresses():
    a = s.parse_args(["codex", "--allow-http", "api.github.com POST /repos/*/issues"])
    config = s.proxy_config(a, {"api.github.com": ["192.0.2.21"], "pypi.org": ["192.0.2.30"]}, "l")
    assert config["http_rules"] == [{"host": "api.github.com", "methods": ["POST"], "path": "/repos/*/issues",
                                     "addresses": ["192.0.2.21"]}]


def test_policy_network_ceiling_and_mcp(tmp_path, monkeypatch):
    policy = {"version": 1, "allow_within": ["10.0.0.0/8"], "mcp_network": "none"}
    s.check_policy(s.parse_args(["codex", "--allow", "10.1.0.0/16", "--allow-tcp", "10.2.3.4:5432"]), policy)
    for options in (["--allow", "0.0.0.0/1"], ["--allow-tcp", "192.0.2.9:22"], ["--egress-all-allow"]):
        with pytest.raises(ValueError, match="administrator"):
            s.check_policy(s.parse_args(["codex", *options]), policy)
    a = s.parse_args(["codex"])
    s.check_policy(a, {"version": 1, "forbid": ["--no-proxy"], "allow_within": ["10.0.0.0/8"]})
    assert a.policy == {"forbid": ["--no-proxy"], "allow_within": ["10.0.0.0/8"]}  # handed to the supervisor


def test_runtime_changes_obey_the_policy(tmp_path):
    """sandbox-ai net allow and approvals during a run: the supervisor holds the ceiling and the forbidden options."""
    network = rt.RuntimeNetwork({}, None, lambda rules: None, within=["10.0.0.0/8"])
    network.change({"op": "allow", "nets": ["10.9.0.0/16"]})
    with pytest.raises(ValueError, match="outside the networks"):
        network.change({"op": "allow", "nets": ["0.0.0.0/1"]})
    with pytest.raises(ValueError, match="outside the networks"):
        network.allow_once("192.0.2.9", 443)
    import socket as socket_module
    path = tmp_path / "control.sock"
    stop = threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(path, rt.RuntimeNetwork({}, None, lambda r: None), stop,
                                                               ["--allow", "--egress-all-allow"]), daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.02)

    def ask(request):
        with socket_module.socket(socket_module.AF_UNIX) as client:
            client.connect(str(path))
            client.sendall((json.dumps(request) + "\n").encode())
            return json.loads(client.makefile().readline())
    assert "not allowed by the administrator" in ask({"op": "allow", "nets": ["192.0.2.0/24"]})["error"]
    assert ask({"op": "allow", "tcp": [["192.0.2.9", 443]]})["ok"]
    stop.set()
    thread.join(5)


def test_host_name(scenario):
    if scenario == "happy":
        assert rt.host_name("API.github.com:443") == "api.github.com"
    elif scenario == "sad":
        assert rt.host_name(None) is None and rt.host_name("") is None
    else:
        assert rt.host_name("[2001:db8::1]:8443") == "2001:db8::1" and rt.host_name("[broken") is None


def test_is_address_origin(scenario):
    if scenario == "happy":
        assert rt.is_address_origin("192.0.2.7:80") and rt.is_address_origin("[2001:db8::1]:443")
    elif scenario == "sad":
        assert not rt.is_address_origin("api.github.com")
    else:
        assert rt.is_address_origin("192.0.2.7")


def test_normal_path(scenario):
    if scenario == "happy":
        assert rt.normal_path("/repos/a/b/../c?q=1#f") == "/repos/a/c"
    elif scenario == "sad":  # encodings and dot segments resolve as the server would
        assert rt.normal_path("/x/%2e%2e/%2e%2e/../gists") == "/gists"
    else:
        assert rt.normal_path("") == "" and rt.normal_path("relative/path") == "/relative/path"


def test_path_matches(scenario):
    if scenario == "happy":
        assert rt.path_matches("/repos/*/pulls", "/repos/app/pulls") and rt.path_matches("/**", "/a/b/c")
    elif scenario == "sad":
        assert not rt.path_matches("/repos/*", "/repos/a/b") and not rt.path_matches("/a.b", "/axb")
    else:
        assert rt.path_matches("/", "") and rt.path_matches("/*", "/")


def test_within_ceiling(scenario):
    a = s.parse_args(["codex"])
    a.policy = {"allow_within": ["10.0.0.0/8"], "allow_hosts": ["*.corp.example", "pypi.org"]}
    if scenario == "happy":
        assert s.within_ceiling(a, network="10.1.2.3") and s.within_ceiling(a, host="git.corp.example")
    elif scenario == "sad":
        assert not s.within_ceiling(a, network="0.0.0.0/1") and not s.within_ceiling(a, host="pastebin.com")
    else:
        a.policy = {}
        assert s.within_ceiling(a, network="0.0.0.0/0") and s.within_ceiling(a, host="anything.example")


def test_check_ceiling(scenario):
    policy = {"version": 1, "allow_within": ["10.0.0.0/8"], "allow_hosts": ["pypi.org", "files.pythonhosted.org"]}
    if scenario == "happy":  # the provider and listed hosts are fine
        s.check_policy(s.parse_args(["codex", "--allow-pypi", "--allow-host", "pypi.org"]), policy)
    elif scenario == "sad":  # named hosts, presets, DNS, host ports: all widen past the ceiling
        for options in (["--allow-host", "pastebin.com"], ["--allow-github"], ["--dns"], ["--nameserver", "8.8.8.8"],
                        ["--allow-host-port", "11434"]):
            with pytest.raises(ValueError, match="administrator"):
                s.check_policy(s.parse_args(["codex", *options]), policy)
    else:  # a resolver inside the ceiling is fine
        s.check_policy(s.parse_args(["codex", "--nameserver", "10.0.0.53"]), policy)
