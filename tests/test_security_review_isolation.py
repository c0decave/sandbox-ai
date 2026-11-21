"""Security review B (SAI-01, -02, -06, -07, -09, -10, -11, -25): what a run's isolation is made of besides the
system call filters (tests/test_security_review_seccomp.py), and what the human sees of it.

- every folder of the host is mounted nosuid and nodev in every run, and the guard looks at the flags as they are
  inside Firejail (SAI-06);
- Firejail's own folder in the agent's HOME is the sandbox's root's alone before Firejail starts, and hidden in it
  (SAI-11);
- the disks behind what a run writes are watched: said in the modes up to medium, the run ended in high and
  paranoid (SAI-25);
- the container's confinement by AppArmor or SELinux is the administrator's to name (policy "lsm"); the supervisor
  starts nothing outside it (SAI-02);
- the connection proxy has no capability in any set and the sandbox's filter (SAI-10);
- the run's manifest says what the supervisor found, without a path of the host (SAI-01, SAI-07);
- `sandbox-ai doctor`, a start's notes and the dry run say it, through ./sandbox-ai.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import test_feature_matrix as fm

s = fm.s
rt = s.RUNTIME
ROOT = Path(__file__).resolve().parents[1]
CLI = str(ROOT / "sandbox-ai")
KNOWN = os.uname().machine in rt.SECCOMP_MACHINES


def parsed(*options, agent="claude"):
    return s.parse_args([agent, "--no-config", *options])


def dry_run(tmp_path, *options, env=None):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return subprocess.run([CLI, "claude", "--no-config", "--no-auth", "--workspace", str(work), *options, "--dry-run"],
                          capture_output=True, text=True, timeout=180,
                          env={**os.environ, **(env or {})})


# --- through ./sandbox-ai -------------------------------------------------------------------------------------------

def test_e2e_a_dry_run_says_the_runs_isolation(tmp_path):
    done = dry_run(tmp_path, env={"SANDBOX_AI_SYSCALL_FILTER": ""})
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)["isolation"]
    assert plan["syscall_filter"] == "on" and plan["strict"] is False and plan["lsm"] == "unconfined"
    assert plan["host_folders"] == {"/root": ["nosuid", "nodev"], "/work": ["nosuid", "nodev"]}
    assert plan["disk"]["act"] == "warn" and "/root" in plan["disk"]["paths"] and "/work" in plan["disk"]["paths"]
    assert plan["firejail"][-2:] == ["--restrict-namespaces", "--protocol=unix,inet,inet6,netlink"]
    if KNOWN:
        assert plan["filters"]["sandbox"]["new_namespaces"] == "refused"
        assert plan["filters"]["sandbox"]["socket_families"] == ["unix", "inet", "inet6", "netlink"]
        assert plan["filters"]["container"]["new_namespaces"] == "allowed"
        assert len(plan["filters"]["sandbox"]["sha256"]) == 64
    # an ordinary start has nothing to say about it
    assert "SYSCALL_FILTER" not in done.stderr and "not confined" not in done.stderr


def test_e2e_a_strict_mode_says_what_this_host_does_not_give_it_and_ends_a_run_on_a_full_disk(tmp_path):
    done = dry_run(tmp_path, "--mode", "high", env={"SANDBOX_AI_SYSCALL_FILTER": ""})
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)["isolation"]
    assert plan["strict"] is True and plan["disk"]["act"] == "stop" and plan["disk"]["reserve"] == 512 << 20
    assert "512 MiB" in done.stderr and "security mode high" in done.stderr
    if sys.platform != "darwin":
        assert "not confined by" in done.stderr and '"lsm"' in done.stderr


def test_e2e_the_filters_are_switched_off_only_outside_the_strict_modes(tmp_path):
    off = {"SANDBOX_AI_SYSCALL_FILTER": "off"}
    medium = dry_run(tmp_path, env=off)
    assert medium.returncode == 0, medium.stderr
    assert "warning: SANDBOX_AI_SYSCALL_FILTER=off" in medium.stderr
    plan = json.loads(medium.stdout)["isolation"]
    assert plan["syscall_filter"] == "off" and plan["filters"] == {}
    assert not [option for option in plan["firejail"] if option.startswith(("--restrict", "--protocol"))]
    for mode in ("high", "paranoid"):
        strict = dry_run(tmp_path, "--mode", mode, env=off)
        assert strict.returncode != 0 and f"widens the security mode {mode}" in strict.stderr, strict.stderr
        assert strict.stdout.strip() == ""
    wrong = dry_run(tmp_path, env={"SANDBOX_AI_SYSCALL_FILTER": "sometimes"})
    assert wrong.returncode != 0 and "one of on, off" in wrong.stderr


def test_e2e_doctor_says_the_isolation_of_this_host():
    done = subprocess.run([CLI, "doctor", "--json"], capture_output=True, text=True, timeout=300,
                          env={**os.environ, "SANDBOX_AI_SYSCALL_FILTER": ""})
    entries = {entry["check"]: entry for entry in json.loads(done.stdout)}
    for title in ("Isolation: system calls", "Isolation: io_uring", "Isolation: security module", "Isolation: disk"):
        assert title in entries, sorted(entries)
        assert entries[title]["group"] == "Engine"
    if KNOWN:
        assert entries["Isolation: system calls"]["status"] == "ok"
    assert "io_uring" in entries["Isolation: io_uring"]["detail"]
    assert "policy.json" in entries["Isolation: security module"]["detail"] or \
        entries["Isolation: security module"]["status"] != "ok"


# --- the mounts -----------------------------------------------------------------------------------------------------

def test_every_host_folder_of_every_run_is_nosuid_and_nodev(tmp_path):
    """SAI-06: before, only --nested-containers had nosuid. The devices' folders keep their devices."""
    data, logs = tmp_path / "data", tmp_path / "logs"
    for folder in (data, logs, tmp_path / "w", tmp_path / "second"):
        folder.mkdir()
    a = parsed("--workspace", str(tmp_path / "w"), "--workspace", str(tmp_path / "second"), "--mount",
               f"{data}:/mnt/data:ro", "--log-dir", str(logs))
    _, run = s.pod_commands(a, "agent-x", tmp_path / "session", tmp_path / "home")
    binds = [value for value in run if value.startswith("type=bind,")]
    assert len(binds) >= 8
    for value in binds:
        options = value.split(",")
        assert "nosuid" in options and "nodev" in options, value
    assert any(",dst=/root,rw,nodev,nosuid" in value for value in binds)
    assert any(",dst=/mnt/data,ro,nodev,nosuid" in value for value in binds)
    # the tmpfs of the run had them already
    for value in run:
        if value.startswith("--tmpfs="):
            assert "nosuid" in value and "nodev" in value, value


