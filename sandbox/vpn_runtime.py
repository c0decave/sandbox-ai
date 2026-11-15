#!/usr/bin/env python3
"""The VPN container's runtime: tunnel, kill switch and checks. Standard library only.

Runs as `python3 -I /vpn/vpn_runtime.py serve|check` in localhost/sandbox-ai-vpn (Containerfile.vpn; a Tor
profile in localhost/sandbox-ai-tor, Containerfile.tor), in the network namespace of the agent's pod (or of its
own container for `sandbox-ai vpn check` and MCP image servers). The launcher (vpn.py) loads this file by path too, for what both sides share: the DNS client, the
address echo and the kill-switch rules.

The launcher writes one JSON line to stdin: the profile with its secrets, the names to resolve and the
checks. Secrets arrive that way only, never as arguments, environment or files outside this container's
private /run. Events go to stdout as JSON lines ({"event": "progress" | "login" | "ready" | "error" | "down"}).
`serve` keeps the tunnel until stdin closes; `check` exits after the checks. `killswitch` (podman exec in a serving
container) says whether the kill switch is in place: MCP servers start in its namespace only when it is.

Order: the endpoints are resolved first (the only lookups outside the tunnel), then the kill switch is
loaded, and only then is the tunnel brought up: nothing leaves unencrypted while it is being set up, and a
tunnel that dies leaves the kill switch behind (fail closed).

Tor has no tunnel interface: the kill switch lets only tor's own user (debian-tor, TOR_UID) out, over IPv4 TCP
(to its bridges alone when it has some), and a NAT chain hands every other TCP connection of the namespace to
tor's TransPort and every lookup on port 53 to its DNSPort; other UDP and all IPv6 are dropped.

A start's tunnel lent to its own MCP servers (a WireGuard key, Mullvad's too, or a persistent Tailscale node works in
one sandbox at a time): the start's VPN container lends it (payload "lend": a Unix socket, LEND_SOCKET), and each such
server's VPN container borrows it (payload "borrow": no tunnel of its own, a kill switch through which nothing leaves
but over loopback, and a relay to the lender) - see "the start's tunnel, lent" below.
"""
import base64
import contextlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import stat
import secrets
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

# The tunnel's own packets carry this mark (WireGuard's fwmark, OpenVPN's --mark). Setting a mark needs
# CAP_NET_ADMIN (or CAP_NET_RAW) in the network namespace, which the agent and the proxy never have.
MARK = 0xca6c
# tailscaled marks its own sockets (coordination server, DERP, peers) with this value under this mask.
TAILSCALE_MARK = (0x80000, 0xff0000)
TABLE = "sandbox_ai_vpn"
INTERFACES = {"wireguard": "wg0", "openvpn": "tun0", "tailscale": "tailscale0"}
RUN = Path("/run/vpn")
TAILNET = ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
QUAD100 = "100.100.100.100"  # Tailscale's MagicDNS resolver
FALLBACK_DNS = ["1.1.1.1", "8.8.8.8"]  # what the sandbox uses with --dns when the profile names no resolver
# Tor (vpn add --tor). Containerfile.tor creates debian-tor with this uid before Debian's tor package (which keeps
# an existing debian-tor with a system uid): the kill switch and the agent's firewall know tor's packets by it
# (meta skuid), which only a process with CAP_SETUID could take on, and the agent and the proxy have none.
TOR_UID = 950
TOR_USER = "debian-tor"
TOR_TRANS_PORT = 9040
TOR_SOCKS_PORT = 9050
# tor's DNSPort, on a loopback address of its own: the agent's DNS stub has 127.0.0.1:53.
TOR_DNS = "127.0.53.1"
TOR_VIRTUAL_NET = "10.192.0.0/10"  # where AutomapHostsOnResolve maps .onion names
TOR_CHECK = "https://check.torproject.org/api/ip"  # {"IsTor": true, "IP": ...}
# The Tor Project's own onion service: tor maps it at once (no network), an outside resolver has no answer.
ONION_PROBE = "2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion"
OUTSIDE_RESOLVER = "1.1.1.1"  # asked to show that a lookup sent elsewhere reaches tor's DNSPort as well
IPV6_PROBE = "2606:4700:4700::1111"  # a literal IPv6 address that must stay unreachable behind Tor
# Tor's bootstrap (a profile's tor.bootstrap_timeout, bootstrap_attempts, bootstrap_stall; bootstrap_settings):
# each attempt has BOOTSTRAP_TIMEOUT seconds, more while its percentage still rises (up to twice as long); one whose
# percentage stays for BOOTSTRAP_STALL seconds has stalled, and tor starts again (a new guard, fresh circuits), up
# to BOOTSTRAP_ATTEMPTS attempts in all. The kill switch stays loaded all the while.
BOOTSTRAP_TIMEOUT = 300
BOOTSTRAP_ATTEMPTS = 3
BOOTSTRAP_STALL = 90
BOOTSTRAP_LIMITS = {"timeout": (60, 3600), "attempts": (1, 10), "stall": (30, 1800)}
BOOTSTRAP_STILL = 30  # while the percentage stays, "still at N%" this often: the launcher sees the start goes on
BOOTSTRAP_RESTART = 15  # seconds a restart of tor may take (the old one ended, the kill switch looked at)
BRIDGE_TRANSPORTS = {"obfs4": "/usr/bin/obfs4proxy"}
DEFAULT_MTU = 1420
DEFAULT_KEEPALIVE = 25  # keeps the conntrack entry (and NAT on the way) of the endpoint alive
ECHO_LIMIT = 64 << 10
# While serving, the address the echo services see is checked again this often (seconds; the payload's
# checks.recheck, 0: never): a provider's exit that moved, a tunnel that stalls, show during the run, not only at
# its start.
RECHECK = 600
# With a Tor profile the first check during the run comes this soon after the start (seconds): it sees the namespace
# with the agent's own firewall in place, not only as it was before the agent started.
FIRST_RECHECK = 60
# The checks' own sockets carry this mark once the container serves (serve, check, resolve): the agent's firewall in
# the same namespace (agent_sandbox.firewall_rules) lets them pass unredirected, so that a check during the run
# measures the tunnel, not the agent's connection proxy or its allow-list. Setting a mark needs CAP_NET_ADMIN, which
# neither the agent nor the proxy has; the kill switch treats a marked check like any other packet (only MARK, the
# tunnel's own, and tailscaled's mark are let past it), so it leaves through the tunnel, or through Tor.
CHECK_MARK = 0xca6d
# A lent connection's sockets (the start's tunnel, lent to its own MCP servers: Lender, below) carry a mark of their
# own. The agent's firewall lets them by too, unredirected - they are the servers', not the agent's - but only AFTER
# its cut chain (agent_sandbox.firewall_rules), where the checks' mark passes before it: `sandbox-ai net cut` judges a
# lent connection as it judges the agent's own packets (runtime.cut_rules), so what the cut forbids stops, also one
# that was open before. The VPN's kill switch treats it like any other packet: through the tunnel, or not at all.
LEND_MARK = 0xca6e
REQUEST_LIMIT = 8 << 20  # the body of an answer to a request of the launcher (llm check|models through the VPN)
REQUEST_TIMEOUT = 10
SERVE_STATE = RUN / "serve.json"  # what `resolve` (podman exec during a run) needs of the tunnel: its resolvers
QTYPES = {4: 1, 6: 28}  # A, AAAA
SECRET_PATTERNS = [re.compile(r"[A-Za-z0-9+/]{42,43}="), re.compile(r"tskey-[A-Za-z0-9-]+"),
                   re.compile(r"\b[0-9]{16}\b")]  # WireGuard keys, Tailscale auth keys, Mullvad accounts


def emit(event, out=None, **fields):
    """One event line for the launcher."""
    stream = out or sys.stdout
    stream.write(json.dumps({"event": event, **fields}) + "\n")
    stream.flush()


def redact(text, extra=()):
    """TEXT without keys, auth keys, account numbers or the given secret values."""
    text = str(text)
    for value in extra:
        if value:
            text = text.replace(value, "[secret]")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[secret]", text)
    return text


# --- DNS: a small stub client, so that lookups go to the resolver named, through the tunnel -----------------

def encode_name(name):
    labels = name.rstrip(".").split(".")
    if not name or len(name) > 253:
        raise ValueError(f"invalid name {name!r}")
    out = b""
    for label in labels:
        raw = label.encode("idna") if not label.isascii() else label.encode()
        if not 0 < len(raw) < 64:
            raise ValueError(f"invalid name {name!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def dns_query_packet(name, qtype, ident):
    return struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + encode_name(name) + struct.pack("!HH", qtype, 1)


def skip_name(data, offset):
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS reply")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += 1 + length


RCODES = {1: "FORMERR", 2: "SERVFAIL", 4: "NOTIMP", 5: "REFUSED"}


class DnsError(ValueError):
    """A resolver's answer with an error code other than NXDOMAIN (RCODE: 2 is SERVFAIL)."""

    def __init__(self, rcode):
        self.rcode = int(rcode)
        named = RCODES.get(self.rcode)
        super().__init__(f"DNS error code {self.rcode}" + (f" ({named})" if named else ""))


def dns_answers(data, ident, qtype):
    """Addresses of type QTYPE in a reply to query IDENT; [] for NXDOMAIN or no data. The resolver follows CNAME
    chains and returns their records in the same answer."""
    if len(data) < 12:
        raise ValueError("short DNS reply")
    rid, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", data[:12])
    if rid != ident or not flags & 0x8000:
        raise ValueError("not a reply to this query")
    rcode = flags & 0xF
    if rcode == 3:
        return []
    if rcode:
        raise DnsError(rcode)
    offset = 12
    for _ in range(qdcount):
        offset = skip_name(data, offset) + 4
    found = []
    for _ in range(ancount):
        offset = skip_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("truncated DNS reply")
        rtype, rclass, _ttl, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + length]
        if len(rdata) != length:
            raise ValueError("truncated DNS reply")
        offset += length
        if rclass == 1 and rtype == qtype == 1 and length == 4:
            found.append(str(ipaddress.IPv4Address(rdata)))
        elif rclass == 1 and rtype == qtype == 28 and length == 16:
            found.append(str(ipaddress.IPv6Address(rdata)))
    return found


def dns_lookup(name, server, qtype, timeout=4.0, port=53):
    """Addresses of NAME from the resolver SERVER: UDP, TCP when the reply is truncated."""
    ident = secrets.randbits(16)
    packet = dns_query_packet(name, qtype, ident)
    family = socket.AF_INET6 if ipaddress.ip_address(server).version == 6 else socket.AF_INET
    deadline = time.monotonic() + timeout
    with check_socket(family, socket.SOCK_DGRAM) as sock:
        sock.connect((server, port))
        sock.send(packet)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([sock], [], [], remaining)[0]:
                raise TimeoutError(f"no answer from {server}")
            data = sock.recv(4096)
            if len(data) >= 4 and struct.unpack("!H", data[:2])[0] == ident:
                break  # anything else is a stray or spoofed packet: keep waiting
    if struct.unpack("!H", data[2:4])[0] & 0x0200:  # truncated: ask again over TCP
        with check_connection((server, port), timeout=max(deadline - time.monotonic(), 0.5)) as sock:
            sock.sendall(struct.pack("!H", len(packet)) + packet)
            length = struct.unpack("!H", read_exact(sock, 2))[0]
            data = read_exact(sock, length)
    return dns_answers(data, ident, qtype)


def read_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ValueError("connection closed")
        data += chunk
    return data


def resolver_for(servers, lookup=None):
    """A resolver (name, family) -> [addresses] that asks SERVERS in turn; literal addresses stay as they are."""
    def resolve(name, family):
        ask = lookup or dns_lookup
        try:
            address = ipaddress.ip_address(name)
            return [str(address)] if address.version == family else []
        except ValueError:
            pass
        errors = []
        for server in servers:
            try:
                return ask(name, server, QTYPES[family])
            except (OSError, ValueError) as exc:
                errors.append(f"{server}: {exc}")
        raise OSError("; ".join(errors) or "no resolver")
    return resolve


# tor says "Bootstrapped 100%" before it has a circuit an exit looks names up through: until then its DNSPort answers
# SERVFAIL (or nothing), and it does so for a name one exit cannot resolve. Such an answer means "not yet" (TorDns).
TOR_DNS_PATIENCE = 90  # seconds: how long the lookups wait for tor's first circuit after its bootstrap
TOR_DNS_NAME_PATIENCE = 30  # seconds: once tor has resolved a name, how long another name's SERVFAIL is asked again
TOR_DNS_RESOLVE_PATIENCE = 20  # seconds: a name allowed during the run (vpn.resolver_for_run waits 30 s in all)
# seconds, in all: a lookup the launcher asks the serving container for (handle_request: sandbox-ai llm check's
# resolve and connect, which wait 15 s beyond their own timeout)
TOR_DNS_REQUEST_BUDGET = 12
TOR_DNS_DELAYS = (2, 3, 5, 8, 10, 15)  # the pauses before the 2nd, 3rd, ... try
TOR_DNS_WELL_KNOWN = "check.torproject.org"  # asked too while tor has no circuit: no circuit, or a failing name?


class TorDnsError(OSError):
    """tor's DNSPort gave no answer for a name within the patience (TorDns)."""


