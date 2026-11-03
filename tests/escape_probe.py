"""Escape attempts run *inside* a sandbox as the agent; prints one JSON report.

Every check is an attack that must fail (ok=True means contained). The host
side (tests/test_escape_live.py) plants canaries the probe is told about only
by name, so finding any of them means the boundary leaked. Standard library
only: it runs with the image's or host's python3 -I.
"""
import ctypes
import errno
import json
import os
import shutil
import socket
import subprocess
import sys

# Read from a file: the canary must not appear in any command line in here.
cfg = json.loads(open(sys.argv[1]).read())
canary = cfg["canary"]
report = {}


def check(name, ok, detail=""):
    report[name] = {"ok": bool(ok), "detail": str(detail)[:300]}


def attempt(name, action):
    """action() succeeding is an escape; any OSError means it was refused."""
    try:
        result = action()
    except OSError as exc:
        check(name, True, f"refused: {errno.errorcode.get(exc.errno, 'timeout' if isinstance(exc, TimeoutError) else exc)}")
    else:
        check(name, False, f"succeeded: {result!r}")


def connects(family, address, timeout=2):
    """ADDRESS if something there exchanges data. The connection proxy accepts every connection and
    resets a forbidden one at once; a reset or a close counts as refused, an answer or a connection
    held open counts as reachable (an escape, to be safe)."""
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(address)
        try:
            sock.sendall(b"\x16\x03\x01\x00\x01\x00")
            if not sock.recv(1):
                raise ConnectionRefusedError(111, "closed right away (connection proxy)")
        except socket.timeout:
            pass
        return address


def status():
    return dict(line.split(":\t", 1) for line in open("/proc/self/status").read().splitlines() if ":\t" in line)


# --- Privileges: no capabilities, no way to gain any -------------------------
fields = status()
check("no_effective_capabilities", int(fields["CapEff"], 16) == 0, fields["CapEff"])
check("no_bounding_capabilities", int(fields["CapBnd"], 16) == 0, fields["CapBnd"])
check("no_new_privs", fields["NoNewPrivs"].strip() == "1", fields["NoNewPrivs"])
check("seccomp_filter", fields["Seccomp"].strip() == "2", fields["Seccomp"])
uid_map = open("/proc/self/uid_map").read().split()
# Root inside must not be root outside: either an unprivileged single mapping
# or keep-id style, never "0 0 4294967295" (the initial namespace).
check("not_initial_user_namespace", uid_map[:3] != ["0", "0", "4294967295"], " ".join(uid_map))

libc = ctypes.CDLL(None, use_errno=True)


def syscall_fails(name, function, *args):
    result = function(*args)
    error = ctypes.get_errno()
    check(name, result != 0, f"result {result}, errno {errno.errorcode.get(error, error)}")


MS_REMOUNT, MS_BIND = 32, 4096
syscall_fails("remount_root_rw", libc.mount, None, b"/", None, MS_REMOUNT | MS_BIND, None)
syscall_fails("mount_tmpfs", libc.mount, b"tmpfs", b"/tmp", b"tmpfs", 0, None)
syscall_fails("mount_proc_again", libc.mount, b"proc", b"/tmp", b"proc", 0, None)
# New namespaces would not reach the host either, but must not be available
# to rebuild a network or mount view the firewall does not cover.
CLONE_NEWNS, CLONE_NEWNET = 0x00020000, 0x40000000
pid = os.fork()
if pid == 0:
    os._exit(0 if libc.unshare(CLONE_NEWNET) != 0 or libc.unshare(CLONE_NEWNS) != 0 else 1)
check("unshare_net_and_mount_refused", os.waitpid(pid, 0)[1] == 0, "without user namespace")
if subprocess.run(["sh", "-c", "command -v nft"], capture_output=True).returncode == 0:
    tamper = subprocess.run(["nft", "flush", "ruleset"], capture_output=True, text=True)
    check("nft_flush_refused", tamper.returncode != 0, tamper.stderr.strip())
for path in ["/usr/bin/sudo", "/usr/bin/su", "/bin/su"]:
    if os.path.exists(path):
        escalate = subprocess.run([path, "-c", "id -u"] if path.endswith("su") else [path, "-n", "id", "-u"],
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10)
        # Already uid 0 inside; what matters is that no capability came along.
        check(f"setuid_{os.path.basename(path)}_gains_nothing", "CapEff" not in escalate.stdout, escalate.returncode)

