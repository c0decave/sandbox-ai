"""The "MCP server" of tests/test_mcp_vpn_podman_live.py: runs in a container started as sandbox-ai starts an image
server's connection (mcp.image_command, through vpn.py's guard behind a VPN), reads what to try from
/work/targets.json (the definition's mount) and prints one JSON report: its network namespace, and where each
attempt went. Standard library only (python3 of the image it is copied into).

  {"mode": "probe"}   connections and lookups that must not leave a VPN container whose tunnel is not up
  {"mode": "tor"}     through a Tor profile's container: lookups at Tor's DNSPort, HTTPS at the Tor Project's check,
                      what the server is told (SANDBOX_AI_TOR, SANDBOX_AI_TOR_VIRTUAL_NETWORK) and an HTTP request to
                      an .onion name at the address tor mapped it to, as a fetcher behind Tor makes it
  {"mode": "wait"}    say the namespace, then wait (until the container is stopped)"""
import errno
import http.client
import ipaddress
import json
import os
import socket
import ssl
import struct
import sys
import time

REFUSED = {errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


def tcp(family, address, port, timeout=5.0, send=b"hello\n"):
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((address, port))
        sock.sendall(send)
        return "connected"
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        return "refused" if exc.errno in REFUSED else f"error {errno.errorcode.get(exc.errno, exc.errno)}"
    finally:
        sock.close()


def lookup(name, server, timeout=10.0, qtype=1):
    """A records (QTYPE 28: AAAA records) of NAME from SERVER (UDP port 53), or why not."""
    ident = int.from_bytes(os.urandom(2), "big")
    question = b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\0"
    packet = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + question + struct.pack("!HH", qtype, 1)
    sock = socket.socket(socket.AF_INET6 if ":" in server else socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.connect((server, 53))
        sock.send(packet)
        data = sock.recv(4096)
    except OSError as exc:
        return f"failed: {exc}"
    finally:
        sock.close()
    if len(data) < 12 or struct.unpack("!H", data[:2])[0] != ident:
        return "failed: not an answer"
    count = struct.unpack("!H", data[6:8])[0]
    offset = 12 + len(question) + 4
    found = []
    for _ in range(count):
        while data[offset] and data[offset] & 0xC0 != 0xC0:
            offset += 1 + data[offset]
        offset += 2 if data[offset] & 0xC0 == 0xC0 else 1
        rtype, _, _, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        if rtype == 1 and length == 4:
            found.append(socket.inet_ntoa(data[offset:offset + 4]))
        elif rtype == 28 and length == 16:
            found.append(socket.inet_ntop(socket.AF_INET6, data[offset:offset + 16]))
        offset += length
    return found


def tor_check(addresses, timeout=60.0):
    """The Tor Project's answer ({"IsTor": ..., "IP": ...}) over HTTPS to one of ADDRESSES (check.torproject.org)."""
    context = ssl.create_default_context()
    for address in addresses:
        try:
            raw = socket.create_connection((address, 443), timeout=timeout)
            with context.wrap_socket(raw, server_hostname="check.torproject.org") as sock:
                connection = http.client.HTTPConnection("check.torproject.org")
                connection.sock = sock
                connection.request("GET", "/api/ip")
                return json.loads(connection.getresponse().read(65536))
        except (OSError, ValueError) as exc:
            last = str(exc)
    return {"error": last if addresses else "no address"}


def onion_http(address, host, timeout=120.0):
    """The status of GET / at the onion service HOST through the address tor mapped it to (as a fetcher that takes
    the announced network connects), or why not."""
    try:
        connection = http.client.HTTPConnection(address, 80, timeout=timeout)
        connection.request("GET", "/", headers={"Host": host})
        return connection.getresponse().status
    except (OSError, http.client.HTTPException) as exc:
        return f"failed: {exc}"


def main():
    targets = json.load(open("/work/targets.json"))
    report = {"netns": os.readlink("/proc/self/ns/net"), "uid": os.getuid(),
              "env": {key: os.environ.get(key) for key in ("SANDBOX_AI_TOR", "SANDBOX_AI_TOR_VIRTUAL_NETWORK")}}
    if targets["mode"] == "wait":
        print(json.dumps(report), flush=True)
        time.sleep(600)
        return
    v4, v6 = socket.AF_INET, socket.AF_INET6
    attempts = {}
    for name, (address, port) in targets.get("tcp", {}).items():
        attempts[name] = tcp(v6 if ":" in address else v4, address, port)
    for name, (host, server) in targets.get("dns", {}).items():
        attempts[name] = lookup(host, server)
    if targets["mode"] == "tor":
        found = lookup("check.torproject.org", "1.1.1.1", timeout=60)
        attempts["check.torproject.org via 1.1.1.1"] = found
        attempts["tor check"] = tor_check(found if isinstance(found, list) else [])
        onion = targets["onion"]
        mapped = lookup(onion, "1.1.1.1")
        attempts["onion via 1.1.1.1"] = mapped
        attempts["onion mapped by tor"] = isinstance(mapped, list) and bool(mapped) and all(
            ipaddress.ip_address(a) in ipaddress.ip_network(targets["tor_virtual"]) for a in mapped)
        announced = os.environ.get("SANDBOX_AI_TOR_VIRTUAL_NETWORK")
        attempts["onion in the announced network"] = bool(announced) and isinstance(mapped, list) and bool(mapped) \
            and all(ipaddress.ip_address(a) in ipaddress.ip_network(announced) for a in mapped)
        # A fetcher's getaddrinfo asks for AAAA too: an IPv6 address tor would map an .onion name to (its
        # VirtualAddrNetworkIPv6 is link-local) would be refused by such a fetcher, and IPv6 leaves nowhere here.
        attempts["onion AAAA"] = lookup(onion, "1.1.1.1", qtype=28)
        attempts["onion http"] = onion_http(mapped[0], onion) if isinstance(mapped, list) and mapped else "no address"
    report["attempts"] = attempts
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - the test reads it
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
        sys.exit(1)
