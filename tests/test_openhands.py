"""OpenHands as a sandbox-ai agent (sandbox/openhands.py, sandbox/openhands_cli.py, docs/OPENHANDS_REVIEW.md): the
features that turn on again what a sandbox turns off, the settings a start lays over OpenHands' own and the image's
command that applies them, its uvx in front of the real one, telemetry that is never switchable, the image (both
sides hash-locked, nothing piped into a shell, nothing fetched at start), its build, sessions, kits and helper mode.
The start itself is in tests/test_feature_matrix.py (OPENHANDS) and the launcher's contracts
(test_agent_sandbox_contracts.py)."""
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


openhands = load("openhands_tests", "sandbox/openhands.py")
cli = load("openhands_cli_tests", "sandbox/openhands_cli.py")
s = load("launcher_openhands_tests", "sandbox/agent_sandbox.py")
llm = load("llm_openhands_tests", "sandbox/llm.py")
sessions = load("sessions_openhands_tests", "sandbox/sessions.py")
presets = load("presets_openhands_tests", "sandbox/presets.py")
kits = load("kits_openhands_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_openhands_tests", "sandbox/agent_mcp.py")
NPM_LOCK = ROOT / "sandbox/Containerfile.openhands.lock"
PYTHON_LOCK = ROOT / "sandbox/Containerfile.openhands-python.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.openhands"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def ns(agent="openhands", *enable, **more):
    return argparse.Namespace(**{"agent": agent, "openhands_enable": list(enable), "allow_host": [],
                                 "secret_env": [], "env": [], "offline": False, "agent_args": [], **more})


# --- the features ---------------------------------------------------------------------------------------------------


def without_companion_commands(skill):
    """SKILL in lower case without the one-shot commands of the agents --with (its part on other agents names
    every agent's: the rest is this agent's own)."""
    text = " ".join(skill.lower().split())
    for command in s.COMPANION_COMMANDS.values():
        text = text.replace(f"`{command.lower()}`", "")
    return text

def test_every_feature_names_its_hosts_and_says_what_it_does():
    for name, spec in openhands.FEATURES.items():
        assert openhands.FEATURE_SYNTAX.fullmatch(name) and spec["help"] and spec["env"], name
        assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in spec["hosts"]), name
    # No feature for its telemetry: never switchable.
    assert not {"telemetry", "analytics", "posthog", "laminar"} & set(openhands.FEATURES)
    assert not openhands.telemetry_hosts_in(openhands.hosts(list(openhands.FEATURES)))
    assert set(openhands.FEATURES) == {"automations", "model-catalog"}


def test_parse_happy_sad_edge():
    assert openhands.parse(["model-catalog", "automations,model-catalog"]) == ["model-catalog", "automations"]
    for wrong in (["docker"], ["telemetry"], ["acp"], ["automations;rm"], ["../x"], ["remote-backends"]):
        with pytest.raises(ValueError, match="not an OpenHands feature"):
            openhands.parse(wrong)
    assert openhands.parse([" AUTOMATIONS ", ",,"]) == ["automations"]
    assert openhands.parse(None) == [] and openhands.parse([]) == []


def test_network_options_add_only_the_features_hosts():
    a = ns("openhands", "automations", "model-catalog")
    a.allow_host = ["github.com"]
    openhands.network_options(a)
    assert a.allow_host == ["github.com", "raw.githubusercontent.com"]
    assert a.secret_env == []
    other = ns("claude", "model-catalog")
    openhands.network_options(other)  # not OpenHands: nothing (check() refuses the option)
    assert other.allow_host == []
    offline = ns("openhands", "model-catalog", offline=True)
    openhands.network_options(offline)
    assert offline.allow_host == []
    broken = ns("openhands", "nope")
    openhands.network_options(broken)  # the error is check()'s
    assert broken.allow_host == []


def test_the_option_counts_as_weakening():
    assert "--openhands-enable" in s.MODES.WEAKENING_OPTIONS and s.SHORT_OPTIONS["--openhands-enable"] == "-ox"
    assert s.MODES.weakening_options(s, ["-ox", "automations", "--no-tmux"]) == [("--openhands-enable", "automations")]
    assert len(set(s.SHORT_OPTIONS.values())) == len(s.SHORT_OPTIONS), "every short option once"


