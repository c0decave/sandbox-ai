"""Burp Suite as an MCP server (burp.py, mcp_http.py; the catalog entry burp of sandbox-ai-mcp), with a stand-in for
PortSwigger's MCP extension on loopback (its Streamable HTTP endpoint as its SDK serves it, its Host check):

- host mode (the default): `mcp add burp` writes a definition reached at its endpoint (network none, no folders, no
  variables, no VPN), checks the endpoint and keeps the edition (Professional by its scanner and Collaborator tools);
  the relay runs mcp_http.py on this host for each connection - never a container, never the port in a sandbox;
  a bind address other than loopback is warned about; nothing listening is a note at a start, not a problem;
- container mode: your jar (--burp-jar) linked into the server's own /burp, its SHA-256 and edition passed as plain
  values, BURP_LICENSE as a secret (Professional only, and refused when the secret is not there), /data writable;
- mcp_http.py: the client's initialize forwarded with the session it opens, JSON and SSE answers, a call that waits
  (Burp's approval) not holding a ping, a session the extension forgot opened again with the client's initialize,
  an endpoint that does not answer said as an MCP error, only loopback http URLs;
- `mcp burp extension`: the jar from the image (podman create/cp) or rootfs, checked against the image's SHA-256.
"""
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import test_mcp as tm
from test_mcp import isolated, mcp_key, trusted_mcp_key  # noqa: F401  (fixtures)

m, DIGEST = tm.m, tm.DIGEST
B = m.burp_module()
H = B.http_module()
ROOT = Path(__file__).resolve().parent.parent
BURP = {"id": "mcp:burp", "name": "burp", "title": "Burp Suite (PortSwigger's MCP extension)", "kind": "oci",
        "image": "ghcr.io/c0decave/sandbox-ai-mcp-burp", "digest": DIGEST, "network": "full",
        "env": [{"name": "BURP_EDITION", "secret": False, "required": False},
                {"name": "BURP_LICENSE", "secret": True, "required": False},
                {"name": "BURP_JAR_SHA256", "secret": False, "required": False}],
        "mounts": [{"target": "/burp", "description": "your Burp jar, read-only (--mount /burp=DIR:ro)",
                    "required": False},
                   {"target": "/data", "description": "Burp's user folder, writable (--mount /data=DIR:rw)",
                    "required": False}]}
