"""macOS support, stage 2 (TODO.md): the host side on a Mac, simulated on Linux - processes through sysctl (procinfo.py,
with a fake kernel), and the sockets in podman machine's VM reached through `podman exec` (attach, `sandbox-ai net`,
the terminal relay's readiness) with `podman update` for the limits. No Podman and no Mac here: every podman call is
recorded, and where it runs runtime.py's client it runs it for real against a supervisor's socket in this process."""
import ctypes
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p = load("procinfo", "procinfo_macos_test")
net = load("net", "net_macos_test")
sessions = load("sessions", "sessions_macos_test")
devices = load("devices", "devices_macos_test")
s = net.launcher()
rt = s.RUNTIME


# --- a fake macOS kernel --------------------------------------------------------------------------------------

def kinfo(pid, ppid=1, uid=501, start=(1_700_000_000, 250_000), state=2, pgid=None):
    info = p.KinfoProc()
    info.kp_proc.p_pid = pid
    info.kp_proc.p_stat = bytes([state])
    info.kp_proc.p_starttime.tv_sec, info.kp_proc.p_starttime.tv_usec = start
    info.kp_eproc.e_ppid = ppid
    info.kp_eproc.e_pgid = pgid if pgid is not None else pid
    info.kp_eproc.e_ucred.cr_uid = uid
    return bytes(info)


def procargs(argv, exe=b"/usr/bin/python3"):
    """KERN_PROCARGS2 as macOS lays it out: argc, the executable's path, padding NULs, the arguments, the environment."""
    return struct.pack("i", len(argv)) + exe + b"\0\0\0\0" + b"\0".join(argv) + b"\0HOME=/Users/ada\0"


class Kernel:
    def __init__(self, processes):
        self.processes = processes  # {pid: (kinfo bytes, argv or None)}

    def sysctl(self, mib, size=None):
        if mib[:3] == [p.CTL_KERN, p.KERN_PROC, p.KERN_PROC_PID]:
            found = self.processes.get(mib[3])
            return found[0] if found else b""
        if mib[:3] == [p.CTL_KERN, p.KERN_PROC, p.KERN_PROC_ALL]:
            return b"".join(info for info, _ in self.processes.values())
        if mib == [p.CTL_KERN, p.KERN_ARGMAX]:
            return struct.pack("i", 1 << 20)
        if mib[:2] == [p.CTL_KERN, p.KERN_PROCARGS2]:
            found = self.processes.get(mib[2])
            if not found or found[1] is None:
                raise OSError(22, "Invalid argument")  # another user's, or gone
            return procargs(found[1])
        raise OSError(22, f"unexpected sysctl {mib}")

    def pidinfo(self, pid, flavor, size):
        assert flavor == p.PROC_PIDVNODEPATHINFO
        if pid not in self.processes:
            raise OSError(3, "No such process")
        cdir = b"\0" * p.VNODE_INFO_SIZE + b"/Users/ada/src/app".ljust(p.MAXPATHLEN, b"\0")
        return cdir + b"\0" * (p.VNODE_INFO_SIZE + p.MAXPATHLEN)


@pytest.fixture
def mac(monkeypatch):
    """sys.platform is darwin, and procinfo's kernel calls answer from a fake process table."""
    kernel = Kernel({
        1: (kinfo(1, ppid=0, uid=0), None),
        700: (kinfo(700, uid=501), [b"/usr/bin/python3", b"-I", b"/opt/sandbox/agent_sandbox.py", b"claude"]),
        701: (kinfo(701, ppid=700, uid=501, start=(1_700_000_100, 0)), [b"podman", b"run"]),
        702: (kinfo(702, ppid=701, uid=501, state=5), [b"zombie"]),
        900: (kinfo(900, uid=0), None),
    })
    monkeypatch.setattr(sys, "platform", "darwin")
    for module in (p, s.procinfo()):
        monkeypatch.setattr(module, "SYSCTL", kernel.sysctl)
        monkeypatch.setattr(module, "PIDINFO", kernel.pidinfo)
    return kernel


# --- stage 2: processes -------------------------------------------------------------------------------------

