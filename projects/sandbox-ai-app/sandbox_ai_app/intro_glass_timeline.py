"""The "Glass cube" intro's story, as numbers over time (no Qt; intro_glass.py draws it).

1. cube    a glass cube draws itself, edge by edge, and turns slowly: the sandbox;
2. grow    inside it a network of light grows from a seed - nodes and links, signals running along them: the AI;
3. walls   tendrils reach the walls: each touch ripples over the glass, and the wall holds;
4. port    one tendril finds the port in the right wall - the proxy. Two signals pass it to their hosts (on the
           allow-list), a third, to a tracker, flashes red at the port and bounces back;
5. title   the cube shrinks into the mark, "sandbox-ai", the tagline and the agents' names (as the classic one);
6. out     everything fades and the app is there.

The cube is a 3D model (its corners at -1..1 on each axis, the network inside it) turned and projected on the
stage (project()). scene(t) says what is where at second t; scene(t, reduced=True) is the still title card.
"""
from __future__ import annotations

import math
import random

from . import intro_timeline as C
from .intro_timeline import clamp, ease_in_out_cubic, ease_out_cubic, lerp, spring, window

WIDTH, HEIGHT = C.WIDTH, C.HEIGHT
PHASES = (("cube", 0.0, 1.3), ("grow", 1.3, 3.15), ("walls", 3.15, 3.95), ("port", 3.95, 6.6),
          ("title", C.TITLE_START, C.OUT[0]), ("out", *C.OUT))
DURATION = PHASES[-1][2]
STATIC_DURATION = C.STATIC_DURATION
CAPTIONS = ((0, 1.45, 3.2), (1, 3.2, 4.45), (2, 4.45, 6.3))

CENTRE = (690.0, 392.0)  # the cube's centre on the stage
SIZE = 168.0  # half its edge, in stage units
PITCH = 0.42  # seen from a little above
DISTANCE = 2400.0  # the camera's distance (a gentle perspective)

# The faces: (name, centre/normal, axis u, axis v); a point on a face is normal + u * axis u + v * axis v.
FACES = (("+x", (1, 0, 0), (0, 0, -1), (0, 1, 0)), ("-x", (-1, 0, 0), (0, 0, 1), (0, 1, 0)),
         ("+y", (0, 1, 0), (1, 0, 0), (0, 0, -1)), ("-y", (0, -1, 0), (1, 0, 0), (0, 0, 1)),
         ("+z", (0, 0, 1), (1, 0, 0), (0, 1, 0)), ("-z", (0, 0, -1), (-1, 0, 0), (0, 1, 0)))
CORNERS = tuple((x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1))
EDGES = tuple((a, b) for a in range(8) for b in range(a + 1, 8)
              if sum(p != q for p, q in zip(CORNERS[a], CORNERS[b])) == 1)
EDGE_DRAW = (0.1, 1.15)  # the edges draw themselves one after the other, in this window

PORT = ("+x", -0.18, 0.02)  # the proxy: a round port in the right wall (face, u, v)
PORT_RADIUS = 0.2
# Tendrils that touch a wall (face, u, v, when it touches): the wall ripples and holds.
TOUCHES = (("+y", -0.4, 0.32, 3.3), ("+z", -0.5, -0.38, 3.62), ("-x", 0.2, -0.45, 3.86))
PORT_REACH = 3.95  # the tendril to the port arrives
# The signals: (host, what the proxy does, when it leaves the seed); they run along the network to the port.
SIGNALS = (("api.anthropic.com", "allow", 4.0), ("pypi.org", "allow", 4.35), ("tracker.example.net", "block", 4.75))
TO_PORT, TO_HOST = 0.55, 0.42  # seconds: from the seed to the port, from the port to the host
CHIP = (1130.0, 320.0, 58.0)  # x, width, height of a host
CHIP_Y = (262.0, 372.0, 482.0)

GROW = (1.3, 3.3)  # the network grows in this window
NODE_COUNT = 46
LINK = 0.32  # seconds a link takes to grow to its node


def _network():
    """The network's nodes (3D, inside the cube), each one's parent (the node it grew from) and when it is
    there, and a few cross links; the same every time (a fixed seed)."""
    rng = random.Random(23)
    nodes, parents = [(0.0, 0.05, 0.0)], [None]
    while len(nodes) < NODE_COUNT:
        point = tuple(rng.uniform(-0.74, 0.74) for _ in range(3))
        distances = [math.dist(point, node) for node in nodes]
        nearest = min(range(len(nodes)), key=distances.__getitem__)
        if 0.2 <= distances[nearest] <= 0.42:
            nodes.append(point)
            parents.append(nearest)
    start, end = GROW
    births = [start + (end - start - LINK) * (k / (NODE_COUNT - 1)) ** 0.85 for k in range(NODE_COUNT)]
    births[0] = start
    links = []
    for k in range(NODE_COUNT):
        for j in range(k):
            if parents[k] != j and parents[j] != k and math.dist(nodes[k], nodes[j]) < 0.33 and len(links) < 16 \
                    and rng.random() < 0.55:
                links.append((j, k))
    return nodes, parents, births, links


