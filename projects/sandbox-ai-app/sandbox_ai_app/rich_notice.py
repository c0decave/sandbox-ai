"""What a notification is about, at a glance: a small structured renderer for the app's popups.

A notification's text is lines of parts, each part a piece of text with a role:

  session    the start or session it is about                    bold
  address    a host, an IP address and port, a URL               the theme's accent colour, monospaced
  allow, deny, ask                                               the status colours ok, bad, warn; bold
  running, busy, stopped, failed (a state)                       ok, warn, muted, bad; bold
  solution   the command or option that solves it                bold, monospaced
  code       a name to be read exactly (a tool, a rule)          monospaced
  label      "Why:", "To allow it:"                              semibold
  heading    a title's own words                                 semibold
  why, text  why it happened, everything else                    the text colour

Code that makes a notification passes its structure: fmt() fills a translated template with parts (part(),
session(), address(), solution()...), lines() stacks them. Free text - what another program said, an older
sandbox-ai's text - is recognised carefully by recognise(): addresses, backquoted and `sandbox-ai ...` commands,
decision and state words in English and German (never one after a negation: "does not allow" is not green).

Colour is never the only signal: decisions and states are words, set bold; addresses and commands are set in the
monospaced face. role_colors() makes every colour at least 4.5:1 against the notification's background in every
theme (a colour that is not is mixed towards the text colour until it is).

Rich is a str whose value is the plain text as shown - what the log, screen readers, the clipboard and the
desktop's notifications get - so every place that takes a notification's text as a string keeps working.
to_html() escapes every part: what an agent chose is never read as markup. No Qt widgets here.
"""
from __future__ import annotations

import html
import ipaddress
import re

from .files import printable
from .i18n import tr

ROLES = ("text", "heading", "session", "address", "allow", "deny", "ask", "running", "busy", "stopped", "failed",
         "solution", "code", "label", "why")
DECISIONS = ("allow", "deny", "ask")
STATES = ("running", "busy", "stopped", "failed")
BOLD = {"session": 700, "allow": 700, "deny": 700, "ask": 700, "running": 700, "busy": 700, "stopped": 700,
        "failed": 700, "solution": 700, "label": 600, "heading": 600}
MONO_ROLES = ("address", "solution", "code")
MIN_CONTRAST = 4.5  # WCAG AA for body text

# Words of a decision or a state, English and German, lower case: their role.
WORDS = {
    **dict.fromkeys(("allow", "allowed", "allows", "erlaubt", "erlauben", "erlaubte", "zugelassen", "freigegeben"),
                    "allow"),
    **dict.fromkeys(("deny", "denied", "denies", "block", "blocked", "blocks", "refuse", "refused", "refuses",
                     "dropped", "rejected", "verweigert", "verweigern", "abgelehnt", "ablehnen", "blockiert",
                     "blockieren", "gesperrt", "verworfen"), "deny"),
    **dict.fromkeys(("ask", "asks", "asked", "fragt", "gefragt", "nachgefragt"), "ask"),
    **dict.fromkeys(("running", "ready", "läuft", "bereit"), "running"),
    **dict.fromkeys(("starting", "stopping", "building", "waiting", "startet", "wartet"), "busy"),
    **dict.fromkeys(("stopped", "ended", "exited", "detached", "gestoppt", "beendet", "angehalten", "getrennt"),
                    "stopped"),
    **dict.fromkeys(("failed", "crashed", "fehlgeschlagen", "abgestürzt"), "failed"),
}
NEGATION = re.compile(r"(?:\b(?:not|never|no|nicht|nie|kein\w*)\b|n't)\W*(?:\w+\W+)?$", re.IGNORECASE)
NEGATION_AFTER = re.compile(r"^\W*(?:\w+\W+)?nicht\b", re.IGNORECASE)
# Endings that make a dotted name a file, not a host.
FILE_ENDINGS = frozenset(
    "log py pyc pyz json jsonl yaml yml txt md rst sh bash zsh fish toml conf cfg ini xml html htm css js mjs cjs ts "
    "tsx jsx png jpg jpeg gif svg ico webp gz tgz tar zip xz zst bz2 lock pem key crt csr sock pid service desktop "
    "deb rpm whl exe dmg appimage so dylib dll db sqlite csv env bak tmp swp mount timer socket target img iso qcow2 "
    "rs go c h cc cpp hpp java kt rb php lua pl ps1 bat cmd mp4 mp3 wav webm pdf doc docx odt ods xls xlsx ppt pptx "
    "patch diff orig old new local example".split())
