"""Rules for the agents' own tools and kit hooks for the newer agents: Qwen Code, Hermes, Pi, OpenCode 2, DeepSeek
Harness, Letta Code and Goose get the same rule engine and question flow as Claude Code (the supervisor's HookServer
behind `runtime.py --internal-hook AGENT EVENT`), each through its own hook, plugin or extension API in the reviewed
release; OpenHands and nanobot have no mechanism a start can install, so a start with such rules stays refused, with
what is missing named.

The hook client and server are the real code on real sockets; each agent's message is the shape its reviewed release
sends (its source at the pinned commit: docs/*_REVIEW.md); the JavaScript plugins and extensions run under node with
the agent's API faked as its source defines it."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime_tool_rules_agents_test", "runtime.py")
s = load("agent_sandbox_tool_rules_agents_test", "agent_sandbox.py")
k = load("kits_tool_rules_agents_test", "kits.py")

RULES = {"version": 1,
         "untrusted": [{"id": "search", "agent": "*", "tool": ["WebSearch", "WebFetch"]}],
         "tool": [
             {"id": "no-curl", "tool": "Bash", "args": {"command": {"pattern": r".*\bcurl\b.*"}}, "decision": "deny"},
             {"id": "writes-in-work", "tool": ["Write", "Edit", "MultiEdit", "apply_patch"],
              "args": {"paths": {"not": {"items": {"path_glob": ["/work/**", "/tmp/**"]}}}}, "decision": "deny"},
             {"id": "fetch-docs", "tool": "WebFetch", "args": {"url": {"glob": "https://docs.example/*"}},
              "decision": "allow"},
             {"id": "no-pi-reads", "agent": "pi", "tool": "Read", "args": {"file_path": {"glob": "/secret/*"}},
              "decision": "deny"}]}
# The agents with rules for their own tools (runtime.HOOK_AGENTS) and the two without a mechanism.
NEW_AGENTS = ("qwen", "hermes", "pi", "opencode2", "dsh", "letta", "goose")
NO_MECHANISM = ("openhands", "nanobot")


class Broker:
    def __init__(self, decision="deny"):
        self.asked, self.decision = [], decision

    def ask(self, kind, details, wait=True):
        self.asked.append((kind, details))
        return {"decision": self.decision, "scope": "once", "reason": "answered"}


@pytest.fixture
def served(tmp_path):
    directory = Path(tempfile.mkdtemp(prefix="hooks-", dir="/tmp"))
    broker, log, stop = Broker(), tmp_path / "tools.jsonl", threading.Event()
    thread = rt.start_hook_server({"agent": "qwen", "rules": [{"name": "rules.json", **RULES}]}, broker, rt.Taint(),
                                  [], path=directory / "decide.sock", log=log, stop=stop)
    yield {"path": directory / "decide.sock", "broker": broker, "log": log}
    stop.set()
    thread.join(5)
    shutil.rmtree(directory, ignore_errors=True)


def run_hook(served, agent, event, message):
    stderr = io.StringIO()
    status = rt.hook_client(agent, event, stdin=io.BytesIO(json.dumps(message).encode()), stderr=stderr,
                            socket_path=served["path"], timeout=20)
    return status, stderr.getvalue()


def logged(served):
    return [json.loads(line) for line in served["log"].read_text().splitlines()]


# Each agent's own call of the same four actions, as its reviewed release hands them to a hook: (Bash curl,
# a Write outside /work, a WebFetch of the docs, an Edit inside /work).
CALLS = {
    # Qwen Code 0.26.4: Claude Code's hook input, its own tool ids (packages/core/src/tools/tool-names.ts).
    "qwen": [{"tool_name": "run_shell_command", "tool_input": {"command": "curl x"}, "cwd": "/work"},
             {"tool_name": "write_file", "tool_input": {"file_path": "/etc/x", "content": "y"}, "cwd": "/work"},
             {"tool_name": "web_fetch", "tool_input": {"url": "https://docs.example/a", "prompt": "p"}},
             {"tool_name": "edit", "tool_input": {"file_path": "a.py", "old_string": "a", "new_string": "b"},
              "cwd": "/work"}],
    # Hermes v0.22.5: its shell hooks' stdin (agent/shell_hooks.py: tool_name, tool_input, cwd, extra).
    "hermes": [{"hook_event_name": "pre_tool_call", "tool_name": "terminal", "tool_input": {"command": "curl x"},
                "cwd": "/work", "extra": {"tool_call_id": "c1"}},
               {"tool_name": "write_file", "tool_input": {"path": "/etc/x", "content": "y"}, "cwd": "/work"},
               {"tool_name": "web_extract", "tool_input": {"urls": ["https://docs.example/a"]}, "cwd": "/work"},
               {"tool_name": "patch", "tool_input": {"mode": "replace", "path": "a.py", "old_string": "a",
                                                     "new_string": "b"}, "cwd": "/work"}],
    # Pi 1.4.0, OpenCode 2.2.5 and DSH 0.2.1-rc.5: sandbox-ai's own extension or plugin sends {tool, args, cwd}.
    "pi": [{"tool": "bash", "args": {"command": "curl x"}, "cwd": "/work"},
           {"tool": "write", "args": {"path": "/etc/x", "content": "y"}, "cwd": "/work"},
           {"tool": "webfetch", "args": {"url": "https://docs.example/a"}, "cwd": "/work"},
           {"tool": "edit", "args": {"path": "a.py", "edits": [{"oldText": "a", "newText": "b"}]}, "cwd": "/work"}],
    "opencode2": [{"tool": "shell", "args": {"command": "curl x"}, "sessionID": "o", "callID": "c", "cwd": "/work"},
                  {"tool": "write", "args": {"path": "/etc/x", "content": "y"}, "cwd": "/work"},
                  {"tool": "webfetch", "args": {"url": "https://docs.example/a"}, "cwd": "/work"},
                  {"tool": "edit", "args": {"path": "a.py", "oldString": "a", "newString": "b"}, "cwd": "/work"}],
    "dsh": [{"tool": "bash", "args": {"command": "curl x"}, "cwd": "/work"},
            {"tool": "write", "args": {"file_path": "/etc/x", "content": "y"}, "cwd": "/work"},
            {"tool": "web_fetch", "args": {"url": "https://docs.example/a"}, "cwd": "/work"},
            {"tool": "edit", "args": {"file_path": "a.py", "old_string": "a", "new_string": "b"}, "cwd": "/work"}],
    # Letta Code 0.34.36: Claude Code's hook input and tool names (src/hooks/types.ts, src/tools/schemas).
    "letta": [{"event_type": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "curl x"},
               "working_directory": "/work"},
              {"tool_name": "Write", "tool_input": {"file_path": "/etc/x", "content": "y"},
               "working_directory": "/work"},
              {"tool_name": "WebFetch", "tool_input": {"url": "https://docs.example/a"}},
              {"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "a", "new_string": "b"},
               "working_directory": "/work"}],
    # Goose 1.57.0: its plugin hooks' context (crates/goose/src/hooks/mod.rs: HookContext), extension__tool names.
    "goose": [{"event": "PreToolUse", "session_id": "g", "tool_name": "developer__shell",
               "tool_input": {"command": "curl x"}, "working_dir": "/work", "tool_call_id": "c1"},
              {"tool_name": "developer__write", "tool_input": {"path": "/etc/x", "content": "y"},
               "working_dir": "/work"},
              # Goose 1.57.0 has no web fetch of its own (MCP extensions only): its tree view instead.
              {"tool_name": "developer__tree", "tool_input": {"path": "/work"}, "working_dir": "/work"},
              {"tool_name": "developer__edit", "tool_input": {"path": "a.py", "before": "a", "after": "b"},
               "working_dir": "/work"}],
}


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_every_agents_calls_meet_the_same_rules(served, agent):
    """One ruleset, written in Claude Code's names, holds for each agent's own names and arguments: curl refused,
    a write outside /work refused (its "paths" made absolute against the call's directory), the docs fetched, an
    edit in /work allowed; each logged under the common name with the agent's own."""
    curl, write, fetch, edit = CALLS[agent]
    status, err = run_hook(served, agent, "PreToolUse", curl)
    assert status == 2 and err.startswith("sandbox-ai: ") and "no-curl" in err
    status, err = run_hook(served, agent, "PreToolUse", write)
    assert status == 2 and "writes-in-work" in err
    assert run_hook(served, agent, "PreToolUse", fetch) == (0, "")
    assert run_hook(served, agent, "PreToolUse", edit) == (0, "")
    assert run_hook(served, agent, "PostToolUse", {**edit, "tool_response": "done"})[0] == 0
    records = logged(served)
    third = "LS" if agent == "goose" else "WebFetch"
    assert [(r["agent"], r["tool"], r.get("decision")) for r in records] == [
        (agent, "Bash", "deny"), (agent, "Write", "deny"), (agent, third, "allow"), (agent, "Edit", "allow"),
        (agent, "Edit", None)]


def test_a_rule_can_name_one_of_the_newer_agents(served):
    """"agent": "pi" holds for Pi alone: Pi's read of /secret is refused, Qwen Code's same read is not."""
    status, err = run_hook(served, "pi", "PreToolUse", {"tool": "read", "args": {"path": "/secret/k"}, "cwd": "/w"})
    assert status == 2 and "no-pi-reads" in err
    assert run_hook(served, "qwen", "PreToolUse", {"tool_name": "read_file",
                                                   "tool_input": {"file_path": "/secret/k"}})[0] == 0
    for agent in (*NEW_AGENTS, *NO_MECHANISM, "claude", "codex", "opencode", "*"):
        rt.Ruleset([{"name": "r", "version": 1, "tool": [{"id": "x", "agent": agent, "tool": "Bash",
                                                          "decision": "deny"}]}])
    with pytest.raises(rt.RuleError, match='"agent"'):
        rt.Ruleset([{"name": "r", "version": 1, "tool": [{"id": "x", "agent": "aider", "tool": "Bash",
                                                          "decision": "deny"}]}])


@pytest.mark.parametrize("agent", NO_MECHANISM)
def test_agents_without_a_mechanism_are_refused_by_the_hook_too(served, agent):
    """No hook of OpenHands or nanobot reaches the supervisor (their starts with tool rules are refused); a message
    that says it comes from one is refused, never let through."""
    status, err = run_hook(served, agent, "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert status == 2 and err.startswith("sandbox-ai: ")


def test_hermes_patch_in_patch_mode_is_apply_patch(served):
    """Hermes' patch tool in its V4A mode is a patch like Codex's: every file it names counts."""
    patch = "*** Begin Patch\n*** Update File: /etc/passwd\n@@\n-a\n+b\n*** End Patch\n"
    status, err = run_hook(served, "hermes", "PreToolUse", {"tool_name": "patch", "tool_input": {
        "mode": "patch", "patch": patch}, "cwd": "/work"})
    assert status == 2 and "writes-in-work" in err and logged(served)[-1]["tool"] == "apply_patch"


def test_the_agents_own_paths_never_count(served):
    """An agent's own "paths" argument is dropped as for Claude Code: the files the call names decide."""
    status, _ = run_hook(served, "qwen", "PreToolUse", {"tool_name": "write_file", "tool_input": {
        "file_path": "/etc/x", "content": "y", "paths": ["/work/a"]}, "cwd": "/work"})
    assert status == 2


@pytest.mark.parametrize("agent, call", [
    ("pi", {"tool": "write", "args": {"path": "/etc/x", "file_path": "/work/ok", "content": "y"}, "cwd": "/work"}),
    ("pi", {"tool": "read", "args": {"path": "/secret/k", "file_path": "/work/readme"}, "cwd": "/work"}),
    ("opencode2", {"tool": "write", "args": {"path": "/etc/x", "file_path": "/work/ok", "content": "y"},
                   "cwd": "/work"}),
    ("opencode", {"tool": "write", "args": {"filePath": "/etc/x", "file_path": "/work/ok", "content": "y"},
                  "cwd": "/work"}),
    ("hermes", {"tool_name": "write_file", "tool_input": {"path": "/etc/x", "file_path": "/work/ok", "content": "y"},
                "cwd": "/work"}),
    ("goose", {"tool_name": "developer__write", "tool_input": {"path": "/etc/x", "file_path": "/work/ok",
                                                               "content": "y"}, "working_dir": "/work"}),
    ("letta", {"tool_name": "ExecCommand", "tool_input": {"cmd": "curl x", "command": "ls"},
               "working_directory": "/work"}),
    # (the other order too: whichever came last used to be the one the rules saw)
    ("pi", {"tool": "write", "args": {"file_path": "/work/ok", "path": "/etc/x", "content": "y"}, "cwd": "/work"}),
])
def test_an_argument_given_under_both_names_is_refused(served, agent, call):
    """Regression (security review 1): an agent's argument is renamed to Claude Code's for the rules (Pi's path is
    file_path). A call that also carried Claude Code's name with another value - which the agent's tool ignores -
    had that harmless value checked instead: `write {path: /etc/x, file_path: /work/ok}` passed the rule that keeps
    writes in /work, and Pi wrote /etc/x. Such a call cannot be judged and is refused, never let through."""
    status, err = run_hook(served, agent, "PreToolUse", call)
    assert status == 2 and "twice" in err, err
    assert not served["log"].exists() or not any(r.get("decision") == "allow" for r in logged(served))


def test_an_argument_given_under_both_names_alike_is_one_argument(served):
    status, err = run_hook(served, "pi", "PreToolUse", {"tool": "write", "args": {
        "path": "/work/a", "file_path": "/work/a", "content": "y"}, "cwd": "/work"})
    assert (status, err) == (0, "")
    assert rt.agent_call("pi", "write", {"path": True, "file_path": True}) == ("Write", {"file_path": True})
    with pytest.raises(ValueError, match="twice"):  # (true is not 1: as the rules compare)
        rt.agent_call("pi", "write", {"path": True, "file_path": 1})


# --- the launcher: each agent's hooks, through its own API ------------------------------------------------

@pytest.fixture
def with_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(RULES))
    return path


def started(agent, rules, *more):
    a = s.parse_args([agent, "--rules", str(rules), *more])
    assert s.hooks_active(a) and s.managed_hooks(a)
    return a


def rule_command(agent, event):
    return f"/usr/bin/python3 -I /sandbox/runtime.py --internal-hook {agent} {event}"


def test_qwen_gets_its_hooks_in_its_system_settings(with_rules):
    """Qwen Code's system settings (read-only, above the user's and the project's) carry the rule hooks, sequential
    (Qwen Code then runs the project's and the user's first, and their rewrites are what the rules check); hooks
    cannot be switched off there; nothing in the session directory besides."""
    a = started("qwen", with_rules)
    settings = json.loads(s.agent_files(a)["qwen/settings.json"])
    assert settings["disableAllHooks"] is False
    for event in ("PreToolUse", "PostToolUse"):
        assert settings["hooks"][event] == [{"matcher": "*", "sequential": True, "hooks": [{
            "type": "command", "command": rule_command("qwen", event), "timeout": s.HOOK_AGENT_TIMEOUT}]}]
    assert s.hook_files(a) == {}  # nothing beside the system settings (no OpenCode plugin)
    assert s.hook_mount(a, Path("/s")) == (Path("/s/qwen"), "/etc/qwen-code")
    assert s.hooks_config(a)["agent"] == "qwen"
    without = json.loads(s.agent_files(s.parse_args(["qwen"]))["qwen/settings.json"])
    assert "hooks" not in without and "disableAllHooks" not in without


def test_qwen_refuses_what_turns_its_hooks_off(with_rules):
    """--bare and --safe-mode load no hook at all (Qwen Code 0.26.4's Config.getDisableAllHooks): refused while the
    rules hold, fine without them."""
    qwen = s.load_qwen_module()
    for args in (["--bare"], ["--safe-mode"], ["-p", "x", "--bare"]):
        with pytest.raises(ValueError, match="rules for Qwen Code's own tools"):
            qwen.check(s.parse_args(["qwen", "--rules", str(with_rules), "--", *args]), hooks_active=True)
        qwen.check(s.parse_args(["qwen", "--", *args]), hooks_active=False)
    qwen.check(s.parse_args(["qwen", "--rules", str(with_rules)]), hooks_active=True)


# --- kit hooks: which agents take them, in which form -----------------------------------------------------

def hooks_folder(tmp_path, files):
    folder = tmp_path / "hooks"
    folder.mkdir(parents=True)
    for name, text in files.items():
        (folder / name).write_text(text)
        (folder / name).chmod(0o755)
    return folder


HOOKS_JSON = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                      "command": "${HOOK_DIR}/check.sh"}]}]}}


