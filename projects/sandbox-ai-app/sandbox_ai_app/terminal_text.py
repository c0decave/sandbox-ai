"""What the terminal tabs read in their text, without Qt: links (also when a URL is split over several rows),
the text of a selection, what a paste would send, OSC 52 clipboard requests, and the sign-in links and codes a
login prints.

Rows come from xterm.js one string per row, one character per cell (terminal.js: a wide character's second cell is
"\\0", a cell of several code points one "\\ufffd"), so a character's index is its column. A row "wrapped" continues
the row before it (xterm.js's soft wrap). tmux and the agents' own interfaces (Claude Code's Ink) draw a long line as
rows of their own instead - a URL then fills its row to the edge (tmux) or to the edge of its box (Ink) and goes on at
the start of the next row, or at the same indentation: links_in_rows joins those when the row ends in the URL and
the next one starts with what can only be its rest (see continues()).
"""
from __future__ import annotations

import base64
import binascii
import codecs
from collections import namedtuple
import ipaddress
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

# -- links in rows --

URL_START = re.compile(r"https?://", re.IGNORECASE)
URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;=%")
BORDERS = frozenset("│┃║|╎╏┆┇┊┋")  # a box's side, as tmux's pane borders and Ink's boxes draw it
URLISH = frozenset("/?&=%#_~+0123456789")  # a row that goes on with one of these is a URL's rest, not a word
TRAILING = ".,;:!?'\""  # after a URL in prose: not part of it
EDGE_SLACK = 4  # columns between the end of a URL and the row's edge (a box's padding and border) still "at the edge"
MAX_ROWS = 40  # rows one link may span
LINK_LIMIT = 8192  # characters of one URL

Link = namedtuple("Link", "url start end")  # start, end: (row, column) of the first and last character


def pad(row, cols):
    row = str(row)[:cols]
    return row + " " * (cols - len(row))


def indent(row):
    """The column of the first character of ROW that is neither a space nor a box's side (len(row) for none)."""
    for column, char in enumerate(row):
        if char != " " and char not in BORDERS:
            return column
    return len(row)


def blank_after(row, column):
    """Whether ROW holds only spaces and box sides from COLUMN on."""
    return all(char == " " or char in BORDERS for char in row[column:])


def at_edge(row, column, cols):
    """Whether a URL whose last character is at COLUMN of ROW ends at the row's edge: the last column (tmux draws a
    wrapped line so), or near it - or near the side of the box it is in - with only spaces and that side after it
    (Ink's boxes)."""
    if column >= cols - 1:
        return True
    edge = cols - 1
    right = len(row.rstrip(" "))
    if right - 1 > column and row[right - 1] in BORDERS:
        edge = right - 2  # a box narrower than the terminal: its side is the edge
    return column >= edge - EDGE_SLACK and blank_after(row, column + 1)


def run_of_url(row, column):
    """The end (exclusive) of the URL characters of ROW from COLUMN on."""
    end = column
    while end < len(row) and row[end] in URL_CHARS:
        end += 1
    return end


def continues(rows, cols, last_row, start_row):
    """Where a URL whose last character so far is at the end of row LAST_ROW goes on in row START_ROW (the next
    row, not a soft wrap): (column, end) of its rest there, or None.

    It goes on when the next row starts with URL characters - at its first column (tmux) or at the indentation of
    the URL's own row (an indented or boxed text) - that do not begin a URL of their own, and that are either the
    whole rest of that row's text or contain what a word of prose does not (/ ? & = % # _ ~ + or a digit): a row
    "Paste code here > " after a URL that happens to end at the edge is not taken as its rest."""
    if start_row >= len(rows):
        return None
    row, before = rows[start_row], rows[last_row]
    column = indent(row)
    if column >= len(row) or column not in (0, indent(before)) or row[column] not in URL_CHARS:
        return None
    if URL_START.match(row, column):
        return None
    end = run_of_url(row, column)
    piece = row[column:end]
    if blank_after(row, end) or any(char in URLISH for char in piece):
        return column, end
    return None


