"""sandbox-ai bridge end to end in processes of their own (the bridge check of 2026-11-22): the broker is the real
`bridge.py --internal-broker ID` that `bridge add` starts, each agent's side is the real MCP client of a sandbox
(`runtime.py --internal-mcp-bridge SOCKET`, as the agent's configuration runs it) talking to its run's real relay, the
human answers through the real `./sandbox-ai approvals` and reads through the real `./sandbox-ai bridge list|show`,
and the halves on other hosts are the real `bridge-stub`, reached through a stand-in ssh (FAKE_SSH).

What is not real here, and why: the runs are test_bridge's (registered runs with real supervisor control sockets and
MCP relays, in threads of this test): this development machine has no Podman and no user namespaces, so no sandbox
runs here - the live check bridge-live (scripts/live_check.sh, tests/test_live_gaps.py) bridges two real sessions.
For the same reason the run's socket folder is handed over as it is instead of as a detached clone made in the
sandbox's user namespace (and the sessions' launchers are the world's /proc entries); and since this machine is
itself a sandbox, where `bridge add|revoke|resume` and `bridge-stub` refuse to run, as they must, those run as
`bridge.main` in a process of their own with only that check passed (DRIVER) when inside_sandbox() says so. `add`
always runs through DRIVER (the socket folder); elsewhere revoke and resume are the real `./sandbox-ai bridge`."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

import pytest

import test_bridge as tb
from test_bridge import world  # noqa: F401  (fixture)
from test_bridge import wait_for

b = tb.b
ROOT = tb.ROOT
SAI = ROOT / "sandbox-ai"
INSIDE = b.module("liveness").inside_sandbox()

DRIVER = r'''
import importlib.util, os, sys
root = sys.argv[1]
spec = importlib.util.spec_from_file_location("bridge", os.path.join(root, "sandbox", "bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
if os.environ.get("SAI_E2E_INSIDE") == "1":
    bridge.inside_sandbox = lambda: False  # (this machine is a sandbox: the real command refuses here, as it must)
mcp = bridge.module("mcp")
def handle(record, directory, identity):  # (no user namespace here to make the detached clone in)
    return os.open(directory, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
mcp.socket_dir_handle = handle
if os.environ.get("SAI_E2E_PROC"):  # (the sessions' launchers of test_bridge's world: their /proc entries)
    from pathlib import Path
    mcp.PROC = Path(os.environ["SAI_E2E_PROC"])
    bridge.module("starts").module("sessions").PROC = Path(os.environ["SAI_E2E_PROC"])
sys.exit(bridge.main(sys.argv[2:]))
'''


def environment():
    env = {**os.environ, "SAI_E2E_INSIDE": "1" if INSIDE else "0",
           "SAI_E2E_PROC": str(Path(os.environ["XDG_STATE_HOME"]).parent / "proc")}
    env.pop("SANDBOX_AI_RUN", None)
    return env


def sai(*args, timeout=60):
    """The real ./sandbox-ai ARGS (this test's folders: the world's XDG variables)."""
    return subprocess.run([str(SAI), *map(str, args)], capture_output=True, text=True, env=environment(),
                          timeout=timeout, stdin=subprocess.DEVNULL)


def bridge_command(*args, timeout=60):
    """`sandbox-ai bridge ARGS`: add always through DRIVER (the socket folder is handed over as it is: no user
    namespace here); revoke and resume through it only inside a sandbox, else the real ./sandbox-ai."""
    if args[0] == "add" or INSIDE:
        script = Path(os.environ["XDG_STATE_HOME"]).parent / "bridge-driver.py"
        script.write_text(DRIVER)
        return subprocess.run([sys.executable, "-I", str(script), str(ROOT), *map(str, args)], capture_output=True,
                              text=True, env=environment(), timeout=timeout, stdin=subprocess.DEVNULL)
    return sai("bridge", *args, timeout=timeout)


def ok(done):
    assert done.returncode == 0, f"exit {done.returncode}\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
    return done.stdout


class Agent:
    """An agent's MCP client in a process of its own: the sandbox's runtime.py --internal-mcp-bridge on the relay
    socket of SERVER in the run SIDE (what the agent's configuration starts for bridge-PEER)."""

    def __init__(self, side, server):
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(ROOT / "sandbox" / "runtime.py"), "--internal-mcp-bridge",
             str(side.relays.run_dir / server / "server.sock")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.number = 0
        reply = self.request("initialize", {"protocolVersion": b.PROTOCOL, "capabilities": {},
                                            "clientInfo": {"name": "e2e", "version": "1"}})
        assert reply["result"]["serverInfo"], reply
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params, timeout=60):
        self.number += 1
        self.send({"jsonrpc": "2.0", "id": self.number, "method": method, "params": params})
        found = {}

        def read():
            found["line"] = self.process.stdout.readline()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        line = found.get("line")
        if not line:
            raise ConnectionError(f"no answer to {method} (the relay or the broker is gone): "
                                  f"{self.process.stderr.read1(2000) if self.process.poll() is not None else ''}")
        return json.loads(line)

    def tools(self):
        return [tool["name"] for tool in self.request("tools/list", {})["result"]["tools"]]

    def call(self, tool, arguments=None, timeout=60):
        return self.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout)["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()


def text(result):
    return result["content"][0]["text"]


@pytest.fixture
def processes(world):  # noqa: F811
    """Agents and brokers of the test, ended at its end (each by its own PID)."""
    made = {"agents": [], "brokers": []}
    yield made
    for agent in made["agents"]:
        agent.close()
    for pid in made["brokers"]:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        wait_for(lambda: not alive(pid), 10)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # (a zombie of ours: ended)
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][:1] != "Z"
    except (OSError, IndexError):
        return False


