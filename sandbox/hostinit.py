#!/usr/bin/env python3
"""Whether Podman's --init works on this host, what a container gets instead, and the host's package command.

Podman's --init does not take an init from the image: it mounts the HOST's catatonit into the container as its PID 1.
A host without that package cannot start such a container ("lookup init binary: exec: "catatonit": executable file
not found in $PATH"), which is why sandbox-ai does not depend on it where it need not:

  sandbox-ai's own containers (the browser's, Cairn's)  never --init: their PID 1 is sandbox-ai's own init, pid1.py,
      run by the Python of their image and mounted read-only beside their runtime. A --service of sandbox-ai's own
      Containerfiles (service-images/) neither: its program is its own PID 1 (services.OWN_INIT: nginx's master).
  an image that is not sandbox-ai's (an MCP image server, a --service)  --init where the host has the init binary,
      found as Podman finds it (init_binary). Without it: no --init where the container has an init of its own - an
      MCP server whose definition says "pid1": "server" (it reaps its children itself), an image whose ENTRYPOINT is
      an init (tini, dumb-init, catatonit: image_init) - and otherwise it is not started: said plainly, with the
      package command of this distribution (missing_text). Never a silently weaker start: without an init as PID 1
      nothing reaps what the server starts, and an MCP server's own process would hold the MCP pipes as PID 1.

SANDBOX_AI_INIT_BINARY=PATH: where Podman's init binary is when sandbox-ai does not find it (taken as given);
=none: none of the host's is used. Standard library only.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

INIT_NAME = "catatonit"
VARIABLE = "SANDBOX_AI_INIT_BINARY"
DEFAULT_INIT = "usr/libexec/podman/catatonit"  # Podman's old default, still looked at first
# Podman's helper_binaries_dir defaults (containers.conf), in its order; CONTAINERS_HELPER_BINARY_DIR goes first.
HELPER_DIRS = ("usr/local/libexec/podman", "usr/local/lib/podman", "usr/libexec/podman", "usr/lib/podman")
# An init as an image's ENTRYPOINT (its first word's name): such an image needs no --init.
IMAGE_INITS = ("tini", "tini-static", "dumb-init", "catatonit", "docker-init")
INIT_PATH = re.compile(r'^\s*init_path\s*=\s*(["\'])(?P<path>.+?)\1\s*(?:#.*)?$', re.MULTILINE)


def conf_files(environ=None, root="/", home=None):
    """The containers.conf files Podman reads here, the one that wins last: the distribution's, the system's, their
    drop-ins, yours ($XDG_CONFIG_HOME/containers), and CONTAINERS_CONF alone when it is set."""
    environ = os.environ if environ is None else environ
    if environ.get("CONTAINERS_CONF"):
        return [Path(environ["CONTAINERS_CONF"])]
    root = Path(root)
    home = Path(home) if home is not None else Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    found = [root / "usr/share/containers/containers.conf", root / "etc/containers/containers.conf"]

    def drop_ins(folder):
        try:
            return sorted(path for path in folder.iterdir() if path.name.endswith(".conf"))
        except OSError:
            return []
    return [*found, *drop_ins(root / "etc/containers/containers.conf.d"), home / "containers/containers.conf",
            *drop_ins(home / "containers/containers.conf.d")]


def conf_init_path(environ=None, root="/", home=None):
    """init_path of [engine] in the containers.conf files (the last one that sets it), or None."""
    chosen = None
    for path in conf_files(environ, root, home):
        try:
            text = path.read_text(errors="replace")[:1 << 20]
        except OSError:
            continue
        for match in INIT_PATH.finditer(text):
            chosen = match["path"]
    return chosen


def init_binary(environ=None, root="/", which=shutil.which, home=None):
    """The init binary Podman's --init would use on this host, found as Podman finds it - init_path of
    containers.conf, /usr/libexec/podman/catatonit, CONTAINERS_HELPER_BINARY_DIR, its helper folders (/usr/local/
    libexec/podman, /usr/local/lib/podman, /usr/libexec/podman, /usr/lib/podman), then catatonit on PATH - or None
    when there is none (SANDBOX_AI_INIT_BINARY: the module's docstring)."""
    environ = os.environ if environ is None else environ
    given = environ.get(VARIABLE)
    if given:
        return None if given == "none" else given
    root = Path(root)
    configured = conf_init_path(environ, root, home)
    if configured:  # (Podman takes it as it is, without looking whether it is there)
        return configured
    candidates = [root / DEFAULT_INIT]
    if environ.get("CONTAINERS_HELPER_BINARY_DIR"):
        candidates.append(Path(environ["CONTAINERS_HELPER_BINARY_DIR"]) / INIT_NAME)
    candidates += [root / folder / INIT_NAME for folder in HELPER_DIRS]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return which(INIT_NAME)


def image_init(entrypoint):
    """The init that is the image's own ENTRYPOINT (its first word: tini, dumb-init ...), or None."""
    if not isinstance(entrypoint, (list, tuple)) or not entrypoint or not isinstance(entrypoint[0], str):
        return None
    name = entrypoint[0].rpartition("/")[2]
    return name if name in IMAGE_INITS else None


def image_entrypoint(image, podman=("podman",), run=subprocess.run):
    """The ENTRYPOINT of IMAGE as Podman has it (a list), or None when it cannot be read."""
    try:
        done = run([*podman, "image", "inspect", "--format", "{{json .Config.Entrypoint}}", image],
                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        found = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        return None
    # (an older Podman gives it as one string, its words joined)
    return found if isinstance(found, list) else found.split() if isinstance(found, str) and found.strip() else None


# --- the host's packages ---------------------------------------------------------------------------------------

# tool: its package (one name everywhere, or {package manager: package}; "": none there)
PACKAGES = {
    "catatonit": {"brew": ""}, "podman": "podman", "pasta": "passt", "tmux": "tmux", "slirp4netns": "slirp4netns",
    "sshfs": {"dnf": "fuse-sshfs", "brew": ""}, "bwrap": "bubblewrap", "firejail": "firejail", "git": "git",
    "nft": "nftables", "nsenter": "util-linux", "setpriv": "util-linux",
    "newuidmap": {"apt": "uidmap", "dnf": "shadow-utils", "pacman": "shadow", "zypper": "shadow",
                  "apk": "shadow-uidmap", "brew": ""},
    "newgidmap": {"apt": "uidmap", "dnf": "shadow-utils", "pacman": "shadow", "zypper": "shadow",
                  "apk": "shadow-uidmap", "brew": ""},
}
MANAGERS = (("apt", ("debian", "ubuntu"), ["sudo", "apt", "install", "-y"]),
            ("dnf", ("fedora", "rhel", "centos"), ["sudo", "dnf", "install", "-y"]),
            ("pacman", ("arch",), ["sudo", "pacman", "-S", "--needed"]),
            ("zypper", ("suse", "opensuse"), ["sudo", "zypper", "install"]),
            ("apk", ("alpine",), ["sudo", "apk", "add"]))


def package_manager(os_release="/etc/os-release", platform=None, which=shutil.which):
    """(name, the words of its install command) of this host's package manager: by /etc/os-release (ID, ID_LIKE),
    else the first one on PATH; brew on a Mac; (None, []) when none is known."""
    if (platform or sys.platform) == "darwin":
        return "brew", ["brew", "install"]
    ids = []
    try:
        for line in Path(os_release).read_text(errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key in ("ID", "ID_LIKE"):
                ids += value.strip().strip('"\'').lower().split()
    except OSError:
        pass
    for name, families, words in MANAGERS:
        if any(family in one for one in ids for family in families):
            return name, words
    for name, _, words in MANAGERS:
        if which("apt-get" if name == "apt" else name):
            return name, words
    return None, []


def install_command(tools, manager=None):
    """The command that installs the host tools TOOLS (their commands' names) with this distribution's package
    manager, as its words - [] when the manager is not known or has no such package. It is shown, never run: it
    needs root, and sandbox-ai types no sudo for you."""
    name, words = manager if manager is not None else package_manager()
    packages = []
    for tool in tools:
        package = PACKAGES.get(tool, tool)
        if isinstance(package, dict):
            package = package.get(name, tool)
        if package and package not in packages:
            packages.append(package)
    return [*words, *packages] if words and packages else []


MISSING = "needs an init process as its PID 1,"  # how missing_text begins after its subject (a start reads it)


def missing_text(what, manager=None):
    """What a start is told when WHAT (an MCP server, a service) needs Podman's --init and this host has no init
    binary: what is missing, why it is needed, and the command that installs it."""
    command = install_command([INIT_NAME], manager)
    how = f"install it: {shlex.join(command)}" if command else \
        f"install the package {INIT_NAME} with your distribution's package manager"
    return (f"{what} {MISSING} which Podman's --init takes from this host: its init binary "
            f"{INIT_NAME} is not installed here, and the image brings no init of its own. Without one nothing reaps "
            f"what it starts, so it is not started: {how} (sandbox-ai never runs sudo for you; sandbox-ai doctor "
            "checks it)")


def wants_init(what, image=None, own_pid1=False, local=True, podman=("podman",), run=subprocess.run, found=None):
    """Whether the container of WHAT is started with Podman's --init: True where the engine has its init binary (a
    remote engine or podman machine's, LOCAL False, is not this host's to check: taken as there). False where it
    needs none of the host's - OWN_PID1 (it is its own PID 1 by its definition), or its IMAGE's ENTRYPOINT is an init.
    ValueError (missing_text) when the host has none and the image brings none. FOUND: init_binary()'s answer, when
    it was asked already."""
    if own_pid1:
        return False
    if not local or (found if found is not None else init_binary()):
        return True
    if image and image_init(image_entrypoint(image, podman, run)):
        return False
    raise ValueError(missing_text(what))
