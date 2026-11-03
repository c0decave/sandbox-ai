#!/usr/bin/env python3
"""sandbox-ai runtime: the code that runs inside the sandbox; standard library only.

Only this file is mounted into the sandbox (read-only, at /sandbox/runtime.py). It holds the
bootstrap that becomes PID 1 of the outer sandbox, the guard that verifies Firejail before the
agent starts, the egress and file loggers, the virtual display, the terminal relay for
detachable sessions and the MCP serve/bridge ends. The host launcher (agent_sandbox.py)
loads it for the few helpers both sides share, never the other way round.
"""
import base64
import contextlib
import ctypes
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import sys
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
    try:
        subprocess.run(["nft", "-f", "/sandbox/session/firewall.nft"], check=True)
    except subprocess.CalledProcessError:
        with contextlib.suppress(OSError):
            if "type nat" in Path("/sandbox/session/firewall.nft").read_text():
                print("agent-sandbox: the connection proxy redirects with nftables NAT; load its modules on the "
                      "host with `sudo modprobe nft_chain_nat nft_redir` (or start with --no-proxy)",
                      file=sys.stderr)
        raise
    # Installation failure aborts before Firejail or the agent starts.
    subprocess.run(["nft", "list", "table", "inet", "agent_sandbox"], check=True, stdout=subprocess.DEVNULL)
    config = Path("/sandbox/session/config.json")
    cfg = json.loads(config.read_text()) if config.exists() else {}
    before = namespace_state()
    loggers = start_loggers() if LOG_DIR.is_dir() else None
    gui, proxy, control = [], None, None
    try:
        if cfg.get("proxy"):
            proxy = start_proxy(cfg["proxy"])
        if CONTROL_DIR.is_dir():
            base = json.loads((PROXY_DIR / "proxy.json").read_text()).get("allow", {}) if proxy else {}
            control = threading.Event()
            threading.Thread(target=control_server, daemon=True,
                             args=(CONTROL_DIR / "control.sock", base, PROXY_RUN / "allow.json" if proxy else None,
                                   control)).start()
        if GUI_CONFIG.exists():
            gui = start_gui(json.loads(GUI_CONFIG.read_text()))
        # Built only now: the blacklist names the proxy's directories, which exist from here on.
        cmd = firejail_command(before)
        return wait_child(subprocess.Popen(cmd, env=base_env(), close_fds=True))
    finally:
        for process in gui:
            stop_process(process)
        if proxy:
            stop_process(proxy)
        if control:
            control.set()
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
    loggers = [(denied_loop, ()), (dns_logger, (set(cfg["pinned"]), 53, stop))]
    if cfg["watch"]:  # file access only with an explicit --log-dir
        loggers.append((file_logger, (cfg["watch"], stop)))
    for target, args in loggers:
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
    for private in (LOG_DIR, VNC_DIR, PROXY_DIR, PROXY_RUN, CONTROL_DIR):
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


def allowed_destination(allow, address, port):
    """True if the firewall lets the proxy reach ADDRESS:PORT (ALLOW from the launcher's proxy.json)."""
    ip = ipaddress.ip_address(address)
    if ip.is_loopback:
        return port in allow.get("loopback", [])
    if allow.get("all"):
        return True
    if any(ip in ipaddress.ip_network(net, strict=False) for net in allow.get("nets", [])):
        return True
    if port == 443 and any(ip in ipaddress.ip_network(net, strict=False) for net in allow.get("https", [])):
        return True
    return any(ip == ipaddress.ip_address(a) and port == p for a, p in allow.get("tcp", []))


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


def send_all(sock, data):
    """sendall that also works on a non-blocking (TLS) socket."""
    view = memoryview(data)
    while view:
        try:
            view = view[sock.send(view):]
        except (ssl.SSLWantWriteError, BlockingIOError):
            select.select([], [sock], [], IDLE_TIMEOUT)
        except ssl.SSLWantReadError:
            select.select([sock], [], [], IDLE_TIMEOUT)


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


