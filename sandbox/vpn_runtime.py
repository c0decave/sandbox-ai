#!/usr/bin/env python3
"""The VPN container's runtime: tunnel, kill switch and checks. Standard library only.

Runs as `python3 -I /vpn/vpn_runtime.py serve|check` in localhost/sandbox-ai-vpn (Containerfile.vpn), in the
network namespace of the agent's pod (or of its own container for `sandbox-ai vpn check` and MCP image
servers). The launcher (vpn.py) loads this file by path too, for what both sides share: the DNS client, the
address echo and the kill-switch rules.

The launcher writes one JSON line to stdin: the profile with its secrets, the names to resolve and the
checks. Secrets arrive that way only, never as arguments, environment or files outside this container's
private /run. Events go to stdout as JSON lines ({"event": "progress" | "login" | "ready" | "error" | "down"}).
`serve` keeps the tunnel until stdin closes; `check` exits after the checks.

Order: the endpoints are resolved first (the only lookups outside the tunnel), then the kill switch is
loaded, and only then is the tunnel brought up: nothing leaves unencrypted while it is being set up, and a
tunnel that dies leaves the kill switch behind (fail closed).

Tor has no tunnel interface: the kill switch lets only tor's own user (debian-tor, TOR_UID) out, over IPv4 TCP
(to its bridges alone when it has some), and a NAT chain hands every other TCP connection of the namespace to
tor's TransPort and every lookup on port 53 to its DNSPort; other UDP and all IPv6 are dropped.
"""
import contextlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import select
import socket
import ssl
import struct
import subprocess
import sys
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
# Tor (vpn add --tor). Containerfile.vpn creates debian-tor with this uid before Debian's tor package (which keeps
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
BOOTSTRAP_TIMEOUT = 240
BRIDGE_TRANSPORTS = {"obfs4": "/usr/bin/obfs4proxy"}
DEFAULT_MTU = 1420
DEFAULT_KEEPALIVE = 25  # keeps the conntrack entry (and NAT on the way) of the endpoint alive
ECHO_LIMIT = 64 << 10
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
        raise ValueError(f"DNS error code {rcode}")
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
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
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
        with socket.create_connection((server, port), timeout=max(deadline - time.monotonic(), 0.5)) as sock:
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

class PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP to ADDRESS, whatever the host name resolves to elsewhere (the name came from our own resolver)."""

    def __init__(self, host, address, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ADDRESS with the certificate checked for the host name (SNI)."""

    def __init__(self, host, address, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self.address = address
        self.tls = context

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
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
    except ValueError:
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
    errors, kinds = [], set()
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
            blocked = exc.errno in BLOCKED_ERRNOS or isinstance(exc, (socket.timeout, TimeoutError))
            kinds.add("blocked" if blocked else "failed")
            errors.append(f"{host}: {exc.strerror or exc}")
        except (ValueError, http.client.HTTPException) as exc:
            kinds.add("failed")
            errors.append(f"{host}: {exc}")
    kind = "blocked" if kinds == {"blocked"} or kinds == {"blocked", "no-address"} else \
        "no-address" if kinds == {"no-address"} else "failed"
    return {"error": "; ".join(errors) or "no echo service configured", "kind": kind}


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


def tor_rules(uid=TOR_UID, bridges=()):
    """The kill switch for tor: only tor's own TCP over IPv4 leaves (to its bridges alone when it has some); every
    other TCP connection is redirected to its TransPort and every lookup on port 53 to its DNSPort, before the
    filter sees them; other UDP (QUIC included) and all IPv6 are dropped. The redirect runs after the agent's own
    to its connection proxy (priority -100): what that one redirects reaches tor through the proxy."""
    uid = int(uid)
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
    lines += ["  reject with icmpx type admin-prohibited", " }",
              " chain input { type filter hook input priority -5; policy drop;",
              '  iifname "lo" accept', "  ct state established,related accept", " }",
              " chain forward { type filter hook forward priority -5; policy drop; }",
              " chain divert { type nat hook output priority -90; policy accept;",
              f"  meta skuid {uid} return", "  ip daddr 127.0.0.0/8 return", "  meta nfproto ipv6 return",
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


def wait_bootstrap(log_path, process, timeout=BOOTSTRAP_TIMEOUT, out=None, secrets=(), clock=time.monotonic,
                   sleep=time.sleep):
    """Wait until tor's log says "Bootstrapped 100%", reporting each step; an error when tor exits or the
    TIMEOUT passes, with how far it got and the log's last lines (SECRETS, the bridges, hidden)."""
    deadline = clock() + timeout
    shown = None

    def tail(text):
        return "\n".join(redact(line, secrets) for line in text.splitlines()[-15:])
    while True:
        try:
            text = Path(log_path).read_text(errors="replace")
        except OSError:
            text = ""
        percent, summary = bootstrap_progress(text)
        if percent != shown:
            emit("progress", out=out, message=f"tor: bootstrapped {percent}%"
                 + (f" ({redact(summary, secrets)})" if summary else ""))
            shown = percent
        if percent >= 100:
            return percent
        if process.poll() is not None:
            raise RuntimeError(f"tor exited ({process.returncode}) at {percent}% of its bootstrap:\n{tail(text)}")
        if clock() > deadline:
            got = f"{percent}%" + (f": {redact(summary, secrets)}" if summary else "")
            raise RuntimeError(f"tor did not finish its bootstrap within {timeout} s (it got to {got}); is the Tor "
                               "network blocked here? Bridges help: sandbox-ai vpn add NAME --tor --bridges FILE\n"
                               + tail(text))
        sleep(0.5)


def tor_uid(name=TOR_USER):
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        raise RuntimeError(f"the VPN image has no user {name}: rebuild it with sandbox-ai build vpn") from None


def up_tor(payload, runner, out=None, timeout=BOOTSTRAP_TIMEOUT, uid_of=None):
    """tor behind its kill switch: the rules first, then the torrc (private, with the bridges), then tor, which
    binds its ports as root and becomes debian-tor; ready once it has bootstrapped."""
    tor = payload["profile"]["tor"]
    bridges = parse_bridges((payload.get("secrets") or {}).get("tor_bridges") or "")
    if tor.get("bridges") and not bridges:
        raise ValueError("the profile has bridges, but none arrived")
    uid = (uid_of or tor_uid)()
    if uid != TOR_UID:
        raise RuntimeError(f"{TOR_USER} has the uid {uid} in the VPN image, not {TOR_UID}: rebuild it with "
                           "sandbox-ai build vpn")
    load_killswitch(runner, tor_rules(uid, bridges))
    RUN.mkdir(mode=0o700, exist_ok=True)
    config, defaults = RUN / "torrc", RUN / "torrc-defaults"
    write_private(config, tor_config(tor, bridges))
    write_private(defaults, "")
    log_path = RUN / "tor.log"
    process = runner.spawn(["tor", "-f", str(config), "--defaults-torrc", str(defaults)], open(log_path, "ab"))
    wait_bootstrap(log_path, process, timeout, out, secret_values(payload))
    count = len(bridges)
    return {"interface": f"TransPort 127.0.0.1:{TOR_TRANS_PORT}", "implementation": "tor", "addresses": [],
            "ipv4": True, "ipv6": False, "full4": True, "full6": False,
            "endpoints": [f"{count} bridge{'s' if count != 1 else ''}"] if count else ["the Tor network"],
            "dns": [TOR_DNS],
            "tor": {"uid": uid, "bridges": count, "exit_country": exit_countries(tor.get("exit_country")),
                    "socks": f"127.0.0.1:{TOR_SOCKS_PORT}" if tor.get("socks") else None,
                    "trans_port": f"127.0.0.1:{TOR_TRANS_PORT}"}}


def probe_ipv6(timeout, connect=socket.create_connection):
    """Behind Tor there is no IPv6 resolver to ask an echo service with: a connection to a literal IPv6 address
    must fail locally. {"error", "kind": "blocked"|"failed"}, or {"ip", ...} when it connected (a leak)."""
    try:
        sock = connect((IPV6_PROBE, 443), timeout)
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


def gather_checks(payload, tunnel, runner, get=fetch):
    """The checks from inside: the address each echo service sees over IPv4 and IPv6, the way to each resolver,
    their answers, and the egress address of the resolver chain (whoami.akamai.net answers with the address of
    the resolver that asked it)."""
    checks = payload.get("checks") or {}
    servers = tunnel["dns"] or list(FALLBACK_DNS)
    resolve = resolver_for(servers)
    result = {"dns": {"servers": servers, "configured": bool(tunnel["dns"]), "routes": {}, "answers": {}}}
    timeout = float(checks.get("timeout", 8))
    tor = bool(tunnel.get("tor"))
    if checks.get("ip", True):
        result["ipv4"] = echo(checks.get("urls") or [], 4, resolve, get, timeout)
        result["ipv6"] = probe_ipv6(timeout / 2) if tor else echo(checks.get("urls") or [], 6, resolve, get, timeout)
    probe_name = (checks.get("urls") or ["https://example.com"])[0]
    probe_name = urllib.parse.urlsplit(probe_name).hostname
    for server in servers:
        if not tor:  # tor's DNSPort is on loopback; that every lookup reaches it is shown below
            result["dns"]["routes"][server] = route_device(server, runner)
        try:
            dns_lookup(probe_name, server, 1, timeout=timeout / 2)
            result["dns"]["answers"][server] = True
        except (OSError, ValueError) as exc:
            result["dns"]["answers"][server] = str(exc)
    if checks.get("dns_probe"):
        try:
            result["dns"]["egress"] = resolve(checks["dns_probe"], 4)
        except (OSError, ValueError) as exc:
            result["dns"]["egress_error"] = str(exc)
    if tor:
        # Mapped into TOR_VIRTUAL_NET by tor's DNSPort, and so by an outside resolver if the redirect works.
        onion = {}
        for where, server in (("tor", TOR_DNS), ("outside", OUTSIDE_RESOLVER)):
            try:
                onion[where] = dns_lookup(ONION_PROBE, server, 1, timeout=timeout / 2)
            except (OSError, ValueError) as exc:
                onion[where] = str(exc)
        result["dns"]["onion"] = onion
    return result


def resolve_hosts(names, tunnel, mode_tailnet=False):
    """{name: [addresses]} for the agent's pinned names, through the tunnel's resolvers (Tailscale without an exit
    node: MagicDNS, then the container's own resolver, since only the tailnet goes through the tunnel)."""
    resolve = resolver_for(tunnel["dns"] or list(FALLBACK_DNS))
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


def main(argv, stdin=sys.stdin, out=sys.stdout, runner=None):
    if argv[:1] == ["openvpn-route-up"]:
        return openvpn_route_up()
    if argv[:1] not in (["serve"], ["check"]):
        print("sandbox-ai vpn runtime: internal entry point; use sandbox-ai vpn", file=sys.stderr)
        return 2
    payload = json.loads(stdin.readline())
    runner = runner or Runner()
    try:
        kind = payload["profile"]["type"]
        emit("progress", out=out, message=f"bringing up {kind}")
        if kind == "wireguard":
            tunnel = up_wireguard(payload, runner)
        elif kind == "openvpn":
            tunnel = up_openvpn(payload, runner)
        elif kind == "tailscale":
            tunnel = up_tailscale(payload, runner, out=out)
        elif kind == "tor":
            tunnel = up_tor(payload, runner, out=out)
        else:
            raise ValueError(f"unknown VPN type {kind!r}")
        tailnet = kind == "tailscale" and not payload["profile"]["tailscale"].get("exit_node")
        checks = gather_checks(payload, tunnel, runner) if not tailnet else \
            gather_checks({**payload, "checks": {**(payload.get("checks") or {}), "ip": False}}, tunnel, runner)
        hosts = resolve_hosts(payload.get("resolve") or [], tunnel, tailnet)
        emit("ready", out=out, tunnel=tunnel, checks=checks, hosts=hosts)
    except Exception as exc:  # noqa: BLE001 - everything ends as an event the launcher shows
        emit("error", out=out, message=redact(f"{exc}", secret_values(payload)))
        return 1
    if argv[0] == "check":
        return 0
    # Serve: keep the tunnel until the launcher closes stdin; report a daemon that dies (the kill switch stays).
    while True:
        ready = select.select([stdin], [], [], 2)[0]
        if ready and not stdin.readline():
            return 0
        for child in list(runner.children):
            if child.poll() is not None:
                emit("down", out=out, message=f"{child.args[0]} exited ({child.returncode}); the kill switch "
                     "blocks all traffic now")
                runner.children.remove(child)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
