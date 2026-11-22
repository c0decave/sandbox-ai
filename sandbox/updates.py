#!/usr/bin/env python3
"""sandbox-ai update mode: whether this installation looks for, fetches and installs new releases by itself.

  sandbox-ai update mode [--json]       the mode, the last check and its result, the next check, the timer
  sandbox-ai update mode MODE [--at HH:MM[-HH:MM]] [--source URL] [--json]
  sandbox-ai update --apply             install the release `download` fetched (checked again first)
  sandbox-ai update --install           download the newest signed release now, check it and install it
  sandbox-ai update --rollback          back to the version before (install.sh keeps the last 2 in versions/)
  sandbox-ai update --scheduled         what the timer runs: the daily check, the download, the install in the window

The modes (one per installation, update_mode in ~/.config/sandbox-ai/defaults.json):
  off       nothing is checked unless asked (sandbox-ai update --check, the app's Check for updates)
  notify    the default: a check once a day; a newer release signed by a trusted key is announced - a desktop
            notification, the app's banner, one line on the command line (at most once a day, on standard error,
            never with --json or in a pipe) and the notify channels' event "update"
  download  also fetches it and checks it in full in the background (SHA256SUMS, the signature by a trusted key):
            ready for sandbox-ai update --apply or the app's button; nothing is switched
  auto      also installs it, in the window (--at, default 03:00-05:00 local time) and only while no sandbox,
            session, MCP server or scheduled run of this installation runs or is due within the hour (one paused or
            kept is fine); otherwise it waits for the next window and says so. Never a downgrade, never unsigned.
            Afterwards a notification names the old and the new version and where the release notes are.

download and auto are for a copy that replaces itself: an installation of a user's own made by install.sh (the
bundle, through install.sh), and - package_update.py - the .pyz and the AppImage (their file), the macOS app
(sandbox-ai.app, from the release's disk image) and a pip or pipx installation (the release's wheel), each where
whoever runs the update may write it. A system installation is root's to update (sudo sandbox-ai update; notify works
for every user); a git checkout, an unpacked AppImage or a copy in a folder you cannot write say the command that
updates them instead.

The timer (`update mode` makes and removes it; sandbox-ai uninstall removes it too): a systemd user timer or a
LaunchAgent beside the schedules' one (the same machinery, schedule.py), every 15 minutes, as the user who set the
mode - never root for a user's installation. It runs `sandbox-ai update --scheduled`, which asks the release source
(update_source, else SANDBOX_AI_SOURCE, else install.sh's own) at most once a day and nothing else - the command
fetches the package guard's copy of the known malicious packages from that same source along with it, signed too
and also once a day at most (sync_packages_along; sandbox-ai packages); an update only
takes the signed path (install.sh --fetch, then install.sh --from BUNDLE --verify; a package: package_update.py's
download, checked twice the same way), never code from a workspace.
Without a timer (notify, never set) the check runs when sandbox-ai is used on a terminal or the app
starts, at most once a day. SANDBOX_AI_UPDATE_NOW (epoch seconds or an ISO time) stands in for the clock (tests).

Standard library only.
"""
import argparse
import contextlib
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()

MODES = ("off", "notify", "download", "auto")
DEFAULT_MODE = "notify"
DEFAULT_WINDOW = "03:00-05:00"
SELF_MODES = ("download", "auto")      # the modes that change the installation: a copy that replaces itself only
CHECK_EVERY = 86400                    # seconds between two checks
RETRY_EVERY = 3600                     # after a check that failed (offline)
HINT_EVERY = 86400                     # the command line's one line about a newer release
SOON = 3600                            # a scheduled run due within this counts as about to run
TICK = 900                             # the timer's seconds between two runs
MIN_WINDOW = 15                        # minutes a window lasts at least (one tick falls into it)
DEFAULT_SOURCE = "https://github.com/c0decave/sandbox-ai"  # install.sh's DEFAULT_SOURCE
PENDING = "update-pending"             # below the installation's data folder: what download fetched
KEYS = ("update_mode", "update_window", "update_source")   # in defaults.json
WINDOW = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])(?:-([01][0-9]|2[0-3]):([0-5][0-9]))?")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][0-9A-Za-z.+-]+)?")
HOMES = ("/home/", "/root/", "/Users/", "/var/home/")
PACKAGES = {"pyz": "sandbox-ai.pyz", "appimage": "the AppImage (sandbox-ai-ARCH.AppImage)",
            "macos-app": "the macOS app (sandbox-ai-ARCH.dmg)"}
WHAT = {"pyz": "sandbox-ai.pyz", "appimage": "the AppImage", "macos-app": "the macOS app", "pip": "a pip installation"}
TIMER_NAME = "sandbox-ai-update"
LAUNCHD_LABEL = "ai.sandbox-ai.update"
UNIT_MARKER = "# Written by sandbox-ai update mode; removed by update mode off and sandbox-ai uninstall."
BUSY_STATUS = 3                        # --apply, --rollback: refused while something of this installation runs
QUIET_COMMANDS = {"update", "__complete", "bridge-stub", "completion", "help", "version", "uninstall"}


def load_module(name):
    alias = f"sandbox_ai_{name}_for_updates"
    loaded = sys.modules.get(alias)
    if loaded is None:
        with LOADING:
            loaded = sys.modules.get(alias)
            if loaded is None:
                spec = importlib.util.spec_from_file_location(alias, HERE / f"{name}.py")
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                sys.modules[alias] = loaded
    return loaded


def printable(text, limit=300):
    text = "".join(c if c.isprintable() else "?" for c in str(text))
    return text if len(text) <= limit else text[:limit] + "…"


# -- the clock, versions ----------------------------------------------------------------------------------------------

def now():
    """Seconds since the epoch; SANDBOX_AI_UPDATE_NOW (epoch seconds or an ISO time, local) stands in (tests)."""
    given = os.environ.get("SANDBOX_AI_UPDATE_NOW")
    if given:
        with contextlib.suppress(ValueError):
            return float(given)
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(given).timestamp()
    return time.time()


def version_key(version):
    """install.sh's order: 1.2.3 < 1.2.4; a pre-release (1.2.4-rc1) before its release."""
    match = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)(.*)", str(version or ""))
    return (int(match[1]), int(match[2]), int(match[3]), match[4] == "", match[4]) if match else (0, 0, 0, False, "")


def newer(available, installed):
    return bool(available and installed and VERSION.fullmatch(available) and
                version_key(available) > version_key(installed))