class TorDns:
    """Lookups at tor's DNSPort after its bootstrap, patient with SERVFAIL and with no answer: asked again after
    TOR_DNS_DELAYS - while no lookup has worked yet, up to PATIENCE seconds in all for tor's first circuit (every
    other try asks TOR_DNS_WELL_KNOWN too, which tells a missing circuit from a name that fails); once one has, up
    to NAME_PATIENCE seconds for each name. Each try is said (OUT: the launcher's progress). Never past DEADLINE
    (CLOCK's time: the launcher's wait). Only the server it is given is asked - tor's DNSPort on loopback, behind the
    kill switch, which nothing here touches. Any other error (NXDOMAIN is an empty answer, the kill switch's refusal)
    is not tried again."""

    def __init__(self, out=None, patience=TOR_DNS_PATIENCE, name_patience=TOR_DNS_NAME_PATIENCE, deadline=None,
                 clock=time.monotonic, sleep=time.sleep, ask=None, circuit=False):
        self.out, self.patience, self.name_patience, self.deadline = out, patience, name_patience, deadline
        self.clock, self.sleep, self.ask = clock, sleep, ask
        self.circuit = circuit  # a lookup through tor has answered: tor has a circuit
        self.waiting = None  # when the wait for the first circuit began
        self.unanswered = []  # what each lookup without an answer got while there was no circuit (any name)

    @staticmethod
    def not_yet(exc):
        """What an answer of tor's DNSPort that means "not yet" is called, else None."""
        if isinstance(exc, DnsError) and exc.rcode == 2:
            return "SERVFAIL"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "no answer"
        return None

    def say(self, text):
        if self.out is not None:
            emit("progress", out=self.out, message=text)

    def lookup(self, name, server, qtype, timeout=4.0, port=53):
        ask = self.ask or dns_lookup
        tries, seen, begun, known, entered = 0, [], None, None, self.clock()
        while True:
            tries += 1
            try:
                found = ask(name, server, qtype, timeout=timeout, port=port)
            except (OSError, ValueError) as exc:
                what = self.not_yet(exc)
                if what is None:
                    raise
                seen.append(what)
                if not self.circuit:
                    self.unanswered.append(what)
            else:
                # tor maps an .onion name itself: its answer proves no circuit to an exit
                self.circuit = self.circuit or not name.rstrip(".").lower().endswith(".onion")
                return found
            now = self.clock()
            begun = now if begun is None else begun
            if not self.circuit:
                self.waiting = now if self.waiting is None else self.waiting
                if tries % 2 == 0 and name.rstrip(".").lower() != TOR_DNS_WELL_KNOWN:
                    try:
                        ask(TOR_DNS_WELL_KNOWN, server, 1, timeout=timeout, port=port)
                        self.circuit, begun, known = True, now, True  # the name's own patience from now on
                    except (OSError, ValueError) as exc:
                        known = False
                        if self.not_yet(exc) is None:
                            raise
                        self.unanswered.append(self.not_yet(exc))
            start = begun if self.circuit else self.waiting
            patience = self.name_patience if self.circuit else self.patience
            delay = TOR_DNS_DELAYS[min(tries - 1, len(TOR_DNS_DELAYS) - 1)]
            if now + delay - start > patience or \
                    (self.deadline is not None and now + delay + timeout > self.deadline):
                raise TorDnsError(self.failure(name, seen, now - (begun if self.circuit else entered), known, server,
                                               waited=now - start))
            if self.circuit:
                self.say(f"Tor: {name} has no answer through Tor yet ({seen[-1]}, try {tries}); asking again")
            else:
                self.say(f"Tor: waiting for its first circuit (DNS through Tor: {seen[-1]}, try {tries})")
            self.sleep(delay)

    def failure(self, name, seen, seconds, known, server=TOR_DNS, waited=None):
        """Why NAME has no answer: what it was asked how often (SEEN: each try's "not yet", in SECONDS), and while
        there was no circuit how many lookups of any name went unanswered in WAITED seconds."""
        def count(what):
            return ", ".join(f"{kind} {what.count(kind)}x" for kind in dict.fromkeys(what))
        asked = f"{len(seen)} tr{'ies' if len(seen) != 1 else 'y'} in {int(seconds)} s: {count(seen)}"
        if not self.circuit and len(self.unanswered) > len(seen):
            asked += (f"; {len(self.unanswered)} lookups of any name in {int(waited or seconds)} s since the first "
                      f"unanswered one: {count(self.unanswered)}")
        if name.rstrip(".").lower().endswith(".onion"):  # tor maps these itself, without a circuit or an exit
            via = "" if server == TOR_DNS else f", sent to {server} and redirected there"
            return (f"tor's DNSPort {TOR_DNS} did not map {name} into its virtual network ({asked}{via}): tor is not "
                    "answering lookups yet. Try again in a minute. Nothing left past Tor: the kill switch held "
                    "throughout")
        if self.circuit:
            return (f"tor resolves other names, but not {name} ({asked} from tor's DNSPort {TOR_DNS}): the Tor exits "
                    "could not look it up. Is the name right and its DNS working? Try again later")
        neither = f"; {TOR_DNS_WELL_KNOWN} neither" if known is False else ""
        return (f"tor has bootstrapped, but its DNSPort {TOR_DNS} answered no lookup of {name} ({asked}{neither}): "
                "tor has no working circuit to an exit yet. Try again in a minute; if it stays so, the Tor network "
                "is slow or blocked from here (bridges: sandbox-ai vpn add NAME --tor --bridges FILE), or this "
                "host's clock is off. Nothing left past Tor: the kill switch held throughout")


def system_resolver(name, family):
    """The host's (or container's) own resolver, for one address family."""
    kind = socket.AF_INET if family == 4 else socket.AF_INET6
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(name, 443, family=kind, type=socket.SOCK_STREAM)})
    except socket.gaierror as exc:
        if exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5), getattr(socket, "EAI_ADDRFAMILY", -9)):
            return []
        raise OSError(str(exc)) from None


# --- the address echo: who the internet thinks we are, over IPv4 and IPv6 separately -------------------------

_check_mark = None  # CHECK_MARK once use_check_mark() ran (the serving container), else None


def use_check_mark(mark=CHECK_MARK):
    """Mark the checks' sockets from now on (in the VPN container, which has CAP_NET_ADMIN)."""
    global _check_mark
    _check_mark = mark


def check_socket(family, kind):
    """A socket for a check: marked with CHECK_MARK when use_check_mark() ran. A mark that cannot be set (no
    CAP_NET_ADMIN: a plain container for the baseline) leaves it unmarked."""
    sock = socket.socket(family, kind)
    if _check_mark is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK", 36), _check_mark)
    return sock


def check_connection(address, timeout=None):
    """socket.create_connection for the IP literal ADDRESS (address, port), through check_socket."""
    host, port = address[0], int(address[1])
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(host.split("%")[0]).version == 6 else socket.AF_INET
    except ValueError:  # a name (only in tests: the checks connect to the addresses they resolved)
        return socket.create_connection((host, port), timeout)
    sock = check_socket(family, socket.SOCK_STREAM)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        sock.connect((host, port))
    except BaseException:
        sock.close()
        raise
    return sock


class PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP to ADDRESS, whatever the host name resolves to elsewhere (the name came from our own resolver)."""

    def __init__(self, host, address, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = check_connection((self.address, self.port), self.timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ADDRESS with the certificate checked for the host name (SNI)."""

    def __init__(self, host, address, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self.address = address
        self.tls = context

    def connect(self):
        sock = check_connection((self.address, self.port), self.timeout)
        self.sock = self.tls.wrap_socket(sock, server_hostname=self.host)


def fetch(url, address, timeout=8.0, context=None, limit=ECHO_LIMIT):
    """(status, body) of GET URL from ADDRESS."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        port = parts.port or 443
        connection = PinnedHTTPSConnection(parts.hostname, address, port, timeout,
                                           context or ssl.create_default_context())
    elif parts.scheme == "http":
        port = parts.port or 80
        connection = PinnedHTTPConnection(parts.hostname, address, port, timeout)
    else:
        raise ValueError(f"unsupported URL {url}")
    try:
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection.request("GET", target, headers={"User-Agent": "sandbox-ai-vpn-check",
                                                   "Accept": "application/json, text/plain"})
        response = connection.getresponse()
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("reply too large")
        return response.status, body
    finally:
        connection.close()


def parse_echo(body):
    """(address, provider says VPN or None) of an echo reply: JSON with "ip" (Mullvad: "mullvad_exit_ip") or a bare
    address."""
    text = body.decode("utf-8", "replace").strip()
    vpn = None
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        document = None
    if isinstance(document, dict):
        value = next((document[key] for key in ("ip", "IP", "address", "ip_addr") if isinstance(document.get(key), str)), "")
        if isinstance(document.get("mullvad_exit_ip"), bool):
            vpn = document["mullvad_exit_ip"]
        if isinstance(document.get("IsTor"), bool):  # check.torproject.org/api/ip
            vpn = document["IsTor"]
    else:
        value = text.splitlines()[0] if text else ""
    return str(ipaddress.ip_address(value.strip())), vpn


BLOCKED_ERRNOS = {1, 13, 101, 113}  # EPERM (the kill switch), EACCES, ENETUNREACH, EHOSTUNREACH


def echo(urls, family, resolve, get=fetch, timeout=8.0):
    """What the first echo service that answers over IPv4 or IPv6 (FAMILY) sees: {"ip", "vpn", "url"}, or
    {"error", "kind"}: "blocked" (no route or refused locally), "no-address" (the service has no address of this
    family), "failed" (anything else)."""
    errors, kinds, timeouts, refused = [], set(), 0, 0
    for url in urls:
        host = urllib.parse.urlsplit(url).hostname
        try:
            addresses = resolve(host, family)
        except (OSError, ValueError) as exc:
            errors.append(f"{host}: lookup failed: {exc}")
            kinds.add("failed")
            continue
        if not addresses:
            errors.append(f"{host}: no IPv{family} address")
            kinds.add("no-address")
            continue
        try:
            status, body = get(url, addresses[0], timeout)
            if status != 200:
                raise ValueError(f"HTTP {status}")
            ip, vpn = parse_echo(body)
            if ipaddress.ip_address(ip).version != family:
                raise ValueError(f"answered with {ip}")
            return {"ip": ip, "vpn": vpn, "url": url}
        except OSError as exc:
            timed_out = isinstance(exc, (socket.timeout, TimeoutError))
            blocked = exc.errno in BLOCKED_ERRNOS or timed_out
            timeouts += 1 if timed_out and exc.errno not in BLOCKED_ERRNOS else 0
            refused += 1 if exc.errno in BLOCKED_ERRNOS else 0
            kinds.add("blocked" if blocked else "failed")
            errors.append(f"{host}: {exc.strerror or exc}")
        except (ValueError, http.client.HTTPException) as exc:
            kinds.add("failed")
            errors.append(f"{host}: {exc}")
    kind = "blocked" if kinds == {"blocked"} or kinds == {"blocked", "no-address"} else \
        "no-address" if kinds == {"no-address"} else "failed"
    # "timeout": nothing was refused here (EPERM, EACCES, no route: the kill switch, a missing route), only no answer
    # came in time - a tunnel that just came up (echo_retried tries it again).
    return {"error": "; ".join(errors) or "no echo service configured", "kind": kind,
            **({"timeout": True} if timeouts and not refused else {})}


# A connection through a tunnel that just came up (above all a Tor circuit after bootstrap: a slow or refusing exit,
# a TLS connection closed at once) can fail once and work a few seconds later. The address check tries again before
# it says "no connection" - only for an error, never past an answer (an address that is not the VPN's fails at once).
ATTEMPTS, TOR_ATTEMPTS = 2, 4
RETRY_DELAYS = (3, 6, 12)  # seconds before the 2nd, 3rd, 4th attempt (Tor: about 21 s in all, plus each attempt's own)


def echo_retried(urls, family, resolve, get=fetch, timeout=8.0, tor=False, attempts=ATTEMPTS, sleep=time.sleep,
                 deadline=None, clock=time.monotonic):
    """echo(), tried up to ATTEMPTS times while it ends in an error: behind Tor any error (a new circuit may pick
    another exit), else one that is "failed" or a timeout with nothing refused (echo's "timeout") - never what the
    kill switch or a missing route refuses (EPERM, EACCES, ENETUNREACH, EHOSTUNREACH), nor "no-address". DEADLINE
    (CLOCK's time): no attempt starts that could not end before it (TIMEOUT for each echo service); the answer says
    "cut_short" then. The answer of the last attempt, with "attempts" when more than one was made."""
    attempts = max(1, int(attempts))
    per_attempt = float(timeout) * max(1, len(urls))
    cut_short = False
    for attempt in range(attempts):
        result = echo(urls, family, resolve, get, timeout)
        retry = "error" in result and (tor and result.get("kind") != "no-address" or result.get("kind") == "failed"
                                       or bool(result.get("timeout")))
        if not retry or attempt == attempts - 1:
            break
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        if deadline is not None and clock() + delay + per_attempt > deadline:
            cut_short = True  # the launcher would stop waiting before another one ended: the reason is this one
            break
        sleep(delay)
    if attempt:
        result = {**result, "attempts": attempt + 1}
    if cut_short:
        result = {**result, "cut_short": True}
    return result


# --- the kill switch -------------------------------------------------------------------------------------------

def mark_match(mark=MARK, mask=None):
    """The nft expression for packets carrying the tunnel's mark."""
    return f"meta mark and {mask:#x} == {mark:#x}" if mask else f"meta mark {mark:#x}"


def killswitch_rules(interface, endpoints=(), mark=MARK, mask=None, any_marked=False, tunnel_ipv6=True):
    """The table that lets nothing leave this network namespace except through INTERFACE, plus the tunnel's own
    packets (carrying MARK) to its ENDPOINTS [(address, "udp"|"tcp", port)]. ANY_MARKED (Tailscale, whose DERP
    relays, coordination server and peers change at run time) lets marked packets go anywhere. Without
    TUNNEL_IPV6 (the tunnel has no IPv6 address) no IPv6 packet leaves at all, the tunnel included.

    Established connections are not accepted by state: one that left through the tunnel must not continue over
    the uplink when the tunnel goes away. Connections the agent's firewall redirects to its proxy (loopback after
    NAT) pass; the proxy's own connections meet this table again."""
    match = mark_match(mark, mask)
    lines = [f"table inet {TABLE} {{",
             " chain output { type filter hook output priority -5; policy drop;",
             '  oifname "lo" accept']
    if not tunnel_ipv6:
        lines.append(f'  oifname "{interface}" meta nfproto ipv6 drop')
    lines += [f'  oifname "{interface}" accept',
              "  ct status dnat ip daddr 127.0.0.0/8 accept", "  ct status dnat ip6 daddr ::1 accept"]
    if any_marked:
        lines.append(f"  {match} accept")
    for address, proto, port in endpoints:
        ip = ipaddress.ip_address(address)
        if proto not in ("udp", "tcp"):
            raise ValueError(f"endpoint protocol {proto!r}")
        lines.append(f"  {match} {'ip' if ip.version == 4 else 'ip6'} daddr {ip} {proto} dport {int(port)} accept")
    lines += ["  ip6 hoplimit 255 icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept",
              "  reject with icmpx type admin-prohibited", " }",
              " chain input { type filter hook input priority -5; policy drop;",
              '  iifname "lo" accept', "  ct state established,related accept",
              "  ip6 hoplimit 255 icmpv6 type { nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept",
              " }",
              " chain forward { type filter hook forward priority -5; policy drop; }",
              "}", ""]
    return "\n".join(lines)


def covers_all(networks, version):
    """True if NETWORKS (CIDR strings) together cover every address of IP VERSION (0.0.0.0/0, or /1 + /1, ...)."""
    nets = [ipaddress.ip_network(n, strict=False) for n in networks]
    nets = [n for n in nets if n.version == version]
    whole = ipaddress.ip_network("0.0.0.0/0" if version == 4 else "::/0")
    return list(ipaddress.collapse_addresses(nets)) == [whole] if nets else False


# --- WireGuard -------------------------------------------------------------------------------------------------

def format_endpoint(address, port):
    ip = ipaddress.ip_address(address)
    return f"[{ip}]:{int(port)}" if ip.version == 6 else f"{ip}:{int(port)}"


def wg_setconf(wireguard, private_key, preshared, endpoints, mark=MARK):
    """The `wg setconf` text: keys, the mark and every peer, with each endpoint as the address resolved once at
    the start (a later lookup could point the tunnel somewhere the kill switch does not allow)."""
    lines = ["[Interface]", f"PrivateKey = {private_key}", f"FwMark = {mark:#x}"]
    if wireguard.get("listen_port"):
        lines.append(f"ListenPort = {int(wireguard['listen_port'])}")
    for index, peer in enumerate(wireguard["peers"]):
        lines += ["", "[Peer]", f"PublicKey = {peer['public_key']}"]
        if preshared.get(str(index)):
            lines.append(f"PresharedKey = {preshared[str(index)]}")
        lines.append("AllowedIPs = " + ", ".join(peer["allowed_ips"]))
        if str(index) in endpoints:
            address, port = endpoints[str(index)]
            lines.append(f"Endpoint = {format_endpoint(address, port)}")
            lines.append(f"PersistentKeepalive = {int(peer.get('keepalive') or DEFAULT_KEEPALIVE)}")
        elif peer.get("keepalive"):
            lines.append(f"PersistentKeepalive = {int(peer['keepalive'])}")
    return "\n".join(lines) + "\n"


def parse_default_routes(text4, text6):
    """{4: (gateway or None, device), 6: ...} from `ip -4/-6 route show default`."""
    routes = {}
    for version, text in ((4, text4), (6, text6)):
        for line in text.splitlines():
            match = re.match(r"default(?: via (\S+))?(?: .*?)? dev (\S+)", line.strip())
            if match:
                routes[version] = (match[1], match[2])
                break
    return routes


def route_commands(interface, addresses, allowed, endpoints, defaults, mtu=DEFAULT_MTU):
    """ip(8) argument lists that put the tunnel's networks on INTERFACE. The endpoints keep their way out over the
    uplink (DEFAULTS {4: (gateway, device), 6: ...}); a /0 replaces the default route; a family the tunnel has no
    address of gets no route at all (the kill switch drops it). Nothing else changes: split tunnels route only
    their networks, and the kill switch refuses the rest."""
    commands = []
    for address in dict.fromkeys(addr for addr, _, _ in endpoints):
        ip = ipaddress.ip_address(address)
        if ip.version not in defaults:
            raise ValueError(f"the endpoint {ip} needs an IPv{ip.version} uplink, which this network has not")
        gateway, device = defaults[ip.version]
        commands.append(["ip", f"-{ip.version}", "route", "replace", f"{ip}/{ip.max_prefixlen}",
                         *(["via", gateway] if gateway else []), "dev", device])
    for address in addresses:
        version = ipaddress.ip_interface(address).version
        commands.append(["ip", f"-{version}", "address", "add", address, "dev", interface])
    commands.append(["ip", "link", "set", "dev", interface, "mtu", str(int(mtu)), "up"])
    families = {ipaddress.ip_interface(address).version for address in addresses}
    seen = set()
    for network in allowed:
        net = ipaddress.ip_network(network, strict=False)
        if net.version not in families or net in seen:
            continue
        seen.add(net)
        commands.append(["ip", f"-{net.version}", "route", "replace", "default" if net.prefixlen == 0 else str(net),
                         "dev", interface])
    return commands


def resolve_endpoint(host, port, resolve=system_resolver):
    """(address, port) of a peer's endpoint: an address as it is, a name resolved once (IPv4 preferred)."""
    for family in (4, 6):
        addresses = resolve(host, family)
        if addresses:
            return addresses[0], int(port)
    raise ValueError(f"the endpoint {host} has no address")


def wireguard_tunnel(wireguard, endpoints):
    """(tunnel facts) of a WireGuard profile once its endpoints are known."""
    allowed = [n for peer in wireguard["peers"] for n in peer["allowed_ips"]]
    families = {ipaddress.ip_interface(a).version for a in wireguard["addresses"]}
    return {"interface": INTERFACES["wireguard"], "addresses": list(wireguard["addresses"]),
            "ipv4": 4 in families, "ipv6": 6 in families,
            "full4": 4 in families and covers_all(allowed, 4), "full6": 6 in families and covers_all(allowed, 6),
            "endpoints": [format_endpoint(a, p) for a, p in endpoints.values()]}


# --- Tor --------------------------------------------------------------------------------------------------------

def parse_bridges(text):
    """[{"transport", "address", "port", "fingerprint", "args"}] of bridge lines: `[Bridge] [obfs4] IPV4:PORT
    [FINGERPRINT] [KEY=VALUE ...]`; empty lines and # comments are skipped. An error names the line's number, never
    its contents: a bridge's address is its secret."""
    bridges = []
    for number, raw in enumerate(text.splitlines(), 1):
        words = raw.split()
        if not words or words[0].startswith("#"):
            continue
        if words[0].lower() == "bridge":
            words = words[1:]
        where = f"bridge line {number}"
        transport = None
        if words and not re.match(r"[0-9\[]", words[0]):
            transport = words.pop(0).lower()
            if transport == "snowflake":
                raise ValueError(f"{where}: snowflake needs UDP (WebRTC), which the kill switch drops; use obfs4 "
                                 "bridges")
            if transport not in BRIDGE_TRANSPORTS:
                raise ValueError(f"{where}: the transport {transport[:24]!r} is not supported (obfs4 or plain "
                                 "bridges)")
        if not words:
            raise ValueError(f"{where}: no address")
        address = words.pop(0)
        if address.startswith("["):
            raise ValueError(f"{where}: IPv6 bridges are not supported (tor runs with ClientUseIPv6 0 here)")
        match = re.fullmatch(r"([0-9.]{7,15}):([0-9]{1,5})", address)
        if not match:
            raise ValueError(f"{where}: expected IPV4:PORT after the transport")
        try:
            ip = ipaddress.IPv4Address(match[1])
        except ValueError:
            raise ValueError(f"{where}: invalid address") from None
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local:
            raise ValueError(f"{where}: not a bridge's address")
        port = int(match[2])
        if not 1 <= port <= 65535:
            raise ValueError(f"{where}: the port must be 1-65535")
        fingerprint = None
        if words and re.fullmatch(r"[0-9A-Fa-f]{40}", words[0]):
            fingerprint = words.pop(0).upper()
        args = []
        for word in words:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}=[A-Za-z0-9+/=._,:-]{1,512}", word):
                raise ValueError(f"{where}: expected KEY=VALUE options after the fingerprint")
            args.append(word)
        if transport == "obfs4":
            keys = {arg.split("=", 1)[0] for arg in args}
            if not {"cert", "iat-mode"} <= keys:
                raise ValueError(f"{where}: an obfs4 bridge needs cert= and iat-mode=")
        elif args:
            raise ValueError(f"{where}: options belong to a transport (obfs4 ...)")
        bridges.append({"transport": transport, "address": str(ip), "port": port, "fingerprint": fingerprint,
                        "args": args})
    if len(bridges) > 32:
        raise ValueError("more than 32 bridges")
    return bridges


def bridge_line(bridge):
    return " ".join(["Bridge", *([bridge["transport"]] if bridge["transport"] else []),
                     f"{bridge['address']}:{int(bridge['port'])}",
                     *([bridge["fingerprint"]] if bridge["fingerprint"] else []), *bridge["args"]])


def exit_countries(values):
    codes = [str(value).lower() for value in values or []]
    if len(codes) > 16 or not all(re.fullmatch(r"[a-z]{2}", code) for code in codes):
        raise ValueError("exit countries are two-letter country codes (de, or de,nl,ch)")
    return codes


def tor_config(tor, bridges=()):
    """The torrc: a client only, TransPort and DNSPort for the whole namespace, .onion names mapped into
    TOR_VIRTUAL_NET, a SocksPort only when the profile asks for one, IPv4 only, the exit countries, the bridges."""
    lines = ["# sandbox-ai: written for this start from the profile; nothing else is read",
             f"User {TOR_USER}", "DataDirectory /var/lib/tor", "RunAsDaemon 0", "Log notice stdout", "SafeLogging 1",
             "AvoidDiskWrites 1", "ClientOnly 1", "ControlPort 0", "ClientUseIPv4 1", "ClientUseIPv6 0",
             "ClientPreferIPv6ORPort 0",
             f"SocksPort 127.0.0.1:{TOR_SOCKS_PORT} IsolateDestAddr" if tor.get("socks") else "SocksPort 0",
             f"TransPort 127.0.0.1:{TOR_TRANS_PORT} IsolateDestAddr", f"DNSPort {TOR_DNS}:53",
             "AutomapHostsOnResolve 1", "AutomapHostsSuffixes .onion", f"VirtualAddrNetworkIPv4 {TOR_VIRTUAL_NET}"]
    countries = exit_countries(tor.get("exit_country"))
    if countries:
        lines += ["ExitNodes " + ",".join("{" + code + "}" for code in countries), "StrictNodes 1"]
    if bridges:
        lines.append("UseBridges 1")
        for transport in sorted({b["transport"] for b in bridges if b["transport"]}):
            lines.append(f"ClientTransportPlugin {transport} exec {BRIDGE_TRANSPORTS[transport]}")
        lines += [bridge_line(bridge) for bridge in bridges]
    return "\n".join(lines) + "\n"


# The networks a Tor start may reach past Tor (--vpn-bypass-tcp): the local network's, never one on the internet,
# which would see your own address. Carrier-grade NAT (100.64.0.0/10) lies outside your network.
BYPASS_NETWORKS = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                           "169.254.0.0/16"))