@pytest.mark.parametrize("agent", ("qwen", "letta", "goose", "hermes"))
def test_json_agents_take_a_hooks_json(tmp_path, agent):
    folder = hooks_folder(tmp_path, {"hooks.json": json.dumps(HOOKS_JSON), "check.sh": "#!/bin/sh\n"})
    found = k.read_hooks(agent, folder)
    assert found["errors"] == [] and found["sha256"]
    assert found["hooks"] == [{"event": "PreToolUse", "matcher": "Bash", "command": "${HOOK_DIR}/check.sh",
                               "timeout": None, "files": ["check.sh"]}]


def test_hermes_refuses_an_event_it_has_no_shell_hook_for(tmp_path):
    document = {"hooks": {"Notification": [{"hooks": [{"type": "command", "command": "x"}]}]}}
    found = k.read_hooks("hermes", hooks_folder(tmp_path, {"hooks.json": json.dumps(document)}))
    assert found["sha256"] is None and "no event Hermes runs shell hooks for" in found["errors"][0]
    assert k.read_hooks("qwen", tmp_path / "hooks")["errors"] == []  # Qwen Code has Notification


@pytest.mark.parametrize("agent", ("opencode2", "pi"))
def test_module_agents_take_javascript_modules(tmp_path, agent):
    folder = hooks_folder(tmp_path, {"guard.mjs": "export default {}\n", "lib.txt": "x"})
    found = k.read_hooks(agent, folder)
    assert found["errors"] == [] and found["plugins"] == ["guard.mjs"]
    typescript = k.read_hooks(agent, hooks_folder(tmp_path / "ts", {"guard.ts": "export default {}\n"}))
    assert typescript["errors"] == ["no plugin module (.js, .mjs) at the top of the folder"]


