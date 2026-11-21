#!/usr/bin/env python3
"""Cairn's workers are the session's agents: the narrow relay between Cairn's dispatcher (in Cairn's own container)
and the agent's CLIs (in the agent's container). Docs: cairn.py, docs/CAIRN_REVIEW.md.

    cairn_relay.py supervise CONFIG-JSON in the agent's container, inside Firejail, with the agent's environment
                                         (runtime.inner_guard starts it): runs `serve CONFIG-JSON` and starts it
                                         again when it ends (supervise)
    cairn_relay.py serve CONFIG-JSON     the relay: {"socket", "workspace", "workers": {AGENT: [ARGUMENT, ...]}} -
                                         the arguments sandbox-ai puts before the agent's own (its model, its --llm
                                         profile), never Cairn's
    cairn_relay.py worker AGENT ARGS...  in Cairn's container: the `claude` and `codex` Cairn's dispatcher starts
                                         in its local execution mode (cairn_runtime.py puts them on its PATH)

The worker hands its command line, its folder and the graph snapshots its prompt names (Cairn writes them to
/tmp/cairn-prompts in its own container) over the Unix socket; the relay takes exactly the command lines of Cairn's
claudecode and codex drivers at the reviewed commit - `claude --session-id UUID|-r UUID
--dangerously-skip-permissions -p -- PROMPT`, `codex exec [resume ID] --dangerously-bypass-approvals-and-sandbox --
PROMPT`, `--help` - and nothing else: no other option, no other program, no shell. It runs that agent's own CLI in
/work/cairn/runs/PROJECT, with the arguments the launcher gave for it (the model of --cairn-workers or the lead's
--model, Codex's --llm profile: sandbox-ai's choice, never Cairn's), and sends back its
output and exit status. The worker gone (Cairn's timeout, the dispatcher stopped), the CLI's process group ends.
The snapshots it writes into the agent's /tmp are bounded: the newest KEPT, KEPT_BYTES at most (prune).

A relay that ends during the session (killed, out of memory) is started again by its supervisor after
RESTART_DELAYS, at most RESTARTS times within RESTART_WINDOW seconds; beyond that the supervisor leaves a note beside
the socket (ENDED) that says why and removes the socket: a worker says the note, and Cairn's container stops its
dispatcher with it (cairn_runtime.py) - never a task that fails at once, again and again.

After FAILURES failed tasks in a row the relay pauses the workers (PAUSE seconds, doubled each time up to
PAUSE_LIMIT): Cairn hands a failing task out again without a limit, and every attempt spends the model's usage.

Frames both ways: one byte of kind, four of length (network order), the payload. Worker to relay: b"q" the request
(JSON). Relay to worker: b"o" output, b"e" error output, b"x" the exit status (!i), last.

Standard library only.
"""
import collections
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

