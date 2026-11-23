"""Goose as a sandbox-ai agent (sandbox/goose.py, docs/GOOSE_REVIEW.md): the features that turn on again what a sandbox
turns off, its system configuration and variables, telemetry that is never switchable, the image (the reviewed
release's binary checked against its SHA-256, no pipe into a shell), its build, sessions, kits and helper mode. The
start itself is in tests/test_feature_matrix.py (GOOSE) and the launcher's contracts (test_agent_sandbox_contracts.py)."""
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


goose = load("goose_tests", "sandbox/goose.py")
s = load("launcher_goose_tests", "sandbox/agent_sandbox.py")
llm = load("llm_goose_tests", "sandbox/llm.py")
sessions = load("sessions_goose_tests", "sandbox/sessions.py")
presets = load("presets_goose_tests", "sandbox/presets.py")
kits = load("kits_goose_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_goose_tests", "sandbox/agent_mcp.py")
LOCK = ROOT / "sandbox/Containerfile.goose.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.goose"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def ns(agent="goose", *enable, **more):
    return argparse.Namespace(**{"agent": agent, "goose_enable": list(enable), "allow_host": [], "secret_env": [],
                                 "offline": False, "agent_args": [], **more})


# --- the features ---------------------------------------------------------------------------------------------------

def test_every_feature_names_its_hosts_and_says_what_it_does():
    for name, spec in goose.FEATURES.items():
        assert goose.FEATURE_SYNTAX.fullmatch(name) and spec["help"], name
        assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in spec["hosts"]), name
        assert spec["hosts"] or spec.get("background") == "serve", "only the scheduler opens nothing"
    # No feature for its telemetry: never switchable.
    assert not {"telemetry", "usage-data", "posthog"} & set(goose.FEATURES)
    assert not goose.telemetry_hosts_in(goose.hosts(list(goose.FEATURES)))
    assert set(goose.FEATURES) == {"scheduler", "gateway:telegram", "local-inference", "model-catalog", "updates"}


def test_parse_happy_sad_edge():
    assert goose.parse(["scheduler", "updates,local-inference"]) == ["scheduler", "updates", "local-inference"]
    for wrong in (["docker"], ["telemetry"], ["updates;rm"], ["../x"], ["gateway:discord"], ["gateway"]):
        with pytest.raises(ValueError, match="not a Goose feature"):
            goose.parse(wrong)
    assert goose.parse([" UPDATES ", "updates", ",,"]) == ["updates"]
    assert goose.parse(["gateway:all"]) == ["gateway:telegram"]
    assert goose.parse(None) == [] and goose.parse([]) == []


def test_network_options_add_only_the_features_hosts_and_secrets():
    a = ns("goose", "gateway:telegram", "updates")
    a.allow_host = ["github.com"]
    goose.network_options(a)
    assert a.allow_host == ["github.com", "api.telegram.org", "api.github.com", "objects.githubusercontent.com",
                            "release-assets.githubusercontent.com", "tmaproduction.blob.core.windows.net"]
    assert a.secret_env == ["TELEGRAM_BOT_TOKEN=goose_telegram"]
    given = ns("goose", "gateway:telegram", secret_env=["TELEGRAM_BOT_TOKEN=my_bot"])
    goose.network_options(given)
    assert given.secret_env == ["TELEGRAM_BOT_TOKEN=my_bot"], "the command line's own secret is kept"
    other = ns("claude", "updates")
    goose.network_options(other)  # not Goose: nothing (check() refuses the option)
    assert other.allow_host == []
    offline = ns("goose", "updates", offline=True)
    goose.network_options(offline)
    assert offline.allow_host == []
    broken = ns("goose", "nope")
    goose.network_options(broken)  # the error is check()'s
    assert broken.allow_host == []


def test_the_option_counts_as_weakening():
    assert "--goose-enable" in s.MODES.WEAKENING_OPTIONS and s.SHORT_OPTIONS["--goose-enable"] == "-gx"
    assert s.MODES.weakening_options(s, ["-gx", "updates", "--no-tmux"]) == [("--goose-enable", "updates")]
    assert len(set(s.SHORT_OPTIONS.values())) == len(s.SHORT_OPTIONS), "every short option once"


def test_check_refuses_what_goose_cannot_do_here():
    goose.check(ns("goose", "updates"))  # fine
    goose.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="is for Goose"):
        goose.check(ns("claude", "updates"))
    goose.check(ns("goose"), companions=["claude"])  # --with: a layer of the team image since 0.1.98
    goose.check(ns("goose"), hooks_active=True)  # rules for its own tools: sandbox-ai's hooks plugin
    with pytest.raises(ValueError, match="needs the network"):
        goose.check(ns("goose", "updates", offline=True))


