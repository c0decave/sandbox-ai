"""Qwen Code as a sandbox-ai agent (sandbox/qwen.py, docs/QWEN_CODE_REVIEW.md): the features that turn on again what a
sandbox turns off, its system settings, telemetry that is never switchable, the image (the reviewed release, the
hash-locked npm install, no pipe into a shell), its build, login, sessions and helper mode. The start itself is in
tests/test_feature_matrix.py (QWEN) and the launcher's contracts (test_agent_sandbox_contracts.py)."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qwen = load("qwen_tests", "sandbox/qwen.py")
s = load("launcher_qwen_tests", "sandbox/agent_sandbox.py")
llm = load("llm_qwen_tests", "sandbox/llm.py")
sessions = load("sessions_qwen_tests", "sandbox/sessions.py")
presets = load("presets_qwen_tests", "sandbox/presets.py")
kits = load("kits_qwen_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_qwen_tests", "sandbox/agent_mcp.py")
lock_script = load("agent_npm_lock_qwen_tests", "scripts/agent_npm_lock.py")
LOCK = ROOT / "sandbox/Containerfile.qwen.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.qwen"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("QWEN_HOME", raising=False)
    (tmp_path / "home").mkdir()


def ns(agent="qwen", *enable, **more):
    return argparse.Namespace(**{"agent": agent, "qwen_enable": list(enable), "allow_host": [], "offline": False,
                                 "agent_args": [], **more})


# --- the features ---------------------------------------------------------------------------------------------------

def test_every_feature_names_its_hosts_and_says_what_it_does():
    for name, spec in qwen.FEATURES.items():
        assert qwen.FEATURE_SYNTAX.fullmatch(name) and spec["help"] and spec["hosts"]
        assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in spec["hosts"]), name
    # No feature for its own sandbox or its telemetry: neither is ever switchable.
    assert not {"sandbox", "telemetry", "usage-statistics"} & set(qwen.FEATURES)
    assert not qwen.telemetry_hosts_in(qwen.hosts(list(qwen.FEATURES)))


def test_parse_happy_sad_edge():
    assert qwen.parse(["updates", "extensions,model-catalog"]) == ["updates", "extensions", "model-catalog"]
    for wrong in (["docker"], ["telemetry"], ["updates;rm"], ["../x"], ["sandbox"]):
        with pytest.raises(ValueError, match="not a Qwen Code feature"):
            qwen.parse(wrong)
    assert qwen.parse([" UPDATES ", "updates", ",,"]) == ["updates"]
    assert qwen.parse(None) == [] and qwen.parse([]) == []


def test_network_options_add_only_the_features_hosts():
    a = ns("qwen", "updates")
    a.allow_host = ["registry.npmjs.org"]
    qwen.network_options(a)
    assert a.allow_host == ["registry.npmjs.org"]  # once
    other = ns("claude", "updates")
    qwen.network_options(other)  # not Qwen Code: nothing (check() refuses the option)
    assert other.allow_host == []
    offline = ns("qwen", "extensions", offline=True)
    qwen.network_options(offline)
    assert offline.allow_host == []
    broken = ns("qwen", "nope")
    qwen.network_options(broken)  # the error is check()'s
    assert broken.allow_host == []


def test_the_option_counts_as_weakening():
    assert "--qwen-enable" in s.MODES.WEAKENING_OPTIONS and s.SHORT_OPTIONS["--qwen-enable"] == "-qe"
    assert s.MODES.weakening_options(s, ["-qe", "updates", "--no-tmux"]) == [("--qwen-enable", "updates")]
    assert len(set(s.SHORT_OPTIONS.values())) == len(s.SHORT_OPTIONS), "every short option once"


def test_check_refuses_what_qwen_cannot_do_here():
    qwen.check(ns("qwen", "updates"))  # fine
    qwen.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="is for Qwen Code"):
        qwen.check(ns("claude", "updates"))
    with pytest.raises(ValueError, match="not in the team image"):
        qwen.check(ns("qwen"), companions=["claude"])
    with pytest.raises(ValueError, match="not enforced for Qwen Code"):
        qwen.check(ns("qwen"), hooks_active=True)
    with pytest.raises(ValueError, match="needs the network"):
        qwen.check(ns("qwen", "updates", offline=True))


def test_its_own_sandbox_and_telemetry_options_are_refused_in_its_arguments():
    for args in (["--sandbox"], ["--sandbox=docker"], ["-s"], ["-ys"], ["--sandbox-image", "img"],
                 ["--telemetry"], ["--telemetry=true"], ["--telemetry-target", "gcp"], ["--telemetry-outfile", "f"]):
        assert qwen.refused_arg(args), args
        with pytest.raises(ValueError, match="drop it"):
            qwen.check(ns("qwen", agent_args=args))
    for args in (["--model", "qwen3-coder-plus"], ["-y"], ["--yolo", "fix it"], ["--", "--sandbox"], []):
        assert qwen.refused_arg(args) is None, args  # after "--" it is the prompt's text


# --- the system settings ---------------------------------------------------------------------------------------------

def test_settings_pin_the_reviewed_defaults():
    settings = qwen.managed_settings(ns())
    assert settings["$version"] == qwen.SETTINGS_VERSION == 4, "never migrated (rewritten) by Qwen Code"
    assert settings["tools"] == {"sandbox": False} and settings["ide"] == {"enabled": False}
    assert settings["general"] == {"enableAutoUpdate": False}
    assert settings["privacy"] == {"usageStatisticsEnabled": False}
    assert settings["telemetry"] == {"enabled": False, "logPrompts": False, "includeSensitiveSpanAttributes": False}


def test_features_turn_their_switches_on_and_telemetry_on_leaves_qwen_its_own():
    assert qwen.managed_settings(ns("qwen", "updates"))["general"] == {"enableAutoUpdate": True}
    assert qwen.managed_settings(ns(), telemetry="on")["general"] == {"enableAutoUpdate": False}, "on is gone"
    env = qwen.environment(ns("qwen", "model-catalog"))
    assert "QWEN_CODE_MODELS_DEV_REFRESH" not in env and qwen.environment(ns())["QWEN_CODE_MODELS_DEV_REFRESH"] == "off"
    assert qwen.managed_settings(ns(effort="max"))["model"] == {"reasoningEffort": "max"}


def test_telemetry_is_pinned_off_whatever_is_enabled():
    """The user's rule: never switchable. Every feature, --telemetry on, and an --llm section that tries: the pins
    hold, last."""
    sneaky = {"privacy": {"usageStatisticsEnabled": True}, "telemetry": {"enabled": True, "target": "gcp"}}
    settings = qwen.managed_settings(ns("qwen", *qwen.FEATURES), telemetry="on", llm=sneaky)
    assert settings["privacy"] == {"usageStatisticsEnabled": False}
    assert settings["telemetry"]["enabled"] is False and settings["telemetry"]["logPrompts"] is False
    env = qwen.environment(ns("qwen", *qwen.FEATURES))
    for key, value in qwen.TELEMETRY_ENV.items():
        assert env[key] == value
    assert env["QWEN_USAGE_STATISTICS_ENABLED"] == "false", "the variable wins over the setting upstream"


def test_telemetry_hosts_are_never_opened():
    assert qwen.telemetry_hosts_in(["gb4w8c3ygj-default-sea.rum.aliyuncs.com"]) == ["*.rum.aliyuncs.com"]
    assert qwen.telemetry_hosts_in(["*.aliyuncs.com", "play.googleapis.com"]) == ["*.rum.aliyuncs.com",
                                                                               "play.googleapis.com"]
    assert qwen.telemetry_hosts_in(["dashscope.aliyuncs.com", "registry.npmjs.org", "googleapis.com.evil"]) == []
    for opened in ({"allow_host": ["x.rum.aliyuncs.com"]}, {"no_inspect": ["play.googleapis.com"]},
                   {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            qwen.check_telemetry(argparse.Namespace(**opened))
    rules = qwen.telemetry_rules()
    assert [rule["host"] for rule in rules] == qwen.rule_hosts()
    assert all(rule["decision"] == "deny" and rule["id"].startswith("telemetry-qwen-") for rule in rules)


def test_the_provider_hosts_hold_no_telemetry_host():
    assert not qwen.telemetry_hosts_in(s.PROVIDERS["qwen"])


def test_mcp_servers_go_through_the_bridge():
    args = qwen.mcp_args(["demo", "helper-claude-x", "demo"], lambda name: ["/bridge", name], {"helper-claude-x": 2000})
    config = json.loads(args[1])
    assert args[0] == "--mcp-config" and list(config["mcpServers"]) == ["demo", "helper-claude-x"]
    assert config["mcpServers"]["demo"] == {"command": "python3", "args": ["/bridge", "demo"], "alwaysLoadTools": True}
    assert config["mcpServers"]["helper-claude-x"]["timeout"] == 2000 * 1000
    assert qwen.mcp_args([], lambda name: []) == []


def test_the_mcp_servers_tools_are_declared_to_the_model_not_deferred():
    """CI's first live run (43633858837): the scripted model was offered Qwen Code's own tools and its tool_search /
    tool_call bridge, never mcp__demo__version - Qwen Code 0.26.4 defers every MCP tool (DiscoveredMCPTool passes
    shouldDefer=true to BaseDeclarativeTool) and getFunctionDeclarations drops a deferred tool unless its alwaysLoad,
    which discovery sets from the server config's alwaysLoadTools === true; servers from --mcp-config reach discovery
    as given (parseMcpConfig, assembleMcpServers). Every --mcp server of the start says so, the helpers' too."""
    for names in (["demo"], ["demo", "helper-claude-x"]):
        servers = json.loads(qwen.mcp_args(names, lambda name: ["/bridge", name])[1])["mcpServers"]
        assert all(server["alwaysLoadTools"] is True for server in servers.values()) and list(servers) == names
    launched = json.loads(s.mcp_agent_args(s.parse_args(["qwen", "--mcp", "demo"]))[1])["mcpServers"]
    assert launched["demo"]["alwaysLoadTools"] is True and launched["demo"]["args"] == s.mcp_bridge_args("demo")


