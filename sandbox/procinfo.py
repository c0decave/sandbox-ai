"""What the host knows about its processes, on Linux and macOS: whether one lives, when it started, its command line,
its owner, state, parent and working directory, and every process of the machine. Standard library only; the
modules load it by path.

Linux reads /proc as sandbox-ai always did (PROC, or the directory a caller hands in: the tests' fake /proc). macOS
has no /proc: the same answers come from the kernel's sysctl (KERN_PROC_PID, KERN_PROC_ALL, KERN_PROCARGS2) and
libproc (proc_pidinfo for the working directory), through ctypes. Without them every process would look gone there:
sessions stopped, and `sandbox-ai mcp` removing a live server's state (TODO.md: macOS support).

Start times are compared, never shown as they are: Linux gives clock ticks after boot (field 22 of /proc/PID/stat, as
the records written so far hold), macOS microseconds since the epoch. A record is only ever compared on the host that
wrote it; started_epoch() turns either into seconds since the epoch.
"""
import ctypes
import ctypes.util
import os
from pathlib import Path
import struct
import sys

PROC = Path("/proc")

# sysctl names (<sys/sysctl.h>) and the libproc flavour (<sys/proc_info.h>) of macOS.
CTL_KERN, KERN_ARGMAX, KERN_PROC, KERN_PROCARGS2 = 1, 8, 14, 49
KERN_PROC_ALL, KERN_PROC_PID = 0, 1
PROC_PIDVNODEPATHINFO = 9
VNODE_INFO_SIZE, MAXPATHLEN = 152, 1024
F_GETPATH = 50  # fcntl.F_GETPATH where Python has it; the number is macOS's
# p_stat of macOS's extern_proc as /proc/PID/stat's state letters: SIDL, SRUN, SSLEEP, SSTOP, SZOMB.
DARWIN_STATES = {1: "R", 2: "R", 3: "S", 4: "T", 5: "Z"}


def darwin(proc=PROC):
    """macOS, and the caller asks about this machine's processes (not a fake /proc of a test)."""
    return sys.platform == "darwin" and Path(proc) == PROC


# --- macOS: struct kinfo_proc (<sys/sysctl.h>, <sys/proc.h>), 648 bytes on 64-bit macOS (x86_64 and arm64) ----------

class Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int32)]


class ExternProc(ctypes.Structure):
    _fields_ = [("p_starttime", Timeval), ("p_vmspace", ctypes.c_void_p), ("p_sigacts", ctypes.c_void_p),
                ("p_flag", ctypes.c_int), ("p_stat", ctypes.c_char), ("p_pid", ctypes.c_int32),
                ("p_oppid", ctypes.c_int32), ("p_dupfd", ctypes.c_int), ("user_stack", ctypes.c_void_p),
                ("exit_thread", ctypes.c_void_p), ("p_debugger", ctypes.c_int), ("sigwait", ctypes.c_int),
                ("p_estcpu", ctypes.c_uint), ("p_cpticks", ctypes.c_int), ("p_pctcpu", ctypes.c_uint32),
                ("p_wchan", ctypes.c_void_p), ("p_wmesg", ctypes.c_void_p), ("p_swtime", ctypes.c_uint),
                ("p_slptime", ctypes.c_uint), ("p_realtimer", Timeval * 2), ("p_rtime", Timeval),
                ("p_uticks", ctypes.c_uint64), ("p_sticks", ctypes.c_uint64), ("p_iticks", ctypes.c_uint64),
                ("p_traceflag", ctypes.c_int), ("p_tracep", ctypes.c_void_p), ("p_siglist", ctypes.c_int),
                ("p_textvp", ctypes.c_void_p), ("p_holdcnt", ctypes.c_int), ("p_sigmask", ctypes.c_uint32),
                ("p_sigignore", ctypes.c_uint32), ("p_sigcatch", ctypes.c_uint32), ("p_priority", ctypes.c_ubyte),
                ("p_usrpri", ctypes.c_ubyte), ("p_nice", ctypes.c_byte), ("p_comm", ctypes.c_char * 17),
                ("p_pgrp", ctypes.c_void_p), ("p_addr", ctypes.c_void_p), ("p_xstat", ctypes.c_uint16),
                ("p_acflag", ctypes.c_uint16), ("p_ru", ctypes.c_void_p)]


