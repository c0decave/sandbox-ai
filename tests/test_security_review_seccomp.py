"""Security review B (SAI-03, -13, -16, -20, and the filter part of -09/-10): sandbox-ai's own system call filters.

Firejail's --seccomp is a list of calls its release refuses; what it does not name is allowed - io_uring, new
namespaces, every socket family (AF_VSOCK, which no firewall rule sees) - and the container around it has no filter.
runtime.py makes two filters of its own from the calls' names: "container" (the supervisor, and so everything of the
run) and "sandbox" (the agent and the connection proxy).

Three kinds of test:
- the programs are run here by a small interpreter of classic BPF, for every architecture a sandbox can make calls
  of (x86-64 and its 32-bit calls, arm64 and its 32-bit calls): what each call number and argument gets;
- the programs are installed for real, in a child process of the test, and the kernel is asked (x86-64 and arm64);
- the tables are compared with libseccomp's and with the filter the installed Firejail makes, where they are there.

Nothing here leaves the child process: a refused call fails, an allowed one makes a socket or a process that ends
with the child.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

import test_feature_matrix as fm

s = fm.s
rt = s.RUNTIME
RUNTIME_FILE = Path(rt.__file__)
ALLOW = rt.SECCOMP_ALLOW
MACHINE = os.uname().machine
FSECCOMP = next((path for path in (Path("/usr/lib/x86_64-linux-gnu/firejail/fseccomp"),
                                   Path("/usr/lib/firejail/fseccomp"), Path("/usr/lib64/firejail/fseccomp"))
                 if path.is_file()), None)


def refused(error):
    return rt.SECCOMP_ERRNO | error


def judge(program, arch, number, *args):
    """What the classic BPF PROGRAM returns for the call NUMBER of the architecture ARCH (an AUDIT_ARCH value) with
    ARGS: the kernel's seccomp_data, read as the kernel reads it."""
    data = struct.pack("=iIQ6Q", number if number < 0x80000000 else number - (1 << 32), arch, 0,
                       *(list(args) + [0] * 6)[:6])
    accumulator, index, steps = 0, 0, 0
    while True:
        steps += 1
        assert steps < 10000, "a loop"
        code, yes, no, value = struct.unpack_from("=HBBI", program, index * 8)
        index += 1
        if code == rt.BPF_LOAD:
            accumulator = struct.unpack_from("=I", data, value)[0]
        elif code == rt.BPF_JUMP:
            index += value
        elif code == rt.BPF_RETURN:
            return value
        else:
            hit = {rt.BPF_EQUAL: accumulator == value, rt.BPF_ABOVE: accumulator > value,
                   rt.BPF_FROM: accumulator >= value, rt.BPF_BITS: bool(accumulator & value)}[code]
            index += yes if hit else no


def program(profile="sandbox", nested=False, machine="x86_64"):
    return rt.seccomp_program(rt.seccomp_policy(profile, nested=nested), machine)


def number(arch, name):
    return {**rt.SYSCALL_NUMBERS[arch], **rt.SYSCALL_COMMON}[name]


ARCHS = [("x86_64", "x86_64"), ("x86_64", "i386"), ("aarch64", "aarch64"), ("aarch64", "arm")]
# Calls every program makes, by number: they pass every filter. (read, write, openat, execve, exit_group, futex,
# mmap, seccomp, memfd_create, pidfd_open, openat2, the three of Landlock, which `runtime.py --untrusted` needs)
EVERYDAY = {"x86_64": [0, 1, 257, 59, 231, 202, 9, 317, 319, 434, 437, 444, 445, 446],
            "i386": [3, 4, 295, 11, 252, 240, 192, 354, 356, 434, 437, 444, 445, 446],
            "aarch64": [63, 64, 56, 221, 94, 98, 222, 277, 279, 434, 437, 444, 445, 446],
            "arm": [3, 4, 322, 11, 248, 240, 192, 383, 385, 434, 437, 444, 445, 446]}


# --- the programs, run here ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("machine,arch", ARCHS)
@pytest.mark.parametrize("profile,nested", [("container", False), ("sandbox", False), ("sandbox", True)])
def test_everyday_calls_pass_and_every_named_call_gets_its_answer(machine, arch, profile, nested):
    policy = rt.seccomp_policy(profile, nested=nested)
    built = rt.seccomp_program(policy, machine)
    assert len(built) % 8 == 0 and 0 < len(built) // 8 <= 4096
    for nr in EVERYDAY[arch]:
        assert judge(built, rt.SECCOMP_ARCH[arch], nr) == ALLOW, (arch, nr)
    known = {**rt.SYSCALL_NUMBERS[arch], **rt.SYSCALL_COMMON}
    for name, error in policy["refuse"].items():
        if name in known:
            assert judge(built, rt.SECCOMP_ARCH[arch], known[name]) == refused(getattr(errno, error)), (arch, name)


