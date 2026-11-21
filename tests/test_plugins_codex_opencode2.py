"""Kits' plugins for Codex and OpenCode 2 (sandbox/kits.py, sandbox/plugins.py, sandbox/kitctl.py, the launcher).

One plugin store (Claude Code installs into it, `sandbox-ai plugins claude|codex|opencode2`); each Codex and OpenCode 2
sandbox gets only its kits' plugins, read-only, as the reviewed releases read them:

- Codex 0.169.0: the plugin folder at ~/.codex/plugins/cache/MARKETPLACE/NAME/local, enabled in its managed
  configuration (above every config.toml and -c), marketplace sources restricted in its requirements (no curated or
  other marketplace is fetched), no plugin MCP server, no plugin of the ChatGPT account; a plugin's hooks run only once
  approved (only managed hooks run then); a plugin with ChatGPT apps is not given; strict telemetry turns them off.
- OpenCode 2.2.5: an approved plugin's folder, named in OPENCODE_CONFIG_CONTENT after sandbox-ai's rule plugin; an
  unapproved one is not loaded at all (a plugin is code in OpenCode 2's own process)."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10: the same parser as a package
    import tomli as tomllib

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m, om2  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]
s = fm.s
sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugins = load("plugins_for_codex_oc2_tests", "sandbox/plugins.py")
k = plugins.kits()
runtime = load("runtime_for_codex_oc2_tests", "sandbox/runtime.py")
kitctl = load("kitctl_for_codex_oc2_tests", "sandbox/kitctl.py")
PLUGIN = "superpowers@sandbox-ai"
INSTALL = "/root/.claude/plugins/cache/sandbox-ai/superpowers/7.0.1"
CODEX_ROOT = "/root/.codex/plugins/cache/sandbox-ai/superpowers/local"
OC2_ROOT = "/sandbox/plugins/sandbox-ai/superpowers"
CLAUDE_HOOKS = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
    {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-start"}]}]}}
RULES = {"version": 1, "tool": [{"id": "no-curl", "tool": "Bash", "args": {"command": {"pattern": "(?s).*curl.*"}},
                                 "decision": "deny"}]}


def install_store(home, codex_manifest=None, extra=None, plugin=PLUGIN, claude_manifest=True, hooks=CLAUDE_HOOKS):
    """The plugin store as `claude plugin install` leaves it, with a folder shaped like upstream's superpowers 7.0.1:
    a Claude manifest and SessionStart hook, a Codex manifest (by default with "hooks": {}, as upstream's), skills and
    an OpenCode 2 plugin module (index.js). Returns the installed folder."""
    store = Path(home) / ".local/share/sandbox-ai/plugins/claude/.claude/plugins"
    store.mkdir(parents=True, exist_ok=True)
    name, marketplace = plugin.split("@")
    (store / "known_marketplaces.json").write_text(json.dumps(
        {marketplace: {"source": {"source": "directory", "path": "/sandbox/marketplace"}}}))
    install_path = f"/root/.claude/plugins/cache/{marketplace}/{name}/7.0.1"
    (store / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {plugin: [{"scope": "user", "installPath": install_path}]}}))
    root = store / f"cache/{marketplace}/{name}/7.0.1"
    (root / "skills/brainstorming").mkdir(parents=True, exist_ok=True)
    (root / "skills/brainstorming/SKILL.md").write_text("---\nname: brainstorming\ndescription: x\n---\nbody\n")
    if claude_manifest:
        (root / ".claude-plugin").mkdir(exist_ok=True)
        (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name, "version": "7.0.1"}))
    if hooks is not None:
        (root / "hooks").mkdir(exist_ok=True)
        (root / "hooks/hooks.json").write_text(json.dumps(hooks))
        (root / "hooks/session-start").write_text("#!/bin/sh\necho '{}'\n")
        (root / "hooks/session-start").chmod(0o755)
    manifest = {"name": name, "skills": "./skills/", "hooks": {}} if codex_manifest is None else codex_manifest
    if manifest is not False:
        (root / ".codex-plugin").mkdir(exist_ok=True)
        (root / ".codex-plugin/plugin.json").write_text(json.dumps(manifest))
    (root / "index.js").write_text('export { default } from "./.opencode/plugins/superpowers.js";\n')
    (root / ".opencode/plugins").mkdir(parents=True, exist_ok=True)
    (root / ".opencode/plugins/superpowers.js").write_text("export default { id: 'superpowers', setup() {} };\n")
    (root / "package.json").write_text(json.dumps({"name": name, "type": "module"}))
    for relative, text in (extra or {}).items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    return root


def make_kit(home, agents=("codex", "opencode2"), name="sp", plugin=PLUGIN):
    for agent in agents:
        folder = Path(home) / f".local/share/sandbox-ai/kits/{name}/{agent}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "marketplaces.json").write_text(json.dumps({
            "marketplaces": {"sandbox-ai": {"source": {"source": "directory", "path": "/sandbox/marketplace"}}},
            "plugins": [plugin]}))


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home


def codex_info(root, plugin=PLUGIN):
    return k.read_store_plugin("codex", plugin, root, INSTALL)


# --- what Codex reads of a plugin folder ----------------------------------------------------------------------

def test_codex_takes_the_codex_manifest_and_its_empty_hooks_over_the_default_hooks_file(host):
    """Upstream's superpowers: .codex-plugin/plugin.json with "hooks": {} - Codex loads no hook, though
    hooks/hooks.json (Claude's SessionStart hook) lies in the folder (loader.rs: manifest hooks replace the default)."""
    info = codex_info(install_store(host))
    assert info["manifest"] == ".codex-plugin/plugin.json" and info["format"] == "legacy"
    assert not info["declared"] and info["hooks"] == [] and not info["errors"] and info["sha256"]


def test_without_a_codex_manifest_codex_reads_claudes_and_the_default_hooks(host):
    info = codex_info(install_store(host, codex_manifest=False))
    assert info["manifest"] == ".claude-plugin/plugin.json" and info["declared"]
    [group] = info["groups"]["SessionStart"]
    command = group["hooks"][0]["command"]
    assert command.startswith(f"export PLUGIN_ROOT={CODEX_ROOT}; export CLAUDE_PLUGIN_ROOT={CODEX_ROOT}; "
                              "export PLUGIN_DATA=/root/.codex/plugins/data/superpowers-sandbox-ai; ")
    assert command.endswith(f"; {CODEX_ROOT}/hooks/session-start")


def test_manifest_hook_paths_replace_the_default_file(host):
    other = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "${PLUGIN_ROOT}/check", "timeout": 5}]}]}})
    info = codex_info(install_store(host, codex_manifest={"name": "superpowers", "hooks": "./more/hooks.json"},
                                    extra={"more/hooks.json": other, "check": "#!/bin/sh\n"}))
    assert list(info["groups"]) == ["PreToolUse"], "the manifest's file, not hooks/hooks.json"
    assert info["groups"]["PreToolUse"][0]["hooks"][0]["command"].endswith(f"; {CODEX_ROOT}/check")
    # A path without ./ is ignored by Codex: it falls back to the default file.
    info = codex_info(install_store(host, codex_manifest={"name": "superpowers", "hooks": "more/hooks.json"}))
    assert list(info["groups"]) == ["SessionStart"]


