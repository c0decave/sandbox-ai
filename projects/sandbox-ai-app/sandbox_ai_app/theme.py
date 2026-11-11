"""The app's looks: the system's own, nine of the app's (light, dark, hacker, cyberpunk, amber, unicorn,
Windows 3.11, Mac, Fallout) and well-known editor themes in their upstream palettes (Solarized Dark and Light,
Dracula, Nord, Gruvbox Dark, One Dark, Catppuccin Mocha and Latte, Tokyo Night). Each is a set of tokens -
colours, font, corner radius, bevelled or flat controls, the board's canvas and the terminal's colours - from
which a QPalette (with the Fusion style, which follows it), an application stylesheet and the app's own icons
are made (painted: no files, no network). The fonts chosen in the settings (fonts.py) take the place of the
theme's: the interface font of the app's, the monospace font of the terminal's and of code and logs.

Text colours are chosen for a contrast of at least 4.5:1 against every background they are drawn on, borders
of controls and the canvas grid at least 3:1 (WCAG 2.1); tests/test_theme.py checks every theme."""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from .i18n import N_

_system = None  # (style name, palette, font) the application started with
_current = "system"
_fonts = {"ui": None, "mono": None}  # the fonts chosen in the settings, when installed (fonts.use sets them)

MONO = ["JetBrains Mono", "Fira Code", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "monospace"]
SANS = ["Inter", "Cantarell", "Noto Sans", "Segoe UI", "Ubuntu", "DejaVu Sans", "sans-serif"]

