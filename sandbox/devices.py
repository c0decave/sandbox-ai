#!/usr/bin/env python3
"""sandbox-ai devices / limits: change the hardware and resource limits of a running sandbox.

  sandbox-ai devices list [RUN] [--json]        the run's devices: identity, rule, state
  sandbox-ai devices add [RUN] DEVICE           hand a host device to the running sandbox (as --device)
  sandbox-ai devices remove [RUN] DEVICE|ID     take it away again
  sandbox-ai limits [RUN] [--memory SIZE] [--cpus N] [--pids N]   change the running sandbox's limits

DEVICE: a path under /dev, or usb:VENDOR:PRODUCT[:SERIAL] (the USB device's nodes, found through sysfs).
RUN as for `sandbox-ai net` ($SANDBOX_AI_RUN; may be left out when only one sandbox runs). A device gets
the same checks as --device at the start (refused devices, the udev rule your user needs, Bluetooth
adapters, the administrator's policy, the device rules of the run's rulesets: deny refuses it, ask puts a
question to `sandbox-ai approvals`) and lasts until the sandbox ends or `remove`. A USB device given by
identity is attached again when it is replugged under another node (within 5 minutes; logged to
devices.jsonl).

How: nothing runs in the sandbox. A helper of your own user enters the sandbox's user namespace (you own it),
clones the device node there (open_tree) and mounts it into each of the sandbox's mount namespaces, the
agent's under Firejail included (move_mount; Linux 5.2 or later). Limits go to the sandbox's cgroup:
`systemctl --user set-property --runtime` for a systemd scope, else the cgroup files where delegated.

Standard library only.
"""
import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
SYS_OPEN_TREE, SYS_MOVE_MOUNT = 428, 429  # the same number on every architecture
OPEN_TREE_CLONE, OPEN_TREE_CLOEXEC, AT_RECURSIVE = 1, 0o2000000, 0x8000
MOVE_MOUNT_F_EMPTY_PATH = 0x4
AT_FDCWD = -100
CLONE_NEWNS, CLONE_NEWUSER = 0x00020000, 0x10000000
MNT_DETACH = 2
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_devices", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


# --- which processes are the sandbox ----------------------------------------------------------------


def read(path, limit=1 << 16):
    try:
        with open(path, "rb") as stream:
            return stream.read(limit)
    except OSError:
        return None


def namespace(proc, pid, kind):
    try:
        return os.readlink(proc / str(pid) / "ns" / kind)
    except OSError:
        return None


def cgroup_path(proc, pid):
    """The cgroup v2 path of PID ("/user.slice/..."), or None."""
    data = read(proc / str(pid) / "cgroup")
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        if line.startswith("0::"):
            return line[3:]
    return None


def sandbox_processes(record, proc=PROC):
    """{"pids", "mntns": {ns: pid}, "user": pid, "cgroup"} of the running sandbox RECORD (run.json): the processes
    whose /sandbox/session/config.json is this run's (the session directory is the run's own), and every process
    of the same pod (Podman: the pod's cgroup) or user namespace (Bubblewrap). None if it is not found."""
    want = read(Path(record.get("session_dir", "")) / "config.json")
    if not want:
        return None
    pids = sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()) if proc.is_dir() else []
    anchors = [pid for pid in pids if read(proc / str(pid) / "root" / "sandbox" / "session" / "config.json") == want]
    if not anchors:
        return None
    anchor = anchors[0]
    if record.get("backend") == "podman":
        # All rootless containers share Podman's user namespace; the pod's cgroup is this sandbox's alone.
        own = cgroup_path(proc, anchor) or ""
        prefix = own.rsplit("/", 1)[0] if "/libpod-" in own else own
        members = [pid for pid in pids if prefix and (cgroup_path(proc, pid) or "").startswith(prefix + "/")
                   or pid in anchors]
        group = prefix
    else:
        user = namespace(proc, anchor, "user")
        members = [pid for pid in pids if namespace(proc, pid, "user") == user]
        group = cgroup_path(proc, anchor)
    mounts = {}
    for pid in members:
        ns = namespace(proc, pid, "mnt")
        # Only the sandbox's own mount namespaces (the pod's infra container has no /sandbox).
        if ns and ns not in mounts and (proc / str(pid) / "root" / "sandbox").is_dir():
            mounts[ns] = pid
    return {"pids": members, "mntns": mounts, "user": anchor, "cgroup": group}


# --- the namespace helper (runs in a process of its own: setns needs a single thread) --------------


def libc():
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    return library


def check(result, what):
    if result < 0:
        code = ctypes.get_errno()
        if code == errno.ENOSYS:
            raise OSError(code, f"{what}: this kernel has no new mount API (Linux 5.2 or later is needed)")
        raise OSError(code, f"{what}: {os.strerror(code)}")
    return result


def helper(op, user_pid, device, targets, proc=PROC, c=None):
    """In this (fresh, single-threaded) process: enter the user namespace of USER_PID, then add (or remove)
    DEVICE in the mount namespace of every pid in TARGETS."""
    c = c or libc()
    user = os.open(proc / str(user_pid) / "ns" / "user", os.O_RDONLY)
    mounts = [os.open(proc / str(pid) / "ns" / "mnt", os.O_RDONLY) for pid in targets]
    check(c.setns(user, CLONE_NEWUSER), "enter the sandbox's user namespace")
    trees = []
    if op == "add":
        # A mount namespace of our own, owned by the sandbox's user namespace, may clone the host's node.
        check(c.unshare(CLONE_NEWNS), "a private mount namespace")
        flags = OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC | (AT_RECURSIVE if os.path.isdir(device) else 0)
        trees = [check(c.syscall(SYS_OPEN_TREE, AT_FDCWD, device.encode(), flags), f"clone {device}")
                 for _ in mounts]
    for index, fd in enumerate(mounts):
        check(c.setns(fd, CLONE_NEWNS), "enter a mount namespace of the sandbox")
        if op == "add":
            if not os.path.lexists(device):
                if os.path.isdir(f"/proc/self/fd/{trees[index]}"):
                    os.makedirs(device, exist_ok=True)
                else:
                    os.close(os.open(device, os.O_CREAT | os.O_WRONLY, 0o600))
            check(c.syscall(SYS_MOVE_MOUNT, trees[index], b"", AT_FDCWD, device.encode(), MOVE_MOUNT_F_EMPTY_PATH),
                  f"mount {device}")
        else:
            check(c.umount2(device.encode(), MNT_DETACH), f"unmount {device}")
            if os.path.isfile(device) or (os.path.lexists(device) and not os.path.isdir(device)):
                try:
                    os.unlink(device)
                except OSError:
                    pass
    return 0