def tree_version(tree):
    """The version of the sandbox-ai tree TREE (its sandbox/agent_sandbox.py), or None."""
    try:
        with open(Path(tree) / "sandbox" / "agent_sandbox.py", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                found = re.match(r'__version__ = "([^"\n]{1,40})"$', line.rstrip("\n"))
                if found:
                    return found[1]
    except OSError:
        return None
    return None


def own_version():
    """This copy's version: its tree's (sandbox/agent_sandbox.py), a pip installation's (agent_sandbox.py beside
    this module: its folder is sandbox_ai there). A .pyz or an AppImage whose file an update of ours has replaced
    while this copy of it - unpacked from the file before - still runs (the app, after Install now): the version of
    the file in place, which the next start runs."""
    own = tree_version(HERE.parent) or packages().module_version(HERE) or "unknown"
    try:
        info = installation()
        if flavour(info) in ("pyz", "appimage"):
            return packages().current_version(info)
    except Exception:  # noqa: BLE001 - never an error of a version
        pass
    return own


def packages():
    """package_update.py: the copies install.sh did not install (the .pyz, the AppImage, the macOS app, pip)."""
    return load_module("package_update")


def flavour(info):
    """"pyz", "appimage", "macos-app" or "pip" when INFO is a copy package_update.py updates, else None."""
    return packages().flavour(info)


def self_updating(info):
    """Whether `sandbox-ai update` of whoever runs it replaces the copy INFO: install.sh's installation of a user's
    own, and a package or a pip installation whose file or folder can be written (package_update.target)."""
    if info["kind"] == "user":
        return True
    return flavour(info) is not None and packages().target(info)[0] is not None


def shown(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


# -- the settings (defaults.json) and the state -----------------------------------------------------------------------

def check_mode(value, where="update_mode"):
    if value not in MODES:
        raise ValueError(f"{where} is one of {', '.join(MODES)}")
    return value


def parse_window(text, where="--at"):
    """(start, end) in minutes after midnight of HH:MM-HH:MM (it may pass midnight); HH:MM alone: that hour."""
    match = WINDOW.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise ValueError(f"{where} is HH:MM or HH:MM-HH:MM (local time, e.g. 03:00-05:00)")
    start = int(match[1]) * 60 + int(match[2])
    end = int(match[3]) * 60 + int(match[4]) if match[3] else (start + 60) % 1440
    if (end - start) % 1440 < MIN_WINDOW:
        raise ValueError(f"{where}: the window lasts at least {MIN_WINDOW} minutes (the timer looks every "
                         f"{TICK // 60} minutes)")
    return start, end


def window_text(text):
    start, end = parse_window(text)
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def check_source(value, where="update_source"):
    parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    if not parts or parts.scheme not in ("http", "https") or not parts.hostname or \
            len([p for p in parts.path.split("/") if p]) < 2 or parts.query or parts.fragment or \
            parts.username or parts.password or len(value) > 300 or not value.isprintable():
        raise ValueError(f"{where} is the repository with the releases: https://HOST/OWNER/REPO (Gitea, Forgejo or "
                         "https://github.com/OWNER/REPO)")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"{where}: plain http only to this machine (localhost); use https")
    return value


def check_setting(key, value, where):
    """The launcher's check of the update keys of defaults.json (agent_sandbox.load_user_defaults)."""
    if key == "update_mode":
        check_mode(value, f"{where}: update_mode")
    elif key == "update_window":
        parse_window(value, f"{where}: update_window")
    elif key == "update_source":
        check_source(value, f"{where}: update_source")


def defaults_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"


def settings():
    """{"mode", "set" (whether it was chosen), "window", "source"} from defaults.json; a broken file: the defaults."""
    try:
        document = json.loads(defaults_path().read_text())
    except (OSError, ValueError, RecursionError):
        document = {}
    document = document if isinstance(document, dict) else {}
    mode, source = document.get("update_mode"), document.get("update_source")
    try:
        window = window_text(document.get("update_window") or DEFAULT_WINDOW)
    except ValueError:
        window = DEFAULT_WINDOW
    return {"mode": mode if mode in MODES else DEFAULT_MODE, "set": mode in MODES, "window": window,
            "source": source if isinstance(source, str) else None}


def release_source(chosen=None):
    """The repository the releases come from: update_source, else SANDBOX_AI_SOURCE, else install.sh's own."""
    return chosen or os.environ.get("SANDBOX_AI_SOURCE") or DEFAULT_SOURCE


def save_settings(changes):
    """CHANGES (key: value, None removes it) into defaults.json, checked by the launcher as `defaults set` does."""
    defaults = load_module("defaults_cmd")
    with defaults.locked():
        document, path = defaults.read()
        for key, value in changes.items():
            if value is None:
                document.pop(key, None)
            else:
                document[key] = value
        defaults.check(document, path)
        defaults.write(path, document)


def state_path():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "sandbox-ai" / "update.json"


def load_state():
    try:
        document = json.loads(state_path().read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return document if isinstance(document, dict) else {}


def save_state(state):
    path = state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=1)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


# -- this installation ------------------------------------------------------------------------------------------------

def installation(here=None):
    """{"kind", "tree", "data", "prefix", "package"} of the copy whose sandbox/ is HERE: KIND "user" (install.sh, a
    user's own: DATA/versions/X), "system" (install.sh in PREFIX/lib/sandbox-ai), "package" (pyz, AppImage, the macOS
    app), "checkout" (git), "pip" (no install.sh beside it) or "tree" (an unpacked bundle, not installed)."""
    tree = Path(here or HERE).parent
    info = {"kind": "tree", "tree": str(tree), "data": None, "prefix": None, "package": None}
    try:
        package = (tree / "PACKAGING").read_text().strip()
    except (OSError, UnicodeDecodeError):
        package = None
    if package in PACKAGES:
        return {**info, "kind": "package", "package": package}
    if (tree / ".git").exists():
        return {**info, "kind": "checkout"}
    if not (tree / "install.sh").is_file():
        return {**info, "kind": "pip"}
    if tree.parent.name != "versions":
        return info
    data = tree.parent.parent
    if data.name == "sandbox-ai" and data.parent.name == "lib" and not str(data).startswith(HOMES):
        return {**info, "kind": "system", "data": str(data), "prefix": str(data.parent.parent)}
    return {**info, "kind": "user", "data": str(data)}


def installs_by_hand(info, access=os.access):
    """Whether whoever runs it can install a release into the copy INFO now (--install, the app's Install now): a
    copy that replaces itself, and a system installation by whoever may write it - root, as a rule (asked of the
    system, not concluded from the user's number: a root without its capabilities, as in some containers, cannot).
    (download and auto, which act by themselves, stay with the copies of a user's own: self_updating.)"""
    if self_updating(info):
        return True
    return info["kind"] == "system" and bool(info.get("data")) and bool(access(info["data"], os.W_OK))


def as_root(info, what="update"):
    """Why a system installation is not changed by this user, with the command that does it."""
    command = "sudo sandbox-ai update" + ("" if what == "update" else f" --{what}")
    return (f"this is a system installation ({info.get('data')}), which only root changes: {command}")


def manual_way(info):
    """How the copy INFO is updated (the words after "update: "): `sandbox-ai update` where it replaces itself, else
    the command that does it."""
    kind = info["kind"]
    if flavour(info):
        path, why = packages().target(info)
        return "sandbox-ai update" if path is not None else why
    if kind == "package":
        return (f"download the new {PACKAGES.get(info.get('package'), 'package')} from the release page and check it "
                "with SHA256SUMS (or install with install.sh, which updates itself)")
    if kind == "checkout":
        return f"git -C {info['tree']} pull, then install it: ./install.sh --from . (in {info['tree']})"
    if kind == "system":
        return "sudo sandbox-ai update (a system installation is root's to update)"
    if kind == "tree":
        return f"install it with install.sh (./install.sh --from {info['tree']}): that installation updates itself"
    return "sandbox-ai update"


def refusal(info, mode):
    """Why MODE cannot be this copy's, or None."""
    if mode not in SELF_MODES or self_updating(info):
        return None
    what = {"pip": "a pip or pipx installation", "package": f"a package ({info.get('package')})",
            "checkout": "a git checkout", "system": "a system installation", "tree": "not installed by install.sh"}
    return (f"update mode {mode} changes the installation by itself, which only a copy that whoever runs the update "
            f"can replace does (install.sh's installation of a user's own, a package or a pip installation you may "
            f"write); this copy is {what.get(info['kind'], info['kind'])}: notify works and says how to update "
            f"it - {manual_way(info)}")


def install_args(info, euid=None):
    """install.sh's options for the kind of installation INFO is (sandbox-ai update's installation_mode)."""
    euid = os.geteuid() if euid is None else euid
    if info["kind"] == "system":
        return ["--system", "--prefix", info["prefix"]]
    if info["kind"] == "user" and euid == 0:
        return ["--user"]
    return []


def find_bash():
    try:
        return load_module("main").find_bash()
    except SystemExit as exc:
        raise ValueError(str(exc.code)) from None


def installer(info):
    path = Path(info["tree"]) / "install.sh"
    return path if path.is_file() else None


def pending_dir(info):
    return Path(info["data"]) / PENDING if info.get("data") else None


def pending_release(info):
    """{"version", "bundle"} of what download fetched (a bundle - a package: its file - with its SHA256SUMS and
    SHA256SUMS.sig), or None."""
    if flavour(info):
        found = packages().pending(info)
        return {"version": found["version"], "bundle": found["file"]} if found else None
    folder = pending_dir(info)
    if folder is None or not folder.is_dir() or folder.is_symlink():
        return None
    found = []
    for path in folder.iterdir():
        match = re.fullmatch(r"sandbox-ai-bundle-(" + VERSION.pattern + r")\.tar\.gz", path.name)
        if match and path.is_file() and not path.is_symlink():
            found.append((version_key(match[1]), match[1], path))
    if not found or not (folder / "SHA256SUMS").is_file() or not (folder / "SHA256SUMS.sig").is_file():
        return None
    _, version, path = max(found)
    return {"version": version, "bundle": str(path)}


def drop_pending(info):
    if flavour(info):
        return packages().drop_pending(info)
    folder = pending_dir(info)
    if folder is not None and folder.is_dir() and not folder.is_symlink():
        shutil.rmtree(folder, ignore_errors=True)


def kept_versions(info):
    """(current, [the other versions kept in versions/, newest first]) of install.sh's installation INFO."""
    data = Path(info["data"])
    try:
        current = (data / "current").resolve().name if (data / "current").is_symlink() else None
        others = [p.name for p in (data / "versions").iterdir() if p.is_dir() and not p.is_symlink()
                  and p.name != current and VERSION.fullmatch(p.name) and tree_version(p) == p.name]
    except OSError:
        return None, []
    return current, sorted(others, key=version_key, reverse=True)


def rollback_target(info):
    """The version --rollback switches to: the newest kept one older than the current one, else the newest other
    one; a package or a pip installation: the version kept before its last update (package_update.previous). None
    when there is none."""
    if flavour(info):
        kept = packages().previous(info) if packages().target(info)[0] is not None else None
        return kept["version"] if kept else None
    if info["kind"] not in ("user", "system"):
        return None
    current, others = kept_versions(info)
    older = [v for v in others if current and version_key(v) < version_key(current)]
    return (older or others or [None])[0]


# -- what runs --------------------------------------------------------------------------------------------------------

def busy_reasons(at=None):
    """What of this installation runs now or is about to: running sandboxes and sessions (a paused or kept one is
    fine), MCP servers, scheduled runs that run or are due within the hour. [] when nothing; what cannot be read
    counts as running (an update waits rather than guesses)."""
    at = now() if at is None else at
    reasons = []
    try:
        for entry in load_module("starts").entries(include_all=False, prune=False):
            state, name = entry.get("state"), printable(entry.get("name"), 60)
            if entry.get("kind") == "mcp":
                if state == "running":
                    reasons.append(f"the MCP server {name} runs")
            elif state in ("running", "orphaned"):
                reasons.append(f"{name} runs")
    except Exception as exc:  # noqa: BLE001 - unknown: as if something ran
        reasons.append(f"what runs could not be read ({printable(exc, 120)})")
    try:
        schedule = load_module("schedule")
        for name in schedule.names():
            try:
                document = schedule.load(name)
            except (OSError, ValueError):
                continue
            if schedule.running(name):
                reasons.append(f"the scheduled run {name} runs")
            elif not document.get("paused") and document.get("timing"):
                due = schedule.next_due(document, at)
                if due is not None and due - at <= SOON:
                    reasons.append(f"the schedule {name} starts a run at {datetime.fromtimestamp(due):%H:%M}")
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"the schedules could not be read ({printable(exc, 120)})")
    return reasons


