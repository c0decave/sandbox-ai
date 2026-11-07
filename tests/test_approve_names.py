""""Allow always" for a connection remembers the host name when it can be proved, else the address.

A connection question carries the name the agent sent (TLS SNI or the Host header): the agent's claim. It counts
only when sandbox-ai pinned that name to the address itself, or when the certificate of the approved connection,
checked against the system's CAs, is valid for it. The question says beforehand which it will be. Later starts
resolve a remembered name like --allow-host, but open only public addresses (or ones inside the administrator's
allow_within). The supervisor and the proxy are the real code on real sockets with real TLS; the firewall is a
stand-in that opens what the broker allows."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import threading
import time

import pytest

import test_proxy as tp
from test_proxy import pki  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
rt = tp.rt


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_names_test", "agent_sandbox.py")
ap = load("approvals_names_test", "approvals.py")
needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
GITHUB = "140.82.121.4"  # a public address (TEST-NET ranges do not count as public)


class Network:
    """The firewall for these tests: what the broker allows, the proxy may reach."""

    def __init__(self, config=None):
        self.config, self.changes = config, []

    def allow_once(self, address, port):
        self.change({"op": "allow", "tcp": [[address, port]]})

    def change(self, request, ssh=True):
        self.changes.append(request)
        if self.config is not None:
            self.config["allow"].setdefault("loopback", []).extend(port for _, port in request.get("tcp", []))


def broker(tmp_path, network=None, **options):
    (tmp_path / "hosts").write_text("127.0.0.1 localhost\n")
    return rt.ApprovalBroker(tmp_path / "approvals", network or Network(), timeout=20, poll=0.02,
                             hosts_file=tmp_path / "hosts", **options)


def connect(host, address=GITHUB, port=443, protocol="tls"):
    destination = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    return {"destination": destination, "address": address, "port": port, "protocol": protocol,
            **({"host": host} if host else {})}


# --- what the question says "Allow always" will remember ----------------------------------------------------

def test_a_name_counts_only_where_it_can_be_proved(tmp_path):
    b = broker(tmp_path, no_inspect=["*.passthrough.example"])
    on_connection = {"host": "api.github.com", "verified": "on connection", "tcp": f"{GITHUB}:443"}
    assert b.remembers(connect("api.github.com")) == on_connection
    # Case and trailing dots are one name.
    assert b.remembers(connect("API.GitHub.com.")) == on_connection

    def address_only(details):
        verdict = b.remembers(details)
        assert set(verdict) == {"tcp", "why"}, verdict
        return verdict["why"]
    assert "an address" in address_only(connect(GITHUB))                     # an IP literal as SNI
    assert "an address" in address_only(connect("2001:db8::1"))
    assert "not a dotted host name" in address_only(connect("intranet"))    # one label: nothing to remember
    assert "no host name" in address_only(connect(None))
    assert "port 443" in address_only(connect("api.github.com", port=8443))
    assert "not a public address" in address_only(connect("router.example", address="192.168.1.1"))
    assert "--no-inspect" in address_only(connect("a.passthrough.example"))
    assert "not TLS" in address_only(connect("api.github.com", port=443, protocol="http"))
    assert "an address" in address_only(connect("bad name\n"))              # not a host name at all


def test_a_name_sandbox_ai_pinned_or_a_certificate_proved_is_verified_beforehand(tmp_path):
    b = broker(tmp_path)
    (tmp_path / "hosts").write_text(f"127.0.0.1 localhost\n{GITHUB} api.github.com\n# {GITHUB} evil.example\n")
    assert b.remembers(connect("api.github.com"))["verified"] == "pinned"
    assert b.remembers(connect("evil.example"))["verified"] == "on connection"  # a comment pins nothing
    # Pinned to another address: no proof for this one.
    assert b.remembers(connect("api.github.com", address="140.82.121.5"))["verified"] == "on connection"
    b.prove("140.82.121.5", 443, "API.GitHub.com.", True)  # an earlier approved connection proved it
    assert b.remembers(connect("api.github.com", address="140.82.121.5"))["verified"] == "certificate"
    b.prove("140.82.121.6", 443, "api.github.com", False)  # a failed check proves nothing
    assert b.remembers(connect("api.github.com", address="140.82.121.6"))["verified"] == "on connection"


def test_the_proof_is_awaited_and_a_failed_or_missing_one_leaves_the_address(tmp_path):
    b = broker(tmp_path, verify_seconds=0.3)
    remember = b.remembers(connect("api.github.com"))
    threading.Timer(0.05, b.prove, args=(GITHUB, 443, "api.github.com", True)).start()
    assert b.await_proof(connect("api.github.com"), remember) == {
        "host": "api.github.com", "verified": "certificate", "tcp": f"{GITHUB}:443"}
    other = connect("www.example.org", address="93.184.215.14")
    b.prove("93.184.215.14", 443, "www.example.org", False)
    assert b.await_proof(other, b.remembers(other)) == {
        "tcp": "93.184.215.14:443", "why": "the server's certificate did not prove the name"}
    late = connect("late.example.org", address="93.184.215.15")
    started = time.monotonic()
    assert b.await_proof(late, b.remembers(late))["why"] == "no certificate proved the name in time"
    assert time.monotonic() - started < 2
    # A certificate for another name proves nothing for this one.
    b.prove("93.184.215.16", 443, "other.example.org", True)
    wrong = connect("wanted.example.org", address="93.184.215.16")
    assert "tcp" in b.await_proof(wrong, b.remembers(wrong)) and "host" not in b.await_proof(wrong, b.remembers(wrong))


def test_the_question_file_says_what_always_remembers(tmp_path):
    b = broker(tmp_path)
    threading.Thread(target=b.ask, args=("connect", connect("api.github.com")), daemon=True).start()
    for _ in range(200):
        files = list((tmp_path / "approvals").glob("*.json"))
        if files:
            break
        time.sleep(0.01)
    question = json.loads(files[0].read_text())
    assert question["remembers"] == {"host": "api.github.com", "verified": "on connection", "tcp": f"{GITHUB}:443"}
    assert ap.describe(question) == (
        f"connect to {GITHUB}:443 (tls), named api.github.com by the agent (not verified); Allow always: "
        f"api.github.com if its certificate proves it on this connection, else {GITHUB}:443 (address only)")
    for remembers, text in (
            ({"host": "a.example", "verified": "certificate", "tcp": "x"}, "Allow always: a.example (verified by its certificate)"),
            ({"host": "a.example", "verified": "pinned", "tcp": "x"}, "Allow always: a.example (verified: sandbox-ai pinned"),
            ({"tcp": f"{GITHUB}:443", "why": "not TLS"}, f"Allow always: {GITHUB}:443 (address only: not TLS)"),
            ({"tcp": f"{GITHUB}:443"}, "(address only: the name is the agent's claim and could not be verified)"),
            ({"tcp": "1.2.3.4:443", "host": "evil‮example", "verified": "certificate"}, "evil\\u202eexample")):
        assert text in ap.describe({**question, "remembers": remembers})
    assert "Allow always" not in ap.describe({**question, "remembers": None})  # questions of older versions


# --- the whole way: question, answer, approved connection, certificate, what is remembered -------------------

def upstream_with(pki, names):  # noqa: F811
    """An HTTPS server whose certificate (from the test's upstream CA) names NAMES (wildcards allowed)."""
    run = Path(pki["run"]).parent / "upstream-run"
    path = run / "certs" / f"multi-{abs(hash(tuple(names)))}.pem"
    path.parent.mkdir(exist_ok=True)
    rt.run_openssl(["req", "-x509", "-new", "-key", str(run / "leaf.key"), "-out", str(path), "-days", "30",
                    "-subj", f"/CN={names[0]}", "-config", str(run / "openssl.cnf"), "-extensions", "leaf",
                    "-CA", str(run / "ca.pem"), "-CAkey", str(run / "ca.key")],
                   san=",".join(f"DNS:{name}" for name in names))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(path), str(run / "leaf.key"))
    return tp.serve(tp.Handler, context)


