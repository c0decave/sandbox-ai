#!/usr/bin/env python3
"""sandbox-ai plugins: Claude plugin marketplaces and plugins, kept for all sandboxes.

  sandbox-ai plugins [--status]                  what the kits declare and what is installed
  sandbox-ai plugins claude [launcher options]   install what the default kit and every kit declare
  sandbox-ai plugins claude --update             also update the marketplaces and installed plugins
  sandbox-ai plugins claude --kit egm            also install what the built-in kit egm declares
  sandbox-ai plugins claude --marketplaces-only  fetch the declared marketplaces (their plugin lists), install nothing
  sandbox-ai plugins marketplaces [NAME] [--agent A] [--json]
                                                 every marketplace: sandbox-ai's own, the kits', the plugin store's and
                                                 those Claude, Codex and OpenCode already know on this machine, with
                                                 their plugins and each plugin's state (kits, store, the agent's own)
  sandbox-ai plugins claude -- ARGS              run `claude plugin ARGS` in the plugin store's sandbox
  sandbox-ai plugins claude trust-hooks PLUGIN [--yes|--revoke]
                                                 review a plugin's hooks and approve them: they run while the
                                                 agent's managed hooks are on (tool rules, kit hooks)

The kits declare marketplaces and plugins in AGENT/marketplaces.json ("plugins": enabled, "disabled": declared but
off; change them with sandbox-ai kits add-plugin, enable-plugin, disable-plugin, add-marketplace ...). Installing runs
`claude plugin ...` inside a sandbox whose HOME is the plugin store ~/.local/share/sandbox-ai/plugins/claude,
with network access to the marketplaces' hosts only. Every Claude sandbox then gets the store
read-only and enables the plugins its kits list, so no sandbox can change another one's plugins.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_plugins", HERE / f"{name}.py")
        _modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_modules[name])
    return _modules[name]


def kits():
    return module("kits")


def network(sources):
    """Launcher options that reach the hosts of SOURCES (marketplace sources) and nothing else."""
    options = []
    if any(source["source"] == "github" for source in sources):
        options.append("--allow-github")
    for host in sorted({kits().source_host(source) for source in sources} - {None}):
        options += ["--allow-host", host]
    return options


def plugin_sources(plugin, marketplace):
    """Sources whose hosts installing PLUGIN ("NAME@MARKETPLACE") needs. A plugin of sandbox-ai's own
    marketplace (a directory, no network) may live on GitHub, pinned in its marketplace.json; it then
    needs the GitHub preset too. Other marketplaces: their own source only."""
    if marketplace["source"] != "directory":
        return [marketplace]
    manifest = kits().read_store_json(kits().MARKETPLACE / ".claude-plugin" / "marketplace.json") or {}
    entries = manifest.get("plugins") if isinstance(manifest.get("plugins"), list) else []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == plugin.split("@")[0]:
            remote = entry.get("source")
            if isinstance(remote, dict) and remote.get("source") == "github":
                return [marketplace, {"source": "github", "repo": remote.get("repo")}]
    return [marketplace]


def store_command(args, options=(), sources=()):
    """The launcher command that runs `claude plugin ARGS` with the plugin store as HOME."""
    store = kits().plugin_store("claude")
    return [sys.executable, "-I", str(HERE / "agent_sandbox.py"), "claude", *options, *network(sources),
            "--no-auth", "--no-config", "--no-tmux", "--no-default-kit", "--no-memory", "--no-plugins",
            "--binary", "claude", "--state-dir", str(store), "--", "plugin", *args]


def run_in_store(args, options=(), sources=(), runner=None):
    """Run `claude plugin ARGS` in a sandbox on the plugin store; its exit status."""
    runner = runner or subprocess.run
    store = kits().plugin_store("claude")
    if store.is_symlink():
        raise ValueError(f"{store} is a symlink; refusing to use it")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    store.chmod(0o700)
    # A workspace of its own: the store sandbox never sees a project.
    work = Path(tempfile.mkdtemp(prefix=".work-", dir=store.parent))
    try:
        command = store_command(args, options, sources)
        position = command.index("--")
        command[position:position] = ["--workspace", str(work)]
        return runner(command).returncode
    finally:
        shutil.rmtree(work, ignore_errors=True)


def sync(options=(), update=False, out=None, runner=None, builtin=(), marketplaces_only=False):
    """Install every marketplace and plugin the kits declare, enabled or disabled (and the built-in kits named in
    BUILTIN); with UPDATE also refresh them; with MARKETPLACES_ONLY only fetch (or refresh) the marketplaces, so
    their plugin lists can be browsed. 0 if all is there."""
    out = out or (lambda line: print(line, file=sys.stderr))
    k = kits()
    if k.ensure_default_home("claude"):
        out(f"sandbox-ai plugins: created the default kit {k.default_home('claude')} with examples")
    # Each marketplaces.json checked link by link: one reached through a symlink an agent planted would choose the
    # plugins (programs) this installs.
    marketplaces, plugins, notes = k.declarations("claude", k.all_plugin_files("claude", builtin), with_disabled=True,
                                                  check=module("agent_sandbox").kit_check())
    for note in notes:
        out(f"sandbox-ai plugins: {note}")
    known, _ = k.installed()
    for name, source in marketplaces.items():
        if name in known:
            continue
        out(f"sandbox-ai plugins: adding marketplace {name} ({k.source_argument(source)})")
        runner_status = run_in_store(["marketplace", "add", k.source_argument(source)], options, [source], runner)
        known, _ = k.installed()
        if runner_status == 0 and name not in known:
            out(f"sandbox-ai plugins: the marketplace from {k.source_argument(source)} is not called {name}; "
                "use the name its marketplace.json gives in marketplaces.json")
    if update and known:
        out("sandbox-ai plugins: updating the marketplaces")
        run_in_store(["marketplace", "update"], options,
                     [marketplaces[name] for name in known if name in marketplaces], runner)
    if marketplaces_only:
        known, _ = k.installed()
        missing = [name for name in marketplaces if name not in known]
        if missing:
            out(f"sandbox-ai plugins: not fetched: {', '.join(missing)}")
            return 1
        out(f"sandbox-ai plugins: {len(marketplaces)} marketplace(s) in {k.plugin_store('claude')}; see their plugins "
            "with: sandbox-ai plugins marketplaces")
        return 0
    known, installed = k.installed()
    for plugin in plugins:
        source = plugin_sources(plugin, marketplaces[plugin.split("@")[1]])
        if plugin in installed:
            if update:
                out(f"sandbox-ai plugins: updating {plugin}")
                run_in_store(["update", plugin], options, source, runner)
            continue
        if plugin.split("@")[1] not in known:
            continue  # its marketplace failed above
        out(f"sandbox-ai plugins: installing {plugin}")
        if run_in_store(["install", plugin], options, source, runner) != 0 and not update:
            # Claude installs from its copy of the marketplace's plugin list; one fetched by an older version may
            # lack the plugin. Refreshed once, then tried again.
            name = plugin.split("@")[1]
            out(f"sandbox-ai plugins: updating the marketplace {name} and trying {plugin} again")
            run_in_store(["marketplace", "update", name], options, [marketplaces[name]], runner)
            run_in_store(["install", plugin], options, source, runner)
    known, installed = k.installed()
    missing = [p for p in plugins if p not in installed] + [f"marketplace {m}" for m in marketplaces if m not in known]
    if missing:
        out(f"sandbox-ai plugins: not installed: {', '.join(missing)}")
        return 1
    out(f"sandbox-ai plugins: {len(installed)} plugin(s) in {k.plugin_store('claude')}; every Claude sandbox "
        "enables those its kits list")
    return 0


def status(out=print):
    k = kits()
    known, installed = k.installed()
    out(f"claude    plugin store {k.plugin_store('claude')}")
    out(f"          marketplaces: {', '.join(sorted(known)) or 'none'}")
    out(f"          plugins: {', '.join(sorted(installed)) or 'none'}")
    try:
        marketplaces, plugins, _ = k.declarations("claude", k.all_plugin_files("claude"), with_disabled=True)
    except ValueError as exc:
        out(f"          kits: {exc}")
        return 1
    for label, path in k.all_plugin_files("claude"):
        document = k.read_declaration(label, path)
        own = [(p, "") for p in document.get("plugins", []) if p in plugins]
        own += [(p, ", off") for p in document.get("disabled", []) if p in plugins]
        state = ", ".join(p + (f" ({off[2:]})" if off and p in installed else "" if p in installed
                               else f" (not installed{off})") for p, off in own) or "no plugins"
        out(f"          {label}: {state}")
    missing = [p for p in plugins if p not in installed]
    if missing:
        out("          -> sandbox-ai plugins claude")
    for name in k.builtin_kits("claude"):
        path = k.BUILTIN_KITS / name / "claude" / k.PLUGINS_FILE
        if path.is_file():
            own = k.read_declaration(f"built-in kit {name}", path).get("plugins", [])
            state = ", ".join(f"{p}{'' if p in installed else ' (not installed)'}" for p in own) or "no plugins"
            hint = "" if all(p in installed for p in own) else f" -> sandbox-ai plugins claude --kit {name}"
            out(f"          built-in kit {name} (--kit {name}): {state}{hint}")
    for agent in ("opencode", "codex"):
        try:
            _, listed, notes = k.declarations(agent, k.all_plugin_files(agent))
        except ValueError as exc:
            out(f"{agent:9} {exc}")
            continue
        if agent == "opencode":
            out(f"opencode  npm plugins from the kits: {', '.join(listed) or 'none'} (loaded by OpenCode itself; "
                "needs --allow-host registry.npmjs.org)")
        else:
            out("codex     plugins: not supported yet" + (f" ({len(notes)} kit(s) declare some)" if notes else ""))
    return 0


REVIEW_LIMIT = 64 << 10  # a file trust-hooks shows in full


def printable(text):
    """TEXT safe for a terminal: the launcher's printable (one implementation for every command)."""
    return module("agent_sandbox").printable(str(text))