def test_a_subcommand_of_a_feature_that_is_off_is_refused():
    for args, feature in ((["update"], "updates"), (["update", "--canary"], "updates"),
                          (["local-models", "download", "x"], "local-inference"), (["lm", "list"], "local-inference"),
                          (["gateway", "start", "telegram"], "gateway:telegram"), (["gw", "pair"], "gateway:telegram")):
        refused = goose.refused_arg(args)
        assert refused and refused[0] == args[0] and f"--goose-enable {feature}" in refused[1], args
        assert goose.refused_arg(args, [feature]) is None, args
    for args in (["serve", "--host", "0.0.0.0"], ["serve", "--host=10.0.0.2"], ["serve", "--dangerously-unauthenticated"]):
        assert goose.refused_arg(args, list(goose.FEATURES)), args
    for args in (["session"], ["run", "--text", "update the docs"], ["serve", "--host", "127.0.0.1"], [],
                 ["--", "update"], ["recipe", "list"], ["schedule", "list"]):
        assert goose.refused_arg(args) is None, args


# --- the system configuration and the variables ---------------------------------------------------------------------

def test_the_system_configuration_holds_the_sandboxs_defaults():
    config = goose.managed_config(ns())
    assert config == {"GOOSE_MODE": "smart_approve", "GOOSE_DISABLE_KEYRING": True, "GOOSE_TELEMETRY_ENABLED": False}
    files = goose.files(ns(), config)
    assert list(files) == ["goose/config.yaml"] and json.loads(files["goose/config.yaml"]) == config
    looped = goose.files(ns(), config, "LOOP")
    assert looped["goose/sandbox-ai-loop-guard.md"] == "LOOP" and goose.LOOP_FILE.endswith("/sandbox-ai-loop-guard.md")


def test_mcp_servers_become_stdio_extensions_through_the_bridge():
    extensions = goose.extensions(["demo", "helper-claude-x", "demo"], lambda name: ["/bridge", name],
                                  {"helper-claude-x": 2000})
    assert list(extensions) == ["demo", "helper-claude-x"]
    assert extensions["demo"]["cmd"] == "python3" and extensions["demo"]["args"] == ["/bridge", "demo"]
    assert extensions["demo"]["type"] == "stdio" and extensions["demo"]["timeout"] == 300
    assert extensions["helper-claude-x"]["timeout"] == 2000
    assert goose.managed_config(ns(), extensions)["extensions"] == extensions
    assert goose.extensions([], lambda name: []) == {}


# CI's first live run (43633858837): `goose run --quiet --text=...` answered (the demo server's version in its output)
# and the run ended with 143. Goose 1.57.0 closes an extension's stdin and exits at once; its extensions run with
# PR_SET_PDEATHSIG SIGTERM (crates/goose/src/subprocess.rs, configure_parent_death_signal). The launcher's bridge
# waits for the server's end to close after that EOF (several relays away), so it is still there when Goose exits and
# ends by SIGTERM - the last process of the sandbox, and Firejail 0.9.72 exits with the status of the last process to
# end (sandbox.c, monitor_application) unless --deterministic-exit-code: 128 + 15.

def test_firejail_exits_with_the_agents_own_status():
    runtime = load("runtime_goose_tests", "sandbox/runtime.py")
    helps = {"text": "  --deterministic-exit-code - always exit with first child's status code.\n  --shell=none\n"}
    original = runtime.subprocess.run
    runtime.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, helps["text"], "")
    try:
        command = runtime.firejail_command({})
        assert "--deterministic-exit-code" in command
        assert command.index("--deterministic-exit-code") < command.index(runtime.SUPERVISOR_PYTHON[0])
        assert "--deterministic-exit-code" in runtime.firejail_command({}, nested=True, shutdown=True)
        helps["text"] = "  --shell=none\n"  # a Firejail without it: started as before
        assert "--deterministic-exit-code" not in runtime.firejail_command({})
    finally:
        runtime.subprocess.run = original