def approve_always(tmp_path, pki, upstream, sni, verify_seconds=10):  # noqa: F811
    """The agent connects with SNI to a destination not allowed; the human answers "Allow always". Returns the
    question and the supervisor's .done (what is to be remembered)."""
    port = upstream.server_address[1]
    proxy = tp.Proxy(tmp_path, pki, port, {"allow": {"loopback": []}})
    b = broker(tmp_path, Network(proxy.server.config), public=lambda ip: True, name_ports=(port,),
               verify_seconds=verify_seconds)
    proxy_end, supervisor_end = socket.socketpair()
    threading.Thread(target=rt.serve_asks, args=(supervisor_end, b), daemon=True).start()
    proxy.server.asker = proxy.server.who = rt.AskChannel(proxy_end)
    asked = []

    def human():
        for _ in range(1000):
            files = list((tmp_path / "approvals").glob("*.json"))
            if files:
                asked.append(json.loads(files[0].read_text()))
                files[0].with_suffix(".answer").write_text(json.dumps({"decision": "allow", "scope": "always"}))
                return
            time.sleep(0.01)
    threading.Thread(target=human, daemon=True).start()
    try:
        context = ssl.create_default_context(cafile=str(pki["ca"]))
        with socket.create_connection(("127.0.0.1", proxy.port)) as raw, \
                context.wrap_socket(raw, server_hostname=sni) as tls:
            tls.settimeout(20)
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {sni}\r\nConnection: close\r\n\r\n".encode())
            answer = tls.recv(100)
    except (ssl.SSLError, OSError):
        answer = b""  # the proxy closed it: the real server's certificate was not valid for that name
    finally:
        proxy.close()
    done_path = (tmp_path / "approvals" / asked[0]["id"]).with_suffix(".done")
    for _ in range(1000):
        if done_path.exists():
            break
        time.sleep(0.02)
    return asked[0], json.loads(done_path.read_text()), answer


