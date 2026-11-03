#!/usr/bin/env python3
"""Named, detachable sandbox sessions: start, attach, pause, resume, stop.

A session runs the normal launcher as a background supervisor. Inside the
sandbox the guard starts a detached tmux session (windows "agent" and
"shell"), so terminals can attach and detach while the agent keeps working.
State lives in $XDG_STATE_HOME/sandbox-ai/sessions/NAME:

  session.json     agent, launcher options, supervisor PID, pod name
  home/            persistent sandbox HOME (--state-dir)
  tmux/            tmux socket directory, bound at /sandbox/tmux
  supervisor.log   launcher output
  paused           present while the session is frozen
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
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
READY_TIMEOUT = 120
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
    if not re.fullmatch(launcher().SESSION_NAME, name):
        raise ValueError(f"Invalid session name: {name!r}")
    return root() / name


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load(name):
    path = session_dir(name) / "session.json"
    if not path.is_file():
        raise ValueError(f"No session named {name}")
    return json.loads(path.read_text())


def save(name, data):
    directory = session_dir(name)
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=directory)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
    os.replace(temporary, directory / "session.json")


def split_argv(argv):
    """Launcher options without the agent, --session and agent arguments."""
    options, agent_args = list(argv[1:]), []
    if "--" in options:
        position = options.index("--")
        options, agent_args = options[:position], options[position + 1:]
    kept, skip = [], False
    for value in options:
        if skip:
            skip = False
        elif value == "--session":
            skip = True
        elif not value.startswith("--session="):
            kept.append(value)
    return kept, agent_args


def alive(name, data):
    """The supervisor PID still runs this session's launcher (not a reused PID)."""
    pid = data.get("pid")
    if not pid:
        return False
    try:
        cmdline = (PROC / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return os.fsencode(str(session_dir(name) / "tmux")) in cmdline


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


def signal_tree(pid, sig):
    # Parents first, so a stopped parent cannot fork unseen children; a
    # second pass catches anything created during the first.
    for _ in range(2 if sig == signal.SIGSTOP else 1):
        for target in [pid, *descendants(pid)]:
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
def session_lock(name, create=False):
    """Exclusive lock for commands that change a session's state; a second one fails at once.

    Not inherited by the supervisor (Python opens files close-on-exec), so it ends with the command."""
    directory = session_dir(name)
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not directory.is_dir():
        raise ValueError(f"No session named {name}")
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"Session {name} is being started or stopped by another command; "
                             "try again in a moment") from None
        yield
    finally:
        os.close(fd)


def attach_if_terminal(name):
    if sys.stdin.isatty() and sys.stdout.isatty():
        return attach(name)
    return 0


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


def launch(name, data, agent_args, a=None):
    """Start the session's supervisor. A: the merged launcher arguments if the caller has them."""
    directory = session_dir(name)
    data["pod"] = f"agent-session-{name}"
    a = a or effective(data)[0]
    if a is not None:
        # pause/stop must reach the sandbox the launcher creates, wherever its settings came from.
        data.update(backend=a.backend, podman_url=a.podman_url)
    clear_stale_pod(name, data)
    # Without agent arguments the project configuration's agent_args apply.
    cmd = [sys.executable, "-I", str(HERE / "agent_sandbox.py"), data["agent"], *data["options"],
           "--state-dir", str(directory / "home"), "--tmux-socket-dir", str(directory / "tmux"),
           "--pod-name", data["pod"], *(["--", *agent_args] if agent_args else [])]
    (directory / "paused").unlink(missing_ok=True)
    # The sandbox can write tmux/ and HOME: start from a fresh socket directory (a stale socket would
    # look ready at once; a directory in its place would block the start) and a private HOME again.
    shutil.rmtree(directory / "tmux", ignore_errors=True)
    (directory / "tmux").mkdir(mode=0o700, exist_ok=True)
    (directory / "home").chmod(0o700)
    with open(directory / "supervisor.log", "ab") as log:
        # Own session: closing the terminal no longer ends the sandbox.
        process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, cwd=data["cwd"])
    data.update(pid=process.pid, started=now())
    save(name, data)
    wait_ready(directory, process)
    if not data.get("ready"):
        data["ready"] = True
        save(name, data)
    print(f"agent-sandbox: session {name} is running. Detach: Ctrl-b d. "
          f"Re-attach: sandbox-session attach {name}", file=sys.stderr)
    return 0


def start(a, argv):
    """Entry point for `<agent>-sandbox --session NAME ...`."""
    created = not session_dir(a.session).exists()
    try:
        with session_lock(a.session, create=True):
            result = start_locked(a, argv)
    except BaseException:
        # A start that failed before anything was saved leaves no empty, unlisted directory behind.
        if created and not (session_dir(a.session) / "session.json").exists():
            shutil.rmtree(session_dir(a.session), ignore_errors=True)
        raise
    # Attached after the lock is released: stop/pause from another terminal must work meanwhile.
    return result if result else attach_if_terminal(a.session)