def test_files_environment_and_plan():
    files = qwen.files(ns(), qwen.managed_settings(ns()))
    assert list(files) == ["qwen/settings.json"] and json.loads(files["qwen/settings.json"])["$version"] == 4
    env = qwen.environment(ns())
    assert env["QWEN_CODE_SYSTEM_SETTINGS_PATH"] == qwen.SETTINGS == "/etc/qwen-code/settings.json"
    assert env["SANDBOX"] == "sandbox-ai" and env["QWEN_SANDBOX"] == "false"
    # Qwen Code leaves its synchronized output out whenever TMUX is set (always, in a session): on, with every feature
    assert env["QWEN_CODE_SYNCHRONIZED_OUTPUT"] == "1"
    assert qwen.environment(ns("qwen", *qwen.FEATURES))["QWEN_CODE_SYNCHRONIZED_OUTPUT"] == "1"
    plan = qwen.plan(ns("qwen", "updates"))
    assert plan["version"] == qwen.VERSION and plan["experimental"] and plan["telemetry"]["switchable"] is False
    assert plan["enabled"] == [{"feature": "updates", "hosts": ["registry.npmjs.org"]}]
    assert plan["off"] == ["model-catalog", "extensions"]


def test_login_document():
    assert qwen.login_document_ok({"access_token": "a", "refresh_token": "r", "resource_url": "portal.qwen.ai"})
    assert not qwen.login_document_ok({}) and not qwen.login_document_ok([]) and not qwen.login_document_ok(
        {"access_token": ""})
    assert s.AUTH_TARGETS["qwen"] == ".qwen/oauth_creds.json"