def test_an_agent_plugin_manifest_brings_no_hooks_to_codex(host):
    root = install_store(host, codex_manifest=False, extra={"plugin.json": json.dumps(
        {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "name": "superpowers"})})
    info = codex_info(root)
    assert info["format"] == "agent" and not info["declared"]


def test_apps_and_mcp_servers_are_named(host):
    root = install_store(host, extra={".app.json": "{}", ".mcp.json": json.dumps({"mcpServers": {"db": {}}})})
    info = codex_info(root)
    assert info["apps"] is True and info["mcp"] == ["db"]


def test_a_plugin_codex_cannot_read_is_an_error(host):
    info = codex_info(install_store(host, codex_manifest=False, claude_manifest=False))
    assert info["errors"] and info["sha256"] is None


# --- what OpenCode 2 loads of a plugin folder -----------------------------------------------------------------

@pytest.mark.parametrize("files,expected", [
    ({"index.js": "x"}, "index.js"),
    ({"server.mjs": "x", "index.js": "x"}, "server.mjs"),
    ({"package.json": json.dumps({"name": "p", "exports": "./lib/main.js"}), "lib/main.js": "x", "index.js": "x"},
     "lib/main.js"),
    ({"package.json": json.dumps({"name": "p", "exports": {"./server": "./s.js"}}), "s.js": "x"}, "s.js"),
])
def test_opencode2_entry_follows_its_host_resolve(files, expected):
    listing = {path: {"path": path} for path in files}
    kept = {"package.json": files["package.json"].encode()} if "package.json" in files else {}
    assert k.opencode2_entry(listing, kept) == (expected, None)