def run_helper(op, found, device):
    """Run helper() in a process of its own; (status, error text)."""
    targets = list(found["mntns"].values())
    if not targets:
        return 1, "no mount namespace of the sandbox found"
    result = subprocess.run([sys.executable, "-I", str(Path(__file__)), "--internal-helper", op,
                             str(found["user"]), device, *map(str, targets)],
                            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    return result.returncode, result.stderr.strip()[-500:]


# --- what a device node is: its identity in sysfs and udev ----------------------------------------------------
#
# USB IDs, serial numbers, product names and the udev links made from them (/dev/serial/by-id/...) are what a
# device reports about itself, so another device can claim them: they identify, they do not authenticate. What
# the kernel decides (the node's device number, the sysfs tree it belongs to, the bus and device number of a USB
# device, which change at every plug) is what tells one plugged device from the next.

SYS = Path("/sys")
UDEV_DATA = Path("/run/udev/data")
SELECTOR = re.compile(r"usb:([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})(?::([!-~]{1,126}))?")
DEVNAME = re.compile(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*")
ATTRIBUTE_LIMIT = 4096
NODE_LIMIT = 64          # device nodes of one USB device
USB_SCAN_LIMIT = 512     # USB devices looked at in one search
# The raw node (/dev/bus/usb/BBB/DDD) of such a device is withheld: through it the agent could detach the
# kernel's driver and read keystrokes, a disk, a smart card or a radio directly.
RAW_WITHHELD = {"03": "an input device or security key (HID)", "08": "a disk (USB mass storage)",
                "0b": "a smart-card reader", "e0": "a Bluetooth or wireless adapter"}


def parse_selector(value):
    """{"usb": "vvvv:pppp", "serial": SERIAL or None} of usb:VENDOR:PRODUCT[:SERIAL]; None for anything else."""
    if not isinstance(value, str) or not value.startswith("usb:"):
        return None
    match = SELECTOR.fullmatch(value)
    if not match:
        raise ValueError(f"--device {shown(value)}: usb:VENDOR:PRODUCT[:SERIAL] with four hex digits each (lsusb "
                         "shows them), e.g. usb:2e8a:000a or usb:2e8a:000a:E6609CB2D3A7; the serial is printable "
                         "ASCII without spaces")
    return {"usb": f"{match[1].lower()}:{match[2].lower()}", "serial": match[3]}


def shown(value, limit=200):
    """VALUE as it may be printed: everything outside printable ASCII escaped (device-reported text included)."""
    text = str(value)[:limit]
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape").decode("ascii") for c in text)


def serial_shown(serial, debug=False):
    """A serial number as logs and questions show it: a short hash (it can identify a person's device), in full
    with --log-debug."""
    if not isinstance(serial, str):
        return serial
    if debug:
        return serial
    digest = hashlib.sha256(b"sandbox-ai device serial\0" + serial.encode("utf-8", "replace")).hexdigest()
    return "sha256:" + digest[:12]


def attribute(directory, name):
    """The sysfs attribute NAME of DIRECTORY as text, or None: missing, unreadable, not a regular file (a symlink
    planted where an attribute should be is not followed)."""
    try:
        fd = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, ATTRIBUTE_LIMIT + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > ATTRIBUTE_LIMIT:
        return None
    return data.decode("utf-8", "replace").strip()


def inside(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def node_numbers(path):
    """"MAJOR:MINOR" of the character device PATH (symlinks followed), or None."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISCHR(info.st_mode):
        return None
    return f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"


def sysfs_device(numbers, sys_root=None):
    """The sysfs directory of the character device NUMBERS ("M:m"), or None; only inside SYS/devices (a link
    that leads elsewhere is not followed there)."""
    sys_root = Path(sys_root or SYS)
    if not isinstance(numbers, str) or not re.fullmatch(r"[0-9]{1,10}:[0-9]{1,10}", numbers):
        return None
    try:
        found = (sys_root / "dev" / "char" / numbers).resolve(strict=True)
        top = (sys_root / "devices").resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return found if found != top and inside(found, top) else None


def usb_ancestor(directory, sys_root=None):
    """The USB device (the directory with idVendor and idProduct) DIRECTORY belongs to, or None."""
    sys_root = Path(sys_root or SYS)
    try:
        top = (sys_root / "devices").resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    for candidate in (Path(directory), *Path(directory).parents):
        if candidate == top or not inside(candidate, top):
            return None
        if attribute(candidate, "idVendor") is not None and attribute(candidate, "idProduct") is not None:
            return candidate
    return None


def usb_facts(device_dir):
    """What the USB device in DEVICE_DIR says about itself ("usb", "serial", "product", "classes") and where it is
    ("port", "bus": BBB/DDD, new at every plug); "unknown" lists what could not be read."""
    vendor, product = (attribute(device_dir, name) or "" for name in ("idVendor", "idProduct"))
    facts, unknown = {"port": device_dir.name}, []
    if re.fullmatch(r"[0-9a-fA-F]{4}", vendor) and re.fullmatch(r"[0-9a-fA-F]{4}", product):
        facts["usb"] = f"{vendor.lower()}:{product.lower()}"
    else:
        facts["usb"] = None
        unknown.append("usb")
    serial = attribute(device_dir, "serial")
    if serial is None and (device_dir / "serial").exists():
        unknown.append("serial")  # there, but not readable as an attribute
    elif serial is not None and not (0 < len(serial) <= 126 and all(" " <= c <= "~" for c in serial)):
        serial, unknown = None, unknown + ["serial"]
    facts["serial"] = serial
    for key in ("product", "manufacturer"):
        text = attribute(device_dir, key)
        if text:
            facts[key] = shown(text, 100)
    try:
        bus, number = int(attribute(device_dir, "busnum") or ""), int(attribute(device_dir, "devnum") or "")
        facts["bus"] = f"{bus:03d}/{number:03d}"
    except ValueError:
        facts["bus"] = None
        unknown.append("bus")
    classes = [attribute(device_dir, "bDeviceClass")]
    try:
        interfaces = sorted(device_dir.glob(f"{glob_escape(device_dir.name)}:*"))[:64]
    except OSError:
        interfaces = []
    classes += [attribute(interface, "bInterfaceClass") for interface in interfaces]
    facts["classes"] = sorted({c.lower() for c in classes if c and re.fullmatch(r"[0-9a-fA-F]{2}", c)})
    if not facts["classes"]:
        unknown.append("class")
    facts["unknown"] = unknown
    return facts


def glob_escape(name):
    return re.sub(r"([*?\[])", r"[\1]", name)


def subsystem_of(directory):
    try:
        name = os.path.basename(os.readlink(Path(directory) / "subsystem"))
    except OSError:
        return None
    return name if re.fullmatch(r"[a-z0-9_-]{1,32}", name) else None


def udev_links(numbers, udev_root=None, dev_root=None):
    """The links udev made to the device NUMBERS (/dev/serial/by-id/...), from its database."""
    data = attribute(Path(udev_root or UDEV_DATA), f"c{numbers}") if numbers else None
    dev_root = Path(dev_root or "/dev")
    links = []
    for line in (data or "").splitlines():
        link = line[2:]
        if line.startswith("S:") and DEVNAME.fullmatch(link) and ".." not in link.split("/"):
            links.append(str(dev_root / link))
    return links[:32]


def dev_names(names, dev_root=None):
    """NAMES as rules see them: below /dev (a /dev elsewhere, as in tests, is read as /dev)."""
    root = Path(dev_root or "/dev")
    result = []
    for name in names:
        if root != Path("/dev") and isinstance(name, str) and inside(name, root):
            name = "/dev/" + Path(name).relative_to(root).as_posix()
        result.append(name)
    return list(dict.fromkeys(result))


def identity(path, given=None, sys_root=None, udev_root=None, dev_root=None):
    """FACTS of the character device PATH (resolved) for the rules and the log: "node", "names" (the node, the
    path GIVEN and udev's links to it), "numbers", "subsystem", "usb", "serial", "classes", "port", "bus",
    "product" and "unknown" (what could not be read; see runtime.Ruleset.device)."""
    node = str(path)
    names = list(dict.fromkeys([node] + ([str(given)] if given and str(given) != node else [])))
    numbers = node_numbers(node)
    directory = sysfs_device(numbers, sys_root) if numbers else None
    if directory is None:
        # Not a device sysfs knows (or none at all): nothing about it can be told.
        return {"node": node, "names": dev_names(names, dev_root), "numbers": numbers, "subsystem": None, "usb": None,
                "serial": None, "classes": [], "unknown": ["subsystem", "usb", "serial", "class"]}
    names += [link for link in udev_links(numbers, udev_root, dev_root) if link not in names]
    facts = {"node": node, "names": dev_names(names, dev_root), "numbers": numbers,
             "subsystem": subsystem_of(directory)}
    unknown = [] if facts["subsystem"] else ["subsystem"]
    usb = usb_ancestor(directory, sys_root)
    if usb is None:
        facts.update(usb=None, serial=None, classes=[])
    else:
        found = usb_facts(usb)
        unknown += found.pop("unknown")
        facts.update(found)
    facts["unknown"] = unknown
    return facts


def other_facts(path, kind, given=None, dev_root=None):
    """FACTS of a directory passed whole (/dev/bus/usb, /dev/dri) or a CDI device (nvidia.com/gpu=all)."""
    node = f"cdi:{path}" if kind == "cdi" else str(path)
    names = dev_names([node] + ([str(given)] if given and kind != "cdi" else []), dev_root)
    return {"node": node, "names": names, "numbers": None, "subsystem": None, "usb": None, "serial": None,
            "classes": [], "unknown": []}


def usb_devices(sys_root=None):
    """[directory] of the USB devices present (not their interfaces), from SYS/bus/usb/devices."""
    sys_root = Path(sys_root or SYS)
    try:
        top = (sys_root / "devices").resolve(strict=True)
        entries = sorted((sys_root / "bus" / "usb" / "devices").iterdir())[:USB_SCAN_LIMIT]
    except (OSError, RuntimeError):
        return []
    found = []
    for entry in entries:
        if ":" in entry.name:
            continue  # an interface
        try:
            directory = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if directory != top and inside(directory, top) and directory not in found \
                and attribute(directory, "idVendor") is not None:
            found.append(directory)
    return found


def matching(selector, sys_root=None):
    """[(directory, usb facts)] of the USB devices present that SELECTOR names."""
    found = []
    for directory in usb_devices(sys_root):
        facts = usb_facts(directory)
        if facts["usb"] == selector["usb"] and selector.get("serial") in (None, facts["serial"]):
            found.append((directory, facts))
    return found


def usb_nodes(device_dir, sys_root=None, dev_root=None):
    """([(node, numbers)], notes) of the character devices of the USB device in DEVICE_DIR: its own raw node and its
    interfaces' (tty, video, sound ...), not those of devices behind it (a hub's). The node in /dev must have the
    device number sysfs names, or it is not taken."""
    dev_root = Path(dev_root or "/dev")
    facts = usb_facts(device_dir)
    withheld = sorted(set(facts["classes"]) & set(RAW_WITHHELD))
    nodes, notes = [], []
    for root, directories, files in os.walk(device_dir):  # symlinks (driver, subsystem, port) are not followed
        here = Path(root)
        if here != device_dir and "idVendor" in files:
            directories[:] = []  # another USB device, behind this one
            continue
        directories.sort()
        if "dev" not in files:
            continue
        numbers = attribute(here, "dev")
        event = dict(line.split("=", 1) for line in (attribute(here, "uevent") or "").splitlines() if "=" in line)
        name = event.get("DEVNAME", "")
        if not numbers or not DEVNAME.fullmatch(name) or ".." in name.split("/"):
            continue
        node = str(dev_root / name)
        if subsystem_of(here) == "block":
            notes.append(f"{node} left out: block devices (disks) are never passed to an agent")
            continue
        if node_numbers(node) != numbers:
            notes.append(f"{node} left out: it is not the device {numbers} sysfs names")
            continue
        if here == device_dir and withheld:
            notes.append(f"{node} (raw USB) left out: the device is " + " and ".join(RAW_WITHHELD[c] for c in withheld)
                         + "; through raw USB the agent could take it from its kernel driver")
            continue
        nodes.append((node, numbers))
        if len(nodes) >= NODE_LIMIT:
            notes.append(f"more than {NODE_LIMIT} device nodes: the rest is left out")
            break
    return nodes, notes


def plan(value, launcher=None, option="--device"):
    """What VALUE (--device or `devices add`) gives the agent: ([{"path", "kind", "facts"}], notes, device), DEVICE
    the USB device's facts for usb:VENDOR:PRODUCT[:SERIAL] (one device: several that match are refused, as a
    serial can be claimed by any device), else None. LAUNCHER: agent_sandbox (its checks of device paths)."""
    launcher = launcher or module("agent_sandbox")
    dev_root = launcher.DEV
    selector = parse_selector(value)
    if selector is None:
        path, kind = launcher.parse_device(value, option)
        facts = identity(path, value, dev_root=dev_root) if kind == "node" else \
            other_facts(path, kind, value, dev_root)
        return [{"path": path, "kind": kind, "facts": facts}], [], None
    found = matching(selector)
    if not found:
        raise ValueError(f"{option} {shown(value)}: no such USB device is plugged in (lsusb lists them)")
    if len(found) > 1:
        what = "devices claim this serial number" if selector["serial"] else "devices have these IDs"
        serials = sorted({serial_shown(f["serial"]) for _, f in found if f.get("serial")})
        raise ValueError(f"{option} {shown(value)}: {len(found)} {what} (ports "
                         + ", ".join(shown(f["port"], 40) for _, f in found) + "); "
                         + ("name one by its serial, usb:VENDOR:PRODUCT:SERIAL, or by its path"
                            if not selector["serial"] else
                            "a serial number is what a device claims: pass the one you mean by its path")
                         + (f" (serials {', '.join(serials)})" if serials and not selector["serial"] else ""))
    directory, device = found[0]
    nodes, notes = usb_nodes(directory, dev_root=dev_root)
    entries = []
    for node, _ in nodes:
        try:
            path, kind = launcher.parse_device(node, f"{option} {shown(value)}")
        except ValueError as exc:
            notes.append(f"{node} left out: {exc}")
            continue
        entries.append({"path": path, "kind": kind, "facts": identity(path, dev_root=dev_root)})
    if not entries:
        raise ValueError(f"{option} {shown(value)}: no device node of it can be passed"
                         + (f" ({'; '.join(notes)})" if notes else " (it has none)"))
    return entries, notes, device


def judge(rules, entries):
    """[(entry, verdict)] of ENTRIES under RULES (a runtime Ruleset)."""
    return [(entry, rules.device(entry["facts"])) for entry in entries]


# --- the run's devices: devices.json, devices.jsonl, questions ---------------------------------------------------
#
# devices.json in the run's directory (hidden from the agent) holds every grant of the run: what was given
# (a path or usb:...), what it is, which nodes are attached, its state and the rule that decided. The launcher
# writes the grants of the start; `devices add` and `remove` change it; the watcher follows the devices while
# the sandbox runs. All of them hold devices.lock while they change it.

STATES = ("attached", "held", "gone", "dropped", "refused", "removed")
APPROVAL_SECONDS = 60
POLL_SECONDS = 1.0       # how often the watcher looks at sysfs
REPLUG_SECONDS = 300     # how long a device may be gone before its grant is dropped
OPEN_RECORD_SECONDS = 10  # opens of a node are counted, one record per node at most this often


def registry(record):
    return Path(record["directory"]) / "devices.json"


@contextlib.contextmanager
def locked(record):
    """devices.lock of the run, held (the registry is changed by the CLI and the watcher)."""
    fd = os.open(Path(record["directory"]) / "devices.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load(record):
    """[grant] of the run (an older run's list of paths read as grants added by path)."""
    try:
        data = json.loads(registry(record).read_text())
    except (OSError, ValueError):
        return []
    if isinstance(data, list):
        return [{"id": str(index), "value": path, "by": "path", "source": "added", "kind": "node", "path": path,
                 "nodes": {path: None}, "state": "attached", "track": False}
                for index, path in enumerate((p for p in data if isinstance(p, str)), 1)]
    grants = data.get("grants") if isinstance(data, dict) else None
    return [g for g in grants if isinstance(g, dict) and g.get("state") in STATES] if isinstance(grants, list) else []


def save(record, grants):
    path = registry(record)
    temporary = path.with_name(".devices.json")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as stream:
        json.dump({"version": 2, "grants": grants}, stream)
    os.replace(temporary, path)


def added(record):
    """The values of the devices added during the run and not removed."""
    return [g["value"] for g in load(record) if g.get("source") == "added" and g.get("state") != "removed"]


def next_id(grants):
    return str(max([int(g["id"]) for g in grants if str(g.get("id", "")).isdigit()] + [0]) + 1)


def debug(record):
    return bool(record.get("log_debug"))


def value_shown(value, debug_on=False):
    """A --device value for logs: the serial of usb:VENDOR:PRODUCT:SERIAL hashed (see serial_shown)."""
    selector = parse_selector(value) if isinstance(value, str) and value.startswith("usb:") and \
        SELECTOR.fullmatch(value) else None
    if selector and selector["serial"]:
        return f"usb:{selector['usb']}:{serial_shown(selector['serial'], debug_on)}"
    return shown(value, 300)


def device_shown(facts, debug_on=False):
    """The identity fields of FACTS for logs, questions and lists (the serial hashed)."""
    if not isinstance(facts, dict):
        return {}
    result = {key: facts[key] for key in ("usb", "product", "manufacturer", "port", "bus", "subsystem", "classes")
              if facts.get(key) not in (None, [], "")}
    if facts.get("serial"):
        result["serial"] = serial_shown(facts["serial"], debug_on)
    return result


def log(record, event, grant=None, **fields):
    """One line of devices.jsonl in the run's log directory (none without one): EVENT about GRANT."""
    directory = record.get("log_dir")
    if not directory:
        return
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "run": record.get("run")}
    if grant:
        entry.update(grant=grant.get("id"), value=value_shown(grant.get("value"), debug(record)), by=grant.get("by"),
                     source=grant.get("source"))
        for key in ("rule", "ruleset", "decision"):
            if grant.get(key) is not None:
                entry[key] = grant[key]
        if grant.get("device"):
            entry["device"] = device_shown(grant["device"], debug(record))
    entry.update({key: value for key, value in fields.items() if value is not None})
    try:
        fd = os.open(Path(directory) / "devices.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600)
        with open(fd, "a") as stream:
            stream.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def run_rules(record):
    """The rulesets that judge this run's devices: the ones its start had (device-rules.json, written by the
    launcher) and the administrator's policy as it is now, so a policy changed during the run holds too."""
    runtime = module("runtime")
    sources = []
    try:
        saved = json.loads((Path(record["directory"]) / "device-rules.json").read_text())
    except (OSError, ValueError):
        saved = []
    sources += [s for s in saved if isinstance(s, dict)] if isinstance(saved, list) else []
    policy = module("agent_sandbox").load_policy().get("rules")
    if policy is not None:
        sources.insert(0, {"name": "policy", **runtime.Ruleset.validated(policy, "the administrator's policy: rules")})
    try:
        return runtime.Ruleset(sources)
    except runtime.RuleError as exc:
        raise ValueError(str(exc)) from None


def question_id():
    return f"{int(time.time())}-{secrets.randbelow(10 ** 9)}-{secrets.randbelow(10 ** 6) + 1}"


def file_question(record, grant, entries, verdict):
    """Put a device question (kind "device") to the human, as the sandbox's own questions: in the run's approvals
    directory, where `sandbox-ai approvals`, the app and the desktop notifications find it. Its ID."""
    directory = Path(record["directory"]) / "approvals"
    directory.mkdir(mode=0o700, exist_ok=True)
    ident = question_id()
    question = {"id": ident, "kind": "device", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "timeout": APPROVAL_SECONDS, "value": value_shown(grant.get("value"), debug(record)),
                "nodes": [entry["path"] for entry in entries], "rule": verdict.get("rule"),
                "ruleset": verdict.get("ruleset"), "reason": shown(verdict.get("reason") or "", 500),
                "replug": bool(grant.get("after_replug")), "sandbox": {"run": record.get("run")},
                **device_shown(grant.get("device") or (entries[0]["facts"] if entries else {}), debug(record))}
    with open(os.open(directory / f"{ident}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
              "w") as stream:
        json.dump(question, stream)
    log(record, "ask", grant, question=ident, nodes=question["nodes"], reason=question["reason"])
    return ident


def read_answer(record, ident):
    """{"decision", "scope"} of the answer to question IDENT, or None while there is none."""
    try:
        answer = json.loads((Path(record["directory"]) / "approvals" / f"{ident}.answer").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(answer, dict) or answer.get("decision") not in ("allow", "deny"):
        return {"decision": "deny", "scope": "once", "reason": "unreadable answer"}
    scope = answer.get("scope") if answer.get("scope") in ("once", "run") else "run" if \
        answer.get("scope") == "always" else "once"  # a device is approved for the run at most
    return {"decision": answer["decision"], "scope": scope, "reason": "answered"}


def close_question(record, ident, answer):
    with contextlib.suppress(OSError):
        path = Path(record["directory"]) / "approvals" / f"{ident}.done"
        path.write_text(json.dumps({**answer, "id": ident}))


def wait_answer(record, ident, seconds=APPROVAL_SECONDS, poll=0.2, clock=time.monotonic):
    deadline = clock() + seconds
    while clock() < deadline:
        answer = read_answer(record, ident)
        if answer:
            return answer
        time.sleep(poll)
    return {"decision": "deny", "scope": "once", "reason": f"no answer within {seconds} s"}


# --- attaching and detaching ------------------------------------------------------------------------------------


def attach_nodes(record, paths, found, runner):
    """Mount PATHS into the sandbox FOUND; the paths attached and the errors."""
    done, errors = [], []
    for path in paths:
        status, error = runner("add", found, path)
        (errors.append(f"{path}: {error or 'the helper failed'}") if status else done.append(path))
    return done, errors


def detach_nodes(record, paths, found, runner):
    errors = []
    for path in paths:
        status, error = runner("remove", found, path)
        if status:
            errors.append(f"{path}: {error or 'the helper failed'}")
    return errors


def grant_from(value, entries, verdicts, device, source, approved=None):
    """A new grant of VALUE: ENTRIES (the nodes to attach) with their VERDICTS."""
    strictest = max(verdicts, key=lambda v: ("allow", "ask", "deny").index(v["decision"])) if verdicts else \
        {"decision": "allow", "rule": None, "ruleset": None}
    first = entries[0]["facts"] if entries else {}
    selector = parse_selector(value)
    usb_backed = bool(device) or isinstance(first.get("usb"), str)
    return {"value": value, "by": "identity" if selector else "path", "source": source,
            "selector": selector, "kind": entries[0]["kind"] if entries else "node",
            "path": entries[0]["path"] if entries and not selector else None,
            "device": {key: (device or first).get(key) for key in ("usb", "serial", "product", "manufacturer", "port",
                                                                  "bus", "classes")} if usb_backed else None,
            "nodes": {entry["path"]: entry["facts"].get("numbers") for entry in entries},
            "track": usb_backed and all(entry["kind"] == "node" for entry in entries),
            "decision": strictest["decision"], "rule": strictest.get("rule"), "ruleset": strictest.get("ruleset"),
            "approved": approved, "since": time.time(), "replugs": 0}


def present(grant, sys_root=None):
    """Whether the device GRANT attached is still there: every node with its device number, on the same plugged
    USB device (vendor, product, serial, bus and device number). Else the reason it is not."""
    for node, numbers in (grant.get("nodes") or {}).items():
        now = node_numbers(node)
        if now is None:
            return "unplugged"
        if numbers and now != numbers:
            return "another device has the node now"
        device = grant.get("device")
        if device and numbers:
            directory = sysfs_device(numbers, sys_root)
            usb = usb_ancestor(directory, sys_root) if directory else None
            if usb is None:
                return "unplugged"
            facts = usb_facts(usb)
            if any(facts.get(key) != device.get(key) for key in ("usb", "serial", "bus")):
                return "another device has the node now"
    return None


# --- inotify: who opens the nodes ------------------------------------------------------------------------------


class Opens:
    """Opens of the attached nodes, through inotify on the host: a watch is on the device node's inode, which the
    sandbox's bind mount shares, so it sees opens in the sandbox's mount namespaces and also the host's own (they
    cannot be told apart here). Counted per node, one record at most every OPEN_RECORD_SECONDS."""
    IN_CLOSE_WRITE, IN_CLOSE_NOWRITE, IN_OPEN, IN_IGNORED = 0x8, 0x10, 0x20, 0x8000

    def __init__(self, c=None):
        self.c = c or ctypes.CDLL(None, use_errno=True)
        self.fd = self.c.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        self.watches, self.counts = {}, {}

    def watch(self, node):
        if self.fd < 0 or node in self.watches.values():
            return
        wd = self.c.inotify_add_watch(self.fd, os.fsencode(node), self.IN_OPEN | self.IN_CLOSE_WRITE
                                      | self.IN_CLOSE_NOWRITE)
        if wd >= 0:
            self.watches[wd] = node

    def forget(self, node):
        for wd, watched in list(self.watches.items()):
            if watched == node:
                self.c.inotify_rm_watch(self.fd, wd)
                del self.watches[wd]

    def read(self, now):
        if self.fd < 0:
            return
        while True:
            try:
                data = os.read(self.fd, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            offset = 0
            while offset + 16 <= len(data):
                wd, mask, _, length = struct.unpack_from("iIII", data, offset)
                offset += 16 + length
                node = self.watches.get(wd)
                if mask & self.IN_IGNORED:
                    self.watches.pop(wd, None)
                if node is None:
                    continue
                count = self.counts.setdefault(node, {"opens": 0, "closed_after_write": 0, "first": now})
                count["opens"] += bool(mask & self.IN_OPEN)
                count["closed_after_write"] += bool(mask & self.IN_CLOSE_WRITE)

    def records(self, now, final=False):
        """[(node, counts)] due now (all with FINAL)."""
        due = [(node, count) for node, count in self.counts.items()
               if final or now - count["first"] >= OPEN_RECORD_SECONDS]
        for node, _ in due:
            del self.counts[node]
        return [(node, {k: v for k, v in count.items() if k != "first"}) for node, count in due
                if count["opens"] or count["closed_after_write"]]

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


# --- the watcher: unplug, replug, questions -----------------------------------------------------------------------


class Watcher:
    """Follows the run's grants while it runs (one step every POLL_SECONDS):

    - attached and tracked (on USB): when the device is unplugged, or another device has its node now, its nodes
      are taken from the sandbox (the stale node would open whatever gets that device number next) - "detach";
    - gone: a grant by identity is attached again when a device with its vendor, product (and serial) appears,
      under whatever node, and the rules still allow it - "replug"; a grant by path only when the same device
      comes back at the same path. After REPLUG_SECONDS gone the grant is dropped - "drop";
    - held: its question is filed, and the nodes attached once it is allowed - "answer", "attach"."""

    def __init__(self, record, runner=None, finder=None, rules=None, clock=time.time, opens=None, launcher=None):
        self.record, self.clock = record, clock
        self.runner, self.finder = runner or run_helper, finder or sandbox_processes
        self.rules = rules
        self.launcher = launcher
        self.opens = opens
        self.found, self.found_time = None, 0.0

    def sandbox(self):
        """The sandbox's processes (looked up at most every 5 s)."""
        now = self.clock()
        if self.found is None or now - self.found_time > 5:
            self.found, self.found_time = self.finder(self.record), now
        return self.found

    def ruleset(self):
        if self.rules is None:
            self.rules = run_rules(self.record)
        return self.rules

    def step(self):
        with locked(self.record):
            grants = load(self.record)
            before = json.dumps(grants, sort_keys=True)
            for grant in grants:
                try:
                    if grant.get("state") == "attached" and grant.get("track"):
                        self.check(grant, grants)
                    elif grant.get("state") == "gone":
                        self.replug(grant, grants)
                    elif grant.get("state") == "held":
                        self.pursue(grant)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    log(self.record, "error", grant, reason=shown(exc, 500))
                if self.opens is not None:
                    for node in grant.get("nodes") or {}:
                        (self.opens.watch if grant.get("state") == "attached" else self.opens.forget)(node)
            if json.dumps(grants, sort_keys=True) != before:
                save(self.record, grants)
        if self.opens is not None:
            self.opens.read(self.clock())
            for node, counts in self.opens.records(self.clock()):
                log(self.record, "open", None, node=node, **counts, seen="every process that opened it, the host's "
                    "own included")
        return grants

    def check(self, grant, grants):
        reason = present(grant)
        if reason is None:
            return
        found = self.sandbox()
        errors = detach_nodes(self.record, list(grant["nodes"]), found, self.runner) if found else []
        grant.update(state="gone", since=self.clock())
        log(self.record, "detach", grant, nodes=list(grant["nodes"]), reason=reason,
            errors=errors or None)

    def candidates(self, grant, grants):
        """(entries, notes, device) of the device that may take GRANT's place now, or None."""
        launcher = self.launcher or module("agent_sandbox")
        taken = {g["device"].get("bus") for g in grants if g is not grant and g.get("state") == "attached"
                 and g.get("device")}
        if grant.get("by") == "identity":
            found = [(d, f) for d, f in matching(grant["selector"]) if f.get("bus") not in taken]
            if len(found) > 1:
                port = (grant.get("device") or {}).get("port")
                found = [(d, f) for d, f in found if f.get("port") == port] or found
            if len(found) != 1:
                if len(found) > 1 and grant.get("noted") != "several":
                    grant["noted"] = "several"
                    log(self.record, "replug", grant, outcome="waiting", reason=f"{len(found)} devices match; "
                        "none is attached")
                return None
            directory, device = found[0]
            nodes, notes = usb_nodes(directory, dev_root=launcher.DEV)
            entries = []
            for node, _ in nodes:
                try:
                    path, kind = launcher.parse_device(node, f"--device {value_shown(grant['value'])}")
                except ValueError as exc:
                    notes.append(f"{node} left out: {exc}")
                    continue
                entries.append({"path": path, "kind": kind, "facts": identity(path, dev_root=launcher.DEV)})
            return (entries, notes, device) if entries else None
        path = grant.get("path")
        if not path or node_numbers(path) is None:
            return None
        facts = identity(path, grant.get("value"), dev_root=launcher.DEV)
        old = grant.get("device") or {}
        if facts.get("bus") in taken or any(facts.get(key) != old.get(key) for key in ("usb", "serial")):
            if grant.get("noted") != facts.get("bus"):
                grant["noted"] = facts.get("bus")
                log(self.record, "refuse", grant, node=path, reason="another device has the path now",
                    other=device_shown(facts, debug(self.record)))
            return None
        return [{"path": path, "kind": "node", "facts": facts}], [], facts

    def replug(self, grant, grants):
        if self.clock() - grant.get("since", 0) > REPLUG_SECONDS:
            grant.update(state="dropped", since=self.clock())
            log(self.record, "drop", grant, reason=f"gone for more than {REPLUG_SECONDS} s")
            return
        candidate = self.candidates(grant, grants)
        if candidate is None:
            return
        entries, notes, device = candidate
        verdicts = judge(self.ruleset(), entries)
        allowed = [(e, v) for e, v in verdicts if v["decision"] != "deny"]
        if not allowed:
            if grant.get("noted") != device.get("bus"):
                grant["noted"] = device.get("bus")
                log(self.record, "refuse", grant, nodes=[e["path"] for e in entries],
                    reason=shown(verdicts[0][1]["reason"], 500), rule=verdicts[0][1]["rule"],
                    ruleset=verdicts[0][1]["ruleset"], replug=True)
            return
        grant["device"] = {key: device.get(key) for key in ("usb", "serial", "product", "manufacturer", "port",
                                                            "bus", "classes")}
        grant["pending"] = [e["path"] for e, _ in allowed]
        grant["pending_numbers"] = {e["path"]: e["facts"].get("numbers") for e, _ in allowed}
        asking = [v for _, v in allowed if v["decision"] == "ask"]
        if asking and grant.get("approved") != "run":
            grant["after_replug"] = True
            grant.update(state="held", question=file_question(self.record, grant, [e for e, _ in allowed], asking[0]),
                         asked=self.clock(), rule=asking[0]["rule"], ruleset=asking[0]["ruleset"])
            return
        self.attach(grant, "replug")

    def attach(self, grant, event):
        found = self.sandbox()
        if not found:
            return False
        done, errors = attach_nodes(self.record, grant.get("pending") or [], found, self.runner)
        numbers = grant.pop("pending_numbers", {}) or {}
        grant.pop("pending", None)
        if not done:
            grant.update(state="refused", since=self.clock())
            log(self.record, "refuse", grant, reason="could not attach: " + "; ".join(errors))
            return False
        grant.update(state="attached", since=self.clock(), nodes={path: numbers.get(path) for path in done})
        if event == "replug":
            grant["replugs"] = grant.get("replugs", 0) + 1
        grant.pop("noted", None)
        log(self.record, event, grant, nodes=done, errors=errors or None)
        return True

    def pursue(self, grant):
        if not grant.get("question"):
            entries = [{"path": p, "facts": {}} for p in grant.get("pending") or []]
            verdict = {"rule": grant.get("rule"), "ruleset": grant.get("ruleset"),
                       "reason": f"rule {grant.get('rule')} ({grant.get('ruleset')}) asks about this device"}
            grant.update(question=file_question(self.record, grant, entries, verdict), asked=self.clock())
            return
        answer = grant.get("answer") or read_answer(self.record, grant["question"])
        if answer is None:
            if self.clock() - grant.get("asked", 0) <= APPROVAL_SECONDS:
                return
            answer = {"decision": "deny", "scope": "once", "reason": f"no answer within {APPROVAL_SECONDS} s"}
        if not grant.get("answer"):
            close_question(self.record, grant["question"], answer)
            log(self.record, "answer", grant, question=grant["question"], answer=answer["decision"],
                scope=answer["scope"], reason=answer["reason"])
            grant["answer"] = answer
        if answer["decision"] != "allow":
            grant.update(state="refused", since=self.clock())
            grant.pop("answer", None)
            return
        grant["approved"] = answer["scope"]
        if self.attach(grant, "replug" if grant.get("after_replug") else "attach"):
            grant.pop("answer", None)
            grant.pop("after_replug", None)

    def close(self):
        if self.opens is not None:
            for node, counts in self.opens.records(self.clock(), final=True):
                log(self.record, "open", None, node=node, **counts, seen="every process that opened it, the host's "
                    "own included")
            self.opens.close()


def run_alive(record):
    """The run's directory is there and its launcher runs."""
    if not Path(record["directory"], "run.json").exists():
        return False
    try:
        os.kill(int(record.get("pid")), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def watch_devices(record, stop, interval=POLL_SECONDS, watcher=None):
    """The watcher's loop until STOP is set or the run ends (a thread of the launcher, or `--internal-watch`, which
    `devices add` starts when no watcher runs). Only one runs per run: it holds device-watch.lock."""
    try:
        fd = os.open(Path(record["directory"]) / "device-watch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return  # another one watches
    if watcher is None:
        try:
            opens = Opens()
        except OSError:
            opens = None
        watcher = Watcher(record, opens=opens)
    try:
        while not stop.is_set() and run_alive(record):
            try:
                watcher.step()
            except (OSError, ValueError) as exc:
                log(record, "error", None, reason=shown(exc, 500))
            stop.wait(interval)
    finally:
        watcher.close()
        os.close(fd)


def ensure_watcher(record):
    """Start a watcher for RUN unless one runs (it ends with the run)."""
    path = Path(record["directory"]) / "device-watch.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False  # one runs
    finally:
        os.close(fd)
    subprocess.Popen([sys.executable, "-I", str(Path(__file__)), "--internal-watch", str(record["directory"])],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def start_grants(record, grants):
    """The launcher's part: the grants of the start in devices.json, logged ("grant", "held" for a question)."""
    with locked(record):
        existing = load(record)
        for grant in grants:
            grant["id"] = next_id(existing)
            existing.append(grant)
            log(record, "grant", grant, nodes=list(grant.get("nodes") or grant.get("pending") or []),
                state=grant.get("state"))
        save(record, existing)
    return existing


# --- devices add / remove / list ----------------------------------------------------------------------------------


def checked_device(value, launcher=None):
    """(entries, notes, device) of VALUE for a running sandbox: --device's checks, the administrator's policy."""
    launcher = launcher or module("agent_sandbox")
    policy = launcher.load_policy()
    if any(entry.split("=", 1)[0] == "--device" for entry in policy.get("forbid", [])):
        raise ValueError(f"--device is not allowed by the administrator's policy ({launcher.POLICY_FILE})")
    entries, notes, device = plan(value, launcher)
    if parse_selector(value) is None:
        a = argparse.Namespace(device=[value], gpu=None, backend="podman", podman_url=None, rulesets=[])
        notes = notes + launcher.device_notes(a)
    return entries, notes, device


def active(grants):
    return [g for g in grants if g.get("state") in ("attached", "held", "gone")]


def add(record, value, out=print, runner=run_helper, finder=sandbox_processes, rules=None, wait=APPROVAL_SECONDS,
        launcher=None, watcher=ensure_watcher):
    entries, notes, device = checked_device(value, launcher)
    paths = [entry["path"] for entry in entries]

    def taken(grants):
        for grant in active(grants):
            if grant.get("value") == value or set(paths) & set(grant.get("nodes") or {}) or \
                    set(paths) & set(grant.get("pending") or []):
                raise ValueError(f"{shown(value)} was added already (grant {grant.get('id')}, {grant.get('state')})")
    taken(load(record))
    rules = rules or run_rules(record)
    verdicts = judge(rules, entries)
    refused = [(e, v) for e, v in verdicts if v["decision"] == "deny"]
    allowed = [(e, v) for e, v in verdicts if v["decision"] != "deny"]
    for entry, verdict in refused:
        notes.append(f"{entry['path']} refused: {verdict['reason']}")
    if not allowed:
        grant = {**grant_from(value, entries, [v for _, v in verdicts], device, "added"), "id": "-"}
        log(record, "refuse", grant, nodes=paths, reason=shown(refused[0][1]["reason"], 500))
        raise ValueError(f"{shown(value)}: {shown(refused[0][1]['reason'], 500)}")
    entries = [e for e, _ in allowed]
    grant = grant_from(value, entries, [v for _, v in allowed], device, "added")
    asking = [v for _, v in allowed if v["decision"] == "ask"]
    if asking:
        ident = file_question(record, {**grant, "id": "-"}, entries, asking[0])
        out(f"a rule asks about {shown(value)} ({asking[0]['rule']} of {asking[0]['ruleset']}): answer within "
            f"{wait} s with sandbox-ai approvals allow {ident} (or deny), in the app or the notification")
        answer = wait_answer(record, ident, wait)
        close_question(record, ident, answer)
        log(record, "answer", {**grant, "id": "-"}, question=ident, answer=answer["decision"], scope=answer["scope"],
            reason=answer["reason"])
        if answer["decision"] != "allow":
            raise ValueError(f"{shown(value)} was not approved ({answer['reason']})")
        grant["approved"] = answer["scope"]
    found = finder(record)
    if not found:
        raise ValueError("the sandbox's processes were not found (has it ended?)")
    with locked(record):
        grants = load(record)
        taken(grants)
        done, errors = attach_nodes(record, [e["path"] for e in entries], found, runner)
        if not done:
            raise ValueError(f"could not add {shown(value)}: {'; '.join(errors)}")
        if errors:
            notes += [f"not attached: {error}" for error in errors]
        grant.update(id=next_id(grants), state="attached", nodes={path: grant["nodes"].get(path) for path in done})
        grants.append(grant)
        save(record, grants)
    log(record, "grant", grant, nodes=done)
    log(record, "attach", grant, nodes=done)
    for note in notes:
        out(f"  {shown(note, 1000)}")
    out(f"added {', '.join(done)} to {record.get('run')} (until it ends, or: sandbox-ai devices remove "
        f"{record.get('run')} {shown(value)})")
    if grant["track"]:
        watcher(record)
    return done[0] if len(done) == 1 else done


def remove(record, value, out=print, runner=run_helper, finder=sandbox_processes):
    with locked(record):
        grants = load(record)
        resolved = str(Path(value).resolve()) if value.startswith("/") else value
        matches = [g for g in active(grants) if g.get("source") == "added" and (
            g.get("value") == value or g.get("id") == value or resolved in (g.get("nodes") or {}))]
        if not matches:
            raise ValueError(f"{shown(value)} was not added during this run (devices given at the start stay)")
        grant = matches[0]
        if grant.get("state") == "attached":
            found = finder(record)
            if not found:
                raise ValueError("the sandbox's processes were not found (has it ended?)")
            errors = detach_nodes(record, list(grant["nodes"]), found, runner)
            if errors:
                raise ValueError(f"could not remove {shown(value)}: {'; '.join(errors)}")
        grant.update(state="removed", since=time.time())
        save(record, grants)
    log(record, "remove", grant, nodes=list(grant.get("nodes") or {}))
    out(f"removed {', '.join(grant.get('nodes') or {}) or shown(value)} from {record.get('run')}")


def listing(record):
    """The grants as `devices list --json` shows them (serials hashed unless the run logs with --log-debug)."""
    result = []
    for grant in load(record):
        result.append({"id": grant.get("id"), "value": value_shown(grant.get("value"), debug(record)),
                       "by": grant.get("by"), "source": grant.get("source"), "state": grant.get("state"),
                       "nodes": list(grant.get("nodes") or {}), "device": device_shown(grant.get("device"),
                                                                                       debug(record)),
                       "decision": grant.get("decision"), "rule": grant.get("rule"), "ruleset": grant.get("ruleset"),
                       "approved": grant.get("approved"), "replugs": grant.get("replugs", 0),
                       "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(grant.get("since") or 0))})
    return result


def list_lines(record, entries):
    if not entries:
        return [f"no devices given to {record.get('run')}"]
    rows = [["ID", "STATE", "NODES", "DEVICE", "RULE", "GIVEN"]]
    for entry in entries:
        device = entry["device"]
        what = " ".join(filter(None, [device.get("usb"), device.get("product") and repr(device["product"]),
                                      device.get("serial") and f"serial {device['serial']}",
                                      device.get("port") and f"port {device['port']}"])) or "-"
        rule = f"{entry['rule']} ({entry['ruleset']})" if entry.get("rule") else "-"
        rows.append([str(entry["id"]), entry["state"] + (f", {entry['replugs']} replugs" if entry["replugs"] else ""),
                     ", ".join(entry["nodes"]) or "-", shown(what, 200), shown(rule, 120),
                     f"{entry['value']} ({entry['source']})"])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[-1] for row in rows]


# --- limits ---------------------------------------------------------------------------------------------


def limit_properties(memory=None, cpus=None, pids=None):
    """systemd properties and cgroup files of the new limits (validated as --memory/--cpus/--pids)."""
    l = module("agent_sandbox")
    a = argparse.Namespace(memory=memory or "none", cpus=cpus or "none", pids=pids or "none")
    values = l.resource_limits(a)  # validated as at the start
    properties, files = [], {}
    if memory:
        size = values.get("memory", memory).upper() if memory != "none" else "infinity"
        properties += [f"MemoryMax={size}", f"MemorySwapMax={size}"]
        files["memory.max"] = files["memory.swap.max"] = bytes_of(size) if size != "infinity" else "max"
    if cpus:
        if cpus == "none":
            properties.append("CPUQuota=")
            files["cpu.max"] = "max 100000"
        else:
            properties.append(f"CPUQuota={int(round(float(values.get('cpus', cpus)) * 100))}%")
            files["cpu.max"] = f"{int(round(float(values.get('cpus', cpus)) * 100000))} 100000"
    if pids:
        properties.append(f"TasksMax={'infinity' if pids == 'none' else values.get('pids', pids)}")
        files["pids.max"] = "max" if pids == "none" else str(values.get("pids", pids))
    return properties, files


def bytes_of(size):
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    size = size.upper()
    return str(int(float(size[:-1]) * units[size[-1]])) if size[-1] in units else str(int(size))


def set_limits(record, memory=None, cpus=None, pids=None, out=print, finder=sandbox_processes, runner=None,
               cgroup_root=CGROUP_ROOT):
    if not (memory or cpus or pids):
        raise ValueError("give --memory, --cpus or --pids")
    # A strict security mode caps the limits at the start; a change during the run keeps to the same cap.
    mode = record.get("mode") if isinstance(record.get("mode"), dict) else {}
    modes = module("modes")
    if mode.get("name") in modes.MODES:
        wider = modes.limit_widening(argparse.Namespace(memory=memory, cpus=cpus, pids=pids), mode["name"])
        if wider:
            raise ValueError("; ".join(f"{given}: {why} (security mode {mode['name']})" for given, why in wider))
    properties, files = limit_properties(memory, cpus, pids)
    found = finder(record)
    if not found or not found.get("cgroup"):
        raise ValueError("the sandbox's cgroup was not found (has it ended?)")
    unit = found["cgroup"].rsplit("/", 1)[-1]
    runner = runner or subprocess.run
    if unit.endswith((".scope", ".slice", ".service")) and shutil.which("systemctl"):
        result = runner(["systemctl", "--user", "set-property", "--runtime", unit, *properties],
                        capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            out(f"limits of {record.get('run')}: {' '.join(properties)} (systemd unit {unit}, until it ends)")
            return "systemd"
    directory = cgroup_root / found["cgroup"].lstrip("/")
    try:
        for name, value in files.items():
            if (directory / name).exists():
                (directory / name).write_text(value)
    except OSError as exc:
        raise ValueError(f"could not change the limits in {directory}: {exc.strerror} (is the cgroup delegated to "
                         "you? a systemd user session does that)") from None
    out(f"limits of {record.get('run')}: {', '.join(f'{k}={v}' for k, v in files.items())} (cgroup {directory})")
    return "cgroup"


# --- command line -------------------------------------------------------------------------------------------


def main(argv, out=print, prog="devices"):
    net = module("net")
    if prog == "limits":
        parser = argparse.ArgumentParser(prog="sandbox-ai limits", description="Change a running sandbox's limits.")
        parser.add_argument("run", nargs="?")
        parser.add_argument("-me", "--memory", metavar="SIZE", help="e.g. 8g, 512m; none: no limit")
        parser.add_argument("-cu", "--cpus", metavar="N", help="e.g. 2 or 1.5; none: no limit")
        parser.add_argument("-pi", "--pids", metavar="N", help="processes and threads; none: no limit")
    else:
        parser = argparse.ArgumentParser(prog="sandbox-ai devices", description=__doc__.split("\n\n")[0],
                                         formatter_class=argparse.RawDescriptionHelpFormatter,
                                         epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
        commands = parser.add_subparsers(dest="command", required=True)
        lister = commands.add_parser("list")
        lister.add_argument("run", nargs="?")
        lister.add_argument("--json", action="store_true", help="as JSON")
        for name in ("add", "remove"):
            command = commands.add_parser(name)
            command.add_argument("args", nargs="+", metavar="[RUN] DEVICE")
    args = parser.parse_args(argv)
    try:
        if prog == "limits":
            record = net.choose(net.running(), args.run)
            set_limits(record, args.memory, args.cpus, args.pids, out=out)
            return 0
        if args.command == "list":
            record = net.choose(net.running(), args.run)
            entries = listing(record)
            out(json.dumps(entries, indent=2) if args.json else "\n".join(list_lines(record, entries)))
            return 0
        if len(args.args) > 2:
            raise ValueError("give [RUN] DEVICE")
        run, value = args.args if len(args.args) == 2 else (None, args.args[0])
        record = net.choose(net.running(), run)
        (add if args.command == "add" else remove)(record, value, out=out)
        return 0
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai {prog}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--internal-helper":
        try:
            sys.exit(helper(sys.argv[2], int(sys.argv[3]), sys.argv[4], [int(p) for p in sys.argv[5:]]))
        except OSError as exc:
            print(exc.strerror or str(exc), file=sys.stderr)
            sys.exit(1)
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-watch":
        directory = Path(sys.argv[2])
        try:
            run = {**json.loads((directory / "run.json").read_text()), "directory": str(directory)}
        except (OSError, ValueError):
            sys.exit(1)
        import threading
        watch_devices(run, threading.Event())
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--limits":
        sys.exit(main(sys.argv[2:], prog="limits"))
    sys.exit(main(sys.argv[1:]))
