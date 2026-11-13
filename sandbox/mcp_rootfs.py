#!/usr/bin/env python3
"""Catalog MCP servers under Bubblewrap: the same server as the container image, from a root file system archive
of that image (published with it, its SHA-256 in the signed catalog), run per connection like the container:

  bwrap --unshare-all (the network too, or slirp4netns without the host's loopback for network "full")
        --uid U --gid U --ro-bind ROOTFS / --proc /proc --dev /dev --tmpfs /tmp --cap-drop ALL --clearenv
        --die-with-parent --new-session [--ro-bind|--bind HOST TARGET] -- ENTRYPOINT

in a systemd scope with the memory and process limits when the user's systemd runs (as Bubblewrap agents are).
The archive is checked against its SHA-256 before anything of it is unpacked, and unpacked without trusting it:
no absolute paths, no "..", no devices, nothing written through a link, no setuid bits. The tree lives in
~/.local/share/sandbox-ai/mcp-rootfs/SHA256/ and is mounted read-only.

  mcp_rootfs.py --internal-bwrap DEFINITION.json   (the bridge's command for each connection: bwrap with its
                                                   arguments through a pipe, and slirp4netns for network "full")

Standard library only (bwrap, and slirp4netns for network "full", on the host).
"""
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOTFS_LIMIT = 2 << 30          # bytes of an archive
UNPACKED_LIMIT = 6 << 30        # bytes of what it unpacks to
MEMBERS_LIMIT = 200000
SHA256 = re.compile(r"[0-9a-f]{64}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def store():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai" / "mcp-rootfs"


def rootfs_dir(sha256):
    if not SHA256.fullmatch(str(sha256)):
        raise ValueError("a rootfs is named by its SHA-256 (64 hex digits)")
    return store() / sha256


def fetch(url, sha256, read=None):
    """The archive at URL (https, or a local file), checked against SHA256; the bytes."""
    if url.startswith("https://"):
        import urllib.request
        read = read or (lambda u: urllib.request.urlopen(u, timeout=120).read(ROOTFS_LIMIT + 1))
        data = read(url)
    elif url.startswith("/") or url.startswith("file://"):
        path = Path(url[len("file://"):] if url.startswith("file://") else url)
        with open(path, "rb") as stream:
            data = stream.read(ROOTFS_LIMIT + 1)
    else:
        raise ValueError(f"{url}: a rootfs comes from an https URL or a local file")
    if len(data) > ROOTFS_LIMIT:
        raise ValueError(f"{url}: larger than {ROOTFS_LIMIT >> 30} GiB")
    found = hashlib.sha256(data).hexdigest()
    if found != sha256:
        raise ValueError(f"{url}: SHA-256 {found}, the signed catalog says {sha256}: not used")
    return data


def safe_name(name):
    """NAME of an archive member as a relative path inside the tree, or ValueError."""
    # Every root goes: POSIX keeps a leading "//" as a root of its own ("//etc/x" has the parts "//", "etc", "x"),
    # which joined to the tree would name the host's /etc/x itself.
    path = PurePosixPath(str(name).lstrip("/"))
    parts = [p for p in path.parts if p not in ("", ".")]
    if path.is_absolute() or any(p == ".." or "\0" in p or p.startswith("/") for p in parts):
        raise ValueError(f"archive member {name!r} leaves the tree")
    return PurePosixPath(*parts) if parts else None


def directory_of(root, relative):
    """ROOT/RELATIVE's parent, created as real directories: never through a link the archive made."""
    if PurePosixPath(relative).is_absolute():  # safe_name gives none; joined, it would replace ROOT
        raise ValueError(f"archive member {relative} leaves the tree")
    current = root
    for part in relative.parent.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"archive member {relative} lies below {current.relative_to(root)}, which is no "
                             "directory (a link?)")
    return current