class Pcred(ctypes.Structure):
    _fields_ = [("pc_lock", ctypes.c_char * 72), ("pc_ucred", ctypes.c_void_p), ("p_ruid", ctypes.c_uint32),
                ("p_svuid", ctypes.c_uint32), ("p_rgid", ctypes.c_uint32), ("p_svgid", ctypes.c_uint32),
                ("p_refcnt", ctypes.c_int)]


class Ucred(ctypes.Structure):
    _fields_ = [("cr_ref", ctypes.c_int32), ("cr_uid", ctypes.c_uint32), ("cr_ngroups", ctypes.c_short),
                ("cr_groups", ctypes.c_uint32 * 16)]


class Vmspace(ctypes.Structure):
    _fields_ = [("vm_refcnt", ctypes.c_int), ("vm_shm", ctypes.c_void_p), ("vm_sizes", ctypes.c_int32 * 5),
                ("vm_addrs", ctypes.c_void_p * 3)]


class Eproc(ctypes.Structure):
    _fields_ = [("e_paddr", ctypes.c_void_p), ("e_sess", ctypes.c_void_p), ("e_pcred", Pcred),
                ("e_ucred", Ucred), ("e_vm", Vmspace), ("e_ppid", ctypes.c_int32), ("e_pgid", ctypes.c_int32),
                ("e_jobc", ctypes.c_short), ("e_tdev", ctypes.c_int32), ("e_tpgid", ctypes.c_int32),
                ("e_tsess", ctypes.c_void_p), ("e_wmesg", ctypes.c_char * 8), ("e_xsize", ctypes.c_int32),
                ("e_xrssize", ctypes.c_short), ("e_xccount", ctypes.c_short), ("e_xswrss", ctypes.c_short),
                ("e_flag", ctypes.c_int32), ("e_login", ctypes.c_char * 12), ("e_spare", ctypes.c_int32 * 4)]


class KinfoProc(ctypes.Structure):
    _fields_ = [("kp_proc", ExternProc), ("kp_eproc", Eproc)]


_libc = None


def libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    return _libc


def native_sysctl(mib, size=None):
    """The value of the sysctl MIB (a list of ints) as bytes; OSError when the kernel refuses (ESRCH, EPERM,
    EINVAL for a process that is gone)."""
    name = (ctypes.c_int * len(mib))(*mib)
    length = ctypes.c_size_t(0)
    if size is None:
        if libc().sysctl(name, len(mib), None, ctypes.byref(length), None, 0) != 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        size = length.value + (length.value >> 3) + 4096  # the process table may grow meanwhile
    buffer = ctypes.create_string_buffer(size)
    length.value = size
    if libc().sysctl(name, len(mib), buffer, ctypes.byref(length), None, 0) != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return buffer.raw[:length.value]


def native_pidinfo(pid, flavor, size):
    """libproc's proc_pidinfo(PID, FLAVOR, 0, buffer, SIZE) as bytes; OSError when it fails."""
    library = ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib", use_errno=True)
    buffer = ctypes.create_string_buffer(size)
    got = library.proc_pidinfo(int(pid), flavor, ctypes.c_uint64(0), buffer, size)
    if got <= 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno() or 3))
    return buffer.raw[:got]


# The tests hand in fakes for these two (there is no macOS kernel to ask on Linux).
SYSCTL = native_sysctl
PIDINFO = native_pidinfo