SOCKET = "/sandbox-cairn/relay/relay.sock"  # the worker's side (cairn.MOUNT/relay)
PROMPTS = re.compile(r"/tmp/cairn-prompts/[a-z_]{1,32}-[0-9a-f]{12}/graph\.yaml")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
CODEX_SESSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,63}")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
REQUEST_LIMIT = 16 << 20
PROMPT_LIMIT = 1 << 20
FILE_LIMIT = 4 << 20
FILES = 8
# The snapshots live in the agent's /tmp (a tmpfs: memory), one folder for each task Cairn hands out - and it hands a
# failing task out again without a limit. The relay keeps the newest KEPT of them, KEPT_BYTES at most, and removes
# the older ones it wrote (prune); those of a call whose worker still runs (BUSY) stay whatever their age.
SNAPSHOT_FOLDER = re.compile(r"[a-z_]{1,32}-[0-9a-f]{12}")
SNAPSHOT_FILE = "graph.yaml"
KEPT = 64
KEPT_BYTES = 64 << 20
BUSY = collections.Counter()  # {a snapshot's folder: the calls that run with it}
BUSY_LOCK = threading.Lock()
CHUNK = 64 << 10
CONNECTIONS = 8  # at once (Cairn runs a worker or two, and checks each once with --help at its start)
WORKER_LIMIT = 3 * 3600  # seconds a worker may run, whatever Cairn's timeouts say
KILL_GRACE = 5
FAILURES = 5
PAUSE = 60
PAUSE_LIMIT = 900
PAUSED = 75  # the worker's exit status while the relay pauses (EX_TEMPFAIL)
REFUSED = 2
LOG_LIMIT = 1 << 20
# Where `--help` (Cairn's check at its start, no task: no project folder) runs - the agent's workspace, or "/" where
# there is none (the relay's tests on a host without /work).
HELP_CWD = "/work"
RESTARTS = 5  # restarts of a relay that ended, within RESTART_WINDOW seconds (supervise)
RESTART_WINDOW = 600
RESTART_DELAYS = (1, 2, 5, 10, 30)  # seconds before the 1st, 2nd, ... restart within the window
ENDED = "relay.ended"  # beside the socket: why the relay is not started again (both containers read it)
NOTE_LIMIT = 2000
CLAUDE_SKIP = "--dangerously-skip-permissions"
CODEX_SKIP = "--dangerously-bypass-approvals-and-sandbox"


# -- frames --

def frame(kind, payload):
    return kind + struct.pack("!I", len(payload)) + payload


def read_exactly(connection, size):
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError("the connection ended")
        data += chunk
    return data


def read_frame(connection, limit):
    head = read_exactly(connection, 5)
    size = struct.unpack("!I", head[1:])[0]
    if size > limit:
        raise ValueError(f"a frame of {size} bytes is more than {limit}")
    return head[:1], read_exactly(connection, size)


# -- what the relay takes --

def command(request, workers, workspace):
    """(argv, cwd, files, extra environment) of a worker REQUEST ({"agent", "argv", "cwd", "files"}), checked
    against the session's WORKERS ({agent: [argument]}) and the WORKSPACE root; ValueError for anything else."""
    if not isinstance(request, dict):
        raise ValueError("the request is no JSON object")
    agent, argv = request.get("agent"), request.get("argv")
    if agent not in workers:
        raise ValueError(f"{agent!r} is no worker of this session ({', '.join(workers) or 'none'})")
    if not isinstance(argv, list) or not all(isinstance(word, str) and "\0" not in word for word in argv):
        raise ValueError("argv is a list of strings")
    own = list(workers[agent] or [])
    if argv == ["--help"]:
        return [agent, "--help"], None, {}, {}
    prompt = argv[-1] if argv else ""
    if len(prompt.encode("utf-8", "surrogatepass")) > PROMPT_LIMIT:
        raise ValueError(f"the prompt is larger than {PROMPT_LIMIT} bytes")
    extra = {}
    if agent == "claude":
        shapes = len(argv) == 6 and argv[0] in ("--session-id", "-r") and UUID.fullmatch(argv[1]) and \
            argv[2:5] == [CLAUDE_SKIP, "-p", "--"]
        if not shapes:
            raise ValueError("not a command line of Cairn's claudecode driver")
        run = ["claude", *argv[:4], *own, "--", prompt]
        # Claude Code refuses --dangerously-skip-permissions as root unless it is told it runs in a sandbox (as -D
        # tells it in sandbox-ai): Cairn gives the flag, the sandbox is the boundary.
        extra["IS_SANDBOX"] = "1"
    elif agent == "codex":
        if len(argv) == 4 and argv[:3] == ["exec", CODEX_SKIP, "--"]:
            pass
        elif len(argv) == 6 and argv[:2] == ["exec", "resume"] and CODEX_SESSION.fullmatch(argv[2]) and \
                argv[3:5] == [CODEX_SKIP, "--"]:
            pass
        else:
            raise ValueError("not a command line of Cairn's codex driver")
        run = ["codex", "exec", *own, *argv[1:]]
    else:
        raise ValueError(f"{agent}: no driver")
    cwd = request.get("cwd")
    if not isinstance(cwd, str) or not cwd.startswith(workspace.rstrip("/") + "/") or \
            not PROJECT.fullmatch(cwd[len(workspace.rstrip("/")) + 1:]):
        raise ValueError(f"the worker's folder is not one of {workspace}")
    files = request.get("files") or {}
    if not isinstance(files, dict) or len(files) > FILES:
        raise ValueError(f"at most {FILES} graph snapshots")
    for path, content in files.items():
        if not isinstance(path, str) or not PROMPTS.fullmatch(path) or not isinstance(content, str) or \
                len(content.encode("utf-8", "surrogatepass")) > FILE_LIMIT:
            raise ValueError("a file that is no graph snapshot of Cairn's")
    return run, cwd, files, extra


