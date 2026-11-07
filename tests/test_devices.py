"""sandbox-ai devices / limits: devices and limits of a running sandbox (the namespace work is faked here; the
live tests run it for real)."""
import importlib.util
import json
import os
from pathlib import Path
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_devices_tests", ROOT / "sandbox/devices.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


def fake_process(proc, pid, user, mnt, cgroup, config=None, sandbox=True):
    base = proc / str(pid)
    (base / "ns").mkdir(parents=True)
    (base / "ns" / "user").symlink_to(f"user:[{user}]")
    (base / "ns" / "mnt").symlink_to(f"mnt:[{mnt}]")
    (base / "cgroup").write_text(f"0::{cgroup}\n")
    if sandbox:
        (base / "root" / "sandbox" / "session").mkdir(parents=True)
        if config is not None:
            (base / "root" / "sandbox" / "session" / "config.json").write_text(config)
    else:
        (base / "root").mkdir()


@pytest.fixture
def world(tmp_path):
    """Two Podman sandboxes in one rootless user namespace, told apart by their pods' cgroups."""
    proc, session = tmp_path / "proc", tmp_path / "session"
    session.mkdir()
    (session / "config.json").write_text('{"run": "mine"}')
    pod = "/user.slice/user-1000.slice/user@1000.service/user.slice/libpod_pod_abc.slice"
    fake_process(proc, 100, 1, 10, f"{pod}/libpod-sup.scope", config='{"run": "mine"}')  # supervisor
    fake_process(proc, 101, 1, 11, f"{pod}/libpod-sup.scope")                            # the agent under Firejail
    fake_process(proc, 102, 1, 12, f"{pod}/libpod-infra.scope", sandbox=False)            # pod infra: no /sandbox
    fake_process(proc, 200, 1, 20, "/user.slice/.../libpod_pod_other.slice/libpod-x.scope", config='{"run": "other"}')
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    record = {"run": "mine", "backend": "podman", "session_dir": str(session), "directory": str(run_dir)}
    return proc, record


def test_sandbox_processes_finds_this_pod_only(world):
    proc, record = world
    found = d.sandbox_processes(record, proc)
    assert sorted(found["pids"]) == [100, 101, 102] and found["user"] == 100
    assert sorted(found["mntns"].values()) == [100, 101]  # not the infra container's, not the other sandbox's
    assert found["cgroup"].endswith("libpod_pod_abc.slice")
    assert d.sandbox_processes({**record, "session_dir": str(proc / "none")}, proc) is None


def test_bubblewrap_sandboxes_are_told_apart_by_their_user_namespace(tmp_path):
    proc, session = tmp_path / "proc", tmp_path / "session"
    session.mkdir()
    (session / "config.json").write_text("{}")
    fake_process(proc, 300, 7, 30, "/user.slice/run-1.scope", config="{}")
    fake_process(proc, 301, 7, 31, "/user.slice/run-1.scope")
    fake_process(proc, 400, 8, 40, "/user.slice/run-2.scope")
    found = d.sandbox_processes({"backend": "bwrap", "session_dir": str(session)}, proc)
    assert sorted(found["pids"]) == [300, 301] and found["cgroup"] == "/user.slice/run-1.scope"


class FakeLibc:
    def __init__(self):
        self.calls, self.next_fd = [], 900

    def setns(self, fd, kind):
        self.calls.append(("setns", kind))
        return 0

    def unshare(self, kind):
        self.calls.append(("unshare", kind))
        return 0

    def syscall(self, number, *args):
        self.calls.append(("syscall", number))
        if number == d.SYS_OPEN_TREE:
            self.next_fd += 1
            return self.next_fd
        return 0

    def umount2(self, path, flags):
        self.calls.append(("umount2", path))
        return 0


def test_helper_enters_the_user_namespace_clones_once_per_mount_namespace_and_mounts(world, tmp_path):
    proc, _ = world
    for pid in (100, 101):  # os.open needs files where the real /proc has namespace links
        for kind in ("user", "mnt"):
            link = proc / str(pid) / "ns" / kind
            link.unlink()
            link.write_text("")
    device = tmp_path / "ttyUSB0"
    device.write_text("")
    c = FakeLibc()
    d.helper("add", 100, str(device), [100, 101], proc, c)
    kinds = [call[0] if call[0] != "syscall" else call[1] for call in c.calls]
    assert kinds == ["setns", "unshare", d.SYS_OPEN_TREE, d.SYS_OPEN_TREE, "setns", d.SYS_MOVE_MOUNT,
                     "setns", d.SYS_MOVE_MOUNT]
    assert c.calls[0] == ("setns", d.CLONE_NEWUSER) and c.calls[1] == ("unshare", d.CLONE_NEWNS)
    c = FakeLibc()
    d.helper("remove", 100, str(device), [100], proc, c)
    assert [call[0] for call in c.calls] == ["setns", "setns", "umount2"]
    # A kernel without the new mount API says so.
    broken = FakeLibc()
    broken.syscall = lambda *a: -1
    import ctypes
    original = ctypes.get_errno
    try:
        ctypes.get_errno = lambda: 38  # ENOSYS
        with pytest.raises(OSError, match="Linux 5.2"):
            d.helper("add", 100, str(device), [100], proc, broken)
    finally:
        ctypes.get_errno = original


def plain_device(value, launcher=None):
    """checked_device for a node that is no USB device (nothing about it in sysfs)."""
    facts = {"node": value, "names": [value], "subsystem": "tty", "usb": None, "serial": None, "classes": [],
             "numbers": "4:64", "unknown": []}
    return [{"path": value, "kind": "node", "facts": facts}], ["warning: try a udev rule"], None


