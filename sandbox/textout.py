"""Readable output of the list and show commands: aligned tables, sections, colour only for a terminal.

Colour follows the usual conventions: only when the output is a terminal, never with NO_COLOR set or
TERM=dumb, always with FORCE_COLOR. Texts that may come from a sandbox are passed through printable()
by the callers before they get here; this module only lays them out.
"""
from __future__ import annotations

import os
import shutil
import sys

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


def table(headers, rows, out=print, indent=2, empty="(none)", colour=None):
    """Rows under headers, columns aligned; the last column wraps at the terminal's width, indented
    under itself. Cells may already carry colour codes."""
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
        last = wrap(cells[-1], room)
        first = "  ".join(parts + [last[0]]).rstrip()
        out(pad + (style(first, kind, colour) if kind else first))
        for more in last[1:]:
            out(pad + " " * (lead + (2 if len(cells) > 1 else 0)) + more)
    line(headers, "bold")
    for row in rows:
        line(row)


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