def prune(root, busy=(), keep=None, limit=None):
    """The graph snapshots below ROOT (the relay's own: FOLDER/graph.yaml, FOLDER named as Cairn names them) down to
    the newest KEEP (KEPT) and LIMIT bytes (KEPT_BYTES): the older ones are removed, each with its folder when
    nothing else is in it. Those in the folders BUSY, and of calls whose workers run (the module's BUSY), are counted
    but never removed. Nothing is followed: a ROOT or a folder that is a symbolic link, and a snapshot that is no
    regular file, are left alone. How many were removed."""
    keep = KEPT if keep is None else keep
    limit = KEPT_BYTES if limit is None else limit
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError:
        return 0
    removed = 0
    try:
        with BUSY_LOCK:
            held = {folder for folder, count in BUSY.items() if count > 0} | set(busy)
        found = []
        for name in os.listdir(root_fd):
            if not SNAPSHOT_FOLDER.fullmatch(name):
                continue
            try:
                folder_fd = os.open(name, flags, dir_fd=root_fd)
            except OSError:
                continue
            try:
                status = os.lstat(SNAPSHOT_FILE, dir_fd=folder_fd)
            except OSError:
                continue
            finally:
                os.close(folder_fd)
            if stat.S_ISREG(status.st_mode):
                found.append((status.st_mtime, name, status.st_size))
        count = size = 0
        full = False
        for _, name, length in sorted(found, reverse=True):  # the newest first
            count, size = count + 1, size + length
            full = full or count > keep or size > limit
            if not full or os.path.join(root, name) in held:
                continue
            try:
                folder_fd = os.open(name, flags, dir_fd=root_fd)
            except OSError:
                continue
            try:
                os.unlink(SNAPSHOT_FILE, dir_fd=folder_fd)
                removed += 1
            except OSError:
                pass
            finally:
                os.close(folder_fd)
            try:
                os.rmdir(name, dir_fd=root_fd)
            except OSError:
                pass  # (something else is in it: not the relay's to remove)
    finally:
        os.close(root_fd)
    return removed


def prepare(cwd, files):
    """The project's folder (made, no symbolic link on the way) and the graph snapshots, written; older snapshots
    beyond what the relay keeps are removed (prune), never this call's own."""
    os.makedirs(cwd, mode=0o755, exist_ok=True)
    if os.path.realpath(cwd) != os.path.normpath(cwd):
        raise ValueError(f"{cwd} leads elsewhere (a symbolic link)")
    for path, content in files.items():
        folder = os.path.dirname(path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        if os.path.realpath(folder) != folder:
            raise ValueError(f"{folder} leads elsewhere (a symbolic link)")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogatepass") as stream:
            stream.write(content)
    own = {os.path.dirname(path) for path in files}
    for root in sorted({os.path.dirname(folder) for folder in own}):
        prune(root, busy=own)


class Guard:
    """FAILURES failed tasks in a row pause the workers: PAUSE seconds, doubled with each pause up to PAUSE_LIMIT; a
    task that succeeds ends it."""

    def __init__(self, clock=time.monotonic, failures=FAILURES, pause=PAUSE, limit=PAUSE_LIMIT):
        self.clock, self.failures, self.pause, self.limit = clock, failures, pause, limit
        self.lock = threading.Lock()
        self.failed, self.pauses, self.until = 0, 0, 0.0

    def paused(self):
        """Seconds the workers still wait, or 0."""
        with self.lock:
            return max(0.0, self.until - self.clock())

    def done(self, status):
        with self.lock:
            if status == 0:
                self.failed, self.pauses = 0, 0
                return
            self.failed += 1
            if self.failed >= self.failures:
                self.until = self.clock() + min(self.limit, self.pause * (2 ** self.pauses))
                self.pauses += 1
                self.failed = 0


def end_group(child, grace=KILL_GRACE):
    """CHILD (a session leader) and its process group: SIGTERM (Claude Code keeps its session), SIGKILL after GRACE."""
    for sent in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sent)
        except OSError:
            pass
        try:
            child.wait(grace if sent == signal.SIGTERM else None)
            return
        except subprocess.TimeoutExpired:
            continue


