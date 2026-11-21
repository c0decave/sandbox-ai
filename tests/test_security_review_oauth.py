"""Security review SAI-26: a login's callback is handed to the socket that listened when the human allowed the
login - not to whichever process of the sandbox owns the port when the callback comes.

The supervisor reached the agent's login server by its port alone. A process of the sandbox that listens on the
other loopback address of that port, or takes the port once the agent's server has ended, got the authorization
code. Now the supervisor looks who listens (runtime.oauth_listeners) before it connects: two listening sockets of
different processes - delivered to neither; another socket than the one the host kept at the question
(oauth_relay.pin_listener) - not delivered. Real sockets and processes of this test."""
import base64
import http.server
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


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime", "sandbox_runtime_review_oauth_test")
oauth = load("oauth_relay", "sandbox_oauth_relay_review_test")


def free_port():
    for _ in range(50):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        try:
            with socket.socket(socket.AF_INET6) as six:
                six.bind(("::1", port))
            return port
        except OSError:
            continue
    pytest.skip("no port free on both loopback addresses")


class Login(http.server.ThreadingHTTPServer):
    """The agent's login server: it keeps the paths asked and answers "signed in"."""
    daemon_threads = True

    def __init__(self, port, address="127.0.0.1"):
        self.address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        self.paths = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.paths.append(self.path)
                body = b"signed in"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        super().__init__((address, port), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


SQUATTER = """
import socket, sys
server = socket.socket(socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((sys.argv[1], int(sys.argv[2])))
server.listen(4)
print("listening", flush=True)
while True:
    connection, _ = server.accept()
    data = connection.recv(65536)
    sys.stderr.write("STOLEN " + data.decode("latin-1").splitlines()[0] + "\\n")
    sys.stderr.flush()
    connection.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: 5\\r\\nConnection: close\\r\\n\\r\\nowned")
    connection.close()
"""


def squatter(address, port):
    """Another process of the sandbox that listens on ADDRESS:PORT and keeps what it is sent."""
    process = subprocess.Popen([sys.executable, "-c", SQUATTER, address, str(port)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "listening"
    return process


def stolen(process):
    process.kill()
    return process.communicate(timeout=10)[1]


def request(port, address="localhost", **more):
    return {"op": "oauth-callback", "port": port, "address": address, "method": "GET",
            "target": "/auth/callback?code=SECRET-CODE&state=S", "host": f"localhost:{port}", "headers": {},
            "body": "", **more}


def test_the_callback_reaches_the_one_listener_and_names_it():
    port = free_port()
    login = Login(port)
    try:
        listeners = rt.oauth_listeners(port)
        assert [(entry["address"], entry["pid"]) for entry in listeners] == [("127.0.0.1", os.getpid())]
        assert isinstance(listeners[0]["inode"], int) and listeners[0]["comm"]
        answer = rt.oauth_callback(request(port))
        assert answer["status"] == 200 and base64.b64decode(answer["body"]) == b"signed in"
        assert answer["listener"]["pid"] == os.getpid()
        assert login.paths == ["/auth/callback?code=SECRET-CODE&state=S"]
    finally:
        login.shutdown()
        login.server_close()


def test_a_second_process_on_the_other_loopback_address_gets_nothing_and_neither_does_the_login():
    """The agent listens on 127.0.0.1:PORT; a process of the sandbox binds [::1]:PORT. For a redirect to "localhost"
    the supervisor tried ::1 first: the code went to that process. Now two processes listen - the callback is handed
    to neither, and the human is told."""
    port = free_port()
    login = Login(port)
    other = squatter("::1", port)
    try:
        with pytest.raises(ValueError, match="more than one process listens on the callback's port") as refused:
            rt.oauth_callback(request(port))
        assert f"(pid {other.pid})" in str(refused.value) and f"(pid {os.getpid()})" in str(refused.value)
        assert login.paths == []
    finally:
        said = stolen(other)
        login.shutdown()
        login.server_close()
    assert "STOLEN" not in said and "SECRET-CODE" not in said


def test_a_process_that_takes_the_port_after_the_login_was_allowed_gets_nothing():
    """The host kept the socket that listened when the human allowed the login. The agent's server ends (or is
    ended) and another process takes the port: it is not the socket the human was asked about."""
    port = free_port()
    login = Login(port)
    pinned = rt.oauth_listeners(port)[0]
    login.shutdown()
    login.server_close()
    other = squatter("127.0.0.1", port)
    try:
        with pytest.raises(ValueError, match="another socket listens on the callback's port than when you allowed"):
            rt.oauth_callback(request(port, listener={"inode": pinned["inode"], "comm": pinned["comm"]}))
        # (without what the host kept, a sandbox-ai before 0.1.117: delivered to whoever listens, as it was)
    finally:
        said = stolen(other)
    assert "STOLEN" not in said and "SECRET-CODE" not in said


def test_the_pinned_socket_still_listening_takes_the_callback():
    port = free_port()
    login = Login(port)
    try:
        pinned = rt.oauth_listeners(port)[0]
        answer = rt.oauth_callback(request(port, listener={"inode": pinned["inode"], "comm": pinned["comm"]}))
        assert answer["status"] == 200 and login.paths
    finally:
        login.shutdown()
        login.server_close()


def test_who_listens_is_judged_by_process_and_socket():
    refusal = rt.oauth_listener_refusal
    one = {"address": "127.0.0.1", "inode": 11, "pid": 5, "start": 100, "comm": "codex"}
    both = {"address": "::1", "inode": 12, "pid": 5, "start": 100, "comm": "codex"}  # the same process, both addresses
    unknown = {"address": "::1", "inode": 13, "pid": None}                           # another container of the pod
    assert refusal([]) is None and refusal([one]) is None and refusal([one, both]) is None
    assert refusal([unknown]) is None
    assert "more than one process" in refusal([one, {**both, "pid": 6, "comm": "python3"}])
    assert "more than one process" in refusal([one, unknown]) and "outside the sandbox's own" in refusal([one, unknown])
    assert refusal([one], {"inode": 11}) is None and refusal([one, both], {"inode": 12}) is None
    assert "another socket listens" in refusal([one], {"inode": 99, "comm": "codex"})
    assert refusal([], {"inode": 99}) is None            # nothing listens: the connection itself fails
    assert refusal([one], {"inode": True}) is None and refusal([one], "x") is None  # (no pin of that shape)


def test_the_control_socket_says_who_listens(tmp_path):
    port = free_port()
    login = Login(port)
    path, stop = tmp_path / "control.sock", threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(path, rt.RuntimeNetwork({}, None, lambda s: None), stop),
                              daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

        def ask(message):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(10)
                client.connect(str(path))
                client.sendall((json.dumps(message) + "\n").encode())
                return json.loads(client.makefile().readline())
        answer = ask({"op": "oauth-listener", "port": port})
        assert answer["ok"] is True and [entry["pid"] for entry in answer["listeners"]] == [os.getpid()]
        assert ask({"op": "oauth-listener", "port": free_port()}) == {"ok": True, "listeners": []}
        assert ask({"op": "oauth-listener", "port": "x"})["ok"] is False
    finally:
        stop.set()
        thread.join(5)
        login.shutdown()
        login.server_close()


# --- the host's side: what listened when the login was allowed is kept, once, and sent with the callback -----------

def test_the_host_keeps_the_one_listener_of_the_question(tmp_path):
    (tmp_path / "approvals").mkdir()
    target = {"port": 1455, "url": "http://localhost:1455/auth/callback"}
    asked = []

    def ask(directory, message, timeout=None):
        asked.append(message)
        return {"ok": True, "listeners": [{"address": "127.0.0.1", "inode": 4711, "pid": 12, "comm": "codex",
                                           "exe": "/usr/local/bin/codex"}]}
    note = oauth.pin_listener("1-2-3", target, tmp_path, ask=ask)
    assert asked == [{"op": "oauth-listener", "port": 1455}]
    assert note == "; in the sandbox codex (pid 12) takes it, and only that process's socket"
    kept = json.loads((tmp_path / "approvals" / "1-2-3.listener").read_text())
    assert (kept["inode"], kept["pid"], kept["comm"], kept["port"]) == (4711, 12, "codex", 1455)
    # Kept once: what listens later (asked again for the same question) does not replace it.
    again = oauth.pin_listener("1-2-3", target, tmp_path, ask=lambda *a, **k: {"listeners": [{"inode": 1, "pid": 99,
                                                                                              "comm": "evil"}]})
    assert again == "" and json.loads((tmp_path / "approvals" / "1-2-3.listener").read_text())["inode"] == 4711


@pytest.mark.parametrize("answer", [
    {"ok": True, "listeners": []},                                         # the agent's server does not listen yet
    {"ok": True, "listeners": [{"inode": 1, "pid": 5}, {"inode": 2, "pid": 6}]},   # two: refused at the callback
    {"ok": True, "listeners": [{"inode": "1"}]}, {"ok": True, "listeners": "x"}, {"ok": True}, None, [],
])
def test_nothing_is_kept_when_there_is_not_exactly_one_listener(tmp_path, answer):
    (tmp_path / "approvals").mkdir()
    assert oauth.pin_listener("1-2-3", {"port": 1455}, tmp_path, ask=lambda *a, **k: answer) == ""
    assert list((tmp_path / "approvals").iterdir()) == []


def test_a_supervisor_that_does_not_know_the_question_keeps_the_login_working(tmp_path):
    """A sandbox started by a sandbox-ai before 0.1.117 answers "no such operation": nothing is kept, the login goes
    on as before."""
    (tmp_path / "approvals").mkdir()

    def ask(*args, **kwargs):
        raise ValueError("op: allow or revoke")
    assert oauth.pin_listener("1-2-3", {"port": 1455}, tmp_path, ask=ask) == ""
    assert list((tmp_path / "approvals").iterdir()) == []
