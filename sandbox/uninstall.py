#!/usr/bin/env python3
"""sandbox-ai uninstall: remove this installation; with --purge also the data sandbox-ai keeps for you.

  sandbox-ai uninstall [--dry-run] [--yes]                 the installation, as install.sh --uninstall removes it
  sandbox-ai uninstall --purge [--secrets] [--mcp-data] [--stop-running] [--dry-run] [--yes]

First a table of what goes, where and how much space it takes, then a question (y/N, typed; --yes answers it for a
script, and without a terminal nothing is removed unless --yes); --dry-run shows the table only.

  plain      the installation this command runs from (your own, or a system one: as root), its command links, the
             PATH block in ~/.bashrc and ~/.zshrc, the Tab completion, the menu entry and the app's login entry of
             it - exactly what install.sh --uninstall removes (install.sh lists them: --uninstall --dry-run); first
             of all the schedules' timer (sandbox-ai schedule enable's systemd user timer or LaunchAgent) and the
             update timer (sandbox-ai update mode's) are turned off and their files removed - only those sandbox-ai
             wrote; a release update mode download fetched goes with the installation
  --purge    also sandbox-ai's own data of this user: the kept starts with their HOMEs, logs and state
             (~/.local/state/sandbox-ai), kits, plugins and the other data (~/.local/share/sandbox-ai), the settings
             (~/.config/sandbox-ai), the caches and run directories, the web interface's files, the app's login
             entry, and sandbox-ai's own Podman images, volumes, networks and stopped containers (those it built or
             labelled: never another's)
  --secrets  with --purge, also the secrets and logins, which cannot be recovered: sandbox-ai's entries in the
             keyring (macOS: the Keychain) and its file store, the Git and registry logins, the agents' logins
             (sandbox-ai login), the VPN profiles and keys, the app's login browser
  --mcp-data with --purge, also the MCP servers' own folders (~/.local/share/sandbox-ai/mcp-folders, which
             `mcp add` makes for a server's folders: a memory server's graph, its uploads); kept otherwise

Never touched: your workspaces and the folders you mounted, anything outside sandbox-ai's own folders (one of its
folders that holds a mounted folder, or is a link elsewhere, is left and named), other users' data, and other hosts
(sandbox-ai hosts: each keeps its installation - uninstall there). Running starts are refused, with their names,
unless --stop-running stops them first (as sandbox-ai stop does). A copy that install.sh did not install - pip,
pipx, the AppImage, the macOS app, sandbox-ai.pyz - is not this command's to remove: plain, it says how to remove it
and removes nothing; with --purge it removes the data as above (an install.sh installation beside it stays) and
then says exactly how to remove the program itself (`pip uninstall sandbox-ai-app`, the AppImage's or .pyz's path
and the links to it). A git checkout or a loose folder says what to do instead and removes nothing.

Standard library only.
"""
import argparse
import contextlib
import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent
HOMES = ("/home/", "/root/", "/Users/", "/var/home/")  # (main.HOMES: where a system prefix never is)
# What install.sh --uninstall removes in its data folder (install.sh's UNINSTALL_FILES, and .install-*): never purged
# here, install.sh takes them.
INSTALL_NAMES = ("versions", "current", "app-env", "release-state.json")
# The secrets and logins below sandbox-ai's folders: kept unless --secrets.
SECRET_PATHS = {"data": ("secrets", "auth", "vpn", "app/login-browser"),
                "config": ("git-logins.json", "publish-logins.json", "vpn")}
# The MCP servers' own folders (`mcp add` makes one per folder a server keeps: a memory server's graph, its uploads):
# the user's data, kept unless --mcp-data.
MCP_DATA_PATHS = {"data": ("mcp-folders",)}
INSTALL_TITLES = {"link": "Command links", "installation": "The installation", "menu": "Menu entry",
                  "login": "The app's login entry", "rc": "PATH block", "completion": "Tab completion"}
PACKAGE_HOW = {"pyz": "this sandbox-ai runs from sandbox-ai.pyz: delete that file to uninstall it",
               "appimage": "this sandbox-ai runs from the AppImage (sandbox-ai-ARCH.AppImage): delete that file to "
                           "uninstall it",
               "macos-app": "this sandbox-ai runs from the macOS app: move sandbox-ai.app to the Trash to uninstall "
                            "it"}
# Copies install.sh did not install whose data --purge removes all the same (the program itself stays: said how).
PURGEABLE = ("pip", "pipx", "package")
PROGRAM_NAMES = ("sandbox-ai", "sandbox-ai-app")  # the links to a package a human may have made (AppRun's example)
OWN_VOLUMES = ("sandbox-ai-cairn-",)  # volumes sandbox-ai names without a label (cairn.relay_volume)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
RUNNING_STATES = ("running", "paused", "orphaned")
LOGIN_ENTRY = {"linux": ("autostart", "sandbox-ai-app.desktop"), "darwin": ("LaunchAgents", "org.sandbox-ai.app.plist")}
_modules = {}


