"""nanobot as a sandbox-ai agent (sandbox/nanobot.py, docs/NANOBOT_REVIEW.md): the features that turn on again what a
sandbox turns off, sandbox-ai's settings laid over nanobot's own file (the script that does it, run here), telemetry
that is never switchable, the image (the reviewed wheel and every dependency hash-pinned, no pipe into a shell), its
build, sessions, kits and helper mode. The start itself is in tests/test_feature_matrix.py (NANOBOT) and the launcher's
contracts (test_agent_sandbox_contracts.py)."""
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


nanobot = load("nanobot_tests", "sandbox/nanobot.py")
s = load("launcher_nanobot_tests", "sandbox/agent_sandbox.py")
llm = load("llm_nanobot_tests", "sandbox/llm.py")
sessions = load("sessions_nanobot_tests", "sandbox/sessions.py")
presets = load("presets_nanobot_tests", "sandbox/presets.py")
kits = load("kits_nanobot_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_nanobot_tests", "sandbox/agent_mcp.py")
LOCK = ROOT / "sandbox/Containerfile.nanobot.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.nanobot"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def ns(agent="nanobot", *enable, **more):
    return argparse.Namespace(**{"agent": agent, "nanobot_enable": list(enable), "allow_host": [], "secret_env": [],
                                 "offline": False, "agent_args": [], **more})


# --- the features ---------------------------------------------------------------------------------------------------


def without_companion_commands(skill):
    """SKILL in lower case without the one-shot commands of the agents --with (its part on other agents names
    every agent's: the rest is this agent's own)."""
    text = " ".join(skill.lower().split())
    for command in s.COMPANION_COMMANDS.values():
        text = text.replace(f"`{command.lower()}`", "")
    return text

def test_every_feature_names_its_hosts_and_says_what_it_does():
    for name, spec in nanobot.FEATURES.items():
        assert nanobot.FEATURE_SYNTAX.fullmatch(name) and spec["help"], name
        assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in spec["hosts"]), name
        assert spec["hosts"] or name == "scheduler", "only the scheduler opens nothing"
    # No feature for telemetry: never switchable. No feature for nanobot's own sandbox: it stays off.
    assert not {"telemetry", "langfuse", "tracing", "sandbox", "bwrap", "docker"} & set(nanobot.FEATURES)
    assert not nanobot.telemetry_hosts_in(nanobot.hosts(list(nanobot.FEATURES)))
    assert set(nanobot.FEATURES) == {"scheduler", "gateway:telegram", "web-search", "web-reader", "updates"}


def test_parse_happy_sad_edge():
    assert nanobot.parse(["scheduler", "updates,web-search"]) == ["scheduler", "updates", "web-search"]
    for wrong in (["docker"], ["telemetry"], ["updates;rm"], ["../x"], ["gateway:discord"], ["gateway"], ["bwrap"]):
        with pytest.raises(ValueError, match="not a nanobot feature"):
            nanobot.parse(wrong)
    assert nanobot.parse([" UPDATES ", "updates", ",,"]) == ["updates"]
    assert nanobot.parse(["gateway:all"]) == ["gateway:telegram"]
    assert nanobot.parse(None) == [] and nanobot.parse([]) == []


def test_network_options_add_only_the_features_hosts_and_secrets():
    a = ns("nanobot", "gateway:telegram", "updates")
    a.allow_host = ["github.com"]
    nanobot.network_options(a)
    assert a.allow_host == ["github.com", "api.telegram.org", "pypi.org", "raw.githubusercontent.com"]
    assert a.secret_env == ["TELEGRAM_BOT_TOKEN=nanobot_telegram"]
    given = ns("nanobot", "gateway:telegram", secret_env=["TELEGRAM_BOT_TOKEN=my_bot"])
    nanobot.network_options(given)
    assert given.secret_env == ["TELEGRAM_BOT_TOKEN=my_bot"], "the command line's own secret is kept"
    other = ns("claude", "updates")
    nanobot.network_options(other)  # not nanobot: nothing (check() refuses the option)
    assert other.allow_host == []
    offline = ns("nanobot", "updates", offline=True)
    nanobot.network_options(offline)
    assert offline.allow_host == []
    broken = ns("nanobot", "nope")
    nanobot.network_options(broken)  # the error is check()'s
    assert broken.allow_host == []


