"""One answer of a local model has a ceiling (every model family's profile: max_tokens), and an answer the agent gave
up is cancelled at the server at once.

The local-model research's round 3 (research-llm/round3.md, NOTES 22:40-23:05 UTC): OpenCode's request 13 on
Devstral Small 2 started streaming and never finished; OpenCode asks for max_tokens 32000 (~31 minutes at 17 tokens/s),
Claude Code 32768. The run was ended at 22:50:40; the next run (Codex) got no answer in 15 minutes, as the server kept
generating that answer until the model was unloaded at 23:05. The user's decision (2026-11-17): a max_tokens ceiling
per profile, the user's own value wins; and the proxy must cancel the upstream request at once when the harness
abandons an answer.

LM Studio's side, read in its own server log on the research Mac (read-only, 2026-11-17): it stops a generation when
its client's connection closes - "Client disconnected. Stopping generation... (If the model is busy processing the
prompt, it will finish first.)", 196 times this month - but there is no such line at 23:50:40 (its clock, CET): the
connection to it was never closed. Nothing stood between the agent's close and the server's socket but a proxy that
was waiting for the server, not watching the agent. So the proxy now watches the agent's side while the server works
(before the answer's first byte, while it holds an answer whole, in a silent stream) and closes the server's
connection the moment the agent's goes; no unload is needed.

The ceilings, measured (every traffic record of the research's phase 2 and rounds 1-3, output tokens per answer):
Devstral max 2,605 (8192), Qwen3-Coder-Next max 2,726 (16384), Laguna-S max 10,418 (16384), Qwen3.6 max 8,317 and one
runaway to 31,999 (24576), Nemotron max 2,474, GLM-4.5-Air 642, Seed-OSS 10,819 (its title request) - thinking models
16384 or more, the others 8192-16384. `--llm-param max_tokens=N` (or `llm set --param max_tokens=N`) wins;
max_tokens=0 lifts the ceiling (the agent's own value).

Unit tests of the profiles, llm.param_value / runtime.llm_apply_params for 0, runtime.llm_client_gone and LlmClientWatch,
and end to end through the real proxy (ProxyServer) with a mock model server that never finishes."""
import argparse
import http.server
import json
from pathlib import Path
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
mp = live.load("sandbox_model_profiles_ceiling", "sandbox/model_profiles.py")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


# --- the ceilings of the profiles -------------------------------------------------------------------------------------

def thinks(family):
    return not str(family["all"].get("reasoning") or "none").startswith("none")


def test_every_model_family_has_a_ceiling():
    table = mp.load(None, llm.profile_param, llm.loop_setting)
    for family in table["families"]:
        ceiling = family["all"]["params"].get("max_tokens")
        assert isinstance(ceiling, int), f"{family['id']}: no max_tokens"
        assert 8192 <= ceiling <= 32768, family["id"]
        if thinks(family):
            assert ceiling >= 16384, f"{family['id']} thinks: room for its reasoning and the answer"
        for agent, pair in family.get("harnesses", {}).items():
            assert pair.get("params", {}).get("max_tokens", ceiling) <= 32768, (family["id"], agent)


def test_the_measured_ceilings():
    table = mp.load(None, llm.profile_param, llm.loop_setting)
    for model, ceiling in (("devstral-small-2-24b-instruct-2512", 8192), ("qwen/qwen3-coder-next", 16384),
                           ("poolside/laguna-s-2.1", 16384), ("qwen3.6-35b-a3b-mlx", 24576),
                           ("seed-oss-36b-instruct-mlx", 16384), ("glm-4.7-flash", 16384)):
        assert mp.match(model, "opencode", table)["params"]["max_tokens"] == ceiling, model


def test_zero_lifts_the_ceiling_and_is_the_users_value():
    assert llm.param_value("lmstudio", "max_tokens", "0") == 0
    assert llm.param_value("lmstudio", "max_tokens", "16") == 16
    with pytest.raises(ValueError):
        llm.param_value("lmstudio", "max_tokens", "15")
    entry = live.entry("lmstudio", presets={"m": {"max_tokens": 8192}})
    document = {"model": "m", "max_tokens": 32000}
    assert rt.llm_apply_params(document, entry, "chat") == {"max_tokens": 8192}
    lifted = live.entry("lmstudio", presets={"m": {"max_tokens": 8192}}, params={"*": {"max_tokens": 0}})
    for document in ({"model": "m", "max_tokens": 32000}, {"model": "m"}):
        before = dict(document)
        assert "max_tokens" not in rt.llm_apply_params(document, lifted, "chat")
        assert document == before, "the agent's own value (or none) stays"
    assert rt.llm_apply_params({"model": "m"}, live.entry("lmstudio", override={"max_tokens": 0}), "responses") == {}


def test_zero_tells_the_agents_no_ceiling(tmp_path):
    llm.main(["add", "mac", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234", "--model",
              "devstral-small-2-24b-instruct-2512"], out=lambda line: None)
    found = llm.endpoint("mac")
    found["llm_profile"] = {"agent": "opencode", "model": "devstral-small-2-24b-instruct-2512"}
    model = "devstral-small-2-24b-instruct-2512"
    assert llm.effective_params(found, model)[0]["max_tokens"] == 8192
    assert llm.effective_params(found, model, {"max_tokens": 0})[0]["max_tokens"] == 0
    config = llm.opencode_config(found, model, contexts={model: 131072}, outputs={model: 0})
    assert config["provider"][llm.provider_id(found)]["models"][model]["limit"]["output"] == \
        llm.output_limit(131072), "0: OpenCode's output limit as without a ceiling"


