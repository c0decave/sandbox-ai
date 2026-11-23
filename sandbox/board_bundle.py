#!/usr/bin/env python3
"""sandbox-ai board: a board or a session as one self-sufficient file, and that file taken in on another machine.

  sandbox-ai board export (--session NAME | --board FILE|NAME|-) OUT|- [--images] [--host HOST] [--json]
  sandbox-ai board import FILE|- [--check] [--as NAME] [--map OLD=NEW]... [--value NAME=VALUE]...
                          [--rename KIND:OLD=NEW]... [--replace KIND:NAME]... [--images] [--yes] [--host HOST] [--json]
  sandbox-ai board list [--json]                   the boards imported here (~/.config/sandbox-ai/boards)
  sandbox-ai board show NAME [--check] [--json]    one of them: its start's command line (--check: the dry run)
  sandbox-ai board delete NAME                     forget one of them (what it brought stays)

The bundle (schema "sandbox-ai-bundle", version 1) is one JSON document, or - with --images - a tar archive (the
standard library's, uncompressed) whose first member bundle.json is that document and whose images/N.tar members are
`podman save` archives. It carries what a start needs that exists only on this machine:

  start      the agent, its launcher options and arguments (a session: its saved options with what its trusted
             project file adds; a board: what the app's board starts with)
  board      the app's board itself (parts, edges, positions, labels, bridges, team), when it is one
  folders    every host folder or file the options name, as a placeholder <folder:NAME> with the path as its default
             (asked at the import, never its contents)
  parts      the user's presets it starts from, the MCP servers it names (as `mcp export` writes them: image
             references with their digests), the LLM servers (without a key: the names of its secrets), rulesets
             (the files' contents), kits (their files), image variants (their extension: `images extend`'s recipe,
             not the image), your images (their pinned record), the loop guard of your defaults
  by name    identities, VPN profiles, secrets (sandbox-ai secrets), host variables (--env): only their names - the
             import says which ones that machine lacks and how to keep them there
  withheld   what is never exported, by name: logins (--copy-auth, --auth-file, --git-credentials, a --copy of a
             login file, --local-config, --git-token-file ...) and any value that looks like a credential

Never in a bundle: a secret's value, a token, a login, a password, a sealed VPN file's content, the agent's login
files, cookies, a HOME's contents. The bundle does hold this machine's paths and names (folders, servers, hosts): it
is your file, pass it on knowingly.

An import takes the bundle as passed on, not as yours: what a preset passed on may not carry is refused (a login read
from a file, the internal options of a start, --llm-traffic all, a way past the VPN, hidden characters, a folder that
is no placeholder), an MCP server is checked as `mcp import` checks it (its catalog's signature there), and nothing is
written, loaded or started before the plan is shown and confirmed (--yes when nothing can ask). An image of the
bundle (--images) is loaded only as the image of one of its parts - an MCP server's pinned reference, your image's
record - and never under a name of sandbox-ai's own images (localhost/AGENT-sandbox ...). Names this machine has
already are reused when they mean the same, else refused unless renamed (--rename) or replaced (--replace). The
imported board is kept in ~/.config/sandbox-ai/boards/NAME.json; it is never started by the import.

Standard library only.
"""
import argparse
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
SCHEMA = "sandbox-ai-bundle"
VERSION = 1
BOARD_SCHEMA = "sandbox-ai-board"
BOARD_VERSION = 1
JSON_LIMIT = 32 << 20           # bytes of a bundle's JSON document
BOARD_LIMIT = 4 << 20           # bytes of a board file
IMAGE_LIMIT = 64 << 30          # bytes of one image archive in a bundle
IMAGES_LIMIT = 64               # image archives in one bundle
TREE_LIMIT = 8 << 20            # bytes of the files of one kit or image variant in a bundle
RULES_LIMIT = 1 << 20           # bytes of one ruleset
TEXT_LIMIT = 4000               # characters of one text of a plan, a label, a note
ITEMS_LIMIT = 500               # entries of one list of a bundle
EXPORT_TIMEOUT = 3600
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MARKER = re.compile(r"<(folder|text):([a-z0-9][a-z0-9-]{0,31})>")
RULES_MARKER = re.compile(r"<rules:([a-z0-9][a-z0-9-]{0,31})>")
MEMBER = re.compile(r"images/([1-9][0-9]{0,3})\.tar")
KINDS = ("mcp", "llm", "kit", "variant", "preset", "image", "board")
# What logs in or reads a token from a file of this machine: never exported, named as withheld (logins).
LOGIN_OPTIONS = ("--copy-auth", "--auth-file", "--auth-from-session", "--import-codex-auth", "--git-credentials", "--git-token-file",
                 "--local-config")
# The internal options of a start and what a passed-on preset may not carry (presets.REFUSED, the app's NOT_PASSED_ON):
# never in an imported start.
INTERNAL_OPTIONS = ("--config", "--no-config", "--session", "--dry-run", "--tmux-socket-dir", "--pod-name",
                    "--mcp-socket-dir", "--mcp-approvals-dir", "--helper-of", "--start-id", "--name", "--vpn-lent-by",
                    "--preset-value", "--probe")
# Options whose value is a host folder or file (the launcher's PATH_OPTIONS, without the logins and the rulesets).
FOLDER_OPTIONS = ("--workspace", "--mount", "--copy", "--state-dir", "--log-dir", "--gui-vnc", "--runtime", "--kit-dir",
                  "--privacy-rules", "--mcp-mount", "--workspace-name")
SECRET_WORDS = re.compile(r"(?i)secret|token|passw|pwd|api[_-]?key|private[_-]?key|access[_-]?key|credential|cookie|"
                          r"auth")
LOADING = threading.RLock()
_modules = {}


# --- the modules of sandbox-ai this one uses (each loaded once, one launcher for all) -------------------------------

def module(name):
    """sandbox-ai's NAME.py, loaded once (the launcher shared: sessions, mcp and the others get this one's)."""
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_board", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            if name != "agent_sandbox" and hasattr(loaded, "_launcher_source"):
                loaded._launcher_source = launcher
            _modules[name] = loaded
        return _modules[name]


def launcher():
    return module("agent_sandbox")


def mcp():
    return module("mcp")


def mcp_copy():
    return mcp().copy_module()


def printable(text):
    return launcher().printable(str(text))


def shown(text, limit=300):
    return printable(" ".join(str(text).split()))[:limit]


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai"


def boards_dir():
    return config_dir() / "boards"


# --- options as (name, value) pairs ---------------------------------------------------------------------------------

def pairs(tokens):
    """[[name, value or None, attached]] of launcher option TOKENS (canonical): --name=value (ATTACHED) and --name
    value for the options that take one."""
    actions = launcher().option_actions()
    found, index = [], 0
    tokens = [str(t) for t in tokens]
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
            found.append([name, value, True])
            index += 1
            continue
        action = actions.get(token)
        if token.startswith("-") and action is not None and action.nargs != 0 and index + 1 < len(tokens):
            found.append([token, tokens[index + 1], False])
            index += 2
            continue
        found.append([token, None, False])
        index += 1
    return found


def tokens_of(found):
    out = []
    for name, value, attached in found:
        if value is None:
            out.append(name)
        elif attached:
            out.append(f"{name}={value}")
        else:
            out += [name, value]
    return out


def option_path(name, value):
    """The host path in VALUE of option NAME (a folder option's), or None."""
    if name in ("--mcp-mount",):
        host = value.partition("=")[2]
        if host.count(":") and host.rsplit(":", 1)[1] in ("ro", "rw"):
            host = host.rsplit(":", 1)[0]
        return host or None
    if name == "--workspace-name":
        return value.partition("=")[2] or None
    getter = launcher().PATH_OPTIONS.get(name)
    if getter is None:
        return None
    try:
        path = getter(value)
    except (TypeError, ValueError, IndexError):
        return None
    return str(path) if path else None


def with_path(value, old, new):
    """VALUE with its host path OLD written NEW (the rest of the value as it is)."""
    if value == old:
        return new
    if value.startswith(old):
        return new + value[len(old):]
    if value.endswith(old):
        return value[:len(value) - len(old)] + new
    return value.replace(old, new, 1)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)


def replaced(value, function):
    """VALUE with FUNCTION applied to every string in it (keys stay)."""
    if isinstance(value, str):
        return function(value)
    if isinstance(value, list):
        return [replaced(item, function) for item in value]
    if isinstance(value, dict):
        return {key: replaced(item, function) for key, item in value.items()}
    return value


def credential_in(texts):
    """Why one of TEXTS looks like a credential (the privacy check's built-in shapes), else None."""
    rules = [(regex, reason) for _key, regex, _severity, reason in mcp().privacy_module().BUILTIN]
    for text in texts:
        for regex, reason in rules:
            if re.search(regex, str(text)):
                return reason
    return None


def hidden_characters(texts):
    """Whether a text holds control, direction or zero-width characters (a line break and a tab excepted)."""
    return any(not (c in "\n\t" or c.isprintable()) for text in texts for c in str(text))


# --- what is exported --------------------------------------------------------------------------------------------

class Export:
    """The bundle being made: its folders (path -> marker), withheld values, names and parts."""

    def __init__(self):
        self.folders = {}        # path -> {"marker", "default", "label", "options"}
        self.values = {}         # literal -> {"marker", "label"}
        self.withheld = []       # {"kind", "option", "text"}
        self.secrets = {}        # name -> [what needs it]
        self.env = {}            # name -> [what needs it]
        self.by_name = {"identities": set(), "vpn": set()}
        self.parts = {"presets": [], "mcp": [], "llm": [], "rules": [], "kits": [], "variants": [], "images": [],
                      "settings": {}}
        self.notes = []
        self.markers = set()

    def marker(self, kind, base):
        slug = re.sub(r"[^a-z0-9]+", "-", str(base).lower()).strip("-")[:28] or kind
        if not slug[0].isalnum():
            slug = kind
        name, number = slug, 2
        while f"<{kind}:{name}>" in self.markers:
            name, number = f"{slug[:26]}-{number}", number + 1
        found = f"<{kind}:{name}>"
        self.markers.add(found)
        return found

    def folder(self, path, label, option):
        entry = self.folders.get(path)
        if entry is None:
            entry = self.folders[path] = {"marker": self.marker("folder", posixpath.basename(path.rstrip("/"))
                                                                 or "root"),
                                          "default": path, "label": label, "options": []}
        if option not in entry["options"]:
            entry["options"].append(option)
        return entry["marker"]

    def value(self, literal, label):
        entry = self.values.get(literal)
        if entry is None:
            entry = self.values[literal] = {"marker": self.marker("text", label), "label": label}
        return entry["marker"]

    def secret(self, name, what):
        self.secrets.setdefault(name, [])
        if what not in self.secrets[name]:
            self.secrets[name].append(what)

    def withhold(self, kind, option, text):
        self.withheld.append({"kind": kind, "option": option, "text": text})

    def swap(self, text):
        """TEXT with this machine's folders written as their markers and the withheld values as theirs."""
        for literal, entry in sorted(self.values.items(), key=lambda item: -len(item[0])):
            text = text.replace(literal, entry["marker"])
        if not self.folders:
            return text
        pattern = re.compile(r"(?<![\w.~-])(" + "|".join(re.escape(p) for p in sorted(self.folders, key=len,
                                                                                         reverse=True))
                             + r")(?=$|[:,/=\s])")
        return pattern.sub(lambda match: self.folders[match[1]]["marker"], text)


def read_board_file(text):
    """The board document in TEXT (a board file: the app's board, or one imported here), its shape checked."""
    if len(text) > BOARD_LIMIT:
        raise ValueError(f"the board file is larger than {BOARD_LIMIT >> 20} MiB")
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError(f"not a board file ({BOARD_SCHEMA}): not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != BOARD_SCHEMA or \
            document.get("version") != BOARD_VERSION:
        raise ValueError(f"not a board file (schema {BOARD_SCHEMA}, version {BOARD_VERSION})")
    if not isinstance(document.get("agent"), str) or document["agent"] not in launcher().AUTH_TARGETS:
        raise ValueError("the board file names no agent sandbox-ai has")
    for key in ("options", "agent_args"):
        value = document.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value) or len(value) > 2000:
            raise ValueError(f"the board file's {key} are not a list of texts")
    for key in ("project", "state", "app_preset"):
        if document.get(key) is not None and not isinstance(document[key], dict):
            raise ValueError(f"the board file's {key} is not an object")
    name = document.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError("the board file's name is not a text")
    return document


def board_path(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"a board's name is 1-40 lower-case letters, digits, '_' and '-': {shown(name, 80)}")
    return boards_dir() / f"{name}.json"


