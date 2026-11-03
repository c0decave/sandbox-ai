#!/usr/bin/env python3
"""sandbox-ai runtime: the code that runs inside the sandbox; standard library only.

Only this file is mounted into the sandbox (read-only, at /sandbox/runtime.py). It holds the
bootstrap that becomes PID 1 of the outer sandbox, the guard that verifies Firejail before the
agent starts, the egress and file loggers, the virtual display, the terminal relay for
detachable sessions and the MCP serve/bridge ends. The host launcher (agent_sandbox.py)
loads it for the few helpers both sides share, never the other way round.
"""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time

LOG_DIR = Path("/sandbox/log")
VNC_DIR = Path("/sandbox/vnc")
TMUX_DIR = "/sandbox/tmux"
ATTACH_SOCKET = f"{TMUX_DIR}/attach.sock"
GUI_CONFIG = Path("/sandbox/session/gui.json")
# Display helpers run without any capability or privilege gain.
DROP_ALL = ["setpriv", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs", "--"]
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
    server.bind(str(staging))
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


def mcp_serve(socket_path, command, cwd="/work"):
    """Server sandbox: one instance of the stdio MCP server per connection."""
    server = listen_unix(socket_path)
    while True:
        connection, _ = server.accept()
        def run(connection=connection):
            with connection:
                try:
                    subprocess.run(command, stdin=connection.fileno(), stdout=connection.fileno(), cwd=cwd, check=False)
                except OSError as exc:
                    print(f"agent-sandbox: MCP server failed: {exc}", file=sys.stderr)
        threading.Thread(target=run, daemon=True).start()


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
    subprocess.run(["nft", "-f", "/sandbox/session/firewall.nft"], check=True)
    # Installation failure aborts before Firejail or the agent starts.
    subprocess.run(["nft", "list", "table", "inet", "agent_sandbox"], check=True, stdout=subprocess.DEVNULL)
    before = namespace_state()
    cmd = firejail_command(before)
    loggers = start_loggers() if LOG_DIR.is_dir() else None
    gui = []
    try:
        if GUI_CONFIG.exists():
            gui = start_gui(json.loads(GUI_CONFIG.read_text()))
        return wait_child(subprocess.Popen(cmd, env=base_env(), close_fds=True))
    finally:
        for process in gui:
            stop_process(process)
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
            # -rfbport 0: no TCP listener; only the private Unix socket.
            processes.append(subprocess.Popen(DROP_ALL + ["x11vnc", "-display", cfg["display"], "-unixsock",
                                                          str(VNC_DIR / "vnc.sock"), "-rfbport", "0", "-forever",
                                                          "-shared", "-nopw", "-quiet", "-nosel"],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    except BaseException:
        for process in processes:
            stop_process(process)
        raise
    return processes


def log_event(name, record):
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    with open(LOG_DIR / name, "a") as stream:
        stream.write(json.dumps(record) + "\n")


def start_loggers():
    # Runs in PID 1 outside Firejail; the agent cannot see LOG_DIR.
    cfg = json.loads(Path("/sandbox/session/config.json").read_text())
    stop = threading.Event()
    stop.seen = set()
    stop.threads = []
    def denied_loop():
        while not stop.wait(1):
            poll_denied(stop.seen)
    for target, args in [(denied_loop, ()), (dns_logger, (set(cfg["pinned"]), 53, stop)),
                         (file_logger, (cfg["watch"], stop))]:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        stop.threads.append(thread)
    return stop


def poll_denied(seen):
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
                    log_event("egress-denied.jsonl", {"address": key[0], "protocol": key[1], "port": key[2]})


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


def dns_logger(pinned, port=53, stop=None):
    # Unpinned names never get an address; this stub only records the attempt.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    if stop is not None:
        sock.settimeout(0.2)
    while stop is None or not stop.is_set():
        try:
            query, peer = sock.recvfrom(4096)
            name, reply = dns_reply(query)
            sock.sendto(reply, peer)
            if name not in pinned:
                log_event("egress-denied.jsonl", {"host": name, "reason": "not in allowed hosts"})
        except (OSError, ValueError, IndexError, struct.error):
            continue
    sock.close()


def file_logger(roots, stop=None):
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(0o2000000)
    if fd < 0:
        log_event("files.jsonl", {"error": "inotify unavailable"})
        return
    mask = sum(INOTIFY_EVENTS) | 0x02000000 | 0x04000000  # DONT_FOLLOW, EXCL_UNLINK
    paths = {}
    exhausted = []
    def watch(top):
        if os.path.islink(top):
            return  # Swapped for a symlink: os.walk would follow it out of the watched tree.
        for directory, dirs, _ in os.walk(top):
            if exhausted:
                return
            wd = libc.inotify_add_watch(fd, os.fsencode(directory), mask)
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
    for root in roots:
        watch(root)
    recent = {}
    while True:
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
                continue
            path = os.path.join(paths.get(wd, "?"), name)
            if event & 0x40000000:  # directory
                if event & (0x100 | 0x80):
                    watch(path)
                if event & 0x18:
                    continue
            action = next((label for bit, label in INOTIFY_EVENTS.items() if event & bit), None)
            now = time.monotonic()
            if action and now - recent.get((action, path), -2) >= 2:
                recent[(action, path)] = now
                log_event("files.jsonl", {"action": action, "path": path})
    os.close(fd)


def firejail_command(before):
    help_text = subprocess.run(["firejail", "--help"], check=True, text=True, capture_output=True).stdout
    command = ["firejail", "--noprofile", "--quiet", "--caps.drop=all", "--nonewprivs", "--seccomp"]
    for private in (LOG_DIR, VNC_DIR):
        if private.is_dir():
            command.append(f"--blacklist={private}")
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
    cfg = json.loads(Path("/sandbox/session/config.json").read_text())
    if cfg["probe"]:
        print(json.dumps({"verified": True, "firejail": current, "outer": before}), flush=True)
        return 0
    env = base_env()
    env.update(cfg["env"])
    # Both tmux windows start here, after verification and inside Firejail.
    cmd = tmux_command(cfg["command"], detached=cfg["tmux"] == "detached") if cfg.get("tmux") else cfg["command"]
    os.chdir("/work")
    os.execvpe(cmd[0], cmd, env)


def stop_process(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--internal-bootstrap":
            result = inner_bootstrap()
        elif len(sys.argv) > 2 and sys.argv[1] == "--internal-guard":
            result = inner_guard(json.loads(sys.argv[2]))
        elif len(sys.argv) > 4 and sys.argv[1] == "--internal-mcp-serve" and sys.argv[3] == "--":
            result = mcp_serve(sys.argv[2], sys.argv[4:])
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-mcp-bridge":
            result = mcp_bridge(sys.argv[2])
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-attach-relay":
            result = attach_relay(sys.argv[2], ["tmux", "attach", "-t", "sandbox"])
        else:
            print("sandbox-ai runtime: internal entry point; use sandbox-ai", file=sys.stderr)
            result = 2
        sys.exit(result if result >= 0 else 128 - result)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"agent-sandbox: {exc}", file=sys.stderr)
        sys.exit(1)