def unpack(data, target):
    """DATA (a tar archive, gzip or not) into TARGET (made new), without trusting it; the tree read-only."""
    if target.exists():
        raise ValueError(f"{target} exists")
    work = Path(tempfile.mkdtemp(prefix=".unpack-", dir=target.parent))
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > MEMBERS_LIMIT:
                raise ValueError(f"the archive has more than {MEMBERS_LIMIT} entries")
            for member in members:
                relative = safe_name(member.name)
                if relative is None:
                    continue
                parent = directory_of(work, relative)
                path = parent / relative.name
                mode = member.mode & 0o755  # no setuid, setgid, sticky or world-write bits
                if member.isdir():
                    try:
                        os.mkdir(path, 0o755)
                    except FileExistsError:
                        if not stat.S_ISDIR(os.lstat(path).st_mode):
                            raise ValueError(f"archive member {relative}: a directory where something else is")
                elif member.isfile():
                    total += member.size
                    if total > UNPACKED_LIMIT:
                        raise ValueError(f"the archive unpacks to more than {UNPACKED_LIMIT >> 30} GiB")
                    source = archive.extractfile(member)
                    with contextlib_suppress_exists(path):
                        os.unlink(path)  # a later entry of the same name replaces the earlier one
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        shutil.copyfileobj(source, stream)
                    os.chmod(path, mode | 0o400)
                elif member.issym():
                    with contextlib_suppress_exists(path):
                        os.unlink(path)
                    os.symlink(member.linkname, path)  # resolved inside the sandbox's root, never here
                elif member.islnk():
                    other = safe_name(member.linkname)
                    if other is None:
                        raise ValueError(f"archive member {relative}: a hard link to nothing")
                    source_path = work / other
                    if os.path.islink(source_path) or not source_path.is_file() or \
                            any(os.path.islink(work / Path(*other.parts[:i])) for i in range(1, len(other.parts))):
                        raise ValueError(f"archive member {relative}: a hard link to {other}, which is no file of it")
                    with contextlib_suppress_exists(path):
                        os.unlink(path)
                    os.link(source_path, path)
                # devices, FIFOs and anything else are left out: /dev comes from bwrap
        for directory, dirs, files in os.walk(work):  # read-only from now on (bwrap mounts it read-only too)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    os.chmod(path, os.lstat(path).st_mode & 0o555)
        os.replace(work, target)
        for directory, dirs, _ in os.walk(target):
            for name in dirs:
                path = Path(directory) / name
                if not path.is_symlink():
                    os.chmod(path, 0o555)
        os.chmod(target, 0o555)
    except BaseException:
        remove(work)
        raise
    return target


