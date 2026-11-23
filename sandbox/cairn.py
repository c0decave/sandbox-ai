#!/usr/bin/env python3
"""Cairn (oritera/Cairn, AGPL-3.0) in sandbox-ai: a part of its own beside the agent (--cairn), reviewed in
docs/CAIRN_REVIEW.md.

Cairn is no agent of sandbox-ai's own: it is a blackboard (Facts, Intents, Hints) with a server, a web UI and a
dispatcher that hands tasks to coding-agent CLIs, its workers. sandbox-ai runs it as a container of its own in the
session's Podman pod, like the sandbox's browser (browser.py) and the services (services.py):

  --cairn                       Cairn's board beside the agent (its image once: sandbox-ai build cairn)
  --cairn-workers AGENT[=MODEL],...  which of the session's agents are Cairn's workers: any harness, the lead or
                                an agent --with (default: every one of them the session has, the best supported
                                first), each with its own model if given (else the agent's own; with --llm the
                                server's); how far each is supported: sandbox-ai cairn workers (cairn_workers.py)
  --cairn-origin TEXT           what is given: the target, the scope (Cairn's origin fact)
  --cairn-goal TEXT             what counts as done (Cairn's goal fact); with the origin, the project is made at
                                the start
  --cairn-hint TEXT             the first hint for the workers (optional)
  --cairn-board fresh|session   the board's database: fresh (the default: new at every start) or kept with the
                                session

The container (POD-service-cairn) of localhost/sandbox-ai-cairn: Cairn at the reviewed commit, unchanged, on a
Debian base pinned by digest. It is started like a service (created, initialised, proven to be in the pod's user
namespace and, with --vpn, behind the VPN's kill switch; after the supervisor's firewall and proxy are up), shares
the pod's network (the agent's firewall, allow-list, proxy, kill switch, VPN), runs as the pod's root without
capabilities on a read-only root, and holds no login, no key and no file of the project. Cairn's server listens on the
pod's loopback (127.0.0.1:8000); the sandbox's browser starts on it (--cairn brings --browser where the live view
works), so the board opens in the app's Browser window.

Its workers are the session's agents, in the agent's container: Cairn's dispatcher runs in its own container in
Cairn's local execution mode, and the claude and codex it starts there are sandbox-ai's shims (cairn_relay.py
worker), which hand the call over a Unix socket to the relay in the agent's container (cairn_relay.py serve, started
inside Firejail with the agent's environment). Claude Code and Codex are called through Cairn's own drivers; every
other harness through Cairn's claudecode driver, the worker's SANDBOX_AI_CAIRN_WORKER naming it, and the relay maps
that driver's command line onto the harness's own one-shot run (cairn_relay.harness_argv) - Cairn is unchanged.
The relay accepts exactly the command lines Cairn's own drivers make
(a prompt, a session to resume, nothing else), in a folder below /work/cairn/runs, and runs the agent's own CLI there:
its login, --api-key, OpenRouter or --llm server (with the loop guard on the wire), its managed settings, hooks and
rules, behind the same firewall and proxy. Never a Docker or Podman socket, never more than the agent has.

Standard library only.
"""
import json
import re
import subprocess
from pathlib import Path

