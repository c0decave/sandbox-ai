"""Hotfix 0.1.96: a start's own networked MCP servers share its WireGuard / Mullvad / persistent Tailscale tunnel.

What the user ran into: `sandbox-ai claude --vpn mullvad --mcp fetch` no longer started. A networked MCP server behind
its start's --vpn got a tunnel of its OWN of the same profile; with WireGuard that is the same key twice (the same
tunnel address, the server sends to whichever tunnel spoke last, so the tunnels steal each other's traffic), and the
team-VPN change made the start refuse it ("would need a tunnel of its own"). The user's decision: fix now - one key,
one tunnel, the agent and its MCP servers behind it.

Now the start's VPN container LENDS its tunnel (a Unix socket in a private folder only it and its servers' VPN
containers mount) and each such server's VPN container BORROWS it: no tunnel, no key of its own, a kill switch
through which nothing leaves but over loopback, and a relay to the lender (vpn_runtime.Lender, Borrower). Held here:

  - the real ./sandbox-ai (tests/live_fakes.py: Podman faked, the VPN containers the real vpn_runtime.py with the
    kernel stood in for): `--vpn wg --mcp fetch` starts; exactly one tunnel of the key comes up (the start's, which
    lends it), the server's VPN container borrows it (no secret, a read-only folder, no tun device) and reaches the
    start's lender, it comes up only after the start's tunnel, and when the start ends nothing of it is left;
  - the relay with real sockets: a lent TCP connection reaches a public address only (never loopback, the start's
    own addresses or the networks beside the tunnel), DNS questions reach the tunnel's resolver, a borrower that
    loses its lender reaches nothing;
  - the rules: Tor and OpenVPN keep their own tunnels, a server with ANOTHER profile of its own keeps that, the lock
    still refuses a second sandbox, `mcp start NAME --vpn PROFILE` with a held profile is refused naming the holder,
    `mcp start NAME --for START` borrows the running start's tunnel, and a borrowed instance serves its run alone."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import home_guard  # noqa: E402

home_guard.ensure()
from live_fakes import Host  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("mcp_for_lent_vpn", "sandbox/mcp.py")
v = m.launcher().load_vpn_module()
rt = v.runtime()
TOKEN = "5a" * 32  # a run's token (tests/test_hotfix_mcp_lent_vpn_cut.py: what it is for, and who is denied)


@pytest.fixture(autouse=True)
def run_token(monkeypatch):
    """This process is the lender's and the borrowers' at once: the run's token is known to both."""
    monkeypatch.setattr(rt, "_lend_token", TOKEN)

PRIVATE = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="
PEER = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
WIREGUARD = f"""[Interface]
PrivateKey = {PRIVATE}
Address = 10.66.1.2/32
DNS = 10.66.0.1

[Peer]
PublicKey = {PEER}
AllowedIPs = 0.0.0.0/0
Endpoint = 198.51.100.7:51820
"""
FETCH = {"command": ["uvx", "mcp-server-fetch"], "options": ["--allow-pypi", "--egress-all-allow"]}


# --- the real ./sandbox-ai ---------------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    found = Host(tmp_path / "host")
    found.env["SANDBOX_AI_SECRETS"] = "file"
    found.env["FAKE_VPN_DIR"] = str(found.directory / "vpn-containers")
    return found


def add_wireguard(host, name="wg"):
    conf = host.directory / f"{name}.conf"
    conf.write_text(WIREGUARD)
    conf.chmod(0o600)
    done = host.run("vpn", "add", name, "--wireguard", str(conf))
    assert done.returncode == 0, done.stderr
    # The checks compare with this host's own address: asked of an echo service that answers at once (none).
    checks = host.home / ".config/sandbox-ai/vpn/checks.json"
    checks.write_text(json.dumps({"urls": ["https://echo.invalid/json"], "dns_probe": None, "timeout": 1,
                                  "attempts": 1}))
    checks.chmod(0o600)


def add_fetch(host, definition=FETCH):
    path = host.home / ".config/sandbox-ai/mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetch": definition}))


def events(host):
    path = Path(host.env["FAKE_VPN_DIR"]) / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def vpn_runs(host):
    """The podman run lines of VPN containers, in their order."""
    return [call for call in host.calls() if call[:1] == ["run"] and "localhost/sandbox-ai-vpn:latest" in call]


def option(call, name):
    return call[call.index(name) + 1] if name in call else None


def lend_mounts(call):
    return [call[i + 1] for i, token in enumerate(call) if token == "--mount" and
            call[i + 1].endswith((",dst=/run/sandbox-ai-lend", ",dst=/run/sandbox-ai-lend,ro"))]


