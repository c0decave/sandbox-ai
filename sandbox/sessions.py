#!/usr/bin/env python3
"""Detachable sandbox sessions: start, attach, pause, resume, stop.

Every interactive start is one (named automatically, or with --name/--session). A session runs the normal
launcher as a background supervisor. Inside the sandbox the guard starts a detached tmux session (windows
"agent" and "shell"), so terminals can attach and detach while the agent keeps working; closing the terminal
detaches.
State lives in $XDG_STATE_HOME/sandbox-ai/sessions/ID (ID: the session's first name; after a rename,
session.json's "name" is its name):

  session.json     agent, launcher options, name, supervisor PID, pod name
  home/            persistent sandbox HOME (--state-dir), when the session keeps it ("keep")
  tmux/            tmux socket directory, bound at /sandbox/tmux
  supervisor.log   launcher output
  paused           present while the session is frozen
  exit.json        run, exit status and end of its last run (written by the launcher)

A session that does not keep its HOME is removed once it has ended; its history stays (starts.py).
"""
import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import secrets
import tempfile
import time
from types import SimpleNamespace
import threading

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
READY_TIMEOUT = 120
DETACH_GRACE = 3   # seconds the terminal relay may take to go once the agent ended
ENDING_WAIT = 60   # seconds a supervisor may take to clean up after its agent ended
# Restarting a stopped session continues the agent's most recent conversation.
CONTINUE_ARGS = {"claude": ["--continue"], "codex": ["resume", "--last"], "opencode": ["--continue"]}
# Agent arguments that run one task and exit; repeating them on a restart would run it again.
ONE_SHOT_ARGS = {"-p", "--print", "exec", "e", "run"}
# Options that take a value, per agent; anything not listed counts as a switch.
VALUE_OPTIONS = {
    "claude": {"--model", "--fallback-model", "--effort", "--add-dir", "--permission-mode", "--allowedTools",
               "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--append-system-prompt",
               "--system-prompt", "--mcp-config", "--settings", "--agents", "--output-format", "--input-format"},
    "codex": {"--model", "-m", "--sandbox", "-s", "--profile", "-p", "--config", "-c", "--cd", "-C",
              "--ask-for-approval", "-a", "--image", "-i", "--add-dir"},
    "opencode": {"--model", "-m", "--agent", "--log-level", "--port", "--hostname"},
}
# Options that pick the conversation themselves; the restart adds its own continue arguments.
SESSION_OPTIONS = {"claude": {"--continue", "-c", "--resume", "-r", "--session-id"},
                   "codex": set(), "opencode": {"--continue", "-c", "--session", "-s"}}
_launcher = None
_starts = None


def starts():
    """starts.py (names, history), sharing this module's launcher."""
    global _starts
    if _starts is None:
        spec = importlib.util.spec_from_file_location("sandbox_starts_for_sessions", HERE / "starts.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _starts is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded._modules["agent_sandbox"] = launcher()
                _starts = loaded
    return _starts


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_sessions", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


_liveness = None


def liveness():
    """liveness.py: whether a session's last run still runs without its supervisor (its Podman pod)."""
    global _liveness
    if _liveness is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_liveness_for_sessions", HERE / "liveness.py")
        with LOADING:
            if _liveness is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _liveness = loaded
    return _liveness


def root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "sessions"


def session_dir(name):
    """The directory of the session whose id (its first name) is NAME."""
    if not isinstance(name, str) or not re.fullmatch(launcher().START_ID, name):
        raise ValueError(f"Invalid session name: {launcher().printable(str(name))!r}")
    return root() / name


def display(key, data):
    """The name of the session KEY: session.json's "name" (after a rename), else its id."""
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) and re.fullmatch(launcher().SESSION_NAME, name) else key


def resolve(name):
    """The id of the session called NAME: by its name first, then by its id; ValueError if there is none."""
    if not isinstance(name, str):
        raise ValueError("No session named None")
    by_id = None
    if root().is_dir():
        for directory in sorted(root().iterdir()):
            if directory.is_symlink() or not re.fullmatch(launcher().START_ID, directory.name):
                continue
            try:
                data = json.loads((directory / "session.json").read_text())
            except (OSError, ValueError):
                data = None
            if data is not None and display(directory.name, data) == name:
                return directory.name
            if directory.name == name and (directory / "session.json").exists():
                by_id = name
    if by_id is None:
        raise ValueError(f"No session named {launcher().printable(name)}")
    return by_id


def new_key(name):
    """The id for a new session called NAME: NAME, or NAME-xxxx when a renamed session has that directory."""
    key = name
    while (root() / key).exists() or (root() / key).is_symlink():
        key = f"{name}-{secrets.token_hex(2)}"
    return key


