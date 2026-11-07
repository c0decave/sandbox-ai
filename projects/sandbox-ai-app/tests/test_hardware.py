"""What this computer has, for the forms: memory, CPUs, devices with their names, the GPU kinds."""
from pathlib import Path

import pytest

from sandbox_ai_app import hardware


def test_memory_and_cpus(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       32768000 kB\nMemFree: 1 kB\n")
    assert hardware.memory_bytes(meminfo) == 32768000 * 1024
    assert hardware.human_bytes(32768000 * 1024) == "31 GiB" and hardware.human_bytes(3 << 30) == "3.0 GiB"
    assert hardware.memory_bytes(tmp_path / "missing") >= 0  # falls back to sysconf, never raises
    assert hardware.cpu_count() >= 1
    assert hardware.limits_hint(8 << 30, 4) == ("8.0 GiB", "4")


def fake_tree(tmp_path):
    dev, sys_root = tmp_path / "dev", tmp_path / "sys"
    for name in ("ttyUSB0", "ttyACM1", "kvm", "video0", "null", "sda"):
        (dev / name).parent.mkdir(parents=True, exist_ok=True)
        (dev / name).write_text("")
    (dev / "bus" / "usb").mkdir(parents=True)
    usb = sys_root / "devices" / "usb1" / "1-1"
    (usb / "1-1:1.0" / "ttyUSB0").mkdir(parents=True)
    (usb / "product").write_text("CP2102 USB to UART Bridge\n")
    (usb / "manufacturer").write_text("Silicon Labs\n")
    (sys_root / "class" / "tty").mkdir(parents=True)
    (sys_root / "class" / "tty" / "ttyUSB0").mkdir()
    (sys_root / "class" / "tty" / "ttyUSB0" / "device").symlink_to(usb / "1-1:1.0" / "ttyUSB0")
    (sys_root / "class" / "video4linux" / "video0").mkdir(parents=True)
    (sys_root / "class" / "video4linux" / "video0" / "device").mkdir()
    (sys_root / "class" / "video4linux" / "video0" / "name").write_text("Integrated Camera\n")
    return dev, sys_root


def test_devices_are_listed_with_what_they_are_and_their_names(tmp_path):
    dev, sys_root = fake_tree(tmp_path)
    found = hardware.devices(dev, sys_root)
    assert [(Path(p).name, kind) for p, kind, _ in found] == [
        ("ttyUSB0", "USB serial"), ("ttyACM1", "USB serial (CDC ACM)"), ("kvm", "KVM (virtual machines)"),
        ("video0", "camera / video"), ("usb", "all USB devices")]  # no /dev/null or disks
    labels = {Path(p).name: label for p, _, label in found}
    assert labels["ttyUSB0"] == "Silicon Labs CP2102 USB to UART Bridge" and labels["video0"] == "Integrated Camera"
    assert labels["ttyACM1"] == ""


def test_gpu_kinds_are_only_what_the_launcher_would_accept(tmp_path):
    dev, cdi = tmp_path / "dev", tmp_path / "cdi"
    dev.mkdir()
    cdi.mkdir()
    assert hardware.gpu_kinds(dev, (cdi,)) == []
    (dev / "dri").mkdir()
    assert hardware.gpu_kinds(dev, (cdi,)) == ["dri"]
    (dev / "kfd").write_text("")
    assert hardware.gpu_kinds(dev, (cdi,)) == ["dri", "rocm"]
    (cdi / "nvidia.yaml").write_text("kind: nvidia.com/gpu\n")
    assert hardware.gpu_kinds(dev, (cdi,)) == ["nvidia", "dri", "rocm"]


def test_the_form_offers_only_what_is_there_and_keeps_what_a_project_says(qapp, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from sandbox_ai_app import backend, ui
    monkeypatch.setattr(hardware, "gpu_kinds", lambda: ["dri"])
    monkeypatch.setattr(hardware, "devices", lambda: [("/dev/ttyUSB0", "USB serial", "Silicon Labs CP2102")])
    monkeypatch.setattr(hardware, "limits_hint", lambda: ("31 GiB", "16"))
    form = ui.OptionsForm(backend.SandboxAI().options())
    gpu = form.widgets["--gpu"]
    assert [gpu.itemText(i) for i in range(gpu.count())] == ["", "dri"]
    form.set_values({"--gpu": "nvidia"})  # a project's choice this computer lacks: kept, not dropped
    assert form.values()["--gpu"] == "nvidia"
    device = form.widgets["--device"]
    assert isinstance(device, ui.DeviceField)
    device.pick.menu().actions()[0].trigger()
    device.pick.menu().actions()[0].trigger()  # once only
    assert form.values()["--device"] == ["/dev/ttyUSB0"]
    assert "Silicon Labs CP2102" in device.pick.menu().actions()[0].text()
    assert "31 GiB" in form.widgets["--memory"].placeholderText()
    assert "16 CPUs" in form.widgets["--cpus"].placeholderText()
