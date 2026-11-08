"""MCP: demo server protocol, serve/bridge over real Unix sockets, host lifecycle."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = load("mcp_demo_test", "mcp_demo.py")
s = load("agent_sandbox_mcp_test", "agent_sandbox.py")
m = load("sandbox_mcp_test", "mcp.py")


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()


def rpc(method, params=None, id=1):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        message["id"] = id
    return message


# --- demo server -----------------------------------------------------------

def test_demo_handle(scenario, tmp_path):
    if scenario == "happy":
        init = demo.handle(rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
        assert init["result"]["serverInfo"] == {"name": "sandbox-ai-demo", "version": demo.VERSION}
        assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
        tools = demo.handle(rpc("tools/list", id=2))["result"]["tools"]
        assert [t["name"] for t in tools] == ["list_directory", "version"]
        assert demo.handle(rpc("notifications/initialized", id=None)) is None
        assert demo.handle(rpc("ping", id=3)) == {"jsonrpc": "2.0", "id": 3, "result": {}}
    elif scenario == "sad":
        assert demo.handle(rpc("nope", id=4))["error"]["code"] == -32601
        result = demo.handle(rpc("tools/call", {"name": "list_directory", "arguments": {"path": str(tmp_path / "x")}}))
        assert result["result"]["isError"] is True and "No such file" in result["result"]["content"][0]["text"]
        assert demo.handle(rpc("tools/call", {"name": "missing"}))["result"]["isError"] is True
    else:
        (tmp_path / "dir").mkdir()
        (tmp_path / "file.txt").write_text("12345")
        (tmp_path / "link").symlink_to(tmp_path / "file.txt")
        text = demo.handle(rpc("tools/call", {"name": "list_directory", "arguments": {"path": str(tmp_path)}}))
        listing = json.loads(text["result"]["content"][0]["text"])
        assert listing["path"] == str(tmp_path)
        assert {"name": "file.txt", "type": "file", "size": 5} in listing["entries"]
        assert {"name": "dir", "type": "directory", "size": None} in listing["entries"]
        assert {"name": "link", "type": "symlink", "size": None} in listing["entries"]
        assert [e["name"] for e in listing["entries"]] == sorted(e["name"] for e in listing["entries"])


def test_demo_version_describes_environment():
    info = json.loads(demo.handle(rpc("tools/call", {"name": "version", "arguments": {}}))["result"]["content"][0]["text"])
    assert info["server"] == "sandbox-ai-demo" and info["version"] == demo.VERSION
    for key in ("python", "hostname", "kernel", "pid_namespace", "uid", "container", "seccomp", "cwd"):
        assert key in info


def test_demo_serve_loop_over_stdio():
    lines = "\n".join([json.dumps(rpc("initialize", {"protocolVersion": "2025-06-18"})),
                       json.dumps(rpc("notifications/initialized", id=None)), "not json", '"a string"',
                       json.dumps(rpc("tools/list", id=2))]) + "\n"
    out = io.StringIO()
    demo.serve(io.StringIO(lines), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, None, 2]
    assert replies[1]["error"]["code"] == -32700 and replies[2]["error"]["code"] == -32600


# --- serve and bridge over a real Unix socket ------------------------------

def test_serve_and_bridge_carry_mcp_end_to_end(tmp_path):
    sock = tmp_path / "server.sock"
    server = threading.Thread(target=s.mcp_serve, args=(str(sock), [sys.executable, "-I", str(ROOT / "sandbox/mcp_demo.py")]),
                              kwargs={"cwd": str(tmp_path)}, daemon=True)
    server.start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.05)
    (tmp_path / "hello.txt").write_text("hi")
    requests = [rpc("initialize", {"protocolVersion": "2025-06-18"}), rpc("notifications/initialized", id=None),
                rpc("tools/call", {"name": "list_directory", "arguments": {"path": str(tmp_path)}}, id=2),
                rpc("tools/call", {"name": "version", "arguments": {}}, id=3)]
    # Two concurrent clients each get their own server instance.
    def client():
        bridge = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/runtime.py"), "--internal-mcp-bridge", str(sock)],
                                input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True, text=True, timeout=30)
        return [json.loads(line) for line in bridge.stdout.splitlines()]
    results = []
    threads = [threading.Thread(target=lambda: results.append(client())) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)
    assert len(results) == 2
    for replies in results:
        assert [r["id"] for r in replies] == [1, 2, 3]
        listing = json.loads(replies[1]["result"]["content"][0]["text"])
        assert {"name": "hello.txt", "type": "file", "size": 2} in listing["entries"]
        assert json.loads(replies[2]["result"]["content"][0]["text"])["version"] == demo.VERSION


def test_mcp_bridge_reports_missing_socket(tmp_path):
    result = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/runtime.py"), "--internal-mcp-bridge",
                             str(tmp_path / "absent.sock")], input="", capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and "MCP server socket unavailable" in result.stderr


# --- launcher integration --------------------------------------------------

def test_agent_injection_per_agent(scenario):
    bridge = ["-I", "/sandbox/runtime.py", "--internal-mcp-bridge", "/run/sandbox-mcp/demo/server.sock"]
    if scenario == "happy":
        a = s.parse_args(["claude", "--mcp", "demo", "--", "-p", "hi"])
        cmd = s.agent_command(a, "claude")
        assert cmd[:2] == ["claude", "--mcp-config"] and cmd[-2:] == ["-p", "hi"]
        assert json.loads(cmd[2]) == {"mcpServers": {"demo": {"command": "python3", "args": bridge}}}
    elif scenario == "sad":
        # Custom binaries and probes get nothing injected.
        assert s.agent_command(s.parse_args(["claude", "--mcp", "demo", "--binary", "/bin/sh"]), "/bin/sh") == ["/bin/sh"]
        assert set(json.loads((s.agent_environment(s.parse_args(["opencode"]))).get("OPENCODE_CONFIG_CONTENT", "{}"))) <= set(s.OPENCODE_TELEMETRY_CONFIG)  # only --telemetry off's settings
    else:
        cmd = s.agent_command(s.parse_args(["codex", "--mcp", "demo", "--mcp", "fs", "--", "exec", "x"]), "codex")
        assert cmd[1:5] == ["-c", 'mcp_servers.demo.command="python3"', "-c", f"mcp_servers.demo.args={json.dumps(bridge)}"]
        assert 'mcp_servers.fs.command="python3"' in cmd and cmd[-2:] == ["exec", "x"]
        env = s.agent_environment(s.parse_args(["opencode", "--mcp", "demo"]))
        assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {
            "mcp": {"demo": {"type": "local", "command": ["python3", *bridge], "enabled": True}},
            **s.OPENCODE_TELEMETRY_CONFIG}


def test_mcp_socket_dirs_are_mounted_read_only(tmp_path):
    a = s.parse_args(["codex", "--workspace", str(tmp_path / "w"), "--mcp", "demo"])
    s.validate(a)
    host = s.mcp_socket_dir("demo")
    _, run = s.pod_commands(a, "pod", tmp_path, tmp_path / "home")
    assert f"type=bind,src={host},dst=/sandbox/mcp-upstream/demo,ro,nodev" in run
    a.backend = "bwrap"
    cmd = s.bwrap_command(a, tmp_path, tmp_path / "home")
    index = cmd.index("/sandbox/mcp-upstream/demo")
    assert cmd[index - 2:index] == ["--ro-bind", str(host)]
    server = s.parse_args(["codex", "--workspace", str(tmp_path / "w"), "--mcp-socket-dir", str(tmp_path / "sock")])
    s.validate(server)
    assert f"type=bind,src={tmp_path / 'sock'},dst=/sandbox/mcp-sock,rw,nodev" in s.pod_commands(server, "p", tmp_path, tmp_path)[1]


def test_mcp_names_validated():
    for name in ("bad name", "a.b", "x" * 33, ""):
        with pytest.raises(ValueError):
            s.validate(s.parse_args(["codex", "--mcp", name]))


def test_launcher_starts_missing_servers_before_the_sandbox(tmp_path, monkeypatch):
    order = []
    module = Mock()
    module.ensure_running.side_effect = lambda name, a: order.append(("ensure", name))
    monkeypatch.setattr(s, "load_mcp_module", lambda: module)
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(s, "run_pod", lambda *args: order.append(("sandbox",)) or 0)
    assert s.main(["codex", "--workspace", str(tmp_path / "w"), "--offline", "--no-auth", "--mcp", "demo", "--mcp", "fs"]) == 0
    assert order == [("ensure", "demo"), ("ensure", "fs"), ("sandbox",)]


def test_dry_run_lists_mcp_without_starting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(s, "load_mcp_module", Mock(side_effect=AssertionError("must not start")))
    s.main(["codex", "--mcp", "demo", "--dry-run"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["mcp"] == [{"name": "demo", "socket": str(s.mcp_socket_dir("demo") / "server.sock")}]


# --- host lifecycle (mcp.py) -----------------------------------------------

def test_definitions(scenario, tmp_path):
    registry = tmp_path / "cfg/sandbox-ai/mcp.json"
    if scenario == "happy":
        defs = m.definitions()
        assert defs["demo"]["command"] == ["python3", "-I", "/opt/sandbox-ai/mcp_demo.py"]
        assert f"{ROOT / 'sandbox/mcp_demo.py'}:/opt/sandbox-ai/mcp_demo.py:ro" in defs["demo"]["options"]
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"fs": {"command": ["npx", "-y", "server-fs", "/work"], "workspace": "/srv/data"}}))
        project = {"mcp_servers": {"fs": {"command": ["uvx", "other"]}}}
        assert m.definitions(project)["fs"]["command"] == ["uvx", "other"]
        assert m.definitions()["fs"]["workspace"] == "/srv/data"
    elif scenario == "sad":
        registry.parent.mkdir(parents=True)
        for bad in [{"x": {}}, {"x": {"command": "npx"}}, {"x": {"command": ["a"], "options": ["--mcp", "y"]}},
                    {"x": {"command": ["a"], "options": ["--copy-auth"]}}, {"x": {"command": ["a"], "agent": "gpt"}},
                    {"bad name": {"command": ["a"]}}, {"x": {"command": ["a"], "extra": 1}}]:
            registry.write_text(json.dumps(bad))
            with pytest.raises(ValueError):
                m.definitions()
    else:
        registry.parent.mkdir(parents=True)
        registry.write_text("{broken")
        with pytest.raises(ValueError, match="mcp.json"):
            m.definitions()


def fake_supervisor(tmp_path, pid, name, start=None):
    entry = tmp_path / "proc" / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"python3\0--mcp-socket-dir\0" + os.fsencode(str(s.mcp_socket_dir(name))) + b"\0")
    if start is not None:
        (entry / "stat").write_text(f"{pid} (python3) S 1" + " 0" * 17 + f" {start} 4096\n")  # start: field 22


def test_status_compares_the_start_time(scenario, tmp_path):
    fake_supervisor(tmp_path, 66, "demo", start=7000)
    state = {"happy": {"pid": 66, "pid_start": 7000}, "sad": {"pid": 66, "pid_start": 6999},
             "edge": {"pid": 66}}[scenario]
    m.save_state("demo", state)
    # sad: same PID and command line, but a later process; edge: a record of an older version.
    assert m.status("demo") == {"happy": "running", "sad": "stopped", "edge": "running"}[scenario]


def test_ensure_running(scenario, tmp_path, monkeypatch):
    a = s.parse_args(["claude", "--backend", "podman"])
    if scenario == "edge":
        # Already running: reused, nothing started.
        sock = s.mcp_socket_dir("demo")
        sock.mkdir(parents=True)
        (sock / "server.sock").touch()
        fake_supervisor(tmp_path, 77, "demo")
        demo = m.definitions(None)["demo"]
        m.save_state("demo", {"pid": 77, "definition": m.definition_digest(demo, "podman")})
        monkeypatch.setattr(m.subprocess, "Popen", Mock(side_effect=AssertionError("started twice")))
        assert m.ensure_running("demo", a) == "running"
        # Same name, other definition (another project, or unknown origin): never shared.
        for state in [{"pid": 77, "definition": m.definition_digest({**demo, "workspace": "/other"}, "podman")},
                      {"pid": 77}]:
            m.save_state("demo", state)
            with pytest.raises(ValueError, match="different definition"):
                m.ensure_running("demo", a)
        return
    if scenario == "sad":
        with pytest.raises(ValueError, match="Unknown MCP server: nope"):
            m.ensure_running("nope", a)
        return
    launched, readers = {}, []
    def popen(cmd, **kwargs):
        if "--internal-log" in cmd:  # the log reader: the supervisor's output goes through it
            readers.append(cmd)
            return Mock(pid=4320)
        launched.update(cmd=cmd, **kwargs)
        (s.mcp_socket_dir("demo") / "server.sock").touch()
        fake_supervisor(tmp_path, 4321, "demo", start=31337)
        return Mock(pid=4321, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m.launcher(), "remove_stale_pod", Mock(return_value=False))
    assert m.ensure_running("demo", a) == "started"
    assert readers == [[sys.executable, "-I", str(ROOT / "sandbox/mcp.py"), "--internal-log", str(m.state_dir("demo"))]]
    cmd = launched["cmd"]
    assert cmd[3] == "codex" and "--no-auth" in cmd and "--no-provider-defaults" in cmd
    assert cmd[cmd.index("--mcp-socket-dir") + 1] == str(s.mcp_socket_dir("demo"))
    assert cmd[cmd.index("--backend") + 1] == "podman" and cmd[cmd.index("--binary") + 1] == "python3"
    assert cmd[cmd.index("--") + 1:] == ["-I", "/sandbox/runtime.py", "--internal-mcp-serve",
                                         "/sandbox/mcp-sock/server.sock", "--", "python3", "-I", "/opt/sandbox-ai/mcp_demo.py"]
    assert launched["start_new_session"] is True
    # A fixed, labelled pod name: a pod left by a crashed supervisor can be found and removed.
    assert cmd[cmd.index("--pod-name") + 1] == "agent-mcp-demo"
    m.launcher().remove_stale_pod.assert_called_once_with(a, "agent-mcp-demo", s.mcp_socket_dir("demo").resolve())
    assert m.load_state("demo")["pid"] == 4321 and m.load_state("demo")["pid_start"] == 31337
    assert s.mcp_socket_dir("demo").stat().st_mode & 0o777 == 0o700


def test_wait_for_socket_reports_log(tmp_path):
    directory = tmp_path / "x"
    (directory / "sock").mkdir(parents=True)
    (directory / "supervisor.log").write_text("image missing\n")
    with pytest.raises(RuntimeError, match="image missing"):
        m.wait_for_socket(directory / "sock" / "server.sock", directory / "supervisor.log",
                          Mock(poll=Mock(return_value=1), returncode=1))


def test_stop_and_list(tmp_path, monkeypatch, capsys):
    fake_supervisor(tmp_path, 88, "demo")
    sock = s.mcp_socket_dir("demo")
    sock.mkdir(parents=True)
    (sock / "server.sock").touch()
    m.save_state("demo", {"pid": 88})
    assert m.cli(["list"]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert rows[0].split()[:2] == ["NAME", "STATUS"] and rows[1].split()[:2] == ["demo", "running"]
    kills = []
    def kill(pid, sig):
        kills.append(pid)
        (tmp_path / "proc/88/cmdline").write_bytes(b"other\0")
    monkeypatch.setattr(m.os, "kill", kill)
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    assert m.cli(["stop", "demo"]) == 0
    assert kills == [88] and m.status("demo") == "stopped"


def test_wrapper_script(tmp_path):
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "st"), "XDG_CONFIG_HOME": str(tmp_path / "c")}
    result = subprocess.run([str(ROOT / "sandbox-mcp"), "list"], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[1].split()[:2] == ["demo", "stopped"]


def test_review_status_follows_the_supervisor_not_the_socket(tmp_path, monkeypatch):
    # The untrusted server can delete its socket; it must still count as running and be stoppable.
    fake_supervisor(tmp_path, 99, "demo")
    s.mcp_socket_dir("demo").mkdir(parents=True)
    m.save_state("demo", {"pid": 99})
    assert m.status("demo") == "running"
    kills = []
    def kill(pid, sig):
        kills.append(pid)
        (tmp_path / "proc/99/cmdline").write_bytes(b"gone\0")
    monkeypatch.setattr(m.os, "kill", kill)
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    assert m.stop("demo") == 0 and kills == [99]


def test_review_running_server_without_socket_is_restarted(tmp_path, monkeypatch):
    fake_supervisor(tmp_path, 98, "demo")
    s.mcp_socket_dir("demo").mkdir(parents=True)
    m.save_state("demo", {"pid": 98, "definition": m.definition_digest(m.definitions(None)["demo"], "podman")})
    stopped = []
    monkeypatch.setattr(m, "stop_locked", lambda name, timeout=30: stopped.append(name) or
                        (tmp_path / "proc/98/cmdline").write_bytes(b"gone\0"))
    def popen(cmd, **kwargs):
        (s.mcp_socket_dir("demo") / "server.sock").touch()
        return Mock(pid=4000, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m.launcher(), "remove_stale_pod", Mock(return_value=False))
    assert m.ensure_running("demo", s.parse_args(["codex"])) == "started"
    assert stopped == ["demo"]


def test_definitions_refuse_relative_paths_and_engine_options(scenario):
    """Review finding: a relative workspace resolved per agent while the running server is shared."""
    definition = {"command": ["python3", "x.py"]}
    if scenario == "happy":
        ok = {**definition, "workspace": "~/srv/data", "options": ["--mount", "/srv/a:/mnt/a:ro"]}
        assert m.validated({"files": ok}, "test")["files"] == ok
    elif scenario == "sad":
        for bad in [{"workspace": "."}, {"options": ["--mount", "data:/mnt/data:rw"]},
                    {"options": ["--local-config=conf.json"]}]:
            with pytest.raises(ValueError, match="absolute paths"):
                m.validated({"files": {**definition, **bad}}, "test")
    else:
        # The agent's backend and Podman service always apply; a definition cannot pick its own.
        for bad in [["--backend", "bwrap"], ["--podman-url=unix:///x"]]:
            with pytest.raises(ValueError, match="not allowed"):
                m.validated({"files": {**definition, "options": bad}}, "test")


def test_stop_removes_own_pod_after_kill(scenario, monkeypatch):
    m.save_state("demo", {"pid": 99, "backend": "podman" if scenario != "edge" else "bwrap", "podman_url": None})
    states = iter(["running", "running", "running", "stopped"])
    monkeypatch.setattr(m, "status", lambda name: next(states, "stopped"))
    monkeypatch.setattr(m.os, "kill", Mock())
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    remove = Mock(return_value=True) if scenario != "sad" else Mock(side_effect=ValueError("not created here"))
    monkeypatch.setattr(m.launcher(), "remove_stale_pod", remove)
    assert m.stop("demo", timeout=0) == 0
    if scenario == "edge":
        remove.assert_not_called()
    else:
        assert remove.call_args.args[1:] == ("agent-mcp-demo", s.mcp_socket_dir("demo").resolve())


def test_demo_server_rejects_malformed_requests(scenario):
    import importlib.util as util
    spec = util.spec_from_file_location("mcp_demo_review", ROOT / "sandbox/mcp_demo.py")
    demo = util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    params = {"happy": {"name": "list_directory", "arguments": {"path": 5}},
              "sad": {"name": "list_directory", "arguments": ["x"]}, "edge": "not an object"}[scenario]
    reply = demo.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    if scenario == "edge":
        assert reply["result"]["isError"] is True
    else:
        assert reply["result"]["isError"] is True and "must be" in reply["result"]["content"][0]["text"]
    assert demo.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": []})["result"]["protocolVersion"]


def test_server_lock_serializes_starts(scenario, tmp_path, monkeypatch):
    """A second agent waits for the first start and then reuses the server instead of starting another."""
    import fcntl
    directory = m.state_dir("demo")
    directory.mkdir(parents=True)
    held = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT)
    try:
        if scenario == "happy":
            fcntl.flock(held, fcntl.LOCK_EX)
            threading.Timer(0.5, lambda: fcntl.flock(held, fcntl.LOCK_UN)).start()
            start = time.monotonic()
            with m.server_lock("demo", timeout=10):
                assert time.monotonic() - start >= 0.4
        elif scenario == "sad":
            fcntl.flock(held, fcntl.LOCK_EX)
            with pytest.raises(ValueError, match="busy"):
                with m.server_lock("demo", timeout=0.3):
                    pass
        else:
            with m.server_lock("demo"):
                pass
            with m.server_lock("demo"):  # released again
                pass
    finally:
        os.close(held)


# --- catalog servers (container images) -------------------------------------------------------------

DIGEST = "sha256:" + "a" * 64


@pytest.fixture(scope="session")
def mcp_key(tmp_path_factory):
    """An MCP signing key for the tests (the real ones are in sandbox/mcp-signers)."""
    directory = tmp_path_factory.mktemp("mcp-key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(directory / "key")],
                   check=True)
    kind, key = (directory / "key.pub").read_text().split()[:2]
    k = m.release_keys()
    (directory / "signers").write_text(k.line(kind, key, k.MCP) + "\n")
    return directory


@pytest.fixture(autouse=True)
def trusted_mcp_key(mcp_key, monkeypatch):
    monkeypatch.setitem(m.release_keys().MCP, "signers", mcp_key / "signers")
    SIGNING_KEY[:] = [mcp_key / "key"]
    return mcp_key


SIGNING_KEY = []


def sign(path, key=None, ring=None):
    k = m.release_keys()
    data = Path(path).read_bytes()
    Path(str(path) + ".sig").write_bytes(k.sign(data, key or SIGNING_KEY[0], ring or k.MCP))


def catalog(tmp_path, entries, signed=True):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": entries}))
    if signed:
        sign(path)
    return [("t", str(path))]


ENTRY = {"id": "mcp:duckduckgo", "name": "duckduckgo", "title": "DuckDuckGo search", "kind": "oci",
         "image": "ghcr.io/c0decave/sandbox-ai-mcp-duckduckgo", "digest": DIGEST, "network": "allowlist",
         "hosts": ["html.duckduckgo.com:443"]}
BINWALK = {"id": "hub:binwalk", "name": "binwalk", "title": "binwalk", "kind": "oci",
           "image": "ghcr.io/c0decave/axross-mcp-binwalk", "digest": DIGEST, "network": "none",
           "mounts": [{"target": "/work", "required": True}, {"target": "/out"}]}
BRAVE = {"id": "mcp:brave-search", "name": "brave-search", "title": "Brave", "kind": "oci",
         "image": "ghcr.io/c0decave/sandbox-ai-mcp-brave-search", "digest": DIGEST, "network": "allowlist",
         "hosts": ["api.search.brave.com:443"], "env": [{"name": "BRAVE_API_KEY", "required": True, "secret": True}]}


def test_catalog_entry_becomes_a_checked_definition(tmp_path):
    sources = catalog(tmp_path, [ENTRY, BINWALK, BRAVE, {**ENTRY, "name": "pending", "digest": None}])
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    out = []
    # An allowlist entry needs the full network granted explicitly.
    with pytest.raises(ValueError, match="add --network full"):
        m.add("duckduckgo", catalogs=sources, pull=False, out=out.append)
    definition = m.add("duckduckgo", network="full", catalogs=sources, pull=False, out=out.append)
    assert definition["image"] == f"ghcr.io/c0decave/sandbox-ai-mcp-duckduckgo@{DIGEST}" and definition["network"] == "full"
    assert any("html.duckduckgo.com:443" in line for line in out)
    with pytest.raises(ValueError, match="needs --mount /work"):
        m.add("t:binwalk", catalogs=sources, pull=False, out=out.append)
    m.add("binwalk", mounts=[f"/work={firmware}"], catalogs=sources, pull=False, out=out.append)
    with pytest.raises(ValueError, match="needs --env BRAVE_API_KEY"):
        m.add("brave-search", network="full", catalogs=sources, pull=False, out=out.append)
    with pytest.raises(ValueError, match="not published"):
        m.add("pending", network="full", catalogs=sources, pull=False, out=out.append)
    servers = m.definitions()
    assert servers["binwalk"]["mounts"] == [{"target": "/work", "host": str(firmware), "mode": "ro"}]
    assert m.config_file().stat().st_mode & 0o077 == 0


def test_image_command_is_the_hardened_run_line(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    definition = m.validated_image({"image": f"ghcr.io/a/b@{DIGEST}", "network": "none", "env": ["KEY"],
                                    "mounts": [{"target": "/work", "host": str(work), "mode": "rw"}],
                                    "memory": "2g", "pids": 256}, "t")
    command = m.image_command(definition)
    assert command[:2] == ["podman", "run"] and command[-1] == f"ghcr.io/a/b@{DIGEST}"
    for flag in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--pull=never",
                 "--network=none", "--env=KEY", "--memory=2g", "--pids-limit=256", "--user=0:0",
                 f"--volume={work.resolve()}:/work:rw"):
        assert flag in command, flag
    full = m.image_command({**definition, "network": "full", "env": [], "mounts": []})
    assert f"--network={s.POD_NETWORK}" in full and "--user=0:0" not in full


@pytest.mark.parametrize("bad", [
    {"image": "ghcr.io/a/b:latest"}, {"image": f"ghcr.io/a/b@{DIGEST}", "network": "allowlist"},
    {"image": f"ghcr.io/a/b@{DIGEST}", "mounts": [{"target": "/work/../etc", "host": "/x"}]},
    {"image": f"ghcr.io/a/b@{DIGEST}", "mounts": [{"target": "/work", "host": "relative"}]},
    {"image": f"ghcr.io/a/b@{DIGEST}", "mounts": [{"target": "/work", "host": "/x,y"}]},
    {"image": f"ghcr.io/a/b@{DIGEST}", "env": ["A=B"]}, {"image": f"ghcr.io/a/b@{DIGEST}", "memory": "1t"},
    {"image": f"ghcr.io/a/b@{DIGEST}", "pids": 1}, {"image": f"ghcr.io/a/b@{DIGEST}", "command": ["sh"]},
    {"image": f"ghcr.io/a/b@{DIGEST}; rm -rf /"},
])
def test_image_definitions_are_refused_unless_every_value_is_safe(bad):
    with pytest.raises(ValueError):
        m.validated({"x": bad}, "test")


def test_image_server_starts_as_a_bridge_on_the_host(tmp_path, monkeypatch):
    config = m.config_file()
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"ddg": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "full", "env": ["KEY"]}}))
    started = []

    class Process:
        pid = os.getpid()

        def poll(self):
            return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)) or Process())
    monkeypatch.setattr(m, "wait_for_socket", lambda *a, **kw: None)
    a = s.parse_args(["codex"])
    monkeypatch.delenv("KEY", raising=False)
    with pytest.raises(ValueError, match="needs the variable KEY"):
        m.ensure_running("ddg", a)
    monkeypatch.setenv("KEY", "value")
    fake_supervisor(tmp_path, os.getpid(), "ddg", start=4242)
    assert m.ensure_running("ddg", a) == "started"
    assert m.load_state("ddg")["pid_start"] == 4242
    cmd, kwargs = started[-1]
    assert cmd[2:4] == [str(ROOT / "sandbox/runtime.py"), "--internal-mcp-serve"]
    assert cmd[4] == str(s.mcp_socket_dir("ddg") / "server.sock") and cmd[5] == "--"
    assert cmd[6:8] == ["podman", "run"] and "--env=KEY" in cmd and "value" not in " ".join(cmd)


def test_catalog_command_lists_both_catalogs(tmp_path, capsys):
    first = tmp_path / "a.json"
    first.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [ENTRY]}))
    second = tmp_path / "b.json"
    second.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [{**BINWALK, "digest": None}]}))
    assert m.cli(["catalog", "-c", str(first), "-c", str(second), "-c", str(tmp_path / "missing.json")]) == 0
    captured = capsys.readouterr()
    assert "c0:duckduckgo  [allowlist html.duckduckgo.com:443]  DuckDuckGo search  unsigned" in captured.out
    assert "c1:binwalk  [none]  binwalk  (not published yet)  unsigned" in captured.out
    sign(first)
    assert m.cli(["catalog", "-c", str(first)]) == 0
    assert "DuckDuckGo search  signed" in capsys.readouterr().out
    assert "catalog not read" in captured.err


def test_tool_calls_are_logged_by_the_serve_end(tmp_path):
    out = io.StringIO()
    short = Path(tempfile.mkdtemp(prefix="mcp-", dir="/tmp"))
    sock = short / "s.sock"
    threading.Thread(target=s.RUNTIME.mcp_serve, args=(str(sock), ["cat"]), kwargs={"cwd": "/", "out": out},
                     daemon=True).start()
    deadline = time.monotonic() + 5
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(sock))
        call = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "list_directory", "arguments": {"path": "/work"}}}) + "\n"
        client.sendall(call.encode())
        client.settimeout(5)
        assert client.makefile("r").readline() == call  # passed through unchanged
    deadline = time.monotonic() + 5
    while "sandbox-ai-mcp-call" not in out.getvalue() and time.monotonic() < deadline:
        time.sleep(0.02)
    line = next(l for l in out.getvalue().splitlines() if l.startswith("sandbox-ai-mcp-call "))
    assert json.loads(line.split(" ", 1)[1])["tool"] == "list_directory"


def test_server_stderr_cannot_forge_call_records():
    """The server's stderr shares the log with our tool-call records: each of its lines is prefixed."""
    import io
    rt = load("runtime_for_stderr_test", "runtime.py")
    out = io.StringIO()
    rt.relay_server_stderr(io.BytesIO(b'sandbox-ai-mcp-call {"tool": "fake"}\nplain\n'), out)
    assert out.getvalue() == 'mcp-server: sandbox-ai-mcp-call {"tool": "fake"}\nmcp-server: plain\n'
    assert not any(line.startswith("sandbox-ai-mcp-call ") for line in out.getvalue().splitlines())


