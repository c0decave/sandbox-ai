#!/usr/bin/env python3
"""sandbox-ai kits: see what every kit brings, and change your own kits without editing files by hand.

  sandbox-ai kits [list] [--json] [--agent A]                    every kit and what it brings per agent
  sandbox-ai kits show NAME [--json]                             one kit
  sandbox-ai kits new NAME [--agent A]...                        a new kit (default: for all three agents)
  sandbox-ai kits add-skill NAME PATH [--agent A]... [--as SKILL] copy the skill folder PATH into the kit
  sandbox-ai kits new-skill NAME SKILL [--agent A]... [--description TEXT]   a skill from a template
  sandbox-ai kits remove-skill NAME SKILL [--agent A]...
  sandbox-ai kits add-plugin NAME PLUGIN [--agent claude|opencode] [--source SOURCE]
  sandbox-ai kits remove-plugin NAME PLUGIN [--agent A]
  sandbox-ai kits enable-plugin NAME PLUGIN [--agent A]           a disabled plugin of the kit on again
  sandbox-ai kits disable-plugin NAME PLUGIN [--agent A]          declared (and installed) but off with this kit
  sandbox-ai kits add-marketplace NAME MARKETPLACE SOURCE        SOURCE: OWNER/REPO, https://..., sandbox-ai
  sandbox-ai kits remove-marketplace NAME MARKETPLACE [--with-plugins]
  sandbox-ai kits import-marketplace NAME MARKETPLACE [--from claude|codex] [--with-plugins]
                                                                 one Claude (or Codex) already knows on this machine
  sandbox-ai kits add-hook NAME FILE [--agent A]... [--event E] [--matcher M] [--timeout S]
                                                                 a hook script, a hooks.json, or an OpenCode plugin
  sandbox-ai kits remove-hook NAME FILE [--agent A]...
  sandbox-ai kits trust-hooks KIT [--agent A]... [--yes|--revoke]  review a kit's hooks and approve (or revoke) them
  sandbox-ai kits delete NAME --yes

NAME is a kit of yours in ~/.local/share/sandbox-ai/kits; `show` also finds sandbox-ai's built-in kits
and, when no kit of yours has that name, `default` (the default kit) and `built-in` (the built-in skill,
part of every sandbox). Only your own kits are changed, and the default kit's plugins and marketplaces (the
plugin and marketplace commands take `default`): built-in kits and the built-in skill are read-only here, and
the rest of the default kit is edited by hand. Every marketplace with its plugins: sandbox-ai plugins
marketplaces. Nothing is followed through a symlink: a kit,
agent directory or skill that is a symlink (to a checkout, say) is changed where it lives. A copied skill
is a snapshot: symlinks, FIFOs and devices in it are skipped, never followed or opened.

Standard library only.
"""
import argparse
import contextlib
import errno
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import unicodedata
import urllib.parse

HERE = Path(__file__).resolve().parent
AGENTS = ("claude", "codex", "opencode")
KINDS = ("skills", "agents", "commands", "prompts")
INVENTORY_VERSION = 1
MARKETPLACE_NAME = "sandbox-ai"
# Claude's rule for skill names (the front matter's name); new-skill holds to it for every agent.
SKILL_NAME = r"[a-z0-9][a-z0-9-]{0,63}"
DESCRIPTION_LIMIT = 1024
README_LIMIT = 64 << 10
NOTES_LIMIT = 20
REVIEW_LIMIT = 64 << 10  # a hook file trust-hooks shows in full
GITHUB_REPO = r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}"
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_kitctl", HERE / f"{name}.py")
        _modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_modules[name])
    return _modules[name]


def kits():
    return module("kits")


def printable(text):
    """TEXT safe for a terminal: control, bidi and other non-printable characters escaped."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(text))


# --- where kits live -------------------------------------------------------------------------------

def builtin_kits_root():
    """sandbox-ai's own named kits (BUILTIN/kits/NAME/AGENT), whether or not kits.py knows them yet."""
    k = kits()
    return Path(getattr(k, "BUILTIN_KITS", k.BUILTIN / "kits"))


def names_in(root):
    """Kit names in ROOT: directories (a symlink to one counts, as for the launcher) with a valid name."""
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return []
    return [e.name for e in entries if re.fullmatch(kits().KIT_NAME, e.name) and e.is_dir()]


def agents_of(path):
    return [agent for agent in AGENTS if (Path(path) / agent).is_dir()]


def default_root():
    return kits().default_home("claude").parent


def plugins_take_kit_option():
    """True if this sandbox-ai's `plugins` command installs a built-in kit's plugins (--kit NAME)."""
    try:
        return '"--kit"' in (HERE / "plugins.py").read_text()
    except OSError:
        return False


# --- inventory -------------------------------------------------------------------------------------

def clean(text, limit=300):
    text = "".join(c for c in text if c.isprintable() or c == " ").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def readme_line(path):
    """First non-empty line of PATH/README.md (a small regular file), without Markdown heading marks."""
    try:
        data = kits().read_regular(Path(path) / "README.md", README_LIMIT)
    except (OSError, ValueError):
        return None
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        line = clean(line.lstrip("#"))
        if line:
            return line
    return None


def agent_info(agent, label, directory):
    """What the kit DIRECTORY (one agent's part of a kit) holds; a ValueError lands in "error"."""
    k = kits()
    info = {"path": str(directory), **{kind: [] for kind in KINDS}, "memory": None, "marketplaces": {},
            "plugins": [], "disabled": [], "hooks": None, "notes": [], "error": None}
    comment = None
    if Path(directory).is_symlink():
        info["notes"].append(f"{agent}/ is a symlink to {os.readlink(directory)}; change it where it lives")
    try:
        plan = k.scan(agent, [(label, Path(directory))])
        for kind, items in plan["entries"].items():
            info[kind] = sorted(items)
        info["memory"] = k.MEMORY_FILE[agent][0] if plan["memory"] else None
        info["notes"] += plan["notes"]
        info["hooks"] = hooks_object(agent, label, Path(directory))
        for _, path in plan["plugins"][:1]:
            document = k.read_declaration(label, path)
            _, _, notes = k.declarations(agent, [(label, path)])
            listed = document.get("plugins", [])
            declared = document.get("marketplaces", {})
            comment = document.get("$comment") if isinstance(document.get("$comment"), str) else None
            info["plugins"] = list(listed)
            info["disabled"] = list(document.get("disabled", []))
            if agent == "claude":
                info["marketplaces"] = {name: entry["source"] for name, entry in declared.items()}
                elsewhere = [p for p in listed if p.split("@")[1] not in declared]
                notes = [n for n in notes if not any(n.startswith(f"plugin {p} skipped") for p in elsewhere)]
                notes += [f"plugin {p}: this kit does not declare the marketplace {p.split('@')[1]}; a kit used "
                          "with it must" for p in elsewhere]
            info["notes"] += notes
    except (OSError, ValueError, RecursionError) as exc:
        info["error"] = str(exc) if isinstance(exc, ValueError) else f"{label}: {directory}: {exc}"
    return info, comment


def hooks_object(agent, label, directory):
    """The kit's (LABEL) hooks for AGENT (DIRECTORY/hooks) as the inventory shows them, with their approval for
    this kit; None without."""
    k = kits()
    folder = directory / k.HOOKS_DIR
    if not os.path.lexists(folder):
        return None
    found = k.read_hooks(agent, folder)
    trusted = k.hooks_trusted(agent, found["sha256"], k.trust_argument(label, folder))
    return {"path": str(folder), "sha256": found["sha256"], "trusted": trusted,
            "hooks": found["hooks"], "files": found["files"], "notes": found["notes"], "errors": found["errors"]}


def kit_object(kind, name, path, label):
    """One entry of the inventory's "kits"."""
    path = Path(path)
    agents, comments = {}, []
    for agent in agents_of(path):
        agents[agent], comment = agent_info(agent, label, path / agent)
        comments += [comment] if comment else []
    description = readme_line(path) or (clean(comments[0], 1000) if comments else None)
    if description is None and kind == "always":
        description = ("sandbox-ai's built-in skill, part of every sandbox: how to work in it; an entry of the "
                       "same name in a kit replaces it")
    replaced = [a for a in agents if (kits().kits_dir() / name / a).is_dir()] if kind == "builtin" else []
    return {"name": name, "kind": kind, "path": str(path), "editable": kind == "user" and not path.is_symlink(),
            "replaced": replaced, "description": description, "agents": agents}


def all_kits():
    k = kits()
    found = [kit_object("always", k.BUILTIN_LABEL, k.BUILTIN, k.BUILTIN_LABEL),
             kit_object("default", "default", default_root(), "home")]
    found += [kit_object("builtin", name, builtin_kits_root() / name, f"built-in kit {name}")
              for name in names_in(builtin_kits_root())]
    found += [kit_object("user", name, k.kits_dir() / name, f"kit {name}") for name in names_in(k.kits_dir())]
    return found


def source_label(source):
    """"local", "github:OWNER/REPO" or KIND:ARGUMENT of a plugin's source in a marketplace.json."""
    if isinstance(source, str):
        return "local"
    if isinstance(source, dict) and isinstance(source.get("source"), str):
        argument = next((source[key] for key in ("repo", "url", "package", "path") if isinstance(source.get(key), str)),
                        "")
        return f"{source['source']}:{argument}" if argument else source["source"]
    return "unknown"


