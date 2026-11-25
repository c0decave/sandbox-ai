"""A panel (QDockWidget) that can also be folded away while it is docked.

Its own title bar keeps what Qt's has - the title (drag it to move the panel to another edge of the window or out of
it; a double click undocks it or docks it again), the float button and the close button - and adds a fold button:
folded, a panel docked at the left or the right is a slim strip with its title turned, one at the top or the bottom
its title bar alone (and one that shares its column or row with another panel folds the other way, leaving the
other panel its room). A floating panel is never folded (undocking a folded one unfolds it).

Undocked by its float button or a double click on its title, a panel is a window centred over the app's window and
sized to what it shows (FloatPlacement) - unless the user moved or resized it while it floated before (in this app
session): then it comes back where they had it. A panel dragged out stays where it was dropped, unless that leaves it
partly off the screen."""
from __future__ import annotations

import time
import weakref

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QBoxLayout, QDockWidget, QLabel, QScrollArea, QSizePolicy, QStyle, QToolButton,
    QWidget,
)

from .i18n import tr
from .widgets import safe_tooltip

UNLIMITED = (1 << 24) - 1  # QWIDGETSIZE_MAX
SIDES = (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea)
VERTICAL_BAR = QDockWidget.DockWidgetFeature.DockWidgetVerticalTitleBar


class TurnedTitle(QWidget):
    """The panel's title written along the strip (top to bottom) while it is folded at the left or the right."""

    def __init__(self, text=""):
        super().__init__()
        self.text = text
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_text(self, text):
        self.text = text
        self.updateGeometry()
        self.update()

    def sizeHint(self):  # noqa: N802
        metrics = self.fontMetrics()
        return QSize(metrics.height() + 6, metrics.horizontalAdvance(self.text) + 16)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(self.fontMetrics().height() + 6, 0)

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.translate(self.width(), 0)
        painter.rotate(90)
        room = self.height() - 8
        text = self.fontMetrics().elidedText(self.text, Qt.TextElideMode.ElideRight, max(room, 0))
        painter.drawText(4, 0, room, self.width(), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         text)
        painter.end()


