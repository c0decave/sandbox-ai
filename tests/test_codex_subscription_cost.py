"""Codex on a ChatGPT plan is a subscription in every cost place (user 2026-11-20: "systemmonitor erkennt nicht codex
subscription bei kosten").

Codex (rust-v0.166.0, the image's) sends its model requests as the Responses API over a WebSocket: GET
wss://chatgpt.com/backend-api/codex/responses with a ChatGPT login (its JWT bearer, chatgpt-account-id, OpenAI-Beta:
responses_websockets=2026-02-06), wss://api.openai.com/v1/responses with an API key - one connection for many turns,
each a response.create the client sends and the events of the answer (response.completed with its usage) the server
sends back as text frames. The connection proxy relayed those frames unread: such a run had no request in its spend
log, so the monitor said "-" or "USD 0.00", the meter never counted an API key's WebSocket turns, and a budget never
held one back. These tests send Codex's real request shapes (WebSocket and the HTTP+SSE fallback) through the real
proxy with a fake backend, and check what each place that shows cost says: `sandbox-ai spend` (list, run, --json),
`sandbox-ai stats usage` (the System monitor's data and text), the report, the web page's cost cells and box (under
node); the app's monitor page has its own end-to-end test (projects/sandbox-ai-app/tests/test_codex_cost_app.py)."""
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_runtime_codex_cost", "sandbox/runtime.py")
spend = load("sandbox_spend_codex_cost", "sandbox/spend.py")
spend._modules["runtime"] = rt
TABLE = spend.builtin_prices()
needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")

MODEL = "gpt-5.1-codex"
# One turn's usage as the Responses API says it: 12000 input (10000 of them read from the cache), 300 output (200 of
# them reasoning). At the table's gpt-5.1-codex (USD 1.25 in, 0.125 cached, 10 out per million): USD 0.00675.
USAGE = {"input_tokens": 12000, "input_tokens_details": {"cached_tokens": 10000}, "output_tokens": 300,
         "output_tokens_details": {"reasoning_tokens": 200}, "total_tokens": 12300}
TURN_USD = (2000 * 1.25 + 10000 * 0.125 + 300 * 10) / 1e6
JWT = ("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9." + base64.urlsafe_b64encode(json.dumps(
    {"https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_account_id": "acc-1"}}).encode())
    .decode().rstrip("=") + ".c2lnbmF0dXJl")
API_KEY = "sk-proj-" + "T3st" * 12


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "cfg"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("NO_COLOR", "1")


# --- WebSocket frames (RFC 6455) ------------------------------------------------------------------------------------

def frame(opcode, payload, fin=True, mask=None, rsv1=False):
    """One frame; MASK (4 bytes) as a client sends it."""
    first = (0x80 if fin else 0) | (0x40 if rsv1 else 0) | opcode
    size = len(payload)
    if size < 126:
        head = bytes([first, (0x80 if mask else 0) | size])
    elif size < 1 << 16:
        head = bytes([first, (0x80 if mask else 0) | 126]) + struct.pack(">H", size)
    else:
        head = bytes([first, (0x80 if mask else 0) | 127]) + struct.pack(">Q", size)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return head + mask + payload
    return head + payload


