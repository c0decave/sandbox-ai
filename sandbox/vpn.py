#!/usr/bin/env python3
"""sandbox-ai vpn: a VPN in front of a sandbox, with a kill switch and IP/DNS checks.

  sandbox-ai vpn add NAME --wireguard FILE          any wg-quick configuration (Proton, IVPN, AirVPN, your own server)
  sandbox-ai vpn add NAME --openvpn FILE [--auth-secret SECRET]
  sandbox-ai vpn add NAME --mullvad [--account-secret SECRET] [--location se-sto]
  sandbox-ai vpn add NAME --tailscale [--auth-key-secret SECRET] [--exit-node HOST] [--login-server URL]
  sandbox-ai vpn add NAME --tor [--bridges FILE | --bridge-secret SECRET] [--exit-country CC] [--socks]
  sandbox-ai vpn list | show NAME | remove NAME | check NAME

Then: sandbox-ai claude --vpn NAME (also in .sandbox-ai.json), an MCP server's options or `sandbox-ai mcp add
... --vpn NAME`. The tunnel runs in its own container (localhost/sandbox-ai-vpn, `sandbox-ai build vpn`) in the
network namespace of the agent's pod: there nothing leaves except through the tunnel and the tunnel's own packets
to its endpoints, over IPv4 and IPv6; a family the tunnel does not carry is blocked. Before the agent starts, the
address the internet sees (IPv4 and IPv6 separately) must differ from this host's own, the provider must confirm
the VPN where it can (Mullvad), and DNS must go to the VPN's resolvers through the tunnel; a failed check stops the
start (--vpn-check warn only warns). Tor has no tunnel: every TCP connection goes to tor's TransPort, every lookup
to its DNSPort (.onion names included), only tor's own connections leave, and the exit must be one by the Tor
Project's check.

Keys, account numbers, passwords and auth keys live in sandbox-ai secrets (or 0600 files); they reach only the VPN
container, through its standard input, never the agent's or an MCP server's files, environment, logs or the
dry-run plan. Profiles: ~/.config/sandbox-ai/vpn. Standard library only.
"""
import argparse
import base64
import contextlib
import fcntl
import getpass
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent
VPN_IMAGE = "localhost/sandbox-ai-vpn:latest"
PROFILE_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"  # = agent_sandbox.VPN_PROFILE_NAME
TYPES = ("wireguard", "openvpn", "tailscale", "tor")
# Address echo services (JSON with "ip", or the bare address). Mullvad's also says whether the address is one of
# its exits. Each is asked over IPv4 and over IPv6 separately; both are dual-stack.
ECHO_GENERIC = ["https://api64.ipify.org/?format=json", "https://ifconfig.co/json"]
ECHO_MULLVAD = ["https://am.i.mullvad.net/json"]
# Answers with the address of the resolver that asked for it: where DNS lookups really reach the internet.
DNS_PROBE = "whoami.akamai.net"
MULLVAD_API = "https://api.mullvad.net"
MULLVAD_DNS = ("10.64.0.1", "fc00:bbbb:bbbb:bb01::1")
MULLVAD_PORT = 51820
READY_TIMEOUT = 120
LOGIN_TIMEOUT = 300
FILE_LIMIT = 1 << 20
_runtime = _launcher = None


def runtime():
    """vpn_runtime.py (the container's runtime), whose helpers both sides share."""
    global _runtime
    if _runtime is None:
        spec = importlib.util.spec_from_file_location("sandbox_vpn_runtime", HERE / "vpn_runtime.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _runtime is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _runtime = loaded
    return _runtime


def launcher():
    """The launcher module; agent_sandbox.load_vpn_module() replaces this with the running launcher."""
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_vpn", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


def secret_store():
    spec = importlib.util.spec_from_file_location("sandbox_secret_store_for_vpn", HERE / "secret_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the profile store ------------------------------------------------------------------------------------------

def profiles_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "vpn"


def state_dir(name):
    """Private run-time state of a profile (Tailscale's node keys), mounted into its VPN container only."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return data / "sandbox-ai" / "vpn" / check_name(name)


def check_name(name):
    if not isinstance(name, str) or not re.fullmatch(PROFILE_NAME, name):
        raise ValueError(f"a VPN profile name is 1-32 letters, digits, '_' or '-': {name!r}")
    return name


def secret_name(profile, suffix):
    """Name of a secret that belongs to PROFILE (sandbox-ai secrets allow letters, digits and _)."""
    return f"VPN_{re.sub(r'[^A-Za-z0-9]', '_', check_name(profile)).upper()}_{suffix}"


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_private(path, text):
    """Replace PATH by a new private (0600) file with TEXT."""
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
    os.replace(temporary, path)


def read_private(path, limit=FILE_LIMIT):
    """A private file of ours: regular, not a link, owned by us, mode 0600 or stricter."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(f"{path} must be your own private file (chmod 600)")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path} is larger than {limit} bytes")
    return data.decode("utf-8", "replace")


def profile_path(name):
    return profiles_dir() / f"{check_name(name)}.json"


def load_profile(name):
    path = profile_path(name)
    if not path.exists():
        known = ", ".join(list_profiles()) or "none yet"
        raise ValueError(f"no VPN profile {name} (profiles: {known}); add one with: sandbox-ai vpn add {name} ...")
    try:
        profile = json.loads(read_private(path))
    except ValueError as exc:
        raise ValueError(f"VPN profile {name}: {exc}") from None
    if not isinstance(profile, dict) or profile.get("version") != 1 or profile.get("type") not in TYPES \
            or profile.get("name") != name:
        raise ValueError(f"{path} is not a VPN profile of this sandbox-ai version")
    return profile


def save_profile(profile):
    write_private(profile_path(profile["name"]), json.dumps(profile, indent=2) + "\n")


def list_profiles():
    try:
        return sorted(p.stem for p in profiles_dir().glob("*.json") if re.fullmatch(PROFILE_NAME, p.stem))
    except OSError:
        return []


# --- WireGuard ----------------------------------------------------------------------------------------------------

WG_KEY = re.compile(r"[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=")
HOST = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
WG_COMMANDS = {"preup", "postup", "predown", "postdown"}


def wg_key(value, what):
    value = value.strip()
    if not WG_KEY.fullmatch(value) or len(base64.b64decode(value)) != 32:
        raise ValueError(f"{what} is not a WireGuard key (44 characters of base64)")
    return value


def parse_endpoint(value, what="Endpoint"):
    """{"host", "port"} of HOST:PORT, IPV4:PORT or [IPV6]:PORT."""
    match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:([0-9]{1,5})|([^\[\]:\s]+):([0-9]{1,5})", value.strip())
    if not match:
        raise ValueError(f"{what} {value!r}: expected HOST:PORT or [IPV6]:PORT")
    host, port = (match[1], match[2]) if match[1] else (match[3], match[4])
    if not 1 <= int(port) <= 65535:
        raise ValueError(f"{what} {value!r}: port out of range")
    try:
        host = str(ipaddress.ip_address(host))
        if "%" in host:
            raise ValueError(f"{what} {value!r}: no scope IDs")
    except ValueError:
        if not HOST.fullmatch(host):
            raise ValueError(f"{what} {value!r}: invalid host") from None
    return {"host": host, "port": int(port)}


def parse_wireguard(text):
    """(wireguard, secrets, notes) of a wg-quick configuration. Keys go to SECRETS, the rest (addresses, DNS, peers
    with their public keys, AllowedIPs and endpoints) to WIREGUARD. Commands (PreUp, PostUp, ...) are refused:
    wg-quick would run them as root; sandbox-ai sets the tunnel up itself and runs nothing from a configuration."""
    section, interface, peers, notes = None, {}, [], []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section == "peer":
                peers.append({})
            elif section != "interface":
                raise ValueError(f"line {number}: unknown section {line}")
            continue
        key, sep, value = line.partition("=")
        name, key, value = key.strip(), key.strip().lower(), value.strip()
        if not sep or section is None:
            raise ValueError(f"line {number}: expected KEY = VALUE inside [Interface] or [Peer]")
        if key in WG_COMMANDS:
            raise ValueError(f"line {number}: {name} would run a command as root (wg-quick does); sandbox-ai runs "
                             "no commands from a configuration: remove the line")
        target = interface if section == "interface" else peers[-1]
        if key in ("address", "dns", "allowedips"):
            target.setdefault(key, []).extend(v.strip() for v in value.split(",") if v.strip())
        elif key in target:
            raise ValueError(f"line {number}: {name} given twice")
        else:
            target[key] = value
    known_interface = {"privatekey", "address", "dns", "mtu", "listenport", "fwmark", "table", "saveconfig"}
    known_peer = {"publickey", "presharedkey", "allowedips", "endpoint", "persistentkeepalive"}
    for key in set(interface) - known_interface:
        raise ValueError(f"[Interface] {key}: not supported")
    if "privatekey" not in interface:
        raise ValueError("[Interface] has no PrivateKey")
    secrets_ = {"private_key": wg_key(interface["privatekey"], "PrivateKey"), "preshared_keys": {}}
    addresses = []
    for value in interface.get("address", []):
        try:
            address = ipaddress.ip_interface(value)
        except ValueError:
            raise ValueError(f"Address {value!r} is not an address with prefix") from None
        addresses.append(str(address))
    if not addresses:
        raise ValueError("[Interface] has no Address")
    dns, search = [], []
    for value in interface.get("dns", []):
        try:
            address = ipaddress.ip_address(value)
            if "%" in value:
                raise ValueError(f"DNS {value!r}: no scope IDs")
            dns.append(str(address))
        except ValueError:
            if not HOST.fullmatch(value):
                raise ValueError(f"DNS {value!r} is neither an address nor a search domain") from None
            search.append(value)
    if search:
        notes.append(f"search domains are not used: {', '.join(search)}")
    if interface.get("fwmark") and interface["fwmark"].lower() not in ("off", "0"):
        notes.append("FwMark is replaced by sandbox-ai's own mark (the kill switch knows the tunnel by it)")
    if interface.get("table", "auto").lower() not in ("auto",):
        raise ValueError("Table: sandbox-ai routes the tunnel itself; remove the line (only auto is accepted)")
    if interface.get("saveconfig", "false").lower() not in ("false", "no"):
        raise ValueError("SaveConfig: remove the line (sandbox-ai never writes the configuration back)")
    mtu = None
    if "mtu" in interface:
        if not interface["mtu"].isdigit() or not 1280 <= int(interface["mtu"]) <= 9000:
            raise ValueError("MTU must be 1280-9000")
        mtu = int(interface["mtu"])
    listen_port = None
    if "listenport" in interface:
        if not interface["listenport"].isdigit() or not 1 <= int(interface["listenport"]) <= 65535:
            raise ValueError("ListenPort must be 1-65535")
        listen_port = int(interface["listenport"])
    result_peers = []
    for index, peer in enumerate(peers):
        where = f"[Peer] {index + 1}"
        for key in set(peer) - known_peer:
            raise ValueError(f"{where} {key}: not supported")
        if "publickey" not in peer:
            raise ValueError(f"{where} has no PublicKey")
        allowed = []
        for value in peer.get("allowedips", []):
            try:
                allowed.append(str(ipaddress.ip_network(value, strict=False)))
            except ValueError:
                raise ValueError(f"{where}: AllowedIPs {value!r} is not a network") from None
        if not allowed:
            raise ValueError(f"{where} has no AllowedIPs")
        keepalive = peer.get("persistentkeepalive", "")
        if keepalive.lower() == "off":
            keepalive = ""
        if keepalive and (not keepalive.isdigit() or not 0 <= int(keepalive) <= 65535):
            raise ValueError(f"{where}: PersistentKeepalive must be 0-65535 or off")
        if "presharedkey" in peer:
            secrets_["preshared_keys"][str(index)] = wg_key(peer["presharedkey"], f"{where} PresharedKey")
        result_peers.append({"public_key": wg_key(peer["publickey"], f"{where} PublicKey"), "allowed_ips": allowed,
                             "endpoint": parse_endpoint(peer["endpoint"], f"{where} Endpoint")
                             if peer.get("endpoint") else None,
                             "keepalive": int(keepalive) if keepalive else None,
                             "preshared": "presharedkey" in peer})
    if not any(peer["endpoint"] for peer in result_peers):
        raise ValueError("no [Peer] has an Endpoint: nothing to connect to")
    families = {ipaddress.ip_interface(a).version for a in addresses}
    allowed_all = [n for peer in result_peers for n in peer["allowed_ips"]]
    if 6 not in families and any(ipaddress.ip_network(n).version == 6 for n in allowed_all):
        notes.append("AllowedIPs name IPv6 networks but the tunnel has no IPv6 address: IPv6 is blocked")
    return ({"addresses": addresses, "dns": dns, "mtu": mtu, "listen_port": listen_port, "peers": result_peers},
            secrets_, notes)


# Curve25519 (RFC 7748) for the public key of a private one, and new keys for Mullvad, without any library.
P25519 = 2 ** 255 - 19


def x25519(scalar, point):
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k = int.from_bytes(k, "little")
    u = bytearray(point)
    u[31] &= 127
    x1 = int.from_bytes(u, "little")
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % P25519, (x2 - z2) % P25519
        aa, bb = a * a % P25519, b * b % P25519
        e = (aa - bb) % P25519
        c, d = (x3 + z3) % P25519, (x3 - z3) % P25519
        da, cb = d * a % P25519, c * b % P25519
        x3, z3 = (da + cb) ** 2 % P25519, x1 * (da - cb) ** 2 % P25519
        x2, z2 = aa * bb % P25519, e * (aa + 121665 * e) % P25519
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, P25519 - 2, P25519) % P25519).to_bytes(32, "little")


