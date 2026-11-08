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
        _starts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_starts)
        _starts._modules["agent_sandbox"] = launcher()
    return _starts


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_sessions", HERE / "agent_sandbox.py")
        _launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_launcher)
    return _launcher


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
    return json.loads(path.read_text())


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
        return data.get("pid_start") is not None and any(arg.endswith(b"agent_sandbox.py") for arg in cmdline)
    return os.fsencode(str(session_dir(name) / "tmux")) in cmdline


def foreground(data):
    """A start that runs (or last ran) in the foreground of a terminal or script: no tmux, nothing to attach."""
    return data.get("mode") == "foreground"


def status(name, data):
    if not alive(name, data):
        return "stopped"
    return "paused" if (session_dir(name) / "paused").exists() else "running"


def descendants(pid):
    children = {}
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm may contain spaces and parentheses; fields follow the last ')'.
            ppid = int((entry / "stat").read_text().rpartition(")")[2].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
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
    try:
        return b"--mcp-socket-dir" in (PROC / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False


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
                "ready": True, "name": a.start_name, "project": launcher().project_label(a), "keep": True,
                "mode": "foreground", "pid": pid, "pid_start": launcher().process_start(pid, PROC), "started": now(),
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
        shutil.rmtree(session_dir(name), onerror=make_writable_and_retry)
        return True
    try:
        if locked:
            return remove()
        with session_lock(name):
            return remove()
    except (OSError, ValueError):  # gone, damaged, or being started or stopped by another command
        return False


def after_attach(name):
    """The terminal came back from the session NAME: either it detached (the sandbox runs on; a hint how to come
    back, 0), or the agent ended (wait for the supervisor's cleanup; its exit status)."""
    data = load(name)
    shown = launcher().printable(display(name, data))
    socket_path = session_dir(name) / "tmux" / "attach.sock"
    deadline = time.monotonic() + DETACH_GRACE
    while alive(name, data) and relay_listening(socket_path):
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
    argv = [data["agent"], *data["options"]]
    try:
        with in_directory(data["cwd"]):
            a = l.parse_args(argv)
            _, document = l.load_project_config(a)
            if document:
                a = l.parse_args(l.merge_config(argv, a, document))
    except (OSError, ValueError, SystemExit):
        return None, None  # the launcher reports the problem when it starts
    return a, document


def wait_ready(directory, process, timeout=READY_TIMEOUT):
    # The terminal relay starts after the tmux session exists.
    socket_path = directory / "tmux" / "attach.sock"
    deadline = time.monotonic() + timeout
    while not socket_path.exists():
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
    cmd = [sys.executable, "-I", str(HERE / "agent_sandbox.py"), data["agent"], *data["options"],
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
    data.update(pid=process.pid, pid_start=launcher().process_start(process.pid, PROC), started=now())
    save(name, data)
    shown = display(name, data)
    starts().record(shown, event, name, run, agent=data["agent"], workspace=data.get("workspace"))
    try:
        wait_ready(directory, process)
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
    if state != "running":
        raise ValueError(f"Session {shown} is {state}; run: sandbox-ai resume {shown}")
    # Only terminal bytes cross: the tmux client runs inside the sandbox, so
    # nothing the agent's tmux server requests can execute on the host.
    activity_event(name, "attach")
    try:
        launcher().attach_client(str(session_dir(name) / "tmux" / "attach.sock"))
    finally:
        activity_event(name, "detach")
    return after_attach(name)


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


def resume(name, workspaces=(), mounts=(), mcp=()):
    """Thaw or restart the session NAME; with WORKSPACES, MOUNTS or MCP servers a stopped session starts with them
    added (kept for its later starts)."""
    with session_lock(name):
        result, restarted = resume_locked(name, workspaces, mounts, mcp)
    return attach_if_terminal(name) if restarted and not result else result


def add_mcp(name, servers):
    """Add the MCP SERVERS to the saved options of session NAME (--mcp, kept for its later starts) without starting
    it: the options it starts with from now on."""
    with session_lock(name):
        data = load(name)
        data["options"] = added_options(data, mcp=servers)
        save(name, data)
        return list(data["options"])


def resume_locked(name, workspaces=(), mounts=(), mcp=()):
    data = load(name)
    state = status(name, data)
    shown = launcher().printable(display(name, data))
    if (workspaces or mounts or mcp) and state in ("paused", "running"):
        raise ValueError(f"{shown} is {state}: a workspace, mount or MCP server is added at a start; add --restart "
                         "(the agent continues its conversation)")
    if state == "paused":
        thaw(name, data)
        activity_event(name, "resume")
        starts().record(display(name, data), "resume", name, current_run(name, data))
        print(f"agent-sandbox: session {shown} resumed", file=sys.stderr)
        return 0, False
    if state == "running":
        print(f"agent-sandbox: session {shown} is already running", file=sys.stderr)
        return 0, False
    if not keeps_home(data):
        prune(name, locked=True)  # resume holds the session's lock
        raise ValueError(f"{shown} has ended and its HOME was discarded (it was started with --no-keep); "
                         "start anew")
    check_keep_policy(shown)
    before = list(data["options"])
    if workspaces or mounts or mcp:
        data["options"] = added_options(data, workspaces, mounts, mcp)
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


def cli(argv):
    p = argparse.ArgumentParser(prog="sandbox-session", description="Manage named sandbox sessions. "
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
    a = p.parse_args(argv)
    if a.command == "list":
        return list_sessions()
    key = resolve(a.name)
    if a.command == "delete":
        return delete(key, a.yes)
    return {"attach": attach, "pause": pause, "resume": resume, "stop": stop}[a.command](key)


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"sandbox-session: {exc}", file=sys.stderr)
        sys.exit(1)
