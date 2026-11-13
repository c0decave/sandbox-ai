"""Runs as root in a network namespace of its own (tests/test_tor_killswitch_live.py starts it with unshare): loads
the Tor kill switch of sandbox/vpn_runtime.py with the kernel's nftables, then tries every way out and prints one JSON
report of where each attempt went:

  "tor"       tor's TransPort (or DNSPort) got it: it would have left through Tor
  "direct"    it left the namespace past Tor (the interface took it; nothing answers there, so it times out)
  "refused"   the kill switch refused it at once

The namespace has loopback and a dummy interface with the routes of a local network (192.168.1.0/24), the internet
(default route) and IPv6, so a packet the rules let out really reaches an interface. Tor's own user cannot be tested
as root (the rules are made for uid 4242 here, which never sends). Standard library only.
Usage: tor_killswitch_probe.py VPN_RUNTIME_PY BYPASS_JSON [OPTIONS_JSON]

OPTIONS: {"tor": false} - tor is gone (died, its container ended): nothing listens on its ports, as during a run whose
Tor container ended; {"other_table": true} - a table of another part of the run in the same namespace accepts
everything (the agent's own firewall with --egress-all-allow, at priorities before and after the kill switch's): one
base chain's drop is final, another's accept opens nothing past it."""
import errno
import importlib.util
import json
import socket
import struct
import subprocess
import sys
import threading
import time

spec = importlib.util.spec_from_file_location("vpn_runtime_probe", sys.argv[1])
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)
bypass = json.loads(sys.argv[2])
options = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}


def sh(*command):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


sh("ip", "link", "set", "lo", "up")
sh("ip", "link", "add", "d0", "type", "dummy")
sh("ip", "link", "set", "d0", "up")
sh("ip", "addr", "add", "192.168.1.10/24", "dev", "d0")
sh("ip", "route", "add", "default", "via", "192.168.1.1", "dev", "d0")
sh("ip", "-6", "addr", "add", "fd00:1::10/64", "dev", "d0", "nodad")
sh("ip", "-6", "route", "add", "default", "via", "fd00:1::1", "dev", "d0")
rules = rt.tor_rules(uid=4242, bypass=bypass)
subprocess.run(["nft", "-f", "-"], input=rules.encode(), check=True)
if options.get("other_table"):
    subprocess.run(["nft", "-f", "-"], check=True, input=b"""table inet other_part {
 chain early {
  type filter hook output priority -50; policy accept;
  accept
 }
 chain late {
  type filter hook output priority 50; policy accept;
  accept
 }
 chain nat_early {
  type nat hook output priority -150; policy accept;
  accept
 }
}
""")

got = []  # (kind, original destination) tor's ports saw


def trans_port():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", rt.TOR_TRANS_PORT))
    server.listen(64)
    while True:
        conn, _ = server.accept()
        raw = conn.getsockopt(socket.SOL_IP, 80, 16)  # SO_ORIGINAL_DST
        port, packed = struct.unpack("!2xH4s8x", raw)
        got.append(("tcp", f"{socket.inet_ntoa(packed)}:{port}"))
        conn.close()


def dns_port():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((rt.TOR_DNS, 53))
    while True:
        _, peer = server.recvfrom(512)
        got.append(("udp", "dns"))


if options.get("tor", True):
    threading.Thread(target=trans_port, daemon=True).start()
    threading.Thread(target=dns_port, daemon=True).start()
time.sleep(0.2)


def tcp(family, address, port):
    before = len(got)
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    try:
        sock.connect((address, port))
    except socket.timeout:
        return "direct"
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH):
            return "refused"
        return f"error {errno.errorcode.get(exc.errno, exc.errno)}"
    finally:
        sock.close()
    time.sleep(0.1)
    return "tor" if len(got) > before else "connected"


def udp(family, address, port):
    before = len(got)
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"\0" * 12, (address, port))
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH):
            return "refused"
        return f"error {errno.errorcode.get(exc.errno, exc.errno)}"
    finally:
        sock.close()
    time.sleep(0.2)
    return "tor" if len(got) > before else "direct"


attempts = {
    "tcp 192.168.1.20:1234": lambda: tcp(socket.AF_INET, "192.168.1.20", 1234),
    "tcp 192.168.1.20:1235": lambda: tcp(socket.AF_INET, "192.168.1.20", 1235),
    "tcp 192.168.1.20:22": lambda: tcp(socket.AF_INET, "192.168.1.20", 22),
    "tcp 192.168.1.21:1234": lambda: tcp(socket.AF_INET, "192.168.1.21", 1234),
    "tcp 192.168.1.1:80": lambda: tcp(socket.AF_INET, "192.168.1.1", 80),
    "tcp 10.0.0.5:1234": lambda: tcp(socket.AF_INET, "10.0.0.5", 1234),
    "tcp 1.1.1.1:443": lambda: tcp(socket.AF_INET, "1.1.1.1", 443),
    "tcp 1.1.1.1:1234": lambda: tcp(socket.AF_INET, "1.1.1.1", 1234),
    "udp 192.168.1.20:1234": lambda: udp(socket.AF_INET, "192.168.1.20", 1234),
    "udp 192.168.1.20:53": lambda: udp(socket.AF_INET, "192.168.1.20", 53),
    "udp 1.1.1.1:443": lambda: udp(socket.AF_INET, "1.1.1.1", 443),
    "tcp6 [fd00:1::131]:1234": lambda: tcp(socket.AF_INET6, "fd00:1::131", 1234),
    "tcp6 [2606:4700::1111]:443": lambda: tcp(socket.AF_INET6, "2606:4700::1111", 443),
    "udp6 [2606:4700::1111]:443": lambda: udp(socket.AF_INET6, "2606:4700::1111", 443),
}
print(json.dumps({"rules": rules, "results": {name: attempt() for name, attempt in attempts.items()}}))