def public_key(private_key):
    return base64.b64encode(x25519(base64.b64decode(private_key), (9).to_bytes(32, "little"))).decode()


def generate_private_key():
    key = bytearray(secrets.token_bytes(32))
    key[0] &= 248
    key[31] = (key[31] & 127) | 64
    return base64.b64encode(bytes(key)).decode()


# --- OpenVPN --------------------------------------------------------------------------------------------------------

# Directives kept as they are (values checked by OpenVPN itself); everything not named here or below is refused.
OVPN_KEEP = {
    "client", "tls-client", "pull", "nobind", "float", "remote-random", "persist-key", "persist-tun", "connect-retry",
    "connect-retry-max", "connect-timeout", "server-poll-timeout", "explicit-exit-notify", "ping", "ping-restart",
    "ping-exit", "keepalive", "hand-window", "reneg-sec", "reneg-bytes", "reneg-pkts", "fast-io", "sndbuf", "rcvbuf",
    "tun-mtu", "tun-mtu-extra", "link-mtu", "mssfix", "fragment", "mtu-disc", "mute", "mute-replay-warnings",
    "replay-window", "cipher", "data-ciphers", "data-ciphers-fallback", "ncp-ciphers", "ncp-disable", "auth",
    "tls-cipher", "tls-ciphersuites", "tls-groups", "tls-version-min", "tls-version-max", "remote-cert-tls",
    "remote-cert-ku", "remote-cert-eku", "verify-x509-name", "ns-cert-type", "key-direction", "ecdh-curve",
    "tls-timeout", "tran-window", "auth-retry", "push-peer-info", "compress", "comp-lzo", "allow-compression",
    "redirect-gateway", "route", "route-ipv6", "route-delay", "route-metric", "route-nopull", "pull-filter",
    "block-ipv6", "topology", "ifconfig-nowarn", "allow-pull-fqdn", "resolv-retry", "tun-ipv6", "auth-nocache",
    "disable-dco", "peer-fingerprint"}
# Dropped with a note: Windows-only, or replaced by sandbox-ai's own setting.
OVPN_DROP = {"ip-win32", "route-method", "block-outside-dns", "register-dns", "setenv", "ignore-unknown-option",
             "dev", "dev-type", "verb", "mark", "dhcp-option", "proto", "port", "rport", "lport", "bind"}
OVPN_FILES = ("ca", "cert", "key", "tls-auth", "tls-crypt", "tls-crypt-v2", "pkcs12", "extra-certs", "crl-verify")
OVPN_REFUSED = {
    **{name: "runs a program" for name in (
        "up", "down", "route-up", "route-pre-down", "ipchange", "tls-verify", "auth-user-pass-verify",
        "client-connect", "client-disconnect", "learn-address", "plugin", "script-security", "dns-updown",
        "iproute", "up-delay", "up-restart", "down-pre")},
    **{name: "reads or writes files outside the configuration" for name in (
        "config", "cd", "chroot", "log", "log-append", "status", "writepid", "askpass", "secret", "tmp-dir",
        "dev-node", "auth-token-user", "management", "management-client-auth", "management-hold",
        "management-query-passwords", "daemon", "syslog", "user", "group", "mktun", "rmtun", "replay-persist")},
    **{name: "sends the tunnel through a proxy outside the kill switch's endpoints" for name in (
        "http-proxy", "http-proxy-option", "socks-proxy", "http-proxy-user-pass")}}
OVPN_PROTOS = {"udp": "udp", "udp4": "udp", "udp6": "udp", "tcp": "tcp-client", "tcp-client": "tcp-client",
               "tcp4": "tcp-client", "tcp6": "tcp-client", "tcp4-client": "tcp-client", "tcp6-client": "tcp-client"}


def ovpn_quote(value):
    return value if re.fullmatch(r"[A-Za-z0-9_.:/+=,@*!-]+", value) else \
        '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ovpn_file(directory, value, what):
    """The contents of a file a configuration names: only beside the configuration (or below it), so that an
    imported configuration cannot pick up any other file of yours."""
    if directory is None:
        raise ValueError(f"{what} {value}: put the file's contents inline (<{what}>...</{what}>)")
    base = Path(directory).resolve()
    path = (base / value).resolve()
    if base != path.parent and base not in path.parents:
        raise ValueError(f"{what} {value}: only files beside the configuration are read")
    if not path.is_file():
        raise ValueError(f"{what} {value}: no such file beside the configuration")
    data = path.read_bytes()
    if len(data) > 256 << 10:
        raise ValueError(f"{what} {value}: larger than 256 KiB")
    return data


