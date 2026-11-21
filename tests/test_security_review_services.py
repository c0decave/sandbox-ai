"""Security review SAI-12 and SAI-22: the supervisor's services the sandbox reaches hold a bounded number of
connections, and the hook server knows who asks.

- The connection proxy serves PROXY_CONNECTIONS at once and resets one more in its accepting thread.
- The hook server answers HOOK_AT_ONCE messages at once and refuses one more unread; its socket is the agent's user's
  alone; a peer of another user (or, in the sandbox, none of its processes) is refused unread and said as a
  safeguard, never recorded as a tool call; the process that asked is kept in the record and shown in the question.

Real sockets and threads; the real ProxyServer and HookServer."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime", "sandbox_runtime_review_services_test")


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


# --- the connection proxy --------------------------------------------------------------------------------------------

def echo_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve(connection):
        with connection:
            while True:
                try:
                    data = connection.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                connection.sendall(b"echo:" + data)

    def accept():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(connection,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return server


class Proxy:
    """The real ProxyServer: the firewall's redirect replaced by a fixed destination, its connections going to an
    echo server."""

    def __init__(self, tmp_path):
        self.echo = echo_server()
        self.log = tmp_path / "connections.jsonl"
        self.fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        target = self.echo.getsockname()
        self.server = rt.ProxyServer({"allow": {"nets": ["192.168.1.0/24"], "loopback": []}}, self.fd,
                                     run_dir=tmp_path, destination=lambda sock: ("192.168.1.20", 5000),
                                     connect=lambda address, timeout=None: socket.create_connection(target, timeout))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def talk(connection, data):
    connection.sendall(data)
    return connection.recv(4096)


def refused(proxy):
    """The proxy reset (or closed) one more connection instead of serving it (the reset may come so soon that
    connect() itself sees it)."""
    try:
        with proxy.connect() as connection:
            connection.sendall(b"x\n")
            return connection.recv(4096) == b""
    except (ConnectionResetError, BrokenPipeError):
        return True
    except socket.timeout:
        return False


def test_the_proxy_serves_a_bounded_number_of_connections_and_resets_one_more(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROXY_CONNECTIONS", 4)
    proxy = Proxy(tmp_path)
    held = [proxy.connect() for _ in range(4)]
    try:
        assert all(talk(connection, b"hi\n") == b"echo:hi\n" for connection in held)
        threads = threading.active_count()
        assert all(refused(proxy) for _ in range(20))
        # Refused in the accepting thread: no thread was started for any of them, and the log names them once.
        assert threading.active_count() <= threads
        assert wait_for(lambda: any("connections through the proxy at once" in r.get("reason", "")
                                    for r in proxy.records()))
        said = [r for r in proxy.records() if "connections through the proxy at once" in r.get("reason", "")]
        assert len(said) == 1 and said[0]["event"] == "denied" and "more than 4 connections" in said[0]["reason"]
        # Those served go on, and a place is free again once one of them ended.
        assert talk(held[0], b"again\n") == b"echo:again\n"
        held.pop().close()
        assert wait_for(lambda: served_now(proxy))
    finally:
        for connection in held:
            connection.close()
        proxy.listener.close()
        proxy.echo.close()


def served_now(proxy):
    try:
        with proxy.connect() as connection:
            return talk(connection, b"new\n") == b"echo:new\n"
    except OSError:
        return False


def test_the_proxys_refusals_are_said_at_most_every_few_seconds_with_their_count(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PROXY_CONNECTIONS", 1)
    server = rt.ProxyServer({"allow": {}}, os.open(os.devnull, os.O_WRONLY), run_dir=tmp_path)
    logged = []
    monkeypatch.setattr(server, "log", logged.append)
    now = [100.0]
    pairs = [socket.socketpair() for _ in range(7)]
    for index, (ours, theirs) in enumerate(pairs):
        if index == 5:
            now[0] += rt.PROXY_REFUSED_SECONDS + 1
        server.refuse_many(ours, clock=lambda: now[0])
        assert theirs.recv(1) == b""  # closed
        theirs.close()
    assert [record["connections"] for record in logged] == [1, 5]
    assert server.refused_many == 1  # (the seventh: said with the next ones)


def test_the_proxys_limit_is_far_above_what_a_run_holds():
    assert rt.PROXY_CONNECTIONS == 512 and rt.HOOK_AT_ONCE == 64


# --- the hook server -------------------------------------------------------------------------------------------------

RULES = {"version": 1, "tool": [{"id": "ask-bash", "tool": "Bash", "decision": "ask", "reason": "shell"},
                                {"id": "allow-read", "tool": "Read", "decision": "allow"}]}


class Broker:
    """The run's broker as the hook server asks it: it keeps the questions and answers ANSWER."""

    def __init__(self, answer="allow"):
        self.asked, self.answer = [], answer

    def ask(self, kind, details):
        self.asked.append((kind, details))
        return {"decision": self.answer, "scope": "once", "reason": "answered"}


