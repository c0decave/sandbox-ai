"""--llm, a local model server: the proxy keeps a streamed answer alive while the server reads a long prompt.

User: with local models "some took ages before they finished". LM Studio answers a streamed request with its headers
at once and then sends nothing until the first token - minutes for a fresh 50-100k prompt on a large local model. The
agents wait for that with the settings sandbox-ai gives them (tests/test_llm_waits.py), except two that have no
setting for it (the local-model research, phase 2's evidence/timeouts_mock.txt and round 1's evidence/waits.txt,
the real CLIs against a server that is silent for 420 s after its headers, through sandbox-ai's own proxy):
Claude Code gives the request up after 300 s without a byte and sends it again, DeepSeek Harness after 300 s
("pi-ai stream idle timeout") - and the server starts the whole prompt from the beginning each time: the turn never
ends. With a keep-alive in the stream every 20 s Claude Code waits and finishes (a comment line or Anthropic's
ping event: round 1, evidence/waits.txt); DeepSeek Harness does not (neither with a comment line nor with an empty
chunk of the API's own shape: its limit counts something else).

So the proxy sends the keep-alive itself: while a streamed answer of the --llm endpoint (a model server of your
own) stands between two events and the server has sent nothing for the endpoint's "keepalive" seconds, the agent
gets a line a server-sent-events reader skips (": keep-alive") - Anthropic's own "ping" event on the Messages API.
And an agent that went away during the silence is noticed then, not at the first token: the server's connection
closes, and it stops reading a prompt nobody waits for.

Unit tests of runtime.llm_beat and llm_kept_alive, llm.proxy_params' setting, and end to end through the real proxy
(ProxyServer) with a model server that is silent before and between its events."""
import argparse
import http.server
import json
from pathlib import Path
import select
import shutil
import socket
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402 - its Relay (the real proxy)

rt, llm = live.rt, live.llm
PATHS = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "messages": "/v1/messages"}
COMMENT = b": keep-alive\n\n"
PING = b'event: ping\ndata: {"type": "ping"}\n\n'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


# --- the beat ---------------------------------------------------------------------------------------------------------

def test_the_keepalive_is_a_line_every_reader_of_the_api_skips():
    assert rt.llm_beat("chat") == rt.llm_beat("responses") == COMMENT
    assert rt.llm_beat("messages") == PING, "Anthropic's API sends this event itself; its clients skip it"
    assert rt.llm_beat("completions") == COMMENT and rt.llm_beat(None) == COMMENT


class Feed:
    """A server's side of a socket pair as runtime.Stream reads it, with the body's whole chunks."""

    def __init__(self):
        self.server, client = socket.socketpair()
        self.stream = rt.Stream(client)

    def chunk(self, data):
        self.server.sendall(b"%x\r\n" % len(data) + data + b"\r\n")

    def end(self):
        self.server.sendall(b"0\r\n\r\n")

    def pieces(self, seconds=0.2, api="chat"):
        return rt.llm_kept_alive(rt.whole_chunks(self.stream.body(("chunked",))), self.stream, seconds,
                                 rt.llm_beat(api))


def framed(data):
    return b"%x\r\n" % len(data) + data + b"\r\n"


def test_beats_while_the_server_is_silent_and_its_chunks_as_they_come():
    feed = Feed()
    pieces = feed.pieces()
    started = time.monotonic()
    assert next(pieces) == (framed(COMMENT), None) and next(pieces) == (framed(COMMENT), None)
    assert 0.35 < time.monotonic() - started < 2, "one every 0.2 s of silence"
    event = b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
    feed.chunk(event)
    assert next(pieces) == (framed(event), event)
    feed.chunk(b"data: [DONE]\n\n")
    feed.end()
    assert list(pieces) == [(framed(b"data: [DONE]\n\n"), b"data: [DONE]\n\n"), (b"0\r\n\r\n", b"")]