def test_the_option_counts_as_weakening():
    assert "--nanobot-enable" in s.MODES.WEAKENING_OPTIONS and s.SHORT_OPTIONS["--nanobot-enable"] == "-nb"
    assert s.MODES.weakening_options(s, ["-nb", "updates", "--no-tmux"]) == [("--nanobot-enable", "updates")]
    assert len(set(s.SHORT_OPTIONS.values())) == len(s.SHORT_OPTIONS), "every short option once"


def test_check_refuses_what_nanobot_cannot_do_here():
    nanobot.check(ns("nanobot", "updates"))  # fine
    nanobot.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="is for nanobot"):
        nanobot.check(ns("claude", "updates"))
    nanobot.check(ns("nanobot"), companions=["claude"])  # --with: a layer of the team image since 0.1.98
    with pytest.raises(ValueError, match="cannot be enforced for nanobot: version 0.4.0 has hooks only for Python programs"):
        nanobot.check(ns("nanobot"), hooks_active=True)
    with pytest.raises(ValueError, match="needs the network"):
        nanobot.check(ns("nanobot", "updates", offline=True))
    with pytest.raises(ValueError, match="nanobot update needs --nanobot-enable updates"):
        nanobot.check(ns("nanobot", agent_args=["update"]))


def test_arguments_that_would_undo_a_default_are_refused():
    for args, feature in ((["update"], "updates"), (["update", "--check"], "updates"),
                          (["gateway", "--background"], "scheduler"), (["gateway", "restart"], "scheduler")):
        refused = nanobot.refused_arg(args)
        assert refused and refused[0] == args[0] and f"--nanobot-enable {feature}" in refused[1], args
        assert nanobot.refused_arg(args, [feature]) is None, args
    for args in (["serve", "--host", "0.0.0.0"], ["serve", "-H", "10.0.0.2"], ["serve", "--host=::"],
                 ["--config", "/work/other.json"], ["agent", "-c", "x.json"], ["--home", "/tmp/n"],
                 ["webui", "--config=/x"]):
        assert nanobot.refused_arg(args, list(nanobot.FEATURES)), args
    for args in ([], ["agent", "-m", "update the docs"], ["serve", "--host", "127.0.0.1"], ["webui", "--yes"],
                 ["--", "update"], ["status"], ["agent", "--session", "s1"], ["-m", "update"],
                 ["--session", "gateway", "agent"]):
        assert nanobot.refused_arg(args) is None, args


def test_webui_gets_no_open():
    assert nanobot.command_args(["webui"]) == ["webui", "--no-open"]
    assert nanobot.command_args(["webui", "--yes"]) == ["webui", "--no-open", "--yes"]
    assert nanobot.command_args(["webui", "--no-open"]) == ["webui", "--no-open"]
    assert nanobot.command_args([]) == [] and nanobot.command_args(["agent", "-m", "webui"]) == ["agent", "-m", "webui"]


# --- sandbox-ai's settings and the variables --------------------------------------------------------------------------

def test_the_settings_hold_the_sandboxs_defaults():
    config = nanobot.managed_config(ns())
    assert config["tools"]["exec"]["sandbox"] == "", "nanobot's own bwrap sandbox stays off"
    assert config["gateway"]["host"] == config["api"]["host"] == config["channels"]["websocket"]["host"] == "127.0.0.1"
    assert config["channels"]["websocket"]["websocketRequiresToken"] is True
    assert config["gateway"]["heartbeat"]["enabled"] is False and config["agents"]["defaults"]["dream"]["enabled"] is False
    assert config["channels"]["telegram"] == {"enabled": False, "token": ""}
    assert config["tools"]["web"]["fetch"]["useJinaReader"] is False
    assert config["tools"]["webuiAllowRemotePackageInstall"] is False
    files = nanobot.files(ns(), config)
    assert set(files) == {"nanobot/sandbox-ai.json", "nanobot/pin_config.py"}
    assert json.loads(files["nanobot/sandbox-ai.json"]) == config and files["nanobot/pin_config.py"] == nanobot.PIN_SCRIPT


