"""Runs as root in a network namespace of its own (tests/test_mcp_vpn_live.py starts it with unshare): a real
WireGuard VPN between two namespaces, with sandbox/vpn_runtime.py's own setup (`vpn_runtime.py serve`, as in the VPN
container of an MCP image server: kill switch, kernel WireGuard, routes, the redirect of the MCP server's lookups),
then "the MCP server" - a process in the VPN's network namespace, as a container started with
--network=container:<the VPN container> is - tries every way out. Prints one JSON report.

  outside (this namespace)            vpn (a namespace of its own: the VPN container's)
  "the internet": 203.0.113.10        vv 192.168.50.2/24, fd00:50::2/64 (pasta's uplink in a container)
  the VPN endpoint 198.18.0.1:51820   wg0 10.66.0.2 - the tunnel - wgs 10.66.0.1 (the VPN's server and resolver)
  "the LAN"/"the host" 192.168.50.1   the MCP server's process
  (resolver there too), fd00:50::1

Every service of the outside listens on all its addresses and writes down who came: a packet that left past the
tunnel would arrive there from 192.168.50.2 (your own address), one through the tunnel from 10.66.0.2. Phases:
"before" (no VPN: the way past it is there), "up", "peer down" (the VPN's server stops answering), "tunnel gone"
(wg0 removed, a default route over the uplink offered again), "runtime ended" (the VPN's process ended: its kill
switch stays). Standard library only.
Usage: mcp_vpn_probe.py VPN_RUNTIME_PY            (as root, in a fresh network namespace)
       mcp_vpn_probe.py --client VPN_RUNTIME_PY   (inside the vpn namespace: the attempts, JSON on stdout)"""
import errno
import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

TARGET = "203.0.113.10"     # a server on the internet
ENDPOINT = "198.18.0.1"     # the VPN's endpoint
LAN = "192.168.50.1"        # the uplink's gateway: the host, the LAN, its resolver
OWN = "192.168.50.2"        # the VPN namespace's own address on the uplink
TUNNEL_SERVER, TUNNEL_CLIENT = "10.66.0.1", "10.66.0.2"
LAN6, TARGET6 = "fd00:50::1", "2001:db8::10"
VPN_ANSWER, LAN_ANSWER = "198.51.100.7", "192.0.2.66"
PORT, UDP_PORT, WG_PORT = 8000, 9999, 51820