def module(name):
    """sandbox-ai's module NAME, loaded once by path (python -I: the script's folder is not on sys.path)."""
    if name not in _modules:
        if name == "starts":  # (it loads net, mcp, sessions ... itself, one copy each)
            spec = importlib.util.spec_from_file_location("sandbox_ai_starts_for_uninstall", HERE / "starts.py")
        else:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_uninstall", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def textout():
    loaded = sys.modules.get("sandbox_ai_textout")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_textout", HERE / "textout.py")
        made = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(made)
        loaded = sys.modules.setdefault("sandbox_ai_textout", made)
    return loaded


def launch_log():
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        made = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(made)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", made)
    return loaded


def say(text):
    """One message of this command on standard error."""
    print(textout().message(f"sandbox-ai uninstall: {text}", stream=sys.stderr), file=sys.stderr)


# --- which copy runs ---------------------------------------------------------------------------------------------

def links_into(link, folder):
    try:
        return link.is_symlink() and os.readlink(link).startswith(str(folder) + os.sep)
    except OSError:
        return False


def program_links(program, environ):
    """The links named sandbox-ai or sandbox-ai-app on PATH and in ~/.local/bin that lead to PROGRAM."""
    home = environ.get("HOME") or ""
    folders = [folder for folder in (environ.get("PATH") or "").split(os.pathsep) if os.path.isabs(folder)]
    if os.path.isabs(home):
        folders.append(os.path.join(home, ".local", "bin"))
    found = []
    for folder in dict.fromkeys(folders):
        for name in PROGRAM_NAMES:
            link = os.path.join(folder, name)
            with contextlib.suppress(OSError):
                if os.path.islink(link) and os.path.realpath(link) == program and link not in found:
                    found.append(link)
    return found


def package_how(packaged, environ):
    """(how to remove the package PACKAGED, its path or None): the path its start named (SANDBOX_AI_PACKAGE, set by
    scripts/dist_boot.py), when it is there, with the links to it - only said, never removed here."""
    named = environ.get("SANDBOX_AI_PACKAGE") or ""
    program = os.path.realpath(named) if os.path.isabs(named) else None
    if program and packaged == "macos-app":
        program = program if program.endswith(".app") and os.path.isdir(program) else None
    elif program and not os.path.isfile(program):
        program = None
    if program is None:
        return PACKAGE_HOW[packaged], None
    links = program_links(program, environ)
    if packaged == "macos-app":
        return (f"this sandbox-ai runs from {program} (the macOS app): move it to the Trash to uninstall it"
                + (" and remove the links to it: rm " + " ".join(map(shlex.quote, links)) if links else "")), program
    what = "sandbox-ai.pyz" if packaged == "pyz" else "the AppImage"
    return (f"this sandbox-ai runs from {program} ({what}): delete that file to uninstall it: rm "
            + " ".join(shlex.quote(path) for path in [program, *links])), program


def copy_kind(here=HERE, euid=None, writable=None, prefix=None, environ=None):
    """What this copy of sandbox-ai is: {"kind": "user" | "system" (install.sh's installations: DATA/versions/X) |
    "checkout" | "pip" | "pipx" | "package" | "folder", "data": install.sh's data folder, "prefix": a system one's,
    "tree": the copy's folder, "how": what to do instead (the kinds this command does not remove), "program": the
    package file (the .pyz, the AppImage, the macOS app) when its start named it, "refused": why a system
    installation is not removed by this user}."""
    here = Path(here)
    tree = here.parent
    euid = os.geteuid() if euid is None else euid
    writable = writable or (lambda path: os.access(path, os.W_OK))
    prefix = sys.prefix if prefix is None else prefix
    environ = os.environ if environ is None else environ
    copy = {"kind": None, "data": None, "prefix": None, "tree": str(tree), "how": None, "program": None,
            "refused": None}
    try:
        packaged = (tree / "PACKAGING").read_text().strip()
    except (OSError, UnicodeDecodeError):
        packaged = None
    if packaged in PACKAGE_HOW:
        how, program = package_how(packaged, environ)
        copy.update(kind="package", how=how, program=program)
    elif here.name == "sandbox_ai" or not (tree / "install.sh").is_file():
        tool = "pipx" if "pipx" in Path(prefix).parts else "pip"
        python = Path(prefix) / "bin" / "python"
        command = f"{shlex.quote(str(python))} -m pip" if tool == "pip" and python.is_file() else tool
        copy.update(kind=tool, how=f"this sandbox-ai was installed with {tool} ({here}): {command} uninstall "
                                   "sandbox-ai-app")
    elif (tree / ".git").exists():
        copy.update(kind="checkout", how=f"this sandbox-ai runs from a git checkout ({tree}), not from an "
                                         "installation: there is nothing to uninstall; delete the checkout yourself "
                                         "when you no longer want it")
    elif tree.parent.name != "versions" or tree.parent.parent.name != "sandbox-ai":
        copy.update(kind="folder", how=f"this sandbox-ai runs from a folder install.sh did not install ({tree}): "
                                       "delete it yourself when you no longer want it")
    else:
        data = tree.parent.parent
        system = data.parent.name == "lib" and (links_into(data.parent.parent / "bin" / "sandbox-ai", data)
                                                or not str(data).startswith(HOMES))
        copy.update(kind="system" if system else "user", data=str(data))
        if system:
            copy["prefix"] = str(data.parent.parent)
            if euid != 0 and not writable(data):
                copy["refused"] = (f"this is a system installation ({data}): uninstall it as root: sudo sandbox-ai "
                                   "uninstall")
    return copy