def parse_openvpn(text, directory=None):
    """(configuration, info, credentials, notes) of an OpenVPN client configuration. Only a fixed set of directives
    passes; whatever runs programs, reads or writes other files, or detours the tunnel is refused. Files the
    configuration names (ca, cert, key, tls-auth ...) are taken in as inline blocks; remotes are kept apart (the
    VPN container resolves them once and the kill switch allows exactly those); credentials from auth-user-pass
    come back separately for the secret store."""
    lines, blocks, remotes, notes, dns = [], {}, [], [], []
    port, proto, credentials, needs_auth, full_routes = "1194", "udp", None, False, False
    source = text.splitlines()
    index = 0
    while index < len(source):
        raw = source[index].strip()
        index += 1
        if not raw or raw[0] in "#;":
            continue
        block = re.fullmatch(r"<([a-z0-9-]+)>", raw)
        if block:
            tag, body = block[1], []
            while index < len(source) and source[index].strip() != f"</{tag}>":
                body.append(source[index])
                index += 1
            if index >= len(source):
                raise ValueError(f"<{tag}> is not closed")
            index += 1
            if tag == "auth-user-pass":
                credentials = [line.strip() for line in body if line.strip()][:2]
                needs_auth = True
                continue
            if tag == "connection":
                raise ValueError("<connection> blocks are not supported; use plain remote lines")
            if tag not in OVPN_FILES:
                raise ValueError(f"<{tag}> is not supported")
            blocks[tag] = "\n".join(body)
            continue
        try:
            words = shlex.split(raw, comments=False, posix=True)
        except ValueError:
            raise ValueError(f"cannot read the line {raw[:80]!r}") from None
        name, args = words[0].lower().removeprefix("--"), words[1:]
        if name in OVPN_REFUSED:
            raise ValueError(f"{name}: not allowed, it {OVPN_REFUSED[name]}")
        if name == "remote":
            if not args or len(args) > 3:
                raise ValueError("remote HOST [PORT] [PROTO]")
            remotes.append(args)
        elif name in ("port", "rport") and args:
            port = args[0]
        elif name == "proto" and args:
            proto = args[0].lower()
        elif name in ("dev", "dev-type") and args and not args[0].lower().startswith("tun"):
            raise ValueError(f"{name} {args[0]}: only tun devices are supported (no layer 2)")
        elif name == "dhcp-option":
            if len(args) == 2 and args[0].upper() in ("DNS", "DNS6"):
                with contextlib.suppress(ValueError):
                    dns.append(str(ipaddress.ip_address(args[1])))
        elif name == "auth-user-pass":
            needs_auth = True
            if args:
                found = ovpn_file(directory, args[0], name).decode("utf-8", "replace").splitlines()
                credentials = [line.strip() for line in found if line.strip()][:2]
        elif name in OVPN_FILES:
            if not args:
                raise ValueError(f"{name} needs a file")
            if args[0] != "[inline]":
                if name == "crl-verify" and args[1:2] == ["dir"]:
                    raise ValueError("crl-verify DIR dir: put the CRL inline")
                data = ovpn_file(directory, args[0], name)
                blocks[name] = base64.b64encode(data).decode() if name == "pkcs12" else data.decode("utf-8", "replace")
            if name == "tls-auth" and len(args) > 1:
                lines.append(f"key-direction {args[1]}")
        elif name in OVPN_DROP:
            if name not in ("dev", "dev-type", "proto", "port", "rport"):
                notes.append(f"{name}: left out (sandbox-ai sets it itself or it is for Windows)")
            if name == "verb" and args and args[0].isdigit():
                lines.append(f"verb {min(int(args[0]), 4)}")  # more would log packet contents
        elif name in OVPN_KEEP:
            if name == "redirect-gateway":
                full_routes = True
            if any("\n" in a or "\0" in a for a in args):
                raise ValueError(f"{name}: invalid value")
            lines.append(" ".join([name, *map(ovpn_quote, args)]))
        else:
            raise ValueError(f"{name}: not supported by sandbox-ai's OpenVPN import")
    if not remotes:
        raise ValueError("the configuration has no remote")
    if len(remotes) > 16:
        raise ValueError("more than 16 remotes")
    parsed = []
    for args in remotes:
        host = args[0]
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            if not HOST.fullmatch(host):
                raise ValueError(f"remote {host}: invalid host") from None
        remote_port = args[1] if len(args) > 1 else port
        if not remote_port.isdigit() or not 1 <= int(remote_port) <= 65535:
            raise ValueError(f"remote {host}: invalid port {remote_port}")
        remote_proto = (args[2] if len(args) > 2 else proto).lower()
        if remote_proto not in OVPN_PROTOS:
            raise ValueError(f"remote {host}: protocol {remote_proto} is not supported")
        parsed.append({"host": host, "port": int(remote_port), "proto": OVPN_PROTOS[remote_proto]})
    if "ca" not in blocks and "pkcs12" not in blocks and "peer-fingerprint" not in " ".join(lines):
        raise ValueError("the configuration has no CA (ca or <ca>): the server could not be verified")
    for tag, body in blocks.items():
        lines += [f"<{tag}>", body.strip("\n"), f"</{tag}>"]
    info = {"remotes": parsed, "auth": needs_auth, "dns": list(dict.fromkeys(dns)), "pushed_routes": full_routes}
    return "\n".join(lines) + "\n", info, credentials, notes


# --- Mullvad ---------------------------------------------------------------------------------------------------------

class Mullvad:
    """Mullvad's API, as its app uses it: an access token for the account number (auth/v1/token), a device for a
    WireGuard public key (accounts/v1/devices) and the relay list (app/v1/relays, else the public WireGuard list).
    The account number is sent to Mullvad only; it is never part of a message, log or file here."""

    def __init__(self, base=MULLVAD_API, context=None, timeout=30):
        self.base, self.context, self.timeout = base.rstrip("/"), context, timeout

    def request(self, method, path, body=None, token=None):
        headers = {"Accept": "application/json", "User-Agent": "sandbox-ai"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{self.base}/{path}", data=json.dumps(body).encode() if body is not None
                                         else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                status, data = response.status, response.read(8 << 20)
        except urllib.error.HTTPError as exc:
            status, data = exc.code, exc.read(64 << 10)
        except (urllib.error.URLError, OSError) as exc:
            raise ValueError(f"Mullvad's API is not reachable: {getattr(exc, 'reason', exc)}") from None
        try:
            return status, json.loads(data) if data else None
        except ValueError:
            return status, None

    @staticmethod
    def problem(status, document, action):
        code = document.get("code") if isinstance(document, dict) else None
        reasons = {"INVALID_ACCOUNT": "the account number is not valid",
                   "MAX_DEVICES_REACHED": "the account has the maximum number of devices; remove one at "
                                          "https://mullvad.net/account/devices",
                   "TOO_MANY_REQUESTS": "too many requests; wait a minute",
                   "INVALID_ACCESS_TOKEN": "the login expired"}
        reason = reasons.get(code) or (f"{code}" if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,64}", code)
                                       else f"HTTP {status}")
        return f"Mullvad: could not {action}: {reason}"

    def token(self, account):
        status, document = self.request("POST", "auth/v1/token", {"account_number": account})
        token = document.get("access_token") if isinstance(document, dict) else None
        if status != 200 or not isinstance(token, str):
            raise ValueError(self.problem(status, document, "log in"))
        return token

    def create_device(self, token, key):
        status, document = self.request("POST", "accounts/v1/devices", {"pubkey": key, "hijack_dns": False}, token)
        if status not in (200, 201) or not isinstance(document, dict):
            raise ValueError(self.problem(status, document, "register the key"))
        addresses = []
        for field in ("ipv4_address", "ipv6_address"):
            value = document.get(field)
            if isinstance(value, str) and value:
                addresses.append(str(ipaddress.ip_interface(value)))
        if not addresses:
            raise ValueError("Mullvad: the device has no tunnel address")
        device_id = document.get("id") if isinstance(document.get("id"), str) else None
        return {"id": device_id, "name": str(document.get("name") or ""), "addresses": addresses}

    def delete_device(self, token, device_id):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", device_id or ""):
            raise ValueError("Mullvad: no device to remove")
        status, document = self.request("DELETE", f"accounts/v1/devices/{device_id}", token=token)
        if status not in (200, 204, 404):
            raise ValueError(self.problem(status, document, "remove the device"))

    def relays(self):
        status, document = self.request("GET", "app/v1/relays")
        relays = mullvad_relays(document) if status == 200 else ([], {})
        if not relays[0]:
            status, document = self.request("GET", "public/relays/wireguard/v1/")
            relays = mullvad_relays(document) if status == 200 else ([], {})
        if not relays[0]:
            raise ValueError("Mullvad: the relay list could not be read")
        return relays


def mullvad_relays(document):
    """([{hostname, location, public_key, ipv4, ipv6}], {"ipv4": gateway, "ipv6": gateway}) of the active WireGuard
    relays, from the app's list (wireguard.relays) or the public one (countries[].cities[].relays[])."""
    found, gateways = [], {}

    def add(relay, location):
        if not isinstance(relay, dict) or relay.get("active") is False:
            return
        try:
            entry = {"hostname": str(relay["hostname"]), "location": location,
                     "public_key": wg_key(str(relay["public_key"]), "relay key"),
                     "ipv4": str(ipaddress.IPv4Address(relay["ipv4_addr_in"])),
                     "ipv6": str(ipaddress.IPv6Address(relay["ipv6_addr_in"])) if relay.get("ipv6_addr_in") else None}
        except (KeyError, ValueError, TypeError):
            return
        if HOST.fullmatch(entry["hostname"]):
            found.append(entry)

    if not isinstance(document, dict):
        return found, gateways
    wireguard = document.get("wireguard")
    if isinstance(wireguard, dict) and isinstance(wireguard.get("relays"), list):
        for relay in wireguard["relays"]:
            location = relay.get("location") if isinstance(relay, dict) else None
            add(relay, str(location) if isinstance(location, str) else relay_location(relay))
        for family in ("ipv4", "ipv6"):
            with contextlib.suppress(ValueError, TypeError):
                gateways[family] = str(ipaddress.ip_address(wireguard.get(f"{family}_gateway")))
    for country in document.get("countries") or []:
        for city in (country.get("cities") or []) if isinstance(country, dict) else []:
            for relay in (city.get("relays") or []) if isinstance(city, dict) else []:
                codes = (country.get("code"), city.get("code"))
                add(relay, f"{codes[0]}-{codes[1]}" if all(isinstance(c, str) for c in codes) else relay_location(relay))
    return found, gateways


def relay_location(relay):
    """se-sto of se-sto-wg-001."""
    hostname = str(relay.get("hostname", "")) if isinstance(relay, dict) else ""
    return "-".join(hostname.split("-")[:2])


def choose_relay(relays, location=None, choice=secrets.SystemRandom().choice):
    wanted = (location or "").lower()
    matching = [r for r in relays if not wanted or r["hostname"].lower() == wanted
                or r["location"].lower() == wanted or r["location"].lower().startswith(wanted + "-")]
    if not matching:
        known = sorted({r["location"] for r in relays})
        raise ValueError(f"Mullvad has no WireGuard relay at {location}; locations: {', '.join(known[:40])}"
                         + (" ..." if len(known) > 40 else ""))
    return choice(matching)


def read_account(secret=None, prompt=getpass.getpass):
    """The Mullvad account number: from sandbox-ai secrets or typed in (not echoed). Never printed."""
    value = secret_store().get(secret) if secret else prompt("Mullvad account number (not shown): ")
    if value is None:
        raise ValueError(f"no secret {secret}; keep the account number with: sandbox-ai secrets set {secret}")
    value = re.sub(r"\s", "", value)
    if not re.fullmatch(r"[0-9]{16}", value):
        raise ValueError("a Mullvad account number has 16 digits")
    return value


# --- the checks ------------------------------------------------------------------------------------------------------