def test_a_refreshed_qwen_login_goes_back_but_never_another_one():
    stored = {"access_token": "a1", "refresh_token": "r1", "resource_url": "portal.qwen.ai", "expiry_date": 1}
    fresh = {**stored, "access_token": "a2", "refresh_token": "r2", "expiry_date": 2}
    assert s.merge_refreshed("qwen", stored, fresh) == fresh
    assert s.merge_refreshed("qwen", stored, {**fresh, "resource_url": "evil.example"}) is None
    assert s.merge_refreshed("qwen", stored, {"access_token": "x"}) is None
    assert s.merge_refreshed("qwen", stored, {**fresh, "extra": "key"}) == fresh, "nothing but the tokens"


def test_the_hosts_login_is_qwen_home(tmp_path, monkeypatch):
    assert s.auth_sources("qwen") == [(tmp_path / "home/.qwen/oauth_creds.json", ".qwen/oauth_creds.json")]
    monkeypatch.setenv("QWEN_HOME", str(tmp_path / "q"))
    assert s.auth_sources("qwen")[0][0] == tmp_path / "q/oauth_creds.json"


# --- --llm -----------------------------------------------------------------------------------------------------------

def test_llm_keeps_qwens_values_in_step():
    assert llm.QWEN_SETTINGS == qwen.SETTINGS and "qwen" in llm.AGENTS and "dsh" in llm.AGENTS
    assert set(llm.AGENTS) == set(s.AUTH_TARGETS)