def in_window(at, window):
    start, end = parse_window(window)
    local = datetime.fromtimestamp(at)
    minute = local.hour * 60 + local.minute
    return start <= minute < end if start < end else (minute >= start or minute < end)


def next_window(at, window):
    """When the next window after AT begins (epoch seconds)."""
    start, _ = parse_window(window)
    local = datetime.fromtimestamp(at)
    begin = local.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if begin <= local:
        begin += timedelta(days=1)
    return begin.timestamp()


# -- the check --------------------------------------------------------------------------------------------------------

def signature_kind(line):
    if line.startswith("good"):
        return "good"
    if line.startswith("BAD"):
        return "bad"
    if line.startswith("none"):
        return "none"
    return "unchecked"


def check_with_installer(info, source, runner=subprocess.run):
    """install.sh --check of this copy: its own installation's (its keys), or, for a copy install.sh did not install,
    with a data folder of nothing (its tree's keys). {"available", "signature", "signer", "error"}."""
    script = installer(info)
    command = [find_bash(), str(script), "--check", "--source", source, *install_args(info)]
    with tempfile.TemporaryDirectory(prefix="sandbox-ai-check.") as empty:
        env = dict(os.environ)
        if info["kind"] not in ("user", "system"):
            env["XDG_DATA_HOME"] = empty
        try:
            done = runner(command, capture_output=True, text=True, timeout=180, env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": None, "signature": None, "signer": None, "error": printable(exc)}
    return parse_check(done)


def parse_check(done):
    """{"available", "signature", "signer", "error"} of what install.sh --check (a finished process) said."""
    out = done.stdout or ""
    available = re.search(r"^available: (\S+)", out, re.M)
    signature = re.search(r"^signature: (.*)$", out, re.M)
    if done.returncode != 0 or not available or not VERSION.fullmatch(available[1]):
        lines = [line for line in (done.stderr or "").splitlines() if line.strip()]
        why = lines[-1] if lines else f"install.sh --check ended with {done.returncode}"
        return {"available": None, "signature": None, "signer": None,
                "error": printable(re.sub(r"^install\.sh: (error: )?", "", why))}
    kind = signature_kind(signature[1]) if signature else "none"
    signer = re.search(r"(SHA256:[A-Za-z0-9+/]+)", signature[1]) if signature else None
    return {"available": available[1], "signature": kind, "signer": signer[1] if signer and kind == "good" else None,
            "error": None}


def check(info=None, source=None, runner=subprocess.run, clock=now):
    """Ask the release source for the newest release: {"time", "installed", "available", "signature", "signer",
    "newer" (a newer one, signed by a trusted key), "error"}."""
    info = info or installation()
    source = release_source(source)
    # A package or a pip installation asks by itself (package_update.check, with this copy's release keys); the
    # others through their install.sh.
    found = packages().check(source) if flavour(info) or not installer(info) else \
        check_with_installer(info, source, runner)
    installed = own_version()
    return {"time": clock(), "installed": installed, "source": source, **found,
            "newer": bool(found.get("signature") == "good" and newer(found.get("available"), installed))}


def manual_check(args, info=None, runner=subprocess.run, stdout=None, stderr=None):
    """`sandbox-ai update --check [ARGS]` of install.sh's installation: install.sh says it as always, and what it found
    is kept as the last check (update mode, the app, the command line's hint)."""
    info = info or installation()
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    done = runner([find_bash(), str(installer(info)), *args], capture_output=True, text=True, timeout=300,
                  stdin=subprocess.DEVNULL)
    stdout.write(done.stdout or "")
    stderr.write(done.stderr or "")
    if "--version" not in args and not any(arg.startswith("--version=") for arg in args):
        given = args[args.index("--source") + 1] if "--source" in args[:-1] else None
        found = parse_check(done)
        installed = own_version()
        result = {"time": now(), "installed": installed, "source": release_source(given), **found,
                  "newer": bool(found["signature"] == "good" and newer(found["available"], installed))}
        with contextlib.suppress(OSError):
            save_state(record_check(result))
    return done.returncode


def check_due(state, at):
    last = state.get("last_check")
    if not isinstance(last, (int, float)):
        return True
    wait = RETRY_EVERY if (state.get("result") or {}).get("error") else CHECK_EVERY
    return at - last >= wait or at < last


def record_check(result, state=None):
    state = load_state() if state is None else state
    state["last_check"], state["result"] = result["time"], result
    return state


# -- telling ----------------------------------------------------------------------------------------------------------

def notes_link(version, source=None):
    """Where the release notes of VERSION are: the release's page, and this installation's copy when it has one."""
    source = release_source(source).rstrip("/").removesuffix(".git")
    return f"{source}/releases/tag/v{version}"


def announce(title, text, urgency="normal"):
    """A desktop notification and the notify channels' event "update". Never an error."""
    told = []
    with contextlib.suppress(Exception):
        load_module("approvals").desktop_notification(title, text, urgency=urgency)
        told.append("desktop")
    if (HERE / "notify.py").is_file():
        with contextlib.suppress(Exception):
            notify = load_module("notify")
            notify.tell("update", None, f"{title}: {text}".replace("\n", " "))
            notify.finish(5)
            told.append("notify")
    return told


# -- the timer --------------------------------------------------------------------------------------------------------

def timer_command(info=None):
    """What the timer runs: the installation's current/ (an update never leaves the timer pointing at an old
    version), else this copy's main.py."""
    info = info or installation()
    if flavour(info):  # (the package's file, not the copy of one version it unpacked)
        return packages().timer_command(info)
    main = Path(info["data"]) / "current" / "sandbox" / "main.py" if info.get("data") else HERE / "main.py"
    return [sys.executable, "-I", str(main), "update", "--scheduled"]


def timer(info=None):
    """The update timer as schedule.py's timer functions take it."""
    schedule = load_module("schedule")
    return {"name": TIMER_NAME, "label": LAUNCHD_LABEL, "marker": UNIT_MARKER, "args": ["update", "--scheduled"],
            "command": timer_command(info), "description": "sandbox-ai updates: the update mode's check (sandbox-ai "
            "update --scheduled)", "every": f"every {TICK // 60} minutes", "calendar": f"*-*-* *:00/{TICK // 60}:00",
            "accuracy": "1min", "interval": TICK, "log": state_path().with_name("update.log"),
            "said": "sandbox-ai update mode", "on": f"the timer looks every {TICK // 60} minutes",
            "off": "the timer is off", "none": "no update timer was on",
            "no_systemctl": "no systemctl here: run `sandbox-ai update --scheduled` every 15 minutes yourself "
                            "(cron, a loop)", "schedule": schedule}


def timer_files(platform=None):
    spec = timer()
    return spec.pop("schedule").timer_files(platform, spec)


def timer_state(run=subprocess.run):
    spec = timer()
    return spec.pop("schedule").timer_state(run, spec)


def timer_on(run=subprocess.run, out=print, info=None):
    spec = timer(info)
    return spec.pop("schedule").enable(run=run, out=out, timer=spec)


def timer_off(run=subprocess.run, out=print):
    spec = timer()
    return spec.pop("schedule").disable(run=run, out=out, timer=spec)


# -- what the commands do ---------------------------------------------------------------------------------------------

def status(info=None, run=subprocess.run, at=None):
    """Everything `update mode` shows (and --json gives)."""
    info = info or installation()
    at = now() if at is None else at
    chosen, state = settings(), load_state()
    result = dict(state.get("result") or {})
    installed = own_version()
    if result:
        result["installed"] = installed
        result["newer"] = bool(result.get("signature") == "good" and newer(result.get("available"), installed))
    timer_now = timer_state(run)
    last = state.get("last_check") if isinstance(state.get("last_check"), (int, float)) else None
    next_check = None
    if chosen["mode"] != "off":
        next_check = at if last is None else last + (RETRY_EVERY if result.get("error") else CHECK_EVERY)
    pending = pending_release(info)
    return {"mode": chosen["mode"], "chosen": chosen["set"], "window": chosen["window"],
            "source": release_source(chosen["source"]), "installed": installed, "kind": info["kind"],
            "data": info.get("data"), "self_updating": self_updating(info),
            "install_now": installs_by_hand(info),
            "manual": None if self_updating(info) else manual_way(info),
            "refused": {mode: refusal(info, mode) for mode in SELF_MODES if refusal(info, mode)},
            "last_check": last, "result": result or None, "next_check": next_check,
            "timer": {"installed": timer_now["installed"], "active": timer_now["active"], "kind": timer_now["kind"],
                      "files": timer_now["files"]},
            "downloaded": pending["version"] if pending else None, "waiting": state.get("waiting"),
            "last_install": state.get("last_install"), "rollback": rollback_target(info),
            "skipped": state.get("skip_version"), "notes": notes_link(result["available"], chosen["source"])
            if result.get("available") else None}


def result_text(result):
    if not result:
        return "not checked yet"
    if result.get("error"):
        return f"failed: {result['error']}"
    available, installed = result.get("available"), result.get("installed")
    signed = {"good": f"signed by {result.get('signer') or 'a trusted key'}", "bad": "its signature is BAD",
              "none": "not signed", "unchecked": "its signature not checked (no release key is trusted)"}.get(
        result.get("signature"), "")
    if result.get("newer"):
        return f"{available} is available (installed: {installed}), {signed}"
    if newer(available, installed):
        return f"{available} is there, but {signed}: not offered"
    return f"up to date ({installed}; the newest release is {available})"


def show(info=None, out=print, run=subprocess.run):
    data = status(info, run)
    mode = data["mode"] + ("" if data["chosen"] else " (the default)")
    out(f"mode: {mode}")
    if data["mode"] == "auto":
        out(f"window: {data['window']} (local time)")
    out(f"source: {data['source']}")
    out(f"last check: {shown(data['last_check'])}" + (f": {result_text(data['result'])}" if data["result"] else ""))
    if data["mode"] == "off":
        out("next check: none (mode off: sandbox-ai update --check asks now)")
    else:
        when = "now" if data["next_check"] is None or data["next_check"] <= now() else f"after {shown(data['next_check'])}"
        how = f"by the timer ({TIMER_NAME})" if data["timer"]["installed"] else \
            "when sandbox-ai is used on a terminal or the app starts (no timer: sandbox-ai update mode " \
            f"{data['mode']} makes one)"
        out(f"next check: {when}, {how}")
    if data["manual"]:
        out(f"this copy: {data['manual']}")
    if data["downloaded"]:
        out(f"downloaded: {data['downloaded']}, checked: sandbox-ai update --apply installs it")
    if data["waiting"]:
        out(f"waiting: {data['waiting'].get('text')}")
    if data["last_install"]:
        last = data["last_install"]
        out(f"last install: {last.get('from')} -> {last.get('to')} at {shown(last.get('time'))}")
    if data["rollback"]:
        out(f"rollback: {data['rollback']} (sandbox-ai update --rollback)")
    if data["notes"] and (data["result"] or {}).get("newer"):
        out(f"release notes: {data['notes']}")
    return 0


def set_mode(mode, window=None, source=None, info=None, out=print, run=subprocess.run):
    """Set the update mode (with its window and source), then make or remove the timer."""
    info = info or installation()
    check_mode(mode, "the mode")
    why = refusal(info, mode)
    if why:
        raise ValueError(why)
    if window is not None and mode != "auto":
        raise ValueError("--at is the window of mode auto")
    changes = {"update_mode": mode}
    if window is not None:
        changes["update_window"] = window_text(window)
    if source is not None:
        changes["update_source"] = check_source(source, "--source") if source else None
    save_settings(changes)
    out(f"update mode: {mode}" + (f", window {settings()['window']}" if mode == "auto" else ""))
    if mode not in SELF_MODES:
        drop_pending(info)
    if mode == "off":
        timer_off(run, out)
        return 0
    try:
        timer_on(run, out, info)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        out(f"  note: no timer ({printable(exc)}); the check runs when sandbox-ai is used on a terminal or the app "
            "starts")
    return 0


def fetch(info, version, source, runner=subprocess.run):
    """install.sh --fetch into the pending folder: the release downloaded and checked in full (a package or a pip
    installation: package_update.fetch_release, its file). Its version."""
    if flavour(info):
        try:
            return packages().fetch_release(info, source, version)["version"]
        except OSError as exc:
            raise ValueError(printable(exc)) from None
    folder = pending_dir(info)
    drop_pending(info)
    done = runner([find_bash(), str(installer(info)), "--fetch", str(folder), "--version", version, "--source",
                   source, *install_args(info)], capture_output=True, text=True, timeout=1800,
                  stdin=subprocess.DEVNULL)
    lines = (done.stdout or "").splitlines()
    if done.returncode != 0 or not lines or lines[0] != version:
        drop_pending(info)
        why = [line for line in (done.stderr or "").splitlines() if line.strip()]
        raise ValueError(printable(re.sub(r"^install\.sh: (error: )?", "", why[-1]) if why else
                                   f"install.sh --fetch ended with {done.returncode}"))
    return version


class Busy(ValueError):
    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def apply(info=None, scheduled=False, runner=subprocess.run, busy=busy_reasons, while_running=False, out=print,
          by_hand=False):
    """Install what download fetched: checked again by install.sh (--from BUNDLE --verify: SHA256SUMS and a signature
    by a trusted key), never a downgrade, only while nothing runs (WHILE_RUNNING: anyway - a running sandbox goes on
    with the version it started with). (old, new)."""
    info = info or installation()
    if not (self_updating(info) or (by_hand and installs_by_hand(info))):
        raise ValueError(refusal(info, "download") or "this copy does not update itself")
    pending = pending_release(info)
    if pending is None:
        raise ValueError("nothing is downloaded (update mode download or auto fetches a new release; sandbox-ai "
                         "update installs the newest now)")
    installed = own_version()
    if not newer(pending["version"], installed):
        drop_pending(info)
        raise ValueError(f"the downloaded {pending['version']} is not newer than the installed {installed}: never a "
                         "downgrade (removed)")
    reasons = busy() if not while_running else []
    if reasons:
        raise Busy(reasons)
    if flavour(info):
        return apply_package(info, installed, scheduled, runner, out)
    command = [find_bash(), str(installer(info)), "--from", pending["bundle"], "--verify", *install_args(info)]
    if scheduled:
        command.append("--no-app-deps")  # (nothing from PyPI unasked: the app's environment stays as it is)
    done = runner(command, capture_output=scheduled, text=True, timeout=1800, stdin=subprocess.DEVNULL)
    after = tree_version(Path(info["data"]) / "current")
    if done.returncode != 0 or after != pending["version"]:
        why = [line for line in (done.stderr or "").splitlines() if line.strip()] if scheduled else []
        raise ValueError(printable(why[-1] if why else f"install.sh ended with {done.returncode}; nothing switched"))
    drop_pending(info)
    state = load_state()
    state["last_install"] = {"from": installed, "to": after, "time": now(), "by": "timer" if scheduled else "hand"}
    state.pop("waiting", None)
    save_state(state)
    return installed, after


def apply_package(info, installed, scheduled, runner, out):
    """apply() for a package or a pip installation: what was downloaded is checked again (the signature of its
    SHA256SUMS, its SHA-256), started once and put in this copy's place; the version before is kept."""
    try:
        found = packages().verified_pending(info)
        _, after, note = packages().install(info, found, runner, scheduled=scheduled)
    except OSError as exc:
        raise ValueError(f"{printable(exc)}; nothing replaced") from None
    drop_pending(info)
    if note:
        out(f"  note: {note}")
    state = load_state()
    state["last_install"] = {"from": installed, "to": after, "time": now(), "by": "timer" if scheduled else "hand"}
    state.pop("waiting", None)
    save_state(state)
    return installed, after


def install_now(info=None, version=None, source=None, while_running=False, allow_downgrade=False, force=False,
                runner=subprocess.run, busy=busy_reasons, out=print):
    """Download the newest signed release now (VERSION: that one), check it in full and install it - what `download`
    and --apply do, in one go (the app's Install now; `sandbox-ai update` of a package or a pip installation).
    (old, new), or None when the installed version is the newest."""
    info = info or installation()
    if not installs_by_hand(info):
        raise ValueError(as_root(info) if info["kind"] == "system" else
                         f"this copy does not replace itself: {manual_way(info)}")
    source = release_source(source or settings()["source"])
    reasons = busy() if not while_running else []
    if reasons:
        raise Busy(reasons)
    if flavour(info):
        try:
            found = packages().fetch_release(info, source, version, allow_downgrade, force)
        except packages().UpToDate as exc:
            out(str(exc))
            return None
        except OSError as exc:
            raise ValueError(printable(exc)) from None
        signed = f"signed by {found['signature'][len('good '):]}" if found["signature"].startswith("good ") else \
            "not signed: this copy trusts no release key yet"
        out(f"sandbox-ai update: downloaded {found['name']} of sandbox-ai {found['version']} from {source} (SHA-256 "
            f"verified, {signed})")
        installed = own_version()
        return apply_package(info, installed, False, runner, out)
    result = check(info, source, runner)
    with contextlib.suppress(OSError):
        save_state(record_check(result))
    if result.get("error"):
        raise ValueError(result["error"])
    if not result["newer"]:
        if newer(result.get("available"), result["installed"]):
            raise ValueError(f"{result_text(result)}; nothing installed")
        out(f"sandbox-ai {result['installed']} is already installed and up to date")
        return None
    fetch(info, result["available"], source, runner)
    return apply(info, runner=runner, busy=busy, while_running=True, out=out, by_hand=True)


def rollback(info=None, busy=busy_reasons, while_running=False, runner=subprocess.run):
    """current/ back to the version before (kept in versions/): (from, to). The version left is not taken again by
    download or auto (a newer one is). A package or a pip installation: the version kept before its last update
    (package_update.rollback)."""
    info = info or installation()
    if flavour(info):
        return rollback_package(info, busy, while_running, runner)
    if info["kind"] not in ("user", "system"):
        raise ValueError(f"only an installation made by install.sh keeps versions to go back to; this copy: "
                         f"{manual_way(info)}")
    current, _ = kept_versions(info)
    target = rollback_target(info)
    if target is None:
        raise ValueError(f"no other version is kept in {Path(info['data']) / 'versions'}: nothing to go back to")
    if info["kind"] == "system" and not installs_by_hand(info):
        raise ValueError(as_root(info, "rollback"))
    reasons = busy() if not while_running else []
    if reasons:
        raise Busy(reasons)
    data = Path(info["data"])
    link = data / f".current-rollback-{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        link.unlink()
    os.symlink(f"versions/{target}", link)
    os.replace(link, data / "current")  # (rename(2): the link is replaced at once)
    drop_pending(info)
    state = load_state()
    state["last_install"] = {"from": current, "to": target, "time": now(), "by": "rollback"}
    state["skip_version"] = current
    state.pop("waiting", None)
    save_state(state)
    return current, target


def rollback_package(info, busy, while_running, runner):
    if rollback_target(info) is None:
        path, why = packages().target(info)
        raise ValueError(f"this copy does not replace itself: {why}" if path is None else
                         "no version is kept to go back to (the one before an update is kept until the next one)")
    reasons = busy() if not while_running else []
    if reasons:
        raise Busy(reasons)
    try:
        current, target = packages().rollback(info, release_source(settings()["source"]), runner)
    except OSError as exc:
        raise ValueError(f"{printable(exc)}") from None
    drop_pending(info)
    state = load_state()
    state["last_install"] = {"from": current, "to": target, "time": now(), "by": "rollback"}
    state["skip_version"] = current
    state.pop("waiting", None)
    save_state(state)
    return current, target


def scheduled(info=None, clock=now, runner=subprocess.run, busy=busy_reasons, tell=announce, out=print):
    """One run of the timer: the check once a day, the announcement of a newer signed release (once per version),
    in download and auto its download, in auto its installation in the window while nothing runs. One at a time
    (a lock beside the state: a second run at the same moment does nothing)."""
    if settings()["mode"] == "off":
        out("update mode off: nothing checked")
        return 0
    with load_module("schedule").Lock(state_path().with_name("update.lock")) as lock:
        if not lock.got:
            out("another update run is under way: nothing done")
            return 0
        return scheduled_run(info or installation(), clock, runner, busy, tell, out)


def failed_lately(state, key, version, at, wait):
    """Whether STATE's KEY (download_error, install_error) says VERSION failed less than WAIT seconds before AT."""
    failure = state.get(key)
    return isinstance(failure, dict) and failure.get("version") == version and \
        isinstance(failure.get("time"), (int, float)) and 0 <= at - failure["time"] < wait


def sync_packages(source=None, out=print):
    """The package guard's copy of the known malicious packages, synced from the release source SOURCE
    (packages.sync_with_update: signed by a release key, never older than the one here). It never fails an update:
    whatever goes wrong there is one line."""
    try:
        return load_module("packages").sync_with_update(out, base=source)
    except Exception as exc:  # noqa: BLE001 - the update is what was asked for
        out(f"sandbox-ai: the copy of the known malicious packages was not synced: {printable(exc)}")
        return None


def sync_packages_along(words=(), source=None, out=print, clock=now):
    """What the command `sandbox-ai update` does for that copy once its own work is done (main.py calls this, for
    WORDS, its options; never a function of this module: an update asked for in a program or a test fetches nothing
    else): one try a day at most - the state's "packages_sync", so a source without a copy is not asked again at
    every tick of the timer - from the update's own source (SOURCE, else update_source). Not in mode off, where
    the timer's run (--scheduled) asks nothing of the network."""
    chosen = settings()
    if "--scheduled" in words and chosen["mode"] == "off":
        return None
    at = clock()
    state = load_state()
    last = state.get("packages_sync")
    if isinstance(last, (int, float)) and not isinstance(last, bool) and 0 <= at - last < CHECK_EVERY:
        return None
    state["packages_sync"] = at
    with contextlib.suppress(OSError):
        save_state(state)
    return sync_packages(source or chosen["source"], out)


def scheduled_run(info, clock, runner, busy, tell, out):
    chosen = settings()
    mode = chosen["mode"]
    at = clock()
    state = load_state()
    if check_due(state, at):
        result = check(info, chosen["source"], runner, lambda: at)
        state = record_check(result, state)
        out(f"checked: {result_text(result)}")
        if result["newer"] and state.get("announced") != result["available"]:
            way = "sandbox-ai update" if self_updating(info) else manual_way(info)
            tell(f"sandbox-ai {result['available']} is available",
                 f"installed: {result['installed']}; update: {way}; release notes: "
                 f"{notes_link(result['available'], chosen['source'])}")
            state["announced"] = result["available"]
        save_state(state)
    result = state.get("result") or {}
    available = result.get("available")
    wanted = bool(result.get("signature") == "good" and newer(available, own_version())
                  and not (state.get("skip_version") and version_key(available) <= version_key(state["skip_version"])))
    if mode not in SELF_MODES or not self_updating(info) or not wanted:
        return 0
    pending = pending_release(info)
    if pending is None or pending["version"] != available:
        if failed_lately(state, "download_error", available, at, RETRY_EVERY):
            return 1  # (tried again an hour after it failed, not at every tick)
        try:
            fetch(info, available, release_source(chosen["source"]), runner)
            out(f"downloaded and checked: {available}")
            state = load_state()
            state.pop("download_error", None)
            save_state(state)
        except ValueError as exc:
            out(f"download failed: {exc}")
            state = load_state()
            state["download_error"] = {"version": available, "error": str(exc), "time": at}
            save_state(state)
            return 1
    if mode != "auto":
        return 0
    if not in_window(at, chosen["window"]):
        return 0
    if failed_lately(load_state(), "install_error", available, at, CHECK_EVERY):
        return 1  # (an install that failed is tried again the next day, and told once)
    try:
        old, new = apply(info, scheduled=True, runner=runner, busy=lambda: busy(at))
    except Busy as exc:
        later = at + TICK if in_window(at + TICK, chosen["window"]) else next_window(at, chosen["window"])
        text = (f"{available} waits: {'; '.join(exc.reasons)}; tried again at {shown(later)} "
                f"({'in this window' if later == at + TICK else 'the next window'})")
        state = load_state()
        state["waiting"] = {"version": available, "reasons": exc.reasons, "next": later, "text": text, "time": at}
        save_state(state)
        out(text)
        return 0
    except ValueError as exc:
        out(f"install failed: {exc}")
        state = load_state()
        state["install_error"] = {"version": available, "error": str(exc), "time": at}
        save_state(state)
        tell("sandbox-ai update failed", f"{available}: {exc}", "critical")
        return 1
    out(f"installed: {old} -> {new}")
    tell(f"sandbox-ai updated: {old} -> {new}", f"release notes: {notes_link(new, chosen['source'])}; a running "
         "app or sandbox goes on with the version it started with (restart the app to use the new one)")
    return 0


# -- the command line's hint ------------------------------------------------------------------------------------------

def spawn_check():
    """The daily check in a process of its own (it outlives the command that starts it)."""
    subprocess.Popen([sys.executable, "-I", str(HERE / "main.py"), "update", "--scheduled"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def cli_hint(command, argv, stream=None, clock=now, interactive=None, start=spawn_check):
    """The command line's part of notify: on a terminal (standard output and error), never with --json, at most once
    a day: one line on standard error about a newer signed release. Without a timer the day's check is started here
    too (in the background). Never an error, never slow (two small files are read)."""
    stream = stream or sys.stderr
    if command in QUIET_COMMANDS or "--json" in argv or "-j" in argv:
        return None
    if interactive is None:
        interactive = sys.stderr.isatty() and sys.stdout.isatty()
    if not interactive:
        return None
    chosen = settings()
    if chosen["mode"] == "off":
        return None
    at = clock()
    state = load_state()
    line = None
    result = state.get("result") or {}
    installed = own_version()
    if result.get("signature") == "good" and newer(result.get("available"), installed):
        last = state.get("hinted")
        if not isinstance(last, (int, float)) or at - last >= HINT_EVERY or at < last:
            info = installation()
            way = "sandbox-ai update" + (" --apply" if pending_release(info) else "") if self_updating(info) \
                else manual_way(info)
            line = f"sandbox-ai: {result['available']} is available (installed: {installed}): {way}"
            print(line, file=stream)
            state["hinted"] = at
            save_state(state)
    claimed = state.get("claimed")
    # Only notify's check: download and auto have a timer (update mode makes it), and an install never starts from a
    # command that may be a start itself.
    if chosen["mode"] == "notify" and check_due(state, at) and not (isinstance(claimed, (int, float)) and 0 <= at - claimed < RETRY_EVERY) and \
            not any(path.is_file() for path in (defaults_path().parent.parent / "systemd" / "user" /
                                                f"{TIMER_NAME}.timer",
                                                Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist")):
        state["claimed"] = at  # (one background check at a time; it records its own result)
        save_state(state)
        with contextlib.suppress(OSError):
            start()
    return line


# -- the command ------------------------------------------------------------------------------------------------------

MODE_HELP = """Usage:  sandbox-ai update mode [MODE] [OPTIONS]

How this installation updates itself (one setting, update_mode in ~/.config/sandbox-ai/defaults.json). Without
MODE: the mode, the last check and its result, the next check.

Modes:
  off       Nothing is checked unless asked (sandbox-ai update --check)
  notify    The default: a check once a day; a newer signed release is announced (a desktop notification,
            the app's banner, one line on the command line, the notify channels' event "update")
  download  Also downloads it and checks it in full (signature, checksums): sandbox-ai update --apply
  auto      Also installs it in the window, while nothing of this installation runs (never a downgrade,
            never unsigned); after it a notification with the old and the new version

Options:
  --at HH:MM[-HH:MM]  auto's window, local time (default 03:00-05:00; HH:MM alone: that hour)
  --source URL        The repository the releases come from (default: SANDBOX_AI_SOURCE or GitHub's);
                      an empty URL forgets it
  -j, --json          Print the result as JSON (for programs)
  -h, --help          Show this help

download and auto need a copy that replaces itself: install.sh's installation of a user's own, the .pyz,
the AppImage, the macOS app or a pip installation in a folder you may write. The others (a git checkout, a
system installation, a copy you cannot write) get notify, which says how to update them.

The timer: a systemd user timer or a LaunchAgent (sandbox-ai-update), every 15 minutes, runs
sandbox-ai update --scheduled as you; mode off removes it.

Examples:
  sandbox-ai update mode                       the mode and the last check
  sandbox-ai update mode auto --at 02:00-04:00 install new releases at night
  sandbox-ai update mode off                   never look by itself"""


def mode_command(argv, out=print, err=None):
    err = err or (lambda text: print(text, file=sys.stderr))
    if any(word in ("-h", "--help") for word in argv):
        out(MODE_HELP)
        return 0
    parser = argparse.ArgumentParser(prog="sandbox-ai update mode", add_help=False)
    parser.add_argument("mode", nargs="?", choices=MODES)
    parser.add_argument("--at", help="auto's window, local time (HH:MM[-HH:MM], default 03:00-05:00)")
    parser.add_argument("--source", help="the repository the releases come from (empty: forget it)")
    parser.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    try:
        a = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        if a.mode is None:
            if a.at is not None or a.source is not None:
                raise ValueError("--at and --source go with a mode: sandbox-ai update mode MODE --at ...")
            if a.json:
                out(json.dumps(status(), indent=2))
                return 0
            return show(out=out)
        said = []
        set_mode(a.mode, a.at, a.source, out=(said.append if a.json else out))
        if a.json:
            out(json.dumps({**status(), "said": said}, indent=2))
        return 0
    except ValueError as exc:
        err(f"sandbox-ai update mode: {printable(exc, 600)}")
        return 2


def main(argv, out=print, err=None):
    """`sandbox-ai update mode ...`, --apply, --install, --rollback, --scheduled (main.py hands these here)."""
    err = err or (lambda text: print(text, file=sys.stderr))
    if argv[:1] == ["mode"]:
        return mode_command(argv[1:], out, err)
    while_running = "--while-running" in argv
    words = [word for word in argv if word != "--while-running"]
    try:
        if words == ["--scheduled"]:
            return scheduled(out=out)
        if words == ["--apply"]:
            old, new = apply(while_running=while_running, out=out)
            out(f"sandbox-ai update: installed {new} (was {old}); release notes: "
                f"{notes_link(new, settings()['source'])}")
            return 0
        if words == ["--install"]:
            done = install_now(while_running=while_running, out=out)
            if done is not None:
                out(f"sandbox-ai update: installed {done[1]} (was {done[0]}); release notes: "
                    f"{notes_link(done[1], settings()['source'])}")
            return 0
        if words == ["--rollback"]:
            old, new = rollback(while_running=while_running)
            out(f"sandbox-ai update: back to {new} (was {old}); download and auto leave {old} out from now on")
            return 0
    except Busy as exc:
        err("sandbox-ai update: not now, this installation is in use: " + "; ".join(exc.reasons) +
            " (stop them first, or --while-running: they go on with the version they started with)")
        return BUSY_STATUS
    except (ValueError, OSError) as exc:
        err(f"sandbox-ai update: {printable(exc, 600)}")
        return 1
    err(f"sandbox-ai update: {' '.join(argv)!r}: --apply, --install, --rollback and --scheduled take no other options "
        "(--while-running only)")
    return 2


def signature_line(result, trusted):
    """The "signature: ..." line of --check, as install.sh words it."""
    kind = result.get("signature")
    if kind == "good":
        return f"good, by the trusted key {result.get('signer')}"
    if kind == "none":
        return "none (the release has no SHA256SUMS.sig)"
    if kind == "unchecked":
        return "not checked: no release key is trusted yet"
    return f"BAD: {'not signed by a trusted release key' if trusted else 'no release key is trusted'}"


def package_check(info, source, out):
    """`sandbox-ai update --check` of a package, a pip installation or a git checkout: the lines install.sh --check
    prints, and what it found kept as the last check (update mode, the app, the command line's hint)."""
    installed = own_version()
    where = f"a git checkout, {info['tree']}" if info["kind"] == "checkout" else \
        packages().target(info)[0] or WHAT.get(flavour(info), info["kind"])
    out(f"installed: {installed} ({where})")
    found = packages().check(source)
    result = {"time": now(), "installed": installed, "source": source, **found,
              "newer": bool(found.get("signature") == "good" and newer(found.get("available"), installed))}
    with contextlib.suppress(OSError):
        save_state(record_check(result))
    if found.get("available") is None:
        raise ValueError(found.get("error") or "the release source did not answer")
    out(f"available: {found['available']} ({source})")
    if version_key(found["available"]) < version_key(installed):
        out(f"           older than the installed {installed}: an update refuses it without --allow-downgrade")
    try:
        keys = load_module("release_keys")
        trusted = [keys.fingerprint(key) for _, key in keys.load()]
    except ValueError as exc:
        raise ValueError(printable(exc)) from None
    out(f"signature: {signature_line(found, trusted)}")
    out(f"trusted keys: {', '.join(trusted) or 'none'} (this copy's sandbox/release-signers)")
    if found.get("error"):
        out(f"           {found['error']}")
    if trusted and found.get("signature") != "good":
        out("           an update refuses this release")
    way = manual_way(info)
    out(f"update: {way}" + (" (it replaces this copy; the version before is kept: sandbox-ai update --rollback)"
                            if way == "sandbox-ai update" else ""))
    return 0


PACKAGE_OPTIONS = {"--check": 0, "--source": 1, "--version": 1, "--allow-downgrade": 0, "--force": 0,
                   "--while-running": 0}


def package_main(argv, out=print, err=None):
    """`sandbox-ai update [--check] [--version X.Y.Z] [--source URL] [--allow-downgrade] [--force] [--while-running]`
    of a package or a pip installation (main.py hands it here): the release's file for this copy, downloaded, checked
    and put in its place. install.sh's other options (--user, --no-app-deps, ...: sandbox-ai hosts update sends
    some) mean nothing here and are passed over."""
    err = err or (lambda text: print(text, file=sys.stderr))
    given, words, passed = {}, list(argv), packages().INSTALL_SH_OPTIONS
    while words:
        word = words.pop(0)
        name, equals, value = word.partition("=")
        if name in PACKAGE_OPTIONS and (not equals or PACKAGE_OPTIONS[name]):
            if PACKAGE_OPTIONS[name]:
                if not equals and not words:
                    err(f"sandbox-ai update: {name} needs a value")
                    return 2
                given[name] = value if equals else words.pop(0)
            else:
                given[name] = True
        elif word in passed:
            del words[:passed[word]]
        else:
            err(f"sandbox-ai update: {printable(word, 80)!r} is no option of this copy's update (--check, --version "
                "X.Y.Z, --source URL, --allow-downgrade, --force, --while-running; see sandbox-ai update --help)")
            return 2
    info = installation()
    try:
        version = given.get("--version")
        if version is not None:
            version = version[1:] if version.startswith("v") else version
            if not VERSION.fullmatch(version):
                raise ValueError("--version expects X.Y.Z")
        source = release_source(check_source(given["--source"], "--source") if "--source" in given else
                                settings()["source"])
        if given.get("--check"):
            return package_check(info, source, out)
        done = install_now(info, version, source, bool(given.get("--while-running")),
                           bool(given.get("--allow-downgrade")), bool(given.get("--force")), out=out)
        if done is not None:
            out(f"sandbox-ai update: installed {done[1]} (was {done[0]}); the version before is kept (sandbox-ai "
                f"update --rollback); release notes: {notes_link(done[1], source)}")
            if flavour(info) == "macos-app":
                out("sandbox-ai update: restart the app (and sessions started before) to use the new version")
        return 0
    except Busy as exc:
        err("sandbox-ai update: not now, this copy is in use: " + "; ".join(exc.reasons) +
            " (stop them first, or --while-running: they go on with the version they started with)")
        return BUSY_STATUS
    except (ValueError, OSError) as exc:
        err(f"sandbox-ai update: {printable(exc, 900)}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