def own_marketplace_entries():
    """[entry] of sandbox-ai's own marketplace.json (dicts with a valid name)."""
    k = kits()
    manifest = k.read_store_json(k.MARKETPLACE / ".claude-plugin" / "marketplace.json") or {}
    entries = manifest.get("plugins") if isinstance(manifest.get("plugins"), list) else []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)
            and re.fullmatch(k.PLUGIN_NAME, e["name"])]


def marketplace_object(found):
    k = kits()
    plugins = []
    for entry in own_marketplace_entries():
        plugin = f"{entry['name']}@{MARKETPLACE_NAME}"
        users = [kit["name"] for kit in found if plugin in kit["agents"].get("claude", {}).get("plugins", [])]
        plugins.append({"name": entry["name"], "id": plugin,
                        "description": entry["description"] if isinstance(entry.get("description"), str) else "",
                        "version": entry["version"] if isinstance(entry.get("version"), str) else None,
                        "source": source_label(entry.get("source")),
                        "homepage": entry["homepage"] if isinstance(entry.get("homepage"), str) else None,
                        "kits": list(dict.fromkeys(users))})
    return {"name": MARKETPLACE_NAME, "path": str(k.MARKETPLACE), "target": k.MARKETPLACE_TARGET, "plugins": plugins}


def inventory(agent=None):
    """Every kit and what it brings per agent (only AGENT's part, and kits that have one, if given), sandbox-ai's
    own marketplace and what the plugin store holds; the dict `sandbox-ai kits list --json` prints."""
    k = kits()
    found = all_kits()
    marketplace = marketplace_object(found)
    if agent:
        found = [dict(kit, agents={agent: kit["agents"][agent]}, replaced=[a for a in kit["replaced"] if a == agent])
                 for kit in found if agent in kit["agents"]]
    known, installed = k.installed()
    return {"version": INVENTORY_VERSION, "kits_dir": str(k.kits_dir()), "store": str(k.plugin_store("claude")),
            "kits": found, "marketplace": marketplace,
            "installed": {"marketplaces": sorted(known), "plugins": sorted(installed)}}


# --- text output -----------------------------------------------------------------------------------

def agent_summary(agent, info, installed, detail):
    parts = [f"{kind}: {', '.join(info[kind])}" for kind in KINDS if info[kind]]
    parts += [info["memory"]] if info["memory"] else []
    hooks = info.get("hooks")
    if hooks:
        state = "cannot run" if hooks["errors"] else "approved" if hooks["trusted"] else "NOT approved"
        parts.append(f"hooks: {len(hooks['hooks'])} ({state})")
    if info["marketplaces"]:
        parts.append("marketplaces: " + ", ".join(
            f"{name} ({kits().source_argument(source)})" if detail else name
            for name, source in info["marketplaces"].items()))
    off = info.get("disabled", [])
    if info["plugins"] or off:
        listed = [(p, "") for p in info["plugins"]] + [(p, "off") for p in off]
        if agent == "claude":
            parts.append("plugins: " + ", ".join(
                p + (f" ({', '.join(w for w in (state, '' if p in installed else 'not installed') if w)})"
                     if state or p not in installed else "") for p, state in listed))
        elif agent == "opencode":
            parts.append("npm plugins: " + ", ".join(p + (" (off)" if state else "") for p, state in listed))
        else:
            parts.append("plugins (ignored: not supported for Codex yet): " + ", ".join(p for p, _ in listed))
    return "; ".join(parts) or "empty"


def install_hint(kit, installed):
    """The command that installs the kit's missing Claude plugins, or None."""
    info = kit["agents"].get("claude")
    if not info or not [p for p in info["plugins"] if p not in installed] or kit["kind"] == "always":
        return None
    if kit["kind"] != "builtin":
        return "install its plugins: sandbox-ai plugins claude"
    if "claude" in kit["replaced"]:
        return None
    if plugins_take_kit_option():
        return f"install its plugins: sandbox-ai plugins claude --kit {kit['name']}"
    return ("this sandbox-ai's `sandbox-ai plugins` does not install a built-in kit's plugins; declare them in "
            "a kit of yours (sandbox-ai kits add-plugin)")


def render(kit, data, out, detail=False):
    name, kind, installed = printable(kit["name"]), kit["kind"], set(data["installed"]["plugins"])
    first = next(iter(kit["agents"]), "claude")
    path = printable(kit["path"])
    head = {"always": f"{name}: sandbox-ai's built-in skill, part of every sandbox; read-only ({path})",
            "default": f"{name}: the default kit, part of every sandbox unless --no-default-kit; its plugins and "
                       f"marketplaces change with sandbox-ai kits add-plugin default ..., the rest by hand in {path}",
            "builtin": f"{name}: built-in kit of sandbox-ai, read-only ({path}); use: sandbox-ai {first} --kit {name}",
            "user": f"{name}: your kit ({path}); use: sandbox-ai {first} --kit {name}"}[kind]
    out(head)
    if kit["description"]:
        out(f"  {printable(kit['description'])}")
    if kind == "user" and not kit["editable"]:
        out(f"  a symlink to {printable(os.readlink(kit['path']))}: sandbox-ai kits does not change it; change it "
            "where it lives")
    if kit["replaced"]:
        out(f"  replaced by your kit {name} for: {', '.join(kit['replaced'])}")
    if kind == "user" and kit["name"] in names_in(builtin_kits_root()):
        shipped = agents_of(builtin_kits_root() / kit["name"])
        mine = [a for a in shipped if a in kit["agents"]]
        out(f"  replaces sandbox-ai's built-in kit {name} for: {', '.join(mine) or 'none'}"
            + "".join(f"; for {a}, --kit {name} still uses the built-in one" for a in shipped if a not in mine))
    if not kit["agents"]:
        out("  no agent directory yet" + (" (created on an agent's first start)" if kind == "default" else ""))
    for agent, info in kit["agents"].items():
        out(f"  {agent:9} {printable(agent_summary(agent, info, installed, detail))}")
        if detail:
            for line in hook_lines(info.get("hooks")):
                out(f"            {printable(line)}")
            hooks = info.get("hooks")
            if hooks and not hooks["trusted"] and not hooks["errors"]:
                out(f"            approve after a review: sandbox-ai kits trust-hooks {name} --agent {agent}")
            for note in info["notes"]:
                out(f"            note: {printable(note)}")
        elif info["notes"]:
            out(f"            {len(info['notes'])} note(s): sandbox-ai kits show {name}")
        if info["error"]:
            out(f"            error: {printable(info['error'])}")
    hint = install_hint(kit, installed)
    if hint:
        out(f"  {hint}")


def hook_lines(hooks):
    """One line per hook (event, matcher, command, the files it runs) and per error of the hooks of a kit."""
    if not hooks:
        return []
    lines = []
    for hook in hooks["hooks"]:
        if "plugin" in hook:
            lines.append(f"hook: OpenCode plugin {hook['plugin']}")
        else:
            runs = f" (runs {', '.join(hook['files'])})" if hook["files"] else ""
            timeout = f", timeout {hook['timeout']} s" if hook.get("timeout") else ""
            lines.append(f"hook: {hook['event']}, matcher {hook.get('matcher') or '*'}: {hook['command']}{timeout}{runs}")
    lines += [f"hooks error: {error}" for error in hooks["errors"]]
    lines += [f"hooks note: {note}" for note in hooks["notes"]]
    return lines


def render_list(data, out):
    out(f"Kits of yours: {printable(data['kits_dir'])}; Claude plugin store: {printable(data['store'])}")
    for kit in data["kits"]:
        out("")
        render(kit, data, out)
    if not any(kit["kind"] == "user" for kit in data["kits"]):
        out("")
        out("No kit of yours yet; create one with: sandbox-ai kits new NAME")
    market = data["marketplace"]
    if market["plugins"]:
        out("")
        out(f"sandbox-ai's marketplace ({MARKETPLACE_NAME}, {market['target']} in a Claude sandbox):")
        for plugin in market["plugins"]:
            users = ", ".join(plugin["kits"]) or "no kit"
            installed = "" if plugin["id"] in data["installed"]["plugins"] else ", not installed"
            out(f"  {plugin['id']} ({plugin['source']}; listed by {printable(users)}{installed}): "
                f"{printable(plugin['description'])}")
        out(f"  add one to a kit of yours with: sandbox-ai kits add-plugin NAME PLUGIN@{MARKETPLACE_NAME}")


# --- resolving and opening kits --------------------------------------------------------------------

def check_kit_name(name):
    if not re.fullmatch(kits().KIT_NAME, name):
        raise ValueError(f"kit name {printable(name)!r}: 1-64 letters, digits, '_', '.', '-' "
                         "(not starting with '.' or '-')")


def find_kit(name):
    """The inventory entry `show NAME` means: your kit, else the built-in kit NAME, else `default`/`built-in`."""
    check_kit_name(name)
    k = kits()
    if (k.kits_dir() / name).is_dir():
        return kit_object("user", name, k.kits_dir() / name, f"kit {name}")
    if name in names_in(builtin_kits_root()):
        return kit_object("builtin", name, builtin_kits_root() / name, f"built-in kit {name}")
    if name == "default":
        return kit_object("default", "default", default_root(), "home")
    if name == k.BUILTIN_LABEL:
        return kit_object("always", k.BUILTIN_LABEL, k.BUILTIN, k.BUILTIN_LABEL)
    known = names_in(k.kits_dir()) + names_in(builtin_kits_root()) + ["default", k.BUILTIN_LABEL]
    raise ValueError(f"no kit {name} (kits: {', '.join(dict.fromkeys(known))})")


