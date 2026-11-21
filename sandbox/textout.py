"""Readable output of the list and show commands: aligned tables, sections, colour only for a terminal.

Colour follows the usual conventions: only when the output is a terminal, never with NO_COLOR set or
TERM=dumb, always with FORCE_COLOR. Texts that may come from a sandbox are passed through printable()
by the callers before they get here; this module only lays them out.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time

CODES = {"bold": "1", "dim": "2", "ok": "32", "warn": "33", "bad": "31", "accent": "36", "muted": "90",
         "blue": "34", "magenta": "35",
         "orange": "38;5;208", "violet": "38;5;141"}  # 256 colours: every terminal of the last decades


def colour_enabled(stream=None):
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM", "") != "dumb"


def style(text, kind, enabled=None):
    """TEXT in the colour or weight KIND (a key of CODES) when colour is on."""
    if not text or kind not in CODES or not (colour_enabled() if enabled is None else enabled):
        return text
    return f"\033[{CODES[kind]}m{text}\033[0m"


def visible_len(text):
    """Length without colour codes."""
    length, skipping = 0, False
    for char in text:
        if char == "\033":
            skipping = True
        elif skipping and char == "m":
            skipping = False
        elif not skipping:
            length += 1
    return length


def width():
    return max(60, shutil.get_terminal_size((100, 24)).columns)


def table(headers, rows, out=print, indent=2, empty="(none)", colour=None, truncate=None, stream=None):
    """Rows under headers, columns aligned; the last column wraps at the terminal's width, indented
    under itself - or, with TRUNCATE ("left": a path's end is kept, "right"), is cut to it with an
    ellipsis. Cells may already carry colour codes (such a cell is never cut)."""
    colour = colour_enabled() if colour is None else colour
    pad = " " * indent
    rows = [[str(cell) for cell in row] for row in rows]
    if not rows:
        out(f"{pad}{style(empty, 'muted', colour)}")
        return
    widths = [max([visible_len(h) for h in [headers[i]]] + [visible_len(r[i]) for r in rows])
              for i in range(len(headers))]
    lead = sum(widths[:-1]) + 2 * (len(headers) - 1)
    room = max(20, width() - indent - lead)

    def line(cells, kind=None):
        parts = [cell + " " * (w - visible_len(cell)) for cell, w in zip(cells[:-1], widths[:-1])]
        cut = truncate and "\033" not in cells[-1]
        last = [shortened(cells[-1], room, truncate == "left", stream)] if cut else wrap(cells[-1], room)
        first = "  ".join(parts + [last[0]]).rstrip()
        out(pad + (style(first, kind, colour) if kind else first))
        for more in last[1:]:
            out(pad + " " * (lead + (2 if len(cells) > 1 else 0)) + more)
    line(headers, "bold")
    for row in rows:
        line(row)


def ellipsis(stream=None):
    """The mark of a shortened text: "…" where the output can carry it, else "..."."""
    encoding = str(getattr(stream or sys.stdout, "encoding", "") or "").lower().replace("-", "")
    return "\u2026" if encoding in ("utf8", "utf_8") else "..."


def shortened(text, room, left=False, stream=None):
    """TEXT in at most ROOM characters: cut at its end (LEFT: at its start, so a path keeps its last folders), the
    cut marked with an ellipsis."""
    text = str(text)
    if len(text) <= room:
        return text
    mark = ellipsis(stream)
    head, gap, note = text.partition("  ")
    if left and gap and len(note) + 2 + len(mark) + 8 <= room:  # "PATH  (a note)": the path is cut, the note stays
        return shortened(head, room - len(note) - 2, True, stream) + gap + note
    keep = max(room - len(mark), 1)
    return mark + text[-keep:] if left else text[:keep] + mark


def home_short(path, home=None):
    """PATH with your HOME directory written as ~ ("~/src/app"); any other path as it is."""
    path = str(path)
    home = (home if home is not None else os.path.expanduser("~")).rstrip("/")
    if home and home != "~" and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


ISO_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def utc_seconds(value):
    """The seconds of an ISO time in UTC ("2026-11-16T08:00:00Z", fractions and +00:00 allowed), or None (without
    calendar or datetime: the commands the app polls stay light)."""
    found = ISO_TIME.match(value) if isinstance(value, str) else None  # (not strptime: it loads calendar)
    if not found:
        return None
    year, month, day, hour, minute, second = (int(part) for part in found.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 62):
        return None
    year -= month <= 2
    era = year // 400  # days since 1970-01-01 of the date (H. Hinnant's days_from_civil)
    of_era = year - era * 400
    of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    days = era * 146097 + of_era * 365 + of_era // 4 - of_era // 100 + of_year - 719468
    return days * 86400 + hour * 3600 + minute * 60 + second


def ago(value, now=None):
    """An ISO time (UTC) as Docker says how long ago it was: "just now", "5 minutes ago", "an hour ago",
    "3 days ago", "2 weeks ago", "4 months ago"; "-" when it is none."""
    then = utc_seconds(value)
    if then is None:
        return "-"
    seconds = max(0, (time.time() if now is None else now) - then)
    for unit, size, limit in (("minute", 60, 60), ("hour", 3600, 48), ("day", 86400, 14), ("week", 604800, 9),
                              ("month", 2629746, 24), ("year", 31556952, None)):
        count = int(seconds // size)
        if seconds < 60:
            return "just now"
        if limit is None or count < limit:
            if count == 1:
                return f"{'an' if unit == 'hour' else 'a'} {unit} ago"
            return f"{count} {unit}s ago"
    return "-"


def wrap(text, room):
    """TEXT split at spaces into lines of at most ROOM visible characters (long words stay whole)."""
    if visible_len(text) <= room:
        return [text]
    lines, current = [], ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and visible_len(candidate) > room:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + [current] if current else lines


def section(title, out=print, colour=None, note=""):
    """A heading line (with an optional dimmed NOTE)."""
    colour = colour_enabled() if colour is None else colour
    out(style(title, "accent", colour) + (f"  {style(note, 'muted', colour)}" if note else ""))


def pairs(items, out=print, indent=2, colour=None):
    """KEY  value lines with the keys aligned."""
    colour = colour_enabled() if colour is None else colour
    items = [(str(k), str(v)) for k, v in items]
    if not items:
        return
    key_width = max(len(k) for k, _ in items)
    for key, value in items:
        out(" " * indent + style(key.ljust(key_width), "bold", colour) + "  " + value)


def grid(headers, rows, right=(), out=print, indent=2, empty="(none)", colour=None, footer=None):
    """Like table(), with the columns whose index is in RIGHT aligned to the right (sizes, counts, shares) and an
    optional FOOTER row (a total) under a rule, in bold. The last column wraps at the terminal's width. Cells may
    already carry colour codes."""
    colour = colour_enabled() if colour is None else colour
    pad = " " * indent
    rows = [[str(cell) for cell in row] for row in rows]
    footer = [str(cell) for cell in footer] if footer else None
    if not rows and not footer:
        out(f"{pad}{style(empty, 'muted', colour)}")
        return
    every = [list(headers)] + rows + ([footer] if footer else [])
    widths = [max(visible_len(r[i]) for r in every) for i in range(len(headers))]
    lead = sum(widths[:-1]) + 2 * (len(headers) - 1)
    room = max(20, width() - indent - lead - 2)  # (a wrapped rest is indented by 2 more)
    right = set(right)

    def cell(text, index):
        gap = " " * (widths[index] - visible_len(text))
        return gap + text if index in right else text + gap

    def line(cells, kind=None):
        last = len(cells) - 1
        # a size ("12.0 KiB") is never split over two lines
        kept = re.sub(r"(\d) ((?:[KMGT]i)?B\b)", "\\1\0\\2", cells[last])
        tail = [part.replace("\0", " ") for part in wrap(kept, room)] if last not in right else \
            [cell(cells[last], last)]
        first = "  ".join([cell(c, i) for i, c in enumerate(cells[:last])] + [tail[0]]).rstrip()
        out(pad + (style(first, kind, colour) if kind else first))
        for more in tail[1:]:
            out(pad + " " * (lead + (2 if last else 0)) + more)
    line(list(headers), "bold")
    for row in rows:
        line(row)
    if footer:
        out(pad + style("-" * (lead + min(widths[-1], room + 2)), "muted", colour))
        line(footer, "bold")
# --- what a styled text means: help pages and messages ------------------------------------------------------------
# One place for every command (helpstyle.py's help pages, main.py's and the launcher's messages): headings, commands,
# options and their values in a help; errors, warnings and notes in what sandbox-ai says on standard error.
CODES.update({"heading": "1;4", "command": "1;36", "option": "36", "metavar": "35", "error": "31",
              "warning": "33", "note": "36", "number": "1", "label": "1"})

# "sandbox-ai net allow: ", "agent-sandbox: ": the start of a message of sandbox-ai's own; its label, if any.
MESSAGE = re.compile(r"^((?:sandbox-ai(?: [a-z][\w./-]*)*|agent-sandbox(?: [a-z][\w-]*)?|install\.sh|build_image\.sh)"
                     r":)( (warning|error|note|hint|debug):)?(?= |$)")
# What makes a message without a label an error: refused, failed, not found ... (a note says none of them).
FAILURE = re.compile(r"\b(?:errors?|fail(?:s|ed|ure)?|refus(?:e|es|ed)|cannot|can't|could not|unknown|not found|"
                     r"no such|invalid|denied|not allowed|(?:is|are) required|not running|unreadable|damaged|"
                     r"missing|no run|no start|not a)\b", re.IGNORECASE)


def message(text, colour=None, stream=None, error=False):
    """TEXT - one or more lines sandbox-ai says on standard error - with each of its own messages marked: an error
    ("sandbox-ai net: no run named 'web'", "...: error: ...") red, a warning yellow, a note's label in its colour, a
    debug line dimmed, the "sandbox-ai X:" before it in bold; any other line (a tool's own output) as it is. ERROR:
    each message without a label is an error (what ended a command). COLOUR: None decides for STREAM (standard
    error)."""
    colour = colour_enabled(stream or sys.stderr) if colour is None else colour
    if not colour or not text:
        return text
    lines = text.split("\n")
    return "\n".join(_message_line(line, error) for line in lines)


def _message_line(line, error=False):
    found = MESSAGE.match(line)
    if not found or "\033" in line:
        return line
    prefix, label, kind = found.group(1), found.group(2) or "", found.group(3)
    rest = line[found.end():]
    if kind == "debug":
        return style(line, "muted")
    if kind in ("note", "hint"):
        return style(prefix, "label", True) + " " + style(label.strip(), "note", True) + rest
    kind = kind or ("error" if error or FAILURE.search(rest) else None)
    if kind is None:
        return style(prefix, "label", True) + rest
    return style(prefix, "label", True) + (" " if label else "") + style(label.strip() + rest, kind, True)


# --- a help page ---------------------------------------------------------------------------------------------------
HEADING = re.compile(r"[A-Z][\w ,'/.()-]{0,58}:")  # "Options:", "Common Commands:", "Examples (more in ...):"
COMMAND_WORDS = re.compile(r"sandbox-ai(?: (?:[a-z][\w-]*|COMMAND|AGENT))*")  # "sandbox-ai net allow"
OPTION = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*)")
METAVAR = re.compile(r"(?<![\w-])([A-Z][A-Z0-9_]*(?:[.:|=@,][A-Z0-9_]+)*(?:\.\.\.)?|\{[^}]*\})(?![\w])")


def _synopsis(text, colour, prog=None):
    """A command line of a help (usage, an example): the command bold, its options and placeholders in theirs. PROG
    (the page's own command, "sandbox-ai net" - "+" after it when it has subcommands): only it, and its subcommand,
    is the command of a line that starts with it; the words after are arguments."""
    name = None
    if prog:
        own = prog.rstrip("+")
        if text == own or text.startswith(own + " "):
            name = own
            if prog.endswith("+"):
                sub = re.match(r" [a-z][\w-]*(?= |$)", text[len(own):])
                name += sub.group(0) if sub else ""
    if name is None:
        found = COMMAND_WORDS.match(text)
        name = found.group(0) if found else ""
    if not name:
        return _options(text, colour, values=True)
    return style(name, "command", colour) + _options(text[len(name):], colour, values=True)


def _prog(synopsis):
    """The command of a usage line ("sandbox-ai net allow" of "sandbox-ai net allow [OPTIONS] [RUN]"), "+" after it
    when it takes a subcommand (COMMAND)."""
    found = COMMAND_WORDS.match(synopsis)
    if not found:
        return None
    words = found.group(0).split()
    own = [word for word in words if word not in ("COMMAND", "AGENT")]
    return " ".join(own) + ("+" if "COMMAND" in synopsis.split() else "")


def _options(text, colour, values=False):
    """TEXT with its option names (and with VALUES: its placeholders, "DIR", "[OPTIONS]") styled."""
    parts, last = [], 0
    pattern = re.compile(OPTION.pattern + (r"|" + METAVAR.pattern if values else ""))
    for found in pattern.finditer(text):
        parts.append(text[last:found.start()])
        kind = "option" if found.group(1) else "metavar"
        parts.append(style(found.group(0), kind, colour))
        last = found.end()
    return "".join(parts) + text[last:]


def _entry(head, section, colour, prog=None):
    """The first column of a help's two-column line, styled for what it names."""
    if head.startswith("-"):
        return _options(head, colour, values=True)
    if head.startswith("sandbox-ai"):
        return _synopsis(head, colour, prog)
    if section == "Arguments" or re.fullmatch(r"[A-Z][A-Z0-9_]*(?:[|.][A-Z0-9_]+)*", head):
        return style(head, "metavar", colour)
    if "ommand" in section or section in ("Agents", "Targets"):
        return re.sub(r"[a-z][\w-]*", lambda name: style(name.group(0), "command", colour), head)
    return head


def help_page(text, colour=None, stream=None):
    """A help page (helpstyle.py lays them out) coloured for a terminal: the headings ("Usage:", "Options:" ...), the
    command, the options and their placeholders, the commands of a list, the examples' command lines. Only colour is
    added: without it (COLOUR False, or None and STREAM - standard output - no terminal) TEXT comes back as it is."""
    colour = colour_enabled(stream or sys.stdout) if colour is None else colour
    if not colour or not text:
        return text
    out, section, in_usage, prog, synopsis_at = [], "", False, None, None
    for line in text.split("\n"):
        if not line.strip():
            in_usage = False
            out.append(line)
            continue
        usage = re.match(r"(Usage:)(\s+)(.*)", line)
        if usage:
            in_usage, section = True, "Usage"
            prog = prog or _prog(usage.group(3))
            out.append(style(usage.group(1), "heading", True) + usage.group(2) + _synopsis(usage.group(3), True, prog))
            continue
        if in_usage:
            lead = line[:len(line) - len(line.lstrip())]
            out.append(lead + _synopsis(line.lstrip(), True, prog))
            continue
        if HEADING.fullmatch(line):
            section = line[:-1]
            out.append(style(line, "heading", True))
            continue
        hint = re.match(r"(Run ')([^']+)(' .*)", line)
        if hint:
            out.append(hint.group(1) + style(hint.group(2), "command", True) + hint.group(3))
            continue
        entry = re.match(r"(  )(\S.*?)((?:\s{2,}\S.*)?)$", line)
        if entry and not line.startswith("   "):
            synopsis_at = 2 if entry.group(2).startswith("sandbox-ai") else None
            out.append(entry.group(1) + _entry(entry.group(2), section, True, prog) + entry.group(3))
            continue
        more = re.match(r"( +)([\[(|].*?)((?:\s{2,}\S.*)?)$", line)
        if synopsis_at is not None and more and len(more.group(1)) > synopsis_at:  # a synopsis, continued
            out.append(more.group(1) + _options(more.group(2), True, values=True) + more.group(3))
            continue
        out.append(line)
    return "\n".join(out)