def plugin_hooks(plugin):
    """read_plugin_hooks of the installed PLUGIN (NAME@MARKETPLACE)."""
    k = kits()
    _, installed = k.installed()
    if plugin not in installed:
        raise ValueError(f"{plugin} is not in the plugin store; see: sandbox-ai plugins --status")
    where = k.plugin_install(plugin, installed[plugin])
    if where is None:
        raise ValueError(f"{plugin}: the plugin store does not say where it is installed")
    info = k.read_plugin_hooks(plugin, *where)
    if not info["declared"]:
        raise ValueError(f"{plugin} has no hooks")
    return info


def review(info, out):
    """Each hook of the plugin, the full text of every file its commands name, and what else its folder holds."""
    k = kits()
    out(f"{info['plugin']}: hooks in {printable(info['path'])}")
    named = set()
    for hook in info["hooks"]:
        out(f"  {hook['event']}, matcher {printable(hook['matcher'] or 'every tool')}: {printable(hook['command'])}")
        for item in info["files"]:
            if f"{k.PLUGIN_ROOT_VARIABLE}/{item['path']}" in hook["command"] or \
                    f"$CLAUDE_PLUGIN_ROOT/{item['path']}" in hook["command"]:
                named.add(item["path"])
    for note in info["notes"]:
        out(f"  note: {printable(note)}")
    for error in info["errors"]:
        out(f"  error: {printable(error)}")
    for item in info["files"]:
        if item["path"] not in named:
            continue
        out("")
        out(f"  --- {printable(item['path'])} ({item['size']} bytes{', executable' if item['executable'] else ''}, "
            f"sha256 {item['sha256'][:16]})")
        data, _ = k.read_at_path(info["path"], item["path"]) if item["size"] <= REVIEW_LIMIT else (None, None)
        if data is None:
            out("  (larger than 64 KiB: read it yourself before approving)")
            continue
        for line in data.decode("utf-8", "replace").splitlines()[:400]:
            out(f"  | {printable(line)}")
    others = [f for f in info["files"] if f["path"] not in named]
    size = sum(f["size"] for f in others)
    out("")
    out(f"  and {len(others)} other file(s), {size >> 10} KiB, which the hooks may run too; the approval covers "
        f"them all (read them in {printable(info['path'])})")