@needs_openssl
def test_a_certificate_valid_for_the_name_remembers_the_name(tmp_path, pki):  # noqa: F811
    upstream = upstream_with(pki, ["example.test"])
    try:
        question, done, answer = approve_always(tmp_path, pki, upstream, "example.test")
    finally:
        upstream.shutdown()
    assert answer.startswith(b"HTTP/1.1 200")  # the approved connection went through, unhindered by the check
    assert question["remembers"]["verified"] == "on connection"
    assert done["decision"] == "allow" and done["remember"]["host"] == "example.test"
    assert done["remember"]["verified"] == "certificate"


@needs_openssl
def test_an_sni_the_certificate_does_not_name_remembers_the_address(tmp_path, pki):  # noqa: F811
    """The agent claims api.github.com at an address whose certificate is for another name."""
    upstream = upstream_with(pki, ["evil.test"])
    try:
        question, done, answer = approve_always(tmp_path, pki, upstream, "api.github.com")
    finally:
        upstream.shutdown()
    assert not answer.startswith(b"HTTP/1.1 200")
    assert "host" not in done["remember"] and done["remember"]["why"] == "the server's certificate did not prove the name"
    assert done["remember"]["tcp"] == question["remembers"]["tcp"]


@needs_openssl
@pytest.mark.parametrize("sni,proved", [("api.example.test", True), ("example.test", False),
                                        ("a.b.example.test", False)], ids=["below", "apex", "two-below"])
def test_a_wildcard_certificate_proves_only_the_names_it_covers(tmp_path, pki, sni, proved):  # noqa: F811
    upstream = upstream_with(pki, ["*.example.test"])
    try:
        _, done, _ = approve_always(tmp_path, pki, upstream, sni, verify_seconds=3)
    finally:
        upstream.shutdown()
    # The name the agent sent is remembered, never the wildcard.
    assert done["remember"].get("host") == (sni if proved else None), done


# --- the host side: what is remembered, with where it came from ---------------------------------------------

@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(ap, "policy_allows_host", lambda name: True)
    directory = tmp_path / "runs" / "claude-1"
    (directory / "approvals").mkdir(parents=True)
    return {"claude-1": {"run": "claude-1", "directory": str(directory), "workspace": str(tmp_path / "app"),
                         "pid": os.getpid()}}


def answered(run, question, done):
    """Ask QUESTION, answer it "Allow always" as the CLI does; the sandbox's side writes DONE."""
    directory = Path(run["claude-1"]["directory"]) / "approvals"
    (directory / f"{question['id']}.json").write_text(json.dumps({"time": "now", "kind": "connect", **question}))

    def sandbox():
        for _ in range(500):
            if (directory / f"{question['id']}.answer").exists():
                (directory / f"{question['id']}.done").write_text(json.dumps({"id": question["id"], **done}))
                return
            time.sleep(0.01)
    threading.Thread(target=sandbox, daemon=True).start()
    lines = []
    ap.answer(question["id"], "allow", "always", runs=run, out=lines.append)
    return lines[0]


QUESTION = {"id": "1-2-3", **connect("api.github.com"), "remembers": {
    "host": "api.github.com", "verified": "on connection", "tcp": f"{GITHUB}:443"}}


def test_a_proved_name_is_remembered_with_where_it_came_from(run, tmp_path):
    line = answered(run, QUESTION, {"decision": "allow", "scope": "always", "remember": {
        "host": "api.github.com", "verified": "certificate", "tcp": f"{GITHUB}:443"}})
    assert "remembered as the name api.github.com (verified by its certificate)" in line
    assert ap.approved_for(tmp_path / "app") == {"hosts": ["api.github.com"], "tcp": []}
    details = ap.remembered()[str((tmp_path / "app").resolve())]["details"]["hosts:api.github.com"]
    assert details["question"] == "1-2-3" and details["verified"] == "certificate"
    assert details["destination"] == f"{GITHUB}:443" and details["added"].startswith("20")
    lines = []
    ap.main(["remembered"], out=lines.append)
    assert lines[1].startswith("  allow api.github.com  name, verified by its certificate; added 20")
    assert lines[1].endswith(f"from question 1-2-3 (connect to {GITHUB}:443 (tls), named api.github.com by the agent "
                             "(not verified))")
    assert ap.forget(str(tmp_path / "app"), "api.github.com")
    assert json.loads(ap.approved_file().read_text()) == {}  # its details go with it