def test_the_guard_looks_at_the_flags_inside_firejail(monkeypatch, tmp_path, capsys):
    """After all of Podman's and Firejail's mounts: a host folder without nosuid is said, and in a strict mode the
    agent does not start."""
    table = ("1 0 0:1 / / ro - overlay overlay rw\n"
             "2 1 8:1 /home/u/p /work rw,nosuid,nodev - ext4 /dev/sda rw\n"
             "3 1 8:1 /home/u/h /root rw,nodev - ext4 /dev/sda rw\n")
    executed = []
    config = tmp_path / "config.json"
    monkeypatch.setattr(rt, "SESSION_CONFIG", config)
    monkeypatch.setattr(rt, "verify_inner", lambda before, current: None)
    monkeypatch.setattr(rt.os, "chdir", lambda path: None)
    monkeypatch.setattr(rt.os, "execvpe", lambda cmd, args, env: executed.append(args))
    real = rt.check_mounts
    monkeypatch.setattr(rt, "check_mounts", lambda settings: real(settings, table))
    flags = {"/root": ["nosuid", "nodev"], "/work": ["nosuid", "nodev"]}
    base = {"probe": False, "env": {}, "command": ["claude"], "tmux": None}
    config.write_text(json.dumps({**base, "isolation": {"mounts": flags}}))
    rt.inner_guard({})
    assert executed == [["claude"]] and "/root lacks nosuid" in capsys.readouterr().err
    config.write_text(json.dumps({**base, "isolation": {"mounts": flags, "strict": True}}))
    with pytest.raises(RuntimeError, match="/root lacks nosuid"):
        rt.inner_guard({})
    assert executed == [["claude"]]