# Every theme: label, dark, colours, status colours, font (families, size in pt, or None: the system's),
# radius (px), bevel (Windows 3.11 style 3D controls), canvas (board) and terminal colours.
# Colour tokens:
#   window, base (fields, tables, cards), alternate (every other row), text, secondary (hints), placeholder,
#   button, border, highlight + highlight_text (selection), accent + accent_text (primary buttons, focus),
#   danger, tooltip, disabled, nav + nav_text (the navigation), nav_selected + nav_selected_text,
#   title (page titles), canvas, grid (the board), node (a board node), node_header (its title band) and,
#   optionally, node_title (the title's colour there; default: text).
THEMES_DATA = {
    "light": {
        "label": N_("light"), "dark": False,
        "colors": {"window": "#f5f6f8", "base": "#ffffff", "alternate": "#f7f8fa", "text": "#1d1f23",
                   "secondary": "#575c66", "placeholder": "#6b707a", "button": "#ffffff", "border": "#828893",
                   "highlight": "#2563eb", "highlight_text": "#ffffff", "accent": "#2563eb", "accent_text": "#ffffff",
                   "danger": "#b42318", "tooltip": "#ffffff", "disabled": "#8a8f98", "nav": "#eceef2",
                   "nav_text": "#2b2f36", "nav_selected": "#dbe6ff", "nav_selected_text": "#1d3f99",
                   "title": "#111318", "canvas": "#f0f2f5", "grid": "#818791", "node": "#ffffff",
                   "node_header": "#e8eefc"},
        "status": {"ok": "#16692b", "warn": "#8a5300", "bad": "#b42318", "muted": "#575c66"},
        "font": {"families": SANS, "size": 10}, "radius": 8, "bevel": False,
        "terminal": {"background": "#ffffff", "foreground": "#1d1f23", "cursor": "#2563eb",
                     "selectionBackground": "#c7d7fe"},
    },
    "dark": {
        "label": N_("dark"), "dark": True,
        "colors": {"window": "#17191d", "base": "#1f2228", "alternate": "#23272e", "text": "#e6e8eb",
                   "secondary": "#a9afb9", "placeholder": "#959ba6", "button": "#262a31", "border": "#6b727e",
                   "highlight": "#3a63c8", "highlight_text": "#ffffff", "accent": "#7aa2ff", "accent_text": "#0b1020",
                   "danger": "#ff8a80", "tooltip": "#262a31", "disabled": "#80868f", "nav": "#121418",
                   "nav_text": "#c9cdd4", "nav_selected": "#26324d", "nav_selected_text": "#dbe5ff",
                   "title": "#f3f4f6", "canvas": "#14161a", "grid": "#626975", "node": "#1f2228",
                   "node_header": "#28334a"},
        "status": {"ok": "#7ddc8f", "warn": "#ffc46b", "bad": "#ff8a80", "muted": "#a9afb9"},
        "font": {"families": SANS, "size": 10}, "radius": 8, "bevel": False,
        "terminal": {"background": "#14161a", "foreground": "#e6e8eb", "cursor": "#7aa2ff",
                     "selectionBackground": "#2c4a8a"},
    },
    "hacker": {
        "label": N_("hacker"), "dark": True,
        "colors": {"window": "#000000", "base": "#050a05", "alternate": "#081108", "text": "#33ff66",
                   "secondary": "#22c24d", "placeholder": "#1fae45", "button": "#031003", "border": "#1f9e40",
                   "highlight": "#33ff66", "highlight_text": "#000000", "accent": "#33ff66", "accent_text": "#000000",
                   "danger": "#ff5555", "tooltip": "#031003", "disabled": "#1b7a33", "nav": "#000000",
                   "nav_text": "#33ff66", "nav_selected": "#0b3d18", "nav_selected_text": "#9dffb5",
                   "title": "#66ff8c", "canvas": "#000000", "grid": "#178a35", "node": "#030c03",
                   "node_header": "#0b3d18"},
        "status": {"ok": "#33ff66", "warn": "#e6ff33", "bad": "#ff5555", "muted": "#22c24d"},
        "font": {"families": MONO, "size": 10}, "radius": 0, "bevel": False, "extra": "hacker",
        "terminal": {"background": "#000000", "foreground": "#33ff66", "cursor": "#33ff66",
                     "selectionBackground": "#0b5a22"},
    },
    "cyberpunk": {
        "label": N_("cyberpunk"), "dark": True,
        "colors": {"window": "#0d0221", "base": "#150533", "alternate": "#1b0840", "text": "#f2e9ff",
                   "secondary": "#05d9e8", "placeholder": "#9a86c9", "button": "#1e0a4a", "border": "#c03fd0",
                   "highlight": "#ff2a6d", "highlight_text": "#0d0221", "accent": "#ff2a6d", "accent_text": "#0d0221",
                   "danger": "#ff6b8b", "tooltip": "#1e0a4a", "disabled": "#7e6aa8", "nav": "#08011a",
                   "nav_text": "#05d9e8", "nav_selected": "#ff2a6d", "nav_selected_text": "#0d0221",
                   "title": "#f9f002", "canvas": "#0a011c", "grid": "#8d36b9", "node": "#170639",
                   "node_header": "#3a0f66"},
        "status": {"ok": "#3dffb0", "warn": "#f9f002", "bad": "#ff6b8b", "muted": "#b8a6e6"},
        "font": {"families": ["Rajdhani", "Orbitron", "Exo 2", *SANS], "size": 10}, "radius": 2, "bevel": False, "extra": "cyberpunk",
        "terminal": {"background": "#0a011c", "foreground": "#f2e9ff", "cursor": "#ff2a6d",
                     "selectionBackground": "#5b1a7a"},
    },
    "amber": {
        "label": N_("amber"), "dark": True,
        "colors": {"window": "#0f0900", "base": "#160d00", "alternate": "#1d1200", "text": "#ffb000",
                   "secondary": "#d99400", "placeholder": "#c48600", "button": "#1d1200", "border": "#a86f00",
                   "highlight": "#ffb000", "highlight_text": "#120a00", "accent": "#ffb000", "accent_text": "#120a00",
                   "danger": "#ff7043", "tooltip": "#1d1200", "disabled": "#8a5c00", "nav": "#0a0600",
                   "nav_text": "#ffb000", "nav_selected": "#4a3000", "nav_selected_text": "#ffd27a",
                   "title": "#ffc94d", "canvas": "#0a0600", "grid": "#8f5f00", "node": "#140c00",
                   "node_header": "#3d2700"},
        "status": {"ok": "#ffd27a", "warn": "#ff9e2c", "bad": "#ff7043", "muted": "#d99400"},
        "font": {"families": MONO, "size": 10}, "radius": 0, "bevel": False, "extra": "amber",
        "terminal": {"background": "#0a0600", "foreground": "#ffb000", "cursor": "#ffb000",
                     "selectionBackground": "#5c3b00"},
    },
    "unicorn": {
        "label": N_("unicorn"), "dark": False,
        "colors": {"window": "#fff4fb", "base": "#ffffff", "alternate": "#fdf2ff", "text": "#3b1f4a",
                   "secondary": "#6e4c84", "placeholder": "#7d5d92", "button": "#ffffff", "border": "#a878bd",
                   "highlight": "#a020c8", "highlight_text": "#ffffff", "accent": "#a020c8", "accent_text": "#ffffff",
                   "danger": "#c2185b", "tooltip": "#fffaff", "disabled": "#a58fb3", "nav": "#fbe3f6",
                   "nav_text": "#4a2160", "nav_selected": "#e7d1ff", "nav_selected_text": "#5b118a",
                   "title": "#7a1fa2", "canvas": "#fff7fd", "grid": "#aa77bb", "node": "#ffffff",
                   "node_header": "#f6defb"},
        "status": {"ok": "#1d7a4d", "warn": "#9a4d00", "bad": "#c2185b", "muted": "#6e4c84"},
        "font": {"families": ["Nunito", "Quicksand", "Comfortaa", *SANS], "size": 10}, "radius": 14, "bevel": False, "extra": "unicorn",
        "gradient": ["#ffd6f0", "#e7d1ff", "#d3f5ff", "#d6ffe8"],
        "terminal": {"background": "#fff7fd", "foreground": "#3b1f4a", "cursor": "#a020c8",
                     "selectionBackground": "#ead2ff"},
    },
    "win311": {
        "label": N_("Windows 3.11"), "dark": False,
        "colors": {"window": "#c0c0c0", "base": "#ffffff", "alternate": "#f0f0f0", "text": "#000000",
                   "secondary": "#303030", "placeholder": "#4a4a4a", "button": "#c0c0c0", "border": "#000000",
                   "highlight": "#000080", "highlight_text": "#ffffff", "accent": "#000080", "accent_text": "#ffffff",
                   "danger": "#800000", "tooltip": "#ffffe1", "disabled": "#5e5e5e", "nav": "#ffffff",
                   "nav_text": "#000000", "nav_selected": "#000080", "nav_selected_text": "#ffffff",
                   "title": "#000080", "canvas": "#008080", "grid": "#00eded", "node": "#c0c0c0",
                   "node_header": "#000080", "node_title": "#ffffff", "light_edge": "#ffffff", "dark_edge": "#808080"},
        "status": {"ok": "#005900", "warn": "#654600", "bad": "#800000", "muted": "#303030"},
        "font": {"families": ["MS Sans Serif", "Microsoft Sans Serif", "Arial", "Liberation Sans", "sans-serif"],
                 "size": 9}, "radius": 0, "bevel": True, "extra": "win311",
        "terminal": {"background": "#000000", "foreground": "#c0c0c0", "cursor": "#ffffff",
                     "selectionBackground": "#000080"},
    },
    "mac": {
        "label": N_("Mac"), "dark": False,
        "colors": {"window": "#ececec", "base": "#ffffff", "alternate": "#f5f5f7", "text": "#1d1d1f",
                   "secondary": "#55555a", "placeholder": "#6e6e73", "button": "#ffffff", "border": "#828287",
                   "highlight": "#0064d1", "highlight_text": "#ffffff", "accent": "#0064d1", "accent_text": "#ffffff",
                   "danger": "#c4251b", "tooltip": "#f5f5f7", "disabled": "#8e8e93", "nav": "#e8e8ed",
                   "nav_text": "#1d1d1f", "nav_selected": "#d4d4da", "nav_selected_text": "#1d1d1f",
                   "title": "#1d1d1f", "canvas": "#f5f5f7", "grid": "#8a8a8f", "node": "#ffffff",
                   "node_header": "#e8f0fc"},
        "status": {"ok": "#197834", "warn": "#8a5300", "bad": "#c4251b", "muted": "#55555a"},
        "font": {"families": ["SF Pro Text", "Helvetica Neue", "Inter", "Helvetica", *SANS], "size": 10},
        "radius": 7, "bevel": False, "extra": "mac",
        "terminal": {"background": "#ffffff", "foreground": "#1d1d1f", "cursor": "#0064d1",
                     "selectionBackground": "#b3d7ff"},
    },
    "fallout": {
        "label": N_("Fallout"), "dark": True,
        "colors": {"window": "#0b1a0b", "base": "#0f220f", "alternate": "#122812", "text": "#4dff88",
                   "secondary": "#3ccf6d", "placeholder": "#36b862", "button": "#102610", "border": "#2fa65a",
                   "highlight": "#4dff88", "highlight_text": "#0b1a0b", "accent": "#4dff88", "accent_text": "#0b1a0b",
                   "danger": "#ffb347", "tooltip": "#102610", "disabled": "#258a48", "nav": "#081408",
                   "nav_text": "#4dff88", "nav_selected": "#4dff88", "nav_selected_text": "#081408",
                   "title": "#8dffb2", "canvas": "#081408", "grid": "#26874a", "node": "#0f220f",
                   "node_header": "#1c4a2a"},
        "status": {"ok": "#4dff88", "warn": "#f2e35c", "bad": "#ffb347", "muted": "#3ccf6d"},
        "font": {"families": ["Share Tech Mono", "VT323", *MONO], "size": 11}, "radius": 4, "bevel": False, "extra": "fallout",
        "terminal": {"background": "#081408", "foreground": "#4dff88", "cursor": "#4dff88",
                     "selectionBackground": "#1c5a32"},
    },
    # Well-known editor themes, in their upstream palettes. Where an upstream colour is below 4.5:1 for text
    # (WCAG 2.1) on the theme's background, the nearest shade of the same hue that reaches it is used instead
    # and named in a comment beside it; every other colour is the palette's own.
    # Solarized (Ethan Schoonover): base03 #002b36 base02 #073642 base01 #586e75 base00 #657b83 base0 #839496
    # base1 #93a1a1 base2 #eee8d5 base3 #fdf6e3; yellow #b58900 orange #cb4b16 red #dc322f magenta #d33682
    # violet #6c71c4 blue #268bd2 cyan #2aa198 green #859900.
    "solarized_dark": {
        "label": N_("Solarized Dark"), "dark": True,
        "colors": {"window": "#002b36", "base": "#002b36", "alternate": "#073642", "text": "#93a1a1",
                   "secondary": "#839496", "placeholder": "#839496", "button": "#002b36", "border": "#657b83",
                   "highlight": "#586e75", "highlight_text": "#fdf6e3", "accent": "#2aa198", "accent_text": "#002b36",
                   "danger": "#e56462", "tooltip": "#073642", "disabled": "#586e75", "nav": "#073642",
                   "nav_text": "#93a1a1", "nav_selected": "#586e75", "nav_selected_text": "#fdf6e3",
                   "title": "#eee8d5", "canvas": "#002b36", "grid": "#657b83", "node": "#073642",
                   "node_header": "#586e75", "node_title": "#fdf6e3"},
        # bad, danger: red #dc322f lightened (3.3:1 on base03)
        "status": {"ok": "#859900", "warn": "#b58900", "bad": "#e56462", "muted": "#839496"},
        "font": {"families": SANS, "size": 10}, "radius": 4, "bevel": False,
        "terminal": {"background": "#002b36", "foreground": "#839496", "cursor": "#93a1a1",
                     "selectionBackground": "#073642"},
    },
    "solarized_light": {
        "label": N_("Solarized Light"), "dark": False,
        # accent, highlight: blue #268bd2 darkened (base3 on it: 3.4:1); danger: red #dc322f darkened (4.3:1)
        "colors": {"window": "#fdf6e3", "base": "#fdf6e3", "alternate": "#eee8d5", "text": "#073642",
                   "secondary": "#586e75", "placeholder": "#586e75", "button": "#fdf6e3", "border": "#657b83",
                   "highlight": "#2076b3", "highlight_text": "#fdf6e3", "accent": "#2076b3", "accent_text": "#fdf6e3",
                   "danger": "#da2925", "tooltip": "#eee8d5", "disabled": "#93a1a1", "nav": "#eee8d5",
                   "nav_text": "#073642", "nav_selected": "#93a1a1", "nav_selected_text": "#002b36",
                   "title": "#002b36", "canvas": "#eee8d5", "grid": "#657b83", "node": "#fdf6e3",
                   "node_header": "#eee8d5"},
        # ok: green #859900 darkened (3.0:1 on base3), warn: yellow #b58900 darkened (3.0:1), bad: red darkened
        "status": {"ok": "#697800", "warn": "#8f6c00", "bad": "#da2925", "muted": "#586e75"},
        "font": {"families": SANS, "size": 10}, "radius": 4, "bevel": False,
        "terminal": {"background": "#fdf6e3", "foreground": "#657b83", "cursor": "#586e75",
                     "selectionBackground": "#eee8d5"},
    },
    # Dracula (draculatheme.com): background #282a36, current line #44475a, foreground #f8f8f2, comment #6272a4,
    # cyan #8be9fd, green #50fa7b, orange #ffb86c, pink #ff79c6, purple #bd93f9, red #ff5555, yellow #f1fa8c;
    # its darker backgrounds #21222c and #191a21, lighter #343746.
    "dracula": {
        "label": N_("Dracula"), "dark": True,
        # secondary, placeholder, muted: comment #6272a4 lightened (3.0:1 on the background)
        "colors": {"window": "#21222c", "base": "#282a36", "alternate": "#343746", "text": "#f8f8f2",
                   "secondary": "#8390b7", "placeholder": "#8390b7", "button": "#282a36", "border": "#6272a4",
                   "highlight": "#44475a", "highlight_text": "#f8f8f2", "accent": "#bd93f9", "accent_text": "#282a36",
                   "danger": "#ff5555", "tooltip": "#343746", "disabled": "#6272a4", "nav": "#191a21",
                   "nav_text": "#f8f8f2", "nav_selected": "#44475a", "nav_selected_text": "#f8f8f2",
                   "title": "#f8f8f2", "canvas": "#21222c", "grid": "#6272a4", "node": "#282a36",
                   "node_header": "#44475a"},
        "status": {"ok": "#50fa7b", "warn": "#ffb86c", "bad": "#ff5555", "muted": "#8390b7"},
        "font": {"families": SANS, "size": 10}, "radius": 6, "bevel": False,
        "terminal": {"background": "#282a36", "foreground": "#f8f8f2", "cursor": "#f8f8f2",
                     "selectionBackground": "#44475a"},
    },
    # Nord (nordtheme.com): Polar Night nord0 #2e3440 nord1 #3b4252 nord2 #434c5e nord3 #4c566a (comments
    # #616e88), Snow Storm nord4 #d8dee9 nord5 #e5e9f0 nord6 #eceff4, Frost nord7 #8fbcbb nord8 #88c0d0
    # nord9 #81a1c1 nord10 #5e81ac, Aurora nord11 #bf616a nord12 #d08770 nord13 #ebcb8b nord14 #a3be8c
    # nord15 #b48ead.
    "nord": {
        "label": N_("Nord"), "dark": True,
        # secondary, placeholder, muted: the comment grey #616e88 lightened (2.4:1 on nord0)
        "colors": {"window": "#2e3440", "base": "#2e3440", "alternate": "#3b4252", "text": "#d8dee9",
                   "secondary": "#919cb1", "placeholder": "#919cb1", "button": "#2e3440", "border": "#5e81ac",
                   "highlight": "#434c5e", "highlight_text": "#eceff4", "accent": "#88c0d0", "accent_text": "#2e3440",
                   "danger": "#cf888f", "tooltip": "#3b4252", "disabled": "#616e88", "nav": "#3b4252",
                   "nav_text": "#d8dee9", "nav_selected": "#4c566a", "nav_selected_text": "#eceff4",
                   "title": "#eceff4", "canvas": "#2e3440", "grid": "#5e81ac", "node": "#3b4252",
                   "node_header": "#434c5e"},
        # bad, danger: nord11 #bf616a lightened (3.0:1 on nord0)
        "status": {"ok": "#a3be8c", "warn": "#ebcb8b", "bad": "#cf888f", "muted": "#919cb1"},
        "font": {"families": SANS, "size": 10}, "radius": 4, "bevel": False,
        "terminal": {"background": "#2e3440", "foreground": "#d8dee9", "cursor": "#d8dee9",
                     "selectionBackground": "#434c5e"},
    },
    # Gruvbox dark, medium contrast (morhetz/gruvbox): bg0 #282828 bg0_h #1d2021 bg0_s #32302f bg1 #3c3836
    # bg2 #504945 bg4 #7c6f64 gray #928374 fg #ebdbb2 fg0 #fbf1c7 fg2 #d5c4a1 fg4 #a89984; bright red #fb4934
    # green #b8bb26 yellow #fabd2f blue #83a598 purple #d3869b aqua #8ec07c orange #fe8019.
    "gruvbox_dark": {
        "label": N_("Gruvbox Dark"), "dark": True,
        "colors": {"window": "#282828", "base": "#282828", "alternate": "#32302f", "text": "#ebdbb2",
                   "secondary": "#a89984", "placeholder": "#a89984", "button": "#282828", "border": "#928374",
                   "highlight": "#504945", "highlight_text": "#fbf1c7", "accent": "#83a598", "accent_text": "#282828",
                   "danger": "#fb533f", "tooltip": "#3c3836", "disabled": "#7c6f64", "nav": "#3c3836",
                   "nav_text": "#ebdbb2", "nav_selected": "#504945", "nav_selected_text": "#fbf1c7",
                   "title": "#fbf1c7", "canvas": "#1d2021", "grid": "#7c6f64", "node": "#282828",
                   "node_header": "#3c3836"},
        # bad, danger: bright red #fb4934 a little lighter (4.3:1 on bg0)
        "status": {"ok": "#b8bb26", "warn": "#fabd2f", "bad": "#fb533f", "muted": "#a89984"},
        "font": {"families": SANS, "size": 10}, "radius": 4, "bevel": False,
        "terminal": {"background": "#282828", "foreground": "#ebdbb2", "cursor": "#ebdbb2",
                     "selectionBackground": "#504945"},
    },
    # One Dark (Atom): background #282c34, panels #21252b, #2c313a, selection #3e4451, foreground #abb2bf,
    # mono-2 #828997, mono-3 #5c6370, UI text #9da5b4 and #d7dae0, red #e06c75, green #98c379, yellow #e5c07b,
    # orange #d19a66, blue #61afef, purple #c678dd, cyan #56b6c2, cursor #528bff.
    "one_dark": {
        "label": N_("One Dark"), "dark": True,
        "colors": {"window": "#21252b", "base": "#282c34", "alternate": "#2c313a", "text": "#abb2bf",
                   "secondary": "#9da5b4", "placeholder": "#9da5b4", "button": "#282c34", "border": "#828997",
                   "highlight": "#3e4451", "highlight_text": "#d7dae0", "accent": "#61afef", "accent_text": "#282c34",
                   "danger": "#e17079", "tooltip": "#21252b", "disabled": "#5c6370", "nav": "#21252b",
                   "nav_text": "#abb2bf", "nav_selected": "#2c313a", "nav_selected_text": "#d7dae0",
                   "title": "#d7dae0", "canvas": "#21252b", "grid": "#828997", "node": "#282c34",
                   "node_header": "#3e4451"},
        # bad, danger: red #e06c75 a little lighter (4.4:1 on the background)
        "status": {"ok": "#98c379", "warn": "#e5c07b", "bad": "#e17079", "muted": "#9da5b4"},
        "font": {"families": SANS, "size": 10}, "radius": 4, "bevel": False,
        "terminal": {"background": "#282c34", "foreground": "#abb2bf", "cursor": "#528bff",
                     "selectionBackground": "#3e4451"},
    },
    # Catppuccin (catppuccin.com/palette), Mocha: crust #11111b mantle #181825 base #1e1e2e surface0 #313244
    # surface1 #45475a overlay0 #6c7086 overlay2 #9399b2 subtext0 #a6adc8 subtext1 #bac2de text #cdd6f4,
    # red #f38ba8 yellow #f9e2af green #a6e3a1 mauve #cba6f7 rosewater #f5e0dc.
    "catppuccin_mocha": {
        "label": N_("Catppuccin Mocha"), "dark": True,
        "colors": {"window": "#181825", "base": "#1e1e2e", "alternate": "#181825", "text": "#cdd6f4",
                   "secondary": "#a6adc8", "placeholder": "#9399b2", "button": "#313244", "border": "#6c7086",
                   "highlight": "#45475a", "highlight_text": "#cdd6f4", "accent": "#cba6f7", "accent_text": "#11111b",
                   "danger": "#f38ba8", "tooltip": "#313244", "disabled": "#6c7086", "nav": "#11111b",
                   "nav_text": "#bac2de", "nav_selected": "#313244", "nav_selected_text": "#cdd6f4",
                   "title": "#cdd6f4", "canvas": "#11111b", "grid": "#6c7086", "node": "#1e1e2e",
                   "node_header": "#313244"},
        "status": {"ok": "#a6e3a1", "warn": "#f9e2af", "bad": "#f38ba8", "muted": "#a6adc8"},
        "font": {"families": SANS, "size": 10}, "radius": 8, "bevel": False,
        "terminal": {"background": "#1e1e2e", "foreground": "#cdd6f4", "cursor": "#f5e0dc",
                     "selectionBackground": "#45475a"},
    },
    # Catppuccin Latte: base #eff1f5 mantle #e6e9ef crust #dce0e8 surface0 #ccd0da overlay1 #8c8fa1
    # overlay2 #7c7f93 subtext1 #5c5f77 text #4c4f69, red #d20f39 yellow #df8e1d green #40a02b mauve #8839ef
    # rosewater #dc8a78.
    "catppuccin_latte": {
        "label": N_("Catppuccin Latte"), "dark": False,
        "colors": {"window": "#eff1f5", "base": "#eff1f5", "alternate": "#e6e9ef", "text": "#4c4f69",
                   "secondary": "#5c5f77", "placeholder": "#5c5f77", "button": "#eff1f5", "border": "#7c7f93",
                   "highlight": "#ccd0da", "highlight_text": "#4c4f69", "accent": "#8839ef", "accent_text": "#eff1f5",
                   "danger": "#d20f39", "tooltip": "#e6e9ef", "disabled": "#8c8fa1", "nav": "#e6e9ef",
                   "nav_text": "#4c4f69", "nav_selected": "#ccd0da", "nav_selected_text": "#4c4f69",
                   "title": "#4c4f69", "canvas": "#e6e9ef", "grid": "#7c7f93", "node": "#eff1f5",
                   "node_header": "#dce0e8"},
        # ok: green #40a02b darkened (3.0:1 on base), warn: yellow #df8e1d darkened (2.3:1)
        "status": {"ok": "#327d22", "warn": "#996214", "bad": "#d20f39", "muted": "#5c5f77"},
        "font": {"families": SANS, "size": 10}, "radius": 8, "bevel": False,
        "terminal": {"background": "#eff1f5", "foreground": "#4c4f69", "cursor": "#dc8a78",
                     "selectionBackground": "#ccd0da"},
    },
    # Tokyo Night, night (folke/tokyonight.nvim): bg_dark #16161e bg #1a1b26 bg_highlight #292e42
    # bg_visual #283457 fg #c0caf5 fg_dark #a9b1d6 dark3 #545c7e dark5 #737aa2 blue #7aa2f7 red #f7768e
    # yellow #e0af68 green #9ece6a.
    "tokyo_night": {
        "label": N_("Tokyo Night"), "dark": True,
        "colors": {"window": "#16161e", "base": "#1a1b26", "alternate": "#16161e", "text": "#c0caf5",
                   "secondary": "#a9b1d6", "placeholder": "#a9b1d6", "button": "#292e42", "border": "#737aa2",
                   "highlight": "#283457", "highlight_text": "#c0caf5", "accent": "#7aa2f7", "accent_text": "#16161e",
                   "danger": "#f7768e", "tooltip": "#16161e", "disabled": "#545c7e", "nav": "#16161e",
                   "nav_text": "#a9b1d6", "nav_selected": "#292e42", "nav_selected_text": "#c0caf5",
                   "title": "#c0caf5", "canvas": "#16161e", "grid": "#737aa2", "node": "#1a1b26",
                   "node_header": "#292e42"},
        "status": {"ok": "#9ece6a", "warn": "#e0af68", "bad": "#f7768e", "muted": "#a9b1d6"},
        "font": {"families": SANS, "size": 10}, "radius": 6, "bevel": False,
        "terminal": {"background": "#1a1b26", "foreground": "#c0caf5", "cursor": "#c0caf5",
                     "selectionBackground": "#283457"},
    },
}