class DockTitleBar(QWidget):
    """The title bar of a foldable panel: its title, then fold, float and close (Qt draws none of its own buttons
    under a title bar widget). Presses on the title are left to the panel, so dragging and double clicks work as on
    Qt's own title bar."""

    def __init__(self, fold):
        super().__init__()
        self.fold = fold
        dock = fold.dock
        self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.box.setContentsMargins(6, 2, 2, 2)
        self.box.setSpacing(2)
        self.label = QLabel(dock.windowTitle())
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.label.elides = True  # (tests/test_layout.py: a narrow panel cuts its title on purpose)
        self.turned = TurnedTitle(dock.windowTitle())
        self.fold_button = self.button(fold.toggle)
        self.float_button = self.button(lambda: float_out(dock) if not dock.isFloating() else dock.setFloating(False))
        self.close_button = self.button(dock.close)
        self.close_button.setToolTip(tr("Close the panel (View > Panels brings it back)"))
        self.close_button.setAccessibleName(self.close_button.toolTip())
        dock.windowTitleChanged.connect(self.set_title)
        self.vertical = None
        self.arrange(False)

    def button(self, slot):
        button = QToolButton()
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the canvas keeps the keyboard; the panel's menu does it all
        button.clicked.connect(lambda _checked=False: slot())
        return button

    def set_title(self, text):
        self.label.setText(text)
        self.label.setToolTip(safe_tooltip(text))  # (a panel's title: the app's words, shown as text)
        self.turned.set_text(text)

    def arrange(self, vertical):
        """The order of the title and the buttons: in a row, or (folded at a side) down the strip."""
        if vertical == self.vertical:
            return
        self.vertical = vertical
        while self.box.count():
            self.box.takeAt(0)
        if vertical:
            self.box.setDirection(QBoxLayout.Direction.TopToBottom)
            self.box.setContentsMargins(1, 4, 1, 4)
            for widget in (self.fold_button, self.float_button, self.close_button):
                self.box.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
            self.box.addWidget(self.turned, 1, Qt.AlignmentFlag.AlignHCenter)
        else:
            self.box.setDirection(QBoxLayout.Direction.LeftToRight)
            self.box.setContentsMargins(6, 2, 2, 2)
            self.box.addWidget(self.label, 1)
            for widget in (self.fold_button, self.float_button, self.close_button):
                self.box.addWidget(widget)
        self.label.setVisible(not vertical)
        self.turned.setVisible(vertical)

    def show_state(self, folded, area, floating):
        """The buttons as they are now: the fold button's arrow points where the panel goes."""
        dock = self.fold.dock
        features = dock.features()
        self.fold_button.setVisible(not floating)
        self.float_button.setVisible(bool(features & QDockWidget.DockWidgetFeature.DockWidgetFloatable))
        self.close_button.setVisible(bool(features & QDockWidget.DockWidgetFeature.DockWidgetClosable))
        towards = {Qt.DockWidgetArea.LeftDockWidgetArea: Qt.ArrowType.LeftArrow,
                   Qt.DockWidgetArea.TopDockWidgetArea: Qt.ArrowType.UpArrow,
                   Qt.DockWidgetArea.BottomDockWidgetArea: Qt.ArrowType.DownArrow}.get(area, Qt.ArrowType.RightArrow)
        away = {Qt.ArrowType.LeftArrow: Qt.ArrowType.RightArrow, Qt.ArrowType.RightArrow: Qt.ArrowType.LeftArrow,
                Qt.ArrowType.UpArrow: Qt.ArrowType.DownArrow, Qt.ArrowType.DownArrow: Qt.ArrowType.UpArrow}
        self.fold_button.setArrowType(away[towards] if folded else towards)
        fold_text = tr("Expand the panel") if folded else tr("Collapse the panel (a strip at the window's edge)")
        self.fold_button.setToolTip(safe_tooltip(fold_text))
        self.fold_button.setAccessibleName(fold_text)
        style = self.style()  # (the icons of Qt's own title bar)
        self.float_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton))
        float_text = tr("Dock the panel again (or drag its title to an edge of the window)") if floating else \
            tr("Undock the panel as a window of its own (or drag its title out of the window)")
        self.float_button.setToolTip(safe_tooltip(float_text))
        self.float_button.setAccessibleName(float_text)
        self.close_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton))

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.FontChange):
            self.fold.apply()  # another theme: the strip's width


