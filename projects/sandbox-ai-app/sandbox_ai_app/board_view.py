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

import functools
import json
import os
import posixpath
import re
import time

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QShortcut, QColor, QContextMenuEvent, QCursor, QDrag, QFont, QFontMetricsF, QKeySequence, QPainter, QPainterPath,
    QPainterPathStroker, QPen, QPixmap, QPolygonF, QTextLayout, QTextOption, QTransform, QUndoCommand, QUndoGroup,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGraphicsEllipseItem,
    QGroupBox,
    QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSplitter, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)

from . import applog, backend, board as B, board_icons, remote_chooser as RC, widgets
from .prerequisites import Prerequisites, busy_now
from .session_start import SessionStart, TeamStart
from . import help_view, labels as OL, project_trust as PT, rules_tools, tables, theme
from .files import printable, printable_text
from .host_choice import on_host
from .name_fields import NameButton, NameField, add_line
from .copying import copy_entries
from .i18n import N_, tr
from .short_notices import short_notice
from . import mcp_fields as MF
from . import list_fields as LF
from . import device_picker as DP
from .widgets import (
    MONO, AsyncCommand, ElidedLabel, MoreHeader, StartControls, ask_first, background, confirm, disclosure, explanation,
    fill, flow, inform, plain_label, safe_tooltip, set_enabled, table as tables_table, warn,
)

NF_OPTIONS = ("--secret-env", "--api-key", "--git-token-secret")  # their lines name a secret (name_fields)
# Options whose value names something the host has (name_fields' kinds): an editable list of those, any other typed.
VALUE_NAMES = {"--vpn": "vpn", "--image-variant": "variant", "--image": "agent_image", "--llm": "llm"}
# Parts whose properties end in options that seldom need to be set: the options after this one are folded under "More
# options" (open or folded per kind while the app runs: BoardPage.more_open). The agent: after its questions option
# (--dangerously-skip-permissions, "Agent without approval prompts"); the network: its Podman network and fixed
# addresses; the engine: Podman's address, the agent's executable and the OCI runtime. (The Bridge part folds its
# limits after "Ask you about": bridge_rows.)
MORE_AFTER = {"agent": "--dangerously-skip-permissions", "network": "--egress-all-allow",
              "engine": "--nested-containers"}
# Parts whose list options are ONE field (list_fields.ListFields, user 2026-11-19): a drop-down of the kinds of value,
# Add, what was added as chips grouped by kind - a kind without values takes no room (they were as many boxes of
# their own, mostly empty). Each option keeps its key and its launcher option: a board saves and starts as before.
# (Title of the row, the options in it.) The agents' "features turned on again" (--hermes-enable ...): only the
# board's agent's is offered; another agent's values are shown, to be removed.
LIST_GROUPS = {
    "network": (N_("Hosts, addresses, name servers"), ("--allow-host", "--allow-domain", "--allow-shared-wildcard",
                                                    "--allow", "--allow-tcp", "--allow-host-port", "--nameserver")),
    "agent": (N_("Variables, keys, features"), ("--env", "--secret-env", "--api-key", "--hermes-enable", "--qwen-enable",
                                             "--goose-enable", "--openhands-enable", "--nanobot-enable",
                                             "--letta-enable", "--opencode2-enable", "--pi-enable")),
    "git": (N_("Push tokens"), ("--git-token-env", "--git-token-file", "--git-token-secret")),
    "proxy": ("", ("--no-inspect",)),  # (one kind: its own label names the row)
    "vpn": ("", ("--vpn-bypass-tcp",)),
    "devices": ("", ("--device",)),
}
# What a suggestion of a secret's name becomes in each of these (name_fields.add_line): HOST to be typed over.
SECRET_SUGGESTIONS = {"--secret-env": "{name}", "--api-key": "HOST={name}", "--git-token-secret": "https://HOST={name}"}
FLAG_MENTION = re.compile(r"--[a-z0-9][a-z0-9-]*")


def hidden_by_itself(widget):
    """WIDGET was hidden on purpose (setVisible(False)), not merely not shown yet (a row just added)."""
    return widget.isHidden() and widget.testAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide)


def alive(widget):
    """Whether WIDGET's C++ object still exists (a late result must not touch a deleted one)."""
    try:
        from shiboken6 import isValid
        return isValid(widget)
    except ImportError:  # pragma: no cover - PySide6 always has it
        return True

LOCAL = "local"  # this machine, as `sandbox-ai hosts` names it


def remote_run_for(found, path):
    """The name of the running start of another host (FOUND: its host_instances) whose workspace is PATH, or None."""
    wanted = str(path or "").rstrip("/")
    return next((i.name for i in found or [] if wanted and i.state == "running" and i.kind in ("run", "session")
                 and str(i.project or "").rstrip("/") == wanted), None)
OTHER_IMAGE = "\x00other"  # the image drop-down's "Other…" (no image's name: a name is typed in then)
ADD_IMAGE = "\x00add"  # the image drop-down's "Add a service image (Images page)…"
REMOTE_CHECK_DELAY = 2000  # ms after an edit before another host's launcher checks the board (a dry run over SSH)
MORE_FOCUS_KEPT = 1.5  # s a finding's jump into More options keeps the keyboard there across the inspector being made anew
HOST_RETRY = 60  # seconds before another host's launcher options are asked for again after a failed read
MCP_KEEP = 30  # seconds another host's MCP servers (and catalog entries) are offered before they are read again


class OpeningCombo(QComboBox):
    """A combo box that says when its list is opened (opening): its entries are read again then; and when it is
    closed again (closed) - however it closes: a choice, Escape, a click elsewhere."""
    opening = Signal()
    closed = Signal()

    def showPopup(self):  # noqa: N802
        self.opening.emit()
        super().showPopup()
        popup = self.view().window()
        if popup is not self.window() and not getattr(self, "watching_popup", False):
            popup.installEventFilter(self)
            self.watching_popup = True

    def popup_open(self):
        return self.view().isVisible()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Hide and watched is self.view().window():
            self.closed.emit()
        return super().eventFilter(watched, event)


HISTORY_STEPS = 20  # steps of Undo kept per project in the state file (with its draft)
# Steps of Undo per project in this run (each holds the board before and after it, some KB): without a limit a day of
# work on a board grew the app by megabytes, for good.
UNDO_LIMIT = 500
BOARDS_BUDGET = 1 << 20  # characters of the state file for the boards of every project (within_budget)


def history_of(stack):
    """{"steps": [{"text", "before", "after"}], "index"}: the last HISTORY_STEPS steps of Undo of STACK (those that
    can be redone too) and where it stands among them; None when there are none. The states are copies, scrubbed
    like a draft."""
    # Only the last steps are copied: this runs at every write of the state (each change of the board), and copying
    # every step of the stack made each write take longer with every change (a third of a second after 10000).
    # A Change folder is a step too (R2-18: it was not, and a draft whose last 20 steps held one came back without
    # its Undo): with both folders and the trust bookkeeping of each (folder_trust) - after a restart Undo onto a
    # folder whose project file nobody trusted, or one changed meanwhile, leaves the board not trusted there.
    first = max(0, stack.count() - HISTORY_STEPS)
    kept = []
    for index in range(first, stack.count()):
        command = stack.command(index)
        if not isinstance(command, (Snapshot, FolderChange, HostFolderChange)):
            return None
        step = {"text": command.text(), "before": scrub_board(json.loads(json.dumps(command.before_state()))),
                "after": scrub_board(json.loads(json.dumps(command.after_state())))}
        if isinstance(command, FolderChange):
            step.update({"kind": "folder", "before_dir": None if command.before is None else str(command.before),
                         "after_dir": None if command.after is None else str(command.after),
                         "trust": {key: record for key, record in ((key, folder_trust(command, key)) for key in
                                                                   {command.page.key_of(command.before),
                                                                    command.page.key_of(command.after)} if key)}})
        elif isinstance(command, HostFolderChange):
            step.update({"kind": "host-folder",
                         "before_remote": [str(v) for v in command.before] if command.before else None,
                         "after_remote": [str(v) for v in command.after] if command.after else None})
        kept.append(step)
    if not kept:
        return None
    return {"steps": kept, "index": max(0, stack.index() - first)}


def folder_trust(command, key):
    """{"untrusted", "source"}: what the board's page knows of the project file of the folder KEY (a step of Change
    folder's) - the path of one nobody trusted, and the version the board was read from (BoardPage.sources); what
    the step itself brought from the state file while the page has not looked at that folder in this run."""
    page = command.page
    if key in page.sources or key in page.untrusted:
        untrusted, source = page.untrusted.get(key), page.sources.get(key)
        return {"untrusted": str(untrusted) if untrusted else None, "source": dict(source) if source else None}
    record = (command.trust or {}).get(key)
    return dict(record) if isinstance(record, dict) else {"untrusted": None, "source": None}


def within_budget(boards, shown=None, budget=None):
    """BOARDS (keep_boards' entries, changed in place) within BOARDS_BUDGET characters of the state file: the steps of
    Undo kept with the drafts go first - the largest first, the board SHOWN last; the drafts themselves (what was not
    applied) and the views stay. Kept without a bound, the drafts of a few dozen projects (those not opened for months
    too) made the state file larger than state.LIMIT: the next start read none of it, and every draft was lost."""
    budget = BOARDS_BUDGET if budget is None else budget
    sizes = {key: len(json.dumps(entry, separators=(",", ":"))) for key, entry in boards.items()}
    total = sum(sizes.values())
    if total <= budget:
        return boards
    histories = sorted((key for key, entry in boards.items() if isinstance(entry, dict) and "history" in entry),
                       key=lambda key: (key == shown, -sizes[key]))
    for key in histories:
        history = boards[key].pop("history")
        total -= len(json.dumps(history, separators=(",", ":")))
        if total <= budget:
            break
    return boards


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


def scrub_values(props):
    """A part's settings without the values that look like a credential themselves (state.looks_secret): for the
    clipboard, which other programs read."""
    from .state import looks_secret
    if isinstance(props, dict):
        return {key: scrub_values(value) for key, value in props.items() if not (isinstance(value, str)
                                                                                  and looks_secret(value))}
    if isinstance(props, list):
        return [scrub_values(value) for value in props if not (isinstance(value, str) and looks_secret(value))]
    return props


def same_folder(a, b):
    if not a or not b:
        return False
    return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))

NODE_W, NODE_H, PORT_R = 210.0, 66.0, 6.0  # NODE_H: the least height; a node with more text grows (NodeItem)
HEADER_H = 26.0  # the title band of a node (a title of two lines makes it taller)
CLOSE_SIZE = 16.0  # the [x] of a node that may be removed
NODE_PAD = 10.0  # the text's margin inside a node
ICON_ROOM = board_icons.SIZE + board_icons.GAP  # the kind's icon before the title (board_icons)
TEXT_LINES, TITLE_LINES = 3, 2  # a node shows at most 3 lines of text, at most 2 of them for its title


def wrapped(text, font, width, lines):
    """TEXT broken into at most LINES lines no wider than WIDTH in FONT (anywhere in a word too long for a line, a
    path); the last one ends in "…" when more is left. [] for no text."""
    text = " ".join(str(text).split())
    if not text or lines < 1:
        return []
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    starts = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        starts.append((line.textStart(), line.textLength()))
    layout.endLayout()
    metrics = QFontMetricsF(font)
    shown = [metrics.elidedText(text[start:start + length].strip(), Qt.TextElideMode.ElideRight, width)
             for start, length in starts[:lines]]
    if len(starts) > lines:  # the rest does not fit: the last line shown ends in "…"
        rest = text[starts[lines - 1][0]:].strip()
        shown[-1] = metrics.elidedText(rest, Qt.TextElideMode.ElideRight, width)
        if not shown[-1].endswith("…"):
            shown[-1] = metrics.elidedText(rest + " …", Qt.TextElideMode.ElideRight, width)
    return shown
def wrapped_title(text, font, width, lines):
    """A node's title in at most LINES lines (wrapped()); a title naming a path ("Project folder: /a/b/c") that does
    not fit keeps the path's end - the folder's own name - and is cut in the middle: "Project folder:" over
    "/a/…/b/c" (design review 0.1.95: the end was cut, and the folder's name was never shown) - in a theme's large
    font too, whose "Project folder:" does not fit its line: "Project fol…" over "/a/…/c"."""
    shown = wrapped(text, font, width, lines)
    head, colon, path = str(text).partition(": ")
    if not colon or not path.startswith(("/", "~")) or lines < 2 or not shown or not shown[-1].endswith("…"):
        return shown
    first = wrapped(head + ":", font, width, 1)
    if not first:
        return shown
    # (a label too long for its line in a large font, fallout's: cut itself - the folder's name matters more)
    return [first[0], elided_path(" ".join(path.split()), font, width)]


def elided_path(path, font, width):
    """PATH cut in the middle to WIDTH in FONT, its last part - the folder's own name with its "/" - kept whole when
    it fits: "/tmp/py…/project", not "/tmp/py…project" (Qt's middle cut halves the room, and in a wide font, a
    monospace theme's, the half was too short for the name)."""
    metrics = QFontMetricsF(font)
    middle = metrics.elidedText(path, Qt.TextElideMode.ElideMiddle, width)
    cut = path.rstrip("/").rfind("/")
    name = path[cut:] if cut > 0 else ""
    if not name or middle == path or middle.endswith(name):
        return middle
    room = width - metrics.horizontalAdvance(name)
    while room > metrics.horizontalAdvance("…"):
        start = metrics.elidedText(path[:cut], Qt.TextElideMode.ElideRight, room)
        if start.endswith("…") and metrics.horizontalAdvance(start + name) <= width:
            return start + name
        room -= 1  # (kerning: the two parts together a pixel wider than each alone)
    return middle


MIME_PART = "application/x-sandbox-ai-part"  # a part dragged from the palette: its kind
MIME_PARTS = "application/x-sandbox-ai-parts"  # parts copied (Copy, Cut): their kinds, settings and connections
PARTS_FORMAT = "sandbox-ai-board-parts"  # the same as JSON text, for a clipboard that keeps only text
CLIP_LIMIT = 256 * 1024  # bytes of copied parts read back at most (the clipboard may hold anything)



# The launcher's refusal of a model server in the local network behind Tor names the option that lets it in.
BYPASS_OFFER = re.compile(r"--vpn-bypass-tcp ((?:\d{1,3}\.){3}\d{1,3}:\d{1,5})")

class Snapshot(QUndoCommand):
    """One change of the board: the state before and after it (redo restores after, undo before)."""

    def __init__(self, page, text, before, after):
        super().__init__(text)
        self.page, self.before, self.after = page, before, after

    def before_state(self):
        return self.before

    def after_state(self):
        return self.after

    def redo(self):
        self.page.restore(self.after)

    def undo(self):
        self.page.restore(self.before)


class FolderChange(QUndoCommand):
    """Change folder…: the board's project folder from BEFORE to AFTER (its main workspace part with it: the states
    BOARD_BEFORE and BOARD_AFTER) as one undo step. TRUST ({folder key: folder_trust's record}): the trust
    bookkeeping of a step that came back from the state file - looked at again when the board moves onto that folder
    (BoardPage.take_trust). While the page puts its steps back (history_restoring) only the board changes."""

    def __init__(self, page, text, before, after, board_before, board_after, trust=None):
        super().__init__(text)
        self.page, self.before, self.after = page, before, after
        self.board_before, self.board_after = board_before, board_after
        self.trust = trust

    def before_state(self):
        return self.board_before

    def after_state(self):
        return self.board_after

    def redo(self):
        if self.page.history_restoring:
            self.page.restore(self.board_after)
            return
        self.page.move_to_folder(self.after, self.board_after, self)

    def undo(self):
        if self.page.history_restoring:
            self.page.restore(self.board_before)
            return
        self.page.move_to_folder(self.before, self.board_before, self)


class HostFolderChange(QUndoCommand):
    """Change folder… with another host chosen: the board's folder there from BEFORE to AFTER ((host, folder) each:
    BoardPage.remote_project) and its main workspace part with it (the states BOARD_BEFORE and BOARD_AFTER), as one
    undo step. Nothing of this machine changes."""

    def __init__(self, page, text, before, after, board_before, board_after):
        super().__init__(text)
        self.page, self.before, self.after = page, before, after
        self.board_before, self.board_after = board_before, board_after

    def before_state(self):
        return self.board_before

    def after_state(self):
        return self.board_after

    def redo(self):
        if self.page.history_restoring:
            self.page.restore(self.board_after)
            return
        self.page.move_to_host_folder(self.after, self.board_after)

    def undo(self):
        if self.page.history_restoring:
            self.page.restore(self.board_before)
            return
        self.page.move_to_host_folder(self.before, self.board_before)


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
        painter.drawPixmap(0, 0, help_view.mark_pixmap(help_view.SIZE, self.hovered))  # (drawn once, kept)

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


class NodeCloseMark(QGraphicsItem):
    """The [x] at a node's top right corner (parts that may be removed): a click asks first (unless the setting
    board_confirm_remove is off), then removes the part (one step of Undo)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.hovered = False
        self.setPos(NODE_W - CLOSE_SIZE - 5, (HEADER_H - CLOSE_SIZE) / 2)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tr("Remove this part"))
        self.setZValue(3)

    def boundingRect(self):  # noqa: N802
        return QRectF(0, 0, CLOSE_SIZE, CLOSE_SIZE)

    def paint(self, painter, option, widget=None):
        colors = theme.colors()
        rect = self.boundingRect()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bad = QColor(theme.color("bad"))
        painter.setPen(QPen(bad if self.hovered else QColor(colors["secondary"]), 1.3))
        painter.setBrush(bad if self.hovered else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(1.0, 1.0, -1.0, -1.0), 3.0, 3.0)
        cross = QPen(QColor(colors.get("accent_text", "#ffffff")) if self.hovered else QColor(colors["secondary"]), 1.6)
        cross.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(cross)
        inset = 5.0
        painter.drawLine(QPointF(inset, inset), QPointF(CLOSE_SIZE - inset, CLOSE_SIZE - inset))
        painter.drawLine(QPointF(CLOSE_SIZE - inset, inset), QPointF(inset, CLOSE_SIZE - inset))
        painter.restore()

    def hoverEnterEvent(self, event):  # noqa: N802
        self.hovered = True
        self.update()

    def hoverLeaveEvent(self, event):  # noqa: N802
        self.hovered = False
        self.update()

    def mousePressEvent(self, event):  # noqa: N802
        event.accept()  # not a move or a selection of the node

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        event.accept()  # not the node's properties

    def mouseReleaseEvent(self, event):  # noqa: N802
        event.accept()
        node = self.parentItem()
        if node is not None and self.boundingRect().contains(event.pos()):
            QTimer.singleShot(0, node.page, lambda node_id=node.node_id, page=node.page: page.ask_remove(node_id))


class NodeItem(QGraphicsRectItem):
    """A part on the board: its kind's icon and its title in the title band and what it sets below, wrapped to the
    node's width in at most TEXT_LINES lines (the node grows for them, the last one shown ends in "…" when more is
    left); the whole text is in its tooltip and in the inspector. The text is painted as plain text (paths and names
    may be the agent's), never outside the node."""

    def __init__(self, page, node):
        super().__init__(0.0, 0.0, NODE_W, NODE_H)
        self.page, self.node_id, self.kind = page, node.id, node.kind
        self.hint = None  # while a connection is dragged: "target" (it can take it) or "hover" (it would)
        self.titles, self.details, self.header_h = [], [], HEADER_H  # the lines shown, the title band's height
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setBrush(QBrush(QColor(theme.colors()["node"])))
        self.title_font = QFont()
        self.title_font.setBold(True)
        self.detail_font = QFont()
        has_in, has_out = B.ports(node.kind)
        self.inport = PortItem(self, "in") if has_in else None
        self.outport = PortItem(self, "out") if has_out else None
        self.help_mark = NodeHelpMark(self, node.kind)
        removable = not B.KINDS[node.kind].required
        self.close_mark = NodeCloseMark(self) if removable else None
        if removable:  # the (?) moves left of the [x]
            self.help_mark.setPos(NODE_W - CLOSE_SIZE - help_view.SIZE - 9, (HEADER_H - help_view.SIZE) / 2)
        self.setPos(node.x, node.y)
        self.update_from(node, None, True)

    def update_from(self, node, level, included, why=(), agent=None):
        """The node's texts, the colour of its worst problem and whether it counts; nothing is done again when none
        of them changed (every step of the board updates every node). WHY: its problems' texts - a part outlined in
        red says why on it (its first error) and in its tooltip (all of them)."""
        color = {"error": theme.color("bad"), "warning": theme.color("warn")}.get(level, theme.colors()["border"])
        detail = summary(node, agent)
        if node.kind == "llm" and hasattr(self.page, "profile_badge"):
            badge = self.page.profile_badge()  # the model profile of its model in the board's agent
            detail = " · ".join(filter(None, [detail, badge]))
        if why:
            detail = ("✘ " if level == "error" else "! ") + why[0]
        help_text = "\n".join([*(f"- {text}" for text in why), *([""] if why else []), tr(B.KINDS[node.kind].help)])
        shown = (node.title(), detail, help_text, color, bool(level), bool(included))
        if shown == getattr(self, "shown", None):
            return
        self.shown = shown
        self.set_text(*shown[:3])
        self.setPen(QPen(QColor(color), 2.5 if level else 1.2))
        self.setOpacity(1.0 if included else 0.45)

    def set_text(self, title, detail, help_text=""):
        """The lines of TITLE (beside the (?)) and DETAIL in the node, its height for them, the ports at its middle
        and the edges after them; the whole text in the tooltip."""
        title_lines = QFontMetricsF(self.title_font).lineSpacing()
        detail_lines = QFontMetricsF(self.detail_font).lineSpacing()
        room = NODE_W - 2 * NODE_PAD - ICON_ROOM - help_view.SIZE - 6 - \
            (CLOSE_SIZE + 4 if getattr(self, "close_mark", None) else 0)
        self.titles = wrapped_title(title, self.title_font, room, TITLE_LINES)
        self.details = wrapped(detail, self.detail_font, NODE_W - 2 * NODE_PAD, TEXT_LINES - max(1, len(self.titles)))
        self.header_h = max(HEADER_H, 10.0 + len(self.titles) * title_lines)
        height = max(NODE_H, self.header_h + 9.0 + len(self.details) * detail_lines + 9.0)
        if height != self.rect().height():
            self.setRect(0.0, 0.0, NODE_W, height)
            for port in (self.inport, self.outport):
                if port is not None:
                    port.setPos(port.pos().x(), height / 2)
            for edge in getattr(self.page, "edges", []):
                if self in (edge.source, edge.target):
                    edge.update_path()
        whole = "\n".join(" ".join(str(text).split()) for text in (title, detail) if str(text).strip())
        self.setToolTip(safe_tooltip("\n\n".join(text for text in (whole, help_text) if text)))
        self.update()

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
        header.addRect(QRectF(rect.left(), rect.top(), rect.width(), self.header_h))
        painter.fillPath(header.intersected(body), QColor(colors["node_header"]))
        pen = QPen(self.pen())
        if self.isSelected():
            pen = QPen(QColor(colors["accent"]), max(2.5, pen.widthF()))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(body)
        self.paint_text(painter, body, colors)
        if data.get("bevel"):  # Windows 3.11: a raised frame inside the black outline
            painter.setPen(QPen(QColor(colors.get("light_edge", "#ffffff")), 1.0))
            painter.drawLine(QPointF(rect.left() + 2, rect.bottom() - 2), QPointF(rect.left() + 2,
                                                                                  rect.top() + self.header_h))
            painter.setPen(QPen(QColor(colors.get("dark_edge", "#808080")), 1.0))
            painter.drawLine(QPointF(rect.left() + 2, rect.bottom() - 2), QPointF(rect.right() - 2, rect.bottom() - 2))
        if self.hint:
            frame = QPainterPath()
            frame.addRoundedRect(rect.adjusted(-5, -5, 5, 5), radius + 4, radius + 4)
            painter.setPen(QPen(QColor(colors["accent"]), 3.0 if self.hint == "hover" else 1.6,
                                Qt.PenStyle.SolidLine if self.hint == "hover" else Qt.PenStyle.DashLine))
            painter.drawPath(frame)

    def paint_text(self, painter, body, colors):
        """The kind's icon (beside the title's first line, in the title's colour, from board_icons' cache), the
        title's lines after it and the detail's lines below (wrapped by set_text), clipped to the node's shape."""
        painter.save()
        painter.setClipPath(body)
        rect = self.rect()
        title_color = colors.get("node_title", colors["text"])
        title_metrics = QFontMetricsF(self.title_font)
        board_icons.paint(painter, self.kind, QPointF(rect.left() + NODE_PAD, rect.top() + 5.0 + max(
            0.0, (title_metrics.lineSpacing() - board_icons.SIZE) / 2)), board_icons.SIZE, title_color)
        for font, color, lines, x, y in (
                (self.title_font, title_color, self.titles, NODE_PAD + ICON_ROOM, rect.top() + 5.0),
                (self.detail_font, colors["secondary"], self.details, NODE_PAD, rect.top() + self.header_h + 9.0)):
            metrics = QFontMetricsF(font)
            painter.setFont(font)
            painter.setPen(QColor(color))
            for line in lines:
                painter.drawText(QPointF(rect.left() + x, y + metrics.ascent()), line)
                y += metrics.lineSpacing()
        painter.restore()


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

    def near(self, point):
        """Whether the scene point POINT is on the line (its shape is the area under the curve too)."""
        stroker = QPainterPathStroker()
        stroker.setWidth(10.0)
        return stroker.createStroke(self.path()).contains(self.mapFromScene(point))

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


class KindIcon(QGraphicsItem):
    """A kind's icon on the canvas (board_icons: drawn from its cache, sharp at any zoom); decoration only."""

    def __init__(self, kind, color, parent=None):
        super().__init__(parent)
        self.kind, self.color = kind, color
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self):  # noqa: N802
        return QRectF(0, 0, board_icons.SIZE, board_icons.SIZE)

    def paint(self, painter, option, widget=None):
        board_icons.paint(painter, self.kind, QPointF(0, 0), board_icons.SIZE, self.color)


class DropGhost(QGraphicsPathItem):
    """Where a dragged part or folder will land: a dashed card in the accent colour (the danger colour when the
    board cannot take it), snapped to the grid, with what a drop does - for a part, after its kind's icon."""

    def __init__(self, label, refused=False, kind=None):
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
        self.label = QGraphicsSimpleTextItem(elide(label, 30 if kind is None else 27), self)
        self.label.setBrush(QBrush(QColor(colors["text"])))
        font = QFont()
        font.setBold(True)
        self.label.setFont(font)
        self.icon = KindIcon(kind, colors["text"], self) if kind is not None else None
        if self.icon is not None:
            self.icon.setPos(10.0, (NODE_H - board_icons.SIZE) / 2)
        self.label.setPos(10.0 + (ICON_ROOM if self.icon is not None else 0.0),
                          (NODE_H - self.label.boundingRect().height()) / 2)

    def move_to(self, point, grid):
        self.setPos(B.snap(point.x() - NODE_W / 2, grid), B.snap(point.y() - NODE_H / 2, grid))


class BoardScene(QGraphicsScene):
    """Connecting by dragging from an output port onto a node that can take it (its input port or anywhere on it;
    the nodes that can are framed while dragging, Esc cancels); one undo step per move (snapshot at the left
    button's press: only the left button selects and moves nodes, BoardView keeps the others for itself)."""

    def __init__(self, page):
        super().__init__()
        self.page, self.dragging, self.line, self.pressed = page, None, None, None

    def node_items(self):
        return [i for i in self.items() if isinstance(i, NodeItem)]

    def target_at(self, point):
        """The node a connection dragged from self.dragging would go to at POINT (or None)."""
        for item in self.items(point):
            node = item.parentItem() if isinstance(item, (PortItem, NodeHelpMark, NodeCloseMark)) else item
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
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        self.pressed = self.page.board.state()
        super().mousePressEvent(event)  # a click selects; held down, the part moves with the pointer

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        """A double click on a part opens its properties (a single click only selects it, and moves it while held)."""
        if event.button() == Qt.MouseButton.LeftButton:
            for item in self.items(event.scenePos()):
                if isinstance(item, (NodeCloseMark, NodeHelpMark)):
                    event.accept()
                    return
                node = item.parentItem() if isinstance(item, PortItem) else item
                if isinstance(node, NodeItem):
                    event.accept()
                    self.page.show_properties(node.node_id)
                    return
        super().mouseDoubleClickEvent(event)

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
        if event.button() != Qt.MouseButton.LeftButton:
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
    """The canvas. The left button selects, moves and connects nodes and draws the rubber band on the empty canvas;
    the right or the middle button drags the view (pans; nothing on the board moves, nothing is selected, no step
    of Undo); a right click without moving opens the context menu of the node or of the canvas there."""

    def __init__(self, scene, page):
        super().__init__(scene)
        self.page = page
        self.pan = None  # while the right or middle button is down: {"button", "start", "last", "panning"}
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        # The grid of dots is drawn once into a cache and again only when the view scrolls, zooms or changes size
        # (every repaint drew it anew: a few ms each, under every move of a part).
        self.setCacheMode(QGraphicsView.CacheModeFlag.CacheBackground)
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

    def zoom_reset(self):
        """Zoom 100 %, the centre of the view staying where it is."""
        center = self.mapToScene(self.viewport().rect().center())
        self.setTransform(QTransform())
        self.centerOn(center)

    # -- the mouse: the left button for the nodes, the right and the middle one to pan, a right click for the menu --

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            if self.pan is None:
                point = event.position().toPoint()
                self.pan = {"button": event.button(), "start": point, "last": point,
                            "panning": event.button() == Qt.MouseButton.MiddleButton}
                if self.pan["panning"]:
                    self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if self.pan is not None:  # another button while panning: ignored
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self.pan is None:
            super().mouseMoveEvent(event)
            return
        point = event.position().toPoint()
        if not self.pan["panning"] and \
                (point - self.pan["start"]).manhattanLength() >= QApplication.startDragDistance():
            self.pan["panning"] = True  # a drag, not a click: no menu at the release
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        if self.pan["panning"]:
            delta = point - self.pan["last"]
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self.pan["last"] = point
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self.pan is None:
            super().mouseReleaseEvent(event)
            return
        event.accept()
        if event.button() != self.pan["button"]:
            return
        pan, self.pan = self.pan, None
        self.viewport().unsetCursor()
        if pan["button"] == Qt.MouseButton.RightButton and not pan["panning"]:
            self.show_menu(event.position().toPoint())

    def contextMenuEvent(self, event):  # noqa: N802
        """The menu key (a right click opens the menu at its release, see mouseReleaseEvent: a press may start a
        pan): at the selected node, else in the middle of the view."""
        event.accept()
        if event.reason() == QContextMenuEvent.Reason.Mouse:
            return
        nodes = [i for i in self.scene().selectedItems() if isinstance(i, NodeItem)]
        point = self.mapFromScene(nodes[0].sceneBoundingRect().center()) if nodes else self.viewport().rect().center()
        self.show_menu(point)

    def show_menu(self, point):
        """The context menu at POINT of the viewport: the node's or the edge's there, else the canvas's."""
        menu = self.page.canvas_menu(self.mapToScene(point))
        if menu is not None:
            self.open_menu(menu, self.viewport().mapToGlobal(point))

    def open_menu(self, menu, global_point):
        menu.exec(global_point)
        menu.deleteLater()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.scene().cancel_connect():
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.page.fold_inspector():  # its docked panel folds
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.page.delete_selected()
            event.accept()
            return
        # The keyboard alone (there was no way to choose a part without the mouse - none to its properties, its menu,
        # its place): the arrow keys choose the next part that way, with Alt they move the parts chosen by one step
        # of the grid, Enter opens the chosen part's properties with the keyboard in them, Ctrl+A chooses all.
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if event.key() in arrows and modifiers == Qt.KeyboardModifier.NoModifier:
            self.page.select_towards(*arrows[event.key()])
            event.accept()
            return
        if event.key() in arrows and modifiers == Qt.KeyboardModifier.AltModifier:
            self.page.nudge_selected(*arrows[event.key()])
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and modifiers == Qt.KeyboardModifier.NoModifier \
                and self.page.properties_by_keyboard():
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.page.select_all()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            self.page.copy_selected()
        elif event.matches(QKeySequence.StandardKey.Cut):
            self.page.cut_selected()
        elif event.matches(QKeySequence.StandardKey.Paste):
            point = self.viewport().mapFromGlobal(QCursor.pos())  # under the pointer when it is on the canvas
            self.page.paste(self.mapToScene(point) if self.viewport().rect().contains(point) else None)
        else:
            super().keyPressEvent(event)
            return
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
        QTimer.singleShot(0, self, lambda: self.page.drop(what, self.mapToScene(point),
                                                          self.viewport().mapToGlobal(point)))


class PartsDelegate(QStyledItemDelegate):
    """A row of the parts: its kind's icon (board_icons, in the theme now: greyed with the row, in the selection's
    colour when selected), its title, and a (?) at the right that explains the part."""

    def __init__(self, parts):
        super().__init__(parts)
        self.parts = parts

    def mark(self, rect):
        """Where the (?) of the row in RECT is: at its right end, inside the visible part of the list; an empty
        rectangle while the (?)s are switched off (the setting help_buttons)."""
        if not help_view.buttons_shown():
            return QRect()
        size = help_view.SIZE
        right = min(rect.right(), self.parts.viewport().width() - 1)
        return QRect(right - size - 4, rect.center().y() - size // 2 + 1, size, size)

    def initStyleOption(self, option, index):  # noqa: N802
        super().initStyleOption(option, index)
        kind = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(kind, str):
            option.icon = board_icons.icon(kind)
            option.decorationSize = QSize(board_icons.SIZE, board_icons.SIZE)
            option.features |= QStyleOptionViewItem.ViewItemFeature.HasDecoration

    def paint(self, painter, option, index):
        mark = self.mark(option.rect)
        if mark.isNull():  # no (?): the title has the whole row
            super().paint(painter, option, index)
            return
        text_option = QStyleOptionViewItem(option)
        text_option.rect = QRect(option.rect.left(), option.rect.top(), max(0, mark.left() - 4 - option.rect.left()),
                                 option.rect.height())
        super().paint(painter, text_option, index)
        hovered = self.parts.hovered == (index.row(), True)
        painter.drawPixmap(mark.topLeft(), help_view.mark_pixmap(mark.width(), hovered, option.font,
                                                                painter.device().devicePixelRatioF()))

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
    """The card shown under the pointer while a part is dragged: its kind's icon and title in the title band."""
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
    title_color = colors.get("node_title", colors["text"])
    board_icons.paint(painter, kind, QPointF(6, 2), board_icons.SIZE, title_color)
    painter.setPen(QColor(title_color))
    painter.drawText(QRectF(6 + ICON_ROOM, 1, width - 12 - ICON_ROOM, 18), int(Qt.AlignmentFlag.AlignVCenter),
                     elide(tr(B.KINDS[kind].title), 20))
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
        self.setIconSize(QSize(board_icons.SIZE, board_icons.SIZE))
        self.setItemDelegate(PartsDelegate(self))
        help_view.on_shown_changed(self.help_shown)

    def help_shown(self, _shown):
        """The (?) of each row follows the setting help_buttons: painted again with or without it."""
        self.hovered = None
        self.viewport().update()

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
        copy_entries(menu, self, event.pos())
        widgets.exec_dropped(menu, event.globalPos())


def elide(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def elide_middle(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit // 2 - 1] + "…" + text[-(limit // 2):]


def summary(node, agent=None):
    """One line of what a node sets (shown under its title); AGENT: the board's agent (what its LLM part offers)."""
    props = node.props
    if node.kind == "agent":
        # Which model and effort it works with, at a glance (the agent's default when none is chosen; the effort
        # only for an agent that has levels), then -D.
        from . import guide
        parts = [str(props.get("model") or tr("default model"))]
        if guide.AGENT_EFFORTS.get(props.get("agent", "claude")):
            parts.append(tr("effort {level}", level=props["effort"]) if props.get("effort") else tr("default effort"))
        if props.get("thinking") in ("on", "off"):
            parts.append(tr("thinking shown") if props["thinking"] == "on" else tr("thinking hidden"))
        if props.get("--dangerously-skip-permissions"):
            parts.append("-D")
        return printable(" · ".join(parts))
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
        from . import ask_choice as AC
        parts = [AC.label("allow") if props.get("--ask") == "allow" else f"--ask {props['--ask']}"] \
            if props.get("--ask") else []
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
    if node.kind == "service":  # its image and where the agent reaches it, unchecked
        if props.get("raw"):
            return tr("cannot be read")
        where = ", ".join(f"localhost:{port}" for port in props.get("ports") or [])
        return printable(" · ".join(filter(None, [str(props.get("image") or ""), where, tr("UNCHECKED")])))
    if node.kind == "llm":  # which server (and model, when named) the agent works with, on the board at a glance
        value = str(props.get("--llm") or "")
        # Which of the server's models the agent offers besides (--llm-models; all by default).
        offered = str(props.get("--llm-models") or "").strip()
        # (An agent given only the start's model - Pi, Letta Code, nanobot, OpenHands - offers no other.)
        models = tr("the start's model only") if value and agent in B.ONE_MODEL_AGENTS else \
            tr("models: {models}", models=elide(offered, 30)) if offered and offered != "all" else \
            tr("all its models") if value and "," not in value else ""
        # The use case (--use-case) whose choice and settings the start takes.
        case = tr("use case {name}", name=elide(str(props.get("--use-case")), 24)) if props.get("--use-case") else ""
        return printable(" · ".join(filter(None, [elide(value, 40), models, case, tr("its address and port only")]))) \
            if value else tr("its address and port only")
    if node.kind == "home":  # where the login comes from, at a glance (and a kept HOME)
        choice = B.login_choice(props)
        login = tr("login from session {name}", name=elide(str(props.get("--auth-from-session") or "?"), 24)) \
            if choice == "session" else tr("no login") if choice == "none" else \
            tr("login file {path}", path=elide(str(props.get("--auth-file") or "?"), 30)) if choice == "file" else \
            tr("the automatic login")
        return printable(" · ".join(filter(None, [login, tr("HOME in {path}", path=elide(str(props["--state-dir"]), 30))
                                                  if props.get("--state-dir") else ""])))
    if node.kind == "kits" and props.get("--kit"):
        return printable(", ".join(props["--kit"]))
    if node.kind == "network":
        if props.get("--offline"):
            return tr("offline")
        parts = list(props.get("--profile", [])) + [o[len("--allow-"):] for o in B.PRESET_OPTIONS if props.get(o)]
        parts += list(props.get("--allow-host", []))[:3]
        # --allow-domain as what it stands for, as `sandbox-ai net show` shows it: the name and *.DOMAIN.
        parts += [part for value in list(props.get("--allow-domain", []))[:3]
                  for part in (B._domain(value), "*." + B._domain(value))]
        return ", ".join(parts) or tr("the agent's provider only")
    shown = [f"{option} {value if not isinstance(value, (list, bool)) else ''}".strip()
             for option, value in props.items() if option.startswith("--") and value not in (None, "", [], False)]
    return printable(", ".join(shown)) if shown else tr("defaults")


class PathEdit(QWidget):
    """A line edit with a folder (or file) chooser: this machine's, or for a path of another HOST that host's folders
    (remote_chooser: listed over SSH, the path kept as it is there). OTHER_MACHINE: a second button builds a folder of
    another machine (sshfs://..., remote_source.RemoteSourceDialog) - the start's host mounts it with sshfs."""
    changed = Signal(str)

    def __init__(self, value="", directory=True, host=LOCAL, other_machine=False):
        super().__init__()
        self.host = host or LOCAL
        # What takes a path chosen after this field went (the inspector is rebuilt while a host is being listed): the
        # same as its changed signal, set by the inspector
        self.fallback = None
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
        self.remote_button = None
        if other_machine:
            remote = self.remote_button = QToolButton()
            remote.setText(tr("sshfs…"))
            remote.setAccessibleName(tr("Folder of another machine…"))
            remote.setToolTip(safe_tooltip(tr("A folder of another machine (sshfs://user@machine/path): the start's "
                                              "host mounts it with sshfs, the agent gets only the files")))
            remote.clicked.connect(self.choose_remote)
            layout.addWidget(remote)
        if self.host != LOCAL:  # a path of that host: chosen from its folders, never from this computer's
            self.edit.setToolTip(safe_tooltip(tr("A path on {host}: … lists {host}'s folders", host=printable(host))))

    def choose_remote(self):
        from . import remote_source
        dialog = remote_source.RemoteSourceDialog(self, self.edit.text().strip())
        if widgets.exec_dropped(dialog) == QDialog.DialogCode.Accepted and alive(self):
            self.edit.setText(dialog.source())
            self.changed.emit(dialog.source())

    def browse(self, directory):
        path = (RC.choose_folder(self, self.host, tr("Choose a folder"), "folder", start=self.edit.text().strip())
                if directory else
                RC.choose_file(self, self.host, tr("Choose a file"), "file", start=self.edit.text().strip()))
        if path and alive(self):
            self.edit.setText(path)
            self.changed.emit(path)
        elif path and self.fallback is not None:  # (the inspector was rebuilt meanwhile)
            self.fallback(path)


class LinesEdit(QPlainTextEdit):
    """One value per line, committed shortly after typing stops (one undo step per pause). ITEMS (a list option of
    the launcher's, backend.LIST_OPTIONS): a line may list several, separated by commas or spaces, each passed on its
    own (once)."""
    changed = Signal(list)

    def __init__(self, values, items=False):
        super().__init__("\n".join(map(str, values or [])))
        self.items = items
        if items:
            self.setToolTip(safe_tooltip(tr("One or more per line, separated by commas or spaces")))
        self.setMaximumHeight(80)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.changed.emit(self.values()))
        self.textChanged.connect(lambda: self.timer.start(600))

    def values(self):
        if self.items:
            return backend.list_items(self.toPlainText())
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
        tables.keep(tables.setup(self.table, 2, (1,)), "board-rules")
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
        widgets.list_menu(self.table, lambda: [remove, None, add])
        layout.addWidget(self.table)
        layout.addLayout(row)

    def add_row(self, host, methods, path, emit=False):
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate((host, methods, path)):
            self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.blockSignals(False)
        tables.fit_columns(self.table)
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


def fill_agent_combo(combo, sai, current="claude"):
    """COMBO with sandbox-ai's agents, each with its name as data; one that has not passed its live test there (its
    launcher's one list: sai.experimental_agent) is listed as “NAME (not live-tested yet)”, a badge only. CURRENT
    chosen (choose_agent); read the choice with combo_agent."""
    from . import presets as PR
    combo.blockSignals(True)
    try:
        combo.clear()
        for agent in sai.agents:
            badge = sai.experimental_agent(agent)
            combo.addItem(tr("{agent} (not live-tested yet)", agent=agent) if badge else agent, agent)
            if badge:
                combo.setItemData(combo.count() - 1, safe_tooltip(PR.experimental_text(badge)),
                                  Qt.ItemDataRole.ToolTipRole)
    finally:
        combo.blockSignals(False)
    choose_agent(combo, current)


def choose_agent(combo, agent):
    """Choose AGENT in an agent COMBO (fill_agent_combo); False if it is not there."""
    index = combo.findData(agent)
    if index >= 0:
        combo.setCurrentIndex(index)
    return index >= 0


def combo_agent(combo):
    """The agent chosen in COMBO (fill_agent_combo: its data, never the shown text with its badge)."""
    data = combo.currentData()
    return data if isinstance(data, str) and data else combo.currentText()


def experimental_note(sai, agent):
    """The words under an agent choice for AGENT: not live-tested yet in this sandbox-ai; "" for the rest."""
    from . import presets as PR
    return PR.experimental_text(sai.experimental_agent(agent))


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
        except Exception as _exc:  # noqa: BLE001 - a damaged hosts file: this machine still counts
            applog.swallowed("board_view", "HostCombo.reload: a damaged hosts file: this machine still counts", _exc)
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


class BoardPage(Prerequisites, SessionStart, TeamStart, QWidget):
    """The board of one sandbox, its checks, and Apply / Dry run / Start."""

    PREREQUISITE_LOG = "board"
    opened = Signal(str)  # a project folder was opened
    server_properties = Signal(str, str)  # an MCP server's Properties (its defaults for every start): name, host
    # The same with a board's values filled in ({limits, variables}), for OK to make them the server's defaults
    server_defaults = Signal(str, str, object)
    images_wanted = Signal()  # a start refused for a service's image: the Images page, Your images
    team_wanted = Signal(str, str)  # a Team part's "Open on the Teams page": the team's name and the board's host

    def __init__(self, sai, terminal, settings=None):
        super().__init__()
        self.sai, self.terminal = sai, terminal
        self.settings = settings or (lambda: {})
        self.board, self.directory, self.items, self.edges = B.Board(), None, {}, []
        self.check_token, self.restoring, self.inspected, self.launcher_ok = 0, False, None, None
        self.problems = []
        self.untrusted = {}  # {project key: path} of boards loaded from a project file nobody trusted yet
        # (host, folder) of the board shown when it came from a folder of another host (open_remote_project): no
        # project of this machine (directory None, no project tab), its folder that host's as it is there
        self.remote_project = None
        # {project key: {"digest", "trusted"}}: the project file each board was read from (digest None: none read), kept
        # with a draft; a draft from another or an untrusted version is asked about again (restore_saved)
        self.sources = {}
        self.reasked = None  # the notice of a draft asked about again (restore_saved), shown once the project is open
        self.ghost = None  # the drop indicator while something is dragged over the board
        self.project_request = None  # the window: open a folder dropped on the board as the project (in its tab)
        self.inspector_request = None  # the window: show the inspector's panel again (a part was selected)
        self.fold_request = None  # the window: fold the inspector's docked panel (Esc, the empty canvas); True if so
        self.close_request = None  # the window: close the properties, folded or floating (their OK and Cancel)
        self.kit_inventory, self.kit_loading = None, False  # sandbox-ai kits list --json, once read
        # The agent sessions of this machine that run (the Sandboxes page's look): what a Bridge part may join; and the
        # waiters of the Bridge parts of the starts made here (each makes its bridges once both sessions run).
        self.running_sessions, self.bridge_waiters = [], []
        # ... and those of the user's hosts ({host: entries named HOST:NAME}), read in the background when a Bridge
        # part is inspected (at most every HOST_SESSIONS_EVERY seconds, each host apart): offered grouped by host
        self.host_sessions, self.host_session_problems, self.host_sessions_asking = {}, {}, set()
        self.host_sessions_read, self.peer_field = None, None
        # The logins of each host's sessions (sandbox-ai login --status --sessions there): {host: its answer}, read in
        # the background when the part HOME and login is inspected or the board takes a login from a session (at most
        # every SESSION_LOGINS_EVERY seconds per host); "Login from" offers them, the checks use them.
        self.session_login_data, self.session_login_asking, self.session_login_when = {}, set(), {}
        # The Team part: team start's verdict on the board's host (team start --dry-run --json; (board state, host,
        # result or None, error or None) of the last answer) and the team's board as last read (team show --json)
        self.team_token, self.team_running, self.team_pending, self.team_verdict = 0, False, False, None
        self.team_board_token, self.team_shown, self.team_view = 0, None, None
        self.llm_servers, self.llm_loading = None, False  # sandbox-ai llm list --json, for the agent's Model server
        # Your images (sandbox-ai images list) and the variants' bases of the host it starts on: the services choose
        # from them, the checks and the score use them (None: not read yet).
        self.user_images, self.image_bases, self.user_images_loading = None, None, False
        self.image_templates = None  # sandbox-ai's own Containerfiles for services there (images templates)
        # Your images listed elsewhere (the Images page added or removed one): the image lists read them again when
        # opened next.
        from .name_fields import hub as names_hub
        names_hub().changed.connect(self.names_changed)
        self.state, self.saved_boards, self.restored = None, {}, set()  # drafts and zoom kept between starts
        self.history_restoring = False  # restore_history puts kept steps back: a Change folder only changes the board
        self.forbidden = self.read_forbidden()
        self.policy_data = self.read_policy()
        # The inspector is rebuilt after the signal that changed the model has returned: its widgets may be
        # the ones that sent it.
        self.inspect_timer = QTimer(self)
        self.inspect_timer.setSingleShot(True)
        self.inspect_timer.setInterval(0)
        self.inspect_timer.timeout.connect(lambda: self.inspect(self.board.node(self.inspected)
                                                                if self.inspected in self.items else None))
        # The properties' "More options" (begin_more): open or folded per kind of part for this app session; what a
        # finding opened once (not again after the user folded it); a row to put the keyboard in once it is shown.
        self.more_open, self.more_auto, self.more_focus = {}, set(), None
        self.more_header, self.more_section, self.option_fields = None, None, {}
        self.list_kinds = {}  # the kind chosen in a part's list field (list_fields_row), per kind of part
        layout = QVBoxLayout(self)
        self.folder = plain_label(tr("No project folder"), word_wrap=False)
        open_button = QPushButton(tr("Open project folder…"))
        # In the window, "Open project folder…" opens the folder in a tab of its own (open_request) - a folder of this
        # machine; with another host chosen, a folder of that host, opened on the board (open_project).
        self.open_request = None
        open_button.clicked.connect(lambda: self.open_request() if self.open_request and self.host() == LOCAL
                                    else self.open_project())
        new_button = self.gallery_button = QPushButton(tr("New from preset…"))
        new_button.setToolTip(tr("Ready boards, simple and complex (MCP servers, VPNs, LLM servers, rules), and "
                                 "your own"))
        new_button.clicked.connect(lambda: self.open_gallery())
        # Save: the board where it belongs (the preset of yours it came from, its project file, or a new preset).
        save = self.save_button = QPushButton(tr("Save"))
        save.setToolTip(tr("Keep this board where it belongs (Ctrl+S): in the preset of yours it was made from, else in "
                           "its project's file, else as a new preset of yours"))
        save.clicked.connect(lambda: self.save_board())
        # The board as a preset of yours (with its values and folders), or the one of yours it came from updated.
        save_preset = self.save_preset_button = QPushButton(tr("Save as preset…"))
        save_preset.setToolTip(tr("Keep this board as a preset of your own, with its values, folders, notes and "
                                  "layout (never a secret); a board made from one of yours can replace it"))
        save_preset.clicked.connect(lambda: self.save_as_preset())
        self.own_preset = None  # the preset of yours the board came from (new_from_preset), to update
        # The board as one self-sufficient file (sandbox-ai board export), and such a file taken in (board import):
        # its parts written after its plan was confirmed, the board shown here (not started).
        export_button = self.export_button = QPushButton(tr("Export…"))
        export_button.setToolTip(tr("Write this board as one file another machine imports: its start, its MCP and LLM "
                                    "servers, rulesets, kits, presets and image recipes - folders as placeholders, "
                                    "secrets by name only"))
        export_button.clicked.connect(lambda: self.export_board())
        import_button = self.import_button = QPushButton(tr("Import…"))
        import_button.setToolTip(tr("Take a board's or session's file in: its plan first (what is created, reused, "
                                    "missing), then, confirmed, its parts written and the board shown here"))
        import_button.clicked.connect(lambda: self.import_board())
        wizard_button = QPushButton(tr("Wizard…"))
        wizard_button.setToolTip(tr("A new project step by step; it ends on this board"))
        wizard_button.clicked.connect(lambda: self.open_wizard())
        # Another project folder for this very board: every part and setting stays (change_folder).
        self.change_folder_button = QPushButton(tr("Change folder…"))
        self.change_folder_button.setToolTip(tr(
            "The board works on another folder: every part and setting stays, its main workspace and where its "
            "project file and changes belong move there (nothing is written before Apply); undo takes it back"))
        self.change_folder_button.clicked.connect(lambda: self.change_folder())
        # One undo stack per project: the board of each project (its tab) keeps its changes, undo and zoom
        # while another project is shown (sessions, keyed by folder).
        self.undo_group = QUndoGroup(self)
        self.new_stack()
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
        # The security score, also the switch of its panel with the findings (closed until asked for).
        # A disclosure (an arrow: right folded, down open): its checked state never looks like a button stuck pressed.
        # (its text on two lines when its row is narrower: in a large font it was the widest of the row, R2-19)
        self.score_button = disclosure(widgets.TwoLineToolButton())
        self.score_button.setAccessibleName(tr("Security score and findings"))
        self.score_button.setToolTip(tr("Show or hide the security score and the findings"))
        self.score_button.clicked.connect(lambda on: self.show_findings(on))
        self.score_request = None  # the window: show (True) or hide (False) the findings' panel
        # The command line the board stands for, behind a button: rarely needed in the app, it took two lines.
        self.cli_button = disclosure(QToolButton())  # an arrow: open or folded, never a button stuck pressed
        self.cli_button.setText(tr("CLI command"))
        self.cli_button.setToolTip(tr("Show the sandbox-ai command this board starts (to copy it into a terminal)"))
        self.cli_button.toggled.connect(lambda on: self.show_cli(on))
        # The project and its folder are named at the top of the window (its project chooser): not again here, the
        # row keeps its room for the buttons. (self.folder is still the board's record of its folder.)
        # What belongs together wraps together (user 2026-11-18): Undo with Redo, the zoom buttons, and Arrange beside
        # Fit - the zoom buttons right before them.
        layout.addWidget(flow(open_button, new_button, save, save_preset, export_button, import_button, wizard_button,
                              self.change_folder_button, widgets.together(undo, redo),
                              widgets.together(zoom_out, zoom_in), widgets.together(arrange, fit), self.cli_button,
                              self.score_button))
        # The board's notes (a project file not trusted yet, placeholders to fill in, restored changes) go to the
        # window's log window, one place for every message; the last one is kept here (not shown).
        # (Not in a layout, so given the page as their parent: a widget of no parent whose signal holds the page
        # would keep both alive for good - a cycle through C++ that Python's collector cannot see.)
        self.notice = plain_label(role="secondary", selectable=True)
        self.notice.setParent(self)
        self.notice.hide()
        self.folder.setParent(self)
        self.folder.hide()
        self.logged_notice = ""
        self.discard_button = QPushButton(tr("Discard"), self)  # (kept for callers; the canvas menu offers it now)
        self.discard_button.clicked.connect(lambda _=False: self.discard_draft())
        self.discard_button.hide()
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
        # The palette (a dock of its own in the window); how to use it is in its tooltip and the (?), not in a line
        # that takes the list's room.
        self.parts_panel = QWidget()
        self.parts_panel.setMaximumWidth(230)
        self.parts_panel.setToolTip(safe_tooltip(self.palette.toolTip()))
        parts_layout = QVBoxLayout(self.parts_panel)
        parts_layout.setContentsMargins(0, 0, 0, 0)
        parts_layout.setSpacing(2)
        parts_layout.addWidget(help_view.HelpButton("board.palette"), 0, Qt.AlignmentFlag.AlignRight)
        parts_layout.addWidget(self.palette, 1)
        self.scene = BoardScene(self)
        self.scene.selectionChanged.connect(self.selection_changed)
        self.view = BoardView(self.scene, self)
        zoom_in.clicked.connect(lambda: self.view.zoom(1.2))
        zoom_out.clicked.connect(lambda: self.view.zoom(1 / 1.2))
        fit.clicked.connect(self.fit)
        right = QWidget()
        # The inspector (the selected part's properties): beside the canvas; in the window a panel of its own that can
        # be moved to another edge, undocked, folded to a strip and closed (ui.make_docks).
        self.side = right
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.inspector_title = plain_label(role="section")
        self.inspector_icon = QLabel()  # the selected part's kind's icon (decoration: the title names the part)
        self.inspector_icon.setFixedSize(board_icons.SIZE, board_icons.SIZE)
        self.inspector_icon.hide()
        self.inspector_help = help_view.HelpButton("board.inspector")  # the selected part's kind, else the inspector
        title_row = QHBoxLayout()
        title_row.addWidget(self.inspector_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.inspector_title, 1)
        title_row.addWidget(self.inspector_help, 0, Qt.AlignmentFlag.AlignVCenter)
        self.inspector_holder = QWidget()
        self.inspector = QFormLayout(self.inspector_holder)
        scroll = self.inspector_scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.inspector_holder)
        right_layout.addLayout(title_row)
        right_layout.addWidget(scroll, 1)
        # The score and the findings: a panel of their own, shown only when asked for (the score's button; in the
        # window a panel of View > Panels), never together with the inspector.
        self.findings_panel = QWidget()
        findings_layout = QVBoxLayout(self.findings_panel)
        findings_layout.setContentsMargins(0, 0, 0, 0)
        self.score = plain_label(role="section")
        self.findings = QListWidget()
        self.findings.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.findings.itemDoubleClicked.connect(self.finding_clicked)
        widgets.list_menu(self.findings, lambda: [(
            tr("Show the part"), self.findings.currentItem() is not None and
            self.findings.currentItem().data(Qt.ItemDataRole.UserRole) in self.items, tr("Not about a part"),
            lambda: self.finding_clicked(self.findings.currentItem()))])
        findings_layout.addWidget(self.score)
        findings_layout.addWidget(self.findings, 1)
        self.findings_panel.hide()
        # The panels are the Board's wherever they are (in the window: docks of their own): a dialog of one of them
        # veils the Board, a dialog of the Board veils them too (widgets.page_of, veil_also).
        for panel in (self.parts_panel, right, self.findings_panel):
            panel.veil_page = self
        split.addWidget(self.parts_panel)
        split.addWidget(self.view)
        split.addWidget(right)
        split.setSizes([170, 700, 380])
        layout.addWidget(split, 1)
        layout.addWidget(self.findings_panel)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(MONO)
        self.preview.setMaximumHeight(64)
        self.preview.setAccessibleName(tr("Command line"))
        self.preview.hide()  # until CLI command is pressed
        layout.addWidget(self.preview)
        # One line under the board (the page does not scroll): a long refusal is cut there, whole in the tooltip,
        # copied with its context menu or Ctrl+C, shown whole on a double click.
        self.verdict = ElidedLabel(role="secondary")
        verdict_row = QHBoxLayout()
        verdict_row.setContentsMargins(0, 0, 0, 0)
        verdict_row.addWidget(self.verdict, 1)
        # A model server of the local network refused behind Tor: the launcher names the --vpn-bypass-tcp it needs.
        self.bypass_target, self.bypass_state = None, None
        self.bypass_button = QPushButton()
        self.bypass_button.hide()
        self.bypass_button.clicked.connect(lambda: self.allow_past_tor())
        verdict_row.addWidget(self.bypass_button)
        # A workspace folder that is not there yet: made at once (else at Start or Apply, after a yes).
        self.create_button = QPushButton(tr("Create now"))
        self.create_button.setToolTip(tr("Create the board's workspace folders that do not exist yet (with the "
                                         "folders above them)"))
        self.create_button.hide()
        self.create_button.clicked.connect(lambda: self.create_missing())
        verdict_row.addWidget(self.create_button)
        # An image the start needs is not built on its host: built now in a terminal tab, then checked again.
        self.build_commands = []
        self.build_button = QPushButton(tr("Build now"))
        self.build_button.setToolTip(tr("Build the images the launcher's check names (sandbox-ai build ...) in a "
                                        "terminal tab, then check the board again"))
        self.build_button.hide()
        self.build_button.clicked.connect(lambda: self.build_missing())
        verdict_row.addWidget(self.build_button)
        # A key the start names is not kept yet (--api-key, a preset's ANTHROPIC_API_KEY ...): kept now, from a password
        # field (the launcher's `secrets set NAME`, the value on its standard input), then checked again.
        self.offered_keys, self.secret_state = [], None
        self.secret_button = QPushButton()
        self.secret_button.hide()
        self.secret_button.clicked.connect(lambda: self.keep_secret())
        verdict_row.addWidget(self.secret_button)
        # What the start's preparation does now and how far (a bar for the whole, one per item; its line opens the
        # terminal tab where it runs), or what keeps a start from going on, with the buttons that make it.
        from .preparing import make_panel
        self.preparing_panel = make_panel()
        self.preparing_panel.clicked.connect(lambda: self.show_preparing_where())
        layout.addWidget(self.preparing_panel)
        layout.addLayout(verdict_row)
        self.apply_button = QPushButton(tr("Apply…"))
        self.apply_button.setToolTip(tr("Write .sandbox-ai.json into the first workspace and trust it, after showing "
                                        "it and what changes"))
        self.apply_button.clicked.connect(lambda: self.apply())
        self.dry_button = QPushButton(tr("Dry run"))
        self.dry_button.clicked.connect(lambda: self.dry_run())
        # Clone: this setup as a new project (another folder, its own .sandbox-ai.json) or as a new session.
        self.clone_button = QPushButton(tr("Clone"))
        self.clone_button.setToolTip(tr("This setup as a new project (another folder) or as a new session"))
        clone_menu = QMenu(self.clone_button)
        clone_menu.addAction(tr("As a new project…"), lambda: self.clone_to_project())
        clone_menu.addAction(tr("As a new session"), lambda: self.clone_to_session())
        self.clone_button.setMenu(clone_menu)
        # One Start: where, and whether as a session (a codename unless one is typed) - widgets.StartControls.
        self.start_controls = StartControls(lambda embedded, session: self.start(embedded=embedded, session=session),
                                            sai, self.settings())
        self.session = self.start_controls.name
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
        layout.addWidget(flow(self.host_choice, self.apply_button, self.dry_button, self.clone_button,
                              self.start_controls))
        self.check_timer = QTimer(self)
        self.check_timer.setSingleShot(True)
        self.check_timer.timeout.connect(self.check)
        self.set_board(B.with_backend(B.from_argv(sai, self.default_agent(), []),
                                      self.settings().get("default_backend")), None)
        help_view.on_shown_changed(self.show_help_marks)
        # Ctrl+S saves the board where it belongs (save_board): its preset of yours, its project file (Apply: it shows
        # the file and what changes, then writes and trusts it) or a new preset of yours.
        save = QShortcut(QKeySequence(QKeySequence.StandardKey.Save), self)
        save.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save.activated.connect(lambda: self.save_board())
        self.edit_node, self.edit_index = None, 0  # the part whose properties are open, and Undo's index then

    def show_cli(self, shown):
        """CLI command: the command line of the board under it (SHOWN) or not; kept in the app's state."""
        self.preview.setVisible(bool(shown))
        if self.cli_button.isChecked() != bool(shown):
            self.cli_button.setChecked(bool(shown))
        if self.state is not None:
            self.state.set("board_cli", bool(shown))

    @property
    def session_suggestion(self):
        return self.start_controls.suggestion

    def start_session(self):
        """As a session, where the Start controls say (kept for callers)."""
        self.start_controls.as_session.setChecked(True)
        return self.start_controls.start()

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
        except Exception as _exc:  # noqa: BLE001 - shown on the System page
            applog.swallowed("board_view", "BoardPage.read_forbidden: shown on the System page", _exc)
            return {}

    def read_policy(self):
        try:
            return self.sai.policy()[0]
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("board_view", "BoardPage.read_policy failed (went on without it)", _exc)
            return {}

    def policy(self):
        return self.policy_data

    # -- the model --

    def show_folder(self):
        if self.remote_project is not None:  # a folder of another host (open_remote_project)
            host, folder = self.remote_project
            self.folder.setText(tr("{folder} on {host}", folder=printable(elide_middle(folder, 40)),
                                   host=printable(host)))
            self.folder.setToolTip(safe_tooltip(tr("{folder} on {host}", folder=folder, host=host)))
            return
        self.folder.setText(printable(elide_middle(self.directory, 48)) if self.directory else tr("No project folder"))
        self.folder.setToolTip(safe_tooltip(self.directory) if self.directory else "")

    def set_board(self, board, directory):
        self.board, self.directory = board, (str(directory) if directory else None)
        self.remote_project = None  # (host, folder) of a board opened from another host's folder: set after load
        # The offer of the last verdict was the board's before: never applied to this one.
        self.bypass_target, self.bypass_state = None, None
        self.bypass_button.hide()
        self.offered_keys, self.secret_state = [], None
        self.secret_button.hide()
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
            "center": center, "inspected": self.inspected, "notice": (self.notice.text(), self.notice.property("role")),
            "remote": self.remote_project}

    def new_stack(self):
        self.undo = QUndoStack(self)
        self.undo.setUndoLimit(UNDO_LIMIT)  # (the oldest steps go; a board whose first step went is not clean again)
        self.undo_group.addStack(self.undo)
        self.undo_group.setActiveStack(self.undo)

    def activate(self, directory):
        """Show the board of the project DIRECTORY: as it was left (unapplied changes, undo, zoom) when it was
        shown before, else read from its .sandbox-ai.json."""
        key = self.key_of(directory)
        if key == self.key_of(self.directory):
            return
        session = self.sessions.get(key)
        if session is not None and not alive(session["stack"]):
            # (never left so by the app itself - see drop_kept; whatever did: the project is read again, not an
            # exception at every switch to its tab)
            applog.log("WARNING", "board", f"the board kept for {printable(directory)} lost its Undo: read again")
            self.sessions.pop(key, None)
            session = None
        self.autosave()  # the board shown until now is kept at once
        if getattr(self, "unnamed_again", False) and self.directory is None:
            # The app was left with the board of no folder shown (another host's, a new one) beside a project's tab:
            # the tab's project is read, then that board is shown again as it was.
            self.unnamed_again = False
            QTimer.singleShot(0, self, self.show_unnamed)
        if session is None:
            self.open_project(directory, quiet=True)  # load() keeps the board shown until now
            return
        self.stash()
        self.board, self.undo, self.directory = session["board"], session["stack"], session["directory"]
        self.remote_project = session.get("remote")  # (another host's folder it came from, if it did)
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
        self.drop_kept(key)

    def drop_kept(self, key):
        """The board kept for the project KEY goes; its Undo with it, unless the board shown or another kept one uses
        that very stack (deleted then, the board that still used it raised at every later switch to it)."""
        session = self.sessions.pop(key, None)
        if session is None:
            return
        stack = session["stack"]
        if stack is self.undo or not alive(stack) or any(kept["stack"] is stack for kept in self.sessions.values()):
            return
        self.undo_group.removeStack(stack)
        stack.deleteLater()

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
        """A note about the board: into the log window (a warning or an error by ROLE, else information), once per
        text; the last one is kept in self.notice. Nothing is shown above the board any more."""
        self.notice.setText(text)
        self.notice.setProperty("role", role)
        if text and text != self.logged_notice:
            level = {"notice-error": "ERROR", "notice-warn": "WARNING"}.get(role, "INFO")
            applog.log(level, "board", text)
        self.logged_notice = text

    def restore(self, state):
        """The board as STATE says (a step of Undo or Redo): its parts updated where they are when only their
        properties or places changed - the usual step, which made the whole scene again before (a few ms each,
        with every item made anew) - else the scene made again."""
        self.restoring = True
        try:
            board = B.Board.from_state(state)
            same = bool(self.items) and {node.id for node in board.nodes} == set(self.items) and \
                sorted(board.edges) == sorted(getattr(edge, "ids", ()) for edge in self.edges)
            self.board = board
            if same:
                self.update_in_place()
            else:
                self.rebuild()
        finally:
            self.restoring = False

    def update_in_place(self):
        """The items of the parts (and their edges) follow self.board: places, texts, problems; the selection stays."""
        for node in self.board.nodes:
            item = self.items[node.id]
            if (item.pos().x(), item.pos().y()) != (node.x, node.y):
                item.setPos(node.x, node.y)  # (itemChange moves its edges)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-200, -200, 400, 400))
        self.refresh()
        self.inspect_timer.start()

    def change(self, text, function):
        """Apply FUNCTION to the model as one undo step."""
        before = self.board.state()
        try:
            function()
        except ValueError as exc:
            warn(self, tr("Board"), printable_text(exc))
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
                QTimer.singleShot(0, self, self.restyle)  # (with the page as its context: not run once it went)
        super().changeEvent(event)

    def restyle(self):
        self.restyle_queued = False
        if getattr(self, "restyle_pending", False):
            self.restyle_pending = False
            self.rebuild()
            self.view.resetCachedContent()  # the grid in the new theme's colours
            self.view.viewport().update()

    def rebuild(self):
        selected = self.inspected
        # A connection being dragged ends here: its port and its dashed line go with the scene's items (a Delete, a
        # new theme or another project meanwhile), and every later move of the mouse raised on the deleted port.
        self.scene.cancel_connect()
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
        """After a drag with the left button: the nodes it moved snap to the grid, kept as one undo step. Nodes it did
        not move stay where they are (off the grid of the setting, too): a click moves nothing."""
        grid = self.grid()
        was = {n["id"]: (n["x"], n["y"]) for n in before["nodes"]}
        for node in self.board.nodes:
            if was.get(node.id) != (node.x, node.y):
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
        if kind == "browser" and not props:
            props = {"--browser": "chromium"}  # on the board: a browser (for you, until you choose otherwise)
        if kind == "cairn" and not props:
            props = {"--cairn": True}  # on the board: Cairn's board, the session's agents its workers
        if kind == "team" and not props:  # the board's agent leads, one more member beside it (the least of a team)
            agent = self.board.first("agent")
            lead = agent.props.get("agent", "claude") if agent is not None else "claude"
            lead = lead if lead in backend.TEAM_AGENTS else "claude"
            props = {"name": "", "lead": lead, "members": [lead, "codex" if lead != "codex" else "claude"]}
        if kind == "bridge" and not props:  # the first session that runs here, messages (the least it can do)
            props = {"peer": self.running_sessions[0]["name"] if self.running_sessions else "", "direction": "out",
                     "message": True}
        if kind == "service" and not props:
            taken = {n.props.get("name") for n in self.board.of_kind("service")}
            name = next(f"service{i}" if i else "service" for i in range(100) if (f"service{i}" if i else "service")
                        not in taken)
            # The first of your images, else none yet (chosen in the guide or the inspector): never the part's own
            # name, which names no image of yours (the guide showed "service" as its Image).
            mine = sorted(self.user_image_names() or ())
            props = {**B.default_service(name), "image": mine[0] if mine else ""}

        added = []

        def add():
            node = self.board.add(kind, **props)
            if at is not None:
                node.x, node.y = B.snap(at.x() - NODE_W / 2, self.grid()), B.snap(at.y() - NODE_H / 2, self.grid())
            else:
                node.x, node.y = self.free_place(40.0 + B.KINDS[kind].column * 260.0, skip=node.id)
            self.inspected = node.id
            added.append(node.id)
        index = self.undo.index()
        self.change(tr("Add {name}", name=tr(B.KINDS[kind].title)), add)
        # Only the node just added (none when the board refused it: a second agent, say).
        if guided and added and self.settings().get("guide_nodes", True) and self.board.node(added[0]):
            if self.guide_node(added[0]) is False:
                # Cancel in the setup of a part just added: it was never added (user 2026-11-19) - its undo step
                # taken back and dropped (obsolete: no Redo of it left behind).
                self.drop_step(index)

    def drop_step(self, index):
        """The undo step pushed after INDEX taken back and removed from the stack (an add cancelled in its setup)."""
        if self.undo.index() != index + 1:
            return
        command = self.undo.command(index)
        self.undo.undo()  # the add taken back
        if command is not None:
            # (Qt runs neither undo() nor redo() of an obsolete step: redo() here only removes it from the stack)
            command.setObsolete(True)
            self.undo.redo()

    def free_place(self, x, skip=None):
        """(x, y) on the grid for a part added without a place of its own (the parts list's double click or Enter, a
        kit used): in the column at X, below the last part there that it would cover - never on another part. (It was
        40 + 110 * the number of parts, modulo 700: from the eighth part on, parts lay on each other, one hidden
        under the other until Arrange.)"""
        grid = self.grid()
        x = B.snap(x, grid)
        taken = []
        for node in self.board.nodes:
            if node.id == skip:
                continue
            item = self.items.get(node.id)
            height = item.rect().height() if item is not None else NODE_H
            taken.append(QRectF(node.x, node.y, NODE_W, height))
        gap = 24.0  # between two parts of a column (Arrange leaves 110 from top to top)
        y = B.snap(40.0, grid)
        while True:
            # (as tall as a part with three lines of text: the new one may grow once it is filled in)
            wanted = QRectF(x, y - gap / 2, NODE_W, NODE_H + 30.0 + gap)
            blocking = [rect for rect in taken if rect.intersects(wanted)]
            if not blocking:
                return x, y
            below = max(rect.bottom() for rect in blocking) + gap
            y = max(y + (grid if grid > 1 else 10.0), B.snap(below, grid) if grid > 1 else below)
            if grid > 1 and y < below:
                y += grid

    def guide_node(self, node_id):
        """The guide's dialog for node NODE_ID: its questions one by one, then what is still wrong with it. True: OK
        (the answers set, one undo step), False: cancelled (nothing set), None: no questions for that kind."""
        from . import guide, guide_view
        node = self.board.node(node_id)
        if node is None or not guide.questions(node.kind):
            return None  # no setup for it: nothing asked
        seen = self.intro_seen()
        dialog = guide_view.NodeGuide(node, self.problems, self, skip_intro=node.kind in seen)
        if self.state is not None and node.kind not in seen:  # its introduction shown: skipped from now on
            self.state.set("guide_intro_seen", sorted({*seen, node.kind}))
        if widgets.exec_dropped(dialog) == QDialog.DialogCode.Accepted:
            guide_view.apply(self, node_id, dialog.answers())
            return True
        return False  # cancelled: nothing set (the dialog works on a copy of the part)

    def intro_seen(self):
        """The kinds of part whose introduction the guide showed already (kept in the app's state): it starts at their
        first question then ("Show the introduction" in it goes back)."""
        seen = self.state.get("guide_intro_seen", []) if self.state is not None else []
        return {kind for kind in seen if isinstance(kind, str)} if isinstance(seen, list) else set()

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
            flags = item.flags() & ~movable if taken else item.flags() | movable
            if flags != item.flags():  # (set again, unchanged, it laid the whole list out and drew it again)
                item.setFlags(flags)
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
                                   else tr("Drop to add {name}", name=name), refused=taken, kind=value)
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
        chosen = widgets.exec_dropped(menu, global_point or self.view.viewport().mapToGlobal(self.view.viewport().rect().center()))
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
        if key == "project" and self.host() != LOCAL:
            # (this computer's project: the board starts on that host, where Open project folder… chooses)
            warn(self, tr("Board"), tr("{path} is this computer's, but the board starts on {host}: its folders and "
                                       "files are {host}'s. Open project folder… lists {host}'s folders.",
                                       path=printable(paths[0]), host=printable(self.host())))
            return None
        if key in ("use", "workspace", "mount-ro", "mount-rw") and self.host() != LOCAL:
            # (what was dropped is this computer's: no folder of the host the board starts on)
            warn(self, tr("Board"), tr("{path} is this computer's, but the board starts on {host}: its folders and "
                                       "files are {host}'s. Choose one with … in the part's properties (it lists "
                                       "{host}'s folders).", path=printable(paths[0]), host=printable(self.host())))
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
                warn(self, tr("Not imported"), printable_text(exc))
                return None
            if key == "preset-board":
                self.new_from_preset(preset)
            else:
                short_notice(self, tr("Board"), tr("Imported as your preset {name}: it is under My presets in the "
                                                   "gallery.", name=printable(preset.title)))
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

    def remove_node(self, node_id):
        """Remove the part NODE_ID (and its connections) as one step of Undo; the agent, the proxy and the network
        stay."""
        node = self.board.node(node_id)
        if node is None or B.KINDS[node.kind].required:
            return False
        if self.edit_node == node_id:
            self.edit_node = None
        self.change(tr("Remove"), lambda: self.board.remove(node_id))
        return self.board.node(node_id) is None

    def ask_remove(self, node_id):
        """The [x] of a part: asked first (Remove / Keep, Keep the default) unless the setting board_confirm_remove
        is off; then removed."""
        node = self.board.node(node_id)
        if node is None or B.KINDS[node.kind].required:
            return False
        ask = self.settings().get("board_confirm_remove", True) is not False
        if not ask_first(self, "board-remove-part", tr("Remove the part"),
                         tr("Remove {title} from the board? Undo brings it back.", title=printable(node.title())),
                         tr("Remove"), detail={"ask": ask}, reject=tr("Keep")):
            return False
        return self.remove_node(node_id)

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
            QTimer.singleShot(0, self, self.fit)  # (with the page as its context: not run once it went)

    def canvas_menu(self, point):
        """The context menu at the scene point POINT: a node's (Properties, Wizard…, What is this?, Cut,
        Copy, Paste, Remove), an edge's (Delete), else the canvas's (add a part there, paste there, select all, fit,
        zoom 100 %, arrange, undo, redo, what the board is). A node or edge right-clicked is selected first (alone,
        unless it already was)."""
        menu = QMenu(self.view)
        hit = None
        for item in self.scene.items(point):
            item = item.parentItem() if isinstance(item, (PortItem, NodeHelpMark, NodeCloseMark)) else item
            if isinstance(item, NodeItem) or (isinstance(item, EdgeItem) and item.near(point) and item.flags()
                                              & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable):
                hit = item
                break
        if hit is not None:
            if not hit.isSelected():
                self.scene.clearSelection()
                hit.setSelected(True)
            if isinstance(hit, EdgeItem):
                menu.addAction(tr("Delete"), self.delete_selected)
                return menu
            from . import guide
            node_id, kind = hit.node_id, hit.kind
            menu.setToolTipsVisible(True)
            menu.addAction(board_icons.icon(kind), tr("Properties"), lambda: self.show_properties(node_id))
            server = self.board.node(node_id).props if kind == "mcp" else {}
            if server.get("name") and not server.get("project"):  # the server's own (every start), on the MCP page
                action = menu.addAction(tr("The server's defaults (every start)…"),
                                        lambda name=str(server["name"]): self.server_properties.emit(name, self.host()))
                action.setToolTip(tr("The server's own limits, network, folders and variables for every start (the "
                                     "MCP page's Properties…)"))
            if guide.questions(kind):
                menu.addAction(tr("Wizard…"), lambda: self.guide_node(node_id))
            menu.addAction(tr("What is this?"), lambda: hit.help_mark.explain())
            # What a drag from its port does, for the keyboard too (the menu key opens this menu on the part chosen).
            connect, disconnect = self.connection_choices(node_id)
            for title, choices, act in ((tr("Connect with"), connect, self.connect_nodes),
                                        (tr("Disconnect from"), disconnect, self.disconnect_nodes)):
                if choices:
                    sub = menu.addMenu(title)
                    for other, edge in choices:
                        sub.addAction(board_icons.icon(other.kind), elide(other.title(), 60).replace("&", "&&"),
                                      lambda e=edge, a=act: a(*e))
            menu.addSeparator()
            removable = self.removable_selected()
            cut = menu.addAction(tr("Cut"), self.cut_selected)
            cut.setEnabled(bool(removable))
            menu.addAction(tr("Copy"), self.copy_selected)
            paste = menu.addAction(tr("Paste"), lambda: self.paste(point))
            paste.setEnabled(self.clipboard_parts() is not None)
            menu.addSeparator()
            remove = menu.addAction(tr("Remove"), self.delete_selected)
            remove.setEnabled(bool(removable))
            if not removable:
                for action in (cut, remove):
                    action.setToolTip(tr("The agent, the proxy and the network are always on the board"))
            return menu
        parts = menu.addMenu(tr("Add a part here"))
        for row in range(self.palette.count()):
            entry = self.palette.item(row)
            kind = entry.data(Qt.ItemDataRole.UserRole)
            action = parts.addAction(board_icons.icon(kind), entry.text(),
                                     lambda k=kind: self.add_node(k, guided=True, at=point))
            action.setEnabled(bool(entry.flags() & Qt.ItemFlag.ItemIsEnabled))
            action.setToolTip(safe_tooltip(tr(B.KINDS[kind].help)) if kind in B.KINDS else "")
        paste = menu.addAction(tr("Paste"), lambda: self.paste(point))
        paste.setEnabled(self.clipboard_parts() is not None)
        menu.addSeparator()
        menu.addAction(tr("Select all"), self.select_all)
        menu.addAction(tr("Fit the board"), self.fit)
        menu.addAction(tr("Zoom 100 %"), self.view.zoom_reset)
        menu.addAction(tr("Arrange"), self.arrange)
        menu.addSeparator()
        menu.addAction(tr("Save as preset…"), lambda: self.save_as_preset())
        menu.addAction(tr("Export…"), lambda: self.export_board())
        menu.addAction(tr("Import…"), lambda: self.import_board())
        state = menu.addAction(tr("Import a session with its state…"), lambda: self.import_session_state())
        state.setToolTip(tr("A session's archive (Export with state…, sandbox-ai session export) as a session of this "
                            "machine that Resume continues (sandbox-ai session import)"))
        discard = menu.addAction(tr("Discard the changes not applied"), lambda: self.discard_changes())
        discard.setEnabled(self.dirty())  # (the board of no project folder too: it is kept like a project's now)
        # The board of no project folder (a new one, another host's) kept with changes while a project's is shown:
        # the way back to it (there was none).
        if self.unnamed_kept():
            menu.addAction(tr("Show the board without a project folder"), lambda: self.show_unnamed())
        undo = menu.addAction(tr("Undo"), self.undo_group.undo)
        undo.setEnabled(self.undo_group.canUndo())
        redo = menu.addAction(tr("Redo"), self.undo_group.redo)
        redo.setEnabled(self.undo_group.canRedo())
        menu.addSeparator()
        global_point = self.view.viewport().mapToGlobal(self.view.mapFromScene(point))
        menu.addAction(tr("What is this?"), lambda: help_view.show_tip("board.canvas", self.view, global_point))
        return menu

    def show_properties(self, node_id):
        """Properties (a part's menu): that part alone selected, its inspector out (the strip or its window)."""
        item = self.items.get(node_id)
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)
        self.show_inspector()

    def removable_selected(self):
        return [i for i in self.scene.selectedItems() if isinstance(i, NodeItem) and not B.KINDS[i.kind].required]

    # -- the keyboard on the canvas (BoardView.keyPressEvent) --

    def select_towards(self, dx, dy):
        """An arrow key: the part next to the one chosen in the direction (DX, DY) is chosen instead (the nearest that
        way, one beside the line counting as farther); with none or several chosen, the agent. Brought into view.
        The part chosen, or None when there is none that way."""
        chosen = [i for i in self.scene.selectedItems() if isinstance(i, NodeItem)]
        if len(chosen) != 1:
            agent = self.board.first("agent")
            target = self.items.get(agent.id) if agent is not None else next(iter(self.items.values()), None)
        else:
            here, target, nearest = chosen[0].sceneBoundingRect().center(), None, None
            for item in self.items.values():
                there = item.sceneBoundingRect().center()
                along = (there.x() - here.x()) * dx + (there.y() - here.y()) * dy
                beside = abs((there.x() - here.x()) * dy) + abs((there.y() - here.y()) * dx)
                if item is chosen[0] or along <= 0:
                    continue
                distance = along + 2.0 * beside
                if nearest is None or distance < nearest:
                    target, nearest = item, distance
        if target is None:
            return None
        self.scene.clearSelection()
        target.setSelected(True)
        self.view.ensureVisible(target)
        return self.board.node(target.node_id)

    def nudge_selected(self, dx, dy):
        """Alt and an arrow key: the parts chosen move one step of the grid that way (one step of Undo). True if any
        moved."""
        chosen = [i for i in self.scene.selectedItems() if isinstance(i, NodeItem)]
        if not chosen:
            return False
        step = float(self.grid()) if self.grid() > 1 else 10.0
        before = self.board.state()
        for item in chosen:
            item.setPos(item.pos().x() + dx * step, item.pos().y() + dy * step)  # (node_moved: the model follows)
        self.moved(before)
        self.view.ensureVisible(chosen[0])
        return True

    def properties_by_keyboard(self):
        """Enter on the canvas: the one part chosen has its properties opened (as a double click does) and the
        keyboard put into their first field; OK or Cancel there bring it back to the canvas. False without one."""
        chosen = [i for i in self.scene.selectedItems() if isinstance(i, NodeItem)]
        if len(chosen) != 1:
            return False
        self.show_properties(chosen[0].node_id)

        def focus(left=40):
            if not alive(self) or self.inspect_timer.isActive():
                if alive(self) and left > 0:
                    QTimer.singleShot(50, self, lambda: focus(left - 1))
                return
            fields = [w for w in self.inspector_holder.findChildren(QWidget)
                      if w.isEnabled() and w.focusPolicy() & Qt.FocusPolicy.TabFocus and not w.focusProxy()
                      and isinstance(w, (QLineEdit, QComboBox, QCheckBox, QPlainTextEdit, QPushButton, QToolButton))]
            shown = [w for w in fields if w.isVisible()]
            if shown:
                if shown[0].window() is not self.view.window():  # (the properties float: a window of their own)
                    shown[0].window().activateWindow()
                shown[0].setFocus(Qt.FocusReason.TabFocusReason)
            elif fields and left > 0:  # (its panel unfolds, its rows are shown with the next round of events)
                QTimer.singleShot(50, self, lambda: focus(left - 1))
        QTimer.singleShot(0, self, focus)
        return True

    def disconnect_nodes(self, source, target):
        if (source, target) in self.board.edges:
            self.change(tr("Disconnect"), lambda: self.board.disconnect(source, target))

    def connection_choices(self, node_id):
        """([(part, edge)] the part NODE_ID can be connected with, [(part, edge)] it is connected with): for its
        menu's Connect with and Disconnect from - what a drag from its port does, without the mouse."""
        connect, disconnect = [], []
        for node in self.board.nodes:
            edge = (node_id, node.id) if self.board.can_connect(node_id, node.id) else \
                (node.id, node_id) if self.board.can_connect(node.id, node_id) else None
            if node.id == node_id or edge is None:
                continue
            (disconnect if edge in self.board.edges else connect).append((node, edge))
        return connect, disconnect

    # -- Copy, Cut, Paste: parts with their settings and the connections among them, on the system clipboard --

    def copy_selected(self, items=None):
        """The selected parts (or ITEMS) on the clipboard: their kinds, settings and places, and the connections
        between them (one to a part not copied is left out). Agent arguments that may be secrets and values that
        look like a credential are left out (a part names a secret, never holds it). The number of parts copied."""
        items = items if items is not None else [i for i in self.scene.selectedItems() if isinstance(i, NodeItem)]
        ids = {i.node_id for i in items}
        if not ids:
            return 0
        state = scrub_board({"nodes": [n for n in self.board.state()["nodes"] if n["id"] in ids]})
        parts = [{"id": n["id"], "kind": n["kind"], "props": scrub_values(n["props"]), "x": n["x"], "y": n["y"]}
                 for n in state["nodes"]]
        edges = sorted([a, b] for a, b in self.board.edges if a in ids and b in ids)
        data = json.dumps({"format": PARTS_FORMAT, "version": 1, "parts": parts, "edges": edges}).encode()
        mime = QMimeData()
        mime.setData(MIME_PARTS, data)
        mime.setText(data.decode())
        QApplication.clipboard().setMimeData(mime)
        return len(parts)

    def cut_selected(self):
        """Copy the selected parts, then remove those that may go (one step of Undo)."""
        items = self.removable_selected()
        if not items or not self.copy_selected(items):
            return
        ids = [i.node_id for i in items]

        def cut():
            for node_id in ids:
                self.board.remove(node_id)
        self.change(tr("Cut"), cut)

    @staticmethod
    def clipboard_parts():
        """The parts on the clipboard ({"parts", "edges"}, checked: known kinds, settings as a JSON object), or
        None. It may hold anything (another program, another board): read as untrusted data."""
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return None
        raw = bytes(mime.data(MIME_PARTS)) if mime.hasFormat(MIME_PARTS) else \
            (mime.text().encode() if mime.hasText() and PARTS_FORMAT in mime.text()[:200] else b"")
        if not raw or len(raw) > CLIP_LIMIT:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(data, dict) or data.get("format") != PARTS_FORMAT or not isinstance(data.get("parts"), list):
            return None
        parts = []
        for part in data["parts"][:64]:
            if not isinstance(part, dict) or part.get("kind") not in B.KINDS or not isinstance(part.get("props"), dict):
                continue
            x, y = part.get("x", 0), part.get("y", 0)
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or abs(x) > 1e6 or abs(y) > 1e6:
                x, y = 0.0, 0.0
            # (its number among the copied parts, for the connections: anything else names no part)
            ident = part.get("id") if type(part.get("id")) in (int, str) else None
            parts.append({"id": ident, "kind": part["kind"], "props": part["props"], "x": float(x), "y": float(y)})
        edges = [tuple(e) for e in (data.get("edges") if isinstance(data.get("edges"), list) else [])
                 if isinstance(e, list) and len(e) == 2 and all(type(end) in (int, str) for end in e)]
        return {"parts": parts, "edges": edges} if parts else None

    def paste(self, point=None):
        """The parts on the clipboard added (one step of Undo) with new ids and their connections among them: at
        the scene point POINT (the group's top left corner there), else beside where they were copied. A part a
        board has only once is refused when the board has it already (a message says so); a connection to it goes
        to the board's own (a workspace copied with the agent is connected to this board's agent). The new parts
        are selected."""
        clip = self.clipboard_parts()
        if clip is None:
            return []
        grid, parts = self.grid(), clip["parts"]
        left, top = min(p["x"] for p in parts), min(p["y"] for p in parts)
        dx, dy = (point.x() - left, point.y() - top) if point is not None else (40.0, 40.0)
        added, ends, refused = [], {}, []  # ends: the node each copied part's connections go to now
        # What another program put there (or another version of this app) is pasted only when a board can show and
        # use it (B.Board.part_problem, and its text on the board): a part that cannot is left out and said - it
        # raised in the slot before, some of the parts added already, the scene and Undo knowing nothing of them.
        agent = self.board.first("agent")
        unread = []
        for part in parts:
            problem = B.Board.part_problem(part["kind"], part["props"], self.sai, self.policy())
            if problem is None:
                try:
                    summary(B.Node(0, part["kind"], json.loads(json.dumps(part["props"]))),
                            agent.props.get("agent") if agent is not None else None)
                except Exception as exc:  # noqa: BLE001 - whatever a foreign part makes fail: not pasted
                    problem = f"{type(exc).__name__}: {exc}"
            if problem is not None:
                unread.append(part)
                applog.log("WARNING", "board", f"Paste: a part ({part['kind']}) on the clipboard was left out: "
                                               f"{printable_text(problem)[:300]}")
        parts = [part for part in parts if part not in unread]

        def add():
            for part in parts:
                kind = B.KINDS[part["kind"]]
                existing = self.board.first(kind.key) if kind.single else None
                if existing is not None:
                    refused.append(tr(kind.title))
                    if part["id"] is not None:
                        ends[part["id"]] = existing.id
                    continue
                node = self.board.add(kind.key, connect=False)
                node.props = json.loads(json.dumps(part["props"]))  # (never as Board.add's own arguments)
                node.x, node.y = B.snap(part["x"] + dx, grid), B.snap(part["y"] + dy, grid)
                added.append(node.id)
                if part["id"] is not None:
                    ends[part["id"]] = node.id
            for a, b in clip["edges"]:
                if a in ends and b in ends and (ends[a] in added or ends[b] in added) and \
                        self.board.can_connect(ends[a], ends[b]):
                    self.board.connect(ends[a], ends[b])
        if parts:
            self.change(tr("Paste"), add)
        if unread:
            warn(self, tr("Paste"), tr("Not pasted: {count} of the parts on the clipboard cannot be read as parts of "
                                       "this app (their settings are not what a part keeps). The log names them.",
                                       count=len(unread)))
        if refused:
            inform(self, tr("Paste"), tr("Not pasted: a board has only one of each of these parts, and this one has "
                                         "it already: {parts}.", parts=", ".join(refused)))
        new = [self.items[i] for i in added if i in self.items]
        if new:
            self.scene.clearSelection()
            for item in new:
                item.setSelected(True)
        return [i.node_id for i in new]

    def select_all(self):
        for item in self.scene.items():
            if item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable:
                item.setSelected(True)

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
        engine = self.board.first("engine")
        if self.user_images is None and (self.board.of_kind("service") or (engine is not None and
                                                                           engine.props.get("--image-variant"))):
            self.load_user_images()
        problems = B.problems(self.sai, self.board, self.policy(), self.user_image_names(),
                              session_logins=self.session_logins_known(), templates=self.template_names())
        missing = self.missing_folders()
        problems += B.missing_problems(missing)
        if getattr(self, "create_button", None) is not None:
            self.create_button.setVisible(bool(missing))
        score, badges = B.badges(self.board, self.policy(), bases=self.image_bases)
        self.board_findings = (problems, badges)
        if getattr(self, "launcher_state", None) != self.board.state():
            self.launcher_found = []  # (the launcher's problems of another board: its check of this one comes)
        if getattr(self, "tools_state", None) != self.board.state():
            self.tools_found = []  # (the MCP tools another board's model would see: this one's check comes)
        self.fill_findings()
        self.score.setText(tr("Security score: {score} of 100", score=score))
        self.score_button.setText(self.score.text())
        self.refresh_palette()
        command = B.command_line(self.sai, self.board)
        if command != self.preview.toPlainText():  # (the same text set again was laid out again, scrolled to the top)
            self.preview.setPlainText(command)
        self.problems = problems
        self.verdict.setText(tr("The launcher checks the options…"))
        self.check_timer.start(self.check_delay())

    def fill_findings(self):
        """The board's findings (B.problems, the badges) and what the launcher's last check of this very board refused
        about its MCP servers (B.launcher_problems: each problem on its own, on its part, before Start): the parts
        marked, the list filled."""
        problems, badges = getattr(self, "board_findings", ([], []))
        problems = [*problems, *getattr(self, "launcher_found", []), *getattr(self, "tools_found", []),
                    *self.team_findings()]
        levels, why = {}, {}
        for problem in problems:
            if problem.node is not None and levels.get(problem.node) != "error":
                levels[problem.node] = problem.level
        for problem in sorted(problems, key=lambda p: p.level != "error"):  # errors first
            if problem.node is not None:
                why.setdefault(problem.node, []).append(problem.text)
        lead = self.board.first("agent")
        agent = lead.props.get("agent", "claude") if lead is not None else None
        for node in self.board.nodes:
            if node.id in self.items:
                self.items[node.id].update_from(node, levels.get(node.id), self.board.connected(node),
                                                tuple(why.get(node.id, ())), agent=agent)
        # The findings (text, colour, part) as they would be listed: the list is filled again only when they differ -
        # every step of the board (a move, a property) filled it anew, the list laid out and drawn again each time.
        findings = [(("✘ " if problem.level == "error" else "! ") + problem.text,
                     theme.color("bad" if problem.level == "error" else "warn"), problem.node) for problem in problems]
        findings += [({"bad": "▼ ", "warn": "▽ ", "ok": "✔ ", "info": "· "}[badge.level] + badge.text,
                      theme.color(badge.level) if badge.level in ("bad", "warn", "ok") else None, badge.node)
                     for badge in badges]
        if findings != getattr(self, "findings_shown", None):
            self.findings_shown = findings
            self.findings.clear()
            for text, color, node_id in findings:
                item = QListWidgetItem(text)
                if color is not None:
                    item.setForeground(QBrush(QColor(color)))
                item.setData(Qt.ItemDataRole.UserRole, node_id)
                self.findings.addItem(item)

    def check(self):
        """The launcher's own verdict: a dry run of the board (no side effects), in the background. One runs at a time,
        here too (a Python process each; on another host a dry run over SSH, seconds each): an edit meanwhile asks for
        one more after it, with the board as it is then; an answer for an older board or another host is dropped
        (the token)."""
        agent, _, _ = B.values(self.board)
        host = self.host()
        if host != LOCAL:
            self.read_host_options(host, agent)
        self.check_team()  # (a board with a Team part: team start's own verdict too)
        if self.check_running:
            self.check_pending = True
            return
        self.check_token += 1
        token = self.check_token
        self.check_running, self.check_pending = True, False
        self.checking_state = self.board.state()  # (one check at a time: the board its verdict is about)
        background(self.sai.host_dry_run, host, agent, B.check_argv(self.sai, self.board, local=host == LOCAL),
                   timeout=120, what=f"the launcher's check of the board (a dry run of {agent} on {host})",
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
                   what=f"reading which options the sandbox-ai on {host} takes for {agent}",
                   done=lambda found, error: self.host_options_read(host, agent, found, error))

    def checked(self, token, result, error, host=None):
        self.check_running = False
        if self.check_pending:  # edited meanwhile: the verdict of the board as it is now
            self.check_pending = False
            self.check_timer.start(0)
            return
        if token != self.check_token or (host is not None and host != self.host()):
            applog.log("DEBUG", "board", "the launcher's verdict is dropped: it is about an older board or another "
                                         "host")
            return
        plan, message = result if error is None else (None, str(error))
        # A host that does not answer (ssh's own failure, nothing in time) is no launcher that refuses the options.
        silent = isinstance(error, backend.HostUnreachable)
        applog.changed(("board verdict", id(self)), "INFO", "board", "the launcher accepts the board's options"
                       if plan is not None else (f"{printable(host)} does not answer (the board is not checked): "
                                                 if silent else "the launcher refuses the board's options: ")
                       + applog.tail(message, lines=4, limit=600), quiet="DEBUG")
        # What it refused about the MCP servers, each problem in the findings (on its part) - all of them, before Start.
        found = B.launcher_problems(self.board, message) if plan is None and error is None else []
        # Any other refusal of this very board (a missing secret, an option the mode refuses...): Start would open a
        # terminal that only says it again. Kept to hold Start back once what the start needs is there
        # (start_refused), after one more check.
        refusal = None
        if plan is None and error is None and not found and (message or "").strip():
            last = [line.strip() for line in str(message).splitlines() if line.strip()][-1]
            refusal = printable(re.sub(r"^(?:agent-sandbox|sandbox-ai): ", "", last))[:800]
        if silent:  # (Start says it, after one more try - not a terminal tab that only shows ssh failing)
            refusal = tr("{host} does not answer: {message}", host=printable(host),
                         message=printable_text(" ".join((message or "").split()))[-600:])
        self.launcher_refusal = (refusal, getattr(self, "checking_state", None), host if host is not None
                                 else self.host())
        if found or getattr(self, "launcher_found", None):
            self.launcher_found, self.launcher_state = found, getattr(self, "checking_state", None)
            self.fill_findings()
        # More MCP tools than the model's class copes with (the dry run's "mcp_tools"): a finding with the servers to
        # leave out, on the LLM part - a warning, the start goes on.
        tools = B.tool_budget_problems(self.board, plan) if plan is not None else []
        if tools or getattr(self, "tools_found", None):
            self.tools_found, self.tools_state = tools, getattr(self, "checking_state", None)
            self.fill_findings()
        warned = B.path_warnings(B.launcher_warnings(message)) if plan is not None else ""
        if plan is not None and warned:
            # You decide what a sandbox gets: a risky folder is given, and the launcher says what it means.
            self.verdict.setText(tr("The launcher accepts these options, with a warning: {message}",
                                    message=" ".join(warned.split())[:400]),
                                 whole=tr("The launcher accepts these options, with a warning: {message}",
                                          message=warned[-20000:]))
            self.verdict.setProperty("role", "notice-warn")
        elif plan is not None:
            self.verdict.setText(tr("The launcher accepts these options."))
            self.verdict.setProperty("role", "notice-ok")
        elif silent:
            whole = printable_text((message or "").strip())
            self.verdict.setText(tr("{host} does not answer: {message}", host=printable(host),
                                    message=" ".join(whole[-400:].split())),
                                 whole=tr("{host} does not answer: {message}", host=printable(host),
                                          message=whole[-20000:]))
            self.verdict.setProperty("role", "notice-error")
        else:
            # One line here (cut to the room it has); its lines whole in the tooltip and the window of the whole text.
            whole = printable_text((message or "").strip())
            said = " ".join(whole[-400:].split())
            self.verdict.setText(tr("The launcher refuses: {message}", message=said),
                                 whole=tr("The launcher refuses: {message}", message=whole[-20000:]))
            self.verdict.setProperty("role", "notice-error")
        self.offered_keys = B.secret_offers(message) if plan is None else []
        self.secret_state = getattr(self, "checking_state", None) if self.offered_keys else None
        if self.offered_keys:
            self.secret_button.setText(tr("Keep the key {name}…", name=self.offered_keys[0]))
            self.secret_button.setToolTip(safe_tooltip(tr(
                "The start needs the secret {name}, which is not kept yet: type its value into a password field and "
                "keep it (sandbox-ai secrets set {name}; the value goes on the command's standard input, is never "
                "shown or logged), then the board is checked again", name=self.offered_keys[0])))
        self.secret_button.setVisible(bool(self.offered_keys))
        self.build_commands = B.build_offers(message) if plan is None else []
        self.build_button.setVisible(bool(self.build_commands))
        offered = BYPASS_OFFER.search(message or "") if plan is None else None
        self.bypass_target = offered.group(1) if offered else None
        self.bypass_state = getattr(self, "checking_state", None) if offered else None
        if offered:
            self.bypass_button.setText(tr("Reach {target} past Tor…", target=self.bypass_target))
        self.bypass_button.setVisible(bool(offered))
        self.verdict.style().unpolish(self.verdict)
        self.verdict.style().polish(self.verdict)
        self.launcher_ok = plan is not None
        then, self.after_check = getattr(self, "after_check", None), None
        if then is not None:  # a Start that waited for this verdict (start: checked again first)
            then()

    def finding_clicked(self, item):
        node_id = item.data(Qt.ItemDataRole.UserRole)
        if node_id in self.items:
            # A finding about an option folded under "More options": the section opens and the keyboard goes to the
            # option's field once the part's properties are shown (focus_more).
            node = self.board.node(node_id)
            pointed = self.more_mentioned(node, item.text()) if node is not None else []
            if pointed:
                self.more_open[node.kind] = True
                self.more_focus = (node_id, pointed[0])
            self.scene.clearSelection()
            self.items[node_id].setSelected(True)
            self.view.centerOn(self.items[node_id])
            if pointed:
                self.inspect_timer.start()  # (also when the part was selected already)
                self.show_inspector()

    # -- the properties' "More options": the rows that seldom need to be set, folded --

    @staticmethod
    def more_options(kind, shown):
        """The options of a part of KIND (SHOWN: those with a row, in order) folded under "More options": those after
        MORE_AFTER's option of the kind; none when nothing follows it."""
        anchor = MORE_AFTER.get(kind)
        if anchor not in shown:
            return []
        return shown[shown.index(anchor) + 1:]

    def more_mentioned(self, node, text):
        """What under NODE's "More options" TEXT (a finding, the launcher's refusal) is about: its options named as
        such (--env) or by their labels; the Bridge's limits by their names."""
        text = str(text)
        if node.kind == "bridge":
            from . import bridges as BR
            return ["limits"] if any(tr(title) in text for _key, title, _default, _tip in BR.LIMITS) else []
        options = B._options(self.sai)
        keys = self.more_options(node.kind, [o for o in B.KINDS[node.kind].options if o in options])
        named = set(FLAG_MENTION.findall(text))
        return [key for key in keys if key in named or OL.label(key) in text]

    def begin_more(self, node, count):
        """The header of NODE's "More options" (the rows added until end_more fold under it): a disclosure that
        says how many of them are set (COUNT); open or folded as last chosen for the kind (folded at first), or
        open when an error is about one of them (once: folded again by the user, it stays so)."""
        kind = node.kind
        for problem in self.more_problems(node):
            if (kind, problem) not in self.more_auto and self.more_mentioned(node, problem):
                self.more_auto.add((kind, problem))
                self.more_open[kind] = True
        header = disclosure(MoreHeader())
        text = tr("More options") + (f" ({tr('{count} set', count=count)})" if count else "")
        header.setText(text)
        header.setAccessibleName(text)
        header.setAccessibleDescription(tr("Options that seldom need to be set; Enter or Space shows or folds them"))
        header.setToolTip(safe_tooltip(tr("Options that seldom need to be set: shown or folded away (kept for this "
                                          "kind of part while the app runs)")))
        header.setChecked(bool(self.more_open.get(kind)))
        header.toggled.connect(lambda on, k=kind: self.show_more(k, on))
        self.inspector.addRow(header)
        self.more_header = header
        self.more_section = {"start": self.inspector.rowCount(), "end": None, "own": set()}

    def more_problems(self, node):
        """The errors about NODE now: the board's, the launcher's problems and its refusal of this very board."""
        problems, _badges = getattr(self, "board_findings", ([], []))
        texts = [p.text for p in (*problems, *getattr(self, "launcher_found", []))
                 if p.node == node.id and p.level == "error"]
        refusal = getattr(self, "launcher_refusal", None)
        if refusal and refusal[0] and refusal[1] == self.board.state():
            texts.append(refusal[0])
        return texts

    def more_widgets(self):
        """The labels and fields of the rows under "More options"."""
        section, found = self.more_section, []
        end = section["end"] if section["end"] is not None else self.inspector.rowCount()
        for row in range(section["start"], end):
            for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole,
                         QFormLayout.ItemRole.SpanningRole):
                item = self.inspector.itemAt(row, role)
                if item is not None and item.widget() is not None:
                    found.append(item.widget())
        return found

    def end_more(self):
        """The rows under "More options" end here: shown or folded as its header says. (A row hidden by its own
        logic - a note shown only for some values - stays hidden when the section opens.)"""
        section = self.more_section
        if section is None or section["end"] is not None:
            return
        section["end"] = self.inspector.rowCount()
        section["own"] = {id(w) for w in self.more_widgets() if hidden_by_itself(w)}
        self.apply_more(self.more_header.isChecked())

    def apply_more(self, opened):
        section = self.more_section
        if section is None or section["end"] is None:
            return
        found = self.more_widgets()
        if not opened:  # (what is hidden while it is open is hidden by its own logic)
            section["own"] = {id(w) for w in found if hidden_by_itself(w)}
        for widget in found:
            if id(widget) not in section["own"]:
                widget.setVisible(bool(opened))

    def show_more(self, kind, opened):
        """The header was clicked (or Enter, Space): the rows shown or folded, kept for KIND."""
        self.more_open[kind] = bool(opened)
        self.apply_more(opened)

    def focus_more(self, node):
        """After a finding pointed into "More options" (finding_clicked): the keyboard in that option's field (the
        Bridge: its first limit), scrolled into view."""
        # (kept until the field has the keyboard: the inspector may be made anew once more meanwhile - the checks'
        # answer arriving - and the field asked for is then the new one's)
        target = self.more_focus
        if target is not None and (node is None or target[0] != node.id
                                   or time.monotonic() > getattr(self, "more_focus_until", float("inf"))):
            self.more_focus, self.more_focus_until = None, float("inf")  # another part shown, or long done: over
            return
        if target is None or self.more_section is None:
            return
        widget = self.option_fields.get(target[1])
        if isinstance(widget, LF.ListFields):
            widget.choose_kind(target[1])  # (the option the finding is about, chosen to be added to)
        if widget is None:
            fields = [w for w in self.more_widgets() if not isinstance(w, QLabel)]
            widget = fields[0] if fields else None
        if widget is None:
            return
        focusable = widget if widget.focusPolicy() != Qt.FocusPolicy.NoFocus else next(
            (w for w in widget.findChildren(QWidget) if w.focusPolicy() != Qt.FocusPolicy.NoFocus), widget)

        def focus(left=20):  # (once the rows just made are shown: a hidden field takes no focus)
            if self.more_focus != target:
                return  # another finding, or done
            if alive(widget) and alive(focusable) and widget.isVisible():
                self.inspector_scroll.ensureWidgetVisible(widget)
                focusable.setFocus(Qt.FocusReason.OtherFocusReason)
                # (kept a moment: an inspector made anew right after - the checks' answer - takes the keyboard there
                # again instead of where it was before the jump)
                if getattr(self, "more_focus_until", float("inf")) == float("inf"):
                    self.more_focus_until = time.monotonic() + MORE_FOCUS_KEPT
            elif left > 0 and alive(widget):
                QTimer.singleShot(50, self, lambda: focus(left - 1))
            elif not alive(widget):
                return  # the inspector was made anew: its focus_more takes the target
            else:
                self.more_focus, self.more_focus_until = None, float("inf")
        QTimer.singleShot(0, self, focus)

    # -- the inspector --

    def selection_changed(self):
        if self.restoring:
            return
        try:
            selected = self.scene.selectedItems()
        except RuntimeError:  # the scene is being destroyed: the app quits
            return
        nodes = [i for i in selected if isinstance(i, NodeItem)]
        inspected = nodes[0].node_id if len(nodes) == 1 else None
        if inspected != self.inspected:
            self.commit_lists()  # a list typed into a moment ago belongs to the part shown until now
            self.commit_list_fields()  # and so does a value typed into a list field and not added yet
            self.commit_field()  # and so does a field the keyboard is still in
        self.inspected = inspected
        self.inspect_timer.start()  # an open inspector follows the selection; a double click opens it
        if not selected:  # a click on the empty canvas: its docked panel folds
            self.fold_inspector()

    def show_inspector(self):
        """A part was selected or clicked: the inspector's panel is shown (closed), unfolded (folded) or raised
        (floating, where it was) - the keyboard stays on the canvas."""
        if self.inspector_request is not None:
            self.inspector_request()

    def veil_also(self):
        """What a dialog of the Board veils besides the page: its panels (in docks of the window)."""
        return [self.parts_panel, self.side, self.findings_panel]

    def fold_inspector(self):
        """Esc or a click on the empty canvas: the inspector's panel folds to its strip if it is docked and unfolded
        (a floating one stays as it is). True if it folded."""
        return bool(self.fold_request is not None and self.fold_request())

    def show_findings(self, shown):
        """The score's button: the score and the findings shown (SHOWN) or hidden; in the window, its panel."""
        if self.score_request is not None:
            self.score_request(shown)
        else:
            self.findings_panel.setVisible(shown)
            self.score_button.setChecked(shown)

    def clear_inspector(self):
        for field in self.inspector_holder.findChildren(LF.ListFields):
            field.release()  # (its going takes nothing typed: the rows made anew keep it, restore_focus)
        while self.inspector.rowCount():
            self.inspector.removeRow(0)

    def focus_memo(self):
        """Where the keyboard is in the inspector, to bring it back after the inspector is made again (a list field
        takes what was typed after a pause, and the change makes the rows anew): (row, role, index of the widget of
        its kind in that row's widget, the text as typed, the cursor, the field's option, whether the text was typed
        and not taken yet), or None when the keyboard is elsewhere."""
        focused = QApplication.focusWidget()
        if focused is None or not self.inspector_holder.isAncestorOf(focused):
            return None
        for row in range(self.inspector.rowCount()):
            for role in (QFormLayout.ItemRole.FieldRole, QFormLayout.ItemRole.SpanningRole,
                         QFormLayout.ItemRole.LabelRole):
                item = self.inspector.itemAt(row, role)
                holder = item.widget() if item is not None else None
                if holder is None or not (holder is focused or holder.isAncestorOf(focused)):
                    continue
                kind = type(focused)
                index = -1 if holder is focused else holder.findChildren(kind).index(focused)
                typed = False  # typed by the human and not taken yet (a line is taken when the field is left)
                if isinstance(focused, QPlainTextEdit):
                    text, cursor = focused.toPlainText(), focused.textCursor().position()
                elif isinstance(focused, QLineEdit):
                    text, cursor, typed = focused.text(), focused.cursorPosition(), self.typed_in(focused)
                elif isinstance(focused, QComboBox) and focused.isEditable() and focused.lineEdit() is not None:
                    # (a name field - a VPN profile, an image, a variant: the keyboard is the box's)
                    text, cursor = focused.currentText(), focused.lineEdit().cursorPosition()
                    typed = self.typed_in(focused.lineEdit())
                else:
                    text, cursor = None, None
                # (an option's field also by its option: a row that comes later, e.g. once the model servers are read,
                # moves the rows below it)
                option = next((key for key, field in self.option_fields.items()
                               if alive(field) and (field is focused or field.isAncestorOf(focused))), None)
                return row, role, kind, index, text, cursor, option, typed
        return None

    @staticmethod
    def typed_in(edit):
        """Whether the line EDIT of the inspector holds text the human typed that is not taken yet: edited, and not
        what the field showed when it was made (note_shown). A text taken with Enter is the model's again - the
        inspector is made anew with it -, and one typed and deleted again is no text typed: both follow the model
        (an Undo of that very value must show in the field the keyboard is in)."""
        shown = edit.property("inspected_text")
        return edit.isModified() and (not isinstance(shown, str) or edit.text() != shown)

    def note_shown(self):
        """Each line of the inspector remembers the text it was made with (typed_in compares with it)."""
        for edit in self.inspector_holder.findChildren(QLineEdit):
            edit.setProperty("inspected_text", edit.text())

    def restore_focus(self, memo):
        """The keyboard back in the field it was in (focus_memo), with the cursor where it was and the text as typed:
        when only the committed form differs (a list's last empty line, spaces at the ends), and when it was typed
        into a line and not taken yet. The inspector is made anew by every late answer too (a host's model servers,
        images, sessions and logins, a team's verdict - seconds after the part was opened on another host): what
        was being typed then was dropped, the cursor left in a field showing the old value."""
        if memo is None:
            return
        row, role, kind, index, text, cursor, option, typed = memo
        field = self.option_fields.get(option) if option else None
        if field is not None:  # the option's field wherever its row is now
            found = [] if isinstance(field, kind) else field.findChildren(kind)
            # (the index of its kind there: a list field's chip - after one was removed, the one in its place)
            widget = field if isinstance(field, kind) else (found[min(index, len(found) - 1)] if found and index > 0
                                                             else next(iter(found), None))
        else:
            if row >= self.inspector.rowCount():
                return
            item = self.inspector.itemAt(row, role)
            holder = item.widget() if item is not None else None
            if holder is None:
                return
            if index < 0:
                widget = holder if isinstance(holder, kind) else None
            else:
                found = holder.findChildren(kind)
                widget = found[index] if index < len(found) else None
        if widget is None or not widget.isEnabled():
            return
        if text is not None and isinstance(widget, QPlainTextEdit):
            if widget.toPlainText() != text and widget.toPlainText().split() == text.split():
                widget.blockSignals(True)
                widget.setPlainText(text)
                widget.blockSignals(False)
            moved = widget.textCursor()
            moved.setPosition(min(cursor, len(widget.toPlainText())))
            widget.setTextCursor(moved)
        elif text is not None and isinstance(widget, QLineEdit):
            if widget.text() != text and (typed or widget.text().strip() == text.strip()):
                widget.blockSignals(True)
                widget.setText(text)
                widget.blockSignals(False)
                widget.setModified(typed)  # (still typed, not taken: leaving the field takes it)
            widget.setCursorPosition(min(cursor, len(widget.text())))
        elif text is not None and isinstance(widget, QComboBox) and widget.isEditable() and \
                widget.lineEdit() is not None:
            if widget.currentText() != text and typed:
                widget.blockSignals(True)
                widget.setEditText(text)
                widget.blockSignals(False)
                widget.lineEdit().setModified(True)
            widget.lineEdit().setCursorPosition(min(cursor, len(widget.currentText())))

        def focus(left=20):
            # (a row just made is shown with the next round of events, and a hidden widget takes no keyboard: under
            # load the inspector made anew right after a finding's jump left the keyboard nowhere)
            elsewhere = QApplication.focusWidget()
            # (a field of the inspector just replaced may still hold the keyboard until it is deleted: hidden, it
            # is no "somewhere else")
            if not alive(widget) or (elsewhere is not None and elsewhere is not widget and elsewhere.isVisible()
                                     and not self.inspector_holder.isAncestorOf(elsewhere)):
                return  # (gone, or the keyboard went somewhere else meanwhile: left there)
            widget.setFocus(Qt.FocusReason.OtherFocusReason)  # (a widget not shown yet takes it once it is)
            if not widget.isVisible() and left > 0:
                QTimer.singleShot(50, self, lambda: focus(left - 1))
        focus()

    def inspect(self, node):
        if node is not None and node.id == self.edit_node and not self.restoring:
            # The same part's properties made anew: a list typed a moment ago is taken first (it is taken 600 ms after
            # the typing stops; its field goes with the rows now). The change makes the inspector anew once more.
            self.commit_lists()
            node = self.board.node(node.id) or node
        memo = self.focus_memo() if node is not None and node.id == self.edit_node else None
        self.clear_inspector()
        self.more_header, self.more_section, self.option_fields = None, None, {}
        self.live_token = getattr(self, "live_token", 0) + 1  # a live row still being looked up is for another look
        if node is None:
            self.edit_node = None
            self.inspector_title.setText(tr("Double-click a part to change it"))
            self.inspector_icon.hide()
            self.inspector_help.set_tip("board.inspector")
            return
        if node.id != self.edit_node or self.undo is not getattr(self, "edit_stack", None):
            # another part's properties (or another project's board): what Cancel goes back to starts now
            self.edit_node, self.edit_index, self.edit_stack = node.id, self.undo.index(), self.undo
        try:
            self.inspect_rows(node)
            self.end_more()
        except Exception as exc:  # noqa: BLE001 - said in its place, with Remove: never an exception in the slot
            # A part whose settings the inspector cannot show (a draft or a file of another version, edited by hand):
            # the rows made so far were left standing under the part's title, and every later look raised again.
            import traceback
            applog.log("ERROR", "board", f"the properties of {printable(node.kind)} cannot be shown: "
                       f"{applog.error_text(exc)}\n" + applog.redact("".join(traceback.format_exception(exc))[-3000:]))
            self.clear_inspector()
            self.more_header, self.more_section, self.option_fields = None, None, {}
            self.inspector.addRow(plain_label(tr(
                "These properties cannot be shown: the part holds settings this app does not keep there ({error}). "
                "Remove the part (Undo brings it back) and add it anew; the log window has the details.",
                error=printable_text(f"{type(exc).__name__}: {exc}")[:200]), role="notice-error", selectable=True))
        self.edit_buttons(node)
        self.note_shown()
        self.restore_focus(memo)
        self.focus_more(node)

    def edit_buttons(self, node):
        """OK (keep the changes made here), Cancel (back to the part as it was when its properties opened: those
        steps undone) and Remove (the part, at once: Undo brings it back)."""
        changed = self.undo.index() != self.edit_index
        box = QDialogButtonBox()
        ok = box.addButton(tr("OK"), QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = box.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        ok.setToolTip(tr("Keep the changes and close the properties"))
        cancel.setToolTip(tr("Undo the changes made since the properties were opened"))
        cancel.setEnabled(changed)
        ok.clicked.connect(lambda _=False: self.edit_done(True))
        cancel.clicked.connect(lambda _=False: self.edit_done(False))
        row = QHBoxLayout()
        from . import guide
        if guide.questions(node.kind):  # bottom left, named as everyone knows it: the guide for this part
            helper = QPushButton(tr("Wizard"))
            helper.setToolTip(tr("The guide asks what matters for this part, one question at a time"))
            helper.clicked.connect(lambda _=False, node_id=node.id: self.guide_node(node_id))
            row.addWidget(helper)
        if not B.KINDS[node.kind].required:
            remove = QPushButton(tr("Remove"))
            remove.setToolTip(tr("Remove this part from the board (Undo brings it back)"))
            remove.clicked.connect(lambda _=False, node_id=node.id: self.remove_node(node_id))
            row.addWidget(remove)
        row.addStretch(1)
        row.addWidget(box)
        holder = QWidget()
        holder.setLayout(row)
        self.inspector.addRow(holder)
        self.edit_box = box

    def edit_done(self, keep):
        """OK (KEEP) or Cancel of the inspector: Cancel undoes what was changed since the part's properties opened;
        both close (fold) the properties."""
        if not keep and self.undo is getattr(self, "edit_stack", None) and self.undo.index() != self.edit_index:
            self.undo.setIndex(self.edit_index)
        self.edit_index = self.undo.index()
        focused = QApplication.focusWidget()
        if focused is not None and self.inspector_holder.isAncestorOf(focused):
            if focused.window() is not self.view.window():  # (the properties float: back to the app's window)
                self.view.window().activateWindow()
            self.view.setFocus(Qt.FocusReason.OtherFocusReason)  # (not left in a panel that folds or closes)
        if self.close_request is not None:
            self.close_request()
        else:
            self.fold_inspector()
        self.inspect_timer.start()

    def inspect_rows(self, node):
        self.inspector_title.setText(node.title())
        self.inspector_icon.setPixmap(board_icons.pixmap(node.kind, board_icons.SIZE, theme.colors()["title"],
                                                         self.inspector_icon.devicePixelRatioF()))
        self.inspector_icon.show()
        self.inspector_help.set_tip(f"kind.{node.kind}")
        node_id = node.id
        kind = node.kind
        if kind == "agent":
            agent = QComboBox()
            agent.setAccessibleName(tr("Agent"))
            fill_agent_combo(agent, self.sai, node.props.get("agent", "claude"))
            # An agent not live-tested yet says so below the choice (a badge only: it starts like any other).
            experimental = plain_label(experimental_note(self.sai, combo_agent(agent)), role="secondary")

            def agent_chosen(_index, combo=agent, note=experimental):
                chosen = combo_agent(combo)
                note.setText(experimental_note(self.sai, chosen))
                note.setVisible(bool(note.text()))
                node_ = self.board.node(node_id)
                if node_ is not None and node_.props.get("agent") != chosen:
                    # (what only the agent before took goes along: one step of Undo)
                    self.change(tr("Change {name}", name=tr("Agent")), lambda: B.switch_agent(node_.props, chosen))
            agent.currentIndexChanged.connect(agent_chosen)
            self.inspector.addRow(tr("Agent"), agent)
            self.inspector.addRow("", experimental)
            experimental.setVisible(bool(experimental.text()))  # (in its place first: never a window of its own)
            self.llm_server_row(node)
            self.use_case_row()
            self.model_row(node)
            self.effort_row(node)
            self.thinking_row(node)
            self.agent_llm_rows()
            self.text_row(node, "arguments", tr("Agent arguments"), tr("e.g. --verbose"))
        elif kind == "workspace":
            path = PathEdit(node.props.get("path", ""), host=self.host(), other_machine=True)
            path.fallback = lambda text: self.set_prop(node_id, "path", text.strip())
            path.changed.connect(path.fallback)
            self.inspector.addRow(tr("Folder"), path)
            self.remote_note_row(node.props.get("path", ""), bool(node.props.get("readonly")))
            readonly = QCheckBox(OL.shown(tr("every workspace read-only (--workspace-ro)")))
            readonly.setChecked(bool(node.props.get("readonly")))
            readonly.toggled.connect(lambda on: self.set_readonly(on))
            self.disable_if_forbidden(readonly, "--workspace-ro")
            self.inspector.addRow("", readonly)
        elif kind == "mount":
            source = PathEdit(node.props.get("source", ""), host=self.host(), other_machine=True)
            source.fallback = lambda text: self.set_prop(node_id, "source", text.strip())
            source.changed.connect(source.fallback)
            self.inspector.addRow(tr("Host folder or file"), source)
            self.remote_note_row(node.props.get("source", ""), node.props.get("mode", "ro") != "rw")
            self.text_row(node, "dest", tr("In the sandbox"), "/mnt/data")
            mode = QComboBox()
            for text, key in ((tr("read-only"), "ro"), (tr("writable"), "rw")):
                mode.addItem(text, key)
            mode.setCurrentIndex(max(0, mode.findData(node.props.get("mode", "ro"))))
            mode.currentIndexChanged.connect(lambda _: self.set_prop(node_id, "mode", mode.currentData()))
            self.inspector.addRow(tr("Mode"), mode)
        elif kind == "mcp":
            name = self.mcp_chooser(node)
            self.inspector.addRow(tr("Server"), name)
            self.disable_if_forbidden(name, "--mcp")
            definition = node.props.get("definition")  # the project's own server
            if not isinstance(definition, dict) and self.host() != LOCAL:
                definition = None  # (another host's server: defined there, not in this machine's mcp.json)
            elif not isinstance(definition, dict):
                try:
                    definition = self.sai.mcp_definitions().get(node.props.get("name", ""))
                except Exception as _exc:  # noqa: BLE001 - an MCP file that does not load: said in the row
                    applog.swallowed("board_view", "BoardPage.inspect_rows: an MCP file that does not load: sai…", _exc)
                    definition = None
            for label, value in B.mcp_summary(definition):
                self.inspector.addRow(label, plain_label(value))
            self.mcp_limit_rows(node, definition)
        elif kind == "proxy":
            enabled = QCheckBox(OL.shown(tr("on (every connection passes it; off: --no-proxy)")))
            enabled.setChecked(node.props.get("enabled", True) is not False)
            enabled.toggled.connect(lambda on: self.set_prop(node_id, "enabled", bool(on)))
            if None in self.forbidden.get("--no-proxy", set()):
                enabled.setChecked(True)
                enabled.setEnabled(False)
                enabled.setToolTip(tr("The administrator's policy forbids --no-proxy"))
            self.inspector.addRow(tr("Connection proxy"), enabled)
            rules = RulesEdit(node.props.get("--allow-http", []))
            rules.changed.connect(lambda values: self.set_prop(node_id, "--allow-http", values))
            self.inspector.addRow(OL.shown(tr("Requests per host (--allow-http)")), rules)
        elif kind == "service":
            self.service_rows(node)
        elif kind == "bridge":
            self.bridge_rows(node)
        elif kind == "team":
            self.team_rows(node)
        elif kind == "kits":
            self.kit_rows(node)
        elif kind == "home":
            self.login_rows(node)
        elif kind == "approvals" and isinstance(node.props.get("rules"), dict):
            rules = QPlainTextEdit(json.dumps(node.props["rules"], indent=1))
            rules.setReadOnly(True)
            rules.setFont(MONO)
            rules.setMaximumHeight(180)
            rules.setToolTip(tr("The project's ruleset, written into .sandbox-ai.json (key rules)"))
            self.inspector.addRow(tr("Project rules"), rules)
        options = B._options(self.sai)
        shown = [option for option in B.KINDS[kind].options
                 if option not in ("--allow-http", "--no-llm-profile", "--use-case") and option in options
                 # (the part HOME and login: its "Login from" sets these, login_rows)
                 and not (kind == "home" and option in ("--no-auth", "--auth-file", "--auth-from-session"))
                 # (--no-llm-profile: the model profile's own switch, llm_profile_rows; --use-case: its choice)
                 and not (option == "--kit" and kind == "kits" and self.kit_inventory)]  # (kits: by name above)
        tail = self.more_options(kind, shown)
        title, grouped = LIST_GROUPS.get(kind, ("", ()))
        grouped = [option for option in shown if option in grouped]
        for option in shown:
            if tail and option == tail[0]:  # what follows seldom needs to be set: folded under "More options"
                self.begin_more(node, sum(B.prop_set(node.props.get(o)) for o in tail))
            if option in grouped:  # the part's list options: one field where the first of them was
                if option == grouped[0]:
                    self.list_fields_row(node, title, grouped, options)
                continue
            self.option_row(node, options[option])
            if option == "--ask":
                self.ask_note()
        self.end_more()
        if kind == "devices" and isinstance(self.option_fields.get("--device"), LF.ListFields):
            again = QPushButton(tr("Detect devices again"))
            again.setAutoDefault(False)
            again.setToolTip(safe_tooltip(tr("Read the devices of {host} again (a device plugged in since)",
                                             host=DP.host_name(self.host()))))
            again.clicked.connect(lambda _=False: self.refresh_devices())
            self.inspector.addRow("", again)
        if kind in ("devices", "limits"):
            self.live_rows(node)
        if kind == "engine":
            self.engine_rows(node)
        if kind == "llm":
            self.use_case_row()
            self.llm_profile_rows(node)
            self.llm_param_rows(node)

    # -- the part HOME and login: where the start's login comes from --

    SESSION_LOGINS_EVERY = 30  # seconds between two reads of a host's sessions' logins

    def session_logins_known(self):
        """The logins of the board's host's sessions as last read, or None (not read yet); reads them when the board
        takes a login from a session."""
        host = self.host()
        home = self.board.first("home")
        if home is not None and B.login_choice(home.props) == "session":
            self.load_session_logins(host)
        return self.session_login_data.get(host)

    def load_session_logins(self, host, now=False):
        """HOST's sessions' logins (`sandbox-ai login --status --sessions --json` there), in the background; not again
        while a read runs or one ended less than SESSION_LOGINS_EVERY seconds ago (unless NOW)."""
        last = self.session_login_when.get(host)
        if host in self.session_login_asking or (not now and last is not None and
                                                 time.monotonic() - last < self.SESSION_LOGINS_EVERY):
            return
        self.session_login_asking.add(host)
        self.session_login_when[host] = time.monotonic()
        background(lambda: self.sai.session_logins(host), owner=self, timeout=150,
                   done=lambda result, error: self.session_logins_read(host, result, error))

    def session_logins_read(self, host, result, error):
        self.session_login_asking.discard(host)
        if not alive(self):
            return
        if error is not None:
            applog.log("INFO", "board_view", f"the logins of the sessions of {printable(host)} were not read: "
                                             f"{printable_text(str(error))[:300]}")
            return
        before = self.session_login_data.get(host)
        self.session_login_data[host] = result
        if host != self.host() or before == result:
            return
        if self.inspected in self.items and self.board.node(self.inspected).kind == "home":
            self.inspect_timer.start()  # "Login from" offers them now
        home = self.board.first("home")
        if home is not None and B.login_choice(home.props) == "session":
            self.refresh()  # its checks know them now

    def central_login(self, agent):
        """True: a central login of AGENT is stored on the board's host; False: none; None: not known yet."""
        data = self.session_login_data.get(self.host())
        if not isinstance(data, dict) or not isinstance(data.get("central"), dict):
            return None
        return isinstance(data["central"].get(agent), dict)

    def login_sessions(self, agent):
        """[row] of the board's host's sessions with a usable login of AGENT (login --status --sessions)."""
        data = self.session_login_data.get(self.host())
        rows = data.get("sessions") if isinstance(data, dict) else None
        return [row for row in rows or [] if isinstance(row, dict) and row.get("agent") == agent and row.get("login")]

    def login_rows(self, node):
        """"Login from" of the part HOME and login: the central login (the default when one is stored), this
        computer's, a session's (with the session to choose, the rotating-token warning and Import as central login
        instead), a login file, none."""
        from .session_logins_view import account_text
        node_id = node.id
        self.load_session_logins(self.host())
        agent = self.board.first("agent").props.get("agent", "claude") if self.board.first("agent") else "claude"
        central = self.central_login(agent)
        choice = B.login_choice(node.props, central)
        combo = QComboBox()
        combo.setAccessibleName(tr("Login from"))
        for key, title in B.LOGIN_CHOICES:
            combo.addItem(tr(title), key)
        model = combo.model()
        if central is False:  # nothing stored: the automatic choice is this computer's own
            model.item(combo.findData("central")).setEnabled(False)
            model.item(combo.findData("central")).setToolTip(safe_tooltip(tr(
                "No central {agent} login is stored on this host: sandbox-ai login {agent} (Credentials page, Agent "
                "logins) keeps one", agent=agent)))
        elif central is True:  # a stored one is what the automatic choice takes
            model.item(combo.findData("host")).setEnabled(False)
            model.item(combo.findData("host")).setToolTip(safe_tooltip(tr(
                "A central {agent} login is stored: sandboxes get it instead of this computer's own (sandbox-ai login "
                "{agent} --logout forgets it)", agent=agent)))
        combo.setCurrentIndex(max(0, combo.findData(choice)))

        def chosen(_index):
            key = combo.currentData()
            node_ = self.board.node(node_id)
            if node_ is None or key == B.login_choice(node_.props, central):
                return
            value = None
            if key == "session":
                offered = self.login_sessions(agent)
                value = offered[0].get("name") if offered else None
            self.change(tr("Change {name}", name=tr("Login from")), lambda: B.set_login_choice(node_.props, key, value))
            self.inspect_timer.start()
        combo.currentIndexChanged.connect(chosen)
        self.inspector.addRow(tr("Login from"), combo)
        self.login_combo = combo
        if choice == "file":
            path = PathEdit(node.props.get("--auth-file", ""), host=self.host(), other_machine=True)
            path.fallback = lambda text: self.change(tr("Change {name}", name=tr("Login file")), lambda: B.set_login_choice(
                self.board.node(node_id).props, "file", text.strip()))
            path.changed.connect(path.fallback)
            self.inspector.addRow(tr("Login file"), path)
        if choice != "session":
            return
        sessions = QComboBox()
        sessions.setAccessibleName(tr("Session"))
        current = str(node.props.get("--auth-from-session") or "")
        offered = self.login_sessions(agent)
        for row in offered:
            account = account_text(row.get("info") or {})
            sessions.addItem(printable(f"{row.get('name')} - {account or row.get('info', {}).get('fingerprint', '')} "
                                       f"({row.get('state')})"), row.get("name"))
        if current and sessions.findData(current) < 0:
            sessions.addItem(printable(current), current)  # (named on the board; the checks say what is wrong with it)
        sessions.setCurrentIndex(max(0, sessions.findData(current)))
        if not offered and not current:
            sessions.addItem(tr("No session of {agent} with a login on this host", agent=agent), "")
            sessions.setEnabled(False)

        def session_chosen(_index):
            name = sessions.currentData()
            node_ = self.board.node(node_id)
            if node_ is not None and name and node_.props.get("--auth-from-session") != name:
                self.change(tr("Change {name}", name=tr("Session")),
                            lambda: B.set_login_choice(node_.props, "session", name))
        sessions.currentIndexChanged.connect(session_chosen)
        self.inspector.addRow(tr("Session"), sessions)
        warning = plain_label(tr(B.ROTATING_WARNING, name=printable(current or "?")), role="notice-warn")
        self.inspector.addRow("", warning)
        instead = QPushButton(tr("Import as central login instead"))
        instead.setToolTip(safe_tooltip(tr("Make the session's login the central login of {agent} (asked first), and "
                                           "take the central login for this start", agent=agent)))
        row = next((r for r in offered if r.get("name") == current), None)
        set_enabled(instead, row is not None, None if row is not None else tr("Choose a session with a login first."))
        instead.clicked.connect(lambda _=False: self.import_instead(node_id, row))
        self.inspector.addRow("", instead)
        self.import_instead_button = instead

    def import_instead(self, node_id, row):
        """Import as central login instead: ROW's login (a session's, login --status --sessions) imported as the
        central login on the board's host - asked first, naming the account and what it replaces - then this start
        takes the central login."""
        from .session_logins_view import account_text
        if not isinstance(row, dict):
            return False
        host, agent, name = self.host(), row.get("agent"), row.get("name")
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        data = self.session_login_data.get(host) or {}
        stored = (data.get("central") or {}).get(agent) if isinstance(data.get("central"), dict) else None
        account = account_text(info) or printable(str(info.get("fingerprint") or ""))
        where = "" if host == LOCAL else " " + tr("on {host}", host=printable(host))
        text = tr("Make the {agent} login of {name} ({account}) the central login{where}? It replaces the central login "
                  "({old}) for every {agent} sandbox started from now on.", agent=agent, name=printable(str(name)),
                  account=account, where=where, old=account_text(stored) or printable(str(stored.get("fingerprint"))))            if isinstance(stored, dict) and stored.get("fingerprint") != info.get("fingerprint") else \
            tr("Make the {agent} login of {name} ({account}) the central login{where}? Every {agent} sandbox started "
               "from now on uses it.", agent=agent, name=printable(str(name)), account=account, where=where)
        if not ask_first(self, "login-import", tr("Import as central login"), text, tr("Import")):
            return False
        fingerprint = info.get("fingerprint")
        replace = isinstance(stored, dict) and stored.get("fingerprint") != fingerprint
        background(lambda: self.sai.import_session_login(host, agent, name, fingerprint, replace), owner=self,
                   work=True, timeout=240, done=lambda result, error: self.imported_instead(node_id, host, result, error))
        return True

    def imported_instead(self, node_id, host, result, error):
        if not alive(self):
            return
        if error is not None:
            warn(self, tr("Import as central login"), tr("Not imported: {error}", error=printable_text(error)[:500]))
            return
        node = self.board.node(node_id)
        if node is not None:
            self.change(tr("Change {name}", name=tr("Login from")), lambda: B.set_login_choice(node.props, "central"))
        self.load_session_logins(host, now=True)
        self.inspect_timer.start()

    def use_session_login(self, host, agent, name):
        """The Credentials page's Use for a new start…: this board on HOST, its agent AGENT, its part HOME and login
        taking the login of the session NAME (one step of Undo); that part's properties shown. False when HOST is not
        one of the board's hosts."""
        if not self.host_choice.choose(host or LOCAL):
            inform(self, tr("Use for a new start"), tr("{host} is not among the Board's hosts.", host=printable(host)))
            return False

        def update():
            agent_node = self.board.first("agent")
            if agent_node is not None and agent_node.props.get("agent") != agent:
                B.switch_agent(agent_node.props, agent)
            home = self.board.first("home") or self.board.add("home")
            B.set_login_choice(home.props, "session", name)
        self.change(tr("Login from a session"), update)
        self.load_session_logins(host or LOCAL, now=True)
        home = self.board.first("home")
        if home is not None:
            self.show_properties(home.id)
        return True

    # -- the Bridge part: a session bridge made once the board's start runs --

    def set_running(self, instances):
        """The Sandboxes page's look ({key: Instance} of this machine): the sessions a Bridge part may join."""
        from .bridges_view import candidates
        self.running_sessions = candidates(instances.values() if isinstance(instances, dict) else instances)

    HOST_SESSIONS_EVERY = 30  # seconds between two reads of the hosts' running sessions for the Bridge part

    def load_host_sessions(self, now=False):
        """The sessions that run on the user's hosts, in the background (`sandbox-ai hosts`, then each host apart:
        one that does not answer holds up none of the others); the Bridge part offers them once they answer."""
        if self.host_sessions_asking or not hasattr(self.sai, "host_instances") or (
                not now and self.host_sessions_read is not None and
                time.monotonic() - self.host_sessions_read < self.HOST_SESSIONS_EVERY):
            return
        self.host_sessions_read = time.monotonic()
        self.host_sessions_asking.add("")  # (the hosts' names are read first)
        background(self.sai.remote_hosts, timeout=30, owner=self,
                   done=lambda hosts, error: self.hosts_for_bridge(hosts, error))

    def hosts_for_bridge(self, hosts, error):
        from .bridges_view import REMOTE_WAIT
        from . import bridges as BR
        self.host_sessions_asking.discard("")
        if error is not None:
            self.host_session_problems = {"sandbox-ai hosts": printable_text(error)[:200]}
            return
        names = [h for h in hosts or () if isinstance(h, str) and BR.HOST.fullmatch(h) and h != LOCAL]
        self.host_sessions = {host: entries for host, entries in self.host_sessions.items() if host in names}
        for host in names:
            self.host_sessions_asking.add(host)
            background(self.sai.host_instances, host, REMOTE_WAIT, owner=self, timeout=REMOTE_WAIT + 15,
                       done=lambda found, failed, h=host: self.host_sessions_listed(h, found, failed))

    def host_sessions_listed(self, host, found, error):
        """HOST's running sessions (or why it did not answer): the Bridge part shown again, unless its field for the
        other session is being typed in."""
        from .bridges_view import candidates
        self.host_sessions_asking.discard(host)
        if error is not None:
            self.host_session_problems[host] = printable_text(error)[:200]
        else:
            self.host_session_problems.pop(host, None)
            self.host_sessions[host] = candidates([i for i in found or () if getattr(i, "host", None) == host],
                                                  remote=True)
        field = self.peer_field
        typing = field is not None and alive(field) and field.hasFocus()
        if not typing and self.inspected in self.items and self.board.node(self.inspected).kind == "bridge":
            self.inspect_timer.start()

    def bridge_rows(self, node):
        """The Bridge part's properties: the other session (one that runs here or on one of your hosts, grouped by
        host and named as the board's host sees it; or a name typed for one to come), which way, what A may do to B
        (paths relative to B's workspace), the observer's actions, both directions, until, the ask mode and the
        limits."""
        from . import bridges as BR
        node_id, props = node.id, node.props
        self.load_host_sessions()
        peer = QComboBox()
        peer.setEditable(True)
        peer.setAccessibleName(tr("Other session"))
        board_host = self.host()
        for host, entries in BR.peer_groups(self.running_sessions, self.host_sessions, board_host):
            title = tr("This machine") if host is None else printable(host)
            if host == (None if board_host == LOCAL else board_host):
                title += " " + tr("(the board's host)")
            peer.addItem(title)
            peer.model().item(peer.count() - 1).setEnabled(False)  # (a heading, not a choice)
            for value, entry in entries:
                peer.addItem(f"  {printable(value)} ({printable(entry.get('agent') or '?')})", value)
        asking = sorted(h for h in self.host_sessions_asking if h)
        problems = sorted(self.host_session_problems.items())
        peer.setEditText(str(props.get("peer") or ""))
        peer.setToolTip(tr("A running agent session: NAME on the board's host, local:NAME of this machine, HOST:NAME of "
                           "another of your hosts (or the name of one you will start before this board's start runs)"))
        peer.lineEdit().editingFinished.connect(lambda: self.set_prop(node_id, "peer", peer.currentText().strip()))
        peer.activated.connect(lambda index: self.set_prop(node_id, "peer", str(
            peer.itemData(index) if peer.itemData(index) else peer.currentText()).strip()))
        self.peer_field = peer.lineEdit()
        self.inspector.addRow(tr("Other session"), peer)
        if asking or problems:
            lines = ([tr("Asking your hosts for their running sessions: {hosts}…", hosts=", ".join(
                printable(h) for h in asking))] if asking else []) + [
                tr("{host}: no answer", host=printable(host)) + f" ({problem})" for host, problem in problems]
            self.inspector.addRow(plain_label("\n".join(lines), role="secondary"))
        start = "board-start" if board_host == LOCAL else f"{board_host}:board-start"
        other = BR.on_host(str(props.get("peer") or ""), board_host)
        where = BR.placement_note(start, other) if other else None
        if where:
            self.inspector.addRow(plain_label(where, role="secondary"))
        direction = QComboBox()
        direction.setAccessibleName(tr("Direction"))
        for key, text in B.BRIDGE_DIRECTIONS:
            direction.addItem(tr(text), key)
        direction.setCurrentIndex(max(0, direction.findData(props.get("direction", "out"))))
        direction.currentIndexChanged.connect(lambda _=0: self.set_prop(node_id, "direction", direction.currentData()))
        self.inspector.addRow(tr("Direction"), direction)
        for key, text in (("message", tr("Send messages (message)")),
                          ("ask", tr("Ask questions and wait for the answer (ask)")),
                          ("watch", tr("Observe B (watch)")),
                          ("both", tr("Both directions (B may do the same to A)"))):
            box = QCheckBox(text)
            box.setChecked(bool(props.get(key)))
            box.toggled.connect(lambda on, k=key: self.set_prop(node_id, k, bool(on)))
            self.inspector.addRow("", box)
        # The files it may read and write: one field of the two kinds (list_fields), a path each.
        where = tr("one path per line, relative to its workspace (. for all of it)")
        files = LF.ListFields([LF.ListKind(key, label, tip=where, placeholder="src")
                               for key, label in (("read", tr("Read files (files:ro)")),
                                                  ("write", tr("Write files (files:rw)")))],
                              {key: props.get(key) if isinstance(props.get(key), list) else []
                               for key in ("read", "write")}, name=tr("Files"))
        if self.list_kinds.get("bridge"):
            files.choose_kind(self.list_kinds["bridge"])
        files.kind_chosen.connect(lambda key: self.list_kinds.__setitem__("bridge", key))
        files.changed.connect(lambda key, values: self.set_prop(node_id, key, values))
        self.inspector.addRow(tr("Files"), files)
        self.option_fields["bridge:files"] = files  # (where the keyboard was, when the rows are made anew)
        chosen = props.get("actions") if isinstance(props.get("actions"), list) else list(BR.DEFAULT_ACTIONS)
        actions = []
        for key, text in (("alert", tr("Alert you")), ("hint", tr("Hint")), ("net-cut", tr("Cut B's network")),
                          ("pause", tr("Pause B"))):
            box = QCheckBox(text)
            box.setChecked(key in chosen)
            box.setEnabled(bool(props.get("watch")))
            actions.append((key, box))
        for key, box in actions:
            box.toggled.connect(lambda _on: self.set_prop(node_id, "actions", [k for k, b in actions if b.isChecked()]))
        self.inspector.addRow(tr("The observer may"), flow(*(box for _, box in actions)))
        self.text_row(node, "until", tr("Until"), tr("30m, 2h, 1d, HH:MM or an ISO time; empty: until either session "
                                                     "ends"))
        ask = QComboBox()
        ask.setAccessibleName(tr("Ask you about"))
        for key, text in BR.ASK_MODES:
            ask.addItem(tr(text), key)
        ask.setCurrentIndex(max(0, ask.findData(props.get("ask_mode", "first"))))
        ask.currentIndexChanged.connect(lambda _=0: self.set_prop(node_id, "ask_mode", ask.currentData()))
        self.inspector.addRow(tr("Ask you about"), ask)
        limits = dict(props.get("limits")) if isinstance(props.get("limits"), dict) else {}
        # The limits seldom need to be set (each has its default): folded under "More options".
        self.begin_more(node, sum(bool(str(limits.get(key, "") or "").strip()) for key, *_ in BR.LIMITS))
        for key, title, default, tip in BR.LIMITS:
            edit = QLineEdit(str(limits.get(key, "") or ""))
            edit.setPlaceholderText(tr("default: {value}", value=default))
            edit.setToolTip(tr(tip))

            def commit(_=None, k=key, e=edit):
                current = dict(node.props.get("limits")) if isinstance(node.props.get("limits"), dict) else {}
                if e.text().strip():
                    current[k] = e.text().strip()
                else:
                    current.pop(k, None)
                self.set_prop(node_id, "limits", current)
            edit.editingFinished.connect(commit)
            self.inspector.addRow(tr(title), edit)
        self.end_more()
        self.inspector.addRow(explanation(tr("Made once the board's start and the other session run (here or on "
                                             "their hosts), after you confirm what the launcher warns about; not "
                                             "written into the project file.")))

    # -- the Team part: the board's sandbox once per agent (sandbox-ai team) --

    def team_rows(self, node):
        """The Team part's properties: its name, lead and members (sandbox-ai's team agents), the shared project
        folder (the board's), each member's role and its own model, LLM server and security mode, the OpenVPN
        confirmation; then team start's verdict on the board's host and the team's board as it is there (read-only:
        members, tasks and answers) with Refresh and the way to the Teams page."""
        node_id, props = node.id, node.props
        members = B.team_members(props)
        from .team_page import NAME_EXAMPLE
        name = QLineEdit(str(props.get("name") or ""))
        name.setPlaceholderText(NAME_EXAMPLE)
        name.setAccessibleName(tr("Team name"))
        name.setToolTip(tr("1-20 of a-z, 0-9, '_' and '-'; its members are the sessions NAME-AGENT"))
        name.editingFinished.connect(lambda: self.set_prop(node_id, "name", name.text().strip()))
        self.inspector.addRow(tr("Name"), name)
        lead = QComboBox()
        lead.setAccessibleName(tr("Lead"))
        for agent in backend.TEAM_AGENTS:
            lead.addItem(agent, agent)
        lead.setCurrentIndex(max(0, lead.findData(members[0] if members else "claude")))
        lead.setToolTip(tr("The lead talks to you and hands the others tasks on the team's board"))
        lead.currentIndexChanged.connect(lambda _=0: self.set_team_lead(node_id, lead.currentData()))
        self.inspector.addRow(tr("Lead"), lead)
        boxes = []
        for agent in backend.TEAM_AGENTS:
            box = QCheckBox(agent)
            box.setChecked(agent in members)
            box.setEnabled(agent != (members[0] if members else None))
            boxes.append((agent, box))
        for _agent, box in boxes:
            box.toggled.connect(lambda _on=False: self.set_prop(node_id, "members",
                                                                [a for a, b in boxes if b.isChecked()]))
        self.inspector.addRow(tr("Members"), flow(*(box for _, box in boxes)))
        values = B.team_values(self.sai, self.board, local=self.host() == LOCAL) or {}
        where = values.get("workspace") or tr("(none yet: connect a project folder to the agent)")
        self.inspector.addRow(tr("Shared workspace"), plain_label(printable(where), selectable=True))
        modes = getattr(B._options(self.sai).get("--mode"), "choices", None) or ()
        for agent in members:
            heading = plain_label(tr("{agent} (leads)", agent=agent) if agent == members[0] else agent,
                                  role="secondary", word_wrap=False)
            self.inspector.addRow(heading)
            for key, label, placeholder in (
                    ("roles", tr("Role"), tr("e.g. reviews every change; writes the tests first")),
                    ("models", tr("Model"), tr("the team's (the board's)")),
                    ("llms", tr("LLM server"), tr("the team's: SERVER[:MODEL] of sandbox-ai llm"))):
                edit = QLineEdit(str((props.get(key) or {}).get(agent) or "")
                                 if isinstance(props.get(key), dict) else "")
                edit.setPlaceholderText(placeholder)
                edit.setAccessibleName(tr("{what} of {agent}", what=label, agent=agent))
                edit.editingFinished.connect(lambda k=key, a=agent, e=edit: self.set_member_value(
                    node_id, k, a, e.text().strip()))
                self.inspector.addRow(label, edit)
            mode = QComboBox()
            mode.setAccessibleName(tr("{what} of {agent}", what=tr("Mode"), agent=agent))
            mode.addItem(tr("(the team's)"), "")
            for choice in modes:
                mode.addItem(str(choice), str(choice))
            own = props.get("modes") if isinstance(props.get("modes"), dict) else {}
            mode.setCurrentIndex(max(0, mode.findData(own.get(agent) or "")))
            mode.currentIndexChanged.connect(lambda _=0, a=agent, m=mode: self.set_member_value(
                node_id, "modes", a, m.currentData() or ""))
            self.inspector.addRow(tr("Mode"), mode)
        shared = QCheckBox(tr("Allow one OpenVPN profile in several members (your server must allow it: "
                              "duplicate-cn)"))
        shared.setChecked(bool(props.get("shared_openvpn")))
        shared.toggled.connect(lambda on: self.set_prop(node_id, "shared_openvpn", bool(on)))
        self.inspector.addRow("", shared)
        self.inspector.addRow(explanation(tr("Every member gets this board's settings; with the Team part the "
                                             "board's Start runs sandbox-ai team start on the board's host. Not "
                                             "written into the project file.")))
        verdict = plain_label(self.team_verdict_text(), role="secondary", selectable=True)
        verdict.setAccessibleName(tr("team start's verdict"))
        self.inspector.addRow(tr("Check"), verdict)
        self.team_board_rows(node)

    def set_team_lead(self, node_id, agent):
        """AGENT leads the Team part NODE_ID (and is one of its members): one step of Undo."""
        node = self.board.node(node_id)
        if node is None or agent not in backend.TEAM_AGENTS or node.props.get("lead") == agent:
            return

        def update():
            node.props["lead"] = agent
            members = node.props.get("members") if isinstance(node.props.get("members"), list) else []
            node.props["members"] = [agent, *[a for a in members if a != agent]]
        self.change(tr("Change {name}", name=tr("Lead")), update)
        self.inspect_timer.start()  # (the lead's box greyed, its rows first)

    def set_member_value(self, node_id, key, agent, value):
        """The Team part's KEY (roles, models, llms, modes) of member AGENT: VALUE, or none when empty."""
        node = self.board.node(node_id)
        if node is None:
            return
        current = dict(node.props.get(key)) if isinstance(node.props.get(key), dict) else {}
        if value:
            current[agent] = value
        else:
            current.pop(agent, None)
        self.set_prop(node_id, key, current)

    def team_verdict_text(self):
        """What team start on the board's host said about the board as it is now (or that it is being asked)."""
        verdict = self.team_verdict
        if any(p.level == "error" for p in B.team_problems(self.board)):
            return tr("team start checks the team once the part's problems are solved.")
        if verdict is None or verdict[0] != self.board.state() or verdict[1] != self.host():
            return tr("team start checks the team on {host}…", host=printable(self.host()))
        result, error = verdict[2], verdict[3]
        if error is not None:
            return tr("team start could not be asked on {host}: {error}", host=printable(self.host()),
                      error=printable_text(error)[:300])
        if result.get("ok") and result.get("continues"):
            return tr("team start continues the team that is there: {members} (with the options they were made "
                      "with)", members=", ".join(printable(f"{result.get('name')}-{agent}")
                                                for agent in result.get("continues") or []))
        if result.get("ok"):
            return tr("team start accepts the team: {members}", members=", ".join(
                printable(m.get("session")) for m in result.get("plan") or [] if isinstance(m, dict)))
        return tr("team start refuses: {error}", error=printable_text(result.get("error") or "?")[:600])

    def check_team(self):
        """team start's own verdict on the board's Team part: `team start --dry-run --json` on the board's host (every
        check of a team start there, each member's start as the launcher's dry run), in the background, one at a
        time (an edit meanwhile asks once more after it); its answer for an older board or another host is
        dropped."""
        host, state = self.host(), self.board.state()
        values = B.team_values(self.sai, self.board, local=host == LOCAL)
        if values is None or any(p.level == "error" for p in B.team_problems(self.board)):
            if self.team_verdict is not None and values is None:
                self.team_verdict = None
            return
        if self.team_running:
            self.team_pending = True
            return
        self.team_token += 1
        token = self.team_token
        self.team_running, self.team_pending = True, False
        background(lambda: self.sai.team_check(host, **values), timeout=300, owner=self,
                   what=f"team start's check of the board's team on {host}",
                   done=lambda result, error: self.team_checked(token, state, host, result, error))

    def team_checked(self, token, state, host, result, error):
        self.team_running = False
        if self.team_pending:
            self.team_pending = False
            self.check_team()
            return
        if token != self.team_token:
            return
        self.team_verdict = (state, host, result if isinstance(result, dict) else None,
                             error if error is not None or isinstance(result, dict) else ValueError("no answer"))
        applog.changed(("board team verdict", id(self)), "INFO", "board", "team start accepts the board's team"
                       if error is None and result.get("ok") else "team start refuses the board's team: "
                       + applog.redact(str(error if error is not None else result.get("error")))[:400], quiet="DEBUG")
        self.fill_findings()
        if self.inspected in self.items and self.board.node(self.inspected) is not None and \
                self.board.node(self.inspected).kind == "team":
            self.inspect_timer.start()  # (its verdict row shown anew)
        then, self.after_team_check = getattr(self, "after_team_check", None), None
        if then is not None:
            then()

    def team_findings(self):
        """[Problem] of team start's verdict on the board as it is now: its refusal on the Team part (a team of that
        name that exists there already as a warning: its board is shown in the part)."""
        node, verdict = B.team_node(self.board), self.team_verdict
        if node is None or verdict is None or verdict[0] != self.board.state() or verdict[1] != self.host():
            return []
        result, error = verdict[2], verdict[3]
        if error is not None:
            return [B.Problem("warning", tr("Team: team start could not be asked on {host}: {error}",
                                            host=printable(self.host()), error=printable_text(error)[:300]), node.id)]
        if result.get("ok"):
            return []
        # (a team or a member's session of that name there: a warning - Start asks what to do about it)
        level = "warning" if result.get("code") in ("exists", "taken") else "error"
        return [B.Problem(level, tr("Team: {error}", error=printable_text(result.get("error") or "?")[:600]), node.id)]

    def team_board_rows(self, node):
        """The team's board as it is on the board's host (team show --json): its members and their state, and its
        tasks with their answers - read-only; Refresh asks again, Open on the Teams page shows the team there."""
        name = str(node.props.get("name") or "").strip()
        box = QGroupBox(tr("The team's board"))
        layout = QVBoxLayout(box)
        status = plain_label(role="secondary", selectable=True)
        members = tables_table([tr("Member"), tr("Role"), tr("State")], stretch=0, compact=(1, 2))
        members.setAccessibleName(tr("Members"))
        members.setMaximumHeight(130)
        tasks = tables_table([tr("Task"), tr("From"), tr("For"), tr("State"), tr("What"), tr("Answer")], stretch=4,
                             compact=(0, 1, 2, 3))
        tasks.setAccessibleName(tr("The team's board"))
        tasks.setMinimumHeight(140)
        refresh = QPushButton(tr("Refresh"))
        refresh.setToolTip(tr("sandbox-ai team show on the board's host"))
        teams = QPushButton(tr("Open on the Teams page"))
        teams.setToolTip(tr("The Teams page with this team selected: post a task, talk to the lead, stop or delete"))
        layout.addWidget(status)
        layout.addWidget(members)
        layout.addWidget(tasks, 1)
        layout.addWidget(flow(refresh, teams))
        self.inspector.addRow(box)
        self.team_view = (name, self.host(), status, members, tasks, refresh, teams)
        refresh.clicked.connect(lambda _=False: self.load_team_board(now=True))
        teams.clicked.connect(lambda _=False: self.open_team_page())
        self.load_team_board()

    def open_team_page(self):
        """The Teams page, the board's host chosen and this team selected."""
        node = B.team_node(self.board) or self.board.first("team")
        name = str(node.props.get("name") or "").strip() if node is not None else ""
        if B.TEAM_NAME.fullmatch(name):
            self.team_wanted.emit(name, self.host())

    def load_team_board(self, now=False):
        """team show --json NAME on the board's host, in the background, into the part's board (team_view); an answer
        for another team, host or view is dropped. Read again when shown unless NOW or the last answer is for
        another team."""
        view = self.team_view
        if view is None or not alive(view[2]):
            return
        name, host, status = view[0], view[1], view[2]
        if not B.TEAM_NAME.fullmatch(name):
            status.setText(tr("Give the team a name: its board is shown here once it runs."))
            set_enabled(view[6], False, tr("Give the team a name first."))
            return
        shown = self.team_shown
        if shown is not None and shown[:2] == (name, host) and not now:
            self.show_team_board(*shown)
            return
        self.team_board_token += 1
        token = self.team_board_token
        status.setText(tr("Asking {host}…", host=printable(host)))
        background(lambda: self.sai.team_show(host, name), owner=self, timeout=75, source=view[5],
                   what=f"the board of team {name} on {host}",
                   done=lambda result, error: self.team_board_read(token, name, host, result, error))

    def team_board_read(self, token, name, host, result, error):
        if token != self.team_board_token:
            return
        self.team_shown = (name, host, result if error is None else None, error)
        view = self.team_view
        if view is not None and alive(view[2]) and view[:2] == (name, host):
            self.show_team_board(name, host, result if error is None else None, error)

    def show_team_board(self, name, host, result, error):
        """Fill the part's board (team_view) with team NAME on HOST as team show --json said (RESULT), or why not."""
        from .team_page import ROLES, STATES, STATE_COLORS
        _name, _host, status, members, tasks, _refresh, teams = self.team_view
        if error is not None or not isinstance(result, dict):
            fill(members, [])
            fill(tasks, [])
            text = printable_text(error)[:300] if error is not None else "?"
            status.setText(tr("No team {name} on {host} yet: it is made by the board's Start.", name=name,
                              host=printable(host)) if "no team" in text else
                           tr("Could not read team {name} on {host}: {error}", name=name, host=printable(host),
                              error=text))
            set_enabled(teams, "no team" not in text, tr("No such team on the board's host yet."))
            return
        set_enabled(teams, True)
        agents = [a for a in result.get("members") or [] if a in backend.TEAM_AGENTS]
        states = result.get("states") if isinstance(result.get("states"), dict) else {}
        lead = result.get("lead")
        fill(members, [[B.team_label(f"{name}-{agent}", host), tr(ROLES["lead" if agent == lead else "member"]),
                        printable(states.get(agent) or "?")] for agent in agents],
             [STATE_COLORS.get(states.get(agent)) for agent in agents])
        board = result.get("board") if isinstance(result.get("board"), dict) else {}
        listed = [t for t in board.get("tasks") or [] if isinstance(t, dict)][-200:]
        # What a board holds comes from the members: text through printable(), never markup.
        def line(value):
            return printable(" ".join(str(value or "").split()))[:300]
        fill(tasks, [[f"#{printable(t.get('id'))}", printable(t.get("from") or "?")[:64],
                      printable(t.get("to") or "?")[:20], tr(STATES[t["state"]]) if t.get("state") in STATES
                      else printable(t.get("state") or "?"), line(t.get("task")), line(t.get("answer"))]
                     for t in listed],
             ["ok" if t.get("state") == "done" else "warn" if t.get("state") == "working" else None for t in listed])
        text = tr("Team {name}: {count} tasks on its board", name=B.team_label(name, host),
                  count=len(board.get("tasks") or []))
        if board.get("error"):
            text += " · " + tr("board: {error}", error=printable(board["error"])[:300])
        status.setText(text)

    def run_team_start(self, embedded, continued=False):
        """Start the board's team: `sandbox-ai team start` on the board's host in a terminal, as the Teams page starts
        one (each member starts there, one after the other, and says so), once team start's own check of this very
        board accepted it - asked again first when its last verdict is for another board. A team of that name that is
        paused there is asked about first (Continue it: CONTINUED), as a single start's paused session is."""
        host, state = self.host(), self.board.state()
        verdict = self.team_verdict
        if verdict is None or verdict[0] != state or verdict[1] != host:
            self.after_team_check = lambda: self.run_team_start(embedded) \
                if self.board.state() == state and self.host() == host else None
            self.show_notice(tr("team start checks the team on {host}…", host=printable(host)))
            self.check_team()
            return False
        values = B.team_values(self.sai, self.board, local=host == LOCAL)
        paused = verdict[3] is None and verdict[2].get("ok") and verdict[2].get("continues") and "paused" in (
            ((verdict[2].get("team") or {}).get("states") or {}).values())
        if (paused and not continued) or (
                verdict[3] is None and not verdict[2].get("ok") and verdict[2].get("code") in ("exists", "taken")):
            # A team of that name there (it runs, has other members, a member is gone) or a member's session name in
            # use: said before anything runs, with what can be done - as a single start's session check.
            node = B.team_node(self.board)
            self.team_ask(host, values["name"], verdict[2], embedded,
                          lambda: (setattr(self, "team_verdict", None), self.run_team_start(embedded))
                          if self.host() == host else None,
                          lambda name: node is not None and self.set_prop(node.id, "name", name),
                          go_on_continued=lambda: self.run_team_start(embedded, continued=True)
                          if self.host() == host and self.board.state() == state else None)
            return False
        if verdict[3] is not None or not verdict[2].get("ok"):
            warn(self, tr("Not started"), self.team_verdict_text())
            return False
        try:
            # (the members' logins were asked for before - prerequisite_login_agents -: a team start there that
            # says which logins it lacks takes --no-login-question, so that its terminal does not ask again)
            argv = self.sai.team_start_argv(host, **values, login_question="logins" not in verdict[2])
        except ValueError as exc:
            warn(self, tr("Not started"), printable_text(exc))
            return False
        title = f"sandbox-ai team start {values['name']}" + ("" if host == LOCAL else f" ({printable(host)})")
        applog.log("INFO", "board", f"the board's team {values['name']} starts on {printable(host)} in a terminal "
                                    f"({', '.join(values['agents'])}), options: "
                                    f"{applog.argv_text(values['options'], 2000)}")
        self.terminal(argv, title, embedded=embedded, cwd=None)
        self.team_shown = None  # (its board is read anew: the team is made now)
        self.show_notice(tr("Team {name} starts in the terminal on {host}; the Team part shows its board.",
                            name=B.team_label(values["name"], host), host=printable(host)))
        return True

    def bridge_entries(self, name, host=LOCAL):
        """[(values, args)] of the Bridge parts for the start NAME on HOST; ValueError of the first that cannot be
        made."""
        entries = []
        for node in B.bridge_nodes(self.board):
            if not str(node.props.get("peer") or "").strip():
                continue  # (a warning on the board: none is made)
            values = B.bridge_values(node.props, name, host)
            entries.append((values, self.sai.bridge_add_args(**values)))
        return entries

    def start_bridges(self, name, host=LOCAL):
        """Wait for the start NAME (on HOST) and the other sessions, then make the Bridge parts' bridges (each
        confirmed)."""
        from .bridges_view import BridgeWaiter
        try:
            entries = self.bridge_entries(name, host)
        except ValueError as exc:
            warn(self, tr("Not bridged"), printable_text(exc), modal=False)
            return None
        if not entries:
            return None
        waiter = self.keep_waiter(BridgeWaiter(self, self.sai, entries))
        self.show_notice(tr("The board's bridges are made once {name} and the other sessions run; you confirm each.",
                            name=printable(name)))
        return waiter

    def keep_waiter(self, waiter):
        """WAITER among the board's: what it still waits for is kept in the app's state at once (a crash, a quit:
        the next start waits on - resume_bridge_waits), and again whenever a bridge of it is done."""
        self.bridge_waiters.append(waiter)
        waiter.finished.connect(lambda w=waiter: self.bridge_waiters.remove(w) if w in self.bridge_waiters else None)
        if self.state is not None:
            waiter.changed.connect(self.state.schedule)
            waiter.finished.connect(self.state.schedule)
            self.state.schedule()
        return waiter

    def bridge_waits_kept(self):
        """What the board's bridges still wait for (BridgeWaiter.kept), for the app's state."""
        return [row for waiter in self.bridge_waiters for row in waiter.kept()]

    def resume_bridge_waits(self, rows):
        """The waits the app's state kept (ROWS: bridge_waits_kept's when the app was last left): those whose time is
        not over wait on, until then (said above the board); those whose time ran out while the app was closed, and
        those that cannot be used, are said - make them on the Sandboxes page. Nothing is made without the usual
        question of each bridge."""
        from .bridges_view import BridgeWaiter, kept_waits
        entries, problems = kept_waits(self.sai, rows)
        now = time.time()
        waiting = [(until, values, args) for until, values, args in entries if until > now]
        over = [values for until, values, _args in entries if until <= now]
        names = lambda chosen: ", ".join(f"{printable(v['a'])} → {printable(v['b'])}" for v in chosen)  # noqa: E731
        for until in sorted({until for until, _v, _a in waiting}):
            group = [(values, args) for end, values, args in waiting if end == until]
            self.keep_waiter(BridgeWaiter(self, self.sai, group, seconds=until - now))
        if waiting:
            text = tr("The app was closed while the board's bridges waited for their sessions: they wait again, until "
                      "{time} ({list}); you confirm each.", time=time.strftime("%H:%M", time.localtime(max(
                          until for until, _v, _a in waiting))), list=names([v for _u, v, _a in waiting]))
            applog.log("INFO", "board", text)
            inform(self, tr("Bridges wait"), text, modal=False)  # (at the app's start: a line above the board is
            # replaced by the project's own as soon as it is read)
        if over or problems:
            lines = []
            if over:
                lines.append(tr("The app was closed while the board's bridges waited for their sessions, and their wait "
                                "is over: {list} not made. Make them on the Sandboxes page (New bridge…).",
                                list=names(over)))
            if problems:
                lines.append(tr("Waits kept from the last start of the app that cannot be used were dropped: {list}",
                                list="; ".join(problems)))
            applog.log("WARNING", "board", " ".join(lines))
            warn(self, tr("Not bridged"), "\n\n".join(lines), modal=False)
        if self.state is not None and rows:
            self.state.schedule()  # (said once: what is over is not kept)

    def end_bridge_waits(self):
        """The app quits: what the board's bridges still wait for is kept (the state is written before this) and
        said in the log; the waits end here and go on at the next start (resume_bridge_waits)."""
        rows = self.bridge_waits_kept()
        if rows:
            applog.log("INFO", "board", "the app quits while the board's bridges wait for their sessions: "
                       + ", ".join(f"{printable(r['values']['a'])} → {printable(r['values']['b'])}" for r in rows)
                       + "; the next start of the app waits on until "
                       + time.strftime("%H:%M", time.localtime(max(r["until"] for r in rows))))
        self.bridges_ended = True
        for waiter in list(self.bridge_waiters):
            waiter.timer.stop()
            waiter.pending = []
        self.bridge_waiters = []

    # -- services and your own images --

    def set_user_images(self, rows, bases, host=LOCAL, templates=None, error=None):
        """Your images and the variants' bases of the host the board starts on (backend.user_images, variant_bases),
        read in the background: the service nodes choose from them, the checks and the score use them. TEMPLATES:
        sandbox-ai's own Containerfiles there (backend.service_templates: offered, built when a start needs one);
        ERROR: why ROWS is None (the host did not answer, an older sandbox-ai) - said in the list, never "none"."""
        if host != self.host():  # (an answer of a host before: the board's own host is read now)
            if getattr(self, "user_images_loading", False) in (host, True):
                self.user_images_loading = False
            self.load_user_images()
            return
        self.user_images_loading = False
        before = (getattr(self, "user_images", None), getattr(self, "user_images_failed", False),
                  getattr(self, "image_bases", None), getattr(self, "image_templates", None))
        self.user_images_failed = rows is None  # not answered: the services' images are not checked then
        self.user_images_error = (str(error or "") or None) if rows is None else None
        self.user_images = rows if isinstance(rows, list) else []
        self.user_images_read_at = time.monotonic()
        self.image_bases = bases if isinstance(bases, dict) else {}
        self.image_templates = {str(row["image"]): row for row in templates or []
                                if isinstance(row, dict) and isinstance(row.get("image"), str)}
        opened = []
        for combo in getattr(self, "image_combos", []):  # (the guide's question, open meanwhile: its list now)
            if alive(combo):
                typed = combo.currentData() if combo.currentText() == combo.itemText(combo.currentIndex()) else \
                    combo.currentText()
                self.fill_image_combo(combo, typed if typed not in (OTHER_IMAGE, ADD_IMAGE) else "")
                if isinstance(combo, OpeningCombo) and combo.popup_open():
                    opened.append(combo)
        self.image_combos = []
        self.refresh()
        changed = before != (self.user_images, self.user_images_failed, self.image_bases, self.image_templates)
        if changed and self.inspected in self.items and self.board.node(self.inspected) is not None and \
                self.board.node(self.inspected).kind in ("service", "engine"):
            # The inspector anew - never under an image list the user holds open (its read was what came back):
            # once that one closes. Unchanged, the lists were filled in place and nothing else depends on them.
            if opened:
                def again(combo=opened[0]):
                    if alive(self) and alive(combo):
                        combo.closed.disconnect(again)
                    if alive(self):
                        self.inspect_timer.start()
                opened[0].closed.connect(again)
            else:
                self.inspect_timer.start()
        then, self.after_images = getattr(self, "after_images", None), None
        if then is not None:  # a start waited for them (read just now: a failed read is not tried again by it)
            self.images_just_read = True
            try:
                then()
            finally:
                self.images_just_read = False

    def load_user_images(self):
        """Your images, the variants' bases and sandbox-ai's own templates of the board's host, read in the background
        (set_user_images). A read of another host still under way does not hold this one up (a slow host kept the
        next one's list at "Reading…" until it answered): its late answer is dropped."""
        host = self.host()
        if getattr(self, "user_images_loading", False) == host:
            return
        self.user_images_loading = host
        self.user_images_token = token = getattr(self, "user_images_token", 0) + 1

        def read():
            rows, bases, templates, why = [], {}, [], None
            try:
                rows = self.sai.user_images(host)
            except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai or a host that does not answer: said
                applog.swallowed("board_view", "load_user_images.read: an older sandbox-ai or a host that does …", _exc)
                rows, why = None, str(_exc) or type(_exc).__name__
            try:
                bases = self.sai.variant_bases(host)
            except Exception as _exc:  # noqa: BLE001
                applog.swallowed("board_view", "load_user_images.read failed (went on without it)", _exc)
                bases = {}
            if hasattr(self.sai, "service_templates"):
                try:
                    templates = self.sai.service_templates(host)
                except Exception as _exc:  # noqa: BLE001 - none offered then
                    applog.swallowed("board_view", "load_user_images.read: sandbox-ai's own templates", _exc)
                    templates = []
            return rows, bases, templates, why

        def done(result, error):
            if token != getattr(self, "user_images_token", token):
                return  # (a read of the board's host now is under way: this answer is another one's)
            if error is None and isinstance(result, tuple) and len(result) == 4:
                rows, bases, templates, why = result
            else:
                rows, bases, templates, why = None, {}, [], str(error or "") or None
            self.set_user_images(rows, bases, host, templates=templates, error=why)
        background(read, owner=self, done=done, timeout=180,
                   what=f"reading the images for services on {host} (sandbox-ai images list, templates)")

    def image_combo(self, current):
        """A drop-down to choose a service's image from: your images (sandbox-ai images list: those pulled and built,
        each with its kind and what it is pinned as), sandbox-ai's own Containerfiles (images templates: built when a
        start needs one), why agent images are none of them, the Images page to add one, then "Other…" for a name
        typed in - CURRENT chosen, or typed in when it lists no such image. Read in the background where not known
        yet: the list follows once read. The inspector and the guide's question use it; its items' data are the
        names."""
        combo = OpeningCombo()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.lineEdit().setPlaceholderText(tr("Choose one of your images, or type a name…"))
        combo.setAccessibleName(tr("Image"))
        combo.setToolTip(tr("One of your images (sandbox-ai images list; the Images page adds them) or one of "
                            "sandbox-ai's own (built when a start needs it), or Other… for a name typed in"))
        self.fill_image_combo(combo, current)
        combo.chosen_value = str(current or "")
        if getattr(self, "user_images", None) is None:
            self.load_user_images()
            waiting = getattr(self, "image_combos", [])
            self.image_combos = [c for c in waiting if alive(c)] + [combo]

        def other(_index):
            data = combo.itemData(_index)
            if data == OTHER_IMAGE:
                combo.setCurrentIndex(-1)
                combo.setEditText("")
                combo.lineEdit().setFocus()
            elif data == ADD_IMAGE:  # the Images page (Your images: Add from a registry, from a Containerfile)
                combo.adding = True  # (the inspector's own handler, after this one, leaves the image as it is)
                self.fill_image_combo(combo, combo.chosen_value)
                self.images_wanted.emit()
            elif isinstance(data, str):
                combo.chosen_value = data
        combo.activated.connect(other)
        combo.lineEdit().editingFinished.connect(
            lambda: setattr(combo, "chosen_value", combo.currentText().strip())
            if combo.currentText() != combo.itemText(combo.currentIndex()) else None)
        combo.opening.connect(lambda: self.reread_user_images(combo))
        return combo

    def names_changed(self, kind, host):
        if kind == "image" and alive(self) and host == self.host():
            self.user_images_read_at = 0.0

    def reread_user_images(self, combo=None):
        """Your images read again (an image combo opened: one added meanwhile, in a terminal or on the Images page, is
        offered); COMBO is filled once they are. Not again while a read runs or one ended a moment ago."""
        if combo is not None:
            waiting = getattr(self, "image_combos", [])
            self.image_combos = [c for c in waiting if alive(c) and c is not combo] + [combo]
        if getattr(self, "user_images_loading", False) or \
                time.monotonic() - getattr(self, "user_images_read_at", 0.0) < 3.0:
            return
        self.load_user_images()

    def host_words(self):
        return tr("this computer") if self.host() == LOCAL else printable(self.host())

    def images_failed_text(self):
        """Why your images of the board's host are not known (their read failed), in words: an older sandbox-ai
        there (no images list) names its update."""
        error = printable_text(str(getattr(self, "user_images_error", None) or ""))
        if "invalid choice" in error:
            return tr("Your images on {host} could not be read: its sandbox-ai has no images list - update it "
                      "(Hosts page, or sandbox-ai update there)", host=self.host_words())
        return tr("Your images on {host} could not be read: {error} (opening the list reads them again)",
                  host=self.host_words(), error=" ".join(error.split())[-200:] or tr("no answer"))

    def offered_templates(self):
        """{image: template row} of sandbox-ai's own Containerfiles the board's host has that are not one of your
        images by that name (one that is - built already, or another image of yours of that name - is yours)."""
        mine = {str(row.get("name")) for row in getattr(self, "user_images", None) or [] if isinstance(row, dict)}
        return {name: entry for name, entry in (getattr(self, "image_templates", None) or {}).items()
                if name not in mine}

    def fill_image_combo(self, combo, current):
        """COMBO (image_combo) with your images as known now, sandbox-ai's own templates, why agent images are none,
        the Images page, "Other…" last, CURRENT chosen or typed in."""
        text = str(current or "")
        combo.blockSignals(True)
        combo.clear()

        def note(words, tooltip=None):
            combo.addItem(words, None)
            combo.model().item(combo.count() - 1).setEnabled(False)
            if tooltip:
                combo.setItemData(combo.count() - 1, safe_tooltip(tooltip), Qt.ItemDataRole.ToolTipRole)
        rows = getattr(self, "user_images", None)
        for entry in rows or []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                kind = {"pulled": tr("pulled"), "built": tr("built")}.get(entry.get("kind"), "")
                own = entry.get("builtin")
                combo.addItem(f"{entry['name']} · " + (f"{kind} · " if kind else "")
                              + (tr("from sandbox-ai's {template}", template=printable(own)) if isinstance(own, str)
                                 else printable(entry.get("ref") or entry.get("image") or "")), entry["name"])
                combo.setItemData(combo.count() - 1, safe_tooltip(printable(entry.get("image") or "")),
                                  Qt.ItemDataRole.ToolTipRole)
        if rows is None:
            note(tr("Reading your images (sandbox-ai images list)…"))
        elif getattr(self, "user_images_failed", False):
            note(self.images_failed_text())
        elif not rows:
            note(tr("No images of yours yet on {host} (the Images page adds one)", host=self.host_words()))
        for name, entry in sorted(self.offered_templates().items()):
            combo.addItem(tr("{name} · sandbox-ai's own, built when needed", name=printable(name)), name)
            combo.setItemData(combo.count() - 1, safe_tooltip(tr(
                "{description}. sandbox-ai's own Containerfile ({template}): built on {host} when a start needs it "
                "(you are asked first; the output is in the tab Preparing the start), its FROM pinned by digest. "
                "UNCHECKED like your images.", description=printable(str(entry.get("description") or "")).rstrip("."),
                template=printable(str(entry.get("template") or name)), host=self.host_words())),
                Qt.ItemDataRole.ToolTipRole)
        note(tr("Agent images and their variants are not service images: they run an agent under the sandbox's "
                "supervisor, not a program beside it"),
             tr("A service is a container of an image of yours, pinned when it was added (sandbox-ai images pull or "
                "build-image) with the user it runs as and its ports. An agent's image or a variant (Images page, "
                "sandbox-ai build) starts the sandbox's own supervisor - its firewall and proxy need the capabilities "
                "a service never gets - so it cannot run as one; a variant is chosen in the Engine part. The images "
                "of MCP servers are their own (MCP page)."))
        combo.addItem(tr("Add a service image (Images page)…"), ADD_IMAGE)
        combo.addItem(tr("Other…"), OTHER_IMAGE)
        index = combo.findData(text) if text else -1
        combo.setCurrentIndex(index)
        if index < 0:
            combo.setEditText(text)
        combo.blockSignals(False)

    def user_image_names(self):
        """The names of your images, or None while they are not known (the check of the services leaves it then)."""
        rows = getattr(self, "user_images", None)
        if rows is None or getattr(self, "user_images_failed", False):
            return None
        return {str(row.get("name")) for row in rows if isinstance(row, dict)}

    def template_names(self):
        """The images of sandbox-ai's own Containerfiles on the board's host (built when a start needs one)."""
        return set(getattr(self, "image_templates", None) or {})

    def service_rows(self, node):
        """The service node's properties: its name, one of your images (or add one), ports, readiness, variables,
        secrets (names only), data folders, user, read-only root and limits; edited as the spec sandbox-ai reads."""
        node_id = node.id
        if getattr(self, "user_images", None) is None:
            self.load_user_images()
        props = dict(node.props)
        warning = plain_label(tr("UNCHECKED: the image of a service has been reviewed by nobody. The service runs "
                                 "beside the agent, in the sandbox's pod, behind the same firewall and connection proxy; "
                                 "the agent reaches it at localhost:PORT. Treat what it returns as untrusted."),
                              role="notice-error", selectable=True)
        self.inspector.addRow(warning)
        if props.get("raw"):
            self.inspector.addRow(tr("As given"), plain_label(printable(props["raw"]), selectable=True))
            if props.get("error"):
                self.inspector.addRow(plain_label(printable(props["error"]), role="notice-error", selectable=True))
            return

        def update(**changes):
            current = self.board.node(node_id)
            if current is None:
                return
            merged = {**current.props, **changes}
            if merged == current.props:
                return

            def apply():
                current.props = merged
            self.change(tr("Change {name}", name=tr("Service (container)")), apply)

        name = QLineEdit(str(props.get("name", "")))
        name.setPlaceholderText(tr("e.g. db or cache"))
        name.editingFinished.connect(lambda: update(name=name.text().strip()))
        self.inspector.addRow(tr("Name"), name)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        image = self.image_combo(props.get("image"))

        def image_chosen(_index=None):
            if getattr(image, "adding", False):
                image.adding = False
                return  # the Images page: nothing chosen, the image stays
            if image.currentData() == OTHER_IMAGE:
                return  # "Other…": the field is emptied for a name typed in
            data = image.currentData()
            text = data if isinstance(data, str) and image.currentText() == image.itemText(image.currentIndex()) \
                else image.currentText()
            update(image=str(text or "").strip())
            entry = next((e for e in getattr(self, "user_images", None) or [] if e.get("name") == text), None)
            current = self.board.node(node_id)
            if entry is not None and current is not None and not current.props.get("ports") and entry.get("ports"):
                update(ports=[p for p in entry["ports"] if isinstance(p, int)][:16])  # the ports the image names
        image.activated.connect(image_chosen)
        image.lineEdit().editingFinished.connect(image_chosen)
        row.addWidget(image, 1)
        add = QPushButton(tr("Add…"))
        add.setToolTip(tr("Add one of your images: from a registry (pulled once, pinned by digest) or your own "
                          "Containerfile"))
        menu = QMenu(add)
        menu.addAction(tr("Add from a registry…"), lambda: self.add_user_image(node_id, "pull"))
        menu.addAction(tr("Add from a Containerfile…"), lambda: self.add_user_image(node_id, "build"))
        add.setMenu(menu)
        row.addWidget(add)
        self.inspector.addRow(tr("Image"), holder)
        if getattr(self, "user_images", None) is None:
            self.inspector.addRow(plain_label(tr("Reading your images (sandbox-ai images list)…"), role="secondary"))
        elif getattr(self, "user_images_failed", False):
            self.inspector.addRow(plain_label(self.images_failed_text(), role="notice-error", selectable=True))
        entry = next((e for e in getattr(self, "user_images", None) or [] if e.get("name") == props.get("image")), None)
        if entry is not None:
            self.inspector.addRow(tr("Pinned as"), plain_label(printable(entry.get("image") or ""), selectable=True))
        template = self.offered_templates().get(props.get("image")) if entry is None else None
        if template is not None:
            self.inspector.addRow(explanation(tr(
                "{name} is sandbox-ai's own Containerfile ({description}): it is built on {host} when a start needs "
                "it - you are asked first, the output is in the tab Preparing the start - and pinned like your images "
                "(UNCHECKED).", name=printable(props.get("image")),
                description=printable(str(template.get("description") or "")).rstrip("."), host=self.host_words()),
                selectable=True))

        def number_list(text):
            return [int(part) for part in re.split(r"[\s,]+", text.strip()) if part.isdigit()]
        ports = QLineEdit(", ".join(str(p) for p in props.get("ports") or []))
        ports.setPlaceholderText(tr("e.g. 5432 (the agent reaches it at localhost:5432)"))
        ports.editingFinished.connect(lambda: update(ports=number_list(ports.text())))
        self.inspector.addRow(tr("Ports"), ports)
        ready = QLineEdit("" if props.get("ready") is None else str(props["ready"]))
        ready.setPlaceholderText(tr("the first port; none: do not wait"))
        ready.setToolTip(tr("The agent starts once this port accepts connections"))

        def ready_value():
            text = ready.text().strip()
            return int(text) if text.isdigit() else "none" if text == "none" else None
        ready.editingFinished.connect(lambda: update(ready=ready_value()))
        self.inspector.addRow(tr("Ready when"), ready)
        timeout = QLineEdit(str(props.get("ready_timeout", B.SERVICE_DEFAULTS["ready_timeout"])))
        timeout.editingFinished.connect(lambda: update(ready_timeout=int(timeout.text()) if timeout.text().strip()
                                                       .isdigit() else B.SERVICE_DEFAULTS["ready_timeout"]))
        self.inspector.addRow(tr("Wait at most (s)"), timeout)
        # Its variables, secrets and data folders: one field of kinds (list_fields; each value a line of before).
        def env_values(lines):
            found = []
            for line in lines:
                key, has_value, value = line.partition("=")
                found.append({"name": key.strip(), "value": value} if has_value else
                             {"name": key.strip(), "from_host": True})
            return found

        def secret_values(lines):
            found = []
            for line in lines:
                key, _, secret = line.partition("=")
                found.append({"name": key.strip(), "secret": secret.strip() or key.strip()})
            return found

        def lists_changed(key, values):
            if key == "env":
                update(env=env_values(values))
            elif key == "secrets":
                update(secrets=secret_values(values))
            else:
                update(data=[v.strip() for v in values if v.strip()],
                       keep=bool(values) and bool(self.board.node(node_id).props.get("keep")))
        lists = LF.ListFields([
            LF.ListKind("env", tr("Variables"), placeholder="KEY=VALUE", tip=tr(
                "KEY=VALUE per line (nothing secret: it is in the project file); KEY alone passes your variable")),
            LF.ListKind("secrets", tr("Secrets"), placeholder="KEY=SECRET", tip=tr(
                "KEY=SECRET per line: KEY gets the secret SECRET of sandbox-ai secrets (only its name is kept)")),
            LF.ListKind("data", tr("Data folders"), placeholder="/data", tip=tr(
                "a writable folder per line, e.g. /data"))], {
            "env": [(f"{e['name']}" if e.get("from_host") else f"{e['name']}={e.get('value', '')}")
                    for e in props.get("env") or []],
            "secrets": [e["name"] if (e.get("secret") or e["name"]) == e["name"] else f"{e['name']}={e['secret']}"
                        for e in props.get("secrets") or []],
            "data": list(props.get("data") or [])}, name=tr("Variables, secrets, data folders"))
        if self.list_kinds.get("service"):
            lists.choose_kind(self.list_kinds["service"])
        lists.kind_chosen.connect(lambda key: self.list_kinds.__setitem__("service", key))
        lists.changed.connect(lists_changed)
        lists.set_provider("secrets", functools.partial(self.secret_suggestions, "--secret-env"))
        self.inspector.addRow(tr("Variables, secrets, data folders"), lists)
        for key in ("env", "secrets", "data"):
            self.option_fields[f"service:{key}"] = lists  # (where the keyboard was, when the rows are made anew)
        keep = QCheckBox(tr("keep the data with the start (a Podman volume; else a tmpfs that goes with the run)"))
        keep.setChecked(bool(props.get("keep")))
        keep.setEnabled(bool(props.get("data")))
        keep.toggled.connect(lambda on: update(keep=bool(on)))
        self.inspector.addRow("", keep)
        user = QLineEdit(str(props.get("user") or ""))
        user.setPlaceholderText(tr("the image's own user; UID or UID:GID"))
        user.editingFinished.connect(lambda: update(user=user.text().strip() or None))
        self.inspector.addRow(tr("User"), user)
        read_only = QCheckBox(tr("read-only root file system"))
        read_only.setChecked(props.get("read_only", True) is not False)
        read_only.toggled.connect(lambda on: update(read_only=bool(on)))
        self.inspector.addRow("", read_only)
        for key, label, hint in (("memory", tr("Memory"), "512m"), ("cpus", tr("CPUs"), "1"),
                                 ("pids", tr("Processes"), "256")):
            edit = QLineEdit(str(props.get(key, B.SERVICE_DEFAULTS[key])))
            edit.setPlaceholderText(hint)

            def changed(edit=edit, key=key):
                text = edit.text().strip() or str(B.SERVICE_DEFAULTS[key])
                update(**{key: int(text) if key == "pids" and text.isdigit() else text})
            edit.editingFinished.connect(changed)
            self.inspector.addRow(label, edit)
        self.inspector.addRow(tr("Option"), plain_label("--service " + B.service_spec(self.board.node(node_id).props),
                                                        selectable=True))

    def add_user_image(self, node_id, how):
        """Add one of your images from the service's properties (the Images page's dialogs, its command in the
        background); the service then uses it."""
        from .images_page import BuildImageDialog, PullDialog
        host = self.host()
        taken = sorted(self.user_image_names() or ())
        dialog = PullDialog(self, taken) if how == "pull" else \
            BuildImageDialog(self, taken, local=host == LOCAL, host=host)
        if not widgets.exec_dropped(dialog):
            return
        name = dialog.chosen_name() if how == "pull" else dialog.name.text().strip()
        command = AsyncCommand(timeout=7200 if how == "build" else 3600, parent=self)
        self.image_commands = [c for c in getattr(self, "image_commands", []) if c is not command] + [command]

        def done(status, out, err):
            self.image_commands = [c for c in self.image_commands if c is not command]
            if status != 0:
                warn(self, tr("Board"), tr("sandbox-ai did not add the image {name}.", name=name),
                     details=printable_text((err or out).strip()[-3000:]), modal=False)
                return
            self.user_images = None
            self.load_user_images()
            current = self.board.node(node_id)
            if current is not None and host == self.host():
                self.set_prop(node_id, "image", name)
            self.show_notice(tr("{name} is added, pinned. It is UNCHECKED: nobody reviewed it.", name=name))
        command.finished.connect(done)
        command.start(self.sai.host_command(host, *dialog.argv()))
        self.show_notice(tr("Adding the image {name}…", name=name))

    def engine_rows(self, node):
        """Beside the engine's options: the chosen variant's base, when it is one of your images (UNCHECKED)."""
        variant = node.props.get("--image-variant")
        if getattr(self, "image_bases", None) is None:
            self.load_user_images()
        base = (getattr(self, "image_bases", None) or {}).get(variant) if variant else None
        if base:
            self.inspector.addRow(plain_label(tr(
                "The variant {name} is built on your image {base}, UNCHECKED: the sandbox's own supervisor (its "
                "firewall, connection proxy and approvals) runs from that image's programs. The security modes high "
                "and paranoid refuse it.", name=printable(variant), base=printable(base)), role="notice-error",
                selectable=True))

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
        self.inspector.addRow(OL.shown(tr("Kits (--kit)")), holder)

    def allow_past_tor(self):
        """The launcher refused a model server of the local network behind Tor and named the --vpn-bypass-tcp it
        needs: after one question, exactly that address and port go into the VPN part."""
        target = self.bypass_target
        # The board changed since the verdict that offered it (an edit meanwhile): the next verdict says anew.
        if not target or self.bypass_state != self.board.state():
            return
        if not confirm(self, tr("Past Tor"), tr(
                "Reach {target} directly, past Tor? Only this address and port of your network: the rest of your "
                "network stays closed, everything else still goes through Tor, and the connection proxy still checks "
                "and logs it. Whatever listens there (a model server, your machine) could reach the internet from "
                "your own address.", target=target), tr("Allow")):
            return

        def update():
            vpn = self.board.first("vpn")
            if vpn is None:  # Tor from a preset or the project file: the option still goes with the VPN part
                vpn = self.board.add("vpn")
                vpn.x, vpn.y = B.snap(40.0, self.grid()), B.snap(40.0 + 110.0 * len(self.board.nodes) % 700,
                                                                self.grid())
            values = list(vpn.props.get("--vpn-bypass-tcp") or [])
            if target not in values:
                vpn.props["--vpn-bypass-tcp"] = values + [target]
        self.change(tr("Reach {target} past Tor", target=target), update)

    def keep_secret(self):
        """The launcher refused a start for a secret that is not kept (--api-key host=NAME ...): the value is asked for
        in a password field and kept with the launcher's own `secrets set NAME` (on this board's host; the value goes
        on its standard input, never on a command line), then the board is checked again."""
        import functools
        # The board changed since the verdict that offered it (an edit meanwhile): the next verdict says anew.
        if not self.offered_keys or self.secret_state != self.board.state():
            return
        name, host = self.offered_keys[0], self.host()
        if not self.sai.valid_secret(name):
            return
        where = tr("on {host}", host=printable(host)) if host != LOCAL else ""
        value = widgets.ask_secret_value(
            self, tr("Keep the key"), tr("The value of {name}{where}: it is kept as a secret of sandbox-ai "
                                         "(sandbox-ai secrets set {name}) and put into the request by the connection "
                                         "proxy; it is never shown or logged.", name=printable(name),
                                         where=(" " + where) if where else ""), tr("Keep"))
        if not value:
            return
        if not self.sai.valid_secret(name, value):
            warn(self, tr("Keep the key"), tr("That is no usable value: one line of printable characters."))
            return
        # The app's one policy for keeping a secret (widgets.ask_first): the launcher's verdict of this board's state
        # said it is not kept, so nothing is replaced and nothing is asked; were it kept, Replace would be asked.
        if not ask_first(self, "secret-replace", tr("Keep the key"), tr(
                "A secret {name} is kept already: replace its value? Everything that names it gets the new one.",
                name=printable(name)), tr("Replace"), detail={"exists": False}):
            return
        self.secret_button.setEnabled(False)

        def kept(result, error):
            if alive(self):
                self.secret_button.setEnabled(True)
                status, out, err = result if error is None else (1, "", str(error))
                applog.log("INFO" if status == 0 else "WARNING", "board",
                           f"keep the secret {name}: " + ("kept" if status == 0 else "not kept"))
                if status == 0:
                    self.show_notice(tr("Kept the secret {name}.", name=printable(name)))
                    self.check()
                else:
                    warn(self, tr("Keep the key"), tr("Not kept: {error}", error=printable_text(
                        (err or out).strip())[-500:] or tr("exit status {status}", status=status)))
        # (Handed over, not referred to by a closure: nothing else holds the value once it has reported.)
        background(functools.partial(self.sai.set_secret, name, value, host=host), owner=self, work=True,
                   timeout=120, done=kept)
        value = None  # noqa: F841

    def clone_to_project(self, directory=None):
        """The board shown now as the board of another project folder DIRECTORY (asked for): the same parts and
        options, its workspace that folder instead of this one; not applied yet (Apply writes its .sandbox-ai.json
        after showing it). A folder that has a project file already is opened, never overwritten."""
        host = self.host()
        remote = host != LOCAL  # a folder of that host: its path as it is there, never resolved or looked at here
        if directory is None:
            directory = RC.choose_folder(self, host, tr("Folder of the new project"), "project", sai=self.sai)
        if not directory:
            return False
        if remote:
            return self.clone_on_host(host, str(directory).strip())
        directory = os.path.abspath(os.path.expanduser(str(directory)))
        if self.key_of(directory) == self.key_of(self.directory):
            inform(self, tr("Clone"), tr("Choose another folder than this project's."))
            return False
        if os.path.exists(os.path.join(directory, ".sandbox-ai.json")):
            warn(self, tr("Clone"), tr("{path} has a project file already: open it instead (Open project folder…).",
                                       path=printable(directory)))
            return False
        # That folder's board kept with changes not applied (its tab, another project's board) is replaced: asked.
        if self.dirty_for(directory) and not ask_first(
                self, "board-discard", tr("Changes not applied"),
                tr("The board of {path} has changes that are not applied. Drop them?", path=printable(directory)),
                tr("Drop the changes"), reject=tr("Keep them")):
            return False
        old_directory = self.directory
        board = B.Board.from_state(self.board.state())
        # Apply writes into the first workspace: that one becomes the folder (also when it was not this project's
        # folder, or the board had none - the clone's Apply wrote into the old workspace then), and every workspace
        # that was the same folder with it.
        main = next((n for n in board.included() if n.kind == "workspace" and str(n.props.get("path", "")).strip()),
                    None)
        before = main.props.get("path") if main is not None else old_directory
        for node in board.of_kind("workspace"):
            if node is main or (before and self.key_of(node.props.get("path")) == self.key_of(before)):
                node.props["path"] = directory
        key = self.key_of(directory)
        self.restored.add(key)  # a draft once kept for that folder does not replace the clone
        # A clone of a board from a project file nobody trusted is not trusted either: it carries that file's keys no
        # part of the board shows (its security mode, canaries, ...), and Start ran them unasked. Apply (it shows
        # everything it trusts) lifts it, as for the original.
        untrusted = self.untrusted.get(self.key_of(old_directory)) if old_directory else None
        self.sources[key] = {"digest": None, "trusted": not untrusted}
        if untrusted:
            self.untrusted[key] = untrusted
        else:
            self.untrusted.pop(key, None)
        self.load(board, directory, positioned=True)
        self.undo.resetClean()  # not applied: Apply writes the project file
        self.show_notice(tr("A clone of {old}: Apply writes .sandbox-ai.json into {new}.",
                            old=printable(old_directory or tr("this board")), new=printable(directory)), "notice-ok")
        return True

    def clone_to_session(self):
        """The next Start starts this setup as a new session: as a session, under a new codename (or the name
        typed)."""
        controls = self.start_controls
        controls.as_session.setChecked(True)
        controls.name.clear()
        controls.new_name()
        controls.name.setFocus()
        self.show_notice(tr("Start now starts this setup as the new session {name} (or the name you type).",
                            name=controls.suggestion or "…"), "notice-ok")
        return True

    def enable_kit(self, directory, name):
        """Use kit NAME in the project DIRECTORY: the board of that project gets it in its kits node, and Apply
        writes .sandbox-ai.json after showing the change and trusts exactly that."""
        self.activate(directory)  # the board shown until now is kept for its project
        node = self.board.first("kits")
        if node is not None and name in (node.props.get("--kit") or []):
            short_notice(self, tr("Board"), tr("{path} uses the kit {name} already.", path=printable(directory),
                                               name=printable(name)))
            return False

        def update():
            target = self.board.first("kits")
            if target is None:
                target = self.board.add("kits")
                target.x, target.y = self.free_place(40.0, skip=target.id)
            target.props["--kit"] = list(target.props.get("--kit") or []) + [name]
        self.change(tr("Use the kit {name}", name=name), update)
        return self.apply()

    def live_rows(self, node):
        """A running sandbox of this project can get the devices and limits now (sandbox-ai devices add /
        limits): clearly marked as live, until that sandbox ends; the project file is not changed by it. Which
        sandbox runs it is read in the background (sandbox-ai's registry may ask Podman); the rows come when it is
        known, if the inspector still shows this node."""
        workspaces = self.board.of_kind("workspace")
        self.live_token = getattr(self, "live_token", 0) + 1
        if not workspaces:
            return
        token = self.live_token
        path, host = workspaces[0].props.get("path"), self.host()
        if host != LOCAL:  # the start of that host whose workspace this is (sandbox-ai list --json there)
            background(self.sai.host_instances, host, owner=self, timeout=90, work=False,
                       done=lambda found, error: token == self.live_token and error is None and self.live_rows_found(
                           node, remote_run_for(found, path), host))
            return
        background(self.sai.run_for, path, owner=self, timeout=60, work=False,
                   done=lambda run, error: token == self.live_token and error is None and self.live_rows_found(node, run))

    def live_rows_found(self, node, run, host=LOCAL):
        if not run:
            return
        label = plain_label(tr("Live, until the sandbox ends: {run} runs this project now.", run=printable(run))
                            if host == LOCAL else
                            tr("Live, until the sandbox ends: {run} runs this project on {host} now.",
                               run=printable(run), host=printable(host)), role="notice-warn")
        self.inspector.addRow(label)
        if node.kind == "devices":
            button = QPushButton(tr("Add these devices to {run} now", run=printable(run)))
            button.clicked.connect(lambda: self.live_devices(run, list(node.props.get("--device", [])), button, host))
        else:
            button = QPushButton(tr("Apply these limits to {run} now", run=printable(run)))
            button.clicked.connect(lambda: self.live_limits(run, node.props, button, host))
        self.inspector.addRow(button)
        if node.kind == "devices":  # (below the part's whole list: what the run has now, one device more or less)
            from .live_devices import LiveDevices
            live = self.live_devices_part = LiveDevices(self.sai, run, host)
            live.added.connect(lambda value, node_id=node.id: self.live_device_added(node_id, value))
            self.inspector.addRow(live)

    def live_device_added(self, node_id, value):
        """VALUE was given to the running sandbox: the board's later starts get it too only when the human says so."""
        node = self.board.node(node_id)
        if node is None or value in (node.props.get("--device") or []):
            return
        if not confirm(self, tr("Board"), tr("Also give {value} to the later starts of this board (its Devices part)?",
                                             value=printable(value)),
                       tr("Also for later starts"), reject=tr("Only this run")):
            return

        def update():
            node.props["--device"] = list(node.props.get("--device") or []) + [value]
        self.change(tr("Change {name}", name="--device"), update)

    def live_devices(self, run, devices, button, host=LOCAL):
        if not devices:
            inform(self, tr("Board"), tr("Add devices to the node first."))
            return
        text = tr("Give {list} to {run} until it ends? Devices weaken the isolation.",
                  list=", ".join(map(printable, devices)), run=printable(run))
        if not ask_first(self, "device-add", tr("Add device") if host == LOCAL else
                         tr("Add device on {host}", host=printable(host)),
                         text if host == LOCAL else on_host(host, text), tr("Add")):
            return
        # (this machine's as before; another host's with host=)
        add = self.sai.add_device if host == LOCAL else functools.partial(self.sai.add_device, host=host)
        background(lambda: [add(run, d) for d in devices], owner=self, source=button,
                   timeout=300, work=True, done=lambda results, error: self.live_done(tr("Add device"), results, error))

    def live_limits(self, run, props, button, host=LOCAL):
        values = [props.get(o) for o in ("--memory", "--cpus", "--pids")]
        if not any(values):
            inform(self, tr("Board"), tr("Set a limit on the node first."))
            return
        # More than it has now is asked first (the limits now read in the background; not known: asked too).
        background(self.sai.limits_shown, run, *([] if host == LOCAL else [host]), owner=self, source=button,
                   timeout=90,
                   done=lambda current, error: self.live_limits_known(run, values, button,
                                                                      current if error is None else None, host))

    def live_limits_known(self, run, values, button, current, host=LOCAL):
        memory, cpus, pids = ("" if value in (None, "") else str(value) for value in values)
        wanted = {"memory": memory, "cpus": cpus, "pids": pids}
        labels = {"memory": tr("memory"), "cpus": tr("CPUs"), "pids": tr("processes")}
        changes = ", ".join(tr("{limit}: no limit", limit=labels[key]) if wanted[key].lower() == "none" else
                            tr("{limit}: {value}", limit=labels[key], value=printable(wanted[key]))
                            for key in backend.raised_limits(current, wanted))
        text = tr("Let {run} use more of its host until it ends: {changes}?", run=printable(run), changes=changes)
        if current is None:
            text += " " + tr("The limits it has now are not known: this may be more than now.")
        if not ask_first(self, "limits-raise", tr("Limits") if host == LOCAL else
                         tr("Limits of {run} on {host}", run=printable(run), host=printable(host)),
                         text if host == LOCAL else on_host(host, text), tr("Raise"),
                         detail={"current": current, "values": wanted}):
            return
        limit = self.sai.set_limits if host == LOCAL else functools.partial(self.sai.set_limits, host=host)
        background(lambda: [limit(run, *values)], owner=self, work=True, source=button,
                   timeout=150, done=lambda results, error: self.live_done(tr("Limits"), results, error))

    def live_done(self, title, results, error):
        if error is not None:
            warn(self, title, printable_text(error))
            return
        failed = [(out + err).strip() for status, out, err in results if status != 0]
        text = "\n".join(printable_text((out + err).strip()) for _, out, err in results)
        (warn if failed else inform)(self, title, text)

    # -- the agent's model server: its own provider or one of yours (sandbox-ai llm) --

    def set_llm_servers(self, servers, host=LOCAL, asking=False):
        """SERVERS of HOST for the Model choice: the list (ASKING: their models are being asked for now) or the list
        with the models they answered."""
        self.llm_loading = bool(asking)
        if host != self.host():  # another host was chosen meanwhile: its servers, never these
            self.llm_loading = False
            self.load_llm_servers()
            return
        self.llm_servers = servers if isinstance(servers, list) else []
        self.llm_asking = bool(asking)
        if self.inspected in self.items and self.board.node(self.inspected).kind in ("agent", "llm"):
            self.inspect_timer.start()  # (the LLM part: its use case's choice among the servers' models)

    def load_llm_servers(self):
        """The model servers of the host it starts on, in the background and in two steps: every one registered there
        (sandbox-ai llm list), shown at once; then each asked for its models (llm models NAME, all at once with a
        short timeout: backend.host_llm_servers), shown when they answered. A server that did not answer stays listed,
        with why."""
        if self.llm_loading:
            return
        host = self.host()
        self.llm_loading = True

        def listed(result, error):
            # Not read (a host that does not answer): said under the choice and asked again after HOST_RETRY seconds
            # when the part is looked at - it read as "no model servers" until another host was chosen.
            self.llm_problem = None if error is None else (printable_text(str(error))[-200:] or "?")
            self.llm_failed = time.monotonic()
            if error is not None or not isinstance(result, list) or not result:
                self.set_llm_servers(result if error is None else [], host)
                return
            self.set_llm_servers(result, host, asking=True)
            if host != self.host():
                return
            background(lambda: self.sai.host_llm_servers(host, result), timeout=150,
                       done=lambda found, failed: self.set_llm_servers(found if failed is None else result, host))
        background(lambda: self.sai.host_llm_endpoints(host), timeout=90, done=listed)

    def llm_server_row(self, node):
        """Beside the agent: the model server it works with, its own provider (cloud) or a registered one as NAME or
        NAME:MODEL (wizard_llm). A server puts the LLM server node on the board with --llm and takes the agent's own
        model away (the launcher refuses --model with --llm); "only this server" is --no-provider-defaults."""
        from . import wizard_llm as WL
        from .wizard import fill_model_combo
        problem = getattr(self, "llm_problem", None)
        if self.llm_servers is None or (problem and time.monotonic() - getattr(self, "llm_failed", 0) >= HOST_RETRY):
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
        # The picker: every model with search, sorts by name, parameters and size, and its tags (model_picker.py)
        from .model_picker import picker_button
        picker = picker_button(combo, lambda: WL.picker_rows(self.llm_servers or [], node.props.get("agent", "claude"),
                                                             self.sai), lambda _value: chosen())
        row.addWidget(combo, 1)
        row.addWidget(picker)
        row.addWidget(only)
        self.llm_picker = picker
        self.inspector.addRow(tr("Model server"), holder)
        if self.llm_servers is None:
            self.inspector.addRow(plain_label(tr("Reading the model servers (sandbox-ai llm list)…"), role="secondary"))
        elif problem:
            self.inspector.addRow(plain_label(tr(
                "The model servers of {host} could not be read: {error}",
                host=tr("this computer") if self.host() == LOCAL else printable(self.host()), error=problem),
                role="notice-warn", selectable=True))
        elif getattr(self, "llm_asking", False):
            self.inspector.addRow(plain_label(tr("Asking the model servers for their models (sandbox-ai llm models)…"),
                                              role="secondary"))
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
                    node.x, node.y = self.free_place(40.0 + B.KINDS[node.kind].column * 260.0, skip=node.id)
        self.change(tr("Model server"), update)

    def model_row(self, node):
        """The agent's model: its models with their versions to choose from, grouped by provider, typing filters them
        (models.py: sandbox-ai's table of cloud models), any other typed (the checks say it is not in the known
        list)."""
        from . import models
        combo = QComboBox()
        combo.setEditable(True)
        combo.setAccessibleName(tr("Model"))
        combo.setToolTip(tr("passed as --model to the agent"))
        models.fill_combo(combo, node.props.get("agent", "claude"), str(node.props.get("model", "") or ""), self.sai)
        combo.activated.connect(lambda _i: self.set_model(node.id, models.combo_value(combo)))
        combo.lineEdit().editingFinished.connect(lambda: self.set_model(node.id, models.combo_value(combo)))
        from . import wizard_llm as WL
        used = WL.used_model(self.board, self.llm_servers)
        if used is not None:  # the launcher refuses --model with --llm: the model is the server's, shown as such
            model, source = used
            name, _ = WL.parse_value(self.board.first("llm").props.get("--llm"))
            shown = {"named": tr("{model} (named with the server)", model=model),
                     "default": tr("{model} (the default model of {name})", model=model, name=name),
                     "only": tr("{model} (the only model of {name})", model=model, name=name)}.get(
                source, tr("the default of {name} (the server decides; choose NAME:MODEL to name one)", name=name))
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(printable(shown), "")
            combo.blockSignals(False)
            combo.setEnabled(False)
            combo.setToolTip(tr("With a model server the model is the server's: choose NAME:MODEL as the model "
                                "server"))
        self.inspector.addRow(tr("Model"), combo)
        self.model_combo = combo
        return combo

    def set_model(self, node_id, model):
        """The agent part's model is MODEL: one step of Undo, which also takes an effort the new model does not take
        (said below the Effort choice and in the log; Undo brings both back)."""
        node = self.board.node(node_id)
        model = str(model or "").strip()
        if node is None or str(node.props.get("model") or "") == model:
            return
        dropped = []
        self.change(tr("Change {name}", name=tr("Model")),
                    lambda: dropped.append(B.set_model(node.props, model, self.sai)))
        if dropped and dropped[0]:
            self.dropped_effort = (node_id, model, dropped[0])
            self.show_notice(self.dropped_effort_text(), "notice-warn")
        else:
            self.dropped_effort = None

    def dropped_effort_text(self):
        _, model, level = self.dropped_effort
        return tr("The effort {level} was reset: {model} does not take it (Undo brings it back).", level=level,
                  model=model or tr("the agent's default model"))

    def effort_row(self, node):
        """How hard the agent thinks (--effort): the levels the chosen model takes (models.efforts: the table of cloud
        models; a model it does not list: the agent's levels), the agent's default first. Greyed out, with where it is
        set instead, for an agent without --effort."""
        from . import guide, models
        agent = node.props.get("agent", "claude")
        model = str(node.props.get("model") or "").strip()
        current = str(node.props.get("effort") or "")
        combo = QComboBox()
        combo.setAccessibleName(tr("Effort"))
        default = models.default_effort(agent, model, self.sai)
        combo.addItem(tr("the agent's default ({level} for this model)", level=default) if default
                      else tr("the agent's default"), "")
        levels = models.efforts(agent, model, self.sai)
        for level in levels:
            combo.addItem(level, level)
        if current and current not in levels:  # (a command line or project file set it: kept, the checks say why)
            combo.addItem(tr("{level} (not taken by this model)", level=current), current)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        if guide.AGENT_EFFORTS.get(agent):
            combo.setToolTip(safe_tooltip(tr("passed as --effort; the levels {model} takes", model=model) if model and
                             models.entry(agent, model, self.sai) else tr("passed as --effort; the agent's levels")))
        else:
            note = models.effort_note(agent, self.sai)
            combo.setToolTip(safe_tooltip(tr("{agent} sets its effort per model in its own configuration",
                                             agent=agent) + (f": {note}" if note else "")))
        combo.setEnabled(bool(guide.AGENT_EFFORTS.get(agent)) or bool(current))
        combo.currentIndexChanged.connect(
            lambda _i, node_id=node.id: self.set_prop(node_id, "effort", combo.currentData() or ""))
        self.inspector.addRow(tr("Effort"), combo)
        dropped = getattr(self, "dropped_effort", None)
        if dropped and dropped[0] == node.id and dropped[1] == model and not current:
            self.inspector.addRow("", plain_label(self.dropped_effort_text(), role="secondary"))
        self.effort_combo = combo
        return combo

    def thinking_row(self, node):
        """Whether the agent shows its model's thinking in its terminal (--show-thinking on|off, the agent's own
        switch; empty: the agent's default, said with it); greyed out with the reason for an agent without a switch
        (Qwen Code) or a launcher without the option."""
        from . import thinking
        agent = node.props.get("agent", "claude")
        combo = QComboBox()
        combo.setAccessibleName(tr("Show thinking"))
        for value, label in thinking.choices(agent):
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(str(node.props.get("thinking") or ""))))
        why = thinking.reason(agent)
        if why is None and not B.launcher_knows(self.sai, thinking.OPTION):
            why = tr("This sandbox-ai has no --show-thinking yet: update it")
        combo.setToolTip(safe_tooltip(why or thinking.tooltip(agent)))
        # (a value a command line or project file set for an agent without the switch stays choosable: to remove it)
        combo.setEnabled(why is None or (not thinking.available(agent) and bool(node.props.get("thinking"))))
        combo.currentIndexChanged.connect(
            lambda _i, node_id=node.id: self.set_prop(node_id, "thinking", combo.currentData() or ""))
        self.inspector.addRow(tr("Show thinking"), combo)
        if why is not None:
            self.inspector.addRow("", plain_label(why, role="secondary"))
        self.thinking_combo = combo
        return combo

    def llm_kind(self, llm):
        """The kind (ollama, lmstudio ...) of the server the LLM part LLM names, as the model servers list says (None:
        not read yet, or not registered here)."""
        from . import wizard_llm as WL
        name, _ = WL.parse_value(llm.props.get("--llm")) if llm is not None else ("", "")
        for endpoint in self.llm_servers or []:
            if isinstance(endpoint, dict) and endpoint.get("name") == name and isinstance(endpoint.get("kind"), str):
                return endpoint["kind"]
        return None

    def agent_llm_rows(self):
        """Beside the agent, with a model server of yours: the model's advanced settings for this board's start (the
        LLM part's --llm-param values: sampling, context, keeping it loaded, LM Studio's load settings ... - what the
        server's kind takes), folded under a button; what only the server's own start sets, shown."""
        llm = self.board.first("llm")
        if llm is None or not self.board.connected(llm) or not str(llm.props.get("--llm") or "").strip():
            return
        holder = QWidget()
        form = QFormLayout(holder)
        form.setContentsMargins(0, 0, 0, 0)
        self.llm_profile_rows(llm)  # the model profile of the model in this agent, its switch and values
        button = QPushButton(tr("Advanced settings of the model…"))
        button.setCheckable(True)
        button.setAccessibleName(tr("Advanced settings of the model"))
        button.setToolTip(tr("Temperature, context length, max. output tokens, keeping the model loaded, LM Studio's "
                             "load settings (Flash Attention, KV cache) and more - only what this server takes - for "
                             "this board's start, over the server's defaults (Advanced settings… on the LLM servers "
                             "page)"))
        opened = bool(getattr(self, "agent_llm_open", False) or (llm.props.get("params") or {}))
        button.setChecked(opened)

        def toggled(on):
            self.agent_llm_open = on
            holder.setVisible(on)
        button.toggled.connect(toggled)
        self.inspector.addRow("", button)
        self.llm_param_rows(llm, form=form)
        self.inspector.addRow(holder)
        holder.setVisible(opened)  # only once in its place (tests/test_no_flash.py)
        self.agent_llm_button, self.agent_llm_box = button, holder

    def use_case_row(self, form=None):
        """The use case of the board's start (--use-case on the LLM part): the use cases of sandbox-ai's local-model
        research, each with its status (measured, from other tasks, not tested yet); choosing one puts its best tested
        pair (model x harness) among the board's server's models on the board - the server and model, the agent - as
        one undo step, and shows the choice with why, its evidence and the "What we know" box of the use case. Below
        it every model of that server as a pair, ranked by the use case's table (use_case_view.PairList; not measured
        and unsafe ones too, with why): Use this pair sets the agent and the model. Overridable: another model or
        agent chosen afterwards stays. Greyed out with why for a sandbox-ai without use cases."""
        from . import use_case as UC
        from . import use_case_view as UCV
        from . import wizard_llm as WL
        from .model_profile_view import KnowledgeBox
        form = form if form is not None else self.inspector
        if self.llm_servers is None:
            self.load_llm_servers()  # (shown again when they answered: set_llm_servers)
        llm = self.board.first("llm")
        current = str((llm.props.get(UC.OPTION) if llm is not None else "") or "")
        agent = self.board.first("agent")
        agent_name = agent.props.get("agent", "claude") if agent is not None else None
        # The board's server (its LLM part's --llm SERVER[:MODEL]): the pairs of its models; none yet: every server's.
        server, model = WL.parse_value(str((llm.props.get("--llm") if llm is not None else "") or ""))
        endpoints = self.llm_servers or []
        own = [e for e in endpoints if isinstance(e, dict) and e.get("name") == server]
        found = UC.recommendations(self.sai, own or endpoints, self.host() == LOCAL, note_agent=agent_name)
        combo = UCV.fill_use_cases(QComboBox(), found, current)
        combo.setAccessibleName(tr("Use case"))
        knows = found is not None and B.launcher_knows(self.sai, UC.OPTION)
        combo.setEnabled(knows)
        combo.setToolTip(safe_tooltip(
            tr("Picks the model and agent sandbox-ai's local-model research found best for this kind of work among "
               "this host's servers (what is loaded, what fits), and its settings for them: --use-case")
            if knows else tr("This sandbox-ai has no use cases yet (sandbox-ai llm recommend): update it")))
        chosen = next((entry for entry in found or [] if entry["use_case"] == current), None)
        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.addWidget(combo)
        text = plain_label(UC.choice_text(chosen) if chosen else "", role="secondary", selectable=True)
        rows.addWidget(text)
        text.setVisible(bool(chosen))  # (in its place first: never a window of its own)
        pairs = UCV.PairList(holder)
        rows.addWidget(pairs)
        pairs.set(chosen, server=server if own else None, current=(server, model) if model else None)
        pairs.picked.connect(lambda pair: self.choose_pair(current, pair))
        box = KnowledgeBox(holder)
        rows.addWidget(box)
        box.set(UC.knowledge(chosen))
        combo.activated.connect(lambda _index: self.choose_use_case(combo.currentData() or "", found or []))
        form.addRow(tr("Use case"), holder)
        self.use_case_combo, self.use_case_text, self.use_case_box, self.use_case_pairs = combo, text, box, pairs
        choice = (chosen or {}).get("choice")
        if agent is not None and choice and choice["harness"] != agent_name:
            # Said as the pairs say it: where it was measured best, and what is known of the model in this agent.
            pair = next((p for p in UC.pairs_of(chosen) if (p["server"], p["model"]) ==
                         (choice["server"], choice["model"])), None)
            note = UC.agent_note(pair) if pair else ""
            text.setText(text.text() + "\n" + (note or tr(
                "The agent chosen is not the one measured: the use case's settings apply where its table has this "
                "model in this agent.")))
        return combo

    def put_pair(self, name, server, model, harness):
        """The pair SERVER:MODEL in HARNESS (None: the board's agent stays) for the use case NAME on the board: the
        LLM part's --llm SERVER:MODEL ("only this server" as it was, else on), the agent, --use-case NAME. Part of
        the caller's undo step."""
        from . import wizard_llm as WL
        from . import use_case as UC
        before = {n.id for n in self.board.nodes}
        current = WL.from_board(self.board)
        WL.apply(self.board, {"name": server, "model": model, "only": current["only"] if current else True})
        agent = self.board.first("agent")
        if agent is not None and harness and agent.props.get("agent", "claude") != harness:
            if harness not in getattr(self.sai, "agents", (harness,)):
                raise ValueError(tr("{agent} is not an agent of this sandbox-ai: choose the agent yourself.",
                                    agent=printable(str(harness))))
            B.switch_agent(agent.props, harness)
        llm = self.board.first("llm")
        llm.props[UC.OPTION] = name
        for node in self.board.nodes:
            if node.id not in before:
                node.x, node.y = B.snap(40.0 + B.KINDS[node.kind].column * 260.0, self.grid()), \
                    B.snap(40.0 + 110.0 * len(self.board.nodes) % 700, self.grid())

    def choose_pair(self, name, pair):
        """The pair PAIR (use_case_view.PairList: a model of the server with its best harness) on the board for the
        use case NAME, as one undo step: the agent and the model (put_pair); the profile follows."""
        if not name or not isinstance(pair, dict):
            return
        self.change(tr("Use this pair"), lambda: self.put_pair(name, pair["server"], pair["model"],
                                                               pair.get("harness")))

    def choose_use_case(self, name, found):
        """The use case NAME (its id; "" none) on the board as one undo step: --use-case on the LLM part and, when the
        use case has a choice (FOUND: use_case.recommendations), its pair: server and model (--llm SERVER:MODEL,
        "only this server" as it was, else on) and its agent. A use case without a choice keeps the server the board
        has (none: refused, said)."""
        from . import use_case as UC

        def update():
            llm = self.board.first("llm")
            if not name:
                if llm is not None:
                    llm.props.pop(UC.OPTION, None)
                return
            entry = next((e for e in found if e["use_case"] == name), None)
            choice = (entry or {}).get("choice")
            if choice:
                self.put_pair(name, choice["server"], choice["model"], choice["harness"])
                return
            llm = self.board.first("llm")
            if llm is None or not str(llm.props.get("--llm") or "").strip():
                raise ValueError(tr("A use case chooses among the models of a server of yours: choose a model "
                                    "server first (Model server), or add one on the LLM servers page."))
            llm.props[UC.OPTION] = name
        self.change(tr("Use case"), update)

    def remote_note_row(self, value, read_only):
        """Under a workspace's or mount's field holding a folder of another machine (sshfs://...): what it means (its
        host mounts it with sshfs; writable, the agent changes files on that machine), or why sandbox-ai refuses it."""
        from . import remote_source
        if not remote_source.is_remote(value):
            return None
        try:
            remote_source.parse(value)
            text, role = remote_source.warning(value, read_only), "notice-warn" if not read_only else "secondary"
        except ValueError as exc:
            text, role = str(exc), "notice-warn"
        note = plain_label(text, role=role, selectable=True)
        self.inspector.addRow("", note)
        return note

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
            # (its label in the app's language where there is one; sandbox-ai's help is English)
            widget = QCheckBox(OL.label(long) if long in OL.OPTION_LABELS or not option.help
                               else elide(option.help.split(";")[0], 70))
            widget.setChecked(bool(value))
            widget.toggled.connect(lambda on: self.set_prop(node_id, long, bool(on)))
        elif option.kind == "choice":
            items = ([""] if option.default is None else []) + [str(c) for c in option.choices]
            if long == "--ask":  # its values in words, "allow" marked and explained below it (ask_note)
                from . import ask_choice as AC
                widget = self.ask_box = AC.AskChoice(items)
            else:
                widget = QComboBox()
                widget.addItems(items)
            widget.setCurrentText(str(value) if value not in (None, "") else ("" if option.default is None
                                                                               else str(option.default)))
            for index, item in enumerate(items):
                if item and item in self.forbidden.get(long, set()):
                    widget.model().item(index).setEnabled(False)
            if long == "--ask":  # the value, not the words shown
                widget.currentIndexChanged.connect(lambda _i, box=widget: self.set_prop(
                    node_id, long, "" if box.currentText() == str(option.default) else box.currentText()))
            else:
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
        elif option.kind == "list" and long in NF_OPTIONS:
            # a secret's name per line: that host's secrets to add from a "+" menu (names only), any other typed
            lines = LinesEdit(value or [])
            lines.changed.connect(lambda values: self.set_prop(node_id, long, values))
            pick = NameButton("secret", self.sai, self.host())
            pick.picked.connect(lambda name, e=lines: e.setPlainText(add_line(e.toPlainText(), name, long)))
            widget = QWidget()
            row = QHBoxLayout(widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(lines, 1)
            row.addWidget(pick, alignment=Qt.AlignmentFlag.AlignTop)
            lines.setToolTip(safe_tooltip(option.help))
        elif long == "--rules":  # ruleset files: what to enter, a starter from the example, the launcher's check
            lines = LinesEdit(value or [])
            lines.setPlaceholderText(rules_tools.placeholder())
            lines.changed.connect(lambda values: self.set_prop(node_id, long, values))
            widget = QWidget()
            column = QVBoxLayout(widget)
            column.setContentsMargins(0, 0, 0, 0)
            column.addWidget(lines)
            agent = self.board.first("agent")
            self.rules_tools = rules_tools.RulesetTools(
                self.sai, lines.values, lambda path, e=lines: e.appendPlainText(path),
                lambda: [n.props.get("path") for n in self.board.of_kind("workspace") if n.props.get("path")],
                local=lambda: self.host() == LOCAL, agent=lambda: agent.props.get("agent") if agent else "claude")
            column.addWidget(self.rules_tools)
        elif long == "--hide":  # globs: the suggested ones a click away
            lines = LinesEdit(value or [])
            lines.setPlaceholderText(rules_tools.hide_placeholder())
            lines.changed.connect(lambda values: self.set_prop(node_id, long, values))
            suggest = QPushButton(tr("Suggested"))
            suggest.setToolTip(safe_tooltip(tr("Add the usual secret files: {globs}",
                                               globs=", ".join(rules_tools.HIDE_SUGGESTED))))
            suggest.clicked.connect(lambda _=False, e=lines: (e.setPlainText("\n".join(dict.fromkeys(
                [*e.values(), *rules_tools.HIDE_SUGGESTED]))), self.set_prop(node_id, long, e.values())))
            widget = QWidget()
            column = QVBoxLayout(widget)
            column.setContentsMargins(0, 0, 0, 0)
            column.addWidget(lines)
            column.addWidget(suggest, alignment=Qt.AlignmentFlag.AlignLeft)
        elif option.kind == "list":
            widget = LinesEdit(value or [], items=long in backend.LIST_OPTIONS)
            if long in backend.LIST_OPTIONS:  # (the option's own help, with how several are given)
                widget.setToolTip(safe_tooltip(f"{option.help}\n{tr('One or more per line, separated by commas or spaces')}"))
            widget.changed.connect(lambda values: self.set_prop(node_id, long, values))
        elif long in VALUE_NAMES:  # what that host has to choose from (its VPN profiles, variants ...), any other typed
            widget = NameField(VALUE_NAMES[long], self.sai, self.host(), placeholder=option.metavar or "")
            widget.setText("" if value is None else str(value))
            widget.setAccessibleName(long)
            widget.editingFinished.connect(lambda: self.set_prop(node_id, long, widget.text().strip()))
            widget.activated.connect(lambda _i: self.set_prop(node_id, long, widget.text().strip()))
        else:
            widget = QLineEdit("" if value is None else str(value))
            widget.setPlaceholderText(option.metavar or (str(option.default) if option.default else ""))
            widget.editingFinished.connect(lambda: self.set_prop(node_id, long, widget.text().strip()))
        # The option's name in the language chosen; the option itself (--…) only while the setting show_flags is on.
        flag = long + (f"  ({option.short})" if option.short else "")
        widget.setToolTip(safe_tooltip(OL.tip(flag, option.help)))
        label = QLabel(flag if OL.flags_shown() else OL.label(long))
        label.setToolTip(safe_tooltip(OL.tip(flag, option.help)))
        if long == "--rules":  # an example in the tooltip of its lines
            lines.setToolTip(rules_tools.tooltip())
        if None in self.forbidden.get(long, set()):
            self.disable_if_forbidden(widget, long)
        self.inspector.addRow(label, widget)
        self.option_fields[long] = widget  # (a finding about it puts the keyboard there: focus_more)

    def list_fields_row(self, node, title, keys, options):
        """The list options KEYS of NODE as one field (LIST_GROUPS): the kinds in a drop-down (each with its label and
        the launcher's help), the values as chips; each change one step of Undo, under the option's own key. The kind
        chosen is kept for this kind of part while the app runs (the inspector is made anew after every change)."""
        node_id = node.id
        agent = node.props.get("agent") if node.kind == "agent" else None
        kinds = []
        for long in keys:
            option = options[long]
            flag = long + (f"  ({option.short})" if option.short else "")
            own = not long.endswith("-enable") or long == f"--{agent}-enable"  # (another agent's: shown, not offered)
            kind = LF.option_kind(option, flag if OL.flags_shown() else OL.label(long), tip=OL.tip(flag, option.help),
                                  offered=own)
            if None in self.forbidden.get(long, set()):
                kind.enabled, kind.why = False, tr("The administrator's policy forbids {option}", option=long)
            if long == "--device":  # (a path, usb:VENDOR:PRODUCT[:SERIAL], or a GPU kind the picker offers)
                kind.check = DP.check_board_value
            kinds.append(kind)
        single = len(keys) == 1
        name = OL.label(keys[0]) if single else tr(title)
        field = LF.ListFields(kinds, {key: node.props.get(key) or [] for key in keys}, name=name)
        chosen = self.list_kinds.get(node.kind)
        if chosen:
            field.choose_kind(chosen)
        field.kind_chosen.connect(lambda key, k=node.kind: self.list_kinds.__setitem__(k, key))
        field.changed.connect(lambda key, values: self.list_changed(node_id, key, values))
        for key in keys:
            provider = self.value_provider(key)
            if provider is not None:
                field.set_provider(key, provider)
        if single:
            option = options[keys[0]]
            flag = keys[0] + (f"  ({option.short})" if option.short else "")
            label = QLabel(flag if OL.flags_shown() else name)
            label.setToolTip(safe_tooltip(OL.tip(flag, option.help)))
        else:
            label = QLabel(name)
            label.setToolTip(safe_tooltip(tr("Choose the kind, type the value, Enter or Add: {kinds}",
                                             kinds=", ".join(kind.label for kind in kinds if kind.offered))))
        self.inspector.addRow(label, field)
        for key in keys:
            self.option_fields[key] = field  # (a finding about one of them puts the keyboard there: focus_more)
        return field

    def value_provider(self, option):
        """What OPTION's values may be chosen from in its list field (list_fields: provider(fill)), or None: the
        board's host's secrets for the options that name one; the board's host's devices for --device."""
        if option in SECRET_SUGGESTIONS:
            return functools.partial(self.secret_suggestions, option)
        if option == "--device":
            return self.device_suggestions
        return None

    def list_changed(self, node_id, key, values):
        """A list field's KEY now holds VALUES. A GPU kind chosen in the device field (device_picker.gpu_value) is the
        part's --gpu, not a --device: one step of Undo for both."""
        gpus = [DP.gpu_of(value) for value in values if DP.gpu_of(value)] if key == "--device" else []
        node = self.board.node(node_id)
        if not gpus or node is None:
            self.set_prop(node_id, key, values)
            return
        rest = [value for value in values if not DP.gpu_of(value)]

        def update():
            if rest:
                node.props["--device"] = rest
            else:
                node.props.pop("--device", None)
            node.props["--gpu"] = gpus[-1]
        self.change(tr("Change {name}", name="--gpu"), update)
        self.inspect_timer.start()  # (the field still shows it as a device: made anew without it)

    def device_suggestions(self, fill, refresh=False):
        """FILL with the devices the board's host has (sandbox-ai devices scan there, device_picker), grouped by kind;
        what the launcher refuses left out, nothing found or the host not answering said in one line."""
        host = self.host()

        def found(entries, error):
            if host != self.host():  # another host was chosen meanwhile: its field asks for its own
                return
            items = DP.board_items(entries or []) if error is None else []
            if None in self.forbidden.get("--gpu", set()):  # (the administrator's policy: no GPU kind offered)
                items = [item for item in items if not DP.gpu_of(item[1])]
            fill(items, DP.note(host, entries or [], error))
        DP.read(self.sai, host, found, owner=self, refresh=refresh)

    def refresh_devices(self):
        """Detect again: the board's host's devices read anew, into the device field and the live part."""
        DP.forget(self.host())
        field = self.option_fields.get("--device")
        if isinstance(field, LF.ListFields):
            field.ask_provider("--device")
        live = getattr(self, "live_devices_part", None)
        if live is not None and alive(live):
            live.reload(refresh=True)

    def secret_suggestions(self, option, fill):
        """FILL with the board's host's secrets (their names only), as OPTION takes them (SECRET_SUGGESTIONS)."""
        from . import name_fields
        host = self.host()

        def read(names, problem):
            if problem:
                fill([], tr("Could not be read: {error}", error=problem))
            elif not names:
                fill([], name_fields.none_yet("secret"))
            else:
                fill([(name, SECRET_SUGGESTIONS[option].format(name=name)) for name in names])
        name_fields.names_of(self.sai, "secret", host, read)

    def ask_note(self):
        """Below --ask: what "Always allow everything" (--ask allow) means, shown while it is chosen."""
        from . import ask_choice as AC
        box = getattr(self, "ask_box", None)
        if box is None:
            return
        note = plain_label(f"{AC.WARN} {tr(AC.ALLOW_HELP)}", role="notice-warn")
        box.currentIndexChanged.connect(lambda _i, b=box, n=note: n.setVisible(AC.warned("--ask", b.currentText())))
        self.inspector.addRow("", note)
        note.setVisible(AC.warned("--ask", box.currentText()))  # (in the inspector first: no window of its own)

    def disable_if_forbidden(self, widget, option):
        if None in self.forbidden.get(option, set()):
            widget.setEnabled(False)
            widget.setToolTip(safe_tooltip(tr("The administrator's policy forbids {option}", option=option)))

    def set_readonly(self, on):
        def update():
            for node in self.board.of_kind("workspace"):
                node.props["readonly"] = bool(on)
        self.change(tr("Change {name}", name="--workspace-ro"), update)

    def mcp_limit_rows(self, node, definition):
        """The MCP part's own limits and variables for this board's starts (--mcp-limits, --mcp-env): empty fields and
        "the server's own" show the server's (its defaults for every start) as such; a value typed in is checked at
        once by sandbox-ai's rules and kept only when valid. Two buttons of their own size, each saying what it does:
        "Use the server's limits" discards this board's limits (the server's defaults hold); "Save as the server's
        defaults…" opens the server's Properties (MCP page) with this board's values filled in, for OK to store them as
        its defaults for every start. A line says the difference."""
        node_id = node.id
        mcp = MF.mcp_module(self.sai)
        defaults = B.mcp_defaults(self.sai, definition) if self.host() == LOCAL else None
        name = str(node.props.get("name") or "")
        fields = MF.LimitsFields(MF.limit_checks(mcp), defaults, node.props.get("limits") or {},
                                 default_text=tr("the server's own"), reset_text=tr("Use the server's limits"),
                                 reset_tip=tr("Discards this board's limits for {name}: its starts take the server's "
                                              "own limits (its defaults for every start) again", name=name or "?"))
        fields.setToolTip(tr("This board's limits for the server (--mcp-limits); empty: the server's own. Other "
                             "limits give this board's starts an instance of the server of their own."))

        def commit():
            values = {key: str(value) for key, value in fields.values().items()}
            self.set_prop(node_id, "limits", values or None)
        fields.committed.connect(commit)
        self.inspector.addRow(tr("Limits for this board"), fields)
        if not B.launcher_knows(self.sai, "--mcp-limits"):
            fields.setEnabled(False)
            self.inspector.addRow("", plain_label(tr("This sandbox-ai has no --mcp-limits: the server runs with its "
                                                     "own limits."), role="notice-warn"))
        variables = None
        if isinstance(definition, dict) and ("image" in definition or "rootfs" in definition) and not \
                node.props.get("project"):
            rows = MF.board_rows(MF.variable_rows(self.mcp_entry(name, definition), definition),
                                 node.props.get("variables"))
            variables = MF.VariablesFields(rows, mcp, self.secret_names(), board=True)
            variables.setToolTip(tr("This board's variables for the server (--mcp-env): a value that is no secret, or "
                                    "a secret, in place of the server's own; other values give this board's starts an "
                                    "instance of the server of their own."))

            def commit_variables():
                self.set_prop(node_id, "variables", variables.specs() or None)
            variables.committed.connect(commit_variables)
            self.inspector.addRow(tr("Variables for this board"), variables)
            if not B.launcher_knows(self.sai, "--mcp-env"):
                variables.setEnabled(False)
                self.inspector.addRow("", plain_label(tr("This sandbox-ai has no --mcp-env: the server runs with its "
                                                         "own variables."), role="notice-warn"))
        if node.props.get("project") or not name:
            return
        self.inspector.addRow("", explanation(tr("These values hold for this board's starts only. \"Use the server's "
                                                 "limits\" discards this board's limits; \"Save as the server's "
                                                 "defaults…\" stores the values shown as the server's own, for every "
                                                 "start.")))
        server = QPushButton(tr("Save as the server's defaults…"))
        server.setToolTip(safe_tooltip(tr("Opens the server's Properties (MCP page) with this board's limits and "
                                          "variables filled in: OK stores them as {name}'s defaults for every start "
                                          "that uses it (every board and project); Cancel changes nothing",
                                          name=name)))

        def save_defaults(_=False):
            values = {"limits": {key: str(value) for key, value in fields.values().items()},
                      "variables": variables.specs() if variables is not None else []}
            self.server_defaults.emit(name, self.host(), values)
        server.clicked.connect(save_defaults)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(server)
        row.addStretch(1)  # its own size, not the inspector's whole width
        self.inspector.addRow("", holder)
        self.server_defaults_button = server

    def mcp_entry(self, name, definition):
        """The catalog entry server NAME (DEFINITION) was added from, once the catalogs are read (load_mcp_catalog):
        the variables it takes, with their descriptions; None before."""
        entries = getattr(self, "mcp_catalog_entries", None)
        if entries is None or getattr(self, "mcp_catalog_host", LOCAL) != self.host():
            self.load_mcp_catalog()
            return None
        source = str((definition or {}).get("source") or "")
        return entries.get(source) or entries.get(source.rpartition(":")[2]) or entries.get(name)

    def secret_names(self):
        """The names of the secrets of the host it starts on (sandbox-ai secrets list), read in the background once
        per host; [] until then."""
        host = self.host()
        known = getattr(self, "secrets_known", {})
        self.secrets_known = known
        if host not in known:
            known[host] = []

            def done(result, error):
                known[host] = [name for name, _where in result[1]] if error is None and result else []
                if known[host] and self.inspected in self.items and self.board.node(self.inspected).kind == "mcp":
                    self.inspect_timer.start()
            background(lambda: self.sai.host_secrets(host), owner=self, done=done, timeout=60)
        return list(known[host])

    def board_profile(self):
        """(profile, enabled, your keys) of the board's model in its agent (model_profile.py), or None."""
        from . import model_profile as MP
        return MP.board_profile(self.sai, self.board, self.llm_servers)

    def profile_badge(self):
        """The LLM part's line of the model profile ("Model profile: qwen3-coder-next × opencode (verified)"), "!"
        first for a pair known not to work; "" without one."""
        from . import model_profile as MP
        try:
            found = self.board_profile()
        except Exception as _exc:  # noqa: BLE001 - a card is never broken by it
            applog.swallowed("board_view", "profile_badge failed (the card shows none)", _exc)
            return ""
        if found is None:
            return ""
        profile, enabled, _ = found
        bad = enabled and profile.get("status") in MP.WARNING_STATUSES
        return ("! " if bad else "") + MP.badge(profile, enabled)

    def llm_profile_rows(self, node, form=None):
        """The model profile of the board's model in its agent (sandbox-ai applies it under your own values): its
        badge, a switch (off: --no-llm-profile), its values with "your value wins" beside the board's and the
        server's own, and its warnings (a pair known not to work: said, never refused)."""
        from . import model_profile as MP
        form = form if form is not None else self.inspector
        found = self.board_profile()
        llm = self.board.first("llm")
        if llm is None:
            return None
        if found is None:  # a model of no family: the box says nothing is known about it
            self.knowledge_row(form)
            return None
        profile, enabled, mine = found
        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 0, 0)
        headline = plain_label(MP.badge(profile, enabled), selectable=True)
        headline.setAccessibleName(tr("Model profile"))
        whole = "\n".join(MP.details(profile, mine, enabled))
        headline.setToolTip(safe_tooltip(whole))
        rows.addWidget(headline)
        switch = QCheckBox(tr("Apply the model profile (its settings under yours)"))
        switch.setChecked(enabled)
        knows = B.launcher_knows(self.sai, MP.OPTION)
        switch.setEnabled(knows)
        switch.setToolTip(safe_tooltip(
            tr("sandbox-ai sets the settings measured for this model in this agent - sampling, context length, "
               "turn limit, the agent's own switches - wherever you set nothing yourself; off: --no-llm-profile")
            if knows else tr("This sandbox-ai has no --no-llm-profile yet: update it")))
        node_id = llm.id
        switch.toggled.connect(lambda on: self.set_prop(node_id, MP.OPTION, not on))
        rows.addWidget(switch)
        values = plain_label("\n".join(MP.value_lines(profile, mine, enabled)), role="secondary", selectable=True)
        values.setToolTip(safe_tooltip(whole))
        rows.addWidget(values)
        for warning in MP.warnings(profile):
            rows.addWidget(plain_label(warning, role="notice-warn", selectable=True))
        form.addRow(tr("Model profile"), holder)
        self.profile_label, self.profile_switch, self.profile_values = headline, switch, values
        self.knowledge_row(form)
        return holder

    def knowledge_row(self, form):
        """The "What we know" box of the board's model in its agent (model_profile_view.KnowledgeBox): the pair's
        status in words, then what works, what does not, what is not known yet; none without a model of yours."""
        from . import model_profile as MP
        from .model_profile_view import KnowledgeBox
        try:
            found = MP.board_knowledge(self.sai, self.board, self.llm_servers)
        except Exception as _exc:  # noqa: BLE001 - the properties are never broken by it
            applog.swallowed("board_view", "knowledge_row failed (no box shown)", _exc)
            found = None
        if found is None:
            return None
        box = KnowledgeBox()
        box.set(found)
        form.addRow(tr("What we know"), box)
        self.knowledge_box = box
        return box

    def llm_param_rows(self, node, form=None):
        """The LLM node's own parameters for this board's starts (--llm-param KEY=VALUE, forced for them): empty fields
        leave the server's own (sandbox-ai llm set, the LLM servers page's Parameters…); a value typed in is checked at
        once against sandbox-ai's catalogue (names, types, ranges) and kept only when valid; Reset to defaults empties
        them."""
        from . import llm_params as LP
        from . import llm_params_view as LPV
        node_id = node.id
        form = form if form is not None else self.inspector
        kind = self.llm_kind(node)  # only what this kind of server takes (every parameter while not known)
        catalogue = LP.local_catalogue(self.sai, kind)
        if catalogue is None:
            form.addRow("", plain_label(tr("This sandbox-ai has no model parameters (sandbox-ai llm "
                                           "params): the server's own hold."), role="notice-warn"))
            return
        params = node.props.get("params") if isinstance(node.props.get("params"), dict) else {}
        fields = LPV.ParamsFields(catalogue, {k: v for k, v in params.items() if not k.startswith("loop_guard.")},
                                  force=False, default_text=tr("the server's own"))
        fields.setToolTip(tr("This board's model parameters (--llm-param): in place of the agent's and the server's "
                             "own for these starts; empty: the server's (its Advanced settings… on the LLM servers page)."))
        spec = LP.loop_spec(self.sai)
        layers = LPV.LoopFields(spec, {k[11:]: v for k, v in params.items() if k.startswith("loop_guard.")}) \
            if spec is not None else None

        def commit():
            if not fields.valid() or layers is not None and not layers.valid():
                return
            values = dict(fields.texts())
            values.update({f"loop_guard.{k}": v for k, v in (layers.texts() if layers is not None else {}).items()})
            self.set_prop(node_id, "params", values or None)
        fields.committed.connect(commit)
        form.addRow(tr("Model parameters for this board"), fields)
        self.llm_fields = fields
        info = LPV.server_only_box(LP.server_only(self.sai, kind))
        if info is not None:
            form.addRow("", info)
        if layers is not None:
            layers.committed.connect(commit)
            layers.setToolTip(tr("This board's loop guard (--llm-param loop_guard.LAYER=VALUE); empty: the server's"))
            form.addRow(tr("Loop guard for this board"), layers)
        if not B.launcher_knows(self.sai, "--llm-param"):
            fields.setEnabled(False)
            if layers is not None:
                layers.setEnabled(False)
            form.addRow("", plain_label(tr("This sandbox-ai has no --llm-param: the server's own parameters "
                                           "hold."), role="notice-warn"))

    def mcp_names(self):
        """The MCP servers defined on the host the board starts on: this machine's mcp.json, another host's `sandbox-ai
        mcp list --json` (read in the background; [] until then, the inspector shown again after; read again when
        older than MCP_KEEP, after the start's preparation made something there, and when that host is chosen
        again)."""
        host = self.host()
        if host != LOCAL:
            known = self.__dict__.setdefault("mcp_known", {})
            failed = self.__dict__.setdefault("mcp_failed", {})  # {host: when its read failed}: asked again later
            read_at = self.__dict__.setdefault("mcp_read_at", {})  # {host: when it was read}
            if host in failed and known.get(host) is not None and time.monotonic() - failed[host] >= HOST_RETRY:
                del known[host], failed[host]
            stale = known.get(host) is not None and host not in failed and \
                time.monotonic() - read_at.get(host, 0) >= MCP_KEEP
            if host not in known or stale:
                # (once per host, it was: a server added or removed there since - the MCP page, a start's preparation,
                # a terminal - was never seen while the app ran; now read again when older than MCP_KEEP, the list
                # read before offered meanwhile)
                known[host] = known.get(host) if stale else None
                read_at[host] = time.monotonic()

                def done(rows, error):
                    known[host] = sorted(str(row[0]) for row in rows or []) if error is None else []
                    read_at[host] = time.monotonic()
                    if error is not None:
                        failed[host] = time.monotonic()
                    if known[host] and self.inspected in self.items and \
                            self.board.node(self.inspected).kind == "mcp":
                        self.inspect_timer.start()
                background(self.sai.host_mcp_servers, host, owner=self, done=done, timeout=90)
            return list(known[host] or [])
        try:
            return sorted(self.sai.mcp_definitions())
        except Exception as _exc:  # noqa: BLE001
            applog.swallowed("board_view", "BoardPage.mcp_names failed (went on without it)", _exc)
            return []

    def mcp_chooser(self, node):
        """Every MCP server there is to choose from: those defined here (sandbox-ai mcp list, the built-in ones),
        the project's own (.sandbox-ai.json) and, once read, the catalogs' entries not added yet (marked: they are
        added on the MCP page first). A name may still be typed (a server of another host)."""
        node_id = node.id
        combo = self.mcp_combo(node.props.get("name", ""))

        def chosen(_index=None):
            data = combo.currentData()
            text = data if isinstance(data, str) and combo.currentText() == combo.itemText(combo.currentIndex()) \
                else combo.currentText()
            self.set_prop(node_id, "name", str(text or "").strip())
        combo.activated.connect(chosen)
        combo.lineEdit().editingFinished.connect(chosen)
        return combo

    def mcp_combo(self, current):
        """An editable combo box of every MCP server there is to choose from (see mcp_chooser), CURRENT chosen (or
        typed in when it lists no such server); its items' data are the names. The inspector and the guide use it."""
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.lineEdit().setPlaceholderText(tr("Choose a server…"))
        names = self.mcp_names()
        own = sorted(name for name in B.project_servers(self.board) if name not in names)
        for name in names:
            combo.addItem(name, name)
        for name in own:
            combo.addItem(tr("{name} (the project's own)", name=name), name)
        catalog = getattr(self, "mcp_catalog_names", None)
        if catalog is None or getattr(self, "mcp_catalog_host", LOCAL) != self.host():
            # (read for the host chosen: another host's catalog, not this machine's - what a start adds there)
            catalog = catalog if getattr(self, "mcp_catalog_host", LOCAL) == self.host() else None
            self.load_mcp_catalog()
        for name in sorted(set(catalog or ()) - set(names) - set(own)):
            index = combo.count()
            combo.addItem(tr("{name} (catalog: added at the start, asked first)", name=name), name)
            combo.setItemData(index, tr("Not added on this host yet: Start adds it from its catalog after asking (or the "
                                        "MCP page now)"), Qt.ItemDataRole.ToolTipRole)
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentIndex(-1)
            combo.setEditText(current)
        return combo

    def load_mcp_catalog(self):
        """The catalogs' entries of the board's host (sandbox-ai mcp catalog: a command, so in the background), once per
        host chosen - another host's own (`mcp catalog --json` there: what a start adds there), not this machine's."""
        host = self.host()
        if getattr(self, "mcp_catalog_loading", None) == host:
            return
        self.mcp_catalog_loading = host

        def read():
            if host == LOCAL:
                return self.sai.mcp_catalog()
            data = self.sai.host_json(host, "mcp", "catalog", "--json", timeout=150)
            listed = data.get("entries", []) if isinstance(data, dict) else []
            return [(str(e.get("prefix") or ""), e) for e in listed if isinstance(e, dict)], []

        def done(result, error):
            self.mcp_catalog_loading = None
            if host != self.host():  # (another host chosen meanwhile: its own is read)
                return
            self.mcp_catalog_host = host
            entries = result[0] if isinstance(result, tuple) and result else []
            self.mcp_catalog_names = sorted({str(entry.get("name")) for _prefix, entry in entries
                                             if isinstance(entry, dict) and entry.get("name")}) if not error else []
            # By id and name: an MCP part's variables (their descriptions, required or secret) from its entry.
            self.mcp_catalog_entries = {} if error else {
                key: entry for _prefix, entry in entries if isinstance(entry, dict)
                for key in (entry.get("id"), entry.get("name")) if isinstance(key, str)}
            if self.inspected in self.items and self.board.node(self.inspected).kind == "mcp":
                self.inspect_timer.start()
        background(read, owner=self, done=done, timeout=180)

    # -- projects --

    def open_project(self, directory=None, quiet=False):
        """The board of the project DIRECTORY (asked for when None). QUIET (a switch of the current project):
        what would be a message box is a line above the board instead. The board shown until now is kept for
        its project (activate); reading the same project again drops its unapplied changes, after asking."""
        if directory is None and self.host() != LOCAL:  # a folder of that host, read there
            return self.open_remote_project()
        if directory is None:
            directory = RC.choose_folder(self, LOCAL, tr("Project folder"), "project")
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
            self.show_notice(text) if quiet else short_notice(self, tr("Board"), text)
            self.show_reasked()
            return True
        except (OSError, ValueError) as exc:
            if quiet:
                self.load(B.from_argv(self.sai, self.default_agent(), [], workspaces=[directory]), directory)
                self.show_notice(tr("The project's .sandbox-ai.json cannot be read: {error}", error=printable_text(exc)),
                                 "notice-error")
                return False
            warn(self, tr("No project configuration"), printable_text(exc))
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

    def clone_on_host(self, host, folder):
        """clone_to_project with another HOST chosen: the board shown with FOLDER (that host's, as it is there) as its
        first workspace - and every workspace that was the same folder with it -, as a board of that host's folder
        (open_remote_project). Nothing is written: Apply is this machine's; on that host it starts as shown."""
        if not backend.valid_directory(folder):
            warn(self, tr("Clone"), tr("Give the absolute path of a folder on {host}.", host=printable(host)))
            return False
        main = next((n for n in self.board.included() if n.kind == "workspace" and
                     str(n.props.get("path", "")).strip()), None)
        before = str(main.props.get("path")).strip() if main is not None else \
            (self.remote_project[1] if self.remote_project else "")
        if before and posixpath.normpath(before) == posixpath.normpath(folder):
            inform(self, tr("Clone"), tr("Choose another folder than this project's."))
            return False
        if self.dirty_for(None) and self.directory is not None and not self.may_discard_unnamed():
            return False
        old = self.remote_project[1] if self.remote_project else (self.directory or tr("this board"))
        untrusted = self.untrusted.get(self.key_of(self.directory))
        board = B.Board.from_state(self.board.state())
        for node in board.of_kind("workspace"):
            path = str(node.props.get("path", "")).strip()
            if node.id == getattr(main, "id", None) or (before and path and
                                                         posixpath.normpath(path) == posixpath.normpath(before)):
                node.props["path"] = folder
        if main is None:
            board.add("workspace", path=folder)
        self.sources[""] = {"digest": None, "trusted": not untrusted}
        if untrusted:
            self.untrusted[""] = untrusted
        else:
            self.untrusted.pop("", None)
        self.load(board, None, positioned=True)
        self.undo.resetClean()  # (not applied: changes of the board shown)
        self.remote_project = (host, folder)
        self.show_folder()
        self.show_notice(tr("A clone of {old}: on {host} it starts with {new} as its workspace, as shown (Apply "
                            "writes project files on this machine only).", old=printable(old), host=printable(host),
                            new=printable(folder)), "notice-ok")
        return True

    def open_remote_project(self, folder=None):
        """The board of FOLDER (chosen from that host's folders when None) on the host chosen: its project file read by
        that host's sandbox-ai (`wizard trust --show`, in the background: host_project_show), else the folder as the
        board's workspace. FOLDER stays that host's path as it is there - never resolved or looked at here; the board
        is no project of this machine (no tab, no Apply: on that host it starts as shown, --no-config), and a project
        file nobody trusted there is asked about before its start (start_untrusted_on_host)."""
        host = self.host()
        if folder is None:
            start = self.remote_project[1] if self.remote_project and self.remote_project[0] == host else ""
            folder = RC.choose_folder(self, host, tr("Project folder"), "project", start=start, sai=self.sai)
        folder = str(folder or "").strip()
        if not folder:
            return False
        if self.dirty_for(None) and not self.may_discard_unnamed():
            return False
        path = PT.config_path(folder)
        if path is None:
            warn(self, tr("No project configuration"), tr("Give the absolute path of a folder on {host}.",
                                                          host=printable(host)))
            return False
        self.show_notice(tr("Reading {path} on {host}…", path=printable(path), host=printable(host)))
        background(self.sai.host_project_show, host, path, owner=self, timeout=90,
                   done=lambda shown, error: self.remote_project_read(host, folder, path, shown, error))
        return True

    def remote_project_read(self, host, folder, path, shown, error):
        """What that host's sandbox-ai read of the project file PATH in FOLDER (open_remote_project): its board, else
        the folder as the workspace of a board of the default agent."""
        if host != self.host():  # another host chosen meanwhile: that host's folder is no folder here
            self.show_notice(tr("Not opened: another host was chosen meanwhile."), "notice-warn")
            return False
        document = shown.get("document") if error is None and isinstance(shown, dict) and \
            not shown.get("error") and not shown.get("problem") else None
        if isinstance(document, dict):
            board = B.from_project(self.sai, document, folder)
        else:
            board = B.from_argv(self.sai, self.default_agent(), [], workspaces=[folder])
        trusted = bool(isinstance(document, dict) and shown.get("trusted"))
        self.sources[""] = {"digest": shown.get("digest") if isinstance(document, dict) else None, "trusted": True}
        if isinstance(document, dict) and not trusted:
            self.untrusted[""] = path
        else:
            self.untrusted.pop("", None)
        self.load(board, None)
        self.remote_project = (host, folder)
        self.show_folder()
        if isinstance(document, dict):
            text = tr("The board of {path} on {host}: it starts there as shown (--no-config).", path=printable(path),
                      host=printable(host)) if trusted else \
                tr("{path} on {host} is not trusted: the board shows it; Start asks before it starts it there as "
                   "shown.", path=printable(path), host=printable(host))
            self.show_notice(text, "notice-ok" if trusted else "notice-warn")
        else:
            reason = printable_text(str(error)) if error is not None else \
                printable(shown.get("problem") or shown.get("error") or "") if isinstance(shown, dict) else ""
            self.show_notice(tr("{folder} on {host} is the board's workspace (its project file was not read: "
                                "{reason}).", folder=printable(folder), host=printable(host),
                                reason=reason or tr("none")))
        return True

    def may_discard_unnamed(self):
        """True if the board of no project folder (key "") may be replaced: shown or kept, nothing unapplied, or the
        human agrees."""
        if ask_first(self, "board-discard", tr("Changes not applied"),
                     tr("The board of {path} has changes that are not applied. Drop them?",
                        path=tr("the new board")), tr("Drop the changes"), reject=tr("Keep them")):
            return True
        self.show_unnamed()  # (kept: shown, so that what is kept can be seen, applied or dropped)
        return False

    def load(self, board, directory, positioned=False):
        """Show BOARD for the project DIRECTORY, laid out (its saved layout, if any) unless POSITIONED (a
        preset: its own positions)."""
        self.own_preset = None  # another board: no preset of yours it came from (new_from_preset sets it after)
        if self.key_of(directory) != self.key_of(self.directory):
            self.stash()  # the project shown until now keeps its board
            self.new_stack()
            self.drop_kept(self.key_of(directory))  # replaced by what is loaded now
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
        self.restore_unnamed(self.saved_boards.get(""))
        self.resume_bridge_waits(state.get("bridge_waits", [], list))
        self.host_choice.chosen.connect(lambda _host: state.schedule())
        self.undo_group.cleanChanged.connect(lambda _clean: state.schedule())
        self.undo_group.indexChanged.connect(lambda _index: state.schedule())
        self.show_cli(state.get("board_cli", False) is True)

    def restore_unnamed(self, entry):
        """The board of no project folder as it was left (keep_boards' entry of the key ""), on the board the page
        starts with: its changes as a draft with their Undo (restore_saved), the folder of the host it is for (when
        that host is still the one chosen), and that its project file there was not trusted. Shown again after the
        window's project when it was the board shown (unnamed_again)."""
        self.unnamed_again = False
        if isinstance(entry, dict) and isinstance(entry.get("draft"), dict) and \
                (self.directory is not None or self.undo.count()):
            return  # (a page in use already: what is kept stays in the state file for the next start)
        self.restored.add("")
        if not isinstance(entry, dict) or not isinstance(entry.get("draft"), dict):
            return
        self.restore_saved(entry, "", None)
        if not self.dirty():
            return  # (a draft this app cannot show: left out, and said in the log)
        remote = entry.get("remote")
        if isinstance(remote, list) and len(remote) == 2 and all(isinstance(v, str) and v for v in remote) and \
                remote[0] == self.host() and backend.valid_directory(remote[1]):
            self.remote_project = (remote[0], remote[1])
            self.show_folder()
        if isinstance(entry.get("untrusted"), str) and entry["untrusted"]:
            self.untrusted[""] = entry["untrusted"]
        # Shown again after the project of the window's tab, which is restored right after this (activate) - not after
        # a project the human opens later: the note is dropped once the window's start is through.
        self.unnamed_again = entry.get("shown") is True
        QTimer.singleShot(0, self, lambda: setattr(self, "unnamed_again", False))

    def show_unnamed(self):
        """The board of no project folder, kept while a project's board is shown (the canvas menu, "Keep them"): shown
        again as it was left - the project's board is kept for its tab. True if it is shown now."""
        if self.directory is None:
            return True
        session = self.sessions.get("")
        if session is None or not alive(session["stack"]):
            return False
        self.autosave()
        self.stash()
        self.board, self.undo, self.directory = session["board"], session["stack"], None
        self.remote_project = session.get("remote")
        self.undo_group.setActiveStack(self.undo)
        self.inspected = session["inspected"]
        self.show_folder()
        self.rebuild()
        self.view.setTransform(session["transform"])
        self.view.centerOn(session["center"])
        text, role = session["notice"]
        self.show_notice(text, role or "secondary")
        return True

    def unnamed_kept(self):
        """Whether a board of no project folder with changes is kept while a project's board is shown."""
        session = self.sessions.get("") if self.directory is not None else None
        return session is not None and alive(session["stack"]) and not session["stack"].isClean()

    def keep_boards(self):
        """Per project: zoom and scroll position, and the board itself while it has changes not applied (so a
        crash or a restart loses nothing). Projects not opened in this run keep what they had."""
        sessions = dict(self.sessions)
        self.stash()
        sessions[self.key_of(self.directory)] = self.sessions[self.key_of(self.directory)]
        boards = {}
        for key, session in sessions.items():
            if not alive(session["stack"]) or (not key and session["stack"].isClean()):
                continue  # (the board of no folder: only while it has changes)
            center = session["center"]
            entry = {"zoom": round(session["transform"].m11(), 4), "center": [round(center.x(), 1), round(center.y(), 1)]}
            if not session["stack"].isClean():
                entry["draft"] = scrub_board(session["board"].state())
                if self.sources.get(key):
                    entry["source"] = dict(self.sources[key])
                history = history_of(session["stack"])  # Undo comes back with the draft
                if history is not None:
                    entry["history"] = history
            if not key:
                # The board of no project folder - a new installation's first board, a preset's without a folder, the
                # board of another host's folder: kept too (it was not: closing the app, an update's restart or a
                # crash lost all of it without a word). With the folder of the host it is for, the project file it
                # came from when nobody trusted that one (Start asks first, as before the restart), and whether it
                # was the board shown.
                remote = session.get("remote")
                if remote:
                    entry["remote"] = [str(remote[0]), str(remote[1])]
                if self.untrusted.get(""):
                    entry["untrusted"] = str(self.untrusted[""])
                entry["shown"] = self.directory is None
            boards[key] = entry
        for key, entry in self.saved_boards.items():
            if key not in boards and key not in self.restored:
                boards[key] = entry
        within_budget(boards, self.key_of(self.directory))
        self.state.data["boards"] = boards
        if not getattr(self, "bridges_ended", False):  # (after the quit's end_bridge_waits: as it was written then)
            self.state.data["bridge_waits"] = self.bridge_waits_kept()
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
                B.Board.checked(draft, self.sai, self.policy())
            except ValueError as exc:
                applog.log("WARNING", "board", f"{printable(directory or key)}: the changes kept in the app's state "
                           f"file were left out ({printable_text(exc)})")
                draft = None
            before = self.board.state()
            if draft and draft != before and not self.restore_history(entry.get("history"), before, draft,
                                                                      key or ""):
                self.undo.push(Snapshot(self, tr("Restore the changes not applied"), before, draft))
            if draft and draft != before:
                self.draft_notice = tr("Restored the changes not applied when the app was last closed: Undo goes "
                                       "back to the project's file, Discard drops them.")
                applog.log("INFO", "board", f"{printable(directory or key)}: {self.draft_notice}")
                source, now = entry.get("source"), self.sources.get(key)
                if key and directory and not (isinstance(source, dict) and source == now and source.get("trusted")):
                    self.untrusted[key] = PT.path_text(directory)
                    self.reasked = tr(
                        "These changes were made to a version of the project file that is not trusted or no longer "
                        "there: Apply (it shows everything that will be trusted and asks) before you start them.")
        zoom, center = entry.get("zoom"), entry.get("center")
        if isinstance(zoom, (int, float)) and 0.05 <= zoom <= 20:
            self.fitted = True  # not fitted again when first shown
            self.view.setTransform(QTransform.fromScale(zoom, zoom))
            if isinstance(center, list) and len(center) == 2 and all(isinstance(v, (int, float)) and abs(v) < 1e9
                                                                     for v in center):
                self.view.centerOn(QPointF(*center))

    def restore_history(self, history, before, draft, key=""):
        """The steps of Undo kept with a draft (history_of), back on the stack: the file's board BEFORE first, then
        each step, Undo's place where it was. True when it ends at DRAFT on the folder KEY (else nothing is kept of
        it). A Change folder comes back as a step (with what its folders' trust was: take_trust) when its folders
        still are folders and the steps lead from one to the next to KEY's."""
        try:
            steps, index = history["steps"], int(history["index"])
            states = [self.kept_step(step) for step in steps]
            for _text, older, newer, _made, _folders in states:
                B.Board.checked(older, self.sai, self.policy())
                B.Board.checked(newer, self.sai, self.policy())
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        if not states or not 0 <= index <= len(states):
            return False
        start = self.folder_at_start(states, index, key)
        if start is None:
            applog.log("INFO", "board", "the steps of Undo kept with a draft were left out: their Change folder "
                                        "does not lead to this folder (or a folder of it is gone)")
            return False
        reached = states[index - 1][2] if index else states[0][1]
        if reached != draft:
            return False
        self.undo.clear()
        self.history_restoring = True  # (a Change folder put back changes the board only, not its folder)
        try:
            if states[0][1] != before and start == key:
                self.undo.push(Snapshot(self, tr("Restore the changes not applied"), before, states[0][1]))
            offset = self.undo.count()
            for text, older, newer, made, _folders in states:
                self.undo.push(made(text, older, newer))
            if start != key:
                # (its first steps were made on another folder: no step of them is what this folder's file says -
                # the board counts as changed at each, Undo ends at that folder's board as it was read then)
                self.undo.resetClean()
            self.undo.setIndex(offset + index)
        finally:
            self.history_restoring = False
        if self.board.state() != draft:
            self.history_restoring = True
            try:
                self.undo.setIndex(0)
                self.undo.clear()
            finally:
                self.history_restoring = False
            return False
        return True

    def kept_step(self, step):
        """(text, before, after, make, folders) of a step of Undo as history_of kept it; make(text, before, after) is
        its command, FOLDERS (folder before, folder after) of a Change folder (else None). ValueError for one that
        cannot be put back."""
        text, older, newer, kind = str(step["text"]), step["before"], step["after"], step.get("kind", "snapshot")
        if kind == "snapshot":
            return text, older, newer, lambda t, b, a: Snapshot(self, t, b, a), None
        if kind == "folder":
            folders = [step["before_dir"], step["after_dir"]]
            if not all(f is None or (isinstance(f, str) and os.path.isabs(f)) for f in folders):
                raise ValueError("not a folder")
            trust = {str(k): {"untrusted": v.get("untrusted") if isinstance(v.get("untrusted"), str) else None,
                              "source": v.get("source") if isinstance(v.get("source"), dict) else None}
                     for k, v in (step.get("trust") or {}).items() if isinstance(v, dict)}
            return (text, older, newer, lambda t, b, a: FolderChange(self, t, folders[0], folders[1], b, a, trust),
                    tuple(folders))
        if kind == "host-folder":
            remotes = [step["before_remote"], step["after_remote"]]
            if not all(r is None or (isinstance(r, list) and len(r) == 2 and all(isinstance(v, str) and v for v in r))
                       for r in remotes):
                raise ValueError("not a host's folder")
            remotes = [tuple(r) if r else None for r in remotes]
            return text, older, newer, lambda t, b, a: HostFolderChange(self, t, remotes[0], remotes[1], b, a), None
        raise ValueError(f"a step of an unknown kind: {kind!r}")

    def folder_at_start(self, states, index, key):
        """The key of the folder the steps STATES (kept_step's) start on, when their Change folder steps follow each
        other - each from the folder the one before went to - and the board stands on the folder KEY at INDEX, every
        folder of them still a folder; else None."""
        moves = [state[4] for state in states]  # (folder before, folder after) of a Change folder, else None
        changes = [move for move in moves if move]
        start = current = self.key_of(changes[0][0]) if changes else key
        for position, move in enumerate(moves):
            if position == index and current != key:
                return None
            if move:
                if self.key_of(move[0]) != current or any(f is not None and not os.path.isdir(f) for f in move):
                    return None
                current = self.key_of(move[1])
        return start if index < len(moves) or current == key else None

    def autosave(self):
        """The board's changes kept now (state file: the draft, its Undo, zoom), not after the usual delay: when the
        page is left, another project is shown or the window goes; a crash then loses nothing."""
        if self.state is not None:
            try:
                self.state.flush(wait=False)  # (the writer thread syncs it to the disk; the switch goes on)
            except Exception as exc:  # noqa: BLE001 - never stops the switch
                applog.log("WARNING", "board", f"the board could not be saved: {exc}")

    def commit_edits(self):
        """What is being typed in the inspector, taken now as leaving the field would take it (a list: without the
        pause): before quitting keeps the draft."""
        focused = QApplication.focusWidget()
        if focused is not None and (self.isAncestorOf(focused) or self.inspector_holder.isAncestorOf(focused)):
            focused.clearFocus()  # (a line edit's editingFinished)
        self.commit_lists()
        self.commit_list_fields()

    def commit_field(self):
        """A field of the inspector the keyboard is still in when another part is selected (from the parts list by
        keyboard, a finding, a search): what was typed is taken for the part it was typed for, as leaving the field
        would take it - the rows made anew for the other part dropped it (the bug class of 2026-11-16). The keyboard
        stays where it is."""
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) and self.inspector_holder.isAncestorOf(focused) and not focused.isReadOnly():
            focused.editingFinished.emit()

    def commit_list_fields(self):
        """A value typed into a list field (list_fields) and not added yet: added now for the part it was typed for -
        another part selected, quitting -, as leaving the field adds it. (Not when the same part's rows are made
        anew: the field made anew keeps it, restore_focus.)"""
        for field in self.inspector_holder.findChildren(LF.ListFields):
            field.take_typed()

    def commit_lists(self):
        """A list field of the inspector takes what was typed 600 ms after the typing stops: taken now (another part
        selected, quitting), not lost."""
        for edit in self.inspector_holder.findChildren(LinesEdit) + self.findChildren(LinesEdit):
            if edit.timer.isActive():
                edit.timer.stop()
                edit.changed.emit(edit.values())

    def hideEvent(self, event):  # noqa: N802
        self.autosave()
        super().hideEvent(event)

    def discard_changes(self):
        """The board as the project's file says, after asking (what is not applied is lost); the board of no
        project folder: a new one."""
        if self.directory and self.may_discard():
            self.undo.setClean()  # (asked already: open_project need not ask again)
            self.discard_draft()
        elif not self.directory and self.dirty() and self.may_discard():
            self.untrusted.pop("", None)
            self.sources.pop("", None)
            self.set_board(B.with_backend(B.from_argv(self.sai, self.default_agent(), []),
                                          self.settings().get("default_backend")), None)
            self.fit()

    def discard_draft(self):
        """Drop the restored changes: the project's board as its file says."""
        if self.directory:
            self.open_project(self.directory, quiet=True)
        self.discard_button.hide()

    def change_folder(self, folder=None):
        """Change folder…: this board on another project folder (FOLDER, else chosen, the current one preselected).
        Every part and setting stays; its main workspace and the place of its project file and changes follow, as
        one undo step; the folder checks run again; nothing is written there before Apply. A folder with a
        .sandbox-ai.json of its own: asked whether that file is loaded instead (its trust checked as always) - else
        the board's settings stay and the file is left alone (Start waits for Apply, which writes the board there).
        With another host chosen: a folder of that host (change_folder_on_host)."""
        if self.host() != LOCAL:
            return self.change_folder_on_host(self.host(), folder)
        if folder is None:
            from .state import file_dialog, run_dialog
            chosen = run_dialog(file_dialog(self, tr("Change folder"), self.project_folder() or "", "folder"))
            folder = chosen[0] if chosen else None
        if not folder:
            applog.log("DEBUG", "board", "Change folder: no folder chosen")
            return False
        folder = os.path.realpath(os.path.expanduser(str(folder)))
        if self.key_of(folder) == self.key_of(self.directory):
            applog.log("INFO", "board", f"Change folder: {printable(folder)} is the board's folder already: nothing "
                                        "changes")
            return False
        # That folder's own board kept with changes not applied (its tab) is replaced by this one: asked, as Clone asks.
        if self.dirty_for(folder) and not ask_first(
                self, "board-discard", tr("Changes not applied"),
                tr("The board of {path} has changes that are not applied. Drop them?", path=printable(folder)),
                tr("Drop the changes"), reject=tr("Keep them")):
            return False
        applog.log("INFO", "board", f"Change folder: from {printable(self.directory or '-')} to {printable(folder)} "
                                    "asked (is there a project file of its own?)")
        asked_by = self.undo  # the board that asks: the answer is for it, not for the one of the tab shown by then

        def looked(has_file, error):
            if self.undo is not asked_by:
                applog.log("INFO", "board", f"Change folder to {printable(folder)}: not changed, another project's "
                                            "board is shown by now")
                return
            if error is not None:
                warn(self, tr("Change folder"), printable_text(error))
                return
            if has_file and confirm(self, tr("Change folder"), tr(
                    "{path} has a project file of its own (.sandbox-ai.json). Load it into the board? (Keep: this "
                    "board's settings stay and that file is left as it is until you apply.)", path=printable(folder)),
                    tr("Load it"), reject=tr("Keep this board")):
                applog.log("INFO", "board", f"Change folder: the project file of {printable(folder)} is loaded "
                                            "instead of the board")
                self.open_project(folder, quiet=True)  # its trust is checked as for any project opened
                return
            self.switch_folder(folder, own_file=bool(has_file))
        background(lambda: os.path.isfile(os.path.join(folder, ".sandbox-ai.json")), owner=self, timeout=30,
                   done=looked)
        return True

    def host_folder(self, host):
        """The board's folder on HOST as it is there: the folder it was opened from (remote_project), else its main
        workspace's path; "" for none - and for this machine's project folder on another host (is_local_folder: a
        path of this machine, never a folder of that host by default; user report 2026-11-19)."""
        if self.remote_project is not None and self.remote_project[0] == host:
            return self.remote_project[1]
        main = next((n for n in self.board.included() if n.kind == "workspace" and
                     str(n.props.get("path", "")).strip()), None)
        path = str(main.props.get("path")).strip() if main is not None else ""
        return "" if self.is_local_folder(host, path) else path

    def is_local_folder(self, host, path):
        """Whether PATH, used for a start on HOST, is this machine's project folder of the board (the folder of its
        project tab) and not a folder of HOST: another host chosen for the board of a project of this machine, and no
        folder of that host opened (remote_project). Never looked at on either machine: by the paths alone."""
        if host == LOCAL or not path or not self.directory or \
                (self.remote_project is not None and self.remote_project[0] == host):
            return False
        here = {posixpath.normpath(str(self.directory)), self.key_of(self.directory)}
        return posixpath.normpath(str(path)) in here

    def change_folder_on_host(self, host, folder=None):
        """Change folder… with HOST chosen at the bottom: FOLDER (else chosen from that host's folders, the board's
        folder there preselected), kept as that host's path as it is there - never resolved or looked at here. Its
        project file is looked for on that host (`wizard trust --show`, in the background: host_project_show); one of
        its own: asked whether it is loaded instead (open_remote_project) - else the board's main workspace and its
        folder follow, its settings kept, as one undo step (host_folder_looked). Nothing is written: a board of
        another host has no Apply (it starts there as shown, --no-config)."""
        current = self.host_folder(host)
        if folder is None:
            folder = RC.choose_folder(self, host, tr("Change folder"), "project", start=current, sai=self.sai)
        folder = str(folder or "").strip()
        if not folder:
            applog.log("DEBUG", "board", "Change folder: no folder chosen")
            return False
        if current and posixpath.normpath(current) == posixpath.normpath(folder):
            applog.log("INFO", "board", f"Change folder: {printable(folder)} on {printable(host)} is the board's "
                                        "folder already: nothing changes")
            return False
        path = PT.config_path(folder)
        if path is None:
            warn(self, tr("Change folder"), tr("Give the absolute path of a folder on {host}.", host=printable(host)))
            return False
        applog.log("INFO", "board", f"Change folder: from {printable(current or '-')} to {printable(folder)} on "
                                    f"{printable(host)} asked (is there a project file of its own there?)")
        self.show_notice(tr("Reading {path} on {host}…", path=printable(path), host=printable(host)))
        asked_by = self.undo  # (the board that asks: see change_folder)
        background(self.sai.host_project_show, host, path, owner=self, timeout=90,
                   done=lambda shown, error: self.undo is asked_by and self.host_folder_looked(host, folder, path,
                                                                                               shown, error))
        return True

    def host_folder_looked(self, host, folder, path, shown, error):
        """What HOST's sandbox-ai said of the project file PATH in FOLDER (change_folder_on_host): not answered -
        said, nothing changes; a file of its own - Load it (open_remote_project) or Keep this board; else (or Keep)
        the board on FOLDER."""
        if host != self.host():  # another host chosen meanwhile: that host's folder is no folder here
            self.show_notice(tr("Not changed: another host was chosen meanwhile."), "notice-warn")
            return False
        if error is not None:
            self.show_notice("")
            applog.log("WARNING", "board", f"Change folder: {printable(host)} did not answer: nothing changed")
            warn(self, tr("Change folder"), tr("{host} did not answer, so its folder {folder} was not looked at: "
                                               "nothing changed. {error}", host=printable(host),
                                               folder=printable(folder), error=printable_text(str(error))[-400:]))
            return False
        own_file = isinstance(shown, dict) and shown.get("digest") is not None
        self.show_notice("")
        if own_file and confirm(self, tr("Change folder"), tr(
                "{path} on {host} has a project file of its own (.sandbox-ai.json). Load it into the board? (Keep: "
                "this board's settings stay; on {host} it starts as shown.)", path=printable(folder),
                host=printable(host)), tr("Load it"), reject=tr("Keep this board")):
            applog.log("INFO", "board", f"Change folder: the project file of {printable(folder)} on {printable(host)} "
                                        "is loaded instead of the board")
            return self.open_remote_project(folder)  # its trust asked about before its start, as always
        return self.switch_host_folder(host, folder, own_file)

    def switch_host_folder(self, host, folder, own_file=False):
        """The board on HOST's FOLDER, kept as it is: its main workspace (the one on the board's folder there, else
        the first; none: one added) and its folder follow, one undo step."""
        current = self.host_folder(host)
        before = self.board.state()
        board = B.Board.from_state(before)
        workspaces = [n for n in board.of_kind("workspace") if str(n.props.get("path", "")).strip()]
        main = next((n for n in workspaces if current and posixpath.normpath(str(n.props["path"]).strip()) ==
                     posixpath.normpath(current)), None) or (workspaces[0] if workspaces else None)
        if main is not None:
            main.props["path"] = folder
        else:
            added = board.add("workspace", path=folder)
            added.x, added.y = B.snap(40.0, self.grid()), B.snap(40.0, self.grid())
        self.undo.push(HostFolderChange(self, tr("Change folder"), self.remote_project, (host, folder), before,
                                        board.state()))
        applog.log("INFO", "board", f"Change folder: the board is on {printable(folder)} on {printable(host)} now "
                   f"(main workspace {'moved' if main is not None else 'added'}" +
                   ("; its own project file there is not shown: the board starts as shown)" if own_file else ")"))
        if own_file:
            self.show_notice(tr("{path} on {host} has a project file of its own that this board does not show: on "
                                "{host} the board starts as shown (--no-config).", path=printable(folder),
                                host=printable(host)), "notice-warn")
        return True

    def move_to_host_folder(self, remote, state):
        """HostFolderChange's step: the board's folder on its host REMOTE ((host, folder) or None) and its parts as
        STATE say; the checks run again."""
        self.restore(state)
        self.remote_project = tuple(remote) if remote else None
        self.show_folder()
        self.refresh()

    def switch_folder(self, folder, own_file=False):
        """The board on FOLDER, kept as it is (change_folder): one undo step; OWN_FILE: the folder's own project file
        is left alone (the board does not show it: Start waits for Apply)."""
        before_dir, before = self.directory, self.board.state()
        board = B.Board.from_state(before)
        workspaces = board.of_kind("workspace")
        main = next((n for n in workspaces if self.key_of(n.props.get("path")) == self.key_of(before_dir)), None) \
            or (workspaces[0] if workspaces else None)
        if main is not None:
            main.props["path"] = folder
        # A board read from a project file nobody trusted carries what that file set (also what no part shows): on
        # the new folder it is not trusted either - as a clone of it is not - until Apply shows it all and asks.
        carried = self.untrusted.get(self.key_of(before_dir)) if before_dir else None
        if own_file:
            self.untrusted[self.key_of(folder)] = PT.path_text(folder)
            self.reasked = tr("{path} has a project file of its own that this board does not show: Apply writes the "
                              "board there (it shows what changes and asks) before you start it.", path=printable(folder))
        elif carried:
            self.untrusted[self.key_of(folder)] = carried
        self.sources[self.key_of(folder)] = {"digest": None, "trusted": not (own_file or carried)}
        self.undo.push(FolderChange(self, tr("Change folder"), before_dir, folder, before, board.state()))
        applog.log("INFO", "board", f"Change folder: the board is on {printable(folder)} now (main workspace "
                   f"{'moved' if main is not None else 'none'}; " + ("its own project file is left alone: Start waits "
                                                                    "for Apply)" if own_file else "no project file "
                                                                    "there: Apply writes it)"))
        self.show_reasked()
        return True

    def move_to_folder(self, folder, state, step=None):
        """FolderChange's step: the board's folder FOLDER and its parts as STATE say; the checks run again, the
        window's project follows. The board moves with its Undo: what was kept under its old folder (this very board,
        as it was when its tab was last left) and under the new one (that folder's own board: asked about in
        change_folder) is not kept beside it - the old folder's tab reads its project again. Left there, two kept
        boards shared one Undo, and loading one of them deleted it under the other (an exception at every later
        switch to that tab). A STEP that came back from the state file brings FOLDER's trust as it was (take_trust)."""
        if self.key_of(folder) != self.key_of(self.directory):
            for key in (self.key_of(self.directory), self.key_of(folder)):
                self.drop_kept(key)
        if step is not None and step.trust and folder and self.key_of(folder) in step.trust:
            self.take_trust(folder, step.trust.pop(self.key_of(folder)))
        self.directory = str(folder) if folder else None
        self.show_folder()
        self.restore(state)
        self.refresh()
        if self.directory:
            self.opened.emit(self.directory)

    def take_trust(self, folder, record):
        """FOLDER's trust as a step of Change folder kept it in the state file (folder_trust's RECORD), the board back
        on it after a restart (R2-18): trusted only when nothing said it was not and its project file is the very
        version it was then, trusted (none there: as none was) - else Start waits for Apply, which shows everything
        it trusts and asks. Looked at once per step and folder: afterwards the page's own bookkeeping holds."""
        key = self.key_of(folder)
        try:
            review = self.sai.review_project(folder)
            now = {"digest": review.digest, "trusted": bool(review.trusted)}
        except FileNotFoundError:
            now = {"digest": None, "trusted": True}
        except (OSError, ValueError):
            now = None
        record = record if isinstance(record, dict) else {}
        source, untrusted = record.get("source"), record.get("untrusted")
        if now is not None and now["trusted"] and source == now and not untrusted:
            self.sources[key] = now
            self.untrusted.pop(key, None)
            return True
        self.sources[key] = {"digest": now["digest"] if now else None, "trusted": False}
        self.untrusted[key] = str(untrusted) if untrusted else PT.path_text(folder)
        applog.log("INFO", "board", f"{printable(folder)}: the board is back on it (Undo of Change folder after a "
                                    "restart); its project file is not trusted, or not the version it was: Start waits "
                                    "for Apply")
        return False

    def project_folder(self):
        return self.directory or (self.board.of_kind("workspace")[0].props.get("path")
                                  if self.board.of_kind("workspace") else None)

    def preset_folder(self):
        """The folder a preset's board gets as its workspace: the project's here; on another host the board's folder
        there (host_folder) - never this machine's project folder - else none."""
        host = self.host()
        return self.project_folder() if host == LOCAL else (self.host_folder(host) or None)

    def open_gallery(self):
        """The preset gallery: the preset chosen (its placeholders filled in, or left for later) becomes the
        board, after asking whether unapplied changes may go."""
        from .preset_gallery import PresetGallery
        gallery = PresetGallery(self.sai, self.preset_folder(), self.default_agent(), self.policy(), self.board,
                                self, self.settings().get("default_backend"), host=self.host())
        self.gallery = gallery
        chosen = gallery.result_ if gallery.exec() == gallery.DialogCode.Accepted else None
        widgets.drop_after_exec(gallery)  # (made anew each time)
        if chosen is None:
            return None
        preset, values = chosen
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
        host = self.host()
        folder = self.preset_folder()
        # Another host: the preset's folder is that host's (the folder of it the board is on, else none), and the
        # board stays the board of what it was - of that host's folder (remote_project), or of this machine's project
        # tab. Loaded as the board of a project of this machine at that host's path, the window opened it here and
        # said "... is not a folder" (user report 2026-11-19).
        remote = self.remote_project if self.remote_project is not None and self.remote_project[0] == host else None
        directory = folder if host == LOCAL else (None if remote is not None else self.directory)
        applog.log("INFO", "board", f"new board from the preset {printable(getattr(preset, 'id', '?'))} "
                   f"({getattr(preset, 'source', '?')}) for {printable(folder or 'no folder')}"
                   + (f" on {printable(host)}" if host != LOCAL else "")
                   + (f", {len(values)} value(s) filled in" if values else ", its placeholders left to fill in"
                      if values is None else ""))
        board = presets.build(self.sai, preset, folder, agent=self.default_agent(), values=values,
                              grid=self.grid(), backend=self.settings().get("default_backend"))
        self.load(board, directory, positioned=True)
        if remote is not None:
            self.remote_project = remote  # (load forgets it: the board of that host's folder still)
            self.show_folder()
        # Not applied: no project file says this board (Apply writes it). It counted as unchanged - the preset's
        # board (a locked one, say) was gone with the next start of the app, the project's default in its place,
        # and closing its tab asked nothing.
        self.undo.resetClean()
        self.own_preset = preset if preset.source == "mine" else None  # (Save as preset… offers to update it)
        if B.placeholders(board) and values is not None:  # values given and some left empty: asked again, by name
            self.fill_in()
        self.say_left_to_fill()
        self.check_prerequisites(why="preset")  # what the preset needs (an identity, an image variant) made now
        return board

    def save_board(self):
        """Save (button, Ctrl+S): the board where it belongs - into the preset of yours it was made from, at once and
        without a question (its name, description, category and notes kept; its folders kept if it kept them), else
        into the project file (Apply), else as a new preset of yours (Save as preset…'s dialog). "preset", "project"
        or None (nothing saved)."""
        from . import presets as PR
        current = None
        if getattr(self, "own_preset", None) is not None:
            current = {p.id: p for p in PR.load_mine(self.sai)[0]}.get(self.own_preset.id)
        if current is not None:
            folders = [p.default for p in current.placeholders if p.kind == "folder" and p.default]
            generated = PR.machine_specific(list(current.options), folders, host=self.host())  # (written anew)
            try:
                preset = PR.from_board(self.sai, self.board, current.title, current.description,
                                       current.category or "simple", ident=current.id,
                                       keep=bool(folders) or "--workspace" in current.options,
                                       notes=[n for n in current.notes if n != generated], host=self.host())
                PR.add_mine(self.sai, preset)
            except (OSError, ValueError) as exc:
                warn(self, tr("Not saved"), printable_text(exc))
                return None
            self.own_preset = preset
            applog.log("INFO", "board", f"Save: into your preset {printable(preset.id)} ({printable(preset.title)})")
            self.show_notice(tr("Saved into your preset {name}.", name=printable(preset.title)), "notice-ok")
            return "preset"
        if self.project_folder() and self.host() == LOCAL:  # (another host's board: no project file of this machine)
            applog.log("INFO", "board", f"Save: into the project file of {printable(self.project_folder())} (Apply)")
            return "project" if self.apply() else None
        applog.log("INFO", "board", "Save: no folder and no preset of yours: as a new preset (Save as preset…)")
        return "preset" if self.save_as_preset() is not None else None

    def save_as_preset(self, choices=None):
        """The board as a preset of yours (SavePresetDialog unless CHOICES: name, description of several lines,
        category, notes, the folders and values kept by default); made from one of yours (own_preset), it replaces that
        one after asking, unless unticked. The preset, or None."""
        from . import presets as PR
        from .preset_gallery import SavePresetDialog, save_board_preset
        mine = {p.id: p for p in PR.load_mine(self.sai)[0]}
        current = mine.get(self.own_preset.id) if getattr(self, "own_preset", None) is not None else None
        if choices is None:
            dialog = SavePresetDialog(self, self.sai, current)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            choices = dialog.choices() if accepted else None
            widgets.drop_after_exec(dialog)  # (made anew each time)
            if not accepted:
                applog.log("INFO", "board", "Save as preset: cancelled")
                return None
        applog.log("INFO", "board", f"Save as preset: {printable(choices.get('title', ''))!r}, category "
                   f"{printable(choices.get('category') or 'simple')}, folders and values kept "
                   f"{bool(choices.get('keep', True))}" + (f", replacing {printable(current.id)}" if current is not None
                                                           and choices.get("replace") else ""))
        preset = save_board_preset(self, self.sai, self.board, choices, replacing=current, host=self.host())
        applog.log("INFO" if preset is not None else "WARNING", "board", "Save as preset: " + (
            f"saved as {printable(preset.id)}" if preset is not None else "not saved (see the message before)"))
        if preset is not None:
            self.own_preset = preset
            self.show_notice(tr("Saved as your preset {name}: New from preset… lists it under My presets.",
                                name=printable(preset.title)), "notice-ok")
        return preset

    def bundle_name(self):
        """The name the board's bundle offers: its preset's, its folder's, else "board"."""
        if getattr(self, "own_preset", None) is not None:
            return self.own_preset.title
        folder = self.project_folder() or (self.remote_project[1] if self.remote_project else None)
        return os.path.basename(str(folder).rstrip("/")) if folder else "board"

    def export_board(self):
        """Export…: the board shown as one file (sandbox-ai board export --board -, on the board's host), where the
        human chooses (bundle_dialogs.ExportDialog: the images too when asked). Nothing of the board changes."""
        if not self.sai.board_bundle_supported():
            warn(self, tr("Export"), tr("This sandbox-ai cannot export a board yet ({command}): update it.",
                                        command="sandbox-ai board export"))
            return None
        self.commit_edits()
        from .bundle_dialogs import ExportDialog
        name = self.bundle_name()
        document = B.bundle_document(self.sai, self.board, name)
        applog.log("INFO", "board", f"Export…: the board {printable(name)} (host {printable(self.host())})")
        dialog = ExportDialog(self, self.sai, name, board=document, host=self.host(), owner=self)
        dialog.exec()
        widgets.drop_after_exec(dialog)
        return dialog.written

    def import_session_state(self):
        """Import a session with its state…: session_state_dialogs.ImportDialog (sandbox-ai session import, checked
        first) - the session it makes is on the Sandboxes page, to resume."""
        try:
            supported = self.sai.session_state_supported()
        except Exception as _exc:  # noqa: BLE001 - not known: said as not supported
            applog.swallowed("board", "import_session_state: not known", _exc)
            supported = False
        if not supported:
            warn(self, tr("Import a session"), tr("This sandbox-ai cannot import a session yet ({command}): update it.",
                                                  command="sandbox-ai session import"))
            return None
        from . import session_state_dialogs as SSD
        return SSD.open_import(self, self.sai, owner=self)

    def import_board(self, path=None):
        """Import…: a board's or session's file taken in on the board's host (bundle_dialogs.ImportDialog: its plan
        first, Import asked), then shown here - not started."""
        if not self.sai.board_bundle_supported():
            warn(self, tr("Import a board"), tr("This sandbox-ai cannot import a board yet ({command}): update it.",
                                                command="sandbox-ai board import"))
            return None
        from .bundle_dialogs import ImportDialog
        dialog = ImportDialog(self, self.sai, host=self.host(), path=path, owner=self,
                              imported=lambda document, host: alive(self) and self.show_imported(document, host))
        dialog.exec()
        widgets.drop_after_exec(dialog)
        return dialog.done_with

    def show_imported(self, document, host):
        """The board `sandbox-ai board import` kept (DOCUMENT), shown for its main workspace (on HOST: its folder there),
        after asking whether unapplied changes may go. Not started: Start does that, as for any board."""
        if host != self.host():
            self.show_notice(tr("Imported on {host}: choose that host to see the board.", host=printable(host)),
                             "notice-warn")
            return False
        try:
            board, folder = B.imported_board(self.sai, document)
            B.Board.checked(board.state(), self.sai, self.policy())  # (shown only when a board can show it)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.show_notice(tr("The imported board cannot be shown: {error}", error=printable_text(exc)),
                             "notice-warn")
            return False
        if not self.may_discard():
            return False
        if host == LOCAL:
            self.load(board, folder, positioned=True)
        else:
            self.load(board, None, positioned=True)
            self.remote_project = (host, folder) if folder else None
            self.show_folder()
        self.undo.resetClean()  # (not applied: no project file says this board - kept as a draft like a preset's)
        applog.log("INFO", "board", f"Import…: the board {printable(document.get('name'))} shown (not started)")
        self.show_notice(tr("Imported board {name}: check it, then Start (it was not started).",
                            name=printable(document.get("name"))), "notice-ok")
        return True

    def prerequisite_argv(self):
        """The launcher options the board starts with (what prerequisites.Prerequisites reads its needs from)."""
        return B.launcher_argv(self.sai, self.board, local=self.host() == LOCAL)

    def prerequisite_agent(self):
        return B.values(self.board)[0]

    def prerequisite_login_agents(self):
        """With the Team part: every member's agent (each with the team's options and its own), all on the board's
        host - asked for before team start runs there, as a single start's agent is; else the board's agent and those
        --with."""
        values = B.team_values(self.sai, self.board, local=self.host() == LOCAL) \
            if B.team_node(self.board) is not None else None
        if not values:
            return super().prerequisite_login_agents()
        from .prerequisites import login_needs
        found = []
        for agent in values["agents"]:
            own = [word for option, value in (values["member_options"].get(agent) or {}).items()
                   for word in (option, value)]
            found += login_needs(agent, [*values["options"], *own])
        return list(dict.fromkeys(found))

    def prerequisite_own_servers(self):
        return set(B.project_servers(self.board))

    def prerequisite_workspace(self, host, argv):
        """The start's workspace when it is a folder of HOST: on another host not this machine's project folder (the
        board of a project here with that host chosen), which is no folder there to offer an MCP server."""
        workspace = super().prerequisite_workspace(host, argv)
        return None if self.is_local_folder(host, workspace) else workspace

    def prerequisite_mcp_entries(self):
        return {n.props["name"]: n.props["entry"] for n in self.board.of_kind("mcp")
                if isinstance(n.props.get("name"), str) and isinstance(n.props.get("entry"), str)}

    def prerequisite_context(self):
        return (self.host(), self.board.serial, str(getattr(self, "directory", "") or ""))

    def say_left_to_fill(self):
        """The placeholders still to fill in, by the preset's names for them (the parts are outlined in red)."""
        left = B.placeholders(self.board)
        if left:
            names = ", ".join(B.placeholder_label(self.board, marker) for marker, _ in left)
            self.show_notice(tr("To fill in: {names}. Start asks for them; the parts are outlined in red.",
                                names=printable(names)), "notice-warn")

    def fill_in(self):
        """A form for every placeholder still on the board (the preset's labels and hints, the same fields as the
        gallery's): what is filled in goes onto the board as one undo step. True when none is left."""
        left = B.placeholders(self.board)
        if not left:
            return True
        from .preset_gallery import PlaceholderForm
        from . import presets as PR

        images = B.board_image_markers(self.board)  # a service's image: offered from your images

        class Left:  # what PlaceholderForm reads of a preset
            agent = None
            title = tr("the board")
            placeholders = tuple(PR.Placeholder(marker, B.placeholder_label(self.board, marker),
                                                (self.board.labels.get(marker) or ["", ""])[1] if
                                                len(self.board.labels.get(marker) or []) > 1 else "",
                                                names="image" if marker in images else "")
                                 for marker, _ in left)

            @staticmethod
            def text(value):
                return value
        form = PlaceholderForm(self.sai, Left, self, agent=B.values(self.board)[0], host=self.host())
        if widgets.AUTO_ANSWER is not None or widgets.exec_dropped(form) != QDialog.DialogCode.Accepted:
            return False
        values = {k: v for k, v in (form.values() or {}).items() if str(v or "").strip()}
        if values:
            self.change(tr("Fill in"), lambda: B.fill(self.board, values, self.sai))
        return not B.placeholders(self.board)

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
        widgets.drop_after_exec(wizard)  # (made anew each time; it wrote what it wrote before it closed)
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
        self.llm_problem = None
        self.__dict__.get("mcp_known", {}).pop(host, None)  # that host's MCP servers, read again (mcp_names)
        # that host's images, templates and variants, read again (a read of the host before: its answer dropped)
        self.user_images, self.image_bases, self.image_templates = None, None, None
        if remote:
            self.host_note_base = tr(
                "Starts on {host}: folders and files are {host}'s (… and Open project folder… list {host}'s folders), "
                "and its sandbox-ai checks the board.", host=printable(host))
            self.host_note.setText(self.host_note_base)
        if self.remote_project is not None and self.remote_project[0] != host:
            # (the board stays as it is; its folder is that other host's, which the label no longer claims for this)
            self.remote_project = None
            self.show_folder()
        self.inspect_timer.start()  # the path fields: a chooser of the host chosen now
        self.check_timer.start(0)

    def host_options_read(self, host, agent, found, error):
        self.host_reading.discard((host, agent))
        self.host_known[(host, agent)] = set(found) if error is None and found else None
        if self.host_known[(host, agent)] is None:
            self.host_failed[(host, agent)] = time.monotonic()  # asked again later, or when needed
        else:
            self.host_failed.pop((host, agent), None)
        if host == self.host() and host != LOCAL:
            # Said once, in place of what the read before said (it was added to the note at every failed read: after
            # some minutes on a host that does not answer the note was a paragraph of the same sentence), and taken
            # back once the options are read.
            base = getattr(self, "host_note_base", "") or self.host_note.text()
            self.host_note.setText(base if error is None else base + " " + tr(
                "Its options could not be read: {error}", error=printable_text(str(error))[-200:]))

    # -- actions --

    def missing_folders(self):
        """[(node id, folder)] of the board's workspaces not there on this machine (B.missing_workspaces); none for a
        start on another host (its folders are that host's)."""
        return B.missing_workspaces(self.board) if self.host() == LOCAL else []

    def create_missing(self, then=None, ask=False):
        """Create the board's workspace folders that are not there (the launcher's checks first: refused with the
        reason where a start would refuse it). ASK: after a short question (Start and Apply), THEN once they are
        made. True when nothing is missing (THEN is not run then: the caller goes on)."""
        folders = [folder for _node, folder in self.missing_folders()]
        if not folders:
            return True
        if ask and not confirm(self, tr("Create the folder"), tr(
                "This folder does not exist yet: {list}\n\nCreate it (with the folders above it) and go on?",
                list="\n".join(printable(folder) for folder in folders)), tr("Create")):
            return False

        said = []  # the launcher's warnings of a risky folder you chose (made all the same: you decide)

        def made(result, error):
            if error is not None:
                warn(self, tr("Not created"), printable_text(error))
                return
            self.show_notice(tr("Created: {list}", list=", ".join(printable(folder) for folder in result)))
            if said:
                warn(self, tr("Created, with a warning"), B.path_warnings(said), modal=False)
            self.refresh()
            if then is not None:
                then()
        background(self.sai.create_folders, folders, said, owner=self, work=True, source=self.create_button,
                   timeout=60, done=made)
        return False

    def build_missing(self):
        """Build now: the image builds the launcher's check named (B.build_offers), on the board's host, in a terminal
        tab of the app with their progress (else in the background, their output in the log window); the board is
        checked again at their end."""
        commands = list(self.build_commands)
        if not commands:
            return
        from . import preparing as P
        host = self.host()
        steps = [[" ".join(args), self.sai.command(*args) if host == LOCAL else self.sai.host_terminal(host, *args)]
                 for args in commands]
        in_tab = self.prerequisite_terminal()
        # Its progress on the Board as a start's preparation's (what is built, its step, how long; the line opens
        # the tab): "--variant NAME" a variant, else the image of an agent (or of the Tor VPN, ...).
        # (Every item runs in CHAIN, in the tab or in the background: its status file says how far.)
        preparation = P.Preparation([P.Item("build", args[args.index("--variant") + 1], in_tab=True)
                                     if "--variant" in args[:-1] else P.Item("image", args[-1], in_tab=True)
                                     for args in commands])
        # (Build now starts nothing: its last line says what follows, not "the start goes on")
        argv = self.chain_argv(preparation, steps, end=tr("the board is checked again; Start starts it"))
        self.build_button.setEnabled(False)
        self.track(preparation)
        applog.log("INFO", "board", f"Build now on {printable(host)}: " + "; ".join(
            "sandbox-ai " + applog.argv_text(args, 200) for args in commands)
            + (" (in a terminal tab)" if in_tab is not None else " (in the background, no terminal tab)"))

        def ended(status):
            applog.log("INFO" if status == 0 else "WARNING", "board", f"Build now ended: exit status {status}"
                       + ("; the board is checked again" if status == 0 else " - its terminal tab (or the entry "
                          "before) says why; the board is checked again"))
            if alive(self):
                self.untrack(preparation, status == 0)
                self.build_button.setEnabled(True)
                if status != 0:
                    self.show_notice(tr("The build failed (exit status {status}): its terminal tab says why.",
                                        status=status))
                self.check()
        if in_tab is not None and in_tab(argv, tr("Building the image"), ended=ended):
            return

        def built(result, error):
            if error is not None or result.returncode != 0:
                applog.log("ERROR", "board", f"{' '.join(argv[-1:])}: " + printable_text(
                    str(error) if error is not None else (result.stdout or "") + (result.stderr or ""))[-20000:])
            ended(1 if error is not None else result.returncode)
        import subprocess
        background(lambda: subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL),
                   owner=self, work=True, timeout=4 * 3600, done=built)

    def errors(self):
        """The board's errors (a service's image checked against your images: start reads them first), and what the
        launcher's check of this very board refused about its MCP servers."""
        found = [p for p in B.problems(self.sai, self.board, self.policy(), self.user_image_names(),
                                       templates=self.template_names()) if p.level == "error"]
        services = [n for n in self.board.included() if n.kind == "service" and not n.props.get("raw")]
        if services and getattr(self, "user_images_failed", False):
            # (not read: no service's image could be checked - the start would only be refused by the launcher)
            found.append(B.Problem("error", tr(
                "Your images on {host} could not be read (sandbox-ai images list), so the image of the service "
                "{name} is not checked: look at the Images page, Your images, then start again.",
                host=tr("this computer") if self.host() == LOCAL else printable(self.host()),
                name=printable(services[0].props.get("name") or "?")), services[0].id))
        if getattr(self, "launcher_state", None) == self.board.state():
            found += [p for p in getattr(self, "launcher_found", []) if p.level == "error"]
        return found

    def apply(self, then=None, lead=""):
        """Show the project file of the board and what changes, then write and trust exactly it (in a thread).
        THEN runs once it is written (a start that needs the file); LEAD is said before the question. True when
        the file is being written."""
        if self.host() != LOCAL:  # its folders are that host's: never a file of this machine at their paths
            inform(self, tr("Project configuration"), tr(
                "Apply writes the project file on this machine; on {host} the board starts as it is (--no-config)",
                host=printable(self.host())))
            return False
        errors = self.errors()
        if errors:
            warn(self, tr("Not written"), "\n".join(p.text for p in errors))
            return False
        document, main = B.project_document(self.sai, self.board)
        if not main:
            inform(self, tr("Project configuration"), tr("Add a workspace first: the file is written there."))
            return False
        if not self.create_missing(then=lambda: self.apply(then, lead), ask=True):
            return False  # (made first, after a yes: Apply goes on then)
        # Every key that will be trusted is named, what weakens the sandbox marked, and the details show the changes
        # against the version trusted LAST (never against the file in the folder: the agent may have written it).
        intro = tr("Write {path} with this content and trust exactly it? The details show the changes against the "
                   "version you trusted last, and the whole file.", path=f"{main}/.sandbox-ai.json")
        question, details = PT.question(self.sai, main, document, (lead + "\n\n" + intro) if lead else intro)
        # Trusted, the file's options apply at every later start of the project: asked first (Cancel the default).
        if not ask_first(self, "project-trust", tr("Write and trust"), question, tr("Write and trust"), details=details):
            return False
        at = (self.undo, self.undo.index())  # what is written: the board as it is now (it may change meanwhile)
        background(self.sai.write_project, main, document, owner=self, work=True, source=self.apply_button, timeout=60,
                   done=lambda result, error: self.applied(main, result, error, then, at))
        return True

    def applied(self, main, result, error, then=None, at=None):
        """The project file was written (ERROR: not). AT: (the undo stack, its index) of the board written - a board
        changed since, or another project's shown by now, is not marked as applied (what changed is no draft then,
        and would be lost)."""
        if error is not None:
            warn(self, tr("Not written"), printable_text(error))
            if then is not None:
                warn(self, tr("Not started"), tr("The project file was not written, so the start that needs it "
                                                 "did not happen."))
            return
        stack, index = at or (self.undo, self.undo.index())
        shown = stack is self.undo
        if shown:
            try:
                B.write_layout(main, self.board)
            except OSError:
                pass  # the layout is a convenience
            self.directory = main
            self.show_folder()
        try:
            if stack.index() == index:
                stack.setClean()
        except RuntimeError:  # the project's tab was closed meanwhile: its stack is gone
            pass
        if shown:
            self.show_notice("")
        self.untrusted.pop(self.key_of(main), None)
        try:  # the file just written and trusted is the source of the board's next draft
            self.sources[self.key_of(main)] = {"digest": self.sai.review_project(main).digest, "trusted": True}
        except (OSError, ValueError):
            self.sources.pop(self.key_of(main), None)
        if shown:  # (another project shown meanwhile stays shown)
            self.opened.emit(str(main))
        if then is not None:  # the start waited for the file: it goes on now (no second message in between)
            then()
            return
        short_notice(self, tr("Project configuration"), tr("Written and trusted: {path}", path=printable(result)))

    def dry_run(self):
        agent, _, _ = B.values(self.board)
        self.read_host_options(self.host(), agent, now=True)  # on demand, also after a failed read
        asked = self.dry_run_asked()
        background(self.sai.host_dry_run, self.host(), agent,
                   B.check_argv(self.sai, self.board, local=self.host() == LOCAL), owner=self,
                   source=self.dry_button, timeout=120,
                   done=lambda result, error: self.dry_run_done(result, error, asked))

    def dry_run_asked(self):
        """What a dry run is asked for (widgets.dry_run_answer): the board as it is, its host, the time."""
        return "board", self.board.state(), self.host(), time.strftime("%H:%M:%S")

    def dry_run_done(self, result, error, asked=None):
        """The launcher's plan (or refusal) of the board; one for a board changed meanwhile, or another host chosen,
        is said to be out of date, with the host and time it is from (R2-20)."""
        from .widgets import dry_run_answer, show_text
        suffix, note = dry_run_answer(asked, self.dry_run_asked()) if asked is not None else ("", "")
        lead = note + "\n\n" if note else ""
        plan, message = result if error is None else (None, str(error))
        if isinstance(error, backend.HostUnreachable):  # no answer of that host: no refusal of its launcher
            warn(self, tr("{host} does not answer", host=printable(error.host)), lead + printable_text(message))
            return
        if plan is None:
            warn(self, tr("The launcher refused these options") + suffix, lead + printable_text(message))
            return
        show_text(self, tr("Plan (dry run)") + suffix,
                  lead + (printable_text(message) + "\n\n" if message else "") + json.dumps(plan, indent=2))

    def start(self, embedded=False, session=None, recheck=True):
        """Start exactly the board (--no-config: a project file is not merged in on top)."""
        try:
            agent_ = B.values(self.board)[0]
        except Exception as exc:  # noqa: BLE001 - only for the log line
            agent_ = f"? ({type(exc).__name__})"
        applog.log("INFO", "board", f"Start: the board of {printable(self.directory or 'a new board')} with {agent_} "
                   f"on {printable(self.host())}" + (f", session {printable(session)}" if session else "")
                   + (" in a tab of the app" if embedded else "")
                   + ("" if recheck else " (after the launcher's check)"))
        self.starting_session = session or None  # (the login check asks whether that session has a login of its own)
        if B.placeholders(self.board) and not self.fill_in():  # asked for, by name, before anything else
            applog.log("INFO", "board", "not started: the board's placeholders are not all filled in")
            self.say_left_to_fill()
            return False
        if self.images_unread(lambda: self.start(embedded, session, recheck)):
            applog.log("INFO", "board", "the start waits for your images (sandbox-ai images list): its services' "
                                        "images are checked first")
            return False
        errors = self.errors()
        found = getattr(self, "launcher_found", [])
        if busy_now(self) and all(problem in found for problem in errors) and \
                self.check_prerequisites(then=lambda: self.start(embedded, session)):
            # What it needs is made right now (an MCP server, a variant, asked for when the preset was loaded): the
            # launcher's refusal of it ("Unknown MCP server") is about to be stale. It goes on once it is made; an
            # error of the board itself is said at once (below), as before.
            applog.log("INFO", "board", "the start waits for what is prepared for it now; it goes on once it is made")
            return False
        # The launcher refused only MCP servers the board uses that its host has not (yet): they are added from their
        # catalogs below - asked first, as for a preset - and the start goes on then. That refusal alone used to be
        # all a Start did ("Unknown MCP server: playwright, radare2 ..."; user report 2026-11-19).
        held = errors if errors and all(problem in found for problem in errors) and \
            self.unknown_servers_only(errors) else []
        if held:
            errors = []
            applog.log("INFO", "board", "the launcher's check refused MCP servers its host has not: they are read "
                                        "and added there first (asked), then the start goes on")
        if errors and recheck and all(problem in found for problem in errors):
            # Only what the launcher's last check refused - an image not built, an MCP server not added: it may be
            # there by now (built in a terminal meanwhile), and the check ran when the board last changed. Checked
            # again, then started or refused by that verdict (a stale one said "not built" once after the build).
            state, host = self.board.state(), self.host()
            # (only the board the human started: one changed or another host meanwhile is not started by itself)
            self.after_check = lambda: self.start(embedded, session, recheck=False) \
                if self.board.state() == state and self.host() == host else None
            self.show_notice(tr("The launcher checks the options…"))
            applog.log("INFO", "board", "the start waits for the launcher's check again (its last check refused: "
                       + "; ".join(applog.redact(p.text)[:200] for p in errors) + ")")
            self.check()
            return False
        if errors:
            self.say_not_started(errors)
            return False
        if not self.create_missing(then=lambda: self.start(embedded, session), ask=True):
            applog.log("INFO", "board", "the start waits: the board's missing folders are made first (or were not)")
            return False  # (its folders made first, after a yes: the start goes on then)
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
        # What it needs on the host (an identity, an image variant, a VPN profile, MCP servers) made last, once every
        # check above passed - nothing is made for a start that is refused anyway; then the start goes on.
        if self.check_prerequisites(then=lambda: self.start(embedded, session, recheck) if held else
                                    self.start(embedded, session)):
            applog.log("INFO", "board", "the start does not go on now: what it needs on the host is read, made or "
                                        "missing (the prerequisites' lines say which: it goes on by itself once "
                                        "made, not when something is missing)")
            return False
        if held:
            # What the launcher refused is there now (made, or found there - added elsewhere meanwhile), or could not be
            # read: its refusal is from before - checked again once, then started or refused by that verdict.
            if recheck:
                state, host = self.board.state(), self.host()
                self.after_check = lambda: self.start(embedded, session, recheck=False) \
                    if self.board.state() == state and self.host() == host else None
                self.show_notice(tr("The launcher checks the options…"))
                applog.log("INFO", "board", "the MCP servers the launcher refused are there now: checked again")
                self.check()
                return False
            self.say_held(held)
            return False
        if self.shared_unasked(lambda: self.start(embedded, session, recheck)):
            applog.log("INFO", "board", "the start waits: the board's wildcards below a shared service are asked about "
                                        "first")
            return False
        team = B.team_node(self.board) is not None
        # (a team: the launcher's check of the board's own agent is not what starts - each member's is, in team
        # start's verdict, which holds the start back itself)
        if not team and self.start_refused(embedded, session, recheck):
            return False
        if team:
            if servers:
                warn(self, tr("Not started"), tr("The board uses MCP servers the project defines ({names}); a team's "
                                                 "members start without the project file. Use MCP servers "
                                                 "registered with sandbox-ai mcp.", names=", ".join(servers)))
                return False
            return self.run_team_start(embedded)
        # Last, once everything above passed: is a session of that name there already (on this machine or the board's
        # host)? The same agent's that runs: its terminal. Another agent's, a paused one, one in the foreground: said
        # first, with what can be done - not left for the launcher's "is running; attach with" in a tab.
        state = self.board.state()

        def unchanged():
            return self.board.state() == state and self.host() == host
        if session and self.session_in_the_way(session, embedded, lambda name: unchanged() and self.start_now(
                servers, embedded, name), same=unchanged):
            applog.log("INFO", "board", f"the start waits: what is there under the session name {printable(session)} "
                                        f"on {printable(host)} is looked up first (or was said)")
            return False
        return self.start_now(servers, embedded, session)

    def shared_unasked(self, then):
        """True while the board's wildcards below a shared service (--allow-shared-wildcard: anyone can rent a name
        there) were not asked about for this very board on this host: their risk is read in the background
        (sai.shared_wildcards), asked (ask_first "shared-wildcard", naming the risk and the shared suffix), and THEN
        runs after a yes. A change of the board or of its host asks again."""
        values = [str(v) for v in B.values(self.board)[1].get("--allow-shared-wildcard") or []]
        key = (self.board.state(), self.host())
        if not values or self.__dict__.get("shared_asked") == key:
            return False

        def answered(found, error):
            if (self.board.state(), self.host()) != key:
                return  # changed meanwhile: a new Start asks again
            found = found if error is None else {}
            if error is None and not found:  # none is below a shared service: nothing to ask
                self.shared_asked = key
                then()
                return
            risks = [risk for risk, _ in found.values()] or [tr(
                "{wildcards}: below a shared service, where anyone can rent a name, the agent could open any server "
                "anyone rents there, and send data there", wildcards=", ".join(values))]
            text = tr("The board allows a wildcard below a shared service, where anyone can rent a name:\n\n{risks}"
                      "\n\nStart it on {host} anyway?", risks="\n".join(f"• {printable(r)}" for r in risks),
                      host=printable(key[1]))
            if ask_first(self, "shared-wildcard", tr("Start on {host}", host=printable(key[1])), text,
                         tr("Start anyway")):
                self.shared_asked = key
                then()
            else:
                applog.log("INFO", "board", "not started: the wildcards below a shared service were not confirmed")
        background(self.sai.shared_wildcards, values, owner=self, timeout=60, done=answered)
        return True

    def start_now(self, servers, embedded, session):
        """The start itself, once every check passed: from the project's file when the board uses MCP servers the
        project defines (SERVERS), else exactly the board."""
        if servers:
            return self.start_from_project_file(servers, embedded, session)
        return self.run_start(B.launcher_argv(self.sai, self.board, local=self.host() == LOCAL), embedded, session)

    def say_held(self, held):
        """Not started: the launcher still refuses MCP servers its host has not (HELD), after the host was read and
        what was missing added. Why, in words - the host did not say what it has, or lists them while its launcher
        does not know them - with what helps: Start again, the MCP servers page; never the launcher's words alone."""
        host = self.host()
        names = [n.strip() for problem in held for n in re.sub(r"^(?:agent-sandbox|sandbox-ai): ", "", problem.text)[
            len("Unknown MCP server: "):].split(";", 1)[0].split(",") if n.strip()]
        unread = self.__dict__.get("prerequisite_unread", {}).get("--mcp")
        if unread:
            text = tr("Not started: {host} did not say which MCP servers it has ({error}), so {names} could not be "
                      "added there. Start again to read it once more, or add them on the MCP servers page.",
                      host=printable(host), error=printable(unread), names=printable(", ".join(names)))
        else:
            text = tr("Not started: the launcher on {host} does not know the MCP servers {names}, although they were "
                      "looked for there: {refusal}", host=printable(host), names=printable(", ".join(names)),
                      refusal="\n".join(p.text for p in held))
        applog.log("WARNING", "board", "not started: " + applog.redact(text)[:600])
        self.show_missing(tr("Not started: the start needs what is not there yet."),
                          [(text, [(tr("Start again"), lambda: self.start()),
                                   (tr("Open the MCP servers page"), lambda: self.open_page("mcp"))])])
        warn(self, tr("Not started"), text)

    def prerequisites_made(self, host):
        """What the start's preparation made on HOST is there now: the MCP servers listed for it are read again."""
        self.__dict__.get("mcp_known", {}).pop(host, None)
        self.__dict__.get("mcp_read_at", {}).pop(host, None)

    def unknown_servers_only(self, errors):
        """Whether every one of ERRORS is the launcher's refusal of MCP servers the board uses and its host has not
        ("Unknown MCP server: NAME, NAME; defined: ..."): what the start's preparation adds there from a catalog
        (prerequisites), never one the project's file defines (Apply's)."""
        wanted = set(self.board_needs().get("--mcp", []))
        for problem in errors:
            text = re.sub(r"^(?:agent-sandbox|sandbox-ai): ", "", str(getattr(problem, "text", problem)))
            if not text.startswith("Unknown MCP server: "):
                return False
            names = [n.strip() for n in text[len("Unknown MCP server: "):].split(";", 1)[0].split(",")]
            if not names or not set(names) <= wanted:
                return False
        return bool(errors)

    def images_unread(self, then):
        """True while the board has services and your images are not read yet, or their last read failed (the check of
        their images needs them): they are read, and THEN runs once they are - for this very board on this host."""
        if not any(n.kind == "service" for n in self.board.included()) or self.user_image_names() is not None \
                or getattr(self, "images_just_read", False):
            return False
        state, host = self.board.state(), self.host()
        self.after_images = lambda: then() if self.board.state() == state and self.host() == host else None
        self.show_notice(tr("Reading your images (sandbox-ai images list)…"))
        self.load_user_images()
        return True

    def say_not_started(self, errors):
        """Not started, and why (ERRORS); one about a service's image offers the Images page (Your images)."""
        text = "\n".join(p.text for p in errors)
        mine = self.user_image_names()
        mine = None if mine is None else mine | self.template_names()
        lacking = {n.id for n in self.board.of_kind("service") if not n.props.get("raw") and (
            mine is None or not str(n.props.get("image") or "").strip() or n.props.get("image") not in mine)}
        if any(p.node in lacking for p in errors):
            if confirm(self, tr("Not started"), text, tr("Open the Images page"), reject=tr("Close")):
                self.images_wanted.emit()
            return
        warn(self, tr("Not started"), text)

    def start_refused(self, embedded, session, recheck):
        """True (said, or checked again first) when the launcher's last check refused this very board for a reason
        that is neither an MCP server nor an image (those are findings: errors()) - a missing secret, a mode's
        refusal: Start would only open a terminal that says it again. Checked once more first (it may be fixed by
        now: a secret kept in a terminal), then started or refused by that verdict."""
        refusal, state, host = getattr(self, "launcher_refusal", (None, None, None))
        if not refusal or state != self.board.state() or host != self.host():
            return False
        if recheck:
            board_state, board_host = self.board.state(), self.host()
            self.after_check = lambda: self.start(embedded, session, recheck=False) \
                if self.board.state() == board_state and self.host() == board_host else None
            self.show_notice(tr("The launcher checks the options…"))
            applog.log("INFO", "board", "the start waits for the launcher's check again (its last check refused: "
                       + applog.redact(refusal)[:200] + ")")
            self.check()
            return True
        applog.log("INFO", "board", "not started: the launcher refuses the board: " + applog.redact(refusal)[:300])
        warn(self, tr("Not started"), refusal)
        return True

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
        """Run the start in a terminal; a failure to start it is a message, never an exception in a slot. The Bridge
        parts are made once it runs (here or on the board's host): it is named for them (its session's name, the
        board's --name, or one given here)."""
        try:
            agent, _, agent_args = B.values(self.board, self.sai)
            host = self.host()
            bridged = B.bridge_nodes(self.board) and any(str(n.props.get("peer") or "").strip()
                                                         for n in B.bridge_nodes(self.board))
            name = None
            if bridged:
                name = session or next((options[i + 1] for i, word in enumerate(options[:-1]) if word == "--name"),
                                       None)
                if not name:
                    import secrets
                    name = f"{agent}-board-{secrets.token_hex(3)}"
                    options = [*options, "--name", name]
                self.bridge_entries(name, host)  # (what cannot be made is said before anything starts)
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
                applog.log("INFO", "board", f"the start on {printable(host)} goes to a terminal ({title}), options: "
                                            f"{applog.argv_text(options, 2000)}")
                self.start_terminal(argv, title, embedded, None, ended=self.start_watch(
                    host, agent, session, embedded, lambda name_: self.start(embedded, name_)) if embedded else None)
                self.session_started(session)
                if name:
                    self.start_bridges(name, host)  # (made once that host lists both as running)
                return True
            argv = self.sai.launch_argv(agent, options, agent_args, session or None)
            workspaces = self.board.of_kind("workspace")
            cwd = workspaces[0].props.get("path") if workspaces else None
            title = agent + (f" ({session})" if session else "")
            applog.log("INFO", "board", f"the start goes to a terminal ({title}, in {printable(cwd or '?')}), options: "
                                        f"{applog.argv_text(options, 2000)}")
            self.start_terminal(argv, title, embedded, cwd, ended=self.start_watch(
                host, agent, session, embedded, lambda name_: self.start(embedded, name_)) if embedded else None)
            self.session_started(session)
        except Exception as exc:  # noqa: BLE001 - the window stays; the reason is shown
            import traceback
            applog.log("WARNING", "board", f"the start failed: {applog.error_text(exc)}\n"
                       + applog.redact("".join(traceback.format_exception(exc))[-3000:]))
            warn(self, tr("Not started"), printable_text(exc))
            return False
        if name:
            self.start_bridges(name, host)
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
        applog.log("INFO", "board", f"the start uses the project's MCP servers {names}: its project file "
                                    f"{printable(main)}/.sandbox-ai.json is {state}")
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

