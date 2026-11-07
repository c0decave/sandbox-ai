#!/usr/bin/env python3
"""A team of agents: one sandbox per agent on a shared workspace, started and stopped together, with a task board
between them (`sandbox-ai team`).

  sandbox-ai team start NAME --agents claude,codex[,opencode] [-w DIR] [-- LAUNCHER OPTIONS]
  sandbox-ai team list | show NAME | post NAME AGENT TASK | stop NAME | delete NAME --yes

Each member is a named session (NAME-AGENT: attach, pause, resume, stop like any start). The first agent leads:
it starts with a prompt that tells it about its team. The others start with a prompt to wait for tasks on the
board, do them and report back. The board is the MCP server team-NAME (team_board.py) in a sandbox of its own,
without network; every member reaches it through its relay, so mcp rules, questions and mcp-calls.jsonl hold.
Its file lies in the team's directory (TEAMS/NAME/board); `team post` writes there from the host.
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
AGENTS = ("claude", "codex", "opencode")
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"the record of team {name} cannot be read: {exc}") from None
    members = data.get("members") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("name") != name or not isinstance(members, list) or len(members) < 2 \
            or any(m not in AGENTS for m in members) or len(set(members)) != len(members) \
            or data.get("lead") not in members or not isinstance(data.get("workspace"), str) \
            or not isinstance(data.get("options"), list) or not all(isinstance(o, str) for o in data["options"]):
        raise ValueError(f"the record of team {name} is damaged: {record_path(name)}")
    return data


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
    """The agent's interactive start with a first prompt."""
    return ["--prompt", prompt] if agent == "opencode" else [prompt]


def launcher_argv(data, agent):
    role = lead_prompt(data) if agent == data["lead"] else member_prompt(data, agent)
    return [sys.executable, "-I", str(HERE / "agent_sandbox.py"), agent, "--session", member_name(data["name"], agent),
            "--workspace", data["workspace"], "--mcp", server_name(data["name"]), *data["options"],
            "--", *prompt_args(agent, role)]


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


def start(name, agents, workspace, options, out=print, run=subprocess.run, taken=None):
    check_name(name)
    if record_path(name).exists() or (teams_dir() / name).exists():
        raise ValueError(f"team {name} exists; see: sandbox-ai team show {name}, or delete it first")
    agents = list(dict.fromkeys(agents))
    if len(agents) < 2 or any(agent not in AGENTS for agent in agents):
        raise ValueError(f"--agents: two or three of {', '.join(AGENTS)}, the first leads")
    launcher = load_module("agent_sandbox_for_team", "agent_sandbox.py")
    canonical = launcher.canonical_options(list(options))  # short forms (-s, -w, ...) as their long options
    forbidden = [o for o in canonical if o.split("=", 1)[0] in TEAM_CHOSEN]
    if forbidden:
        raise ValueError(f"the team chooses these itself: {' '.join(forbidden)}")
    # A member is a session of its own name: one that exists already (another project's) is never taken over.
    taken = existing_sessions(name, agents) if taken is None else taken
    if taken:
        raise ValueError(f"these names are in use already: {', '.join(taken)}; choose another team name")
    directory = teams_dir() / name
    (directory / "board").mkdir(mode=0o700, parents=True)
    data = {"version": 1, "name": name, "members": agents, "lead": agents[0],
            "workspace": str(Path(workspace).expanduser().resolve()), "options": list(options),
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
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
    for agent in data["members"]:
        role = "lead" if agent == data["lead"] else "member"
        out(f"  {member_name(name, agent):<30} {role:<7} {states[agent]}")
    if board.get("error"):
        out(f"  board: {printable(board['error'])}")
    for task in board["tasks"][-20:]:
        # The board's text comes from the members: never a control sequence on your terminal.
        first = printable(" ".join(task["task"].split())[:70])
        sender = printable(str(task.get("from") or "?")[:64])
        out(f"  #{task['id']:<4} {sender} -> {printable(task['to']):<9} {task['state']:<8} {first}")
    return 0


def post(name, agent, task, out=print):
    data = load(name)
    board = board_module().Board(data["members"], teams_dir() / name / "board")
    ident = board.post(agent, task, sender="human")["id"]
    out(f"sandbox-ai team: task #{ident} posted for {agent}")
    return 0


def stop(name, out=print, run=subprocess.run, states=None):
    """Stop every member that runs (or is paused) and the board; 1 if one of them could not be stopped."""
    data = load(name)
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
    """Stop the team, delete its members (their HOMEs) and its board; the record stays while a member is left."""
    data = load(name)
    if not yes:
        raise ValueError(f"this deletes team {name}, its members' HOMEs and its board; add --yes")
    states = member_states(data) if states is None else states
    if stop(name, out, run, states) != 0:
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


def main(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai team", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="start a team: one sandbox per agent, the first leads")
    begin.add_argument("name")
    begin.add_argument("--agents", required=True, help="claude,codex[,opencode]: the first leads")
    begin.add_argument("-w", "--workspace", default=".", help="the shared workspace (default: this directory)")
    begin.add_argument("options", nargs=argparse.REMAINDER, help="-- launcher options for every member")
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
    a = parser.parse_args(argv)
    try:
        if a.command == "start":
            options = a.options[1:] if a.options[:1] == ["--"] else a.options
            return start(a.name, a.agents.split(","), a.workspace, options, out)
        if a.command == "list":
            for name in names():
                data = load(name)
                out(f"{name:<20} {', '.join(data['members']):<24} {data['workspace']}")
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
