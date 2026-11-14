"""Stability round 3 (0.1.95): servers that crash, full disks, signals and races - each test failed before its fix."""
import contextlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from test_proxy import Handler, Proxy, https_request, pki, rt, serve  # noqa: F401 - pki is a fixture

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(f"stability3_{name}", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def full_disk(error=errno.ENOSPC):
    return OSError(error, os.strerror(error))


# --- an MCP server that ends while a call is in flight --------------------------------------------------------

CRASHING_SERVER = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "tools/call":
        sys.exit(3)  # crashes on the call, without an answer
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
"""


@pytest.fixture
def crashing_relay():
    directory = Path(tempfile.mkdtemp(prefix="s3-", dir="/tmp"))  # a short socket path
    sock, out = directory / "s.sock", io.StringIO()
    threading.Thread(target=rt.mcp_serve, args=(str(sock), [sys.executable, "-c", CRASHING_SERVER]),
                     kwargs={"cwd": "/", "out": out}, daemon=True).start()
    for _ in range(250):
        if sock.exists():
            break
        time.sleep(0.02)
    yield sock, out
    shutil.rmtree(directory, ignore_errors=True)


def send(client, message):
    client.sendall((json.dumps(message) + "\n").encode())


def test_a_call_in_flight_when_the_mcp_server_crashes_is_answered_and_the_connection_ends(crashing_relay):
    """The server instance of a connection crashed on a tool call: the relay kept reading the agent's lines, so the
    call waited for an answer that could not come (until the agent's own timeout, minutes), and only the agent's
    next message ended the connection. Now the open requests are answered with an error and the connection ends,
    so the agent sees the server gone at once (and may start it again)."""
    sock, out = crashing_relay
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(str(sock))
        send(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        send(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read", "arguments": {}}})
        data = b""
        try:
            while chunk := client.recv(65536):
                data += chunk
        except socket.timeout:
            pytest.fail(f"no end of the connection within 10 s after the server crashed; got {data!r}")
    replies = {reply["id"]: reply for reply in map(json.loads, data.splitlines())}
    assert replies[1]["result"] == {}
    assert "ended before it answered" in replies[2]["error"]["message"]
    results = [json.loads(line.split(" ", 1)[1]) for line in out.getvalue().splitlines()
               if line.startswith("sandbox-ai-mcp-call ")]
    assert any(r.get("event") == "result" and r.get("id") == 2 and r["is_error"] and r.get("server_ended")
               for r in results), results


def test_a_new_connection_after_the_crash_gets_a_new_server_instance(crashing_relay):
    """A server that crashes on each call, called again and again (a restart storm): each connection is answered and
    ended, none is left waiting, and the relay goes on taking connections."""
    sock, _ = crashing_relay
    for attempt in range(20):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(sock))
            send(client, {"jsonrpc": "2.0", "id": attempt, "method": "tools/call", "params": {"name": "x"}})
            data = b""
            while chunk := client.recv(65536):
                data += chunk
        assert json.loads(data)["error"]["code"] == -32000


# --- the MCP server's log reader on a full disk ------------------------------------------------------------------

class FullFile:
    """A log file of split_log whose first FAILS writes fail as on a full disk."""

    def __init__(self, raw, fails):
        self.raw, self.fails = raw, fails

    def write(self, data):
        if self.fails:
            self.fails -= 1
            raise full_disk()
        return self.raw.write(data)

    def flush(self):
        self.raw.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.raw.close()


def test_the_mcp_servers_log_reader_goes_on_after_a_full_disk(tmp_path, monkeypatch):
    """The reader of an MCP server's output (mcp.py --internal-log) ended at the first line it could not write
    (a full disk): the server's supervisor then found its output closed, and its relay failed every call from then
    on, also once there was room again. Now that line is lost, counted, and said in the file later."""
    m = load("mcp")
    real_open = open
    monkeypatch.setattr(m, "open", lambda *a, **k: FullFile(real_open(*a, **k), 1), raising=False)
    (tmp_path / "approvals").mkdir()
    call = {"event": "call", "id": 1, "tool": "read", "decision": "allow"}
    lines = [b"server: first line\n", b"sandbox-ai-mcp-call " + json.dumps(call).encode() + b"\n",
             b"server: second line\n", b"sandbox-ai-mcp-call " + json.dumps({**call, "id": 2}).encode() + b"\n"]
    m.split_log(tmp_path, io.BytesIO(b"".join(lines)))
    log = (tmp_path / "supervisor.log").read_text()
    assert "1 lines of this log were not written" in log and log.endswith("server: second line\n"), log
    calls = [json.loads(line) for line in (tmp_path / "mcp-calls.jsonl").read_text().splitlines()]
    assert calls[0]["event"] == "lost" and calls[0]["lines"] == 1 and calls[1]["id"] == 2, calls
    assert not list(tmp_path.glob("approvals/*"))


def test_an_unwritable_question_of_the_mcp_server_does_not_end_its_log_reader(tmp_path, monkeypatch):
    m = load("mcp")
    (tmp_path / "approvals").mkdir()

    def refuse(path, data):
        raise full_disk(errno.EDQUOT)
    monkeypatch.setattr(m, "write_private", refuse)
    ask = {"id": "1-2-3", "kind": "mcp", "tool": "read"}
    m.split_log(tmp_path, io.BytesIO(b"sandbox-ai-mcp-ask " + json.dumps(ask).encode() + b"\nafter\n"))
    assert (tmp_path / "supervisor.log").read_text() == "after\n"


# --- the connection proxy with its log on a full disk -----------------------------------------------------------

@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full (macOS)")
def test_the_proxy_goes_on_while_its_log_cannot_be_written(tmp_path, pki):  # noqa: F811
    """The run's connection log on a full disk: each connection's thread ended with the log's OSError (a traceback
    in the supervisor's log) instead of closing the agent's side - the answer came, its end did not (a kept-alive
    connection hung). Now the proxy goes on, and the first record written again says how many were lost."""
    upstream = serve(Handler, pki["server"])
    proxy = Proxy(tmp_path, pki, upstream.server_address[1])
    full = os.open("/dev/full", os.O_WRONLY)
    proxy.server.log_fd = full
    request = b"GET /x HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n"
    errors = []
    previous = threading.excepthook
    threading.excepthook = lambda args: errors.append(args.exc_value)
    try:
        for _ in range(2):
            done = []
            worker = threading.Thread(target=lambda: done.append(https_request(proxy.port, pki["ca"], request)),
                                      daemon=True)
            worker.start()
            worker.join(15)
            assert done and done[0].startswith(b"HTTP/1.1 200"), "the connection did not end"
        assert not errors, errors
        proxy.server.log_fd = proxy.fd
        assert https_request(proxy.port, pki["ca"], request).startswith(b"HTTP/1.1 200")
        records = proxy.records(until=lambda r: r.get("event") == "http")
        assert records[0]["event"] == "log-gap" and records[0]["lost"] >= 2, records
        assert records[1]["event"] == "http" and records[1]["url"] == "https://example.test/x"
    finally:
        threading.excepthook = previous
        os.close(full)
        proxy.close()
        upstream.shutdown()


# --- the launcher's terminal closed, its session log on a full disk ----------------------------------------------

def hung_up_terminal():
    """A text stream on a terminal whose other end is gone (the window closed): writes fail with EIO."""
    master, slave = os.openpty()
    os.close(master)
    return open(slave, "w", buffering=1, closefd=True)


@pytest.mark.skipif(sys.platform != "linux", reason="EIO after a pty's master closed is Linux's behaviour")
def test_the_launchers_clean_up_runs_on_after_its_terminal_closed(monkeypatch):
    """SIGHUP (the terminal closed) ends a run with SystemExit, and its clean-up says what it does on that very
    terminal: the first line raised EIO, and the steps after it were skipped (the full chain is test_steady_output
    in the contracts)."""
    s = load("agent_sandbox")
    steps = []

    def launch(argv):
        try:
            raise SystemExit(128 + 1)  # terminate() on SIGHUP
        finally:
            print("agent-sandbox: memory note", file=sys.stderr)
            steps.append("history")
    monkeypatch.setattr(s, "launch", launch)
    terminal = hung_up_terminal()
    with pytest.raises(OSError):
        terminal.write("x\n")
    monkeypatch.setattr(sys, "stderr", terminal)
    with pytest.raises(SystemExit) as ended:
        s.main([])
    assert ended.value.code == 129 and steps == ["history"]
    with contextlib.suppress(OSError):
        terminal.close()


# --- signals to the CLI's own long-running processes ---------------------------------------------------------------

def environment(tmp_path):
    for folder in ("home", "config", "state", "runtime", "data"):
        (tmp_path / folder).mkdir(mode=0o700, exist_ok=True)
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "XDG_DATA_HOME": str(tmp_path / "data"),
            "SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_NO_UPDATE_CHECK": "1", "LANG": "C.UTF-8"}


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_the_web_interface_ended_by_a_signal_cleans_up_and_starts_again(tmp_path, monkeypatch, signum):
    """`sandbox-ai web serve` ended by SIGTERM (systemd, kill) or SIGHUP (its terminal closed) died at once: its
    listen.json stayed (the launcher refused --allow-host-port for that port whenever another process had its PID).
    Now it ends through its clean-up, and a second one starts on the same files."""
    env = environment(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    web = load("webui")
    web.save_users({"tester": {"password": web.hash_password("pw-for-the-test-123456"),
                               "totp": web.new_totp_secret(), "totp_counter": 0}})
    listen = tmp_path / "config" / "sandbox-ai" / "web" / "listen.json"
    for attempt in range(2):
        process = subprocess.Popen([sys.executable, str(ROOT / "sandbox" / "main.py"), "web", "serve", "--listen",
                                    "127.0.0.1:0"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(300):
                if listen.exists() or process.poll() is not None:
                    break
                time.sleep(0.05)
            assert listen.exists(), process.communicate(timeout=10)[0]
            assert json.loads(listen.read_text())["pid"] == process.pid
            process.send_signal(signum)
            output = process.communicate(timeout=30)[0]
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        assert process.returncode == 128 + signum and "Traceback" not in output, output
        assert not listen.exists(), f"attempt {attempt}: listen.json left behind"


# --- out of descriptors at an accept() --------------------------------------------------------------------------

class Flaky:
    """A listener whose first FAILS accept() calls fail with EMFILE (the process is out of descriptors), as a busy
    supervisor's do; everything else is the real socket's."""

    def __init__(self, server, fails=3):
        self.server, self.fails = server, fails

    def accept(self):
        if self.fails:
            self.fails -= 1
            raise full_disk(errno.EMFILE)
        return self.server.accept()

    def __getattr__(self, name):
        return getattr(self.server, name)


@pytest.fixture
def flaky_listeners(monkeypatch):
    real = rt.listen_unix
    monkeypatch.setattr(rt, "listen_unix", lambda path: Flaky(real(path)))


real_sleep = time.sleep


def wait_for(path):
    for _ in range(250):
        if Path(path).exists():
            return
        real_sleep(0.02)


def test_the_runs_control_socket_serves_on_after_running_out_of_descriptors(tmp_path, flaky_listeners):
    """The supervisor out of descriptors for a moment (an agent's many connections through its relays): the
    control socket's accept() raised EMFILE, and its loop ended for good - sandbox-ai net allow, approvals answered
    live and workspace add reached nothing for the rest of the run."""
    directory = Path(tempfile.mkdtemp(prefix="s3-", dir="/tmp"))
    path, stop = directory / "c.sock", threading.Event()
    network = rt.RuntimeNetwork({}, None, lambda script: None)
    thread = threading.Thread(target=rt.control_server, args=(path, network, stop), daemon=True)
    thread.start()
    wait_for(path)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(path))
            client.sendall(json.dumps({"op": "show"}).encode() + b"\n")
            answer = client.makefile().readline()
        assert answer and "ok" in json.loads(answer), answer
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)


def test_the_hook_server_serves_on_after_running_out_of_descriptors(tmp_path, flaky_listeners):
    """As the control socket: the agent's hooks reached no server after one EMFILE."""
    directory = Path(tempfile.mkdtemp(prefix="s3-", dir="/tmp"))
    path, stop = directory / "h.sock", threading.Event()
    server = rt.HookServer(rt.Ruleset([]), None, rt.Taint())
    thread = threading.Thread(target=server.serve, args=(path, stop), daemon=True)
    thread.start()
    wait_for(path)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(path))
            client.sendall(json.dumps({"agent": "claude", "event": "PreToolUse", "tool": "Bash",
                                       "input": {"command": "ls"}}).encode() + b"\n")
            client.shutdown(socket.SHUT_WR)
            answer = client.makefile().readline()
        assert answer, "no answer from the hook server"
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)


def test_the_mcp_relay_serves_on_after_running_out_of_descriptors(flaky_listeners):
    """An MCP server sandbox's relay (mcp_serve) out of descriptors for a moment raised, and the server ended."""
    directory = Path(tempfile.mkdtemp(prefix="s3-", dir="/tmp"))
    sock = directory / "s.sock"
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": 1}}), flush=True)
"""
    thread = threading.Thread(target=rt.mcp_serve, args=(str(sock), [sys.executable, "-c", server]),
                              kwargs={"cwd": "/", "out": io.StringIO()}, daemon=True)
    thread.start()
    wait_for(sock)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(sock))
            send(client, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
            client.shutdown(socket.SHUT_WR)
            assert json.loads(client.makefile().readline())["result"] == {"ok": 1}
        assert thread.is_alive()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# --- a question file nested too deep ----------------------------------------------------------------------------

DEEP = "[" * 100000 + "]" * 100000  # nested deeper than Python's json reads: RecursionError, no ValueError


def a_run_with_questions(tmp_path, env):
    """A run of this process (alive) with a question damaged into thousands of brackets and a good one."""
    run = Path(env["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs" / "claude-deep"
    (run / "approvals").mkdir(parents=True)
    liveness = load("liveness")
    (run / "run.json").write_text(json.dumps({
        "run": "claude-deep", "agent": "claude", "backend": "podman", "workspace": str(tmp_path), "pid": os.getpid(),
        "pid_start": load("agent_sandbox").process_start(os.getpid()), "pid_ns": liveness.pid_namespace(),
        "name": "deep", "kind": "agent"}))
    (run / "approvals" / "1-2-3.json").write_text(DEEP)
    (run / "approvals" / "1-2-4.json").write_text(json.dumps({"id": "1-2-4", "kind": "dns", "name": "example.org",
                                                               "time": 1}))
    return run


@pytest.mark.parametrize("args", [("approvals", "list"), ("approvals", "list", "--json"), ("approvals", "history"),
                                  ("approvals", "deny", "1-2-4")])
def test_a_question_file_nested_too_deep_is_skipped(tmp_path, args):
    """One damaged question file (thousands of brackets) ended approvals list, history and every answer with a
    RecursionError traceback - none of the run's other questions could be listed or answered."""
    env = environment(tmp_path)
    run = a_run_with_questions(tmp_path, env)
    done = subprocess.run([sys.executable, str(ROOT / "sandbox" / "main.py"), *args], env=env, capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert "Traceback" not in done.stdout + done.stderr, done.stderr[-1500:]
    if args[1] == "deny":  # (an allowed name is looked up: no network here)
        assert (run / "approvals" / "1-2-4.answer").exists(), done.stderr[-1500:]
    else:
        assert done.returncode == 0 and "example.org" in done.stdout, done.stdout + done.stderr


def test_the_launchers_question_watcher_survives_a_question_nested_too_deep(tmp_path):
    """The launcher's watcher of its run's questions (desktop notifications; --ask allow answers them) ended at a
    damaged question file: no later question was announced, and with --ask allow none was answered."""
    approvals = load("approvals")
    approvals.watched = lambda now=None: False
    directory = tmp_path / "approvals"
    directory.mkdir()
    (tmp_path / "run.json").write_text(json.dumps({"run": "claude-x", "name": "x", "agent": "claude"}))
    (directory / "1-1-1.json").write_text(DEEP)
    told, stop = [], threading.Event()
    thread = threading.Thread(target=approvals.watch, args=(directory, stop), daemon=True,
                              kwargs={"notify": lambda title, text: told.append(text), "interval": 0.05})
    thread.start()
    try:
        time.sleep(0.3)
        (directory / "2-2-2.json").write_text(json.dumps({"id": "2-2-2", "kind": "dns", "name": "example.org"}))
        for _ in range(100):
            if told:
                break
            time.sleep(0.05)
        assert thread.is_alive() and any("example.org" in text for text in told), told
    finally:
        stop.set()
        thread.join(5)


def test_every_json_read_that_tolerates_damage_tolerates_deep_nesting_too():
    """A guard: wherever a json.load(s) is caught as damaged (ValueError), a document nested deeper than Python reads
    (RecursionError, no ValueError) is caught as well - in sandbox-ai and in the app."""
    import ast
    found = []
    folders = (ROOT / "sandbox", ROOT / "projects" / "sandbox-ai-app" / "sandbox_ai_app")
    for path in sorted(p for folder in folders for p in folder.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Try) or not any(
                    isinstance(n, ast.Call) and getattr(n.func, "attr", None) in ("loads", "load")
                    and getattr(getattr(n.func, "value", None), "id", None) == "json"
                    for stmt in node.body for n in ast.walk(stmt)):
                continue
            names = set()
            for handler in node.handlers:
                kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                names |= {"*" if kind is None else getattr(kind, "id", getattr(kind, "attr", "?")) for kind in kinds}
            if names & {"ValueError", "JSONDecodeError"} and not names & {"RecursionError", "Exception", "*",
                                                                          "BaseException"}:
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    allowed = {"projects/sandbox-ai-app/sandbox_ai_app/files.py"}  # (the file manager's review keeps its own)
    assert [site for site in found if site.split(":")[0] not in allowed] == []


def test_a_long_socket_path_listens_also_where_there_is_no_proc(tmp_path, monkeypatch):
    """macOS has no /proc/self/fd: a socket in a directory whose path is too long for bind() is reached through a
    short link to that directory instead, which is gone again once the socket listens."""
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    monkeypatch.setattr(rt, "PROC_FD", str(tmp_path / "no-proc-here"))
    links = Path(tempfile.mkdtemp(prefix="links-", dir="/tmp"))
    monkeypatch.setattr(rt.tempfile, "gettempdir", lambda: str(links))
    try:
        server = rt.listen_unix(deep / "s.sock")
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(f"/proc/self/fd/{os.open(deep, os.O_RDONLY)}/s.sock")
            client.close()
        finally:
            server.close()
        assert (deep / "s.sock").exists()
        assert list(links.iterdir()) == []
    finally:
        shutil.rmtree(links, ignore_errors=True)


@pytest.mark.parametrize("writer", ["sessions.save", "starts.write_history", "starts.rewrite_json",
                                    "vpn.write_private", "agent_sandbox.save_trusted", "hosts.status_cache"])
def test_a_full_disk_leaves_no_temporary_file_behind(writer, tmp_path, monkeypatch):
    """Each of these writers makes a temporary file next to its target and renames it over the target; on a full
    disk (here: the rename fails as a write would) the temporary file goes again instead of staying as a dot file."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    module = load(writer.partition(".")[0])

    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    if writer == "sessions.save":
        directory = module.session_dir("demo")
        directory.mkdir(parents=True)
        call = lambda: module.save("demo", {"a": 1})  # noqa: E731
    elif writer == "starts.write_history":
        directory = module.history_dir()
        directory.mkdir(parents=True, exist_ok=True)
        call = lambda: module.write_history("demo", [{"id": "x"}])  # noqa: E731
    elif writer == "starts.rewrite_json":
        directory = tmp_path / "j"
        directory.mkdir()
        (directory / "f.json").write_text("{}")
        call = lambda: module.rewrite_json(directory / "f.json", {"a": 1})  # noqa: E731
    elif writer == "vpn.write_private":
        directory = tmp_path / "v"
        call = lambda: module.write_private(directory / "p.conf", "x")  # noqa: E731
    elif writer == "agent_sandbox.save_trusted":
        directory = module.config_store().parent
        directory.mkdir(parents=True, exist_ok=True)
        call = lambda: module.save_trusted({})  # noqa: E731
    else:
        directory = module.status_file().parent
        directory.mkdir(parents=True, exist_ok=True)
        call = lambda: module.save_statuses({"a": {}})  # noqa: E731
    monkeypatch.setattr(module.os, "replace", no_space)
    if writer == "hosts.status_cache":
        call()  # a cache: a failed write is no error
    else:
        with pytest.raises(OSError):
            call()
    monkeypatch.undo()
    assert [p.name for p in directory.iterdir() if p.name.startswith(".")] == []