@pytest.mark.parametrize("agent,missing", [("dsh", "dsh-hooks-claude-code"), ("openhands", ".openhands/hooks.json"),
                                           ("nanobot", "AgentHook")])
def test_agents_without_kit_hooks_say_what_is_missing(tmp_path, agent, missing):
    found = k.read_hooks(agent, hooks_folder(tmp_path, {"hooks.json": json.dumps(HOOKS_JSON)}))
    assert found["sha256"] is None and missing in found["errors"][0]
    assert "yet" not in found["errors"][0]


# --- Hermes -------------------------------------------------------------------------------------------------

def hermes_config(a):
    return json.loads(s.hermes_files(a)["hermes/config.yaml"])


def test_hermes_gets_shell_hooks_in_its_managed_configuration(with_rules):
    """Hermes' managed configuration (read-only, laid over the user's leaf by leaf) holds the rule hooks as shell
    hooks, failing closed, consented to there; every other shell-hook event is empty there, so the user's own shell
    hooks do not run while these hold (as Claude Code's allowManagedHooksOnly); the guard's marker is written."""
    a = started("hermes", with_rules)
    config = hermes_config(a)
    hermes = s.load_hermes_module()
    assert config["hooks"]["pre_tool_call"] == [{"command": rule_command("hermes", "PreToolUse"),
                                                 "timeout": s.HOOK_AGENT_TIMEOUT, "fail_closed": True}]
    assert config["hooks"]["post_tool_call"] == [{"command": rule_command("hermes", "PostToolUse"),
                                                  "timeout": s.HOOK_AGENT_TIMEOUT}]
    assert set(config["hooks"]) == set(hermes.SHELL_HOOK_EVENTS)
    assert all(entries == [] for event, entries in config["hooks"].items()
               if event not in ("pre_tool_call", "post_tool_call"))
    assert config["hooks_auto_accept"] is True
    assert json.loads(s.hermes_files(a)[f"hermes/{hermes.HOOKS_MARKER_NAME}"]) == {"rules": True}
    assert s.hook_files(a) == {}
    plain = hermes_config(s.parse_args(["hermes"]))
    assert "hooks" not in plain and "hooks_auto_accept" not in plain
    assert hermes.HOOKS_MARKER_NAME not in "".join(s.hermes_files(s.parse_args(["hermes"])))