def hook_server(tmp_path, broker=None):
    path, stop = tmp_path / "hooks" / "decide.sock", threading.Event()
    path.parent.mkdir()
    server = rt.HookServer(rt.Ruleset([{"name": "r", **RULES}]), broker, None, tmp_path / "tools.jsonl")
    threading.Thread(target=server.serve, args=(path, stop), daemon=True).start()
    assert server.ready.wait(5)
    return server, path, stop


def ask(path, message, timeout=20):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        if message is not None:
            client.sendall((json.dumps(message) + "\n").encode())
        return json.loads(client.makefile("rb").readline() or b"null")


def call(tool="Bash", **arguments):
    return {"agent": "claude", "event": "PreToolUse", "tool": tool, "input": arguments, "cwd": "/work",
            "session_id": "s", "tool_use_id": "t"}


def records(tmp_path):
    path = tmp_path / "tools.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_the_hook_socket_is_the_agents_users_alone(tmp_path):
    server, path, stop = hook_server(tmp_path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        stop.set()


def test_the_hooks_record_and_question_name_the_process_that_asked(tmp_path):
    """Who sent a hook message, as the kernel says: in tools.jsonl and in the question the human answers (the host
    shows a question's "process")."""
    broker = Broker()
    server, path, stop = hook_server(tmp_path, broker)
    try:
        assert ask(path, call(command="ls"))["decision"] == "allow"
        assert ask(path, call("Read", file_path="/work/a"))["decision"] == "allow"
    finally:
        stop.set()
    (kind, details), = broker.asked
    assert kind == "tool" and details["process"]["pid"] == os.getpid()
    assert details["process"]["uid"] == os.getuid() and details["process"]["comm"]
    for record in records(tmp_path):
        assert record["process"]["pid"] == os.getpid(), record


@pytest.mark.parametrize("peer,sandboxed,why", [
    (lambda: (4242, os.getuid() + 1, 0), False, "is not the agent's"),      # another user of the sandbox
    (lambda: (0, os.getuid(), 0), True, "none of this sandbox"),             # a process the sandbox does not show
    (lambda: (_ for _ in ()).throw(OSError(95, "no credentials")), False, "cannot be told"),
])
def test_a_hook_message_of_another_peer_is_refused_unread(tmp_path, monkeypatch, peer, sandboxed, why):
    """SAI-22: before anything of the message is parsed. No rule is asked, no question is made, nothing is recorded
    as a tool call; it is said as a safeguard, where the agent cannot write."""
    broker, events = Broker(), []
    server, path, stop = hook_server(tmp_path, broker)
    server.sandboxed = sandboxed
    monkeypatch.setattr(rt.Attribution, "peer", staticmethod(lambda connection: peer()))
    monkeypatch.setattr(rt, "safeguard_event", lambda kind, what, why_, **fields: events.append((kind, what, why_)))
    decided = []
    monkeypatch.setattr(server, "decide", lambda *args: decided.append(args) or {"decision": "allow"})
    try:
        # (even without a message: the refusal does not wait for one)
        for message in (call(command="curl evil"), call("Read", file_path="/x"), None):
            answer = ask(path, message)
            assert answer["decision"] == "deny" and "not a hook of this sandbox" in answer["reason"]
            assert why in answer["reason"]
    finally:
        stop.set()
    assert decided == [] and broker.asked == [] and records(tmp_path) == []
    assert len(events) == 3 and all(kind == "guard" and "hook socket" in what and why in said
                                    for kind, what, said in events)


def test_the_hook_client_refuses_the_call_when_the_server_refuses_the_peer(tmp_path, monkeypatch):
    """Fail closed end to end: the agent's hook (runtime.hook_client) blocks the tool call."""
    import io
    server, path, stop = hook_server(tmp_path, Broker())
    monkeypatch.setattr(rt.Attribution, "peer", staticmethod(lambda connection: (7, os.getuid() + 5, 0)))
    monkeypatch.setattr(rt, "safeguard_event", lambda *a, **k: None)
    said = io.StringIO()
    try:
        message = {"tool_name": "Read", "tool_input": {"file_path": "/work/a"}, "cwd": "/work"}
        status = rt.hook_client("claude", "PreToolUse", stdin=io.BytesIO(json.dumps(message).encode()), stderr=said,
                                socket_path=path)
    finally:
        stop.set()
    assert status == 2 and "not a hook of this sandbox" in said.getvalue()


def test_the_hook_server_answers_a_bounded_number_of_messages_at_once(tmp_path, monkeypatch):
    """SAI-12: connections that send nothing hold a place each (for the read's 10 s); one more is refused at once,
    unread - and a hook refused is a tool call refused."""
    monkeypatch.setattr(rt, "HOOK_AT_ONCE", 3)
    server, path, stop = hook_server(tmp_path, Broker())
    held = []
    try:
        for _ in range(3):
            client = socket.socket(socket.AF_UNIX)
            client.connect(str(path))
            held.append(client)
        assert wait_for(lambda: server.slots._value == 0)
        began = time.monotonic()
        answer = ask(path, call("Read", file_path="/work/a"), timeout=5)
        assert answer == {"decision": "deny", "reason": "more than 3 tool calls are being checked at once"}
        assert time.monotonic() - began < 2 and records(tmp_path) == []
        for client in held:
            client.close()
        held.clear()
        assert wait_for(lambda: server.slots._value == 3)
        assert ask(path, call("Read", file_path="/work/a"))["decision"] == "allow"
    finally:
        for client in held:
            client.close()
        stop.set()


# --- "allow once" is the asking connection's, not the address's (SAI-15) ---------------------------------------------

class OnceProxy:
    """The real ProxyServer with the supervisor's real RuntimeNetwork behind its allow-list; ANSWERS (a list, used up
    one per question) is what the human answers to each connection question."""

    def __init__(self, tmp_path, answers):
        self.echo = echo_server()
        self.allow_path = tmp_path / "allow.json"
        self.network = rt.RuntimeNetwork({}, self.allow_path, lambda rules: None)
        self.network._commit(self.network.state, set())
        self.answers, self.asked, self.timers = list(answers), [], []
        target = self.echo.getsockname()
        self.server = rt.ProxyServer({"allow_file": str(self.allow_path), "allow": {}}, os.open(os.devnull, os.O_WRONLY),
                                     run_dir=tmp_path, destination=lambda sock: ("198.51.100.9", 5000),
                                     connect=lambda address, timeout=None: socket.create_connection(target, timeout),
                                     asker=self.ask)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.server.serve([self.listener])

    def ask(self, kind, details):
        self.asked.append((kind, details["destination"]))
        decision, scope = self.answers.pop(0)
        if decision == "allow" and scope == "once":  # as ApprovalBroker._apply does, before the answer
            self.network.allow_once(details["address"], details["port"],
                                    timer=lambda seconds, function: self.timers.append(function) or FakeTimer())
        elif decision == "allow":
            self.network.change({"op": "allow", "tcp": [[details["address"], details["port"]]]}, ssh=False)
        return {"decision": decision, "scope": scope, "reason": "answered"}

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def close(self):
        self.listener.close()
        self.echo.close()


class FakeTimer:
    daemon = True

    def start(self):
        pass


def served(connection):
    try:
        connection.sendall(b"hi\n")
        return connection.recv(4096).startswith(b"echo:")
    except (ConnectionResetError, BrokenPipeError):
        return False


def test_allow_once_lets_the_asking_connection_through_and_no_other(tmp_path):
    """The human allows a connection once. While the firewall still holds the address open (30 s), a second
    connection to the same address and port - another process's, another name on the same address - does not pass on
    that answer: it is asked for itself, and refused when the human says no."""
    proxy = OnceProxy(tmp_path, [("allow", "once"), ("deny", "once"), ("allow", "once")])
    try:
        with proxy.connect() as first:
            assert served(first)
            allow = json.loads(proxy.allow_path.read_text())
            # The supervisor still holds the target open - and tells it apart from what is allowed for the run.
            assert allow["once"] == [["198.51.100.9", 5000]] and allow["tcp"] == []
            assert not rt.allowed_destination(allow, "198.51.100.9", 5000)
            with proxy.connect() as second:
                assert not served(second)
            with proxy.connect() as third:
                assert served(third)
        assert proxy.asked == [("connect", "198.51.100.9:5000")] * 3
        for expire in proxy.timers:
            expire()
        assert json.loads(proxy.allow_path.read_text())["once"] == []
    finally:
        proxy.close()


def test_allowed_for_the_run_is_allowed_for_every_connection(tmp_path):
    proxy = OnceProxy(tmp_path, [("allow", "run")])
    try:
        for _ in range(3):
            with proxy.connect() as connection:
                assert served(connection)
        assert len(proxy.asked) == 1
    finally:
        proxy.close()


def test_once_allowed_needs_the_connections_own_answer_the_open_target_and_no_cut():
    allow = {"once": [["198.51.100.9", 443], ["2001:db8::1", 8443]], "tcp": []}
    once = {"decision": "allow", "scope": "once"}
    assert rt.once_allowed(once, allow, "198.51.100.9", 443)
    assert rt.once_allowed(once, allow, "2001:db8:0::1", 8443)
    assert not rt.once_allowed(once, allow, "198.51.100.9", 80)        # another port
    assert not rt.once_allowed(once, allow, "198.51.100.10", 443)      # another address
    assert not rt.once_allowed({"decision": "allow", "scope": "run"}, allow, "198.51.100.9", 443)
    assert not rt.once_allowed({"decision": "deny", "scope": "once"}, allow, "198.51.100.9", 443)
    assert not rt.once_allowed(None, allow, "198.51.100.9", 443)
    assert not rt.once_allowed(once, {"tcp": [["198.51.100.9", 443]]}, "198.51.100.9", 443)  # (the supervisor's word)
    assert not rt.once_allowed(once, {**allow, "cut": {"modes": ["all"]}}, "198.51.100.9", 443)  # the kill switch
    assert not rt.once_allowed(once, {"once": [["not an address", 443]]}, "198.51.100.9", 443)


def test_the_firewall_still_opens_what_was_allowed_once(tmp_path):
    """The proxy's own connection to the server passes the firewall: its rule is there for ONCE_SECONDS, as before
    (and for a run without the proxy it is all there is)."""
    rules, timers = [], []
    network = rt.RuntimeNetwork({}, tmp_path / "allow.json", rules.append)
    network.allow_once("198.51.100.9", 443, timer=lambda seconds, function: timers.append((seconds, function))
                       or FakeTimer())
    assert "ip daddr 198.51.100.9 tcp dport 443 accept" in rules[-1] and timers[0][0] == rt.RuntimeNetwork.ONCE_SECONDS
    timers[0][1]()
    assert "198.51.100.9" not in rules[-1]


# --- the agent's display never shares a clipboard (SAI-24) ---------------------------------------------------------

def test_the_agents_display_is_never_started_with_a_shared_selection(tmp_path, monkeypatch):
    """The finding names x11vnc without -nosel for "a non-private GUI workload". The launcher never asks for it: the
    display of --gui and --gui-vnc - where every program of the sandbox is an X client - has no "selection", and
    start_gui gives x11vnc -nosel without it. Only the browser's own display (its own container, no abstract X
    socket) passes the clipboard, for the app's viewer, which shares it only while "Share the clipboard" is ticked."""
    launcher = load("agent_sandbox", "sandbox_launcher_review_services_test")
    for options in (["--gui"], ["--gui", "--gui-vnc", str(tmp_path / "vnc")],
                    ["--gui", "--gui-vnc", str(tmp_path / "vnc"), "--nested-containers"]):
        a = launcher.parse_args(["codex", *options])
        config = launcher.gui_config(a)
        assert config is not None and "selection" not in config and "private" not in config, options
    started = []
    display = tmp_path / "X99"
    monkeypatch.setattr(rt, "x_socket", lambda name: display)

    def popen(command, **kwargs):
        started.append(command)
        display.write_text("")
        return type("Process", (), {"poll": lambda self: None, "pid": None})()
    monkeypatch.setattr(rt.subprocess, "Popen", popen)
    monkeypatch.setattr(rt, "wait_for_unix_listener", lambda *a: None)
    rt.start_gui({"display": ":99", "screen": "1280x800x24", "vnc": True})
    assert "-nosel" in started[1] and started[1][len(rt.DROP_ALL)] == "x11vnc"
    started.clear()
    rt.start_gui({"display": ":99", "screen": "1280x800x24", "vnc": True, "private": True, "selection": True})
    assert "-nosel" not in started[1] and "-nolisten" in started[0] and "local" in started[0]  # the browser's own


# --- sandbox-ai doctor says what the host's kernel holds for --untrusted (SAI-18, SAI-21) ----------------------------

def test_doctor_says_what_landlock_holds_on_this_kernel():
    """The sandbox shares the host's kernel: whether a command can be kept from the agent's own is the host's to
    see, with what to do about it."""
    doctor = load("doctor", "sandbox_doctor_review_services_test")
    full = doctor.check_landlock(lambda: {"abi": 10, "fs": 0, "unix": True, "scope": 3})
    assert full == ("ok", "Landlock (--untrusted)",
                    "ABI 10: files and other processes' memory, signals, Unix sockets (tmux, hooks, MCP relays)", "")
    status, title, detail, fix = doctor.check_landlock(lambda: {"abi": 4, "fs": 0, "unix": False, "scope": 0})
    assert status == "warn" and detail == "ABI 4: files and other processes' memory"
    assert "signals to the agent's processes (Linux 6.12)" in fix and "Unix sockets of its terminal" in fix

    def none():
        raise OSError(38, "this kernel has no Landlock (Function not implemented)")
    status, title, detail, fix = doctor.check_landlock(none)
    assert status == "warn" and "not available" in detail
    assert "--untrusted refuses to run" in fix and "cat /sys/kernel/security/lsm" in fix and "lsm=landlock" in fix
    assert doctor.group_of(title) == "System"
    # In the table of a Linux host, with whatever this kernel has.
    assert doctor.check_landlock()[1] == "Landlock (--untrusted)"