METADATA_ADDRESSES = (ipaddress.IPv4Address("169.254.169.254"), ipaddress.IPv4Address("169.254.170.2"))


def bypass_target(value):
    """(address, port) of one --vpn-bypass-tcp value, an IPv4 ADDRESS:PORT in the local network."""
    if isinstance(value, dict):
        address, port = value.get("address"), value.get("port")
    else:
        address, sep, port = str(value).rpartition(":")
        if not sep:
            raise ValueError(f"--vpn-bypass-tcp {value}: give ADDRESS:PORT, e.g. 192.168.1.20:1234")
    try:
        ip = ipaddress.IPv4Address(str(address))
        port = int(port)
    except ValueError:
        raise ValueError(f"--vpn-bypass-tcp {value}: give an IPv4 address and a port, e.g. 192.168.1.20:1234") from None
    if not 0 < port < 65536:
        raise ValueError(f"--vpn-bypass-tcp {value}: the port must lie between 1 and 65535")
    if ip in METADATA_ADDRESSES:
        raise ValueError(f"--vpn-bypass-tcp {value}: {ip} is a cloud's metadata service (instance credentials), "
                         "never reached past Tor")
    if not any(ip in network for network in BYPASS_NETWORKS):
        raise ValueError(f"--vpn-bypass-tcp {value}: only an address of your local network (10.0.0.0/8, "
                         "172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16) may be reached past Tor; one on the internet "
                         "would see your own address")
    return str(ip), port


def tor_rules(uid=TOR_UID, bridges=(), bypass=()):
    """The kill switch for tor: only tor's own TCP over IPv4 leaves (to its bridges alone when it has some); every
    other TCP connection is redirected to its TransPort and every lookup on port 53 to its DNSPort, before the
    filter sees them; other UDP (QUIC included) and all IPv6 are dropped. The redirect runs after the agent's own
    to its connection proxy (priority -100): what that one redirects reaches tor through the proxy. BYPASS
    (--vpn-bypass-tcp): TCP to exactly these addresses and ports of the local network leaves directly, neither
    redirected nor dropped; the agent reaches them through its connection proxy only, as everything else."""
    uid = int(uid)
    targets = [bypass_target(value) for value in bypass]
    lines = [f"table inet {TABLE} {{",
             " chain output { type filter hook output priority -5; policy drop;",
             '  oifname "lo" accept',
             "  ct status dnat ip daddr 127.0.0.0/8 accept", "  ct status dnat ip6 daddr ::1 accept",
             "  meta nfproto ipv6 reject with icmpx type admin-prohibited"]
    for bridge in bridges:
        lines.append(f"  meta skuid {uid} ip daddr {ipaddress.IPv4Address(bridge['address'])} tcp dport "
                     f"{int(bridge['port'])} accept")
    if not bridges:
        lines.append(f"  meta skuid {uid} meta nfproto ipv4 meta l4proto tcp accept")
    lines += [f"  ip daddr {address} tcp dport {port} accept" for address, port in targets]
    lines += ["  reject with icmpx type admin-prohibited", " }",
              " chain input { type filter hook input priority -5; policy drop;",
              '  iifname "lo" accept', "  ct state established,related accept", " }",
              " chain forward { type filter hook forward priority -5; policy drop; }",
              " chain divert { type nat hook output priority -90; policy accept;",
              f"  meta skuid {uid} return", "  ip daddr 127.0.0.0/8 return", "  meta nfproto ipv6 return",
              *[f"  ip daddr {address} tcp dport {port} return" for address, port in targets],
              f"  udp dport 53 dnat ip to {TOR_DNS}:53", f"  meta l4proto tcp redirect to :{TOR_TRANS_PORT}", " }",
              "}", ""]
    return "\n".join(lines)


BOOTSTRAPPED = re.compile(r"Bootstrapped (\d{1,3})%(?: \(([a-z0-9_]+)\))?:? ?(.*)")


def bootstrap_progress(text):
    """(percent, summary) of tor's last bootstrap line in TEXT; (0, None) before the first."""
    found = (0, None)
    for line in text.splitlines():
        match = BOOTSTRAPPED.search(line)
        if match:
            found = (min(int(match[1]), 100), (match[3] or match[2] or "").strip()[:120] or None)
    return found


def bootstrap_settings(tor):
    """{"timeout", "attempts", "stall"} of Tor's bootstrap from a profile's "tor" (TOR: its bootstrap_timeout,
    bootstrap_attempts, bootstrap_stall; the defaults where it has none); ValueError for one out of its limits."""
    tor = tor if isinstance(tor, dict) else {}
    found = {}
    for key, default in (("timeout", BOOTSTRAP_TIMEOUT), ("attempts", BOOTSTRAP_ATTEMPTS), ("stall", BOOTSTRAP_STALL)):
        value = tor.get(f"bootstrap_{key}")
        value = default if value is None else value
        low, high = BOOTSTRAP_LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"Tor's bootstrap_{key} is {low}-{high}" + (" seconds" if key != "attempts" else "")
                             + f", not {value!r}")
        found[key] = value
    return found


def attempt_seconds(settings):
    """How long one attempt at the bootstrap may take at most (its timeout, twice that while it still rises)."""
    return 2 * settings["timeout"] + BOOTSTRAP_RESTART


def bootstrap_seconds(settings):
    """How long the bootstrap may take at most, every attempt included (vpn.ready_timeout waits that long)."""
    return settings["attempts"] * attempt_seconds(settings)


class BootstrapStalled(RuntimeError):
    """An attempt at the bootstrap that ended without 100% while tor still ran: tor is started again."""

    def __init__(self, message, percent, summary):
        super().__init__(message)
        self.percent, self.summary = percent, summary


def read_from(path, offset):
    try:
        with open(path, "rb") as stream:
            stream.seek(offset)
            return stream.read().decode(errors="replace")
    except OSError:
        return ""


def wait_bootstrap(log_path, process, timeout=BOOTSTRAP_TIMEOUT, out=None, secrets=(), clock=time.monotonic,
                   sleep=time.sleep, stall=None, attempt=(1, 1), offset=0):
    """One attempt (ATTEMPT: its number and how many there are) at tor's bootstrap: until tor's log (LOG_PATH, from
    OFFSET on: this attempt's tor) says "Bootstrapped 100%", reporting each new percentage, and every
    BOOTSTRAP_STILL seconds one that stays. tor that exits: RuntimeError. BootstrapStalled: no new percentage for
    STALL seconds (default: TIMEOUT); past TIMEOUT an attempt that still rises (never STALL seconds without a new
    percentage) goes on, up to twice TIMEOUT. Messages carry how far it got and the log's last lines, the bridges (SECRETS) hidden."""
    stall = timeout if stall is None else stall
    begun = clock()
    shown, rose, said = None, begun, begun
    number, attempts = attempt
    of = f", attempt {number} of {attempts}" if attempts > 1 else ""

    def tail(text):
        return "\n".join(redact(line, secrets) for line in text.splitlines()[-15:])

    def got(percent, summary):
        return f"{percent}%" + (f" ({redact(summary, secrets)})" if summary else "")
    while True:
        text = read_from(log_path, offset)
        percent, summary = bootstrap_progress(text)
        now_ = clock()
        if percent != shown:
            emit("progress", out=out, message=f"tor: bootstrapped {got(percent, summary)}{of}")
            if shown is not None:
                rose = now_
            shown, said = percent, now_
        if percent >= 100:
            return percent
        if process.poll() is not None:
            raise RuntimeError(f"tor exited ({process.returncode}) at {percent}% of its bootstrap:\n{tail(text)}")
        still, took = now_ - rose, now_ - begun
        if still >= stall:
            raise BootstrapStalled(f"stalled at {got(percent, summary)} for {int(still)} s", percent, summary)
        if took >= 2 * timeout:
            raise BootstrapStalled(f"was not done within {int(took)} s, twice its {timeout} s although it still rose "
                                   f"(at {got(percent, summary)})", percent, summary)
        if now_ - said >= BOOTSTRAP_STILL:
            emit("progress", out=out, message=f"tor: still at {got(percent, summary)} for {int(still)} s{of}")
            said = now_
        sleep(0.5)