def host_matches(host, patterns):
    """True if HOST is one of PATTERNS (exact names, or *.domain for every name below domain)."""
    host = (host or "").lower().rstrip(".")
    return any(host == p or (p.startswith("*.") and host.endswith(p[1:])) for p in patterns)


class ProxyServer:
    """Connections of one sandbox run; one thread per connection."""

    def __init__(self, config, log_fd, checker=None, run_dir=PROXY_RUN, destination=original_destination,
                 upstream=None):
        self.config, self.log_fd, self.checker, self.run_dir = config, log_fd, checker, run_dir
        self.destination, self.lock, self.check_lock = destination, threading.Lock(), threading.Lock()
        self.counter, self.contexts, self.adverts, self.verified = 0, {}, {}, {}
        self.allow_file = Path(config.get("allow_file", "")) if config.get("allow_file") else None
        self.allow_mtime = None
        system = run_dir / "system.pem"
        self.upstream_cafile = str(system) if system.is_file() and system.stat().st_size else None
        self.upstream = upstream or ssl.create_default_context(cafile=self.upstream_cafile)
        self.upstream.set_alpn_protocols(["http/1.1"])

    # -- bookkeeping --

    def log(self, record):
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        with self.lock:
            os.write(self.log_fd, line)

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
        with self.lock:
            context = self.contexts.get(host)
        if context is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.set_alpn_protocols(["http/1.1"])
            context.load_cert_chain(str(server_certificate(self.run_dir, host)), str(self.run_dir / "leaf.key"))
            with self.lock:
                self.contexts[host] = context
        return context

    def ssh_refused(self, address):
        """Why SSH to ADDRESS is refused, or None: a push over SSH would bypass the privacy check."""
        ssh = self.config.get("ssh", {})
        if not ssh.get("block"):
            return None
        ip = ipaddress.ip_address(address)
        if str(ip) in ssh.get("public_git", []):
            return "SSH to a public Git host would bypass the privacy check of pushes; use HTTPS"
        allowed = ssh.get("allowed", []) + self.allow().get("ssh", [])  # + `sandbox-ai net allow`
        if any(ip in ipaddress.ip_network(net, strict=False) for net in allowed):
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
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def handle(self, client):
        with self.lock:
            self.counter += 1
            number = self.counter
        started = time.monotonic()
        record = {"connection": number}
        upstream = None
        try:
            address, port = self.destination(client)
            record["destination"] = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
            if not allowed_destination(self.allow(), address, port):
                self.log({**record, "event": "denied", "reason": "not allowed by the network options"})
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
            first = self.peek(client)
            if first.startswith(b"SSH-") and self.ssh_refused(address):
                self.log({**record, "event": "denied", "protocol": "ssh", "reason": self.ssh_refused(address)})
                return
            if not self.passthrough_allowed(first, address, port, record):
                return
            try:
                upstream = socket.create_connection((address, port), timeout=30)
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
            if first.startswith(b"SSH-") or server_first.startswith(b"SSH-"):
                reason = self.ssh_refused(address)
                if reason:
                    self.log({**record, "event": "denied", "protocol": "ssh", "reason": reason})
                    return
            kind, name = client_hello_sni(first)
            upstream.settimeout(IDLE_TIMEOUT)
            client.settimeout(IDLE_TIMEOUT)
            if first and first_bytes_state(first) == "more":
                self.log({**record, "event": "denied", "reason": "an incomplete TLS ClientHello, HTTP request "
                          "line or SSH banner"})
                return
            if kind == "tls":
                record["host"] = name
                if host_matches(name, self.config.get("no_inspect", [])):
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
            elif first and port in HTTP_PORTS:
                self.log({**record, "event": "denied", "reason": f"neither HTTP nor TLS on port {port}"})
            else:
                counts = [0, 0]
                relay(client, upstream, counts)
                self.log({**record, "event": "tcp",
                          "protocol": "ssh" if (first or server_first).startswith(b"SSH-") else None,
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
            self.verified[key] = time.monotonic() + 600
        return True

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

    def intercept(self, client, upstream, name, address, record, started, port=443):
        try:
            server = self.upstream.wrap_socket(upstream, server_hostname=name or address)
        except (ssl.SSLError, OSError) as exc:
            self.log({**record, "event": "tls-upstream-failed", "error": str(exc),
                      "hint": f"the server's certificate is not trusted; --no-inspect {name or address} passes "
                              "this connection through unopened"})
            return
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
        if stream.buffer and port in HTTP_PORTS:
            self.log({**record, "event": "denied", "reason": f"not HTTP inside TLS on port {port}"})
            return
        counts = [len(stream.buffer), len(greeting)]
        if stream.buffer:
            send_all(server, stream.buffer)
        if greeting:
            send_all(agent, greeting)
        relay(agent, server, counts)
        self.log({**record, "event": "tls", "bytes_out": counts[0], "bytes_in": counts[1],
                  "ms": int((time.monotonic() - started) * 1000)})

    def exchange(self, stream, client, upstream, scheme, origin, record):
        """HTTP/1.1 requests and responses on one connection, until either side ends it. ORIGIN is the
        server as the proxy knows it (the name its certificate was checked for, else address:port); the
        push check goes by it, never by the agent's Host header."""
        replies = Stream(upstream)
        limit = int(self.config.get("body_limit", 65536))
        while True:
            head = stream.head()
            if head is None:
                return
            started = time.monotonic()
            line, headers = parse_head(head)
            method, _, rest = line.partition(" ")
            target = rest.rpartition(" ")[0] or rest
            authority = header(headers, "host") or origin
            url = target if "://" in target else f"{scheme}://{authority}{target}"
            if "://" in target:
                send_all(client, b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                self.log({**record, "event": "denied", "method": method, "url": redact_text(target),
                          "reason": "requests through another proxy (absolute URLs) would hide where they go"})
                return
            if method.upper() == "CONNECT":
                send_all(client, b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                self.log({**record, "event": "denied", "method": method, "url": redact_text(target),
                          "reason": "tunnels through another proxy would hide what passes"})
                return
            # Where the request really goes: the server the proxy verified, with the path from the request
            # line. Never the agent's Host header.
            path = urllib.parse.unquote(target.split("?")[0].split("#")[0])
            path = re.sub(r"/+", "/", path).rstrip("/")
            checked = f"{scheme}://{origin}{path}"
            entry = {**record, "event": "http", "method": method, "url": redact_text(url)}
            framing = request_framing(headers)
            if (header(headers, "expect") or "").lower() == "100-continue":
                # Answered here, so that a body to check arrives before anything reaches the server.
                send_all(client, b"HTTP/1.1 100 Continue\r\n\r\n")
                head = re.sub(rb"(?im)^expect:[^\r\n]*\r\n", b"", head)
            pending, captured = [], Capture(limit)
            parts = stream.body(framing)
            content_type = (header(headers, "content-type") or "").split(";")[0].strip().lower()
            if (method.upper() == "POST" and path.endswith("git-receive-pack")) or \
                    content_type == "application/x-git-receive-pack-request":
                verdict, commands, capabilities = self.check_push(checked, headers, parts, pending, captured)
                entry["push"] = verdict
                if not verdict["ok"]:
                    with contextlib.suppress(ValueError, OSError):
                        for _, payload in parts:  # read the rest, so that Git reads our answer
                            captured.add(payload)
                    send_all(client, push_rejection(commands or [], verdict["message"], capabilities))
                    entry.update(status=200, request={"headers": redact_headers(headers),
                                                      "body": body_record(captured.data, captured.total, headers, limit)},
                                 ms=int((time.monotonic() - started) * 1000))
                    self.log(entry)
                    return
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
                    return
                status_line, reply_headers = parse_head(reply)
                try:
                    status = int(status_line.split(" ")[1])
                except (IndexError, ValueError):
                    raise ValueError("malformed HTTP status line") from None
                send_all(client, reply)
                if 100 <= status < 200 and status != 101:
                    continue
                break
            advert = method.upper() == "GET" and path.endswith("/info/refs") and \
                "service=git-receive-pack" in urllib.parse.unquote(target)
            key = checked.rsplit("/info/refs", 1)[0]
            answer = Capture(ADVERT_LIMIT if advert else limit)
            received = len(reply)
            closing = response_framing(method, status, reply_headers)
            for raw, payload in replies.body(closing):
                send_all(client, raw)
                received += len(raw)
                answer.add(payload)
            if advert and status == 200:
                with self.lock:
                    self.adverts[key] = advertised_refs(self.decoded(answer.data, reply_headers))
                    while len(self.adverts) > 64:
                        self.adverts.pop(next(iter(self.adverts)))
            entry.update(status=status, response={"headers": redact_headers(reply_headers),
                                                  "body": body_record(answer.data[:limit], answer.total,
                                                                      reply_headers, limit)},
                         bytes_out=sent, bytes_in=received, ms=int((time.monotonic() - started) * 1000))
            self.log(entry)
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

    @staticmethod
    def decoded(data, headers):
        if (header(headers, "content-encoding") or "").lower() in ("gzip", "x-gzip", "deflate"):
            with contextlib.suppress(zlib.error):
                return zlib.decompressobj(47).decompress(data, ADVERT_LIMIT)
        return data

    def check_push(self, url, headers, parts, pending, captured):
        """(verdict, commands, capabilities) of a receive-pack request; reads the command lines into PENDING."""
        repository = url.rsplit("/git-receive-pack", 1)[0]
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
        except (ValueError, zlib.error) as exc:
            return {"ok": False, "message": f"the push cannot be checked: {exc}"}, None, set()
        if self.checker is None:
            return {"ok": True, "message": "privacy check off"}, commands, capabilities
        with self.lock:
            advertised = sorted(self.adverts.get(repository, ()))
        request = {"url": repository, "commands": [list(c) for c in commands], "advertised": advertised}
        try:
            with self.check_lock:
                self.checker.stdin.write((json.dumps(request) + "\n").encode())
                self.checker.stdin.flush()
                verdict = json.loads(self.checker.stdout.readline() or b"null")
            if not isinstance(verdict, dict) or "ok" not in verdict:
                raise ValueError("no answer from the privacy check")
        except (OSError, ValueError) as exc:
            verdict = {"ok": False, "message": f"the privacy check is not available: {exc}"}
        verdict["refs"] = [ref for _, _, ref in commands]
        return verdict, commands, capabilities


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


def proxy_main(log_fd, ready_fd):
    """The proxy process: CA, listeners and push checker as root, then the proxy user for the rest."""
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
    for root, dirs, files in os.walk(PROXY_RUN):
        for name in [*dirs, *files]:
            os.chown(os.path.join(root, name), PROXY_UID, PROXY_UID)
    os.chown(PROXY_RUN, PROXY_UID, PROXY_UID)
    drop_to_proxy_user()
    server = ProxyServer(config, log_fd, checker)
    server.serve(listeners)
    os.write(ready_fd, b"1")
    os.close(ready_fd)
    return server.wait()


def start_proxy(cfg):
    """Start the proxy before the agent and make the agent's TLS libraries trust the session CA."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    log_fd = os.open(LOG_DIR / cfg["log"], flags, 0o600) if LOG_DIR.is_dir() else os.open(os.devnull, os.O_WRONLY)
    ready_r, ready_w = os.pipe()
    try:
        process = subprocess.Popen(["python3", "-I", "/sandbox/runtime.py", "--internal-proxy", str(log_fd),
                                    str(ready_w)], pass_fds=(log_fd, ready_w), env=base_env(),
                                   stdin=subprocess.DEVNULL)
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

# --- network changes during a run (sandbox-ai net) -------------------------------------------------
#
# The supervisor listens on CONTROL_DIR/control.sock, a host directory the agent cannot see. The host
# user's `sandbox-ai net allow|revoke` sends what to add or take back; the supervisor rewrites the
# firewall's "runtime" chain and the proxy's allow-list. Names reach /etc/hosts from the host side.

CONTROL_DIR = Path("/sandbox/control")


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
    return "\n".join(lines) + "\n"


def runtime_change(state, request):
    """STATE after REQUEST ({"op": "allow"|"revoke", "nets", "https", "tcp"}); every value validated."""
    if request.get("op") not in ("allow", "revoke"):
        raise ValueError("op must be allow or revoke")
    nets = {str(ipaddress.ip_network(str(v), strict=False)) for v in request.get("nets", [])}
    https = {str(ipaddress.ip_network(str(v), strict=False)) for v in request.get("https", [])}
    tcp = set()
    for address, port in request.get("tcp", []):
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError(f"bad port {port!r}")
        tcp.add((str(ipaddress.ip_address(str(address))), port))
    if any("%" in value for value in nets | https | {a for a, _ in tcp}):
        raise ValueError("scope IDs are not accepted")
    change = {"nets": nets, "https": https, "tcp": tcp}
    if request["op"] == "allow":
        return {key: state[key] | change[key] for key in state}
    return {key: state[key] - change[key] for key in state}


def merged_allow(base, state):
    """The proxy's allow-list: the launcher's plus what was allowed during the run."""
    allow = dict(base)
    allow["nets"] = [*base.get("nets", []), *sorted(state["nets"])]
    allow["https"] = [*base.get("https", []), *sorted(state["https"])]
    allow["tcp"] = [*base.get("tcp", []), *[[a, p] for a, p in sorted(state["tcp"])]]
    # Explicit destinations may also take SSH, as at the start (--allow, --allow-tcp).
    allow["ssh"] = sorted(state["nets"]) + sorted({a for a, _ in state["tcp"]})
    return allow


def control_server(path, base_allow, allow_file=None, stop=None, apply=None):
    """Serve `sandbox-ai net` requests on the Unix socket PATH until STOP is set."""
    def nft(script):
        subprocess.run(["nft", "-f", "-"], input=script, text=True, check=True, capture_output=True)
    apply = apply or nft
    state = {"nets": set(), "https": set(), "tcp": set()}
    listener = listen_unix(path)
    listener.settimeout(0.5)
    while stop is None or not stop.is_set():
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with connection:
            try:
                connection.settimeout(10)
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data)
                if request.get("op") != "show":
                    new = runtime_change(state, request)
                    apply(runtime_rules(new))
                    state = new
                    if allow_file is not None:
                        temporary = allow_file.with_name(".allow.json")
                        temporary.write_text(json.dumps(merged_allow(base_allow, state)))
                        temporary.chmod(0o644)
                        os.replace(temporary, allow_file)
                answer = {"ok": True, "nets": sorted(state["nets"]), "https": sorted(state["https"]),
                          "tcp": [[a, p] for a, p in sorted(state["tcp"])]}
            except (ValueError, TypeError, KeyError, AttributeError, OSError,
                    subprocess.CalledProcessError) as exc:
                answer = {"ok": False, "error": str(exc)[:500]}
            with contextlib.suppress(OSError):
                connection.sendall((json.dumps(answer) + "\n").encode())
    listener.close()


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
            # In a server sandbox the work directory is /work; a catalog server's bridge runs on the host.
            result = mcp_serve(sys.argv[2], sys.argv[4:], cwd="/work" if os.path.isdir("/work") else "/")
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-mcp-bridge":
            result = mcp_bridge(sys.argv[2])
        elif len(sys.argv) == 4 and sys.argv[1] == "--internal-proxy":
            result = proxy_main(int(sys.argv[2]), int(sys.argv[3]))
        elif len(sys.argv) == 3 and sys.argv[1] == "--internal-attach-relay":
            result = attach_relay(sys.argv[2], ["tmux", "attach", "-t", "sandbox"])
        else:
            print("sandbox-ai runtime: internal entry point; use sandbox-ai", file=sys.stderr)
            result = 2
        sys.exit(result if result >= 0 else 128 - result)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"agent-sandbox: {exc}", file=sys.stderr)
        sys.exit(1)