def test_e2e_a_wireguard_start_with_a_networked_mcp_server_starts_and_lends_it_its_one_tunnel(host):
    """The report: `--vpn mullvad --mcp fetch` no longer started. Now it does - and the key is up once."""
    add_wireguard(host)
    add_fetch(host)
    host.add_image("localhost/codex-sandbox:latest", "localhost/sandbox-ai-vpn:latest")
    done = host.run("codex", "--no-tmux", "--no-auth", "--vpn", "wg", "--mcp", "fetch",
                    env={"FAKE_PODMAN_RUN": "0:0"}, timeout=300)
    said = done.stdout + done.stderr
    assert "would need a tunnel of its own" not in said, said[-3000:]
    assert "is in use by another sandbox" not in said, said[-3000:]
    runs = vpn_runs(host)
    assert len(runs) == 2, [call[:12] for call in host.calls()]
    session, server = runs
    # The start's own VPN container: in the agent's pod, with the key, lending its tunnel through a folder of its own.
    run = option(session, "--pod")[len("agent-"):]
    assert option(session, "--pod").startswith("agent-codex-") and "--device=/dev/net/tun" in session
    lender_folder = Path(host.home / ".local/share/sandbox-ai/vpn/.lend" / run)
    assert lend_mounts(session) == [f"type=bind,src={lender_folder}/socket,dst=/run/sandbox-ai-lend"]
    # The server's VPN container: in the server's own pod (the run's own instance), borrowing - no tun device, the
    # folder read-only, the relay's resolver on port 53.
    pod = option(server, "--pod")
    assert pod.startswith("agent-mcp-fetch.") and pod != "agent-mcp-fetch", "an instance of this run alone"
    assert lend_mounts(server) == [f"type=bind,src={lender_folder}/socket,dst=/run/sandbox-ai-lend,ro"]
    assert "--device=/dev/net/tun" not in server and "--cap-add=NET_BIND_SERVICE" in server
    assert option(server, "--label") == "sandbox-ai.vpn-profile=wg"
    # One tunnel of the key: brought up once (with the key, lending), borrowed once (no secret at all), and the
    # borrower reached the start's lender.
    seen = events(host)
    ups = [event for event in seen if event.get("up")]
    assert [(event["up"], event["secrets"]) for event in ups] == [
        ("wireguard", ["lend_token", "preshared_keys", "private_key"]), ("borrowed", ["lend_token"])], seen
    assert ups[0]["lend"] is True and ups[1]["borrow"] == {"run": run}
    assert {"container": f"{option(session, '--pod')}-vpn", "lender": "ping", "profile": "wg"} in seen
    assert any(event.get("borrowed", {}).get("run") == run for event in seen if "borrowed" in event)
    # It came up after the start's tunnel: the server's pod was made only once the start's VPN container ran.
    calls = host.calls()
    made = [i for i, call in enumerate(calls) if call[:2] == ["pod", "create"]]
    assert [option(calls[i], "--name") for i in made] == [option(session, "--pod"), pod]
    assert calls.index(session) < made[1]
    # The server's own launcher had the borrowed tunnel, not one of its own.
    log = (host.home / ".local/state/sandbox-ai/launcher.log").read_text()
    assert f"borrowed from the run {run}" in log
    # The fake server never made its socket: the start ended - and nothing of the tunnel is left behind.
    assert not lender_folder.exists(), "the lent tunnel's folder goes with the start"
    assert not (host.home / ".local/share/sandbox-ai/vpn/wg/.holder").exists(), "the profile's lock is free again"


def test_e2e_tor_still_gives_each_server_a_tor_of_its_own(host):
    """Tor is not lent: each sandbox its own Tor, as before (the server's own launcher gets --vpn tor, no lend)."""
    assert host.run("vpn", "add", "tor", "--tor").returncode == 0
    add_fetch(host)
    host.add_image("localhost/codex-sandbox:latest", "localhost/sandbox-ai-tor:latest")
    done = host.run("codex", "--no-tmux", "--no-auth", "--vpn", "tor", "--mcp", "fetch", "--dry-run")
    assert done.returncode == 0, done.stderr[-3000:]
    plan = json.loads(done.stdout)
    assert [(entry["name"], entry["vpn"], entry["vpn_lent"]) for entry in plan["mcp"]] == \
        [("fetch", "tor", False)]
    assert "lends" not in plan["vpn"]


def test_e2e_the_dry_run_says_the_server_borrows_the_starts_tunnel(host):
    add_wireguard(host)
    add_fetch(host)
    host.add_image("localhost/codex-sandbox:latest", "localhost/sandbox-ai-vpn:latest")
    done = host.run("codex", "--no-tmux", "--no-auth", "--vpn", "wg", "--mcp", "fetch", "--dry-run")
    assert done.returncode == 0, done.stderr[-3000:]
    plan = json.loads(done.stdout)
    assert [(entry["name"], entry["vpn"], entry["vpn_lent"]) for entry in plan["mcp"]] == \
        [("fetch", "wg", True)]
    assert plan["vpn"]["lends"] is True
    assert "through the start's tunnel of wg, lent to it" in done.stderr


