#!/usr/bin/env python3
"""sandbox-ai presets: ready setups to start an agent with.

  sandbox-ai presets list [--json]                      the built-in presets and your own
  sandbox-ai presets show NAME [--json]                 what NAME sets, needs and asks for
  sandbox-ai presets check NAME [--value KEY=VALUE]... [--agent AGENT]
                                                        what is missing before NAME can start (values, MCP
                                                        servers, VPN profiles, LLM servers the agent can use)
  sandbox-ai presets dirs [list] [--json]               the extra preset directories (defaults.json)
  sandbox-ai presets dirs add|remove DIR                add or remove one
  sandbox-ai AGENT --preset NAME [--preset-value KEY=VALUE]... [options]   start with it

A preset is launcher options (with {placeholders} for what only you know: a folder, a port, a device, an LLM
server), MCP servers by name, an optional ruleset file and an optional first task for the agent. The built-in
presets ship with sandbox-ai (sandbox/presets/*.json); your own, private ones live in
~/.config/sandbox-ai/presets/*.json, outside every workspace (an agent must not write what it starts with), and
win over a built-in one of the same id. Further directories of presets (a private collection, a team's checkout)
are listed in ~/.config/sandbox-ai/defaults.json as "preset_dirs" (`sandbox-ai presets dirs add DIR`): yours win
over them, the first listed over the later ones, and they over the built-in presets. A directory inside a folder
sandboxes can or could write (a workspace, a rw mount, a kept state) is refused and skipped, and so is a preset
file or ruleset there that is a symlink. The command line wins over the preset, the preset over nothing else:
a project's .sandbox-ai.json, your defaults, the security mode and the administrator's policy apply as always.

A preset never holds a secret: options only name them (--secret-env NAME, --git-token-secret URL=NAME), and
options that copy a login in are refused. Format (JSON):

  {"format": "sandbox-ai preset", "version": 1, "id": "software-analysis", "title": "...", "description": "...",
   "category": "analysis", "agents": ["claude", "codex", "opencode"], "options": ["--offline", "--mount",
   "{samples}:/mnt/samples:ro"], "mcp": ["hub:capa"], "rules": "software-analysis.rules.json",
   "values": {"samples": {"label": "Folder with the samples", "kind": "folder"}},
   "prompt": "...", "notes": ["..."]}

Standard library only.
"""
import argparse
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
BUILTIN = HERE / "presets"
FORMAT = "sandbox-ai preset"
LIMIT = 256 << 10
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
VALUE_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
KINDS = ("text", "folder", "file", "port", "device", "host", "secret", "llm", "vpn", "number")
CATEGORIES = ("simple", "development", "llm", "analysis", "research", "media", "privacy", "testing", "fun")
AGENTS = ("claude", "codex", "opencode")
KEYS = {"format", "version", "id", "title", "description", "category", "agents", "options", "mcp", "rules",
        "values", "prompt", "notes", "needs"}
# What a preset may not set: what copies a login in (a preset is meant to be passed on), what only a start decides,
# and what is chosen another way (the security mode: --mode on the command line or the project's "mode").
REFUSED = ("--copy-auth", "--auth-file", "--import-codex-auth", "--git-credentials", "--git-token-file",
           "--workspace", "--config", "--no-config", "--session", "--dry-run", "--tmux-socket-dir", "--pod-name",
           "--mcp-socket-dir", "--mcp-approvals-dir", "--helper-of", "--start-id", "--name", "--rules", "--preset",
           "--preset-value", "--")
# A value of these kinds is checked before it goes into an option.
CHECKS = {"port": re.compile(r"[1-9][0-9]{0,4}"), "number": re.compile(r"[0-9]+(?:\.[0-9]+)?"),
          "host": re.compile(r"[A-Za-z0-9.:\[\]_-]{1,255}"), "secret": re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}"),
          # An LLM server as --llm takes it: NAME or NAME:MODEL (llm.py's NAME and MODEL_NAME; qwen2.5-coder:7b,
          # org/Model-GGUF).
          "llm": re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}(?::[\w.:/@+-]{1,200})?", re.ASCII),
          "vpn": re.compile(r"[A-Za-z0-9_.-]{1,64}")}


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def user_dir():
    return config_home() / "sandbox-ai" / "presets"


