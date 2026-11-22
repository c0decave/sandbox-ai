"""The Files page's Markdown viewer: a file of a pane rendered as Markdown (CommonMark and GitHub's tables, task lists,
strikethrough and autolinks; footnotes made into links; front matter, alerts and math as GitHub shows them:
markdown_text.py), headings in an outline beside it to jump to (the heading being read marked in it), fenced code
highlighted (Copy code block on its menu), images beside the file shown, zoom, and the editor's find bar
(file_editor.py). The editor's text view shows the same rendering as a live preview beside the text.

What is shown may have been written by an agent, so it is untrusted:

- Qt renders it (QTextDocument.setMarkdown) without HTML: tags are shown as the text they are, nothing runs, no
  script, no style sheet, no frame;
- nothing is loaded from the network, ever: a remote image is a placeholder that names its URL; the browser's own
  loading is replaced (MarkdownBrowser.loadResource answers from what this module read, nothing else);
- an image beside the file is read through the pane's files.Root like every other file (relative to open folders,
  never through a symbolic link, never above the root - and for a file of a sandbox's folder shown in the host pane,
  never outside that folder: ImageSource's top), in a thread, at most IMAGE_LIMIT bytes and IMAGE_PIXELS pixels, at
  most IMAGES of them and IMAGES_PIXELS pixels together; Reload reads them again; an SVG image that names anything
  but a part of itself is not shown (Qt's SVG reader would read the files it names, past the Root: svg_refers), nor
  one whose references draw its parts again and again (a few KB drew a billion paths: svg_draws_too_much);
- what Qt cannot lay out in time is not rendered: the rendering ends before the line that goes past LIST_ITEMS list
  items, TABLE_ROWS table rows or LONG_RUN characters without a space, and says so (renderable);
- a link is never followed inside the viewer: "#heading" scrolls there, a relative link to another Markdown file of
  the same root opens that file in a viewer of its own, an http(s) link opens in the browser only after the human
  said so (the window asks, naming the address as it is sent - its host in punycode, controls visible - and the
  link's text when that differs: last_link_text); anything else is named, not opened.
"""
from __future__ import annotations

import base64
import bisect
import posixpath
import re
import threading
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QImage, QImageReader, QPainter, QPalette, QSyntaxHighlighter, QTextCharFormat, QTextCursor,
    QTextDocument, QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication, QListWidget, QListWidgetItem, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from . import markdown_text, theme
from .files import printable
from .i18n import tr
from .widgets import background, plain_label

SUFFIXES = (".md", ".markdown", ".mdown", ".mkd")
FEATURES = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
IMAGE_LIMIT = 8 << 20  # bytes of one image
IMAGE_PIXELS = 40_000_000  # pixels of one image (a small file may say it is huge)
# Pixels of all the images of one document together (its views, until Reload): each one bounded, a small PNG of
# 36 million pixels named 200 times ("big.png?1", "./big.png", ...) was decoded 200 times - 28 GB.
IMAGES_PIXELS = 100_000_000
IMAGES = 200  # images of one document that are read
RENDER_DELAY = 300  # ms: the live preview follows the typing after this pause
# What Qt lays out in time (renderable): it numbers each item of a list and lays out each row of a table in time that
# grows with the list or table (20,000 items: 16 s, 20,000 rows: 24 s), and wraps a line without a space in time that
# grows with its square (1 MB: over a minute) - a file an agent wrote held up the window. The rendering stops before
# the line that goes past one of these (Edit shows the whole text).
LIST_ITEMS = 3000
TABLE_ROWS = 2000
LONG_RUN = 100_000  # characters without a space (file_editor's Wrap lines too)
LONG_WORD = re.compile(r"(?<!\S)\S{%d}" % LONG_RUN)  # (from where a run begins only: never quadratic)
LIST_ITEM = re.compile(r"^[ \t>]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")
REMOTE = ("http", "https", "ftp", "ftps", "ws", "wss", "smb", "sftp", "ssh")


def is_markdown(name):
    """Whether the file NAME is Markdown by its name (.md, .markdown, .mdown, .mkd; any case)."""
    return str(name).lower().endswith(SUFFIXES)