def test_the_guard_starts_nothing_when_its_filter_does_not_hold(monkeypatch, tmp_path):
    executed, relays = [], []
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"probe": False, "env": {}, "command": ["claude"], "tmux": None,
                                  "cairn": {"socket": "x"}}))
    monkeypatch.setattr(rt, "SESSION_CONFIG", config)
    monkeypatch.setattr(rt, "verify_inner", lambda before, current: None)
    monkeypatch.setattr(rt.os, "chdir", lambda path: None)
    monkeypatch.setattr(rt.os, "execvpe", lambda cmd, args, env: executed.append(args))
    monkeypatch.setattr(rt, "start_cairn_relay", lambda *a, **kw: relays.append(a))
    monkeypatch.setattr(rt, "SANDBOX_PROCESS", True)
    monkeypatch.setattr(rt, "seccomp_install", Mock(side_effect=OSError(22, "the kernel did not take the filter")))
    monkeypatch.setattr(rt, "seccomp_probe", Mock(side_effect=AssertionError("nothing was installed")))
    if not KNOWN:
        pytest.skip("no filter for this machine: the agent runs with Firejail's alone, which is said")
    with pytest.raises(RuntimeError, match="does not hold"):
        rt.inner_guard({})
    assert executed == [] and relays == []  # not Cairn's relay either: it runs the agents' commands


# --- Firejail's own folder ------------------------------------------------------------------------------------------

def test_firejails_folder_in_the_agents_home_is_sealed_before_firejail_starts(tmp_path, monkeypatch):
    """SAI-11: a kept HOME is the agent's to write; what it leaves at ~/.config/firejail never reaches a Firejail."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "default.profile").write_text("noblacklist /\n")
    (home / ".config" / "firejail").symlink_to(hostile)  # what an earlier run left
    assert rt.seal_firejail_config(home) is None
    sealed = home / ".config" / "firejail"
    assert sealed.is_dir() and not sealed.is_symlink() and list(sealed.iterdir()) == []
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o700 and sealed.stat().st_uid == os.geteuid()
    assert (hostile / "default.profile").exists()  # nothing was followed
    monkeypatch.setattr(rt, "FIREJAIL_CONFIG", sealed)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=""))
    assert f"--blacklist={sealed}" in rt.firejail_command({})
    assert "--noprofile" in rt.firejail_command({})


def test_the_supervisor_seals_first_and_a_strict_mode_does_not_start_unsealed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rt, "SANDBOX_PROCESS", True)
    monkeypatch.setattr(rt, "write_manifest", lambda manifest: [])
    monkeypatch.setattr(rt, "isolation_manifest", lambda *a: {"lsm": {"label": None}, "proxy": None})
    reason = "/root/.config is no folder (a link or a file): the folder it leads to is not sealed"
    strict = rt.isolation_settings({"isolation": {"strict": True, "sealed": reason}})
    assert "is not sealed" in rt.isolation_ready({}, strict, None, ["firejail"]) and "--no-keep" in \
        rt.isolation_ready({}, strict, None, ["firejail"])
    plain = rt.isolation_settings({"isolation": {"sealed": reason}})
    assert rt.isolation_ready({}, plain, None, ["firejail"]) is None
    assert "--noprofile" in capsys.readouterr().err
    source = Path(rt.__file__).read_text()
    boot = source[source.index("def inner_bootstrap("):source.index("def start_gui(")]
    assert boot.index("seal_firejail_config()") < boot.index("firejail_command(")


# --- the supervisor -------------------------------------------------------------------------------------------------

def bootstrap(monkeypatch, tmp_path, isolation, ready=None, container=None):
    """inner_bootstrap with the machine's side faked: what it runs, in order."""
    order = []
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"isolation": isolation}))
    monkeypatch.setattr(rt, "SESSION_CONFIG", config)
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path / "absent-log")
    monkeypatch.setattr(rt, "CONTROL_DIR", tmp_path / "absent-control")
    monkeypatch.setattr(rt, "MCP_RUN_DIR", tmp_path / "mcp-run")
    monkeypatch.setattr(rt, "GUI_CONFIG", tmp_path / "absent-gui.json")  # (not those of a sandbox running the tests)
    monkeypatch.setattr(rt, "AUDIO_CONFIG", tmp_path / "absent-audio.json")
    monkeypatch.setattr(rt.os, "getpid", lambda: 1)
    monkeypatch.setattr(rt.ctypes, "CDLL", lambda *a, **kw: Mock(prctl=Mock(return_value=0)))
    monkeypatch.setattr(rt, "SANDBOX_PROCESS", True)
    monkeypatch.setattr(rt, "RUN_ISOLATION", None)

    def own(profile, settings, **kw):
        order.append(f"filter:{profile}")
        if isinstance(container, Exception):
            raise container
        return container
    monkeypatch.setattr(rt, "own_filter", own)
    monkeypatch.setattr(rt, "seal_firejail_config", lambda: order.append("seal"))
    monkeypatch.setattr(rt.subprocess, "run", lambda cmd, **kw: order.append(cmd[0]) or SimpleNamespace(stdout=""))
    monkeypatch.setattr(rt, "namespace_state", lambda: {})
    monkeypatch.setattr(rt, "firejail_command", lambda *a, **kw: order.append("firejail_command") or ["firejail"])
    monkeypatch.setattr(rt, "isolation_ready", lambda cfg, settings, held, cmd, proxy:
                        order.append(("ready", settings.get("sealed"), held)) or ready)
    monkeypatch.setattr(rt, "safeguard_event", lambda *a, **kw: order.append("safeguard"))
    child = Mock(pid=4242)
    monkeypatch.setattr(rt.subprocess, "Popen", lambda cmd, **kw: order.append("popen") or child)
    monkeypatch.setattr(rt, "oom_first", lambda pid: None)
    monkeypatch.setattr(rt, "wait_child", lambda process: 0)
    watched = []
    monkeypatch.setattr(rt, "watch_disk", lambda disk, process, stop: watched.append(disk))
    return order, watched


