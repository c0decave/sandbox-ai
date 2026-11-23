"""The fonts that come with the app (the folder typefaces/), for reading: the Help pages are set in Atkinson
Hyperlegible Next (made by the Braille Institute for legibility at low vision) with Atkinson Hyperlegible Mono for
commands, and the setting reading_font puts one of READING on all of the app's text (not on terminals, code and
logs: they keep the monospace font).

Nothing is downloaded: the files are part of the package, unchanged from each family's repository at a pinned commit
(typefaces/fonts.json: where from, SHA-256, git blob id; the SIL Open Font License beside them). A file is loaded
only when its SHA-256 is the one fonts.json gives; a file that is missing, changed or unreadable is left out, and
the text falls back to the theme's font.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .i18n import N_

FOLDER = Path(__file__).resolve().parent / "typefaces"
SETTING = "reading_font"
HELP_FAMILY = "Atkinson Hyperlegible Next"
HELP_MONO = "Atkinson Hyperlegible Mono"
FILES = {
    HELP_FAMILY: ("AtkinsonHyperlegibleNext-Regular.ttf", "AtkinsonHyperlegibleNext-Bold.ttf"),
    HELP_MONO: ("AtkinsonHyperlegibleMono-Regular.ttf",),
    "OpenDyslexic": ("OpenDyslexic-Regular.otf", "OpenDyslexic-Bold.otf"),
    "Lexend": ("Lexend-Regular.ttf", "Lexend-Bold.ttf"),
}
# The choices of the setting reading_font: (value, family or None, label, what it is for). "" is the theme's font
# (or the one chosen as the interface font).
READING = (
    ("", None, N_("The theme's font"), N_("The theme's font, or the interface font chosen below.")),
    ("atkinson", HELP_FAMILY, N_("Atkinson Hyperlegible Next (easy to tell apart)"),
     N_("Made by the Braille Institute for readers with low vision: letters that are easy to tell apart, such as "
        "I, l and 1 or O and 0.")),
    ("opendyslexic", "OpenDyslexic", N_("OpenDyslexic (for dyslexia)"),
     N_("Letters with heavy bottoms and distinct shapes, made for readers with dyslexia. Wider than most fonts.")),
    ("lexend", "Lexend", N_("Lexend (wide spacing, calm reading)"),
     N_("Wide letters and spacing, made to make reading easier and faster, also with dyslexia.")),
)
VALUES = tuple(value for value, *_ in READING)
LICENSES = ("OFL-AtkinsonHyperlegibleNext.txt", "OFL-AtkinsonHyperlegibleMono.txt", "OFL-OpenDyslexic.txt",
            "OFL-Lexend.txt")

_loaded = {}  # folder -> {family: [the files of it that were loaded]}


def manifest(folder=None):
    """typefaces/fonts.json of FOLDER as a dict ({} when it is missing or damaged)."""
    try:
        data = json.loads((Path(folder or FOLDER) / "fonts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def verified(path, files):
    """Whether the file PATH is there and its SHA-256 is the one FILES (fonts.json's "files") gives for it."""
    want = (files.get(path.name) or {}).get("sha256") if isinstance(files, dict) else None
    try:
        return bool(want) and hashlib.sha256(path.read_bytes()).hexdigest() == want
    except OSError:
        return False


def register(folder=None):
    """Load the fonts of FOLDER (default: the package's) into Qt's font database, once per folder: only files whose
    SHA-256 fonts.json gives. Returns {family: [files loaded]}; a family none of whose files loaded is left out."""
    folder = Path(folder or FOLDER)
    if folder in _loaded:
        return _loaded[folder]
    from PySide6.QtGui import QFontDatabase, QGuiApplication
    if QGuiApplication.instance() is None:  # Qt's font database needs the application (asked again later)
        return {}
    files = manifest(folder).get("files") or {}
    found = {}
    for family, names in FILES.items():
        for name in names:
            path = folder / name
            if not verified(path, files):
                continue
            ident = QFontDatabase.addApplicationFont(str(path))
            if ident >= 0 and family in QFontDatabase.applicationFontFamilies(ident):
                found.setdefault(family, []).append(name)
    _loaded[folder] = found
    return found


def loaded(family, folder=None):
    """Whether FAMILY came with the app and is loaded (register() first)."""
    return bool(register(folder).get(family))


def family_of(value, folder=None):
    """The family of the reading font VALUE (one of VALUES) when it is loaded, else None (the theme's font)."""
    family = next((f for v, f, *_ in READING if v == value and value), None)
    return family if family and loaded(family, folder) else None


def chosen(settings=None):
    """The reading font the settings name (VALUES; a damaged value is the default "")."""
    from . import backend
    value = backend.setting(SETTING, settings)
    return value if value in VALUES else ""


def text_families(base, settings=None, folder=None):
    """The families for the Help's text: the reading font chosen, else Atkinson Hyperlegible Next, then BASE's
    (the theme's) as the fallback."""
    first = family_of(chosen(settings), folder) or (HELP_FAMILY if loaded(HELP_FAMILY, folder) else None)
    return [f for f in [first, *base] if f]


def mono_families(base, folder=None):
    """The families for the Help's commands: Atkinson Hyperlegible Mono, then BASE's (the app's monospace font)."""
    return [f for f in [HELP_MONO if loaded(HELP_MONO, folder) else None, *base] if f]
