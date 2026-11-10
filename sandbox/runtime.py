#!/usr/bin/env python3
"""sandbox-ai runtime: the code that runs inside the sandbox; standard library only.

Only this file is mounted into the sandbox (read-only, at /sandbox/runtime.py). It holds the
bootstrap that becomes PID 1 of the outer sandbox, the guard that verifies Firejail before the
agent starts, the egress and file loggers, the virtual display, the terminal relay for
detachable sessions and the MCP serve/bridge ends. The host launcher (agent_sandbox.py)
loads it for the few helpers both sides share, never the other way round.
"""
import base64
import binascii
import contextlib
import errno
import fnmatch
import ctypes
import fcntl
import hashlib
import ipaddress
import io
import itertools
import json
import os
import posixpath
from pathlib import Path
import re
import secrets
import select
import shutil
import shlex
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.parse
import zlib

LOG_DIR = Path("/sandbox/log")
VNC_DIR = Path("/sandbox/vnc")
TMUX_DIR = "/sandbox/tmux"
ATTACH_SOCKET = f"{TMUX_DIR}/attach.sock"
GUI_CONFIG = Path("/sandbox/session/gui.json")
SESSION_CONFIG = Path("/sandbox/session/config.json")
# Display helpers run without any capability or privilege gain.
DROP_ALL = ["setpriv", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "--"]
FILE_RECENT_LIMIT = 4096
INOTIFY_EVENTS = {0x8: "write", 0x10: "read", 0x100: "create", 0x200: "delete", 0x40: "move-from", 0x80: "move-to"}


def x_socket(display):
    return Path("/tmp/.X11-unix/X" + display[1:])


def listen_unix(socket_path):
    """Listening Unix socket that appears under its name only once it accepts."""
    path = Path(socket_path)
    # bind() creates the file before listen(); a client waiting for the file
    # would otherwise be refused in between.
    staging = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    staging.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Socket paths are limited to 107 bytes; a long directory is reached through its descriptor.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        server.bind(f"/proc/self/fd/{directory}/{staging.name}" if len(str(staging)) > 100 else str(staging))
    finally:
        os.close(directory)
    server.listen(16)
    os.replace(staging, path)
    return server


def read_frames(buffer):
    """Complete frames from BUFFER and the unconsumed remainder."""
    frames = []
    while len(buffer) >= 5:
        size = struct.unpack("!I", buffer[1:5])[0]
        if len(buffer) < 5 + size:
            break
        frames.append((buffer[:1], buffer[5:5 + size]))
        buffer = buffer[5 + size:]
    return frames, buffer


def attach_relay(socket_path, command):
    """Inside the sandbox: run COMMAND (tmux attach) on its own pty per connection."""
    server = listen_unix(socket_path)
    while True:
        connection, _ = server.accept()
        threading.Thread(target=relay_terminal, args=(connection, command), daemon=True).start()


def relay_terminal(connection, command):
    connection.settimeout(10)
    frames, buffer = [], b""
    try:
        # The first frame carries the window size; apply it before the client starts.
        while not frames:
            chunk = connection.recv(4096)
            if not chunk:
                break
            frames, buffer = read_frames(buffer + chunk)
    except OSError:
        frames = []
    if not frames:
        connection.close()
        return
    connection.settimeout(None)
    master, slave = os.openpty()
    def resize(payload):
        if len(payload) == 4:
            rows, columns = struct.unpack("!HH", payload)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    pending = []
    for kind, payload in frames:
        if kind == b"r":
            resize(payload)
        else:
            pending.append(payload)
    try:
        # Session leader with the pty as controlling terminal, so SIGWINCH arrives.
        child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                 preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    except OSError:
        os.close(master)
        os.close(slave)
        connection.close()
        return
    os.close(slave)
    try:
        for payload in pending:
            os.write(master, payload)
        while True:
            ready, _, _ = select.select([connection, master], [], [])
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""  # EIO: the client exited
                if not data:
                    break
                connection.sendall(data)
            if connection in ready:
                data = connection.recv(65536)
                if not data:
                    break
                frames, buffer = read_frames(buffer + data)
                for kind, payload in frames:
                    if kind == b"r":
                        resize(payload)
                    else:
                        view = memoryview(payload)
                        while view:
                            view = view[os.write(master, view):]
    except OSError:
        pass
    finally:
        connection.close()
        os.close(master)
        if child.poll() is None:
            child.send_signal(signal.SIGHUP)
        child.wait()


def attach_frame(kind, payload):
    """Host-to-sandbox terminal frame: b"d" input bytes or b"r" (rows, columns)."""
    return kind + struct.pack("!I", len(payload)) + payload


def connect_relay(client, socket_path):
    """Connect CLIENT to the terminal relay's SOCKET_PATH. The directory is writable from the sandbox: a symlink
    there could point at another socket of the user (on the host; in the container, the supervisor's) and receive
    the keystrokes. Opened without following one, checked to be a socket of this user, connected through it."""
    fd = os.open(socket_path, os.O_PATH | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(errno.EPERM, "not a socket owned by you")
        client.connect(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)


def attach_probe(socket_path):
    """--internal-attach-probe (macOS: through podman exec): 0 while the terminal relay accepts, else 1."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        try:
            connect_relay(client, socket_path)
        except OSError:
            return 1
    return 0


def attach_client(socket_path, stdin=0, stdout=1):
    """The client of an attach: a dumb byte pipe. Nothing from the sandbox is executed. It runs on the host (Linux),
    or, on macOS, in the container (--internal-attach-client through `podman exec`: the socket is in podman
    machine's VM, and a Unix socket does not cross its shared folders)."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connect_relay(client, socket_path)
    except OSError as exc:
        client.close()
        raise RuntimeError(f"Session terminal relay not reachable: {socket_path}: {exc}") from None
    interactive = os.isatty(stdin)
    def send_size():
        size = os.get_terminal_size(stdin) if interactive else os.terminal_size((80, 24))
        client.sendall(attach_frame(b"r", struct.pack("!HH", size.lines, size.columns)))
    send_size()
    # The SIGWINCH handler only wakes the loop: a frame sent from the handler could land
    # inside a partially sent input frame and corrupt the stream.
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_write, False)
    def resized(*_):
        try:
            os.write(wake_write, b"r")
        except BlockingIOError:
            pass
    saved = termios.tcgetattr(stdin) if interactive else None
    previous = signal.signal(signal.SIGWINCH, resized) if interactive else None
    if interactive:
        import tty
        tty.setraw(stdin)
    sources = [stdin, client, wake_read]
    try:
        while True:
            ready, _, _ = select.select(sources, [], [])
            if wake_read in ready:
                os.read(wake_read, 4096)
                send_size()
            if stdin in ready:
                data = os.read(stdin, 65536)
                if data:
                    client.sendall(attach_frame(b"d", data))
                else:
                    sources = [client, wake_read]
            if client in ready:
                data = client.recv(65536)
                if not data:
                    break
                view = memoryview(data)
                while view:
                    view = view[os.write(stdout, view):]
    except (ConnectionResetError, BrokenPipeError):
        pass  # the relay went (the agent ended) while keys were still being sent: the end of the attach, as EOF is
    finally:
        if interactive:
            termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
            signal.signal(signal.SIGWINCH, previous)
            # Drop terminal answers to queries the sandbox sent just before it closed,
            # so they do not reach the host shell as typed input.
            termios.tcflush(stdin, termios.TCIFLUSH)
        os.close(wake_read)
        os.close(wake_write)
        client.close()
    return 0


def shortened(value, budget=None):
    """VALUE with strings cut to 500 characters and at most 200 items in all, for the tool-call log."""
    budget = budget if budget is not None else [200]
    if budget[0] <= 0:
        return "..."
    budget[0] -= 1
    if isinstance(value, str):
        return value[:500] + ("..." if len(value) > 500 else "")
    if isinstance(value, dict):
        return {str(k)[:200]: shortened(v, budget) for k, v in list(value.items())[:200]}
    if isinstance(value, list):
        return [shortened(v, budget) for v in value[:200]]
    return value


def tool_call_record(line):
    """The log record of a JSON-RPC line from an agent if it calls an MCP tool, else None."""
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    return {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": str(params.get("name", ""))[:200],
            "arguments": McpGateway.arguments(params.get("arguments", {}))}


MCP_LINE_LIMIT = 64 << 20  # one JSON-RPC message from the client
MCP_INSTANCE_LIMIT = 16  # server instances (connections) at once
MCP_EOF_GRACE = 10  # seconds an instance has to exit once its input ended, before it is terminated
MCP_PENDING_LIMIT = 1000  # requests of one connection the server has not answered yet
MCP_CONNECT_SECONDS = 5  # how long a relay waits for a running server to take a connection (its backlog full)


def relay_server_stderr(stream, out=None):
    """The MCP server's stderr into the log, every line prefixed: the server cannot write a line that reads as
    one of our `sandbox-ai-mcp-call` records."""
    out = out or sys.stderr
    with contextlib.suppress(OSError, ValueError):
        for line in iter(lambda: stream.readline(64 << 10), b""):
            out.write("mcp-server: " + line.decode("utf-8", "replace").rstrip("\n") + "\n")
            out.flush()


def mounts_changed(mounts):
    """Why the host folders MOUNTS ([{"path", "dev", "ino"}], recorded when the server started) may no longer be
    mounted, or None: a folder on the way became a symlink, or the folder was replaced. Podman resolves the
    path again for every container, so an agent able to write a parent could swap it for a link meanwhile."""
    for mount in mounts:
        path = Path(mount["path"])
        for part in (path, *path.parents):
            if part.is_symlink():
                return f"{part} has become a symlink since the server started"
        try:
            info = os.stat(path)
        except OSError as exc:
            return f"{path}: {exc.strerror}"
        if (info.st_dev, info.st_ino) != (mount["dev"], mount["ino"]):
            return f"{path} has been replaced since the server started"
    return None


class McpGateway:
    """The checks of one MCP server's tool calls, shared by its connections: the rules of the gateway
    configuration ({"server", "rules", "approvals"}), the questions to the human and the records. Records
    and questions are lines on OUT (stderr): on the host, the supervisor's log reader (mcp.py) writes them
    to mcp-calls.jsonl and into the approvals directory, which this side can only read for the answers."""

    def __init__(self, config=None, out=None, asker=None, taint=None, plain=False):
        config = config or {}
        # ASKER (the run's broker) and TAINT exist where the run is known: the supervisor's relay. PLAIN: records
        # as JSON lines (a log file) rather than prefixed lines for the log reader on the host.
        self.asker, self.taint, self.plain = asker, taint, plain
        self.server = str(config.get("server") or "")
        self.rules = Ruleset(config.get("rules", []))
        self.approvals = Path(config["approvals"]) if config.get("approvals") else None
        self.timeout = int(config.get("timeout", APPROVAL_SECONDS))
        self.out = out or sys.stderr
        self.lock = threading.Lock()
        # Answers for "run" (or "always", which is capped to it): until the server stops.
        self.decided, self.asked, self.token = {}, 0, secrets.randbelow(10 ** 9)
        self.who = Attribution()

    def client(self, connection):
        """{"process": ...} of the agent's process at the other end of CONNECTION, as the kernel reports it
        (SO_PEERCRED). The relay on the host (image servers) sees it; one in a server sandbox sees no process
        of another sandbox (PID 0). Which agent sandbox connected is not known here."""
        try:
            pid, uid, _ = Attribution.peer(connection)
        except OSError:
            return {"process": {"unknown": "no peer credentials"}}
        if pid <= 0:
            return {"process": {"unknown": "the client runs in another sandbox (its process is not visible here)",
                                "uid": uid}}
        info = self.who.process(pid)
        if info is None and sys.platform == "darwin":  # a relay on a Mac: no /proc to say more than the kernel did
            return {"process": {"pid": pid, "uid": uid}}
        return {"process": info or {"unknown": "the client process ended", "pid": pid, "uid": uid}}

    def emit(self, kind, record):
        """A record line (KIND sandbox-ai-mcp-call or sandbox-ai-mcp-ask); JSON keeps it on one line."""
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  **({"server": self.server} if self.server else {}), **record}
        line = json.dumps(record, ensure_ascii=False, default=repr)
        with self.lock:
            print(line if self.plain else f"{kind} {line}", file=self.out, flush=True)

    @staticmethod
    def arguments(arguments):
        """ARGUMENTS as the log and questions show them: shortened values (not JSON text: a multi-MB write_file
        must not stall the call, and a cut inside a string would leave "client_secret": "... without its
        closing quote, unmatched by the redaction), redacted, at most 2000 characters."""
        text = redact_text(json.dumps(shortened(arguments), ensure_ascii=False, default=repr))
        return text[:2000] + ("..." if len(text) > 2000 else "")

    def decide(self, tool, arguments, ident, client=None):
        """(allowed, reason) for a call of TOOL, asking the human where a rule says ask; the call is recorded
        with CLIENT, the calling process (see client)."""
        state = self.taint.shown() if self.taint is not None else None
        verdict = self.rules.mcp(self.server, tool, arguments, state)
        record = {"event": "call", "id": ident, "tool": tool[:200], "arguments": self.arguments(arguments),
                  **(client or {}), **({"tainted": state.get("since")} if state and state.get("tainted") else {})}
        if self.rules.mcp_active:
            record.update(decision=verdict["decision"], rule=verdict["rule"], ruleset=verdict["ruleset"])
        allowed, reason = verdict["decision"] == "allow", verdict["reason"]
        if verdict["decision"] == "ask":
            answer = self.ask(tool, record["arguments"], verdict, client)
            record["approval"] = {k: answer.get(k) for k in ("decision", "scope", "reason")}
            if answer.get("question"):
                record["question"] = answer["question"]
            allowed = answer.get("decision") == "allow"
            if not allowed:
                reason = f"not approved ({answer.get('reason', 'refused')}): {reason}"
        if not allowed:
            record["reason"] = reason
        self.emit("sandbox-ai-mcp-call", record)
        return allowed, reason

    def ask(self, tool, arguments, verdict, client=None):
        """The human's answer about a call (kind "mcp"), through the log reader on the host; refused after the
        timeout. CLIENT: the calling process, shown in the question."""
        if self.asker is not None:  # the run's broker: it asks, waits and remembers answers for the run
            answer = self.asker("mcp", {"server": self.server, "tool": tool, "arguments": arguments,
                                        "rule": verdict["rule"], "ruleset": verdict["ruleset"],
                                        "reason": printable_text(verdict["reason"], 1000),
                                        **({"taint": verdict["taint"]} if verdict.get("taint") else {}), **(client or {})})
            answer = answer if isinstance(answer, dict) else {}
            return {"decision": answer.get("decision", "deny"), "scope": answer.get("scope", "once"),
                    "reason": answer.get("reason", "no answer")}
        key = (verdict["ruleset"], verdict["rule"], tool)
        with self.lock:
            if key in self.decided:
                return self.decided[key]
            if self.approvals is None:
                return {"decision": "deny", "scope": "once", "reason": "this server cannot ask (no approvals directory)"}
            if self.asked >= RUN_QUESTIONS:
                return {"decision": "deny", "scope": "once", "reason": "too many questions"}
            self.asked += 1
            ident = f"{int(time.time())}-{self.token}-{self.asked}"
        self.emit("sandbox-ai-mcp-ask", {"id": ident, "kind": "mcp", "timeout": self.timeout, "tool": tool,
                                         "arguments": arguments, "rule": verdict["rule"], "ruleset": verdict["ruleset"],
                                         "reason": printable_text(verdict["reason"], 1000), **(client or {})})
        deadline, answer = time.monotonic() + self.timeout, None
        while time.monotonic() < deadline:
            answer = self.answer(ident)
            if answer:
                break
            time.sleep(0.2)
        answer = {**(answer or {"decision": "deny", "scope": "once", "reason": f"no answer within {self.timeout} s"}),
                  "question": ident}
        if answer["scope"] in ("run", "always"):
            with self.lock:  # later calls are not asked: their records name no question
                self.decided[key] = {**{k: v for k, v in answer.items() if k != "question"}, "scope": "run"}
        return answer

    def observe(self, tool):
        """The result of TOOL arrives: if a ruleset marks its results untrusted, the run is tainted first."""
        if self.taint is not None and tool:
            source = self.rules.untrusted_tool(self.server, tool)
            if source:
                self.taint.mark(source)

    def answer(self, ident):
        try:
            data = json.loads((self.approvals / f"{ident}.answer").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("decision") not in ("allow", "deny"):
            return {"decision": "deny", "scope": "once", "reason": "unreadable answer"}
        return {"decision": data["decision"], "scope": data.get("scope") if data.get("scope") in SCOPES else "once",
                "reason": "answered"}


class McpConnection:
    """One agent connection through the relay. The agent's lines are checked and passed on re-encoded, so that
    the server parses exactly what was checked; the server's lines go back with tools/list filtered and the
    results of tool calls recorded."""
    GATED = ("tools/call", "tools/list")

    def __init__(self, gateway, connection, process):
        self.gateway, self.connection, self.process = gateway, connection, process
        self.write_lock, self.lock = threading.Lock(), threading.Lock()
        self.pending = {}  # JSON of a request's id: {"method", "tool", "started"}
        self.client = gateway.client(connection)

    def send(self, data):
        with self.write_lock:
            self.connection.sendall(data)

    def reply(self, ident, error=None, result=None):
        message = {"jsonrpc": "2.0", "id": ident,
                   **({"error": {"code": error[0], "message": error[1]}} if error else {"result": result})}
        with contextlib.suppress(OSError):
            self.send((json.dumps(message) + "\n").encode())

    def refuse(self, ident, code, reason, **details):
        """Answer a message here instead of passing it on, and record why (and who sent it)."""
        self.gateway.emit("sandbox-ai-mcp-call", {"event": "refused", "reason": reason, **details, **self.client})
        self.reply(ident, error=(code, f"sandbox-ai: {reason}"))

    def forward(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def client_line(self, line):
        """Check one line from the agent: pass it on, or answer it here."""
        if not line.strip():
            return
        try:
            message, duplicated = Ruleset.parse_json(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # Not forwarded: a server reading JSON across lines could find a message here the relay never saw.
            return self.refuse(None, -32700, "not one JSON message on this line")
        if isinstance(message, list):
            methods = sorted({str(m.get("method"))[:100] for m in message if isinstance(m, dict) and "method" in m})
            return self.refuse(None, -32600, "JSON-RPC batches are not passed on (MCP has none since 2025-06-18)",
                               methods=methods[:20], messages=len(message))
        if not isinstance(message, dict):
            return self.refuse(None, -32600, "not a JSON-RPC object")
        ident = message.get("id", MISSING)
        # Strings and integers a server keeps as they are only: an id it may rewrite (1.0 as 1, an integer beyond
        # 2^53 in JavaScript) would not match its reply, which then passes without the gateway seeing it.
        if ident is not MISSING and ident is not None and (isinstance(ident, bool) or not isinstance(ident, (str, int))
                                                           or isinstance(ident, int) and abs(ident) > (1 << 53) - 1):
            return self.refuse(None, -32600, "an id is a string or an integer (at most 2^53 - 1)")
        shown = None if ident is MISSING else ident
        if duplicated:
            return self.refuse(shown, -32600, "a key given twice (servers read such messages differently)")
        method = message.get("method")
        if isinstance(method, str) and method not in self.GATED and method.strip().casefold() in self.GATED:
            return self.refuse(shown, -32601, f"no method {method[:100]!r} (method names are exact)")
        if method == "notifications/cancelled" and isinstance(message.get("params"), dict):
            with self.lock, contextlib.suppress(TypeError, ValueError):
                self.pending.pop(json.dumps(message["params"].get("requestId")), None)
        if isinstance(method, str) and ident is not MISSING:
            key = json.dumps(ident)
            with self.lock:
                if key in self.pending:
                    return self.refuse(shown, -32600, "the id of a request still open")
                if len(self.pending) >= MCP_PENDING_LIMIT:
                    return self.refuse(shown, -32000, f"more than {MCP_PENDING_LIMIT} requests open")
                self.pending[key] = {"method": method, "started": time.monotonic()}
        if method == "tools/call":
            return self.call(message, ident)
        self.forward(message)

    def call(self, message, ident):
        params = message.get("params")
        key = None if ident is MISSING else json.dumps(ident)
        shown = None if ident is MISSING else ident

        def refused(code, reason):
            with self.lock:
                self.pending.pop(key, None)
            self.refuse(shown, code, reason)
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return refused(-32602, "tools/call needs params.name")
        tool, arguments = params["name"], params.get("arguments", {})
        if not isinstance(arguments, dict):
            return refused(-32602, "params.arguments must be an object")
        if self.gateway.rules.mcp_active and not TOOL_NAME.fullmatch(tool):
            return refused(-32602, "tool names have 1 to 128 of A-Z a-z 0-9 _ - . : /")
        if key is not None:
            with self.lock:
                if key in self.pending:
                    self.pending[key]["tool"] = tool[:200]
        allowed, reason = self.gateway.decide(tool, arguments, shown, self.client)
        if allowed:
            return self.forward(message)
        with self.lock:
            self.pending.pop(key, None)
        if key is not None:  # a call without id (a notification) gets no answer, and is not run either
            self.reply(ident, result={"content": [{"type": "text", "text": f"sandbox-ai: {reason}"}], "isError": True})

    def pump(self):
        """The server's lines back to the agent until it ends."""
        with contextlib.suppress(OSError, ValueError):
            for line in iter(lambda: self.process.stdout.readline(MCP_LINE_LIMIT + 1), b""):
                self.send(self.server_line(line))

    def server_line(self, line):
        if not self.pending:
            return line
        message = None
        if len(line) <= MCP_LINE_LIMIT:
            with contextlib.suppress(ValueError, RecursionError):
                message = json.loads(line)
        if not isinstance(message, dict):
            # Not readable (too long, too deep, not JSON): it may be any pending call's result, so each pending
            # call counts as answered by its tool before the agent reads it (an untrusted one taints: fail closed).
            with self.lock:
                calls = [request.get("tool") for request in self.pending.values() if request["method"] == "tools/call"]
            for tool in calls:
                self.gateway.observe(tool)
            return line
        if "method" in message or "id" not in message:
            return line
        with self.lock:
            request = self.pending.pop(json.dumps(message["id"]), None)
            calls = [r.get("tool") for r in self.pending.values() if r["method"] == "tools/call"] if request is None else []
        if request is None:
            for tool in calls:  # a reply that matches no call may be any pending call's: fail closed, as above
                self.gateway.observe(tool)
            return line
        if request["method"] == "tools/list":
            return self.filtered(message, line)
        if request["method"] == "tools/call":
            self.gateway.observe(request.get("tool"))  # before the agent reads the result
            result = message.get("result") if isinstance(message.get("result"), dict) else {}
            self.gateway.emit("sandbox-ai-mcp-call", {
                "event": "result", "id": message["id"], "tool": request.get("tool"), "bytes": len(line),
                "is_error": "error" in message or result.get("isError") is True,
                "ms": int((time.monotonic() - request["started"]) * 1000)})
        return line

    def filtered(self, message, line):
        """A tools/list answer without the tools every call of which is denied."""
        result = message.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return line
        hidden = [tool["name"] for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                  and self.gateway.rules.hidden(self.gateway.server, tool["name"])]
        if not hidden:
            return line
        self.gateway.emit("sandbox-ai-mcp-call", {"event": "list", "hidden": [name[:200] for name in hidden[:100]]})
        kept = [tool for tool in tools if not (isinstance(tool, dict) and tool.get("name") in hidden)]
        return (json.dumps({**message, "result": {**result, "tools": kept}}) + "\n").encode()


MCP_RUN_DIR = Path("/run/sandbox-mcp")              # agent side: the supervisor's per-run relays
MCP_UPSTREAM_DIR = Path("/sandbox/mcp-upstream")    # supervisor side: the servers' sockets, hidden from the agent


def connect_socket_file(sock, name, dir_fd=None, timeout=MCP_CONNECT_SECONDS):
    """Connect SOCK to the Unix socket NAME (in the directory DIR_FD if given) without following a link: the
    socket's directory is the MCP server sandbox's to write, and a link it put there would resolve in the
    supervisor's own file system (its control socket, say). The socket file is opened (O_PATH, O_NOFOLLOW), checked
    to be a socket, and connected through its descriptor, waiting at most TIMEOUT seconds (a server that takes no
    more connections, its backlog full, is a refusal: socket.timeout); SOCK blocks again afterwards."""
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        if not stat.S_ISSOCK(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTSOCK, f"{name} is no socket (a link or file the server put there?)")
        sock.settimeout(timeout)
        sock.connect(f"/proc/self/fd/{fd}")
        sock.settimeout(None)
    finally:
        os.close(fd)


class McpSocket:
    """A running MCP server's socket in the place of a server process (mcp_serve's SPAWN): stdin and stdout
    are its two directions; closing stdin ends the requests, so the server's end finishes its answers. PATH is
    reached without following a link (connect_socket_file); DIR_FD: PATH is a name in that directory (a server
    attached during the run, its directory handed over as a descriptor)."""

    class Input:
        def __init__(self, sock):
            self.sock, self.stream = sock, sock.makefile("wb")

        def write(self, data):
            return self.stream.write(data)

        def flush(self):
            self.stream.flush()

        def close(self):
            with contextlib.suppress(OSError):
                self.stream.close()
                self.sock.shutdown(socket.SHUT_WR)

    def __init__(self, path, dir_fd=None):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connect_socket_file(self.sock, str(path), dir_fd)
        except OSError:
            self.sock.close()
            raise
        self.stdin, self.stdout = self.Input(self.sock), self.sock.makefile("rb")

    def wait(self):
        return 0

    def close(self):
        with contextlib.suppress(OSError):
            self.sock.close()


def start_hook_server(config, broker=None, taint=None, secret_values=(), path=None, log=None, stop=None):
    """The supervisor's answer to the agent's hooks (see HookServer) on PATH (HOOK_SOCKET) until STOP is set, with
    CONFIG's rules {"agent", "rules": [rulesets]}; LOG: tools.jsonl in the log directory. Returns once the socket
    is open to the agent."""
    path = Path(path or HOOK_SOCKET)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    log = log or (LOG_DIR / "tools.jsonl" if LOG_DIR.is_dir() else None)
    server = HookServer(Ruleset(config.get("rules", [])), broker, taint, log, secret_values)
    thread = threading.Thread(target=server.serve, args=(path, stop), daemon=True)
    thread.start()
    server.ready.wait(5)
    return thread


class McpRelay:
    """One MCP relay of the supervisor, as it is closed again (`sandbox-ai mcp detach --live`): its listening
    socket and the connections it serves (cut with it), its gateway, and for a server attached during the run the
    descriptor of the server's socket directory (DIR_FD), through which alone it reaches the server."""

    def __init__(self, gateway, dir_fd=None):
        self.gateway, self.dir_fd = gateway, dir_fd
        self.listener, self.connections, self.closed = None, set(), False
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def listening(self, listener):
        """mcp_serve's listener; False when the relay was closed meanwhile (then it is closed here)."""
        with self.lock:
            if not self.closed:
                self.listener = listener
        self.ready.set()
        if self.listener is not listener:
            listener.close()
            return False
        return True

    def add(self, connection):
        with self.lock:
            if self.closed:
                return False
            self.connections.add(connection)
            return True

    def discard(self, connection):
        with self.lock:
            self.connections.discard(connection)

    def connect(self):
        """The server's socket (McpSocket) through the directory descriptor; never once the relay is closed (its
        descriptor's number may name another file then). The lock is held only to take a copy of the descriptor
        (dup): the connection, which waits for the server (MCP_CONNECT_SECONDS at most), is made without it, so that
        a server that takes no connections never holds up close() or add() (nor the control socket with them)."""
        with self.lock:
            if self.closed or self.dir_fd is None:
                raise OSError(errno.ECONNREFUSED, "the MCP server was detached from this sandbox")
            fd = os.dup(self.dir_fd)
        try:
            return McpSocket("server.sock", fd)
        finally:
            os.close(fd)

    def close(self):
        """Stop accepting, cut the connections (the agent's end sees the server go), give the descriptor back."""
        with self.lock:
            if self.closed:
                return False
            self.closed = True
            listener, connections, fd, self.dir_fd = self.listener, list(self.connections), self.dir_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)  # wakes the accept() of mcp_serve
            with contextlib.suppress(OSError):
                listener.close()
        return True


class McpRelays:
    """The supervisor's MCP relays of this run by server name: the start's (their servers' sockets mounted at
    UPSTREAM_DIR, hidden from the agent) and those `sandbox-ai mcp attach --live` opens during the run (the server's
    socket directory handed over the control socket as a descriptor: no mount, nothing the agent can reach). The
    agent's bridge reaches RUN_DIR/NAME/server.sock. Here the run is known: the rules for tainted runs hold,
    questions go to the run's BROKER, results of untrusted tools taint the run, calls are logged to LOG
    (mcp-calls.jsonl). LIVE: RUN_DIR was made before the agent started, so the agent sees it read-only (Firejail's
    --read-only holds for what is made in it later): only then are relays opened during the run."""

    def __init__(self, broker=None, taint=None, run_dir=None, upstream_dir=None, log=None, live=False):
        self.broker, self.taint, self.live = broker, taint, live
        self.run_dir, self.upstream_dir = Path(run_dir or MCP_RUN_DIR), Path(upstream_dir or MCP_UPSTREAM_DIR)
        self.log = log or (LOG_DIR / "mcp-calls.jsonl" if LOG_DIR.is_dir() else None)
        self.out, self.relays, self.threads = None, {}, []
        self.lock = threading.Lock()

    def stream(self):
        if self.out is None:
            self.out = open(self.log, "a", buffering=1) if self.log else open(os.devnull, "w")  # noqa: SIM115
        return self.out

    def names(self):
        with self.lock:
            return sorted(self.relays)

    def open(self, name, config, dir_fd=None, wait=5):
        """The relay of NAME with the gateway CONFIG: to UPSTREAM_DIR/NAME/server.sock, or (DIR_FD) to server.sock
        in the directory that descriptor holds (taken over: closed with the relay)."""
        directory = self.run_dir / name
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        gate = McpGateway(config, self.stream(), asker=self.broker.ask if self.broker is not None else None,
                          taint=self.taint, plain=True)
        relay = McpRelay(gate, dir_fd)
        upstream = self.upstream_dir / name / "server.sock"
        spawn = relay.connect if dir_fd is not None else (lambda path=upstream: McpSocket(path))
        thread = threading.Thread(target=mcp_serve, daemon=True, args=(str(directory / "server.sock"), None),
                                  kwargs={"gateway": gate, "spawn": spawn, "relay": relay})
        with self.lock:
            self.relays[name] = relay
        thread.start()
        self.threads.append(thread)
        relay.ready.wait(wait)  # attach() checks that it listens (relay.listener), a start's relay goes on as before
        return relay

    def attach(self, name, config, dir_fd):
        """`sandbox-ai mcp attach --live`: open the relay of NAME to the server's socket directory DIR_FD (taken
        over); False (and DIR_FD closed) when this run relays NAME already."""
        if not self.live:
            os.close(dir_fd)
            raise ValueError("this sandbox made no place for MCP servers attached while it runs (started by an older "
                             "version): restart it with the server (sandbox-ai mcp attach NAME START --restart)")
        with self.lock:
            known = name in self.relays
        if known:
            os.close(dir_fd)
            return False
        try:
            relay = self.open(name, config, dir_fd)
            if relay.listener is None:  # not listening within the wait: never reported opened
                raise OSError(errno.ETIMEDOUT, f"the relay of {name} did not open its socket in the sandbox")
        except BaseException:
            with self.lock:
                failed = self.relays.pop(name, None)
            if failed is not None:
                failed.close()
            else:
                with contextlib.suppress(OSError):
                    os.close(dir_fd)
            raise
        relay.gateway.emit("sandbox-ai-mcp-call", {"event": "attached", "live": True})
        return True

    def detach(self, name):
        """`sandbox-ai mcp detach --live`: close the relay of NAME (a start's too) and take its socket away; False
        when this run relays no NAME."""
        with self.lock:
            relay = self.relays.pop(name, None)
        if relay is None:
            return False
        relay.close()
        directory = self.run_dir / name
        with contextlib.suppress(OSError):
            (directory / "server.sock").unlink()
        with contextlib.suppress(OSError):
            directory.rmdir()
        relay.gateway.emit("sandbox-ai-mcp-call", {"event": "detached", "live": True})
        return True


def start_mcp_relays(relays, broker=None, taint=None, who=None, run_dir=None, upstream_dir=None, log=None,
                     registry=None):
    """The supervisor's MCP relays of this run, one per server (RELAYS: {name: gateway configuration}): the
    agent's bridge reaches RUN_DIR/NAME/server.sock, the relay the server's socket in UPSTREAM_DIR (hidden from
    the agent, so it cannot go around). Here the run is known: the rules for tainted runs hold, questions go
    to the run's BROKER, results of untrusted tools taint the run, calls are logged to LOG (mcp-calls.jsonl).
    REGISTRY: the run's McpRelays, which keeps them (so that they can be closed during the run)."""
    if not relays:
        return []
    registry = registry or McpRelays(broker, taint, run_dir, upstream_dir, log)
    threads = []
    for name, config in relays.items():
        registry.open(name, config)
        threads.append(registry.threads[-1])
    return threads


def mcp_serve(socket_path, command, cwd="/work", out=None, gateway=None, spawn=None, relay=None):
    """Server sandbox: one instance of the stdio MCP server per connection, behind the gateway (GATEWAY: its
    configuration, see McpGateway). Every tool call is written as a line `sandbox-ai-mcp-call {json}` to OUT
    (stderr: on the host, mcp.py writes these to the server's mcp-calls.jsonl). RELAY (McpRelay, the supervisor's
    relays): it keeps the listener and the connections, and closing it ends this loop (returns 0)."""
    out = out or sys.stderr
    gate = gateway if isinstance(gateway, McpGateway) else McpGateway(gateway, out)
    server = listen_unix(socket_path)
    if relay is not None and not relay.listening(server):
        return 0
    slots = threading.BoundedSemaphore(MCP_INSTANCE_LIMIT)
    mounts = json.loads(os.environ.get("SANDBOX_AI_MCP_MOUNTS") or "[]")  # an image server's host folders
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            if relay is not None and relay.closed:
                return 0
            raise
        changed = mounts_changed(mounts)
        if changed:
            print(f"agent-sandbox: MCP server connection refused: {changed}", file=sys.stderr, flush=True)
            connection.close()
            continue
        if not slots.acquire(blocking=False):
            # Each instance may be a container of its own: an agent opening connections in a loop must not
            # fill the host.
            print(f"agent-sandbox: more than {MCP_INSTANCE_LIMIT} connections to this MCP server at once; "
                  "connection refused", file=sys.stderr, flush=True)
            connection.close()
            continue
        if relay is not None and not relay.add(connection):  # closed meanwhile
            connection.close()
            slots.release()
            return 0

        def run(connection=connection):
            with connection:
                try:
                    if spawn is not None:  # a running server's socket (the supervisor's per-run relay)
                        process = spawn()
                    else:
                        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=cwd,
                                                   stderr=subprocess.PIPE)
                except OSError as exc:
                    print(f"agent-sandbox: MCP server failed: {exc}", file=sys.stderr)
                    slots.release()
                    if relay is not None:  # never kept until the detach: the agent's end is closed here
                        relay.discard(connection)
                    return
                pump = None
                try:
                    # Inside the try: a thread that cannot start (the sandbox's process limit) ends this connection
                    # with its server instance and gives its slot back, instead of keeping both for good.
                    if isinstance(process, subprocess.Popen):
                        threading.Thread(target=relay_server_stderr, args=(process.stderr,), daemon=True).start()
                    session = McpConnection(gate, connection, process)
                    pump = threading.Thread(target=session.pump, daemon=True)
                    pump.start()
                    stream = connection.makefile("rb")
                    while True:
                        line = stream.readline(MCP_LINE_LIMIT + 1)
                        if not line:
                            break
                        if len(line) > MCP_LINE_LIMIT:
                            print(f"agent-sandbox: MCP message over {MCP_LINE_LIMIT >> 20} MiB; connection closed",
                                  file=sys.stderr)
                            break
                        session.client_line(line)
                except (OSError, ValueError, RuntimeError):
                    pass
                finally:
                    try:
                        with contextlib.suppress(OSError):
                            process.stdin.close()
                        if isinstance(process, subprocess.Popen):
                            # A server that ignores the end of its input is stopped: it must not keep its slot.
                            stop_process(process, grace=MCP_EOF_GRACE)
                        else:
                            process.wait()
                        if pump is not None and pump.ident is not None:
                            pump.join(30)  # the server's last answers reach the agent before the connection closes
                        getattr(process, "close", lambda: None)()
                    finally:
                        slots.release()
                        if relay is not None:
                            relay.discard(connection)
        try:
            threading.Thread(target=run, daemon=True).start()
        except RuntimeError:  # no thread to be had (the sandbox's process limit): this connection only
            if relay is not None:
                relay.discard(connection)
            connection.close()
            slots.release()
            time.sleep(0.1)


def mcp_bridge(socket_path):
    """Agent sandbox: connect the agent's stdio to an MCP server socket."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(socket_path)
    except OSError as exc:
        raise RuntimeError(f"MCP server socket unavailable: {socket_path}: {exc}") from None
    def upstream():
        try:
            while data := os.read(0, 65536):
                client.sendall(data)
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    threading.Thread(target=upstream, daemon=True).start()
    try:
        while data := client.recv(65536):
            view = memoryview(data)
            while view:
                view = view[os.write(1, view):]
    except ConnectionResetError:
        pass  # the server instance ended without reading everything
    return 0


def namespace_state():
    state = {name: os.readlink(f"/proc/self/ns/{name}") for name in ["mnt", "pid"]}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb", "NoNewPrivs", "Seccomp"):
            state[key] = value.strip()
    return state


def verify_inner(before, current):
    if any(before[key] == current[key] for key in ("mnt", "pid")):
        raise RuntimeError("Firejail did not create both mount and PID namespaces; refusing unconfined agent")
    if any(int(current[key], 16) for key in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb")):
        masks = ", ".join(f"{key}={value}" for key, value in current.items() if key.startswith("Cap"))
        raise RuntimeError(f"Firejail left Linux capabilities enabled: {masks}")
    if current["NoNewPrivs"] != "1" or current["Seccomp"] != "2":
        raise RuntimeError("Firejail did not enable no-new-privileges and seccomp")


def wait_child(child):
    previous = {}
    def forward(sig, _frame):
        if child.poll() is None:
            child.send_signal(sig)
            # A frozen child (a canary's pause, sandbox-ai pause) would hold the signal until thawed, and this wait
            # with it: Ctrl-C or sandbox-ai net stop would hang. Thawed, it ends (and takes its namespace along).
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signal.SIGCONT)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[sig] = signal.signal(sig, forward)
    try:
        return child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def inner_bootstrap():
    # Firejail 0.9.80's check_kernel_procs() skips confinement when none of
    # /proc/{1..10}/comm begins with a kernel-worker name. In a fresh PID
    # namespace our PID-1 supervisor deliberately supplies this name. This only
    # bypasses that heuristic: the guard below verifies real confinement.
    if os.getpid() != 1:
        raise RuntimeError("Bootstrap must be PID 1 in a fresh outer PID namespace")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(15, ctypes.c_char_p(b"kthreadd"), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot name namespace supervisor")
    try:
        subprocess.run(["nft", "-f", "/sandbox/session/firewall.nft"], check=True)
    except subprocess.CalledProcessError:
        with contextlib.suppress(OSError):
            if "type nat" in Path("/sandbox/session/firewall.nft").read_text():
                print("agent-sandbox: the connection proxy redirects with nftables NAT; load its modules on the "
                      "host with `sudo modprobe -a nft_chain_nat nft_redir` (or start with --no-proxy)",
                      file=sys.stderr)
        raise
    # Installation failure aborts before Firejail or the agent starts.
    subprocess.run(["nft", "list", "table", "inet", "agent_sandbox"], check=True, stdout=subprocess.DEVNULL)
    cfg = json.loads(SESSION_CONFIG.read_text()) if SESSION_CONFIG.exists() else {}
    before = namespace_state()
    proxy_config = json.loads((PROXY_DIR / "proxy.json").read_text()) if cfg.get("proxy") else {}
    base = proxy_config.get("allow", {})
    policy = cfg.get("policy") or {}
    network = RuntimeNetwork(base, PROXY_RUN / "allow.json" if cfg.get("proxy") else None,
                             within=policy.get("allow_within"))
    # A strict security mode (the launcher's modes.py): it caps answers and refuses what `sandbox-ai net` would open.
    mode = cfg.get("mode") if isinstance(cfg.get("mode"), dict) else None
    scope = narrowest_scope(cfg.get("ask_max_scope", "always"), (mode or {}).get("approval_scope", "always"))
    broker = ApprovalBroker(CONTROL_DIR / "approvals", network, max_scope=scope, blocked=cfg.get("ask_blocked"),
                            no_inspect=proxy_config.get("no_inspect", [])) \
        if cfg.get("ask") and CONTROL_DIR.is_dir() else None
    if broker is not None:  # who caps an answer's scope, for the safeguard record of a capped answer
        broker.capped_by = ("policy" if narrowest_scope(cfg.get("ask_max_scope", "always")) == scope else "mode",
                            (mode or {}).get("name"))
    # Which process a connection, lookup or dropped packet belongs to: read here, where /proc shows them all.
    env = cfg.get("env", {})
    who = Attribution(sandbox=cfg.get("sandbox"), debug=bool(cfg.get("log_debug")),
                      secret_values=[env[name] for name in cfg.get("secret_names", []) if name in env])
    canaries = Canaries.load(who=who) if CONTROL_DIR.is_dir() else None  # decoy files of the run (canary.py)
    loggers = start_loggers(broker, cfg.get("ask_quiet", []), who, canaries, network) if LOG_DIR.is_dir() else None
    network.dns_stub = loggers is not None  # `sandbox-ai net allow --dns` needs it (resolv.conf names it)
    # `sandbox-ai net allow --allow-host-port`: the host relays listen in the control directory; with the proxy
    # the relayed ports go through it (the relay chain of the firewall).
    network.relay_dir = CONTROL_DIR if CONTROL_DIR.is_dir() else None
    network.proxied = bool(cfg.get("proxy"))
    machine = cfg.get("machine") if isinstance(cfg.get("machine"), dict) else None
    host_relays = []
    if machine:  # macOS (see VM_CONTROL_DIR): the control socket in the VM's own /run, the Mac's ports by its address
        VM_CONTROL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        network.upstream_host = str(ipaddress.ip_address(machine["host"]))
        # pasta's -T would reach the VM's loopback, not the Mac's: the ports of the start are relayed here instead.
        host_relays = [PortRelay(port, CONTROL_DIR, upstream=(network.upstream_host, int(port)))
                       for port in base.get("loopback", [])]
    held_loopback = hold_unserved_loopback(base.get("loopback", []))  # before the agent starts; kept for the run
    # The run's taint (see Taint): the proxy reads its mirror; its events go to the connection log.
    taint = Taint(mirror=PROXY_RUN / "taint.json" if cfg.get("proxy") else None,
                  log=LOG_DIR / cfg["proxy"]["log"] if cfg.get("proxy") and LOG_DIR.is_dir() else None)
    gui, proxy, control = [], None, None
    # The MCP relays' directory, made before the agent starts (so that Firejail keeps it read-only for the agent, also
    # what is made in it later): servers attached during the run (sandbox-ai mcp attach --live) get their relay there.
    try:
        MCP_RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        live_mcp = MCP_RUN_DIR.is_dir() and not MCP_RUN_DIR.is_symlink() and CONTROL_DIR.is_dir()
    except OSError:
        live_mcp = False
    relays = McpRelays(broker, taint, live=live_mcp)
    guard = threading.Event()
    MemoryGuard(CONTROL_DIR if CONTROL_DIR.is_dir() else None).start(guard)
    try:
        if canaries is not None:
            canaries.start()
        if cfg.get("proxy"):
            proxy = start_proxy(cfg["proxy"], broker, who, taint)
        if CONTROL_DIR.is_dir():
            control = threading.Event()
            rules = RunRules(proxy_config, PROXY_RUN / RELOADED_RULES if cfg.get("proxy") else None,
                             policy.get("forbid", []))
            threading.Thread(target=control_server, daemon=True,
                             args=((VM_CONTROL_DIR if machine else CONTROL_DIR) / "control.sock", network, control,
                                   policy.get("forbid", []), mode, taint, relays, True, rules),
                             kwargs={"token_file": CONTROL_DIR / EXEC_TOKEN if machine else None}).start()
        if machine and machine.get("browser") and CONTROL_DIR.is_dir():
            threading.Thread(target=browser_relay, kwargs={"stop": control}, daemon=True).start()
        start_mcp_relays(cfg.get("mcp_relay") or {}, broker, taint, who, registry=relays)
        problem = hook_problem(cfg)
        if problem:
            print(f"agent-sandbox: {problem}", file=sys.stderr)
            safeguard_event("guard", "the run's tool rules (the agent's hooks)", problem)
            return 1
        if cfg.get("hooks") is not None:  # the agent's hooks: their rules, the human's questions, tools.jsonl
            start_hook_server(cfg["hooks"], broker, taint,
                              [env[name] for name in cfg.get("secret_names", []) if name in env])
        if GUI_CONFIG.exists():
            gui = start_gui(json.loads(GUI_CONFIG.read_text()))
        if AUDIO_CONFIG.exists():
            gui += start_audio(json.loads(AUDIO_CONFIG.read_text()))  # stopped with the display at the end
        # Built only now: the blacklist names the proxy's directories, which exist from here on.
        cmd = firejail_command(before)
        child = subprocess.Popen(cmd, env=base_env(), close_fds=True)
        oom_first(child.pid)  # an out-of-memory kill takes the agent's processes before PID 1 (the whole run)
        return wait_child(child)
    finally:
        guard.set()
        for process in gui:
            stop_process(process)
        if proxy:
            stop_process(proxy)
        if control:
            control.set()
        network.close_relays()
        if canaries is not None:
            canaries.close()
        if loggers:
            loggers.set()
            for thread in loggers.threads:
                thread.join(3)
            poll_denied(loggers.seen)


def start_gui(cfg):
    """Start Xvfb (and x11vnc on a Unix socket) before the agent; fail closed."""
    processes = []
    try:
        processes.append(subprocess.Popen(DROP_ALL + ["Xvfb", cfg["display"], "-screen", "0", cfg["screen"],
                                                      "-nolisten", "tcp"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        deadline = time.monotonic() + 15
        while not x_socket(cfg["display"]).exists():
            if processes[0].poll() is not None:
                raise RuntimeError(f"Xvfb exited with status {processes[0].poll()}")
            if time.monotonic() > deadline:
                raise RuntimeError("Timed out waiting for Xvfb")
            time.sleep(0.1)
        if cfg["vnc"]:
            # -rfbport 0: no TCP listener; only the private Unix socket. Started only once it accepts
            # connections; otherwise the run stops with x11vnc's own message.
            errors = tempfile.TemporaryFile()
            processes.append(subprocess.Popen(DROP_ALL + ["x11vnc", "-display", cfg["display"], "-unixsock",
                                                          str(VNC_DIR / "vnc.sock"), "-rfbport", "0", "-forever",
                                                          "-shared", "-nopw", "-quiet", "-nosel"],
                                              stdout=subprocess.DEVNULL, stderr=errors))
            wait_for_unix_listener(VNC_DIR / "vnc.sock", processes[-1], errors, "x11vnc")
    except BaseException:
        for process in processes:
            stop_process(process)
        raise
    return processes


AUDIO_CONFIG = Path("/sandbox/session/audio.json")
AUDIO_DIR = Path("/tmp/sandbox-audio")
AUDIO_SOCKET = AUDIO_DIR / "native"
AUDIO_UID = 2  # PulseAudio refuses to run as root (the sandbox's uid 0): a user of its own, as the proxy has


def audio_command(socket_path=AUDIO_SOCKET):
    """PulseAudio as AUDIO_UID without capabilities: a null sink (what is played can be recorded from its monitor,
    nothing reaches a sound card) and the native protocol on SOCKET_PATH for the agent's programs."""
    return [*DROP_ALL[:1], f"--reuid={AUDIO_UID}", f"--regid={AUDIO_UID}", "--clear-groups", *DROP_ALL[1:],
            # --disallow-module-loading: a client (the agent) loads nothing more into this process, which runs
            # outside Firejail (a module is a shared library: its code would run here).
            "pulseaudio", "--daemonize=no", "--exit-idle-time=-1", "--disallow-exit", "--disallow-module-loading",
            "--disable-shm=yes", "--use-pid-file=no", "-n",
            "--load=module-null-sink sink_name=sandbox sink_properties=device.description=sandbox",
            f"--load=module-native-protocol-unix socket={socket_path} auth-anonymous=1",
            "--load=module-always-sink"]


def start_audio(cfg, which=shutil.which, popen=subprocess.Popen, directory=AUDIO_DIR, uid=AUDIO_UID):
    """--audio: a sound server only inside the sandbox, before the agent; [] (and a note) when the image has none.
    Not fail-closed as the display is: without it nothing is opened, the agent's programs just find no sound."""
    if not which("pulseaudio"):
        print("agent-sandbox: --audio: this image has no PulseAudio; make one with: sandbox-ai images recipe media "
              "--from AGENT, then start with --image-variant media", file=sys.stderr)
        return []
    directory.mkdir(mode=0o755, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chown(directory, uid, uid)
    socket_path = directory / "native"
    errors = tempfile.TemporaryFile()
    env = {"HOME": str(directory), "XDG_RUNTIME_DIR": str(directory), "PATH": base_env()["PATH"]}
    process = popen(audio_command(socket_path), env=env, stdout=subprocess.DEVNULL, stderr=errors)
    try:
        wait_for_unix_listener(socket_path, process, errors, "pulseaudio")
    except RuntimeError as exc:
        stop_process(process)
        print(f"agent-sandbox: --audio: {exc}", file=sys.stderr)
        return []
    return [process]


def wait_for_unix_listener(path, process, errors, name, timeout=15.0):
    """Wait until PROCESS accepts connections on the Unix socket PATH; RuntimeError with what it wrote to
    ERRORS (a file) if it exits or does not get there within TIMEOUT."""
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None or time.monotonic() > deadline:
            errors.seek(0)
            text = errors.read()[-2000:].decode("utf-8", "replace").strip()
            state = f"exited with status {process.poll()}" if process.poll() is not None else "did not listen"
            raise RuntimeError(f"{name} {state}" + (f": {text}" if text else ""))
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(path))
                return
            except OSError:
                pass
        time.sleep(0.1)


def log_event(name, record):
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    with open(LOG_DIR / name, "a") as stream:
        stream.write(json.dumps(record) + "\n")


SAFEGUARD_FILE = "safeguard-events.jsonl"
SAFEGUARD_LIMIT = 4 << 20


def safeguard_event(kind, what, why, directory=None, **fields):
    """A safeguard the supervisor applied (KIND "mode", "policy" or "guard"; WHAT was refused, WHY), for the
    launcher's safeguard watch (safeguards.py, which says the fix): a line of CONTROL_DIR/safeguard-events.jsonl,
    which the agent cannot see. Plain text, bounded; never raises."""
    directory = Path(directory) if directory is not None else CONTROL_DIR
    with contextlib.suppress(Exception):
        if not directory.is_dir():
            return
        path = directory / SAFEGUARD_FILE
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_size >= SAFEGUARD_LIMIT:
                return
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": str(kind)[:20],
                 "what": printable_text(str(what), 300), "why": printable_text(redact_text(str(why)), 500),
                 **{key: printable_text(str(value), 200) for key, value in fields.items() if value is not None}}
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with open(fd, "a") as stream:
            stream.write(json.dumps(entry) + "\n")


def start_loggers(broker=None, quiet=(), who=None, canaries=None, network=None):
    # Runs in PID 1 outside Firejail; the agent cannot see LOG_DIR. WHO (an Attribution) names the process
    # behind a refused lookup or a dropped packet; CANARIES (Canaries) checks the names looked up.
    cfg = json.loads(SESSION_CONFIG.read_text())
    stop = threading.Event()
    stop.seen = memory_part("denied seen", set())
    stop.threads = []
    def denied_loop():
        while not stop.wait(1):
            poll_denied(stop.seen, who)
    starting = {wildcard_suffix(v) for v in cfg.get("wildcards", [])}

    def wildcards():
        with WILDCARDS_LOCK:
            return starting | WILDCARDS
    dns = (set(cfg["pinned"]), 53, stop, broker, quiet, who, canaries, network.nameservers if network else None, 53,
           wildcards)
    loggers = [(denied_loop, ()), (dns_logger, dns), (process_logger, (stop, 0.5, Path("/proc"),
                                                                     [cfg.get("env", {})[k] for k in cfg.get("secret_names", [])
                                                                      if k in cfg.get("env", {})]))]
    if cfg["watch"]:  # file access only with an explicit --log-dir
        loggers.append((file_logger, (cfg["watch"], stop)))
    for target, args in loggers:
        thread = threading.Thread(target=target, args=args, name=target.__name__, daemon=True)
        thread.start()
        stop.threads.append(thread)
    return stop


PROCESS_LOG_LIMIT = 256 << 20  # processes.jsonl per log directory


def process_logger(stop, interval=0.5, proc=Path("/proc"), secret_values=(), limit=PROCESS_LOG_LIMIT):
    """processes.jsonl: every process that appears in the sandbox (polled: very short ones can be missed),
    with its command line, redacted (patterns, and the exact values of SECRET_VALUES: what --env and
    --secret-env pass). Runs in PID 1 outside Firejail, which sees the agent's processes. Stops at LIMIT."""
    seen = set()
    memory_part("processes seen", lambda: len(seen))
    values = sorted({v for v in secret_values if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True)
    try:
        written = (LOG_DIR / "processes.jsonl").stat().st_size
    except OSError:
        written = 0
    while not stop.wait(interval):
        if written >= limit:
            log_event("processes.jsonl", {"error": f"processes.jsonl reached {limit >> 20} MiB; no more entries"})
            return
        try:
            pids = [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
        except OSError:
            continue
        current = set()
        for pid in pids:
            try:
                stat_fields = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
                start = stat_fields[19]
                key = (pid, start)
                current.add(key)
                if key in seen:
                    continue
                with open(proc / str(pid) / "cmdline", "rb") as stream:
                    argv = stream.read(4096).split(b"\0")
            except (OSError, IndexError, ValueError):
                continue
            seen.add(key)
            command = " ".join(part.decode("utf-8", "replace") for part in argv if part)
            for value in values:
                command = command.replace(value, "[redacted]")
            command = redact_text(command)
            if command:
                entry = {"pid": pid, "ppid": int(stat_fields[1]), "command": command[:1000]}
                log_event("processes.jsonl", entry)
                written += len(json.dumps(entry)) + 32
        seen &= current


def poll_denied(seen, who=None):
    """egress-denied.jsonl: each destination the firewall dropped (first time), with the process whose socket
    is connected to it (WHO, an Attribution; a TCP socket waits in SYN_SENT while it retries)."""
    for version in (4, 6):
        result = subprocess.run(["nft", "-j", "list", "set", "inet", "agent_sandbox", f"denied{version}"],
                                text=True, capture_output=True)
        if result.returncode:
            continue
        for item in json.loads(result.stdout).get("nftables", []):
            for elem in item.get("set", {}).get("elem", []):
                if isinstance(elem, dict) and "elem" in elem:
                    elem = elem["elem"].get("val", elem["elem"])
                key = tuple(elem["concat"]) if isinstance(elem, dict) and "concat" in elem else None
                if key and key not in seen:
                    seen.add(key)
                    found = who.by_remote(key[1], key[0], key[2]) if who is not None else {}
                    log_event("egress-denied.jsonl", {"address": key[0], "protocol": key[1], "port": key[2], **found})


def dns_reply(query):
    # NXDOMAIN for the single question, preserving id, RD bit and question.
    end = 12
    while end < len(query) and query[end]:
        end += 1 + query[end]
    labels, pos = [], 12
    while pos < end:
        labels.append(query[pos + 1:pos + 1 + query[pos]].decode("ascii", "replace"))
        pos += 1 + query[pos]
    flags = 0x8083 | (struct.unpack("!H", query[2:4])[0] & 0x0100)
    return ".".join(labels).lower(), query[:2] + struct.pack("!HHHHH", flags, 1, 0, 0, 0) + query[12:end + 5]


# NAT64 prefixes count as global to Python, yet a local translator turns them into any IPv4 address: the LAN, this
# host. Never public here.
NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def public_address(ip):
    """IP (an ipaddress object) is a public address a name may open: global, and no NAT64 translation of another."""
    return ip.is_global and not (ip.version == 6 and any(ip in net for net in NAT64))


def dns_address_reply(query, addresses, ttl=30):
    """The answer to the single question of QUERY with ADDRESSES (those of its type: A or AAAA), id, RD bit and
    question kept; no record of the asked type (an AAAA question for a name with IPv4 addresses only) is NOERROR
    without answers, so the client takes the other type instead of giving up. None for other types."""
    end = 12
    while end < len(query) and query[end]:
        end += 1 + query[end]
    if end + 5 > len(query):
        return None
    qtype = struct.unpack("!H", query[end + 1:end + 3])[0]
    if qtype not in (1, 28):
        return None
    family = socket.AF_INET if qtype == 1 else socket.AF_INET6
    records = []
    for address in addresses:
        try:
            packed = socket.inet_pton(family, address)
        except (OSError, ValueError, TypeError):
            continue
        records.append(struct.pack("!HHHIH", 0xC00C, qtype, 1, ttl, len(packed)) + packed)
    flags = 0x8180 | (struct.unpack("!H", query[2:4])[0] & 0x0100)
    return query[:2] + struct.pack("!HHHHH", flags, 1, len(records), 0, 0) + query[12:end + 5] + b"".join(records)


# Host wildcards (--allow-host *.DOMAIN): the suffixes, from the start (config.json "wildcards") and added during the
# run (sandbox-ai net allow, control op "wildcards"). A lookup of a name below one waits for its question, which the
# launcher answers on the host by itself; the answer's addresses go back at once.
WILDCARDS = set()
WILDCARDS_LOCK = threading.Lock()
# Suffixes under which anyone can have a name of their own (a public suffix, a hosting or cloud service): a wildcard
# at or above one would let the agent open any server it rents there, unasked (*.X matches names at every depth
# below X). Three sources, all refused:
# - the Public Suffix List (ICANN and PRIVATE sections), a pinned copy beside this file (public_suffix_list.dat; the
#   launcher binds it into the sandbox next to /sandbox/runtime.py): *.X is refused when X is a public suffix, or
#   when a public suffix lies below X (*.amazonaws.com covers s3.amazonaws.com, where anyone has a bucket);
# - SHARED_SUFFIXES: hosting, tunnel and request-catcher services (some not in the list at all), refused at or
#   below them: they hand out names at any depth;
# - COUNTRY_SECOND_LEVEL: two-label country domains (co.uk, com.au, ...) by their shape.
# Without the list file (a copy of sandbox-ai that lacks it), FALLBACK_PUBLIC_SUFFIXES stands in for the list:
# private-section entries refused at or below them. A list file that is there but malformed refuses every wildcard.
SHARED_SUFFIXES = frozenset("""
amazonaws.com cloudfront.net elasticbeanstalk.com awsapprunner.com on.aws lambda-url.on.aws
azurewebsites.net azureedge.net azure-api.net azurestaticapps.net cloudapp.net cloudapp.azure.com blob.core.windows.net
trafficmanager.net windows.net appspot.com googleusercontent.com googleapis.com run.app cloudfunctions.net web.app
firebaseapp.com firebaseio.com withgoogle.com github.io githubusercontent.com gitlab.io pages.dev workers.dev
r2.dev netlify.app netlify.com vercel.app now.sh herokuapp.com herokudns.com onrender.com fly.dev
railway.app up.railway.app glitch.me repl.co replit.dev replit.app deno.dev ngrok.io ngrok-free.app ngrok.app
loca.lt trycloudflare.com serveo.net surge.sh readthedocs.io blogspot.com wordpress.com wixsite.com
squarespace.com tumblr.com myshopify.com webflow.io carrd.co neocities.org digitaloceanspaces.com ondigitalocean.app
linodeobjects.com scw.cloud ovh.net duckdns.org no-ip.org ddns.net dyndns.org freedns.org sslip.io nip.io
xip.io traefik.me localtest.me lvh.me
devtunnels.ms b-cdn.net azurefd.net webhook.site pipedream.net interact.sh oast.fun oast.pro oast.live oast.site
oast.online oast.me oastify.com burpcollaborator.net lhr.life localhost.run serveousercontent.com localtunnel.me
pinggy.link bore.pub requestbin.net beeceptor.com canarytokens.com dnslog.cn ceye.io
""".split())
FALLBACK_PUBLIC_SUFFIXES = frozenset("""
github.io githubusercontent.com gitlab.io devtunnels.ms ts.net hf.space azurefd.net b-cdn.net supabase.co csb.app
eu.org vercel.app vercel.dev netlify.app pages.dev workers.dev fly.dev herokuapp.com appspot.com blogspot.com
cloudfront.net azurewebsites.net azurestaticapps.net web.app firebaseapp.com run.app ngrok.io ngrok.app ngrok-free.app
ngrok.dev ngrok-free.dev trycloudflare.com onrender.com glitch.me repl.co readthedocs.io surge.sh myshopify.com
deno.dev r2.dev
""".split())
COUNTRY_SECOND_LEVEL = re.compile(r"(?:co|com|net|org|gov|edu|ac|or|ne|go|gob|nic|mil|ltd|plc|sch|info|biz)\.[a-z]{2}")
WILDCARD_SUFFIX = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")
PUBLIC_SUFFIX_FILE = Path(__file__).resolve().parent / "public_suffix_list.dat"
PSL_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
PSL_SECTIONS = ("// ===BEGIN ICANN DOMAINS===", "// ===END ICANN DOMAINS===", "// ===BEGIN PRIVATE DOMAINS===",
                "// ===END PRIVATE DOMAINS===")
class PublicSuffixList:
    """The Public Suffix List, parsed strictly: both sections there, whole and in order, a "*" only as a rule's
    first label, a "!" only before a rule, at least 1000 rules; anything else is a ValueError (a list that cannot be
    read refuses every wildcard, never none). Labels outside ASCII are kept in their xn-- form, as DNS has them."""
    _loaded, _lock = {}, threading.Lock()

    def __init__(self, text):
        lines = [line.strip() for line in text.splitlines()]
        marks = [lines.index(mark) if mark in lines else -1 for mark in PSL_SECTIONS]
        if -1 in marks or marks != sorted(marks):
            raise ValueError("not the Public Suffix List: its ICANN and PRIVATE sections are missing or out of order")
        self.rules, self.wildcards, self.exceptions = set(), set(), set()
        for number, line in enumerate(lines, 1):
            if not line or line.startswith("//"):
                continue
            rule = line.split()[0].lower()  # a rule ends at the first white space
            exception = rule.startswith("!")
            labels = rule[1:].split(".") if exception else rule.split(".")
            try:
                if any(not label for label in labels):
                    raise ValueError("an empty label")
                wildcard = labels[0] == "*"
                if wildcard and exception:
                    raise ValueError("an exception with a wildcard")
                if any("*" in label for label in labels[1:]) or "*" in labels[0] and not wildcard:
                    raise ValueError('"*" not as the first label')
                labels = [self.label(label) for label in (labels[1:] if wildcard else labels)]
                if not labels:
                    raise ValueError("a bare *")
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"public suffix list line {number}: {printable_text(line, 100)!r}: {exc}") from None
            (self.exceptions if exception else self.wildcards if wildcard else self.rules).add(".".join(labels))
        if len(self.rules) < 1000:
            raise ValueError(f"the public suffix list has only {len(self.rules)} rules: cut short")
        self.above = set()  # every name a public suffix lies below
        for name in self.rules:
            parts = name.split(".")
            self.above.update(".".join(parts[i:]) for i in range(1, len(parts)))
        for name in self.wildcards:  # *.P: every LABEL.P is public, so P and what it lies below
            parts = name.split(".")
            self.above.update(".".join(parts[i:]) for i in range(len(parts)))

    @staticmethod
    def label(label):
        """A rule's LABEL in the form DNS uses (xn-- for one outside ASCII); ValueError for anything else."""
        if not label.isascii():
            label = "xn--" + label.encode("punycode").decode("ascii")
        if not PSL_LABEL.fullmatch(label):
            raise ValueError(f"not a label: {label!r}")
        return label

    def public(self, name):
        """NAME (lower case, dotted) is a public suffix: listed, or below a *. rule, and no ! exception."""
        parent = name.split(".", 1)[1] if "." in name else ""
        return name not in self.exceptions and (name in self.rules or parent in self.wildcards)

    def refuses(self, suffix):
        """*.SUFFIX would reach names anyone can register: SUFFIX is a public suffix, or one lies below it."""
        return self.public(suffix) or suffix in self.above

    @classmethod
    def load(cls, path=None):
        """The list of PATH (default: PUBLIC_SUFFIX_FILE), read once; None when there is no such file
        (FALLBACK_PUBLIC_SUFFIXES stands in); ValueError when it cannot be read or parsed."""
        path = Path(path) if path is not None else PUBLIC_SUFFIX_FILE
        with cls._lock:
            if str(path) not in cls._loaded:
                try:
                    cls._loaded[str(path)] = cls(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    cls._loaded[str(path)] = None
                except (OSError, UnicodeError) as exc:
                    cls._loaded[str(path)] = ValueError(f"the public suffix list {path} cannot be read: {exc}")
                except ValueError as exc:
                    cls._loaded[str(path)] = exc
            loaded = cls._loaded[str(path)]
        if isinstance(loaded, ValueError):
            raise ValueError(str(loaded))
        return loaded


def wildcard_suffix(value, path=None):
    """"example.com" of "*.example.com" (lower case); ValueError for anything else: a wildcard needs at least two
    labels below it (*.com would open a whole top-level domain), and none at or above a public suffix or a shared
    service (SHARED_SUFFIXES, PublicSuffixList; PATH: another public suffix list, for tests)."""
    if not isinstance(value, str) or not value.startswith("*.") or not WILDCARD_SUFFIX.fullmatch(value[2:].lower()) \
            or "." not in value[2:]:
        raise ValueError(f"not a host wildcard *.DOMAIN.TLD: {printable_text(str(value), 100)!r}")
    suffix = value[2:].lower()
    shared = next((s for s in SHARED_SUFFIXES if suffix == s or suffix.endswith("." + s)), None)
    if not shared and COUNTRY_SECOND_LEVEL.fullmatch(suffix):
        shared = suffix
    if not shared:
        try:
            listed = PublicSuffixList.load(path)
        except ValueError as exc:
            raise ValueError(f"not a host wildcard here: {exc}; every wildcard is refused until it is restored") \
                from None
        if listed is None:
            shared = next((s for s in FALLBACK_PUBLIC_SUFFIXES if suffix == s or suffix.endswith("." + s)), None)
        elif listed.refuses(suffix):
            shared = suffix
    if shared:
        raise ValueError(f"not a host wildcard here: it reaches {printable_text(shared, 100)}, where anyone can have "
                         "a name of their own (a public suffix or hosting service), so the agent could open any server "
                         "it rents there; give the hosts by name instead")
    return suffix


def under_wildcard(name, suffixes):
    """NAME lies below one of SUFFIXES (a subdomain, not the domain itself: *.example.com is not example.com)."""
    name = str(name).lower().rstrip(".")
    return any(name.endswith("." + suffix) for suffix in suffixes)


def change_wildcards(request, forbid=(), mode=None):
    """Control op "wildcards": {"add": [...], "remove": [...]} of *.DOMAIN; the wildcards now. The administrator's
    policy and a strict security mode refuse --allow-host here as at the start."""
    add = [wildcard_suffix(v) for v in request.get("add", []) or []]
    remove = [wildcard_suffix(v) for v in request.get("remove", []) or []]
    if add and ("--allow-host" in forbid or "--allow-host" in ((mode or {}).get("refuses") or [])):
        raise ValueError("--allow-host is refused for this run (the administrator's policy or its security mode)")
    with WILDCARDS_LOCK:
        WILDCARDS.update(add)
        WILDCARDS.difference_update(remove)
        return sorted(WILDCARDS)


DNS_WORKERS = 8      # lookups the DNS stub forwards at once
DNS_BACKOFF = 30.0   # seconds a nameserver that did not answer is skipped while another one is there to ask


def forward_dns(query, servers, timeout=2.0, port=53, failed=None):
    """(answer, server) of the first of SERVERS (UDP PORT) to answer QUERY with the query's id, or None. FAILED
    ({server: when it last did not answer}, kept by the caller): a server that did not answer within DNS_BACKOFF
    seconds is skipped while another one is there; when all did not, they are asked, the longest failed first."""
    if failed is not None:
        now = time.monotonic()
        servers = [s for s in servers if now - failed.get(s, -DNS_BACKOFF) >= DNS_BACKOFF] or \
            sorted(servers, key=lambda s: failed.get(s, 0.0))
    for server in servers:
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        with contextlib.suppress(OSError):
            with socket.socket(family, socket.SOCK_DGRAM) as upstream:
                upstream.settimeout(timeout)
                upstream.connect((server, port))  # answers from anywhere else are not taken
                upstream.send(query)
                answer = upstream.recv(4096)
                if len(answer) >= 12 and answer[:2] == query[:2]:
                    if failed is not None:
                        failed.pop(server, None)
                    return answer, server
        if failed is not None:
            failed[server] = time.monotonic()
    return None


def dns_logger(pinned, port=53, stop=None, broker=None, quiet=(), who=None, canaries=None, forward=None,
               upstream_port=53, wildcards=None):
    # Unpinned names never get an address unless DNS was allowed during the run (FORWARD: the nameservers
    # `sandbox-ai net allow --dns` gave): then the stub asks them, and every name it forwards is logged
    # (dns.jsonl). Otherwise it records the attempt and, with approvals, files a question (the lookup is
    # answered at once: a retry after the approval reaches the name). A name with a canary token is never
    # forwarded. WHO (an Attribution) names the process that asked: its UDP socket is still open.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    if stop is not None:
        sock.settimeout(0.2)
    # Forwarded lookups wait for nameservers (2 s each that does not answer): beside this loop, DNS_WORKERS at
    # once, so a dead nameserver holds neither the other lookups nor the pinned names; FAILED: its back-off.
    failed, workers = {}, threading.BoundedSemaphore(DNS_WORKERS)

    def forwarded(query, peer, name, reply, servers):
        try:
            found = who.lookup("udp", peer[0], peer[1]) if who is not None else {}
            if canaries is not None and canaries.lookup(name, found):
                sock.sendto(reply, peer)  # the token stays here
                return
            answered = forward_dns(query, servers, port=upstream_port, failed=failed)
            # Logged before the answer goes out: once a program has its address, the record of it exists.
            log_event("dns.jsonl", {"host": name, "forwarded": answered[1] if answered else None,
                                    **({} if answered else {"error": "no nameserver answered"}), **found})
            sock.sendto(answered[0] if answered else reply, peer)
        except (OSError, ValueError, RuntimeError):
            pass
        finally:
            workers.release()

    def held(query, peer, name, reply, found):
        """A name below an allowed wildcard: its question is answered on the host by the launcher (a second or
        so); the addresses go back to the client, which still waits, instead of a refusal it would not retry."""
        try:
            # "wildcard": the suffix it lies below; such questions have a budget of their own (ApprovalBroker.ask).
            suffix = next((s for s in sorted(wildcards(), key=len) if under_wildcard(name, {s})), True)
            answer = broker.ask("dns", {"name": name, "wildcard": suffix, **found}, wait=True) or {}
            addresses = (answer.get("addresses") or []) if answer.get("decision") == "allow" else []
            sock.sendto((dns_address_reply(query, addresses) if addresses else None) or reply, peer)
        except (OSError, ValueError, RuntimeError, struct.error):
            pass
        finally:
            workers.release()
    while stop is None or not stop.is_set():
        try:
            query, peer = sock.recvfrom(4096)
            name, reply = dns_reply(query)
            if name not in pinned and broker is not None and wildcards is not None and \
                    re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}",
                                 name) and \
                    under_wildcard(name, wildcards()) and not (canaries is not None and canaries.names.name(name)):
                if workers.acquire(blocking=False):
                    found = who.lookup("udp", peer[0], peer[1]) if who is not None else {}
                    try:
                        threading.Thread(target=held, args=(query, peer, name, reply, found), name="dns-wildcard",
                                         daemon=True).start()
                        continue
                    except RuntimeError:
                        workers.release()
            servers = forward() if forward is not None and name not in pinned else []
            if servers:
                if not workers.acquire(blocking=False):
                    continue  # DNS_WORKERS lookups wait for nameservers already: the client asks again
                try:
                    threading.Thread(target=forwarded, args=(query, peer, name, reply, servers), name="dns-forward",
                                     daemon=True).start()
                except RuntimeError:  # no thread to be had (the sandbox's process limit): the client asks again
                    workers.release()
                continue
            sock.sendto(reply, peer)
            if name not in pinned:
                found = who.lookup("udp", peer[0], peer[1]) if who is not None else {}
                log_event("egress-denied.jsonl", {"host": name, "reason": "not in allowed hosts", **found})
                # A name with a canary token is reported and never asked about: the question would go to the host,
                # which looks the name up (below a wildcard by itself), and the token would leave through its DNS.
                hit = canaries.lookup(name, found) if canaries is not None else None
                if not hit and broker is not None and \
                        re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}",
                                     name) and \
                        not any(name == q or name.endswith("." + q) for q in quiet):
                    broker.ask("dns", {"name": name, **found}, wait=False)
        except (OSError, ValueError, IndexError, struct.error, RuntimeError):
            continue
    sock.close()


FILE_PRUNE_SECONDS = 60.0
FILE_DEPTH = 200  # directories below a watched root followed at most this deep
# Watches the file log takes at most: inotify's limit is the host user's, shared with every program of theirs
# (editors, file managers): half of it, at most 65536, so a run that makes directories without end never uses it up.
FILE_WATCHES = 65536


def watch_budget(proc=Path("/proc")):
    try:
        limit = int((Path(proc) / "sys/fs/inotify/max_user_watches").read_text().strip())
    except (OSError, ValueError):
        return FILE_WATCHES // 8
    return max(1024, min(FILE_WATCHES, limit // 2))
# Workspaces added during the run (sandbox-ai workspace add, through the control socket): watched from then on.
FILE_ROOTS_ADDED = []
FILE_ROOTS_LOCK = threading.Lock()
FILE_LOGGING = threading.Event()
ADDED_WORKSPACE = re.compile(r"/workspaces/[^/\0\n\r]{1,255}")


def watch_added_workspace(path):
    """Have the file log watch the workspace PATH (/workspaces/NAME, mounted during the run); True if the run keeps
    a file log. ValueError for anything else: only the human reaches the control socket, still nothing but a
    directory below /workspaces is taken."""
    if not isinstance(path, str) or not ADDED_WORKSPACE.fullmatch(path) or os.path.basename(path) in (".", ".."):
        raise ValueError("watch: a directory /workspaces/NAME")
    if os.path.islink(path) or not os.path.isdir(path):
        raise ValueError(f"watch: {path} is no directory in the sandbox")
    if not FILE_LOGGING.is_set():
        return False
    with FILE_ROOTS_LOCK:
        FILE_ROOTS_ADDED.append(path)
    return True


def live_watches(fd, proc=Path("/proc")):
    """The watch descriptors the kernel still holds on inotify FD (/proc/self/fdinfo), or None if unreadable or FD is
    no inotify instance (then nothing may be pruned)."""
    try:
        if os.readlink(Path(proc) / "self" / "fd" / str(fd)) != "anon_inode:inotify":
            return None
        text = (Path(proc) / "self" / "fdinfo" / str(fd)).read_text()
    except OSError:
        return None
    return {int(match, 16) for match in re.findall(r"^inotify wd:([0-9a-f]+)", text, re.MULTILINE)}


def file_logger(roots, stop=None, prune_every=FILE_PRUNE_SECONDS):
    """files.jsonl: what happens to the files below ROOTS (inotify). A directory's watch goes when the directory
    does (IN_IGNORED); when the event queue overflows those events are lost, and since the kernel never reuses a
    watch number their entries would stay for the whole run: test suites that make and remove tens of thousands of
    directories a run grew the supervisor by gigabytes that way until the OOM killer ended the run. So after an
    overflow, and every PRUNE_EVERY seconds, the table is matched with the watches the kernel still holds. When
    the supervisor runs short of memory (MemoryGuard) the file log stops, with a note."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(0o2000000)
    if fd < 0:
        log_event("files.jsonl", {"error": "inotify unavailable"})
        return
    mask = sum(INOTIFY_EVENTS) | 0x02000000 | 0x04000000  # DONT_FOLLOW, EXCL_UNLINK
    paths = {}
    exhausted = []
    budget = watch_budget()
    # Through the directory's descriptor (/proc/self/fd/N, a link that must be followed): what is watched is the
    # directory the walk opened, not whatever a path names by now (the agent may swap a directory for a link).
    by_descriptor = (mask & ~0x02000000) | 0x01000000  # without DONT_FOLLOW, with ONLYDIR

    def watch(top):
        if os.path.islink(top):
            return  # Swapped for a symlink: never followed out of the watched tree.
        # fwalk opens each directory without following links and checks it is the one it listed.
        base = top.rstrip("/").count("/")
        for directory, dirs, _, dir_fd in os.fwalk(top, follow_symlinks=False):
            if exhausted:
                return
            if len(paths) >= budget:  # the file log's own share of the user's watches
                exhausted.append(directory)
                log_event("files.jsonl", {"error": f"file log watch budget ({budget}) reached; later directories "
                                                   "unwatched", "path": directory})
                return
            if directory.count("/") - base >= FILE_DEPTH:  # deeper is no project: never a recursion that ends the log
                dirs.clear()
            wd = libc.inotify_add_watch(fd, os.fsencode(f"/proc/self/fd/{dir_fd}"), by_descriptor)
            if wd < 0:
                errno = ctypes.get_errno()
                if errno == 28:  # ENOSPC: the host user's watch limit is shared
                    exhausted.append(directory)
                    log_event("files.jsonl", {"error": "inotify watch limit reached; later directories unwatched",
                                              "path": directory})
                    return
                log_event("files.jsonl", {"error": os.strerror(errno), "path": directory})
                dirs.clear()
                continue
            paths[wd] = directory
    def watch_safely(top):
        try:
            watch(top)
        except (OSError, RecursionError, ValueError) as exc:  # the log goes on with what it has
            log_event("files.jsonl", {"error": f"not all directories watched: {type(exc).__name__}", "path": top})
    for root in roots:
        watch_safely(root)
    FILE_LOGGING.set()
    recent = {}
    memory_part("file watches", lambda: len(paths))
    memory_part("file recent", lambda: len(recent))
    shed = threading.Event()
    memory_shedder("file log", shed.set)
    pruned, overflowed = time.monotonic(), False

    def prune():
        live = live_watches(fd)
        if live is not None:
            for wd in [wd for wd in paths if wd not in live]:
                del paths[wd]
    while True:
        if shed.is_set():
            log_event("files.jsonl", {"error": "file log stopped: sandbox-ai's supervisor ran short of memory; "
                                               "the run goes on without it"})
            paths.clear()
            recent.clear()
            FILE_LOGGING.clear()  # workspace add must not say "watched" any more
            break
        if overflowed or time.monotonic() - pruned >= prune_every:
            prune()
            pruned, overflowed = time.monotonic(), False
        if FILE_ROOTS_ADDED:
            with FILE_ROOTS_LOCK:
                added, FILE_ROOTS_ADDED[:] = list(FILE_ROOTS_ADDED), []
            for root in added:
                watch_safely(root)
                log_event("files.jsonl", {"action": "watch", "path": root, "note": "workspace added during the run"})
        # After stop, queued events are still drained before returning.
        if stop is not None and not select.select([fd], [], [], 0.2)[0]:
            if stop.is_set():
                break
            continue
        data = os.read(fd, 65536)
        pos = 0
        while pos < len(data):
            wd, event, _cookie, size = struct.unpack_from("iIII", data, pos)
            name = data[pos + 16:pos + 16 + size].rstrip(b"\0").decode("utf-8", "replace")
            pos += 16 + size
            if event & 0x4000:
                log_event("files.jsonl", {"error": "event queue overflow; events lost"})
                overflowed = True  # IN_IGNORED of removed directories may be among the lost ones
                continue
            if event & 0x8000:  # IN_IGNORED: the watch is gone (directory removed): forget its path
                paths.pop(wd, None)
                continue
            path = os.path.join(paths.get(wd, "?"), name)
            if event & 0x40000000:  # directory
                if event & (0x100 | 0x80):
                    watch_safely(path)
                if event & 0x18:
                    continue
            action = next((label for bit, label in INOTIFY_EVENTS.items() if event & bit), None)
            now = time.monotonic()
            if action and now - recent.get((action, path), -2) >= 2:
                if len(recent) >= FILE_RECENT_LIMIT:  # only the last 2 s matter; a long run touches millions of paths
                    recent = {key: seen for key, seen in recent.items() if now - seen < 2}
                    while len(recent) >= FILE_RECENT_LIMIT:
                        recent.pop(next(iter(recent)))
                recent[(action, path)] = now
                log_event("files.jsonl", {"action": action, "path": path})
    os.close(fd)


# The agent's processes get the kernel's highest oom_score_adj: the OOM killer then always takes one of them while any
# is left (it adds 1000/1000 of the memory limit to their score), never PID 1, the supervisor, whose death ends the run.
OOM_MARGIN = 1000


def oom_first(pid, proc=Path("/proc"), margin=OOM_MARGIN):
    """Raise PID's oom_score_adj MARGIN above the supervisor's, so the kernel's OOM killer picks the agent's
    processes (which inherit it) before PID 1, whose death ends the whole run. Raising needs no privilege;
    lowering the supervisor's own would (CAP_SYS_RESOURCE of the host). The value written, or None."""
    try:
        own = int((proc / "self" / "oom_score_adj").read_text().strip())
        value = max(-1000, min(1000, own + margin))
        (proc / str(int(pid)) / "oom_score_adj").write_text(f"{value}\n")
        return value
    except (OSError, ValueError, TypeError):
        return None  # no /proc, or the process ended: the run goes on as before


# --- the supervisor's own memory ---------------------------------------------------------------------
#
# The supervisor is PID 1 of the sandbox: if the kernel's OOM killer takes it, the whole run ends. Runs were lost
# that way with 1.2 and 2.8 GB in it. MemoryGuard looks at its own resident memory every SUPERVISOR_MEMORY_INTERVAL
# seconds, gives freed heap back to the system (malloc_trim) and, each time it passes the next step (256 MiB, then
# double), writes what it holds to supervisor-memory.jsonl in the run's control directory (the launcher's side
# reads it): its threads by name, the most numerous object types and the size of each table registered in
# MEMORY_PARTS. From SUPERVISOR_TRACE_FROM it traces allocations (tracemalloc), so the steps after that name the
# lines that keep allocating. Only sizes, counts and code locations: no data of the run.

SUPERVISOR_MEMORY_INTERVAL = 30.0
SUPERVISOR_MEMORY_STEP = 256 << 20
SUPERVISOR_TRACE_FROM = 512 << 20
SUPERVISOR_MEMORY_RECORDS = 12   # steps written per run at most
SUPERVISOR_MEMORY_FILE = "supervisor-memory.jsonl"
MEMORY_PARTS = {}  # name: () -> int, the number of entries of one of the supervisor's tables
MEMORY_SHEDDERS = {}  # name: () -> None, drops something the run can go on without (MemoryGuard, from SHED_FROM)
SUPERVISOR_SHED_FROM = 768 << 20


def memory_shedder(name, shed):
    """Register SHED (a callable) under NAME: MemoryGuard calls it once when the supervisor passes SHED_FROM."""
    MEMORY_SHEDDERS[name] = shed
    return shed


def memory_part(name, sized):
    """Register SIZED (anything with a len, or a callable giving a number) under NAME for MemoryGuard's records."""
    MEMORY_PARTS[name] = sized if callable(sized) and not hasattr(sized, "__len__") else (lambda: len(sized))
    return sized


def resident_bytes(proc=Path("/proc")):
    """This process's resident memory (VmRSS) in bytes, or None."""
    try:
        for line in (Path(proc) / "self" / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class MemoryGuard:
    """See above. DIRECTORY: where supervisor-memory.jsonl goes (None: nowhere, only the trimming); RSS() reads the
    resident memory; TRIM() gives freed memory back; TRACE (bool) whether allocations may be traced."""

    def __init__(self, directory=None, rss=resident_bytes, trim=None, step=SUPERVISOR_MEMORY_STEP,
                 trace_from=SUPERVISOR_TRACE_FROM, records=SUPERVISOR_MEMORY_RECORDS, trace=True,
                 interval=SUPERVISOR_MEMORY_INTERVAL, parts=None, shed_from=SUPERVISOR_SHED_FROM, shedders=None):
        self.directory, self.rss, self.step, self.trace_from = directory, rss, step, trace_from
        self.records, self.trace, self.interval, self.shed_from = records, trace, interval, shed_from
        self.parts = MEMORY_PARTS if parts is None else parts
        self.shedders = MEMORY_SHEDDERS if shedders is None else shedders
        self.shed = []  # the names already called
        self.trim = trim if trim is not None else self.malloc_trim()
        self.next, self.written, self.peak = step, 0, 0

    @staticmethod
    def malloc_trim():
        try:
            function = ctypes.CDLL(None).malloc_trim
        except (OSError, AttributeError):  # not glibc (musl has none): nothing to give back this way
            return lambda: None
        function.argtypes, function.restype = [ctypes.c_size_t], ctypes.c_int
        return lambda: function(0)

    def census(self, top=15):
        """{type name: count} of the most numerous objects the garbage collector tracks."""
        import gc
        counts = {}
        for item in gc.get_objects():
            name = type(item).__name__
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda pair: -pair[1])[:top])

    def snapshot(self, rss):
        threads = {}
        for thread in threading.enumerate():
            name = re.sub(r"[-_ ]?\d+(?: \(.*\))?$", "", thread.name) or "thread"
            threads[name] = threads.get(name, 0) + 1
        sizes = {}
        for name, size in list(self.parts.items()):
            with contextlib.suppress(Exception):
                sizes[name] = int(size())
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rss": rss, "peak": self.peak,
                  "threads": threads, "parts": sizes}
        with contextlib.suppress(Exception):
            record["objects"] = self.census()
        import tracemalloc
        if tracemalloc.is_tracing():
            with contextlib.suppress(Exception):
                stats = tracemalloc.take_snapshot().statistics("lineno")[:15]
                record["traced"] = [{"where": f"{stat.traceback[0].filename.rsplit('/', 1)[-1]}:"
                                              f"{stat.traceback[0].lineno}", "bytes": stat.size, "count": stat.count}
                                    for stat in stats]
        return record

    def check(self):
        """One look; the record written (or None)."""
        with contextlib.suppress(Exception):
            self.trim()
        rss = self.rss()
        if rss is None:
            return None
        self.peak = max(self.peak, rss)
        if rss >= self.shed_from:
            self.shed_memory(rss)
        if rss < self.next:
            return None
        while self.next <= rss:
            self.next *= 2
        if self.trace and rss >= self.trace_from:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start(1)  # from here on: the growth, not what was there before
        if self.written >= self.records:
            return None
        record = self.snapshot(rss)
        self.written += 1
        if self.directory is not None:
            with contextlib.suppress(OSError):
                with open(Path(self.directory) / SUPERVISOR_MEMORY_FILE, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
        return record

    def shed_memory(self, rss):
        """Call each shedder not called yet; one record of what was dropped."""
        names = [name for name in list(self.shedders) if name not in self.shed]
        if not names:
            return
        for name in names:
            self.shed.append(name)
            with contextlib.suppress(Exception):
                self.shedders[name]()
        import gc
        gc.collect()
        with contextlib.suppress(Exception):
            self.trim()
        if self.directory is not None:
            with contextlib.suppress(OSError):
                with open(Path(self.directory) / SUPERVISOR_MEMORY_FILE, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rss": rss,
                                             "shed": names}) + "\n")

    def run(self, stop):
        while not stop.wait(self.interval):
            with contextlib.suppress(Exception):  # the guard never ends the run
                self.check()

    def start(self, stop):
        thread = threading.Thread(target=self.run, args=(stop,), name="memory-guard", daemon=True)
        thread.start()
        return thread


def firejail_command(before):
    help_text = subprocess.run(["firejail", "--help"], check=True, text=True, capture_output=True).stdout
    command = ["firejail", "--noprofile", "--quiet", "--caps.drop=all", "--nonewprivs", "--seccomp"]
    for private in (LOG_DIR, VNC_DIR, PROXY_DIR, PROXY_RUN, CONTROL_DIR, MCP_UPSTREAM_DIR, VM_CONTROL_DIR):
        if private.is_dir():
            command.append(f"--blacklist={private}")
    # The supervisor's sockets for the agent: reachable, not replaceable (the agent runs as the same user).
    # macOS (VM_CONTROL_DIR made): the browser socket is the supervisor's too (browser_relay), on a tmpfs.
    machine = (Path(BROWSER_SOCKET).parent,) if VM_CONTROL_DIR.is_dir() else ()
    for shared in (MCP_RUN_DIR, HOOK_DIR, *machine):
        if shared.is_dir():
            command.append(f"--read-only={shared}")
    # New releases always exec directly and reject the former shell option.
    if "--shell=" in help_text:
        command.append("--shell=none")
    return ["setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--", *command,
            "/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-guard", json.dumps(before)]


def base_env():
    return {"HOME": "/root", "USER": "root", "LOGNAME": "root", "SHELL": "/bin/bash",
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
            "TERM": os.environ.get("TERM", "xterm-256color"), "LANG": "C.UTF-8",
            "CODEX_HOME": "/root/.codex",
            "XDG_CONFIG_HOME": "/root/.config", "XDG_DATA_HOME": "/root/.local/share",
            "XDG_CACHE_HOME": "/root/.cache"}


def tmux_command(command, detached=False):
    """tmux session: the agent in window 0, a sandboxed bash in window 1.

    Detached (named sessions): the guard keeps the sandbox alive for as long
    as the tmux session exists, so clients can attach and detach."""
    tmux = ["tmux", "-u", "new-session", *(["-d"] if detached else []), "-s", "sandbox", "-n", "agent", "-c", "/work",
            shlex.join(command), ";", "new-window", "-d", "-n", "shell", "-c", "/work", "bash"]
    if not detached:
        return tmux
    # Terminals attach through a byte relay started here, inside Firejail: a
    # host-side tmux client could be told by the agent's server to run commands.
    relay = shlex.join(["python3", "-I", "/sandbox/runtime.py", "--internal-attach-relay", ATTACH_SOCKET])
    return ["sh", "-c", f"{shlex.join(tmux)} && ({relay} &) && "
                        "while tmux has-session -t sandbox 2>/dev/null; do sleep 1; done"]


def inner_guard(before):
    current = namespace_state()
    verify_inner(before, current)
    cfg = json.loads(SESSION_CONFIG.read_text())
    if cfg["probe"]:
        print(json.dumps({"verified": True, "firejail": current, "outer": before}), flush=True)
        return 0
    env = base_env()
    env.update(cfg["env"])
    # Both tmux windows start here, after verification and inside Firejail.
    cmd = tmux_command(cfg["command"], detached=cfg["tmux"] == "detached") if cfg.get("tmux") else cfg["command"]
    os.chdir("/work")
    os.execvpe(cmd[0], cmd, env)


# --- connection proxy ------------------------------------------------------------------------------
#
# The firewall redirects every TCP connection of the agent here (see the launcher's firewall_rules).
# The proxy runs outside Firejail as its own user (PROXY_UID), whose connections the redirect skips,
# and connects to the original destination itself, still behind the firewall. TLS is opened with a
# certificate from a CA made for this run alone, which the agent trusts; the real server is checked
# against the system's CAs. HTTP/1.1 exchanges are logged with headers and bodies (redacted and
# truncated); a Git push passes the privacy check before the server sees it.

PROXY_DIR = Path("/sandbox/proxy")        # from the launcher: proxy.json, push.json, privacy.py
PROXY_RUN = Path("/run/sandbox-proxy")    # CA key and server certificates: the proxy user's alone
CA_DIR = Path("/run/sandbox-ca")          # the CA certificate and the CA bundle the agent trusts
PROXY_PORT = 15001
PROXY_UID = 1
SYSTEM_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem")
HEAD_LIMIT = 64 << 10
ADVERT_LIMIT = 16 << 20
COMMANDS_LIMIT = 1 << 20
PUSH_CHECK_SECONDS = 600  # one push's privacy check, at most (each of its Git commands has 300 s in privacy.py)
PEEK_TIMEOUT = 1.0
IDLE_TIMEOUT = 3600
ZERO_ID = re.compile(rb"0{40}|0{64}")
HTTP_PORTS = (80, 443)       # other protocols there are refused: they would only hide HTTP from the checks
LEADING_CRLF = re.compile(rb"^(?:\r\n)+")
OPENSSL_CONFIG = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = sandbox-ai
[ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = $ENV::SAN
authorityKeyIdentifier = keyid:always
subjectKeyIdentifier = none
"""
SECRET_HEADER = re.compile(r"auth|cookie|token|secret|key|passw|session|signature|credential", re.I)
SECRET_TEXT = [
    (re.compile(r'("[A-Za-z0-9_-]*(?:token|secret|passw|api_?key|apikey|authorization|credential|session'
                r'|cookie|code_verifier|private|signature)[A-Za-z0-9_-]*"\s*:\s*)"(?:[^"\\]|\\.)*"', re.I),
     r'\1"[redacted]"'),
    (re.compile(r"(\b(?:[a-z_]*token|secret|client_secret|password|api_?key|code|code_verifier|signature|sig)=)"
                r"[^&\s\"']+", re.I), r"\1[redacted]"),
    (re.compile(r"\b(Bearer|Basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 [redacted]"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]{0,20}://[^/\s:@]{0,200}:)[^@\s/]{1,500}@", re.I), r"\1[redacted]@"),
    (re.compile(r"(--?[a-z0-9_-]*(?:token|secret|password|passwd|api-?key|apikey|credential)[a-z0-9_-]*(?:=|\s+))"
                r"[^\s\"'-][^\s\"']*", re.I), r"\1[redacted]"),
    (re.compile(r"((?:^|\s)(?:-u|--user)\s+[^\s:]{1,200}:)\S+"), r"\1[redacted]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|glpat-[A-Za-z0-9_-]{16,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
                r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+)"), "[redacted]"),
]


def run_openssl(args, san="DNS:sandbox-ai"):
    """Run the openssl tool; the configuration reads SAN from the environment."""
    result = subprocess.run(["openssl", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                            env={"PATH": "/usr/bin:/bin", "SAN": san}, timeout=60)
    if result.returncode:
        raise RuntimeError(f"openssl {args[0]} failed: {result.stderr.strip()[-300:]}")


def create_ca(run_dir, ca_dir, bundles=SYSTEM_BUNDLES):
    """A CA for this run in RUN_DIR (with the key all server certificates share) and, for the agent,
    CA_DIR/ca.pem plus CA_DIR/bundle.pem: the system's CAs and this one. RUN_DIR/system.pem keeps the
    system's CAs for the proxy's own checks."""
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (run_dir / "certs").mkdir(mode=0o700, exist_ok=True)
    ca_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    config = run_dir / "openssl.cnf"
    config.write_text(OPENSSL_CONFIG)
    run_openssl(["req", "-x509", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                 "-keyout", str(run_dir / "ca.key"), "-out", str(run_dir / "ca.pem"), "-days", "30",
                 "-subj", "/CN=sandbox-ai session CA (this run only)", "-config", str(config), "-extensions", "ca"])
    run_openssl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                 "-out", str(run_dir / "leaf.key")])
    system = next((Path(path).read_bytes() for path in bundles if Path(path).is_file()), b"")
    (run_dir / "system.pem").write_bytes(system)
    certificate = (run_dir / "ca.pem").read_bytes()
    for name, content in (("ca.pem", certificate), ("bundle.pem", system.rstrip(b"\n") + b"\n" + certificate)):
        (ca_dir / name).write_bytes(content.lstrip(b"\n"))
        (ca_dir / name).chmod(0o644)


def server_certificate(run_dir, host):
    """Path of the certificate for HOST (a DNS name or an IP address), made on first use."""
    try:
        san = f"IP:{ipaddress.ip_address(host)}"
    except ValueError:
        if len(host) > 253 or not re.fullmatch(r"(?:[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*"
                                               r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.?", host):
            raise ValueError(f"not a host name: {host!r}")
        host = host.rstrip(".").lower()
        san = f"DNS:{host}"
    path = run_dir / "certs" / f"{host}.pem"
    if not path.exists():
        temporary = path.with_name(f".{host}.{threading.get_ident()}.pem")
        run_openssl(["req", "-x509", "-new", "-key", str(run_dir / "leaf.key"), "-out", str(temporary), "-days", "30",
                     "-subj", f"/CN={host[:64]}", "-config", str(run_dir / "openssl.cnf"), "-extensions", "leaf",
                     "-CA", str(run_dir / "ca.pem"), "-CAkey", str(run_dir / "ca.key")], san=san)
        os.replace(temporary, path)
    return path


HOST_NAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)*"
                       r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")


def question_name(name):
    """NAME as a question may show it: a plain host name (letters, digits, - and _, IDNs as punycode),
    lower case; None for anything else (control characters, spaces, unicode: what could mislead)."""
    if not isinstance(name, str):
        return None
    name = name.rstrip(".").lower()
    return name if HOST_NAME.fullmatch(name) else None


def printable_text(text, limit=4000):
    """TEXT with everything but printable ASCII percent-escaped (URLs, refs and messages in questions)."""
    data = str(text)[:limit].encode("utf-8", "replace")
    return "".join(chr(b) if 0x20 <= b < 0x7f and b != 0x25 else f"%{b:02X}" for b in data)


def client_hello_sni(data):
    """("tls", name or None) for a complete TLS ClientHello at the start of DATA, ("partial", None) while
    more is needed, ("other", None) for anything else."""
    if len(data) < 5:
        return ("partial", None) if data and data[0] == 0x16 else ("other", None)
    if data[0] != 0x16 or data[1] != 3:
        return "other", None
    length = struct.unpack("!H", data[3:5])[0]
    if len(data) < 5 + length:
        return "partial", None
    try:
        body = data[5:5 + length]
        if body[0] != 1:
            return "tls", None
        pos = 4 + 2 + 32
        pos += 1 + body[pos]                                   # session id
        pos += 2 + struct.unpack("!H", body[pos:pos + 2])[0]   # cipher suites
        pos += 1 + body[pos]                                   # compression methods
        end = pos + 2 + struct.unpack("!H", body[pos:pos + 2])[0]
        pos += 2
        while pos + 4 <= end:
            kind, size = struct.unpack("!HH", body[pos:pos + 4])
            pos += 4
            if kind == 0:  # server_name: list length, type 0 (host_name), name length, name
                name_length = struct.unpack("!H", body[pos + 3:pos + 5])[0]
                return "tls", body[pos + 5:pos + 5 + name_length].decode("ascii")
            pos += size
    except (IndexError, struct.error, UnicodeDecodeError):
        pass
    return "tls", None


def first_bytes_state(data):
    """"complete" once DATA shows what a client speaks; "more" while it is only the start of a TLS
    record, of an HTTP method or of an SSH banner (a client that sends those piece by piece is waited
    for, never waved through as something else)."""
    data = LEADING_CRLF.sub(b"", data)  # servers skip empty lines before a request line (RFC 9112 2.2)
    if not data or data == b"\r":
        return "more"
    if data[0] == 0x16:
        return "more" if client_hello_sni(data)[0] == "partial" else "complete"
    if len(data) < 4 and b"SSH-".startswith(data):
        return "more"
    if re.fullmatch(rb"[A-Z]{1,10}", data):
        return "more"
    return "complete"


def original_destination(sock):
    """(address, port) the agent connected to before the redirect."""
    if sock.family == socket.AF_INET6:
        raw = sock.getsockopt(socket.IPPROTO_IPV6, 80, 28)  # IP6T_SO_ORIGINAL_DST
        return str(ipaddress.IPv6Address(raw[8:24])), struct.unpack("!H", raw[2:4])[0]
    raw = sock.getsockopt(socket.SOL_IP, 80, 16)  # SO_ORIGINAL_DST
    return socket.inet_ntoa(raw[4:8]), struct.unpack("!H", raw[2:4])[0]


# The loopback addresses at which the sandbox reaches the host's ports (pasta's forwards, the relay): only these.
# Another 127.x address is the sandbox's own, where the agent can listen itself.
HOST_LOOPBACK = ("127.0.0.1", "::1")


def hold_unserved_loopback(ports):
    """[socket]: for each host port forwarded at the start, [::1]:PORT taken here when pasta does not serve it there
    (a host without IPv6 gives pasta none). Otherwise the agent could listen at [::1]:PORT itself, and the proxy,
    which counts ::1 as the host's (HOST_LOOPBACK), would hand that port's Git login to it. Held, never listening,
    until the run ends: a connection there is refused."""
    held = []
    for port in ports:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(("::1", int(port)))
        except OSError:  # pasta serves it (EADDRINUSE), or there is no IPv6 loopback at all: nothing to hold
            sock.close()
            continue
        held.append(sock)
    return held


def allowed_destination(allow, address, port):
    """True if the firewall lets the proxy reach ADDRESS:PORT (ALLOW from the launcher's proxy.json)."""
    ip = ipaddress.ip_address(address)
    if ip.is_loopback:
        return str(ip) in HOST_LOOPBACK and port in allow.get("loopback", [])
    if allow.get("all"):
        return True
    if any(ip in ipaddress.ip_network(net, strict=False) for net in allow.get("nets", [])):
        return True
    if port == 443 and any(ip in ipaddress.ip_network(net, strict=False) for net in allow.get("https", [])):
        return True
    return any(ip == ipaddress.ip_address(a) and port == p for a, p in allow.get("tcp", []))


MODEL_PATHS = re.compile(r"/(?:v1/messages|v1/responses|v1/chat/completions|v1/completions|backend-api/codex/responses|"
                         r"responses|chat/completions)(?:\?|$)")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
              "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_tokens")


def is_model_request(path):
    """True for the paths of model APIs (Anthropic, OpenAI and compatible ones)."""
    return bool(MODEL_PATHS.search(path or ""))


def credential_headers(headers):
    """Lower-case names of the credential headers a request carried (their values are never logged)."""
    return sorted({name.lower() for name, _ in headers if SECRET_HEADER.search(name)})


class UsageScanner:
    """Model and token usage from a model API's answer, streamed (server-sent events) or not, read from all
    of it (not only the logged part); the largest value of each counter wins (streams repeat them)."""
    LIMIT = 8 << 20

    def __init__(self):
        self.model, self.usage, self.pending, self.seen = None, {}, b"", 0

    def _take(self, value):
        if isinstance(value, dict):
            if isinstance(value.get("model"), str) and not self.model:
                self.model = value["model"][:100]
            usage = value.get("usage")
            if isinstance(usage, dict):
                for key in USAGE_KEYS:
                    number = usage.get(key)
                    if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                        self.usage[key] = max(self.usage.get(key, 0), number)
                for detail in ("input_tokens_details", "output_tokens_details", "prompt_tokens_details"):
                    if isinstance(usage.get(detail), dict):
                        self._take({"usage": usage[detail]})
            for nested in value.values():
                if isinstance(nested, dict):
                    self._take(nested)

    def feed(self, data):
        self.seen += len(data)
        if self.seen > self.LIMIT:
            return
        self.pending += data
        *lines, self.pending = self.pending.split(b"\n")
        for line in lines:
            line = line.strip()
            if line.startswith(b"data:"):
                line = line[5:].strip()
            if line.startswith(b"{") and (b'"usage"' in line or b'"model"' in line[:200]):
                with contextlib.suppress(ValueError, RecursionError):
                    self._take(json.loads(line))

    def result(self):
        if self.pending.strip().startswith(b"{"):
            with contextlib.suppress(ValueError, RecursionError):
                self._take(json.loads(self.pending))
            self.pending = b""
        if not self.usage:
            return None
        return {"model": self.model, "usage": dict(self.usage)}


def redact_headers(headers):
    """[[name, value]] with the values of credential headers replaced."""
    return [[name, "[redacted]" if SECRET_HEADER.search(name) else redact_text(value)] for name, value in headers]


def redact_text(text):
    for pattern, replacement in SECRET_TEXT:
        text = pattern.sub(replacement, text)
    return text


def body_record(data, total, headers, limit):
    """The logged form of a body of TOTAL bytes whose first bytes are DATA: decompressed and redacted
    text where possible, else base64; at most LIMIT bytes."""
    if total == 0:
        return None
    record = {"size": total}
    encoding = (header(headers, "content-encoding") or "").strip().lower()
    if encoding in ("gzip", "x-gzip", "deflate"):
        try:
            data = zlib.decompressobj(47).decompress(data, limit)  # 47: gzip or zlib header, detected
        except zlib.error:
            record["encoding"] = encoding
        else:
            encoding = ""
    elif encoding and encoding != "identity":
        record["encoding"] = encoding  # br, zstd: not in the standard library
    data = data[:limit]
    record["truncated"] = total > len(data) if not encoding or encoding == "identity" else total > limit
    text = None
    for cut in range(4):  # a multi-byte character may have been cut at the end
        try:
            text = data[:len(data) - cut].decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    if text is None and "encoding" not in record and b"\x00" not in data:
        lossy = data.decode("utf-8", "replace")
        if lossy.count("\ufffd") <= max(4, len(lossy) // 50):  # text with a few broken bytes
            text, record["lossy"] = lossy, True
    if text is not None and "\x00" not in text and "encoding" not in record:
        record["text"] = redact_text(text)
    else:
        # Binary: token-shaped byte runs are redacted too (latin-1 keeps every byte as it is).
        record["base64"] = base64.b64encode(redact_text(data.decode("latin-1")).encode("latin-1", "replace")).decode()
    return record


def pkt_lines(data):
    """(lines, complete) of Git pkt-lines at the start of DATA up to the first flush packet:
    complete is False while the flush packet has not arrived."""
    lines, pos = [], 0
    while pos + 4 <= len(data):
        try:
            size = int(data[pos:pos + 4], 16)
        except ValueError:
            raise ValueError("not a Git pkt-line stream") from None
        if size == 0:
            return lines, True
        if size < 4:
            raise ValueError("unexpected Git packet")
        if pos + size > len(data):
            break
        lines.append(data[pos + 4:pos + size])
        pos += size
    return lines, False


def pkt_sections(data, count):
    """(sections, complete): the first COUNT flush-terminated lists of Git pkt-lines at the start of DATA (a
    receive-pack request: its commands, then its push options); complete once all COUNT ended."""
    sections, pos = [], 0
    while len(sections) < count:
        lines, complete = pkt_lines(data[pos:])
        if not complete:
            return sections, False
        sections.append(lines)
        pos += sum(len(line) + 4 for line in lines) + 4
    return sections, True


def push_commands(lines):
    """([(old, new, ref)], capabilities) of a receive-pack request's command lines."""
    commands, capabilities = [], set()
    for line in lines:
        if line.startswith(b"shallow "):
            continue
        line = line.rstrip(b"\n")
        if b"\0" in line:
            line, _, caps = line.partition(b"\0")
            capabilities |= set(caps.decode("ascii", "replace").split())
        fields = line.split(b" ")
        if len(fields) != 3 or not all(re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", f) for f in fields[:2]):
            raise ValueError("unrecognised Git push command")
        commands.append(tuple(f.decode("ascii", "replace") for f in fields))
    if not commands:
        raise ValueError("a Git push without commands")
    return commands, capabilities


def advertised_refs(body):
    """Commit ids a receive-pack advertisement (GET .../info/refs?service=git-receive-pack) names."""
    ids, pos = set(), 0
    while pos + 4 <= len(body):
        try:
            size = int(body[pos:pos + 4], 16)
        except ValueError:
            break
        if size < 4:
            pos += 4
            continue
        line = body[pos + 4:pos + size]
        pos += size
        oid = line.split(b" ", 1)[0]
        if re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid) and not ZERO_ID.fullmatch(oid):
            ids.add(oid.decode())
    return ids


def pkt(data):
    return b"%04x" % (len(data) + 4) + data


def push_rejection(commands, message, capabilities):
    """HTTP response refusing every ref of a push the way a Git server would, with MESSAGE as remote output."""
    report = pkt(b"unpack ok\n") + b"".join(pkt(f"ng {ref} refused by the sandbox-ai privacy check\n".encode())
                                             for _, _, ref in commands) + b"0000"
    if "side-band-64k" in capabilities or "side-band" in capabilities:
        size = 65515 if "side-band-64k" in capabilities else 995
        body = b"".join(pkt(b"\x02" + f"sandbox-ai: {line}\n".encode()[:size]) for line in message.splitlines())
        body += b"".join(pkt(b"\x01" + report[i:i + size]) for i in range(0, len(report), size)) + b"0000"
    else:
        body = report
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/x-git-receive-pack-result\r\n"
            b"Cache-Control: no-cache\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(body)) + body


def parse_head(data):
    """(start line, [[name, value]]) of an HTTP message head. Anything a server might read differently
    (bare CR or LF, folded or malformed lines) is refused, so that what the proxy checks is what arrives."""
    if re.search(rb"\r(?!\n)|(?<!\r)\n|\x00", data):
        raise ValueError("bare CR, LF or NUL in an HTTP head")
    lines = data.decode("latin-1").split("\r\n")
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise ValueError("malformed HTTP header")
        headers.append([name, value.strip()])
    return lines[0], headers


def header(headers, name):
    """The last value of header NAME, or None."""
    values = [value for key, value in headers if key.lower() == name]
    return values[-1] if values else None


def no_delay(sock):
    """TCP_NODELAY on SOCK: the proxy passes on what it has as soon as it has it (a request's head and its body come in
    separate writes; with Nagle's algorithm the second waited for the peer's delayed ACK, up to 40 ms per request)."""
    with contextlib.suppress(OSError, AttributeError):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


def send_all(sock, data):
    """sendall that also works on a non-blocking (TLS) socket. A peer that takes nothing for IDLE_TIMEOUT
    seconds (a zero window) raises socket.timeout, an OSError: a broken connection to the callers."""
    view = memoryview(data)
    deadline = time.monotonic() + IDLE_TIMEOUT
    while view:
        try:
            sent = sock.send(view)
        except (ssl.SSLWantWriteError, BlockingIOError):
            waiting = ([], [sock])
        except ssl.SSLWantReadError:
            waiting = ([sock], [])
        else:
            view = view[sent:]
            deadline = time.monotonic() + IDLE_TIMEOUT  # progress: the idle time starts again
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not any(select.select(*waiting, [], remaining)[:2]):
            raise socket.timeout(f"the peer took nothing for {IDLE_TIMEOUT} s")


def relay(left, right, counts, idle=IDLE_TIMEOUT, grace=30):
    """Copy bytes both ways until both sides closed (or one did and the other stays quiet for GRACE
    seconds); COUNTS[0] gets left->right, COUNTS[1] the rest."""
    peers = {left: (right, 0), right: (left, 1)}
    open_ = {left, right}
    for sock in peers:
        sock.setblocking(False)
    try:
        while open_:
            ready = [s for s in open_ if isinstance(s, ssl.SSLSocket) and s.pending()]
            if not ready:
                ready = select.select(list(open_), [], [], idle if len(open_) == 2 else grace)[0]
                if not ready:
                    return  # idle too long
            for sock in ready:
                other, index = peers[sock]
                try:
                    data = sock.recv(65536)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    continue
                except OSError:
                    data = b""
                if not data:
                    open_.discard(sock)
                    with contextlib.suppress(OSError):
                        # The plain socket's half-close: SSLSocket.shutdown would drop the TLS state.
                        socket.socket.shutdown(other, socket.SHUT_WR)
                    continue
                send_all(other, data)
                counts[index] += len(data)
    except OSError:
        return


class Stream:
    """Buffered reads from a (TLS) socket."""

    def __init__(self, sock, data=b""):
        self.sock, self.buffer = sock, data

    def fill(self):
        data = self.sock.recv(65536)
        if not data:
            return False
        self.buffer += data
        return True

    def take(self, size):
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def head(self):
        """The next message head (up to the blank line), or None at a clean end of stream. Empty lines
        before it are dropped, as servers ignore them."""
        dropped = 0
        while True:
            stripped = LEADING_CRLF.sub(b"", self.buffer)
            dropped += len(self.buffer) - len(stripped)
            self.buffer = stripped
            if dropped > HEAD_LIMIT:
                raise ValueError("too many empty lines")
            if self.buffer not in (b"", b"\r") or not self.fill():
                break
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > HEAD_LIMIT:
                raise ValueError("HTTP head too large")
            if not self.fill():
                if self.buffer:
                    raise ValueError("connection closed inside an HTTP head")
                return None
        return self.take(self.buffer.index(b"\r\n\r\n") + 4)

    def line(self):
        while b"\r\n" not in self.buffer:
            if len(self.buffer) > 4096 or not self.fill():
                raise ValueError("bad chunked encoding")
        return self.take(self.buffer.index(b"\r\n") + 2)

    def some(self, limit):
        if not self.buffer and not self.fill():
            return b""
        return self.take(limit)

    def body(self, framing):
        """(raw, payload) pieces of a message body: framing ("length", n), ("chunked",), ("close",) or None."""
        if framing is None:
            return
        if framing[0] == "length":
            left = framing[1]
            while left:
                data = self.some(min(left, 65536))
                if not data:
                    raise ValueError("connection closed inside a body")
                left -= len(data)
                yield data, data
        elif framing[0] == "chunked":
            while True:
                line = self.line()
                match = re.fullmatch(rb"([0-9A-Fa-f]{1,16})(;[^\r\n]*)?\r\n", line)
                if not match:
                    raise ValueError("bad chunk size")
                size = int(match[1], 16)
                if size == 0:
                    trailer = line
                    while True:
                        end = self.line()
                        trailer += end
                        if end == b"\r\n":
                            break
                        if len(trailer) > HEAD_LIMIT:
                            raise ValueError("chunk trailer too large")
                    yield trailer, b""
                    return
                yield line, b""
                left = size
                while left:
                    data = self.some(min(left, 65536))
                    if not data:
                        raise ValueError("connection closed inside a chunk")
                    left -= len(data)
                    yield data, data
                if self.line() != b"\r\n":
                    raise ValueError("bad chunk end")
                yield b"\r\n", b""
        else:
            while True:
                data = self.some(65536)
                if not data:
                    return
                yield data, data


class Capture:
    """The first LIMIT bytes of a body, and its size."""

    def __init__(self, limit):
        self.limit, self.data, self.total = limit, b"", 0

    def add(self, payload):
        self.total += len(payload)
        if len(self.data) < self.limit:
            self.data += payload[:self.limit - len(self.data)]


def request_framing(headers):
    """Body framing of a request; ambiguous framing (smuggling) is refused."""
    encodings = [value for name, value in headers if name.lower() == "transfer-encoding"]
    lengths = {value.strip() for name, value in headers if name.lower() == "content-length"}
    if encodings:
        if lengths or [value.strip().lower() for value in encodings] != ["chunked"]:
            raise ValueError("ambiguous request framing (Transfer-Encoding)")
        return ("chunked",)
    if lengths:
        if len(lengths) > 1 or not next(iter(lengths)).isdigit():
            raise ValueError("bad Content-Length")
        length = int(next(iter(lengths)))
        return ("length", length) if length else None
    return None


def response_framing(method, status, headers):
    if method == "HEAD" or 100 <= status < 200 or status in (204, 304):
        return None
    if "chunked" in (header(headers, "transfer-encoding") or "").lower():
        return ("chunked",)
    length = header(headers, "content-length")
    if length is not None and length.isdigit():
        return ("length", int(length)) if int(length) else None
    return ("close",)


def ssh_banner(data):
    """DATA (a side's first bytes) carries an SSH banner: a line starting with "SSH-" in its first 255 bytes. Servers
    skip lines before the client's banner (RFC 4253 allows them before the server's), so one junk line first is no
    way past a refusal of SSH."""
    return any(line.lstrip(b"\r").startswith(b"SSH-") for line in (data or b"")[:255].split(b"\n"))


def host_matches(host, patterns):
    """True if HOST is one of PATTERNS (exact names, or *.domain for every name below domain)."""
    host = (host or "").lower().rstrip(".")
    return any(host == p or (p.startswith("*.") and host.endswith(p[1:])) for p in patterns)


REQUEST_LINE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,20} [!-~]{1,8000} HTTP/1\.[01]")


def normal_path(target):
    """The path of a request TARGET as servers resolve it: decoded, without query and fragment, repeated slashes
    and dot segments resolved (/repos/x/../../gists is /gists), no trailing slash. Rules and the push check go by
    this, so an encoding or a .. is no way past them."""
    path = target.split("?")[0].split("#")[0]
    for _ in range(4):  # decoded until stable: a server that decodes twice reads %252e%252e as ..
        decoded = urllib.parse.unquote(path)
        if decoded == path:
            break
        path = decoded
    path = path.replace("\\", "/")
    # ;parameters of a segment are dropped, as servers such as Tomcat and GitHub do: /x/..;/y is /y.
    path = "/".join(segment.split(";", 1)[0] for segment in path.split("/"))
    path = posixpath.normpath("/" + re.sub(r"/+", "/", path).lstrip("/"))
    return "" if path == "/" else path.rstrip("/")


def ambiguous_path(target):
    """The raw path of request TARGET reads differently by server: decoded (as normal_path decodes it, %2F and a
    backslash as slashes) it has a dot segment, so where it leads depends on whether a server resolves it before or
    after decoding, or at all. normal_path takes the strictest reading, which is right for what refuses; what
    grants (an --llm key below its path prefix, an allow rule for some paths, the push check's own path test) must
    not go by it: a server that routes on the raw path (/tenantB/x%2F..%2F..%2FtenantA) serves another place than
    the one granted. An encoded slash alone (GitLab's group%2Fproject) changes no place."""
    path = target.split("?")[0].split("#")[0]
    for _ in range(4):
        decoded = urllib.parse.unquote(path)
        if decoded == path:
            break
        path = decoded
    return any(segment.split(";", 1)[0].strip() in (".", "..") for segment in path.replace("\\", "/").split("/"))


def path_matches(pattern, path, ignore_case=False):
    """PATH matches PATTERN: * stands for any characters within one segment, ** for any, slashes included; a
    /** at the end or between slashes also matches no segment at all (/gists/** covers POST /gists)."""
    expression = "".join("(?:/.*)?" if part == "/**" else ".*" if part == "**" else "[^/]*" if part == "*"
                         else re.escape(part) for part in re.split(r"(/\*\*(?=/|$)|\*\*|\*)", pattern))
    return re.fullmatch(expression, path or "/", re.S | (re.I if ignore_case else 0)) is not None


def is_address_origin(origin):
    host = origin.rsplit(":", 1)[0].strip("[]") if ":" in origin else origin
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def host_name(value):
    """The host of a Host header, lower case and without port, or None."""
    if not value:
        return None
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else None
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


DEFAULT_PORTS = {"http": 80, "https": 443}
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
HOST_HEADER = re.compile(r"(?:\[([0-9A-Fa-f:.]{2,45})\]|([^\s\[\]:/@?#\\]{1,253}))(?::(\d{1,5}))?")


def host_authority(value):
    """(name, port) a Host header VALUE names: the name in lower case without a trailing dot (an address in its
    usual form), the port as given (None when it names none); None when it is malformed."""
    match = HOST_HEADER.fullmatch((value or "").strip())
    if not match:
        return None
    name = (match.group(1) or match.group(2)).lower().rstrip(".")
    with contextlib.suppress(ValueError):
        name = str(ipaddress.ip_address(name))
    if match.group(1) and ":" not in name:
        return None  # [ ] holds IPv6 addresses only
    port = int(match.group(3)) if match.group(3) else None
    if port is not None and not 0 < port < 65536:
        return None
    return name, port


def host_agrees(value, names, port):
    """The Host header VALUE names one of NAMES (see host_authority), on PORT when it names a port: clients leave
    out only the scheme's default port, and a name without one still names that server, so only the name decides
    which tenant a front end serves."""
    found = host_authority(value)
    return found is not None and found[0] in names and found[1] in (None, port)


def verified_authority(scheme, origin, record, host_header):
    """(authority, claim) of a request on a connection to ORIGIN (the name the proxy verified, else address:port):
    the authority as a URL names where the request really goes, and the agent's Host header HOST_HEADER, printable,
    when it names anything else (None when it agrees; then its own form is the authority, as the client wrote it).
    The claim is what a server routing by Host may act on; only the authority is checked."""
    port = str(record.get("destination", "")).rsplit(":", 1)[-1]
    port = int(port) if port.isdigit() else DEFAULT_PORTS.get(scheme)
    if is_address_origin(origin):
        address = origin.rsplit(":", 1)[0].strip("[]") if origin.count(":") == 1 or origin.startswith("[") else origin
        name = str(ipaddress.ip_address(address))
    else:
        name = origin.lower().rstrip(".")
    if host_header is not None and host_agrees(host_header, {name}, port):
        return printable_text(host_header.strip(), 300), None
    shown = f"[{name}]" if ":" in name else name
    authority = shown if port == DEFAULT_PORTS.get(scheme) else f"{shown}:{port}"
    return authority, (printable_text(host_header, 300) if host_header is not None else None)


def host_header_problem(headers, scheme, port, names, shown=None):
    """Why the request's Host header does not name the server a credential the proxy adds is bound to (one of
    NAMES, on PORT), or None. Front ends route by the Host header and one certificate and address serve many
    tenants, so a key bound to a.example must not go with "Host: b.example" (domain fronting). Absolute-form
    targets, HTTP/2 (:authority) and a second Host header are refused before this; the comparison ignores case
    and a trailing dot (host_agrees)."""
    values = [value for name, value in headers if name.lower() == "host"]
    if len(values) != 1:
        return "the request names no single Host"
    shown = shown or (sorted(names) or ["?"])[0]
    if host_authority(values[0]) is None:
        return f"the Host header {printable_text(values[0], 300)!r} is malformed"
    if host_agrees(values[0], names, port):
        return None
    return f"the Host header {printable_text(values[0], 300)} differs from {shown}" \
        + ("" if port == DEFAULT_PORTS.get(scheme) else f":{port}") + ", the server the credential is for"


LLM_KEY_HEADERS = ("authorization", "x-api-key", "api-key")


def llm_head(entries, scheme, origin, port, path, head, target=None):
    """--llm: (HEAD, endpoint name) with the endpoint's key where the agent sent the placeholder (or as a Bearer token
    where it sent no credentials) and its header secrets in place of the agent's own; (HEAD, None) unchanged for
    every other request. Only over the endpoint's scheme and port, to its verified name (HTTPS) or one of its
    pinned addresses (plain http), below its path prefix."""
    for entry in (entries or []) if head.endswith(b"\r\n\r\n") else []:
        if scheme != entry["scheme"] or port != entry["port"]:
            continue
        if scheme == "https" and origin.lower() != entry["host"]:
            continue
        if scheme == "http" and origin.rsplit(":", 1)[0].strip("[]") not in entry["addresses"]:
            continue
        prefix = entry.get("prefix", "")
        if prefix and (path != prefix and not path.startswith(prefix + "/") or ambiguous_path(target or path)):
            continue  # below the prefix only as every server reads the path (ambiguous_path)
        lines = head[:-4].split(b"\r\n")
        own = {name.lower().encode() for name, _ in entry["headers"]}
        placeholder, key = entry["placeholder"].encode(), (entry.get("key") or "").encode()
        kept, credentials = [lines[0]], False
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            name = name.lower()
            if name in own:
                continue
            if name.decode("latin-1") in LLM_KEY_HEADERS:
                if placeholder in value:
                    if not key:
                        continue  # no key for this server: the placeholder goes nowhere
                    line = line[:len(name)] + b":" + value.replace(placeholder, key)
                credentials = True
            kept.append(line)
        if key and not credentials:
            kept.append(b"Authorization: Bearer " + key)
        kept += [f"{name}: {value}".encode("latin-1") for name, value in entry["headers"]]
        return b"\r\n".join(kept) + b"\r\n\r\n", entry["name"]
    return head, None


API_KEY_HEADERS = LLM_KEY_HEADERS + ("private-token",)  # GitLab's own


def basic_with_key(value, entries):
    """VALUE of an Authorization header ("Basic B64"), with each entry's key in place of its placeholder inside the
    decoded user:password (PyPI's __token__:TOKEN), re-encoded; (VALUE, []) when it holds none."""
    scheme, _, encoded = value.strip().partition(b" ")
    if scheme.lower() != b"basic":
        return value, []
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True)
    except (ValueError, binascii.Error):
        return value, []
    added = []
    for entry in entries:
        placeholder = entry["placeholder"].encode()
        if placeholder in decoded:
            decoded = decoded.replace(placeholder, entry["key"].encode())
            added.append(entry["name"])
    return (b" Basic " + base64.b64encode(decoded), added) if added else (value, [])


ADDED_API_KEYS = "api-keys-added.json"  # in PROXY_RUN: --api-key given during the run (sandbox-ai net allow)
API_KEYS_LIMIT = 64
_added_api_keys = {"stamp": None, "entries": []}


def checked_api_keys(entries):
    """The well-formed ones of ENTRIES ([{host, port, name, placeholder, key}], as `sandbox-ai net allow --api-key`
    sends them): host in lower case, a key usable in a header, the placeholder of sandbox-ai's form."""
    found = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        host, name, placeholder, key = (entry.get(k) for k in ("host", "name", "placeholder", "key"))
        if not all(isinstance(v, str) and v and len(v) <= 4096 for v in (host, name, placeholder, key)) or \
                not isinstance(entry.get("port"), int) or isinstance(entry.get("port"), bool) or \
                any(ord(c) < 33 or ord(c) == 127 for c in key) or \
                placeholder != f"sandbox-ai-api-key-placeholder-{name}":
            continue
        found.append({"host": host.lower(), "port": entry["port"], "name": name, "placeholder": placeholder,
                      "key": key})
    return found[:API_KEYS_LIMIT]


def give_api_keys(entries, path=None, owner=None):
    """Control op "api-keys": ENTRIES (every key given during the run, the whole list) for the proxy, in PATH (its
    run directory, which only it and the supervisor reach), 0600 and owned by the proxy's user (OWNER, by default
    the directory's owner): the proxy runs as that user and reads it again when it changes. Returns the names."""
    path = Path(path) if path else PROXY_RUN / ADDED_API_KEYS
    if not path.parent.is_dir():
        raise ValueError("this sandbox runs without the connection proxy: it takes no API key")
    if not isinstance(entries, list) or len(entries) > API_KEYS_LIMIT:
        raise ValueError(f"api-keys: a list of at most {API_KEYS_LIMIT} keys")
    checked = checked_api_keys(entries)
    if len(checked) != len(entries):
        raise ValueError("api-keys: every key has a host, port, name, its placeholder and a value usable in a header")
    if owner is None:  # the run directory's owner: the proxy's user, to whom the supervisor gave it at the start
        info = path.parent.stat()
        owner = (info.st_uid, info.st_gid)
    write_new(path, json.dumps({"version": 1, "api_keys": checked}), 0o600, owner=owner)
    return [entry["name"] for entry in checked]


def added_api_keys(path=None):
    """The --api-key entries given while the run goes on (give_api_keys wrote them into the proxy's run directory):
    [{host, port, name, placeholder, key}], read again whenever the file changes; [] without one. An entry that is
    not well-formed is left out."""
    path = Path(path) if path else PROXY_RUN / ADDED_API_KEYS
    try:
        info = path.stat()
    except OSError:
        _added_api_keys.update(stamp=None, entries=[])
        return []
    stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
    if stamp == _added_api_keys["stamp"]:
        return _added_api_keys["entries"]
    try:
        document = json.loads(path.read_text()[:1 << 20])
    except (OSError, ValueError):
        document = {}
    entries = checked_api_keys(document.get("api_keys") if isinstance(document, dict) else None)
    _added_api_keys.update(stamp=stamp, entries=entries)
    return entries


def api_key_head(entries, scheme, origin, port, head):
    """--api-key and --publish: (HEAD, [names]) with each key in place of its placeholder, in a credential header of
    an HTTPS request to the key's verified host and port only (inside a Basic login for an entry marked "basic");
    other headers, the body and every other request keep the placeholder."""
    added = []
    if scheme != "https" or not head.endswith(b"\r\n\r\n"):
        return head, added
    mine = [entry for entry in entries or [] if entry["port"] == port and origin.lower() == entry["host"]]
    if not mine:
        return head, added
    lines = head[:-4].split(b"\r\n")
    for index, line in enumerate(lines[1:], 1):
        name, sep, value = line.partition(b":")
        if not sep or name.strip().lower().decode("latin-1") not in API_KEY_HEADERS:
            continue
        basic = [entry for entry in mine if entry.get("basic")]
        if basic and name.strip().lower() == b"authorization":
            value, inside = basic_with_key(value, basic)
            added += inside
        for entry in mine:
            placeholder = entry["placeholder"].encode()
            if placeholder in value:
                value = value.replace(placeholder, entry["key"].encode())
                added.append(entry["name"])
        lines[index] = name + b":" + value
    return b"\r\n".join(lines) + b"\r\n\r\n", added


class Scrubber:
    """Masks SECRETS (bytes) in a stream of pieces, with stars of the same length, so that lengths and chunk sizes
    stay right: keeps back the last bytes that could begin one until the next piece or the end."""

    def __init__(self, secrets):
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self.keep = max((len(s) for s in self.secrets), default=1) - 1
        self.pending, self.masked = b"", 0

    def mask(self, data):
        for secret in self.secrets:
            if secret in data:
                self.masked += data.count(secret)
                data = data.replace(secret, b"*" * len(secret))
        return data

    def feed(self, data):
        data = self.mask(self.pending + data)
        if self.keep and len(data) > self.keep:
            data, self.pending = data[:-self.keep], data[-self.keep:]
        elif self.keep:
            data, self.pending = b"", data
        return data

    def flush(self):
        data, self.pending = self.mask(self.pending), b""
        return data


def scrubbed_pieces(pieces, scrubber, chunked):
    """The raw pieces of an answer's body with SCRUBBER applied to the payload. Chunked: newly framed (what is kept
    back must not cross a chunk's end); otherwise the bytes keep their places, so a length stays right."""
    for raw, payload in pieces:
        if chunked:
            if payload:
                data = scrubber.feed(payload)
                if data:
                    yield b"%x\r\n" % len(data) + data + b"\r\n", payload
            elif re.match(rb"0+(?:;[^\r\n]*)?\r\n", raw):  # the last chunk and its trailer: what was kept back first
                rest = scrubber.flush()
                if rest:
                    yield b"%x\r\n" % len(rest) + rest + b"\r\n", b""
                yield scrubber.mask(raw), b""
            continue
        data = scrubber.feed(raw)
        if data:
            yield data, payload
    if not chunked:
        rest = scrubber.flush()
        if rest:
            yield rest, b""


def identity_head(head):
    """HEAD asking for an uncompressed answer: one the proxy added a credential to is scrubbed of it on the way
    back, which needs its bytes as they are."""
    head = re.sub(rb"(?im)^accept-encoding:[^\r\n]*\r\n", b"", head)
    return head[:-2] + b"Accept-Encoding: identity\r\n\r\n"


PROXY_CONTEXTS = 256    # TLS contexts the proxy keeps (one per host name it opens)
PROXY_VERIFIED = 4096   # (address, port, name) answers of serves_name it keeps


class ProxyServer:
    """Connections of one sandbox run; one thread per connection."""

    def __init__(self, config, log_fd, checker=None, run_dir=PROXY_RUN, destination=original_destination,
                 upstream=None, asker=None, who=None):
        self.config, self.log_fd, self.checker, self.run_dir = config, log_fd, checker, run_dir
        # ASKER puts questions to the supervisor; WHO (the same channel, also with --ask off) asks it which
        # process a connection belongs to. DEBUG (--log-debug) logs more of what a refused client sent.
        self.asker, self.who, self.debug = asker, who, bool(config.get("debug"))
        self.destination, self.lock, self.check_lock = destination, threading.Lock(), threading.Lock()
        # The checker's answers come in the order of the requests; the late ones to checks that took too long
        # (CHECKER_OWED of them) are read and dropped before the next request (under check_lock).
        self.checker_buffer, self.checker_owed = b"", 0
        self.counter, self.contexts, self.adverts, self.verified = 0, {}, {}, {}
        self.allow_file = Path(config.get("allow_file", "")) if config.get("allow_file") else None
        self.allow_mtime = None
        system = run_dir / "system.pem"
        self.upstream_cafile = str(system) if system.is_file() and system.stat().st_size else None
        self.upstream = upstream or ssl.create_default_context(cafile=self.upstream_cafile)
        self.upstream.set_alpn_protocols(["http/1.1"])
        # The launcher's rulesets, then --allow-http as one more (an allow-list per host); a rule that does not
        # compile stops the proxy, and with it the sandbox.
        sources, pinned = list(config.get("rules", [])), dict(config.get("pinned", {}))
        legacy = config.get("http_rules", [])
        if legacy:
            sources.append({"name": "--allow-http", "version": 1, "http": [
                {"id": f"allow-http-{index}", "host": rule["host"], "methods": rule["methods"], "path": rule["path"],
                 "decision": "allow"} for index, rule in enumerate(legacy, 1)]})
            for rule in legacy:
                pinned[rule["host"]] = [*pinned.get(rule["host"], []), *rule.get("addresses", [])]
        self.rules = Ruleset(sources, pinned, config.get("providers", []))
        # `sandbox-ai rules reload`: the supervisor writes the run's rulesets anew (rules_file, re-read on change);
        # --allow-http, the addresses and the providers stay as they were at the start.
        self.rules_file = Path(config["rules_file"]) if config.get("rules_file") else None
        self.rules_extra, self.rules_pinned = sources[len(config.get("rules", [])):], pinned
        self.rules_providers, self.rules_mtime, self.rules_checked = config.get("providers", []), None, 0.0
        # The run's taint, as the supervisor keeps it (taint_file, re-read on change); without a supervisor (tests)
        # the proxy keeps its own.
        self.taint_file = Path(config["taint_file"]) if config.get("taint_file") else None
        self.taint_mtime, self.taint_state, self.local_taint = None, {"tainted": False}, None

    # -- bookkeeping --

    def log(self, record):
        record = self.resolve({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record})
        line = json.dumps(record, ensure_ascii=False)
        for value in self.own_secrets():
            line = line.replace(value, "[redacted]")
        line = (line + "\n").encode()
        with self.lock:
            os.write(self.log_fd, line)

    def own_secrets(self):
        """The credentials the proxy adds itself (--llm keys and header secrets, --token-proxy tokens), longest first,
        also as JSON escapes them: never logged, not even when a server echoes one back."""
        if getattr(self, "_own_secrets", None) is None:
            entries = self.config.get("llm") or []
            values = [entry.get("key") for entry in entries] + [value for entry in entries
                                                                for _, value in entry.get("headers", [])]
            values += [entry.get("token") for entry in self.config.get("git_tokens") or []]
            values += [entry.get("key") for entry in self.config.get("api_keys") or []]
            found = {form for value in values if isinstance(value, str) and len(value) >= 4
                     for form in (value, json.dumps(value, ensure_ascii=False)[1:-1])}
            self._own_secrets = sorted(found, key=len, reverse=True)
        added = added_api_keys()
        if not added:
            return self._own_secrets
        # Given during the run: merged again only when the file changed (added_api_keys keeps its list until then).
        cached = getattr(self, "_own_with_added", None)
        if cached is None or cached[0] is not added:
            extra = {form for entry in added for form in (entry["key"], json.dumps(entry["key"], ensure_ascii=False)[1:-1])
                     if len(entry["key"]) >= 4}
            self._own_with_added = cached = (added, sorted(set(self._own_secrets) | extra, key=len, reverse=True))
        return cached[1]

    def attribute(self, client):
        """The supervisor's lookup of the process behind CLIENT (Attribution), started now and read when a
        record is written; None without a channel."""
        if self.who is None:
            return None
        with contextlib.suppress(OSError):
            address, port = client.getpeername()[:2]
            return self.who.request("who", {"peer": [address, port]})
        return None

    @staticmethod
    def resolve(record, timeout=None):
        """RECORD with its pending lookup replaced, in place, by the answer: "process" (or {"unknown": why})
        and "sandbox". A late answer is unknown for this record, never a delay of the connection."""
        pending = record.get("process")
        if isinstance(pending, Pending):
            answer = pending.result(ATTRIBUTION_WAIT if timeout is None else timeout) or {}
            record["process"] = answer["process"] if isinstance(answer.get("process"), dict) else \
                {"unknown": "no answer in time"}
            if isinstance(answer.get("sandbox"), dict):
                record["sandbox"] = answer["sandbox"]
        return record

    def asked_by(self, record):
        """{"process", "sandbox"} of RECORD's connection, for a question: the human sees who asks."""
        self.resolve(record)
        return {key: record[key] for key in ("process", "sandbox") if key in record}

    def refuse(self, client, entry, reset=False):
        """Close CLIENT (with a reset: no lingering half-open connection), then log ENTRY. The lookup gets a few
        milliseconds first (the kernel forgets the client's socket once it is reset), never more."""
        self.resolve(entry, REFUSAL_WAIT)
        with contextlib.suppress(OSError):
            if reset:
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
        self.log(entry)

    @staticmethod
    def client_hello(data):
        """What a TLS ClientHello at the start of DATA claims, for the log: {"sni", "alpn", "version",
        "versions", "ciphers", "extensions"}; None for anything else. Never used for a decision."""
        try:
            if len(data) < 5 or data[0] != 0x16:
                return None
            body = data[5:5 + struct.unpack("!H", data[3:5])[0]]
            if len(body) < 40 or body[0] != 1:
                return None
            result = {"sni": None, "alpn": [], "version": "0x" + body[4:6].hex(), "versions": [], "ciphers": 0,
                      "extensions": []}
            pos = 38 + 1 + body[38]                                   # random, session id
            size = struct.unpack("!H", body[pos:pos + 2])[0]
            result["ciphers"], pos = size // 2, pos + 2 + size
            pos += 1 + body[pos]                                      # compression methods
            end = min(len(body), pos + 2 + struct.unpack("!H", body[pos:pos + 2])[0])
            pos += 2
            while pos + 4 <= end and len(result["extensions"]) < 64:
                kind, size = struct.unpack("!HH", body[pos:pos + 4])
                value, pos = body[pos + 4:pos + 4 + size], pos + 4 + size
                result["extensions"].append(kind)
                if kind == 0 and len(value) > 5:
                    result["sni"] = printable_text(value[5:5 + struct.unpack("!H", value[3:5])[0]].decode("latin-1"), 253)
                elif kind == 16 and len(value) > 2:
                    index = 2
                    while index < len(value) and len(result["alpn"]) < 10:
                        result["alpn"].append(printable_text(value[index + 1:index + 1 + value[index]].decode("latin-1"), 50))
                        index += 1 + value[index]
                elif kind == 43 and value:
                    result["versions"] = ["0x" + value[1 + i:3 + i].hex() for i in range(0, min(value[0], 32), 2)]
            return result
        except (IndexError, struct.error):
            return None

    def origin(self, first):
        """What the first bytes of a refused connection tell, for its record: the server name (SNI) and ALPN
        of a ClientHello, or the request line and Host of plain HTTP; the agent's claims, escaped. In debug
        mode also the ClientHello's version, cipher count and extensions."""
        hello = self.client_hello(first or b"")
        if hello:
            details = {"sni": hello["sni"], "alpn": hello["alpn"]} if hello["sni"] else {"alpn": hello["alpn"]}
            if self.debug:
                details["client_hello"] = {key: hello[key] for key in ("version", "versions", "ciphers", "extensions")}
            return details
        if first and self.http_host(first):
            line = LEADING_CRLF.sub(b"", first).split(b"\r\n", 1)[0].decode("latin-1")
            return {"http_host": printable_text(self.http_host(first), 253),
                    "request_line": printable_text(redact_text(line), 500)}
        return {}

    def taint(self):
        """The run's taint state if it is tainted, else None."""
        if self.taint_file is not None:
            try:
                mtime = self.taint_file.stat().st_mtime_ns
                if mtime != self.taint_mtime:
                    self.taint_state, self.taint_mtime = json.loads(self.taint_file.read_text()), mtime
                    if self.local_taint and mtime != self.local_taint.get("_mirror"):
                        self.local_taint = None  # the supervisor's record is newer: it decides (untaint included)
            except FileNotFoundError:
                self.taint_state = {"tainted": False}
            except (OSError, ValueError):
                pass
            state = self.taint_state if isinstance(self.taint_state, dict) else {"tainted": False}
            if self.local_taint and not state.get("tainted"):
                # The supervisor did not answer in time when this proxy saw untrusted content: tainted all the same
                # (the mirror catches up once the supervisor records it).
                state = self.local_taint
        else:
            state = self.local_taint or {"tainted": False}
        return state if state.get("tainted") else None

    def observe(self, facts, any_path=False):
        """Untrusted content arrives (FACTS: of the request whose response comes, or of a connection whose
        content is not seen): the run is tainted before the agent reads it. The supervisor records it and
        answers before the proxy goes on, so the agent's next request already meets the rules for tainted runs."""
        source = self.rules.untrusted_http(facts, any_path)
        if source is None or self.taint() is not None:
            return
        if self.who is not None:
            answer = self.who.request("taint", {"source": source}).result(2.0) or {}
            if isinstance(answer.get("taint"), dict):
                self.taint_state, self.taint_mtime = answer["taint"], None
                return
        self.local_taint = {"tainted": True, "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "source": source, "sources": 1, "_mirror": self.taint_mtime}

    @property
    def rules(self):
        """The run's rulesets, re-read (at most every RULES_CHECK_SECONDS) when `sandbox-ai rules reload` had the
        supervisor write them anew; a file that does not compile leaves the rules as they were."""
        path = getattr(self, "rules_file", None)
        if path is not None and time.monotonic() - self.rules_checked >= RULES_CHECK_SECONDS:
            self.rules_checked = time.monotonic()
            try:
                mtime = path.stat().st_mtime_ns
                if mtime != self.rules_mtime:
                    written = json.loads(path.read_text())
                    sources = written.get("rulesets", []) if isinstance(written, dict) else written
                    pinned = dict(self.rules_pinned)
                    if isinstance(written, dict) and isinstance(written.get("pinned"), dict):
                        for name, addresses in written["pinned"].items():  # names allowed during the run
                            pinned.setdefault(name, addresses)
                    self._rules = Ruleset([*sources, *self.rules_extra], pinned, self.rules_providers)
                    self.rules_mtime = mtime
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return self._rules

    @rules.setter
    def rules(self, value):
        self._rules = value

    def allow(self):
        """The allow-list, re-read when `sandbox-ai net` changed it during the run."""
        if self.allow_file:
            try:
                mtime = self.allow_file.stat().st_mtime_ns
                if mtime != self.allow_mtime:
                    self.config["allow"], self.allow_mtime = json.loads(self.allow_file.read_text()), mtime
            except (OSError, ValueError):
                pass
        return self.config.get("allow", {})

    def server_context(self, host):
        """The TLS context for HOST, kept for the PROXY_CONTEXTS hosts used last (each holds a certificate in
        OpenSSL's memory; names under an allowed wildcard are endless)."""
        with self.lock:
            context = self.contexts.pop(host, None)
            if context is not None:
                self.contexts[host] = context  # the most recently used last
        if context is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.set_alpn_protocols(["http/1.1"])
            context.load_cert_chain(str(server_certificate(self.run_dir, host)), str(self.run_dir / "leaf.key"))
            with self.lock:
                self.contexts[host] = context
                while len(self.contexts) > PROXY_CONTEXTS:
                    self.contexts.pop(next(iter(self.contexts)))
        return context

    def ssh_refused(self, address, port=None):
        """Why SSH to ADDRESS:PORT is refused, or None: a push over SSH would bypass the privacy check."""
        ssh = self.config.get("ssh", {})
        if not ssh.get("block"):
            return None
        ip = ipaddress.ip_address(address)
        if str(ip) in ssh.get("public_git", []):
            return "SSH to a public Git host would bypass the privacy check of pushes; use HTTPS"
        allow = self.allow()
        public = [ipaddress.ip_address(addr) for addr in ssh.get("public_git", [])]
        allowed = [ipaddress.ip_network(net, strict=False) for net in ssh.get("allowed", []) + allow.get("ssh", [])]
        # A range holding a public Git host (--allow 140.82.112.0/20) gives no SSH: the host has other addresses.
        if any(ip in net and any(addr in net for addr in public) for net in allowed):
            return "SSH into a range with a public Git host would bypass the privacy check of pushes; use HTTPS"
        if any(ip in net for net in allowed):
            return None
        # --allow-tcp IP:PORT (at the start or with `sandbox-ai net allow`): that port only.
        if any(ipaddress.ip_address(addr) == ip and (port is None or int(allowed_port) == port)
               for addr, allowed_port in ssh.get("allowed_tcp", []) + allow.get("ssh_tcp", [])):
            return None
        return ("SSH reaches only destinations you allowed with --allow or --allow-tcp IP:PORT, because a push "
                "over SSH would bypass the privacy check; use HTTPS")

    # -- connections --

    def wait(self):
        """Block for good: the proxy ends with the sandbox."""
        threading.Event().wait()

    def serve(self, listeners):
        for listener in listeners:
            threading.Thread(target=self.accept_loop, args=(listener,), daemon=True).start()

    def accept_loop(self, listener):
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                time.sleep(0.1)  # e.g. out of file descriptors: no busy loop
                continue
            try:
                threading.Thread(target=self.handle, args=(client,), daemon=True).start()
            except RuntimeError:  # no thread to be had (the sandbox's process limit): this connection only
                client.close()
                time.sleep(0.1)

    def handle(self, client):
        with self.lock:
            self.counter += 1
            number = self.counter
        started = time.monotonic()
        record = {"connection": number}
        upstream = None
        no_delay(client)
        try:
            address, port = self.destination(client)
            record["destination"] = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
            with contextlib.suppress(OSError):
                source = client.getpeername()
                record["source"] = f"[{source[0]}]:{source[1]}" if ":" in source[0] else f"{source[0]}:{source[1]}"
            pending = self.attribute(client)  # looked up meanwhile; read when the first record is written
            if pending is not None:
                record["process"] = pending
            first = None
            if not allowed_destination(self.allow(), address, port):
                answer = None
                # What the agent wants, for the question and the record: the server name it sends, if any. Without
                # a question, only briefly: a refusal is not held up for the log.
                first = self.peek(client) if self.asker is not None else self.peek(client, wait=0.2, complete=1.0)
                if self.asker is not None:
                    # The name is what the agent sends: shown only if it is a plain host name, and never
                    # verified (the human approves the address, which is what the firewall opens).
                    name = question_name(client_hello_sni(first)[1] or self.http_host(first))
                    protocol = ("ssh" if first.startswith(b"SSH-") else "tls" if first[:1] == b"\x16"
                                else "http" if self.http_host(first) else "tcp")
                    question = {"destination": record["destination"], "address": address, "port": port,
                                "protocol": protocol, **self.asked_by(record)}
                    answer = self.asker("connect", {**question, **({"host": name} if name else {})})
                    record["approval"] = {k: answer.get(k) for k in ("decision", "scope", "reason")}
                if not answer or answer.get("decision") != "allow" or not allowed_destination(self.allow(), address, port):
                    self.refuse(client, {**record, **self.origin(first), "event": "denied",
                                         "reason": "not allowed by the network options"
                                         if not answer else f"not approved ({answer.get('reason', 'refused')})"},
                                reset=True)
                    return
            first = self.peek(client) if first is None else first
            hello = self.client_hello(first)
            if hello and hello["alpn"]:
                record["alpn"] = hello["alpn"]
            if hello and self.debug:
                record["client_hello"] = {key: hello[key] for key in ("version", "versions", "ciphers", "extensions")}
            if ssh_banner(first) and self.ssh_refused(address, port):
                self.refuse(client, {**record, "event": "denied", "protocol": "ssh",
                                     "reason": self.ssh_refused(address, port)})
                return
            if not self.passthrough_allowed(first, address, port, record):
                return
            try:
                upstream = socket.create_connection((address, port), timeout=30)
                no_delay(upstream)
            except OSError as exc:
                self.log({**record, "event": "error", "error": f"connect: {exc}"})
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
            server_first = b""
            if not first:
                # Nothing yet: either the server speaks first (SMTP, SSH, databases), or the client waits on
                # purpose. Whoever speaks first decides; a late client is classified like a prompt one.
                ready = select.select([client, upstream], [], [], IDLE_TIMEOUT)[0]
                if client in ready:
                    first = self.peek(client, wait=PEEK_TIMEOUT)
                    if not self.passthrough_allowed(first, address, port, record):
                        return
                elif upstream in ready:
                    upstream.settimeout(PEEK_TIMEOUT)
                    with contextlib.suppress(socket.timeout, OSError):
                        server_first = upstream.recv(256, socket.MSG_PEEK)
                else:
                    return
            spoken = LEADING_CRLF.sub(b"", first)
            if first and not server_first and first[:1] != b"\x16" and not re.match(rb"[A-Z]{3,10} \S", spoken) \
                    and self.ssh_refused(address, port) and select.select([upstream], [], [], PEEK_TIMEOUT)[0]:
                # Where SSH is refused, the server's first bytes count too: an SSH server sends its banner at once,
                # whatever the client sent first (a junk line before its own banner, which some servers skip).
                upstream.settimeout(PEEK_TIMEOUT)
                with contextlib.suppress(socket.timeout, OSError):
                    server_first = upstream.recv(256, socket.MSG_PEEK)
            if ssh_banner(first) or ssh_banner(server_first):
                reason = self.ssh_refused(address, port)
                if reason:
                    self.log({**record, "event": "denied", "protocol": "ssh", "reason": reason})
                    return
            kind, name = client_hello_sni(first)
            if kind == "tls" and canary_sni(self, client, name, record):
                return
            upstream.settimeout(IDLE_TIMEOUT)
            client.settimeout(IDLE_TIMEOUT)
            if first and first_bytes_state(first) == "more":
                self.log({**record, "event": "denied", "reason": "an incomplete TLS ClientHello, HTTP request "
                          "line or SSH banner"})
                return
            if kind == "tls":
                record["host"] = name
                if host_matches(name, self.config.get("no_inspect", [])):
                    self.observe(Ruleset.request(name or f"{address}:{port}", "GET", "/", [], address), any_path=True)
                    counts = [0, 0]
                    relay(client, upstream, counts)
                    self.log({**record, "event": "tls-passthrough", "bytes_out": counts[0], "bytes_in": counts[1],
                              "ms": int((time.monotonic() - started) * 1000)})
                    return
                self.intercept(client, upstream, name, address, record, started, port)
            elif re.match(rb"[A-Z]{3,10} \S", LEADING_CRLF.sub(b"", first)) and \
                    not LEADING_CRLF.sub(b"", first).startswith(b"PRI * HTTP/2"):
                origin = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
                self.exchange(Stream(client), client, upstream, "http", origin, record)
            elif LEADING_CRLF.sub(b"", first).startswith(b"PRI * HTTP/2"):
                self.log({**record, "event": "denied", "reason": "HTTP/2 without TLS is not supported"})
            elif first and (port in HTTP_PORTS or self.has_http_rules(address)):
                self.log({**record, "event": "denied", "reason": f"neither HTTP nor TLS on port {port}"})
            else:
                self.observe(Ruleset.request(f"[{address}]:{port}" if ":" in address else f"{address}:{port}", "GET",
                                             "/", [], address), any_path=True)
                counts = [0, 0]
                relay(client, upstream, counts)
                self.log({**record, "event": "tcp",
                          "protocol": "ssh" if ssh_banner(first) or ssh_banner(server_first) else None,
                          "bytes_out": counts[0], "bytes_in": counts[1], "ms": int((time.monotonic() - started) * 1000)})
        except Exception as exc:  # noqa: BLE001 - one broken connection must not stop the proxy
            self.log({**record, "event": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    with contextlib.suppress(OSError):
                        sock.close()

    def passthrough_allowed(self, first, address, port, record):
        """False (logged) for a ClientHello naming a --no-inspect host that ADDRESS does not serve."""
        kind, name = client_hello_sni(first)
        if kind != "tls" or not host_matches(name, self.config.get("no_inspect", [])):
            return True
        if self.serves_name(address, port, name):
            return True
        self.log({**record, "host": name, "event": "denied",
                  "reason": f"{address} is not a server of {name}, so --no-inspect does not apply"})
        return False

    def serves_name(self, address, port, name):
        """True if ADDRESS:PORT proves, with a certificate the system trusts, to be a server of NAME: an
        unopened connection may not carry a name that belongs elsewhere. Answers are cached for a while."""
        key = (address, port, name)
        with self.lock:
            cached = self.verified.get(key)
        if cached and cached > time.monotonic():
            return True
        try:
            with socket.create_connection((address, port), timeout=15) as raw:
                with self.upstream.wrap_socket(raw, server_hostname=name):
                    pass
        except (OSError, ssl.SSLError, ValueError):
            return False
        with self.lock:
            now = time.monotonic()
            if len(self.verified) >= PROXY_VERIFIED:  # expired ones first, then the oldest
                self.verified = {k: until for k, until in self.verified.items() if until > now}
                while len(self.verified) >= PROXY_VERIFIED:
                    self.verified.pop(next(iter(self.verified)))
            self.verified[key] = now + 600
        return True

    @staticmethod
    def http_host(data):
        """The Host header of a plain HTTP request at the start of DATA, or None."""
        match = re.search(rb"\r\nhost:[ \t]*([A-Za-z0-9.-]{1,253})", data or b"", re.I)
        return match[1].decode() if match else None

    def peek(self, client, wait=PEEK_TIMEOUT, complete=30.0):
        """The client's first bytes, once they show what it speaks (see first_bytes_state); b"" if it says
        nothing for WAIT seconds; what it sent so far if that stays incomplete for COMPLETE seconds more."""
        started, data = time.monotonic(), b""
        client.settimeout(0.05)
        while True:
            try:
                data = client.recv(16384, socket.MSG_PEEK)
                if not data:
                    return b""  # closed
            except (socket.timeout, BlockingIOError):
                pass
            elapsed = time.monotonic() - started
            if data and first_bytes_state(data) == "complete":
                return data
            if not data and elapsed >= wait:
                return b""
            if data and elapsed >= wait + complete:
                return data
            if data:
                time.sleep(0.05)

    def upstream_context(self, name, port):
        """How the real server is checked: against the system's CAs, an --llm endpoint also against its own CA."""
        for entry in self.config.get("llm") or []:
            if entry.get("ca") and entry["scheme"] == "https" and entry["port"] == port and entry["host"] == name.lower():
                with self.lock:
                    contexts = self.__dict__.setdefault("llm_contexts", {})
                    if entry["name"] not in contexts:
                        context = ssl.create_default_context(cafile=self.upstream_cafile)
                        context.load_verify_locations(cadata=entry["ca"])
                        context.set_alpn_protocols(["http/1.1"])
                        contexts[entry["name"]] = context
                    return contexts[entry["name"]]
        return self.upstream

    def prove(self, record, address, port, name, ok):
        """Tell the supervisor whether the certificate of a connection the human approved is valid for the
        server name the agent sent: what lets "Allow always" remember the name instead of the address."""
        approval = record.get("approval")
        if name and self.who is not None and isinstance(approval, dict) and approval.get("decision") == "allow":
            self.who.send("verified", {"address": address, "port": port, "name": name, "ok": bool(ok)})

    def intercept(self, client, upstream, name, address, record, started, port=443):
        try:
            server = self.upstream_context(name or address, port).wrap_socket(upstream, server_hostname=name or address)
        except (ssl.SSLError, OSError) as exc:
            self.prove(record, address, port, name, False)
            self.log({**record, "event": "tls-upstream-failed", "error": str(exc),
                      "hint": f"the server's certificate is not trusted; --no-inspect {name or address} passes "
                              "this connection through unopened"})
            return
        self.prove(record, address, port, name, True)
        try:
            context = self.server_context(name or address)
            agent = context.wrap_socket(client, server_side=True)
        except (ssl.SSLError, OSError, ValueError, RuntimeError) as exc:
            self.log({**record, "event": "tls-refused", "error": str(exc),
                      "hint": f"the program did not accept the session CA; --no-inspect {name or address} passes "
                              "this host through unopened (then only its name is logged)"})
            return
        stream = Stream(agent)
        # Whoever speaks first: the agent (a request, read until it is clear what it is) or the server
        # (a protocol where it greets first). Nothing is passed on before that is decided.
        greeting = b""
        while True:
            ready = [agent] if agent.pending() else select.select([agent, server], [], [], IDLE_TIMEOUT)[0]
            if not ready:
                return
            if agent in ready:
                break
            # The server's socket also turns readable for TLS records without data (TLS 1.3 session
            # tickets); only application data means that the server speaks first.
            server.setblocking(False)
            try:
                greeting = server.recv(65536)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                continue
            finally:
                server.setblocking(True)
                server.settimeout(IDLE_TIMEOUT)
            if not greeting:
                return
            break
        if agent in ready:
            def decided(data):
                data = LEADING_CRLF.sub(b"", data)
                return len(data) >= 16 or b" " in data or b"\r\n" in data or \
                    (bool(data) and data != b"\r" and not re.fullmatch(rb"[A-Z]+", data))
            agent.settimeout(30)
            with contextlib.suppress(socket.timeout, ssl.SSLError, OSError):
                while not decided(stream.buffer) and stream.fill():
                    pass
            if not stream.buffer:
                return
            if not decided(stream.buffer):
                self.log({**record, "event": "denied", "reason": "an incomplete HTTP request line"})
                return
        agent.settimeout(IDLE_TIMEOUT)
        request = LEADING_CRLF.sub(b"", stream.buffer)
        if request.startswith(b"PRI * HTTP/2"):
            self.log({**record, "event": "denied", "reason": "HTTP/2 was not negotiated"})
            return
        if re.match(rb"[A-Z]{3,10} \S", request):
            self.exchange(stream, agent, server, "https", name or address, record)
            return
        if stream.buffer and (port in HTTP_PORTS or self.has_http_rules(address)):
            self.log({**record, "event": "denied", "reason": f"not HTTP inside TLS on port {port}"})
            return
        self.observe(Ruleset.request(name or f"{address}:{port}", "GET", "/", [], address), any_path=True)
        counts = [len(stream.buffer), len(greeting)]
        if stream.buffer:
            send_all(server, stream.buffer)
        if greeting:
            send_all(agent, greeting)
        relay(agent, server, counts)
        self.log({**record, "event": "tls", "bytes_out": counts[0], "bytes_in": counts[1],
                  "ms": int((time.monotonic() - started) * 1000)})

    def git_token(self, scheme, origin, record, method, path, target, headers):
        """The Basic credentials (bytes) the proxy adds to this request (--token-proxy, sandbox-ai git-logins), or
        None: over HTTPS only to the server whose certificate was checked for the token's host (ORIGIN is that
        name); for a server on the host's loopback (plain http://localhost:PORT) only to the loopback port it was
        given for, which the sandbox reaches only as the host's (pasta, the relay); always on the token's port, for
        Git's smart HTTP requests, and never when the agent sends credentials of its own."""
        tokens = self.config.get("git_tokens", [])
        if not tokens or header(headers, "authorization"):
            return None
        destination = str(record.get("destination", ""))
        port = destination.rsplit(":", 1)[-1]
        address = destination.rsplit(":", 1)[0].strip("[]")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
        git = path.endswith(("/git-upload-pack", "/git-receive-pack")) and method.upper() == "POST" or \
            path.endswith("/info/refs") and method.upper() == "GET" and \
            query.get("service", [""])[0] in ("git-upload-pack", "git-receive-pack")
        if not git:
            return None
        for entry in tokens:
            if str(entry["port"]) != port:
                continue
            if entry.get("loopback"):
                try:
                    # Only where the host's port is (HOST_LOOPBACK): at 127.0.0.5 the agent could listen itself.
                    to_host = str(ipaddress.ip_address(address)) in HOST_LOOPBACK
                except ValueError:
                    to_host = False
                if scheme == "http" and to_host:
                    return base64.b64encode(f"{entry['user']}:{entry['token']}".encode())
            elif scheme == "https" and not is_address_origin(origin) and entry["host"] == origin.lower():
                return base64.b64encode(f"{entry['user']}:{entry['token']}".encode())
        return None

    def credential_host_refusal(self, scheme, origin, port, headers, token, llm, keys):
        """Why a request the proxy would add a credential to (a Git login TOKEN, the --llm endpoint LLM, the
        --api-key/--publish KEYS) is refused, or None: its Host header must name the server the credential is
        bound to, the verified name (HTTPS) or, over plain http, the endpoint's name or pinned addresses (an --llm
        endpoint) or the host's loopback (a Git login for it). Never forwarded without the credential instead: the
        agent would learn nothing, and the human would not see the attempt."""
        if not (token or llm or keys):
            return None
        if scheme == "https":
            names = {origin.lower().rstrip(".")}
            shown = next(iter(names))
        else:
            names, shown = (set(LOOPBACK_NAMES), "localhost") if token else (set(), None)
            for entry in self.config.get("llm") or []:
                if entry["name"] == llm:
                    names |= {str(entry["host"]).lower(), *entry.get("addresses", [])}
                    shown = str(entry["host"]).lower()
                    if shown in LOOPBACK_NAMES:
                        names |= LOOPBACK_NAMES
        problem = host_header_problem(headers, scheme, port, names, shown)
        if not problem:
            return None
        what = ", ".join(([f"the Git login for {origin}"] if token else []) + ([f"--llm {llm}"] if llm else [])
                         + [f"--api-key {name}" for name in keys])
        return f"{problem} ({what}); a request carrying it must name that server, so it was not sent"

    def has_http_rules(self, address):
        """ADDRESS is pinned for a host with HTTP rules: on any port, only HTTP the proxy can check gets there."""
        return self.rules.pins(address)

    def http_refused(self, origin, method, path, host_header=None, address=None):
        """Why the rules refuse METHOD PATH without a body (or hold it for a question), or None; see Ruleset.http.
        The rules that apply are those of every name the request could reach: the verified server name (ORIGIN,
        else address:port), the Host header (servers such as GitHub route by it, and one certificate covers many
        names) and the pinned addresses of ADDRESS, the destination."""
        facts = Ruleset.request(origin, method, path, [["Host", host_header]] if host_header else [], address)
        verdict = self.rules.http(facts)
        return None if verdict["decision"] == "allow" else verdict["reason"]

    def http_rule_body(self, parts, headers):
        """(pieces read, body) for body conditions: up to RULE_BODY_LIMIT of PARTS is read before anything goes
        to the server; a larger body is AMBIGUOUS. The pieces are passed on afterwards as they came."""
        early, data = [], b""
        for raw, payload in parts:
            early.append((raw, payload))
            data += payload
            if len(data) > RULE_BODY_LIMIT:
                return early, AMBIGUOUS
        return early, Ruleset.body(data, headers)

    def http_ask(self, verdict, method, url, host, path, body, asked_by=None):
        """The human's answer to a request an ask rule holds (kind "http"); without approvals it is refused.
        ASKED_BY: the process and sandbox of the connection, shown in the question."""
        if self.asker is None:
            return {"decision": "deny", "scope": "once", "reason": "asking is off (--ask off or no approvals)"}
        details = {"rule": verdict["rule"], "ruleset": verdict["ruleset"], "method": printable_text(method, 20),
                   "host": printable_text(host, 300), "path": printable_text(path, 2000),
                   "url": printable_text(url, 2000), "reason": printable_text(verdict["reason"], 1000),
                   **({"taint": verdict["taint"]} if verdict.get("taint") else {}), **(asked_by or {})}
        if isinstance(body, (dict, list)):
            details["body"] = printable_text(redact_text(json.dumps(shortened(body), default=repr)), 1000)
        return self.asker("http", details)

    def exchange(self, stream, client, upstream, scheme, origin, record):
        """HTTP/1.1 requests and responses on one connection, until either side ends it. ORIGIN is the
        server as the proxy knows it (the name its certificate was checked for, else address:port); the
        push check goes by it, never by the agent's Host header."""
        replies = Stream(upstream)
        limit = int(self.config.get("body_limit", 65536))
        while True:
            current = {}
            try:
                head = stream.head()
                if head is None:
                    return
                started = time.monotonic()
                line, headers = parse_head(head)
                method, _, rest = line.partition(" ")
                # Exactly METHOD SP TARGET SP VERSION, no fragment (RFC 9112): a lenient server could read another
                # target than the one the rules and the push check saw (/x#/../gists, or "/x /gists").
                if not REQUEST_LINE.fullmatch(line) or "#" in rest.rpartition(" ")[0]:
                    send_all(client, b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    self.log({**record, "event": "denied", "method": printable_text(method, 20),
                              "request_line": printable_text(redact_text(line), 500),
                              "request": {"headers": redact_headers(headers)},
                              "reason": "a malformed request line (one space each, no fragment)"})
                    return
                if sum(1 for name, _ in headers if name.lower() == "host") > 1:
                    # Servers disagree on which one counts; rules and logs must see the one that is used.
                    send_all(client, b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    self.log({**record, "event": "denied", "method": method, "reason": "more than one Host header",
                              "request": {"headers": redact_headers(headers)}})
                    return
                target = rest.rpartition(" ")[0] or rest
                # Logged and asked about as where it goes: the verified name (else the address), never the Host
                # header the agent wrote; a Host naming something else is kept beside it as a claim.
                authority, claim = verified_authority(scheme, origin, record, header(headers, "host"))
                url = target if "://" in target else f"{scheme}://{authority}{target}"
                if "://" in target:
                    send_all(client, b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    self.log({**record, "event": "denied", "method": method, "url": redact_text(target),
                              "reason": "requests through another proxy (absolute URLs) would hide where they go",
                              "request": {"headers": redact_headers(headers)}})
                    return
                if method.upper() == "CONNECT":
                    send_all(client, b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    self.log({**record, "event": "denied", "method": method, "url": redact_text(target),
                              "reason": "tunnels through another proxy would hide what passes",
                              "request": {"headers": redact_headers(headers)}})
                    return
                # Where the request really goes: the server the proxy verified, with the path from the request
                # line. Never the agent's Host header.
                path = normal_path(target)
                checked = f"{scheme}://{origin}{path}"
                entry = {**record, "event": "http", "method": method, "url": redact_text(url),
                         **({"host_header": claim} if claim else {})}
                current["entry"] = entry
                destination = str(record.get("destination", "")).rsplit(":", 1)[0].strip("[]") or None
                parts, facts = None, None
                if self.rules.http_active:
                    facts = Ruleset.request(origin, method, target, headers, destination)
                    facts["taint"] = self.taint()
                    if facts["taint"]:
                        entry["tainted"] = facts["taint"].get("since")
                    if self.rules.needs_body(facts):
                        # Read before the decision and before anything reaches the server; passed on as it came.
                        framing = request_framing(headers)
                        head = self.continued(client, head, headers)
                        parts = stream.body(framing)
                        early, facts["body"] = self.http_rule_body(parts, headers)
                        parts = itertools.chain(early, parts)
                    ruled = self.rules.http(facts)
                    entry.update(decision=ruled["decision"], rule=ruled["rule"], ruleset=ruled["ruleset"])
                    refusal = ruled["reason"] if ruled["decision"] == "deny" else None
                    if ruled["decision"] == "ask":
                        answer = self.http_ask(ruled, method, redact_text(url), authority,
                                               facts["paths"][0] or "/", facts["body"],
                                               {**self.asked_by(record), **({"host_header": claim} if claim else {})})
                        entry["approval"] = {k: answer.get(k) for k in ("decision", "scope", "reason")}
                        if answer.get("decision") != "allow":
                            refusal = f"not approved ({answer.get('reason', 'refused')}): {ruled['reason']}"
                    if refusal:
                        message = f"sandbox-ai: {refusal}\n".encode()
                        send_all(client, b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                                 + f"Content-Length: {len(message)}\r\n\r\n".encode() + message)
                        request = {"headers": redact_headers(headers)}
                        if self.debug:  # what it would have sent, read after the refusal went out
                            request["body"] = self.refused_body(client, stream, parts, headers, limit)
                        self.log({**entry, "event": "denied", "reason": refusal, "request": request})
                        current.pop("entry", None)
                        return
                if parts is None:
                    framing = request_framing(headers)
                    head = self.continued(client, head, headers)
                    parts = stream.body(framing)
                parts = canary_request(self, head, parts, headers, entry, record, origin)
                pending, captured = [], Capture(limit)
                content_type = (header(headers, "content-type") or "").split(";")[0].strip().lower()
                if (method.upper() == "POST" and path.endswith("git-receive-pack")) or \
                        content_type == "application/x-git-receive-pack-request" or \
                        (ambiguous_path(target) and "receive-pack" in urllib.parse.unquote(target).lower()):
                    verdict, commands, capabilities = self.check_push(checked, headers, parts, pending, captured,
                                                                      record)
                    entry["push"] = verdict
                    canary_push(self, verdict, entry, record)
                    if not verdict["ok"]:
                        with contextlib.suppress(ValueError, OSError):
                            for _, payload in parts:  # read the rest, so that Git reads our answer
                                captured.add(payload)
                        send_all(client, push_rejection(commands or [], verdict["message"], capabilities))
                        entry.update(status=200, request={"headers": redact_headers(headers),
                                                          "body": body_record(captured.data, captured.total, headers, limit)},
                                     ms=int((time.monotonic() - started) * 1000))
                        self.log(entry)
                        current.pop("entry", None)
                        return
                token = self.git_token(scheme, origin, record, method, path, target, headers)
                if token and head.endswith(b"\r\n\r\n"):
                    # Added on the wire only: the log keeps the agent's headers, the token never appears there.
                    head = head[:-2] + b"Authorization: Basic " + token + b"\r\n\r\n"
                    entry["token_added"] = True
                port = int(str(record.get("destination", "")).rsplit(":", 1)[-1] or 0)
                before = head
                head, llm = llm_head(self.config.get("llm"), scheme, origin, port, path, head, target)
                if llm:
                    entry["llm"] = llm  # its key went in on the wire; the log keeps the agent's headers
                llm_added = bool(llm) and head != before
                # Those given during the run first: a key given again under a name of the start replaces it.
                head, keys = api_key_head(added_api_keys() + (self.config.get("api_keys") or []), scheme, origin,
                                          port, head)
                if keys:
                    entry["api_keys"] = keys  # the keys went in on the wire; the log keeps the placeholders
                refusal = self.credential_host_refusal(scheme, origin, port, headers, bool(token),
                                                       llm if llm_added else None, keys)
                if refusal:
                    message = f"sandbox-ai: {refusal}\n".encode()
                    send_all(client, b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                             + f"Content-Length: {len(message)}\r\n\r\n".encode() + message)
                    for name in ("token_added", "llm", "api_keys"):
                        entry.pop(name, None)  # nothing was added: the request went nowhere
                    self.log({**entry, "event": "denied", "reason": refusal, "rule": "(host)",
                              "request": {"headers": redact_headers(headers)}})
                    current.pop("entry", None)
                    return
                added = bool(entry.get("token_added") or llm or keys)
                if added:
                    head = identity_head(head)
                send_all(upstream, head)
                sent = len(head)
                for raw in pending:
                    send_all(upstream, raw)
                    sent += len(raw)
                for raw, payload in parts:
                    send_all(upstream, raw)
                    sent += len(raw)
                    captured.add(payload)
                entry["request"] = {"headers": redact_headers(headers),
                                    "body": body_record(captured.data, captured.total, headers, limit)}
                while True:
                    reply = replies.head()
                    if reply is None:
                        entry.update(error="the server closed the connection without an answer")
                        self.log(entry)
                        current.pop("entry", None)
                        return
                    status_line, reply_headers = parse_head(reply)
                    try:
                        status = int(status_line.split(" ")[1])
                    except (IndexError, ValueError):
                        raise ValueError("malformed HTTP status line") from None
                    if facts is not None:
                        self.observe(facts)  # before the agent sees a byte of the answer
                    if added:  # a server that echoes what the proxy added gives the agent stars
                        reply = Scrubber(s.encode("latin-1", "replace") for s in self.own_secrets()).mask(reply)
                    send_all(client, reply)
                    if 100 <= status < 200 and status != 101:
                        continue
                    break
                advert = method.upper() == "GET" and path.endswith("/info/refs") and \
                    "service=git-receive-pack" in urllib.parse.unquote(target)
                usage = UsageScanner() if is_model_request(path) else None
                inflate = None
                key = checked.rsplit("/info/refs", 1)[0]
                answer = Capture(ADVERT_LIMIT if advert else limit)
                received = len(reply)
                closing = response_framing(method, status, reply_headers)
                pieces = replies.body(closing)
                if added:
                    encoding = (header(reply_headers, "content-encoding") or "identity").lower()
                    if encoding != "identity":
                        entry["unscrubbed"] = encoding  # asked for identity, got this: passed on as it came
                    else:
                        scrubber = Scrubber(s.encode("utf-8") for s in self.own_secrets())
                        pieces = scrubbed_pieces(pieces, scrubber, bool(closing and closing[0] == "chunked"))
                for raw, payload in pieces:
                    send_all(client, raw)
                    received += len(raw)
                    answer.add(payload)
                    if usage is not None:
                        encoding = (header(reply_headers, "content-encoding") or "").lower()
                        if encoding in ("gzip", "x-gzip", "deflate"):
                            inflate = inflate or zlib.decompressobj(47)
                            with contextlib.suppress(zlib.error):
                                usage.feed(inflate.decompress(payload, 1 << 20))
                        elif not encoding or encoding == "identity":
                            usage.feed(payload)
                if advert and status == 200:
                    with self.lock:
                        self.adverts[key] = advertised_refs(self.decoded(answer.data, reply_headers))
                        while len(self.adverts) > 64:
                            self.adverts.pop(next(iter(self.adverts)))
                model = usage.result() if usage is not None else None
                if model:
                    entry["model"] = model
                if credential_headers(headers):
                    entry["credentials"] = credential_headers(headers)
                entry.update(status=status, response={"headers": redact_headers(reply_headers),
                                                      "body": body_record(answer.data[:limit], answer.total,
                                                                          reply_headers, limit)},
                             bytes_out=sent, bytes_in=received, ms=int((time.monotonic() - started) * 1000))
                self.log(entry)
                current.pop("entry", None)
                if status == 101:
                    counts = [len(stream.buffer), len(replies.buffer)]
                    if stream.buffer:
                        send_all(upstream, stream.buffer)
                    if replies.buffer:
                        send_all(client, replies.buffer)
                    relay(client, upstream, counts)
                    self.log({**record, "event": "upgraded", "url": entry["url"], "bytes_out": counts[0],
                              "bytes_in": counts[1]})
                    return
                wants_close = any((header(h, "connection") or "").lower() == "close" for h in (headers, reply_headers))
                if wants_close or (closing and closing[0] == "close") or line.endswith("HTTP/1.0"):
                    return
            except CanaryRefused as refused:
                canary_refusal(self, client, refused, current.get("entry") or record)
                return
            except OSError as exc:
                # The request went out: it is logged even if a side breaks the connection afterwards (a client
                # that closes once it has what it wanted), never only as a bare error.
                if current.get("entry") is not None:
                    current["entry"].setdefault("error", f"{type(exc).__name__}: {exc}")
                    self.log(current["entry"])
                    return
                raise

    @staticmethod
    def refused_body(client, stream, parts, headers, limit):
        """--log-debug: the logged form of a refused request's body (up to LIMIT), read within 2 seconds."""
        captured = Capture(limit)
        with contextlib.suppress(OSError, ValueError):
            client.settimeout(2)
            for _, payload in parts if parts is not None else stream.body(request_framing(headers)):
                captured.add(payload)
                if captured.total > limit:
                    break
        return body_record(captured.data, captured.total, headers, limit)

    @staticmethod
    def continued(client, head, headers):
        """HEAD without its Expect line, with 100-continue answered here: a body to check arrives before anything
        reaches the server."""
        if (header(headers, "expect") or "").lower() == "100-continue":
            send_all(client, b"HTTP/1.1 100 Continue\r\n\r\n")
            head = re.sub(rb"(?im)^expect:[^\r\n]*\r\n", b"", head)
        return head

    @staticmethod
    def decoded(data, headers):
        if (header(headers, "content-encoding") or "").lower() in ("gzip", "x-gzip", "deflate"):
            with contextlib.suppress(zlib.error):
                return zlib.decompressobj(47).decompress(data, ADVERT_LIMIT)
        return data

    def check_push(self, url, headers, parts, pending, captured, record=None):
        """(verdict, commands, capabilities) of a receive-pack request; reads the command lines into PENDING.
        RECORD: the connection's record, whose process a question names."""
        repository = url.rsplit("/git-receive-pack", 1)[0]
        parts = iter(parts)  # the commands, then (git push -o) the options: read on from where the first stopped
        gzip = (header(headers, "content-encoding") or "").lower() in ("gzip", "x-gzip")
        inflater, payload = zlib.decompressobj(47) if gzip else None, b""
        complete, lines = False, []
        try:
            for raw, data in parts:
                pending.append(raw)
                captured.add(data)
                payload += inflater.decompress(data) if inflater else data
                lines, complete = pkt_lines(payload)
                if complete or len(payload) > COMMANDS_LIMIT:
                    break
            if not complete:
                if not payload:
                    return {"ok": True, "message": "empty request"}, None, set()
                raise ValueError("the push's command list did not end")
            if not lines:
                return {"ok": True, "message": "probe request"}, None, set()
            commands, capabilities = push_commands(lines)
            options = None
            if "push-options" in capabilities:
                # git push -o: what follows the commands is published too (GitLab makes merge request titles
                # and descriptions of them), so it is checked as the commits are.
                sections, complete = pkt_sections(payload, 2)
                for raw, data in parts if not complete else ():
                    pending.append(raw)
                    captured.add(data)
                    payload += inflater.decompress(data) if inflater else data
                    sections, complete = pkt_sections(payload, 2)
                    if complete or len(payload) > COMMANDS_LIMIT:
                        break
                if not complete:
                    raise ValueError("the push's options did not end")
                options = [line.rstrip(b"\n").decode("utf-8", "replace") for line in sections[1]]
        except (ValueError, zlib.error) as exc:
            return {"ok": False, "message": f"the push cannot be checked: {exc}"}, None, set()
        if self.checker is None:
            return {"ok": True, "message": "privacy check off"}, commands, capabilities
        with self.lock:
            advertised = sorted(self.adverts.get(repository, ()))
        request = {"url": repository, "commands": [list(c) for c in commands], "advertised": advertised,
                   **({"push_options": options} if options else {})}
        try:
            with self.check_lock:
                verdict = self.ask_checker(request)
            if not isinstance(verdict, dict) or "ok" not in verdict:
                raise ValueError("no answer from the privacy check")
        except (OSError, ValueError) as exc:
            verdict = {"ok": False, "message": f"the privacy check is not available: {exc}"}
        if verdict.get("ok") and verdict.get("warnings") and self.asker is not None:
            answer = self.asker("push", {"url": printable_text(repository, 2000),
                                         "refs": [printable_text(ref, 300) for _, _, ref in commands][:50],
                                         "message": printable_text(verdict.get("message", "")),
                                         **(self.asked_by(record) if record is not None else {})})
            verdict["approval"] = {k: answer.get(k) for k in ("decision", "scope", "reason")}
            if answer.get("decision") != "allow":
                verdict = {**verdict, "ok": False, "message": "the push has privacy warnings and was not approved:\n"
                           + str(verdict.get("message", ""))}
        verdict["refs"] = [ref for _, _, ref in commands]
        return verdict, commands, capabilities

    def ask_checker(self, request):
        """The checker's verdict on REQUEST (under check_lock); a refusal when it did not come within
        PUSH_CHECK_SECONDS, or while the checker is still busy with an earlier request that took too long. The
        checker runs as another user and cannot be stopped or restarted from here (the proxy has no privileges
        left): its late answers are read and dropped once they arrive, and it is used again after that."""
        while self.checker_owed:
            line = self.checker_line(time.monotonic())  # what arrived meanwhile; no waiting
            if line is None:
                return {"ok": False, "message": "the privacy check is still busy with an earlier push that took too "
                        "long; the push is refused, try again later"}
            self.checker_owed -= 1
        self.checker.stdin.write((json.dumps(request) + "\n").encode())
        self.checker.stdin.flush()
        line = self.checker_line(time.monotonic() + PUSH_CHECK_SECONDS)
        if line is None:
            self.checker_owed += 1
            return {"ok": False, "message": f"the privacy check did not answer within {PUSH_CHECK_SECONDS} s; the "
                    "push is refused, try again later"}
        return json.loads(line or b"null")

    def checker_line(self, deadline):
        """The checker's next line (b"" once it ended), or None if none came by DEADLINE (monotonic)."""
        fd = self.checker.stdout.fileno()
        while b"\n" not in self.checker_buffer:
            if not select.select([fd], [], [], max(0.0, deadline - time.monotonic()))[0]:
                return None
            chunk = os.read(fd, 65536)
            if not chunk:
                line, self.checker_buffer = self.checker_buffer, b""
                return line
            self.checker_buffer += chunk
        line, _, self.checker_buffer = self.checker_buffer.partition(b"\n")
        return line + b"\n"


def drop_to_proxy_user():
    """No capabilities, groups or privilege gain from here on: the proxy reads what agents send."""
    os.setgroups([])
    os.setresgid(PROXY_UID, PROXY_UID, PROXY_UID)
    os.setresuid(PROXY_UID, PROXY_UID, PROXY_UID)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "Cannot set no_new_privs for the proxy")


def proxy_listeners(port=PROXY_PORT):
    """Listening sockets on 127.0.0.1 and ::1 (where IPv6 exists) for the redirected connections."""
    listeners = []
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
            sock.listen(128)
        except OSError:
            sock.close()
            if family == socket.AF_INET:
                raise
            continue
        listeners.append(sock)
    return listeners


def stdin_is_pipe(fd=0):
    try:
        return (os.fstat(fd).st_mode & 0o170000) == 0o010000  # S_IFIFO
    except OSError:
        return False


def exit_on_eof(stream, exit=os._exit):
    """Exit once STREAM (the proxy's stdin, a pipe from the supervisor) is closed or fails."""
    with contextlib.suppress(OSError, ValueError):
        while stream.read(4096):
            pass
    exit(0)


def proxy_main(log_fd, ready_fd, ask_fd=None, asking=True):
    """The proxy process: CA, listeners and push checker as root, then the proxy user for the rest. ASK_FD is
    the channel to the supervisor: questions (approvals, unless ASKING is False) and which process a
    connection belongs to."""
    config = json.loads((PROXY_DIR / "proxy.json").read_text())
    create_ca(PROXY_RUN, CA_DIR)
    listeners = proxy_listeners(int(config.get("port", PROXY_PORT)))
    checker = None
    if config.get("push_check"):
        # The checker reads the workspaces as their owner, without any capability; nothing it is sent
        # comes from the network unchecked.
        checker = subprocess.Popen(DROP_ALL + ["python3", "-I", str(PROXY_DIR / "privacy.py"), "serve",
                                               str(PROXY_DIR / "push.json")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd="/",
                                   env={"PATH": base_env()["PATH"], "HOME": "/nonexistent", "LANG": "C.UTF-8"})
    allow = PROXY_RUN / "allow.json"  # `sandbox-ai net` changes it through the supervisor
    allow.write_text(json.dumps(config.get("allow", {})))
    allow.chmod(0o644)
    config["allow_file"] = str(allow)
    config["taint_file"] = str(PROXY_RUN / "taint.json")  # written by the supervisor (Taint)
    config["rules_file"] = str(PROXY_RUN / RELOADED_RULES)  # written by the supervisor (RunRules)
    for root, dirs, files in os.walk(PROXY_RUN):
        for name in [*dirs, *files]:
            os.chown(os.path.join(root, name), PROXY_UID, PROXY_UID)
    os.chown(PROXY_RUN, PROXY_UID, PROXY_UID)
    drop_to_proxy_user()
    if stdin_is_pipe():  # started by the supervisor (start_proxy), which stops it by closing the pipe
        threading.Thread(target=exit_on_eof, args=(sys.stdin.buffer,), daemon=True).start()
    channel = AskChannel(socket.socket(fileno=ask_fd)) if ask_fd is not None else None
    server = ProxyServer(config, log_fd, checker, run_dir=PROXY_RUN, asker=channel if asking else None, who=channel)
    server.serve(listeners)
    # Freed memory back to the system every half minute, its TLS contexts dropped if it grows large anyway (the
    # supervisor's guard; no records: this user writes no control directory).
    def drop_contexts():
        with server.lock:
            server.contexts.clear()
    memory_shedder("proxy tls contexts", drop_contexts)
    MemoryGuard(None, trace=False).start(threading.Event())
    os.write(ready_fd, b"1")
    os.close(ready_fd)
    return server.wait()


def start_proxy(cfg, broker=None, who=None, taint=None):
    """Start the proxy before the agent and make the agent's TLS libraries trust the session CA. With a
    BROKER the proxy gets a channel to put questions to it (approvals), with WHO (an Attribution) one to ask
    which process a connection belongs to; the same channel."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    log_fd = os.open(LOG_DIR / cfg["log"], flags, 0o600) if LOG_DIR.is_dir() else os.open(os.devnull, os.O_WRONLY)
    ready_r, ready_w = os.pipe()
    mine, theirs = socket.socketpair() if broker is not None or who is not None or taint is not None else (None, None)
    try:
        ask = [str(theirs.fileno()), *([] if broker is not None else ["--no-ask"])] if theirs else []
        # stdin is how the proxy is stopped: it runs as its own user, which the supervisor may not signal
        # (Podman gives it no CAP_KILL); it exits when the pipe closes.
        process = subprocess.Popen(["python3", "-I", "/sandbox/runtime.py", "--internal-proxy", str(log_fd),
                                    str(ready_w), *ask], pass_fds=(log_fd, ready_w, *([theirs.fileno()] if theirs else [])),
                                   env=base_env(), stdin=subprocess.PIPE)
        os.close(ready_w)
        ready_w = -1
        deadline = time.monotonic() + 30
        while not select.select([ready_r], [], [], 0.2)[0]:
            if process.poll() is not None or time.monotonic() > deadline:
                stop_process(process)
                raise RuntimeError("The connection proxy did not start (see its error above)")
        if os.read(ready_r, 1) != b"1":
            stop_process(process)
            raise RuntimeError("The connection proxy did not start (see its error above)")
    finally:
        os.close(log_fd)
        os.close(ready_r)
        if ready_w >= 0:
            os.close(ready_w)
        if theirs is not None:
            theirs.close()
    if mine is not None:
        threading.Thread(target=serve_asks, args=(mine, broker, who, taint), daemon=True).start()
    trust_session_ca()
    return process


def trust_session_ca(bundles=SYSTEM_BUNDLES, mount=None):
    """Bind the bundle with the session CA over the system's CA bundles, for programs that ignore
    SSL_CERT_FILE and the like (GnuTLS, for one)."""
    libc = ctypes.CDLL(None, use_errno=True)
    def bind(source, target):
        for flags in (4096, 4096 | 32 | 1):  # MS_BIND, then MS_BIND | MS_REMOUNT | MS_RDONLY
            if libc.mount(source.encode(), target.encode(), None, flags, None) != 0:
                raise OSError(ctypes.get_errno(), f"Cannot bind the session CA bundle over {target}")
    mount = mount or bind
    done = set()
    for path in bundles:
        real = os.path.realpath(path)
        if os.path.isfile(real) and real not in done:
            mount(str(CA_DIR / "bundle.pem"), real)
            done.add(real)
    return sorted(done)

# --- who opened a connection -----------------------------------------------------------------------
#
# Every connection the proxy sees, every lookup the DNS stub answers and every packet the firewall drops
# belongs to a socket of some process in the sandbox. The supervisor (PID 1, outside Firejail, the same
# user as the agent's processes) finds it: the socket's inode in /proc/net/{tcp,tcp6,udp,udp6}, then the
# process holding it in /proc/*/fd. Nothing the agent sends is taken for this (no header, no variable);
# what a process calls itself (comm, argv) is its own choice and is shown escaped, cut and redacted. A
# lookup that fails or takes too long is "unknown": it never holds a connection.

ATTRIBUTION_BUDGET = 0.2   # seconds one scan of /proc/*/fd may take; what it did not reach is unknown
ATTRIBUTION_RATE = 50      # lookups per second; beyond that they are unknown ("too many lookups")
ATTRIBUTION_WAIT = 0.25    # how long the proxy waits for the answer before it writes a record
REFUSAL_WAIT = 0.05        # how long a refusal waits for it: a reset unhashes the client's socket
TCP_STATES = {1: "ESTABLISHED", 2: "SYN_SENT", 3: "SYN_RECV", 4: "FIN_WAIT1", 5: "FIN_WAIT2", 6: "TIME_WAIT",
              7: "CLOSE", 8: "CLOSE_WAIT", 9: "LAST_ACK", 10: "LISTEN", 11: "CLOSING"}


class Attribution:
    """The process behind a socket of this sandbox, read from PROC as the supervisor sees it. SANDBOX names
    the sandbox ({"run", "agent", "backend", "role", "pod"}); DEBUG adds the ancestors' command lines and the
    process's sockets; SECRET_VALUES are redacted from command lines by value (--env, --secret-env)."""

    def __init__(self, proc=Path("/proc"), sandbox=None, debug=False, secret_values=(), budget=ATTRIBUTION_BUDGET,
                 rate=ATTRIBUTION_RATE, clock=time.monotonic):
        self.proc, self.debug, self.budget, self.rate, self.clock = Path(proc), debug, budget, rate, clock
        self.sandbox = {key: value for key, value in (sandbox or {}).items() if value is not None}
        self.values = sorted({v for v in secret_values if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True)
        self.lock = threading.Lock()
        self.owners, self.scanned = {}, None  # socket inode: (pid, fd), from the last scan
        self.cache, self.window, self.count = {}, 0.0, 0
        memory_part("attribution cache", lambda: len(self.cache))
        memory_part("attribution sockets", lambda: len(self.owners))
        memory_shedder("attribution cache", self.cache.clear)

    # -- sockets --

    @staticmethod
    def endpoint(text):
        """(address, port) of a /proc/net endpoint such as 0100007F:3A99; IPv4-mapped addresses as IPv4."""
        raw, _, port = text.partition(":")
        data = bytes.fromhex(raw)
        # The kernel prints each 32-bit word in host byte order (little endian on every target here).
        data = b"".join(data[i:i + 4][::-1] for i in range(0, len(data), 4))
        address = ipaddress.ip_address(data)
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address), int(port, 16)

    def sockets(self):
        """[{"protocol", "local", "remote", "state", "uid", "inode"}] of the sandbox's TCP and UDP sockets."""
        found = []
        for name in ("tcp", "tcp6", "udp", "udp6"):
            try:
                lines = (self.proc / "net" / name).read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                try:
                    state = int(fields[3], 16)
                    found.append({"protocol": name.rstrip("6"), "local": self.endpoint(fields[1]),
                                  "remote": self.endpoint(fields[2]), "uid": int(fields[7]), "inode": int(fields[9]),
                                  "state": TCP_STATES.get(state, str(state)) if name.startswith("tcp") else None})
                except (IndexError, ValueError):
                    continue
        return found

    def scan(self):
        """{inode: (pid, fd)} of every socket a process holds, as far as BUDGET allows."""
        owners, deadline = {}, self.clock() + self.budget
        try:
            pids = [entry.name for entry in os.scandir(self.proc) if entry.name.isdigit()]
        except OSError:
            return owners
        for pid in pids:
            if self.clock() > deadline:
                break
            try:
                fds = [entry.name for entry in os.scandir(self.proc / pid / "fd")]
            except OSError:
                continue  # gone, or not ours to read
            for fd in fds:
                with contextlib.suppress(OSError):
                    target = os.readlink(self.proc / pid / "fd" / fd)
                    if target.startswith("socket:["):
                        owners.setdefault(int(target[8:-1]), (int(pid), fd))
        return owners

    def owner(self, inode):
        """The PID holding socket INODE now, or None. A cached owner is checked again (its fd may have closed
        and its PID been reused); a socket not seen yet starts a new scan, at most every 50 ms."""
        with self.lock:
            for attempt in range(2):
                found = self.owners.get(inode)
                if found:
                    pid, fd = found
                    with contextlib.suppress(OSError):
                        if os.readlink(self.proc / str(pid) / "fd" / fd) == f"socket:[{inode}]":
                            return pid
                if attempt or (self.scanned is not None and self.clock() - self.scanned < 0.05 and not found):
                    return None
                self.owners, self.scanned = self.scan(), self.clock()
        return None

    # -- processes --

    def start(self, pid):
        """Start time of PID (field 22 of stat, clock ticks after boot): with the PID, it names one process."""
        return int((self.proc / str(pid) / "stat").read_text().rpartition(")")[2].split()[19])

    def status(self, pid):
        fields = {}
        for line in (self.proc / str(pid) / "status").read_text().splitlines():
            name, _, value = line.partition(":")
            fields[name] = value.split()
        return fields

    def argv(self, pid, count=32, total=2000):
        """PID's command line: at most COUNT arguments and TOTAL characters, secrets redacted, escaped."""
        with open(self.proc / str(pid) / "cmdline", "rb") as stream:
            raw = stream.read(64 << 10)
        result, used = [], 0
        for part in raw.split(b"\0")[:-1] if raw.endswith(b"\0") else raw.split(b"\0"):
            if len(result) >= count or used >= total:
                result.append("...")
                break
            text = part.decode("utf-8", "replace")
            for value in self.values:
                text = text.replace(value, "[redacted]")
            text = printable_text(redact_text(text), min(300, total - used))
            result.append(text)
            used += len(text)
        return result

    def link(self, pid, name):
        with contextlib.suppress(OSError):
            return printable_text(os.readlink(self.proc / str(pid) / name), 500)
        return None

    def process(self, pid):
        """What can be told of PID: PID and the PID inside Firejail (ns_pid), comm, argv, exe, cwd, uid, its
        ancestors' names; None if it ended, or if its PID belonged to another process by the end of reading."""
        try:
            start = self.start(pid)
            status = self.status(pid)
            comm = (self.proc / str(pid) / "comm").read_text().rstrip("\n")
            info = {"pid": pid, "ns_pid": int(status.get("NSpid", [pid])[-1]), "comm": printable_text(comm, 64),
                    "argv": self.argv(pid), "exe": self.link(pid, "exe"), "cwd": self.link(pid, "cwd"),
                    "uid": int(status["Uid"][0]), "ppid": int(status["PPid"][0]), "start": start}
            if self.start(pid) != start:
                return None
        except (OSError, ValueError, IndexError, KeyError):
            return None
        info["parents"] = [entry["comm"] for entry in self.ancestors(info["ppid"], 4)]
        if self.debug:
            info["tree"] = self.ancestors(info["ppid"], 16, argv=True)
            info["sockets"] = self.held(pid)
        return info

    def ancestors(self, pid, depth, argv=False):
        """PID and its parents, up to DEPTH of them: {"pid", "comm"} (and "argv")."""
        chain = []
        while pid > 0 and len(chain) < depth:
            try:
                status = self.status(pid)
                entry = {"pid": pid, "comm": printable_text((self.proc / str(pid) / "comm").read_text().rstrip("\n"), 64)}
                if argv:
                    entry["argv"] = self.argv(pid, count=12, total=600)
                parent = int(status["PPid"][0])
            except (OSError, ValueError, IndexError, KeyError):
                break
            chain.append(entry)
            pid = parent
        return chain

    def held(self, pid, limit=32):
        """The sockets PID holds: protocol, local and remote endpoint, state."""
        inodes = set()
        with contextlib.suppress(OSError):
            for entry in os.scandir(self.proc / str(pid) / "fd"):
                with contextlib.suppress(OSError):
                    target = os.readlink(entry.path)
                    if target.startswith("socket:["):
                        inodes.add(int(target[8:-1]))
        return [{"protocol": s["protocol"], "local": f"{s['local'][0]}:{s['local'][1]}",
                 "remote": f"{s['remote'][0]}:{s['remote'][1]}", "state": s["state"]}
                for s in self.sockets() if s["inode"] in inodes][:limit]

    # -- lookups --

    def allowed(self):
        """Rate limit: ATTRIBUTION_RATE lookups per second; an agent opening sockets in a loop gets unknowns."""
        with self.lock:
            now = self.clock()
            if now - self.window >= 1:
                self.window, self.count = now, 0
            self.count += 1
            return self.count <= self.rate

    def found(self, pid, why):
        info = self.process(pid) if pid else None
        result = {"process": info} if info else {"process": {"unknown": why}}
        return {**result, **({"sandbox": self.sandbox} if self.sandbox else {})}

    def lookup(self, protocol, address, port):
        """{"process", "sandbox"} for the socket of PROTOCOL bound to ADDRESS:PORT: the client end of a
        connection the proxy accepted, or of a DNS query."""
        try:
            key = (protocol, str(ipaddress.ip_address(str(address).split("%")[0])), int(port))
        except ValueError:
            return {"process": {"unknown": "not an address"}}
        with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() - cached[0] < 2:
                return cached[1]
        if not self.allowed():
            return {"process": {"unknown": "too many lookups"}, **({"sandbox": self.sandbox} if self.sandbox else {})}
        sockets = [s for s in self.sockets() if s["protocol"] == protocol and s["local"] == key[1:] and s["inode"]]
        if not sockets:
            result = self.found(None, "no such socket (closed already)")
        else:
            pid = next((p for p in map(self.owner, (s["inode"] for s in sockets)) if p), None)
            result = self.found(pid, "the process ended or closed the socket before it was found")
        with self.lock:
            self.cache[key] = (self.clock(), result)
            if len(self.cache) > 4096:
                self.cache.pop(next(iter(self.cache)))
        return result

    def by_remote(self, protocol, address, port):
        """For a packet the firewall dropped: the process whose socket of PROTOCOL is connected to
        ADDRESS:PORT (a TCP socket stays in SYN_SENT while it retries), with the socket's state."""
        if not self.allowed():
            return {"process": {"unknown": "too many lookups"}}
        try:
            target = (str(ipaddress.ip_address(str(address))), int(port))
        except ValueError:
            return {"process": {"unknown": "not an address"}}
        for candidate in self.sockets():
            if candidate["protocol"] == protocol and candidate["remote"] == target and candidate["inode"]:
                pid = self.owner(candidate["inode"])
                if pid:
                    return {**self.found(pid, "the process ended"), "state": candidate["state"]}
        return self.found(None, "no socket to that destination any more")

    def openers(self, path, limit=4):
        """[process info] of the processes that hold the file PATH open now (a canary file inotify reported):
        an fd whose target names the file (its path, or the same file name seen through another mount) and, when
        the file is still there, is the same file (device and inode). As far as BUDGET allows; a process that
        opened, read and closed the file before the scan is not found (inotify does not say who it was)."""
        try:
            info = os.stat(path, follow_symlinks=False)
            key = (info.st_dev, info.st_ino)
        except OSError:
            key = None
        name = os.path.basename(str(path))
        found, deadline, me = [], self.clock() + self.budget, os.getpid()
        try:
            pids = sorted((int(entry.name) for entry in os.scandir(self.proc) if entry.name.isdigit()), reverse=True)
        except OSError:
            return found
        for pid in pids:  # the newest first: a reader started for the occasion
            if self.clock() > deadline or len(found) >= limit:
                break
            if pid == me:
                continue
            try:
                fds = [entry.name for entry in os.scandir(self.proc / str(pid) / "fd")]
            except OSError:
                continue
            for fd in fds:
                with contextlib.suppress(OSError):
                    target = os.readlink(self.proc / str(pid) / "fd" / fd)
                    if target.endswith(" (deleted)"):
                        target = target[:-len(" (deleted)")]
                    if target != str(path) and not target.endswith("/" + name):
                        continue
                    if key is not None and target != str(path):
                        held = os.stat(self.proc / str(pid) / "fd" / fd)
                        if (held.st_dev, held.st_ino) != key:
                            continue
                    process = self.process(pid)
                    if process is not None:
                        found.append({**process, "fd": int(fd) if fd.isdigit() else fd})
                    break
        return found

    @staticmethod
    def peer(connection):
        """(pid, uid, gid) of the process at the other end of a Unix socket (SO_PEERCRED), as the kernel
        reports it: pid 0 when that process is not in this PID namespace's view. macOS (the relay of an MCP server
        on the host) has no SO_PEERCRED: LOCAL_PEERPID and LOCAL_PEERCRED (level SOL_LOCAL, 0) tell the same; an
        OSError where neither is there."""
        if not hasattr(socket, "SO_PEERCRED"):
            if sys.platform != "darwin":
                raise OSError(errno.ENOPROTOOPT, "no peer credentials on this system")
            pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]  # SOL_LOCAL, LOCAL_PEERPID
            # struct xucred: cr_version, cr_uid, cr_ngroups (short, padded), cr_groups[16]; the first group is the gid.
            uid, _, gid = struct.unpack_from("IhxxI", connection.getsockopt(0, 1, 76), 4)  # LOCAL_PEERCRED
            return pid, uid, gid
        data = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", data)


# --- taint: untrusted content in the run ------------------------------------------------------------
#
# CaMeL's rule at sandbox-ai's layer: nothing here sees how data flows inside the model's context, so once
# untrusted content has entered a run (a response of a host, or the result of an MCP tool, that a ruleset
# marks untrusted), every later action may depend on it. Rules with "when": {"tainted": true} hold from then
# on (writes to hosts other than the agent's own provider, pushes, chosen MCP tools: ask). The taint lasts for
# the whole run, and only the human takes it back (sandbox-ai net untaint); a new run starts untainted.

class Taint:
    """The run's taint, held by the supervisor. MIRROR: a copy the proxy reads (in PROXY_RUN, which the agent
    cannot see); LOG: the file taint events are appended to (the run's connection log)."""

    def __init__(self, mirror=None, log=None, clock=time.time):
        self.mirror, self.log, self.clock = mirror, log, clock
        # The mirror is written on a change only: the proxy reads a missing one as untainted, and a file made at
        # start-up would be handed to the proxy's user with the rest of its directory.
        self.lock, self.state = threading.Lock(), {"tainted": False}

    def shown(self):
        with self.lock:
            return dict(self.state)

    def mark(self, source):
        """Untrusted content entered the run: SOURCE {"layer", "rule", "ruleset", "what"}. The first source and
        its time are kept, later ones counted. Returns the new state."""
        source = {key: printable_text(str(value), 300) for key, value in (source or {}).items()
                  if key in ("layer", "rule", "ruleset", "what") and value is not None}
        with self.lock:
            first = not self.state.get("tainted")
            if first:
                self.state = {"tainted": True, "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
                              "source": source, "sources": 1}
            else:
                self.state["sources"] = self.state.get("sources", 1) + 1
            state = dict(self.state)
        if first:
            self.write()
            self.append({"event": "taint", "taint": state})
        return state

    def reset(self, by="the human (sandbox-ai net untaint)"):
        """Take the taint back; only the human does. Returns the state before."""
        with self.lock:
            before, self.state = self.state, {"tainted": False}
        if before.get("tainted"):
            self.write()
            self.append({"event": "untaint", "by": by, "was": before})
        return before

    def write(self):
        if self.mirror is not None:
            with contextlib.suppress(OSError):
                write_new(Path(self.mirror), json.dumps(self.shown()), 0o644)

    def append(self, record):
        if self.log is not None:
            with contextlib.suppress(OSError):
                with open(self.log, "a") as stream:
                    stream.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record})
                                 + "\n")

    @staticmethod
    def because(state):
        """Why a rule for tainted runs held: "untrusted content from WHAT (rule ID of RULESET) entered this run
        at TIME"."""
        source = state.get("source") or {}
        rule = f" (rule {source['rule']} of {source.get('ruleset', '?')})" if source.get("rule") else ""
        return f"untrusted content from {source.get('what', 'a source marked untrusted')}{rule} entered this run " \
               f"at {state.get('since', '?')}"


# --- rulesets --------------------------------------------------------------------------------------
#
# Allow, ask or deny per HTTP request (host, method, path, query, headers, JSON body) in the connection proxy
# and per MCP tool call (server, tool, arguments) in the MCP relay. Rulesets come from the administrator's
# policy, your own files (~/.config/sandbox-ai/rules.json, --rules, --allow-http) and the trusted project
# file. Each ruleset decides on its own and the strictest decision wins (deny over ask over allow), so no
# ruleset can widen what another one narrows, and none opens the firewall. Within a ruleset the first rule
# that matches decides, lowest priority number first, deny before ask before allow at equal priority.
#
# A condition is True, False or None: unknown, for what servers may read differently (a key given twice or
# in another case, a body that cannot be parsed, a method override header, a path that matches only when case
# is ignored). Allow rules need True; deny and ask rules take None as well, so that whatever cannot be judged
# is never allowed by a rule. A value that is missing fails a condition unless it says "optional": true.

RULE_DECISIONS = ("allow", "ask", "deny")  # in rising strictness
DECIDED = {"allow": "allowed", "ask": "held for a question", "deny": "denied"}
RULESET_KEYS = {"version", "description", "defaults", "http", "mcp", "devices", "canary", "tool", "untrusted"}
RULE_SECTIONS = ("http", "mcp", "devices", "tool")
# "canary": what a read of a canary file and a canary token leaving do (canary.py), in rising strictness
CANARY_ON_READ = ("report", "ask", "pause", "stop")
CANARY_ON_SEND = ("report", "block", "pause", "stop")
HTTP_RULE_KEYS = {"id", "description", "host", "methods", "path", "query", "headers", "body", "decision",
                  "priority", "reason", "when"}
MCP_RULE_KEYS = {"id", "description", "server", "tool", "args", "decision", "priority", "reason", "when"}
DEVICE_RULE_KEYS = {"id", "description", "usb", "serial", "class", "subsystem", "node", "decision", "priority",
                    "reason"}
DEVICE_MATCHERS = ("usb", "serial", "class", "subsystem", "node")
RULE_USB = re.compile(r"[0-9a-f]{4}:(?:[0-9a-f]{4}|\*)")
RULE_SUBSYSTEM = re.compile(r"[a-z0-9_-]{1,32}")
RULE_SERIAL = re.compile(r"[!-~]{1,126}")
RULE_NODE = re.compile(r"/dev/[!-~]{1,1000}|cdi:[!-~]{1,200}")
# USB class codes (bDeviceClass, bInterfaceClass) by the names rules may use; two hex digits work as well.
USB_CLASSES = {"audio": "01", "cdc": "02", "hid": "03", "physical": "05", "image": "06", "printer": "07",
               "mass-storage": "08", "hub": "09", "cdc-data": "0a", "smart-card": "0b", "security": "0d",
               "video": "0e", "healthcare": "0f", "audio-video": "10", "billboard": "11", "diagnostic": "dc",
               "wireless": "e0", "misc": "ef", "application": "fe", "vendor": "ff"}
UNTRUSTED_KEYS = {"id", "description", "host", "path", "server", "agent", "tool"}
TOOL_RULE_KEYS = {"id", "description", "agent", "tool", "args", "decision", "priority", "reason", "when"}
RULE_AGENT = re.compile(r"\*|claude|codex|opencode")
CONDITION_KEYS = {"type", "const", "enum", "pattern", "glob", "path_glob", "minLength", "maxLength", "minimum",
                  "maximum", "items", "properties", "required", "additionalProperties", "not", "anyOf", "allOf",
                  "optional"}
JSON_TYPES = ("string", "number", "integer", "boolean", "object", "array", "null")
RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
RULE_HOST = re.compile(r"\*|(?:\*\.)?(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)*[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
RULE_SERVER = re.compile(r"\*|[A-Za-z0-9_-]{1,32}")
RULE_TOOL = re.compile(r"[A-Za-z0-9_.:/*-]{1,128}")
TOOL_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")  # what MCP servers name tools (spec: A-Z a-z 0-9 _ - .)
RULE_LIMIT = 2000             # rules in one ruleset
PATTERN_INPUT_LIMIT = 100000  # characters a pattern is tried on: a longer value is unknown
RULE_BODY_LIMIT = 1 << 20     # request body read for body conditions: a larger one is unknown
# Headers that some frameworks run as the method or the path instead of the request line's.
METHOD_OVERRIDES = ("x-http-method-override", "x-http-method", "x-method-override")
PATH_OVERRIDES = ("x-original-url", "x-rewrite-url")


class RuleError(ValueError):
    """A ruleset that cannot be used; the message names the ruleset, the rule and the key."""


class Unknown:
    """MISSING (not there) and AMBIGUOUS (there, but not one value that every server reads alike)."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


MISSING, AMBIGUOUS = Unknown("MISSING"), Unknown("AMBIGUOUS")


class Ruleset:
    """The rulesets of a run, in order (the administrator's first), compiled for the proxy and the MCP relay."""

    def __init__(self, sources=(), pinned=None, providers=()):
        """SOURCES: [{"name", "version": 1, ...}], each checked again here (it comes from the launcher, but a
        rule that does not compile must stop the proxy, not pass). PINNED: {host: [addresses]} of the run, so
        that the rules of a host also hold for a request to its bare address."""
        self.sources = []
        for source in sources:
            name = str(source.get("name") or "rules")
            checked = self.validated({key: value for key, value in source.items() if key != "name"}, name)
            for section in RULE_SECTIONS:
                # sorted() is stable: file order decides among rules of equal priority and decision.
                checked[section] = sorted(checked[section], key=lambda rule: (
                    rule["priority"], -RULE_DECISIONS.index(rule["decision"])))
            self.sources.append({"name": name, **checked})
        # The agent's own provider (its model API): never a source of untrusted content, never held by a rule
        # for tainted runs. Known by its verified name only, never by a Host header the agent sends.
        self.providers = [self.name(host) for host in providers]
        self.pinned = {}
        for host, addresses in (pinned or {}).items():
            with contextlib.suppress(ValueError, TypeError):
                self.pinned[self.name(host)] = {str(ipaddress.ip_address(str(a))) for a in addresses}
        for source in self.sources:
            for rule in source["http"] + self.http_sources(source):
                rule["addresses"] = set() if "*" in rule["host"] else {
                    address for host, values in self.pinned.items() if self.host(rule["host"], [host])
                    for address in values}

    # -- checking a ruleset --

    @classmethod
    def validated(cls, document, name):
        """DOCUMENT (a ruleset, version 1) checked and in its normal form; RuleError names NAME and the rule."""
        def fail(message):
            raise RuleError(f"{name}: {message}")
        if not isinstance(document, dict):
            fail('a ruleset is a JSON object with "version": 1')
        unknown = sorted(set(document) - RULESET_KEYS)
        if unknown:
            fail(f"unknown key {unknown[0]!r} (a ruleset has {', '.join(sorted(RULESET_KEYS))})")
        if document.get("version") != 1 or isinstance(document.get("version"), bool):
            fail('"version": 1 is required')
        if not isinstance(document.get("description", ""), str):
            fail('"description" is text')
        defaults = document.get("defaults", {})
        if not isinstance(defaults, dict) or set(defaults) - set(RULE_SECTIONS) or \
                any(value not in RULE_DECISIONS for value in defaults.values()):
            fail('"defaults" takes "http", "mcp", "devices" and "tool", each "allow", "ask" or "deny"')
        result = {"version": 1, "defaults": {key: defaults.get(key, "allow") for key in RULE_SECTIONS},
                  "http": [], "mcp": [], "devices": [], "tool": [], "untrusted": []}
        if "canary" in document:
            canary = document["canary"]
            if not isinstance(canary, dict) or not canary or set(canary) - {"on_read", "on_send"} or \
                    canary.get("on_read", "report") not in CANARY_ON_READ or \
                    canary.get("on_send", "block") not in CANARY_ON_SEND:
                fail(f'"canary" takes "on_read" ({", ".join(CANARY_ON_READ)}) and "on_send" '
                     f'({", ".join(CANARY_ON_SEND)})')
            result["canary"] = dict(canary)
        seen = set()
        untrusted = document.get("untrusted", [])
        if not isinstance(untrusted, list) or len(untrusted) > RULE_LIMIT:
            fail(f'"untrusted" is a list of up to {RULE_LIMIT} sources')
        for index, source in enumerate(untrusted, 1):
            ident = source.get("id") if isinstance(source, dict) else None
            where = f"untrusted source {index}" + (f" ({ident})" if isinstance(ident, str) and
                                                   RULE_ID.fullmatch(ident) else "")
            try:
                checked = cls.untrusted_source(source)
            except RuleError as exc:
                fail(f"{where}: {exc}")
            if checked["id"] in seen:
                fail(f"{where}: the id {checked['id']} is used twice")
            seen.add(checked["id"])
            result["untrusted"].append(checked)
        for section, check in (("http", cls.http_rule), ("mcp", cls.mcp_rule), ("devices", cls.device_rule),
                               ("tool", cls.tool_rule)):
            rules = document.get(section, [])
            if not isinstance(rules, list):
                fail(f'"{section}" is a list of rules')
            if len(rules) > RULE_LIMIT:
                fail(f'"{section}" has more than {RULE_LIMIT} rules')
            for index, rule in enumerate(rules, 1):
                ident = rule.get("id") if isinstance(rule, dict) else None
                kind = "device" if section == "devices" else section
                where = f"{kind} rule {index}" + (f" ({ident})" if isinstance(ident, str) and
                                                  RULE_ID.fullmatch(ident) else "")
                try:
                    checked = check(rule)
                except RuleError as exc:
                    fail(f"{where}: {exc}")
                if checked["id"] in seen:
                    fail(f"{where}: the id {checked['id']} is used twice")
                seen.add(checked["id"])
                result[section].append(checked)
        return result

    @staticmethod
    def rule(rule, keys):
        """What every rule has: id, decision, priority, reason."""
        if not isinstance(rule, dict):
            raise RuleError("a rule is a JSON object")
        unknown = sorted(set(rule) - keys)
        if unknown:
            raise RuleError(f"unknown key {unknown[0]!r} (a rule has {', '.join(sorted(keys))})")
        ident = rule.get("id")
        if not isinstance(ident, str) or not RULE_ID.fullmatch(ident):
            raise RuleError('"id" is required: up to 64 letters, digits, ".", "_", ":" or "-", starting with a '
                            'letter or digit')
        if rule.get("decision") not in RULE_DECISIONS:
            raise RuleError('"decision" is required: "allow", "ask" or "deny"')
        priority = rule.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000000:
            raise RuleError('"priority" is a whole number from 0 to 1000000 (default 100; lower comes first)')
        reason = rule.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason or len(reason) > 500
                                   or not reason.isprintable()):
            raise RuleError('"reason" is one line of at most 500 characters')
        if not isinstance(rule.get("description", ""), str):
            raise RuleError('"description" is text')
        when = rule.get("when")
        if when is not None and when != {"tainted": True}:
            raise RuleError('"when" takes only {"tainted": true}: the rule holds once untrusted content entered '
                            'the run')
        return {"id": ident, "decision": rule["decision"], "priority": priority, **({"reason": reason} if reason else {}),
                **({"when": {"tainted": True}} if when else {})}

    @classmethod
    def untrusted_source(cls, source):
        """A source of untrusted content: responses of hosts ("host", "path"), results of MCP tools ("server",
        "tool") or of the agent's own tools ("agent", "tool": seen by its hooks). The agent's own provider is never
        one."""
        if not isinstance(source, dict):
            raise RuleError("a source is a JSON object")
        unknown = sorted(set(source) - UNTRUSTED_KEYS)
        if unknown:
            raise RuleError(f"unknown key {unknown[0]!r} (a source has {', '.join(sorted(UNTRUSTED_KEYS))})")
        ident = source.get("id")
        if not isinstance(ident, str) or not RULE_ID.fullmatch(ident):
            raise RuleError('"id" is required: up to 64 letters, digits, ".", "_", ":" or "-", starting with a '
                            'letter or digit')
        if ("host" in source) == ("tool" in source):
            raise RuleError('a source names either "host" (responses of hosts) or "tool" (results of MCP tools, '
                            'or with "agent" of the agent\'s own tools)')
        if "agent" in source:
            if "server" in source or "path" in source:
                raise RuleError('"agent" names the agent\'s own tools: not with "server" or "path"')
            rule = cls.tool_rule({"id": ident, "agent": source["agent"], "tool": source["tool"], "decision": "deny"})
            return {"id": ident, "agent": rule["agent"], "tool": rule["tool"]}
        if "host" in source:
            if "server" in source:
                raise RuleError('"server" belongs to a source with "tool"')
            rule = cls.http_rule({"id": ident, "host": source["host"], "path": source.get("path"), "decision": "deny"})
            return {"id": ident, "host": rule["host"], "path": rule["path"]}
        if "path" in source:
            raise RuleError('"path" belongs to a source with "host"')
        rule = cls.mcp_rule({"id": ident, "server": source.get("server"), "tool": source["tool"], "decision": "deny"})
        return {"id": ident, "server": rule["server"], "tool": rule["tool"]}

    @staticmethod
    def strings(value, key, pattern, what, default=None):
        """VALUE (a string or a list of up to 100) as a list, each matching PATTERN (WHAT says what that is)."""
        if value is None and default is not None:
            value = default
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not values or len(values) > 100:
            raise RuleError(f'"{key}" is {what}, or a list of up to 100')
        for item in values:
            if not isinstance(item, str) or not pattern.fullmatch(item):
                raise RuleError(f'"{key}" {item!r}: not {what}')
        return list(dict.fromkeys(values))

    @classmethod
    def http_rule(cls, rule):
        checked = cls.rule(rule, HTTP_RULE_KEYS)
        if "host" not in rule:
            raise RuleError('"host" is required: a name, *.DOMAIN, an address, or * for every host')
        hosts = []
        what = "a host name, *.DOMAIN, an address, or * for every host"
        for value in cls.strings(rule["host"], "host", re.compile(r"[!-~]{1,253}"), what):
            host = cls.name(value)
            if not RULE_HOST.fullmatch(host) and not cls.is_address(host):
                raise RuleError(f'"host" {value!r}: not {what}')
            hosts.append(host)
        methods = [m.upper() for m in cls.strings(rule.get("methods"), "methods", re.compile(r"\*|[A-Za-z]{1,20}"),
                                                  "a method such as GET, or *", "*")]
        paths = []
        for value in cls.strings(rule.get("path"), "path", re.compile(r"/[!-~]{0,1000}"),
                                 "a path pattern starting with / (printable ASCII)", "/**"):
            if "?" in value or "#" in value:
                raise RuleError(f'"path" {value!r}: a path has no query (use "query") and no fragment')
            # As the requests are compared: decoded, dot segments and repeated slashes resolved, no trailing /.
            paths.append(normal_path(value) or "/")
        checked.update(host=hosts, methods=["*"] if "*" in methods else list(dict.fromkeys(methods)),
                       path=list(dict.fromkeys(paths)))
        if "query" in rule:
            checked["query"] = cls.conditions(rule["query"], "query")
        if "headers" in rule:
            checked["headers"] = cls.conditions(rule["headers"], "headers", lower=True)
            if any(not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]{1,100}", name) for name in checked["headers"]):
                raise RuleError('"headers": header names are tokens such as content-type')
        if "body" in rule:
            checked["body"] = cls.condition(rule["body"], "body")
        return checked

    @classmethod
    def mcp_rule(cls, rule):
        checked = cls.rule(rule, MCP_RULE_KEYS)
        if "tool" not in rule:
            raise RuleError('"tool" is required: a tool name, a pattern with *, or a list of them')
        checked.update(server=cls.strings(rule.get("server"), "server", RULE_SERVER, "an MCP server name, or *", "*"),
                       tool=cls.strings(rule["tool"], "tool", RULE_TOOL,
                                        "a tool name (A-Z a-z 0-9 _ - . : /, * as a wildcard)"))
        if "args" in rule:
            checked["args"] = cls.conditions(rule["args"], "args")
        return checked

    @classmethod
    def device_rule(cls, rule):
        """A device rule: what it matches (USB vendor:product, serial, USB class, kernel subsystem, node path), all
        of the given ones at once; each a value or a list of up to 100, any of which may match."""
        checked = cls.rule(rule, DEVICE_RULE_KEYS)
        if not any(key in rule for key in DEVICE_MATCHERS):
            raise RuleError('a device rule needs "usb", "serial", "class", "subsystem" or "node" (for every device: '
                            '"defaults": {"devices": ...})')
        if "usb" in rule:
            value = rule["usb"]
            lowered = value.lower() if isinstance(value, str) else \
                [v.lower() if isinstance(v, str) else v for v in value] if isinstance(value, list) else value
            checked["usb"] = cls.strings(lowered, "usb", RULE_USB, "VENDOR:PRODUCT in hex (2e8a:000a), or VENDOR:*")
        if "serial" in rule:
            checked["serial"] = cls.strings(rule["serial"], "serial", RULE_SERIAL,
                                            "a serial number or a pattern with * (printable ASCII, no spaces)")
        if "class" in rule:
            names = cls.strings(rule["class"], "class", re.compile(r"[0-9A-Fa-f]{2}|[a-z-]{2,20}"),
                                "a USB class (" + ", ".join(USB_CLASSES) + ") or two hex digits")
            for name in names:
                if not re.fullmatch(r"[0-9A-Fa-f]{2}", name) and name not in USB_CLASSES:
                    raise RuleError(f'"class" {name!r}: not a USB class (' + ", ".join(USB_CLASSES) + ")")
            checked["class"] = list(dict.fromkeys(USB_CLASSES.get(name, name.lower()) for name in names))
        if "subsystem" in rule:
            checked["subsystem"] = cls.strings(rule["subsystem"], "subsystem", RULE_SUBSYSTEM,
                                               "a kernel subsystem such as tty, usb, video4linux, sound or drm")
        if "node" in rule:
            nodes = cls.strings(rule["node"], "node", RULE_NODE, "a device path pattern under /dev (* within a "
                                "segment, ** across), or cdi:NAME")
            for node in nodes:
                if node.startswith("/dev/") and (posixpath.normpath(node) != node or "//" in node):
                    raise RuleError(f'"node" {node!r}: a path without . or .. segments and repeated slashes')
            checked["node"] = nodes
        return checked

    @classmethod
    def tool_rule(cls, rule):
        """A rule for the agent's own tools (through its hooks): agent, tool, args."""
        checked = cls.rule(rule, TOOL_RULE_KEYS)
        if "tool" not in rule:
            raise RuleError('"tool" is required: a tool name such as Bash, Read or WebFetch, a pattern with *, or a '
                            'list of them')
        checked.update(agent=cls.strings(rule.get("agent"), "agent", RULE_AGENT, "claude, codex, opencode, or *", "*"),
                       tool=cls.strings(rule["tool"], "tool", RULE_TOOL,
                                        "a tool name (A-Z a-z 0-9 _ - . : /, * as a wildcard)"))
        if "args" in rule:
            checked["args"] = cls.conditions(rule["args"], "args")
        return checked

    @classmethod
    def conditions(cls, spec, where, depth=0, lower=False):
        """{NAME: condition} of arguments, query parameters, headers or properties."""
        if not isinstance(spec, dict) or len(spec) > 200:
            raise RuleError(f'"{where}" is an object of NAME: condition (at most 200)')
        result = {}
        for name, condition in spec.items():
            if not name or len(name) > 200:
                raise RuleError(f'"{where}": names have 1 to 200 characters')
            result[name.lower() if lower else name] = cls.condition(condition, f"{where}.{name}", depth)
        return result

    @classmethod
    def condition(cls, spec, where, depth=0):
        """A condition (a subset of JSON Schema, see the README) checked and in its normal form."""
        if not isinstance(spec, dict):
            raise RuleError(f'"{where}": a condition is a JSON object such as {{"pattern": "..."}}')
        if depth > 16:
            raise RuleError(f'"{where}": conditions nest at most 16 deep')
        unknown = sorted(set(spec) - CONDITION_KEYS)
        if unknown:
            raise RuleError(f'"{where}": unknown keyword {unknown[0]!r} (known: {", ".join(sorted(CONDITION_KEYS))})')
        result = {}
        for key, value in spec.items():
            here = f'"{where}.{key}"'
            number = isinstance(value, (int, float)) and not isinstance(value, bool)
            if key == "type":
                names = [value] if isinstance(value, str) else value
                if not isinstance(names, list) or not names or any(n not in JSON_TYPES for n in names):
                    raise RuleError(f"{here}: one of {', '.join(JSON_TYPES)} or a list of them")
                result[key] = list(names)
            elif key == "const":
                result[key] = value
            elif key == "enum":
                if not isinstance(value, list) or not value or len(value) > 1000:
                    raise RuleError(f"{here}: a list of 1 to 1000 values")
                result[key] = value
            elif key == "pattern":
                if not isinstance(value, str) or len(value) > 1000:
                    raise RuleError(f"{here}: a regular expression of at most 1000 characters")
                try:
                    re.compile(value)
                except re.error as exc:
                    raise RuleError(f"{here}: {exc}") from None
                result[key] = value
            elif key in ("glob", "path_glob"):
                values = [value] if isinstance(value, str) else value
                if not isinstance(values, list) or not values or len(values) > 100 or \
                        not all(isinstance(v, str) and 0 < len(v) <= 1000 for v in values):
                    raise RuleError(f"{here}: a pattern or a list of up to 100")
                if key == "path_glob" and not all(v.startswith("/") for v in values):
                    raise RuleError(f"{here}: absolute paths such as /work/src/**")
                result[key] = list(values)
            elif key in ("minLength", "maxLength"):
                if not number or not isinstance(value, int) or value < 0:
                    raise RuleError(f"{here}: a whole number of 0 or more")
                result[key] = value
            elif key in ("minimum", "maximum"):
                if not number or value != value:
                    raise RuleError(f"{here}: a number")
                result[key] = value
            elif key in ("items", "not"):
                result[key] = cls.condition(value, f"{where}.{key}", depth + 1)
            elif key in ("anyOf", "allOf"):
                if not isinstance(value, list) or not value or len(value) > 100:
                    raise RuleError(f"{here}: a list of 1 to 100 conditions")
                result[key] = [cls.condition(v, f"{where}.{key}", depth + 1) for v in value]
            elif key == "properties":
                result[key] = cls.conditions(value, f"{where}.properties", depth + 1)
            elif key == "required":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise RuleError(f"{here}: a list of names")
                result[key] = value
            elif key == "additionalProperties":
                if value is not False:
                    raise RuleError(f"{here}: only false (no other names than under properties)")
                result[key] = False
            elif key == "optional":
                if not isinstance(value, bool):
                    raise RuleError(f"{here}: true or false")
                result[key] = value
        return result

    # -- values --

    @staticmethod
    def parse_json(text):
        """(value, duplicated) of the JSON text TEXT: a key given twice holds AMBIGUOUS. ValueError for anything
        but exactly one JSON value, NaN and Infinity included (other parsers refuse them)."""
        duplicated = []

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    duplicated.append(key)
                    value = AMBIGUOUS
                result[key] = value
            return result

        def constant(name):
            raise ValueError(f"{name} is not JSON")
        try:
            return json.loads(text, object_pairs_hook=pairs, parse_constant=constant), bool(duplicated)
        except RecursionError:
            raise ValueError("JSON nested too deeply") from None

    @staticmethod
    def member(value, name):
        """VALUE[NAME], MISSING, or AMBIGUOUS when NAME is given twice or also in another case (Go's
        encoding/json and ASP.NET read names regardless of case) or VALUE is no object."""
        if not isinstance(value, dict):
            return AMBIGUOUS
        folded = name.casefold()
        if any(isinstance(key, str) and key != name and key.casefold() == folded for key in value):
            return AMBIGUOUS
        return value.get(name, MISSING)

    @staticmethod
    def json_type(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "integer" if value.is_integer() else "number"
        return {str: "string", list: "array", dict: "object"}.get(type(value))

    @classmethod
    def same(cls, value, expected):
        """VALUE equals the rule's EXPECTED as JSON (true is not 1); None where VALUE holds AMBIGUOUS."""
        if value is AMBIGUOUS:
            return None
        kind, wanted = cls.json_type(value), cls.json_type(expected)
        if kind in ("integer", "number") and wanted in ("integer", "number"):
            return value == expected
        if kind != wanted:
            return False
        if kind == "array":
            return len(value) == len(expected) and cls.all_of(cls.same(a, b) for a, b in zip(value, expected))
        if kind == "object":
            return set(value) == set(expected) and cls.all_of(cls.same(value[k], expected[k]) for k in value)
        return value == expected

    @staticmethod
    def all_of(outcomes):
        """Kleene AND: False if one is False, else None if one is None, else True."""
        result = True
        for outcome in outcomes:
            if outcome is False:
                return False
            if outcome is None:
                result = None
        return result

    @staticmethod
    def any_of(outcomes):
        """Kleene OR: True if one is True, else None if one is None, else False."""
        result = False
        for outcome in outcomes:
            if outcome is True:
                return True
            if outcome is None:
                result = None
        return result

    @staticmethod
    def every(outcomes):
        """For several readings of one request (methods, paths): True if all match, False if none, else None."""
        outcomes = list(outcomes)
        if all(outcome is True for outcome in outcomes):
            return True
        return False if all(outcome is False for outcome in outcomes) else None

    @classmethod
    def holds(cls, condition, value):
        """True, False or None (unknown): whether VALUE (MISSING, AMBIGUOUS or a JSON value) meets CONDITION."""
        if value is MISSING:
            return bool(condition.get("optional"))  # never a skipped condition: missing fails unless optional
        keys = [key for key in condition if key != "optional"]
        if value is AMBIGUOUS:
            return None if keys else True
        return cls.all_of(cls.keyword(key, condition[key], value, condition) for key in keys)

    @classmethod
    def keyword(cls, key, expected, value, condition):
        kind = cls.json_type(value)
        if key in ("type", "glob", "path_glob") and isinstance(expected, str):
            expected = [expected]  # the normal form has lists; a condition written by hand may not
        if key == "type":
            return kind in expected or (kind == "integer" and "number" in expected)
        if key == "const":
            return cls.same(value, expected)
        if key == "enum":
            return cls.any_of(cls.same(value, option) for option in expected)
        if key == "not":
            outcome = cls.holds(expected, value)
            return None if outcome is None else not outcome
        if key == "anyOf":
            return cls.any_of(cls.holds(option, value) for option in expected)
        if key == "allOf":
            return cls.all_of(cls.holds(option, value) for option in expected)
        # A keyword for another type than the value's is unknown, not passed: a list where a deny rule expects
        # a string must not slip by it, and must not meet an allow rule either.
        if key in ("pattern", "glob", "path_glob", "minLength", "maxLength"):
            if kind != "string":
                return None
            if key == "minLength":
                return len(value) >= expected
            if key == "maxLength":
                return len(value) <= expected
            if key == "glob":
                return any(cls.glob(pattern, value) for pattern in expected)
            if key == "path_glob":
                # Relative paths depend on the server's working directory, which no rule knows.
                if not value.startswith("/") or "\x00" in value:
                    return None
                path = posixpath.normpath(re.sub(r"/+", "/", value))
                return any(path_matches(pattern, path) for pattern in expected)
            if len(value) > PATTERN_INPUT_LIMIT:
                return None
            plain = re.fullmatch(expected, value) is not None
            if "\n" not in value:
                return plain
            # Whether "." takes a line break decides: unknown (fullmatch with and without re.S).
            return plain if plain == (re.fullmatch(expected, value, re.S) is not None) else None
        if key in ("minimum", "maximum"):
            if kind not in ("integer", "number"):
                return None
            return value >= expected if key == "minimum" else value <= expected
        if key == "items":
            return cls.all_of(cls.holds(expected, item) for item in value) if kind == "array" else None
        if kind != "object":
            return None
        if key == "properties":
            return cls.all_of(cls.holds(sub, cls.member(value, name)) for name, sub in expected.items())
        if key == "required":
            return cls.all_of(False if cls.member(value, name) is MISSING else None
                              if cls.member(value, name) is AMBIGUOUS else True for name in expected)
        allowed = set(condition.get("properties", {}))  # additionalProperties: false
        return all(name in allowed for name in value)

    @staticmethod
    def glob(pattern, value, ignore_case=False):
        """VALUE matches PATTERN, where * stands for any characters (/ and line breaks included)."""
        expression = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", pattern))
        return re.fullmatch(expression, value, re.S | (re.I if ignore_case else 0)) is not None

    @classmethod
    def tool(cls, patterns, name):
        """Whether the tool NAME is one of PATTERNS; None where only the case differs."""
        if any(cls.glob(pattern, name) for pattern in patterns):
            return True
        return None if any(cls.glob(pattern, name, ignore_case=True) for pattern in patterns) else False

    @staticmethod
    def is_address(value):
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    @classmethod
    def name(cls, value):
        """A host name or address as rules compare it: lower case, no trailing dots, addresses in one form."""
        value = str(value).strip().lower().rstrip(".")
        bare = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        return str(ipaddress.ip_address(bare)) if cls.is_address(bare) else value

    @staticmethod
    def host(patterns, names):
        return any(pattern == "*" or name == pattern or (pattern.startswith("*.") and name.endswith(pattern[1:]))
                   for pattern in patterns for name in names)

    # -- HTTP --

    @property
    def http_active(self):
        return any(source["http"] or source["defaults"]["http"] != "allow" or self.http_sources(source)
                   for source in self.sources)

    @staticmethod
    def http_sources(source):
        return [entry for entry in source.get("untrusted", []) if "host" in entry]

    def provider(self, facts):
        """The request FACTS go to the agent's own provider: the verified server name is one of its hosts and no
        other name is claimed (a Host header naming the provider at another server proves nothing)."""
        return not facts["literal"] and facts["claimed"] in (None, facts["server"]) and \
            self.host(self.providers, [facts["server"]])

    def untrusted_http(self, facts, any_path=False):
        """{"layer", "rule", "ruleset", "what"} of the first source that marks the response to the request FACTS
        untrusted, or None (never the agent's provider). ANY_PATH: what is not HTTP, or not opened, matches a
        source by its host alone (its content is unseen, so unknown)."""
        if self.provider(facts):
            return None
        for source in self.sources:
            for entry in self.http_sources(source):
                if (self.host(entry["host"], facts["names"]) or facts["address"] in entry["addresses"]) and (any_path or any(
                        path_matches(pattern, path, ignore_case=True) for pattern in entry["path"]
                        for path in facts["paths"])):
                    return {"layer": "proxy", "rule": entry["id"], "ruleset": source["name"],
                            "what": facts["claimed"] or facts["server"]}
        return None

    def untrusted_tool(self, server, tool):
        """The source that marks the results of TOOL on SERVER untrusted, or None (case variants count)."""
        for source in self.sources:
            for entry in source.get("untrusted", []):
                if "server" in entry and ("*" in entry["server"] or server in entry["server"]) and \
                        self.tool(entry["tool"], tool) is not False:
                    return {"layer": "mcp", "rule": entry["id"], "ruleset": source["name"],
                            "what": f"the MCP tool {tool} of {server}"}
        return None

    def mcp_taint_active(self):
        """Some rule holds only in tainted runs, or some MCP tool is a source: the per-run relay is needed."""
        return any(any("server" in entry for entry in source.get("untrusted", [])) or
                   any(rule.get("when") for rule in source["mcp"]) for source in self.sources)

    def untrusted_agent_tool(self, agent, tool):
        """The source that marks the results of the agent's own TOOL untrusted (seen by its hooks), or None."""
        for source in self.sources:
            for entry in source.get("untrusted", []):
                if "agent" in entry and ("*" in entry["agent"] or agent in entry["agent"]) and \
                        self.tool(entry["tool"], tool) is not False:
                    return {"layer": "hook", "rule": entry["id"], "ruleset": source["name"],
                            "what": f"the tool {tool} of {agent}"}
        return None

    def pins(self, address):
        """ADDRESS is pinned for a host that has HTTP rules: only HTTP the proxy can check may reach it."""
        address = self.name(address)
        return any(address in rule["addresses"] for source in self.sources for rule in source["http"])

    @classmethod
    def request(cls, origin, method, target, headers, address=None):
        """What HTTP rules judge of a request: every name it could reach (the verified server name ORIGIN and
        the Host header), the destination ADDRESS, each method and path a server might run (override headers
        and _method included), the query and the headers."""
        literal = is_address_origin(origin)
        server = cls.name(origin.rsplit(":", 1)[0] if literal else origin)
        claimed = host_name(header(headers, "host"))
        claimed = cls.name(claimed) if claimed else None
        address = cls.name(address) if address else (server if literal else None)
        names = {name for name in (None if literal else server, claimed, address) if name}
        query = cls.query(target)
        fields = {}
        for name, value in headers:
            fields[name.lower()] = AMBIGUOUS if name.lower() in fields else value
        methods = [method.upper()] + [value.strip().upper() for name, value in headers
                                      if name.lower() in METHOD_OVERRIDES]
        override = cls.member(query, "_method")
        if override is not MISSING:
            methods.append(override.upper() if isinstance(override, str) else "\0")
        paths = [normal_path(target)] + [normal_path(value.strip()) for name, value in headers
                                         if name.lower() in PATH_OVERRIDES]
        paths = list(dict.fromkeys(reading for path in paths for reading in cls.readings(path)))
        ambiguous = ambiguous_path(target) or any(ambiguous_path(value.strip()) for name, value in headers
                                                  if name.lower() in PATH_OVERRIDES)
        return {"server": server, "claimed": claimed, "literal": literal, "names": names, "address": address,
                "methods": methods, "paths": paths, "query": query, "headers": fields, "body": AMBIGUOUS,
                "ambiguous": ambiguous}

    @staticmethod
    def readings(path):
        """PATH (normalised) and how lenient servers may also read it: cut at a control character (C code ends
        strings at NUL), and without dots and spaces at the end of a segment (IIS)."""
        cut = re.split(r"[\x00-\x1f\x7f]", path, maxsplit=1)[0]
        trimmed = normal_path("/".join(segment.rstrip(". ") for segment in path.split("/")))
        return list(dict.fromkeys([path, cut, trimmed]))

    @staticmethod
    def query(target):
        """The query of TARGET as {name: value}; AMBIGUOUS for a name given twice, and for the whole query when
        servers could split it differently (on ;) or decode it differently (not UTF-8)."""
        if "?" not in target:
            return {}
        raw = target.split("?", 1)[1]
        if ";" in raw:
            return AMBIGUOUS
        try:
            pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return AMBIGUOUS
        result = {}
        for name, value in pairs:
            result[name] = AMBIGUOUS if name in result else value
        return result

    @classmethod
    def body(cls, data, headers):
        """A request body as body conditions see it: JSON whatever the declared type (some servers parse JSON
        regardless), else a form when declared as one; MISSING when empty, AMBIGUOUS when it cannot be read."""
        if not data:
            return MISSING
        encodings = [value.strip().lower() for name, value in headers if name.lower() == "content-encoding"]
        if encodings and encodings not in (["gzip"], ["x-gzip"], ["deflate"], ["identity"]):
            return AMBIGUOUS  # br, zstd, several layers: not read here, so not judged
        if encodings and encodings != ["identity"]:
            try:
                inflater = zlib.decompressobj(47)
                data = inflater.decompress(data, RULE_BODY_LIMIT + 1)
            except zlib.error:
                return AMBIGUOUS
            if len(data) > RULE_BODY_LIMIT or inflater.unconsumed_tail or not inflater.eof:
                return AMBIGUOUS
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return AMBIGUOUS
        with contextlib.suppress(ValueError):
            return cls.parse_json(text)[0]
        types = [value.split(";")[0].strip().lower() for name, value in headers if name.lower() == "content-type"]
        if types == ["application/x-www-form-urlencoded"]:
            return cls.query("?" + text)
        return AMBIGUOUS

    def needs_body(self, facts):
        """A rule with a body condition may decide the request FACTS: the proxy reads the body first."""
        return any("body" in rule and self.applies(rule, facts) and self.route(rule, facts, facts["methods"])
                   is not False for source in self.sources for rule in source["http"])

    def applies(self, rule, facts):
        return self.host(rule["host"], facts["names"]) or facts["address"] in rule["addresses"]

    def route(self, rule, facts, methods):
        """Method and path of the request FACTS against RULE (True, False or None). A path that servers read
        differently (ambiguous_path) is never known to match an allow rule for some paths only: None."""
        if facts.get("ambiguous") and rule["decision"] == "allow" and \
                not all(pattern in ("/**", "**") for pattern in rule["path"]):
            return None if self.every("*" in rule["methods"] or method in rule["methods"] for method in methods) \
                is not False else False
        return self.all_of([
            self.every("*" in rule["methods"] or method in rule["methods"] for method in methods),
            self.every(self.any_of(True if path_matches(pattern, path) else None
                                   if path_matches(pattern, path, ignore_case=True) else False
                                   for pattern in rule["path"]) for path in facts["paths"])])

    def http_match(self, rule, facts, methods):
        if rule.get("when") and (not facts.get("taint") or self.provider(facts)):
            return False
        if not self.applies(rule, facts):
            return False
        outcomes = [self.route(rule, facts, methods)]
        for field in ("query", "headers"):
            if field in rule:
                outcomes.append(self.all_of(self.holds(condition, self.member(facts[field], name))
                                            for name, condition in rule[field].items()))
        if "body" in rule:
            outcomes.append(self.holds(rule["body"], facts["body"]))
        return self.all_of(outcomes)

    def http(self, facts):
        """{"decision", "rule", "ruleset", "reason"} for the request FACTS (see request); rule None when no
        ruleset has a say, "(unlisted)" or "(default)" when a ruleset decided without a rule."""
        methods = list(facts["methods"])
        if isinstance(facts["body"], dict):
            override = self.member(facts["body"], "_method")  # Rails, Laravel: a form field
            if override is not MISSING:
                methods.append(override.upper() if isinstance(override, str) else "\0")
        method, path = facts["methods"][0], facts["paths"][0] or "/"
        shown_host = facts["claimed"] or facts["server"]
        applying = [source["name"] for source in self.sources if any(
            "*" not in rule["host"] and self.applies(rule, facts) for rule in source["http"])]
        if applying and facts["claimed"] and not facts["literal"] and facts["claimed"] != facts["server"]:
            # Servers route by Host and one certificate covers many names: the rules of the name verified
            # and of the name claimed could differ, so neither is trusted.
            return {"decision": "deny", "rule": "(host)", "ruleset": applying[0],
                    "reason": f"the Host header {facts['claimed']} differs from the server name {facts['server']} "
                              f"({', '.join(applying)})"}
        best = None
        for source in self.sources:
            verdict = None
            for rule in source["http"]:
                outcome = self.http_match(rule, facts, methods)
                if outcome or (outcome is None and rule["decision"] != "allow"):
                    verdict = {"decision": rule["decision"], "rule": rule["id"], "ruleset": source["name"],
                               "reason": rule.get("reason") or f"{method} {path} on {shown_host} is "
                               f"{DECIDED[rule['decision']]} "
                               f"by rule {rule['id']} ({source['name']})"}
                    if rule.get("when"):
                        verdict["reason"] += f": {Taint.because(facts['taint'])}"
                        verdict["taint"] = facts["taint"]
                    break
            else:
                if any(rule["decision"] == "allow" and "*" not in rule["host"] and self.applies(rule, facts)
                       for rule in source["http"]):
                    verdict = {"decision": "deny", "rule": "(unlisted)", "ruleset": source["name"],
                               "reason": f"{method} {path} is not among the requests allowed for {shown_host} "
                                         f"({source['name']})"}
                elif source["defaults"]["http"] != "allow":
                    verdict = {"decision": source["defaults"]["http"], "rule": "(default)", "ruleset": source["name"],
                               "reason": f"{method} {path} on {shown_host}: no rule of {source['name']} allows it "
                                         f"(its default is {source['defaults']['http']})"}
            if verdict and (best is None or RULE_DECISIONS.index(verdict["decision"]) >
                            RULE_DECISIONS.index(best["decision"])):
                best = verdict
        return best or {"decision": "allow", "rule": None, "ruleset": None, "reason": None}

    # -- MCP --

    @property
    def mcp_active(self):
        return any(source["mcp"] or source["defaults"]["mcp"] != "allow" for source in self.sources)

    def mcp_asks(self):
        """Some rule or default of this ruleset may ask about a tool call."""
        return any(source["defaults"]["mcp"] == "ask" or any(r["decision"] == "ask" for r in source["mcp"])
                   for source in self.sources)

    @staticmethod
    def for_server(source, server, section="mcp"):
        key = "agent" if section == "tool" else "server"
        return [rule for rule in source[section] if "*" in rule[key] or server in rule[key]]

    # -- the agent's own tools (its hooks) --

    @property
    def tool_active(self):
        """Some rule or default holds for the agent's own tools, or one of them is a source of untrusted content."""
        return any(source["tool"] or source["defaults"]["tool"] != "allow" or
                   any("agent" in entry for entry in source.get("untrusted", [])) for source in self.sources)

    def agent_tool(self, agent, tool, arguments, taint=None):
        """{"decision", "rule", "ruleset", "reason"} for AGENT's call of its own TOOL with ARGUMENTS: as mcp()."""
        return self.mcp(agent, tool, arguments, taint, section="tool")

    def mcp(self, server, tool, arguments, taint=None, section="mcp"):
        """{"decision", "rule", "ruleset", "reason"} for a call of TOOL with ARGUMENTS (an object) on SERVER; TAINT:
        the run's taint state, for the rules that hold only in tainted runs. SECTION "tool": the agent's own
        tools, SERVER the agent."""
        best = None
        tainted = bool(taint and taint.get("tainted"))
        for source in self.sources:
            verdict, rules = None, [rule for rule in self.for_server(source, server, section)
                                    if not rule.get("when") or tainted]
            for rule in rules:
                outcome = self.all_of([self.tool(rule["tool"], tool), *(self.holds(c, self.member(arguments, n))
                                                                        for n, c in rule.get("args", {}).items())])
                if outcome or (outcome is None and rule["decision"] != "allow"):
                    verdict = {"decision": rule["decision"], "rule": rule["id"], "ruleset": source["name"],
                               "reason": rule.get("reason") or f"the tool {tool} of {server} is "
                               f"{DECIDED[rule['decision']]} "
                               f"by rule {rule['id']} ({source['name']})"}
                    if rule.get("when"):
                        verdict["reason"] += f": {Taint.because(taint)}"
                        verdict["taint"] = taint
                    break
            else:
                # A tool with allow rules is listed: calls of it they do not match are refused (a case variant of
                # its name too), as a host with allow rules refuses the requests they do not match.
                if any(rule["decision"] == "allow" and self.tool(rule["tool"], tool) is not False for rule in rules):
                    verdict = {"decision": "deny", "rule": "(unlisted)", "ruleset": source["name"],
                               "reason": f"this call of {tool} is not among the calls of it allowed for {server} "
                                         f"({source['name']})"}
                elif source["defaults"][section] != "allow":
                    verdict = {"decision": source["defaults"][section], "rule": "(default)", "ruleset": source["name"],
                               "reason": f"no rule of {source['name']} allows the tool {tool} of {server} (its "
                                         f"default is {source['defaults'][section]})"}
            if verdict and (best is None or RULE_DECISIONS.index(verdict["decision"]) >
                            RULE_DECISIONS.index(best["decision"])):
                best = verdict
        return best or {"decision": "allow", "rule": None, "ruleset": None, "reason": None}

    def hidden(self, server, tool):
        """True if every call of TOOL on SERVER is denied, whatever its arguments: tools/list leaves it out."""
        for source in self.sources:
            rules, verdict = self.for_server(source, server), None
            for rule in rules:
                if self.tool(rule["tool"], tool) is not True:
                    continue
                conditional = rule.get("args") or rule.get("when")
                if conditional and rule["decision"] == "deny":
                    continue  # denies some calls only
                verdict = "maybe" if conditional else rule["decision"]
                break
            if verdict is None:  # no allow rule names it either (that would have been "maybe")
                verdict = source["defaults"]["mcp"]
            if verdict == "deny":
                return True
        return False

    # -- devices --
    #
    # FACTS of a device node (see devices.identity): "node" (its path), "names" (the path given, the node and the
    # udev links to it), "subsystem", "usb" ("vvvv:pppp", or None when not on USB), "serial", "classes" (the USB
    # device's and its interfaces' class codes) and "unknown" (the fields that could not be read: a rule on one
    # of them is unknown, so allow rules do not match and deny and ask rules do). USB IDs, serials and the udev
    # links made from them are what the device reports about itself: an identification aid, not authentication.

    @property
    def devices_active(self):
        return any(source["devices"] or source["defaults"]["devices"] != "allow" for source in self.sources)

    def device_asks(self):
        return any(source["defaults"]["devices"] == "ask" or any(r["decision"] == "ask" for r in source["devices"])
                   for source in self.sources)

    @classmethod
    def device_field(cls, rule, key, facts):
        """True, False or None: the device FACTS against the rule's KEY."""
        unknown = set(facts.get("unknown") or ())
        if key == "node":
            names = [n for n in facts.get("names") or [facts.get("node")] if isinstance(n, str)]
            return any(path_matches(pattern, name) if pattern.startswith("/") else cls.glob(pattern, name)
                       for pattern in rule["node"] for name in names)
        if key in unknown or (key == "serial" and "usb" in unknown):
            return None
        if key == "usb":
            return isinstance(facts.get("usb"), str) and any(cls.glob(p, facts["usb"]) for p in rule["usb"])
        if key == "serial":
            return isinstance(facts.get("serial"), str) and any(cls.glob(p, facts["serial"]) for p in rule["serial"])
        if key == "class":
            return bool(set(rule["class"]) & set(facts.get("classes") or ()))
        return facts.get("subsystem") in rule["subsystem"]

    def device_match(self, rule, facts):
        return self.all_of(self.device_field(rule, key, facts) for key in DEVICE_MATCHERS if key in rule)

    @staticmethod
    def device_name(facts):
        usb = facts.get("usb")
        return f"{facts.get('node')}" + (f" (usb {usb})" if isinstance(usb, str) else "")

    def device(self, facts):
        """{"decision", "rule", "ruleset", "reason"} for the device FACTS; rule None when no ruleset has a say."""
        best, shown = None, self.device_name(facts)
        for source in self.sources:
            verdict = None
            for rule in source["devices"]:
                outcome = self.device_match(rule, facts)
                if outcome or (outcome is None and rule["decision"] != "allow"):
                    verdict = {"decision": rule["decision"], "rule": rule["id"], "ruleset": source["name"],
                               "reason": rule.get("reason") or f"the device {shown} is {DECIDED[rule['decision']]} "
                                                               f"by rule {rule['id']} ({source['name']})"}
                    break
            else:
                # A vendor:product with allow rules is listed: a device of it they do not match (another serial,
                # another class) is refused, as a tool with allow rules refuses the calls they do not match.
                if any(rule["decision"] == "allow" and "usb" in rule and self.device_field(rule, "usb", facts)
                       is not False for rule in source["devices"]):
                    verdict = {"decision": "deny", "rule": "(unlisted)", "ruleset": source["name"],
                               "reason": f"the USB IDs of the device {shown} cannot be read, and {source['name']} "
                                         "allows devices by their IDs" if "usb" in (facts.get("unknown") or ())
                               else f"the device {shown} is not among the devices of its vendor and product "
                                    f"allowed ({source['name']})"}
                elif source["defaults"]["devices"] != "allow":
                    verdict = {"decision": source["defaults"]["devices"], "rule": "(default)",
                               "ruleset": source["name"],
                               "reason": f"no rule of {source['name']} allows the device {shown} (its default is "
                                         f"{source['defaults']['devices']})"}
            if verdict and (best is None or RULE_DECISIONS.index(verdict["decision"]) >
                            RULE_DECISIONS.index(best["decision"])):
                best = verdict
        return best or {"decision": "allow", "rule": None, "ruleset": None, "reason": None}


# --- agent hooks: rules for the agents' own tools ----------------------------------------------------------
#
# Claude Code, Codex and OpenCode call a hook before and after each of their own tools (Bash, file edits,
# WebFetch, their MCP tools). The launcher installs it as managed configuration on read-only paths, so the agent
# cannot drop it: Claude Code's /etc/claude-code/managed-settings.json (allowManagedHooksOnly), Codex's
# /etc/codex/requirements.toml (allow_managed_hooks_only, the hooks feature pinned on), an OpenCode plugin. The
# hook (hook_client) asks the supervisor over HOOK_SOCKET; the supervisor (HookServer) applies the "tool" rules
# of the rulesets, asks the human where a rule says ask, records untrusted tools in the run's taint and logs each
# call to tools.jsonl.
#
# A guard rail, not a boundary: Bash runs whatever it is given (python -c, a script, base64), and the agent's
# network goes through the proxy anyway. The hook fails closed: whatever goes wrong refuses the call.

HOOK_DIR = Path("/run/sandbox-hooks")
HOOK_SOCKET = HOOK_DIR / "decide.sock"
HOOK_LIMIT = 4 << 20          # one hook message to the supervisor
HOOK_VALUE_LIMIT = 256 << 10  # a longer string of a tool's input is unknown to the rules (never matched, never cut)
HOOK_READ_LIMIT = 64 << 20    # what the client reads of the agent's message
HOOK_TIMEOUT = 110            # seconds the client waits: below the agents' own hook timeout (120, see the launcher)
HOOK_EVENTS = ("PreToolUse", "PostToolUse")
HOOK_AGENTS = ("claude", "codex", "opencode")
# The first Codex that reads hooks and allow_managed_hooks_only from requirements.toml (rust-v0.131.0); an older
# one ignores both without a word, so its run is refused rather than started without the rules.
CODEX_HOOKS_VERSION = (0, 131, 0)
# OpenCode's tool names as Claude Code and Codex name the same tools, so that one rule holds for all three.
OPENCODE_TOOLS = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit", "multiedit": "MultiEdit",
                  "glob": "Glob", "grep": "Grep", "list": "LS", "webfetch": "WebFetch", "websearch": "WebSearch",
                  "patch": "apply_patch", "task": "Task", "todowrite": "TodoWrite", "todoread": "TodoRead"}
OPENCODE_ARGUMENTS = {"filePath": "file_path", "oldString": "old_string", "newString": "new_string",
                      "replaceAll": "replace_all", "patchText": "command"}
PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.M)
SEARCH_TOOLS = ("Glob", "Grep", "LS")  # without "path" they search the working directory


def hook_value(value, depth=0):
    """VALUE as the rules see it: a string longer than HOOK_VALUE_LIMIT, and anything nested deeper than 32, is
    replaced by {"sandbox-ai": "too large"}: no condition on a string holds for it, so it is unknown, never cut
    short into something a rule would allow."""
    if isinstance(value, str):
        return value if len(value) <= HOOK_VALUE_LIMIT else {"sandbox-ai": "too large", "length": len(value)}
    if depth >= 32:
        return {"sandbox-ai": "too large"}
    if isinstance(value, dict):
        return {str(key): hook_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [hook_value(item, depth + 1) for item in value]
    return value


def hooks_supported(agent, command, run=subprocess.run):
    """None if AGENT's executable (COMMAND[0]) holds the hooks it is given, else why not: a Codex older than
    CODEX_HOOKS_VERSION, or one whose version cannot be read."""
    if agent != "codex":
        return None
    try:
        out = run([command[0], "--version"], capture_output=True, text=True, timeout=60).stdout or ""
    except (OSError, IndexError, subprocess.SubprocessError) as exc:
        return f"the version of Codex could not be read ({exc})"
    found = re.search(r"\b(\d+)\.(\d+)\.(\d+)", out)
    if not found:
        return f"the version of Codex could not be read ({printable_text(out.strip(), 80)!r})"
    if tuple(map(int, found.groups())) < CODEX_HOOKS_VERSION:
        return f"Codex {found.group(0)} is older than {'.'.join(map(str, CODEX_HOOKS_VERSION))}, the first that " \
               "holds managed hooks"
    return None


def hook_problem(cfg, run=subprocess.run):
    """None if the run's managed hooks hold, else why not. They are the rule hooks (cfg "hooks") and approved kit
    hooks (cfg "hook_agent" names the agent then, with or without rules): a Codex older than CODEX_HOOKS_VERSION
    ignores both, so the run is refused rather than started without them."""
    agent = cfg.get("hook_agent") or (cfg.get("hooks") or {}).get("agent")
    # A helper's command is its server: the agent it runs for each task is found by its name.
    command = [cfg["hook_binary"]] if isinstance(cfg.get("hook_binary"), str) else cfg.get("command") or []
    problem = hooks_supported(agent, command, run=run) if agent else None
    if problem is None:
        for companion in cfg.get("companion_hook_agents") or []:  # the agents --with, started from the shell
            problem = hooks_supported(companion, [companion], run=run)
            if problem:
                return f"the rules for {companion}'s own tools (--with {companion}) cannot hold: {problem}; " \
                       "update the image (sandbox-ai build team)"
        return None
    what = "the rules for the agent's own tools" if cfg.get("hooks") is not None else "the kits' approved hooks"
    return f"{what} cannot hold: {problem}; update the agent (sandbox-ai build)"


def hook_client(agent, event, stdin=None, stderr=None, socket_path=None, timeout=HOOK_TIMEOUT):
    """The agents' hook (`runtime.py --internal-hook AGENT EVENT`): the agent's JSON on STDIN (Claude Code and Codex:
    tool_name, tool_input, cwd; the OpenCode plugin: tool, args, cwd), the supervisor's decision back. Refused is
    exit status 2 with the reason on STDERR, which Claude Code and Codex both take as blocked (the OpenCode plugin
    throws); allowed is 0 without output, and the agent's own permissions go on. Fails closed: whatever goes wrong
    before a PreToolUse is decided refuses the call. PostToolUse only records (the tool ran already): always 0."""
    stdin, stderr = stdin or sys.stdin.buffer, stderr or sys.stderr
    pre = event != "PostToolUse"  # an event of no known name is refused too
    try:
        data = stdin.read(HOOK_READ_LIMIT + 1)
        if len(data) > HOOK_READ_LIMIT:
            raise ValueError("the hook message is too large")
        message = json.loads(data.decode("utf-8")) if data.strip() else None
        if agent not in HOOK_AGENTS or event not in HOOK_EVENTS or not isinstance(message, dict):
            raise ValueError("not a hook message")
        tool = message.get("tool_name", message.get("tool"))
        if not isinstance(tool, str) or not tool:
            raise ValueError("the hook message names no tool")
        arguments = message.get("tool_input", message.get("args"))
        if agent == "opencode":
            tool = OPENCODE_TOOLS.get(tool, tool)
            if isinstance(arguments, dict):
                arguments = {OPENCODE_ARGUMENTS.get(key, key): value for key, value in arguments.items()}
        result = message.get("tool_response", message.get("result"))
        size = message.get("result_bytes")
        request = {"agent": agent, "event": event, "tool": tool[:200], "input": hook_value(arguments),
                   "cwd": message.get("cwd") if isinstance(message.get("cwd"), str) else "",
                   "session_id": str(message.get("session_id", message.get("sessionID", "")))[:100],
                   "tool_use_id": str(message.get("tool_use_id", message.get("callID", "")))[:100]}
        if not pre:
            request["input"] = None  # recorded at PreToolUse already
            request["result_bytes"] = size if isinstance(size, int) and not isinstance(size, bool) else \
                len(json.dumps(result, default=repr)) if result is not None else None
        line = (json.dumps(request, ensure_ascii=False, default=repr) + "\n").encode()
        if len(line) > HOOK_LIMIT:
            line = (json.dumps({**request, "input": {"sandbox-ai": "too large", "length": len(line)}}) + "\n").encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path or HOOK_SOCKET))
            client.sendall(line)
            answer = json.loads(client.makefile("rb").readline(65536) or b"null")
        if not pre:
            return 0
        if isinstance(answer, dict) and answer.get("decision") == "allow":
            return 0
        reason = answer.get("reason") if isinstance(answer, dict) and isinstance(answer.get("reason"), str) else ""
        stderr.write(f"sandbox-ai: {printable_text(reason, 2000) or 'the tool call is refused'}\n")
        stderr.flush()
        return 2
    except Exception as exc:  # noqa: BLE001 - whatever breaks, the call is refused, never let through
        if not pre:
            return 0
        stderr.write(f"sandbox-ai: the tool call is refused: its rules could not be checked ({type(exc).__name__}: "
                     f"{printable_text(str(exc), 200)})\n")
        stderr.flush()
        return 2


def kit_hook(agent, timeout, command, stdin=None, stdout=None, stderr=None, check=None):
    """A kit's PreToolUse hook while rules hold (`runtime.py --internal-kit-hook AGENT TIMEOUT COMMAND`): runs
    COMMAND (sh -c) with the agent's message, within TIMEOUT seconds, and hands back its output and exit status.
    Claude Code runs a call's hooks in parallel, so the rule hook never sees the input a kit hook rewrites
    (hookSpecificOutput.updatedInput): that input is asked of the rules here (CHECK, the hook client) before it
    reaches the agent, and a refusal blocks the call (status 2). Output that cannot be read, or an updatedInput
    that is no object, refuses too: never let through unchecked."""
    stdin, stdout, stderr = stdin or sys.stdin.buffer, stdout or sys.stdout.buffer, stderr or sys.stderr
    check = check or hook_client

    def refuse(why):
        stderr.write(f"sandbox-ai: the tool call is refused: {why}\n")
        stderr.flush()
        return 2
    data = stdin.read(HOOK_READ_LIMIT + 1)
    if len(data) > HOOK_READ_LIMIT:
        return refuse("the hook message is too large")
    try:
        # A process group of its own: a hook that runs too long is stopped with everything it started (the shell's
        # children would run on, and hold the agent's pipes of this hook open).
        limit = float(timeout)
        done = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                start_new_session=True)
    except (OSError, ValueError) as exc:
        stderr.write(f"sandbox-ai: the kit hook did not start ({type(exc).__name__}: {printable_text(str(exc), 200)})\n")
        stderr.flush()
        return 1
    try:
        out, _ = done.communicate(data, timeout=limit)
    except subprocess.TimeoutExpired:  # what the agent does with a hook that runs too long: not an answer
        with contextlib.suppress(OSError):
            os.killpg(done.pid, signal.SIGKILL)
        done.wait()  # not its output: a process that left the group could hold that pipe for good
        for stream in (done.stdin, done.stdout):
            with contextlib.suppress(OSError):
                stream.close()
        stderr.write(f"sandbox-ai: the kit hook ran longer than {timeout} s and was stopped\n")
        stderr.flush()
        return 1
    out = out or b""
    if done.returncode == 0 and b"updatedInput" in out:  # the agents read updatedInput only after status 0
        if len(out) > HOOK_READ_LIMIT:
            return refuse("the kit hook's answer is too large to be checked")
        try:
            answer = json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return refuse("the kit hook's answer names updatedInput but is no JSON the rules can check")
        specific = answer.get("hookSpecificOutput") if isinstance(answer, dict) else None
        found = [place["updatedInput"] for place in (answer, specific)
                 if isinstance(place, dict) and "updatedInput" in place]
        if any(not isinstance(value, dict) for value in found):
            return refuse("the kit hook's updatedInput is no object")
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            message = None
        for value in found:
            if not isinstance(message, dict):
                return refuse("not a hook message")
            status = check(agent, "PreToolUse", stdin=io.BytesIO(json.dumps({**message, "tool_input": value}).encode()),
                           stderr=stderr)
            if status != 0:
                return 2
    stdout.write(out)
    stdout.flush()
    return done.returncode


class HookServer:
    """The supervisor's side of the hooks: RULES (a Ruleset with the "tool" rules and the untrusted tools), BROKER
    for "ask" (kind "tool"), TAINT, and LOG (tools.jsonl)."""

    def __init__(self, rules, broker=None, taint=None, log=None, secret_values=()):
        self.rules, self.broker, self.taint, self.log = rules, broker, taint, log
        self.values = sorted({v for v in secret_values if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True)
        self.lock, self.ready = threading.Lock(), threading.Event()  # ready: listening, open to the agent

    @staticmethod
    def facts(tool, arguments, cwd):
        """ARGUMENTS as the rules see them: with "paths", the files the call names, made absolute against CWD
        (file_path, notebook_path or path; the files of a patch; the working directory a search runs in). The
        agent's own "paths" never counts."""
        facts = dict(arguments)
        facts.pop("paths", None)
        found = [arguments[key] for key in ("file_path", "notebook_path", "path") if isinstance(arguments.get(key), str)]
        if tool == "apply_patch" and isinstance(arguments.get("command"), str):
            found += PATCH_FILE.findall(arguments["command"])
        if not found and tool in SEARCH_TOOLS and cwd:
            found = [cwd]
        if found:
            base = cwd if isinstance(cwd, str) and cwd.startswith("/") else None
            facts["paths"] = [posixpath.join(base, path) if base and not path.startswith("/") else path
                              for path in found]
        return facts

    def decide(self, message):
        """{"decision", "reason"} for one hook MESSAGE; logged."""
        agent, event = str(message.get("agent", ""))[:20], str(message.get("event", ""))[:40]
        tool, arguments = str(message.get("tool", ""))[:200], message.get("input")
        arguments = arguments if isinstance(arguments, dict) else {} if arguments is None else {"input": arguments}
        cwd = message.get("cwd") if isinstance(message.get("cwd"), str) else ""
        record = {"agent": agent, "event": event, "tool": tool,
                  **{key: str(message[key])[:100] for key in ("session_id", "tool_use_id") if message.get(key)}}
        state = self.taint.shown() if self.taint is not None else None
        if state and state.get("tainted"):
            record["tainted"] = state.get("since")
        if event != "PreToolUse":
            if isinstance(message.get("result_bytes"), int):
                record["result_bytes"] = message["result_bytes"]
            self.write(record)
            return {"decision": "allow"}
        record["input"] = self.text(arguments)
        if agent not in HOOK_AGENTS or not tool:
            verdict = {"decision": "deny", "rule": None, "ruleset": None, "reason": "not a hook of a known agent"}
        else:
            verdict = self.rules.agent_tool(agent, tool, self.facts(tool, arguments, cwd), state)
        record.update(decision=verdict["decision"], rule=verdict["rule"], ruleset=verdict["ruleset"])
        answer = {"decision": verdict["decision"], "reason": verdict.get("reason") or ""}
        if verdict["decision"] == "ask":
            if self.broker is None:
                answer = {"decision": "deny", "reason": f"{verdict['reason']}; asking is off"}
            else:
                approval = self.broker.ask("tool", {"agent": agent, "tool": tool, "arguments": record["input"],
                                                   "rule": verdict["rule"], "ruleset": verdict["ruleset"],
                                                   "reason": printable_text(verdict["reason"], 1000),
                                                   **({"taint": verdict["taint"]} if verdict.get("taint") else {})})
                record["approval"] = {key: approval.get(key) for key in ("decision", "scope", "reason")}
                answer = {"decision": "allow" if approval.get("decision") == "allow" else "deny",
                          "reason": f"not approved ({approval.get('reason', 'refused')}): {verdict['reason']}"}
        if answer["decision"] == "allow":
            # Marked before the tool runs: its result reaches the agent only once the run is tainted.
            source = self.rules.untrusted_agent_tool(agent, tool)
            if source and self.taint is not None:
                self.taint.mark(source)
                record["untrusted"] = source["rule"]
        else:
            record["reason"] = answer["reason"]
        self.write(record)
        return answer

    def text(self, arguments):
        text = McpGateway.arguments(arguments)
        for value in self.values:
            text = text.replace(value, "[redacted]")
        return text

    def write(self, record):
        if self.log is None:
            return
        line = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}, ensure_ascii=False,
                          default=repr) + "\n"
        with self.lock, contextlib.suppress(OSError):
            with open(self.log, "a") as stream:
                stream.write(line)

    def serve(self, path=None, stop=None):
        """Answer the agents' hooks on PATH (one message per connection) until STOP is set."""
        path = Path(path or HOOK_SOCKET)
        server = listen_unix(str(path))
        os.chmod(path, 0o666)
        self.ready.set()
        server.settimeout(0.5)
        while stop is None or not stop.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                threading.Thread(target=self.answer, args=(connection,), daemon=True).start()
            except RuntimeError:  # no thread to be had (the sandbox's process limit): this hook only
                connection.close()
                time.sleep(0.1)
        server.close()

    def answer(self, connection):
        with connection:
            try:
                connection.settimeout(10)
                data = b""
                while not data.endswith(b"\n") and len(data) <= HOOK_LIMIT:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                message = json.loads(data) if len(data) <= HOOK_LIMIT else None
                answer = self.decide(message) if isinstance(message, dict) else \
                    {"decision": "deny", "reason": "not a hook message"}
            except (OSError, ValueError, RecursionError) as exc:
                answer = {"decision": "deny", "reason": printable_text(f"the hook message could not be read ({exc})", 300)}
            except Exception as exc:  # noqa: BLE001 - a refusal, never a dropped connection the client cannot read
                answer = {"decision": "deny", "reason": printable_text(f"the rules could not be checked ({exc})", 300)}
            connection.settimeout(None)
            with contextlib.suppress(OSError):
                connection.sendall((json.dumps(answer) + "\n").encode())


# --- network changes during a run (sandbox-ai net) -------------------------------------------------
#
# The supervisor listens on CONTROL_DIR/control.sock, a host directory the agent cannot see. The host
# user's `sandbox-ai net allow|revoke` sends what to add or take back; the supervisor rewrites the
# firewall's "runtime" chain and the proxy's allow-list. Names reach /etc/hosts from the host side.

CONTROL_DIR = Path("/sandbox/control")

# macOS: the container runs in podman machine's Linux VM, and a Unix socket made on one side of its shared folders
# (virtiofs) cannot be connected from the other. The launcher marks such a run (config.json "machine": {"host": the
# Mac's address as the VM reaches it}); then the supervisor listens in VM_CONTROL_DIR (the container's own /run, hidden
# from the agent as the control directory is), and the host's commands reach it through `podman exec ... runtime.py
# --internal-control-client`. That client runs in the sandbox's PID namespace, where control_peer_refused refuses every
# peer: a request of it is taken only with the token the launcher wrote into the run's directory (EXEC_TOKEN, 0600,
# never mounted for the agent), which `sandbox-ai net` reads on the host and sends along.
VM_CONTROL_DIR = Path("/run/sandbox-control")
EXEC_TOKEN = "exec.token"
CONTROL_REQUEST_LIMIT = 65536
# The supervisor's own connections to the Mac's host ports (PortRelay upstream): marked, so the firewall lets them
# through to the Mac's address and the proxy's redirect leaves them alone. Only a process with CAP_NET_ADMIN can mark
# a socket; the agent has none.
HOST_RELAY_MARK = 0x5a1d


def runtime_rules(state):
    """nft commands that replace the runtime chain with STATE's permissions."""
    lines = ["flush chain inet agent_sandbox runtime"]
    for net in sorted(state["nets"]):
        family = "ip" if ipaddress.ip_network(net).version == 4 else "ip6"
        lines.append(f"add rule inet agent_sandbox runtime {family} daddr {net} accept")
    for net in sorted(state["https"]):
        family = "ip" if ipaddress.ip_network(net).version == 4 else "ip6"
        lines.append(f"add rule inet agent_sandbox runtime {family} daddr {net} tcp dport 443 accept")
    for address, port in sorted(state["tcp"]):
        family = "ip" if ipaddress.ip_address(address).version == 4 else "ip6"
        lines.append(f"add rule inet agent_sandbox runtime {family} daddr {address} tcp dport {port} accept")
    for address in sorted(state.get("dns", ())):  # DNS allowed during the run: port 53 of these nameservers
        family = "ip" if ipaddress.ip_address(address).version == 4 else "ip6"
        lines.append(f"add rule inet agent_sandbox runtime {family} daddr {address} udp dport 53 accept")
        lines.append(f"add rule inet agent_sandbox runtime {family} daddr {address} tcp dport 53 accept")
    return "\n".join(lines) + "\n"


RELAY_PORTS = 16          # host ports relayed at a time
RELAY_CONNECTIONS = 64    # open connections per relayed port


def relay_ports(request):
    """The host ports of REQUEST ("host_ports": a list of at most RELAY_PORTS ports), validated."""
    ports = request.get("host_ports", [])
    if not isinstance(ports, list) or len(ports) > RELAY_PORTS:
        raise ValueError(f"host_ports is a list of at most {RELAY_PORTS} ports")
    for port in ports:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError(f"bad port {port!r}")
    return sorted(set(ports))


def relay_rules(ports, proxy_port=PROXY_PORT):
    """nft commands that send the agent's connections to the relayed host PORTS through the proxy (which checks
    them as it checks the ports forwarded at the start, pushes included); the relay chain is jumped to from the
    proxy chain, after the proxy's own connections returned."""
    lines = ["flush chain inet agent_sandbox relay"]
    for port in sorted(ports):
        lines.append(f"add rule inet agent_sandbox relay ip daddr 127.0.0.1 tcp dport {int(port)} redirect to :{int(proxy_port)}")
        lines.append(f"add rule inet agent_sandbox relay ip6 daddr ::1 tcp dport {int(port)} redirect to :{int(proxy_port)}")
    return "\n".join(lines) + "\n"


RELAY_GRACE = 30  # seconds a relayed connection waits for its other side once one side ended


def join_streams(first, second):
    """Copy both ways between the sockets FIRST and SECOND until both sides are done; closes them. As the proxy's
    relay: once one side ended, the other gets RELAY_GRACE quiet seconds to finish (a service that never closes
    does not keep the connection), and a connection quiet both ways for IDLE_TIMEOUT ends."""
    try:
        relay(first, second, [0, 0], idle=IDLE_TIMEOUT, grace=RELAY_GRACE)
    finally:
        for sock in (first, second):
            with contextlib.suppress(OSError):
                sock.close()


class PortRelay:
    """The sandbox's end of a host port relayed during the run (`sandbox-ai net allow --allow-host-port PORT`):
    listens on 127.0.0.1:PORT (and [::1]:PORT) in the sandbox and joins each connection to the host relay's
    Unix socket in the run's control directory (DIRECTORY/relay-PORT.sock), which the host side (net.py) serves
    and which connects only to the host's 127.0.0.1:PORT. The agent cannot reach that directory.
    UPSTREAM (macOS, where that socket would not cross podman machine's shared folders): (address, port) of the
    Mac as the VM reaches it (gvproxy forwards it to the Mac's loopback), connected with HOST_RELAY_MARK; for the
    ports of the start (--allow-host-port, --llm) and those added during the run alike."""

    def __init__(self, port, directory, limit=RELAY_CONNECTIONS, listen_port=None, upstream=None):
        # LISTEN_PORT differs from PORT only in tests, where the host's port and the sandbox's share one network.
        self.port, self.path, self.limit = int(port), Path(directory) / f"relay-{int(port)}.sock", limit
        self.upstream = upstream
        self.listen_port = int(listen_port or port)
        self.slots = threading.BoundedSemaphore(limit)
        self.listeners = []
        for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            listener = socket.socket(family, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind((address, self.listen_port))
                listener.listen(32)
            except OSError as exc:
                listener.close()
                if family == socket.AF_INET6 and self.listeners and exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                    continue  # no IPv6 loopback in this sandbox: IPv4 is enough (taken by the agent is refused below)
                self.close()
                raise ValueError(f"port {self.port} cannot be relayed: {exc.strerror or exc} (something in the "
                                 "sandbox may listen on it already)") from None
            self.listeners.append(listener)
        for listener in self.listeners:
            threading.Thread(target=self.serve, args=(listener,), daemon=True).start()

    def serve(self, listener):
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return  # closed: the relay was revoked or the run ends
            if not self.slots.acquire(blocking=False):
                connection.close()  # RELAY_CONNECTIONS open already
                continue
            try:
                threading.Thread(target=self.handle, args=(connection,), daemon=True).start()
            except RuntimeError:  # no thread to be had (the sandbox's process limit): this connection only
                connection.close()
                self.slots.release()
                time.sleep(0.1)

    def handle(self, connection):
        try:
            upstream = socket.socket(socket.AF_UNIX if self.upstream is None else socket.AF_INET, socket.SOCK_STREAM)
            try:
                upstream.settimeout(10)
                if self.upstream is None:
                    upstream.connect(str(self.path))
                else:
                    upstream.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK", 36), HOST_RELAY_MARK)
                    upstream.connect(tuple(self.upstream))
                upstream.settimeout(None)
            except OSError:
                upstream.close()
                connection.close()  # the host relay ended (revoked, or its port is not served)
                return
            join_streams(connection, upstream)
        finally:
            self.slots.release()

    def close(self):
        for listener in self.listeners:
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                listener.close()
        self.listeners = []


def runtime_change(state, request):
    """STATE after REQUEST ({"op": "allow"|"revoke", "nets", "https", "tcp", "dns"}); every value validated."""
    if request.get("op") not in ("allow", "revoke"):
        raise ValueError("op must be allow or revoke")
    nets = {str(ipaddress.ip_network(str(v), strict=False)) for v in request.get("nets", [])}
    https = {str(ipaddress.ip_network(str(v), strict=False)) for v in request.get("https", [])}
    tcp = set()
    for address, port in request.get("tcp", []):
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError(f"bad port {port!r}")
        tcp.add((str(ipaddress.ip_address(str(address))), port))
    dns = request.get("dns", [])
    if not isinstance(dns, list) or len(dns) > 8:
        raise ValueError("dns is a list of at most 8 nameserver addresses")
    dns = {str(ipaddress.ip_address(str(v))) for v in dns}
    if any("%" in value for value in nets | https | dns | {a for a, _ in tcp}):
        raise ValueError("scope IDs are not accepted")
    change = {"nets": nets, "https": https, "tcp": tcp, "dns": dns}
    state = {"dns": set(), **state}
    if request["op"] == "allow":
        return {key: state[key] | change[key] for key in state}
    return {key: state[key] - change[key] for key in state}


def merged_allow(base, state, no_ssh=()):
    """The proxy's allow-list: the launcher's plus what was allowed during the run."""
    allow = dict(base)
    allow["nets"] = [*base.get("nets", []), *sorted(state["nets"])]
    allow["https"] = [*base.get("https", []), *sorted(state["https"])]
    allow["tcp"] = [*base.get("tcp", []), *[[a, p] for a, p in sorted(state["tcp"])],
                    *[[a, 53] for a in sorted(state.get("dns", ()))]]  # DNS over TCP to the allowed nameservers
    # Explicit destinations may also take SSH, as at the start (--allow, --allow-tcp); approved ones
    # (NO_SSH) may not.
    allow["ssh"] = sorted(state["nets"])
    allow["ssh_tcp"] = [[a, p] for a, p in sorted(state["tcp"]) if (a, p) not in set(no_ssh)]  # that port only
    return allow


def write_new(path, text, mode, owner=None):
    """Write PATH by replacing it with a new file; the temporary file is created exclusively and never
    through a symlink (the directory may be writable by a less trusted user, such as the proxy's). OWNER: (uid,
    gid) given to the file before it takes PATH's place (its reader is never shown a file it cannot read)."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(temporary)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        if owner is not None:
            os.fchown(fd, *owner)
        os.fchmod(fd, mode)  # whatever the umask
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


NFT_SECONDS = 30  # one firewall change during the run, at most


class RuntimeNetwork:
    """What was allowed during the run: the firewall's runtime chain and the proxy's allow-list, changed by
    `sandbox-ai net` and by approvals. A connection allowed "once" is open for ONCE_SECONDS, long enough
    to connect (an established connection outlives its rule)."""
    ONCE_SECONDS = 30

    def __init__(self, base_allow, allow_file=None, apply=None, within=None):
        def nft(script):
            # Under the lock: an nft that hangs must not hold every later change and approval. One stopped in
            # time changed nothing (nft -f applies all or nothing): the change fails as a refused one does.
            try:
                subprocess.run(["nft", "-f", "-"], input=script, text=True, check=True, capture_output=True,
                               timeout=NFT_SECONDS)
            except subprocess.TimeoutExpired:
                raise OSError(errno.ETIMEDOUT, f"nft did not finish within {NFT_SECONDS} s; nothing was changed") from None
        self.base, self.allow_file, self.apply_rules = base_allow, allow_file, apply or nft
        self.state = {"nets": set(), "https": set(), "tcp": set(), "dns": set()}
        self.dns_stub = True  # the supervisor says whether the run has its DNS stub (inner_bootstrap)
        self.within = [ipaddress.ip_network(n, strict=False) for n in within] if within is not None else None
        self.once = set()
        self.once_count = {}
        self.no_ssh = set()  # approved destinations: no SSH to them
        self.lock = threading.Lock()
        # Host ports relayed during the run ({port: PortRelay}); the supervisor sets RELAY_DIR (the control
        # directory, where the host relays listen) and PROXIED (the agent's TCP goes through the proxy).
        self.relays, self.relay_dir, self.proxied, self.relay_factory = {}, None, False, PortRelay
        # macOS (podman machine): the Mac's address the relays connect to instead of the host relay's socket.
        self.upstream_host = None

    def _commit(self, state, once):
        effective = dict(state, tcp=state["tcp"] | once)
        self.apply_rules(runtime_rules(effective) + (relay_rules(self.relays) if self.proxied else ""))
        self.state, self.once = state, once
        if self.allow_file is not None:
            allow = merged_allow(self.base, effective, self.no_ssh | once)
            allow["loopback"] = sorted({*self.base.get("loopback", []), *self.relays})
            write_new(self.allow_file, json.dumps(allow), 0o644)

    def relay(self, op, ports):
        """Start (allow) or stop (revoke) relaying host PORTS; called under the lock, before the commit."""
        if op == "revoke":
            for port in ports:
                relay = self.relays.pop(port, None)
                if relay is not None:
                    relay.close()
            return
        if self.relay_dir is None:
            raise ValueError("this run cannot relay host ports (no control directory)")
        started = []
        try:
            for port in ports:
                if port in self.relays:
                    continue
                if port in self.base.get("loopback", []):
                    raise ValueError(f"host port {port} is forwarded since the start already")
                if len(self.relays) >= RELAY_PORTS:
                    raise ValueError(f"at most {RELAY_PORTS} host ports are relayed at a time")
                self.relays[port] = self.relay_factory(port, self.relay_dir) if self.upstream_host is None else \
                    self.relay_factory(port, self.relay_dir, upstream=(self.upstream_host, port))
                started.append(port)
        except BaseException:
            for port in started:
                self.relays.pop(port).close()
            raise

    def close_relays(self):
        with self.lock:
            for relay in self.relays.values():
                relay.close()
            self.relays = {}

    def check_within(self, request):
        """Refuse an allow request that reaches outside the administrator's networks (policy allow_within)."""
        if self.within is None or request.get("op") != "allow":
            return
        added = runtime_change({"nets": set(), "https": set(), "tcp": set()}, request)
        networks = [ipaddress.ip_network(n, strict=False) for n in [*added["nets"], *added["https"], *added["dns"]]]
        networks += [ipaddress.ip_network(address) for address, _ in added["tcp"]]
        for network in networks:
            if not any(network.version == limit.version and network.subnet_of(limit) for limit in self.within):
                raise ValueError(f"{network} lies outside the networks the administrator's policy allows")

    def change(self, request, ssh=True):
        """Apply an allow or revoke request ({"op", "nets", "https", "tcp"}); the new state. SSH=False: the
        destinations added may not take SSH (approvals)."""
        self.check_within(request)
        if request.get("op") == "allow" and request.get("dns") and not self.dns_stub:
            raise ValueError("this run has no DNS stub to forward with (it was started with --no-proxy and without "
                             "--log-dir): start it again with --dns")
        ports = relay_ports(request)
        with self.lock:
            state = runtime_change(self.state, request)
            previous = dict(self.relays)
            if ports:
                self.relay(request.get("op"), ports)
            if request.get("op") == "allow":
                requested = runtime_change({"nets": set(), "https": set(), "tcp": set()}, request)["tcp"]
                if ssh:
                    self.no_ssh -= requested  # the human allowed it explicitly (sandbox-ai net)
                else:
                    self.no_ssh |= requested - self.state["tcp"]  # newly approved; explicit ones keep SSH
            self.no_ssh &= state["tcp"]
            try:
                self._commit(state, set(self.once))
            except BaseException:
                if ports and request.get("op") == "allow":
                    for port in set(self.relays) - set(previous):
                        self.relays.pop(port).close()
                raise
            return self.shown()

    def allow_once(self, address, port, timer=threading.Timer):
        target = (str(ipaddress.ip_address(address)), int(port))
        self.check_within({"op": "allow", "tcp": [[target[0], target[1]]]})
        with self.lock:
            # Counted: two overlapping "once" approvals of one target keep it open until the later one ends.
            self.once_count[target] = self.once_count.get(target, 0) + 1
            self._commit(self.state, self.once | {target})

        def expire():
            with self.lock:
                self.once_count[target] -= 1
                if self.once_count[target] <= 0:
                    del self.once_count[target]
                    self._commit(self.state, self.once - {target})
        closing = timer(self.ONCE_SECONDS, expire)
        closing.daemon = True
        closing.start()

    def nameservers(self):
        """The nameservers DNS was allowed to during the run, for the DNS stub (in the order they were given is
        not kept: sorted)."""
        with self.lock:
            return sorted(self.state.get("dns", ()))

    def shown(self):
        return {"nets": sorted(self.state["nets"]), "https": sorted(self.state["https"]),
                "tcp": [[a, p] for a, p in sorted(self.state["tcp"])], "dns": sorted(self.state.get("dns", ())),
                "host_ports": sorted(self.relays)}


NET_OPTIONS = (("nets", "--allow"), ("https", "--allow-host"), ("tcp", "--allow-tcp"), ("dns", "--dns"),
               ("host_ports", "--allow-host-port"))


def mode_refusal(mode, request):
    """Why the run's security MODE ({"name", "refuses": start options, ...} from config.json) refuses the allow
    REQUEST, or None: a run-time change must not open what the mode refuses at the start (a preset is
    --allow-host here: the request carries its addresses)."""
    if not mode or request.get("op") != "allow":
        return None
    refuses = mode.get("refuses") or []
    for field, option in NET_OPTIONS:
        if request.get(field) and option in refuses:
            return (f"{option} widens the security mode {mode.get('name')} of this run; answer its questions "
                    "instead, or start it again with a laxer mode")
    return None


# --- sandbox-ai rules reload: the HTTP rules of a running sandbox, read anew ---------------------------------------
#
# The human's rulesets (rules.json, --rules files, the trusted project file's "rules") are read again on the host and
# sent over the control socket; the supervisor keeps the start's own (the administrator's policy, the security mode,
# --publish, --telemetry, --allow-http) as they were, puts the new ones between them and writes the result for the
# proxy (RELOADED_RULES in PROXY_RUN, re-read on change). Each ruleset decides on its own and the strictest decision
# wins, so no reload gets past the policy's or the mode's rules. MCP, tool and device rules stay as the run started.

RELOADED_RULES = "rules.json"
RULES_CHECK_SECONDS = 1.0
# A ruleset's name, as the launcher gives it (--rules FILE: the file's name, whatever it is): any printable text.
RULESET_NAME = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,300}")
RELOAD_LIMIT = 64  # rulesets in one reload


def http_sources(sources):
    """The rulesets the proxy holds (as the launcher's proxy_rules writes them): their HTTP part, the ones with HTTP
    rules, a default for HTTP other than allow, or untrusted hosts."""
    return [{"name": source["name"], "version": 1, "defaults": {"http": source["defaults"]["http"]},
             "http": source["http"], "untrusted": [entry for entry in source.get("untrusted", []) if "host" in entry]}
            for source in sources
            if source["name"] != "--allow-http" and (source["http"] or source["defaults"]["http"] != "allow"
                                                      or any("host" in entry for entry in source.get("untrusted", [])))]


class RunRules:
    """The rulesets of the run's proxy (proxy.json's "rules" at the start), and their reload. PATH: where the proxy
    reads them anew (None without the proxy); FORBID: the options the administrator's policy forbids."""

    def __init__(self, proxy_config, path=None, forbid=()):
        self.started = list(proxy_config.get("rules", []))
        self.current, self.reloaded = list(self.started), None
        self.pinned, self.providers = dict(proxy_config.get("pinned", {})), list(proxy_config.get("providers", []))
        self.no_inspect = list(proxy_config.get("no_inspect", []))
        self.path, self.forbid, self.lock = path, list(forbid), threading.Lock()

    @staticmethod
    def own(name):
        """NAME is one of the start's own rulesets, which a reload keeps as they were."""
        return name in ("policy", "publish", "telemetry", "--allow-http") or name.startswith("mode ")

    def shown(self):
        return {"rules": [{"name": source["name"], "defaults": source["defaults"],
                           "http": [rule["id"] for rule in source["http"]],
                           "untrusted": [entry["id"] for entry in source.get("untrusted", [])],
                           "kept": self.own(source["name"])} for source in self.current],
                "reloaded": self.reloaded, "proxy": self.path is not None}

    def reload(self, sources, pinned=None):
        """Put SOURCES ([{"name", ...ruleset}], the human's rulesets read anew) in place of the ones the run started
        with, the start's own kept around them; checked and compiled before anything is written. PINNED: {name:
        [addresses]} of the names allowed during the run (the start's stay), so that a host's rules hold for its
        bare address too."""
        extra = {}
        for name, addresses in (pinned or {}).items() if isinstance(pinned, dict) else ():
            if not (isinstance(name, str) and 0 < len(name) <= 253 and isinstance(addresses, list)):
                continue
            kept = []
            for address in addresses[:64]:  # each on its own: one that is no address drops only itself
                with contextlib.suppress(ValueError, TypeError):
                    kept.append(str(ipaddress.ip_address(address)))
            if kept:
                extra[name.lower()] = kept
        if self.path is None:
            raise ValueError("this sandbox runs without the connection proxy (--no-proxy): it holds no HTTP rules to "
                             "reload")
        if not isinstance(sources, list) or len(sources) > RELOAD_LIMIT:
            raise ValueError(f"rules: a list of at most {RELOAD_LIMIT} rulesets")
        incoming, names = [], set()
        for source in sources:
            name = source.get("name") if isinstance(source, dict) else None
            if not isinstance(name, str) or not RULESET_NAME.fullmatch(name) or name in names:
                raise ValueError("every ruleset has a name of its own (printable, at most 300 characters)")
            names.add(name)
            if self.own(name):
                raise ValueError(f"{name} is the start's own ruleset: a reload keeps it as it was")
            if name.startswith("--rules") and "--rules" in self.forbid:
                safeguard_event("policy", "sandbox-ai rules reload --rules",
                                "--rules is not allowed by the administrator's policy", option="--rules")
                raise ValueError("--rules is not allowed by the administrator's policy")
            incoming.append({"name": name, **Ruleset.validated({k: v for k, v in source.items() if k != "name"},
                                                               name)})
        http = http_sources(incoming)
        started = {source["name"]: json.dumps(source, sort_keys=True) for source in self.started}
        # The policy as it is now joins the start's (it can only narrow), unless it is the same.
        http = [source for source in http if not (source["name"] == "policy (now)" and started.get("policy") ==
                                                  json.dumps({**source, "name": "policy"}, sort_keys=True))]
        for source in http:
            hosts = [host for rule in source["http"] for host in rule["host"]]
            hosts += ["*"] if source["defaults"]["http"] != "allow" else []
            for value in self.no_inspect:
                for host in hosts:
                    if host == "*" or self.same_server(host, value):
                        raise ValueError(f"--no-inspect {value}: the HTTP rules of {source['name']} ({host}) cannot "
                                         "hold for requests the proxy does not open")
        composed = [s for s in self.started if s["name"] == "policy" or s["name"].startswith("mode ")] + http + \
            [s for s in self.started if s["name"] in ("publish", "telemetry")]
        pins = {**extra, **self.pinned}  # the start's names keep their addresses
        Ruleset(composed, pins, self.providers)  # compiles, or the reload is refused (RuleError)
        with self.lock:
            # The proxy reads {"rulesets", "pinned"} (a bare list: a supervisor before 0.1.92 wrote that).
            write_new(self.path, json.dumps({"rulesets": composed, "pinned": extra}), 0o644)
            self.current, self.reloaded = composed, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {**self.shown(), "not_reloaded": sorted(
            source["name"] for source in incoming
            if source["mcp"] or source["tool"] or source["devices"]
            or source["defaults"]["mcp"] != "allow" or source["defaults"]["tool"] != "allow"
            or source["defaults"]["devices"] != "allow")}

    @staticmethod
    def same_server(host, pattern):
        """HOST (a name or *.DOMAIN) and PATTERN (the same) can name the same server (the launcher's
        host_matches_any)."""
        host, pattern = host.lower(), pattern.lower()
        bare = lambda value: value[2:] if value.startswith("*.") else value  # noqa: E731
        return host == pattern or bare(host).endswith("." + bare(pattern)) or \
            bare(pattern).endswith("." + bare(host)) or bare(host) == bare(pattern)


MCP_OPS = ("mcp-open", "mcp-close", "mcp-list")
CONTROL_FDS = 4  # descriptors a request may carry (SCM_RIGHTS): mcp-open hands over one
CONTROL_LIMIT = 4 << 20  # bytes of one request (rules reload sends the human's rulesets; up to 1 MiB each file)


def detached_root(fd):
    """The directory descriptor FD is the root of a tree of its own: '..' from it is itself (a detached clone,
    open_tree(OPEN_TREE_CLONE), as `sandbox-ai mcp attach --live` hands over the server's socket directory), so that
    nothing outside it can be reached through it. A host directory's descriptor leads to its parent: False."""
    try:
        here, parent = os.fstat(fd), os.stat("..", dir_fd=fd, follow_symlinks=False)
    except OSError:
        return False
    return (here.st_dev, here.st_ino) == (parent.st_dev, parent.st_ino) and stat.S_ISDIR(here.st_mode)


def mcp_control(request, relays, fds, forbid=(), mode=None):
    """The answer to an MCP request on the control socket (`sandbox-ai mcp attach|detach NAME START --live`):
    mcp-open {"name", "relay": gateway configuration} with the server's socket directory as the one descriptor in
    FDS (taken over), mcp-close {"name"}, mcp-list. RELAYS: the run's McpRelays. FORBID and MODE as for allow: a
    run whose policy forbids --mcp, or whose security mode refuses it, opens none."""
    op = request.get("op")
    if relays is None:
        raise ValueError("this sandbox relays no MCP servers (an MCP server's own sandbox, or an older version)")
    if op == "mcp-list":
        return {"ok": True, "servers": relays.names(), "live": relays.live}
    name = request.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise ValueError("an MCP server's name: 1-32 letters, digits, '_' or '-'")
    if op == "mcp-close":
        return {"ok": True, "closed": relays.detach(name), "servers": relays.names()}
    if "--mcp" in forbid:
        raise ValueError("--mcp is not allowed by the administrator's policy")
    if "--mcp" in ((mode or {}).get("refuses") or []):
        raise ValueError(f"--mcp widens the security mode {(mode or {}).get('name')} of this run")
    if len(fds) != 1:
        raise ValueError("mcp-open takes the server's socket directory as one descriptor")
    info = os.fstat(fds[0])
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("mcp-open: the descriptor is no directory")
    if not detached_root(fds[0]):
        raise ValueError("mcp-open: the descriptor is no detached copy of the server's socket directory (its '..' "
                         "leads out of it); nothing outside that folder is taken (sandbox-ai 0.1.76 or later)")
    config = request.get("relay")
    if not isinstance(config, dict) or config.get("server") != name or not isinstance(config.get("rules", []), list):
        raise ValueError("mcp-open: the relay's configuration is {\"server\": NAME, \"rules\": [...]}")
    Ruleset(config.get("rules", []))  # checked before anything is opened
    fd = fds.pop()  # taken over: closed with the relay (or by attach() when it refuses)
    opened = relays.attach(name, {"server": name, "rules": config.get("rules", [])}, fd)
    return {"ok": True, "opened": opened, "already": not opened, "socket": str(relays.run_dir / name / "server.sock"),
            "servers": relays.names()}


def control_peer_refused(connection, sandboxed=None):
    """Why the peer of CONNECTION on the control socket is refused, or None. Only the human reaches the socket (the
    run's directory on the host, never mounted for the agent): its `sandbox-ai net`, `mcp --live` and `workspace`
    run outside the sandbox's PID namespace, where SO_PEERCRED reports pid 0. A peer whose pid shows in this
    namespace is a process of the sandbox (one that got hold of the socket somehow): refused. The supervisor itself
    (its own pid) is no such peer; no other process inside connects to this socket. SANDBOXED: this supervisor is the
    first process of the sandbox's PID namespace (PID 1, as inner_bootstrap is); elsewhere (tests, a probe) every
    pid shows and nothing is refused."""
    if not (os.getpid() == 1 if sandboxed is None else sandboxed):
        return None
    try:
        pid, _, _ = Attribution.peer(connection)
    except (OSError, struct.error):
        return "the peer of this connection cannot be told"
    if pid in (0, os.getpid()):
        return None
    return f"process {pid} of the sandbox may not use the control socket"


def exec_token(path):
    """The token of the run's directory (EXEC_TOKEN, see VM_CONTROL_DIR), or None when there is none."""
    if path is None:
        return None
    try:
        token = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token if re.fullmatch(r"[0-9a-f]{32,128}", token) else None


def control_client(source=None, out=None, paths=None, timeout=60):
    """--internal-control-client (macOS: `podman exec` runs it in the container for `sandbox-ai net`, `workspace`
    and `mcp`): one request line from SOURCE (stdin) to the supervisor's control socket, its answer to OUT (stdout).
    Only bytes are passed on; the supervisor checks the request's token (control_server)."""
    source = source or sys.stdin.buffer
    out = out or sys.stdout.buffer
    paths = paths or (VM_CONTROL_DIR / "control.sock", CONTROL_DIR / "control.sock")
    # As much as the control server takes (CONTROL_LIMIT: a rules reload sends the human's rulesets).
    line = source.readline(CONTROL_LIMIT + 1)
    if not line.endswith(b"\n") or len(line) > CONTROL_LIMIT:
        print(f"agent-sandbox: one request line of at most {CONTROL_LIMIT >> 20} MiB is expected", file=sys.stderr)
        return 2
    path = next((p for p in paths if Path(p).exists()), paths[0])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall(line)
        answer = b""
        while not answer.endswith(b"\n") and len(answer) <= CONTROL_REQUEST_LIMIT * 64:
            chunk = client.recv(65536)
            if not chunk:
                break
            answer += chunk
    out.write(answer)
    out.flush()
    return 0


def control_server(path, network, stop=None, forbid=(), mode=None, taint=None, relays=None, sandboxed=None,
                   rules=None, token_file=None):
    """Serve `sandbox-ai net` requests (allow, revoke, show, untaint; watch: a workspace added during the run, for
    the file log) and `sandbox-ai mcp ... --live` requests (MCP_OPS, see mcp_control; RELAYS: the run's McpRelays)
    on the Unix socket PATH until STOP is set.
    FORBID: the options the administrator's policy forbids, MODE: the run's strict security mode (or None); a
    run-time change must not do what they would. Checked here, not only by `sandbox-ai net`: a request written by
    hand reaches this socket too. TAINT: the run's taint, shown and taken back (untaint) here: only the human
    reaches this socket, and a peer inside the sandbox is refused (control_peer_refused; SANDBOXED as there).
    Descriptors a request carries are taken with close-on-exec, and closed unless taken over. Whatever fails for one
    request (a thread that cannot be started at the sandbox's pids limit, say) refuses that one; the socket keeps
    serving. RULES: the run's RunRules (op "rules": show them, or reload them with the human's rulesets read anew).
    TOKEN_FILE (macOS, see VM_CONTROL_DIR): a peer control_peer_refused refuses is heard when its request carries
    that file's token ("exec_token") and no descriptors."""
    listener = listen_unix(path)
    listener.settimeout(0.5)
    while stop is None or not stop.is_set():
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        fds = []
        with connection:
            try:
                connection.settimeout(10)
                refused = control_peer_refused(connection, sandboxed)
                token = exec_token(token_file) if refused else None
                if refused and token is None:
                    raise PermissionError(refused)
                data = b""
                while not data.endswith(b"\n") and len(data) < CONTROL_LIMIT:
                    chunk, received, _, _ = socket.recv_fds(connection, 4096, CONTROL_FDS,
                                                            getattr(socket, "MSG_CMSG_CLOEXEC", 0))
                    fds += received
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data)
                if refused:  # heard only as the host's request through podman exec (macOS)
                    given = request.pop("exec_token", None) if isinstance(request, dict) else None
                    if fds or not isinstance(given, str) or not secrets.compare_digest(given.encode(), token.encode()):
                        raise PermissionError(refused)
                if request.get("op") in MCP_OPS:
                    answer = mcp_control(request, relays, fds, forbid, mode)
                    with contextlib.suppress(OSError):
                        connection.sendall((json.dumps(answer) + "\n").encode())
                    continue
                if request.get("op") == "wildcards":
                    answer = {"ok": True, "wildcards": change_wildcards(request, forbid, mode)}
                    with contextlib.suppress(OSError):
                        connection.sendall((json.dumps(answer) + "\n").encode())
                    continue
                if request.get("op") == "api-keys":
                    # `sandbox-ai net allow|revoke --api-key`: as the start option, within the policy and the mode.
                    if "--api-key" in forbid:
                        safeguard_event("policy", "sandbox-ai net allow --api-key",
                                        "--api-key is not allowed by the administrator's policy", option="--api-key")
                        raise ValueError("--api-key is not allowed by the administrator's policy")
                    if request.get("api_keys") and mode and "--api-key" in (mode.get("refuses") or []):
                        raise ValueError(f"--api-key widens the security mode {mode.get('name')} of this run")
                    answer = {"ok": True, "api_keys": give_api_keys(request.get("api_keys"))}
                    with contextlib.suppress(OSError):
                        connection.sendall((json.dumps(answer) + "\n").encode())
                    continue
                if request.get("op") == "rules":
                    if rules is None:
                        raise ValueError("this sandbox keeps no rules to show or reload")
                    answer = {"ok": True, **(rules.reload(request.get("rules"), request.get("pinned"))
                                             if request.get("action") == "reload"
                                             else rules.shown())}
                    with contextlib.suppress(OSError):
                        connection.sendall((json.dumps(answer) + "\n").encode())
                    continue
                if request.get("op") == "watch":
                    answer = {"ok": True, "watched": watch_added_workspace(request.get("path"))}
                    with contextlib.suppress(OSError):
                        connection.sendall((json.dumps(answer) + "\n").encode())
                    continue
                if request.get("op") == "allow":
                    for field, option in NET_OPTIONS:
                        if request.get(field) and (option in forbid or field == "dns" and "--nameserver" in forbid
                                                   or (field == "nets" and "--egress-all-allow" in forbid
                                                                        and any(ipaddress.ip_network(n, strict=False).prefixlen == 0
                                                                                for n in request[field]))):
                            safeguard_event("policy", f"sandbox-ai net allow {option}",
                                            f"{option} is not allowed by the administrator's policy", option=option)
                            raise ValueError(f"{option} is not allowed by the administrator's policy")
                refused = mode_refusal(mode, request)
                if refused:
                    asked = [option for field, option in NET_OPTIONS if request.get(field)]
                    safeguard_event("mode", f"sandbox-ai net allow {' '.join(asked)}", refused,
                                    mode=(mode or {}).get("name"), option=" ".join(asked))
                    raise ValueError(refused)
                if request.get("op") == "untaint":
                    if taint is None:
                        raise ValueError("this sandbox keeps no taint")
                    answer = {"ok": True, "was": taint.reset(), "taint": taint.shown()}
                else:
                    answer = {"ok": True, **(network.shown() if request.get("op") == "show" else network.change(request)),
                              **({"taint": taint.shown()} if taint is not None and request.get("op") == "show" else {})}
            except Exception as exc:  # noqa: BLE001 - one request's failure never ends the control socket
                answer = {"ok": False, "error": str(exc)[:500] or type(exc).__name__}
            finally:
                for fd in fds:  # not taken over
                    with contextlib.suppress(OSError):
                        os.close(fd)
            with contextlib.suppress(OSError):
                connection.sendall((json.dumps(answer) + "\n").encode())
    listener.close()


# --- approvals -------------------------------------------------------------------------------------
#
# A connection to a destination the options do not allow, a lookup of a name they do not pin, a push
# the privacy check has warnings about, and a request an ask rule holds are put to the human: the
# request appears as a file in the run's control directory on the host (sandbox-ai approvals, the app,
# a desktop notification); the answer comes back as a file next to it. Without an answer within
# APPROVAL_SECONDS it is refused.

APPROVAL_SECONDS = 60
VERIFY_SECONDS = 15      # how long "Allow always" for a name waits for the certificate that proves it
# A name "Allow always" may remember (as the approvals store takes it): a dotted host name, never an address.
REMEMBERED_NAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9-]{1,63}")
SCOPES = ("once", "run", "always")


def narrowest_scope(*scopes):
    """The narrowest of the approval SCOPES; an unknown one counts as once."""
    return min((scope if scope in SCOPES else "once" for scope in scopes), key=SCOPES.index)
OPEN_QUESTIONS = 20      # at a time; more are refused without asking
RUN_QUESTIONS = 500      # per run; after that, nothing more is asked
# Lookups below a host wildcard (details "wildcard": its suffix) are answered on the host by themselves: they have a
# budget of their own, per wildcard, so that a program looking up names without end cannot use up the questions the
# human is asked (RUN_QUESTIONS, OPEN_QUESTIONS). Twice the names the host opens below one (approvals.WILDCARD_NAMES):
# past those it asks the human, and refusals count too. At a time they are held by the DNS stub's DNS_WORKERS.
WILDCARD_QUESTIONS = 512


# --ask allow: the launcher answers every question by itself (approvals.auto_answer) and marks its answer so ("by");
# it is recorded with this reason (as approvals.AUTO_REASON). Only the host writes answers: the agent cannot.
AUTO_BY = "ask-allow"
AUTO_REASON = "allowed automatically (--ask allow)"
AUTO_REFUSED = "refused automatically: --ask allow could not allow it"


def answered_reason(answer):
    """The reason an answer (an .answer file's content, written on the host) is recorded with: "answered" for a
    human's, AUTO_REASON or AUTO_REFUSED (with why) for one --ask allow gave."""
    if answer.get("by") != AUTO_BY:
        return "answered"
    if answer.get("decision") == "allow":
        return AUTO_REASON
    return AUTO_REFUSED + (f" ({printable_text(answer['why'], 300)})" if isinstance(answer.get("why"), str) else "")


class ApprovalBroker:
    """The supervisor's side: one request per question (repeats wait for the same answer), decisions
    remembered for the run, firewall changes for what is allowed."""

    def __init__(self, directory, network, timeout=APPROVAL_SECONDS, poll=0.2, clock=time.monotonic, max_scope="always",
                 blocked=None, hosts_file=Path("/etc/hosts"), no_inspect=(), public=None, verify_seconds=VERIFY_SECONDS,
                 name_ports=(443,)):
        self.directory, self.network, self.timeout, self.poll, self.clock = Path(directory), network, timeout, poll, clock
        # What "Allow always" for a connection remembers (see remembers): the names sandbox-ai pinned (HOSTS_FILE,
        # the sandbox's /etc/hosts), the hosts whose TLS is not opened (NO_INSPECT), which addresses count as
        # public (PUBLIC), the ports a remembered name opens (NAME_PORTS: HTTPS, as --allow-host) and the certificates
        # the proxy validated in this run (verified). PUBLIC and NAME_PORTS differ only in tests.
        self.hosts_file, self.no_inspect, self.verify_seconds = Path(hosts_file), list(no_inspect), verify_seconds
        self.name_ports = tuple(name_ports)
        self.public = public or public_address
        self.verified, self.proved = {}, threading.Condition()
        # The administrator's policy may cap the scope: an answer wider than that counts as the widest allowed.
        self.max_scope = max_scope if max_scope in SCOPES else "once"
        # Blocked always for this project (sandbox-ai approvals deny --scope always): refused without a question.
        blocked = blocked if isinstance(blocked, dict) else {}
        self.blocked_hosts = {h for h in blocked.get("hosts", []) if isinstance(h, str)}
        self.blocked_tcp = {t for t in blocked.get("tcp", []) if isinstance(t, str)}
        self.lock, self.pending, self.decided, self.counter = threading.Lock(), {}, {}, 0
        self.serial, self.wildcard_asked = 0, {}  # every question's number (its ID); wildcard questions per suffix
        self.run_token = secrets.randbelow(10 ** 9)  # IDs differ between sandboxes asking in the same second
        self.directory.mkdir(mode=0o700, exist_ok=True)
        memory_part("questions open", lambda: len(self.pending))
        memory_part("answers kept", lambda: len(self.decided))
        memory_part("certificates seen", lambda: len(self.verified))

    @staticmethod
    def key(kind, details):
        if kind == "push":  # an answer covers this repository with these warnings, not later other ones
            digest = hashlib.sha256(str(details.get("message", "")).encode("utf-8", "replace")).hexdigest()[:16]
            return kind, f"{details.get('url', '')} {digest}"
        if kind == "http":  # "run" covers this endpoint under this rule, not the rule's other requests
            return kind, " ".join(str(details.get(k, "")) for k in ("ruleset", "rule", "method", "host", "path",
                                                                   "host_header"))
        if kind == "mcp":  # "run" covers this tool under this rule
            return kind, " ".join(str(details.get(k, "")) for k in ("ruleset", "rule", "server", "tool"))
        if kind == "tool":  # the agent's own tool: "run" covers it under this rule
            return kind, " ".join(str(details.get(k, "")) for k in ("ruleset", "rule", "agent", "tool"))
        return kind, str(details.get("destination") or details.get("name") or details.get("url") or "")

    def blocked(self, kind, details):
        if kind == "dns":
            return str(details.get("name", "")).lower() in self.blocked_hosts
        if kind == "connect":
            address, port = str(details.get("address", "")), details.get("port")
            return (f"[{address}]:{port}" if ":" in address else f"{address}:{port}") in self.blocked_tcp
        return False

    def ask(self, kind, details, wait=True):
        """{"decision": "allow"|"deny", "scope", "reason"} for a question; WAIT=False only files it."""
        if self.blocked(kind, details):
            return {"decision": "deny", "scope": "run", "reason": "blocked always for this project"}
        key = self.key(kind, details)
        with self.lock:
            if key in self.decided:
                return self.decided[key]
            waiting = self.pending.get(key)
            if waiting is None:
                below = str(details.get("wildcard")) if kind == "dns" and details.get("wildcard") else None
                if below is not None:
                    if self.wildcard_asked.get(below, 0) >= WILDCARD_QUESTIONS:
                        return {"decision": "deny", "scope": "once", "reason": "too many lookups below the wildcard"}
                    self.wildcard_asked[below] = self.wildcard_asked.get(below, 0) + 1
                else:
                    asked = sum(1 for entry in self.pending.values() if not entry.get("wildcard"))
                    if asked >= OPEN_QUESTIONS or self.counter >= RUN_QUESTIONS:
                        return {"decision": "deny", "scope": "once", "reason": "too many questions"}
                    self.counter += 1
                self.serial += 1
                ident = f"{int(time.time())}-{self.run_token}-{self.serial}"
                waiting = self.pending[key] = {"id": ident, "event": threading.Event(), "answer": None,
                                               "wildcard": below is not None}
                if kind == "connect":  # shown in the question: what "Allow always" will remember
                    details = {**details, "remembers": self.remembers(details)}
                try:
                    self._write(f"{ident}.json", {"id": ident, "kind": kind, "time": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "timeout": self.timeout, **details})
                    threading.Thread(target=self._wait, args=(key, kind, details, waiting), name="approval-wait",
                                     daemon=True).start()
                except (OSError, RuntimeError) as exc:  # no space, no more threads: refused, not asked
                    self.pending.pop(key, None)
                    return {"decision": "deny", "scope": "once", "reason": f"could not ask: {exc}"}
        if not wait:
            return None
        waiting["event"].wait(self.timeout + 5)
        return waiting["answer"] or {"decision": "deny", "scope": "once", "reason": "no answer"}

    def _write(self, name, data):
        write_new(self.directory / name, json.dumps(data), 0o600)

    def _read_answer(self, ident):
        path = self.directory / f"{ident}.answer"
        try:
            answer = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(answer, dict) or answer.get("decision") not in ("allow", "deny"):
            return {"decision": "deny", "scope": "once", "reason": "unreadable answer"}
        scope = answer.get("scope", "once")
        scope = scope if scope in SCOPES else "once"
        if SCOPES.index(scope) > SCOPES.index(self.max_scope):
            if answer["decision"] == "allow":  # a wider allow than the mode or the policy lets through
                by, mode = getattr(self, "capped_by", ("policy", None))
                capper = f"the security mode {mode}" if by == "mode" else "the administrator's policy"
                safeguard_event(by, f"the answer \"allow {scope}\" to question {ident}",
                                f"answers are capped to \"{self.max_scope}\" by {capper}",
                                directory=self.directory.parent, mode=mode, target="approval scope")
            scope = self.max_scope
        return {"decision": answer["decision"], "scope": scope,
                "reason": answered_reason(answer), "addresses": answer.get("addresses", [])}

    def _wait(self, key, kind, details, waiting):
        try:
            self._settle(key, kind, details, waiting)
        except OSError:  # the .done file not written (a full disk): refused below, the thread ends quietly
            pass
        finally:
            if not waiting["event"].is_set():
                # Something failed on the way (a damaged answer, a full disk): the asker is refused now, not after
                # its timeout, and the question is open no more (it would hold one of OPEN_QUESTIONS for good, and
                # every later ask of it would wait in vain).
                with self.lock:
                    if self.pending.get(key) is waiting:
                        self.pending.pop(key, None)
                waiting["answer"] = waiting["answer"] or {"decision": "deny", "scope": "once",
                                                          "reason": "the answer could not be handled"}
                waiting["event"].set()

    def _settle(self, key, kind, details, waiting):
        deadline = self.clock() + self.timeout
        answer = None
        while self.clock() < deadline:
            answer = self._read_answer(waiting["id"])
            if answer:
                break
            time.sleep(self.poll)
        answer = answer or {"decision": "deny", "scope": "run", "reason": f"no answer within {self.timeout} s"}
        if kind in ("push", "http", "mcp", "tool") and answer["scope"] == "always":
            answer = {**answer, "scope": "run"}  # pushes, requests and tool calls are never remembered beyond the run
        if answer["decision"] == "allow":
            try:
                self._apply(kind, details, answer)
            except (ValueError, TypeError, KeyError, OSError, subprocess.CalledProcessError) as exc:
                answer = {"decision": "deny", "scope": "once", "reason": f"could not be applied: {exc}"}
        remember = None
        if kind == "connect" and answer["decision"] == "allow" and answer["scope"] == "always":
            remember = details.get("remembers") or self.remembers(details)
        with self.lock:
            self.pending.pop(key, None)
            if answer["scope"] in ("run", "always") or answer["reason"].startswith("no answer"):
                self.decided[key] = answer  # asked once per run (a deny counts for the run unless "once")
        proof = bool(remember) and remember.get("verified") == "on connection"
        if proof:  # the proxy goes on first: the approved connection is what proves (or not) the name it sent
            waiting["answer"] = answer
            waiting["event"].set()
            remember = self.await_proof(details, remember)
        self._write(f"{waiting['id']}.done", {**answer, "id": waiting["id"], **({"remember": remember} if remember else {})})
        if not proof:
            waiting["answer"] = answer
            waiting["event"].set()

    def pinned(self):
        """{name: {addresses}} sandbox-ai pinned for this run (the sandbox's /etc/hosts, written by the launcher
        and by approved lookups; the agent cannot change it)."""
        names = {}
        with contextlib.suppress(OSError):
            for line in self.hosts_file.read_text().splitlines():
                fields = line.split("#", 1)[0].split()
                for name in fields[1:]:
                    with contextlib.suppress(ValueError):
                        names.setdefault(name.lower().rstrip("."), set()).add(str(ipaddress.ip_address(fields[0])))
        return names

    def remembers(self, details):
        """What "Allow always" for the connection question DETAILS remembers for the project: the host name
        ({"host", "verified", "tcp"}) when it can be proved, else the address ({"tcp", "why"}). The name is the
        agent's claim; it counts when sandbox-ai pinned it to this address, or a certificate the proxy checked
        against the system's CAs is valid for it (already in this run, else on the approved connection:
        "verified": "on connection"). Never checked before the answer: the server name the agent chose would
        then leave the sandbox unapproved. A name is opened like --allow-host (HTTPS on port 443)."""
        address, port = str(details.get("address", "")), details.get("port")
        tcp = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        claimed = details.get("host")
        name = question_name(claimed)

        def address_only(why):
            return {"tcp": tcp, "why": why}
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return address_only("no address")
        if not claimed:
            return address_only("no host name was sent")
        if not name or not REMEMBERED_NAME.fullmatch(name) or Ruleset.is_address(name):
            return address_only("the name sent is an address or not a dotted host name")
        if port not in self.name_ports:
            return address_only("a name is remembered for HTTPS (port 443) only")
        if not self.public(ip):
            return address_only("not a public address")
        if any(host_matches(name, [pattern]) for pattern in self.no_inspect):
            return address_only("its TLS passes unopened (--no-inspect), so no certificate is checked")
        if address in self.pinned().get(name, ()):
            return {"host": name, "verified": "pinned", "tcp": tcp}
        with self.proved:
            if name in self.verified.get((address, port), {}).get(True, ()):
                return {"host": name, "verified": "certificate", "tcp": tcp}
        if details.get("protocol") != "tls":
            return address_only("not TLS: nothing proves the name the agent sent")
        return {"host": name, "verified": "on connection", "tcp": tcp}

    def prove(self, address, port, name, ok):
        """The proxy checked the certificate of ADDRESS:PORT for NAME against the system's CAs: valid (OK) or
        not. Kept for the run (at most 1024 destinations)."""
        name = question_name(name)
        try:
            key = (str(ipaddress.ip_address(str(address))), int(port))
        except (ValueError, TypeError):
            return
        if not name:
            return
        with self.proved:
            if key not in self.verified and len(self.verified) >= 1024:
                return
            self.verified.setdefault(key, {True: set(), False: set()})[bool(ok)].add(name)
            self.proved.notify_all()

    def await_proof(self, details, remember):
        """REMEMBER as the name once the approved connection proved it, else as the address (a certificate for
        another name, none in time, no connection)."""
        key, name = (str(details["address"]), int(details["port"])), remember["host"]
        deadline = self.clock() + self.verify_seconds
        with self.proved:
            while True:
                outcome = self.verified.get(key, {True: set(), False: set()})
                if name in outcome[True]:
                    return {"host": name, "verified": "certificate", "tcp": remember["tcp"]}
                if name in outcome[False] or self.clock() >= deadline:
                    return {"tcp": remember["tcp"], "why": "the server's certificate did not prove the name"
                            if name in outcome[False] else "no certificate proved the name in time"}
                self.proved.wait(self.poll)

    def _apply(self, kind, details, answer):
        if kind == "connect":
            address, port = details["address"], int(details["port"])
            # An approved destination never takes SSH: that would bypass the push check.
            if answer["scope"] == "once":
                self.network.allow_once(address, port)
            else:
                self.network.change({"op": "allow", "tcp": [[address, port]]}, ssh=False)
        elif kind == "dns":
            # Only public addresses: a name the agent controls must not open the LAN or this host.
            addresses = [str(ip) for ip in (ipaddress.ip_address(str(a)) for a in answer.get("addresses", []))
                         if public_address(ip)][:32]
            if addresses:
                self.network.change({"op": "allow", "https": addresses})


def serve_asks(channel, broker, who=None, taint=None):
    """The supervisor's end of the proxy's channel: JSON lines {"id", "kind", ...} in, answers out. Kinds:
    questions (connect, push, http; refused without a BROKER) and who (the process behind the client end
    {"peer": [address, port]} of a connection; WHO is the Attribution)."""
    lock = threading.Lock()
    reader = channel.makefile("r", encoding="utf-8")

    def answer(message):
        kind = message.get("kind")
        details = {k: v for k, v in message.items() if k not in ("id", "kind")}
        peer = details.get("peer") if isinstance(details.get("peer"), list) else []
        if kind == "taint":  # the proxy saw untrusted content arrive; it waits for this answer before going on
            result = {"taint": taint.mark(details.get("source")) if taint is not None else {"tainted": False}}
        elif kind == "verified":
            if broker is not None:
                broker.prove(details.get("address"), details.get("port", 0), details.get("name"), details.get("ok"))
            return  # nobody waits for an answer
        elif kind == "who":
            result = {"process": {"unknown": "no attribution here"}} if who is None else \
                who.lookup("tcp", *peer[:2]) if len(peer) >= 2 else {"process": {"unknown": "no peer given"}}
        elif kind in ("connect", "push", "http") and broker is not None:
            result = broker.ask(kind, details)
        elif kind == "canary":  # found by the proxy: for the launcher (canary.py)
            result = {"ok": canary_event(details)}
        else:
            result = {"decision": "deny", "reason": "asking is off" if broker is None else "unknown"}
        with lock, contextlib.suppress(OSError):
            channel.sendall((json.dumps({"id": message.get("id"), **result}) + "\n").encode())
    for line in reader:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            try:
                threading.Thread(target=answer, args=(message,), name="proxy-ask", daemon=True).start()
            except RuntimeError:  # no thread to be had (the sandbox's process limit): refused at once
                if message.get("kind") != "verified":  # nobody waits for that one
                    with lock, contextlib.suppress(OSError):
                        channel.sendall((json.dumps({"id": message.get("id"), "decision": "deny",
                                                     "reason": "no thread to answer it (process limit)"}) + "\n").encode())
                time.sleep(0.05)


class Pending:
    """An answer of the supervisor still to come (see AskChannel.request)."""

    def __init__(self, channel, ident):
        self.channel, self.ident, self.event, self.answer = channel, ident, threading.Event(), None

    def result(self, timeout):
        """The answer, or None if it did not come within TIMEOUT seconds (it is not waited for again)."""
        self.event.wait(timeout)
        self.channel.waiting.pop(self.ident, None)
        return self.answer


ASK_SEND_SECONDS = 10  # a message to the supervisor goes out within this time, else its request fails


class AskChannel:
    """The proxy's end: asks the supervisor and waits for the answer (several threads at once)."""

    def __init__(self, sock, timeout=APPROVAL_SECONDS + 10):
        self.sock, self.timeout, self.lock, self.waiting, self.counter = sock, timeout, threading.Lock(), {}, 0
        # STALLED: the last send did not go out in time (the supervisor reads nothing); CUT: a line went out only
        # in part, so the next message first ends it (the supervisor skips the broken line).
        self.stalled, self.cut = False, False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.sock.makefile("r", encoding="utf-8"):
            with contextlib.suppress(ValueError):
                message = json.loads(line)
                slot = self.waiting.get(message.get("id"))
                if slot:
                    slot.answer = message
                    slot.event.set()

    def _send(self, data):
        """Send DATA (under the lock); False if it did not go out whole. The lock is held at most ASK_SEND_SECONDS
        (not at all again while a send stalled and the supervisor still reads nothing): a supervisor that stops
        reading fails the requests instead of holding every connection that asks. The socket itself stays
        blocking for the reader: each send is non-blocking on its own (MSG_DONTWAIT)."""
        view = memoryview(b"\n" + data if self.cut else data)
        deadline = time.monotonic() + (0 if self.stalled else ASK_SEND_SECONDS)
        try:
            while view:
                try:
                    sent = self.sock.send(view, socket.MSG_DONTWAIT)
                except (BlockingIOError, InterruptedError):
                    if not select.select([], [self.sock], [], max(0.0, deadline - time.monotonic()))[1]:
                        self.stalled = True
                        return False
                    continue
                self.cut, view = True, view[sent:]
        except OSError:
            return False
        self.stalled = self.cut = False
        return True

    def request(self, kind, details):
        """Send a request; its Pending answer. A broken or stalled channel gives one that is already there: None."""
        with self.lock:
            self.counter += 1
            slot = self.waiting[self.counter] = Pending(self, self.counter)
            sent = self._send((json.dumps({"id": self.counter, "kind": kind, **details}) + "\n").encode())
        if not sent:
            slot.event.set()  # no answer will come: nobody waits for one
        return slot

    def __call__(self, kind, details):
        return self.request(kind, details).result(self.timeout) or {"decision": "deny", "reason": "no answer"}

    def send(self, kind, details):
        """Tell the supervisor something; no answer is waited for."""
        with self.lock:
            self._send((json.dumps({"id": None, "kind": kind, **details}) + "\n").encode())


BROWSER_SOCKET = "/sandbox/browser/open.sock"
# macOS (config.json "machine"): the host's browser socket would not cross podman machine's shared folders. The
# supervisor listens at BROWSER_SOCKET itself (a tmpfs the agent may not write) and hands each request to the host as
# a file in CONTROL_DIR/BROWSER_REQUESTS (approvals.serve_browser_files), the answer coming back the same way.
BROWSER_REQUESTS = "browser"
BROWSER_REQUEST_LIMIT = 8192
BROWSER_WAIT = 10


BROWSER_AT_ONCE = 8


def browser_relay(socket_path=BROWSER_SOCKET, directory=None, stop=None, wait=BROWSER_WAIT):
    """macOS: serve the sandbox's xdg-open on SOCKET_PATH through request files in DIRECTORY (see BROWSER_REQUESTS)
    until STOP is set."""
    directory = Path(directory or CONTROL_DIR / BROWSER_REQUESTS)
    directory.mkdir(mode=0o700, exist_ok=True)
    server = listen_unix(socket_path)
    server.settimeout(0.5)
    counter = itertools.count(1)
    # A few at once: each holds a request file in the host's run directory until the host answers (a flood of them
    # by the agent stays this small).
    slots = threading.BoundedSemaphore(BROWSER_AT_ONCE)

    def forward(connection, number):
        try:
            browser_forward(connection, directory, number, wait)
        finally:
            slots.release()
    while stop is None or not stop.is_set():
        try:
            connection, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        if not slots.acquire(blocking=False):
            connection.close()  # too many waiting already: this one is refused
            continue
        try:
            threading.Thread(target=forward, args=(connection, next(counter)), daemon=True).start()
        except RuntimeError:  # no thread to be had (the sandbox's process limit): this request only
            slots.release()
            connection.close()
    server.close()


def browser_forward(connection, directory, number, wait=BROWSER_WAIT):
    """One request of the sandbox's xdg-open: its line to DIRECTORY/ID.request, the host's DIRECTORY/ID.reply back."""
    with connection:
        reply = b'{"ok": false, "message": "sandbox-ai: the host did not take the request"}\n'
        try:
            connection.settimeout(3)
            line = connection.makefile("rb").readline(BROWSER_REQUEST_LIMIT + 1)
            if not line.endswith(b"\n") or len(line) > BROWSER_REQUEST_LIMIT:
                raise ValueError("request too long or cut off")
            ident = f"{os.getpid()}-{number}-{secrets.token_hex(4)}"
            write_new(directory / f"{ident}.request", line.decode("utf-8", "replace"), 0o600)
            answer, deadline = directory / f"{ident}.reply", time.monotonic() + wait
            while time.monotonic() < deadline:
                if answer.exists():
                    with open(answer, "rb") as stream:
                        reply = stream.read(BROWSER_REQUEST_LIMIT).split(b"\n", 1)[0] + b"\n"
                    answer.unlink()
                    break
                time.sleep(0.1)
            else:
                with contextlib.suppress(OSError):
                    (directory / f"{ident}.request").unlink()
        except (OSError, ValueError) as exc:
            reply = (json.dumps({"ok": False, "message": f"sandbox-ai: {str(exc)[:200]}"}) + "\n").encode()
        with contextlib.suppress(OSError):
            connection.sendall(reply)


def open_request(arguments, socket_path=BROWSER_SOCKET, out=None):
    """xdg-open in the sandbox: ask the human (on the host) to open an http(s) URL; 0 if the request
    reached them. Nothing is opened without their answer."""
    out = out or sys.stderr
    urls = [a for a in arguments if not a.startswith("-")]
    if len(urls) != 1 or not re.fullmatch(r"https?://[!-~]{1,4000}", urls[0]):
        print("sandbox-ai: only one http(s) URL can be opened in your browser", file=out)
        return 1
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(socket_path)
            client.sendall((json.dumps({"url": urls[0]}) + "\n").encode())
            answer = json.loads(client.makefile("r").readline() or "{}")
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai: cannot reach the host to open the URL ({exc}); open it yourself:\n{urls[0]}", file=out)
        return 1
    print(answer.get("message", "sandbox-ai: asked the human to open the URL"), file=out)
    return 0 if answer.get("ok") else 1


# --- canaries --------------------------------------------------------------------------------------
#
# Decoy files the launcher planted for the run (canary.py: fake credentials with tokens made for this run).
# The supervisor watches the files (inotify: opened, read, changed, deleted, renamed) and the names its DNS
# stub is asked for; the proxy searches every request (request line, headers, body, the name it connects
# to) for the tokens, in the forms the launcher listed (plain, URL-encoded, base64, hex, JSON-escaped). The
# lists reach them through directories the agent cannot see (CONTROL_DIR/canary.json, proxy.json); what they
# find goes to CONTROL_DIR/canary-events.jsonl, which the launcher follows (notification, pause, stop).

CANARY_CONFIG = "canary.json"
CANARY_EVENTS = "canary-events.jsonl"
# inotify bits on a canary file and on its directory (the name: deleted, renamed, replaced), as event names
CANARY_FILE_EVENTS = {0x20: "read", 0x1: "read", 0x2: "modify", 0x4: "modify", 0x400: "delete", 0x800: "move"}
CANARY_DIR_EVENTS = {0x200: "delete", 0x40: "move", 0x80: "replace", 0x100: "replace"}
CANARY_REPEAT = 5.0          # seconds: one event of a kind per canary within that time
CANARY_INFLATE = 4 << 20     # bytes of a compressed request body searched per piece


def canary_event(record, path=None):
    """Append RECORD (an event of a canary) to the launcher's CONTROL_DIR/canary-events.jsonl; True if written."""
    path = Path(path) if path is not None else CONTROL_DIR / CANARY_EVENTS
    line = json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError:
        return False
    try:
        os.write(fd, line.encode())
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


class CanaryRefused(Exception):
    """A request that carries a canary token: refused before the rest of it reaches the server."""

    def __init__(self, hit):
        super().__init__(f"canary {hit.get('canary')}")
        self.hit = hit


class CanaryMatcher:
    """Finds a canary token in data: FORMS [[text, canary id, encoding]] (the launcher's list); LOWER for host
    names, where case means nothing."""

    def __init__(self, forms=(), lower=False):
        self.forms, self.lower = {}, lower
        for entry in forms or []:
            if not isinstance(entry, (list, tuple)) or len(entry) != 3 or not isinstance(entry[0], str):
                continue
            needle = (entry[0].lower() if lower else entry[0]).encode()
            if len(needle) >= 8:  # never a short piece that ordinary traffic carries
                self.forms.setdefault(needle, {"canary": str(entry[1]), "encoding": str(entry[2])})
        self.longest = max(map(len, self.forms), default=0)
        self.pattern = re.compile(b"|".join(re.escape(form) for form in sorted(self.forms, key=len, reverse=True))) \
            if self.forms else None

    def __bool__(self):
        return self.pattern is not None

    def find(self, data):
        """{"canary", "encoding"} of the first token in DATA (bytes or text), or None."""
        if self.pattern is None or not data:
            return None
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        match = self.pattern.search(data.lower() if self.lower else data)
        return dict(self.forms[match.group()]) if match else None

    def name(self, name):
        """A token in the host name NAME: as it is, or across labels (the dots taken out)."""
        name = str(name or "").lower().rstrip(".")
        return self.find(name) or self.find(name.replace(".", "")) if name else None

    def scan(self, parts, headers, hit):
        """PARTS ((raw, payload) of a body) passed on unchanged, each searched first together with the end of the
        part before (a token across two parts is found too) and, for a gzip or deflate body, uncompressed. HIT
        is called with what was found before that part is passed on; it raises to stop the request there."""
        keep = max(self.longest - 1, 0)
        encoding = (header(headers, "content-encoding") or "").lower() if headers is not None else ""
        inflate = zlib.decompressobj(47) if encoding in ("gzip", "x-gzip", "deflate") else None
        tail = plain_tail = b""
        for raw, payload in parts:
            window = tail + payload
            found = self.find(window)
            if inflate is not None:
                try:
                    plain = plain_tail + inflate.decompress(payload, CANARY_INFLATE)
                except zlib.error:
                    inflate, plain = None, b""
                if found is None and plain:
                    found = self.find(plain)
                    if found is not None:
                        found["encoding"] += f"+{encoding}"
                plain_tail = plain[-keep:] if keep else b""
            if found is not None:
                hit(found)
            tail = window[-keep:] if keep else b""
            yield raw, payload


class Canaries:
    """The supervisor's side: the planted files watched, host names checked, events reported."""

    FILE_MASK = 0x1 | 0x2 | 0x4 | 0x20 | 0x400 | 0x800 | 0x02000000            # ... | IN_DONT_FOLLOW
    DIR_MASK = 0x40 | 0x80 | 0x100 | 0x200 | 0x02000000 | 0x01000000          # ... | IN_ONLYDIR

    def __init__(self, config, directory=None, who=None):
        self.files = [entry for entry in config.get("files", []) if isinstance(entry, dict)
                      and isinstance(entry.get("path"), str) and isinstance(entry.get("id"), str)]
        self.names = CanaryMatcher(config.get("names", []), lower=True)
        self.events = Path(directory if directory is not None else CONTROL_DIR) / CANARY_EVENTS
        self.stopping, self.thread = threading.Event(), None
        self.who = who  # Attribution: which process holds a canary file open right after inotify reported it

    @classmethod
    def load(cls, directory=None, who=None):
        """The canaries of this run (CONTROL_DIR/canary.json), or None without any."""
        directory = Path(directory if directory is not None else CONTROL_DIR)
        try:
            config = json.loads((directory / CANARY_CONFIG).read_text())
        except (OSError, ValueError):
            return None
        return cls(config, directory, who) if isinstance(config, dict) else None

    def opened_by(self, entry, action):
        """{"process": ...} for a file event: the process(es) that hold the canary open now, looked up at once in
        /proc (best effort: inotify does not say who; a reader that already closed it is "unknown")."""
        if self.who is None:
            return {"process": {"unknown": "no process lookup here"}}
        if action in ("delete", "move", "replace"):
            return {"process": {"unknown": "inotify does not say who; the file's name changed, nothing holds it"}}
        if not self.who.allowed():
            return {"process": {"unknown": "too many lookups"}}
        try:
            found = self.who.openers(entry["path"])
        except Exception:  # noqa: BLE001 - the lookup is a courtesy; the event is reported either way
            found = []
        if not found:
            return {"process": {"unknown": "no process held the file open when it was looked up (closed already)"}}
        return {"process": found[0], **({"processes": found} if len(found) > 1 else {})}

    def report(self, record):
        return canary_event(record, self.events)

    def lookup(self, name, found=None):
        """A token in a name the DNS stub was asked for (the lookup failed, but the name left the agent)."""
        hit = self.names.name(name)
        if hit:
            self.report({"event": "send", "source": "dns", "host": printable_text(name, 300), **hit, **(found or {})})
        return hit

    def start(self):
        if self.files:
            self.thread = threading.Thread(target=self.watch, args=(self.stopping,), daemon=True)
            self.thread.start()
        return self

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(3)

    def watch(self, stop, libc=None):
        """Report what happens to the files until STOP: read (opened or read), modify, delete, move, replace (a new
        file under its name). Who did it inotify does not say: the processes holding the file open right after the
        event are looked up (opened_by); one event of a kind per file every few seconds."""
        libc = libc or ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(0o2000000)
        if fd < 0:
            self.report({"event": "error", "source": "files", "error": "inotify unavailable: canary files unwatched"})
            return
        files, dirs = {}, {}
        for entry in self.files:
            wd = libc.inotify_add_watch(fd, os.fsencode(entry["path"]), self.FILE_MASK)
            if wd < 0:
                self.report({"event": "error", "source": "files", "canary": entry["id"], "path": entry["path"],
                             "error": f"not watched: {os.strerror(ctypes.get_errno() or 2)}"})
                continue
            files[wd] = entry
            parent, name = os.path.split(entry["path"])
            wd = libc.inotify_add_watch(fd, os.fsencode(parent), self.DIR_MASK)
            if wd >= 0:
                dirs.setdefault(wd, {})[name] = entry
        recent = {}
        try:
            while not stop.is_set():
                if not select.select([fd], [], [], 0.2)[0]:
                    continue
                data, pos = os.read(fd, 65536), 0
                while pos + 16 <= len(data):
                    wd, mask, _cookie, size = struct.unpack_from("iIII", data, pos)
                    name = data[pos + 16:pos + 16 + size].rstrip(b"\0").decode("utf-8", "replace")
                    pos += 16 + size
                    if mask & 0x4000:
                        self.report({"event": "error", "source": "files", "error": "inotify queue overflow"})
                        continue
                    entry, table = (files.get(wd), CANARY_FILE_EVENTS) if wd in files else \
                        (dirs.get(wd, {}).get(name), CANARY_DIR_EVENTS)
                    action = next((label for bit, label in table.items() if mask & bit), None)
                    if entry is None or action is None:
                        continue
                    now = time.monotonic()
                    if now - recent.get((entry["id"], action), -CANARY_REPEAT) < CANARY_REPEAT:
                        continue
                    recent[(entry["id"], action)] = now
                    self.report({"event": action, "source": "files", "canary": entry["id"], "path": entry["path"],
                                 **self.opened_by(entry, action)})
        finally:
            os.close(fd)


def canary_matchers(server):
    """(request forms, host-name forms, block, model hosts) of SERVER's proxy.json "canary", built once."""
    cached = getattr(server, "_canary", None)
    if cached is None:
        entry = server.config.get("canary") or {}
        cached = server._canary = (CanaryMatcher(entry.get("forms")), CanaryMatcher(entry.get("names"), lower=True),
                                   entry.get("block", True) is not False,
                                   {str(h).lower() for h in entry.get("model_hosts", [])})
    return cached


def canary_found(server, hit, where, entry, record, origin=None):
    """Report HIT (where the token was: url, header, body, sni, push); CanaryRefused when the run blocks. A token in
    a request to the agent's model provider (ORIGIN, the server the proxy verified) entered the agent's
    conversation: reported as context, never blocked here."""
    _, _, block, models = canary_matchers(server)
    context = where in ("url", "header", "body") and str(origin or "").lower() in models
    event = {"event": "context" if context else "send", "source": "proxy", "where": where, **hit,
             **{key: entry[key] for key in ("url", "method", "destination", "host") if key in entry}}
    seen = server.__dict__.setdefault("_canary_seen", set())
    if not context or (hit.get("canary"), "context") not in seen:
        seen.add((hit.get("canary"), "context" if context else where))
        if server.who is not None:
            with contextlib.suppress(Exception):
                server.who.request("canary", {**event, **server.asked_by(dict(record))})
    entry["canary"] = {**hit, "where": where, **({"context": True} if context else {})}
    if block and not context:
        raise CanaryRefused({**hit, "where": where})


def canary_request(server, head, parts, headers, entry, record, origin=None):
    """PARTS of a request whose HEAD carried no token (else CanaryRefused), searched as they pass."""
    forms, names, _, _ = canary_matchers(server)
    if not forms and not names:
        return parts
    found, where = forms.find(head.split(b"\r\n", 1)[0]), "url"
    if found is None:
        found, where = forms.find(head) or names.name(header(headers, "host")), "header"
    if found is not None:
        canary_found(server, found, where, entry, record, origin)
    return forms.scan(parts, headers, lambda hit: canary_found(server, hit, "body", entry, record, origin)) \
        if forms else parts


def canary_refusal(server, client, refused, entry):
    """Answer a request CanaryRefused stopped and log it."""
    message = b"sandbox-ai: refused: this request carries a credential that must not leave the sandbox\n"
    with contextlib.suppress(OSError):
        send_all(client, b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                 + f"Content-Length: {len(message)}\r\n\r\n".encode() + message)
    server.log({**entry, "event": "denied", "reason": f"a canary token ({refused.hit.get('canary')}, "
                f"{refused.hit.get('encoding')}) in the request's {refused.hit.get('where')}",
                "canary": refused.hit})


def canary_sni(server, client, name, record):
    """True (refused, logged) if the name a TLS client asks for carries a token and the run blocks."""
    _, names, _, _ = canary_matchers(server)
    hit = names.name(name) if names and name else None
    if hit is None:
        return False
    entry = {**record, "host": name}
    try:
        canary_found(server, hit, "sni", entry, record)
    except CanaryRefused:
        server.refuse(client, {**entry, "event": "denied", "reason": f"a canary token ({hit['canary']}) in the "
                               "name the client asked for", "canary": {**hit, "where": "sni"}}, reset=True)
        return True
    return False


def canary_push(server, verdict, entry, record):
    """Report the canaries the push check found in a push (it refuses the push itself when the run blocks)."""
    for ident in verdict.get("canary") or []:
        if server.who is not None:
            with contextlib.suppress(Exception):
                server.who.request("canary", {"event": "send", "source": "push", "where": "push", "canary": str(ident),
                                              "encoding": "git", "url": entry.get("url"),
                                              **server.asked_by(dict(record))})
        entry["canary"] = {"canary": str(ident), "where": "push"}


def stop_process(process, grace=2):
    """Stop PROCESS: close its stdin (the proxy exits on that), give it GRACE seconds, then signal it. A process
    of another user may not be signalled without CAP_KILL: it ends with the sandbox's PID namespace at the latest."""
    if not process or process.poll() is not None:
        return
    if process.stdin is not None:
        with contextlib.suppress(OSError):
            process.stdin.close()
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except (PermissionError, subprocess.TimeoutExpired):
        pass


GIT_TOKENS = Path("/sandbox/session/git-tokens.json")
GIT_CREDENTIAL_LIMIT = 1 << 16


def git_credential(operation, stream=None, out=None, path=None):
    """Git's credential helper for logins added during the run (`sandbox-ai net allow --git-token-*`, which writes
    PATH on the host; read-only here). Answers "get" for the origin (protocol://host[:port]) of a login, nothing
    else; "store" and "erase" are ignored: the agent cannot change what the human gave. Always 0: no login known
    means Git asks its next helper."""
    if operation != "get":
        return 0
    stream, out = stream or sys.stdin, out or sys.stdout
    asked = {}
    for line in stream.read(GIT_CREDENTIAL_LIMIT).splitlines():
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            asked[key] = value
    protocol, host = asked.get("protocol", "").lower(), asked.get("host", "").lower()
    if protocol not in ("http", "https") or not host:
        return 0
    default = {"http": ":80", "https": ":443"}[protocol]
    origin = f"{protocol}://{host.removesuffix(default)}"
    try:
        with open(path or GIT_TOKENS, encoding="utf-8") as tokens:
            logins = json.loads(tokens.read(GIT_CREDENTIAL_LIMIT)).get("logins", [])
    except (OSError, ValueError, AttributeError):
        return 0
    for login in logins if isinstance(logins, list) else []:
        if not isinstance(login, dict) or login.get("url") != origin:
            continue
        user, token = login.get("user"), login.get("token")
        if not all(isinstance(v, str) and v and "\n" not in v and "\r" not in v and "\0" not in v for v in (user, token)):
            return 0
        if asked.get("username") and asked["username"] != user:
            return 0  # the remote names another user: not this login
        out.write(f"username={user}\npassword={token}\n")
        return 0
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--internal-bootstrap":
            result = inner_bootstrap()
        elif len(sys.argv) > 2 and sys.argv[1] == "--internal-guard":
            result = inner_guard(json.loads(sys.argv[2]))
        elif len(sys.argv) > 4 and sys.argv[1] == "--internal-mcp-serve" and "--" in sys.argv[3:]:
            # SOCKET [--gateway BASE64-JSON] -- COMMAND (the command may hold a -- of its own)
            options, command = sys.argv[3:sys.argv.index("--", 3)], sys.argv[sys.argv.index("--", 3) + 1:]
            if not command or options and (len(options) != 2 or options[0] != "--gateway"):
                raise ValueError("usage: --internal-mcp-serve SOCKET [--gateway CONFIG] -- COMMAND")
            gateway = json.loads(base64.urlsafe_b64decode(options[1].encode())) if options else None
            # Not dumpable: the server it starts runs as the same user and must not reach the relay's stderr
            # (the records and questions) or its memory through /proc.
            with contextlib.suppress(OSError, AttributeError):
                ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
            # In a server sandbox the work directory is /work; a catalog server's bridge runs on the host.
            result = mcp_serve(sys.argv[2], command, cwd="/work" if os.path.isdir("/work") else "/", gateway=gateway)
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-mcp-bridge":
            result = mcp_bridge(sys.argv[2])
        elif len(sys.argv) in (4, 5, 6) and sys.argv[1] == "--internal-proxy":
            # LOG_FD READY_FD [CHANNEL_FD [--no-ask]]: --no-ask, the channel only says who connects
            result = proxy_main(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]) if len(sys.argv) >= 5 else None,
                                asking=sys.argv[5:] != ["--no-ask"])
        elif len(sys.argv) == 4 and sys.argv[1] == "--internal-hook":
            result = hook_client(sys.argv[2], sys.argv[3])
        elif len(sys.argv) == 5 and sys.argv[1] == "--internal-kit-hook":
            result = kit_hook(sys.argv[2], sys.argv[3], sys.argv[4])
        elif len(sys.argv) >= 2 and sys.argv[1] == "--internal-open":
            result = open_request(sys.argv[2:])
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-git-credential":
            result = git_credential(sys.argv[2])
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-attach-relay":
            result = attach_relay(sys.argv[2], ["tmux", "attach", "-t", "sandbox"])
        # macOS: the host's sandbox-ai reaches the sockets in podman machine's VM through `podman exec`.
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-attach-client":
            result = attach_client(sys.argv[2])
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-attach-probe":
            result = attach_probe(sys.argv[2])
        elif len(sys.argv) == 2 and sys.argv[1] == "--internal-control-client":
            result = control_client()
        else:
            print("sandbox-ai runtime: internal entry point; use sandbox-ai", file=sys.stderr)
            result = 2
        sys.exit(result if result >= 0 else 128 - result)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"agent-sandbox: {exc}", file=sys.stderr)
        sys.exit(1)