def test_the_supervisor_filters_itself_first_and_starts_firejail_only_when_the_isolation_is_ready(monkeypatch,
                                                                                                 tmp_path):
    disk = {"paths": ["/root"], "reserve": 1, "files": 1, "act": "warn"}
    order, watched = bootstrap(monkeypatch, tmp_path, {"disk": disk}, container={"profile": "container"})
    assert rt.inner_bootstrap() == 0
    assert order[0] == "filter:container" and order.index("filter:container") < order.index("nft")
    assert order.index("seal") < order.index("firejail_command") < order.index("popen")
    assert ("ready", True, {"profile": "container"}) in order
    for _ in range(50):
        if watched:
            break
        time.sleep(0.02)
    assert watched == [disk]
    assert rt.RUN_ISOLATION["syscall_filter"] == "on"


def test_the_supervisor_starts_nothing_without_what_the_isolation_needs(monkeypatch, tmp_path, capsys):
    # the isolation is not ready (the policy's confinement is missing, the proxy has capabilities, ...)
    order, _ = bootstrap(monkeypatch, tmp_path, {}, ready="the administrator's policy asks for the apparmor "
                                                           "confinement sandbox-ai, and the sandbox runs as unconfined")
    assert rt.inner_bootstrap() == 1
    assert "popen" not in order and "safeguard" in order
    assert "runs as unconfined" in capsys.readouterr().err
    # a strict mode whose container filter the kernel does not take: not even the firewall is loaded
    order, _ = bootstrap(monkeypatch, tmp_path, {"strict": True},
                         container=RuntimeError("sandbox-ai's own system call filter (container) does not hold: x"))
    assert rt.inner_bootstrap() == 1
    assert order == ["filter:container"] and "does not hold" in capsys.readouterr().err


