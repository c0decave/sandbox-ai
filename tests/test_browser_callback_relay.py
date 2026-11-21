"""A login's callback to the sandbox's loopback, relayed once (sandbox/oauth_relay.py; runtime.oauth_callback, the
control op "oauth-callback"; approvals: a browser question allowed).

Regression (2026-11-17, live): `codex login` in a sandbox printed
https://auth.openai.com/oauth/authorize?...&redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback; the human
opened it, signed in, and the browser was sent to its own 127.0.0.1:1455, which is not the pod's: the login never
ended. Now the human's "Open" starts a one-shot catcher on 127.0.0.1:1455 where the browser is, which hands exactly
that one request to the sandbox's 127.0.0.1:1455 through the run's control socket and shows the agent's page.

Security: only loopback, only the callback's port and path, one request, a top-level navigation with the right Host,
never the browser's cookies, only the run whose question it is, closed after the request or WAIT."""
import base64
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


net = load("sandbox_net_for_callback_relay", "sandbox/net.py")
rt = net.launcher().RUNTIME
ap = load("sandbox_approvals_for_callback_relay", "sandbox/approvals.py")
relay = ap.oauth_relay()

CODEX = ("https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_X&redirect_uri=http%3A%2F%2F"
         "127.0.0.1%3A{port}%2Fauth%2Fcallback&code_challenge=c&code_challenge_method=S256&state=S1&scope=openid")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# --- which URLs carry a callback ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    (CODEX.format(port=1455), (1455, "/auth/callback", "127.0.0.1")),
    ("https://claude.ai/oauth/authorize?code=true&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&state=x",
     (54545, "/callback", "127.0.0.1")),
    ("https://a.example/authorize?redirect_url=http://[::1]:8080/cb", (8080, "/cb", "::1")),
    ("https://a.example/authorize?callback=http%3A%2F%2F127.0.0.1%3A9000", (9000, "/", "127.0.0.1")),
    ("https://a.example/authorize?REDIRECT_URI=http%3A%2F%2F127.0.0.1%3A9000%2Fx", (9000, "/x", "127.0.0.1")),
    # The same callback twice is one.
    ("https://a.example/a?redirect_uri=http://127.0.0.1:9000/x&callback=http://127.0.0.1:9000/x",
     (9000, "/x", "127.0.0.1")),
])
def test_a_callback_to_the_loopback_is_found(url, expected):
    target = relay.callback_target(url)
    assert (target["port"], target["path"], target["bind"]) == expected


@pytest.mark.parametrize("url", [
    "https://auth.openai.com/codex/device",                                         # none
    "https://a.example/a?redirect_uri=https%3A%2F%2F127.0.0.1%3A1455%2Fcb",          # https: no server of the agent's
    "https://a.example/a?redirect_uri=http%3A%2F%2Fexample.com%3A1455%2Fcb",         # another host
    "https://a.example/a?redirect_uri=http%3A%2F%2F127.0.0.1.evil.example%3A1455%2F",
    "https://a.example/a?redirect_uri=http%3A%2F%2F127.0.0.2%3A1455%2Fcb",           # only the three names
    "https://a.example/a?redirect_uri=http%3A%2F%2F127.0.0.1%2Fcb",                  # no port: port 80, never taken
    "https://a.example/a?redirect_uri=http%3A%2F%2F127.0.0.1%3A0%2Fcb",
    "https://a.example/a?redirect_uri=http%3A%2F%2F127.0.0.1%3A70000%2Fcb",
    "https://a.example/a?redirect_uri=http%3A%2F%2Fuser%40127.0.0.1%3A1455%2Fcb",    # a user part
    "https://a.example/a?redirect_uri=http://127.0.0.1:1455/a&callback=http://127.0.0.1:1456/a",  # which one?
    "https://a.example/a?next=http%3A%2F%2F127.0.0.1%3A1455%2Fcb",                   # not a callback parameter
    None, 42,
])
def test_anything_else_has_none(url):
    assert relay.callback_target(url) is None


