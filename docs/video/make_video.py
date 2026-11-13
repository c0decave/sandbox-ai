#!/usr/bin/env python3
"""Render the sandbox-ai learning video from docs/video/storyboard.py.

    python3 docs/video/make_video.py --out DIR [--lang en|de|both] [--size 1280x720]

Requirements (not dependencies of sandbox-ai; put them into a throwaway virtual environment):
  * Python 3.9 or newer
  * Pillow (draws the frames)
  * ffmpeg: the `imageio-ffmpeg` package (it bundles one), `ffmpeg` on PATH, or --ffmpeg PATH
  * git, for the live privacy-check demo (optional: without it that step shows its fallback)

What it does:
  1. runs the storyboard's commands marked "live" with this checkout's own `sandbox-ai`, in a throwaway HOME
     (shown as /home/me; nothing of yours is read or changed), to show their real output;
  2. draws every frame with Pillow in the colours of the desktop app's dark theme and pipes the frames straight
     into ffmpeg (H.264, nothing is written to disk frame by frame);
  3. writes the narration as WebVTT subtitles and the chapters as a WebVTT chapter track.

The timing depends only on the storyboard (the longer of the English and German text decides), so the
subtitles of either language fit either video. Real screenshots of the desktop app are used when
docs/video/capture_app.py has written them into --app-shots DIR (default: OUT/app-shots); otherwise the app's
pages are drawn as schematics.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import wave

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import storyboard as SB  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # the check in main() explains what to install
    Image = ImageDraw = ImageFont = None

# The desktop app's default dark theme (sandbox-ai-app, sandbox_ai_app/theme.py, THEMES_DATA["dark"]).
C = {
    "window": "#17191d", "base": "#1f2228", "alternate": "#23272e", "text": "#e6e8eb", "secondary": "#a9afb9",
    "border": "#6b727e", "highlight": "#3a63c8", "accent": "#7aa2ff", "accent_text": "#0b1020", "danger": "#ff8a80",
    "nav": "#121418", "nav_selected": "#26324d", "nav_selected_text": "#dbe5ff", "title": "#f3f4f6",
    "canvas": "#14161a", "grid": "#626975", "node": "#1f2228", "node_header": "#28334a",
    "ok": "#7ddc8f", "warn": "#ffc46b", "bad": "#ff8a80", "muted": "#a9afb9",
    "terminal": "#14161a", "terminal_text": "#e6e8eb", "dim": "#3a3f48",
}
BOX_STYLES = {  # fill, border, label colour
    "outer": ("#1a1d22", "#3a3f48", C["secondary"]),
    "layer1": (C["base"], C["highlight"], C["title"]),
    "layer2": (C["alternate"], C["accent"], C["title"]),
    "agent": (C["node_header"], C["accent"], C["title"]),
    "proxy": (C["nav_selected"], C["nav_selected_text"], C["title"]),
    "ok": ("#1b2a20", C["ok"], C["title"]),
    "bad": ("#2e1e1e", C["bad"], C["title"]),
    "warn": ("#2c2518", C["warn"], C["title"]),
    "note": (None, None, C["secondary"]),
}
FONT_CANDIDATES = {
    "sans": ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
             "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
             "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"],
    "bold": ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
             "/System/Library/Fonts/Helvetica.ttc"],
    "mono": ["DejaVuSansMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
             "/usr/share/fonts/TTF/DejaVuSansMono.ttf", "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", "/System/Library/Fonts/Menlo.ttc",
             "/System/Library/Fonts/Monaco.ttf"],
    "monobold": ["DejaVuSansMono-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                 "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf", "/System/Library/Fonts/Menlo.ttc"],
}

FPS = 25
TITLE_SECONDS = 4.5
READ_CPS = 15.0  # characters per second a viewer reads comfortably
TYPE_CPS = 32.0
LINES_PER_SECOND = 45.0
FADE = 0.35
VOICE_LEAD = 0.5  # seconds before the first spoken cue of a scene
VOICE_PAUSE = 0.55  # after each spoken cue
VOICE_TAIL = 0.4  # after the last one


def version():
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', (REPO / "sandbox" / "agent_sandbox.py").read_text(), re.M)
    return match.group(1) if match else "?"


# --- live commands -----------------------------------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Live:
    """Runs the storyboard's live commands with this checkout's sandbox-ai in a throwaway HOME."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="sai-video-"))
        self.home = self.root / "home" / "me"
        self.home.mkdir(parents=True)
        (self.root / "run").mkdir(mode=0o700)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "sandbox-ai").symlink_to(REPO / "sandbox-ai")
        self.env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(self.home),
                    "XDG_RUNTIME_DIR": str(self.root / "run"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    "TERM": "dumb", "COLUMNS": "110", "NO_COLOR": "1", "SANDBOX_AI_SECRETS": "file",
                    "SANDBOX_AI_NO_DEPRECATION": "1", "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC"}
        self.warnings = []

    def path(self, value):
        return re.sub(r"(^|[,=:])~(?=/|$)", lambda m: m.group(1) + str(self.home), value)

    def setup(self):
        for item in SB.LIVE_SETUP:
            try:
                if "mkdir" in item:
                    Path(self.path(item["mkdir"])).mkdir(parents=True, exist_ok=True)
                elif "write" in item:
                    Path(self.path(item["write"])).write_text(item["text"].replace("{HOME}", str(self.home)))
                elif "run" in item:
                    subprocess.run(item["run"], cwd=self.path(item.get("cwd", "~")), env=self.env, check=True,
                                   capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as error:
                self.warnings.append(f"setup {item}: {error}")

    def clean(self, text):
        text = ANSI.sub("", text).replace(str(self.home), "/home/me").replace(str(self.root), "/tmp/sai-demo")
        return text.replace(str(REPO), "/opt/sandbox-ai")

    def run(self, step):
        if "file" in step:
            return self.clean(Path(self.path(step["file"])).read_text()).splitlines()
        argv = [self.path(arg) for arg in shlex.split(step["cmd"])]
        if argv[0] != "sandbox-ai":
            raise ValueError(f"only sandbox-ai commands run live: {step['cmd']}")
        cwd = self.home / "src" / "shop"
        done = subprocess.run(argv, cwd=cwd, env=self.env, input=step.get("stdin", ""), text=True, timeout=120,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE if "pick" in step else subprocess.STDOUT)
        out = done.stdout
        if "pick" in step:
            out = dump_json(pick_json(json.loads(out), step["pick"]))
        lines = self.clean(out).rstrip("\n").splitlines()
        if "head" in step and len(lines) > step["head"]:
            lines = lines[:step["head"]] + ["…"]
        return lines

    def capture(self):
        """{scene id: {step index: lines}} for every live step."""
        self.setup()
        found = {}
        for chapter, index, scene in SB.iter_scenes():
            for number, step in enumerate(scene.get("steps", [])):
                if not (step.get("live") or "file" in step):
                    continue
                try:
                    found.setdefault(SB.scene_id(chapter, index), {})[str(number)] = self.run(step)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.warnings.append(f"{SB.scene_id(chapter, index)} step {number} ({step['cmd']}): {error}")
        return found

    def remove(self):
        shutil.rmtree(self.root, ignore_errors=True)


def pick_json(data, paths):
    """The parts of DATA named by PATHS ("a", "a.b", "a[].b"), in that order."""
    result = {}

    def put(dst, src, parts):
        key = parts[0]
        if key.endswith("[]"):
            key = key[:-2]
            if not isinstance(src.get(key), list):
                return
            items = dst.setdefault(key, [{} for _ in src[key]])
            for target, item in zip(items, src[key]):
                if len(parts) > 1 and isinstance(item, dict):
                    put(target, item, parts[1:])
        elif key in src:
            if len(parts) == 1:
                dst[key] = src[key]
            elif isinstance(src[key], dict):
                put(dst.setdefault(key, {}), src[key], parts[1:])

    for path in paths:
        put(result, data, path.split("."))
    return result


def dump_json(value, indent=0, width=110):
    """JSON with short lists and objects on one line, and long lists of plain values packed into lines."""
    flat = json.dumps(value, ensure_ascii=False)
    if len(flat) + indent <= width or not isinstance(value, (dict, list)) or not value:
        return flat
    pad = " " * (indent + 2)
    if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
        rows, row = [], ""
        for item in (json.dumps(v, ensure_ascii=False) for v in value):
            if row and len(pad) + len(row) + len(item) + 2 > width:
                rows.append(row.rstrip())
                row = ""
            row += item + ", "
        rows.append(row.rstrip(", "))
        rows = [pad + r for r in rows]
        return "[\n" + "\n".join(rows) + "\n" + " " * indent + "]"
    if isinstance(value, dict):
        inner = [f'{pad}{json.dumps(k)}: {dump_json(v, indent + 2, width).lstrip()}' for k, v in value.items()]
        return "{\n" + ",\n".join(inner) + "\n" + " " * indent + "}"
    inner = [pad + dump_json(v, indent + 2, width).lstrip() for v in value]
    return "[\n" + ",\n".join(inner) + "\n" + " " * indent + "]"


# --- drawing helpers ---------------------------------------------------------------------------------------


class Canvas:
    """Sizes and fonts for one output size; the design is made for 1920 x 1080 and scaled."""

    def __init__(self, width, height, font_dir=None):
        self.w, self.h = width, height
        self.k = width / 1920
        self.font_dir = font_dir
        self.cache = {}

    def s(self, value):
        return int(round(value * self.k))

    def font(self, kind, size):
        key = (kind, size)
        if key not in self.cache:
            self.cache[key] = load_font(kind, self.s(size), self.font_dir)
        return self.cache[key]


def load_font(kind, size, font_dir=None):
    names = FONT_CANDIDATES[kind]
    if font_dir:
        names = [str(Path(font_dir) / Path(name).name) for name in names] + names
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    raise SystemExit(f"make_video: no {kind} font found (tried {', '.join(names)}); install DejaVu fonts or pass "
                     "--font-dir")


def rgb(colour):
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, amount):
    a, b = rgb(a) if isinstance(a, str) else a, rgb(b) if isinstance(b, str) else b
    return tuple(int(x + (y - x) * amount) for x, y in zip(a, b))