# sandbox-ai's commands: "sandbox-ai net allow x.example" is a command, "sandbox-ai keeps answering" is prose.
COMMANDS = ("activity", "approvals", "artex", "attach", "browser", "build", "cairn", "canaries", "claude", "codex",
            "completion", "defaults", "demo-device", "devices", "doctor", "dsh", "gc", "git-logins", "goose", "hermes",
            "hosts", "identities", "images", "kits", "limits", "llm", "logs", "mcp", "net", "opencode", "pause",
            "plugins", "presets", "privacy-check", "publish-logins", "qwen", "rename", "report", "resume", "rules",
            "run", "safeguards", "secrets", "session", "stop", "team", "update", "version", "vpn", "web", "wizard",
            "workspace")
TOKENS = re.compile(r"""
    (?P<tick>`[^`\n]{1,300}`)
  | (?P<url>\b[a-zA-Z][a-zA-Z0-9+.-]{1,15}://[^\s<>"'`]+)
  | (?P<command>(?<![\w/.-])sandbox-ai\s+(?:""" + "|".join(re.escape(c) for c in COMMANDS) + r""")\b(?!:)[^\n);"]*?)
    (?=[);"]|[.,]\s|[.,]?$)
  | (?P<ip6b>\[[0-9a-fA-F:.]{2,45}\](?::\d{1,5})?)
  | (?P<ip4>(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?::\d{1,5})?(?![\w]|\.\d))
  | (?P<ip6>(?<![\w:])[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}(?![\w:]))
  | (?P<localhost>\blocalhost(?::\d{1,5})?\b)
  | (?P<host>(?<![\w./@-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?P<tld>[a-z]{2,24})(?::\d{1,5})?
      (?![\w-]|\.[a-z0-9]))
  | (?P<word>(?<![\w-])[^\W\d_]{2,16}(?![\w-]))
""", re.VERBOSE)


def part(role, text):
    """One part: (ROLE, TEXT); control characters in TEXT shown escaped."""
    return (role if role in ROLES else "text", printable(text))


def session(text):
    return part("session", text)


def address(text):
    return part("address", text)


def solution(text):
    return part("solution", text)


def code(text):
    return part("code", text)


class Rich(str):
    """Lines of (role, text) parts; as a str, the plain text: the parts joined, the lines by line breaks."""

    def __new__(cls, lines=()):
        kept = tuple(tuple((role, text) for role, text in line if text) for line in lines)
        made = super().__new__(cls, "\n".join("".join(text for _, text in line) for line in kept))
        made.lines = kept
        return made

    def parts(self):
        """Every part, line after line."""
        return [p for line in self.lines for p in line]

    def of(self, role):
        """The texts of the parts with ROLE."""
        return [text for r, text in self.parts() if r == role]

    def cut(self, limit):
        """At most LIMIT characters, the last one an ellipsis when it is shortened (its line breaks counted)."""
        if len(self) <= limit:
            return self
        left, lines = limit - 1, []
        for line in self.lines:
            kept = []
            for role, text in line:
                if left <= 0:
                    break
                kept.append((role, text[:left]))
                left -= len(kept[-1][1])
            lines.append(kept)
            left -= 1  # its line break
            if left <= 0:
                break
        lines[-1].append(("text", "…"))
        return Rich(lines)

    def one_line(self):
        """The lines joined by spaces (a title)."""
        joined = []
        for index, line in enumerate(self.lines):
            if index:
                joined.append(("text", " "))
            joined.extend(line)
        return Rich([joined])


def _parts_of(value, base="text"):
    """The parts of VALUE in one line: a part, a Rich (its lines joined), else text recognised."""
    if isinstance(value, tuple) and len(value) == 2:
        return [value]
    if isinstance(value, Rich):
        return list(value.one_line().lines[0]) if value.lines else []
    return list(recognise(str(value), base).one_line().lines[0]) if str(value) else []


