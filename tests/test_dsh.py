"""DeepSeek Harness as a sandbox-ai agent (sandbox/dsh.py, docs/DSH_REVIEW.md): its read-only patch, its command line,
telemetry that is never switchable, the image (the reviewed release, the hash-locked npm install, no pipe into a
shell), its build, sessions and helper mode. The start itself is in tests/test_feature_matrix.py (DSH) and the
launcher's contracts (test_agent_sandbox_contracts.py)."""
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


dsh = load("dsh_tests", "sandbox/dsh.py")
s = load("launcher_dsh_tests", "sandbox/agent_sandbox.py")
llm = load("llm_dsh_tests", "sandbox/llm.py")
sessions = load("sessions_dsh_tests", "sandbox/sessions.py")
presets = load("presets_dsh_tests", "sandbox/presets.py")
kits = load("kits_dsh_tests", "sandbox/kits.py")
agent_mcp = load("agent_mcp_dsh_tests", "sandbox/agent_mcp.py")
lock_script = load("agent_npm_lock_dsh_tests", "scripts/agent_npm_lock.py")
LOCK = ROOT / "sandbox/Containerfile.dsh.lock"
CONTAINERFILE = ROOT / "sandbox/Containerfile.dsh"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DSH_HOME", raising=False)
    (tmp_path / "home").mkdir()


def ns(agent="dsh", args=(), **more):
    return argparse.Namespace(**{"agent": agent, "agent_args": list(args), "allow_host": [], "no_inspect": [],
                                 "model": None, **more})


# --- the command line -------------------------------------------------------------------------------------------------

def test_the_patch_comes_right_after_the_profile():
    assert dsh.command_args([]) == ["web", "--patch", dsh.PATCH, *dsh.DEFAULT_ARGS[1:]]
    assert dsh.command_args(["headless", "run the tests"]) == ["headless", "--patch", dsh.PATCH, "run the tests"]
    assert dsh.command_args(["--profile", "acp"]) == ["--profile", "acp", "--patch", dsh.PATCH]
    assert dsh.command_args(["--help"]) == ["--help"]  # the launcher's own flags: no profile, no patch
    assert dsh.DEFAULT_ARGS == ("web", "--no-open", "--host", "127.0.0.1", "--port", "3080")


def test_check_refuses_what_dsh_cannot_do_here():
    dsh.check(ns(args=["headless", "x"]))  # fine
    dsh.check(ns("claude"))  # nothing to say about another agent
    with pytest.raises(ValueError, match="not in the team image"):
        dsh.check(ns(), companions=["claude"])
    with pytest.raises(ValueError, match="not in the team image"):
        dsh.check(ns("claude"), companions=["dsh"])
    with pytest.raises(ValueError, match="not enforced for DeepSeek Harness"):
        dsh.check(ns(), hooks_active=True)
    for args in (["desktop"], ["plugin", "--profile", "web", "add", "x"], ["--profile", "desktop"]):
        with pytest.raises(ValueError, match="not in a sandbox"):
            dsh.check(ns(args=args))
    for args in (["web", "--patch", "x.yml"], ["headless", "--patch=x.yml", "task"]):
        with pytest.raises(ValueError, match="--patch in DSH's arguments"):
            dsh.check(ns(args=args))
    dsh.check(ns(args=["headless", "--", "--patch is just text"]))  # after "--"


# --- the patch ----------------------------------------------------------------------------------------------------

def test_the_patch_pins_telemetry_last():
    assert dsh.patch(ns()) == dsh.TELEMETRY_PINNED
    assert {row["id"] for row in dsh.TELEMETRY_PINNED} == {"session-telemetry-otel", "otel"}
    sneaky = {"route": {"api": "openai-completions"}, "model": "m", "instructions": None}
    rows = dsh.patch(ns(model="deepseek-pro"), mcp=[{"id": "x"}], llm=sneaky)
    assert rows[-2:] == dsh.TELEMETRY_PINNED, "after everything: never turned on again"