class contextlib_suppress_exists:
    """Suppress FileNotFoundError for an unlink of a path that may not be there; refuse a directory."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        if os.path.lexists(self.path) and stat.S_ISDIR(os.lstat(self.path).st_mode) and not os.path.islink(self.path):
            raise ValueError(f"archive member {self.path.name}: a file where a directory is")

    def __exit__(self, kind, value, trace):
        return kind is FileNotFoundError


def remove(path):
    """A tree of read-only directories, removed."""
    if not os.path.lexists(path):
        return
    for directory, dirs, _ in os.walk(path):
        for name in dirs:
            child = Path(directory) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
    os.chmod(path, 0o700)
    shutil.rmtree(path)


def install(url, sha256, read=None):
    """The checked, unpacked tree of the archive URL with SHA256 (kept: a second server or add of it is free)."""
    target = rootfs_dir(sha256)
    if target.is_dir():
        return target
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return unpack(fetch(url, sha256, read), target)


def validated(rootfs, where):
    """The "rootfs" of a definition: {"path", "sha256", "entrypoint", "user", "workdir", "env"}, every value checked."""
    if not isinstance(rootfs, dict) or set(rootfs) - {"path", "sha256", "entrypoint", "user", "workdir", "env"}:
        raise ValueError(f"{where}: rootfs is {{path, sha256, entrypoint, user, workdir, env}}")
    if not SHA256.fullmatch(str(rootfs.get("sha256"))):
        raise ValueError(f"{where}: rootfs sha256 must be 64 hex digits")
    if rootfs.get("path") != str(rootfs_dir(rootfs["sha256"])):
        raise ValueError(f"{where}: rootfs path must be its place in {store()}")
    entrypoint = rootfs.get("entrypoint")
    if not isinstance(entrypoint, list) or not entrypoint or len(entrypoint) > 32 or not all(
            isinstance(v, str) and v and "\0" not in v and len(v) < 4096 for v in entrypoint) or \
            not entrypoint[0].startswith("/"):
        raise ValueError(f"{where}: rootfs entrypoint must be an absolute program and its arguments")
    user = rootfs.get("user", 65534)
    if isinstance(user, bool) or not isinstance(user, int) or not 1 <= user <= 65535:
        raise ValueError(f"{where}: rootfs user must be a UID from 1 to 65535 (never root)")
    workdir = rootfs.get("workdir", "/tmp")
    if not isinstance(workdir, str) or not workdir.startswith("/") or ".." in workdir.split("/"):
        raise ValueError(f"{where}: rootfs workdir must be an absolute path")
    env = rootfs.get("env", {})
    if not isinstance(env, dict) or len(env) > 32 or not all(
            isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(v, str) and "\0" not in v and len(v) < 4096
            for k, v in env.items()):
        raise ValueError(f"{where}: rootfs env must map variable names to text")
    return rootfs


def limits_prefix(definition, launcher):
    """systemd-run --user --scope with the memory, CPU and process limits, as the Bubblewrap agents get them, or []. The
    CPU limit only where the cpu controller is delegated to the user (as the launcher's limits_prefix)."""
    if not shutil.which("systemd-run") or not launcher.user_manager_running():
        return []
    memory = str(definition.get("memory", "512m")).upper()
    cpus = definition.get("cpus")
    cpus = float(cpus) if cpus is not None else max(1, (os.cpu_count() or 1) // 2)
    controllers = launcher.delegated_controllers() if hasattr(launcher, "delegated_controllers") else None
    quota = ["-p", f"CPUQuota={int(round(cpus * 100))}%"] if controllers is None or "cpu" in controllers else []
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", f"MemoryMax={memory}",
            "-p", f"MemorySwapMax={memory}", *quota, "-p", f"TasksMax={definition.get('pids', 128)}", "--"]


def tmp_bytes(size):
    """Bytes of the server's /tmp size ("64m", "1g"; mcp.py checked it): 64 MiB for anything else."""
    match = re.fullmatch(r"([1-9][0-9]{0,5})([mg])", str(size).strip().lower())
    return int(match[1]) << (30 if match[2] == "g" else 20) if match else 64 << 20


def bwrap_args(definition, environ=None):
    """bwrap's arguments for one instance of a rootfs server, hardened as image_command. They go to bwrap through a
    pipe (--args FD), never on a command line: a variable's value (a secret) would show in /proc/PID/cmdline."""
    environ = os.environ if environ is None else environ
    rootfs = definition["rootfs"]
    user = str(rootfs.get("user", 65534))
    args = ["--die-with-parent", "--new-session", "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
            "--unshare-cgroup-try", "--unshare-net", "--uid", user, "--gid", user,
            "--ro-bind", rootfs["path"], "/", "--proc", "/proc", "--dev", "/dev",
            "--size", str(tmp_bytes(definition.get("tmp", "64m"))), "--tmpfs", "/tmp", "--cap-drop", "ALL",
            "--clearenv"]
    if definition.get("network", "none") == "full":
        # The resolver slirp4netns answers at (10.0.2.3), as pasta gives the image servers theirs.
        args += ["--ro-bind", resolv_conf(), "/etc/resolv.conf"]
    for mount in definition.get("mounts", []):
        host = str(Path(os.path.expanduser(mount["host"])).resolve())
        args += ["--bind" if mount.get("mode") == "rw" else "--ro-bind", host, mount["target"]]
    for key, value in sorted(rootfs.get("env", {}).items()):
        args += ["--setenv", key, value]
    for name in definition.get("env", []):  # from the bridge's environment, as podman's --env=NAME
        if name in environ:
            args += ["--setenv", name, environ[name]]
    # Options only: bwrap reads nothing but options from --args FD - a command after "--" there is dropped, and bwrap
    # without one printed its usage and exited 1 (every rootfs server failed to start). The command follows --args on
    # bwrap's command line (entrypoint(): from the signed rootfs.json, no secret in it).
    return [*args, "--chdir", rootfs.get("workdir", "/tmp")]


def entrypoint(definition):
    """The server's command, for bwrap's command line after --args FD and "--"."""
    return ["--", *definition["rootfs"]["entrypoint"]]


def bwrap_command(definition_file, definition, launcher=None):
    """The bridge's command for one connection: this file's wrapper reads DEFINITION_FILE (the checked definition,
    in the server's state directory) and starts bwrap; in a systemd scope with the limits when there is one."""
    prefix = limits_prefix(definition, launcher) if launcher is not None else []
    return [*prefix, sys.executable, "-I", str(Path(__file__).resolve()), "--internal-bwrap", str(definition_file)]


def resolv_conf():
    """A resolv.conf naming slirp4netns's DNS forwarder, written once for every server."""
    path = store() / "resolv.conf"
    if not path.is_file() or path.read_text() != "nameserver 10.0.2.3\n":
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text("nameserver 10.0.2.3\n")
        path.chmod(0o644)
    return str(path)


def args_pipe(args):
    """A pipe's read end holding ARGS NUL-separated, for bwrap --args."""
    read, write = os.pipe()
    data = b"".join(os.fsencode(arg) + b"\0" for arg in args)
    if len(data) > 60000:  # the pipe's buffer; a definition never comes near it
        raise ValueError("the bwrap arguments are too long")
    os.write(write, data)
    os.close(write)
    os.set_inheritable(read, True)  # bwrap reads it after exec
    return read


def run(definition, environ=None):
    """One instance: bwrap with the arguments through --args; for network "full" with slirp4netns attached to its
    network namespace (the outside network, never the host's loopback). Its stdin and stdout stay the MCP
    connection the bridge gave this process. Returns bwrap's exit status."""
    import json
    args = args_pipe(bwrap_args(definition, environ))
    if definition.get("network", "none") != "full":
        os.execvp("bwrap", ["bwrap", "--args", str(args), *entrypoint(definition)])
    info_r, info_w = os.pipe()
    ready_r, ready_w = os.pipe()
    child = subprocess.Popen(["bwrap", "--info-fd", str(info_w), "--args", str(args), *entrypoint(definition)],
                             pass_fds=(info_w, args))
    os.close(info_w)
    os.close(args)
    slirp = None
    try:
        with os.fdopen(info_r) as stream:
            pid = int(json.loads(stream.readline() or "{}")["child-pid"])
        slirp = subprocess.Popen(["slirp4netns", "--configure", "--disable-host-loopback", "--enable-sandbox",
                                  "--enable-seccomp", "--mtu=65520", "--ready-fd", str(ready_w), str(pid), "tap0"],
                                 pass_fds=(ready_w,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        os.close(ready_w)
        ready_w = None
        os.read(ready_r, 1)
        return child.wait()
    except (OSError, ValueError, KeyError):
        child.kill()
        child.wait()
        return 125
    finally:
        for fd in (ready_r, ready_w):
            if fd is not None:
                os.close(fd)
        if slirp is not None:
            slirp.terminate()
            slirp.wait()


def main(argv):
    if len(argv) == 2 and argv[0] == "--internal-bwrap":
        import json
        definition = json.loads(Path(argv[1]).read_text())
        validated(definition["rootfs"], argv[1])
        return run(definition)
    print("mcp_rootfs.py is used by sandbox-ai mcp (see its docstring)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