THEMES = ("system", *THEMES_DATA)
# Kept for code that knows only light and dark.
COLORS = {name: data["colors"] for name, data in THEMES_DATA.items()}
STATUS = {name: data["status"] for name, data in THEMES_DATA.items()}


def choices():
    """(key, label) of every theme, for the theme boxes; labels are translated by the caller."""
    return [("system", N_("as the system"))] + [(key, data["label"]) for key, data in THEMES_DATA.items()]


def luminance(color):
    color = QColor(color)

    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def palette(mode):
    """The QPalette of the theme MODE."""
    c, R, G = THEMES_DATA[mode]["colors"], QPalette.ColorRole, QPalette.ColorGroup
    p = QPalette()
    roles = {R.Window: c["window"], R.WindowText: c["text"], R.Base: c["base"], R.AlternateBase: c["alternate"],
             R.ToolTipBase: c["tooltip"], R.ToolTipText: c["text"], R.Text: c["text"], R.Button: c["button"],
             R.ButtonText: c["text"], R.BrightText: c["danger"], R.Highlight: c["highlight"],
             R.HighlightedText: c["highlight_text"], R.Link: c["accent"], R.LinkVisited: c["accent"],
             R.PlaceholderText: c["placeholder"], R.Light: c.get("light_edge", c["button"]),
             R.Midlight: c["border"], R.Mid: c["border"], R.Dark: c.get("dark_edge", c["border"]),
             R.Shadow: "#000000"}
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(G.Disabled, role, QColor(c["disabled"]))
    return p


