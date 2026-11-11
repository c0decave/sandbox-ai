"""Boundary regressions supplement the per-function and cross-product tests."""
import importlib.util
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write logins: never into the real HOME, also when conftest.py was not run

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "sandbox/agent_sandbox.py"
RUNTIME = ROOT / "sandbox/runtime.py"
SPEC = importlib.util.spec_from_file_location("sandbox_edges", SOURCE)
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)


@pytest.mark.parametrize("bad_path", ("source,comma", "destination,comma", "dangling-workspace", "duplicate-workspace"))
def test_invalid_path_boundaries(bad_path, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    args = ["codex", "--workspace", str(tmp_path / "work")]
    if bad_path == "source,comma":
        bad = tmp_path / "source,comma"
        bad.mkdir()
        args += ["--mount", f"{bad}:/mnt/data:ro"]
    elif bad_path == "destination,comma":
        args += ["--mount", f"{source}:/mnt/data,invalid:ro"]
    elif bad_path == "dangling-workspace":
        dangling = tmp_path / "dangling"
        dangling.symlink_to(tmp_path / "missing", target_is_directory=True)
        args = ["codex", "--workspace", str(dangling)]
    else:
        args += ["--mount", f"{source}:/work:rw"]
    with pytest.raises(ValueError):
        s.validate(s.parse_args(args))


def test_mount_into_existing_workspace_subdirectory_is_allowed(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "existing").mkdir()
    args = s.parse_args(["codex", "--workspace", str(workspace),
                         "--mount", f"{tmp_path}:/work/existing/reference:ro"])
    s.validate(args)


def test_empty_relative_copy_destination_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.write_text("fixture")
    for destination in ("", "."):
        with pytest.raises(ValueError, match="relative path"):
            s.copy_private(source, tmp_path, destination)


@pytest.mark.parametrize("leaf", (".codex/auth.json", ".codex"))
def test_auto_auth_never_follows_destination_symlinks(leaf, tmp_path, monkeypatch):
    source = tmp_path / "fixture-auth.json"
    source.write_text('{"fixture":true}')
    home, outside = tmp_path / "home", tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    link = home / leaf
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(s, "auth_sources", lambda agent: [(source, ".codex/auth.json")])
    with pytest.raises(ValueError, match="symlink"):
        s.prepare_home(s.parse_args(["codex"]), home)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("value", ("", None, [], 1))
def test_auth_import_rejects_nonempty_nonstring_token_fields(value, tmp_path):
    source = tmp_path / "fixture.json"
    source.write_text(json.dumps({"tokens": {"access_token": value, "refresh_token": "fixture", "account_id": "fixture"}}))
    with pytest.raises(ValueError, match="ChatGPT"):
        s.opencode_auth_from_codex(source)


def test_bwrap_accepts_regular_system_dirs_and_explicit_runtime(tmp_path, monkeypatch):
    real_symlink = s.Path.is_symlink
    monkeypatch.setattr(s.Path, "is_symlink", lambda path: False if str(path) == "/bin" else real_symlink(path))
    args = s.parse_args(["codex", "--backend", "bwrap", "--runtime", "/usr/bin",
                         "--mount", f"{tmp_path}:/mnt/reference:ro"])
    command = s.bwrap_command(args, tmp_path, tmp_path)
    assert ["--ro-bind", "/bin", "/bin"] == command[command.index("/bin") - 1:command.index("/bin") + 2]
    assert "/mnt/reference" in command
    assert command.count("/usr/bin") >= 2


def test_missing_agent_fails_before_workspace_or_auth_preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: None if name == "codex" else "/usr/bin/" + name)
    prepare = Mock(side_effect=AssertionError("must not copy auth"))
    monkeypatch.setattr(s, "prepare_home", prepare)
    workspace = tmp_path / "workspace"
    with pytest.raises(ValueError, match="Agent executable missing"):
        s.main(["codex", "--backend", "bwrap", "--offline", "--workspace", str(workspace)])
    prepare.assert_not_called()
    assert not workspace.exists()


IN_SANDBOX = Path("/sandbox/session/config.json").exists()


@pytest.mark.parametrize("mode", ("dry-run", "bad-bootstrap-pid", "bad-guard-namespace", pytest.param(
    "terminated-bootstrap", marks=pytest.mark.skipif(
        IN_SANDBOX, reason="runs a fresh runtime.py, which would read this sandbox's own session config"))))