def read_only_reason(name):
    """Why NAME (not a kit of yours) cannot be changed here."""
    k = kits()
    if name in names_in(builtin_kits_root()):
        return (f"{name} is a built-in kit of sandbox-ai ({builtin_kits_root() / name}) and read-only; a kit of yours "
                f"of that name replaces it for its agents: sandbox-ai kits new {name}")
    if name == "default":
        return (f"sandbox-ai kits changes only the default kit's plugins and marketplaces (add-plugin, "
                f"add-marketplace, import-marketplace, ...); edit it by hand in {default_root()}/AGENT "
                f"(e.g. {k.default_home('claude')})")
    if name == k.BUILTIN_LABEL:
        return ("the built-in skill ships with sandbox-ai and is read-only; a skill named sandbox-ai in a kit of "
                "yours replaces it")
    return f"no kit {name} in {k.kits_dir()}; create it with: sandbox-ai kits new {name}"


def kits_root(create=False):
    """The kits directory, resolved (it may be a symlink the user made, as for the launcher); None if missing."""
    root = kits().kits_dir()
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(os.path.realpath(root)) if root.is_dir() else None


def user_kit(name):
    """(kits root, kit path) of your kit NAME, which must be a real directory; ValueError otherwise."""
    check_kit_name(name)
    root = kits_root()
    path = (root or kits().kits_dir()) / name
    if root is None or not os.path.lexists(path):
        raise ValueError(read_only_reason(name))
    if path.is_symlink():
        raise ValueError(f"kit {name} is a symlink to {printable(os.readlink(path))}; sandbox-ai kits changes no "
                         "kit through a symlink: change it where it lives")
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    return root, path


def why_not(root, parts):
    """The first component of ROOT/PARTS that is a symlink or no directory, as a message."""
    path = Path(root)
    for part in parts:
        path = path / part
        try:
            info = os.lstat(path)
        except OSError:
            break
        if stat.S_ISLNK(info.st_mode):
            return (f"{printable(path)} is a symlink to {printable(os.readlink(path))}; sandbox-ai kits changes "
                    "nothing through a symlink: change it where it lives")
        if not stat.S_ISDIR(info.st_mode):
            return f"{printable(path)} is not a directory"
    return f"{printable(Path(root).joinpath(*parts))}: a component is a symlink or not a directory"


def open_parts(root, parts, create=False):
    """Directory fd of ROOT/PARTS, no component followed as a symlink (kits.open_dir); None if missing."""
    try:
        return kits().open_dir(root, tuple(parts), create=create)
    except ValueError:
        raise ValueError(why_not(root, parts)) from None


@contextlib.contextmanager
def opened(root, parts, create=False):
    fd = open_parts(root, parts, create)
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def lstat_at(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def chosen_agents(values, default):
    return list(dict.fromkeys(values)) if values else list(default)


# --- new, delete -----------------------------------------------------------------------------------

def cmd_new(a, out):
    check_kit_name(a.name)
    k = kits()
    if a.name in ("default", k.BUILTIN_LABEL):
        raise ValueError(f"{a.name} is how sandbox-ai kits names the "
                         f"{'default kit' if a.name == 'default' else 'built-in skill'}; choose another name")
    root = kits_root(create=True)
    kit = root / a.name
    if kit.is_symlink():
        raise ValueError(f"kit {a.name} is a symlink to {printable(os.readlink(kit))}; change it where it lives")
    if os.path.lexists(kit) and not kit.is_dir():
        raise ValueError(f"{kit} exists and is not a directory")
    created, existing = [], []
    for agent in chosen_agents(a.agent, AGENTS):
        if os.path.lexists(kit / agent):
            existing.append(agent)
            continue
        with opened(root, (a.name, agent, "skills"), create=True):
            created.append(agent)
    if not created:
        raise ValueError(f"kit {a.name} already has {', '.join(existing)} ({kit})")
    out(f"kit {a.name}: created {', '.join(f'{agent}/skills' for agent in created)} in {printable(kit)}"
        + (f" ({', '.join(existing)} existed)" if existing else ""))
    shipped = [agent for agent in agents_of(builtin_kits_root() / a.name) if agent in created]
    if shipped:
        out(f"note: your kit {a.name} replaces sandbox-ai's built-in kit {a.name} for {', '.join(shipped)}; "
            "what the built-in one declares (see: sandbox-ai kits show --json) is no longer used there, so add "
            "what you need from it to your kit")
    out(f"add skills with: sandbox-ai kits add-skill {a.name} FOLDER, or: sandbox-ai kits new-skill {a.name} SKILL")
    out(f"use it with: sandbox-ai {created[0]} --kit {a.name}")
    return 0


def cmd_delete(a, out):
    check_kit_name(a.name)
    root = kits_root()
    path = (root or kits().kits_dir()) / a.name
    if root is None or not os.path.lexists(path):
        raise ValueError(read_only_reason(a.name))
    if path.is_symlink():
        if not a.yes:
            raise ValueError(f"kit {a.name} is a symlink to {printable(os.readlink(path))}; deleting it removes only "
                             "the link: repeat with --yes")
        os.unlink(path)
        out(f"kit {a.name}: removed the link {printable(path)} (its target is untouched)")
        return 0
    if not a.yes:
        raise ValueError(f"deleting kit {a.name} removes {printable(path)} and everything in it (skills, memory "
                         "templates, marketplaces.json): repeat with --yes")
    fd = os.open(root, kits().DIR_FLAGS & ~os.O_NOFOLLOW)
    try:
        kits().remove_tree_at(fd, a.name)
    finally:
        os.close(fd)
    out(f"kit {a.name}: deleted {printable(path)}")
    return 0


# --- skills ----------------------------------------------------------------------------------------

def check_entry_name(value, what="skill"):
    if not value or "/" in value or value.startswith(".") or kits().bad_name(value):
        raise ValueError(f"{what} name {printable(value)!r}: no '/', not starting with '.', no control characters")


class Copy:
    """Copy a skill folder from directory fds: nothing followed, special files skipped, limits enforced."""

    def __init__(self, label):
        k = kits()
        self.label, self.files, self.bytes, self.entries, self.notes = label, 0, 0, 0, []
        self.limit_files, self.limit_bytes, self.limit_depth = k.KIT_FILES, k.KIT_BYTES, k.KIT_DEPTH
        self.limit_entries = 4 * k.KIT_FILES

    def note(self, text):
        if len(self.notes) < NOTES_LIMIT:
            self.notes.append(text)
        elif len(self.notes) == NOTES_LIMIT:
            self.notes.append("further entries skipped without a note")

    def directory(self, source_fd, target_fd, relative, depth):
        with os.scandir(source_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            self.entries += 1
            if self.entries > self.limit_entries:
                raise ValueError(f"{self.label} holds more than {self.limit_entries} entries")
            shown = f"{relative}/{name}" if relative else name
            problem = kits().bad_name(name)
            if problem:
                self.note(f"{printable(shown)} skipped: {problem}")
                continue
            if name == ".git":
                self.note(f"{shown} skipped: a Git repository's own data")
                continue
            info = lstat_at(source_fd, name)
            if info is None:
                continue
            if stat.S_ISLNK(info.st_mode):
                self.note(f"{printable(shown)} skipped: a symlink (not followed)")
            elif stat.S_ISDIR(info.st_mode):
                if depth + 1 >= self.limit_depth:
                    raise ValueError(f"{self.label}: {printable(shown)} is nested deeper than {self.limit_depth} "
                                     "directories")
                try:
                    child = os.open(name, kits().DIR_FLAGS, dir_fd=source_fd)
                except OSError:
                    self.note(f"{printable(shown)} skipped: changed while copying")
                    continue
                try:
                    os.mkdir(name, 0o700, dir_fd=target_fd)
                    copy = os.open(name, kits().DIR_FLAGS, dir_fd=target_fd)
                    try:
                        self.directory(child, copy, shown, depth + 1)
                        os.fchmod(copy, 0o755)
                    finally:
                        os.close(copy)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                self.file(source_fd, target_fd, name, shown)
            else:
                self.note(f"{printable(shown)} skipped: not a regular file or directory (FIFO, socket or device)")

    def file(self, source_fd, target_fd, name, shown):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                         dir_fd=source_fd)
        except OSError as exc:
            self.note(f"{printable(shown)} skipped: {'a symlink' if exc.errno == errno.ELOOP else exc.strerror}")
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                self.note(f"{printable(shown)} skipped: not a regular file")
                return
            self.files += 1
            if self.files > self.limit_files:
                raise ValueError(f"{self.label} holds more than {self.limit_files} files")
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                          0o600, dir_fd=target_fd)
            with os.fdopen(out, "wb") as stream:
                while True:
                    chunk = os.read(fd, 1 << 20)
                    if not chunk:
                        break
                    self.bytes += len(chunk)
                    if self.bytes > self.limit_bytes:
                        raise ValueError(f"{self.label} holds more than {self.limit_bytes >> 20} MiB")
                    stream.write(chunk)
                stream.flush()
                # Scripts in skills stay executable.
                os.fchmod(stream.fileno(), 0o755 if info.st_mode & 0o111 else 0o644)
        finally:
            os.close(fd)