def test_kinfo_proc_has_macos_layout():
    """struct kinfo_proc of 64-bit macOS: 648 bytes, the fields read at their offsets there."""
    assert ctypes.sizeof(p.KinfoProc) == 648 and ctypes.sizeof(p.ExternProc) == 296
    assert p.KinfoProc.kp_eproc.offset + p.Eproc.e_ppid.offset == 560
    assert p.KinfoProc.kp_eproc.offset + p.Eproc.e_ucred.offset + p.Ucred.cr_uid.offset == 420
    assert p.ExternProc.p_stat.offset == 36 and p.ExternProc.p_pid.offset == 40


def test_macos_processes_from_sysctl(mac):
    assert p.start_time(700) == 1_700_000_000_250_000 and p.start_time(4242) is None
    assert p.cmdline(700) == [b"/usr/bin/python3", b"-I", b"/opt/sandbox/agent_sandbox.py", b"claude", b""]
    assert p.cmdline(900) is None  # another user's arguments are not readable
    assert p.owner(700) == 501 and p.owner(1) == 0 and p.owner(4242) is None
    assert p.state(702) == ("Z", 1_700_000_000_250_000) and p.state(701)[0] == "R"
    assert p.parent(701) == 700 and p.children(700) == [701] and p.children(701) == [702]
    assert p.pids() == [1, 700, 701, 702, 900] and p.table()[702] == (701, 702, "Z")
    assert p.started_epoch(701) == 1_700_000_100.0
    assert p.cwd(700) == "/Users/ada/src/app" and p.cwd(4242) is None


def test_a_fake_proc_is_read_as_proc_also_on_macos(mac, tmp_path):
    """The tests' fake /proc (any directory but /proc) is read as Linux's, whatever the platform."""
    (tmp_path / "55").mkdir()
    (tmp_path / "55" / "stat").write_text("55 (a b) c) S 1 55 55 0 -1" + " 0" * 13 + " 4242\n")
    (tmp_path / "55" / "cmdline").write_bytes(b"x\0y\0")
    assert p.start_time(55, tmp_path) == 4242 and p.cmdline(55, tmp_path) == [b"x", b"y", b""]
    assert p.state(55, tmp_path) == ("S", 4242) and p.children(1, tmp_path) == [55]


@pytest.mark.skipif(sys.platform != "darwin", reason="the real macOS kernel (the CI's macOS job)")
def test_the_real_macos_kernel(tmp_path):
    me = os.getpid()
    assert p.start_time(me) and p.state(me)[0] in ("R", "S") and p.owner(me) == os.getuid()
    assert p.parent(me) == os.getppid() and me in p.children(os.getppid()) and me in p.pids()
    assert p.cwd(me) == os.path.realpath(os.getcwd())
    assert abs(p.started_epoch(me) - time.time()) < 24 * 3600
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        deadline = time.monotonic() + 10
        while p.cmdline(child.pid) in (None, []) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert p.cmdline(child.pid)[1:3] == [b"-c", b"import time; time.sleep(30)"]
        assert child.pid in p.children(me) and p.start_time(child.pid) >= p.start_time(me)
    finally:
        child.kill()
        child.wait()
    assert p.start_time(child.pid) is None
    with open(__file__) as stream:
        assert os.path.realpath(p.fd_path(stream.fileno())) == os.path.realpath(__file__)
    left, right = socket.socketpair()
    with left, right:
        pid, uid, _ = rt.Attribution.peer(left)
        assert (pid, uid) == (me, os.getuid())  # LOCAL_PEERPID and LOCAL_PEERCRED


@pytest.mark.skipif(sys.platform != "linux", reason="Linux reads /proc")
def test_linux_reads_proc_as_before():
    me = os.getpid()
    fields = Path(f"/proc/{me}/stat").read_text().rpartition(")")[2].split()
    assert p.start_time(me) == int(fields[19]) and p.state(me)[1] == int(fields[19])
    assert p.cmdline(me) == Path(f"/proc/{me}/cmdline").read_bytes().split(b"\0")
    assert p.owner(me) == os.getuid() and p.parent(me) == os.getppid() and me in p.children(os.getppid())
    assert p.cwd(me) == os.getcwd() and p.start_time(2 ** 22 + 7) is None
    with open(__file__) as stream:
        assert p.fd_path(stream.fileno()) == __file__


