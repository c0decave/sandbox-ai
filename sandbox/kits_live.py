#!/usr/bin/env python3
"""Skills, subagents, commands and plugins for a sandbox while it runs.

  sandbox-ai kits attach START (KIT | --skill DIR | --command FILE | --agent-file FILE) [--agent A] [--keep]
                                                       [--restart] [--json]
  sandbox-ai kits detach START NAME [--kind KIND] [--restart] [--json]
  sandbox-ai kits restart-agent START [--json]
  sandbox-ai plugins attach START NAME@MARKETPLACE [--keep] [--restart] [--json]
  sandbox-ai plugins detach START NAME@MARKETPLACE [--restart] [--json]

A start's kits reach its sandbox as a read-only snapshot: the launcher copies their skills, subagents and commands
into the run's folder on this host (kits.stage) and mounts each kind read-only at its place in the sandbox's HOME
(kits.LAYOUT), an empty folder where the kits have none (Podman and Bubblewrap alike: a bind mount of that folder).
`kits attach` copies into those folders on this host, with the checks of a start (the kit read as a start reads it,
no symlink followed below an entry, a host folder the agent can change refused, the kits' size limits counted over
the whole snapshot, nothing hidden or special copied): the sandbox sees it at once, still read-only - the agent can
neither change nor remove it. A kit's hooks, memory and plugins are not given while it runs (hooks are part of the
agent's managed configuration, set at its start, and run only once approved); --keep adds what was attached to the
start's kits for its later starts (a kit as --kit KIT in its options; a single skill, subagent or command into the kit
start-NAME of yours, which its options name).

Plugins of the plugin store (sandbox-ai plugins): Claude Code's sandbox has the whole store mounted read-only, so a
plugin installed into it now (the store's own install, in its own sandbox) is there at once; `plugins attach` then
enables it in the sandbox HOME's Claude settings (written here, never by the agent). A plugin's hooks run only once
approved (sandbox-ai plugins claude trust-hooks): one with hooks not approved is refused where Claude Code would run
them itself. Codex and OpenCode 2 get each plugin as a mount of its own at their start: such a plugin is kept for the
start's later starts (--keep) and reaches the sandbox when it starts again (sandbox-ai resume START --restart).

Whether the agent sees an addition by itself, after a command typed in it, or only after it started again: RELOAD
(each agent's documentation or source, at the version sandbox-ai pins). `kits restart-agent` (and --restart) starts
the agent again inside the running sandbox - in its tmux window, from the script the start wrote
(/sandbox/session/agent-restart.sh: its continue flags, its conversation goes on) - through the sandbox's shell relay,
so nothing of the sandbox runs on the host; the sandbox, its network, its MCP servers and its kits stay. Never without
being asked.

Standard library only.
"""
import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

HERE = Path(__file__).resolve().parent
LIVE_FILE = "kits-live.json"  # in the run's directory: what was attached while it runs
LIVE_LOCK = "kits-live.lock"
INCOMING = ".live-incoming-"  # a copy under way, in the run's kit folder (moved into place once whole)
REMOVED = ".live-removed-"
ENTRY_NAME = r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}"
START_KIT_PREFIX = "start-"
RESTART_FILE = "agent-restart.sh"  # agent_sandbox.AGENT_RESTART_FILE, in the session directory
RESTART_TARGET = "/sandbox/session/agent-restart.sh"
KINDS = ("skills", "agents", "commands", "prompts")
AT_A_START = ("hooks", "kit")  # what a kit kept for later starts holds that only a start gives (keep_for_a_start)

# How each agent takes what arrives while it runs, per kind, at the version sandbox-ai pins (Containerfile.AGENT):
# "auto" - it re-reads by itself; "command" - after the command named (typed in the agent); "restart" - only when the
# agent starts again (sandbox-ai kits restart-agent START: the conversation continues); "sandbox" - only at the
# sandbox's start (a plugin of Codex or OpenCode 2 is a mount of its own: sandbox-ai resume START --restart).
# Sources (2026-11-18): Claude Code - code.claude.com/docs/en/skills ("watches skill directories ... picks up the
# change within the current session"), docs/en/sub-agents ("watches ~/.claude/agents/"), docs/en/discover-plugins
# (/reload-plugins); commands are skills now, not said to be watched: /reload-skills. Codex - developers.openai.com/
# codex/skills ("Codex detects skill changes automatically. If an update doesn't appear, restart Codex"); its custom
# prompts are read at its start. OpenCode 1 and 2 - anomalyco/opencode issues #27956, #20010, #34408 (skills, agents,
# commands read once per process; /new does not re-read). Hermes 0.22 - hermes-agent docs, reference/slash-commands
# (/reload-skills re-scans the skills; the conversation's skill index is built at its start: /new). DeepSeek Harness -
# its skill provider watches the existing skill roots (community docs; no official word). Qwen Code - docs/users/
# features/skills.md ("watches personal and project Skill directories"). Goose - goose-docs.ai using-skills (found
# when a session starts; aaif-goose/goose#6382: no hot reload). OpenHands - docs.openhands.dev skills/adding ("Skills
# load when a conversation starts": a new conversation; a resumed one keeps its skills). nanobot - nanobot/agent/
# context.py (the skills are read again for every message's system prompt). Letta Code - docs.letta.com skills
# (discovered on the next message). Pi - pi-mono coding-agent README (/reload: extensions, skills, prompts).
RELOAD = {
    "claude": {"skills": ("auto", None), "agents": ("auto", None), "commands": ("command", "/reload-skills"),
               "plugins": ("command", "/reload-plugins")},
    "codex": {"skills": ("auto", None), "prompts": ("restart", None), "plugins": ("sandbox", None)},
    "opencode": {"skills": ("restart", None), "agents": ("restart", None), "commands": ("restart", None)},
    "opencode2": {"skills": ("restart", None), "agents": ("restart", None), "commands": ("restart", None),
                  "plugins": ("sandbox", None)},
    "hermes": {"skills": ("command", "/reload-skills")},
    "dsh": {"skills": ("auto", None)},
    "qwen": {"skills": ("auto", None)},
    "goose": {"skills": ("restart", None)},
    "openhands": {"skills": ("command", "a new conversation")},
    "nanobot": {"skills": ("auto", None)},
    "letta": {"skills": ("auto", None)},
    "pi": {"skills": ("command", "/reload")},
}
# On a Mac (podman machine) the run's folders reach the VM through its shared folders, where a change made on the Mac
# raises no file event in the VM: an agent that watches its folders does not notice. Claude Code's own command then.
MACHINE_RELOAD = {"claude": {"skills": ("command", "/reload-skills"), "agents": ("restart", None)}}
_modules = {}
LOADING = threading.Lock()


def module(name):
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_kits_live", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def kits():
    return module("kits")


def mcp():
    return module("mcp")


def launcher():
    return mcp().launcher()