def test_the_features_change_only_their_settings():
    config = nanobot.managed_config(ns("nanobot", *nanobot.FEATURES))
    assert config["gateway"]["heartbeat"]["enabled"] is True and config["agents"]["defaults"]["dream"]["enabled"]
    assert config["channels"]["telegram"] == {"enabled": True, "token": "${TELEGRAM_BOT_TOKEN}"}
    assert config["tools"]["web"]["fetch"]["useJinaReader"] is True
    assert config["tools"]["exec"]["sandbox"] == "" and config["gateway"]["host"] == "127.0.0.1"


def test_mcp_servers_run_the_bridge():
    servers = nanobot.mcp_servers(["demo", "helper-claude-x", "demo"], lambda name: ["/bridge", name],
                                  {"helper-claude-x": 2000})
    assert list(servers) == ["demo", "helper-claude-x"]
    assert servers["demo"] == {"type": "stdio", "command": "python3", "args": ["/bridge", "demo"], "toolTimeout": 30}
    assert servers["helper-claude-x"]["toolTimeout"] == 2000
    assert nanobot.managed_config(ns(), servers)["tools"]["mcpServers"] == servers
    assert nanobot.mcp_servers([], lambda name: []) == {}


def test_an_api_key_of_a_known_provider_picks_nanobot_s_provider():
    placeholders = {"ANTHROPIC_API_KEY": "sandbox-ai-api-key-placeholder-ANTHROPIC_API_KEY"}
    a = ns(api_key=["api.anthropic.com=ANTHROPIC_API_KEY"])
    assert nanobot.api_provider(a, placeholders) == ("anthropic", placeholders["ANTHROPIC_API_KEY"])
    config = nanobot.managed_config(a, placeholders=placeholders)
    assert config["providers"] == {"anthropic": {"apiKey": placeholders["ANTHROPIC_API_KEY"]}}
    assert config["agents"]["defaults"]["provider"] == "anthropic"
    assert nanobot.api_provider(ns(api_key=["api.deepseek.com=DEEPSEEK_API_KEY=s"]))[0] == "deepseek"
    for keys, more in ((["api.anthropic.com=CLAUDE_KEY"], {}),
                       (["api.anthropic.com=ANTHROPIC_API_KEY", "api.openai.com=OPENAI_API_KEY"], {}),
                       (["api.anthropic.com=ANTHROPIC_API_KEY"], {"llm": ["ollama"]}),
                       (["example.org=KEY"], {}), ([], {})):
        assert nanobot.api_provider(ns(api_key=keys, **more)) is None, keys
        assert "providers" not in nanobot.managed_config(ns(api_key=keys, **more))
    # The launcher's own placeholder for that variable is what nanobot gets (never the key).
    assert s.api_key_environment(argparse.Namespace(api_key=["api.anthropic.com=ANTHROPIC_API_KEY"])) == {
        "ANTHROPIC_API_KEY": s.API_KEY_PLACEHOLDER.format(name="ANTHROPIC_API_KEY")}


