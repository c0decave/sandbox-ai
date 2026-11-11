"""--nested-containers (the agent runs containers itself) and --tmp-size (the size of /tmp): the options, the command
line they give the agent's container, where they are refused (Bubblewrap, --podman-url, behind Tor, the strict security
modes, the administrator's policy, no /dev/net/tun), the dry-run plan, the session's configuration and environment,
and the supervisor's part (runtime.py): the agent's user namespace and its ID map, which never maps the proxy's,
PulseAudio's or tor's ID, Firejail's filter, the guard entering the namespace. tests/test_nested_containers_live.py
runs a real sandbox with Podman inside."""
import json
import os
from pathlib import Path
import random
import time

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

s = fm.s
rt = s.RUNTIME
RESERVED = [1, 2, 950]  # the proxy's, PulseAudio's and tor's IDs
SUBUIDS = [(0, 1000, 1), (1, 100000, 65536)]  # rootless Podman's own map: your user, then 65536 subordinate IDs


@pytest.fixture
def devices(monkeypatch):
    """/dev/fuse and /dev/net/tun as this host had them (the test machine may lack them); returns the set to change."""
    present = set(s.NESTED_DEVICES)
    real = os.path.exists
    monkeypatch.setattr(s.os.path, "exists", lambda path: str(path) in present if str(path) in s.NESTED_DEVICES
                        else real(path))
    return present


def parsed(*options, agent="claude"):
    return s.parse_args([agent, "--no-config", *options])


# --- the options --------------------------------------------------------------------------------------------------

def test_the_options_have_short_forms_topics_and_count_as_weakening():
    a = s.parse_args(["claude", "-ct", "-ts", "6g"])
    assert a.nested_containers is True and a.tmp_size == "6g"
    groups = dict(s.HELP_GROUPS)
    assert "--nested-containers" in groups["Engine"] and "--tmp-size" in groups["Hardware and limits"]
    assert {"--nested-containers", "--tmp-size"} <= s.MODES.WEAKENING_OPTIONS
    assert s.parse_args(["claude"]).nested_containers is False and s.parse_args(["claude"]).tmp_size is None
    text = s.build_parser().format_help()
    assert "-ct, --nested-containers" in text and "-ts, --tmp-size SIZE" in text


def test_the_reserved_ids_are_the_proxys_pulseaudios_and_tors():
    assert s.nested_reserved_ids() == RESERVED
    assert rt.PROXY_UID == 1 and rt.AUDIO_UID == 2 and s.load_vpn_module().runtime().TOR_UID == 950


# --- the command line ---------------------------------------------------------------------------------------------

def test_the_container_gets_setfcap_and_the_devices_and_nothing_without_the_option(devices):
    session, home = Path("/tmp/session"), Path("/tmp/home")
    _, plain = s.pod_commands(parsed(), "agent-x", session, home)
    _, run = s.pod_commands(parsed("--nested-containers"), "agent-x", session, home)
    assert "SETFCAP" not in plain and "/dev/fuse" not in plain and "/dev/net/tun" not in plain
    assert run[run.index("SETFCAP") - 1] == "--cap-add"
    assert run[run.index("/dev/fuse") - 1] == "--device" and run[run.index("/dev/net/tun") - 1] == "--device"
    # Everything else stays as it was: the same capabilities, no privileged mode, seccomp left to Firejail.
    # (and the host's folders nosuid: test_every_host_folder_is_mounted_nosuid_in_a_nested_run)
    stripped = [x[:-len(",nosuid")] if x.startswith("type=bind,") and x.endswith(",nosuid") else x for x in run]
    for value in ("SETFCAP", "/dev/fuse", "/dev/net/tun"):
        index = stripped.index(value)
        del stripped[index - 1:index + 1]
    assert stripped == plain
    assert "--privileged" not in run and not any(x.startswith("--security-opt=unmask") for x in run)


