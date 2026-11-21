"""End to end through ./sandbox-ai (the fake Podman's user, live_fakes.Host): a login's callback relayed once into the
sandbox that asked (oauth_relay.py).

The sandbox here: a registered run whose supervisor is the real runtime.control_server (its control socket in the
run's directory), and a fake agent's login server on the sandbox's [::1]:PORT (Codex's shape: /auth/callback answers
302 to /success, which shows the page). The agent's xdg-open asks to open a URL whose redirect_uri is
http://localhost:PORT/auth/callback (approvals.serve_browser, as the launcher serves it); the human allows it with
`./sandbox-ai approvals allow ID`; the browser (this test) is sent to 127.0.0.1:PORT - the catcher's - and gets the
agent's page, through the control socket. (This machine has no network namespaces for a test: the sandbox's server
listens on ::1, which a "localhost" callback reaches first, and the catcher on 127.0.0.1 of the same port.)"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from live_fakes import Host  # noqa: E402
from test_browser_callback_relay import Agent, free_port, wait_for  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


net = load("sandbox_net_for_callback_e2e", "sandbox/net.py")
rt = net.launcher().RUNTIME
ap = load("sandbox_approvals_for_callback_e2e", "sandbox/approvals.py")

LOGIN = ("https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_X&redirect_uri=http%3A%2F%2F"
         "localhost%3A{port}%2Fauth%2Fcallback&state=S1&scope=openid")


class Six(Agent):
    address_family = socket.AF_INET6


def free_both():
    """A port free on 127.0.0.1 and ::1 alike."""
    for _ in range(50):
        port = free_port()
        with socket.socket(socket.AF_INET6) as probe:
            try:
                probe.bind(("::1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no port")


@pytest.fixture
def sandbox(monkeypatch):
    """The user (fakes on PATH, a fake xdg-open that logs what it opens), one running sandbox with its supervisor's
    control socket and its browser socket, and the agent's login server in it."""
    directory = Path(tempfile.mkdtemp(prefix="cbe-", dir="/tmp"))
    host = Host(directory)
    opened = directory / "opened.txt"
    (host.bin / "xdg-open").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {opened}\n")
    (host.bin / "xdg-open").chmod(0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(host.runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(host.home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(host.home / ".local/state"))
    # Here the "sandbox" and the human's machine share one network: the catcher (127.0.0.1) would count as a second
    # process listening on the callback's port in the sandbox (runtime.oauth_listeners, which refuses two). The
    # agent's server listens on ::1: only that address is the sandbox's in this test.
    monkeypatch.setattr(rt, "OAUTH_LISTENER_ADDRESSES", ("::1",))
    run_dir = net.launcher().runs_dir() / "codex-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run": "codex-1", "agent": "codex", "pid": os.getpid(), "workspace": str(host.workspace),
        "session_dir": str(directory), "started": "2026-11-17T10:00:00Z", "session": None}))
    stop = threading.Event()
    supervisor = threading.Thread(target=rt.control_server, args=(run_dir / "control.sock",
                                                                  rt.RuntimeNetwork({}, None, lambda s: None), stop),
                                  daemon=True)
    supervisor.start()
    browser = threading.Thread(target=ap.serve_browser, args=(run_dir / "browser.sock", run_dir / "approvals", stop),
                               kwargs={"notify": lambda *a: None}, daemon=True)
    browser.start()
    assert wait_for(lambda: (run_dir / "control.sock").exists() and (run_dir / "browser.sock").exists())
    port = free_both()
    agent = Six(port=port, host="::1")
    yield {"host": host, "run_dir": run_dir, "agent": agent, "port": port, "opened": opened}
    agent.shutdown()
    stop.set()
    supervisor.join(5)
    browser.join(5)
    shutil.rmtree(directory, ignore_errors=True)


