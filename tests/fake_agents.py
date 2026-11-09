"""Claude's, Codex's and OpenCode's own plugin configuration as each keeps it, faked below a private HOME, for the
tests of `sandbox-ai plugins marketplaces` and the marketplace browser of the web (no network, nothing real).

  Claude    ~/.claude/plugins/known_marketplaces.json (with its fetched copy of acme), settings.json
            (enabledPlugins, extraKnownMarketplaces), plugins/installed_plugins.json
  Codex     ~/.codex/config.toml ([marketplaces.cx], [plugins."tool@cx"]), its copy in Claude's format under
            .tmp/marketplaces/cx, an installed plugin under plugins/cache
  OpenCode  ~/.config/opencode/opencode.jsonc ("plugin", with comments and a trailing comma), a plugin module
"""
import json
from pathlib import Path

ACME = {"name": "acme", "description": "Acme plugins", "owner": {"name": "acme"}, "plugins": [
    {"name": "lint", "source": "./plugins/lint", "description": "Lints", "version": "1.2", "category": "quality"},
    {"name": "fmt", "source": {"source": "github", "repo": "acme/fmt"}, "description": "Formats"}]}


def write(home, relative, text):
    path = Path(home) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def fake_agents(home):
    """HOME with the three agents' configuration; returns HOME."""
    home = Path(home)
    write(home, ".claude/plugins/marketplaces/acme/.claude-plugin/marketplace.json", json.dumps(ACME))
    write(home, ".claude/plugins/known_marketplaces.json", json.dumps({
        "acme": {"source": {"source": "github", "repo": "acme/plugins"},
                 "installLocation": str(home / ".claude/plugins/marketplaces/acme"), "lastUpdated": "2026-11-01"},
        "localdir": {"source": {"source": "directory", "path": "/opt/plugins"}, "installLocation": "/opt/plugins"}}))
    write(home, ".claude/settings.json", json.dumps({
        "enabledPlugins": {"lint@acme": True, "fmt@acme": False},
        "extraKnownMarketplaces": {"team": {"source": {"source": "git",
                                                       "url": "https://git.example.org/team/plugins.git"}}}}))
    write(home, ".claude/plugins/installed_plugins.json", json.dumps({
        "version": 2, "plugins": {"lint@acme": [{"scope": "user", "version": "1.2"}]}}))
    write(home, ".codex/config.toml", '[marketplaces.cx]\nsource_type = "git"\n'
                                      'source = "https://github.com/acme/cx-plugins.git"\n\n'
                                      '[plugins."tool@cx"]\nenabled = true\n')
    write(home, ".codex/.tmp/marketplaces/cx/.claude-plugin/marketplace.json", json.dumps(
        {"name": "cx-claude", "plugins": [{"name": "tool", "description": "A tool"}]}))
    (home / ".codex/plugins/cache/cx/tool/1.0").mkdir(parents=True)
    write(home, ".config/opencode/opencode.jsonc", '{\n  // the plugins\n  "plugin": ["opencode-foo@1.0",],\n}\n')
    write(home, ".config/opencode/plugin/guard.js", "export default {}\n")
    return home