DIRS_KEY = "preset_dirs"  # defaults.json: the extra preset directories, in order
DIRS_LIMIT = 32
# The launcher sets this to its exposed_before() (the trees sandboxes could write in earlier runs) when it loads
# this module; elsewhere net.py's launcher gives it, together with the workspaces of running sandboxes.
EXPOSED_BEFORE = None


def defaults_path():
    return config_home() / "sandbox-ai" / "defaults.json"


def check_dirs(value, where=DIRS_KEY):
    """VALUE as defaults.json's preset_dirs: a list of distinct absolute paths; ValueError otherwise."""
    if not isinstance(value, list) or len(value) > DIRS_LIMIT or not all(
            isinstance(v, str) and os.path.isabs(v) and "\0" not in v and len(v) < 4096
            and not any(ord(c) < 32 for c in v) for v in value):
        raise ValueError(f"{where} is a list of at most {DIRS_LIMIT} absolute paths of directories")
    normal = [os.path.normpath(v) for v in value]
    if len(set(normal)) != len(normal):
        raise ValueError(f"{where} names a directory twice")
    return normal


def listed_dirs():
    """The extra preset directories of defaults.json, in order ([] without the file or the key); a damaged file
    or value is a ValueError."""
    try:
        document = json.loads(defaults_path().read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{defaults_path()}: {exc}") from None
    if not isinstance(document, dict) or DIRS_KEY not in document:
        return []
    return [Path(p) for p in check_dirs(document[DIRS_KEY], f"{defaults_path()}: {DIRS_KEY}")]


def _module(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_presets", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def writable_trees():
    """[(tree, why)]: the folders sandboxes can or could write (workspaces, rw mounts, kept states of earlier runs,
    the workspaces of running sandboxes). An error when they cannot be known: a directory is not trusted then."""
    if EXPOSED_BEFORE is not None:
        return [(Path(t), "a folder sandboxes could write in an earlier run") for t in EXPOSED_BEFORE()]
    net = _module("net")
    trees = [(Path(t), "a folder sandboxes could write in an earlier run") for t in net.launcher().exposed_before()]
    trees += [(Path(record["workspace"]), f"the workspace of the running sandbox {name}")
              for name, record in net.running().items() if isinstance(record.get("workspace"), str)]
    return trees


def unsafe_dir(path, trees):
    """Why the preset directory PATH must not be used (None when it may): it lies in a folder sandboxes can write
    (an agent must not write what it is started with), or it is sandbox-ai's own or your presets' folder."""
    literal = Path(os.path.normpath(os.path.abspath(path)))
    real = Path(os.path.realpath(path))
    for candidate in dict.fromkeys([literal, real]):
        for tree, why in trees:
            for form in dict.fromkeys([Path(os.path.normpath(tree)), Path(os.path.realpath(tree))]):
                if candidate == form or form in candidate.parents:
                    return f"it lies in {form}, {why}"
    if real == Path(os.path.realpath(BUILTIN)):
        return "it is the folder of sandbox-ai's built-in presets"
    if real == Path(os.path.realpath(user_dir())):
        return "it is the folder of your own presets, read anyway"
    return None


def extra_dirs(out_problems=None):
    """The extra preset directories that may be used, in order; the others are left out (and said in
    OUT_PROBLEMS)."""
    try:
        listed = listed_dirs()
    except ValueError as exc:
        if out_problems is not None:
            out_problems.append(f"extra preset directories: {exc}")
        return []
    if not listed:
        return []
    try:
        trees = writable_trees()
    except Exception as exc:  # noqa: BLE001 - not knowing where sandboxes write means not trusting the directories
        if out_problems is not None:
            out_problems.append(f"extra preset directories skipped: cannot tell where sandboxes write ({exc})")
        return []
    usable = []
    for directory in listed:
        why = unsafe_dir(directory, trees)
        if why:
            if out_problems is not None:
                out_problems.append(f"preset directory {directory} skipped: {why}")
            continue
        usable.append(directory)
    return usable


def directories(out_problems=None):
    """[(directory, origin)] from the lowest precedence to the highest: the built-in presets, the extra directories
    (the first listed wins over the later ones), yours (they win over all)."""
    extra = [(directory, "extra") for directory in reversed(extra_dirs(out_problems))]
    return [(BUILTIN, "built-in"), *extra, (user_dir(), "yours")]


def problems(document, where="preset"):
    """Why DOCUMENT is not a usable preset ([] when it is). Shape only; the launcher checks the options themselves."""
    found = []
    if not isinstance(document, dict):
        return [f"{where}: not a JSON object"]
    if document.get("format") != FORMAT or document.get("version") != 1:
        found.append(f'{where}: "format" must be "{FORMAT}" and "version" 1')
    unknown = set(document) - KEYS
    if unknown:
        found.append(f"{where}: unknown keys {sorted(unknown)}")
    if not isinstance(document.get("id"), str) or not ID.fullmatch(document.get("id", "")):
        found.append(f"{where}: id is lower-case letters, digits and -, at most 64")
    for key in ("title", "description"):
        if not isinstance(document.get(key), str) or not document.get(key, "").strip():
            found.append(f"{where}: {key} is a non-empty text")
    if document.get("category", "simple") not in CATEGORIES:
        found.append(f"{where}: category is one of {', '.join(CATEGORIES)}")
    agents = document.get("agents", list(AGENTS))
    if not isinstance(agents, list) or not agents or set(agents) - set(AGENTS):
        found.append(f"{where}: agents is a list of {', '.join(AGENTS)}")
    for key in ("options", "mcp", "notes", "needs"):
        value = document.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            found.append(f"{where}: {key} is a list of texts")
    values = document.get("values", {})
    if not isinstance(values, dict):
        found.append(f"{where}: values is an object")
        values = {}
    for name, spec in values.items():
        if not VALUE_NAME.fullmatch(name) or not isinstance(spec, dict) or spec.get("kind", "text") not in KINDS \
                or not isinstance(spec.get("label", ""), str) or set(spec) - {"label", "kind", "default", "optional",
                                                                             "hint", "vpn_type"}:
            found.append(f"{where}: value {name!r} needs a label, a kind ({', '.join(KINDS)}) and may have a "
                         "default, optional, hint and (kind vpn) vpn_type")
        elif "vpn_type" in spec and (spec.get("kind") != "vpn" or not isinstance(spec["vpn_type"], str)
                                     or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", spec["vpn_type"])):
            found.append(f"{where}: value {name!r}: vpn_type (the type its VPN profile must have, e.g. tor) is for a "
                         "value of kind vpn")
    for option in document.get("options", []) if isinstance(document.get("options"), list) else []:
        if not isinstance(option, str):
            continue
        flag = option.split("=", 1)[0]
        if flag in REFUSED:
            found.append(f"{where}: {flag} is not allowed in a preset")
        for name in PLACEHOLDER.findall(option):
            if name not in values:
                found.append(f"{where}: {{{name}}} is used but not declared in values")
    rules = document.get("rules")
    if rules is not None and (not isinstance(rules, str) or "/" in rules or not rules.endswith(".json")):
        found.append(f"{where}: rules names a JSON file beside the preset (no path)")
    if document.get("prompt") is not None and not isinstance(document.get("prompt"), str):
        found.append(f"{where}: prompt is a text")
    return found


def read_bytes(path, follow=True):
    """The bytes of PATH; FOLLOW=False refuses a symlink (and anything but a regular file)."""
    if follow:
        return Path(path).read_bytes()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{path}: a symlink, not followed in an extra preset directory") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path}: not a regular file")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            size += len(chunk)
            if size > LIMIT:
                break
            chunks.append(chunk)
        return b"".join(chunks) + (b" " if size > LIMIT else b"")
    finally:
        os.close(fd)


def read(path, follow=True):
    data = read_bytes(path, follow)
    if len(data) > LIMIT:
        raise ValueError(f"{path}: larger than {LIMIT >> 10} KiB")
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError(f"{path}: not valid JSON") from None
    found = problems(document, str(path))
    if found:
        raise ValueError("; ".join(found))
    return document


def catalog(out_problems=None):
    """{id: preset}, each with "_path" and "_origin" (built-in, extra or yours; "_dir" for an extra one); damaged
    files are skipped (and reported to OUT_PROBLEMS). In an extra directory no symlink is followed: a preset or
    ruleset that is one is skipped."""
    found = {}
    for directory, origin in directories(out_problems):
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            if path.name.endswith(".rules.json"):
                continue
            try:
                document = read(path, follow=origin != "extra")
                if origin == "extra" and document.get("rules") and (path.parent / document["rules"]).is_symlink():
                    raise ValueError(f"{path}: its ruleset {document['rules']} is a symlink, not followed in an "
                                     "extra preset directory")
            except (OSError, ValueError) as exc:
                if out_problems is not None:
                    out_problems.append(str(exc))
                continue
            found[document["id"]] = {**document, "_path": str(path), "_origin": origin,
                                     **({"_dir": str(directory)} if origin == "extra" else {})}
    return found


LOADED = None  # the file of the preset load() gave last (the launcher checks it lies where no agent writes)


def load(name):
    global LOADED
    presets = catalog()
    if name not in presets:
        raise ValueError(f"no preset {name!r}; see: sandbox-ai presets list")
    LOADED = presets[name]
    return presets[name]


def parse_values(pairs):
    values = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not VALUE_NAME.fullmatch(key):
            raise ValueError(f"--preset-value expects KEY=VALUE: {pair!r}")
        values[key] = value
    return values


def resolved_values(preset, given):
    """{name: value} for every placeholder: given, else its default; ValueError naming what is missing or wrong."""
    declared = preset.get("values", {})
    unknown = set(given) - set(declared)
    if unknown:
        raise ValueError(f"preset {preset['id']} has no value {', '.join(sorted(unknown))}; it takes: "
                         f"{', '.join(sorted(declared)) or 'none'}")
    values, missing = {}, []
    for name, spec in declared.items():
        value = given.get(name, spec.get("default"))
        if value is None or value == "":
            if not spec.get("optional"):
                missing.append(name)
            continue
        value = str(value)
        kind = spec.get("kind", "text")
        if kind in CHECKS and not CHECKS[kind].fullmatch(value):
            raise ValueError(f"preset {preset['id']}: {name} must be a {kind}: {value!r}")
        if kind == "port" and not 1 <= int(value) <= 65535:
            raise ValueError(f"preset {preset['id']}: {name} is a port from 1 to 65535: {value!r}")
        if kind in ("folder", "file", "device"):
            value = os.path.expanduser(value)
            if not os.path.isabs(value):
                raise ValueError(f"preset {preset['id']}: {name} needs an absolute path (or ~/...): {value!r}")
        if any(ord(c) < 32 for c in value):
            raise ValueError(f"preset {preset['id']}: {name} contains control characters")
        values[name] = value
    if missing:
        hints = ", ".join(f"--preset-value {name}=... ({declared[name].get('label', name)})" for name in missing)
        raise ValueError(f"preset {preset['id']} needs: {hints}")
    return values


def expand(preset, given=None, extras=True):
    """The launcher options of PRESET with its values filled in. An option whose placeholders are all optional and
    empty is left out, with the flag before it (["--mount", "{extra}"] goes when extra is not given)."""
    values = resolved_values(preset, given or {})
    options, result = list(preset.get("options", [])), []
    index = 0
    while index < len(options):
        option = options[index]
        names = PLACEHOLDER.findall(option)
        if names and any(name not in values for name in names):
            # an optional value left empty: drop this token, and the flag before it when it takes it as its value
            if result and result[-1].startswith("--") and not option.startswith("--"):
                result.pop()
            index += 1
            continue
        result.append(PLACEHOLDER.sub(lambda m: values[m.group(1)], option))
        index += 1
    if not extras:
        return result
    return result + extras_of(preset, values)


def extras_of(preset, values):
    """--mcp and --rules for PRESET's servers and ruleset file (added by sandbox-ai, not options of the preset)."""
    result = []
    for server in preset.get("mcp", []):
        # A catalog entry (hub:capa) is started by the name `sandbox-ai mcp add` gave its definition (capa).
        result += ["--mcp", PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), server).split(":", 1)[-1]]
    if preset.get("rules"):
        result += ["--rules", str(Path(preset["_path"]).parent / preset["rules"])]
    return result


