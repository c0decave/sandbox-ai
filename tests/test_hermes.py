"""Hermes Agent as sandbox-ai's fourth agent (sandbox/hermes.py, docs/HERMES_REVIEW.md): the features that turn on
again what a sandbox turns off, Hermes' managed configuration, the image (the reviewed tag, the hash-pinned lock, no
pipe into a shell), its build, its login, its sessions and presets. The start itself is in tests/test_feature_matrix.py
(HERMES) and the launcher's contracts (test_agent_sandbox_contracts.py)."""
import argparse
import importlib.util
import json
import os
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


hermes = load("hermes_tests", "sandbox/hermes.py")
s = load("launcher_hermes_tests", "sandbox/agent_sandbox.py")
llm = load("llm_hermes_tests", "sandbox/llm.py")
sessions = load("sessions_hermes_tests", "sandbox/sessions.py")
presets = load("presets_hermes_tests", "sandbox/presets.py")
kits = load("kits_hermes_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_hermes_tests", "sandbox/agent_mcp.py")
LOCK = ROOT / "sandbox/Containerfile.hermes.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.hermes"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    (tmp_path / "home").mkdir()


def ns(agent="hermes", *enable, **more):
    return argparse.Namespace(**{"agent": agent, "hermes_enable": list(enable), "allow_host": [], "secret_env": [],
                                 "offline": False, **more})


# --- the features ---------------------------------------------------------------------------------------------------

def test_every_feature_names_its_hosts_and_says_what_it_does():
    for name, spec in hermes.FEATURES.items():
        assert hermes.FEATURE_SYNTAX.fullmatch(name) and spec["help"] and isinstance(spec["hosts"], list)
        assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in spec["hosts"]), name
        assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", secret) for secret in spec.get("secrets", {}).values())
    # The remote terminals are no feature: they would run the agent's commands outside the sandbox.
    assert not any("modal" in host or "daytona" in host for spec in hermes.FEATURES.values() for host in spec["hosts"])
    assert "modal-gateway.nousresearch.com" not in hermes.FEATURES["tool-gateway"]["hosts"]


def test_parse_happy_sad_edge():
    assert hermes.parse(["kanban", "web-search,skills-hub"]) == ["kanban", "web-search", "skills-hub"]
    for wrong in (["docker"], ["gateway:irc"], ["GATEWAY:TELEGRAM;rm"], ["../x"]):
        with pytest.raises(ValueError, match="not a Hermes feature"):
            hermes.parse(wrong)
    assert hermes.parse([" KANBAN ", "kanban", ",,"]) == ["kanban"]  # case, blanks, each once
    assert hermes.parse(None) == [] and hermes.parse([]) == []


def test_network_options_add_hosts_and_secrets_but_keep_yours():
    a = ns("hermes", "gateway:slack,kanban")
    a.secret_env = ["SLACK_BOT_TOKEN=mine"]
    hermes.network_options(a)
    assert a.allow_host == hermes.FEATURES["gateway:slack"]["hosts"]
    assert a.secret_env == ["SLACK_BOT_TOKEN=mine", "SLACK_APP_TOKEN=hermes_slack_app"]
    other = ns("claude", "kanban")
    hermes.network_options(other)  # not Hermes: nothing (check() refuses the option)
    assert other.allow_host == [] and other.secret_env == []
    offline = ns("hermes", "skills-hub", offline=True)
    hermes.network_options(offline)
    assert offline.allow_host == []
    broken = ns("hermes", "nope")
    hermes.network_options(broken)  # the error is check()'s
    assert broken.allow_host == []


def test_the_option_counts_as_weakening():
    """A project file or a mode sees --hermes-enable like --allow-host: it opens hosts and gives secrets."""
    assert "--hermes-enable" in s.MODES.WEAKENING_OPTIONS and s.SHORT_OPTIONS["--hermes-enable"] == "-he"
    assert s.MODES.weakening_options(s, ["-he", "kanban", "--no-tmux"]) == [("--hermes-enable", "kanban")]


def test_check_refuses_what_hermes_cannot_do_here():
    hermes.check(ns("hermes", "kanban"))  # fine
    hermes.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="is for Hermes"):
        hermes.check(ns("claude", "kanban"))
    with pytest.raises(ValueError, match="not in the team image"):
        hermes.check(ns("hermes"), companions=["claude"])
    with pytest.raises(ValueError, match="not in the team image"):
        hermes.check(ns("claude"), companions=["hermes"])
    with pytest.raises(ValueError, match="not enforced for Hermes"):
        hermes.check(ns("hermes"), hooks_active=True)
    with pytest.raises(ValueError, match="needs the network"):
        hermes.check(ns("hermes", "kanban", offline=True))