def kinfo(pid):
    """macOS: the kinfo_proc of PID, or None when it is gone."""
    try:
        data = SYSCTL([CTL_KERN, KERN_PROC, KERN_PROC_PID, int(pid)], ctypes.sizeof(KinfoProc))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if len(data) < ctypes.sizeof(KinfoProc):
        return None  # an empty answer: no such process
    info = KinfoProc.from_buffer_copy(data[:ctypes.sizeof(KinfoProc)])
    return info if info.kp_proc.p_pid == int(pid) else None


def every_kinfo():
    """macOS: the kinfo_proc of every process of the machine ([] when the kernel does not answer)."""
    try:
        data = SYSCTL([CTL_KERN, KERN_PROC, KERN_PROC_ALL])  # three names on macOS (FreeBSD takes a fourth)
    except (OSError, ValueError, AttributeError):
        return []
    size = ctypes.sizeof(KinfoProc)
    return [KinfoProc.from_buffer_copy(data[offset:offset + size]) for offset in range(0, len(data) - size + 1, size)]


def darwin_start(info):
    return info.kp_proc.p_starttime.tv_sec * 1_000_000 + info.kp_proc.p_starttime.tv_usec


def darwin_cmdline(pid):
    """macOS: PID's arguments from KERN_PROCARGS2 (argc, the executable's path, NULs, then the arguments), as
    /proc/PID/cmdline split at its NULs (with the empty item after the last one); None when it cannot be read."""
    try:
        size = struct.unpack("i", SYSCTL([CTL_KERN, KERN_ARGMAX], 4)[:4])[0]
        data = SYSCTL([CTL_KERN, KERN_PROCARGS2, int(pid)], max(size, 4096))
    except (OSError, ValueError, TypeError, AttributeError, struct.error):
        return None
    if len(data) < 4:
        return None
    argc = struct.unpack("i", data[:4])[0]
    rest = data[4:]
    end = rest.find(b"\0")
    if end < 0:
        return None
    rest = rest[end:].lstrip(b"\0")  # the executable's path and its padding
    args = rest.split(b"\0")[:argc]
    return [*args, b""] if args else []


# --- what the callers ask ----------------------------------------------------------------------------------------

def stat_fields(pid, proc=PROC):
    """Linux: the fields of /proc/PID/stat after the command's name (comm may hold spaces and parentheses: they
    follow the last ')'), field 3 (the state) first; None when it is gone."""
    try:
        return (Path(proc) / str(int(pid)) / "stat").read_text().rpartition(")")[2].split()
    except (OSError, ValueError, TypeError):
        return None


def start_time(pid, proc=PROC):
    """When PID started (see above for the unit), or None when it is gone. Recorded with a PID, so a later look
    tells a PID given out again apart."""
    if darwin(proc):
        info = kinfo(pid)
        return darwin_start(info) if info is not None else None
    fields = stat_fields(pid, proc)
    try:
        return int(fields[19])
    except (TypeError, IndexError, ValueError):
        return None


def state(pid, proc=PROC):
    """(state letter as in /proc/PID/stat, start time) of PID, or None when it is gone."""
    if darwin(proc):
        info = kinfo(pid)
        if info is None:
            return None
        return DARWIN_STATES.get(ord(info.kp_proc.p_stat or b"\0"), "S"), darwin_start(info)
    fields = stat_fields(pid, proc)
    try:
        return fields[0], int(fields[19])
    except (TypeError, IndexError, ValueError):
        return None


def started_epoch(pid, proc=PROC):
    """Seconds since the epoch at which PID started, or None."""
    if darwin(proc):
        found = start_time(pid, proc)
        return found / 1_000_000 if found is not None else None
    ticks = start_time(pid, proc)
    if ticks is None:
        return None
    try:
        for line in (Path(proc) / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1]) + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        pass
    return None


