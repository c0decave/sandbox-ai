"""A small PDF writer for sandbox-ai's reports: headings, paragraphs with links, lists and tables as text, page
breaks, a footer with page numbers, the document outline (bookmarks) and internal links (link annotations). It
uses only the base-14 fonts every PDF reader has (Helvetica, Helvetica-Bold, Courier), so nothing is embedded,
and text is written in their WinAnsi encoding (a character it does not hold is shown as "?").

A document is a list of blocks (report.py builds them; the Markdown and HTML writers read the same list):

  ("heading", LEVEL, TEXT, ANCHOR)       a heading (1-3) that links and the outline can point to
  ("para", SEGMENTS, STYLE)              a paragraph; STYLE None, "muted" or "mono"
  ("list", [SEGMENTS, ...])              a bulleted list
  ("table", HEADERS, ROWS)               a table; each cell is SEGMENTS
  ("toc", [(LEVEL, TEXT, ANCHOR), ...])  the table of contents, with the page of each entry

SEGMENTS is a list of ("t", TEXT), ("b", TEXT) (bold), ("c", TEXT) (fixed width) and ("l", TEXT, ANCHOR) (a link
to the heading ANCHOR). Standard library only.
"""
import time
import zlib

PAGE = (595.28, 841.89)  # A4 in points
MARGIN = 48
FOOTER = 28

# Glyph widths (1/1000 of the font size) of the printable ASCII characters, from the fonts' AFM files; any other
# character is measured as an average glyph.
HELVETICA = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
             556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
             500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469,
             556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
             278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
HELVETICA_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
                  556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778,
                  722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
                  278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
                  611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
FONTS = {"F1": ("Helvetica", HELVETICA), "F2": ("Helvetica-Bold", HELVETICA_BOLD), "F3": ("Courier", None)}
STYLE_FONT = {"t": "F1", "b": "F2", "c": "F3", "l": "F1"}
LINK_COLOUR = (0.05, 0.25, 0.65)
MUTED_COLOUR = (0.38, 0.38, 0.38)
HEADING_SIZES = {1: 17, 2: 13, 3: 10.5}
TEXT_SIZE = 9.5
TABLE_SIZE = 7.6
CELL_LINES = 30  # lines of one table cell at most (the rest: "…")


def width(text, font, size):
    """The width of TEXT in FONT (F1, F2, F3) at SIZE, in points."""
    widths = FONTS[font][1]
    if widths is None:
        return len(text) * 0.6 * size
    total = 0
    for character in text:
        code = ord(character)
        total += widths[code - 32] if 32 <= code < 127 else 556
    return total * size / 1000


def encode(text):
    """TEXT as a PDF string literal in WinAnsi: what the encoding lacks as "?", control characters as spaces."""
    data = "".join(c if c >= " " else " " for c in str(text)).encode("cp1252", "replace")
    return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def text_string(text):
    """TEXT as a PDF text string for the outline and the document information (UTF-16BE: any character)."""
    return b"<feff" + str(text).encode("utf-16-be").hex().encode() + b">"


def text_op(x, y, text, font, size, colour=None):
    """The content stream operators that draw TEXT at X, Y."""
    colour = colour or (0, 0, 0)
    return b"BT %s %s %s rg /%s %s Tf %s %s Td %s Tj ET" % (
        number(colour[0]).encode(), number(colour[1]).encode(), number(colour[2]).encode(), font.encode(),
        number(size).encode(), number(x).encode(), number(y).encode(), encode(text))


def number(value):
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