def test_model_rows():
    assert dsh.model_rows(ns()) == []
    assert dsh.model_rows(ns(model="deepseek-pro")) == [
        {"id": "agent-default-model", "config": {"provider": "deepseek-official", "model": "deepseek-pro"}}]
    rows = dsh.model_rows(ns(), {"route": {"api": "openai-completions"}, "model": "q", "instructions": "be brief"})
    assert rows[0] == {"id": "llm-pi-ai", "config": {"providers": {"sandbox-ai": {"api": "openai-completions"}}}}
    assert rows[1]["config"] == {"provider": "sandbox-ai", "model": "q"}
    assert rows[2]["config"]["personaPrefix"] == dsh.PERSONA + "\n\nbe brief"


def test_mcp_rows_go_through_the_bridge():
    rows = dsh.mcp_rows(["demo", "helper-claude-x", "demo"], lambda name: ["/bridge", name], {"helper-claude-x": 2000})
    assert [row["config"]["serverName"] for row in rows] == ["demo", "helper-claude-x"]
    assert rows[0]["config"] == {"serverName": "demo", "transport": "stdio", "command": "python3",
                                 "args": ["/bridge", "demo"]}
    assert rows[1]["config"]["toolCallTimeoutMs"] == 2000 * 1000
    long = dsh.mcp_rows(["x" * 40], lambda name: [])[0]["config"]["serverName"]
    assert re.fullmatch(dsh.MCP_NAME, long)


def test_telemetry_hosts_are_never_opened():
    assert dsh.telemetry_hosts_in(["*.deepseeksvc.com"]) == list(dsh.TELEMETRY_HOSTS)
    assert dsh.telemetry_hosts_in(["api.deepseek.com", "platform.deepseek.com", "deepseeksvc.com.evil"]) == []
    assert not dsh.telemetry_hosts_in(s.PROVIDERS["dsh"])
    for opened in ({"allow_host": ["dsh-otel-collector.deepseeksvc.com"]},
                   {"no_inspect": ["dsh-otel-collector.deepseeksvc.com"]}, {"egress_all_allow": True, "no_proxy": True}):
        with pytest.raises(ValueError, match="telemetry is always off"):
            dsh.check_telemetry(argparse.Namespace(**opened))
    assert [rule["host"] for rule in dsh.telemetry_rules()] == dsh.rule_hosts() == list(dsh.TELEMETRY_HOSTS)


def test_environment_and_plan():
    env = dsh.environment(ns())
    assert env["DSH_TELEMETRY_DISABLED"] == "1" and env["DSH_HOME"] == "/root/.dsh"
    assert env["DSH_TELEMETRY_OTLP_URL"].startswith("http://127.0.0.1:9/"), "a closed port, never the collector"
    plan = dsh.plan(ns())
    assert plan["version"] == dsh.VERSION and plan["profile"] == "web" and plan["web_ui"] == dsh.WEB_URL
    assert plan["telemetry"]["switchable"] is False and plan["repository"] == dsh.REPOSITORY
    assert dsh.plan(ns(args=["headless", "x"]))["web_ui"] is None
    assert dsh.plan(ns(args=["--profile", "acp"]))["profile"] == "acp"


def test_the_launcher_knows_dsh():
    assert s.DANGER_ENV["dsh"] == {"DSH_PERMISSION_MODE": dsh.DANGER_MODE} and s.DANGER_ARGS["dsh"] == []
    assert s.AUTH_TARGETS["dsh"] == ".dsh/.credentials.yaml" and "dsh" in s.NOT_IN_TEAM
    assert s.AGENT_EFFORTS["dsh"] == () and "dsh" in s.AGENT_PROGRAMS
    assert llm.DSH_PATCH == dsh.PATCH and llm.DSH_ROUTE == dsh.LLM_ROUTE and llm.KEY_VARIABLE == dsh.KEY_VARIABLE
    assert agent_mcp.DSH_PATCH == dsh.PATCH


def test_the_hosts_dsh_home(tmp_path, monkeypatch):
    assert s.auth_sources("dsh") == [(tmp_path / "home/.dsh/.credentials.yaml", ".dsh/.credentials.yaml")]
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "d"))
    assert s.auth_sources("dsh")[0][0] == tmp_path / "d/.credentials.yaml"
    assert not s.merge_refreshed("dsh", {"a": 1}, {"a": 2}), "its store is YAML: never merged back"


# --- the image ----------------------------------------------------------------------------------------------------