def test_no_beat_inside_an_event_and_none_after_the_end():
    """A server that stops in the middle of an event (its line not ended) gets no line of ours put there; and
    after the body's last chunk nothing follows (the connection may carry the agent's next request)."""
    feed = Feed()
    pieces = feed.pieces(seconds=0.1)
    feed.chunk(b'data: {"choices": [{"delta": {"con')
    assert next(pieces)[1] == b'data: {"choices": [{"delta": {"con'
    got = []
    reader = threading.Thread(target=lambda: got.append(next(pieces)), daemon=True)
    reader.start()
    reader.join(0.5)
    assert got == [], "nothing while the event is open, however long the server waits"
    feed.chunk(b'tent": "Hi"}}]}\n\n')
    reader.join(2)
    assert got == [(framed(b'tent": "Hi"}}]}\n\n'), b'tent": "Hi"}}]}\n\n')]
    assert next(pieces) == (framed(COMMENT), None), "between two events again"
    feed.end()
    assert next(pieces) == (b"0\r\n\r\n", b"")
    started = time.monotonic()
    assert list(pieces) == [] and time.monotonic() - started < 0.09, "the body ended: no waiting, no beat"


def test_what_is_buffered_already_comes_before_any_beat():
    feed = Feed()
    feed.chunk(b"data: 1\n\n")
    feed.chunk(b"data: 2\n\n")
    feed.end()
    time.sleep(0.3)
    assert [payload for _, payload in feed.pieces(seconds=0.1)] == [b"data: 1\n\n", b"data: 2\n\n", b""]


def test_a_server_that_closes_in_the_silence_ends_the_body_as_before():
    feed = Feed()
    pieces = feed.pieces(seconds=0.1)
    assert next(pieces) == (framed(COMMENT), None)
    feed.server.close()
    with pytest.raises(ValueError):  # as Stream.body without the keep-alive: "bad chunked encoding"
        for _ in range(5):
            next(pieces)


# --- the endpoint's setting -------------------------------------------------------------------------------------------

def test_a_model_server_of_your_own_gets_the_keepalive_and_a_public_service_none():
    def entry(kind, url, **more):
        assert llm.main(["add", "x", "--kind", kind, "--url", url, "--model", "m", "--replace"],
                        out=lambda line: None) == 0
        a = argparse.Namespace(llm="x", agent="claude", llm_param=[], **more)
        a.llm_addresses = ["192.168.1.20"]
        return llm.proxy_entry(a)[0]
    for kind, url in (("lmstudio", "http://192.168.1.20:1234"), ("llamacpp", "http://192.168.1.20:8080"),
                      ("ollama", "http://192.168.1.20:11434"), ("vllm", "http://192.168.1.20:8000")):
        assert entry(kind, url)["keepalive"] == llm.KEEPALIVE == 20
    assert "keepalive" not in entry("openai-compatible", "https://openrouter.ai/api/v1")


# --- end to end: the real proxy, a server that is silent ------------------------------------------------------------------

EVENTS = {
    "chat": [b'data: {"id": "c", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "DO"}}]}\n\n',
             b'data: {"id": "c", "choices": [{"index": 0, "delta": {"content": "NE"}, "finish_reason": "stop"}]}\n\n',
             b"data: [DONE]\n\n"],
    "responses": [b'event: response.created\ndata: {"type": "response.created", "response": {"id": "r"}}\n\n',
                  b'event: response.output_text.delta\ndata: {"type": "response.output_text.delta", "delta": "DONE"}\n\n',
                  b'event: response.completed\ndata: {"type": "response.completed", "response": {"id": "r", '
                  b'"status": "completed", "output": []}}\n\n'],
    "messages": [b'event: message_start\ndata: {"type": "message_start", "message": {"id": "m", "role": "assistant", '
                 b'"content": []}}\n\n',
                 b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": '
                 b'"text_delta", "text": "DONE"}}\n\n',
                 b'event: message_stop\ndata: {"type": "message_stop"}\n\n'],
}