def installer_args(copy):
    return ["--system", "--prefix", copy["prefix"]] if copy["kind"] == "system" else ["--user"]


def installer(copy):
    """[bash, the copy's own install.sh, its kind of installation], and the environment it runs in: a user's
    installation with XDG_DATA_HOME where it is (install.sh finds it there)."""
    env = dict(os.environ)
    if copy["kind"] == "user":
        env["XDG_DATA_HOME"] = str(Path(copy["data"]).parent)
    bash = shutil.which("bash") or "/bin/bash"  # (install.sh runs in macOS's bash 3.2 too)
    return [bash, str(Path(copy["tree"]) / "install.sh"), "--uninstall", *installer_args(copy)], env


def installer_targets(copy, run=subprocess.run):
    """[(kind, path)] of what install.sh --uninstall removes (its --dry-run): the one list, install.sh's."""
    argv, env = installer(copy)
    result = run([*argv, "--dry-run"], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=120)
    if result.returncode != 0:
        said = (result.stderr or "").strip().splitlines()
        raise ValueError(f"install.sh --uninstall --dry-run failed: {said[-1] if said else result.returncode}")
    return [tuple(line.split("\t", 1)) for line in result.stdout.splitlines() if "\t" in line]


def run_installer(copy):
    """install.sh --uninstall itself (what it says goes to the terminal); its exit status."""
    argv, env = installer(copy)
    return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL).returncode


# --- what --purge takes --------------------------------------------------------------------------------------------

def places(env=None, platform=None, uid=None):
    """[(part, folder)] of every folder sandbox-ai keeps data in for this user, and its login entry's file."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    uid = os.getuid() if uid is None else uid
    home = Path(env.get("HOME") or Path.home())
    cache = Path(env.get("XDG_CACHE_HOME") or home / ".cache")
    found = [("state", Path(env.get("XDG_STATE_HOME") or home / ".local/state") / "sandbox-ai")]
    if env.get("XDG_RUNTIME_DIR"):
        found.append(("runtime", Path(env["XDG_RUNTIME_DIR"]) / "sandbox-ai"))
    found += [("data", Path(env.get("XDG_DATA_HOME") or home / ".local/share") / "sandbox-ai"),
              ("config", Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "sandbox-ai"),
              ("cache", cache / "sandbox-ai"), ("cache", cache / "sandbox-ai-app")]
    if platform == "darwin":
        found.append(("cache", home / "Library" / "Caches" / "sandbox-ai"))  # (hosts.control_folders)
    elif not env.get("XDG_RUNTIME_DIR"):
        found.append(("cache", Path(env.get("TMPDIR") or "/tmp") / f"sandbox-ai-{uid}"))
    return found


def login_entry(env=None, platform=None):
    """The app's login entry (backend.autostart_file): a LaunchAgent on macOS, an autostart desktop entry elsewhere."""
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or Path.home())
    if (platform or sys.platform) == "darwin":
        return home / "Library" / "LaunchAgents" / LOGIN_ENTRY["darwin"][1]
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "autostart" / LOGIN_ENTRY["linux"][1]


GROUPS = {
    ("state", "sessions"): "Kept starts and their HOMEs", ("state", "logs"): "Logs",
    ("state", "web"): "The web interface's files", ("state", ""): "State: histories, MCP servers, launcher.log",
    ("runtime", ""): "Run directories",
    ("data", "kits"): "Kits, plugins and default kits", ("data", "home"): "Kits, plugins and default kits",
    ("data", "plugins"): "Kits, plugins and default kits", ("data", "web-vendor"): "The web interface's files",
    ("data", "secrets"): "Secrets (file store)", ("data", "auth"): "Agent logins (sandbox-ai login)",
    ("data", "vpn"): "VPN profiles and keys", ("data", "app/login-browser"): "The app's login browser",
    ("data", "mcp-folders"): "The MCP servers' own folders",
    ("data", ""): "Data: MCP root file systems, the app's files",
    ("config", "web"): "The web interface's files", ("config", "git-logins.json"): "Git and registry logins",
    ("config", "publish-logins.json"): "Git and registry logins", ("config", "vpn"): "VPN profiles and keys",
    ("config", ""): "Settings", ("cache", ""): "Caches",
}


def group(part, relative):
    return GROUPS.get((part, relative)) or GROUPS[(part, "")]