def start_locked(a, argv):
    name = a.session
    directory = session_dir(name)
    if (directory / "session.json").exists():
        data = load(name)
        state = status(name, data)
        if state != "stopped":
            raise ValueError(f"Session {name} is {state}; attach with: sandbox-session attach {name}")
        if data["agent"] != a.agent:
            raise ValueError(f"Session {name} belongs to {data['agent']}, not {a.agent}")
        options, agent_args = split_argv(argv)
        if (options and options != data["options"]) or agent_args:
            # A restart reuses the saved options and continues the last conversation.
            ignored = (options if options != data["options"] else []) + (["--", *agent_args] if agent_args else [])
            given = launcher().printable(shlex.join(ignored))
            print(f"agent-sandbox: warning: session {name} keeps the options it was created with "
                  f"({launcher().printable(shlex.join(data['options'])) or 'none'}); ignored now: {given or 'none'}. "
                  "Use a new session name for other options.", file=sys.stderr)
        print(f"agent-sandbox: resuming stopped session {name} with its saved options", file=sys.stderr)
        document = getattr(a, "project_document", None) or {}
        return launch(name, data, restart_args(data, document.get("agent_args", []), bool(a.binary)), a)
    options, agent_args = split_argv(argv)
    for path in (directory, directory / "home", directory / "tmux"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    data = {"agent": a.agent, "options": options, "agent_args": agent_args, "backend": a.backend,
            "podman_url": a.podman_url, "workspace": a.workspace, "cwd": os.getcwd(), "created": now(),
            "ready": False}
    save(name, data)
    return launch(name, data, agent_args, a)


def attach(name):
    data = load(name)
    state = status(name, data)
    if state != "running":
        raise ValueError(f"Session {name} is {state}; run: sandbox-session resume {name}")
    # Only terminal bytes cross: the tmux client runs inside the sandbox, so
    # nothing the agent's tmux server requests can execute on the host.
    return launcher().attach_client(str(session_dir(name) / "tmux" / "attach.sock"))


def pause(name):
    with session_lock(name):
        return pause_locked(name)


def pause_locked(name):
    data = load(name)
    state = status(name, data)
    if state != "running":
        raise ValueError(f"Session {name} is {state}, not running")
    if parsed(data).backend == "podman":
        subprocess.run(launcher().podman_command(parsed(data), "pod", "pause", data["pod"]), check=True,
                       stdout=subprocess.DEVNULL)
    else:
        signal_tree(data["pid"], signal.SIGSTOP)
    (session_dir(name) / "paused").touch()
    print(f"agent-sandbox: session {name} paused", file=sys.stderr)
    return 0


def thaw(name, data):
    if parsed(data).backend == "podman":
        subprocess.run(launcher().podman_command(parsed(data), "pod", "unpause", data["pod"]), check=True,
                       stdout=subprocess.DEVNULL)
    else:
        signal_tree(data["pid"], signal.SIGCONT)
    (session_dir(name) / "paused").unlink(missing_ok=True)


def resume(name):
    with session_lock(name):
        result, restarted = resume_locked(name)
    return attach_if_terminal(name) if restarted and not result else result


def resume_locked(name):
    data = load(name)
    state = status(name, data)
    if state == "paused":
        thaw(name, data)
        print(f"agent-sandbox: session {name} resumed", file=sys.stderr)
        return 0, False
    if state == "running":
        print(f"agent-sandbox: session {name} is already running", file=sys.stderr)
        return 0, False
    print(f"agent-sandbox: restarting stopped session {name}; the agent continues its last conversation",
          file=sys.stderr)
    a, document = effective(data)
    args = restart_args(data, (document or {}).get("agent_args", []), bool(a and a.binary))
    return launch(name, data, args, a), True


def stop(name, timeout=30):
    with session_lock(name):
        return stop_locked(name, timeout)


def stop_locked(name, timeout):
    data = load(name)
    state = status(name, data)
    if state == "stopped":
        print(f"agent-sandbox: session {name} is not running", file=sys.stderr)
        return 0
    if state == "paused":
        thaw(name, data)
    # The supervisor forwards SIGTERM and runs the normal cleanup, including
    # pod removal and the Claude credential write-back. State stays on disk.
    os.kill(data["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while alive(name, data) and time.monotonic() < deadline:
        time.sleep(0.2)
    if alive(name, data):
        os.kill(data["pid"], signal.SIGKILL)
        time.sleep(0.5)
        try:
            clear_stale_pod(name, data)  # only this session's own, labelled pod
        except (ValueError, subprocess.CalledProcessError) as exc:
            print(f"agent-sandbox: {exc}", file=sys.stderr)
    print(f"agent-sandbox: session {name} stopped; state kept in {session_dir(name)}", file=sys.stderr)
    return 0


def delete(name, yes=False):
    with session_lock(name):
        return delete_locked(name, yes)


def delete_locked(name, yes):
    data = load(name)
    if status(name, data) != "stopped":
        raise ValueError(f"Stop session {name} before deleting it")
    if not yes:
        raise ValueError(f"Deleting removes {session_dir(name)} including its HOME; repeat with --yes")
    shutil.rmtree(session_dir(name), onerror=make_writable_and_retry)
    print(f"agent-sandbox: session {name} deleted", file=sys.stderr)
    return 0


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
                data = json.loads((directory / "session.json").read_text())
                result.append([directory.name, data["agent"], data["backend"], status(directory.name, data),
                               data["workspace"], data["created"]])
            except (OSError, ValueError, KeyError, TypeError):
                if (directory / "session.json").exists():
                    result.append([directory.name, "?", "?", "damaged", "-", "-"])
    return result


def list_sessions():
    table = [["NAME", "AGENT", "BACKEND", "STATUS", "WORKSPACE", "CREATED"], *rows()]
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    for row in table:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
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
    if a.command == "delete":
        return delete(a.name, a.yes)
    return {"attach": attach, "pause": pause, "resume": resume, "stop": stop}[a.command](a.name)


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"sandbox-session: {exc}", file=sys.stderr)
        sys.exit(1)