@pytest.mark.parametrize("files,why", [
    ({"README.md": "x"}, "no server or index module"),
    ({"index.js": "x", "index.ts": "x"}, "depends on Bun's order"),
    ({"package.json": json.dumps({"name": "p", "exports": {".": {"import": "./a.js"}}}), "a.js": "x"},
     "does not resolve"),
])
def test_opencode2_entries_sandbox_ai_cannot_check_are_refused(files, why):
    listing = {path: {"path": path} for path in files}
    kept = {"package.json": files["package.json"].encode()} if "package.json" in files else {}
    entry, problem = k.opencode2_entry(listing, kept)
    assert entry is None and why in problem


# --- the kits declare them ------------------------------------------------------------------------------------

def test_codex_and_opencode2_kits_declare_plugins_in_claudes_format(host):
    make_kit(host)
    for agent in ("codex", "opencode2"):
        marketplaces, listed, notes = k.declarations(agent, [("kit sp", host / f".local/share/sandbox-ai/kits/sp/"
                                                                         f"{agent}/marketplaces.json")])
        assert listed == [PLUGIN] and "sandbox-ai" in marketplaces and not notes


def test_kitctl_adds_a_codex_plugin(host):
    said = []
    assert kitctl.main(["new", "mine", "--agent", "codex"], out=said.append) == 0
    assert kitctl.main(["add-plugin", "mine", PLUGIN, "--agent", "codex"], out=said.append) == 0
    document = json.loads((host / ".local/share/sandbox-ai/kits/mine/codex/marketplaces.json").read_text())
    assert document["plugins"] == [PLUGIN] and document["marketplaces"]["sandbox-ai"]["source"]["source"] == \
        "directory"
    assert any("sandbox-ai plugins codex" in line for line in said)
    assert any("trust-hooks" in line for line in said)


# --- a Codex start ----------------------------------------------------------------------------------------------

def codex_files(run):
    files = s.hook_files(run.a) if (s.managed_hooks(run.a) or getattr(run.a, "codex_plugins", None)) else {}
    return files, s.telemetry_guard_files(run.a, files)


def test_a_codex_start_gets_its_kits_plugin_read_only_and_enabled_in_its_managed_configuration(m):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex")
    root = install_store(home())
    make_kit(home())
    run = m.run("--kit", "sp")
    assert (str(root), CODEX_ROOT, "ro") in run.kit_mounts
    requirements, managed = codex_files(run)
    config = tomllib.loads(managed["codex/managed_config.toml"])
    assert config["plugins"][PLUGIN] == {"enabled": True}
    assert config["features"]["remote_plugin"] is False
    assert config["marketplaces"]["sandbox-ai"] == {"source_type": "local",
                                                    "source": "/sandbox/session/codex-marketplaces/sandbox-ai"}
    assert config["analytics"]["enabled"] is False, "telemetry stays pinned off beside them"
    rules = tomllib.loads(requirements["codex/requirements.toml"])
    assert rules["marketplaces"]["restrict_to_allowed_sources"] is True
    assert rules["marketplaces"]["allowed_sources"]["sandbox-ai"] == {
        "source": "local", "path": "/sandbox/session/codex-marketplaces/sandbox-ai"}
    assert rules["plugins"][PLUGIN] == {"mcp_servers": {}}, "no MCP server of the plugin runs"
    assert "allow_managed_hooks_only" not in rules, "upstream's Codex manifest brings no hooks"
    assert run.config["codex_plugins"] is True