# --- Filesystem: nothing of the host beyond what was mounted ------------------
for path in ["/sandbox/runtime.py", "/sandbox/session/config.json", "/etc/hosts", "/etc/resolv.conf"]:
    if os.path.exists(path):
        attempt(f"write_{path.strip('/').replace('/', '_')}", lambda p=path: open(p, "a").close())
attempt("write_proc_sys", lambda: open("/proc/sys/kernel/hostname", "w").write("escaped"))
attempt("write_sys", lambda: open("/sys/kernel/uevent_helper", "w").write("/tmp/x"))
attempt("mknod_block_device", lambda: os.mknod("/tmp/disk", 0o600 | 0o060000, os.makedev(8, 0)))
for device in ["/dev/mem", "/dev/kmem", "/dev/kvm", "/dev/sda", "/dev/nvme0n1", "/dev/vda", "/dev/fuse"]:
    check(f"no_{device[5:]}", not os.path.exists(device))
for path in cfg["absent_paths"]:
    check(f"absent:{path}", not os.path.lexists(path))
check("workspace_canary_visible", open(cfg["workspace_canary"]).read() == canary, "positive control")
skip = {"/proc", "/sys", "/dev", "/usr", "/lib", "/lib64", "/lib32", "/bin", "/sbin"}
found, scanned = [], 0
for root, dirs, files in os.walk("/", onerror=lambda error: None):
    dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
    scanned += len(files)
    found += [os.path.join(root, f) for f in files + dirs if canary in f]
found = [path for path in found if not path.startswith(os.path.dirname(cfg["workspace_canary"]))]
check("no_host_canary_file", not found, f"{found or 'none'} in {scanned} files")
roots = [f"/proc/{p}/root" for p in os.listdir("/proc") if p.isdigit()]
check("no_canary_via_proc_root", not any(os.path.exists(r + cfg["host_canary_path"]) for r in roots),
      f"{len(roots)} process roots")

# --- Processes and environment of the host -----------------------------------
leaked = []
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    for part in ("cmdline", "environ"):
        try:
            if canary.encode() in open(f"/proc/{entry}/{part}", "rb").read():
                leaked.append(f"{entry}/{part}")
        except OSError:
            pass
check("no_host_process_or_environment", not leaked, leaked or f"{len(os.listdir('/proc'))} /proc entries")
check("no_canary_in_own_environment", not any(canary in v for v in os.environ.values()))
for name in ["SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DOCKER_HOST", "CONTAINER_HOST"]:
    check(f"no_env_{name}", name not in os.environ or name == "XDG_RUNTIME_DIR" and cfg.get("wayland"))
for path in ["/run/podman/podman.sock", "/var/run/docker.sock", "/run/docker.sock", "/run/dbus/system_bus_socket"]:
    check(f"no_socket_{os.path.basename(path)}", not os.path.exists(path))

# --- Network: only what the firewall allows ------------------------------------
for address, port in cfg["host_tcp"]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    attempt(f"tcp_host:{address}:{port}", lambda f=family, a=address, p=port: connects(f, (a, p)))
for address, port in cfg.get("allowed_tcp", []):
    try:
        connects(socket.AF_INET, (address, port))
    except OSError as exc:
        check(f"allowed_tcp:{address}:{port}", False, f"positive control failed: {exc}")
    else:
        check(f"allowed_tcp:{address}:{port}", True, "positive control")
for address in [("1.1.1.1", 443), ("1.1.1.1", 80), ("8.8.8.8", 53)]:
    attempt(f"tcp_internet:{address[0]}:{address[1]}", lambda a=address: connects(socket.AF_INET, a))
attempt("tcp6_internet", lambda: connects(socket.AF_INET6, ("2606:4700:4700::1111", 443)))


def udp_dns():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
        sock.sendto(query, ("8.8.8.8", 53))
        return sock.recvfrom(512)[0][:2]


attempt("udp_dns_8.8.8.8", udp_dns)
attempt("raw_icmp_socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP).close())
attempt("packet_socket", lambda: socket.socket(socket.AF_PACKET, socket.SOCK_RAW).close())
if shutil.which("ip"):
    route = subprocess.run(["ip", "route", "add", "blackhole", "192.0.2.0/24"], capture_output=True, text=True)
    check("netlink_route_change_refused", route.returncode != 0, route.stderr.strip())


def abstract_socket():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect("\0" + cfg["abstract_socket"])
        return "connected"


attempt("host_abstract_unix_socket", abstract_socket)

print(json.dumps(report))