@pytest.mark.parametrize("done,why", [
    ({"remember": {"tcp": f"{GITHUB}:443", "why": "the server's certificate did not prove the name"}},
     "the server's certificate did not prove the name"),
    ({}, "the name could not be verified"),  # a supervisor of an older version
    # Defence in depth: a "proof" for another name than the question offered, or a mere claim, is not taken.
    ({"remember": {"host": "evil.example", "verified": "certificate"}}, "the name could not be verified"),
    ({"remember": {"host": "api.github.com", "verified": "on connection"}}, "the name could not be verified"),
], ids=["not-proved", "older-supervisor", "other-name", "unproved-claim"])
def test_what_was_not_proved_is_remembered_as_the_address(run, tmp_path, done, why):
    line = answered(run, QUESTION, {"decision": "allow", "scope": "always", **done})
    assert f"remembered as the address {GITHUB}:443: {why}" in line
    assert ap.approved_for(tmp_path / "app") == {"hosts": [], "tcp": [f"{GITHUB}:443"]}
    details = ap.remembered()[str((tmp_path / "app").resolve())]["details"][f"tcp:{GITHUB}:443"]
    assert details["why"] == why and "verified" not in details


def test_the_policy_keeps_a_name_it_does_not_list_out(run, tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "policy_allows_host", lambda name: False)
    line = answered(run, QUESTION, {"decision": "allow", "scope": "always", "remember": {
        "host": "api.github.com", "verified": "certificate", "tcp": f"{GITHUB}:443"}})
    assert "not among the hosts the administrator's policy allows" in line
    assert ap.approved_for(tmp_path / "app") == {"hosts": [], "tcp": [f"{GITHUB}:443"]}


def test_policy_allows_host_reads_the_ceiling(monkeypatch):
    monkeypatch.setattr(ap, "launcher_module", lambda: s)
    monkeypatch.setattr(s, "load_policy", lambda: {"version": 1, "allow_within": ["10.0.0.0/8"],
                                                   "allow_hosts": ["*.corp.example"]})
    assert ap.policy_allows_host("git.corp.example") and not ap.policy_allows_host("api.github.com")
    monkeypatch.setattr(s, "load_policy", lambda: {})
    assert ap.policy_allows_host("api.github.com")


# --- later starts: a remembered name opens only what an approval may open ----------------------------------

def args(policy=None):
    a = s.parse_args(["claude"])
    a.policy = policy or {}
    return a


def test_a_remembered_name_resolves_to_public_addresses_only(capsys):
    table = {"api.github.com": ["140.82.121.4", "10.0.0.7"],      # one rebound into the LAN: dropped
             "rebound.example": ["192.168.1.10", "127.0.0.1"],     # DNS rebinding: nothing public left
             "gone.example": None}                                 # does not resolve any more

    def resolve(names):
        if any(table[name] is None for name in names):
            raise ValueError("Cannot resolve allowed host gone.example")
        return {name: table[name] for name in names}
    pins, notes = s.remembered_pins(args(), list(table), resolve)
    assert pins == {"api.github.com": ["140.82.121.4"]}
    assert any("api.github.com (approved always): not opened, not public: 10.0.0.7" in n for n in notes)
    assert any("rebound.example (approved always) is not opened: it resolves only to addresses that are not public"
               in n for n in notes)
    assert any("gone.example (approved always) is not opened: it does not resolve now" in n for n in notes)


def test_inside_the_administrators_networks_a_private_address_is_kept():
    policy = {"allow_within": ["10.0.0.0/8"], "allow_hosts": ["*.corp.example"]}
    pins, notes = s.remembered_pins(args(policy), ["git.corp.example"],
                                    lambda names: {"git.corp.example": ["10.1.2.3", "192.168.0.9"]})
    assert pins == {"git.corp.example": ["10.1.2.3"]} and "192.168.0.9" in notes[0]


def test_a_start_applies_remembered_names_apart_from_the_options(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "work").mkdir()
    ap.remember(tmp_path / "work", host="api.github.com")       # also in --allow-github: no special handling
    ap.remember(tmp_path / "work", host="rebound.example")
    a = s.parse_args(["claude", "--workspace", str(tmp_path / "work"), "--allow-github"])
    a.policy = {}
    s.apply_remembered_approvals(a)
    assert a.remembered_hosts == ["rebound.example"] and "rebound.example" in a.allow_host
    pins = s.apply_remembered_pins(a, a.remembered_hosts, lambda names: {"rebound.example": ["127.0.0.1"]})
    assert pins == {} and "rebound.example" not in a.allow_host and "rebound.example" not in s.allowed_hosts(a)
    assert "rebound.example (approved always) is not opened" in capsys.readouterr().err
