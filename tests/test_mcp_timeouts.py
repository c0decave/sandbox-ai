"""How long an agent waits for one call of an --mcp server (0.1.95): Codex waited 60 s (its own default) for every
server but a helper and a team board, and cut off servers whose tools run longer (exec: commands up to 600 s). Every
--mcp server now gets its wait in every agent's configuration - the server's tool_timeout (its definition: from its
catalog entry, `mcp set --tool-timeout`, a project file; 10-3600 s), a helper's and a board's LONG_TOOL_TIMEOUT, else
DEFAULT_MCP_TOOL_TIMEOUT - and Codex and Claude Code a longer wait for a server's start (MCP_STARTUP_TIMEOUT)."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_mcp_timeouts_test", "agent_sandbox.py")
m = load("sandbox_mcp_timeouts_test", "mcp.py")
DIGEST = "sha256:" + "b" * 64
EXEC = {"id": "mcp:exec", "name": "exec", "title": "exec", "kind": "oci", "image": "ghcr.io/c0decave/sandbox-ai-mcp-exec",
        "digest": DIGEST, "network": "none", "tool_timeout": 630}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    monkeypatch.setattr(s, "load_mcp_module", lambda: m)


def define(name, **extra):
    m.write_definition(name, {"image": f"ghcr.io/x/{name}@{DIGEST}", **extra})


# -- the definition's field --

@pytest.mark.parametrize("value", [10, 600, 3600])
def test_a_definition_takes_a_tool_timeout_of_10_to_3600_seconds(value):
    assert m.validated_image({"image": f"ghcr.io/x/y@{DIGEST}", "tool_timeout": value}, "t")["tool_timeout"] == value
    command = m.validated({"c": {"command": ["srv"], "tool_timeout": value}}, "file")["c"]
    assert command["tool_timeout"] == value, "a command server says it too"


@pytest.mark.parametrize("value", [9, 3601, 0, -5, "600", 60.5, True, None, [600]])
def test_a_tool_timeout_out_of_range_or_not_a_number_is_refused(value):
    with pytest.raises(ValueError, match="tool_timeout must be whole seconds, 10-3600"):
        m.validated_image({"image": f"ghcr.io/x/y@{DIGEST}", "tool_timeout": value}, "t")
    with pytest.raises(ValueError, match="tool_timeout must be whole seconds"):
        m.validated({"c": {"command": ["srv"], "tool_timeout": value}}, "file")


def test_the_catalogs_tool_timeout_reaches_the_definition_and_a_bad_one_is_left_out():
    assert m.definition_from_entry(EXEC)["tool_timeout"] == 630
    assert "tool_timeout" not in m.definition_from_entry({**EXEC, "tool_timeout": 99999}), \
        "a value out of range is left out (the default holds), as an entry's limits that are not valid"
    assert "tool_timeout" not in m.definition_from_entry({**EXEC, "tool_timeout": "630"})
    assert "tool_timeout" not in m.definition_from_entry({key: v for key, v in EXEC.items() if key != "tool_timeout"})
    assert m.well_formed({**EXEC, "tool_timeout": "x"}), "the listing still shows the entry"


def test_the_tool_timeout_is_no_part_of_a_servers_identity():
    """The agents' wait, not what the server can reach: a change of it makes no instance of its own."""
    base = {"image": f"ghcr.io/x/y@{DIGEST}", "network": "none"}
    assert m.definition_digest({**base, "tool_timeout": 900}, "podman") == m.definition_digest(base, "podman")
    assert m.definition_digest({**base, "network": "full"}, "podman") != m.definition_digest(base, "podman")


def test_mcp_set_tool_timeout_and_back_to_the_default():
    define("exec", network="none")
    m.write_definition("cmd", {"command": ["srv"]})
    result = m.set_server("exec", m.set_changes(m_args(tool_timeout="900")), out=lambda _: None)
    assert result["changed"] == ["tool_timeout"] and m.definitions()["exec"]["tool_timeout"] == 900
    m.set_server("cmd", m.set_changes(m_args(tool_timeout="45")), out=lambda _: None)
    assert m.definitions()["cmd"]["tool_timeout"] == 45, "a command server's too"
    m.set_server("exec", m.set_changes(m_args(tool_timeout="default", memory="1g")), out=lambda _: None)
    assert "tool_timeout" not in m.definitions()["exec"] and m.definitions()["exec"]["memory"] == "1g"
    for bad in ("5", "4000", "ten", "60s", "-1"):
        with pytest.raises(ValueError, match="--tool-timeout"):
            m.set_changes(m_args(tool_timeout=bad))
    with pytest.raises(ValueError, match="--tool-timeout"):
        m.set_changes(m_args())


def m_args(**given):
    keys = dict(memory=None, cpus=None, pids=None, tmp=None, network=None, mount=None, no_mounts=False, env=None,
                no_env=False, vpn=None, no_vpn=False, recommended=False, tool_timeout=None)
    return argparse.Namespace(**{**keys, **given})


# -- each agent's configuration --