def stop_tor(process, runner):
    """End the tor of an attempt that stalled (TERM, KILL after 10 s); the runner no longer reports it as down."""
    with contextlib.suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
    with contextlib.suppress(ValueError):
        runner.children.remove(process)


def fresh_guard(uid, path=None):
    """Tor's state file (its guards) gone before tor starts again: the next attempt picks a new guard; the cached
    directory documents stay. The data directory is debian-tor's (0700) and root here has no CAP_DAC_OVERRIDE: it
    becomes root's for the moment (CHOWN), then debian-tor's again. A failure leaves the guard as it was."""
    path = Path(path or TOR_DATA)
    try:
        os.chown(path, 0, 0, follow_symlinks=False)
        with contextlib.suppress(FileNotFoundError):
            (path / "state").unlink()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            own_data_directory(uid, path)


def bootstrap_hints(bridges):
    return ("What helps: " + ("other bridges" if bridges else "bridges, if this network blocks Tor")
            + " (sandbox-ai vpn add NAME --tor --bridges FILE --force); another network; this host's clock (tor "
            "refuses the directory when the clock is hours off: timedatectl); more time or attempts (sandbox-ai vpn "
            "set NAME --bootstrap-timeout SECONDS --bootstrap-attempts N)")


def bootstrap(start, log_path, runner, settings, out=None, secrets=(), bridges=(), uid=TOR_UID, clock=time.monotonic,
              sleep=time.sleep):
    """Tor's bootstrap with its retries (SETTINGS, bootstrap_settings): START() starts tor (its process); an attempt
    that stalls ends that tor and starts it again with a fresh guard - said, with the attempt's number - up to
    SETTINGS["attempts"] attempts. Between two attempts the kill switch (loaded before the first, never touched
    here) is looked at: gone, nothing starts again. Returns tor's process at 100%; RuntimeError with what was
    tried, the last bootstrap line and what helps."""
    attempts, tried, last, tail = settings["attempts"], [], None, ""
    for number in range(1, attempts + 1):
        try:
            offset = Path(log_path).stat().st_size
        except OSError:
            offset = 0
        process = start()
        try:
            wait_bootstrap(log_path, process, settings["timeout"], out, secrets, clock=clock, sleep=sleep,
                           stall=settings["stall"], attempt=(number, attempts), offset=offset)
            return process
        except BootstrapStalled as exc:
            tried.append(f"attempt {number} {exc}")
            lines = read_from(log_path, offset).splitlines()
            last = next((redact(line, secrets).strip() for line in reversed(lines) if BOOTSTRAPPED.search(line)), last)
            tail = "\n".join(redact(line, secrets) for line in lines[-10:])
            stop_tor(process, runner)
            if number == attempts:
                break
            listing = runner.run(["nft", "list", "table", "inet", TABLE], check=False)
            problem = killswitch_problem(listing.stdout if listing.returncode == 0 else "")
            if problem:
                raise RuntimeError(f"Tor's bootstrap {exc}, and {problem}: tor is not started again") from None
            fresh_guard(uid)
            emit("progress", out=out, message=f"Tor bootstrap {exc}; trying again ({number + 1} of {attempts})")
    each = f"each {settings['timeout']} s, up to {2 * settings['timeout']} s while it still rose; stalled: no new " \
           f"percentage for {settings['stall']} s"
    raise RuntimeError(f"Tor did not finish its bootstrap in {attempts} attempt{'s' if attempts != 1 else ''} "
                       f"({each}): " + "; ".join(tried) + f". Its last bootstrap line: {last or 'none'}. "
                       + bootstrap_hints(bridges) + (f"\ntor's last lines:\n{tail}" if tail else ""))


def tor_uid(name=TOR_USER):
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        raise RuntimeError(f"the Tor image has no user {name}: rebuild it with sandbox-ai build tor") from None


TOR_DATA = Path("/var/lib/tor")


def own_data_directory(uid, path=None):
    """Tor's data directory (the container's tmpfs, root's at first) becomes debian-tor's alone (0700), as tor wants
    it once it has become that user; a link or anything but a directory there is refused."""
    path = Path(path or TOR_DATA)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"{path} is no directory in the Tor container")
    os.chmod(path, 0o700)  # first: once it is debian-tor's, root (no CAP_FOWNER here) may not change its mode
    os.chown(path, uid, uid, follow_symlinks=False)


def up_tor(payload, runner, out=None, uid_of=None, clock=time.monotonic, sleep=time.sleep):
    """tor behind its kill switch: the rules first, then the torrc (private, with the bridges), then tor, which
    binds its ports as root and becomes debian-tor; ready once it has bootstrapped (bootstrap: an attempt that
    stalls is tried again, with the kill switch in place all the while)."""
    tor = payload["profile"]["tor"]
    settings = bootstrap_settings(tor)
    bridges = parse_bridges((payload.get("secrets") or {}).get("tor_bridges") or "")
    if tor.get("bridges") and not bridges:
        raise ValueError("the profile has bridges, but none arrived")
    uid = (uid_of or tor_uid)()
    if uid != TOR_UID:
        raise RuntimeError(f"{TOR_USER} has the uid {uid} in the Tor image, not {TOR_UID}: rebuild it with "
                           "sandbox-ai build tor")
    load_killswitch(runner, tor_rules(uid, bridges, payload.get("bypass") or ()))
    own_data_directory(uid)
    RUN.mkdir(mode=0o700, exist_ok=True)
    config, defaults = RUN / "torrc", RUN / "torrc-defaults"
    write_private(config, tor_config(tor, bridges))
    write_private(defaults, "")
    log_path = RUN / "tor.log"

    def start():
        with open(log_path, "ab") as log:
            return runner.spawn(["tor", "-f", str(config), "--defaults-torrc", str(defaults)], log)
    bootstrap(start, log_path, runner, settings, out, secret_values(payload), bridges, uid, clock, sleep)
    count = len(bridges)
    return {"interface": f"TransPort 127.0.0.1:{TOR_TRANS_PORT}", "implementation": "tor", "addresses": [],
            "ipv4": True, "ipv6": False, "full4": True, "full6": False,
            "endpoints": [f"{count} bridge{'s' if count != 1 else ''}"] if count else ["the Tor network"],
            "dns": [TOR_DNS],
            "tor": {"uid": uid, "bridges": count, "exit_country": exit_countries(tor.get("exit_country")),
                    "socks": f"127.0.0.1:{TOR_SOCKS_PORT}" if tor.get("socks") else None,
                    "trans_port": f"127.0.0.1:{TOR_TRANS_PORT}"}}


def probe_ipv6(timeout, connect=None):
    """Behind Tor there is no IPv6 resolver to ask an echo service with: a connection to a literal IPv6 address
    must fail locally. {"error", "kind": "blocked"|"failed"}, or {"ip", ...} when it connected (a leak)."""
    try:
        sock = (connect or check_connection)((IPV6_PROBE, 443), timeout)
    except OSError as exc:
        blocked = exc.errno in BLOCKED_ERRNOS or isinstance(exc, (socket.timeout, TimeoutError))
        return {"error": exc.strerror or str(exc), "kind": "blocked" if blocked else "failed"}
    sock.close()
    return {"ip": IPV6_PROBE, "vpn": None, "url": f"tcp://[{IPV6_PROBE}]:443"}


def secret_values(payload):
    """The values a message must never show: the secrets, and each bridge's address, fingerprint and certificate
    (tor's log can name them), longest first."""
    found = [v for v in (payload.get("secrets") or {}).values() if isinstance(v, str) and v]
    with contextlib.suppress(ValueError, AttributeError):
        for bridge in parse_bridges((payload.get("secrets") or {}).get("tor_bridges") or ""):
            found += [f"{bridge['address']}:{bridge['port']}", bridge["address"],
                      *([bridge["fingerprint"]] if bridge["fingerprint"] else []),
                      *[arg.split("=", 1)[1] for arg in bridge["args"] if arg.startswith("cert=")]]
    return sorted(set(found), key=len, reverse=True)


# --- running it ------------------------------------------------------------------------------------------------

class Runner:
    """The commands of the setup; tests replace it with a recorder."""

    def __init__(self):
        self.children = []

    def run(self, command, data=None, check=True):
        result = subprocess.run(command, input=data, capture_output=True, text=True, timeout=60)
        if check and result.returncode != 0:
            raise RuntimeError(f"{command[0]} {command[1] if len(command) > 1 else ''} failed: "
                               f"{redact(result.stderr.strip())[-400:]}")
        return result

    def spawn(self, command, log):
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.children.append(process)
        return process

    def stream(self, command):
        """COMMAND with its output to read as it comes (tailscale up prints a login URL and waits)."""
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)


def load_killswitch(runner, rules):
    runner.run(["nft", "-f", "-"], data=rules)
    runner.run(["nft", "list", "table", "inet", TABLE])


def killswitch_problem(listing):
    """None when LISTING (`nft list table inet TABLE`) shows the kill switch in place - its output, input and forward
    chains each drop what no rule accepts - else what is missing. What MCP servers are checked against before each
    one starts in this namespace (vpn.mcp_guard, vpn.check_pod_vpn): a container whose tunnel is not up yet, or
    whose table is gone, lets none start."""
    if not isinstance(listing, str) or f"table inet {TABLE}" not in listing:
        return f"the kill switch (nft table inet {TABLE}) is not loaded"
    for hook in ("output", "input", "forward"):
        if not re.search(rf"type filter hook {hook} priority [^;\n]*; policy drop;", listing):
            return f"the kill switch's {hook} chain does not drop what it does not accept"
    return None


def killswitch_command(out, runner=None):
    """`vpn_runtime.py killswitch`, run with podman exec in a running VPN container: {"ok": true} when the kill
    switch is in place (killswitch_problem), else {"ok": false, "problem": ...}; exit status 0 or 1."""
    try:
        listing = (runner or Runner()).run(["nft", "list", "table", "inet", TABLE], check=False)
        problem = killswitch_problem(listing.stdout if listing.returncode == 0 else "")
    except (OSError, subprocess.SubprocessError) as exc:
        problem = f"nft did not run: {exc}"
    out.write(json.dumps({"ok": problem is None, **({"problem": problem} if problem else {})}) + "\n")
    return 0 if problem is None else 1


MCP_DNS_TABLE = "sandbox_ai_mcp_dns"


def mcp_dns_resolver(tunnel):
    """The resolver an MCP server's lookups go to (mcp_dns_rules): the tunnel's first IPv4 resolver, else the first
    of FALLBACK_DNS (as gather_checks asks them), through the tunnel."""
    for server in list(tunnel.get("dns") or []) + list(FALLBACK_DNS):
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(server).version == 4:
                return str(ipaddress.ip_address(server))
    return FALLBACK_DNS[0]


def mcp_dns_source(tunnel):
    """The tunnel's own IPv4 address - the first of its addresses ("10.66.0.2/32" as a WireGuard profile writes it,
    or bare as OpenVPN and Tailscale report theirs) - or None: the source address of an MCP server's redirected
    lookups (mcp_dns_rules)."""
    for address in tunnel.get("addresses") or []:
        if not isinstance(address, str):  # (ipaddress takes a number for an address too)
            continue
        with contextlib.suppress(ValueError):
            found = ipaddress.ip_interface(address).ip
            if found.version == 4:
                return str(found)
    return None


MCP_DNS_TUNNEL_CHAIN = "mcp_dns_tunnel_only"


def mcp_dns_rules(resolver, interface, mark=CHECK_MARK, source=None):
    """For the VPN container of an MCP image server (payload "mcp"): every lookup on port 53, whatever resolver the
    server's /etc/resolv.conf names (the one Podman gave the VPN container: pasta's forwarder or the host's own),
    goes to the VPN's RESOLVER, through the tunnel (INTERFACE); one over IPv6 is refused. The runtime's own checks
    (MARK) keep the resolvers they name. Without it a lookup never left past the tunnel either (the kill switch), but
    went to a resolver of your own network through the tunnel, or nowhere.

    The kill switch's output chain judges a packet by the interface of the route chosen before the redirect (nft's
    oifname stays that of the first route through all of the output hook, while the redirect routes it anew): a
    lookup sent to a resolver on the uplink's own network (pasta's, the host's - CI's killswitch job: "dns lan
    resolver" failed with EPERM) was refused although it would have gone through the tunnel. So the kill switch's
    table gets two more rules: its output chain lets a redirected lookup (conntrack: destination NAT) to RESOLVER:53
    pass, and a postrouting chain of its own - which sees the interface the packet really leaves by - drops such a
    lookup that would leave by any other interface than INTERFACE (the tunnel gone, its route back on the uplink).
    Both in the kill switch's table: they never outlive it, nor it them.

    SOURCE (the tunnel's own IPv4 address, mcp_dns_source): the address a redirected lookup leaves the tunnel with.
    The kernel chooses a packet's source address with its first route too, and the redirect changes the destination
    only: a lookup sent to a resolver on the uplink's own network went into the tunnel with the UPLINK's address,
    which the VPN's server drops - WireGuard as not one of the peer's allowed addresses, any provider as not the
    address it gave (CI's killswitch job, once the lookup passed the kill switch: "no answer from 192.168.50.1"; on a
    host whose resolver is on its own network, as pasta copies it into the container, no MCP server behind a VPN
    resolved a name). A postrouting NAT chain gives each redirected lookup that leaves by INTERFACE the address
    SOURCE (snat: of the redirect's own kernel module, and the address is known - no masquerade to load); its answer
    comes back through the same connection. Without SOURCE (a tunnel without an IPv4 address) there is no such
    chain."""
    resolver = ipaddress.IPv4Address(resolver)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", str(interface or "")):
        raise ValueError(f"not an interface name: {interface!r}")
    redirected = f"ct status dnat ip daddr {resolver}"
    source_chain = []
    if source is not None:
        source = ipaddress.IPv4Address(source)
        source_chain = [" chain dns_source { type nat hook postrouting priority 100; policy accept;",
                        f'  {redirected} udp dport 53 oifname "{interface}" snat ip to {source}',
                        f'  {redirected} tcp dport 53 oifname "{interface}" snat ip to {source}', " }"]
    return "\n".join([
        f"table inet {MCP_DNS_TABLE} {{",
        # (no nft keyword as a chain's name: "redirect" made nft -f fail, and with it every such VPN container)
        " chain dns_redirect { type nat hook output priority -90; policy accept;",
        f"  meta mark {int(mark):#x} return",
        f"  meta nfproto ipv4 udp dport 53 dnat ip to {resolver}:53",
        f"  meta nfproto ipv4 tcp dport 53 dnat ip to {resolver}:53", " }",
        *source_chain,
        " chain dns_refuse { type filter hook output priority -6; policy accept;",
        f"  meta mark {int(mark):#x} return",
        "  meta nfproto ipv6 udp dport 53 reject with icmpx type admin-prohibited",
        "  meta nfproto ipv6 tcp dport 53 reject with icmpx type admin-prohibited", " }",
        "}",
        f"add chain inet {TABLE} {MCP_DNS_TUNNEL_CHAIN} {{ type filter hook postrouting priority -5; policy accept; }}",
        f'add rule inet {TABLE} {MCP_DNS_TUNNEL_CHAIN} {redirected} udp dport 53 oifname != "{interface}" drop',
        f'add rule inet {TABLE} {MCP_DNS_TUNNEL_CHAIN} {redirected} tcp dport 53 oifname != "{interface}" drop',
        f"insert rule inet {TABLE} output {redirected} udp dport 53 accept",
        f"insert rule inet {TABLE} output {redirected} tcp dport 53 accept", ""])