def strip_trailing(chars):
    """CHARS ([(char, row, column)] of a URL) without what prose puts after one: a full stop, a comma, a closing
    quote, and a closing bracket that opens nowhere in the URL."""
    while chars:
        last = chars[-1][0]
        text = "".join(c for c, _, _ in chars)
        if last in TRAILING or (last == ")" and text.count("(") < text.count(")")) or \
                (last == "]" and text.count("[") < text.count("]")):
            chars.pop()
            continue
        break
    return chars


def logical_lines(rows, wrapped):
    """[[row index, ...]] of ROWS: a row marked wrapped belongs to the line of the row before it."""
    lines = []
    for index in range(len(rows)):
        if index and index < len(wrapped) and wrapped[index] and lines:
            lines[-1].append(index)
        else:
            lines.append([index])
    return lines


def links_in_rows(rows, wrapped=(), cols=None, first=0, touching=None):
    """The http(s) links in ROWS (strings, one per row; WRAPPED[i]: row i continues row i-1), as Link(url, start,
    end) with (row, column) of their first and last character, rows counted from FIRST. A link goes on over soft
    wraps, and over hard ones where continues() says so. TOUCHING: only the links that reach that row (FIRST
    counted)."""
    cols = cols or max((len(r) for r in rows), default=0)
    if cols <= 0:
        return []
    rows = [pad(r, cols) for r in rows]
    lines = logical_lines(rows, list(wrapped))
    line_of = {index: number for number, line in enumerate(lines) for index in line}
    found, taken = [], set()
    for line in lines:
        text = "".join(rows[i] for i in line)
        for match in URL_START.finditer(text):
            position = match.start()
            row, column = line[position // cols], position % cols
            if (row, column) in taken:
                continue  # the rest of a URL that began in a row before
            chars = []
            end = run_of_url(text, position)
            chars += [(text[p], line[p // cols], p % cols) for p in range(position, end)]
            # Over hard wraps: while the URL ends at its row's edge and the next row goes on with it.
            spanned = 1
            while chars and spanned < MAX_ROWS and len(chars) < LINK_LIMIT:
                _, last_row, last_column = chars[-1]
                if not at_edge(rows[last_row], last_column, cols):
                    break
                following = line_of.get(last_row + 1)
                if following is None or lines[following][0] != last_row + 1:
                    break  # (the next row wraps softly: the URL ended there already)
                rest = continues(rows, cols, last_row, last_row + 1)
                if rest is None:
                    break
                next_line = lines[following]
                next_text = "".join(rows[i] for i in next_line)
                stop = run_of_url(next_text, rest[0])
                chars += [(next_text[p], next_line[p // cols], p % cols) for p in range(rest[0], stop)]
                spanned += 1
            chars = strip_trailing(chars[:LINK_LIMIT])
            url = "".join(c for c, _, _ in chars)
            if not plausible(url):
                continue
            taken.update((r, c) for _, r, c in chars)
            found.append(Link(url, (chars[0][1] + first, chars[0][2]), (chars[-1][1] + first, chars[-1][2])))
    if touching is not None:
        found = [link for link in found if link.start[0] <= touching <= link.end[0]]
    return found


def plausible(url):
    """An http(s) URL with a host."""
    try:
        parts = urlsplit(url)
        return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


# -- the text of a selection --

BOX_DRAWING = (0x2500, 0x259F)


def drawing(char):
    return BOX_DRAWING[0] <= ord(char) <= BOX_DRAWING[1]


def joined_selection(segments, cols):
    """The text of a selection of SEGMENTS ([{"text", "wrapped", "full"}], one per row: "wrapped" when the next row
    continues it softly, "full" when it reaches the last column and that cell is not empty). Rows are joined
    without a line break over a soft wrap, and where a row is full and the next goes on without a space - tmux and
    the agents' interfaces draw a long line as full rows of their own (a line of box drawing is not joined)."""
    out = []
    for index, segment in enumerate(segments):
        text = str(segment.get("text", "")).replace("\0", "")
        last = index == len(segments) - 1
        if last:
            out.append(text.rstrip(" "))
            break
        following = str(segments[index + 1].get("text", "")).replace("\0", "")
        if segment.get("wrapped"):
            out.append(text)
            continue
        stripped = text.rstrip(" ")
        if segment.get("full") and stripped and following[:1] not in ("", " ") and \
                not drawing(stripped[-1]) and not drawing(following[0]) and \
                len(text) >= 1 and text[-1] != " " and stripped[-1] not in BORDERS and following[0] not in BORDERS:
            out.append(stripped)
            continue
        out.append(stripped + "\n")
    return "".join(out)


# -- pasting --

PASTE_LIMIT = 1 << 20  # characters: the pseudo-terminal bridge takes a megabyte per write
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
CONTROL_NAMES = {"\x1b": "ESC", "\x00": "NUL", "\x7f": "DEL", "\x08": "BS", "\x03": "Ctrl-C", "\x04": "Ctrl-D",
                 "\x1a": "Ctrl-Z", "\x9b": "CSI", "\x9d": "OSC"}
Paste = namedtuple("Paste", "text controls lines runs too_large")


def control_name(char):
    return CONTROL_NAMES.get(char) or (f"Ctrl-{chr(ord(char) + 64)}" if ord(char) < 0x20 else f"U+{ord(char):04X}")


def check_paste(text, bracketed=False):
    """What pasting TEXT would do: Paste(text, controls - the names of the control characters in it (an escape
    sequence starts with ESC; xterm.js would send them as they are), lines - how many lines it has, runs - whether
    a line break would act as Enter at once (no bracketed paste: a shell runs each line), too_large)."""
    text = str(text)
    controls = sorted({control_name(c) for c in CONTROL.findall(text)})
    lines = len(re.split(r"\r\n|\r|\n", text.rstrip("\r\n"))) if text.strip("\r\n") else 0
    has_break = bool(re.search(r"[\r\n]", text))
    return Paste(text, controls, lines, has_break and not bracketed, len(text) > PASTE_LIMIT)


def without_controls(text):
    """TEXT without its control characters (tabs and line breaks stay)."""
    return CONTROL.sub("", str(text))


def one_line(text):
    """TEXT as one line: line breaks at its end removed, those inside it a space each."""
    return re.sub(r"[ \t]*(?:\r\n|\r|\n)[ \t]*", " ", str(text).rstrip("\r\n"))


def needs_question(check):
    return bool(check.controls) or check.runs


# -- OSC 52: a program sets the clipboard --

OSC52_LIMIT = 1 << 20  # characters of text a program may put on the clipboard
Osc52 = namedtuple("Osc52", "kind text targets")  # kind: "set", "read" (never answered), "clear" or "invalid"


def parse_osc52(payload):
    """An OSC 52 request's PAYLOAD ("TARGETS;BASE64", e.g. "c;aGk="): Osc52("set", text, targets), "read" for
    "?", "clear" for nothing, "invalid" for anything else (bad base64, too long, not text)."""
    targets, sep, data = str(payload).partition(";")
    if not sep or not re.fullmatch(r"[cpqs0-7]*", targets):
        return Osc52("invalid", "", targets)
    if data == "?":
        return Osc52("read", "", targets)
    if not data:
        return Osc52("clear", "", targets)
    if len(data) > (OSC52_LIMIT * 4) // 3 + 4:
        return Osc52("invalid", "", targets)
    try:
        text = base64.b64decode(data, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return Osc52("invalid", "", targets)
    return Osc52("set", text, targets)


# -- what a login prints --

ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[PX^_][^\x1b]*\x1b\\|"
                     r"\x1b[ -/]*[0-~]|[\x00-\x08\x0b-\x1f\x7f]")
OSC8 = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
PLAIN_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
DEVICE_CODE = re.compile(r"(?<![A-Za-z0-9-])([A-Z0-9]{4,8}-[A-Z0-9]{4,8})(?![A-Za-z0-9-])")
WATCH_LIMIT = 256 << 10  # characters of a login's output kept to look in


class LoginWatch:
    """Reads a login's output (feed(bytes)) and finds the links it prints - those of OSC 8 hyperlinks first (their
    target whole, however the text is wrapped), and plain ones - and a one-time code printed after a device page's
    link (Codex's device code login, GitHub's): urls (in order, each once) and code."""

    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending, self.text, self.urls, self.code = "", "", [], None

    def feed(self, data):
        """New output DATA (bytes); returns the links found in it that were not found before."""
        self.pending += self.decoder.decode(data)
        # Up to the last line end: an escape sequence or URL cut by the chunk's end waits for the rest.
        cut = max(self.pending.rfind("\n"), self.pending.rfind("\r"))
        if cut < 0:
            if len(self.pending) < 8192:
                return []
            cut = len(self.pending) - 1
        chunk, self.pending = self.pending[:cut + 1], self.pending[cut + 1:]
        new = []
        for target in OSC8.findall(chunk):
            target = strip_trailing([(c, 0, 0) for c in target.strip()])
            new.append("".join(c for c, _, _ in target))
        plain = ESCAPES.sub("", chunk.replace("\r\n", "\n"))
        for match in PLAIN_URL.finditer(plain):
            target = strip_trailing([(c, 0, 0) for c in match.group()])
            new.append("".join(c for c, _, _ in target))
        self.text = (self.text + plain)[-WATCH_LIMIT:]
        found = []
        for url in new:
            if plausible(url) and url not in self.urls:
                self.urls.append(url)
                found.append(url)
        if self.code is None:
            self.code = device_code(self.text, self.urls)
        return found


def device_code(text, urls):
    """The one-time code TEXT shows after the link of a device page among URLS (within a few lines), or None."""
    for url in urls:
        if flow_of(url).kind != "device":
            continue
        at = text.find(url)
        if at < 0:
            continue
        after = text[at + len(url):at + len(url) + 600]
        match = DEVICE_CODE.search(after)
        if match:
            return match.group(1)
    return None


# -- sign-in flows --

Flow = namedtuple("Flow", "kind redirect state")
CODE_PATHS = ("/oauth/code/callback",)  # a provider's page that shows the code to paste ("CODE#STATE")
DEVICE_PATHS = ("/codex/device", "/login/device", "/device", "/activate")


def query_value(url, name):
    try:
        values = parse_qs(urlsplit(url).query, keep_blank_values=True).get(name)
    except ValueError:
        return None
    return values[0] if values else None


def loopback(host):
    """Whether HOST (a name or an address) is this machine: localhost, a name below .localhost, 127.0.0.0/8, ::1,
    0.0.0.0."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def private_address(host):
    """Whether HOST is an address of this machine or of a private or link-local network (what a web page must not
    reach from the login browser: the host's own services and the local network) - every address that is not
    global: also a cloud's metadata service (169.254.169.254, link-local; 100.100.100.200) and the shared range of
    carriers and Tailscale (100.64.0.0/10). A name is not an address: NameChecks resolves it."""
    if loopback(host):
        return True
    try:
        address = ipaddress.ip_address((host or "").strip("[]").partition("%")[0])
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return address.is_private or address.is_link_local or address.is_multicast or address.is_reserved or \
        not address.is_global


def system_addresses(host):
    """The addresses HOST resolves to here (the system's resolver, as Chromium asks it)."""
    return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


class NameChecks:
    """Which names the login browser may reach: a name that resolves to this machine or the local network
    (private_address of any of its addresses: localtest.me, 127.0.0.1.nip.io, nas.lan, a metadata service's name) is
    "local", one that does not resolve "unresolved", else "open". A request cannot wait in Qt's interceptor, so a
    name is resolved in the background (START, a thread each) and its answer kept for TTL seconds (FAILED_TTL for no
    answer); until then lookup() says None and the request is refused - the caller asks again once DONE(host,
    verdict, address) was called (from the worker's thread). A literal address is decided at once. What it cannot
    hold: a name whose answer changes between this lookup and Chromium's own (DNS rebinding) within TTL."""

    def __init__(self, resolve=None, start=None, clock=time.monotonic, ttl=120, failed_ttl=15, limit=1024):
        self.resolve = resolve or system_addresses
        self.start = start or (lambda work: threading.Thread(target=work, daemon=True, name="login-names").start())
        self.clock, self.ttl, self.failed_ttl, self.limit = clock, ttl, failed_ttl, limit
        self.known, self.waiting, self.lock = {}, {}, threading.Lock()

    @staticmethod
    def name(host):
        return (host or "").strip().lower().rstrip(".")

    def cached(self, host):
        with self.lock:
            hit = self.known.get(self.name(host))
            if hit is None or hit[2] < self.clock():
                return None
            return hit[0], hit[1]

    @staticmethod
    def classify(addresses):
        addresses = [str(a) for a in addresses or []]
        if not addresses:
            return "unresolved", None
        local = next((a for a in addresses if private_address(a)), None)
        return ("local", local) if local else ("open", addresses[0])

    def lookup(self, host, done=None):
        """(verdict, address) of HOST, or None while it is looked up (DONE is called then, once it is known)."""
        host = self.name(host)
        try:
            ipaddress.ip_address(host.strip("[]").partition("%")[0])
            return ("local" if private_address(host) else "open"), host.strip("[]")
        except ValueError:
            pass
        if loopback(host):
            return "local", host
        hit = self.cached(host)
        if hit is not None:
            return hit
        with self.lock:
            first = host not in self.waiting
            if first:
                self.waiting[host] = []
            elif done is not None:
                self.waiting[host].append(done)
                return None
        if first:
            self.start(lambda: self.work(host))
        with self.lock:
            if host in self.waiting:  # (still being looked up: told when it is known)
                if done is not None:
                    self.waiting[host].append(done)
                return None
        return self.cached(host)

    def work(self, host):
        try:
            addresses = self.resolve(host)
        except (OSError, UnicodeError, ValueError):
            addresses = []
        verdict, address = self.classify(addresses)
        with self.lock:
            if len(self.known) >= self.limit:
                self.known.clear()
            self.known[host] = (verdict, address, self.clock() + (self.ttl if verdict != "unresolved" else
                                                                  self.failed_ttl))
            callbacks = self.waiting.pop(host, [])
        for callback in callbacks:
            try:
                callback(host, verdict, address)
            except Exception:  # noqa: BLE001 - one caller's failure is not another's
                pass


def flow_of(url):
    """The sign-in an address starts: Flow(kind, redirect, state). kind "code": an OAuth authorization whose
    redirect_uri is the provider's page that shows a code to paste (Claude Code, OpenCode's Anthropic login); "loopback":
    one that returns to a program on this machine (http://localhost:PORT/...; inside the sandbox that is the sandbox's
    own); "device": a page where a one-time code is entered (Codex's --device-auth, GitHub's device login); "page":
    anything else."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return Flow("page", None, None)
    redirect = query_value(url, "redirect_uri")
    state = query_value(url, "state")
    if redirect:
        try:
            target = urlsplit(redirect)
        except ValueError:
            target = None
        if target is not None and target.scheme == "https" and target.path.rstrip("/").endswith(CODE_PATHS):
            return Flow("code", redirect, state)
        if target is not None and target.scheme in ("http", "https") and loopback(target.hostname):
            return Flow("loopback", redirect, state)
    if parts.path.rstrip("/").endswith(DEVICE_PATHS):
        return Flow("device", None, None)
    return Flow("page", None, None)


def same_endpoint(url, redirect):
    """Whether URL goes to REDIRECT (scheme, host, port and path; the query aside)."""
    try:
        a, b = urlsplit(url), urlsplit(redirect)
        return (a.scheme.lower(), (a.hostname or "").lower(), a.port, a.path.rstrip("/")) == \
            (b.scheme.lower(), (b.hostname or "").lower(), b.port, b.path.rstrip("/"))
    except ValueError:
        return False


Result = namedtuple("Result", "text problem")  # what to paste into the terminal; why not (problem)


def callback_result(flow, url):
    """What the browser at URL hands back to the terminal of FLOW, or None when URL is not the end of FLOW:
    for "code" the provider's code page with ?code=...&state=... gives "CODE#STATE" (what Claude Code reads: it
    splits at the "#"); its state must be the one the sign-in began with. For "loopback" the address itself (it
    was meant for the program in the sandbox; it is never opened on this machine)."""
    if flow.kind not in ("code", "loopback") or not flow.redirect or not same_endpoint(url, flow.redirect):
        return None
    code, state = query_value(url, "code"), query_value(url, "state")
    error = query_value(url, "error")
    if error:
        return Result(None, error)
    if flow.kind == "loopback":
        return Result(url, None) if code else None
    if not code or not state:
        return None
    if flow.state and state != flow.state:
        return Result(None, "state")
    if "#" in code or "#" in state or not re.fullmatch(r"[\x21-\x7e]{1,4096}", code + state):
        return Result(None, "code")
    return Result(f"{code}#{state}", None)


# -- which hosts a login browser opens --

# sandbox-ai's own lists (agent_sandbox.py PROVIDERS and FRONTIER), for when its launcher cannot be loaded; a test
# keeps them the same.
PROVIDERS = {
    "codex": ["api.openai.com", "chatgpt.com", "auth.openai.com"],
    "claude": ["api.anthropic.com", "console.anthropic.com", "claude.ai", "claude.com", "platform.claude.com"],
    "opencode": ["opencode.ai"],
}
LOGIN_FRONTIER = ("opencode",)  # sandbox-ai login gives these --profile frontier (login.py LOGIN_OPTIONS)


def provider_hosts(launcher=None):
    """{agent: [host]} of sandbox-ai's providers (its launcher's PROVIDERS, else the copy here)."""
    providers = getattr(launcher, "PROVIDERS", None)
    if isinstance(providers, dict) and all(isinstance(v, (list, tuple)) for v in providers.values()):
        return {str(k): [str(h) for h in v] for k, v in providers.items()}
    return {k: list(v) for k, v in PROVIDERS.items()}


def login_hosts(agent=None, launcher=None):
    """The hosts a login browser opens without asking: those of AGENT's provider (OpenCode's login: also OpenAI's
    and Anthropic's, as sandbox-ai login gives it --profile frontier); without an agent those of every provider."""
    providers = provider_hosts(launcher)
    if agent is None:
        hosts = [h for v in providers.values() for h in v]
    else:
        hosts = list(providers.get(agent, []))
        if agent in LOGIN_FRONTIER:
            frontier = getattr(launcher, "FRONTIER", None)
            hosts += [str(h) for h in frontier] if isinstance(frontier, (list, tuple)) else \
                providers.get("codex", []) + providers.get("claude", [])
    return tuple(dict.fromkeys(h.lower() for h in hosts))


def host_in(host, hosts):
    """Whether HOST is one of HOSTS or a name below one of them."""
    host = (host or "").lower().rstrip(".")
    return bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def agent_of_url(url, launcher=None):
    """The agent whose provider serves URL's host, or None."""
    host = host_of(url)
    for agent, hosts in provider_hosts(launcher).items():
        if host_in(host, [h.lower() for h in hosts]):
            return agent
    return None


def navigation(url, hosts, allowed=()):
    """What the login browser does with a page it is sent to: "open" (https on HOSTS or a host allowed for this
    sign-in, ALLOWED), "ask" (https elsewhere), "local" (this machine: never loaded - a sign-in's return to a program
    in the sandbox), "refuse" (anything that is not https: http, file, data, javascript ...)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "refuse"
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme in ("http", "https") and private_address(host):
        return "local"
    if scheme != "https" or not host:
        return "refuse"
    if host_in(host, hosts) or host_in(host, allowed):
        return "open"
    return "ask"


def shown_url(url, limit=400):
    """URL for the address bar and the messages: printable, its middle cut when long."""
    text = "".join(c if c.isprintable() else "�" for c in str(url))
    return text if len(text) <= limit else text[:limit // 2] + "…" + text[-limit // 2:]


def without_query(url):
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return url


# -- which tab runs a login --

AGENTS = ("claude", "codex", "opencode")
NOT_A_SIGN_IN = frozenset({"--logout", "-L", "--status", "-s"})  # options of `sandbox-ai login` that print no link


def login_agent(words):
    """The agent of `sandbox-ai login AGENT` in WORDS (a command line: sandbox-ai or its main.py, then its words),
    None for every other command and for --logout or --status."""
    words = [str(word) for word in words]
    at = next((i + 1 for i, word in enumerate(words) if word.rsplit("/", 1)[-1] in ("main.py", "sandbox-ai")), None)
    if at is None or at >= len(words) or words[at] != "login":
        return None
    rest = words[at + 1:]
    if "--" in rest:
        rest = rest[:rest.index("--")]
    if any(word.split("=", 1)[0] in NOT_A_SIGN_IN for word in rest):
        return None
    return next((word for word in rest if word in AGENTS), None)