def test_without_dev_fuse_the_images_are_stored_by_copying(devices):
    devices.discard("/dev/fuse")
    a = parsed("--nested-containers")
    assert s.nested_devices(a) == ["/dev/net/tun"] and s.nested_storage(a) == "vfs"
    files = s.nested_files(a)
    assert 'driver = "vfs"' in files["containers/storage.conf"] and "fuse-overlayfs" not in files["containers/storage.conf"]
    assert "/dev/fuse" not in s.pod_commands(a, "agent-x", Path("/tmp/s"), Path("/tmp/h"))[1]


def test_the_engines_configuration(devices):
    files = s.nested_files(parsed("--nested-containers"))
    containers, storage = files["containers/containers.conf"], files["containers/storage.conf"]
    # The containers share the agent's network namespace: its firewall and proxy (a bridge would need the
    # sandbox's NET_ADMIN, which the agent has not).
    assert 'netns = "host"' in containers and 'cgroups = "disabled"' in containers
    assert 'cgroup_manager = "cgroupfs"' in containers and "keyring = false" in containers
    assert 'driver = "overlay"' in storage and 'mount_program = "/usr/bin/fuse-overlayfs"' in storage
    assert f'graphroot = "{s.NESTED_GRAPHROOT}"' in storage and s.NESTED_GRAPHROOT.startswith("/var/tmp/")
    assert set(s.NESTED_ENV) == {"CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF", "BUILDAH_ISOLATION"}
    assert s.NESTED_ENV["CONTAINERS_CONF"] == "/sandbox/session/containers/containers.conf"


# --- --tmp-size ---------------------------------------------------------------------------------------------------

def tmpfs_options(run):
    return [x for x in run if x.startswith(("--tmpfs=/tmp:", "--tmpfs=/var/tmp:"))]


def test_tmp_size_sets_both_tmpfs_and_the_default_is_half_the_memory(monkeypatch):
    monkeypatch.setattr(s, "enforced_limits", lambda a: ({"memory": "8g"}, []))
    _, run = s.pod_commands(parsed(), "agent-x", Path("/tmp/s"), Path("/tmp/h"))
    assert tmpfs_options(run) == [f"--tmpfs=/tmp:rw,nosuid,nodev,size={4 << 30}",
                                  f"--tmpfs=/var/tmp:rw,nosuid,nodev,size={4 << 30}"]
    _, run = s.pod_commands(parsed("--tmp-size", "6g"), "agent-x", Path("/tmp/s"), Path("/tmp/h"))
    assert tmpfs_options(run) == [f"--tmpfs=/tmp:rw,nosuid,nodev,size={6 << 30}",
                                  f"--tmpfs=/var/tmp:rw,nosuid,nodev,size={6 << 30}"]
    # Also where the memory limit is not enforced (no systemd user session): the size asked for holds.
    monkeypatch.setattr(s, "enforced_limits", lambda a: ({}, []))
    assert s.tmp_mount_size(parsed("--tmp-size", "512m")) == f",size={512 << 20}"
    assert s.tmp_mount_size(parsed()) == ""


@pytest.fixture
def host_of_16g(monkeypatch):
    """This host: 16 GiB of memory, a local engine."""
    monkeypatch.setattr(s, "host_memory", lambda path=None: 16 << 30)
    monkeypatch.setattr(s, "local_engine", lambda a: True)


@pytest.mark.parametrize("options,message", [
    (["--tmp-size", "12"], "at least 16m"),
    (["--tmp-size", "16777215"], "at least 16m"),
    (["--tmp-size", "lots"], "a size such as 6g or 512m, or a number of bytes"),
    (["--tmp-size", "1.5g"], "a size such as 6g or 512m, or a number of bytes"),
    (["--tmp-size", "5g"], r"more than half the memory limit 8g: /tmp and /var/tmp each get it"),
    (["--tmp-size", "12g", "--memory", "16g"], "more than half the memory limit 16g"),
    (["--tmp-size", "4294967297"], "more than half the memory limit 8g"),  # bytes: one more than 4g
    (["--tmp-size", "5g", "--memory", "none"], r"more than a quarter of this host's memory \(16384 MiB"),
    (["--tmp-size", "2g", "--backend", "bwrap"], "needs the Podman backend"),
])
def test_tmp_size_refusals(options, message, host_of_16g):
    with pytest.raises(ValueError, match=message):
        s.check_tmp_size(parsed(*options))


