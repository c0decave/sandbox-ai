"""The "Vault" intro (intro_vault_timeline.py tells the story; this draws it, on intro.py's machinery).

A blueprint: a grid on paper (or night-blue paper in a dark theme), construction lines, five rings drawn by a
compass around the agent's core and turned into place, a terminal ticking off the checks, probes stopped at their
rings, and the allowed request passing the lined-up notches, getting its key at the outer ring. Each ring's
shapes are made once (paths in its own units) and turned; the terminal's body and the legend come from pixmaps.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainterPath, QPen

from . import intro_timeline as T, intro_vault_timeline as V, theme
from .i18n import tr
from .intro import Layout, Renderer, draw_text, faded, grain, mix, partial, stroke, vignette


def captions():
    return (tr("The sandbox's layers lock around your AI agent, each one verified."),
            tr("Each layer stops something: a system call, a privilege, your files, a host."),
            tr("Allowed requests pass; the proxy adds your key outside the sandbox."))


def ring_names():
    return ("Seccomp", "NoNewPrivs", tr("Firejail namespaces"), "Podman · Bubblewrap", tr("Firewall · proxy"))


def checks():
    """The terminal's lines: the command, one check per ring, the start."""
    return ("$ sandbox-ai claude", tr("Seccomp filter"), tr("capabilities dropped, NoNewPrivs"),
            tr("mount and PID namespaces"), tr("rootless Podman or Bubblewrap"), tr("default-deny network, proxy"),
            tr("agent started"))


def polar(r, angle):
    return QPointF(V.CENTRE[0] + r * math.cos(angle), V.CENTRE[1] + r * math.sin(angle))


class VaultLayout(Layout):
    """The vault's texts: the rings' legend and numbers, the terminal's lines, the probes, the host."""

    def captions(self):
        return captions()

    def build_story(self):
        semi, medium = QFont.Weight.DemiBold, QFont.Weight.Medium
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        x, y, step, width = V.LEGEND
        for k, name in enumerate(ring_names()):
            cy = y + k * step
            self.add(f"ring{k}", name, 17, self.rect(x + 34, cy - 14, width - 34, 28), left, weight=medium, least=9)
            self.add(f"num{k}", str(k + 1), 13, self.centred(x + 12, cy, 20, 20), weight=semi, least=8)
            self.add(f"call{k}", str(k + 1), 13, self.centred(V.CENTRE[0] - V.RINGS[k], V.CENTRE[1], 20, 20),
                     weight=semi, least=8)
        tx, ty, tw, _th = V.TERMINAL
        for k, line in enumerate(checks()):
            self.add(f"line{k}", line, 16, self.rect(tx + 52, ty + 50 + k * 31 - 13, tw - 74, 26), left, mono=True,
                     least=8)
        for k, (what, ring, angle, _leave) in enumerate(V.PROBES):
            stop = V.RINGS[ring] - V.BAND / 2 - 8
            at = polar(stop + 44, angle)
            self.add(f"probe{k}", what, 15, self.centred(at.x() + 8, at.y(), 220, 24), mono=True, least=8)
        cx, cy, cw, ch = V.CHIP
        self.add("host", V.HOST, 19, self.rect(cx + 56, cy + 14, cw - 110, ch - 28), left, mono=True, least=9)
        self.add("keyed", tr("key added by the proxy"), 15, self.centred(cx + cw / 2, cy + ch + 24, cw + 40, 24),
                 least=9)

    GROUPS = (tuple(f"ring{k}" for k in range(5)) + tuple(f"line{k}" for k in range(7)) +
              tuple(f"probe{k}" for k in range(4)) + ("caption0", "skip", "esc"),
              tuple(f"ring{k}" for k in range(5)) + tuple(f"line{k}" for k in range(7)) +
              ("host", "keyed", "caption2", "skip", "esc"),
              ("word", "tag", "agents", "skip", "esc"))


