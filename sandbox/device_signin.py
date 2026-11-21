"""A device sign-in seen in a login's output: the page to open and the one-time code to enter there.

`sandbox-ai login codex` runs Codex's own `codex login --device-auth` in a sandbox. Codex prints the page
(https://auth.openai.com/codex/device) and a one-time code and waits; nothing in the sandbox can open a browser. This
module reads that output as it passes (Watch), so that the launcher's side can say the code on a line of its own and
open the page in the human's browser - asked first, as every URL a sandbox wants opened is. Hermes' Nous Portal login
and Qwen OAuth are device sign-ins too: theirs carry the code in the link (user_code=...).

Only a link to one of the agent's own provider hosts (the launcher's PROVIDERS) counts, and only one that is a device
page: a link anything else printed is never opened. Standard library only; the terminal relay (run) needs a POSIX
pseudo-terminal.
"""
import codecs
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

# Escape sequences (OSC, CSI, DCS and the like, single ones) and other control characters: taken out before a link
# or a code is looked for.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[PX^_][^\x1b]*\x1b\\|"
                     r"\x1b[ -/]*[0-~]|[\x00-\x08\x0b-\x1f\x7f]")
OSC8 = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
PLAIN_URL = re.compile(r"https://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
# A one-time code as device pages take them: two groups of letters and digits (Codex: ABCD-12345, GitHub: AB12-CD34).
CODE = re.compile(r"(?<![A-Za-z0-9-])([A-Z0-9]{4,8}-[A-Z0-9]{4,8})(?![A-Za-z0-9-])")
# The same from a link's user_code=: what a provider puts there (letters, digits, a dash), nothing to break a line.
QUERY_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{3,39}")
DEVICE_PATHS = ("/codex/device", "/login/device", "/device", "/activate")
URL_LIMIT = 2000
WATCH_LIMIT = 64 << 10  # characters of the output kept to look in
AFTER_URL = 600  # characters after the link in which its code is looked for
QUIET = 0.3  # seconds without output after which the launcher's own lines follow the login's
# The agents whose `sandbox-ai login` is a device sign-in (login.LOGIN_ARGS: Codex's --device-auth, Hermes' Nous
# Portal; Qwen Code's Qwen OAuth, chosen in its dialog).
AGENTS = ("codex", "hermes", "qwen")
# Set by the desktop app in each of its terminal tabs: the app watches the output itself and opens the page in the
# human's browser (or its own), so the command line neither asks nor opens it a second time.
APP_TAB = "SANDBOX_AI_APP_TAB"
# SANDBOX_AI_LOGIN_BROWSER: "ask" (the default: one key, Enter is yes), "open" (without the question), "no" (the
# link and the code are only said).
BROWSER_CHOICE = "SANDBOX_AI_LOGIN_BROWSER"


def host_in(host, hosts):
    """Whether HOST is one of HOSTS or a name below one of them."""
    host = (host or "").lower().rstrip(".")
    return bool(host) and any(host == h or host.endswith("." + h) for h in (str(h).lower() for h in hosts))


def device_page(url, hosts):
    """(True, code or None) when URL is a device sign-in page of one of HOSTS (https, no user part, a device path
    or a user_code in its query - that code, when it is one), else (False, None)."""
    if not isinstance(url, str) or len(url) > URL_LIMIT:
        return False, None
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True)
        host = parts.hostname
    except ValueError:
        return False, None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or \
            not host_in(host, hosts):
        return False, None
    given = (query.get("user_code") or [None])[0]
    code = given if given and QUERY_CODE.fullmatch(given) else None
    if code is None and not parts.path.rstrip("/").endswith(DEVICE_PATHS):
        return False, None
    return True, code


def trimmed(url):
    """URL without what a sentence put after it (a full stop, a closing bracket without its opening one)."""
    while url:
        last = url[-1]
        if last in ".,;:!?'\"":
            url = url[:-1]
        elif last in ")]" and url.count(last) > url.count("(" if last == ")" else "["):
            url = url[:-1]
        else:
            break
    return url


class Watch:
    """Reads a login's output (feed(bytes)) for the device sign-in of an agent whose provider hosts are HOSTS: found
    once its page's link came and its one-time code after it (within a few lines) or in it; then .url and .code are
    set and feed returns True that one time."""

    def __init__(self, hosts):
        self.hosts = tuple(hosts)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending, self.text, self.urls = "", "", []
        self.url = self.code = None

    def feed(self, data):
        if self.code is not None:
            return False
        self.pending += self.decoder.decode(data)
        # Up to the last line end: an escape sequence or a link cut by the chunk's end waits for the rest.
        cut = max(self.pending.rfind("\n"), self.pending.rfind("\r"))
        if cut < 0:
            if len(self.pending) < 8192:
                return False
            cut = len(self.pending) - 1
        chunk, self.pending = self.pending[:cut + 1], self.pending[cut + 1:]
        found = [trimmed(target.strip()) for target in OSC8.findall(chunk)]
        plain = ESCAPES.sub("", chunk.replace("\r\n", "\n").replace("\r", "\n"))
        found += [trimmed(match.group()) for match in PLAIN_URL.finditer(plain)]
        self.text = (self.text + plain)[-WATCH_LIMIT:]
        for url in found:
            if url not in self.urls and device_page(url, self.hosts)[0]:
                self.urls.append(url)
        for url in self.urls:
            code = device_page(url, self.hosts)[1]
            if code is None:
                at = self.text.find(url)
                match = CODE.search(self.text[at + len(url):at + len(url) + AFTER_URL]) if at >= 0 else None
                code = match.group(1) if match else None
            if code is not None:
                self.url, self.code = url, code
                return True
        return False


