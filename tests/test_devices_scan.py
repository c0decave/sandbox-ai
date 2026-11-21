"""sandbox-ai devices scan: the devices of this machine a sandbox could be given, for the Board's device picker and the
running sandbox's "Add device" (user 2026-11-19: "ob beim Hinzufügen von Geräten lokale oder Remote-Geräte erkannt
werden und im Dropdown angezeigt werden"). Read from a fake sysfs and /dev (devices.SYS, the launcher's DEV): USB
devices by identity with their product names, serial ports, cameras, render nodes, /dev/kvm, the GPU kinds of --gpu,
sound; what the launcher refuses (keyboards, input) listed with why, never offered."""
import json
import os

import pytest

from test_devices import FakeUsb, L, d


@pytest.fixture
def hw(tmp_path, monkeypatch):
    usb = FakeUsb(tmp_path, monkeypatch)
    monkeypatch.setattr(L, "nvidia_cdi_available", lambda: False)
    return usb


def ftdi(usb, port="1-3", devnum=6, serial="A50285BI", node="ttyUSB0", numbers="188:0"):
    usb.plug(port, usb="0403:6001", serial=serial, devnum=devnum, cls="00", product="FT232R USB UART",
             interfaces=(("ff", "tty", node, numbers),))
    (usb.sys / "devices/pci0000:00/usb1" / port / "manufacturer").write_text("FTDI\n")


def machine(usb):
    """A Pico, an FTDI adapter, a webcam, a keyboard receiver, a hub; /dev/kvm, an Intel render node, /dev/kfd,
    NVIDIA nodes, a sound card."""
    usb.plug()
    ftdi(usb)
    usb.plug("1-4", usb="046d:0825", serial=None, devnum=7, cls="ef", product="Webcam C270",
             interfaces=(("0e", "video4linux", "video0", "81:0"),))
    (usb.sys / "devices/pci0000:00/usb1/1-4/1-4:1.0/video4linux/video0/name").write_text("UVC Camera (046d:0825)\n")
    usb.plug("1-5", usb="046d:c52b", serial=None, devnum=11, cls="00", product="USB Receiver",
             interfaces=(("03", "hidraw", "hidraw0", "240:0"), ("03", "input", "input/event5", "13:69")))
    usb.plug("1-1", usb="05e3:0610", serial=None, devnum=2, cls="09", product="USB2.0 Hub",
             interfaces=(("09", None, None, None),))
    usb.sysfs_node(usb.sys / "devices/virtual/misc/kvm", "misc", "kvm", "10:232")
    pci = usb.sys / "devices/pci0000:00/0000:00:02.0"
    pci.mkdir(parents=True)
    (pci / "vendor").write_text("0x8086\n")
    (usb.sys / "bus/pci/drivers/i915").mkdir(parents=True)
    (pci / "driver").symlink_to(usb.sys / "bus/pci/drivers/i915")
    usb.sysfs_node(pci / "drm/renderD128", "drm", "dri/renderD128", "226:128")
    usb.node("kfd", "235:0")
    usb.node("nvidia0", "195:0")
    usb.node("nvidiactl", "195:255")
    usb.node("snd/controlC0", "116:2")
    card = usb.sys / "class/sound/card0"
    card.mkdir(parents=True)
    (card / "id").write_text("PCH\n")


def by_value(found):
    return {entry["value"]: entry for entry in found["devices"]}