class VaultRenderer(Renderer):
    TIMELINE = V
    LAYOUT = VaultLayout

    def __init__(self, mode=None, font=None):
        super().__init__(mode, font)
        self.shapes = [self.ring_shapes(k) for k in range(len(V.RINGS))]

    def ground(self):
        return mix(self.pal.bg, self.pal.accent, 0.13 if self.pal.dark else 0.045)

    def ink(self):
        """The blueprint's line colour."""
        pal = self.pal
        return mix(pal.accent, QColor("#ffffff"), 0.25) if pal.dark else pal.accent_deep

    def paint_background(self, p, width, height):
        """Blueprint paper: a tinted ground, a fine grid with a coarser one over it, the stage's corner marks."""
        pal, L = self.pal, self.layout
        paper = mix(pal.bg, pal.accent, 0.13 if pal.dark else 0.045)
        gradient = QLinearGradient(0, 0, 0, height)
        gradient.setColorAt(0, mix(paper, QColor("#ffffff"), 0.02))
        gradient.setColorAt(1, mix(paper, QColor("#000000"), 0.25) if pal.dark else paper)
        p.fillRect(QRectF(0, 0, width, height), gradient)
        ink = self.ink()
        for step, alpha in ((20, 0.05 if pal.dark else 0.06), (100, 0.12 if pal.dark else 0.13)):
            spacing = step * L.s
            if spacing < 5:
                continue
            p.setPen(QPen(faded(ink, alpha), max(1.0, 0.8 * L.s) if step == 100 else 1.0))
            x = L.ox % spacing
            while x < width:
                p.drawLine(QPointF(x, 0), QPointF(x, height))
                x += spacing
            y = L.oy % spacing
            while y < height:
                p.drawLine(QPointF(0, y), QPointF(width, y))
                y += spacing
        p.setPen(QPen(faded(ink, 0.35), max(1.0, 1.2 * L.s)))
        for cx, cy in ((24, 24), (V.WIDTH - 24, 24), (24, V.HEIGHT - 24), (V.WIDTH - 24, V.HEIGHT - 24)):
            x, y, r = L.ox + cx * L.s, L.oy + cy * L.s, 10 * L.s
            p.drawLine(QPointF(x - r, y), QPointF(x + r, y))
            p.drawLine(QPointF(x, y - r), QPointF(x, y + r))
        p.fillRect(QRectF(0, 0, width, height), vignette(width, height, pal, 0.5, 100, 14))
        p.setOpacity(0.022 if pal.dark else 0.018)
        p.drawTiledPixmap(QRectF(0, 0, width, height), grain())
        p.setOpacity(1.0)

    @staticmethod
    def ring_shapes(k):
        """Ring K in its own units (centre 0, its notch at angle 0): the band without the notch, its ticks, its
        bolts."""
        r, half = V.RINGS[k], V.BAND / 2
        gap = math.degrees(V.NOTCH) / 2
        outer, inner = QRectF(-r - half, -r - half, 2 * (r + half), 2 * (r + half)), \
            QRectF(-r + half, -r + half, 2 * (r - half), 2 * (r - half))
        band = QPainterPath()
        band.arcMoveTo(outer, -gap)
        band.arcTo(outer, -gap, -(360 - 2 * gap))
        band.arcTo(inner, gap, 360 - 2 * gap)
        band.closeSubpath()
        ticks = QPainterPath()
        count = 72 if k > 1 else 48
        for j in range(count):
            angle = 2 * math.pi * j / count
            if abs(math.remainder(angle, 2 * math.pi)) < V.NOTCH / 2 + 0.03:
                continue
            length = 7.0 if j % 6 == 0 else 3.5
            a = r + half + 2
            ticks.moveTo(a * math.cos(angle), a * math.sin(angle))
            ticks.lineTo((a + length) * math.cos(angle), (a + length) * math.sin(angle))
        bolts = [QPointF(r * math.cos(a), r * math.sin(a)) for a in (math.pi * 2 / 3, math.pi * 4 / 3, math.pi)]
        return band, ticks, bolts

    # -- the story --

    def paint_story(self, p, L, s):
        st = s["story"]
        if st is None:
            return
        self.stage(p, L)
        self.paint_construction(p, st)
        rings = st["rings"]
        still = [k for k, ring in enumerate(rings) if ring["still"]]
        if still:  # the rings that do not move: one pixmap, made again only when that changes
            version = tuple((k, round(rings[k]["angle"], 4)) for k in still)
            out = V.RINGS[-1] + V.BAND / 2 + 16
            area = QRectF(V.CENTRE[0] - out, V.CENTRE[1] - out, 2 * out, 2 * out)
            self.latest(p, "vault-rings", version, area, lambda q: [self.ring_body(q, k, rings[k]) for k in still])
        for k, ring in enumerate(rings):
            if ring["still"]:
                self.ring_hit(p, k, ring)
            else:
                self.paint_ring(p, k, ring)
        self.paint_core(p, st)
        self.paint_probes(p, L, st)
        self.paint_request(p, st)
        self.paint_legend(p, L, st)
        self.paint_terminal(p, L, st)
        self.paint_chip(p, L, st)
        p.restore()
        self.paint_caption(p, L, s)

    def paint_construction(self, p, st):
        """The centre lines (dash-dot) through the core, and the way out, drawing themselves from the middle."""
        f = st["lines"]
        if f <= 0:
            return
        ink = self.ink()
        cx, cy = V.CENTRE
        reach = 330 * f
        pen = QPen(self.solid(ink, 0.45), 1.1)
        pen.setDashPattern([14, 4, 2, 4])
        p.setPen(pen)
        p.drawLine(QPointF(cx - reach, cy), QPointF(cx + reach, cy))
        p.drawLine(QPointF(cx, cy - reach), QPointF(cx, cy + reach))
        pen = QPen(self.solid(ink, 0.25), 1.0)
        pen.setDashPattern([4, 5])
        p.setPen(pen)
        p.drawLine(polar(0, V.EXIT), polar(reach * 1.05, V.EXIT))
        p.setPen(QPen(self.solid(ink, 0.4 * f), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), 6, 6)

    def paint_ring(self, p, k, ring):
        pal = self.pal
        ink = self.ink()
        r, half = V.RINGS[k], V.BAND / 2
        cx, cy = V.CENTRE
        if 0 < ring["compass"] < 1 or (ring["compass"] >= 1 and ring["fill"] < 1):  # the compass at work
            start = ring["angle"]
            sweep = ring["compass"] * 2 * math.pi
            arc = QPainterPath()
            rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
            arc.arcMoveTo(rect, -math.degrees(start))
            arc.arcTo(rect, -math.degrees(start), -math.degrees(sweep))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(self.solid(ink, 0.7 * (1 - ring["fill"])), 1.2))
            p.drawPath(arc)
            if ring["compass"] < 1:
                tip = polar(r, start + sweep)
                p.setPen(QPen(self.solid(ink, 0.3), 1.0))
                p.drawLine(QPointF(cx, cy), tip)  # the compass' arm
                self.glow(p, tip, 16, pal.accent, 0.8)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(mix(pal.accent, QColor("#ffffff"), 0.5))
                p.drawEllipse(tip, 2.6, 2.6)
        if ring["fill"] <= 0:
            return
        self.ring_body(p, k, ring)
        if ring["lock"] > 0:  # locked: a flash running round it
            self.glow(p, polar(r, ring["angle"] + math.pi), 40, pal.accent, 0.7 * ring["lock"])
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(faded(mix(pal.accent, QColor("#ffffff"), 0.4), 0.8 * ring["lock"]), 2.4))
            grow = (1 - ring["lock"]) * 10
            p.drawEllipse(QPointF(cx, cy), r + half + grow, r + half + grow)
        self.ring_hit(p, k, ring)

    def ring_body(self, p, k, ring):
        """Ring K itself: its band (without the notch), its ticks and bolts, turned to its angle."""
        pal, ink = self.pal, self.ink()
        band, ticks, bolts = self.shapes[k]
        cx, cy = V.CENTRE
        p.save()
        p.setOpacity(p.opacity() * ring["fill"])
        p.translate(cx, cy)
        p.rotate(math.degrees(ring["angle"]))
        fill = faded(pal.accent, (0.16 if pal.dark else 0.11) + 0.25 * ring["lock"])
        p.setBrush(fill)
        p.setPen(QPen(self.solid(ink, 0.85), 1.4))
        p.drawPath(band)
        p.setPen(QPen(self.solid(ink, 0.55), 1.1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(ticks)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(faded(ink, 0.7))
        for bolt in bolts:
            p.drawEllipse(bolt, 2.6, 2.6)
        p.restore()

    def ring_hit(self, p, k, ring):
        pal = self.pal
        r = V.RINGS[k]
        cx, cy = V.CENTRE
        if ring["hit"] > 0:  # a probe stopped here: its arc lights up red
            for what, which, angle, _leave in V.PROBES:
                if which == k:
                    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
                    arc = QPainterPath()
                    arc.arcMoveTo(rect, -math.degrees(angle) + 16)
                    arc.arcTo(rect, -math.degrees(angle) + 16, -32)
                    p.setPen(stroke(faded(pal.bad, 0.85 * ring["hit"]), V.BAND - 4, Qt.PenCapStyle.FlatCap))
                    p.drawPath(arc)

    def paint_core(self, p, st):
        """The agent's core: a disc with a spark, a dashed orbit turning around it."""
        pal = self.pal
        f = st["core"]
        if f <= 0:
            return
        cx, cy = V.CENTRE
        p.save()
        p.translate(cx, cy)
        p.scale(f, f)
        self.glow(p, QPointF(0, 0), V.CORE * 2.0, pal.accent, pal.glow * (1.2 + 0.5 * st["pulse"]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(self.solid(self.ink(), 0.6), 1.4)
        pen.setDashPattern([3, 3])
        p.setPen(pen)
        p.save()
        p.rotate(math.degrees(st["spin"]))
        p.drawEllipse(QPointF(0, 0), V.CORE - 6, V.CORE - 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.accent)
        p.drawEllipse(QPointF(V.CORE - 6, 0), 3.6, 3.6)
        p.restore()
        core = V.CORE * 0.55
        gradient = QLinearGradient(0, -core, 0, core)
        gradient.setColorAt(0, mix(pal.accent, QColor("#ffffff"), 0.15))
        gradient.setColorAt(1, pal.accent_deep)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(gradient)
        p.drawEllipse(QPointF(0, 0), core, core)
        r, q = core * (0.62 + 0.06 * st["pulse"]), core * 0.1
        spark = QPainterPath(QPointF(0, -r))
        spark.quadTo(q, -q, r, 0)
        spark.quadTo(q, q, 0, r)
        spark.quadTo(-q, q, -r, 0)
        spark.quadTo(-q, -q, 0, -r)
        p.setBrush(pal.on_accent)
        p.drawPath(spark)
        p.restore()

    def paint_probes(self, p, L, st):
        """The probes: out from the core, stopped at their ring with a shake, their tag with a lock."""
        pal = self.pal
        for k, probe in enumerate(st["probes"]):
            if probe["alpha"] > 0:
                _what, _ring, angle, _leave = V.PROBES[k]
                at = polar(probe["r"], angle) + QPointF(-math.sin(angle), math.cos(angle)) * probe["shake"] * 6
                colour = pal.bad if probe["stopped"] else mix(pal.bad, pal.fg, 0.35)
                p.save()
                p.setOpacity(p.opacity() * probe["alpha"])
                tail = [polar(max(V.CORE, probe["r"] - 7 * j), angle) for j in range(6)]
                if not probe["stopped"]:
                    for j in range(5):
                        p.setPen(stroke(self.solid(colour, 0.5 * (1 - j / 5)), 4.0 * (1 - j / 7)))
                        p.drawLine(tail[j], tail[j + 1])
                self.glow(p, at, 20, pal.bad, 0.5)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(colour)
                p.drawEllipse(at, 5.0, 5.0)
                p.restore()
            if probe["tag"] > 0:
                text = L.texts[f"probe{k}"]
                rect = QRectF((text.rect.left() - L.ox) / L.s - 30, (text.rect.top() - L.oy) / L.s - 4,
                              text.rect.width() / L.s + 40, text.rect.height() / L.s + 8)
                p.save()
                p.setOpacity(p.opacity() * probe["tag"])
                p.setBrush(faded(pal.surface, 0.92))
                p.setPen(QPen(mix(pal.surface, pal.bad, 0.6), 1.1))
                p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
                dot = QPointF(rect.left() + 14, rect.center().y())
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(pal.bad)
                p.drawEllipse(dot, 8.5, 8.5)
                self.paint_lock(p, dot, 0.62, pal.on_accent if theme.luminance(pal.bad) < 0.5 else pal.bg)
                self.texts(p, L, ((f"probe{k}", pal.fg),))
                p.restore()

    @staticmethod
    def request_path():
        """The allowed request's way: straight out through the notches, then on to the host."""
        out = V.RINGS[-1] + V.BAND / 2 + 16
        start, edge = polar(V.CORE + 4, V.EXIT), polar(out, V.EXIT)
        end = QPointF(V.CHIP[0] - 6, V.CHIP[1] + V.CHIP[3] / 2)
        path = QPainterPath(start)
        path.lineTo(edge)
        path.cubicTo(edge + QPointF(math.cos(V.EXIT), math.sin(V.EXIT)) * 80, end - QPointF(80, 0), end)
        return path, path.percentAtLength(out - V.CORE - 4)

    def paint_request(self, p, st):
        pal = self.pal
        req = st["request"]
        path, split = self.request_path()
        if st["aligned"] > 0:  # the lane through the lined-up notches
            pen = QPen(self.solid(pal.ok, 0.55 * st["aligned"]), 1.6)
            pen.setDashPattern([3, 4])
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(partial(path, split, 24))
        if req["u"] > 1:
            p.setPen(stroke(self.solid(pal.ok, 0.45), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(partial(path, split + (1 - split) * T.clamp(req["u"] - 1), 64))
        if req["alpha"] <= 0:
            return

        def at(u):
            return path.pointAtPercent(T.clamp(split * u if u <= 1 else split + (1 - split) * (u - 1)))
        p.save()
        p.setOpacity(p.opacity() * req["alpha"])
        for j in range(8):
            a, b = at(max(0.0, req["u"] - 0.04 * j)), at(max(0.0, req["u"] - 0.04 * (j + 1)))
            p.setPen(stroke(self.solid(pal.ok, 0.6 * (1 - j / 8)), 5.0 * (1 - j / 10)))
            p.drawLine(a, b)
        head = at(req["u"])
        self.glow(p, head, 26, pal.ok, 0.8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.ok)
        p.drawEllipse(head, 5.5, 5.5)
        if req["key"] > 0:  # the key the proxy added, riding along
            self.glow(p, head + QPointF(0, -20), 22, pal.warn, 0.6 * req["flash"] + 0.25)
            self.draw_key(p, head + QPointF(2, -20), 0.85 * req["key"], pal.warn)
        p.restore()
        if req["flash"] > 0:
            edge = polar(V.RINGS[-1], V.EXIT)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(faded(pal.warn, 0.8 * req["flash"]), 2.0))
            r = 14 + (1 - req["flash"]) * 40
            p.drawEllipse(edge, r, r)

    @staticmethod
    def draw_key(p, at, scale, colour):
        p.save()
        p.translate(at)
        p.scale(scale, scale)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(stroke(colour, 3.0))
        p.drawEllipse(QPointF(-10, 0), 6, 6)
        p.drawLine(QPointF(-4, 0), QPointF(14, 0))
        p.drawLine(QPointF(8, 0), QPointF(8, 6))
        p.drawLine(QPointF(13, 0), QPointF(13, 5))
        p.restore()

    def legend_item(self, p, L, k, shown):
        pal, ink = self.pal, self.ink()
        x, y, step, _width = V.LEGEND
        p.save()
        p.setOpacity(p.opacity() * shown)
        for centre, name in ((QPointF(V.CENTRE[0] - V.RINGS[k], V.CENTRE[1]), f"call{k}"),
                             (QPointF(x + 12, y + k * step), f"num{k}")):
            p.setPen(QPen(ink, 1.2))
            p.setBrush(pal.surface)
            p.drawEllipse(centre, 11, 11)
            self.texts(p, L, ((name, ink),))
        self.texts(p, L, ((f"ring{k}", pal.fg),), dx=(1 - shown) * -10 * L.s)
        p.restore()

    def paint_legend(self, p, L, st):
        """The rings' numbers on them (left, on the centre line) and the legend naming them; the ones shown in
        full from one pixmap."""
        shown = [ring["label"] for ring in st["rings"]]
        full = sum(1 for value in shown if value >= 1)
        if full:
            x, y, step, width = V.LEGEND
            area = QRectF(x - 10, y - 30, V.CENTRE[0] - x, step * len(V.RINGS) + 40)
            self.latest(p, "vault-legend", full, area, lambda q: [self.legend_item(q, L, k, 1.0) for k in range(full)])
        for k, value in enumerate(shown):
            if 0 < value < 1:
                self.legend_item(p, L, k, value)

    def terminal_body(self, q, L):
        pal = self.pal
        x, y, w, h = V.TERMINAL
        rect = QRectF(x, y, w, h)
        self.shadow(q, rect, 14, 1.0, spread=30, drop=10)
        q.setBrush(pal.editor)
        q.setPen(QPen(mix(pal.hairline, self.ink(), 0.35), 1.2))
        q.drawRoundedRect(rect, 14, 14)
        q.setPen(QPen(pal.hairline, 1.0))
        q.drawLine(QPointF(x + 1, y + 30), QPointF(x + w - 1, y + 30))
        q.setPen(Qt.PenStyle.NoPen)
        for k, colour in enumerate((pal.bad, pal.warn, pal.ok)):
            q.setBrush(faded(colour, 0.75))
            q.drawEllipse(QPointF(x + 20 + k * 18, y + 15), 5, 5)

    def terminal_line(self, p, L, k, typed):
        """Line K of the terminal, TYPED (0..1) of it, with its tick (a check) or its dot (the start)."""
        pal = self.pal
        x, y, _w, _h = V.TERMINAL
        count = len(checks())
        text = L.texts[f"line{k}"]
        colour = pal.fg if k == 0 else pal.accent if k == count - 1 else mix(pal.fg, pal.muted, 0.3)
        p.save()
        p.scale(1 / L.s, 1 / L.s)
        p.translate(-L.ox, -L.oy)
        if typed < 1:
            p.setClipRect(QRectF(text.rect.left() - 2, text.rect.top() - 4, text.rect.width() * typed + 4,
                                 text.rect.height() + 8))
        draw_text(p, text, colour)
        p.restore()
        cy = y + 50 + k * 31
        if 0 < k < count - 1:  # a tick before each check
            p.setPen(stroke(pal.ok, 2.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            tick = QPainterPath(QPointF(x + 26, cy))
            tick.lineTo(x + 30, cy + 4)
            tick.lineTo(x + 38, cy - 5)
            p.drawPath(partial(tick, T.clamp(typed * 1.5), 12))
        elif k == count - 1:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.accent)
            p.drawEllipse(QPointF(x + 32, cy), 4.5 * typed, 4.5 * typed)
        return QPointF((text.rect.left() - L.ox) / L.s + text.rect.width() / L.s * typed + 4, cy - 9)

    def paint_terminal(self, p, L, st):
        """The terminal: the command, then a ticked line per ring as it locks, then the agent's start. Its body
        and the lines already typed come from one pixmap (made again when a line is complete)."""
        pal = self.pal
        term = st["terminal"]
        if term["in"] <= 0:
            return
        x, y, w, h = V.TERMINAL
        count = len(checks())
        done = min(count, int(term["lines"]))
        p.save()
        p.setOpacity(p.opacity() * term["in"])
        p.translate(0, (1 - term["in"]) * 16)

        def still(q):
            self.terminal_body(q, L)
            for k in range(done):
                self.terminal_line(q, L, k, 1.0)
        self.latest(p, "vault-terminal", done, QRectF(x - 50, y - 50, w + 100, h + 100), still)
        caret = None
        if done < count and term["lines"] > done:
            caret = self.terminal_line(p, L, done, term["lines"] - done)
        elif done < count:
            caret = QPointF((L.texts[f"line{done}"].rect.left() - L.ox) / L.s + 4, y + 50 + done * 31 - 9)
        if caret is not None and term["caret"]:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.accent)
            p.drawRect(QRectF(caret.x(), caret.y(), 2.4, 18))
        p.restore()

    def paint_chip(self, p, L, st):
        """The host the allowed request reaches: its name, its badge, and that the key was added there."""
        pal = self.pal
        shown = st["chip"]
        if shown <= 0:
            return
        req = st["request"]
        x, y, w, h = V.CHIP
        rect = QRectF(x + (1 - shown) * 24, y, w, h)
        p.save()
        p.setOpacity(p.opacity() * shown)
        self.shadow(p, rect, h / 2, 0.6, spread=18, drop=6)
        p.setBrush(pal.surface)
        p.setPen(QPen(mix(pal.hairline, pal.ok, 0.6 * req["badge"]), 1.3))
        p.drawRoundedRect(rect, h / 2, h / 2)
        self.texts(p, L, (("host", pal.fg),), dx=(1 - shown) * 24 * L.s)
        dot = QPointF(rect.left() + 30, rect.center().y())
        if req["badge"] > 0:
            radius = 12 * T.spring(req["badge"], 6, 1.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.ok)
            p.drawEllipse(dot, radius, radius)
            tick = QPainterPath(dot + QPointF(-5, 0.3))
            tick.lineTo(dot + QPointF(-1.5, 4))
            tick.lineTo(dot + QPointF(5.3, -4))
            p.setPen(stroke(pal.on_accent if theme.luminance(pal.ok) < 0.5 else pal.bg, 2.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(partial(tick, T.clamp(req["badge"] * 1.6), 12))
            self.draw_key(p, QPointF(rect.right() - 34, rect.center().y()), 0.8 * req["badge"], pal.warn)
            p.setOpacity(p.opacity() * req["badge"])
            self.texts(p, L, (("keyed", pal.muted),))
        else:
            pen = QPen(pal.line, 1.5)
            pen.setDashPattern([2, 3])
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(dot, 12, 12)
        p.restore()