def test_add_list_remove(world, monkeypatch):
    proc, record = world
    monkeypatch.setattr(d, "checked_device", plain_device)
    monkeypatch.setattr(d.module("agent_sandbox"), "load_policy", lambda path=None: {})
    calls, lines = [], []
    finder = lambda r: d.sandbox_processes(r, proc)  # noqa: E731
    runner = lambda op, found, path: calls.append((op, sorted(found["mntns"].values()), path)) or (0, "")  # noqa: E731
    d.add(record, "/dev/ttyUSB0", out=lines.append, runner=runner, finder=finder)
    assert calls == [("add", [100, 101], "/dev/ttyUSB0")] and d.added(record) == ["/dev/ttyUSB0"]
    assert any("udev rule" in line for line in lines)
    with pytest.raises(ValueError, match="added already"):
        d.add(record, "/dev/ttyUSB0", runner=runner, finder=finder)
    d.remove(record, "/dev/ttyUSB0", out=lines.append, runner=runner, finder=finder)
    assert d.added(record) == [] and calls[-1][0] == "remove"
    with pytest.raises(ValueError, match="not added during this run"):
        d.remove(record, "/dev/ttyUSB0", runner=runner, finder=finder)
    failing = lambda op, found, path: (1, "Operation not permitted")  # noqa: E731
    with pytest.raises(ValueError, match="Operation not permitted"):
        d.add(record, "/dev/ttyUSB1", runner=failing, finder=finder)
    assert d.added(record) == []  # nothing recorded for a failed attach


def test_device_checks_and_policy(monkeypatch):
    l = d.module("agent_sandbox")
    monkeypatch.setattr(l, "load_policy", lambda path=None: {"version": 1, "forbid": ["--device"]})
    with pytest.raises(ValueError, match="administrator's policy"):
        d.checked_device("/dev/ttyUSB0")
    monkeypatch.setattr(l, "load_policy", lambda path=None: {})
    with pytest.raises(ValueError, match="under /dev"):
        d.checked_device("/etc/passwd")


def test_limits_through_systemd_or_the_cgroup_files(world, tmp_path, monkeypatch):
    proc, record = world
    finder = lambda r: d.sandbox_processes(r, proc)  # noqa: E731
    commands, lines = [], []

    def systemctl(command, **kwargs):
        commands.append(command)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/" + name)
    assert d.set_limits(record, memory="4g", cpus="2", out=lines.append, finder=finder, runner=systemctl) == "systemd"
    assert commands[0][:5] == ["systemctl", "--user", "set-property", "--runtime", "libpod_pod_abc.slice"]
    assert "MemoryMax=4G" in commands[0] and "CPUQuota=200%" in commands[0]
    # Without systemd: the delegated cgroup files.
    cgroup = tmp_path / "cg" / record_cgroup(proc, record).lstrip("/")
    cgroup.mkdir(parents=True)
    for name in ("memory.max", "memory.swap.max", "cpu.max", "pids.max"):
        (cgroup / name).write_text("max")
    refused = lambda command, **kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr="")  # noqa: E731
    assert d.set_limits(record, memory="512m", pids="none", finder=finder, runner=refused, out=lines.append,
                        cgroup_root=tmp_path / "cg") == "cgroup"
    assert (cgroup / "memory.max").read_text() == str(512 << 20) and (cgroup / "pids.max").read_text() == "max"
    with pytest.raises(ValueError, match="--memory expects"):
        d.set_limits(record, memory="lots", finder=finder, runner=systemctl)
    with pytest.raises(ValueError, match="give --memory"):
        d.set_limits(record, finder=finder)


def test_limits_keep_to_the_runs_security_mode(world, monkeypatch):
    """A strict mode caps --memory/--cpus/--pids at the start; `sandbox-ai limits` during the run keeps to it."""
    proc, record = world
    finder = lambda r: d.sandbox_processes(r, proc)  # noqa: E731
    commands = []
    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/" + name)
    systemctl = lambda command, **kwargs: commands.append(command) or types.SimpleNamespace(returncode=0)  # noqa: E731
    strict = {**record, "mode": {"name": "high"}}
    with pytest.raises(ValueError, match=r"--memory none: the mode limits it to 4g \(security mode high\)"):
        d.set_limits(strict, memory="none", finder=finder, runner=systemctl)
    with pytest.raises(ValueError, match="--pids 9999"):
        d.set_limits(strict, memory="2g", pids="9999", finder=finder, runner=systemctl)
    assert commands == []  # nothing changed
    assert d.set_limits(strict, memory="2g", finder=finder, runner=systemctl, out=lambda line: None) == "systemd"
    assert d.set_limits({**record, "mode": {"name": "medium"}}, memory="none", finder=finder, runner=systemctl,
                        out=lambda line: None) == "systemd"  # no cap in medium


def record_cgroup(proc, record):
    return d.sandbox_processes(record, proc)["cgroup"]


# --- device identity, rules, replug: a fake sysfs and udev ----------------------------------------------------------
#
# The bypass cases assume what the analysis assumes of devices: the attacker controls what a device reports
# about itself (USB IDs, serial, product name, and so the udev links made from them), not the kernel.

import shutil  # noqa: E402
import stat  # noqa: E402
import threading  # noqa: E402

L = d.module("agent_sandbox")
RT = d.module("runtime")