class DockFold(QObject):
    """Folds DOCK (a panel of the main window WINDOW) to its title bar while it is docked, and back. FOLDED: how
    it starts. sizes: its width at a side and its height at the top or the bottom when unfolded (kept by the
    window across starts)."""
    changed = Signal(bool)  # folded (True) or unfolded, by its button or the app

    def __init__(self, dock, window, folded=False, width=340, height=260):
        super().__init__(dock)
        self.dock, self.window, self.folded = dock, window, bool(folded)
        self.applying = True  # (until it is made: a theme's change reaches the title bar early)
        self.sizes = {"width": width, "height": height}
        self.bar = DockTitleBar(self)
        dock.setTitleBarWidget(self.bar)
        dock.topLevelChanged.connect(self.floated)
        dock.dockLocationChanged.connect(lambda _area: self.apply())
        dock.featuresChanged.connect(lambda _features: self.apply())
        for other in window.findChildren(QDockWidget):  # another panel beside it or below it: maybe the other way
            if other is not dock:
                other.dockLocationChanged.connect(lambda _area: self.apply_soon())
                other.topLevelChanged.connect(lambda _floating: self.apply_soon())
                other.visibilityChanged.connect(lambda _shown: self.apply_soon())
        self.pending = False  # a change while the panel is not shown: applied when it is shown again
        dock.visibilityChanged.connect(lambda shown: shown and self.pending and self.apply_soon())
        self.applying = False
        self.apply()

    def area(self):
        return self.window.dockWidgetArea(self.dock)

    def as_strip(self):
        """Folded along the edge (a strip as wide as its title bar is high) - at the left or the right, unless
        another panel shares its column (it would be squeezed to the strip): then across, like at the top or the
        bottom unless another panel shares its row."""
        area, dock = self.area(), self.dock
        side = area in SIDES
        tabbed = set(self.window.tabifiedDockWidgets(dock))
        mine = dock.geometry()
        for other in self.window.findChildren(QDockWidget):
            if other is dock or other in tabbed or other.isFloating() or other.isHidden() \
                    or self.window.dockWidgetArea(other) != area:
                continue
            place = other.geometry()
            if side and place.left() < mine.right() and mine.left() < place.right():
                return False  # above or below it, in its column
            if not side and place.top() < mine.bottom() and mine.top() < place.bottom():
                return True  # beside it, in its row
        return side

    def apply_soon(self):
        QTimer.singleShot(0, self, self.apply)  # (once the window placed them; never after it is gone)

    def is_folded(self):
        """Folded as it is now (a floating panel never is)."""
        return self.folded and not self.dock.isFloating()

    def fold(self):
        self.set_folded(True)

    def unfold(self):
        self.set_folded(False)

    def toggle(self):
        self.set_folded(not self.is_folded())

    def set_folded(self, folded):
        """Fold the docked panel to its title bar (FOLDED) or unfold it to the size it had; a floating one stays."""
        folded = bool(folded)
        if self.dock.isFloating():
            folded = False
        if folded == self.folded:
            return
        strip = self.as_strip()
        if folded:
            self.remember_size()
        self.folded = folded
        self.apply()
        if not folded:
            self.restore_size(strip)
        self.changed.emit(folded)

    def remember_size(self):
        if self.dock.isFloating() or not self.dock.isVisible():
            return
        if self.as_strip():
            fitted = self.dock.property(FITTED)  # (widened for a part's rows, fit_docked: not the user's width)
            if self.dock.width() > 2 * self.bar.sizeHint().width() and not (
                    isinstance(fitted, int) and abs(self.dock.width() - fitted) <= 2):
                self.sizes["width"] = self.dock.width()
        elif self.dock.height() > 2 * self.bar.sizeHint().height():
            self.sizes["height"] = self.dock.height()

    def restore_size(self, strip):
        if self.dock.isFloating() or self.area() == Qt.DockWidgetArea.NoDockWidgetArea:
            return
        if strip:
            self.window.resizeDocks([self.dock], [self.sizes["width"]], Qt.Orientation.Horizontal)
        else:
            self.window.resizeDocks([self.dock], [self.sizes["height"]], Qt.Orientation.Vertical)

    def floated(self, floating):
        """Undocked: never folded (a floating panel unfolds); too small to be used (it was folded): its size."""
        if floating and self.folded:
            self.folded = False
            self.apply()
            self.changed.emit(False)
        else:
            self.apply()
        if floating and (self.dock.width() < 160 or self.dock.height() < 160):
            self.dock.resize(max(self.dock.width(), self.sizes["width"]), max(self.dock.height(), 420))

    def apply(self):
        """Show the state: the content shown or not, the title bar along the strip or across, the panel's room."""
        if self.applying:
            return
        if not self.dock.isVisible() and not self.dock.isFloating() and self.dock.widget() is not None \
                and self.dock.widget().isHidden() == (self.folded and not self.dock.isFloating()):
            # A panel of another page (hidden as that page is not shown): nothing to look at now - every page switch
            # applied every panel, each looking through all panels' places (as_strip). Done when it is shown.
            self.pending = True
            return
        self.pending = False
        self.applying = True
        try:
            dock, floating, area = self.dock, self.dock.isFloating(), self.area()
            folded = self.folded and not floating
            vertical = folded and self.as_strip()  # a strip along the edge, its title turned
            content = dock.widget()
            if content is not None and content.isHidden() != folded:
                content.setVisible(not folded)
            features = dock.features()
            wanted = (features | VERTICAL_BAR) if vertical else (features & ~VERTICAL_BAR)
            if wanted != features:
                dock.setFeatures(wanted)
            self.bar.arrange(vertical)
            self.bar.show_state(folded, area, floating)
            if vertical:
                dock.setMaximumSize(self.bar.sizeHint().width(), UNLIMITED)
            elif folded:
                dock.setMaximumSize(UNLIMITED, self.bar.sizeHint().height())
            else:
                dock.setMaximumSize(UNLIMITED, UNLIMITED)
        finally:
            self.applying = False