def rename(key, name):
    """Call the session KEY NAME from now on (its id, directory and pod stay)."""
    with session_lock(key):
        data = load(key)
        data["name"] = name
        save(key, data)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def activity_event(name, event):
    """Note EVENT (pause, resume, attach, detach) of the running session NAME in its timeline (activity.py;
    never fails)."""
    try:
        spec = importlib.util.spec_from_file_location("sandbox_ai_activity_for_sessions", HERE / "activity.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.session_event(name, event, launcher().runs_dir())
    except Exception:  # noqa: BLE001 - the session command matters more than its timeline
        return False


def load(name):
    path = session_dir(name) / "session.json"
    if not path.is_file():
        raise ValueError(f"No session named {launcher().printable(name)}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):  # valid JSON of another shape is as damaged as a broken file (ValueError)
        raise ValueError(f"the record of session {launcher().printable(name)} is damaged: {path}")
    return data


def save(name, data):
    directory = session_dir(name)
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=directory)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
    os.replace(temporary, directory / "session.json")


# What names a start (or says whether it keeps its HOME) rather than configures its sandbox: not saved with its
# options; the switches take no value.
START_OPTIONS = ("--session", "--name", "--project-name")
START_SWITCHES = ("--keep", "--no-keep")


def split_argv(argv):
    """Launcher options without the agent, the naming options (START_OPTIONS) and agent arguments."""
    options, agent_args = list(argv[1:]), []
    if "--" in options:
        position = options.index("--")
        options, agent_args = options[:position], options[position + 1:]
    kept, skip = [], False
    for value in options:
        if skip:
            skip = False
        elif value in START_OPTIONS:
            skip = True
        elif value not in START_SWITCHES and not value.startswith(tuple(option + "=" for option in START_OPTIONS)):
            kept.append(value)
    return kept, agent_args


def alive(name, data):
    """The supervisor PID still runs this session's launcher (not a reused PID). A start in the foreground (a
    task) is its own launcher: the recorded PID of that start time, running agent_sandbox.py."""
    cmdline = launcher().process_cmdline(data.get("pid"), data.get("pid_start"), PROC)
    if foreground(data):
        # The launcher's own file by name (not "test_agent_sandbox.py" or any path merely ending like it).
        return data.get("pid_start") is not None and any(os.path.basename(arg) == b"agent_sandbox.py"
                                                         for arg in cmdline)
    return os.fsencode(str(session_dir(name) / "tmux")) in cmdline


def foreground(data):
    """A start that runs (or last ran) in the foreground of a terminal or script: no tmux, nothing to attach."""
    return data.get("mode") == "foreground"


def status(name, data):
    """running, paused, stopped, or orphaned: its supervisor is gone (killed without its cleanup) but its Podman pod
    still runs the sandbox (lost). An orphaned session is attached to as a running one; `stop` ends its pod and
    records the end, after which it is resumed as any stopped one."""
    if not alive(name, data):
        return "orphaned" if lost(name, data) else "stopped"
    return "paused" if (session_dir(name) / "paused").exists() else "running"


def lost(name, data):
    """The session's last run still runs without its supervisor: True when its Podman pod (session.json "pod") runs
    a container although the supervisor is gone, False when it does not (or the run's end was recorded: exit.json
    of that run; or no Podman pod), None when Podman does not answer. Asked only once the supervisor is gone."""
    pod = data.get("pod") if isinstance(data, dict) else None
    if data.get("backend") != "podman" or not isinstance(pod, str) or not liveness().POD_NAME.fullmatch(pod):
        return False
    if ended_run(name, data):
        return False
    url = data.get("podman_url") if isinstance(data.get("podman_url"), str) else None
    return liveness().pod_live(pod, url)


def ended_run(name, data):
    """exit.json records the end of the session's current run (its launcher, or `stop`, wrote it)."""
    try:
        ended = json.loads((session_dir(name) / "exit.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(ended, dict) and ended.get("run") == current_run(name, data)


def descendants(pid):
    children = {}
    for child, (ppid, _, _) in launcher().procinfo().table(PROC).items():  # /proc; sysctl on macOS
        children.setdefault(ppid, []).append(child)
    order, queue = [], [pid]
    while queue:
        current = queue.pop(0)
        for child in sorted(children.get(current, [])):
            if mcp_supervisor(child):
                continue  # shared with other agents: never frozen or signalled with this session
            order.append(child)
            queue.append(child)
    return order


def mcp_supervisor(pid):
    """An MCP server supervisor started from this session; it serves other agents too."""
    return b"--mcp-socket-dir" in (launcher().procinfo().cmdline(pid, PROC) or [])


def signal_tree(pid, sig, include_root=True):
    """SIG to PID and its descendants (not MCP servers); without INCLUDE_ROOT to the descendants only (a
    launcher in a terminal must not stop itself: the shell would take the terminal back)."""
    # Parents first, so a stopped parent cannot fork unseen children; a
    # second pass catches anything created during the first.
    for _ in range(2 if sig == signal.SIGSTOP else 1):
        for target in [*([pid] if include_root else []), *descendants(pid)]:
            try:
                os.kill(target, sig)
            except ProcessLookupError:
                pass


def restart_args(data, config_args=(), binary=False):
    """Agent arguments for restarting a stopped session.

    The original agent arguments (from the command line, else the project configuration's
    agent_args) are kept before the continue arguments when they are options, such as
    --model sonnet or Codex's --sandbox read-only; one-shot tasks and prompts are not.
    BINARY: the project configuration sets --binary."""
    # A custom --binary is not an agent CLI; repeat its original arguments.
    if binary or any(value == "--binary" or value.startswith("--binary=") for value in data["options"]):
        return data["agent_args"] or list(config_args)
    # The first start never came up: there is no conversation to continue.
    if data.get("ready") is False:
        return data["agent_args"]
    base = list(data["agent_args"]) or list(config_args)
    one_shot = base[:1] in (["exec"], ["e"]) if data["agent"] == "codex" else bool(ONE_SHOT_ARGS & set(base))
    if one_shot:
        return CONTINUE_ARGS[data["agent"]]
    return leading_options(data["agent"], base) + CONTINUE_ARGS[data["agent"]]


def leading_options(agent, args):
    """The options at the start of ARGS with their values, up to the first prompt or subcommand.
    Conversation-selecting options are dropped; an unknown option right before a positional
    argument is dropped too, since that argument may have been its value."""
    kept, index = [], 0
    values, sessions = VALUE_OPTIONS[agent], SESSION_OPTIONS[agent]
    while index < len(args):
        arg = args[index]
        if arg == "--" or not arg.startswith("-") or arg == "-":
            break
        name = arg.split("=", 1)[0]
        takes_value = name in values and "=" not in arg
        has_next = index + 1 < len(args) and not args[index + 1].startswith("-")
        if name in sessions:
            # --resume [ID]: skip its optional value as well.
            index += 2 if has_next and name in ("--resume", "-r", "--session-id", "--session", "-s") else 1
            continue
        if takes_value:
            if index + 1 >= len(args):
                break
            kept += [arg, args[index + 1]]
            index += 2
            continue
        if has_next and name not in values and "=" not in arg:
            # A switch followed by a positional argument: the argument is a prompt, unless the
            # switch is an option we do not know that takes a value; keep neither to be safe.
            break
        kept.append(arg)
        index += 1
    return kept


@contextlib.contextmanager
def names_lock():
    """Exclusive, waiting lock over every start's choice of a name: the check that a name is free and the
    session.json that takes it are one step (two starts with the same --name at once: the second is refused)."""
    root().mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root() / ".names.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def claim_name(key, name, data):
    """Save DATA as session KEY's session.json, NAME being free for it: under names_lock, checked again against
    every other start's name (a start of the same name may have saved its own since the name was first checked;
    another id of that name is fine, new_key chose this one beside it)."""
    with names_lock():
        for entry in starts().entries():
            if entry.get("id") != key and entry.get("name") == name and entry.get("state") != "damaged":
                raise ValueError(f"The name {launcher().printable(name)} was taken meanwhile; choose another")
        save(key, data)


@contextlib.contextmanager
def session_lock(name, create=False):
    """Exclusive lock for commands that change a session's state; a second one fails at once.

    Not inherited by the supervisor (Python opens files close-on-exec), so it ends with the command."""
    directory = session_dir(name)
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not directory.is_dir():
        raise ValueError(f"No session named {launcher().printable(name)}")
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"Session {launcher().printable(name)} is being started or stopped by another "
                             "command; try again in a moment") from None
        yield
    finally:
        os.close(fd)


def attach_if_terminal(name):
    if sys.stdin.isatty() and sys.stdout.isatty():
        return attach(name)
    return 0


def keeps_home(data):
    """The session keeps its HOME (sessions of versions before 0.1.30 always do)."""
    return data.get("keep", True) is not False


def relay_listening(socket_path):
    """The sandbox's terminal relay still accepts connections (the sandbox runs). The socket is checked like
    attach_client does: the directory is writable from the sandbox."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        fd = os.open(socket_path, os.O_PATH | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                return False
            client.settimeout(2)
            client.connect(f"/proc/self/fd/{fd}")
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
    finally:
        client.close()


def machine_relay_listening(data, runner=None):
    """macOS: the terminal relay in podman machine's VM accepts (runtime.py --internal-attach-probe through podman
    exec; its socket does not cross the VM's shared folders)."""
    try:
        done = (runner or subprocess.run)(launcher().machine_exec(data, ["--internal-attach-probe",
                                                                          launcher().ATTACH_SOCKET], interactive=False),
                                          stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return done.returncode == 0


def terminal_listening(name, data):
    """The session's terminal relay still accepts connections (the sandbox runs)."""
    if launcher().machine_record(data):
        return machine_relay_listening(data)
    return relay_listening(session_dir(name) / "tmux" / "attach.sock")


def begin(a, argv):
    """A kept start in the foreground (a task, no terminal, --no-tmux): its directory, HOME and session.json,
    made by the launcher that runs it right here (plan_kept_home chose a.start_key and a.state_dir)."""
    key = a.start_key
    directory = session_dir(key)
    with session_lock(key, create=True):
        if (directory / "session.json").exists():
            raise ValueError(f"The name {launcher().printable(a.start_name)} was taken meanwhile; choose another")
        for path in (directory, directory / "home"):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        options, agent_args = split_argv(argv)
        pid = os.getpid()
        data = {"agent": a.agent, "options": options, "agent_args": agent_args, "backend": a.backend,
                "podman_url": a.podman_url, "workspace": a.workspace, "cwd": os.getcwd(), "created": now(),
                "cwd_project": getattr(a, "cwd_project", None),
                "ready": True, "name": a.start_name, "project": launcher().project_label(a), "keep": True,
                "mode": "foreground", "pid": pid, "pid_start": launcher().process_start(pid, PROC), "started": now(),
                "pid_ns": liveness().pid_namespace(),
                "run": a.run_id, "pod": launcher().pod_name(a) if a.backend == "podman" else None}
        claim_name(key, a.start_name, data)


def discard_unstarted(a):
    """The launcher's cleanup of a kept start in the foreground: if it never got as far as running (refused or
    failed before it registered), its directory goes again; it was never a start to come back to."""
    if getattr(a, "control_dir", None) is None and getattr(a, "start_key", None):
        shutil.rmtree(session_dir(a.start_key), ignore_errors=True)


def exit_status(name, data):
    """The exit status of the session's last run (exit.json, written by its launcher), or None."""
    try:
        ended = json.loads((session_dir(name) / "exit.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(ended, dict) or ended.get("run") != current_run(name, data):
        return None
    status = ended.get("status")
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def prune(name, locked=False):
    """Remove the session NAME if it has ended and does not keep its HOME (its history stays); True if
    removed. Under the session's lock (LOCKED: the caller holds it): a session being started (saved, no
    supervisor yet) holds it and looks stopped, so it is left alone, never removed under its starting
    supervisor."""
    def remove():
        data = load(name)
        if keeps_home(data) or status(name, data) != "stopped":
            return False
        if not alive(name, data) and (lost(name, data) is None
                                      or not liveness().same_namespace(data.get("pid_ns"))):
            return False  # Podman did not answer, or its supervisor's PIDs are not visible here: it may still run
        shutil.rmtree(session_dir(name), onerror=make_writable_and_retry)
        return True
    try:
        if locked:
            return remove()
        # A first look without the lock: what keeps its HOME or runs is not locked at all (sandbox-ai list and the
        # app look at every session often; a start, stop or pause of one at that moment would be refused as busy).
        data = load(name)
        if keeps_home(data) or status(name, data) != "stopped":
            return False
        with session_lock(name):
            return remove()
    # gone, damaged (a session.json that is no object included), or being started or stopped by another command
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def after_attach(name):
    """The terminal came back from the session NAME: either it detached (the sandbox runs on; a hint how to come
    back, 0), or the agent ended (wait for the supervisor's cleanup; its exit status)."""
    data = load(name)
    shown = launcher().printable(display(name, data))
    if not alive(name, data) and lost(name, data):
        print(f"agent-sandbox: detached from {shown}; it runs on without its supervisor. End it with: sandbox-ai stop "
              f"{shown}, then continue it with: sandbox-ai resume {shown}", file=sys.stderr)
        return 0
    deadline = time.monotonic() + DETACH_GRACE
    while alive(name, data) and terminal_listening(name, data):
        if time.monotonic() >= deadline:
            print(f"agent-sandbox: detached from {shown}; it keeps running. Back: sandbox-ai attach {shown}, "
                  f"end it: sandbox-ai stop {shown}", file=sys.stderr)
            return 0
        time.sleep(0.5)
    deadline = time.monotonic() + ENDING_WAIT
    while alive(name, data) and time.monotonic() < deadline:
        time.sleep(0.2)
    if alive(name, data):
        print(f"agent-sandbox: {shown} is still cleaning up; see: sandbox-ai list", file=sys.stderr)
        return 0
    result = exit_status(name, data)
    prune(name)
    return result or 0


def run_options(data):
    """The options a session's next run is launched with: its saved options, with the main workspace of its first
    start kept. A session records the folder that was /work because the directory it started in held a project file
    ("cwd_project"); its runs pass that first and --workspaces-as-given, so that a project file that appears later in
    the saved working directory (one the agent may have written) never takes the session over. A record from before
    (no "cwd_project") runs as it always did."""
    options = list(data["options"])
    if "cwd_project" not in data or "--workspaces-as-given" in options:
        return options
    first = data.get("cwd_project")
    return [*(["--workspace", first] if isinstance(first, str) and first else []), *options, "--workspaces-as-given"]


def parsed(data):
    """Backend and Podman service of the session's running sandbox, as the launcher used them,
    including values from the project configuration."""
    return SimpleNamespace(backend=data["backend"], podman_url=data.get("podman_url"))


@contextlib.contextmanager
def in_directory(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def effective(data):
    """(launcher arguments, project document) of a session as the launcher will see them at its next
    start: saved options plus the trusted project configuration, resolved from the session's directory."""
    l = launcher()
    argv = [data["agent"], *run_options(data)]
    try:
        with in_directory(data["cwd"]):
            a = l.parse_args(argv)
            _, document = l.load_project_config(a)
            if document:
                a = l.parse_args(l.merge_config(argv, a, document))
    except (OSError, ValueError, SystemExit):
        return None, None  # the launcher reports the problem when it starts
    return a, document


def wait_ready(directory, process, timeout=READY_TIMEOUT, ready=None):
    """Until the terminal relay is there (it starts after the tmux session exists): its socket on the host, or
    READY() (macOS: a probe through podman exec)."""
    socket_path = directory / "tmux" / "attach.sock"
    ready = ready or socket_path.exists
    deadline = time.monotonic() + timeout
    while not ready():
        if process.poll() is not None:
            log = launcher().printable((directory / "supervisor.log").read_text(errors="replace")[-3000:])
            raise RuntimeError(f"Session failed to start (status {process.returncode}):\n{log}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"Session did not become ready within {timeout} s; see {directory / 'supervisor.log'}")
        time.sleep(0.2)


def clear_stale_pod(name, data):
    """Remove the pod a stopped session left behind when its supervisor died without cleanup
    (crash, kill -9, host reboot); otherwise the restart fails because the pod name is taken.

    Only a pod labelled with this session's directory is removed: a session of the same
    name under another XDG_STATE_HOME may still be running in it."""
    a = parsed(data)
    if a.backend != "podman":
        return
    if launcher().remove_stale_pod(a, data["pod"], (session_dir(name) / "tmux").resolve()):
        print(f"agent-sandbox: removed pod {data['pod']} left behind by an earlier run of session {name}",
              file=sys.stderr)


def launch(name, data, agent_args, a=None, event="rebirth"):
    """Start the session's supervisor. A: the merged launcher arguments if the caller has them. EVENT: what the
    history notes (start, rebirth)."""
    directory = session_dir(name)
    a = a or effective(data)[0]
    if a is not None:
        # pause/stop must reach the sandbox the launcher creates, wherever its settings came from.
        data.update(backend=a.backend, podman_url=a.podman_url)
    if data.get("pod") and not foreground(data):
        clear_stale_pod(name, data)  # the last run's pod, if its supervisor died without removing it
    # Every run of a session is a run of its own: a new run id (and pod), linked to the name by its history.
    # A task that is brought back comes back as a session.
    run = f"{data['agent']}-{secrets.token_hex(4)}"
    data.update(pod=f"agent-{run}", run=run, mode="tmux")
    # Without agent arguments the project configuration's agent_args apply.
    # Without a kept HOME the launcher makes a fresh one and discards it at the end.
    cmd = [sys.executable, "-I", str(HERE / "agent_sandbox.py"), data["agent"], *run_options(data),
           *(["--state-dir", str(directory / "home")] if keeps_home(data) else []),
           "--tmux-socket-dir", str(directory / "tmux"),
           "--pod-name", data["pod"], "--start-id", name, *(["--", *agent_args] if agent_args else [])]
    (directory / "paused").unlink(missing_ok=True)
    (directory / "exit.json").unlink(missing_ok=True)
    # The sandbox can write tmux/ and HOME: start from a fresh socket directory (a stale socket would
    # look ready at once; a directory in its place would block the start) and a private HOME again.
    shutil.rmtree(directory / "tmux", ignore_errors=True)
    (directory / "tmux").mkdir(mode=0o700, exist_ok=True)
    if keeps_home(data):
        (directory / "home").mkdir(mode=0o700, exist_ok=True)
        (directory / "home").chmod(0o700)
    with open(directory / "supervisor.log", "ab") as log:
        # Own session: closing the terminal no longer ends the sandbox.
        process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, cwd=data["cwd"])
    data.update(pid=process.pid, pid_start=launcher().process_start(process.pid, PROC), started=now(),
                pid_ns=liveness().pid_namespace())  # only a process in this PID namespace judges it by its PID
    save(name, data)
    shown = display(name, data)
    starts().record(shown, event, name, run, agent=data["agent"], workspace=data.get("workspace"))
    try:
        wait_ready(directory, process, ready=(lambda: machine_relay_listening(data))
                   if launcher().machine_record(data) else None)
    except RuntimeError:
        if process.poll() is not None:
            starts().record(shown, "exit", name, run, status=process.returncode)
        raise
    if not data.get("ready"):
        data["ready"] = True
        save(name, data)
    print(f"agent-sandbox: {launcher().printable(shown)} is running. Detach: Ctrl-b d (or close the terminal). "
          f"Re-attach: sandbox-ai attach {launcher().printable(shown)}", file=sys.stderr)
    return 0


def start(a, argv):
    """Entry point for every interactive start (named a.start_name) and for `--session NAME`: a stopped session
    of that name restarts, else a new one begins (in a directory of its own: KEY)."""
    if a.session is None:
        key = new_key(a.start_name)
    else:
        try:
            key = resolve(a.session)
        except ValueError:
            key = new_key(a.session)
    created = not session_dir(key).exists()
    try:
        with session_lock(key, create=True):
            result = start_locked(a, argv, key)
    except BaseException:
        # A start that failed before anything was saved leaves no empty, unlisted directory behind.
        if created and not (session_dir(key) / "session.json").exists():
            shutil.rmtree(session_dir(key), ignore_errors=True)
        raise
    # Attached after the lock is released: stop/pause from another terminal must work meanwhile.
    return result if result else attach_if_terminal(key)


def start_locked(a, argv, key=None):
    name = key or a.session
    directory = session_dir(name)
    if (directory / "session.json").exists() and a.session is None:
        # Taken between the check of the name and now (another start of the same name).
        raise ValueError(f"The name {launcher().printable(a.start_name)} was taken meanwhile; choose another")
    if (directory / "session.json").exists():
        data = load(name)
        state = status(name, data)
        if state == "orphaned":
            stop_orphaned(name, data)  # asked to start it again: its pod first (said), then as any stopped one
            data, state = load(name), status(name, load(name))
        if state != "stopped":
            raise ValueError(f"Session {launcher().printable(display(name, data))} is {state}; attach with: "
                             f"sandbox-ai attach {launcher().printable(display(name, data))}")
        shown = launcher().printable(display(name, data))
        if data["agent"] != a.agent:
            raise ValueError(f"Session {shown} belongs to {data['agent']}, not {a.agent}")
        options, agent_args = split_argv(argv)
        if (options and options != data["options"]) or agent_args:
            # A restart reuses the saved options and continues the last conversation.
            ignored = (options if options != data["options"] else []) + (["--", *agent_args] if agent_args else [])
            given = launcher().printable(shlex.join(ignored))
            print(f"agent-sandbox: warning: session {shown} keeps the options it was created with "
                  f"({launcher().printable(shlex.join(data['options'])) or 'none'}); ignored now: {given or 'none'}. "
                  "Use a new session name for other options.", file=sys.stderr)
        print(f"agent-sandbox: resuming stopped session {shown} with its saved options", file=sys.stderr)
        review_home(name, data)
        document = getattr(a, "project_document", None) or {}
        return launch(name, data, restart_args(data, document.get("agent_args", []), bool(a.binary)), a)
    options, agent_args = split_argv(argv)
    keep = a.session is not None or bool(getattr(a, "keep_home", False))
    for path in (directory, *([directory / "home"] if keep else []), directory / "tmux"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    data = {"agent": a.agent, "options": options, "agent_args": agent_args, "backend": a.backend,
            "podman_url": a.podman_url, "workspace": a.workspace, "cwd": os.getcwd(), "created": now(),
            "cwd_project": getattr(a, "cwd_project", None),
            "ready": False, "name": a.session if a.session is not None else a.start_name,
            "project": launcher().project_label(a), "keep": keep, "mode": "tmux"}
    claim_name(name, data["name"], data)  # refused if a start of the same name saved its own meanwhile
    return launch(name, data, agent_args, a, event="start")


def attach(name):
    data = load(name)
    state = status(name, data)
    shown = launcher().printable(display(name, data))
    if foreground(data) and state != "stopped":
        raise ValueError(f"{shown} runs without tmux (a task, no terminal, or --no-tmux): there is no terminal to "
                         f"attach to; it ends with its task, or with: sandbox-ai stop {shown}")
    if state == "orphaned":
        print(f"agent-sandbox: {shown} runs without its supervisor (it ended unexpectedly): its terminal is reached, "
              f"but approvals, net allow and workspace add are not. Save your work, then: sandbox-ai stop {shown} && "
              f"sandbox-ai resume {shown}", file=sys.stderr)
    elif state != "running":
        raise ValueError(f"Session {shown} is {state}; run: sandbox-ai resume {shown}")
    # Only terminal bytes cross: the tmux client runs inside the sandbox, so
    # nothing the agent's tmux server requests can execute on the host.
    activity_event(name, "attach")
    try:
        if launcher().machine_record(data):
            attach_machine(data)
        else:
            launcher().attach_client(str(session_dir(name) / "tmux" / "attach.sock"))
    finally:
        activity_event(name, "detach")
    return after_attach(name)


def attach_machine(data, runner=None):
    """macOS: attach this terminal to the session's relay in podman machine's VM: the same dumb byte pipe as on
    Linux (runtime.attach_client), run in the container by podman exec, which carries the keys, the screen and the
    window size. Nothing of the sandbox runs on the Mac."""
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    command = launcher().machine_exec(data, ["--internal-attach-client", launcher().ATTACH_SOCKET], tty=tty)
    try:
        done = (runner or subprocess.run)(command, check=False)
    except OSError as exc:
        raise RuntimeError(f"Session terminal relay not reachable: podman exec did not run ({exc.strerror or exc})") \
            from None
    if done.returncode != 0:
        raise RuntimeError(f"Session terminal relay not reachable through podman machine (podman exec: status "
                           f"{done.returncode})")
    return 0


def pause(name):
    with session_lock(name):
        return pause_locked(name)


def pause_locked(name):
    data = load(name)
    state = status(name, data)
    if state != "running":
        raise ValueError(f"Session {launcher().printable(display(name, data))} is {state}, not running")
    if parsed(data).backend == "podman":
        subprocess.run(launcher().podman_command(parsed(data), "pod", "pause", data["pod"]), check=True,
                       stdout=subprocess.DEVNULL)
    else:
        # A task's launcher holds a terminal: only what runs below it stops.
        signal_tree(data["pid"], signal.SIGSTOP, include_root=not foreground(data))
    (session_dir(name) / "paused").touch()
    activity_event(name, "pause")
    starts().record(display(name, data), "pause", name, current_run(name, data))
    print(f"agent-sandbox: session {launcher().printable(display(name, data))} paused", file=sys.stderr)
    return 0


def current_run(name, data):
    """The run id of the session's supervisor (versions before 0.1.30 did not record it)."""
    return data.get("run") if isinstance(data.get("run"), str) else f"session-{name}"


def thaw(name, data):
    if parsed(data).backend == "podman":
        subprocess.run(launcher().podman_command(parsed(data), "pod", "unpause", data["pod"]), check=True,
                       stdout=subprocess.DEVNULL)
    else:
        signal_tree(data["pid"], signal.SIGCONT, include_root=not foreground(data))
    (session_dir(name) / "paused").unlink(missing_ok=True)


def added_options(data, workspaces=(), mounts=(), mcp=()):
    """The session's saved options with WORKSPACES (directories), MOUNTS (SOURCE:DESTINATION[:ro|rw]) and MCP servers
    (--mcp NAME; one it names already is not added again) added, their host paths made absolute from here (the
    session starts in its own directory). A session that named no workspace had its start directory as /work: it
    stays first, the new ones follow at /workspaces/<name>. Checked as the launcher parses them; what a start
    checks besides (policy, mode, private locations) it checks when it starts."""
    l = launcher()
    options = list(data["options"])
    if mcp:
        with in_directory(data["cwd"]):
            try:
                given = l.parse_args([data["agent"], *options])
            except SystemExit:
                raise ValueError("the session's saved options no longer parse; start anew") from None
        for name in dict.fromkeys(mcp):
            if not re.fullmatch(l.MCP_NAME, name):
                raise ValueError(f"--mcp NAME: 1-32 letters, digits, '_' or '-': {l.printable(name)!r}")
            if name not in given.mcp:
                options += ["--mcp", name]
    if workspaces:
        with in_directory(data["cwd"]):
            try:
                given = l.parse_args([data["agent"], *options])
            except SystemExit:
                raise ValueError("the session's saved options no longer parse; start anew") from None
        named = any(option in ("-w", "--workspace") or option.startswith("--workspace=")
                    or (option.startswith("-w") and not option.startswith("--")) for option in options)
        if not named:
            options += ["--workspace", ",".join(given.workspaces)]
    for value in workspaces:
        path = Path(os.path.expanduser(value)).absolute()
        if "," in str(path):
            raise ValueError(f"{l.printable(str(path))}: a workspace path cannot hold a comma (it separates entries)")
        if not path.is_dir():
            raise ValueError(f"{l.printable(str(path))}: no such directory")
        options += ["--workspace", str(path)]
    for value in mounts:
        head, _, mode = value.rpartition(":")
        source_destination = head if mode in ("ro", "rw") else value
        source, separator, destination = source_destination.rpartition(":")
        if not separator or not source or not destination:
            raise ValueError(f"--mount {l.printable(value)}: SOURCE:DESTINATION[:ro|rw]")
        source = str(Path(os.path.expanduser(source)).absolute())
        options += ["--mount", f"{source}:{destination}" + (f":{mode}" if mode in ("ro", "rw") else "")]
    with in_directory(data["cwd"]):
        try:
            l.parse_args([data["agent"], *options])
        except SystemExit:
            raise ValueError("these options do not go with the session's own; see sandbox-ai claude --help") from None
    return options


# Options a rebirth does not change (resume NAME OPTION): the start's own (its name, HOME, tmux, pod), what only a
# new start decides (its backend and Podman service, its project file), what starts nothing, and the workspaces,
# mounts and MCP servers resume adds with options of its own (--workspace, --mount, --mcp).
REBIRTH_FIXED = ("--session", "--name", "--project-name", "--name-style", "--keep", "--no-keep", "--state-dir",
                 "--start-id", "--tmux-socket-dir", "--pod-name", "--mcp-socket-dir", "--helper-of",
                 "--mcp-approvals-dir", "--workspace-name", "--workspaces-as-given", "--tmux", "--no-tmux",
                 "--dry-run", "--probe", "--version", "--help", "--backend", "--podman-url", "--config", "--no-config",
                 "--workspace", "--mount", "--mcp")
# ... of which --drop-option may take back these (a mount or an MCP server added earlier).
DROPPABLE = ("--mount", "--mcp")


def option_groups(options, actions):
    """OPTIONS (long forms, as canonical_options writes them) as [(action or None, option, value or None, tokens)]:
    one entry per option with its value."""
    groups, index = [], 0
    while index < len(options):
        token = options[index]
        name, equals, value = token.partition("=")
        action = actions.get(name) if token.startswith("--") else None
        if action is None or action.nargs == 0:
            groups.append((action, name if action else token, None, [token]))
            index += 1
        elif equals:
            groups.append((action, name, value, [token]))
            index += 1
        else:
            groups.append((action, name, options[index + 1] if index + 1 < len(options) else None,
                           options[index:index + 2]))
            index += 2
    return groups


def is_list(action):
    """ACTION adds to a list (repeatable: --allow-host, --env, --allow-pypi ...) rather than setting one value."""
    return type(action).__name__ in ("_AppendAction", "_AppendConstAction")


def same_value(action, a, b):
    if a is None or b is None:
        return a == b
    if action.type is str.lower:
        return a.lower() == b.lower()
    if action.type is Path or action.option_strings[-1] in launcher().PATH_OPTIONS:
        return os.path.normpath(os.path.expanduser(a)) == os.path.normpath(os.path.expanduser(b))
    return a == b


def absolute_value(option, value):
    """VALUE of OPTION with its host path made absolute from here (the session starts in its own directory)."""
    part = launcher().PATH_OPTIONS.get(option)
    path = part(value) if part and value else None
    if not path or os.path.isabs(os.path.expanduser(path)):
        return value
    absolute = str(Path(path).absolute())
    return absolute + value[len(path):] if value.startswith(path) else value[:len(value) - len(path)] + absolute


def changed_options(data, given=(), dropped=()):
    """(options, changes) of a rebirth with other options: the session's saved options with DROPPED (OPTION or
    OPTION=VALUE: every value of OPTION, or that one) taken out, then GIVEN (launcher options) applied - a
    single-valued option (--cpus N, --gui, --memory SIZE, --mode MODE, --canaries/--no-canaries ...) replaces the one
    saved, a list option (--allow-host, --env, --allow-pypi ...) adds its value unless saved already. CHANGES: lines
    that say what changed. What the project file sets is not among the saved options (change the file). Checked as
    the launcher parses them; the checks of a start (policy, mode, private locations, trust) are check_start's."""
    l = launcher()
    printable = l.printable
    actions = l.option_actions()
    shown = printable(data.get("name") or "the start")
    groups = option_groups(l.canonical_options(list(data["options"])), actions)
    changes = []
    for spec in dict.fromkeys(dropped):
        option, equals, value = str(spec).partition("=")
        option = l.canonical_options([option if option.startswith("-") else "--" + option])[0]
        action = actions.get(option)
        if action is None:
            raise ValueError(f"--drop-option {printable(spec)}: no such option (see sandbox-ai claude --help)")
        if option in REBIRTH_FIXED and option not in DROPPABLE:
            raise ValueError(f"--drop-option {printable(spec)}: {printable(option)} is not changed by a rebirth; "
                             "start anew")
        value = value if equals else None
        if value is not None and action.nargs == 0:
            raise ValueError(f"--drop-option {printable(spec)}: {printable(option)} takes no value")
        value = absolute_value(option, value) if value is not None else None

        def matches(group):
            found, name, saved, _ = group
            if found is None or found.dest != action.dest:
                return False
            if type(action).__name__ == "_AppendConstAction":
                return name == option  # --allow-pypi among the presets: that one only
            return value is None or same_value(action, saved, value)
        kept = [group for group in groups if not matches(group)]
        if len(kept) == len(groups):
            raise ValueError(f"--drop-option {printable(spec)}: not among the saved options of {shown} (what its "
                             "project file sets stays: change the file)")
        changes += [f"dropped: {printable(' '.join(tokens))}" for group in groups if matches(group)
                    for tokens in [group[3]]]
        groups = kept
    for action, option, value, tokens in option_groups(l.canonical_options(list(given)), actions):
        if action is None:
            raise ValueError(f"{printable(option)}: not an option of the launcher (see sandbox-ai claude --help)")
        if option in REBIRTH_FIXED:
            hint = {"--keep": "a kept start keeps its HOME", "--no-keep": "delete it instead: sandbox-ai delete NAME",
                    "--workspace": "--workspace DIR of resume", "--mount": "--mount SPEC of resume",
                    "--mcp": "--mcp NAME of resume"}.get(option, "start anew for that")
            raise ValueError(f"{printable(option)} is not changed by a rebirth ({hint})")
        if action.nargs != 0 and value is None:
            raise ValueError(f"{printable(option)} needs a value")
        if value is not None:
            value = absolute_value(option, value)
            tokens = [option, value]
        if is_list(action):
            if any(found is not None and found.dest == action.dest and name == option
                   and same_value(action, saved, value) for found, name, saved, _ in groups):
                changes.append(f"kept: {printable(' '.join(tokens))} (saved already)")
                continue
            groups.append((action, option, value, tokens))
            changes.append(f"added: {printable(' '.join(tokens))}")
            continue
        before = [group for group in groups if group[0] is not None and group[0].dest == action.dest]
        groups = [group for group in groups if group not in before]
        groups.append((action, option, value, tokens))
        was = " ".join(token for group in before for token in group[3])
        changes.append(f"set: {printable(' '.join(tokens))}" + (f" (was: {printable(was)})" if was else ""))
    options = [token for group in groups for token in group[3]]
    with in_directory(data["cwd"]):
        try:
            l.parse_args([data["agent"], *options])
        except SystemExit:
            raise ValueError("these options do not go together; see sandbox-ai claude --help") from None
    return options, changes


def check_start(name, data):
    """The checks of a new start for the session's options as they are now: the launcher with --dry-run, as the
    supervisor will start (its project file and whether it is trusted, the administrator's policy, the security
    mode, private locations, devices, the network). Nothing is started or written; ValueError with what the
    launcher said when it would refuse."""
    directory = session_dir(name)
    cmd = [sys.executable, "-I", str(HERE / "agent_sandbox.py"), data["agent"], *run_options(data),
           *(["--state-dir", str(directory / "home")] if keeps_home(data) else []),
           "--tmux-socket-dir", str(directory / "tmux"), "--start-id", name, "--dry-run"]
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=data["cwd"],
                                timeout=READY_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError("the checks of the start took too long; nothing was changed") from None
    if result.returncode != 0:
        said = [line for line in result.stderr.splitlines() if line.strip()][-6:]
        raise ValueError("a start with these options would be refused, so nothing was changed:\n  "
                         + "\n  ".join(launcher().printable(line) for line in said or ["(no reason given)"]))


def resume(name, workspaces=(), mounts=(), mcp=(), options=(), dropped=()):
    """Thaw or restart the session NAME; with WORKSPACES, MOUNTS or MCP servers a stopped session starts with them
    added, with OPTIONS (launcher options) and DROPPED (--drop-option) its saved options changed (changed_options);
    kept for its later starts."""
    with session_lock(name):
        result, restarted = resume_locked(name, workspaces, mounts, mcp, options, dropped)
    return attach_if_terminal(name) if restarted and not result else result


def add_mcp(name, servers):
    """Add the MCP SERVERS to the saved options of session NAME (--mcp, kept for its later starts) without starting
    it: the options it starts with from now on."""
    with session_lock(name):
        data = load(name)
        data["options"] = added_options(data, mcp=servers)
        save(name, data)
        return list(data["options"])


def resume_locked(name, workspaces=(), mounts=(), mcp=(), options=(), dropped=()):
    data = load(name)
    state = status(name, data)
    shown = launcher().printable(display(name, data))
    if (workspaces or mounts or mcp or options or dropped) and state in ("paused", "running"):
        raise ValueError(f"{shown} is {state}: a workspace, mount, MCP server or other option is added at a start; "
                         "add --restart (the agent continues its conversation)")
    if state == "paused":
        thaw(name, data)
        activity_event(name, "resume")
        starts().record(display(name, data), "resume", name, current_run(name, data))
        print(f"agent-sandbox: session {shown} resumed", file=sys.stderr)
        return 0, False
    if state == "running":
        print(f"agent-sandbox: session {shown} is already running", file=sys.stderr)
        return 0, False
    if state == "orphaned":
        print(f"agent-sandbox: {shown} still runs without its supervisor; ending its pod first, then it starts again",
              file=sys.stderr)
        stop_orphaned(name, data)
        data = load(name)
    if not keeps_home(data):
        prune(name, locked=True)  # resume holds the session's lock
        raise ValueError(f"{shown} has ended and its HOME was discarded (it was started with --no-keep); "
                         "start anew")
    check_keep_policy(shown)
    before = list(data["options"])
    if workspaces or mounts or mcp:
        data["options"] = added_options(data, workspaces, mounts, mcp)
    changes = []
    if options or dropped:
        data["options"], changes = changed_options({**data, "name": display(name, data)}, options, dropped)
        try:
            check_start(name, data)  # the checks of a new start, before anything is saved or started
        except ValueError:
            data["options"] = before
            raise
    review_home(name, data)
    print(f"agent-sandbox: restarting stopped session {shown}; the agent continues its last conversation",
          file=sys.stderr)
    for path in workspaces:
        print(f"agent-sandbox: added: {launcher().printable(str(Path(os.path.expanduser(path)).absolute()))} at "
              f"/workspaces/{launcher().printable(Path(os.path.expanduser(path)).absolute().name)} (a repeated name "
              "gets -2, -3 ...)", file=sys.stderr)
    for server in dict.fromkeys(mcp):
        print(f"agent-sandbox: added: the MCP server {launcher().printable(server)} (--mcp), kept for its later "
              "starts", file=sys.stderr)
    for line in changes:
        print(f"agent-sandbox: {line}" + ("" if line.startswith("kept:") else ", kept for its later starts"),
              file=sys.stderr)
    a, document = effective(data)
    args = restart_args(data, (document or {}).get("agent_args", []), bool(a and a.binary))
    try:
        return launch(name, data, args, a), True
    except BaseException:
        if data["options"] != before:  # refused or failed: the session keeps the options it had
            data["options"] = before
            save(name, data)
        raise


def home_review():
    spec = importlib.util.spec_from_file_location("sandbox_home_review_for_sessions", HERE / "home_review.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def review_home(name, data):
    """Before a kept HOME starts again: name what changed in its start files since its last start (compared with
    the manifest its launcher recorded then), keep that review for --review-home, and in a terminal let the user
    stop here. Raises ValueError when the user says no."""
    if not keeps_home(data):
        return None
    reviews = home_review()
    review = reviews.compare(session_dir(name) / "home", session_dir(name), with_diffs=True)
    if review is None:
        return None  # started before 0.1.30: no manifest yet (the coming start records one)
    reviews.save_review(session_dir(name), review)
    shown = launcher().printable(display(name, data))
    lines = reviews.notice(review, display(name, data), launcher().printable)
    for line in lines:
        print(f"agent-sandbox: {line}", file=sys.stderr)
    # Not complete (more files than checked) asks too: what was not checked may have changed.
    if (review["changes"] or not review["complete"]) and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            answer = input(f"agent-sandbox: start {shown} anyway? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer in ("n", "no", "nein"):
            raise ValueError(f"{shown} was not started; see the changes with: sandbox-ai resume {shown} "
                             "--review-home")
    return review


def review(name, as_json=False, out=print):
    """What changed in the start files of the HOME of NAME: since its last start (stopped: what the next resume
    will name), or what its latest start found (running or paused)."""
    data = load(name)
    shown = display(name, data)
    if not keeps_home(data):
        raise ValueError(f"{launcher().printable(shown)} keeps no HOME: there is nothing to review")
    state = status(name, data)
    reviews = home_review()
    if state == "stopped":
        found = reviews.compare(session_dir(name) / "home", session_dir(name), with_diffs=True)
    else:
        found = reviews.saved_review(session_dir(name))
    if as_json:
        out(json.dumps({"name": shown, "id": name, "state": state, "pending": state == "stopped", "review": found},
                       indent=2))
        return 0
    if state != "stopped":
        out(f"{launcher().printable(shown)} is {state}; what its latest start found:")
    reviews.report(found, shown, launcher().printable, out)
    return 0


def restore_home(name, paths=(), yes=False, as_json=False, out=print, ask=None):
    """sandbox-ai resume NAME --restore-home: put back, in the stopped start's HOME, the text its changed start files
    had at its last start, as its manifest kept it (home_review.restorable: small text files, no keys or tokens;
    links never followed). PATHS: only these. Asked file by file in a terminal (ASK(question) -> answer), or every
    one with YES. Starts nothing."""
    with session_lock(name):
        data = load(name)
        shown = display(name, data)
        printable = launcher().printable
        if not keeps_home(data):
            raise ValueError(f"{printable(shown)} keeps no HOME: there is nothing to restore")
        state = status(name, data)
        if state != "stopped":
            raise ValueError(f"{printable(shown)} is {state}: its agent could write the files again meanwhile; stop it "
                             f"first (sandbox-ai stop {printable(shown)})")
        reviews = home_review()
        home, directory = session_dir(name) / "home", session_dir(name)
        manifest = reviews.load(directory)
        if manifest is None:
            raise ValueError(f"{printable(shown)}: no record of its HOME yet (it is made at every start from version "
                             "0.1.30 on): nothing to restore")
        found = reviews.restorable(reviews.compare(home, directory, with_diffs=True), manifest)
        if paths:
            known = {item["path"] for item in found}
            unknown = [path for path in paths if path not in known]
            if unknown:
                raise ValueError(f"not among the changes of its HOME: {', '.join(printable(p) for p in unknown)} "
                                 f"(see sandbox-ai resume {printable(shown)} --review-home)")
            found = [item for item in found if item["path"] in paths]
        if not yes and any(item["data"] is not None for item in found) and not (
                ask is not None or (sys.stdin.isatty() and sys.stdout.isatty() and not as_json)):
            raise ValueError("--restore-home asks file by file in a terminal; elsewhere add --yes (and --path PATH "
                             "for only some files)")
        ask = ask or (lambda question: input(question))
        folders = {path: entry.get("mode") for path, entry in manifest["files"].items()
                   if isinstance(entry, dict) and entry.get("type") == "dir" and isinstance(entry.get("mode"), int)}
        restored, skipped = [], []
        for item in found:
            path = item["path"]
            if item["data"] is None:
                skipped.append({"path": path, "change": item["change"], "reason": item["why"]})
                continue
            if not yes:
                current = reviews.read_regular(home / path, reviews.TEXT_LIMIT + 1)
                now = current[0].decode("utf-8", "replace") if current else ""
                diff = reviews.differences(now, item["data"].decode("utf-8"), path) or ""
                out(f"== {item['change']} {reviews.shown_path(path, printable)}: back to its text at the last start")
                for line in diff.splitlines()[2:]:
                    out(printable(line))
                try:
                    answer = ask(f"agent-sandbox: put back {reviews.shown_path(path, printable)}? [y/N] ").strip().lower()
                except EOFError:
                    answer = "n"
                if answer not in ("y", "yes", "j", "ja"):
                    skipped.append({"path": path, "change": item["change"], "reason": "not put back (your answer)"})
                    continue
            try:
                reviews.put_back(home, path, item["data"], item["mode"], folders)
            except (OSError, ValueError) as exc:
                reason = (f"a link or no folder on the way: not followed ({exc.strerror})"
                          if isinstance(exc, OSError) and exc.strerror else str(exc))
                skipped.append({"path": path, "change": item["change"], "reason": reason})
                continue
            restored.append(path)
        if restored:
            starts().record(shown, "restore-home", name, current_run(name, data), paths=restored[:50])
        if as_json:
            out(json.dumps({"name": shown, "id": name, "restored": restored, "skipped": skipped}, indent=2))
            return 0
        for path in restored:
            out(f"put back: {reviews.shown_path(path, printable)}")
        for entry in skipped:
            out(f"left as it is: {reviews.shown_path(entry['path'], printable)} ({printable(entry['reason'])})")
        if not found:
            out(f"{printable(shown)}: nothing changed in the watched files of its HOME since its last start")
        return 0


def check_keep_policy(shown):
    """Refuse to bring back a kept HOME when the administrator's policy forbids kept HOMEs (forbid_keep)."""
    policy = launcher().load_policy()
    if policy.get("forbid_keep") is True:
        raise ValueError(f"{shown} has a kept HOME, and the administrator's policy forbids using one (forbid_keep in "
                         f"{launcher().POLICY_FILE}); start anew (its HOME is discarded then), or remove it: "
                         f"sandbox-ai delete {shown} --yes")


def stop(name, timeout=30):
    with session_lock(name):
        return stop_locked(name, timeout)


def stop_locked(name, timeout):
    data = load(name)
    state = status(name, data)
    shown = launcher().printable(display(name, data))
    if state == "stopped":
        print(f"agent-sandbox: session {shown} is not running", file=sys.stderr)
        return 0
    if state == "orphaned":
        return stop_orphaned(name, data, timeout)
    if state == "paused":
        thaw(name, data)
    activity_event(name, "stop")
    starts().record(display(name, data), "stop", name, current_run(name, data))
    # The supervisor forwards SIGTERM and runs the normal cleanup, including
    # pod removal and the Claude credential write-back. State stays on disk.
    # It may end by itself between the look above and the signal: then there is nothing left to stop.
    with contextlib.suppress(ProcessLookupError):
        os.kill(data["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while alive(name, data) and time.monotonic() < deadline:
        time.sleep(0.2)
    if alive(name, data):
        with contextlib.suppress(ProcessLookupError):
            os.kill(data["pid"], signal.SIGKILL)
        time.sleep(0.5)
        try:
            clear_stale_pod(name, data)  # only this session's own, labelled pod
        except (ValueError, subprocess.CalledProcessError) as exc:
            print(f"agent-sandbox: {exc}", file=sys.stderr)
    print(f"agent-sandbox: session {shown} stopped; state kept in {session_dir(name)}", file=sys.stderr)
    return 0


def stop_orphaned(name, data, timeout=30):
    """End the session NAME whose supervisor is gone while its Podman pod runs on: stop the pod (its supervisor in
    the sandbox gets SIGTERM and ends the agent, as at any stop; after TIMEOUT seconds Podman kills it), remove it,
    record the end (exit.json, history) so that resume works, and remove the run directory left behind. Only the
    pod this session's directory labelled (a session's) or recorded (a task's) is touched."""
    shown = launcher().printable(display(name, data))
    pod, run = data["pod"], current_run(name, data)
    url = parsed(data).podman_url
    found = liveness().pods(url, fresh=True)
    if found is None:
        raise ValueError(f"Podman does not answer: {shown} cannot be stopped now (its pod is {pod})")
    labels = (found.get(pod) or {}).get("labels") or {}
    owner = str((session_dir(name) / "tmux").resolve())
    if pod in found and not foreground(data) and labels.get(launcher().SESSION_LABEL) != owner:
        print(f"agent-sandbox: warning: the Podman pod {pod} does not carry {shown}'s label (another state directory's, "
              "or an older version's); stopping it as you asked", file=sys.stderr)
    activity_event(name, "stop")
    starts().record(display(name, data), "stop", name, run)
    print(f"agent-sandbox: {shown} runs without its supervisor; stopping its pod {pod} (up to {timeout:g} s)",
          file=sys.stderr)
    stopped, said = liveness().stop_pod(pod, url, timeout)
    if not stopped:
        raise ValueError(f"{shown}: its pod {pod} could not be stopped ({launcher().printable(said) or 'it still runs'});"
                         f" try: podman pod stop {pod} && podman pod rm {pod}")
    starts().write_exit(name, run, None)
    starts().record(display(name, data), "exit", name, run, status=None)
    (session_dir(name) / "paused").unlink(missing_ok=True)
    left = launcher().runs_dir() / run  # its launcher's run directory, if a killed launcher left it
    if left.is_dir() and not left.is_symlink():
        liveness().discard(left)
    print(f"agent-sandbox: session {shown} stopped; state kept in {session_dir(name)}"
          + (f"; continue it with: sandbox-ai resume {shown}" if keeps_home(data) else ""), file=sys.stderr)
    return 0


def delete(name, yes=False):
    with session_lock(name):
        return delete_locked(name, yes)


def delete_locked(name, yes):
    data = load(name)
    shown = launcher().printable(display(name, data))
    if status(name, data) != "stopped":
        raise ValueError(f"Stop session {shown} before deleting it")
    if not yes:
        raise ValueError(f"Deleting removes {session_dir(name)} including its HOME; repeat with --yes")
    home = session_dir(name) / "home"
    shutil.rmtree(session_dir(name), onerror=make_writable_and_retry)
    forget_canaries(home)
    starts().record(display(name, data), "delete", name)
    print(f"agent-sandbox: session {shown} deleted", file=sys.stderr)
    return 0


def forget_canaries(home):
    """The record of a kept HOME's canary files (canary.py keeps it outside the HOME) goes with the HOME."""
    try:
        spec = importlib.util.spec_from_file_location("sandbox_canary_for_sessions", HERE / "canary.py")
        canary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(canary)
        canary.home_state(home).unlink(missing_ok=True)
    except (OSError, ImportError, AttributeError):
        pass  # nothing recorded, or no canaries in this version


def make_writable_and_retry(function, path, _):
    """rmtree helper: the agent may have left read-only directories (e.g. Go's module cache)."""
    parent = os.path.dirname(path)
    for target in (parent, path):
        try:
            if not os.path.islink(target):
                os.chmod(target, 0o700)
        except OSError:
            pass
    function(path)


def rows():
    result = []
    if root().is_dir():
        for directory in sorted(root().iterdir()):
            try:
                if prune(directory.name):
                    continue
            except ValueError:
                pass  # not a session's id: shown as damaged below if it holds a session.json
            try:
                data = json.loads((directory / "session.json").read_text())
                result.append([display(directory.name, data), data["agent"], data["backend"],
                               status(directory.name, data), data["workspace"], data["created"]])
            except (OSError, ValueError, KeyError, TypeError):
                if (directory / "session.json").exists():
                    result.append([directory.name, "?", "?", "damaged", "-", "-"])
    return result


def list_sessions(out=print):
    spec = importlib.util.spec_from_file_location("sandbox_ai_textout_for_sessions", HERE / "textout.py")
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    found = rows()
    colours = {"running": "ok", "paused": "warn"}
    # The workspace last: it is the longest and wraps at the terminal's width.
    t.table(["NAME", "AGENT", "BACKEND", "STATUS", "CREATED", "WORKSPACE"],
            [[launcher().printable(name), agent, backend,
              t.style(status, colours.get(status.split()[0] if status else "", "muted")), created,
              launcher().printable(str(workspace))] for name, agent, backend, status, workspace, created in found],
            out, indent=0, empty="no sessions; start one with: sandbox-ai AGENT --session NAME")
    return 0


# -- a session to another host: its configuration copied there, started again --

_hosts = None


def hosts():
    """hosts.py (sandbox-ai hosts: SSH to the other machines)."""
    global _hosts
    if _hosts is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_hosts_for_sessions", HERE / "hosts.py")
        with LOADING:
            if _hosts is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _hosts = loaded
    return _hosts


def parse_maps(maps):
    """[(local prefix, remote prefix)] of --map LOCAL=REMOTE (absolute paths), the longest prefix first."""
    pairs = []
    for value in maps:
        local, equals, remote = str(value).partition("=")
        if not equals or not local.startswith("/") or not remote.startswith("/"):
            raise ValueError(f"--map takes LOCAL=REMOTE, both absolute paths: {launcher().printable(value)}")
        pairs.append((local.rstrip("/") or "/", remote.rstrip("/") or "/"))
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def mapped(path, maps):
    for local, remote in maps:
        if path == local or path.startswith(local.rstrip("/") + "/"):
            return remote + path[len(local):] if local != "/" else remote.rstrip("/") + path
    return path


def map_options(options, maps):
    """OPTIONS with the host paths of path options (the launcher's PATH_OPTIONS, -w too) moved by MAPS."""
    path_options = dict(launcher().PATH_OPTIONS)
    path_options.update({"-w": str, "--workspace-ro": str})
    result, pending = [], None
    for value in options:
        if pending is not None:
            part = path_options[pending](value)
            result.append(value.replace(part, mapped(part, maps), 1) if part else value)
            pending = None
        elif value in path_options:
            pending = value
            result.append(value)
        elif "=" in value and value.split("=", 1)[0] in path_options:
            name, inner = value.split("=", 1)
            part = path_options[name](inner)
            result.append(f"{name}={inner.replace(part, mapped(part, maps), 1)}" if part else value)
        else:
            result.append(value)
    return result


def option_paths(options):
    """The absolute host paths the launcher OPTIONS name (path options, -w and --workspace-ro too), in their order,
    each once: what a copy to another host may need moved (--map)."""
    getters = dict(launcher().PATH_OPTIONS)
    getters.update({"-w": str, "--workspace-ro": str})
    found, pending = [], None
    for value in map(str, options):
        if pending is not None:
            name, inner, pending = pending, value, None
        elif value in getters:
            pending = value
            continue
        elif "=" in value and value.split("=", 1)[0] in getters:
            name, inner = value.split("=", 1)
        else:
            continue
        try:
            part = str(getters[name](inner) or "")
        except (TypeError, ValueError, IndexError):
            continue
        part = part.rstrip("/") or part
        if part.startswith("/") and "\n" not in part and part not in found:
            found.append(part)
    return found


def plan_json(name, maps=()):
    """`session plan NAME --json`: what a copy of NAME to another host would start with ({"agent", "options",
    "agent_args", "paths", "problems"}) - for the app and the web, which never change directory in their own
    process to read it."""
    agent, options, agent_args, problems = transfer_plan(name, maps)
    return {"agent": agent, "options": options, "agent_args": agent_args, "paths": option_paths(options),
            "problems": [str(p) for p in problems]}


def transfer_plan(name, maps=()):
    """(agent, options, agent arguments, problems) a copy of session NAME starts with on another host: its saved
    options with what its trusted project file added (the copy passes --no-config: the other host's file of that
    folder, if any, is not read - exactly this configuration starts), host paths moved by MAPS, and the agent's own
    options of its first start (not a task or prompt: that is not run again). PROBLEMS: what cannot go along."""
    data = load(name)
    l = launcher()
    argv = [data["agent"], *run_options(data)]
    problems, document = [], None
    try:
        with in_directory(data["cwd"]):
            a = l.parse_args(argv)
            _, document = l.load_project_config(a)
            if document:
                argv = l.merge_config(argv, a, document)
    except (OSError, ValueError, SystemExit) as exc:
        problems.append(f"the session's configuration cannot be read here: {exc}")
    if isinstance(document, dict) and document.get("mcp_servers"):
        names = ", ".join(sorted(map(str, document["mcp_servers"])))
        problems.append(f"the project defines its own MCP servers ({names}) in its .sandbox-ai.json: trust that "
                        "file on the other host and start the project there, or register the servers there "
                        "(sandbox-ai mcp add)")
    if isinstance(document, dict) and document.get("rules"):
        problems.append("the project's own ruleset (.sandbox-ai.json \"rules\") holds only on this machine: give it "
                        "there as a file (--rules FILE)")
    options, agent_args = split_argv(argv)
    options = [value for value in options if value not in ("--no-config",)]
    return data["agent"], map_options(options, parse_maps(maps)), leading_options(data["agent"],
                                                                                  data.get("agent_args") or []), problems


def copy_to_host(name, host, new_name=None, maps=(), move=False, start=True, run=None, out=print):
    """Session NAME to HOST (a host of sandbox-ai hosts): its configuration checked there (a dry run with exactly
    those options), then started there as a session of its own (NEW_NAME, else the same name); MOVE stops this
    one afterwards (its HOME stays here until it is deleted). Logins and the session's HOME are not copied: the
    other host uses its own (sandbox-ai login there). Returns the exit status."""
    run = run or hosts().run
    shown = launcher().printable(display(name, load(name)))
    target = new_name or display(name, load(name))
    if not re.fullmatch(launcher().SESSION_NAME, str(target)):
        raise ValueError(f"not a session name: {launcher().printable(target)}")
    agent, options, agent_args, problems = transfer_plan(name, maps)
    if problems:
        for problem in problems:
            out(f"sandbox-ai session copy: {problem}")
        return 1
    check = [agent, "--dry-run", "--no-config", *options]
    status, stdout, stderr = run(host, check, timeout=180)
    if status != 0:
        out(f"sandbox-ai session copy: {shown} cannot start on {launcher().printable(host)} as it is:")
        for line in (stderr or stdout or "").strip().splitlines()[-12:]:
            out(f"  {launcher().printable(line)}")
        out("  (folders of this machine that lie elsewhere there: --map LOCAL=REMOTE)")
        return 1
    if not start:
        out(f"sandbox-ai session copy: {shown} can start on {launcher().printable(host)} (checked, not started)")
        return 0
    command = [agent, "--session", target, "--no-config", *options, *(["--", *agent_args] if agent_args else [])]
    status, stdout, stderr = run(host, command, timeout=900)
    if status != 0:
        out(f"sandbox-ai session copy: the start on {launcher().printable(host)} failed (exit {status}):")
        for line in (stderr or stdout or "").strip().splitlines()[-12:]:
            out(f"  {launcher().printable(line)}")
        return status or 1
    out(f"sandbox-ai session copy: {shown} runs on {launcher().printable(host)} as {launcher().printable(target)} "
        f"(attach: sandbox-ai hosts run {launcher().printable(host)} -- session attach {launcher().printable(target)})")
    if move:
        stop(name)
        out(f"sandbox-ai session copy: {shown} stopped here; its HOME stays until: sandbox-ai session delete {shown}")
    return 0


def cli(argv):
    p = argparse.ArgumentParser(prog="sandbox-ai session", description="Manage named sandbox sessions. "
                                "Start one with: <agent>-sandbox --session NAME [options]")
    p.add_argument("-V", "--version", action="version", version=f"sandbox-ai {launcher().__version__}")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show sessions and their status")
    for command, text in [("attach", "Attach this terminal (detach again with Ctrl-b d)"),
                          ("pause", "Freeze every process of a running session"),
                          ("resume", "Thaw a paused session, or restart a stopped one"),
                          ("stop", "Stop a session; its state is kept"), ("delete", "Remove a stopped session")]:
        sub = commands.add_parser(command, help=text)
        sub.add_argument("name")
        if command == "delete":
            sub.add_argument("-y", "--yes", action="store_true", help="Confirm removal of the session's HOME")
    for command, text in [("copy", "Start a session's configuration again on another host (sandbox-ai hosts)"),
                          ("move", "As copy, then stop the session here (its HOME stays until it is deleted)")]:
        sub = commands.add_parser(command, help=text)
        sub.add_argument("name")
        sub.add_argument("host", help="a host of sandbox-ai hosts")
        sub.add_argument("--as", dest="new_name", metavar="NAME", help="the session's name there (default: the same)")
        sub.add_argument("--map", action="append", default=[], metavar="LOCAL=REMOTE",
                         help="a folder of this machine that lies elsewhere there (repeatable)")
        sub.add_argument("--check", action="store_true", help="only check that it can start there")
    plan = commands.add_parser("plan", help="What a copy of a session to another host starts with (JSON)")
    plan.add_argument("name")
    plan.add_argument("--map", action="append", default=[], metavar="LOCAL=REMOTE")
    plan.add_argument("--json", action="store_true", help="(the only output there is)")
    a = p.parse_args(argv)
    if a.command == "list":
        return list_sessions()
    if a.command == "plan":
        print(json.dumps(plan_json(resolve(a.name), a.map), indent=2))
        return 0
    if a.command in ("copy", "move"):
        return copy_to_host(resolve(a.name), a.host, a.new_name, a.map, move=a.command == "move", start=not a.check)
    key = resolve(a.name)
    if a.command == "delete":
        return delete(key, a.yes)
    return {"attach": attach, "pause": pause, "resume": resume, "stop": stop}[a.command](key)


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"sandbox-ai session: {exc}", file=sys.stderr)
        sys.exit(1)