def test_every_server_gets_its_wait(monkeypatch):
    define("exec", tool_timeout=630)
    define("plain")
    a = s.parse_args(["codex", "--mcp", "exec", "--mcp", "plain", "--mcp", "helper-claude-12345678",
                      "--mcp", "team-app", "--mcp", "exec"])
    assert s.mcp_tool_timeouts(a) == {"exec": 630, "plain": s.DEFAULT_MCP_TOOL_TIMEOUT,
                                      "helper-claude-12345678": s.LONG_TOOL_TIMEOUT, "team-app": s.LONG_TOOL_TIMEOUT}
    assert s.DEFAULT_MCP_TOOL_TIMEOUT > 600, "longer than exec's longest command: the server's own limit answers first"
    assert s.mcp_tool_timeouts(s.parse_args(["codex"])) == {}
    # Definitions that cannot be read: the default for each (the start says what is wrong elsewhere).
    (Path(m.config_file())).write_text("{not json")
    assert s.mcp_tool_timeouts(s.parse_args(["codex", "--mcp", "exec"])) == {"exec": s.DEFAULT_MCP_TOOL_TIMEOUT}


def test_codex_gets_a_tool_and_a_startup_timeout_for_every_server():
    define("exec", tool_timeout=630)
    args = s.mcp_agent_args(s.parse_args(["codex", "--mcp", "exec", "--mcp", "demo"]))
    assert "mcp_servers.exec.tool_timeout_sec=630" in args
    assert f"mcp_servers.demo.tool_timeout_sec={s.DEFAULT_MCP_TOOL_TIMEOUT}" in args, "Codex's 60 s no longer"
    assert f"mcp_servers.exec.startup_timeout_sec={s.MCP_STARTUP_TIMEOUT}" in args
    assert f"mcp_servers.demo.startup_timeout_sec={s.MCP_STARTUP_TIMEOUT}" in args
    assert all(args[index - 1] == "-c" for index, value in enumerate(args) if value.startswith("mcp_servers."))


def test_claude_code_gets_a_timeout_per_server_and_the_variables(monkeypatch):
    define("exec", tool_timeout=630)
    a = s.parse_args(["claude", "--mcp", "exec", "--mcp", "demo"])
    servers = json.loads(s.mcp_agent_args(a)[1])["mcpServers"]
    assert servers["exec"]["timeout"] == 630_000 and servers["demo"]["timeout"] == s.DEFAULT_MCP_TOOL_TIMEOUT * 1000
    env = s.agent_environment(a)
    assert env["MCP_TOOL_TIMEOUT"] == str(s.DEFAULT_MCP_TOOL_TIMEOUT * 1000), "the longest of the start's"
    assert env["MCP_TIMEOUT"] == str(s.MCP_STARTUP_TIMEOUT * 1000)
    assert "MCP_TOOL_TIMEOUT" not in s.agent_environment(s.parse_args(["claude"])), "no server: nothing set"
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "5000")
    mine = s.agent_environment(s.parse_args(["claude", "--mcp", "exec", "--env", "MCP_TOOL_TIMEOUT"]))
    assert mine["MCP_TOOL_TIMEOUT"] == "5000", "your own --env wins"


def test_opencode_gets_its_timeout_in_milliseconds():
    define("exec", tool_timeout=630)
    env = s.agent_environment(s.parse_args(["opencode", "--mcp", "exec", "--mcp", "helper-claude-x"]))
    mcp = json.loads(env["OPENCODE_CONFIG_CONTENT"])["mcp"]
    assert mcp["exec"]["timeout"] == 630_000 and mcp["helper-claude-x"]["timeout"] == s.LONG_TOOL_TIMEOUT * 1000


def test_hermes_dsh_qwen_and_goose_get_it_in_their_units():
    define("exec", tool_timeout=630)
    hermes = json.loads(s.hermes_files(s.parse_args(["hermes", "--mcp", "exec", "--mcp", "demo"]))
                        ["hermes/config.yaml"])["mcp_servers"]
    assert hermes["exec"]["timeout"] == 630 and hermes["demo"]["timeout"] == s.DEFAULT_MCP_TOOL_TIMEOUT
    rows = json.loads(s.agent_files(s.parse_args(["dsh", "--mcp", "exec"]))["dsh/sandbox-ai.patch.yml"])
    assert rows[0]["insert"][0]["config"]["toolCallTimeoutMs"] == 630_000
    goose = json.loads(s.agent_files(s.parse_args(["goose", "--mcp", "exec", "--mcp", "demo"]))["goose/config.yaml"])
    assert goose["extensions"]["exec"]["timeout"] == 630
    assert goose["extensions"]["demo"]["timeout"] == s.DEFAULT_MCP_TOOL_TIMEOUT
    qwen = json.loads(s.mcp_agent_args(s.parse_args(["qwen", "--mcp", "exec", "--mcp", "demo"]))[1])["mcpServers"]
    assert qwen["exec"]["timeout"] == 630_000 and qwen["demo"]["timeout"] == s.DEFAULT_MCP_TOOL_TIMEOUT * 1000
