"""What this computer has, for the forms: memory and CPUs (to show the most a sandbox can get), the devices a
sandbox could be given (serial ports, USB, KVM, cameras ...) and the GPU kinds the launcher would accept. No Qt.

Read from /proc, /sys and /dev only (no commands run); every function returns something usable on a system where
a file is missing (macOS: memory through sysctl's numbers in os.sysconf where Python has them, no devices)."""
from __future__ import annotations

import glob
import os
from pathlib import Path

from .i18n import N_

DEV, SYS = Path("/dev"), Path("/sys")
# (pattern under /dev, what it is): devices a sandbox may need, in this order.
DEVICE_PATTERNS = (("ttyUSB*", N_("USB serial")), ("ttyACM*", N_("USB serial (CDC ACM)")), ("ttyAMA*", N_("serial")),
                   ("kvm", N_("KVM (virtual machines)")), ("video*", N_("camera / video")), ("hidraw*", N_("HID")),
                   ("i2c-*", N_("I²C bus")), ("spidev*", N_("SPI")), ("gpiochip*", N_("GPIO")), ("bus/usb", N_("all USB devices")),
                   ("net/tun", N_("TUN/TAP")), ("fuse", N_("FUSE")))
# Where nvidia-ctk writes CDI specifications (as the launcher's CDI_DIRS).
CDI_DIRS = (Path("/etc/cdi"), Path("/var/run/cdi"), Path("/run/cdi"))


def memory_bytes(meminfo=Path("/proc/meminfo")):
    """The computer's memory in bytes (0 when it cannot be read)."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def cpu_count():
    """The CPUs this process may use (its affinity), else the computer's."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def human_bytes(value):
    """8589934592 -> "8 GiB" (whole GiB above 10, one decimal below)."""
    gib = value / (1 << 30)
    return f"{gib:.0f} GiB" if gib >= 10 else f"{gib:.1f} GiB"


def limits_hint(memory=None, cpus=None):
    """(memory text, cpus text): what the computer has, for the limit fields."""
    memory = memory_bytes() if memory is None else memory
    cpus = cpu_count() if cpus is None else cpus
    return (human_bytes(memory) if memory else "", str(cpus))


def _sysfs_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def device_label(path, sys_root=SYS):
    """A readable name for a device node from sysfs (the USB product and maker of a serial adapter, a camera's
    name), else ""."""
    name = Path(path).name
    for base in (sys_root / "class" / "tty" / name / "device", sys_root / "class" / "video4linux" / name / "device"):
        if not base.exists():
            continue
        title = _sysfs_text(sys_root / "class" / "video4linux" / name / "name")
        if title:
            return title
        current = base.resolve()
        for _ in range(4):  # up to the USB device that has product and manufacturer
            product = _sysfs_text(current / "product")
            if product:
                maker = _sysfs_text(current / "manufacturer")
                return f"{maker} {product}".strip()
            current = current.parent
    return ""


def devices(dev=DEV, sys_root=SYS):
    """[(path, what it is, label)] of the devices present that a sandbox could be given, in a stable order."""
    found = []
    for pattern, kind in DEVICE_PATTERNS:
        for path in sorted(glob.glob(str(dev / pattern))):
            if os.path.exists(path):
                found.append((path, kind, device_label(path, sys_root)))
    return found


def gpu_kinds(dev=DEV, cdi_dirs=CDI_DIRS):
    """The --gpu kinds this computer would pass the launcher's checks with: nvidia (a CDI specification naming
    NVIDIA GPUs, or /dev/nvidia0), dri (/dev/dri), rocm (/dev/dri and /dev/kfd)."""
    kinds = []
    nvidia = (dev / "nvidia0").exists()
    for directory in cdi_dirs:
        for path in sorted(glob.glob(str(directory / "*"))):
            if path.endswith((".yaml", ".yml", ".json")):
                try:
                    with open(path, "rb") as stream:
                        nvidia = nvidia or b"nvidia.com/gpu" in stream.read(1 << 20)
                except OSError:
                    continue
    if nvidia:
        kinds.append("nvidia")
    if (dev / "dri").is_dir():
        kinds.append("dri")
        if (dev / "kfd").exists():
            kinds.append("rocm")
    return kinds