class FakeUsb:
    """SYS, UDEV_DATA and /dev of fake USB devices; device numbers of the fake nodes in self.numbers."""

    def __init__(self, tmp_path, monkeypatch, devices=d, launcher=L, dev=None):
        """DEVICES: the devices module to fake sysfs for; LAUNCHER: the agent_sandbox whose /dev is faked here
        (None: DEV, a /dev faked elsewhere)."""
        self.root = tmp_path / "hw"
        self.sys, self.udev, self.dev = self.root / "sys", self.root / "udev", dev or self.root / "dev"
        for path in (self.sys / "devices/pci0000:00/usb1", self.sys / "bus/usb/devices", self.sys / "dev/char",
                     self.sys / "class", self.udev, self.dev / "serial/by-id"):
            path.mkdir(parents=True, exist_ok=True)
        self.numbers, self.ports = {}, {}
        monkeypatch.setattr(devices, "SYS", self.sys)
        monkeypatch.setattr(devices, "UDEV_DATA", self.udev)
        monkeypatch.setattr(devices, "node_numbers", lambda path: self.numbers.get(os.path.realpath(path)))
        if launcher is None:
            return
        monkeypatch.setattr(launcher, "DEV", self.dev)
        real_mode = L.device_mode

        def mode(path):
            path = Path(path)
            if path.is_dir():
                return stat.S_IFDIR | 0o755
            return stat.S_IFCHR | 0o666 if self.dev in path.parents else real_mode(path)
        monkeypatch.setattr(launcher, "device_mode", mode)
        monkeypatch.setattr(launcher, "load_policy", lambda path=None: {})

    def node(self, name, numbers):
        path = self.dev / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        self.numbers[str(path)] = numbers
        return path

    def sysfs_node(self, directory, subsystem, name, numbers):
        (self.sys / "class" / subsystem).mkdir(parents=True, exist_ok=True)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "dev").write_text(numbers + "\n")
        (directory / "uevent").write_text(f"MAJOR={numbers.split(':')[0]}\nDEVNAME={name}\n")
        (directory / "subsystem").symlink_to(self.sys / "class" / subsystem)
        link = self.sys / "dev/char" / numbers
        if link.is_symlink():
            link.unlink()
        link.symlink_to(directory)
        self.node(name, numbers)

    def plug(self, port="1-2", usb="2e8a:000a", serial="E6609CB2", devnum=5, cls="ef", product="Pico",
             interfaces=(("02", "tty", "ttyACM0", "166:0"), ("0a", None, None, None))):
        vendor, model = usb.split(":")
        device = self.sys / "devices/pci0000:00/usb1" / port
        device.mkdir(parents=True)
        values = {"idVendor": vendor, "idProduct": model, "busnum": "1", "devnum": str(devnum), "bDeviceClass": cls,
                  "product": product}
        if serial is not None:
            values["serial"] = serial
        for name, value in values.items():
            (device / name).write_text(value + "\n")
        raw = f"bus/usb/001/{devnum:03d}"
        self.sysfs_node(device, "usb", raw, f"189:{devnum - 1}")
        (self.sys / "bus/usb/devices" / port).symlink_to(device)
        nodes = [str(self.dev / raw)]
        for index, (interface_class, subsystem, name, numbers) in enumerate(interfaces):
            interface = device / f"{port}:1.{index}"
            interface.mkdir()
            (interface / "bInterfaceClass").write_text(interface_class + "\n")
            if subsystem:
                self.sysfs_node(interface / subsystem / name.replace("/", "!"), subsystem, name, numbers)
                nodes.append(str(self.dev / name))
                if subsystem == "tty":
                    link = f"serial/by-id/usb-{product}_{serial}-if0{index}"
                    (self.udev / f"c{numbers}").write_text(f"S:{link}\nE:ID_SERIAL={serial}\n")
                    target = self.dev / link
                    if target.is_symlink():
                        target.unlink()
                    target.symlink_to(self.dev / name)
        self.ports[port] = nodes
        return nodes

    def unplug(self, port="1-2"):
        device = (self.sys / "bus/usb/devices" / port).resolve()
        for link in (self.sys / "dev/char").iterdir():
            if link.resolve() == device or device in link.resolve().parents:
                link.unlink()
        (self.sys / "bus/usb/devices" / port).unlink()
        shutil.rmtree(device)
        for node in self.ports.pop(port):
            Path(node).unlink()
            self.numbers.pop(node, None)


@pytest.fixture
def usb(tmp_path, monkeypatch):
    return FakeUsb(tmp_path, monkeypatch)


def ruleset(*documents):
    return RT.Ruleset([{"name": f"r{index}", **document} for index, document in enumerate(documents, 1)])


def device_rule(ident, decision, **fields):
    return {"id": ident, "decision": decision, **fields}


@pytest.fixture
def run(world, tmp_path, monkeypatch):
    """The running sandbox of `world` with a log directory, a recording helper and a fake clock."""
    proc, record = world
    logs = tmp_path / "logs"
    logs.mkdir()
    record["log_dir"] = str(logs)
    calls = []
    state = types.SimpleNamespace(record=record, logs=logs, calls=calls, now=1000.0,
                                  finder=lambda r: d.sandbox_processes(r, proc),
                                  runner=lambda op, found, path: calls.append((op, path)) or (0, ""))
    state.log = lambda: [json.loads(line) for line in (logs / "devices.jsonl").read_text().splitlines()] \
        if (logs / "devices.jsonl").exists() else []
    state.events = lambda: [entry["event"] for entry in state.log()]
    state.add = lambda value, rules=None, **kw: d.add(record, value, out=lambda line: None, runner=state.runner,
                                                      finder=state.finder, rules=rules or ruleset(),
                                                      watcher=lambda r: None, **kw)

    def watcher(rules=None):
        return d.Watcher(record, runner=state.runner, finder=state.finder, rules=rules or ruleset(),
                         clock=lambda: state.now, launcher=L)
    state.watcher = watcher
    return state


