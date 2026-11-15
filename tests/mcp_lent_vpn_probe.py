"""Runs as root in a network namespace of its own (tests/test_mcp_lent_vpn_live.py starts it with unshare): a start
behind a real WireGuard VPN that lends its tunnel to its own MCP servers, in the kernel - sandbox/vpn_runtime.py's own
`serve` on both sides (the start's VPN container lends, each server's borrows), the start's firewall as the launcher
writes it (agent_sandbox.firewall_rules), and its kill switch as the supervisor sets it (runtime.RuntimeNetwork.set_cut,
what `sandbox-ai net cut` reaches). Prints one JSON report.

  outside (this namespace)             start (the agent's pod: its VPN container lends)
  "the internet": 93.184.216.34        vv 192.168.50.2/24 (pasta's uplink)
  the start's model API 160.79.104.10  wg0 10.66.0.2 - the tunnel - wgs 10.66.0.1 (the VPN's server and resolver)
  the VPN endpoint 198.18.0.1:51820    the kill switch, the start's firewall (LEND_MARK after its cut chain)
  a public resolver 1.1.1.1
  each namespace's gateway             image (an MCP image server's VPN container: it borrows)
  (192.168.50.1, .60.1, .70.1:              sv 192.168.60.2/24, fd00:60::2/64; the server's process is in this
  "the LAN", "the host", a resolver)        namespace (--network=container:<the VPN container>)
                                       command (a command server's pod: its VPN container borrows, its sandbox has
                                            the launcher's firewall with the relay's rules and a connection proxy -
                                            here a stand-in that forwards as the proxy's user)
                                            cv 192.168.70.2/24

Each VPN "container" is `vpn_runtime.py serve` in its namespace with a /run of its own (a tmpfs, as the container
has) and the folder of the lent tunnel's socket mounted at /run/sandbox-ai-lend - writable for the lender, read-only
for a borrower. The folder, the run's token and the profile's lock are made by sandbox/vpn.py as a start makes them
(prepare_lending, lend_token, profile_lock).

Every service of the outside answers a TCP connection with "from ADDRESS" (who it saw), echoes each line, and writes
down who came and what was said: a connection through the tunnel comes from 10.66.0.2, one past it from the
namespace's own uplink address. Phases (each in the report; one that fails says so as {"error": ...} and the others
go on): "before" (the control: a server's namespace without anything - every way is open), "start" (the lender, the
lock, the token), "image" and "command" (what each server reaches, and from where), "agent" (the start's own
programs gain nothing from the lent mark), "cut MODE" for each mode (a connection opened before the cut, a new one,
a lookup; then the cut lifted), "idle" (a cut connection that says nothing), "lender killed" (the start's VPN
container is gone: the borrowers end, nothing leaves). Standard library only.
Usage: mcp_lent_vpn_probe.py VPN_RUNTIME_PY            (as root, in a fresh network namespace; vpn.py and
                                                       agent_sandbox.py are taken from beside the runtime)
       mcp_lent_vpn_probe.py --client VPN_RUNTIME_PY   (inside a namespace: attempts, one JSON line each)
       mcp_lent_vpn_probe.py --proxy PORT UID          (inside the command namespace: the proxy's stand-in)"""
