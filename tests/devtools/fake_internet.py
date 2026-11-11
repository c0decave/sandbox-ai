#!/usr/bin/env python3
"""A local "internet" for egress tests: an HTTP and HTTPS echo server and a DNS server that answers fixed names, so
that allow and deny, the connection proxy, the kill switch and Tor's bypass rules can be checked end to end without
the real internet. Standard library only (openssl makes the HTTPS certificate when --https is given).

  python3 tests/devtools/fake_internet.py [--bind ADDR] [--http PORT] [--https PORT] [--dns PORT]
                                          [--name HOST=ADDR]... [--default ADDR] [--log FILE]

HTTP(S): every request is answered with JSON naming what arrived (method, path, Host, headers, body length, the
port, TLS or not); /status/CODE answers CODE, /bytes/N sends N bytes, /slow/S waits S seconds first. DNS (UDP): the
A record of a --name (or *.DOMAIN=ADDR), else --default if given, else NXDOMAIN; AAAA and the rest: no answer. Each
request is a line in --log (JSONL). Run it on the host, in a sandbox (sandbox-ai-dev) or in a container there:
podman run --rm --rootfs /:O python3 /work/tests/devtools/fake_internet.py ...
"""
import argparse
import http.server
import json
from pathlib import Path
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

LOCK = threading.Lock()
HANDSHAKE_TIMEOUT = 10  # a client that stalls in the TLS handshake holds only its own thread, this long


def record(log, entry):
    if log:
        with LOCK, open(log, "a") as handle:
            handle.write(json.dumps({"time": time.time(), **entry}) + "\n")


class Echo(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-internet"
    log_file = None

    def log_message(self, *args):  # quiet: --log has every request
        pass

    def setup(self):
        context = getattr(self.server, "ssl_context", None)
        if context is not None:  # TLS: the handshake in this connection's own thread, never in accept()
            self.request.settimeout(HANDSHAKE_TIMEOUT)
            self.request = context.wrap_socket(self.request, server_side=True)
            self.request.settimeout(None)
        super().setup()

    def answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        tls = isinstance(self.connection, ssl.SSLSocket)
        seen = {"method": self.command, "path": self.path, "host": self.headers.get("Host"),
                "headers": dict(self.headers.items()), "body_bytes": len(body), "port": self.server.server_address[1],
                "tls": tls, "client": self.client_address[0]}
        record(self.log_file, {"kind": "https" if tls else "http", **{k: seen[k] for k in
                                                                       ("method", "path", "host", "port", "client")}})
        status, payload, kind = 200, (json.dumps(seen) + "\n").encode(), "application/json"
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) == 2 and parts[0] == "status" and parts[1].isdigit():
            status = int(parts[1])
        elif len(parts) == 2 and parts[0] == "bytes" and parts[1].isdigit():
            payload, kind = b"x" * min(int(parts[1]), 1 << 30), "application/octet-stream"
        elif len(parts) == 2 and parts[0] == "slow":
            time.sleep(min(float(parts[1]), 600))
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = answer


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    ssl_context = None  # set for the HTTPS server: each connection's thread does its handshake (Echo.setup)

    def handle_error(self, request, client_address):
        # A client gone, or one that failed or stalled in the TLS handshake, is no error of the server's.
        if not isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError, socket.timeout)):
            super().handle_error(request, client_address)

    def __init__(self, address, handler, family=socket.AF_INET):
        self.address_family = family
        super().__init__(address, handler)


def family_of(address):
    return socket.AF_INET6 if ":" in address else socket.AF_INET


def certificate(directory, names):
    """A self-signed certificate for NAMES (and localhost) in DIRECTORY: (cert, key)."""
    if not shutil.which("openssl"):
        raise SystemExit("fake_internet: --https needs openssl")
    cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
    alt = ",".join(["DNS:localhost", "IP:127.0.0.1"] + [f"DNS:{n}" for n in names if n and "*" not in n])
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj",
                    "/CN=fake-internet", "-addext", f"subjectAltName={alt}", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    return cert, key


# --- DNS -----------------------------------------------------------------------------------------------------------

def parse_query(packet):
    """(id, flags, name, qtype, end of the question) of a DNS query; ValueError when it is none."""
    if len(packet) < 12:
        raise ValueError("short packet")
    ident, flags, questions = struct.unpack("!HHH", packet[:6])
    if questions != 1:
        raise ValueError("one question expected")
    labels, index = [], 12
    while True:
        if index >= len(packet):
            raise ValueError("truncated name")
        size = packet[index]
        index += 1
        if size == 0:
            break
        if size & 0xC0 or index + size > len(packet):
            raise ValueError("compressed or long label")
        labels.append(packet[index:index + size].decode("ascii", "replace"))
        index += size
    if index + 4 > len(packet):
        raise ValueError("truncated question")
    qtype, _qclass = struct.unpack("!HH", packet[index:index + 4])
    return ident, flags, ".".join(labels).lower(), qtype, index + 4


