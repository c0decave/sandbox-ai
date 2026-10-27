"""Cross-product tests of public launcher behavior, with no real credentials."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_matrix", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)
AGENTS = ("codex", "claude", "opencode")
BACKENDS = ("podman", "bwrap")


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.setenv("HOME", str(host))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(key, raising=False)
    tools = tmp_path / "bin"
    tools.mkdir()
    for agent in AGENTS:
        executable = tools / agent
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)


def fake_tool(tmp_path, name):
    return str(tmp_path / "bin" / name) if name in AGENTS else "/usr/bin/" + name


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("auth", ("auto-present", "auto-missing", "none", "explicit", "copy-required"))
@pytest.mark.parametrize("workspace_exists", (False, True), ids=("new-workspace", "existing-workspace"))
def test_startup_matrix(agent, backend, auth, workspace_exists, tmp_path, monkeypatch, capsys):
    """60 combinations preserve workspace, auth source and external isolation."""
    workspace = tmp_path / "project with spaces"
    if workspace_exists:
        workspace.mkdir()
        (workspace / "existing.txt").write_text("operator data")
    source = tmp_path / "fixture-auth.json"
    if auth != "auto-missing":
        source.write_text('{"fixture":"host credentials"}')
    monkeypatch.setattr(s, "auth_sources", lambda name: [(source, s.AUTH_TARGETS[name])])
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: fake_tool(tmp_path, name))
    options = [agent, "--backend", backend, "--workspace", str(workspace), "--offline"]
    if auth == "none":
        options += ["--no-auth"]
    elif auth == "explicit":
        options += ["--auth-file", str(source)]
    elif auth == "copy-required":
        options += ["--copy-auth"]
    recorded = {}
    def run(args, session, home):
        recorded.update(home=home, session=session)
        config = json.loads((session / "config.json").read_text())
        target = home / s.AUTH_TARGETS[agent]
        assert target.exists() == (auth in ("auto-present", "explicit", "copy-required"))
        if target.exists():
            assert target.read_text() == source.read_text()
            assert target.stat().st_mode & 0o777 == 0o600
            target.write_text('{"fixture":"sandbox refreshed copy"}')
        assert config["command"][0] == (agent if backend == "podman" else fake_tool(tmp_path, agent))
        assert (config["command"][1:3] == ["--sandbox", "danger-full-access"]) == (agent == "codex")
        assert config["env"] == {} and not config["probe"]
        # The workspace persists while the sandbox's private home does not.
        (workspace / "result.txt").write_text("persisted")
        return 0
    runner = Mock(side_effect=run)
    monkeypatch.setattr(s, "run_pod" if backend == "podman" else "run_bwrap", runner)
    assert s.main(options) == 0
    runner.assert_called_once()
    assert (workspace / "result.txt").read_text() == "persisted"
    assert ("Workspace existiert bereits" in capsys.readouterr().err) == workspace_exists
    if workspace_exists:
        assert (workspace / "existing.txt").read_text() == "operator data"
    if source.exists():
        assert json.loads(source.read_text()) == {"fixture": "host credentials"}
    assert not recorded["home"].exists() and not recorded["session"].exists()


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("mode", ("defaults", "offline", "explicit-only"))
def test_network_policy_matrix(agent, backend, mode, tmp_path):
    """18 combinations cover agent-specific defaults and both deny modes."""
    options = {"defaults": [], "offline": ["--offline"],
               "explicit-only": ["--no-provider-defaults", "--allow-host", "example.org"]}[mode]
    args = s.parse_args([agent, "--backend", backend, *options])
    s.validate(args)
    hosts = s.allowed_hosts(args)
    if mode == "defaults":
        assert "api.openai.com" in hosts and "api.anthropic.com" in hosts
        assert ("opencode.ai" in hosts) == (agent == "opencode")
    else:
        assert hosts == ([] if mode == "offline" else ["example.org"])
    rules = s.firewall_rules([], ["203.0.113.1"] if hosts else [])
    assert rules.count("policy drop") == 3
    assert ("tcp dport 443 accept" in rules) == bool(hosts)
    assert "dport 53" not in rules


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("failure", ("missing-tool", "bad-state-mode", "state-symlink", "foreign-state-owner"))
def test_startup_failure_matrix(agent, backend, failure, tmp_path, monkeypatch):
    """24 failures never launch an agent or copy host credentials."""
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: fake_tool(tmp_path, name))
    runner = Mock(side_effect=AssertionError("agent must not start"))
    prepare = Mock(side_effect=AssertionError("auth must not be read"))
    monkeypatch.setattr(s, "run_pod", runner)
    monkeypatch.setattr(s, "run_bwrap", runner)
    monkeypatch.setattr(s, "prepare_home", prepare)
    if failure == "missing-tool":
        monkeypatch.setattr(s.shutil, "which", lambda name: None)
        message = "Required tool missing"
    elif failure == "bad-state-mode":
        state.chmod(0o755)
        message = "mode 0700"
    elif failure == "state-symlink":
        link = tmp_path / "state-link"
        link.symlink_to(state, target_is_directory=True)
        state = link
        message = "symlink"
    else:
        # Exercise ownership rejection without chown or an elevated process.
        monkeypatch.setattr(s.os, "getuid", lambda: state.stat().st_uid + 1)
        message = "owned by you"
    with pytest.raises(ValueError, match=message):
        s.main([agent, "--backend", backend, "--workspace", str(tmp_path / "work"),
                "--state-dir", str(state), "--offline", "--no-auth"])
    runner.assert_not_called()
    prepare.assert_not_called()


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("backend", BACKENDS)
def test_persistent_auth_refresh_reuse_matrix(agent, backend, tmp_path, monkeypatch):
    """Refreshed private tokens survive restarts; host originals stay unchanged."""
    source = tmp_path / "source.json"
    source.write_text('{"fixture":"old host credentials"}')
    monkeypatch.setattr(s, "auth_sources", lambda name: [(source, s.AUTH_TARGETS[name])])
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: fake_tool(tmp_path, name))
    calls = []
    def run(args, session, home):
        target = home / s.AUTH_TARGETS[agent]
        calls.append(json.loads(target.read_text())["fixture"])
        target.write_text('{"fixture":"refreshed sandbox credentials"}')
        return 0
    monkeypatch.setattr(s, "run_pod" if backend == "podman" else "run_bwrap", run)
    args = [agent, "--backend", backend, "--workspace", str(tmp_path / "work"),
            "--state-dir", str(tmp_path / "state"), "--offline"]
    assert s.main(args) == s.main(args) == 0
    assert calls == ["old host credentials", "refreshed sandbox credentials"]
    assert json.loads(source.read_text())["fixture"] == "old host credentials"


@pytest.mark.parametrize("allocation_failure", (0, 1, 2, 3))
def test_pipe_allocation_failure_matrix(allocation_failure, tmp_path, monkeypatch):
    real_pipe = os.pipe
    allocated = []
    calls = 0
    def pipe():
        nonlocal calls
        if calls == allocation_failure:
            raise OSError("pipe allocation failed")
        calls += 1
        pair = real_pipe()
        allocated.extend(pair)
        return pair
    monkeypatch.setattr(s.os, "pipe", pipe)
    with pytest.raises(OSError, match="allocation failed"):
        s.run_bwrap(s.parse_args(["codex", "--backend", "bwrap"]), tmp_path, tmp_path)
    for fd in allocated:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("stage", ("image", "create", "spawn", "wait"))
def test_pod_failure_ownership_matrix(stage, tmp_path, monkeypatch):
    run = Mock()
    if stage in ("image", "create"):
        run.side_effect = ([subprocess.CalledProcessError(1, stage)] if stage == "image"
                           else [Mock(), subprocess.CalledProcessError(1, stage)])
    monkeypatch.setattr(s.subprocess, "run", run)
    popen = Mock(side_effect=OSError("spawn") if stage == "spawn" else None)
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    monkeypatch.setattr(s, "wait_child", Mock(side_effect=OSError("wait")))
    with pytest.raises((OSError, subprocess.CalledProcessError)):
        s.run_pod(s.parse_args(["codex", "--offline"]), tmp_path, tmp_path)
    cleaned = [c for c in run.call_args_list if c.args[0][1:4] == ["pod", "rm", "--force"]]
    assert len(cleaned) == (1 if stage in ("spawn", "wait") else 0)


@pytest.mark.parametrize("parts", ([b'{"child-', b'pid":12345}'], [b'{"child-pid":', b'12345}']))
def test_read_ready_fragmented_json(parts, monkeypatch):
    monkeypatch.setattr(s.select, "select", lambda *a: ([9], [], []))
    monkeypatch.setattr(s.os, "read", Mock(side_effect=parts))
    child = Mock()
    child.poll.return_value = None
    assert s.read_ready(9, child, timeout=1) == {"child-pid": 12345}


@pytest.mark.parametrize("failure", ("timeout", "process-exit", "malformed"))
def test_read_ready_failure_matrix(failure, monkeypatch):
    monkeypatch.setattr(s.select, "select", lambda *a: ([9], [], []) if failure == "malformed" else ([], [], []))
    monkeypatch.setattr(s.os, "read", lambda *a: b'invalid-json')
    child = Mock(returncode=17)
    child.poll.return_value = 17 if failure == "process-exit" else None
    monkeypatch.setattr(s.time, "monotonic", Mock(side_effect=[0, 0.1, 0.2, 2]))
    with pytest.raises(RuntimeError, match="status 17" if failure == "process-exit" else "Timed out"):
        s.read_ready(9, child, timeout=1)


@pytest.mark.parametrize("failure", ("prctl", "nft-install", "nft-list", "guard-command", "spawn"))
def test_bootstrap_failure_matrix(failure, monkeypatch):
    monkeypatch.setattr(s.os, "getpid", lambda: 1)
    libc = Mock()
    libc.prctl.return_value = -1 if failure == "prctl" else 0
    monkeypatch.setattr(s.ctypes, "CDLL", lambda *a, **kw: libc)
    run = Mock()
    if failure in ("nft-install", "nft-list"):
        run.side_effect = ([subprocess.CalledProcessError(1, "nft")] if failure == "nft-install"
                           else [Mock(), subprocess.CalledProcessError(1, "nft")])
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s, "namespace_state", lambda: {})
    monkeypatch.setattr(s, "firejail_command", Mock(side_effect=OSError("guard") if failure == "guard-command" else None))
    popen = Mock(side_effect=OSError("spawn"))
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    with pytest.raises((OSError, subprocess.CalledProcessError)):
        s.inner_bootstrap()
    assert popen.call_count == (1 if failure == "spawn" else 0)