REPOSITORY = "https://github.com/oritera/Cairn"
# main, not a release: the local execution mode (commit 233f5e8505b9, 2026-07-15) came after v0.2.1 (2026-05-10);
# 8e7e0ea6 (2026-09-07, "docs: add logo") is main's newest commit and more than 14 days old.
COMMIT = "8e7e0ea67552383851dfcabfba0c4e9c8d007878"
COMMIT_DATE = "2026-09-07"
VERSION = "0.2.1+8e7e0ea6"  # pyproject.toml's version at that commit, with the commit
LICENSE = "AGPL-3.0"
PYTHON = "3.12"
IMAGE = "localhost/sandbox-ai-cairn:latest"
ROLE = "cairn"  # the image's label sandbox-ai.role (build_image.sh)
BUILD = "sandbox-ai build cairn"
NAME = "cairn"  # the container: POD-service-cairn (services.container_name)
USER = "0:0"  # the pod's root (your user on the host): the relay's folder and the board's stay yours
PORT = 8000
SERVER = f"http://127.0.0.1:{PORT}"  # cairn serve's default address: the pod's loopback
LIMITS = {"memory": "1g", "cpus": "1", "pids": 512}
READY_TIMEOUT = 120
MOUNT = "/sandbox-cairn"  # in its container: the runtime, the relay's client, the configuration, the relay's socket
CONFIG_FILE = "cairn.json"  # in the run's session folder, mounted at MOUNT/config.json
DATA = "/data"  # the board's SQLite database: a tmpfs, or the session's folder (--cairn-board session)
RELAY_DIR = "/sandbox/cairn"  # in the agent's container: the relay's socket (cairn_relay.SOCKET)
RELAY_SOCKET = "relay.sock"
WORKSPACE = "/work/cairn/runs"  # one folder per Cairn project, in the agent's /work
PRESET = "cairn-pentest-board"
LOCAL_PRESET = "cairn-local-llm"
# Cairn's driver for each harness the launcher knows: codex and claude Cairn's own, every other one Cairn's
# claudecode driver, which the relay maps onto that harness's one-shot run. Cairn's own pi driver runs `/bin/sh -lc
# SCRIPT` in local mode: an arbitrary shell line no narrow relay can take, so it is never used - Pi is a worker
# through the claudecode driver like the others.
WORKERS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot",
           "letta", "pi")
DRIVERS = {agent: "codex" if agent == "codex" else "claudecode" for agent in WORKERS}
WORKER_VARIABLE = "SANDBOX_AI_CAIRN_WORKER"  # the harness a worker is, in its environment (cairn_relay.worker)
TEXT_LIMIT = 4000
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
# Its options and their places in the launcher's namespace (--cairn-workers' parsed list is a.cairn_workers).
OPTIONS = {"--cairn-workers": "cairn_workers_text", "--cairn-origin": "cairn_origin", "--cairn-goal": "cairn_goal",
           "--cairn-hint": "cairn_hint", "--cairn-board": "cairn_board"}
BOARDS = ("fresh", "session")
TASKS = {"bootstrap": {"timeout": 300, "conclude_timeout": 60}, "reason": {"timeout": 120},
         "explore": {"timeout": 900, "conclude_timeout": 120}}

_launcher = None
_workers = None


def workers():
    """cairn_workers.py: how each harness fares as Cairn's worker (the traffic light), loaded once."""
    global _workers
    if _workers is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("sandbox_ai_cairn_workers", Path(__file__).with_name(
            "cairn_workers.py"))
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _workers = loaded
    return _workers


def launcher():
    global _launcher
    if _launcher is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_cairn", Path(__file__).with_name(
            "agent_sandbox.py"))
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _launcher = loaded
    return _launcher


def parse_workers(value):
    """[{"agent", "model"}] of --cairn-workers AGENT[=MODEL],...; ValueError for anything else."""
    found = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        agent, given, model = item.partition("=")
        agent = agent.strip()
        if agent not in DRIVERS:
            raise ValueError(f"--cairn-workers: {agent!r} is none of the launcher's harnesses ({', '.join(WORKERS)})")
        if given and not MODEL.fullmatch(model.strip()):
            raise ValueError(f"--cairn-workers {agent}=MODEL: {model!r} is not a model name")
        if given and agent not in workers().MODEL_OPTION:
            raise ValueError(f"--cairn-workers {agent}=MODEL: {agent} has no model option on its command line (its "
                             "model is its settings'; as the lead the start's --model or --llm): give it as "
                             f"--cairn-workers {agent}")
        if agent in {w["agent"] for w in found}:
            raise ValueError(f"--cairn-workers: {agent} is given twice")
        found.append({"agent": agent, "model": model.strip() if given else None})
    if not found:
        raise ValueError(f"--cairn-workers names no worker ({', '.join(WORKERS)})")
    return found