def purge_targets(secrets=False, env=None, platform=None, uid=None, mcp_data=False):
    """What --purge removes: {"remove": [{"group", "path", "secret"}], "kept": the secrets and logins that stay
    (without SECRETS), "kept_mcp": the MCP servers' own folders that stay (without MCP_DATA), "links": sandbox-ai's folders that are links elsewhere (left), "roots": the folders removed
    from (only below them is anything removed), "folders": those looked into for a secret in them, "login": the app's
    login entry, when there is one}."""
    found = {"remove": [], "kept": [], "kept_mcp": [], "links": [], "roots": [], "folders": [], "login": None}
    seen = set()
    for part, root in places(env, platform, uid):
        if str(root) in seen:
            continue
        seen.add(str(root))
        if root.is_symlink():
            found["links"].append(str(root))
            continue
        if not root.is_dir():
            continue
        found["roots"].append(str(root))
        keeps = SECRET_PATHS.get(part, ())

        def walk(folder, prefix):
            try:
                children = sorted(os.listdir(folder))
            except OSError:
                return
            for name in children:
                relative = prefix + name
                if part == "data" and not prefix and (name in INSTALL_NAMES or name.startswith(".install-")):
                    continue  # install.sh's
                path = str(Path(folder) / name)
                secret = relative in keeps
                if relative in MCP_DATA_PATHS.get(part, ()) and not mcp_data:
                    found["kept_mcp"].append({"group": group(part, relative), "path": path, "secret": False})
                elif secret and not secrets:
                    found["kept"].append({"group": group(part, relative), "path": path, "secret": True})
                elif any(keep.startswith(relative + "/") for keep in keeps) and os.path.isdir(path) and \
                        not os.path.islink(path):
                    found["folders"].append(path)  # (removed after what is in it, when nothing stayed)
                    walk(path, relative + "/")
                else:
                    found["remove"].append({"group": group(part, relative if secret else relative.split("/")[0]),
                                            "path": path, "secret": secret})
        walk(root, "")
    entry = login_entry(env, platform)
    if entry.is_file() or entry.is_symlink():
        found["login"] = str(entry)
        found["remove"].append({"group": "The app's login entry", "path": str(entry), "secret": False, "login": True})
    return found


# --- removing --------------------------------------------------------------------------------------------------------

def mount_points(path="/proc/self/mountinfo"):
    """The mount points this process sees (Linux: mountinfo), or None where that cannot be read (macOS)."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    points = set()
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            points.add(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
    return points


def holds_mount(path, mounts):
    """A mount point at or below PATH (a folder you mounted, a remote folder), or None. MOUNTS: mount_points(); None:
    found by walking PATH (another device below it), never following a link."""
    real = os.path.realpath(path)
    if mounts is not None:
        for point in sorted(mounts):
            if point == real or point.startswith(real.rstrip(os.sep) + os.sep):
                return point
        return None
    top = os.lstat(path).st_dev
    for folder, dirs, _ in os.walk(path):
        for name in dirs:
            child = os.path.join(folder, name)
            with contextlib.suppress(OSError):
                if os.lstat(child).st_dev != top or os.path.ismount(child):
                    return child
    return None


def inside(path, roots):
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    for root in roots:
        root = os.path.realpath(root)
        if parent == root or parent.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def remove_path(path, roots, mounts=None):
    """PATH removed - only below one of ROOTS (sandbox-ai's own folders), a link as a link (never followed), never a
    folder that holds a mounted one; None when it is gone, else why it stays."""
    path = str(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not inside(path, roots):
        return f"{path} is not in sandbox-ai's folders: left as it is"
    if stat.S_ISLNK(info.st_mode):
        os.unlink(path)
        return None
    if mounts is None:
        mounts = mount_points()
    mounted = holds_mount(path, mounts)
    if mounted:
        return f"{path} holds a mounted folder ({mounted}): left as it is (unmount it, then remove it yourself)"
    try:
        if stat.S_ISDIR(info.st_mode):
            module("statedirs").remove_tree(path)
        else:
            os.unlink(path)
    except OSError as exc:
        return f"{path}: {exc}"
    return f"{path} could not be removed completely" if os.path.lexists(path) else None


# --- Podman: sandbox-ai's own -----------------------------------------------------------------------------------------

def labelled(labels):
    return isinstance(labels, dict) and any(isinstance(key, str) and key.startswith("sandbox-ai.") for key in labels)


def podman_objects(run=subprocess.run):
    """sandbox-ai's own in Podman - those with a label sandbox-ai.* (every image it builds has one; its pods,
    containers and service volumes too) and Cairn's relay volumes: {"images": [{"id", "names", "refs", "ref",
    "bytes"}], "volumes": [{"name", "bytes"}], "networks", "containers" (stopped), "pods" (stopped), "running" (names
    of those that run: they stay)}. None without Podman or an answer from it."""
    if not shutil.which("podman"):
        return None
    sd = module("statedirs")
    images = sd.podman_json(["images", "--all", "--format", "json"], run)
    if not isinstance(images, list):
        return None

    def rows(arguments):
        found = sd.podman_json(arguments, run)
        return [row for row in found if isinstance(row, dict)] if isinstance(found, list) else []
    usage = sd.podman_json(["system", "df", "-v", "--format", "json"], run)
    usage = usage if isinstance(usage, dict) else {}
    unique = {row["ImageID"][:12]: sd.number(row.get("UniqueSize")) for row in usage.get("Images") or []
              if isinstance(row, dict) and isinstance(row.get("ImageID"), str)}
    sizes = {row["VolumeName"]: sd.number(row.get("RawSize", row.get("Size"))) for row in usage.get("Volumes") or []
             if isinstance(row, dict) and isinstance(row.get("VolumeName"), str)}
    found = {"images": [], "volumes": [], "networks": [], "containers": [], "pods": [], "running": []}
    seen = set()
    for row in images:
        ident = row.get("Id") or row.get("ID") if isinstance(row, dict) else None
        if not isinstance(ident, str) or not re.fullmatch(r"(sha256:)?[0-9a-f]{12,64}", ident) or \
                not labelled(row.get("Labels")):
            continue
        ident = ident.split(":")[-1][:12]
        if ident in seen:
            continue
        seen.add(ident)
        names = [name for name in sd.image_names(row) if sd.IMAGE_REF.fullmatch(name)]
        size = unique.get(ident)
        found["images"].append({"id": ident, "names": names, "refs": names or [ident], "ref": (names or [ident])[0],
                                "bytes": size if size is not None else sd.number(row.get("Size")) or 0})
    for row in rows(["ps", "-a", "--format", "json"]):
        ident = row.get("Id") or row.get("ID")
        names = row.get("Names") if isinstance(row.get("Names"), list) else [row.get("Names")]
        name = next((n for n in names if isinstance(n, str)), None)
        if not labelled(row.get("Labels")) or not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{12,64}", ident):
            continue
        if str(row.get("State", "")).lower() in ("running", "paused"):
            found["running"].append(name or ident[:12])
        else:
            found["containers"].append({"id": ident[:12], "name": name})
    for row in rows(["pod", "ps", "--format", "json"]):
        name = row.get("Name")
        if not labelled(row.get("Labels")) or not isinstance(name, str) or not NAME.fullmatch(name):
            continue
        if "running" in str(row.get("Status", "")).lower():
            found["running"].append(name)
        else:
            found["pods"].append({"name": name})
    for row in rows(["volume", "ls", "--format", "json"]):
        name = row.get("Name")
        if isinstance(name, str) and NAME.fullmatch(name) and (labelled(row.get("Labels"))
                                                                or name.startswith(OWN_VOLUMES)):
            found["volumes"].append({"name": name, "bytes": sizes.get(name)})
    for row in rows(["network", "ls", "--format", "json"]):
        name = row.get("Name") or row.get("name")
        if isinstance(name, str) and NAME.fullmatch(name) and labelled(row.get("Labels") or row.get("labels")):
            found["networks"].append({"name": name})
    return found


def remove_podman(found, run=subprocess.run):
    """Remove FOUND (podman_objects) - the stopped containers and pods, then the images (those built on another
    after it: again while that frees more), the volumes and networks - never forced: what something else still uses
    stays. {what: why} of what stayed."""
    stayed = {}

    def podman(*arguments):
        try:
            result = run(["podman", *arguments], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)[-200:]
        return None if result.returncode == 0 else ((result.stderr or "").strip()[-200:] or f"exit status "
                                                    f"{result.returncode}")
    for container in found["containers"]:
        why = podman("rm", container["id"])
        if why:
            stayed[container["name"] or container["id"]] = why
    for pod in found["pods"]:
        why = podman("pod", "rm", pod["name"])
        if why:
            stayed[pod["name"]] = why
    pending = [ref for image in found["images"] for ref in image["refs"] if not ref.startswith("-")]
    while pending:
        failed = {}
        for ref in pending:
            why = podman("image", "rm", ref)
            if why:
                failed[ref] = why
        if not failed or len(failed) == len(pending):
            stayed.update(failed)
            break
        pending = list(failed)
    for kind, key in (("volumes", "volume"), ("networks", "network")):
        for item in found[kind]:
            why = podman(key, "rm", item["name"])
            if why:
                stayed[item["name"]] = why
    return stayed


# --- secrets in the keyring ---------------------------------------------------------------------------------------

def keyring_entries():
    """(store, [names]) of sandbox-ai's entries in the keyring (macOS: the Keychain); (None, []) without one."""
    secrets = module("secret_store")
    try:
        if secrets.keychain_usable():
            store = secrets.KeychainStore()
        elif secrets.keyring_usable():
            store = secrets.KeyringStore()
        else:
            return None, []
        return store, store.names()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, []