def open_skill_source(value):
    """(directory fd, display path) of the skill folder VALUE: a real directory with a regular SKILL.md."""
    path = value.rstrip("/") or "/"
    try:
        fd = os.open(path, kits().DIR_FLAGS)
    except FileNotFoundError:
        raise ValueError(f"{printable(value)} does not exist") from None
    except OSError as exc:
        # O_NOFOLLOW with O_DIRECTORY fails with ENOTDIR (not ELOOP) for a symlink on Linux.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR) and os.path.islink(path):
            raise ValueError(f"{printable(value)} is a symlink; give the folder itself") from None
        if exc.errno == errno.ENOTDIR:
            raise ValueError(f"{printable(value)} is not a folder") from None
        raise ValueError(f"{printable(value)}: {exc.strerror}") from None
    info = lstat_at(fd, "SKILL.md")
    if info is None or not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ValueError(f"{printable(value)} has no SKILL.md at its top (a regular file, not a symlink); "
                         "a skill folder holds one")
    return fd, path


def agent_writable(path):
    """[(tree, why)] of the folders sandboxes can or could write that hold PATH or lie inside it."""
    trees = []
    try:
        net = module("net")
        trees += [(Path(tree), "a folder sandboxes could write in an earlier run") for tree in net.launcher().exposed_before()]
        trees += [(Path(record["workspace"]), f"the workspace of the running sandbox {name}")
                  for name, record in net.running().items() if isinstance(record.get("workspace"), str)]
    except Exception:  # noqa: BLE001 - the check is a courtesy; the copy is a snapshot either way
        return []
    real = Path(os.path.realpath(path))
    found = []
    for tree, why in trees:
        tree = Path(os.path.realpath(tree))
        if real == tree or tree in real.parents or real in tree.parents:
            found.append((tree, why))
    return list(dict.fromkeys(found))


def skill_agents(kit, values):
    agents = chosen_agents(values, agents_of(kit))
    if not agents:
        raise ValueError(f"kit {kit.name} has no agent directory; give --agent (or: sandbox-ai kits new {kit.name})")
    return agents


def cmd_add_skill(a, out):
    root, kit = user_kit(a.name)
    source_fd, path = open_skill_source(a.path)
    try:
        skill = a.skill or os.path.basename(os.path.abspath(path))
        if not re.fullmatch(kits().KIT_NAME, skill):
            raise ValueError(f"skill name {printable(skill)!r}: 1-64 letters, digits, '_', '.', '-' (not starting "
                             "with '.' or '-'); choose one with --as SKILL")
        agents = skill_agents(kit, a.agent)
        for agent in agents:
            with opened(root, (a.name, agent, "skills")) as fd:
                if fd is not None and lstat_at(fd, skill) is not None:
                    raise ValueError(f"kit {a.name} already has the skill {skill} for {agent}; remove it first: "
                                     f"sandbox-ai kits remove-skill {a.name} {skill} --agent {agent}")
        done, copy = [], None
        try:
            for agent in agents:
                copy = copy_skill(root, a.name, agent, skill, source_fd, path)
                done.append(agent)
        except BaseException:
            for agent in done:
                with contextlib.suppress(OSError, ValueError), opened(root, (a.name, agent, "skills")) as fd:
                    kits().remove_tree_at(fd, skill)
            raise
    finally:
        os.close(source_fd)
    out(f"kit {a.name}: skill {skill} copied for {', '.join(agents)} from {printable(os.path.abspath(path))} "
        f"({copy.files} file(s), {copy.bytes} bytes)")
    for note in copy.notes:
        out(f"note: {note}")
    out("note: the kit holds a copy; later changes to the folder do not reach it (symlink a skill into the kit "
        "by hand to keep using the original)")
    for tree, why in agent_writable(path):
        out(f"note: {printable(path)} lies in or holds {printable(tree)}, {why}: the copy is a snapshot of files an "
            "agent may have written; review it before using the kit")
    if skill == "sandbox-ai":
        out("note: it replaces sandbox-ai's built-in skill sandbox-ai in sandboxes of this kit")
    return 0


def copy_skill(root, name, agent, skill, source_fd, path):
    """Copy the skill folder into NAME/AGENT/skills/SKILL through a temporary directory renamed when complete."""
    copy = Copy(printable(path))
    with opened(root, (name, agent, "skills"), create=True) as skills:
        temporary = f".add-{skill[:40]}-{os.getpid()}-{os.urandom(4).hex()}"
        os.mkdir(temporary, 0o700, dir_fd=skills)
        try:
            target = os.open(temporary, kits().DIR_FLAGS, dir_fd=skills)
            try:
                copy.directory(source_fd, target, "", 0)
                info = lstat_at(target, "SKILL.md")
                if info is None or not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"{printable(path)}: SKILL.md changed while copying; nothing was added")
                os.fchmod(target, 0o755)
            finally:
                os.close(target)
            if lstat_at(skills, skill) is not None:
                raise ValueError(f"kit {name} already has the skill {skill} for {agent}")
            os.rename(temporary, skill, src_dir_fd=skills, dst_dir_fd=skills)
        except BaseException:
            with contextlib.suppress(OSError):
                kits().remove_tree_at(skills, temporary)
            raise
    return copy


def skill_template(skill, description):
    return (f"---\nname: {skill}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"
            f"# {skill}\n\n"
            "Write here what the agent should know or do when this skill applies: the steps, the commands, the\n"
            "files and conventions involved, and what to avoid. The description above (one line) is what the agent\n"
            "reads to decide when to use the skill, so say there what it does and when it applies.\n\n"
            "Further files (scripts, references, examples) can sit next to this SKILL.md; mention them here.\n")


def check_description(text):
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text):
        raise ValueError("--description: one line without control or formatting characters")
    if not text.strip():
        raise ValueError("--description must not be empty")
    if len(text) > DESCRIPTION_LIMIT:
        raise ValueError(f"--description: at most {DESCRIPTION_LIMIT} characters ({len(text)} given)")
    return text.strip()


def cmd_new_skill(a, out):
    if not re.fullmatch(SKILL_NAME, a.skill):
        raise ValueError(f"skill name {printable(a.skill)!r}: 1-64 lowercase letters, digits and '-', starting with a "
                         "letter or digit (Claude's rule)")
    description = check_description(a.description if a.description is not None else
                                    f"TODO: say what the skill {a.skill} does and when the agent should use it.")
    root, kit = user_kit(a.name)
    agents = skill_agents(kit, a.agent)
    for agent in agents:
        with opened(root, (a.name, agent, "skills")) as fd:
            if fd is not None and lstat_at(fd, a.skill) is not None:
                raise ValueError(f"kit {a.name} already has the skill {a.skill} for {agent}")
    data = skill_template(a.skill, description).encode()
    done = []
    try:
        for agent in agents:
            with opened(root, (a.name, agent, "skills"), create=True) as skills:
                os.mkdir(a.skill, 0o755, dir_fd=skills)
            done.append(agent)
            kits().write_at(root, f"{a.name}/{agent}/skills/{a.skill}/SKILL.md", data)
    except BaseException:
        for agent in done:
            with contextlib.suppress(OSError, ValueError), opened(root, (a.name, agent, "skills")) as fd:
                kits().remove_tree_at(fd, a.skill)
        raise
    for agent in agents:
        out(f"kit {a.name}: wrote {printable(kit / agent / 'skills' / a.skill / 'SKILL.md')}")
    if a.description is None:
        out("note: replace the TODO description: the agent decides by it when to use the skill")
    if a.skill == "sandbox-ai":
        out("note: it replaces sandbox-ai's built-in skill sandbox-ai in sandboxes of this kit")
    return 0


def cmd_remove_skill(a, out):
    check_entry_name(a.skill)
    root, kit = user_kit(a.name)
    agents = [agent for agent in chosen_agents(a.agent, agents_of(kit))
              if os.path.lexists(kit / agent / "skills" / a.skill)]
    missing = [agent for agent in chosen_agents(a.agent, ()) if agent not in agents]
    if missing or not agents:
        raise ValueError(f"kit {a.name} has no skill {a.skill}" + (f" for {', '.join(missing)}" if missing else ""))
    for agent in agents:
        with opened(root, (a.name, agent, "skills")) as fd:
            info = lstat_at(fd, a.skill)
            link = info is not None and stat.S_ISLNK(info.st_mode)
            kits().remove_tree_at(fd, a.skill)
        out(f"kit {a.name}: removed the skill {a.skill} for {agent}" + (" (a symlink: only the link)" if link else ""))
    return 0


# --- plugins and marketplaces ----------------------------------------------------------------------
#
# A kit's AGENT/marketplaces.json: "marketplaces" (Claude), "plugins" (enabled when the kit is used) and "disabled"
# (declared, installed by `sandbox-ai plugins claude`, but off; the last kit of a start that names a plugin decides).
# These commands change your kits and the default kit (`default`, part of every sandbox; created from its examples
# first when missing); sandbox-ai's built-in kits stay read-only.

def plugins_module():
    """sandbox/plugins.py, on the same kits module as this one (so what a caller set there holds for both)."""
    loaded = module("plugins")
    loaded._modules["kits"] = kits()
    return loaded


def validate(agent, document, label):
    """The rules kits.declarations applies, on DOCUMENT (through a private temporary file)."""
    with tempfile.TemporaryDirectory(prefix="sandbox-ai-kits-") as directory:
        path = Path(directory) / kits().PLUGINS_FILE
        path.write_text(json.dumps(document))
        kits().declarations(agent, [(label, path)])


