"""sandbox-ai net against a real control socket (the supervisor's control_server, firewall mocked)."""
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_net", ROOT / "sandbox/net.py")
net = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(net)
rt = net.launcher().RUNTIME


@pytest.fixture(autouse=True)
def no_login_runtime(tmp_path, monkeypatch):
    """The runs of a real login session (/run/user/UID) stay out of these tests. PIDs are judged as on the host
    (these tests may run inside a sandbox, where nothing is removed: liveness.py), and Podman is never asked."""
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(net.liveness(), "inside_sandbox", lambda *a, **k: False)

    def no_podman(*args, **kwargs):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(net.liveness(), "RUNNER", no_podman)
    net.liveness()._cache.clear()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A registered run whose supervisor is a real control_server thread (in a short directory: the
    supervisor binds there, and socket paths are limited to 107 bytes)."""
    short = Path(tempfile.mkdtemp(prefix="net-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    session = tmp_path / "session"
    session.mkdir()
    (session / "hosts").write_text("127.0.0.1 localhost\n192.0.2.1 api.openai.com\n")
    run = net.launcher().runs_dir() / "codex-1234abcd"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run": "codex-1234abcd", "agent": "codex", "pid": os.getpid(),
                                              "workspace": str(tmp_path / "app"), "session_dir": str(session),
                                              "started": "now", "session": None}))
    applied, stop = [], threading.Event()
    allow = tmp_path / "allow.json"
    network = rt.RuntimeNetwork({"loopback": []}, allow, applied.append)
    thread = threading.Thread(target=rt.control_server, args=(run / "control.sock", network, stop), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (run / "control.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    yield {"run": run, "hosts": session / "hosts", "applied": applied, "allow": allow, "network": network, "inode": (session / "hosts").stat().st_ino}
    stop.set()
    thread.join(5)
    shutil.rmtree(short, ignore_errors=True)


def fake_resolver(hosts):
    return {host: ["192.0.2.50"] for host in hosts}


def run_net(argv):
    lines = []
    status = net.main(argv, out=lines.append, resolver=fake_resolver)
    return status, lines


def test_list_and_show(sandbox, tmp_path):
    status, lines = run_net(["list"])
    assert status == 0 and lines[0].split() == ["RUN", "NAME", "AGENT", "SINCE", "WORKSPACE"]
    assert lines[1].split() == ["codex-1234abcd", "-", "codex", "now", str(tmp_path / "app")]
    status, lines = run_net(["show"])
    assert status == 0 and "allowed during this run" in lines[0] and "0 destinations" in lines[0]
    assert "nothing beyond its start options" in lines[1]
    status, lines = run_net(["list", "--json"])
    assert json.loads("\n".join(lines))[0]["run"] == "codex-1234abcd"
    assert json.loads("\n".join(lines))[0]["name"] is None  # registered by an older version: no name
    status, lines = run_net(["show", "--json"])
    assert json.loads("\n".join(lines)) == {"run": "codex-1234abcd", "all_ports": [], "dns": [], "git": [], "host_ports": [],
                                            "https": [], "tcp": [], "host_wildcards": []}


def test_allow_host_reaches_firewall_proxy_and_hosts_in_place(sandbox):
    status, lines = run_net(["allow", "--allow-host", "example.org", "--allow-tcp", "192.0.2.9:5432"])
    assert status == 0
    text = "\n".join(lines)
    # Grouped by kind; the HTTPS address under the name it was allowed for, the TCP one with its port.
    assert any(line.split()[:4] == ["example.org", "1", "0", "192.0.2.50"] for line in lines), text
    assert any(line.split()[:2] == ["192.0.2.9", "5432"] for line in lines), text
    assert "/32" not in text and "start with: --allow-host example.org --allow-tcp 192.0.2.9:5432" in lines[-1]
    status, lines = run_net(["show", "--json"])
    assert json.loads("\n".join(lines))["https"] == [{"address": "192.0.2.50", "names": ["example.org"]}]
    assert "tcp dport 443 accept" in sandbox["applied"][-1]
    assert json.loads(sandbox["allow"].read_text())["https"] == ["192.0.2.50/32"]
    # Same file (the sandbox has it bound), the new name added, the old ones kept.
    assert sandbox["hosts"].stat().st_ino == sandbox["inode"]
    assert sandbox["hosts"].read_text().splitlines() == ["127.0.0.1 localhost", "192.0.2.1 api.openai.com",
                                                         "192.0.2.50 example.org"]
    status, lines = run_net(["revoke", "--allow-host", "example.org"])
    assert status == 0 and "192.0.2.50" not in "\n".join(lines)
    assert "example.org" not in sandbox["hosts"].read_text() and "api.openai.com" in sandbox["hosts"].read_text()


def test_refusals(sandbox, capsys):
    assert run_net(["allow"])[0] == 1
    assert "nothing to allow" in capsys.readouterr().err
    assert run_net(["allow", "--allow-host", "bad host"])[0] == 1
    assert run_net(["allow", "--allow", "10.0.0.0/8; flush ruleset"])[0] == 1
    assert run_net(["revoke", "--allow-host", "never.example.org"])[0] == 1
    assert "was not allowed during this run" in capsys.readouterr().err
    assert run_net(["show", "other-run"])[0] == 1
    assert sandbox["applied"] == []


def test_allow_inside_the_sandbox_subnet_is_refused(sandbox, capsys):
    """A run with --subnet: destinations inside the sandbox's own network would reach its gateway or DNS
    forwarder, so `allow` refuses them as the launcher does at the start; elsewhere it works as before."""
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "subnets": ["10.20.0.0/24", "fd20::/64"]}))
    for options in (["--allow-tcp", "10.20.0.2:22"], ["--allow", "10.0.0.0/8"], ["--allow-tcp", "[fd20::3]:53"]):
        assert run_net(["allow", *options])[0] == 1, options
        assert "lies inside the sandbox's own network" in capsys.readouterr().err
    resolver = lambda hosts: {host: ["10.20.0.7"] for host in hosts}  # noqa: E731
    assert net.main(["allow", "--allow-host", "inside.example"], out=lambda line: None, resolver=resolver) == 1
    assert sandbox["applied"] == [] and "inside.example" not in sandbox["hosts"].read_text()
    assert run_net(["allow", "--allow-tcp", "10.21.0.2:22"])[0] == 0
    assert run_net(["revoke", "--allow-tcp", "10.21.0.2:22"])[0] == 0


def test_choose_and_stale_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runs = net.launcher().runs_dir()
    for name, pid, workspace in (("a", os.getpid(), "/w/a"), ("b", os.getpid(), "/w/b"), ("dead", 2 ** 22 + 7, "/w/c")):
        (runs / name).mkdir(parents=True)
        (runs / name / "run.json").write_text(json.dumps({"run": name, "pid": pid, "workspace": workspace}))
    found = net.running()
    assert sorted(found) == ["a", "b"] and not (runs / "dead").exists()
    assert net.choose(found, None, cwd="/w/b")["run"] == "b"
    with pytest.raises(ValueError, match="several sandboxes"):
        net.choose(found, None, cwd="/elsewhere")
    with pytest.raises(net.NotRunning, match="no start named zzz; running: a, b"):
        net.choose(found, "zzz")


def test_choose_by_name(tmp_path):
    """A run is named by its id or by the name of its start (sandbox-ai list)."""
    runs = {"claude-1": {"run": "claude-1", "name": "review"}, "codex-2": {"run": "codex-2", "name": "twin"},
            "codex-3": {"run": "codex-3", "name": "twin"}}
    assert net.choose(runs, "review")["run"] == "claude-1"
    assert net.choose(runs, "codex-2")["run"] == "codex-2"  # the id wins
    with pytest.raises(ValueError, match="several running sandboxes are named twin; name one by its run id: codex-2, "
                                         "codex-3"):
        net.choose(runs, "twin")
    with pytest.raises(ValueError, match=r"no start named x\\x1b; running: review, twin \(run codex-2\)"):
        net.choose(runs, "x\x1b")  # escaped in the message


def test_revoke_keeps_start_entries_and_names_sharing_an_address(sandbox, monkeypatch):
    # api.openai.com was pinned at the start; allowing and revoking it during the run leaves that entry.
    lines = []
    assert net.main(["allow", "-A", "api.openai.com"], out=lines.append, resolver=lambda hosts: {h: ["192.0.2.1"] for h in hosts}) == 0
    assert net.main(["revoke", "-A", "api.openai.com"], out=lines.append) == 0
    assert "192.0.2.1 api.openai.com" in sandbox["hosts"].read_text()
    # Two names on one address (a CDN): revoking one keeps the address for the other.
    shared = lambda hosts: {h: ["192.0.2.60"] for h in hosts}
    assert net.main(["allow", "-A", "a.example.org", "-A", "b.example.org"], out=lines.append, resolver=shared) == 0
    assert net.main(["revoke", "-A", "a.example.org"], out=lines.append) == 0
    assert json.loads(sandbox["allow"].read_text())["https"] == ["192.0.2.60/32"]
    assert "b.example.org" in sandbox["hosts"].read_text() and "a.example.org" not in sandbox["hosts"].read_text()


# --- runs registered elsewhere, net stop ---------------------------------------------------------------

def register(directory, name, **fields):
    (directory / name).mkdir(parents=True)
    record = {"run": name, "agent": "claude", "workspace": "/w", "session": None, **fields}
    (directory / name / "run.json").write_text(json.dumps(record))
    return directory / name


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@pytest.fixture
def child():
    """Start harmless processes; those with LAUNCHER have an argument ending in agent_sandbox.py, like a launcher."""
    started = []

    def start(launcher=True):
        extra = [str(ROOT / "sandbox/agent_sandbox.py"), "claude"] if launcher else []
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *extra])
        started.append(process)
        return process
    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_running_finds_runs_registered_under_another_runtime_dir(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    state = Path(os.environ["XDG_STATE_HOME"]) / "sandbox-ai" / "runs"
    user = tmp_path / "run-user" / str(os.getuid())
    register(state, "claude-1", pid=os.getpid(), workspace="/w/state")
    register(state, "both", pid=os.getpid(), workspace="/w/state")
    register(net.launcher().runs_dir(), "both", pid=os.getpid(), workspace="/w/runtime")
    register(user / "sandbox-ai" / "runs", "codex-2", pid=os.getpid())
    register(user / "sandbox-ai" / "runs", "dead", pid=2 ** 22 + 7)
    found = net.running()
    assert set(found) == {"claude-1", "both", "codex-2"}
    assert found["both"]["workspace"] == "/w/runtime"  # launcher().runs_dir() first
    assert found["claude-1"]["directory"] == str(state / "claude-1")
    assert not (user / "sandbox-ai" / "runs" / "dead").exists()
    # Without XDG_RUNTIME_DIR the launcher registers in the state directory; the login session's is still read.
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert net.run_dirs() == [state, user / "sandbox-ai" / "runs"]
    assert set(net.running()) == {"claude-1", "both", "codex-2"}
    # One directory reached under two names is read once.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(user))
    assert net.run_dirs() == [user / "sandbox-ai" / "runs", state]


def test_register_run_records_the_start_time_of_its_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))  # beside the workspace: never around it
    l = net.launcher()
    a = l.parse_args(["claude", "--workspace", str(tmp_path / "app")])
    l.validate(a)
    record = json.loads((l.register_run(a, "claude-1234abcd", tmp_path / "session") / "run.json").read_text())
    assert record["pid"] == os.getpid() and record["pid_start"] == l.process_start(os.getpid())


def test_stop_ends_a_one_off_sandbox(child, capsys):
    process = child()
    runs = net.launcher().runs_dir()
    register(runs, "claude-0badc0de", pid=process.pid, pid_start=net.launcher().process_start(process.pid),
             started=now())
    lines = []
    assert net.main(["stop", "claude-0badc0de", "--wait", "10"], out=lines.append) == 0
    assert process.wait(5) == -signal.SIGTERM
    assert lines == [f"claude-0badc0de: stopped (its launcher, PID {process.pid}, cleaned up and ended)"]
    # Gone now; RUN is required (no guessing).
    assert net.main(["stop", "claude-0badc0de"], out=lines.append) == 1
    assert "no start named claude-0badc0de" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_:
        net.main(["stop"])
    assert exit_.value.code == 2


def test_stop_of_an_old_record_checks_the_start_time(child):
    """Records of older versions have no pid_start: the process must have started before the registration."""
    process = child()
    record = {"run": "claude-1", "pid": process.pid, "started": now(), "session": None}
    assert net.launcher_process(record)[0] == process.pid
    with pytest.raises(ValueError, match="started after claude-1 registered"):
        net.launcher_process(dict(record, started="2020-01-01T00:00:00Z"))
    with pytest.raises(ValueError, match="no start time"):
        net.launcher_process(dict(record, started=None))
    lines = []
    # Asked (or, on a busy machine, already ended by the time stop looks again): signalled either way.
    assert net.stop(record, 0, out=lines.append) == 0
    assert "asked its launcher" in lines[0] or "cleaned up and ended" in lines[0], lines
    assert process.wait(5) == -signal.SIGTERM


def test_stop_refusals(child, monkeypatch, capsys):
    process, other = child(), child(launcher=False)
    start = net.launcher().process_start(process.pid)
    runs = net.launcher().runs_dir()
    register(runs, "claude-reused", pid=process.pid, pid_start=start + 1, started=now())
    register(runs, "claude-other", pid=other.pid, pid_start=net.launcher().process_start(other.pid), started=now())
    register(runs, "web", pid=process.pid, pid_start=start, started=now(), session="web")
    register(runs, "mcp-ddg", pid=process.pid, pid_start=start, started=now())
    # A PID given out again after the run ended (another start time) is no running sandbox at all any more: its
    # stale entry goes, so nothing is ever signalled (net.alive).
    expected = {"claude-reused": "no start named claude-reused",
                "claude-other": "does not run a sandbox-ai launcher",
                "web": "use: sandbox-ai stop web",
                "mcp-ddg": "use: sandbox-ai mcp stop ddg"}
    for name, message in expected.items():
        assert net.main(["stop", name, "--wait", "0"], out=lambda line: None) == 1
        assert message in capsys.readouterr().err
    record = {"run": "claude-1", "pid": process.pid, "pid_start": start, "started": now()}
    with monkeypatch.context() as patched:
        patched.setattr(net.os, "getuid", lambda: os.geteuid() + 4242)
        with pytest.raises(ValueError, match="belongs to another user"):
            net.launcher_process(record)
    for pid in (None, True, 1, "12"):
        with pytest.raises(ValueError, match="no usable PID"):
            net.launcher_process(dict(record, pid=pid))
    with pytest.raises(SystemExit):
        net.main(["stop", "claude-1", "--wait", "-1"])
    time.sleep(0.2)
    assert process.poll() is None and other.poll() is None  # nothing was signalled


def mode_record(name):
    """The security mode of a run as the launcher writes it to run.json (and, when strict, config.json)."""
    l = net.launcher()
    argv = ["codex", "--mode", name, "--no-config"]
    a = l.MODES.apply(l.launcher_namespace(), argv, l.parse_args(argv), None, None, {}, {})
    return l.MODES.runtime_record(l.launcher_namespace(), a)


def test_mode_records():
    presets = [f"--allow-{p}" for p in net.launcher().PRESETS]
    for name in ("high", "paranoid"):
        record = mode_record(name)
        assert record["refuses"] == ["--allow", "--allow-host", "--allow-tcp", "--dns", "--allow-host-port", *presets]
        assert record["origin"] == "--mode" and record["approval_scope"] == {"high": "run", "paranoid": "once"}[name]
    for name in ("lax", "low", "medium"):
        assert mode_record(name)["refuses"] == [] and mode_record(name)["approval_scope"] == "always"


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_allow_keeps_to_the_mode_of_the_run(sandbox, capsys, mode):
    """A run started in a strict mode: `sandbox-ai net allow` opens nothing the mode refuses at the start."""
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "mode": mode_record(mode)}))
    for options, named in ((["--allow-host", "example.org"], "--allow-host"), (["--allow-github"], "--allow-github"),
                           (["--allow-tcp", "192.0.2.9:5432"], "--allow-tcp"), (["--allow", "192.0.2.0/24"], "--allow"),
                           (["--dns"], "--dns"), (["--nameserver", "9.9.9.9"], "--dns")):
        assert run_net(["allow", *options])[0] == 1, options
        err = capsys.readouterr().err
        assert f"{named} widens the security mode {mode} of codex-1234abcd (--mode)" in err
        assert "answer its questions instead" in err and "--mode medium" in err
    assert sandbox["applied"] == [] and "example.org" not in sandbox["hosts"].read_text()
    assert run_net(["show"])[0] == 0
    # A run of a laxer mode (or one without a recorded mode, from before modes) is changed as before.
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "mode": mode_record("low")}))
    assert run_net(["allow", "--allow-tcp", "192.0.2.9:5432"])[0] == 0


def test_a_forged_control_request_meets_the_mode(tmp_path):
    """The supervisor checks the mode itself: a request written to the control socket by hand, past
    `sandbox-ai net`, is refused the same way; revoke and show still work."""
    short = Path(tempfile.mkdtemp(prefix="net-", dir="/tmp"))
    applied, stop = [], threading.Event()
    network = rt.RuntimeNetwork({"loopback": []}, tmp_path / "allow.json", applied.append)
    thread = threading.Thread(target=rt.control_server, args=(short / "control.sock", network, stop, [],
                                                              mode_record("high")), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not (short / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        for field, value, option in (("https", ["203.0.113.5/32"], "--allow-host"), ("tcp", [["192.0.2.9", 22]], "--allow-tcp"),
                                     ("nets", ["0.0.0.0/0"], "--allow")):
            with pytest.raises(ValueError, match=f"refused: {option} widens the security mode high of this run"):
                net.ask(short, {"op": "allow", "nets": [], "https": [], "tcp": [], field: value})
        assert applied == []
        assert net.ask(short, {"op": "show"})["https"] == []
        assert net.ask(short, {"op": "revoke", "nets": [], "https": ["203.0.113.5/32"], "tcp": []})["ok"]
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(short, ignore_errors=True)
    assert rt.mode_refusal(None, {"op": "allow", "https": ["203.0.113.5/32"]}) is None
    assert rt.mode_refusal(mode_record("lax"), {"op": "allow", "https": ["203.0.113.5/32"]}) is None
    assert rt.narrowest_scope("always", "run") == "run" and rt.narrowest_scope("run", "once") == "once"
    assert rt.narrowest_scope("always", "bogus") == "once"



# --- DNS during the run (sandbox-ai net allow --dns) ----------------------------------------------------------

def test_allow_dns_opens_port_53_of_the_nameservers_only(sandbox):
    status, lines = run_net(["allow", "--dns"])
    assert status == 0, lines
    rules = sandbox["applied"][-1]
    for server in net.launcher().DEFAULT_NAMESERVERS:
        assert f"ip daddr {server} udp dport 53 accept" in rules and f"ip daddr {server} tcp dport 53 accept" in rules
    assert "dport 443" not in rules, "names resolve; their addresses stay closed"
    allow = json.loads(sandbox["allow"].read_text())
    assert ["1.1.1.1", 53] in allow["tcp"], "DNS over TCP passes the proxy to them"
    assert "--dns" in lines[-1] and "DNS (port 53)" in "\n".join(lines)
    status, lines = run_net(["show", "--json"])
    assert json.loads("\n".join(lines))["dns"] == sorted(net.launcher().DEFAULT_NAMESERVERS)
    assert run_net(["allow", "--nameserver", "9.9.9.9", "--nameserver", "2620:fe::fe"])[0] == 0
    assert "ip6 daddr 2620:fe::fe udp dport 53 accept" in sandbox["applied"][-1]
    assert run_net(["revoke", "--dns"])[0] == 0
    rules = sandbox["applied"][-1]
    assert "1.1.1.1" not in rules and "9.9.9.9 udp dport 53" in rules
    assert run_net(["revoke", "--nameserver", "9.9.9.9", "--nameserver", "2620:fe::fe"])[0] == 0
    assert "dport 53" not in sandbox["applied"][-1]


def test_dns_refusals(sandbox, capsys):
    for options, message in ((["--nameserver", "dns.google"], "an IP address"),
                             (["--nameserver", "fe80::1%eth0"], "scope ID"), ([], "--dns")):
        assert run_net(["allow", *options])[0] == 1
        assert message in capsys.readouterr().err
    assert sandbox["applied"] == []
    with pytest.raises(ValueError):
        rt.runtime_change({"nets": set(), "https": set(), "tcp": set()}, {"op": "allow", "dns": ["1.1.1.1"] * 9})
    network = rt.RuntimeNetwork({}, None, lambda script: None)
    network.dns_stub = False
    with pytest.raises(ValueError, match="no DNS stub"):
        network.change({"op": "allow", "dns": ["1.1.1.1"]})
    network = rt.RuntimeNetwork({}, None, lambda script: None, within=["10.0.0.0/8"])
    with pytest.raises(ValueError, match="outside the networks"):
        network.change({"op": "allow", "dns": ["1.1.1.1"]})
    assert network.change({"op": "allow", "dns": ["10.0.0.53"]})["dns"] == ["10.0.0.53"]


def test_the_policy_forbids_dns_at_run_time(tmp_path):
    short = Path(tempfile.mkdtemp(prefix="net-", dir="/tmp"))
    network = rt.RuntimeNetwork({}, None, lambda script: None)
    stop = threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(short / "control.sock", network, stop, ["--nameserver"]),
                              daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not (short / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with pytest.raises(ValueError, match="not allowed by the administrator's policy"):
            net.ask(short, {"op": "allow", "nets": [], "https": [], "tcp": [], "dns": ["1.1.1.1"]})
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(short, ignore_errors=True)


def query(name, ident=b"\x12\x34"):
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split("."))
    return ident + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + labels + b"\x00\x00\x01\x00\x01"


def test_the_dns_stub_forwards_what_was_allowed_and_logs_it(tmp_path, monkeypatch):
    """The stub in the sandbox: pinned names stay answered by /etc/hosts (NXDOMAIN here), other names go to
    the allowed nameserver once DNS was allowed, each one logged; a name carrying a canary token never leaves."""
    import socket
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    asked = []

    def answer():
        while True:
            try:
                data, peer = upstream.recvfrom(4096)
            except OSError:
                return
            asked.append(data)
            try:
                upstream.sendto(data[:2] + b"\x81\x80" + data[4:] + b"ANSWER", peer)
            except OSError:
                return
    threading.Thread(target=answer, daemon=True).start()
    stub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stub.bind(("127.0.0.1", 0))
    port = stub.getsockname()[1]
    stub.close()
    servers = []

    class Canaries:
        def lookup(self, name, found=None):
            return {"canary": "aws"} if "akiaexample" in name else None
    stop = threading.Event()
    thread = threading.Thread(target=rt.dns_logger, args=({"api.openai.com"}, port, stop, None, (), None, Canaries(),
                                                         lambda: list(servers), upstream.getsockname()[1]), daemon=True)
    thread.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # The stub waits up to 2 s for a nameserver that does not answer; a query sent before it is bound is lost
    # (an unconnected UDP socket gets no "refused"), so each try waits 3 s, not longer.
    client.settimeout(3)

    def ask_stub(name):
        for _ in range(50):
            try:
                client.sendto(query(name), ("127.0.0.1", port))
                return client.recvfrom(4096)[0]
            except (ConnectionRefusedError, socket.timeout):
                time.sleep(0.05)
        raise AssertionError("the stub did not answer")
    try:
        assert ask_stub("example.org")[3] & 0x0F == 3, "NXDOMAIN while DNS is not allowed"
        servers.append("127.0.0.1")
        assert ask_stub("example.org").endswith(b"ANSWER"), "forwarded"
        assert ask_stub("api.openai.com")[3] & 0x0F == 3 and len(asked) == 1, "pinned names stay in /etc/hosts"
        assert ask_stub("akiaexample123.evil.example")[3] & 0x0F == 3 and len(asked) == 1, "a canary never leaves"
        logged = [json.loads(line) for line in (tmp_path / "dns.jsonl").read_text().splitlines()]
        assert [(r["host"], r["forwarded"]) for r in logged] == [("example.org", "127.0.0.1")]
        servers[:] = ["127.0.0.2"]  # nobody answers there (the stand-in listens on 127.0.0.1 only)
        assert ask_stub("second.example")[3] & 0x0F == 3, "no nameserver answered: NXDOMAIN"
        assert json.loads((tmp_path / "dns.jsonl").read_text().splitlines()[-1])["error"] == "no nameserver answered"
    finally:
        stop.set()
        thread.join(5)
        client.close()
        upstream.close()


# --- Git logins during the run (sandbox-ai net allow --git-token-*) --------------------------------------------

TOKEN = "gitea_0123456789abcdef"


def git_ready(sandbox, **fields):
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "runtime_git_tokens": True, **fields}))
    return Path(record["session_dir"]) / net.launcher().RUNTIME_GIT_TOKENS


def git_fill(tokens, url, extra_config=()):
    """`git credential fill` for URL with the sandbox's helper reading TOKENS, as the launcher configures it
    (GIT_CONFIG_* variables, the helper first): what Git would send."""
    helper = ("!python3 -I -c 'import importlib.util, sys; spec = importlib.util.spec_from_file_location(\"rt\", "
              f"\"{ROOT / 'sandbox/runtime.py'}\"); rt = importlib.util.module_from_spec(spec); "
              f"spec.loader.exec_module(rt); sys.exit(rt.git_credential(sys.argv[1], path=\"{tokens}\"))'")
    entries = [("credential.helper", helper), *extra_config]
    env = {"PATH": os.environ["PATH"], "HOME": str(tokens.parent), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
    result = subprocess.run(["git", "credential", "fill"], input=f"url={url}\n\n", text=True, capture_output=True,
                            env=env, timeout=30)
    return result.returncode, dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_allow_git_token_gives_the_running_sandbox_a_login(sandbox, monkeypatch, capsys):
    tokens = git_ready(sandbox)
    monkeypatch.setenv("GITEA_TOKEN", TOKEN)
    status, lines = run_net(["allow", "--git-token-env", "http://localhost:3080=GITEA_TOKEN"])
    text = "\n".join(lines)
    assert status == 0, capsys.readouterr().err
    # Listed by server and user, never the token; a loopback server needs its port forwarded from the start.
    assert "http://localhost:3080" in text and "x-access-token" in text and TOKEN not in text
    assert "--allow-host-port 3080" in text and "start with: --git-token-env http://localhost:3080=GITEA_TOKEN" in text
    assert tokens.stat().st_mode & 0o777 == 0o600
    assert json.loads(tokens.read_text())["logins"] == [{"url": "http://localhost:3080", "user": "x-access-token",
                                                         "token": TOKEN}]
    assert sandbox["applied"] == []  # nothing opened: the loopback is not the firewall's
    status, lines = run_net(["show", "--json"])
    assert json.loads("\n".join(lines))["git"] == [{"url": "http://localhost:3080", "user": "x-access-token"}]
    # Git in the sandbox: the helper answers for that server (path and default ports do not matter) and no other.
    assert git_fill(tokens, "http://localhost:3080/ch/repo.git") == \
        (0, {"protocol": "http", "host": "localhost:3080", "username": "x-access-token",
             "password": TOKEN})
    assert git_fill(tokens, "http://localhost:3001/repo.git")[0] != 0  # no login: Git gives up (no prompt)
    assert git_fill(tokens, "http://other@localhost:3080/repo.git")[0] != 0  # another user named in the URL
    # A login given at the start (its own helper after the empty value) wins over the run's.
    start = [("credential.http://localhost:3080.helper", ""),
             ("credential.http://localhost:3080.helper", "!f() { echo username=start; echo password=start-token; }; f")]
    assert git_fill(tokens, "http://localhost:3080/repo.git", start)[1]["password"] == "start-token"
    # Revoked: the file keeps the others; a second revoke says there is none.
    status, lines = run_net(["revoke", "--git-token-env", "http://localhost:3080=ANY"])
    assert status == 0 and json.loads(tokens.read_text())["logins"] == []
    assert git_fill(tokens, "http://localhost:3080/repo.git")[0] != 0
    assert run_net(["revoke", "--git-token-env", "http://localhost:3080=ANY"])[0] == 1
    assert "no Git login for http://localhost:3080" in capsys.readouterr().err


def test_allow_git_token_for_github_opens_its_hosts(sandbox, monkeypatch, tmp_path):
    tokens = git_ready(sandbox)
    token_file = tmp_path / "gh-token"
    token_file.write_text("ghp_" + "a" * 36 + "\n")
    token_file.chmod(0o600)
    status, lines = run_net(["allow", "--git-token-file", f"https://github.com={token_file}"])
    assert status == 0
    # As at the start: a token for github.com opens the GitHub preset (pinned names, TCP/443).
    assert "tcp dport 443 accept" in sandbox["applied"][-1]
    assert "github.com" in sandbox["hosts"].read_text()
    assert json.loads(tokens.read_text())["logins"][0]["token"] == "ghp_" + "a" * 36
    # Another login is added beside it; the same server again replaces its token.
    monkeypatch.setenv("OTHER", "glpat-" + "b" * 20)
    assert run_net(["allow", "--git-token-env", "https://git.example.org=OTHER"])[0] == 0
    assert [entry["url"] for entry in json.loads(tokens.read_text())["logins"]] == \
        ["https://github.com", "https://git.example.org"]
    assert "git.example.org" in sandbox["hosts"].read_text()
    monkeypatch.setenv("OTHER", "glpat-" + "c" * 20)
    assert run_net(["allow", "--git-token-env", "https://git.example.org=OTHER"])[0] == 0
    logins = json.loads(tokens.read_text())["logins"]
    assert len(logins) == 2 and logins[-1]["token"] == "glpat-" + "c" * 20


def test_allow_git_token_refusals(sandbox, monkeypatch, capsys):
    tokens = git_ready(sandbox, runtime_git_tokens=False)
    monkeypatch.setenv("GITEA_TOKEN", TOKEN)
    # A run started with --token-proxy or by an older sandbox-ai has no helper to read it.
    assert run_net(["allow", "--git-token-env", "http://localhost:3080=GITEA_TOKEN"])[0] == 1
    assert "takes no Git login during the run" in capsys.readouterr().err
    git_ready(sandbox)
    for argv, message in (
            (["--git-token-env", "http://localhost:3080=UNSET_VARIABLE"], "Host variable is not set: UNSET_VARIABLE"),
            (["--git-token-env", "http://git.example.org=GITEA_TOKEN"], "plain http would send the token"),
            (["--git-token-env", "https://git.example.org/path=GITEA_TOKEN"], "without a path"),
            (["--git-token-env", "https://a.example=GITEA_TOKEN", "--git-token-env", "https://a.example=OTHER"],
             "given twice"),
            (["--git-token-secret", "https://a.example=NO_SUCH_SECRET_HERE"], "no secret NO_SUCH_SECRET_HERE")):
        assert run_net(["allow", *argv])[0] == 1, argv
        assert message in capsys.readouterr().err, argv
    monkeypatch.setenv("BAD", "two words")
    assert run_net(["allow", "--git-token-env", "https://a.example=BAD"])[0] == 1
    assert "does not look like an access token" in capsys.readouterr().err
    assert not tokens.exists() and sandbox["applied"] == []  # nothing written, nothing opened
    # The administrator's policy forbids the option at the start: then during the run too.
    monkeypatch.setattr(net.launcher(), "load_policy", lambda path=None: {"forbid": ["--git-token-env"]})
    assert run_net(["allow", "--git-token-env", "http://localhost:3080=GITEA_TOKEN"])[0] == 1
    assert "forbidden by the administrator's policy" in capsys.readouterr().err
    assert not tokens.exists()


def test_allow_git_token_keeps_to_the_mode_of_the_run(sandbox, monkeypatch, capsys):
    """A strict mode refuses the GitHub preset at the start; a GitHub token during the run opens it, so it is
    refused as well (no login written)."""
    tokens = git_ready(sandbox, mode=mode_record("high"))
    monkeypatch.setenv("GH", "ghp_" + "a" * 36)
    assert run_net(["allow", "--git-token-env", "https://github.com=GH"])[0] == 1
    assert "widens the security mode" in capsys.readouterr().err
    assert not tokens.exists() and sandbox["applied"] == []


# --- host ports relayed during the run (sandbox-ai net allow --allow-host-port) --------------------------------

@pytest.fixture
def host_service():
    """A TCP service on the host's loopback that answers each line with "host:" and the line."""
    server = __import__("socket").socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    stop = threading.Event()

    def serve():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                continue
            with connection:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = connection.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                connection.sendall(b"host:" + data)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield port
    stop.set()
    thread.join(2)
    server.close()


