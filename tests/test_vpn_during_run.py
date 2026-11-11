"""VPN during a run: the requests the serving container answers (lookups, connections, HTTP through the tunnel), the
address checked again while the run goes on, names allowed during the run looked up through the VPN, and
sandbox-ai llm check|models|status of a server behind a VPN. Without Podman: the container's side runs in this
process, the launcher's against stand-ins."""
import base64
import http.server
import json
import os
from pathlib import Path
import socket
import sys
import threading
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_vpn import FakeAPI, FakeRunner, certificate, home, wireguard_profile, rt, s, v  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1]


class Recorder(http.server.ThreadingHTTPServer):
    """Plain HTTP on 127.0.0.1: answers 200 with the request's path; records method, path, headers and body."""

    def __init__(self):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_one(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                             "body": self.rfile.read(length) if length else b""})
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://elsewhere.example/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = json.dumps({"path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            do_GET = do_POST = handle_one

            def log_message(self, *args):
                pass
        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


@pytest.fixture
def recorder():
    server = Recorder()
    yield server
    server.shutdown()


TUNNEL = {"interface": "wg0", "dns": ["10.66.0.1"], "ipv4": True, "ipv6": False}


# --- the container's side -------------------------------------------------------------------------------------------

def test_requests_are_answered_through_the_tunnels_resolvers(monkeypatch):
    asked = []
    monkeypatch.setattr(rt, "dns_lookup", lambda name, server, qtype, timeout=4.0, port=53:
                        asked.append((name, server)) or ["203.0.113.7"])
    answer = rt.handle_request(json.dumps({"op": "resolve", "id": 3, "names": ["llm.corp.example"]}), TUNNEL, False)
    assert answer == {"event": "answer", "id": 3, "hosts": {"llm.corp.example": ["203.0.113.7"]}}
    assert asked == [("llm.corp.example", "10.66.0.1")], "only the VPN's resolver is asked"
    # What it cannot read or does not know is an answer with an error, never an end of the tunnel.
    assert rt.handle_request("not json", TUNNEL, False)["error"].startswith("unreadable request")
    assert rt.handle_request(json.dumps({"op": "shell", "id": 4}), TUNNEL, False) == \
        {"event": "answer", "id": 4, "error": "unknown request 'shell'"}
    assert "a list of host names" in rt.handle_request(json.dumps({"op": "resolve", "id": 5, "names": "x"}),
                                                       TUNNEL, False)["error"]


def test_http_through_the_tunnel_keeps_the_key_off_plain_http_to_other_networks(recorder):
    request = {"op": "http", "id": 1, "url": recorder.url + "/v1/models", "headers": {"Authorization": "Bearer sk-1"},
               "secret": True, "plain_networks": ["10.0.0.0/8", "192.168.0.0/16"]}
    answer = rt.handle_request(json.dumps(request), TUNNEL, False, secrets=["sk-1"])
    assert "would cross networks you do not control" in answer["error"] and "sk-1" not in json.dumps(answer)
    assert recorder.seen == [], "nothing was sent"
    # The endpoint added with --insecure-http (plain_networks None), or a local address: sent.
    answer = rt.handle_request(json.dumps({**request, "plain_networks": None}), TUNNEL, False)
    assert answer["status"] == 200 and json.loads(base64.b64decode(answer["body"])) == {"path": "/v1/models"}
    assert answer["address"] == "127.0.0.1" and recorder.seen[-1]["headers"]["Authorization"] == "Bearer sk-1"
    answer = rt.handle_request(json.dumps({**request, "plain_networks": ["127.0.0.0/8"], "method": "POST",
                                           "body": base64.b64encode(b'{"x": 1}').decode()}), TUNNEL, False)
    assert answer["status"] == 200 and recorder.seen[-1]["method"] == "POST" and recorder.seen[-1]["body"] == b'{"x": 1}'
    # A redirect is answered as it is, never followed (it would carry the key elsewhere).
    answer = rt.handle_request(json.dumps({**request, "url": recorder.url + "/redirect", "plain_networks": None}),
                               TUNNEL, False)
    assert answer["status"] == 302 and recorder.seen[-1]["path"] == "/redirect"
    # Only http and https.
    assert "only http and https" in rt.handle_request(json.dumps({**request, "url": "file:///etc/passwd"}), TUNNEL,
                                                      False)["error"]


