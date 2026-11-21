"""OpenCode 2 as a sandbox-ai agent of its own, sandbox-ai opencode2 (sandbox/opencode2.py, docs/OPENCODE2_REVIEW.md),
beside OpenCode 1 (sandbox-ai opencode): the features that turn on again what a sandbox turns off, its variables and
its configuration layer, its shared background service off, telemetry that is never switchable, the image (the
reviewed release's platform package checked against its sha512 integrity, no install script, no pipe into a shell),
the lock and its 14-day gate, its build, sessions, kits and helper mode. The start itself is in
tests/test_feature_matrix.py (OPENCODE2) and the launcher's contracts (test_agent_sandbox_contracts.py)."""
import argparse
import datetime
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


oc2 = load("opencode2_tests", "sandbox/opencode2.py")
lock_script = load("opencode2_lock_tests", "scripts/opencode2_lock.py")
LOCK = ROOT / "sandbox/Containerfile.opencode2.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.opencode2"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def ns(agent="opencode2", *enable, **more):
    return argparse.Namespace(**{"agent": agent, "opencode2_enable": list(enable), "allow_host": [],
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
    for name, spec in oc2.FEATURES.items():
        assert oc2.FEATURE_SYNTAX.fullmatch(name) and spec["help"] and spec["hosts"], name
        assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in spec["hosts"]), name
    # No feature for its telemetry or remote pairing: never switchable; no feature drops a telemetry variable.
    assert not {"telemetry", "crash-reports", "pairing", "remote", "share"} & set(oc2.FEATURES)
    assert not oc2.telemetry_hosts_in(oc2.hosts(list(oc2.FEATURES)))
    assert not {v for spec in oc2.FEATURES.values() for v in spec.get("drops", [])} & set(oc2.TELEMETRY_ENV)
    assert set(oc2.FEATURES) == {"models-catalog", "updates", "plugins", "web-search"}
    assert oc2.FEATURES["models-catalog"]["hosts"] == ["models.opencode.ai"], "only its host"


def test_parse_happy_sad_edge():
    assert oc2.parse(["plugins", "updates,web-search"]) == ["plugins", "updates", "web-search"]
    for wrong in (["docker"], ["telemetry"], ["plugins;rm"], ["../x"], ["pair"], ["Plugins:x"]):
        with pytest.raises(ValueError, match="not an OpenCode 2 feature"):
            oc2.parse(wrong)
    assert oc2.parse([" PLUGINS ", "plugins", ",,"]) == ["plugins"]
    assert oc2.parse(None) == [] and oc2.parse([]) == []


def test_network_options_add_only_the_features_hosts():
    a = ns("opencode2", "models-catalog", "plugins")
    a.allow_host = ["github.com"]
    oc2.network_options(a)
    assert a.allow_host == ["github.com", "models.opencode.ai", "registry.npmjs.org"]
    for other in (ns("opencode", "plugins"), ns("opencode2", "plugins", offline=True), ns("opencode2", "nope")):
        oc2.network_options(other)  # not OpenCode 2, offline, a wrong name: nothing (check() says why)
        assert other.allow_host == []


def test_check_refuses_what_opencode2_cannot_do_here():
    with pytest.raises(ValueError, match="is for OpenCode 2"):
        oc2.check(ns("opencode", "plugins"))
    oc2.check(ns("claude"), companions=("opencode2",))  # --with: a layer of the team image since 0.1.98
    oc2.check(ns(), hooks_active=True)  # rules for its own tools: sandbox-ai's V2 plugin
    with pytest.raises(ValueError, match="OpenCode 1's"):
        oc2.check(ns(local_config=Path("/x")))
    with pytest.raises(ValueError, match="OpenCode 1's"):
        oc2.check(ns(import_codex_auth=Path("/x")))
    with pytest.raises(ValueError, match="needs the network"):
        oc2.check(ns("opencode2", "plugins", offline=True, no_inspect=[]))
    oc2.check(ns(no_inspect=[]))  # a plain start
    oc2.check(ns("claude"))  # another agent, nothing of OpenCode 2's


@pytest.mark.parametrize("args,why", [
    (["upgrade"], "new review"), (["update", "2.3.0"], "new review"), (["uninstall", "-f"], "read-only"),
    (["plugin", "add", "opencode-acme"], "--opencode2-enable plugins"), (["plugin", "update"], "plugins"),
    (["plugin", "check"], "plugins"), (["pair", "--remote"], "internet"), (["pair", "--remote=true", "-y"], "internet"),
    (["serve", "--hostname", "0.0.0.0"], "loopback only"), (["serve", "--hostname=192.168.1.5"], "loopback only"),
    (["serve", "--hostname", "127.0.0.1", "--hostname", "::"], "loopback only"),
])
def test_an_argument_that_undoes_a_default_is_refused(args, why):
    refused = oc2.refused_arg(args, [])
    assert refused and re.search(why, refused[1]), (args, refused)


@pytest.mark.parametrize("args,names", [
    ([], []), (["run", "--", "upgrade the readme"], []), (["run", "please", "upgrade"], []),
    (["plugin", "list"], []), (["plugin", "remove", "x"], []), (["plugin", "add", "x"], ["plugins"]),
    (["serve", "--hostname", "127.0.0.1"], []), (["serve", "--hostname=localhost", "--port", "4096"], []),
    (["serve", "--hostname", "[::1]"], []), (["serve"], []), (["pair"], []), (["--continue"], []),
    (["mini", "--prompt", "upgrade"], []), (["--", "upgrade"], []),
])
def test_what_stays_allowed(args, names):
    assert oc2.refused_arg(args, names) is None, args


def test_address_host():
    assert oc2.address_host("127.0.0.1:4096") == "127.0.0.1" and oc2.address_host("LOCALHOST") == "localhost"
    assert oc2.address_host("[::1]:80") == "[::1]" and oc2.address_host("::1") == "::1"
    assert oc2.address_host("http://example.com:8080/x") == "example.com"


# --- variables, the configuration layer, the service ----------------------------------------------------------------

def test_the_variables_keep_opencode2_quiet():
    env = oc2.environment(ns())
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1" and env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert all(env[key] == value for key, value in oc2.TELEMETRY_ENV.items())
    assert "OPENCODE_CONFIG_CONTENT" not in env and "OPENCODE_DISABLE_PROJECT_CONFIG" not in env
    env = oc2.environment(ns("opencode2", "models-catalog", "updates"), {"update": "notify"})
    assert "OPENCODE_DISABLE_AUTOUPDATE" not in env and "OPENCODE_DISABLE_MODELS_FETCH" not in env
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"update": "notify"}
    helper = oc2.environment(ns(helper_of="claude"))
    assert helper["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1", "the lead shares /work: nothing of the project's"


def test_telemetry_is_pinned_off_whatever_is_enabled():
    every = ns("opencode2", *oc2.FEATURES)
    env = oc2.environment(every, oc2.config_content(every))
    assert all(env[key] == value for key, value in oc2.TELEMETRY_ENV.items())
    assert oc2.TELEMETRY_ENV["BUN_ENABLE_CRASH_REPORTING"] == "0" and oc2.TELEMETRY_ENV["OTEL_SDK_DISABLED"] == "true"


def test_telemetry_and_never_hosts_are_never_opened():
    for opened in (["bun.report"], ["*.bun.report"], ["opentunnel.xyz"], ["api.opentunnel.xyz"]):
        with pytest.raises(ValueError, match="telemetry is always off"):
            oc2.check_telemetry(ns(allow_host=opened, no_inspect=[]))
    with pytest.raises(ValueError, match="drop --allow-domain bun.report"):
        oc2.check_telemetry(ns(allow_host=["bun.report", "*.bun.report"], allow_domain=["bun.report"], no_inspect=[]))
    with pytest.raises(ValueError, match="proxy must see"):
        oc2.check_telemetry(ns(no_inspect=["bun.report"]))
    with pytest.raises(ValueError, match="--egress-all-allow"):
        oc2.check_telemetry(ns(no_inspect=[], egress_all_allow=True, no_proxy=True))
    oc2.check_telemetry(ns(allow_host=["opencode.ai", "registry.npmjs.org"], no_inspect=["opencode.ai"]))
    rules = oc2.telemetry_rules(ns())
    assert {(rule["host"], rule["path"]) for rule in rules} == {
        ("bun.report", "/**"), ("opentunnel.xyz", "/**"), ("opencode.ai", "/update/**")}
    assert all(rule["decision"] == "deny" for rule in rules)
    assert ("opencode.ai", "/update/**") not in {(r["host"], r["path"]) for r in oc2.telemetry_rules(ns("opencode2", "updates"))}
    assert oc2.rule_hosts() == ["bun.report", "opentunnel.xyz"]


def test_the_configuration_layer():
    assert oc2.config_content(ns()) == {"update": "disable", "autoupdate": False}
    assert oc2.config_content(ns("opencode2", "updates")) == {"update": "notify"}
    servers = oc2.mcp_servers(["demo", "demo", "helper-claude-x"], lambda name: ["/bridge", name], {"demo": 30})
    assert list(servers["servers"]) == ["demo", "helper-claude-x"]
    assert servers["servers"]["demo"] == {"type": "local", "command": ["python3", "/bridge", "demo"], "codemode": False,
                                          "timeout": {"startup": 30000, "catalog": 30000, "execution": 30000}}
    assert servers["servers"]["helper-claude-x"]["timeout"]["execution"] == 300000
    content = oc2.config_content(ns(dangerously_skip_permissions=True, model="anthropic/x"), servers)
    assert content["permissions"] == [{"action": "*", "resource": "*", "effect": "allow"}]
    assert content["model"] == "anthropic/x" and content["mcp"] == servers
    assert oc2.config_content(ns(model="anthropic/x"), None, "llm-gpu/m")["model"] == "llm-gpu/m", "--llm's first"
    content["permissions"].append("x")
    assert oc2.DANGER_PERMISSIONS == [{"action": "*", "resource": "*", "effect": "allow"}], "a copy each time"


@pytest.mark.parametrize("text,expected", [
    (None, {"disabled": True}), ("", {"disabled": True}), ("not json", {"disabled": True}), ("[1]", {"disabled": True}),
    ('{"password": "p"}', {"password": "p", "disabled": True}), ('{"disabled": false}', {"disabled": True}),
    ("[" * 100000 + "]" * 100000, {"disabled": True}), ('{"a": ' * 100000 + "1" + "}" * 100000, {"disabled": True}),
])
def test_the_shared_service_is_switched_off(text, expected):
    assert json.loads(oc2.service_settings(text)) == expected


def test_a_service_already_off_is_left_alone():
    assert oc2.service_settings('{"disabled": true, "password": "p"}') is None


def test_plan_says_what_is_on_and_off():
    plan = oc2.plan(ns("opencode2", "plugins"))
    assert plan["version"] == oc2.VERSION and plan["commit"] == oc2.COMMIT and plan["experimental"]
    assert plan["telemetry"]["switchable"] is False and plan["background_service"] is False
    assert plan["enabled"] == [{"feature": "plugins", "hosts": ["registry.npmjs.org"]}]
    assert plan["off"] == ["models-catalog", "updates", "web-search"] and plan["own_sandbox"] is False
    assert plan["never"] == ["opentunnel.xyz"] and plan["package"] == "@opencode/cli@" + oc2.VERSION


def test_no_login_is_taken_in():
    assert not oc2.login_document_ok({"anthropic": {"type": "api", "key": "x"}})


# --- the image, the lock and its 14-day gate, the build -------------------------------------------------------------

def test_the_containerfile_builds_exactly_the_reviewed_release():
    text = CONTAINERFILE.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert f"ARG OPENCODE2_VERSION={oc2.VERSION}" in text and "COPY Containerfile.opencode2.lock" in text
    assert "npm install" not in code and "npm ci" not in code and "postinstall" not in code, \
        "never the launcher package or its install script"
    assert "@opencode/cli@" not in code and "@opencode/cli/" not in code
    assert "opencode.ai/install" not in text and "opencode.ai/v2/install" not in text
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "https://registry.npmjs.org/${package}/-/" in text and "openssl dgst -sha512 -binary" in text
    assert text.index("openssl dgst") < text.index("tar -xzf"), "checked before it is unpacked"
    assert "install -m 0755 package/bin/opencode /usr/local/bin/opencode" in text
    assert "ln -s opencode /usr/local/bin/opencode2" in text and "ripgrep" in text
    assert '"opencode v${OPENCODE2_VERSION}"' in text and "grep -qw avx2 /proc/cpuinfo" in text
    assert "DO_NOT_TRACK=1" in text and "ENV OPENCODE_DISABLE_AUTOUPDATE" not in text, "updates stays switchable"


def test_the_lock_holds_the_reviewed_packages_with_their_integrity():
    entries = lock_script.parse_lock(LOCK.read_text())
    assert set(entries) == set(oc2.PLATFORMS) == {"amd64", "amd64-baseline", "arm64"}
    for arch, (package, version, integrity) in entries.items():
        assert (package, integrity) == oc2.PLATFORMS[arch] and version == oc2.VERSION
        assert package.startswith("@opencode/cli-linux-") and integrity.startswith("sha512-")
    assert LOCK.read_text() == lock_script.lock_text(oc2, oc2.VERSION, oc2.PLATFORMS)


def test_the_pin_rule_gates_a_younger_release():
    published = oc2.PUBLISHED
    assert oc2.eligible(published, datetime.datetime(2026, 11, 15, tzinfo=datetime.timezone.utc))
    assert not oc2.eligible(published, datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc))
    assert not oc2.eligible("2026-11-06T23:38:54Z", datetime.datetime(2026, 11, 15, tzinfo=datetime.timezone.utc))
    assert oc2.eligible("2026-11-06T23:38:54Z", datetime.datetime(2026, 11, 21, tzinfo=datetime.timezone.utc))
    assert oc2.NEXT["version"] != oc2.VERSION