def segments(text):
    """TEXT split into (part, is_code) by `backticks`."""
    parts = text.split("`")
    return [(part, index % 2 == 1) for index, part in enumerate(parts) if part]


def layout(cv, text, size, width, kind="sans", code_kind="mono"):
    """TEXT wrapped to WIDTH pixels: a list of lines, each a list of (word, is_code, font)."""
    font, code = cv.font(kind, size), cv.font(code_kind, size - 2)
    words = []
    for part, is_code in segments(text):
        pieces = re.split(r"(\s+)", part)
        for piece in pieces:
            if piece:
                words.append((piece, is_code, code if is_code else font))
    lines, line, used = [], [], 0
    for word, is_code, f in words:
        if word.isspace():
            if line:
                line.append((" ", is_code, f))
                used += f.getlength(" ")
            continue
        length = f.getlength(word)
        if line and used + length > width:
            while line and line[-1][0] == " ":
                used -= line[-1][2].getlength(" ")
                line.pop()
            lines.append(line)
            line, used = [], 0
        line.append((word, is_code, f))
        used += length
    while line and line[-1][0] == " ":
        line.pop()
    if line:
        lines.append(line)
    return lines


def line_width(line):
    return sum(f.getlength(word) for word, _, f in line)


def draw_lines(draw, lines, x, y, line_height, colour, code_colour, align="left", width=0, alpha=1.0, bg=None):
    for line in lines:
        cx = x
        if align == "center":
            cx = x + (width - line_width(line)) / 2
        for word, is_code, f in line:
            c = code_colour if is_code else colour
            if alpha < 1 and bg is not None:
                c = mix(bg, c, alpha)
            draw.text((cx, y), word, font=f, fill=c)
            cx += f.getlength(word)
        y += line_height
    return y