def test_connect_checks_the_certificate_with_the_endpoints_ca(certificate, monkeypatch):
    server = FakeAPI(certificate, {("GET", "/"): (200, {})})
    try:
        port = server.server.server_address[1]
        monkeypatch.setattr(rt, "dns_lookup", lambda *a, **k: ["127.0.0.1"])
        ca = (certificate / "cert.pem").read_text()
        answer = rt.handle_request(json.dumps({"op": "connect", "id": 1, "url": f"https://localhost:{port}", "ca": ca}),
                                   TUNNEL, False)
        assert answer["address"] == "127.0.0.1" and answer["tls"].startswith("TLS")
        # Without the CA the certificate is not trusted: an error, no connection made "anyway".
        refused = rt.handle_request(json.dumps({"op": "connect", "id": 2, "url": f"https://localhost:{port}"}),
                                    TUNNEL, False)
        assert "certificate" in refused["error"].lower()
    finally:
        server.close()


def serve(monkeypatch, lines, checks=None, rechecks=None):
    """rt.main serve with LINES after the payload on its standard input (then closed): the events it wrote."""
    monkeypatch.setattr(rt, "up_wireguard", lambda payload, runner: dict(TUNNEL))
    monkeypatch.setattr(rt, "gather_checks", lambda payload, tunnel, runner, get=None: {"dns": {}})
    monkeypatch.setattr(rt, "resolve_hosts", lambda names, tunnel, tailnet: {n: ["203.0.113.7"] for n in names})
    monkeypatch.setattr(rt, "save_serve_state", lambda tunnel, tailnet, path=None: None)
    if rechecks is not None:
        monkeypatch.setattr(rt, "recheck", lambda payload, tunnel, runner: rechecks.append(1) or {"ipv4": {"ip": "x"}})
    read, write = os.pipe()
    out = []

    def feed():
        os.write(write, json.dumps({"profile": {"type": "wireguard"}, "secrets": {}, "resolve": [],
                                    "checks": checks or {}}).encode() + b"\n")
        for line in lines:
            if isinstance(line, (int, float)):
                threading.Event().wait(line)
            else:
                os.write(write, line.encode() + b"\n")
        os.close(write)
    feeder = threading.Thread(target=feed)
    feeder.start()
    with os.fdopen(read) as stdin:
        assert rt.main(["serve"], stdin, types.SimpleNamespace(write=out.append, flush=lambda: None), FakeRunner()) == 0
    feeder.join()
    return [json.loads(line) for line in out]


def test_serve_answers_requests_line_by_line(monkeypatch):
    events = serve(monkeypatch, [json.dumps({"op": "resolve", "id": 1, "names": ["a.example"]}), "",
                                 json.dumps({"op": "resolve", "id": 2, "names": ["b.example"]})])
    answers = [e for e in events if e["event"] == "answer"]
    assert [(a["id"], a["hosts"]) for a in answers] == [(1, {"a.example": ["203.0.113.7"]}),
                                                         (2, {"b.example": ["203.0.113.7"]})]


def test_serve_checks_the_address_again_during_the_run(monkeypatch):
    rechecks = []
    # A recheck every 0.2 s while the launcher sends nothing for 0.7 s.
    events = serve(monkeypatch, [0.7], checks={"recheck": 0.2}, rechecks=rechecks)
    assert [e for e in events if e["event"] == "recheck"] and rechecks
    assert all(e["checks"] == {"ipv4": {"ip": "x"}} for e in events if e["event"] == "recheck")
    # recheck 0: only the check at the start.
    rechecks.clear()
    events = serve(monkeypatch, [0.3], checks={"recheck": 0}, rechecks=rechecks)
    assert not rechecks and not [e for e in events if e["event"] == "recheck"]