@pytest.mark.parametrize("machine,arch", ARCHS)
def test_io_uring_is_refused_in_every_filter(machine, arch):
    """SAI-13: the three calls, in the container's filter and the sandbox's, nested or not."""
    for profile, nested in (("container", False), ("sandbox", False), ("sandbox", True)):
        built = program(profile, nested, machine)
        for name in ("io_uring_setup", "io_uring_enter", "io_uring_register"):
            assert judge(built, rt.SECCOMP_ARCH[arch], number(arch, name)) == refused(errno.EPERM)


@pytest.mark.parametrize("machine,arch", ARCHS)
def test_no_new_namespace_in_the_ordinary_sandbox(machine, arch):
    """SAI-03: unshare, setns, clone with a namespace flag; clone3 answers "no such call" (its flags lie behind a
    pointer). A thread, a fork and an unshare of the file table are no namespace."""
    built, code = program("sandbox", False, machine), rt.SECCOMP_ARCH[arch]
    for flag in (0x10000000, 0x00020000, 0x20000000, 0x40000000, 0x08000000, 0x04000000, 0x02000000):
        assert judge(built, code, number(arch, "clone"), flag | 17) == refused(errno.EPERM), hex(flag)
        assert judge(built, code, number(arch, "unshare"), flag) == refused(errno.EPERM), hex(flag)
    assert judge(built, code, number(arch, "unshare"), 0x80) == refused(errno.EPERM)  # CLONE_NEWTIME
    assert judge(built, code, number(arch, "setns"), 3, 0) == refused(errno.EPERM)
    assert judge(built, code, number(arch, "clone3"), 0, 88) == refused(errno.ENOSYS)
    assert judge(built, code, number(arch, "clone"), 0x3D0F00) == ALLOW       # a thread (pthread_create)
    assert judge(built, code, number(arch, "clone"), 0x01200011) == ALLOW     # fork: SIGCHLD and the tid flags
    assert judge(built, code, number(arch, "clone"), 0x80 | 17) == ALLOW      # (that bit is the exit signal's)
    assert judge(built, code, number(arch, "unshare"), 0x400) == ALLOW        # CLONE_FILES
    # The high half of the argument is not the kernel's to read either: a flag there changes nothing.
    assert judge(built, code, number(arch, "clone"), (1 << 40) | 17) == ALLOW


@pytest.mark.parametrize("machine,arch", ARCHS)
def test_nested_containers_keep_namespaces_and_mounts_but_nothing_else(machine, arch):
    built, code = program("sandbox", True, machine), rt.SECCOMP_ARCH[arch]
    for name in ("unshare", "setns", "clone3", "mount", "umount2", "pivot_root", "chroot", "sethostname",
                 "fsopen", "move_mount", "mount_setattr", "open_tree_attr"):
        assert judge(built, code, number(arch, name), 0x10000000) == ALLOW, name
    assert judge(built, code, number(arch, "clone"), 0x10000000 | 17) == ALLOW
    for name in ("bpf", "ptrace", "keyctl", "init_module", "kexec_load", "perf_event_open", "userfaultfd",
                 "io_uring_setup", "process_vm_readv", "open_by_handle_at"):
        assert judge(built, code, number(arch, name)) == refused(errno.EPERM), name
    # The container's filter, which holds for a nested run too, never takes what an engine needs.
    outer = program("container", False, machine)
    for name in ("unshare", "setns", "clone3", "mount", "umount2", "pivot_root", "chroot"):
        assert judge(outer, code, number(arch, name), 0x10000000) == ALLOW, name