def read_private_file(path, limit):
    """The bytes of PATH, a regular file never reached through a symlink (O_NOFOLLOW), at most LIMIT."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ValueError(f"{path}: {'a symlink, not followed' if exc.errno == 40 else exc.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path}: not a regular file")
        data = os.read(fd, limit + 1)
        while len(data) <= limit:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError(f"{path}: larger than {limit >> 10} KiB")
    return data


def kept_board(name):
    """The board imported here as NAME (~/.config/sandbox-ai/boards/NAME.json) - only that: never a file of the
    current folder, which may be a workspace an agent writes (`board show`, a schedule's --board: what starts)."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or not board_path(name).is_file():
        raise ValueError(f"no board {shown(name, 80)} here (sandbox-ai board list)")
    return read_board_file(read_private_file(board_path(name), BOARD_LIMIT).decode("utf-8", "replace"))


def source_of_board(board, stdin=None):
    """The board document BOARD names for an export: '-' (standard input), a board imported here by name (it wins
    over a file of that name in the current folder: ./NAME names the file), else a file."""
    if board == "-":
        text = (stdin or sys.stdin).read(BOARD_LIMIT + 1)
    elif NAME.fullmatch(board) and (board_path(board).is_file() or not os.path.exists(board)):
        path = board_path(board)
        if not path.is_file():
            raise ValueError(f"no board {board} here (sandbox-ai board list) and no such file")
        text = read_private_file(path, BOARD_LIMIT).decode("utf-8", "replace")
    else:
        text = read_private_file(Path(board).expanduser(), BOARD_LIMIT).decode("utf-8", "replace")
    return read_board_file(text)


def source_of_session(name):
    """(the board document of session NAME: its agent, options with its project file's, the leading options of its
    agent arguments, the project file's MCP servers and ruleset), the session's name."""
    sessions = module("sessions")
    key = sessions.resolve(name)
    data = sessions.load(key)
    agent, options, agent_args, problems = sessions.transfer_plan(key)
    # The project's own MCP servers and ruleset go along (as parts of the bundle): not problems here.
    problems = [p for p in problems if "defines its own MCP servers" not in p and "own ruleset" not in p]
    if problems:
        raise ValueError("; ".join(problems))
    _a, document = sessions.effective(data)
    project = {}
    if isinstance(document, dict):
        for key_ in ("mcp_servers", "rules", "description"):
            if document.get(key_):
                project[key_] = document[key_]
    shown_name = sessions.display(key, data)
    return {"schema": BOARD_SCHEMA, "version": BOARD_VERSION, "name": shown_name, "agent": agent,
            "options": list(options), "agent_args": list(agent_args), "project": project}, shown_name


def export_bundle(document, kind, name, out_notes=None, run=subprocess.run):
    """The bundle (a dict, schema SCHEMA) of the board DOCUMENT (read_board_file's shape), from KIND ("board" or
    "session") NAME. ValueError when something in it cannot go along at all."""
    e = Export()
    l = launcher()
    options = l.canonical_options(list(document.get("options", [])))
    if "--" in options:
        options = options[:options.index("--")]
    project = document.get("project") or {}
    project_servers = project.get("mcp_servers") if isinstance(project.get("mcp_servers"), dict) else {}
    kept = []
    for name_, value, attached in pairs(options):
        item = export_option(e, name_, value, attached, project_servers, run)
        if item is not None:
            kept.append(item)
    if isinstance(project.get("rules"), dict):
        checked_ruleset(project["rules"], "the project's ruleset")
        e.parts["rules"].append({"id": "project", "from": "the project file's own ruleset",
                                 "ruleset": project["rules"]})
        kept.append(["--rules", "<rules:project>", False])
    for server, definition in sorted(project_servers.items()):
        if not any(item["name"] == server for item in e.parts["mcp"]):
            add_server(e, server, definition, project=True, run=run)
    # The loop guard of your defaults (the LLM servers' own go with them): shown at the import, never set there.
    if e.parts["llm"]:
        guard = module("llm").user_loop()
        if guard:
            e.parts["settings"]["loop_guard"] = guard
    agent_args = [str(a) for a in document.get("agent_args", [])]
    # Every value left: a credential's shape is withheld (its marker, to give at the import).
    for item in kept:
        if item[1] is not None and credential_in([item[1]]):
            e.value(item[1], f"{item[0].lstrip('-')} value")
    for argument in agent_args:
        if credential_in([argument]):
            e.value(argument, "agent argument")
    options = [e.swap(token) for token in tokens_of(kept)]
    agent_args = [e.swap(argument) for argument in agent_args]
    state = document.get("state")
    if isinstance(state, dict):
        for text in strings(state):
            reason = credential_in([text])
            if reason:
                e.value(text, "board text")
        state = replaced(state, e.swap)
    bundle = {
        "schema": SCHEMA, "version": VERSION,
        "sandbox_ai": getattr(l, "__version__", None),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {"kind": kind, "name": str(name)[:80]},
        "name": suggested_name(document.get("name") or name),
        "start": {"agent": document["agent"], "options": options, "agent_args": agent_args},
        "project": {key: value for key, value in project.items() if key == "description" and isinstance(value, str)},
        "board": state,
        "folders": [{"marker": entry["marker"], "default": path, "label": entry["label"], "options": entry["options"]}
                    for path, entry in e.folders.items()],
        "values": [{"marker": entry["marker"], "label": entry["label"]} for entry in e.values.values()],
        "secrets": [{"name": secret, "for": what} for secret, what in sorted(e.secrets.items())],
        "env": [{"name": variable, "for": what} for variable, what in sorted(e.env.items())],
        "by_name": {key: sorted(values) for key, values in e.by_name.items()},
        "withheld": e.withheld,
        "parts": e.parts,
        "notes": e.notes,
        "images": [],
    }
    if isinstance(document.get("app_preset"), dict):
        bundle["app_preset"] = replaced(document["app_preset"], e.swap)
    reason = credential_in(strings(bundle))
    if reason:  # (never: every value of that shape was withheld above; a part's own text is refused here)
        raise ValueError(f"the bundle would hold what looks like a credential ({reason}): nothing was exported; keep "
                         "secrets in sandbox-ai secrets and name them")
    if out_notes is not None:
        out_notes.extend(e.notes)
    return bundle


def suggested_name(text):
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(text or "board").lower()).strip("-_")[:40]
    return slug if slug and NAME.fullmatch(slug) else "board"


def checked_ruleset(document, where):
    try:
        return launcher().RUNTIME.Ruleset.validated(document, where)
    except Exception as exc:  # noqa: BLE001 - RuleError or ValueError: the ruleset's own words
        raise ValueError(f"{where}: {exc}") from None


def export_option(e, name, value, attached, project_servers, run):
    """One option of the start as it goes into the bundle ([name, value, attached]), or None (withheld)."""
    l = launcher()
    if name in LOGIN_OPTIONS:
        e.withhold("login", name, f"{name}" + (" (a file of this machine)" if value else "")
                   + ": logins and tokens of this machine never go along - give the start its own login there")
        return None
    if name == "--copy" and value is not None and l.copies_credential(value):
        e.withhold("login", name, f"--copy of a login file ({shown(value.rsplit(':', 1)[0], 200)}): never goes along")
        return None
    if name in ("--preset-value",):
        e.withhold("value", name, f"{name} {shown(value, 200)}: the preset's values are in the start's options")
        return None
    if name in INTERNAL_OPTIONS:
        return None
    if value is None:
        return [name, value, attached]
    if name == "--rules" and str(value).startswith("builtin:"):
        return [name, value, attached]  # sandbox-ai's own ruleset: every installation has it
    if name == "--rules":
        path = Path(value).expanduser()
        l.read_ruleset(path, str(path))  # (checked as a start checks it; its own words go along, as written)
        ruleset = parse_json(read_private_file(path, RULES_LIMIT))
        ident = f"rules-{len(e.parts['rules']) + 1}"
        e.parts["rules"].append({"id": ident, "from": str(value), "ruleset": ruleset})
        return [name, f"<rules:{ident}>", attached]
    if name in FOLDER_OPTIONS:
        if name == "--workspace":
            parts = [part for part in value.split(",") if part]
            labels = ["project folder" if index == 0 else "another project folder" for index in range(len(parts))]
            return [name, ",".join(e.folder(part, label, name) for part, label in zip(parts, labels)), attached]
        path = option_path(name, value)
        if path:
            label = {"--mount": "extra folder", "--copy": "file copied into HOME", "--state-dir": "the sandbox HOME's "
                     "folder", "--log-dir": "the logs' folder", "--kit-dir": "a folder of kits",
                     "--mcp-mount": "a folder of an MCP server", "--privacy-rules": "the privacy check's rules folder",
                     "--gui-vnc": "the VNC socket's folder", "--runtime": "a runtime folder",
                     "--workspace-name": "a named project folder"}.get(name, "a folder")
            return [name, with_path(value, path, e.folder(path, label, name)), attached]
        return [name, value, attached]
    if name == "--secret-env":
        variable, _, secret = value.partition("=")
        e.secret(secret or variable, f"--secret-env {variable}")
    elif name == "--api-key":
        parts = value.split("=")
        if len(parts) >= 2:
            e.secret(parts[2] if len(parts) > 2 else parts[1], f"--api-key {parts[0]}")
    elif name == "--git-token-secret":
        e.secret(value.rpartition("=")[2], f"--git-token-secret {value.rpartition('=')[0]}")
    elif name == "--git-token-env":
        e.env.setdefault(value.rpartition("=")[2], []).append(f"--git-token-env {value.rpartition('=')[0]}")
    elif name == "--env":
        e.env.setdefault(value.partition("=")[0], []).append("--env")
    elif name == "--identity":
        e.by_name["identities"].add(value)
    elif name == "--vpn":
        e.by_name["vpn"].add(value)
    elif name == "--mcp-env":
        server, _, spec = value.partition(":")
        variable, at, secret = spec.partition("@")
        if at:
            e.secret(secret, f"--mcp-env {server}:{variable}")
        else:
            variable, _, given = spec.partition("=")
            if SECRET_WORDS.search(variable) or credential_in([given]):
                return [name, f"{server}:{variable}={e.value(given, f'{server} {variable}')}", attached] if given \
                    else [name, value, attached]
    elif name == "--service":
        return [name, export_service(e, value), attached]
    elif name == "--mcp":
        if value in project_servers:
            add_server(e, value, project_servers[value], project=True, run=run)
        else:
            add_server(e, value, None, run=run)
    elif name == "--llm":
        add_llm(e, value.partition(":")[0])
    elif name == "--kit":
        add_kit(e, value)
    elif name == "--image-variant":
        add_variant(e, value)
    elif name == "--preset":
        add_preset(e, value)
    return [name, value, attached]


def export_service(e, value):
    """A --service value: its image (yours) recorded, a variable whose name or value says secret withheld, a secret
    named."""
    name, colon, rest = value.partition(":")
    out, image = [], name
    for part in rest.split(",") if colon else []:
        key, equals, given = part.partition("=")
        if key == "image" and equals:
            image = given
        elif key == "secret" and equals:
            e.secret(given.partition("=")[2] or given.partition("=")[0], f"--service {name}")
        elif key == "env" and equals:
            variable, _, literal = given.partition("=")
            if literal and (SECRET_WORDS.search(variable) or credential_in([literal])):
                part = f"env={variable}={e.value(literal, f'{name} {variable}')}"
        out.append(part)
    add_user_image(e, image)
    return name + (":" + ",".join(out) if colon else "")


def add_server(e, name, definition, project=False, run=subprocess.run):
    """The MCP server NAME (yours, or the project's DEFINITION) as `mcp export` writes it; a built-in one or a team's
    is on every machine (or the team's own): a note."""
    if any(item["name"] == name for item in e.parts["mcp"]):
        return
    mc, m = mcp_copy(), mcp()
    if definition is None:
        if name in m.BUILTIN:
            return
        if name.startswith("team-"):
            e.notes.append(f"MCP server {name} is a team's board: it is the team's, not exported")
            return
        try:
            document = mc.export(name, run)
        except ValueError as exc:  # (not defined here any more, a credential in it: named, never carried)
            e.withhold("mcp", "--mcp", f"MCP server {name}: {exc}")
            return
    else:
        document = server_document(name, definition)
    if project:
        document["project"] = True
    for path in document.get("paths", []):
        if isinstance(path.get("path"), str):
            e.folder(path["path"], f"a folder of the MCP server {name}", "mcp")
    for secret in document.get("secrets", []):
        if secret.get("from") == "secret" or secret.get("required"):
            e.secret(secret["secret"], f"MCP server {name}")
    vpn = (document.get("network") or {}).get("vpn")
    if vpn:
        e.by_name["vpn"].add(vpn)
    e.parts["mcp"].append(document)


def server_document(name, definition):
    """The `mcp export` document of a server the project file defines (not in mcp.json): as export() makes one."""
    mc, m = mcp_copy(), mcp()
    if not re.fullmatch(launcher().MCP_NAME, name):
        raise ValueError(f"the project's MCP server {shown(name, 40)} has no valid name")
    definition = m.validated({name: definition}, "the project's mcp_servers")[name]
    if "image" in definition or "rootfs" in definition:
        if "rootfs" in definition and not isinstance(definition.get("source"), str):
            raise ValueError(f"the project's MCP server {name}: a Bubblewrap rootfs of this machine's cannot go along")
        kind = "catalog" if isinstance(definition.get("source"), str) else "image"
        secrets = mc.image_secrets(definition)
        paths = [{"kind": "mount", "target": mount["target"], "path": mount["host"], "mode": mount.get("mode", "ro")}
                 for mount in definition.get("mounts", [])]
    else:
        kind = "command"
        reason = mc.credential_in([*definition["command"], *definition.get("options", []),
                                   definition.get("workspace", "")])
        if reason:
            raise ValueError(f"the project's MCP server {name}: its definition holds what looks like a credential "
                             f"({reason})")
        secrets, paths = mc.command_secrets(definition), mc.command_paths(definition)
    document = {"schema": mc.SCHEMA, "version": mc.VERSION, "name": name, "kind": kind,
                "sandbox_ai": getattr(launcher(), "__version__", None), "definition": definition,
                "secrets": secrets, "paths": paths, "network": mc.network_of(definition)}
    if kind == "catalog":
        document["catalog"] = {"source": definition["source"], "catalog": definition.get("catalog"),
                               "signed": None if "signed_by" not in definition else bool(definition["signed_by"]),
                               "image": definition.get("image"),
                               "rootfs": (definition.get("rootfs") or {}).get("sha256")}
    if "image" in definition:
        document["image"] = {"ref": definition["image"], "present": None, "size": None}
    return document


