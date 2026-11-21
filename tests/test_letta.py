"""Letta Code as a sandbox-ai agent (sandbox/letta.py, docs/LETTA_REVIEW.md): the features that turn on again what a
sandbox turns off, its variables and its start script, telemetry that is never switchable, the image (the reviewed
release's npm install hash-locked, no install script, no pipe into a shell), its build, sessions, kits and helper
mode. The start itself is in tests/test_feature_matrix.py (LETTA) and the launcher's contracts
(test_agent_sandbox_contracts.py)."""
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


letta = load("letta_tests", "sandbox/letta.py")
lock_script = load("agent_npm_lock_letta_tests", "scripts/agent_npm_lock.py")
LOCK = ROOT / "sandbox/Containerfile.letta.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.letta"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LETTA_CONFIG_DIR", raising=False)
    (tmp_path / "home").mkdir()


def ns(agent="letta", *enable, **more):
    return argparse.Namespace(**{"agent": agent, "letta_enable": list(enable), "allow_host": [], "secret_env": [],
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
    for name, spec in letta.FEATURES.items():
        # (a feature that only trusts something of the workspace opens no host: letta.TRUSTS)
        assert letta.FEATURE_SYNTAX.fullmatch(name) and spec["help"] and (spec["hosts"] or name in letta.TRUSTS), name
        assert all(re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) for host in spec["hosts"]), name
    # No feature for its telemetry: never switchable; no feature drops a telemetry variable.
    assert not {"telemetry", "analytics", "scarf", "channels"} & set(letta.FEATURES)
    assert not letta.telemetry_hosts_in(letta.hosts(list(letta.FEATURES)))
    assert not {v for spec in letta.FEATURES.values() for v in spec.get("drops", [])} & set(letta.TELEMETRY_ENV)
    assert set(letta.FEATURES) == {"cloud", "updates", "skills-install", "project-hooks"}
    assert letta.FEATURES["cloud"]["hosts"] == ["api.letta.com", "app.letta.com"], "Letta Cloud: only its hosts"


def test_parse_happy_sad_edge():
    assert letta.parse(["cloud", "updates,skills-install"]) == ["cloud", "updates", "skills-install"]
    for wrong in (["docker"], ["telemetry"], ["cloud;rm"], ["../x"], ["channels"], ["Cloud:x"]):
        with pytest.raises(ValueError, match="not a Letta Code feature"):
            letta.parse(wrong)
    assert letta.parse([" CLOUD ", "cloud", ",,"]) == ["cloud"]
    assert letta.parse(None) == [] and letta.parse([]) == []


def test_network_options_add_only_the_features_hosts():
    a = ns("letta", "cloud", "updates")
    a.allow_host = ["github.com"]
    letta.network_options(a)
    assert a.allow_host == ["github.com", "api.letta.com", "app.letta.com", "registry.npmjs.org"]
    for other in (ns("claude", "cloud"), ns("letta", "cloud", offline=True), ns("letta", "nope")):
        letta.network_options(other)  # not Letta Code, offline, a wrong name: nothing (check() says why)
        assert other.allow_host == []


def test_check_refuses_what_letta_cannot_do_here():
    letta.check(ns("letta", "cloud"))  # fine
    letta.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="is for Letta Code"):
        letta.check(ns("claude", "cloud"))
    letta.check(ns("letta"), companions=["claude"])  # --with: a layer of the team image since 0.1.98
    letta.check(ns("letta"), hooks_active=True)  # rules for its own tools: hooks laid in by sandbox-ai's start
    with pytest.raises(ValueError, match="needs the network"):
        letta.check(ns("letta", "updates", offline=True))
    with pytest.raises(ValueError, match="--backend cloud needs --letta-enable cloud"):
        letta.check(ns("letta", agent_args=["--backend", "cloud"]))