class Reader:
    """Frames off a socket: (fin, rsv1, opcode, payload), unmasked."""

    def __init__(self, sock, data=b""):
        self.sock, self.buffer = sock, data

    def exactly(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("the connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def frame(self):
        first, second = self.exactly(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack(">H", self.exactly(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", self.exactly(8))[0]
        mask = self.exactly(4) if second & 0x80 else None
        payload = self.exactly(size)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(first & 0x80), bool(first & 0x40), first & 0x0F, payload

    def message(self):
        """The next data message (control frames between its pieces answered or skipped): (opcode, payload)."""
        opcode, parts = None, []
        while True:
            fin, _, code, payload = self.frame()
            if code >= 8:
                if code == 8:
                    return 8, payload
                continue
            if code:
                opcode = code
            parts.append(payload)
            if fin:
                return opcode, b"".join(parts)


def read_head(sock, data=b""):
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def headers_of(head):
    return {line.split(b":", 1)[0].strip().lower().decode(): line.split(b":", 1)[1].strip().decode()
            for line in head.split(b"\r\n")[1:] if b":" in line}


def events(number, model=MODEL, usage=USAGE):
    """The Responses API's events of one answer, as chatgpt.com and api.openai.com send them."""
    rid = f"resp_{number:04d}"
    base = {"id": rid, "object": "response", "created_at": 1800000000 + number, "model": model}
    item = {"type": "message", "id": f"msg_{number}", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": "Done: tests pass.", "annotations": []}]}
    return [{"type": "response.created", "sequence_number": 0, "response": {**base, "status": "in_progress",
                                                                             "output": [], "usage": None}},
            {"type": "response.in_progress", "sequence_number": 1, "response": {**base, "status": "in_progress",
                                                                                "usage": None}},
            {"type": "response.output_item.added", "sequence_number": 2, "output_index": 0,
             "item": {**item, "status": "in_progress", "content": []}},
            *[{"type": "response.output_text.delta", "sequence_number": 3 + i, "item_id": f"msg_{number}",
               "output_index": 0, "content_index": 0, "delta": piece} for i, piece in enumerate(("Done: ", "tests ",
                                                                                                  "pass."))],
            {"type": "response.output_item.done", "sequence_number": 6, "output_index": 0, "item": item},
            {"type": "response.completed", "sequence_number": 7, "response": {
                **base, "status": "completed", "output": [item], "parallel_tool_calls": False,
                "reasoning": {"effort": "medium", "summary": None}, "store": False, "tools": [],
                "usage": usage, "user": None, "metadata": {}}}]


class Backend:
    """chatgpt.com's or api.openai.com's Codex endpoints, over TLS with a certificate for NAME: the Responses API over
    a WebSocket (each response.create answered with the events of one turn, response.completed in two fragments with
    a ping between; permessage-deflate when the client offers it, as the real servers do), the same over HTTP and SSE
    (Codex's fallback), the compaction endpoint, and requests that are no model call (the model list, the plan's
    usage)."""

    def __init__(self, pki, name):
        self.name, self.seen, self.turns, self.lock = name, [], 0, threading.Lock()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(rt.server_certificate(pki["upstream_dir"], name)),
                                     str(pki["upstream_dir"] / "leaf.key"))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(raw,), daemon=True).start()

    def close(self):
        self.listener.close()

    def serve(self, raw):
        try:
            with self.context.wrap_socket(raw, server_side=True) as tls:
                head, rest = read_head(tls)
                if not head:
                    return
                line = head.split(b"\r\n")[0].decode()
                method, path, _ = line.split(" ", 2)
                headers = headers_of(head)
                with self.lock:
                    self.seen.append((method, path, headers))
                if headers.get("upgrade", "").lower() == "websocket":
                    return self.websocket(tls, headers, rest)
                size = int(headers.get("content-length") or 0)
                while len(rest) < size:
                    rest += tls.recv(65536)
                if path.endswith("/responses/compact"):
                    body = {"id": "cmp_1", "object": "response.compaction", "output": [], "created_at": 1}
                    if self.name == "api.openai.com":
                        body["usage"] = USAGE
                    return self.answer(tls, b"application/json", json.dumps(body).encode())
                if method == "POST" and path.endswith("/responses"):
                    return self.sse(tls)
                if path.startswith("/backend-api/codex/models") or path.startswith("/v1/models"):
                    return self.answer(tls, b"application/json", json.dumps(
                        {"models": [{"slug": MODEL, "usage": {"input_tokens": 1}}]}).encode())
                return self.answer(tls, b"application/json", json.dumps(
                    {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 3}}}).encode())
        except (OSError, ValueError, EOFError):
            return

    @staticmethod
    def answer(tls, kind, body):
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + kind + b"\r\nContent-Length: %d\r\nConnection: close\r\n"
                    b"\r\n" % len(body) + body)

    def turn(self):
        with self.lock:
            self.turns += 1
            return self.turns

    def sse(self, tls):
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\n")
        for event in events(self.turn()):
            data = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
            tls.sendall(b"%x\r\n" % len(data) + data + b"\r\n")
        tls.sendall(b"0\r\n\r\n")

    def websocket(self, tls, headers, rest):
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                               .encode()).digest())
        deflate = "permessage-deflate" in headers.get("sec-websocket-extensions", "")
        tls.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: " + accept + b"\r\n" +
                    (b"Sec-WebSocket-Extensions: permessage-deflate\r\n" if deflate else b"") + b"\r\n")
        compressor = zlib.compressobj(wbits=-15) if deflate else None
        reader = Reader(tls, rest)
        while True:
            opcode, payload = reader.message()
            if opcode == 8:
                tls.sendall(frame(8, payload[:2]))
                return
            request = json.loads(payload)
            assert request["type"] == "response.create"
            for event in events(self.turn(), request.get("model") or MODEL):
                data = json.dumps(event).encode()
                rsv1 = False
                if compressor is not None:
                    data = (compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH))[:-4]
                    rsv1 = True
                if event["type"] == "response.completed":
                    half = len(data) // 2
                    tls.sendall(frame(1, data[:half], fin=False, rsv1=rsv1) + frame(9, b"keep") +
                                frame(0, data[half:]))
                else:
                    tls.sendall(frame(1, data, rsv1=rsv1))