def test_a_selector_is_parsed_strictly():
    assert d.parse_selector("usb:2E8A:000a") == {"usb": "2e8a:000a", "serial": None}
    assert d.parse_selector("usb:2e8a:000a:E66:09") == {"usb": "2e8a:000a", "serial": "E66:09"}
    assert d.parse_selector("/dev/ttyACM0") is None
    for bad in ("usb:2e8a", "usb:2e8a:0x0a", "usb:2e8a:000a:", "usb:2e8a:000a:a b", "usb:2e8a:000a:\u00e9"):
        with pytest.raises(ValueError, match="usb:VENDOR:PRODUCT"):
            d.parse_selector(bad)


def test_the_identity_of_a_node_comes_from_sysfs_and_udev(usb):
    usb.plug()
    facts = d.identity(str(usb.dev / "ttyACM0"), dev_root=usb.dev)
    assert facts["usb"] == "2e8a:000a" and facts["serial"] == "E6609CB2" and facts["subsystem"] == "tty"
    assert facts["classes"] == ["02", "0a", "ef"] and facts["bus"] == "001/005" and facts["port"] == "1-2"
    # Names as rules see them (this /dev stands for /dev): the node and udev's link made from what it reports.
    assert facts["names"] == ["/dev/ttyACM0", "/dev/serial/by-id/usb-Pico_E6609CB2-if00"]
    assert facts["unknown"] == [] and facts["product"] == "Pico" and facts["numbers"] == "166:0"
    raw = d.identity(str(usb.dev / "bus/usb/001/005"))
    assert raw["subsystem"] == "usb" and raw["usb"] == "2e8a:000a"
    # A node sysfs does not know: nothing about it can be told.
    assert d.identity(str(usb.dev / "nothing"))["unknown"] == ["subsystem", "usb", "serial", "class"]


def test_usb_selectors_resolve_to_the_devices_nodes(usb):
    usb.plug()
    entries, notes, device = d.plan("usb:2e8a:000a", L)
    assert [e["path"] for e in entries] == [str(usb.dev / "bus/usb/001/005"), str(usb.dev / "ttyACM0")]
    assert device["serial"] == "E6609CB2" and notes == []
    assert [e["path"] for e in d.plan("usb:2e8a:000a:E6609CB2", L)[0]] == [e["path"] for e in entries]
    with pytest.raises(ValueError, match="no such USB device"):
        d.plan("usb:2e8a:000a:OTHER", L)
    with pytest.raises(ValueError, match="no such USB device"):
        d.plan("usb:0403:6001", L)
    # Two of a kind without a serial to tell them apart: refused, not both.
    usb.plug("1-3", devnum=7, serial="SECOND", interfaces=(("02", "tty", "ttyACM1", "166:1"),))
    with pytest.raises(ValueError, match="2 devices have these IDs.*name one by its serial"):
        d.plan("usb:2e8a:000a", L)
    assert [e["path"] for e in d.plan("usb:2e8a:000a:SECOND", L)[0]][-1] == str(usb.dev / "ttyACM1")


def test_a_forged_serial_claimed_twice_is_refused(usb):
    """A device can report any serial: when two claim the one given, neither is taken."""
    usb.plug()
    usb.plug("1-4", devnum=9, interfaces=(("02", "tty", "ttyACM3", "166:3"),))  # the same IDs and serial
    with pytest.raises(ValueError, match="2 devices claim this serial number.*by its path"):
        d.plan("usb:2e8a:000a:E6609CB2", L)


def test_input_devices_and_disks_give_no_raw_usb(usb):
    # A keyboard (or a security key): its hidraw and input nodes are refused, its raw node withheld.
    usb.plug("1-5", usb="046d:c52b", serial=None, devnum=11, cls="00", product="Receiver",
             interfaces=(("03", "hidraw", "hidraw0", "240:0"), ("03", "input", "input/event5", "13:69")))
    with pytest.raises(ValueError, match="no device node of it can be passed") as caught:
        d.plan("usb:046d:c52b", L)
    assert "raw USB" in str(caught.value) and "keyboards" in str(caught.value)
    # A debug probe with a HID interface and a serial port: the serial port only.
    usb.plug("1-6", usb="0d28:0204", serial="P1", devnum=12, cls="ef", product="DAPLink",
             interfaces=(("03", "hidraw", "hidraw1", "240:1"), ("02", "tty", "ttyACM2", "166:2")))
    entries, notes, _ = d.plan("usb:0d28:0204", L)
    assert [e["path"] for e in entries] == [str(usb.dev / "ttyACM2")]
    assert any("raw USB" in note for note in notes) and any("hidraw1 left out" in note for note in notes)


def test_symlinked_sysfs_entries_are_not_believed(usb, tmp_path):
    usb.plug()
    rules = ruleset({"version": 1, "devices": [device_rule("pico", "allow", usb="2e8a:000a")]})
    # The device-number link leads out of SYS/devices, to a tree that claims the allowed IDs.
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / "idVendor").write_text("2e8a\n")
    (forged / "idProduct").write_text("000a\n")
    link = usb.sys / "dev/char/166:0"
    link.unlink()
    link.symlink_to(forged)
    facts = d.identity(str(usb.dev / "ttyACM0"))
    # Its IDs are unknown: the allow rule does not match, and a vendor:product with allow rules refuses it.
    assert "usb" in facts["unknown"] and rules.device(facts)["rule"] == "(unlisted)"
    strict = ruleset({"version": 1, "defaults": {"devices": "deny"},
                      "devices": [device_rule("pico", "allow", usb="2e8a:000a")]})
    assert strict.device(facts)["decision"] == "deny"
    deny = ruleset({"version": 1, "devices": [device_rule("no-pico", "deny", usb="2e8a:000a")]})
    assert deny.device(facts)["decision"] == "deny"  # unknown counts for a deny
    # An attribute planted as a symlink is not read either.
    link.unlink()
    link.symlink_to(usb.sys / "devices/pci0000:00/usb1/1-2/1-2:1.0/tty/ttyACM0")
    vendor = usb.sys / "devices/pci0000:00/usb1/1-2/idVendor"
    vendor.unlink()
    vendor.symlink_to(forged / "idVendor")
    facts = d.identity(str(usb.dev / "ttyACM0"))
    assert facts["usb"] != "2e8a:000a" and strict.device(facts)["decision"] == "deny"