def add_llm(e, name):
    """The LLM server NAME as stored (its secrets by name; its CA's certificate, which is no secret)."""
    if any(item["name"] == name for item in e.parts["llm"]):
        return
    llm = module("llm")
    try:
        endpoint = llm.endpoint(name)
    except ValueError as exc:
        e.withhold("llm", "--llm", f"LLM server {name}: {exc}")
        return
    record = llm.record_of(endpoint)
    for key in ("checked",):
        record.pop(key, None)
    entry = {"name": name, "record": record}
    if endpoint.get("ca"):
        entry["ca_text"] = llm.ca_text(endpoint)
    if endpoint.get("api_key_secret"):
        e.secret(endpoint["api_key_secret"], f"LLM server {name} (its key)")
    for header, secret in (endpoint.get("headers") or {}).items():
        e.secret(secret, f"LLM server {name} (header {header})")
    if endpoint.get("vpn"):
        e.by_name["vpn"].add(endpoint["vpn"])
    e.parts["llm"].append(entry)


def read_folder(root, label):
    """{relative path: {"data": base64, "mode"}} of the folder ROOT (nothing followed, kits' limits), and notes."""
    hostkits, kits = module("hostkits"), module("kits")
    fd = kits.open_dir(root.parent, (root.name,))
    if fd is None:
        raise ValueError(f"{label}: {root} is not there")
    try:
        files, notes = hostkits.read_tree(fd, label)
    finally:
        os.close(fd)
    return files, notes


def add_tree(e, kind, name, root, label):
    """The files of ROOT (a kit, a variant): ([files], notes); a file with a secret-looking line is withheld."""
    files, notes = read_folder(root, label)
    flagged = {item["file"] for item in module("hostkits").scan(files)}
    total, kept = 0, []
    for path, data, mode in files:
        if path in flagged:
            e.withhold("file", kind, f"{label}: {path} holds a secret-looking line - left out")
            continue
        total += len(data)
        if total > TREE_LIMIT:
            raise ValueError(f"{label} is larger than {TREE_LIMIT >> 20} MiB: it cannot go into a bundle")
        kept.append({"path": path, "mode": 0o755 if mode & 0o111 else 0o644,
                     "data": base64.b64encode(data).decode()})
    e.notes += [f"{label}: {note}" for note in notes]
    return kept


def add_kit(e, name):
    if any(item["name"] == name for item in e.parts["kits"]):
        return
    kits = module("kits")
    root = kits.kits_dir() / name
    if not root.is_dir() or root.is_symlink():
        if not (kits.BUILTIN_KITS / name).is_dir():
            e.notes.append(f"kit {name}: not a kit of yours here")
        return  # (a built-in kit is in every sandbox-ai)
    e.parts["kits"].append({"name": name, "files": add_tree(e, "kit", name, root, f"kit {name}")})


def add_variant(e, name):
    """The image variant NAME of every agent that has one (its extension: image.json, fragment, files)."""
    images = module("images")
    found = False
    for agent in images.AGENTS:
        directory = images.variant_dir(name, agent)
        if not (directory / images.DOCUMENT).is_file():
            continue
        found = True
        if any(item["name"] == name and item["agent"] == agent for item in e.parts["variants"]):
            continue
        e.parts["variants"].append({"name": name, "agent": agent,
                                    "files": add_tree(e, "variant", name, directory, f"image variant {name}@{agent}")})
    if not found:
        e.notes.append(f"image variant {name}: none here")


def add_user_image(e, name):
    if any(item["name"] == name for item in e.parts["images"]):
        return
    services = module("services")
    try:
        record = services.record(name)
    except ValueError:
        e.notes.append(f"image {name}: not one of your images here")
        return
    e.parts["images"].append({"name": name, "record": record})


def add_preset(e, name):
    """The preset NAME when it is one of yours (with its ruleset beside it); sandbox-ai's own are on every machine."""
    if any(item["document"].get("id") == name for item in e.parts["presets"]):
        return
    presets = module("presets")
    found = presets.catalog().get(name)
    if found is None or found.get("_origin") != "yours":
        return
    document = {key: value for key, value in found.items() if not key.startswith("_")}
    entry = {"document": document}
    if document.get("rules"):
        path = Path(found["_path"]).parent / document["rules"]
        entry["rules"] = parse_json(read_private_file(path, RULES_LIMIT))
        checked_ruleset(entry["rules"], f"preset {name}'s ruleset")
    e.parts["presets"].append(entry)


# --- the images (--images) --------------------------------------------------------------------------------------

def bundle_images(bundle):
    """[(ref, what)]: the images a bundle with --images carries: the MCP servers' (pinned by digest) and your images.
    The agents' own images never: they are sandbox-ai's, built on each machine from its signed Containerfiles."""
    found = []
    for document in bundle["parts"]["mcp"]:
        image = document.get("image") if isinstance(document.get("image"), dict) else None
        if image and isinstance(image.get("ref"), str):
            found.append((image["ref"], f"mcp:{document['name']}"))
    for entry in bundle["parts"]["images"]:
        record = entry["record"]
        found.append((record["image"], f"image:{entry['name']}"))
    return list(dict.fromkeys(found))


def save_images(bundle, out=print, run=subprocess.run):
    """[(member, path, entry)] of the images of BUNDLE saved by Podman into temporary files (the caller removes
    them); the bundle's "images" list filled in."""
    saved = []
    folder = tempfile.mkdtemp(prefix="sandbox-ai-bundle-", dir=mcp_copy().spool_folder())
    for index, (ref, what) in enumerate(bundle_images(bundle)[:IMAGES_LIMIT], 1):
        member, path = f"images/{index}.tar", os.path.join(folder, f"{index}.tar")
        out(f"sandbox-ai board: saving the image {ref} ({what})")
        with open(path, "wb") as stream:
            done = run(["podman", "save", "--format", "oci-archive", ref], stdout=stream, stdin=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, check=False)
        if done.returncode != 0:
            shutil.rmtree(folder, ignore_errors=True)
            said = done.stderr.decode(errors="replace") if isinstance(done.stderr, bytes) else str(done.stderr or "")
            raise ValueError(f"the image {ref} could not be saved: {shown(said.strip()[-400:], 400)}")
        digest, size = hashlib.sha256(), 0
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        entry = {"member": member, "ref": ref, "for": what, "size": size, "sha256": digest.hexdigest()}
        bundle["images"].append(entry)
        saved.append((member, path, entry))
    return saved, folder


def write_bundle(bundle, out_path, saved=(), stdout=None):
    """BUNDLE written to OUT_PATH ('-': standard output): JSON, or a tar of bundle.json and the SAVED images."""
    text = json.dumps(bundle, indent=1, ensure_ascii=False) + "\n"
    data = text.encode()
    if len(data) > JSON_LIMIT:
        raise ValueError(f"the bundle is larger than {JSON_LIMIT >> 20} MiB (its kits or variants): not written")
    if out_path == "-":
        target = stdout or sys.stdout.buffer
        writer = contextlib.nullcontext(target)
    else:
        path = Path(out_path).expanduser()
        if path.is_symlink() or path.is_dir():
            raise ValueError(f"{out_path}: a symlink or a folder - give a new file's path")
        temporary = path.with_name(f".{path.name}.{os.getpid()}-{os.urandom(4).hex()}")
        writer = open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600),
                      "wb")
    try:
        with writer as stream:
            if not saved:
                stream.write(data)
            else:
                with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                    info = tarfile.TarInfo("bundle.json")
                    info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
                    tar.addfile(info, io.BytesIO(data))
                    for member, path_, entry in saved:
                        info = tarfile.TarInfo(member)
                        info.size, info.mode, info.mtime = entry["size"], 0o600, int(time.time())
                        with open(path_, "rb") as image:
                            tar.addfile(info, image)
            stream.flush()
        if out_path != "-":
            os.replace(temporary, path)
    except BaseException:
        if out_path != "-":
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        raise


# --- reading a bundle (another machine's: data, never trusted) --------------------------------------------------

class Received:
    """A bundle read from a file: its document, and (with images) the archive members spooled to files."""

    def __init__(self, document, images=None, folder=None):
        self.document, self.images, self.folder = document, images or {}, folder

    def close(self):
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)


def read_bundle_bytes(stream):
    """The bundle in STREAM (binary): a JSON document, or a tar archive (bundle.json first, then images/N.tar). Archive
    members are data: only those two kinds of names, regular files, no link, no path of their own, within limits;
    each image checked against the digest its entry gives."""
    head = stream.read(1)
    while head in (b" ", b"\n", b"\r", b"\t"):
        head = stream.read(1)
    if head == b"{":
        data = head + stream.read(JSON_LIMIT + 1)
        if len(data) > JSON_LIMIT:
            raise ValueError(f"the bundle is larger than {JSON_LIMIT >> 20} MiB")
        return Received(checked_bundle(parse_json(data)))
    folder = tempfile.mkdtemp(prefix="sandbox-ai-bundle-in-", dir=mcp_copy().spool_folder())
    images, document = {}, None
    try:
        rest = head + stream.read(1023)  # (the first block: the tar reads on from the stream)
        joined = io.BufferedReader(_Joined(rest, stream))
        try:
            tar = tarfile.open(fileobj=joined, mode="r|")
        except tarfile.TarError:
            raise ValueError("not a bundle (sandbox-ai board export): neither JSON nor a tar archive") from None
        with tar:
            for member in tar:
                name = member.name
                # (a sparse member counts as a file to tarfile and reads as its full size: a few KiB that become
                # GiB of zeros in the spool folder - only plain files)
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.sparse is not None or \
                        not member.isfile() or member.islnk() or member.issym():
                    raise ValueError(f"the bundle's archive holds {shown(name, 100)}, which is no regular file: "
                                     "refused")
                if document is None:
                    if name != "bundle.json":
                        raise ValueError("the bundle's archive does not start with bundle.json: refused")
                    if member.size > JSON_LIMIT:
                        raise ValueError(f"the bundle's bundle.json is larger than {JSON_LIMIT >> 20} MiB")
                    document = checked_bundle(parse_json(tar.extractfile(member).read(JSON_LIMIT + 1)))
                    continue
                match = MEMBER.fullmatch(name)
                if match is None or name in images:
                    raise ValueError(f"the bundle's archive holds {shown(name, 100)}: only bundle.json and "
                                     "images/N.tar belong in one - refused (nothing was read from it)")
                entry = next((item for item in document["images"] if item["member"] == name), None)
                if entry is None:
                    raise ValueError(f"the bundle's archive holds {name}, which its bundle.json does not name: refused")
                if member.size > IMAGE_LIMIT or member.size != entry["size"]:
                    raise ValueError(f"the bundle's {name} is not the size its bundle.json says: refused")
                target = os.path.join(folder, f"{match[1]}.tar")
                digest = hashlib.sha256()
                source = tar.extractfile(member)
                with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600),
                          "wb") as spooled:
                    for chunk in iter(lambda: source.read(1 << 20), b""):
                        digest.update(chunk)
                        spooled.write(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    raise ValueError(f"the bundle's {name} does not match its digest: refused")
                images[name] = target
        if document is None:
            raise ValueError("the bundle's archive holds no bundle.json")
        missing = [item["member"] for item in document["images"] if item["member"] not in images]
        if missing:
            raise ValueError(f"the bundle's archive lacks {', '.join(missing[:5])}")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return Received(document, images, folder)


class _Joined(io.RawIOBase):
    """FIRST, then the rest of STREAM: a stream whose first bytes were read already."""

    def __init__(self, first, stream):
        self.first, self.stream = first, stream

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.first:
            count = min(len(buffer), len(self.first))
            buffer[:count] = self.first[:count]
            self.first = self.first[count:]
            return count
        data = self.stream.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def parse_json(data):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("not a bundle (sandbox-ai board export): its JSON cannot be read") from None


def _texts(value, what, limit=ITEMS_LIMIT):
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(v, str) for v in value):
        raise ValueError(f"the bundle's {what} is not a list of texts")
    return value


def _objects(value, what, limit=ITEMS_LIMIT):
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"the bundle's {what} is not a list of objects")
    return value