def test_scan_finds_each_kind_with_readable_labels(hw):
    machine(hw)
    found = d.scan(launcher=L)
    entries = by_value(found)
    # USB devices by identity, with the product's name and their nodes as /dev names; hubs are not offered.
    pico, adapter = entries["usb:2e8a:000a"], entries["usb:0403:6001"]
    assert pico["kind"] == "usb" and pico["product"] == "Pico" and pico["refused"] is None
    assert pico["label"] == "Pico (usb:2e8a:000a) - /dev/ttyACM0"
    assert adapter["label"] == "FTDI FT232R USB UART (usb:0403:6001) - /dev/ttyUSB0"
    assert adapter["nodes"] == ["/dev/bus/usb/001/006", "/dev/ttyUSB0"]
    assert not any(e.get("usb") == "05e3:0610" for e in found["devices"]), "a hub is no device to hand over"
    # Serial ports: the node, what it belongs to, udev's links.
    acm = entries["/dev/ttyACM0"]
    assert acm["kind"] == "serial" and acm["label"] == "Pico serial port (usb:2e8a:000a) - /dev/ttyACM0"
    assert acm["links"] == ["/dev/serial/by-id/usb-Pico_E6609CB2-if00"] and acm["nodes"] == ["/dev/ttyACM0"]
    assert entries["/dev/ttyUSB0"]["label"] == "FTDI FT232R USB UART serial port (usb:0403:6001) - /dev/ttyUSB0"
    # A camera, with the launcher's warning; /dev/kvm; a render node with its maker and driver.
    camera = entries["/dev/video0"]
    assert camera["kind"] == "video" and camera["label"] == "UVC Camera (046d:0825) (usb:046d:0825) - /dev/video0"
    assert "camera" in camera["note"]
    assert entries["/dev/kvm"]["kind"] == "kvm" and entries["/dev/kvm"]["refused"] is None
    render = entries["/dev/dri/renderD128"]
    assert render["kind"] == "render" and render["label"] == "Intel graphics (i915) render node - /dev/dri/renderD128"
    # The GPU kinds of --gpu that this machine has (rocm: /dev/dri and /dev/kfd), nvidia with what Podman needs.
    gpus = {e["value"]: e for e in found["devices"] if e["kind"] == "gpu"}
    assert sorted(gpus) == ["dri", "nvidia", "rocm"] and all(e["option"] == "--gpu" for e in gpus.values())
    assert gpus["nvidia"]["nodes"] == ["/dev/nvidia0", "/dev/nvidiactl"]
    assert "nvidia-ctk cdi generate" in gpus["nvidia"]["note"]
    assert gpus["rocm"]["nodes"] == ["/dev/dri", "/dev/kfd"]
    # Sound as the launcher passes it: the whole /dev/snd, its cards named.
    sound = entries["/dev/snd"]
    assert sound["kind"] == "sound" and "PCH" in sound["label"] and "microphones" in sound["note"]
    # Raw USB as a whole, with its warning.
    assert "security keys" in entries["/dev/bus/usb"]["note"]


def test_what_the_launcher_refuses_is_listed_with_why(hw):
    machine(hw)
    entries = by_value(d.scan(launcher=L))
    receiver = entries["usb:046d:c52b"]
    assert receiver["refused"] and "no device node of it can be passed" in receiver["refused"]
    event = entries["/dev/input/event5"]
    assert event["kind"] == "input" and "keyboards" in event["refused"]
    assert event["label"] == "USB Receiver (usb:046d:c52b) - /dev/input/event5"
    # Nothing every sandbox has anyway (null, zero, ptmx ...) is listed.
    assert not any(value in entries for value in ("/dev/null", "/dev/zero", "/dev/ptmx"))


def test_two_of_a_kind_are_named_by_their_serial(hw):
    ftdi(hw)
    ftdi(hw, port="1-6", devnum=8, serial="B7", node="ttyUSB1", numbers="188:1")
    values = [e["value"] for e in d.scan(launcher=L)["devices"] if e["kind"] == "usb"]
    assert "usb:0403:6001:A50285BI" in values and "usb:0403:6001:B7" in values
    # Without a serial to tell them apart: listed, refused with what to do instead.
    ftdi(hw, port="1-7", devnum=9, serial=None, node="ttyUSB2", numbers="188:2")
    ftdi(hw, port="1-8", devnum=10, serial=None, node="ttyUSB3", numbers="188:3")
    unnamed = [e for e in d.scan(launcher=L)["devices"] if e["value"] == "usb:0403:6001"]
    assert unnamed and all("by its path" in e["refused"] for e in unnamed)


def test_an_empty_machine_lists_nothing(hw):
    found = d.scan(launcher=L)
    assert found["devices"] == [] and found["notes"] == []


def test_a_machine_without_sysfs_lists_by_dev_alone(hw, monkeypatch, tmp_path):
    """macOS, or /sys not mounted: nothing that sysfs would tell, nothing fails."""
    monkeypatch.setattr(d, "SYS", tmp_path / "no-sys")
    hw.node("kvm", "10:232")
    entries = by_value(d.scan(launcher=L))
    assert list(entries) == ["/dev/kvm"] and entries["/dev/kvm"]["label"] == "KVM (virtual machines) - /dev/kvm"


def test_devices_scan_on_the_command_line(hw, capsys, monkeypatch):
    machine(hw)
    assert d.main(["scan", "--json"], launcher=L) == 0
    found = json.loads(capsys.readouterr().out)
    assert "usb:2e8a:000a" in by_value(found)
    assert d.main(["scan"], launcher=L) == 0
    text = capsys.readouterr().out
    assert "USB devices" in text and "Pico (usb:2e8a:000a) - /dev/ttyACM0" in text
    assert "refused:" in text and "GPU (--gpu)" in text


def test_scan_is_read_only(hw, monkeypatch):
    """No root, nothing written: a scan opens nothing for writing and starts no process."""
    machine(hw)
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda path, flags, *a, **k: opened.append(flags) or real_open(path, flags, *a, **k))
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: pytest.fail("a scan starts no process"))
    d.scan(launcher=L)
    assert opened and not any(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) for flags in opened)
