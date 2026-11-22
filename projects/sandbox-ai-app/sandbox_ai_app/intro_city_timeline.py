"""The "Night city" intro's story, as numbers over time (no Qt; intro_city.py draws it).

1. night   dusk falls, a city of hosts rises from the horizon and its windows light up: the internet;
2. dome    on the left a glass dome draws itself over the agent, a decoy key with a canary beside it: the sandbox;
3. trails  the agent's requests rise through the beacon on the dome's top (the proxy) and fly as light trails:
           two reach their towers (allowed), a new host waits at the beacon until it is approved;
4. canary  a script takes the decoy key and runs for the beacon: the tripwire flashes, the canary sings, the leak
           is refused and the run is paused;
5. title   the city recedes: the mark, "sandbox-ai", the tagline and the agents' names (as the classic one);
6. out     everything fades and the app is there.

scene(t) says what is where at second t; scene(t, reduced=True) is the still title card.
"""
from __future__ import annotations

import math
import random

from . import intro_timeline as C
from .intro_timeline import clamp, ease_in_out_cubic, ease_out_cubic, lerp, spring, window

WIDTH, HEIGHT = C.WIDTH, C.HEIGHT
PHASES = (("night", 0.0, 1.0), ("dome", 1.0, 2.45), ("trails", 2.45, 4.6), ("canary", 4.6, 6.6),
          ("title", C.TITLE_START, C.OUT[0]), ("out", *C.OUT))
DURATION = PHASES[-1][2]
STATIC_DURATION = C.STATIC_DURATION
CAPTIONS = ((0, 1.35, 2.6), (1, 2.6, 4.6), (2, 4.6, 6.3))

HORIZON = 612.0  # the ground line
DOME = (330.0, HORIZON, 236.0, 214.0)  # centre x, base y, radius x, radius y
BEACON = (DOME[0], DOME[1] - DOME[3])  # the proxy: the lantern on the dome's top
AGENT = (DOME[0] + 34.0, HORIZON - 84.0, 30.0)  # centre and radius
KEY = (DOME[0] - 118.0, HORIZON - 26.0)  # the decoy key on the dome's floor; the canary sits on it
TRIPWIRE_Y = BEACON[1] + 46.0  # the tripwire across the dome, under the beacon
# The towers: (centre x, width, height, host index or -1); the hosts' towers carry a name and a light.
TOWERS = ((640, 58, 168, -1), (712, 74, 252, -1), (808, 92, 304, 0), (902, 62, 196, -1), (978, 80, 270, -1),
          (1046, 96, 236, 1), (1130, 56, 180, -1), (1196, 70, 296, -1), (1282, 98, 338, 2), (1370, 64, 214, -1),
          (1478, 104, 266, 3), (1556, 52, 182, -1))
# The hosts: (name, what happens). "ask" waits for the approval; "canary" is where the decoy key is sent.
HOSTS = (("api.anthropic.com", "allow"), ("pypi.org", "allow"), ("github.com", "ask"), ("drop.example.net", "canary"))
REQUESTS = ((0, 2.55), (1, 2.85), (2, 3.15))  # (host, when it leaves the agent)
UP, ACROSS = 0.38, 0.62  # seconds: from the agent up to the beacon, from the beacon to the tower
ASK = (0.12, 0.5, 0.12)  # at the beacon: the card appears, Allow is pressed, it goes on
CARD = (96.0, 236.0, 330.0, 86.0)  # the approval question, above the beacon (its pointer down to it)
SCRIPT = (4.55, 4.75, 5.1, 5.18, 5.55)  # the script: appears, walks to the key, takes it, runs, hits the wire
SCRIPT_FROM = (DOME[0] + 120.0, HORIZON - 40.0)
PAUSE = 5.75  # the run is paused


def tower_top(index):
    x, _w, height, _host = TOWERS[index]
    return x, HORIZON - height


def host_tower(host):
    return next(k for k, tower in enumerate(TOWERS) if tower[3] == host)


