"""The "Night city" intro (intro_city_timeline.py tells the story; this draws it, on intro.py's machinery).

A skyline of hosts at dusk (a far one in the cached background, the near towers rising), a glass dome on the left
with the agent, a decoy key and its canary inside, a beacon on its top - the proxy - and the requests flying as
light trails. Each tower is painted once into pixmaps (its body, its windows) and moved while it rises; once the
city stands, the whole skyline is one pixmap.
"""
from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainterPath, QPen

from . import intro_city_timeline as Y, intro_timeline as T, theme
from .i18n import tr
from .intro import Layout, Renderer, faded, glow, grain, mix, partial, stroke, vignette


def captions():
    return (tr("Your AI agent works in its sandbox. Outside: the internet."),
            tr("Its requests reach only the hosts you allow; a new one asks you first."),
            tr("A decoy key is taken? The canary sings: the leak is refused, the run paused."))


def dome_path():
    cx, base, rx, ry = Y.DOME
    path = QPainterPath(QPointF(cx - rx, base))
    path.arcTo(QRectF(cx - rx, base - ry, 2 * rx, 2 * ry), 180, -180)
    return path


def dome_x(y):
    """Half the dome's width at height Y."""
    cx, base, rx, ry = Y.DOME
    return rx * math.sqrt(max(0.0, 1 - ((base - y) / ry) ** 2))


class CityLayout(Layout):
    """The city's texts: the hosts over their towers, the beacon's and the key's names, the approval card."""

    def captions(self):
        return captions()

    def build_story(self):
        semi, medium = QFont.Weight.DemiBold, QFont.Weight.Medium
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for k, (host, _) in enumerate(Y.HOSTS):
            x, top = Y.tower_top(Y.host_tower(k))
            self.add(f"host{k}", host, 16, self.centred(x + 12, top - 50, 186, 24), mono=True, least=9)
        bx, by = Y.BEACON
        self.add("proxy", tr("Proxy"), 18, self.centred(bx - 104, by - 18, 130, 26), weight=medium, least=10)
        ax, ay, ar = Y.AGENT
        self.add("agent", tr("Agent"), 17, self.centred(ax, ay + ar + 26, 150, 24), weight=medium, least=10)
        self.add("paused", tr("Run paused"), 17, self.centred(ax, ay + ar + 26, 170, 24), weight=semi, least=10)
        kx, ky = Y.KEY
        self.add("key", tr("Decoy key"), 16, self.centred(kx, Y.HORIZON + 30, 190, 24), weight=medium, least=10)
        x, y, w, h = Y.CARD
        self.add("ask", tr("Allow {host}?", host=Y.HOSTS[2][0]), 19, self.rect(x + 62, y + 12, w - 78, 28), left,
                 weight=semi, least=10)
        self.add("deny", tr("Deny"), 15, self.rect(*self.button(0)), weight=medium, least=9)
        self.add("allow", tr("Allow"), 15, self.rect(*self.button(1)), weight=semi, least=9)

    @staticmethod
    def button(which):
        x, y, w, h = Y.CARD
        return (x + w - 14 - 96 - (1 - which) * 106, y + h - 14 - 30, 96, 30)

    GROUPS = (("host0", "host1", "host2", "host3", "proxy", "agent", "key", "ask", "deny", "allow", "caption0",
               "skip", "esc"),
              ("host0", "host1", "host2", "host3", "proxy", "paused", "key", "caption2", "skip", "esc"),
              ("word", "tag", "agents", "skip", "esc"))


