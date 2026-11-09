"""Chromium inside the app (QtWebEngine: the terminal tabs, xterm.js): its own sandbox, and the check before a web
view is made.

On Linux, Chromium's sandbox puts its zygote and renderers into new user, PID and network namespaces. When the kernel
cannot give them right now (user.max_user_namespaces or a related limit reached - every rootless podman or bubblewrap
sandbox holds some - or no memory left), Chromium does not fail softly: a FATAL check (sandbox/linux/services/
credentials.cc) ends the whole process, the app with it. So before each new web view a short-lived child process
creates the same namespaces (probe()); when it cannot, the app makes no web view and opens the command in a terminal
window instead, saying why and what to do. The next terminal checks again (namespaces may be free by then).

Settings > Window > "Use web views without Chromium's sandbox" (web_without_sandbox, off by default) sets
QTWEBENGINE_DISABLE_SANDBOX=1 for the next start (apply_setting): no namespaces are needed then, and nothing is
checked. Qt's own variable, or --no-sandbox in QTWEBENGINE_CHROMIUM_FLAGS, does the same.
"""
from __future__ import annotations

from collections import namedtuple
import os
import shutil
import subprocess
import sys

SANDBOX_OFF = "QTWEBENGINE_DISABLE_SANDBOX"
SETTING = "web_without_sandbox"
LIMITS = ("user/max_user_namespaces", "user/max_pid_namespaces", "user/max_net_namespaces")

# What the child does: the namespaces Chromium's zygote gets (CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET), then a
# process in the new PID namespace that starts one more (as the zygote's reaper does). It prints "ok", or the step
# that failed and its errno name. Python 3.12+ has os.unshare; before, libc's unshare through ctypes.
CHILD = r"""
import ctypes, errno, os, sys
def fail(step, number):
    sys.stdout.write("%s %s\n" % (step, errno.errorcode.get(number, number)))
    sys.stdout.flush()
    os._exit(1)
flags = 0x10000000 | 0x20000000 | 0x40000000
try:
    if hasattr(os, "unshare"):
        os.unshare(flags)
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.unshare(flags) != 0:
            raise OSError(ctypes.get_errno(), "unshare")
except OSError as exc:
    fail("unshare", exc.errno)
try:
    pid = os.fork()
except OSError as exc:
    fail("fork", exc.errno)
if pid == 0:
    try:
        inner = os.fork()
    except OSError as exc:
        fail("fork-in-namespace", exc.errno)
    if inner == 0:
        os._exit(0)
    os.waitpid(inner, 0)
    os._exit(0)
if os.waitpid(pid, 0)[1] != 0:
    os._exit(1)
sys.stdout.write("ok\n")
"""

# ok: True when a web view may be made (namespaces created, nothing to check, or the check itself could not run);
# checked: whether a probe ran; error: the errno name (ENOMEM, ENOSPC, EPERM ...) or "timeout"; detail: for the log.
Probe = namedtuple("Probe", "ok checked error detail")


def sandbox_off(environ=None):
    """Whether Chromium runs without its sandbox in this process (QTWEBENGINE_DISABLE_SANDBOX, or --no-sandbox in
    QTWEBENGINE_CHROMIUM_FLAGS): then it needs no namespaces."""
    environ = os.environ if environ is None else environ
    if environ.get(SANDBOX_OFF, "") not in ("", "0"):
        return True
    return "--no-sandbox" in environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()


def apply_setting(settings, environ=None):
    """At the start, before QtWebEngine is loaded: the setting web_without_sandbox sets QTWEBENGINE_DISABLE_SANDBOX=1
    (a variable you set yourself stays as it is). Returns whether Chromium runs without its sandbox."""
    from .backend import setting
    environ = os.environ if environ is None else environ
    if setting(SETTING, settings) and SANDBOX_OFF not in environ:
        environ[SANDBOX_OFF] = "1"
    return sandbox_off(environ)