# --- running starts, other hosts ----------------------------------------------------------------------------------

def running_starts():
    """The starts that run, are paused or run on without their launcher (orphaned), and the MCP servers that run."""
    return [entry for entry in module("starts").entries() if entry.get("state") in RUNNING_STATES]


def stop_running(entries, out=print):
    """Stop ENTRIES as sandbox-ai stop does (the starts: stop --all's path; the MCP servers: mcp stop); the names of
    those that still run afterwards."""
    starts = module("starts")
    if any(entry.get("kind") != "mcp" for entry in entries):
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            # (only what this account's own folders register: the starts listed above)
            starts.stop_all(out, yes=True, inside=False)
    for entry in entries:
        if entry.get("kind") == "mcp":
            name = Path(entry.get("directory") or "").name or str(entry.get("name", ""))[len("mcp-"):]
            try:
                starts.module("mcp").stop(name)
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                say(f"{name}: {exc}")
    try:
        return [entry["name"] for entry in running_starts()]
    except (OSError, ValueError, RuntimeError) as exc:
        return [f"(could not tell: {exc})"]


def host_names():
    """The other hosts of sandbox-ai hosts (each keeps its own installation)."""
    try:
        return [entry["name"] for entry in module("hosts").entries() if entry.get("kind") != "local"]
    except (OSError, ValueError, RuntimeError):
        return []


# --- the plan and its table --------------------------------------------------------------------------------------

def size_of(path):
    return module("statedirs").Scan().size(path)