def light_of(agent):
    """How AGENT fares as Cairn's worker (cairn_workers.row): its light, words, evidence, limits."""
    return workers().row(agent)


def refuse_red(worker, agents):
    """A worker whose light is red: ValueError with its reason and another of the session's AGENTS offered."""
    reason = workers().refusal(worker)
    if reason is None:
        return
    others = [agent for agent in agents if agent != worker and agent in DRIVERS and workers().refusal(agent) is None]
    offer = (f"; use another of this session's agents: --cairn-workers {others[0]}" if others else
             "; start the session with another lead, or add --with claude")
    raise ValueError(f"--cairn-workers {worker}: {worker} does not work as Cairn's worker: {reason}{offer}")


def text(value, option):
    """A text of Cairn's project, checked: printable, at most TEXT_LIMIT characters."""
    value = str(value).strip()
    if not value:
        raise ValueError(f"{option} is empty")
    if len(value) > TEXT_LIMIT:
        raise ValueError(f"{option}: at most {TEXT_LIMIT} characters")
    if any(not (c.isprintable() or c in "\n\t") for c in value):
        raise ValueError(f"{option}: control characters are not taken")
    return value


def given(a):
    return [option for option, dest in OPTIONS.items() if getattr(a, dest, None)]


def session_agents(a, l):
    return [a.agent, *l.companions(a)]


def keeps_board(a):
    """Whether this start can keep the board with its session (as the browser's profile: browser.keeps_profile)."""
    if getattr(a, "tmux_socket_dir", None):
        return bool(getattr(a, "state_dir", None))
    return bool(getattr(a, "keep_home", False)) or bool(getattr(a, "start_key", None))


def session_key(a):
    key = getattr(a, "start_key", None)
    if key:
        return key
    if getattr(a, "tmux_socket_dir", None) and getattr(a, "state_dir", None) and getattr(a, "start_id", None):
        return a.start_id
    return None


def worker_choice(l, message, lacking, choice, answers):
    """The refusal MESSAGE as one that carries what would let the start go on (the launcher's fixes.py: a choice of
    the human's between ways to start - asked on a terminal, shown as whole command lines elsewhere). ANSWERS:
    (AGENT, None) - AGENT added beside the lead (--with AGENT) - or (None, [agents]) - those agents of the session as
    Cairn's workers instead (--cairn-workers)."""
    fixes = getattr(l, "FIXES", None)
    if fixes is None:
        return ValueError(message)
    found = []
    for agent, own in answers:
        if agent:
            found.append(fixes.fix(
                "option", f"with-{agent}", lacking, f"add {agent} beside the lead (the team image with both is built "
                "when it is not there)", options={"add": ["--with", agent]}, choice=choice,
                security=f"{agent} runs in the same sandbox, with its own login and its provider's hosts"))
        else:
            found.append(fixes.fix(
                "option", f"cairn-workers-{'-'.join(own)}", lacking,
                f"let the session's own {' and '.join(own)} be Cairn's worker{'s' if len(own) > 1 else ''}",
                options={"remove": ["--cairn-workers"], "add": ["--cairn-workers", ",".join(own)]}, choice=choice))
    return fixes.Missing(message, found)