def added(processes, *args):
    """`bridge add ARGS --json` in a process of its own; the bridge's record (its broker a real process)."""
    plan = json.loads(ok(bridge_command("add", *args, "--json")))
    data = b.load(plan["id"])
    pid = data["broker_pid"]
    processes["brokers"].append(pid)
    assert pid != os.getpid() and alive(pid), "the broker is a process of its own"
    return data


def agent(processes, side, server):
    made = Agent(side, server)
    processes["agents"].append(made)
    return made


def listed(ident):
    rows = json.loads(ok(sai("bridge", "list", "--all", "--json")))
    return next(row for row in rows if row["id"] == ident)


def open_question(ident, seconds=20):
    """The bridge's question `sandbox-ai approvals list --json` lists (its id), once there is one."""
    found = {}

    def look():
        rows = json.loads(ok(sai("approvals", "list", "--json")))
        mine = [row for row in rows if row.get("sandbox") == f"bridge-{ident}" or
                row.get("run") == f"bridge-{ident}" or row.get("bridge") == ident]
        if mine:
            found["row"] = mine[0]
        return bool(mine)
    assert wait_for(look, seconds), ok(sai("approvals", "list", "--json"))
    return found["row"]


def slowly(condition, seconds):
    """wait_for with a look every half second (each look is a call, and every call counts against the rate)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.5)
    return False


def in_thread(function, *args, **kwargs):
    result = {}
    thread = threading.Thread(target=lambda: result.update(value=function(*args, **kwargs)), daemon=True)
    thread.start()
    return thread, result


def test_both_directions_message_ask_files_and_watch_between_real_processes(world, processes):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    for side in (alpha, beta):
        (side.folder / "src").mkdir()
        (side.folder / "src" / "app.py").write_text(f"print('{side.key}')\n")
        (side.folder / "out").mkdir()
    (beta.logs / "activity.jsonl").write_text(json.dumps({
        "v": 1, "time": "2026-11-22T10:00:00Z", "run": "session-beta", "kind": "command",
        "summary": "make test", "details": {}}) + "\n")
    data = added(processes, "session-alpha", "session-beta", "--allow", "message,ask,files:ro:src,files:rw:out,watch",
                 "--both", "--ask", "never", "--observer-actions", "alert,hint")
    ident = data["id"]
    assert listed(ident)["state"] == "active"
    a = agent(processes, alpha, data["a"]["server"])
    bb = agent(processes, beta, data["b"]["server"])
    assert {"send_message", "ask", "read_file", "write_file", "watch_changes", "alert", "hint", "inbox",
            "answer"} <= set(a.tools())
    # message, and the reader is tainted by it (its own relay), the sender not.
    assert not a.call("send_message", {"text": "hello from alpha"})["isError"]
    assert "hello from alpha" in text(bb.call("inbox", {"wait_seconds": 5}))
    assert beta.taint.shown()["tainted"] and not alpha.taint.shown()["tainted"]
    # ask: the peer's agent answers (it learns the question's id from its inbox).
    asker = agent(processes, alpha, data["a"]["server"])
    thread, asked = in_thread(asker.call, "ask", {"question": "which port?", "timeout_seconds": 30}, timeout=60)
    question = {}
    assert wait_for(lambda: question.update(text=text(bb.call("inbox", {"wait_seconds": 2}))) or
                    "which port?" in question["text"], 20)
    question_id = re.search(r"question ([0-9a-f]{8}) \(", question["text"]).group(1)
    assert not bb.call("answer", {"question_id": question_id, "text": "8080"})["isError"]
    thread.join(30)
    assert "8080" in text(asked["value"]) and alpha.taint.shown()["tainted"]
    # files:ro: below the grant only.
    assert "print('beta')" in text(a.call("read_file", {"path": "src/app.py"}))
    assert "app.py" in text(a.call("list_files", {"path": "src"}))
    for path in ("../beta/src/app.py", "/etc/passwd", "out/../../x"):
        assert a.call("read_file", {"path": path})["isError"], path
    # files:rw: every write is asked, the human answers with the real `sandbox-ai approvals`, the diff shown there.
    thread, wrote = in_thread(a.call, "write_file", {"path": "out/note.txt", "content": "made by alpha\n"},
                              timeout=60)
    row = open_question(ident)
    shown = ok(sai("approvals", "show", row["id"]))
    assert "+made by alpha" in shown and "write_file" in shown
    ok(sai("approvals", "allow", row["id"]))
    thread.join(30)
    assert not wrote["value"]["isError"], text(wrote["value"])
    assert (beta.folder / "out" / "note.txt").read_text() == "made by alpha\n"
    thread, denied = in_thread(a.call, "write_file", {"path": "out/note.txt", "content": "evil\n"}, timeout=60)
    row = open_question(ident)
    ok(sai("approvals", "deny", row["id"]))
    thread.join(30)
    assert denied["value"]["isError"] and (beta.folder / "out" / "note.txt").read_text() == "made by alpha\n"
    assert a.call("write_file", {"path": "src/app.py", "content": "x"})["isError"], "src is read-only for alpha"
    # watch, and the observer's actions that are switched on.
    assert "make test" in text(a.call("watch_activity", {"limit": 100}))
    assert wait_for(lambda: "note.txt" in text(a.call("watch_changes")), 15)
    assert not a.call("alert", {"message": "beta writes a lot", "severity": "warning"})["isError"]
    assert any(x.get("text") == "beta writes a lot" for x in json.loads(ok(sai("bridge", "show", ident, "--json")))
               .get("alerts") or [])
    assert not a.call("hint", {"text": "slow down"})["isError"]
    assert "slow down" in text(bb.call("inbox", {"wait_seconds": 5}))
    assert "not granted" in text(a.call("cut_network", {"reason": "x"})), "net-cut is not switched on"
    # --both: beta has the same over alpha.
    assert not bb.call("send_message", {"text": "hello from beta"})["isError"]
    assert "hello from beta" in text(a.call("inbox", {"wait_seconds": 5}))
    assert "print('alpha')" in text(bb.call("read_file", {"path": "src/app.py"}))
    # (the broker writes its counters into the record at its next look, once a second)
    assert wait_for(lambda: json.loads(ok(sai("bridge", "show", ident, "--json")))["calls"] >= 10, 15)
    assert json.loads(ok(sai("bridge", "show", ident, "--json")))["state"] == "active"
    # revoke: the broker process ends, both relays and configurations go, the agents' next call fails.
    ok(bridge_command("revoke", ident))
    assert wait_for(lambda: not alive(data["broker_pid"]), 15), "the broker process ended"
    assert listed(ident)["state"] == "revoked"
    assert alpha.relays.names() == [] and beta.relays.names() == []
    assert "mcpServers" not in json.loads((alpha.home / ".claude.json").read_text())
    with pytest.raises((ConnectionError, OSError, ValueError, KeyError)):
        a.call("info", timeout=10)


def test_limits_hold_between_real_processes(world, processes):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    (beta.folder / "big.txt").write_text("x" * (100 << 10))
    data = added(processes, "session-alpha", "session-beta", "--allow", "message,ask,files:ro:.", "--ask", "never",
                 "--limit", "size=256", "--limit", "ask-timeout=5", "--limit", "daily=64k")
    a = agent(processes, alpha, data["a"]["server"])
    too_long = a.call("send_message", {"text": "y" * 300})
    assert too_long["isError"] and "256" in text(too_long)
    began = time.monotonic()
    unanswered = a.call("ask", {"question": "anyone?", "timeout_seconds": 60}, timeout=60)
    assert unanswered["isError"] and "no answer within 5 s" in text(unanswered) and time.monotonic() - began < 20
    assert not a.call("read_file", {"path": "big.txt", "max_bytes": 40 << 10})["isError"]
    over = a.call("read_file", {"path": "big.txt", "offset": 40 << 10, "max_bytes": 40 << 10})
    assert over["isError"] and "day" in text(over).lower(), text(over)
    gamma = world.make("gamma")
    rated = added(processes, "session-gamma", "session-alpha", "--allow", "message", "--ask", "never",
                  "--limit", "rate=2")
    bb = agent(processes, gamma, rated["a"]["server"])
    # (a burst of 5 at most - Broker.burst -, then 2 a minute)
    results = [bb.call("send_message", {"text": f"n{i}"}) for i in range(8)]
    assert not any(r["isError"] for r in results[:5]), [text(r) for r in results]
    assert all(r["isError"] and "more than 2 calls a minute" in text(r) for r in results[6:]), \
        [text(r) for r in results]


def test_a_kill_switch_or_canary_suspends_until_the_human_resumes(world, processes):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    data = added(processes, "session-alpha", "session-beta", "--allow", "message,watch", "--ask", "never",
                 "--observer-actions", "alert,net-cut")
    ident = data["id"]
    a = agent(processes, alpha, data["a"]["server"])
    (beta.run / "cut.json").write_text(json.dumps({"mode": "api", "cuts": {"session-beta": "api"}}))
    assert wait_for(lambda: listed(ident)["state"] == "suspended", 15)
    refused = a.call("send_message", {"text": "x"})
    assert refused["isError"] and "suspended" in text(refused)
    held = bridge_command("resume", ident)
    assert held.returncode == 1 and "still holds" in held.stderr
    (beta.run / "cut.json").unlink()
    ok(bridge_command("resume", ident))
    assert slowly(lambda: not a.call("send_message", {"text": "again"})["isError"], 15)
    # A canary of either run.
    events = Path(os.environ["XDG_STATE_HOME"]) / "sandbox-ai/canaries/runs/session-alpha.events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text('{"event": "read"}\n')
    assert wait_for(lambda: listed(ident)["state"] == "suspended", 15)
    assert "canary" in listed(ident).get("reason", "")
    ok(bridge_command("resume", ident))
    assert slowly(lambda: not a.call("send_message", {"text": "after the canary"})["isError"], 15)
    # The observer pulls the peer's kill switch: the peer's supervisor is asked (it listens for cuts: CUT_READY, as
    # a supervisor that runs writes it) and the bridge suspends. (These stand-in runs have no firewall to cut: their
    # supervisor refuses, and the observer is told so - the live check bridge-live cuts a real one.)
    (beta.run / b.module("net").CUT_READY).write_text("")
    cut = a.call("cut_network", {"reason": "it uploads the workspace"})
    assert cut["isError"] and "beta: NOT cut: the sandbox refused" in text(cut), text(cut)
    assert wait_for(lambda: listed(ident)["state"] == "suspended", 15)
    assert "cut the network of beta" in listed(ident).get("reason", "")
    alerts = json.loads(ok(sai("bridge", "show", ident, "--json"))).get("alerts") or []
    assert any(x.get("severity") == "critical" and "cut the network of beta" in x.get("text", "") for x in alerts)


def test_the_end_of_a_session_ends_the_broker_process(world, processes):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    data = added(processes, "session-alpha", "session-beta", "--allow", "message", "--ask", "never")
    record = json.loads((beta.run / "run.json").read_text())
    (beta.run / "run.json").write_text(json.dumps({**record, "pid": 999999999}))  # another launcher: beta ended
    assert wait_for(lambda: not alive(data["broker_pid"]), 15)
    row = listed(data["id"])
    assert row["state"] == "ended" and "has ended" in row.get("reason", "")
    assert alpha.relays.names() == [], "the other side's relay is closed"


def test_until_ends_the_broker_process_at_its_time(world, processes):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 62))
    data = added(processes, "session-alpha", "session-beta", "--allow", "message", "--ask", "never",
                 "--until", until)
    a = agent(processes, alpha, data["a"]["server"])
    assert not a.call("send_message", {"text": "in time"})["isError"]
    assert wait_for(lambda: not alive(data["broker_pid"]), 90)
    row = listed(data["id"])
    assert row["state"] == "ended" and "time ran out" in row.get("reason", "")
    assert alpha.relays.names() == [] and beta.relays.names() == []


# --- across hosts: the other hosts' halves in processes of their own, reached through a fake ssh --------------------

FAKE_SSH = r'''#!PYTHON -I
"""ssh for this test: `ssh [OPTIONS] -- USER@HOST.example COMMAND` runs COMMAND with sh as the host HOST would - this
machine with the folders of that host (its own state: bridges, approvals, bridge identity; the runs are this
machine's, which the test registered). No network, no port; every forwarding option ssh was given is said in the log."""
import json, os, sys
args = sys.argv[1:]
if "-O" in args or "-G" in args or "--" not in args:
    sys.exit(255)
at = args.index("--")
target, command = args[at + 1], " ".join(args[at + 2:])
host = target.split("@")[-1].split(".")[0]
with open(os.environ["SAI_E2E_SSH_LOG"], "a") as log:
    log.write(json.dumps({"host": host, "options": args[:at], "command": command}) + "\n")
home = os.path.join(os.environ["SAI_E2E_HOSTS"], host)
env = dict(os.environ, XDG_STATE_HOME=os.path.join(home, "state"), XDG_CONFIG_HOME=os.path.join(home, "config"))
os.execvpe("sh", ["sh", "-c", command], env)
'''

HOST_SAI = r'''#!PYTHON -I
"""The sandbox-ai of a fake host (FAKE_SSH): bridge and bridge-stub through DRIVER (this machine is a sandbox, and
has no user namespace), everything else the real ./sandbox-ai."""
import os, sys
args = sys.argv[1:]
root, driver = os.environ["SAI_E2E_ROOT"], os.environ["SAI_E2E_DRIVER"]
if args[:1] in (["bridge"], ["bridge-stub"]):
    rest = ["--stub", *args[1:]] if args[0] == "bridge-stub" else args[1:]
    os.execv(sys.executable, [sys.executable, "-I", driver, root, *rest])
os.execv(os.path.join(root, "sandbox-ai"), [os.path.join(root, "sandbox-ai"), *args])
'''


@pytest.fixture
def hosts(world, processes, tmp_path, monkeypatch):  # noqa: F811
    """host1 and host2 of `sandbox-ai hosts`, reached through FAKE_SSH (each this machine with folders of its own)."""
    folder = tmp_path / "fake-bin"
    folder.mkdir()
    for name, script in (("ssh", FAKE_SSH), ("host-sandbox-ai", HOST_SAI)):
        (folder / name).write_text(script.replace("PYTHON", sys.executable))
        (folder / name).chmod(0o755)
    driver = tmp_path / "bridge-driver.py"
    driver.write_text(DRIVER)
    for host in ("host1", "host2"):
        for sub in ("state", "config"):
            (tmp_path / "hosts" / host / sub).mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SANDBOX_AI_SSH_MULTIPLEX", "0")
    monkeypatch.setenv("SAI_E2E_ROOT", str(ROOT))
    monkeypatch.setenv("SAI_E2E_DRIVER", str(driver))
    monkeypatch.setenv("SAI_E2E_HOSTS", str(tmp_path / "hosts"))
    monkeypatch.setenv("SAI_E2E_SSH_LOG", str(tmp_path / "ssh.jsonl"))
    b.module("hosts").save({host: {"ssh": f"me@{host}.example", "port": 22, "command": str(folder / "host-sandbox-ai")}
                            for host in ("host1", "host2")})
    seen = set()

    def on(host, *args, timeout=60):
        """./sandbox-ai ARGS as HOST's human runs it there (its folders)."""
        env = {**environment(), "XDG_STATE_HOME": str(tmp_path / "hosts" / host / "state"),
               "XDG_CONFIG_HOME": str(tmp_path / "hosts" / host / "config")}
        return subprocess.run([str(SAI), *map(str, args)], capture_output=True, text=True, env=env, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def record(host, ident):
        return json.loads((tmp_path / "hosts" / host / "state/sandbox-ai/bridges" / ident / "bridge.json").read_text())

    def halves(host, ident):
        """The PID of HOST's half of bridge IDENT (a process of its own there), once it recorded itself."""
        found = {}
        assert wait_for(lambda: found.update(pid=record(host, ident).get("broker_pid")) or
                        isinstance(found["pid"], int), 20)
        if found["pid"] not in seen:
            seen.add(found["pid"])
            processes["brokers"].append(found["pid"])
        return found["pid"]

    def ssh_log():
        path = tmp_path / "ssh.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return tb.SimpleNamespace(on=on, record=record, halves=halves, ssh_log=ssh_log)


def test_a_session_of_another_host_through_real_halves(world, processes, hosts):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    (beta.folder / "out").mkdir()
    data = added(processes, "session-alpha", "host1:session-beta", "--allow", "message,files:rw:out", "--ask",
                 "never")
    ident = data["id"]
    far = hosts.halves("host1", ident)
    assert far != data["broker_pid"] and alive(far), "host1's half is a process of its own"
    stub = next(line for line in hosts.ssh_log() if "bridge-stub" in line["command"])
    assert stub["host"] == "host1" and "ClearAllForwardings=yes" in " ".join(stub["options"])
    assert hosts.record("host1", ident)["remote_half"] is True
    a = agent(processes, alpha, data["a"]["server"])
    assert beta.relays.names(), "host1's half attached beta there"
    bb = agent(processes, beta, beta.relays.names()[0])
    assert not a.call("send_message", {"text": "across"})["isError"]
    assert "across" in text(bb.call("inbox", {"wait_seconds": 10}))
    # The write is asked on host1, where the files are: its human answers there.
    thread, wrote = in_thread(a.call, "write_file", {"path": "out/far.txt", "content": "over ssh\n"}, timeout=90)
    found = {}
    assert wait_for(lambda: found.update(rows=json.loads(ok(hosts.on("host1", "approvals", "list", "--json")))) or
                    bool(found["rows"]), 30)
    assert not json.loads(ok(sai("approvals", "list", "--json"))), "nothing asked here about host1's files"
    ok(hosts.on("host1", "approvals", "allow", found["rows"][0]["id"]))
    thread.join(60)
    assert not wrote["value"]["isError"], text(wrote["value"])
    assert (beta.folder / "out" / "far.txt").read_text() == "over ssh\n"
    # A revoke here ends both halves.
    ok(bridge_command("revoke", ident))
    assert wait_for(lambda: not alive(data["broker_pid"]) and not alive(far), 30)
    assert hosts.record("host1", ident)["state"] in ("revoked", "ended")
    assert wait_for(lambda: beta.relays.names() == [] and alpha.relays.names() == [], 15)


def test_two_sessions_of_one_other_host_are_bridged_there(world, processes, hosts):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    plan = json.loads(ok(bridge_command("add", "host1:session-alpha", "host1:session-beta", "--allow", "message",
                                        "--ask", "never", "--json")))
    assert plan.get("host") == "host1"
    ident = plan["id"]
    there = hosts.halves("host1", ident)
    assert hosts.record("host1", ident).get("remote_half") is not True, "host1's own bridge"
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "sandbox-ai/bridges" / ident).exists(), "nothing made here"
    rows = json.loads(ok(sai("bridge", "list", "--hosts", "--json")))
    assert any(row["id"] == ident and row.get("host") == "host1" for row in rows), rows
    a = agent(processes, alpha, alpha.relays.names()[0])
    bb = agent(processes, beta, beta.relays.names()[0])
    assert not a.call("send_message", {"text": "both on host1"})["isError"]
    assert "both on host1" in text(bb.call("inbox", {"wait_seconds": 10}))
    ok(bridge_command("revoke", f"host1:{ident}"))
    assert wait_for(lambda: not alive(there), 20)
    assert hosts.record("host1", ident)["state"] == "revoked"


def test_two_sessions_of_two_hosts_are_relayed_by_this_machine(world, processes, hosts):  # noqa: F811
    alpha, beta = world.make("alpha"), world.make("beta")
    data = added(processes, "host1:session-alpha", "host2:session-beta", "--allow", "message,ask", "--ask", "never")
    ident = data["id"]
    one, two = hosts.halves("host1", ident), hosts.halves("host2", ident)
    assert len({one, two, data["broker_pid"]}) == 3, "the relay here and a half on each host"
    assert hosts.record("host1", ident)["relayed_by"] and hosts.record("host2", ident)["relayed_by"]
    a = agent(processes, alpha, alpha.relays.names()[0])
    bb = agent(processes, beta, beta.relays.names()[0])
    assert not a.call("send_message", {"text": "relayed"})["isError"]
    assert "relayed" in text(bb.call("inbox", {"wait_seconds": 10}))
    thread, asked = in_thread(a.call, "ask", {"question": "ready?", "timeout_seconds": 30}, timeout=60)
    question = {}
    assert wait_for(lambda: question.update(text=text(bb.call("inbox", {"wait_seconds": 2}))) or
                    "ready?" in question["text"], 20)
    number = re.search(r"question ([0-9a-f]{8}) \(", question["text"]).group(1)
    assert not bb.call("answer", {"question_id": number, "text": "yes"})["isError"]
    thread.join(30)
    assert "yes" in text(asked["value"])
    ok(bridge_command("revoke", ident))
    assert wait_for(lambda: not any(alive(pid) for pid in (one, two, data["broker_pid"])), 30)
    assert wait_for(lambda: alpha.relays.names() == [] and beta.relays.names() == [], 15)