def slug(text):
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces as dashes."""
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
NOTE_DEFINITION = re.compile(r"^\[\^([^\]\s]+)\]:\s?(.*)$")
NOTE_REFERENCE = re.compile(r"\[\^([^\]\s]+)\](?!:)")


def with_footnotes(text):
    """TEXT with its footnotes ("text[^1]", "[^1]: the note") as Qt shows them: a reference becomes a link "[1]" to
    "#fn-1", the notes go to the end, each a paragraph that begins with "[1]". Nothing inside fenced code changes."""
    lines, notes, fence = [], [], None
    for line in text.split("\n"):
        opened = FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            lines.append(line)
            continue
        if opened:
            fence = opened.group(1)
            lines.append(line)
            continue
        definition = NOTE_DEFINITION.match(line)
        if definition:
            notes.append(definition.groups())
            continue
        lines.append(NOTE_REFERENCE.sub(lambda m: f"[\\[{m.group(1)}\\]](#fn-{m.group(1)})", line))
    if notes:
        lines += ["", "---", ""]
        for name, note in notes:
            lines += [f"\\[{name}\\] {note}", ""]
    return "\n".join(lines), [name for name, _ in notes]


def renderable(text):
    """(the part of TEXT rendered, why the rest is not - or "") - see LIST_ITEMS: the lines before the one that goes
    past LIST_ITEMS list items or TABLE_ROWS lines of tables (outside fenced code), or holds LONG_RUN characters without a
    space."""
    if not LONG_WORD.search(text) and text.count("|") < TABLE_ROWS and text.count("\n") < LIST_ITEMS:
        return text, ""  # (most texts: nothing to count)
    items = rows = 0
    fence, lines = None, text.split("\n")
    for number, line in enumerate(lines):
        why = ""
        if len(line) >= LONG_RUN and LONG_WORD.search(line):
            why = tr("a line of more than {count} characters without a space", count=LONG_RUN)
        opened = FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
        elif opened:
            fence = opened.group(1)
        elif LIST_ITEM.match(line):
            items += 1
            if items > LIST_ITEMS:
                why = tr("lists of more than {count} items", count=LIST_ITEMS)
        elif "|" in line:
            rows += 1
            if rows > TABLE_ROWS:
                why = tr("tables of more than {count} rows", count=TABLE_ROWS)
        if why:
            return "\n".join(lines[:number]), tr(
                "Only the first {count} of {total} lines are rendered: more would hold up the window ({why}). The "
                "text shows them all.", count=number, total=len(lines), why=why)
    return text, ""


# -- fenced code --

KEYWORDS = set("""and as assert async await break case catch class const continue def default del delete do elif else
enum except export extends false final finally fn for foreach from func function go if impl import in interface is
lambda let loop match mod module mut namespace new nil none not null or package pass private protected pub public
raise return self static struct super switch this throw true try type typeof use var void while with yield then fi
done esac local echo""".split())
TOKENS = re.compile(r"""(?P<comment>\#.*$|//.*$|--\s.*$)|(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`)"""
                    r"""|(?P<number>\b\d[\d_.xXa-fA-F]*\b)|(?P<word>\b[A-Za-z_]\w*\b)""")


class CodeHighlighter(QSyntaxHighlighter):
    """Colours in fenced code blocks (any language: comments, strings, numbers, common keywords), in the look's
    colours - made once a rendering (MarkdownView.render forgets them: a look chosen since is taken then)."""
    formats = None

    def colours(self):
        """{kind: QTextCharFormat} in the look's colours. (Made anew for every line of code, they were most of the
        time a rendering took: a code block of 150,000 lines held up the window for 4 s.)"""
        if self.formats is None:
            self.formats = {}
            for kind, name in (("comment", "muted"), ("string", "ok"), ("number", "warn"), ("word", "accent")):
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(theme.color(name) if name != "accent" else theme.colors().get(
                    "accent", theme.color("warn"))))
                if kind == "word":
                    fmt.setFontWeight(QFont.Weight.Bold)
                self.formats[kind] = fmt
        return self.formats

    def highlightBlock(self, text):  # noqa: N802
        block = self.currentBlock()
        if not block.blockFormat().hasProperty(QTextFormat.Property.BlockCodeFence):
            return
        formats = self.colours()
        for found in TOKENS.finditer(text):
            kind = found.lastgroup
            if kind == "word" and found.group() not in KEYWORDS:
                continue
            self.setFormat(found.start(), found.end() - found.start(), formats[kind])


