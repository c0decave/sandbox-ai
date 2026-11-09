"""The Board page: one sandbox as a node graph (Qt view of sandbox_ai_app/board.py).

Nodes are dragged, connected from an output port (right) to an input port (left), added from the palette and
removed with Delete; the inspector edits the selected node with fields built from the launcher's own options
(their kinds, choices and help texts; what the administrator's policy forbids is greyed out). Every change is
one step of undo/redo (whole snapshots of the model). Below the graph: the command line, the problems, the
security badges and the launcher's own verdict (a dry run in the background after each change). Apply
writes .sandbox-ai.json through the same review-and-trust step as the Start page; the layout is saved next
to it. Pure Python and Qt: nothing here needs Node.js or a browser engine.
"""
from __future__ import annotations

import json
import os
import re
import time

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QDrag, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform, QUndoCommand,
    QUndoGroup, QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QGraphicsEllipseItem, QGraphicsItem,
    QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from . import backend, board as B
from . import help_view, project_trust as PT, theme
from .files import printable
from .state import choose_file, choose_folder
from .i18n import tr
from .widgets import MONO, ElidedLabel, ask_first, background, flow, inform, plain_label, safe_tooltip, warn

LOCAL = "local"  # this machine, as `sandbox-ai hosts` names it
REMOTE_CHECK_DELAY = 2000  # ms after an edit before another host's launcher checks the board (a dry run over SSH)
HOST_RETRY = 60  # seconds before another host's launcher options are asked for again after a failed read


def scrub_board(state):
    """A board's state to keep in the state file: agent arguments that may be secrets left out."""
    import shlex
    from .state import scrub_arguments
    for node in state.get("nodes", []):
        props = node.get("props", {})
        if isinstance(props.get("arguments"), str):
            try:
                props["arguments"] = shlex.join(scrub_arguments(shlex.split(props["arguments"])))
            except ValueError:
                props["arguments"] = ""
    return state


def same_folder(a, b):
    if not a or not b:
        return False
    return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))

NODE_W, NODE_H, PORT_R = 210.0, 66.0, 6.0
HEADER_H = 26.0  # the title band of a node
MIME_PART = "application/x-sandbox-ai-part"  # a part dragged from the palette: its kind


class Snapshot(QUndoCommand):
    """One change of the board: the state before and after it (redo restores after, undo before)."""

    def __init__(self, page, text, before, after):
        super().__init__(text)
        self.page, self.before, self.after = page, before, after

    def redo(self):
        self.page.restore(self.after)

    def undo(self):
        self.page.restore(self.before)


class PortItem(QGraphicsEllipseItem):
    def __init__(self, parent, side):
        super().__init__(-PORT_R, -PORT_R, 2 * PORT_R, 2 * PORT_R, parent)
        self.side = side
        self.setPos(0.0 if side == "in" else NODE_W, NODE_H / 2)
        self.setBrush(QBrush(QColor(theme.colors()["accent"])))
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setToolTip(safe_tooltip(tr("Drag from here to an input port to connect") if side == "out" else ""))
        self.setZValue(2)

    @property
    def node_id(self):
        return self.parentItem().node_id


class NodeHelpMark(QGraphicsItem):
    """The (?) in a node's title band: a click explains the part's kind."""

    def __init__(self, parent, kind):
        super().__init__(parent)
        self.kind, self.hovered = kind, False
        self.setPos(NODE_W - help_view.SIZE - 6, (HEADER_H - help_view.SIZE) / 2)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tr("What is this? {title}", title=tr(B.KINDS[kind].title)))
        self.setZValue(3)
        self.setVisible(help_view.buttons_shown())  # the setting help_buttons

    def boundingRect(self):  # noqa: N802
        return QRectF(0, 0, help_view.SIZE, help_view.SIZE)

    def paint(self, painter, option, widget=None):
        help_view.paint_mark(painter, self.boundingRect(), self.hovered)

    def hoverEnterEvent(self, event):  # noqa: N802
        self.hovered = True
        self.update()

    def hoverLeaveEvent(self, event):  # noqa: N802
        self.hovered = False
        self.update()

    def mousePressEvent(self, event):  # noqa: N802
        event.accept()  # not a move of the node

    def mouseReleaseEvent(self, event):  # noqa: N802
        event.accept()
        self.explain(event.screenPos())

    def explain(self, point=None):
        views = self.scene().views() if self.scene() is not None else []
        owner = views[0] if views else None
        if point is None and owner is not None:
            point = owner.viewport().mapToGlobal(owner.mapFromScene(self.sceneBoundingRect().bottomLeft()))
        return help_view.show_tip(f"kind.{self.kind}", owner, point or QPoint(0, 0))


class NodeItem(QGraphicsRectItem):
    def __init__(self, page, node):
        super().__init__(0.0, 0.0, NODE_W, NODE_H)
        self.page, self.node_id, self.kind = page, node.id, node.kind
        self.hint = None  # while a connection is dragged: "target" (it can take it) or "hover" (it would)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        colors = theme.colors()
        self.setBrush(QBrush(QColor(colors["node"])))
        self.title = QGraphicsSimpleTextItem(self)  # plain text: paths and names may be the agent's
        font = QFont()
        font.setBold(True)
        self.title.setFont(font)
        self.title.setBrush(QBrush(QColor(colors.get("node_title", colors["text"]))))
        self.title.setPos(10.0, 5.0)
        self.detail = QGraphicsSimpleTextItem(self)
        self.detail.setBrush(QBrush(QColor(colors["secondary"])))
        self.detail.setPos(10.0, HEADER_H + 9.0)
        has_in, has_out = B.ports(node.kind)
        self.inport = PortItem(self, "in") if has_in else None
        self.outport = PortItem(self, "out") if has_out else None
        self.help_mark = NodeHelpMark(self, node.kind)
        self.setToolTip(tr(B.KINDS[node.kind].help))
        self.setPos(node.x, node.y)
        self.update_from(node, None, True)

    def update_from(self, node, level, included):
        self.title.setText(elide(node.title(), 24))
        self.detail.setText(elide(summary(node), 34))
        color = {"error": theme.color("bad"), "warning": theme.color("warn")}.get(level, theme.colors()["border"])
        self.setPen(QPen(QColor(color), 2.5 if level else 1.2))
        self.setOpacity(1.0 if included else 0.45)

    def set_hint(self, hint):
        if hint != self.hint:
            self.hint = hint
            self.update()

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.page.node_moved(self)
        return super().itemChange(change, value)

    def paint(self, painter, option, widget=None):
        """A card: rounded as the theme's controls (square in the retro themes), a soft shadow in the modern
        ones, a title band, the outline in the colour of its worst problem, the accent when selected; while a
        connection is dragged, a dashed accent frame around each node that can take it (solid under the
        pointer)."""
        data = theme.data()
        colors, radius = data["colors"], float(min(data["radius"], 12))
        rect = self.rect()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if radius and not data.get("bevel"):
            shadow = QColor("#000000")
            shadow.setAlpha(70 if data["dark"] else 28)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shadow)
            painter.drawRoundedRect(rect.translated(0, 3), radius, radius)
        body = QPainterPath()
        body.addRoundedRect(rect, radius, radius)
        painter.fillPath(body, self.brush())
        header = QPainterPath()
        header.addRect(QRectF(rect.left(), rect.top(), rect.width(), HEADER_H))
        painter.fillPath(header.intersected(body), QColor(colors["node_header"]))
        pen = QPen(self.pen())
        if self.isSelected():
            pen = QPen(QColor(colors["accent"]), max(2.5, pen.widthF()))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(body)
        if data.get("bevel"):  # Windows 3.11: a raised frame inside the black outline
            painter.setPen(QPen(QColor(colors.get("light_edge", "#ffffff")), 1.0))
            painter.drawLine(QPointF(rect.left() + 2, rect.bottom() - 2), QPointF(rect.left() + 2, rect.top() + HEADER_H))
            painter.setPen(QPen(QColor(colors.get("dark_edge", "#808080")), 1.0))
            painter.drawLine(QPointF(rect.left() + 2, rect.bottom() - 2), QPointF(rect.right() - 2, rect.bottom() - 2))
        if self.hint:
            frame = QPainterPath()
            frame.addRoundedRect(rect.adjusted(-5, -5, 5, 5), radius + 4, radius + 4)
            painter.setPen(QPen(QColor(colors["accent"]), 3.0 if self.hint == "hover" else 1.6,
                                Qt.PenStyle.SolidLine if self.hint == "hover" else Qt.PenStyle.DashLine))
            painter.drawPath(frame)


class EdgeItem(QGraphicsPathItem):
    def __init__(self, source, target, removable):
        super().__init__()
        self.source, self.target = source, target
        self.setZValue(-1)
        self.setPen(QPen(QColor(theme.colors()["secondary"]), 2.0))
        if removable:  # the edges that decide whether a workspace, mount or MCP server counts
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
            self.setToolTip(tr("Select and press Delete to disconnect"))
        self.update_path()

    def update_path(self):
        start = self.source.outport.scenePos() if self.source.outport else self.source.sceneBoundingRect().center()
        end = self.target.inport.scenePos() if self.target.inport else self.target.sceneBoundingRect().center()
        self.setPath(curve(start, end))


def curve(start, end):
    """The bent line of an edge from START (an output port) to END (an input port)."""
    path = QPainterPath(start)
    bend = max(60.0, abs(end.x() - start.x()) / 2)
    path.cubicTo(QPointF(start.x() + bend, start.y()), QPointF(end.x() - bend, end.y()), end)
    return path


class DropGhost(QGraphicsPathItem):
    """Where a dragged part or folder will land: a dashed card in the accent colour (the danger colour when the
    board cannot take it), snapped to the grid, with what a drop does."""

    def __init__(self, label, refused=False):
        super().__init__()
        colors = theme.colors()
        color = QColor(colors["danger"] if refused else colors["accent"])
        radius = float(min(theme.data()["radius"], 12))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, NODE_W, NODE_H), radius, radius)
        self.setPath(path)
        self.setPen(QPen(color, 2.0, Qt.PenStyle.DashLine))
        fill = QColor(color)
        fill.setAlpha(40)
        self.setBrush(QBrush(fill))
        self.setZValue(10)
        self.refused = refused
        self.label = QGraphicsSimpleTextItem(elide(label, 30), self)
        self.label.setBrush(QBrush(QColor(colors["text"])))
        font = QFont()
        font.setBold(True)
        self.label.setFont(font)
        self.label.setPos(10.0, (NODE_H - self.label.boundingRect().height()) / 2)

    def move_to(self, point, grid):
        self.setPos(B.snap(point.x() - NODE_W / 2, grid), B.snap(point.y() - NODE_H / 2, grid))