def rounded(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


# --- scenes ------------------------------------------------------------------------------------------------


class Scene:
    """One scene: a duration, its caption cues and a way to draw a frame at time t (seconds into the scene)."""
    content_seconds = 6.0
    header = True

    def __init__(self, cv, lang, chapter_index, captions, label, ident, track=None):
        self.cv, self.lang = cv, lang
        self.chapter_index = chapter_index  # 0-based, or None for the course cards
        self.track = track  # a video of a series: (0-based index, number of videos, its title) for the header
        self.captions = captions
        self.label = label
        self.ident = ident
        self.duration = 0.0
        self.cues = []
        self.voice = None  # spoken narration: [(WAV path, seconds)], one per caption (narrate.py)
        self.sounds = []  # (start in the scene, WAV path) of each spoken cue
        self._base = None

    # timing
    def caption_seconds(self):
        return sum(cue_seconds(c) for c in self.captions)

    def plan(self):
        if self.voice:
            return self.plan_spoken()
        need = self.caption_seconds() + (0.8 if self.captions else 0)
        self.duration = max(self.content_seconds, need)
        total = self.caption_seconds() or 1
        start = 0.4 if self.captions else 0
        span = self.duration - start - 0.3
        for caption in self.captions:
            length = span * cue_seconds(caption) / total
            self.cues.append((start, start + length, caption))
            start += length

    def plan_spoken(self):
        """The timing of the spoken narration: each cue as long as its audio and a pause; the scene at least as
        long as its content needs."""
        start = VOICE_LEAD
        for caption, (path, seconds) in zip(self.captions, self.voice):
            self.cues.append((start, start + seconds + VOICE_PAUSE, caption))
            self.sounds.append((start, path))
            start += seconds + VOICE_PAUSE
        self.duration = max(self.content_seconds, start + VOICE_TAIL)
        if self.cues:  # the last cue stays until the scene ends
            a, _b, caption = self.cues[-1]
            self.cues[-1] = (a, self.duration - 0.2, caption)

    def cue_at(self, t):
        for index, (a, b, _) in enumerate(self.cues):
            if a <= t < b:
                return index
        return None

    # drawing
    def t(self, value):
        return SB.text(value, self.lang)

    def base(self):
        if self._base is None:
            img = Image.new("RGB", (self.cv.w, self.cv.h), C["window"])
            draw = ImageDraw.Draw(img)
            if self.header:
                self.draw_header(draw)
            self.draw_static(img, draw)
            self._base = img
        return self._base

    def draw_header(self, draw):
        cv = self.cv
        draw.rectangle((0, 0, cv.w, cv.s(84)), fill=C["nav"])
        draw.text((cv.s(48), cv.s(26)), "sandbox-ai", font=cv.font("bold", 26), fill=C["title"])
        if self.track is not None:
            return self.draw_track(draw)
        if self.chapter_index is not None:
            number = self.chapter_index + 1
            title = self.t(SB.CHAPTERS[self.chapter_index]["title"])
            draw.text((cv.s(230), cv.s(28)), f"{number:02d}  {title}", font=cv.font("sans", 24), fill=C["secondary"])
        # chapter progress: one segment per chapter
        n = len(SB.CHAPTERS)
        x0, x1, y = cv.s(1180), cv.s(1872), cv.s(38)
        gap = cv.s(6)
        seg = (x1 - x0 - gap * (n - 1)) / n
        for i in range(n):
            a = x0 + i * (seg + gap)
            if self.chapter_index is None:
                colour = C["dim"]
            elif i < self.chapter_index:
                colour = mix(C["nav"], C["accent"], 0.45)
            elif i == self.chapter_index:
                colour = C["accent"]
            else:
                colour = C["dim"]
            draw.rounded_rectangle((a, y, a + seg, y + cv.s(8)), radius=cv.s(4), fill=colour)

    def draw_track(self, draw):
        """The header of a series video: its number and title, and one segment per video of the series."""
        cv = self.cv
        index, count, title = self.track
        draw.text((cv.s(230), cv.s(28)), f"{index + 1:02d}  {title}", font=cv.font("sans", 24), fill=C["secondary"])
        x0, x1, y = cv.s(1180), cv.s(1872), cv.s(38)
        gap = cv.s(6)
        seg = (x1 - x0 - gap * (count - 1)) / count
        for i in range(count):
            a = x0 + i * (seg + gap)
            colour = C["accent"] if i == index else mix(C["nav"], C["accent"], 0.45) if i < index else C["dim"]
            draw.rounded_rectangle((a, y, a + seg, y + cv.s(8)), radius=cv.s(4), fill=colour)

    def draw_heading(self, draw, text, y=118):
        cv = self.cv
        draw.text((cv.s(96), cv.s(y)), text, font=cv.font("bold", 50), fill=C["title"])

    def draw_static(self, img, draw):
        pass

    def key(self, t):
        """Frames with the same key look the same (the encoder gets the previous frame again)."""
        return (self.dynamic_key(t), self.cue_at(t), fade_step(t))

    def dynamic_key(self, t):
        return None

    def frame(self, t, captions=True):
        img = self.base().copy()
        draw = ImageDraw.Draw(img)
        self.draw_dynamic(img, draw, t)
        if captions:
            self.draw_caption(draw, t)
        alpha = min(1.0, t / FADE) if FADE else 1.0
        if alpha < 1:
            img = Image.blend(Image.new("RGB", img.size, C["window"]), img, alpha)
        return img

    def draw_dynamic(self, img, draw, t):
        pass

    def draw_caption(self, draw, t):
        index = self.cue_at(t)
        if index is None:
            return
        cv = self.cv
        text = self.t(self.cues[index][2]).replace("`", "")
        lines = layout(cv, text, 30, cv.s(1700))
        size = 30
        if len(lines) > 2:
            size = 26
            lines = layout(cv, text, size, cv.s(1740))
        lh = cv.s(size * 1.4)
        top = cv.h - cv.s(28) - lh * len(lines) - cv.s(22)
        draw.rectangle((0, top, cv.w, cv.h), fill=C["nav"])
        draw_lines(draw, lines, 0, top + cv.s(18), lh, C["text"], C["text"], align="center", width=cv.w)


def cue_seconds(caption):
    longest = max(len(caption["en"]), len(caption["de"]))
    return max(3.0, longest / READ_CPS + 0.7)


def fade_step(t):
    return int(t / FADE * 8) if t < FADE else 8


class CourseTitle(Scene):
    header = False
    content_seconds = 7.0

    def draw_static(self, img, draw):
        cv = self.cv
        cx, cy = cv.w // 2, cv.s(380)
        # the mark: a sandbox (two nested frames) with the agent inside
        for inset, colour, width in ((0, C["highlight"], 6), (34, C["accent"], 5)):
            size = cv.s(150 - inset)
            rounded(draw, (cx - size, cy - size, cx + size, cy + size), cv.s(36 - inset // 3),
                    outline=colour, width=cv.s(width))
        r = cv.s(30)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=C["ok"])
        title = self.t(SB.COURSE["title"])
        f = cv.font("bold", 110)
        draw.text(((cv.w - f.getlength(title)) / 2, cv.s(570)), title, font=f, fill=C["title"])
        sub = self.t(SB.COURSE["subtitle"])
        f = cv.font("sans", 40)
        draw.text(((cv.w - f.getlength(sub)) / 2, cv.s(715)), sub, font=f, fill=C["secondary"])
        line = f"sandbox-ai {version()}"
        f = cv.font("mono", 26)
        draw.text(((cv.w - f.getlength(line)) / 2, cv.s(790)), line, font=f, fill=C["accent"])


class Contents(Scene):
    content_seconds = 8.0

    def draw_static(self, img, draw):
        cv = self.cv
        self.draw_heading(draw, self.t(SB.COURSE["contents"]))
        half = (len(SB.CHAPTERS) + 1) // 2
        for i, chapter in enumerate(SB.CHAPTERS):
            col, row = divmod(i, half)
            x, y = cv.s(110 + col * 870), cv.s(235 + row * 88)
            draw.text((x, y), f"{i + 1:02d}", font=cv.font("bold", 38), fill=C["accent"])
            draw.text((x + cv.s(80), y + cv.s(4)), self.t(chapter["title"]), font=cv.font("sans", 32), fill=C["text"])


class ChapterTitle(Scene):
    header = False
    content_seconds = TITLE_SECONDS

    def draw_static(self, img, draw):
        cv = self.cv
        chapter = SB.CHAPTERS[self.chapter_index]
        n = len(SB.CHAPTERS)
        x = cv.s(180)
        label = f"{self.t(SB.COURSE['chapter'])} {self.chapter_index + 1} {self.t(SB.COURSE['of'])} {n}"
        draw.text((x, cv.s(230)), label.upper(), font=cv.font("bold", 28), fill=C["secondary"])
        draw.text((x - cv.s(8), cv.s(270)), f"{self.chapter_index + 1:02d}", font=cv.font("bold", 190), fill=C["accent"])
        lines = layout(cv, self.t(chapter["title"]), 66, cv.s(1560), kind="bold")
        y = draw_lines(draw, lines, x, cv.s(500), cv.s(84), C["title"], C["title"])
        y += cv.s(30)
        draw.text((x, y), self.t(SB.COURSE["in_chapter"]), font=cv.font("bold", 28), fill=C["secondary"])
        y += cv.s(52)
        for topic in chapter["topics"]:
            draw.ellipse((x, y + cv.s(13), x + cv.s(12), y + cv.s(25)), fill=C["accent"])
            draw_lines(draw, layout(cv, self.t(topic), 34, cv.s(1500)), x + cv.s(32), y, cv.s(46), C["text"], C["accent"])
            y += cv.s(54)
        # progress along the bottom
        bar_y = cv.h - cv.s(70)
        seg = (cv.w - cv.s(360)) / n
        for i in range(n):
            a = cv.s(180) + i * seg
            colour = C["accent"] if i == self.chapter_index else mix(C["window"], C["accent"], 0.4) if i < self.chapter_index else C["dim"]
            draw.rounded_rectangle((a, bar_y, a + seg - cv.s(8), bar_y + cv.s(10)), radius=cv.s(5), fill=colour)


class Bullets(Scene):
    def __init__(self, *args, scene, **kwargs):
        super().__init__(*args, **kwargs)
        self.scene = scene
        self.items = scene.get("bullets", [])
        self.content_seconds = 1.5 + 0.8 * len(self.items) + 3.5

    def reveal(self, i):
        return 0.6 + i * 0.8

    def dynamic_key(self, t):
        shown = sum(1 for i in range(len(self.items)) if t >= self.reveal(i))
        last = self.reveal(shown - 1) if shown else 0
        return shown, min(4, int((t - last) / 0.06)) if shown else 0

    def draw_static(self, img, draw):
        self.draw_heading(draw, self.t(self.scene["heading"]))

    def draw_dynamic(self, img, draw, t):
        cv = self.cv
        y = cv.s(250)
        for i, item in enumerate(self.items):
            start = self.reveal(i)
            if t < start:
                break
            alpha = min(1.0, (t - start) / 0.25)
            lines = layout(cv, self.t(item), 38, cv.s(1580))
            dot = mix(C["window"], C["accent"], alpha)
            draw.rounded_rectangle((cv.s(110), y + cv.s(17), cv.s(126), y + cv.s(33)), radius=cv.s(4), fill=dot)
            y = draw_lines(draw, lines, cv.s(160), y, cv.s(54), C["text"], C["accent"], alpha=alpha, bg=C["window"])
            y += cv.s(26)
        note = self.scene.get("note")
        if note and t >= self.reveal(len(self.items)):
            lines = layout(cv, self.t(note), 30, cv.s(1560))
            top = max(y + cv.s(20), cv.s(700))
            height = cv.s(42) * len(lines) + cv.s(36)
            rounded(draw, (cv.s(110), top, cv.s(1810), top + height), cv.s(12), fill=C["base"], outline=C["highlight"],
                    width=cv.s(2))
            draw_lines(draw, lines, cv.s(140), top + cv.s(18), cv.s(42), C["secondary"], C["accent"])

    def dynamic_key_with_note(self, t):
        return self.dynamic_key(t), bool(self.scene.get("note") and t >= self.reveal(len(self.items)))

    def key(self, t):
        return (self.dynamic_key_with_note(t), self.cue_at(t), fade_step(t))


class Table(Scene):
    def __init__(self, *args, scene, **kwargs):
        super().__init__(*args, **kwargs)
        self.scene = scene
        self.content_seconds = 1.5 + 0.6 * len(scene["rows"]) + 4

    def reveal(self, i):
        return 0.6 + i * 0.5

    def dynamic_key(self, t):
        return sum(1 for i in range(len(self.scene["rows"])) if t >= self.reveal(i))

    def geometry(self):
        cv = self.cv
        total = cv.s(1720)
        widths = self.scene.get("widths") or ([0.42, 0.58] if len(self.scene["columns"]) == 2 else [0.24, 0.38, 0.38])
        return [int(total * w) for w in widths]

    def draw_static(self, img, draw):
        cv = self.cv
        self.draw_heading(draw, self.t(self.scene["heading"]))
        x = cv.s(100)
        y = cv.s(225)
        widths = self.geometry()
        rounded(draw, (x, y, x + sum(widths), y + cv.s(56)), cv.s(10), fill=C["node_header"])
        cx = x
        for width, column in zip(widths, self.scene["columns"]):
            draw.text((cx + cv.s(20), y + cv.s(13)), self.t(column), font=cv.font("bold", 28), fill=C["title"])
            cx += width

    def row_lines(self, row):
        widths = self.geometry()
        return [layout(self.cv, self.t(cell), 30, width - self.cv.s(40)) for cell, width in zip(row, widths)]

    def draw_dynamic(self, img, draw, t):
        cv = self.cv
        x, y = cv.s(100), cv.s(290)
        widths = self.geometry()
        for i, row in enumerate(self.scene["rows"]):
            cells = self.row_lines(row)
            height = max(len(c) for c in cells) * cv.s(42) + cv.s(30)
            if t >= self.reveal(i):
                fill = C["base"] if i % 2 == 0 else C["alternate"]
                draw.rectangle((x, y, x + sum(widths), y + height), fill=fill)
                cx = x
                for width, lines in zip(widths, cells):
                    draw_lines(draw, lines, cx + cv.s(20), y + cv.s(15), cv.s(42), C["text"], C["accent"])
                    cx += width
            y += height + cv.s(4)


class Compare(Scene):
    def __init__(self, *args, scene, **kwargs):
        super().__init__(*args, **kwargs)
        self.scene = scene
        self.n = max(len(scene["left"]["items"]), len(scene["right"]["items"]))
        self.content_seconds = 1.5 + 0.5 * self.n * 2 + 4

    def reveal(self, side, i):
        return 0.6 + (i * 0.5 if side == 0 else 0.6 + self.n * 0.5 + i * 0.5)

    def dynamic_key(self, t):
        return tuple(sum(1 for i in range(self.n) if t >= self.reveal(s, i)) for s in (0, 1))

    def draw_static(self, img, draw):
        cv = self.cv
        self.draw_heading(draw, self.t(self.scene["heading"]))
        bottom = max(self.items_bottom(side) for side in ("left", "right")) + cv.s(40)
        for s, side in enumerate(("left", "right")):
            x = cv.s(100 + s * 880)
            colour = C["ok"] if s == 0 else C["bad"]
            rounded(draw, (x, cv.s(230), x + cv.s(840), max(bottom, cv.s(640))), cv.s(16), fill=C["base"],
                    outline=colour, width=cv.s(3))
            draw.text((x + cv.s(36), cv.s(255)), self.t(self.scene[side]["title"]), font=cv.font("bold", 38), fill=colour)

    def items_bottom(self, side):
        cv = self.cv
        y = cv.s(335)
        for item in self.scene[side]["items"]:
            y += len(layout(cv, self.t(item), 31, cv.s(720))) * cv.s(44) + cv.s(26)
        return y

    def draw_dynamic(self, img, draw, t):
        cv = self.cv
        for s, side in enumerate(("left", "right")):
            x = cv.s(100 + s * 880)
            y = cv.s(335)
            colour = C["ok"] if s == 0 else C["bad"]
            for i, item in enumerate(self.scene[side]["items"]):
                if t < self.reveal(s, i):
                    break
                mark = "✓" if s == 0 else "✗"
                draw.text((x + cv.s(36), y), mark, font=cv.font("bold", 32), fill=colour)
                lines = layout(cv, self.t(item), 31, cv.s(720))
                y = draw_lines(draw, lines, x + cv.s(84), y, cv.s(44), C["text"], C["accent"]) + cv.s(26)


class Terminal(Scene):
    """Commands typed into a terminal, with their output."""

    def __init__(self, *args, scene, live, **kwargs):
        super().__init__(*args, **kwargs)
        self.scene = scene
        cv = self.cv
        self.font = cv.font("mono", 22)
        self.bold = cv.font("monobold", 22)
        self.cw = self.font.getlength("M")
        self.box = (cv.s(80), cv.s(205), cv.s(1840), cv.s(905))
        self.cols = int((self.box[2] - self.box[0] - cv.s(48)) / self.cw)
        self.lh = cv.s(30)
        self.rows = int((self.box[3] - self.box[1] - cv.s(70)) / self.lh)
        self.events = []  # (time, kind, text): kind "comment", "type", "out"
        self.kinds = set()
        t = 0.6
        sid = self.ident
        for number, step in enumerate(scene["steps"]):
            comment = step.get("comment")
            if comment:
                self.events.append((t, "comment", "# " + SB.text(comment, self.lang)))
            out = None
            if step.get("live") or "file" in step:
                out = live.get(sid, {}).get(str(number))
                if out is not None:
                    self.kinds.add("live")
            if out is None and "out" in step:
                out = step["out"]
                self.kinds.add("example" if step.get("example") else "live")
            elif out is None:
                self.kinds.add("commands")
            self.events.append((t, "type", step["cmd"]))
            t += 0.3 + len(step["cmd"]) / TYPE_CPS
            if out:
                t += 0.25
                if step.get("example") and out is step.get("out"):
                    self.events.append((t, "example", "# " + SB.text(SB.COURSE["badges"]["example_comment"], self.lang)))
                elif "pick" in step and out is not step.get("out"):
                    self.events.append((t, "comment", "# " + SB.text(SB.COURSE["badges"]["picked"], self.lang)))
                for line in out:
                    for index, piece in enumerate(self.wrap(line)):
                        # a wrapped line keeps the colour of its start ("out:" + the whole line)
                        self.events.append((t, "out" if index == 0 else "out:" + line, piece))
                        t += 1 / LINES_PER_SECOND
                t += 1.4 + min(5.0, sum(len(line) for line in out) / 80)
            else:
                t += 0.7
        self.content_seconds = t + 2.5
        if self.kinds == {"commands"}:
            self.badge = "commands"
        elif "example" in self.kinds and "live" in self.kinds:
            self.badge = "mixed"
        elif "example" in self.kinds:
            self.badge = "example"
        elif "live" in self.kinds:
            self.badge = "live"
        else:
            self.badge = "commands"

    def wrap(self, line):
        if not line:
            return [""]
        return [line[i:i + self.cols] for i in range(0, len(line), self.cols)]

    def state(self, t):
        """The visible lines at T: [(kind, text)], the last command possibly partly typed."""
        lines = []
        for start, kind, text in self.events:
            if t < start:
                break
            if kind == "type":
                typed = int((t - start - 0.3) * TYPE_CPS) if t - start > 0.3 else 0
                shown = text[:max(0, typed)]
                cursor = len(shown) < len(text)
                for i, piece in enumerate(self.wrap("$ " + shown + ("█" if cursor else ""))):
                    lines.append(("cmd" if i == 0 else "cmdcont", piece))
            else:
                lines.append((kind, text))
        return lines

    def dynamic_key(self, t):
        lines = self.state(t)
        idle_cursor = int(t * 2) % 2 if (not lines or lines[-1][0] != "cmd" or "█" not in lines[-1][1]) else 0
        return len(lines), lines[-1][1] if lines else "", idle_cursor

    def draw_static(self, img, draw):
        cv = self.cv
        self.draw_heading(draw, self.t(self.scene["heading"]))
        x0, y0, x1, y1 = self.box
        rounded(draw, (x0, y0, x1, y1), cv.s(14), fill=C["terminal"], outline=C["dim"], width=cv.s(2))
        draw.rounded_rectangle((x0, y0, x1, y0 + cv.s(46)), radius=cv.s(14), fill=C["base"])
        draw.rectangle((x0, y0 + cv.s(30), x1, y0 + cv.s(46)), fill=C["base"])
        for i, colour in enumerate((C["bad"], C["warn"], C["ok"])):
            cx = x0 + cv.s(28 + i * 26)
            draw.ellipse((cx - cv.s(7), y0 + cv.s(16), cx + cv.s(7), y0 + cv.s(30)), fill=colour)
        draw.text((x0 + cv.s(120), y0 + cv.s(11)), "me@host: ~/src/shop", font=cv.font("sans", 21), fill=C["secondary"])
        label = self.t(SB.COURSE["badges"][self.badge])
        colour = {"live": C["ok"], "example": C["warn"], "mixed": C["warn"], "commands": C["secondary"]}[self.badge]
        f = cv.font("bold", 20)
        width = f.getlength(label) + cv.s(32)
        rounded(draw, (x1 - width - cv.s(14), y0 + cv.s(8), x1 - cv.s(14), y0 + cv.s(38)), cv.s(15),
                outline=colour, width=cv.s(2))
        draw.text((x1 - width + cv.s(2), y0 + cv.s(11)), label, font=f, fill=colour)

    def colour_of(self, kind, text):
        if kind in ("comment",):
            return C["secondary"]
        if kind == "example":
            return C["warn"]
        if kind in ("cmd", "cmdcont"):
            return C["title"]
        stripped = text.lstrip()
        if stripped.startswith(("ok ", "ok\t")) or stripped == "ok":
            return C["ok"]
        if stripped.startswith(("WARN", "->")):
            return C["warn"]
        if stripped.startswith("FAIL") or " block:" in text or "refus" in text and "agent-sandbox:" in text:
            return C["bad"]
        if stripped.startswith("agent-sandbox:") or stripped.startswith("sandbox-mcp:"):
            return C["warn"] if "widens" in text or "refus" in text else C["secondary"]
        return C["terminal_text"]

    def draw_dynamic(self, img, draw, t):
        cv = self.cv
        lines = self.state(t)
        if len(lines) > self.rows:
            lines = lines[-self.rows:]
        x = self.box[0] + cv.s(24)
        y = self.box[1] + cv.s(62)
        for kind, text in lines:
            colour = self.colour_of("out", kind[4:]) if kind.startswith("out:") else self.colour_of(kind, text)
            if kind == "cmd":
                draw.text((x, y), "$", font=self.bold, fill=C["ok"])
                draw.text((x + self.cw * 2, y), text[2:], font=self.bold, fill=colour)
            elif kind == "out" and re.match(r'\s*"[^"]+":', text):
                match = re.match(r'(\s*"[^"]+":)(.*)', text)
                draw.text((x, y), match.group(1), font=self.font, fill=C["accent"])
                draw.text((x + self.font.getlength(match.group(1)), y), match.group(2), font=self.font, fill=C["terminal_text"])
            else:
                draw.text((x, y), text, font=self.font, fill=colour)
            y += self.lh
        # an idle prompt with a blinking cursor once everything is typed
        if lines and lines[-1][0] != "cmd" and len(lines) < self.rows and t > self.events[-1][0] + 0.4:
            draw.text((x, y), "$", font=self.bold, fill=C["ok"])
            if int(t * 2) % 2 == 0:
                draw.text((x + self.cw * 2, y), "█", font=self.font, fill=C["title"])


class Diagram(Scene):
    def __init__(self, *args, scene, **kwargs):
        super().__init__(*args, **kwargs)
        self.scene = scene
        self.data = SB.DIAGRAMS[scene["diagram"]]
        self.steps = sorted({b["step"] for b in self.data["boxes"]} | {a["step"] for a in self.data["arrows"]})
        self.content_seconds = 1.0 + 1.4 * len(self.steps) + 3.0
        self.states = {}
        self.area = (100, 230, 1820, 880)

    def step_time(self, k):
        span = max(self.duration * 0.62, 1.4 * len(self.steps)) if self.duration else 1.4 * len(self.steps)
        return 0.5 + k * span / max(1, len(self.steps))

    def level(self, t):
        shown = sum(1 for k in range(len(self.steps)) if t >= self.step_time(k))
        partial = 1.0
        if shown:
            partial = min(1.0, (t - self.step_time(shown - 1)) / 0.4)
        return shown, partial

    def dynamic_key(self, t):
        shown, partial = self.level(t)
        return shown, int(partial * 6)

    def rect(self, box):
        cv = self.cv
        x0, y0, x1, y1 = self.area
        ux, uy = (x1 - x0) / 100, (y1 - y0) / 40
        return (cv.s(x0 + box["x"] * ux), cv.s(y0 + box["y"] * uy), cv.s(x0 + (box["x"] + box["w"]) * ux),
                cv.s(y0 + (box["y"] + box["h"]) * uy))

    def unit(self):
        x0, y0, x1, y1 = self.area
        return self.cv.s((x1 - x0) / 100), self.cv.s((y1 - y0) / 40)

    def draw_static(self, img, draw):
        self.draw_heading(draw, self.t(self.scene["heading"]))

    def state_image(self, shown):
        """The base with every element of the first SHOWN steps."""
        if shown not in self.states:
            img = self.base().copy()
            draw = ImageDraw.Draw(img)
            visible = set(self.steps[:shown])
            boxes = {b["id"]: b for b in self.data["boxes"]}
            for box in self.data["boxes"]:
                if box["step"] in visible:
                    self.draw_box(draw, box)
            for arrow in self.data["arrows"]:
                if arrow["step"] in visible:
                    self.draw_arrow(draw, boxes[arrow["from"]], boxes[arrow["to"]], arrow)
            self.states[shown] = img
        return self.states[shown]

    def draw_box(self, draw, box):
        cv = self.cv
        x0, y0, x1, y1 = self.rect(box)
        fill, border, colour = BOX_STYLES[box.get("style", "layer1")]
        if fill or border:
            rounded(draw, (x0, y0, x1, y1), cv.s(14), fill=fill, outline=border, width=cv.s(3))
        pad = cv.s(16)
        width = x1 - x0 - 2 * pad
        label = layout(cv, self.t(box["label"]), 25, width, kind="bold", code_kind="monobold")
        sub = layout(cv, self.t(box["sub"]), 20, width) if box.get("sub") else []
        lh, slh = cv.s(33), cv.s(27)
        height = len(label) * lh + (cv.s(6) + len(sub) * slh if sub else 0)
        if box.get("align") == "top":
            y = y0 + cv.s(10)
            y = draw_lines(draw, label, x0 + pad, y, lh, colour, colour)
            draw_lines(draw, sub, x0 + pad, y + cv.s(4), slh, C["secondary"], C["secondary"])
        else:
            y = y0 + (y1 - y0 - height) / 2
            y = draw_lines(draw, label, x0 + pad, y, lh, colour, colour, align="center", width=width)
            draw_lines(draw, sub, x0 + pad, y + cv.s(6), slh, C["secondary"], C["secondary"], align="center", width=width)

    def endpoints(self, a, b, arrow):
        ax0, ay0, ax1, ay1 = self.rect(a)
        bx0, by0, bx1, by1 = self.rect(b)
        sides = arrow.get("sides")
        hgap = max(bx0 - ax1, ax0 - bx1)
        vgap = max(by0 - ay1, ay0 - by1)
        if sides is None:
            sides = "h" if hgap >= vgap else "v"

        def mid(lo1, hi1, lo2, hi2):
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            return ((lo + hi) / 2, (lo + hi) / 2) if lo < hi else ((lo1 + hi1) / 2, (lo2 + hi2) / 2)

        if sides == "h":
            ya, yb = mid(ay0, ay1, by0, by1)
            if bx0 >= ax1:
                return (ax1, ya), (bx0, yb)
            return (ax0, ya), (bx1, yb)
        if sides == "v":
            xa, xb = mid(ax0, ax1, bx0, bx1)
            if by0 >= ay1:
                return (xa, ay1), (xb, by0)
            return (xa, ay0), (xb, by1)
        points = {"r": lambda r: (r[2], (r[1] + r[3]) / 2), "l": lambda r: (r[0], (r[1] + r[3]) / 2),
                  "t": lambda r: ((r[0] + r[2]) / 2, r[1]), "b": lambda r: ((r[0] + r[2]) / 2, r[3])}
        return points[sides[0]](self.rect(a)), points[sides[1]](self.rect(b))

    def draw_arrow(self, draw, a, b, arrow):
        cv = self.cv
        (x0, y0), (x1, y1) = self.endpoints(a, b, arrow)
        dx, dy = x1 - x0, y1 - y0
        length = max(1.0, (dx * dx + dy * dy) ** 0.5)
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        offset = arrow.get("offset", 0) * self.unit()[0] * 0.5
        x0, y0, x1, y1 = x0 + px * offset, y0 + py * offset, x1 + px * offset, y1 + py * offset
        gap = cv.s(6)
        x0, y0, x1, y1 = x0 + ux * gap, y0 + uy * gap, x1 - ux * gap, y1 - uy * gap
        colour = {"ok": C["ok"], "bad": C["bad"], "warn": C["warn"], "dashed": C["secondary"]}.get(arrow.get("style"), C["border"])
        width = cv.s(4)
        head = cv.s(18)
        if arrow.get("style") == "dashed":
            dash, pos = cv.s(14), 0.0
            while pos < length - head:
                end = min(pos + dash, length - head)
                draw.line((x0 + ux * pos, y0 + uy * pos, x0 + ux * end, y0 + uy * end), fill=colour, width=width)
                pos += dash * 2
        else:
            start = head if arrow.get("both") else 0
            draw.line((x0 + ux * start, y0 + uy * start, x1 - ux * head, y1 - uy * head), fill=colour, width=width)

        def tip(tx, ty, vx, vy):
            draw.polygon([(tx, ty), (tx - vx * head + -vy * head * 0.55, ty - vy * head + vx * head * 0.55),
                          (tx - vx * head - -vy * head * 0.55, ty - vy * head - vx * head * 0.55)], fill=colour)

        tip(x1, y1, ux, uy)
        if arrow.get("both"):
            tip(x0, y0, -ux, -uy)
        if arrow.get("label"):
            text = self.t(arrow["label"])
            f = cv.font("bold", 20)
            w, h = f.getlength(text), cv.s(26)
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            if arrow.get("offset"):
                side = 1 if arrow["offset"] > 0 else -1
                mx += px * side * (w / 2 + cv.s(14)) * (1 if abs(px) > abs(py) else 0)
                my += py * side * (h / 2 + cv.s(10)) * (1 if abs(py) >= abs(px) else 0)
            rounded(draw, (mx - w / 2 - cv.s(10), my - h / 2 - cv.s(4), mx + w / 2 + cv.s(10), my + h / 2 + cv.s(4)),
                    cv.s(10), fill=C["window"], outline=colour, width=cv.s(2))
            draw.text((mx - w / 2, my - h / 2), text, font=f, fill=C["text"])

    def frame(self, t, captions=True):
        shown, partial = self.level(t)
        img = self.state_image(shown)
        if partial < 1 and shown:
            img = Image.blend(self.state_image(shown - 1), img, partial)
        else:
            img = img.copy()
        if captions:
            self.draw_caption(ImageDraw.Draw(img), t)
        alpha = min(1.0, t / FADE)
        if alpha < 1:
            img = Image.blend(Image.new("RGB", img.size, C["window"]), img, alpha)
        return img


class AppPage(Scene):
    """A page of the app: its screenshot (or a schematic) and the bullets beside it. "shot" names a state captured
    by capture_app.py; "zoom" [x0, y0, x1, y1] (fractions) shows only that part of it, larger; "focus" frames a
    part of it. Without bullets the picture takes the whole width."""

    def __init__(self, *args, scene, shots, **kwargs):
        super().__init__(*args, **kwargs)
        self.scene = scene
        self.items = scene.get("bullets", [])
        self.content_seconds = 2 + 0.8 * len(self.items) + 4
        self.shot = None
        if shots:
            names = [f"{scene['shot']}-{self.lang}.png"] if scene.get("shot") else []
            names += [f"{scene['page']}-{self.lang}.png", f"{scene['page']}.png"]
            for name in names:
                if (Path(shots) / name).is_file():
                    self.shot = Path(shots) / name
                    break
            # a state not captured shows the page itself, without the frame or zoom meant for the state
            self.region = scene.get("zoom") if self.shot and self.shot.name.startswith(scene.get("shot") or
                                                                                         scene["page"]) else None
            self.focus = scene.get("focus") if self.region is None and self.shot and self.shot.name.startswith(
                scene.get("shot") or scene["page"]) else None
        else:
            self.region = self.focus = None

    def reveal(self, i):
        return 1.0 + i * 0.8

    def dynamic_key(self, t):
        return sum(1 for i in range(len(self.items)) if t >= self.reveal(i))

    def box(self):
        cv = self.cv
        if self.items:
            return (cv.s(80), cv.s(205), cv.s(1310), cv.s(205 + 700))
        return (cv.s(80), cv.s(195), cv.s(1840), cv.s(195 + 715))

    def draw_static(self, img, draw):
        cv = self.cv
        self.draw_heading(draw, self.t(self.scene["heading"]))
        box = self.box()
        if self.shot:
            shot = Image.open(self.shot).convert("RGB")
            frame = None
            if self.region:
                x0, y0, x1, y1 = self.region
                shot = shot.crop((int(x0 * shot.width), int(y0 * shot.height), int(x1 * shot.width),
                                  int(y1 * shot.height)))
            elif self.focus:
                frame = self.focus
            width, height = box[2] - box[0], box[3] - box[1]
            scale = min(width / shot.width, height / shot.height)
            shot = shot.resize((max(1, int(shot.width * scale)), max(1, int(shot.height * scale))), Image.LANCZOS)
            x = box[0] + (width - shot.width) // 2
            y = box[1]
            if frame:
                shot = self.framed(shot, frame)
            img.paste(shot, (x, y))
            draw.rectangle((x - 1, y - 1, x + shot.width, y + shot.height), outline=C["dim"], width=cv.s(2))
            bottom = y + shot.height
            badge = "screenshot"
            left = x
        else:
            self.draw_schematic(draw, box)
            bottom = box[3]
            badge = "schematic"
            left = box[0]
        label = self.t(SB.COURSE["badges"][badge])
        colour = C["ok"] if badge == "screenshot" else C["warn"]
        draw.text((left, bottom + cv.s(8)), "● " + label, font=cv.font("sans", 20), fill=colour)

    def framed(self, shot, frame):
        """SHOT with the part FRAME (fractions) framed in the accent colour and the rest a little darker."""
        cv = self.cv
        x0, y0, x1, y1 = (int(frame[0] * shot.width), int(frame[1] * shot.height), int(frame[2] * shot.width),
                          int(frame[3] * shot.height))
        dim = Image.blend(shot, Image.new("RGB", shot.size, (0, 0, 0)), 0.45)
        dim.paste(shot.crop((x0, y0, x1, y1)), (x0, y0))
        draw = ImageDraw.Draw(dim)
        pad = cv.s(4)
        draw.rounded_rectangle((x0 - pad, y0 - pad, x1 + pad, y1 + pad), radius=cv.s(8), outline=C["accent"],
                               width=cv.s(4))
        return dim

    def draw_schematic(self, draw, box):
        cv = self.cv
        x0, y0, x1, y1 = box
        rounded(draw, box, cv.s(10), fill=C["window"], outline=C["dim"], width=cv.s(2))
        nav = x0 + cv.s(250)
        draw.rectangle((x0 + cv.s(2), y0 + cv.s(2), nav, y1 - cv.s(2)), fill=C["nav"])
        draw.text((x0 + cv.s(20), y0 + cv.s(16)), "sandbox-ai", font=cv.font("bold", 24), fill=C["title"])
        y = y0 + cv.s(62)
        for section, pages in SB.APP_NAV:
            draw.text((x0 + cv.s(20), y), self.t(section).upper(), font=cv.font("sans", 15), fill=C["secondary"])
            y += cv.s(26)
            for key, title in pages:
                if key == self.scene["page"]:
                    rounded(draw, (x0 + cv.s(10), y - cv.s(4), nav - cv.s(10), y + cv.s(26)), cv.s(6), fill=C["nav_selected"])
                draw.text((x0 + cv.s(28), y), self.t(title), font=cv.font("sans", 18),
                          fill=C["nav_selected_text"] if key == self.scene["page"] else C["text"])
                y += cv.s(31)
            y += cv.s(8)
        title = self.t(SB.APP_PAGES[self.scene["page"]])
        draw.text((nav + cv.s(30), y0 + cv.s(22)), title, font=cv.font("bold", 34), fill=C["title"])
        yy = y0 + cv.s(100)
        for i in range(7):
            w = (x1 - nav - cv.s(60)) * (0.9 if i % 3 else 0.6)
            rounded(draw, (nav + cv.s(30), yy, nav + cv.s(30) + w, yy + cv.s(46)), cv.s(8), fill=C["base"],
                    outline=C["dim"], width=1)
            yy += cv.s(70)

    def draw_dynamic(self, img, draw, t):
        cv = self.cv
        y = cv.s(225)
        for i, item in enumerate(self.items):
            if t < self.reveal(i):
                break
            draw.rounded_rectangle((cv.s(1350), y + cv.s(14), cv.s(1364), y + cv.s(28)), radius=cv.s(4), fill=C["accent"])
            lines = layout(cv, self.t(item), 30, cv.s(460))
            y = draw_lines(draw, lines, cv.s(1385), y, cv.s(41), C["text"], C["accent"]) + cv.s(24)


class SeriesTitle(Scene):
    """The title card of a series video: the series, the part, the page's name and place, its topics."""
    header = False
    content_seconds = 5.0

    def __init__(self, *args, series, video, **kwargs):
        super().__init__(*args, **kwargs)
        self.series, self.video = series, video

    def draw_static(self, img, draw):
        cv = self.cv
        index, count, title = self.track
        x = cv.s(180)
        label = f"{self.t(self.series['title'])}  ·  {self.t(self.series['part'])} {index + 1} {self.t(SB.COURSE['of'])} {count}"
        draw.text((x, cv.s(200)), label.upper(), font=cv.font("bold", 28), fill=C["secondary"])
        draw.text((x - cv.s(8), cv.s(240)), f"{index + 1:02d}", font=cv.font("bold", 170), fill=C["accent"])
        draw.text((x, cv.s(450)), title, font=cv.font("bold", 84), fill=C["title"])
        section = next(self.t(name) for name, pages in SB.APP_NAV if any(k == self.video["page"] for k, _ in pages))
        draw.text((x, cv.s(560)), f"sandbox-ai-app  ›  {section}  ›  {title}", font=cv.font("sans", 30),
                  fill=C["accent"])
        y = cv.s(640)
        for topic in self.video["topics"]:
            draw.ellipse((x, y + cv.s(13), x + cv.s(12), y + cv.s(25)), fill=C["accent"])
            draw_lines(draw, layout(cv, self.t(topic), 34, cv.s(1500)), x + cv.s(32), y, cv.s(46), C["text"], C["accent"])
            y += cv.s(54)
        line = f"sandbox-ai {version()}"
        f = cv.font("mono", 24)
        draw.text((cv.w - cv.s(180) - f.getlength(line), cv.s(210)), line, font=f, fill=C["secondary"])


class SeriesOutro(Bullets):
    """The closing card of a series video: what comes next, and where to read more."""

    def __init__(self, *args, series, following, **kwargs):
        heading = (f"{SB.text(series['next'], args[1])}: {following}" if following
                   else SB.text(series["last"], args[1]))
        super().__init__(*args, scene={"heading": heading, "bullets": series["outro_bullets"]}, **kwargs)
        self.content_seconds = 1.5 + 0.8 * len(self.items) + 2.5


class Outro(Bullets):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, scene={"heading": SB.COURSE["outro"]["heading"], "bullets": SB.COURSE["outro"]["bullets"]},
                         **kwargs)


