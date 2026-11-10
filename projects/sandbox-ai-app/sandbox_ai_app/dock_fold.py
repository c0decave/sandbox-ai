"""A panel (QDockWidget) that can also be folded away while it is docked.

Its own title bar keeps what Qt's has - the title (drag it to move the panel to another edge of the window or out of
it; a double click undocks it or docks it again), the float button and the close button - and adds a fold button:
folded, a panel docked at the left or the right is a slim strip with its title turned, one at the top or the bottom
its title bar alone (and one that shares its column or row with another panel folds the other way, leaving the
other panel its room). A floating panel is never folded (undocking a folded one unfolds it)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QBoxLayout, QDockWidget, QLabel, QSizePolicy, QStyle, QToolButton, QWidget,
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
        self.float_button = self.button(lambda: dock.setFloating(not dock.isFloating()))
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
            if self.dock.width() > 2 * self.bar.sizeHint().width():
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
