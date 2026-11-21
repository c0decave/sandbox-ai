#!/usr/bin/env python3
"""A team of agents: one sandbox per agent on a shared workspace, started and stopped together, with a task board
between them (`sandbox-ai team`).

  sandbox-ai team start NAME --agents claude,codex[,...] [-w DIR] [--vpn PROFILE] [--member-vpn AGENT=PROFILE]
                        [--allow-shared-openvpn] [--role AGENT=TEXT] [--member-model AGENT=MODEL]
                        [--member-llm AGENT=SERVER[:MODEL]] [--member-mode AGENT=MODE] [--dry-run [--json]]
                        [-- LAUNCHER OPTIONS]
  sandbox-ai team list | show NAME | post NAME AGENT TASK | stop NAME | delete NAME --yes

Each member is a named session (NAME-AGENT: attach, pause, resume, stop like any start). The first agent leads:
it starts with a prompt that tells it about its team. The others start with a prompt to wait for tasks on the
board, do them and report back. The board is the MCP server team-NAME (team_board.py) in a sandbox of its own,
without network; every member reaches it through its relay, so mcp rules, questions and mcp-calls.jsonl hold.
Its file lies in the team's directory (TEAMS/NAME/board); `team post` writes there from the host.

Members: any of AGENTS (every agent of sandbox-ai), each started with its first prompt in its own form (prompt_args:
interactive where the agent has a first prompt there; OpenHands, nanobot and Letta Code run it as one long task that
waits for the board's tasks in it) and the board as its --mcp server.

Per member: --role AGENT=TEXT adds its role to its first prompt (what it is there for: "reviews every change"); its
own model, LLM server and security mode (--member-model, --member-llm, --member-mode) take the place of the --model,
--llm and --mode of the options after -- for that member only. The record keeps them; `team show --json` shows them.

--dry-run checks a start without starting anything: every check of team start, then each member's start as the
launcher's dry run (without the board, which a dry run does not make); --json prints the plan or the refusal as JSON
(the app's Board and the web interface ask so). Nothing is written.

A VPN: --vpn PROFILE (or --vpn in the options after --) puts it in front of every member, --member-vpn AGENT=PROFILE
gives one member another. Each member is a sandbox of its own with its own VPN container, so one profile in several
members means several tunnels of it at once: Tor is fine (each member its own Tor, said); WireGuard - Mullvad's too -
is refused (the same key twice gets the same tunnel address, and the server sends to whichever spoke last), with
what to do instead; OpenVPN only with --allow-shared-openvpn (many servers end the first connection of an account or
certificate when it connects twice); a persistent Tailscale node runs once (an --ephemeral profile in parallel). A
profile another sandbox holds already is refused before the first member starts (vpn.profile_holder). The team's
record keeps the choice; each member is a session that keeps its own --vpn when it is resumed or restarted.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
AGENTS = ("claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "opencode2",
          "pi")
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")  # NAME-opencode stays a session name; team-NAME an MCP name
# A member's own launcher options (--member-model, --member-llm, --member-mode): each replaces the team's for it.
MEMBER_OPTIONS = {"--member-model": "--model", "--member-llm": "--llm", "--member-mode": "--mode"}
MEMBER_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+,=-]{0,199}")  # a model, SERVER[:MODEL[,...]] or a mode
ROLE_LIMIT = 1000  # characters of a member's role (one line, added to its first prompt)
BOARD_SCRIPT = HERE / "team_board.py"
BOARD_TARGET = "/opt/sandbox-ai/team_board.py"


def teams_dir():
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "sandbox-ai" / "teams"


def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_loaded = {}


def cached_module(name, file):
    """load_module, once per run (the launcher is large: loaded again for every member, a check took seconds)."""
    if name not in _loaded:
        _loaded[name] = load_module(name, file)
    return _loaded[name]


def board_module():
    return load_module("team_board_for_team", "team_board.py")


_vpn = None


def vpn_module():
    global _vpn
    if _vpn is None:
        _vpn = load_module("vpn_for_team", "vpn.py")
    return _vpn


_launcher = None


def printable(text):
    """TEXT safe for a terminal: the launcher's printable (control, bidi and other non-printable characters
    escaped), one implementation for every command."""
    global _launcher
    if _launcher is None:
        _launcher = load_module("agent_sandbox_for_team_text", "agent_sandbox.py")
    return _launcher.printable(str(text))


def check_name(name):
    if not isinstance(name, str) or not TEAM_NAME.fullmatch(name):
        raise ValueError("a team's name: 1-20 of a-z, 0-9, '_' and '-', starting with a letter or digit")
    return name


def server_name(name):
    return f"team-{check_name(name)}"


def member_name(name, agent):
    return f"{check_name(name)}-{agent}"


def record_path(name):
    return teams_dir() / check_name(name) / "team.json"


def load(name):
    try:
        data = json.loads(record_path(name).read_text())
    except FileNotFoundError:
        raise ValueError(f"no team {name}; see: sandbox-ai team list") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"the record of team {name} cannot be read: {exc}") from None
    members = data.get("members") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("name") != name or not isinstance(members, list) or len(members) < 2 \
            or any(m not in AGENTS for m in members) or len(set(members)) != len(members) \
            or data.get("lead") not in members or not isinstance(data.get("workspace"), str) \
            or not isinstance(data.get("options"), list) or not all(isinstance(o, str) for o in data["options"]) \
            or not isinstance(data.get("vpn"), (str, type(None))) \
            or not isinstance(data.get("member_vpn", {}), dict) \
            or any(k not in members or not isinstance(v, str) for k, v in data.get("member_vpn", {}).items()) \
            or not isinstance(data.get("roles", {}), dict) \
            or any(k not in members or not isinstance(v, str) for k, v in data.get("roles", {}).items()) \
            or not isinstance(data.get("member_options", {}), dict) \
            or any(k not in members or not isinstance(v, dict) or any(
                o not in MEMBER_OPTIONS.values() or not isinstance(x, str) for o, x in v.items())
                for k, v in data.get("member_options", {}).items()):
        raise ValueError(f"the record of team {name} is damaged: {record_path(name)}")
    return data


def member_vpns(data):
    """{agent: the VPN profile in front of it, or None} of team DATA: --member-vpn's, else --vpn's."""
    own = data.get("member_vpn") or {}
    return {agent: own.get(agent) or data.get("vpn") for agent in data["members"]}


def names():
    """The teams: each folder with a record - and one a start left without it (killed before it wrote it): listed
    as damaged, so that `team delete NAME --yes` is named for it."""
    directory = teams_dir()
    try:
        return sorted(p.name for p in directory.iterdir() if TEAM_NAME.fullmatch(p.name)
                      and ((p / "team.json").is_file() or (p / "board").is_dir())) if directory.is_dir() else []
    except OSError:
        return []


def write_record(name, data):
    """Team NAME's record, written whole or not at all (0600 from the start; a full disk leaves no half record)."""
    path = record_path(name)
    temporary = path.with_name(f".team.json.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def definition(data):
    """The MCP server definition of the team's board (mcp.py reads it for team-NAME)."""
    return {"command": ["python3", "-I", BOARD_TARGET, "--members", ",".join(data["members"])],
            "options": ["--offline", "--mount", f"{BOARD_SCRIPT}:{BOARD_TARGET}:ro"],
            "workspace": str(teams_dir() / data["name"] / "board"),
            "description": f"the task board of team {data['name']}: {', '.join(data['members'])}"}


def definitions():
    """{team-NAME: definition} of every team, for mcp.definitions."""
    found = {}
    for name in names():
        try:  # a damaged team never stops another start
            found[server_name(name)] = definition(load(name))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return found


def role_text(data, agent):
    """" Your role in the team: ROLE" of AGENT in team DATA (--role), or ""."""
    role = (data.get("roles") or {}).get(agent)
    return f" Your role in the team: {role}" if role else ""


def lead_prompt(data):
    others = [m for m in data["members"] if m != data["lead"]]
    return (f"You lead the team {data['name']} of sandbox-ai: {', '.join(others)} work in sandboxes of their own on "
            f"this same /work. Hand them tasks with the tool post of the MCP server {server_name(data['name'])} "
            f"(to: {', '.join(others)}; sender: {data['lead']}), wait for their answers with wait_for_answer, and "
            "see every task with board. Split the work so that they do not change the same files at once. Wait for "
            "the human's first request now." + role_text(data, data["lead"]))


def member_prompt(data, agent):
    return (f"You are {agent} in the team {data['name']} of sandbox-ai; {data['lead']} leads it and works on this "
            f"same /work. Use the MCP server {server_name(data['name'])}: call wait_for_task with agent "
            f"{agent}, do the task you get, report with done (its id and your answer), and wait again. Do not stop "
            "between tasks; when no task comes, wait again." + role_text(data, agent))


def prompt_args(agent, prompt):
    """The agent's interactive start with a first prompt, in the agent's own form (as presets.prompt_args gives a
    preset's first task): the text is an option's own value where the agent has one, so it is never read as an
    option. Each stays interactive after it, so that you can attach and talk to it."""
    if agent in ("opencode", "opencode2"):
        return ["--prompt", prompt]
    if agent == "hermes":
        return ["chat", "--query=" + prompt]  # Hermes' chat seeds its interactive session with the query
    if agent == "qwen":
        return ["--prompt-interactive=" + prompt]  # Qwen Code runs it and stays interactive
    if agent == "goose":
        return ["run", "--interactive", "--text=" + prompt]  # Goose runs it and stays interactive
    if agent == "openhands":
        # The Canvas takes no first prompt: one task without the UI, in which the member waits for the board's tasks.
        return ["task", "--text=" + prompt]
    if agent == "nanobot":
        # Its terminal client takes no first prompt: one message (in nanobot's own workspace, so it is told where the
        # project is), in which the member waits for the board's tasks.
        return ["agent", "--message=(sandbox-ai: the project is /work; work there - your tools' relative paths start "
                "in your own workspace.)\n\n" + prompt]
    if agent == "letta":
        # Its terminal UI takes no first prompt: headless (one task, in which the member waits for the board's tasks;
        # a restart goes on interactively in the same conversation); after "--" a text that starts with "-" stays it.
        return ["-p", "--", prompt]
    if agent == "pi":
        # Pi's terminal UI starts with it as its first message and stays interactive (after "--"; "@" would be a file).
        return ["--", " " + prompt if prompt.startswith("@") else prompt]
    if agent == "dsh":
        # sandbox-ai's chat for DSH (DSH has no chat in the terminal of its own): the prompt is its first message,
        # then the chat waits for yours; its runs have the board as their MCP server (dsh.mcp_rows).
        return ["chat", "--prompt=" + prompt]
    return [prompt]


def launcher_argv(data, agent):
    role = lead_prompt(data) if agent == data["lead"] else member_prompt(data, agent)
    vpn = member_vpns(data).get(agent)
    # --workspaces-as-given: the team's workspace is /work, never a project file of the directory the team was
    # started from (the web service's, the caller's), which the launcher would otherwise make the main workspace.
    return [sys.executable, "-I", str(HERE / "agent_sandbox.py"), agent, "--session", member_name(data["name"], agent),
            "--workspace", data["workspace"], "--workspaces-as-given", "--mcp", server_name(data["name"]),
            *member_launcher_options(data, agent), *(["--vpn", vpn] if vpn else []), "--", *prompt_args(agent, role)]


def without_options(canonical, names):
    """The options CANONICAL (long forms, up to an optional --) without every NAMES option and its value."""
    kept, index = [], 0
    while index < len(canonical):
        token = canonical[index]
        if token == "--":
            kept += canonical[index:]
            break
        if token in names and index + 1 < len(canonical):
            index += 2
            continue
        if token.split("=", 1)[0] not in names:
            kept.append(token)
        index += 1
    return kept


def member_launcher_options(data, agent):
    """The team's options for AGENT: its own --model, --llm and --mode (member_options) in place of the team's."""
    own = (data.get("member_options") or {}).get(agent) or {}
    if not own:
        return list(data["options"])
    launcher = load_module("agent_sandbox_for_team", "agent_sandbox.py")
    options = without_options(launcher.canonical_options(list(data["options"])), set(own))
    return [*options, *[word for option, value in own.items() for word in (option, value)]]


def split_vpn(canonical):
    """(the --vpn of the options CANONICAL - in their long forms - or None, the options without it). Given twice
    with two profiles: an error (each member would take the last)."""
    found, kept, index = [], [], 0
    while index < len(canonical):
        token = canonical[index]
        if token == "--":
            kept += canonical[index:]
            break
        if token == "--vpn" and index + 1 < len(canonical):
            found.append(canonical[index + 1])
            index += 2
            continue
        if token.startswith("--vpn="):
            found.append(token.split("=", 1)[1])
        else:
            kept.append(token)
        index += 1
    if len(set(found)) > 1:
        raise ValueError(f"--vpn is given {len(found)} times in the options ({', '.join(found)}); give one, or each "
                         "member its own: --member-vpn AGENT=PROFILE")
    return (found[0] if found else None), kept


def vpn_label(name, profile):
    provider = profile.get("provider")
    return f"{name} ({provider if provider not in (None, 'generic') else profile['type']})"


def vpn_plan(agents, vpn, member_vpn, shared_openvpn=False, out=print, holder=None):
    """Check the VPNs of the members AGENTS (VPN for all, MEMBER_VPN {agent: profile} each) before anything starts:
    every profile there, one that would run in several members at once only where it can (vpn.sharing: Tor and an
    ephemeral Tailscale profile said, WireGuard and a persistent Tailscale node refused with what to do instead,
    OpenVPN only with SHARED_OPENVPN), and none that another sandbox holds already (HOLDER(profile), default
    vpn.profile_holder). {agent: profile or None}; ValueError with the reason."""
    unknown = sorted(set(member_vpn) - set(agents))
    if unknown:
        raise ValueError(f"--member-vpn {unknown[0]}=...: {unknown[0]} is no member of this team "
                         f"(--agents {','.join(agents)})")
    chosen = {agent: member_vpn.get(agent) or vpn for agent in agents}
    if not any(chosen.values()):
        return chosen
    v = vpn_module()
    holder = holder or v.profile_holder
    groups = {}
    for agent, profile in chosen.items():
        if profile:
            groups.setdefault(profile, []).append(agent)
    for name, members in groups.items():
        profile = v.load_profile(name)  # (a missing or damaged one: its own message)
        kind, (why, fix), label = v.sharing(profile), v.sharing_text(profile), vpn_label(name, profile)
        held = holder(profile)
        if held and kind == "once":
            raise ValueError(f"VPN profile {label} is in use by {held}; {why} ({fix})")
        if len(members) > 1:
            who = ", ".join(members)
            if kind == "shared":
                out(f"sandbox-ai team: VPN {label} for {who}: {why}" +
                    (" - each member gets its own" if profile["type"] == "tor" else ""))
            elif kind == "once":
                raise ValueError(f"VPN profile {label} would run in {len(members)} members at once ({who}): {why}. "
                                 f"Instead: {fix}; one per member with --member-vpn AGENT=PROFILE")
            elif not shared_openvpn:
                raise ValueError(f"VPN profile {label} would run in {len(members)} members at once ({who}): {why}. "
                                 "If your server allows it, confirm with --allow-shared-openvpn; else give each member "
                                 "a profile of its own: --member-vpn AGENT=PROFILE")
            else:
                out(f"sandbox-ai team: VPN {label} for {who} (confirmed with --allow-shared-openvpn): {why}")
        if held and kind == "ask":
            out(f"sandbox-ai team: VPN {label} is in use by {held} as well: {why}")
    return chosen


def parse_member_vpn(values):
    """{agent: profile} of --member-vpn AGENT=PROFILE (repeatable)."""
    found = {}
    for value in values or []:
        agent, equals, profile = str(value).partition("=")
        if not equals or agent not in AGENTS or not profile:
            raise ValueError(f"--member-vpn {value}: AGENT=PROFILE, AGENT one of {', '.join(AGENTS)}")
        if agent in found and found[agent] != profile:
            raise ValueError(f"--member-vpn: {agent} is given twice")
        vpn_module().check_name(profile)
        found[agent] = profile
    return found


def parse_member_values(values, flag):
    """{agent: value} of FLAG AGENT=VALUE (repeatable): --member-model, --member-llm, --member-mode or --role."""
    found = {}
    for value in values or []:
        agent, equals, given = str(value).partition("=")
        if not equals or agent not in AGENTS or not given:
            raise ValueError(f"{flag} {printable(value)[:80]}: AGENT=VALUE, AGENT one of {', '.join(AGENTS)}")
        if agent in found and found[agent] != given:
            raise ValueError(f"{flag}: {agent} is given twice")
        found[agent] = given
    return found


def check_roles(roles):
    """ROLES {agent: text} as a role may be: one line of printable text, at most ROLE_LIMIT characters."""
    for agent, text in roles.items():
        if not isinstance(text, str) or not text.strip() or not text.isprintable() or len(text) > ROLE_LIMIT:
            raise ValueError(f"--role {agent}=...: one line of text, at most {ROLE_LIMIT} characters")
    return {agent: text.strip() for agent, text in roles.items()}


def check_member_options(member_options):
    """MEMBER_OPTIONS {agent: {launcher option: value}} (--model, --llm, --mode) as the launcher takes them: a mode of
    its MODES, a model or SERVER[:MODEL] of MEMBER_VALUE's characters; {agent: {option: value}} without empty ones."""
    modes = load_module("agent_sandbox_for_team", "agent_sandbox.py").MODES.NAMES
    flags = {option: flag for flag, option in MEMBER_OPTIONS.items()}
    checked = {}
    for agent, own in (member_options or {}).items():
        for option, value in own.items():
            if option not in flags:
                raise ValueError(f"a member's own option is one of {', '.join(flags)}, not {printable(option)[:40]}")
            if option == "--mode" and value not in modes:
                raise ValueError(f"{flags[option]} {agent}={printable(value)[:40]}: a mode is one of "
                                 f"{', '.join(modes)}")
            if not isinstance(value, str) or not MEMBER_VALUE.fullmatch(value):
                raise ValueError(f"{flags[option]} {agent}={printable(value)[:80]}: letters, digits and . _ : / @ + , "
                                 "= - (at most 200)")
        if own:
            checked[agent] = dict(own)
    return checked


TEAM_CHOSEN = ("--session", "--name", "--project-name", "--workspace", "--workspace-ro", "--mcp-socket-dir",
               "--tmux-socket-dir", "--pod-name", "--start-id", "--helper-of", "--config")
# (--no-config is the team's to choose: every member starts without the workspace's project file then - the app's Board
# passes it, as its start of one sandbox does)


def existing_sessions(name, agents):
    """The member names of team NAME that some start already uses (a session, kept or running)."""
    sessions = load_module("sessions_for_team", "sessions.py")
    taken = []
    for agent in agents:
        try:
            sessions.resolve(member_name(name, agent))
        except ValueError:
            continue
        taken.append(member_name(name, agent))
    return taken


def start(name, agents, workspace, options, out=print, run=subprocess.run, taken=None, vpn=None, member_vpn=None,
          shared_openvpn=False, holder=None, roles=None, member_options=None, dry_run=False, ask=None, log_in=None,
          tty=None, login_question=True):
    """Start team NAME: AGENTS (the first leads) on WORKSPACE with the launcher OPTIONS for every member, its VPNs
    (VPN, MEMBER_VPN, SHARED_OPENVPN), each member's role (ROLES {agent: text}) and own launcher options
    (MEMBER_OPTIONS {agent: {--model/--llm/--mode: value}}). DRY_RUN: every check, then each member's start as the
    launcher's dry run; nothing made, nothing started (dry_run_members). 0, or 1 when a member did not start;
    ValueError with the reason of a refusal.

    A team of that name with the same members (the same lead first) as a single start meets its session: one that
    runs is attached to on a terminal (TTY(), default: standard input and output are one) - its lead -, one whose
    members are stopped or paused continues them (`sandbox-ai resume`, with the options they were made with); one
    of other members, or one that runs without a terminal here, is refused with what is seen there (Exists).
    Before a member starts, the logins its agents lack here are asked for on a terminal (ASK, LOG_IN: the tests'),
    else - or without LOGIN_QUESTION (--no-login-question: the desktop app asked already) - named (logins_first)."""
    tty = tty if tty is not None else (lambda: sys.stdin.isatty() and sys.stdout.isatty())
    found = existing(name, agents, options)
    if found is not None:
        return start_existing(found, out, run, dry_run, ask, log_in, tty, login_question)
    data = prepare(name, agents, workspace, options, out, taken, vpn, member_vpn, shared_openvpn, holder, roles,
                   member_options)
    if dry_run:
        return dry_run_members(data, out, run)
    logins_first(data, data["members"], out, ask, log_in, tty if login_question else (lambda: False))
    directory = teams_dir() / name
    (directory / "board").mkdir(mode=0o700, parents=True)
    try:
        write_record(name, data)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)  # (nothing started yet: the name is free again)
        raise
    for number, agent in enumerate(data["members"]):
        done = run(launcher_argv(data, agent), stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if done.returncode != 0:
            out(f"sandbox-ai team: {member_name(name, agent)} did not start ({done.returncode}): "
                f"{(done.stderr or '').strip()[-1500:]}")
            if number:
                out(f"sandbox-ai team: the members that started keep running; stop them with: sandbox-ai team stop "
                    f"{name}")
            else:  # (none had started: nothing runs)
                out(f"sandbox-ai team: no member of {name} started; once that is fixed, start it again with the same "
                    f"command, or remove it: sandbox-ai team delete {name} --yes")
            return 1
        out(f"sandbox-ai team: {member_name(name, agent)} started" + (" (leads)" if agent == data["lead"] else ""))
    out(f"sandbox-ai team: talk to the lead with: sandbox-ai attach {member_name(name, data['lead'])}")
    return 0


# --- a team of that name is there already (as a start meets a session of its name: sessions.start_locked) ---------

_sessions = None


def sessions_module():
    global _sessions
    if _sessions is None:
        _sessions = load_module("sessions_for_team", "sessions.py")
    return _sessions


SEEN_KEYS = ("name", "id", "agent", "state", "foreground", "terminal", "attachable", "seen", "login")


def member_seen(session):
    """What is there under the session name SESSION now (sessions.details, without probing its relay or Podman):
    {"name", "id", "agent", "state", "foreground", "terminal", "attachable", "seen", "login"}; None when no start
    has that name; {"name", "state": "damaged", "seen": why} when its record cannot be read."""
    sessions = sessions_module()
    try:
        key = sessions.resolve(session)
    except ValueError:
        return None
    try:
        found = sessions.details(key, probe=False)
    except (OSError, ValueError) as exc:
        return {"name": session, "state": "damaged", "seen": printable(exc)[:300]}
    return {key_: found.get(key_) for key_ in SEEN_KEYS}


def seen_words(name, members, seen):
    """What is there of team NAME (its MEMBERS, SEEN {agent: member_seen or None}) in one line per member."""
    return "; ".join(f"{member_name(name, agent)}: " + (
        "none" if seen.get(agent) is None else f"{seen[agent].get('state')} ({printable(seen[agent].get('seen') or '')})"
        if seen[agent].get("seen") else str(seen[agent].get("state"))) for agent in members)


def existing(name, agents, options=()):
    """What a start of team NAME with AGENTS (OPTIONS: what it gives now) meets there: None (no team of that name),
    else {"name", "data": its record, "members", "lead", "same": the same members with the same lead, "seen":
    {agent: member_seen}, "states", "verdict": "attach" (it runs; its lead has a terminal), "continue" (some
    members are stopped or paused: they continue), "exists" (refused: other members, a member gone, or it runs
    without a terminal to attach to), "error": why it is refused or what is done, "ignored": options given now that
    the team does not take (it keeps its own)}. Exists for a team that is there without a readable record."""
    check_name(name)
    if not record_path(name).exists():
        if (teams_dir() / name).exists():
            raise Exists(f"team {name} exists without its record (a start that was ended before it wrote it): "
                         f"remove it with: sandbox-ai team delete {name} --yes")
        return None
    try:
        data = load(name)
    except ValueError as exc:
        raise Exists(f"team {name} exists, but {printable(exc)}; remove it with: sandbox-ai team delete {name} "
                     "--yes") from None
    agents = list(dict.fromkeys(agents))
    members, lead = data["members"], data["lead"]
    seen = {agent: member_seen(member_name(name, agent)) for agent in members}
    states = {agent: (found or {}).get("state") or "gone" for agent, found in seen.items()}
    said = seen_words(name, members, seen)
    found = {"name": name, "data": data, "members": members, "lead": lead, "same": agents == members, "seen": seen,
             "states": states, "ignored": [o for o in options if list(options) != list(data["options"])]}
    lead_session = member_name(name, lead)
    if agents != members:
        found.update(verdict="exists", error=(
            f"team {name} exists with {', '.join(members)} (lead {lead}), not {', '.join(agents)} - {said}: start "
            f"under another team name, or delete it first: sandbox-ai team delete {name} --yes"))
        return found
    gone = [agent for agent in members if states[agent] in ("gone", "damaged")]
    if gone:
        found.update(verdict="exists", error=(
            f"team {name} exists, but {', '.join(member_name(name, a) for a in gone)} cannot start again - {said}: "
            f"delete the team (sandbox-ai team delete {name} --yes) and start it anew, or start under another "
            "team name"))
        return found
    waiting = [agent for agent in members if states[agent] in ("stopped", "paused", "orphaned")]
    if waiting:
        found.update(verdict="continue", waiting=waiting, error=(
            f"team {name} exists - {said}: " + ", ".join(member_name(name, a) for a in waiting)
            + " continue with the options they were made with"))
        return found
    if (seen[lead] or {}).get("attachable"):
        found.update(verdict="attach", error=(
            f"team {name} runs already - {said}; talk to its lead: sandbox-ai attach {lead_session}, stop it: "
            f"sandbox-ai team stop {name}, or start under another team name"))
        return found
    found.update(verdict="exists", error=(
        f"team {name} runs already, its lead without a terminal to attach to - {said}; stop it: sandbox-ai team stop "
        f"{name}, then start it again, or start under another team name"))
    return found


def start_existing(found, out=print, run=subprocess.run, dry_run=False, ask=None, log_in=None, tty=None,
                   login_question=True):
    """A start of the team FOUND (existing()) describes: attached to its lead on a terminal, continued, or refused
    (Exists, with what is seen there)."""
    name, data, verdict = found["name"], found["data"], found["verdict"]
    lead = member_name(name, data["lead"])
    if verdict == "exists" or (verdict == "attach" and (dry_run or not tty())):
        raise Exists(found["error"])
    if verdict == "attach":
        out(f"sandbox-ai team: team {name} runs already: attaching to its lead {lead} (its members keep the options "
            "they were started with)")
        return run([sys.executable, "-I", str(HERE / "main.py"), "attach", lead]).returncode
    if found["ignored"]:
        out(f"sandbox-ai team: warning: team {name} keeps the options it was made with "
            f"({printable(' '.join(data['options'])) or 'none'}); ignored now: "
            f"{printable(' '.join(found['ignored']))}. Use another team name for other options.")
    if dry_run:
        for agent in found["waiting"]:
            out(f"sandbox-ai team: {member_name(name, agent)} would continue ({found['states'][agent]})")
        return 0
    logins_first(data, [a for a in found["waiting"] if not (found["seen"].get(a) or {}).get("login")], out, ask,
                 log_in, tty if login_question else (lambda: False))
    out(f"sandbox-ai team: team {name} exists: " + ", ".join(
        f"{member_name(name, a)} ({found['states'][a]})" for a in found["waiting"]) + " continue")
    for agent in found["waiting"]:
        done = run([sys.executable, "-I", str(HERE / "main.py"), "resume", member_name(name, agent)],
                   stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if done.returncode != 0:
            out(f"sandbox-ai team: {member_name(name, agent)} did not continue ({done.returncode}): "
                f"{printable((done.stderr or '').strip()[-1500:])}")
            out(f"sandbox-ai team: the members that run keep running; stop them with: sandbox-ai team stop {name}")
            return 1
        out(f"sandbox-ai team: {member_name(name, agent)} continued" + (" (leads)" if agent == data["lead"] else ""))
    out(f"sandbox-ai team: talk to the lead with: sandbox-ai attach {lead}")
    return 0


# --- the members' logins (as a start's: agent_sandbox.check_login), asked for before the first member starts -------

# Options with which a member gets its login (or its model) otherwise: no login of sandbox-ai's is asked for then
# (the desktop app's prerequisites.LOGIN_ELSEWHERE).
LOGIN_ELSEWHERE = frozenset({"--no-auth", "--copy-auth", "--auth-file", "--import-codex-auth", "--auth-from-session",
                             "--llm", "--api-key", "--local-config", "--offline", "--state-dir", "--binary",
                             "--probe", "--dry-run"})
KEY_VARIABLE = re.compile(r"API_KEY|AUTH_TOKEN|OAUTH_TOKEN|ACCESS_TOKEN")


def login_wanted(data, agent):
    """Whether member AGENT of team DATA signs in with a login of sandbox-ai's (an agent `sandbox-ai login` takes,
    without an option that brings its login or model otherwise: LOGIN_ELSEWHERE, a key handed in as a variable)."""
    if agent not in cached_module("login_for_team", "login.py").AGENTS:
        return False
    launcher = cached_module("agent_sandbox_for_team", "agent_sandbox.py")
    options = launcher.canonical_options(member_launcher_options(data, agent))
    for index, token in enumerate(options):
        option, equals, value = str(token).partition("=")
        if option == "--":
            break
        if option in LOGIN_ELSEWHERE:
            return False
        if option in ("--env", "--secret-env"):
            value = value if equals else (str(options[index + 1]) if index + 1 < len(options) else "")
            if KEY_VARIABLE.search(value.partition("=")[0]):
                return False
    return True


def login_missing(data, agents=None):
    """[(agent, problem, where)] of the members AGENTS (default: all) of team DATA whose login sandbox-ai's login
    store or this machine lacks ("missing") or has run out ("expired"), where: "store" or "host"."""
    launcher = cached_module("agent_sandbox_for_team", "agent_sandbox.py")
    found = []
    for agent in data["members"] if agents is None else agents:
        if not login_wanted(data, agent):
            continue
        where, problem = launcher.login_state(agent)
        if problem is not None:
            found.append((agent, problem, where))
    return found


def login_words(agent, problem, where):
    place = "sandbox-ai's login store" if where == "store" else "this machine"
    return f"{place} has {'no' if problem == 'missing' else 'an expired'} {agent} login"


def logins_first(data, agents, out=print, ask=None, log_in=None, tty=None):
    """The logins the members AGENTS of team DATA lack here, before the first of them starts: on a terminal (TTY())
    asked for one after the other - `sandbox-ai login AGENT` in a sandbox, kept for every later sandbox of that agent
    (Codex's device sign-in with its page and code; in a tab of the desktop app, the app's sign-in window) -, else
    named: a member without one asks for it in its own terminal, which nobody watches at the start."""
    missing = login_missing(data, agents)
    if not missing:
        return
    asking = tty() if tty is not None else sys.stdin.isatty() and sys.stdout.isatty()
    for agent, problem, where in missing:
        member = member_name(data["name"], agent)
        what = login_words(agent, problem, where)
        if not asking:
            out(f"sandbox-ai team: note: {what}: {member} asks for it in its own terminal (sandbox-ai attach "
                f"{member}); keep one for every {agent} sandbox with: sandbox-ai login {agent}")
            continue
        try:
            answer = (ask or input)(f"sandbox-ai team: {what}: {member} would start without one and ask for it in "
                                    f"its own terminal. Log in now and keep that login for every {agent} sandbox? "
                                    "[Y/n] ")
        except EOFError:
            answer = "n"
        if str(answer or "").strip().lower() not in ("", "y", "yes", "j", "ja"):
            out(f"sandbox-ai team: {member} starts without a login of sandbox-ai's (it asks in its own terminal)")
            continue
        status = (log_in or cached_module("login_for_team", "login.py").login)(agent)
        if status != 0:
            out(f"sandbox-ai team: no {agent} login was made (exit status {status}): {member} starts without one")


class Exists(ValueError):
    """Team NAME exists already (its record, or what a start left of it)."""


class Taken(ValueError):
    """A member's session name is in use by another start (SEEN: {session: member_seen})."""

    def __init__(self, text, seen=None):
        super().__init__(text)
        self.seen = dict(seen or {})


def prepare(name, agents, workspace, options, out=print, taken=None, vpn=None, member_vpn=None, shared_openvpn=False,
            holder=None, roles=None, member_options=None):
    """Every check of a team start (start's arguments), nothing made: the team's record as it would be written;
    ValueError (Exists for a name in use) with the reason."""
    check_name(name)
    if (teams_dir() / name).exists() and not record_path(name).exists():
        raise Exists(f"team {name} exists without its record (a start that was ended before it wrote it): "
                     f"remove it with: sandbox-ai team delete {name} --yes")
    if record_path(name).exists() or (teams_dir() / name).exists():
        raise Exists(f"team {name} exists; see: sandbox-ai team show {name}, or delete it first")
    agents = list(dict.fromkeys(agents))
    if len(agents) < 2 or any(agent not in AGENTS for agent in agents):
        raise ValueError(f"--agents: two or more of {', '.join(AGENTS)}, the first leads")
    launcher = load_module("agent_sandbox_for_team", "agent_sandbox.py")
    canonical = launcher.canonical_options(list(options))  # short forms (-s, -w, ...) as their long options
    forbidden = [o for o in canonical if o.split("=", 1)[0] in TEAM_CHOSEN]
    if forbidden:
        raise ValueError(f"the team chooses these itself: {' '.join(forbidden)}")
    # --vpn after -- reaches every member: the same as the team's --vpn, and checked the same way.
    given, rest = split_vpn(canonical)
    if given is not None:
        if vpn is not None and vpn != given:
            raise ValueError(f"--vpn {vpn} and --vpn {given} in the options: give one")
        vpn, options = given, rest
    member_vpn = dict(member_vpn or {})
    roles, member_options = check_roles(dict(roles or {})), check_member_options(member_options)
    for flag, given in (("--role", roles), ("--member-model/--member-llm/--member-mode", member_options)):
        unknown = sorted(set(given) - set(agents))
        if unknown:
            raise ValueError(f"{flag} {unknown[0]}=...: {unknown[0]} is no member of this team "
                             f"(--agents {','.join(agents)})")
    if vpn is not None:
        vpn_module().check_name(vpn)
    vpn_plan(agents, vpn, member_vpn, shared_openvpn, out, holder)
    # A member is a session of its own name: one that exists already (another project's) is never taken over.
    taken = existing_sessions(name, agents) if taken is None else taken
    if taken:
        seen = {session: member_seen(session) for session in taken}
        raise Taken(f"these names are in use already: " + ", ".join(
            session + (f" (seen: {printable(seen[session].get('seen'))})" if (seen[session] or {}).get("seen") else "")
            for session in taken) + "; choose another team name", seen)
    return {"version": 1, "name": name, "members": agents, "lead": agents[0],
            "workspace": str(Path(workspace).expanduser().resolve()), "options": list(options), "vpn": vpn,
            "member_vpn": {agent: profile for agent, profile in member_vpn.items() if profile != vpn},
            "shared_openvpn": bool(shared_openvpn), "roles": roles, "member_options": member_options,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def dry_run_argv(data, agent):
    """AGENT's start of team DATA as the launcher's dry run: without the board (team-NAME is made by the start, so a
    dry run would not know it) and with --dry-run before the agent's own arguments."""
    argv = launcher_argv(data, agent)
    cut = argv.index("--")
    options = argv[:cut]
    at = options.index("--mcp")
    return [*options[:at], *options[at + 2:], "--dry-run", *argv[cut:]]


def dry_run_members(data, out=print, run=subprocess.run):
    """Each member's start of team DATA as the launcher's dry run, the lead first: 0 with a line per member, else
    ValueError naming the first member the launcher refuses and its reason."""
    for agent in data["members"]:
        done = run(dry_run_argv(data, agent), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            lines = [line.strip() for line in (done.stderr or done.stdout or "").splitlines() if line.strip()]
            why = re.sub(r"^(?:agent-sandbox|sandbox-ai): ", "", lines[-1]) if lines else f"exit status {done.returncode}"
            raise ValueError(f"{member_name(data['name'], agent)}: {printable(why)[:600]}")
        out(f"sandbox-ai team: {member_name(data['name'], agent)} would start" +
            (" (leads)" if agent == data["lead"] else ""))
    return 0


def plan(data):
    """The plan of a team start (--dry-run --json): its record as it would be written, and each member's session,
    whether it leads, its options and its first prompt."""
    return {**{k: v for k, v in data.items() if k != "created"}, "ok": True,
            "plan": [{"agent": agent, "session": member_name(data["name"], agent), "lead": agent == data["lead"],
                      "options": member_launcher_options(data, agent),
                      "vpn": member_vpns(data).get(agent),
                      "prompt": lead_prompt(data) if agent == data["lead"] else member_prompt(data, agent)}
                     for agent in data["members"]]}


def member_states(data):
    sessions = load_module("sessions_for_team", "sessions.py")
    states = {}
    for agent in data["members"]:
        try:
            key = sessions.resolve(member_name(data["name"], agent))
            states[agent] = sessions.status(key, sessions.load(key))
        except (OSError, ValueError):
            states[agent] = "gone"
    return states


def board_of(data):
    try:
        return board_module().read(teams_dir() / data["name"] / "board")
    except (OSError, ValueError) as exc:
        return {"next": 0, "tasks": [], "error": str(exc)}


def show(name, out=print, as_json=False):
    data = load(name)
    states, board = member_states(data), board_of(data)
    if as_json:
        out(json.dumps({**data, "states": states, "board": board}, indent=2))
        return 0
    out(f"team {name}: {data['workspace']}")
    vpns = member_vpns(data)
    for agent in data["members"]:
        role = "lead" if agent == data["lead"] else "member"
        out(f"  {member_name(name, agent):<30} {role:<7} {states[agent]}" +
            (f"  vpn {printable(vpns[agent])}" if vpns[agent] else ""))
    if board.get("error"):
        out(f"  board: {printable(board['error'])}")
    for task in board["tasks"][-20:]:
        # The board's text comes from the members: never a control sequence on your terminal.
        first = printable(" ".join(task["task"].split())[:70])
        sender = printable(str(task.get("from") or "?")[:64])
        out(f"  #{task['id']:<4} {sender} -> {printable(task['to']):<9} {task['state']:<8} {first}")
    return 0


def post(name, agent, task, out=print, run=subprocess.run):
    data = load(name)
    mcp = load_module("mcp_for_team", "mcp.py") if sys.platform == "darwin" else None
    if mcp is not None and mcp.status(server_name(name)) == "running":
        # macOS: the board's server runs in podman machine's VM, and its lock (flock) does not cross the VM's shared
        # folders: the task is posted in there, by team_board.py itself, under the lock the server takes.
        container = f"{mcp.pod_name(server_name(name))}-agent"
        result = run(["podman", "exec", "--interactive", "--user=0:0", container, "python3", "-I", BOARD_TARGET,
                      "--members", ",".join(data["members"]), "--dir", "/work", "--post", agent],
                     input=task, capture_output=True, text=True, timeout=60, check=False)
        try:
            ident = json.loads(result.stdout.strip().splitlines()[-1])["id"] if result.returncode == 0 else None
        except (ValueError, IndexError, KeyError, TypeError, RecursionError):
            ident = None
        if not isinstance(ident, int):
            why = (result.stderr or "").strip().splitlines()[-1:] or [f"status {result.returncode}"]
            raise ValueError(f"the board in podman machine did not take the task: {printable(why[0][:300])}")
        out(f"sandbox-ai team: task #{ident} posted for {agent}")
        return 0
    board = board_module().Board(data["members"], teams_dir() / name / "board")
    ident = board.post(agent, task, sender="human")["id"]
    out(f"sandbox-ai team: task #{ident} posted for {agent}")
    return 0


def damaged_record(name):
    """What is left of team NAME whose record is damaged: every agent's member name, so that stop and delete reach
    whatever runs or is kept under them (a member that never was is "gone")."""
    return {"name": check_name(name), "members": list(AGENTS), "lead": AGENTS[0], "workspace": "?", "options": []}


def stop(name, out=print, run=subprocess.run, states=None, data=None):
    """Stop every member that runs (or is paused) and the board; 1 if one of them could not be stopped."""
    data = load(name) if data is None else data
    states = member_states(data) if states is None else states
    status = 0
    for agent in reversed(data["members"]):
        if states.get(agent) in ("stopped", "gone"):
            out(f"sandbox-ai team: {member_name(name, agent)}: {states[agent]}")
            continue
        done = run([sys.executable, "-I", str(HERE / "main.py"), "stop", member_name(name, agent)],
                   stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if done.returncode != 0:
            status = 1
        out(f"sandbox-ai team: {member_name(name, agent)}: " + ("stopped" if done.returncode == 0 else
                                                               printable((done.stderr or "").strip()[-300:])
                                                               or "not stopped"))
    run([sys.executable, "-I", str(HERE / "mcp.py"), "stop", server_name(name)], stdin=subprocess.DEVNULL,
        capture_output=True, text=True)
    return status


def delete(name, yes=False, out=print, run=subprocess.run, states=None):
    """Stop the team, delete its members (their HOMEs) and its board; the record stays while a member is left. A
    team whose record is damaged is deleted too (with --yes): its members by every agent's member name."""
    try:
        data = load(name)
    except ValueError:
        if not record_path(name).exists() and not (teams_dir() / check_name(name) / "board").is_dir():
            raise
        if not yes:
            raise ValueError(f"the record of team {name} is damaged; this deletes it, every member of its name "
                             f"({', '.join(member_name(name, a) for a in AGENTS)}) and their HOMEs; add --yes") from None
        out(f"sandbox-ai team: the record of {name} is damaged: its members are looked up by name")
        data = damaged_record(name)
    if not yes:
        raise ValueError(f"this deletes team {name}, its members' HOMEs and its board; add --yes")
    states = member_states(data) if states is None else states
    if stop(name, out, run, states, data) != 0:
        out(f"sandbox-ai team: {name} is kept: a member could not be stopped")
        return 1
    left = []
    for agent in data["members"]:
        if states.get(agent) == "gone":
            continue
        done = run([sys.executable, "-I", str(HERE / "main.py"), "delete", member_name(name, agent), "--yes"],
                   stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if done.returncode != 0:
            left.append(member_name(name, agent))
    if left:
        out(f"sandbox-ai team: {name} is kept: {', '.join(left)} could not be deleted")
        return 1
    mcp = load_module("mcp_for_team", "mcp.py")
    shutil.rmtree(mcp.state_dir(server_name(name)), ignore_errors=True)  # the board server's own state
    shutil.rmtree(teams_dir() / name, ignore_errors=True)
    out(f"sandbox-ai team: {name} deleted")
    return 0


def member_options_of(a):
    """{agent: {--model/--llm/--mode: value}} of the parsed --member-model, --member-llm and --member-mode."""
    found = {}
    for flag, option in MEMBER_OPTIONS.items():
        for agent, value in parse_member_values(getattr(a, flag[2:].replace("-", "_")), flag).items():
            found.setdefault(agent, {})[option] = value
    return found


def dry_run_json(a, out=print, run=subprocess.run):
    """`team start ... --dry-run --json`: {"ok": true, the record as it would be written, "plan": [each member],
    "notes": [what the checks said], "logins": [the members' logins this host lacks: {"agent", "problem", "where"}]},
    or {"ok": false, "error": why, "code": "exists" for a name in use ("team": what is there - existing()'s
    "members", "lead", "same", "states", "seen", "verdict": attach or exists), "taken" for a member's name another
    start has ("sessions": {session: what is there})}, exit 1. A team of that name with the same members, some of
    them stopped or paused: {"ok": true, "continues": [its members that continue], "team": ..., the plan of its
    record}."""
    notes = []
    agents = a.agents.split(",")

    def team_of(found):
        return {key: found[key] for key in ("members", "lead", "same", "states", "seen", "verdict")}

    def logins(data, agents=None):
        return [{"agent": agent, "problem": problem, "where": where}
                for agent, problem, where in login_missing(data, agents)]
    try:
        found = existing(a.name, agents, a.options)
        if found is not None and found["verdict"] == "continue":
            if found["ignored"]:
                notes.append(f"team {a.name} keeps the options it was made with; ignored now: "
                             f"{' '.join(found['ignored'])}")
            notes.append(found["error"])
            out(json.dumps({**plan(found["data"]), "continues": found["waiting"], "team": team_of(found),
                            "logins": logins(found["data"], [x for x in found["waiting"]
                                                              if not (found["seen"].get(x) or {}).get("login")]),
                            "notes": [printable(n) for n in notes]}, indent=2))
            return 0
        if found is not None:
            out(json.dumps({"ok": False, "error": printable(found["error"]), "code": "exists", "team": team_of(found),
                            "notes": []}, indent=2))
            return 1
        data = prepare(a.name, agents, a.workspace, a.options, notes.append, vpn=a.vpn,
                       member_vpn=parse_member_vpn(a.member_vpn), shared_openvpn=a.allow_shared_openvpn,
                       roles=parse_member_values(a.role, "--role"), member_options=member_options_of(a))
        dry_run_members(data, notes.append, run)
    except (OSError, ValueError) as exc:
        out(json.dumps({"ok": False, "error": printable(exc), "code": "exists" if isinstance(exc, Exists) else
                        "taken" if isinstance(exc, Taken) else None,
                        **({"sessions": exc.seen} if isinstance(exc, Taken) else {}),
                        "notes": [printable(n) for n in notes]}, indent=2))
        return 1
    out(json.dumps({**plan(data), "logins": logins(data), "notes": [printable(n) for n in notes]}, indent=2))
    return 0


def start_options(argv):
    """(ARGV up to the first --, the launcher options after it) for `team start`; (ARGV, []) for anything else. The
    team's own arguments come in any order before the -- (NAME --agents A,B -w DIR, or --agents A,B -w DIR NAME)."""
    if argv[:1] == ["start"] and "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1:]
    return argv, []


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def main(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai team", formatter_class=help_style,
                                     description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="start a team: one sandbox per agent, the first leads")
    begin.add_argument("name")
    begin.add_argument("--agents", required=True, help=f"two or more of {', '.join(AGENTS)} (comma-separated: --agents claude,codex): "
                       "the first leads")
    begin.add_argument("-w", "--workspace", default=".", help="the shared workspace (default: this directory)")
    begin.add_argument("--vpn", metavar="PROFILE", help="the VPN profile in front of every member (each member its "
                       "own VPN container: Tor in each; WireGuard and Mullvad in one member at a time)")
    begin.add_argument("--member-vpn", action="append", default=[], metavar="AGENT=PROFILE",
                       help="another VPN profile for one member (repeatable)")
    begin.add_argument("--allow-shared-openvpn", action="store_true",
                       help="let one OpenVPN profile run in several members at once (your server must allow an "
                            "account or certificate twice: duplicate-cn)")
    begin.add_argument("--role", action="append", default=[], metavar="AGENT=TEXT",
                       help="a member's role, added to its first prompt (one line; repeatable)")
    begin.add_argument("--member-model", action="append", default=[], metavar="AGENT=MODEL",
                       help="a member's own model, in place of the --model of the options (repeatable)")
    begin.add_argument("--member-llm", action="append", default=[], metavar="AGENT=SERVER[:MODEL]",
                       help="a member's own LLM server (sandbox-ai llm), in place of the --llm of the options "
                            "(repeatable)")
    begin.add_argument("--member-mode", action="append", default=[], metavar="AGENT=MODE",
                       help="a member's own security mode, in place of the --mode of the options (repeatable)")
    begin.add_argument("--dry-run", action="store_true",
                       help="check the start (each member's as the launcher's dry run); nothing starts")
    begin.add_argument("--json", action="store_true", help="with --dry-run: the plan or the refusal as JSON")
    begin.add_argument("--no-login-question", action="store_true",
                       help="name the members' missing logins instead of asking for them on the terminal (the "
                            "desktop app asked before)")
    begin.epilog = "Launcher options for every member follow a --: team start NAME --agents A,B -w DIR -- OPTIONS"
    commands.add_parser("list", help="the teams")
    shown = commands.add_parser("show", help="a team's members and its board")
    shown.add_argument("name")
    shown.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    posted = commands.add_parser("post", help="a task for a member, from you")
    posted.add_argument("name")
    posted.add_argument("agent", choices=AGENTS)
    posted.add_argument("task")
    stopped = commands.add_parser("stop", help="stop every member and the board")
    stopped.add_argument("name")
    deleted = commands.add_parser("delete", help="stop and remove the team with its members' HOMEs")
    deleted.add_argument("name")
    deleted.add_argument("--yes", action="store_true", help="delete without asking")
    argv, options = start_options(list(argv))
    # The options after -- are the namespace's "options" as they always were (front ends read it so).
    a = parser.parse_args(argv, namespace=argparse.Namespace(options=options))
    try:
        if a.command == "start" and a.json and not a.dry_run:
            raise ValueError("--json goes with --dry-run")
        if a.command == "start" and a.json:
            return dry_run_json(a, out)
        if a.command == "start":
            return start(a.name, a.agents.split(","), a.workspace, a.options, out, vpn=a.vpn,
                         member_vpn=parse_member_vpn(a.member_vpn), shared_openvpn=a.allow_shared_openvpn,
                         roles=parse_member_values(a.role, "--role"), member_options=member_options_of(a),
                         dry_run=a.dry_run, login_question=not a.no_login_question)
        if a.command == "list":
            if not names():  # (on standard error: the app and the web read standard output as the list)
                print("no teams; start one with: sandbox-ai team start NAME --agents claude,codex", file=sys.stderr)
            for name in names():
                try:
                    data = load(name)
                except ValueError:  # one damaged record never hides the other teams
                    out(f"{name:<20} {'(damaged)':<24} remove it with: sandbox-ai team delete {name} --yes")
                    continue
                out(f"{name:<20} {', '.join(data['members']):<24} {printable(data['workspace'])}")
            return 0
        if a.command == "show":
            return show(a.name, out, a.json)
        if a.command == "post":
            return post(a.name, a.agent, a.task, out)
        if a.command == "stop":
            return stop(a.name, out)
        return delete(a.name, a.yes, out)
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai team: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