DEVICE_MALFORMED = [
    ({"devices": [device_rule("x", "allow")]}, "a device rule needs"),
    ({"devices": [device_rule("x", "allow", usb="2e8a")]}, '"usb"'),
    ({"devices": [device_rule("x", "allow", usb="*:000a")]}, '"usb"'),
    ({"devices": [device_rule("x", "allow", **{"class": "keyboard"})]}, "not a USB class"),
    ({"devices": [device_rule("x", "allow", node="/dev/../etc/passwd")]}, "without . or .."),
    ({"devices": [device_rule("x", "allow", node="ttyACM0")]}, '"node"'),
    ({"devices": [device_rule("x", "allow", serial="a b")]}, '"serial"'),
    ({"devices": [device_rule("x", "allow", subsystem="TTY")]}, '"subsystem"'),
    ({"devices": [device_rule("x", "allow", vendor="2e8a")]}, "unknown key 'vendor'"),
    ({"defaults": {"devices": "block"}}, '"defaults" takes'),
    ({"devices": {}}, '"devices" is a list'),
]


@pytest.mark.parametrize("document,message", DEVICE_MALFORMED, ids=[str(i) for i in range(len(DEVICE_MALFORMED))])
def test_malformed_device_rules_are_refused(document, message):
    with pytest.raises(RT.RuleError, match=message) as caught:
        RT.Ruleset.validated({"version": 1, **document}, "rules.json")
    assert "device rule" in str(caught.value) or "defaults" in str(caught.value) or '"devices"' in str(caught.value)


def test_device_rules_normal_form():
    checked = RT.Ruleset.validated({"version": 1, "defaults": {"devices": "ask"}, "devices": [
        device_rule("pico", "allow", usb=["2E8A:000A", "2e8a:*"], **{"class": ["cdc", "0A"]}, subsystem="tty",
                    node="/dev/serial/by-id/usb-Raspberry*")]}, "x")
    rule = checked["devices"][0]
    assert rule["usb"] == ["2e8a:000a", "2e8a:*"] and rule["class"] == ["02", "0a"] and rule["subsystem"] == ["tty"]
    assert checked["defaults"] == {"http": "allow", "mcp": "allow", "devices": "ask", "tool": "allow"}
    assert RT.Ruleset.validated(checked, "x") == checked


def test_a_ruleset_allowing_one_device_refuses_every_other(usb):
    usb.plug()
    usb.plug("1-3", usb="0403:6001", serial="FT1", devnum=7, cls="00", product="FT232R",
             interfaces=(("ff", "tty", "ttyUSB0", "188:0"),))
    only = ruleset({"version": 1, "defaults": {"devices": "deny"},
                    "devices": [device_rule("pico", "allow", usb="2e8a:000a", serial="E6609CB2")]})
    pico = d.identity(str(usb.dev / "ttyACM0"))
    assert only.device(pico) == {"decision": "allow", "rule": "pico", "ruleset": "r1",
                                 "reason": f"the device {usb.dev / 'ttyACM0'} (usb 2e8a:000a) is allowed by rule pico (r1)"}
    assert only.device(d.identity(str(usb.dev / "ttyUSB0")))["rule"] == "(default)"
    assert only.device(d.other_facts(str(usb.dev / "bus/usb"), "dir"))["decision"] == "deny"
    assert only.device(d.other_facts("nvidia.com/gpu=all", "cdi"))["decision"] == "deny"
    # Without a default: the same vendor:product with another serial is still not among the allowed ones.
    listed = ruleset({"version": 1, "devices": [device_rule("pico", "allow", usb="2e8a:000a", serial="E6609CB2")]})
    assert listed.device({**pico, "serial": "FORGED"})["rule"] == "(unlisted)"
    assert listed.device({**pico, "unknown": ["serial"]})["rule"] == "(unlisted)"
    assert listed.device(d.identity(str(usb.dev / "ttyUSB0")))["rule"] is None  # other devices: no say


def test_device_rules_match_class_subsystem_and_every_name_of_a_node(usb):
    usb.plug()
    pico = d.identity(str(usb.dev / "ttyACM0"))
    rules = ruleset({"version": 1, "devices": [
        device_rule("by-id", "ask", node="/dev/serial/by-id/usb-Pico_*"),
        device_rule("raw", "deny", subsystem="usb", priority=10),
        device_rule("cams", "deny", **{"class": "video"})]})
    by_id = {**pico, "names": [n.replace(str(usb.dev), "/dev") for n in pico["names"]]}
    assert rules.device(by_id)["rule"] == "by-id"
    assert rules.device(d.identity(str(usb.dev / "bus/usb/001/005")))["rule"] == "raw"
    assert rules.device({**pico, "classes": ["0e"]})["rule"] == "cams"
    # The strictest of several rulesets wins: the administrator's deny over a project's allow.
    layered = RT.Ruleset([{"name": "policy", "version": 1, "devices": [device_rule("no-tty", "deny", subsystem="tty")]},
                          {"name": "project", "version": 1, "devices": [device_rule("pico", "allow", usb="2e8a:000a")]}])
    assert layered.device(pico)["ruleset"] == "policy" and layered.device(pico)["decision"] == "deny"