COMMUNITY = ["base64_decode", "get_proxy_http_history", "send_http1_request", "url_encode"]
PROFESSIONAL = COMMUNITY + ["get_scanner_issues", "generate_collaborator_payload", "get_collaborator_interactions"]


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Extension:
    """PortSwigger's MCP extension as its SDK serves /mcp (JSON answers; sse=True: SSE): a session from initialize,
    202 for notifications, 404 for a session it does not know, 403 for a Host that is not 127.0.0.1:PORT or
    localhost:PORT (KtorServerManager.isValidHost). A call of slow_tool waits for the event release (Burp's dialog)."""

    def __init__(self, tools=COMMUNITY, sse=False):
        self.tools, self.sse = list(tools), sse
        self.sessions, self.opened, self.seen, self.deleted = set(), 0, [], []
        self.release = threading.Event()
        self.port = free_port()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_DELETE(self):  # noqa: N802
                owner.deleted.append(self.headers.get("Mcp-Session-Id"))
                owner.sessions.discard(self.headers.get("Mcp-Session-Id"))
                self.answer(200, b"")

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.seen.append((dict(self.headers), body))
                if self.headers.get("Host") not in (f"127.0.0.1:{owner.port}", f"localhost:{owner.port}"):
                    return self.answer(403, b"")
                if body.get("method") == "initialize":
                    owner.opened += 1
                    sid = f"s{owner.opened}"
                    owner.sessions.add(sid)
                    return self.json({"jsonrpc": "2.0", "id": body["id"], "result": {
                        "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "burp-suite", "version": "1.1.2"}}}, {"Mcp-Session-Id": sid})
                if self.headers.get("Mcp-Session-Id") not in owner.sessions:
                    return self.answer(404, b"Session not found")
                if "id" not in body:
                    return self.answer(202, b"")
                if body["method"] == "tools/list":
                    result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in owner.tools]}
                elif body["method"] == "ping":
                    result = {}
                else:
                    if body["params"]["name"] == "slow_tool":
                        owner.release.wait(10)
                    result = {"content": [{"type": "text", "text": f"called {body['params']['name']}"}]}
                return self.json({"jsonrpc": "2.0", "id": body["id"], "result": result})

            def json(self, message, headers=None):
                if owner.sse:
                    return self.answer(200, f"event: message\ndata: {json.dumps(message)}\n\n".encode(), headers,
                                       "text/event-stream")
                return self.answer(200, json.dumps(message).encode(), headers)

            def answer(self, status, data, headers=None, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def extension():
    found = Extension()
    yield found
    found.close()


def initialize(request_id=0):
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def lines(*messages):
    return io.StringIO("".join(json.dumps(message) + "\n" for message in messages))


def answers(out):
    return {found.get("id"): found for found in (json.loads(line) for line in out.getvalue().splitlines())}


# --- mcp_http.py -------------------------------------------------------------------------------------------------------

def test_only_loopback_http_urls():
    assert H.checked_url("http://127.0.0.1:9876/mcp") == ("127.0.0.1", 9876, "/mcp")
    assert H.checked_url("http://localhost:9877/mcp")[0] == "localhost"
    assert H.checked_url("http://[::1]:9876/mcp")[0] == "::1"
    for bad in ("https://127.0.0.1:9876/mcp", "http://192.168.1.5:9876/mcp", "http://127.0.0.1/mcp",
                "http://user:pw@127.0.0.1:9876/mcp", "http://127.0.0.1:9876/mcp?x=1", "http://burp:9876/mcp",
                "http://127.0.0.1:9876/../etc", "http://127.0.0.1:9876/mcp\n", 42):
        with pytest.raises(ValueError):
            H.checked_url(bad)


@pytest.mark.parametrize("sse", [False, True])
def test_the_relay_connection_forwards_with_the_session_and_the_host_burp_wants(sse):
    fake = Extension(sse=sse)
    try:
        out = io.StringIO()
        bridge = H.Bridge(fake.url, out=out)
        bridge.serve(lines(initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "url_encode", "arguments": {"content": "a b"}}}))
        found = answers(out)
        assert found[0]["result"]["serverInfo"]["name"] == "burp-suite"
        assert [t["name"] for t in found[1]["result"]["tools"]] == COMMUNITY
        assert found[2]["result"]["content"][0]["text"] == "called url_encode"
        headers = fake.seen[-1][0]
        assert headers["Host"] == f"127.0.0.1:{fake.port}" and headers["Mcp-Session-Id"] == "s1"
        assert headers["MCP-Protocol-Version"] == "2025-06-18"
        assert "text/event-stream" in headers["Accept"] and "application/json" in headers["Accept"]
        assert not any(word in headers["User-Agent"].lower() for word in ("mozilla", "browser", "chrome"))
        assert fake.deleted == ["s1"], "the session is closed when the connection ends"
    finally:
        fake.close()


def test_a_call_burp_holds_for_approval_does_not_hold_a_ping(extension):
    out = io.StringIO()
    bridge = H.Bridge(extension.url, out=out)
    read, write = os.pipe()
    stream = os.fdopen(read, "r")
    feeder = os.fdopen(write, "w")
    worker = threading.Thread(target=bridge.serve, args=(stream,), daemon=True)
    worker.start()
    for message in (initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "slow_tool"}},
                    {"jsonrpc": "2.0", "id": 6, "method": "ping"}):
        feeder.write(json.dumps(message) + "\n")
        feeder.flush()
    deadline = time.monotonic() + 5
    while 6 not in answers(out) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert 6 in answers(out) and 5 not in answers(out), "the ping is answered while the call waits"
    extension.release.set()
    feeder.close()
    worker.join(10)
    stream.close()
    assert answers(out)[5]["result"]["content"][0]["text"] == "called slow_tool"


