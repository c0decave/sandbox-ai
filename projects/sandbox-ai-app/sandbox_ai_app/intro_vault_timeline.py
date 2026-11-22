"""The "Vault" intro's story, as numbers over time (no Qt; intro_vault.py draws it).

A blueprint. sandbox-ai's layers assemble around the agent like the rings of a vault, each verified as it locks:

1. plan    construction lines draw themselves, the agent's core appears in the middle: the AI;
2. rings   five rings, drawn by a compass and turned into place, lock one after the other - Seccomp,
           NoNewPrivs, Firejail's namespaces, rootless Podman or Bubblewrap, the firewall and the proxy - while the
           terminal beside them ticks each check off: the sandbox, verified (a run stops when a check fails);
3. probes  each layer stops something: a mount() call, sudo, a read of ~/.ssh, a connection to a tracker;
4. key     the rings turn until their notches line up, an allowed request passes, and at the outer ring the proxy
           adds the key - outside the sandbox; the agent never sees it;
5. title   the plan recedes: the mark, "sandbox-ai", the tagline and the agents' names (as the classic one);
6. out     everything fades and the app is there.

scene(t) says what is where at second t; scene(t, reduced=True) is the still title card.
"""
from __future__ import annotations

import math

from . import intro_timeline as C
from .intro_timeline import ease_in_out_cubic, ease_out_cubic, lerp, spring, window

WIDTH, HEIGHT = C.WIDTH, C.HEIGHT
PHASES = (("plan", 0.0, 0.95), ("rings", 0.95, 3.95), ("probes", 3.95, 5.1), ("key", 5.1, 6.6),
          ("title", C.TITLE_START, C.OUT[0]), ("out", *C.OUT))
DURATION = PHASES[-1][2]
STATIC_DURATION = C.STATIC_DURATION
CAPTIONS = ((0, 1.0, 3.95), (1, 3.95, 5.2), (2, 5.2, 6.3))

CENTRE = (540.0, 420.0)
CORE = 50.0  # the agent's core
RINGS = (96.0, 138.0, 180.0, 222.0, 264.0)  # the rings' middle radii, inside out
BAND = 20.0  # a ring's width
NOTCH = math.radians(15)  # the gap in each ring
RING_START, RING_STEP = 0.95, 0.56  # ring k starts drawing at RING_START + k * RING_STEP
COMPASS, FILL, LOCK = 0.42, 0.3, 0.62  # after its start: the compass has drawn it, it is filled, it locks
REST = tuple(math.radians(a) for a in (330, 250, 60, 140, 210))  # the notches' places at rest
EXIT = math.radians(17.5)  # the way out, towards the host (stage angles: y downwards)
# What each probe tries, the ring that stops it (its index), its direction and when it leaves the core.
PROBES = (("mount()", 0, math.radians(112), 4.0), ("sudo", 1, math.radians(66), 4.16),
          ("~/.ssh", 3, math.radians(236), 4.32), ("tracker.example.net", 4, math.radians(296), 4.48))
ALIGN = (4.85, 0.38, 0.06)  # the notches line up: from, how long, the stagger from ring to ring
REQUEST = (5.48, 5.88, 6.12)  # the allowed request: leaves, reaches the outer ring (the key is added), the host
HOST = "api.anthropic.com"
TERMINAL = (900.0, 136.0, 600.0, 272.0)  # x, y, width, height
CHIP = (1090.0, 560.0, 340.0, 60.0)  # the host: x, y, width, height
LEGEND = (40.0, 330.0, 45.0, 210.0)  # the rings' legend, left of them: x, first line's centre, step, width


def ring_times(k):
    start = RING_START + k * RING_STEP
    return start, start + COMPASS, start + FILL + COMPASS * 0.5, start + LOCK


def angle_of(k, t):
    """Where ring K's notch is at T (radians): turned into its place while it is made, lined up with the way out
    for the allowed request."""
    start, _drawn, _filled, lock = ring_times(k)
    turn = (1 if k % 2 else -1) * math.radians(150)
    rest = REST[k] + turn * (1 - ease_in_out_cubic(window(t, start, lock)))
    begin = ALIGN[0] + k * ALIGN[2]
    target = EXIT + 2 * math.pi * round((rest - EXIT) / (2 * math.pi))
    return lerp(rest, target, ease_in_out_cubic(window(t, begin, begin + ALIGN[1])))