# --- the start's tunnel, lent to its own MCP servers ------------------------------------------------------------
#
# A WireGuard key (Mullvad's too) or a persistent Tailscale node works in one sandbox at a time: the same key twice
# gets the same tunnel address, and the server sends to whichever tunnel spoke last. So a networked MCP server behind
# its start's such --vpn brings up no tunnel of its own: the start's VPN container LENDS its tunnel, and the server's
# VPN container BORROWS it.
#   lender    the start's VPN container, in the agent's pod behind its kill switch: a Unix socket (LEND_SOCKET) in a
#             folder only it and its servers' VPN containers mount - never the agent's container nor a server's own.
#             Each connection asks for one thing: one TCP connection to a public IPv4 address ("tcp"), one DNS
#             question to the tunnel's resolver ("dns"), or what the lender is ("ping"). Its sockets carry LEND_MARK:
#             the agent's connection proxy never sees them and its firewall lets them by (they are the server's, not
#             the agent's) - after its cut chain, so `sandbox-ai net cut` holds them as it holds the agent's own
#             packets - and the kill switch lets them out through the tunnel only. Nothing of the start's own
#             namespace (loopback, the pod's addresses) nor of the networks beside the tunnel is reached.
#   borrower  the server's VPN container, its own network namespace: a kill switch through which nothing leaves at
#             all but over loopback (borrow_rules: as behind Tor, every TCP connection over IPv4 is redirected to
#             BORROW_PORT and every lookup on port 53 to BORROW_DNS; other UDP and IPv6 are dropped), and the relay
#             (Borrower) that hands each connection and each lookup to the lender.
# The start's end takes its VPN container, and the lender, with it: a borrower reaches nothing from then on, says so
# ("down") and ends - its kill switch stays in its namespace (fail closed); the launcher stops the servers too.
#
# Who may ask the lender is decided by the mount (every container of this user's Podman is the same uid) - so the
# folder is never one a sandbox sees (vpn.check_paths, the launcher's check_given_path) and never a link
# (vpn.lend_folder) - and by the run's token: 256 random bits the start's launcher makes for each run (vpn.py
# prepare_lending), handed to the lender and to each borrower of that run on standard input with the profile's secrets
# (never in the environment, a command line or a log; on the host a file of the user's alone beside the mounted
# folder, not in it). Every request carries it and the lender compares it in constant time (token_matches): one
# without it, or with another run's (a borrower a killed start left behind, a volume of the run's name used again,
# any other container that got the mount), is denied (LenderDenied) - that borrower's lender is gone for it, and it
# ends. The servers are not trusted either, so one of them must not
# take the tunnel from the others: a relay hands the lender BORROW_LIMIT connections and lookups at once at most (the
# lender serves LEND_LIMIT), a lender that is full is there, not gone (LenderRefused, lender_gone), a connection one
# side of which has ended is closed after LEND_HALF_OPEN seconds of silence (splice), and no accept loop ends on an
# error that passes.
#
# `sandbox-ai net cut` of the start holds the lent connections too (LEND_MARK passes the start's firewall after its
# cut chain): "all" and "remote" stop them, "api" all but those to the start's own model API, "local" none (they reach
# the internet only; a lookup stops where the tunnel's resolver has a private address, as the agent's own does). A
# packet of a lent TCP connection that the cut forbids is answered with a reset (runtime.cut_rules), so a connection
# that was open before ends at once at both relays, and one that says nothing ends at its next keepalive probe
# (LEND_KEEPALIVE).
LEND_DIR = Path("/run/sandbox-ai-lend")
LEND_SOCKET = LEND_DIR / "lend.sock"
BORROW_PORT = 9041
BORROW_DNS = "127.0.54.1"
LEND_LIMIT = 512  # connections a lender serves at once
# ... and a borrower's relay hands it at once (its TCP connections and its lookups together): one server alone never
# holds all the lender's connections against the run's other servers.
BORROW_LIMIT = 256
LEND_LINE = 8192  # bytes of a request's line (a DNS question of 4096 bytes in base64, and what names it)
LEND_ANSWER = 128 << 10  # bytes of an answer's line (a DNS answer of 65535 bytes in base64)
ACCEPT_PAUSE = 0.2  # seconds before accept() is tried again after an error that passes (no descriptor left)
# Seconds a spliced connection stays open, silent, once one of its directions has ended (the server's program closed
# it, or the far end did): then it is closed, and its slot is free again.
LEND_HALF_OPEN = 120
LEND_CONNECT_TIMEOUT = 20
LEND_PING = 5  # seconds between a borrower's looks at its lender
LEND_MISSES = 3  # looks in a row that find no lender: it is gone
LEND_READY = 15  # seconds a borrower waits for its lender at its start
# A lent TCP connection probes its far end after IDLE seconds of silence, every INTERVAL seconds, COUNT times: each
# probe is a packet the start's kill switch (sandbox-ai net cut) judges, so a cut connection that says nothing ends
# too (and one whose far end is gone without a word is not kept for good).
LEND_KEEPALIVE = (15, 5, 3)
LEND_TOKEN = re.compile(r"[0-9a-f]{64}")  # the run's token: 256 random bits in hex (vpn.py new_lend_token)
LEND_DENIED = "this is not the token of the run that lends this tunnel"
SO_ORIGINAL_DST = 80  # (linux/netfilter_ipv4.h)


_lend_token = None  # the run's token once use_lend_token() ran (a borrowing container): each request carries it


def lend_token(value):
    """VALUE as a run's token (LEND_TOKEN), or ValueError - which never shows it."""
    if not isinstance(value, str) or not LEND_TOKEN.fullmatch(value):
        raise ValueError("no token of the run that lends its tunnel (the launcher hands it over with the secrets)")
    return value


def use_lend_token(token):
    """Every request to the lender carries TOKEN from now on (the borrowing container: its launcher handed it over
    on standard input)."""
    global _lend_token
    _lend_token = lend_token(token)


def token_matches(given, token):
    """Whether GIVEN (a request's "token": anything a borrower sent) is TOKEN, compared in constant time."""
    if not isinstance(given, str) or len(given) > 256:
        return False
    return hmac.compare_digest(given.encode("utf-8", "replace"), token.encode())


def read_line(sock, limit=LEND_LINE):
    """One line (without its newline) from SOCK, read a byte at a time: what follows it is the connection's own
    data. ValueError for a line longer than LIMIT or one that ends early."""
    data = bytearray()
    while True:
        byte = sock.recv(1)
        if not byte:
            raise ValueError("the line ended early")
        if byte == b"\n":
            return bytes(data)
        data += byte
        if len(data) > limit:
            raise ValueError("the line is too long")


def send_line(sock, document):
    sock.sendall(json.dumps(document, separators=(",", ":")).encode() + b"\n")


def splice(one, other, half_open=None):
    """Copy both ways between the connected sockets ONE and OTHER until both directions have ended (the end of one
    direction is passed on as a half-close; an error ends both); both are closed then. Once one direction has ended,
    the other has HALF_OPEN seconds (LEND_HALF_OPEN) of silence at most: a far end that never closes - the server's
    program long gone - would else keep the connection, and its slot of the lender and of the relay, for good."""
    half_open = LEND_HALF_OPEN if half_open is None else half_open
    ended = []  # [when a direction ended, or data last went by after that]

    def copy(source, target):
        try:
            # poll, not select (a descriptor above 1023 is none to select) and not the socket's own timeout (the
            # other direction's thread sends on this socket: its timeout would be that one's too).
            waiting = select.poll()
            waiting.register(source, select.POLLIN)
            while True:
                if not waiting.poll(min(1000, max(10, half_open * 250))):
                    if ended and time.monotonic() - ended[0] >= half_open:
                        raise OSError("the other direction ended, and this one says nothing")
                    continue
                data = source.recv(65536)
                if not data:
                    ended[:] = [time.monotonic()]
                    with contextlib.suppress(OSError):
                        target.shutdown(socket.SHUT_WR)
                    return
                target.sendall(data)
                if ended:
                    ended[:] = [time.monotonic()]
        except (OSError, ValueError):
            for sock in (source, target):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
    back = threading.Thread(target=copy, args=(other, one), daemon=True)
    try:
        back.start()
    except RuntimeError:  # (no thread to be had now: nothing is copied one way only)
        back = None
    if back is not None:
        copy(one, other)
        back.join()
    for sock in (one, other):
        with contextlib.suppress(OSError):
            sock.close()


def is_local_address(address):
    """Whether ADDRESS is one of this network namespace's own (it can be bound to)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((str(address), 0))
        except OSError:
            return False
        return True


# Never lent, whatever ipaddress calls them (its tables change with Python's versions, and multicast is "global" to
# it): this network, the private ones, carrier-grade NAT (a tailnet's addresses), loopback, link-local (the clouds'
# metadata services), the protocol assignments, the documentation and benchmark networks, multicast, the reserved
# block and broadcast.
NOT_LENT = tuple(ipaddress.IPv4Network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4",
    "240.0.0.0/4"))


def lend_target(address, port, local=is_local_address):
    """(address, port) of a lent connection: a public IPv4 address (the tunnel is lent for the internet) that is not
    one of the start's own namespace, and a port; ValueError for anything else - loopback, the pod's addresses, the
    local and private networks (NOT_LENT), IPv6, a name, an address in any other spelling than its plain one."""
    try:
        if not isinstance(address, str):
            raise ValueError("no text")
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        raise ValueError(f"not an IPv4 address: {str(address)[:64]!r}") from None
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError(f"not a port: {str(port)[:16]!r}")
    if not ip.is_global or ip.is_multicast or any(ip in network for network in NOT_LENT):
        raise ValueError(f"{ip} is no public address: the start's tunnel is lent for the internet only")
    if local(str(ip)):
        raise ValueError(f"{ip} is an address of the start's own sandbox")
    return str(ip), port


def lent_socket(kind=socket.SOCK_STREAM, mark=LEND_MARK):
    """An IPv4 socket for a lent connection, marked with MARK (the agent's proxy never sees it, its firewall lets it
    by after its cut chain; the kill switch holds it to the tunnel). OSError when the mark does not hold: unmarked,
    the agent's rules would judge it as the agent's."""
    sock = socket.socket(socket.AF_INET, kind)
    option = getattr(socket, "SO_MARK", 36)
    try:
        sock.setsockopt(socket.SOL_SOCKET, option, mark)
        if sock.getsockopt(socket.SOL_SOCKET, option) != mark:
            raise OSError("the mark did not hold")
    except OSError as exc:
        sock.close()
        raise OSError(f"a lent connection cannot be marked ({exc}): it needs CAP_NET_ADMIN") from None
    return sock


def keep_lent_alive(sock, keepalive=LEND_KEEPALIVE):
    """The lent TCP connection SOCK probes its far end while it says nothing (KEEPALIVE: idle, interval, count): a
    connection the start's kill switch cut ends whether it speaks or not (each probe is a packet the cut answers with
    a reset). Where the kernel has no such option it stays as it is: the cut holds its packets all the same. (No
    limit on unanswered data, TCP_USER_TIMEOUT: that would also end a connection whose far end only reads slowly.)"""
    idle, interval, count = keepalive
    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (("TCP_KEEPIDLE", idle), ("TCP_KEEPINTVL", interval), ("TCP_KEEPCNT", count)):
        if hasattr(socket, name):
            options.append((socket.IPPROTO_TCP, getattr(socket, name), int(value)))
    for level, option, value in options:
        with contextlib.suppress(OSError):
            sock.setsockopt(level, option, value)
    return sock


def lent_connection(target, timeout=LEND_CONNECT_TIMEOUT):
    """A marked TCP connection to TARGET (lend_target's), through the tunnel."""
    sock = lent_socket()
    try:
        keep_lent_alive(sock)
        sock.settimeout(timeout)
        sock.connect(target)
        sock.settimeout(None)
    except BaseException:
        sock.close()
        raise
    return sock


def lent_lookup(query, resolver, timeout=5.0, tcp=False):
    """The answer of the tunnel's RESOLVER to the DNS QUERY (bytes), asked over UDP through the tunnel. TCP (the
    question came to the relay over TCP): an answer that came truncated is asked again over TCP - a stub without EDNS
    gets 512 bytes over UDP, sees "truncated" and asks again over TCP, and must not get the truncated answer once
    more. (A question that came over UDP gets the UDP answer as it is: its asker takes no more than that.)"""
    if not isinstance(query, bytes) or not 12 <= len(query) <= 4096:
        raise ValueError("not a DNS question")
    resolver = str(ipaddress.IPv4Address(resolver))
    deadline = time.monotonic() + timeout
    with lent_socket(socket.SOCK_DGRAM) as sock:
        sock.connect((resolver, 53))
        sock.send(query)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:  # (answers to other questions, one after another, are no answer)
                raise socket.timeout("the tunnel's resolver did not answer the question")
            sock.settimeout(left)
            answer = sock.recv(65535)
            if len(answer) >= 12 and answer[:2] == query[:2]:
                break
    if tcp and answer[2] & 0x02:  # TC
        with contextlib.suppress(OSError, ValueError, struct.error):
            with lent_socket() as sock:
                sock.settimeout(max(1.0, deadline - time.monotonic()))
                sock.connect((resolver, 53))
                sock.sendall(struct.pack("!H", len(query)) + query)
                whole = read_exact(sock, struct.unpack("!H", read_exact(sock, 2))[0])
            if len(whole) >= 12 and whole[:2] == query[:2]:
                return whole
    return answer


def accepting_ended(server):
    """Whether the listening socket SERVER is closed (its accept() fails for good), not only failing for a moment."""
    try:
        return server.fileno() < 0
    except (OSError, ValueError):
        return True


def refuse(conn, why):
    """The connection CONN told WHY it is not served (the lender's answer line), and closed."""
    with contextlib.suppress(OSError):
        conn.settimeout(2)
        send_line(conn, {"error": why})
    with contextlib.suppress(OSError):
        conn.close()


class LenderRefused(OSError):
    """The lender answered and refused the request (its reason): it is there - unlike one that does not answer."""


class LenderDenied(OSError):
    """The lender answered and denied the request: its token is not that of the lender's run. For this borrower the
    lender is gone (lender_gone) - it is another run's, or none of sandbox-ai's."""


class Lender:
    """The lender of a start's VPN container (see above): its socket at PATH; its TUNNEL and PROFILE (the name) said
    to whoever asks (ping) with the run's TOKEN - without which none is made, and no request is served. CONNECT,
    LOOKUP and LOCAL: how it connects, asks the resolver, knows its own addresses."""

    def __init__(self, tunnel, profile, path=None, connect=lent_connection, lookup=lent_lookup,
                 local=is_local_address, limit=LEND_LIMIT, token=None):
        self.token = lend_token(_lend_token if token is None else token)
        self.tunnel, self.profile, self.path = tunnel, str(profile), Path(path or LEND_SOCKET)
        self.connect, self.lookup, self.local = connect, lookup, local
        self.slots = threading.BoundedSemaphore(limit)
        self.resolver = mcp_dns_resolver(tunnel)
        self.server = None

    def facts(self):
        return {"ok": True, "profile": self.profile, "interface": self.tunnel.get("interface"),
                "resolver": self.resolver}

    def start(self):
        """Listen at PATH (its folder is the mount the launcher gave; a socket an earlier container left goes)."""
        if not self.path.parent.is_dir():
            raise RuntimeError(f"the folder of the lent tunnel's socket ({self.path.parent}) is not there")
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(64)
        except BaseException:
            server.close()
            raise
        self.server = server
        threading.Thread(target=self.serve, daemon=True).start()
        return self

    def serve(self):
        """Each connection in a thread of its own while a slot is left; one beyond them is told so (a lender that is
        full is there: LenderRefused). Only its socket's end (close) ends this: an accept() that fails for a moment
        (no descriptor left) or a thread that cannot be had would else take every server's network for good."""
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                if accepting_ended(self.server):
                    return
                time.sleep(ACCEPT_PAUSE)
                continue
            if not self.slots.acquire(blocking=False):
                refuse(conn, "too many connections through the start's tunnel at once")
                continue
            try:
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
            except RuntimeError:  # (no thread to be had now: handle, which gives the slot back, never ran)
                self.slots.release()
                refuse(conn, "the start's tunnel takes no further connection now")

    def handle(self, conn):
        spliced = False
        try:
            conn.settimeout(10)
            try:
                request = json.loads(read_line(conn))
                if not isinstance(request, dict):
                    raise ValueError("not a request")
                if not token_matches(request.get("token"), self.token):
                    # Every request, a look (ping) too: who has not this run's token learns nothing and gets nothing.
                    send_line(conn, {"error": LEND_DENIED, "denied": True})
                    return
                op = request.get("op")
                if op == "ping":
                    send_line(conn, self.facts())
                elif op == "dns":
                    query = base64.b64decode(str(request.get("query", "")), validate=True)
                    if not 12 <= len(query) <= 4096:
                        raise ValueError("not a DNS question")
                    answer = self.lookup(query, self.resolver, tcp=True) if request.get("tcp") is True else \
                        self.lookup(query, self.resolver)
                    send_line(conn, {"ok": True, "answer": base64.b64encode(answer).decode()})
                elif op == "tcp":
                    target = lend_target(request.get("address"), request.get("port"), self.local)
                    out = self.connect(target)
                    send_line(conn, {"ok": True})
                    conn.settimeout(None)
                    spliced = True
                    splice(conn, out)
                else:
                    raise ValueError(f"unknown request {str(op)[:32]!r}")
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                if not spliced:
                    with contextlib.suppress(OSError):
                        send_line(conn, {"error": str(exc)[:300]})
        finally:
            if not spliced:
                with contextlib.suppress(OSError):
                    conn.close()
            self.slots.release()

    def close(self):
        if self.server is not None:
            with contextlib.suppress(OSError):
                self.server.close()
            with contextlib.suppress(OSError):
                self.path.unlink()


def ask_lender(request, path=None, timeout=10, token=None):
    """REQUEST to the lender at PATH, with the run's TOKEN (use_lend_token's when none is given): (its answer, the
    socket - for "tcp" the connection through it). OSError when it is not there, LenderRefused (an OSError) when it
    is and refuses, with its reason, LenderDenied (an OSError) when the token is not its run's."""
    token = _lend_token if token is None else token
    if token is not None:
        request = {**request, "token": token}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(path or LEND_SOCKET))
        # A lender that is full says so and closes at once, without reading: the request may meet a closed socket
        # (EPIPE) - its line is there to be read all the same, and says that it is there.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            send_line(sock, request)
        answer = json.loads(read_line(sock, LEND_ANSWER))
    except (OSError, ValueError, RecursionError) as exc:
        sock.close()
        raise OSError(f"the start's lent tunnel did not answer ({exc})") from None
    if not isinstance(answer, dict) or not answer.get("ok"):
        sock.close()
        why = answer.get("error") if isinstance(answer, dict) else answer
        if isinstance(answer, dict) and answer.get("denied") is True:
            raise LenderDenied(f"the start's lent tunnel denied it: {str(why)[:300]}")
        raise LenderRefused(f"the start's lent tunnel refused it: {str(why)[:300]}")
    return answer, sock


