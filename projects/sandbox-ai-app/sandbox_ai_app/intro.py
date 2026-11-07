"""The intro animation at the app's start (intro_timeline.py tells the story; this draws it).

An overlay over the main window, drawn with QPainter (no images, nothing fetched) in the colours and the font of
the theme in use. Shapes are drawn on the timeline's stage, scaled into the window; texts are laid out in
pixels (Layout), each fitted into its box, so that they stay sharp and inside the window at every size. The
background is painted once per size into pixmaps (HiDPI aware); a frame only draws what moves.

A click or any key (Esc, Space, ...) skips it, and so does the Skip button. The "intro" setting (Settings,
Appearance) or $SANDBOX_AI_APP_NO_INTRO=1 turns it off; $SANDBOX_AI_APP_REDUCED_MOTION=1 shows a short static
title card instead. A drawing error ends it: it never stands between the user and the app.
"""
from __future__ import annotations

import math
import os

from PySide6.QtCore import QElapsedTimer, QEvent, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import QApplication, QWidget

from . import applog, intro_timeline as T, theme
from .i18n import tr

AGENTS_LINE = "Claude Code  ·  Codex  ·  OpenCode"  # names: the same in every language
WORDMARK = ("sandbox", "-ai")
ON = ("1", "true", "yes", "on")


def wanted(settings, environ=None):
    """Play the intro: the setting is on, and $SANDBOX_AI_APP_NO_INTRO is not set."""
    environ = os.environ if environ is None else environ
    return bool(settings.get("intro", True)) and not environ.get("SANDBOX_AI_APP_NO_INTRO")


def reduced_motion(settings=None, environ=None):
    """Reduced motion: $SANDBOX_AI_APP_REDUCED_MOTION=1 (or a "reduced_motion" setting) asks for the static card."""
    environ = os.environ if environ is None else environ
    return environ.get("SANDBOX_AI_APP_REDUCED_MOTION", "").strip().lower() in ON or \
        bool((settings or {}).get("reduced_motion", False))


def captions():
    """The three beats' captions."""
    return (tr("Your AI agent works inside a sandbox."),
            tr("Every connection goes through the proxy and its allow-list."),
            tr("Anything else is blocked, or waits for your approval."))


def tagline():
    return tr("Let AI agents work freely. Your machine stays protected.")


# -- colours --

def mix(a, b, f):
    a, b = QColor(a), QColor(b)
    return QColor.fromRgbF(*(x + (y - x) * f for x, y in zip(a.getRgbF()[:3], b.getRgbF()[:3])))


def faded(color, alpha):
    color = QColor(color)
    color.setAlphaF(T.clamp(alpha))
    return color


class Palette:
    """The intro's few colours, taken from theme MODE (default: the one in use)."""

    def __init__(self, mode=None):
        data = theme.THEMES_DATA.get(mode) if mode else None
        data = data or theme.data()
        c, status = data["colors"], data["status"]
        self.dark = bool(data["dark"])
        black, white = QColor("#000000"), QColor("#ffffff")
        self.bg = QColor(c["window"])
        self.surface = QColor(c["base"])
        if self.dark:
            self.surface = mix(self.surface, white, 0.025)
        self.fg = QColor(c["title"])
        self.muted = QColor(c["secondary"])
        accent = c["accent"] if theme.contrast(c["accent"], c["window"]) >= 2.2 else c["title"]
        self.accent = QColor(accent)
        self.accent_deep = mix(self.accent, black, 0.28 if self.dark else 0.18)
        self.on_accent = white if theme.luminance(accent) < 0.5 else QColor(c["accent_text"])
        self.ok, self.warn, self.bad = QColor(status["ok"]), QColor(status["warn"]), QColor(status["bad"])
        self.line = mix(self.bg, self.fg, 0.17 if self.dark else 0.14)
        self.hairline = mix(self.bg, self.fg, 0.10 if self.dark else 0.09)
        self.editor = mix(self.surface, black, 0.22) if self.dark else mix(self.surface, self.bg, 0.7)
        self.shadow = QColor(0, 0, 0, 120 if self.dark else 30)
        self.glow = 0.30 if self.dark else 0.16  # how strongly the accent glows


# -- layout: every text's box, in pixels --

class Text:
    """One text: what, in which font, and the rectangle it takes (pixels, in the window)."""

    def __init__(self, text, font, rect, align):
        self.text, self.font, self.rect, self.align = text, font, rect, align