def test_tmp_size_within_half_the_memory_bytes_included(host_of_16g):
    """/tmp and /var/tmp each get --tmp-size: together they stay within the memory limit (the host's half with
    --memory none). A number without a unit is bytes."""
    for options in (["--tmp-size", "4g"], ["--tmp-size", "8g", "--memory", "16g"], ["--tmp-size", "4g", "--memory",
                    "none"], ["--tmp-size", "16m"], ["--tmp-size", str(16 << 20)], ["--tmp-size", str(4 << 30)]):
        s.check_tmp_size(parsed(*options))
    assert s.tmp_mount_size(parsed("--tmp-size", str(1 << 30))) == f",size={1 << 30}"


def test_without_a_memory_limit_each_tmpfs_gets_a_quarter_of_the_host(host_of_16g, monkeypatch):
    assert s.tmp_mount_size(parsed("--memory", "none")) == f",size={4 << 30}"
    monkeypatch.setattr(s, "host_memory", lambda path=None: None)  # unreadable: the kernel's default
    assert s.tmp_mount_size(parsed("--memory", "none")) == ""
    meminfo = Path(__file__).resolve().parent / "golden"  # any directory: not a meminfo
    assert s.host_memory(meminfo) is None


# --- refusals -----------------------------------------------------------------------------------------------------

def test_refused_with_bubblewrap_podman_url_tor_and_without_a_tun_device(devices):
    with pytest.raises(ValueError, match="needs the Podman backend"):
        s.check_nested(parsed("--nested-containers", "--backend", "bwrap"))
    with pytest.raises(ValueError, match="--podman-url"):
        s.check_nested(parsed("--nested-containers", "--podman-url", "unix:///tmp/p.sock"))
    tor = parsed("--nested-containers")
    tor.vpn, tor.vpn_profile = "anon", {"type": "tor"}
    with pytest.raises(ValueError, match=r"refused behind Tor \(--vpn anon\)"):
        s.check_nested(tor)
    wireguard = parsed("--nested-containers")
    wireguard.vpn, wireguard.vpn_profile = "corp", {"type": "wireguard"}
    s.check_nested(wireguard)  # its kill switch knows the VPN's packets by a mark the agent cannot set
    devices.discard("/dev/net/tun")
    with pytest.raises(ValueError, match="needs /dev/net/tun"):
        s.check_nested(parsed("--nested-containers"))
    s.check_nested(parsed())  # without the option nothing is checked


def test_a_start_behind_tor_is_refused_after_the_vpns_own_check(m, devices, monkeypatch):
    if m.backend != "podman":
        m.refused("--nested-containers", match="needs the Podman backend")
        return

    def tor_profile(a):
        a.vpn_profile = {"type": "tor"}
    monkeypatch.setattr(m.vpn, "validate", tor_profile)
    m.refused("--nested-containers", match="refused behind Tor")


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_the_strict_modes_refuse_it(m, mode, devices):
    m.refused("--mode", mode, "--nested-containers",
              match=rf"--nested-containers widens the security mode {mode} .*choose a laxer mode: --mode medium")


def test_the_administrators_policy_can_forbid_it(m, devices):
    m.policy(forbid=["--nested-containers"])
    m.refused("--nested-containers", match=r"--nested-containers is not allowed by the administrator's policy")
    m.policy(forbid=["--tmp-size"])
    m.refused("--tmp-size", "2g", match=r"--tmp-size is not allowed by the administrator's policy")


# --- a whole start (the backend a recorder) -----------------------------------------------------------------------