def test_e2e_a_second_sandbox_with_the_profile_is_still_refused(host):
    """The lock stays for genuinely separate sandboxes: only a start's own servers share its tunnel."""
    add_wireguard(host)
    host.add_image("localhost/codex-sandbox:latest", "localhost/sandbox-ai-vpn:latest")
    script = (f"import importlib.util, sys\nspec = importlib.util.spec_from_file_location('v', "
              f"{str(ROOT / 'sandbox/vpn.py')!r})\nv = importlib.util.module_from_spec(spec)\n"
              "spec.loader.exec_module(v)\nwith v.profile_lock(v.load_profile('wg'), 'the session work-codex'):\n"
              "    print('held', flush=True)\n    sys.stdin.readline()\n")
    holder = subprocess.Popen([sys.executable, "-I", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, env=host.env)
    try:
        assert holder.stdout.readline().strip() == "held"
        done = host.run("codex", "--no-tmux", "--no-auth", "--vpn", "wg", env={"FAKE_PODMAN_RUN": "0:0"})
        assert done.returncode != 0
        assert "VPN profile wg is in use by another sandbox (the session work-codex, PID" in done.stderr
        assert vpn_runs(host) == []
        # mcp start NAME --vpn wg (a tunnel of its own) while a session holds it: refused up front, naming the holder
        # and the way to share that session's tunnel.
        add_fetch(host)
        done = host.run("mcp", "start", "fetch", "--vpn", "wg")
        assert done.returncode != 0
        assert "which is in use by the session work-codex, PID" in done.stderr, done.stderr[-2000:]
        assert "mcp start fetch --for START" in done.stderr and "--live" in done.stderr
        assert vpn_runs(host) == [] and not any(call[:2] == ["pod", "create"] for call in host.calls())
    finally:
        holder.communicate("\n", timeout=30)


# --- the relay: lender and borrower with real sockets ------------------------------------------------------------

@pytest.fixture
def folder():
    """A short folder for the lender's socket (a Unix socket's path has at most 107 bytes)."""
    import shutil
    import tempfile
    short = Path(tempfile.mkdtemp(prefix="lend-"))
    yield short
    shutil.rmtree(short, ignore_errors=True)


def echo_server():
    """A TCP server on loopback that answers each connection with "hello " + what it got first."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"hello " + conn.recv(100))
    threading.Thread(target=serve, daemon=True).start()
    return server


def lender(folder, connected, **kw):
    """A Lender at FOLDER/lend.sock whose "public" connections go to CONNECTED (the test's loopback server)."""
    return rt.Lender({"interface": "wg0", "dns": ["10.66.0.1"]}, "wg", path=folder / "lend.sock",
                     connect=lambda target: socket.create_connection(connected, timeout=5),
                     local=lambda address: address == "203.0.113.50", **kw).start()


def test_a_lent_connection_reaches_a_public_address_through_the_lender(folder):
    upstream = echo_server()
    lent = lender(folder, upstream.getsockname())
    try:
        answer, sock = rt.ask_lender({"op": "tcp", "address": "93.184.216.34", "port": 443}, folder / "lend.sock")
        assert answer == {"ok": True}
        sock.sendall(b"world")
        assert sock.recv(100) == b"hello world"
        sock.close()
        facts, sock = rt.ask_lender({"op": "ping"}, folder / "lend.sock")
        sock.close()
        assert facts == {"ok": True, "profile": "wg", "interface": "wg0", "resolver": "10.66.0.1"}
        assert oct((folder / "lend.sock").stat().st_mode & 0o777) == "0o600"
    finally:
        lent.close()
        upstream.close()


@pytest.mark.parametrize("address, why", [
    ("127.0.0.1", "no public address"), ("10.0.2.100", "no public address"), ("192.168.1.1", "no public address"),
    ("169.254.169.254", "no public address"), ("100.100.100.100", "no public address"),
    ("203.0.113.50", "no public address"), ("2001:db8::1", "not an IPv4 address"), ("example.com", "not an IPv4"),
])
def test_a_lent_connection_never_reaches_the_starts_own_namespace_or_the_networks_beside_the_tunnel(
        folder, address, why):
    reached = []
    lent = rt.Lender({"interface": "wg0", "dns": []}, "wg", path=folder / "lend.sock",
                     connect=lambda target: reached.append(target), local=lambda a: False).start()
    try:
        with pytest.raises(OSError, match=why):
            rt.ask_lender({"op": "tcp", "address": address, "port": 443}, folder / "lend.sock")
        assert reached == [], "nothing was connected"
    finally:
        lent.close()


def test_a_public_address_of_the_starts_own_sandbox_is_refused_too():
    """pasta gives the pod this host's own address - a public one on a server: a connection there would be local."""
    with pytest.raises(ValueError, match="own sandbox"):
        rt.lend_target("8.8.4.4", 53, local=lambda address: address == "8.8.4.4")
    assert rt.lend_target("8.8.4.4", 53, local=lambda address: False) == ("8.8.4.4", 53)
    for port in (0, 65536, True, "443"):
        with pytest.raises(ValueError, match="not a port"):
            rt.lend_target("8.8.4.4", port, local=lambda address: False)


def test_dns_questions_go_to_the_tunnels_resolver(folder):
    asked = []
    lent = rt.Lender({"interface": "wg0", "dns": ["10.66.0.1", "fd00::1"]}, "wg", path=folder / "lend.sock",
                     lookup=lambda query, resolver: asked.append((query, resolver)) or query[:2] + b"\x81\x80" +
                     query[4:]).start()
    try:
        query = rt.dns_query_packet("example.com", 1, 0x1234)
        borrower = rt.Borrower(path=folder / "lend.sock")
        answer = borrower.lookup(query)
        assert asked == [(query, "10.66.0.1")] and answer[:2] == b"\x12\x34"
        with pytest.raises(OSError, match="not a DNS question"):
            rt.ask_lender({"op": "dns", "query": base64.b64encode(b"short").decode()}, folder / "lend.sock")
    finally:
        lent.close()


def test_a_lent_socket_must_carry_the_mark_or_none_is_made(monkeypatch):
    """Unmarked, the agent's firewall and connection proxy would judge the server's connection as the agent's."""
    if os.geteuid() != 0:
        with pytest.raises(OSError, match="cannot be marked"):
            rt.lent_socket()
    else:  # (root without CAP_NET_ADMIN in this sandbox, or with it: either it holds, or none is made)
        try:
            sock = rt.lent_socket()
        except OSError as exc:
            assert "cannot be marked" in str(exc)
        else:
            assert sock.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK", 36)) == rt.LEND_MARK
            sock.close()


def test_a_borrower_relays_a_redirected_connection_to_the_lender_for_its_original_destination(folder):
    upstream = echo_server()
    lent = lender(folder, upstream.getsockname())
    asked = []
    original = rt.ask_lender

    def ask(request, path=None, timeout=10):
        asked.append(request)
        return original(request, path, timeout)
    borrower = rt.Borrower(path=folder / "lend.sock", destination=lambda conn: ("93.184.216.34", 443), ask=ask)
    left, right = socket.socketpair()
    try:
        thread = threading.Thread(target=borrower.relay, args=(right,), daemon=True)
        thread.start()
        left.sendall(b"there")
        assert left.recv(100) == b"hello there"
        assert asked == [{"op": "tcp", "address": "93.184.216.34", "port": 443}]
    finally:
        left.close()
        lent.close()
        upstream.close()


def test_a_borrower_without_its_lender_reaches_nothing(folder):
    """The start ended (its VPN container and lender gone): every connection is closed, every lookup fails."""
    borrower = rt.Borrower(path=folder / "lend.sock", destination=lambda conn: ("93.184.216.34", 443))
    left, right = socket.socketpair()
    borrower.relay(right)
    assert left.recv(100) == b"", "closed at once"
    left.close()
    with pytest.raises(OSError, match="did not answer"):
        borrower.lookup(rt.dns_query_packet("example.com", 1, 1))
    with pytest.raises(ValueError, match="not lent here"):
        rt.lender_facts("wg", folder / "lend.sock", wait=0)


def test_a_borrower_takes_only_the_tunnel_of_its_own_profile(folder):
    lent = rt.Lender({"interface": "wg0", "dns": []}, "other", path=folder / "lend.sock").start()
    try:
        with pytest.raises(ValueError, match="lends the tunnel of 'other', not of wg"):
            rt.lender_facts("wg", folder / "lend.sock", wait=0)
        assert rt.lender_facts("other", folder / "lend.sock", wait=0)["profile"] == "other"
    finally:
        lent.close()


def test_the_borrowers_kill_switch_lets_nothing_out_but_over_loopback():
    rules = rt.borrow_rules()
    assert rt.killswitch_problem(rules) is None, "what the guard and the pod check prove"
    output = rules.split(" chain output", 1)[1].split(" }", 1)[0]
    assert 'oifname "lo" accept' in output and "reject with icmpx type admin-prohibited" in output
    assert "oifname \"wg0\"" not in rules and "skuid" not in rules and "mark" not in rules, \
        "no way out of its own: not a tunnel, not a user, not a mark"
    divert = rules.split(" chain divert", 1)[1]
    assert f"udp dport 53 dnat ip to {rt.BORROW_DNS}:53" in divert and f"tcp dport 53 dnat ip to {rt.BORROW_DNS}:53" \
        in divert and f"meta l4proto tcp redirect to :{rt.BORROW_PORT}" in divert
    assert divert.index("ip daddr 127.0.0.0/8 return") < divert.index("dnat"), "loopback stays loopback"


def test_the_borrowers_rules_parse_where_nft_is_there():
    import shutil
    if not shutil.which("nft"):
        pytest.skip("no nft here (tests/test_mcp_vpn.py checks the other tables the same way where it is)")
    done = subprocess.run(["nft", "-c", "-f", "-"], input=rt.borrow_rules(), text=True, capture_output=True)
    if "Operation not permitted" in done.stderr:
        pytest.skip("nft -c needs CAP_NET_ADMIN here")
    assert done.returncode == 0, done.stderr


def test_the_runtime_borrows_and_lends_only_where_it_may(monkeypatch, folder):
    """main(): a borrowing container brings up no tunnel and loads the borrower's kill switch; a Tor profile neither
    lends nor borrows; the lender is up before "ready"."""
    lent = rt.Lender({"interface": "wg0", "dns": []}, "wg", path=folder / "lend.sock").start()
    loaded = []

    class Runner(rt.Runner):
        def run(self, command, data=None, check=True):
            loaded.append(data)
            return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(rt, "LEND_SOCKET", folder / "lend.sock")
    monkeypatch.setattr(rt.Borrower, "start", lambda self: self)
    monkeypatch.setattr(rt, "gather_checks", lambda payload, tunnel, runner, **kw: {})
    monkeypatch.setattr(rt, "up_wireguard", lambda *args, **kw: pytest.fail("a borrower brings up no tunnel"))
    import io

    def piped(document):
        """Standard input as the launcher gives it: the payload's line, then closed (the start ended)."""
        read, write = os.pipe()
        os.write(write, (json.dumps(document) + "\n").encode())
        os.close(write)
        return open(read)
    out = io.StringIO()
    payload = {"profile": {"name": "wg", "type": "wireguard", "wireguard": {}}, "secrets": {"lend_token": TOKEN},
               "checks": {"ip": True}, "borrow": {"run": "codex-1"}}
    try:
        with piped(payload) as stdin:
            assert rt.main(["serve"], stdin=stdin, out=out, runner=Runner()) == 0
    finally:
        lent.close()
    ready = [json.loads(line) for line in out.getvalue().splitlines() if '"ready"' in line][0]
    assert ready["tunnel"]["interface"] == "lo" and ready["tunnel"]["dns"] == [rt.BORROW_DNS]
    assert ready["tunnel"]["lent"] == {"run": "codex-1", "interface": "wg0", "resolver": rt.FALLBACK_DNS[0]}
    assert loaded[0] == rt.borrow_rules(), "the borrower's kill switch first"
    out = io.StringIO()
    with piped({**payload, "profile": {"name": "t", "type": "tor", "tor": {}}}) as stdin:
        assert rt.main(["serve"], stdin=stdin, out=out, runner=Runner()) == 1
    assert "only a serving container of a VPN profile that is no Tor profile borrows" in out.getvalue()
    # A lender whose folder is not there (no mount) fails the start before "ready": its servers would reach nothing.
    monkeypatch.setattr(rt, "LEND_SOCKET", folder / "missing" / "lend.sock")
    monkeypatch.setattr(rt, "up_wireguard", lambda payload, runner: {"interface": "wg0", "dns": ["10.66.0.1"]})
    out = io.StringIO()
    lending = {"profile": {"name": "wg", "type": "wireguard", "wireguard": {}}, "secrets": {"lend_token": TOKEN},
               "lend": True}
    with piped(lending) as stdin:
        assert rt.main(["serve"], stdin=stdin, out=out, runner=Runner()) == 1
    assert '"event": "ready"' not in out.getvalue() and "the lent tunnel's socket" in out.getvalue()
    # ... and with it, the lender answers once "ready" is said.
    monkeypatch.setattr(rt, "LEND_SOCKET", folder / "lend.sock")
    out = io.StringIO()
    with piped(lending) as stdin:
        assert rt.main(["serve"], stdin=stdin, out=out, runner=Runner()) == 0
    ready = [json.loads(line) for line in out.getvalue().splitlines() if '"ready"' in line][0]
    assert ready["tunnel"]["lends"] is True


# --- the host's side: what is lent, the lock, the plan, the instance --------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def profile(name, kind, **extra):
    sections = {"wireguard": {"addresses": ["10.66.1.2/32"], "private_key_secret": f"VPN_{name.upper()}_KEY",
                              "preshared_key_secrets": {}, "peers": []},
                "openvpn": {"config_file": "/dev/null", "remotes": [], "auth": False},
                "tailscale": {"ephemeral": False, "exit_node": "exit.example"}, "tor": {}}
    found = {"version": 1, "name": name, "type": kind, "provider": extra.pop("provider", None),
             kind: {**sections[kind], **extra}}
    v.save_profile(found)
    return v.load_profile(name)


def test_which_profiles_a_start_lends(home):
    assert v.lends(profile("wg", "wireguard"))
    assert v.lends(profile("mv", "wireguard", provider="mullvad"))
    assert v.lends(profile("ts", "tailscale"))
    assert not v.lends(profile("tsn", "tailscale", exit_node=None)), "carries only the tailnet: no way out"
    assert not v.lends(profile("tse", "tailscale", ephemeral=True)), "a new node in each sandbox"
    assert not v.lends(profile("tor", "tor")), "each sandbox its own Tor"
    assert not v.lends(profile("ovpn", "openvpn")), "OpenVPN: a tunnel of its own (with the warning), as before"


def test_the_lent_tunnels_folder_is_among_the_private_paths_no_sandbox_may_see(home):
    profile("wg", "wireguard")
    folder = v.lend_dir("codex-1a2b3c4d")
    assert any(place in folder.parents for place in v.private_paths())
    with pytest.raises(ValueError, match="where the VPN profiles keep"):
        v.check_paths([folder], "--mount")
    with pytest.raises(ValueError, match="not a run's name"):
        v.lend_dir("../escape")
    assert v.lend_mount("codex-1", lender=True) == ["--mount", f"type=bind,src={v.lend_dir('codex-1')}/socket,dst="
                                                               "/run/sandbox-ai-lend"]
    assert v.lend_mount("codex-1")[1].endswith(",ro"), "a borrower cannot replace the lender's socket"
    assert v.lend_mount("codex-1", machine=True) == [
        "--mount", "type=volume,src=sandbox-ai-lend-codex-1,dst=/run/sandbox-ai-lend,ro"], "macOS: in the VM"


def test_a_borrowers_container_has_no_tunnel_no_key_and_no_node_state(home):
    ts = profile("ts", "tailscale")
    lender = v.container_command(["podman"], "agent-x-vpn", ts, pod="agent-x", lend=("lender", "x", False))
    borrower = v.container_command(["podman"], "agent-mcp-f-vpn", ts, pod="agent-mcp-f", lend=("borrower", "x", False))
    assert "--device=/dev/net/tun" in lender and f"type=bind,src={v.tailscale_state(ts)},dst=/var/lib/tailscale,rw" \
        in lender
    assert "--device=/dev/net/tun" not in borrower and not any("tailscale" in token for token in borrower[:-6])
    assert "--cap-add=NET_RAW" not in borrower and "--cap-add=NET_BIND_SERVICE" in borrower


def test_a_sandbox_behind_a_borrowed_tunnel_gets_the_relays_rules_and_resolver(home):
    l = m.launcher()
    profile("wg", "wireguard")
    a = l.parse_args(["codex", "--vpn", "wg", "--vpn-lent-by", "codex-1", "--mcp-socket-dir", str(home / "sock"),
                      "--egress-all-allow"])
    v.validate(a)
    assert a.vpn_dns == [rt.BORROW_DNS] and a.vpn_mark is None
    assert a.vpn_firewall == [f"ct status dnat ip daddr 127.0.0.1 tcp dport {rt.BORROW_PORT} meta skuid "
                              f"{l.PROXY_UID} accept", f"ip daddr 127.0.0.1 tcp dport {rt.BORROW_PORT} drop"]
    closed = l.parse_args(["codex", "--vpn", "wg", "--vpn-lent-by", "codex-1", "--mcp-socket-dir", str(home / "s")])
    v.validate(closed)
    assert closed.vpn_firewall[-1] == f"ip daddr {rt.BORROW_DNS} drop", "its resolver only where DNS is allowed"
    for argv, why in ((["--no-proxy", "--mcp-socket-dir", str(home / "n")], "drop --no-proxy"),
                      ([], "for the sandbox of an MCP server")):
        with pytest.raises(ValueError, match=why):
            v.validate(l.parse_args(["codex", "--vpn", "wg", "--vpn-lent-by", "codex-1", *argv]))
    with pytest.raises(ValueError, match="goes with --vpn"):
        v.validate(l.parse_args(["codex", "--vpn-lent-by", "codex-1"]))
    profile("tor", "tor")
    with pytest.raises(ValueError, match="is not lent"):
        v.validate(l.parse_args(["codex", "--vpn", "tor", "--vpn-lent-by", "codex-1", "--mcp-socket-dir",
                                 str(home / "t")]))
    assert "--vpn-lent-by" in l.CONFIG_FORBIDDEN, "a project file cannot set it"


def start_with(home, vpn, servers, run="codex-1a2b3c4d"):
    l = m.launcher()
    a = l.parse_args(["codex", "--vpn", vpn])
    a.vpn_profile, a.project_document, a.run_id = v.load_profile(vpn), None, run
    m.definitions = lambda project=None, servers=servers: servers
    return a


def test_who_borrows_the_starts_tunnel_and_who_keeps_its_own(home, monkeypatch):
    for name, kind in (("wg", "wireguard"), ("wg2", "wireguard"), ("tor", "tor"), ("ovpn", "openvpn")):
        profile(name, kind)
    monkeypatch.setattr(m, "definitions", m.definitions)
    servers = {"fetch": FETCH, "own-same": {"command": ["x"], "options": ["--vpn", "wg"]},
               "own-other": {"command": ["x"], "options": ["--vpn", "wg2"]},
               "offline": {"command": ["x"], "options": ["--offline"]}}
    behind = m.vpn_for_servers(start_with(home, "wg", servers), list(servers))
    assert m.lent_of(behind["fetch"]) == (True, "codex-1a2b3c4d") and behind["fetch"] == "wg"
    assert m.lent_of(behind["own-same"]) == (True, "codex-1a2b3c4d"), "its own profile is the start's: not twice"
    assert behind["own-other"] is None and behind["offline"] is None, "another profile: a tunnel of its own of that"
    for vpn in ("tor", "ovpn"):
        found = m.vpn_for_servers(start_with(home, vpn, {"fetch": FETCH}), ["fetch"])
        assert found["fetch"] == vpn and not m.lent_of(found["fetch"])[0]
    no_proxy = {"command": ["x"], "options": ["--no-proxy"]}
    with pytest.raises(ValueError, match="has --no-proxy.*lent to it"):
        m.vpn_for_servers(start_with(home, "wg", {"np": no_proxy}), ["np"])


def test_the_starts_checks_no_longer_refuse_a_networked_server_behind_its_wireguard_vpn(home, monkeypatch):
    """The refusal of 0a12c4d5 ("would need a tunnel of its own") is gone: the server borrows the tunnel."""
    profile("wg", "wireguard")
    monkeypatch.setattr(m, "definitions", m.definitions)
    a = start_with(home, "wg", {"fetch": FETCH})
    problems, instances, notes = m.start_problems(a, ["fetch"], {"fetch": FETCH}, present=lambda image: True)
    assert problems == [] and instances == {}
    assert any("through the start's tunnel of wg, lent to it" in note for note in notes)
    assert not hasattr(m, "second_tunnel")


def test_a_borrowed_instance_serves_its_run_alone(home, monkeypatch):
    profile("wg", "wireguard")
    monkeypatch.setattr(m, "definitions", m.definitions)
    a = start_with(home, "wg", {"fetch": FETCH})
    first = m.start_instance("fetch", a, lent_by="codex-1")[0]
    second = m.start_instance("fetch", a, lent_by="codex-2")[0]
    assert len({first, second, "fetch"}) == 3 and m.server_of(first) == m.server_of(second) == "fetch"
    # One that borrows another run's tunnel (or none) is not this one's way out.
    proc = home / "proc"
    m.save_state(first, {"pid": os.getpid(), "vpn": "wg", "lent_by": "codex-1"})
    monkeypatch.setattr(m, "status", lambda name: "running" if name == first else "stopped")
    m.check_running_vpn(first, m.LentVpn("wg", "codex-1"))
    with pytest.raises(ValueError, match="behind the tunnel of the run codex-1, lent to it"):
        m.check_running_vpn(first, m.LentVpn("wg", "codex-2"))
    with pytest.raises(ValueError, match="behind the tunnel of the run codex-1"):
        m.check_running_vpn(first, "wg")
    assert m.lent_instances("codex-1") == [first] and m.lent_instances("codex-2") == []
    stopped = []
    monkeypatch.setattr(m, "stop_locked", lambda name, timeout=30, quiet=False: stopped.append(name))
    assert m.stop_lent("codex-1") == [first] and stopped == [first]
    assert not proc.exists()


def test_a_borrowed_tunnel_from_a_start_that_does_not_run_starts_nothing(home, monkeypatch):
    profile("wg", "wireguard")
    monkeypatch.setattr(m, "definitions", m.definitions)
    a = start_with(home, "wg", {"fetch": FETCH}, run=None)
    behind = m.vpn_for_servers(a, ["fetch"])
    assert m.lent_of(behind["fetch"]) == (True, None)
    with pytest.raises(ValueError, match="lends it only while it runs"):
        m.ensure_running("fetch", a, behind["fetch"])
    plan = m.server_network("fetch", FETCH, behind["fetch"])
    with pytest.raises(ValueError, match="lends it only while it runs"):
        m.server_command("fetch", plan, a, home, home, None)


def test_mcp_start_for_a_running_start_borrows_its_tunnel_and_for_a_stopped_one_waits(home, monkeypatch):
    """`mcp start NAME --for START` (attach with start_server): a running start lends its tunnel (its run); one that
    does not run starts nothing now - the server comes up with its next start."""
    profile("wg", "wireguard")
    started = []
    monkeypatch.setattr(m, "ensure_running", lambda name, a, vpn=None: started.append((name, vpn, a.run_id)) or
                        "started")
    monkeypatch.setattr(m, "definitions", lambda project=None: {"fetch": FETCH})

    class Starts:
        def __init__(self, state):
            self.state = state

        def find(self, start):
            return {"name": "work", "kind": "agent", "state": self.state, "directory": "/x", "id": "work-1",
                    "run": "codex-77" if self.state == "running" else None, "mcp_running": []}

        def module(self, name):
            return argparse.Namespace(load=lambda key: {"options": ["--vpn", "wg"]}, add_mcp=lambda key, names: None)

        def mcp_names(self, options):
            return []
    l = m.launcher()

    def arguments(data, extra):
        a = l.parse_args(["codex", "--vpn", "wg", *extra])
        a.project_document = None
        return a
    monkeypatch.setattr(m, "start_arguments", arguments)
    said = []
    monkeypatch.setattr(m, "starts_module", lambda: Starts("running"))
    m.attach("fetch", "work", start_server=True, out=said.append)
    assert len(started) == 1 and m.lent_of(started[0][1]) == (True, "codex-77") and started[0][2] == "codex-77"
    for explicit in ("wg",):  # mcp start fetch --for work --vpn wg: the start's own profile is its tunnel, borrowed
        m.attach("fetch", "work", start_server=True, vpn=explicit, out=said.append)
        assert m.lent_of(started[-1][1]) == (True, "codex-77")
    started.clear()
    monkeypatch.setattr(m, "starts_module", lambda: Starts("stopped"))
    m.attach("fetch", "work", start_server=True, out=said.append)
    assert started == [] and any("comes up with work's next start" in line for line in said)


def test_on_macos_and_with_a_remote_engine_the_lent_tunnel_lies_in_a_podman_volume(home, monkeypatch):
    """macOS: the start's and its servers' VPN containers all run in podman machine's VM, where a socket of the Mac's
    folders cannot be reached - the lent tunnel's socket lies in a Podman volume there (so with --podman-url: on
    that engine). Gone with the start; a server's VPN container mounts it read-only."""
    import contextlib
    l = m.launcher()
    profile("wg", "wireguard")
    local = argparse.Namespace(backend="podman", podman_url=None, run_id="codex-1")
    remote = argparse.Namespace(backend="podman", podman_url="ssh://core@machine/run/podman.sock", run_id="codex-2")
    assert v.lends_by_volume(remote) and v.lends_by_volume(local) == (sys.platform == "darwin")
    monkeypatch.setattr(l, "on_machine", lambda a: True)
    removed = []
    monkeypatch.setattr(v, "podman_run", lambda command, **kw: removed.append(command))
    with contextlib.ExitStack() as cleanup:
        lend, token = v.prepare_lending(local, ["podman"], cleanup)
        assert lend == ("lender", "codex-1", True) and local.vpn_lends
        command = v.container_command(["podman"], "agent-codex-1-vpn", v.load_profile("wg"), pod="agent-codex-1",
                                      lend=lend)
        mount = "type=volume,src=sandbox-ai-lend-codex-1,dst=/run/sandbox-ai-lend"
        assert command[command.index(mount) - 1] == "--mount"
        # On this host: the run's folder with its token only (the servers' launchers read it here) - no container
        # mounts anything of it.
        assert v.lend_token("codex-1") == token and not any(str(v.lend_dir("codex-1")) in part for part in command)
        assert v.lent_tunnel_up("codex-1", machine=True), \
            "the socket is in the VM: its VPN container finds it, or says it is not there"
    assert removed == [["podman", "volume", "rm", "--force", "sandbox-ai-lend-codex-1"]]
    assert not v.lend_dir("codex-1").exists(), "gone with the start"
    assert not v.lent_tunnel_up("codex-1", machine=True), "a run that ended lends nothing: no token of it is left"
    borrower = v.container_command(["podman"], "agent-mcp-f-vpn", v.load_profile("wg"), pod="agent-mcp-f",
                                   lend=("borrower", "codex-1", True))
    assert mount + ",ro" in borrower


def test_a_catalog_server_cannot_borrow_a_tunnel_lent_on_a_remote_engine(home, monkeypatch):
    profile("wg", "wireguard")
    image = {"image": "ghcr.io/x/fetch@sha256:" + "a" * 64, "network": "full"}
    monkeypatch.setattr(m, "definitions", m.definitions)
    a = start_with(home, "wg", {"img": image, "cmd": FETCH})
    a.podman_url = "ssh://core@host/run/podman.sock"
    with pytest.raises(ValueError, match="catalog server.*lent on the engine of --podman-url"):
        m.vpn_for_servers(a, ["img"])
    assert m.lent_of(m.vpn_for_servers(a, ["cmd"])["cmd"])[0], "a command server's sandbox runs on that engine too"