# --- the image: the reviewed release, the lock, its build -------------------------------------------------------------

def test_the_containerfile_builds_exactly_the_reviewed_release():
    text = CONTAINERFILE.read_text()
    assert f"ARG QWEN_VERSION={qwen.VERSION}" in text and "COPY Containerfile.qwen.lock" in text
    assert "npm ci --ignore-scripts" in text and "npm install" not in text
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "mkdir -p -m 755 /etc/qwen-code" in text and "rm -rf /opt/qwen/node_modules/@oven" in text
    assert "/usr/local/bin/qwen" in text


def test_the_lock_holds_the_reviewed_package_with_every_integrity():
    lock = json.loads(LOCK.read_text())
    assert lock_script.check(lock, "qwen", qwen.PACKAGE, qwen.VERSION) > 10
    entry = lock["packages"][f"node_modules/{qwen.PACKAGE}"]
    assert entry["integrity"] == qwen.INTEGRITY and entry["resolved"].startswith("https://registry.npmjs.org/")
    assert all(entry.get("resolved", "").startswith("https://registry.npmjs.org/")
               for path, entry in lock["packages"].items() if path), "only npm's registry"
    assert lock_script.package_json("qwen", qwen.PACKAGE, qwen.VERSION) == lock["packages"][""] | {"private": True}


def test_the_lock_check_refuses_what_is_not_the_reviewed_lock():
    lock = json.loads(LOCK.read_text())
    with pytest.raises(ValueError, match="not of"):
        lock_script.check(lock, "qwen", qwen.PACKAGE, "9.9.9")
    broken = json.loads(LOCK.read_text())
    del broken["packages"]["node_modules/sharp"]["integrity"]
    with pytest.raises(ValueError, match="no sha512 integrity for node_modules/sharp"):
        lock_script.check(broken, "qwen", qwen.PACKAGE, qwen.VERSION)
    with pytest.raises(ValueError, match="lockfileVersion 3"):
        lock_script.check({"lockfileVersion": 2}, "qwen", qwen.PACKAGE, qwen.VERSION)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    log = tmp_path / "podman.log"
    script = tmp_path / "bin" / "podman"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) "
                      "+ '\\n')\nif sys.argv[1:3] == ['image', 'inspect']:\n    print('[]')\n")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_PYTHON", sys.executable)
    monkeypatch.setenv("SANDBOX_AI_BUILD_NO_DNS_CHECK", "1")
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def build(*args):
    return subprocess.run(["/bin/bash", str(ROOT / "sandbox/build_image.sh"), *args], capture_output=True, text=True)