def limits(proc="/proc/sys"):
    """The kernel's namespace limits, for the log ("max_user_namespaces=63432 ...")."""
    found = []
    for name in LIMITS:
        try:
            with open(os.path.join(proc, name)) as stream:
                found.append(f"{os.path.basename(name)}={stream.read().strip()}")
        except OSError:
            pass
    return " ".join(found)


def _python_probe(run, timeout):
    done = run([sys.executable, "-I", "-S", "-c", CHILD], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               stdin=subprocess.DEVNULL, text=True, timeout=timeout)
    words = (done.stdout or "").split()
    if done.returncode == 0 and words[-1:] == ["ok"]:
        return Probe(True, True, "", "namespaces created")
    if done.returncode == 1 and len(words) == 2:
        return Probe(False, True, words[1], f"{words[0]}: {words[1]}")
    return None  # the child itself did not run (no ctypes, a broken interpreter): the unshare command instead


def _unshare_probe(run, timeout):
    program = shutil.which("unshare")
    if program is None:
        return None
    done = run([program, "--user", "--pid", "--net", "--fork", "true"], stdout=subprocess.PIPE,
               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, timeout=timeout)
    if done.returncode == 0:
        return Probe(True, True, "", "namespaces created (unshare)")
    lines = [line.strip() for line in (done.stderr or "").splitlines() if line.strip()]
    why = lines[-1][:200] if lines else f"exit status {done.returncode}"
    return Probe(False, True, "ENOMEM" if "Cannot allocate memory" in why else
                 "ENOSPC" if "No space left" in why else "EPERM" if "not permitted" in why else "error", why)


def probe(timeout=5, run=subprocess.run, platform=None, environ=None):
    """Can Chromium's sandbox get its namespaces right now? Tried in a short-lived child process (never in the app's
    own process, which runs Qt's threads)."""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return Probe(True, False, "", "not Linux: no namespaces needed")
    if sandbox_off(environ):
        return Probe(True, False, "", f"Chromium's sandbox is off ({SANDBOX_OFF})")
    last = ""
    for attempt in (_python_probe, _unshare_probe):
        try:
            result = attempt(run, timeout)
        except subprocess.TimeoutExpired:
            return Probe(False, True, "timeout", f"the check did not end within {timeout} seconds")
        except OSError as exc:
            result, last = None, f": {exc}"
        if result is not None:
            return result
    return Probe(True, False, "", "could not be checked (no Python child, no unshare command)" + last)


def check(timeout=5, **kwargs):
    """probe(), written to the app's log (the result, the step, the errno name and the kernel's limits; nothing
    else)."""
    from . import applog
    result = probe(timeout, **kwargs)
    if result.ok:
        level = "WARNING" if not result.checked and "could not be checked" in result.detail else "INFO"
        applog.write(f"{applog.header(level, 'webengine')}\n  Chromium's sandbox before a terminal tab: "
                     f"{result.detail}\n")
        return result
    applog.write(f"{applog.header('WARNING', 'webengine')}\n  Chromium's sandbox cannot get its namespaces now "
                 f"({result.detail}; {limits() or 'limits unknown'}): no web view, a terminal window instead\n")
    return result


def unavailable_text(result):
    """Why no terminal tab now, and what you can do (wrapped by the label or dialog that shows it)."""
    from .i18n import tr
    if result.error in ("EPERM", "EACCES"):
        why = tr("this system does not allow unprivileged user namespaces ({error})", error=result.error)
    elif result.error == "timeout":
        why = tr("the check for a user namespace did not finish in time")
    else:
        why = tr("no user namespace is available ({error}: user.max_user_namespaces reached or out of memory)",
                 error=result.error)
    return tr("Terminal tabs show a web view, and Chromium's sandbox for it cannot start right now: {why}. Making "
              "the view now would end the whole app, so the command opens in a terminal window instead.\n\n"
              "What you can do: end sandboxes you no longer need, or raise the limit (sysctl "
              "user.max_user_namespaces); the next terminal checks again. Or switch on Settings > Window > \"Use "
              "web views without Chromium's sandbox\" (applies at the next start).", why=why)