def check_settings(profile):
    """What the checks ask: the echo services (the profile's, else ~/.config/sandbox-ai/vpn/checks.json, else the
    defaults: Mullvad's own first for Mullvad), the DNS probe and the timeout."""
    configured = {}
    path = profiles_dir() / "checks.json"
    if path.exists():
        try:
            configured = json.loads(read_private(path, 64 << 10))
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from None
        if not isinstance(configured, dict) or set(configured) - {"urls", "dns_probe", "timeout"}:
            raise ValueError(f"{path}: expected {{\"urls\": [...], \"dns_probe\": NAME or null, \"timeout\": SECONDS}}")
    own = profile.get("checks") or {}
    tor = profile.get("type") == "tor"
    # Tor: the Tor Project's own check, which says whether the address is a Tor exit; over Tor more time.
    defaults = [runtime().TOR_CHECK] if tor else ECHO_MULLVAD + ECHO_GENERIC if profile.get("provider") == "mullvad" \
        else ECHO_GENERIC
    urls = own.get("urls") or (None if tor else configured.get("urls")) or defaults
    for url in urls:
        check_url(url)
    probe = own["dns_probe"] if "dns_probe" in own else configured.get("dns_probe", DNS_PROBE)
    timeout = configured.get("timeout", 30 if tor else 8)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise ValueError("checks: timeout is 1-60 seconds")
    return {"urls": list(urls), "dns_probe": probe, "timeout": timeout}


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not HOST.fullmatch(parts.hostname) or parts.username:
        raise ValueError(f"check URL {url!r}: an https URL with a host name")
    return url


def host_baseline(settings, resolve=None, get=None):
    """This host's own public addresses (IPv4, IPv6) and the egress of its resolver, asked outside every sandbox."""
    rt = runtime()
    resolve, get = resolve or rt.system_resolver, get or rt.fetch
    result = {}
    for family in (4, 6):
        result[f"ipv{family}"] = rt.echo(settings["urls"], family, resolve, get, settings["timeout"]).get("ip")
    if settings.get("dns_probe"):
        try:
            result["dns_egress"] = resolve(settings["dns_probe"], 4)
        except (OSError, ValueError):
            result["dns_egress"] = []
    return result


def evaluate(profile, tunnel, inside, host):
    """{"failures", "warnings", "notes"} of the checks from inside the namespace against this host's own view."""
    failures, warnings, notes = [], [], []
    tailnet = profile["type"] == "tailscale" and not profile["tailscale"].get("exit_node")
    tor = profile["type"] == "tor"
    provider = profile.get("provider") or "the provider"
    if not tailnet:
        for version in (4, 6):
            got, own = inside.get(f"ipv{version}") or {}, host.get(f"ipv{version}")
            carries, full = tunnel.get(f"ipv{version}"), tunnel.get(f"full{version}")
            label = f"IPv{version}"
            if "ip" in got:
                seen = urllib.parse.urlsplit(got.get("url", "")).hostname or "the echo service"
                if own and got["ip"] == own:
                    failures.append(f"{label}: the internet sees this host's own address {own}: traffic leaves past "
                                    "the VPN")
                elif not carries:
                    failures.append(f"{label}: {got['ip']} answered although the VPN carries no {label}: traffic "
                                    "leaves past the VPN")
                elif got.get("vpn") is False:
                    failures.append(f"{label}: the Tor Project says {got['ip']} is not a Tor exit" if tor else
                                    f"{label}: {provider} says {got['ip']} is not one of its VPN exits")
                elif tor and got.get("vpn") is not True:
                    failures.append(f"{label}: {seen} did not confirm that {got['ip']} is a Tor exit (no "
                                    '"IsTor": true)')
                else:
                    confirmed = " (a Tor exit, says the Tor Project)" if tor else \
                        " (the provider confirms the VPN)" if got.get("vpn") else ""
                    notes.append(f"{label}: {got['ip']} seen by {seen}{confirmed}")
                    if not own and not got.get("vpn"):
                        warnings.append(f"{label}: this host's own public address is unknown, so {got['ip']} could not "
                                        "be compared with it")
            elif got:
                error, kind = got.get("error", "no answer"), got.get("kind")
                if carries and full:
                    (failures if version == 4 else warnings).append(f"{label}: no connection through the VPN ({error})")
                elif kind == "blocked":
                    notes.append(f"{label}: blocked ({'the VPN carries none' if not carries else 'the echo services lie outside the tunnel'})")
                else:
                    warnings.append(f"{label}: could not be tested ({error})")
    dns = inside.get("dns") or {}
    interface = tunnel.get("interface")
    if not dns.get("configured"):
        warnings.append("DNS: the profile names no resolver; --dns would use "
                        f"{', '.join(runtime().FALLBACK_DNS)} through the tunnel")
    for server, device in (dns.get("routes") or {}).items():
        if device != interface:
            failures.append(f"DNS: the resolver {server} is reached over {device or 'no route'}, not through the "
                            f"tunnel {interface}")
        answer = (dns.get("answers") or {}).get(server)
        if answer is not True:
            failures.append(f"DNS: the resolver {server} does not answer through the tunnel ({answer})")
    if tor:
        failures += tor_dns_failures(dns)
    egress = dns.get("egress") or []
    if egress:
        own = {host.get("ipv4"), host.get("ipv6")} - {None}
        if own & set(egress) and not tailnet:
            (failures if tunnel.get("full4") else warnings).append(
                f"DNS leak: lookups through the VPN's resolver reach the internet from this host's own address "
                f"{', '.join(sorted(own & set(egress)))}")
        elif dns.get("configured") and set(egress) & set(host.get("dns_egress") or []) and not tailnet:
            warnings.append(f"DNS: the VPN's resolver reaches the internet from {', '.join(egress)}, like this host's "
                            "own resolver (does it forward to it?)")
        else:
            notes.append(f"DNS: lookups reach the internet from {', '.join(egress)}")
    elif dns.get("egress_error"):
        notes.append(f"DNS: the leak probe found nothing ({dns['egress_error']})")
    return {"failures": failures, "warnings": warnings, "notes": notes}


def tor_dns_failures(dns):
    """Tor's resolver checks: its DNSPort answers, maps an .onion name into the virtual network, and a lookup sent
    to a resolver outside reaches it as well (the redirect): an outside resolver has no answer for .onion."""
    rt = runtime()
    failures = []
    for server in dns.get("servers") or [rt.TOR_DNS]:
        answer = (dns.get("answers") or {}).get(server)
        if answer is not True:
            failures.append(f"DNS: the resolver {server} does not answer (tor's DNSPort; {answer})")
    network = ipaddress.ip_network(rt.TOR_VIRTUAL_NET)
    onion = dns.get("onion") or {}
    for where, what in (("tor", f"tor's DNSPort {rt.TOR_DNS}"),
                        ("outside", f"a lookup sent to {rt.OUTSIDE_RESOLVER} (it must reach tor's DNSPort too)")):
        got = onion.get(where)
        mapped = isinstance(got, list) and bool(got) and all(
            ipaddress.ip_address(address) in network for address in got)
        if not mapped:
            failures.append(f"DNS: {what} did not map {rt.ONION_PROBE[:16]}....onion into {network} "
                            f"({', '.join(got) if isinstance(got, list) and got else got or 'no answer'})")
    return failures


def endpoint_text(endpoint):
    host = endpoint["host"]
    return f"[{host}]:{endpoint['port']}" if ":" in host else f"{host}:{endpoint['port']}"


def describe(profile):
    """Short facts of a profile, for list, show, the plan and the logs; never a secret."""
    kind = profile["type"]
    facts = {"profile": profile["name"], "type": kind, "provider": profile.get("provider") or "generic",
             "dns": list(profile.get("dns") or [])}
    if kind == "wireguard":
        wireguard = profile["wireguard"]
        allowed = [n for peer in wireguard["peers"] for n in peer["allowed_ips"]]
        families = {ipaddress.ip_interface(a).version for a in wireguard["addresses"]}
        full4 = 4 in families and runtime().covers_all(allowed, 4)
        facts.update({"interface": "wg0 (the kernel's WireGuard, else wireguard-go)",
                      "endpoints": [endpoint_text(p["endpoint"]) for p in wireguard["peers"] if p.get("endpoint")],
                      "addresses": wireguard["addresses"], "public_key": profile.get("public_key"),
                      "tunnel": "full" if full4 else "split", "ipv6": 6 in families,
                      "allowed_ips": allowed})
    elif kind == "openvpn":
        openvpn = profile["openvpn"]
        facts.update({"interface": "tun0 (OpenVPN)", "endpoints": [f"{r['host']}:{r['port']}/{r['proto']}"
                                                                   for r in openvpn["remotes"]],
                      "tunnel": "full" if openvpn.get("full", True) else "split (the server's routes)",
                      "ipv6": "if the server gives the tunnel an IPv6 address"})
        if not facts["dns"]:
            facts["dns"] = "pushed by the server"
    elif kind == "tor":
        rt, tor = runtime(), profile["tor"]
        count = int(tor.get("bridges") or 0)
        kinds = ", ".join(tor.get("transports") or []) or "plain"
        facts.update({"interface": f"none: tor's TransPort 127.0.0.1:{rt.TOR_TRANS_PORT} and DNSPort {rt.TOR_DNS}:53",
                      "endpoints": [f"{count} bridge{'s' if count != 1 else ''} ({kinds})"] if count
                      else ["the Tor network"],
                      "tunnel": "transparent: every TCP connection through Tor; UDP other than DNS dropped, IPv6 "
                                "blocked", "transparent": True, "ipv6": False, "dns": [rt.TOR_DNS],
                      "socks": f"127.0.0.1:{rt.TOR_SOCKS_PORT} (socks5h; what goes through it passes the connection "
                               "proxy by)" if tor.get("socks") else "off",
                      "onion": f".onion names resolve into {rt.TOR_VIRTUAL_NET} and connect through Tor",
                      "bridges": count, "exit_country": list(tor.get("exit_country") or []) or "any"})
    else:
        tailscale = profile["tailscale"]
        facts.update({"interface": "tailscale0 (tailscaled)",
                      "endpoints": [tailscale.get("login_server") or "the Tailscale coordination server"],
                      "tunnel": f"exit node {tailscale['exit_node']}" if tailscale.get("exit_node") else
                      "tailnet only (other connections follow the sandbox's own rules)",
                      "tailnet_allow": tailscale.get("tailnet_allow", []), "ephemeral": tailscale.get("ephemeral", False),
                      "dns": list(profile.get("dns") or [runtime().QUAD100])})
    return facts


# --- the VPN container ------------------------------------------------------------------------------------------

