"""What the big review of 2026-11-05 found, each with the scenario that showed it."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import tomllib

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
s = fm.s


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- a HOME of your own outlives the run: refused where nothing may -----------------------------------------

def test_paranoid_refuses_a_home_of_your_own(m, tmp_path):  # noqa: F811
    home = tmp_path / "my-home"
    m.refused("--mode", "paranoid", "--state-dir", str(home), match="--state-dir .* widens the security mode paranoid")
    assert m.run("--mode", "high", "--state-dir", str(home)).a.state_dir, "high keeps HOMEs, its own too"


def test_forbid_keep_refuses_a_home_of_your_own(m, tmp_path):  # noqa: F811
    m.monkeypatch.setattr(s, "load_policy", lambda path=None: {"forbid_keep": True})
    m.refused("--state-dir", str(tmp_path / "my-home"), match="--state-dir keeps the sandbox HOME beyond the run")
    assert not m.run().a.keep_home, "without it the HOME is discarded, as forbid_keep says"


# --- a helper and the lead's /work/.git -------------------------------------------------------------------

def test_a_helper_runs_no_fsmonitor_hooks_or_filters_of_the_shared_repository(m, tmp_path):  # noqa: F811
    run = m.run("--helper-of", "helper-x", "--mcp-socket-dir", str(tmp_path / "sock"), "--binary", "python3",
                agent_args=["-I", "/sandbox/runtime.py", "--internal-mcp-serve", "/sandbox/mcp-sock/server.sock",
                            "--", "python3", "-I", "/opt/sandbox-ai/agent_mcp.py", m.agent])
    env = run.env
    settings = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert settings["core.fsmonitor"] == "false" and settings["core.hooksPath"]
    assert env["GIT_ATTR_SOURCE"] == s.EMPTY_TREE
    lead = m.run()
    assert "GIT_ATTR_SOURCE" not in lead.env, "the lead's own repository works as always"


def test_git_runs_no_planted_fsmonitor_with_the_helpers_settings(tmp_path):
    """The real git: a /work/.git/config with core.fsmonitor that would run a command, and the helper's
    environment: git status runs it not."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    marker = tmp_path / "ran"
    subprocess.run(["git", "-C", str(work), "config", "core.fsmonitor", f"touch {marker}; false"], check=True)
    (work / "a.txt").write_text("x")
    env = {**os.environ, "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false",
           "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": "/dev/null", "GIT_ATTR_SOURCE": s.EMPTY_TREE}
    subprocess.run(["git", "-C", str(work), "status", "--porcelain"], env=env, capture_output=True, check=False)
    assert not marker.exists()


# --- sandbox-ai gc --older-than 0 ----------------------------------------------------------------------------

def test_gc_older_than_zero_removes_every_stopped_kept_start(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    starts = load("starts_for_review_tests", "starts.py")
    sessions = starts.module("sessions")
    key = "old"
    directory = sessions.session_dir(key)
    (directory / "home").mkdir(parents=True)
    sessions.save(key, {"agent": "claude", "options": [], "agent_args": [], "backend": "bwrap", "podman_url": None,
                        "workspace": str(tmp_path), "cwd": str(tmp_path), "created": sessions.now(), "ready": True,
                        "name": "old", "keep": True, "mode": "tmux", "pid": 2 ** 22 + 1, "started": sessions.now()})
    kept = starts.gc(None, dry_run=True)
    assert kept["removed"] == [], "no keep_days, no --older-than: kept starts never expire"
    result = starts.gc(0)
    assert result["days"] == 0 and [r["name"] for r in result["removed"]] == ["old"], result
    assert not directory.exists(), "0 days: every stopped kept start goes (it was 'no expiry')"


# --- Codex's requirements.toml with characters beyond the BMP ------------------------------------------------

def test_a_hook_with_an_emoji_keeps_requirements_toml_valid():
    a = s.parse_args(["codex"])
    groups = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo 🚫 blocked"}]}]}
    text = s.agent_hook_files(a, "codex", groups)["codex/requirements.toml"]
    assert tomllib.loads(text)["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo 🚫 blocked"
    with pytest.raises(ValueError, match="unpaired surrogate"):
        s.toml_string("x\ud800y")
    k = load("kits_for_review_tests", "kits.py")
    with pytest.raises(ValueError):  # never approved in the first place
        k.hook_groups("codex", {"PreToolUse": [{"hooks": [{"type": "command", "command": "a\ud800"}]}]})


# --- two starts of one name at once; a start being made --------------------------------------------------------

def test_a_name_saved_meanwhile_refuses_the_second_start(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    sessions = load("sessions_for_review_tests", "sessions.py")
    data = {"agent": "claude", "options": [], "agent_args": [], "backend": "bwrap", "podman_url": None,
            "workspace": str(tmp_path), "cwd": str(tmp_path), "created": sessions.now(), "ready": False,
            "name": "web", "keep": True, "mode": "tmux"}
    sessions.session_dir("web").mkdir(parents=True)
    sessions.claim_name("web", "web", dict(data))
    sessions.session_dir("web-ab12").mkdir(parents=True)  # the second start's key, chosen beside the first
    with pytest.raises(ValueError, match="taken meanwhile"):
        sessions.claim_name("web-ab12", "web", dict(data))
    assert not (sessions.session_dir("web-ab12") / "session.json").exists()


def test_prune_leaves_a_start_being_made_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sessions = load("sessions_for_prune_tests", "sessions.py")
    data = {"agent": "claude", "options": [], "agent_args": [], "backend": "bwrap", "podman_url": None,
            "workspace": str(tmp_path), "cwd": str(tmp_path), "created": sessions.now(), "ready": False,
            "name": "task", "keep": False, "mode": "tmux"}
    sessions.session_dir("task").mkdir(parents=True)
    sessions.save("task", data)  # saved, no supervisor yet: looks stopped
    with sessions.session_lock("task"):  # its start holds the lock
        assert sessions.prune("task") is False and sessions.session_dir("task").exists()
    assert sessions.prune("task") is True and not sessions.session_dir("task").exists(), "once it is really over"


# --- kits remove-hook: the file named, nothing else ------------------------------------------------------------

def test_remove_hook_removes_exactly_the_file_named(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    kc = load("kitctl_for_review_tests", "kitctl.py")
    out = []
    assert kc.main(["new", "mine", "--agent", "claude"], out=out.append) == 0
    for name in ("a.sh", "a.sh.bak"):
        script = tmp_path / name
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        assert kc.main(["add-hook", "mine", str(script)], out=out.append) == 0
    assert kc.main(["remove-hook", "mine", "a.sh"], out=out.append) == 0
    hooks = json.loads((kc.kits().kits_dir() / "mine/claude/hooks/hooks.json").read_text())["hooks"]
    commands = [h["command"] for g in hooks["PreToolUse"] for h in g["hooks"]]
    assert commands == ["${HOOK_DIR}/a.sh.bak"], "a.sh.bak is no a.sh"
    assert kc.main(["remove-hook", "mine", "typo.sh"], out=out.append) == 1, "nothing removed: never 'removed'"
