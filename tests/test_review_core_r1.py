"""Whole-codebase review, round 1 (core: launcher and runtime): each fix's test, failing without it."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(f"review_core_r1_{name}", ROOT / f"sandbox/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sessions = load("sessions")


# --- a restart never gives the agent its first task again ----------------------------------------------------------

@pytest.mark.parametrize("agent,args,expected", [
    # A preset's first task (presets.prompt_args): Qwen Code's and OpenCode's are option values.
    ("qwen", ["--prompt-interactive=-fix it"], ["--continue"]),
    ("qwen", ["--model", "m", "-i", "fix it"], ["--model", "m", "--continue"]),
    ("qwen", ["--model", "m", "--prompt-interactive", "fix it"], ["--model", "m", "--continue"]),
    ("opencode", ["--prompt", "-fix it"], ["--continue"]),
    ("opencode", ["--model=a/b", "--prompt=fix it"], ["--model=a/b", "--continue"]),
    # One task and exit, however written: never run again.
    ("qwen", ["--prompt", "fix it"], ["--continue"]),
    ("qwen", ["--model", "m", "--prompt=fix it"], ["--continue"]),
    ("hermes", ["--oneshot=fix it"], ["--continue"]),
    ("hermes", ["--query", "fix it"], ["--continue"]),
    ("hermes", ["--model", "m", "-q", "fix it"], ["--model", "m", "--continue"]),
    # What stays as it was.
    ("qwen", ["--model", "m", "--approval-mode", "yolo"], ["--model", "m", "--approval-mode", "yolo", "--continue"]),
    ("claude", ["--model", "opus", "fix it"], ["--model", "opus", "--continue"]),
    ("codex", ["-p", "work", "--sandbox", "read-only"], ["-p", "work", "--sandbox", "read-only", "resume", "--last"]),
])
def test_a_restart_never_repeats_the_first_task(agent, args, expected):
    assert sessions.restart_args({"agent": agent, "options": [], "agent_args": args}) == expected


# --- a kept HOME's review names what the newer agents run at the next start ---------------------------------------

home_review = load("home_review")


@pytest.mark.parametrize("relative", [
    ".hermes/config.yaml", ".hermes/skills/x/SKILL.md", ".qwen/settings.json", ".qwen/QWEN.md",
    ".qwen/extensions/x/qwen-extension.json", ".qwen/commands/x.toml", ".qwen/agents/x.md", ".dsh/AGENTS.md",
    ".dsh/skills/x/SKILL.md", ".config/goose/config.yaml", ".config/goose/.goosehints", ".config/goose/AGENTS.md",
    ".config/goose/skills/x/SKILL.md", ".agents/skills/x/SKILL.md",
])
def test_a_kept_homes_review_names_the_newer_agents_files(tmp_path, relative):
    home, start = tmp_path / "home", tmp_path / "start"
    start.mkdir()
    (home / relative).parent.mkdir(parents=True)
    (home / relative).write_text("before\n")
    home_review.record(home, start)
    (home / relative).write_text("mcpServers: an agent's server that runs at the next start\n")
    review = home_review.compare(home, start)
    assert (relative, "changed") in {(entry["path"], entry["change"]) for entry in review["changes"]}


def launcher():
    return load("agent_sandbox")


# --- the host's Goose keys never reach a Goose sandbox ---------------------------------------------------------------

def test_a_goose_start_never_copies_the_hosts_goose_secrets(tmp_path, monkeypatch):
    """GOOSE_REVIEW.md: "nothing of the host's Goose is copied in" - the automatic login copy took the host's
    ~/.config/goose/secrets.yaml (every provider key `goose configure` kept there) into the sandbox's HOME."""
    s = launcher()
    host = tmp_path / "host"
    (host / ".config/goose").mkdir(parents=True)
    (host / ".config/goose/secrets.yaml").write_text("OPENAI_API_KEY: sk-host-key-of-the-user\n")
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    home = tmp_path / "sandbox-home"
    home.mkdir()
    copied = s.prepare_home(s.parse_args(["goose", "--workspace", str(tmp_path)]), home)
    assert not (home / ".config/goose/secrets.yaml").exists() and copied == []
    # --copy-auth, which copies the agent's login, has none to copy for Goose: said, nothing taken.
    with pytest.raises(ValueError, match="goose has no login"):
        s.prepare_home(s.parse_args(["goose", "--copy-auth", "--workspace", str(tmp_path)]), home)
    assert not (home / ".config/goose/secrets.yaml").exists()