def test_check_refuses_what_openhands_cannot_do_here():
    openhands.check(ns("openhands", "automations"))  # fine
    openhands.check(ns("openhands", "automations", offline=True))  # it opens no host: fine offline
    openhands.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="is for OpenHands"):
        openhands.check(ns("claude", "automations"))
    openhands.check(ns("openhands"), companions=["claude"])  # --with: a layer of the team image since 0.1.98
    with pytest.raises(ValueError, match="cannot be enforced for OpenHands: its agent server 1.58.1 takes hooks only with each conversation"):
        openhands.check(ns("openhands"), hooks_active=True)
    with pytest.raises(ValueError, match="needs the network"):
        openhands.check(ns("openhands", "model-catalog", offline=True))
    for env, secrets in ((["OH_CONVERSATION_RUNTIME"], []), ([], ["OH_AGENT_SERVER_GIT_REF=x"]),
                         (["LOCAL_BACKEND_API_KEY"], [])):
        with pytest.raises(ValueError, match="sandbox-ai sets it for OpenHands"):
            openhands.check(ns(env=env, secret_env=secrets))


def test_the_canvas_options_that_would_undo_a_default_are_refused():
    for args in (["--public"], ["--host", "0.0.0.0"], ["-H", "::"], ["--host=10.0.0.2"], ["--automation-ref", "main"],
                 ["--automation-repo=https://x"], ["--dynamic"], ["--port", "9000", "--public"]):
        refused = openhands.refused_arg(args)
        assert refused, args
        assert cli.refused(args), args  # the image's command refuses them as well
    for args in ([], ["--host", "127.0.0.1"], ["-p", "9000"], ["task", "--text=--public please"], ["--", "--public"],
                 ["--help"], ["--frontend-only"]):
        assert openhands.refused_arg(args) is None, args
    with pytest.raises(ValueError, match="--public in OpenHands' arguments"):
        openhands.check(ns(agent_args=["--public"]))


def test_command_args_default_to_the_canvas_on_the_loopback():
    assert openhands.command_args([]) == ["--host", "127.0.0.1", "--port", "8000"]
    assert openhands.command_args(["-p", "9000"]) == ["--host", "127.0.0.1", "-p", "9000"]
    assert openhands.command_args(["--host", "localhost"]) == ["--host", "localhost"]
    assert openhands.command_args(["task", "--text=hi"]) == ["task", "--text=hi"]
    assert openhands.command_args(["--version"]) == ["--version"]
    assert openhands.WEB_URL == "http://127.0.0.1:8000/" and openhands.PROGRAM == "openhands"


# --- the settings a start lays over OpenHands' ------------------------------------------------------------------------

def test_the_settings_hold_the_sandboxs_defaults():
    settings = openhands.managed_settings(ns(dangerously_skip_permissions=False))
    assert settings["conversation_settings_diff"] == {"confirmation_mode": True, "security_analyzer": "llm"}
    assert settings["agent_settings_diff"] == {} and settings["sandbox_ai"] == {"mcp": []}
    assert settings["misc_settings_diff"] == {"telemetry": {"consent": "denied", "managed": True}}
    danger = openhands.managed_settings(ns(dangerously_skip_permissions=True))
    assert danger["conversation_settings_diff"]["confirmation_mode"] is False
    files = openhands.files(ns(), settings)
    assert list(files) == ["openhands/settings.json"] and json.loads(files["openhands/settings.json"]) == settings
    assert openhands.SETTINGS == "/etc/openhands/settings.json" == openhands.MANAGED_DIR + "/settings.json"


def test_mcp_servers_become_stdio_servers_through_the_bridge():
    servers = openhands.mcp_servers(["demo", "helper-claude-x", "demo"], lambda name: ["/bridge", name],
                                    {"helper-claude-x": 2000})
    assert list(servers) == ["demo", "helper-claude-x"]
    assert servers["demo"] == {"transport": "stdio", "command": "python3", "args": ["/bridge", "demo"],
                               "description": "sandbox-ai's MCP server demo"}
    assert servers["helper-claude-x"]["timeout"] == 2000.0
    settings = openhands.managed_settings(ns(), servers)
    assert settings["agent_settings_diff"]["mcp_config"] == servers
    assert settings["sandbox_ai"] == {"mcp": ["demo", "helper-claude-x"]}