def lender_gone(profile_name, path=None, ask=None):
    """Why the lender at PATH is no longer the one of a borrower of PROFILE_NAME's tunnel, or None while it is: it
    does not answer (the start ended), it denies this borrower's token (another run lends there now: the folder -
    macOS: the volume - of a run's name, used again by its next start; LenderDenied is an OSError that is no
    LenderRefused), or it lends another profile's tunnel. A lender that answers and refuses - every connection in
    use - is there: one busy server must not end the VPN containers of all the others."""
    try:
        facts, sock = (ask or ask_lender)({"op": "ping"}, path, timeout=5)
        sock.close()
    except LenderRefused:
        return None
    except OSError as exc:
        return str(exc)
    if facts.get("profile") != profile_name:
        return f"it lends the tunnel of {str(facts.get('profile'))[:40]!r} now"
    return None


def borrow_rules(port=BORROW_PORT, dns=BORROW_DNS):
    """The kill switch of a borrowing VPN container: nothing leaves this namespace but over loopback (it has no
    tunnel: the lender is reached through a Unix socket). Every TCP connection over IPv4 is redirected to PORT and
    every lookup on port 53 (UDP and TCP) to DNS - the relay's - before the filter sees them; other UDP and all IPv6
    are dropped. The redirect runs after an agent's own to its connection proxy (priority -100): in a command server's
    pod what that one redirected reaches the relay through the proxy."""
    dns = ipaddress.IPv4Address(dns)
    return "\n".join([
        f"table inet {TABLE} {{",
        " chain output { type filter hook output priority -5; policy drop;",
        '  oifname "lo" accept',
        "  ct status dnat ip daddr 127.0.0.0/8 accept", "  ct status dnat ip6 daddr ::1 accept",
        "  reject with icmpx type admin-prohibited", " }",
        " chain input { type filter hook input priority -5; policy drop;",
        '  iifname "lo" accept', "  ct state established,related accept", " }",
        " chain forward { type filter hook forward priority -5; policy drop; }",
        " chain divert { type nat hook output priority -90; policy accept;",
        "  ip daddr 127.0.0.0/8 return", "  meta nfproto ipv6 return",
        f"  udp dport 53 dnat ip to {dns}:53", f"  tcp dport 53 dnat ip to {dns}:53",
        f"  meta l4proto tcp redirect to :{int(port)}", " }",
        "}", ""])


def original_destination(sock):
    """(address, port) a connection redirected here was meant for (SO_ORIGINAL_DST)."""
    raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    port, packed = struct.unpack_from("!2xH4s", raw)
    return socket.inet_ntoa(packed), port


class Borrower:
    """The relay of a borrowing VPN container: TCP on 127.0.0.1:PORT (each redirected connection to the lender, for
    its original destination) and DNS on DNS:53 (UDP and TCP: each question to the lender)."""

    def __init__(self, path=None, port=BORROW_PORT, dns=BORROW_DNS, destination=original_destination,
                 ask=ask_lender, limit=BORROW_LIMIT):
        self.path, self.port, self.dns = path, int(port), str(dns)
        self.destination, self.ask = destination, ask
        self.sockets = []
        # What it hands the lender at once, connections and lookups together (BORROW_LIMIT): a server's flood ends
        # here - closed or dropped - not as a thread each, nor as all the lender's connections.
        self.slots = threading.BoundedSemaphore(limit)

    def hand(self, work, *args):
        """WORK(*ARGS) in a thread of its own while a slot is left: whether it was begun."""
        if not self.slots.acquire(blocking=False):
            return False

        def run():
            try:
                work(*args)
            finally:
                self.slots.release()
        try:
            threading.Thread(target=run, daemon=True).start()
        except RuntimeError:  # (no thread to be had now)
            self.slots.release()
            return False
        return True

    def accepted(self, server, work):
        """Each connection of the listening SERVER to WORK (hand); one beyond the slots is closed at once. Ends with
        the socket (close), not on an accept() that fails for a moment."""
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                if accepting_ended(server):
                    return
                time.sleep(ACCEPT_PAUSE)
                continue
            if not self.hand(work, conn):
                with contextlib.suppress(OSError):
                    conn.close()

    def start(self):
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dns_tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sockets = [tcp, udp, dns_tcp]
        try:
            for sock in (tcp, dns_tcp):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp.bind(("127.0.0.1", self.port))
            tcp.listen(128)
            udp.bind((self.dns, 53))
            dns_tcp.bind((self.dns, 53))
            dns_tcp.listen(32)
        except BaseException:
            self.close()
            raise
        for target, sock in ((self.accept_tcp, tcp), (self.serve_udp, udp), (self.accept_dns, dns_tcp)):
            threading.Thread(target=target, args=(sock,), daemon=True).start()
        return self

    def close(self):
        for sock in self.sockets:
            with contextlib.suppress(OSError):
                sock.close()

    def accept_tcp(self, server):
        self.accepted(server, self.relay)

    def relay(self, conn):
        try:
            address, port = self.destination(conn)
            _answer, lent = self.ask({"op": "tcp", "address": address, "port": port}, self.path,
                                     timeout=LEND_CONNECT_TIMEOUT + 5)
        except (OSError, ValueError, struct.error):
            with contextlib.suppress(OSError):
                conn.close()
            return
        lent.settimeout(None)
        splice(conn, lent)

    def lookup(self, query, tcp=False):
        """The lender's answer to the DNS QUERY; TCP: it came over TCP (a truncated answer is asked again so)."""
        answer, sock = self.ask({"op": "dns", "query": base64.b64encode(query).decode(), **({"tcp": True} if tcp
                                                                                             else {})}, self.path)
        sock.close()
        return base64.b64decode(str(answer.get("answer", "")), validate=True)

    def serve_udp(self, sock):
        while True:
            try:
                query, peer = sock.recvfrom(4096)
            except OSError:
                if accepting_ended(sock):
                    return
                time.sleep(ACCEPT_PAUSE)
                continue

            def answer(query=query, peer=peer):
                with contextlib.suppress(OSError, ValueError):
                    sock.sendto(self.lookup(query), peer)
            self.hand(answer)  # (beyond the slots: dropped, as a resolver under load drops; the client asks again)

    def accept_dns(self, server):
        self.accepted(server, self.serve_dns_tcp)

    def serve_dns_tcp(self, conn):
        try:
            conn.settimeout(30)
            while True:
                head = conn.recv(2, socket.MSG_WAITALL)
                if len(head) < 2:
                    return
                query = conn.recv(struct.unpack("!H", head)[0], socket.MSG_WAITALL)
                answer = self.lookup(query, tcp=True)
                conn.sendall(struct.pack("!H", len(answer)) + answer)
        except (OSError, ValueError):
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def lender_facts(profile_name, path=None, ask=None, wait=LEND_READY, clock=time.monotonic,
                 sleep=time.sleep):
    """What the lender at PATH says of itself (ping), waited for up to WAIT seconds; ValueError when there is none,
    when it denies this borrower's token (at once: waiting changes nothing), or when it lends the tunnel of another
    profile than PROFILE_NAME."""
    deadline = clock() + wait
    while True:
        try:
            facts, sock = (ask or ask_lender)({"op": "ping"}, path, timeout=5)
            sock.close()
            break
        except LenderDenied as exc:
            raise ValueError(f"the start's tunnel is not lent to this server: {exc}") from None
        except OSError as exc:
            if clock() >= deadline:
                raise ValueError(f"the start's tunnel is not lent here: {exc} (a start lends it while it runs; the "
                                 "server comes up with that start)") from None
            sleep(0.5)
    if facts.get("profile") != profile_name:
        raise ValueError(f"the start lends the tunnel of {str(facts.get('profile'))[:40]!r}, not of {profile_name}")
    return facts


def up_borrowed(payload, runner, borrower=None, facts=None):
    """A borrowing VPN container: the kill switch first (nothing leaves but over loopback), then the relay, then the
    lender asked what it lends. The "tunnel" the checks see: loopback, the relay's resolver, IPv4 only."""
    load_killswitch(runner, borrow_rules())
    (borrower or Borrower()).start()
    lent = (facts or lender_facts)(payload["profile"]["name"])
    run = str((payload.get("borrow") or {}).get("run") or "")[:80]
    return {"interface": "lo", "implementation": "the start's tunnel, lent", "addresses": [],
            "ipv4": True, "ipv6": False, "full4": True, "full6": False,
            "endpoints": [f"the tunnel of the start {run}" if run else "the start's tunnel"], "dns": [BORROW_DNS],
            "lent": {"run": run or None, "interface": lent.get("interface"), "resolver": lent.get("resolver")}}


# What became of the optional wireguard-go when the image was built (Containerfile.vpn writes it).
WIREGUARD_GO_STATUS = Path("/usr/local/share/sandbox-ai/wireguard-go")


def wireguard_go():
    """wireguard-go's path in this image, or None: it is left out when it could not be built."""
    return shutil.which("wireguard-go")


def no_wireguard_go_message():
    try:
        status = WIREGUARD_GO_STATUS.read_text().strip()[:300]
    except OSError:
        status = ""
    return ("the host has no WireGuard kernel module loaded (the kernel's WireGuard could not create the interface) "
            "and this VPN image has no wireguard-go, the fallback for that" + (f" ({status})" if status else "") +
            ": load the module on the host (sudo modprobe wireguard; sandbox-ai doctor shows it), or rebuild the "
            "image with network for Go's module proxy: sandbox-ai build vpn")


def up_wireguard(payload, runner, resolve=system_resolver):
    wireguard = payload["profile"]["wireguard"]
    endpoints = {}
    for index, peer in enumerate(wireguard["peers"]):
        if peer.get("endpoint"):
            endpoints[str(index)] = resolve_endpoint(peer["endpoint"]["host"], peer["endpoint"]["port"], resolve)
    tunnel = wireguard_tunnel(wireguard, endpoints)
    interface = tunnel["interface"]
    load_killswitch(runner, killswitch_rules(interface, [(a, "udp", p) for a, p in endpoints.values()],
                                             tunnel_ipv6=tunnel["ipv6"]))
    defaults = parse_default_routes(runner.run(["ip", "-4", "route", "show", "default"]).stdout,
                                    runner.run(["ip", "-6", "route", "show", "default"], check=False).stdout)
    # The kernel's WireGuard where the module is there (it is namespace-aware); wireguard-go through /dev/net/tun
    # otherwise.
    if runner.run(["ip", "link", "add", "dev", interface, "type", "wireguard"], check=False).returncode == 0:
        tunnel["implementation"] = "kernel"
    else:
        if not wireguard_go():
            raise RuntimeError(no_wireguard_go_message())
        RUN.mkdir(mode=0o700, exist_ok=True)
        log = open(RUN / "wireguard-go.log", "ab")
        runner.spawn(["wireguard-go", "--foreground", interface], log)
        deadline = time.monotonic() + 15
        while runner.run(["ip", "link", "show", "dev", interface], check=False).returncode != 0:
            if time.monotonic() > deadline:
                raise RuntimeError("neither the kernel's WireGuard nor wireguard-go could create the interface "
                                   "(is /dev/net/tun there?)")
            time.sleep(0.2)
        tunnel["implementation"] = "wireguard-go"
    secrets_ = payload.get("secrets", {})
    runner.run(["wg", "setconf", interface, "/dev/stdin"],
               data=wg_setconf(wireguard, secrets_["private_key"], secrets_.get("preshared_keys", {}), endpoints))
    for command in route_commands(interface, wireguard["addresses"],
                                  [n for peer in wireguard["peers"] for n in peer["allowed_ips"]],
                                  [(a, "udp", p) for a, p in endpoints.values()], defaults,
                                  wireguard.get("mtu") or DEFAULT_MTU):
        runner.run(command)
    tunnel["dns"] = list(payload["profile"].get("dns") or [])
    return tunnel