# --- the agent that went away -----------------------------------------------------------------------------------------

def test_a_closed_agent_is_seen_and_an_open_one_not():
    ours, theirs = socket.socketpair()
    try:
        assert rt.llm_client_gone(ours) is False
        theirs.sendall(b"GET /next HTTP/1.1\r\n")  # the agent's next request (pipelined): still there
        assert rt.llm_client_gone(ours) is False
        theirs.close()
        assert rt.llm_client_gone(ours) is True
    finally:
        ours.close()
    closed = socket.socket()
    closed.close()
    assert rt.llm_client_gone(closed) is True, "a socket that is gone"


def test_the_watch_closes_the_servers_connection_when_the_agent_goes():
    agent, ours = socket.socketpair()
    upstream, server = socket.socketpair()
    watch = rt.LlmClientWatch(ours, upstream, every=0.05)
    try:
        time.sleep(0.2)
        assert not watch.fired
        agent.close()
        server.settimeout(3)
        assert server.recv(10) == b"", "the server sees its connection end"
        assert watch.fired and watch.seconds() >= 0
    finally:
        watch.stop()
        for sock in (ours, upstream, server):
            sock.close()


def test_a_stopped_watch_does_nothing():
    agent, ours = socket.socketpair()
    upstream, server = socket.socketpair()
    watch = rt.LlmClientWatch(ours, upstream, every=0.05)
    watch.stop()
    agent.close()
    time.sleep(0.3)
    server.setblocking(False)
    with pytest.raises(BlockingIOError):
        server.recv(10)  # still open: nothing sent, nothing closed
    assert not watch.fired
    for sock in (ours, upstream, server):
        sock.close()


# --- end to end: the real proxy and a server that never finishes ------------------------------------------------------

class Endless(http.server.BaseHTTPRequestHandler):
    """A model that thinks for ever: MODE "silent" sends nothing at all, "stream" its headers and one event, then
    nothing; each request's connection is watched and its end noted (CLOSED: seconds after the request came)."""
    protocol_version = "HTTP/1.1"
    mode, closed, bodies = "silent", [], []

    def log_message(self, *args):
        pass

    def do_POST(self):
        started = time.monotonic()
        Endless.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))
        if Endless.mode == "stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            event = b'data: {"choices":[{"index":0,"delta":{"content":"Let me think"}}]}\n\n'
            self.wfile.write(b"%x\r\n" % len(event) + event + b"\r\n")
            self.wfile.flush()
        self.connection.settimeout(15)
        try:
            data = self.connection.recv(10)  # the generation goes on until the proxy closes the connection
        except OSError:
            data = b""
        Endless.closed.append((data, time.monotonic() - started))
        self.close_connection = True


@pytest.fixture
def endless():
    Endless.mode, Endless.closed, Endless.bodies = "silent", [], []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Endless)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def abandon(relay, document, after):
    """The agent sends DOCUMENT and gives up after AFTER seconds (its own timeout, the human's cancel)."""
    body = json.dumps(document).encode()
    sock = socket.create_connection(("127.0.0.1", relay.port), timeout=10)
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                 b"Content-Length: %d\r\n\r\n" % len(body) + body)
    time.sleep(after)
    sock.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("mode", ["silent", "stream"])
def test_e2e_an_abandoned_answer_is_cancelled_at_the_server_at_once(tmp_path, endless, mode):
    Endless.mode = mode
    relay = live.Relay(tmp_path, endless.server_address[1])
    try:
        abandon(relay, {"model": "m", "stream": True, "max_tokens": 32000,
                        "messages": [{"role": "user", "content": "hi"}]}, 0.5)
        deadline = time.monotonic() + 5
        while not Endless.closed and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Endless.closed, "the server's connection stayed open (it would generate for 31 minutes)"
        data, seconds = Endless.closed[0]
        assert data == b"" and seconds < 3, "closed by the proxy within moments of the agent's close"
        record = relay.records(1)[-1]
        assert "cancelled" in record["llm_cancelled"] and record["llm"] == "mac"
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_e2e_the_ceiling_reaches_the_server(tmp_path, scripted):
    relay = live.Relay(tmp_path, scripted.server_address[1])
    relay.server.config["llm"][0]["presets"] = {"m": {"max_tokens": 8192}}
    try:
        relay.post(live.PATHS["chat"], {"model": "m", "max_tokens": 32000, "messages": [{"role": "user",
                                                                                         "content": "hi"}]})
        relay.post(live.PATHS["messages"], {"model": "m", "max_tokens": 32768, "messages": [{"role": "user",
                                                                                            "content": "hi"}]})
        relay.post(live.PATHS["responses"], {"model": "m", "input": "hi"})
        sent = live.Scripted.bodies
        assert sent[0]["max_tokens"] == 8192 and sent[1]["max_tokens"] == 8192
        assert sent[2]["max_output_tokens"] == 8192, "asked for nothing: the ceiling all the same"
    finally:
        relay.close()


def test_a_start_with_the_profile_tells_the_proxy_the_ceiling(tmp_path):
    llm.main(["add", "mac", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234", "--model",
              "devstral-small-2-24b-instruct-2512"], out=lambda line: None)
    a = argparse.Namespace(llm="mac", agent="opencode", llm_param=[], llm_addresses=["127.0.0.1"],
                           no_llm_profile=False, dry_run=True)
    found, model = llm.selection(a)
    entry = llm.proxy_params(a, found, model)
    assert entry["presets"][model]["max_tokens"] == 8192
    a.llm_param = ["max_tokens=0"]
    assert llm.proxy_params(a, found, model)["override"] == {"max_tokens": 0}


scripted = live.scripted