class BoardScene(QGraphicsScene):
    """Connecting by dragging from an output port onto a node that can take it (its input port or anywhere on it;
    the nodes that can are framed while dragging, Esc cancels); one undo step per move (snapshot at press)."""

    def __init__(self, page):
        super().__init__()
        self.page, self.dragging, self.line, self.pressed = page, None, None, None

    def node_items(self):
        return [i for i in self.items() if isinstance(i, NodeItem)]

    def target_at(self, point):
        """The node a connection dragged from self.dragging would go to at POINT (or None)."""
        for item in self.items(point):
            node = item.parentItem() if isinstance(item, (PortItem, NodeHelpMark)) else item
            if isinstance(node, NodeItem) and node.node_id != self.dragging.node_id \
                    and self.page.board.can_connect(self.dragging.node_id, node.node_id):
                return node
        return None

    def mousePressEvent(self, event):  # noqa: N802
        port = next((i for i in self.items(event.scenePos()) if isinstance(i, PortItem) and i.side == "out"), None)
        if port is not None and event.button() == Qt.MouseButton.LeftButton:
            self.dragging = port
            self.line = self.addPath(QPainterPath(), QPen(QColor(theme.colors()["accent"]), 2.0, Qt.PenStyle.DashLine))
            self.line.setZValue(5)
            for node in self.node_items():
                node.set_hint("target" if self.page.board.can_connect(port.node_id, node.node_id) else None)
            event.accept()
            return
        self.pressed = self.page.board.state()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self.dragging is not None:
            target = self.target_at(event.scenePos())
            for node in self.node_items():
                if node.hint:
                    node.set_hint("hover" if node is target else "target")
            end = target.inport.scenePos() if target is not None and target.inport is not None else event.scenePos()
            self.line.setPath(curve(self.dragging.scenePos(), end))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self.dragging is not None:
            target = self.target_at(event.scenePos())
            source = self.dragging
            self.cancel_connect()
            if target is not None:
                self.page.connect_nodes(source.node_id, target.node_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        before, self.pressed = self.pressed, None
        if before is not None:
            self.page.moved(before)

    def cancel_connect(self):
        """End a connection being dragged without connecting (Esc, or after the release)."""
        if self.dragging is None:
            return False
        self.dragging = None
        if self.line is not None:
            self.removeItem(self.line)
            self.line = None
        for node in self.node_items():
            node.set_hint(None)
        return True


class BoardView(QGraphicsView):
    def __init__(self, scene, page):
        super().__init__(scene)
        self.page = page
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAcceptDrops(True)

    def drawBackground(self, painter, rect):  # noqa: N802
        """The canvas: the theme's colour with a dot at every grid point (the grid nodes snap to)."""
        colors = theme.colors()
        painter.fillRect(rect, QColor(colors["canvas"]))
        step = float(self.page.grid()) if hasattr(self.page, "grid") else 20.0
        if step < 4 or self.transform().m11() * step < 8:  # too dense to help
            return
        pen = QPen(QColor(colors["grid"]), 2.0 / max(self.transform().m11(), 0.01))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        left, top = int(rect.left() // step) * step, int(rect.top() // step) * step
        columns, rows = int((rect.right() - left) // step) + 1, int((rect.bottom() - top) // step) + 1
        if columns * rows > 40000:  # counted before anything is built: an exposed area may be huge
            return
        painter.drawPoints(QPolygonF([QPointF(left + c * step, top + r * step)
                                      for r in range(rows) for c in range(columns)]))

    def wheelEvent(self, event):  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)

    def zoom(self, factor):
        current = self.transform().m11()
        if 0.2 <= current * factor <= 4.0:
            self.scale(factor, factor)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:  # pan with the middle button
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        super().mouseReleaseEvent(event)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.scene().cancel_connect():
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.page.delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- drops: parts from the palette, folders and files from a file manager --

    def drag_point(self, event):
        return self.mapToScene(event.position().toPoint())

    def dragEnterEvent(self, event):  # noqa: N802
        what = self.page.drop_what(event.mimeData())
        if what is None:
            event.ignore()
            return
        self.page.show_ghost(what, self.drag_point(event))
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragMoveEvent(self, event):  # noqa: N802
        if self.page.ghost is None:
            event.ignore()
            return
        self.page.ghost.move_to(self.drag_point(event), self.page.grid())
        if self.page.ghost.refused:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragLeaveEvent(self, event):  # noqa: N802
        self.page.hide_ghost()
        event.accept()

    def dropEvent(self, event):  # noqa: N802
        what = self.page.drop_what(event.mimeData())
        refused = self.page.ghost is not None and self.page.ghost.refused
        self.page.hide_ghost()
        if what is None or refused:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        point = event.position().toPoint()
        # The drop's own event has to return before a menu or a dialog opens (the drag would hang otherwise).
        QTimer.singleShot(0, lambda: self.page.drop(what, self.mapToScene(point), self.viewport().mapToGlobal(point)))


class PartsDelegate(QStyledItemDelegate):
    """A row of the parts: its title, and a (?) at the right that explains the part."""

    def __init__(self, parts):
        super().__init__(parts)
        self.parts = parts

    def mark(self, rect):
        """Where the (?) of the row in RECT is: at its right end, inside the visible part of the list."""
        size = help_view.SIZE
        right = min(rect.right(), self.parts.viewport().width() - 1)
        return QRect(right - size - 4, rect.center().y() - size // 2 + 1, size, size)

    def paint(self, painter, option, index):
        mark = self.mark(option.rect)
        text_option = QStyleOptionViewItem(option)
        text_option.rect = QRect(option.rect.left(), option.rect.top(), max(0, mark.left() - 4 - option.rect.left()),
                                 option.rect.height())
        super().paint(painter, text_option, index)
        hovered = self.parts.hovered == (index.row(), True)
        help_view.paint_mark(painter, QRectF(mark), hovered, option.font)

    def sizeHint(self, option, index):  # noqa: N802
        hint = super().sizeHint(option, index)
        hint.setHeight(max(hint.height(), help_view.SIZE + 6))
        return hint

    def editorEvent(self, event, model, option, index):  # noqa: N802
        kinds = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick)
        if event.type() in kinds and self.mark(option.rect).contains(event.position().toPoint()):
            if event.type() == QEvent.Type.MouseButtonRelease:
                self.parts.explain(index.row(), self.parts.viewport().mapToGlobal(option.rect.bottomRight()))
            return True  # a click on the (?) neither selects, drags nor adds the part
        return super().editorEvent(event, model, option, index)


def part_pixmap(kind, ratio=1.0):
    """The card shown under the pointer while a part is dragged."""
    colors, radius = theme.colors(), float(min(theme.data()["radius"], 12))
    width, height = 170, 44
    pixmap = QPixmap(int(width * ratio), int(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    body = QPainterPath()
    body.addRoundedRect(QRectF(1, 1, width - 2, height - 2), radius, radius)
    painter.fillPath(body, QColor(colors["node"]))
    band = QPainterPath()
    band.addRect(QRectF(1, 1, width - 2, 18))
    painter.fillPath(band.intersected(body), QColor(colors["node_header"]))
    painter.setPen(QPen(QColor(colors["accent"]), 2.0))
    painter.drawPath(body)
    font = QFont()
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(colors.get("node_title", colors["text"])))
    painter.drawText(QRectF(8, 1, width - 16, 18), int(Qt.AlignmentFlag.AlignVCenter),
                     elide(tr(B.KINDS[kind].title), 22))
    painter.end()
    return pixmap


class PartsList(QListWidget):
    """The palette: parts to drag onto the board (or add with a double-click or Enter); a (?) per row explains
    one ("?" or the context menu do too)."""
    chosen = Signal(str)  # Enter on a part: add it

    def __init__(self):
        super().__init__()
        self.hovered = None  # (row, on the (?)) under the pointer
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # long titles are elided
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setItemDelegate(PartsDelegate(self))

    def kind_of(self, row):
        item = self.item(row)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def mimeTypes(self):  # noqa: N802
        return [MIME_PART]

    def mimeData(self, items):  # noqa: N802
        data = QMimeData()
        if items:
            data.setData(MIME_PART, str(items[0].data(Qt.ItemDataRole.UserRole)).encode())
        return data

    def startDrag(self, actions):  # noqa: N802
        item = self.currentItem()
        if item is None or not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            return
        drag = QDrag(self)
        drag.setMimeData(self.mimeData([item]))
        pixmap = part_pixmap(item.data(Qt.ItemDataRole.UserRole), self.devicePixelRatioF())
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(int(pixmap.width() / pixmap.devicePixelRatio() / 2), 10))
        drag.exec(Qt.DropAction.CopyAction)

    def explain(self, row, point=None):
        kind = self.kind_of(row)
        if kind is None:
            return None
        if point is None:
            point = self.viewport().mapToGlobal(self.visualItemRect(self.item(row)).bottomLeft())
        return help_view.show_tip(f"kind.{kind}", self, point)

    def mouseMoveEvent(self, event):  # noqa: N802
        index = self.indexAt(event.position().toPoint())
        hovered = (index.row(), self.itemDelegate().mark(self.visualRect(index)).contains(event.position().toPoint())) \
            if index.isValid() else None
        if hovered != self.hovered:
            self.hovered = hovered
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.hovered = None
        self.viewport().update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        item = self.currentItem()
        if item is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if item.flags() & Qt.ItemFlag.ItemIsEnabled:
                self.chosen.emit(item.data(Qt.ItemDataRole.UserRole))
            event.accept()
            return
        if item is not None and event.text() == "?":
            self.explain(self.row(item))
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        item = self.itemAt(event.pos())
        if item is None:
            return
        menu = QMenu(self)
        add = menu.addAction(tr("Add to the board"), lambda: self.chosen.emit(item.data(Qt.ItemDataRole.UserRole)))
        add.setEnabled(bool(item.flags() & Qt.ItemFlag.ItemIsEnabled))
        menu.addAction(tr("What is this?"), lambda: self.explain(self.row(item)))
        menu.exec(event.globalPos())


def elide(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def elide_middle(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit // 2 - 1] + "…" + text[-(limit // 2):]


def summary(node):
    """One line of what a node sets (shown under its title)."""
    props = node.props
    if node.kind == "agent":
        return " ".join(filter(None, [props.get("model"), "-D" if props.get("--dangerously-skip-permissions") else ""]))
    if node.kind == "workspace":
        return tr("read-only") if props.get("readonly") else tr("writable")
    if node.kind == "mount":
        return f"→ {props.get('dest', '')} ({props.get('mode', 'ro')})"
    if node.kind == "proxy":
        if props.get("enabled", True) is False:
            return tr("off")
        rules = len(props.get("--allow-http", []))
        return tr("on, {count} request rules", count=rules) if rules else tr("on, TLS inspected")
    if node.kind == "approvals":
        parts = [f"--ask {props['--ask']}"] if props.get("--ask") else []
        if isinstance(props.get("rules"), dict):
            rules = props["rules"]
            count = len(rules.get("http", []) or []) + len(rules.get("mcp", []) or [])
            parts.append(tr("{count} project rules", count=count))
        if props.get("--rules"):
            parts.append(tr("{count} ruleset files", count=len(props["--rules"])))
        return printable(", ".join(parts)) if parts else tr("ask (default)")
    if node.kind == "mcp":
        definition = props.get("definition")
        if B.PLACEHOLDER.search(str(props.get("name", ""))):
            return tr("to fill in")
        if not isinstance(definition, dict):
            return tr("registered server")
        reaches, hosts, vpn = B.server_network(definition)
        network = (", ".join(hosts) or tr("every destination")) if reaches else tr("no network")
        return printable(tr("VPN {vpn} · {network}", vpn=vpn, network=network) if vpn else network)
    if node.kind == "llm":
        return tr("its address and port only")
    if node.kind == "kits" and props.get("--kit"):
        return printable(", ".join(props["--kit"]))
    if node.kind == "network":
        if props.get("--offline"):
            return tr("offline")
        parts = list(props.get("--profile", [])) + [o[len("--allow-"):] for o in B.PRESET_OPTIONS if props.get(o)]
        parts += list(props.get("--allow-host", []))[:3]
        return ", ".join(parts) or tr("the agent's provider only")
    shown = [f"{option} {value if not isinstance(value, (list, bool)) else ''}".strip()
             for option, value in props.items() if option.startswith("--") and value not in (None, "", [], False)]
    return printable(", ".join(shown)) if shown else tr("defaults")


class PathEdit(QWidget):
    """A line edit with a folder (or file) chooser."""
    changed = Signal(str)

    def __init__(self, value="", directory=True, host=LOCAL):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(str(value or ""))
        self.edit.editingFinished.connect(lambda: self.changed.emit(self.edit.text()))
        browse = self.browse_button = QToolButton()
        browse.setText("…")
        browse.setAccessibleName(tr("Choose…"))
        browse.clicked.connect(lambda: self.browse(directory))
        layout.addWidget(self.edit, 1)
        layout.addWidget(browse)
        if host != LOCAL:  # a path of that host: typed in, never chosen from this computer's folders
            browse.setEnabled(False)
            self.edit.setToolTip(safe_tooltip(tr("A path on {host}: type it in (this computer's folders are not {host}'s)",
                                    host=printable(host))))

    def browse(self, directory):
        path = (choose_folder(self, tr("Choose a folder"), "folder") if directory
                else choose_file(self, tr("Choose a file"), "file"))
        if path:
            self.edit.setText(path)
            self.changed.emit(path)


class LinesEdit(QPlainTextEdit):
    """One value per line, committed shortly after typing stops (one undo step per pause)."""
    changed = Signal(list)

    def __init__(self, values):
        super().__init__("\n".join(map(str, values or [])))
        self.setMaximumHeight(80)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.changed.emit(self.values()))
        self.textChanged.connect(lambda: self.timer.start(600))

    def values(self):
        return [line.strip() for line in self.toPlainText().splitlines() if line.strip()]


class RulesEdit(QWidget):
    """--allow-http rules as a table: host (or *.domain), methods, path."""
    changed = Signal(list)

    def __init__(self, rules):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("Host"), tr("Methods"), tr("Path")])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMaximumHeight(150)
        for rule in rules or []:
            parts = str(rule).split(None, 2)
            self.add_row(*(parts + ["", "", ""])[:3])
        self.table.itemChanged.connect(lambda _: self.changed.emit(self.rules()))
        row = QHBoxLayout()
        add = QPushButton(tr("Add rule"))
        add.clicked.connect(lambda: self.add_row("api.example.org", "GET,HEAD", "/**", emit=True))
        remove = QPushButton(tr("Remove rule"))
        remove.clicked.connect(self.remove_row)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout.addWidget(self.table)
        layout.addLayout(row)

    def add_row(self, host, methods, path, emit=False):
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate((host, methods, path)):
            self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.blockSignals(False)
        if emit:
            self.changed.emit(self.rules())

    def remove_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        if rows:
            self.changed.emit(self.rules())

    def rules(self):
        found = []
        for row in range(self.table.rowCount()):
            cells = [(self.table.item(row, c).text().strip() if self.table.item(row, c) else "") for c in range(3)]
            if any(cells):
                found.append(" ".join(cells))
        return found


class HostCombo(QComboBox):
    """Where a start runs: this machine, or a host of `sandbox-ai hosts` (over SSH). The list is read from the hosts
    file (no SSH) and again by reload() (a page shown: a host may have been added meanwhile); hidden while there is
    no other host. The host chosen stays while it is in the list; one that left it falls back to this machine."""
    chosen = Signal(str)

    def __init__(self, sai, parent=None):
        super().__init__(parent)
        self.sai = sai
        self.setAccessibleName(tr("Host"))
        self.setToolTip(tr("Where it runs: this machine, or a host of sandbox-ai hosts (over SSH)"))
        self.currentIndexChanged.connect(lambda _index: self.chosen.emit(self.current()))
        self.reload()
        from .host_choice import follow_hosts
        follow_hosts(self, self.reload)  # a host added, renamed or removed: here at once

    def reload(self):
        current = self.current() if self.count() else LOCAL
        try:
            others = [e for e in self.sai.host_entries() if e.get("kind") != LOCAL and e.get("name") != LOCAL] \
                if self.sai.hosts_supported() else []
        except Exception:  # noqa: BLE001 - a damaged hosts file: this machine still counts
            others = []
        self.blockSignals(True)
        self.clear()
        self.addItem(tr("This machine"), LOCAL)
        for entry in others:
            if isinstance(entry.get("name"), str):
                self.addItem(printable(f"{entry['name']}  {entry.get('ssh', '')}".strip()), entry["name"])
        index = self.findData(current)
        self.setCurrentIndex(max(0, index))
        self.blockSignals(False)
        self.setVisible(self.count() > 1)
        if index < 0 and current != LOCAL:
            self.chosen.emit(LOCAL)  # the host chosen left the list: this machine instead

    def current(self):
        return self.currentData() or LOCAL

    def choose(self, host):
        """Choose HOST if it is in the list; False otherwise (the choice stays)."""
        index = self.findData(host or LOCAL)
        if index < 0:
            return False
        self.setCurrentIndex(index)
        return True


class BoardPage(QWidget):
    """The board of one sandbox, its checks, and Apply / Dry run / Start."""
    opened = Signal(str)  # a project folder was opened

    def __init__(self, sai, terminal, settings=None):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.settings = settings or (lambda: {})
        self.board, self.directory, self.items, self.edges = B.Board(), None, {}, []
        self.check_token, self.restoring, self.inspected, self.launcher_ok = 0, False, None, None
        self.problems = []
        self.untrusted = {}  # {project key: path} of boards loaded from a project file nobody trusted yet
        # {project key: {"digest", "trusted"}}: the project file each board was read from (digest None: none read), kept
        # with a draft; a draft from another or an untrusted version is asked about again (restore_saved)
        self.sources = {}
        self.reasked = None  # the notice of a draft asked about again (restore_saved), shown once the project is open
        self.ghost = None  # the drop indicator while something is dragged over the board
        self.project_request = None  # the window: open a folder dropped on the board as the project (in its tab)
        self.kit_inventory, self.kit_loading = None, False  # sandbox-ai kits list --json, once read
        self.llm_servers, self.llm_loading = None, False  # sandbox-ai llm list --json, for the agent's Model server
        self.state, self.saved_boards, self.restored = None, {}, set()  # drafts and zoom kept between starts
        self.forbidden = self.read_forbidden()
        self.policy_data = self.read_policy()
        # The inspector is rebuilt after the signal that changed the model has returned: its widgets may be
        # the ones that sent it.
        self.inspect_timer = QTimer(self)
        self.inspect_timer.setSingleShot(True)
        self.inspect_timer.setInterval(0)
        self.inspect_timer.timeout.connect(lambda: self.inspect(self.board.node(self.inspected)
                                                                if self.inspected in self.items else None))
        layout = QVBoxLayout(self)
        self.folder = plain_label(tr("No project folder"), word_wrap=False)
        open_button = QPushButton(tr("Open project folder…"))
        # In the window, "Open project folder…" opens the folder in a tab of its own (open_request).
        self.open_request = None
        open_button.clicked.connect(lambda: self.open_request() if self.open_request else self.open_project())
        new_button = self.gallery_button = QPushButton(tr("New from preset…"))
        new_button.setToolTip(tr("Ready boards, simple and complex (MCP servers, VPNs, LLM servers, rules), and "
                                 "your own"))
        new_button.clicked.connect(lambda: self.open_gallery())
        wizard_button = QPushButton(tr("Wizard…"))
        wizard_button.setToolTip(tr("A new project step by step; it ends on this board"))
        wizard_button.clicked.connect(lambda: self.open_wizard())
        # One undo stack per project: the board of each project (its tab) keeps its changes, undo and zoom
        # while another project is shown (sessions, keyed by folder).
        self.undo_group = QUndoGroup(self)
        self.undo = QUndoStack(self)
        self.undo_group.addStack(self.undo)
        self.undo_group.setActiveStack(self.undo)
        self.sessions = {}
        undo = QToolButton()
        undo.setText(tr("Undo"))
        undo.clicked.connect(self.undo_group.undo)
        self.undo_group.canUndoChanged.connect(undo.setEnabled)
        undo.setEnabled(False)
        redo = QToolButton()
        redo.setText(tr("Redo"))
        redo.clicked.connect(self.undo_group.redo)
        self.undo_group.canRedoChanged.connect(redo.setEnabled)
        redo.setEnabled(False)
        arrange = self.arrange_button = QToolButton()
        arrange.setText(tr("Arrange"))
        arrange.setToolTip(tr("Lay the nodes out in columns: files, agent, proxy, network"))
        arrange.clicked.connect(self.arrange)
        zoom_in, zoom_out, fit = QToolButton(), QToolButton(), QToolButton()
        zoom_in.setText("+")
        zoom_out.setText("−")
        zoom_in.setAccessibleName(tr("Zoom in"))
        zoom_out.setAccessibleName(tr("Zoom out"))
        fit.setText(tr("Fit"))
        # A row that wraps when the window is narrow (the folder's name is shortened, in full in its tooltip).
        layout.addWidget(flow(QLabel(tr("Project")), self.folder, open_button, new_button, wizard_button,
                              undo, redo, arrange, zoom_out, zoom_in, fit))
        self.notice = plain_label(role="secondary")
        self.notice.hide()
        self.discard_button = QPushButton(tr("Discard"))
        self.discard_button.setToolTip(tr("Drop the restored changes: the board as the project's file says"))
        self.discard_button.clicked.connect(lambda _=False: self.discard_draft())
        self.discard_button.hide()
        notice_row = QHBoxLayout()
        notice_row.addWidget(self.notice, 1)
        notice_row.addWidget(self.discard_button)
        layout.addLayout(notice_row)
        split = QSplitter(Qt.Orientation.Horizontal)
        # The parts: dragged onto the board (where they are dropped), or added with a double-click or Enter.
        self.palette = PartsList()
        self.palette.setToolTip(tr("Drag a part onto the board, or double-click it"))
        for kind in B.KINDS.values():
            if not kind.required:
                item = QListWidgetItem(tr(kind.title))
                item.setData(Qt.ItemDataRole.UserRole, kind.key)
                item.setToolTip(tr(kind.help))
                self.palette.addItem(item)
        self.palette.itemDoubleClicked.connect(lambda item: self.add_node(item.data(Qt.ItemDataRole.UserRole),
                                                                          guided=True))
        self.palette.chosen.connect(lambda kind: self.add_node(kind, guided=True))
        self.parts_panel = QWidget()  # the palette with a line on how to use it (a dock of its own in the window)
        self.parts_panel.setMaximumWidth(230)
        parts_layout = QVBoxLayout(self.parts_panel)
        parts_layout.setContentsMargins(0, 0, 0, 0)
        parts_hint = QHBoxLayout()
        self.parts_hint = plain_label(tr("Drag a part onto the board, or double-click it."), role="secondary")
        parts_hint.addWidget(self.parts_hint, 1)
        parts_hint.addWidget(help_view.HelpButton("board.palette"), 0, Qt.AlignmentFlag.AlignTop)
        parts_layout.addLayout(parts_hint)
        parts_layout.addWidget(self.palette, 1)
        self.scene = BoardScene(self)
        self.scene.selectionChanged.connect(self.selection_changed)
        self.view = BoardView(self.scene, self)
        zoom_in.clicked.connect(lambda: self.view.zoom(1.2))
        zoom_out.clicked.connect(lambda: self.view.zoom(1 / 1.2))
        fit.clicked.connect(self.fit)
        right = QWidget()
        self.side = right  # the inspector, the score and the findings (a dock of its own in the window)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.inspector_title = plain_label(role="section")
        self.inspector_help = help_view.HelpButton("board.inspector")  # the selected part's kind, else the inspector
        title_row = QHBoxLayout()
        title_row.addWidget(self.inspector_title, 1)
        title_row.addWidget(self.inspector_help, 0, Qt.AlignmentFlag.AlignVCenter)
        self.inspector_holder = QWidget()
        self.inspector = QFormLayout(self.inspector_holder)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.inspector_holder)
        self.score = plain_label(role="section")
        self.findings = QListWidget()
        self.findings.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.findings.itemDoubleClicked.connect(self.finding_clicked)
        right_layout.addLayout(title_row)
        right_layout.addWidget(scroll, 3)
        right_layout.addWidget(self.score)
        right_layout.addWidget(self.findings, 2)
        split.addWidget(self.parts_panel)
        split.addWidget(self.view)
        split.addWidget(right)
        split.setSizes([170, 700, 380])
        layout.addWidget(split, 1)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(MONO)
        self.preview.setMaximumHeight(64)
        self.preview.setAccessibleName(tr("Command line"))
        layout.addWidget(self.preview)
        # One line under the board (the page does not scroll): a long refusal is cut there, whole in the tooltip.
        self.verdict = ElidedLabel(role="secondary")
        layout.addWidget(self.verdict)
        self.apply_button = QPushButton(tr("Apply…"))
        self.apply_button.setToolTip(tr("Write .sandbox-ai.json into the first workspace and trust it, after showing "
                                        "it and what changes"))
        self.apply_button.clicked.connect(lambda: self.apply())
        self.dry_button = QPushButton(tr("Dry run"))
        self.dry_button.clicked.connect(lambda: self.dry_run())
        start_app = QPushButton(tr("Start in the app"))
        start_app.setProperty("role", "primary")  # the main action of the page
        start_app.clicked.connect(lambda: self.start(embedded=True))
        start_window = QPushButton(tr("Start in a terminal window"))
        start_window.clicked.connect(lambda: self.start(embedded=False))
        self.session = QLineEdit()
        self.session.setPlaceholderText(tr("session name"))
        self.session.setAccessibleName(tr("Session name"))
        self.session.setMaximumWidth(160)
        start_session = QPushButton(tr("Start as session"))
        start_session.clicked.connect(lambda: self.start(session=self.session.text().strip()))
        # Where it starts: this machine or a host (its own launcher checks and starts it; paths are that host's).
        self.host_choice = HostCombo(sai)
        self.host_choice.chosen.connect(lambda _host: self.host_changed())
        self.host_known = {}  # {(host, agent): the option strings of that host's launcher, or None: not known}
        # {(host, agent): time.monotonic() of a read that failed}: asked again after HOST_RETRY s, or at once when the
        # host is chosen again or a start or dry run needs them (never "not known" for good after one failure).
        self.host_failed = {}
        self.host_reading = set()  # (host, agent) whose options are being read now
        # The launcher's verdict on another host is a dry run over SSH: one at a time, the last edit's after it.
        self.check_running, self.check_pending = False, False
        self.host_note = plain_label(role="secondary")
        self.host_note.hide()
        layout.addWidget(self.host_note)
        layout.addWidget(flow(self.host_choice, self.apply_button, self.dry_button, start_app, start_window,
                              self.session, start_session))
        self.check_timer = QTimer(self)
        self.check_timer.setSingleShot(True)
        self.check_timer.timeout.connect(self.check)
        self.set_board(B.with_backend(B.from_argv(sai, self.default_agent(), []),
                                      self.settings().get("default_backend")), None)
        help_view.on_shown_changed(self.show_help_marks)

    def show_help_marks(self, shown):
        """The (?) in each node's title band follows the setting help_buttons."""
        for item in self.items.values():
            item.help_mark.setVisible(shown)

    # -- settings and policy --

    def default_agent(self):
        agent = self.settings().get("default_agent")
        return agent if agent in B.AGENTS else "claude"

    def grid(self):
        value = self.settings().get("board_grid", 20)
        return int(value) if isinstance(value, int) and 0 <= value <= 200 else 20

    def read_forbidden(self):
        try:
            return self.sai.forbidden()
        except Exception:  # noqa: BLE001 - shown on the System page
            return {}

    def read_policy(self):
        try:
            return self.sai.policy()[0]
        except Exception:  # noqa: BLE001
            return {}

    def policy(self):
        return self.policy_data

    # -- the model --

    def show_folder(self):
        self.folder.setText(printable(elide_middle(self.directory, 48)) if self.directory else tr("No project folder"))
        self.folder.setToolTip(safe_tooltip(self.directory) if self.directory else "")

    def set_board(self, board, directory):
        self.board, self.directory = board, (str(directory) if directory else None)
        self.show_folder()
        self.undo.clear()
        self.show_notice("")
        self.discard_button.hide()
        self.rebuild()

    def dirty(self):
        """True while the board has changes that are not applied (written to .sandbox-ai.json)."""
        return not self.undo.isClean()

    # -- one board per project --

    @staticmethod
    def key_of(directory):
        return os.path.realpath(os.path.expanduser(str(directory))) if directory else ""

    def stash(self):
        """Keep the board shown now (model, undo stack, zoom, selection) for its project."""
        center = self.view.mapToScene(self.view.viewport().rect().center())
        self.sessions[self.key_of(self.directory)] = {
            "board": self.board, "stack": self.undo, "directory": self.directory, "transform": self.view.transform(),
            "center": center, "inspected": self.inspected, "notice": (self.notice.text(), self.notice.property("role"))}

    def new_stack(self):
        self.undo = QUndoStack(self)
        self.undo_group.addStack(self.undo)
        self.undo_group.setActiveStack(self.undo)

    def activate(self, directory):
        """Show the board of the project DIRECTORY: as it was left (unapplied changes, undo, zoom) when it was
        shown before, else read from its .sandbox-ai.json."""
        key = self.key_of(directory)
        if key == self.key_of(self.directory):
            return
        session = self.sessions.get(key)
        if session is None:
            self.open_project(directory, quiet=True)  # load() keeps the board shown until now
            return
        self.stash()
        self.board, self.undo, self.directory = session["board"], session["stack"], session["directory"]
        self.undo_group.setActiveStack(self.undo)
        self.inspected = session["inspected"]
        self.show_folder()
        self.rebuild()
        self.view.setTransform(session["transform"])
        self.view.centerOn(session["center"])
        text, role = session["notice"]
        self.show_notice(text, role or "secondary")

    def forget(self, directory):
        """The project's tab was closed: its kept board goes (the one shown stays)."""
        key = self.key_of(directory)
        if key == self.key_of(self.directory):
            return
        session = self.sessions.pop(key, None)
        if session is not None and session["stack"] is not self.undo:
            self.undo_group.removeStack(session["stack"])
            session["stack"].deleteLater()

    def dirty_for(self, directory):
        """True while the board of the project DIRECTORY (shown or kept) has unapplied changes."""
        key = self.key_of(directory)
        if key == self.key_of(self.directory):
            return self.dirty()
        session = self.sessions.get(key)
        return bool(session) and not session["stack"].isClean()

    def may_discard(self):
        """True if the board may be replaced in place (by a template, the wizard, another folder opened on a
        board of its own): nothing is unapplied, or the human agrees to drop it."""
        if not self.dirty():
            return True
        where = printable(self.directory) if self.directory else tr("the new board")
        return ask_first(self, "board-discard", tr("Changes not applied"),
                         tr("The board of {path} has changes that are not applied. Drop them?", path=where),
                         tr("Drop the changes"), reject=tr("Keep them"))

    def show_notice(self, text, role="secondary"):
        draft = getattr(self, "draft_notice", "")
        if text and draft and text != draft and not self.discard_button.isHidden():
            text, role = f"{draft}\n{text}", "notice-warn"  # the restored changes stay announced
        self.notice.setText(text)
        self.notice.setProperty("role", role)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)
        self.notice.setVisible(bool(text))

    def restore(self, state):
        self.restoring = True
        try:
            self.board = B.Board.from_state(state)
            self.rebuild()
        finally:
            self.restoring = False

    def change(self, text, function):
        """Apply FUNCTION to the model as one undo step."""
        before = self.board.state()
        try:
            function()
        except ValueError as exc:
            warn(self, tr("Board"), printable(exc))
            return
        after = self.board.state()
        if after != before:
            self.undo.push(Snapshot(self, text, before, after))

    def changeEvent(self, event):  # noqa: N802
        """A new theme: nodes, edges and the canvas are drawn again in its colours - once, however many
        change events a theme sends, and for a hidden board only when it is shown again."""
        if event.type() == QEvent.Type.PaletteChange and getattr(self, "items", None) is not None:
            self.restyle_pending = True
            if self.isVisible() and not getattr(self, "restyle_queued", False):
                self.restyle_queued = True
                QTimer.singleShot(0, self.restyle)
        super().changeEvent(event)

    def restyle(self):
        self.restyle_queued = False
        if getattr(self, "restyle_pending", False):
            self.restyle_pending = False
            self.rebuild()
            self.view.viewport().update()

    def rebuild(self):
        selected = self.inspected
        self.scene.blockSignals(True)
        self.scene.clear()
        self.scene.blockSignals(False)
        self.items, self.edges = {}, []
        for node in self.board.nodes:
            item = NodeItem(self, node)
            self.scene.addItem(item)
            self.items[node.id] = item
        for source, target in sorted(self.board.edges):
            if source in self.items and target in self.items:
                a, b = self.board.node(source), self.board.node(target)
                edge = EdgeItem(self.items[source], self.items[target], {a.kind, b.kind} & B.CONNECTED_ONLY != set())
                edge.ids = (source, target)
                self.scene.addItem(edge)
                self.edges.append(edge)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-200, -200, 400, 400))
        if selected in self.items:
            self.scene.blockSignals(True)
            self.items[selected].setSelected(True)
            self.scene.blockSignals(False)
        self.inspected = selected if selected in self.items else None
        self.refresh()
        self.inspect_timer.start()

    def node_moved(self, item):
        node = self.board.node(item.node_id)
        if node is not None:
            node.x, node.y = item.pos().x(), item.pos().y()
        for edge in self.edges:
            if item in (edge.source, edge.target):
                edge.update_path()

    def moved(self, before):
        """After a drag: snap to the grid and keep it as one undo step."""
        grid = self.grid()
        for node in self.board.nodes:
            node.x, node.y = B.snap(node.x, grid), B.snap(node.y, grid)
        after = self.board.state()
        if [(n["x"], n["y"]) for n in after["nodes"]] != [(n["x"], n["y"]) for n in before["nodes"]]:
            self.undo.push(Snapshot(self, tr("Move"), before, after))

    def add_node(self, kind, guided=False, at=None, **props):
        """Add a node of KIND (centred on the scene point AT, else in its column); GUIDED (from the parts list):
        then help set it up, unless the setting says no."""
        if kind == "workspace" and not props:
            props = {"path": "", "readonly": False}
        if kind == "mount" and not props:
            props = {"source": "", "dest": "/mnt/data", "mode": "ro"}
        if kind == "mcp" and not props:
            names = self.mcp_names()
            props = {"name": names[0] if names else ""}

        added = []

        def add():
            node = self.board.add(kind, **props)
            if at is not None:
                node.x, node.y = B.snap(at.x() - NODE_W / 2, self.grid()), B.snap(at.y() - NODE_H / 2, self.grid())
            else:
                node.x, node.y = B.snap(40.0 + B.KINDS[kind].column * 260.0, self.grid()), \
                    B.snap(40.0 + 110.0 * len(self.board.nodes) % 700, self.grid())
            self.inspected = node.id
            added.append(node.id)
        self.change(tr("Add {name}", name=tr(B.KINDS[kind].title)), add)
        # Only the node just added (none when the board refused it: a second agent, say).
        if guided and added and self.settings().get("guide_nodes", True) and self.board.node(added[0]):
            self.guide_node(added[0])

    def guide_node(self, node_id):
        """The guide's dialog for node NODE_ID: its questions one by one, then what is still wrong with it."""
        from . import guide, guide_view
        node = self.board.node(node_id)
        if node is None or not guide.questions(node.kind):
            return
        dialog = guide_view.NodeGuide(node, self.problems, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            guide_view.apply(self, node_id, dialog.answers())

    def connect_nodes(self, source, target):
        if self.board.can_connect(source, target):
            self.change(tr("Connect"), lambda: self.board.connect(source, target))

    # -- drag and drop: parts from the palette, folders and files from a file manager --

    def refresh_palette(self):
        """A part a board has once that is on the board already is greyed out: it cannot be added again."""
        movable = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        for row in range(self.palette.count()):
            item = self.palette.item(row)
            kind = item.data(Qt.ItemDataRole.UserRole)
            taken = B.KINDS[kind].single and self.board.first(kind) is not None
            item.setFlags(item.flags() & ~movable if taken else item.flags() | movable)
            item.setToolTip(safe_tooltip(tr(B.KINDS[kind].help) + ("\n" + tr("On the board already: a board has one.")
                                                       if taken else "")))

    @staticmethod
    def drop_what(mime):
        """What a drag carries that the board takes: ("part", kind) or ("paths", [local paths]); else None."""
        if mime.hasFormat(MIME_PART):
            kind = bytes(mime.data(MIME_PART)).decode("utf-8", "replace")
            return ("part", kind) if kind in B.KINDS and not B.KINDS[kind].required else None
        if mime.hasUrls():
            paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            return ("paths", paths) if paths and len(paths) <= 32 else None
        return None

    def show_ghost(self, what, point):
        """The drop indicator for WHAT at the scene POINT."""
        self.hide_ghost()
        kind, value = what
        if kind == "part":
            name = tr(B.KINDS[value].title)
            taken = B.KINDS[value].single and self.board.first(value) is not None
            self.ghost = DropGhost(tr("{name} is on the board already", name=name) if taken
                                   else tr("Drop to add {name}", name=name), refused=taken)
        else:
            names = ", ".join(os.path.basename(p.rstrip("/")) or p for p in value[:3]) \
                + ("…" if len(value) > 3 else "")
            self.ghost = DropGhost(tr("Drop: {names}", names=printable(names)))
        self.scene.addItem(self.ghost)
        self.ghost.move_to(point, self.grid())

    def hide_ghost(self):
        ghost, self.ghost = self.ghost, None
        try:
            if ghost is not None and ghost.scene() is self.scene:
                self.scene.removeItem(ghost)
        except RuntimeError:  # gone with the scene's items (the board was rebuilt meanwhile)
            pass

    def drop(self, what, point, global_point=None):
        """WHAT dropped at the scene POINT: a part is added there (one step of Undo, then the guide); files and
        folders as drop_paths() says. Returns the node added or what was done (None: nothing)."""
        kind, value = what
        if kind == "part":
            if value not in B.KINDS or (B.KINDS[value].single and self.board.first(value) is not None):
                return None
            before = len(self.board.nodes)
            self.add_node(value, guided=True, at=point)
            return self.board.nodes[-1] if len(self.board.nodes) > before else None
        return self.drop_paths(value, point, global_point)

    def node_at(self, point):
        for item in self.scene.items(point):
            node = item.parentItem() if isinstance(item, (PortItem, NodeHelpMark)) else item
            if isinstance(node, NodeItem):
                return self.board.node(node.node_id)
        return None

    def drop_choices(self, paths, node=None):
        """What a drop of PATHS (onto NODE, or the free canvas) can do: [(key, text)], the likeliest first."""
        if len(paths) == 1 and os.path.isfile(paths[0]):
            path = paths[0]
            if os.path.basename(path) == ".sandbox-ai.json":
                return [("project", tr("Open the project {path}", path=printable(os.path.dirname(path))))]
            kind = preset_kind(path) if path.lower().endswith(".json") else None
            if kind == "app":
                return [("preset-board", tr("Import as my preset and build the board from it")),
                        ("preset", tr("Import as my preset"))]
            if kind == "sandbox-ai":
                return [("sandbox-ai-preset", "")]
        folders = all(os.path.isdir(p) for p in paths)
        choices = []
        if node is not None and len(paths) == 1 and (node.kind == "mount" or (node.kind == "workspace" and folders)):
            choices.append(("use", tr("Use it for {name}", name=node.title())))
        if len(paths) == 1 and folders and os.path.isfile(os.path.join(paths[0], ".sandbox-ai.json")):
            choices.append(("project", tr("Open the project {path}", path=printable(paths[0]))))
        if folders:
            choices.append(("workspace", tr("Add as workspace")))
        return choices + [("mount-ro", tr("Add as read-only mount")), ("mount-rw", tr("Add as writable mount"))]

    def choose_drop(self, choices, global_point=None):
        """The human's choice of CHOICES, in a menu at the drop (None: none)."""
        menu = QMenu(self)
        for key, text in choices:
            menu.addAction(text).setData(key)
        chosen = menu.exec(global_point or self.view.viewport().mapToGlobal(self.view.viewport().rect().center()))
        return chosen.data() if chosen is not None else None

    def mount_dest(self, path):
        """/mnt/NAME for a dropped PATH, not yet used by a mount of the board."""
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(path.rstrip("/"))).strip("-.") or "data"
        taken = {n.props.get("dest") for n in self.board.of_kind("mount")}
        dest, number = f"/mnt/{name}", 2
        while dest in taken:
            dest, number = f"/mnt/{name}-{number}", number + 1
        return dest

    def drop_paths(self, paths, point, global_point=None):
        """Files or folders dropped at the scene POINT: a folder becomes a workspace or a mount, a file a mount, a
        .sandbox-ai.json (or a folder with one) opens its project, a preset file of this app is imported. The
        board's own changes are one step of Undo. Returns what was done (a key of drop_choices) or None."""
        paths = [os.path.abspath(os.path.expanduser(p)) for p in paths if p and os.path.exists(p)]
        if not paths:
            return None
        node = self.node_at(point)
        choices = self.drop_choices(paths, node)
        if choices[0][0] == "sandbox-ai-preset":
            inform(self, tr("Board"), tr("{path} is a preset of sandbox-ai itself: keep it in {folder} and it is "
                                         "listed under sandbox-ai's presets in the gallery.",
                                         path=printable(paths[0]), folder="~/.config/sandbox-ai/presets/"))
            return None
        key = choices[0][0] if len(choices) == 1 else self.choose_drop(choices, global_point)
        if key is None:
            return None
        if key == "project":
            directory = os.path.dirname(paths[0]) if os.path.isfile(paths[0]) else paths[0]
            (self.project_request or self.open_project)(directory)
        elif key in ("preset", "preset-board"):
            from . import presets
            try:
                preset = presets.import_file(self.sai, paths[0])
                presets.add_mine(self.sai, preset)
            except (OSError, ValueError) as exc:
                warn(self, tr("Not imported"), printable(exc))
                return None
            if key == "preset-board":
                self.new_from_preset(preset)
            else:
                inform(self, tr("Board"), tr("Imported as your preset {name}: it is under My presets in the gallery.",
                                             name=printable(preset.title)))
        elif key == "use":
            self.set_prop(node.id, "path" if node.kind == "workspace" else "source", paths[0])
        else:
            self.add_paths(key, paths, point)
        return key

    def add_paths(self, key, paths, point):
        """PATHS as workspaces (KEY "workspace") or mounts ("mount-ro", "mount-rw"), from POINT downwards."""
        kind = "workspace" if key == "workspace" else "mount"

        def add():
            readonly = any(n.props.get("readonly") for n in self.board.of_kind("workspace"))
            for index, path in enumerate(paths):
                if kind == "workspace":
                    node = self.board.add("workspace", path=path, readonly=readonly)
                else:
                    node = self.board.add("mount", source=path, dest=self.mount_dest(path),
                                          mode="ro" if key == "mount-ro" else "rw")
                node.x = B.snap(point.x() - NODE_W / 2, self.grid())
                node.y = B.snap(point.y() - NODE_H / 2 + index * (NODE_H + 24), self.grid())
                self.inspected = node.id
        self.change(tr("Add {name}", name=tr(B.KINDS[kind].title)), add)

    def delete_selected(self):
        nodes = [i.node_id for i in self.scene.selectedItems() if isinstance(i, NodeItem)
                 and not B.KINDS[self.board.node(i.node_id).kind].required]
        edges = [e.ids for e in self.scene.selectedItems() if isinstance(e, EdgeItem)]
        if not nodes and not edges:
            return

        def delete():
            for source, target in edges:
                self.board.disconnect(source, target)
            for node_id in nodes:
                self.board.remove(node_id)
        self.change(tr("Delete"), delete)

    def arrange(self):
        self.change(tr("Arrange"), lambda: self.board.layout(self.grid()))
        self.fit()

    def showEvent(self, event):  # noqa: N802
        """Shown the first time: the whole board in view (it is laid out before the view has a size). The hosts are
        read again (one added on the Hosts page meanwhile)."""
        self.host_choice.reload()
        super().showEvent(event)
        if getattr(self, "restyle_pending", False):
            self.restyle()
        if not getattr(self, "fitted", False):
            self.fitted = True
            QTimer.singleShot(0, self.fit)

    def fit(self):
        rect = self.scene.itemsBoundingRect()
        if not rect.isEmpty():
            self.view.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)

    def set_prop(self, node_id, key, value):
        node = self.board.node(node_id)
        if node is None or node.props.get(key) == value:
            return
        label = key if key.startswith("--") else tr("Property")

        def update():
            if value in (None, "", [], False) and key.startswith("--"):
                node.props.pop(key, None)
            else:
                node.props[key] = value
        self.change(tr("Change {name}", name=label), update)

    # -- checks --

    def refresh(self):
        problems = B.problems(self.sai, self.board, self.policy())
        score, badges = B.badges(self.board, self.policy())
        levels = {}
        for problem in problems:
            if problem.node is not None and levels.get(problem.node) != "error":
                levels[problem.node] = problem.level
        for node in self.board.nodes:
            if node.id in self.items:
                self.items[node.id].update_from(node, levels.get(node.id), self.board.connected(node))
        self.findings.clear()
        for problem in problems:
            item = QListWidgetItem(("✘ " if problem.level == "error" else "! ") + problem.text)
            item.setForeground(QBrush(QColor(theme.color("bad" if problem.level == "error" else "warn"))))
            item.setData(Qt.ItemDataRole.UserRole, problem.node)
            self.findings.addItem(item)
        for badge in badges:
            item = QListWidgetItem({"bad": "▼ ", "warn": "▽ ", "ok": "✔ ", "info": "· "}[badge.level] + badge.text)
            if badge.level in ("bad", "warn", "ok"):
                item.setForeground(QBrush(QColor(theme.color(badge.level))))
            item.setData(Qt.ItemDataRole.UserRole, badge.node)
            self.findings.addItem(item)
        self.score.setText(tr("Security score: {score} of 100", score=score))
        self.refresh_palette()
        self.preview.setPlainText(B.command_line(self.sai, self.board))
        self.problems = problems
        self.verdict.setText(tr("The launcher checks the options…"))
        self.check_timer.start(self.check_delay())

    def check(self):
        """The launcher's own verdict: a dry run of the board (no side effects), in the background. On another host
        (a dry run over SSH, seconds each) one runs at a time: an edit meanwhile asks for one more after it, with the
        board as it is then; an answer for an older board or another host is dropped (the token)."""
        agent, _, _ = B.values(self.board)
        host = self.host()
        if host != LOCAL:
            self.read_host_options(host, agent)
            if self.check_running:
                self.check_pending = True
                return
        self.check_token += 1
        token = self.check_token
        self.check_running, self.check_pending = host != LOCAL, False
        background(self.sai.host_dry_run, host, agent, B.launcher_argv(self.sai, self.board, local=host == LOCAL),
                   timeout=120,
                   done=lambda result, error: self.checked(token, result, error, host))

    def check_delay(self):
        """Milliseconds after an edit before the launcher's verdict is asked for: longer for another host (SSH)."""
        delay = int(self.settings().get("board_check_delay", 700))
        return delay if self.host() == LOCAL else max(delay, REMOTE_CHECK_DELAY)

    def read_host_options(self, host, agent, now=False):
        """The options the launcher of HOST takes for AGENT, read in the background unless known or being read; a
        failed read is tried again after HOST_RETRY seconds (NOW: at once)."""
        key = (host, agent)
        if host == LOCAL or key in self.host_reading or self.host_known.get(key) is not None:
            return
        failed = self.host_failed.get(key)
        if failed is not None and not now and time.monotonic() - failed < HOST_RETRY:
            return
        self.host_known.setdefault(key, None)
        self.host_reading.add(key)
        background(self.sai.host_launch_options, host, agent, timeout=90,
                   done=lambda found, error: self.host_options_read(host, agent, found, error))

    def checked(self, token, result, error, host=None):
        if host is not None and host != LOCAL:
            self.check_running = False
            if self.check_pending:  # edited meanwhile: the verdict of the board as it is now
                self.check_pending = False
                self.check_timer.start(0)
                return
        if token != self.check_token or (host is not None and host != self.host()):
            return
        plan, message = result if error is None else (None, str(error))
        if plan is not None:
            self.verdict.setText(tr("The launcher accepts these options."))
            self.verdict.setProperty("role", "notice-ok")
        else:
            said = " ".join(printable((message or "").strip()[-400:]).split())
            self.verdict.setText(tr("The launcher refuses: {message}", message=said))
            self.verdict.setProperty("role", "notice-error")
        self.verdict.style().unpolish(self.verdict)
        self.verdict.style().polish(self.verdict)
        self.launcher_ok = plan is not None

    def finding_clicked(self, item):
        node_id = item.data(Qt.ItemDataRole.UserRole)
        if node_id in self.items:
            self.scene.clearSelection()
            self.items[node_id].setSelected(True)
            self.view.centerOn(self.items[node_id])

    # -- the inspector --

    def selection_changed(self):
        if self.restoring:
            return
        try:
            selected = self.scene.selectedItems()
        except RuntimeError:  # the scene is being destroyed: the app quits
            return
        nodes = [i for i in selected if isinstance(i, NodeItem)]
        self.inspected = nodes[0].node_id if len(nodes) == 1 else None
        self.inspect_timer.start()

    def clear_inspector(self):
        while self.inspector.rowCount():
            self.inspector.removeRow(0)

    def inspect(self, node):
        self.clear_inspector()
        if node is None:
            self.inspector_title.setText(tr("Select a part to change it"))
            self.inspector_help.set_tip("board.inspector")
            return
        self.inspector_title.setText(node.title())
        self.inspector_help.set_tip(f"kind.{node.kind}")
        node_id = node.id
        kind = node.kind
        from . import guide
        if guide.questions(kind):
            helper = QPushButton(tr("Set up step by step…"))
            helper.setToolTip(tr("The guide asks what matters for this part, one question at a time"))
            helper.clicked.connect(lambda _=False: self.guide_node(node_id))
            self.inspector.addRow(helper)
        if kind == "agent":
            agent = QComboBox()
            agent.addItems(list(self.sai.agents))
            agent.setCurrentText(node.props.get("agent", "claude"))
            agent.currentTextChanged.connect(lambda text: self.set_prop(node_id, "agent", text))
            self.inspector.addRow(tr("Agent"), agent)
            self.llm_server_row(node)
            self.model_row(node)
            self.text_row(node, "arguments", tr("Agent arguments"), tr("e.g. --verbose"))
        elif kind == "workspace":
            path = PathEdit(node.props.get("path", ""), host=self.host())
            path.changed.connect(lambda text: self.set_prop(node_id, "path", text.strip()))
            self.inspector.addRow(tr("Folder"), path)
            readonly = QCheckBox(tr("every workspace read-only (--workspace-ro)"))
            readonly.setChecked(bool(node.props.get("readonly")))
            readonly.toggled.connect(lambda on: self.set_readonly(on))
            self.disable_if_forbidden(readonly, "--workspace-ro")
            self.inspector.addRow("", readonly)
        elif kind == "mount":
            source = PathEdit(node.props.get("source", ""), host=self.host())
            source.changed.connect(lambda text: self.set_prop(node_id, "source", text.strip()))
            self.inspector.addRow(tr("Host folder or file"), source)
            self.text_row(node, "dest", tr("In the sandbox"), "/mnt/data")
            mode = QComboBox()
            for text, key in ((tr("read-only"), "ro"), (tr("writable"), "rw")):
                mode.addItem(text, key)
            mode.setCurrentIndex(max(0, mode.findData(node.props.get("mode", "ro"))))
            mode.currentIndexChanged.connect(lambda _: self.set_prop(node_id, "mode", mode.currentData()))
            self.inspector.addRow(tr("Mode"), mode)
        elif kind == "mcp":
            name = QComboBox()
            name.setEditable(True)
            name.addItems(self.mcp_names())
            name.setCurrentText(node.props.get("name", ""))
            name.currentTextChanged.connect(lambda text: self.set_prop(node_id, "name", text.strip()))
            self.inspector.addRow(tr("Server"), name)
            self.disable_if_forbidden(name, "--mcp")
            definition = node.props.get("definition")  # the project's own server
            if not isinstance(definition, dict):
                try:
                    definition = self.sai.mcp_definitions().get(node.props.get("name", ""))
                except Exception:  # noqa: BLE001 - an MCP file that does not load: said in the row
                    definition = None
            for label, value in B.mcp_summary(definition):
                self.inspector.addRow(label, plain_label(value))
        elif kind == "proxy":
            enabled = QCheckBox(tr("on (every connection passes it; off: --no-proxy)"))
            enabled.setChecked(node.props.get("enabled", True) is not False)
            enabled.toggled.connect(lambda on: self.set_prop(node_id, "enabled", bool(on)))
            if None in self.forbidden.get("--no-proxy", set()):
                enabled.setChecked(True)
                enabled.setEnabled(False)
                enabled.setToolTip(tr("The administrator's policy forbids --no-proxy"))
            self.inspector.addRow(tr("Connection proxy"), enabled)
            rules = RulesEdit(node.props.get("--allow-http", []))
            rules.changed.connect(lambda values: self.set_prop(node_id, "--allow-http", values))
            self.inspector.addRow(tr("Requests per host (--allow-http)"), rules)
        elif kind == "kits":
            self.kit_rows(node)
        elif kind == "approvals" and isinstance(node.props.get("rules"), dict):
            rules = QPlainTextEdit(json.dumps(node.props["rules"], indent=1))
            rules.setReadOnly(True)
            rules.setFont(MONO)
            rules.setMaximumHeight(180)
            rules.setToolTip(tr("The project's ruleset, written into .sandbox-ai.json (key rules)"))
            self.inspector.addRow(tr("Project rules"), rules)
        options = B._options(self.sai)
        for option in B.KINDS[kind].options:
            if option == "--allow-http" or option not in options:
                continue
            if option == "--kit" and kind == "kits" and self.kit_inventory:
                continue  # the kits are offered by name above
            self.option_row(node, options[option])
        if kind in ("devices", "limits"):
            self.live_rows(node)

    # -- kits --

    def set_kit_inventory(self, inventory):
        """What `sandbox-ai kits list --json` said (from the Kits page or read here): the kits node offers them."""
        self.kit_inventory, self.kit_loading = inventory or {}, False
        if self.inspected in self.items and self.board.node(self.inspected).kind == "kits":
            self.inspect_timer.start()

    def load_kit_inventory(self):
        if self.kit_loading:
            return
        self.kit_loading = True
        if not self.sai.has_kits_command():
            self.set_kit_inventory({})
            return
        background(self.sai.kits_inventory, timeout=90,
                   done=lambda result, error: self.set_kit_inventory(result if error is None else {}))

    def kit_rows(self, node):
        """The kits for the board's agent, by name with what they are (sandbox-ai's built-in ones such as egm
        marked); kits named in the project that this agent does not have are listed too, as problems."""
        if self.kit_inventory is None:
            self.load_kit_inventory()
            self.inspector.addRow(plain_label(tr("Reading the kits (sandbox-ai kits list)…"), role="secondary"))
            return
        if not self.kit_inventory:
            self.inspector.addRow(plain_label(tr("This sandbox-ai cannot list its kits (sandbox-ai kits is missing): "
                                                 "type their names below."), role="secondary"))
            return
        agent = B.values(self.board)[0]
        chosen = list(node.props.get("--kit", []))
        holder = QWidget()
        holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)  # no gaps between the kits
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        boxes = {}
        offered = [kit for kit in self.kit_inventory.get("kits", []) if isinstance(kit, dict)
                   and kit.get("kind") in ("user", "builtin") and isinstance(kit.get("name"), str)]
        names = set()
        for kit in offered:
            name, agents = kit["name"], kit.get("agents") if isinstance(kit.get("agents"), dict) else {}
            if name in names:
                continue
            names.add(name)
            usable = agent in agents and not (kit.get("kind") == "builtin" and agent in (kit.get("replaced") or []))
            if kit.get("kind") == "builtin" and agent in (kit.get("replaced") or []):
                continue  # your kit of that name is listed instead
            label = name + (" " + tr("(built-in)") if kit.get("kind") == "builtin" else "")
            box = QCheckBox(label)
            box.setChecked(name in chosen)
            tip = printable(kit.get("description") or "")
            if not usable:
                box.setEnabled(name in chosen)
                tip = tr("Not for {agent}: only for {agents}", agent=agent, agents=", ".join(sorted(agents)) or "-")
            box.setToolTip(safe_tooltip(tip))
            column.addWidget(box)
            boxes[name] = box
        for name in chosen:
            if name not in boxes:
                box = QCheckBox(tr("{name} (not found)", name=printable(name)))
                box.setChecked(True)
                box.setToolTip(safe_tooltip(tr("No kit of this name: the launcher refuses to start")))
                column.addWidget(box)
                boxes[name] = box
        if not boxes:
            column.addWidget(plain_label(tr("No kits yet: create one on the Kits & plugins page."), role="secondary"))
        node_id = node.id

        def toggled(_=False):
            kept = [name for name in chosen if name in boxes and boxes[name].isChecked()]
            kept += [name for name, box in boxes.items() if box.isChecked() and name not in kept]
            self.set_prop(node_id, "--kit", kept)
        for box in boxes.values():
            box.toggled.connect(toggled)
        self.disable_if_forbidden(holder, "--kit")
        self.inspector.addRow(tr("Kits (--kit)"), holder)

    def enable_kit(self, directory, name):
        """Use kit NAME in the project DIRECTORY: the board of that project gets it in its kits node, and Apply
        writes .sandbox-ai.json after showing the change and trusts exactly that."""
        self.activate(directory)  # the board shown until now is kept for its project
        node = self.board.first("kits")
        if node is not None and name in (node.props.get("--kit") or []):
            inform(self, tr("Board"), tr("{path} uses the kit {name} already.", path=printable(directory),
                                         name=printable(name)))
            return False

        def update():
            target = self.board.first("kits")
            if target is None:
                target = self.board.add("kits")
                target.x, target.y = B.snap(40.0, self.grid()), B.snap(40.0 + 110.0 * len(self.board.nodes) % 700,
                                                                       self.grid())
            target.props["--kit"] = list(target.props.get("--kit") or []) + [name]
        self.change(tr("Use the kit {name}", name=name), update)
        return self.apply()

    def live_rows(self, node):
        """A running sandbox of this project can get the devices and limits now (sandbox-ai devices add /
        limits): clearly marked as live, until that sandbox ends; the project file is not changed by it."""
        workspaces = self.board.of_kind("workspace")
        run = self.sai.run_for(workspaces[0].props.get("path")) if workspaces else None
        if not run:
            return
        label = plain_label(tr("Live, until the sandbox ends: {run} runs this project now.", run=printable(run)),
                            role="notice-warn")
        self.inspector.addRow(label)
        if node.kind == "devices":
            button = QPushButton(tr("Add these devices to {run} now", run=printable(run)))
            button.clicked.connect(lambda: self.live_devices(run, list(node.props.get("--device", [])), button))
        else:
            button = QPushButton(tr("Apply these limits to {run} now", run=printable(run)))
            button.clicked.connect(lambda: self.live_limits(run, node.props, button))
        self.inspector.addRow(button)

    def live_devices(self, run, devices, button):
        if not devices:
            inform(self, tr("Board"), tr("Add devices to the node first."))
            return
        if not ask_first(self, "device-add", tr("Add device"), tr(
                "Give {list} to {run} until it ends? Devices weaken the isolation.",
                list=", ".join(map(printable, devices)), run=printable(run)), tr("Add")):
            return
        background(lambda: [self.sai.add_device(run, d) for d in devices], owner=self, source=button, timeout=300,
                   done=lambda results, error: self.live_done(tr("Add device"), results, error))

    def live_limits(self, run, props, button):
        values = [props.get(o) for o in ("--memory", "--cpus", "--pids")]
        if not any(values):
            inform(self, tr("Board"), tr("Set a limit on the node first."))
            return
        # More than it has now is asked first (the limits now read in the background; not known: asked too).
        background(self.sai.limits_shown, run, owner=self, source=button, timeout=90,
                   done=lambda current, error: self.live_limits_known(run, values, button,
                                                                      current if error is None else None))

    def live_limits_known(self, run, values, button, current):
        memory, cpus, pids = ("" if value in (None, "") else str(value) for value in values)
        wanted = {"memory": memory, "cpus": cpus, "pids": pids}
        labels = {"memory": tr("memory"), "cpus": tr("CPUs"), "pids": tr("processes")}
        changes = ", ".join(tr("{limit}: no limit", limit=labels[key]) if wanted[key].lower() == "none" else
                            tr("{limit}: {value}", limit=labels[key], value=printable(wanted[key]))
                            for key in backend.raised_limits(current, wanted))
        text = tr("Let {run} use more of its host until it ends: {changes}?", run=printable(run), changes=changes)
        if current is None:
            text += " " + tr("The limits it has now are not known: this may be more than now.")
        if not ask_first(self, "limits-raise", tr("Limits"), text, tr("Raise"),
                         detail={"current": current, "values": wanted}):
            return
        background(lambda: [self.sai.set_limits(run, *values)], owner=self, source=button, timeout=150,
                   done=lambda results, error: self.live_done(tr("Limits"), results, error))

    def live_done(self, title, results, error):
        if error is not None:
            warn(self, title, printable(error))
            return
        failed = [(out + err).strip() for status, out, err in results if status != 0]
        text = "\n".join(printable((out + err).strip()) for _, out, err in results)
        (warn if failed else inform)(self, title, text)

    # -- the agent's model server: its own provider or one of yours (sandbox-ai llm) --

    def set_llm_servers(self, servers, host=LOCAL):
        self.llm_loading = False
        if host != self.host():  # another host was chosen meanwhile: its servers, never these
            self.load_llm_servers()
            return
        self.llm_servers = servers if isinstance(servers, list) else []
        if self.inspected in self.items and self.board.node(self.inspected).kind == "agent":
            self.inspect_timer.start()

    def load_llm_servers(self):
        """The model servers of the host it starts on (sandbox-ai llm list there, and llm models NAME for a host's
        servers), in the background."""
        if self.llm_loading:
            return
        host = self.host()
        self.llm_loading = True
        background(lambda: self.sai.host_llm_servers(host), timeout=180,
                   done=lambda result, error: self.set_llm_servers(result if error is None else [], host))

    def llm_server_row(self, node):
        """Beside the agent: the model server it works with, its own provider (cloud) or a registered one as NAME or
        NAME:MODEL (wizard_llm). A server puts the LLM server node on the board with --llm and takes the agent's own
        model away (the launcher refuses --model with --llm); "only this server" is --no-provider-defaults."""
        from . import wizard_llm as WL
        from .wizard import fill_model_combo
        if self.llm_servers is None:
            self.load_llm_servers()
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        combo = QComboBox()
        combo.setToolTip(tr("The agent's own provider (cloud), or a model server of yours (sandbox-ai llm): --llm "
                            "NAME[:MODEL], without --model"))
        only = QCheckBox(tr("only this server"))
        only.setToolTip(tr("--no-provider-defaults: the agent reaches the model server and nothing else"))
        current = WL.from_board(self.board)
        llm = self.board.first("llm")
        value = WL.llm_value(current["name"], current["model"]) if current else \
            str(llm.props.get("--llm") or "") if llm is not None and self.board.connected(llm) else ""
        network = self.board.first("network")
        fill_model_combo(combo, only, self.llm_servers or [], node.props.get("agent", "claude"), self.sai, value,
                         bool(network and network.props.get("--no-provider-defaults")))

        def chosen():
            self.choose_llm_server(WL.choice_of(combo.currentData() or "", only.isChecked()))
        combo.activated.connect(lambda _index: chosen())
        only.toggled.connect(lambda _on: chosen())
        row.addWidget(combo, 1)
        row.addWidget(only)
        self.inspector.addRow(tr("Model server"), holder)
        if self.llm_servers is None:
            self.inspector.addRow(plain_label(tr("Reading the model servers (sandbox-ai llm list)…"), role="secondary"))
        self.llm_combo = combo
        return combo

    def choose_llm_server(self, choice):
        """CHOICE ({name, model, only}, or None: the agent's own provider) on the board, as one undo step; a node
        it adds goes into its column."""
        from . import wizard_llm as WL

        def update():
            before = {n.id for n in self.board.nodes}
            if choice:
                WL.apply(self.board, choice)
            else:
                WL.clear(self.board)
            for node in self.board.nodes:
                if node.id not in before:
                    node.x, node.y = B.snap(40.0 + B.KINDS[node.kind].column * 260.0, self.grid()), \
                        B.snap(40.0 + 110.0 * len(self.board.nodes) % 700, self.grid())
        self.change(tr("Model server"), update)

    def model_row(self, node):
        """The agent's model: its models with their versions to choose from (models.py), any other typed."""
        from . import models
        combo = QComboBox()
        combo.setEditable(True)
        combo.setToolTip(tr("passed as --model to the agent"))
        models.fill_combo(combo, node.props.get("agent", "claude"), str(node.props.get("model", "") or ""))
        combo.activated.connect(lambda _i: self.set_prop(node.id, "model", models.combo_value(combo)))
        combo.lineEdit().editingFinished.connect(lambda: self.set_prop(node.id, "model", models.combo_value(combo)))
        from . import wizard_llm as WL
        if WL.uses_server(self.board):  # the launcher refuses --model with --llm: the model is the server's
            combo.setEnabled(False)
            combo.setToolTip(tr("With a model server the model is the server's: choose NAME:MODEL as the model "
                                "server"))
        self.inspector.addRow(tr("Model"), combo)
        self.model_combo = combo
        return combo

    def text_row(self, node, key, label, placeholder):
        edit = QLineEdit(str(node.props.get(key, "") or ""))
        edit.setPlaceholderText(placeholder)
        edit.editingFinished.connect(lambda: self.set_prop(node.id, key, edit.text().strip()))
        self.inspector.addRow(label, edit)
        return edit

    def option_row(self, node, option):
        """A field for a launcher option, from its kind (flag, choice, several choices, list, value)."""
        node_id, long = node.id, option.long
        value = node.props.get(long)
        if option.kind == "flag":
            widget = QCheckBox(elide(option.help.split(";")[0], 70) if option.help else long)
            widget.setChecked(bool(value))
            widget.toggled.connect(lambda on: self.set_prop(node_id, long, bool(on)))
        elif option.kind == "choice":
            widget = QComboBox()
            items = ([""] if option.default is None else []) + [str(c) for c in option.choices]
            widget.addItems(items)
            widget.setCurrentText(str(value) if value not in (None, "") else ("" if option.default is None
                                                                               else str(option.default)))
            for index, item in enumerate(items):
                if item and item in self.forbidden.get(long, set()):
                    widget.model().item(index).setEnabled(False)
            widget.currentTextChanged.connect(lambda text: self.set_prop(
                node_id, long, "" if text == str(option.default) else text))
        elif option.kind == "multi":
            widget = QWidget()
            row = QHBoxLayout(widget)
            row.setContentsMargins(0, 0, 0, 0)
            boxes = {}
            for choice in option.choices:
                box = QCheckBox(str(choice))
                box.setChecked(str(choice) in (value or []))
                if str(choice) in self.forbidden.get(long, set()) or (long == "--profile" and choice == "egress-all"
                                                                       and "--egress-all-allow" in self.forbidden):
                    box.setEnabled(False)
                    box.setToolTip(tr("The administrator's policy forbids this"))
                row.addWidget(box)
                boxes[str(choice)] = box
            for box in boxes.values():
                box.toggled.connect(lambda _=False: self.set_prop(node_id, long, [c for c, b in boxes.items()
                                                                                  if b.isChecked()]))
        elif option.kind == "list":
            widget = LinesEdit(value or [])
            widget.changed.connect(lambda values: self.set_prop(node_id, long, values))
        else:
            widget = QLineEdit("" if value is None else str(value))
            widget.setPlaceholderText(option.metavar or (str(option.default) if option.default else ""))
            widget.editingFinished.connect(lambda: self.set_prop(node_id, long, widget.text().strip()))
        widget.setToolTip(safe_tooltip(option.help))
        label = QLabel(long + (f"  ({option.short})" if option.short else ""))
        label.setToolTip(safe_tooltip(option.help))
        if None in self.forbidden.get(long, set()):
            self.disable_if_forbidden(widget, long)
        self.inspector.addRow(label, widget)

    def disable_if_forbidden(self, widget, option):
        if None in self.forbidden.get(option, set()):
            widget.setEnabled(False)
            widget.setToolTip(safe_tooltip(tr("The administrator's policy forbids {option}", option=option)))

    def set_readonly(self, on):
        def update():
            for node in self.board.of_kind("workspace"):
                node.props["readonly"] = bool(on)
        self.change(tr("Change {name}", name="--workspace-ro"), update)

    def mcp_names(self):
        try:
            return sorted(self.sai.mcp_definitions())
        except Exception:  # noqa: BLE001
            return []

    # -- projects --

    def open_project(self, directory=None, quiet=False):
        """The board of the project DIRECTORY (asked for when None). QUIET (a switch of the current project):
        what would be a message box is a line above the board instead. The board shown until now is kept for
        its project (activate); reading the same project again drops its unapplied changes, after asking."""
        if directory is None:
            directory = choose_folder(self, tr("Project folder"), "project")
        if not directory:
            return False
        if not quiet and self.key_of(directory) == self.key_of(self.directory) and not self.may_discard():
            return False
        self.reasked = None
        self.sources[self.key_of(directory)] = {"digest": None, "trusted": True}  # a board of nothing read from a file
        try:
            review = self.sai.review_project(directory)
        except FileNotFoundError:
            board = B.from_argv(self.sai, self.default_agent(), [], workspaces=[directory])
            self.untrusted.pop(self.key_of(directory), None)
            self.load(board, directory)
            text = tr("{path} has no .sandbox-ai.json yet: Apply writes one.", path=printable(directory))
            self.show_notice(text) if quiet else inform(self, tr("Board"), text)
            self.show_reasked()
            return True
        except (OSError, ValueError) as exc:
            if quiet:
                self.load(B.from_argv(self.sai, self.default_agent(), [], workspaces=[directory]), directory)
                self.show_notice(tr("The project's .sandbox-ai.json cannot be read: {error}", error=printable(exc)),
                                 "notice-error")
                return False
            warn(self, tr("No project configuration"), printable(exc))
            return False
        if review.document is None:
            if quiet:
                self.load(B.from_argv(self.sai, self.default_agent(), [], workspaces=[directory]), directory)
                self.show_notice(tr("The project's .sandbox-ai.json cannot be used: {problems}",
                                    problems="; ".join(map(printable, review.problems))), "notice-error")
                return False
            warn(self, tr("This project configuration cannot be used"), "\n".join(map(printable, review.problems)),
                 details=review.text())
            return False
        board = B.from_project(self.sai, review.document, directory)
        # A project file nobody trusted is shown, never started as it is: the agent can write it (it lies in the
        # workspace), and Start passes --no-config. Apply (written and trusted after you looked) lifts this. Decided
        # before the board is loaded: a draft restored then may be asked about again (restore_saved).
        self.sources[self.key_of(directory)] = {"digest": review.digest, "trusted": bool(review.trusted)}
        if review.trusted:
            self.untrusted.pop(self.key_of(directory), None)
        else:
            self.untrusted[self.key_of(directory)] = str(review.path)
        self.load(board, directory)
        # Keys no part of the board shows (the security mode, canaries, a kept HOME, ...): named here, and again in
        # Apply's question, which trusts them too.
        weak = PT.weakening(self.sai, review.document)
        extra = ", ".join(f"{'⚠ ' if key in weak else ''}{printable(key)}: {PT.short(value, 60)}"
                          for key, value in PT.unshown(review.document).items())
        also = ("\n" + tr("The file also sets (no part of the board shows it; Apply trusts it too): {list}",
                          list=extra)) if extra else ""
        if not review.trusted:
            text = tr("{path} is not trusted yet: the board shows it; Apply trusts what you see after you review it.",
                      path=printable(review.path)) + also
            self.show_notice(text, "notice-warn") if quiet else inform(self, tr("Board"), text)
        elif also:
            risky = weak.keys() & PT.unshown(review.document).keys()
            self.show_notice(also.strip(), "notice-warn" if risky else "secondary")
        self.show_reasked()
        return True

    def show_reasked(self):
        if self.reasked:
            self.show_notice(self.reasked, "notice-warn")
            self.reasked = None

    def load(self, board, directory, positioned=False):
        """Show BOARD for the project DIRECTORY, laid out (its saved layout, if any) unless POSITIONED (a
        preset: its own positions)."""
        if self.key_of(directory) != self.key_of(self.directory):
            self.stash()  # the project shown until now keeps its board
            old = self.sessions.pop(self.key_of(directory), None)  # replaced by what is loaded now
            if old is not None:
                self.undo_group.removeStack(old["stack"])
                old["stack"].deleteLater()
            self.new_stack()
        if not positioned:
            board.layout(self.grid())
            if directory:
                board.place(B.read_layout(directory))
        self.set_board(board, directory)
        self.fit()
        key = self.key_of(directory)
        if key and self.state is not None and key not in self.restored:
            self.restored.add(key)
            self.restore_saved(self.saved_boards.get(key), key, directory)
        if directory:
            self.opened.emit(str(directory))

    # -- drafts: what was not applied when the app was left --

    def attach_state(self, state):
        self.state = state
        saved = state.get("boards", {})
        self.saved_boards = {k: v for k, v in saved.items() if isinstance(k, str) and isinstance(v, dict)}
        state.collect(self.keep_boards)
        if isinstance(state.get("board_host"), str):
            self.host_choice.choose(state.get("board_host"))
        self.host_choice.chosen.connect(lambda _host: state.schedule())
        self.undo_group.cleanChanged.connect(lambda _clean: state.schedule())
        self.undo_group.indexChanged.connect(lambda _index: state.schedule())

    def keep_boards(self):
        """Per project: zoom and scroll position, and the board itself while it has changes not applied (so a
        crash or a restart loses nothing). Projects not opened in this run keep what they had."""
        sessions = dict(self.sessions)
        self.stash()
        sessions[self.key_of(self.directory)] = self.sessions[self.key_of(self.directory)]
        boards = {}
        for key, session in sessions.items():
            if not key:
                continue
            center = session["center"]
            entry = {"zoom": round(session["transform"].m11(), 4), "center": [round(center.x(), 1), round(center.y(), 1)]}
            if not session["stack"].isClean():
                entry["draft"] = scrub_board(session["board"].state())
                if self.sources.get(key):
                    entry["source"] = dict(self.sources[key])
            boards[key] = entry
        for key, entry in self.saved_boards.items():
            if key not in boards and key not in self.restored:
                boards[key] = entry
        self.state.data["boards"] = boards
        self.state.data["board_host"] = self.host()

    def restore_saved(self, entry, key=None, directory=None):
        """The draft kept for this project (as an undo step: Undo goes back to the file; Discard drops it),
        and its zoom and scroll position. A draft of a board read from another version of the project file than the
        one there now, or from one nobody trusted, carries what that version set (also what no part of the board
        shows: the security mode, canaries, options no node holds): the project counts as not trusted then, so Start
        waits for Apply, which names all of it and asks."""
        if not isinstance(entry, dict):
            return
        draft = entry.get("draft")
        if isinstance(draft, dict):
            try:
                B.Board.from_state(draft)
            except (KeyError, TypeError, ValueError):
                draft = None
            before = self.board.state()
            if draft and draft != before:
                self.undo.push(Snapshot(self, tr("Restore the changes not applied"), before, draft))
                self.draft_notice = tr("Restored the changes not applied when the app was last closed: Undo goes "
                                       "back to the project's file, Discard drops them.")
                self.show_notice(self.draft_notice, "notice-warn")
                self.discard_button.show()
                source, now = entry.get("source"), self.sources.get(key)
                if key and directory and not (isinstance(source, dict) and source == now and source.get("trusted")):
                    self.untrusted[key] = PT.path_text(directory)
                    self.reasked = self.draft_notice + "\n" + tr(
                        "These changes were made to a version of the project file that is not trusted or no longer "
                        "there: Apply (it shows everything that will be trusted and asks) before you start them.")
        zoom, center = entry.get("zoom"), entry.get("center")
        if isinstance(zoom, (int, float)) and 0.05 <= zoom <= 20:
            self.fitted = True  # not fitted again when first shown
            self.view.setTransform(QTransform.fromScale(zoom, zoom))
            if isinstance(center, list) and len(center) == 2 and all(isinstance(v, (int, float)) for v in center):
                self.view.centerOn(QPointF(*center))

    def discard_draft(self):
        """Drop the restored changes: the project's board as its file says."""
        if self.directory:
            self.open_project(self.directory, quiet=True)
        self.discard_button.hide()

    def project_folder(self):
        return self.directory or (self.board.of_kind("workspace")[0].props.get("path")
                                  if self.board.of_kind("workspace") else None)

    def open_gallery(self):
        """The preset gallery: the preset chosen (its placeholders filled in, or left for later) becomes the
        board, after asking whether unapplied changes may go."""
        from .preset_gallery import PresetGallery
        gallery = PresetGallery(self.sai, self.project_folder(), self.default_agent(), self.policy(), self.board,
                                self, self.settings().get("default_backend"), host=self.host())
        self.gallery = gallery
        if gallery.exec() != gallery.DialogCode.Accepted or gallery.result_ is None:
            return None
        preset, values = gallery.result_
        return self.new_from_preset(preset, values)

    def new_from_preset(self, preset, values=None):
        """The board of PRESET (a Preset, or the key or id of one of the board's host's catalogue: its sandbox-ai
        presets, the app's own and yours) for the current project; VALUES fill in its placeholders (None: they stay,
        shown as problems until filled in on the board)."""
        from . import presets
        if not self.may_discard():
            return None
        if isinstance(preset, str):
            preset = presets.catalogue(self.sai, self.host()).find(preset)
        directory = self.project_folder()
        board = presets.build(self.sai, preset, directory, agent=self.default_agent(), values=values,
                              grid=self.grid(), backend=self.settings().get("default_backend"))
        self.load(board, directory, positioned=True)
        left = B.placeholders(board)
        if left:
            self.show_notice(tr("Placeholders to fill in: {count}. Select the parts outlined in red.",
                                count=len(left)), "notice-warn")
        return board

    def new_from_template(self, name=None):
        """A built-in preset by name, its placeholders left to fill in (kept for older callers)."""
        return self.new_from_preset(name or "development")

    def open_wizard(self, ask=True):
        if ask and not self.may_discard():
            return None
        from .wizard import SandboxWizard
        wizard = SandboxWizard(self.sai, self.settings, self, board_host=self.host())
        wizard.written.connect(lambda board, directory: self.wizard_written(board, directory, asked=ask))
        wizard.board_wanted.connect(self.board_for_host)
        wizard.exec()
        return wizard

    def board_for_host(self):
        """The wizard is for this machine; "Use the Board for a host" lands here: the host is chosen beside Apply."""
        self.show_notice(tr("Choose the host the board starts on (beside Apply), then a preset (New from preset…) or "
                            "the parts; paths are that host's, typed in."), "notice-warn")
        self.host_choice.setFocus()

    def wizard_written(self, board, directory, asked=True):
        """Show the board the wizard wrote. Opened without asking first (new_project) and written for the
        folder of the board shown, it would replace that board's unapplied changes: asked now."""
        if not asked and self.key_of(directory) == self.key_of(self.directory) and not self.may_discard():
            return  # the board stays, with its changes (the wizard's file is written all the same)
        self.load(board, directory)

    def show_board(self, board, directory):
        """Open BOARD (e.g. from the wizard) for the project DIRECTORY."""
        self.load(board, directory)

    # -- the host it starts on --

    def host(self):
        return self.host_choice.current()

    def host_changed(self):
        """Another host: Apply (it writes this machine's project file) only here; the launcher's verdict, the dry
        run and the start from that host's launcher, whose options are read in the background."""
        host = self.host()
        remote = host != LOCAL
        self.host_failed = {key: when for key, when in self.host_failed.items() if key[0] != host}  # asked again
        self.check_pending = self.check_running  # an answer for the host before is dropped; this one's after it
        self.apply_button.setEnabled(not remote)
        self.apply_button.setToolTip(safe_tooltip(tr("Apply writes the project file on this machine; on {host} the board starts "
                                        "as it is (--no-config)", host=printable(host)) if remote else
                                     tr("Write .sandbox-ai.json into the first workspace and trust it, after showing "
                                        "it and what changes")))
        self.host_note.setVisible(remote)
        self.llm_servers = None  # the agent's Model server: that host's servers, read again (load_llm_servers)
        if remote:
            self.host_note.setText(tr("Starts on {host}: folders and files are {host}'s (typed in; nothing of this "
                                      "computer is chosen), and its sandbox-ai checks the board.",
                                      host=printable(host)))
        self.inspect_timer.start()  # the path fields: a chooser only for this machine's
        self.check_timer.start(0)

    def host_options_read(self, host, agent, found, error):
        self.host_reading.discard((host, agent))
        self.host_known[(host, agent)] = set(found) if error is None and found else None
        if self.host_known[(host, agent)] is None:
            self.host_failed[(host, agent)] = time.monotonic()  # asked again later, or when needed
        else:
            self.host_failed.pop((host, agent), None)
        if error is not None and host == self.host():
            self.host_note.setText(self.host_note.text() + " " + tr("Its options could not be read: {error}",
                                                                    error=printable(str(error))[-200:]))

    # -- actions --

    def errors(self):
        return [p for p in B.problems(self.sai, self.board, self.policy()) if p.level == "error"]

    def apply(self, then=None, lead=""):
        """Show the project file of the board and what changes, then write and trust exactly it (in a thread).
        THEN runs once it is written (a start that needs the file); LEAD is said before the question. True when
        the file is being written."""
        errors = self.errors()
        if errors:
            warn(self, tr("Not written"), "\n".join(p.text for p in errors))
            return False
        document, main = B.project_document(self.sai, self.board)
        if not main:
            inform(self, tr("Project configuration"), tr("Add a workspace first: the file is written there."))
            return False
        # Every key that will be trusted is named, what weakens the sandbox marked, and the details show the changes
        # against the version trusted LAST (never against the file in the folder: the agent may have written it).
        intro = tr("Write {path} with this content and trust exactly it? The details show the changes against the "
                   "version you trusted last, and the whole file.", path=f"{main}/.sandbox-ai.json")
        question, details = PT.question(self.sai, main, document, (lead + "\n\n" + intro) if lead else intro)
        # Trusted, the file's options apply at every later start of the project: asked first (Cancel the default).
        if not ask_first(self, "project-trust", tr("Write and trust"), question, tr("Write and trust"), details=details):
            return False
        background(self.sai.write_project, main, document, owner=self, source=self.apply_button, timeout=60,
                   done=lambda result, error: self.applied(main, result, error, then))
        return True

    def applied(self, main, result, error, then=None):
        if error is not None:
            warn(self, tr("Not written"), printable(error))
            if then is not None:
                warn(self, tr("Not started"), tr("The project file was not written, so the start that needs it "
                                                 "did not happen."))
            return
        try:
            B.write_layout(main, self.board)
        except OSError:
            pass  # the layout is a convenience
        self.directory = main
        self.show_folder()
        self.undo.setClean()
        self.show_notice("")
        self.untrusted.pop(self.key_of(main), None)
        try:  # the file just written and trusted is the source of the board's next draft
            self.sources[self.key_of(main)] = {"digest": self.sai.review_project(main).digest, "trusted": True}
        except (OSError, ValueError):
            self.sources.pop(self.key_of(main), None)
        self.opened.emit(str(main))
        if then is not None:  # the start waited for the file: it goes on now (no second message in between)
            then()
            return
        inform(self, tr("Project configuration"), tr("Written and trusted: {path}", path=printable(result)))

    def dry_run(self):
        agent, _, _ = B.values(self.board)
        self.read_host_options(self.host(), agent, now=True)  # on demand, also after a failed read
        background(self.sai.host_dry_run, self.host(), agent,
                   B.launcher_argv(self.sai, self.board, local=self.host() == LOCAL), owner=self,
                   source=self.dry_button, timeout=120, done=self.dry_run_done)

    def dry_run_done(self, result, error):
        from .widgets import show_text
        plan, message = result if error is None else (None, str(error))
        if plan is None:
            warn(self, tr("The launcher refused these options"), message)
            return
        show_text(self, tr("Plan (dry run)"), (message + "\n\n" if message else "") + json.dumps(plan, indent=2))

    def start(self, embedded=False, session=None):
        """Start exactly the board (--no-config: a project file is not merged in on top)."""
        errors = self.errors()
        if errors:
            warn(self, tr("Not started"), "\n".join(p.text for p in errors))
            return False
        if not any(n.props.get("path") for n in self.board.of_kind("workspace") if n in self.board.included()):
            # Without one the launcher takes the folder the app (or the SSH login) runs in, often HOME, as /work.
            warn(self, tr("Not started"), tr("Add a workspace first: without one the sandbox would get the folder the "
                                             "app runs in - often your HOME - as its writable /work."))
            return False
        untrusted = self.untrusted.get(self.key_of(self.directory))
        if untrusted and self.host() != LOCAL:
            # Apply writes and trusts this machine's file, so it is not offered for another host: there the board starts
            # as it is shown (--no-config: no project file is read there), after a yes to what came from that file.
            if not self.start_untrusted_on_host(untrusted):
                return False
        elif untrusted:
            warn(self, tr("Not started"), tr("{path} is not trusted: look at the board, then Apply (it writes and trusts "
                                             "what you see) before you start it.", path=printable(untrusted)))
            return False
        if session == "":
            inform(self, tr("Session"), tr("Give the session a name first."))
            return False
        servers = B.project_servers(self.board)
        host = self.host()
        if servers and host != LOCAL:
            warn(self, tr("Not started"), tr("The board uses MCP servers the project defines ({names}): the launcher "
                                             "knows them only from the project's trusted .sandbox-ai.json, which "
                                             "Apply writes on this machine. On {host}, use MCP servers registered "
                                             "there (sandbox-ai mcp list).", names=", ".join(servers),
                                             host=printable(host)))
            return False
        if B.project_rules(self.board) is not None and host != LOCAL:
            warn(self, tr("Not started"), tr("The board has the project's own ruleset (.sandbox-ai.json \"rules\"): "
                                             "it holds only for starts on this machine. On {host}, give it as a "
                                             "ruleset file there (--rules FILE), or start on this machine.",
                                             host=printable(host)))
            return False
        if servers:
            return self.start_from_project_file(servers, embedded, session)
        return self.run_start(B.launcher_argv(self.sai, self.board, local=host == LOCAL), embedded, session)

    def start_untrusted_on_host(self, path):
        """The board came from the untrusted project file PATH of this machine and starts on another host: asked first
        (Cancel the default) whether exactly the board shown may start there - the file itself is neither written
        nor trusted (Apply is this machine's), and nothing of it is read on that host."""
        host = printable(self.host())
        hidden = self.hidden_settings()
        text = tr(
            "The board comes from {path}, which is not trusted. Apply (write and trust) is for this machine only, so "
            "on {host} the board starts as it is shown, without any project file (--no-config). Look at every part of "
            "the board first: whoever could write that folder chose it. Start it on {host} as shown?",
            path=printable(path), host=host)
        if hidden:
            text += "\n\n" + tr("It also passes what no part of the board shows:") + "\n" + "\n".join(hidden)
            if any(PT.WARN in line for line in hidden):
                text += "\n\n" + tr("Lines marked {mark} weaken the sandbox: trust them only if you chose them.",
                                     mark=PT.WARN)
        return ask_first(self, "project-trust", tr("Start on {host}", host=host), text, tr("Start as shown"))

    def hidden_settings(self):
        """Lines naming what a start of the board passes that no part of the board shows: the project file's own keys
        (the security mode, canaries, a kept HOME, debug logs, its name: PT.unshown) and the options no node holds
        (Board.extra), each in full, what weakens the sandbox marked."""
        keys = PT.unshown(self.board.document_extra)
        weak = PT.weakening(self.sai, keys)
        lines = [f"  {PT.WARN + ' ' if key in weak else ''}{printable(key)}: {PT.whole(value)}"
                 for key, value in keys.items()]
        if self.board.extra:
            lines.append("  options: " + " ".join(printable(str(token)) for token in self.board.extra))
            lines += [f"    {PT.WARN} {printable(widening)}: " + tr("widens what the sandbox may do or see")
                      for widening in PT.option_warnings(self.sai, self.board.extra)]
        return lines

    def run_start(self, options, embedded, session):
        """Run the start in a terminal; a failure to start it is a message, never an exception in a slot."""
        try:
            agent, _, agent_args = B.values(self.board, self.sai)
            host = self.host()
            if host != LOCAL:
                known = self.host_known.get((host, agent))
                if not known:  # not read yet, or the read failed: asked again now, for the next start and the verdict
                    self.read_host_options(host, agent, now=True)
                unknown = backend.unknown_options(self.sai.options(), options, known) if known else []
                if unknown:
                    warn(self, tr("Not started"), tr("The sandbox-ai on {host} does not know these options: {list}. "
                                                     "Take them off the board, or update sandbox-ai there.",
                                                     host=printable(host), list=" ".join(unknown)))
                    return False
                argv = self.sai.host_launch_argv(host, agent, options, agent_args, session or None)
                title = f"{printable(host)}: {agent}" + (f" ({session})" if session else "")
                self.terminal(argv, title, embedded=None if session else embedded, cwd=None)
                return True
            argv = self.sai.launch_argv(agent, options, agent_args, session or None)
            workspaces = self.board.of_kind("workspace")
            cwd = workspaces[0].props.get("path") if workspaces else None
            title = agent + (f" ({session})" if session else "")
            self.terminal(argv, title, embedded=None if session else embedded, cwd=cwd)
        except Exception as exc:  # noqa: BLE001 - the window stays; the reason is shown
            warn(self, tr("Not started"), printable(exc))
            return False
        return True

    def project_file_state(self, main, document):
        """"same" when MAIN/.sandbox-ai.json is trusted and holds exactly DOCUMENT; else "missing", "untrusted"
        (not trusted or changed since), "differs" (another content) or "unreadable"."""
        try:
            review = self.sai.review_project(main)
        except FileNotFoundError:
            return "missing"
        except (OSError, ValueError):
            return "unreadable"
        if review.document != json.loads(json.dumps(document)):
            return "differs"
        return "same" if review.trusted else "untrusted"

    def start_from_project_file(self, servers, embedded, session, written=False):
        """The board uses MCP servers the project defines: the launcher knows them only from the trusted project
        file, so the start takes the board from that file (written and trusted by Apply first, after the human
        saw it). Declined: not started, with the reason."""
        document, main = B.project_document(self.sai, self.board)
        names = ", ".join(servers)
        if not main:
            warn(self, tr("Not started"), tr("The board uses MCP servers the project defines ({names}); they are "
                                             "written into the project file, which needs a workspace first.",
                                             names=names))
            return False
        state = self.project_file_state(main, document)
        if state == "same":
            return self.run_start(B.file_start_argv(self.sai, self.board), embedded, session)
        if written:  # just written and trusted, and still not the board (swapped meanwhile?): no second round
            warn(self, tr("Not started"), tr("{path} does not hold the board after it was written; look at it, then "
                                             "Apply again.", path=printable(f"{main}/.sandbox-ai.json")))
            return False
        why = {"missing": tr("The project file is not written yet."),
               "untrusted": tr("The project file is not trusted (or changed since it was trusted)."),
               "differs": tr("The project file differs from the board."),
               "unreadable": tr("The project file cannot be read.")}[state]
        lead = tr("The board uses MCP servers the project defines ({names}). The launcher knows them only from the "
                  "project's trusted .sandbox-ai.json. {why} It is written and trusted now, then the sandbox starts.",
                  names=names, why=why)
        if self.apply(then=lambda: self.start_from_project_file(servers, embedded, session, written=True),
                      lead=lead):
            return True
        if not self.errors():  # errors were shown by apply already
            warn(self, tr("Not started"), tr("The board uses MCP servers the project defines ({names}). The launcher "
                                             "knows them only from the project's trusted .sandbox-ai.json, which was "
                                             "not written. Apply it (it is shown first), then start.", names=names))
        return False


def preset_kind(path):
    """"app" for a preset file of this app, "sandbox-ai" for one of sandbox-ai itself, else None (read: at most the
    size a preset may have, not through a link)."""
    from . import presets
    text = read_small(path, presets.LIMIT + 1)
    try:
        document = json.loads(text) if text and len(text) <= presets.LIMIT else None
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    return {presets.FORMAT: "app", "sandbox-ai preset": "sandbox-ai"}.get(document.get("format"))


def read_small(path, limit=1 << 20):
    """The text of a small regular file (not through a link), or ""."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return ""
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, limit).decode("utf-8", "replace")
    finally:
        os.close(fd)