def test_a_start_hands_the_supervisor_the_namespace_and_the_agent_the_engines(m, devices):
    if m.backend != "podman":
        m.refused("--nested-containers", match="needs the Podman backend")
        return
    plain = m.run()
    assert "nested" not in plain.config and "CONTAINERS_CONF" not in plain.env and plain.containers == {}
    run = m.run("--nested-containers", "--tmp-size", "3g")
    assert run.config["nested"] == {"reserved": RESERVED, "writable": ["/work", "/root"]}
    assert {key: run.env[key] for key in s.NESTED_ENV} == s.NESTED_ENV
    assert set(run.containers) == {"containers.conf", "storage.conf"}
    assert 'netns = "host"' in run.containers["containers.conf"]
    assert "SETFCAP" in run.command and "/dev/net/tun" in run.command
    assert f"--tmpfs=/tmp:rw,nosuid,nodev,size={3 << 30}" in run.command
    assert "--nested-containers: the agent may run containers" in run.err
    # The firewall and the proxy are the same as without it: nothing is opened for the containers.
    assert run.firewall == plain.firewall


def test_the_dry_run_plan_shows_it(m, devices):
    if m.backend != "podman":
        assert m.plan()["nested_containers"] is None and m.plan()["tmp_size"] is None
        return
    plain = m.plan()
    assert plain["nested_containers"] is None and plain["tmp_size"] == 4 << 30  # half of the default 8g
    plan = m.plan("--nested-containers", "--tmp-size", "3g")
    nested = plan["nested_containers"]
    assert plan["tmp_size"] == 3 << 30
    assert nested["reserved_ids"] == RESERVED and nested["capabilities_added"] == ["SETFCAP"]
    assert nested["devices"] == ["/dev/fuse", "/dev/net/tun"] and nested["storage"]["driver"] == "overlay"
    assert "mount" in nested["seccomp"] and "ptrace" not in nested["seccomp"]
    assert nested["environment"] == s.NESTED_ENV and "firewall" in nested["network"]


# --- the supervisor: the agent's user namespace -------------------------------------------------------------------

def covered(extents, side):
    """The IDs a map covers inside (SIDE 0) or outside (SIDE 1)."""
    return {value for extent in extents for value in range(extent[side], extent[side] + extent[2])}


def test_the_id_map_of_the_agents_namespace():
    extents = rt.nested_id_map(SUBUIDS, RESERVED)
    # 0 stays the agent (your user on the host); the reserved IDs inside are the two IDs above "nobody"; 950 is not
    # mapped (no third spare ID); every other ID up to nobody is itself.
    assert extents == [(0, 0, 1), (1, 65535, 2), (3, 3, 947), (951, 951, 64584)]
    assert rt.id_map_text(extents) == "0 0 1\n1 65535 2\n3 3 947\n951 951 64584\n"
    outside = covered(extents, 1)
    assert not outside & set(RESERVED) and 65534 in outside and 0 in outside
    assert covered(extents, 0) == set(range(65535)) - {950}


def test_the_reserved_ids_are_never_mapped_whatever_the_sandboxs_map():
    rng = random.Random(1234)
    for _ in range(300):
        own, inside = [(0, 1000, 1)], 1
        for _ in range(rng.randint(0, 4)):
            count = rng.randint(1, 70000)
            own.append((inside, 200000 + inside, count))
            inside += count + rng.choice([0, 0, 5])
        reserved = rng.sample(range(1, 70000), rng.randint(0, 5)) + RESERVED
        extents = rt.nested_id_map(own, reserved)
        sandbox = covered([(i, i, c) for i, _, c in own], 1)
        outside, inner = covered(extents, 1), covered(extents, 0)
        assert not outside & set(reserved), (own, reserved)
        assert outside <= sandbox  # only IDs the sandbox has
        assert len(outside) == len(inner) == sum(c for _, _, c in extents)  # one to one
        assert (0, 0, 1) == extents[0] or extents[0][:2] == (0, 0)
        assert all(not rt.mapped_outside(extents, r) for r in reserved)