def mode_of(p=None):
    """ "dark" or "light": the look of the palette in use (also for the system theme)."""
    return "dark" if is_dark(p) else "light"


def data():
    """The tokens of the theme in use; for the system theme the light or dark ones, with the system's
    palette colours where they exist."""
    if _current in THEMES_DATA:
        return THEMES_DATA[_current]
    app = QApplication.instance()
    p = app.palette() if app else QPalette()
    # Made once per palette (its cacheKey changes with every change of it): this is asked for at every paint of a
    # board part and every colour of a page - thousands of times a second while the board moves.
    key = (_current, p.cacheKey())
    if _system_data[0] == key:
        return _system_data[1]
    base = dict(THEMES_DATA[mode_of(p)])
    colors_ = dict(base["colors"])
    colors_.update(window=p.color(QPalette.ColorRole.Window).name(), base=p.color(QPalette.ColorRole.Base).name(),
                   text=p.color(QPalette.ColorRole.WindowText).name(), button=p.color(QPalette.ColorRole.Button).name())
    made = {**base, "colors": colors_, "font": None}
    _system_data[0], _system_data[1] = key, made
    return made


_system_data = [None, None]  # (key, data()) of the system theme for the palette in use


def colors():
    """The colour tokens for the current look."""
    return data()["colors"]


def _bevel(c, pressed=False):
    light, dark = c.get("light_edge", "#ffffff"), c.get("dark_edge", "#808080")
    if pressed:
        light, dark = dark, light
    return (f"border-top: 2px solid {light}; border-left: 2px solid {light}; "
            f"border-right: 2px solid {dark}; border-bottom: 2px solid {dark};")


def _asset_dir():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "sandbox-ai-app" / "theme"


def _paint(path, size, draw):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw(painter, size)
    painter.end()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pixmap.save(str(temporary), "PNG")
    os.replace(temporary, path)


def assets(name=None):
    """Small images the stylesheet needs (a check mark, Windows 3.11's cross, arrows), painted in the theme's
    colours into the user's cache: Qt stylesheets can only refer to files. {key: path for url()} or {} when
    the cache cannot be written (the stylesheet then does without)."""
    name = name or _current
    t = THEMES_DATA.get(name)
    if t is None or QApplication.instance() is None:
        return {}
    c = t["colors"]
    folder = _asset_dir() / name
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink():
            return {}

        def pen(color, width):
            result = QPen(QColor(color), width)
            result.setCapStyle(Qt.PenCapStyle.RoundCap)
            result.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            return result

        def check(painter, size):
            painter.setPen(pen(c["accent_text"], size / 7))
            painter.drawPolyline([QPointF(size * .22, size * .52), QPointF(size * .42, size * .72),
                                  QPointF(size * .78, size * .30)])

        def cross(painter, size):  # Windows 3.11 checked its boxes with an X
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(QPen(QColor(c["text"]), 1.5))
            painter.drawLine(QPointF(1, 1), QPointF(size - 2, size - 2))
            painter.drawLine(QPointF(size - 2, 1), QPointF(1, size - 2))

        def dot(painter, size):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c["text"] if t.get("bevel") else c["accent_text"]))
            painter.drawEllipse(QRectF(size * .3, size * .3, size * .4, size * .4))

        def triangle(direction):
            def draw(painter, size):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(c["text"]))
                a, b, m = size * .25, size * .75, size * .5
                points = {"down": [(a, a + 1), (b, a + 1), (m, b - 1)], "up": [(a, b - 1), (b, b - 1), (m, a + 1)],
                          "left": [(b - 1, a), (b - 1, b), (a + 1, m)], "right": [(a + 1, a), (a + 1, b), (b - 1, m)]}
                painter.drawPolygon([QPointF(x, y) for x, y in points[direction]])
            return draw

        def chevron(painter, size):  # macOS pop-up buttons
            painter.setPen(pen(c["secondary"], size / 9))
            painter.drawPolyline([QPointF(size * .3, size * .42), QPointF(size * .5, size * .62),
                                  QPointF(size * .7, size * .42)])
        def scanlines(painter, size):  # Fallout's Pip-Boy screen: a darker line every third pixel row
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.fillRect(0, 0, size, size, QColor(c["base"]))
            line = QColor(c["window"])
            for y in range(0, size, 3):
                painter.fillRect(0, y, size, 1, line)
        found = {}
        for key, size, draw in (("check", 16, check), ("cross", 13, cross), ("dot", 16, dot), ("chevron", 16, chevron),
                                ("scanlines", 12, scanlines),
                                ("down", 12, triangle("down")), ("up", 12, triangle("up")),
                                ("left", 12, triangle("left")), ("right", 12, triangle("right"))):
            path = folder / f"{key}.png"
            _paint(path, size, draw)
            found[key] = path.as_posix()
        return found
    except OSError:
        return {}


def _win311(c, a):
    """Windows 3.11: bevelled grey controls, sunken white fields, navy title bars, an X in check boxes,
    scroll bars with arrow buttons, bold system text on buttons."""
    raised, sunken = _bevel(c), _bevel(c, pressed=True)
    arrow = lambda key: f'image: url("{a[key]}");' if key in a else ""  # noqa: E731
    return f"""
QLabel[role="title"] {{ background: {c['highlight']}; color: {c['highlight_text']}; padding: 3px 8px; font-size: 12pt; }}
QLabel[role="section"] {{ color: {c['text']}; font-weight: 700; }}
QLabel#brand {{ background: {c['highlight']}; color: {c['highlight_text']}; padding: 6px 10px; margin: 4px; }}
QPushButton {{ font-weight: 700; color: {c['text']}; }}
QPushButton[role="primary"] {{ background: {c['button']}; color: {c['text']}; border: 2px solid {c['text']};
                               padding: 4px 14px; }}
QPushButton[role="primary"]:pressed {{ {sunken} }}
QPushButton:focus {{ text-decoration: underline; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ {sunken} border-radius: 0;
    background: {c['base']}; padding: 2px 4px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    {sunken} }}
QComboBox::drop-down {{ width: 16px; border: none; {raised} background: {c['button']}; }}
QComboBox::down-arrow {{ {arrow('down')} }}
QTableWidget, QTableView, QTreeView, QListView {{ {sunken} border-radius: 0; gridline-color: {c['window']}; }}
QHeaderView::section {{ {raised} background: {c['button']}; color: {c['text']}; padding: 3px 6px; }}
QTabWidget::pane {{ {raised} border-radius: 0; top: -2px; }}
QTabBar::tab {{ {raised} border-bottom: none; background: {c['button']}; color: {c['text']}; padding: 4px 12px;
               margin-right: 2px; border-radius: 0; }}
QTabBar::tab:selected {{ font-weight: 700; margin-bottom: -2px; border-top-left-radius: 0; border-top-right-radius: 0; }}
QGroupBox {{ border: 1px solid {c['dark_edge']}; border-radius: 0; background: transparent; margin-top: 14px; }}
QGroupBox::title {{ color: {c['text']}; background: {c['window']}; }}
QFrame[role="kpi"], QFrame[role="card"] {{ {raised} border-radius: 0; background: {c['window']}; }}
QListWidget#nav {{ {sunken} background: {c['nav']}; margin: 4px; }}
QListWidget#nav::item {{ border-radius: 0; padding: 4px 8px; margin: 0; }}
QListWidget#nav::item:hover {{ background: transparent; text-decoration: underline; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {c['text']}; border-radius: 0;
                        background: {c['base']}; }}
QCheckBox::indicator:checked {{ background: {c['base']}; border-color: {c['text']}; {arrow('cross')} }}
QRadioButton::indicator {{ width: 12px; height: 12px; border: 1px solid {c['text']}; border-radius: 7px;
                           background: {c['base']}; }}
QRadioButton::indicator:checked {{ background: {c['base']}; border-color: {c['text']}; {arrow('dot')} }}
QScrollBar:vertical {{ width: 17px; background: {c['alternate']}; margin: 17px 0 17px 0; border: 1px solid {c['text']}; }}
QScrollBar:horizontal {{ height: 17px; background: {c['alternate']}; margin: 0 17px 0 17px; border: 1px solid {c['text']}; }}
QScrollBar::handle {{ {raised} background: {c['button']}; border-radius: 0; min-height: 17px; min-width: 17px; }}
QScrollBar::handle:hover {{ background: {c['button']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ {raised} background: {c['button']}; subcontrol-origin: margin; }}
QScrollBar::add-line:vertical {{ height: 15px; subcontrol-position: bottom; }}
QScrollBar::sub-line:vertical {{ height: 15px; subcontrol-position: top; }}
QScrollBar::add-line:horizontal {{ width: 15px; subcontrol-position: right; }}
QScrollBar::sub-line:horizontal {{ width: 15px; subcontrol-position: left; }}
QScrollBar::up-arrow:vertical {{ {arrow('up')} }}
QScrollBar::down-arrow:vertical {{ {arrow('down')} }}
QScrollBar::left-arrow:horizontal {{ {arrow('left')} }}
QScrollBar::right-arrow:horizontal {{ {arrow('right')} }}
QMenuBar {{ background: {c['base']}; border-bottom: 1px solid {c['text']}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {c['highlight']}; color: {c['highlight_text']}; }}
QMenu {{ border: 1px solid {c['text']}; border-radius: 0; }}
QMenu::item {{ border-radius: 0; }}
QStatusBar {{ border-top: 1px solid {c['dark_edge']}; color: {c['text']}; }}
QToolTip {{ border: 1px solid {c['text']}; border-radius: 0; }}
QSplitter::handle {{ background: {c['window']}; }}
"""