def prompt_args(preset, agent, values=None):
    """The agent's arguments that give it the preset's first task (none without one), its values filled in."""
    text = preset.get("prompt")
    if not text:
        return []
    text = PLACEHOLDER.sub(lambda m: (values or {}).get(m.group(1), m.group(0)), text)
    return ["--prompt", text] if agent == "opencode" else [text]


def missing(preset, given=None, agent=None, mcp_names=None):
    """What keeps PRESET from starting: [text]; empty when nothing does."""
    found = []
    if agent and agent not in preset.get("agents", AGENTS):
        found.append(f"preset {preset['id']} is for {', '.join(preset.get('agents', AGENTS))}, not {agent}")
    try:
        resolved_values(preset, given or {})
    except ValueError as exc:
        found.append(str(exc))
    found += registered_values(preset, given or {}, agent)
    known = mcp_names if mcp_names is not None else registered_mcp()
    for server in preset.get("mcp", []):
        if PLACEHOLDER.search(server):
            continue
        name = server.split(":", 1)[-1]
        if name not in known and server not in known:
            found.append(f"MCP server {server} is not added yet: sandbox-ai mcp add {server}")
    return found


def check_vpn_types(preset, values):
    """ValueError when a value of PRESET with a vpn_type (the tor preset's: tor) names a VPN profile of another type:
    "Everything through Tor" must never start behind any VPN. A profile that does not exist is left to the start's
    own check of --vpn (it names the profiles there are)."""
    for name, spec in preset.get("values", {}).items():
        wanted, value = spec.get("vpn_type"), values.get(name)
        if spec.get("kind") != "vpn" or not wanted or not value:
            continue
        try:
            profile = _module("vpn").load_profile(value)
        except ValueError:
            continue
        if profile.get("type") != wanted:
            raise ValueError(f"preset {preset['id']}: {name} must name a {wanted} VPN profile; {value} is of type "
                             f"{profile.get('type')} (sandbox-ai vpn list)")