def test_a_session_the_extension_forgot_is_opened_again_with_the_clients_initialize(extension):
    out = io.StringIO()
    bridge = H.Bridge(extension.url, out=out)
    bridge.forward(initialize())
    extension.sessions.clear()  # the extension ends sessions idle for 30 minutes
    bridge.forward({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "url_encode"}})
    assert answers(out)[9]["result"]["content"][0]["text"] == "called url_encode"
    assert bridge.session == "s2"
    initializes = [body for _headers, body in extension.seen if body.get("method") == "initialize"]
    assert initializes[-1]["params"]["clientInfo"] == {"name": "t", "version": "1"}, "the client's own initialize"
    assert len([a for a in out.getvalue().splitlines() if '"id":0' in a]) == 1, "its answer was passed on once"


def test_an_endpoint_that_does_not_answer_is_an_mcp_error_naming_the_fix():
    out = io.StringIO()
    bridge = H.Bridge(f"http://127.0.0.1:{free_port()}/mcp", out=out)
    bridge.forward(initialize(3))
    error = answers(out)[3]["error"]
    assert error["code"] == H.NOT_ANSWERING
    assert "does not answer" in error["message"] and "sandbox-ai mcp burp extension" in error["message"]
    bridge.forward({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert len(out.getvalue().splitlines()) == 1, "a notification gets no answer"


def test_mcp_http_as_the_relay_runs_it(extension):
    """End to end: `python3 -I mcp_http.py URL` with MCP on its stdin, as runtime.py's relay starts it."""
    messages = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "not json"]
    done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "mcp_http.py"), extension.url],
                          input="".join((m_ if isinstance(m_, str) else json.dumps(m_)) + "\n" for m_ in messages),
                          capture_output=True, text=True, timeout=30)
    found = [json.loads(line) for line in done.stdout.splitlines()]
    assert done.returncode == 0, done.stderr
    assert {f.get("id") for f in found} == {0, 1, None}
    assert [f for f in found if f.get("id") is None][0]["error"]["code"] == -32700
    refused = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "mcp_http.py"), "http://10.0.0.1:9876/mcp"],
                             capture_output=True, text=True, timeout=30)
    assert refused.returncode == 2 and "loopback" in refused.stderr


# --- burp.py: the endpoint, its bind address ---------------------------------------------------------------------------

def test_the_probe_tells_community_from_professional():
    for tools, edition in ((COMMUNITY, "community"), (PROFESSIONAL, "professional")):
        fake = Extension(tools)
        try:
            found = B.probe(fake.url)
        finally:
            fake.close()
        assert found["ok"] and found["edition"] == edition and found["server"] == "burp-suite 1.1.2"
        assert "Burp Suite " + ("Professional" if edition == "professional" else "Community") in \
            B.probe_text(fake.url, found)
    nobody = B.probe(f"http://127.0.0.1:{free_port()}/mcp")
    assert not nobody["ok"] and "Extensions, Add, Java" in B.probe_text("u", nobody)


def write_proc(tmp_path, tcp="", tcp6=""):
    proc = tmp_path / "fakeproc"
    (proc / "net").mkdir(parents=True)
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (proc / "net" / "tcp").write_text(header + tcp)
    (proc / "net" / "tcp6").write_text(header + tcp6)
    return proc


