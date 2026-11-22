#!/usr/bin/env python3
"""An MCP server's way out: the internet, not your network. The launcher's side of vpn_runtime.py's egress rules.

A catalog MCP server with network ("full", "allowlist" or "lan" in its definition) reaches the network only through
the nftables table vpn_runtime.egress_rules writes, loaded into its network namespace before the server runs there:

  internet ("full")       every public address; never your local network (RFC 1918, carrier-grade NAT, loopback,
                          link-local with the clouds' metadata service 169.254.169.254, IPv6's unique-local and
                          link-local ones, multicast) nor this host's own addresses - whatever a DNS answer said
  hosts ("allowlist")     only the hosts its catalog entry declares (NAME:PORT): looked up when its connection begins,
                          public answers only, pinned in the table and in its /etc/hosts; no DNS of its own
  lan ("lan")             everything but the metadata services: you chose it (mcp add/set --network lan), with a warning
  --allow-private CIDR and --allow-tcp IP:PORT (mcp add/set) open a part of your network to an internet or hosts server.

Where it is loaded (one table, the same rules everywhere):
  Podman    each connection (the bridge's command): `podman create` the server's container, `podman init` it (its
            network namespace and pasta exist, nothing of the server runs), the table loaded into that namespace
            (`podman unshare nsenter --net=SANDBOXKEY nft -f -`: rootless Podman's namespaces belong to its user
            namespace) and listed back, only then `podman start`. Anything else removes the container unstarted.
  bwrap     each connection (mcp_rootfs.py): bwrap waits on --block-fd until slirp4netns is attached and the table is
            loaded into the sandbox's namespaces (nft run in its user and network namespace with CAP_NET_ADMIN, which
            the server never has), then the server starts.
  VPN/Tor   the VPN container of the server loads it into its namespace, which the server's containers join
            (vpn_runtime.load_mcp_egress; each connection checks it again, vpn.mcp_guard).
macOS: catalog servers do not run there yet (mcp.py says so); a server defined with a command is a sandbox of the
launcher with the start's own firewall.

  mcp_egress.py --internal-egress-guard CONFIG -- podman run ... IMAGE   (CONFIG: base64 JSON, egress_config)

Threat model. Against: an MCP server that is hostile, compromised or steered by the agent (a fetch or a browser goes
wherever it is told: "open http://169.254.169.254/latest/meta-data/", "scan 192.168.1.0/24", a name whose DNS answer
points into your network). Protected: your local network and its services, this host's own services (pasta and
slirp4netns reach them through any of its addresses), the clouds' metadata services (instance credentials), and for
an allowlist server everything but its hosts, DNS included (no channel out through lookups). The server cannot change
the rules: they are loaded before it runs, and it never has CAP_NET_ADMIN in that namespace (--cap-drop=ALL,
no-new-privileges; bwrap --cap-drop ALL as a user who is not root there); each connection is checked again before it
starts. Not protected: what a full server sends to the internet (the agent decides what it fetches - its MCP rules
and the gateway's records are the control there); a resolver of the namespace on port 53 (DNS questions reach it, as
the server's lookups must); an allowlist host's address, which may serve other names too (a CDN: the rules pin
addresses, not names - the hosts file and the lack of DNS keep the server to its names, a raw connection with
another SNI to the same address is not refused); what you opened with --network lan, --allow-private or --allow-tcp.
A host without nft runs no server with network at all (fail closed), never one without its rules.

Standard library only.
"""
import base64
import contextlib
import ctypes
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys

HERE = Path(__file__).resolve().parent
EGRESS_ENV = "SANDBOX_AI_MCP_EGRESS"  # behind a VPN: the server's egress (JSON), for its wrapper and each connection
KINDS = {"full": "internet", "allowlist": "hosts", "lan": "lan"}  # a definition's network -> the rules' kind
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
NETNS_PATH = re.compile(r"/[A-Za-z0-9_./-]{1,250}")
# Options of podman run|create that choose or widen a network (= vpn.NETWORK_OPTIONS): the run line has its one
# --network=pasta:... and nothing else of them.
NETWORK_OPTIONS = ("--network", "--net", "--pod", "--pod-id-file", "--publish", "-p", "--publish-all", "-P", "--dns",
                   "--dns-option", "--dns-search", "--add-host", "--http-proxy", "--expose", "--network-alias",
                   "--mac-address", "--ip", "--ip6")
CAP_NET_ADMIN = 12
_runtime = None


def runtime():
    """vpn_runtime.py, where the rules live (the VPN container loads the same ones)."""
    global _runtime
    if _runtime is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_vpn_runtime_for_egress", HERE / "vpn_runtime.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _runtime = module
    return _runtime