def payload(profile, action, names=()):
    """The VPN container's standard input: the profile, its secrets, the names to resolve, the checks."""
    store, kind, values = secret_store(), profile["type"], {}

    def secret(name, what):
        value = store.get(name)
        if value is None:
            raise ValueError(f"VPN profile {profile['name']}: the secret {name} ({what}) is missing; add the profile "
                             "again (sandbox-ai vpn add ...) or keep it with: sandbox-ai secrets set " + name)
        return value

    if kind == "wireguard":
        values["private_key"] = secret(profile["wireguard"]["private_key_secret"], "the private key")
        values["preshared_keys"] = {index: secret(name, "a preshared key")
                                    for index, name in (profile["wireguard"].get("preshared_key_secrets") or {}).items()}
    elif kind == "openvpn":
        openvpn = profile["openvpn"]
        values["openvpn_config"] = read_private(profiles_dir() / openvpn["config_file"])
        if openvpn.get("auth_secret"):
            user, sep, password = secret(openvpn["auth_secret"], "user name and password").partition(":")
            if not sep or not user:
                raise ValueError(f"the secret {openvpn['auth_secret']} must hold USER:PASSWORD")
            values["openvpn_auth"] = f"{user}\n{password}"
        elif openvpn.get("auth"):
            raise ValueError(f"VPN profile {profile['name']} needs a user name and password: add it again with "
                             "--auth-secret SECRET")
    elif kind == "tor":
        if profile["tor"].get("bridges_secret"):
            values["tor_bridges"] = secret(profile["tor"]["bridges_secret"], "the bridge lines")
    else:
        if profile["tailscale"].get("auth_key_secret"):
            values["tailscale_auth_key"] = secret(profile["tailscale"]["auth_key_secret"], "the auth key")
    public = {key: value for key, value in profile.items() if key not in ("created",)}
    return {"version": 1, "action": action, "profile": public, "secrets": values, "resolve": list(names),
            "checks": check_settings(profile)}


def container_command(podman, name, profile, action="serve", pod=None, network=None, labels=()):
    """podman run for the VPN container: only NET_ADMIN (NET_RAW too for tailscaled) and /dev/net/tun, a read-only
    image, nothing of the host but the runtime script and (Tailscale) the profile's own state. No secret is part of
    it: they follow on standard input. Tor needs no tun device; it binds port 53 (NET_BIND_SERVICE) as root and
    then becomes debian-tor (SETUID, SETGID), whose data directory is a tmpfs of its own."""
    tor = profile["type"] == "tor"
    command = [*podman, "run", "--rm", "-i", "--pull=never", "--name", name, "--user=0:0", "--read-only",
               "--cap-drop=ALL", "--cap-add=NET_ADMIN", *([] if tor else ["--device=/dev/net/tun"]),
               "--security-opt=no-new-privileges",
               "--log-driver=none", "--tmpfs=/run:rw,nosuid,nodev,mode=0755", "--tmpfs=/tmp:rw,nosuid,nodev"]
    if tor:
        uid = runtime().TOR_UID
        command += ["--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=NET_BIND_SERVICE",
                    f"--tmpfs=/var/lib/tor:rw,nosuid,nodev,mode=0700,uid={uid},gid={uid}"]
    if profile["type"] == "tailscale":
        command.append("--cap-add=NET_RAW")
        if profile["tailscale"].get("ephemeral"):
            command.append("--tmpfs=/var/lib/tailscale:rw,nosuid,nodev,mode=0700")
        else:
            command += ["--mount", f"type=bind,src={tailscale_state(profile)},dst=/var/lib/tailscale,rw"]
    command += ["--pod", pod] if pod else ["--network", network or "none"]
    for label in labels:
        command += ["--label", label]
    command += ["--mount", f"type=bind,src={HERE / 'vpn_runtime.py'},dst=/vpn/vpn_runtime.py,ro",
                "--entrypoint=/usr/bin/python3", VPN_IMAGE, "-I", "/vpn/vpn_runtime.py", action]
    return command


def podman_run(command, **kwargs):
    """Every podman call but the VPN container itself (tests record them)."""
    return subprocess.run(command, stdin=subprocess.DEVNULL, **kwargs)


def tailscale_state(profile):
    return state_dir(profile["name"]) / "tailscale"


@contextlib.contextmanager
def profile_lock(profile):
    """A persistent Tailscale node can run once at a time: its state (node key) is one."""
    if profile["type"] != "tailscale" or profile["tailscale"].get("ephemeral"):
        yield
        return
    directory = private_dir(tailscale_state(profile))
    fd = os.open(directory.parent / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"VPN profile {profile['name']} is in use by another sandbox; a Tailscale node runs once "
                             "at a time (add an ephemeral profile for parallel use: vpn add ... --ephemeral)") from None
        yield
    finally:
        os.close(fd)


class Tunnel:
    """A running VPN container: the payload goes to its standard input, which stays open as long as the tunnel
    should live; events come back on its standard output."""

    def __init__(self, command, data, say=None):
        self.say = say or (lambda text: print(f"sandbox-ai vpn: {text}", file=sys.stderr))
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)
        self.buffer = b""
        try:
            self.process.stdin.write(json.dumps(data).encode() + b"\n")
            self.process.stdin.flush()
        except BrokenPipeError:
            pass  # wait_ready reports why it ended

    def next_event(self, deadline):
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("the VPN did not come up in time")
            if select.select([self.process.stdout], [], [], min(remaining, 1))[0]:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    return None
                self.buffer += chunk
            elif self.process.poll() is not None and not select.select([self.process.stdout], [], [], 0)[0]:
                return None
        line, _, self.buffer = self.buffer.partition(b"\n")
        try:
            event = json.loads(line)
        except ValueError:
            return {"event": "progress", "message": line.decode("utf-8", "replace")[:300]}
        return event if isinstance(event, dict) else {"event": "progress", "message": str(event)[:300]}

    def wait_ready(self, timeout=READY_TIMEOUT):
        deadline = time.monotonic() + timeout
        while True:
            event = self.next_event(deadline)
            if event is None:
                self.process.wait(timeout=10)
                self.errors.seek(0)
                tail = runtime().redact(self.errors.read()[-2000:].decode("utf-8", "replace").strip())
                hint = " (build the image: sandbox-ai build vpn)" if "image not known" in tail or "no such image" in \
                    tail.lower() else ""
                raise ValueError(f"the VPN container ended before the tunnel was up{hint}: {tail or 'no message'}")
            kind = event.get("event")
            if kind == "ready":
                return event
            if kind == "error":
                raise ValueError(f"the VPN did not come up: {event.get('message')}")
            if kind == "login":
                self.say(f"log the Tailscale node in: {event.get('url')}")
                deadline = max(deadline, time.monotonic() + LOGIN_TIMEOUT)
            elif kind == "progress":
                self.say(str(event.get("message", "")))

    def follow(self, on_event):
        """Report what the container says after it was ready (a tunnel that went down)."""
        def read():
            while True:
                event = self.next_event(time.monotonic() + 10 ** 9)
                if event is None:
                    return
                on_event(event)
        threading.Thread(target=read, daemon=True).start()

    def close(self):
        if self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=10)
        self.errors.close()


def in_background(function):
    """FUNCTION started in a thread; the returned getter waits for it (or runs it when no thread ran)."""
    box = {}

    def work():
        try:
            box["value"] = function()
        except Exception as exc:  # noqa: BLE001 - handed to the getter
            box["error"] = exc
    thread = threading.Thread(target=work, daemon=True)
    thread.start()

    def result():
        thread.join(120)
        if not box:
            work()
        if "error" in box:
            raise box["error"]
        return box["value"]
    return result


def report(profile, ready, verdict, say):
    tunnel = ready.get("tunnel") or {}
    endpoints = ", ".join(tunnel.get("endpoints") or []) or "-"
    tailnet = profile["type"] == "tailscale" and not profile["tailscale"].get("exit_node")
    say(f"{profile['name']}: {profile['type']} ({tunnel.get('implementation', '?')}, {tunnel.get('interface')}) "
        f"is up; endpoints {endpoints}; " + ("only the tailnet goes through it" if tailnet else "kill switch on"))
    for line in verdict["notes"]:
        say(f"check: {line}")
    for line in verdict["warnings"]:
        say(f"warning: {line}")
    for line in verdict["failures"]:
        say(f"FAILED: {line}")


def log_record(directory, profile, ready, host, verdict, run=None):
    """One line in DIRECTORY/vpn.jsonl (the run's log directory, hidden from the agent): what was checked and
    seen, never a key."""
    if not directory:
        return
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run": run, **describe(profile),
              "tunnel": ready.get("tunnel"), "inside": ready.get("checks"), "host": host, **verdict}
    path = Path(directory).expanduser() / "vpn.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    with contextlib.suppress(OSError):
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(record) + "\n")


def mark_expression(profile):
    rt = runtime()
    if profile["type"] == "tailscale":
        return rt.mark_match(*rt.TAILSCALE_MARK)
    if profile["type"] == "tor":
        return f"meta skuid {int(rt.TOR_UID)}"  # tor cannot mark its sockets; its user is its own
    return rt.mark_match(rt.MARK)


def ready_timeout(profile):
    """How long the launcher waits for the container's "ready": tor's bootstrap on top for Tor."""
    return READY_TIMEOUT + (runtime().BOOTSTRAP_TIMEOUT if profile["type"] == "tor" else 0)


def tor_agent_rules(dns_open, proxy_uid):
    """Lines for the top of the agent's output chain (before loopback is accepted) with a Tor profile: tor's
    TransPort takes only what tor's redirect handed it from the connection proxy (whose connections the proxy
    checked), never the agent directly; tor's DNSPort only when the start allows DNS (--dns, --egress-all-allow)."""
    rt = runtime()
    rules = [f"ct status dnat ip daddr 127.0.0.1 tcp dport {rt.TOR_TRANS_PORT} meta skuid {int(proxy_uid)} accept",
             f"ip daddr 127.0.0.1 tcp dport {rt.TOR_TRANS_PORT} drop"]
    if not dns_open:
        rules.append(f"ip daddr {rt.TOR_DNS} drop")
    return rules