def pump(connection, child, limit=WORKER_LIMIT, clock=time.monotonic):
    """CHILD's output to the worker as it comes, until it ends; the worker gone or LIMIT seconds over, its group
    ends. Its exit status (or None when the worker went)."""
    streams = {child.stdout.fileno(): b"o", child.stderr.fileno(): b"e"}
    poller = select.poll()
    for fd in streams:
        poller.register(fd, select.POLLIN | select.POLLHUP)
    poller.register(connection.fileno(), select.POLLIN | getattr(select, "POLLRDHUP", 0x2000) | select.POLLHUP)
    deadline = clock() + limit
    while streams:
        if clock() > deadline:
            end_group(child)
            break
        for fd, events in poller.poll(1000):
            if fd == connection.fileno():
                try:
                    gone = not connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                except BlockingIOError:
                    gone = False
                except OSError:
                    gone = True
                if gone or events & (select.POLLHUP | select.POLLERR):
                    end_group(child)
                    return None
                connection.recv(CHUNK)  # (the worker sends nothing more: whatever it is, it is dropped)
                continue
            data = os.read(fd, CHUNK)
            if not data:
                poller.unregister(fd)
                del streams[fd]
                continue
            try:
                connection.sendall(frame(streams[fd], data))
            except OSError:
                end_group(child)
                return None
    try:
        return child.wait(KILL_GRACE * 2)
    except subprocess.TimeoutExpired:
        end_group(child)
        return child.wait()


def log(cfg, text):
    path = Path(cfg["socket"]).with_name("relay.log")
    try:
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_name("relay.log.1"))
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {text}\n")
    except OSError:
        pass


def handle(connection, cfg, guard, popen=subprocess.Popen):
    """One worker's call: checked, run, answered."""
    def answer(status, message=None):
        try:
            if message:
                connection.sendall(frame(b"e", f"sandbox-ai cairn relay: {message}\n".encode()))
            connection.sendall(frame(b"x", struct.pack("!i", status)))
        except OSError:
            pass
    try:
        connection.settimeout(30)
        try:
            kind, payload = read_frame(connection, REQUEST_LIMIT)
        except EOFError:
            return None  # a look whether the relay answers (cairn_runtime.relay_ready), no call
        if kind != b"q":
            raise ValueError("no request")
        request = json.loads(payload.decode("utf-8", "surrogatepass"))
        argv, cwd, files, extra = command(request, cfg["workers"], cfg["workspace"])
    except (OSError, EOFError, ValueError, UnicodeError, RecursionError) as exc:
        log(cfg, f"refused: {exc}")
        return answer(REFUSED, f"refused: {exc}")
    task = argv[1:] != ["--help"]
    wait = guard.paused() if task else 0
    if wait:
        log(cfg, f"paused {int(wait)} s more: {argv[0]}")
        return answer(PAUSED, f"the workers are paused for {int(wait) + 1} s more after {guard.failures} failed "
                              "tasks in a row (every attempt spends the model's usage); stop the project on Cairn's "
                              "board if it keeps failing")
    run_in = cwd or (HELP_CWD if os.path.isdir(HELP_CWD) else "/")
    folders = {os.path.dirname(path) for path in files}  # this call's snapshots: kept while its worker runs
    with BUSY_LOCK:
        BUSY.update(folders)
    try:
        try:
            if cwd:
                prepare(cwd, files)
            child = popen(argv, cwd=run_in, env={**os.environ, **extra}, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, ValueError) as exc:
            log(cfg, f"could not start {argv[0]}: {exc}")
            if task:
                guard.done(127)
            return answer(127, f"{argv[0]} could not be started: {exc}")
        connection.settimeout(None)
        log(cfg, f"started {argv[0]} {'task' if task else '--help'} in {run_in}")
        status = pump(connection, child)
    finally:
        with BUSY_LOCK:
            BUSY.subtract(folders)
            for folder in folders:
                if BUSY[folder] <= 0:
                    del BUSY[folder]
    if task:
        guard.done(0 if status == 0 else 1)
    log(cfg, f"{argv[0]} ended: {status if status is not None else 'the worker went'}")
    if status is not None:
        answer(status if status >= 0 else 128 - status)