def plan(copy, args):
    """Everything this run would remove, and what stays: {"install": [(kind, path)], "purge": purge_targets or None,
    "podman", "keyring": (store, names), "hosts"}."""
    found = {"install": installer_targets(copy) if copy["kind"] in ("user", "system") else [], "purge": None,
             "podman": None, "keyring": (None, []), "hosts": host_names(), "timer": schedule_timer(),
             "update_timer": update_timer()}
    if args.purge:
        found["purge"] = purge_targets(secrets=args.secrets, mcp_data=args.mcp_data)
        installed = {path for _, path in found["install"]}
        found["purge"]["remove"] = [target for target in found["purge"]["remove"] if target["path"] not in installed]
        found["podman"] = podman_objects()
        found["keyring"] = keyring_entries()
    return found


# The runner of the schedules' timer commands (systemctl --user, launchctl); None: subprocess.run (tests set one).
TIMER_RUNNER = None


def schedule_timer():
    """The files of the schedules' timer sandbox-ai wrote (a systemd user timer, a LaunchAgent: schedule.timer_files,
    only ours) - turned off and removed first, whatever else stays: it would start this copy's main.py every minute.
    [] when there is none or it cannot be told."""
    try:
        return module("schedule").timer_files()
    except Exception:  # noqa: BLE001 - an older or damaged schedule.py: nothing of it is touched
        return []


def update_timer():
    """The files of the update mode's timer (updates.timer_files: a systemd user timer or a LaunchAgent sandbox-ai
    wrote) - turned off and removed first, like the schedules' one: it would run this copy's update every 15 minutes.
    [] when there is none or it cannot be told."""
    try:
        return module("updates").timer_files()
    except Exception:  # noqa: BLE001 - an older or damaged updates.py: nothing of it is touched
        return []


def turn_off_update_timer():
    """`update mode off`'s part that removes the timer (only sandbox-ai's own files); [] when that failed (said)."""
    try:
        return module("updates").timer_off(run=TIMER_RUNNER or subprocess.run, out=lambda line: None)
    except Exception as exc:  # noqa: BLE001 - said; the rest goes on
        say(f"the update timer could not be turned off ({exc}): sandbox-ai update mode off")
        return []


def turn_off_timer(out):
    """`schedule disable` (only sandbox-ai's own files): the files removed; [] when that failed (said)."""
    try:
        return module("schedule").disable(run=TIMER_RUNNER or subprocess.run, out=lambda line: None)
    except Exception as exc:  # noqa: BLE001 - said; the rest goes on
        say(f"the schedules' timer could not be turned off ({exc}): sandbox-ai schedule disable")
        return []


def table_rows(found, secrets):
    """[(what, where, bytes or None, secret)] of the table, one row per kind of thing."""
    short = textout().home_short
    rows, order = [], {}

    def add(what, path, size, secret=False):
        if what not in order:
            order[what] = len(rows)
            rows.append([what, [], 0, secret, False])
        row = rows[order[what]]
        row[1].append(path)
        if size is None:
            row[4] = True
        else:
            row[2] += size

    for path in found.get("timer") or []:
        add("The schedules' timer (turned off first)", path, size_of(path))
    for path in found.get("update_timer") or []:
        add("The update timer (turned off first)", path, size_of(path))
    for kind, path in found["install"]:
        add(INSTALL_TITLES.get(kind, kind), path, None if kind == "rc" else size_of(path))
    purge = found["purge"]
    if purge:
        for target in purge["remove"]:
            add(target["group"], target["path"], size_of(target["path"]), target["secret"])
        store, names = found["keyring"]
        if secrets and names:
            for name in names:
                add(f"Secrets in the {store.name}", f"{store.name}: {name}", None, True)
        podman = found["podman"] or {}
        for image in podman.get("images", []):
            add("Podman images (sandbox-ai's)", image["ref"], image["bytes"])
        for volume in podman.get("volumes", []):
            add("Podman volumes (sandbox-ai's)", volume["name"], volume["bytes"])
        for network in podman.get("networks", []):
            add("Podman networks (sandbox-ai's)", network["name"], None)
        for item in podman.get("containers", []) + podman.get("pods", []):
            add("Podman containers and pods (stopped)", item.get("name") or item.get("id"), None)
    shown = []
    for what, paths, size, secret, unknown in rows:
        shown.append((what, where(paths, short), None if unknown and not size else size, secret))
    return shown


def where(paths, short):
    """PATHS in a few words: each folder once, what goes in it in braces ("~/.local/state/sandbox-ai/{history, mcp}");
    names (Podman's) as they are, the first ones."""
    if not all(os.path.isabs(path) for path in paths):
        return ", ".join(paths[:6]) + (f" and {len(paths) - 6} more" if len(paths) > 6 else "")
    folders = {}
    for path in paths:
        folders.setdefault(os.path.dirname(path), []).append(os.path.basename(path))
    parts = []
    for folder, names in folders.items():
        if len(names) == 1:
            parts.append(short(os.path.join(folder, names[0])))
        else:
            listed = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
            parts.append(f"{short(folder)}/{{{listed}}}")
    return ", ".join(parts)


def lowered(title):
    """A title inside a sentence: "Agent logins" -> "agent logins" ("VPN profiles" stays)."""
    return title[:1].lower() + title[1:] if title[1:2].islower() and not title.startswith("Git ") else title