# -- a panel floated out of the window --

MIN_FLOAT = QSize(320, 240)  # a floating panel at least this big (when the screen has the room)
SCREEN_SHARE = 0.9  # ... and at most this share of the screen's available area


def float_geometry(wanted, minimum, over, available, maximum=None, least=MIN_FLOAT):
    """Where a panel floated out goes: WANTED's size (its content's), at least MINIMUM and LEAST (MIN_FLOAT), at most
    SCREEN_SHARE of AVAILABLE (the screen's available area) and MAXIMUM (the panel's own), centred over OVER (the
    app's window; None: where WANTED is, a QRect then) and kept inside AVAILABLE."""
    cap = QSize(int(available.width() * SCREEN_SHARE), int(available.height() * SCREEN_SHARE))
    if maximum is not None:
        cap = cap.boundedTo(maximum)
    size = wanted.size() if isinstance(wanted, QRect) else wanted
    width = min(max(size.width(), minimum.width(), least.width()), cap.width())
    height = min(max(size.height(), minimum.height(), least.height()), cap.height())
    placed = QRect(0, 0, width, height)
    placed.moveCenter(over.center() if over is not None else wanted.center())
    placed.moveLeft(max(available.left(), min(placed.left(), available.left() + available.width() - width)))
    placed.moveTop(max(available.top(), min(placed.top(), available.top() + available.height() - height)))
    return placed


def content_size(dock):
    """The size DOCK needs to show its content whole: its size hint, and what its scrolled areas (the properties'
    rows) would hide beyond theirs."""
    hint = dock.sizeHint().expandedTo(dock.minimumSizeHint())
    content = dock.widget()
    more_width = more_height = 0
    if content is not None:
        for scroll in content.findChildren(QScrollArea):
            inner = scroll.widget()
            outer = scroll.parentWidget()
            while outer is not None and outer is not content and not isinstance(outer, QScrollArea):
                outer = outer.parentWidget()
            if inner is None or isinstance(outer, QScrollArea) or not scroll.widgetResizable() \
                    or scroll.isHidden():
                continue  # (only the outermost scrolled areas shown: a nested one scrolls inside its own)
            need = inner.sizeHint().expandedTo(inner.minimumSizeHint())
            frame = 2 * scroll.frameWidth()
            have = scroll.sizeHint()
            bar = scroll.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
            more_width = max(more_width, need.width() + frame + bar - have.width())
            more_height = max(more_height, need.height() + frame - have.height())
    return QSize(hint.width() + max(0, more_width), hint.height() + max(0, more_height))


DOCK_SHARE = 0.6  # a panel docked at a side: at most this share of the window's width (the page keeps the rest)
FITTED = "fittedWidth"  # the width fit_docked gave a panel (a dynamic property): not the user's own choice


def least_width(dock):
    """The least width at which nothing DOCK shows is cut sideways: its own layout's, and for each of its scrolled
    areas (the properties' rows) their content's minimum with the frame, a scroll bar beside it and what the panel
    has around the area. (Not the size it would like: content_size.)"""
    width = dock.minimumSizeHint().width()
    content = dock.widget()
    if content is None:
        return width
    for scroll in content.findChildren(QScrollArea):
        inner = scroll.widget()
        if inner is None or not scroll.widgetResizable() or scroll.isHidden() or scroll.parentWidget() is None \
                or any(isinstance(parent, QScrollArea) for parent in ancestors(scroll, content)):
            continue  # (a nested one scrolls inside its own)
        around = max(0, dock.width() - scroll.width())
        bar = scroll.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        width = max(width, inner.minimumSizeHint().width() + 2 * scroll.frameWidth() + bar + around)
    return width