def test_the_question_says_what_happens_and_the_way_out():
    text = ap.describe({"kind": "browser", "url": CODEX.format(port=1455)})
    assert "its login callback goes to http://127.0.0.1:1455/auth/callback in the sandbox" in text
    assert "relays that one request into this sandbox" in text and "codex login --device-auth" in text
    assert "callback" not in ap.describe({"kind": "browser", "url": "https://example.com/"})
    assert ap.browser_fields(CODEX.format(port=1455))["callback"] == {
        "url": "http://127.0.0.1:1455/auth/callback", "port": 1455, "path": "/auth/callback"}
    assert "callback" not in ap.browser_fields("https://example.com/")


# --- the catcher ------------------------------------------------------------------------------------------------------------

class Caught:
    """A catcher on a free port (in a thread) whose FORWARD answers ANSWER and records each request."""

    def __init__(self, path="/auth/callback", answer=None, wait=5.0, fail=None):
        self.port = free_port()
        self.target = relay.callback_target(
            f"https://a.example/a?redirect_uri=http%3A%2F%2F127.0.0.1%3A{self.port}{path.replace('/', '%2F')}")
        self.forwarded = []
        self.answer = answer or {"status": 200, "content_type": "text/html", "body": b"<p>Signed in</p>"}
        self.fail = fail
        listener = relay.bind(self.target)
        self.catcher = relay.Catcher(listener, self.target, self.forward, wait=wait)
        self.outcome = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def forward(self, request):
        self.forwarded.append(request)
        if self.fail:
            raise ValueError(self.fail)
        return self.answer

    def run(self):
        self.outcome = self.catcher.serve()

    def get(self, target, headers=None, data=None, host=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{target}", data=data, headers=headers or {})
        if host:
            request.add_unredirected_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()


def test_the_one_callback_reaches_the_sandbox_and_its_page_comes_back_then_the_port_closes():
    caught = Caught()
    status, headers, body = caught.get("/auth/callback?code=C1&state=S1",
                                       headers={"Cookie": "session=host-secret", "Authorization": "Basic eA==",
                                                "Accept": "text/html", "Sec-Fetch-Mode": "navigate",
                                                "Sec-Fetch-Dest": "document", "Referer": "https://auth.example/"})
    assert status == 200 and body == b"<p>Signed in</p>"
    # Inert in the human's browser: no script of the sandbox's runs at 127.0.0.1's origin.
    assert "script-src" not in headers["Content-Security-Policy"] and "sandbox" in headers["Content-Security-Policy"]
    assert "default-src 'none'" in headers["Content-Security-Policy"] and "Set-Cookie" not in headers
    [request] = caught.forwarded
    assert request["method"] == "GET" and request["target"] == "/auth/callback?code=C1&state=S1"
    assert request["host"] == f"127.0.0.1:{caught.port}" and request["body"] == b""
    # Never the browser's cookies (127.0.0.1's go to every port), authorization or referrer.
    assert set(request["headers"]) <= {"accept", "accept-language", "content-type", "user-agent"}
    caught.thread.join(5)
    assert caught.outcome == {"outcome": "relayed", "status": 200, "refused": 0}
    # A second request: nothing listens any more.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", caught.port), timeout=2).close()


def test_another_path_host_or_a_scripts_request_is_refused_and_the_one_still_comes():
    caught = Caught()
    assert caught.get("/favicon.ico")[0] == 404
    assert caught.get("/auth/callbackX?code=1")[0] == 404
    assert caught.get("/auth/callback?code=1", host="evil.example")[0] == 421   # a rebound name
    assert caught.get("/auth/callback?code=1", host=f"127.0.0.1:{caught.port + 1}")[0] == 421
    assert caught.get("/auth/callback?code=1", headers={"Sec-Fetch-Mode": "no-cors"})[0] == 403  # an <img>, a fetch
    assert caught.get("/auth/callback?code=1", headers={"Sec-Fetch-Dest": "iframe"})[0] == 403
    assert caught.forwarded == []
    status, _, body = caught.get("/auth/callback?code=C&state=S", host=f"localhost:{caught.port}")
    assert status == 200 and len(caught.forwarded) == 1
    caught.thread.join(5)
    assert caught.outcome["outcome"] == "relayed" and caught.outcome["refused"] == 6


def test_a_post_callback_goes_with_its_body():
    caught = Caught(path="/cb")
    status, _, _ = caught.get("/cb", data=b"code=C&state=S",
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 200
    assert caught.forwarded[0]["method"] == "POST" and caught.forwarded[0]["body"] == b"code=C&state=S"
    assert caught.forwarded[0]["headers"]["content-type"] == "application/x-www-form-urlencoded"


def test_it_listens_on_the_loopback_port_only():
    caught = Caught()
    assert caught.catcher.listener.getsockname() == ("127.0.0.1", caught.port)
    other = socket.socket()
    try:  # nothing else of this machine reaches it: it is not bound to every address
        assert caught.catcher.listener.getsockname()[0] != "0.0.0.0"
    finally:
        other.close()
    caught.get("/auth/callback?code=1")
    caught.thread.join(5)


def test_it_closes_after_its_time_without_a_callback():
    caught = Caught(wait=0.5)
    caught.thread.join(5)
    assert caught.outcome["outcome"] == "timed out" and caught.forwarded == []
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", caught.port), timeout=2).close()


def test_a_callback_that_cannot_be_relayed_says_to_use_the_device_login():
    caught = Caught(fail="the sandbox did not answer")
    status, _, body = caught.get("/auth/callback?code=1")
    assert status == 502 and b"device-code login" in body and b"the sandbox did not answer" in body
    caught.thread.join(5)
    assert caught.outcome["outcome"] == "failed"


def test_the_agents_redirect_elsewhere_is_shown_as_a_link_not_followed():
    caught = Caught(answer={"status": 302, "content_type": "", "body": b"",
                            "location": "https://console.example/oauth/code/success"})
    status, headers, body = caught.get("/auth/callback?code=1")
    assert status == 200 and "Location" not in headers
    assert b"https://console.example/oauth/code/success" in body


def test_a_busy_port_says_to_use_the_device_login(tmp_path):
    port = free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        url = CODEX.format(port=port)
        (tmp_path / "approvals").mkdir()
        ok, note = relay.start("1-2-3", url, record_dir=tmp_path, popen=lambda *a, **k: pytest.fail("started"))
        assert ok is False and "is busy" in note and "codex login --device-auth" in note
        record = json.loads((tmp_path / "approvals" / "1-2-3.callback").read_text())
        assert record["outcome"] == "port busy" and record["port"] == port
    finally:
        holder.close()


def test_start_hands_the_bound_socket_to_a_process_of_its_own():
    started = []
    url = CODEX.format(port=free_port())
    ok, note = relay.start("1-2-3", url, popen=lambda command, **kwargs: started.append((command, kwargs)))
    assert ok is True and "relayed into the sandbox once" in note
    [(command, kwargs)] = started
    assert command[2].endswith("oauth_relay.py") and command[3] == "--internal-catch" and command[5] == "1-2-3"
    assert kwargs["pass_fds"] == (int(command[4]),) and kwargs["start_new_session"] is True
    assert relay.start("1-2-3", "https://example.com/") == (None, "")


# --- the sandbox's side: the supervisor's control op ------------------------------------------------------------------------

class Agent(http.server.ThreadingHTTPServer):
    """A fake agent's login server on the sandbox's loopback: /auth/callback answers 302 to /success (as Codex's)."""

    def __init__(self, port=0, redirect=None, host="127.0.0.1"):
        self.requests = []
        self.redirect = redirect
        super().__init__((host, port), AgentHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class AgentHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else b""
        self.server.requests.append({"method": self.command, "path": self.path, "headers": self.headers,
                                     "body": body})
        port = self.server.server_address[1]
        if self.path.startswith("/auth/callback"):
            self.send_response(302)
            self.send_header("Location", self.server.redirect or f"http://localhost:{port}/success?id=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/success"):
            page = b"<html><script>window.close()</script>Signed in to Codex</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", "agent=1")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET  # noqa: N815


def op(port, target="/auth/callback?code=C&state=S", **extra):
    return {"op": "oauth-callback", "port": port, "address": "127.0.0.1", "method": "GET", "target": target,
            "host": f"127.0.0.1:{port}", "headers": {}, "body": "", **extra}


def test_the_supervisor_sends_it_to_the_agent_and_follows_its_redirect_to_its_own_page():
    agent = Agent()
    port = agent.server_address[1]
    try:
        answer = rt.oauth_callback(op(port, headers={"Accept": "text/html", "Cookie": "x=1"}))
        assert answer["status"] == 200 and b"Signed in to Codex" in base64.b64decode(answer["body"])
        assert answer["followed"] == ["/success"] and "location" not in answer
        assert [r["path"] for r in agent.requests] == ["/auth/callback?code=C&state=S", "/success?id=1"]
        assert agent.requests[0]["headers"]["Host"] == f"127.0.0.1:{port}"
        assert "Cookie" not in agent.requests[0]["headers"] and agent.requests[0]["headers"]["Accept"] == "text/html"
    finally:
        agent.shutdown()


def test_a_redirect_to_another_port_or_host_is_not_followed():
    other = free_port()
    agent = Agent(redirect=f"http://127.0.0.1:{other}/steal")
    port = agent.server_address[1]
    try:
        answer = rt.oauth_callback(op(port))
        assert answer["status"] == 302 and answer["location"] == f"http://127.0.0.1:{other}/steal"
        assert answer["followed"] == [] and len(agent.requests) == 1
    finally:
        agent.shutdown()


@pytest.mark.parametrize("change, why", [
    ({"port": 0}, "port"), ({"port": True}, "port"), ({"address": "10.0.0.1"}, "loopback"),
    ({"address": "0.0.0.0"}, "loopback"), ({"method": "DELETE"}, "method"), ({"target": "http://x/"}, "target"),
    ({"target": "/a b"}, "target"), ({"host": "evil.example:1455"}, "host"), ({"host": "127.0.0.1:1"}, "host"),
    ({"body": "@@"}, "body"), ({"body": base64.b64encode(b"x").decode()}, "POST"),
])
def test_the_supervisor_refuses_anything_else(change, why):
    with pytest.raises(ValueError, match=why):
        rt.oauth_request({**op(1455), **change})


def test_an_agent_that_does_not_listen_is_said():
    with pytest.raises(ValueError, match="did not answer"):
        rt.oauth_callback(op(free_port()))


# --- the host's side: one request, into the run that asked --------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(net.liveness(), "inside_sandbox", lambda *a, **k: False)

    def no_podman(*args, **kwargs):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(net.liveness(), "RUNNER", no_podman)
    net.liveness()._cache.clear()
    monkeypatch.setattr(ap, "net", lambda: net)
    monkeypatch.setattr(ap, "POLICY_FILE", tmp_path / "no-policy.json")


def registered(name, tmp_path):
    directory = net.launcher().runs_dir() / name
    directory.mkdir(parents=True)
    record = {"run": name, "agent": "codex", "pid": os.getpid(), "workspace": str(tmp_path / "work"),
              "session_dir": str(tmp_path), "started": "2026-11-17T10:00:00Z", "session": None}
    (directory / "run.json").write_text(json.dumps(record))
    (directory / "control.sock").unlink(missing_ok=True)
    stop = threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(directory / "control.sock",
                                                              rt.RuntimeNetwork({}, None, lambda s: None), stop),
                              daemon=True)
    thread.start()
    assert wait_for(lambda: (directory / "control.sock").exists())
    return directory, stop, thread


def ask_question(directory, ident, url):
    (directory / "approvals").mkdir(exist_ok=True)
    (directory / "approvals" / f"{ident}.json").write_text(json.dumps({"id": ident, "kind": "browser", "url": url,
                                                                       "time": "2026-11-17T10:00:00Z"}))


@pytest.fixture
def runs(tmp_path, monkeypatch):
    short = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    made = {}
    yield lambda name: made.setdefault(name, registered(name, tmp_path))
    for _, stop, thread in made.values():
        stop.set()
        thread.join(5)
    shutil.rmtree(short, ignore_errors=True)


def callback(port, target="/auth/callback?code=C&state=S1"):
    return {"method": "GET", "target": target, "host": f"127.0.0.1:{port}", "headers": {}, "body": b"", "port": port}


def test_relay_once_takes_one_request_into_the_run_that_asked(runs, monkeypatch):
    agent = Agent()
    port = agent.server_address[1]
    directory, _, _ = runs("codex-1")
    ask_question(directory, "1-1-1", CODEX.format(port=port))
    started = []
    monkeypatch.setattr(relay, "start", lambda *a, **k: (started.append(a), (True, "relayed once"))[1])
    ap.answer("1-1-1", "allow", opener=lambda url: None, out=lambda line: None)
    assert started == [("1-1-1", CODEX.format(port=port))]
    try:
        answer = relay.relay_once("1-1-1", callback(port))
        assert answer["status"] == 200 and b"Signed in to Codex" in answer["body"]
        record = json.loads((directory / "approvals" / "1-1-1.callback").read_text())
        assert record["outcome"] == "relayed" and record["status"] == 200 and record["followed"] == ["/success"]
        # Once per question: the next one is refused before it reaches the sandbox.
        with pytest.raises(ValueError, match="relayed already"):
            relay.relay_once("1-1-1", callback(port))
        assert len([r for r in agent.requests if r["path"].startswith("/auth/callback")]) == 1
    finally:
        agent.shutdown()


def test_relay_once_refuses_what_the_question_did_not_allow(runs, monkeypatch):
    directory, _, _ = runs("codex-1")
    port = 1455
    sent = []
    ask = lambda directory, request, timeout: sent.append(request) or {"ok": True, "status": 200}  # noqa: E731
    ask_question(directory, "1-1-1", CODEX.format(port=port))
    with pytest.raises(ValueError, match="no answered question"):  # not answered yet
        relay.relay_once("1-1-1", callback(port), ask=ask)
    ap.answer("1-1-1", "deny", "once", out=lambda line: None)
    with pytest.raises(ValueError, match="was not allowed"):
        relay.relay_once("1-1-1", callback(port), ask=ask)
    ask_question(directory, "1-1-2", CODEX.format(port=port))
    monkeypatch.setattr(relay, "start", lambda *a, **k: (True, ""))
    ap.answer("1-1-2", "allow", opener=lambda url: None, out=lambda line: None)
    with pytest.raises(ValueError, match="not port 1456"):
        relay.relay_once("1-1-2", {**callback(1456)}, ask=ask)
    with pytest.raises(ValueError, match="path /auth/other"):
        relay.relay_once("1-1-2", callback(port, "/auth/other?code=1"), ask=ask)
    ask_question(directory, "1-1-3", "https://example.com/?redirect_uri=https://example.com/cb")
    ap.answer("1-1-3", "allow", opener=lambda url: None, out=lambda line: None)
    with pytest.raises(ValueError, match="no callback"):
        relay.relay_once("1-1-3", callback(port), ask=ask)
    # Too late: the catcher closed long ago.
    old = time.time() - relay.WAIT_SECONDS - relay.LATE_SECONDS - 5
    os.utime(directory / "approvals" / "1-1-2.answer", (old, old))
    with pytest.raises(ValueError, match="more than 10 minutes ago"):
        relay.relay_once("1-1-2", callback(port), ask=ask)
    assert sent == []


def test_only_the_run_that_asked_receives_it(runs, monkeypatch):
    first, _, _ = runs("codex-1")
    second, _, _ = runs("codex-2")
    ask_question(first, "1-1-1", CODEX.format(port=1455))
    monkeypatch.setattr(relay, "start", lambda *a, **k: (True, ""))
    ap.answer("1-1-1", "allow", opener=lambda url: None, out=lambda line: None)
    reached = []
    relay.relay_once("1-1-1", callback(1455),
                     ask=lambda directory, request, timeout: reached.append(directory) or {"ok": True, "status": 200})
    assert reached == [str(first)] and not (second / "approvals").exists()


def test_the_callback_command_reads_one_request_and_prints_the_answer(runs, monkeypatch):
    agent = Agent()
    port = agent.server_address[1]
    directory, _, _ = runs("codex-1")
    ask_question(directory, "1-1-1", CODEX.format(port=port))
    monkeypatch.setattr(relay, "start", lambda *a, **k: (True, ""))
    ap.answer("1-1-1", "allow", opener=lambda url: None, out=lambda line: None)
    import io
    lines = []
    try:
        source = io.BytesIO(json.dumps(relay.wire_request(callback(port))).encode())
        assert relay.callback_command("1-1-1", source=source, out=lines.append) == 0
        answer = relay.unwire(lines[0], "answer")
        assert answer["status"] == 200 and b"Signed in" in answer["body"]
    finally:
        agent.shutdown()


def test_a_remote_hosts_catcher_forwards_over_its_ssh_connection():
    ran = []

    def runner(host, args, input=None, timeout=None):
        ran.append((host, args, json.loads(input)))
        return 0, json.dumps(relay.wire_answer({"status": 200, "content_type": "text/html", "body": b"ok"})) + "\n", ""
    answer = relay.remote_forward("lab", "1-1-1", runner=runner)(callback(1455))
    assert answer["body"] == b"ok" and ran[0][:2] == ("lab", ["approvals", "callback", "1-1-1"])
    assert base64.b64decode(ran[0][2]["body"]) == b"" and ran[0][2]["port"] == 1455
    with pytest.raises(ValueError, match="relayed already"):
        relay.remote_forward("lab", "1-1-1", runner=lambda *a, **k: (1, "", "question 1-1-1's callback was relayed "
                                                                             "already"))(callback(1455))


def test_the_activity_shows_what_became_of_the_callback(runs, tmp_path, monkeypatch):
    activity = load("sandbox_activity_for_callback_relay", "sandbox/activity.py")
    directory, _, _ = runs("codex-1")
    ask_question(directory, "1-1-1", CODEX.format(port=1455))
    monkeypatch.setattr(relay, "start", lambda *a, **k: (True, ""))
    ap.answer("1-1-1", "allow", opener=lambda url: None, out=lambda line: None)
    relay.relay_once("1-1-1", callback(1455), ask=lambda d, r, timeout: {"ok": True, "status": 200, "body": ""})
    source = activity.Approvals(directory)
    events = source.poll(1 << 20, time.time(), final=True)
    [relayed] = [e for e in events if e["kind"] == "network"]
    assert "login callback relayed into the sandbox" in relayed["summary"]
    assert "http://127.0.0.1:1455/auth/callback" in relayed["summary"]
    assert relayed["details"]["event"] == "oauth-callback" and relayed["details"]["status"] == 200
    assert not [e for e in source.poll(1 << 20, time.time(), final=True) if e["kind"] == "network"]  # once


def test_the_report_lists_the_callback_with_the_questions_without_counting_it():
    report = load("sandbox_report_for_callback_relay", "sandbox/report.py")
    gathered = report.Gatherer({"runs": {}, "templates": {}}, live=False)
    record = {"run": "codex-1", "kind": "network", "time": "2026-11-17T10:00:00Z",
              "summary": "login callback relayed into the sandbox: GET http://127.0.0.1:1455/auth/callback -> 200",
              "details": {"event": "oauth-callback", "outcome": "relayed", "status": 200}}
    gathered.activity(record, {"runs": None, "sessions": set()})
    assert gathered.approvals["asked"] == 0
    [item] = gathered.approvals["items"]
    assert item["answer"] == "relayed" and "http://127.0.0.1:1455/auth/callback" in item["question"]


def test_a_localhost_callback_reaches_the_agent_on_either_loopback():
    """A Node server listening on "localhost" may have ::1 only: tried first, then 127.0.0.1 (as a browser does)."""
    class Six(Agent):
        address_family = socket.AF_INET6
    six = Six(host="::1")
    try:
        port = six.server_address[1]
        answer = rt.oauth_callback(op(port, address="localhost", host=f"localhost:{port}"))
        assert answer["status"] == 200 and six.requests[0]["path"].startswith("/auth/callback")
    finally:
        six.shutdown()
    four = Agent()
    try:
        port = four.server_address[1]
        assert rt.oauth_callback(op(port, address="localhost", host=f"localhost:{port}"))["status"] == 200
    finally:
        four.shutdown()


# --- 24h review A (2026-11-17) --------------------------------------------------------------------------------------------

def test_a_second_start_that_finds_the_port_busy_does_not_use_up_the_questions_one_relay(runs, monkeypatch):
    """`approvals relay ID` (the app's and the web's "Open here", or by hand) a second time while the question's
    catcher listens: its port is busy - by that catcher. The note "port busy" was written as the question's record
    (ID.callback), which is also what "relayed once" is told by: the catcher's one request was then refused as
    "relayed already", and the login never ended."""
    agent = Agent()
    port = agent.server_address[1]
    directory, _, _ = runs("codex-1")
    ask_question(directory, "1-1-1", CODEX.format(port=port))
    start = relay.start
    monkeypatch.setattr(relay, "start", lambda *a, **k: (True, "relayed once"))
    ap.answer("1-1-1", "allow", opener=lambda url: None, out=lambda line: None)
    try:
        # The catcher of the question holds the port (here: the fake agent's server does); a second start finds it busy.
        ok, note = start("1-1-1", CODEX.format(port=port), record_dir=directory,
                         popen=lambda *a, **k: pytest.fail("started"))
        assert ok is False and "is busy" in note
        answer = relay.relay_once("1-1-1", callback(port))
        assert answer["status"] == 200 and b"Signed in to Codex" in answer["body"]
        record = json.loads((directory / "approvals" / "1-1-1.callback").read_text())
        assert record["outcome"] == "relayed"
        with pytest.raises(ValueError, match="relayed already"):  # and then once is once
            relay.relay_once("1-1-1", callback(port))
        assert len([r for r in agent.requests if r["path"].startswith("/auth/callback")]) == 1
    finally:
        agent.shutdown()


@pytest.mark.parametrize("change", [{"port": [1455]}, {"port": {"a": 1}}, {"headers": ["a"]}, {"headers": "x"},
                                    {"target": 7}, {"body": 7}, {"method": ["GET"]}])
def test_a_request_of_another_shape_is_refused_not_a_crash(runs, monkeypatch, change):
    """`approvals callback ID` reads its request from another machine's catcher (over SSH): JSON of any shape. A
    port, headers, a target or a body of another type ended it with a traceback (TypeError, AttributeError), and
    after the question's one relay had been taken."""
    directory, _, _ = runs("codex-1")
    ask_question(directory, "1-1-1", CODEX.format(port=1455))
    monkeypatch.setattr(relay, "start", lambda *a, **k: (True, ""))
    ap.answer("1-1-1", "allow", opener=lambda url: None, out=lambda line: None)
    sent = []
    with pytest.raises(ValueError):
        relay.relay_once("1-1-1", {**callback(1455), **change},
                         ask=lambda directory, request, timeout: sent.append(request) or {"ok": True, "status": 200})
    assert sent == [] and not (directory / "approvals" / "1-1-1.callback").exists()  # (not used up by it either)


def test_a_new_login_on_the_same_port_takes_it_from_the_older_catcher(tmp_path, monkeypatch):
    """`codex login` again within ten minutes (the first try's tab closed, the wrong account): the new question's
    callback has the same port, which the first question's catcher still held - "port busy, use the device-code
    login", while the callback then went to the old question (or nowhere, once its ten minutes were over). The
    human's newest "Open" is the one that counts: the older catcher of this user closes, the new one listens."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    port = free_port()
    url = CODEX.format(port=port)
    started = []

    def popen(command, **kwargs):
        import subprocess
        process = subprocess.Popen(command, **{**kwargs, "env": {**os.environ}})
        started.append(process)
        return process
    try:
        ok, _ = relay.start("1-1-1", url, popen=popen)
        assert ok is True
        assert wait_for(lambda: relay.own_catcher(port) is not None), "the catcher did not say that it listens"
        ok, note = relay.start("1-1-2", url, popen=popen)
        assert ok is True and "earlier" in note, note
        assert wait_for(lambda: started[0].poll() is not None), "the older catcher still runs"
        assert wait_for(lambda: (relay.own_catcher(port) or {}).get("ident") == "1-1-2")
        with socket.create_connection(("127.0.0.1", port), timeout=5):  # the new one listens
            pass
        # A port something else holds is busy as before: nothing of another program's is ended.
        other = free_port()
        holder = socket.socket()
        holder.bind(("127.0.0.1", other))
        holder.listen(1)
        try:
            ok, note = relay.start("1-1-3", CODEX.format(port=other), popen=lambda *a, **k: pytest.fail("started"))
            assert ok is False and "is busy" in note
        finally:
            holder.close()
    finally:
        for process in started:
            if process.poll() is None:
                process.terminate()
            process.wait(5)


def test_only_a_catcher_of_sandbox_ai_is_taken_for_one(tmp_path, monkeypatch):
    """The record of a catcher names a process: one that is no catcher (the number given to another program since, a
    record written by hand) is never signalled."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    folder = relay.catchers_dir()
    folder.mkdir(parents=True)
    (folder / "1455.json").write_text(json.dumps({"pid": os.getpid(), "ident": "1-1-1", "port": 1455}))
    assert relay.own_catcher(1455) is None
    (folder / "1456.json").write_text("not json")
    assert relay.own_catcher(1456) is None
    assert relay.own_catcher(1457) is None


def test_only_the_logins_own_callback_with_its_state_is_taken(monkeypatch):
    """Backlog A (review A, 9): the one request could be used up by another local process or a page (a navigation
    to the callback with a code of its own). With the login URL's `state`, the catcher takes only the callback that
    carries that state (in the query, or a form POST's body): any other request gets 404 and it keeps waiting; the
    state never goes on the catcher's command line (the process list is everyone's), only into its environment."""
    port = free_port()
    target = relay.callback_target(CODEX.format(port=port))
    assert target["state"] == "S1"
    caught_requests, outcomes = [], []
    listener = relay.bind(target)
    catcher = relay.Catcher(listener, target, lambda request: caught_requests.append(request) or
                            {"status": 200, "content_type": "text/html", "body": b"ok"}, wait=5)
    thread = threading.Thread(target=lambda: outcomes.append(catcher.serve()), daemon=True)
    thread.start()

    def get(path, data=None, headers=None):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
    assert get("/auth/callback?code=EVIL") == 404, "no state"
    assert get("/auth/callback?code=EVIL&state=S2") == 404, "another login's state"
    assert get("/auth/callback?code=EVIL&state=S1&state=S2") == 404, "two states"
    assert get("/auth/callback", data=b"code=EVIL&state=S9",
               headers={"Content-Type": "application/x-www-form-urlencoded"}) == 404
    assert caught_requests == []
    assert get("/auth/callback?code=MINE&state=S1") == 200
    thread.join(5)
    assert outcomes[0]["outcome"] == "relayed" and outcomes[0]["refused"] == 4
    assert caught_requests[0]["target"] == "/auth/callback?code=MINE&state=S1"
    # A form POST with the state in its body is the login's too.
    port2 = free_port()
    target2 = relay.callback_target(CODEX.format(port=port2).replace("/auth/callback", "/cb").replace(
        "%2Fauth%2Fcallback", "%2Fcb"))
    catcher2 = relay.Catcher(relay.bind(target2), target2, lambda request: {"status": 200, "content_type": "text/html",
                                                                            "body": b"ok"}, wait=5)
    done = []
    thread2 = threading.Thread(target=lambda: done.append(catcher2.serve()), daemon=True)
    thread2.start()
    request = urllib.request.Request(f"http://127.0.0.1:{port2}/cb", data=b"code=MINE&state=S1",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
    thread2.join(5)
    assert done[0]["outcome"] == "relayed"
    # Without a state in the login URL: as before (any one callback request).
    assert relay.callback_target("https://a.example/a?redirect_uri=http://127.0.0.1:9000/x")["state"] is None
    # The state goes to the catcher's process in its environment, never on its command line.
    started = []
    ok, _ = relay.start("1-2-3", CODEX.format(port=free_port()),
                        popen=lambda command, **kwargs: started.append((command, kwargs)))
    [(command, kwargs)] = started
    assert ok and "S1" not in " ".join(command) and kwargs["env"][relay.STATE_VARIABLE] == "S1"
    monkeypatch.setenv(relay.STATE_VARIABLE, "S1")
    assert relay.target_state({"port": 1, "path": "/x"}) == "S1"