import argparse
import contextlib
import errno
import importlib.util
import json
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
INTERNET, API = "93.184.216.34", "160.79.104.10"      # public addresses (the lender lends nothing else)
ENDPOINT, PUBLIC_DNS = "198.18.0.1", "1.1.1.1"
GATEWAYS = {"start": "192.168.50.1", "image": "192.168.60.1", "command": "192.168.70.1"}
OWN = {"start": "192.168.50.2", "image": "192.168.60.2", "command": "192.168.70.2"}
IMAGE6_GATEWAY, IMAGE6_OWN = "fd00:60::1", "fd00:60::2"
TUNNEL_SERVER, TUNNEL_CLIENT = "10.66.0.1", "10.66.0.2"
VPN_ANSWER, LAN_ANSWER = "198.51.100.7", "192.0.2.66"
PORT, API_PORT, UDP_PORT, WG_PORT = 8000, 443, 9999, 51820
RUN = "codex-0123456789ab"
REFUSED = {errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Failed(Exception):
    """A step of the probe failed: its phase says so in the report."""


def sh(*command, data=None, check=True):
    done = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and done.returncode != 0:
        raise Failed(f"{' '.join(map(str, command))[:200]} failed ({done.returncode}): "
                     f"{done.stderr.decode(errors='replace')[-600:]}")
    return done.stdout.decode(errors="replace")


class Lines:
    """The lines a process writes to its standard output, each waited for no longer than asked."""

    def __init__(self, stream):
        self.fd, self.buffer = stream.fileno(), b""

    def read(self, timeout):
        """The next line; None when none came in TIMEOUT seconds, "" when the output has ended."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            if select.select([self.fd], [], [], left)[0]:
                chunk = os.read(self.fd, 65536)
                if not chunk:
                    return ""
                self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line.decode(errors="replace")


# --- a server's (or the agent's) attempts: --client, inside its namespace -------------------------------------------

def read_line(sock):
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
    return data.decode(errors="replace").strip()


def failure(exc):
    if isinstance(exc, socket.timeout):
        return "timeout"
    if exc.errno in REFUSED:
        return "refused"
    if exc.errno in (errno.ECONNRESET, errno.EPIPE):
        return "closed"
    return f"error {errno.errorcode.get(exc.errno, exc.errno)}"


def reach(family, address, port, timeout=4.0):
    """What a TCP connection to ADDRESS:PORT comes to: "from ADDRESS" (the far end answered: who it saw), "closed"
    (taken and closed without an answer: the relay could not hand it on), "refused", "timeout"."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((address, port))
        return read_line(sock) or "closed"
    except OSError as exc:
        return failure(exc)
    finally:
        sock.close()


def udp(family, address, port):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"hello", (address, port))
        return "sent"
    except OSError as exc:
        return failure(exc)
    finally:
        sock.close()


def lookup(rt, server):
    try:
        return rt.dns_lookup("probe.example", server, 1, timeout=3.0)
    except (OSError, ValueError) as exc:
        return f"failed: {exc}"


def attempt(rt, kind, address, port=None):
    if kind == "dns":
        return lookup(rt, address)
    family = socket.AF_INET6 if kind.endswith("6") else socket.AF_INET
    return udp(family, address, port) if kind.startswith("udp") else reach(family, address, port)


def client(rt):
    """Commands on standard input, one JSON line each, each answered with one: "attempts" ({name: [kind, address,
    port]}: each tried), "open" (a connection kept under NAME: the far end's first line), "say" (a line on it: its
    echo, that it ended, or that nothing came back in WAIT seconds), "watch" (nothing is sent: whether it ends within
    WAIT seconds), "close"."""
    held = {}
    for raw in sys.stdin:
        try:
            command = json.loads(raw)
        except ValueError:
            continue
        op, name = command.get("op"), command.get("name")
        try:
            if op == "attempts":
                answer = {key: attempt(rt, *given) for key, given in command["tries"].items()}
            elif op == "open":
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                try:
                    sock.connect((command["address"], command["port"]))
                    answer = {"first": read_line(sock) or "closed"}
                    held[name] = sock
                except OSError as exc:
                    sock.close()
                    answer = {"first": failure(exc)}
            elif op == "say":
                sock = held[name]
                sock.settimeout(command.get("wait", 5))
                try:
                    sock.sendall(command["text"].encode() + b"\n")
                    line = read_line(sock)
                    answer = {"echo": line} if line else {"ended": "closed"}
                except socket.timeout:
                    answer = {"silent": True}
                except OSError as exc:
                    answer = {"ended": failure(exc)}
            elif op == "watch":
                sock = held[name]
                sock.settimeout(command.get("wait", 5))
                try:
                    data = sock.recv(100)
                    answer = {"data": data.decode(errors="replace")} if data else {"ended": "closed"}
                except socket.timeout:
                    answer = {"silent": True}
                except OSError as exc:
                    answer = {"ended": failure(exc)}
            elif op == "close":
                with contextlib.suppress(OSError, KeyError):
                    held.pop(name).close()
                answer = {"closed": name}
            else:
                answer = {"error": f"unknown command {op!r}"}
        except Exception as exc:  # noqa: BLE001 - said, the next command is served
            answer = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(answer), flush=True)


