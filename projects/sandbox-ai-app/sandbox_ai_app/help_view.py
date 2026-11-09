"""The (?) buttons, their explanation and the Help area on screen (help_texts.py and help_content.py have the texts).

A HelpButton is a small round "?" painted in the theme's colours; a click (or Space) opens a HelpPopover with what
the thing is, what it is for, what happens when you use it, the command-line option behind it and "More in Help",
which opens the Help page at the matching section (in a dialog of its own when the (?) sits in a modal dialog).
install() puts the (?)s of help_texts.PLACES on the window's pages. The HelpPage is the Help area: a searchable
list of every section, the text in English or German, and the tours to start from there.
"""
from __future__ import annotations

import html
import re
import weakref

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QBoxLayout, QComboBox, QDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSplitter, QStyle,
    QStyleOptionGroupBox, QTabWidget, QTableView, QTextBrowser, QToolButton, QToolTip, QTreeView, QVBoxLayout,
    QWidget, QWidgetItem,
)

from . import help_texts as H
from . import i18n, theme, tours
from .i18n import N_, tr
from .widgets import MONO, FlowLayout, plain_label, safe_tooltip

OPENER = None   # the window's show_help(section id): set by the main window
OPENED = []     # the ids of the explanations opened in this run (a tour step waits for one)
SIZE = 18
_SHOWN = [True]  # the setting help_buttons: the (?)s are shown (the Help page, tours and F1 stay either way)
_BUTTONS = weakref.WeakSet()  # every HelpButton made, to hide or show them when the setting changes
_LISTENERS = []  # weakref.WeakMethod of callbacks (what paints a (?) itself: the board's nodes and parts list)


def buttons_shown():
    return _SHOWN[0]


def on_shown_changed(method):
    """Call the bound METHOD(shown) whenever the setting changes (kept weakly: a page that is gone is dropped)."""
    _LISTENERS.append(weakref.WeakMethod(method))


def set_shown(on):
    """Show or hide every (?) at once, live (setting help_buttons)."""
    on = bool(on)
    if on == _SHOWN[0]:
        return
    _SHOWN[0] = on
    for button in list(_BUTTONS):
        try:
            button.apply_shown()
        except RuntimeError:  # its C++ side is gone
            _BUTTONS.discard(button)
    for ref in list(_LISTENERS):
        method = ref()
        try:
            if method is None:
                raise RuntimeError
            method(on)
        except RuntimeError:
            _LISTENERS.remove(ref)


class HelpButton(QToolButton):
    """A round "?" that explains TIP_ID."""

    def __init__(self, tip_id, parent=None):
        super().__init__(parent)
        self.setObjectName("helpButton")
        self.setText("?")
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(SIZE, SIZE)
        self.popover = None
        self.wanted = None  # what setVisible asked for (None: as Qt decides, with its parent)
        self.clicked.connect(lambda _=False: self.explain())
        self.set_tip(tip_id)
        _BUTTONS.add(self)
        if not _SHOWN[0]:
            QToolButton.setVisible(self, False)

    def setVisible(self, visible):  # noqa: N802 - Qt's name; the setting help_buttons wins
        self.wanted = bool(visible)
        QToolButton.setVisible(self, self.wanted and _SHOWN[0])

    def apply_shown(self):
        if not _SHOWN[0]:
            QToolButton.setVisible(self, False)
        elif self.wanted is not False:
            if self.parentWidget() is None:  # not placed yet: shown with its parent later, never as a window
                self.setAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide, False)
            else:
                QToolButton.setVisible(self, True)
        corner = getattr(self, "corner", None)
        if corner is not None:
            corner.make_room()

    def set_tip(self, tip_id):
        self.tip_id = tip_id
        title = H.say(H.tip(tip_id).title)
        self.setToolTip(tr("What is this? {title}", title=title))  # the help's own words
        self.setAccessibleName(tr("What is this? {title}", title=title))

    def sizeHint(self):  # noqa: N802 - the stylesheet's padding must not make it larger
        return QSize(SIZE, SIZE)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(SIZE, SIZE)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        paint_mark(painter, QRectF(0, 0, SIZE, SIZE), self.underMouse() or self.hasFocus() or self.isDown(),
                   self.font())

    def explain(self):
        self.popover = show_tip(self.tip_id, self, self.mapToGlobal(QPoint(0, self.height() + 2)))
        return self.popover