def fmt(template, base="text", **values):
    """TEMPLATE (a translated text with {placeholders}) filled with VALUES - parts, Rich or text (recognised); its
    own words recognised too, as BASE. Several lines where the template has them."""
    lines = []
    for template_line in str(template).split("\n"):
        line, at = [], 0
        for found in re.finditer(r"\{(\w+)\}", template_line):
            if template_line[at:found.start()]:
                line.extend(_parts_of(template_line[at:found.start()], base))
            line.extend(_parts_of(values.get(found.group(1), found.group(0)), base))
            at = found.end()
        if template_line[at:]:
            line.extend(_parts_of(template_line[at:], base))
        lines.append(line)
    return Rich(lines)


def lines(*items):
    """ITEMS one below the other: Rich (its lines), a list of parts (one line), or text (recognised); empty ones
    left out."""
    stacked = []
    for item in items:
        if item is None or item == "" or item == []:
            continue
        if isinstance(item, Rich):
            stacked.extend(item.lines)
        elif isinstance(item, list):
            stacked.append(item)
        else:
            stacked.extend(recognise(str(item)).lines)
    return Rich(stacked)


def _labels():
    """{label as shown: the role of what follows it} - the labels of a safeguard's popup, in both languages."""
    found = {}
    for key, role in (("Why", "why"), ("To allow it", "fix"), ("Stays refused", "text"), ("Fix", "fix")):
        found[key.lower()] = role
        found[tr(key).lower()] = role
    return found


def _looks_like_command(text):
    stripped = text.strip()
    return stripped.startswith(("sandbox-ai ", "--", "`")) or stripped.startswith("podman ")


def _word_role(text, start, end, word):
    role = WORDS.get(word.lower())
    if role is None:
        return None
    if NEGATION.search(text[max(0, start - 24):start]) \
            or (role in DECISIONS and NEGATION_AFTER.search(text[end:end + 16])):
        return None  # "does not allow it", "erlauben es nicht": no colour that says the opposite
    return role


def _token(text, found, base="text"):
    """(role, shown text) of a match of TOKENS in TEXT, or None for plain text. In a why (BASE) no word is coloured:
    it explains, the decision is said where it is made."""
    kind, value = found.lastgroup, found.group(0)
    if kind == "tld":
        kind = "host"
    if kind == "tick":
        return "solution" if _looks_like_command(value[1:-1]) else "code", value  # (its quotes kept: as it came)
    if kind == "url":
        return "address", value.rstrip(".,;:!?'\")]}")
    if kind == "command":
        return "solution", value.rstrip()
    if kind in ("ip6b", "ip4", "localhost"):
        if kind == "ip4":
            try:
                ipaddress.ip_address(value.split("/")[0].split(":")[0])
            except ValueError:
                return None
        return "address", value
    if kind == "ip6":
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return None
        return "address", value
    if kind == "host":
        return None if found.group("tld") in FILE_ENDINGS else ("address", value)
    if kind == "word":
        role = _word_role(text, found.start(), found.end(), value) if base != "why" else None
        return (role, value) if role else None
    return None


def _recognise_line(line, base):
    parts, at = [], 0
    for found in TOKENS.finditer(line):
        token = _token(line, found, base)
        if token is None:
            continue
        role, shown = token
        start = found.start()
        end = start + len(shown)  # (a URL's trailing punctuation stays text)
        if start < at:
            continue
        if line[at:start]:
            parts.append((base, line[at:start]))
        parts.append((role, shown))
        at = end
    if line[at:]:
        parts.append((base, line[at:]))
    return parts