def listen(path):
    """The relay's socket at PATH (0600): an old socket or symbolic link there is replaced."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    try:
        if not os.path.isdir(path) or os.path.islink(path):
            os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        server.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    server.listen(16)
    return server


def serve(cfg, server=None, stop=None):
    """The relay: one thread per worker's call, at most CONNECTIONS at once."""
    workers = {str(k): [str(word) for word in v or []] for k, v in dict(cfg.get("workers") or {}).items()}
    cfg = {"socket": str(cfg["socket"]), "workspace": str(cfg.get("workspace") or "/work/cairn/runs"),
           "workers": workers}
    server = server or listen(cfg["socket"])
    guard = Guard()
    slots = threading.BoundedSemaphore(CONNECTIONS)
    log(cfg, f"listening for {', '.join(workers)}")
    while stop is None or not stop.is_set():
        try:
            connection, _ = server.accept()
        except OSError:
            if stop is not None and stop.is_set():
                break
            time.sleep(0.1)
            continue
        if not slots.acquire(blocking=False):
            try:
                connection.sendall(frame(b"e", b"sandbox-ai cairn relay: too many workers at once\n") +
                                   frame(b"x", struct.pack("!i", PAUSED)))
            except OSError:
                pass
            connection.close()
            continue

        def run(connection=connection):
            try:
                handle(connection, cfg, guard)
            finally:
                connection.close()
                slots.release()
        threading.Thread(target=run, daemon=True).start()
    return 0


def ended_note(socket_path):
    """The note beside SOCKET_PATH that says why the relay is not started again (ENDED), or None."""
    try:
        with open(os.path.join(os.path.dirname(str(socket_path)), ENDED), "rb") as stream:
            text = stream.read(NOTE_LIMIT).decode("utf-8", "replace").strip()
    except OSError:
        return None
    return text or None


def how_it_ended(status):
    return f"signal {-status}" if isinstance(status, int) and status < 0 else f"exit status {status}"