def relay_ready(sandbox, monkeypatch, **fields):
    """The run of SANDBOX able to relay: its supervisor's relays listen on free ports of their own (the host's port
    and the sandbox's share this machine's loopback in a test), its launcher is this test process."""
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "host_relay": True, **fields}))
    network = sandbox["network"]
    network.relay_dir = sandbox["run"]
    listening = {}

    def factory(port, directory):
        free = __import__("socket").socket()
        free.bind(("127.0.0.1", 0))
        listen = free.getsockname()[1]
        free.close()
        listening[port] = listen
        return rt.PortRelay(port, directory, listen_port=listen)
    network.relay_factory = factory
    own = (os.getpid(), net.process_state(os.getpid())[1])
    monkeypatch.setattr(net, "launcher_process", lambda record: own)
    return listening


def talk(port, line=b"ping\n"):
    import socket as _socket
    with _socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(line)
        data = b""
        while True:
            chunk = client.recv(1024)
            if not chunk:
                return data
            data += chunk


def test_allow_host_port_relays_the_hosts_port_until_revoked(sandbox, monkeypatch, host_service, capsys):
    listening = relay_ready(sandbox, monkeypatch)
    status, lines = run_net(["allow", "--allow-host-port", str(host_service)])
    assert status == 0, capsys.readouterr().err
    text = "\n".join(lines)
    assert "Host ports (relayed)" in text and f"start with: --allow-host-port {host_service}" in text
    sock_path, info = net.relay_files(sandbox["run"], host_service)
    assert sock_path.exists() and stat_mode(sock_path) == 0o600 and stat_mode(info) == 0o600
    relay = net.relay_running(sandbox["run"], host_service)
    assert relay is not None
    # The agent's connection in the sandbox reaches the host's service, both ways, more than once.
    for word in (b"ping\n", b"second\n"):
        assert talk(listening[host_service], word) == b"host:" + word
    status, lines = run_net(["show", "--json"])
    assert json.loads("\n".join(lines))["host_ports"] == [host_service]
    # The proxy may reach it (its allow-list), as a port forwarded at the start.
    assert host_service in json.loads(sandbox["allow"].read_text())["loopback"]
    # Allowing it again keeps the one relay.
    assert run_net(["allow", "--allow-host-port", str(host_service)])[0] == 0
    assert net.relay_running(sandbox["run"], host_service) == relay
    # Revoked: the sandbox's end is closed, the host relay ends, its files go.
    assert run_net(["revoke", "--allow-host-port", str(host_service)])[0] == 0
    with pytest.raises(OSError):
        talk(listening[host_service])
    deadline = time.monotonic() + 5
    while not net.ended(*relay) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert net.ended(*relay) and not sock_path.exists() and not info.exists()
    assert host_service not in json.loads(sandbox["allow"].read_text())["loopback"]


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_the_host_relay_ends_with_the_run(sandbox, monkeypatch, host_service):
    """The launcher ends: the host relay sees it and ends too (no relay outlives its run)."""
    relay_ready(sandbox, monkeypatch)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(net, "launcher_process", lambda record: (child.pid, net.process_state(child.pid)[1]))
        assert run_net(["allow", "--allow-host-port", str(host_service)])[0] == 0
        relay = net.relay_running(sandbox["run"], host_service)
        assert relay is not None
    finally:
        child.kill()
        child.wait()
    deadline = time.monotonic() + 5
    while not net.ended(*relay) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert net.ended(*relay) and not net.relay_files(sandbox["run"], host_service)[0].exists()