def test_the_containerfile_builds_exactly_the_reviewed_release():
    text = CONTAINERFILE.read_text()
    assert f"ARG DSH_VERSION={dsh.VERSION}" in text and "COPY Containerfile.dsh.lock" in text
    assert "npm ci --ignore-scripts" in text and "npm install" not in text and "pnpm" not in text.split("ARG")[-1]
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert "ENV DSH_TELEMETRY_DISABLED=1" in text and "mkdir -p -m 755 /etc/dsh" in text


def test_the_lock_holds_the_reviewed_package_with_every_integrity():
    lock = json.loads(LOCK.read_text())
    assert lock_script.check(lock, "dsh", dsh.PACKAGE, dsh.VERSION) > 600
    entry = lock["packages"][f"node_modules/{dsh.PACKAGE}"]
    assert entry["integrity"] == dsh.INTEGRITY
    assert all(entry.get("resolved", "").startswith("https://registry.npmjs.org/")
               for path, entry in lock["packages"].items() if path), "only npm's registry"
    ours = [path for path in lock["packages"] if path.startswith("node_modules/@deepseek-ai/dsh-")]
    assert ours and all(lock["packages"][path]["version"] == dsh.VERSION for path in ours
                        if lock["packages"][path]["version"].startswith("0.2.")), "its own plugins at its version"


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


def test_build_dsh_takes_the_reviewed_release_only(engine):
    result = build("dsh")
    assert result.returncode == 0, result.stderr
    calls = [call for call in engine() if "build" in call]
    assert calls and f"DSH_VERSION={dsh.VERSION}" in calls[0] and any(a.endswith("Containerfile.dsh") for a in calls[0])
    refused = build("--pin", "0.2.2-rc.3", "dsh")
    assert refused.returncode == 2 and "DeepSeek Harness is built at the release sandbox-ai reviewed" in refused.stderr
    assert build("dsh", "--build-arg", "DSH_VERSION=0.2.2-rc.3").returncode == 2


# --- the agent's other places ----------------------------------------------------------------------------------

def test_sessions_and_one_shots():
    assert sessions.CONTINUE_ARGS["dsh"] == [] and "headless" in sessions.ONE_SHOT_ARGS
    assert sessions.SESSION_OPTIONS["dsh"] == {"--session-id"} and "--patch" in sessions.VALUE_OPTIONS["dsh"]


def test_a_presets_first_task_runs_headless():
    assert presets.prompt_args({"prompt": "fix {what}"}, "dsh", {"what": "it"}) == ["headless", "fix it"]
    assert presets.prompt_args({"prompt": "-fix it"}, "dsh") == ["headless", " -fix it"], "never an option"


def test_kits_and_memory_for_dsh():
    assert kits.LAYOUT["dsh"] == {"skills": ".agents/skills"}
    assert kits.MEMORY_FILE["dsh"] == ("AGENTS.md", ".dsh/AGENTS.md")
    skill = (kits.BUILTIN / "dsh/skills/sandbox-ai/SKILL.md").read_text()
    assert "## DeepSeek Harness in this sandbox" in skill and dsh.TELEMETRY_HOSTS[0] in skill
    assert (kits.TEMPLATES / "dsh/AGENTS.md").is_file()


def test_dsh_as_a_helper_reads_its_task_on_standard_input():
    assert agent_mcp.command("dsh", "-rf x") == ["dsh", "headless", "--patch", dsh.PATCH, "-"]
    assert "dsh" in agent_mcp.STDIN_PROMPT and "-rf x" not in agent_mcp.command("dsh", "-rf x", resume=True)


def test_the_review_names_the_release_and_the_official_repository():
    review = (ROOT / "docs/DSH_REVIEW.md").read_text()
    assert dsh.VERSION in review and dsh.COMMIT in review and dsh.INTEGRITY in review
    assert "deepseek-ai/deepseek-harness" in review and "DeepSeek-TUI" in review and "## Go / No-Go" in review
    assert "### Telemetry: always off, never switchable" in review
    assert all(host in review for host in dsh.TELEMETRY_HOSTS) and all(name in review for name in dsh.TELEMETRY_ENV)
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "DeepSeek Harness" in text and dsh.TELEMETRY_HOSTS[0] in text, readme
