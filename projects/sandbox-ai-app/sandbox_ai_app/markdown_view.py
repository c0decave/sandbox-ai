"""The Files page's Markdown viewer: a file of a pane rendered as Markdown (CommonMark and GitHub's tables, task lists,
strikethrough and autolinks; footnotes made into links), headings in an outline beside it to jump to, fenced code
highlighted, images beside the file shown, zoom, and the editor's find bar (file_editor.py). The editor's text view
shows the same rendering as a live preview beside the text.

What is shown may have been written by an agent, so it is untrusted:

- Qt renders it (QTextDocument.setMarkdown) without HTML: tags are shown as the text they are, nothing runs, no
  script, no style sheet, no frame;
- nothing is loaded from the network, ever: a remote image is a placeholder that names its URL; the browser's own
  loading is replaced (MarkdownBrowser.loadResource answers from what this module read, nothing else);
- an image beside the file is read through the pane's files.Root like every other file (relative to open folders,
  never through a symbolic link, never above the root - and for a file of a sandbox's folder shown in the host pane,
  never outside that folder: ImageSource's top), in a thread, at most IMAGE_LIMIT bytes and IMAGE_PIXELS pixels, at
  most IMAGES of them; Reload reads them again;
- a link is never followed inside the viewer: "#heading" scrolls there, a relative link to another Markdown file of
  the same root opens that file in a viewer of its own, an http(s) link opens in the browser only after the human
  said so (the window asks, naming the URL); anything else is named, not opened.
"""
from __future__ import annotations

import base64
import posixpath
import re

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QImage, QImageReader, QPainter, QPalette, QSyntaxHighlighter, QTextCharFormat, QTextCursor,
    QTextDocument, QTextFormat,
)
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QSplitter, QTextBrowser, QVBoxLayout, QWidget

from . import theme
from .files import printable
from .i18n import tr
from .widgets import background, plain_label

SUFFIXES = (".md", ".markdown", ".mdown", ".mkd")
FEATURES = QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
IMAGE_LIMIT = 8 << 20  # bytes of one image
IMAGE_PIXELS = 40_000_000  # pixels of one image (a small file may say it is huge)
IMAGES = 200  # images of one document that are read
RENDER_DELAY = 300  # ms: the live preview follows the typing after this pause
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
    colours."""

    def highlightBlock(self, text):  # noqa: N802
        block = self.currentBlock()
        if not block.blockFormat().hasProperty(QTextFormat.Property.BlockCodeFence):
            return
        formats = {}
        for kind, name in (("comment", "muted"), ("string", "ok"), ("number", "warn"), ("word", "accent")):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(theme.color(name) if name != "accent" else theme.colors().get(
                "accent", theme.color("warn"))))
            if kind == "word":
                fmt.setFontWeight(QFont.Weight.Bold)
            formats[kind] = fmt
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


def decode(data):
    """A QImage of DATA (bytes), or (None, why): never more than IMAGE_PIXELS pixels are decoded."""
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if size.isValid() and size.width() * size.height() > IMAGE_PIXELS:
        return None, tr("too big")
    image = reader.read()
    if image.isNull():
        return None, tr("not an image")
    return image, None


class ImageSource:
    """The images of the file RELATIVE of ROOT (a files.Root): where a name points (local(), below the root only - and
    below TOP, a folder relative to the root, when one is given) and reading them (read(), in a thread)."""

    def __init__(self, root, relative, top=""):
        self.root, self.folder = root, posixpath.dirname(relative)
        # (the host pane's root is "/": a Markdown file the agent wrote in its folder, viewed there, showed any image of
        # the host's its "../../.." named - only those of the sandbox's folder now)
        self.top = "/".join(part for part in top.split("/") if part)

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
        return background(self.read, list(names), owner=owner, timeout=60, done=done)

    def read(self, names):
        """{name: QImage or why not} for the local and data: images NAMES (Root.read_file; runs in read_later's
        thread)."""
        found = {}
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
                image, why = decode(data)
                found[name] = image if image is not None else why
            except (OSError, ValueError) as exc:
                found[name] = printable(exc)
        return found


class MarkdownBrowser(QTextBrowser):
    """A QTextBrowser that loads nothing itself: images come from VIEW's cache (read through the pane's Root), every
    other resource is refused; links are never followed here (the view decides)."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setReadOnly(True)
        self.setAccessibleName(tr("The file rendered as Markdown"))

    def loadResource(self, kind, url):  # noqa: N802
        if kind != QTextDocument.ResourceType.ImageResource:
            return None  # (no style sheet, no frame, nothing else)
        return self.view.image(url.toString())


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
        self.split.addWidget(self.outline)
        self.split.addWidget(self.browser)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([180, 600])
        self.outline.setVisible(outline)
        layout.addWidget(self.split, 1)
        self.status = plain_label(role="secondary", word_wrap=False)
        layout.addWidget(self.status)
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
        shown, notes = with_footnotes(text)
        self.browser.document().setMarkdown(shown, FEATURES)
        self.read_outline(notes)
        bar.setValue(where)
        self.read_missing()

    def read_outline(self, notes=()):
        self.outline.clear()
        self.anchors = {}
        block = self.browser.document().begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            text = block.text()
            if level:
                key = slug(text)
                number, unique = 1, key
                while unique in self.anchors:  # (GitHub numbers headings of the same name)
                    unique, number = f"{key}-{number}", number + 1
                self.anchors[unique] = block.position()
                item = QListWidgetItem("    " * (level - 1) + text)
                item.setData(Qt.ItemDataRole.UserRole, block.position())
                self.outline.addItem(item)
            for name in notes:
                if text.startswith(f"[{name}] "):
                    self.anchors.setdefault(f"fn-{name}", block.position())
            block = block.next()

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

    def clicked(self, url):
        """A link clicked: "#anchor" of this text scrolls there; anything else goes to the window (LINK)."""
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
