"""Happy/sad/edge contracts for every launcher function.

Only OS/process boundaries are mocked. Filesystem copies, pipes, validation,
configuration serialization and security decisions use the implementation.
The separate live suite proves the actual namespace and model boundaries.
"""
import ast
import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_contracts", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("HOME", str(host))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TERM"):
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
        assert "api.anthropic.com" in answer and "api.openai.com" in answer


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
                 ["--env", "UNSET_CONTRACT_VARIABLE"], ["--allow-host", "a..example.org"]]
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
        source.write_text('{"model":"changed"}')
        with pytest.raises(ValueError, match="overwrite"):
            s.prepare_home(args, home)
        assert (home / ".config/opencode/opencode.jsonc").read_text() == '{"model":"fixture"}'
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
    monkeypatch.setattr(s.ctypes, "CDLL", lambda *a, **kw: libc)
    run, popen = Mock(), Mock()
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    monkeypatch.setattr(s, "namespace_state", lambda: states()[0])
    monkeypatch.setattr(s, "firejail_command", lambda state: ["firejail-fixture"])
    monkeypatch.setattr(s, "wait_child", lambda child: 0)
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
    monkeypatch.setattr(s, "namespace_state", lambda: current)
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


def test_mount_option(scenario):
    if scenario == "sad":
        for source in ("/tmp/a,b", "/tmp/a\nb", "/tmp/a\rb"):
            with pytest.raises(ValueError):
                s.mount_option(source, "/work", "rw")
    else:
        mode = "ro" if scenario == "edge" else "rw"
        assert s.mount_option("/tmp/space dir", "/work", mode) == ["--mount", f"type=bind,src=/tmp/space dir,dst=/work,{mode}"]


def test_pod_commands(scenario, tmp_path, monkeypatch):
    options = ["--offline"] if scenario == "sad" else []
    if scenario == "edge":
        options = ["--network", "research", "--ip", "192.0.2.2", "--ip6", "2001:db8::2", "--workspace-ro"]
    args = s.parse_args(["codex", "--workspace", str(tmp_path), *options])
    monkeypatch.setattr(s.sys.stdin, "isatty", lambda: scenario == "edge")
    monkeypatch.setattr(s.sys.stdout, "isatty", lambda: scenario == "edge")
    create, run = s.pod_commands(args, "owned-fixture", tmp_path / "session", tmp_path / "home")
    assert create[create.index("--network") + 1] == ("none" if scenario == "sad" else "research" if scenario == "edge" else s.POD_NETWORK)
    assert ("--tty" in run) == (scenario == "edge")
    assert not any(v.startswith("--publish") for v in run)
    assert f"type=bind,src={tmp_path},dst=/work,{'ro' if scenario == 'edge' else 'rw'}" in run


def test_run_pod(scenario, tmp_path, monkeypatch):
    args = s.parse_args(["codex", "--offline"])
    run, popen, wait = Mock(), Mock(), Mock(return_value=9 if scenario == "edge" else 0)
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    monkeypatch.setattr(s, "wait_child", wait)
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
    if scenario == "sad":
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), 0]
    s.stop_process(process)
    assert process.terminate.call_count == (0 if scenario == "edge" else 1)
    assert process.kill.call_count == (1 if scenario == "sad" else 0)
    s.stop_process(None)


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
    monkeypatch.setattr(s, "wait_child", lambda child: 0)
    stop = Mock()
    monkeypatch.setattr(s, "stop_process", stop)
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


def test_contract_manifest_covers_every_launcher_function():
    """New production functions must get the same three contract scenarios."""
    tree = ast.parse((ROOT / "sandbox/agent_sandbox.py").read_text())
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for function in sorted(functions):
        contract = globals().get("test_" + function)
        assert callable(contract), "Missing happy/sad/edge contract for " + function
        assert "scenario" in contract.__code__.co_varnames[:contract.__code__.co_argcount]