def test_a_bind_address_other_than_loopback_is_warned_about(tmp_path):
    loop = "   0: 0100007F:2694 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1\n"
    wide = "   1: 00000000:2694 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2\n"
    other_port = "   2: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 3\n"
    established = "   3: 00000000:2694 0100007F:D431 01 00000000:00000000 00:00000000 00000000  1000        0 4\n"
    assert B.listeners(9876, write_proc(tmp_path / "a", loop)) == ["127.0.0.1"]
    assert B.bind_warning(9876, write_proc(tmp_path / "b", loop + other_port + established)) is None
    warning = B.bind_warning(9876, write_proc(tmp_path / "c", wide))
    assert "0.0.0.0:9876" in warning and "no authentication" in warning and "127.0.0.1" in warning
    six_any = "   0: 00000000000000000000000000000000:2694 00000000000000000000000000000000:0000 0A 0 0 0 0 0 5\n"
    six_loop = "   0: 00000000000000000000000001000000:2694 00000000000000000000000000000000:0000 0A 0 0 0 0 0 6\n"
    assert B.listeners(9876, write_proc(tmp_path / "d", "", six_loop)) == ["::1"]
    assert "::" in B.bind_warning(9876, write_proc(tmp_path / "e", "", six_any))


# --- mcp add burp ------------------------------------------------------------------------------------------------------

def test_host_mode_is_the_default_and_checks_the_endpoint(tmp_path):
    fake = Extension(PROFESSIONAL)
    try:
        said = []
        definition = m.add("burp", catalogs=tm.catalog(tmp_path, [BURP]), pull=False, out=said.append,
                           probe_url=fake.url, burp={"url": fake.url})
    finally:
        fake.close()
    text = " ".join(said)
    assert definition["endpoint"] == fake.url and definition["network"] == "none"
    assert definition["burp"]["mode"] == "host" and definition["burp"]["edition"] == "professional"
    assert not definition.get("mounts") and not m.variables_of(definition), "no folders, no variables: no container"
    assert definition["image"].endswith(DIGEST) and definition["signed_by"], "the signed image: the extension's jar"
    assert "Burp Suite Professional, 7 tools" in text
    assert "the extension jar was not written" in text or "the extension for your Burp" in text
    assert m.definitions()["burp"] == definition
    assert m.burp_module().label(definition) == f"Burp Suite Professional, host mode (127.0.0.1:{fake.port})"
    listed = [entry for entry in m.server_list() if entry["name"] == "burp"][0]
    assert listed["command"].startswith("Burp Suite Professional, host mode")


def test_host_mode_without_burp_running_is_added_with_what_to_do(tmp_path):
    said = []
    definition = m.add("burp", catalogs=tm.catalog(tmp_path, [BURP]), pull=False, out=said.append)
    assert definition["endpoint"] == B.DEFAULT_URL and definition["burp"]["mode"] == "host"
    if not B.probe(B.DEFAULT_URL)["ok"]:  # (a Burp of this machine on 9876 would answer)
        assert definition["burp"]["edition"] is None
        assert any("does not answer" in line and "Extensions, Add, Java" in line for line in said)


def test_host_mode_refuses_what_has_no_meaning_there(tmp_path):
    sources = tm.catalog(tmp_path, [BURP])
    with pytest.raises(ValueError, match="loopback"):
        m.add("burp", catalogs=sources, pull=False, out=[].append, burp={"url": "http://192.168.1.9:9876/mcp"})
    with pytest.raises(ValueError, match="--burp-jar is for container mode"):
        m.add("burp", catalogs=sources, pull=False, out=[].append, burp={"mode": "host", "jar": "/x.jar"})
    with pytest.raises(ValueError, match="for the catalog entry burp"):
        m.add("duckduckgo", catalogs=tm.catalog(tmp_path, [tm.ENTRY]), pull=False, out=[].append, burp={"mode": "host"})


def a_jar(tmp_path, name, content=b"PK\x03\x04 burp"):
    path = tmp_path / "downloads" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


