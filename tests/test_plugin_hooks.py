"""The hooks of Claude plugins (sandbox/kits.py, sandbox/plugins.py, the launcher): while the agent's managed hooks
are on (tool rules, kit hooks), a plugin's own hooks run only once the user approved exactly the plugin's installed
folder (`sandbox-ai plugins claude trust-hooks PLUGIN`); they join the managed configuration after the rule and
kit hooks, with ${CLAUDE_PLUGIN_ROOT} set as Claude Code sets it."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
s = fm.s


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugins = load("plugins_for_plugin_hook_tests", "sandbox/plugins.py")
k = plugins.kits()
PLUGIN = "sandcastle@sandbox-ai"
INSTALL = "/root/.claude/plugins/cache/sandbox-ai/sandcastle/1.0.0"
RULES = {"version": 1, "tool": [{"id": "no-curl", "tool": "Bash", "args": {"command": {"pattern": "(?s).*curl.*"}},
                                 "decision": "deny"}]}
HOOKS = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/scripts/check.sh", "timeout": 20}]}]}}


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home


def install(hooks=HOOKS, install_path=INSTALL, manifest=None, extra=None):
    """The plugin store with PLUGIN installed (as `claude plugin install` leaves it); returns its folder."""
    store = k.plugin_store("claude") / ".claude/plugins"
    store.mkdir(parents=True, exist_ok=True)
    (store / "known_marketplaces.json").write_text(json.dumps({"sandbox-ai": {"source": {}}}))
    (store / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {PLUGIN: [{"scope": "user", "installPath": install_path}]}}))
    root = store / "cache/sandbox-ai/sandcastle/1.0.0"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/check.sh").write_text("#!/bin/sh\necho checked >&2\n")
    (root / "scripts/check.sh").chmod(0o755)
    (root / "node_modules/.bin").mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(root / "node_modules/.bin/tool"):
        (root / "node_modules/.bin/tool").symlink_to("../tool/cli.js")
    if hooks is not None:
        (root / "hooks").mkdir(exist_ok=True)
        (root / "hooks/hooks.json").write_text(json.dumps(hooks))
    if manifest is not None:
        (root / ".claude-plugin").mkdir(exist_ok=True)
        (root / ".claude-plugin/plugin.json").write_text(json.dumps(manifest))
    for name, text in (extra or {}).items():
        (root / name).write_text(text)
    return root


def info_of():
    _, installed = k.installed()
    return k.read_plugin_hooks(PLUGIN, *k.plugin_install(PLUGIN, installed[PLUGIN]))


# --- what a plugin's hooks are --------------------------------------------------------------------------

def test_a_plugins_hooks_become_managed_hooks_with_its_root(host):
    install()
    info = info_of()
    assert info["errors"] == [] and info["sha256"] and info["declared"]
    (group,) = info["groups"]["PreToolUse"]
    command = group["hooks"][0]["command"]
    assert command == f"export CLAUDE_PLUGIN_ROOT={shlex.quote(INSTALL)}; {INSTALL}/scripts/check.sh"
    assert group["matcher"] == "Bash" and group["hooks"][0]["timeout"] == 20
    assert info["hooks"][0]["command"] == "${CLAUDE_PLUGIN_ROOT}/scripts/check.sh", "shown as the plugin wrote it"
    links = [f for f in info["files"] if "link" in f]
    assert links == [{"path": "node_modules/.bin/tool", "size": 0, "link": "../tool/cli.js", "executable": False,
                      "sha256": links[0]["sha256"]}], "a link is content, never followed"


def test_the_hash_covers_every_file_and_link_of_the_plugin(host):
    root = install()
    first = info_of()["sha256"]
    (root / "lib.js").write_text("module.exports = 1\n")  # a file no hook names: the commands may still run it
    second = info_of()["sha256"]
    (root / "node_modules/.bin/tool").unlink()
    (root / "node_modules/.bin/tool").symlink_to("/usr/bin/curl")
    third = info_of()["sha256"]
    assert len({first, second, third}) == 3


def test_hooks_declared_in_the_manifest(host):
    install(hooks=None, manifest={"name": "sandcastle", "hooks": "./more.json"},
            extra={"more.json": json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}})})
    info = info_of()
    assert info["errors"] == [] and list(info["groups"]) == ["Stop"]
    install(hooks=None, manifest={"name": "sandcastle", "hooks": "../../../../etc/passwd"})
    assert "no file of the plugin" in info_of()["errors"][0] and info_of()["sha256"] is None
    install(hooks=None, manifest={"name": "sandcastle", "hooks": {"PostToolUse": [{"hooks": [
        {"type": "prompt", "prompt": "x"}]}]}})
    assert info_of()["errors"] and info_of()["sha256"] is None, "only command hooks run as managed hooks"


def test_an_install_path_outside_the_cache_is_not_taken(host):
    install()
    for path in ("/etc", "/root/.claude/plugins/cache/../../../etc", "/root/.claude/plugins/cache", "relative/x",
                 "/root/.claude/plugins/cache/a//b"):
        assert k.plugin_install(PLUGIN, [{"installPath": path}]) is None, path
    assert k.plugin_install(PLUGIN, "not a list") is None and k.plugin_install(PLUGIN, [{"installPath": 3}]) is None


def test_a_plugin_without_hooks_is_left_alone(host):
    install(hooks=None)
    _, installed = k.installed()
    assert k.stage_plugin_hooks([PLUGIN], installed) == []
    with pytest.raises(ValueError, match="has no hooks"):
        plugins.plugin_hooks(PLUGIN)


# --- approval -------------------------------------------------------------------------------------------

def test_trust_hooks_shows_the_scripts_and_records_the_plugin(host, monkeypatch):
    root = install()
    (root / "scripts/check.sh").write_text("#!/bin/sh\necho '\x1b[31mred'\n")
    lines = []
    assert plugins.trust_hooks([PLUGIN, "--yes"], lines.append) == 0
    text = "\n".join(lines)
    assert "PreToolUse, matcher Bash: ${CLAUDE_PLUGIN_ROOT}/scripts/check.sh" in text
    assert "--- scripts/check.sh" in text and "\\x1b[31mred" in text and "\x1b" not in text, "escaped, never run"
    assert "other file(s)" in text
    _, installed = k.installed()
    (staged,) = k.stage_plugin_hooks([PLUGIN], installed)
    assert staged["trusted"] and staged["kit"] == "plugin:" + PLUGIN
    record = next(r for r in k.trust_records() if r["kit"] == "plugin:" + PLUGIN)
    assert record["agent"] == "claude"
    (root / "scripts/check.sh").write_text("#!/bin/sh\ncurl evil.example\n")  # changed after the approval
    assert not k.stage_plugin_hooks([PLUGIN], k.installed()[1])[0]["trusted"]
    assert plugins.trust_hooks([PLUGIN, "--revoke"], lines.append) == 0
    assert not any(r["kit"] == "plugin:" + PLUGIN for r in k.trust_records())


def test_trust_hooks_needs_a_yes_without_a_terminal(host, monkeypatch):
    install()
    monkeypatch.setattr(plugins.sys, "stdin", io.StringIO("y\n"))
    with pytest.raises(ValueError, match="--yes"):
        plugins.trust_hooks([PLUGIN], lambda line: None)
    with pytest.raises(ValueError, match="not in the plugin store"):
        plugins.trust_hooks(["nope@x", "--yes"], lambda line: None)
    assert plugins.cli(["claude", "trust-hooks", PLUGIN, "--yes"]) == 0


def test_a_kits_approval_of_the_same_content_does_not_approve_the_plugin(host):
    install()
    info = info_of()
    k.change_trust("claude", info, "mine")  # the same hash, recorded for a kit
    assert not k.stage_plugin_hooks([PLUGIN], k.installed()[1])[0]["trusted"]


# --- the start ------------------------------------------------------------------------------------------

def test_approved_plugin_hooks_run_after_the_rule_hooks(m, tmp_path):  # noqa: F811
    if m.agent != "claude":
        pytest.skip("Claude plugins")
    install()
    assert plugins.trust_hooks([PLUGIN, "--yes"], lambda line: None) == 0
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    run = m.run("--rules", str(rules))
    assert f"plugin {PLUGIN}: 1 approved hook(s)" in run.err
    settings = json.loads(s.hook_files(run.a)["claude-code/managed-settings.json"])
    commands = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert commands[0] == "/usr/bin/python3 -I /sandbox/runtime.py --internal-hook claude PreToolUse"
    # Wrapped like a kit's PreToolUse hook: a rewritten input goes past the rules too.
    assert commands[1].startswith("/usr/bin/python3 -I /sandbox/runtime.py --internal-kit-hook claude 20 ")
    assert shlex.split(commands[1])[-1] == f"export CLAUDE_PLUGIN_ROOT={INSTALL}; {INSTALL}/scripts/check.sh"


def test_unapproved_plugin_hooks_are_named_while_managed_hooks_are_on(m, tmp_path):  # noqa: F811
    if m.agent != "claude":
        pytest.skip("Claude plugins")
    install()
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    run = m.run("--rules", str(rules))
    assert f"sandbox-ai plugins claude trust-hooks {PLUGIN}" in run.err
    settings = json.loads(s.hook_files(run.a)["claude-code/managed-settings.json"])
    assert len(settings["hooks"]["PreToolUse"]) == 1, "the rule hook only"
    # Without managed hooks Claude Code runs the plugin's hooks itself: nothing to say, nothing installed.
    plain = m.run()
    assert "trust-hooks" not in plain.err and not s.managed_hooks(plain.a)


def test_a_missing_or_linked_plugin_folder_never_stops_a_start(host, tmp_path):
    """installed_plugins.json names a plugin whose folder is gone (a cleaned cache) or is a link (the store's own
    sandbox can write there): no crash (it was KeyError 'kept'), nothing staged that could run."""
    root = install()
    import shutil as sh
    sh.rmtree(root)
    assert k.stage_plugin_hooks([PLUGIN], k.installed()[1]) == []
    (tmp_path / "elsewhere").mkdir()
    root.symlink_to(tmp_path / "elsewhere")
    staged = k.stage_plugin_hooks([PLUGIN], k.installed()[1])
    assert all(not item["trusted"] and item["errors"] for item in staged)