def registered_values(preset, given, agent=None):
    """What keeps the VPN profiles and LLM servers PRESET's values name from a start: [text] (a profile or server
    that is not registered, a server AGENT cannot use); empty when nothing does."""
    found = []
    try:
        values = resolved_values(preset, given)
    except ValueError:
        return found
    for name, spec in preset.get("values", {}).items():
        kind, value = spec.get("kind"), values.get(name)
        if kind not in ("vpn", "llm") or not value:
            continue
        hint = f"; for this preset: {spec['hint']}" if spec.get("hint") else ""
        try:
            if kind == "vpn":
                profile = _module("vpn").load_profile(value)
                wanted = spec.get("vpn_type")
                if wanted and profile.get("type") != wanted:
                    # A per-value constraint: the tor preset's value must be a Tor profile, never any VPN.
                    found.append(f"{name}: the VPN profile {value} is of type {profile.get('type')}, this preset "
                                 f"needs a {wanted} profile{hint}")
            else:
                llm = _module("llm")
                endpoint, _ = llm.selection(SimpleNamespace(llm=value))  # the server, and a model it can name
                problem = llm.agent_problem(endpoint, agent) if agent else None
                if problem:
                    found.append(problem)
        except ValueError as exc:
            found.append(f"{name}: {exc}{hint}")
        except Exception as exc:  # noqa: BLE001 - a damaged store: the start says it in detail
            found.append(f"{name}: {value} cannot be checked ({exc})")
    return found