def plugin_kit(name, agent, out):
    """(root, the kit's folder below ROOT, label) of the kit NAME whose marketplaces.json the plugin and marketplace
    commands change: a kit of yours, or `default`, the default kit (AGENT's part created from its examples first
    when missing, as a start does: its suggestions are what is changed, not an empty kit)."""
    check_kit_name(name)
    k = kits()
    if name == "default" and not os.path.lexists(k.kits_dir() / name):
        if k.ensure_default_home(agent):
            out(f"created the default kit {printable(k.default_home(agent))} with examples")
        root = Path(os.path.realpath(k.data_root()))
        if not (root / "home").is_dir():
            raise ValueError(f"the default kit {k.default_home('claude').parent} does not exist yet (a start creates "
                             "it)")
        return root, "home", "the default kit"
    root, _ = user_kit(name)
    return root, name, f"kit {name}"


def use_hint(name, agent):
    """How a kit's plugin reaches a sandbox."""
    if name == "default":
        return f"every {agent} sandbox gets it (the default kit)"
    return f"use it with: sandbox-ai {agent} --kit {name}"


def read_plugins_file(root, name, agent):
    """(document, existed) of NAME/AGENT/marketplaces.json: a regular file, never through a symlink."""
    k = kits()
    with opened(root, (name, agent)) as fd:
        if fd is None:
            return {}, False
        info = lstat_at(fd, k.PLUGINS_FILE)
        if info is None:
            return {}, False
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{printable(root / name / agent / k.PLUGINS_FILE)} is a symlink; change it where it lives")
        data, why = k.read_at(fd, k.PLUGINS_FILE, k.DECLARATION_LIMIT)
    path = root / name / agent / k.PLUGINS_FILE
    if why:
        raise ValueError(f"{printable(path)}: {why}")
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        document = None
    if not isinstance(document, dict):
        raise ValueError(f"{printable(path)} must hold a JSON object; fix it by hand")
    return document, True


def change_plugins_file(root, name, agent, change, label=None):
    """Read NAME/AGENT/marketplaces.json, apply CHANGE(document) (True if it changed something), check the
    result like the launcher does and write it atomically; "$comment" and other entries are kept."""
    k = kits()
    label = label or f"kit {name}"
    document, existed = read_plugins_file(root, name, agent)
    if existed:
        try:
            validate(agent, document, label)
        except ValueError as exc:
            raise ValueError(f"{exc} (fix {root / name / agent / k.PLUGINS_FILE} by hand first)") from None
    if not change(document):
        return False
    if "disabled" in document and not document["disabled"]:
        del document["disabled"]
    ordered = {key: document[key] for key in ("$comment", "marketplaces", "plugins", "disabled") if key in document}
    ordered.update(document)
    validate(agent, ordered, label)
    k.write_at(root, f"{name}/{agent}/{k.PLUGINS_FILE}",
               (json.dumps(ordered, indent=2, ensure_ascii=False) + "\n").encode())
    return True


def parse_source(marketplace, value):
    """The marketplace source VALUE names: OWNER/REPO (or github:OWNER/REPO), https://... (git, or url for a
    .json), sandbox-ai or /sandbox/marketplace (sandbox-ai's own). A folder on this machine is refused."""
    k = kits()
    if value in ("sandbox-ai", k.MARKETPLACE_TARGET):
        if marketplace != MARKETPLACE_NAME:
            raise ValueError(f"sandbox-ai's own marketplace is called {MARKETPLACE_NAME}, not {marketplace}")
        source = {"source": "directory", "path": k.MARKETPLACE_TARGET}
    elif value.startswith("github:"):
        source = {"source": "github", "repo": value[len("github:"):]}
    elif value.startswith("https://"):
        kind = "url" if urllib.parse.urlsplit(value).path.endswith(".json") else "git"
        source = {"source": kind, "url": value}
    elif "://" in value or value.startswith("git@"):
        raise ValueError(f"{printable(value)}: only https:// sources are supported (or OWNER/REPO for GitHub)")
    elif re.fullmatch(GITHUB_REPO, value) and not value.startswith(".") and not os.path.lexists(value):
        source = {"source": "github", "repo": value}
    else:
        local = os.path.lexists(os.path.expanduser(value))
        raise ValueError(
            f"{printable(value)}: {'a folder on this machine' if local else 'not a source'}: sandboxes get only "
            f"sandbox-ai's own marketplace as a directory (sandbox-ai, at {k.MARKETPLACE_TARGET}); publish others on "
            "a Git server or GitHub and give OWNER/REPO or https://..."
            + (" (for the GitHub repository of that name write github:OWNER/REPO)"
               if re.fullmatch(GITHUB_REPO, value) else ""))
    return k.check_source(marketplace, source)


def source_value(source):
    """What to give as SOURCE for SOURCE (a source dict)."""
    return "sandbox-ai" if source.get("source") == "directory" else kits().source_argument(source)


def declared_elsewhere(marketplace, but):
    """[(kit label, source)] of the default kit and your other kits that declare MARKETPLACE for Claude."""
    k = kits()
    found = []
    files = [("the default kit", k.default_home("claude") / k.PLUGINS_FILE)]
    files += [(f"kit {name}", k.kits_dir() / name / "claude" / k.PLUGINS_FILE) for name in names_in(k.kits_dir())
              if name != but]
    files += [(f"built-in kit {name}", builtin_kits_root() / name / "claude" / k.PLUGINS_FILE)
              for name in names_in(builtin_kits_root())]
    for label, path in files:
        try:
            if path.is_file():
                entry = k.read_declaration(label, path).get("marketplaces", {}).get(marketplace)
                if isinstance(entry, dict):
                    found.append((label, k.check_source(marketplace, entry.get("source"))))
        except (OSError, ValueError, AttributeError):
            continue
    return found


def own_plugin_names():
    return [entry["name"] for entry in own_marketplace_entries()]


def enable_in(document, plugin):
    """PLUGIN in DOCUMENT's "plugins" (taken out of "disabled"); True if that changed something."""
    listed, off = document.setdefault("plugins", []), document.get("disabled", [])
    changed = plugin in off
    if changed:
        document["disabled"] = [p for p in off if p != plugin]
    if plugin not in listed:
        listed.append(plugin)
        changed = True
    return changed


def declare_marketplace(document, name, marketplace, source):
    """MARKETPLACE with SOURCE in DOCUMENT; True if added, False if declared with it already, ValueError for another
    source."""
    declared = document.setdefault("marketplaces", {})
    if marketplace in declared:
        current = declared[marketplace].get("source") if isinstance(declared[marketplace], dict) else None
        if current != source:
            raise ValueError(f"{name} declares the marketplace {marketplace} already, with another source "
                             f"({source_value(current) if isinstance(current, dict) else '?'}); remove it there first "
                             f"(sandbox-ai kits remove-marketplace)")
        return False
    declared[marketplace] = {"source": source}
    return True


def cmd_add_plugin(a, out):
    k = kits()
    agent = a.agent or "claude"
    if agent == "codex":
        raise ValueError("plugins for Codex are not supported yet (a kit's codex/marketplaces.json is ignored)")
    if agent == "opencode":
        if a.source:
            raise ValueError("OpenCode has no marketplaces; --source is for Claude plugins")
        if not re.fullmatch(k.NPM_PLUGIN, a.plugin):
            raise ValueError(f"{printable(a.plugin)!r} is not an npm package name (optionally @VERSION)")
        root, folder, label = plugin_kit(a.name, agent, out)
        if not change_plugins_file(root, folder, agent, lambda document: enable_in(document, a.plugin), label):
            out(f"{label} already lists {a.plugin} for opencode; nothing changed")
            return 0
        out(f"{label}: npm plugin {a.plugin} added for opencode; OpenCode installs it itself, which needs "
            f"--allow-host registry.npmjs.org ({use_hint(a.name, 'opencode')})")
        return 0
    if not re.fullmatch(f"{k.PLUGIN_NAME}@{k.PLUGIN_NAME}", a.plugin):
        raise ValueError(f"{printable(a.plugin)!r}: a Claude plugin is NAME@MARKETPLACE, e.g. sandcastle@sandbox-ai")
    plugin_name, marketplace = a.plugin.split("@")
    wanted = parse_source(marketplace, a.source) if a.source else None
    root, folder, label = plugin_kit(a.name, "claude", out)
    added = {}

    def add_claude(document):
        declared = document.setdefault("marketplaces", {})
        current = declared.get(marketplace, {}).get("source") if marketplace in declared else None
        if wanted and current and current != wanted:
            raise ValueError(f"{label} declares the marketplace {marketplace} with another source "
                             f"({source_value(current)}); remove it there first or leave out --source")
        source = current or wanted
        if source is None and marketplace == MARKETPLACE_NAME:
            source = {"source": "directory", "path": k.MARKETPLACE_TARGET}
        if source is None:
            elsewhere = declared_elsewhere(marketplace, a.name)
            hint = "".join(f"; {where} declares it: --source {source_value(s)}" for where, s in elsewhere[:3])
            raise ValueError(f"{label} does not declare the marketplace {marketplace}; give its source with "
                             f"--source OWNER/REPO|https://...{hint}")
        if source.get("source") == "directory":
            names = own_plugin_names()
            if plugin_name not in names:
                raise ValueError(f"sandbox-ai's marketplace has no plugin {plugin_name}; it has: "
                                 f"{', '.join(names) or 'none'}")
        changed = False
        if marketplace not in declared:
            declared[marketplace] = {"source": source}
            added["marketplace"] = source
            changed = True
        return enable_in(document, a.plugin) or changed

    if not change_plugins_file(root, folder, "claude", add_claude, label):
        out(f"{label} already lists {a.plugin} for claude; nothing changed")
        return 0
    extra = (f"; declared the marketplace {marketplace} ({source_value(added['marketplace'])})"
             if "marketplace" in added else "")
    out(f"{label}: plugin {a.plugin} added for claude{extra}")
    out(f"install it with: sandbox-ai plugins claude; {use_hint(a.name, 'claude')}")
    return 0