class Layout:
    """One pass over the blocks: lines placed on pages, where each anchor landed, the links to draw."""

    def __init__(self, page=PAGE, margin=MARGIN, toc_pages=None):
        self.page_width, self.page_height = page
        self.margin = margin
        self.left, self.right = margin, page[0] - margin
        self.top, self.bottom = page[1] - margin, margin + FOOTER
        self.pages, self.anchors, self.toc_pages = [], {}, toc_pages or {}
        self.y = None
        self.new_page()

    # -- pages and drawing --

    def new_page(self):
        self.pages.append({"ops": [], "links": []})
        self.y = self.top

    @property
    def ops(self):
        return self.pages[-1]["ops"]

    def room(self, height):
        """A new page unless HEIGHT points still fit on this one."""
        if self.y - height < self.bottom and self.y < self.top:
            self.new_page()

    def text(self, x, y, text, font, size, colour=None):
        if text:
            self.ops.append(text_op(x, y, text, font, size, colour))

    def rectangle(self, x, y, w, h, grey):
        self.ops.append(b"%s g %s %s %s %s re f 0 g" % (number(grey).encode(), number(x).encode(), number(y).encode(),
                                                       number(w).encode(), number(h).encode()))

    def line(self, x1, y1, x2, y2, grey=0.75, thickness=0.4):
        self.ops.append(b"%s G %s w %s %s m %s %s l S 0 G" % (
            number(grey).encode(), number(thickness).encode(), number(x1).encode(), number(y1).encode(),
            number(x2).encode(), number(y2).encode()))

    def link(self, x1, y1, x2, y2, anchor):
        self.pages[-1]["links"].append(((x1, y1, x2, y2), anchor))

    # -- text in lines --

    @staticmethod
    def words(segments, base="t"):
        """[(word, font, anchor or None, space before)] of SEGMENTS; a word longer than a line is split later."""
        found, spaced = [], False
        for segment in segments:
            kind, text = segment[0], str(segment[1])
            anchor = segment[2] if kind == "l" and len(segment) > 2 else None
            font = STYLE_FONT.get(kind if kind != "t" else base, "F1")
            for index, piece in enumerate(text.split(" ")):
                spaced = spaced or index > 0
                if piece:
                    found.append((piece, font, anchor, spaced and bool(found)))
                    spaced = False
        return found

    def wrap(self, segments, size, room, base="t"):
        """The lines of SEGMENTS within ROOM points: [[(x offset, text, font, anchor)]]."""
        lines, current, x = [], [], 0.0
        for word, font, anchor, spaced in self.words(segments, base):
            gap = width(" ", font, size) if spaced and current else 0.0
            needed = width(word, font, size)
            if current and x + gap + needed > room:
                lines.append(current)
                current, x, gap = [], 0.0, 0.0
            while needed > room:  # a word longer than the line (a URL, a path): cut where it no longer fits
                cut = max(1, int(len(word) * room / needed) - 1)
                while cut > 1 and width(word[:cut], font, size) > room - x:
                    cut -= 1
                current.append((x, word[:cut], font, anchor))
                lines.append(current)
                current, x, word = [], 0.0, word[cut:]
                needed = width(word, font, size)
            current.append((x + gap, word, font, anchor))
            x += gap + needed
        if current:
            lines.append(current)
        return lines or [[]]

    def draw_line(self, line, left, y, size, colour=None):
        """The pieces of LINE at baseline Y (joined where they share font and link)."""
        merged = []
        for x, text, font, anchor in line:
            if merged and merged[-1][2] == font and merged[-1][3] == anchor:
                previous = merged[-1]
                gap = x - (previous[0] + width(previous[1], font, size))
                spaces = max(1, round(gap / max(width(" ", font, size), 0.1))) if gap > 0.01 else 0
                merged[-1] = (previous[0], previous[1] + " " * spaces + text, font, anchor)
            else:
                merged.append((x, text, font, anchor))
        for x, text, font, anchor in merged:
            self.text(left + x, y, text, font, size, LINK_COLOUR if anchor else colour)
            if anchor:
                end = left + x + width(text, font, size)
                self.line(left + x, y - 1.2, end, y - 1.2, grey=0.55, thickness=0.3)
                self.link(left + x, y - 2.5, end, y + size, anchor)

    def paragraph(self, segments, size=TEXT_SIZE, style=None, indent=0.0, bullet=None):
        base = "c" if style == "mono" else "t"
        colour = MUTED_COLOUR if style == "muted" else None
        leading = size * 1.32
        left = self.left + indent
        for index, line in enumerate(self.wrap(segments, size, self.right - left, base)):
            self.room(leading)
            self.y -= leading
            if bullet and index == 0:
                self.text(left - 9, self.y, bullet, "F1", size, colour)
            self.draw_line(line, left, self.y, size, colour)
        self.y -= size * 0.45

    def heading(self, level, text, anchor):
        size = HEADING_SIZES.get(level, TEXT_SIZE)
        # A heading is never the last line of a page: room for it and a few lines after it (a finding's heading:
        # most of its facts).
        self.room(size * 1.6 + TEXT_SIZE * (12 if level >= 3 else 4))
        if level == 1 and self.y < self.top:
            self.y -= size * 0.8
        self.y -= size * 1.25
        if anchor:
            self.anchors[anchor] = (len(self.pages) - 1, min(self.top, self.y + size * 1.3))
        for index, line in enumerate(self.wrap([("b", text)], size, self.right - self.left)):
            if index:
                self.room(size * 1.25)
                self.y -= size * 1.25
            self.draw_line(line, self.left, self.y, size)
        if level == 1:
            self.line(self.left, self.y - 4, self.right, self.y - 4, grey=0.6, thickness=0.6)
            self.y -= 6
        self.y -= size * 0.35

    def toc(self, entries):
        size = TEXT_SIZE
        leading = size * 1.45
        for level, text, anchor in entries:
            indent = 14 * (level - 1)
            page = self.toc_pages.get(anchor)
            label = str(page + 1) if page is not None else ""
            room = self.right - self.left - indent - 40
            line = self.wrap([("l", text, anchor)], size, room)[0]
            self.room(leading)
            self.y -= leading
            self.draw_line(line, self.left + indent, self.y, size)
            if label:
                self.text(self.right - width(label, "F1", size), self.y, label, "F1", size, MUTED_COLOUR)
        self.y -= size * 0.6

    def table(self, headers, rows, size=TABLE_SIZE):
        columns = len(headers)
        if not columns:
            return
        room = self.right - self.left
        padding = 3.0
        # A table without headers is a list of facts: names on the left, the full width for what they say.
        facts = not any(str(h) for h in headers)
        natural = []
        for column in range(columns):
            cells = [[("b", str(headers[column]))]] + [row[column] if column < len(row) else [] for row in rows[:400]]
            longest = max((sum(width(str(s[1]), STYLE_FONT.get(s[0], "F1"), size) for s in cell) for cell in cells),
                          default=0)
            natural.append(min(max(longest + 2 * padding, 28.0), room * 0.6))
        if facts and columns == 2:
            widths = [min(natural[0], room * 0.3), room - min(natural[0], room * 0.3)]
        else:
            total = sum(natural)
            widths = natural if total <= room else [max(28.0, room * value / total) for value in natural]
            scale = room / sum(widths) if sum(widths) > room else 1.0
            widths = [value * scale for value in widths]
        leading = size * 1.28

        def cell_lines(cell, column):
            lines = self.wrap(cell, size, widths[column] - 2 * padding)
            if len(lines) > CELL_LINES:
                lines = lines[:CELL_LINES]
                end = lines[-1][-1][0] + width(lines[-1][-1][1], lines[-1][-1][2], size) if lines[-1] else 0.0
                lines[-1] = lines[-1] + [(end + 2, "…", "F1", None)]
            return lines

        def draw_row(cells, header=False):
            wrapped = [cell_lines(cells[column] if column < len(cells) else [], column) for column in range(columns)]
            height = max(len(lines) for lines in wrapped) * leading + padding * 1.6
            if self.y - height < self.bottom:
                self.new_page()
                if not header and not facts:
                    draw_row([[("b", str(h))] for h in headers], header=True)
            top = self.y
            if header:
                self.rectangle(self.left, top - height, sum(widths), height, 0.9)
            x = self.left
            for column, lines in enumerate(wrapped):
                y = top - padding * 0.8
                for line in lines:
                    y -= leading
                    self.draw_line(line, x + padding, y + size * 0.28, size)
                x += widths[column]
            self.y = top - height
            self.line(self.left, self.y, self.left + sum(widths), self.y)

        self.room(leading * 3)
        if facts:
            self.line(self.left, self.y, self.left + sum(widths), self.y)
        else:
            draw_row([[("b", str(h))] for h in headers], header=True)
        for row in rows:
            draw_row(row)
        self.y -= TEXT_SIZE * 0.8

    # -- the blocks --

    def run(self, blocks):
        for block in blocks:
            kind = block[0]
            if kind == "heading":
                self.heading(block[1], block[2], block[3])
            elif kind == "para":
                self.paragraph(block[1], style=block[2] if len(block) > 2 else None)
            elif kind == "list":
                for item in block[1]:
                    self.paragraph(item, indent=12, bullet="-")
            elif kind == "table":
                self.table(block[1], block[2])
            elif kind == "toc":
                self.toc(block[1])
            elif kind == "pagebreak":
                if self.y < self.top:
                    self.new_page()
        return self