def paint_mark(painter, rect, lit=False, font=None):
    """A (?) in RECT: a ring in the accent colour with a "?", filled when LIT (hovered, focused, pressed)."""
    colors = theme.colors()
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    accent = QColor(colors["accent"])
    painter.setPen(QPen(accent, 1.4))
    painter.setBrush(accent if lit else QColor(colors["base"]))
    painter.drawEllipse(rect.adjusted(1.0, 1.0, -1.0, -1.0))
    font = QFont(font) if font is not None else QFont()
    font.setBold(True)
    font.setPixelSize(max(6, int(rect.height()) - 6))
    painter.setFont(font)
    painter.setPen(QColor(colors["accent_text"] if lit else colors["accent"]))
    painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), "?")
    painter.restore()


class HelpPopover(QFrame):
    """The explanation of one (?): it closes on a click outside it or Esc."""

    def __init__(self, tip_id, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("helpPopover")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.tip = H.tip(tip_id)
        self.setFixedWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        self.title = plain_label(H.say(self.tip.title), role="section")
        self.what = plain_label(H.say(self.tip.what))
        layout.addWidget(self.title)
        layout.addWidget(self.what)
        self.does = plain_label(H.say(self.tip.does) if self.tip.does else "")
        self.does.setVisible(self.tip.does is not None)
        layout.addWidget(self.does)
        self.cli = plain_label(tr("Command line: {cli}", cli=self.tip.cli) if self.tip.cli else "", role="secondary")
        self.cli.setFont(MONO)
        self.cli.setVisible(bool(self.tip.cli))
        layout.addWidget(self.cli)
        row = QHBoxLayout()
        row.addStretch(1)
        self.more = QPushButton(tr("More in Help"))
        self.more.setProperty("role", "primary")
        self.more.clicked.connect(lambda _=False: self.more_in_help())
        self.more.setVisible(bool(self.tip.section))
        row.addWidget(self.more)
        layout.addLayout(row)

    def more_in_help(self):
        section, owner = self.tip.section, self.parentWidget()
        self.close()
        open_section(section, owner)

    def show_at(self, point):
        """Below POINT (global), kept on its screen."""
        self.resize(self.width(), self.layout().totalHeightForWidth(self.width()))  # wrapped labels need it
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        x, y = point.x(), point.y()
        if area is not None:
            x = max(area.left() + 4, min(x, area.right() - self.width() - 4))
            if y + self.height() > area.bottom() - 4:
                y = max(area.top() + 4, point.y() - self.height() - SIZE - 6)
        self.move(x, y)
        self.show()
        self.more.setFocus()


def show_tip(tip_id, owner, point):
    """Open the explanation TIP_ID for the widget OWNER at the global POINT."""
    popover = HelpPopover(tip_id, owner)
    popover.show_at(point)
    OPENED.append(tip_id)
    return popover


def open_section(section, owner=None):
    """The Help area at SECTION: the window's Help page, or a dialog when OWNER sits in a modal dialog (the window
    cannot be used then) or there is no window."""
    window = owner.window() if owner is not None else None
    modal = isinstance(window, QDialog) and window.isModal()
    if OPENER is not None and not modal:
        try:
            OPENER(section)
            return None
        except RuntimeError:  # the window that set it is gone
            pass
    dialog = HelpDialog(section, window)
    dialog.show()
    return dialog


# -- where the (?)s go --

class Corner(QObject):
    """Keeps BUTTON in the top right corner of TARGET (left of a vertical scroll bar; in the title row of a group
    box)."""

    def __init__(self, target, button):
        super().__init__(target)
        self.target, self.button = target, button
        self.header = header_of(target)
        if self.header is not None:
            # The (?) sits over the last column's title: that title keeps room for it ("Runnin(?)g" before), only
            # while the (?)s are shown (the setting help_buttons).
            self.header.setStyleSheet(self.header.styleSheet() + " QHeaderView[helpRoom=\"true\"]::section:last "
                                      f"{{ padding-right: {HEADER_ROOM}px; }}")
        target.installEventFilter(self)
        if isinstance(target, QAbstractScrollArea):
            target.verticalScrollBar().rangeChanged.connect(lambda *_: self.place())
        self.make_room()
        self.place()

    def make_room(self):
        """While the (?)s are shown, the header draws no title under its corner: its viewport ends HEADER_ROOM before
        the table's (a title scrolled there is cut at that edge, never covered: review 6 L2, "Answ(?)" on a table
        that scrolls sideways), and the last title keeps that room too (scrolled to the end)."""
        header = self.header
        if header is None or bool(header.property("helpRoom")) == _SHOWN[0]:
            return
        header.setProperty("helpRoom", _SHOWN[0])
        header.setViewportMargins(0, 0, HEADER_ROOM if _SHOWN[0] else 0, 0)
        header.style().unpolish(header)
        header.style().polish(header)
        header.viewport().update()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            self.place()
        return False

    def place(self):
        target = self.target
        inset = 4
        x = target.width() - SIZE - inset
        y = inset
        if isinstance(target, QGroupBox):
            x, y = target.width() - SIZE - 8, 0
        elif isinstance(target, QAbstractScrollArea):
            bar = target.verticalScrollBar()
            if bar.isVisible():
                x -= bar.width()
        self.button.move(max(0, x), y)
        self.button.raise_()


HEADER_ROOM = SIZE + 10  # what the last column's title keeps free for a (?) in a table's corner


def header_of(target):
    """The visible column header of a table or tree TARGET, or None."""
    from PySide6.QtWidgets import QTableView, QTreeView
    header = target.horizontalHeader() if isinstance(target, QTableView) else \
        target.header() if isinstance(target, QTreeView) else None
    return header if header is not None and not header.isHidden() else None


def corner(target, tip_id):
    button = HelpButton(tip_id, target)
    button.corner = Corner(target, button)
    button.show()
    return button


def group_of(widget):
    while widget is not None and not isinstance(widget, QGroupBox):
        widget = widget.parentWidget()
    return widget


def layout_holding(layout, widget):
    """(the layout, index) of the item for WIDGET in LAYOUT or a layout nested in it, or (None, -1)."""
    if layout is None:
        return None, -1
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is widget:
            return layout, index
        if item.layout() is not None:
            found = layout_holding(item.layout(), widget)
            if found[0] is not None:
                return found
    return None, -1


def after(widget, tip_id):
    """A (?) right after WIDGET in its row (a box or flow layout); None where it has none."""
    parent = widget.parentWidget()
    layout, index = layout_holding(parent.layout() if parent is not None else None, widget)
    if layout is None:
        return None
    button = HelpButton(tip_id)
    if isinstance(layout, QBoxLayout):
        layout.insertWidget(index + 1, button, 0, Qt.AlignmentFlag.AlignVCenter)
    elif isinstance(layout, FlowLayout):
        layout.addChildWidget(button)
        layout.items.insert(index + 1, QWidgetItem(button))
        layout.invalidate()
    else:
        button.deleteLater()
        return None
    return button


def place(target, tip_id, how):
    if how == "after":
        return after(target, tip_id)
    if how == "group":
        group = group_of(target)
        return corner(group, tip_id) if group is not None else corner(target, tip_id)
    if isinstance(target, QTabWidget) and target.cornerWidget(Qt.Corner.TopRightCorner) is None:
        button = HelpButton(tip_id)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(4, 2, 4, 2)
        row.addWidget(button)
        target.setCornerWidget(holder, Qt.Corner.TopRightCorner)
        return button
    return corner(target, tip_id)


def install(window, pages=None, tree=None):
    """The (?)s of help_texts.PLACES on the window's pages (PAGES: {key: page}, those only - a window that makes its
    pages later installs each then), and the short explanations on hover in TREE (default: the whole window; those stay
    when the (?)s are off); returns {page: [HelpButton]} and keeps what could not be placed in window.help_missing (the
    tests want it empty)."""
    placed, missing = {}, []
    for page, places in H.PLACES.items():
        if pages is not None and page not in pages:
            continue
        widget = getattr(window, page, None) if pages is None else pages[page]
        for attribute, tip_id, how in places:
            target = getattr(widget, attribute, None) if widget is not None else None
            button = place(target, tip_id, how) if isinstance(target, QWidget) else None
            if button is None:
                missing.append((page, attribute))
            else:
                placed.setdefault(page, []).append(button)
                explain_place(target, tip_id, how)
    explain_tree(window if tree is None else tree)  # the pages and the panels they put in docks of their own
    window.help_missing = missing if pages is None else \
        [entry for entry in getattr(window, "help_missing", []) if entry[0] not in pages] + missing
    return placed


# -- short explanations on hover: a tooltip on the terms, labels and column titles that have a help text --
# They do not depend on the setting help_buttons: switched off, the (?)s go and hovering still explains.

HOVER_LIMIT = 230  # characters at most: one or two sentences


def first_sentences(text, limit=HOVER_LIMIT):
    """The first sentence of TEXT (the second too when the first is very short), at most LIMIT characters."""
    text = " ".join(str(text).split())
    sentences = re.split(r"(?<=[.!?])\s+(?=[^a-zäöü])", text)
    short = sentences[0]
    if len(short) < 40 and len(sentences) > 1:
        short += " " + sentences[1]
    if len(short) > limit:
        short = short[:limit].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return short


def summary(tip_id, language=None):
    """The short explanation of the (?) TIP_ID: the first sentence of what it is."""
    return first_sentences(H.say(H.tip(tip_id).what, language))


def term_text(label, language=None):
    """The short explanation of a column title or form LABEL as shown (help_texts.TERMS), or ""."""
    found = H.term(label, language)
    return H.say(found, language) if found is not None else ""


def hover(widget, text):
    """TEXT as WIDGET's tooltip, unless it has one already (the control's own words win); True when set. A group
    box explains itself only over its title (title_hover)."""
    if not text or widget is None:
        return False
    if isinstance(widget, QGroupBox):
        return title_hover(widget, text)
    if widget.toolTip():
        return False
    widget.setToolTip(safe_tooltip(text))
    return True


class TitleTip(QObject):
    """The explanation of a group box shown over its title only (not over everything inside the box)."""

    def __init__(self, box, text):
        super().__init__(box)
        self.text = text
        box.installEventFilter(self)

    def eventFilter(self, box, event):  # noqa: N802
        if event.type() == QEvent.Type.ToolTip and not box.toolTip() and title_rect(box).contains(event.pos()):
            QToolTip.showText(event.globalPos(), safe_tooltip(self.text), box, title_rect(box))
            return True
        return False


def title_rect(box):
    """Where a group box's title is (its whole top band, so the words are easy to hit)."""
    option = QStyleOptionGroupBox()
    box.initStyleOption(option)
    label = box.style().subControlRect(QStyle.ComplexControl.CC_GroupBox, option,
                                       QStyle.SubControl.SC_GroupBoxLabel, box)
    return label.adjusted(-4, -2, 4, 2) if label.isValid() and not label.isEmpty() else \
        box.rect().adjusted(0, 0, 0, -max(0, box.height() - box.fontMetrics().height() - 6))


def title_hover(box, text):
    if not QGroupBox.title(box) or box.property("hoverTip"):
        return False
    box.setProperty("hoverTip", text)
    TitleTip(box, text)
    return True


def hover_text(widget):
    """What hovering WIDGET explains (its tooltip, or a group box's title explanation), "" for nothing."""
    return widget.toolTip() or str(widget.property("hoverTip") or "")


def label_of(widget):
    """The label in front of WIDGET: its form label, or a QLabel just before it in its row; None."""
    parent = widget.parentWidget()
    if parent is None:
        return None
    for form in parent.findChildren(QFormLayout):
        label = form.labelForField(widget)
        if isinstance(label, QLabel):
            return label
    layout, index = layout_holding(parent.layout(), widget)
    if layout is not None and index > 0:
        before = layout.itemAt(index - 1).widget()
        if isinstance(before, QLabel) and before.text():
            return before
    return None


def explain_place(target, tip_id, how):
    """The short explanation of a (?) of PLACES also on hover, where it can be shown without covering a whole area:
    the control and its label ("after"), the title of the group box around it ("group"); a corner's table explains
    its columns instead (explain_tree)."""
    text = summary(tip_id)
    if how == "after":
        hover(target, text)
        hover(label_of(target), text)
    elif how == "group":
        group = group_of(target)
        if group is not None:
            title_hover(group, text)
    elif isinstance(target, QGroupBox):
        title_hover(target, text)


def explain_columns(view):
    """The column titles of the table or tree VIEW that are terms of help_texts.TERMS explain themselves on hover
    (the model's header tooltip, which a model of its own may give itself)."""
    model = view.model()
    if model is None:
        return 0
    count = 0
    horizontal = Qt.Orientation.Horizontal
    for section in range(model.columnCount()):
        if model.headerData(section, horizontal, Qt.ItemDataRole.ToolTipRole):
            continue
        text = term_text(model.headerData(section, horizontal, Qt.ItemDataRole.DisplayRole) or "")
        if text and model.setHeaderData(section, horizontal, safe_tooltip(text), Qt.ItemDataRole.ToolTipRole):
            count += 1
    return count


def explain_tree(root):
    """Short explanations on hover for everything in ROOT that has a help text: form labels (and their fields) and
    group titles that are terms of help_texts.TERMS, a form label whose field explains itself, and the column
    titles of tables and trees. What has a tooltip already keeps it; a second call adds only what is new."""
    for form in root.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field_item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            label = label_item.widget() if label_item is not None else None
            field = field_item.widget() if field_item is not None else None
            if not isinstance(label, QLabel) or not label.text():
                continue
            text = term_text(label.text())
            if text:
                hover(label, text)
                hover(field, text)
            elif field is not None and field.toolTip() and not label.toolTip():
                tip = field.toolTip()  # the field's own words, made safe where they were set
                label.setToolTip(tip)
    for box in root.findChildren(QGroupBox):
        hover(box, term_text(QGroupBox.title(box)))  # (a subclass may name something else title)
    for view in root.findChildren(QAbstractItemView):
        if isinstance(view, (QTableView, QTreeView)):
            explain_columns(view)


def explain_windows(app):
    """Every dialog or window that gets the focus explains its terms on hover too (once per window): the dialogs
    of the pages are made when they are opened."""
    def focused(_old, new):
        window = new.window() if new is not None else None
        if window is not None and not window.property("hoverExplained"):
            window.setProperty("hoverExplained", True)
            explain_tree(window)
    if not app.property("hoverExplains"):  # once per application, however many windows install it
        app.setProperty("hoverExplains", True)
        app.focusChanged.connect(focused)


# -- the Help area --

def render(section, language):
    """SECTION as HTML (its texts escaped: nothing in them is markup but `code`, - bullets and paragraphs)."""
    def inline(text):
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text))
    parts = [f"<h2>{html.escape(H.say(section.title, language))}</h2>"]
    for block in H.say(section.body, language).split("\n\n"):
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        bullets = [line[2:] for line in lines if line.startswith("- ")]
        text = " ".join(line for line in lines if not line.startswith("- "))
        if text:
            parts.append(f"<p>{inline(text)}</p>")
        if bullets:
            parts.append("<ul>" + "".join(f"<li>{inline(b)}</li>" for b in bullets) + "</ul>")
    if section.cli:
        label = html.escape(tr_in(language, N_("Command line")))
        parts.append(f"<p><b>{label}:</b> " + ", ".join(f"<code>{html.escape(c)}</code>" for c in section.cli)
                     + "</p>")
    from . import help_content
    links = [f'<a href="section:{s}">{html.escape(H.say(help_content.BY_ID[s].title, language))}</a>'
             for s in section.see if s in help_content.BY_ID]
    if links:
        parts.append(f"<p><b>{html.escape(tr_in(language, N_('See also')))}:</b> " + " · ".join(links) + "</p>")
    return "\n".join(parts)


