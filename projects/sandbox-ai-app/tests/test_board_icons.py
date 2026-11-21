"""The Board's icon of each kind of part (user 2026-11-17: every element of the board gets a distinct SVG icon):
one own SVG per board.KINDS key in sandbox_ai_app/icons/board, valid for QtSvg, pairwise distinct, drawn in the
theme's colour, rendered once per kind, size, colour and ratio (a paint takes the cache), shown in a node's title
band, the parts list, the drag pixmap, the drop ghost, the menus that name a kind and the inspector; an unknown kind
gets the neutral fallback, never an error."""
import gc
import itertools
import os
from pathlib import Path
import re

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QByteArray, QEvent, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from sandbox_ai_app import backend, board as B, board_icons, help_view, theme, widgets  # noqa: E402

FOLDER = Path(board_icons.__file__).resolve().parent / "icons" / "board"


def alpha_mask(pixmap):
    """The alpha of every pixel of PIXMAP, as bytes (the shape, whatever the colour)."""
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    return bytes(image.pixel(x, y) >> 24 & 0xFF for y in range(image.height()) for x in range(image.width()))


def same(colors, color):
    """Whether every colour of COLORS is COLOR (give or take the renderer's rounding of one step)."""
    want = QColor(color)
    return bool(colors) and all(max(abs(QColor(c).red() - want.red()), abs(QColor(c).green() - want.green()),
                                    abs(QColor(c).blue() - want.blue())) <= 2 for c in colors)


def inked(image, rect=None):
    """The colours (#rrggbb) of the opaque pixels of IMAGE (in RECT)."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    left, top, width, height = rect or (0, 0, image.width(), image.height())
    pixels = (image.pixel(x, y) for y in range(top, top + height) for x in range(left, left + width))
    return [QColor.fromRgba(pixel).name() for pixel in pixels if pixel >> 24 & 0xFF == 0xFF]


@pytest.fixture(autouse=True)
def fresh_cache():
    board_icons._sources.clear()
    board_icons._pixmaps.clear()
    board_icons._icons.clear()
    yield
    board_icons._sources.clear()
    board_icons._pixmaps.clear()
    board_icons._icons.clear()


# -- the files --


def test_every_kind_has_its_own_svg_file_and_nothing_else_is_there():
    files = {path.stem for path in FOLDER.glob("*.svg")}
    assert set(B.KINDS) <= files, sorted(set(B.KINDS) - files)
    assert files == set(B.KINDS) | {board_icons.FALLBACK}, sorted(files - set(B.KINDS))
    assert sorted(path.name for path in FOLDER.iterdir()) == sorted(f"{name}.svg" for name in files)


@pytest.mark.parametrize("name", [*B.KINDS, board_icons.FALLBACK])
def test_every_icon_is_a_valid_line_drawing_on_24x24_in_current_color(qapp, name):
    text = (FOLDER / f"{name}.svg").read_text(encoding="utf-8")
    renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
    assert renderer.isValid(), name
    assert renderer.viewBoxF() == QRectF(0, 0, 24, 24)
    assert 'stroke="currentColor"' in text and "own drawing" in text
    # One colour, the theme's: no colour of its own, nothing loaded or run, no text (a font's glyphs differ per system).
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|<image|<script|<text|href|<style|<foreignObject", text), name
    assert len(text.encode()) < 4096
    shown = board_icons.pixmap(name, 24, "#000000")
    assert any(alpha_mask(shown)), f"{name} draws nothing"


@pytest.mark.parametrize("size", [16, 24])
def test_the_icons_are_pairwise_distinct_in_shape(qapp, size):
    """Rendered in one colour, no two icons are alike: they differ in shape, not only in colour - at least a quarter
    of the pixels either one inks (16 px: as small as they are shown)."""
    masks = {name: alpha_mask(board_icons.pixmap(name, size, "#000000")) for name in [*B.KINDS, board_icons.FALLBACK]}
    for (a, mask_a), (b, mask_b) in itertools.combinations(masks.items(), 2):
        assert mask_a != mask_b, f"{a} and {b} look the same"
        differ = sum(abs(x - y) > 128 for x, y in zip(mask_a, mask_b))
        ink = sum(max(x, y) > 128 for x, y in zip(mask_a, mask_b))
        assert differ >= ink / 4, f"{a} and {b} differ in {differ} of {ink} pixels only at {size} px"


# -- colours, cache, fallback --


@pytest.mark.parametrize("name", list(theme.THEMES_DATA))
def test_the_icon_takes_the_theme_s_colours_in_every_theme(qapp, monkeypatch, name):
    """In each theme (light, dark, high contrast, the retro ones): the list's icon in its text colour, selected in the
    selection's text colour, greyed in the disabled colour; the default colour is the theme's text; the title
    band's colour reads on the band (3:1 for graphics)."""
    monkeypatch.setattr(theme, "_current", name)
    colors = theme.colors()
    icon = board_icons.icon("network")
    for mode, color in ((icon.Mode.Normal, colors["text"]), (icon.Mode.Selected, colors["highlight_text"]),
                        (icon.Mode.Disabled, colors["disabled"])):
        shown = set(inked(icon.pixmap(16, 16, mode).toImage()))
        assert same(shown, color), (mode, color, shown)
    assert same(set(inked(board_icons.pixmap("git", 24).toImage())), colors["text"])
    title = colors.get("node_title", colors["text"])
    assert theme.contrast(title, colors["node_header"]) >= 3.0