# --- the proxy, as the supervisor runs it ----------------------------------------------------------------------------

@pytest.fixture
def pki(tmp_path):
    """The session CA (the proxy's) and an upstream CA whose certificates the proxy trusts."""
    session, upstream = tmp_path / "session-run", tmp_path / "upstream-run"
    rt.create_ca(session, tmp_path / "session-ca", bundles=())
    rt.create_ca(upstream, tmp_path / "upstream-ca", bundles=())
    return {"run": session, "ca": tmp_path / "session-ca" / "ca.pem", "upstream_dir": upstream,
            "upstream": ssl.create_default_context(cafile=str(upstream / "ca.pem"))}


class Proxy:
    """The real ProxyServer with the spend meter and the run's spend log in the state directory's logs (where
    `sandbox-ai spend` and `stats usage` read it); the backend for each name at its own port."""

    def __init__(self, tmp_path, pki, backends, run="codex-plan1", name="plan", budgets=(), logs=None):
        self.backends = backends
        self.log = tmp_path / f"connections-{run}.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.logs = Path(logs) if logs else tmp_path / "state" / "sandbox-ai" / "logs" / "api-0123456789ab"
        self.logs.mkdir(parents=True, exist_ok=True)
        file = f"spend-20261120-100000-{run}.jsonl"
        self.spend_fd = rt.open_spend_log({"spend": file}, self.logs)
        self.path = self.logs / file
        self.ports = {b.name: b.port for b in backends}
        config = {"body_limit": 65536, "allow": {"loopback": list(self.ports.values())}, "spend": {
            "run": run, "name": name, "project": "/p/api", "project_key": "api-0123456789ab", "agent": "codex",
            "prices": TABLE, "budgets": list(budgets)}}
        self.chosen = {}
        self.server = rt.ProxyServer(config, self.fd, None, run_dir=pki["run"],
                                     destination=lambda sock: ("127.0.0.1", self.chosen.get("port")),
                                     upstream=pki["upstream"])
        self.server.spend = rt.SpendMeter(config["spend"], self.spend_fd, pki["run"] / rt.SPEND_CONTROL)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.ca = pki["ca"]
        self.server.serve([self.listener])

    def connect(self, name):
        """A TLS connection to NAME through the proxy (as Codex makes it: the proxy redirects it)."""
        self.chosen["port"] = self.ports[name]
        context = ssl.create_default_context(cafile=str(self.ca))
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        return context.wrap_socket(raw, server_hostname=name)

    def records(self, until, wait=5.0):
        deadline = time.monotonic() + wait
        while True:
            found = [json.loads(line) for line in self.log.read_text().splitlines()]
            if until(found) or time.monotonic() > deadline:
                return found
            time.sleep(0.05)

    def spent(self):
        return [r for r in (json.loads(line) for line in self.path.read_text().splitlines()) if "event" not in r]

    def close(self):
        self.listener.close()
        os.close(self.fd)
        os.close(self.spend_fd)


def codex_headers(name, token):
    """The headers Codex 0.166 sends to NAME with TOKEN (a ChatGPT login's JWT, or an API key)."""
    lines = [f"Host: {name}", f"Authorization: Bearer {token}", "originator: codex_cli_rs", "version: 0.166.0",
             "session_id: 0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
             "User-Agent: codex_cli_rs/0.166.0 (Debian 13.0.0; x86_64) xterm-256color",
             'x-codex-turn-metadata: {"turn_id":"t1","sandbox":"workspace-write"}']
    if name == "chatgpt.com":
        lines.append("chatgpt-account-id: acc-1")
    return "".join(line + "\r\n" for line in lines).encode()