def xdg_open(run_dir, url):
    """The agent's xdg-open in the sandbox: one request on the browser socket."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(str(run_dir / "browser.sock"))
        client.sendall(json.dumps({"url": url}).encode() + b"\n")
        return json.loads(client.makefile().readline())


def browse(port, path, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def closed(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except ConnectionRefusedError:
        return True
    return False


def question_of(host, url):
    done = host.run("approvals", "list", "--json", timeout=120)
    assert done.returncode == 0, done.stderr
    [entry] = [q for q in json.loads(done.stdout) if q.get("url") == url]
    return entry


def test_the_human_opens_the_login_and_its_callback_reaches_the_agent_once(sandbox):
    host, port, agent = sandbox["host"], sandbox["port"], sandbox["agent"]
    url = LOGIN.format(port=port)
    assert xdg_open(sandbox["run_dir"], url)["ok"] is True
    entry = question_of(host, url)
    assert entry["callback"] == {"url": f"http://localhost:{port}/auth/callback", "port": port,
                                 "path": "/auth/callback"}
    assert "its login callback goes to" in entry["question"] and "device-code login" in entry["question"]
    done = host.run("approvals", "allow", entry["id"], timeout=120)
    assert done.returncode == 0, done.stderr
    assert "relayed into the sandbox once" in done.stdout, done.stdout
    # Who takes it in the sandbox is kept while the human looks at the question (security review SAI-26).
    assert f"(pid {os.getpid()}) takes it, and only that process's socket" in done.stdout, done.stdout
    pinned = json.loads((sandbox["run_dir"] / "approvals" / f"{entry['id']}.listener").read_text())
    assert pinned["pid"] == os.getpid() and pinned["port"] == port and isinstance(pinned["inode"], int)
    assert sandbox["opened"].read_text().splitlines() == [url]
    try:
        assert wait_for(lambda: not closed(port), 10)
        # Not the callback: refused, and the catcher waits on.
        assert browse(port, "/favicon.ico")[0] == 404
        status, page = browse(port, "/auth/callback?code=C1&state=S1",
                              headers={"Sec-Fetch-Mode": "navigate", "Cookie": "host=secret"})
        assert status == 200 and b"Signed in to Codex" in page
        paths = [r["path"] for r in agent.requests]
        assert paths == ["/auth/callback?code=C1&state=S1", "/success?id=1"]
        assert "Cookie" not in agent.requests[0]["headers"]
        # The second request: the catcher is gone.
        assert wait_for(lambda: closed(port), 10)
        record = json.loads((sandbox["run_dir"] / "approvals" / f"{entry['id']}.callback").read_text())
        assert record["outcome"] == "relayed" and record["status"] == 200 and record["port"] == port
        assert record["listener"]["pid"] == str(os.getpid())  # the process that took it, in the record
        # Once, also for another machine's catcher: `approvals callback ID` (over SSH) is refused now.
        again = subprocess.run(host.command("approvals", "callback", entry["id"]), env=host.env, cwd=host.workspace,
                               input=json.dumps({"method": "GET", "target": "/auth/callback?code=X", "port": port,
                                                 "host": f"127.0.0.1:{port}", "headers": {}, "body": ""}),
                               capture_output=True, text=True, timeout=120)
        assert again.returncode == 1 and "relayed already" in again.stderr
        assert len(agent.requests) == 2
        log = (host.home / ".local/state/sandbox-ai/launcher.log")
        assert not log.exists() or "login callback" in log.read_text()
    finally:
        wait_for(lambda: closed(port), 5)


def test_another_hosts_catcher_relays_through_approvals_callback(sandbox):
    """The app or the web opens the page on their machine (allow --no-open here); their catcher sends the one
    request over SSH: `sandbox-ai approvals callback ID` with it on stdin."""
    host, port, agent = sandbox["host"], sandbox["port"], sandbox["agent"]
    url = LOGIN.format(port=port)
    xdg_open(sandbox["run_dir"], url)
    entry = question_of(host, url)
    done = host.run("approvals", "allow", entry["id"], "--no-open", timeout=120)
    assert done.returncode == 0 and closed(port) and not sandbox["opened"].exists()  # no catcher here
    request = {"method": "GET", "target": "/auth/callback?code=C2&state=S1", "port": port,
               "host": f"localhost:{port}", "headers": {"accept": "text/html"}, "body": ""}
    wrong = subprocess.run(host.command("approvals", "callback", entry["id"]), env=host.env, cwd=host.workspace,
                           input=json.dumps({**request, "target": "/other"}), capture_output=True, text=True,
                           timeout=120)
    assert wrong.returncode == 1 and "path /other" in wrong.stderr and agent.requests == []
    relayed = subprocess.run(host.command("approvals", "callback", entry["id"]), env=host.env, cwd=host.workspace,
                             input=json.dumps(request), capture_output=True, text=True, timeout=120)
    assert relayed.returncode == 0, relayed.stderr
    answer = json.loads(relayed.stdout.strip().splitlines()[-1])
    import base64
    assert answer["status"] == 200 and b"Signed in to Codex" in base64.b64decode(answer["body"])
    assert [r["path"] for r in agent.requests] == ["/auth/callback?code=C2&state=S1", "/success?id=1"]


def test_a_busy_port_on_this_computer_says_to_use_the_device_login(sandbox):
    host, port = sandbox["host"], sandbox["port"]
    url = LOGIN.format(port=port)
    xdg_open(sandbox["run_dir"], url)
    entry = question_of(host, url)
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        done = host.run("approvals", "allow", entry["id"], timeout=120)
    finally:
        holder.close()
    assert done.returncode == 0, done.stderr
    assert "cannot be relayed" in done.stdout and "codex login --device-auth" in done.stdout
    assert sandbox["opened"].read_text().splitlines() == [url]  # the page still opens (the human allowed it)
    record = json.loads((sandbox["run_dir"] / "approvals" / f"{entry['id']}.callback").read_text())
    assert record["outcome"] == "port busy"


def test_the_catcher_process_closes_after_its_time(sandbox):
    host, port = sandbox["host"], sandbox["port"]
    url = LOGIN.format(port=port)
    xdg_open(sandbox["run_dir"], url)
    entry = question_of(host, url)
    assert host.run("approvals", "allow", entry["id"], "--no-open", timeout=120).returncode == 0
    relay = ap.oauth_relay()
    env = {**os.environ, **{k: v for k, v in host.env.items() if k.startswith("XDG_") or k == "HOME"}}
    started = []

    def popen(command, **kwargs):  # (the host's folders; the login's state as start() hands it over)
        started.append(subprocess.Popen(command, **{**kwargs, "env": {**kwargs.get("env", {}), **env}}))
        return started[-1]
    ok, _ = relay.start(entry["id"], url, popen=popen, wait=1.0)
    assert ok is True
    try:
        assert started[0].wait(30) == 0
        assert closed(port)
        record = json.loads((sandbox["run_dir"] / "approvals" / f"{entry['id']}.callback").read_text())
        assert record["outcome"] == "timed out" and sandbox["agent"].requests == []
    finally:
        if started and started[0].poll() is None:
            started[0].kill()