def test_tors_first_check_during_the_run_comes_soon(monkeypatch):
    """With Tor (check_settings: first_recheck) the first check during the run comes soon after the start, when the
    agent's firewall is in the namespace too, then at the usual interval."""
    rechecks = []
    serve(monkeypatch, [0.6], checks={"recheck": 60, "first_recheck": 0.2}, rechecks=rechecks)
    assert len(rechecks) == 1
    rechecks.clear()
    serve(monkeypatch, [0.4], checks={"recheck": 60}, rechecks=rechecks)
    assert rechecks == []

def test_resolve_in_the_running_container(tmp_path, monkeypatch):
    state = tmp_path / "serve.json"
    rt.save_serve_state({"dns": ["10.66.0.1"], "ipv4": True, "ipv6": False}, False, state)
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setattr(rt, "dns_lookup", lambda name, server, qtype, timeout=4.0, port=53:
                        ["203.0.113.9"] if server == "10.66.0.1" else [])
    out = []
    stream = types.SimpleNamespace(write=out.append)
    import io
    assert rt.resolve_command(io.StringIO('["api.example.org"]'), stream, state) == 0
    assert json.loads(out[-1]) == {"hosts": {"api.example.org": ["203.0.113.9"]}}
    assert rt.resolve_command(io.StringIO('"api.example.org"'), stream, state) == 1
    assert "JSON list" in json.loads(out[-1])["error"]
    assert rt.resolve_command(io.StringIO("[]"), stream, tmp_path / "missing.json") == 1  # no tunnel served here


# --- the launcher's side ---------------------------------------------------------------------------------------------

ANSWERING = r"""
import json, sys
sys.stdin.readline()
print(json.dumps({"event": "ready", "tunnel": {}, "checks": {}, "hosts": {}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"event": "recheck", "checks": {}}), flush=True)
    if request["op"] == "fail":
        print(json.dumps({"event": "answer", "id": request["id"], "error": "no route"}), flush=True)
    elif request["op"] == "quiet":
        pass
    else:
        print(json.dumps({"event": "answer", "id": request["id"], "echo": request}), flush=True)
"""


def test_a_tunnel_answers_one_request_at_a_time():
    tunnel = v.Tunnel([sys.executable, "-c", ANSWERING], {"profile": {"name": "x"}}, lambda text: None)
    try:
        tunnel.wait_ready(timeout=20)
        seen = []
        answer = tunnel.request("resolve", on_event=seen.append, names=["a.example"])
        assert answer == {"echo": {"op": "resolve", "id": 1, "names": ["a.example"]}}
        assert seen == [{"event": "recheck", "checks": {}}], "events on the way go elsewhere"
        with pytest.raises(ValueError, match="no route"):
            tunnel.request("fail")
        with pytest.raises(ValueError, match="no answer from the VPN container within 0.5 s"):
            tunnel.request("quiet", wait=0.5)
    finally:
        tunnel.close()


HOST = {"ipv4": "198.18.0.5", "ipv6": None}