def test_a_codex_plugins_hooks_run_only_once_approved(m, tmp_path):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex")
    install_store(home(), codex_manifest=False)
    make_kit(home())
    run = m.run("--kit", "sp")
    assert f"sandbox-ai plugins codex trust-hooks {PLUGIN}" in run.err
    requirements, _ = codex_files(run)
    rules = tomllib.loads(requirements["codex/requirements.toml"])
    assert rules["allow_managed_hooks_only"] is True and "hooks" in rules["features"]
    assert "SessionStart" not in rules.get("hooks", {}), "not approved: not installed"
    assert plugins.trust_hooks([PLUGIN, "--yes"], lambda line: None, agent="codex") == 0
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(RULES))
    run = m.run("--kit", "sp", "--rules", str(rules_file))
    assert f"plugin {PLUGIN}: 1 approved hook(s)" in run.err
    rules = tomllib.loads(codex_files(run)[0]["codex/requirements.toml"])
    assert rules["hooks"]["PreToolUse"][0]["hooks"][0]["command"].startswith(
        "/usr/bin/python3 -I /sandbox/runtime.py --internal-hook codex PreToolUse"), "the rule hook comes first"
    assert rules["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith(f"; {CODEX_ROOT}/hooks/session-start")
    # A Claude approval of the same plugin does not approve it for Codex, nor the other way round.
    assert not k.hooks_trusted("claude", codex_info(Path(run.kit_mounts[0][0]))["sha256"], k.plugin_trust_key(PLUGIN))


def test_a_codex_plugin_with_chatgpt_apps_is_not_given(m):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex")
    install_store(home(), extra={".app.json": "{}"})
    make_kit(home())
    run = m.run("--kit", "sp")
    assert "ChatGPT apps" in run.err and not any(dest.startswith("/root/.codex/plugins") for _, dest, _ in
                                                 run.kit_mounts)
    assert not getattr(run.a, "codex_plugins", None)


def test_strict_telemetry_keeps_codex_plugins_off(m):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex")
    install_store(home())
    make_kit(home())
    run = m.run("--kit", "sp", "--telemetry", "strict")
    assert "turns Codex's plugin system off" in run.err
    assert not any(dest.startswith("/root/.codex/plugins") for _, dest, _ in run.kit_mounts)


def test_a_missing_plugin_names_the_install_command(m):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex")
    make_kit(home())
    run = m.run("--kit", "sp")
    assert "install them with: sandbox-ai plugins codex" in run.err


def test_the_dry_run_shows_codex_plugins_and_their_approval(m):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex")
    install_store(home())
    make_kit(home())
    plan = m.plan("--kit", "sp")
    assert plan["kits"]["plugins"]["enabled"] == [PLUGIN]
    assert plan["kits"]["plugins"]["approval"] == {PLUGIN: "nothing to approve"}


# --- an OpenCode 2 start ----------------------------------------------------------------------------------------

def test_an_unapproved_opencode2_plugin_is_not_loaded(om2):  # noqa: F811
    install_store(home())
    make_kit(home())
    run = om2.run("--kit", "sp")
    assert f"sandbox-ai plugins opencode2 trust-hooks {PLUGIN}" in run.err
    content = json.loads(run.env["OPENCODE_CONFIG_CONTENT"])
    assert "plugins" not in content and not any(dest == OC2_ROOT for _, dest, _ in run.kit_mounts)


def test_an_approved_opencode2_plugin_loads_after_the_rule_plugin(om2, tmp_path):  # noqa: F811
    root = install_store(home())
    make_kit(home())
    said = []
    assert plugins.trust_hooks([PLUGIN, "--yes"], said.append, agent="opencode2") == 0
    assert any("OpenCode 2 runs its module index.js" in line for line in said)
    assert any(line.startswith("  --- .opencode/plugins/superpowers.js") for line in said), \
        "what the module imports is shown in full too"
    assert any("id: 'superpowers'" in line for line in said)
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(RULES))
    run = om2.run("--kit", "sp", "--rules", str(rules_file))
    content = json.loads(run.env["OPENCODE_CONFIG_CONTENT"])
    assert content["plugins"] == ["/etc/opencode2/sandbox-ai-hooks", OC2_ROOT]
    assert (str(root), OC2_ROOT, "ro") in run.kit_mounts
    # Without rules the plugin loads alone.
    run = om2.run("--kit", "sp")
    assert json.loads(run.env["OPENCODE_CONFIG_CONTENT"])["plugins"] == [OC2_ROOT]
    # Any change takes the approval away.
    (root / "index.js").write_text("export default { id: 'other', setup() {} };\n")
    run = om2.run("--kit", "sp")
    assert "plugins" not in json.loads(run.env["OPENCODE_CONFIG_CONTENT"])