def openvpn_command(config_path, auth_path, full):
    """openvpn with the sanitized configuration and everything that must not come from it: the device, the mark,
    our own route-up hook (the only script it may run) and, for a full tunnel, the default routes."""
    command = ["openvpn", "--config", str(config_path), "--dev", INTERFACES["openvpn"], "--dev-type", "tun",
               "--mark", str(MARK), "--script-security", "2",
               "--route-up", "/usr/bin/python3 -I /vpn/vpn_runtime.py openvpn-route-up",
               # Reconnects keep the tun device (and so the kill switch's way out); they retry for as long as the
               # sandbox runs, and a start that does not connect in time ends with the container.
               "--persist-tun"]
    if auth_path:
        command += ["--auth-user-pass", str(auth_path)]
    if full:
        command += ["--redirect-gateway", "def1", "ipv6"]
    return command


def openvpn_remote_lines(remotes):
    return "".join(f"remote {address} {int(port)} {proto}\n" for address, port, proto in remotes)


def openvpn_state(environment):
    """What the route-up hook records: the device, the tunnel's addresses and the DNS the server pushed."""
    dns = []
    for key in sorted((k for k in environment if k.startswith("foreign_option_")),
                      key=lambda k: int(k.rpartition("_")[2]) if k.rpartition("_")[2].isdigit() else 0):
        parts = environment[key].split()
        if len(parts) == 3 and parts[0] == "dhcp-option" and parts[1] in ("DNS", "DNS6"):
            with contextlib.suppress(ValueError):
                dns.append(str(ipaddress.ip_address(parts[2])))
    # OpenVPN 2.6 also passes --dns options as dns_server_N_address_M.
    for key in sorted(k for k in environment if re.fullmatch(r"dns_server_\d+_address_\d+", k)):
        with contextlib.suppress(ValueError):
            dns.append(str(ipaddress.ip_address(environment[key].split()[0])))
    return {"dev": environment.get("dev", ""), "ipv4": environment.get("ifconfig_local", ""),
            "ipv6": environment.get("ifconfig_ipv6_local", ""), "dns": list(dict.fromkeys(dns))}


def openvpn_route_up(environment=None, path=RUN / "openvpn.json"):
    """openvpn --route-up: record the state for the runtime (atomically)."""
    state = openvpn_state(dict(os.environ if environment is None else environment))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    os.replace(temporary, path)
    return 0


def up_openvpn(payload, runner, resolve=system_resolver, timeout=90):
    profile = payload["profile"]
    openvpn = profile["openvpn"]
    remotes = []
    for remote in openvpn["remotes"]:
        for family in (4, 6):
            for address in resolve(remote["host"], family)[:4]:
                remotes.append((address, int(remote["port"]), remote["proto"]))
    if not remotes:
        raise ValueError("no remote of the configuration resolves")
    full = openvpn.get("full", True)
    load_killswitch(runner, killswitch_rules(INTERFACES["openvpn"],
                                             [(a, "tcp" if p.startswith("tcp") else "udp", port)
                                              for a, port, p in remotes]))
    RUN.mkdir(mode=0o700, exist_ok=True)
    config = RUN / "client.ovpn"
    write_private(config, payload["secrets"]["openvpn_config"] + "\n" + openvpn_remote_lines(remotes))
    auth = None
    if payload["secrets"].get("openvpn_auth"):
        auth = RUN / "auth"
        write_private(auth, payload["secrets"]["openvpn_auth"] + "\n")
    state = RUN / "openvpn.json"
    with contextlib.suppress(FileNotFoundError):
        state.unlink()
    log_path = RUN / "openvpn.log"
    process = runner.spawn(openvpn_command(config, auth, full), open(log_path, "ab"))
    deadline = time.monotonic() + timeout
    while not state.exists():
        if process.poll() is not None or time.monotonic() > deadline:
            tail = log_tail(log_path, payload)
            raise RuntimeError(f"OpenVPN did not connect{' (it exited)' if process.poll() is not None else ''}:\n{tail}")
        time.sleep(0.3)
    # The credentials stay in this container's private /run: OpenVPN reads them again when it reconnects.
    recorded = json.loads(state.read_text())
    has4, has6 = bool(recorded.get("ipv4")), bool(recorded.get("ipv6"))
    if not has6:
        # The server gave no IPv6 address: none goes into the tunnel either (it would carry a source address of
        # the uplink).
        runner.run(["nft", "insert", "rule", "inet", TABLE, "output", "oifname", INTERFACES["openvpn"], "meta",
                    "nfproto", "ipv6", "drop"])
    return {"interface": INTERFACES["openvpn"], "implementation": "openvpn",
            "addresses": [a for a in (recorded.get("ipv4"), recorded.get("ipv6")) if a],
            "ipv4": has4, "ipv6": has6, "full4": full and has4, "full6": full and has6,
            "endpoints": [f"{format_endpoint(a, port)}/{p}" for a, port, p in remotes],
            "dns": list(profile.get("dns") or recorded.get("dns") or [])}


def tailscale_up_command(tailscale, socket_path, auth_path):
    command = ["tailscale", f"--socket={socket_path}", "up", f"--hostname={tailscale['hostname']}",
               "--accept-dns=false", "--accept-routes=false", "--netfilter-mode=off", "--timeout=300s", "--reset"]
    if auth_path:
        command.append(f"--auth-key=file:{auth_path}")
    if tailscale.get("login_server"):
        command.append(f"--login-server={tailscale['login_server']}")
    if tailscale.get("exit_node"):
        command += [f"--exit-node={tailscale['exit_node']}", "--exit-node-allow-lan-access=false"]
    return command


def tailscale_status(document, exit_node=None):
    """(facts, problems) of `tailscale status --json`."""
    problems = []
    state = document.get("BackendState")
    if state != "Running":
        problems.append(f"tailscale is {state or 'not running'}")
    own = (document.get("Self") or {}).get("TailscaleIPs") or []
    if not own:
        problems.append("the node has no tailnet address")
    exit_status = document.get("ExitNodeStatus")
    if exit_node and not (isinstance(exit_status, dict) and exit_status.get("Online")):
        problems.append(f"the exit node {exit_node} is not active")
    versions = {ipaddress.ip_address(a).version for a in own if isinstance(a, str)}
    return {"addresses": own, "exit_node": exit_status.get("TailscaleIPs") if isinstance(exit_status, dict) else None,
            "ipv4": 4 in versions, "ipv6": 6 in versions}, problems


def up_tailscale(payload, runner, timeout=300, out=None):
    tailscale = payload["profile"]["tailscale"]
    interface = INTERFACES["tailscale"]
    exit_node = tailscale.get("exit_node")
    if exit_node:
        load_killswitch(runner, killswitch_rules(interface, mask=TAILSCALE_MARK[1], mark=TAILSCALE_MARK[0],
                                                 any_marked=True))
    RUN.mkdir(mode=0o700, exist_ok=True)
    socket_path = RUN.parent / "tailscale" / "tailscaled.sock"
    socket_path.parent.mkdir(mode=0o755, exist_ok=True)
    state = "mem:" if tailscale.get("ephemeral") else "/var/lib/tailscale/tailscaled.state"
    log_path = RUN / "tailscaled.log"
    daemon = runner.spawn(["tailscaled", f"--tun={interface}", f"--socket={socket_path}", f"--state={state}",
                           "--port=0"], open(log_path, "ab"))
    deadline = time.monotonic() + 30
    while not socket_path.exists():
        if daemon.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"tailscaled did not start:\n{log_tail(log_path, payload)}")
        time.sleep(0.2)
    auth = None
    if payload["secrets"].get("tailscale_auth_key"):
        auth = RUN / "authkey"
        write_private(auth, payload["secrets"]["tailscale_auth_key"])
    up = runner.stream(tailscale_up_command(tailscale, socket_path, auth))
    announced, output = False, []
    deadline = time.monotonic() + timeout
    while up.poll() is None or select.select([up.stdout], [], [], 0)[0]:
        if time.monotonic() > deadline:
            up.kill()
            raise RuntimeError("tailscale up did not finish (log the node in, or give the profile an auth key)")
        if not select.select([up.stdout], [], [], 0.5)[0]:
            continue
        line = up.stdout.readline()
        if not line:
            break
        output.append(redact(line.rstrip(), [payload["secrets"].get("tailscale_auth_key")]))
        url = re.search(r"https://\S+", line)
        if url and not announced and "visit" in "".join(output[-3:]).lower():
            emit("login", out=out, url=url[0])
            announced = True
    if auth:
        auth.unlink()
    if up.wait() != 0:
        raise RuntimeError("tailscale up failed:\n" + "\n".join(output[-15:]))
    # The node runs once `up` returns; the exit node may take a few seconds more to be reached.
    deadline = time.monotonic() + 30
    while True:
        status = json.loads(runner.run(["tailscale", f"--socket={socket_path}", "status", "--json"]).stdout)
        facts, problems = tailscale_status(status, exit_node)
        if not problems:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("; ".join(problems))
        time.sleep(1)
    return {"interface": interface, "implementation": "tailscale", "addresses": facts["addresses"],
            "ipv4": facts["ipv4"], "ipv6": facts["ipv6"], "full4": bool(exit_node), "full6": bool(exit_node),
            "exit_node": facts["exit_node"], "endpoints": [], "dns": list(payload["profile"].get("dns") or [QUAD100])}


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def log_tail(path, payload, lines=20):
    try:
        text = Path(path).read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return ""
    return "\n".join(redact(line, secret_values(payload)) for line in text)


def route_device(address, runner):
    """The device `ip route get` names for ADDRESS (policy routing included)."""
    result = runner.run(["ip", "route", "get", str(address)], check=False)
    match = re.search(r"\bdev (\S+)", result.stdout or "")
    return match[1] if match else None


READY_SLACK = 10  # seconds of the launcher's wait kept for what follows the checks (the names, the ready event)


def check_deadline(checks, servers, tor, started):
    """When the first address check must end (time.monotonic()): the launcher waits checks["ready_seconds"] from
    about STARTED (vpn.ready_timeout), less what the checks after it take at most (IPv6, each resolver, the DNS and
    onion probes) and READY_SLACK. None without both (a check during the run: nobody waits for it)."""
    seconds = checks.get("ready_seconds")
    if started is None or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    timeout = float(checks.get("timeout", 8))
    urls = len(checks.get("urls") or []) or 1
    after = (timeout / 2 if tor else timeout * urls) + len(servers) * timeout / 2 + \
        (timeout if checks.get("dns_probe") else 0) + (timeout if tor else 0)
    return started + float(seconds) - after - READY_SLACK


def gather_checks(payload, tunnel, runner, get=fetch, started=None, dns=None):
    """The checks from inside: the address each echo service sees over IPv4 and IPv6, the way to each resolver,
    their answers, and the egress address of the resolver chain (whoami.akamai.net answers with the address of
    the resolver that asked it). STARTED (or the payload's "started", main's for the first check): when the launcher
    began to wait for this container - its retries end in time (check_deadline). Behind Tor every lookup, the
    .onion probes too, goes through DNS (a TorDns: main's, else one of its own), patient while tor builds its first
    circuit."""
    checks = payload.get("checks") or {}
    started = payload.get("started") if started is None else started
    servers = tunnel["dns"] or list(FALLBACK_DNS)
    tor = bool(tunnel.get("tor"))
    lookup = (dns or TorDns()).lookup if tor else None
    resolve = resolver_for(servers, lookup)
    result = {"dns": {"servers": servers, "configured": bool(tunnel["dns"]), "routes": {}, "answers": {}}}
    timeout = float(checks.get("timeout", 8))
    if checks.get("ip", True):
        result["ipv4"] = echo_retried(checks.get("urls") or [], 4, resolve, get, timeout, tor,
                                      checks.get("attempts", TOR_ATTEMPTS if tor else ATTEMPTS),
                                      deadline=check_deadline(checks, servers, tor, started))
        result["ipv6"] = probe_ipv6(timeout / 2) if tor else echo(checks.get("urls") or [], 6, resolve, get, timeout)
    probe_name = (checks.get("urls") or ["https://example.com"])[0]
    probe_name = urllib.parse.urlsplit(probe_name).hostname
    for server in servers:
        if not tor:  # tor's DNSPort is on loopback; that every lookup reaches it is shown below
            result["dns"]["routes"][server] = route_device(server, runner)
        try:
            (lookup or dns_lookup)(probe_name, server, 1, timeout=timeout / 2)
            result["dns"]["answers"][server] = True
        except (OSError, ValueError) as exc:
            result["dns"]["answers"][server] = str(exc)
    if checks.get("dns_probe"):
        try:
            result["dns"]["egress"] = resolve(checks["dns_probe"], 4)
        except (OSError, ValueError) as exc:
            result["dns"]["egress_error"] = str(exc)
    if tor:
        # Mapped into TOR_VIRTUAL_NET by tor's DNSPort, and so by an outside resolver if the redirect works - asked as
        # patiently as every other lookup through tor right after its bootstrap (no answer yet is "not yet"); an
        # answer outside TOR_VIRTUAL_NET is a failure all the same (vpn.tor_dns_failures).
        onion = {}
        for where, server in (("tor", TOR_DNS), ("outside", OUTSIDE_RESOLVER)):
            try:
                onion[where] = lookup(ONION_PROBE, server, 1, timeout=timeout / 2)
            except (OSError, ValueError) as exc:
                onion[where] = str(exc)
        result["dns"]["onion"] = onion
    return result


def resolve_hosts(names, tunnel, mode_tailnet=False, lookup=None):
    """{name: [addresses]} for the agent's pinned names, through the tunnel's resolvers (Tailscale without an exit
    node: MagicDNS, then the container's own resolver, since only the tailnet goes through the tunnel). LOOKUP: how
    a resolver is asked (Tor: a TorDns's, patient while tor builds circuits; without one behind Tor - the launcher's
    requests while the tunnel serves - one of TOR_DNS_REQUEST_BUDGET seconds: never a single raw try at tor's
    DNSPort)."""
    if lookup is None and tunnel.get("tor"):
        lookup = TorDns(patience=TOR_DNS_REQUEST_BUDGET, name_patience=TOR_DNS_REQUEST_BUDGET, circuit=True,
                        clock=time.monotonic, sleep=time.sleep,
                        deadline=time.monotonic() + TOR_DNS_REQUEST_BUDGET).lookup
    resolve = resolver_for(tunnel["dns"] or list(FALLBACK_DNS), lookup)
    # Addresses of a family the tunnel does not carry would only make programs try a way the kill switch refuses.
    families = (4, 6) if mode_tailnet else tuple(v for v in (4, 6) if tunnel.get(f"ipv{v}")) or (4,)
    result = {}
    for name in names:
        addresses = []
        for family in families:
            try:
                found = resolve(name, family)
            except OSError:
                if not mode_tailnet and family == families[0]:
                    raise
                found = []
            # Without an exit node only the tailnet goes through Tailscale: other names as without a VPN.
            addresses += found or (system_resolver(name, family) if mode_tailnet else [])
        if not addresses:
            raise ValueError(f"Cannot resolve allowed host {name} through the VPN")
        result[name] = sorted(set(addresses))
    return result