def kit_agents_naming(root, folder, kit_path, plugin, wanted):
    """The agents (WANTED, else all of the kit's) whose marketplaces.json names PLUGIN, enabled or disabled."""
    agents = []
    for agent in chosen_agents([wanted] if wanted else [], agents_of(kit_path)):
        try:
            document, _ = read_plugins_file(root, folder, agent)
        except ValueError:
            if wanted:
                raise
            continue  # another agent's file; this command is not about it
        named = [p for key in ("plugins", "disabled") if isinstance(document.get(key), list) for p in document[key]]
        if plugin in named:
            agents.append(agent)
    return agents


def cmd_remove_plugin(a, out):
    root, folder, label = plugin_kit(a.name, a.agent or "claude", out)
    agents = kit_agents_naming(root, folder, root / folder, a.plugin, a.agent)
    if not agents:
        raise ValueError(f"{label} does not list the plugin {printable(a.plugin)}"
                         + (f" for {a.agent}" if a.agent else ""))
    for agent in agents:
        def remove(document):
            for key in ("plugins", "disabled"):
                if isinstance(document.get(key), list):
                    document[key] = [p for p in document[key] if p != a.plugin]
            return True
        change_plugins_file(root, folder, agent, remove, label)
        out(f"{label}: removed the plugin {a.plugin} for {agent}"
            + ("; its marketplace stays declared" if agent == "claude" and "@" in a.plugin else ""))
    return 0


def cmd_enable_plugin(a, out):
    """A plugin the kit keeps disabled is enabled again."""
    if a.agent == "codex":
        raise ValueError("plugins for Codex are not supported yet (a kit's codex/marketplaces.json is ignored)")
    root, folder, label = plugin_kit(a.name, a.agent or "claude", out)
    agents = kit_agents_naming(root, folder, root / folder, a.plugin, a.agent)
    if not agents:
        raise ValueError(f"{label} does not list the plugin {printable(a.plugin)}"
                         + (f" for {a.agent}" if a.agent else "")
                         + f"; add it with: sandbox-ai kits add-plugin {a.name} {printable(a.plugin)}")
    for agent in agents:
        if change_plugins_file(root, folder, agent, lambda document: enable_in(document, a.plugin), label):
            out(f"{label}: plugin {a.plugin} enabled for {agent}; {use_hint(a.name, agent)}")
        else:
            out(f"{label}: plugin {a.plugin} is enabled for {agent} already; nothing changed")
    return 0


def cmd_disable_plugin(a, out):
    """A plugin stays declared (and installed) but is off in the sandboxes of the kit; a plugin the kit does not
    name is put in its "disabled" list, which turns it off where an earlier kit (the default kit) enables it."""
    k = kits()
    if a.agent == "codex":
        raise ValueError("plugins for Codex are not supported yet (a kit's codex/marketplaces.json is ignored)")
    root, folder, label = plugin_kit(a.name, a.agent or "claude", out)
    agents = kit_agents_naming(root, folder, root / folder, a.plugin, a.agent)
    if not agents:
        agent = a.agent or "claude"
        pattern = k.NPM_PLUGIN if agent == "opencode" else f"{k.PLUGIN_NAME}@{k.PLUGIN_NAME}"
        if not re.fullmatch(pattern, a.plugin):
            raise ValueError(f"{printable(a.plugin)!r}: " + ("not an npm package name" if agent == "opencode"
                                                            else "a Claude plugin is NAME@MARKETPLACE"))
        agents = [agent]

    def disable(document):
        listed, off = document.get("plugins", []), document.setdefault("disabled", [])
        if a.plugin in off:
            return False
        document["plugins"] = [p for p in listed if p != a.plugin]
        off.append(a.plugin)
        return True
    for agent in agents:
        if change_plugins_file(root, folder, agent, disable, label):
            out(f"{label}: plugin {a.plugin} disabled for {agent}: it stays "
                + ("declared (and installed)" if agent == "claude" else "listed")
                + " but is off in every sandbox with this kit, also where a kit before it enables it")
        else:
            out(f"{label}: plugin {a.plugin} is disabled for {agent} already; nothing changed")
    return 0


def cmd_add_marketplace(a, out):
    k = kits()
    agent = a.agent or "claude"
    if agent != "claude":
        raise ValueError(f"only Claude has plugin marketplaces in kits ({'OpenCode loads npm plugins' if agent == 'opencode' else 'Codex plugins are not supported yet'})")
    if not re.fullmatch(k.PLUGIN_NAME, a.marketplace):
        raise ValueError(f"marketplace name {printable(a.marketplace)!r}: 1-64 letters, digits, '_', '.', '-'")
    source = parse_source(a.marketplace, a.source)
    root, folder, label = plugin_kit(a.name, "claude", out)
    if not change_plugins_file(root, folder, "claude",
                               lambda document: declare_marketplace(document, label, a.marketplace, source), label):
        out(f"{label} already declares the marketplace {a.marketplace} with that source; nothing changed")
        return 0
    out(f"{label}: declared the marketplace {a.marketplace} ({source_value(source)}) for claude; add plugins "
        f"from it with: sandbox-ai kits add-plugin {a.name} PLUGIN@{a.marketplace}")
    if source["source"] != "directory":
        out("note: `sandbox-ai plugins claude` adds it in a sandbox that reaches only its host; the marketplace must "
            f"call itself {a.marketplace} in its marketplace.json; its plugin list: sandbox-ai plugins claude "
            "--marketplaces-only, then sandbox-ai plugins marketplaces")
    return 0


def cmd_remove_marketplace(a, out):
    k = kits()
    if (a.agent or "claude") != "claude":
        raise ValueError("only Claude has plugin marketplaces in kits")
    if not re.fullmatch(k.PLUGIN_NAME, a.marketplace):
        raise ValueError(f"marketplace name {printable(a.marketplace)!r}: 1-64 letters, digits, '_', '.', '-'")
    root, folder, label = plugin_kit(a.name, "claude", out)
    taken = []

    def remove(document):
        declared = document.get("marketplaces")
        if not (isinstance(declared, dict) and a.marketplace in declared):
            raise ValueError(f"{label} does not declare the marketplace {a.marketplace}")
        users = [p for key in ("plugins", "disabled") for p in document.get(key, []) if p.endswith(f"@{a.marketplace}")]
        if users and not a.with_plugins:
            raise ValueError(f"{label} lists plugins of the marketplace {a.marketplace}: {', '.join(users)}; remove "
                             "them first (sandbox-ai kits remove-plugin) or give --with-plugins")
        del declared[a.marketplace]
        for key in ("plugins", "disabled"):
            if key in document:
                document[key] = [p for p in document[key] if p not in users]
        taken.extend(users)
        return True
    change_plugins_file(root, folder, "claude", remove, label)
    out(f"{label}: removed the marketplace {a.marketplace} for claude"
        + (f" with its plugins {', '.join(taken)}" if taken else "")
        + "; what sandbox-ai's plugin store fetched of it stays there (sandbox-ai plugins claude -- marketplace "
          f"remove {a.marketplace} removes it)")
    return 0


def cmd_import_marketplace(a, out):
    """A marketplace Claude (or Codex, in Claude's format) already knows on this machine, declared in a kit with the
    source it has there; with --with-plugins also the plugins the agent enables from it."""
    k = kits()
    agent = a.from_agent
    if agent == "opencode":
        raise ValueError("OpenCode has no marketplaces: its plugins are npm packages; add one with: sandbox-ai kits "
                         f"add-plugin {a.name} PACKAGE --agent opencode (see: sandbox-ai plugins marketplaces "
                         "--agent opencode)")
    if not re.fullmatch(k.PLUGIN_NAME, a.marketplace):
        raise ValueError(f"marketplace name {printable(a.marketplace)!r}: 1-64 letters, digits, '_', '.', '-'")
    check_kit_name(a.name)
    if a.name != "default" or os.path.lexists(k.kits_dir() / a.name):
        user_kit(a.name)  # a kit that cannot change is said first
    found = plugins_module().marketplaces(agent)
    market = next((m for m in found["marketplaces"] if m["name"] == a.marketplace and m["agent"] == agent
                   and (agent == "codex" or m["agent_config"])), None)
    if market is None:
        known = [m["name"] for m in found["marketplaces"] if m["agent"] == agent and (agent == "codex"
                                                                                     or m["agent_config"])]
        where = "Claude" if agent == "claude" else "Codex"
        raise ValueError(f"{where} on this machine knows no marketplace {a.marketplace} (it knows: "
                         f"{', '.join(known) or 'none'}); declare another one with: sandbox-ai kits add-marketplace "
                         f"{a.name} NAME SOURCE")
    if agent == "codex":
        if not market["import"]:
            raise ValueError(f"Codex's marketplace {a.marketplace} cannot be declared in a kit: {market['problem']}")
        name, source = market["import"]["name"], market["import"]["source"]
    else:
        if not market["importable"]:
            raise ValueError(f"Claude's marketplace {a.marketplace} cannot be declared in a kit: {market['problem']}")
        name, source = a.marketplace, market["source"]
    chosen = [f"{p['name']}@{name}" for p in market["plugins"] if (p.get("agent") or {}).get("enabled") is True] \
        if a.with_plugins else []
    root, folder, label = plugin_kit(a.name, "claude", out)
    added = []

    def change(document):
        changed = declare_marketplace(document, label, name, source)
        for plugin in chosen:
            if enable_in(document, plugin):
                added.append(plugin)
                changed = True
        return changed
    if not change_plugins_file(root, folder, "claude", change, label):
        out(f"{label} declares the marketplace {name} with that source already"
            + (" and lists its plugins" if chosen else "") + "; nothing changed")
        return 0
    out(f"{label}: declared the marketplace {name} ({source_value(source)}) for claude, as "
        f"{'Claude' if agent == 'claude' else 'Codex'} has it on this machine"
        + (f" (Codex calls it {a.marketplace})" if name != a.marketplace else ""))
    if added:
        out(f"{label}: enabled {', '.join(added)}, as {'Claude' if agent == 'claude' else 'Codex'} enables them")
    elif a.with_plugins:
        out(f"{'Claude' if agent == 'claude' else 'Codex'} enables no plugin of it here")
    out(f"install with: sandbox-ai plugins claude; {use_hint(a.name, 'claude')}")
    return 0