NODES, PARENTS, BIRTHS, LINKS = _network()


def on_face(face, u, v, inset=0.0):
    """The 3D point at U, V on FACE (INSET: that far inside the cube)."""
    _, normal, a, b = next(f for f in FACES if f[0] == face)
    return tuple(n * (1 - inset) + u * x + v * y for n, x, y in zip(normal, a, b))


def _tendrils():
    """The tendrils: (end point, the node it grows from, when it touches, face, u, v); the last one is the
    port's."""
    found = []
    for face, u, v, when in TOUCHES + ((PORT[0], PORT[1], PORT[2], PORT_REACH),):
        end = on_face(face, u, v, 0.02)
        grown = [k for k in range(NODE_COUNT) if BIRTHS[k] < when - 0.5]
        found.append((end, min(grown, key=lambda k: math.dist(NODES[k], end)), when, face, u, v))
    return tuple(found)


TENDRILS = _tendrils()


def _path_to_port():
    """The signals' way: from the seed through the network to the port's tendril, and through the port."""
    k, chain = TENDRILS[-1][1], []
    while k is not None:
        chain.append(NODES[k])
        k = PARENTS[k]
    return tuple(reversed(chain)) + (TENDRILS[-1][0], on_face(PORT[0], PORT[1], PORT[2], -0.04))


PATH = _path_to_port()


def along(points, f):
    """The point F (0..1) of the way along the polyline POINTS (3D)."""
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    total = sum(lengths) or 1.0
    walk = clamp(f) * total
    for (a, b), length in zip(zip(points, points[1:]), lengths):
        if walk <= length:
            g = walk / length if length else 0.0
            return tuple(lerp(x, y, g) for x, y in zip(a, b))
        walk -= length
    return points[-1]


# -- the parts --

def yaw(t):
    """The cube's turn about its upright axis (radians): slowly all the way, faster as it becomes the mark."""
    return -0.86 + 0.34 * ease_in_out_cubic(window(t, 0.0, 6.2)) + 0.9 * ease_in_out_cubic(window(t, 5.95, 6.75))


def cube_state(t):
    into_mark = ease_in_out_cubic(window(t, 5.95, 6.7))
    cx = lerp(CENTRE[0], C.MARK_TITLE[0], into_mark)
    cy = lerp(CENTRE[1], C.MARK_TITLE[1], into_mark)
    draw = [ease_in_out_cubic(window(t, EDGE_DRAW[0] + k * 0.055, EDGE_DRAW[0] + k * 0.055 + 0.42))
            for k in range(len(EDGES))]
    return {"yaw": yaw(t), "pitch": PITCH, "cx": cx, "cy": cy,
            "size": SIZE * lerp(spring(window(t, 0.0, 1.1), 6.0, 1.2) * 0.08 + 0.92, 0.34, into_mark),
            "edges": draw, "glass": ease_out_cubic(window(t, 0.7, 1.5)), "gleam": window(t, 1.0, 2.2),
            "into_mark": into_mark}


def project(point, cube):
    """3D POINT (cube units) on the stage: (x, y, depth); a larger depth is nearer."""
    x, y, z = point
    a, b = cube["yaw"], cube["pitch"]
    x1 = x * math.cos(a) + z * math.sin(a)
    z1 = -x * math.sin(a) + z * math.cos(a)
    y2 = y * math.cos(b) - z1 * math.sin(b)
    z2 = y * math.sin(b) + z1 * math.cos(b)
    f = DISTANCE / (DISTANCE - z2 * cube["size"])
    return cube["cx"] + x1 * cube["size"] * f, cube["cy"] - y2 * cube["size"] * f, z2


def facing(face, cube):
    """How much FACE turns to the viewer (-1..1; > 0: its outside is seen)."""
    normal = next(f[1] for f in FACES if f[0] == face)
    return project(normal, dict(cube, size=1e-6, cx=0.0, cy=0.0))[2]


def node_states(t):
    """Each node's growth 0..1 (a spring: it pops in once its link has reached it)."""
    return [spring(window(t, born, born + 0.45), 7.0, 1.8) if t >= born else 0.0 for born in BIRTHS]


