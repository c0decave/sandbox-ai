"""Pi as a sandbox-ai agent (sandbox/pi.py, docs/PI_REVIEW.md): the features that turn on again what a sandbox turns
off, its variables and sandbox-ai's extension, telemetry that is never switchable, the image (the reviewed release's
npm install hash-locked, no install script, no pipe into a shell), its build, login, sessions, kits and helper mode.
The start itself is in tests/test_feature_matrix.py (PI) and the launcher's contracts
(test_agent_sandbox_contracts.py)."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pi = load("pi_tests", "sandbox/pi.py")
lock_script = load("agent_npm_lock_pi_tests", "scripts/agent_npm_lock.py")
LOCK = ROOT / "sandbox/Containerfile.pi.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.pi"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    (tmp_path / "home").mkdir()


def ns(agent="pi", *enable, **more):
    return argparse.Namespace(**{"agent": agent, "pi_enable": list(enable), "allow_host": [], "secret_env": [],
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
    for name, spec in pi.FEATURES.items():
        assert pi.FEATURE_SYNTAX.fullmatch(name) and spec["help"] and spec["hosts"], name
        assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in spec["hosts"]), name
    # No feature for its telemetry: never switchable; no feature drops a telemetry variable.
    assert not {"telemetry", "analytics", "bug", "bug-reports", "radius"} & set(pi.FEATURES)
    assert not {v for spec in pi.FEATURES.values() for v in spec.get("drops", [])} & set(pi.TELEMETRY_ENV)
    assert set(pi.FEATURES) == {"model-catalog", "updates", "packages", "share"}
    assert not {host for host, _ in pi.TELEMETRY_PATHS if host != "pi.dev"} & set(pi.hosts(list(pi.FEATURES))), \
        "radius.pi.dev (bug reports) is opened by no feature"


def test_parse_happy_sad_edge():
    assert pi.parse(["packages", "share,updates"]) == ["packages", "share", "updates"]
    for wrong in (["docker"], ["telemetry"], ["share;rm"], ["../x"], ["radius"], ["Share:x"]):
        with pytest.raises(ValueError, match="not a Pi feature"):
            pi.parse(wrong)
    assert pi.parse([" SHARE ", "share", ",,"]) == ["share"]
    assert pi.parse(None) == [] and pi.parse([]) == []


def test_network_options_add_only_the_features_hosts():
    a = ns("pi", "model-catalog", "updates", "share")
    a.allow_host = ["github.com"]
    pi.network_options(a)
    assert a.allow_host == ["github.com", "pi.dev", "api.github.com"]
    for other in (ns("claude", "share"), ns("pi", "share", offline=True), ns("pi", "nope")):
        pi.network_options(other)  # not Pi, offline, a wrong name: nothing (check() says why)
        assert other.allow_host == []


def test_check_refuses_what_pi_cannot_do_here():
    pi.check(ns("pi", "packages"))  # fine
    pi.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="is for Pi"):
        pi.check(ns("claude", "share"))
    pi.check(ns("pi"), companions=["claude"])  # --with: a layer of the team image since 0.1.98
    pi.check(ns("pi"), hooks_active=True)  # rules for its own tools: sandbox-ai's extension holds them
    with pytest.raises(ValueError, match="needs the network"):
        pi.check(ns("pi", "updates", offline=True))
    with pytest.raises(ValueError, match="pi install needs --pi-enable packages"):
        pi.check(ns("pi", agent_args=["install", "npm:@x/y"]))


def test_an_argument_of_a_feature_that_is_off_is_refused():
    for args, feature in ((["install", "npm:@example/pi-tools@1.0.0"], "packages"),
                          (["update", "--extensions"], "packages"), (["update", "npm:@x/y"], "packages"),
                          (["update", "--extension", "git:github.com/x/y"], "packages"),
                          (["update", "--models"], "model-catalog"),
                          (["-e", "npm:@example/pi-tools"], "packages"), (["--extension=git:github.com/x/y"], "packages"),
                          (["-e", "https://github.com/x/y"], "packages"), (["-p", "-e", "github.com/x/y", "hi"], "packages")):
        refused = pi.refused_arg(args)
        assert refused and feature in refused[1], args
        assert pi.refused_arg(args, [feature]) is None, args
    # Always refused: Pi updating itself (a read-only install; its own advice pipes an installer into a shell).
    for args in (["update"], ["update", "--self"], ["update", "self"], ["update", "pi"], ["update", "--all"],
                 ["update", "--force"], ["update", "--extensions", "--all"]):
        refused = pi.refused_arg(args, list(pi.FEATURES))
        assert refused and "read-only" in refused[1], args
    for args in ([], ["--continue"], ["-p", "--", "update the docs"], ["--", "install"], ["remove", "npm:@x/y"],
                 ["list"], ["config"], ["mcp", "list"], ["auth", "check", "--provider", "openai"],
                 ["-e", "./my-extension.ts"], ["-e", "builtin:mcp"], ["-p", "install", "the", "tools"],
                 ["--model", "anthropic/claude-sonnet-4-5", "--thinking", "high"]):
        assert pi.refused_arg(args) is None, args


# --- the variables and the extension --------------------------------------------------------------------------------

def test_the_variables_keep_pi_quiet():
    env = pi.environment(ns())
    assert env == {"PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}
    assert pi.environment(ns("pi", "model-catalog")) == {"PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}
    assert pi.environment(ns("pi", "updates")) == {"PI_TELEMETRY": "0"}
    assert pi.environment(ns("pi", "packages", "share")) == env, "these open hosts, no variable changes"


def test_telemetry_is_pinned_off_whatever_is_enabled():
    """The user's rule: never switchable. Every feature: the variables hold."""
    env = pi.environment(ns("pi", *pi.FEATURES))
    assert env["PI_TELEMETRY"] == "0"
    for key, value in pi.TELEMETRY_ENV.items():
        assert env[key] == value