# --- the managed configuration ---------------------------------------------------------------------------------------

def test_managed_config_pins_the_reviewed_defaults():
    config = hermes.managed_config(ns())
    assert config["terminal"] == {"backend": "local"} and config["auth"] == {"adopt_external_logins": False}
    assert config["nous"] == {"guest": False} and config["security"] == {"allow_lazy_installs": False}
    assert config["web"] == {"keyless_fallback": False, "keyless_rescue": False}
    assert config["telemetry"]["shared_metrics"] == {"enabled": False, "send": False}
    assert config["updates"] == {"check": False} and config["model_catalog"] == {"enabled": False}
    assert config["plugins"] == {"auto_update_check_hours": 0, "auto_apply": False}
    assert config["kanban"] == {"dispatch_in_gateway": False} and config["lsp"] == {"install_strategy": "manual"}
    assert "mcp_servers" not in config and "model" not in config
    # JSON is YAML: Hermes' managed scope reads it as it is.
    assert json.loads(hermes.files(ns(), config)["hermes/config.yaml"]) == config


def test_features_turn_their_switches_on_and_telemetry_on_leaves_hermes_its_own():
    config = hermes.managed_config(ns("hermes", "free-tier,model-catalog,updates,lazy-installs,kanban"))
    assert config["nous"] == {"guest": True} and config["model_catalog"] == {"enabled": True}
    assert config["updates"] == {"check": True} and config["plugins"]["auto_update_check_hours"] == 24
    assert config["security"] == {"allow_lazy_installs": True} and config["lsp"] == {"install_strategy": "auto"}
    assert config["kanban"] == {"dispatch_in_gateway": True}
    own = hermes.managed_config(ns(), telemetry="on")  # (gone since 0.1.95: as off)
    assert own["updates"] == {"check": False} and own["terminal"] == {"backend": "local"}
    assert own["telemetry"]["shared_metrics"]["send"] is False  # telemetry: never Hermes' own choice
    # Never undone by a feature: the local terminal, the borrowed logins, Hermes' own proxy.
    every = hermes.managed_config(ns("hermes", ",".join(hermes.FEATURES)))
    assert every["terminal"] == {"backend": "local"} and every["auth"] == {"adopt_external_logins": False}
    assert every["proxy"] == {"enabled": False}


def test_telemetry_is_pinned_off_whatever_is_enabled():
    """No feature and no --telemetry level turns Hermes' telemetry on; no feature opens a telemetry host."""
    for telemetry in ("off", "strict", "on"):
        config = hermes.managed_config(ns("hermes", ",".join(hermes.FEATURES)), telemetry=telemetry)
        for key, value in hermes.TELEMETRY_PINNED.items():
            assert config[key] == hermes.merge(config[key], value), (telemetry, key)
        assert config["telemetry"]["shared_metrics"] == {"enabled": False, "send": False}
    assert not hermes.telemetry_hosts_in(hermes.hosts(list(hermes.FEATURES)))
    env = hermes.environment(ns("hermes", "lazy-installs"))
    assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0" and env["DO_NOT_TRACK"] == "1"
    assert "telemetry" not in " ".join(hermes.FEATURES) and "telemetry" not in hermes.FEATURE_SYNTAX.pattern
    assert [rule["host"] for rule in hermes.telemetry_rules()] == hermes.rule_hosts()
    assert all(rule["decision"] == "deny" and rule["path"] == "/**" for rule in hermes.telemetry_rules())