def test_a_leak_found_during_the_run_stops_it(home, tmp_path):
    PROFILE = wireguard_profile(home)
    said, stopped = [], []
    a = types.SimpleNamespace(log_dir=str(tmp_path), run_id="r1", vpn_check=None)
    ready = {"tunnel": {"interface": "wg0", "ipv4": True, "full4": True}}
    leak = {"checks": {"ipv4": {"ip": "198.18.0.5", "vpn": None, "url": "https://x.example/"}, "dns": {}}}
    verdict = v.rechecked(a, PROFILE, ready, leak, said.append, baseline=lambda settings: HOST,
                          stop=lambda: stopped.append(1))
    assert verdict["leaks"] and stopped == [1] and any("the run is stopped" in line for line in said)
    record = json.loads((tmp_path / "vpn.jsonl").read_text().splitlines()[-1])
    assert record["recheck"] is True and record["leaks"] and record["run"] == "r1"
    # --vpn-check warn: said and logged, the run goes on.
    stopped.clear()
    v.rechecked(types.SimpleNamespace(**{**vars(a), "vpn_check": "warn"}), PROFILE, ready, leak, said.append,
                baseline=lambda settings: HOST, stop=lambda: stopped.append(1))
    assert stopped == []
    # A tunnel that stalls (no answer through it) is no leak: the kill switch keeps it in; only said.
    stall = {"checks": {"ipv4": {"error": "timed out", "kind": "blocked"}, "dns": {}}}
    verdict = v.rechecked(a, PROFILE, ready, stall, said.append, baseline=lambda settings: HOST,
                          stop=lambda: stopped.append(1))
    assert verdict["failures"] and not verdict["leaks"] and stopped == []
    # The container could not check: said, nothing judged.
    assert v.rechecked(a, PROFILE, ready, {"error": "boom"}, said.append) is None and "did not run: boom" in said[-1]


def test_the_recheck_interval_is_a_setting(home):
    profile = wireguard_profile(home)
    assert v.check_settings(profile)["recheck"] == rt.RECHECK
    checks = v.profiles_dir() / "checks.json"
    for value, ok in ((0, True), (120, True), (30, False), (True, False), ("600", False)):
        checks.write_text(json.dumps({"recheck": value}))
        checks.chmod(0o600)
        if ok:
            assert v.check_settings(profile)["recheck"] == value
        else:
            with pytest.raises(ValueError, match="recheck"):
                v.check_settings(profile)


def test_names_allowed_during_a_run_are_looked_up_through_its_vpn():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return types.SimpleNamespace(stdout='{"hosts": {"pypi.org": ["151.101.0.223", "151.101.0.223"]}}\n', stderr="")
    resolve = v.resolver_for_run({"vpn_container": "agent-claude-x-vpn", "podman_url": None}, run)
    assert resolve("pypi.org") == ["151.101.0.223"]
    command, given = calls[0]
    assert command == ["podman", "exec", "-i", "agent-claude-x-vpn", "/usr/bin/python3", "-I", "/vpn/vpn_runtime.py",
                       "resolve"] and json.loads(given) == ["pypi.org"]
    remote = v.resolver_for_run({"vpn_container": "c-vpn", "podman_url": "ssh://h/run/podman.sock"}, run)
    remote("pypi.org")
    assert calls[-1][0][:4] == ["podman", "--remote", "--url", "ssh://h/run/podman.sock"]
    failing = v.resolver_for_run({"vpn_container": "c-vpn"},
                                 lambda command, **kw: types.SimpleNamespace(stdout='{"error": "NXDOMAIN"}', stderr=""))
    with pytest.raises(OSError, match="NXDOMAIN"):
        failing("gone.example")
    # Not a VPN run, or a name a hostile run.json could make into an option: no VPN resolver.
    assert v.resolver_for_run({}) is None and v.resolver_for_run({"vpn_container": "--privileged"}) is None
    assert v.container_name("agent-claude-x") == "agent-claude-x-vpn"