def checked_bundle(document):
    """DOCUMENT's shape checked (what the import reads of it); ValueError otherwise."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("version") != VERSION:
        raise ValueError(f"not a bundle of this sandbox-ai (schema {SCHEMA}, version {VERSION})")
    start = document.get("start")
    if not isinstance(start, dict) or not isinstance(start.get("agent"), str) or \
            start["agent"] not in launcher().AUTH_TARGETS:
        raise ValueError("the bundle names no agent sandbox-ai has")
    _texts(start.get("options"), "options", 4000)
    _texts(start.get("agent_args", []), "agent arguments", 400)
    if not isinstance(document.get("name"), str) or not NAME.fullmatch(document["name"]):
        raise ValueError("the bundle has no valid name")
    for key in ("folders", "values", "secrets", "env", "withheld", "images"):
        _objects(document.get(key, []), key)
    for entry in document.get("folders", []):
        if not (isinstance(entry.get("marker"), str) and MARKER.fullmatch(entry["marker"])
                and entry["marker"].startswith("<folder:") and isinstance(entry.get("default"), str)
                and isinstance(entry.get("label", ""), str)):
            raise ValueError("a folder of the bundle has no marker or default")
    for entry in document.get("values", []):
        if not (isinstance(entry.get("marker"), str) and MARKER.fullmatch(entry["marker"])
                and isinstance(entry.get("label", ""), str)):
            raise ValueError("a value of the bundle has no marker")
    for entry in document.get("secrets", []) + document.get("env", []):
        if not (isinstance(entry.get("name"), str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", entry["name"])):
            raise ValueError("the bundle names a secret or variable that is no name")
    for entry in document.get("images", []):
        if not (isinstance(entry.get("member"), str) and MEMBER.fullmatch(entry["member"])
                and isinstance(entry.get("ref"), str) and isinstance(entry.get("for"), str)
                and type(entry.get("size")) is int and 0 <= entry["size"] <= IMAGE_LIMIT
                and isinstance(entry.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("an image of the bundle is not described as an export describes one")
    by_name = document.get("by_name", {})
    if not isinstance(by_name, dict):
        raise ValueError("the bundle's by_name is not an object")
    for key in ("identities", "vpn"):
        _texts(by_name.get(key, []), key)
    parts = document.get("parts")
    if not isinstance(parts, dict):
        raise ValueError("the bundle has no parts")
    for key in ("presets", "mcp", "llm", "rules", "kits", "variants", "images"):
        _objects(parts.get(key, []), key)
    if not isinstance(parts.get("settings", {}), dict):
        raise ValueError("the bundle's settings are not an object")
    for key in ("board", "app_preset", "project"):
        if document.get(key) is not None and not isinstance(document[key], dict):
            raise ValueError(f"the bundle's {key} is not an object")
    for key in ("notes",):
        _texts(document.get(key, []), key)
    return document


def read_bundle(path, stdin=None):
    """The bundle at PATH ('-': standard input)."""
    if path == "-":
        return read_bundle_bytes(stdin or sys.stdin.buffer)
    target = Path(path).expanduser()
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ValueError(f"{path}: {'a symlink, not followed' if exc.errno == 40 else exc.strerror}") from None
    with open(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path}: not a regular file")
        return read_bundle_bytes(stream)


# --- the import's plan --------------------------------------------------------------------------------------------

def problem(plan, kind, text, fix=""):
    plan["problems"].append({"kind": kind, "text": text, "fix": fix})


def parse_pairs(values, what):
    found = {}
    for value in values or ():
        old, sep, new = str(value).partition("=")
        if not sep or not old:
            raise ValueError(f"{what} takes OLD=NEW: {shown(value, 200)}")
        found[old] = new
    return found


def parse_renames(values):
    """{(kind, old): new} of --rename KIND:OLD=NEW."""
    found = {}
    for value in values or ():
        kind, colon, rest = str(value).partition(":")
        old, sep, new = rest.partition("=")
        if kind not in KINDS or not colon or not sep or not old or not new:
            raise ValueError(f"--rename KIND:OLD=NEW, KIND one of {', '.join(KINDS)}: {shown(value, 200)}")
        found[(kind, old)] = new
    return found


def parse_replaces(values):
    found = set()
    for value in values or ():
        kind, colon, name = str(value).partition(":")
        if kind not in KINDS or not colon or not name:
            raise ValueError(f"--replace KIND:NAME, KIND one of {', '.join(KINDS)}: {shown(value, 200)}")
        found.add((kind, name))
    return found


class Choices:
    """What the human chose for the import: folders (--map), values (--value), names (--as, --rename, --replace)."""

    def __init__(self, as_name=None, maps=(), values=(), renames=(), replaces=(), images=False, pull=False,
                 unsigned=False):
        self.as_name, self.maps, self.pull, self.unsigned = as_name, parse_pairs(maps, "--map"), pull, unsigned
        self.values, self.renames = parse_pairs(values, "--value"), parse_renames(renames)
        self.replaces, self.images = parse_replaces(replaces), images

    def name(self, kind, old):
        return self.renames.get((kind, old), old)

    def replacing(self, kind, name):
        return (kind, name) in self.replaces


def plan_import(received, choices, secrets=None, here="this machine"):
    """What `board import` would do with the bundle RECEIVED (nothing written): a dict for --json and the app."""
    bundle = received.document
    plan = {"bundle": {"name": bundle["name"], "source": bundle.get("source"), "created": bundle.get("created"),
                       "sandbox_ai": bundle.get("sandbox_ai")},
            "board": choices.as_name or bundle["name"], "replace_board": False, "here": here,
            "start": None, "command": None, "folders": [], "values": [], "create": [], "reuse": [], "replace": [],
            "missing": {"secrets": [], "env": [], "identities": [], "vpn": [], "images": [], "mcp": [], "llm": []},
            "withheld": [shown(item.get("text", ""), 400) for item in bundle.get("withheld", [])],
            "mcp": [], "warnings": [], "notes": [shown(n, 400) for n in bundle.get("notes", [])], "problems": [],
            "ok": False, "settings": bundle["parts"].get("settings") or {}}
    if hidden_characters(strings(bundle)):
        problem(plan, "bundle", "the bundle's texts hold control, direction or zero-width characters: it would show "
                                "as something else than it is - refused")
        return plan
    if not NAME.fullmatch(plan["board"]):
        problem(plan, "name", f"{shown(plan['board'], 60)} is not a board's name", "--as NAME (lower-case letters, "
                                                                                    "digits, _ and -)")
    elif board_path(plan["board"]).exists():
        if choices.replacing("board", plan["board"]):
            plan["replace_board"] = True
            plan["replace"].append({"kind": "board", "name": plan["board"]})
        else:
            problem(plan, "name", f"{here} has a board {plan['board']} already", f"--as NEWNAME, or --replace "
                                                                                f"board:{plan['board']}")
    # Folders: each one the human's choice (--map), else its default - a private one only when chosen explicitly.
    l = launcher()
    folder_map = {}
    for entry in bundle.get("folders", []):
        chosen = choices.maps.get(entry["marker"], choices.maps.get(entry["default"]))
        to = chosen if chosen else entry["default"]
        item = {"marker": entry["marker"], "default": entry["default"], "label": shown(entry.get("label", ""), 200),
                "options": [shown(o, 40) for o in entry.get("options", []) if isinstance(o, str)][:20], "to": to,
                "chosen": bool(chosen), "warnings": []}
        if not isinstance(to, str) or not os.path.isabs(os.path.expanduser(to)) or any(c in to for c in "\n\r\0,"):
            problem(plan, "folder", f"{entry['marker']} ({item['label']}): {shown(to, 200)} is no absolute path",
                    f"--map {entry['marker']}=FOLDER")
        else:
            why = l.private_source("--mount", to)
            if not why and to.endswith((":ro", ":rw")):  # (as --mcp-mount reads it: the folder, then its mode)
                why = l.private_source("--mount", to[:-3])
            if why and not chosen:
                problem(plan, "folder", f"{entry['marker']} ({item['label']}): the bundle's default {shown(to, 200)} "
                                        f"is a private location ({shown(why, 200)})",
                        f"--map {entry['marker']}=FOLDER (a folder you choose; {entry['default']}={entry['default']} "
                        "keeps it, with this warning)")
            elif why:
                item["warnings"].append(f"{shown(to, 200)}: {shown(why, 300)} (your choice)")
                plan["warnings"].append(f"folder {shown(to, 200)}: {shown(why, 300)} (your choice)")
            if not os.path.exists(os.path.expanduser(to)):
                item["warnings"].append("not there yet: the start creates or refuses it as for any folder")
        folder_map[entry["marker"]] = to
        plan["folders"].append(item)
    value_map = {}
    for entry in bundle.get("values", []):
        given = choices.values.get(entry["marker"], choices.values.get(MARKER.fullmatch(entry["marker"])[2]))
        plan["values"].append({"marker": entry["marker"], "label": shown(entry.get("label", ""), 200),
                               "given": given is not None})
        if given is None:
            problem(plan, "value", f"{entry['marker']} ({shown(entry.get('label', ''), 200)}) was withheld from the "
                                   "bundle (it looked like a secret)",
                    f"--value {MARKER.fullmatch(entry['marker'])[2]}=VALUE")
        elif any(c in given for c in "\n\r\0"):
            problem(plan, "value", f"{entry['marker']}: a value of one line")
        else:
            value_map[entry["marker"]] = given
    names = {"mcp": {}, "llm": {}, "kit": {}, "variant": {}, "preset": {}, "image": {}}
    plan_parts(plan, bundle, received, choices, names, secrets, here)
    rules_paths = {}
    target = boards_dir() / plan["board"]
    for entry in bundle["parts"].get("rules", []):
        ident = entry.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", ident):
            problem(plan, "rules", "a ruleset of the bundle has no valid id")
            continue
        try:
            checked_ruleset(entry.get("ruleset"), f"the bundle's ruleset {ident}")
        except ValueError as exc:
            problem(plan, "rules", str(exc))
            continue
        rules_paths[ident] = str(target / f"{ident}.rules.json")
        plan["create"].append({"kind": "rules", "name": ident, "what": rules_paths[ident]})
    start = resolved_start(plan, bundle, folder_map, value_map, rules_paths, names, choices)
    if start is not None:
        plan["warnings"] += handed_secrets(start["options"], bundle, secrets, names)
        plan["start"] = start
        plan["command"] = shlex.join(["sandbox-ai", start["agent"], "--no-config", *start["options"],
                                      *(["--", *start["agent_args"]] if start["agent_args"] else [])])
    plan["ok"] = not plan["problems"]
    plan["_maps"] = (folder_map, value_map, rules_paths, names)
    return plan


class _AllNames:
    def __contains__(self, name):
        return True


ALL_NAMES = _AllNames()


def handed_secrets(options, bundle, secrets=None, names=None):
    """A warning for each secret of this machine's (SECRETS: their names; None: read here) that the bundle's start
    OPTIONS (as they run here) or an LLM server of the BUNDLE hands on, and where to: a bundle is someone else's, and
    the secrets it names that are here go wherever it says - an API key to a host it names, a Git login for a server
    it names, a variable the agent reads. (What this machine lacks is listed as missing instead.)"""
    if secrets is None:
        try:
            secrets = mcp_copy().secret_names()
        except (OSError, ValueError, SystemExit):
            secrets = None
    if secrets is None:  # (they cannot be read: each one named is said, as it may be here)
        secrets = ALL_NAMES
    found = []

    def say(secret, what):
        if secret in secrets:
            text = f"your secret {secret} {what} (the bundle's start says so: whoever made it chose where)"
            if text not in found:
                found.append(text)
    renamed = (names or {}).get("llm", {})  # (the start names a renamed server by its new name)
    servers = {renamed.get(entry.get("name"), entry.get("name")): entry.get("record")
               for entry in bundle["parts"].get("llm", [])
               if isinstance(entry, dict) and isinstance(entry.get("record"), dict)}
    with contextlib.suppress(ValueError, SystemExit):
        canonical = launcher().canonical_options(list(options))
        # (where each option sends its secret: the same words as a preset of an extra directory's, presets.py)
        presets = module("presets")
        for entry in presets.secret_destinations(canonical):
            say(entry["secret"], presets.destination_text(entry, shown))
        for name, value, _ in pairs(canonical):
            value = str(value or "")
            if name == "--llm":
                record = servers.get(value.partition(":")[0])
                if record:
                    where = shown(record.get("url", "?"), 200)
                    if isinstance(record.get("api_key_secret"), str):
                        say(record["api_key_secret"], f"goes to {where} as the key of the bundle's LLM server "
                                                      f"{shown(value.partition(':')[0], 60)} (--llm)")
                    for header, secret in (record.get("headers") or {}).items():
                        if isinstance(secret, str):
                            say(secret, f"goes to {where} in the header {shown(header, 60)} of the bundle's LLM "
                                        f"server {shown(value.partition(':')[0], 60)} (--llm)")
    return found


def same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def plan_parts(plan, bundle, received, choices, names, secrets, here):
    """Each part of the bundle: reused (the same is here), created, replaced (asked for) or refused."""
    mc = mcp_copy()
    known = mc.secret_names() if secrets is None else set(secrets)
    # Presets of the user's: checked as a passed-on preset (an extra directory's), written as yours.
    presets = module("presets")
    mine = {}
    with contextlib.suppress(OSError, ValueError):
        mine = {key: value for key, value in presets.catalog().items()}
    for entry in bundle["parts"].get("presets", []):
        document = entry.get("document")
        found = presets.problems(document, "the bundle's preset", origin="extra")
        if found:
            for text in found:
                problem(plan, "preset", text)
            continue
        old = document["id"]
        new = choices.name("preset", old)
        if not presets.ID.fullmatch(new):  # (a file name here: never a path)
            problem(plan, "preset", f"{shown(new, 60)} is no preset name (lower-case letters, digits and -)")
            continue
        ruleset = entry.get("rules")
        if document.get("rules") and ruleset is not None:
            try:
                checked_ruleset(ruleset, f"preset {old}'s ruleset")
            except ValueError as exc:
                problem(plan, "preset", str(exc))
                continue
        decide(plan, "preset", old, new, {**document, "id": new}, mine.get(new) and {
            k: v for k, v in mine[new].items() if not k.startswith("_")}, choices, names,
            here_kind=(mine.get(new) or {}).get("_origin"))
    # MCP servers: checked as `mcp import` checks one (a catalog's from the catalog there).
    folder_choice = {entry["default"]: choices.maps.get(entry["marker"], choices.maps.get(entry["default"]))
                     or entry["default"] for entry in bundle.get("folders", [])}
    have = {}
    with contextlib.suppress(OSError, ValueError):
        have = mcp().read_config()[1]
    for document in bundle["parts"].get("mcp", []):
        try:
            document = mc.read_document(json.dumps(document))
        except ValueError as exc:
            problem(plan, "mcp", str(exc))
            continue
        old = document["name"]
        new = choices.name("mcp", old)
        replace = choices.replacing("mcp", new)
        # Its folders: each one a folder the bundle asks for (a placeholder of its own), never one taken unasked.
        try:
            named = [path["path"] for path in mc.described(document["kind"], document["definition"])[1]]
        except (ValueError, KeyError, TypeError) as exc:
            problem(plan, "mcp", f"MCP server {old}: {shown(exc, 300)}")
            continue
        unasked = [path for path in named if path not in folder_choice]
        if unasked:
            problem(plan, "mcp", f"MCP server {old}: its folder {shown(unasked[0], 200)} is none the bundle asks for: "
                                 "refused (a bundle's folders are placeholders)")
            continue
        paths = {path: folder_choice[path] for path in named}
        image_member = next((item["member"] for item in bundle.get("images", [])
                             if item.get("for") == f"mcp:{old}"), None)
        try:
            server = mc.plan_import(document, as_name=new, replace=True, paths=paths, secrets_later=True,
                                    secrets=known, here=here, pull_image=choices.pull, unsigned=choices.unsigned)
        except ValueError as exc:
            problem(plan, "mcp", f"MCP server {old}: {exc}")
            continue
        ref = (document.get("image") or {}).get("ref") if isinstance(document.get("image"), dict) else None
        if any(p.get("kind") == "image" for p in server["problems"]):
            if choices.images and image_member:
                server["notes"].append("its image is loaded from the bundle (--images)")
                server["image"] = {**(server.get("image") or {}), "ref": ref, "action": "copied"}
            else:
                plan["missing"]["images"].append({"name": str(ref or old), "fix": f"import again with --pull (Podman "
                                                  "pulls it by its digest), or a bundle made with --images"})
        for item in server["problems"]:
            if item.get("kind") == "vpn":
                vpn = (server.get("definition") or document["definition"]).get("vpn")
                plan["missing"]["vpn"].append({"name": str(vpn), "fix": f"sandbox-ai vpn add {vpn} ..."})
        server["problems"] = [p for p in server["problems"] if p.get("kind") not in ("name", "image", "vpn")]
        server["notes"] = [n for n in server["notes"] if not n.startswith("replaces the server")]
        runs = (server.get("definition") or document["definition"])
        # (what is confirmed says what the server is: its image, or its command - run in its own sandbox)
        plan["notes"].append(f"MCP server {new}: runs " + shown(
            runs.get("image") or " ".join(str(word) for word in runs.get("command") or []) or
            (runs.get("rootfs") or {}).get("sha256") or "?", 300))
        plan["warnings"] += [f"MCP server {new}: {text}" for text in server.get("warnings", [])]
        plan["notes"] += [f"MCP server {new}: {text}" for text in server.get("notes", [])
                          if "secrets-later" not in text]
        names["mcp"][old] = new
        if server["problems"]:
            for item in server["problems"]:
                problem(plan, "mcp", f"MCP server {old}: {item['text']}", item.get("fix", ""))
        existing = have.get(new)
        server["exists"] = existing is not None or new in mcp().BUILTIN
        if new in mcp().BUILTIN or new.startswith("team-"):
            problem(plan, "mcp", f"{here} has an MCP server {new} of its own (built in or a team's)",
                    f"--rename mcp:{old}=NEWNAME")
        elif existing is not None and server["definition"] is not None and same(existing, server["definition"]):
            server["action"] = "reuse"
            plan["reuse"].append({"kind": "mcp", "name": new})
        elif existing is not None and not replace:
            server["action"] = "refused"
            problem(plan, "mcp", f"{here} has an MCP server {new} already, defined otherwise",
                    f"--rename mcp:{old}=NEWNAME, or --replace mcp:{new}")
        elif existing is not None:
            server["action"] = "replace"
            plan["replace"].append({"kind": "mcp", "name": new})
        else:
            server["action"] = "create"
            plan["create"].append({"kind": "mcp", "name": new, "what": f"MCP server ({document['kind']})"})
        plan["mcp"].append(server)
    # LLM servers: their record as stored (no key: the names of its secrets).
    llm = module("llm")
    store = {}
    with contextlib.suppress(OSError, ValueError):
        store = llm.load_store()["endpoints"]
    for entry in bundle["parts"].get("llm", []):
        old, record = entry.get("name"), entry.get("record")
        if not isinstance(old, str) or not isinstance(record, dict):
            problem(plan, "llm", "an LLM server of the bundle is not described as an export describes one")
            continue
        new = choices.name("llm", old)
        try:
            checked = llm.check_record(new, {k: v for k, v in record.items() if k != "ca"} | (
                {"ca": f"{new}.pem"} if entry.get("ca_text") else {}))
            if entry.get("ca_text"):
                llm.check_ca(str(entry["ca_text"]), f"the bundle's CA of {old}")
        except ValueError as exc:
            problem(plan, "llm", f"LLM server {old}: {exc}")
            continue
        wanted = llm.record_of(checked)
        # (what is confirmed says where it is: every prompt and file of a start with --llm NAME goes there)
        plan["notes"].append(f"LLM server {new}: the model requests of a start with --llm {new} go to "
                             f"{shown(checked['url'], 300)}")
        if checked.get("insecure_http"):
            plan["warnings"].append(f"LLM server {new}: plain HTTP to {shown(checked['url'], 200)} (insecure_http), as "
                                    "the bundle's definition says")
        existing = store.get(new)
        comparable = {k: v for k, v in (existing or {}).items() if k not in ("checked", "models", "contexts", "ca")}
        decide(plan, "llm", old, new, wanted, existing, choices, names,
               same_as={k: v for k, v in wanted.items() if k not in ("checked", "models", "contexts", "ca")}
               == comparable if existing is not None else False)
    # Kits, image variants: their files, checked as data (paths inside the folder, no links).
    kits = module("kits")
    for entry in bundle["parts"].get("kits", []):
        old = entry.get("name")
        try:
            files = checked_files(entry.get("files"), f"kit {old}")
            if not isinstance(old, str) or not re.fullmatch(kits.KIT_NAME, old):
                raise ValueError("a kit of the bundle has no valid name")
        except ValueError as exc:
            problem(plan, "kit", str(exc))
            continue
        new = choices.name("kit", old)
        if not re.fullmatch(kits.KIT_NAME, new):
            problem(plan, "kit", f"{shown(new, 60)} is no kit name")
            continue
        root = kits.kits_dir() / new
        existing = tree_digest(root) if root.exists() or root.is_symlink() else None
        decide(plan, "kit", old, new, files, existing, choices, names,
               same_as=existing is not None and existing == files_digest(files))
        if any(f["path"].split("/")[1:2] == ["hooks"] or "/hooks/" in f["path"] for f in files):
            plan["notes"].append(f"kit {new} brings hooks: they run only once you approve them (sandbox-ai kits "
                                 f"trust-hooks {new})")
    images = module("images")
    for entry in bundle["parts"].get("variants", []):
        old, agent = entry.get("name"), entry.get("agent")
        try:
            files = checked_files(entry.get("files"), f"image variant {old}")
            if not isinstance(old, str) or not images.valid_name(old) or agent not in images.AGENTS:
                raise ValueError("an image variant of the bundle has no valid name or agent")
            check_variant(files, agent)
        except ValueError as exc:
            problem(plan, "variant", f"image variant {old}: {exc}")
            continue
        new = choices.name("variant", old)
        if not images.valid_name(new):  # (a folder name here: never a path)
            problem(plan, "variant", f"{shown(new, 60)} is no image variant name")
            continue
        directory = images.config_root() / images.VARIANTS / agent / new
        existing = tree_digest(directory) if directory.exists() else None
        decide(plan, "variant", f"{old}@{agent}", f"{new}@{agent}", files, existing, choices, names,
               same_as=existing is not None and existing == files_digest(files), key=old)
    # Your images: their record; the image itself is pulled by digest there, or loaded from the bundle (--images).
    services = module("services")
    registry = {}
    with contextlib.suppress(OSError, ValueError):
        registry = services.read_registry()
    for entry in bundle["parts"].get("images", []):
        old, record = entry.get("name"), entry.get("record")
        new = choices.name("image", old) if isinstance(old, str) else old
        if not isinstance(new, str) or not services.valid_name(new) or not isinstance(record, dict) or \
                services.record_problem(old, record):
            problem(plan, "image", f"an image of the bundle is not described as one of your images "
                                   f"({shown(old, 60)})")
            continue
        if record["kind"] == "built" and new != old:
            problem(plan, "image", f"image {old} was built from a Containerfile: it keeps its name",
                    f"leave out --rename image:{old}")
            continue
        member = next((item["member"] for item in bundle.get("images", []) if item.get("for") == f"image:{old}"), None)
        existing = registry.get(new)
        decide(plan, "image", old, new, record, existing, choices, names,
               same_as=existing is not None and existing.get("id") == record.get("id"))
        if existing is None or not same(existing.get("id"), record.get("id")):
            if member and choices.images:
                plan["notes"].append(f"image {new}: loaded from the bundle (--images), used only when it is the "
                                     f"image recorded ({record['id'][:19]}…)")
            elif record["kind"] == "pulled":
                plan["missing"]["images"].append({"name": new, "fix": f"sandbox-ai images pull {record['image']} "
                                                                       f"--as {new}"})
            else:
                plan["missing"]["images"].append({"name": new, "fix": "build it there (sandbox-ai images build-image "
                                                                       f"{new} --file CONTAINERFILE), or import the "
                                                                       "bundle made with --images"})
    for entry in bundle.get("images", []):
        why = image_entry_problem(bundle, entry)
        if why:
            problem(plan, "image", f"the bundle carries the image {shown(entry['ref'], 200)} "
                                   f"({shown(entry['for'], 80)}): {why} - refused (an export never makes such a "
                                   "bundle)")
    # By name only: what that machine must have of its own.
    needed = {item["name"] for item in bundle.get("secrets", [])}
    if known is not None:
        plan["missing"]["secrets"] = [{"name": name, "for": next((shown(", ".join(i.get("for") or []), 300)
                                                                  for i in bundle["secrets"] if i["name"] == name), ""),
                                       "fix": f"sandbox-ai secrets set {name}"} for name in sorted(needed - known)]
    else:
        plan["notes"].append(f"the secrets of {here} cannot be read: whether it has "
                             f"{', '.join(sorted(needed)) or 'them'} is not known")
    plan["missing"]["env"] = [{"name": item["name"], "fix": f"export {item['name']}=... before the start"}
                              for item in bundle.get("env", []) if item["name"] not in os.environ]
    with contextlib.suppress(OSError, ValueError):
        identities = module("identities").load()
        plan["missing"]["identities"] = [{"name": name, "fix": f"sandbox-ai identities add {name}"}
                                         for name in bundle["by_name"].get("identities", []) if name not in identities]
    profiles = set(module("vpn").list_profiles())
    plan["missing"]["vpn"] = [{"name": name, "fix": f"sandbox-ai vpn add {name} ..."}
                              for name in bundle["by_name"].get("vpn", []) if name not in profiles]
    # The servers the start names that the bundle does not bring: this machine's own, else missing.
    brought = {d.get("name") for d in bundle["parts"].get("mcp", []) if isinstance(d, dict)}
    llms = {e.get("name") for e in bundle["parts"].get("llm", []) if isinstance(e, dict)}
    plan["missing"]["mcp"], plan["missing"]["llm"] = [], []
    with contextlib.suppress(ValueError, SystemExit):
        for name, value, _ in pairs(launcher().canonical_options(list(bundle["start"]["options"]))):
            if name == "--mcp" and value and value not in brought and value not in have and \
                    value not in mcp().BUILTIN:
                plan["missing"]["mcp"].append({"name": value, "fix": f"sandbox-ai mcp add {value} ... (or mcp copy "
                                                                      "from the machine that has it)"})
            if name == "--llm" and value and value.partition(":")[0] not in llms | set(store):
                plan["missing"]["llm"].append({"name": value.partition(":")[0],
                                               "fix": f"sandbox-ai llm add {value.partition(':')[0]} --kind KIND "
                                                      "--url URL"})
    guard = (bundle["parts"].get("settings") or {}).get("loop_guard")
    if isinstance(guard, dict) and guard != llm.user_loop():
        plan["notes"].append("the loop guard of the exporting machine's defaults differs from yours (not changed: "
                             "your defaults are yours): " + shown(json.dumps(guard, sort_keys=True), 300))


def decide(plan, kind, old, new, wanted, existing, choices, names, same_as=None, here_kind=None, key=None):
    """Reuse (the same is here), create, replace (--replace) or refuse a part of KIND, OLD in the bundle and NEW
    here."""
    names[kind][key or old] = new.partition("@")[0] if kind == "variant" else new
    bare = new.partition("@")[0]
    if existing is None:
        plan["create"].append({"kind": kind, "name": new})
        return "create"
    if same_as if same_as is not None else same(existing, wanted):
        plan["reuse"].append({"kind": kind, "name": new})
        return "reuse"
    if here_kind not in (None, "yours"):
        problem(plan, kind, f"{plan['here']} has a {kind} {new} of sandbox-ai's own (or of a preset folder)",
                f"--rename {kind}:{(key or old).partition('@')[0]}=NEWNAME")
        return "refused"
    if choices.replacing(kind, bare):
        plan["replace"].append({"kind": kind, "name": new})
        return "replace"
    problem(plan, kind, f"{plan['here']} has a {kind} {new} already, defined otherwise",
            f"--rename {kind}:{(key or old).partition('@')[0]}=NEWNAME, or --replace {kind}:{bare}")
    return "refused"


def checked_files(files, label):
    """FILES of a kit or variant of the bundle ([{"path", "mode", "data"}]) checked as data: relative paths inside the
    folder (no .., no absolute path, no hidden control characters), a size in all within TREE_LIMIT; [(path, bytes,
    mode)]."""
    kits = module("kits")
    if not isinstance(files, list) or len(files) > kits.KIT_FILES:
        raise ValueError(f"{label}: its files are not a list (at most {kits.KIT_FILES})")
    found, total, seen = [], 0, set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("data"), str):
            raise ValueError(f"{label}: a file without a path or data")
        path = item["path"]
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or "/".join(parts) != path or \
                any(part in ("", ".", "..") for part in parts) or \
                any(kits.bad_name(part) for part in parts) or "\\" in path or len(parts) > kits.KIT_DEPTH or \
                path in seen:
            raise ValueError(f"{label}: {shown(path, 120)} is no path inside its folder - refused")
        seen.add(path)
        try:
            data = base64.b64decode(item["data"], validate=True)
        except ValueError:
            raise ValueError(f"{label}: {shown(path, 120)} is not base64") from None
        total += len(data)
        if total > TREE_LIMIT:
            raise ValueError(f"{label}: larger than {TREE_LIMIT >> 20} MiB")
        mode = 0o755 if item.get("mode") == 0o755 else 0o644
        found.append({"path": path, "data": data, "mode": mode})
    if module("hostkits").scan([(f["path"], f["data"], f["mode"]) for f in found]):
        raise ValueError(f"{label}: a file holds a secret-looking line - refused")
    return found


def files_digest(files):
    return sorted((f["path"], hashlib.sha256(f["data"]).hexdigest(), f["mode"]) for f in files)


def tree_digest(root):
    """files_digest of the folder ROOT as it is here (nothing followed), or a marker that differs from any."""
    try:
        files, _notes = read_folder(root, str(root))
    except (OSError, ValueError):
        return [("<unreadable>", "", 0)]
    return sorted((path, hashlib.sha256(data).hexdigest(), 0o755 if mode & 0o111 else 0o644)
                  for path, data, mode in files)


def check_variant(files, agent):
    """A variant's image.json and fragment, checked as `images extend` checks them."""
    images = module("images")
    by_path = {f["path"]: f["data"] for f in files}
    if images.DOCUMENT not in by_path:
        raise ValueError(f"it has no {images.DOCUMENT}")
    document = parse_json(by_path[images.DOCUMENT])
    if not isinstance(document, dict) or document.get("version") != 1 or document.get("agent") != agent:
        raise ValueError(f"its {images.DOCUMENT} is not one of {agent}")
    found = images.check_document(document)
    fragment = by_path.get(images.FRAGMENT)
    if fragment is not None:
        found += images.check_fragment(fragment.decode("utf-8", "replace"))
    errors = [item for item in found if isinstance(item, dict) and item.get("severity") == "error"]
    if errors:
        raise ValueError("its extension is refused: " + "; ".join(shown(item.get("message") or item, 200)
                                                                   for item in errors[:3]))