# --- requests of the launcher while the tunnel serves --------------------------------------------------------------
#
# A line on standard input is one request {"op", "id", ...}; the answer is one event {"event": "answer", "id", ...}
# (or with "error"). The launcher sends one at a time and waits for its answer. Every lookup goes to the tunnel's
# resolvers, every connection through the tunnel: sandbox-ai llm check|models|status of a server that only the VPN
# reaches, and the lookups of names allowed during a run (`resolve`, through podman exec).

def addresses_of(name, tunnel, tailnet=False):
    """The addresses of NAME through the tunnel's resolvers (resolve_hosts' rules for families and the tailnet)."""
    return resolve_hosts([name], tunnel, tailnet)[name]


def within_networks(address, networks):
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in map(ipaddress.ip_network, networks))


def target_of(url, tunnel, tailnet, resolve=None):
    """(parts, address, port) for URL (http or https): its host resolved through the tunnel."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https URLs")
    try:
        address = str(ipaddress.ip_address(parts.hostname))
    except ValueError:
        found = (resolve or (lambda name: addresses_of(name, tunnel, tailnet)))(parts.hostname)
        if not found:
            raise ValueError(f"{parts.hostname} has no address through the VPN") from None
        address = found[0]
    return parts, address, parts.port or (443 if parts.scheme == "https" else 80)


def tls_context_for(ca=None):
    context = ssl.create_default_context()
    if ca:
        context.load_verify_locations(cadata=ca)
    return context


def plain_problem(parts, address, request):
    """Why a request with a secret (an API key, a header the launcher marked) may not go over plain http to ADDRESS:
    only to the networks the launcher names ("plain_networks"; None: any, the endpoint was added with
    --insecure-http). None when it may."""
    networks = request.get("plain_networks")
    if parts.scheme != "http" or not request.get("secret") or networks is None:
        return None
    if within_networks(address, networks):
        return None
    return (f"plain http to {address} would cross networks you do not control unencrypted (the key would go with "
            "it; nothing was sent)")


def request_connect(request, tunnel, tailnet, connect=socket.create_connection, resolve=None):
    """Reach URL's host and port through the tunnel; for https the TLS handshake with the certificate checked
    (the request's "ca" too): {"address", "tls"}."""
    parts, address, port = target_of(request["url"], tunnel, tailnet, resolve)
    timeout = float(request.get("timeout") or REQUEST_TIMEOUT)
    with connect((address, port), timeout=timeout) as sock:
        if parts.scheme == "https":
            with tls_context_for(request.get("ca")).wrap_socket(sock, server_hostname=parts.hostname) as tls:
                return {"address": address, "tls": tls.version()}
    return {"address": address, "tls": None}


def request_http(request, tunnel, tailnet, resolve=None):
    """One HTTP request through the tunnel, no redirect followed: {"status", "body" (base64), "address"}."""
    parts, address, port = target_of(request["url"], tunnel, tailnet, resolve)
    problem = plain_problem(parts, address, request)
    if problem:
        raise ValueError(problem)
    timeout = float(request.get("timeout") or REQUEST_TIMEOUT)
    if parts.scheme == "https":
        connection = PinnedHTTPSConnection(parts.hostname, address, port, timeout, tls_context_for(request.get("ca")))
    else:
        connection = PinnedHTTPConnection(parts.hostname, address, port, timeout)
    limit = min(int(request.get("limit") or REQUEST_LIMIT), REQUEST_LIMIT)
    body = base64.b64decode(request["body"]) if request.get("body") else None
    headers = {str(k): str(v) for k, v in (request.get("headers") or {}).items()}
    try:
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection.request(request.get("method") or ("GET" if body is None else "POST"), target, body=body,
                           headers=headers)
        response = connection.getresponse()
        data = response.read(limit + 1)[:limit]
        return {"status": response.status, "body": base64.b64encode(data).decode(), "address": address}
    finally:
        connection.close()


def handle_request(line, tunnel, tailnet, secrets=()):
    """The answer event to one request LINE of the launcher."""
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("a request is a JSON object")
    except (ValueError, RecursionError) as exc:
        return {"event": "answer", "id": None, "error": f"unreadable request: {exc}"}
    ident, op = request.get("id"), request.get("op")
    try:
        if op == "resolve":
            names = request.get("names")
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ValueError("names: a list of host names")
            return {"event": "answer", "id": ident, "hosts": resolve_hosts(names, tunnel, tailnet)}
        if op == "connect":
            return {"event": "answer", "id": ident, **request_connect(request, tunnel, tailnet)}
        if op == "http":
            return {"event": "answer", "id": ident, **request_http(request, tunnel, tailnet)}
        raise ValueError(f"unknown request {op!r}")
    except (OSError, ValueError, KeyError, TypeError, ssl.SSLError, http.client.HTTPException) as exc:
        extra = [str(v) for v in (request.get("headers") or {}).values()] if isinstance(request.get("headers"), dict) \
            else []
        return {"event": "answer", "id": ident, "error": redact(f"{getattr(exc, 'reason', None) or exc}",
                                                              [*secrets, *extra])}


def recheck(payload, tunnel, runner, get=fetch):
    """The address checks again while serving (IPv4 and IPv6 echo, the resolvers): the "recheck" event's checks."""
    return gather_checks(payload, tunnel, runner, get)


def save_serve_state(tunnel, tailnet, path=None):
    """What `resolve` needs of the running tunnel, in the container's private /run."""
    with contextlib.suppress(OSError):
        write_private(path or SERVE_STATE, json.dumps({"dns": tunnel.get("dns") or [], "ipv4": tunnel.get("ipv4"),
                                                       "ipv6": tunnel.get("ipv6"), "tailnet": bool(tailnet),
                                                       "tor": bool(tunnel.get("tor"))}))


def baseline(settings, resolve=system_resolver, get=fetch):
    """The public addresses (IPv4, IPv6) and the resolver's egress of the network this runs in, outside every VPN:
    what the checks compare the tunnel's with."""
    result = {}
    for family in (4, 6):
        result[f"ipv{family}"] = echo(settings["urls"], family, resolve, get, settings["timeout"]).get("ip")
    if settings.get("dns_probe"):
        try:
            result["dns_egress"] = resolve(settings["dns_probe"], 4)
        except (OSError, ValueError):
            result["dns_egress"] = []
    return result


def baseline_command(stdin, out):
    """`vpn_runtime.py baseline`: baseline() of the settings on standard input, as one JSON line (a remote Podman
    engine's own addresses: the launcher runs this in a plain container there)."""
    try:
        settings = json.loads(stdin.read(1 << 16))
        out.write(json.dumps(baseline({"urls": [u for u in settings.get("urls") or [] if isinstance(u, str)],
                                       "timeout": float(settings.get("timeout") or 8),
                                       "dns_probe": settings.get("dns_probe")})) + "\n")
        return 0
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        out.write(json.dumps({"error": str(exc)}) + "\n")
        return 1


def resolve_command(stdin, out, path=None, clock=time.monotonic, sleep=time.sleep):
    """`vpn_runtime.py resolve`, run by the launcher with podman exec in the running VPN container: the names on
    standard input (a JSON list) resolved through the tunnel's resolvers, {"hosts": {...}} or {"error"} on stdout.
    Its lookups are marked as the checks' are: the agent's firewall in the namespace does not hold them (tor's
    DNSPort is closed to the agent without --dns)."""
    use_check_mark()
    try:
        state = json.loads(Path(path or SERVE_STATE).read_text())
        names = json.loads(stdin.read(1 << 16))
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names) or len(names) > 256:
            raise ValueError("expected a JSON list of host names")
        tunnel = {"dns": state.get("dns") or [], "ipv4": state.get("ipv4"), "ipv6": state.get("ipv6")}
        # Behind Tor a name an exit could not look up (SERVFAIL) is asked again, within the launcher's 30 s.
        lookup = TorDns(patience=TOR_DNS_RESOLVE_PATIENCE, name_patience=TOR_DNS_RESOLVE_PATIENCE,
                        circuit=True, clock=clock, sleep=sleep).lookup if state.get("tor") else None
        out.write(json.dumps({"hosts": resolve_hosts(names, tunnel, bool(state.get("tailnet")), lookup)}) + "\n")
        return 0
    except (OSError, ValueError, RecursionError) as exc:
        out.write(json.dumps({"error": str(exc)}) + "\n")
        return 1


def main(argv, stdin=sys.stdin, out=sys.stdout, runner=None, clock=time.monotonic, sleep=time.sleep):
    if argv[:1] == ["openvpn-route-up"]:
        return openvpn_route_up()
    if argv[:1] == ["resolve"]:
        return resolve_command(stdin, out)
    if argv[:1] == ["baseline"]:
        return baseline_command(stdin, out)
    if argv[:1] == ["killswitch"]:
        return killswitch_command(out)
    if argv[:1] not in (["serve"], ["check"]):
        print("sandbox-ai vpn runtime: internal entry point; use sandbox-ai vpn", file=sys.stderr)
        return 2
    payload = json.loads(stdin.readline())
    started = time.monotonic()  # (the launcher waits from about now: vpn.ready_timeout)
    runner = runner or Runner()
    use_check_mark()  # the agent's firewall, loaded later in this namespace, lets the checks pass to the tunnel
    borrow = payload.get("borrow")
    try:
        kind = payload["profile"]["type"]
        emit("progress", out=out, message="borrowing the start's tunnel" if borrow else f"bringing up {kind}")
        if borrow:
            # An MCP server's VPN container behind its start's tunnel (lent): no tunnel of its own, the same key never
            # twice. A Tor profile is never lent (each sandbox its own Tor), nor one that carries only the tailnet.
            if kind == "tor" or argv[0] != "serve":
                raise ValueError("only a serving container of a VPN profile that is no Tor profile borrows a tunnel")
            # The run's token, with which the lender knows its own run's borrowers: without it nothing is borrowed.
            use_lend_token((payload.get("secrets") or {}).get("lend_token"))
            tunnel = up_borrowed(payload, runner)
        elif kind == "wireguard":
            tunnel = up_wireguard(payload, runner)
        elif kind == "openvpn":
            tunnel = up_openvpn(payload, runner)
        elif kind == "tailscale":
            tunnel = up_tailscale(payload, runner, out=out)
        elif kind == "tor":
            tunnel = up_tor(payload, runner, out=out)
        else:
            raise ValueError(f"unknown VPN type {kind!r}")
        tailnet = kind == "tailscale" and not payload["profile"]["tailscale"].get("exit_node") and not borrow
        first = {**payload, "started": started}  # (only the first check: the launcher waits for it)
        # Tor: its first lookups may meet a DNSPort without a circuit yet (SERVFAIL): waited for within the
        # launcher's wait and said as progress - one wait for the checks and the agent's names alike.
        ready_seconds = (payload.get("checks") or {}).get("ready_seconds")
        waits = isinstance(ready_seconds, (int, float)) and not isinstance(ready_seconds, bool)
        dns = TorDns(out=out, clock=clock, sleep=sleep, deadline=clock() + float(ready_seconds) - READY_SLACK - (
            time.monotonic() - started) if waits else None) if kind == "tor" else None
        checks = gather_checks(first, tunnel, runner, **({"dns": dns} if dns else {})) if not tailnet else \
            gather_checks({**first, "checks": {**(payload.get("checks") or {}), "ip": False}}, tunnel, runner)
        hosts = resolve_hosts(payload.get("resolve") or [], tunnel, tailnet,
                              **({"lookup": dns.lookup} if dns else {}))
        lend = payload.get("lend")
        if lend and argv[0] == "serve":
            # The start's VPN container lends its tunnel to the start's own MCP servers (see "the start's tunnel,
            # lent"): before "ready". A start that has such servers now ("required": the launcher says so; a payload
            # that does not say is taken so) does not go on without it; any other goes on - its own network needs no
            # lender - and says in "ready" that, and why, its tunnel is not lent (the launcher then refuses a server
            # attached to it later).
            required = not (isinstance(lend, dict) and lend.get("required") is False)
            try:
                if kind == "tor" or tailnet or borrow:
                    raise ValueError("a Tor profile, a tailnet-only profile or a borrowed tunnel is not lent")
                Lender(tunnel, payload["profile"]["name"],
                       token=(payload.get("secrets") or {}).get("lend_token") or "").start()
                tunnel["lends"] = True
            except Exception as exc:  # noqa: BLE001 - whatever it is, the tunnel is not lent
                if required:
                    raise RuntimeError(f"the tunnel cannot be lent to the start's MCP servers: {exc}") from None
                tunnel["lends"] = False
                tunnel["lend_error"] = redact(f"{exc}", secret_values(payload))[:300]
        if payload.get("mcp") and kind != "tor" and not borrow:
            # The container of an MCP image server (vpn.mcp_wrapper): its lookups go to the VPN's resolver. Tor's
            # rules hand every lookup to its DNSPort already (and a borrowing container's to its relay); a
            # tailnet-only profile is refused before (vpn.py egress_problem) and here again.
            if tailnet:
                raise ValueError("a Tailscale profile without an exit node carries only the tailnet; it cannot be "
                                 "an MCP server's way out")
            tunnel["mcp_dns"] = mcp_dns_resolver(tunnel)
            source = mcp_dns_source(tunnel)
            if source:
                tunnel["mcp_dns_source"] = source
            runner.run(["nft", "-f", "-"], data=mcp_dns_rules(tunnel["mcp_dns"], tunnel["interface"], source=source))
        emit("ready", out=out, tunnel=tunnel, checks=checks, hosts=hosts)
    except Exception as exc:  # noqa: BLE001 - everything ends as an event the launcher shows
        emit("error", out=out, message=redact(f"{exc}", secret_values(payload)))
        return 1
    if argv[0] == "check":
        return 0
    # Serve: keep the tunnel until the launcher closes stdin; answer its requests (a line each); check the address
    # again every RECHECK seconds; report a daemon that dies (the kill switch stays).
    save_serve_state(tunnel, tailnet)
    checks_of = payload.get("checks") or {}
    every = float(checks_of.get("recheck", RECHECK) or 0) if not tailnet and checks_of.get("ip", True) else 0
    first = float(checks_of.get("first_recheck") or every)
    due = time.monotonic() + min(every, first)
    secrets_of = secret_values(payload)
    looked, misses = time.monotonic(), 0
    while True:
        if borrow and time.monotonic() - looked >= LEND_PING:
            # The lender is the start's VPN container: once it is gone (the start ended), nothing here reaches
            # anything any more - said, and this container ends (its kill switch stays in the namespace).
            looked = time.monotonic()
            gone = lender_gone(payload["profile"]["name"])
            misses = misses + 1 if gone else 0
            if misses >= LEND_MISSES:
                emit("down", out=out, message=f"the start that lent its tunnel is gone ({gone}); the kill switch "
                     "blocks all traffic now")
                return 3
        ready = select.select([stdin], [], [], max(0.0, min(2.0, due - time.monotonic())) if every else 2)[0]
        if ready:
            line = stdin.readline()
            if not line:
                return 0
            if line.strip():
                answer = handle_request(line, tunnel, tailnet, secrets_of)
                emit(answer.pop("event"), out=out, **answer)
        if every and time.monotonic() >= due:
            due = time.monotonic() + every
            try:
                emit("recheck", out=out, checks=recheck(payload, tunnel, runner))
            except Exception as exc:  # noqa: BLE001 - said, the next one comes at its time
                emit("recheck", out=out, error=redact(f"{exc}", secrets_of))
        for child in list(runner.children):
            if child.poll() is not None:
                emit("down", out=out, message=f"{child.args[0]} exited ({child.returncode}); the kill switch "
                     "blocks all traffic now")
                runner.children.remove(child)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