def _windows():
    """Each tower's lit windows: (column, row, warmth 0..1, brightness 0..1, when it lights)."""
    rng = random.Random(5)
    found = []
    for k, (_x, width, height, _host) in enumerate(TOWERS):
        columns, rows = max(2, int((width - 14) // 14)), int((height - 24) // 18)
        lit = []
        for row in range(rows):
            for column in range(columns):
                if rng.random() < 0.42:
                    lit.append((column, row, rng.random(), 0.45 + 0.55 * rng.random(),
                                0.55 + 0.9 * rng.random() + k * 0.02))
        found.append((columns, rows, tuple(lit)))
    return tuple(found)


WINDOWS = _windows()


def rise(index, t):
    """How far tower INDEX has risen from below the horizon (0..1, a soft spring)."""
    start = 0.12 + 0.065 * ((index * 5) % len(TOWERS))
    return spring(window(t, start, start + 0.85), 7.5, 1.3) if t >= start else 0.0


def trail(host, t, leave):
    """A request from the agent to tower HOST, leaving at LEAVE: u 0..1 up to the beacon, 1..2 across to the
    tower; its state and the badge of the tower."""
    verdict = HOSTS[host][1]
    at_beacon = leave + UP
    u = ease_in_out_cubic(window(t, leave, at_beacon))
    state, wait = "travel", 0.0
    go = at_beacon + (sum(ASK) if verdict == "ask" else 0.0)
    if t >= at_beacon:
        if t < go:
            state, wait = "waiting", t - at_beacon
        u = 1 + ease_in_out_cubic(window(t, go, go + ACROSS))
        if t >= go + ACROSS:
            state = "arrived"
    arrive = go + ACROSS
    return {"host": host, "verdict": verdict, "u": u, "state": state, "wait": wait, "leave": leave,
            "alpha": 1.0 if t >= leave else 0.0, "at_beacon": at_beacon,
            "badge": ease_out_cubic(window(t, arrive, arrive + 0.35)) if t >= arrive else 0.0,
            "lit": ease_out_cubic(window(t, arrive, arrive + 0.6)) if t >= arrive else 0.0}


def approval_state(t):
    leave = next(when for host, when in REQUESTS if HOSTS[host][1] == "ask")
    at_beacon = leave + UP
    shown, press, go = at_beacon + ASK[0], at_beacon + ASK[0] + ASK[1], at_beacon + sum(ASK)
    return {"in": spring(window(t, shown, shown + 0.5), 6.0, 1.6) if t >= shown else 0.0,
            "press": math.sin(math.pi * window(t, press - 0.18, press + 0.06)),
            "done": ease_out_cubic(window(t, press, press + 0.2)),
            "out": ease_in_out_cubic(window(t, go + 0.2, go + 0.55))}


def script_state(t):
    """The script that takes the decoy key: where it is, whether it carries the key, when it is stopped."""
    appear, walk, take, run, wire = SCRIPT
    shown = ease_out_cubic(window(t, appear, appear + 0.25))
    x, y = SCRIPT_FROM
    if t >= walk:
        f = ease_in_out_cubic(window(t, walk, take))
        x, y = lerp(SCRIPT_FROM[0], KEY[0] + 18, f), lerp(SCRIPT_FROM[1], KEY[1] - 6, f)
    if t >= run:
        f = ease_in_out_cubic(window(t, run, wire))
        x, y = lerp(KEY[0] + 18, BEACON[0] - 30, f), lerp(KEY[1] - 6, TRIPWIRE_Y, f)
    caught = t >= wire
    alpha = shown * (1 - ease_out_cubic(window(t, wire + 0.15, wire + 0.55)))
    return {"x": x, "y": y, "alpha": alpha, "carry": t >= take, "caught": caught,
            "burst": window(t, wire, wire + 0.7) if caught else 0.0, "bob": math.sin(t * 22) * (t < wire)}


def canary_state(t):
    """The canary: sitting calmly on the key, singing (alarm rings) once the key is taken."""
    take, wire = SCRIPT[2], SCRIPT[4]
    alarm = clamp((t - take) / 0.2) if t >= take else 0.0
    return {"in": spring(window(t, 1.65, 2.2), 6.0, 1.6) if t >= 1.65 else 0.0, "alarm": alarm,
            "song": ((t - take) * 1.6) % 1.0 if t >= take else 0.0, "flap": math.sin(t * 30) * alarm *
            (1 - window(t, wire + 0.4, wire + 0.9)), "key": 0.0 if t >= take else 1.0}


def scene(t, reduced=False):
    """Everything at second T (REDUCED: the still title card)."""
    t = max(0.0, t)
    if reduced:
        return dict(C.shared_static(t), story=None)
    wire = SCRIPT[4]
    s = C.shared(t, PHASES, CAPTIONS, DURATION)
    s["story"] = {
        "sky": ease_out_cubic(window(t, 0.0, 0.8)),
        "towers": [rise(k, t) for k in range(len(TOWERS))],
        "windows": [clamp((t - 0.5) / 1.2) for _ in TOWERS],
        "labels": [ease_out_cubic(window(t, 1.9 + k * 0.08, 2.4 + k * 0.08)) for k in range(len(HOSTS))],
        "dome": {"draw": ease_in_out_cubic(window(t, 0.95, 1.65)), "fill": ease_out_cubic(window(t, 1.3, 1.9)),
                 "paused": ease_out_cubic(window(t, PAUSE, PAUSE + 0.4))},
        "beacon": {"in": spring(window(t, 1.55, 2.15), 6.0, 1.5) if t >= 1.55 else 0.0,
                   "spin": t * 1.6, "pulse": 0.5 + 0.5 * math.sin(t * 5.0)},
        "agent": {"in": spring(window(t, 1.35, 2.0)) if t >= 1.35 else 0.0, "spin": t * 1.3,
                  "pulse": 0.5 + 0.5 * math.sin(t * 4.2)},
        "trails": [trail(host, t, leave) for host, leave in REQUESTS],
        "approval": approval_state(t),
        "script": script_state(t),
        "canary": canary_state(t),
        "wire": {"in": ease_out_cubic(window(t, 2.0, 2.5)),
                 "flash": (1 - ease_out_cubic(window(t, wire, wire + 0.9))) if t >= wire else 0.0},
        "refused": ease_out_cubic(window(t, wire + 0.1, wire + 0.45)) if t >= wire else 0.0,
        "twinkle": t,
    }
    return s


def duration(reduced=False):
    return STATIC_DURATION if reduced else DURATION