def _mac(c, a):
    """macOS-like: soft white push buttons with a hairline, a blue default button, segmented tabs, sidebar
    pills, borderless tables with zebra rows, rounded group boxes, thin scroll bars, chevron pop-ups."""
    chevron = f'image: url("{a["chevron"]}");' if "chevron" in a else ""
    return f"""
QLabel[role="title"] {{ font-size: 20pt; font-weight: 700; letter-spacing: -0.3px; }}
QLabel#brand {{ font-size: 12pt; color: {c['secondary']}; padding: 12px 14px 4px 14px; }}
QPushButton {{ background: {c['button']}; border: 1px solid #b9b9be; border-bottom-color: #a1a1a6;
              border-radius: 6px; padding: 4px 14px; min-height: 18px; }}
QPushButton:hover {{ border-color: #a1a1a6; }}
QPushButton:pressed {{ background: #e5e5ea; }}
QPushButton:focus {{ border: 2px solid #7fb0ee; padding: 3px 13px; }}
QPushButton[role="primary"] {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2f86f6, stop:1 {c['accent']});
                               color: {c['accent_text']}; border: 1px solid #0a55b6; font-weight: 600; }}
QPushButton[role="primary"]:pressed {{ background: #0a55b6; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    border: 1px solid #b9b9be; border-radius: 6px; padding: 4px 7px; background: {c['base']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid #7fb0ee; padding: 3px 6px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ {chevron} }}
QTabWidget::pane {{ border: 1px solid #d1d1d6; border-radius: 8px; top: -12px; padding-top: 14px; background: #e6e6ea; }}
QTabWidget QScrollArea, QTabWidget QScrollArea > QWidget > QWidget {{ background: #e6e6ea; }}
QTabWidget::tab-bar {{ alignment: center; }}
QTabBar::tab {{ background: #e3e3e8; color: {c['text']}; border: 1px solid #c9c9ce; border-right: none;
               padding: 4px 14px; margin: 0; border-radius: 0; }}
QTabBar::tab:first {{ border-top-left-radius: 7px; border-bottom-left-radius: 7px; }}
QTabBar::tab:last {{ border-top-right-radius: 7px; border-bottom-right-radius: 7px; border-right: 1px solid #c9c9ce; }}
QTabBar::tab:only-one {{ border-radius: 7px; border-right: 1px solid #c9c9ce; }}
QTabBar::tab:selected {{ background: {c['base']}; font-weight: 600; border-top-left-radius: 0; border-top-right-radius: 0; }}
QTabBar::tab:first:selected {{ border-top-left-radius: 7px; }}
QTabBar::tab:last:selected {{ border-top-right-radius: 7px; }}
QGroupBox {{ background: #f5f5f7; border: 1px solid #d9d9de; border-radius: 10px; }}
QFrame[role="kpi"], QFrame[role="card"] {{ background: {c['base']}; border: 1px solid #d9d9de; border-radius: 10px; }}
QTableWidget, QTableView, QTreeView, QListView {{ border: 1px solid #d9d9de; border-radius: 8px;
    gridline-color: transparent; alternate-background-color: #f5f5f7; }}
QHeaderView::section {{ background: {c['base']}; color: {c['secondary']}; border: none;
                        border-bottom: 1px solid #d9d9de; font-weight: 500; padding: 5px 8px; }}
QListWidget#nav {{ background: {c['nav']}; padding: 6px 8px; }}
QListWidget#nav::item {{ border-radius: 6px; padding: 6px 10px; margin: 1px 0; }}
QListWidget#nav::item:hover {{ background: #dedee3; }}
QListWidget#nav::item:selected {{ background: {c['nav_selected']}; color: {c['nav_selected_text']}; }}
QWidget#navPanel {{ background: {c['nav']}; border-right: 1px solid #d1d1d6; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #b0b0b5; border-radius: 4px; background: {c['base']}; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid #b0b0b5; border-radius: 8px; background: {c['base']}; }}
QScrollBar:vertical {{ width: 10px; }}
QScrollBar:horizontal {{ height: 10px; }}
QScrollBar::handle {{ background: #c4c4c9; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:hover {{ background: #a1a1a6; }}
QMenu {{ border-radius: 8px; border: 1px solid #d1d1d6; padding: 5px; }}
QMenu::item {{ border-radius: 5px; padding: 4px 16px; }}
QMenu::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QToolTip {{ border-radius: 6px; border: 1px solid #d1d1d6; }}
QStatusBar {{ color: {c['secondary']}; border-top: 1px solid #d9d9de; }}
"""


def _hacker(c, a):
    """A terminal, not a desktop: transparent controls framed in phosphor green that light up when touched,
    fields that are only an underline, dashed rules, inverted selections."""
    return f"""
QLabel[role="title"] {{ border-bottom: 1px dashed {c['border']}; padding-bottom: 4px; }}
QPushButton {{ background: transparent; border: 1px solid {c['border']}; border-radius: 0; padding: 4px 14px; }}
QPushButton:hover, QPushButton:pressed {{ background: {c['accent']}; color: {c['accent_text']}; border-color: {c['accent']}; }}
QPushButton:focus {{ border-color: {c['accent']}; }}
QPushButton[role="primary"] {{ background: transparent; color: {c['accent']}; border: 1px solid {c['accent']}; }}
QPushButton[role="primary"]:hover {{ background: {c['accent']}; color: {c['accent_text']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ border: none; border-bottom: 1px solid {c['border']}; border-radius: 0;
    background: transparent; padding: 3px 2px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: none; border-bottom: 2px solid {c['accent']}; }}
QPlainTextEdit, QTextEdit {{ border: 1px dashed {c['border']}; border-radius: 0; }}
QTableWidget, QTableView, QTreeView, QListView {{ border: 1px dashed {c['border']}; border-radius: 0; gridline-color: {c['window']}; }}
QHeaderView::section {{ background: {c['window']}; border: none; border-bottom: 1px dashed {c['border']}; color: {c['accent']}; }}
QGroupBox, QFrame[role="kpi"], QFrame[role="card"] {{ border: 1px dashed {c['border']}; border-radius: 0; background: transparent; }}
QTabBar::tab {{ border: none; border-bottom: 2px solid transparent; padding: 5px 12px; }}
QTabBar::tab:selected {{ border-bottom: 2px solid {c['accent']}; background: transparent; color: {c['accent']}; }}
QTabWidget::pane {{ border: 1px dashed {c['border']}; border-radius: 0; }}
QListWidget#nav::item {{ border-radius: 0; }}
QListWidget#nav::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QScrollBar::handle {{ border-radius: 0; }}
"""


def _fallout(c, a):
    """A Pip-Boy: scan lines on every screen surface, thick rounded frames, inverted title bars and selections."""
    scan = f'background-image: url("{a["scanlines"]}");' if "scanlines" in a else ""
    return f"""
QMainWindow, QDialog {{ {scan} }}
QTableWidget, QTableView, QTreeView, QListView, QPlainTextEdit, QTextEdit {{ {scan} border: 2px solid {c['border']};
    border-radius: 6px; }}
QLabel[role="title"] {{ background: {c['accent']}; color: {c['accent_text']}; padding: 2px 10px; border-radius: 4px; }}
QLabel[role="section"] {{ color: {c['title']}; border-bottom: 2px solid {c['border']}; }}
QLabel#brand {{ background: {c['accent']}; color: {c['accent_text']}; border-radius: 4px; margin: 6px; padding: 4px 10px; }}
QPushButton {{ border: 2px solid {c['border']}; border-radius: 6px; background: {c['button']}; padding: 4px 14px; }}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton[role="primary"], QPushButton:pressed {{ background: {c['accent']}; color: {c['accent_text']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ border: 2px solid {c['border']}; border-radius: 6px; }}
QGroupBox, QFrame[role="kpi"], QFrame[role="card"] {{ border: 2px solid {c['border']}; border-radius: 8px; }}
QTabBar::tab {{ border: 2px solid {c['border']}; border-radius: 6px; margin: 0 3px 4px 0; padding: 3px 12px; }}
QTabBar::tab:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QTabWidget::pane {{ border: 2px solid {c['border']}; border-radius: 8px; top: 0; }}
QHeaderView::section {{ background: {c['node_header']}; color: {c['title']}; border: none; }}
QListWidget#nav {{ border-right: 2px solid {c['border']}; }}
"""


