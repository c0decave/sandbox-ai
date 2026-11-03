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
    bridge = ["-I", "/sandbox/runtime.py", "--internal-mcp-bridge", "/sandbox/mcp/demo/server.sock"]
    if scenario == "happy":
        a = s.parse_args(["claude", "--mcp", "demo", "--", "-p", "hi"])
        cmd = s.agent_command(a, "claude")
        assert cmd[:2] == ["claude", "--mcp-config"] and cmd[-2:] == ["-p", "hi"]
        assert json.loads(cmd[2]) == {"mcpServers": {"demo": {"command": "python3", "args": bridge}}}
    elif scenario == "sad":
        # Custom binaries and probes get nothing injected.
        assert s.agent_command(s.parse_args(["claude", "--mcp", "demo", "--binary", "/bin/sh"]), "/bin/sh") == ["/bin/sh"]
        assert "OPENCODE_CONFIG_CONTENT" not in s.agent_environment(s.parse_args(["opencode"]))
    else:
        cmd = s.agent_command(s.parse_args(["codex", "--mcp", "demo", "--mcp", "fs", "--", "exec", "x"]), "codex")
        assert cmd[1:5] == ["-c", 'mcp_servers.demo.command="python3"', "-c", f"mcp_servers.demo.args={json.dumps(bridge)}"]
        assert 'mcp_servers.fs.command="python3"' in cmd and cmd[-2:] == ["exec", "x"]
        env = s.agent_environment(s.parse_args(["opencode", "--mcp", "demo"]))
        assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {
            "mcp": {"demo": {"type": "local", "command": ["python3", *bridge], "enabled": True}}}


def test_mcp_socket_dirs_are_mounted_read_only(tmp_path):
    a = s.parse_args(["codex", "--workspace", str(tmp_path / "w"), "--mcp", "demo"])
    s.validate(a)
    host = s.mcp_socket_dir("demo")
    _, run = s.pod_commands(a, "pod", tmp_path, tmp_path / "home")
    assert f"type=bind,src={host},dst=/sandbox/mcp/demo,ro,nodev" in run
    a.backend = "bwrap"
    cmd = s.bwrap_command(a, tmp_path, tmp_path / "home")
    index = cmd.index("/sandbox/mcp/demo")
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


def fake_supervisor(tmp_path, pid, name):
    entry = tmp_path / "proc" / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"python3\0--mcp-socket-dir\0" + os.fsencode(str(s.mcp_socket_dir(name))) + b"\0")


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
    launched = {}
    def popen(cmd, **kwargs):
        launched.update(cmd=cmd, **kwargs)
        (s.mcp_socket_dir("demo") / "server.sock").touch()
        return Mock(pid=4321, poll=Mock(return_value=None))
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m.launcher(), "remove_stale_pod", Mock(return_value=False))
    assert m.ensure_running("demo", a) == "started"
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
    assert m.load_state("demo")["pid"] == 4321
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


def catalog(tmp_path, entries):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": entries}))
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
    assert m.ensure_running("ddg", a) == "started"
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
    assert "c0:duckduckgo  [allowlist html.duckduckgo.com:443]  DuckDuckGo search" in captured.out
    assert "c1:binwalk  [none]  binwalk  (not published yet)" in captured.out
    assert "catalog not read" in captured.err