def model_path(name):
    return "/backend-api/codex/responses" if name == "chatgpt.com" else "/v1/responses"


def create(number):
    return {"type": "response.create", "model": MODEL, "instructions": "You are Codex.", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"turn {number}"}]}],
        "tools": [], "tool_choice": "auto", "parallel_tool_calls": False,
        "reasoning": {"effort": "medium", "summary": "auto"}, "store": False, "stream": True,
        "include": ["reasoning.encrypted_content"], "prompt_cache_key": "0199a1b2"}


def websocket_turns(proxy, name, token, turns=2, path=None):
    """Codex's turns over one WebSocket: the handshake, each response.create (masked, as a client sends it) and the
    events of its answer read to response.completed; then the close. Returns (status line, [completed events])."""
    with proxy.connect(name) as tls:
        key = base64.b64encode(os.urandom(16))
        tls.sendall(f"GET {path or model_path(name)} HTTP/1.1\r\n".encode() + codex_headers(name, token) +
                    b"Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
                    b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Extensions: permessage-deflate; "
                    b"client_max_window_bits\r\nOpenAI-Beta: responses_websockets=2026-02-06\r\n\r\n")
        head, rest = read_head(tls)
        status = head.split(b"\r\n")[0].decode()
        if " 101 " not in status:
            return status, []
        reader, completed = Reader(tls, rest), []
        inflate = zlib.decompressobj(wbits=-15) if "permessage-deflate" in headers_of(head).get(
            "sec-websocket-extensions", "") else None
        for number in range(turns):
            try:
                tls.sendall(frame(1, json.dumps(create(number)).encode(), mask=os.urandom(4)))
                while True:
                    opcode, payload = reader.message()
                    if opcode == 8:
                        return status, completed
                    if inflate is not None:
                        payload = inflate.decompress(payload + b"\x00\x00\xff\xff")
                    event = json.loads(payload)
                    if event["type"] == "response.completed":
                        completed.append(event)
                        break
            except (OSError, EOFError):
                return status, completed
        with contextlib.suppress(OSError, EOFError):
            tls.sendall(frame(8, struct.pack(">H", 1000), mask=os.urandom(4)))
            while reader.message()[0] != 8:
                pass
        return status, completed


def http(proxy, name, method, path, token, body=None):
    """One of Codex's HTTP requests (the SSE fallback, the compaction, the model list, the plan's usage)."""
    data = json.dumps(body).encode() if body is not None else b""
    with proxy.connect(name) as tls:
        tls.sendall(f"{method} {path} HTTP/1.1\r\n".encode() + codex_headers(name, token) +
                    (b"Content-Type: application/json\r\nAccept: text/event-stream\r\n" if body is not None else b"")
                    + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(data) + data)
        answer = b""
        while chunk := tls.recv(65536):
            answer += chunk
    return answer


def plan_run(tmp_path, pki, run="codex-plan1", name="plan", logs=None, fallback=True):
    """A Codex run logged in with a ChatGPT plan: two turns over the WebSocket (Codex's default), with FALLBACK one
    over HTTP+SSE too and the compaction; the model list and the plan's usage. Returns (proxy, backend)."""
    backend = Backend(pki, "chatgpt.com")
    proxy = Proxy(tmp_path, pki, [backend], run=run, name=name, logs=logs)
    status, completed = websocket_turns(proxy, "chatgpt.com", JWT)
    assert " 101 " in status and len(completed) == 2, (status, completed)
    assert completed[1]["response"]["usage"] == USAGE  # the answer reached Codex whole
    if fallback:
        answer = http(proxy, "chatgpt.com", "POST", "/backend-api/codex/responses", JWT, create(9))
        assert b" 200 " in answer.split(b"\r\n")[0] and b"response.completed" in answer
        http(proxy, "chatgpt.com", "POST", "/backend-api/codex/responses/compact", JWT, {"model": MODEL, "input": []})
    http(proxy, "chatgpt.com", "GET", "/backend-api/codex/models?client_version=0.166.0", JWT)
    http(proxy, "chatgpt.com", "GET", "/backend-api/wham/usage", JWT)
    proxy.records(lambda found: sum(r.get("event") in ("http", "upgraded") for r in found) >= (6 if fallback else 3))
    return proxy, backend