def registered_mcp():
    """The names of the MCP servers sandbox-ai knows (built-in, added with sandbox-ai mcp add, team servers)."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_mcp_for_presets", HERE / "mcp.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return set(module.definitions())
    except Exception:  # noqa: BLE001 - a damaged mcp.json: check reports every server as missing
        return set()


def refused_in(options):
    """The REFUSED flags among OPTIONS (long forms, as the launcher's canonical_options writes them)."""
    return [token.split("=", 1)[0] for token in options if token.split("=", 1)[0] in REFUSED]


def split_mode(options):
    """(OPTIONS without --mode, the mode it named or None): a preset's security mode is a floor, not a choice."""
    kept, mode, index = [], None, 0
    while index < len(options):
        token = options[index]
        if token == "--mode" and index + 1 < len(options):
            mode, index = options[index + 1], index + 2
            continue
        if token.startswith("--mode="):
            mode, index = token.split("=", 1)[1], index + 1
            continue
        kept.append(token)
        index += 1
    return kept, mode


def apply(argv, canonical=None, with_mode=False):
    """ARGV (the launcher's: AGENT options... [-- agent args]) with --preset NAME and --preset-value KEY=VALUE
    replaced by the preset's options, placed right after the agent so that the command line wins; and its first
    task added as the agent's arguments unless the command line gives some. ARGV unchanged without --preset."""
    if not argv or not any(token == "--preset" or token.startswith("--preset=") for token in argv):
        return (argv, None) if with_mode else argv
    head, tail = list(argv), []
    if "--" in head:
        position = head.index("--")
        head, tail = head[:position], head[position + 1:]
    agent, rest, name, pairs = head[0], [], None, []
    index = 1
    while index < len(head):
        token = head[index]
        for flag in ("--preset", "--preset-value"):
            if token == flag or token.startswith(flag + "="):
                value = token.split("=", 1)[1] if "=" in token else (head[index + 1] if index + 1 < len(head) else None)
                if value is None:
                    raise ValueError(f"{flag} needs a value")
                if flag == "--preset":
                    if name is not None and name != value:
                        raise ValueError("only one --preset per start")
                    name = value
                else:
                    pairs.append(value)
                index += 1 if "=" in token else 2
                break
        else:
            rest.append(token)
            index += 1
    preset = load(name)
    if agent not in preset.get("agents", AGENTS):
        raise ValueError(f"preset {name} is for {', '.join(preset.get('agents', AGENTS))}, not {agent}")
    given = parse_values(pairs)
    options = expand(preset, given, extras=False)
    # Checked as the launcher reads them: short forms (-ca for --copy-auth) and values that turned into flags too.
    if canonical:
        options = canonical(options)
    found = refused_in(options)
    if found:
        raise ValueError(f"preset {name}: {', '.join(dict.fromkeys(found))} is not allowed in a preset")
    # The preset's --mode is taken out: the launcher applies it only where it is stricter than what the command
    # line, the project file and your defaults choose (it never lowers a stricter one).
    options, mode = split_mode(options)
    options += extras_of(preset, resolved_values(preset, given))
    check_vpn_types(preset, resolved_values(preset, given))
    agent_args = tail if tail else prompt_args(preset, agent, resolved_values(preset, given))
    result = [agent, *options, *rest, *(["--", *agent_args] if agent_args or "--" in argv else [])]
    return (result, mode) if with_mode else result


def origin_text(preset):
    return f"from {preset['_dir']}" if preset["_origin"] == "extra" else preset["_origin"]


def describe(preset, out=print):
    out(f"{preset['id']}: {preset['title']}  ({preset.get('category', 'simple')}, {origin_text(preset)})")
    out(f"  {preset['description']}")
    out(f"  agents: {', '.join(preset.get('agents', AGENTS))}")
    if preset.get("options"):
        out(f"  options: {shlex.join(preset['options'])}")
    if preset.get("mcp"):
        out(f"  MCP servers: {', '.join(preset['mcp'])}")
    if preset.get("rules"):
        out(f"  ruleset: {preset['rules']}")
    for name, spec in preset.get("values", {}).items():
        default = f" (default {spec['default']})" if spec.get("default") not in (None, "") else ""
        optional = " (optional)" if spec.get("optional") else ""
        hint = f"; {spec['hint']}" if spec.get("hint") else ""
        out(f"  value {name}: {spec.get('label', name)} [{spec.get('kind', 'text')}]{default}{optional}{hint}")
    for need in preset.get("needs", []):
        out(f"  needs: {need}")
    for note in preset.get("notes", []):
        out(f"  note: {note}")
    if preset.get("prompt"):
        out(f"  first task: {preset['prompt']}")


def public(preset):
    return ({key: value for key, value in preset.items() if not key.startswith("_")} | {"origin": preset["_origin"]}
            | ({"directory": preset["_dir"]} if preset.get("_dir") else {}))


# -- the extra preset directories (defaults.json's preset_dirs) --

def dirs_report():
    """[{path, state, why, presets}] of every listed directory: usable (with how many presets it holds) or why not."""
    listed = listed_dirs()
    try:
        trees, problem = writable_trees(), None
    except Exception as exc:  # noqa: BLE001
        trees, problem = [], f"cannot tell where sandboxes write ({exc})"
    report = []
    for directory in listed:
        why = problem or unsafe_dir(directory, trees)
        if why is None and not directory.is_dir():
            why = "it does not exist (or is not a directory)"
        count = 0
        if why is None:
            try:
                count = sum(1 for p in directory.glob("*.json") if not p.name.endswith(".rules.json"))
            except OSError as exc:
                why = exc.strerror
        report.append({"path": str(directory), "state": "skipped" if why else "used", "why": why, "presets": count})
    return report


def change_dirs(change, out):
    """Apply CHANGE(list of paths) -> new list to defaults.json through sandbox-ai defaults (checked, atomic)."""
    defaults = _module("defaults_cmd")
    document, path = defaults.read()
    current = check_dirs(document.get(DIRS_KEY, []), f"{path}: {DIRS_KEY}")
    wanted = change(list(current))
    if wanted:
        document[DIRS_KEY] = wanted
    else:
        document.pop(DIRS_KEY, None)
    defaults.check(document, path)
    defaults.write(path, document)
    return path


def dirs_add(value, out):
    path = os.path.normpath(os.path.abspath(os.path.expanduser(value)))
    if not os.path.isdir(path):
        raise ValueError(f"{value}: not a directory")
    if os.path.islink(path):
        out(f"note: {path} is a symlink to {os.path.realpath(path)}; both are checked, now and at every use")
    why = unsafe_dir(path, writable_trees())
    if why:
        raise ValueError(f"{path} cannot hold presets: {why} (an agent must not write what it is started with)")

    def add(current):
        if path in current:
            raise ValueError(f"{path} is listed already")
        return current + [path]
    where = change_dirs(add, out)
    count = sum(1 for p in Path(path).glob("*.json") if not p.name.endswith(".rules.json"))
    out(f"preset directory {path} added ({count} preset file(s)) in {where}; your own presets still win over it, "
        "it over the built-in ones")
    return 0


def dirs_remove(value, out):
    path = os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    def remove(current):
        if path not in current:
            raise ValueError(f"{path} is not listed; see: sandbox-ai presets dirs list")
        return [p for p in current if p != path]
    where = change_dirs(remove, out)
    out(f"preset directory {path} removed from {where}")
    return 0


def dirs_main(args, out):
    if args.dirs_command == "add":
        return dirs_add(args.directory, out)
    if args.dirs_command == "remove":
        return dirs_remove(args.directory, out)
    report = dirs_report()
    if args.json:
        out(json.dumps({"key": DIRS_KEY, "file": str(defaults_path()), "builtin": str(BUILTIN), "yours": str(user_dir()),
                        "dirs": report}, indent=2))
        return 0
    out(f"yours (win over all): {user_dir()}")
    for number, entry in enumerate(report, 1):
        state = f"{entry['presets']} preset(s)" if entry["state"] == "used" else f"skipped: {entry['why']}"
        out(f"{number}. {entry['path']}  ({state})")
    if not report:
        out("no extra preset directories; add one with: sandbox-ai presets dirs add DIR")
    out(f"built-in: {BUILTIN}")
    return 0


def main(argv, out=print):
    p = argparse.ArgumentParser(prog="sandbox-ai presets", description=__doc__.split("\n\nStandard library")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    commands = p.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="the built-in presets and your own")
    listing.add_argument("-j", "--json", action="store_true")
    shown = commands.add_parser("show", help="what a preset sets, needs and asks for")
    shown.add_argument("name")
    shown.add_argument("-j", "--json", action="store_true")
    checked = commands.add_parser("check", help="what is missing before a preset can start")
    checked.add_argument("name")
    checked.add_argument("--value", action="append", default=[], metavar="KEY=VALUE")
    checked.add_argument("--agent", choices=AGENTS)
    dirs = commands.add_parser("dirs", help="the extra preset directories (defaults.json: preset_dirs)")
    dirs_commands = dirs.add_subparsers(dest="dirs_command")
    dirs_list = dirs_commands.add_parser("list", help="the directories, in order, and whether each is used")
    for listing_parser in (dirs, dirs_list):
        listing_parser.add_argument("-j", "--json", action="store_true")
    dirs_commands.add_parser("add", help="add a directory (after the others)").add_argument("directory")
    dirs_commands.add_parser("remove", help="remove a directory").add_argument("directory")
    args = p.parse_args(argv)
    try:
        if args.command == "dirs":
            return dirs_main(args, out)
        if args.command == "list":
            damaged = []
            presets = catalog(damaged)
            if args.json:
                out(json.dumps([public(preset) for preset in presets.values()], indent=2))
            else:
                for category in CATEGORIES:
                    group = [preset for preset in presets.values() if preset.get("category", "simple") == category]
                    if group:
                        out(f"{category}:")
                        for preset in sorted(group, key=lambda x: x["id"]):
                            mine = ("  (yours)" if preset["_origin"] == "yours" else
                                    f"  (from {preset['_dir']})" if preset["_origin"] == "extra" else "")
                            out(f"  {preset['id']:<22} {preset['title']}{mine}")
            for problem in damaged:
                print(f"sandbox-ai presets: skipped {problem}", file=sys.stderr)
            return 0
        preset = load(args.name)
        if args.command == "show":
            if args.json:
                out(json.dumps(public(preset), indent=2))
            else:
                describe(preset, out)
            return 0
        found = missing(preset, parse_values(args.value), args.agent)
        if found:
            for line in found:
                out(f"missing: {line}")
            return 1
        out(f"{preset['id']}: ready; start with: sandbox-ai {args.agent or preset.get('agents', AGENTS)[0]} --preset "
            f"{preset['id']}" + "".join(f" --preset-value {shlex.quote(v)}" for v in args.value))
        return 0
    except ValueError as exc:
        print(f"sandbox-ai presets: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