def test_a_remembered_telemetry_host_is_left_out_not_a_refused_start(tmp_path, monkeypatch):
    """Bug hunt: a telemetry host approved "always" in the project (for another agent, which may reach it) joined a
    Hermes start's --allow-host, and check_telemetry then refused the start with "drop that --allow-host" - an option
    nobody gave. It is left out of the start now (said so), never opened."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    work = tmp_path / "work"
    work.mkdir()
    approvals = s.load_approvals_module()
    approvals.remember(work, host="o1.ingest.sentry.io")
    approvals.remember(work, host="pypi.org")
    a = s.parse_args(["hermes", "--workspace", str(work)])
    notes = s.apply_remembered_approvals(a)
    assert "o1.ingest.sentry.io" not in a.allow_host and "pypi.org" in a.allow_host
    assert any("o1.ingest.sentry.io" in note and "telemetry" in note for note in notes)
    hermes.check_telemetry(a)  # nothing to refuse
    b = s.parse_args(["claude", "--workspace", str(work)])
    s.apply_remembered_approvals(b)
    assert "o1.ingest.sentry.io" in b.allow_host  # another agent's start: as approved


def test_telemetry_hosts_are_never_opened():
    assert hermes.telemetry_hosts_in(["api.telegram.org", "github.com"]) == []
    assert hermes.telemetry_hosts_in(["telemetry.nousresearch.com"]) == ["telemetry.nousresearch.com"]
    assert hermes.telemetry_hosts_in(["*.nousresearch.com", "eu.i.posthog.com"]) == [
        "*.posthog.com", "telemetry.nousresearch.com"]
    for options in ({"allow_host": ["telemetry.nousresearch.com"]}, {"no_inspect": ["o1.ingest.sentry.io"]},
                    {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            hermes.check(ns("hermes", **options))
    hermes.check(ns("hermes", egress_all_allow=True, no_proxy=False))  # the proxy refuses them then


def test_mcp_servers_go_through_the_bridge_without_sampling():
    servers = hermes.mcp_servers(["demo", "helper-codex-1", "demo"], lambda name: ["-I", "bridge", name],
                                 {"helper-codex-1": 2000})
    assert list(servers) == ["demo", "helper-codex-1"]
    assert servers["demo"] == {"command": "python3", "args": ["-I", "bridge", "demo"], "sampling": {"enabled": False}}
    assert servers["helper-codex-1"]["timeout"] == 2000 and "timeout" not in servers["demo"]


def test_the_llm_section_and_the_loop_guards_skill():
    model = {"model": {"provider": "custom", "base_url": "http://127.0.0.1:1/v1", "default": "m"},
             "skills": {"auto_load": [hermes.LOOP_SKILL]}, "agent": {"max_turns": 40}}
    config = hermes.managed_config(ns(), llm=model)
    assert config["model"]["default"] == "m" and config["agent"] == {"max_turns": 40}
    assert config["skills"]["auto_load"] == [hermes.LOOP_SKILL]
    assert config["skills"]["external_dirs"] == [hermes.KIT_SKILLS, hermes.SKILLS_DIR]
    files = hermes.files(ns(), config, loop_text="# rules\n")
    skill = files[f"hermes/skills/{hermes.LOOP_SKILL}/SKILL.md"]
    assert skill.startswith(f"---\nname: {hermes.LOOP_SKILL}\n") and skill.endswith("# rules\n")
    assert f"hermes/skills/{hermes.LOOP_SKILL}/SKILL.md" not in hermes.files(ns(), hermes.managed_config(ns()), "x")


def test_environment_and_plan():
    assert hermes.environment(ns()) == {"HERMES_HOME": "/root/.hermes", "TERMINAL_ENV": "local",
                                        "HERMES_DISABLE_LAZY_INSTALLS": "1", **hermes.TELEMETRY_ENV}
    assert "HERMES_DISABLE_LAZY_INSTALLS" not in hermes.environment(ns("hermes", "lazy-installs"))
    assert hermes.environment(ns(), placeholder="p")["OPENROUTER_API_KEY"] == "p"
    plan = hermes.plan(ns("hermes", "gateway:telegram"))
    assert plan["version"] == hermes.VERSION and plan["gateway"] and plan["experimental"]
    assert plan["enabled"] == [{"feature": "gateway:telegram", "hosts": ["api.telegram.org"],
                                "secrets": ["hermes_telegram"]}]
    assert "gateway:telegram" not in plan["off"] and "kanban" in plan["off"]


def test_login_document():
    assert hermes.login_document_ok({"version": 1, "providers": {"nous": {"access_token": "x"}}})
    assert hermes.login_document_ok({"credential_pool": {"openrouter": [{"key": "x"}]}})
    for wrong in ({}, {"providers": {}}, {"providers": {"nous": {}}}, [], {"providers": "x"}):
        assert not hermes.login_document_ok(wrong)
    assert s.login_document_ok("hermes", {"providers": {"nous": {"access_token": "x"}}})


def test_the_hosts_login_is_hermes_home(tmp_path, monkeypatch):
    assert s.auth_sources("hermes") == [(tmp_path / "home/.hermes/auth.json", ".hermes/auth.json")]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
    assert s.auth_sources("hermes")[0][0] == tmp_path / "elsewhere/auth.json"


# --- --llm: the values kept twice, OpenRouter, the "other models" line --------------------------------------------

def test_llm_keeps_hermes_values_in_step():
    assert llm.HERMES_OPENROUTER_HOST == hermes.OPENROUTER_HOST
    assert llm.HERMES_OPENROUTER_PLACEHOLDER == hermes.OPENROUTER_PLACEHOLDER
    assert llm.HERMES_CONFIG == f"{hermes.MANAGED_DIR}/config.yaml" and hermes.OPENROUTER_PLACEHOLDER.startswith("sk-or-")
    assert llm.LOOP_TEXT and set(llm.AGENTS) == set(s.AUTH_TARGETS)


def endpoint(kind, url):
    return {"name": "x", "kind": kind, "parsed": llm.parse_url(url)}


def test_the_other_models_line_only_for_servers_that_load_models():
    assert llm.local_model_server(endpoint("ollama", "https://gpu.example.org"))
    assert llm.local_model_server(endpoint("openai-compatible", "http://127.0.0.1:8000"))
    assert llm.local_model_server(endpoint("openai-compatible", "http://192.168.1.5:8000"))
    assert not llm.local_model_server(endpoint("openai-compatible", "https://openrouter.ai/api/v1"))
    assert not llm.local_model_server(endpoint("openai-compatible", "https://203.0.113.9/v1"))


def test_llm_show_hides_the_line_for_openrouter(capsys):
    record = {"kind": "openai-compatible", "url": "https://openrouter.ai/api/v1", "api_key_secret": "OR",
              "model": "anthropic/claude-sonnet-4.5"}
    store = llm.load_store()
    store["endpoints"]["openrouter"] = dict(record)
    store["endpoints"]["box"] = {"kind": "ollama", "url": "http://127.0.0.1:11434", "model": "qwen3"}
    llm.save_store(store)
    lines = llm.describe(llm.endpoint("openrouter"))
    assert not any("other models" in line for line in lines)
    assert "  agents: codex, opencode, hermes, dsh, qwen, goose" in lines
    assert any("other models: kept loaded" in line for line in llm.describe(llm.endpoint("box")))


def test_the_placeholder_hermes_sends_to_openrouter():
    a = argparse.Namespace(agent="hermes")
    assert llm.placeholder_for(a, endpoint("openai-compatible", "https://openrouter.ai/api")) == \
        hermes.OPENROUTER_PLACEHOLDER
    assert llm.placeholder_for(a, endpoint("ollama", "http://127.0.0.1:11434")) == llm.PLACEHOLDER
    assert llm.placeholder_for(argparse.Namespace(agent="opencode"),
                               endpoint("openai-compatible", "https://openrouter.ai/api")) == llm.PLACEHOLDER


# --- the image: the reviewed release, the lock, its build -------------------------------------------------------------

def test_the_containerfile_builds_exactly_the_reviewed_release():
    text = CONTAINERFILE.read_text()
    assert f"ARG HERMES_VERSION={hermes.VERSION.lstrip('v')}" in text and f"ARG HERMES_COMMIT={hermes.COMMIT}" in text
    assert 'test "$(git -C /opt/hermes/src rev-parse HEAD)" = "${HERMES_COMMIT}"' in text
    assert "--require-hashes --no-deps" in text and "COPY Containerfile.hermes.lock" in text
    assert "rm -rf /opt/hermes/src/.git" in text  # nothing for Hermes' own updater to pull
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M), "uv (and with it CPython) pinned"
    assert "mkdir -p -m 755 /etc/hermes" in text


def test_the_lock_hashes_every_requirement_for_linux():
    text = LOCK.read_text()
    blocks = re.split(r"\n(?=[a-z0-9])", text.split("\n", 4)[-1].strip())
    assert len(blocks) > 50
    for block in blocks:
        first = block.splitlines()[0]
        assert "--hash=sha256:" in block and " @ " not in first, block[:80]
        # Only what holds on Linux: a marker naming Windows or Android alone never (nemo-relay names both).
        assert "linux" in first or not re.search(r"sys_platform == '(win32|android|emscripten)'", first), first
    names = {block.split("==")[0] for block in blocks}
    assert {"openai", "mcp", "anthropic", "python-telegram-bot", "discord-py", "slack-bolt"} <= names
    assert not names & {"modal", "daytona", "vercel", "boto3"}, "no remote terminal, no cloud SDK"


def test_the_lock_script_keeps_linux_and_drops_the_rest():
    lock = load("hermes_lock_tests", "scripts/hermes_lock.py")
    text = ("a==1 ; python_full_version >= '3.14' \\\n    --hash=sha256:aa\n"
            "b==2 ; sys_platform == 'win32' \\\n    --hash=sha256:bb\n"
            "c @ git+https://example.org/c.git ; sys_platform == 'android'\n"
            "d==4 ; platform_machine == 'aarch64' and sys_platform == 'linux' \\\n    --hash=sha256:dd\n")
    out = lock.keep(text)
    assert "a==1" in out and "d==4" in out and "b==2" not in out and "git+" not in out
    assert out.startswith(lock.HEADER) and not out.rstrip().endswith("\\")
    with pytest.raises(ValueError, match="no hash"):
        lock.keep("e==5\n")


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


def test_build_hermes_takes_the_reviewed_release_only(engine):
    result = build("hermes")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    reviewed = hermes.VERSION.lstrip("v")
    assert calls and f"HERMES_VERSION={reviewed}" in calls[0] and f"sandbox-ai.agent-version={reviewed}" in calls[0]
    assert any(arg.endswith("Containerfile.hermes") for arg in calls[0])
    assert build("--pin", reviewed, "hermes").returncode == 0  # the same release: fine
    refused = build("--pin", "9.9.9", "hermes")
    assert refused.returncode == 2 and "the release sandbox-ai reviewed" in refused.stderr
    assert build("hermes", "--build-arg", "HERMES_VERSION=1.0.0").returncode == 2


# --- the agent's other places: sessions, presets, kits, its helper mode ---------------------------------------------

def test_sessions_continue_and_one_shots():
    assert sessions.CONTINUE_ARGS["hermes"] == ["--continue"]
    assert {"-z", "--oneshot"} <= sessions.ONE_SHOT_ARGS
    assert {"--continue", "-c", "--resume", "-r"} == sessions.SESSION_OPTIONS["hermes"]
    assert {"--model", "--reasoning", "--query"} <= sessions.VALUE_OPTIONS["hermes"]


def test_a_presets_first_task_seeds_hermes_chat():
    preset = {"prompt": "-fix {what}"}
    assert presets.prompt_args(preset, "hermes", {"what": "it"}) == ["chat", "--query=-fix it"]
    assert presets.prompt_args({}, "hermes") == []
    assert "hermes" in presets.AGENTS


def test_kits_and_memory_for_hermes():
    assert kits.LAYOUT["hermes"] == {"skills": ".agents/skills"} and hermes.KIT_SKILLS == "/root/" + ".agents/skills"
    assert kits.MEMORY_FILE["hermes"] == ("MEMORY.md", ".hermes/memories/MEMORY.md")
    skill = (kits.BUILTIN / "hermes/skills/sandbox-ai/SKILL.md").read_text()
    assert "## Hermes in this sandbox" in skill and all(name in skill for name in hermes.FEATURES if ":" not in name)
    assert (kits.TEMPLATES / "hermes/MEMORY.md").is_file()
    assert "hermes" in kits.builtin_kits("hermes") or kits.builtin_kits("hermes")


def test_hermes_as_a_helper_runs_one_shot():
    assert agent_mcp.command("hermes", "-x", resume=True) == ["hermes", "--continue", "--oneshot=-x"]
    assert agent_mcp.command("hermes", "task") == ["hermes", "--oneshot=task"]


def test_the_review_is_shipped_with_the_release_it_names():
    review = (ROOT / "docs/HERMES_REVIEW.md").read_text()
    assert hermes.VERSION in review and hermes.COMMIT in review
    for name in hermes.FEATURES:
        assert f"`{name}`" in review, name
    assert "Go" in review and "--hermes-enable" in review


def test_the_docs_say_telemetry_is_never_switchable():
    """docs/HERMES_REVIEW.md and both READMEs name the rule, the hosts and the variables."""
    review = (ROOT / "docs/HERMES_REVIEW.md").read_text()
    assert "### Telemetry: always off, never switchable" in review
    for readme, words in (("README.md", "always off, with no switch"), ("README_DE.md", "immer aus, ohne Schalter")):
        text = (ROOT / readme).read_text()
        assert words in text and all(host in text for host in hermes.TELEMETRY_HOSTS), readme
        assert "CUA_DRIVER_RS_TELEMETRY_ENABLED=0" in text
    assert all(host in review for host in hermes.TELEMETRY_HOSTS)
    assert all(name in review for name in hermes.TELEMETRY_ENV)