def ancestors(widget, top):
    """WIDGET's parents up to (not including) TOP."""
    found, parent = [], widget.parentWidget()
    while parent is not None and parent is not top:
        found.append(parent)
        parent = parent.parentWidget()
    return found


def fit_docked(dock, window, fold=None, share=DOCK_SHARE):
    """DOCK (docked at a side of WINDOW, unfolded) made wide enough that nothing it shows is cut (least_width), at
    most SHARE of WINDOW's width, and never narrower than the user left it: FOLD (its DockFold) keeps that width, so
    the panel goes back to it for a part that needs less. True when its width was changed."""
    if dock.isFloating() or not dock.isVisible() or window.dockWidgetArea(dock) not in SIDES:
        return False
    fitted = dock.property(FITTED)
    if fold is not None and (not isinstance(fitted, int) or abs(dock.width() - fitted) > 2):
        fold.sizes["width"] = dock.width()  # (not the width this gave it: the user's own, as it is now)
    own = fold.sizes["width"] if fold is not None else dock.width()
    wanted = max(own, min(least_width(dock), int(window.width() * share)))
    if abs(wanted - dock.width()) <= 2:
        dock.setProperty(FITTED, dock.width())
        return False
    window.resizeDocks([dock], [wanted], Qt.Orientation.Horizontal)
    dock.setProperty(FITTED, wanted)
    QTimer.singleShot(0, dock, lambda: dock.setProperty(FITTED, dock.width()))  # (as the window laid it out)
    return True


def float_out(dock):
    """Undock DOCK as by its float button: centred and sized to its content (FloatPlacement)."""
    placement = getattr(dock, "placement", None)
    if placement is not None:
        placement.ask_centre()
    dock.setFloating(True)