# --- the timeline ------------------------------------------------------------------------------------------


def make_scene(args, scene, live, shots, ident, track=None):
    kind = scene["kind"]
    kwargs = {"track": track} if track else {}
    if kind == "bullets":
        return Bullets(*args, scene=scene, **kwargs)
    if kind == "table":
        return Table(*args, scene=scene, **kwargs)
    if kind == "compare":
        return Compare(*args, scene=scene, **kwargs)
    if kind == "terminal":
        return Terminal(*args, scene=scene, live=live, **kwargs)
    if kind == "diagram":
        return Diagram(*args, scene=scene, **kwargs)
    if kind == "app":
        return AppPage(*args, scene=scene, shots=shots, **kwargs)
    raise SystemExit(f"make_video: unknown scene kind {kind!r} in {ident}")


def timeline(scenes, speaker=None):
    """Plan SCENES one after the other; with SPEAKER (narrate.Speaker) their narration is spoken and sets the time."""
    if speaker is not None:
        for scene in scenes:
            scene.voice = [speaker.say(SB.text(caption, scene.lang)) for caption in scene.captions]
    start = 0.0
    for scene in scenes:
        scene.plan()
        scene.start = start
        start += scene.duration
    return scenes


def build(cv, lang, live, shots, speaker=None):
    scenes = [CourseTitle(cv, lang, None, SB.COURSE["intro_captions"], "intro", "intro"),
              Contents(cv, lang, None, SB.COURSE["contents_captions"], "contents", "contents")]
    for ci, chapter in enumerate(SB.CHAPTERS):
        scenes.append(ChapterTitle(cv, lang, ci, [], chapter["id"], f"{chapter['id']}/title"))
        for index, scene in enumerate(chapter["scenes"]):
            ident = SB.scene_id(chapter, index)
            scenes.append(make_scene((cv, lang, ci, scene.get("captions", []), chapter["id"], ident), scene, live,
                                     shots, ident))
    scenes.append(Outro(cv, lang, None, SB.COURSE["outro"]["captions"], "outro", "outro"))
    return timeline(scenes, speaker)