def api_run(tmp_path, pki, run="codex-key1", name="key", budgets=(), turns=2, logs=None):
    backend = Backend(pki, "api.openai.com")
    proxy = Proxy(tmp_path, pki, [backend], run=run, name=name, budgets=budgets, logs=logs)
    return proxy, backend, websocket_turns(proxy, "api.openai.com", API_KEY, turns)


def cli(tmp_path, *argv):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "state"),
           "XDG_DATA_HOME": str(tmp_path / "data"), "NO_COLOR": "1"}
    env.pop("XDG_RUNTIME_DIR", None)
    done = subprocess.run([str(ROOT / "sandbox-ai"), *argv], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


def places(tmp_path, run):
    """What each place that shows cost says of RUN: `sandbox-ai spend` (its row), `sandbox-ai spend run`, `stats
    usage` (the System monitor's text and its per-run data), the report."""
    listed = cli(tmp_path, "spend")
    row = next(line for line in listed.splitlines() if line.startswith(run))
    shown = cli(tmp_path, "spend", "run", run).splitlines()[0]
    usage = json.loads(cli(tmp_path, "stats", "usage", "--since", "1d", "--json", "--by", "run"))
    text = cli(tmp_path, "stats", "usage", "--since", "1d", "--by", "run")
    report = load("sandbox_report_codex_cost", "sandbox/report.py")
    found = report.spend_data([{"run": run, "log_dir": str(tmp_path / "state" / "sandbox-ai" / "logs" /
                                                           "api-0123456789ab")}])
    return {"list": row, "run": shown, "usage": usage["spend"]["runs"][run], "text": text,
            "report": found["runs"][0]["cost"], "report_models": [m["cost"] for m in found["runs"][0]["models"]],
            "state": json.loads(cli(tmp_path, "spend", "run", run, "--json"))}


# --- the failing shapes, end to end ----------------------------------------------------------------------------------

@needs_openssl
def test_a_codex_plan_run_s_websocket_turns_are_counted_as_a_subscription(tmp_path, pki):
    """The bug: Codex's WebSocket turns on a ChatGPT plan were relayed unread - no request in the spend log, "USD
    0.00" / "-" in the monitor. Now each turn is one request billed "subscription", its tokens counted; the HTTP+SSE
    fallback too; the model list and the plan's usage are no model request."""
    proxy, backend = plan_run(tmp_path, pki)
    try:
        requests = proxy.spent()
        assert [r["billing"] for r in requests] == ["subscription"] * 3, requests
        assert all(r["provider"] == "openai" and r["model"] == MODEL and r["usd"] is None for r in requests)
        assert all((r["in"], r["cached"], r["out"], r["reasoning"]) == (2000, 10000, 300, 200) for r in requests)
        assert [r.get("api") for r in requests] == ["responses"] * 3
        # The proxy took the compression offer off the metered WebSocket (its frames are read for the usage), and
        # left the rest of Codex's handshake as it was.
        handshake = next(h for m, p, h in backend.seen if h.get("upgrade") == "websocket")
        assert "sec-websocket-extensions" not in handshake
        assert handshake["openai-beta"] == "responses_websockets=2026-02-06"
        assert handshake["chatgpt-account-id"] == "acc-1" and handshake["authorization"] == f"Bearer {JWT}"
        upgraded = next(r for r in proxy.records(lambda f: True) if r.get("event") == "upgraded")
        assert upgraded["model_requests"] == 2
        log = proxy.log.read_text() + proxy.path.read_text()
        assert JWT not in log and "acc-1" not in proxy.path.read_text()
    finally:
        proxy.close()
        backend.close()
    shown = places(tmp_path, "codex-plan1")
    assert shown["list"].split()[1] == "3" and shown["list"].endswith("  subscription"), shown["list"]
    assert shown["run"].startswith("plan: subscription - 3 model requests"), shown["run"]
    assert shown["usage"]["subscription"] == 3 and shown["usage"]["requests"] == 3 and shown["usage"]["usd"] == 0
    assert "codex-plan1: subscription - 3 requests" in shown["text"], shown["text"]
    assert shown["report"] == "subscription" and shown["report_models"] == ["subscription"]
    assert shown["state"]["subscription"] == 3 and shown["state"]["requests"] == 3
    assert "USD" not in shown["list"] + shown["run"] + shown["report"]


@needs_openssl
def test_an_api_key_codex_run_s_websocket_turns_are_paid_per_token(tmp_path, pki):
    """Codex with an API key: the same WebSocket to api.openai.com is billed per token ("api"), priced at the table;
    its compaction request too (it says its usage)."""
    proxy, backend, (status, completed) = api_run(tmp_path, pki)
    try:
        assert " 101 " in status and len(completed) == 2
        http(proxy, "api.openai.com", "POST", "/v1/responses/compact", API_KEY, {"model": MODEL, "input": []})
        proxy.records(lambda found: sum(r.get("event") in ("http", "upgraded") for r in found) >= 2)
        requests = proxy.spent()
        assert [r["billing"] for r in requests] == ["api"] * 3, requests
        assert [r["usd"] for r in requests] == [pytest.approx(TURN_USD)] * 3
        assert API_KEY not in proxy.log.read_text() + proxy.path.read_text()
    finally:
        proxy.close()
        backend.close()
    shown = places(tmp_path, "codex-key1")
    assert shown["list"].endswith("  USD 0.02"), shown["list"]
    assert shown["run"].startswith("key: USD 0.02 - 3 model requests"), shown["run"]
    assert shown["usage"]["subscription"] == 0 and shown["usage"]["usd"] == pytest.approx(3 * TURN_USD)
    assert shown["report"] == "USD 0.02" and "subscription" not in shown["text"]


@needs_openssl
def test_a_mixed_run_says_what_was_paid_and_the_subscription(tmp_path, pki):
    """One run on the plan and an API key at once (a second Codex in it with a key): "USD x + subscription" - only
    because some requests really were paid per token."""
    plan, key = Backend(pki, "chatgpt.com"), Backend(pki, "api.openai.com")
    proxy = Proxy(tmp_path, pki, [plan, key], run="codex-mixed1", name="mixed")
    try:
        assert len(websocket_turns(proxy, "chatgpt.com", JWT)[1]) == 2
        assert len(websocket_turns(proxy, "api.openai.com", API_KEY, turns=1)[1]) == 1
        proxy.records(lambda found: sum(r.get("event") == "upgraded" for r in found) >= 2)
        assert sorted(r["billing"] for r in proxy.spent()) == ["api", "subscription", "subscription"]
    finally:
        proxy.close()
        plan.close()
        key.close()
    shown = places(tmp_path, "codex-mixed1")
    assert shown["list"].endswith("  USD 0.0067 + subscription"), shown["list"]
    assert shown["run"].startswith("mixed: USD 0.0067 + subscription - 3 model requests"), shown["run"]
    assert shown["report"] == "USD 0.0067 + subscription"


@needs_openssl
def test_a_budget_holds_back_an_api_key_s_websocket_turns(tmp_path, pki):
    """Before, an API key's WebSocket turns were never counted, so a budget never ended them. Now the turn that uses
    it up is answered whole, then the connection ends; Codex's new one is refused before it goes out (HTTP 402)."""
    proxy, backend, (status, completed) = api_run(
        tmp_path, pki, budgets=[{"id": "run", "scope": "run", "usd": 0.005, "used": 0,
                                 "label": "budget of this run (--budget)"}], turns=3)
    try:
        assert " 101 " in status and len(completed) == 1  # the turn under way was not cut
        again, _ = websocket_turns(proxy, "api.openai.com", API_KEY, turns=1)
        assert " 402 " in again, again
        assert backend.turns == 1
        records = [json.loads(line) for line in proxy.path.read_text().splitlines()]
        assert [r["event"] for r in records if "event" in r][:1] == ["run"] and \
            any(r.get("event") == "refused" for r in records)
        assert [r["billing"] for r in records if "event" not in r] == ["api"]
    finally:
        proxy.close()
        backend.close()


@needs_openssl
def test_a_websocket_on_another_path_of_a_provider_is_counted_too(tmp_path, pki):
    """Review (security): a WebSocket to a provider's host on a path the model paths do not name (a trailing slash,
    the Realtime API's) is metered all the same - no way around the count or a budget."""
    backend = Backend(pki, "api.openai.com")
    proxy = Proxy(tmp_path, pki, [backend], run="codex-odd1")
    try:
        for path in ("/v1/responses/", "/v1/realtime?model=gpt-5.1-codex"):
            assert len(websocket_turns(proxy, "api.openai.com", API_KEY, turns=1, path=path)[1]) == 1
        proxy.records(lambda found: sum(r.get("event") == "upgraded" for r in found) >= 2)
        assert [(r["billing"], r["usd"]) for r in proxy.spent()] == [("api", pytest.approx(TURN_USD))] * 2
    finally:
        proxy.close()
        backend.close()


@needs_openssl
def test_a_plan_run_is_never_held_back_by_a_budget(tmp_path, pki):
    """A subscription has no price per token: a used-up budget does not end its WebSocket."""
    backend = Backend(pki, "chatgpt.com")
    proxy = Proxy(tmp_path, pki, [backend], budgets=[{"id": "run", "scope": "run", "usd": 0.0, "used": 0}])
    try:
        status, completed = websocket_turns(proxy, "chatgpt.com", JWT, turns=3)
        assert " 101 " in status and len(completed) == 3
    finally:
        proxy.close()
        backend.close()


# --- the web page's cost cells and box: the real `stats usage` and `spend run` of a plan run, under node -------------

WEB = ROOT / "sandbox" / "web"


@needs_openssl
@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_web_monitor_says_subscription_for_a_codex_plan_run(tmp_path, pki):
    proxy, backend = plan_run(tmp_path, pki, run="codex-api1", name="api")
    proxy.close()
    backend.close()
    usage = json.loads(cli(tmp_path, "stats", "usage", "--since", "24h", "--json", "--series", "--by", "run"))
    state = json.loads(cli(tmp_path, "spend", "run", "codex-api1", "--json"))
    spec = importlib.util.spec_from_file_location("test_web_monitor_for_codex_cost", ROOT / "tests" /
                                                  "test_web_monitor.py")
    monitor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(monitor)
    driver = tmp_path / "cost.js"
    driver.write_text(monitor.T.FAKE_DOM + r"""
(async () => {
  const TOP = JSON.parse(process.argv[3]), USAGE = JSON.parse(process.argv[4]), SPEND = JSON.parse(process.argv[5]);
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.page = "monitor"; state.host = "ct1";', context);
  answer = (method, path) => (path.endsWith("/monitor") ? TOP : path.includes("/monitor/usage/") ? USAGE
    : path.endsWith("/spend/codex-api1") ? SPEND : { output: "ok" });
  await vm.runInContext("pages.monitor(__main)", context); await settle();
  const result = {};
  const rows = main.all().filter(n => n.tagName === "tr" && n.children.length === 11 && n.children[0].tagName === "td");
  result.cost = rows.map(r => [r.children[0].textContent, r.children[9].textContent]);
  main.all().find(n => n.tagName === "button" && n.textContent === "api").fire("click"); await settle();
  result.panel = main.all().filter(n => n.className === "spend-panel").map(n => n.textContent).join(" ");
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
""")
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), json.dumps(monitor.TOP), json.dumps(usage),
                          json.dumps(state)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert ["api", "subscription"] in result["cost"], result["cost"]
    assert "subscription so far - 3 model requests" in result["panel"], result["panel"]
    assert "USD" not in dict(result["cost"])["api"]


