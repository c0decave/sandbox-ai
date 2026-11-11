"""Runs as root in a network namespace of its own (tests/test_killswitch_live.py starts it with unshare): loads the
sandbox's firewall as the launcher writes it (agent_sandbox.firewall_rules, with the connection proxy's redirect), then
for each kill switch mode its cut chain (runtime.cut_rules, as the supervisor loads it for `sandbox-ai net cut`), and
tries every way out with the kernel's nftables. Prints one JSON report {mode: {attempt: outcome}}:

  "proxy"      the redirect to the connection proxy took it (a listener on the proxy's port got it)
  "connected"  a listener of the sandbox's own loopback got it directly
  "sent"       a UDP datagram left (the firewall let it out)
  "refused"    the firewall refused it at once
  "delivered" / "held"   a connection opened before the cut: its next bytes arrived, or the firewall held them

The namespace has loopback and a dummy interface with the routes of a local network (192.168.1.0/24), the internet
(default route) and IPv6. The proxy's own user cannot be tested as root (the proxy's listener here only takes what the
redirect hands it). Standard library only.
Usage: killswitch_probe.py AGENT_SANDBOX_PY"""
import errno
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time

spec = importlib.util.spec_from_file_location("agent_sandbox_probe", sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
rt = launcher.RUNTIME
PROXY_PORT = rt.PROXY_PORT
HOST_PORT = 8080        # a host port forwarded at the start (the proxy's listener takes it, as the redirect hands it)
OWN_PORT = 4321         # a service of the sandbox's own on loopback
SETTINGS = {"host_ports": [HOST_PORT],
            "api": {"https": ["192.0.2.10"], "tcp": [["10.0.0.5", 11434]], "loopback": [], "names": ["api.example"]},
            "tor": False, "bypass": [], "machine_host": None}
# A Tor-like run: only the --vpn-bypass-tcp target is its local network, everything else lies past Tor.
TOR = {**SETTINGS, "tor": True, "bypass": [["192.168.1.20", 1234]]}
MARK = 0xca6c  # the VPN's (here: a VPN-like tunnel's) own packets: they pass the agent's firewall and the cut
VPN_MARK = f"meta mark {MARK:#x}"


def sh(*command, data=None):
    subprocess.run(command, check=True, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


sh("ip", "link", "set", "lo", "up")
sh("ip", "link", "add", "d0", "type", "dummy")
sh("ip", "link", "set", "d0", "up")
sh("ip", "addr", "add", "192.168.1.10/24", "dev", "d0")
sh("ip", "route", "add", "default", "via", "192.168.1.1", "dev", "d0")
sh("ip", "-6", "addr", "add", "fd00:1::10/64", "dev", "d0", "nodad")
sh("ip", "-6", "route", "add", "default", "via", "fd00:1::1", "dev", "d0")
# What the run may reach without a cut: the local network (all ports), the provider on 443, the --llm server's port,
# DNS to one nameserver; every TCP connection goes to the proxy, the host's port through it too.
table = launcher.firewall_rules(["192.168.1.0/24", "fd00:1::/64", "198.51.100.0/24"], ["192.0.2.10"],
                                tcp=[("10.0.0.5", 11434)], dns=["1.1.1.1"],
                                proxy={"port": PROXY_PORT, "loopback": [HOST_PORT]}, vpn_mark=VPN_MARK)
sh("nft", "-f", "-", data=table.encode())

got = []  # what the listeners took: ("proxy" | "connected", port)
received = {}  # {the client's port: [the bytes the far end got]}


def keep(connection, peer):
    with connection:
        while True:
            try:
                data = connection.recv(100)
            except OSError:
                return
            if not data:
                return
            received.setdefault(peer, []).append(data)


def listen(family, address, port, kind):
    server = socket.socket(family, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    server.bind((address, port))
    server.listen(64)

    def accept():
        while True:
            connection, peer = server.accept()
            got.append((kind, port))
            threading.Thread(target=keep, args=(connection, peer[1]), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()


listen(socket.AF_INET, "127.0.0.1", PROXY_PORT, "proxy")
listen(socket.AF_INET6, "::1", PROXY_PORT, "proxy")
listen(socket.AF_INET, "127.0.0.1", OWN_PORT, "connected")
stub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
stub.bind(("127.0.0.1", 53))  # the DNS stub
time.sleep(0.2)
REFUSED = (errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH)


def tcp(family, address, port):
    before = len(got)
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    try:
        sock.connect((address, port))
    except socket.timeout:
        return "direct"
    except OSError as exc:
        return "refused" if exc.errno in REFUSED else f"error {errno.errorcode.get(exc.errno, exc.errno)}"
    finally:
        sock.close()
    time.sleep(0.1)
    return got[-1][0] if len(got) > before else "connected"


def udp(family, address, port, mark=None):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        if mark:  # as the VPN's own process marks its packets (CAP_NET_ADMIN: root here)
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK", 36), mark)
        sock.sendto(b"\0" * 12, (address, port))
    except OSError as exc:
        return "refused" if exc.errno in REFUSED else f"error {errno.errorcode.get(exc.errno, exc.errno)}"
    finally:
        sock.close()
    return "sent"


def cut(mode, settings=SETTINGS, modes=None):
    """The cut chain as the supervisor sets it: MODE (or several MODES at once: a shared server's cuts)."""
    chosen = modes or ([mode] if mode else [])
    view = {**rt.cut_settings(settings), "mode": rt.cut_label(chosen), "modes": chosen} if chosen else None
    sh("nft", "-f", "-", data=rt.cut_rules(view, VPN_MARK).encode())


def established(family, address, port, mode):
    """A connection to ADDRESS:PORT opened before the cut MODE (the redirect hands it to the proxy's listener): do its
    next bytes still arrive once the cut holds? The cut chain judges a packet by its connection's original
    destination, whenever that connection was opened."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(2)
    sock.connect((address, port))
    mine = sock.getsockname()[1]
    sock.sendall(b"before")
    time.sleep(0.2)
    cut(mode)
    try:
        sock.sendall(b"after")
        time.sleep(0.5)
        return "delivered" if (received.get(mine) or [b""])[-1].endswith(b"after") else "held"
    except OSError:
        return "held"
    finally:
        cut(None)
        sock.close()


ATTEMPTS = {
    "tcp provider 192.0.2.10:443": lambda: tcp(socket.AF_INET, "192.0.2.10", 443),
    "tcp provider 192.0.2.10:80": lambda: tcp(socket.AF_INET, "192.0.2.10", 80),
    "tcp llm 10.0.0.5:11434": lambda: tcp(socket.AF_INET, "10.0.0.5", 11434),
    "tcp lan 192.168.1.20:22": lambda: tcp(socket.AF_INET, "192.168.1.20", 22),
    "udp6 lan [fd00:1::20]:5353": lambda: udp(socket.AF_INET6, "fd00:1::20", 5353),
    "tcp6 lan [fd00:1::20]:80": lambda: tcp(socket.AF_INET6, "fd00:1::20", 80),
    "tcp bypass 192.168.1.20:1234": lambda: tcp(socket.AF_INET, "192.168.1.20", 1234),
    "udp tunnel (marked) 198.51.100.9:51820": lambda: udp(socket.AF_INET, "198.51.100.9", 51820, MARK),
    "tcp internet 198.51.100.7:443": lambda: tcp(socket.AF_INET, "198.51.100.7", 443),
    "tcp host port 127.0.0.1:8080": lambda: tcp(socket.AF_INET, "127.0.0.1", HOST_PORT),
    "tcp own 127.0.0.1:4321": lambda: tcp(socket.AF_INET, "127.0.0.1", OWN_PORT),
    "udp lan 192.168.1.20:5353": lambda: udp(socket.AF_INET, "192.168.1.20", 5353),
    "udp internet 198.51.100.7:443": lambda: udp(socket.AF_INET, "198.51.100.7", 443),
    "udp dns 1.1.1.1:53": lambda: udp(socket.AF_INET, "1.1.1.1", 53),
    "udp dns stub 127.0.0.1:53": lambda: udp(socket.AF_INET, "127.0.0.1", 53),
    "udp other resolver 127.0.53.1:53": lambda: udp(socket.AF_INET, "127.0.53.1", 53),
}
report = {}
for mode in (None, *rt.CUT_MODES):
    cut(mode)
    report[mode or "none"] = {name: attempt() for name, attempt in ATTEMPTS.items()}
    cut(None)
for mode in rt.CUT_MODES:  # the Tor-like run
    cut(mode, TOR)
    report[f"tor {mode}"] = {name: attempt() for name, attempt in ATTEMPTS.items()}
    cut(None)
cut(None, modes=["remote", "api"])  # two cuts at once (a shared MCP server's): only what both keep
report["remote+api"] = {name: attempt() for name, attempt in ATTEMPTS.items()}
cut(None)
# Connections open before the cut: what it forbids is held at once, what it keeps goes on.
report["established"] = {f"{name} {mode}": established(family, address, port, mode)
                         for name, family, address, port in (("host port", socket.AF_INET, "127.0.0.1", HOST_PORT),
                                                             ("lan", socket.AF_INET, "192.168.1.20", 22),
                                                             ("provider", socket.AF_INET, "192.0.2.10", 443))
                         for mode in rt.CUT_MODES}
print(json.dumps({"table": table, "report": report}))