def test_hermes_kit_hooks_become_its_shell_hooks():
    hermes = s.load_hermes_module()
    groups = {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "rule", "timeout": 120}]},
                             {"matcher": "terminal", "hooks": [{"type": "command", "command": "kit", "timeout": 999}]}],
              "SessionStart": [{"hooks": [{"type": "command", "command": "hello"}]}]}
    hooks = hermes.shell_hooks(groups)
    assert hooks["pre_tool_call"] == [{"command": "rule", "timeout": 120, "fail_closed": True},
                                      {"command": "kit", "timeout": 300, "fail_closed": True, "matcher": "terminal"}]
    assert hooks["on_session_start"] == [{"command": "hello", "timeout": 60}]


def test_hermes_refuses_safe_mode_while_rules_hold(with_rules):
    hermes = s.load_hermes_module()
    with pytest.raises(ValueError, match="registers no shell hook"):
        hermes.check(s.parse_args(["hermes", "--rules", str(with_rules), "--", "--safe-mode"]), hooks_active=True)
    hermes.check(s.parse_args(["hermes", "--", "--safe-mode"]), hooks_active=False)
    hermes.check(s.parse_args(["hermes", "--rules", str(with_rules)]), hooks_active=True)


def test_the_hermes_guard_keeps_the_hooks_in_every_hermes(tmp_path, monkeypatch):
    """With the marker (rules hold): a Hermes command with --safe-mode is refused before Hermes runs, and
    HERMES_SAFE_MODE is dropped from every process of Hermes' Python; without it both are Hermes' own."""
    guard = s.load_hermes_module().load_guard()
    marker = tmp_path / "sandbox-ai-hooks.json"
    monkeypatch.setattr(guard, "HOOKS_MARKER", str(marker))
    assert guard.hooks_refused(["chat", "--safe-mode"]) is None
    marker.write_text('{"rules": true}')
    assert "registers no shell hook" in guard.hooks_refused(["chat", "--safe-mode"])
    assert guard.hooks_refused(["chat", "--", "--safe-mode"]) is None
    environ = {"HERMES_SAFE_MODE": "1"}
    guard.pin_environment(environ)
    assert "HERMES_SAFE_MODE" not in environ