def test_model_and_effort_are_settings():
    config = nanobot.managed_config(ns(model="anthropic/claude-sonnet-4-5", effort="high"))
    assert config["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-4-5"
    assert config["agents"]["defaults"]["reasoningEffort"] == "high"
    assert nanobot.EFFORTS == s.AGENT_EFFORTS["nanobot"] and s.DANGER_ENV["nanobot"] == {}
    assert s.model_effort_args(argparse.Namespace(agent="nanobot", model="m", effort="high")) == []


def test_the_variables_pin_telemetry_and_the_install():
    env = nanobot.environment(ns("nanobot", *nanobot.FEATURES))
    for key, value in nanobot.TELEMETRY_ENV.items():
        assert env[key] == value
    assert env["LANGFUSE_SECRET_KEY"] == "" and env["LANGFUSE_TRACING_ENABLED"] == "false"
    assert env["NANOBOT_TUI_NO_DOWNLOAD"] == "1" and env["TIKTOKEN_CACHE_DIR"] == nanobot.TIKTOKEN_DIR


def test_telemetry_hosts_are_never_opened():
    assert nanobot.telemetry_hosts_in(["cloud.langfuse.com"]) == ["*.langfuse.com"]
    assert nanobot.telemetry_hosts_in(["us.i.posthog.com", "o1.ingest.sentry.io"]) == ["*.posthog.com", "*.sentry.io"]
    assert nanobot.telemetry_hosts_in(["api.anthropic.com", "pypi.org", "langfuse.com.evil"]) == []
    for opened in ({"allow_host": ["cloud.langfuse.com"]}, {"no_inspect": ["*.sentry.io"]},
                   {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            nanobot.check_telemetry(argparse.Namespace(**opened))
    with pytest.raises(ValueError, match="drop --allow-domain langfuse.com"):
        nanobot.check_telemetry(argparse.Namespace(allow_host=["langfuse.com", "*.langfuse.com"],
                                                   allow_domain=["langfuse.com"]))
    rules = nanobot.telemetry_rules(ns())
    assert [rule["host"] for rule in rules] == [*nanobot.rule_hosts(), "raw.githubusercontent.com", "pypi.org"]
    assert all(rule["decision"] == "deny" for rule in rules)
    assert [rule["path"] for rule in rules[-2:]] == ["/HKUDS/nanobot/**", "/pypi/nanobot-ai/**"]
    assert [rule["host"] for rule in nanobot.telemetry_rules(ns("nanobot", "updates"))] == nanobot.rule_hosts()


def test_the_provider_hosts_hold_no_telemetry_host():
    assert s.PROVIDERS["nanobot"] == [] and not nanobot.telemetry_hosts_in(nanobot.API_PROVIDERS)


def test_plan_says_what_is_on_and_off():
    plan = nanobot.plan(ns("nanobot", "scheduler"))
    assert plan["version"] == nanobot.VERSION and plan["commit"] == nanobot.COMMIT and "experimental" not in plan
    assert plan["telemetry"]["switchable"] is False and plan["own_sandbox"] == "off"
    assert plan["enabled"] == [{"feature": "scheduler", "hosts": [], "secrets": []}]
    assert plan["off"] == ["gateway:telegram", "web-search", "web-reader", "updates"]
    assert plan["settings"] == "/etc/nanobot/sandbox-ai.json" and plan["loopback"]["webui"] == "127.0.0.1:8765"


def test_nanobot_has_no_login_to_keep(tmp_path):
    assert not nanobot.login_document_ok({"providers": {"anthropic": {"apiKey": "sk"}}})
    assert not s.login_document_ok("nanobot", {"x": "y"})
    assert s.auth_sources("nanobot") == [(tmp_path / "home/.nanobot/config.json", ".nanobot/config.json")]
    assert s.merge_refreshed("nanobot", {"a": 1}, {"a": 2}) is None
    assert "nanobot" in s.NO_LOGIN_AGENTS and "nanobot" in s.LOGIN_COMMANDLESS
    login = load("login_nanobot_tests", "sandbox/login.py")
    assert "nanobot" not in login.AGENTS


# --- the script that lays the settings over nanobot's file -------------------------------------------------------------

def pin(tmp_path, settings, current=None, raw=None):
    directory = tmp_path / "etc"
    directory.mkdir(exist_ok=True)
    (directory / "sandbox-ai.json").write_text(json.dumps(settings))
    (directory / "pin_config.py").write_text(nanobot.PIN_SCRIPT)
    target = tmp_path / "home/.nanobot/config.json"
    if current is not None or raw is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw if raw is not None else json.dumps(current))
    result = subprocess.run([sys.executable, "-I", str(directory / "pin_config.py")], capture_output=True, text=True,
                            env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}, timeout=30)
    return result, target


def test_the_settings_win_and_the_users_other_keys_stay(tmp_path):
    settings = nanobot.managed_config(ns("nanobot", "web-search"))
    user = {"agents": {"defaults": {"model": "mine", "dream": {"enabled": True, "intervalH": 4}}},
            "tools": {"exec": {"sandbox": "bwrap", "maxRuntime": 60}, "mcpServers": {"own": {"command": "x"}}},
            "channels": {"telegram": {"enabled": True, "token": "${TELEGRAM_BOT_TOKEN}", "allowFrom": ["42"]}}}
    result, target = pin(tmp_path, settings, user)
    assert result.returncode == 0, result.stderr
    merged = json.loads(target.read_text())
    assert merged["tools"]["exec"] == {"sandbox": "", "maxRuntime": 60}, "the pin wins, the user's other key stays"
    assert merged["agents"]["defaults"]["model"] == "mine"
    assert merged["agents"]["defaults"]["dream"] == {"enabled": False, "intervalH": 4}
    assert merged["tools"]["mcpServers"] == {"own": {"command": "x"}}
    assert merged["channels"]["telegram"] == {"enabled": False, "token": "", "allowFrom": ["42"]}, \
        "no token reference is left behind for a start without the variable"
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_a_first_start_gets_a_new_file_and_a_broken_one_stops_nanobot(tmp_path):
    result, target = pin(tmp_path, {"api": {"host": "127.0.0.1"}})
    assert result.returncode == 0 and json.loads(target.read_text()) == {"api": {"host": "127.0.0.1"}}
    for raw in ("{not json", "[1, 2]"):
        result, target = pin(tmp_path, {"api": {"host": "127.0.0.1"}}, raw=raw)
        assert result.returncode == 1 and "nanobot is not started" in result.stderr
        assert target.read_text() == raw, "a file sandbox-ai cannot read is left as it is"


def test_a_link_in_place_of_the_file_is_replaced_not_followed(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"keep": True}))
    target = tmp_path / "home/.nanobot/config.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    result, _ = pin(tmp_path, {"api": {"host": "127.0.0.1"}})
    assert result.returncode == 0 and not target.is_symlink()
    assert json.loads(outside.read_text()) == {"keep": True}


