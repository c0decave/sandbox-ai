"""The intro animation's story, as numbers over time (no Qt; intro.py draws it).

1. mark      the sandbox-ai mark (a rounded square, a shield, a tick) draws itself, line by line;
2. open      it opens up into the sandbox: a panel with the mark in its header;
3. work      an agent (an abstract glyph) works inside it, code flowing in its editor;
4. requests  its requests leave through the proxy gate: two allowed ones pass in green, a tracker is stopped
             with a lock, a new host asks and passes once the approval is given;
5. title     the scene recedes: the mark, "sandbox-ai", the tagline and the three agents' names;
6. out       everything fades and the app is there.

Everything lives on a stage of WIDTH x HEIGHT units (intro.py scales it into the window); scene(t) says what
is where at second t, scene(t, reduced=True) is the short static title card for reduced motion. Values are
0..1 progressions, already eased, unless said otherwise.
"""
from __future__ import annotations

import math

WIDTH, HEIGHT = 1600.0, 900.0
# (name, start, end) in seconds; one after the other, without gaps
PHASES = (("mark", 0.0, 1.7), ("open", 1.7, 2.6), ("work", 2.6, 3.9), ("requests", 3.9, 6.6),
          ("title", 6.6, 8.2), ("out", 8.2, 8.7))
DURATION = PHASES[-1][2]
STATIC_DURATION = 2.4  # the reduced-motion title card
STATIC_FADE = 0.3

# The stage's geometry (x, y, width, height or centres), in stage units.
PANEL = (200.0, 190.0, 660.0, 430.0)  # the sandbox
HEADER = 64.0  # its header band
MARK_BIG = (800.0, 400.0, 196.0)  # centre x, y and size of the mark while it draws itself
MARK_HEADER = (PANEL[0] + 44.0, PANEL[1] + HEADER / 2, 30.0)  # ... in the panel's header
MARK_TITLE = (800.0, 292.0, 120.0)  # ... above the title
AGENT = (322.0, 430.0, 50.0)  # centre and radius of the agent's glyph
CODE = (452.0, 266.0, 380.0, 146.0)  # the agent's editor, above the way out
ALLOW = (452.0, 450.0, 380.0, 146.0)  # the allow-list, below it
GATE = (PANEL[0] + PANEL[2], 430.0)  # the proxy gate, on the panel's right edge
GATE_SIZE = (34.0, 150.0)
CHIP = (1060.0, 360.0, 64.0)  # x, width, height of a destination
CHIP_Y = (262.0, 370.0, 478.0, 586.0)  # their centres
CARD = (670.0, 664.0, 380.0, 92.0)  # the approval question, below the gate
CAPTION_Y = 800.0  # the centre of the beat's caption
TITLE_Y = (456.0, 560.0, 622.0)  # centres of the word mark, the tagline and the agents' names

# The requests: (host, what the proxy does, when it leaves the agent). "ask" waits for the approval.
REQUESTS = (("api.anthropic.com", "allow", 3.95), ("pypi.org", "allow", 4.15),
            ("tracker.example.net", "block", 4.35), ("github.com", "ask", 4.55))
LEG = (0.45, 0.4)  # seconds from the agent to the gate, from the gate to the destination
ASK = (0.1, 0.45, 0.1)  # after the arrival at the gate: the card appears, Allow is pressed, the packet goes on
# The captions: (index, from, to); intro.py has their texts.
CAPTIONS = ((0, 2.55, 3.9), (1, 3.9, 5.0), (2, 5.0, 6.55))
CODE_START, CODE_RATE = 2.75, 5.5  # code lines per second
CODE_VISIBLE = 5  # lines the editor shows


# -- easing --

def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def lerp(a, b, f):
    return a + (b - a) * f


def ease_out_cubic(f):
    f = clamp(f)
    return 1 - (1 - f) ** 3


def ease_in_out_cubic(f):
    f = clamp(f)
    return 4 * f ** 3 if f < 0.5 else 1 - (-2 * f + 2) ** 3 / 2


def ease_in_out_expo(f):
    f = clamp(f)
    if f in (0.0, 1.0):
        return f
    return 2 ** (20 * f - 10) / 2 if f < 0.5 else (2 - 2 ** (-20 * f + 10)) / 2


def spring(f, damping=7.0, frequency=2.2):
    """0 to 1 with a soft overshoot that settles (a damped spring), exactly 1 at the end."""
    f = clamp(f)
    if f >= 1:
        return 1.0
    return 1 - math.exp(-damping * f) * math.cos(2 * math.pi * frequency * f) * (1 - f)


def window(t, start, end):
    """0..1: how far T is between START and END (not eased)."""
    return clamp((t - start) / (end - start)) if end > start else float(t >= start)


