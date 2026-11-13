"""Several agents in one setup, mode 1: another agent as the helper (--helper AGENT). The helper runs as an MCP
server in a sandbox of its own, with its login, its provider's hosts, kits, rules and hooks (unlike other MCP
servers), on the lead's main workspace; the lead gives it tasks through the relay (its tool ask, agent_mcp.py)."""
import json
from pathlib import Path
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10: the same parser as a package
    import tomli as tomllib
from unittest.mock import Mock

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)
import test_mcp as tm

ROOT = Path(__file__).resolve().parents[1]
s = fm.s
RULES = {"version": 1, "tool": [{"id": "no-curl", "tool": "Bash", "args": {"command": {"pattern": "(?s).*curl.*"}},
                                 "decision": "deny"}]}


# --- the lead's start -----------------------------------------------------------------------------------

def test_a_helper_is_an_mcp_server_of_the_run(m, tmp_path):  # noqa: F811
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    others = [agent for agent in ("claude", "codex", "opencode") if agent != m.agent]
    run = m.run(*[x for agent in others for x in ("--helper", agent)], "--rules", str(rules))
    names = [s.helper_name(agent, str(m.work.resolve())) for agent in others]
    assert m.mcp_started == names, "each helper is started (or reused) before the lead's sandbox"
    assert all(len(name) <= 32 and name.startswith("helper-") for name in names)
    for agent, name in zip(others, names):
        definition = run.a.helper_definitions[name]
        assert definition["helper"] == agent and definition["workspace"] == str(m.work.resolve())
        assert definition["rules"] == [str(rules)] and definition["skip_permissions"] is False
    assert set(run.config["mcp_relay"]) == set(names), "the lead reaches them only through the relay"