def check(a, l=None):
    """--cairn and its options, checked; a.cairn_container gets Cairn's container (a member of services.members),
    a.cairn_workers its workers. Without --browser, --cairn brings the sandbox's browser where its live view works
    (a.cairn_browser True): the board opens there. ValueError for whatever the launcher refuses."""
    a.cairn_container, a.cairn_workers, a.cairn_browser = None, [], False
    if not getattr(a, "cairn", False):
        if given(a):
            raise ValueError(f"{given(a)[0]} sets an option of Cairn's board; add --cairn")
        return None
    l = l or launcher()
    if a.backend != "podman":
        raise ValueError("--cairn needs the Podman backend: Cairn's board is a container of its own in the sandbox's "
                         "Podman pod, beside the agent; Bubblewrap has no pod and no container engine")
    if a.podman_url:
        raise ValueError("--cairn with --podman-url: its relay's socket would be on that machine; start it there "
                         "(sandbox-ai hosts run HOST -- ...)")
    if getattr(a, "mcp_socket_dir", None) or getattr(a, "helper_of", None):
        raise ValueError("--cairn is for an agent's start; an MCP server's or helper's sandbox has none")
    agents = session_agents(a, l)
    value = getattr(a, "cairn_workers_text", None)
    a.cairn_left_out = []
    if value:
        chosen = parse_workers(value)
    else:
        # Every agent of the session, the best supported first (the first takes every kind of task); a red one left
        # out (the start says why).
        table = workers().load()
        usable = [agent for agent in agents if agent in DRIVERS]
        a.cairn_left_out = [agent for agent in usable if workers().refusal(agent, table) is not None]
        kept = [agent for agent in usable if agent not in a.cairn_left_out]
        chosen = [{"agent": agent, "model": None}
                  for agent in sorted(kept, key=lambda agent: workers().rank(agent, table))]
        if not chosen and a.cairn_left_out:
            refuse_red(a.cairn_left_out[0], agents)
    for worker in chosen:
        if worker["agent"] not in agents:
            # Offered, not only refused (fixes.py): the agent beside the lead, or the session's own agents that
            # work as Cairn's workers instead. A choice of the human's: never taken by --fix.
            own = [agent for agent in agents if agent in DRIVERS and workers().refusal(agent) is None]
            raise worker_choice(
                l, f"--cairn-workers {worker['agent']}: no agent of this session (the lead "
                   f"{a.agent}{', --with ' + ', '.join(agents[1:]) if agents[1:] else ''}); add --with "
                   f"{worker['agent']}" + (f", or let the session's own {', '.join(own)} work: --cairn-workers "
                                           f"{','.join(own)}" if own else ""),
                f"--cairn-workers {worker['agent']}: {worker['agent']} is no agent of this session (the lead "
                f"{a.agent}{', --with ' + ', '.join(agents[1:]) if agents[1:] else ''})",
                f"cairn-workers-{worker['agent']}", [(worker["agent"], None)] + ([(None, own)] if own else []))
        refuse_red(worker["agent"], agents)
        if worker["model"] and worker["agent"] == a.agent and getattr(a, "llm", None):
            raise ValueError(f"--cairn-workers {worker['agent']}={worker['model']}: with --llm the model is the "
                             f"server's (--llm NAME:MODEL)")
    origin = text(a.cairn_origin, "--cairn-origin") if getattr(a, "cairn_origin", None) else None
    goal = text(a.cairn_goal, "--cairn-goal") if getattr(a, "cairn_goal", None) else None
    hint = text(a.cairn_hint, "--cairn-hint") if getattr(a, "cairn_hint", None) else None
    if bool(origin) != bool(goal):
        raise ValueError("--cairn-origin and --cairn-goal go together: Cairn's project starts from its origin and "
                         "its goal (or give neither and make the project on the board)")
    if hint and not goal:
        raise ValueError("--cairn-hint is the first hint of the project --cairn-origin and --cairn-goal make")
    board = getattr(a, "cairn_board", None) or "fresh"
    if board not in BOARDS:
        raise ValueError(f"--cairn-board is fresh or session: {board!r}")
    if board == "session" and not keeps_board(a):
        raise ValueError("--cairn-board session keeps the board with the session, but this start keeps none (its "
                         "HOME is discarded or your own --state-dir): use fresh")
    if PORT in l.host_ports(a):
        raise ValueError(f"--cairn: Cairn's board listens on port {PORT}, which is a port of the host here "
                         f"(--allow-host-port {PORT})")
    if a.agent == "openhands":
        raise ValueError(f"--cairn: OpenHands' Agent Canvas holds port {PORT} of the sandbox's loopback")
    for service in getattr(a, "services", None) or []:
        if service["name"] == NAME:
            raise ValueError(f"--service {NAME}: that name is Cairn's board's (--cairn); name it otherwise")
        if PORT in service.get("ports", []):
            raise ValueError(f"--service {service['name']}: port {PORT} is Cairn's board's (--cairn)")
    if not getattr(a, "browser", None) and not l.on_machine(a):
        a.browser, a.cairn_browser = "chromium", True  # its board opens there (browser.check comes next)
    a.cairn_workers = chosen
    a.cairn_project = {"title": f"sandbox-ai: {origin.splitlines()[0][:80]}", "origin": origin, "goal": goal,
                       "hints": [{"content": hint, "creator": "human"}] if hint else []} if goal else None
    a.cairn_container = {
        "name": NAME, "cairn": True, "image": ROLE, "user": USER, "ports": [PORT], "ready": PORT,
        "ready_timeout": READY_TIMEOUT, "env": [], "secrets": [], "data": [], "keep": False, "read_only": True,
        "board": board, **LIMITS, "record": {"image": IMAGE, "id": None, "kind": "sandbox-ai"}}
    return a.cairn_container