def test_cli_entrypoint_exit_contract(mode, tmp_path, monkeypatch, capsys):
    if mode == "dry-run":
        source = SOURCE
        argv = [str(SOURCE), "codex", "--dry-run", "--workspace", str(tmp_path / "not-created")]
        expected = 0
    elif mode == "bad-bootstrap-pid":
        monkeypatch.setattr(os, "getpid", lambda: 2)
        source = RUNTIME
        argv = [str(RUNTIME), "--internal-bootstrap"]
        expected = 1
    elif mode == "bad-guard-namespace":
        same = {name: os.readlink("/proc/self/ns/" + name) for name in ("mnt", "pid")}
        source = RUNTIME
        argv = [str(RUNTIME), "--internal-guard", json.dumps(same)]
        expected = 1
    else:
        monkeypatch.setattr(os, "getpid", lambda: 1)
        libc = Mock()
        libc.prctl.return_value = 0
        monkeypatch.setattr(s.RUNTIME.ctypes, "CDLL", lambda *a, **kw: libc)
        monkeypatch.setattr(subprocess, "run", Mock(return_value=Mock(stdout="--seccomp")))
        child = Mock()
        child.wait.return_value = -signal.SIGTERM
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=child))
        source = RUNTIME
        argv = [str(RUNTIME), "--internal-bootstrap"]
        expected = 128 + signal.SIGTERM
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(source), run_name="__main__")
    assert exc.value.code == expected
    captured = capsys.readouterr()
    if mode == "dry-run":
        assert json.loads(captured.out)["auth_mode"] == "auto"
        assert not (tmp_path / "not-created").exists()
    elif mode != "terminated-bootstrap":
        assert "agent-sandbox:" in captured.err and "Traceback" not in captured.err


def test_launcher_exit_code_after_signal(monkeypatch):
    """Review finding: a child killed by SIGTERM made the launcher exit 241 instead of 143."""
    import builtins
    real_exec = builtins.exec
    monkeypatch.setattr(sys, "argv", [str(SOURCE), "codex"])
    namespace = runpy.run_path(str(SOURCE), run_name="not_main")
    assert namespace["main"]  # loads without running
    source = SOURCE.read_text().replace("result = main(sys.argv[1:])", "result = -15")
    code = compile(source, str(SOURCE), "exec")
    with pytest.raises(SystemExit) as exc:
        real_exec(code, {"__name__": "__main__", "__file__": str(SOURCE)})
    assert exc.value.code == 128 + 15


def test_an_attach_ends_quietly_when_the_relay_goes_while_keys_are_sent(tmp_path, monkeypatch):
    """Bug hunt: the relay closed (the agent ended) while typed keys were still on their way; the client's send
    failed with BrokenPipeError, which went up as "agent-sandbox: [Errno 32] Broken pipe" instead of ending the
    attach (sessions.after_attach then never reported the agent's exit status)."""
    import select
    import socket
    import threading
    from types import SimpleNamespace
    path = tmp_path / "attach.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)

    def serve():
        connection, _ = server.accept()
        connection.recv(64)  # the window size; then the relay goes
        connection.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def after_the_relay_went(*args):
        thread.join(5)  # keys are waiting (stdin) when the client looks next, and the relay is gone
        return select.select(*args)
    monkeypatch.setattr(s, "select", SimpleNamespace(select=after_the_relay_went))
    stdin_r, stdin_w = os.pipe()
    out_r, out_w = os.pipe()
    os.write(stdin_w, b"typed while it ended")
    try:
        assert s.attach_client(str(path), stdin_r, out_w) == 0
        thread.join(5)  # (the client may see the relay go before it looks at the keys: the relay's close is racing)
        assert not thread.is_alive()
    finally:
        server.close()
        for fd in (stdin_r, stdin_w, out_r, out_w):
            os.close(fd)


def test_a_pod_removal_that_hangs_does_not_hold_the_launcher(tmp_path, monkeypatch, capsys):
    """Bug hunt: the removal of the run's pod at its end ran without a time limit while Ctrl-C and SIGTERM are held
    (signals_deferred, and podman inherits the mask): a podman that hangs held the launcher until a kill -9. It is
    stopped after POD_REMOVE_SECONDS; the run's own exit status stays, and the user is told how to remove it."""
    args = s.parse_args(["codex", "--offline"])
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1:3] == ["pod", "rm"]:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return Mock(returncode=0)
    monkeypatch.setattr(s.subprocess, "run", run)
    monkeypatch.setattr(s.subprocess, "Popen", Mock())
    monkeypatch.setattr(s, "wait_child", Mock(return_value=3))
    assert s.run_pod(args, tmp_path, tmp_path) == 3
    command, kwargs = calls[-1]
    assert command[1:4] == ["pod", "rm", "--force"] and kwargs["timeout"] == s.POD_REMOVE_SECONDS
    assert f"remove it with: podman pod rm --force {command[-1]}" in capsys.readouterr().err


def test_a_signal_ends_the_wait_for_a_frozen_child():
    """Bug hunt: with the sandbox frozen (a canary's pause stops the bwrap process tree with SIGSTOP), Ctrl-C or
    sandbox-ai net stop was forwarded to the child and stayed pending there: the launcher waited for good. The child
    is thawed after each forwarded signal, so it ends."""
    import threading
    import time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    safety = threading.Timer(10, lambda: child.poll() is None and child.kill())  # never a hanging test
    try:
        os.kill(child.pid, signal.SIGSTOP)
        safety.start()
        threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()  # sandbox-ai net stop
        started = time.monotonic()
        assert s.wait_child(child) == -signal.SIGTERM
        assert time.monotonic() - started < 9
    finally:
        safety.cancel()
        if child.poll() is None:
            child.kill()
            child.wait()