def load_runtime(path):
    spec = importlib.util.spec_from_file_location("vpn_runtime_mcp_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sh(*command, data=None, check=True):
    done = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and done.returncode != 0:
        print(f"mcp_vpn_probe: {' '.join(command)} failed ({done.returncode}): "
              f"{done.stderr.decode(errors='replace')}", file=sys.stderr)
        sys.exit(3)
    return done.stdout.decode(errors="replace")


# --- the MCP server's attempts (--client, inside the vpn namespace) ---------------------------------------------

REFUSED = {errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


def tcp(family, address, port, timeout=2.0):
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((address, port))
        sock.sendall(b"hello\n")
        return "connected"
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        return "refused" if exc.errno in REFUSED else f"error {errno.errorcode.get(exc.errno, exc.errno)}"
    finally:
        sock.close()


def udp(family, address, port):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"hello", (address, port))
        return "sent"
    except OSError as exc:
        return "refused" if exc.errno in REFUSED else f"error {errno.errorcode.get(exc.errno, exc.errno)}"
    finally:
        sock.close()


def dns(rt, server):
    try:
        return rt.dns_lookup("probe.example", server, 1, timeout=2.0)
    except (OSError, ValueError) as exc:
        return f"failed: {exc}"


def client(rt):
    v4, v6 = socket.AF_INET, socket.AF_INET6
    results = {
        "tcp internet": tcp(v4, TARGET, PORT),
        "tcp lan": tcp(v4, LAN, PORT),
        "tcp endpoint other port": tcp(v4, ENDPOINT, PORT),
        "udp lan": udp(v4, LAN, UDP_PORT),
        "udp internet": udp(v4, TARGET, UDP_PORT),
        "dns lan resolver": dns(rt, LAN),          # what resolv.conf of the VPN container names (pasta, the host)
        "dns public resolver": dns(rt, "1.1.1.1"),
        "tcp6 lan": tcp(v6, LAN6, PORT),
        "tcp6 internet": tcp(v6, TARGET6, PORT),
        "udp6 lan": udp(v6, LAN6, UDP_PORT),
        "dns6 lan resolver": dns(rt, LAN6),
    }
    time.sleep(0.3)  # what was sent arrives before the outside reads its records
    print(json.dumps(results))


# --- the outside: who arrives where -----------------------------------------------------------------------------

class Records:
    def __init__(self):
        self.lock, self.seen = threading.Lock(), []

    def add(self, kind, peer, local):
        with self.lock:
            self.seen.append([kind, peer.split("%")[0].removeprefix("::ffff:"), local.removeprefix("::ffff:")])

    def take(self):
        with self.lock:
            seen, self.seen = self.seen, []
        return seen


def tcp_listener(records, family, address):
    server = socket.socket(family, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    server.bind((address, PORT))
    server.listen(64)
    while True:
        conn, peer = server.accept()
        records.add("tcp", peer[0], conn.getsockname()[0])
        conn.close()


def udp_listener(records, family, address, port, answer=None):
    server = socket.socket(family, socket.SOCK_DGRAM)
    if family == socket.AF_INET6:
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    server.bind((address, port))
    while True:
        data, peer = server.recvfrom(4096)
        records.add("dns" if port == 53 else "udp", peer[0], address)
        if answer and len(data) >= 12:
            # A reply with one A record: the question as it came, then a pointer to its name.
            ident = struct.unpack("!H", data[:2])[0]
            end = 12
            while end < len(data) and data[end]:
                end += 1 + data[end]
            question = data[12:end + 5]
            reply = struct.pack("!HHHHHH", ident, 0x8180, 1, 1, 0, 0) + question
            reply += struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(answer)
            server.sendto(reply, peer)


def namespace():
    """A process in a network namespace of its own (the VPN container's): its pid once the namespace is made."""
    child = subprocess.Popen(["unshare", "--net", "sleep", "900"])
    own = os.readlink("/proc/self/ns/net")
    deadline = time.monotonic() + 10
    while os.readlink(f"/proc/{child.pid}/ns/net") == own:
        if time.monotonic() > deadline:
            sys.exit("mcp_vpn_probe: the namespace was not made")
        time.sleep(0.05)
    return child


def main(runtime_path):
    rt = load_runtime(runtime_path)
    child = namespace()
    inside = ["nsenter", f"--net=/proc/{child.pid}/ns/net"]
    report = {}
    try:
        sh("ip", "link", "set", "lo", "up")
        sh("ip", "link", "add", "vo", "type", "veth", "peer", "name", "vv")
        sh("ip", "link", "set", "vv", "netns", str(child.pid))
        sh("ip", "addr", "add", f"{LAN}/24", "dev", "vo")
        sh("ip", "-6", "addr", "add", f"{LAN6}/64", "dev", "vo", "nodad")
        sh("ip", "link", "set", "vo", "up")
        sh("ip", "link", "add", "d0", "type", "dummy")
        for address in (f"{TARGET}/32", f"{ENDPOINT}/32"):
            sh("ip", "addr", "add", address, "dev", "d0")
        sh("ip", "-6", "addr", "add", f"{TARGET6}/128", "dev", "d0", "nodad")
        sh("ip", "link", "set", "d0", "up")
        sh(*inside, "ip", "link", "set", "lo", "up")
        sh(*inside, "ip", "addr", "add", f"{OWN}/24", "dev", "vv")
        sh(*inside, "ip", "-6", "addr", "add", "fd00:50::2/64", "dev", "vv", "nodad")
        sh(*inside, "ip", "link", "set", "vv", "up")
        sh(*inside, "ip", "route", "add", "default", "via", LAN, "dev", "vv")
        sh(*inside, "ip", "-6", "route", "add", "default", "via", LAN6, "dev", "vv")

        # The VPN's server, and what listens outside.
        server_key, client_key = sh("wg", "genkey").strip(), sh("wg", "genkey").strip()
        server_pub = sh("wg", "pubkey", data=server_key.encode()).strip()
        client_pub = sh("wg", "pubkey", data=client_key.encode()).strip()
        keys = tempfile.mkdtemp(prefix="mcp-vpn-probe-")
        with open(os.path.join(keys, "server"), "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(server_key + "\n")
        sh("ip", "link", "add", "wgs", "type", "wireguard")
        sh("wg", "set", "wgs", "listen-port", str(WG_PORT), "private-key", os.path.join(keys, "server"),
           "peer", client_pub, "allowed-ips", f"{TUNNEL_CLIENT}/32")
        sh("ip", "addr", "add", f"{TUNNEL_SERVER}/24", "dev", "wgs")
        sh("ip", "link", "set", "wgs", "up")
        records = Records()
        listeners = [(tcp_listener, (records, socket.AF_INET, "0.0.0.0")),
                     (tcp_listener, (records, socket.AF_INET6, "::")),
                     (udp_listener, (records, socket.AF_INET, TUNNEL_SERVER, 53, VPN_ANSWER)),
                     (udp_listener, (records, socket.AF_INET, LAN, 53, LAN_ANSWER)),
                     (udp_listener, (records, socket.AF_INET, "1.1.1.1", 53, LAN_ANSWER)),
                     (udp_listener, (records, socket.AF_INET6, LAN6, 53, LAN_ANSWER)),
                     (udp_listener, (records, socket.AF_INET, "0.0.0.0", UDP_PORT)),
                     (udp_listener, (records, socket.AF_INET6, "::", UDP_PORT))]
        sh("ip", "addr", "add", "1.1.1.1/32", "dev", "d0")  # a public resolver, outside the tunnel
        for target, args in listeners:
            threading.Thread(target=target, args=args, daemon=True).start()
        time.sleep(0.3)

        def attempts():
            done = subprocess.run([*inside, sys.executable, "-I", __file__, "--client", runtime_path],
                                  capture_output=True, text=True, timeout=120)
            if done.returncode != 0:
                sys.exit(f"mcp_vpn_probe: the client failed: {done.stderr}")
            return {"results": json.loads(done.stdout), "arrived": records.take()}

        report["before"] = attempts()

        # The VPN as the VPN container of an MCP image server brings it up (payload "mcp").
        wg_quick = (f"[Interface]\nPrivateKey = {client_key}\nAddress = {TUNNEL_CLIENT}/32\nDNS = {TUNNEL_SERVER}\n"
                    f"[Peer]\nPublicKey = {server_pub}\nAllowedIPs = 0.0.0.0/0, ::/0\n"
                    f"Endpoint = {ENDPOINT}:{WG_PORT}\nPersistentKeepalive = 5\n")
        vpn = load_vpn(runtime_path)
        wireguard, secrets_, _ = vpn.parse_wireguard(wg_quick)
        dns_servers = wireguard.pop("dns")
        payload = {"version": 1, "action": "serve", "mcp": True, "resolve": [],
                   "profile": {"name": "live", "type": "wireguard", "wireguard": wireguard, "dns": dns_servers},
                   "secrets": secrets_,
                   "checks": {"ip": False, "urls": [], "timeout": 2, "recheck": 0, "dns_probe": None}}
        serve = subprocess.Popen([*inside, sys.executable, "-I", runtime_path, "serve"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        serve.stdin.write(json.dumps(payload) + "\n")
        serve.stdin.flush()
        events = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            line = serve.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            events.append(event)
            if event.get("event") in ("ready", "error"):
                break
        report["events"] = [{k: v for k, v in e.items() if k != "checks"} for e in events]
        if not events or events[-1].get("event") != "ready":
            serve.kill()
            report["stderr"] = serve.stderr.read()[-2000:]
            print(json.dumps(report))
            return
        records.take()  # (the runtime's own checks asked the resolver)
        report["killswitch"] = sh(*inside, sys.executable, "-I", runtime_path, "killswitch", check=False).strip()
        report["rules"] = sh(*inside, "nft", "list", "ruleset", check=False)
        report["up"] = attempts()

        # The VPN's server stops answering: nothing may take another way.
        sh("ip", "link", "set", "wgs", "down")
        report["peer down"] = attempts()
        sh("ip", "link", "set", "wgs", "up")

        # The tunnel's interface is gone (a userspace tunnel whose process died), and the uplink offers a way out
        # again: the kill switch alone keeps everything in.
        sh(*inside, "ip", "link", "del", "wg0")
        sh(*inside, "ip", "route", "replace", "default", "via", LAN, "dev", "vv")
        report["tunnel gone"] = attempts()

        # The VPN's process ends (its container's stdin closed): the kill switch stays in the namespace.
        serve.stdin.close()
        serve.wait(timeout=30)
        report["runtime exit"] = serve.returncode
        report["killswitch after"] = sh(*inside, sys.executable, "-I", runtime_path, "killswitch",
                                        check=False).strip()
        report["runtime ended"] = attempts()
    finally:
        child.kill()
    print(json.dumps(report))


def load_vpn(runtime_path):
    """vpn.py beside the runtime (its WireGuard parser: the profile as `sandbox-ai vpn add --wireguard` keeps it)."""
    spec = importlib.util.spec_from_file_location("vpn_mcp_probe", os.path.join(os.path.dirname(runtime_path),
                                                                                "vpn.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    if sys.argv[1] == "--client":
        client(load_runtime(sys.argv[2]))
    else:
        main(sys.argv[1])