def notes(a):
    """What a start with Cairn says about it before it runs."""
    if not getattr(a, "cairn_container", None):
        return []
    names = ", ".join(w["agent"] + (f" ({w['model']})" if w["model"] else "") for w in a.cairn_workers)
    found = [f"cairn: Cairn's board in its own container beside the agent, at {SERVER} on the pod's loopback; its "
             f"workers are this session's {names}, run in the agent's container through sandbox-ai's relay (their "
             "logins, models, rules and network; never a Docker or Podman socket)"]
    # How far each worker is supported: its traffic light, what it comes from, what Cairn expects that it cannot give.
    for worker in a.cairn_workers:
        row = light_of(worker["agent"])
        found.append(f"cairn: worker {workers().summary(row)}"
                     + "".join(f"; {limit}" for limit in row["limits"]))
    for agent in getattr(a, "cairn_left_out", None) or []:
        found.append(f"cairn: {agent} is not one of Cairn's workers here: {workers().refusal(agent)} "
                     f"(sandbox-ai cairn workers {agent})")
    found += [
             "cairn: once a project is active, Cairn hands out tasks on its own until it is complete or stopped, and "
             "every task spends your models' usage; sandbox-ai pauses Cairn's dispatcher after 5 failed tasks within "
             "10 minutes and the workers after 5 failed runs in a row (60 seconds, doubling to 15 minutes). Stop a "
             "project on the board to stop it",
             "cairn: Cairn's workers run without their own approval questions (Cairn gives claude "
             "--dangerously-skip-permissions and codex --dangerously-bypass-approvals-and-sandbox; every other "
             "harness gets its own switch from sandbox-ai's relay): this sandbox's firewall, proxy, approvals and "
             "rules hold",
             "cairn: AGPL-3.0, built unchanged from the reviewed commit by sandbox-ai build cairn on your machine"]
    if getattr(a, "cairn_browser", False):
        found.append("cairn: the sandbox's browser starts on Cairn's board (--browser came with --cairn): the app's "
                     "Browser button shows it")
    elif getattr(a, "browser_container", None):
        found.append("cairn: the sandbox's browser starts on Cairn's board (the app's Browser button)")
    else:
        found.append(f"cairn: no browser shows the board here (the live view of the sandbox's browser is Linux-only "
                     f"for now); the agent reaches it at {SERVER}")
    return found


def relay_dir(a, l=None):
    """The relay's folder on the host (Linux: beside the run's directory, only yours), or None on macOS, where a
    socket does not cross podman machine's shared folders (a Podman volume of the run instead: relay_volume)."""
    l = l or launcher()
    if l.on_machine(a):
        return None
    if getattr(a, "control_dir", None):
        return Path(a.control_dir).with_name(Path(a.control_dir).name + ".cairn")
    return l.runs_dir() / f"{a.run_id}.cairn"


def relay_volume(a):
    """macOS: the Podman volume (in the VM) that holds the relay's socket for both containers."""
    return f"sandbox-ai-cairn-{re.sub(r'[^A-Za-z0-9_.-]', '-', str(a.run_id))[:64]}"