def test_container_mode_links_your_jar_and_passes_its_checksum_and_edition(tmp_path):
    jar = a_jar(tmp_path, "burpsuite_community_v2026.10.jar")
    said = []
    definition = m.add("burp", catalogs=tm.catalog(tmp_path, [BURP]), pull=False, out=said.append,
                       burp={"jar": str(jar)})
    sha = hashlib.sha256(jar.read_bytes()).hexdigest()
    assert definition["burp"] == {"mode": "container", "edition": "community",
                                  "jar": {"name": jar.name, "sha256": sha, "size": jar.stat().st_size,
                                          "from": str(jar)}}
    assert "endpoint" not in definition and definition["network"] == "full"
    mounts = {mount["target"]: mount for mount in definition["mounts"]}
    burp_folder, data_folder = Path(mounts["/burp"]["host"]), Path(mounts["/data"]["host"])
    assert mounts["/burp"]["mode"] == "ro" and mounts["/data"]["mode"] == "rw"
    assert m.is_own_folder("burp", "/burp", str(burp_folder)) and m.is_own_folder("burp", "/data", str(data_folder))
    assert sorted(p.name for p in burp_folder.iterdir()) == [jar.name]
    assert (burp_folder / jar.name).read_bytes() == jar.read_bytes()
    assert definition["env_values"] == {"BURP_EDITION": "community", "BURP_JAR_SHA256": sha}
    assert (definition["memory"], definition["pids"], definition["tmp"]) == ("4g", 512, "256m"), "Burp's JVM"
    assert "env_secrets" not in definition, "Community needs no licence"
    assert any("linked into" in line or "copied into" in line for line in said)
    assert m.burp_module().label(definition) == f"Burp Suite Community, container mode ({jar.name})"


def test_professional_in_a_container_needs_the_licence_secret_and_gets_it_as_one(tmp_path, monkeypatch):
    jar = a_jar(tmp_path, "burpsuite_pro_v2026.10.jar")
    sources = tm.catalog(tmp_path, [BURP])
    monkeypatch.setattr(m, "secret_value", lambda name: None)
    with pytest.raises(ValueError, match="sandbox-ai secrets set BURP_LICENSE"):
        m.add("burp", catalogs=sources, pull=False, out=[].append, burp={"jar": str(jar)})
    assert "burp" not in m.definitions(), "nothing is written when it cannot work"
    monkeypatch.setattr(m, "secret_value", lambda name: "AAAA-BBBB" if name == "BURP_LICENSE" else None)
    said = []
    definition = m.add("burp", catalogs=sources, pull=False, out=said.append, burp={"jar": str(jar)})
    assert definition["burp"]["edition"] == "professional"
    assert definition["env_secrets"] == {"BURP_LICENSE": "BURP_LICENSE"}
    assert "AAAA-BBBB" not in json.dumps(definition) and "AAAA-BBBB" not in " ".join(said)
    assert "activates your licence online at portswigger.net once" in " ".join(said)


def test_container_mode_refuses_what_cannot_run(tmp_path):
    sources = tm.catalog(tmp_path, [BURP])
    with pytest.raises(ValueError, match="needs your Burp jar"):
        m.add("burp", catalogs=sources, pull=False, out=[].append, burp={"mode": "container"})
    with pytest.raises(ValueError, match="which edition"):
        m.add("burp", catalogs=sources, pull=False, out=[].append, burp={"jar": str(a_jar(tmp_path, "burp.jar"))})
    with pytest.raises(ValueError, match="not a jar"):
        m.add("burp", catalogs=sources, pull=False, out=[].append,
              burp={"jar": str(a_jar(tmp_path, "notes.jar", b"hello")), "edition": "community"})
    with pytest.raises(ValueError, match="--burp-url is for host mode"):
        m.add("burp", catalogs=sources, pull=False, out=[].append,
              burp={"jar": str(a_jar(tmp_path, "burpsuite_community.jar")), "url": "http://127.0.0.1:1/mcp"})
    with pytest.raises(ValueError, match="instead of --mount /burp"):
        m.add("burp", catalogs=sources, pull=False, out=[].append, mounts=[f"/burp={tmp_path}:ro"],
              burp={"jar": str(a_jar(tmp_path, "burpsuite_community.jar"))})