def show(found, args, copy, running, out, colour):
    t, human = textout(), module("statedirs").human
    rows = table_rows(found, args.secrets)
    total = sum(size or 0 for _, _, size, _ in rows)
    title = "sandbox-ai uninstall " + ("--purge " if args.purge else "") + ("--secrets " if args.secrets else "")
    if copy["kind"] in PURGEABLE:
        about = "sandbox-ai's data of this user; the program stays"
    else:
        about = f"{'your' if copy['kind'] == 'user' else 'the system'} installation in {t.home_short(copy['data'])}"
    t.section(title.strip() + (" would remove" if args.dry_run else " removes"), out, colour, about)
    t.grid(["WHAT", "SIZE", "WHERE"],
           [[t.style(what, "warn", colour) if secret else what, human(size) if size is not None else "-",
             place + (" - cannot be recovered" if secret else "")] for what, place, size, secret in rows],
           right={1}, out=out, colour=colour, empty="nothing", footer=["Total", human(total), ""] if rows else None)
    stays = []
    purge = found["purge"]
    if found.get("timer") and not args.purge:
        stays.append("your schedules (sandbox-ai schedule list): with the timer off none of them starts; --purge "
                     "removes them with the rest of your data")
    if not args.purge:
        stays.append("your data: the kept starts and their HOMEs, logs, kits, settings, caches, secrets and logins, "
                     "sandbox-ai's Podman images (sandbox-ai uninstall --purge removes them; --purge --secrets also "
                     "the secrets and logins)")
    elif not args.secrets:
        kept = list(dict.fromkeys(lowered(target["group"]) for target in purge["kept"]))
        store, names = found["keyring"]
        if names:
            kept.append(f"{len(names)} secret(s) in the {store.name}")
        if kept:
            stays.append("your secrets and logins: " + ", ".join(kept) + " (--secrets with --purge removes them; "
                         "they cannot be recovered)")
    if purge and purge.get("kept_mcp"):
        stays.append("the MCP servers' own folders (" + ", ".join(t.home_short(k["path"]) for k in purge["kept_mcp"])
                     + "): what your servers keep there, a memory server's graph, their uploads (--mcp-data with "
                       "--purge removes them)")
    if purge:
        for link in purge["links"]:
            stays.append(f"{t.home_short(link)}: a link to {os.path.realpath(link)} - left as it is (remove it "
                         "yourself)")
        podman = found["podman"]
        if podman is None:
            stays.append("Podman's images and volumes: Podman is not installed or did not answer - nothing of it is "
                         "touched")
        elif podman["running"]:
            stays.append("running Podman containers and pods of sandbox-ai: " + ", ".join(podman["running"]))
    if copy["kind"] in PURGEABLE:
        stays.append(f"the program itself - {copy['how']}")
    stays.append("your workspaces and the folders you mounted, everything outside sandbox-ai's own folders, other "
                 "software's Podman images and volumes")
    if copy["kind"] == "system":
        stays.append("other users' sandbox-ai data (their own folders)")
    if found["hosts"]:
        stays.append(f"the other hosts of sandbox-ai hosts ({', '.join(found['hosts'])}): each keeps its "
                     "installation - uninstall there")
    out("")
    t.section("Stays", out, colour)
    for line in stays:
        for number, part in enumerate(t.wrap(line, t.width() - 4)):
            out(("  - " if number == 0 else "    ") + part)
    if running:
        out("")
        out(t.style("Running now (--stop-running stops them first): " + ", ".join(
            f"{entry['name']} ({entry['state']})" for entry in running), "warn", colour))


def purge(found, args, out):
    """Remove what --purge (and --secrets) listed; the reasons of what stayed."""
    problems = []
    targets = found["purge"]
    mounts = mount_points()
    for target in targets["remove"]:
        if target.get("login"):  # the app's login entry: that one file
            with contextlib.suppress(FileNotFoundError):
                os.unlink(target["path"])
            continue
        why = remove_path(target["path"], targets["roots"], mounts)
        if why:
            problems.append(why)
    # What sandbox-ai wrote there since the table was made (stopping the starts: their ends in the histories, the
    # lines of launcher.log): the same folders' own, gone too - never what stays (the secrets and logins).
    again = purge_targets(args.secrets, mcp_data=args.mcp_data)
    for target in again["remove"]:
        if not target.get("login") and target["path"] not in {t["path"] for t in targets["remove"]}:
            why = remove_path(target["path"], again["roots"], mounts)
            if why:
                problems.append(why)
    folders = [*reversed(targets["folders"]), *reversed(again["folders"]), *targets["roots"], *again["roots"]]
    for folder in dict.fromkeys(folders):
        with contextlib.suppress(OSError):
            os.rmdir(folder)  # (when nothing stayed in it)
    if found["podman"]:
        for what, why in remove_podman(found["podman"]).items():
            problems.append(f"Podman: {what}: {why}")
    store, names = found["keyring"]
    if args.secrets and store is not None:
        for name in names:
            try:
                if not store.delete(name):
                    problems.append(f"the {store.name} kept {name}")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                problems.append(f"the {store.name} kept {name}: {exc}")
    return problems


