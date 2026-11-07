#!/usr/bin/env python3
"""sandbox-ai demo-device: an emulated serial device to practise hardware analysis on (the preset
hardware-analysis uses it by default).

  sandbox-ai demo-device start [--port 4000]     run it in the background on 127.0.0.1:PORT
  sandbox-ai demo-device status                  whether it runs, and where
  sandbox-ai demo-device stop                    end it
  sandbox-ai demo-device serve [--port 4000]     run it in the foreground (Ctrl-C ends it)

The device is the "SB-42 sensor node" of a made-up vendor: its serial console as a TCP service, the way ser2net
offers a real one (raw TCP, no RFC 2217). A connection sees what a UART would: the bootloader (U-Boot) with its
countdown, the kernel's messages, a login and a small shell (help, version, uname -a, printenv, ifconfig, ls,
cat, md ADDRESS [LENGTH] for its flash, reboot). Everything is fixed text and arithmetic: nothing on this
computer is read, run or changed. A sandbox reaches it with --allow-host-port PORT; pyserial speaks to it as
socket://127.0.0.1:PORT, nc and telnet work too. It listens on 127.0.0.1 only.

Standard library only.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib

DEFAULT_PORT = 4000
CONNECTIONS = 8
IDLE = 600  # seconds a silent connection is kept
FIRMWARE_VERSION = "2.3.7"
FLASH_SIZE = 1 << 20
BOOT = [
    "",
    "U-Boot 2016.03-sb42 (Mar 14 2023 - 09:12:44 +0000)",
    "",
    "CPU:   SB42 ARM926EJ-S rev 5 @ 400 MHz",
    "DRAM:  64 MiB",
    "Flash: 1 MiB SPI NOR (W25Q80)",
    "In:    serial",
    "Out:   serial",
    "Err:   serial",
    "Net:   sb42_eth",
    "Hit any key to stop autoboot:  2 \b\b\b 1 \b\b\b 0",
    "## Booting kernel from Legacy Image at 80008000 ...",
    "   Image Name:   Linux-4.14.98-sb42",
    "   Verifying Checksum ... OK",
    "Starting kernel ...",
    "",
    "[    0.000000] Booting Linux on physical CPU 0x0",
    "[    0.000000] Linux version 4.14.98-sb42 (build@sb42-ci) (gcc version 7.3.0) #1 PREEMPT",
    "[    0.000000] CPU: ARM926EJ-S [41069265] revision 5 (ARMv5TEJ)",
    "[    0.000000] Machine model: SandBox Industries SB-42 sensor node",
    "[    0.912345] spi-nor spi0.0: w25q80 (1024 Kbytes)",
    "[    0.934567] 3 fixed-partitions partitions found on MTD device spi0.0",
    "[    0.934601] 0x000000000000-0x000000040000 : \"u-boot\"",
    "[    0.934688] 0x000000040000-0x000000080000 : \"kernel\"",
    "[    0.934752] 0x000000080000-0x000000100000 : \"rootfs\"",
    "[    1.204511] sb42-sensor 0-0048: temperature sensor at 0x48",
    f"[    2.002301] sb42-fw: firmware {FIRMWARE_VERSION} (build 4711), config checksum ok",
    "",
    "SB-42 sensor node, firmware " + FIRMWARE_VERSION,
    "",
]
ENV = {"bootdelay": "2", "baudrate": "115200", "bootcmd": "sf probe; sf read 0x80008000 0x40000 0x40000; bootm",
       "bootargs": "console=ttyS0,115200 root=/dev/mtdblock2 rootfstype=squashfs",
       "ethaddr": "02:5b:42:00:13:37", "serial#": "SB42-000137", "hw_rev": "C"}
FILES = {"/etc/os-release": "NAME=\"SB42 Linux\"\nVERSION=\"" + FIRMWARE_VERSION + "\"\nID=sb42\n",
         "/etc/hostname": "sb42\n",
         "/etc/banner": "SandBox Industries - authorised service personnel only\n",
         "/proc/version": "Linux version 4.14.98-sb42 (build@sb42-ci) (gcc version 7.3.0) #1 PREEMPT\n",
         "/etc/sb42/sensor.conf": "interval=30\nunit=celsius\nupload=https://telemetry.sb42.invalid/v1/push\n"}


def flash():
    """The device's 1 MiB SPI flash: a U-Boot area, a firmware header at 0x40000 (magic, version, size, CRC32),
    a squashfs magic at 0x80000; erased (0xff) elsewhere. Deterministic."""
    image = bytearray(b"\xff" * FLASH_SIZE)
    image[0:16] = b"\x13\x00\x00\xea" + b"U-Boot 2016.03\x00\x00"[:12]
    body = b"SB42-KERNEL-IMAGE" + bytes(range(64))
    header = b"SB42FW\x00\x01" + struct.pack(">4sII", FIRMWARE_VERSION.encode()[:4].ljust(4, b"\x00"), len(body),
                                              zlib.crc32(body))
    image[0x40000:0x40000 + len(header)] = header
    image[0x40000 + 32:0x40000 + 32 + len(body)] = body
    image[0x80000:0x80004] = b"hsqs"
    return bytes(image)


FLASH = flash()


def hexdump(address, length):
    lines = []
    for offset in range(address, min(address + length, FLASH_SIZE), 16):
        chunk = FLASH[offset:offset + 16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}: " + " ".join(f"{b:02x}" for b in chunk) + f"    {text}")
    return lines


def answer(line):
    """The shell's output for LINE (a list of lines), or None to reboot."""
    words = line.split()
    if not words:
        return []
    command, args = words[0], words[1:]
    if command == "help":
        return ["commands: help version uname printenv ifconfig ls cat md reboot exit"]
    if command == "version":
        return [f"SB-42 firmware {FIRMWARE_VERSION} build 4711, hardware rev {ENV['hw_rev']}, U-Boot 2016.03-sb42"]
    if command == "uname":
        return ["Linux sb42 4.14.98-sb42 #1 PREEMPT armv5tejl GNU/Linux" if "-a" in args else "Linux"]
    if command == "printenv":
        return [f"{key}={value}" for key, value in ENV.items()]
    if command == "ifconfig":
        return ["eth0      Link encap:Ethernet  HWaddr " + ENV["ethaddr"].upper(),
                "          inet addr:192.168.42.17  Bcast:192.168.42.255  Mask:255.255.255.0"]
    if command == "ls":
        target = args[0] if args else "/"
        names = sorted({path[len(target.rstrip("/")) + 1:].split("/")[0] for path in FILES
                        if path.startswith(target.rstrip("/") + "/")})
        return ["  ".join(names)] if names else [f"ls: {target}: No such file or directory"]
    if command == "cat":
        if not args:
            return ["cat: missing operand"]
        return FILES[args[0]].rstrip("\n").split("\n") if args[0] in FILES else \
            [f"cat: can't open '{args[0]}': No such file or directory"]
    if command == "md":
        try:
            address = int(args[0], 16) if args else 0
            length = min(int(args[1], 16) if len(args) > 1 else 0x40, 0x400)
        except ValueError:
            return ["usage: md ADDRESS [LENGTH] (hexadecimal; at most 0x400)"]
        if not 0 <= address < FLASH_SIZE:
            return [f"md: address out of flash (0-{FLASH_SIZE - 1:x})"]
        return hexdump(address, length)
    if command == "reboot":
        return None
    return [f"-sh: {command}: not found"]


