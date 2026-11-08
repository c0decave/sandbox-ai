"""Several agents in one setup, mode 2: other agents in the same sandbox (--with AGENT). They come with their
logins, their providers' hosts and their managed hooks under the run's rules, in the team image (or the host's
executables with Bubblewrap); the lead calls them from the shell."""
import json
import os
from pathlib import Path
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10: the same parser as a package
    import tomli as tomllib

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write logins: never into the real HOME, also when conftest.py was not run

ROOT = Path(__file__).resolve().parents[1]
s = fm.s
RULES = {"version": 1, "tool": [{"id": "no-curl", "tool": "Bash", "args": {"command": {"pattern": "(?s).*curl.*"}},
                                 "decision": "deny"}]}


def others(m):  # noqa: F811
    return [agent for agent in ("claude", "codex", "opencode") if agent != m.agent]


def with_options(agents):
    return [x for agent in agents for x in ("--with", agent)]


def test_the_others_join_with_their_hosts_and_the_team_image(m):  # noqa: F811
    plan = m.plan(*with_options(others(m)))
    assert plan["companions"] == others(m)
    hosts = set(plan["allowed_hosts"]) if "allowed_hosts" in plan else None
    a = s.parse_args([m.agent, *with_options(others(m))])
    assert a.image == s.TEAM_IMAGE
    assert set(s.allowed_hosts(a)) >= {h for agent in s.team(a) for h in s.PROVIDERS[agent]}
    assert s.parse_args([m.agent, "--with", others(m)[0], "--image", "x/y:1"]).image == "x/y:1"
    assert hosts is None or hosts >= set(s.PROVIDERS[others(m)[0]])


def test_their_logins_are_copied_and_the_lead_learns_about_them(m):  # noqa: F811
    logins = m.tmp / "logins"
    logins.mkdir()
    sources = {}
    for agent in ("claude", "codex", "opencode"):
        path = logins / f"{agent}.json"
        login = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-" + "a" * 40, "refreshToken": "sk-ant-ort01-" + "r" * 40,
                                   "expiresAt": 4102444800000, "scopes": ["user:inference"]}} \
            if agent == "claude" else {"agent": agent}
        path.write_text(json.dumps(login))
        path.chmod(0o600)
        sources[agent] = path
    m.monkeypatch.setattr(s, "auth_sources", lambda name: [(sources[name], s.AUTH_TARGETS[name])])
    run = m.run(*with_options(others(m)))
    for agent in others(m):
        assert s.AUTH_TARGETS[agent] in run.home_files, f"{agent}'s login"
    assert run.env["SANDBOX_AI_COMPANIONS"] == " ".join(others(m))
    if "codex" in others(m):
        assert tomllib.loads(run.home_text[".codex/config.toml"])["sandbox_mode"] == "danger-full-access"
    if "claude" in others(m):
        assert ".claude/settings.json" in run.home_files
    # --no-auth holds for them too.
    run = m.run("--no-auth", *with_options(others(m)))
    assert not any(s.AUTH_TARGETS[agent] in run.home_files for agent in others(m))


def test_the_rules_hold_for_every_agent_of_the_sandbox(m, tmp_path):  # noqa: F811
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    run = m.run("--rules", str(rules), *with_options(others(m)))
    assert set(run.config["companion_hook_agents"]) == set(others(m))
    if "claude" in s.team(run.a):
        assert "/etc/claude-code" in run.joined
    if "codex" in s.team(run.a):
        assert "/etc/codex" in run.joined
    files = s.hook_files(run.a)
    for agent in others(m):
        if agent == "claude":
            commands = [h["command"] for g in json.loads(files["claude-code/managed-settings.json"])["hooks"]
                        ["PreToolUse"] for h in g["hooks"]]
            assert commands == ["/usr/bin/python3 -I /sandbox/runtime.py --internal-hook claude PreToolUse"]
        elif agent == "codex":
            requirements = tomllib.loads(files["codex/requirements.toml"])
            assert requirements["allow_managed_hooks_only"] is True
            assert "--internal-hook codex PreToolUse" in requirements["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        else:
            assert json.loads(run.env["OPENCODE_CONFIG_CONTENT"])["plugin"] == ["file://" + s.OPENCODE_HOOK_PLUGIN]
    # Without rules no managed hooks for them (as for the lead).
    plain = m.run(*with_options(others(m)))
    assert plain.config["companion_hook_agents"] == []


def test_with_is_checked():
    with pytest.raises(SystemExit):
        s.parse_args(["claude", "--with", "gemini"])
    a = s.parse_args(["claude", "--with", "claude"])
    with pytest.raises(ValueError, match="that is the agent of this start"):
        s.check_companions(a)
    a = s.parse_args(["claude", "--with", "codex", "--probe"])
    with pytest.raises(ValueError, match="agent's start"):
        s.check_companions(a)
    assert s.companions(s.parse_args(["claude", "--with", "codex", "--with", "codex"])) == ["codex"]


def test_an_old_codex_beside_the_lead_refuses_the_run():
    rt = fm.load("runtime_for_companion_tests", "sandbox/runtime.py") if hasattr(fm, "load") else None
    if rt is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("runtime_for_companion_tests", ROOT / "sandbox/runtime.py")
        rt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rt)

    def version(text):
        return lambda command, **kwargs: type("Done", (), {"stdout": text})()
    cfg = {"command": ["claude"], "hooks": {"agent": "claude"}, "hook_agent": "claude",
           "companion_hook_agents": ["codex"]}
    assert "--with codex" in rt.hook_problem(cfg, run=version("codex-cli 0.130.0"))
    assert rt.hook_problem(cfg, run=version("codex-cli 0.131.0")) is None


