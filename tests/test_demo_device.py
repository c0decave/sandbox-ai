"""sandbox-ai demo-device: the emulated SB-42 over real TCP, its CLI with a real background process."""
import importlib.util
from pathlib import Path
import socket
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_demo_device", ROOT / "sandbox/demo_device.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


@pytest.fixture
def device():
    stop, port = threading.Event(), []
    thread = threading.Thread(target=d.serve, args=(0, port.append, stop), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not port and time.monotonic() < deadline:
        time.sleep(0.01)
    yield port[0]
    stop.set()
    thread.join(5)


class Console:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.seen = b""

    def until(self, marker):
        while marker.encode() not in self.seen:
            data = self.sock.recv(4096)
            if not data:
                raise EOFError(self.seen.decode(errors="replace"))
            self.seen += data
        text, _, self.seen = self.seen.partition(marker.encode())
        return text.decode(errors="replace")

    def run(self, line):
        self.sock.sendall(line.encode() + b"\r\n")
        return self.until("# ")


def test_the_console_boots_logs_in_and_answers(device):
    console = Console(device)
    boot = console.until("sb42 login: ")
    assert "U-Boot 2016.03-sb42" in boot and "Machine model: SandBox Industries SB-42 sensor node" in boot
    assert f"firmware {d.FIRMWARE_VERSION}" in boot and '0x000000040000-0x000000080000 : "kernel"' in boot
    console.sock.sendall(b"root\n")
    assert "BusyBox" in console.until("# ")
    assert f"SB-42 firmware {d.FIRMWARE_VERSION} build 4711, hardware rev C" in console.run("version")
    assert "bootargs=console=ttyS0,115200" in console.run("printenv")
    assert "armv5tejl" in console.run("uname -a")
    assert 'VERSION="2.3.7"' in console.run("cat /etc/os-release")
    assert "No such file" in console.run("cat /etc/shadow")
    assert "sb42" in console.run("ls /etc")
    header = console.run("md 40000 20")
    first = next(line for line in header.splitlines() if line.startswith("00040000:"))
    assert first.startswith("00040000: 53 42 34 32 46 57 00 01") and "SB42FW" in first
    assert "hsqs" in console.run("md 80000 10")
    assert "usage: md" in console.run("md zz") and "out of flash" in console.run("md 200000")
    assert "not found" in console.run("rm -rf /")  # nothing is ever run
    # reboot replays the boot; exit ends the connection.
    console.sock.sendall(b"reboot\n")
    assert "Restarting system." in console.until("sb42 login: ")
    console.sock.sendall(b"x\nexit\n")
    console.until("# ")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if console.sock.recv(4096) == b"":
            break
    console.sock.close()


def test_the_flash_is_deterministic_and_consistent():
    import struct
    import zlib
    assert d.flash() == d.FLASH and len(d.FLASH) == d.FLASH_SIZE
    magic, version, size, crc = struct.unpack(">8s4sII", d.FLASH[0x40000:0x40000 + 20])
    assert magic == b"SB42FW\x00\x01" and version == b"2.3."
    assert zlib.crc32(d.FLASH[0x40000 + 32:0x40000 + 32 + size]) == crc  # the header's CRC is right


def test_several_consoles_at_once_and_a_peer_that_goes(device):
    first, second = Console(device), Console(device)
    first.until("login: ")
    second.until("login: ")
    first.sock.close()  # gone mid-session: the device goes on
    second.sock.sendall(b"root\n")
    second.until("# ")
    assert "commands:" in second.run("help")
    second.sock.close()


def test_the_cli_starts_reports_and_stops_a_background_device(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    if port < 1024:
        pytest.skip("no unprivileged port")
    try:
        assert d.main(["start", "--port", str(port)]) == 0
        assert f"127.0.0.1:{port}" in capsys.readouterr().out
        assert d.main(["start", "--port", str(port)]) == 0  # once: the running one is kept
        console = Console(port)
        assert "U-Boot" in console.until("login: ")
        console.sock.close()
        assert d.main(["status"]) == 0 and f"running on 127.0.0.1:{port}" in capsys.readouterr().out
        assert d.main(["start", "--port", str(port + 1 if port < 65535 else port - 1)]) == 1
        assert "runs already" in capsys.readouterr().err
    finally:
        assert d.main(["stop"]) == 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.05)
        except OSError:
            break
    else:
        pytest.fail("the device still answers after stop")
    assert d.main(["status"]) == 1 and "not running" in capsys.readouterr().out
    assert d.main(["serve", "--port", "80"]) == 1  # privileged ports are refused


def test_start_refuses_a_port_another_service_holds(tmp_path, monkeypatch, capsys):
    """Something else listens on the port: start says so instead of offering that service as the device."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen()
    port = other.getsockname()[1]
    if port < 1024:
        pytest.skip("no unprivileged port")
    try:
        assert d.main(["start", "--port", str(port)]) == 1
        assert "did not start on 127.0.0.1" in capsys.readouterr().err
        assert d.running() is None
    finally:
        other.close()
