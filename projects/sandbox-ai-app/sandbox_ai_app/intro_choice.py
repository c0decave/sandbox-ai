"""Which intro animation plays (no Qt; intro.py plays it).

There are four, each telling AI + sandbox + security in its own way and ending on the same title card:

- classic  the mark opens up into a sandbox, an agent works in it, its requests pass the proxy (intro_timeline.py);
- glass    a network of light grows in a glass cube, its walls ripple, one port lets out (intro_glass_timeline.py);
- city     a night city of hosts: light trails reach the allowed towers, a decoy key trips the canary
           (intro_city_timeline.py);
- vault    a blueprint: the sandbox's layers lock around the agent like the rings of a vault, each stops
           something, the proxy adds the key on the way out (intro_vault_timeline.py).

The setting "intro_animation" (Settings > Appearance) is ROTATE (the default: a different one at each start) or
one of IDS. Rotating, each start plays the one after the last played (kept in the window's state as STATE_KEY),
so every one is seen and never two identical in a row. An unknown value rotates.
"""
from __future__ import annotations

import importlib

from .i18n import N_

IDS = ("classic", "glass", "city", "vault")
ROTATE = "rotate"
CHOICES = (ROTATE,) + IDS
SETTING = "intro_animation"
STATE_KEY = "intro_last"  # the id of the intro played last (state.json)
TITLES = {"classic": N_("Classic"), "glass": N_("Glass cube"), "city": N_("Night city"), "vault": N_("Vault")}
ROTATE_TITLE = N_("A different one at each start")
TIMELINES = {"classic": "intro_timeline", "glass": "intro_glass_timeline", "city": "intro_city_timeline",
             "vault": "intro_vault_timeline"}


def chosen(settings):
    """The setting's value: ROTATE or an intro's id (an unknown or damaged value: ROTATE)."""
    value = (settings or {}).get(SETTING, ROTATE)
    return value if isinstance(value, str) and value in CHOICES else ROTATE


def following(last):
    """The intro after LAST in the rotation (the first new one when none was played yet, or LAST is unknown)."""
    if last not in IDS:
        return IDS[1]
    return IDS[(IDS.index(last) + 1) % len(IDS)]


def pick(settings, last=None):
    """The intro to play now: the chosen one, or (rotating) the one after LAST."""
    choice = chosen(settings)
    return following(last) if choice == ROTATE else choice


def timeline(which):
    """The timeline module of intro WHICH (pure: scene(t, reduced), duration(reduced), PHASES, CAPTIONS)."""
    return importlib.import_module(f"{__package__}.{TIMELINES[which if which in IDS else IDS[0]]}")