def supervise(cfg, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic):
    """`cairn_relay.py supervise CONFIG`: the relay (serve) in a process of its own, started again when it ends -
    after RESTART_DELAYS (shorter again once the restarts lie RESTART_WINDOW apart), at most RESTARTS times within
    RESTART_WINDOW seconds. Beyond that it is not: the note ENDED beside the socket says why (a worker says it, Cairn's
    container stops its dispatcher with it), the socket goes, and this returns 1. A relay that ends with status 0
    (it never does on its own) ends this too. SIGTERM ends the relay and this."""
    folder = os.path.dirname(str(cfg["socket"]))
    with_note = os.path.join(folder, ENDED)
    try:
        os.unlink(with_note)  # (an earlier relay's, in a folder used again)
    except OSError:
        pass
    child, starts = None, []

    def stop(*_):
        if child is not None and child.poll() is None:
            child.terminate()
        sys.exit(0)
    if popen is subprocess.Popen:
        signal.signal(signal.SIGTERM, stop)
    while True:
        flags = [flag for flag, on in (("-I", sys.flags.isolated), ("-S", sys.flags.no_site)) if on]
        child = popen([sys.executable, *flags, os.path.abspath(__file__), "serve", json.dumps(cfg)],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status = child.wait()
        if status == 0:
            return 0
        now = clock()
        starts = [begun for begun in starts if now - begun < RESTART_WINDOW]
        if len(starts) >= RESTARTS:
            note = (f"the relay to the session's agents ended {RESTARTS + 1} times within {RESTART_WINDOW} s (the last "
                    f"time: {how_it_ended(status)}) and is not started again: Cairn's tasks cannot run in this "
                    "session any more (its board stays); start the session again")
            log(cfg, note)
            try:
                with open(with_note, "w", encoding="utf-8") as stream:
                    stream.write(note + "\n")
            except OSError:
                pass
            try:
                os.unlink(str(cfg["socket"]))
            except OSError:
                pass
            return 1
        delay = RESTART_DELAYS[min(len(starts), len(RESTART_DELAYS) - 1)]
        starts.append(now)
        log(cfg, f"the relay ended ({how_it_ended(status)}); started again in {delay} s (restart {len(starts)} of "
                 f"{RESTARTS} within {RESTART_WINDOW} s)")
        sleep(delay)


# -- the worker's side, in Cairn's container --

def snapshots(argv, read_limit=FILE_LIMIT):
    """{path: text} of the graph snapshots ARGV's prompt names (Cairn wrote them in this container)."""
    found = {}
    for path in dict.fromkeys(PROMPTS.findall(" ".join(argv))):
        try:
            if os.path.islink(path) or os.path.getsize(path) > read_limit:
                continue
            found[path] = Path(path).read_text(encoding="utf-8", errors="surrogatepass")
        except OSError:
            continue
        if len(found) >= FILES:
            break
    return found


def worker(agent, argv, path=None, out=None, err=None, cwd=None):
    """The `claude` or `codex` of Cairn's dispatcher: the call relayed; its exit status."""
    out = out or sys.stdout.buffer
    err = err or sys.stderr.buffer
    path = path or os.environ.get("SANDBOX_AI_CAIRN_RELAY") or SOCKET
    request = json.dumps({"agent": agent, "argv": list(argv), "cwd": cwd or os.getcwd(),
                          "files": snapshots(argv)}).encode("utf-8", "surrogatepass")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(path)
        client.sendall(frame(b"q", request))
    except OSError as exc:
        note = ended_note(path)
        err.write((f"sandbox-ai cairn: {note}\n" if note else
                   f"sandbox-ai cairn: the relay to the session's {agent} is not reachable ({exc})\n").encode())
        err.flush()
        return 127

    def ended(*_):
        client.close()
        os._exit(143)
    signal.signal(signal.SIGTERM, ended)
    signal.signal(signal.SIGINT, ended)
    try:
        while True:
            kind, payload = read_frame(client, CHUNK * 2)
            if kind == b"x":
                return struct.unpack("!i", payload)[0] & 0xFF
            (out if kind == b"o" else err).write(payload)
            (out if kind == b"o" else err).flush()
    except (OSError, EOFError, ValueError):
        err.write(b"sandbox-ai cairn: the relay ended without an exit status\n")
        err.flush()
        return 255
    finally:
        client.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "serve":
        return serve(json.loads(argv[1]))
    if len(argv) == 2 and argv[0] == "supervise":
        return supervise(json.loads(argv[1]))
    if len(argv) >= 2 and argv[0] == "worker":
        return worker(argv[1], argv[2:])
    print("usage: cairn_relay.py supervise|serve CONFIG-JSON | worker AGENT ARGS...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