def test_build_qwen_takes_the_reviewed_release_only(engine):
    result = build("qwen")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"QWEN_VERSION={qwen.VERSION}" in calls[0]
    assert f"sandbox-ai.agent-version={qwen.VERSION}" in calls[0]
    assert any(arg.endswith("Containerfile.qwen") for arg in calls[0])
    assert build("--pin", qwen.VERSION, "qwen").returncode == 0
    refused = build("--pin", "0.27.3", "qwen")
    assert refused.returncode == 2 and "Qwen Code is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("qwen", "--build-arg", "QWEN_VERSION=0.27.3").returncode == 2


# --- the agent's other places: sessions, presets, kits, login, its helper mode --------------------------------------

def test_sessions_continue_and_options():
    assert sessions.CONTINUE_ARGS["qwen"] == ["--continue"]
    assert {"--continue", "-c", "--resume", "-r", "--session-id"} == sessions.SESSION_OPTIONS["qwen"]
    assert {"--model", "--prompt-interactive", "--mcp-config", "--append-system-prompt"} <= sessions.VALUE_OPTIONS["qwen"]


def test_a_presets_first_task_starts_qwen_interactively():
    assert presets.prompt_args({"prompt": "-fix {what}"}, "qwen", {"what": "it"}) == ["--prompt-interactive=-fix it"]
    assert presets.prompt_args({}, "qwen") == [] and "qwen" in presets.AGENTS


def test_kits_and_memory_for_qwen():
    assert kits.LAYOUT["qwen"] == {"skills": ".qwen/skills"}
    assert kits.MEMORY_FILE["qwen"] == ("QWEN.md", ".qwen/QWEN.md")
    skill = (kits.BUILTIN / "qwen/skills/sandbox-ai/SKILL.md").read_text()
    assert "## Qwen Code in this sandbox" in skill and all(f"`{name}`" in skill for name in qwen.FEATURES)
    assert all(host in skill for host in qwen.TELEMETRY_HOSTS)
    assert (kits.TEMPLATES / "qwen/QWEN.md").is_file()
    assert "~/.qwen/QWEN.md" in (kits.TEMPLATES / "qwen/skills/sandbox-example/SKILL.md").read_text()


def test_login_runs_qwen_for_its_auth_dialog():
    login = load("login_qwen_tests", "sandbox/login.py")
    assert "qwen" in login.AGENTS and login.LOGIN_ARGS["qwen"] == []
    assert "dsh" not in login.AGENTS  # its sign-in is its web UI's


def test_qwen_as_a_helper_runs_one_shot():
    assert agent_mcp.command("qwen", "-x", resume=True) == ["qwen", "--continue", "--prompt=-x"]
    assert agent_mcp.command("qwen", "task", skip=True) == ["qwen", "--yolo", "--prompt=task"]


def test_the_review_is_shipped_with_the_release_it_names():
    review = (ROOT / "docs/QWEN_CODE_REVIEW.md").read_text()
    assert qwen.VERSION in review and qwen.COMMIT in review and qwen.INTEGRITY in review
    for name in qwen.FEATURES:
        assert f"`{name}`" in review, name
    assert "## Go / No-Go" in review and "--qwen-enable" in review


def test_the_docs_say_telemetry_is_never_switchable():
    review = (ROOT / "docs/QWEN_CODE_REVIEW.md").read_text()
    assert "### Telemetry: always off, never switchable" in review
    assert all(host in review for host in qwen.TELEMETRY_HOSTS)
    assert all(name in review for name in qwen.TELEMETRY_ENV)
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "Qwen Code" in text and all(host in text for host in qwen.TELEMETRY_HOSTS), readme