def link_states(t):
    """Each tree link's growth (to node k from its parent) and the cross links' (fading in once both are there)."""
    tree = [ease_out_cubic(window(t, born - LINK, born)) if k else 0.0 for k, born in enumerate(BIRTHS)]
    cross = [ease_out_cubic(window(t, max(BIRTHS[a], BIRTHS[b]) + 0.15, max(BIRTHS[a], BIRTHS[b]) + 0.6))
             for a, b in LINKS]
    return tree, cross


def pulses(t):
    """Signals of thought running along the tree links: (node k, fraction from its parent, strength)."""
    found = []
    if t < GROW[0] + 0.4:
        return found
    for k in range(1, NODE_COUNT):
        speed, offset = 0.9 + (k * 37 % 11) / 11, (k * 0.61803) % 1.0
        phase = (t * speed * 0.55 + offset) % 1.0
        if phase < 0.42 and t > BIRTHS[k] + 0.2:
            f = phase / 0.42
            found.append((k, f, math.sin(math.pi * f) * clamp((t - BIRTHS[k] - 0.2) / 0.4)))
    return found


def tendril_states(t):
    """Each tendril: its growth to the wall 0..1, the ripple on the wall after the touch (0..1, or None), and how
    far it draws back from the wall (0..1)."""
    states = []
    for end, node, when, face, u, v in TENDRILS:
        grow = ease_in_out_cubic(window(t, when - 0.5, when))
        since = t - when
        ripple = clamp(since / 1.25) if 0 <= since < 1.25 else None
        port = face == PORT[0] and (u, v) == PORT[1:]
        back = 0.0 if port else 0.12 * math.sin(math.pi * window(t, when, when + 0.5))
        states.append({"grow": grow, "ripple": ripple, "back": back, "face": face, "u": u, "v": v,
                       "node": node, "end": end, "port": port})
    return states


def signal_state(index, t):
    """Signal INDEX: where it is on its way (0..1 to the port, then 1..2 to its host, or back for a blocked one),
    its outcome and the badge of its host."""
    host, verdict, leave = SIGNALS[index]
    at_port = leave + TO_PORT
    u = ease_in_out_cubic(window(t, leave, at_port))
    state, alpha, back = "travel", 1.0 if t >= leave else 0.0, 0.0
    if t >= at_port:
        if verdict == "block":
            state = "blocked"
            back = ease_out_cubic(window(t, at_port, at_port + 0.55))
            alpha = 1 - ease_in_out_cubic(window(t, at_port + 0.3, at_port + 0.75))
        else:
            u = 1 + ease_in_out_cubic(window(t, at_port, at_port + TO_HOST))
            if t >= at_port + TO_HOST:
                state = "arrived"
                alpha = 1 - ease_out_cubic(window(t, at_port + TO_HOST, at_port + TO_HOST + 0.25))
    decided = at_port if verdict == "block" else at_port + TO_HOST
    return {"host": host, "verdict": verdict, "u": u, "back": back, "state": state, "alpha": alpha,
            "at_port": at_port, "badge": ease_out_cubic(window(t, decided, decided + 0.3)) if t >= decided else 0.0}


def port_state(t):
    """The port: there once its tendril arrives; it flashes ok or bad when a signal passes or is stopped."""
    flash, kind = 0.0, "ok"
    for host, verdict, leave in SIGNALS:
        since = t - (leave + TO_PORT)
        if 0 <= since < 0.6:
            value = 1 - ease_out_cubic(since / 0.6)
            if value > flash:
                flash, kind = value, "bad" if verdict == "block" else "ok"
    return {"in": ease_out_cubic(window(t, PORT_REACH - 0.35, PORT_REACH + 0.2)), "flash": flash, "kind": kind,
            "reach": spring(window(t, PORT_REACH, PORT_REACH + 0.6), 6.0, 1.5) if t >= PORT_REACH else 0.0}


def scene(t, reduced=False):
    """Everything at second T (REDUCED: the still title card)."""
    t = max(0.0, t)
    if reduced:
        return dict(C.shared_static(t), story=None)
    tree, cross = link_states(t)
    s = C.shared(t, PHASES, CAPTIONS, DURATION)
    s["story"] = {
        "cube": cube_state(t), "nodes": node_states(t), "tree": tree, "cross": cross, "pulses": pulses(t),
        "tendrils": tendril_states(t), "port": port_state(t),
        "signals": [signal_state(k, t) for k in range(len(SIGNALS))],
        "chips": [ease_out_cubic(window(t, 3.75 + k * 0.1, 4.2 + k * 0.1)) for k in range(len(SIGNALS))],
        "seed": spring(window(t, 1.15, 1.75), 6.0, 1.6) if t >= 1.15 else 0.0,
        "breath": 0.5 + 0.5 * math.sin(t * 3.4),
    }
    return s


def duration(reduced=False):
    return STATIC_DURATION if reduced else DURATION