def renamed_value(name, value, names):
    """VALUE of option NAME with the parts renamed at the import (--rename)."""
    if name == "--mcp":
        return names["mcp"].get(value, value)
    if name in ("--mcp-limits", "--mcp-env", "--mcp-mount"):
        server, colon, rest = value.partition(":")
        return f"{names['mcp'].get(server, server)}{colon}{rest}"
    if name == "--llm":
        server, colon, rest = value.partition(":")
        return f"{names['llm'].get(server, server)}{colon}{rest}"
    if name == "--kit":
        return names["kit"].get(value, value)
    if name == "--image-variant":
        return names["variant"].get(value, value)
    if name == "--preset":
        return names["preset"].get(value, value)
    if name == "--service":
        service, colon, rest = value.partition(":")
        parts = []
        for part in rest.split(",") if colon else []:
            key, equals, given = part.partition("=")
            parts.append(f"image={names['image'].get(given, given)}" if key == "image" and equals else part)
        if not colon and service in names["image"] and names["image"][service] != service:
            return f"{service}:image={names['image'][service]}"
        return service + (":" + ",".join(parts) if colon else "")
    return value


def resolved_start(plan, bundle, folder_map, value_map, rules_paths, names, choices):
    """The start of the bundle as it runs here: folders and values filled in, rulesets where the import writes them,
    renamed parts; what a passed-on start may not carry refused (a problem each). None when it cannot be read."""
    l = launcher()
    start = bundle["start"]
    try:
        options = l.canonical_options(list(start["options"]))
    except (ValueError, SystemExit) as exc:
        problem(plan, "options", f"the bundle's options cannot be read: {shown(exc, 300)}")
        return None
    if "--" in options:
        problem(plan, "options", "the bundle's options hold --: agent arguments go in agent_args")
        return None
    out = []
    for name, value, attached in pairs(options):
        if name in LOGIN_OPTIONS or (name == "--copy" and value is not None and not MARKER.search(value)
                                     and l.copies_credential(value)):
            problem(plan, "options", f"the bundle's start carries {name}: a login or token of a file is never taken "
                                     "from a bundle - give the start its own login here")
            continue
        if name in INTERNAL_OPTIONS:
            problem(plan, "options", f"the bundle's start carries {name}: an option of one start only, never in a "
                                     "bundle")
            continue
        if name == "--vpn-bypass-host-ports" or name == "--vpn-bypass-tcp" and not MARKER.fullmatch(
                str(value).strip()):
            problem(plan, "options", f"the bundle's start carries {name}: it lets the run past its VPN or Tor, from "
                                     "your own address - only you choose that")
            continue
        if value is None:
            out.append([name, value, attached])
            continue
        if name == "--rules":
            match = RULES_MARKER.fullmatch(value)
            if match is None or match[1] not in rules_paths:
                problem(plan, "options", f"the bundle's start names the ruleset {shown(value, 200)}: a ruleset of a "
                                         "bundle comes with it, never as a file of this machine")
                continue
            out.append([name, rules_paths[match[1]], attached])
            continue
        if name == "--llm-traffic" and value == "all":
            problem(plan, "options", "the bundle's start carries --llm-traffic all: only you choose that (your "
                                     "command line, defaults or a preset of yours)")
            continue
        if name == "--mcp-env" and mcp().code_env_in(["--mcp-env", value]):
            # (as a preset passed on: LD_PRELOAD, NODE_OPTIONS, PYTHONSTARTUP ... - also by a value given here)
            problem(plan, "options", f"the bundle's start carries {mcp().code_env_in(['--mcp-env', value])[0]}: it "
                                     "changes what code runs in the MCP server's sandbox (with the server's secrets "
                                     "and network) - only you choose that, on your own command line")
            continue
        if name in FOLDER_OPTIONS or option_path(name, value):
            paths = [p for p in value.split(",") if p] if name == "--workspace" else [option_path(name, value)]
            for path in paths:
                if path and not MARKER.fullmatch(path):
                    problem(plan, "options", f"the bundle's start names the folder {shown(path, 200)} of {name} as it "
                                             "is: a bundle's folders are placeholders, asked for at the import")
        out.append([name, renamed_value(name, value, names), attached])
    if plan["problems"] and any(p["kind"] == "options" for p in plan["problems"]):
        return None

    def fill(text):
        for marker, to in {**folder_map, **value_map}.items():
            text = text.replace(marker, to)
        return text
    options = [fill(token) for token in tokens_of(out)]
    agent_args = [fill(str(a)) for a in start.get("agent_args", [])]
    left = sorted({m[0] for text in options + agent_args for m in MARKER.finditer(text)})
    for marker in left:
        if marker not in folder_map and marker not in value_map and \
                not any(p.get("kind") in ("value", "folder") for p in plan["problems"]):
            problem(plan, "options", f"the bundle's start holds {marker}, which it does not describe")
    return {"agent": start["agent"], "options": options, "agent_args": agent_args}