# --- command line ----------------------------------------------------------------------------------

# --- hooks -----------------------------------------------------------------------------------------

def hook_kit(name):
    """(what the approval records as the kit, {agent: its folder}) of KIT: a kit's name (yours, built-in, default)
    or a folder given as --kit-dir (with a "/" in it)."""
    if "/" in name or name.startswith("."):
        path = Path(os.path.normpath(Path(name).expanduser().absolute()))
        if not path.is_dir():
            raise ValueError(f"{printable(name)} is not a folder")
        return str(path), {agent: path / agent for agent in agents_of(path)}
    kit = find_kit(name)
    return kit["name"], {agent: Path(info["path"]) for agent, info in kit["agents"].items()}


def review(agent, found, out):
    """Everything about a kit's hooks for AGENT a review needs: each hook, and each file with its content."""
    out(f"{agent}: hooks in {printable(found['path'])}")
    for line in hook_lines(found):
        out(f"  {printable(line)}")
    for item in found["files"]:
        out("")
        out(f"  --- {printable(item['path'])} ({item['size']} bytes{', executable' if item['executable'] else ''}, "
            f"sha256 {item['sha256'][:16]})")
        data = read_hook_file(found["path"], item["path"]) if item["size"] <= REVIEW_LIMIT else None
        if data is None:
            out("  (larger than 64 KiB: read it yourself before approving)")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            out("  (not text: read it yourself before approving)")
            continue
        for line in text.splitlines()[:400]:
            out("  | " + printable(line[:300]))
        if len(text.splitlines()) > 400:
            out("  (more lines: read the file yourself before approving)")


def read_hook_file(folder, relative):
    """The bytes of FOLDER/RELATIVE (at most REVIEW_LIMIT), nothing followed; None if it is not there as read."""
    k = kits()
    parts = PurePosixPath(relative).parts
    try:
        fd = k.open_dir(folder, parts[:-1])
    except (OSError, ValueError):
        return None
    if fd is None:
        return None
    try:
        data, _ = k.read_at(fd, parts[-1], REVIEW_LIMIT)
    finally:
        os.close(fd)
    return data


def cmd_trust_hooks(a, out):
    k = kits()
    kit, folders = hook_kit(a.name)
    found = []
    for agent in chosen_agents(a.agent, folders):
        folder = folders[agent] / k.HOOKS_DIR if agent in folders else None
        if folder is not None and os.path.lexists(folder):
            found.append((agent, k.read_hooks(agent, folder)))
    if not found:
        raise ValueError(f"kit {printable(kit)} has no hooks" + (f" for {', '.join(a.agent)}" if a.agent else ""))
    if a.revoke:
        for agent, info in found:
            k.change_trust(agent, info, kit, approve=False)
            out(f"kit {printable(kit)}: the approval of its hooks for {agent} is taken back; they do not run")
        return 0
    for agent, info in found:
        review(agent, info, out)
        out("")
    broken = [(agent, info) for agent, info in found if info["errors"]]
    if broken:
        raise ValueError("these hooks cannot be approved: " + "; ".join(f"{agent}: {info['errors'][0]}"
                                                                         for agent, info in broken))
    for agent, info in found:
        for tree, why in agent_writable(info["path"]):
            out(f"warning: {printable(info['path'])} lies in or holds {printable(tree)}, {why}: an agent may have "
                "written these hooks; a start refuses kits there")
    if not a.yes:
        if not sys.stdin.isatty():
            raise ValueError("review the hooks above, then approve them with --yes")
        answer = input("Approve exactly these hooks? Any later change takes the approval away. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            out("not approved")
            return 1
    for agent, info in found:
        k.change_trust(agent, info, kit)
        out(f"kit {printable(kit)}: {len(info['hooks'])} hook(s) for {agent} approved (sha256 {info['sha256'][:16]}); "
            "they run in its sandboxes until they change")
    return 0


def read_hook_source(value):
    """(name, bytes, executable) of the hook file VALUE: a regular file, not a symlink."""
    path = value.rstrip("/")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise ValueError(f"{printable(value)} does not exist") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{printable(value)} is a symlink; give the file itself") from None
        raise ValueError(f"{printable(value)}: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{printable(value)} is not a regular file")
        if info.st_size > kits().HOOK_BYTES:
            raise ValueError(f"{printable(value)} is larger than {kits().HOOK_BYTES >> 20} MiB")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(kits().HOOK_BYTES + 1)
    finally:
        os.close(fd)
    return os.path.basename(path), data, bool(info.st_mode & 0o111)


def write_hook_file(folder_fd, name, data, mode):
    """Write NAME (new) in the hooks folder FOLDER_FD; an existing entry is an error, never replaced."""
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600,
                     dir_fd=folder_fd)
    except FileExistsError:
        raise ValueError(f"the kit's hooks already have {printable(name)}; remove it first (sandbox-ai kits "
                         "remove-hook)") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        os.fchmod(stream.fileno(), mode)


def change_hooks_file(folder_fd, agent, change):
    """Rewrite hooks.json in FOLDER_FD with CHANGE(groups) (groups in their normal form)."""
    k = kits()
    data, why = k.read_at(folder_fd, k.HOOKS_FILE, k.HOOK_BYTES)
    if why:
        raise ValueError(f"{k.HOOKS_FILE}: {why}")
    groups = k.hook_groups(agent, json.loads(data.decode("utf-8")))[0] if data else {}
    groups = {event: entries for event, entries in change(groups).items() if entries}
    temporary = f".{k.HOOKS_FILE}.{os.getpid()}-{os.urandom(4).hex()}"
    write_hook_file(folder_fd, temporary, (json.dumps({"hooks": groups}, indent=2) + "\n").encode(), 0o644)
    os.replace(temporary, k.HOOKS_FILE, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)


def cmd_add_hook(a, out):
    k = kits()
    root, kit = user_kit(a.name)
    agents = skill_agents(kit, a.agent)
    name, data, executable = read_hook_source(a.file)
    name = a.as_name or name
    check_entry_name(name, "hook file")
    plugin, definitions = name.endswith(k.PLUGIN_SUFFIXES), name.endswith(".json")
    if definitions and (a.event or a.matcher or a.timeout):
        raise ValueError("--event, --matcher and --timeout are for a hook script; a .json file brings its own")
    done, notes = [], []
    for agent in agents:
        if agent == "opencode" and not plugin:
            notes.append(f"opencode: skipped: OpenCode runs plugin modules ({', '.join(k.PLUGIN_SUFFIXES)}) as hooks")
            continue
        if agent != "opencode" and plugin:
            notes.append(f"{agent}: skipped: a plugin module is for OpenCode; give a script or a hooks.json")
            continue
        with opened(root, (a.name, agent, k.HOOKS_DIR), create=True) as fd:
            if agent == "opencode":
                write_hook_file(fd, name, data, 0o644)
            elif definitions:
                added = k.hook_groups(agent, json.loads(data.decode("utf-8")))[0]
                change_hooks_file(fd, agent, lambda groups: {**groups, **{
                    event: groups.get(event, []) + entries for event, entries in added.items()}})
            else:
                if name == k.HOOKS_FILE:
                    raise ValueError(f"{k.HOOKS_FILE} is the kit's list of hooks; give it a .json name of its own")
                event = a.event or "PreToolUse"
                handler = {"type": "command", "command": f"{k.HOOK_DIR_VARIABLE}/{name}",
                           **({"timeout": a.timeout} if a.timeout else {})}
                entry = {**({"matcher": a.matcher} if a.matcher else {}), "hooks": [handler]}
                k.hook_groups(agent, {event: [entry]})  # checked before anything is written
                write_hook_file(fd, name, data, 0o755)
                change_hooks_file(fd, agent, lambda groups: {**groups, event: groups.get(event, []) + [entry]})
                if not data.startswith(b"#!"):
                    notes.append(f"{agent}: {name} has no #! line; the agent runs it as a program")
        done.append(agent)
    if not done:
        raise ValueError("nothing added: " + "; ".join(notes))
    out(f"kit {a.name}: hook {name} added for {', '.join(done)}")
    for note in notes:
        out(f"note: {printable(note)}")
    out(f"note: the kit's hooks run only once approved: review them with sandbox-ai kits trust-hooks {a.name}")
    return 0