def test_the_lock_script_refuses_a_release_younger_than_14_days(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(lock_script, "registry_entry", lambda registry, package, version: (
        "2026-11-06T23:38:54.285Z", "sha512-" + "A" * 86 + "=="))
    out = tmp_path / "lock"
    assert lock_script.main(["--version", "2.2.6", "--out", str(out), "--today", "2026-11-15"]) == 1
    assert "eligible from 2026-11-20" in capsys.readouterr().err and not out.exists()
    assert lock_script.main(["--version", "2.2.6", "--out", str(out), "--today", "2026-11-21"]) == 0
    assert set(lock_script.parse_lock(out.read_text())) == set(oc2.PLATFORMS)


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


def test_build_opencode2_takes_the_reviewed_release_only(engine):
    result = build("opencode2")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"OPENCODE2_VERSION={oc2.VERSION}" in calls[0]
    assert f"sandbox-ai.agent-version={oc2.VERSION}" in calls[0]
    assert any(arg.endswith("Containerfile.opencode2") for arg in calls[0])
    assert "localhost/opencode2-sandbox:latest" in calls[0]
    assert build("--pin", oc2.VERSION, "opencode2").returncode == 0
    refused = build("--pin", "2.3.0", "opencode2")
    assert refused.returncode == 2 and "OpenCode 2 is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("opencode2", "--build-arg", "OPENCODE2_VERSION=2.3.0").returncode == 2
    assert "opencode2" in build("--help").stdout


def test_build_all_builds_opencode2_too_and_opencode_stays_opencode_1():
    text = (ROOT / "sandbox/build_image.sh").read_text()
    line = next(line for line in text.splitlines() if line.strip().startswith("all) agents=("))
    assert "opencode opencode2" in line
    assert 'opencode) echo "opencode-ai" ;;' in text, "sandbox-ai build opencode stays OpenCode 1's package"
    assert "opencode-ai" in (ROOT / "sandbox/Containerfile.opencode").read_text()


# --- the review ---------------------------------------------------------------------------------------------------------

def test_the_review_is_shipped_with_the_release_it_names():
    review = (ROOT / "docs/OPENCODE2_REVIEW.md").read_text()
    assert oc2.VERSION in review and oc2.COMMIT in review and oc2.PACKAGE in review
    for _, integrity in oc2.PLATFORMS.values():
        assert integrity[:15] in review
    for name in oc2.FEATURES:
        assert f"`{name}`" in review, name
    assert "## Go / No-Go" in review and "--opencode2-enable" in review and "## Hosts contacted by default (measured)" in review
    assert "### Telemetry: always off, never switchable" in review and "### Decision: its own agent" in review
    assert all(name in review for name in oc2.TELEMETRY_ENV) and all(name in review for name in oc2.BASE_ENV)
    assert "bun.report" in review and "opentunnel.xyz" in review and oc2.NEXT["eligible"] in review


# --- the agent's other places: the launcher, --llm, sessions, presets, kits, its helper mode -------------------------

s = load("launcher_opencode2_tests", "sandbox/agent_sandbox.py")
llm = load("llm_opencode2_tests", "sandbox/llm.py")
sessions = load("sessions_opencode2_tests", "sandbox/sessions.py")
presets = load("presets_opencode2_tests", "sandbox/presets.py")
kits = load("kits_opencode2_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_opencode2_tests", "sandbox/agent_mcp.py")


def test_the_option_counts_as_weakening_and_the_launcher_knows_opencode2():
    assert "--opencode2-enable" in s.MODES.WEAKENING_OPTIONS and s.SHORT_OPTIONS["--opencode2-enable"] == "-oe"
    assert s.MODES.weakening_options(s, ["-oe", "plugins", "--no-tmux"]) == [("--opencode2-enable", "plugins")]
    assert len(set(s.SHORT_OPTIONS.values())) == len(s.SHORT_OPTIONS), "every short option once"
    assert s.PROVIDERS["opencode2"] == ["opencode.ai"] and s.DANGER_ARGS["opencode2"] == []
    assert s.DANGER_ENV["opencode2"] == {}, "no OPENCODE_PERMISSION in OpenCode 2"
    assert s.UNSWITCHABLE_TELEMETRY["opencode2"] == "OpenCode 2" and "opencode2" in s.COMPANION_AGENTS
    assert s.pinned_telemetry_environment("opencode2") == oc2.TELEMETRY_ENV
    assert s.EXPERIMENTAL_PARTS["opencode2"] == {"agent": "opencode2"} and s.experimental_needs(["opencode2"]) == ["opencode2"]
    assert "opencode" not in s.EXPERIMENTAL_PARTS, "OpenCode 1 is not experimental"
    assert "opencode2" in s.AGENT_PROGRAMS


def test_opencode2_has_no_login_to_keep(tmp_path):
    assert not s.login_document_ok("opencode2", {"anthropic": {"type": "api"}})
    assert s.login_document_ok("opencode", {"anthropic": {"type": "api"}}), "OpenCode 1's login stays"
    assert s.merge_refreshed("opencode2", {"a": {"type": "oauth"}}, {"a": {"type": "oauth"}}) is None
    assert "opencode2" in s.NO_LOGIN_AGENTS and "opencode2" in s.LOGIN_COMMANDLESS
    assert "opencode" not in s.NO_LOGIN_AGENTS
    login = load("login_opencode2_tests", "sandbox/login.py")
    assert "opencode2" not in login.AGENTS and "opencode" in login.AGENTS


def test_llm_writes_opencode_1_s_file_for_opencode2(monkeypatch):
    monkeypatch.setattr(llm, "agent_url", lambda endpoint, api="openai": "http://10.0.0.5:11434/v1")
    assert "opencode2" in llm.AGENTS and set(llm.AGENTS) == set(s.AUTH_TARGETS)
    endpoint = {"name": "gpu", "kind": "ollama", "url": "http://10.0.0.5:11434", "models": [], "model": "m",
                "parsed": {"host": "10.0.0.5"}}
    loop = {"steps": 40, "compaction": True, "instructions": False}
    v1 = llm.opencode_config(endpoint, "m", contexts={"m": 32768}, loop=loop)
    v2 = llm.opencode_config(endpoint, "m", contexts={"m": 32768}, loop=loop, prune=False)
    assert v1["compaction"]["prune"] is True and "prune" not in v2["compaction"]
    assert v2["compaction"]["auto"] is True and v2["agent"]["build"]["steps"] == 40


def test_sessions_continue_and_options():
    assert sessions.CONTINUE_ARGS["opencode2"] == ["--continue"]
    assert {"--continue", "--session", "--fork"} <= sessions.SESSION_OPTIONS["opencode2"]
    assert {"--model", "--agent", "--session", "--prompt", "--hostname"} <= sessions.VALUE_OPTIONS["opencode2"]
    restarted = sessions.restart_args({"agent": "opencode2", "options": [], "agent_args": ["run", "--", "fix it"]})
    assert "fix it" not in restarted, "the first task is not run again"


def test_a_presets_first_task_is_its_prompt():
    assert presets.prompt_args({"prompt": "-fix {what}"}, "opencode2", {"what": "it"}) == ["--prompt", "-fix it"]
    assert presets.prompt_args({}, "opencode2") == [] and "opencode2" in presets.AGENTS
    for name in ("default-opencode2", "coding-opencode2", "opencode2-local-llm"):
        preset = json.loads((ROOT / f"sandbox/presets/{name}.json").read_text())
        assert preset["agents"] == ["opencode2"] and preset["category"] == "agents" and preset["translations"]["de"]
        assert any("not live-tested yet" in note for note in preset["notes"])
    basic = json.loads((ROOT / "sandbox/presets/default-opencode2.json").read_text())
    assert basic["translations"]["de"]["title"] == "OpenCode 2 (Standard)", "every agent's basic preset"
    assert set(basic["options"]) <= {"--api-key", "--model"} and "values" not in basic and "mcp" not in basic


def test_kits_and_memory_for_opencode2():
    assert kits.LAYOUT["opencode2"] == kits.LAYOUT["opencode"], "the same folders of its global configuration"
    assert kits.MEMORY_FILE["opencode2"] == ("AGENTS.md", ".config/opencode/AGENTS.md")
    skill = (kits.BUILTIN / "opencode2/skills/sandbox-ai/SKILL.md").read_text()
    assert "## OpenCode 2 in this sandbox" in skill and all(f"`{name}`" in skill for name in oc2.FEATURES)
    assert all(host in skill for host in oc2.TELEMETRY_HOSTS) and "~/.config/opencode/AGENTS.md" in skill
    assert "letta" not in without_companion_commands(skill) and "goose" not in without_companion_commands(skill)
    assert (kits.TEMPLATES / "opencode2/AGENTS.md").is_file()
    assert "home/opencode2/" in (kits.TEMPLATES / "opencode2/skills/sandbox-example/SKILL.md").read_text()
    for kit in sorted(kits.BUILTIN_KITS.iterdir()):
        if (kit / "goose").is_dir():
            assert (kit / "opencode2" / kits.SHARED_SKILLS_MARKER).is_file(), kit.name
            assert "opencode2" in json.loads((kit / "kit.json").read_text())["agents"], kit.name


def test_opencode2_as_a_helper_runs_one_task():
    assert agent_mcp.command("opencode2", "-x") == ["opencode2", "run", "--", "-x"]
    assert agent_mcp.command("opencode2", "go on", resume=True, skip=True) == \
        ["opencode2", "run", "--continue", "--auto", "--", "go on"]
    assert agent_mcp.command("opencode", "-x") == ["opencode", "run", "--", "-x"], "OpenCode 1's stays"
    assert "opencode2" in agent_mcp.AGENTS