def trust_hooks(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai plugins claude trust-hooks",
                                     description="Review a Claude plugin's hooks and approve them")
    parser.add_argument("plugin", metavar="NAME@MARKETPLACE")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--yes", action="store_true", help="approve what is shown without asking")
    choice.add_argument("--revoke", action="store_true", help="take the approval back")
    a = parser.parse_args(argv)
    k = kits()
    info = plugin_hooks(a.plugin)
    key = k.plugin_trust_key(a.plugin)
    if a.revoke:
        k.change_trust("claude", info, key, approve=False)
        out(f"{a.plugin}: the approval of its hooks is taken back")
        return 0
    review(info, out)
    if info["errors"]:
        raise ValueError(f"these hooks cannot be approved: {printable(info['errors'][0])}")
    if not a.yes:
        if not sys.stdin.isatty():
            raise ValueError("review the hooks above, then approve them with --yes")
        answer = input("Approve exactly these hooks and this plugin's files? Any change takes the approval away. "
                       "[y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            out("not approved")
            return 1
    k.change_trust("claude", info, key)
    out(f"{a.plugin}: {len(info['hooks'])} hook(s) approved (sha256 {info['sha256'][:16]}); they run while the "
        "managed hooks are on, until the plugin changes")
    return 0


# --- every marketplace: sandbox-ai's own, the kits', the plugin store's and the agents' own on this machine -----------
#
# `sandbox-ai plugins marketplaces --json` (the web's and the app's marketplace browser read it). Nothing here
# fetches anything or runs anything: it reads the kits, sandbox-ai's plugin store (written by its own sandbox, so
# never through a symlink) and the agents' own configuration (Claude: ~/.claude, Codex: ~/.codex/config.toml,
# OpenCode: ~/.config/opencode), each file size-limited, a regular file, and a missing or damaged one is a note,
# never a failure. A marketplace's plugin list comes from a copy fetched earlier: sandbox-ai's own, the plugin
# store's (sandbox-ai plugins claude --marketplaces-only fetches one in a sandbox) or the agent's own.

MARKETPLACES_VERSION = 1
CONFIG_LIMIT = 4 << 20      # an agent's own configuration file
MANIFEST_LIMIT = 16 << 20   # a marketplace.json
TEXT_LIMIT = 500            # a description as listed
PLUGIN_LIMIT = 5000         # plugins listed per marketplace
CLAUDE_MANIFEST = ".claude-plugin/marketplace.json"
# Where Codex looks for a marketplace's manifest below its root (codex-rs/core-plugins/src/marketplace.rs).
CODEX_MANIFESTS = (".agents/plugins/marketplace.json", ".agents/plugins/api_marketplace.json", CLAUDE_MANIFEST,
                   ".cursor-plugin/marketplace.json")
STORE_MARKETPLACES = "/root/.claude/plugins/marketplaces/"  # installLocation of a fetched one in the store sandbox
OPENCODE_FILES = ("opencode.json", "opencode.jsonc", "config.json")
OPENCODE_SUFFIXES = (".js", ".mjs", ".ts")
GITHUB_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?")


def claude_home():
    """Claude Code's own configuration directory on this machine."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def opencode_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"


def clean(text, limit=TEXT_LIMIT):
    """TEXT as one printable line of at most LIMIT characters; None for anything but a string."""
    if not isinstance(text, str):
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def read_config(path, limit=CONFIG_LIMIT):
    """(bytes, None) of the regular file PATH (the user's own: a symlink to a dotfiles checkout is followed), (None,
    None) when it is missing, (None, why) when it cannot be read. Never blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as exc:
        return None, f"{path}: {exc.strerror}"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, f"{path}: not a regular file"
        if info.st_size > limit:
            return None, f"{path}: larger than {limit >> 20} MiB"
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        return None, f"{path}: {exc.strerror}"
    finally:
        os.close(fd)
    if len(data) > limit:
        return None, f"{path}: larger than {limit >> 20} MiB"
    return data, None


def strip_jsonc(text):
    """TEXT (JSON with comments, as OpenCode's opencode.jsonc) as JSON: // and /* */ comments and trailing commas
    outside strings removed."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1
                continue
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_json(data, where, problems, jsonc=False):
    """The JSON object in DATA (bytes), or None with a note in PROBLEMS."""
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
        document = json.loads(strip_jsonc(text) if jsonc else text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        problems.append(f"{where}: not valid JSON; left out")
        return None
    if not isinstance(document, dict):
        problems.append(f"{where}: not a JSON object; left out")
        return None
    return document


def config_json(path, problems, jsonc=False):
    data, why = read_config(path)
    if why:
        problems.append(why)
        return None
    return parse_json(data, path, problems, jsonc)


def store_manifest(name, entry, problems):
    """The marketplace.json of the marketplace NAME in sandbox-ai's plugin store (ENTRY: its known_marketplaces.json
    entry), read without following any symlink (the store's own sandbox wrote it); None if not there."""
    k = kits()
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    if source.get("source") == "directory" and source.get("path") == k.MARKETPLACE_TARGET:
        return k.read_store_json(k.MARKETPLACE / CLAUDE_MANIFEST)
    root = k.plugin_dir("claude")
    if root is None:
        return None
    folder = name
    location = entry.get("installLocation")
    if isinstance(location, str) and location.startswith(STORE_MARKETPLACES):
        folder = location[len(STORE_MARKETPLACES):].strip("/")
    if not re.fullmatch(k.PLUGIN_NAME, folder):
        return None
    try:
        fd = k.open_dir(root, ("marketplaces", folder, ".claude-plugin"))
    except (OSError, ValueError):
        problems.append(f"plugin store: marketplace {name}: a symlink or not a directory; left out")
        return None
    if fd is None:
        return None
    try:
        data, why = k.read_at(fd, "marketplace.json", MANIFEST_LIMIT)
    finally:
        os.close(fd)
    if why:
        problems.append(f"plugin store: marketplace {name}: marketplace.json: {why}")
        return None
    return parse_json(data, f"plugin store: marketplace {name}", problems)


def manifest_at(root, candidates, problems):
    """(the first marketplace manifest of CANDIDATES below ROOT, its relative path), or (None, None)."""
    for relative in candidates:
        data, why = read_config(Path(root) / relative, MANIFEST_LIMIT)
        if why:
            problems.append(why)
            return None, None
        if data is not None:
            return parse_json(data, Path(root) / relative, problems), relative
    return None, None


def plugin_source(source):
    """"local", "github:OWNER/REPO" or KIND:ARGUMENT of a plugin's source in a marketplace.json."""
    if isinstance(source, str):
        return "local"
    if isinstance(source, dict) and isinstance(source.get("source"), str):
        argument = next((source[key] for key in ("repo", "url", "package", "path") if isinstance(source.get(key), str)),
                        "")
        return clean(f"{source['source']}:{argument}" if argument else source["source"], 300)
    return "unknown"


def manifest_plugins(document, marketplace):
    """{plugin id: listing} of the plugins a marketplace.json (DOCUMENT) offers."""
    k = kits()
    entries = document.get("plugins") if isinstance(document, dict) and isinstance(document.get("plugins"), list) else []
    found = {}
    for entry in entries[:PLUGIN_LIMIT]:
        if not (isinstance(entry, dict) and isinstance(entry.get("name"), str)
                and re.fullmatch(k.PLUGIN_NAME, entry["name"])):
            continue
        interface = entry.get("interface") if isinstance(entry.get("interface"), dict) else {}
        found[f"{entry['name']}@{marketplace}"] = {
            "description": clean(entry.get("description")) or clean(interface.get("shortDescription")) or "",
            "version": clean(entry.get("version"), 64), "category": clean(entry.get("category"), 64),
            "homepage": clean(entry.get("homepage"), 300), "source": plugin_source(entry.get("source"))}
    return found


def kit_source(name, source):
    """(the source a kit declares for the marketplace NAME whose agent-side source is SOURCE, None) when sandbox-ai's
    kits can take it (kits.check_source: GitHub, https Git or URL, sandbox-ai's own), else (None, why)."""
    if not isinstance(source, dict):
        return None, "its source is not known"
    try:
        return kits().check_source(name, dict(source)), None
    except ValueError as exc:
        extra = sorted(set(source) - {"source", "repo", "url", "path"})
        if extra:
            return None, (f"its source has {', '.join(extra)}, which sandbox-ai's kits do not take (they declare a "
                          "GitHub repository, an https Git or marketplace.json URL, or sandbox-ai's own)")
        if source.get("source") in ("directory", "file"):
            return None, ("it is a folder or file on this machine; sandboxes get only sandbox-ai's own marketplace as "
                          "a folder: publish it on a Git server or GitHub")
        return None, str(exc).split(": ", 1)[-1]


def source_value(source):
    """What `sandbox-ai kits add-marketplace` and `add-plugin --source` take for SOURCE (a kit's source)."""
    return "sandbox-ai" if source.get("source") == "directory" else kits().source_argument(source)


def kit_files(agent):
    """[(kit entry, label, path)] of every kit that may declare plugins for AGENT: the default kit, sandbox-ai's
    built-in kits and yours (the kit entry: {"name", "kind", "editable"})."""
    k = kits()
    own = k.kit_names(k.kits_dir())
    found = [({"name": "default", "kind": "default", "editable": "default" not in own}, "home",
              k.default_home(agent) / k.PLUGINS_FILE)]
    found += [({"name": name, "kind": "builtin", "editable": False}, f"built-in kit {name}",
               k.BUILTIN_KITS / name / agent / k.PLUGINS_FILE) for name in k.kit_names(k.BUILTIN_KITS)]
    found += [({"name": name, "kind": "user", "editable": not (k.kits_dir() / name).is_symlink()}, f"kit {name}",
               k.kits_dir() / name / agent / k.PLUGINS_FILE) for name in own]
    return found


def kit_declarations(agent, errors):
    """[(kit entry, {"marketplaces": {name: source}, "plugins": [...], "disabled": [...]})] of the kits that declare
    something for AGENT; a damaged file is an error of ERRORS ({"kit", "agent", "error"}), its kit left out."""
    k = kits()
    found = []
    for entry, label, path in kit_files(agent):
        if not path.is_file():
            continue
        try:
            document = k.read_declaration(label, path)
            k.declarations(agent, [(label, path)])
        except (OSError, ValueError, RecursionError) as exc:
            errors.append({"kit": entry["name"], "agent": agent, "error": clean(str(exc), 1000)})
            continue
        declared = document.get("marketplaces", {}) if agent == "claude" else {}
        found.append((entry, {"marketplaces": {name: value["source"] for name, value in declared.items()},
                              "plugins": list(document.get("plugins", [])),
                              "disabled": list(document.get("disabled", []))}))
    return found


def kit_states(declared, plugin):
    """[{"kit", "kind", "enabled"}] of the kits (DECLARED: kit_declarations) that name PLUGIN."""
    return [{"kit": entry["name"], "kind": entry["kind"], "enabled": plugin in document["plugins"]}
            for entry, document in declared if plugin in document["plugins"] or plugin in document["disabled"]]


def toml_module():
    """tomllib (Python 3.11 and later) or the tomli package; None without either."""
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def codex_config(problems):
    """Codex's config.toml as a dict ({} when missing or unreadable, with a note)."""
    path = codex_home() / "config.toml"
    data, why = read_config(path)
    if why:
        problems.append(why)
        return {}
    if data is None:
        return {}
    tomllib = toml_module()
    if tomllib is None:
        problems.append(f"{path}: reading TOML needs Python 3.11 or the tomli package; Codex's marketplaces are left out")
        return {}
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        problems.append(f"{path}: not valid TOML ({clean(str(exc), 200)}); left out")
        return {}


def subdirectories(path):
    """Names of the directories in PATH (symlinks not followed); [] when it cannot be read."""
    try:
        with os.scandir(path) as entries:
            return sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def plugin_rows(name, listing, ids, state):
    """The plugins of the marketplace NAME: those LISTING offers (manifest_plugins) and the other IDS named
    somewhere (a kit, a store, the agent), each with STATE(id)."""
    rows = []
    for plugin in list(listing) + [i for i in dict.fromkeys(ids) if i not in listing]:
        known = listing.get(plugin)
        rows.append({"name": plugin.split("@")[0], "id": plugin, "listed": known is not None,
                     **(known or {"description": "", "version": None, "category": None, "homepage": None,
                                  "source": None}), **state(plugin)})
    return rows


def claude_marketplaces(declared, problems):
    """The Claude marketplaces: sandbox-ai's own, those the kits declare, those in sandbox-ai's plugin store and
    those Claude itself knows on this machine (known_marketplaces.json, extraKnownMarketplaces), and the default
    kit's examples (suggested)."""
    k = kits()
    known, installed = k.installed()
    home = claude_home()
    own_known = config_json(home / "plugins" / "known_marketplaces.json", problems) or {}
    settings = config_json(home / "settings.json", problems) or {}
    own_installed = config_json(home / "plugins" / "installed_plugins.json", problems) or {}
    own_installed = own_installed.get("plugins") if isinstance(own_installed.get("plugins"), dict) else {}
    enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    extra = settings.get("extraKnownMarketplaces") if isinstance(settings.get("extraKnownMarketplaces"), dict) else {}
    template = k.read_store_json(k.TEMPLATES / "claude" / k.PLUGINS_FILE) or {}
    suggested = template.get("marketplaces") if isinstance(template.get("marketplaces"), dict) else {}
    valid = lambda name: isinstance(name, str) and re.fullmatch(k.PLUGIN_NAME, name)  # noqa: E731
    names = ["sandbox-ai"] + [n for _, d in declared for n in d["marketplaces"]] + list(known) + \
        [n for n in own_known if isinstance(own_known[n], dict)] + [n for n in extra if isinstance(extra[n], dict)] + \
        list(suggested)
    result = []
    for name in dict.fromkeys(n for n in names if valid(n)):
        kits_of = [{"kit": entry["name"], "kind": entry["kind"], "source": source_value(d["marketplaces"][name])}
                   for entry, d in declared if name in d["marketplaces"]]
        sources = [d["marketplaces"][name] for _, d in declared if name in d["marketplaces"]]
        agent_entry = own_known.get(name) if isinstance(own_known.get(name), dict) else None
        extra_entry = extra.get(name) if isinstance(extra.get(name), dict) else None
        for entry in (agent_entry, extra_entry, known.get(name),
                      suggested.get(name) if isinstance(suggested.get(name), dict) else None):
            if entry and isinstance(entry.get("source"), dict):
                sources.append(entry["source"])
        builtin = name == "sandbox-ai"
        if builtin:
            sources.insert(0, {"source": "directory", "path": k.MARKETPLACE_TARGET})
        source, problem = kit_source(name, sources[0] if sources else None)
        # the plugin list: sandbox-ai's own, the store's copy, Claude's own copy
        listing, listed_from, description = {}, None, None
        manifest = None
        if builtin:
            manifest, listed_from = k.read_store_json(k.MARKETPLACE / CLAUDE_MANIFEST), "sandbox-ai"
        if manifest is None and name in known:
            manifest, listed_from = store_manifest(name, known[name], problems), "store"
        if manifest is None and agent_entry:
            location = agent_entry.get("installLocation")
            root = Path(location) if isinstance(location, str) and location.startswith("/") else \
                home / "plugins" / "marketplaces" / name
            if root.suffix == ".json":
                data, why = read_config(root, MANIFEST_LIMIT)
                problems.extend([why] if why else [])
                manifest = parse_json(data, root, problems)
            else:
                manifest, _ = manifest_at(root, (CLAUDE_MANIFEST,), problems)
            listed_from = "claude"
        if manifest is not None:
            listing = manifest_plugins(manifest, name)
            metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
            description = clean(manifest.get("description")) or clean(metadata.get("description"))
        else:
            listed_from = None
        ids = [p for _, d in declared for p in d["plugins"] + d["disabled"] if p.endswith(f"@{name}")]
        ids += [p for p in installed if p.endswith(f"@{name}")]
        ids += [p for p in list(own_installed) + list(enabled) if isinstance(p, str) and p.endswith(f"@{name}")
                and re.fullmatch(f"{k.PLUGIN_NAME}@{k.PLUGIN_NAME}", p)]

        def state(plugin):
            versions = installed.get(plugin)
            version = next((e.get("version") for e in versions if isinstance(e, dict)), None) \
                if isinstance(versions, list) else None
            switch = enabled.get(plugin)
            return {"kits": kit_states(declared, plugin),
                    "store": {"installed": plugin in installed, "version": clean(version, 64)},
                    "agent": {"installed": plugin in own_installed,
                              "enabled": switch if isinstance(switch, bool) else None}}
        origins = (["sandbox-ai"] if builtin else []) + (["kit"] if kits_of else []) + \
            (["store"] if name in known else []) + (["claude"] if agent_entry or extra_entry else []) + \
            (["suggested"] if name in suggested else [])
        result.append({
            "name": name, "agent": "claude", "builtin": builtin, "origins": origins, "description": description,
            "source": source, "source_value": source_value(source) if source else None,
            "importable": source is not None, "problem": problem, "import": None,
            "kits": kits_of, "store": {"fetched": name in known},
            "agent_config": {"known": agent_entry is not None, "settings": extra_entry is not None,
                             "fetched": listed_from == "claude"} if agent_entry or extra_entry else None,
            "listed_from": listed_from, "plugins": plugin_rows(name, listing, ids, state)})
    return result


def codex_import(name, entry, manifest_path, manifest):
    """(what importing the Codex marketplace NAME into a kit declares: {"name", "source", "source_value"}, None),
    or (None, why): only a Git marketplace over https without a ref or sparse paths whose copy is in Claude's format
    (.claude-plugin/marketplace.json), declared for Claude under the name its manifest gives."""
    if entry is None:
        return None, "Codex's curated marketplace is in Codex's own format"
    if entry.get("source_type") != "git" or not isinstance(entry.get("source"), str):
        return None, ("it is a folder on this machine; sandboxes get only sandbox-ai's own marketplace as a folder: "
                      "publish it on a Git server or GitHub")
    if entry.get("ref") or entry.get("sparse_paths"):
        return None, "it is pinned to a ref or sparse paths, which sandbox-ai's kits do not take"
    if manifest_path != CLAUDE_MANIFEST or not isinstance(manifest, dict):
        return None, ("its copy is not in Claude's format (.claude-plugin/marketplace.json), or Codex has not fetched "
                      "it; sandbox-ai's kits give Codex no plugins yet")
    claude_name = manifest.get("name")
    if not (isinstance(claude_name, str) and re.fullmatch(kits().PLUGIN_NAME, claude_name)):
        return None, "its marketplace.json has no valid name"
    url = entry["source"]
    github = GITHUB_URL.fullmatch(url)
    source = {"source": "github", "repo": github[1]} if github else {"source": "git", "url": url}
    try:
        source = kits().check_source(claude_name, source)
    except ValueError as exc:
        return None, str(exc).split(": ", 1)[-1]
    return {"name": claude_name, "source": source, "source_value": source_value(source)}, None


def codex_marketplaces(problems):
    """Codex's marketplaces on this machine ([marketplaces.NAME] of ~/.codex/config.toml, fetched to
    ~/.codex/.tmp/marketplaces/NAME, and the curated one Codex syncs to ~/.codex/.tmp/plugins) with their plugins
    and Codex's own state ([plugins."NAME@MARKETPLACE"] enabled, ~/.codex/plugins/cache/MARKETPLACE/NAME)."""
    k = kits()
    home = codex_home()
    config = codex_config(problems)
    configured = config.get("marketplaces") if isinstance(config.get("marketplaces"), dict) else {}
    switches = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    cache = home / "plugins" / "cache"
    found = []
    for name, entry in configured.items():
        if not (isinstance(entry, dict) and re.fullmatch(k.PLUGIN_NAME, name)):
            continue
        root = Path(entry["source"]) if entry.get("source_type") == "local" and isinstance(entry.get("source"), str) \
            else home / ".tmp" / "marketplaces" / name
        found.append((name, entry, root))
    curated = home / ".tmp" / "plugins"
    if curated.is_dir():
        found.append((None, None, curated))
    result = []
    for name, entry, root in found:
        manifest, relative = manifest_at(root, CODEX_MANIFESTS, problems)
        if name is None:  # the curated marketplace: its name is its manifest's
            name = manifest.get("name") if isinstance(manifest, dict) else None
            if not (isinstance(name, str) and re.fullmatch(k.PLUGIN_NAME, name)):
                continue
        listing = manifest_plugins(manifest, name) if manifest else {}
        ids = [p for p in switches if isinstance(p, str) and p.endswith(f"@{name}")]
        ids += [f"{plugin}@{name}" for plugin in subdirectories(cache / name) if re.fullmatch(k.PLUGIN_NAME, plugin)]
        installed = set(subdirectories(cache / name))

        def state(plugin, installed=installed):
            switch = switches.get(plugin) if isinstance(switches.get(plugin), dict) else {}
            return {"kits": [], "store": None,
                    "agent": {"installed": plugin.split("@")[0] in installed,
                              "enabled": switch.get("enabled") if isinstance(switch.get("enabled"), bool) else None}}
        target, problem = codex_import(name, entry, relative, manifest)
        raw = {key: entry[key] for key in ("source_type", "source", "ref", "sparse_paths") if key in entry} \
            if entry else {"source_type": "curated", "source": "https://github.com/openai/plugins.git"}
        interface = manifest.get("interface") if isinstance(manifest, dict) and isinstance(manifest.get("interface"),
                                                                                             dict) else {}
        result.append({
            "name": name, "agent": "codex", "builtin": False, "origins": ["codex" if entry else "codex-curated"],
            "description": clean(interface.get("displayName")), "source": None,
            "source_value": raw.get("source") if isinstance(raw.get("source"), str) else None,
            "codex_source": json.loads(json.dumps(raw, default=str)),
            "importable": target is not None, "problem": problem, "import": target, "kits": [], "store": None,
            "agent_config": {"known": entry is not None, "settings": False, "fetched": manifest is not None},
            "listed_from": "codex" if manifest is not None else None,
            "plugins": plugin_rows(name, listing, ids, state)})
    return result


def opencode_plugins(declared, problems):
    """OpenCode's npm plugins: those its own configuration lists ("plugin" in opencode.json) and those the kits
    declare, with their state; and the plugin modules in its plugin folders (kits take those as hooks)."""
    k = kits()
    directory, config, where = opencode_dir(), {}, None
    for name in OPENCODE_FILES:
        data, why = read_config(directory / name)
        if why:
            problems.append(why)
        if data is not None:
            config = parse_json(data, directory / name, problems, jsonc=True) or {}
            where = directory / name
            break
    listed = config.get("plugin") if isinstance(config.get("plugin"), list) else []
    listed = [p for p in listed if isinstance(p, str) and 0 < len(p) <= 300]
    ids = list(dict.fromkeys([p for _, d in declared for p in d["plugins"] + d["disabled"]] + listed))
    plugins = [{"name": clean(p, 300), "id": clean(p, 300), "importable": bool(re.fullmatch(k.NPM_PLUGIN, p)),
                "kits": kit_states(declared, p), "agent": {"configured": p in listed}} for p in ids]
    modules = []
    for folder in ("plugin", "plugins"):
        try:
            with os.scandir(directory / folder) as entries:
                modules += [str(directory / folder / e.name) for e in entries
                            if e.name.endswith(OPENCODE_SUFFIXES) and e.is_file()]
        except OSError:
            continue
    return plugins, sorted(modules)[:200], where


def marketplaces(agent=None, name=None):
    """Everything `sandbox-ai plugins marketplaces --json` prints (only AGENT's and the marketplaces called NAME, if
    given)."""
    k = kits()
    errors, notes = [], {a: [] for a in k.AGENTS}
    declared = {a: kit_declarations(a, errors) for a in ("claude", "opencode")}
    listed = []
    claude_config = codex_file = opencode_file = None
    if agent in (None, "claude"):
        listed += claude_marketplaces(declared["claude"], notes["claude"])
        claude_config = str(claude_home())
    if agent in (None, "codex"):
        listed += codex_marketplaces(notes["codex"])
        codex_file = str(codex_home() / "config.toml")
    oc_plugins, oc_modules, oc_where = opencode_plugins(declared["opencode"], notes["opencode"]) \
        if agent in (None, "opencode") else ([], [], None)
    if name is not None:
        listed = [m for m in listed if m["name"] == name]
    known_kits = {}
    for a in k.AGENTS:
        for entry, _, path in kit_files(a):
            kit = known_kits.setdefault((entry["kind"], entry["name"]), dict(entry, agents=[]))
            if path.parent.is_dir():
                kit["agents"].append(a)
    agents = {
        "claude": {"marketplaces": True, "kits": True, "config": claude_config, "about": None,
                   "notes": notes["claude"]},
        "codex": {"marketplaces": True, "kits": False, "config": codex_file,
                  "about": "sandbox-ai's kits give Codex no plugins yet: Codex's marketplaces and plugins are shown as "
                           "Codex has them on this machine; one in Claude's format can be imported for Claude",
                  "notes": notes["codex"]},
        "opencode": {"marketplaces": False, "kits": True, "config": str(oc_where) if oc_where else None,
                     "about": "OpenCode has no marketplaces: its plugins are npm packages (\"plugin\" in opencode.json), "
                              "which a kit lists and OpenCode installs itself (it needs --allow-host "
                              "registry.npmjs.org)",
                     "notes": notes["opencode"], "plugins": oc_plugins, "modules": oc_modules}}
    if agent:
        agents = {agent: agents[agent]}
    return {"version": MARKETPLACES_VERSION, "store": str(k.plugin_store("claude")),
            "kits": sorted(known_kits.values(), key=lambda e: (["default", "builtin", "user"].index(e["kind"]),
                                                                e["name"])),
            "agents": agents, "marketplaces": listed, "errors": errors}


def state_words(plugin):
    """What the text listing says about one plugin's state."""
    words = [f"{kit['kit']}{'' if kit['enabled'] else ' (off)'}" for kit in plugin["kits"]]
    parts = [f"kits: {', '.join(words)}"] if words else []
    if plugin.get("store") and plugin["store"]["installed"]:
        parts.append("in the store")
    agent = plugin.get("agent") or {}
    if agent.get("installed") or agent.get("enabled") is not None:
        parts.append("agent: " + ("installed" if agent.get("installed") else "not installed")
                     + ({True: ", enabled", False: ", disabled"}.get(agent.get("enabled"), "")))
    return "; ".join(parts)


def render_marketplaces(data, out, detail=False):
    for market in data["marketplaces"]:
        where = ", ".join(market["origins"])
        source = market["source_value"] or "?"
        out(f"{market['agent']:8} {printable(market['name'])} ({printable(source)}; {where})")
        if market["description"]:
            out(f"         {printable(market['description'])}")
        if market["kits"]:
            out("         declared in: " + ", ".join(printable(k["kit"]) for k in market["kits"]))
        if market["problem"]:
            out(f"         not for a kit: {printable(market['problem'])}")
        if market["import"]:
            out(f"         import for Claude as {market['import']['name']}: sandbox-ai kits import-marketplace KIT "
                f"{market['name']} --from codex")
        if market["agent"] == "claude" and not market["store"]["fetched"] and market["kits"]:
            out("         not fetched yet: sandbox-ai plugins claude --marketplaces-only")
        shown = [p for p in market["plugins"] if detail or state_words(p)]
        count = len(market["plugins"])
        out(f"         {count} plugin(s)" + ("" if market["listed_from"] else " (its plugin list is not fetched)")
            + (":" if shown else ""))
        for plugin in shown:
            state = state_words(plugin)
            line = f"           {printable(plugin['id'])}" + (f" [{printable(state)}]" if state else "")
            if detail and plugin["description"]:
                line += f": {printable(plugin['description'])}"
            out(line)
    for agent, info in data["agents"].items():
        for note in ([info["about"]] if info["about"] else []) + info["notes"]:
            out(f"{agent:8} note: {printable(note)}")
        if agent == "opencode":
            for plugin in info["plugins"]:
                out(f"opencode npm plugin {printable(plugin['name'])}"
                    + (f" [{printable(state_words(plugin))}]" if state_words(plugin) else "")
                    + (" (in opencode.json)" if plugin["agent"]["configured"] else ""))
            for path in info["modules"]:
                out(f"opencode plugin module {printable(path)} (a kit takes it as a hook: sandbox-ai kits add-hook KIT "
                    "FILE --agent opencode)")
    for error in data["errors"]:
        out(f"error: kit {printable(error['kit'])} ({error['agent']}): {printable(error['error'])}")
    if not data["marketplaces"]:
        out("no marketplace" + (" of that name" if detail else ""))


def marketplaces_parser():
    parser = argparse.ArgumentParser(prog="sandbox-ai plugins marketplaces",
                                     description="Every plugin marketplace: sandbox-ai's own, the kits', the plugin "
                                                 "store's and those Claude, Codex and OpenCode know on this machine, "
                                                 "with their plugins and each plugin's state. Read only: nothing is "
                                                 "fetched, run or changed (the agents' own configuration included).",
                                     epilog="Change a kit with: sandbox-ai kits add-plugin, remove-plugin, "
                                            "enable-plugin, disable-plugin, add-marketplace, remove-marketplace, "
                                            "import-marketplace (KIT: one of yours, or default). Fetch the kits' "
                                            "marketplaces, so their plugin lists show: sandbox-ai plugins claude "
                                            "--marketplaces-only.")
    parser.add_argument("name", nargs="?", metavar="MARKETPLACE",
                        help="only the marketplace(s) of that name, with every plugin")
    parser.add_argument("-a", "--agent", choices=["claude", "codex", "opencode"], help="only this agent's")
    parser.add_argument("--json", action="store_true", help="as JSON (for programs: the web and the app read it)")
    return parser


def marketplace_names():
    """Names of every marketplace (shell completion)."""
    return sorted({m["name"] for m in marketplaces()["marketplaces"]})


def marketplaces_cli(argv, out=print):
    a = marketplaces_parser().parse_args(argv)
    if a.name is not None and not re.fullmatch(kits().PLUGIN_NAME, a.name):
        raise ValueError(f"marketplace name {printable(a.name)!r}: 1-64 letters, digits, '_', '.', '-'")
    data = marketplaces(a.agent, a.name)
    if a.json:
        out(json.dumps(data, indent=2))
        return 0
    render_marketplaces(data, out, detail=a.name is not None)
    return 0 if data["marketplaces"] or a.name is None else 1


def cli(argv):
    if argv[:2] == ["claude", "trust-hooks"]:
        return trust_hooks(argv[2:])
    if argv[:1] == ["marketplaces"]:
        return marketplaces_cli(argv[1:])
    passthrough = None
    if "--" in argv:
        position = argv.index("--")
        argv, passthrough = argv[:position], argv[position + 1:]
    parser = argparse.ArgumentParser(prog="sandbox-ai plugins", description=__doc__.split("\n\n")[0],
                                     epilog="Launcher options (e.g. --backend bwrap) are passed on to the "
                                            "plugin store's sandbox. Every marketplace with its plugins and their "
                                            "state: sandbox-ai plugins marketplaces [--json] (see its --help).")
    parser.add_argument("agent", nargs="?", choices=["claude", "codex", "opencode", "marketplaces"],
                        help="the agent whose plugins to install, or marketplaces: list every marketplace")
    parser.add_argument("-s", "--status", action="store_true", help="Show declared and installed plugins")
    parser.add_argument("-u", "--update", action="store_true", help="Also update marketplaces and installed plugins")
    parser.add_argument("--kit", action="append", default=[], metavar="NAME",
                        help="Also install the plugins of sandbox-ai's built-in kit NAME (e.g. egm); repeatable")
    parser.add_argument("--marketplaces-only", action="store_true",
                        help="Only fetch (with --update: refresh) the declared marketplaces, so their plugin lists "
                             "can be browsed (sandbox-ai plugins marketplaces); install no plugin")
    a, options = parser.parse_known_args(argv)
    if a.agent == "marketplaces":
        return marketplaces_cli(options)
    if a.status or not a.agent:
        return status()
    if a.agent != "claude":
        print(f"sandbox-ai plugins: {a.agent} has no plugin store; "
              + ("OpenCode loads the npm plugins its kits list by itself" if a.agent == "opencode"
                 else "plugins for Codex are not supported yet"), file=sys.stderr)
        return 1
    if passthrough is not None:
        marketplaces, plugins, _ = kits().declarations("claude", kits().all_plugin_files("claude", a.kit),
                                                       with_disabled=True)
        sources = list(marketplaces.values())
        sources += [s for p in plugins for s in plugin_sources(p, marketplaces[p.split("@")[1]])[1:]]
        return run_in_store(passthrough, options, sources)
    return sync(options, a.update, builtin=a.kit, marketplaces_only=a.marketplaces_only)


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai plugins: {exc}", file=sys.stderr)
        sys.exit(1)