def test_launcher_and_net_judge_processes_through_it(mac, monkeypatch):
    """process_start / process_cmdline (sessions.alive, mcp.status, net.alive) and net's launcher check on a Mac."""
    start = 1_700_000_000_250_000
    assert s.process_start(700) == start and s.process_cmdline(700, start)[2].endswith(b"agent_sandbox.py")
    assert s.process_cmdline(700, start + 1) == []  # the PID belongs to another process now
    with monkeypatch.context() as patch:
        patch.setattr(net.os, "kill", lambda pid, sig: None)  # PID 700 lives (on the fake Mac)
        assert net.alive(700, start) and not net.alive(700, start + 1)
    record = {"run": "claude-1", "pid": 700, "pid_start": start}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "getuid", lambda: 501)
        assert net.launcher_process(record) == (700, start)
        with pytest.raises(ValueError, match="does not run a sandbox-ai launcher"):
            net.launcher_process({"run": "x", "pid": 701, "pid_start": 1_700_000_100_000_000})
        with pytest.raises(ValueError, match="has ended"):
            net.launcher_process({"run": "x", "pid": 4242, "pid_start": 1})
    with pytest.raises(ValueError, match="belongs to another user"):
        net.launcher_process(record)
    assert net.ended(702, start)  # a zombie has ended
    canary = load("canary", "canary_macos_test")
    canary._procinfo = s.procinfo()
    assert canary.process_start(700) == start


def test_sessions_tree_and_mcp_supervisors_on_macos(mac, monkeypatch):
    monkeypatch.setattr(sessions, "_launcher", s)
    assert sessions.descendants(700) == [701, 702]
    assert not sessions.mcp_supervisor(700)


# --- stage 2: podman exec into the VM -------------------------------------------------------------------------

def test_machine_exec_runs_only_runtime_clients():
    record = {"backend": "podman", "pod": "agent-claude-1a2b3c4d", "podman_url": None}
    assert s.machine_exec(record, ["--internal-control-client"]) == [
        "podman", "exec", "--interactive", "--user=0:0", "agent-claude-1a2b3c4d-agent", "/usr/bin/python3", "-I",
        "/sandbox/runtime.py", "--internal-control-client"]
    assert "--tty" in s.machine_exec({**record, "container": "c-1"}, ["x"], tty=True)
    assert s.machine_exec({**record, "container": "c-1"}, ["x"], interactive=False)[2:4] == ["--user=0:0", "c-1"]
    for bad in ({"pod": "-x"}, {"pod": None}, {"container": "a;b"}, {}):
        with pytest.raises(ValueError):
            s.run_container(bad)