def test_allow_host_port_refusals(sandbox, monkeypatch, host_service, capsys):
    relay_ready(sandbox, monkeypatch, host_relay=False)
    assert run_net(["allow", "--allow-host-port", str(host_service)])[0] == 1
    assert "started by a sandbox-ai older than 0.1.37" in capsys.readouterr().err
    relay_ready(sandbox, monkeypatch)
    for value in ("0", "65536", "http"):
        with pytest.raises(SystemExit):
            net.main(["allow", "--allow-host-port", value], out=lambda line: None)
    # A port forwarded since the start: refused by the supervisor, and the host relay is not left behind.
    sandbox["network"].base = {"loopback": [host_service]}
    assert run_net(["allow", "--allow-host-port", str(host_service)])[0] == 1
    assert "forwarded since the start already" in capsys.readouterr().err
    assert net.relay_running(sandbox["run"], host_service) is None
    sandbox["network"].base = {"loopback": []}
    # The administrator's policy and the run's security mode, as for the other options.
    monkeypatch.setattr(net.launcher(), "load_policy", lambda path=None: {"forbid": ["--allow-host-port"]})
    assert run_net(["allow", "--allow-host-port", str(host_service)])[0] == 1
    assert "forbidden by the administrator's policy" in capsys.readouterr().err
    monkeypatch.setattr(net.launcher(), "load_policy", lambda path=None: {})
    relay_ready(sandbox, monkeypatch, mode=mode_record("high"))
    assert run_net(["allow", "--allow-host-port", str(host_service)])[0] == 1
    assert "widens the security mode" in capsys.readouterr().err
    assert net.relay_running(sandbox["run"], host_service) is None and sandbox["network"].relays == {}