def board_dir(a, l=None):
    """The host folder of a kept board (--cairn-board session: the session's folder/cairn, beside its HOME), or
    None (fresh: a tmpfs in its container; or not known yet)."""
    if (getattr(a, "cairn_board", None) or "fresh") != "session":
        return None
    key = session_key(a)
    if not key:
        return None
    return (l or launcher()).load_sessions_module().session_dir(key) / "cairn"


def relay_mount(a, l=None):
    """The podman option that puts the relay's folder at TARGET: a bind of the host folder (Linux) or the run's
    volume (macOS); returned as a function of (target, mode)."""
    l = l or launcher()
    folder = relay_dir(a, l)
    if folder is None:
        return lambda target, mode: ["--mount", f"type=volume,src={relay_volume(a)},dst={target}"
                                     + (",ro" if mode == "ro" else "")]
    return lambda target, mode: l.mount_option(folder, target, mode)


def prepare_folders(a, l=None):
    """At a real start: the relay's folder (0700; removed with the run by the caller) and a kept board's folder.
    Returns the relay's folder (None on macOS)."""
    folder = relay_dir(a, l)
    if folder is not None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
    board = board_dir(a, l)
    if board is not None:
        board.mkdir(mode=0o700, parents=True, exist_ok=True)
    a.cairn_relay_dir = folder
    return folder


def dispatch(a):
    """Cairn's dispatcher configuration (JSON, which its YAML loader reads): local execution only, workers the
    session's agents through the relay, one project at a time."""
    found = []
    for index, worker in enumerate(a.cairn_workers):
        found.append({"name": f"sandbox-{worker['agent']}", "type": DRIVERS[worker["agent"]],
                      # The first takes every kind of task; the others explore (Cairn's own example's split).
                      "task_types": ["bootstrap", "reason", "explore"] if index == 0 else ["explore"],
                      "max_running": 1, "priority": index,
                      # The harness it is: the shim tells the relay (Cairn hands a worker's env to its process).
                      "env": {WORKER_VARIABLE: worker["agent"]}})
    count = len(found)
    return {"server": SERVER,
            "runtime": {"execution": "local", "worker_healthcheck": "disabled", "max_workers": count,
                        "max_running_projects": 1, "max_project_workers": count, "interval": 5,
                        "healthcheck_timeout": 15, "prompt_group": "default"},
            "tasks": json.loads(json.dumps(TASKS)),
            "local": {"workspace_root": WORKSPACE, "completed_action": "keep"},
            "workers": found}


def config(a):
    """MOUNT/config.json of Cairn's runtime (cairn_runtime.py)."""
    return {"port": PORT, "data": DATA, "workers": [w["agent"] for w in a.cairn_workers],
            "socket": f"{MOUNT}/relay/{RELAY_SOCKET}", "dispatch": dispatch(a),
            "project": getattr(a, "cairn_project", None)}


def worker_arguments(a, worker, l=None):
    """The arguments sandbox-ai puts before a worker's own (cairn_relay.command): its model of --cairn-workers; for
    the lead without one what its start gives it (--model, --effort, Codex's --llm profile: model_effort_args,
    llm.agent_arguments), so that a worker is the session's agent as it was started."""
    l = l or launcher()
    # Codex's telemetry overrides, as the lead's command line has them: they win over every config.toml.
    telemetry = list(l.codex_telemetry_args(l.telemetry_level(a))) if worker["agent"] == "codex" else []
    if worker["model"]:
        return [*telemetry, "--model", worker["model"]]
    if worker["agent"] != a.agent:
        return telemetry  # an agent --with: its own login and default model, as from the shell
    found = list(l.model_effort_args(a))
    if getattr(a, "llm", None):
        found = [*l.load_llm_module().agent_arguments(a), *found]
    return [*telemetry, *found]


def relay_config(a, l=None):
    """The agent container's relay (cairn_relay.serve; the supervisor's config.json "cairn")."""
    if not getattr(a, "cairn_container", None):
        return None
    return {"socket": f"{RELAY_DIR}/{RELAY_SOCKET}", "workspace": WORKSPACE,
            "workers": {w["agent"]: worker_arguments(a, w, l) for w in a.cairn_workers}}