def ring_state(k, t):
    start, drawn, filled, lock = ring_times(k)
    since = t - lock
    begin = ALIGN[0] + k * ALIGN[2]
    return {"still": t >= lock + 0.5 and not begin <= t < begin + ALIGN[1],  # (it does not move: one pixmap)
            "compass": ease_in_out_cubic(window(t, start, drawn)), "fill": ease_out_cubic(window(t, drawn - 0.1,
                                                                                                  filled)),
            "angle": angle_of(k, t), "lock": 1 - ease_out_cubic(since / 0.5) if 0 <= since < 0.5 else 0.0,
            "locked": t >= lock, "label": ease_out_cubic(window(t, lock - 0.1, lock + 0.3)),
            "hit": 0.0}


def probe_state(index, t):
    """Probe INDEX: its distance from the centre, stopped at the inner edge of its ring, a shake and a fade."""
    _what, ring, _angle, leave = PROBES[index]
    stop = RINGS[ring] - BAND / 2 - 8
    arrive = leave + 0.16 + 0.0011 * (stop - CORE)
    r = lerp(CORE + 6, stop, ease_in_out_cubic(window(t, leave, arrive)))
    since = t - arrive
    return {"r": r, "alpha": (1.0 if t >= leave else 0.0) * (1 - ease_in_out_cubic(window(t, arrive + 0.55,
                                                                                             arrive + 0.95))),
            "stopped": t >= arrive, "shake": math.sin(since * 46) * math.exp(-since * 8) if 0 <= since < 0.6 else 0.0,
            "flash": 1 - ease_out_cubic(since / 0.6) if 0 <= since < 0.6 else 0.0,
            "tag": ease_out_cubic(window(t, arrive - 0.05, arrive + 0.25)) * (1 - ease_in_out_cubic(
                window(t, 5.5, 5.85))),
            "arrive": arrive}


def request_state(t):
    """The allowed request: 0..1 from the core to the outer ring (through the notches), 1..2 on to the host; the
    key from the outer ring on; the host's badge."""
    leave, at_ring, at_host = REQUEST
    u = ease_in_out_cubic(window(t, leave, at_ring))
    if t >= at_ring:
        u = 1 + ease_in_out_cubic(window(t, at_ring, at_host))
    return {"u": u, "alpha": (1.0 if t >= leave else 0.0) * (1 - ease_out_cubic(window(t, at_host, at_host + 0.25))),
            "key": spring(window(t, at_ring, at_ring + 0.45), 6.0, 1.6) if t >= at_ring else 0.0,
            "flash": 1 - ease_out_cubic((t - at_ring) / 0.6) if 0 <= t - at_ring < 0.6 else 0.0,
            "badge": ease_out_cubic(window(t, at_host, at_host + 0.3)) if t >= at_host else 0.0}


def terminal_state(t):
    """How many check lines the terminal shows (a float: the fraction is the line being typed); the last line,
    "agent started", once the last ring locks."""
    lines = 1.0 if t >= 0.75 else 0.0  # the command
    for k in range(len(RINGS)):
        lock = ring_times(k)[3]
        lines += ease_out_cubic(window(t, lock - 0.05, lock + 0.2))
    lines += ease_out_cubic(window(t, ring_times(len(RINGS) - 1)[3] + 0.25, ring_times(len(RINGS) - 1)[3] + 0.5))
    return {"in": ease_out_cubic(window(t, 0.55, 1.0)), "lines": lines, "caret": (t * 2.2) % 1.0 < 0.6}


def scene(t, reduced=False):
    """Everything at second T (REDUCED: the still title card)."""
    t = max(0.0, t)
    if reduced:
        return dict(C.shared_static(t), story=None)
    rings = [ring_state(k, t) for k in range(len(RINGS))]
    probes = [probe_state(k, t) for k in range(len(PROBES))]
    for probe, (_what, ring, _angle, _leave) in zip(probes, PROBES):
        rings[ring]["hit"] = max(rings[ring]["hit"], probe["flash"])
    s = C.shared(t, PHASES, CAPTIONS, DURATION)
    s["story"] = {
        "lines": ease_in_out_cubic(window(t, 0.1, 0.85)),  # the construction lines
        "core": spring(window(t, 0.45, 1.1), 6.5, 1.5) if t >= 0.45 else 0.0,
        "spin": t * 0.9, "pulse": 0.5 + 0.5 * math.sin(t * 4.2),
        "rings": rings, "probes": probes, "request": request_state(t), "terminal": terminal_state(t),
        "chip": ease_out_cubic(window(t, 5.0, 5.45)),
        "aligned": ease_out_cubic(window(t, ALIGN[0] + ALIGN[1] + 4 * ALIGN[2] - 0.1,
                                         ALIGN[0] + ALIGN[1] + 4 * ALIGN[2] + 0.2)),
    }
    return s


def duration(reduced=False):
    return STATIC_DURATION if reduced else DURATION