class Slow(http.server.BaseHTTPRequestHandler):
    """A model server like LM Studio's: the headers at once, then nothing for WAIT seconds (it reads the prompt),
    then its events. PLAN: [(seconds before, bytes)] in place of that; GONE: whether the client's connection was
    closed when the wait was over."""
    protocol_version = "HTTP/1.1"
    wait, plan, gone, events = 1.0, None, [], EVENTS["chat"]

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        try:
            for before, data in Slow.plan or [(Slow.wait, Slow.events[0])] + [(0, e) for e in Slow.events[1:]]:
                time.sleep(before)
                readable = select.select([self.connection], [], [], 0)[0]
                Slow.gone.append(bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b"")
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            Slow.gone.append(True)


@pytest.fixture
def slow():
    Slow.wait, Slow.plan, Slow.gone, Slow.events = 1.0, None, [], EVENTS["chat"]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def relay_with(tmp_path, slow, keepalive):
    relay = live.Relay(tmp_path, slow.server_address[1])
    if keepalive is None:
        relay.server.config["llm"][0].pop("keepalive", None)
    else:
        relay.server.config["llm"][0]["keepalive"] = keepalive
    return relay


def request(api):
    if api == "responses":
        return {"model": "m", "stream": True, "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "a long prompt"}]}]}
    return {"model": "m", "stream": True, "max_tokens": 100, "messages": [{"role": "user", "content": "a long prompt"}]}


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("api", ["chat", "responses", "messages"])
def test_e2e_the_agent_gets_keepalives_while_the_server_reads_the_prompt(tmp_path, slow, api):
    """Headers at once, 1.2 s of silence, then the answer: with a keep-alive of 0.3 s the agent gets three or four
    beats before the first event (never a silence longer than that), and the server's events byte for byte."""
    Slow.wait, Slow.events = 1.2, EVENTS[api]
    relay = relay_with(tmp_path, slow, 0.3)
    try:
        head, body = live.dechunk(relay.post(PATHS[api], request(api)))
        assert b" 200 " in head.split(b"\r\n", 1)[0]
        beat = PING if api == "messages" else COMMENT
        count = body.count(beat)
        # 1.2 s / 0.3 s: three or four beats; a loaded machine (the full suites) wakes the beat or the server late,
        # so a range around it - what matters is that beats come, only before the first event, and not without end.
        assert 2 <= count <= 6 and body.startswith(beat * count)
        assert body[len(beat) * count:] == b"".join(EVENTS[api]), "the server's own events as they came"
        assert live.read_answer(api, body)["text"] == "DONE", "an agent's reader of the stream takes no notice"
        record = relay.records(1)[0]
        assert record["status"] == 200 and b"keep-alive" not in json.dumps(record).encode() and \
            '"ping"' not in json.dumps(record["response"]), "the log keeps the server's answer, not the proxy's beats"
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("keepalive", [None, 0])
def test_e2e_without_the_setting_the_stream_passes_as_it_came(tmp_path, slow, keepalive):
    Slow.wait = 0.5
    relay = relay_with(tmp_path, slow, keepalive)
    try:
        _, body = live.dechunk(relay.post(PATHS["chat"], request("chat")))
        assert body == b"".join(EVENTS["chat"])
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_silence_between_two_events_gets_beats_and_silence_inside_one_none(tmp_path, slow):
    first, second = EVENTS["chat"][0], EVENTS["chat"][1]
    Slow.plan = [(0, first), (0.7, second[:20]), (0.7, second[20:]), (0, EVENTS["chat"][2])]
    relay = relay_with(tmp_path, slow, 0.25)
    try:
        _, body = live.dechunk(relay.post(PATHS["chat"], request("chat")))
        assert body.replace(COMMENT, b"") == b"".join(EVENTS["chat"])
        between = body[len(first):body.index(second[:20], len(first))]
        # 0.7 s / 0.25 s: two or three beats (a range for a loaded machine)
        assert between == COMMENT * between.count(COMMENT) and 1 <= between.count(COMMENT) <= 5
        assert second in body, "the event the server sent in two parts is whole: no beat inside it"
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("keepalive,noticed", [(0.2, True), (None, True)])
def test_e2e_an_agent_that_went_away_is_noticed_in_the_silence(tmp_path, slow, keepalive, noticed):
    """The agent closes its connection while the server reads the prompt (it gave up, the user pressed Ctrl-C).
    With the keep-alive the proxy's next beat finds out and the server's connection closes - the server stops
    reading a prompt nobody waits for. Without it the proxy found out at the first token - until 0.1.98: now it
    watches the agent's connection while the server works (runtime.LlmClientWatch) and finds out at once either way
    (tests/test_llm_answer_ceiling.py)."""
    Slow.wait = 1.0
    relay = relay_with(tmp_path, slow, keepalive)
    try:
        body = json.dumps(request("chat")).encode()
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as sock:
            sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json"
                         b"\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            assert b" 200 " in sock.recv(4096).split(b"\r\n", 1)[0]
        deadline = time.monotonic() + 5
        while not Slow.gone and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Slow.gone[0] is noticed
    finally:
        relay.close()


# --- with a credential of the run: the scrubber's kept-back bytes go out before a beat ---------------------------------

KEY = "sk-local-0123456789abcdef"


def leading(body, beat=COMMENT):
    count = 0
    while body.startswith(beat):
        body, count = body[len(beat):], count + 1
    return count, body


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_with_a_key_the_whole_event_reaches_the_agent_before_a_beat_and_the_key_stays_masked(tmp_path, slow):
    """With a credential of the run (the server's key here; a git login's token as much) the proxy scrubs every
    answer and keeps its last bytes back until the next piece, so that a key a server echoes never passes in two
    halves. Before a beat those bytes go out: the agent has the whole event, then the beats. A key in the answer is
    masked as ever, also one that comes in two pieces with a pause between them (inside an event: no beat there)."""
    first = EVENTS["chat"][0]
    echo = b'data: {"id": "c", "choices": [{"index": 0, "delta": {"content": "the key is ' + KEY.encode() + b'"}}]}\n\n'
    cut = echo.index(KEY.encode()) + 8
    Slow.plan = [(0.6, first), (0.6, echo[:cut]), (0.6, echo[cut:]), (0, b"data: [DONE]\n\n")]
    relay = relay_with(tmp_path, slow, 0.25)
    try:
        relay.server.config["llm"][0]["key"] = KEY
        _, body = live.dechunk(relay.post(PATHS["chat"], request("chat")))
        masked = echo.replace(KEY.encode(), b"*" * len(KEY))
        assert KEY.encode() not in body and body.replace(COMMENT, b"") == first + masked + b"data: [DONE]\n\n"
        before, rest = leading(body)
        assert before >= 2 and rest.startswith(first + COMMENT), "the whole first event, then the beats"
        between, rest = leading(rest[len(first):])
        assert between >= 2 and rest == masked + b"data: [DONE]\n\n", "no beat inside the event that came in two parts"
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_a_secret_with_a_line_break_in_it_means_no_beats(tmp_path, slow):
    """Such a secret could reach over the end of an event: what the scrubber keeps back stays back, and so no beat
    is sent (it would land inside the event as the agent has it)."""
    Slow.wait = 0.8
    relay = relay_with(tmp_path, slow, 0.2)
    try:
        relay.server.config["git_tokens"] = [{"host": "git.example", "port": 443, "user": "u", "loopback": False,
                                              "token": "two\nlines-0123456789"}]
        _, body = live.dechunk(relay.post(PATHS["chat"], request("chat")))
        assert body == b"".join(EVENTS["chat"])
    finally:
        relay.close()