def build_series(cv, lang, shots, video, name="pages", speaker=None):
    """The scenes of one VIDEO of series NAME: its title card, its scenes, the closing card."""
    series = SB.SERIES[name]
    videos = SB.series_videos(name)
    index = videos.index(video)
    title = SB.text(SB.APP_PAGES[video["page"]], lang)
    track = (index, len(videos), title)
    following = SB.text(SB.APP_PAGES[videos[index + 1]["page"]], lang) if index + 1 < len(videos) else None
    scenes = [SeriesTitle(cv, lang, None, video["intro"], video["id"], f"{video['id']}/title", track=track,
                          series=series, video=video)]
    for number, scene in enumerate(video["scenes"]):
        ident = f"{video['id']}/{number}"
        scenes.append(make_scene((cv, lang, None, scene.get("captions", []), video["id"], ident), scene, {}, shots,
                                 ident, track))
    scenes.append(SeriesOutro(cv, lang, None, series["outro_captions"], video["id"], f"{video['id']}/outro",
                              track=track, series=series, following=following))
    return timeline(scenes, speaker)


def series_file(video, lang, name="pages"):
    """The base name of a series video's files: sandbox-ai-app-NN-PAGE-LANG."""
    number = SB.series_videos(name).index(video) + 1
    return f"sandbox-ai-app-{number:02d}-{video['id']}-{lang}"