def test_the_hermes_guard_asks_the_rules_again_for_arguments_a_hook_changed(tmp_path, monkeypatch):
    """Hermes runs all pre_tool_call hooks on the same arguments and then applies a plugin's or shell hook's
    "modify": the guard asks the rules again for the arguments as they will run, and a refusal blocks the call."""
    guard = s.load_hermes_module().load_guard()
    marker = tmp_path / "sandbox-ai-hooks.json"
    marker.write_text('{"rules": true}')
    monkeypatch.setattr(guard, "HOOKS_MARKER", str(marker))
    asked = []

    def run(command, input=None, **kw):
        asked.append(json.loads(input))
        refused = "curl" in json.dumps(asked[-1])
        return type("Done", (), {"returncode": 2 if refused else 0,
                                 "stderr": b"sandbox-ai: no curl\n" if refused else b""})()
    monkeypatch.setattr(guard.subprocess, "run", run)
    module = type(sys)("hermes_cli.plugins")
    module._dispatch_pre_tool_call_hooks = lambda tool, args, **kw: (None, {"command": "curl x"})
    guard.patch_plugins(module)
    assert module._dispatch_pre_tool_call_hooks("terminal", {"command": "ls"}) == ("sandbox-ai: no curl",
                                                                                   {"command": "curl x"})
    assert asked == [{"tool_name": "terminal", "tool_input": {"command": "curl x"}, "cwd": asked[0]["cwd"]}]
    module._dispatch_pre_tool_call_hooks = lambda tool, args, **kw: (None, None)  # unchanged: never asked again
    guard.patch_plugins(module)
    assert module._dispatch_pre_tool_call_hooks("terminal", {"command": "ls"}) == (None, None) and len(asked) == 1


# --- the JavaScript hosts: Pi's extension, OpenCode 2's plugin, DSH's Cordis plugin -----------------------

# A stand-in for the hook client (python3 runtime.py --internal-hook AGENT EVENT): logs the call, refuses curl and
# crashes on "crash", like test_hooks.NODE_HOOK.
STAND_IN = r"""
import sys, json
message = json.load(sys.stdin)
with open(sys.argv[1] + ".log", "a") as log:
    log.write(json.dumps([sys.argv[-1], message]) + "\n")
if sys.argv[-1] == "PreToolUse" and "curl" in json.dumps(message):
    print("sandbox-ai: no curl", file=sys.stderr)
    sys.exit(2)
if sys.argv[-1] == "PreToolUse" and "crash" in json.dumps(message):
    import os, signal
    os.kill(os.getpid(), signal.SIGKILL)
"""


def stand_in(tmp_path, agent, script, command_line):
    """SCRIPT with its hook command (COMMAND_LINE, as written there) pointed at the stand-in; (path, calls log)."""
    hook = tmp_path / "hook.py"
    hook.write_text(STAND_IN)
    assert command_line in script
    replaced = script.replace(command_line, json.dumps([sys.executable, "-I", str(hook), str(tmp_path / "calls"),
                                                        agent]))
    return replaced, tmp_path / "calls.log"


def hook_calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
PI_HOOK = '["/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-hook", "pi"]'
# Pi 1.4.0's extension API as the extension uses it (packages/coding-agent/src/core/extensions/runner.ts):
# handlers per event in registration order; a tool_call result with block stops the call; a handler that throws
# blocks it too (agent-session.ts: "Extension failed, blocking execution").
PI_DRIVER = r"""
const [extension, kit] = process.argv.slice(2);
const handlers = {};
const pi = { on: (event, handler) => { (handlers[event] ??= []).push(handler); return () => {}; },
             registerProvider: () => {}, registerMcpServer: () => {} };
await (await import(extension)).default(pi);
async function call(toolName, input) {
  const event = { type: "tool_call", toolName, toolCallId: "c-" + JSON.stringify(input), input };
  for (const handler of handlers.tool_call ?? []) {
    let result;
    try { result = await handler(event, { cwd: "/work" }); } catch (error) { return "blocked: threw " + error.name; }
    if (result?.block) return "blocked: " + result.reason;
  }
  return "ran " + JSON.stringify(event.input);
}
const results = [await call("bash", { command: "ls" }), await call("bash", { command: "curl x" }),
                 await call("bash", { command: "crash" }), await call("write", { path: "a", content: "rewrite me" })];
for (const handler of handlers.tool_result ?? []) {
  await handler({ type: "tool_result", toolName: "bash", toolCallId: "c1", input: {}, content: [{ type: "text", text: "ok" }] }, { cwd: "/work" });
}
console.log(JSON.stringify(results));
"""
# A kit's extension that rewrites a write into curl, and a user's extension loaded after sandbox-ai's that tries the
# same on a checked call.
PI_KIT = r"""
export default function kit(pi) {
  pi.on("tool_call", (event) => {
    if (event.input.content === "rewrite me") event.input.content = "curl evil";
  });
}
"""


