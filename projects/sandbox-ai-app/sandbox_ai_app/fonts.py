"""The fonts the settings offer: an interface font for the app's text and a monospace font for the terminal tabs,
code and logs, each from a list of well-liked ones. Only installed fonts are used (QFontDatabase); one that is
not installed is listed as such with how to get it - a package name of the usual distributions and the project's
site, as text. Nothing is downloaded or bundled and nothing is fetched from the network.

The chosen font goes first, the theme's after it: a font that is missing (uninstalled since) falls back to the
theme's font, or the system's.
"""
from __future__ import annotations

from .i18n import N_

# (family, how to get it): package names (Debian and Ubuntu, Fedora, Arch, Homebrew on macOS) and the project's
# site. Only names that are known to exist are given; the site always.
MONO_FONTS = (
    ("JetBrains Mono", "Debian/Ubuntu: fonts-jetbrains-mono · Fedora: jetbrains-mono-fonts · Arch: ttf-jetbrains-mono"
                       " · macOS: brew install --cask font-jetbrains-mono · jetbrains.com/lp/mono"),
    ("Fira Code", "Debian/Ubuntu: fonts-firacode · Fedora: fira-code-fonts · Arch: ttf-fira-code"
                  " · macOS: brew install --cask font-fira-code · github.com/tonsky/FiraCode"),
    ("Cascadia Code", "Debian/Ubuntu: fonts-cascadia-code · Fedora: cascadia-code-fonts · Arch: ttf-cascadia-code"
                      " · macOS: brew install --cask font-cascadia-code · github.com/microsoft/cascadia-code"),
    ("Source Code Pro", "Fedora: adobe-source-code-pro-fonts · Arch: adobe-source-code-pro-fonts"
                        " · macOS: brew install --cask font-source-code-pro · github.com/adobe-fonts/source-code-pro"),
    ("Hack", "Debian/Ubuntu: fonts-hack · Arch: ttf-hack · macOS: brew install --cask font-hack"
             " · sourcefoundry.org/hack"),
    ("IBM Plex Mono", "Debian/Ubuntu: fonts-ibm-plex · Fedora: ibm-plex-mono-fonts · Arch: ttf-ibm-plex"
                      " · macOS: brew install --cask font-ibm-plex-mono · github.com/IBM/plex"),
    ("Iosevka", "Arch: ttc-iosevka · macOS: brew install --cask font-iosevka · typeof.net/Iosevka"),
    ("Ubuntu Mono", "Ubuntu: fonts-ubuntu · Arch: ttf-ubuntu-font-family · macOS: brew install --cask font-ubuntu-mono"
                    " · design.ubuntu.com/font"),
    ("DejaVu Sans Mono", "Debian/Ubuntu: fonts-dejavu-core · Fedora: dejavu-sans-mono-fonts · Arch: ttf-dejavu"
                         " · macOS: brew install --cask font-dejavu · dejavu-fonts.github.io"),
    ("Noto Sans Mono", "Debian/Ubuntu: fonts-noto-core · Fedora: google-noto-sans-mono-fonts · Arch: noto-fonts"
                       " · macOS: brew install --cask font-noto-sans-mono · fonts.google.com/noto"),
    ("Liberation Mono", "Debian/Ubuntu: fonts-liberation · Fedora: liberation-mono-fonts · Arch: ttf-liberation"
                        " · github.com/liberationfonts"),
    ("Menlo", N_("comes with macOS")),
)
UI_FONTS = (
    ("Inter", "Debian/Ubuntu: fonts-inter · Fedora: rsms-inter-fonts · Arch: inter-font"
              " · macOS: brew install --cask font-inter · rsms.me/inter"),
    ("IBM Plex Sans", "Debian/Ubuntu: fonts-ibm-plex · Fedora: ibm-plex-sans-fonts · Arch: ttf-ibm-plex"
                      " · macOS: brew install --cask font-ibm-plex-sans · github.com/IBM/plex"),
    ("Noto Sans", "Debian/Ubuntu: fonts-noto-core · Fedora: google-noto-sans-fonts · Arch: noto-fonts"
                  " · macOS: brew install --cask font-noto-sans · fonts.google.com/noto"),
    ("Cantarell", "Debian/Ubuntu: fonts-cantarell · Fedora: abattis-cantarell-fonts · Arch: cantarell-fonts"
                  " · macOS: brew install --cask font-cantarell · gitlab.gnome.org/GNOME/cantarell-fonts"),
    ("Ubuntu", "Ubuntu: fonts-ubuntu · Arch: ttf-ubuntu-font-family · macOS: brew install --cask font-ubuntu"
               " · design.ubuntu.com/font"),
    ("DejaVu Sans", "Debian/Ubuntu: fonts-dejavu-core · Fedora: dejavu-sans-fonts · Arch: ttf-dejavu"
                    " · macOS: brew install --cask font-dejavu · dejavu-fonts.github.io"),
    ("Liberation Sans", "Debian/Ubuntu: fonts-liberation · Fedora: liberation-sans-fonts · Arch: ttf-liberation"
                        " · github.com/liberationfonts"),
)
KINDS = {"ui": UI_FONTS, "mono": MONO_FONTS}
SETTING = {"ui": "ui_font", "mono": "mono_font"}
_families = None  # tests put a list of installed families here instead of the font database's


def installed():
    """The font families installed on this machine (the font database's; never a download)."""
    if _families is not None:
        return list(_families)
    from PySide6.QtGui import QFontDatabase
    return list(QFontDatabase.families())


def _lookup(families):
    return {f.casefold(): f for f in families}


def choices(kind):
    """[(family, installed?, how to get it)] of the fonts offered for KIND ("ui" or "mono"), in their order."""
    have = _lookup(installed())
    return [(family, family.casefold() in have, how) for family, how in KINDS[kind]]


def pick(kind, choice):
    """The installed family CHOICE names (as the font database spells it), or None: not chosen (""), not
    installed, or not one of the fonts offered for KIND."""
    if not isinstance(choice, str) or not choice:
        return None
    if choice.casefold() not in {family.casefold() for family, _ in KINDS[kind]}:
        return None
    return _lookup(installed()).get(choice.casefold())


def how_to_get(kind, choice):
    """How to install the font CHOICE (text), or "" when it is not one of the list."""
    for family, how in KINDS[kind]:
        if family.casefold() == str(choice).casefold():
            return how
    return ""


def mono_families(mono=None):
    """The families of the app's monospace font (widgets.MONO): the chosen one first, then the system's."""
    return [mono, "monospace"] if mono else ["monospace"]


def use(settings=None, app=None):
    """Use the fonts the settings name (installed ones only; else the theme's): the app's monospace font
    (widgets.MONO and the widgets that show it), the terminal's and, with the theme's next apply(), the
    interface font. Returns {"ui": family or None, "mono": family or None}."""
    from . import backend, theme, widgets
    settings = backend.app_settings() if settings is None else settings
    chosen = {kind: pick(kind, backend.setting(SETTING[kind], settings)) for kind in KINDS}
    theme.set_fonts(chosen["ui"], chosen["mono"])
    before = widgets.MONO.families()
    after = mono_families(chosen["mono"])
    if before != after:
        widgets.MONO.setFamilies(after)
        from PySide6.QtWidgets import QApplication
        app = app or QApplication.instance()
        for widget in (app.allWidgets() if app else []):  # the views that were given MONO before
            current = widget.font()
            if current.families() == before:
                current.setFamilies(after)
                widget.setFont(current)
    return chosen