def test_a_pixmap_is_rendered_once_per_kind_size_colour_and_ratio(qapp):
    before = board_icons.renders
    first = board_icons.pixmap("vpn", 16, "#123456", 1.0)
    assert board_icons.renders == before + 1
    for _ in range(50):
        assert board_icons.pixmap("vpn", 16, "#123456", 1.0).cacheKey() == first.cacheKey()
    assert board_icons.renders == before + 1, "the cache answers again"
    board_icons.pixmap("vpn", 16, "#123456", 2.0)
    board_icons.pixmap("vpn", 24, "#123456", 1.0)
    board_icons.pixmap("vpn", 16, "#654321", 1.0)
    board_icons.pixmap("git", 16, "#123456", 1.0)
    assert board_icons.renders == before + 5, "a new ratio, size, colour or kind is a new picture"
    sharp = board_icons.pixmap("vpn", 16, "#123456", 2.0)
    assert (sharp.width(), sharp.devicePixelRatio()) == (32, 2.0)
    board_icons.pixmap("vpn", 16, "#123456", 2.01)  # zoomed a hair further: the same quarter step
    assert board_icons.renders == before + 5


def test_an_unknown_kind_gets_the_neutral_fallback_never_an_error(qapp, tmp_path, monkeypatch):
    fallback = alpha_mask(board_icons.pixmap(board_icons.FALLBACK, 24, "#000000"))
    for kind in ("a-kind-of-a-newer-board", "quantum", "../board/agent", "", None, "agent/../vpn", "A" * 500):
        assert alpha_mask(board_icons.pixmap(kind, 24, "#000000")) == fallback, kind
        assert not board_icons.icon(kind).isNull()
    image = QImage(40, 40, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    board_icons.paint(painter, "no-such-kind", QPointF(4, 4), 24, "#ff0000")
    painter.end()
    assert same(set(inked(image)), "#ff0000")
    # A broken or missing drawing (a damaged installation): the fallback, else nothing - never an exception.
    broken = tmp_path / "icons"
    broken.mkdir()
    (broken / "agent.svg").write_text("<svg this is not")
    (broken / "vpn.svg").write_bytes(b"\xff\xfe" * 10)
    (broken / "git.svg").write_text("<svg>" + " " * (board_icons.SOURCE_LIMIT + 10) + "</svg>")
    (broken / "fallback.svg").write_text((FOLDER / "fallback.svg").read_text())
    monkeypatch.setattr(board_icons, "FOLDER", broken)
    board_icons._sources.clear()
    for kind in ("agent", "vpn", "git", "network"):
        assert alpha_mask(board_icons.pixmap(kind, 24, "#000000")) == fallback, kind
    (broken / "fallback.svg").unlink()
    board_icons._sources.clear()
    board_icons._pixmaps.clear()
    assert not any(alpha_mask(board_icons.pixmap("agent", 24, "#000000"))), "nothing drawn, nothing raised"


# -- the Board --


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    if not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"):
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(help_view, "OPENED", [])
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20, "guide_nodes": False})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    yield view
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def render_node(item):
    rect = item.rect()
    image = QImage(int(rect.width()) + 20, int(rect.height()) + 20, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.translate(10, 10)
    item.paint(painter, QStyleOptionViewItem(), None)
    painter.end()
    return image


def icon_box(item):
    """Where a node's icon is in render_node's image: left in its title band, beside the title's first line (the
    title's text starts after it)."""
    from sandbox_ai_app.board_view import NODE_PAD
    return int(10 + NODE_PAD), 10 + 5, board_icons.SIZE, board_icons.SIZE + 4


@pytest.mark.parametrize("name", ["light", "dark", "win311", "hacker"])
def test_a_node_paints_its_kind_s_icon_in_the_title_colour_from_the_cache(page, qapp, monkeypatch, name):
    monkeypatch.setattr(theme, "_current", name)
    page.add_node("limits", **{"--memory": "4g"})
    page.add_node("vpn")
    colors = theme.colors()
    title = QColor(colors.get("node_title", colors["text"])).name()
    images = {}
    for kind in ("agent", "limits", "vpn"):
        item = page.items[page.board.first(kind).id]
        images[kind] = render_node(item)
        shown = inked(images[kind], icon_box(item))  # (on the band: its colour, the icon's, blends of both)
        assert shown.count(title) >= 5, (kind, set(shown))
    assert len({bytes(image.constBits()) for image in images.values()}) == 3
    page.view.viewport().grab()  # the canvas at its zoom: one picture per kind at that scale
    before = board_icons.renders
    for _ in range(5):
        for kind in ("agent", "limits", "vpn"):
            render_node(page.items[page.board.first(kind).id])
        page.view.viewport().grab()
    assert board_icons.renders == before, "painted again from the cache, never rendered per paint"


def test_the_title_keeps_its_room_after_the_icon(page, qapp, tmp_path):
    """The title starts after the icon and still wraps (two lines at most) and elides inside the node."""
    from PySide6.QtGui import QFontMetricsF
    from sandbox_ai_app.board_view import ICON_ROOM, NODE_PAD, NODE_W
    long_path = tmp_path / ("folder-with-a-long-name-" * 4) / "project"
    long_path.mkdir(parents=True)
    page.add_node("workspace", path=str(long_path))
    item = page.items[page.board.first("workspace").id]
    metrics = QFontMetricsF(item.title_font)
    assert len(item.titles) == 2 and item.titles[-1].endswith("/project")
    room = NODE_W - 2 * NODE_PAD - ICON_ROOM
    assert all(metrics.horizontalAdvance(line) <= room for line in item.titles)
    image = render_node(item)
    left = int(10 + NODE_PAD + board_icons.SIZE)  # between the icon and the title's text: nothing painted
    title = QColor(theme.colors().get("node_title", theme.colors()["text"])).name()
    assert title not in inked(image, (left + 1, 15, int(board_icons.GAP) - 2, 30))


def test_the_parts_list_shows_each_kind_s_icon_and_keeps_its_names(page, qapp):
    palette = page.palette
    delegate = palette.itemDelegate()
    assert palette.iconSize().width() == board_icons.SIZE
    kinds = []
    for row in range(palette.count()):
        item = palette.item(row)
        kind = item.data(Qt.ItemDataRole.UserRole)
        kinds.append(kind)
        option = QStyleOptionViewItem()
        delegate.initStyleOption(option, palette.indexFromItem(item))
        assert not option.icon.isNull() and option.features & QStyleOptionViewItem.ViewItemFeature.HasDecoration
        assert alpha_mask(option.icon.pixmap(16, 16)) == alpha_mask(board_icons.pixmap(kind, 16))
        assert item.text() == B.tr(B.KINDS[kind].title), "the title stays the row's text and accessible name"
        assert item.toolTip() and B.tr(B.KINDS[kind].help) in item.toolTip()
    assert set(kinds) == {k for k, kind in B.KINDS.items() if not kind.required}
    palette.viewport().grab()
    before = board_icons.renders
    for _ in range(3):
        palette.viewport().grab()
    assert board_icons.renders == before, "the list paints from the cache"
    # The icon is drawn in the row: the row's left end is no longer blank.
    image = palette.viewport().grab().toImage()
    rect = palette.visualItemRect(palette.item(0))
    text = QColor(theme.colors()["text"]).name()
    assert text in inked(image, (rect.left(), rect.top(), 28, rect.height()))


def test_the_drag_pixmap_and_the_drop_ghost_show_the_icon(page, qapp):
    from sandbox_ai_app.board_view import KindIcon, part_pixmap
    colors = theme.colors()
    title = QColor(colors.get("node_title", colors["text"])).name()
    for kind in ("llm", "mcp"):
        dragged = part_pixmap(kind).toImage()
        assert title in inked(dragged, (6, 2, board_icons.SIZE, board_icons.SIZE))
    assert part_pixmap("llm").toImage() != part_pixmap("mcp").toImage()
    page.show_ghost(("part", "llm"), QPointF(300, 300))
    icons = [child for child in page.ghost.childItems() if isinstance(child, KindIcon)]
    assert [icon.kind for icon in icons] == ["llm"]
    assert icons[0].x() + board_icons.SIZE < page.ghost.label.x(), "the label after the icon"
    page.hide_ghost()
    page.show_ghost(("paths", ["/tmp/x"]), QPointF(300, 300))
    assert not [child for child in page.ghost.childItems() if isinstance(child, KindIcon)], "a folder: no kind"
    page.hide_ghost()


def test_the_menus_that_name_a_kind_show_its_icon(page, qapp):
    menu = page.canvas_menu(QPointF(-5000, -5000))  # the free canvas: Add a part here
    parts = next(action.menu() for action in menu.actions() if action.menu() is not None)
    named = {action.text(): action for action in parts.actions() if action.text()}
    for row in range(page.palette.count()):
        entry = page.palette.item(row)
        action = named[entry.text()]
        assert not action.icon().isNull()
        assert alpha_mask(action.icon().pixmap(16, 16)) == alpha_mask(
            board_icons.pixmap(entry.data(Qt.ItemDataRole.UserRole), 16))
    agent = page.items[page.board.first("agent").id]
    menu = page.canvas_menu(agent.sceneBoundingRect().center())
    properties = next(action for action in menu.actions() if action.text() == B.tr("Properties"))
    assert alpha_mask(properties.icon().pixmap(16, 16)) == alpha_mask(board_icons.pixmap("agent", 16))


def test_the_inspector_shows_the_selected_kind_s_icon(page, qapp):
    page.add_node("git")
    page.inspect(page.board.first("git"))
    assert page.inspector_icon.isVisibleTo(page) and page.inspector_title.text() == page.board.first("git").title()
    assert alpha_mask(page.inspector_icon.pixmap()) == alpha_mask(board_icons.pixmap(
        "git", board_icons.SIZE, theme.colors()["title"], page.inspector_icon.devicePixelRatioF()))
    page.inspect(None)
    assert not page.inspector_icon.isVisibleTo(page)


def test_the_guide_s_introduction_and_the_preset_thumbnails_show_the_icon(qapp):
    """The guide's first step names the kind with its icon before the name; a preset's thumbnail draws each part's
    icon in its title band (the gallery: a board at a glance)."""
    from PySide6.QtWidgets import QLabel
    from PySide6.QtCore import QSize
    from sandbox_ai_app import guide_view, preset_gallery
    board = B.Board()
    for kind in ("agent", "proxy", "network"):
        board.add(kind)
    node = board.add("vpn")
    dialog = guide_view.NodeGuide(node, [])
    try:
        marks = [label for label in dialog.findChildren(QLabel) if label.pixmap() is not None
                 and not label.pixmap().isNull()]
        assert len(marks) == 1 and alpha_mask(marks[0].pixmap()) == alpha_mask(board_icons.pixmap(
            "vpn", board_icons.SIZE, theme.colors()["title"], dialog.devicePixelRatioF()))
        assert B.tr(B.KINDS["vpn"].title) in [label.text() for label in dialog.findChildren(QLabel)]
    finally:
        dialog.deleteLater()
    before = board_icons.renders
    preset_gallery._thumbs.clear()
    preset_gallery.thumbnail(board, QSize(240, 150))
    assert board_icons.renders > before, "the parts' icons are drawn into the thumbnail"