def timestamp(seconds):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def write_vtt(path, scenes, lang):
    out = ["WEBVTT", ""]
    number = 0
    for scene in scenes:
        for a, b, caption in scene.cues:
            number += 1
            out += [str(number), f"{timestamp(scene.start + a)} --> {timestamp(scene.start + b)}",
                    SB.text(caption, lang).replace("`", ""), ""]
    Path(path).write_text("\n".join(out))


def write_chapters(path, scenes, lang):
    out = ["WEBVTT", ""]
    marks = [(scene.start, scene.chapter_index) for scene in scenes if isinstance(scene, ChapterTitle)]
    end = scenes[-1].start + scenes[-1].duration
    out += ["0", f"{timestamp(0)} --> {timestamp(marks[0][0])}", SB.text(SB.COURSE["contents"], lang), ""]
    for i, (start, ci) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else end
        out += [str(ci + 1), f"{timestamp(start)} --> {timestamp(stop)}",
                f"{ci + 1}. {SB.text(SB.CHAPTERS[ci]['title'], lang)}", ""]
    Path(path).write_text("\n".join(out))


def find_ffmpeg(explicit):
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise SystemExit("make_video: no ffmpeg: pip install imageio-ffmpeg (in a throwaway venv), install ffmpeg, "
                     "or pass --ffmpeg PATH")