def _amber(c, a):
    """A VT220: double-ruled frames, square everything, reverse-video selections, no hover effects."""
    return f"""
QPushButton {{ border: 3px double {c['border']}; border-radius: 0; background: {c['window']}; padding: 3px 14px; }}
QPushButton:hover {{ border-color: {c['border']}; }}
QPushButton:pressed, QPushButton[role="primary"] {{ background: {c['accent']}; color: {c['accent_text']};
    border-color: {c['accent']}; }}
QPushButton:focus {{ border-color: {c['accent']}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{ border: 1px solid {c['border']}; border-radius: 0; }}
QTableWidget, QTableView, QTreeView, QListView, QGroupBox, QFrame[role="kpi"], QFrame[role="card"] {{
    border: 3px double {c['border']}; border-radius: 0; }}
QTabBar::tab {{ border: 1px solid {c['border']}; border-radius: 0; padding: 3px 12px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QTabWidget::pane {{ border: 3px double {c['border']}; border-radius: 0; }}
QHeaderView::section {{ border: none; border-bottom: 3px double {c['border']}; }}
QLabel[role="title"] {{ border-top: 3px double {c['border']}; border-bottom: 3px double {c['border']}; padding: 2px 0; }}
QListWidget#nav {{ border-right: 3px double {c['border']}; }}
QListWidget#nav::item {{ border-radius: 0; }}
QListWidget#nav::item:selected {{ background: {c['accent']}; color: {c['accent_text']}; }}
QScrollBar::handle {{ border-radius: 0; }}
"""


def _cyberpunk(c, a):
    """Neon: pink-to-purple gradient buttons, cyan focus glow lines, yellow titles over a cyan rule, cut
    corners suggested by asymmetric radii."""
    return f"""
QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2a0a5e, stop:1 #12043a);
    border: 1px solid #c03fd0; border-radius: 0; border-top-right-radius: 8px; border-bottom-left-radius: 8px;
    padding: 5px 16px; color: {c['text']}; }}
QPushButton:hover {{ border-color: #05d9e8; color: #05d9e8; }}
QPushButton[role="primary"] {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ff2a6d, stop:1 #c03fd0);
    color: {c['accent_text']}; border: 1px solid #ff2a6d; font-weight: 700; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid #05d9e8; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{ border-radius: 0;
    border-top-right-radius: 6px; border-bottom-left-radius: 6px; }}
QLabel[role="title"] {{ color: #f9f002; border-bottom: 2px solid #05d9e8; padding-bottom: 3px; letter-spacing: 2px; }}
QLabel#brand {{ color: #ff2a6d; letter-spacing: 2px; }}
QTabBar::tab:selected {{ border: none; border-bottom: 3px solid #ff2a6d; background: transparent; color: #f9f002; }}
QTabBar::tab {{ border: none; border-bottom: 3px solid transparent; }}
QGroupBox, QFrame[role="kpi"], QFrame[role="card"] {{ border: 1px solid #c03fd0; border-radius: 0;
    border-top-right-radius: 12px; border-bottom-left-radius: 12px; }}
QHeaderView::section {{ color: #05d9e8; border-bottom: 1px solid #ff2a6d; }}
QListWidget#nav::item:selected {{ border-radius: 0; border-left: 4px solid #f9f002; }}
"""


def _unicorn(c, a):
    """Soft and round: pill buttons with a pastel gradient, pill tabs, cards with generous radii."""
    return f"""
QPushButton {{ border-radius: 14px; padding: 6px 18px; border: 1px solid {c['border']};
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #fbe9ff); }}
QPushButton:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffe3f3, stop:1 #e9dcff); }}
QPushButton[role="primary"] {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #c2185b, stop:1 #7a1fa2);
    color: #ffffff; border: none; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ border-radius: 12px; padding: 4px 10px; }}
QTabBar::tab {{ border-radius: 12px; margin: 2px 3px; padding: 4px 14px; border: 1px solid transparent; }}
QTabBar::tab:selected {{ background: #e7d1ff; color: {c['nav_selected_text']}; border: 1px solid {c['border']};
    border-radius: 12px; }}
QTabWidget::pane {{ border-radius: 16px; top: 0; }}
QGroupBox, QFrame[role="kpi"], QFrame[role="card"], QTableWidget, QTableView {{ border-radius: 16px; }}
QListWidget#nav::item {{ border-radius: 14px; }}
QLabel[role="title"] {{ color: #7a1fa2; }}
"""


EXTRAS = {"win311": _win311, "mac": _mac, "hacker": _hacker, "fallout": _fallout, "amber": _amber,
          "cyberpunk": _cyberpunk, "unicorn": _unicorn}


# The navigation as a strip of icons (MainWindow.set_compact_nav sets the property): no room is lost to the paddings
# the titles need, so each icon fits in its narrow row whatever the theme pads (with them it was cut away). The
# attribute makes these rules win over every theme's own QListWidget#nav::item.
COMPACT_NAV = """
QListWidget#nav[compact="true"] { padding: 4px 0px; }
QListWidget#nav[compact="true"]::item { padding: 6px 0px 6px 8px; margin: 1px 4px; }
QListWidget#nav[compact="true"]::item:disabled { padding: 0px; }
"""


def stylesheet():
    t = data()
    c, s, r = t["colors"], t["status"], t["radius"]
    small = max(0, r - 2)
    bevel = t.get("bevel")
    gradient = t.get("gradient")
    nav_background = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, " +
                      ", ".join(f"stop:{i / (len(gradient) - 1):.2f} {g}" for i, g in enumerate(gradient)) + ")"
                      if gradient else c["nav"])
    button = (f"QPushButton {{ padding: 4px 14px; {_bevel(c)} border-radius: 0; background: {c['button']};"
              f" min-height: 18px; }}\n"
              f"QPushButton:pressed {{ {_bevel(c, pressed=True)} }}\n"
              if bevel else
              f"QPushButton {{ padding: 6px 14px; border: 1px solid {c['border']}; border-radius: {small}px;"
              f" background: {c['button']}; min-height: 18px; }}\n"
              f"QPushButton:hover {{ border-color: {c['accent']}; }}\n"
              f"QPushButton:pressed {{ background: {c['alternate']}; }}\n"
              f"QPushButton:focus {{ border: 2px solid {c['accent']}; padding: 5px 13px; }}\n")
    images = assets() if _current in THEMES_DATA else {}
    check = f'image: url("{images["check"]}");' if "check" in images else ""
    extra = EXTRAS[t["extra"]](c, images) if t.get("extra") in EXTRAS else ""
    frame = f"border: 1px solid {c['border']}; border-radius: {r}px; background: {c['base']};"
    if bevel:
        frame = f"{_bevel(c, pressed=True)} border-radius: 0; background: {c['base']};"
    return f"""
QWidget {{ font-size: {t['font']['size'] if t.get('font') else 10}pt; }}
QMainWindow, QDialog {{ background: {c['window']}; }}
QLabel[role="title"] {{ font-size: 17pt; font-weight: 700; padding: 2px 0 2px 0; color: {c['title']}; }}
QLabel[role="section"] {{ font-size: 11pt; font-weight: 600; padding-top: 6px; color: {c['title']}; }}
QLabel[role="secondary"] {{ color: {c['secondary']}; }}
QLabel[role="notice-warn"] {{ color: {s['warn']}; }}
QLabel[role="notice-error"] {{ color: {s['bad']}; font-weight: 600; }}
QLabel[role="notice-ok"] {{ color: {s['ok']}; }}
QLabel[role="kpi-value"] {{ font-size: 18pt; font-weight: 700; color: {c['accent'] if t['dark'] else c['title']}; }}
QLabel#brand {{ font-size: 13pt; font-weight: 700; color: {c['nav_text']}; padding: 10px 12px 6px 12px;
               background: transparent; }}
QFrame[role="kpi"], QFrame[role="card"] {{ {frame} }}
QGroupBox {{ {frame} margin-top: 14px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['title']};
                    font-weight: 600; }}
{button}QPushButton:disabled {{ color: {c['disabled']}; border-color: {c['disabled']}; }}
QPushButton[role="primary"] {{ background: {c['accent']}; color: {c['accent_text']}; border-color: {c['accent']};
                               font-weight: 600; }}
QPushButton[role="primary"]:hover {{ border-color: {c['text']}; }}
QPushButton[role="primary"]:disabled {{ background: {c['button']}; color: {c['disabled']};
                                        border-color: {c['disabled']}; }}
QPushButton[role="danger"] {{ color: {c['danger']}; border-color: {c['danger']}; }}
QPushButton[role="danger"]:disabled {{ color: {c['disabled']}; border-color: {c['disabled']}; }}
QToolButton {{ padding: 4px 8px; border: 1px solid transparent; border-radius: {small}px; }}
QToolButton:hover {{ border-color: {c['border']}; background: {c['alternate']}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    border: 1px solid {c['border']}; border-radius: {small}px; padding: 4px 6px; background: {c['base']};
    color: {c['text']}; selection-background-color: {c['highlight']}; selection-color: {c['highlight_text']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {c['accent']}; }}
QComboBox QAbstractItemView {{ background: {c['base']}; color: {c['text']}; border: 1px solid {c['border']};
                               selection-background-color: {c['highlight']}; selection-color: {c['highlight_text']}; }}
QTableWidget, QTableView, QTreeView, QListView {{ border: 1px solid {c['border']}; border-radius: {small}px;
    background: {c['base']}; alternate-background-color: {c['alternate']}; gridline-color: {c['alternate']};
    selection-background-color: {c['highlight']}; selection-color: {c['highlight_text']}; }}
QHeaderView::section {{ padding: 6px 8px; border: none; border-bottom: 1px solid {c['border']};
                        background: {c['window']}; color: {c['title']}; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: {small}px; top: -1px; }}
QTabBar::tab {{ padding: 7px 14px; border: 1px solid transparent; border-bottom: none; color: {c['secondary']}; }}
QTabBar::tab:selected {{ border-color: {c['border']}; background: {c['base']}; color: {c['text']};
                         border-top-left-radius: {small}px; border-top-right-radius: {small}px; font-weight: 600; }}
QTabBar::tab:hover {{ color: {c['text']}; }}
QListWidget#nav {{ border: none; background: {nav_background}; padding: 6px 6px; outline: none;
                   color: {c['nav_text']}; }}
QListWidget#nav::item {{ padding: 8px 10px; margin: 1px 0; border-radius: {small}px; color: {c['nav_text']}; }}
QListWidget#nav::item:hover {{ background: {c['alternate']}; }}
QListWidget#nav::item:selected {{ background: {c['nav_selected']}; color: {c['nav_selected_text']};
                                  font-weight: 600; }}
QListWidget#nav::item:disabled {{ background: transparent; color: {c['nav_text']}; padding: 12px 10px 2px 10px;
                                  font-size: 8pt; font-weight: 700; }}
QWidget#navPanel {{ background: {nav_background}; }}
QWidget#projectBar {{ background: {c['window']}; }}
QWidget#tabStrip {{ background: {c['window']}; border-bottom: 1px solid {c['border']}; }}
QTabBar#projectTabs::tab {{ padding: 6px 12px; margin-right: 2px; border: 1px solid transparent; border-bottom: none;
                            color: {c['secondary']}; }}
QTabBar#projectTabs::tab:selected {{ border-color: {c['border']}; background: {c['base']}; color: {c['text']};
                                     font-weight: 600; border-top-left-radius: {small}px;
                                     border-top-right-radius: {small}px; }}
QTabBar#projectTabs::tab:hover {{ color: {c['text']}; }}
QToolButton#tabClose {{ padding: 0; border: none; }}
QToolButton#tabClose:hover {{ background: {c['alternate']}; border-radius: {small}px; }}
QDockWidget {{ color: {c['title']}; }}
QDockWidget::title {{ background: {c['alternate']}; padding: 5px 8px; border: 1px solid {c['border']}; }}
QLabel#projectName, QComboBox#projectName {{ font-size: 14pt; font-weight: 700; color: {c['title']}; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {c['border']};
                                                 background: {c['base']}; border-radius: {min(3, r)}px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; {check} }}
QRadioButton::indicator:checked {{ border-color: {c['accent']}; background: qradialgradient(cx:0.5, cy:0.5,
    radius:0.5, fx:0.5, fy:0.5, stop:0 {c['accent']}, stop:0.5 {c['accent']}, stop:0.6 {c['base']}, stop:1 {c['base']}); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {c['disabled']}; }}
QListWidget::item {{ padding: 3px 4px; }}
QToolTip {{ border: 1px solid {c['border']}; padding: 5px; background: {c['tooltip']}; color: {c['text']}; }}
QFrame#helpPopover {{ background: {c['base']}; border: 2px solid {c['accent']}; border-radius: {small}px; }}
QFrame#helpPopover QLabel {{ background: transparent; }}
QScrollBar:vertical {{ width: 11px; background: transparent; margin: 0; }}
QScrollBar:horizontal {{ height: 11px; background: transparent; margin: 0; }}
QScrollBar::handle {{ background: {c['border']}; border-radius: {min(5, r)}px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {c['secondary']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{ color: {c['secondary']}; }}
QSplitter::handle {{ background: {c['alternate']}; }}
QMenu {{ background: {c['base']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: {small}px; }}
QMenu::item:selected {{ background: {c['highlight']}; color: {c['highlight_text']}; }}
""" + extra + header_edges(c, bevel) + COMPACT_NAV