def test_add_by_identity_with_rules_and_the_log(usb, run):
    usb.plug()
    deny = ruleset({"version": 1, "devices": [device_rule("no-pico", "deny", usb="2e8a:*", reason="not this one")]})
    with pytest.raises(ValueError, match="not this one"):
        run.add("usb:2e8a:000a", rules=deny)
    assert d.load(run.record) == [] and run.events() == ["refuse"]
    assert run.log()[0]["rule"] == "no-pico" and run.log()[0]["device"]["serial"].startswith("sha256:")
    # Raw USB denied, the serial port allowed: only that is attached.
    partly = ruleset({"version": 1, "devices": [device_rule("no-raw", "deny", subsystem="usb")]})
    assert run.add("usb:2e8a:000a:E6609CB2", rules=partly) == str(usb.dev / "ttyACM0")
    assert run.calls == [("add", str(usb.dev / "ttyACM0"))]
    grant = d.load(run.record)[0]
    assert grant["by"] == "identity" and grant["state"] == "attached" and grant["track"]
    assert grant["nodes"] == {str(usb.dev / "ttyACM0"): "166:0"} and grant["device"]["bus"] == "001/005"
    assert run.events() == ["refuse", "grant", "attach"]
    assert "E6609CB2" not in (run.logs / "devices.jsonl").read_text()  # the serial is hashed
    assert run.log()[-1]["value"] == "usb:2e8a:000a:" + d.serial_shown("E6609CB2")
    with pytest.raises(ValueError, match="added already"):
        run.add(str(usb.dev / "ttyACM0"))


def test_an_ask_rule_goes_through_approvals(usb, run, monkeypatch):
    usb.plug()
    approvals = d.module("approvals")
    monkeypatch.setattr(approvals, "policy_scope", lambda: "always")
    asking = ruleset({"version": 1, "devices": [device_rule("ask-usb", "ask", usb="2e8a:000a")]})
    answers, lines = [], []

    def answer(decision, scope):
        directory = Path(run.record["directory"]) / "approvals"
        for _ in range(200):
            open_ones = approvals.questions({"mine": run.record})
            if open_ones:
                question = open_ones[0][2]
                answers.append(approvals.describe(question))
                approvals.answer(question["id"], decision, scope, runs={"mine": run.record}, out=lines.append)
                return
            threading.Event().wait(0.02)
        raise AssertionError(f"no question in {directory}")
    helper = threading.Thread(target=answer, args=("allow", "always"))
    helper.start()
    run.add("usb:2e8a:000a", rules=asking, wait=10)
    helper.join()
    assert "attach the device" in answers[0] and "usb 2e8a:000a" in answers[0] and '"Pico"' in answers[0]
    assert "E6609CB2" not in answers[0] and "ask-usb" in answers[0]
    assert "approved at most for the run" in lines[0]  # always becomes run
    grant = d.load(run.record)[0]
    assert grant["approved"] == "run" and grant["state"] == "attached"
    assert run.events() == ["ask", "answer", "grant", "attach"]
    done = list((Path(run.record["directory"]) / "approvals").glob("*.done"))
    assert len(done) == 1 and json.loads(done[0].read_text())["decision"] == "allow"
    # A deny refuses it; no answer refuses it too.
    d.remove(run.record, "1", out=lines.append, runner=run.runner, finder=run.finder)
    helper = threading.Thread(target=answer, args=("deny", "once"))
    helper.start()
    with pytest.raises(ValueError, match="not approved"):
        run.add("usb:2e8a:000a", rules=asking, wait=10)
    helper.join()
    with pytest.raises(ValueError, match="no answer within 0.2 s"):
        run.add("usb:2e8a:000a", rules=asking, wait=0.2)


def test_replugged_under_a_new_node_it_is_attached_again(usb, run):
    usb.plug()
    run.add("usb:2e8a:000a:E6609CB2")
    watcher = run.watcher()
    watcher.step()
    assert run.calls == [("add", str(usb.dev / "bus/usb/001/005")), ("add", str(usb.dev / "ttyACM0"))]
    usb.unplug()
    run.now += 1
    watcher.step()
    assert run.calls[-2:] == [("remove", str(usb.dev / "bus/usb/001/005")), ("remove", str(usb.dev / "ttyACM0"))]
    assert d.load(run.record)[0]["state"] == "gone" and run.log()[-1]["reason"] == "unplugged"
    run.now += 1
    watcher.step()  # nothing there yet
    usb.plug(devnum=6, interfaces=(("02", "tty", "ttyACM1", "166:1"),))  # a bootloader reset: another node
    run.now += 2
    watcher.step()
    grant = d.load(run.record)[0]
    assert grant["state"] == "attached" and grant["replugs"] == 1
    assert set(grant["nodes"]) == {str(usb.dev / "bus/usb/001/006"), str(usb.dev / "ttyACM1")}
    assert run.events()[-2:] == ["detach", "replug"] and run.log()[-1]["nodes"] == sorted(grant["nodes"])
    assert d.listing(run.record)[0]["replugs"] == 1