def test_macos_checks_the_log_and_vnc_folders_are_shared_with_podman_machine(tmp_path):
    """check_machine_shares: every host path a start mounts lies in a share of podman machine - --log-dir and
    --gui-vnc are bind mounts as well, and were not checked (podman failed later with its own words)."""
    s = launcher()
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "w"), "--log-dir", "/Volumes/ext/logs",
                      "--gui-vnc", "/Volumes/ext/vnc"])
    paths = dict(s.machine_paths(a))
    assert paths["--log-dir"] == Path("/Volumes/ext/logs") and paths["--gui-vnc"] == Path("/Volumes/ext/vnc")


def test_an_ended_runs_leftovers_go_with_its_browsers_vnc_folder(tmp_path):
    liveness = load("liveness")
    run = tmp_path / "runs" / "claude-0123abcd"
    for path in (run, run.with_name(run.name + ".browser"), run.with_name(run.name + ".browser-vnc")):
        path.mkdir(parents=True)
        (path / "x").write_text("x")
    other = tmp_path / "runs" / "claude-0123abcd-other.browser-vnc"
    other.mkdir()
    liveness.discard(run)
    assert sorted(p.name for p in run.parent.iterdir()) == [other.name]


def test_the_web_opens_no_terminal_for_an_agent_the_command_does_not_take():
    """A login terminal for DSH or Goose (no sandbox-ai login of theirs), the plugin store with Hermes or a team with
    an agent that is none: refused with the agents that are taken, not opened to fail there; a refusal never says
    "claude, codex or opencode" where seven agents are taken (a team takes all seven: hotfix 0.1.96)."""
    w = load("webui")
    for query, taken in (({"kind": "login", "agent": "goose"}, "hermes, qwen or pi"),
                         ({"kind": "login", "agent": "dsh"}, "hermes, qwen or pi"),
                         # (plugins for Codex and OpenCode 2 from kits: the plugin store takes these since 0.1.98)
                         ({"kind": "plugins", "agent": "hermes"}, "claude, codex, opencode, opencode2 or all"),
                         ({"kind": "team-start", "team": "t1", "agents": "claude,bash", "workspace": "/srv/w"},
                          "claude, codex, opencode, hermes, dsh, qwen, goose")):
        with pytest.raises(w.Refused, match=taken):
            w.terminal_command({"host": "local", **query})
    assert w.terminal_command({"host": "local", "kind": "login", "agent": "qwen"})[1][-1] == "qwen"
    with pytest.raises(w.Refused, match="goose, openhands, nanobot, letta or pi"):
        w.terminal_command({"host": "local", "kind": "start", "agent": "nobody"})


@pytest.mark.parametrize("agent,args,interactive", [
    ("qwen", ["--prompt", "fix it"], False), ("qwen", ["--prompt=fix it"], False),
    ("hermes", ["--oneshot=fix it"], False), ("hermes", ["-z", "fix it"], False),
    ("qwen", ["--prompt-interactive=fix it"], True), ("opencode", ["--prompt", "fix it"], True),
    ("claude", ["--model", "opus"], True), ("claude", ["-p", "x"], False),
])
def test_a_one_shot_task_runs_without_tmux(agent, args, interactive):
    """resolve_tmux: no tmux for commands that print and exit - Qwen Code's --prompt and Hermes' --oneshot=TASK ran
    in tmux, whose window went with the answer."""
    assert launcher().interactive_args(agent, args) is interactive


@pytest.mark.parametrize("name,saved", [("git_logins", []), ("publish_logins", []), ("identities", {})])
def test_a_settings_file_is_saved_after_a_write_that_failed(tmp_path, monkeypatch, name, saved):
    """git-logins.json, publish-logins.json, identities.json: a temporary file of a failed write (its PID's name) was
    left behind, and every later save of a process of that PID failed on it (FileExistsError)."""
    import os
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    module = load(name)
    file = module.path()
    file.parent.mkdir(parents=True)
    file.with_name(f".{file.name}.{os.getpid()}").write_text("{half")  # what a write cut off left
    module.save(saved)
    assert json.loads(file.read_text())["version"] == 1
    monkeypatch.setattr(json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        module.save(saved)
    assert sorted(p.name for p in file.parent.iterdir()) == sorted([file.name, f".{file.name}.{os.getpid()}"])


def test_hermes_env_file_is_watched_but_its_keys_never_kept(tmp_path):
    home, start = tmp_path / "home", tmp_path / "start"
    start.mkdir()
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes/.env").write_text("OPENROUTER_API_KEY=sk-or-v1-0123456789\n")
    home_review.record(home, start)
    manifest = (start / home_review.MANIFEST).read_text()
    assert ".hermes/.env" in json.loads(manifest)["files"] and "sk-or-v1" not in manifest