def test_an_argument_of_a_feature_that_is_off_is_refused():
    for args, feature in ((["update"], "updates"), (["upgrade"], "updates"), (["--update"], "updates"),
                          (["install", "official/finance"], "skills-install"),
                          (["computers", "list"], "cloud"), (["teleport", "cloud"], "cloud"),
                          (["-p", "--computer", "laptop"], "cloud"), (["--computer=cloud"], "cloud"),
                          (["--backend", "cloud"], "cloud"), (["--backend=api"], "cloud"), (["server"], "cloud"),
                          (["server", "--computer-name", "x"], "cloud"), (["usage"], "cloud")):
        refused = letta.refused_arg(args)
        assert refused and feature in refused[1], args
        assert letta.refused_arg(args, [feature]) is None, args
    # Always refused: channels (their runtimes come from npm at run time), an App Server off the loopback.
    for args in (["server", "--channels", "telegram"], ["server", "--channels=slack"],
                 ["server", "--install-channel-runtimes"], ["server", "--listen", "ws://0.0.0.0:4500"],
                 ["server", "--listen=ws://10.0.0.2:4500"]):
        assert letta.refused_arg(args, list(letta.FEATURES)), args
    for args in ([], ["--backend", "local"], ["-p", "--", "update the docs"], ["--", "update"], ["connect", "openai"],
                 ["agents", "list"], ["mcp", "tools"], ["server", "--listen"], ["server", "--listen", "ws://127.0.0.1:4500"],
                 ["server", "--listen=ws://localhost:0"], ["--new"], ["--resume"], ["-p", "fix", "the", "update"]):
        assert letta.refused_arg(args) is None, args


# --- the variables and the start script -----------------------------------------------------------------------------

def test_the_variables_keep_letta_local_and_quiet():
    env = letta.environment(ns())
    assert env["LETTA_LOCAL_BACKEND_EXPERIMENTAL"] == "1" and env["LETTA_BASE_URL"] == "http://127.0.0.1:9"
    assert env["DISABLE_AUTOUPDATER"] == "1" and env["LETTA_FS_SANDBOX"] == "0"
    assert env["LETTA_SKIP_KEYCHAIN_CHECK"] == "1" and env["LETTA_DISABLE_LSP_DOWNLOAD"] == "1"
    cloud = letta.environment(ns("letta", "cloud"))
    assert "LETTA_LOCAL_BACKEND_EXPERIMENTAL" not in cloud and "LETTA_BASE_URL" not in cloud
    assert "DISABLE_AUTOUPDATER" not in letta.environment(ns("letta", "updates"))


def test_telemetry_is_pinned_off_whatever_is_enabled():
    """The user's rule: never switchable. Every feature: the variables hold."""
    env = letta.environment(ns("letta", *letta.FEATURES))
    for key, value in letta.TELEMETRY_ENV.items():
        assert env[key] == value
    assert env["LETTA_CODE_TELEM"] == "0" and env["DO_NOT_TRACK"] == "1"