def progress(t, phase):
    """0..1: how far phase PHASE is at T (0 before it, 1 after it)."""
    for name, start, end in PHASES:
        if name == phase:
            return window(t, start, end)
    raise KeyError(phase)


def phase_at(t):
    for name, _start, end in PHASES:
        if t < end:
            return name
    return "done"


# -- the parts --

PAN = WIDTH / 2 - (PANEL[0] + PANEL[2] / 2)  # the sandbox starts in the middle, moves left for the gate


def pan(t):
    """How far (stage units) the sandbox and everything around it is right of its place at T."""
    return PAN * (1 - ease_in_out_cubic(window(t, 3.5, 4.2)))


def opening(t):
    """The mark opening up into the sandbox: the panel grows out of it while it moves into the header."""
    return ease_in_out_expo(window(t, 1.7, 2.55))


def mark_state(t):
    """The mark: its three strokes (square, shield, tick), its fill and where it is."""
    to_header = opening(t)
    to_title = ease_in_out_cubic(window(t, 6.5, 7.3))
    header = (MARK_HEADER[0] + pan(t), MARK_HEADER[1], MARK_HEADER[2])
    x, y, size = (lerp(a, b, to_header) for a, b in zip(MARK_BIG, header))
    if to_title > 0:
        x, y, size = (lerp(a, b, to_title) for a, b in zip((x, y, size), MARK_TITLE))
    return {"square": ease_in_out_cubic(window(t, 0.15, 0.95)), "shield": ease_in_out_cubic(window(t, 0.55, 1.25)),
            "tick": ease_out_cubic(window(t, 1.05, 1.42)), "fill": ease_out_cubic(window(t, 1.2, 1.7)),
            "glow": math.sin(math.pi * window(t, 1.15, 2.1)) if t < 2.1 else 0.0,
            "ring": window(t, 1.38, 2.1),  # a ring going out once the tick is there
            "sweep": window(t, 7.25, 8.0),  # a light sweeping over it on the title card
            "x": x, "y": y, "size": size, "header": to_header * (1 - to_title)}


def code_lines(t):
    """How many lines the agent has typed by T (a float: the fraction is the line being typed)."""
    return max(0.0, (t - CODE_START) * CODE_RATE)


def request_state(index, t):
    """Where request INDEX is: leg 0..1 (agent to gate) then 1..2 (gate to destination); its outcome."""
    host, verdict, leave = REQUESTS[index]
    at_gate = leave + LEG[0]
    u = ease_in_out_cubic(window(t, leave, at_gate))
    state, alpha, shake, wait = "travel", 1.0 if t >= leave else 0.0, 0.0, 0.0
    arrive = None
    if t >= at_gate:
        if verdict == "block":
            state = "blocked"
            since = t - at_gate
            shake = math.sin(since * 42) * math.exp(-since * 7) if since < 0.6 else 0.0
            alpha = 1 - ease_out_cubic(window(t, at_gate + 0.45, at_gate + 0.85))
        else:
            go = at_gate if verdict == "allow" else at_gate + sum(ASK)
            if verdict == "ask" and t < go:
                state, wait = "waiting", t - at_gate
            u = 1 + ease_in_out_cubic(window(t, go, go + LEG[1]))
            arrive = go + LEG[1]
            if t >= arrive:
                state = "arrived"
                alpha = 1 - ease_out_cubic(window(t, arrive, arrive + 0.2))
    # the destination's badge: allowed, blocked or approved, once it is known
    decided = at_gate if verdict == "block" else arrive
    badge = ease_out_cubic(window(t, decided, decided + 0.3)) if decided is not None else 0.0
    return {"host": host, "verdict": verdict, "u": u, "state": state, "alpha": alpha, "shake": shake,
            "wait": wait, "badge": badge, "leave": leave, "at_gate": at_gate}


def gate_flash(t):
    """(kind, strength) of the gate's flashes at T: "ok" when one passes, "bad" for the blocked one, "warn" while
    one waits."""
    flashes = []
    for index, (_, verdict, leave) in enumerate(REQUESTS):
        at_gate = leave + LEG[0]
        if verdict == "ask":
            go = at_gate + sum(ASK)
            if at_gate <= t < go:
                flashes.append(("warn", 0.55 + 0.45 * math.sin((t - at_gate) * 9)))
            at_gate = go
        since = t - at_gate
        if 0 <= since < 0.5:
            flashes.append(("bad" if verdict == "block" else "ok", 1 - ease_out_cubic(since / 0.5)))
    return flashes