# --- the reader of the frames, piece by piece ------------------------------------------------------------------------

class Meter:
    def __init__(self, refuse=False):
        self.recorded, self.refuse = [], refuse

    def record(self, provider, model, billing, api, usage, endpoint=None):
        self.recorded.append((provider, model, billing, api, usage))
        return {"usd": None}

    def refusal(self, billing):
        return "used up" if self.refuse and billing == "api" else None


def server_frames(turns=2, **changes):
    data = b""
    for number in range(turns):
        for event in events(number, **changes):
            payload = json.dumps(event).encode()
            if event["type"] == "response.completed":
                data += frame(1, payload[:40], fin=False) + frame(9, b"") + frame(0, payload[40:90], fin=False) + \
                    frame(10, b"x") + frame(0, payload[90:])
            else:
                data += frame(1, payload)
    return data


@pytest.mark.parametrize("step", [1, 7, 125, 4096])
def test_the_reader_counts_each_turn_once_in_any_pieces(step):
    meter = Meter()
    reader = rt.WebSocketUsage(meter, "chatgpt.com", "subscription", "responses")
    data = server_frames(3)
    for start in range(0, len(data), step):
        assert not reader.feed(data[start:start + step])
    assert [m[:4] for m in meter.recorded] == [("openai", MODEL, "subscription", "responses")] * 3
    assert meter.recorded[0][4]["input_tokens"] == 12000 and meter.recorded[0][4]["cached_tokens"] == 10000
    assert reader.count == 3