# --- the supervisor's check of Codex ----------------------------------------------------------------------------

@pytest.mark.parametrize("version,refused", [("codex-cli 0.168.0", True), ("codex-cli 0.169.0", False),
                                             ("codex-cli 0.170.2", False), ("no version", True)])
def test_the_kits_codex_plugins_need_the_reviewed_codex(version, refused):
    class Done:
        stdout = version
    problem = runtime.hook_problem({"codex_plugins": True, "command": ["codex"]}, run=lambda *a, **kw: Done())
    assert bool(problem) == refused
    if refused:
        assert "the kits' Codex plugins cannot be given" in problem
    assert runtime.hook_problem({"codex_plugins": False, "command": ["codex"]}, run=lambda *a, **kw: Done()) is None


# --- sandbox-ai plugins ---------------------------------------------------------------------------------------

def test_plugins_status_names_codex_and_opencode2_plugins(host):
    install_store(host)
    make_kit(host)
    said = []
    assert plugins.status(said.append) == 0
    assert any(line.startswith("codex") and PLUGIN in line and "not installed" not in line for line in said)
    assert any(line.startswith("opencode2") and PLUGIN in line for line in said)


def test_plugins_codex_installs_into_the_one_store_for_codex(host):
    """`sandbox-ai plugins codex` runs Claude's installer on the one store with the kits' Codex declarations, then
    says what a Codex sandbox makes of each plugin."""
    make_kit(host, agents=("codex",))
    calls = []

    def runner(command):
        calls.append(command)
        store = host / ".local/share/sandbox-ai/plugins/claude/.claude/plugins"
        if command[-3:-1] == ["marketplace", "add"]:  # what the installer leaves
            store.mkdir(parents=True, exist_ok=True)
            (store / "known_marketplaces.json").write_text(json.dumps({"sandbox-ai": {"source": {}}}))
        else:
            install_store(host, extra={".mcp.json": json.dumps({"mcpServers": {"db": {}}})})
        return subprocess.CompletedProcess(command, 0)
    said = []
    assert plugins.sync(out=said.append, runner=runner, agent="codex") == 0
    assert any(command[-2:] == ["install", PLUGIN] for command in calls)
    assert any("its MCP servers (db) do not run in Codex sandboxes" in line for line in said)
    assert "every Codex sandbox gets those its kits list, read-only" in said[-1]


def home():
    return Path(os.environ["HOME"])


# --- end to end: ./sandbox-ai with Podman faked ---------------------------------------------------------------

@pytest.fixture
def fake_host():
    directory = tempfile.mkdtemp(prefix="plugins-e2e-", dir=os.environ.get("TMPDIR") or "/tmp")
    found = Host(directory)
    found.add_image("localhost/codex-sandbox:latest", "localhost/opencode2-sandbox:latest")
    yield found
    subprocess.run(["rm", "-rf", directory], check=False)