def test_net_allow_and_answers_use_the_runs_vpn_resolver(monkeypatch):
    net = s.load_vpn_module  # noqa: F841 - (the launcher loads vpn.py itself)
    import importlib.util
    spec = importlib.util.spec_from_file_location("net_vpn_test", ROOT / "sandbox" / "net.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.vpn_resolver({"run": "x"}) is None
    through = module.resolve_with(lambda name: {"a.example": ["203.0.113.1"]}.get(name, []))
    assert through(["a.example"]) == {"a.example": ["203.0.113.1"]}
    with pytest.raises(ValueError, match="b.example has no address"):
        through(["b.example"])

    def broken(name):
        raise OSError("the VPN's resolver: SERVFAIL")
    with pytest.raises(ValueError, match="cannot resolve c.example: the VPN's resolver: SERVFAIL"):
        module.resolve_with(broken)(["c.example"])
    assert module.vpn_resolver({"vpn_container": "agent-x-vpn"}) is not None


def test_the_run_record_names_the_vpn_container():
    text = (ROOT / "sandbox" / "agent_sandbox.py").read_text()
    assert '"vpn_container": load_vpn_module().container_name(a.vpn_pod)' in text
    approvals = (ROOT / "sandbox" / "approvals.py").read_text()
    assert "resolved = (resolver or net().vpn_resolver(record) or resolve)(host)" in approvals


# --- sandbox-ai llm through the endpoint's VPN ------------------------------------------------------------------------

class FakeTunnel:
    """The launcher's Tunnel as llm uses it: request(op, wait, **fields) answered by ANSWER(op, fields)."""

    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def request(self, op, wait=60, on_event=None, **fields):
        self.requests.append((op, fields))
        return self.answer(op, fields)


@pytest.fixture
def llm(home):
    spec = __import__("importlib").util.spec_from_file_location("llm_vpn_test", ROOT / "sandbox" / "llm.py")
    module = __import__("importlib").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def endpoint(llm, url="http://10.8.0.5:8000", **extra):
    record = {"kind": "vllm", "url": url, "api_key_secret": None, "headers": {}, "ca": None, "vpn": "corp",
              "model": None, "anthropic": False, "insecure_http": False, **extra}
    return llm.check_record("lab", record)


def test_llm_requests_go_through_the_vpn(llm, monkeypatch):
    found = endpoint(llm)
    monkeypatch.setattr(llm, "secret_values", lambda endpoint, get=None: ("sk-lab", {}))

    def answer(op, fields):
        body = {"/v1/models": {"data": [{"id": "qwen", "max_model_len": 32768}]}}.get(
            fields["url"].split("8000", 1)[1], {})
        return {"status": 200, "body": base64.b64encode(json.dumps(body).encode()).decode(), "address": "10.8.0.5"}
    tunnel = FakeTunnel(answer)
    client = llm.VPNClient(found, tunnel)
    assert client.json("/v1/models")["data"][0]["id"] == "qwen"
    op, fields = tunnel.requests[0]
    assert op == "http" and fields["url"] == "http://10.8.0.5:8000/v1/models" and fields["secret"] is True
    assert fields["headers"]["Authorization"] == "Bearer sk-lab" and fields["method"] == "GET"
    assert "10.0.0.0/8" in fields["plain_networks"], "the key goes over plain http only to local networks"
    insecure = llm.VPNClient(endpoint(llm, insecure_http=True), tunnel)
    insecure.call("/v1/models")
    assert tunnel.requests[-1][1]["plain_networks"] is None

    def refused(op, fields):
        raise ValueError("plain http to 203.0.113.5 would cross networks you do not control unencrypted")
    with pytest.raises(ValueError, match="would cross networks"):
        llm.VPNClient(found, FakeTunnel(refused)).json("/v1/models")


def test_llm_check_through_the_vpn(llm, monkeypatch):
    found = endpoint(llm, url="https://llm.corp.example")

    def answer(op, fields):
        if op == "resolve":
            return {"hosts": {"llm.corp.example": ["10.8.0.5"]}}
        if op == "connect":
            return {"address": "10.8.0.5", "tls": "TLSv1.3"}
        path = fields["url"].split("example", 1)[1]
        body = {"/v1/models": {"data": [{"id": "qwen"}]}}.get(path)
        return {"status": 200 if body else 400, "body": base64.b64encode(json.dumps(body or {}).encode()).decode()}
    monkeypatch.setattr(llm, "remember_models", lambda name, names, contexts=None: None)
    monkeypatch.setattr(llm.socket, "create_connection", lambda *a, **k: pytest.fail("connected from the host"))
    lines = []
    tunnel = FakeTunnel(answer)
    assert llm.check(found, out=lines.append, tunnel=tunnel)
    text = "\n".join(lines)
    assert "through the VPN profile corp: its tunnel is up and checked" in text
    assert "address 10.8.0.5 (llm.corp.example)" in text and "TLS TLSv1.3" in text and "through the VPN" in text
    assert [op for op, _ in tunnel.requests][:2] == ["resolve", "connect"]
    assert {op for op, _ in tunnel.requests[2:]} == {"http"}
    # A name the VPN's resolver does not know: said, nothing else tried.
    lines.clear()

    def unknown(op, fields):
        raise ValueError("Cannot resolve allowed host llm.corp.example through the VPN")
    assert not llm.check(found, out=lines.append, tunnel=FakeTunnel(unknown))
    assert "does not resolve: through the VPN" in "\n".join(lines)


def test_llm_status_of_a_vpn_endpoint_that_does_not_come_up(llm, monkeypatch):
    found = endpoint(llm)

    class Broken:
        def channel(self, name, podman_url=None, say=None):
            raise ValueError("the image localhost/sandbox-ai-vpn is missing")
    monkeypatch.setattr(llm, "load_vpn_module", lambda: Broken())
    entry = llm.vpn_status(found)
    assert entry["reachable"] is False and entry["error"].startswith("VPN corp: the image")


def test_a_remote_engines_own_address_is_the_baseline():
    calls = []

    def run(command, **kwargs):
        calls.append((command, json.loads(kwargs["input"])))
        return types.SimpleNamespace(stdout='{"ipv4": "203.0.113.20", "ipv6": null}\n', stderr="")
    settings = {"urls": ["https://api64.ipify.org/?format=json"], "timeout": 8, "dns_probe": None}
    podman = ["podman", "--remote", "--url", "ssh://box/run/podman.sock"]
    assert v.host_baseline(settings, podman=podman, run=run) == {"ipv4": "203.0.113.20", "ipv6": None}
    command, given = calls[0]
    assert command[:5] == [*podman, "run"] and command[-4:] == [v.VPN_IMAGE, "-I", "/vpn/vpn_runtime.py", "baseline"]
    assert "--cap-drop=ALL" in command and given == settings
    # No answer from there: no baseline (the checks then warn that they could not compare), never the launcher's.
    assert v.host_baseline(settings, podman=podman,
                           run=lambda command, **kw: types.SimpleNamespace(stdout="", stderr="no engine")) == {}
    # A local engine: asked here, as before.
    local = v.host_baseline(settings, resolve=lambda name, family: ["198.51.100.1"] if family == 4 else [],
                            get=lambda url, address, timeout: (200, b'{"ip": "198.18.0.5"}'), podman=["podman"])
    assert local["ipv4"] == "198.18.0.5" and local["ipv6"] is None


def test_the_baseline_entry_point(monkeypatch):
    import io
    monkeypatch.setattr(rt, "baseline", lambda settings, *a: {"ipv4": "198.18.0.9", "seen": settings["urls"]})
    out = []
    assert rt.main(["baseline"], io.StringIO('{"urls": ["https://x.example/"], "timeout": 3}'),
                   types.SimpleNamespace(write=out.append)) == 0
    assert json.loads(out[-1]) == {"ipv4": "198.18.0.9", "seen": ["https://x.example/"]}


def test_whatever_uses_a_profile_is_found_by_its_label(home):
    profile = wireguard_profile(home)
    command = v.container_command(["podman"], "c", profile, pod="p")
    assert command[command.index("--label") + 1] == f"sandbox-ai.vpn-profile={profile['name']}"
    asked = []

    def run(command, **kwargs):
        asked.append(command)
        return types.SimpleNamespace(returncode=0, stdout="sandbox-ai-vpn-mcp-files\nagent-x-vpn\n", stderr="")
    assert v.profile_users(profile["name"], run) == ["agent-x-vpn", "sandbox-ai-vpn-mcp-files"]
    assert asked[0][:4] == ["podman", "ps", "--filter", f"label=sandbox-ai.vpn-profile={profile['name']}"]
    # No Podman to ask: nothing known, nothing refused.
    def gone(command, **kwargs):
        raise FileNotFoundError("podman")
    assert v.profile_users(profile["name"], gone) == []