@needs_node
@pytest.mark.parametrize("with_kit", (False, True))
def test_pi_extension_checks_freezes_and_rechecks(tmp_path, with_kit):
    """Pi's extension as Pi loads it, the hook pointed at a stand-in: an allowed call runs, a refused one and a hook
    that dies block it; the checked input is frozen, so an extension after it cannot change it (its write throws:
    blocked); a kit's extension loaded after the check that changes a call is asked again, and refused."""
    pi = s.load_pi_module()
    config = tmp_path / "sandbox-ai.json"
    modules = [str(tmp_path / "kit.mjs")] if with_kit else []
    (tmp_path / "kit.mjs").write_text(PI_KIT)
    config.write_text(json.dumps(pi.extension_config(s.parse_args(["pi"]), hooks={"rules": True, "modules": modules})))
    script, log = stand_in(tmp_path, "pi", pi.EXTENSION_SCRIPT.replace("/etc/pi/sandbox-ai.json", str(config)),
                           PI_HOOK)
    extension = tmp_path / "extension.mjs"
    extension.write_text(script)
    late = tmp_path / "late.mjs"  # a user's extension after sandbox-ai's (the driver runs it last)
    late.write_text(PI_KIT)
    driver = tmp_path / "driver.mjs"
    driver.write_text(PI_DRIVER.replace("const results", f"(await import({json.dumps(str(late))})).default(pi);\n"
                                                           "const results"))
    result = subprocess.run(["node", str(driver), str(extension)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    ls, curl, crash, rewritten = json.loads(result.stdout)
    assert ls == 'ran {"command":"ls"}' and curl == "blocked: sandbox-ai: no curl"
    assert crash.startswith("blocked: sandbox-ai: the tool call is refused")
    calls = hook_calls(log)
    if with_kit:  # the kit rewrote it after the check: asked again with the rewrite, refused
        assert rewritten == "blocked: sandbox-ai: no curl"
        assert [c[1]["args"] for c in calls if c[0] == "PreToolUse"][-2:] == [
            {"path": "a", "content": "rewrite me"}, {"path": "a", "content": "curl evil"}]
    else:  # the late extension's change of the frozen input throws: blocked, never run with it
        assert rewritten == "blocked: threw TypeError"
    assert calls[0] == ["PreToolUse", {"tool": "bash", "args": {"command": "ls"}, "callID": 'c-{"command":"ls"}',
                                       "cwd": "/work"}]
    assert calls[-1][0] == "PostToolUse" and calls[-1][1]["tool"] == "bash" and calls[-1][1]["result_bytes"] > 0


def test_pi_gets_the_rules_in_its_extensions_settings(with_rules):
    a = started("pi", with_rules)
    files = s.agent_files(a)
    assert json.loads(files["pi/sandbox-ai.json"])["hooks"] == {"rules": True, "modules": []}
    assert "--internal-hook" in files["pi/sandbox-ai.js"] and s.hook_files(a) == {}
    assert "hooks" not in json.loads(s.agent_files(s.parse_args(["pi"]))["pi/sandbox-ai.json"])
    s.load_pi_module().check(a, hooks_active=True)  # no refusal


OPENCODE2_HOOK = '["/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-hook", "opencode2"]'
# OpenCode 2.2.5's plugin host as the plugin uses it (packages/core/src/plugin/hooks.ts, tool.ts): execute.before
# callbacks in registration order on one event object; a rejection refuses the call; the tool then runs with
# event.tool and event.input as the callbacks left them.
OPENCODE2_DRIVER = r"""
const [plugin, late] = process.argv.slice(2);
const callbacks = {};
const ctx = { tool: { hook: async (name, callback) => { (callbacks[name] ??= []).push(callback); return { dispose: async () => {} }; } } };
await (await import(plugin)).default.setup(ctx);
if (late) await (await import(late)).default.setup(ctx);
async function call(tool, input) {
  const event = { tool, sessionID: "s", agent: "build", messageID: "m", id: "c-" + JSON.stringify(input), input };
  for (const callback of callbacks["execute.before"] ?? []) {
    try { await callback(event); } catch (error) { return "refused: " + error.message.split("\n")[0]; }
  }
  for (const callback of callbacks["execute.after"] ?? []) {
    await callback({ ...event, status: "completed", result: { output: "done", content: [] } });
  }
  return "ran " + event.tool + " " + JSON.stringify(event.input);
}
console.log(JSON.stringify([await call("shell", { command: "ls" }), await call("shell", { command: "curl x" }),
                            await call("write", { path: "a", content: "rewrite me" })]));
"""
OPENCODE2_KIT = r"""
export default { id: "kit", setup: async (ctx) => {
  await ctx.tool.hook("execute.before", async (event) => {
    if (event.input.content === "rewrite me") event.input = { ...event.input, content: "curl evil" };
  });
} };
"""


@needs_node
@pytest.mark.parametrize("with_kit", (False, True))
def test_opencode2_plugin_checks_freezes_and_rechecks(tmp_path, with_kit):
    """sandbox-ai's V2 plugin as OpenCode 2 loads it: allowed runs, curl refused; the checked event is frozen, so a
    plugin loaded after it cannot swap the input (it throws: refused); a kit's plugin hosted after the check that
    changes a call is asked again, and refused; every finished call recorded."""
    opencode2 = s.load_opencode2_module()
    config = tmp_path / "config.json"
    (tmp_path / "kit.mjs").write_text(OPENCODE2_KIT)
    config.write_text(json.dumps({"rules": True, "modules": [str(tmp_path / "kit.mjs")] if with_kit else []}))
    script, log = stand_in(tmp_path, "opencode2", opencode2.HOOKS_SCRIPT.replace(opencode2.HOOKS_CONFIG, str(config)),
                           OPENCODE2_HOOK)
    plugin, late, driver = tmp_path / "server.mjs", tmp_path / "late.mjs", tmp_path / "driver.mjs"
    plugin.write_text(script)
    late.write_text(OPENCODE2_KIT)
    driver.write_text(OPENCODE2_DRIVER)
    result = subprocess.run(["node", str(driver), str(plugin), *([] if with_kit else [str(late)])],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    ls, curl, rewritten = json.loads(result.stdout)
    assert ls == 'ran shell {"command":"ls"}' and curl == "refused: sandbox-ai: no curl"
    if with_kit:
        assert rewritten == "refused: sandbox-ai: no curl"
    else:
        assert rewritten.startswith("refused: Cannot assign to read only property 'input'"), rewritten
    calls = hook_calls(log)
    assert calls[0] == ["PreToolUse", {"tool": "shell", "args": {"command": "ls"}, "sessionID": "s",
                                       "callID": 'c-{"command":"ls"}', "cwd": calls[0][1]["cwd"]}]
    assert ["PostToolUse"] == [c[0] for c in calls if c[1].get("callID") == 'c-{"command":"ls"}'][1:]


def test_opencode2_gets_its_hooks_plugin(with_rules):
    a = started("opencode2", with_rules)
    opencode2 = s.load_opencode2_module()
    content = json.loads(s.agent_environment(a)["OPENCODE_CONFIG_CONTENT"])
    assert content["plugins"] == [opencode2.HOOKS_PLUGIN] == ["/etc/opencode2/sandbox-ai-hooks"]
    files = s.agent_files(a)
    assert files["opencode2/sandbox-ai-hooks/server.js"] == opencode2.HOOKS_SCRIPT
    assert json.loads(files["opencode2/sandbox-ai-hooks/package.json"]) == {"type": "module"}
    assert json.loads(files["opencode2/sandbox-ai-hooks/config.json"]) == {"rules": True, "modules": []}
    assert s.hook_files(a) == {}
    assert "plugins" not in json.loads(s.agent_environment(s.parse_args(["opencode2"]))["OPENCODE_CONFIG_CONTENT"])


DSH_HOOK = '["/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-hook", "dsh"]'
# DSH 0.2.1-rc.5's tool registry as the plugin uses it (packages/core/tools/src/index.ts, prepareExecution): the
# tools/pre-execute waterfall (each listener gets next(); the innermost default is allow), then, for an allow, the
# monotonic guards on the same execution object; a deny's reason is the call's error.
DSH_DRIVER = r"""
const [plugin, shortcut] = process.argv.slice(2);
const listeners = {}, guards = [];
const ctx = { on: (name, fn) => (listeners[name] ??= []).push(fn), get: () => undefined,
              tools: { guard: (fn) => { guards.push(fn); return () => {}; } } };
if (shortcut) listeners["tools/pre-execute"] = [async () => ({ kind: "allow" })];  // a policy that answers first
(await import(plugin)).apply(ctx, {});
async function call(name, args) {
  const exec = Object.freeze({ name, arguments: Object.freeze(args), callId: "c-" + JSON.stringify(args) });
  const chain = listeners["tools/pre-execute"] ?? [];
  const run = (index) => index < chain.length ? chain[index](exec, () => run(index + 1)) : Promise.resolve({ kind: "allow" });
  const gate = await run(0);
  const reason = gate.kind === "allow" ? guards.map((guard) => guard(exec)).find((r) => r !== undefined) : gate.reason;
  if (reason !== undefined) return "refused: " + reason;
  for (const listener of listeners["tools/post-execute"] ?? []) {
    await listener(exec, { content: [{ type: "text", text: "ok" }] }, async () => ({ kind: "accept" }));
  }
  return "ran " + name;
}
console.log(JSON.stringify([await call("bash", { command: "ls" }), await call("bash", { command: "curl x" })]));
"""


@needs_node
def test_dsh_plugin_denies_and_guards(tmp_path):
    """sandbox-ai's Cordis plugin as DSH loads it: allowed runs (and is recorded), curl is denied with the rules'
    reason; a policy that answers allow before the plugin's listener gets nothing past the guard."""
    dsh = s.load_dsh_module()
    script, log = stand_in(tmp_path, "dsh", dsh.HOOKS_SCRIPT, DSH_HOOK)
    plugin, driver = tmp_path / "plugin.mjs", tmp_path / "driver.mjs"
    plugin.write_text(script)
    driver.write_text(DSH_DRIVER)
    result = subprocess.run(["node", str(driver), str(plugin)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["ran bash", "refused: sandbox-ai: no curl"]
    calls = hook_calls(log)
    assert [c[0] for c in calls] == ["PreToolUse", "PostToolUse", "PreToolUse"]
    assert calls[0][1]["args"] == {"command": "ls"} and calls[0][1]["tool"] == "bash"
    result = subprocess.run(["node", str(driver), str(plugin), "shortcut"], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    first, _ = json.loads(result.stdout)
    assert first.startswith("refused: sandbox-ai: the tool call is refused: its rules were not checked")


def test_dsh_gets_its_plugin_through_its_patch(with_rules):
    a = started("dsh", with_rules)
    dsh = s.load_dsh_module()
    files = s.agent_files(a)
    rows = json.loads(files["dsh/sandbox-ai.patch.yml"])
    assert {"id": "sandbox-ai-hooks", "name": "/etc/dsh/sandbox-ai-hooks.mjs", "config": {}} in \
        [entry for row in rows for entry in row.get("insert", [])]
    assert files["dsh/sandbox-ai-hooks.mjs"] == dsh.HOOKS_SCRIPT
    assert rows[-len(dsh.TELEMETRY_PINNED):] == dsh.TELEMETRY_PINNED  # the telemetry pins stay last
    plain = s.agent_files(s.parse_args(["dsh"]))
    assert "dsh/sandbox-ai-hooks.mjs" not in plain and "sandbox-ai-hooks" not in plain["dsh/sandbox-ai.patch.yml"]


# --- Letta Code -----------------------------------------------------------------------------------------------

def test_letta_gets_the_checked_input_back_as_its_first_rewrite(served):
    """Letta Code takes the first updatedInput of an event's hooks: the rule hook answers an allowed call with the
    very input it checked, so a user's hook after it cannot rewrite the call; a refusal writes nothing there."""
    out = io.StringIO()
    message = {"event_type": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls", "timeout": 5}}
    status = rt.hook_client("letta", "PreToolUse", stdin=io.BytesIO(json.dumps(message).encode()),
                            stderr=io.StringIO(), stdout=out, socket_path=served["path"], timeout=20)
    assert status == 0 and json.loads(out.getvalue()) == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "updatedInput": {"command": "ls", "timeout": 5}}}
    out = io.StringIO()
    message["tool_input"]["command"] = "curl x"
    status = rt.hook_client("letta", "PreToolUse", stdin=io.BytesIO(json.dumps(message).encode()),
                            stderr=io.StringIO(), stdout=out, socket_path=served["path"], timeout=20)
    assert status == 2 and out.getvalue() == ""
    out = io.StringIO()
    status = rt.hook_client("qwen", "PreToolUse", stdin=io.BytesIO(json.dumps(CALLS["qwen"][2]).encode()),
                            stderr=io.StringIO(), stdout=out, socket_path=served["path"], timeout=20)
    assert status == 0 and out.getvalue() == ""  # only Letta Code's


def test_letta_gets_its_hooks_through_its_start(with_rules):
    a = started("letta", with_rules)
    config = json.loads(s.agent_files(a)["letta/sandbox-ai-start.json"])
    (group,) = config["hooks"]["PreToolUse"]
    assert group["hooks"] == [{"type": "command", "command": rule_command("letta", "PreToolUse"),
                               "timeout": s.HOOK_AGENT_TIMEOUT * 1000}]
    assert s.hook_files(a) == {}


# --- Goose ------------------------------------------------------------------------------------------------------

tg = load("telemetry_guard_tool_rules_test", "telemetry_guard.py")


def test_goose_gets_a_plugin_with_its_hooks(with_rules):
    """sandbox-ai's plugin in Goose's read-only system directory: hooks.json in Goose's (Open Plugins) format, the
    rule hook for every tool (no matcher: Goose skips a rule whose matcher is no regular expression, such as *), a
    PreToolUse that fails blocks the call."""
    a = started("goose", with_rules)
    document = json.loads(s.agent_files(a)["goose/sandbox-ai-hooks/hooks/hooks.json"])
    assert document["hooks"]["PreToolUse"] == [{"hooks": [{"type": "command", "command": rule_command(
        "goose", "PreToolUse"), "timeout": s.HOOK_AGENT_TIMEOUT, "on_failure": "block"}]}]
    assert document["hooks"]["PostToolUse"] == [{"hooks": [{"type": "command", "command": rule_command(
        "goose", "PostToolUse"), "timeout": s.HOOK_AGENT_TIMEOUT}]}]
    assert s.load_goose_module().HOOKS_PLUGIN == tg.GOOSE_PLUGIN  # the guard looks where the launcher writes
    assert not any("sandbox-ai-hooks" in name for name in s.agent_files(s.parse_args(["goose"])))
    kit = s.load_goose_module().plugin_hooks({"PreToolUse": [{"matcher": "developer__shell", "hooks": [
        {"type": "command", "command": "k"}]}]})
    assert kit == {"hooks": {"PreToolUse": [{"matcher": "developer__shell", "hooks": [
        {"type": "command", "command": "k", "timeout": 30, "on_failure": "block"}]}]}}


@pytest.fixture
def goose_home(tmp_path, monkeypatch):
    plugin = tmp_path / "etc-goose/sandbox-ai-hooks"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks/hooks.json").write_text("{}")
    monkeypatch.setattr(tg, "GOOSE_PLUGIN", str(plugin))
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return {"plugin": plugin, "home": home, "work": work}


def test_the_goose_command_links_the_plugin_in(goose_home):
    home, work = goose_home["home"], goose_home["work"]
    assert tg.goose_hooks({}, home=str(home), cwd=str(work)) is None
    link = home / ".agents/plugins/sandbox-ai-hooks"
    assert link.is_symlink() and link.resolve() == goose_home["plugin"].resolve()
    assert tg.goose_hooks({}, home=str(home), cwd=str(work)) is None  # again: the link is there


@pytest.mark.parametrize("change,why", [
    (lambda h, w: (h / ".config/goose").mkdir(parents=True) or
     (h / ".config/goose/settings.json").write_text('{"disabled": ["sandbox-ai-hooks"]}'), "disables"),
    (lambda h, w: (w / ".config/goose").mkdir(parents=True) or
     (w / ".config/goose/settings.local.json").write_text('{"disabled": ["sandbox-ai-hooks"]}'), "disables"),
    (lambda h, w: (w / ".agents/plugins/sandbox-ai-hooks/hooks").mkdir(parents=True), "takes the name"),
    (lambda h, w: (h / ".agents/plugins/sandbox-ai-hooks").mkdir(parents=True), "is not sandbox-ai's"),
    (lambda h, w: (h / ".config/goose").mkdir(parents=True) or (h / ".config/goose/config.yaml").write_text(
        f"plugins:\n  {h}/.agents/plugins/sandbox-ai-hooks:\n    enabled: false\n"), "set it to true"),
    (lambda h, w: (h / ".config/goose").mkdir(parents=True) or (h / ".config/goose/config.yaml").write_text(
        f"plugins: {{'{h}/.agents/plugins/sandbox-ai-hooks': {{enabled: false}}}}\n"), "set it to true"),
])
def test_the_goose_command_refuses_a_goose_without_the_plugin(goose_home, change, why):
    """The rules hold only through the plugin: disabled in a settings.json or config.yaml, shadowed by the
    project's plugin of the same name, or its place taken - the image's goose command refuses to start Goose."""
    home, work = goose_home["home"], goose_home["work"]
    change(home, work)
    found = tg.goose_hooks({}, home=str(home), cwd=str(work))
    assert found and why in found


def test_the_goose_command_refuses_another_plugin_folder_and_lets_goose_be_without_hooks(goose_home, tmp_path):
    assert "GOOSE_PATH_ROOT" in tg.goose_hooks({"GOOSE_PATH_ROOT": "/tmp/x"}, home=str(goose_home["home"]))
    (goose_home["plugin"] / "hooks/hooks.json").unlink()  # no hooks this run: Goose as it is
    assert tg.goose_hooks({"GOOSE_PATH_ROOT": "/tmp/x"}, home=str(goose_home["home"])) is None
    started_with = []
    (goose_home["plugin"] / "hooks/hooks.json").write_text("{}")
    err = io.StringIO()
    status = tg.run("goose", ["session"], environ={"HOME": str(goose_home["home"]), "GOOSE_PATH_ROOT": "/x"},
                    execve=lambda *a: started_with.append(a), stderr=err)
    assert status == 2 and not started_with and "Goose is not started" in err.getvalue()


# --- OpenHands and nanobot: no mechanism, a precise refusal ---------------------------------------------------

@pytest.mark.parametrize("agent,missing", [
    ("openhands", "takes hooks only with each conversation"), ("nanobot", "hooks only for Python programs")])
def test_a_start_with_tool_rules_is_refused_saying_what_is_missing(with_rules, agent, missing):
    """Never started with rules it cannot hold: the refusal names what the reviewed release lacks (no "yet") and
    what still holds (the network, MCP and device rules)."""
    a = s.parse_args([agent, "--rules", str(with_rules)])
    with pytest.raises(ValueError, match=missing) as refused:
        s.agent_module(agent).check(a, hooks_active=True)
    assert "yet" not in str(refused.value) and "the network, MCP and device rules hold" in str(refused.value)
    s.agent_module(agent).check(s.parse_args([agent]), hooks_active=False)