def header_edges(c, bevel=False):
    """A line between the titles of a table's or tree's columns, where one is grabbed to make it wider or narrower
    (or to move it, where it moves): after every theme's own rule for the titles, in its border colour. The raised
    titles of a bevelled theme show their edges already."""
    if bevel:
        return ""
    return f"QHeaderView::section:horizontal {{ border-right: 1px solid {c['border']}; }}\n"


def font(name=None):
    """The QFont of theme NAME (default: the one in use), or None for the system's font."""
    spec = THEMES_DATA.get(name or _current, {}).get("font")
    if not spec:
        return None
    result = QFont()
    result.setFamilies(list(spec["families"]))
    result.setPointSize(spec["size"])
    if spec["families"] and spec["families"][0] in MONO + ["Share Tech Mono", "VT323"]:
        result.setStyleHint(QFont.StyleHint.Monospace)
    return result


def set_fonts(ui=None, mono=None):
    """Use the font family UI for the app's text and MONO for the terminal (None: the theme's own); apply() then
    applies them. fonts.use() calls it with installed families only."""
    _fonts.update(ui=ui or None, mono=mono or None)


def chosen_fonts():
    return dict(_fonts)


def with_ui_font(base):
    """BASE (a QFont) with the interface font chosen in the settings first, the theme's families after it."""
    if not _fonts["ui"]:
        return base
    result = QFont(base)
    result.setFamilies([_fonts["ui"], *[f for f in base.families() if f != _fonts["ui"]]])
    return result


def apply_font(app=None):
    """The app's font again, with the interface font chosen in the settings (fonts.use): only the font, not the
    whole theme (its style sheet stays as it is)."""
    app = app or QApplication.instance()
    if app is None or _system is None:
        return
    app.setFont(with_ui_font(_system[2] if _current == "system" else font(_current) or _system[2]))


_style_name = None  # the base style apply() set last ("Fusion", or the system's)


def apply(mode, app=None):
    """Switch the whole application to theme MODE (one of THEMES; anything else is "system")."""
    global _system, _current
    app = app or QApplication.instance()
    if mode not in THEMES:
        mode = "system"
    if _system is None:
        _system = (app.style().name(), QPalette(app.palette()), QFont(app.font()))
    # A new QStyle only when the style really changes: setStyle() deletes the old one, and code still running
    # inside a widget's event (the theme's own combo box, a menu) then used freed memory (a crash on theme change).
    # (app.style() is Qt's style-sheet proxy once a sheet is set, whose name is not the base style's: the base
    # style set last is remembered here instead.)
    global _style_name
    wanted = _system[0] if mode == "system" else "Fusion"
    if (_style_name or app.style().name()).lower() != str(wanted).lower():
        app.setStyle(wanted)
    _style_name = str(wanted)
    if mode == "system":
        app.setPalette(_system[1])
        app.setFont(with_ui_font(_system[2]))
    else:
        app.setPalette(palette(mode))
        app.setFont(with_ui_font(font(mode) or _system[2]))
    _current = mode
    sheet = stylesheet()
    if app.styleSheet() != sheet:  # the same sheet again would re-polish every widget for nothing
        if app.styleSheet():
            # Qt goes from one sheet to another about six times slower (about 3 s for this window) than from none
            # to one; clearing first, in the same turn of the event loop, shows nothing unstyled in between.
            app.setStyleSheet("")
        app.setStyleSheet(sheet)
    return mode


def apply_soon(mode, after=None):
    """apply(MODE) once the event that asked for it is over (a combo box's or a menu's own handling must not run on
    a style that apply() replaced), with a busy cursor while the widgets are polished; then AFTER(mode)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QCursor

    def run():
        app = QApplication.instance()
        app.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            applied = apply(mode)
        finally:
            app.restoreOverrideCursor()
        if after is not None:
            after(applied)
    QTimer.singleShot(0, run)


def current():
    return _current


def is_dark(p=None):
    if p is None and _current in THEMES_DATA:
        return THEMES_DATA[_current]["dark"]
    app = QApplication.instance()
    p = p or (app.palette() if app else QPalette())
    return p.color(QPalette.ColorRole.Window).lightness() < 128


def color(name):
    """A status colour ("ok", "warn", "bad", "muted") readable on the current background."""
    return data()["status"][name]


def terminal_theme():
    """The terminal's colours and font for the theme in use, as JSON for the terminal page (xterm.js)."""
    t = data()
    spec = THEMES_DATA.get(_current, {}).get("font")
    families = [f for f in (spec["families"] if spec else []) if f in MONO + ["Share Tech Mono", "VT323"]]
    families = families or MONO
    if _fonts["mono"]:  # the settings' monospace font first, the theme's after it
        families = [_fonts["mono"], *[f for f in families if f != _fonts["mono"]]]
    return json.dumps({**t["terminal"], "light": not t["dark"],
                       "fontFamily": ", ".join(f'"{f}"' if " " in f else f for f in families)})