@pytest.mark.parametrize("machine,arch", ARCHS)
def test_sockets_only_of_the_sandboxs_families(machine, arch):
    """SAI-20: unix, inet, inet6, and netlink for routes, socket lists and device events; the rest - AF_VSOCK,
    AF_ALG, TIPC, RDS, CAN, Bluetooth, packet, XDP and whatever number comes - is no family here. socketpair makes
    sockets too."""
    built, code = program("sandbox", False, machine), rt.SECCOMP_ARCH[arch]
    for call in ("socket", "socketpair"):
        nr = number(arch, call)
        for family in (1, 2, 10):
            assert judge(built, code, nr, family, 1, 0) == ALLOW, (call, family)
        for protocol in (0, 4, 15):
            assert judge(built, code, nr, 16, 3, protocol) == ALLOW, (call, protocol)
        for protocol in (1, 2, 5, 6, 9, 11, 12, 16, 20, 21, 22, 255):  # generic, netfilter, xfrm, audit, rdma ...
            assert judge(built, code, nr, 16, 3, protocol) == refused(errno.EPROTONOSUPPORT), (call, protocol)
        for family in (0, 3, 4, 5, 9, 15, 17, 21, 29, 30, 31, 33, 36, 38, 39, 40, 41, 43, 44, 45, 46, 99, 0xFFFF):
            assert judge(built, code, nr, family, 1, 0) == refused(errno.EAFNOSUPPORT), (call, family)
        # The kernel reads an int: what lies above its 32 bits does not make another family.
        assert judge(built, code, nr, (7 << 32) | 1, 1, 0) == ALLOW
        assert judge(built, code, nr, (1 << 32) | 40, 1, 0) == refused(errno.EAFNOSUPPORT)


@pytest.mark.parametrize("machine,arch", ARCHS)
def test_the_wider_filters_take_packet_sockets_and_every_netlink_protocol_but_no_other_family(machine, arch):
    code = rt.SECCOMP_ARCH[arch]
    for built in (program("container", False, machine), program("sandbox", True, machine)):
        nr = number(arch, "socket")
        for family in (1, 2, 10, 17):
            assert judge(built, code, nr, family, 3, 0) == ALLOW
        for protocol in (0, 4, 12, 16, 21):
            assert judge(built, code, nr, 16, 3, protocol) == ALLOW
        for family in (38, 40, 30, 21, 29, 31, 44):
            assert judge(built, code, nr, family, 1, 0) == refused(errno.EAFNOSUPPORT)


def test_the_32_bit_socketcall_carries_no_family_past_a_filter():
    """A family behind socketcall's pointer cannot be read by any filter (Firejail's --protocol lets it through): a
    64-bit process reaches it with int 0x80. Answered "no such call" in both filters."""
    assert "socketcall" in rt.SYSCALL_NUMBERS["i386"] and "socketcall" not in rt.SYSCALL_NUMBERS["arm"]
    for profile, nested in (("container", False), ("sandbox", False), ("sandbox", True)):
        built = program(profile, nested, "x86_64")
        assert judge(built, rt.SECCOMP_ARCH["i386"], 102, 1, 0) == refused(errno.ENOSYS)
        # the 32-bit calls by number are filtered as the 64-bit ones
        assert judge(built, rt.SECCOMP_ARCH["i386"], 359, 40, 1, 0) == refused(errno.EAFNOSUPPORT)
        assert judge(built, rt.SECCOMP_ARCH["i386"], 359, 1, 1, 0) == ALLOW


def test_a_call_the_table_does_not_know_is_no_call():
    """SAI-16: a newer kernel's calls are not allowed by default. Above the table's last number, in the kernel's own
    gaps, and x86-64's x32 calls (the same numbers with a bit set, which no name would match)."""
    built = program("sandbox", False, "x86_64")
    code = rt.SECCOMP_ARCH["x86_64"]
    assert judge(built, code, rt.SYSCALL_TOP) == ALLOW and judge(built, code, 334) == ALLOW
    assert judge(built, code, 335) == ALLOW and judge(built, code, 336) == ALLOW  # uretprobe, uprobe: the kernel's own
    for nr in (rt.SYSCALL_TOP + 1, 500, 547, 1000, 0x7FFFFFFF, 337, 400, 423, 0xFFFFFFFF):
        assert judge(built, code, nr) == refused(errno.ENOSYS), nr
    for nr in (rt.X32_CALLS, rt.X32_CALLS | 1, rt.X32_CALLS | 425, rt.X32_CALLS | 41):
        assert judge(built, code, nr) == refused(errno.ENOSYS), hex(nr)
        assert judge(program("container"), code, nr) == refused(errno.ENOSYS), hex(nr)
    arm64 = program("sandbox", False, "aarch64")
    for nr in (244, 259, 295, 402, 423, rt.SYSCALL_TOP + 1):
        assert judge(arm64, rt.SECCOMP_ARCH["aarch64"], nr) == refused(errno.ENOSYS), nr
    assert judge(arm64, rt.SECCOMP_ARCH["aarch64"], 243) == ALLOW and judge(arm64, rt.SECCOMP_ARCH["aarch64"], 260) == ALLOW
    # ARM's own calls lie above every number and every process needs them (set_tls).
    for nr in (0x0F0002, 0x0F0005):
        assert judge(arm64, rt.SECCOMP_ARCH["arm"], nr) == ALLOW
    assert judge(arm64, rt.SECCOMP_ARCH["arm"], 0x0F0007) == refused(errno.ENOSYS)
    assert judge(arm64, rt.SECCOMP_ARCH["arm"], 0x0F0000) == refused(errno.ENOSYS)
    # The container's filter names what it refuses and lets the rest pass: Firejail and the firewall's tools are
    # not the agent.
    assert judge(program("container"), code, 1000) == ALLOW