def test_one_helper_per_agent_and_workspace(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    assert s.helper_name("codex", one) != s.helper_name("codex", two), "another project never gets this one's"
    assert s.helper_name("codex", one) == s.helper_name("codex", one) != s.helper_name("claude", one)


def test_a_helper_takes_the_leads_permissions_and_project_rules(tmp_path):
    a = s.parse_args(["claude", "--helper", "codex", "--helper", "codex", "--dangerously-skip-permissions"])
    a.workspaces, a.project_document = [str(tmp_path)], {"rules": RULES}
    s.add_helpers(a)
    (name,) = a.helper_definitions
    assert a.mcp == [name], "named once"
    assert a.helper_definitions[name]["skip_permissions"] is True
    assert a.helper_definitions[name]["project_rules"] == RULES


def test_helpers_are_for_an_agents_start_only(tmp_path):
    a = s.parse_args(["codex", "--helper", "claude", "--mcp-socket-dir", str(tmp_path / "sock")])
    a.workspaces = [str(tmp_path)]
    with pytest.raises(ValueError, match="--helper is for an agent's start"):
        s.add_helpers(a)
    with pytest.raises(SystemExit):
        s.parse_args(["codex", "--helper", "gemini"])


def test_no_file_makes_a_sandbox_a_helper_or_an_mcp_server_start_one():
    assert "--helper-of" in s.CONFIG_FORBIDDEN
    mcp = tm.m
    for option in ("--helper", "--helper-of"):
        with pytest.raises(ValueError, match="not allowed"):
            mcp.validated({"x": {"command": ["true"], "options": [option, "codex"]}}, "test")


# --- the helper's own sandbox (mcp.py) ----------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(tm.m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()


def test_the_helper_starts_with_its_login_provider_rules_and_server(isolated, tmp_path, monkeypatch):
    mcp, sa = tm.m, tm.s
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    a = sa.parse_args(["claude", "--backend", "podman", "--helper", "codex", "--rules", str(rules),
                       "--dangerously-skip-permissions"])
    a.workspaces, a.project_document = [str(tmp_path)], {"rules": RULES}
    sa.add_helpers(a)
    (name,) = a.helper_definitions
    launched = {}

    def popen(cmd, **kwargs):
        if "--internal-log" in cmd:
            return Mock(pid=4320)
        launched.update(cmd=cmd, **kwargs)
        (sa.mcp_socket_dir(name) / "server.sock").touch()
        tm.fake_supervisor(tmp_path, 4321, name, start=31337)
        return Mock(pid=4321, poll=Mock(return_value=None))
    monkeypatch.setattr(mcp.subprocess, "Popen", popen)
    monkeypatch.setattr(mcp.launcher(), "remove_stale_pod", Mock(return_value=False))
    assert mcp.ensure_running(name, a) == "started"
    cmd = launched["cmd"]
    assert cmd[:4] == [sys.executable, "-I", str(ROOT / "sandbox/launcher/agent_sandbox.py"), "codex"]
    assert "--no-auth" not in cmd and "--no-provider-defaults" not in cmd, "its login and its provider's hosts"
    assert cmd[cmd.index("--helper-of") + 1] == name and "--dangerously-skip-permissions" in cmd
    given = [cmd[i + 1] for i, value in enumerate(cmd) if value == "--rules"]
    project = mcp.state_dir(name) / "project-rules.json"
    assert given == [str(rules), str(project)] and json.loads(project.read_text()) == RULES
    assert project.stat().st_mode & 0o777 == 0o600
    assert cmd[cmd.index("--workspace") + 1] == str(tmp_path) and cmd[cmd.index("--binary") + 1] == "python3"
    assert f"{ROOT / 'sandbox/agent_mcp.py'}:/opt/sandbox-ai/agent_mcp.py:ro" in cmd
    serve = cmd.index("--internal-mcp-serve")
    assert cmd[cmd.index("--", serve) + 1:] == ["python3", "-I", "/opt/sandbox-ai/agent_mcp.py", "codex",
                                               "--skip-permissions"]
    assert mcp.load_state(name)["helper"] == "codex"
    assert cmd[cmd.index("--mode") + 1] == "medium" and "--workspace-ro" not in cmd and "--no-auth" not in cmd
    # Another lead (OpenCode) on the same workspace with the same rules shares the running helper.
    (sa.mcp_socket_dir(name) / "server.sock").touch()
    same = sa.parse_args(["opencode", "--backend", "podman", "--helper", "codex", "--rules", str(rules),
                          "--dangerously-skip-permissions"])
    same.workspaces, same.project_document = [str(tmp_path)], {"rules": RULES}
    sa.add_helpers(same)
    assert same.helper_definitions[name] == a.helper_definitions[name], "the lead is no part of the definition"
    monkeypatch.setattr(mcp.subprocess, "Popen", Mock(side_effect=AssertionError("started twice")))
    assert mcp.ensure_running(name, same) == "running"
    # Other rules for it: never shared, the running one must be stopped first.
    other = sa.parse_args(["claude", "--backend", "podman", "--helper", "codex"])
    other.workspaces, other.project_document = [str(tmp_path)], {}
    sa.add_helpers(other)
    with pytest.raises(ValueError, match="different definition"):
        mcp.ensure_running(name, other)


def test_inside_the_helpers_sandbox_its_kits_rules_and_hooks_hold(m, tmp_path):  # noqa: F811
    """The helper's start (what mcp.helper_command runs) is an agent's: kits, its managed hooks with the rules,
    and no permission flags on its server's command line."""
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    sock = tmp_path / "sock"
    run = m.run("--helper-of", "helper-x", "--mcp-socket-dir", str(sock), "--rules", str(rules),
                "--dangerously-skip-permissions", "--binary", "python3",
                agent_args=["-I", "/sandbox/runtime.py", "--internal-mcp-serve", "/sandbox/mcp-sock/server.sock",
                            "--", "python3", "-I", "/opt/sandbox-ai/agent_mcp.py", m.agent])
    assert s.kits_apply(run.a) and s.hooks_active(run.a) and run.config["hooks"]["agent"] == m.agent
    assert "python3" in run.config["command"][0] and run.config["command"][1] == "-I", \
        "no agent flags in front of its server"
    assert not set(run.config["command"]) & set(s.DANGER_ARGS[m.agent])
    if m.agent == "opencode":
        plugins = json.loads(run.env["OPENCODE_CONFIG_CONTENT"])["plugin"]
        assert "file://" + s.OPENCODE_HOOK_PLUGIN in plugins, "the agent it runs for each task has the hooks"
    elif m.agent == "claude":
        assert run.env.get("IS_SANDBOX") == "1"


def test_codex_helper_settings_hold_in_its_managed_configuration(m, tmp_path):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex only")
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    a = s.parse_args(["codex", "--helper-of", "helper-x", "--mcp-socket-dir", str(tmp_path / "sock"),
                      "--rules", str(rules), "--workspace", str(m.work)])
    requirements = tomllib.loads(s.hook_files(a)["codex/requirements.toml"])
    assert requirements["allow_managed_hooks_only"] is True and requirements["hooks"]["PreToolUse"]


# --- what narrows the lead narrows its helper ---------------------------------------------------------------

def test_the_leads_read_only_workspace_mode_and_no_login_reach_the_helper(isolated, tmp_path, monkeypatch):
    mcp, sa = tm.m, tm.s
    a = sa.parse_args(["claude", "--backend", "bwrap", "--helper", "opencode", "--workspace-ro", "--no-auth",
                       "--mode", "low"])
    a.workspaces, a.project_document, a.mode = [str(tmp_path)], {}, "low"
    sa.add_helpers(a)
    (name,) = a.helper_definitions
    cmd = mcp.helper_command(name, mcp.server_network(name, a.helper_definitions[name]), a, mcp.state_dir(name),
                             sa.mcp_socket_dir(name), None)
    assert cmd[cmd.index("--mode") + 1] == "low" and "--workspace-ro" in cmd and "--no-auth" in cmd
    assert cmd[cmd.index("--telemetry") + 1] == "off", "the lead's telemetry level holds for its helper"
    strict = sa.parse_args(["claude", "--backend", "bwrap", "--helper", "opencode", "--telemetry", "strict"])
    strict.workspaces, strict.project_document = [str(tmp_path)], {}
    sa.add_helpers(strict)
    cmd = mcp.helper_command(name, mcp.server_network(name, strict.helper_definitions[name]), strict,
                             mcp.state_dir(name), sa.mcp_socket_dir(name), None)
    assert cmd[cmd.index("--telemetry") + 1] == "strict"


def test_helpers_need_their_provider(tmp_path):
    a = s.parse_args(["claude", "--helper", "codex", "--offline"])
    a.workspaces = [str(tmp_path)]
    with pytest.raises(ValueError, match="--offline"):
        s.add_helpers(a)
    a = s.parse_args(["claude", "--helper", "codex"])
    a.workspaces, a.llm = [str(tmp_path)], "local"
    with pytest.raises(ValueError, match="--llm"):
        s.add_helpers(a)


def test_strict_modes_and_the_policy_refuse_helpers(m):  # noqa: F811
    other = next(agent for agent in ("claude", "codex", "opencode") if agent != m.agent)
    for mode in ("high", "paranoid"):
        m.refused("--mode", mode, "--helper", other, match="--helper .* widens the security mode")
    a = s.parse_args([m.agent, "--helper", other])
    with pytest.raises(ValueError, match="--mcp"):
        s.check_policy(a, {"forbid": ["--mcp"]})  # a helper is an MCP server with network
    s.check_policy(s.parse_args([m.agent]), {"forbid": ["--mcp"]})


def test_a_codex_helpers_version_is_checked_not_its_servers_python():
    rt = tm.load("runtime_for_helper_version_test", "runtime.py")
    asked = []

    def version(command, **kwargs):
        asked.append(command[0])
        return type("Done", (), {"stdout": "codex-cli 0.130.0" if command[0] == "codex" else "Python 3.11.2"})()
    cfg = {"command": ["python3", "-I", "/sandbox/runtime.py", "--internal-mcp-serve", "s", "--", "python3"],
           "hooks": {"agent": "codex"}, "hook_agent": "codex", "hook_binary": "codex"}
    assert "older than 0.131.0" in rt.hook_problem(cfg, run=version) and asked == ["codex"]


def test_codex_waits_long_enough_for_a_helper_or_a_board():
    a = s.parse_args(["codex", "--mcp", "demo", "--mcp", "helper-claude-12345678", "--mcp", "team-app"])
    args = s.mcp_agent_args(a)
    timeouts = [v for v in args if "tool_timeout_sec" in v]
    assert timeouts == [f"mcp_servers.helper-claude-12345678.tool_timeout_sec={s.LONG_TOOL_TIMEOUT}",
                        f"mcp_servers.team-app.tool_timeout_sec={s.LONG_TOOL_TIMEOUT}"]
    assert s.LONG_TOOL_TIMEOUT > 1800, "longer than a helper's task may run"


def test_the_lead_cannot_plant_settings_for_the_helper(m, tmp_path):  # noqa: F811
    """/work is the lead's too: the helper reads no project settings or MCP servers from there."""
    run = m.run("--helper-of", "helper-x", "--mcp-socket-dir", str(tmp_path / "sock"), "--binary", "python3",
                agent_args=["-I", "/sandbox/runtime.py", "--internal-mcp-serve", "/sandbox/mcp-sock/server.sock",
                            "--", "python3", "-I", "/opt/sandbox-ai/agent_mcp.py", m.agent])
    if m.agent == "opencode":
        assert run.env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    elif m.agent == "codex":
        assert "projects" not in run.home_text.get(".codex/config.toml", ""), "/work is not trusted for settings"
    if m.backend == "bwrap":  # the agent its server runs is the host's, found like a lead's: missing, no start
        tools = m.tmp / "bin"
        m.monkeypatch.setattr(s.shutil, "which", lambda name: None if name == m.agent else
                              str(tools / name) if (tools / name).exists() else "/usr/bin/" + name)
        m.refused("--helper-of", "helper-x", "--mcp-socket-dir", str(tmp_path / "sock2"), "--binary", "python3",
                  match=f"Agent executable missing: {m.agent} \\(the helper\\)")