def test_an_api_key_of_a_known_provider_names_the_model_and_its_placeholder():
    a = ns(api_key=["api.anthropic.com=ANTHROPIC_API_KEY"], model="claude-opus-5-5", effort="high")
    assert openhands.api_provider(a) == ("anthropic", "ANTHROPIC_API_KEY")
    assert openhands.llm_settings(a, "PLACEHOLDER") == {"api_key": "PLACEHOLDER", "model": "anthropic/claude-opus-5-5",
                                                       "reasoning_effort": "high"}
    router = ns(api_key=["openrouter.ai=OPENROUTER_API_KEY=mine"], model="openrouter/qwen/qwen3")
    assert openhands.llm_settings(router, "P")["model"] == "openrouter/qwen/qwen3", "a prefixed name is kept"
    assert openhands.llm_settings(ns(model="gpt-5.6")) == {"model": "gpt-5.6"}
    assert openhands.llm_settings(ns()) == {}
    # Sad: a key under another variable than LiteLLM reads, two providers, an --llm, an unknown host.
    for keys, more in ((["api.anthropic.com=CLAUDE_KEY"], {}),
                       (["api.anthropic.com=ANTHROPIC_API_KEY", "api.openai.com=OPENAI_API_KEY"], {}),
                       (["api.anthropic.com=ANTHROPIC_API_KEY"], {"llm": ["ollama"]}),
                       (["example.org=KEY"], {}), ([], {})):
        assert openhands.api_provider(ns(api_key=keys, **more)) is None, keys
    # Google's key: LiteLLM reads GEMINI_API_KEY.
    assert openhands.API_PROVIDERS["generativelanguage.googleapis.com"] == ("gemini", "GEMINI_API_KEY")


def test_llm_and_the_loop_guard_go_into_the_settings():
    server = {"model": "openai/qwen3", "base_url": "http://127.0.0.1:1234/v1", "api_key": "placeholder",
              "max_input_tokens": 32768}
    settings = openhands.managed_settings(ns(llm=["lm"], effort="low"), llm=server,
                                          loop={"steps": 40, "instructions": "LOOP"})
    assert settings["agent_settings_diff"]["llm"] == {**server, "reasoning_effort": "low"}
    assert settings["agent_settings_diff"]["agent_context"] == {"system_message_suffix": "LOOP"}
    assert settings["conversation_settings_diff"]["max_iterations"] == 40
    plain = openhands.managed_settings(ns(), loop={"steps": None, "instructions": None})
    assert "max_iterations" not in plain["conversation_settings_diff"] and "agent_context" not in \
        plain["agent_settings_diff"]


def test_the_variables_pin_the_release_the_runtime_and_telemetry():
    env = openhands.environment(ns())
    assert env["OH_CONVERSATION_RUNTIME"] == "local" and env["OH_CONVERSATION_RUNTIME_SELECTABLE"] == "false"
    assert env["OH_AGENT_SERVER_VERSION"] == openhands.SERVER_VERSION
    assert env["OH_AUTOMATION_VERSION"] == openhands.AUTOMATION_VERSION
    assert env["OH_AGENT_SERVER_GIT_REF"] == env["OH_AGENT_SERVER_LOCAL_PATH"] == ""
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True" and "SANDBOX_AI_OPENHANDS_AUTOMATIONS" not in env
    enabled = openhands.environment(ns("openhands", "automations", "model-catalog"))
    assert enabled["SANDBOX_AI_OPENHANDS_AUTOMATIONS"] == "1" and enabled["LITELLM_LOCAL_MODEL_COST_MAP"] == "False"
    assert openhands.EFFORTS == s.AGENT_EFFORTS["openhands"] and s.DANGER_ENV["openhands"] == {}