class Layout:
    """The stage's scale and offset in a WIDTH x HEIGHT window, and every text fitted into its box."""

    def __init__(self, width, height, font=None):
        self.width, self.height = float(width), float(height)
        self.s = min(self.width / T.WIDTH, self.height / T.HEIGHT)
        self.ox = (self.width - T.WIDTH * self.s) / 2
        self.oy = (self.height - T.HEIGHT * self.s) / 2
        self.base = QFont(font or theme.font() or QApplication.font())
        self.mono = QFont(self.base)
        self.mono.setFamilies(list(theme.MONO))
        self.mono.setStyleHint(QFont.StyleHint.Monospace)
        self.texts = {}
        self.build()

    def rect(self, x, y, w, h):
        """Stage units to window pixels."""
        return QRectF(self.ox + x * self.s, self.oy + y * self.s, w * self.s, h * self.s)

    def centred(self, cx, cy, w, h):
        return self.rect(cx - w / 2, cy - h / 2, w, h)

    def fit(self, text, px, box, weight=QFont.Weight.Normal, mono=False, spacing=0.0, least=0.0):
        """The font (PX pixels at most, in stage units, but LEAST pixels when that fits) in which TEXT fits BOX;
        its width and height."""
        font = QFont(self.mono if mono else self.base)
        font.setWeight(weight)
        size = max(px * self.s, least)
        while True:
            font.setPixelSize(max(1, round(size)))
            if spacing:
                font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing * font.pixelSize())
            metrics = QFontMetricsF(font)
            width, height = metrics.horizontalAdvance(text), metrics.height()
            if (width <= box.width() and height <= box.height()) or font.pixelSize() <= 6:
                return font, width, height
            size = min(size * 0.94, font.pixelSize() - 0.5)

    def add(self, name, text, px, box, align=Qt.AlignmentFlag.AlignCenter, **style):
        font, width, height = self.fit(text, px, box, **style)
        if align & Qt.AlignmentFlag.AlignLeft:
            x = box.left()
        elif align & Qt.AlignmentFlag.AlignRight:
            x = box.right() - width
        else:
            x = box.center().x() - width / 2
        rect = QRectF(x, box.center().y() - height / 2, width, height)
        self.texts[name] = Text(text, font, rect, align)
        return self.texts[name]

    def build(self):
        bold, semi, medium = QFont.Weight.Bold, QFont.Weight.DemiBold, QFont.Weight.Medium
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        px, py, pw, _ = T.PANEL
        self.add("header", tr("Sandbox"), 22, self.rect(px + 72, py + 14, 260, T.HEADER - 28), left, weight=semi,
                 least=12)
        ax, ay, ar = T.AGENT
        self.add("agent", tr("Agent"), 20, self.centred(ax, ay + ar + 34, 200, 30), weight=medium, least=11)
        gx, gy = T.GATE
        self.add("proxy", tr("Proxy"), 19, self.centred(gx, gy + T.GATE_SIZE[1] / 2 + 26, 160, 28), weight=medium,
                 least=11)
        lx, ly, lw, _ = T.ALLOW
        self.add("allowlist", tr("Allow-list"), 17, self.rect(lx + 18, ly + 12, lw - 36, 26), left, weight=semi,
                 least=10)
        allowed = [host for host, verdict, _ in T.REQUESTS if verdict == "allow"]
        for k, host in enumerate(allowed):
            self.add(f"allow{k}", host, 18, self.rect(lx + 48, ly + 48 + k * 40, lw - 66, 28), left, mono=True,
                     least=10)
        cx, cw, ch = T.CHIP
        for k, (host, _, _) in enumerate(T.REQUESTS):
            self.add(f"host{k}", host, 21, self.rect(cx + 62, T.CHIP_Y[k] - ch / 2 + 12, cw - 80, ch - 24), left,
                     mono=True, least=11)
        x, y, w, h = T.CARD
        host = T.REQUESTS[T.approval_state(0)["index"]][0]
        self.add("ask", tr("Allow {host}?", host=host), 20, self.rect(x + 66, y + 12, w - 84, 30), left, weight=semi,
                 least=11)
        self.add("deny", tr("Deny"), 16, self.rect(*self.button(0)), weight=medium, least=10)
        self.add("allow", tr("Allow"), 16, self.rect(*self.button(1)), weight=semi, least=10)
        for k, text in enumerate(captions()):
            self.add(f"caption{k}", text, 31, self.centred(T.WIDTH / 2, T.CAPTION_Y, 1240, 50), weight=medium,
                     least=14)
        self.add("word", "".join(WORDMARK), 120, self.centred(T.WIDTH / 2, T.TITLE_Y[0], 1100, 140), weight=bold,
                 spacing=-0.02)
        self.add("tag", tagline(), 33, self.centred(T.WIDTH / 2, T.TITLE_Y[1], 1240, 50), least=14)
        self.add("agents", AGENTS_LINE, 22, self.centred(T.WIDTH / 2, T.TITLE_Y[2], 1000, 34), weight=medium,
                 spacing=0.04, least=12)
        # The Skip button, in the window's bottom right corner (outside the stage when there is room).
        size = max(12.0, 15.0 * max(self.s, 0.8))
        margin = max(12.0, 22 * self.s)
        font, width, height = self.fit(tr("Skip"), size / self.s, QRectF(0, 0, 400 * self.s, 200 * self.s),
                                       weight=medium)
        key, key_width, _ = self.fit("Esc", size * 0.8 / self.s, QRectF(0, 0, 200 * self.s, 200 * self.s),
                                     weight=medium)
        pad = height * 0.75
        pill = QRectF(0, 0, pad + width + pad * 0.6 + key_width + pad * 0.9 + pad * 0.55, height * 2.0)
        pill.moveBottomRight(QPointF(self.width - margin, self.height - margin))
        self.skip = pill
        self.texts["skip"] = Text(tr("Skip"), font, QRectF(pill.left() + pad, pill.center().y() - height / 2, width,
                                                         height), left)
        cap = QRectF(self.texts["skip"].rect.right() + pad * 0.6, pill.top() + pill.height() * 0.22,
                     key_width + pad * 0.9, pill.height() * 0.56)
        self.texts["esc"] = Text("Esc", key, cap, Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def button(which):
        """Stage rect of the approval card's Deny (0) or Allow (1) button."""
        x, y, w, h = T.CARD
        return (x + w - 16 - 112 - (1 - which) * 122, y + h - 16 - 32, 112, 32)

    # Texts shown together (for the tests: none may overlap another of its group).
    GROUPS = (("header", "agent", "proxy", "allowlist", "allow0", "allow1", "host0", "host1", "host2", "host3", "ask",
               "deny", "allow", "caption0", "skip", "esc"),
              ("word", "tag", "agents", "skip", "esc"))


# -- drawing --

def partial(path, f, steps=96):
    """The first F (0..1) of PATH, as a polyline."""
    if f >= 1:
        return path
    result = QPainterPath()
    if f <= 0:
        return result
    count = max(2, int(steps * f))
    result.moveTo(path.pointAtPercent(0))
    for k in range(1, count + 1):
        result.lineTo(path.pointAtPercent(f * k / count))
    return result


def mark_paths(cx, cy, size):
    """The mark: a rounded square, a shield and a tick (as in the app's icon), centred at CX, CY."""
    k = size / 60.0

    def at(x, y):
        return QPointF(cx + (x - 32) * k, cy + (y - 32) * k)
    half, radius = size / 2, size * 0.235
    square = QPainterPath()  # from the top centre, clockwise
    square.moveTo(cx, cy - half)
    square.lineTo(cx + half - radius, cy - half)
    square.arcTo(QRectF(cx + half - 2 * radius, cy - half, 2 * radius, 2 * radius), 90, -90)
    square.lineTo(cx + half, cy + half - radius)
    square.arcTo(QRectF(cx + half - 2 * radius, cy + half - 2 * radius, 2 * radius, 2 * radius), 0, -90)
    square.lineTo(cx - half + radius, cy + half)
    square.arcTo(QRectF(cx - half, cy + half - 2 * radius, 2 * radius, 2 * radius), 270, -90)
    square.lineTo(cx - half, cy - half + radius)
    square.arcTo(QRectF(cx - half, cy - half, 2 * radius, 2 * radius), 180, -90)
    square.closeSubpath()
    shield = QPainterPath()
    shield.moveTo(at(32, 12))
    shield.lineTo(at(48, 17.5))
    shield.cubicTo(at(48, 34), at(42.5, 45), at(32, 52))
    shield.cubicTo(at(21.5, 45), at(16, 34), at(16, 17.5))
    shield.closeSubpath()
    tick = QPainterPath()
    tick.moveTo(at(25, 31.5))
    tick.lineTo(at(30, 37))
    tick.lineTo(at(39.5, 26))
    return square, shield, tick


def stroke(color, width, cap=Qt.PenCapStyle.RoundCap):
    pen = QPen(color, width)
    pen.setCapStyle(cap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def glow(p, centre, radius, color, strength):
    if strength <= 0.01 or radius <= 0:
        return
    gradient = QRadialGradient(centre, radius)
    gradient.setColorAt(0, faded(color, strength))
    gradient.setColorAt(0.45, faded(color, strength * 0.35))
    gradient.setColorAt(1, faded(color, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(gradient)
    p.drawEllipse(centre, radius, radius)


def soft_shadow(p, rect, radius, pal, strength=1.0, spread=26.0, drop=10.0):
    """A soft shadow under ROUNDED RECT, from a few widening layers."""
    p.setPen(Qt.PenStyle.NoPen)
    layers = 10  # stacked, the layers fall off smoothly: dense near the shape, thin at the edge
    p.setBrush(faded(pal.shadow, pal.shadow.alphaF() * strength * 1.6 / layers))
    for k in range(layers):
        grow = spread * (k + 1) / layers - spread * 0.35
        r = rect.adjusted(-grow, -grow + drop * 0.6, grow, grow + drop)
        p.drawRoundedRect(r, max(1.0, radius + grow), max(1.0, radius + grow))


_grain = []


def grain():
    """A small tile of noise (made once)."""
    if not _grain:
        import random
        rng = random.Random(7)
        image = QImage(96, 96, QImage.Format.Format_ARGB32_Premultiplied)
        for y in range(96):
            for x in range(96):
                v = rng.randrange(256)
                image.setPixel(x, y, (255 << 24) | (v << 16) | (v << 8) | v)
        _grain.append(QPixmap.fromImage(image))
    return _grain[0]


def draw_text(p, item, color, dx=0.0, dy=0.0):
    p.setFont(item.font)
    p.setPen(color)
    p.drawText(item.rect.translated(dx, dy), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextDontClip, item.text)


class Renderer:
    """Draws scenes for one palette; keeps the background of the last size in pixmaps."""

    def __init__(self, mode=None, font=None):
        self.pal = Palette(mode)
        self.font = font
        self.key = None
        self.layout = self.background = None
        self.layer = {}  # buffers for the moments something fades as one (released after them)
        self.sprites, self.scale, self.ratio = {}, 1.0, 1.0

    def prepare(self, width, height, ratio=1.0):
        key = (int(width), int(height), round(ratio, 3))
        if key != self.key:
            self.key = key
            self.layout = Layout(width, height, self.font)
            self.scale, self.ratio = self.layout.s * ratio, ratio
            self.sprites = {}
            self.background = self.paint_layer(width, height, ratio, self.paint_background)
            self.layer = {}
        return self.layout

    # -- sprites: glows and shadows, painted once at the window's resolution and reused --

    def sprite(self, key, size, draw):
        """The pixmap KEY of SIZE (stage units) at the current resolution, painted by DRAW(painter) once."""
        found = self.sprites.get(key)
        if found is None:
            if len(self.sprites) > 96:
                self.sprites.clear()
            scale = self.scale
            found = QPixmap(QSize(max(1, math.ceil(size.width() * scale)), max(1, math.ceil(size.height() * scale))))
            found.fill(Qt.GlobalColor.transparent)
            painter = QPainter(found)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.scale(scale, scale)
                draw(painter)
            finally:
                painter.end()
            self.sprites[key] = found
        return found

    def glow(self, p, centre, radius, color, strength):
        """A soft radial glow of COLOR around CENTRE (stage units), STRENGTH its opacity."""
        if strength <= 0.01 or radius <= 0:
            return
        color = QColor(color)
        step = max(1, round(radius * self.scale / 6))  # sizes in steps: a few sprites serve every frame
        nominal = max(4.0, round(radius * self.scale / step) * step / self.scale)
        pixmap = self.sprite(("glow", color.rgb(), round(nominal * self.scale)), QSizeF(2 * nominal, 2 * nominal),
                             lambda q: glow(q, QPointF(nominal, nominal), nominal, color, 1.0))
        p.save()
        p.setOpacity(p.opacity() * T.clamp(strength))
        self.blit(p, QRectF(centre.x() - nominal, centre.y() - nominal, 2 * nominal, 2 * nominal), pixmap)
        p.restore()

    def shadow(self, p, rect, radius, strength=1.0, spread=26.0, drop=10.0, size=None):
        """A soft shadow under the rounded RECT; SIZE (default: RECT's) is the shape it is made for, then
        stretched to RECT (the sandbox while it opens)."""
        size = size or rect.size()
        margin = spread + drop + 2
        outer = QSizeF(size.width() + 2 * margin, size.height() + 2 * margin)
        key = ("shadow", round(size.width(), 1), round(size.height(), 1), radius, strength, spread, drop)
        pixmap = self.sprite(key, outer, lambda q: soft_shadow(q, QRectF(margin, margin, size.width(), size.height()),
                                                               radius, self.pal, strength, spread, drop))
        sx, sy = rect.width() / size.width(), rect.height() / size.height()
        target = QRectF(rect.left() - margin * sx, rect.top() - margin * sy, outer.width() * sx, outer.height() * sy)
        self.blit(p, target, pixmap)

    def blit(self, p, target, pixmap):
        """PIXMAP into TARGET (in P's units): pixel for pixel when it has that size there (the fast way),
        else scaled smoothly."""
        transform = p.worldTransform()
        simple = (QTransform.TransformationType.TxNone, QTransform.TransformationType.TxTranslate,
                  QTransform.TransformationType.TxScale)
        if transform.type() in simple:
            mapped = transform.mapRect(target)
            ratio = self.ratio
            if abs(mapped.width() * ratio - pixmap.width()) <= 1.01 and \
                    abs(mapped.height() * ratio - pixmap.height()) <= 1.01:
                p.save()
                p.resetTransform()
                pixmap.setDevicePixelRatio(ratio)
                p.drawPixmap(QPointF(round(mapped.x() * ratio) / ratio, round(mapped.y() * ratio) / ratio), pixmap)
                pixmap.setDevicePixelRatio(1.0)
                p.restore()
                return
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        p.restore()

    @staticmethod
    def paint_layer(width, height, ratio, draw):
        pixmap = QPixmap(QSize(max(1, round(width * ratio)), max(1, round(height * ratio))))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            draw(painter, width, height)
        finally:
            painter.end()
        return pixmap

    def paint_background(self, p, width, height):
        pal, L = self.pal, self.layout
        top = mix(pal.bg, pal.accent, 0.05 if pal.dark else 0.025)
        bottom = mix(pal.bg, QColor("#000000"), 0.28) if pal.dark else mix(pal.bg, pal.fg, 0.025)
        gradient = QLinearGradient(0, 0, 0, height)
        gradient.setColorAt(0, top)
        gradient.setColorAt(1, bottom)
        p.fillRect(QRectF(0, 0, width, height), gradient)
        centre = QPointF(width / 2, L.oy + 300 * L.s)
        glow(p, centre, max(width, height) * 0.62, pal.accent, 0.10 if pal.dark else 0.06)
        vignette = QRadialGradient(QPointF(width / 2, height / 2), math.hypot(width, height) * 0.62)
        vignette.setColorAt(0.55, QColor(0, 0, 0, 0))
        vignette.setColorAt(1, QColor(0, 0, 0, 90 if pal.dark else 14))
        p.fillRect(QRectF(0, 0, width, height), vignette)
        self.paint_grid(p, width, height)
        p.setOpacity(0.022 if pal.dark else 0.018)  # a fine grain: no banding in the soft gradients
        p.drawTiledPixmap(QRectF(0, 0, width, height), grain())
        p.setOpacity(1.0)

    def paint_grid(self, p, width, height):
        """A fine dot grid, strongest in the middle."""
        pal, L = self.pal, self.layout
        step = 40 * L.s
        if step < 8:
            return
        dot = max(0.9, 1.25 * L.s)
        cx, cy = width / 2, height / 2
        reach = math.hypot(width, height) * 0.55
        p.setPen(Qt.PenStyle.NoPen)
        base = 0.16 if pal.dark else 0.13
        x0 = (cx % step) - step
        y0 = (cy % step) - step
        y = y0
        while y < height + step:
            x = x0
            while x < width + step:
                strength = max(0.0, 1 - math.hypot(x - cx, y - cy) / reach) ** 1.3
                if strength > 0.02:
                    p.setBrush(faded(pal.fg, base * strength))
                    p.drawEllipse(QPointF(x, y), dot, dot)
                x += step
            y += step

    # -- a frame --

    def paint(self, p, width, height, s, ratio=1.0):
        """Draw scene S (intro_timeline.scene) into a WIDTH x HEIGHT area of painter P."""
        L = self.prepare(width, height, ratio)
        base = s["fade"] * s["enter"]
        if base <= 0.002:
            return
        if base < 0.998:  # fading in or out: drawn as one layer, so that its parts do not show through
            frame = self.buffer("frame", ratio)
            painter = QPainter(frame)
            try:
                self.paint_frame(painter, L, s, ratio)
            finally:
                painter.end()
            p.save()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.setOpacity(base)
            if s["fade"] < 1:  # ... and dissolves into the app, growing a little
                k = 1 + 0.025 * (1 - s["fade"])
                p.translate(width / 2, height / 2)
                p.scale(k, k)
                p.translate(-width / 2, -height / 2)
            p.drawPixmap(0, 0, frame)
            p.restore()
        else:
            self.layer.pop("frame", None)
            self.paint_frame(p, L, s, ratio)

    def buffer(self, name, ratio):
        """A transparent pixmap of the window's size, kept for the next frames."""
        if name not in self.layer:
            self.layer[name] = QPixmap(self.background.size())
            self.layer[name].setDevicePixelRatio(ratio)
        self.layer[name].fill(Qt.GlobalColor.transparent)
        return self.layer[name]

    def paint_frame(self, p, L, s, ratio):
        width, height = L.width, L.height
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(0, 0, self.background)
        scene = 1 - s["recede"]
        if not 0 < s["recede"] < 1:
            self.layer.pop("story", None)
        if scene > 0.003 and not s["reduced"]:
            # While it recedes, the story is drawn into a layer that fades as one.
            target = p
            if s["recede"] > 0:
                target = QPainter(self.buffer("story", ratio))
                target.setRenderHint(QPainter.RenderHint.Antialiasing)
                target.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            try:
                target.save()
                if s["recede"] > 0:
                    k = 1 - 0.035 * s["recede"]
                    centre = QPointF(width / 2, height / 2)
                    target.translate(centre)
                    target.scale(k, k)
                    target.translate(-centre - QPointF(0, 14 * L.s * s["recede"]))
                self.paint_story(target, L, s)
                target.restore()
            finally:
                if target is not p:
                    target.end()
            if target is not p:
                p.setOpacity(scene)
                p.drawPixmap(0, 0, self.layer["story"])
        p.setOpacity(1.0)
        if s["recede"] > 0:
            self.paint_title(p, L, s)
        self.paint_mark(p, L, s["mark"], s)
        self.paint_chrome(p, L, s, 1.0)
        p.restore()

    def stage(self, p, L):
        p.save()
        p.translate(L.ox, L.oy)
        p.scale(L.s, L.s)

    def paint_mark(self, p, L, m, s):
        pal = self.pal
        if m["square"] <= 0:
            return
        self.stage(p, L)
        cx, cy, size = m["x"], m["y"], m["size"]
        square, shield, tick = mark_paths(cx, cy, size)
        width = max(size * 0.045, 1.6)
        fill = m["fill"]
        self.glow(p, QPointF(cx, cy), size * 1.25, pal.accent, pal.glow * (0.8 * m["glow"] + 0.55 * s["recede"] *
                                                                      (1 - m["header"])))
        if fill < 1:  # the lines, drawing themselves
            p.save()
            p.setOpacity(p.opacity() * (1 - fill))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(stroke(pal.accent, width))
            p.drawPath(partial(square, m["square"]))
            p.setPen(stroke(pal.fg, width))
            p.drawPath(partial(shield, m["shield"]))
            p.setPen(stroke(pal.accent, width * 1.15))
            p.drawPath(partial(tick, m["tick"], 24))
            p.restore()
        if fill > 0:  # ... and the finished mark
            p.save()
            p.setOpacity(p.opacity() * fill)
            gradient = QLinearGradient(cx, cy - size / 2, cx, cy + size / 2)
            gradient.setColorAt(0, mix(pal.accent, QColor("#ffffff"), 0.12))
            gradient.setColorAt(1, pal.accent_deep)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gradient)
            p.drawPath(square)
            p.setBrush(pal.on_accent)
            p.drawPath(shield)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(stroke(pal.accent_deep, max(size * 0.065, 1.6)))
            p.drawPath(tick)
            if 0 < m["sweep"] < 1:  # a soft light passing over it
                p.setClipPath(square)
                x = cx - size * 1.2 + size * 2.4 * T.ease_in_out_cubic(m["sweep"])
                sweep = QLinearGradient(x - size * 0.35, cy - size / 2, x + size * 0.35, cy + size / 2)
                sweep.setColorAt(0, QColor(255, 255, 255, 0))
                sweep.setColorAt(0.5, QColor(255, 255, 255, 70))
                sweep.setColorAt(1, QColor(255, 255, 255, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.fillRect(QRectF(cx - size / 2, cy - size / 2, size, size), sweep)
            p.restore()
        if 0 < m["ring"] < 1:  # ... and a ring going out when it is complete
            f = T.ease_out_cubic(m["ring"])
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(faded(pal.accent, 0.5 * (1 - f)), max(1.2, size * 0.012)))
            r = size * (0.6 + 0.55 * f)
            p.drawRoundedRect(QRectF(cx - r, cy - r, 2 * r, 2 * r), r * 0.47, r * 0.47)
        p.restore()

    def cached(self, p, key, area, draw):
        """What DRAW(painter, in stage units) paints in AREA, from a pixmap made once (for the parts that
        stand still)."""
        def paint(q):
            q.translate(-area.left(), -area.top())
            draw(q)
        pixmap = self.sprite(key, area.size(), paint)
        self.blit(p, QRectF(area.topLeft(), QSizeF(pixmap.width() / self.scale, pixmap.height() / self.scale)), pixmap)

    @staticmethod
    def texts(p, L, items, dx=0.0):
        """Layout texts ((name, colour), ...) on P, which is in stage units."""
        p.save()
        p.scale(1 / L.s, 1 / L.s)
        p.translate(-L.ox, -L.oy)
        for name, colour in items:
            draw_text(p, L.texts[name], colour, dx=dx)
        p.restore()

    def paint_story(self, p, L, s):
        """The sandbox, the agent, its editor and allow-list, the gate, the destinations and the requests."""
        panel = s["panel"]
        if panel["alpha"] <= 0:
            self.paint_caption(p, L, s)
            return
        p.save()
        p.translate(s["pan"] * L.s, 0)  # the whole world: shapes and texts
        self.stage(p, L)
        px, py, pw, ph = T.PANEL
        mx, my, msize = T.MARK_BIG
        f = panel["open"]
        inside = T.clamp((f - 0.82) / 0.18) * panel["alpha"]
        labels = T.clamp(s["agent"]["label"]) * s["code"]["alpha"]
        if f >= 1 and panel["alpha"] >= 1 and labels >= 1:  # it stands still from here on
            margin = 60
            area = QRectF(px - margin, py - margin, pw + 2 * margin, ph + 2 * margin)
            self.cached(p, ("panel",), area, lambda q: self.paint_panel(q, L, QRectF(px, py, pw, ph), 26, 1.0, 1.0))
        else:
            start = QRectF(mx - s["pan"] - msize / 2, my - msize / 2, msize, msize)  # it grows out of the mark
            rect = QRectF(T.lerp(start.left(), px, f), T.lerp(start.top(), py, f), T.lerp(msize, pw, f),
                          T.lerp(msize, ph, f))
            p.save()
            p.setOpacity(p.opacity() * panel["alpha"])
            self.paint_panel(p, L, rect, T.lerp(msize * 0.235, 26, f), T.clamp((f - 0.82) / 0.18), labels)
            p.restore()
        if inside > 0:
            p.save()
            p.setOpacity(p.opacity() * inside)
            self.paint_editor(p, s)
            self.paint_allow_hits(p, s)
            self.paint_lane(p, s)
            self.paint_agent(p, s)
            p.restore()
        self.paint_routes(p, s)
        self.paint_chips(p, L, s)
        self.paint_gate(p, L, s)
        self.paint_requests(p, s)
        self.paint_card(p, L, s)
        p.restore()
        p.restore()
        self.paint_caption(p, L, s)

    def paint_panel(self, p, L, rect, radius, inside, labels):
        """The sandbox's still parts: its body and header, the editor's and the allow-list's boxes, their
        labels. INSIDE and LABELS (0..1) fade the contents and the labels in."""
        pal = self.pal
        self.shadow(p, rect, 26, 1.0, spread=40, drop=14, size=QSizeF(T.PANEL[2], T.PANEL[3]))
        fill = QLinearGradient(0, rect.top(), 0, rect.bottom())
        fill.setColorAt(0, mix(pal.surface, QColor("#ffffff"), 0.03 if pal.dark else 0))
        fill.setColorAt(1, pal.surface)
        p.setBrush(fill)
        p.setPen(QPen(pal.hairline, 1.4))
        p.drawRoundedRect(rect, radius, radius)
        if inside <= 0:
            return
        p.save()
        p.setOpacity(p.opacity() * inside)
        p.setPen(QPen(pal.hairline, 1.2))
        p.drawLine(QPointF(rect.left() + 1, rect.top() + T.HEADER), QPointF(rect.right() - 1, rect.top() + T.HEADER))
        dot = QPointF(rect.right() - 40, rect.top() + T.HEADER / 2)  # a calm status light: isolated
        self.glow(p, dot, 16, pal.ok, 0.35)
        p.setBrush(pal.ok)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(dot, 5, 5)
        for x, y, w, h in (T.CODE, T.ALLOW):
            p.setBrush(pal.editor)
            p.setPen(QPen(pal.hairline, 1.0))
            p.drawRoundedRect(QRectF(x, y, w, h), 14, 14)
        self.texts(p, L, (("header", pal.fg),))
        if labels > 0:
            p.setOpacity(p.opacity() * labels)
            x, y, w, h = T.ALLOW
            for k in range(2):  # the allow-list's entries, each with a tick
                centre = QPointF(x + 30, y + 62 + k * 40)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(faded(pal.ok, 0.18))
                p.drawEllipse(centre, 9, 9)
                p.setPen(stroke(pal.ok, 2.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                tick = QPainterPath(centre + QPointF(-4, 0.2))
                tick.lineTo(centre + QPointF(-1.2, 3))
                tick.lineTo(centre + QPointF(4.2, -3))
                p.drawPath(tick)
            self.texts(p, L, (("agent", pal.muted), ("allowlist", pal.muted), ("allow0", pal.fg), ("allow1", pal.fg)))
        p.restore()

    def paint_editor(self, p, s):
        pal = self.pal
        area = QRectF(*T.CODE)  # its box: paint_panel
        code = s["code"]
        if code["alpha"] <= 0:
            return
        p.save()
        p.setOpacity(p.opacity() * code["alpha"])
        inner = area.adjusted(18, 10, -18, -10)
        clip = QPainterPath()
        clip.addRoundedRect(area.adjusted(1, 1, -1, -1), 13, 13)
        p.setClipPath(clip)
        step = inner.height() / T.CODE_VISIBLE
        done = code["lines"]
        scroll = max(0.0, done + 1 - T.CODE_VISIBLE) * step
        colours = (pal.accent, pal.ok, pal.warn, pal.muted)
        p.setPen(Qt.PenStyle.NoPen)
        first = max(0, int(scroll / step) - 1)
        caret = None
        for line in range(first, int(done) + 1):
            typed = T.clamp(done - line)
            if typed <= 0:
                break
            top = inner.top() + step / 2 - 4.5 + line * step - scroll
            seed = (line * 2654435761) & 0xFFFFFFFF
            indent = 30 + (0, 1, 2, 2, 1, 2, 3, 2, 1, 0)[line % 10] * 22
            tokens = 1 + seed % 3
            p.setBrush(pal.line)  # the gutter's line number
            p.drawRoundedRect(QRectF(inner.left(), top + 1.5, 8 + (line % 3) * 3, 6), 3, 3)
            xx = inner.left() + indent
            total = 0.0
            widths = []
            for t in range(tokens):
                widths.append(26 + ((seed >> (t * 7)) % 90))
                total += widths[-1] + 10
            shown = total * typed
            for t, width in enumerate(widths):
                visible = min(width, shown)
                if visible <= 0:
                    break
                colour = colours[(seed >> (t * 5 + 3)) % 4] if t else pal.accent if line % 3 else pal.muted
                p.setBrush(faded(colour, 0.85 if t else 0.95))
                p.drawRoundedRect(QRectF(xx, top, visible, 9), 4.5, 4.5)
                xx += width + 10
                shown -= width + 10
            if typed < 1:
                caret = QPointF(inner.left() + indent + total * typed + 3, top - 4)
        if caret is not None and code["caret"]:
            p.setBrush(pal.accent)
            p.drawRoundedRect(QRectF(caret.x(), caret.y(), 2.5, 17), 1, 1)
        fade = QLinearGradient(0, area.top(), 0, area.top() + 26)  # lines scroll out softly at the top
        fade.setColorAt(0, pal.editor)
        fade.setColorAt(1, faded(pal.editor, 0))
        p.fillRect(QRectF(area.left() + 1.5, area.top() + 1.5, area.width() - 3, 26), fade)
        p.restore()

    def paint_allow_hits(self, p, s):
        """An entry of the allow-list lights up while the proxy lets its request pass."""
        pal = self.pal
        x, y, w, h = T.ALLOW
        allowed = [i for i, r in enumerate(T.REQUESTS) if r[1] == "allow"]
        for k, index in enumerate(allowed):
            if not s["requests"]:
                return
            request = s["requests"][index]
            since = s["t"] - request["at_gate"]
            if 0 <= since < 0.6:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(faded(pal.ok, 0.16 * (1 - since / 0.6)))
                p.drawRoundedRect(QRectF(x + 10, y + 46 + k * 40, w - 20, 32), 8, 8)

    def paint_lane(self, p, s):
        """The way out: from the agent to the gate, a faint dashed line."""
        pal = self.pal
        if s["gate"]["in"] <= 0:
            return
        ax, ay, ar = T.AGENT
        gx, gy = T.GATE
        pen = QPen(faded(pal.line, 0.9 * T.clamp(s["gate"]["in"])), 1.6)
        pen.setDashPattern([3, 4])
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(ax + ar + 14, ay), QPointF(gx - T.GATE_SIZE[0] / 2 - 4, gy))

    def paint_agent(self, p, s):
        """The agent: a core with a spark, a ring and three orbiting dots."""
        pal = self.pal
        a = s["agent"]
        if a["in"] <= 0:
            return
        ax, ay, ar = T.AGENT
        centre = QPointF(ax, ay)
        p.save()
        p.translate(centre)
        p.scale(a["in"], a["in"])
        self.glow(p, QPointF(0, 0), ar * 2.0, pal.accent, pal.glow * (0.6 + 0.4 * a["pulse"]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(faded(pal.accent, 0.35), 1.6))
        p.drawEllipse(QPointF(0, 0), ar, ar)
        p.setPen(QPen(faded(pal.accent, 0.18), 1.2))
        p.drawEllipse(QPointF(0, 0), ar * 0.78, ar * 0.78)
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(3):
            angle = a["spin"] + k * 2 * math.pi / 3
            p.setBrush(faded(pal.accent, 1.0 - k * 0.25))
            p.drawEllipse(QPointF(math.cos(angle) * ar, math.sin(angle) * ar), 4.6 - k * 0.8, 4.6 - k * 0.8)
        core = ar * 0.56
        gradient = QLinearGradient(0, -core, 0, core)
        gradient.setColorAt(0, mix(pal.accent, QColor("#ffffff"), 0.15))
        gradient.setColorAt(1, pal.accent_deep)
        p.setBrush(gradient)
        p.drawEllipse(QPointF(0, 0), core, core)
        spark = QPainterPath()  # a four-pointed spark, breathing a little
        r = core * (0.62 + 0.06 * a["pulse"])
        q = r * 0.16
        spark.moveTo(0, -r)
        spark.quadTo(q, -q, r, 0)
        spark.quadTo(q, q, 0, r)
        spark.quadTo(-q, q, -r, 0)
        spark.quadTo(-q, -q, 0, -r)
        p.setBrush(pal.on_accent)
        p.drawPath(spark)
        p.restore()

    @staticmethod
    def route(index):
        gx, gy = T.GATE
        end = QPointF(T.CHIP[0] - 6, T.CHIP_Y[index])
        path = QPainterPath(QPointF(gx + T.GATE_SIZE[0] / 2, gy))
        path.cubicTo(QPointF(gx + 110, gy), QPointF(end.x() - 110, end.y()), end)
        return path

    def paint_routes(self, p, s):
        pal = self.pal
        if s["routes"] <= 0:
            return
        p.setBrush(Qt.BrushStyle.NoBrush)
        for index, request in enumerate(s["requests"]):
            path = partial(self.route(index), s["routes"], 48)
            colour, width, dashed = pal.line, 1.6, False
            if request["badge"] > 0:
                colour = mix(pal.line, pal.bad if request["verdict"] == "block" else pal.ok, request["badge"] * 0.85)
                width = 1.6 + 0.6 * request["badge"]
                dashed = request["verdict"] == "block"
            pen = QPen(colour, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            if dashed:
                pen.setDashPattern([2, 4])
            p.setPen(pen)
            p.drawPath(path)

    def chip(self, p, L, index, dx=0.0):
        """Destination INDEX as it stands before its request is decided."""
        pal = self.pal
        cx, cw, ch = T.CHIP
        rect = QRectF(cx + dx, T.CHIP_Y[index] - ch / 2, cw, ch)
        self.shadow(p, rect, 16, 0.7, spread=18, drop=6)
        p.setBrush(pal.surface)
        p.setPen(QPen(pal.hairline, 1.3))
        p.drawRoundedRect(rect, 16, 16)
        pen = QPen(pal.line, 1.6)
        pen.setDashPattern([2, 3])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(rect.left() + 32, rect.center().y()), 13, 13)
        self.texts(p, L, ((f"host{index}", pal.fg),), dx=dx * L.s)

    def paint_chips(self, p, L, s):
        pal = self.pal
        cx, cw, ch = T.CHIP
        if all(shown >= 1 for shown in s["chips"]):
            top, bottom = T.CHIP_Y[0] - ch / 2 - 30, T.CHIP_Y[-1] + ch / 2 + 30
            area = QRectF(cx - 30, top, cw + 60, bottom - top)
            self.cached(p, ("chips",), area, lambda q: [self.chip(q, L, k) for k in range(len(T.REQUESTS))])
        for index, shown in enumerate(s["chips"]):
            if shown <= 0:
                continue
            request = s["requests"][index]
            p.save()
            if shown < 1:
                p.setOpacity(p.opacity() * shown)
                self.chip(p, L, index, (1 - shown) * 24)
            if request["badge"] > 0:
                rect = QRectF(cx, T.CHIP_Y[index] - ch / 2, cw, ch)
                colour = pal.bad if request["verdict"] == "block" else pal.ok
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(faded(colour, 0.55 * request["badge"]), 1.3))
                p.drawRoundedRect(rect, 16, 16)
                self.paint_badge(p, QPointF(rect.left() + 32, rect.center().y()), request)
            p.restore()

    def paint_badge(self, p, centre, request):
        pal = self.pal
        f = request["badge"]
        if f <= 0:
            return
        colour = pal.bad if request["verdict"] == "block" else pal.ok
        radius = 13 * T.spring(f, 6, 1.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(colour)
        p.drawEllipse(centre, radius, radius)
        if request["verdict"] == "block":
            self.paint_lock(p, centre, radius / 13, pal.on_accent if theme.luminance(colour) < 0.5 else pal.bg)
        else:
            tick = QPainterPath(centre + QPointF(-5.5, 0.3))
            tick.lineTo(centre + QPointF(-1.6, 4.3))
            tick.lineTo(centre + QPointF(5.8, -4.2))
            p.setPen(stroke(pal.on_accent if theme.luminance(colour) < 0.5 else pal.bg, 2.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(partial(tick, T.clamp(f * 1.6), 16))

    @staticmethod
    def paint_lock(p, centre, scale, colour):
        p.save()
        p.translate(centre)
        p.scale(scale, scale)
        p.setPen(stroke(colour, 2.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        shackle = QPainterPath(QPointF(-3.6, -1))
        shackle.lineTo(-3.6, -3.6)
        shackle.arcTo(QRectF(-3.6, -7.2, 7.2, 7.2), 180, -180)
        shackle.lineTo(3.6, -1)
        p.drawPath(shackle)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(colour)
        p.drawRoundedRect(QRectF(-5.8, -1.6, 11.6, 8.6), 2, 2)
        p.restore()

    def paint_gate(self, p, L, s):
        pal = self.pal
        g = s["gate"]
        if g["in"] <= 0:
            return
        p.save()
        p.setOpacity(p.opacity() * T.clamp(g["in"]))
        self.texts(p, L, (("proxy", pal.muted),))
        p.restore()
        gx, gy = T.GATE
        w, h = T.GATE_SIZE
        p.save()
        p.translate(gx, gy)
        p.scale(T.clamp(g["in"], 0, 1.2), g["in"])
        colour, strength = pal.accent, 0.0
        for kind, value in g["flash"]:
            if value > strength:
                colour, strength = getattr(pal, kind), value
        self.glow(p, QPointF(0, 0), 70 + 30 * strength, colour, pal.glow * (0.45 + 0.9 * strength))
        rect = QRectF(-w / 2, -h / 2, w, h)
        self.shadow(p, rect, w / 2, 0.8, spread=16, drop=5)
        p.setBrush(pal.surface)
        p.setPen(QPen(mix(pal.accent, colour, strength), 2.0))
        p.drawRoundedRect(rect, w / 2, w / 2)
        p.setPen(Qt.PenStyle.NoPen)  # the slot the requests pass
        p.setBrush(faded(mix(pal.accent, colour, strength), 0.22 + 0.5 * strength))
        p.drawRoundedRect(QRectF(-3, -h / 2 + 18, 6, h - 36), 3, 3)
        p.setBrush(mix(pal.accent, colour, strength))
        p.drawEllipse(QPointF(0, 0), 6 + 2 * strength, 6 + 2 * strength)
        for kind, value in g["flash"]:  # a ring widening from the gate
            if kind != "warn" and value > 0:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(faded(getattr(pal, kind), value * 0.8), 2.0))
                r = 24 + (1 - value) * 46
                p.drawEllipse(QPointF(0, 0), r, r)
        p.restore()

    def packet_at(self, request):
        ax, ay, ar = T.AGENT
        gx, gy = T.GATE
        u = request["u"]
        if u <= 1:
            stop = gx - (T.GATE_SIZE[0] / 2 + 16 if request["verdict"] != "allow" else 0)
            return QPointF(T.lerp(ax + ar + 14, stop, u), ay + (gy - ay) * u)
        return self.route(T.REQUESTS.index(next(r for r in T.REQUESTS if r[0] == request["host"]))) \
            .pointAtPercent(T.clamp(u - 1))

    def paint_requests(self, p, s):
        pal = self.pal
        for request in s["requests"]:
            if request["alpha"] <= 0:
                continue
            state = request["state"]
            colour = {"blocked": pal.bad, "waiting": pal.warn}.get(state, pal.accent if request["u"] <= 1 else pal.ok)
            if state == "waiting" or (request["verdict"] == "ask" and request["u"] > 1):
                colour = pal.warn if state == "waiting" else pal.ok
            point = self.packet_at(request) + QPointF(request["shake"] * 7, 0)
            p.save()
            p.setOpacity(p.opacity() * request["alpha"])
            if state == "travel" or (state != "blocked" and 0 < request["u"] < 2 and state != "waiting"):
                u, low = request["u"], 1.0 if request["u"] > 1 else 0.0
                points = [self.packet_at(dict(request, u=max(low, u - 0.16 * k / 8))) for k in range(9)]
                for k in range(8):  # a comet's tail, fading out behind it
                    p.setPen(stroke(faded(colour, 0.5 * (1 - k / 8)), 5.0 * (1 - k / 10)))
                    p.drawLine(points[k], points[k + 1])
            pulse = 0.5 + 0.5 * math.sin(request["wait"] * 9) if state == "waiting" else 1.0
            self.glow(p, point, 26, colour, 0.55 * pulse)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawRoundedRect(QRectF(point.x() - 11, point.y() - 6, 22, 12), 6, 6)
            if state == "blocked":
                lock = point + QPointF(0, -30)
                p.setBrush(pal.bad)
                p.drawEllipse(lock, 14, 14)
                self.paint_lock(p, lock, 1.0, pal.on_accent if theme.luminance(pal.bad) < 0.5 else pal.bg)
            p.restore()

    def card_body(self, p, L):
        """The approval question's still parts: its shadow, body, question and Deny button."""
        pal = self.pal
        rect = QRectF(*T.CARD)
        self.shadow(p, rect, 16, 1.2, spread=30, drop=10)
        pointer = QPainterPath(QPointF(T.GATE[0] - 11, rect.top() + 1))
        pointer.lineTo(T.GATE[0], rect.top() - 10)
        pointer.lineTo(T.GATE[0] + 11, rect.top() + 1)
        pointer.closeSubpath()
        body = QPainterPath()
        body.addRoundedRect(rect, 16, 16)
        p.setBrush(pal.surface)
        p.setPen(QPen(mix(pal.hairline, pal.warn, 0.45), 1.3))
        p.drawPath(body.united(pointer))
        p.setPen(QPen(pal.hairline, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(*Layout.button(0)), 9, 9)
        self.texts(p, L, (("ask", pal.fg), ("deny", pal.muted)))

    def paint_card(self, p, L, s):
        """The approval question: it pops up below the gate, Allow is pressed, it says yes and goes."""
        pal = self.pal
        a = s["approval"]
        alpha = T.clamp(a["in"]) * (1 - a["out"])
        if alpha <= 0:
            return
        dy = 14 * a["out"]
        p.save()
        p.setOpacity(p.opacity() * alpha)
        p.translate(0, dy)
        scale = 0.9 + 0.1 * max(0.0, a["in"])
        if abs(scale - 1) > 0.004:  # (texts stay on the fast path once it has settled)
            anchor = QPointF(T.GATE[0], T.CARD[1])
            p.translate(anchor)
            p.scale(scale, scale)
            p.translate(-anchor)
        x, y, w, h = T.CARD
        self.cached(p, ("card",), QRectF(x - 44, y - 44, w + 88, h + 88), lambda q: self.card_body(q, L))
        icon = QPointF(x + 36, y + 27)
        colour = mix(pal.warn, pal.ok, a["done"])
        self.glow(p, icon, 26, colour, 0.35)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(colour)
        p.drawEllipse(icon, 14, 14)
        ink = pal.on_accent if theme.luminance(colour) < 0.5 else pal.bg
        if a["done"] < 1:  # an hourglass while it waits
            p.save()
            p.setOpacity(p.opacity() * (1 - a["done"]))
            glass = QPainterPath(icon + QPointF(-5, -6))
            glass.lineTo(icon + QPointF(5, -6))
            glass.lineTo(icon + QPointF(-5, 6))
            glass.lineTo(icon + QPointF(5, 6))
            glass.closeSubpath()
            p.setBrush(ink)
            p.drawPath(glass)
            p.restore()
        if a["done"] > 0:
            tick = QPainterPath(icon + QPointF(-5.5, 0.3))
            tick.lineTo(icon + QPointF(-1.6, 4.3))
            tick.lineTo(icon + QPointF(5.8, -4.2))
            p.setPen(stroke(ink, 2.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(partial(tick, a["done"], 16))
        button = QRectF(*Layout.button(1))
        pressed = a["press"]
        p.save()
        if pressed > 0.004:
            p.translate(button.center())
            p.scale(1 - 0.06 * pressed, 1 - 0.06 * pressed)
            p.translate(-button.center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix(pal.accent, pal.accent_deep, 0.6 * pressed + 0.4 * a["done"]))
        p.drawRoundedRect(button, 9, 9)
        self.texts(p, L, (("allow", pal.on_accent),))
        p.restore()
        p.restore()

    def paint_caption(self, p, L, s):
        pal = self.pal
        caption = s["caption"]
        if caption is None:
            return
        p.save()
        p.setOpacity(p.opacity() * caption["alpha"])
        draw_text(p, L.texts[f"caption{caption['index']}"], pal.fg, dy=caption["rise"] * 16 * L.s)
        p.restore()
        # where in the story: three dots, the current one long
        p.save()
        self.stage(p, L)
        p.setPen(Qt.PenStyle.NoPen)
        y, gap = T.CAPTION_Y + 46, 8.0
        widths = [24.0 if k == caption["index"] else 7.0 for k in range(len(T.CAPTIONS))]
        x = T.WIDTH / 2 - (sum(widths) + gap * (len(widths) - 1)) / 2
        for k, width in enumerate(widths):
            p.setBrush(pal.accent if k == caption["index"] else pal.line)
            p.drawRoundedRect(QRectF(x, y - 3.5, width, 7), 3.5, 3.5)
            x += width + gap
        p.restore()
        p.restore()

    def paint_title(self, p, L, s):
        """"sandbox-ai", letter by letter, the tagline and the agents' names."""
        pal = self.pal
        title = s["title"]
        word = L.texts["word"]
        metrics = QFontMetricsF(word.font)
        text = "".join(WORDMARK)
        p.save()
        p.setFont(word.font)
        baseline = word.rect.top() + metrics.ascent()
        for index, char in enumerate(text):
            f = T.ease_out_cubic(T.clamp(title["word"] * 1.9 - index * 0.085))
            if f <= 0:
                continue
            x = word.rect.left() + metrics.horizontalAdvance(text[:index])
            p.save()
            p.setOpacity(p.opacity() * f)
            p.setPen(pal.accent if index >= len(WORDMARK[0]) else pal.fg)
            p.drawText(QPointF(x, baseline + (1 - f) * 22 * L.s), char)
            p.restore()
        p.restore()
        for name, value, colour in (("tag", title["tag"], pal.muted), ("agents", title["agents"],
                                                                        mix(pal.muted, pal.bg, 0.2))):
            if value > 0:
                p.save()
                p.setOpacity(p.opacity() * value)
                draw_text(p, L.texts[name], colour, dy=(1 - value) * 12 * L.s)
                p.restore()

    def paint_chrome(self, p, L, s, base):
        """The Skip button and a hairline of progress along the bottom."""
        pal = self.pal
        if s["skip"] <= 0:
            return
        p.save()
        p.setOpacity(base * s["skip"])
        pill = L.skip
        p.setBrush(faded(pal.surface, 0.85))
        p.setPen(QPen(pal.hairline, 1.0))
        p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        draw_text(p, L.texts["skip"], pal.fg)
        key = L.texts["esc"]
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(pal.line, 1.0))
        p.drawRoundedRect(key.rect, 4, 4)
        p.setFont(key.font)
        p.setPen(pal.muted)
        p.drawText(key.rect, Qt.AlignmentFlag.AlignCenter, key.text)
        height = max(2.0, 2.5 * L.s)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(faded(pal.accent, 0.55))
        p.drawRect(QRectF(0, L.height - height, L.width * s["progress"], height))
        p.restore()


def render_image(width, height, t, mode=None, reduced=False, ratio=1.0, renderer=None):
    """Scene T as an image (for tests and stills)."""
    image = QImage(QSize(round(width * ratio), round(height * ratio)), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    (renderer or Renderer(mode)).paint(painter, width, height, T.scene(t, reduced), ratio)
    painter.end()
    return image


class Intro(QWidget):
    """The animation, over PARENT's whole area; `finished` when it ends or is skipped."""
    finished = Signal()

    def __init__(self, parent, reduced=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tr("Press Esc to skip"))
        self.reduced = reduced_motion() if reduced is None else reduced
        self.duration = T.duration(self.reduced)
        self.renderer = Renderer()
        self.clock, self.timer, self.over = QElapsedTimer(), QTimer(self), False
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(16)  # about 60 frames a second; the scene follows the clock, not the frames
        self.timer.timeout.connect(self.tick)
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())

    def start(self):
        self.show()
        self.raise_()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.clock.start()
        self.timer.start()

    def elapsed(self):
        return self.clock.elapsed() / 1000.0 if self.clock.isValid() else 0.0

    def tick(self):
        if self.elapsed() >= self.duration:
            self.skip()
        else:
            self.update()

    def skip(self):
        if self.over:
            return
        self.over = True
        self.timer.stop()
        parent = self.parent()
        if parent is not None:
            parent.removeEventFilter(self)
        self.hide()
        self.finished.emit()
        self.deleteLater()

    # -- events --

    def eventFilter(self, watched, event):  # noqa: N802 - follow the window's size
        if watched is self.parent() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parent().rect())
        return False

    def keyPressEvent(self, event):  # noqa: N802 - any key skips it (a modifier alone does not)
        if event.key() in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta,
                           Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock):
            return super().keyPressEvent(event)
        self.skip()

    def mousePressEvent(self, event):  # noqa: N802
        self.skip()

    # -- drawing --

    def paintEvent(self, event):  # noqa: N802
        if self.over:
            return
        painter = QPainter(self)
        try:
            self.renderer.paint(painter, self.width(), self.height(), T.scene(self.elapsed(), self.reduced),
                                self.devicePixelRatioF())
        except Exception as error:  # noqa: BLE001 - a broken frame must never keep the app from the user
            applog.write(applog.entry(type(error), error, error.__traceback__, delivered="the intro: it stops"))
            QTimer.singleShot(0, self.skip)
        finally:
            painter.end()


def play(window, settings, force=False):
    """The intro over WINDOW, if wanted (see wanted()) or FORCEd (watched again); returns it, or None."""
    if not force and not wanted(settings):
        return None
    intro = None
    try:
        intro = Intro(window, reduced_motion(settings))
        intro.start()
    except Exception as error:  # noqa: BLE001 - without the intro the app starts all the same
        applog.write(applog.entry(type(error), error, error.__traceback__, delivered="the intro: it is left out"))
        if intro is not None:
            intro.skip()
        return None
    return intro