def egress_config(definition, hosts_file=None, network=None):
    """The egress of an image server DEFINITION ({"kind", "hosts": ["NAME:PORT"], "allow_private": [CIDR],
    "allow_tcp": ["IP:PORT"], "hosts_file", "network"}), or None for one without network. HOSTS_FILE: where its
    /etc/hosts is kept ("hosts" only); NETWORK: the one podman --network option its connections have (pasta)."""
    kind = KINDS.get(definition.get("network", "none"))
    if kind is None:
        return None
    config = {"kind": kind, "hosts": list(definition.get("hosts") or []) if kind == "hosts" else [],
              "allow_private": list(definition.get("allow_private") or []),
              "allow_tcp": list(definition.get("allow_tcp") or [])}
    if kind == "hosts" and hosts_file is not None:
        config["hosts_file"] = str(hosts_file)
    if network is not None:
        config["network"] = network
    return config


def encoded(config):
    return base64.urlsafe_b64encode(json.dumps(config, separators=(",", ":"), sort_keys=True).encode()).decode()


def decoded(text):
    try:
        config = json.loads(base64.urlsafe_b64decode(text.encode()))
    except (ValueError, RecursionError):
        raise ValueError("the MCP server's egress configuration is not readable") from None
    if not isinstance(config, dict) or config.get("kind") not in runtime().EGRESS_KINDS:
        raise ValueError("the MCP server's egress configuration names no kind (internet, hosts or lan)")
    return config


def host_addresses(fib_trie=Path("/proc/net/fib_trie"), if_inet6=Path("/proc/net/if_inet6")):
    """This host's own addresses (IPv4 from the kernel's local routes, IPv6 of its interfaces): a server reaches none
    of them - over pasta or slirp4netns an address of this host is this host, its services listening there."""
    found = set()
    with contextlib.suppress(OSError):
        last = None
        for line in fib_trie.read_text().splitlines():
            match = re.search(r"\|-- ([0-9.]+)$", line)
            if match:
                last = match[1]
            elif last and re.search(r"/32 host LOCAL", line):
                found.add(last)
    with contextlib.suppress(OSError):
        for line in if_inet6.read_text().splitlines():
            field = line.split()[:1]
            if field and re.fullmatch(r"[0-9a-f]{32}", field[0]):
                found.add(str(ipaddress.IPv6Address(int(field[0], 16))))
    with contextlib.suppress(ValueError):
        return sorted(found, key=lambda text: (ipaddress.ip_address(text).version, ipaddress.ip_address(text)))
    return sorted(found)


def system_lookup(name, port):
    """[addresses] of NAME as this host's resolver answers (getaddrinfo, TCP)."""
    try:
        answers = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return sorted({answer[4][0].split("%", 1)[0] for answer in answers})


def pinned(hosts, lookup=None):
    """(pins [(address, port)], {name: [addresses]}, dropped) of a "hosts" server's HOSTS, looked up with LOOKUP:
    public answers only (vpn_runtime.egress_pins). ValueError when a host has none."""
    rt = runtime()
    lookup = lookup or system_lookup
    resolved = {}
    for host in hosts:
        name, port = rt.egress_host(host)
        resolved[name] = sorted(set(resolved.get(name, [])) | set(lookup(name, port)))
    return rt.egress_pins(hosts, resolved)