# --- --llm -----------------------------------------------------------------------------------------------------------

def test_llm_keeps_nanobot_s_values_in_step():
    assert llm.NANOBOT_SETTINGS == nanobot.SETTINGS
    assert "nanobot" in llm.AGENTS and set(llm.AGENTS) == set(s.AUTH_TARGETS)


def test_llm_gives_nanobot_its_custom_provider(monkeypatch):
    found = {"name": "ollama", "parsed": {"host": "127.0.0.1"}}
    monkeypatch.setattr(llm, "selection", lambda a: (found, "qwen3-coder"))
    monkeypatch.setattr(llm, "llm_context", lambda a, f, m: 32768)
    monkeypatch.setattr(llm, "agent_url", lambda f, api="openai": "http://127.0.0.1:41000/v1")
    monkeypatch.setattr(llm, "loop_guard_of", lambda a, f: {"enabled": True, "steps": 40, "instructions": True})
    a = argparse.Namespace(agent="nanobot", llm=["ollama"])
    config = llm.nanobot_config(a)
    assert config["providers"] == {"custom": {"apiBase": "http://127.0.0.1:41000/v1", "apiKey": llm.PLACEHOLDER}}
    assert config["agents"]["defaults"] == {"provider": "custom", "model": "qwen3-coder",
                                            "contextWindowTokens": 32768, "maxToolIterations": 40}
    assert llm.agent_environment(a) == {}, "nothing in variables: the settings carry it"
    merged = nanobot.managed_config(ns(llm=["ollama"]), llm=config)
    assert merged["agents"]["defaults"]["dream"] == {"enabled": False} and merged["providers"]["custom"]


# --- the image: the reviewed release, the lock, its build -------------------------------------------------------------

def test_the_containerfile_installs_exactly_the_locked_release():
    text = CONTAINERFILE.read_text()
    assert f"ARG NANOBOT_VERSION={nanobot.VERSION}" in text and "COPY Containerfile.nanobot.lock" in text
    assert "--require-hashes --no-deps" in text and "--index-url https://pypi.org/simple" in text
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "NANOBOT_TUI_NO_DOWNLOAD=1" in text and f"TIKTOKEN_CACHE_DIR={nanobot.TIKTOKEN_DIR}" in text
    # The tokenizer's vocabulary: checked against tiktoken's own SHA-256, under the name tiktoken looks for.
    import hashlib
    url = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    assert hashlib.sha1(url.encode()).hexdigest() in text and url in text
    assert "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7" in text and "sha256sum -c --strict" in text
    # The nanobot command lays the settings over nanobot's file first, then runs nanobot.
    assert nanobot.PIN_FILE in text and 'exec /opt/nanobot/venv/bin/nanobot "$@"' in text
    assert "mkdir -p -m 755 /etc/nanobot" in text