def test_e2e_a_codex_start_with_a_kits_plugin(fake_host):
    root = install_store(fake_host.home)
    make_kit(fake_host.home)
    kept = fake_host.directory / "kept"
    done = fake_host.run("codex", "--kit", "sp", "--no-tmux", "--no-keep", timeout=300,
                         env={"FAKE_PODMAN_RUN": "0", "FAKE_PODMAN_KEEP": str(kept)})
    session = kept / "session"
    assert session.is_dir(), (done.returncode, done.stderr[-2000:])
    managed = tomllib.loads((session / "codex/managed_config.toml").read_text())
    assert managed["plugins"][PLUGIN]["enabled"] is True
    requirements = tomllib.loads((session / "codex/requirements.toml").read_text())
    assert requirements["marketplaces"]["restrict_to_allowed_sources"] is True
    assert (session / "codex-marketplaces/sandbox-ai").is_dir()
    assert not any((session / "codex-marketplaces/sandbox-ai").iterdir()), "an empty folder: nothing to copy from"
    assert json.loads((session / "config.json").read_text())["codex_plugins"] is True
    runs = [call for call in fake_host.calls() if call[:1] == ["run"] and "--pod" in " ".join(call)]
    mounts = " ".join(" ".join(call) for call in runs)
    assert f"src={root},dst={CODEX_ROOT},ro" in mounts or f"{root}:{CODEX_ROOT}:ro" in mounts, mounts[-3000:]
    assert "dst=/etc/codex,ro" in mounts


def test_e2e_an_approved_opencode2_plugin(fake_host):
    root = install_store(fake_host.home)
    make_kit(fake_host.home)
    approved = fake_host.run("plugins", "opencode2", "trust-hooks", PLUGIN, "--yes", timeout=120)
    assert approved.returncode == 0, approved.stderr[-1500:]
    kept = fake_host.directory / "kept"
    done = fake_host.run("opencode2", "--kit", "sp", "--no-tmux", "--no-keep", timeout=300,
                         env={"FAKE_PODMAN_RUN": "0", "FAKE_PODMAN_KEEP": str(kept)})
    session = kept / "session"
    assert session.is_dir(), (done.returncode, done.stderr[-2000:])
    layer = json.loads((session / "opencode2/sandbox-ai-layer.json").read_text())
    assert layer["plugins"] == [OC2_ROOT]
    runs = [call for call in fake_host.calls() if call[:1] == ["run"] and "--pod" in " ".join(call)]
    mounts = " ".join(" ".join(call) for call in runs)
    assert f"src={root},dst={OC2_ROOT},ro" in mounts or f"{root}:{OC2_ROOT}:ro" in mounts, mounts[-3000:]


# --- the built-in kit superpowers-plugin: the first real case ----------------------------------------------------

def test_the_builtin_kit_superpowers_plugin_enables_upstreams_plugin_for_the_three_agents(host):
    for agent in ("claude", "codex", "opencode2"):
        path = k.BUILTIN_KITS / "superpowers-plugin" / agent / "marketplaces.json"
        marketplaces, listed, notes = k.declarations(agent, [("built-in kit superpowers-plugin", path)])
        assert listed == [PLUGIN] and marketplaces["sandbox-ai"] == {"source": "directory",
                                                                     "path": "/sandbox/marketplace"} and not notes
        assert k.install_command([PLUGIN], agent) == f"sandbox-ai plugins {agent} --kit superpowers-plugin"
    assert "superpowers-plugin" not in k.builtin_kits("opencode"), "OpenCode 1 takes npm plugins only"