def outline_tree(entries):
    """[(level, title, anchor)] as a tree: [{"title", "anchor", "children"}]."""
    root = {"children": [], "level": 0}
    stack = [root]
    for level, title, anchor in entries:
        node = {"title": title, "anchor": anchor, "children": [], "level": level}
        while stack[-1]["level"] >= level:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)
    return root["children"]


def render(blocks, title="", outline=(), author="sandbox-ai", page=PAGE, created=None):
    """The PDF (bytes) of BLOCKS, with OUTLINE [(level, title, anchor)] as its bookmarks; links to an anchor no
    heading has are left out."""
    first = Layout(page).run(blocks)
    layout = Layout(page, toc_pages={anchor: place[0] for anchor, place in first.anchors.items()}).run(blocks)
    anchors = layout.anchors
    objects = {}  # number: bytes
    catalog, pages_number, info = 1, 2, 3
    fonts = {name: 4 + index for index, name in enumerate(FONTS)}
    next_number = 4 + len(FONTS)
    page_numbers, content_numbers = [], []
    for _ in layout.pages:
        page_numbers.append(next_number)
        content_numbers.append(next_number + 1)
        next_number += 2

    def destination(anchor):
        index, top = anchors[anchor]
        return b"[%d 0 R /XYZ %s %s null]" % (page_numbers[index], number(layout.left - 6).encode(),
                                               number(top).encode())

    for name, number_ in fonts.items():
        objects[number_] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
                            % FONTS[name][0].encode())
    total = len(layout.pages)
    for index, (content, place) in enumerate(zip(layout.pages, page_numbers)):
        shown = title if len(title) <= 90 else title[:89] + "…"
        footer = f"{shown}  ·  page {index + 1} of {total}" if title else f"page {index + 1} of {total}"
        footer_op = text_op(layout.left, layout.margin - 4, footer, "F1", 7.5, MUTED_COLOUR)
        stream = zlib.compress(b"\n".join(content["ops"] + [footer_op]))
        objects[content_numbers[index]] = b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (
            len(stream), stream)
        annotations = []
        for (x1, y1, x2, y2), anchor in content["links"]:
            if anchor not in anchors:
                continue
            annotations.append(next_number)
            objects[next_number] = (b"<< /Type /Annot /Subtype /Link /Rect [%s %s %s %s] /Border [0 0 0] /Dest %s >>"
                                    % (number(x1).encode(), number(y1).encode(), number(x2).encode(),
                                       number(y2).encode(), destination(anchor)))
            next_number += 1
        annots = b" /Annots [%s]" % b" ".join(b"%d 0 R" % a for a in annotations) if annotations else b""
        resources = b" ".join(b"/%s %d 0 R" % (name.encode(), number_) for name, number_ in fonts.items())
        objects[place] = (b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources << /Font << %s >> >> "
                          b"/Contents %d 0 R%s >>" % (pages_number, number(page[0]).encode(), number(page[1]).encode(),
                                                      resources, content_numbers[index], annots))
    objects[pages_number] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % n for n in page_numbers), total)
    # The outline: one object per entry, linked to its parent and siblings.
    tree = outline_tree([entry for entry in outline if entry[2] in anchors])
    outline_root = None
    if tree:
        outline_root = next_number
        next_number += 1

        def number_nodes(nodes):
            nonlocal next_number
            for node in nodes:
                node["number"] = next_number
                next_number += 1
                number_nodes(node["children"])

        def count(node):
            return len(node["children"]) + sum(count(child) for child in node["children"] if child["level"] < 2)

        def write_nodes(nodes, parent):
            for index, node in enumerate(nodes):
                parts = [b"/Title %s" % text_string(node["title"]), b"/Parent %d 0 R" % parent,
                         b"/Dest %s" % destination(node["anchor"])]
                if index:
                    parts.append(b"/Prev %d 0 R" % nodes[index - 1]["number"])
                if index + 1 < len(nodes):
                    parts.append(b"/Next %d 0 R" % nodes[index + 1]["number"])
                if node["children"]:
                    # Open (a positive count) down to the second level; the findings below stay folded.
                    shown = count(node)
                    parts += [b"/First %d 0 R" % node["children"][0]["number"],
                              b"/Last %d 0 R" % node["children"][-1]["number"],
                              b"/Count %d" % (shown if node["level"] < 2 else -len(node["children"]))]
                objects[node["number"]] = b"<< " + b" ".join(parts) + b" >>"
                write_nodes(node["children"], node["number"])
        number_nodes(tree)
        write_nodes(tree, outline_root)
        visible = len(tree) + sum(count(node) for node in tree if node["level"] < 2)
        objects[outline_root] = b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (
            tree[0]["number"], tree[-1]["number"], visible)
    objects[catalog] = b"<< /Type /Catalog /Pages %d 0 R%s >>" % (
        pages_number, b" /Outlines %d 0 R /PageMode /UseOutlines" % outline_root if outline_root else b"")
    stamp = time.strftime("D:%Y%m%d%H%M%SZ", time.gmtime(created if created is not None else time.time()))
    objects[info] = b"<< /Title %s /Producer %s /Creator %s /CreationDate (%s) >>" % (
        text_string(title), text_string("sandbox-ai report (pdfdoc.py)"), text_string(author), stamp.encode())
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number_ in sorted(objects):
        offsets[number_] = len(out)
        out += b"%d 0 obj\n" % number_ + objects[number_] + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number_ in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number_] if number_ in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, catalog, info, xref)
    return bytes(out)