def test_the_lock_holds_the_reviewed_wheels_and_hashes_everything():
    text = LOCK.read_text()
    blocks = re.split(r"\n(?=[A-Za-z0-9])", text.split("\n\n", 1)[-1].strip())
    requirements = [block for block in blocks if not block.startswith("#")]
    assert requirements and all("--hash=sha256:" in block for block in requirements)
    names = [block.split("==", 1)[0].split()[0] for block in requirements if "==" in block]
    assert len(names) == len(set(names)) and len(names) == len(requirements)
    own = next(block for block in requirements if block.startswith(f"nanobot-ai=={nanobot.VERSION}"))
    assert all(digest in own for _, digest in nanobot.WHEELS.values())
    # The Telegram channel's packages (gateway:telegram), nothing nanobot would install at run time otherwise.
    assert "\npython-telegram-bot==" in text and "\nlangfuse==" not in text and "\nboto3==" not in text


def test_the_lock_script_takes_the_reviewed_release_and_the_14_day_rule():
    script = load("nanobot_lock_tests", "scripts/nanobot_lock.py")
    assert script.VERSION == nanobot.VERSION and script.PYTHON == "3.12"
    assert script.CUTOFF <= "2026-10-31T00:00:00Z" and "--exclude-newer" in (ROOT / "scripts/nanobot_lock.py").read_text()
    assert script.HEADER.splitlines()[0] in LOCK.read_text()


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


def test_build_nanobot_takes_the_reviewed_release_only(engine):
    result = build("nanobot")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"NANOBOT_VERSION={nanobot.VERSION}" in calls[0]
    assert f"sandbox-ai.agent-version={nanobot.VERSION}" in calls[0]
    assert any(arg.endswith("Containerfile.nanobot") for arg in calls[0])
    assert build("--pin", nanobot.VERSION, "nanobot").returncode == 0
    refused = build("--pin", "0.4.1", "nanobot")
    assert refused.returncode == 2 and "nanobot is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("nanobot", "--build-arg", "NANOBOT_VERSION=0.4.1").returncode == 2
    assert "nanobot" in build("--help").stdout


def test_build_all_builds_nanobot_too():
    text = (ROOT / "sandbox/build_image.sh").read_text()
    line = next(line for line in text.splitlines() if line.strip().startswith("all) agents=("))
    assert "nanobot" in line.split("(", 1)[1].split(")", 1)[0].split()


# --- the agent's other places: sessions, presets, kits, its helper mode ---------------------------------------------

def test_sessions_and_options():
    assert sessions.CONTINUE_ARGS["nanobot"] == [] and sessions.SESSION_OPTIONS["nanobot"] == {"--session", "-s"}
    assert {"--message", "-m", "--session", "--workspace", "--config"} <= sessions.VALUE_OPTIONS["nanobot"]
    restarted = sessions.restart_args({"agent": "nanobot", "options": [], "agent_args": ["agent", "--message=fix it"]})
    assert "--message=fix it" not in restarted, "the first task is not run again"


def test_a_presets_first_task_runs_as_one_message():
    args = presets.prompt_args({"prompt": "-fix {what}"}, "nanobot", {"what": "it"})
    assert args[0] == "agent" and args[1].startswith("--message=") and args[1].endswith("\n\n-fix it")
    assert "/work" in args[1] and presets.prompt_args({}, "nanobot") == [] and "nanobot" in presets.AGENTS