def test_a_map_without_root_is_refused():
    with pytest.raises(RuntimeError, match="maps no root"):
        rt.nested_id_map([(1, 100000, 65536)], RESERVED)
    assert rt.parse_id_map("         0       1000          1\n         1     100000      65536\n") == SUBUIDS


def test_firejails_filter_lets_the_engines_mount_and_keeps_the_rest_blocked(monkeypatch):
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **kw: type("R", (), {"stdout": ""})())
    plain, nested = rt.firejail_command({}), rt.firejail_command({}, nested=True)
    assert "--seccomp" in plain and rt.NESTED_SECCOMP not in plain
    assert "--seccomp" not in nested and rt.NESTED_SECCOMP in nested
    assert [x for x in nested if x != rt.NESTED_SECCOMP] == [x for x in plain if x != "--seccomp"]
    released = rt.NESTED_SECCOMP.partition("=")[2].split(",")
    assert all(name.startswith("!") for name in released)  # exceptions to Firejail's default list, nothing else
    assert {"!mount", "!umount2", "!pivot_root"} <= set(released)
    for kept in ("ptrace", "keyctl", "bpf", "init_module", "kexec_load", "process_vm_readv", "userfaultfd"):
        assert f"!{kept}" not in released
    assert "--caps.drop=all" in nested and "--nonewprivs" in nested


class FakeLibc:
    def __init__(self, setns=0, unshare=0):
        self.calls, self._setns, self._unshare = [], setns, unshare

    def setns(self, fd, kind):
        self.calls.append(("setns", kind))
        return self._setns

    def unshare(self, kind):
        self.calls.append(("unshare", kind))
        return self._unshare

    def mount(self, source, target, fstype, flags, data):
        self.calls.append(("mount", os.fsdecode(source), os.fsdecode(target), fstype, flags.value, data))
        return 0


def fake_proc(tmp_path, uid_map, gid_map=None):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "uid_map").write_text(uid_map)
    (proc / "self" / "gid_map").write_text(gid_map or uid_map)
    return proc


def test_the_guard_enters_the_namespace_and_checks_its_map(tmp_path):
    userns = tmp_path / "userns"
    userns.write_text("")
    good = rt.id_map_text(rt.nested_id_map(SUBUIDS, RESERVED))
    libc = FakeLibc()
    rt.enter_nested_userns(RESERVED, userns, libc, fake_proc(tmp_path / "good", good))
    # Its own user namespace first, then a mount namespace of it (where the engines may mount).
    assert libc.calls == [("setns", rt.CLONE_NEWUSER), ("unshare", rt.CLONE_NEWNS)]
    with pytest.raises(RuntimeError, match=r"maps the reserved IDs \[1, 2, 950\]"):
        rt.enter_nested_userns(RESERVED, userns, FakeLibc(), fake_proc(tmp_path / "all", "0 0 65537\n"))
    with pytest.raises(RuntimeError, match=r"gid_map maps the reserved IDs \[950\]"):
        rt.enter_nested_userns(RESERVED, userns, FakeLibc(), fake_proc(tmp_path / "gid", good, good + "950 950 1\n"))
    with pytest.raises(OSError, match="cannot enter the agent's user namespace"):
        rt.enter_nested_userns(RESERVED, userns, FakeLibc(setns=-1), fake_proc(tmp_path / "fail", good))
    with pytest.raises(OSError, match="no mount namespace of the agent's own"):
        rt.enter_nested_userns(RESERVED, userns, FakeLibc(unshare=-1), fake_proc(tmp_path / "nomount", good))
    # A map that names a reserved ID stops it before any mount namespace is made.
    refused = FakeLibc()
    with pytest.raises(RuntimeError):
        rt.enter_nested_userns(RESERVED, userns, refused, fake_proc(tmp_path / "early", "0 0 3\n"))
    assert refused.calls == [("setns", rt.CLONE_NEWUSER)]