def test_telemetry_paths_are_refused_and_never_hidden():
    for opened in ({"no_inspect": ["pi.dev"]}, {"no_inspect": ["*.pi.dev"]}, {"no_inspect": ["radius.pi.dev"]},
                   {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            pi.check_telemetry(argparse.Namespace(**opened))
    pi.check_telemetry(argparse.Namespace(no_inspect=["example.org"], allow_host=["pi.dev"]))
    rules = pi.telemetry_rules(ns("pi", *pi.FEATURES))
    assert all(rule["decision"] == "deny" for rule in rules)
    assert {(rule["host"], rule["path"]) for rule in rules} == {
        ("pi.dev", "/api/report-install/**"), ("radius.pi.dev", "/v1/bug-reports/**")}, "also with pi.dev opened"
    shared = [rule for rule in pi.telemetry_rules(ns()) if rule["host"] == "api.github.com"]
    assert shared == [{"id": "pi-share", "host": "api.github.com", "methods": ["POST"], "path": "/gists/**",
                       "decision": "deny", "reason": "Pi's /share (--pi-enable share opens it)"}]
    assert len({rule["id"] for rule in pi.telemetry_rules()}) == len(pi.telemetry_rules())


def test_mcp_servers_run_the_bridge_declared_to_the_model():
    servers = pi.mcp_servers(["demo", "helper-claude-x", "demo"], lambda name: ["/bridge", name], {"demo": 90})
    assert servers == [
        {"name": "demo", "config": {"command": "python3", "args": ["/bridge", "demo"], "exposure": "direct",
                                    "timeout": 90}},
        {"name": "helper-claude-x", "config": {"command": "python3", "args": ["/bridge", "helper-claude-x"],
                                               "exposure": "direct"}}]
    assert pi.mcp_servers([], lambda name: []) == []


def test_files_and_extension_config():
    llm = {"provider": {"id": "sandbox-ai", "config": {"baseUrl": "http://x/v1"}}, "instructions": "no loops",
           "steps": 40}
    config = pi.extension_config(ns(), [{"name": "x"}], llm)
    assert config == {"servers": [{"name": "x"}], "provider": llm["provider"], "instructions": "no loops", "steps": 40}
    assert pi.extension_config(ns()) == {"servers": []}
    assert pi.extension_config(ns(), None, {"provider": {"id": "sandbox-ai"}, "instructions": None, "steps": None}) \
        == {"servers": [], "provider": {"id": "sandbox-ai"}}
    files = pi.files(ns(), config)
    assert set(files) == {"pi/sandbox-ai.js", "pi/sandbox-ai.json"}
    assert json.loads(files["pi/sandbox-ai.json"]) == config and files["pi/sandbox-ai.js"] == pi.EXTENSION_SCRIPT
    assert pi.EXTENSION.startswith(pi.MANAGED_DIR + "/") and pi.EXTENSION_CONFIG in pi.EXTENSION_SCRIPT
    for call in ("pi.registerProvider(", "pi.registerMcpServer(", '"before_agent_start"', '"turn_end"',
                 "ctx.abort()"):
        assert call in pi.EXTENSION_SCRIPT, call
    assert pi.model_effort_args(ns(model="m", effort="high")) == ["--model", "m", "--thinking", "high"]
    assert pi.model_effort_args(ns(model="m", llm="x")) == [] and pi.model_effort_args(ns()) == []


@pytest.mark.skipif(not shutil.which("node"), reason="needs Node.js")
def test_the_extension_registers_what_its_settings_say(tmp_path):
    """The extension's script under Node with a stand-in for Pi's API: the provider, the servers, the guard's section
    and the turn limit; a missing or damaged settings file registers nothing and does not throw."""
    settings = tmp_path / "sandbox-ai.json"
    script = tmp_path / "sandbox-ai.mjs"
    script.write_text(pi.EXTENSION_SCRIPT.replace(pi.EXTENSION_CONFIG, str(settings)))
    driver = tmp_path / "driver.mjs"
    driver.write_text("""
import ext from %s;
const calls = []; const handlers = {};
const api = {registerProvider: (id, c) => calls.push(["provider", id, c.baseUrl]),
             registerMcpServer: (n, c) => { if (n === "bad") throw new Error("invalid"); calls.push(["mcp", n, c.exposure]); },
             on: (e, h) => { handlers[e] = h; }};
ext(api);
const event = {systemPromptOptions: {sections: {a: "b"}}};
if (handlers.before_agent_start) handlers.before_agent_start(event);
let aborted = 0;
const ctx = {hasUI: false, ui: {}, abort: () => { aborted += 1; }};
if (handlers.agent_start) { handlers.agent_start(); for (let i = 0; i < 3; i++) handlers.turn_end({}, ctx); }
console.log(JSON.stringify({calls, sections: event.systemPromptOptions.sections, aborted, events: Object.keys(handlers).sort()}));
""" % json.dumps(str(script)))

    def run(content):
        if content is None:
            settings.unlink(missing_ok=True)
        else:
            settings.write_text(content)
        result = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout), result.stderr

    out, err = run(json.dumps({
        "provider": {"id": "sandbox-ai", "config": {"baseUrl": "http://127.0.0.1:1/v1"}},
        "servers": [{"name": "demo", "config": {"command": "python3", "exposure": "direct"}},
                    {"name": "bad", "config": {}}, {"name": 3}, "x"],
        "instructions": "no loops", "steps": 2}))
    assert out["calls"] == [["provider", "sandbox-ai", "http://127.0.0.1:1/v1"], ["mcp", "demo", "direct"]]
    assert "the MCP server bad was not registered" in err
    assert out["sections"] == {"a": "b", "sandbox_ai_loop_guard": "no loops"} and out["aborted"] == 2
    assert "loop guard's limit of 2 turns" in err
    for broken in (None, "{", "[]", json.dumps({"servers": {}, "steps": "9", "instructions": 5})):
        out, _ = run(broken)
        assert out["calls"] == [] and out["aborted"] == 0 and out["events"] == [], broken


def test_plan_says_what_is_on_and_off():
    plan = pi.plan(ns("pi", "updates"))
    assert plan["version"] == pi.VERSION and plan["commit"] == pi.COMMIT and plan["experimental"]
    assert plan["telemetry"]["switchable"] is False and plan["approvals"] is False
    assert plan["enabled"] == [{"feature": "updates", "hosts": ["pi.dev"]}]
    assert plan["off"] == ["model-catalog", "packages", "share"] and plan["own_sandbox"] is False
    assert plan["telemetry"]["blocked_paths"] == ["pi.dev/api/report-install/**", "radius.pi.dev/v1/bug-reports/**"]


def test_login_document_is_pi_s_auth_json():
    assert pi.login_document_ok({"anthropic": {"type": "oauth", "access": "a", "refresh": "r", "expires": 1},
                                 "openai": {"type": "api_key", "key": "k"}})
    for wrong in ({}, [], {"x": "y"}, {"x": {"type": "token"}}, {"x": {}}, None):
        assert not pi.login_document_ok(wrong), wrong


# --- the image: the reviewed release, the lock, its build -------------------------------------------------------------

def test_the_containerfile_builds_exactly_the_reviewed_release():
    text = CONTAINERFILE.read_text()
    assert f"ARG PI_VERSION={pi.VERSION}" in text and "COPY Containerfile.pi.lock" in text
    assert "npm ci --ignore-scripts" in text and "npm install" not in text
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "mkdir -p -m 755 /etc/pi" in text and "> /usr/local/bin/pi" in text
    assert "fd-find" in text and "ripgrep" in text, "else Pi downloads them from GitHub"
    assert "ENV PI_TELEMETRY=0" in text and "ENV PI_OFFLINE" not in text, "the network switches stay per start"
    assert f"-e {pi.EXTENSION}" in text and f"[ -r {pi.EXTENSION} ]" in text


@pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
def test_the_image_s_pi_command_loads_the_extension_except_for_subcommands(tmp_path):
    """The image's pi command as the Containerfile prints it, with a stand-in for node: sandbox-ai's extension before
    the agent's arguments when it is there; Pi's subcommands as given."""
    text = CONTAINERFILE.read_text()
    printf = re.search(r"RUN (printf '%s\\n' '#!/bin/sh'.*?) > /usr/local/bin/pi", text, re.S).group(1)
    made = subprocess.run(["sh", "-c", printf.replace("\\\n", " ")], capture_output=True, text=True, check=True)
    extension = tmp_path / "sandbox-ai.js"
    wrapper = tmp_path / "pi"
    wrapper.write_text(made.stdout.replace(pi.EXTENSION, str(extension)))
    (tmp_path / "bin").mkdir()
    fake = tmp_path / "bin/node"
    fake.write_text("#!/bin/sh\nfor arg in \"$@\"; do printf '%s\\n' \"$arg\"; done\n")
    fake.chmod(0o755)

    def run(*args):
        result = subprocess.run(["sh", str(wrapper), *args], capture_output=True, text=True,
                                env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"}, check=True)
        return result.stdout.splitlines()[1:]  # after the bundle's path

    assert run("-p", "hi") == ["-p", "hi"], "no extension mounted: Pi as it is"
    extension.write_text("// x")
    assert run("-p", "hi there") == ["-e", str(extension), "-p", "hi there"]
    assert run() == ["-e", str(extension)]
    for sub in ("install", "remove", "uninstall", "update", "list", "config", "auth", "mcp"):
        assert run(sub, "x") == [sub, "x"], sub


def test_the_lock_holds_the_reviewed_package_with_every_integrity():
    lock = json.loads(LOCK.read_text())
    assert lock_script.check(lock, "pi", pi.PACKAGE, pi.VERSION) > 100
    entry = lock["packages"][f"node_modules/{pi.PACKAGE}"]
    assert entry["integrity"] == pi.INTEGRITY and entry["resolved"].startswith("https://registry.npmjs.org/")
    assert all(entry.get("resolved", "").startswith("https://registry.npmjs.org/")
               for path, entry in lock["packages"].items() if path), "only npm's registry"
    assert lock_script.package_json("pi", pi.PACKAGE, pi.VERSION) == lock["packages"][""] | {"private": True}
    assert "pi" in lock_script.AGENTS
    # Pi's own packages at the release; the AWS SDK's core past the versions npm marks for leaking secrets in logs.
    for name in ("pi-ai", "pi-agent-core", "pi-tui", "pi-mcp", "pi-codemode"):
        assert lock["packages"][f"node_modules/@earendil-works/{name}"]["version"] == pi.VERSION, name
    assert not any("redaction" in str(entry.get("deprecated", "")) for entry in lock["packages"].values())


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


def test_build_pi_takes_the_reviewed_release_only(engine):
    result = build("pi")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"PI_VERSION={pi.VERSION}" in calls[0]
    assert f"sandbox-ai.agent-version={pi.VERSION}" in calls[0]
    assert any(arg.endswith("Containerfile.pi") for arg in calls[0])
    assert build("--pin", pi.VERSION, "pi").returncode == 0
    refused = build("--pin", "1.5.2", "pi")
    assert refused.returncode == 2 and "Pi is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("pi", "--build-arg", "PI_VERSION=1.5.2").returncode == 2
    assert "pi builds localhost/pi-sandbox" in build("--help").stdout


def test_build_all_builds_pi_too():
    text = (ROOT / "sandbox/build_image.sh").read_text()
    assert re.search(r"^    all\) agents=\([a-z0-9 ]* pi[a-z0-9 ]*\); shift; break ;;$", text, re.M)


# --- the review ---------------------------------------------------------------------------------------------------------

def test_the_review_is_shipped_with_the_release_it_names():
    review = (ROOT / "docs/PI_REVIEW.md").read_text()
    assert pi.VERSION in review and pi.COMMIT in review and pi.INTEGRITY in review
    for name in pi.FEATURES:
        assert f"`{name}`" in review, name
    assert "## Go / No-Go" in review and "--pi-enable" in review and "## Hosts contacted by default (measured)" in review
    assert "### Telemetry: always off, never switchable" in review
    assert all(name in review for name in pi.TELEMETRY_ENV) and all(name in review for name in pi.BASE_ENV)
    assert "/api/report-install" in review and "/v1/bug-reports" in review and "badlogic/pi-mono" in review


# --- the agent's other places: the launcher, --llm, login, sessions, presets, kits, its helper mode -----------------

s = load("launcher_pi_tests", "sandbox/agent_sandbox.py")
llm = load("llm_pi_tests", "sandbox/llm.py")
sessions = load("sessions_pi_tests", "sandbox/sessions.py")
presets = load("presets_pi_tests", "sandbox/presets.py")
kits = load("kits_pi_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_pi_tests", "sandbox/agent_mcp.py")
login = load("login_pi_tests", "sandbox/login.py")


def test_the_option_counts_as_weakening_and_the_launcher_knows_pi():
    assert "--pi-enable" in s.MODES.WEAKENING_OPTIONS and s.SHORT_OPTIONS["--pi-enable"] == "-px"
    assert s.MODES.weakening_options(s, ["-px", "share", "--no-tmux"]) == [("--pi-enable", "share")]
    assert len(set(s.SHORT_OPTIONS.values())) == len(s.SHORT_OPTIONS), "every short option once"
    assert s.PROVIDERS["pi"] == [] and s.DANGER_ARGS["pi"] == pi.DANGER_ARGS == []
    assert s.UNSWITCHABLE_TELEMETRY["pi"] == "Pi" and "pi" in s.COMPANION_AGENTS and "pi" in s.AGENT_PROGRAMS
    assert s.pinned_telemetry_environment("pi") == pi.TELEMETRY_ENV
    assert s.EXPERIMENTAL_PARTS["pi"] == {"agent": "pi"} and s.experimental_needs(["pi"]) == ["pi"]
    assert s.AGENT_EFFORTS["pi"] == pi.EFFORTS


def test_pi_s_login_is_kept_like_opencode_s(tmp_path):
    assert s.AUTH_TARGETS["pi"] == ".pi/agent/auth.json"
    assert s.auth_sources("pi") == [(tmp_path / "home/.pi/agent/auth.json", ".pi/agent/auth.json")]
    assert "pi" not in s.NO_LOGIN_AGENTS and "pi" not in s.LOGIN_COMMANDLESS
    assert "pi" in login.AGENTS and login.LOGIN_ARGS["pi"] == []
    assert login.LOGIN_OPTIONS["pi"] == ["--profile", "frontier"]
    stored = {"anthropic": {"type": "oauth", "access": "a", "refresh": "r", "expires": 1},
              "openai": {"type": "api_key", "key": "k"}}
    refreshed = {"anthropic": {"type": "oauth", "access": "a2", "refresh": "r2", "expires": 2},
                 "openai": {"type": "api_key", "key": "changed"}, "google": {"type": "api_key", "key": "new"}}
    merged = s.merge_refreshed("pi", stored, refreshed)
    assert merged["anthropic"] == {"type": "oauth", "access": "a2", "refresh": "r2", "expires": 2}
    assert merged["openai"] == {"type": "api_key", "key": "k"} and "google" not in merged, \
        "only refreshed tokens of the same entries go back to the store"
    assert s.login_document_ok("pi", stored) and not s.login_document_ok("pi", {"x": "y"})


def test_llm_keeps_pi_s_values_in_step():
    assert llm.PI_PROVIDER == pi.LLM_PROVIDER and llm.PI_SETTINGS == pi.EXTENSION_CONFIG
    assert "pi" in llm.AGENTS and set(llm.AGENTS) == set(s.AUTH_TARGETS)


def test_sessions_continue_and_options():
    assert sessions.CONTINUE_ARGS["pi"] == ["--continue"]
    assert {"--continue", "--resume", "--session", "--session-id", "--fork", "--no-session"} <= \
        sessions.SESSION_OPTIONS["pi"]
    assert {"--model", "--thinking", "--provider", "--extension", "-e"} <= sessions.VALUE_OPTIONS["pi"]
    restarted = sessions.restart_args({"agent": "pi", "options": [], "agent_args": ["-p", "--", "fix it"]})
    assert restarted == ["--continue"], "the first task is not run again"
    restarted = sessions.restart_args({"agent": "pi", "options": [],
                                       "agent_args": ["--thinking", "high", "--", "the first task"]})
    assert restarted == ["--thinking", "high", "--continue"]


def test_a_presets_first_task_is_its_first_message():
    assert presets.prompt_args({"prompt": "-fix {what}"}, "pi", {"what": "it"}) == ["--", "-fix it"]
    assert presets.prompt_args({"prompt": "@README.md"}, "pi") == ["--", " @README.md"], "text, not a file to read"
    assert presets.prompt_args({}, "pi") == [] and "pi" in presets.AGENTS
    for name in ("default-pi", "coding-pi", "pi-local-llm"):
        preset = json.loads((ROOT / f"sandbox/presets/{name}.json").read_text())
        assert preset["agents"] == ["pi"] and preset["category"] == "agents" and preset["translations"]["de"]
        assert any("not live-tested yet" in note for note in preset["notes"])


def test_kits_and_memory_for_pi():
    assert kits.LAYOUT["pi"] == {"skills": ".agents/skills"}
    assert kits.MEMORY_FILE["pi"] == ("AGENTS.md", pi.MEMORY_NOTES)
    skill = (kits.BUILTIN / "pi/skills/sandbox-ai/SKILL.md").read_text()
    assert "## Pi in this sandbox" in skill and all(f"`{name}`" in skill for name in pi.FEATURES)
    assert "~/.pi/agent/AGENTS.md" in skill and "PI_TELEMETRY=0" in skill
    assert "goose" not in without_companion_commands(skill)
    assert (kits.TEMPLATES / "pi/AGENTS.md").is_file()
    assert "~/.pi/agent/AGENTS.md" in (kits.TEMPLATES / "pi/skills/sandbox-example/SKILL.md").read_text()
    for kit in sorted(kits.BUILTIN_KITS.iterdir()):
        if (kit / "goose").is_dir():
            assert (kit / "pi" / kits.SHARED_SKILLS_MARKER).is_file(), kit.name
            assert "pi" in json.loads((kit / "kit.json").read_text())["agents"], kit.name


def test_pi_as_a_helper_runs_one_task_in_print_mode():
    assert agent_mcp.command("pi", "-x") == ["pi", "-p", "--", "-x"]
    assert agent_mcp.command("pi", "go on", resume=True, skip=True) == ["pi", "-p", "--continue", "--", "go on"]
    assert agent_mcp.command("pi", "@file") == ["pi", "-p", "--", " @file"], "a task, never a file to read"
    assert "pi" in agent_mcp.AGENTS and agent_mcp.SKIP["pi"] == []