def test_another_architectures_calls_are_no_calls():
    built = program("sandbox", False, "x86_64")
    for arch in ("aarch64", "arm"):
        assert judge(built, rt.SECCOMP_ARCH[arch], 0) == refused(errno.ENOSYS)
    assert judge(program("sandbox", False, "aarch64"), rt.SECCOMP_ARCH["x86_64"], 0) == refused(errno.ENOSYS)
    with pytest.raises(ValueError, match="riscv64"):
        rt.seccomp_program(rt.seccomp_policy("sandbox"), "riscv64")
    with pytest.raises(ValueError):
        rt.seccomp_policy("other")


def test_every_number_of_every_architecture_has_one_answer_and_the_manifest_says_the_filter():
    """The whole decision, for the manifest and a review: each number below the table's end is allowed, refused
    with its error or "no such call" - and what is refused is exactly what the policy names."""
    for machine, arch in ARCHS:
        policy = rt.seccomp_policy("sandbox")
        built = rt.seccomp_program(policy, machine)
        known = {**rt.SYSCALL_NUMBERS[arch], **rt.SYSCALL_COMMON}
        named = {known[name] for name in [*policy["refuse"], "socketcall"] if name in known}
        argued = {known[name] for name in ("clone", "unshare", "socket", "socketpair")}
        gaps = {nr for low, high in rt.SYSCALL_UNUSED.get(arch, ()) for nr in range(low, high + 1)}
        for nr in range(0, rt.SYSCALL_TOP + 1):
            answer = judge(built, rt.SECCOMP_ARCH[arch], nr, 1, 1, 0)
            if nr in gaps:
                assert answer == refused(errno.ENOSYS), (arch, nr)
            elif nr in named:
                assert answer != ALLOW, (arch, nr)
            elif nr not in argued:
                assert answer == ALLOW, (arch, nr)
    account = rt.seccomp_account(rt.seccomp_policy("sandbox"), "x86_64")
    assert account["architectures"] == ["x86_64", "i386"] and account["new_namespaces"] == "refused"
    assert account["socket_families"] == ["unix", "inet", "inet6", "netlink"]
    assert account["netlink_protocols"] == ["route", "sock_diag", "kobject_uevent"]
    assert "io_uring_setup" in account["refused"]["EPERM"] and account["refused"]["ENOSYS"] == ["clone3"]
    assert len(account["sha256"]) == 64 and account["instructions"] == len(program()) // 8
    assert account["sha256"] == rt.seccomp_account(rt.seccomp_policy("sandbox"), "x86_64")["sha256"]
    assert account["sha256"] != rt.seccomp_account(rt.seccomp_policy("sandbox", nested=True), "x86_64")["sha256"]
    assert "469" in account["unknown_calls"]
    assert rt.seccomp_account(rt.seccomp_policy("container"), "aarch64")["netlink_protocols"] == "all"


def test_the_assembler_refuses_what_the_kernel_would():
    with pytest.raises(ValueError, match="twice"):
        rt.bpf_assemble([("label", "a"), ("label", "a"), ("return", 0)])
    with pytest.raises(ValueError, match="instructions"):
        rt.bpf_assemble([("label", "a")])
    with pytest.raises(ValueError, match="jump"):  # a conditional jump reaches 255 instructions at most
        rt.bpf_assemble([("if", rt.BPF_EQUAL, 1, "far", None), *[("return", 0)] * 300, ("label", "far"), ("return", 0)])
    with pytest.raises(ValueError, match="jump"):  # and never backwards
        rt.bpf_assemble([("label", "back"), ("return", 0), ("jump", "back")])
    assert rt.bpf_assemble([("return", 7)]) == struct.pack("=HBBI", rt.BPF_RETURN, 0, 0, 7)