# --- the import itself ----------------------------------------------------------------------------------------------

def apply_import(plan, received, choices, out=print, run=subprocess.run):
    """PLAN carried out (no problems left): images loaded (--images), the parts written, the board kept. What this
    import created is removed again when a later step fails."""
    if plan["problems"]:
        raise ValueError(problems_text(plan))
    bundle = received.document
    folder_map, value_map, rules_paths, names = plan["_maps"]
    undo = []
    AFTER.clear()
    try:
        if choices.images:
            load_images(plan, received, out, run)
        presets = module("presets")
        for entry in bundle["parts"].get("presets", []):
            old = entry["document"]["id"]
            new = names["preset"].get(old, old)
            if {"kind": "preset", "name": new} in plan["reuse"]:
                continue
            directory = presets.user_dir()
            document = {**entry["document"], "id": new}
            if entry.get("rules") and document.get("rules"):
                document["rules"] = f"{new}.rules.json"
                undo.append(write_new(directory / document["rules"], json.dumps(entry["rules"], indent=2) + "\n",
                                      replace=True))
            undo.append(write_new(directory / f"{new}.json", json.dumps(document, indent=2) + "\n", replace=True))
            out(f"sandbox-ai board: preset {new} written")
        mc = mcp_copy()
        for server in plan["mcp"]:
            if server.get("action") in ("reuse",):
                continue
            before = mcp().read_config()[1].get(server["server"])
            definition = mc.apply_import(server, out)
            undo.append(lambda name=server["server"], before=before: remove_server(name) if before is None
                        else mcp().write_definition(name, before))
            verb = "replaced" if server.get("action") == "replace" else "written"
            out(f"sandbox-ai board: MCP server {server['server']} {verb}"
                f" ({definition.get('image') or ' '.join(definition.get('command', []))[:80]})")
        llm = module("llm")
        for entry in bundle["parts"].get("llm", []):
            new = names["llm"].get(entry["name"], entry["name"])
            if {"kind": "llm", "name": new} in plan["reuse"]:
                continue
            with llm.settings_lock(llm.store_path()):
                store = llm.load_store()
                had = store["endpoints"].get(new)
                record = {k: v for k, v in entry["record"].items() if k != "ca"}
                if entry.get("ca_text"):
                    record["ca"] = f"{new}.pem"
                    ca = llm.ca_dir() / f"{new}.pem"
                    ca_before = ca.read_bytes() if ca.is_file() and not ca.is_symlink() else None
                    llm.write_private(ca, str(entry["ca_text"]).encode())
                    undo.append(lambda ca=ca, ca_before=ca_before: ca.unlink(missing_ok=True) if ca_before is None
                                else llm.write_private(ca, ca_before))
                store["endpoints"][new] = llm.record_of(llm.check_record(new, record))
                llm.save_store(store)
            undo.append(lambda name=new, had=had: remove_llm(name, had))
            out(f"sandbox-ai board: LLM server {new} written (its key: sandbox-ai secrets, by name)")
        kits = module("kits")
        for entry in bundle["parts"].get("kits", []):
            new = names["kit"].get(entry["name"], entry["name"])
            if {"kind": "kit", "name": new} in plan["reuse"]:
                continue
            undo.append(write_tree(kits.kits_dir(), new, checked_files(entry["files"], f"kit {new}")))
            out(f"sandbox-ai board: kit {new} written")
        images = module("images")
        for entry in bundle["parts"].get("variants", []):
            new = names["variant"].get(entry["name"], entry["name"])
            if {"kind": "variant", "name": f"{new}@{entry['agent']}"} in plan["reuse"]:
                continue
            images.check_location(images.config_root(), "The image extensions in")
            undo.append(write_tree(images.config_root() / images.VARIANTS / entry["agent"], new,
                                   checked_files(entry["files"], f"image variant {new}")))
            out(f"sandbox-ai board: image variant {new} of {entry['agent']} written (built at its first start: "
                f"{images.build_command(new)})")
        services = module("services")
        for entry in bundle["parts"].get("images", []):
            new = names["image"].get(entry["name"], entry["name"])
            if {"kind": "image", "name": new} in plan["reuse"]:
                continue
            record = entry["record"]
            image = record["image"] if record["kind"] == "pulled" else services.IMAGE_TAG.format(name=new)
            if image_id(image, run) != record["id"]:
                continue  # (not here, or another image of that name: listed as missing in the plan)
            before = services.record_entry(new, {**record, "image": image}, replace=True)
            undo.append(lambda name=new, before=before: forget_image(name, before))
            out(f"sandbox-ai board: image {new} recorded (unchecked: nobody reviewed it)")
        target = boards_dir() / plan["board"]
        for entry in bundle["parts"].get("rules", []):
            path = Path(rules_paths[entry["id"]])
            undo.append(write_new(path, json.dumps(entry["ruleset"], indent=2) + "\n", replace=True))
        board = {"schema": BOARD_SCHEMA, "version": BOARD_VERSION, "name": plan["board"],
                 "agent": plan["start"]["agent"], "options": plan["start"]["options"],
                 "agent_args": plan["start"]["agent_args"],
                 "project": dict(bundle.get("project") or {}),
                 "state": imported_state(bundle.get("board"), folder_map, value_map, names, rules_paths),
                 "imported": {"from": bundle.get("source"), "created": bundle.get("created"),
                              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
        if isinstance(bundle.get("app_preset"), dict):
            board["app_preset"] = replaced(bundle["app_preset"], lambda text: fill_markers(text, folder_map, value_map))
        target.mkdir(parents=True, exist_ok=True)
        undo.append(write_new(board_path(plan["board"]), json.dumps(board, indent=2) + "\n", replace=True))
    except BaseException:
        for step in reversed(undo):
            with contextlib.suppress(Exception):
                step()
        AFTER.clear()
        raise
    for step in AFTER:
        with contextlib.suppress(Exception):
            step()
    AFTER.clear()
    return board


def fill_markers(text, folder_map, value_map):
    for marker, to in {**folder_map, **value_map}.items():
        text = text.replace(marker, to)
    return text


def imported_state(state, folder_map, value_map, names, rules_paths):
    """The app's board as imported: markers filled in; the parts renamed; the project's own MCP servers and ruleset
    now yours (mcp.json, a rules file): no longer the project file's."""
    if not isinstance(state, dict):
        return None
    state = replaced(state, lambda text: fill_markers(text, folder_map, value_map))
    for node in state.get("nodes", []) if isinstance(state.get("nodes"), list) else []:
        props = node.get("props") if isinstance(node, dict) else None
        if not isinstance(props, dict):
            continue
        if node.get("kind") == "mcp" and isinstance(props.get("name"), str):
            props["name"] = names["mcp"].get(props["name"], props["name"])
            props.pop("project", None)
            props.pop("definition", None)
        if node.get("kind") == "llm" and isinstance(props.get("--llm"), str):
            server, colon, rest = props["--llm"].partition(":")
            props["--llm"] = names["llm"].get(server, server) + colon + rest
        if node.get("kind") == "approvals" and isinstance(props.get("rules"), dict) and "project" in rules_paths:
            props.pop("rules")
            props["--rules"] = [*(props.get("--rules") or []), rules_paths["project"]]
        for key, value in list(props.items()):
            if isinstance(value, str) and RULES_MARKER.search(value):
                props[key] = RULES_MARKER.sub(lambda m: rules_paths.get(m[1], m[0]), value)
            elif isinstance(value, list):
                props[key] = [RULES_MARKER.sub(lambda m: rules_paths.get(m[1], m[0]), v) if isinstance(v, str) else v
                              for v in value]
    return state


def write_new(path, text, replace=False):
    """TEXT into PATH (mode 0600, its folder 0700) in one step; the step that undoes it (the file before put back)."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"{path}: a symlink - not written through")
    before = path.read_bytes() if path.is_file() else None
    if before is not None and not replace:
        raise ValueError(f"{path} exists")
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{os.urandom(4).hex()}")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600),
              "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)

    def undo():
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)
    return undo


def write_tree(parent, name, files):
    """FILES ([{"path", "data", "mode"}]) as the folder PARENT/NAME (made beside it, then put in place; one there
    before is moved aside and back on undo). The undo step."""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / name
    if target.is_symlink():
        raise ValueError(f"{target}: a symlink - not written through")
    staging = Path(tempfile.mkdtemp(prefix=f".import-{name[:30]}-", dir=parent))
    try:
        for item in files:
            path = staging.joinpath(*PurePosixPath(item["path"]).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                              item["mode"]), "wb") as stream:
                stream.write(item["data"])
            os.chmod(path, item["mode"])
        aside = None
        if target.exists():
            aside = parent / f".replaced-{name[:30]}-{os.getpid()}-{os.urandom(4).hex()}"
            os.rename(target, aside)
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    def undo():
        shutil.rmtree(target, ignore_errors=True)
        if aside is not None:
            os.rename(aside, target)
    if aside is not None:
        AFTER.append(lambda: shutil.rmtree(aside, ignore_errors=True))  # (removed once the whole import is done)
    return undo


AFTER = []  # what an import does once every step of it went well (the folders it replaced, kept aside until then)


def remove_server(name):
    m = mcp()
    with m.config_lock():
        path, document = m.read_config()
        if name in document:
            del document[name]
            path.write_text(json.dumps(document, indent=2))


def remove_llm(name, before=None):
    """The LLM server NAME taken out of the store again (BEFORE: the record it had, put back)."""
    llm = module("llm")
    with llm.settings_lock(llm.store_path()):
        store = llm.load_store()
        if before is None:
            store["endpoints"].pop(name, None)
        else:
            store["endpoints"][name] = before
        llm.save_store(store)


def forget_image(name, before=None):
    """The record of your image NAME as it was before an import (BEFORE; None: none)."""
    services = module("services")
    with services.registry_lock():
        found = services.read_registry()
        if before is None:
            found.pop(name, None)
        else:
            found[name] = before
        services.write_registry(found)


def image_id(image, run=subprocess.run):
    """The ID of IMAGE in this machine's Podman, or None."""
    if not shutil.which("podman"):
        return None
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        done = run(["podman", "image", "inspect", "--format", "{{.Id}}", image], stdin=subprocess.DEVNULL,
                   capture_output=True, text=True, timeout=60)
        if done.returncode == 0:
            return done.stdout.strip().removeprefix("sha256:") or None
    return None


def image_entry_problem(bundle, entry):
    """Why the image ENTRY of BUNDLE's "images" is never loaded here, or None: it is the image of one part of the
    bundle - an MCP server's, by the pinned reference its definition names, or one of your images, by its record's
    reference - and its reference is no name of sandbox-ai's own images (but your built image's own tag)."""
    ref, what = entry.get("ref"), entry.get("for")
    kind, _, name = str(what).partition(":")
    if kind == "mcp":
        document = next((d for d in bundle["parts"].get("mcp", []) if isinstance(d, dict) and d.get("name") == name),
                        None)
        image = document.get("image") if document else None
        definition = document.get("definition") if document else None
        if not isinstance(image, dict) or not isinstance(definition, dict) or image.get("ref") != ref or \
                definition.get("image") != ref or not isinstance(ref, str) or not mcp().IMAGE_REF.fullmatch(ref):
            return "no MCP server of the bundle uses it by that pinned reference"
    elif kind == "image":
        services = module("services")
        part = next((e for e in bundle["parts"].get("images", []) if isinstance(e, dict) and e.get("name") == name),
                    None)
        record = part.get("record") if part else None
        if not isinstance(record, dict) or services.record_problem(name, record) or record.get("image") != ref:
            return "none of the bundle's images of yours is recorded under that reference"
        if record["kind"] == "built":
            return None  # (localhost/sandbox-ai-image-NAME:latest: your image of that name, asked about as a part)
    else:
        return "it is the image of no part of the bundle"
    if mcp_copy().own_image_name(ref):
        return "its reference is the name of one of sandbox-ai's own images"
    return None


def load_images(plan, received, out=print, run=subprocess.run):
    """The images of the bundle loaded into this machine's Podman: each archive read first - it may name only its own
    repository (podman load gives an archive's images every name it says: never one of this machine's other images)."""
    mc = mcp_copy()
    for entry in received.document.get("images", []):
        path = received.images.get(entry["member"])
        if path is None:
            continue
        ref = entry["ref"]
        why = image_entry_problem(received.document, entry)
        if why:
            raise ValueError(f"the bundle's image {shown(ref, 200)} is not loaded: {why}")
        repository = ref.partition("@")[0].rpartition(":")[0] if "@" not in ref and ref.count(":") > ref.count("/") \
            else ref.partition("@")[0]
        names = mc.archive_names(path)
        others = [name for name in names
                  if name != repository and not name.startswith((repository + ":", repository + "@"))]
        # (a name of sandbox-ai's own images only as your built image's own tag: the reference itself)
        others += [name for name in names if mc.own_image_name(name) and name != ref and name not in others]
        if others:
            raise ValueError(f"the bundle's image {shown(ref, 200)} is an archive that names other images "
                             f"({shown(', '.join(others[:5]), 300)}): nothing was loaded")
        done = run(["podman", "load", "--input", path], stdin=subprocess.DEVNULL, capture_output=True, check=False)
        if done.returncode != 0:
            said = done.stderr.decode(errors="replace") if isinstance(done.stderr, bytes) else str(done.stderr)
            raise ValueError(f"podman load of {shown(ref, 200)} failed: {shown(said.strip()[-300:], 300)}")
        out(f"sandbox-ai board: the image {ref} was loaded from the bundle")


def problems_text(plan):
    lines = [f"board {plan['board']}: not imported:"]
    for item in plan["problems"]:
        lines.append(f"  - {item['text']}" + (f"; resolve: {item['fix']}" if item.get("fix") else ""))
    return "\n".join(lines)


def plan_text(plan):
    """What PLAN does, for people."""
    source = plan["bundle"].get("source") or {}
    lines = [f"board {plan['board']} from the bundle of {source.get('kind', '?')} {shown(source.get('name', '?'), 80)}"
             f" (sandbox-ai {plan['bundle'].get('sandbox_ai') or '?'}, {plan['bundle'].get('created') or '?'})"
             + (" - replaces the board there" if plan["replace_board"] else "")]
    for item in plan["folders"]:
        lines.append(f"  folder {item['marker']} ({item['label']}): {shown(item['to'], 200)}"
                     + ("" if item["chosen"] else " (the bundle's default)"))
    for item in plan["values"]:
        lines.append(f"  value {item['marker']} ({item['label']}): " + ("given" if item["given"] else "to give"))
    for verb, key in (("creates", "create"), ("reuses (the same is here)", "reuse"), ("replaces", "replace")):
        for item in plan[key]:
            lines.append(f"  {verb}: {item['kind']} {shown(item['name'], 80)}")
    for kind, items in plan["missing"].items():
        for item in items:
            lines.append(f"  missing here ({kind}): {shown(item['name'], 80)} - {shown(item['fix'], 200)}")
    for text in plan["withheld"]:
        lines.append(f"  not in the bundle: {text}")
    for text in plan["notes"]:
        lines.append(f"  note: {text}")
    for text in plan["warnings"]:
        lines.append(f"  warning: {text}")
    if plan.get("command"):
        lines.append(f"  starts as: {plan['command']}")
    if plan["problems"]:
        lines.append(problems_text(plan))
    return "\n".join(lines)


def public_plan(plan):
    """PLAN as JSON shows it: without what only the import itself reads (its maps, a catalog's entry)."""
    return {**{key: value for key, value in plan.items() if not key.startswith("_")},
            "mcp": [{**server, "entry": None} for server in plan["mcp"]]}


def confirm(question):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    answer = input(f"{printable(question)} [y/N] ")
    return answer.strip().lower() in ("y", "yes", "j", "ja")


# --- the commands -----------------------------------------------------------------------------------------------

def export_main(a, out=print):
    if bool(a.session) == bool(a.board):
        raise ValueError("give the session (--session NAME) or the board (--board FILE|NAME|-) to export")
    if a.host and a.host != "local":
        return export_from_host(a, out)
    say = (lambda text: print(text, file=sys.stderr)) if a.out == "-" or a.json else out
    if a.session:
        document, name = source_of_session(a.session)
        kind = "session"
    else:
        document = source_of_board(a.board)
        name, kind = document.get("name") or Path(a.board).stem, "board"
    bundle = export_bundle(document, kind, name)
    saved, folder = ([], None)
    try:
        if a.images:
            saved, folder = save_images(bundle, say)
        write_bundle(bundle, a.out, saved)
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    summary = {"written": a.out, "name": bundle["name"], "source": bundle["source"],
               "folders": [f["default"] for f in bundle["folders"]], "secrets": [s["name"] for s in bundle["secrets"]],
               "withheld": [w["text"] for w in bundle["withheld"]], "images": len(bundle["images"]),
               "parts": {key: len(value) for key, value in bundle["parts"].items() if isinstance(value, list)}}
    if a.json and a.out != "-":
        print(json.dumps(summary, indent=2))
    elif a.out != "-":
        say(f"sandbox-ai board: {kind} {shown(name, 80)} exported to {a.out}"
            + (f" with {len(bundle['images'])} image(s)" if bundle["images"] else ""))
        for text in bundle["withheld"]:
            say(f"  not in it: {text['text']}")
        if bundle["secrets"]:
            say("  secrets by name only (never a value): " + ", ".join(s["name"] for s in bundle["secrets"]))
        say("  it holds this machine's folder paths and names: pass it on knowingly")
    return 0


def export_from_host(a, out):
    """`board export ... --host HOST`: the export made there, its bundle written here (checked as one first)."""
    hosts = mcp_copy().hosts_module()
    args = ["board", "export", *(["--session", a.session] if a.session else ["--board", "-"]), "-",
            *(["--images"] if a.images else [])]
    stdin = subprocess.DEVNULL
    if a.board:
        stdin = subprocess.PIPE
        document = source_of_board(a.board)
    with tempfile.NamedTemporaryFile(prefix="sandbox-ai-bundle-", suffix=".part", dir=mcp_copy().spool_folder()) \
            as spooled, tempfile.TemporaryFile() as said:
        process = subprocess.Popen(hosts.argv(a.host, args), stdin=stdin, stdout=spooled, stderr=said)
        if a.board:
            process.stdin.write(json.dumps(document).encode())
            process.stdin.close()
        try:
            status = process.wait(timeout=EXPORT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise ValueError(f"{a.host}: the export did not end within {EXPORT_TIMEOUT} s") from None
        said.seek(0)
        err = said.read()[-4000:].decode(errors="replace")
        if status != 0:
            raise ValueError(f"{a.host}: sandbox-ai board export: {shown(err.strip()[-600:] or f'exit {status}', 600)}")
        spooled.seek(0)
        received = read_bundle_bytes(spooled)  # (another machine's answer: read as a bundle before it is kept)
        received.close()
        spooled.seek(0)
        if a.out == "-":
            shutil.copyfileobj(spooled, sys.stdout.buffer)
        else:
            target = Path(a.out).expanduser()
            if target.is_symlink() or target.is_dir():
                raise ValueError(f"{a.out}: a symlink or a folder - give a new file's path")
            with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600),
                      "wb") as stream:
                shutil.copyfileobj(spooled, stream)
    bundle = received.document
    if a.json and a.out != "-":
        print(json.dumps({"written": a.out, "name": bundle["name"], "source": bundle["source"], "host": a.host,
                          "folders": [f["default"] for f in bundle["folders"]],
                          "secrets": [s["name"] for s in bundle["secrets"]],
                          "withheld": [w.get("text") for w in bundle["withheld"]], "images": len(bundle["images"])},
                         indent=2))
    elif a.out != "-":
        out(f"sandbox-ai board: {bundle['source'].get('kind')} {shown(bundle['source'].get('name'), 80)} of "
            f"{shown(a.host, 60)} exported to {a.out}")
    return 0


def import_main(a, out=print, stdin=None):
    if a.host and a.host != "local":
        return import_on_host(a, out)
    choices = Choices(a.as_name, a.map, a.value, a.rename, a.replace, a.images, a.pull, a.unsigned)
    received = read_bundle(a.file, stdin)
    try:
        plan = plan_import(received, choices)
        say = (lambda text: print(text, file=sys.stderr)) if a.json else out
        if plan["problems"] or a.check:
            if a.json:
                print(json.dumps({**public_plan(plan), "written": False, "check": a.check}, indent=2))
            else:
                out(plan_text(plan))
                if not plan["problems"]:
                    out("Checked: nothing was written (--check).")
            return 1 if plan["problems"] else 0
        if not a.yes and not confirm(f"{plan_text(plan)}\nImport the board {plan['board']} with these parts?"):
            if not a.json:
                out(plan_text(plan))
            raise ValueError("nothing was imported (it asks first; --yes when nothing can ask)")
        board = apply_import(plan, received, choices, say)
    finally:
        received.close()
    say(f"sandbox-ai board: {plan['board']} imported (not started); start it: sandbox-ai board show "
        f"{plan['board']} - or: {plan['command']}")
    if a.json:
        print(json.dumps({**public_plan(plan), "written": True, "check": False, "board_document": board}, indent=2))
    return 0


def import_on_host(a, out):
    """`board import FILE --host HOST`: the bundle sent there, its plan made there (a check), asked here, then
    imported there."""
    mc = mcp_copy()
    hosts = mc.hosts_module()
    words = ["board", "import", "-", "--json", *[f"--map={v}" for v in a.map], *[f"--value={v}" for v in a.value],
             *[f"--rename={v}" for v in a.rename], *[f"--replace={v}" for v in a.replace],
             *([f"--as={a.as_name}"] if a.as_name else []), *(["--images"] if a.images else []),
             *(["--pull"] if a.pull else []), *(["--unsigned"] if a.unsigned else [])]

    def there(extra):
        with open(Path(a.file).expanduser(), "rb") as stream:
            done = subprocess.run(hosts.argv(a.host, words + extra), stdin=stream, capture_output=True,
                                  timeout=EXPORT_TIMEOUT)
        try:
            plan = mc.received(json.loads(done.stdout.decode(errors="replace")))
        except (ValueError, RecursionError):
            plan = None
        if not isinstance(plan, dict) or not isinstance(plan.get("problems"), list):
            raise ValueError(f"{a.host}: sandbox-ai board import: "
                             f"{shown((done.stderr or done.stdout).decode(errors='replace').strip()[-600:], 600)}")
        return done.returncode, plan
    status, plan = there(["--check"])
    if a.check or plan["problems"]:
        print(json.dumps(plan, indent=2)) if a.json else out(plan_text_received(plan, a.host))
        return 1 if plan["problems"] else 0
    if not a.yes and not confirm(f"{plan_text_received(plan, a.host)}\nImport it there?"):
        raise ValueError("nothing was imported (it asks first; --yes when nothing can ask)")
    status, plan = there(["--yes"])
    print(json.dumps(plan, indent=2)) if a.json else out(plan_text_received(plan, a.host))
    return status


def plan_text_received(plan, host):
    try:
        return f"on {shown(host, 60)}:\n" + plan_text(plan)
    except (KeyError, TypeError, AttributeError):
        return f"on {shown(host, 60)}: " + shown(json.dumps(plan), 2000)


def list_main(a, out=print):
    rows = []
    folder = boards_dir()
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            document = read_board_file(read_private_file(path, BOARD_LIMIT).decode("utf-8", "replace"))
        except ValueError:
            continue
        rows.append({"name": path.stem, "agent": document["agent"],
                     "from": (document.get("imported") or {}).get("from"),
                     "at": (document.get("imported") or {}).get("at")})
    if a.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        out("No boards imported here yet (sandbox-ai board import FILE).")
    else:
        for row in rows:
            source = row["from"] or {}
            out(f"{row['name']:<24} {row['agent']:<10} from {source.get('kind', '?')} "
                f"{shown(source.get('name', '?'), 40)}  {row['at'] or ''}")
    return 0


def show_main(a, out=print):
    document = kept_board(a.name)
    command = ["sandbox-ai", document["agent"], "--no-config", *document["options"],
               *(["--", *document["agent_args"]] if document["agent_args"] else [])]
    result = {"name": a.name, "agent": document["agent"], "options": document["options"],
              "agent_args": document["agent_args"], "command": shlex.join(command),
              "imported": document.get("imported")}
    status = 0
    if a.check:
        check = [document["agent"], "--dry-run", "--no-config", *document["options"]]
        done = subprocess.run([sys.executable, "-I", str(HERE / "main.py"), *check], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=600)
        status = done.returncode
        result["check"] = {"status": status, "said": [printable(line) for line in
                                                      (done.stderr or done.stdout).strip().splitlines()[-20:]]}
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        out(result["command"])
        if a.check:
            out("checked: it can start here" if status == 0 else "checked: it cannot start here as it is:")
            for line in result["check"]["said"] if status else []:
                out(f"  {line}")
    return status


def delete_main(a, out=print):
    path = board_path(a.name)
    if not path.is_file():
        raise ValueError(f"no board {a.name} here (sandbox-ai board list)")
    path.unlink()
    shutil.rmtree(boards_dir() / a.name, ignore_errors=True)
    out(f"sandbox-ai board: {a.name} deleted (the servers, kits and presets it brought stay)")
    return 0


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai board", formatter_class=help_style, allow_abbrev=False,
                                description="A board or a session as one self-sufficient file (a bundle), and that "
                                            "file taken in on another machine: the start's options, the board, the "
                                            "MCP and LLM servers, rulesets, kits, presets and image recipes it uses "
                                            "- never a secret's value.")
    commands = p.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Write a session's or a board's bundle", formatter_class=help_style,
                                 description="Write the bundle of a session (running or not) or of a board file: "
                                             "one JSON file, or with --images a tar archive with the images.")
    export.add_argument("out", metavar="OUT", help="the bundle's file ('-': standard output)")
    export.add_argument("--session", metavar="NAME", help="The session to export (sandbox-ai list)")
    export.add_argument("--board", metavar="FILE", help="The board file to export, a board imported here by its "
                                                        "name, or '-' (standard input: the app's board)")
    export.add_argument("--images", action="store_true", help="Also the MCP servers' and your images (podman save): "
                                                              "the bundle works offline; a tar archive")
    export.add_argument("--host", metavar="HOST", help="The session or board of this host of sandbox-ai hosts; the "
                                                       "bundle is written here")
    export.add_argument("--json", action="store_true", help="What was written, as JSON")
    importing = commands.add_parser("import", help="Take a bundle in: plan, ask, then write its parts",
                                    formatter_class=help_style,
                                    description="Take a bundle in as passed on: its plan first (what is created, "
                                                "reused, replaced, what is missing), asked, then its parts written "
                                                "and the board kept (never started).")
    importing.add_argument("file", metavar="FILE", help="the bundle ('-': standard input)")
    importing.add_argument("--check", action="store_true", help="Only show the plan: nothing is written")
    importing.add_argument("--as", dest="as_name", metavar="NAME", help="The board's name here (default: the "
                                                                        "bundle's)")
    importing.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                           help="A folder: OLD its placeholder (<folder:NAME>) or the bundle's path, NEW the folder "
                                "here; default: the bundle's path (repeatable)")
    importing.add_argument("--value", action="append", default=[], metavar="NAME=VALUE",
                           help="A value the bundle withheld (<text:NAME>) given here (repeatable)")
    importing.add_argument("--rename", action="append", default=[], metavar="KIND:OLD=NEW",
                           help=f"A part under another name here; KIND: {', '.join(KINDS[:-1])} (repeatable)")
    importing.add_argument("--replace", action="append", default=[], metavar="KIND:NAME",
                           help=f"Replace the part of that name here; KIND: {', '.join(KINDS)} (repeatable)")
    importing.add_argument("--images", action="store_true", help="Load the bundle's images into Podman here (each "
                                                                 "archive checked first)")
    importing.add_argument("--pull", action="store_true", help="Pull the MCP servers' images this machine lacks, "
                                                               "by their digests")
    importing.add_argument("--unsigned", action="store_true", help="Take an MCP server of a catalog that is not "
                                                                   "signed (as mcp import --unsigned)")
    importing.add_argument("-y", "--yes", action="store_true", help="Confirmed: do not ask (when nothing can ask)")
    importing.add_argument("--host", metavar="HOST", help="Import it on this host of sandbox-ai hosts")
    importing.add_argument("--json", action="store_true", help="The plan (and what was written) as JSON")
    listing = commands.add_parser("list", help="The boards imported here", formatter_class=help_style,
                                  description="The boards imported here (~/.config/sandbox-ai/boards).")
    listing.add_argument("--json", action="store_true", help="As JSON")
    showing = commands.add_parser("show", help="An imported board's start", formatter_class=help_style,
                                  description="An imported board's start: its command line; --check runs its dry "
                                              "run here.")
    showing.add_argument("name", metavar="NAME", help="the board (sandbox-ai board list)")
    showing.add_argument("--check", action="store_true", help="The launcher's dry run of its start here")
    showing.add_argument("--json", action="store_true", help="As JSON")
    deleting = commands.add_parser("delete", help="Forget an imported board", formatter_class=help_style,
                                   description="Forget an imported board (the parts it brought stay).")
    deleting.add_argument("name", metavar="NAME", help="the board (sandbox-ai board list)")
    return p


def main(argv, out=print):
    a = parser().parse_args(argv)
    try:
        if a.command == "export":
            return export_main(a, out)
        if a.command == "import":
            return import_main(a, out)
        if a.command == "list":
            return list_main(a, out)
        if a.command == "show":
            return show_main(a, out)
        return delete_main(a, out)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"sandbox-ai board {a.command}: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