def test_the_reader_s_edges():
    meter = Meter()
    reader = rt.WebSocketUsage(meter, "api.openai.com", "api", "responses")
    # The same response's usage twice (a done after the completed) is one request; a failed one with usage counts.
    completed = events(1)[-1]
    reader.feed(frame(1, json.dumps(completed).encode()) + frame(1, json.dumps(completed).encode()))
    failed = {"type": "response.failed", "response": {"id": "resp_x", "model": MODEL, "status": "failed",
                                                      "usage": {"input_tokens": 50, "output_tokens": 0}}}
    reader.feed(frame(2, json.dumps(failed).encode()))  # (a binary frame: read the same way)
    assert len(meter.recorded) == 2 and meter.recorded[1][4]["input_tokens"] == 50
    # No usage, a usage of null, other JSON, broken JSON, a compressed frame (never offered: not read) - nothing.
    for payload in (events(2)[0], {"type": "error", "error": {"message": "x"}}, [1, 2], "x"):
        reader.feed(frame(1, json.dumps(payload).encode()))
    reader.feed(frame(1, b'{"type":"response.completed","response":{"usage":{"input_tokens":'))
    reader.feed(frame(1, zlib.compress(json.dumps(events(3)[-1]).encode()), rsv1=True))
    assert len(meter.recorded) == 2
    # A masked frame (a client's) is unmasked; a message far larger than what is kept is read by its end.
    reader.feed(frame(1, json.dumps(events(4)[-1]).encode(), mask=b"\x01\x02\x03\x04"))
    big = events(5)[-1]
    big["response"]["output"] = [{"type": "message", "content": [{"type": "output_text", "text": "y" * (
        rt.WebSocketUsage.LIMIT + 1000)}]}]
    payload = json.dumps(big).encode()
    for start in range(0, len(payload), 1 << 20):
        reader.feed(frame(1 if not start else 0, payload[start:start + (1 << 20)],
                          fin=start + (1 << 20) >= len(payload)))
    assert len(meter.recorded) == 4 and meter.recorded[3][1] == MODEL and meter.recorded[3][4]["output_tokens"] == 300
    # Garbage never raises; the close frame ends nothing on its own.
    assert reader.feed(os.urandom(5000)) is False
    # A used-up budget: True once a paid turn was counted (the relay then ends the connection).
    refusing = rt.WebSocketUsage(Meter(refuse=True), "api.openai.com", "api", "responses")
    assert refusing.feed(frame(1, json.dumps(events(1)[0]).encode())) is False
    assert refusing.feed(frame(1, json.dumps(events(1)[-1]).encode())) is True
    plan = rt.WebSocketUsage(Meter(refuse=True), "chatgpt.com", "subscription", "responses")
    assert plan.feed(frame(1, json.dumps(events(1)[-1]).encode())) is False


def test_the_monitor_s_per_run_cost_knows_a_local_run(tmp_path):
    """`stats usage`'s per-run spend (the app's and the web's Cost column) kept no "local": a run on an --llm server
    of your own said "USD 0.00" there instead of "0 (local)"."""
    folder = tmp_path / "state" / "sandbox-ai" / "logs" / "p-1"
    folder.mkdir(parents=True)
    now = time.time()
    (folder / "spend-20261120-100000-local-1.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"v": 1, "time": now - 60, "event": "run", "run": "local-1", "project_key": "p-1", "agent": "codex"},
        {"v": 1, "time": now - 50, "provider": "llm:box", "model": "qwen3", "billing": "local", "in": 10, "usd": 0},
    )) + "\n")
    monitor = load("sandbox_monitor_codex_cost", "sandbox/monitor.py")
    assert monitor.usage("1d", "run")["spend"]["runs"]["local-1"]["local"] == 1