def test_the_supervisor_makes_the_namespace_keeps_it_and_hides_a_fresh_proc(tmp_path, monkeypatch):
    """nested_setup with a fake libc and /proc: the child unshares, the maps are written to its /proc entries, its
    namespace is bound to NESTED_USERNS; proc and sysfs are mounted below NESTED_HIDDEN, then covered."""
    proc = fake_proc(tmp_path, rt.id_map_text(SUBUIDS))
    monkeypatch.setattr(rt, "NESTED_DIR", tmp_path / "nested")
    monkeypatch.setattr(rt, "NESTED_USERNS", tmp_path / "nested" / "userns")
    monkeypatch.setattr(rt, "NESTED_HIDDEN", tmp_path / "nested" / "hidden")
    real_fork = os.fork
    children = []

    def fork():
        pid = real_fork()
        if pid:
            children.append(pid)
            (proc / str(pid) / "ns").mkdir(parents=True)
        return pid
    monkeypatch.setattr(rt.os, "fork", fork)
    libc = FakeLibc()
    rt.nested_setup(RESERVED, libc, proc)
    pid = children[0]
    expected = rt.id_map_text(rt.nested_id_map(SUBUIDS, RESERVED))
    assert (proc / str(pid) / "uid_map").read_text() == expected == (proc / str(pid) / "gid_map").read_text()
    mounts = [call for call in libc.calls if call[0] == "mount"]
    nested = tmp_path / "nested"
    assert mounts[0] == ("mount", str(proc / str(pid) / "ns" / "user"), str(nested / "userns"), None, rt.MS_BIND,
                         None)
    hidden = nested / "hidden"
    flags = rt.MS_NOSUID | rt.MS_NODEV | rt.MS_NOEXEC
    assert mounts[1] == ("mount", "proc", str(hidden / "proc"), b"proc", flags, None)
    assert mounts[2] == ("mount", "sysfs", str(hidden / "sys"), b"sysfs", flags | rt.MS_RDONLY, None)
    # Last: the tmpfs over them (read-only, mode 000), so that the agent sees neither.
    assert mounts[3] == ("mount", "tmpfs", str(hidden), b"tmpfs", flags | rt.MS_RDONLY, b"size=4k,mode=000")
    assert len(mounts) == 4


def test_the_supervisor_stops_when_the_namespace_cannot_be_made(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, rt.id_map_text(SUBUIDS))
    monkeypatch.setattr(rt, "NESTED_DIR", tmp_path / "nested")
    monkeypatch.setattr(rt, "NESTED_USERNS", tmp_path / "nested" / "userns")
    with pytest.raises(RuntimeError, match="could not be made"):
        rt.nested_setup(RESERVED, FakeLibc(unshare=-1), proc)


def test_the_guard_enters_it_only_for_a_nested_start(tmp_path, monkeypatch):
    entered, executed = [], []
    config = tmp_path / "config.json"
    monkeypatch.setattr(rt, "SESSION_CONFIG", config)
    monkeypatch.setattr(rt, "verify_inner", lambda before, current: None)
    monkeypatch.setattr(rt, "enter_nested_userns", lambda reserved: entered.append(reserved))
    monkeypatch.setattr(rt.os, "chdir", lambda path: None)
    monkeypatch.setattr(rt.os, "execvpe", lambda cmd, args, env: executed.append(args))
    for nested in (None, {"reserved": RESERVED}):
        config.write_text(rt.json.dumps({"probe": False, "env": {}, "command": ["claude"], "tmux": None,
                                         **({"nested": nested} if nested else {})}))
        rt.inner_guard({})
    assert entered == [RESERVED] and executed == [["claude"], ["claude"]]


def test_the_supervisor_calls_it_before_firejail_and_firejail_gets_the_filter():
    """inner_bootstrap's order, read from its source: the namespace first (a child is forked while no thread runs),
    Firejail's command with the nested filter."""
    import inspect
    source = inspect.getsource(rt.inner_bootstrap)
    assert source.index("nested_setup(") < source.index("namespace_state()") < source.index("start_loggers(")
    assert "firejail_command(before, nested=bool(nested))" in source