def test_the_supervisor_holds_the_run_to_the_administrators_confinement(monkeypatch, tmp_path):
    """SAI-02: the policy's lsm - the container is started in it, and the supervisor that finds itself outside
    starts nothing (an engine that ignored the option, a profile that is not loaded)."""
    proc = tmp_path / "proc"
    (proc / "self" / "attr").mkdir(parents=True)
    (proc / "self" / "status").write_text("CapEff:\t00000000a80425fb\nNoNewPrivs:\t0\n")
    (proc / "self" / "attr" / "current").write_text("unconfined\n")
    monkeypatch.setattr(rt, "SANDBOX_PROCESS", True)
    monkeypatch.setattr(rt, "firejail_version", lambda: "0.9.72")
    monkeypatch.setattr(rt, "write_manifest", lambda manifest: [])
    real = rt.isolation_manifest
    monkeypatch.setattr(rt, "isolation_manifest", lambda cfg, settings, held, cmd, pid: real(cfg, settings, held, cmd,
                                                                                            pid, proc))
    wanted = rt.isolation_settings({"isolation": {"lsm": {"apparmor": "sandbox-ai"}}})
    assert "runs as unconfined" in rt.isolation_ready({}, wanted, None, ["firejail"])
    (proc / "self" / "attr" / "current").write_text("sandbox-ai (enforce)\n")
    assert rt.isolation_ready({}, wanted, None, ["firejail"]) is None
    # the launcher's side: the option of the container, and Bubblewrap, which has none
    a = parsed("--workspace", str(tmp_path))
    _, plain = s.pod_commands(a, "agent-x", tmp_path / "session", tmp_path / "home")
    assert "--security-opt=apparmor=unconfined" in plain and "--security-opt=label=disable" in plain
    a.policy_lsm = {"apparmor": "sandbox-ai"}
    _, run = s.pod_commands(a, "agent-x", tmp_path / "session", tmp_path / "home")
    assert "--security-opt=apparmor=sandbox-ai" in run and "--security-opt=apparmor=unconfined" not in run
    assert s.isolation_config(a)["lsm"] == {"apparmor": "sandbox-ai"}
    a.policy_lsm = {"selinux": "sandbox_ai_t"}
    _, run = s.pod_commands(a, "agent-x", tmp_path / "session", tmp_path / "home")
    assert "--security-opt=label=type:sandbox_ai_t" in run and "--security-opt=label=disable" not in run
    assert "--security-opt=seccomp=unconfined" in run  # (the filters are the supervisor's own: own_filter)


