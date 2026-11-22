"""The "Glass cube" intro (intro_glass_timeline.py tells the story; this draws it, on intro.py's machinery).

A glass cube, turned in 3D and projected on the stage, with a network of light growing inside it; the walls
ripple where a tendril touches them, the port in the right wall (the proxy) lets two signals out to their hosts
and bounces a third. Ripples and the port are drawn in the plane of their face (an affine transform per face).
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QTransform

from . import intro_glass_timeline as G, intro_timeline as T, theme
from .i18n import tr
from .intro import Layout, Renderer, faded, glow, grain, mix, partial, stroke, vignette

def captions():
    return (tr("Your AI agent thinks and works inside a sandbox."),
            tr("Its walls hold. The only way out is through the proxy."),
            tr("Allowed hosts pass; anything else bounces off."))


class GlassLayout(Layout):
    """The glass cube's texts: its hosts and the port's name."""

    def captions(self):
        return captions()

    def build_story(self):
        medium = QFont.Weight.Medium
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        cx, cw, ch = G.CHIP
        for k, (host, _, _) in enumerate(G.SIGNALS):
            self.add(f"host{k}", host, 20, self.rect(cx + 56, G.CHIP_Y[k] - ch / 2 + 12, cw - 72, ch - 24), left,
                     mono=True, least=10)
        x, y = self.port_label_at(G.cube_state(5.0))
        self.add("proxy", tr("Proxy"), 18, self.centred(x, y, 150, 28), weight=medium, least=10)

    @staticmethod
    def port_label_at(cube):
        """Where the port's name is (stage units): right below the port, outside the cube."""
        x, y, _ = G.project(G.on_face(G.PORT[0], G.PORT[1], G.PORT[2]), cube)
        return x + 118, y + 62

    GROUPS = (("host0", "host1", "host2", "proxy", "caption0", "skip", "esc"),
              ("word", "tag", "agents", "skip", "esc"))