def test_a_hand_written_definition_keeps_the_rules():
    image = "ghcr.io/c0decave/sandbox-ai-mcp-burp@" + DIGEST
    with pytest.raises(ValueError, match="mode must be host or container"):
        m.validated_image({"image": image, "network": "none", "endpoint": B.DEFAULT_URL, "burp": {"mode": "x"}}, "t")
    with pytest.raises(ValueError, match="host mode needs its endpoint"):
        m.validated_image({"image": image, "network": "none", "burp": {"mode": "host"}}, "t")
    with pytest.raises(ValueError, match="runs no container"):
        m.validated_image({"image": image, "network": "none", "endpoint": B.DEFAULT_URL,
                           "env_values": {"A": "1"}, "burp": {"mode": "host"}}, "t")
    with pytest.raises(ValueError, match="needs its jar"):
        m.validated_image({"image": image, "network": "full", "burp": {"mode": "container", "jar": {}}}, "t")


# --- the relay and the start -------------------------------------------------------------------------------------------

def host_definition(url=B.DEFAULT_URL):
    return m.validated_image({"image": "ghcr.io/c0decave/sandbox-ai-mcp-burp@" + DIGEST, "network": "none",
                              "endpoint": url, "burp": {"mode": "host", "edition": None}}, "burp")


def test_the_relay_runs_mcp_http_on_this_host_and_no_container(tmp_path, monkeypatch):
    m.write_definition("burp", host_definition())
    started = []
    monkeypatch.setattr(m, "start_logged", lambda cmd, directory, **options: started.append((cmd, options)) or
                        SimpleNamespace(pid=os.getpid()))
    monkeypatch.setattr(m, "wait_for_socket", lambda *args, **kwargs: None)
    monkeypatch.setattr(m.launcher(), "on_machine", lambda a: False)
    a = SimpleNamespace(mcp=["burp"], backend="podman", podman_url=None, vpn=None, mcp_limits=[], mcp_mount=[],
                        mcp_env=[], workspace=str(tmp_path), project_document=None, rules=[], mode="medium",
                        start_key=None, run_id=None)
    monkeypatch.setattr(m.launcher(), "mcp_gateway", lambda *args, **kwargs: None)
    assert m.ensure_running_locked("burp", a) == "started"
    (cmd, _options), = started
    relay = cmd.index("--internal-mcp-serve")
    assert cmd[cmd.index("--", relay) + 1:] == [sys.executable, "-I", str(ROOT / "sandbox" / "mcp_http.py"),
                                                B.DEFAULT_URL]
    assert "podman" not in " ".join(cmd) and "bwrap" not in " ".join(cmd)
    assert m.load_state("burp")["backend"] == "host"


def test_nothing_listening_is_a_note_at_a_start_not_a_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    servers = {"burp": host_definition(f"http://127.0.0.1:{free_port()}/mcp")}
    monkeypatch.setattr(m.launcher(), "on_machine", lambda a: False)
    a = m.launcher().parse_args(["claude", "--mcp", "burp"])
    a.mode = "medium"
    problems, _found, notes = m.start_problems(a, ["burp"], servers, present=lambda image: False)
    assert problems == [], "no image runs in host mode: its absence is no problem"
    assert any("nothing answers on" in note and "sandbox-ai mcp burp check" in note for note in notes)


# --- the extension jar --------------------------------------------------------------------------------------------------

def image_files(tmp_path, content=b"PK extension", checksum=None):
    folder = tmp_path / "img" / "opt" / "burp-mcp"
    folder.mkdir(parents=True)
    (folder / "burp-mcp-all.jar").write_bytes(content)
    digest = checksum or hashlib.sha256(content).hexdigest()
    (folder / "burp-mcp-all.jar.sha256").write_text(f"{digest}  /opt/burp-mcp/burp-mcp-all.jar\n")
    return tmp_path / "img"