def test_the_policy_takes_an_lsm_and_bubblewrap_refuses_a_start_under_it(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    real_lstat, real_stat = os.lstat, os.stat
    owned = lambda info: os.stat_result((info.st_mode & ~0o022, *info[1:4], 0, 0, *info[6:]))  # noqa: E731
    monkeypatch.setattr(s.os, "lstat", lambda path, **kw: owned(real_lstat(path, **kw)))
    monkeypatch.setattr(s.os, "stat", lambda path, **kw: owned(real_stat(path, **kw)))
    policy.write_text(json.dumps({"version": 1, "lsm": {"apparmor": "sandbox-ai"}}))
    assert s.load_policy(policy)["lsm"] == {"apparmor": "sandbox-ai"}
    for wrong in ("sandbox-ai", {"apparmor": "unconfined"}, {"apparmor": "a", "selinux": "b"}, {"smack": "x"}):
        policy.write_text(json.dumps({"version": 1, "lsm": wrong}))
        with pytest.raises(ValueError, match="lsm is"):
            s.load_policy(policy)
    a = parsed("--workspace", str(tmp_path), "--backend", "bwrap")
    s.check_policy(a, {"version": 1, "lsm": {"apparmor": "sandbox-ai"}}, policy)
    assert a.policy_lsm == {"apparmor": "sandbox-ai"}
    with pytest.raises(ValueError, match="Bubblewrap cannot apply"):
        s.validate(a)


# --- the connection proxy -------------------------------------------------------------------------------------------

def test_the_proxy_drops_every_capability_set_and_is_held_to_it(monkeypatch):
    """SAI-10: the bounding set first, then the groups and the user, its memory not another process's to read, no
    privilege to gain - and then what the kernel says of it is looked at, and it gets the sandbox's filter."""
    calls = []
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(rt.os, name, lambda *args, n=name: calls.append(n))
    libc = Mock()
    libc.prctl.side_effect = lambda *args: calls.append(("prctl", args[0], args[1])) or 0
    monkeypatch.setattr(rt.ctypes, "CDLL", lambda *a, **kw: libc)
    confined = []
    monkeypatch.setattr(rt, "proxy_confined", confined.append)
    monkeypatch.setattr(rt, "SANDBOX_PROCESS", True)
    settings = rt.isolation_settings({"isolation": {"strict": True}})
    rt.drop_to_proxy_user(settings)
    dropped = [call[2] for call in calls if isinstance(call, tuple) and call[1] == 24]
    assert dropped == list(range(64))  # PR_CAPBSET_DROP for every capability there can be
    rest = calls[64:]
    assert rest == ["setgroups", "setresgid", "setresuid", ("prctl", 4, 0), ("prctl", 38, 1)]
    assert confined == [settings]
    # a loaded file (the launcher, a test) is never confined
    monkeypatch.setattr(rt, "SANDBOX_PROCESS", False)
    rt.drop_to_proxy_user(settings)
    assert confined == [settings]


# --- the disk -------------------------------------------------------------------------------------------------------

def test_a_run_is_ended_before_the_disk_behind_it_is_full_in_a_strict_mode(tmp_path):
    """SAI-25, with a real process and the real disk: a reserve no disk has left ends the run; the same reserve in
    an ordinary mode is said and the run goes on."""
    events, said = [], []
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"])
    try:
        huge = {"paths": [str(tmp_path), str(tmp_path / "absent")], "reserve": 1 << 60, "files": 0}
        stop = threading.Event()
        warned = threading.Thread(target=rt.watch_disk, args=({**huge, "act": "warn"}, child, stop),
                                  kwargs={"event": lambda *record: events.append(record), "say": said.append})
        real = rt.DISK_SECONDS
        rt.DISK_SECONDS = 0.05
        try:
            warned.start()
            deadline = time.monotonic() + 10
            while not events and time.monotonic() < deadline:
                time.sleep(0.02)
            stop.set()
            warned.join(5)
            assert len(events) == 1 and events[0][0] == "guard" and str(tmp_path) in events[0][1]
            assert child.poll() is None and said == []
            result = rt.watch_disk({**huge, "act": "stop"}, child, threading.Event(),
                                   event=lambda *record: events.append(record), say=said.append)
        finally:
            rt.DISK_SECONDS = real
        assert result == "stopped" and child.wait(10) != 0
        assert "the run is ended" in said[0] and len(events) == 2
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# --- the manifest ---------------------------------------------------------------------------------------------------

def test_the_manifest_of_this_process_names_no_host_path(tmp_path, monkeypatch):
    """SAI-01, SAI-07: written where the agent cannot reach, readable by the human; the mounts by their place in the
    sandbox, never by their source (which names your folders and your user)."""
    monkeypatch.setattr(rt, "firejail_version", lambda: "0.9.72")
    monkeypatch.setattr(rt, "SESSION_CONFIG", tmp_path / "session" / "config.json")
    settings = rt.isolation_settings({"isolation": {"mounts": {"/work": ["nosuid", "nodev"]}}})
    manifest = rt.isolation_manifest({"mode": {"name": "paranoid"}}, settings, None,
                                     ["firejail", "--noprofile", "--seccomp"], os.getpid())
    assert manifest["kernel"]["release"] == os.uname().release
    assert manifest["supervisor"]["NoNewPrivs"] in ("0", "1") and manifest["proxy"]["CapEff"]
    assert manifest["mounts"] and all(set(entry) == {"point", "type", "options"} for entry in manifest["mounts"])
    sources = set()
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split(" ")
        root, source = fields[3], fields[fields.index("-", 6) + 2]
        sources |= {value for value in (root, source) if value.count("/") >= 3}
    points = {entry["point"] for entry in manifest["mounts"]}
    text = json.dumps(manifest)
    for value in sources - points:
        if not any(value == point or point.startswith(value + "/") for point in points):
            assert json.dumps(value)[1:-1] not in text, value
    control = tmp_path / "control"
    control.mkdir()
    written = rt.write_manifest(manifest, (control, tmp_path / "no-log"))
    assert written == [control / "isolation.json"] and json.loads(written[0].read_text())["mode"] == "paranoid"


# --- doctor ---------------------------------------------------------------------------------------------------------

def test_doctor_says_what_only_the_host_can_add(tmp_path, monkeypatch):
    d = s.load_doctor_module() if hasattr(s, "load_doctor_module") else None
    if d is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("doctor_for_isolation", ROOT / "sandbox" / "doctor.py")
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
    # the filters
    assert d.check_syscall_filters("x86_64", {})[0] == "ok"
    status, _, detail, hint = d.check_syscall_filters("riscv64", {})
    assert status == "warn" and "riscv64" in detail and "high and paranoid do not start" in hint
    assert d.check_syscall_filters("aarch64", {"SANDBOX_AI_SYSCALL_FILTER": "off"})[0] == "warn"
    # io_uring: refused in every sandbox whatever the host says; the host's own switch is named with its command
    switch = tmp_path / "io_uring_disabled"
    assert "no switch" in d.check_io_uring(switch)[2]
    switch.write_text("0\n")
    status, _, detail, _ = d.check_io_uring(switch)
    assert status == "ok" and "sudo sysctl -w kernel.io_uring_disabled=2" in detail
    switch.write_text("2\n")
    assert "disabled for the whole host" in d.check_io_uring(switch)[2]
    # the security module
    status, title, detail, _ = d.check_lsm({}, ["apparmor"])
    assert status == "ok" and title == "Isolation: security module" and "AppArmor" in detail
    assert "sudo apparmor_parser -r" in detail and '"lsm"' in detail
    assert d.check_lsm({"lsm": {"apparmor": "sandbox-ai"}}, ["apparmor"])[0] == "ok"
    status, _, detail, hint = d.check_lsm({"lsm": {"selinux": "sandbox_ai_t"}}, ["apparmor"])
    assert status == "warn" and "no sandbox starts so" in hint
    assert d.check_lsm({"lsm": {"apparmor": "sandbox-ai"}}, ["apparmor"], backend="bwrap")[0] == "warn"
    assert "neither AppArmor nor SELinux" in d.check_lsm({}, [])[2]
    # the disk
    roomy = lambda path: SimpleNamespace(f_bavail=50 << 18, f_frsize=4096)  # noqa: E731
    full = lambda path: SimpleNamespace(f_bavail=100 << 8, f_frsize=4096)   # noqa: E731
    status, _, detail, _ = d.check_disk(tmp_path, roomy)
    assert status == "ok" and "50 GiB free" in detail and "quota" in detail and "xfs_quota" in detail
    status, _, detail, hint = d.check_disk(tmp_path / "not" / "there" / "yet", full)
    assert status == "warn" and "100 MiB free" in detail and "512 MiB" in hint
    # Bubblewrap: the host's Firejail
    assert d.check_firejail_version(lambda cmd: (0, "firejail version 0.9.72\n"))[0] == "ok"
    assert d.check_firejail_version(lambda cmd: (0, "firejail version 0.9.66\n"))[0] == "warn"
    assert d.check_firejail_version(lambda cmd: (127, ""))[0] == "skip"
    assert d.group_of("Isolation: disk") == "Engine"


# --- what the host renders (SAI-17) ---------------------------------------------------------------------------------

def test_the_html_report_carries_its_own_policy_and_shows_hostile_text_as_text():
    """Every text of the logs is escaped where the report is made (tests/test_report.py); the page says itself that
    nothing is loaded and no script runs - whatever opens it, and if a text ever is not escaped."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("report_for_isolation", ROOT / "sandbox" / "report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    hostile = '"><script>fetch("https://evil.example/")</script><img src=x onerror=alert(1)>\x1b]52;c;QQ==\x07'
    page = report.html_document([("heading", 1, hostile, "a"), ("para", [("t", hostile), ("c", hostile)]),
                                 ("table", ["h" + hostile], [[[("t", hostile)]]])], hostile)
    head = page[:page.index("</head>")]
    assert '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; ' \
           'base-uri \'none\'; form-action \'none\'">' in head
    assert head.index("Content-Security-Policy") < head.index("<title>")
    assert "<script" not in page and "<img" not in page and "onerror=alert(1)&gt;" in page
    assert " src=" not in page.replace("&lt;img src=x", "") and 'href="http' not in page