def test_telemetry_is_pinned_off_whatever_is_enabled():
    """The user's rule: never switchable. Every feature: the variables and the consent hold."""
    env = openhands.environment(ns("openhands", *openhands.FEATURES))
    for key, value in openhands.TELEMETRY_ENV.items():
        assert env[key] == value
    assert env["DO_NOT_TRACK"] == "1" and env["AGENT_CANVAS_DISABLE_TELEMETRY"] == "1"
    assert env["VITE_DO_NOT_TRACK"] == "1", "the Canvas launcher gives no PostHog key then (measured)"
    assert env["OH_TELEMETRY_EXPORTER"] == "none" and env["ANONYMIZED_TELEMETRY"] == "false"
    assert env["LMNR_PROJECT_API_KEY"] == "" and env["OTEL_SDK_DISABLED"] == "true"
    settings = openhands.managed_settings(ns("openhands", *openhands.FEATURES))
    assert settings["misc_settings_diff"]["telemetry"] == {"consent": "denied", "managed": True}


def test_telemetry_hosts_are_never_opened():
    assert openhands.telemetry_hosts_in(["us.i.posthog.com"]) == ["*.posthog.com"]
    assert openhands.telemetry_hosts_in(["z.openhands.dev", "api.lmnr.ai"]) == ["api.lmnr.ai", "z.openhands.dev"]
    assert openhands.telemetry_hosts_in(["*.openhands.dev"]) == ["z.openhands.dev"]
    assert openhands.telemetry_hosts_in(["api.anthropic.com", "raw.githubusercontent.com", "docs.openhands.dev"]) == []
    for opened in ({"allow_host": ["eu.i.posthog.com"]}, {"allow_host": ["z.openhands.dev"]},
                   {"no_inspect": ["api.lmnr.ai"]}, {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            openhands.check_telemetry(argparse.Namespace(**opened))
    with pytest.raises(ValueError, match="drop --allow-domain openhands.dev"):
        openhands.check_telemetry(argparse.Namespace(allow_host=["openhands.dev", "*.openhands.dev"],
                                                     allow_domain=["openhands.dev"]))
    rules = openhands.telemetry_rules()
    assert [rule["host"] for rule in rules] == ["z.openhands.dev", "*.posthog.com", "posthog.com", "api.lmnr.ai"]
    assert all(rule["decision"] == "deny" and rule["id"].startswith("telemetry-openhands-") for rule in rules)


def test_the_provider_hosts_hold_no_telemetry_host():
    assert s.PROVIDERS["openhands"] == [] and not openhands.telemetry_hosts_in(openhands.API_PROVIDERS)


def test_plan_says_what_is_on_and_off():
    plan = openhands.plan(ns("openhands", "automations"))
    assert plan["version"] == openhands.VERSION and plan["commit"] == openhands.COMMIT and plan["experimental"]
    assert plan["agent_server"]["version"] == openhands.SERVER_VERSION and plan["runtime"] == "local"
    assert plan["telemetry"]["switchable"] is False and plan["web_ui"] == openhands.WEB_URL
    assert plan["enabled"] == [{"feature": "automations", "hosts": []}] and plan["off"] == ["model-catalog"]
    assert openhands.plan(ns(agent_args=["task", "--text=x"]))["web_ui"] is None


def test_openhands_has_no_login_to_keep(tmp_path):
    assert not openhands.login_document_ok({"agent_settings": {}}) and not openhands.login_document_ok({})
    assert not s.login_document_ok("openhands", {"x": "y"})
    assert s.auth_sources("openhands") == [(tmp_path / "home/.openhands/settings.json", ".openhands/settings.json")]
    assert s.merge_refreshed("openhands", {"a": 1}, {"a": 2}) is None
    assert "openhands" in s.NO_LOGIN_AGENTS and "openhands" in s.LOGIN_COMMANDLESS
    login = load("login_openhands_tests", "sandbox/login.py")
    assert "openhands" not in login.AGENTS


# --- the image's command: its uvx, the settings, the Canvas, one task -------------------------------------------------

VERSIONS = {"openhands-agent-server": "1.58.1", "openhands-sdk": "1.58.1", "openhands-tools": "1.58.1",
            "openhands-workspace": "1.58.1", "openhands-automation": "1.26.0"}
CANVAS_SERVER = ["--python", ">=3.12,<3.15", "--from", "openhands-agent-server==1.58.1", "--with",
                 "openhands-sdk==1.58.1", "--with", "openhands-tools==1.58.1", "--with", "openhands-workspace==1.58.1",
                 "--with", "posthog>=6,<7", "agent-server", "--import-modules", "x", "--host", "127.0.0.1"]
CANVAS_AUTOMATION = ["--python", ">=3.12,<3.15", "--from", "openhands-automation==1.26.0", "uvicorn",
                     "openhands.automation.app:app", "--host", "127.0.0.1", "--port", "18001"]


def test_the_image_uvx_runs_the_installed_releases_only():
    assert cli.uvx_target(CANVAS_SERVER, VERSIONS) == ("agent-server", ["--import-modules", "x", "--host",
                                                                        "127.0.0.1"])
    assert cli.uvx_target(CANVAS_AUTOMATION, VERSIONS) == ("automation", CANVAS_AUTOMATION[5:])
    # Every other uvx call is the real uvx's (MCP servers, kits).
    for args in (["ruff", "--version"], ["--from", "mcp-server-fetch==1.0", "mcp-server-fetch"], ["playwright"],
                 ["--with", "requests", "python", "-c", "print(1)"]):
        assert cli.uvx_target(args, VERSIONS) is None, args
    # OpenHands of another version or source: refused.
    for args in (["--from", "openhands-agent-server==1.59.0", "agent-server"],
                 ["--reinstall", "--from", "git+https://github.com/OpenHands/software-agent-sdk@main"
                  "#subdirectory=openhands-agent-server", "agent-server"],
                 ["--from", "/src/openhands-agent-server", "--with-editable", "/src/openhands-sdk", "agent-server"],
                 ["--from", "openhands-agent-server==1.58.1", "--with", "openhands-sdk==1.59.0", "agent-server"],
                 ["--from", "openhands-agent-server>=1.58", "agent-server"],
                 ["--from", "openhands-automation==1.26.0", "--index-url", "https://evil", "uvicorn",
                  "openhands.automation.app:app"],
                 ["--from", "openhands-agent-server==1.58.1", "python", "-c", "x"],
                 ["--unknown-flag", "--from", "openhands-agent-server==1.58.1", "agent-server"]):
        with pytest.raises(ValueError):
            cli.uvx_target(args, VERSIONS)


def test_the_versions_come_from_the_venv(tmp_path):
    site = tmp_path / "venv/lib/python3.12/site-packages"
    for name in ("openhands_agent_server-1.58.1.dist-info", "openhands_sdk-1.58.1.dist-info", "requests-2.dist-info"):
        (site / name).mkdir(parents=True)
    assert cli.installed_versions(tmp_path / "venv") == {"openhands-agent-server": "1.58.1",
                                                         "openhands-sdk": "1.58.1"}


def test_the_uvx_script_passes_other_calls_to_the_real_uvx(tmp_path):
    real = tmp_path / "real-uvx"
    real.write_text("#!/bin/sh\necho \"real $*\"\n")
    real.chmod(0o755)
    shim = tmp_path / "bin" / "uvx"
    shim.parent.mkdir()
    shim.symlink_to(ROOT / "sandbox/openhands_cli.py")
    env = {"PATH": "/usr/bin:/bin", "SANDBOX_AI_REAL_UVX": str(real), "SANDBOX_AI_OPENHANDS_OPT": str(tmp_path),
           "HOME": str(tmp_path)}
    passed = subprocess.run([str(shim), "ruff", "--version"], env=env, capture_output=True, text=True, timeout=30)
    assert passed.returncode == 0 and passed.stdout == "real ruff --version\n", passed.stderr
    refused = subprocess.run([str(shim), "--from", "openhands-agent-server==9.9.9", "agent-server"], env=env,
                             capture_output=True, text=True, timeout=30)
    assert refused.returncode == 2 and "not run here" in refused.stderr
    assert (ROOT / "sandbox/openhands_cli.py").stat().st_mode & 0o111, "the image runs it directly"


def test_the_patch_body_sets_consent_and_removes_old_mcp_servers():
    document = openhands.managed_settings(ns(), {"new": {"command": "python3"}})
    current = {"misc_settings": {"sandbox_ai": {"mcp": ["old", "new"]}, "telemetry": {"consent": "granted"}}}
    body = cli.patch_body(document, current)
    assert body["agent_settings_diff"]["mcp_config"] == {"old": None, "new": {"command": "python3"}}
    assert body["misc_settings_diff"] == {"telemetry": {"consent": "denied", "managed": True},
                                          "sandbox_ai": {"mcp": ["new"]}}
    assert body["conversation_settings_diff"]["confirmation_mode"] is True
    bare = cli.patch_body({}, None)
    assert bare == {"misc_settings_diff": {"telemetry": {"consent": "denied", "managed": True},
                                           "sandbox_ai": {"mcp": []}}}, "the consent even without the launcher"
    # A settings answer of an unexpected shape removes nothing.
    assert "agent_settings_diff" not in cli.patch_body({}, {"misc_settings": "x"})


def test_the_settings_wait_for_the_server_and_fall_back_without_the_model():
    calls = []

    def send(url, key, method="GET", body=None, timeout=10):
        calls.append((method, url.rsplit("/", 1)[-1], body))
        if url.endswith("/server_info"):
            return (200, {}) if len([c for c in calls if c[1] == "server_info"]) >= 3 else (502, None)
        if method == "GET":
            return 200, {"misc_settings": {}}
        return (400, None) if "agent_settings_diff" in body else (200, {})

    said = []
    document = openhands.managed_settings(ns(model="m"))
    assert cli.apply_settings(8000, "k", document, wait=60, log=said.append, send=send, sleep=lambda _: None)
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 2 and "agent_settings_diff" not in patches[1][2]
    assert patches[1][2]["misc_settings_diff"]["telemetry"]["consent"] == "denied"
    assert said[-1].endswith("(without its model and MCP servers, which it refused)")
    never = cli.apply_settings(8000, "k", document, wait=0, log=said.append,
                               send=lambda *a, **k: (502, None), sleep=lambda _: None)
    assert never is False and "did not answer" in said[-1]


def test_the_canvas_gets_its_uvx_a_key_and_its_port():
    env = cli.canvas_env({"PATH": "/usr/bin"})
    assert env["PATH"].startswith(str(cli.SHIM_DIR) + ":") and len(env["LOCAL_BACKEND_API_KEY"]) == 48
    assert cli.canvas_env({"PATH": "/x"})["LOCAL_BACKEND_API_KEY"] != env["LOCAL_BACKEND_API_KEY"], "one per start"
    assert env["EXTENSIONS_PATH"].endswith("node_modules/@openhands/extensions")
    assert cli.port_of(["--host", "127.0.0.1", "--port", "9000"], {}) == 9000
    assert cli.port_of(["-p=9001"], {}) == 9001 and cli.port_of([], {"PORT": "9002"}) == 9002
    assert cli.port_of([], {}) == 8000 == openhands.WEB_PORT
    # an attached value (-pVALUE) and --port=VALUE too, as the launcher's canvas_port reads them
    assert cli.port_of(["-p9003"], {}) == 9003 and cli.port_of(["--port=9004"], {}) == 9004
    assert cli.port_of(["--host", "127.0.0.1", "-p9005"], {"PORT": "9002"}) == 9005
    assert cli.port_of(["-pfoo"], {}) == 8000 and cli.port_of(["--port=70000"], {"PORT": "9002"}) == 9002
    for args in (["-p9006"], ["--port=9006"], ["--port", "9006"], ["-p", "9006"]):
        assert cli.port_of(args, {}) == openhands.canvas_port(args) == 9006


def test_a_task_needs_a_text():
    assert cli.task_words(["--text=-fix it"]) == (False, "-fix it")
    assert cli.task_words(["--resume", "go", "on"]) == (True, "go on")
    with pytest.raises(ValueError, match="needs a text"):
        cli.task_words(["--resume"])
    assert openhands.TASK == "task"


# --- --llm -----------------------------------------------------------------------------------------------------------

def test_llm_keeps_openhands_values_in_step():
    assert llm.OPENHANDS_SETTINGS == openhands.SETTINGS
    assert "openhands" in llm.AGENTS and set(llm.AGENTS) == set(s.AUTH_TARGETS)


# --- the image: the reviewed releases, the locks, its build -----------------------------------------------------------

def test_the_containerfile_installs_exactly_the_locks():
    text = CONTAINERFILE.read_text()
    assert f"ARG OPENHANDS_VERSION={openhands.VERSION}" in text
    assert f"ARG OPENHANDS_SERVER_VERSION={openhands.SERVER_VERSION}" in text
    assert "COPY Containerfile.openhands.lock" in text and "COPY Containerfile.openhands-python.lock" in text
    assert "npm ci --ignore-scripts" in text and "--require-hashes --no-deps" in text
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "COPY openhands_cli.py /usr/local/bin/openhands" in text
    assert "ln -s /usr/local/bin/openhands /opt/openhands/bin/uvx" in text and "/etc/openhands" in text
    assert "docker" not in text.lower().replace("docker.io/library/node", "").replace("no docker", "")
    for key in ("AGENT_CANVAS_DISABLE_TELEMETRY=1", "DO_NOT_TRACK=1", "OH_TELEMETRY_EXPORTER=none",
                "ANONYMIZED_TELEMETRY=false"):
        assert key in text


def test_the_npm_lock_is_of_the_reviewed_canvas_with_every_integrity():
    lock = json.loads(NPM_LOCK.read_text())
    script = load("agent_npm_lock_openhands_tests", "scripts/agent_npm_lock.py")
    assert script.check(lock, "openhands", openhands.PACKAGE, openhands.VERSION) > 500
    entry = lock["packages"]["node_modules/@openhands/agent-canvas"]
    assert entry["integrity"] == openhands.INTEGRITY
    assert "openhands" in script.AGENTS


def test_the_python_lock_pins_the_reviewed_packages_with_hashes():
    text = PYTHON_LOCK.read_text()
    script = load("openhands_lock_tests", "scripts/openhands_lock.py")
    assert script.check(text, openhands) > 200
    blocks = [block for block in re.split(r"\n(?=\S)", text) if block and not block.startswith("#")]
    assert all("--hash=sha256:" in block for block in blocks)
    assert not re.search(r"^(git\+|-e |file:)", text, re.M)
    assert "openhands-agent-server==1.58.1" in text and "openhands-automation==1.26.0" in text


def test_the_lock_script_needs_the_cutoff_and_the_packages(monkeypatch):
    script = load("openhands_lock_cmd_tests", "scripts/openhands_lock.py")
    seen = {}

    def run(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs["env"]
        return argparse.Namespace(stdout="x==1 \\\n    --hash=sha256:" + "0" * 64 + "\n")

    monkeypatch.setattr(script.subprocess, "run", run)
    script.compile_lock(openhands, str(ROOT / "tests"))
    assert "--exclude-newer" in seen["command"] and openhands.LOCK_BEFORE in seen["command"]
    assert "--generate-hashes" in seen["command"] and "--universal" in seen["command"]
    assert seen["env"]["UV_NO_CONFIG"] == "1"
    with pytest.raises(ValueError, match="does not pin"):
        script.check("x==1 \\\n    --hash=sha256:" + "0" * 64 + "\n", openhands)


def test_the_cutoff_is_fourteen_days_before_the_review():
    review = (ROOT / "docs/OPENHANDS_REVIEW.md").read_text()
    assert "Reviewed on 2026-11-14" in review and openhands.LOCK_BEFORE.startswith("2026-10-31")


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


def test_build_openhands_takes_the_reviewed_release_only(engine):
    result = build("openhands")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"OPENHANDS_VERSION={openhands.VERSION}" in calls[0]
    assert f"sandbox-ai.agent-version={openhands.VERSION}" in calls[0]
    assert any(arg.endswith("Containerfile.openhands") for arg in calls[0])
    assert build("--pin", openhands.VERSION, "openhands").returncode == 0
    refused = build("--pin", "1.40.0", "openhands")
    assert refused.returncode == 2 and "OpenHands is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("openhands", "--build-arg", "OPENHANDS_VERSION=1.35.0").returncode == 2
    assert "openhands" in build("--help").stdout


def test_build_all_builds_openhands_too():
    text = (ROOT / "sandbox/build_image.sh").read_text()
    assert re.search(r"^    all\) agents=\([^)]*\bopenhands\b[^)]*\); shift; break ;;$", text, re.M)
    images = load("images_openhands_tests", "sandbox/images.py")
    assert "openhands" in images.AGENTS and "openhands" in images.BUILD_TARGETS


# --- the agent's other places: sessions, presets, kits, its helper mode ---------------------------------------------

def test_sessions_continue_and_options():
    assert sessions.CONTINUE_ARGS["openhands"] == []
    assert sessions.SESSION_OPTIONS["openhands"] == {"--resume", "-r"}
    assert {"--port", "--host", "--text"} <= sessions.VALUE_OPTIONS["openhands"]
    restarted = sessions.restart_args({"agent": "openhands", "options": [], "agent_args": ["task", "--text=fix it"]})
    assert restarted == [], "the first task is not run again; the restart opens the Canvas"
    kept = sessions.restart_args({"agent": "openhands", "options": [], "agent_args": ["--port", "9000"]})
    assert kept == ["--port", "9000"]


def test_a_presets_first_task_runs_as_a_task():
    assert presets.prompt_args({"prompt": "-fix {what}"}, "openhands", {"what": "it"}) == ["task", "--text=-fix it"]
    assert presets.prompt_args({}, "openhands") == [] and "openhands" in presets.AGENTS


def test_kits_and_memory_for_openhands():
    assert kits.LAYOUT["openhands"] == {"skills": ".agents/skills"}
    assert kits.MEMORY_FILE["openhands"] == ("AGENTS.md", ".openhands/skills/AGENTS.md")
    assert kits.AGENT_NAMES["openhands"] == "OpenHands" and kits.MEMORY_DIRS["openhands"] == ()
    skill = (kits.BUILTIN / "openhands/skills/sandbox-ai/SKILL.md").read_text()
    assert "## OpenHands in this sandbox" in skill and all(f"`{name}`" in skill for name in openhands.FEATURES)
    assert all(host.replace("*.", "") in skill for host in openhands.TELEMETRY_HOSTS)
    assert "~/.openhands/skills/AGENTS.md" in skill and "goose" not in without_companion_commands(skill)
    assert (kits.TEMPLATES / "openhands/AGENTS.md").is_file()
    assert "~/.openhands/skills/AGENTS.md" in (kits.TEMPLATES / "openhands/skills/sandbox-example/SKILL.md").read_text()
    for kit in sorted(kits.BUILTIN_KITS.iterdir()):
        if (kit / "goose").is_dir():
            assert (kit / "openhands" / kits.SHARED_SKILLS_MARKER).is_file(), kit.name
            assert "openhands" in json.loads((kit / "kit.json").read_text())["agents"], kit.name


def test_openhands_as_a_helper_runs_one_task():
    assert agent_mcp.command("openhands", "-x", resume=True) == ["openhands", "task", "--resume", "--text=-x"]
    assert agent_mcp.command("openhands", "do it", skip=True) == ["openhands", "task", "--text=do it"]
    assert agent_mcp.SKIP["openhands"] == [] and "openhands" in agent_mcp.AGENTS


def test_the_presets_name_the_agent_and_its_notes():
    for name in ("coding-openhands", "openhands-local-llm"):
        preset = json.loads((ROOT / f"sandbox/presets/{name}.json").read_text())
        assert preset["agents"] == ["openhands"] and preset["category"] == "agents" and preset["id"] == name
        assert "--browser" not in preset["options"] and any("--browser" in note for note in preset["notes"])
        assert any("not live-tested yet" in note for note in preset["notes"])
        assert preset["translations"]["de"]["title"]
    local = json.loads((ROOT / "sandbox/presets/openhands-local-llm.json").read_text())
    assert local["options"][:2] == ["--llm", "{llm}"] and "loop_guard=on" in local["options"]


def test_the_review_is_shipped_with_the_releases_it_names():
    review = (ROOT / "docs/OPENHANDS_REVIEW.md").read_text()
    for value in (openhands.VERSION, openhands.COMMIT, openhands.SERVER_VERSION, openhands.SERVER_COMMIT,
                  openhands.AUTOMATION_VERSION, openhands.AUTOMATION_COMMIT, openhands.INTEGRITY):
        assert value in review, value
    for name in openhands.FEATURES:
        assert f"`{name}`" in review, name
    assert "## Go / No-Go" in review and "--openhands-enable" in review
    assert "## Hosts contacted by default (measured)" in review


def test_the_docs_say_telemetry_is_never_switchable():
    review = (ROOT / "docs/OPENHANDS_REVIEW.md").read_text()
    assert "### Telemetry: always off, never switchable" in review
    assert all(host in review for host in openhands.TELEMETRY_HOSTS) and "us.i.posthog.com" in review
    assert all(name in review for name in openhands.TELEMETRY_ENV if name.startswith(("AGENT", "DO_", "OH_TELE")))