def soundtrack(scenes, path):
    """One WAV file with every spoken cue of SCENES at its time (silence between them); None without narration."""
    sounds = [(scene.start + at, wav) for scene in scenes for at, wav in scene.sounds]
    if not sounds:
        return None
    with wave.open(str(sounds[0][1]), "rb") as first:
        rate, width, channels = first.getframerate(), first.getsampwidth(), first.getnchannels()
    total = scenes[-1].start + scenes[-1].duration
    data = bytearray(int(total * rate + 1) * width * channels)
    for at, wav in sounds:
        with wave.open(str(wav), "rb") as f:
            if (f.getframerate(), f.getsampwidth(), f.getnchannels()) != (rate, width, channels):
                raise SystemExit(f"make_video: {wav} differs in format from the other narration")
            frames = f.readframes(f.getnframes())
        offset = int(at * rate) * width * channels
        frames = frames[:max(0, len(data) - offset)]
        data[offset:offset + len(frames)] = frames
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(bytes(data))
    return path


def render(scenes, cv, lang, out, ffmpeg, fps, captions, crf, stills, preset, name=None, title=None):
    name = name or f"sandbox-ai-course-{lang}"
    path = Path(out) / f"{name}.mp4"
    total = scenes[-1].start + scenes[-1].duration
    frames = int(round(total * fps))
    audio = soundtrack(scenes, Path(out) / f".{name}.narration.wav")
    command = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{cv.w}x{cv.h}",
               "-r", str(fps), "-i", "-"]
    if audio:
        command += ["-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "128k",
                    "-metadata:s:a:0", f"language={'eng' if lang == 'en' else 'deu'}"]
    command += ["-c:v", "libx264", "-preset", preset, "-tune", "stillimage", "-crf", str(crf),
                # mostly still pictures: a short lookahead keeps ffmpeg's memory small, few threads suffice
                "-x264-params", "rc-lookahead=10", "-threads", "4",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-metadata", f"title=sandbox-ai {version()}: {title or SB.text(SB.COURSE['subtitle'], lang)}",
                "-metadata:s:v:0", f"language={'eng' if lang == 'en' else 'deu'}", str(path)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    wanted = {}
    for ident in stills or []:
        for scene in scenes:
            if scene.ident == ident:
                wanted[int((scene.start + scene.duration * 0.82) * fps)] = ident
    index, last_key, last_bytes, began, rendered = 0, None, None, time.time(), 0
    try:
        for scene in scenes:
            end = int(round((scene.start + scene.duration) * fps))
            while index < end:
                t = index / fps - scene.start
                key = (scene.ident, scene.key(t))
                if key != last_key or index in wanted:
                    image = scene.frame(t, captions)
                    last_bytes, last_key = image.tobytes(), key
                    rendered += 1
                    if index in wanted:
                        image.save(Path(out) / f"still-{lang}-{wanted[index].replace('/', '-')}.png")
                process.stdin.write(last_bytes)
                index += 1
                if index % (fps * 60) == 0:
                    print(f"  {lang}: {index / fps / 60:.0f} of {total / 60:.1f} min, {rendered} frames drawn, "
                          f"{time.time() - began:.0f} s", flush=True)
            scene._base = None
            if hasattr(scene, "states"):
                scene.states = {}
        process.stdin.close()
    except BrokenPipeError:
        pass
    status = process.wait()
    if audio:
        audio.unlink()
    if status != 0:
        raise SystemExit(f"make_video: ffmpeg failed for {path}")
    return path, frames, total


def plan_report(scenes):
    total = scenes[-1].start + scenes[-1].duration
    current = None
    for scene in scenes:
        if scene.label != current:
            current = scene.label
            chapter = [s for s in scenes if s.label == current]
            length = sum(s.duration for s in chapter)
            print(f"{timestamp(scene.start)}  {current:16s} {length:6.1f} s  ({len(chapter)} scenes)")
    print(f"total {total / 60:.1f} min ({total:.0f} s)")
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="output directory (created)")
    parser.add_argument("--lang", choices=["en", "de", "both"], default="both", help="language of the video(s) (default: both)")
    parser.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT, 16:9 (default 1920x1080; 1280x720 renders faster)")
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--crf", type=int, default=20, help="x264 quality (lower is better; default 20)")
    parser.add_argument("--preset", default="veryfast", help="x264 preset (default veryfast; slower ones save little "
                        "on still pictures)")
    parser.add_argument("--captions", choices=["burn", "off"], default="burn",
                        help="burn the narration into the video (default) or leave it to the .vtt files")
    parser.add_argument("--app-shots", help="screenshots of the desktop app from capture_app.py (default: OUT/app-shots)")
    parser.add_argument("--no-live", action="store_true", help="do not run commands; show their fallback text")
    parser.add_argument("--live-from", help=argparse.SUPPRESS)  # live output captured by the parent process
    parser.add_argument("--stills", action="store_true", help="also save the storyboard's STILLS as PNG")
    parser.add_argument("--plan", action="store_true", help="print the timing and exit (needs no Pillow, runs nothing)")
    parser.add_argument("--ffmpeg", help="ffmpeg executable")
    parser.add_argument("--font-dir", help="a folder with DejaVuSans.ttf, DejaVuSans-Bold.ttf, DejaVuSansMono.ttf ...")
    parser.add_argument("--jobs", type=int, default=2, help="render the languages (and the videos of a series) in "
                        "parallel (default 2)")
    parser.add_argument("--series", choices=sorted(SB.SERIES), help="render a series of short videos instead of the "
                        "course: pages = one video per page of the desktop app")
    parser.add_argument("--only", help="with --series: only these videos (ids, comma-separated, e.g. files,board)")
    parser.add_argument("--voice", action="append", default=[], metavar="VOICE.onnx",
                        help="speak the narration with this Piper voice (its .onnx.json beside it); the voice's "
                             "language decides which video it speaks; repeatable. The scenes then last as long as "
                             "their speech (see narrate.py)")
    parser.add_argument("--narration-cache", help="where the spoken cues are kept (default: OUT/.narration-cache)")
    parser.add_argument("--tts-threads", type=int, default=4, help="threads of the speech synthesis (default 4)")
    parser.add_argument("--speech-pace", type=float, default=1.08, help="Piper's length scale: above 1 speaks more slowly "
                        "(default 1.08, a calm pace for a tutorial)")
    args = parser.parse_args(argv)
    width, height = (int(v) for v in args.size.lower().split("x"))
    if abs(width / height - 16 / 9) > 0.01:
        parser.error("--size must be 16:9")
    langs = ["en", "de"] if args.lang == "both" else [args.lang]
    speakers = {}
    if args.voice:
        import narrate
        cache = Path(args.narration_cache or Path(args.out) / ".narration-cache")
        for voice in args.voice:
            speaker = narrate.Speaker(voice, cache, length_scale=args.speech_pace, threads=args.tts_threads)
            speakers[speaker.language] = speaker
    videos = None
    if args.series:
        videos = SB.series_videos(args.series)
        if args.only:
            wanted = args.only.split(",")
            unknown = set(wanted) - {v["id"] for v in videos}
            if unknown:
                parser.error(f"--only: no video {', '.join(sorted(unknown))} in the series {args.series}")
            videos = [v for v in videos if v["id"] in wanted]
    if args.plan:
        if videos is None:
            plan_report(build(_PlanCanvas(), langs[0] if speakers else "en", {}, None, speakers.get(langs[0])))
            return 0
        whole = 0.0
        for lang in langs:
            for video in videos:
                scenes = build_series(_PlanCanvas(), lang, None, video, args.series, speakers.get(lang))
                length = scenes[-1].start + scenes[-1].duration
                whole += length
                spoken = "spoken" if lang in speakers else "read"
                print(f"{series_file(video, lang, args.series):40s} {length / 60:4.1f} min ({length:5.1f} s, "
                      f"{len(scenes)} scenes, {spoken})")
        print(f"total {whole / 60:.1f} min")
        return 0
    if Image is None:
        raise SystemExit("make_video: Pillow is missing: python3 -m venv /tmp/video-venv && "
                         "/tmp/video-venv/bin/pip install pillow imageio-ffmpeg")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(args.ffmpeg)
    shots = args.app_shots or (out / "app-shots" if (out / "app-shots").is_dir() else None)
    if videos is not None:
        return render_series(args, langs, videos, speakers, shots, ffmpeg, out, width, height)

    live, live_file = {}, None
    if args.live_from:
        live = json.loads(Path(args.live_from).read_text())
    elif not args.no_live:
        runner = Live()
        try:
            print("make_video: running the live commands in a throwaway HOME …", flush=True)
            live = runner.capture()
        finally:
            runner.remove()
        for warning in runner.warnings:
            print(f"make_video: warning: {warning}", file=sys.stderr)

    if len(langs) > 1 and args.jobs > 1:
        handle, live_file = tempfile.mkstemp(prefix="sai-video-live-", suffix=".json")
        with os.fdopen(handle, "w") as f:
            json.dump(live, f)
        children = []
        try:
            for lang in langs:
                child = [sys.executable, str(Path(__file__).resolve()), "--out", str(out), "--lang", lang,
                         "--size", args.size, "--fps", str(args.fps), "--crf", str(args.crf), "--preset", args.preset,
                         "--captions", args.captions, "--live-from", live_file]
                child += child_flags(args, shots, ffmpeg)
                children.append(subprocess.Popen(child))
            codes = [c.wait() for c in children]
        finally:
            os.unlink(live_file)
        return max(codes)

    cv = Canvas(width, height, args.font_dir)
    for lang in langs:
        scenes = build(cv, lang, live, shots, speakers.get(lang))
        print(f"make_video: rendering {lang} ({width}x{height}, {args.fps} fps) …", flush=True)
        path, frames, total = render(scenes, cv, lang, out, ffmpeg, args.fps, args.captions == "burn", args.crf,
                                     SB.STILLS if args.stills else None, args.preset)
        write_vtt(out / f"sandbox-ai-course-{lang}.vtt", scenes, lang)
        write_chapters(out / f"sandbox-ai-course-chapters-{lang}.vtt", scenes, lang)
        print(f"make_video: {path} ({total / 60:.1f} min, {frames} frames, {path.stat().st_size / 1e6:.1f} MB)")
    return 0