# -- images --

def placeholder(text, palette=None):
    """A box naming what is not shown (a remote image and its URL, an image too big or not found)."""
    palette = palette or QPalette()
    width, height = 420, 36
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(palette.color(QPalette.ColorRole.AlternateBase))
    painter = QPainter(image)
    painter.setPen(palette.color(QPalette.ColorRole.Mid))
    painter.drawRect(0, 0, width - 1, height - 1)
    painter.setPen(palette.color(QPalette.ColorRole.Text))
    metrics = painter.fontMetrics()
    painter.drawText(QRect(8, 0, width - 16, height), Qt.AlignmentFlag.AlignVCenter,
                     metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, width - 16))
    painter.end()
    return image


# What an SVG image may hold (svg_refers): Qt's SVG reader reads the files its <image> elements name, by their paths on
# this computer - past the pane's Root, its links and its top -, so only an SVG that names nothing but parts of itself
# ("#id", url(#id)) is shown; one with an entity declared, an encoding in which ASCII's letters are other bytes, or a
# NUL (UTF-16: the names were not seen) is not either.
SVG_NAMES = re.compile(rb"""href\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
SVG_URLS = re.compile(rb"""url\s*\(\s*["']?\s*(?!#)""", re.IGNORECASE)
SVG_ENCODING = re.compile(rb"""<\?xml[^>]*?encoding\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
ASCII_ENCODINGS = {b"utf-8", b"utf8", b"us-ascii", b"ascii", b"iso-8859-1", b"latin1", b"latin-1"}
SVG_FORMATS = (b"svg", b"svgz")


def svg_refers(data):
    """Whether the SVG DATA (bytes, not compressed) may name another file (see SVG_NAMES)."""
    if b"\0" in data or re.search(rb"<!ENTITY|@import", data, re.IGNORECASE) or SVG_URLS.search(data):
        return True
    declared = SVG_ENCODING.search(data[:1024])
    if declared and declared.group(1).strip().lower().replace(b"_", b"-") not in ASCII_ENCODINGS:
        return True
    return any(not value.startswith(b"#") for _quote, value in SVG_NAMES.findall(data))


# What the references of an SVG (<use>, url(#id)) may draw again (svg_draws_too_much): each one draws its target - and
# the target's own references - once more, so ten levels of ten <use> in 1.9 KB drew a billion paths (seven levels: 6 s,
# every level ten times longer, in a thread nothing can stop). Counted are the elements drawn again and the
# characters of their attributes (one path of 10,000 segments drawn 1000 times is 1000 elements, 60 MB to draw).
SVG_REDRAWN = 20_000
SVG_REDRAWN_CHARS = 2_000_000
SVG_PAINTS = {"linearGradient", "radialGradient", "stop", "solidColor"}  # (a fill="url(#g)" of these: nothing drawn)
SVG_LOCAL_URL = re.compile(r"url\(\s*[\"']?\s*#([^)\"'\s]*)")


def svg_draws_too_much(data):
    """Why the SVG DATA (bytes, not compressed) is not drawn - its references draw more than SVG_REDRAWN elements or
    SVG_REDRAWN_CHARS characters of attributes again, refer to themselves, or it is no XML -, or None. One streaming
    parse (expat: no tree, no entity, no external DTD read) and one pass over its elements: time grows with the file,
    not with what it draws. An id named twice counts its biggest element (which one QtSvg draws is its own choice)."""
    from xml.parsers import expat

    ids, refs, weights, children, opened, paints = {}, [], [], {}, [], set()
    parser = expat.ParserCreate()

    def start(_name, attrs):
        index = len(refs)
        if opened:
            children.setdefault(opened[-1], []).append(index)
        own = []
        for key, value in attrs.items():
            local = key.rsplit(":", 1)[-1].lower()
            if local == "id":
                ids.setdefault(value, []).append(index)
            elif local == "href" and value.strip().startswith("#"):
                own.append(value.strip()[1:])
            elif "url(" in value:
                own += SVG_LOCAL_URL.findall(value)
        refs.append(own)
        weights.append(1 + sum(len(value) for value in attrs.values()))
        if _name.rsplit(":", 1)[-1] in SVG_PAINTS:
            paints.add(index)
        opened.append(index)

    def refuse(*_args):
        raise ValueError("an entity is declared")

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda _name: opened.pop()
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, ValueError) as exc:
        return f"not XML ({exc})"
    if not refs:
        return None
    # Per element, its subtree worked out once (after its children and its references' targets; a target still being
    # worked out is a cycle): what it draws itself (count, characters) and what its subtree's references draw again.
    caps = (SVG_REDRAWN + 1, SVG_REDRAWN_CHARS + 1)
    size, extra = [(0, 0)] * len(refs), [(0, 0)] * len(refs)
    state = bytearray(len(refs))  # 0 not seen, 1 being worked out, 2 done
    for root in range(len(refs)):
        if state[root]:
            continue
        stack = [root]
        while stack:
            node = stack[-1]
            targets = [t for name in refs[node] for t in ids.get(name, ())]
            depends = children.get(node, []) + targets
            if state[node] == 0:
                state[node] = 1
                for other in depends:
                    if state[other] == 1:
                        return "refers to itself"
                    if state[other] == 0:
                        stack.append(other)
                continue
            stack.pop()
            if state[node] == 2:
                continue
            if any(state[other] != 2 for other in depends):
                return "refers to itself"
            kids = children.get(node, ())
            size[node] = (0, 0) if node in paints else tuple(
                min(cap, own + sum(size[kid][part] for kid in kids))
                for part, (cap, own) in enumerate(zip(caps, (1, weights[node]))))
            drawn = [0, 0]
            for name in refs[node]:
                named = ids.get(name, ())
                if named:
                    biggest = max(named, key=lambda t: (size[t][1] + extra[t][1], size[t][0] + extra[t][0]))
                    drawn = [drawn[part] + size[biggest][part] + extra[biggest][part] for part in (0, 1)]
            extra[node] = tuple(min(caps[part], drawn[part] + sum(extra[kid][part] for kid in kids))
                                for part in (0, 1))
            state[node] = 2
    if extra[0][0] > SVG_REDRAWN or extra[0][1] > SVG_REDRAWN_CHARS:  # (the root's subtree: the whole drawing)
        return f"its references draw more than {SVG_REDRAWN} elements or {SVG_REDRAWN_CHARS} characters again"
    return None


def unpacked(data):
    """DATA as it is - or, compressed with gzip (an .svgz), unpacked: at most IMAGE_LIMIT bytes (None beyond, or when it
    does not unpack)."""
    if not data.startswith(b"\x1f\x8b"):
        return data
    import zlib
    unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        found = unpacker.decompress(data, IMAGE_LIMIT + 1)
    except zlib.error:
        return None
    return found if len(found) <= IMAGE_LIMIT and not unpacker.unconsumed_tail else None


def decode(data, budget=None):
    """A QImage of DATA (bytes), or (None, why): never more than IMAGE_PIXELS pixels are decoded - nor more than
    BUDGET["left"] (the pixels all the images of a document may still take: taken from it, under BUDGET["lock"]). An
    SVG that may name other files is not decoded (svg_refers)."""
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    if bytes(reader.format().data()).lower() in SVG_FORMATS or data.startswith(b"\x1f\x8b"):
        plain = unpacked(data)
        if plain is None:
            return None, tr("too big")
        if svg_refers(plain):
            return None, tr("refers to other files")
        if svg_draws_too_much(plain):
            return None, tr("draws too much")
        if plain is not data:  # (an .svgz: read as the SVG it holds - what was looked at)
            buffer.close()
            buffer.setData(QByteArray(plain))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buffer)
            if bytes(reader.format().data()).lower() not in SVG_FORMATS:
                return None, tr("not an image")
    size = reader.size()
    pixels = size.width() * size.height() if size.isValid() else 0
    if pixels > IMAGE_PIXELS:
        return None, tr("too big")
    if budget is not None:
        with budget["lock"]:  # (taken before it is decoded: the views of one window read at the same time)
            if pixels > budget["left"]:
                return None, tr("too big")
            budget["left"] -= pixels
    image = reader.read()
    found = image.width() * image.height() if not image.isNull() else 0
    if budget is not None and found != pixels:  # (what it took: a size not told before, or no image)
        with budget["lock"]:
            if found - pixels > budget["left"]:
                budget["left"] += pixels
                return None, tr("too big")
            budget["left"] -= found - pixels
    if image.isNull():
        return None, tr("not an image")
    if found > IMAGE_PIXELS:
        return None, tr("too big")
    return image, None


class ImageSource:
    """The images of the file RELATIVE of ROOT (a files.Root): where a name points (local(), below the root only - and
    below TOP, a folder relative to the root, when one is given) and reading them (read(), in a thread)."""

    def __init__(self, root, relative, top=""):
        self.root, self.folder = root, posixpath.dirname(relative)
        # (the host pane's root is "/": a Markdown file the agent wrote in its folder, viewed there, showed any image of
        # the host's its "../../.." named - only those of the sandbox's folder now)
        self.top = "/".join(part for part in top.split("/") if part)
        self.budget, self.lock = None, threading.Lock()  # (the pixels its images may still take: decode, forget)

    def forget(self):
        """Reload: the images are read anew - the pixels of those read before count no more."""
        with self.lock:
            self.budget = None

    def within(self, joined):
        """Whether the path JOINED (relative to the root, normalised) lies below TOP."""
        return not self.top or joined == self.top or joined.startswith(self.top + "/")

    def kind(self, name):
        """("remote", url) | ("data", None) | ("local", relative path below the root) | ("outside", name)."""
        url = QUrl(name)
        scheme = url.scheme().lower()
        if scheme in REMOTE or name.startswith("//"):
            return "remote", name
        if scheme == "data":
            return "data", None
        if scheme or name.startswith("/"):  # file:, an absolute path, anything else: never read
            return "outside", name
        path = QUrl.fromPercentEncoding(url.path(QUrl.ComponentFormattingOption.FullyEncoded).encode())
        joined = posixpath.normpath(posixpath.join(self.folder, path)) if path else ""
        if not path or joined == ".." or joined.startswith("../") or joined.startswith("/") or \
                not self.within("" if joined == "." else joined):
            return "outside", name
        return "local", "" if joined == "." else joined

    def read_later(self, names, done, owner=None):
        """read(NAMES) in a thread (never on the GUI thread: a slow or hung file system holds up no window);
        DONE(result, error) on the GUI thread."""
        # (a remote root's: no time limit as a whole - its requests have theirs; an image far away took longer than 60 s
        # and was said not to be there while it was still being read)
        return background(self.read, list(names), owner=owner, done=done,
                          timeout=None if getattr(self.root, "remote", False) else 60)

    def read(self, names):
        """{name: QImage or why not} for the local and data: images NAMES (Root.read_file; runs in read_later's
        thread)."""
        with self.lock:  # (a read begun before a Reload takes from what was left then)
            if self.budget is None:
                self.budget = {"left": IMAGES_PIXELS, "lock": threading.Lock()}
            budget, found = self.budget, {}
        for name in list(names)[:IMAGES]:
            kind, where = self.kind(name)
            try:
                if kind == "data":
                    head, _, body = name.partition(",")
                    if ";base64" not in head or len(body) > IMAGE_LIMIT * 4 // 3 + 4:
                        found[name] = tr("not shown")
                        continue
                    data = base64.b64decode(body, validate=False)
                elif kind == "local":
                    result = self.root.read_file(where, limit=IMAGE_LIMIT)
                    if result.truncated:
                        found[name] = tr("too big")
                        continue
                    data = result.data
                else:
                    continue
                image, why = decode(data, budget)
                found[name] = image if image is not None else why
            except (OSError, ValueError) as exc:
                found[name] = printable(exc)
        return found


ALERT_COLOURS = {"TIP": "ok", "WARNING": "warn", "CAUTION": "bad"}  # (a status colour; NOTE, IMPORTANT: the accent)


class MarkdownBrowser(QTextBrowser):
    """A QTextBrowser that loads nothing itself: images come from VIEW's cache (read through the pane's Root), every
    other resource is refused; links are never followed here (the view decides). Its menu has Copy code block on a
    fenced code block."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.pressed_at = None  # (where the mouse was pressed: the link clicked there - its text: MarkdownView.clicked)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setReadOnly(True)
        self.setAccessibleName(tr("The file rendered as Markdown"))

    def loadResource(self, kind, url):  # noqa: N802
        if kind != QTextDocument.ResourceType.ImageResource:
            return None  # (no style sheet, no frame, nothing else)
        return self.view.image(url.toString())

    def mousePressEvent(self, event):  # noqa: N802
        self.pressed_at = event.position().toPoint()
        super().mousePressEvent(event)

    def code_block_at(self, position):
        """The text of the fenced code block at the page's POSITION (its lines together), or None."""
        block = self.document().findBlock(position)
        fence = QTextFormat.Property.BlockCodeFence
        if not block.isValid() or not block.blockFormat().hasProperty(fence):
            return None
        first = block
        while first.previous().isValid() and first.previous().blockFormat().hasProperty(fence):
            first = first.previous()
        lines, block = [], first
        while block.isValid() and block.blockFormat().hasProperty(fence):
            lines.append(block.text())
            block = block.next()
        return "\n".join(lines)

    def menu_at(self, point):
        """The menu at POINT (viewport coordinates): Qt's own (Copy, Select all, a link's Copy) - and Copy code block on
        a code block."""
        menu = self.createStandardContextMenu(point)
        code = self.code_block_at(self.cursorForPosition(point).position())
        if code is not None:
            menu.addSeparator()
            menu.addAction(tr("Copy code block"), lambda: QApplication.clipboard().setText(code))
        return menu

    def contextMenuEvent(self, event):  # noqa: N802
        menu = self.menu_at(event.pos())
        menu.exec(event.globalPos())
        menu.deleteLater()


class MarkdownView(QWidget):
    """The rendering of a Markdown text: its outline (headings, a click jumps there) and the text. LINK(QUrl) for a link
    clicked that is not an anchor of this text (the window decides what it does). SOURCE: an ImageSource."""
    link = Signal(QUrl)

    def __init__(self, source, outline=True, parent=None):
        super().__init__(parent)
        self.source, self.markdown, self.anchors, self.zoom = source, "", {}, 0
        self.images, self.asked = {}, set()  # name: QImage (or a placeholder); names being read or read
        self.too_many = None  # the one picture of every image beyond 2 * IMAGES (image())
        self.generation = 0  # forget_images(): what a read begun before it brings is dropped
        self.headings = []  # the page's positions of the headings, in order (their rows in the outline)
        self.last_link_text = ""  # the text of the link clicked last (the window's question names it if it differs)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.outline = QListWidget()
        self.outline.setAccessibleName(tr("Outline"))
        self.outline.setToolTip(tr("The headings: a click shows that part"))
        self.outline.itemActivated.connect(self.jump)
        self.outline.itemClicked.connect(self.jump)
        self.browser = MarkdownBrowser(self)
        self.highlighter = CodeHighlighter(self.browser.document())
        self.browser.anchorClicked.connect(self.clicked)
        self.browser.highlighted.connect(self.hovered)
        self.browser.verticalScrollBar().valueChanged.connect(self.follow)
        self.split.addWidget(self.outline)
        self.split.addWidget(self.browser)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([180, 600])
        self.outline.setVisible(outline)
        layout.addWidget(self.split, 1)
        self.status = plain_label(role="secondary", word_wrap=False)
        layout.addWidget(self.status)
        self.notice = plain_label(role="secondary")  # (renderable: why only a part is rendered)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(RENDER_DELAY)
        self.timer.timeout.connect(lambda: self.render(self.markdown))

    # -- the text --

    def show_later(self, text):
        """TEXT rendered after a pause in the typing (the live preview)."""
        self.markdown = text
        self.timer.start()

    def render(self, text):
        """TEXT rendered (where it was scrolled to stays); its outline and anchors made anew."""
        self.timer.stop()
        self.markdown = text
        bar = self.browser.verticalScrollBar()
        where = bar.value()
        began = time.monotonic()
        part, why = renderable(text)
        self.notice.setText(why)
        self.notice.setVisible(bool(why))
        shown, notes = with_footnotes(markdown_text.prepared(part))
        self.highlighter.formats = None  # (the look's colours of now)
        self.browser.document().setMarkdown(shown, FEATURES)
        self.read_outline(notes)
        bar.setValue(where)
        self.follow()
        # The live preview waits for a pause in the typing at least twice as long as a rendering takes: a big text's
        # rendering (a second or more) after every pause of RENDER_DELAY left the typing stuck most of the time.
        self.timer.setInterval(max(RENDER_DELAY, int(2000 * (time.monotonic() - began))))
        self.read_missing()

    def read_outline(self, notes=()):
        """The outline and the anchors of the page made anew - and the title lines of its alerts coloured."""
        self.outline.clear()
        self.anchors, self.headings = {}, []
        titles, alerts = markdown_text.alert_titles(), []
        # (the number each name goes on from: counted up from 1 past every heading of that name before, 20,000
        # headings of one name held up the window for half a minute; and each paragraph compared with every note's
        # name, 10,000 footnotes for 15 s)
        numbers, notes = {}, set(notes)
        block = self.browser.document().begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            text = block.text()
            if level:
                key = slug(text)
                number, unique = numbers.get(key, 1), key
                while unique in self.anchors:  # (GitHub numbers headings of the same name)
                    unique, number = f"{key}-{number}", number + 1
                numbers[key] = number
                self.anchors[unique] = block.position()
                item = QListWidgetItem("    " * (level - 1) + text)
                item.setData(Qt.ItemDataRole.UserRole, block.position())
                self.outline.addItem(item)
                self.headings.append(block.position())
            elif text in titles and block.blockFormat().hasProperty(QTextFormat.Property.BlockQuoteLevel):
                alerts.append((block, titles[text]))
            if notes and text.startswith("[") and "] " in text:
                name = text[1:text.index("] ")]
                if name in notes:
                    self.anchors.setdefault(f"fn-{name}", block.position())
            block = block.next()
        for block, kind in alerts[:1000]:  # (in the look's colours, as GitHub's: a few - each one changes the page)
            status = ALERT_COLOURS.get(kind)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(theme.color(status) if status else theme.colors().get("accent",
                                                                                          theme.color("muted"))))
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(fmt)

    def follow(self, *_):
        """The heading being read (the last one above the view's top) marked in the outline as the page scrolls."""
        if not self.headings:
            return
        top = self.browser.cursorForPosition(self.browser.viewport().rect().topLeft()).position()
        row = max(0, bisect.bisect_right(self.headings, top) - 1)
        if self.outline.currentRow() != row:
            self.outline.blockSignals(True)
            self.outline.setCurrentRow(row)
            self.outline.blockSignals(False)

    def scroll_to(self, position):
        block = self.browser.document().findBlock(position)
        rect = self.browser.document().documentLayout().blockBoundingRect(block)
        self.browser.verticalScrollBar().setValue(int(rect.top()))
        cursor = QTextCursor(block)
        self.browser.setTextCursor(cursor)

    def jump(self, item):
        self.scroll_to(int(item.data(Qt.ItemDataRole.UserRole)))

    # -- links --

    def hovered(self, url):
        self.status.setText(tr("Link: {target}", target=printable(url.toString())) if url.toString() else "")

    def link_text_at(self, position, href):
        """The text of the link to HREF at the page's POSITION (all of its pieces in its line), or ""."""
        block = self.browser.document().findBlock(position)
        parts, inside, piece = [], False, block.begin()
        while not piece.atEnd():
            fragment = piece.fragment()
            fmt = fragment.charFormat()
            if fmt.isAnchor() and fmt.anchorHref() == href:
                parts.append(fragment.text())
                inside = inside or fragment.position() <= position <= fragment.position() + fragment.length()
            elif parts:
                if inside:
                    break
                parts = []
            piece += 1
        return "".join(parts).replace("\ufffc", "").strip() if inside else ""

    def clicked(self, url):
        """A link clicked: "#anchor" of this text scrolls there; anything else goes to the window (LINK) - its text in
        last_link_text."""
        pressed, self.browser.pressed_at = self.browser.pressed_at, None
        position = (self.browser.cursorForPosition(pressed).position() if pressed is not None
                    else self.browser.textCursor().position())
        self.last_link_text = self.link_text_at(position, url.toString()) or \
            self.link_text_at(max(0, position - 1), url.toString())
        if not url.scheme() and not url.path() and url.hasFragment():
            position = self.anchors.get(url.fragment()) or self.anchors.get(slug(url.fragment()))
            if position is None:
                self.status.setText(tr("No heading {anchor} here.", anchor=printable("#" + url.fragment())))
            else:
                self.scroll_to(position)
            return
        self.link.emit(url)

    # -- images --

    def image(self, name):
        """What the browser shows for the image NAME: read already, a placeholder, or (read in a moment) one that
        says it is being read."""
        found = self.images.get(name)
        if found is not None:
            return found
        kind, where = self.source.kind(name) if self.source is not None else ("outside", name)
        if kind in ("local", "data") and (name in self.asked or len(self.asked) < IMAGES):
            self.asked.add(name)  # (read by read_missing)
            return placeholder(tr("Reading {name}…", name=printable(name)), self.palette())
        if len(self.images) >= 2 * IMAGES:
            # (each name kept a picture of its own: a file of a few MiB naming 100,000 images - remote ones, or ones
            # beside it beyond the IMAGES read - took gigabytes; beyond these, one picture shared by all, kept by no
            # name)
            if self.too_many is None:
                self.too_many = placeholder(tr("Not shown (more than {count} images)", count=2 * IMAGES),
                                            self.palette())
            return self.too_many
        if kind == "remote":
            found = placeholder(tr("Remote image, not loaded: {url}", url=printable(where)), self.palette())
        elif kind == "outside":
            found = placeholder(tr("Not shown (outside the folder): {name}", name=printable(where)), self.palette())
        else:
            found = placeholder(tr("Not shown (more than {count} images): {name}", count=IMAGES,
                                   name=printable(name)), self.palette())
        self.images[name] = found
        return found

    def forget_images(self):
        """Read every image again at the next rendering (Reload: an image changed on disk kept its old picture)."""
        self.images, self.asked, self.too_many = {}, set(), None
        self.generation += 1
        if self.source is not None:
            self.source.forget()

    def read_missing(self):
        """Read (in a thread) the images asked for that are not read yet; render again once they are."""
        missing = [name for name in self.asked if name not in self.images]
        if not missing or self.source is None:
            return
        for name in missing:
            self.images[name] = placeholder(tr("Reading {name}…", name=printable(name)), self.palette())
        generation = self.generation
        self.source.read_later(missing, owner=self, done=lambda result, error: self.images_read(
            missing, result, error, generation))

    def images_read(self, names, result, error, generation=None):
        if generation is not None and generation != self.generation:
            return  # (read before a Reload: the next rendering reads them again)
        for name in names:
            found = (result or {}).get(name) if error is None else printable(error)
            if isinstance(found, QImage):
                self.images[name] = found
            else:
                self.images[name] = placeholder(tr("Image not shown: {name} ({why})", name=printable(name),
                                                   why=printable(found or tr("not found"))), self.palette())
        self.render(self.markdown)

    # -- zoom --

    def zoom_by(self, steps):
        """STEPS larger (negative: smaller); 0: as it was."""
        if steps == 0:
            self.browser.zoomOut(self.zoom) if self.zoom > 0 else self.browser.zoomIn(-self.zoom)
            self.zoom = 0
            return
        self.browser.zoomIn(steps) if steps > 0 else self.browser.zoomOut(-steps)
        self.zoom += steps
