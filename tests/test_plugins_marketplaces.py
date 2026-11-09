"""sandbox-ai plugins marketplaces and the kit commands that use it (sandbox/plugins.py, sandbox/kitctl.py), end to end
through the real command (sandbox/main.py) in a private HOME with Claude's, Codex's and OpenCode's own configuration
faked (tests/fake_agents.py): the JSON the web and the app read (its keys are a contract), every marketplace with its
plugins and their state, missing and damaged agent files, and adding, enabling, disabling, removing and importing.
Nothing is fetched (no network) and nothing runs in a sandbox; the agents' own files are never changed. Nothing here
writes a login (HOME is moved for the session by conftest.py, and again per test here)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from fake_agents import ACME, fake_agents, write

ROOT = Path(__file__).resolve().parents[1]

TOP_KEYS = {"version", "store", "kits", "agents", "marketplaces", "errors"}
KIT_KEYS = {"name", "kind", "editable", "agents"}
AGENT_KEYS = {"marketplaces", "kits", "config", "about", "notes"}
MARKET_KEYS = {"name", "agent", "builtin", "origins", "description", "source", "source_value", "importable", "problem",
               "import", "kits", "store", "agent_config", "listed_from", "plugins"}
CODEX_MARKET_KEYS = MARKET_KEYS | {"codex_source"}
PLUGIN_KEYS = {"name", "id", "listed", "description", "version", "category", "homepage", "source", "kits", "store",
               "agent"}
OPENCODE_PLUGIN_KEYS = {"name", "id", "importable", "kits", "agent"}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = fake_agents(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR",
                 "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home


def cli(*args, env=None):
    done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), *args], capture_output=True,
                          text=True, timeout=120, env={**os.environ, **(env or {})})
    return done.returncode, done.stdout, done.stderr


def listing(*args, env=None):
    status, out, err = cli("plugins", "marketplaces", "--json", *args, env=env)
    assert status == 0, err
    return json.loads(out)


def by_key(data):
    return {(m["agent"], m["name"]): m for m in data["marketplaces"]}


def plugin(market, plugin_id):
    return next(p for p in market["plugins"] if p["id"] == plugin_id)


def snapshot(home):
    """The agents' own files (everything but sandbox-ai's own data)."""
    return {path: path.read_bytes() for path in home.rglob("*") if path.is_file() and ".local" not in path.parts}


def kit_file(home, kit, agent="claude"):
    folder = home / ".local/share/sandbox-ai" / ("home" if kit == "default" else f"kits/{kit}")
    return json.loads((folder / agent / "marketplaces.json").read_text())


# --- the listing -----------------------------------------------------------------------------------------------

def test_the_json_has_the_keys_the_web_and_the_app_read(home):
    data = listing()
    assert set(data) == TOP_KEYS and data["version"] == 1 and data["errors"] == []
    assert data["store"] == str(home / ".local/share/sandbox-ai/plugins/claude")
    assert set(data["agents"]) == {"claude", "codex", "opencode"}
    for name, info in data["agents"].items():
        assert set(info) == AGENT_KEYS | ({"plugins", "modules"} if name == "opencode" else set()), name
    assert data["agents"]["opencode"]["marketplaces"] is False and data["agents"]["codex"]["kits"] is False
    assert "OpenCode has no marketplaces" in data["agents"]["opencode"]["about"]
    for kit in data["kits"]:
        assert set(kit) == KIT_KEYS and kit["kind"] in ("default", "builtin", "user")
    assert data["kits"][0] == {"name": "default", "kind": "default", "editable": True, "agents": []}
    for market in data["marketplaces"]:
        assert set(market) == (CODEX_MARKET_KEYS if market["agent"] == "codex" else MARKET_KEYS), market["name"]
        for entry in market["plugins"]:
            assert set(entry) == PLUGIN_KEYS and entry["id"] == f"{entry['name']}@{market['name']}"
    for entry in data["agents"]["opencode"]["plugins"]:
        assert set(entry) == OPENCODE_PLUGIN_KEYS


def test_every_marketplace_with_its_plugins_and_their_state(home):
    markets = by_key(listing())
    own = markets[("claude", "sandbox-ai")]
    assert own["builtin"] and own["source_value"] == "sandbox-ai" and own["listed_from"] == "sandbox-ai"
    assert own["origins"][0] == "sandbox-ai" and own["importable"]
    # Claude's own marketplace with the copy it fetched: Claude's state of each plugin, in no kit yet
    acme = markets[("claude", "acme")]
    assert acme["origins"] == ["claude"] and acme["description"] == "Acme plugins"
    assert acme["source"] == {"source": "github", "repo": "acme/plugins"} and acme["source_value"] == "acme/plugins"
    assert acme["agent_config"] == {"known": True, "settings": False, "fetched": True} and acme["listed_from"] == "claude"
    lint, fmt = plugin(acme, "lint@acme"), plugin(acme, "fmt@acme")
    assert lint["agent"] == {"installed": True, "enabled": True} and lint["listed"] and lint["version"] == "1.2"
    assert lint["category"] == "quality" and lint["source"] == "local" and lint["kits"] == []
    assert lint["store"] == {"installed": False, "version": None}
    assert fmt["agent"] == {"installed": False, "enabled": False} and fmt["source"] == "github:acme/fmt"
    # extraKnownMarketplaces: known, its plugin list not fetched
    team = markets[("claude", "team")]
    assert team["agent_config"]["settings"] and team["listed_from"] is None and team["plugins"] == []
    assert team["source_value"] == "https://git.example.org/team/plugins.git" and team["importable"]
    # a folder on this machine cannot go into a kit, and says why
    local = markets[("claude", "localdir")]
    assert not local["importable"] and "folder or file on this machine" in local["problem"]
    # the default kit's examples, before it exists: suggested
    assert "suggested" in markets[("claude", "claude-plugins-official")]["origins"]
    # Codex: its Git marketplace, its copy in Claude's format importable for Claude under the name its manifest gives
    cx = markets[("codex", "cx")]
    assert cx["import"] == {"name": "cx-claude", "source": {"source": "github", "repo": "acme/cx-plugins"},
                            "source_value": "acme/cx-plugins"}
    assert cx["codex_source"] == {"source_type": "git", "source": "https://github.com/acme/cx-plugins.git"}
    assert plugin(cx, "tool@cx")["agent"] == {"installed": True, "enabled": True} and cx["store"] is None
    # OpenCode: no marketplaces; its npm plugins (opencode.jsonc with comments) and its plugin modules
    opencode = listing()["agents"]["opencode"]
    assert opencode["plugins"] == [{"name": "opencode-foo@1.0", "id": "opencode-foo@1.0", "importable": True,
                                    "kits": [], "agent": {"configured": True}}]
    assert opencode["modules"] == [str(home / ".config/opencode/plugin/guard.js")]
    assert opencode["config"] == str(home / ".config/opencode/opencode.jsonc")


def test_one_marketplace_one_agent_and_the_text(home):
    data = listing("acme")
    assert [m["name"] for m in data["marketplaces"]] == ["acme"]
    assert set(by_key(listing("--agent", "codex"))) == {("codex", "cx")}
    assert set(listing("--agent", "codex")["agents"]) == {"codex"}
    status, out, _ = cli("plugins", "marketplaces")
    assert status == 0 and "claude   acme (acme/plugins; claude)" in out and "lint@acme [agent: installed, enabled]" in out
    assert "import for Claude as cx-claude: sandbox-ai kits import-marketplace KIT cx --from codex" in out
    status, out, _ = cli("plugins", "marketplaces", "acme")
    assert status == 0 and "lint@acme [agent: installed, enabled]: Lints" in out
    status, out, _ = cli("plugins", "marketplaces", "nope")
    assert status == 1 and "no marketplace of that name" in out
    status, _, err = cli("plugins", "marketplaces", "-bad")
    assert status != 0
    status, _, err = cli("plugins", "marketplaces", "a/b")
    assert status == 1 and "1-64 letters" in err


def test_the_agents_own_directories_where_their_variables_say(home, tmp_path):
    claude, codex = tmp_path / "elsewhere-claude", tmp_path / "elsewhere-codex"
    (home / ".claude").rename(claude)
    (home / ".codex").rename(codex)
    assert ("claude", "acme") not in by_key(listing())
    data = listing(env={"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)})
    assert ("claude", "acme") in by_key(data) and ("codex", "cx") in by_key(data)
    assert data["agents"]["claude"]["config"] == str(claude)


@pytest.mark.parametrize("damage", ["invalid-json", "not-an-object", "fifo", "directory", "too-large", "toml",
                                    "dangling-link", "binary", "missing"])
def test_missing_or_damaged_agent_files_are_notes_never_failures(home, damage):
    settings, known = home / ".claude/settings.json", home / ".claude/plugins/known_marketplaces.json"
    config, opencode = home / ".codex/config.toml", home / ".config/opencode/opencode.jsonc"
    if damage == "invalid-json":
        settings.write_text("{nope")
        known.write_text("[1, 2")
        opencode.write_text("{ /* open")
    elif damage == "not-an-object":
        settings.write_text("[]")
        known.write_text('"x"')
    elif damage == "fifo":  # never blocks
        settings.unlink()
        os.mkfifo(settings)
        config.unlink()
        os.mkfifo(config)
    elif damage == "directory":
        known.unlink()
        known.mkdir()
    elif damage == "too-large":
        settings.write_text(" " * (4 << 20) + "{}")
    elif damage == "toml":
        config.write_text("[marketplaces.cx\nsource =")
    elif damage == "dangling-link":
        settings.unlink()
        settings.symlink_to(home / "gone.json")
    elif damage == "binary":
        known.write_bytes(b"\xff\xfe\x00{")
    else:
        for path in (settings, known, config, opencode):
            path.unlink()
    data = listing()
    notes = " ".join(data["agents"]["claude"]["notes"] + data["agents"]["codex"]["notes"]
                     + data["agents"]["opencode"]["notes"])
    expected = {"invalid-json": "not valid JSON", "not-an-object": "not a JSON object", "fifo": "not a regular file",
                "directory": "not a regular file", "too-large": "larger than 4 MiB", "toml": "not valid TOML",
                "dangling-link": "", "binary": "not valid JSON", "missing": ""}[damage]
    assert expected in notes
    markets = by_key(data)
    assert ("claude", "sandbox-ai") in markets  # the rest is there
    status, out, _ = cli("plugins", "marketplaces")
    assert status == 0 and "sandbox-ai" in out


def test_a_damaged_kit_is_an_error_of_that_kit_only(home):
    write(home, ".local/share/sandbox-ai/kits/broken/claude/marketplaces.json", '{"plugins": "x"}')
    write(home, ".local/share/sandbox-ai/kits/fine/claude/marketplaces.json", json.dumps(
        {"marketplaces": {"acme": {"source": {"source": "github", "repo": "acme/plugins"}}}, "plugins": ["lint@acme"]}))
    data = listing()
    assert [(e["kit"], e["agent"]) for e in data["errors"]] == [("broken", "claude")]
    assert "must be a list of strings" in data["errors"][0]["error"]
    acme = by_key(data)[("claude", "acme")]
    assert acme["origins"] == ["kit", "claude"] and acme["kits"] == [{"kit": "fine", "kind": "user",
                                                                      "source": "acme/plugins"}]
    assert plugin(acme, "lint@acme")["kits"] == [{"kit": "fine", "kind": "user", "enabled": True}]


def test_the_plugin_store_s_copy_lists_a_marketplace_and_a_planted_symlink_is_not_followed(home):
    store = home / ".local/share/sandbox-ai/plugins/claude/.claude/plugins"
    write(store, "known_marketplaces.json", json.dumps({"acme": {
        "source": {"source": "github", "repo": "acme/plugins"},
        "installLocation": "/root/.claude/plugins/marketplaces/acme"}}))
    write(store, "installed_plugins.json", json.dumps({"version": 2, "plugins": {"fmt@acme": [{"version": "2.0"}]}}))
    write(store, "marketplaces/acme/.claude-plugin/marketplace.json", json.dumps(
        {**ACME, "plugins": ACME["plugins"] + [{"name": "extra", "source": "./x", "description": "Only in the store"}]}))
    acme = by_key(listing())[("claude", "acme")]
    assert acme["listed_from"] == "store" and acme["store"] == {"fetched": True} and "store" in acme["origins"]
    assert plugin(acme, "extra@acme")["description"] == "Only in the store"
    assert plugin(acme, "fmt@acme")["store"] == {"installed": True, "version": "2.0"}
    # the store's own sandbox could plant a symlink: its copy is then left out, Claude's own copy is used
    (store / "marketplaces/acme/.claude-plugin").rename(store / "real")
    (store / "marketplaces/acme/.claude-plugin").symlink_to(store / "real", target_is_directory=True)
    data = listing()
    acme = by_key(data)[("claude", "acme")]
    assert acme["listed_from"] == "claude" and not any(p["id"] == "extra@acme" for p in acme["plugins"])
    assert any("a symlink or not a directory" in note for note in data["agents"]["claude"]["notes"])


def test_codex_marketplaces_that_cannot_go_into_a_kit_say_why(home):
    write(home, ".codex/config.toml", (home / ".codex/config.toml").read_text() + '''
[marketplaces.pinned]
source_type = "git"
source = "https://github.com/acme/pinned.git"
ref = "v1"

[marketplaces.mine]
source_type = "local"
source = "%s"
''' % (home / "codex-local"))
    write(home, "codex-local/.agents/plugins/marketplace.json", json.dumps({"name": "mine", "plugins": [
        {"name": "p", "interface": {"shortDescription": "Codex's own format"}}]}))
    write(home, ".codex/.tmp/plugins/.agents/plugins/marketplace.json", json.dumps({"name": "openai-curated",
                                                                                     "plugins": [{"name": "c"}]}))
    markets = by_key(listing())
    assert "pinned to a ref" in markets[("codex", "pinned")]["problem"]
    mine = markets[("codex", "mine")]
    assert "a folder on this machine" in mine["problem"] and plugin(mine, "p@mine")["description"] == "Codex's own format"
    curated = markets[("codex", "openai-curated")]
    assert curated["origins"] == ["codex-curated"] and "Codex's own format" in curated["problem"]
    status, _, err = cli("kits", "new", "team")
    status, _, err = cli("kits", "import-marketplace", "team", "pinned", "--from", "codex")
    assert status == 1 and "pinned to a ref" in err


# --- changing kits -------------------------------------------------------------------------------------------

def test_import_add_disable_enable_and_remove_change_only_the_kit(home):
    agents_before = snapshot(home)
    assert cli("kits", "new", "team", "--agent", "claude", "--agent", "opencode")[0] == 0
    status, out, err = cli("kits", "import-marketplace", "team", "acme", "--with-plugins")
    assert status == 0, err
    assert "declared the marketplace acme (acme/plugins) for claude, as Claude has it" in out
    assert "enabled lint@acme, as Claude enables them" in out  # fmt@acme is off in Claude: not taken
    assert kit_file(home, "team") == {"marketplaces": {"acme": {"source": {"source": "github",
                                                                           "repo": "acme/plugins"}}},
                                      "plugins": ["lint@acme"]}
    assert cli("kits", "import-marketplace", "team", "acme")[1].endswith("nothing changed\n")
    status, out, _ = cli("kits", "disable-plugin", "team", "lint@acme")
    assert status == 0 and "disabled for claude" in out
    assert kit_file(home, "team")["plugins"] == [] and kit_file(home, "team")["disabled"] == ["lint@acme"]
    assert "is disabled for claude already" in cli("kits", "disable-plugin", "team", "lint@acme")[1]
    status, out, _ = cli("kits", "enable-plugin", "team", "lint@acme")
    assert status == 0 and "enabled for claude" in out and "disabled" not in kit_file(home, "team")
    status, _, err = cli("kits", "enable-plugin", "team", "fmt@acme")
    assert status == 1 and "add it with: sandbox-ai kits add-plugin team fmt@acme" in err
    assert cli("kits", "add-plugin", "team", "fmt@acme")[0] == 0
    # what the listing says now
    acme = by_key(listing())[("claude", "acme")]
    assert acme["kits"] == [{"kit": "team", "kind": "user", "source": "acme/plugins"}]
    assert plugin(acme, "fmt@acme")["kits"] == [{"kit": "team", "kind": "user", "enabled": True}]
    # a marketplace whose plugins the kit lists stays unless they go with it
    status, _, err = cli("kits", "remove-marketplace", "team", "acme")
    assert status == 1 and "lint@acme, fmt@acme" in err and "--with-plugins" in err
    status, out, _ = cli("kits", "remove-marketplace", "team", "acme", "--with-plugins")
    assert status == 0 and "with its plugins lint@acme, fmt@acme" in out
    assert kit_file(home, "team") == {"marketplaces": {}, "plugins": []}
    status, _, err = cli("kits", "remove-marketplace", "team", "acme")
    assert status == 1 and "does not declare the marketplace acme" in err
    # Codex's marketplace in Claude's format, for Claude, under the name its manifest gives
    status, out, _ = cli("kits", "import-marketplace", "team", "cx", "--from", "codex")
    assert status == 0 and "(Codex calls it cx)" in out and "cx-claude" in kit_file(home, "team")["marketplaces"]
    # what cannot be imported says why
    status, _, err = cli("kits", "import-marketplace", "team", "localdir")
    assert status == 1 and "folder or file on this machine" in err
    status, _, err = cli("kits", "import-marketplace", "team", "nope")
    assert status == 1 and "knows no marketplace nope (it knows: acme, localdir, team)" in err
    status, _, err = cli("kits", "import-marketplace", "team", "x", "--from", "opencode")
    assert status == 1 and "OpenCode has no marketplaces" in err
    # OpenCode's npm plugins: added, disabled, enabled, removed
    assert cli("kits", "add-plugin", "team", "opencode-foo@1.0", "--agent", "opencode")[0] == 0
    assert cli("kits", "disable-plugin", "team", "opencode-foo@1.0", "--agent", "opencode")[0] == 0
    assert kit_file(home, "team", "opencode") == {"plugins": [], "disabled": ["opencode-foo@1.0"]}
    assert listing()["agents"]["opencode"]["plugins"][0]["kits"] == [{"kit": "team", "kind": "user",
                                                                      "enabled": False}]
    assert cli("kits", "enable-plugin", "team", "opencode-foo@1.0")[0] == 0
    assert cli("kits", "remove-plugin", "team", "opencode-foo@1.0")[0] == 0
    assert kit_file(home, "team", "opencode") == {"plugins": []}
    status, _, err = cli("kits", "disable-plugin", "team", "x@y", "--agent", "codex")
    assert status == 1 and "not supported yet" in err
    assert snapshot(home) == agents_before, "the agents' own files are never changed"


def test_the_default_kit_s_plugins_and_marketplaces_change_and_a_later_kit_turns_one_off(home):
    kits = load("kits_for_marketplace_tests", "sandbox/kits.py")
    default = home / ".local/share/sandbox-ai/home"
    assert not default.exists()
    status, out, err = cli("kits", "remove-marketplace", "default", "claude-plugins-official")
    assert status == 0, err
    assert "created the default kit" in out and "the default kit: removed the marketplace" in out
    assert set(kit_file(home, "default")["marketplaces"]) == {"sandbox-ai"}  # its examples, less the one removed
    status, out, _ = cli("kits", "import-marketplace", "default", "acme", "--with-plugins")
    assert status == 0 and "every claude sandbox gets it (the default kit)" in out
    assert kit_file(home, "default")["plugins"] == ["sandcastle@sandbox-ai", "lint@acme"]
    # a kit of yours given after it turns a plugin of the default kit off, without declaring its marketplace
    assert cli("kits", "new", "quiet", "--agent", "claude")[0] == 0
    status, out, _ = cli("kits", "disable-plugin", "quiet", "lint@acme")
    assert status == 0 and "also where a kit before it enables it" in out
    files = [("home", default / "claude/marketplaces.json"),
             ("kit quiet", home / ".local/share/sandbox-ai/kits/quiet/claude/marketplaces.json")]
    _, enabled, _ = kits.declarations("claude", files)
    assert "lint@acme" not in enabled and "sandcastle@sandbox-ai" in enabled
    _, every, _ = kits.declarations("claude", files, with_disabled=True)  # what the plugin store installs
    assert "lint@acme" in every
    _, enabled, _ = kits.declarations("claude", files[:1])  # without the kit: on
    assert "lint@acme" in enabled
    plugin_state = plugin(by_key(listing())[("claude", "acme")], "lint@acme")["kits"]
    assert plugin_state == [{"kit": "default", "kind": "default", "enabled": True},
                            {"kit": "quiet", "kind": "user", "enabled": False}]
    # skills and the rest of the default kit stay read-only here; built-in kits entirely
    status, _, err = cli("kits", "new-skill", "default", "x")
    assert status == 1 and "edit it by hand" in err
    status, _, err = cli("kits", "disable-plugin", "egm", "egm@sandbox-ai")
    assert status == 1 and "built-in kit of sandbox-ai" in err


def test_a_plugin_both_on_and_off_in_one_kit_is_refused(home):
    write(home, ".local/share/sandbox-ai/kits/both/claude/marketplaces.json", json.dumps(
        {"marketplaces": {"sandbox-ai": {"source": {"source": "directory", "path": "/sandbox/marketplace"}}},
         "plugins": ["sandcastle@sandbox-ai"], "disabled": ["sandcastle@sandbox-ai"]}))
    data = listing()
    assert [e["kit"] for e in data["errors"]] == ["both"] and "keep one" in data["errors"][0]["error"]
    status, _, err = cli("kits", "enable-plugin", "both", "sandcastle@sandbox-ai")
    assert status == 1 and "keep one" in err


def test_fetching_the_marketplaces_only_installs_nothing(home):
    plugins = load("plugins_for_marketplace_tests", "sandbox/plugins.py")
    commands = []

    def runner(command):  # stands in for `claude plugin ...` in the store sandbox
        commands.append(command)
        args = command[command.index("--", command.index("--state-dir")) + 1:][1:]
        store = Path(command[command.index("--state-dir") + 1]) / ".claude/plugins"
        store.mkdir(parents=True, exist_ok=True)
        known = store / "known_marketplaces.json"
        entries = json.loads(known.read_text()) if known.exists() else {}
        if args[:2] == ["marketplace", "add"]:
            entries[{"/sandbox/marketplace": "sandbox-ai"}.get(args[2], args[2].split("/")[-1])] = {"source": {}}
        known.write_text(json.dumps(entries))
        return subprocess.CompletedProcess(command, 0)
    lines = []
    assert plugins.sync(out=lines.append, runner=runner, marketplaces_only=True) == 0
    assert sorted(c[-1] for c in commands) == ["/sandbox/marketplace", "anthropics/claude-plugins-official"]
    assert all(c[c.index("plugin", c.index("--state-dir")):][:3] == ["plugin", "marketplace", "add"] for c in commands)
    assert any("2 marketplace(s)" in line and "sandbox-ai plugins marketplaces" in line for line in lines)
    # one that could not be fetched (its marketplace.json gives another name) is said, and the status says so
    (home / ".local/share/sandbox-ai/plugins/claude/.claude/plugins/known_marketplaces.json").unlink()
    commands.clear()
    lines.clear()
    assert cli("kits", "add-marketplace", "default", "other", "acme/plugins-x")[0] == 0
    assert plugins.sync(out=lines.append, runner=runner, marketplaces_only=True) == 1
    assert any("not fetched: other" in line for line in lines)
    assert not any("install" in c for c in commands)