# --- the tables -----------------------------------------------------------------------------------------------------

def test_the_numbers_are_libseccomps():
    try:
        library = ctypes.CDLL("libseccomp.so.2")
    except OSError:
        pytest.skip("libseccomp is not installed here")
    library.seccomp_syscall_resolve_name_arch.argtypes = [ctypes.c_uint32, ctypes.c_char_p]
    multiplexed = {("i386", "socket"): 359, ("i386", "socketpair"): 360}  # (libseccomp answers its own number there)
    for arch, table in rt.SYSCALL_NUMBERS.items():
        for name, nr in table.items():
            found = library.seccomp_syscall_resolve_name_arch(rt.SECCOMP_ARCH[arch], name.encode())
            assert multiplexed.get((arch, name), found) == nr, (arch, name)
    # every call a policy names has a number on the two architectures the images exist for, or is a 32-bit name
    only_32 = {"stime", "umount", "vm86", "vm86old", "clock_settime64", "clock_adjtime64", "socketcall"}
    x86_only = {"modify_ldt", "_sysctl", "afs_syscall", "create_module", "get_kernel_syms", "getpmsg", "putpmsg",
                "query_module", "security", "sysfs", "tuxcall", "uselib", "ustat", "vserver", "ioperm", "iopl"}
    for policy in (rt.seccomp_policy("container"), rt.seccomp_policy("sandbox"), rt.seccomp_policy("sandbox", True)):
        for name in policy["refuse"]:
            if name not in only_32:
                assert name in {**rt.SYSCALL_NUMBERS["x86_64"], **rt.SYSCALL_COMMON}, name
                assert name in x86_only or name in {**rt.SYSCALL_NUMBERS["aarch64"], **rt.SYSCALL_COMMON}, name


def firejail_filter(*arguments):
    out = Path(os.environ.get("TMPDIR", "/tmp")) / f"fseccomp-{os.getpid()}"
    try:
        subprocess.run([str(FSECCOMP), *[str(out) if value == "FILE" else value for value in arguments]], check=True,
                       capture_output=True, timeout=30)
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


@pytest.mark.skipif(FSECCOMP is None, reason="Firejail's fseccomp is not installed here")
def test_the_sandboxs_filter_refuses_all_the_installed_firejails_default_list_does():
    """Whatever the image's Firejail is, sandbox-ai's filter alone refuses its list - and more, which is said."""
    theirs, ours = firejail_filter("default", "FILE"), program("sandbox")
    code = rt.SECCOMP_ARCH["x86_64"]
    names = {nr: name for name, nr in {**rt.SYSCALL_NUMBERS["x86_64"], **rt.SYSCALL_COMMON}.items()}
    more = set()
    for nr in range(0, rt.SYSCALL_TOP + 1):
        if judge(theirs, code, nr) != ALLOW:
            assert judge(ours, code, nr) != ALLOW, names.get(nr, nr)
        elif judge(ours, code, nr, 0x10000000) != ALLOW and nr not in range(337, 424):
            more.add(names.get(nr, nr))
    assert {"io_uring_setup", "io_uring_enter", "io_uring_register", "clone3", "setns", "unshare", "clone",
            "process_madvise", "mount_setattr"} <= more
    assert more <= {"io_uring_setup", "io_uring_enter", "io_uring_register", "clone3", "setns", "unshare", "clone",
                    "process_madvise", "mount_setattr", "quotactl", "quotactl_fd", "open_tree_attr", "socket",
                    "socketpair"}, more


@pytest.mark.skipif(FSECCOMP is None, reason="Firejail's fseccomp is not installed here")
def test_firejails_own_namespace_and_protocol_filters_agree_with_ours():
    """--restrict-namespaces and --protocol=unix,inet,inet6,netlink of the installed Firejail: what they refuse, the
    sandbox's filter refuses."""
    code, ours = rt.SECCOMP_ARCH["x86_64"], program("sandbox")
    spaces = firejail_filter("restrict-namespaces", "FILE", "cgroup,ipc,net,mnt,pid,time,user,uts")
    for name, args in (("clone", (0x10000000 | 17,)), ("clone", (0x20000,)), ("unshare", (0x10000000,)),
                       ("unshare", (0x80,)), ("setns", (3, 0)), ("setns", (3, 0x10000000)), ("clone3", (0, 88))):
        nr = number("x86_64", name)
        assert judge(spaces, code, nr, *args) != ALLOW, (name, args)
        assert judge(ours, code, nr, *args) != ALLOW, (name, args)
    assert judge(spaces, code, number("x86_64", "clone"), 0x3D0F00) == ALLOW
    families = firejail_filter("protocol", "build", "unix,inet,inet6,netlink", "FILE")
    for family in range(0, 48):
        if judge(families, code, 41, family, 1, 0) != ALLOW:
            assert judge(ours, code, 41, family, 1, 0) != ALLOW, family
    assert judge(families, code, 41, 40, 1, 0) != ALLOW and judge(families, code, 41, 1, 1, 0) == ALLOW
    # Firejail's lets the 32-bit socketcall through - ours does not (above).
    assert judge(families, rt.SECCOMP_ARCH["i386"], 102, 1, 0) == ALLOW