def test_a_tor_run_takes_no_host_port_during_the_run(sandbox, monkeypatch, host_service, capsys):
    """Bypass: `sandbox-ai net allow --allow-host-port` during a Tor run would relay a port of your loopback into it
    past Tor (a local proxy there reaches the internet from your own address). Refused as at the start, unless the
    run was started with --vpn-bypass-host-ports; a run of an older version is judged by its profile's type."""
    relay_ready(sandbox, monkeypatch, vpn_profile="anon", vpn_type="tor")
    assert run_net(["allow", "--allow-host-port", str(host_service)])[0] == 1
    assert f"the host's port {host_service} would be reached past Tor" in capsys.readouterr().err
    assert net.relay_running(sandbox["run"], host_service) is None
    record = {"vpn_profile": "anon"}  # no vpn_type: an older run; a profile that cannot be read counts as Tor
    assert net.tor_record(record) is True
    assert net.tor_record({"vpn_profile": "corp", "vpn_type": "wireguard"}) is False and not net.tor_record({})
    relay_ready(sandbox, monkeypatch, vpn_profile="anon", vpn_type="tor", vpn_bypass_host_ports=True)
    assert run_net(["allow", "--allow-host-port", str(host_service)])[0] == 0, capsys.readouterr().err
    assert run_net(["revoke", "--allow-host-port", str(host_service)])[0] == 0