def lookup(names, name, default=None):
    """The address of NAME in NAMES ({host or *.domain: address}), else DEFAULT."""
    if name in names:
        return names[name]
    for pattern, address in names.items():
        if pattern.startswith("*.") and name.endswith(pattern[1:]):
            return address
    return default


def dns_answer(packet, names, default=None):
    """The answer to the DNS query PACKET: an A record, an empty NOERROR (AAAA and others) or NXDOMAIN."""
    ident, flags, name, qtype, end = parse_query(packet)
    question = packet[12:end]
    address = lookup(names, name, default)
    rd = flags & 0x0100
    if address is None:
        return struct.pack("!HHHHHH", ident, 0x8180 | rd | 3, 1, 0, 0, 0) + question  # NXDOMAIN
    if qtype != 1:
        return struct.pack("!HHHHHH", ident, 0x8180 | rd, 1, 0, 0, 0) + question
    record_ = struct.pack("!HHHLH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(address)
    return struct.pack("!HHHHHH", ident, 0x8180 | rd, 1, 1, 0, 0) + question + record_


def serve_dns(sock, names, default, log):
    """Answer queries until the socket is closed; a query that cannot be answered is dropped, never the server."""
    while True:
        try:
            packet, peer = sock.recvfrom(4096)
        except OSError:
            return  # closed (stop)
        try:
            answer = dns_answer(packet, names, default)
            record(log, {"kind": "dns", "name": parse_query(packet)[2], "client": peer[0]})
            sock.sendto(answer, peer)
        except (ValueError, OSError, struct.error):
            continue


def check_address(value, what):
    """VALUE as an IPv4 address (an A record's); ValueError naming WHAT otherwise."""
    try:
        socket.inet_aton(value)
        if value.count(".") != 3:
            raise OSError
    except (OSError, TypeError):
        raise ValueError(f"{what} expects an IPv4 address: {value!r}") from None
    return value


def build_query(name, qtype=1, ident=0x1234):
    """A DNS query for NAME (tests, and `--query NAME` to check a running server)."""
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split(".") if part)
    return struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + labels + b"\0" + struct.pack("!HH", qtype, 1)


# --- the servers -----------------------------------------------------------------------------------------------------

def start(bind="127.0.0.1", http=None, https=None, dns=None, names=None, default=None, log=None, workdir=None):
    """Start the servers asked for in threads; returns {"http": port, "https": port, "dns": port, "stop": fn}."""
    names = {host: check_address(address, f"--name {host}") for host, address in dict(names or {}).items()}
    if default is not None:
        check_address(default, "--default")
    handler = type("Handler", (Echo,), {"log_file": log})
    started, closers = {}, []
    if http is not None:
        server = Server((bind, http), handler, family_of(bind))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started["http"] = server.server_address[1]
        closers.append(server.shutdown)
    if https is not None:
        directory = workdir or tempfile.mkdtemp(prefix="fake-internet-")
        cert, key = certificate(directory, list(names))
        server = Server((bind, https), handler, family_of(bind))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.ssl_context = context
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started["https"], started["certificate"] = server.server_address[1], str(cert)
        closers.append(server.shutdown)
    if dns is not None:
        sock = socket.socket(family_of(bind), socket.SOCK_DGRAM)
        sock.bind((bind, dns))
        threading.Thread(target=serve_dns, args=(sock, names, default, log), daemon=True).start()
        started["dns"] = sock.getsockname()[1]
        closers.append(sock.close)
    started["stop"] = lambda: [close() for close in closers]
    return started


def main(argv=None):
    p = argparse.ArgumentParser(prog="fake_internet.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--http", type=int)
    p.add_argument("--https", type=int)
    p.add_argument("--dns", type=int)
    p.add_argument("--name", action="append", default=[], metavar="HOST=ADDR")
    p.add_argument("--default", metavar="ADDR", help="the address of every other name (default: NXDOMAIN)")
    p.add_argument("--log", metavar="FILE")
    a = p.parse_args(argv)
    names = {}
    for value in a.name:
        host, sep, address = value.partition("=")
        if not sep or not host:
            p.error(f"--name expects HOST=ADDR: {value!r}")
        names[host.lower()] = address
    if a.http is None and a.https is None and a.dns is None:
        p.error("give at least one of --http, --https, --dns")
    try:
        started = start(a.bind, a.http, a.https, a.dns, names, a.default, a.log)
    except ValueError as exc:
        p.error(str(exc))
    print(json.dumps({k: v for k, v in started.items() if k != "stop"}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