def lines(url, code):
    """What the launcher says once a device sign-in was seen: the page, and the code on a line of its own."""
    return [f"sandbox-ai login: sign in at {url}", "sandbox-ai login: the one-time code to enter there:", "",
            f"    {code}", ""]


def desktop(environ=None, platform=None):
    """Whether a browser can open on this machine's screen: a Mac, or a display in the environment (a login over
    SSH has none: the page is opened on the machine the human sits at)."""
    environ = os.environ if environ is None else environ
    if (platform or sys.platform) == "darwin":
        return not environ.get("SSH_CONNECTION")
    return bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def opener(environ=None, platform=None, which=shutil.which):
    """The command that opens a URL in the human's browser here ([program, ...]; the URL is appended), or None:
    $BROWSER when it names one program (no shell, no %s), macOS's open, xdg-open, sensible-browser."""
    environ = os.environ if environ is None else environ
    if not desktop(environ, platform):
        return None
    browser = (environ.get("BROWSER") or "").split(os.pathsep)[0].strip()
    if browser and "%" not in browser and " " not in browser and which(browser):
        return [which(browser)]
    programs = ("/usr/bin/open",) if (platform or sys.platform) == "darwin" else ("xdg-open", "sensible-browser")
    for program in programs:
        found = which(program)
        if found:
            return [found]
    return None


def open_page(url, command, popen=subprocess.Popen):
    """URL opened with COMMAND (opener()), never through a shell; False when it could not be started."""
    try:
        popen([*command, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              start_new_session=True)
    except OSError:
        return False
    return True


def choice(environ=None):
    """ask, open or no: SANDBOX_AI_LOGIN_BROWSER; no in a terminal tab of the desktop app (it opens the page)."""
    environ = os.environ if environ is None else environ
    if environ.get(APP_TAB):
        return "no"
    wanted = (environ.get(BROWSER_CHOICE) or "ask").strip().lower()
    return {"1": "open", "yes": "open", "open": "open", "0": "no", "no": "no", "never": "no"}.get(wanted, "ask")


def run(argv, hosts, environ=None, say=None, popen=subprocess.Popen):
    """ARGV on a pseudo-terminal of its own, this terminal relayed to it (raw: every key goes to it, Ctrl-C too), its
    output watched for the device sign-in of HOSTS. Once seen, the page and the code are said (lines) and - with a
    browser here (opener) - the page is opened after one key (Enter or y: yes; anything else: no; choice()). Returns
    (exit status, Watch). Needs a terminal on standard input and output."""
    import pty
    import select
    import signal
    import termios
    import tty
    environ = os.environ if environ is None else environ
    import time
    watch = Watch(hosts)
    # "seen": when the sign-in was found, until it is said - once the login's own lines have come to rest (QUIET
    # seconds without output: its last hint is not cut in two by ours).
    state = {"asking": None, "seen": None}
    out = sys.stdout.fileno()

    def write(text):
        os.write(out, text.replace("\n", "\r\n").encode())

    say = say or write
    master, slave = pty.openpty()

    def size(*_):
        try:
            import fcntl
            fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
        except OSError:
            pass
    size()
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                               preexec_fn=lambda: __import__("fcntl").ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    before = termios.tcgetattr(0)
    old_winch = signal.signal(signal.SIGWINCH, size)

    def found():
        say("\n" + "\n".join(lines(watch.url, watch.code)) + "\n")
        command, how = opener(environ), choice(environ)
        if how == "no":
            return
        if command is None:
            say("sandbox-ai login: no browser opens from here (no desktop in this terminal): open the link on any "
                "device and enter the code\n")
        elif how == "open":
            opened = open_page(watch.url, command, popen)
            say("sandbox-ai login: " + ("opened in your browser" if opened else "could not start your browser: "
                                        "open the link yourself") + "\n")
        else:
            state["asking"] = command
            say("sandbox-ai login: open it in your browser now? [Y/n] ")

    def answered(key):
        command, state["asking"] = state["asking"], None
        if key in (b"\r", b"\n", b"y", b"Y", b"j", b"J"):
            opened = open_page(watch.url, command, popen)
            say("yes\nsandbox-ai login: " + ("opened in your browser" if opened else "could not start your "
                                             "browser: open the link yourself") + "\n")
        else:
            say("no\nsandbox-ai login: not opened: open the link yourself and enter the code\n")
    watched = [master, 0]
    try:
        tty.setraw(0)
        while True:
            try:
                ready, _, _ = select.select(watched, [], [], QUIET if state["seen"] is not None else 0.5)
            except InterruptedError:
                continue
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                os.write(out, data)
                if watch.feed(data) or state["seen"] is not None:
                    state["seen"] = time.monotonic()
            elif state["seen"] is not None and time.monotonic() - state["seen"] >= QUIET:
                state["seen"] = None
                found()
            if 0 in ready:
                try:
                    keys = os.read(0, 4096)
                except OSError:
                    keys = b""
                if not keys:
                    watched.remove(0)  # (this terminal's input ended: the login's output is still shown to its end)
                    continue
                if state["asking"] is not None:
                    answered(keys[:1])
                    keys = keys[1:] if keys[:1] not in (b"\x03", b"\x04", b"\x1b") else keys
                if keys:
                    os.write(master, keys)
            if not ready and process.poll() is not None:
                break
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, before)
        signal.signal(signal.SIGWINCH, old_winch)
        os.close(master)
    return process.wait(), watch