class FloatPlacement(QObject):
    """Where DOCK goes when it floats out of WINDOW (the app's main window). Its float button (dock_fold's or Qt's
    own) or a double click on its title: centred over WINDOW on its screen, sized to its content (float_geometry,
    content_size). A drag: where it was dropped, unless partly off the screen - then centred, its size kept (settled,
    once no mouse button is held: MainWindow.settle_floating); a panel restored floating (or floated by the app) is
    only moved inside the screen. Once the user moved or resized it while it floated, a later undocking keeps theirs
    (Qt brings it back where it was)."""

    QUIET = 0.5  # seconds after the app placed it in which a move is the window manager's, not the user's

    def __init__(self, dock, window):
        super().__init__(dock)
        # (the window by a weak reference and no lambda holding the dock: a connection is held in C++, where Python's
        # collector cannot see a cycle - a closed window would stay alive with its backend; test_performance)
        self.dock, self.window_ref = dock, weakref.ref(window) if window is not None else (lambda: None)
        self.centre, self.placing, self.dragged, self.user_placed = False, False, False, False
        self.floating = dock.isFloating()  # (as topLevelChanged said: Qt moves it before it says so)
        self.placed, self.quiet_until = None, 0.0
        dock.installEventFilter(self)
        dock.topLevelChanged.connect(self.floated)
        own = dock.findChild(QAbstractButton, "qt_dockwidget_floatbutton")  # Qt's own title bar's float button
        if own is not None:
            own.pressed.connect(self.own_float_pressed)

    @property
    def window(self):
        return self.window_ref()

    def own_float_pressed(self):
        """Qt's own float button: a press while docked is the user's "undock" (centred)."""
        if not self.dock.isFloating():
            self.ask_centre()

    def ask_centre(self):
        """The next undocking (now) is the user's "undock": centred and fitted."""
        self.centre = True
        QTimer.singleShot(0, self, self.forget_centre)  # (undocked meanwhile, or not at all)

    def forget_centre(self):
        self.centre = False

    def eventFilter(self, watched, event):  # noqa: N802
        kind = event.type()
        if watched is self.dock:
            if kind == QEvent.Type.MouseButtonDblClick and not self.dock.isFloating():
                self.ask_centre()  # (Qt undocks it on the title's double click, right after this)
            elif kind in (QEvent.Type.Move, QEvent.Type.Resize) and self.floating and self.dock.isFloating() \
                    and not self.placing \
                    and time.monotonic() >= self.quiet_until and self.dock.geometry() != self.placed:
                self.user_placed = True
        return False

    def floated(self, floating):
        self.floating = bool(floating)
        if not floating:
            self.placing = False
            return
        self.placing = True
        centre, self.centre = self.centre, False
        self.dragged = not centre and QApplication.mouseButtons() != Qt.MouseButton.NoButton
        if centre:
            QTimer.singleShot(0, self, self.place)  # (once it is a window and unfolded: dock_fold's floated)

    def place(self, force=False, keep_size=False, centre=True):
        """Centred over the window and sized to its content - unless the user placed it before. FORCE: also then (it
        is partly off the screen); KEEP_SIZE: its own size kept (at most SCREEN_SHARE of the screen); CENTRE False:
        moved only as far as it takes to be inside the screen."""
        dock = self.dock
        try:
            if not dock.isFloating():
                return
            if force or not self.user_placed:
                screen = (self.window.screen() if self.window is not None and centre else None) or dock.screen()
                available = screen.availableGeometry()
                frame, inner = dock.frameGeometry(), dock.geometry()
                # (the window manager's frame around it, when it has one: kept inside the screen too)
                available = available.adjusted(inner.left() - frame.left(), inner.top() - frame.top(),
                                               inner.right() - frame.right(), inner.bottom() - frame.bottom())
                over = (self.window.frameGeometry() if self.window is not None else available) if centre else None
                wanted = inner if keep_size else content_size(dock)
                placed = float_geometry(wanted, QSize(0, 0) if keep_size else dock.minimumSizeHint(), over,
                                        available, dock.maximumSize(), least=QSize(0, 0) if keep_size else MIN_FLOAT)
                if placed != dock.geometry():
                    dock.setGeometry(placed)
            self.placed = dock.geometry()
        finally:
            self.placing = False
            self.quiet_until = time.monotonic() + self.QUIET

    def grow(self):
        """The floating panel shows something else: at least as big as that needs (content_size) and never smaller
        than it is, around the point it is centred on now (one that was centred stays so; one the user moved stays
        where they put it) - moved only as far as it takes to stay inside the screen."""
        dock = self.dock
        try:
            if not dock.isFloating():
                return
            self.placing = True
            screen = dock.screen()
            if screen is None:
                return
            available = screen.availableGeometry()
            wanted = QRect(dock.geometry().topLeft(), content_size(dock).expandedTo(dock.size()))
            wanted.moveCenter(dock.geometry().center())
            placed = float_geometry(wanted, dock.minimumSizeHint(), None, available, dock.maximumSize())
            if placed != dock.geometry():
                dock.setGeometry(placed)
            self.placed = dock.geometry()
        finally:
            self.placing = False
            self.quiet_until = time.monotonic() + self.QUIET

    def settled(self):
        """No mouse button is held any more (a drag ended, or none ran). A panel partly off the screen is brought
        back with its size: a drag's centred, one restored or floated by the app moved only that far. A drop on the
        screen stays (and counts as the user's place)."""
        dock = self.dock
        if not dock.isFloating():
            return
        dragged, self.dragged = self.dragged, False
        screen = dock.screen()
        if screen is not None and not screen.availableGeometry().contains(dock.frameGeometry()):
            self.place(force=True, keep_size=True, centre=dragged)
            return
        if dragged:
            self.user_placed = True
        self.placing = False
        self.placed = dock.geometry()
        self.quiet_until = time.monotonic() + self.QUIET