def test_the_supervisor_refuses_relays_it_must_not_open(tmp_path):
    """A request written by hand meets the policy and the mode in the supervisor too."""
    short = Path(tempfile.mkdtemp(prefix="net-", dir="/tmp"))
    network = rt.RuntimeNetwork({}, None, lambda script: None)
    network.relay_dir = short
    stop = threading.Event()
    thread = threading.Thread(target=rt.control_server, args=(short / "control.sock", network, stop,
                                                              ["--allow-host-port"]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not (short / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with pytest.raises(ValueError, match="not allowed by the administrator's policy"):
            net.ask(short, {"op": "allow", "nets": [], "https": [], "tcp": [], "host_ports": [3000]})
        assert network.relays == {}
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(short, ignore_errors=True)


def test_the_web_interfaces_port_is_never_relayed(sandbox, monkeypatch, capsys):
    """As at the start: sandbox-ai web controls every sandbox, so no sandbox reaches its port, not during a run."""
    relay_ready(sandbox, monkeypatch)
    monkeypatch.setattr(net.launcher(), "web_service_port", lambda: 8443)
    assert run_net(["allow", "--allow-host-port", "8443"])[0] == 1
    assert "the web interface (sandbox-ai web) listens there" in capsys.readouterr().err
    assert net.relay_running(sandbox["run"], 8443) is None and sandbox["network"].relays == {}


def project_file(tmp_path):
    (tmp_path / "app").mkdir(exist_ok=True)
    return tmp_path / "app" / ".sandbox-ai.json"


def test_allow_keep_writes_the_project_file_and_trusts_it(sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = project_file(tmp_path)
    status, lines = run_net(["allow", "--allow-host", "api.osv.dev", "--allow-pypi", "--keep"])
    assert status == 0 and "kept in" in lines[-1] and "(trusted)" in lines[-1]
    document = json.loads(path.read_text())
    assert document == {"version": 1, "agent": "codex", "options": ["--allow-host", "api.osv.dev", "--allow-pypi"]}
    import hashlib
    assert net.launcher().trusted_configs()[str(path)] == hashlib.sha256(path.read_bytes()).hexdigest()
    # Again, with one more: added once, the rest not twice.
    status, lines = run_net(["allow", "--allow-host", "api.osv.dev", "--allow-host", "osv.dev", "--keep"])
    assert status == 0 and lines[-1].endswith("--allow-host osv.dev")
    assert json.loads(path.read_text())["options"] == ["--allow-host", "api.osv.dev", "--allow-pypi",
                                                       "--allow-host", "osv.dev"]
    status, lines = run_net(["allow", "--allow-host", "osv.dev", "--keep"])
    assert status == 0 and "already has it" in lines[-1]


def test_allow_keep_never_trusts_what_the_agent_changed(sandbox, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = project_file(tmp_path)
    path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--allow-pypi"]}))
    net.launcher().trust_config(path)
    path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--egress-all-allow"]}))  # the agent
    status, _ = run_net(["allow", "--allow-host", "api.osv.dev", "--keep"])
    assert status == 1 and "is not what you trusted" in capsys.readouterr().err
    assert "--egress-all-allow" in path.read_text() and "api.osv.dev" not in path.read_text()
    shown = json.loads("\n".join(run_net(["show", "--json"])[1]))
    assert shown["https"] == [] and "api.osv.dev" not in sandbox["hosts"].read_text()  # the run unchanged too
    # Another agent's file, a link: nothing kept either.
    path.write_text(json.dumps({"version": 1, "agent": "claude", "options": []}))
    net.launcher().trust_config(path)
    assert run_net(["allow", "--allow-host", "api.osv.dev", "--keep"])[0] == 1
    assert "is for claude, this run is codex" in capsys.readouterr().err
    path.unlink()
    (tmp_path / "elsewhere.json").write_text("{}")
    path.symlink_to(tmp_path / "elsewhere.json")
    assert run_net(["allow", "--allow-host", "api.osv.dev", "--keep"])[0] == 1
    assert "symlink" in capsys.readouterr().err and (tmp_path / "elsewhere.json").read_text() == "{}"


def test_allow_keep_keeps_a_login_as_its_reference_only(sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    args = net.parser().parse_args(["allow", "--allow-host", "x.example", "--git-token-env",
                                    "https://git.example.org=GIT_TOKEN", "--dns", "--keep"])
    assert net.kept_options(args, [3000]) == ["--allow-host", "x.example", "--dns", "--allow-host-port", "3000",
                                              "--git-token-env", "https://git.example.org=GIT_TOKEN"]


# --- nothing waits for good ------------------------------------------------------------------------


def test_a_relayed_connection_ends_when_one_side_stays_quiet(monkeypatch):
    """A relayed connection (join_streams: the sandbox's and the host's end) whose one side ended while the other
    never closes, or whose two sides both stay quiet, ends after a grace or idle time: its threads and its slot of
    RELAY_CONNECTIONS are not kept for good."""
    import socket as _socket
    monkeypatch.setattr(rt, "IDLE_TIMEOUT", 0.6)
    monkeypatch.setattr(rt, "RELAY_GRACE", 0.3, raising=False)
    for half_closed in (True, False):
        agent, sandbox_end = _socket.socketpair()
        host_end, service = _socket.socketpair()
        worker = threading.Thread(target=rt.join_streams, args=(sandbox_end, host_end), daemon=True)
        worker.start()
        try:
            agent.sendall(b"request")
            assert service.recv(100) == b"request"
            if half_closed:
                agent.shutdown(_socket.SHUT_WR)
                assert service.recv(100) == b""  # the service sees the end and then never closes
            worker.join(5)
            assert not worker.is_alive(), "the join waits for good" + (" after a half-close" if half_closed else "")
        finally:
            for sock in (agent, service):
                sock.close()
            worker.join(5)


def test_an_nft_that_never_finishes_fails_the_change(tmp_path, monkeypatch):
    """The firewall change runs under the network's lock: an nft that hangs is stopped after NFT_SECONDS and the
    change fails as one nft refused (the state stays, the lock is free again), instead of holding every later change
    and approval."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nft").write_text("#!/bin/sh\nexec sleep 30\n")
    (bin_dir / "nft").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(rt, "NFT_SECONDS", 0.5, raising=False)
    network = rt.RuntimeNetwork({}, None)
    outcome = []

    def change():
        try:
            outcome.append(network.change({"op": "allow", "nets": ["192.0.2.0/24"]}))
        except (OSError, subprocess.CalledProcessError) as exc:
            outcome.append(exc)
    worker = threading.Thread(target=change, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "the change waits for nft for good"
    assert isinstance(outcome[0], OSError) and "nft" in str(outcome[0]), outcome
    assert network.shown()["nets"] == [] and network.nameservers() == []


def test_a_thread_that_cannot_start_does_not_end_a_port_relay(tmp_path, monkeypatch):
    """At the process limit a connection's thread may not start: that connection is closed and its slot given back,
    the next ones are still relayed (the listener does not stay open with connections hanging in its backlog)."""
    import socket as _socket
    real, left = threading.Thread, [1]

    class Failing(real):
        def start(self):
            if getattr(self._target, "__name__", "") == "handle" and left[0]:
                left[0] -= 1
                raise RuntimeError("can't start new thread")
            return super().start()
    monkeypatch.setattr(threading, "Thread", Failing)
    directory = Path(tempfile.mkdtemp(prefix="relay-", dir="/tmp"))
    host = _socket.socket(_socket.AF_UNIX)
    host.bind(str(directory / "relay-5999.sock"))
    host.listen()

    def host_side():
        while True:
            try:
                connection, _ = host.accept()
            except OSError:
                return
            with connection:
                connection.sendall(b"served")
    threading.Thread(target=host_side, daemon=True).start()
    free = _socket.socket()
    free.bind(("127.0.0.1", 0))
    listen = free.getsockname()[1]
    free.close()
    relay = rt.PortRelay(5999, directory, limit=1, listen_port=listen)

    def first_answer():
        with _socket.create_connection(("127.0.0.1", listen), timeout=3) as client:
            try:
                return client.recv(100)
            except ConnectionError:
                return b""
            except OSError:
                return None
    try:
        assert first_answer() == b"", "closed: no thread for it"
        assert first_answer() == b"served", "the next connection is relayed, its slot was given back"
    finally:
        relay.close()
        host.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_dead_nameserver_does_not_stall_the_dns_stub(tmp_path, monkeypatch):
    """The stub forwards beside its loop (a few at once), and a nameserver that did not answer is skipped for a
    while when another one is there: one dead nameserver (2 s per try) neither holds the other lookups nor slows
    every later one. Each forwarded name is still logged before its answer goes out."""
    import contextlib
    import socket
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream_port = upstream.getsockname()[1]
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.2", upstream_port))  # takes the queries and never answers

    def answer():
        while True:
            try:
                data, peer = upstream.recvfrom(4096)
                upstream.sendto(data[:2] + b"\x81\x80" + data[4:] + b"ANSWER", peer)
            except OSError:
                return
    threading.Thread(target=answer, daemon=True).start()
    stub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stub.bind(("127.0.0.1", 0))
    port = stub.getsockname()[1]
    stub.close()
    stop = threading.Event()
    thread = threading.Thread(target=rt.dns_logger, args=({"api.openai.com"}, port, stop, None, (), None, None,
                                                         lambda: ["127.0.0.2", "127.0.0.1"], upstream_port), daemon=True)
    thread.start()
    clients = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(3)]
    for client in clients:
        client.settimeout(10)

    def ask(client, name):
        """(answer, seconds it took)"""
        for _ in range(50):
            started = time.monotonic()
            try:
                client.sendto(query(name), ("127.0.0.1", port))
                return client.recvfrom(4096)[0], time.monotonic() - started
            except ConnectionRefusedError:
                time.sleep(0.05)  # not bound yet
        raise AssertionError("the stub did not answer")
    try:
        clients[0].settimeout(0.5)  # a query sent before the stub is bound is lost (no "refused" for it)
        for _ in range(40):
            with contextlib.suppress(socket.timeout):
                ask(clients[0], "api.openai.com")
                break
        first = []
        slow = threading.Thread(target=lambda: first.append(ask(clients[1], "first.example")), daemon=True)
        slow.start()
        time.sleep(0.2)  # the stub is waiting for the dead nameserver now
        answer_, took = ask(clients[2], "api.openai.com")
        assert answer_[3] & 0x0F == 3 and took < 1.0, f"a pinned name waited {took:.1f} s behind a forwarded one"
        slow.join(10)
        assert first and first[0][0].endswith(b"ANSWER")
        answer_, took = ask(clients[2], "second.example")
        assert answer_.endswith(b"ANSWER") and took < 1.0, f"the dead nameserver was asked again ({took:.1f} s)"
        logged = [json.loads(line) for line in (tmp_path / "dns.jsonl").read_text().splitlines()]
        assert [(r["host"], r["forwarded"]) for r in logged] == [("first.example", "127.0.0.1"),
                                                                 ("second.example", "127.0.0.1")]
    finally:
        stop.set()
        thread.join(5)
        for sock in (*clients, upstream, dead):
            sock.close()


def test_one_host_in_any_case_is_allowed_once(tmp_path):
    """Host names are case-insensitive: EXAMPLE.org and example.org are one pin and one allow-list entry, at the
    start and during the run (and one --keep option)."""
    args = net.parser().parse_args(["allow", "run-1", "--allow-host", "EXAMPLE.org", "-A", "example.ORG"])
    assert args.allow_host == ["example.org", "example.org"]
    l = net.launcher()
    workspace = tmp_path / "w"
    workspace.mkdir()
    a = l.parse_args(["claude", "--workspace", str(workspace), "--allow-host", "Example.org",
                      "--allow-host", "example.org", "--allow-host", "GitHub.com", "--allow-github"])
    hosts = l.allowed_hosts(a)
    assert hosts.count("example.org") == 1 and hosts.count("github.com") == 1 and "Example.org" not in hosts


def test_a_reused_pid_is_no_running_sandbox(tmp_path, monkeypatch):
    """run.json records the launcher's start time with its PID: a later process under the same PID (the run ended,
    the PID was given out again) is not taken for the run, and its stale directory goes. A record without a start
    time (an older version), or a start time that cannot be read, still counts as running."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    runs = net.launcher().runs_dir()
    own = net.launcher().process_start(os.getpid())
    for name, start in (("live", own), ("reused", own + 1), ("old", None)):
        (runs / name).mkdir(parents=True)
        record = {"run": name, "pid": os.getpid(), **({"pid_start": start} if start is not None else {})}
        (runs / name / "run.json").write_text(json.dumps(record))
    assert sorted(net.running()) == ["live", "old"] and not (runs / "reused").exists()
    monkeypatch.setattr(net.launcher(), "process_start", lambda pid, proc=None: None)
    assert net.alive(os.getpid(), own + 1)  # unknown: never removed


def test_show_keep_persists_what_the_run_was_given_into_the_first_workspaces_project_file(sandbox, tmp_path,
                                                                                          monkeypatch):
    """`net show --keep`: everything opened during the run goes into the project file of the run's first workspace
    (/work), the one its start read, never into a further workspace's own file; Git login tokens never do."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    l = net.launcher()
    app, other = tmp_path / "app", tmp_path / "lib"
    for directory in (app, other):
        directory.mkdir()
        (directory / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "codex", "options": []}))
        l.trust_config(directory / ".sandbox-ai.json")
    record = json.loads((sandbox["run"] / "run.json").read_text())
    record.update(directory=str(sandbox["run"]), allow_wildcards=["*.started.example"],
                  workspace_checks={"workspaces": [{"host": str(app), "sandbox": "/work", "mode": "rw"}],
                                    "read_only": False})
    (sandbox["run"] / "run.json").write_text(json.dumps(record))
    out = []
    assert net.main(["allow", "codex-1234abcd", "--allow-host", "pypi.org", "--allow-tcp", "192.0.2.7:8443"],
                    out=out.append, resolver=fake_resolver) == 0
    (sandbox["run"] / "wildcards.json").write_text(json.dumps(["*.started.example", "*.added.example"]))
    (sandbox["run"] / "workspaces.json").write_text(json.dumps({"version": 1, "workspaces": [
        {"host": str(other), "sandbox": "/workspaces/lib", "mode": "rw", "state": "attached"},
        {"host": str(tmp_path / "ro"), "sandbox": "/workspaces/ro", "mode": "ro", "state": "attached"}]}))
    session = Path(record["session_dir"])
    (session / l.RUNTIME_GIT_TOKENS).write_text(json.dumps({"logins": [{"url": "https://git.example", "user": "x",
                                                                        "token": "fixture-secret"}]}))
    lines = []
    assert net.main(["show", "codex-1234abcd", "--keep"], out=lines.append) == 0
    kept = json.loads((app / ".sandbox-ai.json").read_text())
    options = kept["options"]
    assert ["--allow-host", "pypi.org"] == options[options.index("pypi.org") - 1:options.index("pypi.org") + 1]
    assert "*.added.example" in options and "*.started.example" not in options  # the start's own stay where they are
    assert "192.0.2.7:8443" in options
    assert kept["workspaces"] == [str(other)]  # the read-only one is not widened into a writable one
    assert "fixture-secret" not in (app / ".sandbox-ai.json").read_text()
    assert json.loads((other / ".sandbox-ai.json").read_text())["options"] == []  # a further workspace: untouched
    assert l.trusted_configs()[str(l.config_key(app / ".sandbox-ai.json"))]  # still trusted after the write
    assert any("read-only: not kept" in line for line in lines) and any("Git logins" in line for line in lines)
    lines.clear()
    assert net.main(["show", "codex-1234abcd", "--keep"], out=lines.append) == 0
    assert "already has it all" in lines[-1]


def test_the_start_reads_only_its_first_workspaces_project_file(tmp_path, monkeypatch):
    """With several workspaces, the configuration is the first one's (the directory started in, or the first
    --workspace): a further workspace's .sandbox-ai.json is never read, trusted or not."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    l = net.launcher()
    first, further = tmp_path / "first", tmp_path / "further"
    for directory, option in ((first, "--allow-pypi"), (further, "--egress-all-allow")):
        directory.mkdir()
        (directory / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": [option]}))
        l.trust_config(directory / ".sandbox-ai.json")
    a = l.parse_args(["claude", "--workspace", f"{first},{further}"])
    path, document = l.load_project_config(a)
    assert path == l.config_key(first / ".sandbox-ai.json") and document["options"] == ["--allow-pypi"]
    monkeypatch.chdir(first)  # no --workspace: the directory started in
    path, _ = l.load_project_config(l.parse_args(["claude"]))
    assert path == l.config_key(first / ".sandbox-ai.json")
    b = l.parse_args(["claude", "--workspace", str(further), "--workspace", str(first)])
    assert l.load_project_config(b)[0] == l.config_key(first / ".sandbox-ai.json")  # the directory started in wins
    monkeypatch.chdir(tmp_path)  # started where no project file is: the first given wins
    b = l.parse_args(["claude", "--workspace", str(further), "--workspace", str(first)])
    assert l.load_project_config(b)[0] == l.config_key(further / ".sandbox-ai.json")


def test_a_paused_or_silent_sandbox_gives_an_error_not_a_traceback(sandbox, tmp_path, capsys):
    (sandbox["run"] / "paused").write_text("")
    assert net.main(["show", "codex-1234abcd"], out=lambda line: None) == 1
    assert "paused: resume it first" in capsys.readouterr().err
    (sandbox["run"] / "paused").unlink()
    silent = tmp_path / "silent"
    silent.mkdir()
    import socket as sockets
    listener = sockets.socket(sockets.AF_UNIX)
    short = Path(tempfile.mkdtemp(prefix="s-", dir="/tmp"))
    try:
        listener.bind(str(short / "control.sock"))
        listener.listen(1)  # accepts, never answers (a frozen supervisor)
        with pytest.raises(ValueError, match="did not answer"):
            net.ask(short, {"op": "show"}, timeout=0.3)
    finally:
        listener.close()
        shutil.rmtree(short, ignore_errors=True)


def test_host_names_outside_ascii_are_refused_as_at_the_start():
    args = net.parser().parse_args(["allow", "r", "--allow-host", "bücher.de"])
    with pytest.raises(ValueError, match="xn--"):
        net.request_for(args, "allow", resolver=fake_resolver)
    args = net.parser().parse_args(["allow", "r", "--allow-host", "xn--bcher-kva.de"])
    assert net.request_for(args, "allow", resolver=fake_resolver)[2] == ["xn--bcher-kva.de"]


def test_concurrent_host_updates_keep_every_name(sandbox):
    record = {**json.loads((sandbox["run"] / "run.json").read_text()), "directory": str(sandbox["run"])}
    threads = [threading.Thread(target=net.update_hosts, args=(record, {f"n{i}.example.org": [f"192.0.2.{i}"]}))
               for i in range(1, 30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(net.remembered(record)) == sorted(f"n{i}.example.org" for i in range(1, 30))
    assert all(f"n{i}.example.org" in sandbox["hosts"].read_text() for i in range(1, 30))



def test_show_names_the_wildcards_and_revoke_refuses_unknown_ones(sandbox, capsys):
    record = json.loads((sandbox["run"] / "run.json").read_text())
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "allow_wildcards": ["*.start.example"]}))
    (sandbox["run"] / "wildcards.json").write_text(json.dumps(["*.added.example"]))
    status, lines = run_net(["show"])
    assert status == 0 and any("*.added.example" in line for line in lines) and \
        any("*.start.example" in line for line in lines)
    status, lines = run_net(["show", "--json"])
    assert json.loads("\n".join(lines))["host_wildcards"] == ["*.added.example", "*.start.example"]
    assert net.main(["revoke", "codex-1234abcd", "--allow-host", "*.never.example"], out=lambda line: None) == 1
    assert "was not allowed during this run" in capsys.readouterr().err


def test_wildcards_need_the_dns_stub_and_say_why_one_is_refused(sandbox, capsys):
    record = json.loads((sandbox["run"] / "run.json").read_text())
    session = Path(record["session_dir"])
    (session / "config.json").write_text(json.dumps({"ask": True}))
    (session / "resolv.conf").write_text("nameserver 1.1.1.1\n")  # started with --dns: the stub is bypassed
    assert net.main(["allow", "codex-1234abcd", "--allow-host", "*.example.org"], out=lambda line: None) == 1
    assert "past its DNS stub" in capsys.readouterr().err
    assert net.main(["allow", "codex-1234abcd", "--allow-host", "*.github.io"], out=lambda line: None) == 1
    assert "anyone can have" in capsys.readouterr().err  # the real reason, not "invalid"


def test_keep_checks_what_the_supervisor_reports(sandbox):
    """The supervisor runs beside the agent: a value it reports that is no address, network or port never goes into
    the trusted project file."""
    record = {**json.loads((sandbox["run"] / "run.json").read_text()), "directory": str(sandbox["run"])}
    for bad in ({"nets": ["--egress-all-allow"]}, {"tcp": [["192.0.2.1", 70000]]}, {"dns": ["8.8.8.8 --x"]},
                {"https": ["not-an-address"]}):
        with pytest.raises(ValueError):
            net.running_options(record, bad)
    (sandbox["run"] / "wildcards.json").write_text(json.dumps(["*.example.org"]))
    net.update_hosts(record, {"a.example.org": ["192.0.2.60"], "other.example": ["192.0.2.61"]})
    tokens, _, _ = net.running_options(record, {"https": ["192.0.2.60/32", "192.0.2.61/32"]}, everything=True)
    # The names below a wildcard are not kept one by one: the wildcard is.
    assert "a.example.org" not in tokens and tokens.count("--allow-host") == 2 and "*.example.org" in tokens


def wildcard_sandbox(sandbox):
    record = {**json.loads((sandbox["run"] / "run.json").read_text()), "directory": str(sandbox["run"])}
    session = Path(record["session_dir"])
    (session / "config.json").write_text(json.dumps({"ask": True}))
    (session / "resolv.conf").write_text("nameserver 127.0.0.1\n")
    return record


def test_concurrent_wildcard_changes_lose_nothing(sandbox, monkeypatch):
    """`net allow` and `net revoke` of wildcards at once: the supervisor's answer and the file written from it are
    one step under the run's lock, each with a temporary file of its own (a shared one was renamed away under the
    other: FileNotFoundError), so the file ends as the supervisor's last answer, never an older one."""
    monkeypatch.setattr(rt, "WILDCARDS", set())  # the supervisor's set (control_server) for this test only
    record = wildcard_sandbox(sandbox)
    asked, slow = net.ask, threading.Event()

    def ask(directory, request, timeout=net.ASK_SECONDS):
        answer = asked(directory, request, timeout)
        if request.get("add") == ["*.first.example"]:
            slow.set()
            time.sleep(0.4)  # the first answer is written late: the others asked meanwhile
        return answer
    monkeypatch.setattr(net, "ask", ask)
    errors = []

    def change(wildcard):
        try:
            net.change_wildcards(record, "allow", [wildcard])
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)
    first = threading.Thread(target=change, args=("*.first.example",))
    first.start()
    slow.wait(5)
    others = [threading.Thread(target=change, args=(f"*.n{n}.example",)) for n in range(8)]
    for thread in others:
        thread.start()
    for thread in [first, *others]:
        thread.join(20)
    assert errors == []
    wanted = sorted(["*.first.example"] + [f"*.n{n}.example" for n in range(8)])
    assert json.loads(net.wildcards_file(record).read_text()) == wanted == net.run_wildcards(record)
    assert not list(sandbox["run"].glob(".wildcards.*"))  # no temporary file left behind


def keep_project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    l = net.launcher()
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    (app / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "codex", "options": []}))
    l.trust_config(app / ".sandbox-ai.json")
    return app


def test_show_keep_keeps_what_you_gave_and_leaves_out_what_approvals_opened(sandbox, tmp_path, monkeypatch):
    """By default `net show --keep` writes what you allowed with `net allow` and what you approved "always" for the
    project, never the names and addresses an approval opened once or for the run; it shows every option before
    it writes; --keep-all keeps those as well."""
    app = keep_project(tmp_path, monkeypatch)
    record = {**json.loads((sandbox["run"] / "run.json").read_text()), "directory": str(sandbox["run"])}
    assert net.main(["allow", "codex-1234abcd", "--allow-host", "pypi.org", "--allow-tcp", "192.0.2.7:8443"],
                    out=lambda line: None, resolver=fake_resolver) == 0
    # What approvals.answer and the supervisor do for approved questions: a lookup answered for the run, one
    # answered always (remembered for the project), a connection answered for the run.
    net.update_hosts(record, {"approved.example": ["192.0.2.70"], "always.example": ["192.0.2.71"]})
    sandbox["network"].change({"op": "allow", "https": ["192.0.2.70", "192.0.2.71"]})
    sandbox["network"].change({"op": "allow", "tcp": [["192.0.2.8", 9443]]}, ssh=False)
    net.approvals_module().remember(str(app), host="always.example", details={"question": "1-2-3"})
    lines = []
    assert net.main(["show", "codex-1234abcd", "--keep"], out=lines.append) == 0
    options = json.loads((app / ".sandbox-ai.json").read_text())["options"]
    assert options == ["--allow-host", "always.example", "--allow-host", "pypi.org", "--allow-tcp", "192.0.2.7:8443"]
    text = "\n".join(lines)
    assert "approved.example" not in options and "192.0.2.8:9443" not in options
    left = next(line for line in lines if "left out" in line)
    assert "--allow-host approved.example" in left and "--allow-tcp 192.0.2.8:9443" in left and "--keep-all" in left
    preview = lines.index(next(line for line in lines if "writing into" in line))
    assert lines[preview + 1:preview + 4] == ["  --allow-host always.example", "  --allow-host pypi.org",
                                              "  --allow-tcp 192.0.2.7:8443"]
    assert "kept in" in lines[-1] and preview < len(lines) - 1 and "pypi.org" in text
    lines.clear()
    assert net.main(["show", "codex-1234abcd", "--keep-all"], out=lines.append) == 0
    options = json.loads((app / ".sandbox-ai.json").read_text())["options"]
    assert "approved.example" in options and "192.0.2.8:9443" in options
    # Taken back with net revoke: no longer yours to keep.
    assert net.main(["revoke", "codex-1234abcd", "--allow-host", "pypi.org"], out=lambda line: None) == 0
    assert "pypi.org" not in net.given(record)["hosts"] and "192.0.2.7:8443" in net.given(record)["tcp"]


def test_show_keep_is_refused_when_the_policy_caps_approvals(sandbox, tmp_path, monkeypatch, capsys):
    app = keep_project(tmp_path, monkeypatch)
    assert net.main(["allow", "codex-1234abcd", "--allow-host", "pypi.org"], out=lambda line: None,
                    resolver=fake_resolver) == 0
    monkeypatch.setattr(net.approvals_module(), "policy_scope", lambda: "run")
    for option in ("--keep", "--keep-all"):
        assert net.main(["show", "codex-1234abcd", option], out=lambda line: None) == 1
        assert "approves at most for run" in capsys.readouterr().err
    assert json.loads((app / ".sandbox-ai.json").read_text())["options"] == []


def test_allow_keep_is_refused_when_the_policy_caps_approvals(sandbox, tmp_path, monkeypatch, capsys):
    """net allow --keep checks the policy as net show --keep does, before anything changes: nothing allowed, nothing
    written."""
    app = keep_project(tmp_path, monkeypatch)
    monkeypatch.setattr(net.approvals_module(), "policy_scope", lambda: "run")
    assert net.main(["allow", "codex-1234abcd", "--allow-host", "pypi.org", "--keep"], out=lambda line: None,
                    resolver=fake_resolver) == 1
    assert "approves at most for run" in capsys.readouterr().err
    assert json.loads((app / ".sandbox-ai.json").read_text())["options"] == []
    record = net.choose(net.running(), "codex-1234abcd")
    assert "pypi.org" not in net.given(record)["hosts"] and "pypi.org" not in net.remembered(record)
    monkeypatch.setattr(net.approvals_module(), "policy_scope", lambda: "always")
    assert net.main(["allow", "codex-1234abcd", "--allow-host", "pypi.org", "--keep"], out=lambda line: None,
                    resolver=fake_resolver) == 0
    assert "pypi.org" in json.loads((app / ".sandbox-ai.json").read_text())["options"]


def test_an_answer_that_cannot_be_handled_frees_the_question(tmp_path):
    """Bug hunt: an error while the broker handled an answer (addresses that are no list; the .done file not
    written on a full disk) ended its waiting thread: the asker waited out its whole timeout, and a question that
    stayed open held one of OPEN_QUESTIONS for good, every later ask of it waiting in vain."""
    rules = []
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, rules.append), timeout=30, poll=0.02)

    def answer_first(decision="allow", **extra):
        deadline = time.monotonic() + 5
        while not list((tmp_path / "a").glob("*.json")) and time.monotonic() < deadline:
            time.sleep(0.02)
        question = json.loads(sorted((tmp_path / "a").glob("*.json"))[0].read_text())
        (tmp_path / "a" / f"{question['id']}.answer").write_text(json.dumps({"decision": decision, **extra}))
        return question
    results = []
    thread = threading.Thread(target=lambda: results.append(broker.ask("dns", {"name": "pypi.org"})), daemon=True)
    thread.start()
    answer_first(scope="run", addresses=5)
    thread.join(5)
    assert results and results[0]["decision"] == "deny" and "could not be applied" in results[0]["reason"]
    assert broker.pending == {}
    for path in (tmp_path / "a").iterdir():
        path.unlink()

    def full(name, data):
        if name.endswith(".done"):
            raise OSError(28, "No space left on device")
        rt.ApprovalBroker._write(broker, name, data)
    broker._write = full
    started = time.monotonic()
    thread = threading.Thread(target=lambda: results.append(broker.ask("connect", {
        "destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443})), daemon=True)
    thread.start()
    answer_first(scope="run")
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - started < 10, "the asker waited out its timeout"
    assert results[1]["decision"] in ("allow", "deny") and broker.pending == {}


def test_a_pump_that_cannot_start_gives_the_mcp_slot_back(monkeypatch):
    """Bug hunt: at the process limit the thread that carries a server's answers (McpConnection.pump) may not start;
    that RuntimeError went up out of the connection's thread before its cleanup, so the server instance kept
    running and its slot (one of MCP_INSTANCE_LIMIT) was never given back: after that many, every connection was
    refused for the rest of the run."""
    import io
    import socket as _socket
    real, left = threading.Thread, [1]

    class Failing(real):
        def start(self):
            if getattr(self._target, "__name__", "") == "pump" and left[0]:
                left[0] -= 1
                raise RuntimeError("can't start new thread")
            return super().start()
    monkeypatch.setattr(threading, "Thread", Failing)
    monkeypatch.setattr(rt, "MCP_INSTANCE_LIMIT", 1)
    directory = Path(tempfile.mkdtemp(prefix="mcp-", dir="/tmp"))
    upstream = _socket.socket(_socket.AF_UNIX)
    upstream.bind(str(directory / "up.sock"))
    upstream.listen()
    served = []

    def serve_upstream():
        while True:
            try:
                connection, _ = upstream.accept()
            except OSError:
                return
            served.append(connection)
            with connection:
                for _ in connection.makefile("rb"):
                    connection.sendall(b'{"jsonrpc": "2.0", "id": 1, "result": {}}\n')
    threading.Thread(target=serve_upstream, daemon=True).start()
    gate = rt.McpGateway({}, io.StringIO(), plain=True)
    threading.Thread(target=rt.mcp_serve, args=(str(directory / "server.sock"), None), daemon=True,
                     kwargs={"gateway": gate, "spawn": lambda: rt.McpSocket(directory / "up.sock")}).start()
    deadline = time.monotonic() + 5
    while not (directory / "server.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    def ask():
        with _socket.socket(_socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(directory / "server.sock"))
            client.sendall(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
            try:
                return client.recv(1000)
            except ConnectionError:
                return b""
    try:
        assert ask() == b"", "closed: no thread to carry its answers"
        assert b'"result"' in ask(), "the next connection is served: the slot was given back"
        assert len(served) == 2
    finally:
        upstream.close()
        shutil.rmtree(directory, ignore_errors=True)


# --- API keys during the run (sandbox-ai net allow --api-key) ------------------------------------------------------

KEY = "sk-live-0123456789abcdef"


def proxy_ready(sandbox, tmp_path, monkeypatch=None, **fields):
    """A run with the proxy: its supervisor (the control_server thread) gives keys to the proxy's run directory
    (PROXY_RUN, here a directory of the test). Returns the proxy's file of them."""
    record = json.loads((sandbox["run"] / "run.json").read_text())
    proxy = tmp_path / "proxy-run"
    proxy.mkdir(mode=0o700, exist_ok=True)
    if monkeypatch is not None:
        monkeypatch.setattr(rt, "PROXY_RUN", proxy)
    (sandbox["run"] / "run.json").write_text(json.dumps({**record, "proxy": True, "version": "0.1.92", **fields}))
    return proxy / rt.ADDED_API_KEYS


def test_allow_api_key_gives_the_running_sandboxs_proxy_a_key(sandbox, tmp_path, monkeypatch, capsys):
    keys = proxy_ready(sandbox, tmp_path, monkeypatch)
    monkeypatch.setattr(net.launcher(), "load_secret_store_module",
                        lambda: type("S", (), {"get": staticmethod(lambda name: {"OPENROUTER": KEY}.get(name))}))
    status, lines = run_net(["allow", "--api-key", "openrouter.ai=OPENROUTER_API_KEY=OPENROUTER"])
    text = "\n".join(lines)
    assert status == 0, capsys.readouterr().err
    assert KEY not in text and "export OPENROUTER_API_KEY=sandbox-ai-api-key-placeholder-OPENROUTER_API_KEY" in text
    assert "start with: --allow-host openrouter.ai --api-key openrouter.ai=OPENROUTER_API_KEY=OPENROUTER" in text
    assert keys.stat().st_mode & 0o777 == 0o600
    entries = json.loads(keys.read_text())["api_keys"]
    assert entries == [{"host": "openrouter.ai", "port": 443, "name": "OPENROUTER_API_KEY",
                        "placeholder": "sandbox-ai-api-key-placeholder-OPENROUTER_API_KEY", "key": KEY}]
    # The host is allowed with it (resolved, opened on 443, pinned).
    assert "192.0.2.50 openrouter.ai" in sandbox["hosts"].read_text()
    # The proxy reads the file and puts the key in where the agent sends the placeholder, to that host only.
    assert rt.added_api_keys(keys) == entries
    head = (b"GET /api/v1/models HTTP/1.1\r\nHost: openrouter.ai\r\n"
            b"Authorization: Bearer sandbox-ai-api-key-placeholder-OPENROUTER_API_KEY\r\n\r\n")
    swapped, names = rt.api_key_head(rt.added_api_keys(keys), "https", "openrouter.ai", 443, head)
    assert KEY.encode() in swapped and names == ["OPENROUTER_API_KEY"]
    assert rt.api_key_head(rt.added_api_keys(keys), "https", "evil.example", 443, head) == (head, [])
    assert rt.api_key_head(rt.added_api_keys(keys), "http", "openrouter.ai", 443, head) == (head, [])
    # Taken back: the file keeps the others; a second revoke says there is none.
    status, lines = run_net(["revoke", "--api-key", "OPENROUTER_API_KEY"])
    assert status == 0 and json.loads(keys.read_text())["api_keys"] == [] and rt.added_api_keys(keys) == []
    assert run_net(["revoke", "--api-key", "OPENROUTER_API_KEY"])[0] == 1
    assert "no API key OPENROUTER_API_KEY was given during this run" in capsys.readouterr().err


def test_allow_api_key_refusals(sandbox, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(net.launcher(), "load_secret_store_module",
                        lambda: type("S", (), {"get": staticmethod(lambda name: None)}))
    # A run without the proxy's directory in run.json (older, or no proxy): said, nothing written.
    assert run_net(["allow", "--api-key", "openrouter.ai=OPENROUTER_API_KEY"])[0] == 1
    assert "takes no API key during the run" in capsys.readouterr().err
    keys = proxy_ready(sandbox, tmp_path, monkeypatch)
    for value, message in (("openrouter.ai=PATH", "Sandbox-owned variable"),
                           ("192.0.2.9=KEY", "--api-key expects HOST=NAME"),
                           ("openrouter.ai=OPENROUTER_API_KEY", "no secret OPENROUTER_API_KEY")):
        assert run_net(["allow", "--api-key", value])[0] == 1
        assert message in capsys.readouterr().err
    assert not keys.exists() and sandbox["applied"] == [], "a refused key changes nothing"
    monkeypatch.setattr(net.launcher(), "load_policy", lambda *a, **k: {"forbid": ["--api-key"]})
    assert run_net(["allow", "--api-key", "openrouter.ai=K"])[0] == 1
    assert "forbidden by the administrator's policy" in capsys.readouterr().err


def test_the_proxy_reads_only_well_formed_added_keys(tmp_path):
    path = tmp_path / rt.ADDED_API_KEYS
    good = {"host": "API.example.org", "port": 443, "name": "K", "placeholder": "sandbox-ai-api-key-placeholder-K",
            "key": "abc123"}
    path.write_text(json.dumps({"api_keys": [good, {**good, "key": "with space"}, {**good, "port": "443"},
                                             {**good, "placeholder": "anything"}, "junk"]}))
    assert rt.added_api_keys(path) == [{**good, "host": "api.example.org"}]
    path.write_text("not json")
    assert rt.added_api_keys(path) == []
    path.unlink()
    assert rt.added_api_keys(path) == []


def test_allow_api_key_keeps_to_the_mode_of_the_run(sandbox, tmp_path, monkeypatch, capsys):
    keys = proxy_ready(sandbox, tmp_path, monkeypatch, mode={"name": "high", "origin": "test", "refuses": ["--api-key"]})
    monkeypatch.setattr(net.launcher(), "load_secret_store_module",
                        lambda: type("S", (), {"get": staticmethod(lambda name: KEY)}))
    assert run_net(["allow", "--api-key", "openrouter.ai=OPENROUTER_API_KEY"])[0] == 1
    assert "--api-key widens the security mode high" in capsys.readouterr().err
    assert not keys.exists() and sandbox["applied"] == []


@pytest.mark.parametrize("runtime", ["dir", "missing", "unset"])
def test_the_run_directory_is_found_as_the_launcher_finds_it(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    if runtime == "unset":
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    else:
        (tmp_path / "rt").mkdir(exist_ok=runtime == "dir") if runtime == "dir" else None
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("rt" if runtime == "dir" else "gone")))
    assert net.own_runs_dir() == net.launcher().runs_dir()


def test_the_supervisor_refuses_keys_the_policy_forbids(tmp_path, monkeypatch):
    """A request written by hand reaches the control socket too: the supervisor checks --api-key against the policy
    and the mode itself."""
    short = Path(tempfile.mkdtemp(prefix="net-", dir="/tmp"))
    monkeypatch.setattr(rt, "PROXY_RUN", tmp_path)
    stop = threading.Event()
    network = rt.RuntimeNetwork({"loopback": []}, tmp_path / "allow.json", lambda *a: None)
    entry = {"host": "api.x.org", "port": 443, "name": "K", "placeholder": "sandbox-ai-api-key-placeholder-K",
             "key": "abc"}
    for forbid, mode, expected in ((["--api-key"], None, "not allowed by the administrator's policy"),
                                   ((), {"name": "high", "refuses": ["--api-key"]}, "widens the security mode high")):
        socket_path = short / f"control-{len(expected)}.sock"
        thread = threading.Thread(target=rt.control_server, args=(socket_path, network, stop, forbid, mode),
                                  daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with net.socket.socket(net.socket.AF_UNIX) as client:
            client.connect(str(socket_path))
            client.sendall(json.dumps({"op": "api-keys", "api_keys": [entry]}).encode() + b"\n")
            answer = json.loads(client.makefile().readline())
        assert answer["ok"] is False and expected in answer["error"], answer
    assert not (tmp_path / rt.ADDED_API_KEYS).exists()
    stop.set()
    shutil.rmtree(short, ignore_errors=True)


def test_a_key_that_cannot_be_given_opens_nothing(sandbox, tmp_path, monkeypatch, capsys):
    """Refused before the network changes: a run of an older sandbox-ai (its supervisor knows no "api-keys"), a host
    the run does not inspect, a key the supervisor would refuse."""
    monkeypatch.setattr(net.launcher(), "load_secret_store_module",
                        lambda: type("S", (), {"get": staticmethod(lambda name: {"LONG": "x" * 5000}.get(name, KEY))}))
    for fields, value, message in (({"version": "0.1.91"}, "openrouter.ai=K", "older than 0.1.92"),
                                   ({"no_inspect": ["openrouter.ai"]}, "openrouter.ai=K", "--no-inspect openrouter.ai"),
                                   ({"no_inspect": []}, "openrouter.ai=K=LONG", "at most 4096 characters")):
        keys = proxy_ready(sandbox, tmp_path, monkeypatch, **fields)
        assert run_net(["allow", "--api-key", value])[0] == 1
        assert message in capsys.readouterr().err
        assert sandbox["applied"] == [] and "openrouter.ai" not in sandbox["hosts"].read_text() and not keys.exists()


def test_a_key_given_during_the_run_replaces_the_starts_of_that_name():
    start = [{"host": "dev.to", "port": 443, "name": "K", "placeholder": "sandbox-ai-api-key-placeholder-K",
              "key": "OLD-KEY"}]
    added = [{**start[0], "key": "NEW-KEY"}]
    head = b"GET / HTTP/1.1\r\nHost: dev.to\r\napi-key: sandbox-ai-api-key-placeholder-K\r\n\r\n"
    swapped, _ = rt.api_key_head(added + start, "https", "dev.to", 443, head)
    assert b"NEW-KEY" in swapped and b"OLD-KEY" not in swapped
    assert "added_api_keys() + (self.config.get(\"api_keys\") or [])" in (ROOT / "sandbox/runtime.py").read_text()



def test_a_key_for_a_host_below_an_uninspected_wildcard_is_refused(sandbox, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(net.launcher(), "load_secret_store_module",
                        lambda: type("S", (), {"get": staticmethod(lambda name: KEY)}))
    keys = proxy_ready(sandbox, tmp_path, monkeypatch, no_inspect=["*.example.com"])
    assert run_net(["allow", "--api-key", "api.example.com=K"])[0] == 1
    assert "--no-inspect api.example.com" in capsys.readouterr().err and not keys.exists()