def test_a_different_device_on_the_same_node_is_not_attached(usb, run):
    """The stale node in the sandbox would open whatever gets its device number next: it is taken away at the
    unplug, and a device with other IDs or another serial at the same path is not attached."""
    usb.plug()
    run.add(str(usb.dev / "ttyACM0"))
    watcher = run.watcher()
    grant = d.load(run.record)[0]
    assert grant["by"] == "path" and grant["track"] and grant["device"]["serial"] == "E6609CB2"
    usb.unplug()
    usb.plug(serial="ANOTHER", devnum=8)  # same port, same ttyACM0 and 166:0
    watcher.step()
    assert run.calls[-1] == ("remove", str(usb.dev / "ttyACM0"))
    assert run.log()[-1]["reason"] == "another device has the node now"
    run.now += 1
    watcher.step()
    assert d.load(run.record)[0]["state"] == "gone" and run.log()[-1]["event"] == "refuse"
    assert run.log()[-1]["reason"] == "another device has the path now"
    calls = len(run.calls)
    watcher.step()
    assert len(run.calls) == calls and run.events().count("refuse") == 1  # logged once
    usb.unplug()
    usb.plug(devnum=10)  # the device granted, back at its path
    run.now += 1
    watcher.step()
    assert d.load(run.record)[0]["state"] == "attached" and run.calls[-1] == ("add", str(usb.dev / "ttyACM0"))


def test_a_replug_is_judged_by_the_rules_again_and_dropped_after_the_limit(usb, run):
    usb.plug()
    run.add("usb:2e8a:000a")
    usb.unplug()
    deny = ruleset({"version": 1, "devices": [device_rule("now-denied", "deny", usb="2e8a:000a")]})
    watcher = run.watcher(deny)
    watcher.step()
    usb.plug(devnum=6, interfaces=(("02", "tty", "ttyACM1", "166:1"),))
    run.now += 1
    watcher.step()
    watcher.step()
    assert d.load(run.record)[0]["state"] == "gone" and run.events().count("refuse") == 1
    assert run.log()[-1]["rule"] == "now-denied" and run.log()[-1]["replug"] is True
    run.now += d.REPLUG_SECONDS + 1
    watcher.step()
    assert d.load(run.record)[0]["state"] == "dropped" and run.events()[-1] == "drop"
    run.now += 1
    watcher.step()
    assert run.events()[-1] == "drop"  # nothing more for it


def test_a_forged_device_does_not_take_a_replugged_grants_place(usb, run):
    usb.plug()
    run.add("usb:2e8a:000a:E6609CB2")
    watcher = run.watcher()
    usb.unplug()
    watcher.step()
    # Another device with the same IDs (no serial, or a different one) appears: not the device granted.
    usb.plug("1-3", serial=None, devnum=7, interfaces=(("02", "tty", "ttyACM1", "166:1"),))
    usb.plug("1-4", serial="FORGED", devnum=8, interfaces=(("02", "tty", "ttyACM2", "166:2"),))
    run.now += 1
    watcher.step()
    assert d.load(run.record)[0]["state"] == "gone" and run.calls[-1][0] == "remove"
    # Two claiming the serial: the one on the port it had wins; elsewhere, none is taken.
    usb.plug("1-5", devnum=9, interfaces=(("02", "tty", "ttyACM3", "166:3"),))
    usb.plug("1-6", devnum=11, interfaces=(("02", "tty", "ttyACM4", "166:4"),))
    run.now += 1
    watcher.step()
    assert d.load(run.record)[0]["state"] == "gone" and run.log()[-1]["outcome"] == "waiting"
    usb.plug("1-2", devnum=12, interfaces=(("02", "tty", "ttyACM5", "166:5"),))
    run.now += 1
    watcher.step()
    assert str(usb.dev / "ttyACM5") in d.load(run.record)[0]["nodes"]


def test_an_ask_rule_asks_again_at_a_replug_unless_approved_for_the_run(usb, run):
    usb.plug()
    run.add("usb:2e8a:000a")
    asking = ruleset({"version": 1, "devices": [device_rule("ask-pico", "ask", usb="2e8a:000a")]})
    watcher = run.watcher(asking)
    usb.unplug()
    watcher.step()
    usb.plug(devnum=6, interfaces=(("02", "tty", "ttyACM1", "166:1"),))
    run.now += 1
    watcher.step()
    grant = d.load(run.record)[0]
    assert grant["state"] == "held" and run.events()[-1] == "ask"
    question = json.loads((Path(run.record["directory"]) / "approvals" / f"{grant['question']}.json").read_text())
    assert question["kind"] == "device" and question["replug"] is True and question["serial"].startswith("sha256:")
    (Path(run.record["directory"]) / "approvals" / f"{grant['question']}.answer").write_text(
        json.dumps({"decision": "allow", "scope": "run"}))
    run.now += 1
    watcher.step()
    grant = d.load(run.record)[0]
    assert grant["state"] == "attached" and grant["approved"] == "run" and run.events()[-2:] == ["answer", "replug"]
    usb.unplug()
    run.now += 1
    watcher.step()
    usb.plug(devnum=7, interfaces=(("02", "tty", "ttyACM0", "166:0"),))
    run.now += 1
    watcher.step()
    assert d.load(run.record)[0]["state"] == "attached" and run.events().count("ask") == 1
    # Unanswered: refused after the timeout.
    usb.unplug()
    run.now += 1
    watcher.step()
    d.load(run.record)
    with d.locked(run.record):
        grants = d.load(run.record)
        grants[0]["approved"] = "once"
        d.save(run.record, grants)
    usb.plug(devnum=8, interfaces=(("02", "tty", "ttyACM0", "166:0"),))
    run.now += 1
    watcher.step()
    run.now += d.APPROVAL_SECONDS + 1
    watcher.step()
    assert d.load(run.record)[0]["state"] == "refused" and run.log()[-1]["reason"].startswith("no answer")


