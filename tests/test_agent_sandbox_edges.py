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

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "sandbox/agent_sandbox.py"
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


@pytest.mark.parametrize("mode", ("dry-run", "bad-bootstrap-pid", "bad-guard-namespace", "terminated-bootstrap"))
def test_cli_entrypoint_exit_contract(mode, tmp_path, monkeypatch, capsys):
    if mode == "dry-run":
        argv = [str(SOURCE), "codex", "--dry-run", "--workspace", str(tmp_path / "not-created")]
        expected = 0
    elif mode == "bad-bootstrap-pid":
        monkeypatch.setattr(os, "getpid", lambda: 2)
        argv = [str(SOURCE), "--internal-bootstrap"]
        expected = 1
    elif mode == "bad-guard-namespace":
        same = {name: os.readlink("/proc/self/ns/" + name) for name in ("mnt", "pid")}
        argv = [str(SOURCE), "--internal-guard", json.dumps(same)]
        expected = 1
    else:
        monkeypatch.setattr(os, "getpid", lambda: 1)
        libc = Mock()
        libc.prctl.return_value = 0
        monkeypatch.setattr(s.ctypes, "CDLL", lambda *a, **kw: libc)
        monkeypatch.setattr(subprocess, "run", Mock(return_value=Mock(stdout="--seccomp")))
        child = Mock()
        child.wait.return_value = -signal.SIGTERM
        monkeypatch.setattr(subprocess, "Popen", Mock(return_value=child))
        argv = [str(SOURCE), "--internal-bootstrap"]
        expected = 128 + signal.SIGTERM
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SOURCE), run_name="__main__")
    assert exc.value.code == expected
    captured = capsys.readouterr()
    if mode == "dry-run":
        assert json.loads(captured.out)["auth_mode"] == "auto"
        assert not (tmp_path / "not-created").exists()
    elif mode != "terminated-bootstrap":
        assert "agent-sandbox:" in captured.err and "Traceback" not in captured.err