def test_a_host_that_restricts_user_namespaces_is_named_with_the_way_out(tmp_path, monkeypatch, devices):
    restriction = tmp_path / "apparmor_restrict_unprivileged_userns"
    monkeypatch.setattr(s, "USERNS_RESTRICTION", restriction)
    monkeypatch.setattr(s, "local_engine", lambda a: True)
    a = parsed("--nested-containers")
    assert len(s.nested_notes(a)) == 2  # no such file: no restriction
    restriction.write_text("0\n")
    assert len(s.nested_notes(a)) == 2
    restriction.write_text("1\n")
    notes = s.nested_notes(a)
    assert len(notes) == 3 and "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in notes[2]
    assert s.nested_notes(parsed()) == []



# --- what one of the agent's other IDs leaves in the host's folders ---------------------------------------------

def test_every_host_folder_is_mounted_nosuid_in_a_nested_run(devices, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    options = ("--mount", f"{data}:/mnt/data:rw", "--workspace", str(tmp_path / "w"))
    (tmp_path / "w").mkdir()
    _, plain = s.pod_commands(parsed(*options), "agent-x", tmp_path / "session", tmp_path / "home")
    _, run = s.pod_commands(parsed(*options, "--nested-containers"), "agent-x", tmp_path / "session",
                            tmp_path / "home")
    binds = [x for x in run if x.startswith("type=bind,")]
    assert binds and all(x.endswith(",nosuid") and ",nodev" in x for x in binds)
    assert not any("nosuid" in x for x in plain if x.startswith("type=bind,"))
    assert any(x.endswith(",dst=/mnt/data,rw,nodev,nosuid") for x in binds), binds


def test_the_folders_given_back_are_the_writable_ones(tmp_path):
    data, ro = tmp_path / "data", tmp_path / "ro"
    data.mkdir()
    ro.mkdir()
    a = parsed("--nested-containers", "--mount", f"{data}:/mnt/data:rw", "--mount", f"{ro}:/mnt/ro:ro")
    a.workspaces = [str(tmp_path / "w"), str(tmp_path / "x")]
    assert s.nested_writable(a) == ["/work", "/workspaces/x", "/mnt/data", "/root"]
    a.workspace_ro = True  # every workspace read-only: only the rw mount and HOME
    assert s.nested_writable(a) == ["/mnt/data", "/root"]
    a.tmux_socket_dir = tmp_path / "tmux"
    assert s.nested_writable(a)[-1] == s.TMUX_DIR


def owned(real, owners):
    """lstat that presents the paths in OWNERS ({path: (uid, gid, mode or None, ctime or None)}) so."""
    def lstat(path):
        info = real(path)
        if str(path) not in owners:
            return info
        uid, gid, mode, ctime = owners[str(path)]
        values = list(info)
        values[4], values[5] = uid, gid
        if mode is not None:
            values[0] = (info.st_mode & ~0o7777) | mode
        if ctime is not None:
            values[9] = ctime
        return os.stat_result(values)
    return lstat


def test_the_supervisor_gives_back_what_other_ids_left(tmp_path):
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    for name in ("mine", "theirs", "setuid", "old", "sub/deep"):
        (work / name).write_text("x")
    (work / "link").symlink_to("/etc/passwd")
    now = time.time()
    owners = {str(work / "theirs"): (100999, 100999, None, None),
              str(work / "setuid"): (1000, 0, 0o4755, None),  # setuid to one of your subordinate IDs
              str(work / "old"): (5000, 5000, None, now - 3600),  # there before the run: left alone
              str(work / "sub"): (0, 33, None, None),  # only the group: given back too
              str(work / "sub" / "deep"): (7, 7, 0o2755, None),
              str(work / "link"): (9, 9, None, None)}
    calls = []
    found = rt.give_back([str(work)], now - 60, lstat=owned(os.lstat, owners),
                         lchown=lambda path, uid, gid: calls.append(("chown", path, uid, gid)),
                         chmod=lambda path, mode: calls.append(("chmod", path, mode)))
    chowned = sorted(c[1] for c in calls if c[0] == "chown")
    assert chowned == sorted(str(work / n) for n in ("theirs", "setuid", "sub", "sub/deep", "link"))
    assert all(c[2:] == (0, 0) for c in calls if c[0] == "chown")
    assert sorted((c[1], c[2]) for c in calls if c[0] == "chmod") == [(str(work / "setuid"), 0o755),
                                                                      (str(work / "sub" / "deep"), 0o755)]
    assert found["count"] == 5 and found["failed"] == 0 and found["complete"] and len(found["paths"]) == 5


def test_a_give_back_that_fails_or_has_too_much_says_so(tmp_path, capsys):
    work = tmp_path / "work"
    work.mkdir()
    for index in range(30):
        (work / f"f{index}").write_text("")
    owners = {str(work / f"f{index}"): (1000, 1000, None, None) for index in range(30)}

    def refuse(path, uid, gid):
        if path.endswith("f3"):
            raise PermissionError("read-only")
    found = rt.give_back([str(work), str(tmp_path / "missing")], 0, lstat=owned(os.lstat, owners), lchown=refuse,
                         chmod=lambda *a: None)
    assert found["count"] == 29 and found["failed"] == 1 and len(found["paths"]) == rt.GIVE_BACK_NAMED
    logs = tmp_path / "logs"
    logs.mkdir()
    rt.report_given_back(found, logs)
    said = capsys.readouterr().err
    assert "29 files and folders the run left owned by other IDs" in said and "1 could not be" in said and " ..." in said
    assert json.loads((logs / "nested-owners.json").read_text())["count"] == 29
    partial = rt.give_back([str(work)], 0, lstat=owned(os.lstat, owners), lchown=lambda *a: None,
                           chmod=lambda *a: None, limit=5)
    assert partial["complete"] is False
    rt.report_given_back({"count": 0, "paths": [], "failed": 0, "complete": True})
    assert capsys.readouterr().err == ""  # nothing left: nothing said


def test_the_supervisor_gives_back_after_the_agent_ended():
    import inspect
    source = inspect.getsource(rt.inner_bootstrap)
    assert source.index("return wait_child(child)") < source.index("give_back(nested.get(\"writable\", []), began)")
    assert source.index("began = time.time()") < source.index("nested_setup(")


def test_the_plan_names_the_nosuid_mounts_and_the_give_back(devices):
    plan = s.nested_plan(parsed("--nested-containers"))
    assert "nosuid" in plan["host_folders"] and plan["given_back"]["paths"] == ["/work", "/root"]
    assert "podman unshare chown -R 0:0" in plan["given_back"]["what"]
    assert any("given back to you" in note for note in s.nested_notes(parsed("--nested-containers")))


def test_the_live_test_asks_for_pasta_where_the_podman_inside_has_it():
    """tests/test_nested_containers_live.py (CI: nested) fails when slirp4netns is unavailable, and when pasta is,
    from Podman 4.4 on (Debian bookworm's 4.3 has no pasta mode as root); its script tries one port per attempt."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("nested_live", Path(__file__).resolve().parent /
                                                  "test_nested_containers_live.py")
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    assert not live.pasta_supported("4.3.1") and live.pasta_supported("4.4.0") and live.pasta_supported("5.4.2")
    assert not live.pasta_supported("unknown") and not live.pasta_supported(None)
    assert live.ATTEMPTS == ("agent", "shared", "user1", "slirp4netns", "pasta")
    for name in live.ATTEMPTS:
        assert f"PORT_{name}" in live.SCRIPT, name
    assert live.results("RESULT shared refused\nnoise\nRESULT pasta unavailable: no such mode\n") == \
        {"shared": "refused", "pasta": "unavailable: no such mode"}