def child_flags(args, shots, ffmpeg):
    """The options a child process renders with besides its language (and video)."""
    flags = []
    for flag, value in (("--app-shots", shots), ("--ffmpeg", ffmpeg), ("--font-dir", args.font_dir),
                        ("--narration-cache", args.narration_cache), ("--tts-threads", args.tts_threads),
                        ("--speech-pace", args.speech_pace)):
        if value:
            flags += [flag, str(value)]
    for voice in args.voice:
        flags += ["--voice", voice]
    if args.stills:
        flags.append("--stills")
    return flags


def render_series(args, langs, videos, speakers, shots, ffmpeg, out, width, height):
    """Every video of the series in every language: each an MP4 and its .vtt. The narration is spoken first (here,
    once), then the videos are rendered, --jobs at a time."""
    jobs = [(lang, video) for lang in langs for video in videos]
    for lang in langs:
        if lang in speakers:
            print(f"make_video: speaking the {lang} narration of {len(videos)} videos …", flush=True)
            for video in videos:
                build_series(_PlanCanvas(), lang, None, video, args.series, speakers[lang])
    if len(jobs) > 1 and args.jobs > 1:
        waiting, running, codes = list(jobs), [], []
        while waiting or running:
            while waiting and len(running) < args.jobs:
                lang, video = waiting.pop(0)
                child = [sys.executable, str(Path(__file__).resolve()), "--out", str(out), "--lang", lang,
                         "--size", args.size, "--fps", str(args.fps), "--crf", str(args.crf), "--preset", args.preset,
                         "--captions", args.captions, "--series", args.series, "--only", video["id"], "--jobs", "1"]
                running.append(subprocess.Popen(child + child_flags(args, shots, ffmpeg)))
            time.sleep(0.5)
            for child in [c for c in running if c.poll() is not None]:
                codes.append(child.returncode)
                running.remove(child)
        return max(codes)
    cv = Canvas(width, height, args.font_dir)
    for lang, video in jobs:
        scenes = build_series(cv, lang, shots, video, args.series, speakers.get(lang))
        name = series_file(video, lang, args.series)
        title = f"{SB.text(SB.SERIES[args.series]['title'], lang)}: {SB.text(SB.APP_PAGES[video['page']], lang)}"
        stills = [scene.ident for scene in scenes] if args.stills else None
        path, frames, total = render(scenes, cv, lang, out, ffmpeg, args.fps, args.captions == "burn", args.crf,
                                     stills, args.preset, name=name, title=title)
        write_vtt(out / f"{name}.vtt", scenes, lang)
        spoken = ", spoken" if lang in speakers else ""
        print(f"make_video: {path} ({total / 60:.1f} min{spoken}, {path.stat().st_size / 1e6:.1f} MB)", flush=True)
    return 0


class _PlanCanvas:
    """Enough of a Canvas to compute the timing without Pillow (--plan)."""
    w, h, k = 1920, 1080, 1.0

    def s(self, value):
        return int(value)

    def font(self, kind, size):
        return _PlanFont(size)


class _PlanFont:
    def __init__(self, size):
        self.size = size

    def getlength(self, text):
        return len(text) * self.size * 0.6


if __name__ == "__main__":
    sys.exit(main())