def test_machine_records(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert s.machine_record({"backend": "podman"}) and not s.machine_record({"backend": "bwrap"})
    assert not s.machine_record({"backend": "podman", "podman_url": "ssh://vm"}) and not s.machine_record(None)
    monkeypatch.setattr(sys, "platform", "linux")
    assert not s.machine_record({"backend": "podman"})


@pytest.fixture
def machine_run(tmp_path, monkeypatch):
    """A registered macOS run: run.json, its token, and a supervisor whose control socket only takes peers with
    that token (sandboxed: every peer of another process is "in the sandbox", as podman exec's client is)."""
    run = Path("/tmp") / f"mac-{os.getpid()}-{time.monotonic_ns()}"
    run.mkdir()
    token = os.urandom(32).hex()
    (run / rt.EXEC_TOKEN).write_text(token)
    (run / "run.json").write_text(json.dumps({"run": "claude-1a2b3c4d", "backend": "podman", "podman_url": None,
                                              "pod": "agent-claude-1a2b3c4d", "container": "agent-claude-1a2b3c4d-agent",
                                              "host_relay": True}))
    applied, stop = [], threading.Event()
    network = rt.RuntimeNetwork({"loopback": []}, None, applied.append)
    thread = threading.Thread(target=rt.control_server, args=(run / "control.sock", network, stop),
                              kwargs={"sandboxed": True, "token_file": run / rt.EXEC_TOKEN}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (run / "control.sock").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    calls, real_run = [], subprocess.run

    def podman(command, input=None, **kwargs):
        """podman exec: runtime.py's client for real, in a process of its own (a peer the supervisor can see)."""
        calls.append(command)
        assert command[:2] == ["podman", "exec"] and command[-1] == "--internal-control-client"
        code = (f"import importlib.util as u; s = u.spec_from_file_location('rt', {str(ROOT / 'sandbox/runtime.py')!r});"
                f"m = u.module_from_spec(s); s.loader.exec_module(m); "
                f"raise SystemExit(m.control_client(paths=[{str(run / 'control.sock')!r}]))")
        return real_run([sys.executable, "-I", "-c", code], input=input, capture_output=True, text=True, timeout=30)
    monkeypatch.setattr(sys, "platform", "darwin")
    yield SimpleNamespace(run=run, token=token, applied=applied, calls=calls, podman=podman)
    stop.set()
    thread.join(5)
    for path in run.iterdir():
        path.unlink()
    run.rmdir()


def test_net_reaches_the_supervisor_through_podman_exec(machine_run, monkeypatch):
    monkeypatch.setattr(net.subprocess, "run", machine_run.podman)
    answer = net.ask(machine_run.run, {"op": "allow", "https": ["192.0.2.7"]})
    assert answer["ok"] and answer["https"] == ["192.0.2.7/32"] and "tcp dport 443 accept" in machine_run.applied[-1]
    assert machine_run.calls[-1][4] == "agent-claude-1a2b3c4d-agent"
    assert net.ask(machine_run.run, {"op": "show"})["https"] == ["192.0.2.7/32"]
    with pytest.raises(ValueError, match="not possible on macOS"):
        net.ask(machine_run.run, {"op": "mcp-open"}, fds=[0])


def test_a_peer_without_the_token_is_refused(machine_run, monkeypatch):
    """The token is what lets podman exec's client in: one that is wrong (or missing) is refused as any process of
    the sandbox is, and nothing changes."""
    exec_client = ["podman", "exec", "--internal-control-client"]
    for request in ({"op": "allow", "https": ["192.0.2.8"], "exec_token": "0" * 64},
                    {"op": "allow", "https": ["192.0.2.8"]}, {"op": "show", "exec_token": None}):
        out = machine_run.podman(exec_client, input=json.dumps(request) + "\n").stdout
        assert "may not use the control socket" in out
    monkeypatch.setattr(net.subprocess, "run", machine_run.podman)
    (machine_run.run / rt.EXEC_TOKEN).unlink()
    with pytest.raises(ValueError, match="no token for podman exec"):
        net.ask(machine_run.run, {"op": "allow", "https": ["192.0.2.8"]})
    assert machine_run.applied == []


def test_without_a_token_file_peers_in_the_sandbox_are_refused_as_before(tmp_path):
    """Linux: no token file, so a visible peer is refused before anything is read (as before stage 2)."""
    path = Path("/tmp") / f"ctl-{os.getpid()}-{time.monotonic_ns()}.sock"
    stop, network = threading.Event(), rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)
    thread = threading.Thread(target=rt.control_server, args=(path, network, stop), kwargs={"sandboxed": True},
                              daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        code = (f"import socket,sys; c = socket.socket(socket.AF_UNIX); c.connect({str(path)!r}); "
                "c.sendall(b'{\"op\": \"show\", \"exec_token\": \"x\"}\\n'); print(c.makefile().readline())")
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30).stdout
        assert "may not use the control socket" in out
    finally:
        stop.set()
        thread.join(5)
        path.unlink(missing_ok=True)


def test_control_client_takes_one_line():
    out = io.BytesIO()
    assert rt.control_client(io.BytesIO(b"no newline"), out, paths=["/nonexistent"]) == 2 and out.getvalue() == b""


def test_net_allow_host_port_starts_no_host_relay_on_macos(machine_run, monkeypatch):
    """The supervisor in the VM connects to the Mac's port itself (PortRelay upstream): no host relay process."""
    monkeypatch.setattr(net, "start_relay", lambda *a: pytest.fail("no host relay on macOS"))
    record = {**json.loads((machine_run.run / "run.json").read_text()), "directory": str(machine_run.run)}
    asked = []
    monkeypatch.setattr(net, "ask", lambda directory, request, *a, **k: asked.append(request) or
                        {"ok": True, "nets": [], "https": [], "tcp": [], "dns": [], "host_ports": [8080]})
    monkeypatch.setattr(net, "choose", lambda runs, run, command=None: record)
    monkeypatch.setattr(net, "running", lambda: [record])
    monkeypatch.setattr(net, "update_hosts", lambda *a, **k: None)
    assert net.main(["allow", "--allow-host-port", "8080"], out=lambda *a: None) == 0
    assert asked and asked[0].get("host_ports") == [8080]


# --- stage 2: sessions --------------------------------------------------------------------------------------

def test_attach_on_macos_runs_the_byte_pipe_in_the_container(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    calls = []
    data = {"backend": "podman", "pod": "agent-claude-5e5e5e5e", "podman_url": None}
    assert sessions.attach_machine(data, runner=lambda command, **k: calls.append(command) or
                                   SimpleNamespace(returncode=0)) == 0
    assert calls == [["podman", "exec", "--interactive", "--tty", "--user=0:0", "agent-claude-5e5e5e5e-agent",
                      "/usr/bin/python3", "-I", "/sandbox/runtime.py", "--internal-attach-client", s.ATTACH_SOCKET]]
    with pytest.raises(RuntimeError, match="status 125"):
        sessions.attach_machine(data, runner=lambda command, **k: SimpleNamespace(returncode=125))


def test_the_terminal_relay_is_probed_in_the_vm(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    data = {"backend": "podman", "pod": "agent-claude-5e5e5e5e"}
    seen = []
    monkeypatch.setattr(sessions.subprocess, "run", lambda command, **k: seen.append(command) or
                        SimpleNamespace(returncode=0 if len(seen) > 1 else 1))
    monkeypatch.setattr(sessions, "relay_listening", lambda path: pytest.fail("no socket on the Mac to look at"))
    assert not sessions.terminal_listening("t", data) and sessions.terminal_listening("t", data)
    assert seen[0][-2:] == ["--internal-attach-probe", s.ATTACH_SOCKET] and "--tty" not in seen[0]
    ready = []
    sessions.wait_ready(Path("/nonexistent"), SimpleNamespace(poll=lambda: None), timeout=5,
                        ready=lambda: ready.append(1) or len(ready) > 2)
    assert len(ready) == 3


@pytest.mark.skipif(sys.platform != "linux", reason="runs in the container (Linux: O_PATH, /proc/self/fd)")
def test_attach_probe_and_client_in_the_container(tmp_path):
    """--internal-attach-probe: 0 while the relay accepts; a symlink in the sandbox's writable directory is not
    followed (it could lead the keystrokes to the supervisor's control socket)."""
    path = tmp_path / "attach.sock"
    assert rt.attach_probe(str(path)) == 1
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(tmp_path / "control.sock"))
    server.listen(4)
    path.symlink_to(tmp_path / "control.sock")
    assert rt.attach_probe(str(path)) == 1
    path.unlink()
    relay = socket.socket(socket.AF_UNIX)
    relay.bind(str(path))
    relay.listen(4)
    assert rt.attach_probe(str(path)) == 0
    relay.close()
    server.close()


# --- stage 2: limits --------------------------------------------------------------------------------------

def test_limits_through_podman_update(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    record = {"run": "claude-1", "backend": "podman", "pod": "agent-claude-1", "podman_url": None}
    calls, lines = [], []
    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(devices, "limits_cgroup", lambda *a, **k: pytest.fail("no cgroup of the Mac"))
    assert devices.set_limits(record, memory="4g", cpus="2", pids="none", out=lines.append, runner=runner) == "podman"
    assert calls == [["podman", "update", "--memory", "4g", "--memory-swap", "4g", "--cpus", "2", "--pids-limit", "-1",
                      "agent-claude-1-agent"]]
    assert "podman update agent-claude-1-agent" in lines[0]
    with pytest.raises(ValueError, match="refused the limits"):
        devices.set_limits(record, memory="none", runner=lambda c, **k: SimpleNamespace(returncode=125, stdout="",
                                                                                      stderr="Error: no such container"))
    inspect = [{"HostConfig": {"Memory": 4 << 30, "NanoCpus": 1_500_000_000, "PidsLimit": 0}}]
    monkeypatch.setattr(devices.subprocess, "run", lambda command, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(inspect)) if command[1] == "inspect" else pytest.fail(command))
    assert devices.current_limits(record) == {"memory": 4 << 30, "memory_high": None, "cpus": 1.5, "pids": None}


# --- stage 2 and 3: what a start on a Mac asks Podman for -------------------------------------------------------

def test_a_start_on_macos_keeps_its_sockets_in_the_vm(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(s, "podman_machine", lambda runner=None: {})
    a = s.parse_args(["claude", "--workspace", str(tmp_path), "--allow-host-port", "11434"])
    a.run_id, a.tmux_socket_dir, a.pod_name = "claude-1a2b3c4d", tmp_path / "tmux", "agent-claude-1a2b3c4d"
    assert s.on_machine(a) and "-T,11434" not in s.pod_network(a)
    _, run = s.pod_commands(a, "agent-claude-1a2b3c4d", tmp_path, tmp_path)
    assert "agent-claude-1a2b3c4d-agent" in run and f"--tmpfs={s.TMUX_DIR}:rw,nosuid,nodev,mode=0700" in run
    assert not any(str(tmp_path / "tmux") in arg for arg in run)
    a.pod_name = None
    assert s.pod_commands(a, "agent-claude-1a2b3c4d", tmp_path, tmp_path)[1].count("--name") == 1
    monkeypatch.setattr(sys, "platform", "linux")
    assert not s.on_machine(a) and "-T,11434" in s.pod_network(a)
    assert "--name" not in s.pod_commands(a, "agent-claude-1a2b3c4d", tmp_path, tmp_path)[1]


def test_the_firewall_lets_only_the_supervisors_marked_connections_reach_the_mac():
    rules = s.firewall_rules([], [], proxy={"port": 18080, "loopback": [11434]}, machine_host="192.168.127.254")
    mark = f"meta mark {rt.HOST_RELAY_MARK:#x}"
    assert f"  {mark} ip daddr 192.168.127.254 meta l4proto tcp accept" in rules.splitlines()
    assert f"  {mark} return" in rules.splitlines()  # not redirected into the proxy
    assert rules.index(f"{mark} ip daddr") < rules.index("jump runtime")
    assert mark not in s.firewall_rules([], [], proxy={"port": 18080, "loopback": [11434]})


def test_machine_host(monkeypatch):
    assert s.machine_host() == "192.168.127.254"
    monkeypatch.setenv("SANDBOX_AI_MACHINE_HOST", "192.168.5.2")
    assert s.machine_host() == "192.168.5.2"
    monkeypatch.setenv("SANDBOX_AI_MACHINE_HOST", "host; rm")
    with pytest.raises(ValueError, match="IPv4"):
        s.machine_host()


def test_register_run_writes_the_token_on_macos(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(s, "workspace_checks", lambda a, session: {})
    for platform, expected in (("darwin", True), ("linux", False)):
        monkeypatch.setattr(sys, "platform", platform)
        a = s.parse_args(["claude", "--workspace", str(tmp_path)])
        directory = s.register_run(a, f"claude-{platform}", tmp_path)
        token = directory / rt.EXEC_TOKEN
        assert token.exists() is expected
        record = json.loads((directory / "run.json").read_text())
        assert ("container" in record) is expected
        if expected:
            assert len(token.read_text()) == 64 and token.stat().st_mode & 0o777 == 0o600
            assert record["container"] == f"agent-claude-{platform}-agent"
            assert rt.exec_token(token) == token.read_text()


def test_port_relay_connects_to_the_mac(monkeypatch):
    """PortRelay with an upstream (macOS): a TCP connection to the Mac's address, marked for the firewall."""
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)
    marks = []
    real = socket.socket.setsockopt

    class Marked(socket.socket):
        def setsockopt(self, level, name, value, *rest):
            if name == getattr(socket, "SO_MARK", 36):
                marks.append(value)
                return None  # setting a mark needs CAP_NET_ADMIN, which a test may not have
            return real(self, level, name, value, *rest)
    monkeypatch.setattr(rt.socket, "socket", Marked)
    relay = rt.PortRelay(11434, "/nonexistent", listen_port=0, upstream=("127.0.0.1", upstream.getsockname()[1]))
    try:
        port = relay.listeners[0].getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            server, _ = upstream.accept()
            client.sendall(b"ping")
            assert server.recv(4) == b"ping"
            server.sendall(b"pong")
            assert client.recv(4) == b"pong"
            server.close()
        assert marks == [rt.HOST_RELAY_MARK]
    finally:
        relay.close()
        upstream.close()


def test_runtime_network_relays_to_the_mac_during_the_run():
    made = []
    network = rt.RuntimeNetwork({"loopback": []}, None, lambda script: None)
    network.relay_dir, network.upstream_host = Path("/sandbox/control"), "192.168.127.254"
    network.relay_factory = lambda port, directory, upstream=None: made.append((port, upstream)) or \
        SimpleNamespace(close=lambda: None)
    network.change({"op": "allow", "host_ports": [8080]})
    assert made == [(8080, ("192.168.127.254", 8080))]