def test_the_bridge_outlives_the_agents_eof_until_the_server_closes(tmp_path):
    """Why the bridge is still running when Goose exits: after its stdin's EOF it only half-closes the socket and
    waits for the server's end (runtime.mcp_bridge)."""
    import socket
    import threading
    import time
    path = str(tmp_path / "server.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    accepted = []
    threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True).start()
    bridge = subprocess.Popen([sys.executable, "-I", str(ROOT / "sandbox/runtime.py"), "--internal-mcp-bridge", path],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        bridge.stdin.close()  # the agent's EOF
        deadline = time.monotonic() + 5
        while not accepted and time.monotonic() < deadline:
            time.sleep(0.01)
        assert accepted and accepted[0].recv(10) == b""  # the EOF reached the server's end...
        time.sleep(0.3)
        assert bridge.poll() is None  # ...and the bridge still waits for that end to close
        accepted[0].close()
        assert bridge.wait(timeout=5) == 0
    finally:
        if bridge.poll() is None:
            bridge.kill()
            bridge.wait()
        listener.close()


def test_an_api_key_of_a_known_provider_picks_goose_s_provider():
    assert goose.api_provider(ns(api_key=["api.anthropic.com=ANTHROPIC_API_KEY"])) == "anthropic"
    assert goose.api_provider(ns(api_key=["openrouter.ai=OPENROUTER_API_KEY=my_secret"])) == "openrouter"
    assert goose.managed_config(ns(api_key=["api.openai.com=OPENAI_API_KEY"]))["GOOSE_PROVIDER"] == "openai"
    # Sad: a key under another variable than Goose reads, two providers, an --llm, an unknown host.
    for keys, more in ((["api.anthropic.com=CLAUDE_KEY"], {}),
                       (["api.anthropic.com=ANTHROPIC_API_KEY", "api.openai.com=OPENAI_API_KEY"], {}),
                       (["api.anthropic.com=ANTHROPIC_API_KEY"], {"llm": ["ollama"]}),
                       (["example.org=KEY"], {}), ([], {})):
        assert goose.api_provider(ns(api_key=keys, **more)) is None, keys
        assert "GOOSE_PROVIDER" not in goose.managed_config(ns(api_key=keys, **more))


def test_the_variables_pin_telemetry_and_carry_model_and_effort():
    env = goose.environment(ns(model="claude-sonnet-4-5", effort="max"))
    assert env["GOOSE_MODEL"] == "claude-sonnet-4-5" and env["GOOSE_THINKING_EFFORT"] == "max"
    assert env["GOOSE_DISABLE_KEYRING"] == "1"
    plain = goose.environment(ns())
    assert "GOOSE_MODEL" not in plain and "GOOSE_MODE" not in plain
    assert goose.EFFORTS == s.AGENT_EFFORTS["goose"] and s.DANGER_ENV["goose"] == {"GOOSE_MODE": "auto"}


def test_telemetry_is_pinned_off_whatever_is_enabled():
    """The user's rule: never switchable. Every feature: the variables (which win over every file of Goose's) and
    the configuration's pin hold."""
    env = goose.environment(ns("goose", *goose.FEATURES))
    for key, value in goose.TELEMETRY_ENV.items():
        assert env[key] == value
    assert env["GOOSE_TELEMETRY_OFF"] == "1" and env["GOOSE_TELEMETRY_ENABLED"] == "false"
    assert env["OTEL_SDK_DISABLED"] == "true", "also a user's otel_exporter_otlp_endpoint (measured)"
    sneaky = {"GOOSE_TELEMETRY_ENABLED": True}
    assert goose.managed_config(ns("goose", *goose.FEATURES), sneaky)["GOOSE_TELEMETRY_ENABLED"] is False


def test_telemetry_hosts_are_never_opened():
    assert goose.telemetry_hosts_in(["us.i.posthog.com"]) == ["*.posthog.com"]
    assert goose.telemetry_hosts_in(["*.posthog.com", "eu.i.posthog.com"]) == ["*.posthog.com"]
    assert goose.telemetry_hosts_in(["api.anthropic.com", "models.dev", "posthog.com.evil"]) == []
    for opened in ({"allow_host": ["us.i.posthog.com"]}, {"no_inspect": ["*.posthog.com"]},
                   {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            goose.check_telemetry(argparse.Namespace(**opened))
    rules = goose.telemetry_rules(ns())
    assert [rule["host"] for rule in rules] == [*goose.rule_hosts(), "models.dev"]
    assert all(rule["decision"] == "deny" for rule in rules)
    assert [rule["host"] for rule in goose.telemetry_rules(ns("goose", "model-catalog"))] == goose.rule_hosts()


def test_the_provider_hosts_hold_no_telemetry_host():
    assert s.PROVIDERS["goose"] == [] and not goose.telemetry_hosts_in(goose.API_PROVIDERS)


def test_background_parts_start_before_goose_and_goose_stays_the_program():
    assert goose.command(ns(), "/usr/local/bin/goose", ["session"]) == ["/usr/local/bin/goose", "session"]
    command = goose.command(ns("goose", "scheduler", "gateway:telegram"), "goose", ["run", "--text=hi"])
    assert command[:2] == ["/bin/sh", "-c"] and command[3:] == ["goose", "run", "--text=hi"]
    script = command[2]
    assert "serve --host 127.0.0.1 --port 3284" in script and "telegram" in script
    assert script.splitlines()[-1] == 'exec "$0" "$@"'
    assert "--dangerously-unauthenticated" not in script and "$TELEGRAM_BOT_TOKEN" in script
    # A helper's sandbox runs goose run per task: no background part there.
    assert goose.command(ns("goose", "scheduler", helper_of="x"), "goose", []) == ["goose"]
    # The script runs: with sh, a fake goose that records its arguments (no gateway without its token).
    assert goose.background_script(["local-inference"]) == []


def test_the_background_script_runs_in_a_shell(tmp_path):
    fake = tmp_path / "goose"
    log = tmp_path / "calls"
    fake.write_text(f"#!/bin/sh\necho \"$*\" >> {log}\n")
    fake.chmod(0o755)
    command = goose.command(ns("goose", "scheduler", "gateway:telegram"), str(fake), ["session", "--name", "a b"])
    result = subprocess.run(command, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TELEGRAM_BOT_TOKEN": "t"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    import time
    for _ in range(50):
        calls = log.read_text().splitlines() if log.exists() else []
        if len(calls) >= 3:
            break
        time.sleep(0.1)
    assert "session --name a b" in calls
    assert "serve --host 127.0.0.1 --port 3284" in calls and "gateway start --bot-token t telegram" in calls
    assert (tmp_path / ".local/state/goose/logs").is_dir()


def test_plan_says_what_is_on_and_off():
    plan = goose.plan(ns("goose", "scheduler"))
    assert plan["version"] == goose.VERSION and plan["commit"] == goose.COMMIT and "experimental" not in plan
    assert plan["telemetry"]["switchable"] is False and plan["background"] == ["serve"]
    assert plan["enabled"] == [{"feature": "scheduler", "hosts": [], "secrets": []}]
    assert plan["off"] == ["gateway:telegram", "local-inference", "model-catalog", "updates"]
    assert plan["system_config"] == "/etc/goose/config.yaml" and plan["mode"] == "smart_approve"


def test_goose_has_no_login_to_keep(tmp_path):
    assert not goose.login_document_ok({"ANTHROPIC_API_KEY": "sk-ant"}) and not goose.login_document_ok({})
    assert not s.login_document_ok("goose", {"x": "y"})
    assert s.auth_sources("goose") == [(tmp_path / "xdg_config_home/goose/secrets.yaml", ".config/goose/secrets.yaml")]
    assert s.merge_refreshed("goose", {"a": 1}, {"a": 2}) is None
    login = load("login_goose_tests", "sandbox/login.py")
    assert "goose" not in login.AGENTS


# --- --llm -----------------------------------------------------------------------------------------------------------

def test_llm_keeps_goose_s_values_in_step():
    assert llm.GOOSE_CONFIG == goose.CONFIG and llm.GOOSE_LOOP_FILE == goose.LOOP_FILE
    assert "goose" in llm.AGENTS and set(llm.AGENTS) == set(s.AUTH_TARGETS)


# --- the image: the reviewed release, the lock, its build -------------------------------------------------------------

def test_the_containerfile_installs_exactly_the_reviewed_binary():
    text = CONTAINERFILE.read_text()
    assert f"ARG GOOSE_VERSION={goose.VERSION}" in text and "COPY Containerfile.goose.lock" in text
    assert "sha256sum -c --strict" in text and "releases/download/v${GOOSE_VERSION}/${asset}" in text
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert "download_cli.sh" not in text.replace("never upstream's download_cli.sh", "")
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "mkdir -p -m 755 /etc/goose" in text and "install -m 0755 goose /usr/local/bin/goose" in text
    # The digest is checked before anything of the tarball is unpacked.
    assert text.index("sha256sum -c") < text.index("tar -xzf")


def test_the_lock_holds_the_reviewed_assets_digests():
    rows = [line.split() for line in LOCK.read_text().splitlines() if line and not line.startswith("#")]
    assert {name: digest for digest, name in rows} == {name: digest for name, digest in goose.ASSETS.values()}
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest, _ in rows)
    assert all(re.fullmatch(r"goose-(x86_64|aarch64)-unknown-linux-gnu\.tar\.gz", name) for _, name in rows)
    assert "v" + goose.VERSION in LOCK.read_text()


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


def test_build_goose_takes_the_reviewed_release_only(engine):
    result = build("goose")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"GOOSE_VERSION={goose.VERSION}" in calls[0]
    assert f"sandbox-ai.agent-version={goose.VERSION}" in calls[0]
    assert any(arg.endswith("Containerfile.goose") for arg in calls[0])
    assert build("--pin", goose.VERSION, "goose").returncode == 0
    refused = build("--pin", "1.59.0", "goose")
    assert refused.returncode == 2 and "Goose is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("goose", "--build-arg", "GOOSE_VERSION=1.58.1").returncode == 2
    assert "goose" in build("--help").stdout


def test_build_all_builds_every_agent():
    """`sandbox-ai build all` builds every agent the app offers (it left the new ones out, and their starts from
    the app failed with "image not known" after a build all)."""
    text = (ROOT / "sandbox/build_image.sh").read_text()
    assert "    all) agents=(codex claude opencode opencode2 hermes dsh qwen goose openhands nanobot letta pi); shift; break ;;" in text


# --- the agent's other places: sessions, presets, kits, its helper mode ---------------------------------------------

def test_sessions_continue_and_options():
    assert sessions.CONTINUE_ARGS["goose"] == ["session", "--resume"]
    assert {"--resume", "-r", "--name", "-n", "--session-id"} == sessions.SESSION_OPTIONS["goose"]
    assert {"--text", "-t", "--recipe", "--with-extension", "--system"} <= sessions.VALUE_OPTIONS["goose"]
    restarted = sessions.restart_args({"agent": "goose", "options": [], "agent_args": ["run", "--interactive",
                                                                                       "--text=fix it"]})
    assert restarted == ["session", "--resume"], "the first task is not run again"


def test_a_presets_first_task_starts_goose_interactively():
    assert presets.prompt_args({"prompt": "-fix {what}"}, "goose", {"what": "it"}) == [
        "run", "--interactive", "--text=-fix it"]
    assert presets.prompt_args({}, "goose") == [] and "goose" in presets.AGENTS


def test_kits_and_memory_for_goose():
    assert kits.LAYOUT["goose"] == {"skills": ".agents/skills"}
    assert kits.MEMORY_FILE["goose"] == ("AGENTS.md", ".config/goose/AGENTS.md")
    skill = (kits.BUILTIN / "goose/skills/sandbox-ai/SKILL.md").read_text()
    assert "## Goose in this sandbox" in skill and all(f"`{name}`" in skill for name in goose.FEATURES)
    assert all(host in skill for host in goose.TELEMETRY_HOSTS) and "~/.config/goose/AGENTS.md" in skill
    assert (kits.TEMPLATES / "goose/AGENTS.md").is_file()
    assert "~/.config/goose/AGENTS.md" in (kits.TEMPLATES / "goose/skills/sandbox-example/SKILL.md").read_text()
    for kit in sorted(kits.BUILTIN_KITS.iterdir()):
        if (kit / "qwen").is_dir():
            assert (kit / "goose" / kits.SHARED_SKILLS_MARKER).is_file(), kit.name
            assert "goose" in json.loads((kit / "kit.json").read_text())["agents"], kit.name


def test_goose_as_a_helper_runs_one_shot():
    assert agent_mcp.command("goose", "-x", resume=True) == ["goose", "run", "--quiet", "--resume", "--text=-x"]
    assert agent_mcp.command("goose", "task", skip=True) == ["goose", "run", "--quiet", "--text=task"]
    assert agent_mcp.SKIP_ENV["goose"] == {"GOOSE_MODE": "auto"} and "goose" in agent_mcp.AGENTS


def test_the_review_is_shipped_with_the_release_it_names():
    review = (ROOT / "docs/GOOSE_REVIEW.md").read_text()
    assert goose.VERSION in review and goose.COMMIT in review
    assert all(digest in review for _, digest in goose.ASSETS.values())
    for name in goose.FEATURES:
        assert f"`{name}`" in review, name
    assert "## Go / No-Go" in review and "--goose-enable" in review and "## Hosts contacted by default (measured)" in review


def test_the_docs_say_telemetry_is_never_switchable():
    review = (ROOT / "docs/GOOSE_REVIEW.md").read_text()
    assert "### Telemetry: always off, never switchable" in review
    assert all(host in review for host in goose.TELEMETRY_HOSTS) and "us.i.posthog.com" in review
    assert all(name in review for name in goose.TELEMETRY_ENV)
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "Goose" in text and "--goose-enable" in text and all(host in text for host in goose.TELEMETRY_HOSTS), readme