def approval_state(t):
    index = next(i for i, r in enumerate(REQUESTS) if r[1] == "ask")
    at_gate = REQUESTS[index][2] + LEG[0]
    shown, press, go = at_gate + ASK[0], at_gate + ASK[0] + ASK[1], at_gate + sum(ASK)
    return {"index": index, "in": spring(window(t, shown, shown + 0.5), 6.0, 1.6) if t >= shown else 0.0,
            "press": math.sin(math.pi * window(t, press - 0.18, press + 0.06)),
            "done": ease_out_cubic(window(t, press, press + 0.2)),
            "out": ease_in_out_cubic(window(t, go + 0.25, go + 0.6))}


def caption_state(t):
    """The caption shown at T: (index, alpha, rise) with rise 0..1 (1: still below its place) or None."""
    for index, start, end in CAPTIONS:
        if start <= t < end:
            fade_in = ease_out_cubic(window(t, start, start + 0.45))
            fade_out = 1 - ease_in_out_cubic(window(t, end - 0.25, end))
            return {"index": index, "alpha": fade_in * fade_out, "rise": 1 - fade_in}
    return None


def title_state(t):
    return {"word": window(t, 6.85, 7.55), "tag": ease_out_cubic(window(t, 7.25, 7.8)),
            "agents": ease_out_cubic(window(t, 7.45, 8.0))}


def scene(t, reduced=False):
    """Everything at second T (REDUCED: the static title card)."""
    t = max(0.0, t)
    if reduced:
        return static_scene(t)
    out = progress(t, "out")
    recede = ease_in_out_cubic(window(t, 6.35, 6.8))
    return {
        "t": t,
        "phase": phase_at(t),
        "reduced": False,
        "enter": ease_out_cubic(window(t, 0.0, 0.3)),  # the intro's background over the app
        "mark": mark_state(t),
        "pan": pan(t),
        "panel": {"open": opening(t), "alpha": ease_out_cubic(window(t, 1.7, 1.95))},
        "agent": {"in": spring(window(t, 2.45, 3.25)) if t >= 2.45 else 0.0, "spin": t * 1.1,
                  "label": ease_out_cubic(window(t, 2.55, 2.95)),
                  "pulse": 0.5 + 0.5 * math.sin(t * 4.2)},
        "code": {"lines": code_lines(t), "alpha": ease_out_cubic(window(t, 2.6, 3.0)),
                 "caret": (t * 2.2) % 1.0 < 0.6},
        "gate": {"in": spring(window(t, 3.7, 4.3)) if t >= 3.7 else 0.0, "flash": gate_flash(t)},
        "routes": ease_in_out_cubic(window(t, 3.75, 4.35)),
        "chips": [ease_out_cubic(window(t, 3.8 + k * 0.08, 4.25 + k * 0.08)) for k in range(len(REQUESTS))],
        "requests": [request_state(i, t) for i in range(len(REQUESTS))],
        "approval": approval_state(t),
        "caption": caption_state(t),
        "recede": recede,  # 1: the scene is gone, the title card stays
        "title": title_state(t),
        "skip": ease_out_cubic(window(t, 0.4, 0.9)) * (1 - out),
        "progress": clamp(t / DURATION),
        "fade": 1 - ease_in_out_cubic(out),  # the whole intro, fading into the app
    }


def static_scene(t):
    """Reduced motion: the finished title card, faded in and out (no movement)."""
    fade = min(window(t, 0.0, STATIC_FADE), 1 - window(t, STATIC_DURATION - STATIC_FADE, STATIC_DURATION))
    mark = {"square": 1.0, "shield": 1.0, "tick": 1.0, "fill": 1.0, "glow": 0.0, "ring": 0.0, "sweep": 0.0,
            "x": MARK_TITLE[0],
            "y": MARK_TITLE[1], "size": MARK_TITLE[2], "header": 0.0}
    return {"t": t, "phase": "title" if t < STATIC_DURATION else "done", "reduced": True, "enter": 1.0,
            "mark": mark, "pan": 0.0, "panel": {"open": 0.0, "alpha": 0.0}, "agent": {"in": 0.0, "spin": 0.0, "pulse": 0.0, "label": 0.0},
            "code": {"lines": 0.0, "alpha": 0.0, "caret": False}, "gate": {"in": 0.0, "flash": []}, "routes": 0.0,
            "chips": [0.0] * len(REQUESTS), "requests": [], "approval": approval_state(0.0), "caption": None,
            "recede": 1.0, "title": {"word": 1.0, "tag": 1.0, "agents": 1.0}, "skip": 1.0,
            "progress": clamp(t / STATIC_DURATION), "fade": fade}


def duration(reduced=False):
    return STATIC_DURATION if reduced else DURATION