def cmdline(pid, proc=PROC):
    """PID's arguments as bytes, split at their NULs as /proc/PID/cmdline holds them (an empty item after the
    last); None when they cannot be read (gone, or another user's on macOS)."""
    if darwin(proc):
        return darwin_cmdline(pid)
    try:
        return (Path(proc) / str(int(pid)) / "cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError, TypeError):
        return None


def recorded_cmdline(pid, start, proc=PROC):
    """agent_sandbox.process_cmdline (a test keeps both the same), for the modules that do not load the launcher for
    it: the arguments of the process recorded as PID with start time START; [] when it is gone or the PID now belongs
    to another process (START None: a record of an older version, the caller's match of the arguments decides)."""
    if not pid:
        return []
    found = cmdline(pid, proc)
    if found is None:
        return []
    # Compared after reading the arguments: a match means they belonged to the recorded process.
    if start is not None and start_time(pid, proc) != start:
        return []
    return found


def owner(pid, proc=PROC):
    """The user ID PID runs as, or None when it is gone."""
    if darwin(proc):
        info = kinfo(pid)
        return info.kp_eproc.e_ucred.cr_uid if info is not None else None
    try:
        return os.stat(Path(proc) / str(int(pid))).st_uid
    except (OSError, ValueError, TypeError):
        return None


def parent(pid, proc=PROC):
    """PID's parent, or None when it is gone."""
    if darwin(proc):
        info = kinfo(pid)
        return info.kp_eproc.e_ppid if info is not None else None
    fields = stat_fields(pid, proc)
    try:
        return int(fields[1])
    except (TypeError, IndexError, ValueError):
        return None


def table(proc=PROC):
    """{pid: (parent, process group, state letter)} of every process of the machine that can be seen."""
    if darwin(proc):
        return {info.kp_proc.p_pid: (info.kp_eproc.e_ppid, info.kp_eproc.e_pgid,
                                     DARWIN_STATES.get(ord(info.kp_proc.p_stat or b"\0"), "S"))
                for info in every_kinfo() if info.kp_proc.p_pid > 0}
    found = {}
    try:
        entries = [entry for entry in os.listdir(proc) if entry.isdigit()]
    except OSError:
        return found
    for entry in entries:
        fields = stat_fields(entry, proc)
        try:
            ppid = int(fields[1])
        except (TypeError, IndexError, ValueError):
            continue
        try:
            pgid = int(fields[2])
        except (IndexError, ValueError):
            pgid = None
        found[int(entry)] = (ppid, pgid, fields[0])
    return found


def pids(proc=PROC):
    """Every PID of the machine that can be seen."""
    if darwin(proc):
        return sorted(table(proc))
    try:
        return sorted(int(entry) for entry in os.listdir(proc) if entry.isdigit())
    except OSError:
        return []


def children(pid, proc=PROC):
    """PID's children, sorted."""
    return sorted(child for child, (parent_, _, _) in table(proc).items() if parent_ == int(pid))


def cwd(pid, proc=PROC):
    """PID's working directory, or None (gone, another user's)."""
    if darwin(proc):
        try:
            data = PIDINFO(int(pid), PROC_PIDVNODEPATHINFO, 2 * (VNODE_INFO_SIZE + MAXPATHLEN))
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        path = data[VNODE_INFO_SIZE:VNODE_INFO_SIZE + MAXPATHLEN].split(b"\0", 1)[0]
        return os.fsdecode(path) if path else None
    try:
        return os.readlink(Path(proc) / str(int(pid)) / "cwd")
    except (OSError, ValueError, TypeError):
        return None


def fd_path(fd):
    """Where the open file FD really is (Linux: /proc/self/fd; macOS: fcntl F_GETPATH), or None."""
    if sys.platform != "darwin":
        try:
            return os.readlink(f"/proc/self/fd/{int(fd)}")
        except OSError:
            return None
    import fcntl
    try:
        return os.fsdecode(fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", F_GETPATH), bytes(MAXPATHLEN)).split(b"\0", 1)[0])
    except (OSError, ValueError):
        return None