class CityRenderer(Renderer):
    TIMELINE = Y
    LAYOUT = CityLayout

    # -- the still parts --

    def ground(self):
        top, haze, _ground = self.sky()
        return mix(top, haze, 0.6)

    def sky(self):
        """(top, horizon, ground) colours: a night sky in a dark theme, dusk in a light one."""
        pal = self.pal
        if pal.dark:
            return (mix(pal.bg, QColor("#000000"), 0.45), mix(pal.bg, pal.accent, 0.22),
                    mix(pal.bg, QColor("#000000"), 0.5))
        return (mix(pal.bg, pal.accent, 0.16), mix(pal.bg, pal.warn, 0.14), mix(pal.bg, pal.fg, 0.07))

    def tower_colour(self, far=False):
        pal = self.pal
        if pal.dark:
            return mix(pal.bg, QColor("#000000"), 0.15 if far else 0.32) if not far else mix(pal.bg, pal.accent, 0.1)
        return mix(pal.bg, pal.fg, 0.12 if far else 0.3)

    def paint_background(self, p, width, height):
        pal, L = self.pal, self.layout
        top, haze, ground = self.sky()
        horizon = L.oy + Y.HORIZON * L.s
        gradient = QLinearGradient(0, 0, 0, horizon)
        gradient.setColorAt(0, top)
        gradient.setColorAt(0.75, mix(top, haze, 0.55))
        gradient.setColorAt(1, haze)
        p.fillRect(QRectF(0, 0, width, horizon), gradient)
        glow(p, QPointF(L.ox + 1100 * L.s, horizon), 700 * L.s, pal.accent if pal.dark else pal.warn,
             0.16 if pal.dark else 0.12)
        if pal.dark:  # stars, more of them high up
            rng = random.Random(3)
            p.setPen(Qt.PenStyle.NoPen)
            for _ in range(int(160 * max(1.0, width / 1600))):
                x, y = rng.random() * width, rng.random() ** 1.6 * horizon * 0.82
                p.setBrush(faded(pal.fg, 0.15 + 0.45 * rng.random()))
                r = max(0.6, (0.5 + rng.random() * 1.1) * L.s)
                p.drawEllipse(QPointF(x, y), r, r)
        self.paint_far_city(p, L, horizon)
        fill = QLinearGradient(0, horizon, 0, height)
        fill.setColorAt(0, ground)
        fill.setColorAt(1, mix(ground, QColor("#000000"), 0.3) if pal.dark else ground)
        p.fillRect(QRectF(0, horizon, width, height - horizon), fill)
        p.setPen(QPen(mix(haze, pal.fg, 0.12), max(1.0, L.s)))
        p.drawLine(QPointF(0, horizon), QPointF(width, horizon))
        p.fillRect(QRectF(0, 0, width, height), vignette(width, height, pal, 0.5, 110, 16))
        p.setOpacity(0.022 if pal.dark else 0.018)
        p.drawTiledPixmap(QRectF(0, 0, width, height), grain())
        p.setOpacity(1.0)

    def paint_far_city(self, p, L, horizon):
        """A far skyline, pale, across the whole width."""
        rng = random.Random(9)
        colour = self.tower_colour(far=True)
        p.setPen(Qt.PenStyle.NoPen)
        x = -40.0
        while x < L.width / L.s - L.ox / L.s + 40:
            w, h = 30 + rng.random() * 60, 50 + rng.random() * 150 * (0.4 + 0.6 * math.sin(x / 500) ** 2)
            p.setBrush(colour)
            p.drawRect(QRectF(L.ox + x * L.s, horizon - h * L.s, w * L.s + 1, h * L.s))
            x += w + rng.random() * 8

    def tower_body(self, q, index):
        """Tower INDEX (in tower units: x from 0, its foot at the bottom), without its windows."""
        pal = self.pal
        _x, w, h, host = Y.TOWERS[index]
        body = QLinearGradient(0, 0, w, 0)
        colour = self.tower_colour()
        body.setColorAt(0, mix(colour, pal.fg, 0.05 if pal.dark else 0.0))
        body.setColorAt(1, mix(colour, QColor("#000000"), 0.12) if pal.dark else mix(colour, pal.bg, 0.12))
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(body)
        path = QPainterPath()
        top = 30.0
        style = index % 3
        if style == 0:  # a stepped crown
            path.addRect(QRectF(0, top + 16, w, h - 16))
            path.addRect(QRectF(w * 0.18, top, w * 0.64, 18))
        elif style == 1:  # a slanted roof
            path.moveTo(0, top + 22)
            path.lineTo(w, top + 6)
            path.lineTo(w, top + h)
            path.lineTo(0, top + h)
            path.closeSubpath()
        else:
            path.addRect(QRectF(0, top + 4, w, h - 4))
        q.drawPath(path)
        if host >= 0 or style == 0:  # an antenna
            q.setPen(stroke(colour, 2.2))
            q.drawLine(QPointF(w / 2, top + 2), QPointF(w / 2, top - 22))
        q.setPen(QPen(mix(colour, pal.fg, 0.14), 1.0))
        q.drawLine(QPointF(0.5, top + 20), QPointF(0.5, top + h))

    def tower_windows(self, q, index):
        pal = self.pal
        _x, w, h, _host = Y.TOWERS[index]
        columns, rows, lit = Y.WINDOWS[index]
        cool = mix(pal.accent, QColor("#ffffff"), 0.35) if pal.dark else mix(pal.accent, pal.bg, 0.35)
        warm = mix(pal.warn, QColor("#ffffff"), 0.25) if pal.dark else mix(pal.warn, pal.bg, 0.2)
        step_x = (w - 12) / columns
        q.setPen(Qt.PenStyle.NoPen)
        for column, row, warmth, bright, _when in lit:
            q.setBrush(faded(mix(cool, warm, warmth), (0.35 + 0.6 * bright) * (1.0 if pal.dark else 0.75)))
            q.drawRect(QRectF(6 + column * step_x + 2, 30 + 28 + row * 18, step_x - 5, 8))

    def paint_tower(self, p, index, risen, windows):
        x, w, h, _host = Y.TOWERS[index]
        area = QRectF(x - w / 2, Y.HORIZON - h - 30 + (1 - risen) * (h + 40), w, h + 30)

        def body(q):
            self.tower_body(q, index)

        def lights(q):
            self.tower_windows(q, index)
        size = area.size()
        target = QRectF(area.topLeft(), size)
        self.blit(p, target, self.sprite(("tower", index), size, body))
        if windows > 0:
            p.save()
            p.setOpacity(p.opacity() * windows)
            self.blit(p, target, self.sprite(("windows", index), size, lights))
            p.restore()

    def paint_city(self, p, st):
        """The near towers: rising from the horizon, then still (one pixmap for all of them)."""
        if all(r >= 1 for r in st["towers"]) and all(w >= 1 for w in st["windows"]):
            area = QRectF(560, Y.HORIZON - 380, 1060, 384)

            def city(q):
                for k in range(len(Y.TOWERS)):
                    self.paint_tower(q, k, 1.0, 1.0)
            self.cached(p, ("city",), area, city)
            return
        p.save()
        p.setClipRect(QRectF(0, 0, Y.WIDTH + 200, Y.HORIZON))
        for k in range(len(Y.TOWERS)):
            if st["towers"][k] > 0:
                self.paint_tower(p, k, st["towers"][k], st["windows"][k])
        p.restore()

    # -- the story --

    def paint_story(self, p, L, s):
        st = s["story"]
        if st is None:
            return
        self.stage(p, L)
        self.paint_twinkles(p, st)
        self.paint_city(p, st)
        self.paint_hosts(p, L, st)
        self.paint_beams(p, st)
        self.paint_dome(p, L, st)
        self.paint_trails(p, st)
        self.paint_beacon(p, L, st)
        self.paint_card(p, L, st)
        p.restore()
        self.paint_caption(p, L, s)

    def paint_twinkles(self, p, st):
        if not self.pal.dark:
            return
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(12):
            x, y = 90 + (k * 389) % 1420, 40 + (k * 157) % 300
            a = 0.5 + 0.5 * math.sin(st["twinkle"] * (1.3 + k * 0.21) + k)
            self.glow(p, QPointF(x, y), 7, self.pal.fg, 0.35 * a * st["sky"])

    def host_light(self, index, st):
        """(colour, strength) of host INDEX's light on its antenna."""
        pal = self.pal
        for request in st["trails"]:
            if request["host"] == index:
                if request["state"] == "waiting":
                    return pal.warn, 0.6 + 0.4 * math.sin(request["wait"] * 9)
                if request["lit"] > 0:
                    return pal.ok, request["lit"]
        if Y.HOSTS[index][1] == "canary" and st["refused"] > 0:
            return pal.bad, st["refused"]
        return pal.muted, 0.35

    def paint_hosts(self, p, L, st):
        """Each host's tower: its name above it, a light on its antenna, its badge once decided."""
        pal = self.pal
        for k in range(len(Y.HOSTS)):
            shown = st["labels"][k]
            tower = Y.host_tower(k)
            if st["towers"][tower] <= 0:
                continue
            x, top = Y.tower_top(tower)
            top += (1 - st["towers"][tower]) * (Y.TOWERS[tower][2] + 40)
            colour, strength = self.host_light(k, st)
            tip = QPointF(x, top - 22)
            self.glow(p, tip, 24 + 18 * strength, colour, 0.25 + 0.6 * strength)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(mix(colour, QColor("#ffffff"), 0.3) if strength > 0.5 else colour)
            p.drawEllipse(tip, 3.6, 3.6)
            if shown <= 0:
                continue
            request = next((r for r in st["trails"] if r["host"] == k), None)
            p.save()
            p.setOpacity(p.opacity() * shown)
            text = L.texts[f"host{k}"]
            rect = QRectF((text.rect.left() - L.ox) / L.s - 30, (text.rect.top() - L.oy) / L.s - 6,
                          text.rect.width() / L.s + 42, text.rect.height() / L.s + 12)
            p.setBrush(faded(pal.surface, 0.82))
            p.setPen(QPen(mix(pal.hairline, colour, 0.5 * strength), 1.1))
            p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            self.texts(p, L, ((f"host{k}", pal.fg),))
            badge = request["badge"] if request else st["refused"] if Y.HOSTS[k][1] == "canary" else 0.0
            dot = QPointF(rect.left() + 15, rect.center().y())
            if badge > 0:
                bad = Y.HOSTS[k][1] == "canary"
                fill = pal.bad if bad else pal.ok
                radius = 9 * T.spring(badge, 6, 1.5)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(fill)
                p.drawEllipse(dot, radius, radius)
                ink = pal.on_accent if theme.luminance(fill) < 0.5 else pal.bg
                if bad:
                    self.paint_lock(p, dot, radius / 13 * 0.8, ink)
                else:
                    tick = QPainterPath(dot + QPointF(-4, 0.2))
                    tick.lineTo(dot + QPointF(-1.2, 3))
                    tick.lineTo(dot + QPointF(4.2, -3))
                    p.setPen(stroke(ink, 2.0))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawPath(partial(tick, T.clamp(badge * 1.6), 12))
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(pal.line)
                p.drawEllipse(dot, 3.5, 3.5)
            p.restore()

    def paint_beams(self, p, st):
        """The beacon's two soft beams sweeping the sky, like a lighthouse's."""
        pal = self.pal
        strength = T.clamp(st["beacon"]["in"]) * (1 - st["dome"]["paused"] * 0.6)
        if strength <= 0.01:
            return
        bx, by = Y.BEACON
        p.save()
        p.setClipRect(QRectF(0, 0, Y.WIDTH, Y.HORIZON))
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(2):
            angle = st["beacon"]["spin"] * 0.55 + k * math.pi
            direction = math.cos(angle)
            if direction < 0.05:
                continue
            reach = 900 * direction
            spread = 0.07
            a, b = -math.pi / 2 + 0.9 * math.sin(angle) - spread, -math.pi / 2 + 0.9 * math.sin(angle) + spread
            beam = QPainterPath(QPointF(bx, by - 8))
            beam.lineTo(bx + math.cos(a) * reach, by + math.sin(a) * reach)
            beam.lineTo(bx + math.cos(b) * reach, by + math.sin(b) * reach)
            beam.closeSubpath()
            mid = -math.pi / 2 + 0.9 * math.sin(angle)
            light = QLinearGradient(QPointF(bx, by), QPointF(bx + math.cos(mid) * reach, by + math.sin(mid) * reach))
            light.setColorAt(0, faded(pal.accent, (0.22 if pal.dark else 0.16) * strength * direction))
            light.setColorAt(1, faded(pal.accent, 0))
            p.setBrush(light)
            p.drawPath(beam)
        p.restore()

    def paint_dome(self, p, L, st):
        """The dome: glass over the agent, the key and the canary, the tripwire under the beacon."""
        pal = self.pal
        dome = st["dome"]
        if dome["draw"] <= 0:
            return
        cx, base, rx, ry = Y.DOME
        path = dome_path()
        edge = mix(pal.accent, pal.warn, dome["paused"])
        if dome["fill"] > 0:
            filled = QPainterPath(path)
            filled.closeSubpath()
            glass = QLinearGradient(0, base - ry, 0, base)
            glass.setColorAt(0, faded(mix(edge, QColor("#ffffff"), 0.3), (0.16 if pal.dark else 0.2) * dome["fill"]))
            glass.setColorAt(1, faded(edge, (0.07 if pal.dark else 0.08) * dome["fill"]))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glass)
            p.drawPath(filled)
            self.glow(p, QPointF(cx, base - 40), rx * 0.9, edge, pal.glow * 0.8 * dome["fill"])
            self.paint_wire(p, st)
            self.paint_key(p, L, st)
            self.paint_agent(p, L, st)
            self.paint_script(p, st)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(stroke(self.solid(edge, 0.9), 2.4))
        p.drawPath(partial(path, dome["draw"], 120))
        if dome["draw"] >= 1:  # a highlight along the glass
            shine = QPainterPath()
            shine.arcMoveTo(QRectF(cx - rx + 18, base - ry + 18, 2 * rx - 36, 2 * ry - 36), 150)
            shine.arcTo(QRectF(cx - rx + 18, base - ry + 18, 2 * rx - 36, 2 * ry - 36), 150, -38)
            p.setPen(stroke(faded(QColor("#ffffff"), 0.35 if pal.dark else 0.8), 3.0))
            p.drawPath(shine)

    def paint_wire(self, p, st):
        """The tripwire across the dome: a faint dashed line, flashing when the key reaches it."""
        pal = self.pal
        wire = st["wire"]
        if wire["in"] <= 0:
            return
        half = dome_x(Y.TRIPWIRE_Y) - 8
        a, b = QPointF(Y.DOME[0] - half, Y.TRIPWIRE_Y), QPointF(Y.DOME[0] + half, Y.TRIPWIRE_Y)
        flash = wire["flash"]
        pen = QPen(self.solid(pal.bad, (0.32 + 0.68 * flash) * wire["in"]), 1.4 + 2.2 * flash)
        if flash < 0.5:
            pen.setDashPattern([3, 4])
        p.setPen(pen)
        p.drawLine(a, b)
        for end in (a, b):  # its two emitters
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(faded(pal.bad, 0.8 * wire["in"]))
            p.drawEllipse(end, 3.2, 3.2)
        if flash > 0:
            self.glow(p, QPointF(Y.DOME[0], Y.TRIPWIRE_Y), half * 1.1, pal.bad, 0.5 * flash)

    def paint_key(self, p, L, st):
        """The decoy key on the floor, the canary sitting on it (singing once the key is taken), its name."""
        pal = self.pal
        canary = st["canary"]
        if canary["in"] <= 0:
            return
        kx, ky = Y.KEY
        p.save()
        p.setOpacity(p.opacity() * T.clamp(canary["in"]))
        if canary["key"] > 0:
            self.draw_key(p, QPointF(kx + 14, ky + 6), 1.0, mix(pal.warn, pal.fg, 0.35))
        bird = QPointF(kx - 8, ky - 14 - 3 * abs(canary["flap"]))
        if canary["alarm"] > 0:  # its song: rings going out
            for k in range(3):
                g = (canary["song"] + k / 3) % 1.0
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(faded(pal.warn, 0.7 * (1 - g) * canary["alarm"]), 2.0))
                r = 18 + 50 * g
                p.drawEllipse(bird, r, r)
        self.draw_canary(p, bird, T.clamp(canary["in"]), canary["flap"])
        p.restore()
        p.save()
        p.setOpacity(p.opacity() * st["labels"][0])
        self.texts(p, L, (("key", pal.muted),))
        p.restore()

    def draw_key(self, p, at, scale, colour):
        p.save()
        p.translate(at)
        p.scale(scale, scale)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(stroke(colour, 3.0))
        p.drawEllipse(QPointF(-12, 0), 6.5, 6.5)
        p.drawLine(QPointF(-5.5, 0), QPointF(14, 0))
        p.drawLine(QPointF(8, 0), QPointF(8, 6))
        p.drawLine(QPointF(13, 0), QPointF(13, 5))
        p.restore()

    def draw_canary(self, p, at, size, flap):
        """A small canary: a round body, a head with a beak and an eye, a tail and a wing (it flaps when it
        sings)."""
        pal = self.pal
        yellow = pal.warn
        p.save()
        p.translate(at)
        p.scale(size, size)
        p.setPen(Qt.PenStyle.NoPen)
        tail = QPainterPath(QPointF(-11, 1))
        tail.lineTo(-22, -3)
        tail.lineTo(-20, 5)
        tail.closeSubpath()
        p.setBrush(mix(yellow, QColor("#000000"), 0.2))
        p.drawPath(tail)
        p.setBrush(yellow)
        p.drawEllipse(QPointF(0, 2), 12, 9)
        p.drawEllipse(QPointF(9, -7), 7, 7)
        beak = QPainterPath(QPointF(15, -8))
        beak.lineTo(21, -6)
        beak.lineTo(15, -4.5)
        beak.closeSubpath()
        p.setBrush(mix(pal.bad, yellow, 0.4))
        p.drawPath(beak)
        p.setBrush(QColor("#1a1a1a"))
        p.drawEllipse(QPointF(11, -8.5), 1.4, 1.4)
        p.save()
        p.translate(-2, 0)
        p.rotate(-18 - 30 * flap)
        p.setBrush(mix(yellow, QColor("#000000"), 0.14))
        p.drawEllipse(QPointF(-3, 0), 8, 4.6)
        p.restore()
        p.setPen(stroke(mix(yellow, QColor("#000000"), 0.4), 1.4))
        p.drawLine(QPointF(-2, 10), QPointF(-3, 15))
        p.drawLine(QPointF(3, 10), QPointF(3, 15))
        p.restore()

    def paint_agent(self, p, L, st):
        """The agent: a core with a spark, a ring and three orbiting dots; paused: a badge and its word."""
        pal = self.pal
        a = st["agent"]
        if a["in"] <= 0:
            return
        ax, ay, ar = Y.AGENT
        paused = st["dome"]["paused"]
        p.save()
        p.translate(ax, ay)
        p.scale(a["in"], a["in"])
        colour = mix(pal.accent, pal.warn, paused * 0.6)
        self.glow(p, QPointF(0, 0), ar * 2.2, colour, pal.glow * (0.7 + 0.5 * a["pulse"]) * (1 - 0.5 * paused))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self.solid(colour, 0.4), 1.5))
        p.drawEllipse(QPointF(0, 0), ar, ar)
        p.setPen(Qt.PenStyle.NoPen)
        spin = a["spin"] * (1 - paused)
        for k in range(3):
            angle = spin + k * 2 * math.pi / 3
            p.setBrush(faded(colour, 1.0 - k * 0.25))
            p.drawEllipse(QPointF(math.cos(angle) * ar, math.sin(angle) * ar), 3.6 - k * 0.6, 3.6 - k * 0.6)
        core = ar * 0.58
        gradient = QLinearGradient(0, -core, 0, core)
        gradient.setColorAt(0, mix(colour, QColor("#ffffff"), 0.15))
        gradient.setColorAt(1, mix(colour, QColor("#000000"), 0.25))
        p.setBrush(gradient)
        p.drawEllipse(QPointF(0, 0), core, core)
        r, q = core * 0.62, core * 0.1
        spark = QPainterPath(QPointF(0, -r))
        spark.quadTo(q, -q, r, 0)
        spark.quadTo(q, q, 0, r)
        spark.quadTo(-q, q, -r, 0)
        spark.quadTo(-q, -q, 0, -r)
        p.setBrush(pal.on_accent)
        p.drawPath(spark)
        p.restore()
        p.save()
        p.setOpacity(p.opacity() * T.clamp(a["in"]) * (1 - paused))
        self.texts(p, L, (("agent", pal.muted),))
        p.restore()
        if paused > 0:  # the pause badge and its word
            badge = QPointF(ax + ar * 0.85, ay - ar * 0.85)
            radius = 12 * T.spring(paused, 6, 1.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.warn)
            p.drawEllipse(badge, radius, radius)
            ink = pal.on_accent if theme.luminance(pal.warn) < 0.5 else QColor("#1a1a1a")
            p.setBrush(ink)
            k = radius / 12
            p.drawRoundedRect(QRectF(badge.x() - 4.6 * k, badge.y() - 5 * k, 3.2 * k, 10 * k), 1, 1)
            p.drawRoundedRect(QRectF(badge.x() + 1.4 * k, badge.y() - 5 * k, 3.2 * k, 10 * k), 1, 1)
            p.save()
            p.setOpacity(p.opacity() * paused)
            self.texts(p, L, (("paused", mix(pal.warn, pal.fg, 0.35)),))
            p.restore()

    def paint_script(self, p, st):
        """The script that takes the key: a small diamond with a bracket, carrying the key; a burst at the wire."""
        pal = self.pal
        sc = st["script"]
        if sc["alpha"] > 0:
            p.save()
            p.setOpacity(p.opacity() * sc["alpha"])
            at = QPointF(sc["x"], sc["y"] + 2 * sc["bob"])
            colour = mix(pal.bad, pal.fg, 0.2)
            self.glow(p, at, 22, pal.bad, 0.35)
            diamond = QPainterPath(at + QPointF(0, -11))
            diamond.lineTo(at + QPointF(11, 0))
            diamond.lineTo(at + QPointF(0, 11))
            diamond.lineTo(at + QPointF(-11, 0))
            diamond.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawPath(diamond)
            p.setPen(stroke(pal.surface, 1.6))
            p.drawLine(at + QPointF(-3, -4), at + QPointF(-6, 0))
            p.drawLine(at + QPointF(-6, 0), at + QPointF(-3, 4))
            p.drawLine(at + QPointF(3, -4), at + QPointF(6, 0))
            p.drawLine(at + QPointF(6, 0), at + QPointF(3, 4))
            if sc["carry"]:
                self.draw_key(p, at + QPointF(4, 18), 0.8, mix(pal.warn, pal.fg, 0.35))
            p.restore()
        if 0 < sc["burst"] < 1:  # caught at the wire: rings and sparks
            g = T.ease_out_cubic(sc["burst"])
            at = QPointF(sc["x"], sc["y"])
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(faded(pal.bad, 0.8 * (1 - g)), 2.2))
            p.drawEllipse(at, 10 + 44 * g, 10 + 44 * g)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(faded(pal.bad, 1 - g))
            for k in range(8):
                angle = k * math.pi / 4 + 0.3
                p.drawEllipse(at + QPointF(math.cos(angle), math.sin(angle)) * (8 + 38 * g), 2.4, 2.4)

    @staticmethod
    def trail_path(host):
        """From the beacon over the city to the host's tower."""
        bx, by = Y.BEACON
        x, top = Y.tower_top(Y.host_tower(host))
        end = QPointF(x, top - 26)
        path = QPainterPath(QPointF(bx, by - 10))
        lift = 150 + (x - bx) * 0.12
        path.cubicTo(QPointF(bx + (x - bx) * 0.25, by - lift), QPointF(x - (x - bx) * 0.2, top - lift * 0.9), end)
        return path

    @staticmethod
    def up_path():
        ax, ay, ar = Y.AGENT
        bx, by = Y.BEACON
        path = QPainterPath(QPointF(ax, ay - ar))
        path.cubicTo(QPointF(ax, ay - ar - 50), QPointF(bx, by + 60), QPointF(bx, by + 4))
        return path

    def paint_trails(self, p, st):
        """The requests: up to the beacon, then as light trails over the city; the faint arcs they leave."""
        pal = self.pal
        up = self.up_path()
        for request in st["trails"]:
            if request["alpha"] <= 0:
                continue
            path = self.trail_path(request["host"])
            u = request["u"]
            colour = pal.warn if request["state"] == "waiting" else pal.ok if u > 1 else \
                mix(pal.accent, QColor("#ffffff"), 0.3)
            if u > 1:  # the arc it leaves, staying faintly
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(stroke(self.solid(pal.ok, 0.4), 1.6))
                p.drawPath(partial(path, T.clamp(u - 1), 64))
            if request["state"] == "arrived":
                continue
            heads = []
            for k in range(10):  # the comet: its head and a tail fading behind it
                v = u - 0.035 * k
                if v < 0:
                    break
                heads.append(up.pointAtPercent(T.clamp(v)) if v <= 1 else path.pointAtPercent(T.clamp(v - 1)))
            for k in range(len(heads) - 1):
                p.setPen(stroke(self.solid(colour, 0.75 * (1 - k / 9)), 5.0 * (1 - k / 12)))
                p.drawLine(heads[k], heads[k + 1])
            if heads:
                pulse = 0.6 + 0.4 * math.sin(request["wait"] * 9) if request["state"] == "waiting" else 1.0
                self.glow(p, heads[0], 26, colour, 0.8 * pulse)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(mix(colour, QColor("#ffffff"), 0.4))
                p.drawEllipse(heads[0], 4.2, 4.2)

    def paint_beacon(self, p, L, st):
        """The proxy: a lantern on the dome's top, its light following what passes."""
        pal = self.pal
        b = st["beacon"]
        if b["in"] <= 0:
            return
        bx, by = Y.BEACON
        colour, strength = pal.accent, 0.0
        for request in st["trails"]:
            since = st["twinkle"] - request["at_beacon"]
            if request["state"] == "waiting":
                colour, strength = pal.warn, 0.6 + 0.4 * math.sin(request["wait"] * 9)
            elif request["verdict"] != "ask" and 0 <= since < 0.5:
                colour, strength = pal.ok, 1 - since / 0.5
            elif request["verdict"] == "ask" and request["u"] > 1 and request["u"] < 1.4:
                colour, strength = pal.ok, 1.4 - request["u"]
        if st["wire"]["flash"] > 0:
            colour, strength = pal.bad, st["wire"]["flash"]
        p.save()
        p.translate(bx, by)
        p.scale(b["in"], b["in"])
        self.glow(p, QPointF(0, -8), 40 + 24 * strength, colour, pal.glow * 1.5 + 0.45 * strength)
        p.setPen(QPen(mix(pal.hairline, pal.accent, 0.5), 1.4))
        p.setBrush(pal.surface)
        p.drawRoundedRect(QRectF(-11, -20, 22, 20), 5, 5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix(pal.accent, colour, min(1.0, strength + 0.2)))
        p.drawEllipse(QPointF(0, -10), 5 + 1.5 * strength, 5 + 1.5 * strength)
        roof = QPainterPath(QPointF(-14, -20))
        roof.lineTo(0, -31)
        roof.lineTo(14, -20)
        roof.closeSubpath()
        p.setBrush(mix(pal.accent, pal.fg, 0.2))
        p.drawPath(roof)
        p.restore()
        p.save()
        p.setOpacity(p.opacity() * T.clamp(b["in"]))
        self.texts(p, L, (("proxy", pal.muted),))
        p.restore()

    def paint_card(self, p, L, st):
        """The approval question for the new host: it pops up above the beacon, Allow is pressed, it goes."""
        pal = self.pal
        a = st["approval"]
        alpha = T.clamp(a["in"]) * (1 - a["out"])
        if alpha <= 0:
            return
        x, y, w, h = Y.CARD
        rect = QRectF(x, y, w, h)
        p.save()
        p.setOpacity(p.opacity() * alpha)
        p.translate(0, -12 * a["out"])
        scale = 0.9 + 0.1 * max(0.0, a["in"])
        if abs(scale - 1) > 0.004:
            anchor = QPointF(Y.BEACON[0], y + h)
            p.translate(anchor)
            p.scale(scale, scale)
            p.translate(-anchor)

        def body(q):
            self.shadow(q, rect, 14, 1.1, spread=26, drop=9)
            bx = Y.BEACON[0]
            pointer = QPainterPath(QPointF(bx - 10, y + h - 1))
            pointer.lineTo(bx, y + h + 11)
            pointer.lineTo(bx + 10, y + h - 1)
            pointer.closeSubpath()
            shape = QPainterPath()
            shape.addRoundedRect(rect, 14, 14)
            q.setBrush(pal.surface)
            q.setPen(QPen(mix(pal.hairline, pal.warn, 0.45), 1.3))
            q.drawPath(shape.united(pointer))
            q.setPen(QPen(pal.hairline, 1.2))
            q.setBrush(Qt.BrushStyle.NoBrush)
            q.drawRoundedRect(QRectF(*CityLayout.button(0)), 8, 8)
            self.texts(q, L, (("ask", pal.fg), ("deny", pal.muted)))
        self.cached(p, ("city-card",), rect.adjusted(-40, -40, 40, 40), body)
        icon = QPointF(x + 33, y + 26)
        colour = mix(pal.warn, pal.ok, a["done"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(colour)
        p.drawEllipse(icon, 13, 13)
        ink = pal.on_accent if theme.luminance(colour) < 0.5 else pal.bg
        if a["done"] > 0:
            tick = QPainterPath(icon + QPointF(-5, 0.3))
            tick.lineTo(icon + QPointF(-1.5, 4))
            tick.lineTo(icon + QPointF(5.3, -4))
            p.setPen(stroke(ink, 2.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(partial(tick, a["done"], 16))
        else:
            p.setBrush(ink)
            p.drawEllipse(icon + QPointF(0, 4.5), 1.6, 1.6)
            p.drawRoundedRect(QRectF(icon.x() - 1.3, icon.y() - 6, 2.6, 8), 1.2, 1.2)
        button = QRectF(*CityLayout.button(1))
        pressed = a["press"]
        if pressed > 0.004:
            p.translate(button.center())
            p.scale(1 - 0.06 * pressed, 1 - 0.06 * pressed)
            p.translate(-button.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix(pal.accent, pal.accent_deep, 0.6 * pressed + 0.4 * a["done"]))
        p.drawRoundedRect(button, 8, 8)
        self.texts(p, L, (("allow", pal.on_accent),))
        p.restore()