def tr_in(language, key, **values):
    """tr() in LANGUAGE (None: the app's)."""
    if language is None or language == i18n.language():
        return tr(key, **values)
    entry = i18n.TEXTS.get(key) or {}
    text = entry.get(language) or key
    return text.format(**values) if values else text


def tour_title(ident, language=None):
    """The title of a tour: a task tour's own, or "Tour of PAGE" for area:PAGE."""
    if ident.startswith("area:"):
        from . import guide
        page = ident[len("area:"):]
        title = next((step.title for step in guide.TOUR if step.page == page), page)
        return tr_in(language, N_("Tour of the page {page}"), page=tr_in(language, title))
    return H.say(tours.task_tour(ident).title, language)


ALL_TOURS = "tours"  # the index entry listing every tour


class HelpPage(QWidget):
    """The Help area: search, the sections by group, the text, the tours to start."""
    tour_requested = Signal(str)  # a task tour's id, or area:PAGE

    def __init__(self, parent=None, with_tours=True):
        super().__init__(parent)
        from . import help_content
        self.content, self.with_tours, self.shown = help_content, with_tours, None
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setAccessibleName(tr("Search the help"))
        self.search.setPlaceholderText(tr("Search the help, in English or German"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter)
        top.addWidget(self.search, 1)
        top.addWidget(HelpButton("help.search"))
        if with_tours:
            self.tour_about_button = QPushButton(tr("Tour about…"))
            self.tour_about_button.setToolTip(tr("Type a topic: the tours and Help sections about it, best first "
                                                 "(Ctrl+F1)"))
            self.tour_about_button.clicked.connect(lambda _=False: self.tour_about())
            top.addWidget(self.tour_about_button)
        self.language = QComboBox()
        self.language.setAccessibleName(tr("Language"))
        self.language.addItem(tr("Language of the app"), None)
        for code, name in zip(i18n.LANGUAGES, ("English", "Deutsch")):
            self.language.addItem(name, code)
        self.language.currentIndexChanged.connect(lambda _i: self.relabel())
        top.addWidget(self.language)
        top.addWidget(HelpButton("help.language"))
        layout.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.index = QListWidget()
        self.index.setAccessibleName(tr("Sections"))
        self.index.setMinimumWidth(170)
        self.index.currentItemChanged.connect(lambda item, _old: self.show_item(item))
        split.addWidget(self.index)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.text = QTextBrowser()
        self.text.setAccessibleName(tr("Help text"))
        self.text.setOpenLinks(False)
        self.text.anchorClicked.connect(lambda url: self.link(url.toString()))
        right_layout.addWidget(self.text, 1)
        self.tours = QWidget()
        tours_row = QHBoxLayout(self.tours)
        tours_row.setContentsMargins(0, 0, 0, 0)
        self.tour_buttons = QWidget()
        self.tour_flow = FlowLayout(self.tour_buttons)
        tours_row.addWidget(self.tour_buttons, 1)
        tours_row.addWidget(HelpButton("help.tours"), 0, Qt.AlignmentFlag.AlignTop)
        right_layout.addWidget(self.tours)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 700])
        layout.addWidget(split, 1)
        self.empty = plain_label(role="secondary")
        self.empty.hide()
        layout.addWidget(self.empty)
        self.fill()
        self.show_section("page-help")

    def lang(self):
        return self.language.currentData()

    def groups(self):
        return (("app", tr_in(self.lang(), N_("The app"))), ("concepts", tr_in(self.lang(), N_("Concepts"))))

    def fill(self):
        """The index: a heading per group (not selectable), its sections, then the tours."""
        self.index.blockSignals(True)
        self.index.clear()
        self.items = {}
        for key, title in self.groups():
            header = QListWidgetItem(title.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.UserRole + 1, key)
            self.index.addItem(header)
            for section in self.content.SECTIONS:
                if section.group == key:
                    item = QListWidgetItem("  " + H.say(section.title, self.lang()))
                    item.setData(Qt.ItemDataRole.UserRole, section.id)
                    self.index.addItem(item)
                    self.items[section.id] = item
        if self.with_tours:
            header = QListWidgetItem(tr_in(self.lang(), N_("Tours")).upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.UserRole + 1, ALL_TOURS)
            self.index.addItem(header)
            item = QListWidgetItem("  " + tr_in(self.lang(), N_("All tours")))
            item.setData(Qt.ItemDataRole.UserRole, ALL_TOURS)
            self.index.addItem(item)
            self.items[ALL_TOURS] = item
        self.index.blockSignals(False)

    def relabel(self):
        shown = self.shown
        self.fill()
        self.filter(self.search.text())
        self.show_section(shown or "page-help")

    def matches(self, ident, words):
        if not words:
            return True
        if ident == ALL_TOURS:
            haystack = " ".join(H.say(t.title, lang) + " " + H.say(t.summary, lang)
                                for t in tours.TASK_TOURS.values() for lang in i18n.LANGUAGES)
        else:
            section = self.content.BY_ID[ident]
            haystack = " ".join([section.title.en, section.title.de, section.body.en, section.body.de,
                                 " ".join(section.cli)])
        haystack = haystack.lower()
        return all(word in haystack for word in words)

    def filter(self, text):
        """Only the sections with every word of TEXT (in either language); headings of empty groups go."""
        words = text.lower().split()
        header, visible_in_group, first = None, 0, None
        for row in range(self.index.count()):
            item = self.index.item(row)
            ident = item.data(Qt.ItemDataRole.UserRole)
            if ident is None:
                if header is not None:
                    header.setHidden(visible_in_group == 0)
                header, visible_in_group = item, 0
                continue
            hidden = not self.matches(ident, words)
            item.setHidden(hidden)
            if not hidden:
                visible_in_group += 1
                first = first or ident
        if header is not None:
            header.setHidden(visible_in_group == 0)
        self.empty.setText(tr("Nothing matches {text}.", text=text.strip()) if first is None else "")
        self.empty.setVisible(first is None)
        if first is not None and (self.shown not in self.items or self.items[self.shown].isHidden()):
            self.show_section(first)

    def show_item(self, item):
        if item is not None and item.data(Qt.ItemDataRole.UserRole):
            self.show_section(item.data(Qt.ItemDataRole.UserRole))

    def show_section(self, ident):
        """Show section IDENT (a help_content id, or "tours" for every tour); False for an unknown one."""
        if ident != ALL_TOURS and ident not in self.content.BY_ID:
            return False
        self.shown = ident
        language = self.lang()
        if ident == ALL_TOURS:
            rows = [f"<h2>{html.escape(tr_in(language, N_('All tours')))}</h2>"]
            for tour in tours.TASK_TOURS.values():
                rows.append(f'<p><a href="tour:{tour.id}"><b>{html.escape(H.say(tour.title, language))}</b></a><br>'
                            f"{html.escape(H.say(tour.summary, language))}</p>")
            rows.append(f"<h3>{html.escape(tr_in(language, N_('Tours of the pages')))}</h3><p>")
            rows.append(" · ".join(f'<a href="tour:area:{page}">{html.escape(tour_title("area:" + page, language))}'
                                   f"</a>" for page in tours.AREA_TOURS))
            rows.append("</p>")
            self.text.setHtml("\n".join(rows))
            wanted = tuple(tours.TASK_TOURS)
        else:
            section = self.content.BY_ID[ident]
            self.text.setHtml(render(section, language))
            wanted = section.tours
        self.set_tours(wanted if self.with_tours else ())
        item = self.items.get(ident)
        if item is not None and self.index.currentItem() is not item:
            self.index.blockSignals(True)
            self.index.setCurrentItem(item)
            self.index.blockSignals(False)
            self.index.scrollToItem(item)
        return True

    def set_tours(self, idents):
        while self.tour_flow.count():
            taken = self.tour_flow.takeAt(0)
            if taken.widget() is not None:
                taken.widget().deleteLater()
        for ident in idents:
            if not ident.startswith("area:") and ident not in tours.TASK_TOURS:
                continue
            button = QPushButton(tr_in(self.lang(), N_("Start: {tour}"), tour=tour_title(ident, self.lang())))
            button.clicked.connect(lambda _=False, i=ident: self.tour_requested.emit(i))
            self.tour_flow.addWidget(button)
        self.tour_flow.invalidate()
        self.tours.setVisible(bool(idents))

    def tour_about(self, text=None):
        """Tour about…: a topic in, the tours and sections about it out; a tour starts (tour_requested), a section
        opens here."""
        dialog = TourSearchDialog(self, self.search.text() if text is None else text)
        dialog.tour_requested.connect(self.tour_requested.emit)
        dialog.section_requested.connect(self.show_section)
        dialog.show()
        self.tour_dialog = dialog
        return dialog

    def link(self, target):
        if target.startswith("section:"):
            self.show_section(target[len("section:"):])
        elif target.startswith("tour:"):
            self.tour_requested.emit(target[len("tour:"):])