def dot(name, size=10):
    """A small round icon in a status colour: status stays visible in a selected table row."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color(name)))
    painter.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    painter.end()
    return QIcon(pixmap)


# A shield's outline on the 24 x 24 grid (the Approvals page's icon and the project's trust states).
SHIELD = [(12, 3), (20, 6), (20, 12), (12, 21), (4, 12), (4, 6)]
# The navigation's icons: simple line drawings on a 24 x 24 grid, painted in the navigation's text colour.
GLYPHS = {
    "launch": [("poly", [(8, 5), (19, 12), (8, 19)], True)],
    "board": [("rect", (3, 4, 7, 6)), ("rect", (14, 14, 7, 6)), ("rect", (14, 4, 7, 6)),
              ("line", (10, 7, 14, 7)), ("line", (17.5, 10, 17.5, 14))],
    "sandboxes": [("rect", (3, 5, 18, 14)), ("line", (3, 9, 21, 9)), ("dot", (6, 7))],
    "approvals": [("poly", SHIELD, True),
                  ("poly", [(8, 12), (11, 15), (16, 9)], False)],
    "connections": [("dot", (5, 12)), ("dot", (19, 6)), ("dot", (19, 18)), ("line", (5, 12, 19, 6)),
                    ("line", (5, 12, 19, 18))],
    "activity": [("poly", [(3, 12), (7, 12), (10, 5), (14, 19), (17, 12), (21, 12)], False)],
    "statistics": [("line", (5, 20, 5, 12)), ("line", (10, 20, 10, 6)), ("line", (15, 20, 15, 14)),
                   ("line", (20, 20, 20, 9))],
    "files": [("poly", [(3, 6), (9, 6), (11, 8), (21, 8), (21, 19), (3, 19)], True)],
    "mcp": [("rect", (4, 4, 16, 6)), ("rect", (4, 14, 16, 6)), ("dot", (7, 7)), ("dot", (7, 17))],
    "llm": [("rect", (3, 4, 18, 12)), ("poly", [(8, 16), (8, 21), (13, 16)], True), ("line", (7, 8, 17, 8)),
            ("line", (7, 12, 14, 12))],
    "images": [("poly", [(12, 3), (20, 7.5), (20, 16.5), (12, 21), (4, 16.5), (4, 7.5)], True),
               ("line", (12, 12, 12, 21)), ("line", (4, 7.5, 12, 12)), ("line", (20, 7.5, 12, 12))],
    "team": [("circle", (7, 8, 3)), ("circle", (17, 8, 3)), ("line", (3, 19, 11, 19)), ("line", (13, 19, 21, 19)),
             ("line", (7, 11, 7, 15)), ("line", (17, 11, 17, 15))],
    "credentials": [("circle", (8, 12, 4)), ("line", (12, 12, 21, 12)), ("line", (18, 12, 18, 16)),
                    ("line", (21, 12, 21, 15))],
    "terminals": [("rect", (3, 4, 18, 16)), ("poly", [(7, 9), (10, 12), (7, 15)], False), ("line", (12, 15, 17, 15))],
    "kits": [("rect", (3, 8, 18, 12)), ("poly", [(9, 8), (9, 5), (15, 5), (15, 8)], False), ("line", (3, 13, 21, 13)),
             ("line", (12, 11, 12, 15))],
    "project": [("poly", [(3, 6), (9, 6), (11, 8), (21, 8), (21, 19), (3, 19)], True), ("line", (3, 11, 21, 11))],
    # The window's own controls (the header bar), for desktops whose window manager shows none.
    "minimize": [("line", (5, 17, 19, 17))],
    "maximize": [("rect", (5, 5, 14, 14)), ("line", (5, 8, 19, 8))],
    "restore": [("rect", (4, 9, 11, 11)), ("poly", [(8, 9), (8, 5), (20, 5), (20, 16), (15, 16)], False)],
    "fullscreen": [("poly", [(4, 9), (4, 4), (9, 4)], False), ("poly", [(15, 4), (20, 4), (20, 9)], False),
                   ("poly", [(20, 15), (20, 20), (15, 20)], False), ("poly", [(9, 20), (4, 20), (4, 15)], False)],
    "tray": [("rect", (3, 15, 18, 5)), ("line", (12, 3, 12, 12)), ("poly", [(8, 8), (12, 12), (16, 8)], False)],
    "quit": [("line", (6, 6, 18, 18)), ("line", (18, 6, 6, 18))],
    "fold-open": [("poly", [(6, 9), (12, 15), (18, 9)], False)],
    "fold-closed": [("poly", [(9, 6), (15, 12), (9, 18)], False)],
    "system": [("circle", (12, 12, 3.5)), ("circle", (12, 12, 8))],
    "hosts": [("rect", (3, 4, 7, 16)), ("rect", (14, 4, 7, 16)), ("dot", (6.5, 8)), ("dot", (17.5, 8)),
              ("line", (10, 14, 14, 14))],
    "help": [("circle", (12, 12, 9)), ("poly", [(9, 9.5), (9.6, 7.4), (12, 6.6), (14.4, 7.4), (15, 9.5), (12, 12),
                                                 (12, 14)], False), ("dot", (12, 17.3))],
    # The state of a project's .sandbox-ai.json (the project bar; TRUST_GLYPHS): a shield, and in it what it means.
    "trust-none": [("poly", SHIELD, True)],  # empty: nothing to trust
    "trust-trusted": [("poly", SHIELD, True), ("poly", [(8.5, 12), (11, 14.5), (15.5, 9.5)], False)],
    "trust-untrusted": [("poly", SHIELD, True), ("line", (12, 7.5, 12, 12.5)), ("dot", (12, 15.5))],
    # changed since it was trusted: not equal to what you trusted (an equals sign struck through)
    "trust-changed": [("poly", SHIELD, True), ("line", (8.5, 10, 15.5, 10)), ("line", (8.5, 13.5, 15.5, 13.5)),
                      ("line", (13.5, 7.5, 10.5, 16))],
    "trust-unreadable": [("poly", SHIELD, True), ("line", (9.5, 8.5, 14.5, 13.5)), ("line", (14.5, 8.5, 9.5, 13.5))],
}
# Each state of SandboxAI.project_state: its glyph and the status colour it is drawn in.
TRUST_GLYPHS = {"none": ("trust-none", "muted"), "trusted": ("trust-trusted", "ok"),
                "untrusted": ("trust-untrusted", "warn"), "changed": ("trust-changed", "warn"),
                "unreadable": ("trust-unreadable", "bad")}


def _glyph_pixmap(name, color_, size):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 24, size / 24)
    tint = QColor(color_)
    pen = QPen(tint, 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for shape in GLYPHS.get(name, []):
        kind = shape[0]
        if kind == "line":
            x1, y1, x2, y2 = shape[1]
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        elif kind == "rect":
            x, y, w, h = shape[1]
            painter.drawRoundedRect(QRectF(x, y, w, h), 2, 2)
        elif kind == "circle":
            x, y, radius = shape[1]
            painter.drawEllipse(QPointF(x, y), radius, radius)
        elif kind == "dot":
            painter.setBrush(tint)
            painter.drawEllipse(QPointF(*shape[1]), 1.6, 1.6)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        elif kind == "poly":
            path = QPainterPath(QPointF(*shape[1][0]))
            for point in shape[1][1:]:
                path.lineTo(QPointF(*point))
            if shape[2]:
                path.closeSubpath()
            painter.drawPath(path)
    painter.end()
    return pixmap


_glyphs = {}  # (name, colour, selected colour or None, size): QIcon - the same icon is asked for again and again
GLYPHS_KEPT = 512


def glyph(name, color_=None, size=20):
    """The navigation icon NAME (a key of GLYPHS) in COLOR (default: the navigation's text colour; a
    selected item gets the colour of the selected navigation text). Drawn once per name, colours and size (a QIcon
    is shared, never changed in place)."""
    tint = color_ or colors()["nav_text"]
    selected = colors()["nav_selected_text"] if color_ is None else None
    key = (name, str(tint), str(selected) if selected is not None else None, size)
    icon = _glyphs.get(key)
    if icon is None:
        icon = QIcon(_glyph_pixmap(name, tint, size))
        if selected is not None:
            icon.addPixmap(_glyph_pixmap(name, selected, size), QIcon.Mode.Selected)
        if len(_glyphs) >= GLYPHS_KEPT:
            _glyphs.clear()
        _glyphs[key] = icon
    return QIcon(icon)


def trust_glyph(state, size=20):
    """The icon of a project's .sandbox-ai.json in STATE (a key of TRUST_GLYPHS), in its status colour (readable on
    the theme's background)."""
    name, status = TRUST_GLYPHS.get(state, TRUST_GLYPHS["none"])
    return glyph(name, color(status), size)


def counted_glyph(name, count, size=24):
    """The navigation icon NAME with COUNT on a badge in its corner (the strip of icons shows no text): in the
    navigation's colours, the badge in the "bad" status colour."""
    icon = QIcon()
    text = str(count) if count < 100 else "99+"
    for tint, mode in ((colors()["nav_text"], QIcon.Mode.Normal), (colors()["nav_selected_text"], QIcon.Mode.Selected)):
        pixmap = _glyph_pixmap(name, tint, size)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        badge = QFont()
        badge.setBold(True)
        badge.setPixelSize(max(7, int(size * 0.36)))
        painter.setFont(badge)
        width = max(size * 0.5, painter.fontMetrics().horizontalAdvance(text) + size * 0.16)
        area = QRectF(size - width, 0, width, size * 0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color("bad")))
        painter.drawRoundedRect(area, area.height() / 2, area.height() / 2)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(area, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


def app_icon(attention=False):
    """The window and tray icon: a shield on a rounded square (orange with a dot when a question waits)."""
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = size / 64
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1f63c6"))
        painter.drawRoundedRect(QRectF(2 * s, 2 * s, 60 * s, 60 * s), 13 * s, 13 * s)
        shield = QPainterPath()
        shield.moveTo(32 * s, 11 * s)
        shield.lineTo(49 * s, 17 * s)
        shield.cubicTo(49 * s, 34 * s, 43 * s, 46 * s, 32 * s, 53 * s)
        shield.cubicTo(21 * s, 46 * s, 15 * s, 34 * s, 15 * s, 17 * s)
        shield.closeSubpath()
        painter.setBrush(QColor("#ffffff"))
        painter.drawPath(shield)
        pen = QPen(QColor("#1f63c6"), max(1.0, 4 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(25 * s, 31 * s), QPointF(30 * s, 37 * s))
        painter.drawLine(QPointF(30 * s, 37 * s), QPointF(40 * s, 25 * s))
        if attention:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e8710a"))
            painter.drawEllipse(QRectF(40 * s, 2 * s, 22 * s, 22 * s))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def title_font():
    result = with_ui_font(font() or QFont())
    result.setPointSize(17)
    result.setWeight(QFont.Weight.Bold)
    return result
