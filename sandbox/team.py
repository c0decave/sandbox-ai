#!/usr/bin/env python3
"""A team of agents: one sandbox per agent on a shared workspace, started and stopped together, with a task board
between them (`sandbox-ai team`).

  sandbox-ai team start NAME --agents claude,codex[,...] [-w DIR] [--vpn PROFILE] [--member-vpn AGENT=PROFILE]
                        [--allow-shared-openvpn] [-- LAUNCHER OPTIONS]
  sandbox-ai team list | show NAME | post NAME AGENT TASK | stop NAME | delete NAME --yes

Each member is a named session (NAME-AGENT: attach, pause, resume, stop like any start). The first agent leads:
it starts with a prompt that tells it about its team. The others start with a prompt to wait for tasks on the
board, do them and report back. The board is the MCP server team-NAME (team_board.py) in a sandbox of its own,
without network; every member reaches it through its relay, so mcp rules, questions and mcp-calls.jsonl hold.
Its file lies in the team's directory (TEAMS/NAME/board); `team post` writes there from the host.

Members: any of AGENTS (claude, codex, opencode, hermes, dsh, qwen, goose), each started with its first prompt in its
own interactive form (prompt_args) and the board as its --mcp server.

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
AGENTS = ("claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose")
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,19}")  # NAME-opencode stays a session name; team-NAME an MCP name
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
            or any(k not in members or not isinstance(v, str) for k, v in data.get("member_vpn", {}).items()):
        raise ValueError(f"the record of team {name} is damaged: {record_path(name)}")
    return data


def member_vpns(data):
    """{agent: the VPN profile in front of it, or None} of team DATA: --member-vpn's, else --vpn's."""
    own = data.get("member_vpn") or {}
    return {agent: own.get(agent) or data.get("vpn") for agent in data["members"]}


def names():
    directory = teams_dir()
    try:
        return sorted(p.name for p in directory.iterdir() if TEAM_NAME.fullmatch(p.name)
                      and (p / "team.json").is_file()) if directory.is_dir() else []
    except OSError:
        return []


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


def lead_prompt(data):
    others = [m for m in data["members"] if m != data["lead"]]
    return (f"You lead the team {data['name']} of sandbox-ai: {', '.join(others)} work in sandboxes of their own on "
            f"this same /work. Hand them tasks with the tool post of the MCP server {server_name(data['name'])} "
            f"(to: {', '.join(others)}; sender: {data['lead']}), wait for their answers with wait_for_answer, and "
            "see every task with board. Split the work so that they do not change the same files at once. Wait for "
            "the human's first request now.")


def member_prompt(data, agent):
    return (f"You are {agent} in the team {data['name']} of sandbox-ai; {data['lead']} leads it and works on this "
            f"same /work. Use the MCP server {server_name(data['name'])}: call wait_for_task with agent "
            f"{agent}, do the task you get, report with done (its id and your answer), and wait again. Do not stop "
            "between tasks; when no task comes, wait again.")


def prompt_args(agent, prompt):
    """The agent's interactive start with a first prompt, in the agent's own form (as presets.prompt_args gives a
    preset's first task): the text is an option's own value where the agent has one, so it is never read as an
    option. Each stays interactive after it, so that you can attach and talk to it."""
    if agent == "opencode":
        return ["--prompt", prompt]
    if agent == "hermes":
        return ["chat", "--query=" + prompt]  # Hermes' chat seeds its interactive session with the query
    if agent == "qwen":
        return ["--prompt-interactive=" + prompt]  # Qwen Code runs it and stays interactive
    if agent == "goose":
        return ["run", "--interactive", "--text=" + prompt]  # Goose runs it and stays interactive
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
            *data["options"], *(["--vpn", vpn] if vpn else []), "--", *prompt_args(agent, role)]


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


TEAM_CHOSEN = ("--session", "--name", "--project-name", "--workspace", "--workspace-ro", "--mcp-socket-dir",
               "--tmux-socket-dir", "--pod-name", "--start-id", "--helper-of", "--config", "--no-config")


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
          shared_openvpn=False, holder=None):
    check_name(name)
    if record_path(name).exists() or (teams_dir() / name).exists():
        raise ValueError(f"team {name} exists; see: sandbox-ai team show {name}, or delete it first")
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
    if vpn is not None:
        vpn_module().check_name(vpn)
    vpn_plan(agents, vpn, member_vpn, shared_openvpn, out, holder)
    # A member is a session of its own name: one that exists already (another project's) is never taken over.
    taken = existing_sessions(name, agents) if taken is None else taken
    if taken:
        raise ValueError(f"these names are in use already: {', '.join(taken)}; choose another team name")
    directory = teams_dir() / name
    (directory / "board").mkdir(mode=0o700, parents=True)
    data = {"version": 1, "name": name, "members": agents, "lead": agents[0],
            "workspace": str(Path(workspace).expanduser().resolve()), "options": list(options), "vpn": vpn,
            "member_vpn": {agent: profile for agent, profile in member_vpn.items() if profile != vpn},
            "shared_openvpn": bool(shared_openvpn), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    record_path(name).write_text(json.dumps(data, indent=2) + "\n")
    record_path(name).chmod(0o600)
    for agent in agents:
        done = run(launcher_argv(data, agent), stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if done.returncode != 0:
            out(f"sandbox-ai team: {member_name(name, agent)} did not start ({done.returncode}): "
                f"{(done.stderr or '').strip()[-1500:]}")
            out(f"sandbox-ai team: the members that started keep running; stop them with: sandbox-ai team stop {name}")
            return 1
        out(f"sandbox-ai team: {member_name(name, agent)} started" + (" (leads)" if agent == data["lead"] else ""))
    out(f"sandbox-ai team: talk to the lead with: sandbox-ai attach {member_name(name, data['lead'])}")
    return 0


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
        if not record_path(name).exists():
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


def start_options(argv):
    """(ARGV up to the first --, the launcher options after it) for `team start`; (ARGV, []) for anything else. The
    team's own arguments come in any order before the -- (NAME --agents A,B -w DIR, or --agents A,B -w DIR NAME)."""
    if argv[:1] == ["start"] and "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1:]
    return argv, []


def main(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai team", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="start a team: one sandbox per agent, the first leads")
    begin.add_argument("name")
    begin.add_argument("--agents", required=True, help=f"two or more of {','.join(AGENTS)}: the first leads")
    begin.add_argument("-w", "--workspace", default=".", help="the shared workspace (default: this directory)")
    begin.add_argument("--vpn", metavar="PROFILE", help="the VPN profile in front of every member (each member its "
                       "own VPN container: Tor in each; WireGuard and Mullvad in one member at a time)")
    begin.add_argument("--member-vpn", action="append", default=[], metavar="AGENT=PROFILE",
                       help="another VPN profile for one member (repeatable)")
    begin.add_argument("--allow-shared-openvpn", action="store_true",
                       help="let one OpenVPN profile run in several members at once (your server must allow an "
                            "account or certificate twice: duplicate-cn)")
    begin.epilog = "Launcher options for every member follow a --: team start NAME --agents A,B -w DIR -- OPTIONS"
    commands.add_parser("list", help="the teams")
    shown = commands.add_parser("show", help="a team's members and its board")
    shown.add_argument("name")
    shown.add_argument("--json", action="store_true")
    posted = commands.add_parser("post", help="a task for a member, from you")
    posted.add_argument("name")
    posted.add_argument("agent", choices=AGENTS)
    posted.add_argument("task")
    stopped = commands.add_parser("stop", help="stop every member and the board")
    stopped.add_argument("name")
    deleted = commands.add_parser("delete", help="stop and remove the team with its members' HOMEs")
    deleted.add_argument("name")
    deleted.add_argument("--yes", action="store_true")
    argv, options = start_options(list(argv))
    # The options after -- are the namespace's "options" as they always were (front ends read it so).
    a = parser.parse_args(argv, namespace=argparse.Namespace(options=options))
    try:
        if a.command == "start":
            return start(a.name, a.agents.split(","), a.workspace, a.options, out, vpn=a.vpn,
                         member_vpn=parse_member_vpn(a.member_vpn), shared_openvpn=a.allow_shared_openvpn)
        if a.command == "list":
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