def printable(text):
    return launcher().printable(str(text))


def agent_title(agent):
    return kits().AGENT_NAMES.get(agent, agent)


# --- the running start ---------------------------------------------------------------------------------------------

def target(start):
    """(entry, record, live): the running agent start START (sandbox-ai list), its run (run.json with "directory") and
    the run's "kits_live" (its kit folders on this host, whether the plugin store is mounted). ValueError when nothing
    can be given to it while it runs."""
    starts = mcp().starts_module()
    entry = starts.find(start)
    shown = printable(entry["name"])
    if entry.get("kind") == "mcp":
        raise ValueError(f"{shown} is an MCP server's sandbox: kits and plugins are for an agent's sandbox")
    if entry.get("state") == "paused":
        raise ValueError(f"{shown} is paused: resume it first (sandbox-ai resume {shown}), then attach")
    if entry.get("state") != "running":
        raise ValueError(f"{shown} does not run ({entry.get('state')}): attach is for a running sandbox; a kit for its "
                         f"later starts: sandbox-ai resume {shown} --kit KIT")
    record = mcp().net_module().running().get(entry.get("run") or "")
    if record is None:
        raise ValueError(f"{shown}: its run is not found (has it ended?)")
    if record.get("kind") != "agent":
        raise ValueError(f"{shown} is not an agent's sandbox")
    if record.get("podman_url"):
        raise ValueError(f"{shown} runs on another machine (--podman-url): its kit folders are there, not on this "
                         "host; put it into a kit of the start and restart it")
    live = record.get("kits_live")
    staging = Path(live["staging"]) if isinstance(live, dict) and isinstance(live.get("staging"), str) else None
    session = record.get("session_dir")
    if staging is None or not isinstance(session, str) or staging != Path(session) / "kits" or \
            not isinstance(live.get("kinds"), dict):
        raise ValueError(f"{shown} was started by an older sandbox-ai, which gives nothing to a running "
                         f"sandbox, or without any kit (--no-default-kit and no --kit): restart it "
                         f"(sandbox-ai resume {shown} --restart) and attach again")
    if staging.is_symlink() or not staging.is_dir() or staging.stat().st_uid != os.getuid():
        raise ValueError(f"{shown}: its kit folder {printable(staging)} is missing, not yours or a symlink")
    return entry, record, live