def question(args, copy=None):
    text = "Remove the installation listed above"
    if args.purge:
        text = "Remove the installation and sandbox-ai's data listed above"
        if copy and copy["kind"] in PURGEABLE:
            text = "Remove sandbox-ai's data listed above (the program stays)"
    if args.secrets:
        text += ", with your secrets and logins (they cannot be recovered)"
    return text + "? [y/N] "


def main(argv, out=print, ask=None, isatty=None):
    parser = argparse.ArgumentParser(prog="sandbox-ai uninstall", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[2:5]))
    parser.add_argument("--purge", action="store_true",
                        help="also sandbox-ai's data: starts and their HOMEs, logs, kits, settings, caches, the web "
                             "interface's files, its Podman images and volumes")
    parser.add_argument("--secrets", action="store_true",
                        help="with --purge: also the secrets and logins (keyring and file store, Git logins); they "
                             "cannot be recovered")
    parser.add_argument("--mcp-data", action="store_true",
                        help="with --purge: also the MCP servers' own folders (mcp-folders: what the servers keep "
                             "there, a memory server's graph, their uploads)")
    parser.add_argument("--stop-running", action="store_true",
                        help="stop the running starts first (else they refuse the uninstall)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show what would go; remove nothing")
    parser.add_argument("-y", "--yes", action="store_true", help="without asking (for scripts)")
    args = parser.parse_args(argv)
    if args.secrets and not args.purge:
        parser.error("--secrets goes with --purge (sandbox-ai uninstall --purge --secrets)")
    if args.mcp_data and not args.purge:
        parser.error("--mcp-data goes with --purge (sandbox-ai uninstall --purge --mcp-data)")
    copy = copy_kind()
    installed = copy["kind"] in ("user", "system")
    if not installed and not (args.purge and copy["kind"] in PURGEABLE):
        say(f"{copy['how']}; nothing removed (sandbox-ai's data stays: ~/.local/state/sandbox-ai, "
            "~/.local/share/sandbox-ai, ~/.config/sandbox-ai"
            + ("; sandbox-ai uninstall --purge removes it, the program stays)" if copy["kind"] in PURGEABLE else ")"))
        return 1
    if copy["refused"] and not args.dry_run:  # (--dry-run only lists: as anyone)
        say(copy["refused"])
        return 1
    try:
        running = running_starts()
    except (OSError, ValueError, RuntimeError) as exc:
        say(f"could not tell which starts run ({exc}); nothing removed")
        return 1
    listing = ", ".join(f"{entry['name']} ({entry['state']})" for entry in running)
    if running and not args.stop_running and not args.dry_run:
        say(f"these run: {listing}; stop them first (sandbox-ai stop --all, sandbox-ai mcp stop NAME), or add "
            "--stop-running to stop them before anything is removed; nothing removed")
        return 1
    try:
        found = plan(copy, args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        say(f"{exc}; nothing removed")
        return 1
    colour = textout().colour_enabled(sys.stdout)
    show(found, args, copy, running, out, colour)
    if args.dry_run:
        return 0
    if not args.yes:
        if not (isatty or sys.stdin.isatty)():
            say("no terminal to ask on: run it with --yes to remove what is listed above; nothing removed")
            return 1
        out("")
        answer = (ask or input)(question(args, copy))
        if str(answer).strip().lower() not in ("y", "yes"):
            out("nothing removed")
            return 1
    log = launch_log()
    timer_off = []
    if found.get("timer"):  # first: no scheduled run starts while (or after) the rest goes
        log.info("uninstall", "turning the schedules' timer off: " + ", ".join(found["timer"]))
        timer_off = turn_off_timer(out)
    if found.get("update_timer"):  # (it would run this copy's update every 15 minutes)
        log.info("uninstall", "turning the update timer off: " + ", ".join(found["update_timer"]))
        turn_off_update_timer()
    if running:
        log.info("uninstall", f"stopping first: {listing}")
        left = stop_running(running, out)
        if left:
            say(f"still running: {', '.join(left)}; " + (
                "nothing else removed - the schedules' timer is off now (sandbox-ai schedule enable turns it on "
                "again)" if timer_off else "nothing removed"))
            return 1
    log.info("uninstall", (f"removing the {copy['kind']} installation in {copy['data']}" if installed else
                           f"removing sandbox-ai's data (a {copy['kind']} copy: the program stays)")
             + (" and sandbox-ai's data" if args.purge and installed else "")
             + (" with the secrets and logins" if args.secrets else ""))
    if args.purge:
        log._write = lambda line: None  # (from now on: launcher.log would make the state folder again)
    problems = purge(found, args, out) if args.purge else []
    status = run_installer(copy) if installed else 0
    for problem in problems:
        say(f"left: {problem}")
    if status != 0:
        say(f"install.sh --uninstall ended with exit status {status}")
    out(("removed what is listed above" if not problems and status == 0 else "removed the rest of what is listed "
         "above") + ("; your secrets and logins stay (--purge --secrets removes them)" if args.purge and
                     not args.secrets and (found["purge"]["kept"] or found["keyring"][1]) else ""))
    if found["hosts"]:
        out(f"other hosts keep their installation: {', '.join(found['hosts'])} (uninstall there)")
    if not installed:
        out(f"the program itself stays: {copy['how']}")
    return 0 if not problems and status == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