def test_telemetry_hosts_and_paths_are_never_opened():
    assert letta.telemetry_hosts_in(["static.scarf.sh"]) == ["*.scarf.sh"]
    assert letta.telemetry_hosts_in(["api.letta.com", "registry.npmjs.org", "scarf.sh.evil"]) == []
    for opened in ({"allow_host": ["static.scarf.sh"]}, {"no_inspect": ["*.scarf.sh"]},
                   {"no_inspect": ["api.letta.com"]}, {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            letta.check_telemetry(argparse.Namespace(**opened))
    with pytest.raises(ValueError, match="drop --allow-domain scarf.sh"):
        letta.check_telemetry(argparse.Namespace(allow_host=["scarf.sh", "*.scarf.sh"], allow_domain=["scarf.sh"]))
    rules = letta.telemetry_rules(ns("letta", "cloud"))
    assert all(rule["decision"] == "deny" for rule in rules)
    assert {(rule["host"], rule["path"]) for rule in rules} == {
        ("*.scarf.sh", "/**"), ("scarf.sh", "/**"), ("api.letta.com", "/v1/metadata/telemetry/**"),
        ("api.letta.com", "/v1/metadata/feedback/**")}, "the paths also with Letta Cloud opened"
    assert len({rule["id"] for rule in rules}) == len(rules)


def test_mcp_servers_run_the_bridge():
    servers = letta.mcp_servers(["demo", "helper-claude-x", "demo"], lambda name: ["/bridge", name])
    assert servers == [{"name": "demo", "command": "python3", "args": ["/bridge", "demo"]},
                       {"name": "helper-claude-x", "command": "python3", "args": ["/bridge", "helper-claude-x"]}]
    assert letta.mcp_servers([], lambda name: []) == []


def test_files_and_command():
    config = letta.start_config(ns(model="anthropic/claude-sonnet-4-5"), [{"name": "x"}])
    assert config == {"servers": [{"name": "x"}], "model": "anthropic/claude-sonnet-4-5", "cloud": False,
                      "note": None, "hooks": None, "project_hooks": False}
    assert letta.start_config(ns(), None, None, "/sandbox/session/network.md")["note"] == "/sandbox/session/network.md"
    assert letta.start_config(ns("letta", "cloud"), None, "llama-cpp/qwen3")["model"] == "llama-cpp/qwen3"
    files = letta.files(ns(), config)
    assert set(files) == {"letta/sandbox-ai-start.py", "letta/sandbox-ai-start.json"}
    assert json.loads(files["letta/sandbox-ai-start.json"]) == config
    assert files["letta/sandbox-ai-start.py"] == letta.START_SCRIPT
    assert letta.START.startswith(letta.MANAGED_DIR + "/") and letta.START_CONFIG in letta.START_SCRIPT
    assert letta.command(ns(), "/usr/local/bin/letta", ["--new"]) == [
        "python3", "-I", letta.START, "/usr/local/bin/letta", "--new"]
    assert letta.model_args(ns(model="m")) == ["--model", "m"] and letta.model_args(ns(model="m", llm="x")) == []


@pytest.fixture
def start(tmp_path):
    """The start script with its configuration at TMP/start.json and a fake letta that records its arguments and
    creates an agent the way `letta agents create` does."""
    script = tmp_path / "start.py"
    script.write_text(letta.START_SCRIPT.replace(letta.START_CONFIG, str(tmp_path / "start.json")))
    fake = tmp_path / "letta"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
home = os.path.join(os.environ["HOME"], ".letta")
with open(os.path.join(os.environ["HOME"], "calls"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["agents", "create"]:
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "settings.json")
    settings = json.load(open(path)) if os.path.exists(path) else {{}}
    settings.setdefault("agents", []).append({{"agentId": "agent-local-1", "baseUrl": "local:" + home}})
    json.dump(settings, open(path, "w"))
    print(json.dumps({{"id": "agent-local-1"}}))
""")
    fake.chmod(0o755)
    home = tmp_path / "home"

    def run(config, *args):
        (tmp_path / "start.json").write_text(json.dumps(config))
        result = subprocess.run([sys.executable, "-I", str(script), str(fake), *args], capture_output=True, text=True,
                                env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=60)
        assert result.returncode == 0, result.stderr
        calls = [json.loads(line) for line in (home / "calls").read_text().splitlines()]
        (home / "calls").unlink()
        settings = home / ".letta/settings.json"
        return calls, json.loads(settings.read_text()) if settings.exists() else None
    return run


def test_the_start_script_lays_the_servers_in_and_becomes_letta(start, tmp_path):
    server = {"name": "demo", "command": "python3", "args": ["/bridge", "demo"]}
    calls, settings = start({"servers": [server], "model": "llama-cpp/m", "cloud": False})
    assert calls == [["agents", "create", "--model", "llama-cpp/m"], ["--agent", "agent-local-1"]], \
        "a first start creates the agent and goes on with it"
    assert settings["agents"][0]["mcpServers"] == [server]
    # A user's own server stays; the next start's servers replace the ones sandbox-ai laid in before.
    settings["agents"][0]["mcpServers"].append({"name": "mine", "command": "x"})
    (tmp_path / "home/.letta/settings.json").write_text(json.dumps(settings))
    other = {"name": "other", "command": "python3", "args": []}
    calls, settings = start({"servers": [other], "cloud": False}, "-p", "--", "hi")
    assert calls == [["-p", "--", "hi"]], "no second agent, no --agent once one exists"
    assert [s["name"] for s in settings["agents"][0]["mcpServers"]] == ["mine", "other"]
    # Without --mcp: sandbox-ai's servers are taken out, the user's stay.
    calls, settings = start({"servers": [], "cloud": False}, "--new")
    assert calls == [["--new"]] and settings["agents"][0]["mcpServers"] == [{"name": "mine", "command": "x"}]


def test_the_start_script_becomes_letta_named_by_its_name_alone(tmp_path):
    """CI 0.1.186 (agents-live letta): under Podman the launcher gives the start script the binary's name - "letta",
    found in the image's PATH (/usr/local/bin/letta) - and os.execv, which looks in no PATH, said FileNotFoundError
    after the servers were laid in (`letta agents create` went through PATH). Now it becomes the letta of PATH."""
    script = tmp_path / "start.py"
    script.write_text(letta.START_SCRIPT.replace(letta.START_CONFIG, str(tmp_path / "start.json")))
    (tmp_path / "start.json").write_text(json.dumps({"servers": [], "cloud": False}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "letta"
    fake.write_text(f"#!{sys.executable}\nimport sys\nprint('letta ran:', sys.argv[1:])\n")
    fake.chmod(0o755)
    work = tmp_path / "work"  # (the workspace, the start's folder: no letta there)
    work.mkdir()
    result = subprocess.run([sys.executable, "-I", str(script), "letta", "-p", "hi"], capture_output=True, text=True,
                            cwd=work, env={"HOME": str(tmp_path / "home"), "PATH": f"{bin_dir}:/usr/bin:/bin"},
                            timeout=60)
    assert result.returncode == 0 and "letta ran: ['-p', 'hi']" in result.stdout, result.stderr


def test_a_first_start_with_a_model_goes_on_with_the_agent_that_has_the_servers(start, tmp_path):
    """CI 0.1.187 (agents-live letta: "the model was never offered the demo server's tool"): the launcher puts
    --model HANDLE (--model, --llm) before the arguments, and the start script took the handle for a subcommand, so a
    first start with --mcp did not go on with the agent it made and laid the servers into - Letta Code made another
    one, without them. A subcommand (letta agents ...) still gets no --agent, nor does a start that chooses one."""
    server = {"name": "demo", "command": "python3", "args": ["/bridge", "demo"]}
    config = {"servers": [server], "model": "openai-compat/live-model", "cloud": False}
    calls, settings = start(config, "--model", "openai-compat/live-model", "--yolo", "-p", "--", "hi")
    assert calls == [["agents", "create", "--model", "openai-compat/live-model"],
                     ["--agent", "agent-local-1", "--model", "openai-compat/live-model", "--yolo", "-p", "--", "hi"]]
    assert settings["agents"][0]["mcpServers"] == [server]
    for args in (["--model", "m", "agents", "list"], ["-m", "m", "--name", "x", "-p", "--", "hi"]):
        (tmp_path / "home/.letta/settings.json").unlink()
        calls, _ = start(config, *args)
        assert calls[0][:2] == ["agents", "create"] and calls[1] == args, args


def test_a_failed_agent_creation_is_said(tmp_path):
    """When `letta agents create` makes no agent, the --mcp servers have none to go into: said, Letta Code starts."""
    script = tmp_path / "start.py"
    script.write_text(letta.START_SCRIPT.replace(letta.START_CONFIG, str(tmp_path / "start.json")))
    (tmp_path / "start.json").write_text(json.dumps({"servers": [{"name": "demo", "command": "x"}], "cloud": False}))
    fake = tmp_path / "letta"
    fake.write_text(f"#!{sys.executable}\nimport sys\nif sys.argv[1:3] == ['agents', 'create']:\n"
                    "    sys.exit('error: no provider for live-model')\nprint('letta ran')\n")
    fake.chmod(0o755)
    result = subprocess.run([sys.executable, "-I", str(script), str(fake)], capture_output=True, text=True,
                            env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}, timeout=60)
    assert result.returncode == 0 and "letta ran" in result.stdout, result.stderr
    assert "letta agents create did not make the agent for the --mcp servers (exit 1: error: no provider for " \
        "live-model)" in result.stderr


def test_the_start_script_never_follows_a_symlink_and_starts_letta_anyway(start, tmp_path):
    (tmp_path / "home/.letta").mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"agents": [{"agentId": "a", "baseUrl": "local:x"}]}))
    os.symlink(target, tmp_path / "home/.letta/settings.json")
    calls, _ = start({"servers": [{"name": "demo", "command": "python3"}], "cloud": True}, "--resume")
    assert calls == [["--resume"]] and "mcpServers" not in target.read_text()


RULE_HOOKS = {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "timeout": 120,
                                                         "command": "/usr/bin/python3 -I /sandbox/runtime.py "
                                                                    "--internal-hook letta PreToolUse"}]},
                             {"matcher": "Bash", "hooks": [{"type": "command", "timeout": 180,
                                                            "command": "/usr/bin/python3 -I /sandbox/runtime.py "
                                                                       "--internal-kit-hook letta 60 "
                                                                       "/sandbox/session/kits/hooks/1/check.sh"}]}],
              "SessionStart": [{"hooks": [{"type": "command", "command": "/sandbox/session/kits/hooks/1/hi.sh"}]}]}


def test_letta_hooks_in_milliseconds_with_the_rule_hook_after_the_kits():
    hooks = letta.letta_hooks(RULE_HOOKS)
    assert [g["hooks"][0]["command"].split()[3] for g in hooks["PreToolUse"]] == ["--internal-kit-hook",
                                                                                  "--internal-hook"]
    assert [g["hooks"][0]["timeout"] for g in hooks["PreToolUse"]] == [180000, 120000]
    assert hooks["SessionStart"][0]["hooks"][0]["timeout"] == 60000


def test_the_start_script_lays_the_hooks_first_and_takes_an_earlier_starts_out(start, tmp_path):
    """The run's hooks first in each event of the user's settings, hooks on there (the user's level wins over the
    project's); the user's own hooks stay after them; a start without hooks takes sandbox-ai's out again."""
    hooks = letta.letta_hooks(RULE_HOOKS)
    (tmp_path / "home/.letta").mkdir(parents=True)
    mine = {"matcher": "Write", "hooks": [{"type": "command", "command": "~/mine.sh"}]}
    (tmp_path / "home/.letta/settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [mine],
                                                                              "disabled": True}, "theme": "x"}))
    calls, settings = start({"servers": [], "cloud": False, "hooks": hooks}, "--new")
    assert calls == [["--new"]] and settings["theme"] == "x"
    assert settings["hooks"]["PreToolUse"] == [*hooks["PreToolUse"], mine]
    assert settings["hooks"]["SessionStart"] == hooks["SessionStart"] and settings["hooks"]["disabled"] is False
    calls, again = start({"servers": [], "cloud": False, "hooks": hooks}, "--new")
    assert again == settings  # laid once, never twice
    calls, settings = start({"servers": [], "cloud": False, "hooks": None}, "--new")
    assert settings["hooks"] == {"PreToolUse": [mine], "disabled": False}


def test_the_start_script_refuses_letta_when_the_hooks_cannot_be_laid_in(tmp_path):
    """The rules hold only through these hooks: a settings file that is a symlink (never written through) or no JSON
    stops the start before Letta Code runs."""
    script = tmp_path / "start.py"
    script.write_text(letta.START_SCRIPT.replace(letta.START_CONFIG, str(tmp_path / "start.json")))
    (tmp_path / "start.json").write_text(json.dumps({"servers": [], "hooks": letta.letta_hooks(RULE_HOOKS)}))
    home = tmp_path / "home"
    (home / ".letta").mkdir(parents=True)
    (home / ".letta/settings.json").write_text("{not json")
    result = subprocess.run([sys.executable, "-I", str(script), "/bin/false"], capture_output=True, text=True,
                            env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=60)
    assert result.returncode == 1 and "Letta Code is not started" in result.stderr
    (tmp_path / "start.json").write_text("{broken")  # what the start asked for cannot be read: refused too
    (home / ".letta/settings.json").write_text("{}")
    result = subprocess.run([sys.executable, "-I", str(script), "/bin/false"], capture_output=True, text=True,
                            env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=60)
    assert result.returncode == 1 and "Letta Code is not started" in result.stderr


def test_plan_says_what_is_on_and_off():
    plan = letta.plan(ns("letta", "updates"))
    assert plan["version"] == letta.VERSION and plan["commit"] == letta.COMMIT and plan["experimental"]
    assert plan["telemetry"]["switchable"] is False and plan["backend"] == "local"
    assert plan["enabled"] == [{"feature": "updates", "hosts": ["registry.npmjs.org"]}]
    assert plan["off"] == ["cloud", "skills-install", "project-hooks"] and plan["own_sandbox"] is False
    assert letta.plan(ns("letta", "cloud"))["backend"] == "cloud"


# --- the image: the reviewed release, the lock, its build -------------------------------------------------------------

def test_the_containerfile_builds_exactly_the_reviewed_release():
    text = CONTAINERFILE.read_text()
    assert f"ARG LETTA_VERSION={letta.VERSION}" in text and "COPY Containerfile.letta.lock" in text
    assert "npm ci --ignore-scripts" in text and "npm install" not in text
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert "mkdir -p -m 755 /etc/letta" in text and "/usr/local/bin/letta" in text
    assert "fd-find" in text and "ripgrep" in text, "else Letta Code downloads them from GitHub"
    assert "DISABLE_AUTOUPDATER=1 letta --version" in text, "--version checks for updates too"
    assert "LETTA_CODE_TELEM=0" in text and "ENV DISABLE_AUTOUPDATER" not in text, "updates stays switchable"


def test_the_lock_holds_the_reviewed_package_with_every_integrity():
    lock = json.loads(LOCK.read_text())
    assert lock_script.check(lock, "letta", letta.PACKAGE, letta.VERSION) > 100
    entry = lock["packages"][f"node_modules/{letta.PACKAGE}"]
    assert entry["integrity"] == letta.INTEGRITY and entry["resolved"].startswith("https://registry.npmjs.org/")
    assert all(entry.get("resolved", "").startswith("https://registry.npmjs.org/")
               for path, entry in lock["packages"].items() if path), "only npm's registry"
    assert lock_script.package_json("letta", letta.PACKAGE, letta.VERSION) == lock["packages"][""] | {"private": True}
    assert "letta" in lock_script.AGENTS


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


def test_build_letta_takes_the_reviewed_release_only(engine):
    result = build("letta")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"LETTA_VERSION={letta.VERSION}" in calls[0]
    assert f"sandbox-ai.agent-version={letta.VERSION}" in calls[0]
    assert any(arg.endswith("Containerfile.letta") for arg in calls[0])
    assert build("--pin", letta.VERSION, "letta").returncode == 0
    refused = build("--pin", "0.34.59", "letta")
    assert refused.returncode == 2 and "Letta Code is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("letta", "--build-arg", "LETTA_VERSION=0.34.59").returncode == 2
    assert "letta" in build("--help").stdout


def test_build_all_builds_letta_too():
    text = (ROOT / "sandbox/build_image.sh").read_text()
    assert re.search(r"^    all\) agents=\([a-z0-9 ]* letta[a-z0-9 ]*\); shift; break ;;$", text, re.M)


# --- the review ---------------------------------------------------------------------------------------------------------

def test_the_review_is_shipped_with_the_release_it_names():
    review = (ROOT / "docs/LETTA_REVIEW.md").read_text()
    assert letta.VERSION in review and letta.COMMIT in review and letta.INTEGRITY in review
    for name in letta.FEATURES:
        assert f"`{name}`" in review, name
    assert "## Go / No-Go" in review and "--letta-enable" in review and "## Hosts contacted by default (measured)" in review
    assert "### Telemetry: always off, never switchable" in review
    assert all(name in review for name in letta.TELEMETRY_ENV) and all(name in review for name in letta.BASE_ENV)
    assert "/v1/metadata/telemetry" in review and "*.scarf.sh" in review


# --- the agent's other places: the launcher, --llm, sessions, presets, kits, its helper mode -------------------------

s = load("launcher_letta_tests", "sandbox/agent_sandbox.py")
llm = load("llm_letta_tests", "sandbox/llm.py")
sessions = load("sessions_letta_tests", "sandbox/sessions.py")
presets = load("presets_letta_tests", "sandbox/presets.py")
kits = load("kits_letta_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_letta_tests", "sandbox/agent_mcp.py")


def test_the_option_counts_as_weakening_and_the_launcher_knows_letta():
    assert "--letta-enable" in s.MODES.WEAKENING_OPTIONS and s.SHORT_OPTIONS["--letta-enable"] == "-lx"
    assert s.MODES.weakening_options(s, ["-lx", "cloud", "--no-tmux"]) == [("--letta-enable", "cloud")]
    assert len(set(s.SHORT_OPTIONS.values())) == len(s.SHORT_OPTIONS), "every short option once"
    assert s.PROVIDERS["letta"] == [] and s.DANGER_ARGS["letta"] == letta.DANGER_ARGS
    assert s.UNSWITCHABLE_TELEMETRY["letta"] == "Letta Code" and "letta" in s.COMPANION_AGENTS
    assert s.pinned_telemetry_environment("letta") == letta.TELEMETRY_ENV
    assert s.EXPERIMENTAL_PARTS["letta"] == {"agent": "letta"} and s.experimental_needs(["letta"]) == ["letta"]


def test_letta_has_no_login_to_keep(tmp_path):
    assert not letta.login_document_ok({"providers": {}}) and not s.login_document_ok("letta", {"x": "y"})
    assert s.auth_sources("letta") == [(tmp_path / "home/.letta/lc-local-backend/providers/auth.json",
                                        ".letta/lc-local-backend/providers/auth.json")]
    assert s.merge_refreshed("letta", {"a": 1}, {"a": 2}) is None
    assert "letta" in s.NO_LOGIN_AGENTS and "letta" in s.LOGIN_COMMANDLESS
    login = load("login_letta_tests", "sandbox/login.py")
    assert "letta" not in login.AGENTS


def test_llm_keeps_letta_s_values_in_step():
    assert (llm.LETTA_PROVIDER, llm.LETTA_URL_VARIABLE, llm.LETTA_KEY_VARIABLE) == (
        letta.LLM_PROVIDER, letta.LLM_URL_VARIABLE, letta.LLM_KEY_VARIABLE)
    assert "letta" in llm.AGENTS and set(llm.AGENTS) == set(s.AUTH_TARGETS)


def test_sessions_continue_and_options():
    assert sessions.CONTINUE_ARGS["letta"] == [], "Letta Code goes on with the project's last conversation itself"
    assert {"--resume", "--new", "--agent", "--conversation"} <= sessions.SESSION_OPTIONS["letta"]
    assert {"--model", "--agent", "--backend", "--system"} <= sessions.VALUE_OPTIONS["letta"]
    restarted = sessions.restart_args({"agent": "letta", "options": [], "agent_args": ["-p", "--", "fix it"]})
    assert restarted == [], "the first task is not run again"


def test_a_presets_first_task_runs_headless():
    assert presets.prompt_args({"prompt": "-fix {what}"}, "letta", {"what": "it"}) == ["-p", "--", "-fix it"]
    assert presets.prompt_args({}, "letta") == [] and "letta" in presets.AGENTS
    for name in ("coding-letta", "letta-local-llm"):
        preset = json.loads((ROOT / f"sandbox/presets/{name}.json").read_text())
        assert preset["agents"] == ["letta"] and preset["category"] == "agents" and preset["translations"]["de"]
        assert any("not live-tested yet" in note for note in preset["notes"])


def test_kits_and_memory_for_letta():
    assert kits.LAYOUT["letta"] == {"skills": ".letta/skills"}
    assert kits.MEMORY_FILE["letta"] == ("AGENTS.md", letta.MEMORY_NOTES)
    skill = (kits.BUILTIN / "letta/skills/sandbox-ai/SKILL.md").read_text()
    assert "## Letta Code in this sandbox" in skill and all(f"`{name}`" in skill for name in letta.FEATURES)
    assert all(host in skill for host in letta.TELEMETRY_HOSTS) and "~/.letta/AGENTS.md" in skill
    assert "goose" not in without_companion_commands(skill)
    assert (kits.TEMPLATES / "letta/AGENTS.md").is_file()
    assert "~/.letta/AGENTS.md" in (kits.TEMPLATES / "letta/skills/sandbox-example/SKILL.md").read_text()
    for kit in sorted(kits.BUILTIN_KITS.iterdir()):
        if (kit / "goose").is_dir():
            assert (kit / "letta" / kits.SHARED_SKILLS_MARKER).is_file(), kit.name
            assert "letta" in json.loads((kit / "kit.json").read_text())["agents"], kit.name


def test_letta_as_a_helper_runs_one_task_headless():
    assert agent_mcp.command("letta", "-x") == ["letta", "-p", "--new", "--", "-x"]
    assert agent_mcp.command("letta", "go on", resume=True, skip=True) == ["letta", "-p", "--yolo", "--", "go on"]
    assert "letta" in agent_mcp.AGENTS


def test_project_hooks_that_would_run_before_the_rule_check_stop_the_start(start, tmp_path):
    """Backlog A: Letta Code 0.34.36 runs the project's (settings.local.json, settings.json of .letta in the working
    folder) hooks before the user's, where sandbox-ai's rule hook is, and the first updatedInput wins: a project hook
    could rewrite a call the check never saw. While rules hold, a project with hooks stops the start before Letta
    Code runs, naming the file and how to go on; --letta-enable project-hooks trusts them (the human's choice)."""
    hooks = letta.letta_hooks(RULE_HOOKS)
    work = tmp_path / "work"
    (work / ".letta").mkdir(parents=True)
    script = tmp_path / "start.py"
    home = tmp_path / "home"

    def run(config):
        (tmp_path / "start.json").write_text(json.dumps(config))
        return subprocess.run([sys.executable, "-I", str(script), str(tmp_path / "letta")], capture_output=True,
                              text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, cwd=work, timeout=60)
    (work / ".letta/settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))
    assert run({"servers": [], "hooks": hooks}).returncode == 0, "project settings without hooks: fine"
    for name in ("settings.json", "settings.local.json"):
        (work / ".letta/settings.json").write_text("{}")
        (work / ".letta/settings.local.json").write_text("{}")
        (work / ".letta" / name).write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "./rewrite.sh"}]}]}}))
        refused = run({"servers": [], "hooks": hooks})
        assert refused.returncode == 1, name
        assert "Letta Code is not started" in refused.stderr and f".letta/{name}" in refused.stderr
        assert "--letta-enable project-hooks" in refused.stderr and "before sandbox-ai's rule check" in refused.stderr
        assert run({"servers": [], "hooks": hooks, "project_hooks": True}).returncode == 0, "trusted"
        assert run({"servers": [], "hooks": None}).returncode == 0, "no rules: nothing to keep first"
    (work / ".letta/settings.local.json").write_text("{broken")
    (work / ".letta/settings.json").write_text("{}")
    assert run({"servers": [], "hooks": hooks}).returncode == 1, "a project file that cannot be checked: refused"
    assert letta.start_config(ns("letta", "project-hooks"))["project_hooks"] is True
    assert letta.start_config(ns("letta"))["project_hooks"] is False