def write_hosts(path, names):
    """The server's /etc/hosts (its pinned NAMES), replaced in one step: a connection's container keeps the file it
    was created with."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(runtime().egress_hosts_file(names))
    temporary.chmod(0o644)  # the server's own user reads it (the container's root is you)
    os.replace(temporary, path)
    return path


def resolvers(text):
    """The nameservers of a resolv.conf TEXT."""
    found = []
    for line in str(text).splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "nameserver":
            with contextlib.suppress(ValueError):
                found.append(str(ipaddress.ip_address(words[1].split("%", 1)[0])))
    return found


def prerequisite_problem(engine, which=shutil.which):
    """Why this host cannot give a server with network its egress rules (ENGINE podman or bwrap), or None: nft (and
    for Podman nsenter) must be there; without them the server would reach your network, so it does not start."""
    missing = [tool for tool in (("nft", "nsenter") if engine == "podman" else ("nft",)) if not which(tool)]
    if not missing:
        return None
    return (f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not on this host: an MCP server with "
            "network gets rules that keep it out of your local network, loaded with them (install nftables"
            + (" and util-linux" if "nsenter" in missing else "") + "); without them it does not start")


def run_line(command, network):
    """(podman, options, image) of a connection's podman run line COMMAND with its one network option exactly
    --network=NETWORK and nothing else that chooses or widens a network; ValueError otherwise."""
    try:
        at = command.index("run")
    except ValueError:
        raise ValueError("an MCP server's command must be a podman run line") from None
    podman, options, image = command[:at], command[at + 1:-1], command[-1:]
    if not podman or Path(podman[0]).name != "podman" or not image or image[0].startswith("-"):
        raise ValueError("an MCP server's command must be a podman run line ending in its image")
    wanted = f"--network={network}"
    found = 0
    for token in options:
        if not token.startswith("-"):
            raise ValueError(f"an MCP server's run line takes every option as --NAME=VALUE ({token!r})")
        if token.split("=", 1)[0] in NETWORK_OPTIONS:
            if token != wanted:
                raise ValueError(f"an MCP server with network runs with exactly {wanted}, not {token}")
            found += 1
    if found != 1:
        raise ValueError(f"an MCP server with network runs with exactly one {wanted}")
    return podman, options, image[0]


def checked(run, command, what, data=None, timeout=60):
    try:
        done = run(command, input=data, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{what}: podman did not answer within {timeout} s") from None
    except OSError as exc:
        raise ValueError(f"{what}: {exc}") from None
    if done.returncode != 0:
        raise ValueError(f"{what} failed: {(done.stderr or '').strip()[-300:] or done.returncode}")
    return done


def pins_of(config, lookup=None):
    """(pins, {name: [addresses]}) of a "hosts" server's CONFIG, looked up now (([], {}) for another kind); what was
    dropped (a private answer) is said."""
    if config["kind"] != "hosts":
        return [], {}
    pins, names, dropped = pinned(config.get("hosts") or [], lookup)
    for line in dropped:
        print(f"sandbox-ai mcp: egress: not pinned (a private address, refused): {line}", file=sys.stderr, flush=True)
    return pins, names


def rules_for(config, resolver_text="", own=None, pins=()):
    """The egress rules of CONFIG for a namespace whose resolv.conf is RESOLVER_TEXT, a "hosts" server's PINS."""
    return runtime().egress_rules(config["kind"], own=host_addresses() if own is None else own,
                                  resolvers=resolvers(resolver_text), pins=pins,
                                  allow_private=config.get("allow_private") or [],
                                  allow_tcp=config.get("allow_tcp") or [])


def guard(config, command, run=None, popen=None, own=None, lookup=None, read=None):
    """--internal-egress-guard: one connection's container of an MCP image server without a VPN, created, its
    network namespace given the egress rules (loaded, listed back, checked), started only then; removed at the end.
    Returns the server's exit status; ValueError when anything is not as checked (nothing of the server ran)."""
    run = run or subprocess.run
    read = read or (lambda path: Path(path).read_text())
    if not config.get("network"):
        raise ValueError("the MCP server's egress configuration names no network")
    podman, options, image = run_line(command, config["network"])
    if config["kind"] == "hosts":
        mount = f"--volume={config.get('hosts_file')}:/etc/hosts:ro"
        if not config.get("hosts_file") or mount not in options:
            raise ValueError("an allowlist server's run line must mount its /etc/hosts")
    # Its hosts looked up now (public answers only), its /etc/hosts written before its container is made with it.
    pins, names = pins_of(config, lookup)
    if config["kind"] == "hosts":
        write_hosts(config["hosts_file"], names)
    created = checked(run, [*podman, "create", *options, image], "podman create", timeout=120)
    server = ((created.stdout or "").strip().splitlines() or [""])[-1]
    if not CONTAINER_ID.fullmatch(server):
        raise ValueError(f"podman create gave no container: {server[:80] or 'no answer'}")
    process = None
    try:
        checked(run, [*podman, "init", server], "podman init")
        fields = checked(run, [*podman, "container", "inspect", "--format",
                               "{{.HostConfig.NetworkMode}}|{{.NetworkSettings.SandboxKey}}|{{.ResolvConfPath}}|"
                               "{{.State.Running}}", server], "podman inspect").stdout.strip().split("|")
        if len(fields) != 4:
            raise ValueError("podman's answer about the server's container is not understood")
        mode, key, resolv, running = fields
        if not mode.startswith("pasta") or not NETNS_PATH.fullmatch(key) or running != "false":
            raise ValueError(f"the server's container is not as created (network {mode!r}, namespace {key!r}, "
                             f"running {running!r}): removed unstarted")
        text = ""
        with contextlib.suppress(OSError, UnicodeError):
            text = read(resolv) if resolv else ""
        rules = rules_for(config, text, own, pins)
        enter = [*podman, "unshare", "nsenter", f"--net={key}"]
        checked(run, [*enter, "nft", "-f", "-"], "loading the egress rules", data=rules)
        listing = checked(run, [*enter, "nft", "list", "table", "inet", runtime().EGRESS_TABLE],
                          "listing the egress rules").stdout
        problem = runtime().egress_problem(listing, config["kind"])
        if problem:
            raise ValueError(f"{problem}: removed unstarted")
        process = (popen or subprocess.Popen)([*podman, "start", "--attach", "--interactive", server])
        forward = lambda signum, _frame: process.send_signal(signum)  # noqa: E731  (podman proxies it on)
        with contextlib.suppress(ValueError):  # (not the main thread: tests)
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, forward)
        return process.wait()
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        raise
    finally:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            run([*podman, "rm", "--force", "--time", "0", server], capture_output=True, timeout=60)