def write_config(a, session):
    path = Path(session) / CONFIG_FILE
    path.write_text(json.dumps(config(a)))
    path.chmod(0o644)
    return path


def image_id(a, l=None, run=None):
    """The ID of sandbox-ai's Cairn image in this machine's Podman, or None (not there, or not sandbox-ai's: its
    label sandbox-ai.role must be cairn)."""
    l = l or launcher()
    run = run or subprocess.run
    try:
        done = run(l.podman_command(a, "image", "inspect", "--format",
                                    '{{.Id}} {{index .Labels "sandbox-ai.role"}}', IMAGE),
                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ValueError("Required tool missing: podman") from None
    words = (done.stdout or "").split()
    if done.returncode != 0 or len(words) != 2 or words[1] != ROLE or \
            not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", words[0]):
        return None
    return words[0].removeprefix("sha256:")


def prepare(a, l=None, run=None):
    """At a real start: Cairn's image is there; its ID is what the container is made of. ValueError naming the build
    otherwise."""
    container = getattr(a, "cairn_container", None)
    if not container:
        return None
    found = image_id(a, l, run)
    if found is None:
        raise ValueError(f"--cairn: sandbox-ai's Cairn image {IMAGE} is not there (nothing is pulled at a start); "
                         f"build it once: {BUILD}")
    container["record"]["id"] = found
    return found


HERE = Path(__file__).resolve().parent


def mounts(a, session, l=None):
    """[(host path, container path, mode)] of Cairn's container (the relay's folder apart: relay_mount)."""
    session = Path(session)
    found = [(session / "resolv.conf", "/etc/resolv.conf", "ro"), (session / "hosts", "/etc/hosts", "ro"),
             (HERE / "cairn_runtime.py", f"{MOUNT}/cairn_runtime.py", "ro"),
             (HERE / "pid1.py", f"{MOUNT}/pid1.py", "ro"),  # its PID 1: sandbox-ai's own init, nothing of the host
             (HERE / "cairn_relay.py", f"{MOUNT}/cairn_relay.py", "ro"),
             (session / CONFIG_FILE, f"{MOUNT}/config.json", "ro")]
    board = board_dir(a, l)
    if board is not None:
        found.append((board, DATA, "rw"))
    return found


def create_command(a, pod, service, session, l=None, limits=None):
    """(podman create command, environment) of Cairn's container in POD: created only (services.launch proves its
    namespaces before it starts it)."""
    l = l or launcher()
    services = l.load_services_module()
    limits = services.enforceable(a, l) if limits is None else limits
    image = (service.get("record") or {}).get("id")
    if not image:
        raise ValueError(f"Cairn's image was not checked ({BUILD})")
    argv = l.podman_command(a, "create", "--pull=never", "--pod", pod, "--name", services.container_name(pod, service),
                            "--label", f"{l.RUN_LABEL}={a.run_id}", "--label", f"{services.SERVICE_LABEL}={NAME}",
                            "--user", USER, "--cap-drop=ALL", "--security-opt=no-new-privileges",
                            # (no --init: that is the host's catatonit, which a host need not have - its PID 1 is
                            # sandbox-ai's own init, pid1.py, run by the image's Python: the entrypoint below)
                            "--read-only", "--log-driver=k8s-file", "--log-opt", "max-size=10mb",
                            # /tmp holds the workers' shims (cairn_runtime.shims), which Cairn's dispatcher runs.
                            "--image-volume=tmpfs", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m,mode=1777",
                            # The dispatcher's project folders (the workers' are in the agent's /work).
                            "--tmpfs", "/work:rw,noexec,nosuid,nodev,size=16m",
                            "--env", "HOME=/tmp/home")
    if board_dir(a, l) is None:
        argv += ["--tmpfs", f"{DATA}:rw,noexec,nosuid,nodev,size=256m"]
    if "memory" in limits:
        argv += ["--memory", service["memory"]]
    if "cpus" in limits:
        argv += ["--cpus", str(service["cpus"])]
    if "pids" in limits:
        argv += ["--pids-limit", str(service["pids"])]
    for source, target, mode in mounts(a, session, l):
        argv += l.mount_option(source, target, mode)
    argv += relay_mount(a, l)(f"{MOUNT}/relay", "ro")
    return argv + ["--entrypoint", "/opt/cairn/venv/bin/python", image, "-I", f"{MOUNT}/pid1.py",
                   "/opt/cairn/venv/bin/python", "-I", f"{MOUNT}/cairn_runtime.py"], {}


def agent_mounts(a, l=None):
    """The podman options of the agent's container for the relay: its program and its folder (rw: it makes the
    socket there)."""
    if not getattr(a, "cairn_container", None):
        return []
    l = l or launcher()
    return [*l.mount_option(HERE / "cairn_relay.py", "/sandbox/cairn_relay.py", "ro"),
            *relay_mount(a, l)(RELAY_DIR, "rw")]


def plan(a, l=None):
    """The dry run's account of Cairn, or None without it."""
    if not getattr(a, "cairn_container", None):
        return None
    l = l or launcher()
    pod = l.pod_name(a, getattr(a, "run_id", None))
    board = (getattr(a, "cairn_board", None) or "fresh")
    return {"container": f"{pod}-service-{NAME}", "image": IMAGE, "build": BUILD, "commit": COMMIT,
            "licence": LICENSE, "user": USER, "board": SERVER, "tcp_ports": [PORT],
            "network": "the pod's own, which is the agent's: its firewall, allow-list, connection proxy, approvals, "
                       "kill switch and VPN or Tor; Cairn itself reaches nothing",
            "capabilities": "none (--cap-drop=ALL, no-new-privileges)", "read_only": True, "limits": dict(LIMITS),
            "data": {"kind": board, "where": "a tmpfs in its container: new at every start, gone with the run"
                     if board == "fresh" else str(board_dir(a, l) or "the session's folder (cairn), beside its HOME")},
            "workers": [{"agent": w["agent"], "driver": DRIVERS[w["agent"]],
                         "model": w["model"] or ("the --llm server's" if w["agent"] == a.agent and
                                                 getattr(a, "llm", None) else "the agent's own"),
                         "arguments": worker_arguments(a, w, l), **plan_light(w["agent"])}
                        for w in a.cairn_workers],
            "relay": {"agent_socket": f"{RELAY_DIR}/{RELAY_SOCKET}",
                      "host": str(relay_dir(a, l)) if relay_dir(a, l) else f"the Podman volume {relay_volume(a)}",
                      "workspace": WORKSPACE,
                      "takes": "only the command lines of Cairn's claudecode and codex drivers (a prompt, a session); "
                               "a harness other than Claude Code and Codex runs its own one-shot command line, which "
                               "the relay makes; never a shell line, a Docker or Podman socket"},
            "dispatch": dispatch(a), "project": getattr(a, "cairn_project", None),
            "browser": "starts on the board" if getattr(a, "browser_container", None) or
            getattr(a, "cairn_browser", False) else None}


def plan_light(agent):
    """A worker's light in the dry run: its colour, status, the command line a task runs, whether it is mapped."""
    row = light_of(agent)
    return {"light": row["light"], "status": row["status"], "run": row["run"], "mapped": row["mapped"],
            "limits": row["limits"]}


def record(a, pod):
    """Cairn in run.json: sandbox-ai list and the app show it."""
    if not getattr(a, "cairn_container", None):
        return None
    return {"board": SERVER, "container": f"{pod}-service-{NAME}", "workers": [w["agent"] for w in a.cairn_workers]}


def agent_environment(a):
    """What the agent is told of Cairn (the sandbox-ai skill explains it): its board's address."""
    return {"SANDBOX_AI_CAIRN": SERVER} if getattr(a, "cairn_container", None) else {}


def browser_start(a):
    """The URL the sandbox's browser starts on: Cairn's board, else None."""
    return f"{SERVER}/" if getattr(a, "cairn_container", None) else None