def test_a_device_held_at_the_start_is_attached_once_allowed(usb, run):
    usb.plug()
    entries, _, device = d.plan("usb:2e8a:000a", L)
    verdict = {"decision": "ask", "rule": "ask-pico", "ruleset": "policy"}
    grant = d.grant_from("usb:2e8a:000a", entries, [verdict] * len(entries), device, "start")
    grant.update(state="held", pending=list(grant["nodes"]), pending_numbers=dict(grant["nodes"]), nodes={})
    d.start_grants(run.record, [grant])
    watcher = run.watcher()
    watcher.step()
    held = d.load(run.record)[0]
    assert held["state"] == "held" and held["question"] and run.events() == ["grant", "ask"]
    (Path(run.record["directory"]) / "approvals" / f"{held['question']}.answer").write_text(
        json.dumps({"decision": "allow", "scope": "once"}))
    watcher.step()
    assert d.load(run.record)[0]["state"] == "attached" and run.events()[-2:] == ["answer", "attach"]
    assert [path for op, path in run.calls] == [str(usb.dev / "bus/usb/001/005"), str(usb.dev / "ttyACM0")]


def test_the_policy_as_it_is_now_judges_devices_added_later(usb, run, monkeypatch):
    usb.plug()
    (Path(run.record["directory"]) / "device-rules.json").write_text(json.dumps([
        {"name": "rules.json", "version": 1, "devices": [device_rule("mine", "allow", usb="2e8a:000a")]}]))
    assert d.run_rules(run.record).device(d.identity(str(usb.dev / "ttyACM0")))["decision"] == "allow"
    monkeypatch.setattr(L, "load_policy", lambda path=None: {"version": 1, "rules": {"version": 1, "devices": [
        device_rule("no-serial-adapters", "deny", subsystem="tty")]}})
    with pytest.raises(ValueError, match="no-serial-adapters"):
        d.add(run.record, str(usb.dev / "ttyACM0"), out=lambda line: None, runner=run.runner, finder=run.finder)
    monkeypatch.setattr(L, "load_policy", lambda path=None: {"version": 1, "forbid": ["--device"]})
    with pytest.raises(ValueError, match="administrator's policy"):
        d.add(run.record, str(usb.dev / "ttyACM0"), out=lambda line: None, runner=run.runner, finder=run.finder)


def test_list_shows_identity_rule_and_state(usb, run, capsys, monkeypatch):
    usb.plug()
    rules = ruleset({"version": 1, "devices": [device_rule("pico", "allow", usb="2e8a:000a")]})
    run.add("usb:2e8a:000a", rules=rules)
    entries = d.listing(run.record)
    assert entries[0]["device"]["serial"] == d.serial_shown("E6609CB2") and entries[0]["rule"] == "pico"
    assert entries[0]["state"] == "attached" and entries[0]["by"] == "identity"
    lines = d.list_lines(run.record, entries)
    assert lines[0].split()[:5] == ["ID", "STATE", "NODES", "DEVICE", "RULE"] and "pico (r1)" in lines[1]
    assert "2e8a:000a 'Pico'" in lines[1] and "E6609CB2" not in lines[1]
    net = d.module("net")
    monkeypatch.setattr(net, "running", lambda: {"mine": run.record})
    printed = []
    assert d.main(["list", "--json"], out=printed.append) == 0
    assert json.loads(printed[0])[0]["nodes"] == entries[0]["nodes"]
    # With --log-debug the run keeps serials in full.
    run.record["log_debug"] = True
    assert d.listing(run.record)[0]["device"]["serial"] == "E6609CB2"


def test_an_older_runs_registry_is_read(world):
    _, record = world
    d.registry(record).write_text(json.dumps(["/dev/ttyUSB0"]))
    assert d.added(record) == ["/dev/ttyUSB0"] and d.load(record)[0]["state"] == "attached"


def test_opens_of_a_node_are_counted_through_inotify(tmp_path):
    node = tmp_path / "ttyACM0"  # inotify on an inode: a device node's opens are seen the same way
    node.write_text("")
    opens = d.Opens()
    try:
        opens.watch(str(node))
        for mode in ("r", "a"):
            with open(node, mode):
                pass
        opens.read(100.0)
        assert opens.records(105.0) == []  # counted, not yet due
        assert opens.records(111.0) == [(str(node), {"opens": 2, "closed_after_write": 1})]
        opens.forget(str(node))
        with open(node):
            pass
        opens.read(200.0)
        assert opens.records(300.0, final=True) == []
    finally:
        opens.close()


def test_one_watcher_per_run(world, monkeypatch):
    _, record = world
    import fcntl
    spawned = []
    monkeypatch.setattr(d.subprocess, "Popen", lambda command, **kw: spawned.append(command))
    assert d.ensure_watcher(record) and spawned[0][-2:] == ["--internal-watch", record["directory"]]
    fd = os.open(Path(record["directory"]) / "device-watch.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert not d.ensure_watcher(record) and len(spawned) == 1
        stop = threading.Event()
        steps = []
        d.watch_devices(record, stop, watcher=types.SimpleNamespace(step=lambda: steps.append(1), close=lambda: None))
        assert steps == []  # another one watches
    finally:
        os.close(fd)
    # Without a launcher alive the loop ends at once; with STOP set as well.
    d.watch_devices({**record, "pid": 2 ** 22 + 7}, threading.Event(),
                    watcher=types.SimpleNamespace(step=lambda: steps.append(1), close=lambda: None))
    assert steps == []