@contextlib.contextmanager
def run_lock(record):
    fd = os.open(Path(record["directory"]) / LIVE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_state(record):
    try:
        data = json.loads((Path(record["directory"]) / LIVE_FILE).read_text())
    except (OSError, ValueError, RecursionError):
        data = {}
    data = data if isinstance(data, dict) else {}
    entries = [e for e in data.get("entries") or [] if isinstance(e, dict) and isinstance(e.get("name"), str)
               and e.get("kind") in KINDS + AT_A_START]
    plugins = [p for p in data.get("plugins") or [] if isinstance(p, dict) and isinstance(p.get("plugin"), str)]
    return {"entries": entries, "plugins": plugins}


def write_state(record, state):
    mcp().write_private(Path(record["directory"]) / LIVE_FILE, state)


def listing(start):
    """What was attached to the running START: {"start", "agent", "entries": [{"kind", "name", "from", "kit", "kept",
    "path"}], "plugins": [{"plugin", "kept"}], "restart": whether kits restart-agent can start its agent again}."""
    entry, record, live = target(start)
    state = read_state(record)
    for item in state["entries"]:
        item["path"] = "(its next start)" if item["kind"] in AT_A_START else \
            f"{live['kinds'].get(item['kind'], '?')}/{item['name']}"
    return {"start": entry["name"], "run": record.get("run"), "agent": live.get("agent"), **state,
            "restart": bool(live.get("restart"))}


# --- what is attached -------------------------------------------------------------------------------------------------

def checker(entry):
    """CHECK(path, label) for the host files given to the start ENTRY: the launcher's kit check with the start's own
    arguments (its workspaces and other folders the agent can change are refused, as a start refuses them), or without
    them for a run without a kept start."""
    l = launcher()
    if entry.get("directory"):
        data = mcp().starts_module().module("sessions").load(entry["id"])
        return l.kit_check(mcp().start_arguments(data))
    return l.kit_check()


def entry_name(value, what):
    name = Path(value).name
    if not re.fullmatch(ENTRY_NAME, name) or kits().bad_name(name):
        raise ValueError(f"{what} {printable(value)}: its name must be 1-128 letters, digits, '_', '.', '@', '+', '-' "
                         "(not starting with '.' or '-')")
    return name


class AtAStartOnly(ValueError):
    """A kit with nothing a running sandbox takes (no skills, subagents, commands or prompts): what it brings instead -
    hooks, a memory file, plugins - reaches a sandbox only at its start (the user 2026-11-24: the built-in kit
    hallmark-hook, only a hook, was refused with "brings no skills, agents, commands", which named neither its hook nor
    why). BRINGS: [(what, why it waits for a start)]; USABLE: whether a later start of this agent would use any of it
    (hooks the agent cannot run would not); APPROVE: the command that approves its hooks, None when approved or none."""

    def __init__(self, agent, kit, label, path, plan):
        k = kits()
        self.agent, self.kit, self.label = agent, kit, label
        self.brings, self.usable, self.approve, self.problem = [], False, None, None
        for hook_label, folder in k.kit_hook_sources(agent, [(label, path)]):
            info = k.read_hooks(agent, folder)
            if info["errors"]:
                self.problem = f"Its hooks cannot run: {info['errors'][0]}"
                self.brings.append(("hooks", self.problem))
                continue
            self.usable = True
            argument = k.trust_argument(hook_label, folder)
            if not k.hooks_trusted(agent, info["sha256"], argument):
                self.approve = f"sandbox-ai kits trust-hooks {argument}"
            self.brings.append(("hooks", "Hooks are not given while the sandbox runs: they are part of the agent's "
                                         "managed configuration, set at its start, and run only once approved"))
        if plan["memory"]:
            self.usable = True
            self.brings.append((k.MEMORY_FILE[agent][0], "The project's memory starts from it at a first start"))
        if plan["plugins"]:
            self.usable = True
            self.brings.append(("plugins", "A running sandbox gets a plugin with sandbox-ai plugins attach START "
                                           "NAME@MARKETPLACE, a start from the kit"))
        kinds = ", ".join(k.LAYOUT[agent])
        if not self.brings:
            text = (f"{label} brings nothing for {agent_title(agent)} (no {kinds}, hooks, "
                    f"{k.MEMORY_FILE[agent][0]} or plugins)")
        else:
            text = (f"{label} brings only {' and '.join(what for what, _ in self.brings)} for {agent_title(agent)} "
                    f"(no {kinds}). " + ". ".join(why for _, why in self.brings))
        super().__init__(text)

    def text(self, shown, kept=False):
        """What it says for the start SHOWN: why, and how a later start gets it - attached with --keep (KEPT: it is)
        and the sandbox started again."""
        parts = [str(self)]
        if self.usable:
            if self.approve:
                parts.append(f"Approve its hooks first: {self.approve}")
            parts.append(("Kept for its later starts (" + self.label + "): restart" if kept else
                          "To use it: attach it with --keep (it goes into the start's kits), then restart")
                         + f" the sandbox: sandbox-ai resume {shown} --restart (the conversation continues)")
        return ". ".join(parts) + "."


def wanted(agent, kit=None, skill=None, command=None, agent_file=None):
    """({kind: {name: (label, host path)}}, notes, label) of what is to be attached for AGENT."""
    k = kits()
    layout = k.LAYOUT[agent]
    notes = []
    if kit is not None:
        # "default": the default kit (every start has it - what was added to it since the start, say)
        path = k.default_home(agent) if kit == "default" and not (k.kits_dir() / kit).is_dir() \
            else k.kit_path(kit, agent)
        if not path.is_dir():
            error = ValueError(f"kit {printable(kit)} has nothing for {agent_title(agent)} (kits for it: "
                               f"{', '.join(k.available_kits(agent)) or 'none'})")
            # An empty kit would give the running sandbox nothing: what goes into it is yours (fixes.py: shown).
            f = launcher().FIXES
            error.fixes = [f.fix("kit", kit, f"the kit {kit} has nothing for {agent_title(agent)}",
                                 "make it and give it its skills, then attach it",
                                 [["sandbox-ai", "kits", "new", kit, "--agent", agent],
                                  ["sandbox-ai", "kits", "add-skill", kit, "DIR", "--agent", agent]],
                                 manual="what goes into a kit is yours to say (an empty kit gives a running sandbox "
                                        "nothing)")]
            raise error
        label = f"kit {kit}"
        plan = k.scan(agent, [(label, path)])
        notes += plan["notes"]
        if plan["memory"]:
            notes.append(f"{label}: its {k.MEMORY_FILE[agent][0]} is not given while the sandbox runs (the project's "
                         "memory starts from it at a first start)")
        if plan["plugins"]:
            notes.append(f"{label}: its plugins are not given by kits attach: sandbox-ai plugins attach START "
                         "NAME@MARKETPLACE")
        for hook_label, _ in k.kit_hook_sources(agent, [(label, path)]):
            notes.append(f"{hook_label}: its hooks are not given while the sandbox runs (they are part of the agent's "
                         "managed configuration, set at a start, and run only once approved: sandbox-ai kits "
                         "trust-hooks); --keep gives them to its later starts")
        entries = {kind: dict(items) for kind, items in plan["entries"].items() if items}
        if not entries:
            raise AtAStartOnly(agent, kit, label, path, plan)
        return entries, notes, label
    if skill is not None:
        if "skills" not in layout:
            raise ValueError(f"{agent_title(agent)} takes no skills from kits")
        path = Path(os.path.normpath(Path(skill).expanduser().absolute()))
        if not path.is_dir():
            raise ValueError(f"--skill {printable(skill)}: not a folder")
        if not (path / "SKILL.md").is_file():
            raise ValueError(f"--skill {printable(skill)}: it has no SKILL.md at its top")
        return {"skills": {entry_name(path, "--skill"): (f"--skill {path}", path)}}, notes, f"--skill {path}"
    value, option = (command, "--command") if command is not None else (agent_file, "--agent-file")
    kind = ("commands" if "commands" in layout else "prompts" if "prompts" in layout else None) \
        if command is not None else ("agents" if "agents" in layout else None)
    if kind is None:
        raise ValueError(f"{agent_title(agent)} takes no {'commands' if command is not None else 'subagents'} from "
                         f"kits (it takes: {', '.join(layout)})")
    path = Path(os.path.normpath(Path(value).expanduser().absolute()))
    if not path.is_file():
        raise ValueError(f"{option} {printable(value)}: not a file")
    name = entry_name(path, option)
    if not name.endswith(".md"):
        notes.append(f"{option} {name}: the agent reads Markdown files (.md) there; it may ignore this one")
    return {kind: {name: (f"{option} {path}", path)}}, notes, f"{option} {path}"


def used(staging, kinds):
    """{"files", "bytes"} of the run's kit folders now: the start's snapshot and what was attached since (one budget,
    kits.KIT_FILES and KIT_BYTES, as at a start). Nothing is followed."""
    budget = {"files": 0, "bytes": 0}
    for kind in kinds:
        for root, folders, files in os.walk(staging / kind, followlinks=False):
            for name in files:
                with contextlib.suppress(OSError):
                    info = os.lstat(Path(root) / name)
                    if stat.S_ISREG(info.st_mode):
                        budget["files"] += 1
                        budget["bytes"] += info.st_size
    return budget


def advice(agent, kind, on_machine=False, restart_possible=True):
    """{"reload": auto|command|restart|sandbox, "command": what to type or None, "text"}: what AGENT needs to see a
    KIND that arrived while it runs."""
    how, command = (MACHINE_RELOAD.get(agent, {}).get(kind) if on_machine else None) or \
        RELOAD.get(agent, {}).get(kind, ("restart", None))
    if kind in AT_A_START:  # (a kit kept for later starts: only the sandbox's next start gives it)
        how, command = "sandbox", None
    title = agent_title(agent)
    word = {"skills": "skill", "agents": "subagent", "commands": "command", "prompts": "prompt",
            "plugins": "plugin"}.get(kind, kind)
    if how == "auto":
        text = f"{title} takes up the new {word} by itself"
    elif how == "command":
        if command == "a new conversation":
            text = (f"{title} loads its {word}s when a conversation starts: start a new conversation in it (one "
                    "resumed keeps the skills it had)")
        else:
            text = f"{title} takes up the new {word} once you type {command} in it"
            if agent == "hermes":
                text += " (its slash commands; its skill index is the conversation's: /new, or the agent started again)"
    elif kind in AT_A_START:
        text = (f"{title} gets a kit's {'hooks' if kind == 'hooks' else 'hooks, memory file and plugins'} at the "
                "sandbox's start: restart the sandbox to use them: sandbox-ai resume START --restart (the conversation "
                "continues)")
    elif how == "sandbox":
        text = (f"{title} gets a {word} at the sandbox's start (a read-only mount of its own): it is in the start's kits "
                "now; restart the sandbox to use it: sandbox-ai resume START --restart (the conversation continues)")
    else:
        text = (f"{title} reads its {word}s when it starts: start the agent again to use it: sandbox-ai kits "
                "restart-agent START (in the running sandbox; the conversation continues)" if restart_possible else
                f"{title} reads its {word}s when it starts, and this sandbox cannot start its agent again by itself "
                "(no detached tmux session): restart the start (sandbox-ai resume START --restart)")
    return {"reload": how, "command": command if how == "command" else None, "text": text}


# --- kits attach / detach --------------------------------------------------------------------------------------------

def start_kit_name(entry):
    """The kit of yours that keeps what was attached to the start ENTRY one by one (--keep): start-NAME."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", str(entry["name"]))[:64 - len(START_KIT_PREFIX)]
    return START_KIT_PREFIX + name


def keep_option(entry, kit, add=True):
    """--kit KIT into (or out of) the saved options of the start ENTRY; True if they changed."""
    sessions = mcp().starts_module().module("sessions")
    with sessions.session_lock(entry["id"]):
        data = sessions.load(entry["id"])
        options = list(data["options"])
        given = [i for i, token in enumerate(options) if token in ("--kit", "-k") and i + 1 < len(options)
                 and options[i + 1] == kit]
        given += [i for i, token in enumerate(options) if token == f"--kit={kit}"]
        if add:
            if given:
                return False
            data["options"] = [*options, "--kit", kit]
        else:
            if not given:
                return False
            for index in sorted(given, reverse=True):
                del options[index:index + (1 if "=" in options[index] else 2)]
            data["options"] = options
        sessions.save(entry["id"], data)
    return True


def kit_changes():
    return module("kitctl").changing()


def keep_entries(entry, agent, staging, items, out):
    """Copies of the attached ITEMS ([(kind, name)], staged in STAGING) in the kit start-NAME of yours, which the start
    then names (--kit): its later starts have them. The kit's name."""
    k = kits()
    kit = start_kit_name(entry)
    root = k.kits_dir() / kit / agent
    with kit_changes():
        for part in (k.kits_dir(), k.kits_dir() / kit, root):
            if part.is_symlink():
                raise ValueError(f"{printable(part)} is a symlink; nothing kept")
        root.mkdir(mode=0o755, parents=True, exist_ok=True)
        for kind, name in items:
            folder = root / kind
            if folder.is_symlink():
                raise ValueError(f"{printable(folder)} is a symlink; nothing kept")
            folder.mkdir(mode=0o755, exist_ok=True)
            if os.path.lexists(folder / name):
                out(f"  kit {kit}: {kind[:-1]} {name} is there already; left as it is")
                continue
            notes = []
            k.copy_tree(staging / kind / name, folder / name, lambda path, label: None, f"kit {kit}",
                        {"files": 0, "bytes": 0}, notes)
    keep_option(entry, kit)
    return kit


def unkeep_entries(entry, agent, items):
    """ITEMS ([(kind, name)]) out of the kit start-NAME again; the kit out of the start's options once it is empty."""
    k = kits()
    kit = start_kit_name(entry)
    root = k.kits_dir() / kit / agent
    if root.is_symlink() or not root.is_dir():
        return
    with kit_changes():
        for kind, name in items:
            path = root / kind / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        empty = not any(any(os.scandir(child)) for child in root.iterdir()
                        if child.is_dir() and not child.is_symlink()) \
            and not any(child.is_file() for child in root.iterdir())
    if empty:
        keep_option(entry, kit, add=False)


def lint():
    """The hidden-text checker (lint.py)."""
    return module("lint")


def attach(start, kit=None, skill=None, command=None, agent_file=None, agent=None, keep=False, restart=False,
           out=print, allow_findings=False):
    """`sandbox-ai kits attach START (KIT | --skill DIR | --command FILE | --agent-file FILE)`: into the running
    sandbox of START now, read-only (see the module's text). {"start", "run", "agent", "attached": [{"kind", "name",
    "path"}], "notes", "kept", "reload": [advice per kind], "restarted", "message"}."""
    if sum(value is not None for value in (kit, skill, command, agent_file)) != 1:
        raise ValueError("name one thing to attach: KIT, --skill DIR, --command FILE or --agent-file FILE")
    entry, record, live = target(start)
    shown = printable(entry["name"])
    run_agent = live.get("agent")
    if run_agent not in kits().LAYOUT:
        raise ValueError(f"{shown}: its agent is not known")
    if agent is not None and agent != run_agent:
        raise ValueError(f"{shown} runs {agent_title(run_agent)}, not {agent_title(agent)}: kits give the sandbox's "
                         "own agent (its --with agents take theirs at a start)")
    if keep and not entry.get("directory"):
        raise ValueError(f"{shown} is a run without a kept start: --keep has no options to keep it in")
    if restart and not live.get("restart"):
        raise ValueError(f"{shown} cannot start its agent again inside the sandbox (no detached tmux session: a task, "
                         "--no-tmux or a run started without a terminal)")
    try:
        entries, notes, label = wanted(run_agent, kit, skill, command, agent_file)
    except AtAStartOnly as only:
        if not (keep and only.usable):
            raise ValueError(only.text(shown)) from None
        return keep_for_a_start(entry, record, live, only, out)
    # What the agent would read at once: through the hidden-text checker first (as kits add-skill and host-import).
    findings = [dict(item, file=f"{kind}/{name}/{item['file']}" if item["file"] else f"{kind}/{name}")
                for kind, items in entries.items() for name, (_, path) in items.items()
                for item in lint().check_tree(path)]
    if findings and not allow_findings:
        raise ValueError(lint().refusal(label, findings))
    if findings:
        notes.append(f"{label}: given with hidden-text findings (--allow-findings): {lint().summary(findings)}")
    staging = Path(live["staging"])
    missing = [kind for kind in entries if kind not in live["kinds"] or not (staging / kind).is_dir()
               or (staging / kind).is_symlink()]
    if missing:
        raise ValueError(f"{shown} has no read-only folder for {', '.join(missing)} (it was started without any kit "
                         f"for them): put it into a kit of the start and restart it (sandbox-ai resume {shown} --kit "
                         "KIT --restart)")
    check = checker(entry)
    added = []
    with run_lock(record):
        state = read_state(record)
        for kind, items in entries.items():
            for name in list(items):
                if not os.path.lexists(staging / kind / name):
                    continue
                if kit is None:
                    raise ValueError(f"{shown} has a {kind[:-1]} {printable(name)} already (from its kits or attached "
                                     "before); detach it first, or rename one")
                # A kit's entry the sandbox has (its start's kits gave it, or an attach before): left as it is, never
                # replaced; the kit's others are given.
                notes.append(f"{label}: {kind[:-1]} {name} is in the sandbox already; left as it is")
                del items[name]
        if not any(entries.values()):
            raise ValueError(f"{shown} has everything {printable(label)} brings already")
        budget = used(staging, live["kinds"])
        incoming = Path(tempfile.mkdtemp(prefix=INCOMING, dir=staging))
        try:
            os.chmod(incoming, 0o755)
            for kind, items in entries.items():
                (incoming / kind).mkdir(mode=0o755)
                for name, (source_label, path) in items.items():
                    kits().copy_tree(path, incoming / kind / name, check, source_label, budget, notes)
            # Whole first, then each moved into the read-only folder in one step: the agent never sees half of one.
            for kind, items in entries.items():
                for name in items:
                    if os.path.lexists(incoming / kind / name):
                        os.rename(incoming / kind / name, staging / kind / name)
                        added.append((kind, name))
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
        if not added:
            raise ValueError(f"nothing of {printable(label)} could be copied: " + "; ".join(
                printable(note) for note in notes[-3:]))
        state["entries"] = [e for e in state["entries"] if (e["kind"], e["name"]) not in added] + [
            {"kind": kind, "name": name, "from": label, "kit": kit, "kept": None} for kind, name in added]
        write_state(record, state)
    kept = None
    if keep:
        try:
            if kit is not None:
                keep_option(entry, kit)
                kept = f"kit {kit}"
            else:
                kept = f"kit {keep_entries(entry, run_agent, staging, added, out)}"
        except (OSError, ValueError) as exc:
            out(f"  not kept for its later starts: {printable(exc)}")
        if kept:
            with run_lock(record):
                state = read_state(record)
                for item in state["entries"]:
                    if (item["kind"], item["name"]) in added:
                        item["kept"] = kept
                write_state(record, state)
    on_machine = bool(record.get("container"))
    reloads = [{"kind": kind, **advice(run_agent, kind, on_machine, bool(live.get("restart")))}
               for kind in dict.fromkeys(kind for kind, _ in added)]
    result = {"start": entry["name"], "run": record.get("run"), "agent": run_agent,
              "attached": [{"kind": kind, "name": name, "path": f"{live['kinds'][kind]}/{name}"} for kind, name in added],
              "notes": [printable(note) for note in notes], "kept": kept, "reload": reloads, "restarted": False}
    if restart:
        restart_agent(start, out=out, target_found=(entry, record, live))
        result["restarted"] = True
    names = ", ".join(f"{kind[:-1]} {name}" for kind, name in added)
    result["message"] = (f"{shown} has the {names} now, read-only at "
                         f"{', '.join(dict.fromkeys(live['kinds'][kind] for kind, _ in added))}"
                         f"{', kept for its later starts (' + kept + ')' if kept else ''}. "
                         + ("The agent was started again (its conversation continues)." if result["restarted"] else
                            " ".join(item["text"].replace("START", shown) + "." for item in reloads)))
    for note in notes:
        out(f"  {printable(note)}")
    mcp().LOG.info("kits", f"attach {label} to {entry['name']}: {names}" + (f"; kept ({kept})" if kept else ""))
    out(result["message"])
    return result


def keep_for_a_start(entry, record, live, only, out=print):
    """--keep of a kit that brings only what a start gives (AtAStartOnly: hooks, a memory file, plugins): nothing for
    the running sandbox, the kit into the start's options; recorded as attached (kind "hooks" for its hooks, else
    "kit"), so that kits detach takes it out of the later starts again. The result of attach."""
    shown, kit = printable(entry["name"]), only.kit
    keep_option(entry, kit)
    kept = f"kit {kit}"
    kind = "hooks" if any(what == "hooks" for what, _ in only.brings) else "kit"
    with run_lock(record):
        state = read_state(record)
        state["entries"] = [e for e in state["entries"] if not (e["kind"] == kind and e["name"] == kit)] + [
            {"kind": kind, "name": kit, "from": only.label, "kit": kit, "kept": kept}]
        write_state(record, state)
    reload = {"kind": kind, **advice(only.agent, kind)}
    message = only.text(shown, kept=True)
    mcp().LOG.info("kits", f"attach {only.label} to {entry['name']}: kept for its later starts only "
                           f"({', '.join(what for what, _ in only.brings)})")
    out(message)
    return {"start": entry["name"], "run": record.get("run"), "agent": only.agent, "attached": [], "notes": [],
            "kept": kept, "reload": [reload], "restarted": False, "message": message}


def detach(start, name, kind=None, restart=False, out=print):
    """`sandbox-ai kits detach START NAME [--kind KIND]`: what `kits attach` gave the running START (an entry by its
    name, or everything a kit NAME gave) out of its sandbox now; out of its later starts too where it was kept.
    {"start", "run", "agent", "detached": [{"kind", "name"}], "kept_removed", "reload", "restarted", "message"}."""
    entry, record, live = target(start)
    shown = printable(entry["name"])
    if kind is not None and kind not in KINDS:
        raise ValueError(f"--kind is one of {', '.join(KINDS)}")
    if restart and not live.get("restart"):
        raise ValueError(f"{shown} cannot start its agent again inside the sandbox (no detached tmux session)")
    staging = Path(live["staging"])
    agent = live.get("agent")
    with run_lock(record):
        state = read_state(record)
        chosen = [e for e in state["entries"] if (e["name"] == name or e.get("kit") == name)
                  and (kind is None or e["kind"] == kind)]
        if not chosen:
            there = [k for k in live["kinds"] if os.path.lexists(staging / k / name)]
            if there:
                raise ValueError(f"{shown}: the {there[0][:-1]} {printable(name)} comes from the kits of its start, "
                                 "not from kits attach: it stays while the sandbox runs (take it out of the kit for "
                                 "its later starts)")
            raise ValueError(f"{shown}: nothing named {printable(name)} was attached to it")
        if len({e["kind"] for e in chosen if e["name"] == name}) > 1 and not any(e.get("kit") == name for e in chosen):
            raise ValueError(f"{shown}: {printable(name)} is a {' and a '.join(e['kind'][:-1] for e in chosen)}: "
                             "choose one with --kind")
        trash = Path(tempfile.mkdtemp(prefix=REMOVED, dir=staging))
        try:
            for item in chosen:
                path = staging / item["kind"] / item["name"]
                if os.path.lexists(path):
                    os.rename(path, trash / f"{item['kind']}-{item['name']}")  # gone from the sandbox in one step
        finally:
            shutil.rmtree(trash, ignore_errors=True)
        state["entries"] = [e for e in state["entries"] if e not in chosen]
        write_state(record, state)
    kept_removed = False
    kept_kits = {e.get("kit") for e in chosen if e.get("kept") and e.get("kit")}
    loose = [(e["kind"], e["name"]) for e in chosen if e.get("kept") and not e.get("kit")]
    try:
        for kit in kept_kits:
            kept_removed = keep_option(entry, kit, add=False) or kept_removed
        if loose:
            unkeep_entries(entry, agent, loose)
            kept_removed = True
    except (OSError, ValueError) as exc:
        out(f"  still in its kits for later starts: {printable(exc)}")
    on_machine = bool(record.get("container"))
    reloads = []
    for one in dict.fromkeys(e["kind"] for e in chosen):
        how = advice(agent, one, on_machine, bool(live.get("restart")))
        reloads.append({"kind": one, **how})
    result = {"start": entry["name"], "run": record.get("run"), "agent": agent,
              "detached": [{"kind": e["kind"], "name": e["name"]} for e in chosen], "kept_removed": kept_removed,
              "reload": reloads, "restarted": False}
    if restart:
        restart_agent(start, out=out, target_found=(entry, record, live))
        result["restarted"] = True
    names = ", ".join(f"hooks of kit {e['name']}" if e["kind"] == "hooks" else f"kit {e['name']}"
                      if e["kind"] == "kit" else f"{e['kind'][:-1]} {e['name']}" for e in chosen)
    pending = [item for item in reloads if item["reload"] != "auto"]
    if all(e["kind"] in AT_A_START for e in chosen):  # (only kept for later starts: keep_for_a_start)
        result["message"] = f"The later starts of {shown} no longer get the {names}."
    else:
        result["message"] = (f"{shown} no longer has the {names}{' (nor its later starts)' if kept_removed else ''}."
                             + (" The agent was started again (its conversation continues)." if result["restarted"]
                                else " An agent that read it already may still name it until it reads its folders "
                                     "again" + (f" ({', '.join(i['command'] or 'a restart' for i in pending)})."
                                                if pending else ".")))
    mcp().LOG.info("kits", f"detach {name} from {entry['name']}: {names}")
    out(result["message"])
    return result


# --- the agent started again inside the running sandbox ---------------------------------------------------------------

def shell_runner(key, argv):
    """The exit status of ARGV run in the start KEY's sandbox through its shell relay (sessions.shell: inside the
    agent's confinement, nothing of the sandbox on the host)."""
    return mcp().starts_module().module("sessions").shell(key, argv, tty=False)


def restart_agent(start, out=print, target_found=None):
    """`sandbox-ai kits restart-agent START`: the agent of the running START started again in its tmux window - tmux
    respawn-window with the start's restart script - inside the sandbox, which goes on: its network, MCP servers, kits
    and attached parts stay, the conversation continues (the agent's continue flags). {"start", "run", "agent",
    "restarted", "message"}."""
    entry, record, live = target_found or target(start)
    shown = printable(entry["name"])
    if not live.get("restart") or not entry.get("id") or not entry.get("directory"):
        raise ValueError(f"{shown} cannot start its agent again inside the sandbox (no detached tmux session: a task, "
                         f"--no-tmux or a run without a kept start); stop it and start it anew")
    script = Path(record["session_dir"]) / RESTART_FILE
    if script.is_symlink() or not script.is_file():
        raise ValueError(f"{shown} has no restart script (started by an older sandbox-ai): sandbox-ai resume {shown} "
                         "--restart")
    status = shell_runner(entry["id"], ["tmux", "respawn-window", "-k", "-t", "sandbox:agent", "-c", "/work",
                                        f"sh {RESTART_TARGET}"])
    if status != 0:
        raise ValueError(f"{shown}: the agent was not started again (tmux respawn-window: exit status {status}); "
                         f"restart the start instead: sandbox-ai resume {shown} --restart")
    agent = live.get("agent")
    message = (f"{shown}: {agent_title(agent)} started again in its tmux window; its conversation continues "
               "(the sandbox, its network, MCP servers and kits stay)")
    mcp().LOG.info("kits", f"restart-agent {entry['name']}")
    out(message)
    return {"start": entry["name"], "run": record.get("run"), "agent": agent, "restarted": True, "message": message}


# --- plugins ----------------------------------------------------------------------------------------------------------

def plugins_module():
    return module("plugins")


def marketplace_source(marketplace):
    """The source of MARKETPLACE: as the kits declare it, else as the plugin store knows it; None if neither does."""
    declared, _, _ = plugins_module().store_declarations("all")
    if marketplace in declared:
        return declared[marketplace]
    known, _ = kits().installed("claude")
    source = (known.get(marketplace) or {}).get("source")
    return source if isinstance(source, dict) else None


def install(plugin, runner=None, out=print):
    """PLUGIN into the plugin store, by the store's own install (`claude plugin install` in the store's sandbox, with
    network access to the marketplace's hosts only) unless it is there. True when it was installed now. What the
    store's command prints goes to stderr: stdout is the caller's (`plugins attach --json` prints its result there)."""
    k, p = kits(), plugins_module()
    if runner is None:
        def runner(command):
            sys.stdout.flush()
            return subprocess.run(command, stdout=sys.stderr.fileno())
    _, installed = k.installed("claude")
    if plugin in installed:
        return False
    marketplace = plugin.split("@")[1]
    source = marketplace_source(marketplace)
    if source is None:
        raise ValueError(f"the marketplace {printable(marketplace)} is not known: declare it in a kit (sandbox-ai kits "
                         f"add-marketplace KIT {printable(marketplace)} SOURCE), then attach again")
    known, _ = k.installed("claude")
    if marketplace not in known:
        out(f"sandbox-ai plugins: adding marketplace {marketplace} ({k.source_argument(source)})")
        p.run_in_store(["marketplace", "add", k.source_argument(source)], (), [source], runner)
    out(f"sandbox-ai plugins: installing {plugin}")
    p.run_in_store(["install", plugin], (), p.plugin_sources(plugin, source), runner)
    _, installed = k.installed("claude")
    if plugin not in installed:
        raise ValueError(f"{printable(plugin)} was not installed into the plugin store (see what sandbox-ai plugins "
                         "said above)")
    return True


def brings(plugin):
    """What PLUGIN's installed folder brings, by its parts (for the review before it is enabled)."""
    k = kits()
    _, installed = k.installed("claude")
    where = k.plugin_install(plugin, installed.get(plugin))
    if where is None:
        return []
    root = where[0]
    parts = [name for name in ("skills", "agents", "commands", "hooks", "output-styles") if (root / name).is_dir()
             and not (root / name).is_symlink()]
    parts += ["MCP servers (.mcp.json)"] if (root / ".mcp.json").is_file() else []
    return parts


def claude_enable(record, plugin, enable=True):
    """PLUGIN enabled (or off again) in the running Claude sandbox's HOME: its copies of the store's plugin index and
    marketplaces (as the start wrote them) and enabledPlugins in ~/.claude/settings.json - written here, through the
    launcher's checks (nothing followed through a symlink the agent may have put there), never by the agent."""
    l, k = launcher(), kits()
    home = Path(record.get("agent_home") or "")
    if not record.get("agent_home") or not home.is_dir() or home.is_symlink():
        raise ValueError("the sandbox's HOME is not found on this host")
    known, installed = k.installed("claude")
    if enable:
        known = {name: {**entry, "autoUpdate": False} for name, entry in known.items()}
        l.replace_private(json.dumps(known, indent=2).encode(), home, ".claude/plugins/known_marketplaces.json")
        l.replace_private(json.dumps({"version": 2, "plugins": installed}, indent=2).encode(), home,
                          ".claude/plugins/installed_plugins.json")

    def apply(document):
        enabled = document.get("enabledPlugins")
        enabled = enabled if isinstance(enabled, dict) else {}
        if enabled.get(plugin) is enable:
            return False
        enabled[plugin] = enable
        document["enabledPlugins"] = enabled
        return True
    problem = l.update_json_defaults(home, ".claude/settings.json", apply)
    if problem:
        raise ValueError(f"~/.claude/settings.json in the sandbox was left as it is ({problem})")


def keep_plugin(entry, agent, plugin, add=True):
    """PLUGIN into (or out of) the kit start-NAME of yours for AGENT (its marketplace declared there with the source
    known), the kit named in the start's options. The kit's name."""
    kitctl = module("kitctl")
    kit = start_kit_name(entry)
    said = []
    if add:
        if not (kits().kits_dir() / kit / agent).is_dir():
            if kitctl.main(["new", kit, "--agent", agent], out=said.append) != 0:
                raise ValueError(f"kit {kit} could not be made")
        source = marketplace_source(plugin.split("@")[1])
        args = ["add-plugin", kit, plugin, "--agent", agent]
        if source is not None and plugin.split("@")[1] != kitctl.MARKETPLACE_NAME:
            args += ["--source", kitctl.source_value(source)]
        if kitctl.main(args, out=said.append) != 0:
            raise ValueError(f"{plugin} could not be put into kit {kit}")
        keep_option(entry, kit)
    elif (kits().kits_dir() / kit / agent).is_dir():
        kitctl.main(["remove-plugin", kit, plugin, "--agent", agent], out=said.append)
    return kit


def plugin_attach(start, plugin, keep=False, restart=False, runner=None, out=print, allow_findings=False):
    """`sandbox-ai plugins attach START NAME@MARKETPLACE`: the plugin installed into the store if it is not, reviewed
    (what it brings, its hooks' approval), and enabled in the running sandbox (Claude Code); for Codex and OpenCode 2
    kept for the start's next start. {"start", "run", "agent", "plugin", "installed_now", "brings", "hooks", "attached":
    "live" | "next start", "kept", "reload", "restarted", "message"}."""
    k = kits()
    if not re.fullmatch(f"{k.PLUGIN_NAME}@{k.PLUGIN_NAME}", str(plugin)):
        raise ValueError(f"{printable(plugin)!r}: a plugin is NAME@MARKETPLACE")
    entry, record, live = target(start)
    shown = printable(entry["name"])
    agent = live.get("agent")
    if agent == "opencode":
        raise ValueError(f"OpenCode loads its npm plugins at its start: put it into a kit of the start (sandbox-ai kits "
                         f"add-plugin KIT PACKAGE --agent opencode) and restart it (sandbox-ai resume {shown} "
                         "--restart)")
    if agent not in k.STORE_AGENTS:
        raise ValueError(f"{agent_title(agent)} takes no plugins of the plugin store (Claude Code, Codex and OpenCode "
                         "2 do)")
    if agent != "claude" and not keep:
        raise ValueError(f"{agent_title(agent)} gets a plugin only at its sandbox's start (a read-only mount of its "
                         f"own): add --keep (it is kept for its later starts), then restart it: sandbox-ai resume "
                         f"{shown} --restart")
    if keep and not entry.get("directory"):
        raise ValueError(f"{shown} is a run without a kept start: --keep has no options to keep it in")
    if agent == "claude" and not live.get("plugin_store"):
        raise ValueError(f"{shown} started without the plugin store (it held nothing then, or --no-plugins): keep the "
                         f"plugin for its later starts (--keep) and restart it (sandbox-ai resume {shown} --restart)")
    if restart and (agent != "claude" or not live.get("restart")):
        raise ValueError(f"{shown}: --restart starts the agent again inside the sandbox; "
                         + ("Codex and OpenCode 2 need the sandbox's restart for a plugin: sandbox-ai resume "
                            f"{shown} --restart" if agent != "claude" else "it has no detached tmux session"))
    installed_now = install(plugin, runner, out)
    _, installed = k.installed("claude")
    found = plugins_module().plugin_findings(plugin, installed.get(plugin))
    if found and not allow_findings:
        raise ValueError(lint().refusal(f"{plugin} (in the plugin store now; not enabled)", found))
    parts = brings(plugin)
    hooks = None
    if agent == "claude":
        staged = k.stage_plugin_hooks([plugin], installed)
        if staged:
            info = staged[0]
            hooks = {"count": len(info["hooks"]), "trusted": bool(info["trusted"]), "errors": info["errors"][:3]}
            if not info["trusted"] and not live.get("managed_hooks"):
                raise ValueError(f"{printable(plugin)} brings {len(info['hooks'])} hook(s) not approved, and this "
                                 "sandbox's Claude Code runs a plugin's hooks itself (no managed hooks this run): review "
                                 f"and approve them first (sandbox-ai plugins claude trust-hooks {printable(plugin)}), "
                                 "then attach again")
    else:
        staged = k.stage_store_plugins(agent, [plugin], installed)
        if staged and staged[0]["errors"]:
            raise ValueError(f"{printable(plugin)} is not for {agent_title(agent)}: {printable(staged[0]['errors'][0])}")
        if staged:
            hooks = {"count": len(staged[0].get("hooks") or []), "trusted": bool(staged[0]["trusted"]),
                     "errors": []}
    notes = []
    if hooks and hooks["count"]:
        if agent == "claude" and live.get("managed_hooks"):
            notes.append(f"its {hooks['count']} hook(s) do not run in this sandbox while it runs (its hooks are managed "
                         "ones, set at its start)" + ("; approved, they run from its next start" if hooks["trusted"]
                                                      else "; they run only once approved: sandbox-ai plugins claude "
                                                           f"trust-hooks {plugin}"))
        elif agent != "claude" and not hooks["trusted"]:
            notes.append(f"not approved yet: it runs (OpenCode 2) or its hooks run (Codex) only once approved: "
                         f"sandbox-ai plugins {agent} trust-hooks {plugin}")
        elif agent == "claude":
            notes.append(f"its {hooks['count']} approved hook(s) run")
    attached = "next start"
    if agent == "claude":
        with run_lock(record):
            claude_enable(record, plugin)
            state = read_state(record)
            state["plugins"] = [p for p in state["plugins"] if p["plugin"] != plugin] + [{"plugin": plugin,
                                                                                          "kept": None}]
            write_state(record, state)
        attached = "live"
    kept = None
    if keep:
        try:
            kept = f"kit {keep_plugin(entry, agent, plugin)}"
        except (OSError, ValueError) as exc:
            if agent != "claude":
                raise
            out(f"  not kept for its later starts: {printable(exc)}")
        if kept and agent == "claude":
            with run_lock(record):
                state = read_state(record)
                for item in state["plugins"]:
                    if item["plugin"] == plugin:
                        item["kept"] = kept
                write_state(record, state)
    reload = {"kind": "plugins", **advice(agent, "plugins", bool(record.get("container")), bool(live.get("restart")))}
    result = {"start": entry["name"], "run": record.get("run"), "agent": agent, "plugin": plugin,
              "installed_now": installed_now, "brings": parts, "hooks": hooks, "attached": attached, "kept": kept,
              "notes": notes, "reload": [reload], "restarted": False}
    if restart:
        restart_agent(start, out=out, target_found=(entry, record, live))
        result["restarted"] = True
    what = f" ({', '.join(parts)})" if parts else ""
    if attached == "live":
        result["message"] = (f"{shown}: the plugin {plugin}{what} is enabled in its Claude Code"
                             f"{', kept for its later starts (' + kept + ')' if kept else ''}. "
                             + ("The agent was started again (its conversation continues)." if result["restarted"]
                                else reload["text"].replace("START", shown) + "."))
    else:
        result["message"] = (f"{shown}: the plugin {plugin}{what} is kept for its later starts ({kept}). "
                             + reload["text"].replace("START", shown) + ".")
    for note in notes:
        out(f"  {plugin}: {printable(note)}")
    mcp().LOG.info("kits", f"plugins attach {plugin} to {entry['name']}: {attached}")
    out(result["message"])
    return result


def plugin_detach(start, plugin, restart=False, out=print):
    """`sandbox-ai plugins detach START NAME@MARKETPLACE`: a plugin plugins attach gave the running START off again in
    its sandbox (Claude Code) and out of the kit start-NAME (its later starts). {"start", "run", "agent", "plugin",
    "detached": "live" | "next start" | "not attached", "reload", "restarted", "message"}."""
    k = kits()
    if not re.fullmatch(f"{k.PLUGIN_NAME}@{k.PLUGIN_NAME}", str(plugin)):
        raise ValueError(f"{printable(plugin)!r}: a plugin is NAME@MARKETPLACE")
    entry, record, live = target(start)
    shown = printable(entry["name"])
    agent = live.get("agent")
    if restart and (agent != "claude" or not live.get("restart")):
        raise ValueError(f"{shown}: --restart starts the agent again inside the sandbox; it cannot here")
    detached = "not attached"
    if agent == "claude":
        with run_lock(record):
            state = read_state(record)
            if any(p["plugin"] == plugin for p in state["plugins"]):
                claude_enable(record, plugin, enable=False)
                state["plugins"] = [p for p in state["plugins"] if p["plugin"] != plugin]
                write_state(record, state)
                detached = "live"
    if entry.get("directory") and (kits().kits_dir() / start_kit_name(entry) / agent).is_dir():
        declared = k.declarations(agent, [("kit", kits().kits_dir() / start_kit_name(entry) / agent / k.PLUGINS_FILE)],
                                  with_disabled=True)[1] \
            if (kits().kits_dir() / start_kit_name(entry) / agent / k.PLUGINS_FILE).is_file() else []
        if plugin in declared:
            keep_plugin(entry, agent, plugin, add=False)
            detached = "live" if detached == "live" else "next start"
    if detached == "not attached":
        raise ValueError(f"{shown}: the plugin {printable(plugin)} was not attached to it (a plugin of its own kits "
                         "goes with the kit: sandbox-ai kits disable-plugin)")
    reload = {"kind": "plugins", **advice(agent, "plugins", bool(record.get("container")), bool(live.get("restart")))}
    result = {"start": entry["name"], "run": record.get("run"), "agent": agent, "plugin": plugin,
              "detached": detached, "reload": [reload], "restarted": False}
    if restart:
        restart_agent(start, out=out, target_found=(entry, record, live))
        result["restarted"] = True
    result["message"] = (f"{shown}: the plugin {plugin} is off" + (" in its Claude Code now" if detached == "live"
                                                                     else " from its next start")
                         + ("; the agent was started again." if result["restarted"] else
                            f" ({reload['command']} in it takes it out of the running agent)." if reload["command"]
                            else "."))
    mcp().LOG.info("kits", f"plugins detach {plugin} from {entry['name']}: {detached}")
    out(result["message"])
    return result


# --- command lines ------------------------------------------------------------------------------------------------------

def plugins_parser():
    p = argparse.ArgumentParser(prog="sandbox-ai plugins", allow_abbrev=False,
                                description="A plugin of the plugin store for a sandbox while it runs")
    commands = p.add_subparsers(dest="command", metavar="COMMAND", required=True)
    attach_ = commands.add_parser("attach", help="install (if needed), review and enable a plugin in a running "
                                                 "sandbox (Claude Code); Codex and OpenCode 2: kept for its next start")
    attach_.add_argument("start", metavar="START", help="the running start (sandbox-ai list)")
    attach_.add_argument("plugin", metavar="NAME@MARKETPLACE")
    attach_.add_argument("--keep", action="store_true", help="also keep it for the start's later starts (the kit "
                                                             "start-NAME)")
    attach_.add_argument("--restart", action="store_true", help="then start the agent again inside the sandbox (its "
                                                                "conversation continues)")
    attach_.add_argument("--allow-findings", action="store_true",
                         help="give it even with findings of the hidden-text checker (invisible or bidi characters, "
                              "instruction phrasing aimed at the model): by default such a plugin is refused")
    attach_.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    detach_ = commands.add_parser("detach", help="a plugin plugins attach gave off again")
    detach_.add_argument("start", metavar="START")
    detach_.add_argument("plugin", metavar="NAME@MARKETPLACE")
    detach_.add_argument("--restart", action="store_true", help="then start the agent again inside the sandbox")
    detach_.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    return p


def run_cli(work, json_out):
    """Run WORK(out) (a function of this module); its result as JSON with JSON_OUT (the lines it says go to the error
    output then), else its lines. The exit status."""
    out = (lambda line: print(line, file=sys.stderr)) if json_out else print
    try:
        try:
            result = work(out)
        except ValueError as exc:
            if not getattr(exc, "fixes", None):
                raise
            # What it lacks, with what makes it (fixes.py): offered on a terminal, as a start does - else (or only
            # yours to do) refused with the commands; done, it is tried again once.
            launcher().offer_fixes(exc, about="this command", again="run it again")
            result = work(out)
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai: {printable(exc)}", file=sys.stderr)
        return 1
    if json_out:
        print(json.dumps(result, indent=2))
    return 0


def plugins_cli(argv):
    a = plugins_parser().parse_args(argv)
    if a.command == "attach":
        return run_cli(lambda out: plugin_attach(a.start, a.plugin, a.keep, a.restart, out=out,
                                                 allow_findings=a.allow_findings), a.json)
    return run_cli(lambda out: plugin_detach(a.start, a.plugin, a.restart, out=out), a.json)