# --- the kernel, asked in a child process ---------------------------------------------------------------------------

CHILD = r"""
import ctypes, errno, json, os, socket, subprocess, sys, threading
sys.dont_write_bytecode = True
import importlib.util
spec = importlib.util.spec_from_file_location("sandbox_runtime_child", sys.argv[1])
rt = importlib.util.module_from_spec(spec); spec.loader.exec_module(rt)
profile, nested = sys.argv[2], sys.argv[3] == "nested"
libc = ctypes.CDLL(None, use_errno=True)
libc.prctl(38, 1, 0, 0, 0)  # no_new_privs: an ordinary process may install a filter then
rt.SANDBOX_PROCESS = True
account = rt.own_filter(profile, rt.isolation_settings({}), nested=nested)
out = {"account": account}
def call(name, function):
    ctypes.set_errno(0)
    try:
        value = function()
        out[name] = "ok" if value != -1 else errno.errorcode.get(ctypes.get_errno(), ctypes.get_errno())
    except OSError as exc:
        out[name] = errno.errorcode.get(exc.errno, exc.errno)
def sock(family, kind, protocol=0):
    def make():
        socket.socket(family, kind, protocol).close()
        return 0
    return make
numbers = rt.SYSCALL_NUMBERS[rt.SECCOMP_MACHINES[os.uname().machine][0]]
call("io_uring_setup", lambda: libc.syscall(ctypes.c_long(numbers["io_uring_setup"]), ctypes.c_long(0), ctypes.c_long(0)))
call("io_uring_enter", lambda: libc.syscall(ctypes.c_long(numbers["io_uring_enter"]), ctypes.c_long(-1), ctypes.c_long(0)))
call("unknown", lambda: libc.syscall(ctypes.c_long(rt.SYSCALL_TOP + 30)))
call("unix", sock(socket.AF_UNIX, socket.SOCK_STREAM))
call("inet", sock(socket.AF_INET, socket.SOCK_DGRAM))
call("netlink_route", sock(16, socket.SOCK_RAW, 0))
call("netlink_generic", sock(16, socket.SOCK_RAW, 16))
call("vsock", sock(40, socket.SOCK_STREAM))
call("alg", sock(38, socket.SOCK_SEQPACKET))
call("rds", sock(21, socket.SOCK_SEQPACKET))
call("tipc", sock(30, socket.SOCK_RDM))
call("socketpair", lambda: [end.close() for end in socket.socketpair()] and 0)
call("unshare_files", lambda: libc.unshare(0x400))
pid = os.fork()
if pid == 0:
    os._exit(0 if libc.unshare(0x10000000) == 0 else ctypes.get_errno() or 255)
out["unshare_user"] = os.waitpid(pid, 0)[1] >> 8
ran = []
thread = threading.Thread(target=lambda: ran.append(1)); thread.start(); thread.join()
out["thread"] = bool(ran)
out["subprocess"] = subprocess.run([sys.executable, "-I", "-c", "print(6 * 7)"], capture_output=True, text=True).stdout.strip()
out["probe"] = rt.seccomp_probe(rt.seccomp_policy(profile, nested=nested))
print(json.dumps(out))
"""
NATIVE = pytest.mark.skipif(MACHINE not in rt.SECCOMP_MACHINES, reason="no filter for this machine")