class GlassRenderer(Renderer):
    TIMELINE = G
    LAYOUT = GlassLayout

    def ground(self):
        return mix(self.pal.bg, self.pal.accent, 0.09 if self.pal.dark else 0.05)

    def paint_background(self, p, width, height):
        """A calm gradient with a soft light behind the cube, a vignette and a fine grain (no grid: the cube is
        the only geometry)."""
        pal, L = self.pal, self.layout
        top = mix(pal.bg, pal.accent, 0.06 if pal.dark else 0.03)
        bottom = mix(pal.bg, QColor("#000000"), 0.34) if pal.dark else mix(pal.bg, pal.fg, 0.035)
        gradient = QLinearGradient(0, 0, 0, height)
        gradient.setColorAt(0, top)
        gradient.setColorAt(1, bottom)
        p.fillRect(QRectF(0, 0, width, height), gradient)
        centre = QPointF(L.ox + G.CENTRE[0] * L.s, L.oy + G.CENTRE[1] * L.s)
        glow(p, centre, 520 * L.s, pal.accent, 0.16 if pal.dark else 0.08)
        glow(p, QPointF(width * 0.8, L.oy + 360 * L.s), 420 * L.s, pal.ok, 0.05 if pal.dark else 0.03)
        p.fillRect(QRectF(0, 0, width, height), vignette(width, height, pal, 0.5, 100, 16))
        p.setOpacity(0.022 if pal.dark else 0.018)
        p.drawTiledPixmap(QRectF(0, 0, width, height), grain())
        p.setOpacity(1.0)

    # -- the story --

    def paint_story(self, p, L, s):
        st = s["story"]
        if st is None:
            return
        self.stage(p, L)
        cube = st["cube"]
        points = [G.project(corner, cube) for corner in G.CORNERS]
        quads = {face: self.face_quad(face, cube) for face, *_ in G.FACES}
        seen = {face: G.facing(face, cube) for face, *_ in G.FACES}
        inner = 1 - cube["into_mark"]
        self.paint_floor(p, cube, st)
        # the far side: back walls and edges, seen through the glass
        for face, *_ in G.FACES:  # (their glass is too faint to be drawn: only their ripples)
            if seen[face] <= 0:
                self.paint_face(p, face, quads[face], cube, st, front=False)
        self.paint_edges(p, points, cube, back=True)
        if inner > 0.01:
            p.save()
            p.setOpacity(p.opacity() * inner)
            self.paint_network(p, cube, st)
            self.paint_tendrils(p, cube, st)
            self.paint_signals_inside(p, cube, st)
            p.restore()
        for face, *_ in G.FACES:
            if seen[face] > 0:
                self.paint_face(p, face, quads[face], cube, st, front=True)
        self.paint_edges(p, points, cube, back=False)
        if inner > 0.01:
            p.save()
            p.setOpacity(p.opacity() * inner)
            self.paint_port(p, quads[G.PORT[0]], cube, st)
            self.paint_outside(p, L, cube, st, s)
            p.restore()
        p.restore()
        self.paint_caption(p, L, s)

    @staticmethod
    def face_quad(face, cube):
        corners = [G.project(G.on_face(face, u, v), cube) for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        return QPolygonF([QPointF(x, y) for x, y, _ in corners])

    @staticmethod
    def face_transform(quad):
        """Face units (-1..1 on each axis) to the stage, through the projected QUAD: an affine map by three of its
        corners (the perspective is gentle; an affine transform is much cheaper to paint through than a
        projective one)."""
        origin, right, down = quad[0], quad[1], quad[3]
        ax, bx = (right - origin) / 2, (down - origin) / 2
        centre = origin + ax + bx
        return QTransform(ax.x(), ax.y(), bx.x(), bx.y(), centre.x(), centre.y())

    def paint_floor(self, p, cube, st):
        """A soft shadow under the floating cube."""
        pal = self.pal
        x, y = cube["cx"], cube["cy"] + G.SIZE * 1.78
        strength = cube["glass"] * (1 - cube["into_mark"])
        if strength <= 0.01:
            return
        for name, radius, colour, alpha in (
                ("shadow", G.SIZE * 1.5, QColor("#000000") if pal.dark else pal.fg, 0.55 if pal.dark else 0.16),
                ("light", G.SIZE * 1.1, pal.accent, pal.glow * 0.9 * (0.7 + 0.3 * st["breath"]))):
            def squashed(q, radius=radius, colour=colour):  # (painted once, a flat ellipse of light)
                q.translate(x, y)
                q.scale(1.0, 0.2)
                glow(q, QPointF(0, 0), radius, colour, 1.0)
            p.save()
            p.setOpacity(p.opacity() * T.clamp(alpha * strength))
            self.cached(p, ("glass-floor", name), QRectF(x - radius, y - radius * 0.2, 2 * radius, radius * 0.4),
                        squashed)
            p.restore()

    def paint_face(self, p, face, quad, cube, st, front):
        """A wall: tinted glass (the near ones with a sheen and the gleam passing), its ripples. The glass is the
        projected quad itself, its gradients from corner to corner (cheap at every size); only the ripples are
        drawn in the face's plane, clipped to it, while there are any."""
        pal = self.pal
        glass = cube["glass"]
        if glass <= 0:
            return
        corner, opposite = quad[0], quad[2]
        p.setPen(Qt.PenStyle.NoPen)
        if front:  # one flat tint per wall, lighter the more it faces the viewer (its edges hide its own edge)
            light = T.clamp(G.facing(face, cube))
            p.setBrush(faded(mix(pal.accent, QColor("#ffffff"), 0.15 + 0.35 * light),
                             (0.05 + 0.06 * light if pal.dark else 0.05 + 0.07 * light) * glass))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.drawPolygon(quad)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if front and 0 < cube["gleam"] < 1:  # a light passing over the glass, once
            f = -1.1 + 3.2 * T.ease_in_out_cubic(cube["gleam"])
            band = QLinearGradient(corner, opposite)
            for at, alpha in ((f - 0.25, 0), (f, 60 if pal.dark else 110), (f + 0.25, 0)):
                if 0 <= at <= 1:
                    band.setColorAt(at, QColor(255, 255, 255, alpha))
            band.setColorAt(0, QColor(255, 255, 255, int(max(0, 1 - abs(f) / 0.25) * (60 if pal.dark else 110))))
            band.setColorAt(1, QColor(255, 255, 255, int(max(0, 1 - abs(1 - f) / 0.25) * (60 if pal.dark else 110))))
            p.setBrush(band)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.drawPolygon(quad)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ripples = [(QPointF(t["u"], t["v"]), t["ripple"], pal.accent, 3, 1.0) for t in st["tendrils"]
                   if t["face"] == face and t["ripple"] is not None and not t["port"]]
        if face == G.PORT[0]:
            ripples += [(QPointF(G.PORT[1], G.PORT[2]), signal["back"], pal.bad, 4, 1.5) for signal in st["signals"]
                        if signal["verdict"] == "block" and signal["state"] == "blocked" and signal["back"] < 1]
        if not ripples:
            return
        p.save()
        clip = QPainterPath()
        clip.addPolygon(quad)
        p.setClipPath(clip)
        p.setTransform(self.face_transform(quad), True)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for centre, f, colour, rings, reach in ripples:
            self.ripple(p, centre, f, colour, front, rings, reach)
        p.restore()

    def ripple(self, p, centre, f, colour, front, rings=3, reach=1.0):
        """Rings spreading over a wall from CENTRE (face units) as F goes 0..1."""
        for k in range(rings):
            g = (f - k * 0.16) / (1 - (rings - 1) * 0.16)
            if not 0 < g < 1:
                continue
            r = 0.04 + reach * T.ease_out_cubic(g) * 0.75
            alpha = (1 - g) ** 1.4 * (0.95 if front else 0.55)
            p.setPen(QPen(self.solid(colour, alpha), 0.028 * (1 - 0.5 * g)))
            p.drawEllipse(centre, r, r)

    def paint_edges(self, p, points, cube, back):
        """The cube's edges: the ones behind thin and faint, the near ones bright, each drawing itself."""
        pal = self.pal
        for k, (a, b) in enumerate(G.EDGES):
            f = cube["edges"][k]
            if f <= 0:
                continue
            (xa, ya, za), (xb, yb, zb) = points[a], points[b]
            behind = (za + zb) / 2 < -0.35
            if behind != back:
                continue
            end = QPointF(T.lerp(xa, xb, f), T.lerp(ya, yb, f))
            colour = mix(pal.accent, pal.fg, 0.15) if not back else self.solid(pal.accent, 0.38)
            p.setPen(stroke(colour, 1.4 if back else 2.3))
            p.drawLine(QPointF(xa, ya), end)
            if 0 < f < 1 and not back:  # the drawing point glows
                self.glow(p, end, 18, pal.accent, 0.8)
        if not back:  # the near corners catch the light
            for x, y, z in points:
                if z > 0.4 and cube["edges"][-1] >= 1:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(mix(pal.accent, QColor("#ffffff"), 0.5))
                    p.drawEllipse(QPointF(x, y), 2.6, 2.6)

    @staticmethod
    def depth_alpha(z):
        return 0.38 + 0.62 * T.clamp((z + 1.4) / 2.8)

    def paint_network(self, p, cube, st):
        """The network of light: links, then nodes (the far ones fainter and smaller), signals running."""
        pal = self.pal
        projected = [G.project(node, cube) for node in G.NODES]
        bright = mix(pal.accent, QColor("#ffffff"), 0.45 if pal.dark else 0.1)
        for k in range(1, G.NODE_COUNT):
            f = st["tree"][k]
            if f <= 0:
                continue
            (xa, ya, za), (xb, yb, zb) = projected[G.PARENTS[k]], projected[k]
            alpha = self.depth_alpha((za + zb) / 2)
            p.setPen(stroke(self.solid(pal.accent, 0.62 * alpha), 1.3 + 0.9 * alpha))
            p.drawLine(QPointF(xa, ya), QPointF(T.lerp(xa, xb, f), T.lerp(ya, yb, f)))
        for (a, b), f in zip(G.LINKS, st["cross"]):
            if f > 0:
                (xa, ya, za), (xb, yb, zb) = projected[a], projected[b]
                pen = QPen(self.solid(pal.accent, 0.32 * f * self.depth_alpha((za + zb) / 2)), 1.0)
                pen.setDashPattern([2, 3])
                p.setPen(pen)
                p.drawLine(QPointF(xa, ya), QPointF(xb, yb))
        for k, f, strength in st["pulses"]:
            (xa, ya, za), (xb, yb, zb) = projected[G.PARENTS[k]], projected[k]
            point = QPointF(T.lerp(xa, xb, f), T.lerp(ya, yb, f))
            if strength > 0.55:
                self.glow(p, point, 11, bright, 0.75 * strength)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(faded(bright, strength))
            p.drawEllipse(point, 2.2, 2.2)
        order = sorted(range(G.NODE_COUNT), key=lambda k: projected[k][2])
        for k in order:
            f = st["nodes"][k]
            if f <= 0:
                continue
            x, y, z = projected[k]
            alpha = self.depth_alpha(z)
            radius = (3.0 + 2.6 * alpha) * f * (1.9 if k == 0 else 1.0)
            if alpha > 0.86:  # (the nearest ones: a soft halo)
                self.glow(p, QPointF(x, y), radius * 4.2, pal.accent, pal.glow * 1.6 * alpha * min(f, 1.0))
            self.dot(p, QPointF(x, y), radius, mix(pal.accent, bright, round(alpha * 6) / 6))
        if st["seed"] > 0:  # the seed: the agent's core, with a breathing four-pointed spark
            x, y, _ = projected[0]
            self.spark(p, QPointF(x, y), 16 * st["seed"] * (0.92 + 0.08 * st["breath"]))

    def dot(self, p, centre, radius, colour):
        """A round dot, from a small pixmap per colour and size (in half steps): the network has many."""
        radius = max(0.5, round(radius * 2) / 2)
        size = 2 * radius + 2

        def paint(q):
            q.setPen(Qt.PenStyle.NoPen)
            q.setBrush(colour)
            q.drawEllipse(QPointF(size / 2, size / 2), radius, radius)
        pixmap = self.sprite(("dot", colour.rgb(), radius), QSizeF(size, size), paint)
        self.blit(p, QRectF(centre.x() - size / 2, centre.y() - size / 2, size, size), pixmap)

    def spark(self, p, centre, r):
        pal = self.pal
        self.glow(p, centre, r * 3.4, pal.accent, pal.glow * 2.2)
        q = r * 0.18
        path = QPainterPath(centre + QPointF(0, -r))
        path.quadTo(centre + QPointF(q, -q), centre + QPointF(r, 0))
        path.quadTo(centre + QPointF(q, q), centre + QPointF(0, r))
        path.quadTo(centre + QPointF(-q, q), centre + QPointF(-r, 0))
        path.quadTo(centre + QPointF(-q, -q), centre + QPointF(0, -r))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix(pal.accent, QColor("#ffffff"), 0.6))
        p.drawPath(path)

    def paint_tendrils(self, p, cube, st):
        """The tendrils reaching for the walls (a bright tip; it draws back a little where a wall holds)."""
        pal = self.pal
        for tendril in st["tendrils"]:
            f = tendril["grow"] * (1 - tendril["back"])
            if f <= 0:
                continue
            xa, ya, za = G.project(G.NODES[tendril["node"]], cube)
            xb, yb, zb = G.project(tendril["end"], cube)
            tip = QPointF(T.lerp(xa, xb, f), T.lerp(ya, yb, f))
            alpha = self.depth_alpha((za + zb) / 2)
            colour = pal.ok if tendril["port"] else pal.accent
            path = QPainterPath(QPointF(xa, ya))
            path.lineTo(tip)
            p.setPen(stroke(self.solid(mix(pal.accent, colour, 0.5), 0.75 * alpha), 1.8))
            p.drawPath(path)
            self.glow(p, tip, 16, colour, 0.7 * alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(mix(colour, QColor("#ffffff"), 0.4))
            p.drawEllipse(tip, 3.0, 3.0)

    def signal_point(self, signal, cube):
        """Where SIGNAL is on the stage (inside: along the network to the port; outside: on its beam)."""
        if signal["u"] <= 1:
            f = signal["u"] if signal["state"] != "blocked" else 1 - 0.5 * signal["back"]
            x, y, _ = G.project(G.along(G.PATH, f), cube)
            return QPointF(x, y)
        return self.beam(signal["host"], cube).pointAtPercent(T.clamp(signal["u"] - 1))

    def beam(self, host, cube):
        index = [h for h, _, _ in G.SIGNALS].index(host)
        x, y, _ = G.project(G.on_face(G.PORT[0], G.PORT[1], G.PORT[2], -0.04), cube)
        start, end = QPointF(x, y), QPointF(G.CHIP[0] - 4, G.CHIP_Y[index])
        path = QPainterPath(start)
        path.cubicTo(start + QPointF(120, 0), end - QPointF(120, 0), end)
        return path

    def paint_signals_inside(self, p, cube, st):
        pal = self.pal
        for signal in st["signals"]:
            if signal["alpha"] <= 0 or signal["u"] > 1:
                continue
            colour = pal.bad if signal["state"] == "blocked" else pal.ok if signal["verdict"] == "allow" and \
                signal["u"] > 0.75 else mix(pal.accent, QColor("#ffffff"), 0.35)
            p.save()
            p.setOpacity(p.opacity() * signal["alpha"])
            trail = [self.signal_point(dict(signal, u=max(0.0, signal["u"] - 0.06 * k)), cube) for k in range(7)]
            for k in range(6):
                p.setPen(stroke(self.solid(colour, 0.6 * (1 - k / 6)), 4.5 * (1 - k / 8)))
                p.drawLine(trail[k], trail[k + 1])
            point = self.signal_point(signal, cube)
            self.glow(p, point, 24, colour, 0.75)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawEllipse(point, 5.0, 5.0)
            p.restore()

    def paint_port(self, p, quad, cube, st):
        """The port in the right wall: a ring with a slot, lit when its tendril arrives, flashing per signal."""
        pal = self.pal
        port = st["port"]
        if port["in"] <= 0:
            return
        colour = mix(pal.accent, getattr(pal, port["kind"]), port["flash"])
        x, y, _ = G.project(G.on_face(G.PORT[0], G.PORT[1], G.PORT[2]), cube)
        self.glow(p, QPointF(x, y), 46 + 30 * port["flash"], colour, (pal.glow * 1.4 + 0.5 * port["flash"]) *
                  port["in"])
        p.save()
        p.setTransform(self.face_transform(quad), True)
        centre, r = QPointF(G.PORT[1], G.PORT[2]), G.PORT_RADIUS
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(faded(self.pal.surface, 0.85 * port["in"]))
        p.drawEllipse(centre, r * port["in"], r * port["in"])
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self.solid(colour, port["in"]), 0.045))
        p.drawEllipse(centre, r * port["in"], r * port["in"])
        p.setPen(QPen(self.solid(colour, 0.5 * port["in"]), 0.02))
        p.drawEllipse(centre, r * 0.62 * port["in"], r * 0.62 * port["in"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(faded(colour, 0.9 * port["in"]))
        p.drawEllipse(centre, r * 0.22 * (1 + 0.4 * port["flash"]), r * 0.22 * (1 + 0.4 * port["flash"]))
        if port["reach"] > 0 and port["reach"] < 1.2:  # the tendril has arrived: a ring goes out once
            g = T.clamp(port["reach"])
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(faded(pal.ok, 0.7 * (1 - g)), 0.03))
            p.drawEllipse(centre, r * (1 + 1.4 * g), r * (1 + 1.4 * g))
        if port["flash"] > 0:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(faded(colour, 0.8 * port["flash"]), 0.035))
            rr = r * (1.1 + (1 - port["flash"]) * 1.3)
            p.drawEllipse(centre, rr, rr)
        p.restore()

    def paint_outside(self, p, L, cube, st, s):
        """The beams from the port to the hosts, the hosts with their badges, the signals outside, the port's
        name."""
        pal = self.pal
        shown = st["port"]["in"]
        if shown > 0:
            x, y = GlassLayout.port_label_at(cube)
            x0, y0 = GlassLayout.port_label_at(G.cube_state(5.0))
            p.save()
            p.setOpacity(p.opacity() * shown)
            self.texts(p, L, (("proxy", pal.muted),), dx=(x - x0) * L.s, dy=(y - y0) * L.s)
            p.restore()
        for index, signal in enumerate(st["signals"]):
            chip = st["chips"][index]
            if chip <= 0:
                continue
            p.save()
            p.setOpacity(p.opacity() * chip)
            path = partial(self.beam(signal["host"], cube), T.clamp(shown * chip), 40)
            colour, dashed = pal.line, False
            if signal["badge"] > 0:
                colour = mix(pal.line, pal.bad if signal["verdict"] == "block" else pal.ok, 0.85 * signal["badge"])
                dashed = signal["verdict"] == "block"
            pen = QPen(colour, 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            if dashed:
                pen.setDashPattern([2, 4])
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            self.paint_chip(p, L, index, signal, (1 - chip) * 20)
            p.restore()
        for signal in st["signals"]:
            if signal["alpha"] <= 0 or signal["u"] <= 1:
                continue
            p.save()
            p.setOpacity(p.opacity() * signal["alpha"])
            trail = [self.signal_point(dict(signal, u=max(1.0, signal["u"] - 0.05 * k)), cube) for k in range(7)]
            for k in range(6):
                p.setPen(stroke(self.solid(pal.ok, 0.55 * (1 - k / 6)), 4.5 * (1 - k / 8)))
                p.drawLine(trail[k], trail[k + 1])
            point = trail[0]
            self.glow(p, point, 24, pal.ok, 0.7)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.ok)
            p.drawEllipse(point, 5.0, 5.0)
            p.restore()

    def paint_chip(self, p, L, index, signal, dx):
        """Host INDEX: a glassy pill with its name and, once decided, a tick or a lock."""
        pal = self.pal
        cx, cw, ch = G.CHIP
        rect = QRectF(cx + dx, G.CHIP_Y[index] - ch / 2, cw, ch)
        if dx == 0:  # (still: from a pixmap made once)
            self.cached(p, ("glass-chip", index), rect.adjusted(-40, -40, 40, 40), lambda q: self.chip_body(q, L, index))
        else:
            self.chip_body(p, L, index, dx)
        f = signal["badge"]
        if f <= 0:
            return
        colour = pal.bad if signal["verdict"] == "block" else pal.ok
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(mix(pal.surface, colour, 0.6 * f), 1.4))
        p.drawRoundedRect(rect, ch / 2, ch / 2)
        centre = QPointF(rect.left() + 29, rect.center().y())
        radius = 12 * T.spring(f, 6, 1.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(colour)
        p.drawEllipse(centre, radius, radius)
        ink = pal.on_accent if theme.luminance(colour) < 0.5 else pal.bg
        if signal["verdict"] == "block":
            self.paint_lock(p, centre, radius / 13, ink)
        else:
            tick = QPainterPath(centre + QPointF(-5.2, 0.3))
            tick.lineTo(centre + QPointF(-1.5, 4.1))
            tick.lineTo(centre + QPointF(5.5, -4.0))
            p.setPen(stroke(ink, 2.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(partial(tick, T.clamp(f * 1.6), 16))

    def chip_body(self, p, L, index, dx=0.0):
        pal = self.pal
        cx, cw, ch = G.CHIP
        rect = QRectF(cx + dx, G.CHIP_Y[index] - ch / 2, cw, ch)
        self.shadow(p, rect, ch / 2, 0.6, spread=18, drop=6)
        fill = QLinearGradient(0, rect.top(), 0, rect.bottom())
        fill.setColorAt(0, mix(pal.surface, pal.accent, 0.06))
        fill.setColorAt(1, pal.surface)
        p.setBrush(fill)
        p.setPen(QPen(mix(pal.hairline, pal.accent, 0.3), 1.2))
        p.drawRoundedRect(rect, ch / 2, ch / 2)
        pen = QPen(pal.line, 1.5)
        pen.setDashPattern([2, 3])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(rect.left() + 29, rect.center().y()), 12, 12)
        self.texts(p, L, ((f"host{index}", pal.fg),), dx=dx * L.s)

