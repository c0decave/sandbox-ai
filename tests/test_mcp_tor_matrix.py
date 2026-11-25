"""Every MCP server of the two signed catalogs (sandbox-ai-mcp "mcp:", axross-mcp-hub "hub:") behind a Tor profile.

User (2026-11-14): "angeblich können einige MCPs z.B. whatweb nicht Tor benutzen obwohl Tor als Protokoll aktiviert
wurde beim MCP-Setup - bitte in-depth untersuchen ob alle MCPs, wenn Tor aktiviert ist, das auch nutzen können".

Behind a Tor profile (the app's MCP setup and `sandbox-ai mcp add NAME --vpn TOR`, `mcp set --vpn`, or a start's
--vpn TOR for its servers) an image server's every connection runs in the network namespace of a Tor container of
its own (`--network=container:sandbox-ai-vpn-mcp-NAME`, created there by vpn.py's guard), behind its kill switch:
every TCP connection to tor's TransPort, every lookup on port 53 to its DNSPort (A, AAAA and PTR only), nothing else
(no UDP, ICMP or raw sockets). No catalog server can bypass that: a Bubblewrap-only form is refused, a server
without network gets no VPN (there is nothing to put behind it), and the plan is the only place a network is named.

What works through it, for each server (the oracle below, written from what each tool does):
  - "tor": TCP only - HTTPS APIs, Chromium (gowitness, playwright: QUIC falls back to TCP), whatweb (Ruby's resolver
    asks port 53, which tor answers with A records; HTTP over TCP), WHOIS, MTProto, the messengers' websockets;
  - "limited": works, but some of its tools ask DNS for record types tor's resolver does not answer, or need a
    server the exits cannot reach - said when it is added and when it starts;
  - "refused": cannot work behind Tor at all and is refused with the reason: nmap's connect scans would see every
    port open (tor's TransPort accepts each connection before an exit tries the target);
  - "no network": reaches nothing; a VPN for it is refused ("a VPN needs network egress-all").

The entries are those of the catalogs as published (2026-11-14), the fields that matter here."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "c" * 64


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("sandbox_mcp_tor_matrix", "mcp.py")

# The catalogs as published: name, network, hosts, required variables and folders.
MCP_CATALOG = [
    {"name": "binwalk", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "bluesky", "network": "allowlist", "hosts": ["bsky.social:443", "public.api.bsky.app:443"],
     "env": [{"name": "BLUESKY_HANDLE", "required": True, "secret": False},
             {"name": "BLUESKY_APP_PASSWORD", "required": True, "secret": True}]},
    {"name": "brave-search", "network": "allowlist", "hosts": ["api.search.brave.com:443"],
     "env": [{"name": "BRAVE_API_KEY", "required": True, "secret": True}]},
    {"name": "capa", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "code-search", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "duckduckgo", "network": "allowlist", "hosts": ["html.duckduckgo.com:443"]},
    {"name": "engram", "network": "none", "mounts": [{"target": "/data", "required": True}]},
    {"name": "exec", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "files", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "gowitness", "network": "full"},
    {"name": "memory", "network": "none", "mounts": [{"target": "/data", "required": True}]},
    {"name": "openosint", "network": "full"},
    {"name": "osint-mcp", "network": "full"},
    {"name": "playwright", "network": "full"},
    {"name": "radare2", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "searxng", "network": "full", "env": [{"name": "SEARXNG_URL", "required": True, "secret": False}]},
    {"name": "signal", "network": "full", "mounts": [{"target": "/data", "required": True}]},
    {"name": "tasks", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "telegram", "network": "allowlist", "hosts": ["api.telegram.org:443"],
     "env": [{"name": "TELEGRAM_BOT_TOKEN", "required": True, "secret": True}]},
    {"name": "telegram-user", "network": "full",
     "env": [{"name": "TELEGRAM_API_ID", "required": True, "secret": True},
             {"name": "TELEGRAM_API_HASH", "required": True, "secret": True},
             {"name": "TELEGRAM_SESSION", "required": True, "secret": True}]},
    {"name": "web-fetch", "network": "full"},
    {"name": "whatsapp", "network": "full"},
    {"name": "whatweb", "network": "full"},
    {"name": "youtube", "network": "allowlist", "hosts": ["oauth2.googleapis.com:443", "www.googleapis.com:443"],
     "env": [{"name": "YOUTUBE_CLIENT_ID", "required": True, "secret": True},
             {"name": "YOUTUBE_CLIENT_SECRET", "required": True, "secret": True},
             {"name": "YOUTUBE_REFRESH_TOKEN", "required": True, "secret": True}]},
]
HUB_CATALOG = [
    {"name": "binwalk", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "capa", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "crtsh", "network": "allowlist", "hosts": ["crt.sh:443"]},
    {"name": "dnstwist", "network": "full"},
    {"name": "exiftool", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "ffuf", "network": "full"},
    {"name": "floss", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "gitleaks", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "john", "network": "none"},
    {"name": "mcpcap", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "nmap", "network": "full"},
    {"name": "nuclei", "network": "full"},
    {"name": "radare2", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "sleuthkit", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "sqlmap", "network": "full"},
    {"name": "tshark", "network": "none", "mounts": [{"target": "/work", "required": True}]},
    {"name": "yara", "network": "none", "mounts": [{"target": "/work", "required": True}]},
]

# The oracle: how each server fares behind Tor, and for "limited" what its note must name.
TOR = {
    "mcp:bluesky": "tor", "mcp:brave-search": "tor", "mcp:duckduckgo": "tor", "mcp:gowitness": "tor",
    "mcp:playwright": "tor", "mcp:signal": "tor", "mcp:telegram": "tor", "mcp:telegram-user": "tor",
    "mcp:web-fetch": "tor", "mcp:whatsapp": "tor", "mcp:whatweb": "tor", "mcp:youtube": "tor",
    "hub:crtsh": "tor", "hub:ffuf": "tor", "hub:sqlmap": "tor",
    "mcp:osint-mcp": ("limited", "MX, TXT"), "mcp:openosint": ("limited", "MX, NS, TXT"),
    "mcp:searxng": ("limited", "reachable from a Tor exit"), "hub:dnstwist": ("limited", "MX and NS"),
    "hub:nuclei": ("limited", "every port open"),
    "hub:nmap": ("refused", "every port open"),
}


def catalog_id(prefix, entry):
    return f"{prefix}:{entry['name']}"


def cases():
    for prefix, entries in (("mcp", MCP_CATALOG), ("hub", HUB_CATALOG)):
        for entry in entries:
            ident = catalog_id(prefix, entry)
            expected = "no network" if entry["network"] == "none" else TOR[ident]
            yield pytest.param(prefix, entry, expected, id=ident)


def test_the_oracle_covers_every_server_that_reaches_the_network():
    reaching = {catalog_id(p, e) for p, entries in (("mcp", MCP_CATALOG), ("hub", HUB_CATALOG)) for e in entries
                if e["network"] != "none"}
    assert reaching == set(TOR)
    assert {key for key, (support, _) in m.TOR_LIMITS.items()} == {k for k, v in TOR.items() if v != "tor"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    for variable, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "cfg"), ("XDG_DATA_HOME", "data"),
                          ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "rt")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(variable, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    vpn = m.launcher().load_vpn_module()
    assert vpn.main(["add", "tor-test", "--tor"], out=lambda text: None) == 0
    sources = []
    for prefix, entries in (("mcp", MCP_CATALOG), ("hub", HUB_CATALOG)):
        path = tmp_path / prefix / "catalog.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [
            {**entry, "id": catalog_id(prefix, entry), "title": entry["name"], "kind": "oci", "digest": DIGEST,
             "image": f"ghcr.io/c0decave/{prefix}-{entry['name']}"} for entry in entries]}))
        sources.append((prefix, str(path)))
    folder = tmp_path / "folder"
    folder.mkdir()
    return types.SimpleNamespace(sources=sources, folder=folder, tmp=tmp_path)


def add_behind_tor(world, prefix, entry, out):
    """sandbox-ai mcp add PREFIX:NAME --vpn tor-test, as the app's MCP setup runs it (an allowlist entry needs
    --network full; the required folders and variables given)."""
    mounts = [f"{m_['target']}={world.folder}" for m_ in entry.get("mounts", [])]
    env = [f"{e['name']}@S_{e['name']}" if e.get("secret") else f"{e['name']}=https://searx.example.org"
           for e in entry.get("env", [])]
    return m.add(catalog_id(prefix, entry), mounts=mounts, env=env,
                 network="full" if entry["network"] == "allowlist" else None, catalogs=world.sources, pull=False,
                 out=out.append, vpn="tor-test", unsigned=True)


@pytest.mark.parametrize("prefix, entry, expected", list(cases()))
def test_every_catalog_server_behind_tor(world, prefix, entry, expected):
    out = []
    support = expected if isinstance(expected, str) else expected[0]
    if support == "no network":
        with pytest.raises(ValueError, match="a VPN needs network egress-all"):
            add_behind_tor(world, prefix, entry, out)
        return
    if support == "refused":
        with pytest.raises(ValueError, match=f"cannot work behind the Tor profile tor-test: .*{expected[1]}"):
            add_behind_tor(world, prefix, entry, out)
        assert entry["name"] not in m.definitions()
        return
    definition = add_behind_tor(world, prefix, entry, out)
    name = entry["name"]
    notes = [line for line in out if "behind Tor:" in line]
    if support == "limited":
        assert len(notes) == 1 and expected[1] in notes[0], out
    else:
        assert notes == [], out
    # Wired into the Tor namespace: its plan, each connection's podman run line and the guard in front of it.
    definition = m.definitions()[name]
    plan = m.server_network(name, definition)
    container = f"sandbox-ai-vpn-mcp-{name}"
    assert (plan["vpn"], plan["vpn_type"], plan["network"]) == ("tor-test", "tor", f"--network=container:{container}")
    assert bool(plan.get("tor_note")) == (support == "limited")
    command = m.image_command(plan)
    networks = [word for word in command if word.startswith("--network") or word in ("--net", "--pod")]
    assert networks == [f"--network=container:{container}"], command
    assert "--env=SANDBOX_AI_TOR=1" in command  # (the server is told: .onion names, its own messages)
    assert not any(word.startswith(("--dns", "--add-host")) for word in command)
    guard = m.connection_command(plan)
    assert guard[2:5] == [str(ROOT / "sandbox" / "vpn.py"), "--internal-mcp-guard", container]
    assert guard[guard.index("--") + 1:] == command
    # Its Tor container: the Tor image, its kill switch, the same bootstrap with its retries as the agent's.
    vpn = m.launcher().load_vpn_module()
    profile = vpn.load_profile("tor-test")
    tor = vpn.container_command(["podman"], container, profile, network="pasta")
    assert vpn.TOR_IMAGE in tor and "--cap-add=NET_ADMIN" in tor and "--cap-drop=ALL" in tor
    payload = {**vpn.payload(profile, "serve"), "mcp": True}
    assert vpn.runtime().bootstrap_settings(payload["profile"]["tor"])["attempts"] == 3


def test_a_start_behind_tor_refuses_a_server_that_cannot_work_there_and_says_what_does_not(world, capsys):
    """A start with --vpn TOR and --mcp: nmap (added without a VPN) is refused before anything starts, with the
    reason; osint-mcp starts with what does not work behind Tor said."""
    m.add("hub:nmap", catalogs=world.sources, pull=False, out=lambda text: None, unsigned=True)
    m.add("mcp:osint-mcp", catalogs=world.sources, pull=False, out=lambda text: None, unsigned=True)
    vpn = m.launcher().load_vpn_module()
    a = types.SimpleNamespace(vpn="tor-test", vpn_profile=vpn.load_profile("tor-test"), project_document=None)
    with pytest.raises(ValueError, match="MCP server nmap cannot work behind the Tor profile tor-test: its connect "
                                         "scans would see every port open"):
        m.vpn_for_servers(a, ["osint-mcp", "nmap"])
    assert m.vpn_for_servers(a, ["osint-mcp"]) == {"osint-mcp": "tor-test"}
    plan = m.server_network("osint-mcp", m.definitions()["osint-mcp"], "tor-test")
    assert plan["tor_note"].startswith("MCP server osint-mcp behind Tor: dns_lookup of record types other than A")


def test_mcp_set_vpn_tor_is_held_to_the_same(world):
    out = []
    m.add("hub:nmap", catalogs=world.sources, pull=False, out=out.append, unsigned=True)
    with pytest.raises(ValueError, match="cannot work behind the Tor profile tor-test"):
        m.set_server("nmap", {"vpn": "tor-test"}, out=out.append, catalogs=world.sources)
    assert "vpn" not in m.definitions()["nmap"]
    m.add("hub:dnstwist", catalogs=world.sources, pull=False, out=out.append, unsigned=True)
    out.clear()
    m.set_server("dnstwist", {"vpn": "tor-test"}, out=out.append, catalogs=world.sources)
    assert any("MCP server dnstwist behind Tor: only A and AAAA lookups work" in line for line in out), out
    assert m.definitions()["dnstwist"]["vpn"] == "tor-test"


def test_a_catalog_entry_of_its_own_says_how_it_fares_behind_tor(world):
    """A catalog entry's own "tor" wins over the table: a later catalog can say it."""
    entry = {"id": "mcp:whatweb", "tor": {"support": "no", "note": "a reason of the catalog's"}}
    assert m.tor_support({}, entry) == ("no", "a reason of the catalog's")
    assert m.tor_support({"source": "hub:nmap"})[0] == "no"
    assert m.tor_support({"source": "mcp:whatweb"}) == ("yes", "")
    assert m.tor_problem("whatweb", {}, "tor-test", entry).endswith("a reason of the catalog's. Use it without Tor, "
                                                                    "or behind a VPN profile that is no Tor profile")


def test_a_vpn_that_is_no_tor_profile_takes_nmap(world):
    vpn = m.launcher().load_vpn_module()
    conf = world.tmp / "wg.conf"
    conf.write_text("[Interface]\nPrivateKey = " + "A" * 42 + "E=\nAddress = 10.64.0.2/32\n\n[Peer]\nPublicKey = "
                    + "B" * 42 + "E=\nAllowedIPs = 0.0.0.0/0\nEndpoint = 198.51.100.7:51820\n")
    conf.chmod(0o600)
    assert vpn.main(["add", "wg", "--wireguard", str(conf)], out=lambda text: None) == 0
    out = []
    m.add("hub:nmap", catalogs=world.sources, pull=False, out=out.append, vpn="wg", unsigned=True)
    assert m.definitions()["nmap"]["vpn"] == "wg" and not any("Tor" in line for line in out)