def in_a_child(profile, nested=False):
    result = subprocess.run([sys.executable, "-I", "-c", CHILD, str(RUNTIME_FILE), profile,
                             "nested" if nested else "plain"], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@NATIVE
def test_the_kernel_takes_the_sandboxs_filter_and_refuses_what_it_names():
    """SAI-13, -03, -20, -16 against this kernel: io_uring, a new user namespace, AF_VSOCK, AF_ALG, RDS, TIPC and a
    netlink protocol outside the list are refused by the filter; an unknown call number is no call; threads (clone3
    falls back to clone), a fork, a program and the everyday sockets work."""
    found = in_a_child("sandbox")
    assert found["io_uring_setup"] == "EPERM" and found["io_uring_enter"] == "EPERM"
    assert found["unknown"] == "ENOSYS"
    assert found["unix"] == "ok" and found["inet"] == "ok" and found["netlink_route"] == "ok"
    assert found["socketpair"] == "ok"
    assert found["netlink_generic"] == "EPROTONOSUPPORT"
    for family in ("vsock", "alg", "rds", "tipc"):
        assert found[family] == "EAFNOSUPPORT", family
    assert found["unshare_files"] == "ok" and found["unshare_user"] == errno.EPERM
    assert found["thread"] is True and found["subprocess"] == "42"
    assert found["probe"] == []
    assert found["account"]["profile"] == "sandbox" and found["account"]["new_namespaces"] == "refused"


@NATIVE
def test_the_kernel_takes_the_containers_filter_which_leaves_firejail_what_it_needs():
    found = in_a_child("container")
    assert found["io_uring_setup"] == "EPERM"
    for family in ("vsock", "alg", "rds", "tipc"):
        assert found[family] == "EAFNOSUPPORT", family
    assert found["unix"] == "ok" and found["inet"] == "ok" and found["netlink_route"] == "ok"
    assert found["netlink_generic"] in ("ok", "EPROTONOSUPPORT")  # (refused only by a filter around the tests)
    assert found["thread"] is True and found["subprocess"] == "42" and found["probe"] == []
    assert found["account"]["new_namespaces"] == "allowed"


@NATIVE
def test_the_kernel_takes_the_nested_filter():
    found = in_a_child("sandbox", nested=True)
    assert found["io_uring_setup"] == "EPERM" and found["vsock"] == "EAFNOSUPPORT" and found["probe"] == []
    assert found["thread"] is True and found["subprocess"] == "42"


# --- when it is installed, and what happens when it does not hold -------------------------------------------------

def test_a_program_that_loads_the_file_never_gets_a_filter(monkeypatch):
    """The launcher and the tests load runtime.py: only the sandbox's own processes (the file run as a program)
    install a filter on themselves."""
    installed = []
    assert rt.SANDBOX_PROCESS is False
    assert rt.own_filter("sandbox", rt.isolation_settings({}), install=installed.append) is None
    assert installed == []
    source = RUNTIME_FILE.read_text()
    assert 'if __name__ == "__main__":\n    SANDBOX_PROCESS = True' in source


def test_the_settings_keep_the_filter_on_in_a_strict_mode():
    assert rt.isolation_settings({}) == {"strict": False, "syscall_filter": "on"}
    assert rt.isolation_settings({"isolation": {"syscall_filter": "off"}})["syscall_filter"] == "off"
    strict = rt.isolation_settings({"isolation": {"syscall_filter": "off", "strict": True, "mounts": {"/work": []}}})
    assert strict["syscall_filter"] == "on" and strict["strict"] is True and strict["mounts"] == {"/work": []}
    assert rt.isolation_settings({"isolation": "nonsense"}) == {"strict": False, "syscall_filter": "on"}
    assert rt.isolation_settings({"isolation": {"strict": "yes"}})["strict"] is False


def test_a_filter_that_does_not_hold_starts_nothing(monkeypatch):
    monkeypatch.setattr(rt, "SANDBOX_PROCESS", True)
    on, strict = rt.isolation_settings({}), rt.isolation_settings({"isolation": {"strict": True}})
    off = rt.isolation_settings({"isolation": {"syscall_filter": "off"}})
    installed, said = [], []

    def refuses(_program):
        raise OSError(errno.EACCES, "the kernel did not take the filter (Permission denied)")
    # held: its account
    account = rt.own_filter("sandbox", on, install=installed.append, probe=lambda policy, machine: [],
                            machine="x86_64")
    assert account["profile"] == "sandbox" and installed == [program("sandbox")]
    # switched off by the human: none installed
    assert rt.own_filter("sandbox", off, install=installed.append, probe=lambda *a: []) is None and len(installed) == 1
    # the agent's filter: no start without it, in every mode - and the way out is named
    with pytest.raises(RuntimeError, match="SANDBOX_AI_SYSCALL_FILTER=off"):
        rt.own_filter("sandbox", on, install=refuses, machine="x86_64")
    with pytest.raises(RuntimeError, match="installed, but not refused: io_uring"):
        rt.own_filter("sandbox", on, install=lambda p: None, probe=lambda policy, machine: ["io_uring"],
                      machine="x86_64")
    # a machine without a table: said, the agent runs with Firejail's filter alone - a strict mode does not start
    count = len(installed)
    assert rt.own_filter("sandbox", on, install=installed.append, machine="riscv64", say=said.append) is None
    assert "riscv64" in said[-1] and "Firejail's filter alone" in said[-1] and len(installed) == count
    assert rt.own_filter("container", on, install=installed.append, machine="riscv64", say=said.append) is None
    assert len(said) == 1  # (said once per run, by the agent's)
    with pytest.raises(RuntimeError, match="riscv64"):
        rt.own_filter("sandbox", strict, install=installed.append, machine="riscv64")
    said.clear()
    # the container's: said and the run goes on - but not in a strict mode
    assert rt.own_filter("container", on, install=refuses, machine="x86_64", say=said.append) is None
    assert "does not hold" in said[0] and "goes on" in said[0]
    with pytest.raises(RuntimeError) as failure:
        rt.own_filter("container", strict, install=refuses, machine="x86_64", say=said.append)
    assert "SANDBOX_AI_SYSCALL_FILTER" not in str(failure.value)
    assert len(said) == 1


def test_the_guard_installs_the_filter_last_and_the_supervisor_first():
    """Where the calls stand: the guard after the checks and the nested namespace, before the agent; the supervisor
    before the firewall is loaded; the proxy after it has its user."""
    source = RUNTIME_FILE.read_text()
    guard = source[source.index("def inner_guard("):source.index("CAIRN_RELAY = ")]
    assert guard.index("verify_inner(") < guard.index("enter_nested_userns(") < guard.index('own_filter("sandbox"') \
        < guard.index("start_cairn_relay(") < guard.index("os.execvpe(")
    boot = source[source.index("def inner_bootstrap("):source.index("def start_gui(")]
    assert boot.index('own_filter("container"') < boot.index('"nft", "-f"')
    assert boot.index("isolation_ready(") < boot.index("child = subprocess.Popen(cmd")
    drop = source[source.index("def drop_to_proxy_user("):source.index("def proxy_listeners(")]
    assert drop.index("os.setresuid(") < drop.index("prctl(38, 1") < drop.index("proxy_confined(")


def test_firejail_gets_its_own_namespace_and_protocol_filters_where_it_has_them(monkeypatch):
    def with_help(text):
        monkeypatch.setattr(rt.subprocess, "run", lambda *a, **kw: type("R", (), {"stdout": text})())
        return rt.firejail_command({})
    monkeypatch.setattr(rt, "FIREJAIL_CONFIG", Path("/nonexistent/firejail"))
    monkeypatch.setattr(rt, "RUN_ISOLATION", None)
    new = "--restrict-namespaces\n--protocol=protocol,protocol\n--deterministic-exit-code\n"
    command = with_help(new)
    assert "--restrict-namespaces" in command and "--protocol=unix,inet,inet6,netlink" in command
    assert command.index("--seccomp") < command.index("--restrict-namespaces")
    old = with_help("--protocol=protocol\n")  # (Firejail before 0.9.72 has no --restrict-namespaces)
    assert "--restrict-namespaces" not in old and "--protocol=unix,inet,inet6,netlink" in old
    assert not [option for option in with_help("") if option.startswith(("--restrict", "--protocol"))]
    # --nested-containers: its engines make namespaces and packet sockets
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **kw: type("R", (), {"stdout": new})())
    nested = rt.firejail_command({}, nested=True)
    assert not [option for option in nested if option.startswith(("--restrict", "--protocol"))]
    assert rt.NESTED_SECCOMP in nested
    # the human's switch takes Firejail's two with it - never in a strict mode
    monkeypatch.setattr(rt, "RUN_ISOLATION", rt.isolation_settings({"isolation": {"syscall_filter": "off"}}))
    assert not [option for option in rt.firejail_command({}) if option.startswith(("--restrict", "--protocol"))]
    monkeypatch.setattr(rt, "RUN_ISOLATION",
                        rt.isolation_settings({"isolation": {"syscall_filter": "off", "strict": True}}))
    assert "--restrict-namespaces" in rt.firejail_command({})