def fake_podman(root, calls):
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "create"]:
            return subprocess.CompletedProcess(argv, 0, "c0ffee\n", "")
        if argv[:2] == ["podman", "cp"]:
            source = root / argv[2].split(":", 1)[1].lstrip("/")
            if not source.exists():
                return subprocess.CompletedProcess(argv, 125, "", "no such file")
            Path(argv[3]).write_bytes(source.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def test_the_extension_jar_comes_from_the_signed_image_checked(tmp_path):
    root, calls = image_files(tmp_path), []
    written = B.extension(host_definition(), destination=tmp_path / "out" / "burp-mcp-all.jar",
                          run=fake_podman(root, calls))
    assert Path(written["path"]).read_bytes() == b"PK extension"
    assert written["sha256"] == hashlib.sha256(b"PK extension").hexdigest()
    assert calls[0][:3] == ["podman", "create", "--pull=never"] and calls[-1][:3] == ["podman", "rm", "-f"]
    bad = image_files(tmp_path / "bad", checksum="0" * 64)
    with pytest.raises(ValueError, match="does not match its SHA-256"):
        B.extension(host_definition(), destination=tmp_path / "out2" / "x.jar", run=fake_podman(bad, []))
    assert not (tmp_path / "out2" / "x.jar").exists()


def test_the_extension_jar_from_a_rootfs(tmp_path):
    root = image_files(tmp_path)
    definition = {"rootfs": {"path": str(root), "sha256": "b" * 64}, "network": "none"}
    written = B.extension(definition, destination=tmp_path / "jar" / "e.jar")
    assert written["image"].startswith("rootfs bbbb") and Path(written["path"]).read_bytes() == b"PK extension"


# --- the command line ----------------------------------------------------------------------------------------------------

def test_mcp_burp_check_and_extension_through_the_cli(tmp_path, capsys):
    fake = Extension(COMMUNITY)
    try:
        m.write_definition("burp", host_definition(fake.url))
        assert m.cli(["burp", "check", "--json"]) == 0
        found = json.loads(capsys.readouterr().out)
        assert found["ok"] and found["edition"] == "community" and found["endpoint"] == fake.url
        assert m.definitions()["burp"]["burp"]["edition"] == "community", "the edition is kept"
    finally:
        fake.close()
    assert m.cli(["burp", "check"]) == 1
    assert "does not answer" in capsys.readouterr().out
    m.write_definition("burp", {**host_definition(), "burp": {"mode": "host", "edition": None}})
    with pytest.raises(ValueError, match="no Burp server other"):
        m.cli(["burp", "extension", "--as", "other"])


def test_mcp_add_burp_end_to_end_through_sandbox_ai(tmp_path):
    """End to end: `./sandbox-ai mcp add burp` in host mode against the stand-in (the endpoint answers, Community),
    then container mode with a jar, through the real entry point and the fake Podman."""
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "h")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [BURP]}))
    fake = Extension(COMMUNITY)
    try:
        added = host.run("mcp", "add", "burp", "--catalog", str(path), "--no-pull", "--unsigned",
                         "--burp-url", fake.url)
    finally:
        fake.close()
    text = " ".join((added.stdout + added.stderr).split())
    assert added.returncode == 0, text
    assert "Burp Suite Community, 4 tools" in text and "burp added" in text
    listed = host.run("mcp", "list", "--json")
    (entry,) = [e for e in json.loads(listed.stdout) if e["name"] == "burp"]
    assert entry["command"] == f"Burp Suite Community, host mode (127.0.0.1:{fake.port})"
    jar = a_jar(tmp_path, "burpsuite_community_v2026.10.jar")
    again = host.run("mcp", "add", "burp", "--as", "burpbox", "--catalog", str(path), "--no-pull", "--unsigned",
                     "--burp-mode", "container", "--burp-jar", str(jar))
    assert again.returncode == 0, again.stderr
    assert (host.home / ".local/share/sandbox-ai/mcp-folders/burpbox/burp" / jar.name).is_file()
    refused = host.run("mcp", "add", "burp", "--as", "b2", "--catalog", str(path), "--no-pull", "--unsigned",
                       "--burp-mode", "container")
    assert refused.returncode != 0 and "needs your Burp jar" in refused.stderr