def test_kits_and_memory_for_nanobot():
    assert kits.LAYOUT["nanobot"] == {"skills": ".nanobot/workspace/skills"}
    assert kits.MEMORY_FILE["nanobot"] == ("MEMORY.md", ".nanobot/workspace/memory/MEMORY.md")
    skill = (kits.BUILTIN / "nanobot/skills/sandbox-ai/SKILL.md").read_text()
    assert "## nanobot in this sandbox" in skill and all(f"`{name}`" in skill for name in nanobot.FEATURES)
    assert all(host in skill for host in nanobot.TELEMETRY_HOSTS) and "~/.nanobot/workspace/memory/MEMORY.md" in skill
    assert "goose" not in without_companion_commands(skill)
    assert (kits.TEMPLATES / "nanobot/MEMORY.md").is_file()
    example = (kits.TEMPLATES / "nanobot/skills/sandbox-example/SKILL.md").read_text()
    assert "~/.nanobot/workspace/memory/MEMORY.md" in example and "home/nanobot/" in example
    for kit in sorted(kits.BUILTIN_KITS.iterdir()):
        if (kit / "goose").is_dir():
            assert (kit / "nanobot" / kits.SHARED_SKILLS_MARKER).is_file(), kit.name
            assert "nanobot" in json.loads((kit / "kit.json").read_text())["agents"], kit.name


def test_nanobot_as_a_helper_runs_one_message():
    agent_mcp.NANOBOT_SESSIONS.clear()
    first = agent_mcp.command("nanobot", "-x")
    assert first[:3] == ["nanobot", "agent", "--no-markdown"] and first[3] == "--session"
    assert first[-1] == "--message=" + agent_mcp.NANOBOT_PROJECT_NOTE + "-x"
    assert agent_mcp.command("nanobot", "more", resume=True)[4] == first[4]
    assert agent_mcp.command("nanobot", "new")[4] != first[4]
    assert agent_mcp.answer_of("nanobot", "  Created SOUL.md\n\n\N{CAT} nanobot\nDone.\nTwo lines.\n") == \
        "Done.\nTwo lines."
    assert agent_mcp.answer_of("nanobot", "plain\n") == "plain" and "nanobot" in agent_mcp.AGENTS


def test_the_review_is_shipped_with_the_release_it_names():
    review = (ROOT / "docs/NANOBOT_REVIEW.md").read_text()
    assert nanobot.VERSION in review and nanobot.COMMIT in review
    assert all(digest in review for _, digest in nanobot.WHEELS.values())
    for name in nanobot.FEATURES:
        assert f"`{name}`" in review, name
    assert "## Go / No-Go" in review and "--nanobot-enable" in review
    assert "## Hosts contacted by default (measured)" in review and "`tools.exec.sandbox: \"\"`" in review


def test_the_docs_say_telemetry_is_never_switchable():
    review = (ROOT / "docs/NANOBOT_REVIEW.md").read_text()
    assert "### Telemetry: always off, never switchable" in review
    assert all(host in review for host in nanobot.TELEMETRY_HOSTS)
    assert all(name in review for name in nanobot.TELEMETRY_ENV)
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "nanobot" in text and "--nanobot-enable" in text, readme
        assert all(host in text for host in nanobot.TELEMETRY_HOSTS), readme


def test_every_agent_list_names_each_agent_once_and_only_real_ones():
    """Regression (the merge with OpenHands): two agents added to the same list side by side once lost a comma
    (("goose", "openhands" "nanobot") is the one name "openhandsnanobot"). Every list of agents in the launcher's
    modules names only agents of the launcher, each once, and the full lists name all of them."""
    agents = set(s.AUTH_TARGETS)
    assert {"openhands", "nanobot"} <= agents
    for name in ("NO_LOGIN_AGENTS", "LOGIN_COMMANDLESS", "NOT_IN_TEAM", "COMPANION_AGENTS"):
        values = getattr(s, name)
        assert set(values) <= agents and len(values) == len(set(values)), name
    assert {"goose", "openhands", "nanobot"} <= set(s.NO_LOGIN_AGENTS)
    for module_name in ("kits", "kitctl", "images", "agent_mcp", "presets", "webui", "starts", "llm", "completion"):
        module = load(f"{module_name}_lists_nanobot_tests", f"sandbox/{module_name}.py")
        assert sorted(module.AGENTS) == sorted(agents), module_name