def recognise(text, base="text"):
    """Free TEXT as Rich, its text unchanged (control characters other than line breaks and tabs shown escaped), in
    each line the addresses, commands, decision and state words marked; a line that starts with "Why:" or "To allow
    it:" (either language) has its label marked and the rest as why or (a command) solution."""
    labels = _labels()
    result = []
    for line in str(text).split("\n"):
        line = "".join(c if c == "\t" else printable(c) for c in line) if not line.isprintable() else line
        label, rest = (line.split(":", 1) + [""])[:2] if ":" in line[:40] else (None, None)
        follows = labels.get(label.strip().lower()) if label is not None else None
        if follows is not None:
            parts = [("label", label + ":")]
            if follows == "fix" and _looks_like_command(rest):
                lead = rest[:len(rest) - len(rest.lstrip())]
                parts += [("text", lead), ("solution", rest.lstrip())]
            else:
                parts += _recognise_line(rest, "why" if follows == "why" else base)
            result.append(parts)
        else:
            result.append(_recognise_line(line, base))
    return Rich(result)


def rich(value, one_line=False, base="text"):
    """VALUE as Rich: as it is when it is Rich (control characters escaped part by part), else recognised, its
    plain parts as BASE (a title's text: "heading")."""
    if isinstance(value, Rich):
        made = Rich([[(role, printable(text)) for role, text in line] for line in value.lines])
    else:
        made = recognise("" if value is None else str(value), base)
    return made.one_line() if one_line else made


# -- colours and markup --

def readable(color, background, against, ratio=MIN_CONTRAST):
    """COLOR, or COLOR mixed towards AGAINST (the text colour) in steps until it is at least RATIO:1 on BACKGROUND."""
    from PySide6.QtGui import QColor

    from . import theme
    if theme.contrast(color, background) >= ratio:
        return QColor(color).name()
    start, end = QColor(color), QColor(against)
    for step in range(1, 11):
        mix = step / 10
        mixed = QColor(round(start.red() + (end.red() - start.red()) * mix),
                       round(start.green() + (end.green() - start.green()) * mix),
                       round(start.blue() + (end.blue() - start.blue()) * mix)).name()
        if theme.contrast(mixed, background) >= ratio:
            return mixed
    return end.name()


def role_colors(data=None, background=None):
    """{role: colour} in the theme DATA (default: the one in use) on BACKGROUND (default: its base colour, a
    notification's), each at least 4.5:1; the roles that keep the text colour are left out."""
    from . import theme
    t = data or theme.data()
    c, s = t["colors"], t["status"]
    background = background or c["base"]
    wanted = {"address": c["accent"], "allow": s["ok"], "deny": s["bad"], "ask": s["warn"], "running": s["ok"],
              "busy": s["warn"], "stopped": s.get("muted", c["secondary"]), "failed": s["bad"]}
    return {role: readable(color, background, c["text"]) for role, color in wanted.items()}


def mono_families():
    """The monospaced families, the one chosen in the settings first."""
    from . import theme
    chosen = theme.chosen_fonts().get("mono")
    return ([chosen] if chosen else []) + [f for f in theme.MONO if f != chosen]


def to_html(value, colors=None, mono=None, centered=False):
    """VALUE (Rich or text) as Qt rich text: every part escaped, styled by its role; line breaks kept, long lines
    wrap (CENTERED: each line centred)."""
    value = rich(value)
    colors = role_colors() if colors is None else colors
    families = ", ".join(f"'{f}'" if f != "monospace" else f for f in (mono or mono_families()))
    shown = []
    for line in value.lines:
        out = []
        for role, text in line:
            styles = []
            if role in colors:
                styles.append(f"color: {colors[role]}")
            if role in BOLD:
                styles.append(f"font-weight: {BOLD[role]}")
            if role in MONO_ROLES:
                styles.append(f"font-family: {families}")
            escaped = html.escape(text, quote=True)
            out.append(f'<span style="{"; ".join(styles)}">{escaped}</span>' if styles else escaped)
        shown.append("".join(out))
    align = ' align="center"' if centered else ""
    return f'<p{align} style="margin: 0; white-space: pre-wrap;">' + "<br>".join(shown) + "</p>"


def desktop_markup(value):
    """VALUE for the body of a desktop notification that reads markup (freedesktop's body-markup: b, i, u): every
    part escaped, the session, the decisions, the states and the solution in bold."""
    value = rich(value)
    bold = {"session", "solution", *DECISIONS, *STATES}
    return "\n".join("".join(f"<b>{html.escape(text, quote=False)}</b>" if role in bold
                             else html.escape(text, quote=False) for role, text in line) for line in value.lines)