# --- Bubblewrap: nft in the sandbox's namespaces --------------------------------------------------------------------

class CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]


def enter_namespaces(pid, libc=None):
    """In a child about to exec nft: the network namespace of PID and the user namespace that owns it (a bwrap
    sandbox of this user, whose user namespace gives the child every capability there), and CAP_NET_ADMIN kept across exec (ambient): nft runs as the
    sandbox's user, with the right to change its network namespace's rules and nothing of the host's."""
    libc = libc or ctypes.CDLL(None, use_errno=True)
    # The user namespace that OWNS the network namespace: bwrap --unshare-user --uid U puts the sandboxed process
    # into a second, nested user namespace, where a process entering only that one has no right over the network
    # namespace (setns(CLONE_NEWNET): EPERM). NS_GET_USERNS (Linux 4.9) names the owner; without it, PID's own.
    net = os.open(f"/proc/{int(pid)}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
    try:
        user = libc.ioctl(net, 0xB701)  # NS_GET_USERNS: a new descriptor of the owning user namespace
        if user < 0:
            user = os.open(f"/proc/{int(pid)}/ns/user", os.O_RDONLY | os.O_CLOEXEC)
        try:
            if libc.setns(user, 0x10000000) != 0:  # CLONE_NEWUSER
                raise OSError(ctypes.get_errno(), "setns(user) failed")
        finally:
            os.close(user)
        if libc.setns(net, 0x40000000) != 0:  # CLONE_NEWNET
            raise OSError(ctypes.get_errno(), "setns(net) failed")
    finally:
        os.close(net)
    header = CapHeader(0x20080522, 0)  # _LINUX_CAPABILITY_VERSION_3
    data = (CapData * 2)()
    if libc.capget(ctypes.byref(header), data) != 0:
        raise OSError(ctypes.get_errno(), "capget failed")
    data[0].inheritable |= 1 << CAP_NET_ADMIN
    if libc.capset(ctypes.byref(header), data) != 0:
        raise OSError(ctypes.get_errno(), "capset failed")
    if libc.prctl(47, 2, CAP_NET_ADMIN, 0, 0) != 0:  # PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE
        raise OSError(ctypes.get_errno(), "the ambient capability could not be raised")


def load_into_sandbox(pid, rules, kind, run=None, nft=None):
    """The egress RULES loaded into the network namespace of the bwrap sandbox PID (still blocked: nothing of the
    server runs yet) and listed back; ValueError when they are not in place."""
    nft = nft or shutil.which("nft")
    if not nft:
        raise ValueError(prerequisite_problem("bwrap"))
    if run is None:
        # libc is loaded here, before the fork: a dlopen() in the child of a process with threads can wait forever
        # on a loader lock another thread held at the fork.
        libc = ctypes.CDLL(None, use_errno=True)
        run = lambda command, **options: subprocess.run(  # noqa: E731
            command, preexec_fn=lambda: enter_namespaces(pid, libc), **options)
    for command, data, what in (([nft, "-f", "-"], rules, "loading the egress rules"),
                                ([nft, "list", "table", "inet", runtime().EGRESS_TABLE], None,
                                 "listing the egress rules")):
        try:
            done = run(command, input=data, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"{what}: {exc}") from None
        if done.returncode != 0:
            raise ValueError(f"{what} failed: {(done.stderr or '').strip()[-300:] or done.returncode}")
    problem = runtime().egress_problem(done.stdout, kind)
    if problem:
        raise ValueError(problem)


def main(argv):
    if len(argv) > 3 and argv[0] == "--internal-egress-guard" and argv[2] == "--":
        try:
            return guard(decoded(argv[1]), argv[3:])
        except ValueError as exc:
            print(f"sandbox-ai mcp: egress: MCP server refused: {exc}", file=sys.stderr, flush=True)
            return 125
    print("mcp_egress.py is used by sandbox-ai mcp (see its docstring)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