def session(connection, idle=IDLE, pace=0.0):
    """One connection: the boot, a login (any name), the shell; until the peer goes, `exit` or IDLE seconds."""
    connection.settimeout(idle)

    def send(lines):
        for text in lines:
            connection.sendall(text.encode() + b"\r\n")
            if pace:
                time.sleep(pace)
    buffer = b""

    def read_line():
        nonlocal buffer
        while b"\n" not in buffer and b"\r" not in buffer:
            data = connection.recv(1024)
            if not data:
                raise ConnectionError("closed")
            buffer += data
            if len(buffer) > 4096:
                buffer = b""
                raise ConnectionError("line too long")
        line, _, buffer = buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n").partition(b"\n")
        return line.decode("utf-8", "replace").strip()
    with contextlib.suppress(OSError, ConnectionError):
        while True:
            send(BOOT)
            connection.sendall(b"sb42 login: ")
            user = read_line() or "root"
            send([f"Welcome {user[:32]}. BusyBox v1.24.1 built-in shell (ash)"])
            while True:
                connection.sendall(b"# ")
                line = read_line()
                if line == "exit":
                    return
                output = answer(line)
                if output is None:
                    send(["", "Restarting system."])
                    break
                send(output)


def serve(port=DEFAULT_PORT, ready=None, stop=None, pace=0.0):
    """Listen on 127.0.0.1:PORT until STOP is set (or forever); READY(port) once listening."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(8)
    listener.settimeout(0.5)
    slots = threading.BoundedSemaphore(CONNECTIONS)
    if ready:
        ready(listener.getsockname()[1])

    def handle(connection):
        try:
            with connection:
                session(connection, pace=pace)
        finally:
            slots.release()
    try:
        while stop is None or not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            if not slots.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(target=handle, args=(connection,), daemon=True).start()
    finally:
        listener.close()
    return 0


def state_file():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "demo-device.json"


def running():
    """{"pid", "port"} of the demo device this user runs, or None."""
    try:
        data = json.loads(state_file().read_text())
        pid, port = int(data["pid"]), int(data["port"])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"pid": pid, "port": port} if b"demo_device.py" in cmdline and b"serve" in cmdline else None


def start(port=DEFAULT_PORT, wait=5.0):
    current = running()
    if current:
        if current["port"] != port:
            raise ValueError(f"the demo device runs already on port {current['port']}; stop it first")
        return current
    # The child says itself that it listens (a pipe): a port that answers may be another service's, which the
    # preset would then hand to the agent.
    ready_r, ready_w = os.pipe()
    try:
        process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "serve", "--port", str(port),
                                    "--ready-fd", str(ready_w)], pass_fds=(ready_w,), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    finally:
        os.close(ready_w)
    try:
        import select
        readable, _, _ = select.select([ready_r], [], [], wait)
        said = os.read(ready_r, 16) if readable else b""
    finally:
        os.close(ready_r)
    if said.strip() == str(port).encode():
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text(json.dumps({"pid": process.pid, "port": port}))
        return {"pid": process.pid, "port": port}
    if process.poll() is None:
        process.terminate()
    with contextlib.suppress(OSError, ValueError):
        process.wait(5)
    error = process.stderr.read().decode("utf-8", "replace").strip().splitlines() if process.stderr else []
    reason = error[-1] if error else f"it did not start within {wait:g} s"
    raise ValueError(f"the demo device did not start on 127.0.0.1:{port}: {reason}")


def stop():
    current = running()
    if current:
        with contextlib.suppress(ProcessLookupError):
            os.kill(current["pid"], signal.SIGTERM)
    with contextlib.suppress(OSError):
        state_file().unlink()
    return current


def main(argv, out=print):
    p = argparse.ArgumentParser(prog="sandbox-ai demo-device", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("start", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--port", type=int, default=DEFAULT_PORT)
        if name == "serve":
            command.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    commands.add_parser("status")
    commands.add_parser("stop")
    args = p.parse_args(argv)
    try:
        if args.command in ("start", "serve") and not 1024 <= args.port <= 65535:
            raise ValueError("--port is 1024 to 65535")
        if args.command == "serve":
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

            def ready(port):
                if args.ready_fd is not None:
                    os.write(args.ready_fd, str(port).encode())
                    os.close(args.ready_fd)
            return serve(args.port, ready=ready)
        if args.command == "start":
            current = start(args.port)
            out(f"demo device SB-42 on 127.0.0.1:{current['port']} (PID {current['pid']}); in a sandbox started with "
                f"--allow-host-port {current['port']}: socket://127.0.0.1:{current['port']} "
                f"(or: sandbox-ai claude --preset hardware-analysis)")
            return 0
        if args.command == "status":
            current = running()
            out(f"running on 127.0.0.1:{current['port']} (PID {current['pid']})" if current else "not running")
            return 0 if current else 1
        current = stop()
        out("stopped" if current else "was not running")
        return 0
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai demo-device: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