class Client:
    """The attempts' process in a namespace (INSIDE: the nsenter prefix)."""

    def __init__(self, inside, runtime_path):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen([*inside, sys.executable, "-I", os.path.abspath(__file__), "--client",
                                         runtime_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors)
        self.lines = Lines(self.process.stdout)

    def ask(self, timeout=90, **command):
        self.process.stdin.write(json.dumps(command).encode() + b"\n")
        self.process.stdin.flush()
        line = self.lines.read(timeout)
        if not line:
            self.errors.seek(0)
            raise Failed(f"the client {'ended' if line == '' else 'did not answer'} at {command}: "
                         f"{self.errors.read()[-600:].decode(errors='replace')}")
        return json.loads(line)

    def tries(self, **tries):
        return self.ask(op="attempts", tries=tries)

    def close(self):
        with contextlib.suppress(OSError):
            self.process.kill()


# --- the connection proxy's stand-in: --proxy, inside the command server's namespace ------------------------------

def pump(one, other):
    def copy(source, target):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                target.sendall(data)
        except OSError:
            pass
        for sock, how in ((target, socket.SHUT_WR), (source, socket.SHUT_RD)):
            with contextlib.suppress(OSError):
                sock.shutdown(how)
    back = threading.Thread(target=copy, args=(other, one), daemon=True)
    back.start()
    copy(one, other)
    back.join()
    for sock in (one, other):
        with contextlib.suppress(OSError):
            sock.close()


def forward(conn):
    """One connection the firewall's redirect handed here, on to where it was meant to go - as the proxy's user,
    whose connections the redirect leaves alone and the relay's rule lets by."""
    try:
        port, packed = struct.unpack_from("!2xH4s", conn.getsockopt(socket.SOL_IP, 80, 16))  # SO_ORIGINAL_DST
        out = socket.create_connection((socket.inet_ntoa(packed), port), timeout=10)
        out.settimeout(None)
    except OSError:
        conn.close()
        return
    pump(conn, out)


def proxy(port, uid):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(64)
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
    print("ready", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=forward, args=(conn,), daemon=True).start()


# --- the outside: who arrives where, and what they say ---------------------------------------------------------------

def plain(address):
    return address.split("%")[0].removeprefix("::ffff:")


class FarEnd:
    def __init__(self):
        self.lock, self.seen = threading.Lock(), []

    def add(self, kind, peer, local, *more):
        with self.lock:
            self.seen.append([kind, plain(peer), plain(local), *more])

    def take(self):
        time.sleep(0.3)  # what was sent has arrived
        with self.lock:
            seen, self.seen = self.seen, []
        return seen

    def tcp(self, family, port):
        server = socket.socket(family, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        server.bind(("::" if family == socket.AF_INET6 else "0.0.0.0", port))
        server.listen(64)
        threading.Thread(target=self.accept, args=(server, port), daemon=True).start()

    def accept(self, server, port):
        while True:
            conn, peer = server.accept()
            who, where = plain(peer[0]), plain(conn.getsockname()[0])
            self.add("tcp", who, where, port)
            threading.Thread(target=self.talk, args=(conn, who, where, port), daemon=True).start()

    def talk(self, conn, who, where, port):
        """Says who it sees, then echoes each line (and writes it down)."""
        with conn:
            try:
                conn.sendall(f"from {who}\n".encode())
                buffer = b""
                while True:
                    data = conn.recv(4096)
                    if not data:
                        return
                    buffer += data
                    while b"\n" in buffer:
                        line, _, buffer = buffer.partition(b"\n")
                        self.add("line", who, where, port, line.decode(errors="replace"))
                        conn.sendall(b"echo " + line + b"\n")
            except OSError:
                return


# --- a VPN container: vpn_runtime.py serve in a namespace -----------------------------------------------------------

class Serve:
    """`vpn_runtime.py serve` in the namespace INSIDE names, as its VPN container runs it: a /run of its own and the
    folder of the lent tunnel's socket at /run/sandbox-ai-lend (READ_ONLY: a borrower's)."""

    def __init__(self, inside, runtime_path, payload, lend_dir, read_only):
        script = ('mount -t tmpfs tmpfs /run && mkdir -p /run/sandbox-ai-lend && mount --bind "$1" /run/sandbox-ai-lend'
                  + (" && mount -o remount,bind,ro /run/sandbox-ai-lend" if read_only else "")
                  + ' && shift && exec "$@"')
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen([*inside, "unshare", "--mount", "--propagation", "private", "sh", "-c", script,
                                         "sh", str(lend_dir), sys.executable, "-I", runtime_path, "serve"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)
        self.process.stdin.write(json.dumps(payload).encode() + b"\n")
        self.process.stdin.flush()
        self.lines, self.events = Lines(self.process.stdout), []

    def said(self):
        self.errors.seek(0)
        return self.errors.read()[-1500:].decode(errors="replace")

    def until(self, kinds, timeout):
        """Its events up to the first of KINDS (kept in self.events); Failed when none comes in TIMEOUT seconds."""
        deadline = time.monotonic() + timeout
        while True:
            line = self.lines.read(max(0.0, deadline - time.monotonic()))
            if not line:
                raise Failed(f"the VPN container {'ended' if line == '' else 'said nothing more'} before "
                             f"{' or '.join(kinds)}: its events {shown(self.events)}; its errors: {self.said()}")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.events.append(event)
            if event.get("event") in kinds:
                return event

    def up(self):
        event = self.until(("ready", "error"), 120)
        if event.get("event") != "ready":
            raise Failed(f"the VPN container did not come up: {event.get('message')}; its errors: {self.said()}")
        return event

    def ended(self, timeout):
        """Its exit status once it ended within TIMEOUT seconds (None: it still runs), with its last events read."""
        try:
            status = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            status = None
        while True:
            line = self.lines.read(0.2)
            if not line:
                break
            with contextlib.suppress(ValueError):
                self.events.append(json.loads(line))
        return status

    def close(self):
        with contextlib.suppress(OSError):
            self.process.kill()


def shown(events):
    return [{key: value for key, value in event.items() if key != "checks"} for event in events]


def namespace(base, name, gateway, own, device):
    """A network namespace of its own with an uplink to the outside (a veth pair): (its process, the nsenter prefix)."""
    child = base.namespace()
    inside = ["nsenter", f"--net=/proc/{child.pid}/ns/net"]
    sh("ip", "link", "add", f"{device}o", "type", "veth", "peer", "name", f"{device}v")
    sh("ip", "link", "set", f"{device}v", "netns", str(child.pid))
    sh("ip", "addr", "add", f"{gateway}/24", "dev", f"{device}o")
    sh("ip", "link", "set", f"{device}o", "up")
    sh(*inside, "ip", "link", "set", "lo", "up")
    sh(*inside, "ip", "addr", "add", f"{own}/24", "dev", f"{device}v")
    sh(*inside, "ip", "link", "set", f"{device}v", "up")
    sh(*inside, "ip", "route", "add", "default", "via", gateway, "dev", f"{device}v")
    return child, inside


def main(runtime_path):
    home = tempfile.mkdtemp(prefix="lent-")
    for variable, folder in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                             ("XDG_STATE_HOME", "state")):
        os.makedirs(os.path.join(home, folder), mode=0o700)
        os.environ[variable] = os.path.join(home, folder)
    os.environ["SANDBOX_AI_SECRETS"] = "file"
    base = load("mcp_vpn_probe_base", os.path.join(HERE, "mcp_vpn_probe.py"))
    vpn = base.load_vpn(runtime_path)  # (vpn.py beside the runtime: the launcher's side of the lent tunnel)
    rt, launcher = vpn.runtime(), vpn.launcher()
    srt = launcher.RUNTIME  # the sandbox's supervisor: its kill switch
    report, started, children = {}, [], []

    def phase(name, work):
        """WORK()'s findings as the phase NAME of the report; what failed in it, said - the next phase goes on."""
        try:
            report[name] = work()
        except Exception as exc:  # noqa: BLE001 - the report says it
            report[name] = {"error": f"{type(exc).__name__}: {exc}"[:1500],
                            "where": traceback.format_exc().splitlines()[-3:-1]}

    try:
        with contextlib.ExitStack() as start_of_run:
            # --- the outside, the namespaces, the VPN's server -------------------------------------------------------
            sh("ip", "link", "set", "lo", "up")
            inside = {}
            for name, device in (("start", "v"), ("image", "s"), ("command", "c")):
                child, inside[name] = namespace(base, name, GATEWAYS[name], OWN[name], device)
                children.append(child)
            sh("ip", "-6", "addr", "add", f"{IMAGE6_GATEWAY}/64", "dev", "so", "nodad")
            sh(*inside["image"], "ip", "-6", "addr", "add", f"{IMAGE6_OWN}/64", "dev", "sv", "nodad")
            sh(*inside["image"], "ip", "-6", "route", "add", "default", "via", IMAGE6_GATEWAY, "dev", "sv")
            sh("ip", "link", "add", "d0", "type", "dummy")
            for address in (INTERNET, API, ENDPOINT, PUBLIC_DNS):
                sh("ip", "addr", "add", f"{address}/32", "dev", "d0")
            sh("ip", "link", "set", "d0", "up")
            server_key, client_key = sh("wg", "genkey").strip(), sh("wg", "genkey").strip()
            server_pub = sh("wg", "pubkey", data=server_key.encode()).strip()
            client_pub = sh("wg", "pubkey", data=client_key.encode()).strip()
            key_file = os.path.join(home, "server.key")
            with open(key_file, "w") as file:
                os.fchmod(file.fileno(), 0o600)
                file.write(server_key + "\n")
            sh("ip", "link", "add", "wgs", "type", "wireguard")
            sh("wg", "set", "wgs", "listen-port", str(WG_PORT), "private-key", key_file,
               "peer", client_pub, "allowed-ips", f"{TUNNEL_CLIENT}/32")
            sh("ip", "addr", "add", f"{TUNNEL_SERVER}/24", "dev", "wgs")
            sh("ip", "link", "set", "wgs", "up")
            far = FarEnd()
            for family, port in ((socket.AF_INET, PORT), (socket.AF_INET, API_PORT), (socket.AF_INET6, PORT)):
                far.tcp(family, port)
            listeners = [(socket.AF_INET, TUNNEL_SERVER, 53, VPN_ANSWER), (socket.AF_INET, PUBLIC_DNS, 53, LAN_ANSWER),
                         (socket.AF_INET, "0.0.0.0", UDP_PORT, None), (socket.AF_INET6, "::", UDP_PORT, None),
                         *[(socket.AF_INET, gateway, 53, LAN_ANSWER) for gateway in GATEWAYS.values()]]
            for family, address, port, answer in listeners:
                threading.Thread(target=base.udp_listener, args=(far, family, address, port, answer),
                                 daemon=True).start()
            time.sleep(0.3)
            clients = {name: Client(inside[name], runtime_path) for name in inside}
            started.extend(clients.values())

            def server_tries(name):
                """What an MCP server tries from its namespace: the internet, the start's model API, lookups at the
                resolver its container was given and at a public one - and every way that is not the internet."""
                return {"tcp internet": ["tcp", INTERNET, PORT], "tcp api": ["tcp", API, API_PORT],
                        "dns own resolver": ["dns", GATEWAYS[name]], "dns public resolver": ["dns", PUBLIC_DNS],
                        "tcp own gateway": ["tcp", GATEWAYS[name], PORT],
                        "tcp the start's gateway": ["tcp", GATEWAYS["start"], PORT],
                        "tcp the tunnel's private side": ["tcp", TUNNEL_SERVER, PORT],
                        "tcp the vpn endpoint": ["tcp", ENDPOINT, PORT],
                        "udp internet": ["udp", INTERNET, UDP_PORT],
                        "udp own gateway": ["udp", GATEWAYS[name], UDP_PORT],
                        **({"tcp6 own gateway": ["tcp6", IMAGE6_GATEWAY, PORT],
                            "udp6 own gateway": ["udp6", IMAGE6_GATEWAY, UDP_PORT]} if name == "image" else {})}

            def tried(name, tries=None):
                results = clients[name].tries(**(tries or server_tries(name)))
                return {"results": results, "arrived": far.take()}

            # The control: a server's namespace with nothing in it reaches everything from its own address.
            phase("before", lambda: tried("image"))

            # --- the start: it holds the profile, lends its tunnel -------------------------------------------------
            wg_quick = (f"[Interface]\nPrivateKey = {client_key}\nAddress = {TUNNEL_CLIENT}/32\nDNS = {TUNNEL_SERVER}\n"
                        f"[Peer]\nPublicKey = {server_pub}\nAllowedIPs = 0.0.0.0/0\n"
                        f"Endpoint = {ENDPOINT}:{WG_PORT}\nPersistentKeepalive = 5\n")
            wireguard, secrets_, _ = vpn.parse_wireguard(wg_quick)
            dns_servers = wireguard.pop("dns")
            profile = {"name": "live", "type": "wireguard", "provider": None, "wireguard": wireguard,
                       "dns": dns_servers}
            checks = {"ip": False, "urls": [], "timeout": 2, "recheck": 0, "dns_probe": None}
            said = []
            state = {}

            def lender_up():
                start_of_run.enter_context(vpn.profile_lock(profile, "the session lent-probe", said.append))
                a = argparse.Namespace(run_id=RUN, backend="podman", podman_url=None)
                lend, token = vpn.prepare_lending(a, ["podman"], start_of_run)
                state["token"], state["sockets"] = token, str(vpn.lend_socket_dir(RUN))
                token_file = os.path.join(str(vpn.lend_dir(RUN)), "token")
                found = {"lend": list(lend), "lends profile": vpn.lends(profile),
                         "token file mode": oct(os.stat(token_file).st_mode & 0o777),
                         "mounted folder at first": os.listdir(state["sockets"])}
                payload = {"version": 1, "action": "serve", "resolve": [], "profile": profile,
                           "secrets": {**secrets_, "lend_token": token}, "lend": {"required": True}, "checks": checks}
                state["lender"] = Serve(inside["start"], runtime_path, payload, state["sockets"], read_only=False)
                started.append(state["lender"])
                ready = state["lender"].up()
                far.take()  # (its own checks asked the tunnel's resolver)
                found["events"] = shown(state["lender"].events)
                found["lends"] = (ready.get("tunnel") or {}).get("lends")
                found["killswitch"] = sh(*inside["start"], sys.executable, "-I", runtime_path, "killswitch",
                                         check=False).strip()
                # The start's own firewall, as the launcher writes it for a start that lends (the supervisor loads
                # it once the VPN is up): the proxy's redirect, the checks' mark, the lent mark after the cut chain.
                table = launcher.firewall_rules([], [API], dns=[TUNNEL_SERVER],
                                                proxy={"port": srt.PROXY_PORT, "loopback": []},
                                                vpn_mark=vpn.mark_expression(profile), check_mark=rt.CHECK_MARK,
                                                lend_mark=rt.LEND_MARK)
                sh(*inside["start"], "nft", "-f", "-", data=table.encode())
                found["firewall"] = [line.strip() for line in table.splitlines()
                                     if "mark" in line or "jump cut" in line]
                # A second sandbox with the same key: refused, naming who holds it. (A borrower takes no lock.)
                try:
                    with vpn.profile_lock(profile, "a second sandbox", said.append):
                        found["second sandbox"] = "started"
                except ValueError as exc:
                    found["second sandbox"] = str(exc)
                # The lender's socket, asked from here (whoever reaches the folder): only with the run's token.
                socket_path = os.path.join(state["sockets"], "lend.sock")
                found["socket mode"] = oct(os.stat(socket_path).st_mode & 0o777)
                asked = {}
                here = os.getcwd()
                os.chdir(state["sockets"])  # (a Unix socket's path has at most 107 bytes)
                try:
                    for who, given in (("no token", None), ("another run's token", "0" * 64),
                                       ("the run's token", vpn.lend_token(RUN))):
                        for request in ({"op": "ping"}, {"op": "tcp", "address": INTERNET, "port": PORT}):
                            try:
                                rt._lend_token = None
                                answer, sock = rt.ask_lender(request, "lend.sock", timeout=10, token=given)
                                first = read_line(sock) if request["op"] == "tcp" else answer.get("profile")
                                sock.close()
                                asked[f"{who}: {request['op']}"] = f"served ({first})"
                            except OSError as exc:
                                asked[f"{who}: {request['op']}"] = f"{type(exc).__name__}: {exc}"
                finally:
                    os.chdir(here)
                found["asked"] = asked
                found["asked arrived"] = far.take()
                return found
            phase("start", lender_up)

            def borrower(name, mcp):
                payload = {"version": 1, "action": "serve", "resolve": [], "profile": profile,
                           "secrets": {"lend_token": vpn.lend_token(RUN)}, "borrow": {"run": RUN}, "checks": checks,
                           **({"mcp": True} if mcp else {})}
                served = Serve(inside[name], runtime_path, payload, state["sockets"], read_only=True)
                started.append(served)
                state[name] = served
                ready = served.up()
                return {"events": shown(served.events), "tunnel": ready.get("tunnel"),
                        "killswitch": sh(*inside[name], sys.executable, "-I", runtime_path, "killswitch",
                                         check=False).strip()}

            # --- an MCP image server: its process in the borrowing VPN container's namespace -------------------
            def image_server():
                found = borrower("image", mcp=True)
                found.update(tried("image"))
                return found
            phase("image", image_server)

            # --- a command server: its own sandbox (firewall, connection proxy) beside the borrowing container -
            def command_server():
                found = borrower("command", mcp=False)
                table = launcher.firewall_rules([], [], dns=[rt.BORROW_DNS], allow_all=True,
                                                proxy={"port": srt.PROXY_PORT, "loopback": []},
                                                vpn_rules=vpn.lent_agent_rules(True, launcher.PROXY_UID),
                                                check_mark=rt.CHECK_MARK)
                sh(*inside["command"], "nft", "-f", "-", data=table.encode())
                stand_in = subprocess.Popen([*inside["command"], sys.executable, "-I", os.path.abspath(__file__),
                                             "--proxy", str(srt.PROXY_PORT), str(launcher.PROXY_UID)],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                started.append(argparse.Namespace(close=stand_in.kill))
                if Lines(stand_in.stdout).read(20) != "ready":
                    stand_in.kill()
                    raise Failed(f"the proxy's stand-in did not start: {stand_in.stderr.read()[-600:]}")
                tries = server_tries("command")
                tries["dns the relay's resolver"] = ["dns", rt.BORROW_DNS]
                tries["tcp the relay itself"] = ["tcp", "127.0.0.1", rt.BORROW_PORT]
                found.update(tried("command", tries))
                return found
            phase("command", command_server)

            # --- the start's own programs: the lent mark opens nothing for them --------------------------------
            phase("agent", lambda: tried("start", {"tcp internet": ["tcp", INTERNET, PORT],
                                                   "tcp api, another port": ["tcp", API, PORT],
                                                   "udp internet": ["udp", INTERNET, UDP_PORT]}))

            # --- sandbox-ai net cut: the supervisor's own kill switch, loaded into the start's namespace -------
            network = srt.RuntimeNetwork({"loopback": [], "https": [f"{API}/32"]}, None,
                                         lambda script: sh(*inside["start"], "nft", "-f", "-", data=script.encode()))
            network.cut_settings = srt.cut_settings({
                "host_ports": [], "tor": False, "bypass": [], "machine_host": None,
                "api": {"https": [API], "tcp": [], "loopback": [], "names": ["api.example"]}})
            network.vpn_mark = vpn.mark_expression(profile)
            network.lend_mark = srt.cut_lend_mark({"lend_mark": rt.LEND_MARK})
            targets = (("internet", INTERNET, PORT), ("api", API, API_PORT))
            new = {"tcp internet": ["tcp", INTERNET, PORT], "tcp api": ["tcp", API, API_PORT],
                   "dns": ["dns", GATEWAYS["image"]]}

            def cut(mode, server="image"):
                """Connections of SERVER opened before the cut MODE, then the cut: what becomes of them, of new ones
                and of a lookup; what the far end heard meanwhile; and the cut lifted again."""
                found, asker = {}, clients[server]
                try:
                    for name, address, port in targets:
                        found[f"open {name}"] = asker.ask(op="open", name=name, address=address, port=port)
                        found[f"before {name}"] = asker.ask(op="say", name=name, text=f"before {mode} {name}", wait=8)
                    far.take()
                    network.set_cut(mode)
                    found["cut"] = network.cut_mode
                    if mode == "all":
                        found["rules"] = sh(*inside["start"], "nft", "list", "chain", "inet", "agent_sandbox",
                                            "cut_all", check=False).strip()
                    for name, _address, _port in targets:
                        found[f"after {name}"] = asker.ask(op="say", name=name, text=f"after {mode} {name}", wait=10)
                    found["new"] = asker.tries(**new)
                    found["arrived"] = far.take()
                finally:
                    network.set_cut("off")
                    for name, _address, _port in targets:
                        with contextlib.suppress(Failed):
                            asker.ask(op="close", name=name)
                found["lifted"] = asker.tries(**new)
                far.take()
                return found
            for mode in srt.CUT_MODES:
                phase(f"cut {mode}", lambda mode=mode: cut(mode))
            phase("cut all, command server", lambda: cut("all", "command"))

            def idle():
                """A cut connection that says nothing: its next keepalive probe meets the cut, and it ends."""
                asker, found = clients["image"], {}
                try:
                    found["open"] = asker.ask(op="open", name="idle", address=INTERNET, port=PORT)
                    found["before"] = asker.ask(op="say", name="idle", text="before idle", wait=8)
                    far.take()
                    network.set_cut("all")
                    began = time.monotonic()
                    found["after"] = asker.ask(op="watch", name="idle", wait=rt.LEND_KEEPALIVE[0] + 20)
                    found["seconds"] = round(time.monotonic() - began, 1)
                    found["arrived"] = far.take()
                finally:
                    network.set_cut("off")
                    with contextlib.suppress(Failed):
                        asker.ask(op="close", name="idle")
                return found
            phase("idle", idle)

            # --- the start's VPN container is gone (killed): the borrowers end, nothing leaves -----------------
            def lender_killed():
                found = {}
                state["lender"].process.kill()
                found["lender exit"] = state["lender"].ended(20)
                for name in ("image", "command"):
                    served = state.get(name)
                    if served is None:
                        found[name] = {"error": "it never came up"}
                        continue
                    status = served.ended(90)
                    found[name] = {"exit": status, "last": shown(served.events[-1:]),
                                   "killswitch": sh(*inside[name], sys.executable, "-I", runtime_path, "killswitch",
                                                    check=False).strip()}
                found.update(tried("image"))
                found["command results"] = clients["command"].tries(**server_tries("command"))
                found["command arrived"] = far.take()
                return found
            phase("lender killed", lender_killed)
        report["left behind"] = os.path.exists(str(vpn.lend_dir(RUN)))
    except Exception as exc:  # noqa: BLE001 - the report says it
        report["setup"] = {"error": f"{type(exc).__name__}: {exc}"[:1500],
                           "where": traceback.format_exc().splitlines()[-3:-1]}
    finally:
        for process in started:
            with contextlib.suppress(Exception):
                process.close()
        for child in children:
            with contextlib.suppress(OSError):
                child.kill()
        shutil.rmtree(home, ignore_errors=True)
    print(json.dumps(report))


if __name__ == "__main__":
    if sys.argv[1] == "--client":
        client(load("vpn_runtime_lent_probe_client", sys.argv[2]))
    elif sys.argv[1] == "--proxy":
        proxy(int(sys.argv[2]), int(sys.argv[3]))
    else:
        main(sys.argv[1])