def test_superpowers_is_pinned_to_the_reviewed_commit_at_least_14_days_old():
    """The plugin the kit enables is the commit the vendored kit superpowers was reviewed at (docs/skills-review.md),
    committed at least 14 days before it was taken in for Codex and OpenCode 2 (2026-11-16)."""
    import datetime
    marketplace = json.loads((ROOT / "sandbox/marketplace/.claude-plugin/marketplace.json").read_text())
    entry = next(p for p in marketplace["plugins"] if p["name"] == "superpowers")
    upstream = json.loads((ROOT / "sandbox/builtin/kits/superpowers/kit.json").read_text())["upstream"]
    assert entry["source"] == {"source": "github", "repo": "obra/superpowers", "sha": upstream["commit"]}
    committed = datetime.datetime.fromisoformat(upstream["committed"])
    assert datetime.datetime(2026, 11, 16, tzinfo=datetime.timezone.utc) - committed >= datetime.timedelta(days=14)


def test_plugins_all_installs_what_the_three_agents_kits_declare_in_one_go(host):
    make_kit(host, agents=("opencode2",))
    from test_kits import FakeClaude
    fake = FakeClaude()

    def runner(command):
        done = fake(command)
        if command[-1] == PLUGIN:  # its folder, where the index says
            index = k.plugin_store("claude") / ".claude/plugins/installed_plugins.json"
            known = index.with_name("known_marketplaces.json")
            document, marketplaces = json.loads(index.read_text()), known.read_text()
            install_store(host)
            document["plugins"][PLUGIN] = [{"installPath": INSTALL}]
            index.write_text(json.dumps(document))
            known.write_text(marketplaces)
        return done
    said = []
    assert plugins.sync(out=said.append, runner=runner, agent="all") == 0, "\n".join(said[-6:])
    calls = fake.commands
    assert any(command[-2:] == ["install", PLUGIN] for command in calls), "the OpenCode 2 kit's plugin"
    assert any("OpenCode 2 loads index.js; it runs only once approved" in line for line in said)
    assert "every Claude, Codex and OpenCode 2 sandbox" in said[-1]
    with pytest.raises(ValueError, match="no built-in kit of that name"):
        plugins.store_declarations("all", ["nope"])
    assert plugins.store_declarations("all", ["superpowers-plugin"])[1].count(PLUGIN) == 1


def test_the_marketplace_listing_names_the_codex_and_opencode2_kits_of_each_plugin(host):
    make_kit(host)
    data = plugins.marketplaces("claude")
    own = next(m for m in data["marketplaces"] if m["name"] == "sandbox-ai")
    row = next(p for p in own["plugins"] if p["id"] == PLUGIN)
    for agent in ("codex", "opencode2"):
        assert {"kit": "sp", "kind": "user", "enabled": True} in row["agent_kits"][agent]
        assert {"kit": "superpowers-plugin", "kind": "builtin", "enabled": True} in row["agent_kits"][agent]
    assert "sp" in [e["kit"] for e in own["agent_kits"]["codex"]]
    assert not any(e["kit"] == "sp" for e in row["kits"]), "Claude's kits stay Claude's"
    assert data["agents"]["claude"]["kits"] is True
    said = []
    plugins.render_marketplaces(data, said.append)
    assert any("codex kits: " in line and "sp" in line for line in said)


@pytest.mark.parametrize("name,reserved", [("openai-curated", True), ("openai-bundled", True),
                                           ("workspace-shared-with-me-private", True), ("created-by-me-remote", True),
                                           ("sandbox-ai", False), ("workspace", False)])
def test_marketplace_names_codex_keeps_for_its_own(name, reserved):
    assert bool(k.CODEX_RESERVED_MARKETPLACE.fullmatch(name)) == reserved


def test_a_plugin_of_a_reserved_marketplace_name_is_not_given_to_codex(m):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex")
    plugin = "superpowers@openai-curated"
    install_store(home(), plugin=plugin)
    make_kit(home(), agents=("codex",), plugin=plugin)
    folder = home() / ".local/share/sandbox-ai/kits/sp/codex/marketplaces.json"
    document = json.loads(folder.read_text())
    document["marketplaces"] = {"openai-curated": {"source": {"source": "github", "repo": "o/r"}}}
    folder.write_text(json.dumps(document))
    run = m.run("--kit", "sp")
    assert "Codex keeps the marketplace name openai-curated for its own" in run.err
    assert not getattr(run.a, "codex_plugins", None)