KIND_LABELS = {"task": N_("Tour through a task"), "area": N_("Tour of a page"), "section": N_("Help section")}


class TourSearchDialog(QDialog):
    """Tour about…: the human types a topic (English or German); the tours through a task, the tours of the pages
    and the Help sections about it are listed best first (tour_search.py). Enter or a double click starts the
    tour (tour_requested: its id or area:PAGE) or opens the section (section_requested: its id)."""
    tour_requested = Signal(str)
    section_requested = Signal(str)

    def __init__(self, parent=None, text=""):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(tr("Tour about…"))
        self.resize(620, 460)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("Which topic? A tour shows it step by step on the pages; a Help section "
                                        "explains it."), role="secondary"))
        self.query = QLineEdit()
        self.query.setPlaceholderText(tr("A topic, e.g. network, presets, kits, Netzwerk, Vorlagen"))
        self.query.setClearButtonEnabled(True)
        self.query.textChanged.connect(lambda value: self.find(value))
        self.query.returnPressed.connect(self.start_current)
        layout.addWidget(self.query)
        self.results = QListWidget()
        self.results.itemActivated.connect(lambda item: self.start(item))
        layout.addWidget(self.results, 1)
        self.status = plain_label(role="secondary")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.start_button = QPushButton(tr("Start the tour"))
        self.start_button.setProperty("role", "primary")
        self.start_button.clicked.connect(lambda _=False: self.start_current())
        close = QPushButton(tr("Close"))
        close.clicked.connect(lambda _=False: self.close())
        buttons.addWidget(self.start_button)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.results.currentItemChanged.connect(lambda item, _old: self.update_button(item))
        self.query.setText(text or "")
        self.find(self.query.text())
        self.query.setFocus()

    def find(self, text):
        from . import tour_search
        self.results.clear()
        hits = tour_search.search(text)
        for hit in hits:
            item = QListWidgetItem(f"{tr(KIND_LABELS[hit.kind])}:  {H.say(hit.title)}")
            item.setToolTip(safe_tooltip(H.say(hit.summary)))
            item.setData(Qt.ItemDataRole.UserRole, (hit.kind, hit.ident))
            self.results.addItem(item)
        if hits:
            self.results.setCurrentRow(0)
        self.status.setText("" if not text.strip() else tr("Nothing about {text}: try another word, in English or "
                                                           "German.", text=text.strip()) if not hits else
                            tr("{count} found; Enter starts the first.", count=len(hits)))
        self.update_button(self.results.currentItem())
        return hits

    def update_button(self, item):
        kind = item.data(Qt.ItemDataRole.UserRole)[0] if item is not None else None
        self.start_button.setEnabled(kind is not None)
        self.start_button.setText(tr("Open the section") if kind == "section" else tr("Start the tour"))

    def start_current(self):
        self.start(self.results.currentItem())

    def start(self, item):
        if item is None:
            return
        kind, ident = item.data(Qt.ItemDataRole.UserRole)
        if kind == "section":
            self.section_requested.emit(ident)
        else:
            self.tour_requested.emit(ident)
        self.close()


class HelpDialog(QDialog):
    """The Help area in a window of its own (from a (?) in a modal dialog)."""

    def __init__(self, section=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(tr("Help"))
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        self.page = HelpPage(self, with_tours=False)
        layout.addWidget(self.page)
        if section:
            self.page.show_section(section)