def test_build_the_team_image(tmp_path):
    log = tmp_path / "build.json"
    (tmp_path / "podman").write_text("#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\n"
                                     "Path(os.environ['BUILD_LOG']).write_text(json.dumps(sys.argv[1:]))\n")
    (tmp_path / "npm").write_text("#!/bin/sh\necho 9.8.7\n")  # npm view PACKAGE version
    for name in ("podman", "npm"):
        (tmp_path / name).chmod(0o700)
    environment = {**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin", "BUILD_LOG": str(log)}
    result = subprocess.run([str(ROOT / "sandbox-build-image"), "team", "--build-arg", "CODEX_VERSION=1.2.3"],
                            capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stderr
    argv = json.loads(log.read_text())
    if not os.path.exists(f"/run/user/{os.getuid()}/bus"):  # no systemd user session: cgroupfs (as for every build)
        assert argv[0] == "--cgroup-manager=cgroupfs" and "no systemd user session" in result.stderr
        argv = argv[1:]
    assert argv[:4] == ["build", "--tag", "localhost/team-sandbox:latest", "--label"]
    assert "sandbox-ai.codex-version=1.2.3" in argv and "sandbox-ai.claude-version=9.8.7" in argv
    assert "CLAUDE_VERSION=9.8.7" in argv and "CODEX_VERSION=9.8.7" not in argv, "the pinned one stays"
    assert argv[argv.index("--file") + 1] == str(ROOT / "sandbox" / "Containerfile.team")
    recipe = (ROOT / "sandbox" / "Containerfile.team").read_text()
    for package in ("@anthropic-ai/claude-code@", "@openai/codex@", "opencode-ai@"):
        assert package in recipe
    refused = subprocess.run([str(ROOT / "sandbox-build-image"), "--pin", "1.2.3", "team"], capture_output=True,
                             text=True, env=environment)
    assert refused.returncode == 2 and "not team" in refused.stderr and "not all" not in refused.stderr


# --- what the review found: strict modes, providers, manual logins, the store ---------------------------------

def test_strict_modes_refuse_other_agents_and_their_hosts_are_providers(m):  # noqa: F811
    other = others(m)[0]
    for mode in ("high", "paranoid"):
        m.refused("--mode", mode, "--with", other, match="--with .* widens the security mode")
    a = s.parse_args([m.agent, "--with", other])
    assert set(s.PROVIDERS[other]) <= set(s.provider_hosts(a)), "their API calls are a provider's, not a stranger's"
    s.check_policy(a, {"forbid": ["--image"]})  # the team image is the default of --with, not a choice


def test_other_agents_need_their_provider():
    for extra in (["--offline"],):
        a = s.parse_args(["claude", "--with", "codex", *extra])
        with pytest.raises(ValueError, match="--offline"):
            s.check_companions(a)
    a = s.parse_args(["claude", "--with", "codex"])
    a.llm = "local"
    with pytest.raises(ValueError, match="--llm"):
        s.check_companions(a)


def test_a_copied_login_of_another_agent_is_kept(tmp_path, monkeypatch):
    """--copy SRC:.claude/.credentials.json: that account stays, never replaced by (or written back to) the host's."""
    source = tmp_path / "other-account.json"
    source.write_text("{}")
    a = s.parse_args(["codex", "--with", "claude", "--copy", f"{source}:.claude/.credentials.json"])
    assert s.companion_auth(a, "claude") == "manual" and s.companion_auth(a, "codex") == "auto"
    assert s.claude_credential_pair(a, tmp_path / "home") is None
    home = tmp_path / "home"
    home.mkdir()
    host = tmp_path / "host.json"
    host.write_text('{"host": 1}')
    monkeypatch.setattr(s, "auth_sources", lambda agent: [(host, s.AUTH_TARGETS[agent])])
    assert s.companion_logins(a, home) == []


def test_a_stored_login_of_another_agent_is_written_back(tmp_path, monkeypatch):
    """A Codex --with whose login comes from sandbox-ai's login store: a refresh in the run goes back to the store
    (its refresh token rotates), as for a Codex lead."""
    store, home = tmp_path / "store", tmp_path / "home"
    login = json.dumps({"tokens": {"access_token": "a", "refresh_token": "r1", "id_token": "i", "account_id": "x"}})
    for root in (store, home):
        (root / ".codex").mkdir(parents=True)
        (root / ".codex/auth.json").write_text(login)
    monkeypatch.setattr(s, "store_login", lambda agent: agent == "codex")
    monkeypatch.setattr(s, "auth_store", lambda agent: store)
    state = s.store_writeback_state(s.parse_args(["claude", "--with", "codex"]), home, "codex")
    assert state is not None and state["agent"] == "codex" and state["source"] == store / ".codex/auth.json"
    assert s.store_writeback_state(s.parse_args(["claude", "--with", "codex", "--no-auth"]), home, "codex") is None
    assert s.store_writeback_state(s.parse_args(["claude"]), home) is None, "the Claude lead's goes its own way"