def test_git_credentials_are_not_allowed_for_mcp_servers():
    for option in ("--git-token-secret", "--git-identity"):
        with pytest.raises(ValueError, match="not allowed for an MCP server"):
            m.validated({"x": {"command": ["srv"], "options": [option] + (["github.com=T"] if "secret" in option else [])}}, "test")


def test_image_mounts_pass_the_host_path_policy(tmp_path, monkeypatch):
    """An image server's folder inside an agent's workspace could be swapped for a link to ~/.config."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    work = tmp_path / "proj"
    (work / "extracted").mkdir(parents=True)
    l = m.launcher()
    l.remember_exposed(None, [work])
    inside = {"image": "x", "mounts": [{"host": str(work / "extracted"), "target": "/out", "mode": "rw"}]}
    with pytest.raises(ValueError, match="lies inside .*proj"):
        m.check_image_mounts("bw", inside)
    planted = tmp_path / "other"
    planted.mkdir()
    (work / "link").symlink_to(planted)
    with pytest.raises(ValueError, match="goes through the symlink"):
        m.check_image_mounts("bw", {"image": "x", "mounts": [{"host": str(work / "link"), "target": "/o"}]})
    own = tmp_path / "bw-out"
    own.mkdir()
    m.check_image_mounts("bw", {"image": "x", "mounts": [{"host": str(own), "target": "/out", "mode": "rw"}]})
    assert own in l.exposed_before()  # what the server writes is watched from now on


def test_image_servers_obey_the_policy(monkeypatch):
    l = m.launcher()
    monkeypatch.setattr(l, "load_policy", lambda path=None: {"version": 1, "mcp_network": "none"})
    with pytest.raises(ValueError, match="network full is not allowed"):
        m.check_image_policy("fetch", {"image": "x", "network": "full"})
    m.check_image_policy("bw", {"image": "x"})  # no network: fine


def test_the_catalog_in_sandbox_ais_own_tree_comes_first(tmp_path):
    (tmp_path / "projects" / "sandbox-ai-mcp").mkdir(parents=True)
    local = tmp_path / "projects" / "sandbox-ai-mcp" / "catalog.json"
    local.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [{"name": "x"}]}))
    chosen = dict(m.default_catalogs(tmp_path))
    assert chosen["mcp"] == str(local) and chosen["hub"].startswith("https://")
    assert dict(m.default_catalogs(tmp_path / "elsewhere"))["mcp"].startswith("https://")


def test_an_unpublished_catalog_is_named_as_such():
    import urllib.error

    def missing(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    found, problems = m.catalog_entries([("mcp", "https://example.org/catalog.json")], fetch=missing)
    assert found == [] and "not published there (yet)" in problems[0] and "--catalog" in problems[0]


# --- VPN in front of an MCP server -------------------------------------------------------------------------

def test_image_server_vpn_needs_network_full_and_a_profile_name():
    image = f"ghcr.io/a/b@{DIGEST}"
    assert m.validated_image({"image": image, "network": "full", "vpn": "corp"}, "t")["vpn"] == "corp"
    with pytest.raises(ValueError, match="a VPN needs network full"):
        m.validated_image({"image": image, "vpn": "corp"}, "t")
    with pytest.raises(ValueError, match="vpn must name a VPN profile"):
        m.validated_image({"image": image, "network": "full", "vpn": "../x"}, "t")
    definition = m.definition_from_entry({**ENTRY, "network": "none"}, network="full", vpn="corp")
    assert definition["vpn"] == "corp"
    assert "--network=container:sandbox-ai-vpn-mcp-ddg" in m.image_command(definition, vpn_container="sandbox-ai-vpn-mcp-ddg")
    assert f"--network={s.POD_NETWORK}" in m.image_command(definition)


def test_image_server_with_a_vpn_starts_behind_its_container(tmp_path, monkeypatch):
    config = m.config_file()
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"ddg": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "full", "vpn": "corp"},
                                  "plain": {"image": f"ghcr.io/a/b@{DIGEST}", "network": "full"}}))
    started = []

    class Process:
        pid = os.getpid()

        def poll(self):
            return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or Process())
    monkeypatch.setattr(m, "wait_for_socket", lambda *a, **kw: None)
    fake_supervisor(tmp_path, os.getpid(), "ddg", start=4242)
    assert m.ensure_running("ddg", s.parse_args(["codex"])) == "started"
    cmd = started[-1]
    sock = s.mcp_socket_dir("ddg")
    assert cmd[1:7] == ["-I", str(ROOT / "sandbox/vpn.py"), "--internal-mcp-vpn", "corp", "sandbox-ai-vpn-mcp-ddg",
                        str(sock.resolve())]
    bridge = cmd[cmd.index("--") + 1:]
    assert bridge[2:5] == [str(ROOT / "sandbox/runtime.py"), "--internal-mcp-serve", str(sock / "server.sock")]
    assert "--network=container:sandbox-ai-vpn-mcp-ddg" in bridge and m.load_state("ddg")["vpn"] == "corp"
    # mcp start --vpn puts a VPN in front of a server whose definition has none.
    assert m.ensure_running("plain", s.parse_args(["codex"]), vpn="home") == "started"
    assert started[-1][4] == "home" and m.load_state("plain")["vpn"] == "home"


def test_mcp_start_vpn_for_a_command_server(tmp_path, monkeypatch):
    launched = {}

    def popen(cmd, **kwargs):
        if "--internal-log" in cmd:  # the log reader (mcp.start_logged)
            return Mock(pid=4320)
        launched["cmd"] = cmd
        (s.mcp_socket_dir("demo") / "server.sock").touch()
        fake_supervisor(tmp_path, 4321, "demo", start=1)
        return Mock(pid=4321, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m.launcher(), "remove_stale_pod", Mock(return_value=False))
    a = s.parse_args(["codex", "--backend", "podman"])
    with pytest.raises(ValueError, match="profile name"):
        m.ensure_running("demo", a, vpn="bad name")
    assert m.ensure_running("demo", a, vpn="corp") == "started"
    cmd = launched["cmd"]
    options = cmd[:cmd.index("--")]
    assert options[options.index("--vpn") + 1] == "corp" and m.load_state("demo")["vpn"] == "corp"
    # Running, but without that VPN: not silently reused.
    with pytest.raises(ValueError, match="running without the VPN home"):
        m.ensure_running("demo", a, vpn="home")
    assert m.ensure_running("demo", a, vpn="corp") == "running"


def test_image_servers_and_the_vpn_policy(monkeypatch):
    l = m.launcher()
    image = {"image": "x", "network": "full"}
    monkeypatch.setattr(l, "load_policy", lambda path=None: {"version": 1, "require_vpn": True})
    with pytest.raises(ValueError, match="requires a VPN for network full"):
        m.check_image_policy("fetch", image)
    m.check_image_policy("fetch", {**image, "vpn": "corp"})
    m.check_image_policy("bw", {"image": "x"})  # no network, no VPN needed
    monkeypatch.setattr(l, "load_policy", lambda path=None: {"version": 1, "require_vpn": ["corp"]})
    with pytest.raises(ValueError, match=r"requires a VPN for network full \(corp\)"):
        m.check_image_policy("fetch", {**image, "vpn": "home"})
    monkeypatch.setattr(l, "load_policy", lambda path=None: {"version": 1, "forbid": ["--vpn"]})
    with pytest.raises(ValueError, match="--vpn is not allowed"):
        m.check_image_policy("fetch", {**image, "vpn": "corp"})


def test_mcp_cli_passes_the_vpn(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "ensure_running", lambda name, a, vpn=None: calls.append((name, vpn)))
    monkeypatch.setattr(m, "add", lambda *args, **kwargs: calls.append(("add", kwargs.get("vpn"))))
    assert m.cli(["start", "demo", "--vpn", "corp"]) == 0
    assert m.cli(["add", "duckduckgo", "--network", "full", "--vpn", "corp"]) == 0
    assert calls == [("demo", "corp"), ("add", "corp")]



# --- signed catalogs (sandbox/mcp-signers) ---------------------------------------------------------------------

def test_a_signed_catalog_is_taken_and_the_definition_says_by_whom(tmp_path):
    sources = catalog(tmp_path, [ENTRY])
    definition = m.add("duckduckgo", network="full", catalogs=sources, pull=False, out=lambda line: None)
    k = m.release_keys()
    (kind, key), = k.load(ring=k.MCP)
    assert definition["signed_by"] == k.fingerprint(key)
    assert m.definitions()["duckduckgo"]["signed_by"] == k.fingerprint(key)


def test_an_unsigned_catalog_only_on_request(tmp_path):
    sources = catalog(tmp_path, [ENTRY], signed=False)
    with pytest.raises(ValueError, match="not signed by a trusted key .*--unsigned adds it anyway"):
        m.add("duckduckgo", network="full", catalogs=sources, pull=False, out=lambda line: None)
    out = []
    definition = m.add("duckduckgo", network="full", catalogs=sources, pull=False, out=out.append, unsigned=True)
    assert definition["signed_by"] is None and any("added unsigned" in line for line in out)


TELEGRAM = {"id": "mcp:telegram", "name": "telegram", "title": "Telegram bot", "kind": "oci",
            "image": "ghcr.io/c0decave/sandbox-ai-mcp-telegram", "network": "allowlist", "hosts": ["api.telegram.org:443"],
            "env": [{"name": "TELEGRAM_BOT_TOKEN", "required": True, "secret": True},
                    {"name": "TELEGRAM_ALLOWED_CHATS", "required": False, "secret": False}]}


def test_an_unsigned_entry_refused_afterwards_is_not_said_to_be_added(tmp_path):
    """The catalog of sandbox-ai-mcp as it is published now: unsigned, its entries without a digest. `mcp add telegram
    --unsigned` is refused for the missing digest, and says nothing of "added"; once it has one, it is added with
    the token from the environment (or sandbox-ai secrets) and the full network (it needs only api.telegram.org)."""
    sources = catalog(tmp_path, [TELEGRAM], signed=False)
    out = []
    with pytest.raises(ValueError, match="no digest for it yet"):
        m.add("telegram", env=["TELEGRAM_BOT_TOKEN"], catalogs=sources, pull=False, out=out.append, unsigned=True)
    assert not any("added" in line for line in out)
    sources = catalog(tmp_path, [{**TELEGRAM, "digest": DIGEST}], signed=False)
    with pytest.raises(ValueError, match="needs api.telegram.org:443.*--network full"):
        m.add("telegram", env=["TELEGRAM_BOT_TOKEN"], catalogs=sources, pull=False, out=out.append, unsigned=True)
    with pytest.raises(ValueError, match="needs --env TELEGRAM_BOT_TOKEN"):
        m.add("telegram", network="full", catalogs=sources, pull=False, out=out.append, unsigned=True)
    assert not any("added" in line for line in out)
    definition = m.add("telegram", env=["TELEGRAM_BOT_TOKEN"], network="full", catalogs=sources, pull=False,
                       out=out.append, unsigned=True)
    assert definition["env"] == ["TELEGRAM_BOT_TOKEN"] and definition["network"] == "full"
    assert [line for line in out if "added" in line][0].endswith("added unsigned (" + sources[0][1] + ".sig cannot be "
                                                                 "read)")


@pytest.mark.parametrize("how", ["changed", "other key", "release namespace", "garbage"])
def test_a_signature_that_does_not_verify_is_refused_even_unsigned(tmp_path, how, mcp_key):
    sources = catalog(tmp_path, [ENTRY])
    path = Path(sources[0][1])
    k = m.release_keys()
    if how == "changed":  # a digest swapped after signing
        path.write_text(path.read_text().replace(DIGEST, "sha256:" + "1" * 64))
    elif how == "other key":
        other = tmp_path / "other"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
        sign(path, other)
    elif how == "release namespace":  # a release signature (or any other) is no catalog signature
        sign(path, mcp_key / "key", k.RELEASE)
    else:
        Path(str(path) + ".sig").write_text("not a signature")
    for unsigned in (False, True):
        with pytest.raises(ValueError, match="signature does not verify"):
            m.add("duckduckgo", network="full", catalogs=sources, pull=False, out=lambda line: None, unsigned=unsigned)
    assert "duckduckgo" not in m.definitions()


def test_without_a_trusted_key_nothing_is_signed(tmp_path, monkeypatch):
    sources = catalog(tmp_path, [ENTRY])
    monkeypatch.setitem(m.release_keys().MCP, "signers", tmp_path / "no-signers")
    (entry,) = [e for _, e in m.catalog_entries(sources)[0]]
    assert entry["_signature"]["state"] == "unsigned" and "no MCP signing key" in entry["_signature"]["reason"]


def test_the_shipped_signers_file_is_well_formed():
    k = m.release_keys()
    assert k.load(ring=k.MCP) == k.parse(k.MCP["signers"].read_text(), "mcp-signers", k.MCP)
    with pytest.raises(ValueError, match="mcp@sandbox-ai"):
        k.parse('release@sandbox-ai namespaces="sandbox-ai-release" ssh-ed25519 AAAA', "x", k.MCP)


def test_a_server_that_ignores_the_end_of_its_input_gives_its_slot_back(tmp_path, monkeypatch):
    """An instance whose agent connection ended gets its stdin closed; one that does not exit on that is stopped after
    a grace time, and its slot of MCP_INSTANCE_LIMIT is free again (else, after the limit, every connection is
    refused for good)."""
    rt = s.RUNTIME
    monkeypatch.setattr(rt, "MCP_INSTANCE_LIMIT", 1)
    monkeypatch.setattr(rt, "MCP_EOF_GRACE", 0.5, raising=False)
    server = tmp_path / "stubborn.py"
    server.write_text("import sys, time\nfor line in sys.stdin:\n    print(line, end='', flush=True)\ntime.sleep(30)\n")
    short = Path(tempfile.mkdtemp(prefix="mcp-", dir="/tmp"))
    sock = short / "s.sock"
    threading.Thread(target=rt.mcp_serve, args=(str(sock), [sys.executable, "-I", str(server)]),
                     kwargs={"cwd": "/", "out": io.StringIO()}, daemon=True).start()
    deadline = time.monotonic() + 5
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"

    def talk():
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(sock))
            try:
                client.sendall(ping.encode())
                return client.makefile("r").readline()
            except ConnectionError:
                return ""  # refused: closed at once
    assert talk() == ping
    deadline, answer = time.monotonic() + 10, ""
    while answer != ping and time.monotonic() < deadline:
        answer = talk()  # refused (closed at once) while the first instance still holds the slot
        time.sleep(0.1)
    assert answer == ping, "the slot of the instance that ignored its end of input never came back"


def test_a_thread_that_cannot_start_does_not_end_the_mcp_server(monkeypatch):
    """At the process limit a connection's thread may not start: that connection is closed and its slot given back;
    the next ones are still served (mcp_serve does not end with its socket left open)."""
    rt = s.RUNTIME
    monkeypatch.setattr(rt, "MCP_INSTANCE_LIMIT", 1)
    real, left = threading.Thread, [1]

    class Failing(real):
        def start(self):
            if getattr(self._target, "__name__", "") == "run" and left[0]:
                left[0] -= 1
                raise RuntimeError("can't start new thread")
            return super().start()
    monkeypatch.setattr(threading, "Thread", Failing)
    short = Path(tempfile.mkdtemp(prefix="mcp-", dir="/tmp"))
    sock = short / "s.sock"
    threading.Thread(target=rt.mcp_serve, args=(str(sock), ["cat"]), kwargs={"cwd": "/", "out": io.StringIO()},
                     daemon=True).start()
    deadline = time.monotonic() + 5
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"

    def talk():
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(str(sock))
            try:
                client.sendall(ping.encode())
                return client.makefile("r").readline()
            except ConnectionError:
                return ""
            except OSError:
                return None
    assert talk() == "", "closed: no thread for it"
    assert talk() == ping, "the next connection is served, its slot was given back"


# --- an MCP server into a running sandbox: mcp attach|detach NAME START --live ---------------------------------
#
# The supervisor is real (runtime.control_server with the run's McpRelays, in a short directory); the server's own
# sandbox is a fake one listening on its socket directory (ensure_running is faked); the launcher's checks of a start
# (start_arguments) are the real ones.

rt = s.RUNTIME


def fake_server_socket(directory):
    """A running MCP server's socket in DIRECTORY: each request line is answered with its method (tools/list with
    one tool); (stop event, thread)."""
    listener = rt.listen_unix(str(directory / "server.sock"))
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            def answer(connection=connection):
                with connection:
                    for line in connection.makefile("rb"):
                        request = json.loads(line)
                        if "id" in request:
                            result = ({"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
                                      if request.get("method") == "tools/list" else {"method": request.get("method")})
                            connection.sendall((json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
                                                + "\n").encode())
            threading.Thread(target=answer, daemon=True).start()
        listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return stop, thread


def ask_relay(path, method="tools/list"):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall((json.dumps(rpc(method, id=5)) + "\n").encode())
        line = client.makefile("rb").readline()
        return json.loads(line) if line else None


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A kept start "web" (claude) whose run is registered and running, with a real supervisor on its control socket
    (forbid/mode as SUPERVISOR says), and servers that "start" by listening on their socket directory."""
    short = Path(tempfile.mkdtemp(prefix="live-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    starts = m.starts_module()
    sessions, net = starts.module("sessions"), m.net_module()
    monkeypatch.setattr(sessions, "PROC", tmp_path / "proc")
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(starts.module("net"), "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(m.launcher(), "load_policy", lambda path=None: {})
    (tmp_path / "cfg/sandbox-ai").mkdir(parents=True)
    (tmp_path / "cfg/sandbox-ai/mcp.json").write_text(json.dumps({
        "web-search": {"command": ["python3", "-I", "/opt/x.py"], "options": ["--allow-github"]}}))
    folder = tmp_path / "projects" / "web"
    folder.mkdir(parents=True)
    home = tmp_path / "agent-home"
    home.mkdir()
    stops, started, supervisors = [], [], []

    def make(key="web", agent="claude", options=("--backend", "podman"), record=None, forbid=(), mode=None,
             relays_live=True):
        directory = sessions.session_dir(key)
        for path in (directory, directory / "home", directory / "tmux"):
            path.mkdir(parents=True, exist_ok=True)
        pid = 4100 + len(supervisors)
        entry = tmp_path / "proc" / str(pid)
        entry.mkdir(parents=True)
        (entry / "cmdline").write_bytes(b"\0".join(os.fsencode(str(part)) for part in [
            "python3", "agent_sandbox.py", "--tmux-socket-dir", directory / "tmux"]) + b"\0")
        (entry / "stat").write_text(f"{pid} (python3) S 1 1 1 0 -1\n")
        sessions.save(key, {"agent": agent, "options": list(options), "agent_args": [], "backend": "podman",
                            "workspace": str(folder), "cwd": str(folder), "created": "2026-11-01T10:00:00Z",
                            "pod": f"agent-session-{key}", "pid": pid, "started": "2026-11-02T09:30:00Z"})
        run = s.runs_dir() / f"session-{key}"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({
            "run": f"session-{key}", "agent": agent, "kind": "agent", "backend": "podman", "workspace": str(folder),
            "pid": os.getpid(), "start": key, "session": f"agent-session-{key}", "mcp_servers": [],
            "mcp_live": True, "agent_home": str(home), "mode": {"name": "medium", "refuses": []}, **(record or {})}))
        relays = rt.McpRelays(run_dir=short / f"relays-{key}", log=short / f"calls-{key}.jsonl", live=relays_live)
        stop = threading.Event()
        network = rt.RuntimeNetwork({}, None, lambda script: None)
        thread = threading.Thread(target=rt.control_server, args=(run / "control.sock", network, stop, list(forbid),
                                                                  mode, None, relays), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not (run / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        supervisors.append((stop, thread, relays))
        return run, relays

    def ensure_running(name, a, vpn=None):
        sock = s.mcp_socket_dir(name)
        if (sock / "server.sock").exists():
            return "running"
        sock.mkdir(mode=0o700, parents=True, exist_ok=True)
        stops.append(fake_server_socket(sock))
        started.append((name, a.backend, list(a.mcp), sorted(getattr(a, "helper_definitions", None) or {})))
        return "started"
    monkeypatch.setattr(m, "ensure_running", ensure_running)
    said = []
    yield SimpleNamespace(make=make, said=said, out=said.append, home=home, started=started, sessions=sessions,
                          folder=folder)
    for stop, thread, relays in supervisors:
        for name in relays.names():
            relays.detach(name)
        stop.set()
        thread.join(5)
    for stop, thread in stops:
        stop.set()
        thread.join(5)
    import shutil
    shutil.rmtree(short, ignore_errors=True)


from types import SimpleNamespace  # noqa: E402


def test_attach_live_opens_the_relay_now_and_detach_live_closes_it(live):
    run, relays = live.make()
    result = m.attach_live("demo", "web", out=live.out)
    assert result["attached"] == "live" and result["server_started"] and result["kept"]
    assert result["relay"] == "/run/sandbox-mcp/demo/server.sock" and result["agent_restart"] == "maybe"
    assert live.started == [("demo", "podman", ["demo"], [])], "started as the start starts it"
    # The agent's bridge reaches the server through the run's relay at once.
    reply = ask_relay(relays.run_dir / "demo" / "server.sock")
    assert reply["id"] == 5 and reply["result"]["tools"][0]["name"] == "echo"
    assert json.loads((run / "run.json").read_text())["mcp_servers"] == ["demo"]
    assert "--mcp" in live.sessions.load("web")["options"], "kept for its later starts"
    claude = json.loads((live.home / ".claude.json").read_text())
    assert claude["mcpServers"]["demo"] == {"type": "stdio", "command": "python3", "env": {},
                                            "args": s.mcp_bridge_args("demo")}
    assert "/mcp" in live.said[-1] and "resume web --restart" in live.said[-1]
    # Again: already there, nothing doubled.
    assert m.attach_live("demo", "web", out=live.out)["attached"] == "already"
    assert live.sessions.load("web")["options"].count("demo") == 1
    # mcp list (and sandbox-ai list) say whom it serves now.
    assert m.serving()["demo"] == ["web"]
    # detach --live: the relay is closed (a new connection is refused), out of run.json, the options and ~/.claude.json
    result = m.detach("demo", "web", live=True, out=live.out)
    assert result["detached"] == "live" and result["relay_closed"] and result["removed_from_options"]
    with pytest.raises(OSError):
        ask_relay(relays.run_dir / "demo" / "server.sock")
    assert json.loads((run / "run.json").read_text())["mcp_servers"] == []
    assert "--mcp" not in live.sessions.load("web")["options"]
    assert "mcpServers" not in json.loads((live.home / ".claude.json").read_text())  # as it was before
    assert m.detach("demo", "web", live=True, out=live.out)["detached"] == "not attached"
    calls = [json.loads(line).get("event") for line in (relays.log).read_text().splitlines()]
    assert calls == ["attached", "detached"]


def test_attach_live_keeps_every_check_of_a_start(live, monkeypatch, tmp_path):
    live.make()
    monkeypatch.setattr(m.launcher(), "load_policy", lambda path=None: {"forbid": ["--mcp"]})
    with pytest.raises(ValueError, match="--mcp.*policy"):
        m.attach_live("demo", "web", out=live.out)
    monkeypatch.setattr(m.launcher(), "load_policy", lambda path=None: {})
    with pytest.raises(ValueError, match="Unknown MCP server: nope"):
        m.attach_live("nope", "web", out=live.out)
    # The security mode: paranoid refuses a server that reaches the network, as at a start.
    live.make("tight", options=["--backend", "podman", "--mode", "paranoid", "--no-keep"])
    with pytest.raises(ValueError, match="widens the security mode paranoid"):
        m.attach_live("web-search", "tight", out=live.out)
    # ... and the mode the run has, should its options say another one now.
    live.make("strict", record={"mode": {"name": "paranoid", "refuses": []}})
    with pytest.raises(ValueError, match="widens the security mode paranoid of this run"):
        m.attach_live("web-search", "strict", out=live.out)
    # The supervisor checks for itself: its policy forbids --mcp (a request written by hand reaches it too).
    live.make("guarded", forbid=["--mcp"])
    with pytest.raises(ValueError, match="sandbox refused: --mcp is not allowed"):
        m.attach_live("demo", "guarded", out=live.out)
    record = json.loads((s.runs_dir() / "session-guarded" / "run.json").read_text())
    assert record["mcp_servers"] == [] and not (live.home / ".claude.json").exists()
    assert live.started == [("demo", "podman", ["demo"], [])]  # only the one the supervisor then refused


def test_what_cannot_take_a_server_while_it_runs(live, tmp_path):
    live.make("old", record={"mcp_live": False})
    with pytest.raises(ValueError, match="cannot take an MCP server while it runs.*--restart"):
        m.attach_live("demo", "old", out=live.out)
    live.make("remote", record={"podman_url": "ssh://vm/run/podman.sock"})
    with pytest.raises(ValueError, match="another machine"):
        m.attach_live("demo", "remote", out=live.out)
    live.make("older", relays_live=False)  # its supervisor made no read-only place for live relays
    with pytest.raises(ValueError, match="older version"):
        m.attach_live("demo", "older", out=live.out)
    live.make("paused")
    (live.sessions.session_dir("paused") / "paused").touch()
    with pytest.raises(ValueError, match="paused: resume it first"):
        m.attach_live("demo", "paused", out=live.out)
    live.sessions.session_dir("idle").mkdir(parents=True)
    live.sessions.save("idle", {"agent": "claude", "options": [], "agent_args": [], "cwd": str(live.folder),
                                "workspace": str(live.folder), "created": "2026-11-01T10:00:00Z"})
    with pytest.raises(ValueError, match="does not run.*without it the server is attached for its next start"):
        m.attach_live("demo", "idle", out=live.out)
    # Without --live a stopped start loses it from its options only.
    live.sessions.save("idle", {**live.sessions.load("idle"), "options": ["--mcp", "demo"]})
    result = m.detach("demo", "idle", out=live.out)
    assert result["detached"] == "next start" and live.sessions.load("idle")["options"] == []


def test_attach_live_for_codex_and_a_helper(live):
    """Codex reads its MCP servers when it starts: the relay is open, nothing is written into its configuration and
    the human is told to restart the agent. A helper (--helper AGENT) is an MCP server like any other."""
    run, relays = live.make(agent="codex")
    result = m.attach_live("demo", "web", out=live.out)
    assert result["agent"] == "codex" and result["agent_config"] is None and result["agent_restart"] == "required"
    assert "Codex reads its MCP servers only when it starts" in live.said[-1]
    assert "sandbox-ai resume web --restart" in live.said[-1] and not (live.home / ".claude.json").exists()
    result = m.attach_live(None, "web", helper="claude", out=live.out)
    name = s.helper_name("claude", str(live.folder))
    assert result["server"] == name and live.started[-1] == (name, "podman", ["demo", name], [name])
    assert ask_relay(relays.run_dir / name / "server.sock")["result"]["tools"][0]["name"] == "echo"
    options = live.sessions.load("web")["options"]
    assert options[-2:] == ["--helper", "claude"] and "--mcp" in options
    assert m.detach(None, "web", helper="claude", live=True, out=live.out)["relay_closed"]
    assert "--helper" not in live.sessions.load("web")["options"]


def test_the_end_of_the_run_takes_the_live_entries_out_of_claudes_configuration(live):
    run, _ = live.make()
    (live.home / ".claude.json").write_text(json.dumps({"mcpServers": {"own": {"command": "x"}}, "theme": "dark"}))
    m.attach_live("demo", "web", out=live.out)
    m.attach_live("web-search", "web", out=live.out)
    assert sorted(json.loads((live.home / ".claude.json").read_text())["mcpServers"]) == ["demo", "own", "web-search"]
    m.forget_live_config(run)
    assert json.loads((live.home / ".claude.json").read_text()) == {"mcpServers": {"own": {"command": "x"}},
                                                                    "theme": "dark"}
    m.forget_live_config(run / "gone")  # never raises
    # A server of that name of Claude's own is left as it is (and said), the relay is open all the same.
    (live.home / ".claude.json").write_text(json.dumps({"mcpServers": {"demo": {"command": "mine"}}}))
    m.detach("demo", "web", live=True, out=live.out)
    result = m.attach_live("demo", "web", out=live.out)
    assert result["attached"] == "live" and result["agent_config"] is None
    assert any("names an MCP server demo of its own" in line for line in live.said)


def test_the_command_line_of_attach_and_detach_live(live, capsys, monkeypatch):
    live.make()
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: False)
    assert m.cli(["attach", "demo", "web", "--live", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["attached"] == "live" and result["server"] == "demo"
    assert m.cli(["detach", "demo", "web", "--live", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["detached"] == "live"
    with pytest.raises(ValueError, match="choose one"):
        m.cli(["attach", "demo", "web", "--live", "--restart"])
    with pytest.raises(ValueError, match="give NAME START"):
        m.cli(["attach", "web", "--live"])
    with pytest.raises(ValueError, match="--helper AGENT goes with --live"):
        m.cli(["attach", "--helper", "codex", "web"])


def test_the_supervisor_takes_the_descriptor_over_its_control_socket(live):
    """net.ask hands the directory over (SCM_RIGHTS); the supervisor keeps it, never one it was not asked to take."""
    run, relays = live.make()
    upstream = s.mcp_socket_dir("demo")
    upstream.mkdir(parents=True)
    stop, thread = fake_server_socket(upstream)
    try:
        fd = os.open(upstream, os.O_PATH | os.O_DIRECTORY)
        try:
            answer = m.net_module().ask(run, {"op": "mcp-open", "name": "demo",
                                              "relay": {"server": "demo", "rules": []}}, fds=[fd])
        finally:
            os.close(fd)  # the supervisor holds its own copy
        assert answer["opened"] and ask_relay(relays.run_dir / "demo" / "server.sock")["id"] == 5
        fd = os.open(upstream, os.O_PATH | os.O_DIRECTORY)
        try:
            with pytest.raises(ValueError, match="takes the server's socket directory as one descriptor"):
                m.net_module().ask(run, {"op": "mcp-open", "name": "x", "relay": {"server": "x"}}, fds=[fd, fd])
        finally:
            os.close(fd)
        assert m.net_module().ask(run, {"op": "mcp-list"}) == {"ok": True, "servers": ["demo"], "live": True}
    finally:
        stop.set()
        thread.join(5)