def cmd_remove_hook(a, out):
    k = kits()
    root, kit = user_kit(a.name)
    check_entry_name(a.file, "hook file")
    removed = []
    for agent in skill_agents(kit, a.agent):
        with opened(root, (a.name, agent, k.HOOKS_DIR)) as fd:
            if fd is None:
                continue
            found = lstat_at(fd, a.file) is not None
            if found:
                os.unlink(a.file, dir_fd=fd)
            if agent != "opencode" and lstat_at(fd, k.HOOKS_FILE) is not None:
                # ${HOOK_DIR}/FILE as a whole word: not ${HOOK_DIR}/FILE.bak or ${HOOK_DIR}/FILEx.
                runs = re.compile(re.escape(f"{k.HOOK_DIR_VARIABLE}/{a.file}") + r"(?![A-Za-z0-9_./-])")
                dropped = []

                def without(groups):
                    kept = {}
                    for event, entries in groups.items():
                        for entry in entries:
                            handlers = [h for h in entry["hooks"] if not runs.search(h["command"])]
                            dropped.extend(h for h in entry["hooks"] if runs.search(h["command"]))
                            if handlers:
                                kept.setdefault(event, []).append({**entry, "hooks": handlers})
                    return kept
                change_hooks_file(fd, agent, without)
                found = found or bool(dropped)
            if found:
                removed.append(agent)
    if not removed:
        raise ValueError(f"kit {a.name} has no hook {printable(a.file)}")
    out(f"kit {a.name}: hook {printable(a.file)} removed for {', '.join(removed)}")
    return 0


def cmd_list(a, out):
    data = inventory(a.agent)
    if a.json:
        out(json.dumps(data, indent=2))
    else:
        render_list(data, out)
    return 0


def cmd_show(a, out):
    kit = find_kit(a.name)
    if a.json:
        out(json.dumps(kit, indent=2))
        return 0
    k = kits()
    known, installed = k.installed()
    render(kit, {"installed": {"plugins": sorted(installed), "marketplaces": sorted(known)}}, out, detail=True)
    for agent, info in kit["agents"].items():
        out(f"  {agent} path: {printable(info['path'])}")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai kits", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
    commands = p.add_subparsers(dest="command", metavar="COMMAND")

    def agent_option(command, many=True, choices=AGENTS):
        command.add_argument("-a", "--agent", action="append" if many else "store", choices=choices,
                             default=[] if many else None, help="the agent (repeatable)" if many else "the agent")

    listed = commands.add_parser("list", help="every kit and what it brings per agent")
    listed.add_argument("--json", action="store_true", help="the inventory as JSON (for programs)")
    agent_option(listed, many=False)
    shown = commands.add_parser("show", help="one kit")
    shown.add_argument("name")
    shown.add_argument("--json", action="store_true")
    new = commands.add_parser("new", help="create a kit of yours")
    new.add_argument("name")
    agent_option(new)
    add_skill = commands.add_parser("add-skill", help="copy a skill folder into a kit")
    add_skill.add_argument("name")
    add_skill.add_argument("path", help="the skill folder (with SKILL.md at its top)")
    agent_option(add_skill)
    add_skill.add_argument("--as", dest="skill", metavar="SKILL", help="the skill's name in the kit")
    new_skill = commands.add_parser("new-skill", help="write a skill from a template")
    new_skill.add_argument("name")
    new_skill.add_argument("skill")
    agent_option(new_skill)
    new_skill.add_argument("-d", "--description", metavar="TEXT", help="one line: what it does and when to use it")
    remove_skill = commands.add_parser("remove-skill", help="remove a skill from a kit")
    remove_skill.add_argument("name")
    remove_skill.add_argument("skill")
    agent_option(remove_skill)
    add_plugin = commands.add_parser("add-plugin", help="enable a plugin in a kit")
    add_plugin.add_argument("name", help="your kit, or default")
    add_plugin.add_argument("plugin", help="Claude: NAME@MARKETPLACE; OpenCode: an npm package")
    agent_option(add_plugin, many=False)
    add_plugin.add_argument("-s", "--source", metavar="SOURCE", help="declare the marketplace too: OWNER/REPO, "
                                                                     "https://..., sandbox-ai")
    remove_plugin = commands.add_parser("remove-plugin", help="take a plugin out of a kit")
    remove_plugin.add_argument("name", help="your kit, or default")
    remove_plugin.add_argument("plugin")
    agent_option(remove_plugin, many=False)
    for command, text in (("enable-plugin", "enable a plugin the kit keeps disabled"),
                          ("disable-plugin", "keep a plugin declared (and installed) but off with this kit")):
        switch = commands.add_parser(command, help=text, description=text + (
            "; a plugin the kit does not name is put in its \"disabled\" list: off where a kit before it (the "
            "default kit) enables it" if command == "disable-plugin" else ""))
        switch.add_argument("name", help="your kit, or default")
        switch.add_argument("plugin", help="Claude: NAME@MARKETPLACE; OpenCode: an npm package")
        agent_option(switch, many=False)
    add_market = commands.add_parser("add-marketplace", help="declare a Claude plugin marketplace in a kit")
    add_market.add_argument("name", help="your kit, or default")
    add_market.add_argument("marketplace")
    add_market.add_argument("source", help="OWNER/REPO (GitHub), https://... (Git, or a marketplace.json), sandbox-ai")
    agent_option(add_market, many=False)
    remove_market = commands.add_parser("remove-marketplace", help="take a Claude plugin marketplace out of a kit")
    remove_market.add_argument("name", help="your kit, or default")
    remove_market.add_argument("marketplace")
    remove_market.add_argument("--with-plugins", action="store_true",
                               help="also take out the kit's plugins of it (else they must be removed first)")
    agent_option(remove_market, many=False)
    import_market = commands.add_parser(
        "import-marketplace", help="declare a marketplace Claude or Codex already knows on this machine",
        description="Declare in a kit a marketplace Claude (~/.claude) or Codex (~/.codex/config.toml, if it is in "
                    "Claude's format) already knows on this machine, with the source it has there. Nothing is "
                    "fetched: `sandbox-ai plugins claude` does that in a sandbox. OpenCode has no marketplaces.")
    import_market.add_argument("name", help="your kit, or default")
    import_market.add_argument("marketplace", help="its name as the agent has it (sandbox-ai plugins marketplaces)")
    import_market.add_argument("--from", dest="from_agent", choices=AGENTS, default="claude",
                               help="whose configuration (default: claude)")
    import_market.add_argument("--with-plugins", action="store_true",
                               help="also enable in the kit the plugins of it the agent enables")
    add_hook = commands.add_parser("add-hook", help="add a hook to a kit (it runs once approved)")
    add_hook.add_argument("name")
    add_hook.add_argument("file", help="a hook script, a hooks.json (Claude Code's \"hooks\" format), or an "
                                       "OpenCode plugin module (.js, .mjs, .ts)")
    agent_option(add_hook)
    add_hook.add_argument("--as", dest="as_name", metavar="FILE", help="the file's name in the kit")
    add_hook.add_argument("-e", "--event", help="when a script runs: PreToolUse (default), PostToolUse, Stop, ...")
    add_hook.add_argument("-m", "--matcher", help="which tools (Claude Code and Codex): Bash, Write|Edit, ... "
                                                  "(default: all)")
    add_hook.add_argument("-t", "--timeout", type=int, metavar="SECONDS")
    remove_hook = commands.add_parser("remove-hook", help="remove a hook file (and what runs it) from a kit")
    remove_hook.add_argument("name")
    remove_hook.add_argument("file")
    agent_option(remove_hook)
    trust_hooks = commands.add_parser("trust-hooks", help="review a kit's hooks and approve them")
    trust_hooks.add_argument("name", metavar="KIT", help="a kit's name, default, or a --kit-dir folder")
    agent_option(trust_hooks)
    choice = trust_hooks.add_mutually_exclusive_group()
    choice.add_argument("--yes", action="store_true", help="approve what is shown without asking")
    choice.add_argument("--revoke", action="store_true", help="take the approval back")
    delete = commands.add_parser("delete", help="delete a kit of yours")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true", help="really delete it")
    return p


COMMANDS = {"list": cmd_list, "show": cmd_show, "new": cmd_new, "add-skill": cmd_add_skill,
            "new-skill": cmd_new_skill, "remove-skill": cmd_remove_skill, "add-plugin": cmd_add_plugin,
            "remove-plugin": cmd_remove_plugin, "add-marketplace": cmd_add_marketplace, "delete": cmd_delete,
            "enable-plugin": cmd_enable_plugin, "disable-plugin": cmd_disable_plugin,
            "remove-marketplace": cmd_remove_marketplace, "import-marketplace": cmd_import_marketplace,
            "add-hook": cmd_add_hook, "remove-hook": cmd_remove_hook, "trust-hooks": cmd_trust_hooks}


def main(argv, out=print):
    argv = list(argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "list")
    a = parser().parse_args(argv)
    try:
        return COMMANDS[a.command](a, out)
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai kits: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