def bring_up(profile, command, names, say, action="serve", log_dir=None, run=None, warn_only=False):
    """Start the container, wait for the tunnel, run the checks: (tunnel, ready event, verdict). Raises when a
    check fails, unless WARN_ONLY."""
    data = payload(profile, action, names)
    tailnet = profile["type"] == "tailscale" and not profile["tailscale"].get("exit_node")
    baseline = in_background(lambda: {} if tailnet else host_baseline(data["checks"]))
    tunnel = Tunnel(command, data, say)
    try:
        ready = tunnel.wait_ready(timeout=ready_timeout(profile))
        host = baseline()
        verdict = evaluate(profile, ready.get("tunnel") or {}, ready.get("checks") or {}, host)
        report(profile, ready, verdict, say)
        log_record(log_dir, profile, ready, host, verdict, run)
        if verdict["failures"] and not warn_only:
            raise ValueError(f"VPN {profile['name']}: the checks failed, the start is stopped ("
                             + "; ".join(verdict["failures"]) + "). Start with --vpn-check warn to go on anyway.")
    except BaseException:
        tunnel.close()
        raise
    return tunnel, ready, verdict


# --- the launcher's hooks -----------------------------------------------------------------------------------------

def private_paths():
    """Where the VPNs' keys and state live: the profiles (OpenVPN configurations hold client keys), Tailscale's node
    state and the secret files."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai"
    return [profiles_dir(), data / "vpn", secret_store().files_dir()]


def check_paths(paths, option):
    """Once there are VPN profiles, no host path a sandbox gets (read-only mounts and copies included) may be, contain
    or lie in a place where their keys are."""
    if not list_profiles():
        return
    private = {q for p in private_paths() for q in (Path(os.path.normpath(p.expanduser().absolute())),
                                                     p.expanduser().resolve())}
    for raw in paths:
        source = Path(os.path.normpath(Path(raw).expanduser().absolute()))
        for candidate in {source, source.resolve()}:
            for place in private:
                if candidate == place or candidate in place.parents or place in candidate.parents:
                    raise ValueError(f"{option} {raw} would show the sandbox {place}, where the VPN profiles keep "
                                     "their keys; use a directory beside it")


def exposed_paths(a, l):
    """Every host path whose contents the agent gets: workspaces, mounts (read-only too), kits, copies, files whose
    contents become credentials or settings in its HOME, and a persistent HOME."""
    paths = list(a.workspaces) + [l.parse_mount(m)[0] for m in a.mount] + list(a.kit_dir)
    paths += [value.rsplit(":", 1)[0] for value in a.copy if ":" in value]
    paths += [l.git_token_credential(value, "file")["file"] for value in a.git_token_file]
    paths += [p for p in (a.local_config, a.auth_file, a.import_codex_auth, a.git_credentials, a.state_dir) if p]
    return paths


def validate(a):
    """Every start: no VPN key within the sandbox's reach. --vpn: the backend, the conflicts, the profile and what it
    adds to the options (Tailscale's networks)."""
    l = launcher()
    check_paths(exposed_paths(a, l), "Workspace, --mount, --copy or file option")
    if not getattr(a, "vpn", None):
        return
    check_name(a.vpn)
    # An LLM endpoint's VPN (llm.apply_vpn) is named with where it came from.
    option = f"--vpn {a.vpn} (from {a.vpn_origin})" if getattr(a, "vpn_origin", None) else "--vpn"
    if a.backend != "podman":
        raise ValueError(f"{option} needs the Podman backend: the tunnel runs in a container of the pod's network "
                         "namespace (Bubblewrap is not supported yet, see TODO.md)")
    if a.offline:
        raise ValueError(f"{option} needs network; it conflicts with --offline")
    if a.nameserver:
        raise ValueError("--nameserver conflicts with --vpn: lookups go to the VPN's resolvers; choose others in the "
                         f"profile: sandbox-ai vpn add {a.vpn} ... --dns IP --force")
    profile = load_profile(a.vpn)
    # The agent must not be able to change a profile (its endpoint, its DNS) or a node's state through a mount.
    l.check_host_source(a, profiles_dir(), "The VPN profiles (XDG_CONFIG_HOME)")
    if profile["type"] == "tailscale" and not profile["tailscale"].get("ephemeral"):
        l.check_host_source(a, tailscale_state(profile), "The VPN's Tailscale state (XDG_DATA_HOME)")
    a.vpn_profile = profile
    a.vpn_mark = mark_expression(profile)
    if profile["type"] == "tor":
        validate_tor(a, profile, option, l)
        return
    a.vpn_dns = list(profile.get("dns") or ([runtime().QUAD100] if profile["type"] == "tailscale" else []))
    if a.dns or a.egress_all_allow:
        for server in a.vpn_dns:
            if not l.within_ceiling(a, network=server):
                raise ValueError(f"The VPN's resolver {server} lies outside the networks the administrator's policy "
                                 "allows")
    if profile["type"] == "tailscale":
        for network in profile["tailscale"].get("tailnet_allow", []):
            if not l.within_ceiling(a, network=network):
                raise ValueError(f"The tailnet {network} lies outside the networks the administrator's policy allows")
            if network not in a.allow:
                a.allow.append(network)


def validate_tor(a, profile, option, l):
    """A Tor profile: the connection proxy hands the agent's connections to tor (no --no-proxy), tor's ports stay
    its own, lookups go to its DNSPort on loopback, .onion's virtual network is allowed where the policy allows it,
    and the agent's firewall gets tor's rules."""
    rt = runtime()
    if getattr(a, "no_proxy", False):
        raise ValueError(f"{option} {a.vpn} is a Tor profile, which needs the connection proxy: it hands the agent's "
                         "connections to Tor's TransPort; drop --no-proxy")
    ports = {rt.TOR_TRANS_PORT, *([rt.TOR_SOCKS_PORT] if profile["tor"].get("socks") else [])}
    for port in sorted(ports & set(l.host_ports(a))):
        raise ValueError(f"--allow-host-port {port}: Tor listens there in the sandbox ({option} {a.vpn})")
    a.vpn_dns = [rt.TOR_DNS]  # on loopback, inside the sandbox: no network to hold against the policy
    a.vpn_firewall = tor_agent_rules(bool(a.dns or a.egress_all_allow), l.PROXY_UID)
    if l.within_ceiling(a, network=rt.TOR_VIRTUAL_NET) and rt.TOR_VIRTUAL_NET not in a.allow:
        a.allow.append(rt.TOR_VIRTUAL_NET)


def check_policy(a, policy, where=""):
    """require_vpn: true (every sandbox with network), or a list of the profiles allowed."""
    required = policy.get("require_vpn", False)
    if not required or a.offline:
        return
    if not getattr(a, "vpn", None):
        raise ValueError(f"The administrator's policy requires a VPN{where}: start with --vpn PROFILE "
                         "(see sandbox-ai vpn list)")
    if isinstance(required, list) and a.vpn not in required:
        raise ValueError(f"--vpn {a.vpn} is not among the VPN profiles the administrator's policy allows "
                         f"({', '.join(required)}){where}")
    if getattr(a, "vpn_check", "stop") != "stop":
        raise ValueError(f"--vpn-check {a.vpn_check}: the administrator's policy requires a VPN, so a failed check "
                         f"stops the start{where}")


def policy_value_ok(value):
    return isinstance(value, bool) or (isinstance(value, list) and bool(value) and all(
        isinstance(name, str) and re.fullmatch(PROFILE_NAME, name) for name in value))


def plan(a):
    """The dry-run plan's "vpn": profile, endpoints, interface, tunnel and checks; no key, no secret's name."""
    if not getattr(a, "vpn", None):
        return None
    profile = a.vpn_profile
    settings = check_settings(profile)
    tailnet = profile["type"] == "tailscale" and not profile["tailscale"].get("exit_node")
    if profile["type"] == "tor":
        rt = runtime()
        return {**describe(profile), "image": VPN_IMAGE,
                "kill_switch": f"only tor's own connections leave (user {rt.TOR_USER}, meta skuid {rt.TOR_UID}), over "
                               "IPv4 TCP" + (" to its bridges" if profile["tor"].get("bridges") else "") +
                               "; every other TCP connection goes to its TransPort, port 53 to its DNSPort; other "
                               "UDP and all IPv6 are dropped",
                "checks": {"on_failure": a.vpn_check, "ip_echo": settings["urls"],
                           "tor": f"bootstrapped to 100% within {rt.BOOTSTRAP_TIMEOUT} s; the address seen outside "
                                  'is a Tor exit ("IsTor": true)',
                           "dns_probe": settings["dns_probe"],
                           "dns_route": f"tor's DNSPort {rt.TOR_DNS}; a lookup sent to {rt.OUTSIDE_RESOLVER} must "
                                        f"reach it too (an .onion name maps into {rt.TOR_VIRTUAL_NET})"}}
    return {**describe(profile), "image": VPN_IMAGE,
            "kill_switch": "none: only the tailnet goes through the tunnel" if tailnet else
            "only the tunnel and its own packets to its endpoints leave, IPv4 and IPv6",
            "checks": {"on_failure": a.vpn_check, "ip_echo": [] if tailnet else settings["urls"],
                       "dns_probe": settings["dns_probe"], "dns_route": "each resolver through the tunnel"}}


def agent_up(a, session, home, names, cleanup):
    """The VPN for an agent's pod: create the pod, bring the tunnel up in it, check it, and return the agent's
    pinned names resolved through it. CLEANUP (the launcher's ExitStack) removes the pod and the container."""
    l = launcher()
    profile = a.vpn_profile
    podman = l.podman_command(a)
    say = lambda text: print(f"agent-sandbox: vpn: {l.printable(text)}", file=sys.stderr)  # noqa: E731
    if podman_run([*podman, "image", "exists", VPN_IMAGE]).returncode != 0:
        raise ValueError(f"The VPN image {VPN_IMAGE} is missing; build it with: sandbox-ai build vpn")
    cleanup.enter_context(profile_lock(profile))
    a.vpn_pod = a.pod_name or f"agent-{getattr(a, 'run_id', None) or a.agent + '-' + uuid.uuid4().hex[:12]}"
    create = l.pod_commands(a, a.vpn_pod, session, home)[0]
    podman_run(create, check=True, stdout=subprocess.DEVNULL)

    def remove_pod():
        with l.signals_deferred():
            podman_run([*podman, "pod", "rm", "--force", "--time", "0", a.vpn_pod], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cleanup.callback(remove_pod)
    command = container_command(podman, f"{a.vpn_pod}-vpn", profile, pod=a.vpn_pod)
    tunnel, ready, _ = bring_up(profile, command, names, say, log_dir=a.log_dir, run=getattr(a, "run_id", None),
                                warn_only=a.vpn_check == "warn")
    cleanup.callback(tunnel.close)  # before the pod goes (callbacks run last in, first out)

    def follow(event):
        if event.get("event") == "down":
            say(f"the tunnel went down: {event.get('message')}")
            log_record(a.log_dir, profile, {"tunnel": ready.get("tunnel")}, {}, {"failures": [str(event.get("message"))],
                                                                                 "warnings": [], "notes": []})
    tunnel.follow(follow)
    dns = (ready.get("tunnel") or {}).get("dns")
    if dns:
        a.vpn_dns = [str(ipaddress.ip_address(server)) for server in dns]
    hosts = ready.get("hosts") or {}
    if set(hosts) != set(names):
        raise ValueError("the VPN container returned an incomplete host list")
    return {name: sorted({str(ipaddress.ip_address(addr)) for addr in hosts[name]}) for name in names}


# --- MCP servers -------------------------------------------------------------------------------------------------

def mcp_container(server):
    return f"sandbox-ai-vpn-mcp-{server}"


def mcp_wrapper(profile_name, container, owner, command):
    """--internal-mcp-vpn: the VPN container of an MCP image server (its own network namespace), checked, then the
    server's bridge, whose containers join that namespace (--network container:CONTAINER). The tunnel lives as
    long as the bridge; a failed check stops the server."""
    profile = load_profile(profile_name)
    podman = ["podman"]
    # One line per message: the log reader (mcp.py --internal-log) takes lines that start with the relay's
    # markers as its records, and a message can carry what the VPN's server sent.
    say = lambda text: print("sandbox-mcp: vpn: " + launcher().printable(text).replace("\n", "\\n"),  # noqa: E731
                             file=sys.stderr, flush=True)
    found = podman_run([*podman, "container", "inspect", "--format", "{{json .Config.Labels}}", container],
                       capture_output=True, text=True)
    if found.returncode == 0:
        try:
            labels = json.loads(found.stdout.strip() or "null") or {}
        except ValueError:
            labels = {}
        if not isinstance(labels, dict) or labels.get("sandbox-ai.vpn-owner") != owner:
            raise ValueError(f"a container named {container} exists and was not created here")
        podman_run([*podman, "rm", "--force", "--time", "0", container], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    with profile_lock(profile):
        network = launcher().POD_NETWORK
        tunnel, _, _ = bring_up(profile, container_command(podman, container, profile, network=network,
                                                           labels=[f"sandbox-ai.vpn-owner={owner}"]), (), say)
        child = None
        try:
            child = subprocess.Popen(command)
            stop = lambda signum, _frame: child.send_signal(signum)  # noqa: E731
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, stop)
            tunnel.follow(lambda event: say(f"the tunnel went down: {event.get('message')}")
                          if event.get("event") == "down" else None)
            return child.wait()
        finally:
            if child and child.poll() is None:
                child.terminate()
            tunnel.close()
            podman_run([*podman, "rm", "--force", "--time", "0", container], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


# --- sandbox-ai vpn ----------------------------------------------------------------------------------------------

def store_secret(name, value):
    secret_store().store().set(name, value)


def drop_secrets(profile):
    store = secret_store()
    names = set()
    if profile["type"] == "wireguard":
        names.add(profile["wireguard"]["private_key_secret"])
        names.update((profile["wireguard"].get("preshared_key_secrets") or {}).values())
    if profile["type"] == "openvpn" and profile["openvpn"].get("auth_secret_owned"):
        names.add(profile["openvpn"]["auth_secret"])
    if profile["type"] == "tor" and profile["tor"].get("bridges_secret_owned"):
        names.add(profile["tor"]["bridges_secret"])
    for name in names:
        with contextlib.suppress(ValueError, OSError, subprocess.SubprocessError):
            store.store().delete(name)
            store.FileStore().delete(name)


def new_profile(args, kind, **fields):
    profile = {"version": 1, "name": args.name, "type": kind, "provider": args.provider,
               "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    if args.dns:
        profile["dns"] = [str(ipaddress.ip_address(v)) for v in args.dns]
    if args.check_url or args.no_dns_probe:
        profile["checks"] = {**({"urls": [check_url(u) for u in args.check_url]} if args.check_url else {}),
                             **({"dns_probe": None} if args.no_dns_probe else {})}
    return profile


def add_wireguard(args, out):
    text = Path(args.wireguard).expanduser().read_text()
    wireguard, values, notes = parse_wireguard(text)
    wireguard["private_key_secret"] = secret_name(args.name, "KEY")
    wireguard["preshared_key_secrets"] = {index: secret_name(args.name, f"PSK{index}")
                                          for index in values["preshared_keys"]}
    dns = wireguard.pop("dns")
    profile = new_profile(args, "wireguard", wireguard=wireguard, public_key=public_key(values["private_key"]))
    profile.setdefault("dns", dns)
    if not profile["provider"]:
        profile["provider"] = "mullvad" if "10.64.0.1" in dns else "proton" if "10.2.0.1" in dns else None
    store_secret(wireguard["private_key_secret"], values["private_key"])
    for index, value in values["preshared_keys"].items():
        store_secret(wireguard["preshared_key_secrets"][index], value)
    for note in notes:
        out(f"note: {note}")
    out(f"the keys are kept in sandbox-ai secrets ({wireguard['private_key_secret']}); you can delete "
        f"{args.wireguard} now")
    return profile


def add_openvpn(args, out):
    path = Path(args.openvpn).expanduser()
    data = path.read_bytes()
    if len(data) > FILE_LIMIT:
        raise ValueError(f"{path} is larger than {FILE_LIMIT >> 20} MiB")
    config, info, credentials, notes = parse_openvpn(data.decode("utf-8", "replace"), path.parent)
    auth_secret, owned = args.auth_secret, False
    if credentials and not auth_secret:
        if len(credentials) != 2:
            raise ValueError("auth-user-pass: expected a user name and a password")
        auth_secret, owned = secret_name(args.name, "AUTH"), True
        store_secret(auth_secret, f"{credentials[0]}:{credentials[1]}")
        out(f"note: the user name and password were moved to sandbox-ai secrets ({auth_secret})")
    if info["auth"] and not auth_secret:
        raise ValueError("the configuration asks for a user name and password: keep them as USER:PASSWORD with "
                         "`sandbox-ai secrets set NAME` and add --auth-secret NAME")
    if auth_secret:
        secret_store().check_name(auth_secret)
    config_file = f"{args.name}.ovpn"
    write_private(profiles_dir() / config_file, config)
    for note in notes:
        out(f"note: {note}")
    return new_profile(args, "openvpn", openvpn={
        "config_file": config_file, "remotes": info["remotes"], "auth": info["auth"], "auth_secret": auth_secret,
        "auth_secret_owned": owned, "full": not args.split}, **({"dns": info["dns"]} if info["dns"] and not args.dns
                                                                  else {}))


def add_mullvad(args, out, client=None, prompt=getpass.getpass):
    client = client or Mullvad()
    account = read_account(args.account_secret, prompt)
    private = generate_private_key()
    relays, gateways = client.relays()
    relay = choose_relay(relays, args.location)
    token = client.token(account)
    device = client.create_device(token, public_key(private))
    ipv6 = not args.no_ipv6
    addresses = [a for a in device["addresses"] if ipv6 or ipaddress.ip_interface(a).version == 4]
    dns = [gateways.get("ipv4") or MULLVAD_DNS[0]] + ([gateways.get("ipv6") or MULLVAD_DNS[1]] if ipv6 else [])
    endpoint = relay["ipv6"] if args.endpoint_ipv6 and relay.get("ipv6") else relay["ipv4"]
    wireguard = {"addresses": addresses, "mtu": None, "listen_port": None,
                 "private_key_secret": secret_name(args.name, "KEY"), "preshared_key_secrets": {},
                 "peers": [{"public_key": relay["public_key"], "allowed_ips": ["0.0.0.0/0"] + (["::/0"] if ipv6 else []),
                            "endpoint": {"host": endpoint, "port": MULLVAD_PORT}, "keepalive": None,
                            "preshared": False}]}
    args.provider = "mullvad"
    profile = new_profile(args, "wireguard", wireguard=wireguard, public_key=public_key(private),
                          mullvad={"device": device["name"], "device_id": device["id"], "relay": relay["hostname"],
                                   "location": relay["location"], "account_secret": args.account_secret})
    profile.setdefault("dns", dns)
    store_secret(wireguard["private_key_secret"], private)
    out(f"Mullvad device {device['name'] or device['id']} registered; relay {relay['hostname']} ({relay['location']})")
    return profile


def add_tailscale(args, out):
    if args.auth_key_secret:
        secret_store().check_name(args.auth_key_secret)
    if args.exit_node and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", args.exit_node):
        raise ValueError("--exit-node expects a node name or tailnet address")
    if args.login_server:
        parts = urllib.parse.urlsplit(args.login_server)
        if parts.scheme != "https" or not parts.hostname or parts.username:
            raise ValueError("--login-server expects an https URL (Headscale, for one)")
    hostname = args.hostname or f"sandbox-ai-{args.name}".lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
        raise ValueError("--hostname: letters, digits and '-'")
    allow = [str(ipaddress.ip_network(n, strict=False)) for n in (args.tailnet_allow or runtime().TAILNET)]
    if args.ephemeral and not args.auth_key_secret:
        raise ValueError("--ephemeral needs --auth-key-secret: an ephemeral node logs in anew at every start")
    profile = new_profile(args, "tailscale", tailscale={
        "auth_key_secret": args.auth_key_secret, "exit_node": args.exit_node, "login_server": args.login_server,
        "hostname": hostname, "ephemeral": bool(args.ephemeral), "tailnet_allow": allow})
    profile["provider"] = args.provider or ("headscale" if args.login_server else "tailscale")
    if not args.auth_key_secret:
        out("note: no auth key: the first start shows a login URL")
    return profile


def add_tor(args, out):
    """A Tor profile: bridges (obfs4 or plain lines) from a private file, kept in sandbox-ai secrets, or from a
    secret of yours; exit countries; a SocksPort on the sandbox's loopback only with --socks."""
    rt = runtime()
    if args.dns:
        raise ValueError("--dns: a Tor profile resolves through tor's own DNSPort (the exits look the names up)")
    countries = rt.exit_countries([c.strip() for c in args.exit_country.split(",")] if args.exit_country else [])
    text, secret, owned = None, args.bridge_secret, False
    if args.bridges:
        text = read_private(Path(args.bridges).expanduser(), 64 << 10)
    elif secret:
        store = secret_store()
        store.check_name(secret)
        text = store.get(secret)
        if text is None:
            raise ValueError(f"no secret {secret}; keep the bridge lines with: sandbox-ai secrets set {secret}")
    bridges = rt.parse_bridges(text) if text is not None else []
    if text is not None and not bridges:
        raise ValueError("no bridge line found (obfs4 IP:PORT FINGERPRINT cert=... iat-mode=N)")
    if args.bridges:
        secret, owned = secret_name(args.name, "BRIDGES"), True
        store_secret(secret, "".join(rt.bridge_line(bridge) + "\n" for bridge in bridges))
        out(f"note: the bridge lines are kept in sandbox-ai secrets ({secret}); you can delete {args.bridges} now")
    profile = new_profile(args, "tor", tor={
        "bridges": len(bridges), "bridges_secret": secret, "bridges_secret_owned": owned,
        "transports": sorted({b["transport"] or "plain" for b in bridges}), "exit_country": countries,
        "socks": bool(args.socks)})
    profile["provider"] = args.provider or "tor"
    if args.socks:
        out(f"note: 127.0.0.1:{rt.TOR_SOCKS_PORT} in the sandbox is Tor's SocksPort: what goes through it passes the "
            "connection proxy by (its allow-list, approvals and connection log)")
    return profile


def remove(name, out=print, client=None):
    profile = load_profile(name)
    mullvad = profile.get("mullvad") or {}
    if mullvad.get("account_secret") and mullvad.get("device_id"):
        try:
            client = client or Mullvad()
            client.delete_device(client.token(read_account(mullvad["account_secret"])), mullvad["device_id"])
            out(f"Mullvad device {mullvad.get('device') or mullvad['device_id']} removed")
        except ValueError as exc:
            out(f"note: {exc}; remove the device at https://mullvad.net/account/devices")
    elif mullvad:
        out(f"note: remove the Mullvad device {mullvad.get('device')} at https://mullvad.net/account/devices")
    drop_secrets(profile)
    if profile["type"] == "openvpn":
        (profiles_dir() / profile["openvpn"]["config_file"]).unlink(missing_ok=True)
    shutil.rmtree(state_dir(name), ignore_errors=True)
    profile_path(name).unlink()
    out(f"VPN profile {name} removed")
    return 0


def check(name, as_json=False, podman_url=None, out=print):
    """sandbox-ai vpn check: the profile's tunnel in a container of its own, checked, then taken down."""
    profile = load_profile(name)
    podman = ["podman", *(["--remote", "--url", podman_url] if podman_url else [])]
    say = (lambda text: None) if as_json else (lambda text: out(f"vpn: {text}"))
    container = f"sandbox-ai-vpn-check-{uuid.uuid4().hex[:8]}"
    with profile_lock(profile):
        command = container_command(podman, container, profile, action="check", network=launcher().POD_NETWORK)
        data = payload(profile, "check")
        tailnet = profile["type"] == "tailscale" and not profile["tailscale"].get("exit_node")
        baseline = in_background(lambda: {} if tailnet else host_baseline(data["checks"]))
        tunnel = Tunnel(command, data, say)
        try:
            ready = tunnel.wait_ready(timeout=ready_timeout(profile))
        finally:
            tunnel.close()
        host = baseline()
    verdict = evaluate(profile, ready.get("tunnel") or {}, ready.get("checks") or {}, host)
    if as_json:
        out(json.dumps({**describe(profile), "tunnel": ready.get("tunnel"), "inside": ready.get("checks"),
                        "host": host, **verdict}, indent=2))
    else:
        report(profile, ready, verdict, say)
        say("all checks passed" if not verdict["failures"] else "the checks FAILED")
    return 0 if not verdict["failures"] else 1


def show(name, as_json=False, out=print):
    facts = describe(load_profile(name))
    if as_json:
        out(json.dumps(facts, indent=2))
        return 0
    for key, value in facts.items():
        out(f"{key}: {', '.join(map(str, value)) if isinstance(value, list) else value}")
    return 0


def list_command(as_json=False, out=print):
    rows = []
    for name in list_profiles():
        try:
            facts = describe(load_profile(name))
        except (ValueError, KeyError) as exc:
            facts = {"profile": name, "type": "?", "provider": "?", "endpoints": [], "error": str(exc)}
        rows.append(facts)
    if as_json:
        out(json.dumps(rows, indent=2))
    elif not rows:
        out("no VPN profiles; add one with: sandbox-ai vpn add NAME --wireguard FILE (or --openvpn, --mullvad, "
            "--tailscale, --tor)")
    else:
        for facts in rows:
            out(f"{facts['profile']}  {facts['type']}  {facts['provider']}  {', '.join(facts.get('endpoints') or [])}")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai vpn", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
    commands = p.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Add a VPN profile")
    add.add_argument("name")
    kind = add.add_mutually_exclusive_group(required=True)
    kind.add_argument("--wireguard", metavar="FILE", help="A wg-quick configuration (any provider, your own server)")
    kind.add_argument("--openvpn", metavar="FILE", help="An OpenVPN client configuration (.ovpn)")
    kind.add_argument("--mullvad", action="store_true", help="Register a new WireGuard key with your Mullvad account")
    kind.add_argument("--tailscale", action="store_true", help="Tailscale (or Headscale) node in the VPN container")
    kind.add_argument("--tor", action="store_true",
                      help="Tor: every TCP connection through Tor (transparent), DNS through tor, .onion names work")
    add.add_argument("--dns", action="append", default=[], metavar="IP",
                     help="Resolvers to use instead of the configuration's; repeatable")
    add.add_argument("--check-url", action="append", default=[], metavar="URL",
                     help="Address echo service for the checks (https; JSON with \"ip\" or the bare address); "
                          "repeatable, replaces the defaults")
    add.add_argument("--no-dns-probe", action="store_true", help=f"Do not ask {DNS_PROBE} where DNS leaves")
    add.add_argument("--provider", metavar="NAME", help="The provider's name (mullvad enables its own check)")
    add.add_argument("--force", action="store_true", help="Replace an existing profile of that name")
    add.add_argument("--auth-secret", metavar="SECRET", help="OpenVPN: the secret holding USER:PASSWORD")
    add.add_argument("--split", action="store_true",
                     help="OpenVPN: only the server's routes go through the tunnel (the rest stays blocked)")
    add.add_argument("--account-secret", metavar="SECRET",
                     help="Mullvad: the secret holding the account number (else it is asked for, not shown)")
    add.add_argument("--location", metavar="LOC", help="Mullvad: a country (se), city (se-sto) or relay")
    add.add_argument("--no-ipv6", action="store_true", help="Mullvad: no IPv6 in the tunnel (IPv6 is then blocked)")
    add.add_argument("--endpoint-ipv6", action="store_true", help="Mullvad: reach the relay over IPv6")
    add.add_argument("--auth-key-secret", metavar="SECRET", help="Tailscale: the secret holding an auth key")
    add.add_argument("--exit-node", metavar="HOST", help="Tailscale: all egress through this exit node")
    add.add_argument("--login-server", metavar="URL", help="Tailscale: a Headscale server")
    add.add_argument("--hostname", metavar="NAME", help="Tailscale: the node's name (default sandbox-ai-NAME)")
    add.add_argument("--ephemeral", action="store_true",
                     help="Tailscale: a new ephemeral node per start (needs an auth key; parallel starts possible)")
    add.add_argument("--tailnet-allow", action="append", default=[], metavar="CIDR",
                     help="Tailscale: what of the tailnet the agent may reach (default the whole tailnet)")
    bridges = add.add_mutually_exclusive_group()
    bridges.add_argument("--bridges", metavar="FILE",
                         help="Tor: bridge lines (obfs4 or plain) from a private (0600) file; kept in sandbox-ai secrets")
    bridges.add_argument("--bridge-secret", metavar="SECRET", help="Tor: the secret holding the bridge lines")
    add.add_argument("--exit-country", metavar="CC", help="Tor: exits only in these countries (de, or de,nl)")
    add.add_argument("--socks", action="store_true",
                     help="Tor: a SocksPort at 127.0.0.1:9050 in the sandbox (Tor Browser, Playwright); what goes "
                          "through it passes the connection proxy by")
    for name in ("list", "show", "remove", "check"):
        sub = commands.add_parser(name)
        if name != "list":
            sub.add_argument("name")
        if name in ("list", "show", "check"):
            sub.add_argument("--json", action="store_true")
        if name == "check":
            sub.add_argument("--podman-url", metavar="URL")
    return p


def main(argv, out=print):
    args = parser().parse_args(argv)
    if args.command == "list":
        return list_command(args.json, out)
    if args.command == "show":
        return show(args.name, args.json, out)
    if args.command == "remove":
        return remove(args.name, out)
    if args.command == "check":
        return check(args.name, args.json, args.podman_url, out)
    check_name(args.name)
    if not args.tor and (args.bridges or args.bridge_secret or args.exit_country or args.socks):
        raise ValueError("--bridges, --bridge-secret, --exit-country and --socks are for --tor")
    if profile_path(args.name).exists() and not args.force:
        raise ValueError(f"a VPN profile {args.name} exists already; add --force to replace it")
    clash = [other for other in list_profiles() if other != args.name and
             secret_name(other, "") == secret_name(args.name, "")]
    if clash:
        raise ValueError(f"{args.name} and {clash[0]} would share their secrets' names; choose another name")
    if args.wireguard:
        profile = add_wireguard(args, out)
    elif args.openvpn:
        profile = add_openvpn(args, out)
    elif args.mullvad:
        profile = add_mullvad(args, out)
    elif args.tor:
        profile = add_tor(args, out)
    else:
        profile = add_tailscale(args, out)
    save_profile(profile)
    facts = describe(profile)
    out(f"VPN profile {args.name} added: {facts['type']}, {facts['provider']}, "
        f"{', '.join(facts.get('endpoints') or [])}; check it with: sandbox-ai vpn check {args.name}")
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) > 5 and sys.argv[1] == "--internal-mcp-vpn" and sys.argv[5] == "--":
            sys.exit(mcp_wrapper(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[6:]))
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"sandbox-ai vpn: {exc}", file=sys.stderr)
        sys.exit(1)
