"""The Files page: a two-pane file manager. Left: the workspace or HOME of a sandbox or session (or another
folder); right: this machine, starting at HOME. Files go back and forth between the panes (copy, move) and
are deleted, renamed, given new folders, permissions and owners in either.

Everything goes through files.py: relative to open directory descriptors, never through a symbolic link (one
in the middle of a path or as the entry itself), never above the root, within budgets, cancellable, and a copy
cut short leaves nothing behind. Below a sandbox's folder - and in a folder of the right pane that lies in one -
an agent may have written everything, so what is copied out of it is never executable, files that tools run are
named, links are not followed even to open a folder, and no set-ID or sticky bit is set. Replacing and deleting
ask first. Copies, moves, deletes and owner changes run in a thread with progress and Cancel.

Each folder below the top of a pane begins with "..", the folder above (double click, Enter or Backspace). There is
no "." row: what it would offer - the folder shown as a drop target, its properties, paste into it - is the empty
part of the list and its context menu, and a "." row would only be one more thing to copy or delete by mistake.

A file opens in the app's own editor (file_editor.py: text, or a hex editor for other bytes) with a double click or
Enter; it is read and written through the pane's Root like everything else and never run. The context menu (a right
click) and the keys offer Open, View, Edit, the hex editor, Copy, Cut, Paste (an internal clipboard of the items, their
paths as text on the system clipboard too), copy or move to the other pane, Rename, Delete, New folder, Copy path and
Properties (file_properties.py).

Drag and drop: between the panes and onto a folder of the same pane a drop copies, with Shift it moves - through the
same copy and move as F5 and F6 (asking before anything is replaced). Files dropped from other programs (local files
only) are copied in; a path that goes through a symbolic link in a sandbox's folder is refused, as in the path bar.
Dragged out to other programs go the host pane's files and folders; from a sandbox's folder only plain files that are
not executable (a program dropped on would get folders with hooks or executables as they are, executable bits and all).

Keys (in a list): Tab the other pane, Enter open, Backspace up, F3 view, F4 edit, F5 copy, F6 move, F7 new folder, F8
or Delete delete, F2 rename, Ctrl+C copy, Ctrl+X cut, Ctrl+V paste, Alt+Enter properties, Ctrl+A select all, Ctrl+R
refresh, the menu key (or Shift+F10) the context menu.
"""
from __future__ import annotations

import grp
import os
import pwd
import secrets
import stat
import time

from PySide6.QtCore import QEvent, QItemSelectionModel, QMimeData, QPoint, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QDrag, QGuiApplication, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMenu, QSplitter, QVBoxLayout, QWidget,
)

from . import files
from .files import printable
from .i18n import N_, tr
from .state import choose_files, choose_folder
from .widgets import (
    ModelTable, RowModel, Task, ask_first, background, button, confirm, fill, find_row, flow, plain_label, row_key,
    select_keys, selected_rows, set_enabled, table, warn, shown_as_text,
)

ROOT_KINDS = {
    "run": N_("Running sandbox {name}: workspace"), "run-home": N_("Running sandbox {name}: HOME"),
    "session": N_("Session {name}: workspace"), "session-home": N_("Session {name}: HOME"),
    "project": N_("Current project"), "started-in": N_("Folder the app was started in"),
    "other": N_("Chosen folder"),
}
KINDS = {"dir": N_("folder"), "file": N_("file"), "link": N_("symbolic link"), "other": N_("special file")}
COLUMNS = (N_("Name"), N_("Type"), N_("Size"), N_("Modified"), N_("Points to (not followed)"), N_("Owner"),
           N_("Mode"))
LINK_COLUMN = 4
PROGRESS_INTERVAL = 250  # ms
# A folder is listed in a thread (a slow or hung file system never holds up the window), its rows made there too; it
# shows when it comes (a fast folder at once). Still on its way after LIST_WAIT ms: the pane says so.
LIST_WAIT = 1500
LIST_TIMEOUT = 120  # s: a listing that never comes back is given up (the pane says so)
UP = ".."  # the first row of a folder below the top of a pane: the folder above (never the name of an entry)
# What a drag between the panes and the page's clipboard carry besides the paths: a token of this app's own (what was
# dragged or copied is kept in the page, never read back from the data, which another program could forge).
MIME = "application/x-sandbox-ai-app-files"
# The widths the columns start with (fitted to rows of this kind before the first folder is shown): a folder with
# longer texts makes a column wider, never narrower again, so the columns do not jump from folder to folder.
SEED_ROW = ("a-file-name-of-some-length.txt", "", "1023.9 KiB", "2026-12-31 23:59", "→ ../a/link/target",
            "username:groupname", "drwxrwxrwx 0777")


def root_title(kind, label, path):
    template = ROOT_KINDS.get(kind, "Chosen folder")
    return f"{tr(template, name=printable(label))}  ({printable(path)})"


def host_relative(path):
    """PATH (absolute, without links) relative to the host's Root("/")."""
    return os.path.normpath(path).lstrip("/")


def below(path, folder):
    folder = folder.rstrip("/") or "/"
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def user_name(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return str(uid)


def group_name(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except (KeyError, OverflowError):
        return str(gid)


def owner_text(entry, ranges):
    """user:group; an ID a rootless Podman sandbox used (a subordinate ID of the user's) is marked."""
    if entry.uid < 0:
        return ""
    if any(first <= entry.uid < first + count for first, count in ranges):
        who = tr("{uid} (a sandbox's)", uid=entry.uid)
    else:
        who = user_name(entry.uid)
    return f"{printable(who)}:{printable(group_name(entry.gid))}"


def entry_rows(entries, ranges, untrusted):
    """(rows, colours) of the Files table for ENTRIES (in the listing's thread: a folder may have 20,000); UNTRUSTED:
    an agent may have written there (special mode bits are marked)."""
    rows, colors = [], []
    # The owner's text once per user:group of this listing: the user and group databases were asked twice per entry,
    # 70 % of the time of a folder's rows (most entries of a folder share one owner).
    owners = {}
    for entry in entries:
        owner = owners.get((entry.uid, entry.gid))
        if owner is None:
            owner = owners[(entry.uid, entry.gid)] = owner_text(entry, ranges)
        rows.append([printable(entry.name), tr(KINDS[entry.kind]),
                     files.size_text(entry.size) if entry.kind == "file" else "",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime)),
                     "→ " + printable(entry.target) if entry.kind == "link" else "",
                     owner, mode_text(entry.mode)])
        colors.append("warn" if entry.kind == "link" or entry.mode & files.SPECIAL_BITS and untrusted
                      else "muted" if entry.kind == "other" else None)
    return rows, colors


def mode_text(mode):
    return f"{stat.filemode(mode)} {stat.S_IMODE(mode):04o}" if mode else ""


def copy_name(name, number=1):
    """The name of a copy of NAME beside it: "a (copy).txt", "a (copy 2).txt" (a folder's: "src (copy)")."""
    stem, dot, extension = name.rpartition(".")
    if not stem or "/" in extension:  # no extension (".bashrc": the whole name)
        stem, dot, extension = name, "", ""
    mark = tr("copy") if number == 1 else tr("copy {number}", number=number)
    return f"{stem} ({mark}){dot}{extension}"


def nav_button(text, tooltip, slot):
    """A button that goes somewhere (Home, Up, Refresh): an action, never a toggle, and it takes no focus from a
    click (a focused button looks pressed in some themes, as if Home stayed on); Tab still reaches it. Where the
    pane is shows in its path bar."""
    widget = button(text, tooltip, slot)
    widget.setCheckable(False)
    widget.setAutoDefault(False)
    widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    return widget


class FileRows(RowModel):
    """A pane's rows: ".." stays the first row, whatever column the list is sorted by."""

    def ordered(self, rows, column, order):
        found = super().ordered(rows, column, order)
        up = [row for row in found if row.index == 0 and row.values and row.values[0] == UP]
        return up + [row for row in found if row is not up[0]] if up else found


class FileTable(ModelTable):
    """A pane's list. A row selected and dragged further than the drag distance drags the selection (to the other
    pane, onto a folder, to other programs: FilesPage.start_drag); files dropped on it go into the folder whose row they
    are dropped on (framed while dragged over), elsewhere into the folder shown (the list framed). A click on a row
    that is part of a selection keeps the selection until the button is released (Qt's own), so several are dragged."""
    model_class = FileRows

    def __init__(self, headers, numeric=(), stretch=None):
        super().__init__(headers, numeric, stretch)
        self.pane = None  # set by its Pane
        self.press = None  # where the left button went down on a selected row
        self.drop_row = None  # framed while something is dragged over: a row, -1 the whole list, None nothing
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    # -- dragging out --

    def mousePressEvent(self, event):  # noqa: N802
        self.press = None
        super().mousePressEvent(event)
        modifiers = event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and not modifiers and index.isValid() and \
                self.selectionModel().isRowSelected(index.row()):
            self.press = event.position().toPoint()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self.press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (event.position().toPoint() - self.press).manhattanLength() >= QApplication.startDragDistance():
                self.press = None
                self.setState(QAbstractItemView.State.NoState)  # (the drag takes the button's release)
                if self.pane is not None:
                    self.pane.page.start_drag(self.pane)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self.press = None
        super().mouseReleaseEvent(event)

    # -- dropping in --

    def plan(self, event):
        if self.pane is None:
            return None
        return self.pane.page.drop_plan(self.pane, event.mimeData(), event.position().toPoint(), event.modifiers())

    def dragEnterEvent(self, event):  # noqa: N802
        mime = event.mimeData()
        if self.pane is None or not (mime.hasFormat(MIME) or mime.hasUrls()):
            event.ignore()
            return
        plan = self.plan(event)
        self.show_drop(plan)
        if plan is not None:
            event.setDropAction(plan.action)
        event.accept()  # (also where it cannot be dropped yet: the moves over the list follow)

    def dragMoveEvent(self, event):  # noqa: N802
        plan = self.plan(event)
        self.show_drop(plan)
        if plan is None:
            event.ignore()
            return
        event.setDropAction(plan.action)
        event.accept()

    def dragLeaveEvent(self, event):  # noqa: N802
        self.show_drop(None)
        event.accept()

    def dropEvent(self, event):  # noqa: N802
        plan = self.plan(event)
        self.show_drop(None)
        if plan is None:
            event.ignore()
            return
        event.setDropAction(plan.action)
        event.accept()
        # After the drop has ended (a question asked inside a drop handler would hold up the program it came from).
        QTimer.singleShot(0, self, lambda: self.pane.page.run_drop(plan))

    def show_drop(self, plan):
        row = None if plan is None else plan.row
        if row != self.drop_row:
            self.drop_row = row
            self.viewport().update()
        if self.pane is not None:
            self.pane.page.drop_hint(plan)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if self.drop_row is None:
            return
        if self.drop_row >= 0:
            rect = self.visualRect(self.model().index(self.drop_row, 0))
            rect.setLeft(0)
            rect.setRight(self.viewport().width() - 1)
        else:
            rect = self.viewport().rect()
        painter = QPainter(self.viewport())
        pen = QPen(self.palette().color(QPalette.ColorRole.Highlight))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))
        painter.end()


class Clip:
    """Items of one pane's folder that were copied, cut or dragged: ROOT and [(name, relative path, name in the target
    or None)]; UNTRUSTED when an agent may have written them; FOLDER_TEXT where they are (shown). TOKEN: what the
    clipboard or the drag carries to find them again."""

    def __init__(self, root, items, untrusted, folder_text, cut=False):
        self.token = secrets.token_hex(16)
        self.root, self.items, self.untrusted, self.folder_text, self.cut = root, items, untrusted, folder_text, cut
        self.problems = []  # what was refused before (host_items)

    def owns(self, mime):
        return mime is not None and mime.hasFormat(MIME) and bytes(mime.data(MIME)).decode("ascii", "replace") == \
            self.token


class DropPlan:
    """What a drop at one place does: copy or move (ACTION) CLIP's items, or copy the host PATHS, into TARGET_DIR of
    PANE; ROW is framed (-1: the list)."""

    def __init__(self, pane, target_dir, row, action, clip=None, paths=()):
        self.pane, self.target_dir, self.row, self.action, self.clip, self.paths = \
            pane, target_dir, row, action, clip, list(paths)

    @property
    def move(self):
        return self.action == Qt.DropAction.MoveAction

    def text(self):
        count = len(self.clip.items) if self.clip is not None else len(self.paths)
        where = printable(self.pane.path_at(self.target_dir))
        if self.move:
            return tr("Drop: move {count} item(s) into {folder}.", count=count, folder=where)
        if self.clip is not None:
            return tr("Drop: copy {count} item(s) into {folder} (with Shift: move).", count=count, folder=where)
        return tr("Drop: copy {count} item(s) into {folder}.", count=count, folder=where)


class Chosen:
    """What an action on entries of a pane is about, taken before it asks: a question or dialog blocks nothing, so by
    the answer the pane may show another folder or another sandbox - the answer acts on these. ROOT, ITEMS [(name,
    relative path)], RELATIVE (their folder) and FOLDER_TEXT (shown), UNTRUSTED (an agent may have written there),
    SANDBOX (the sandbox pane's), FOLDERS (one of them is a folder)."""

    def __init__(self, root, items, relative="", folder_text="", untrusted=True, sandbox=True, folders=False):
        self.root, self.items, self.relative, self.folder_text = root, list(items), relative, folder_text
        self.untrusted, self.sandbox, self.folders = untrusted, sandbox, folders

    @classmethod
    def of(cls, pane, entries):
        return cls(pane.root, [(entry.name, pane.path_of(entry)) for entry in entries], pane.relative,
                   pane.path_text(), pane.untrusted(), pane.side == "sandbox",
                   any(entry.kind == "dir" for entry in entries))

    def shown_in(self, pane):
        """Whether PANE still shows the folder of these items."""
        return pane.root is self.root and pane.relative == self.relative


class Pane(QWidget):
    """One side: a folder below a root, its list and its path bar. SIDE "sandbox" (roots to choose from) or
    "host" (this machine, Root("/") walked down to the folder shown)."""

    def __init__(self, page, side):
        super().__init__()
        self.page, self.side = page, side
        self.root, self.relative, self.entries, self.by_name = None, "", [], {}
        self.list_token = 0  # the newest listing: one that comes back after another began is dropped
        self.listing = None  # the token of the listing on its way, or None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.title = QLabel(tr("Sandbox") if side == "sandbox" else tr("This machine"))
        top.addWidget(self.title)
        if side == "sandbox":
            self.roots = QComboBox()
            self.roots.setAccessibleName(tr("Folder of a sandbox"))
            self.title.setBuddy(self.roots)
            self.roots.setMinimumWidth(240)
            self.roots.currentIndexChanged.connect(lambda _=0: self.choose_root())
            top.addWidget(self.roots, 1)
            top.addWidget(nav_button(tr("Refresh"), tr("Look for sandboxes and sessions again"), self.refresh_roots))
            top.addWidget(nav_button(tr("Other folder…"), tr("Browse another folder as an agent's"), self.add_other))
        else:
            self.roots = None
            top.addStretch()
            top.addWidget(nav_button(tr("Home"), tr("Your HOME folder"), self.home))
            self.root = files.Root("/", label=tr("This machine"), sandbox=False)
            self.relative = host_relative(os.path.realpath(os.path.expanduser("~")))
        layout.addLayout(top)
        where = QHBoxLayout()
        where.addWidget(nav_button(tr("Up"), tr("The folder above (Backspace)"), self.up))
        self.path = QLineEdit()
        self.path.setAccessibleName(tr("Folder"))
        self.path.setToolTip(tr("Type a folder and press Enter"))
        self.path.returnPressed.connect(self.go_path)
        where.addWidget(self.path, 1)
        where.addWidget(nav_button(tr("Refresh"), tr("List the folder again (Ctrl+R)"), self.list))
        layout.addLayout(where)
        # The Name takes the room. A ModelTable: a folder of 20,000 entries is shown in one step.
        self.table = table([tr(column) for column in COLUMNS], multi=True, stretch=0, many=True,
                           compact=(1, 2, 3, 5, 6), view=FileTable)
        self.table.pane = self
        self.table.setAccessibleName(tr("Files of the sandbox") if side == "sandbox" else tr("Files of this machine"))
        self.table.setTabKeyNavigation(False)
        # Always there: a scroll bar coming and going with the length of a folder would change the list's width, and
        # with it the columns', at every change of folder.
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.table.cellDoubleClicked.connect(self.open_row)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(lambda point: page.context_menu(self, point))
        self.table.installEventFilter(page)
        self.shown = None  # the folder the list shows (relative): another one is shown from its top
        self.seed_columns()
        layout.addWidget(self.table, 1)
        self.note = plain_label(role="secondary")
        self.note.hide()
        layout.addWidget(self.note)

    def seed_columns(self):
        """Fit the columns to rows of the widths they will mostly need (SEED_ROW, in the app's language) before the
        first folder is listed: later folders hardly ever widen a column, so the list does not jump."""
        kinds = max((tr(kind) for kind in KINDS.values()), key=len)
        try:
            group = group_name(os.getgid())
        except Exception:  # noqa: BLE001 - only a width
            group = "groupname"
        row = list(SEED_ROW)
        row[1], row[5] = kinds, max([row[5], f"{user_name(os.getuid())}:{group}"], key=len)
        fill(self.table, [row], keys=[""])
        fill(self.table, [])

    # -- where --

    def path_text(self):
        return self.path_at(self.relative)

    def path_at(self, relative):
        """The absolute path (shown) of the folder RELATIVE of this pane."""
        if self.root is None:
            return ""
        if self.side == "host":
            return "/" + relative
        return self.root.path + ("/" + relative if relative else "")

    def path_of_entry(self, entry):
        """The absolute path (shown) of ENTRY of the folder shown."""
        return os.path.join(self.path_text(), entry.name)

    def untrusted(self):
        """True if an agent may have written what is shown: a sandbox root, or a host folder in one."""
        return self.untrusted_at(self.relative)

    def untrusted_at(self, relative):
        return self.side == "sandbox" or self.page.below_sandbox(self.path_at(relative))

    def can_go_up(self, relative=None):
        """Whether the folder RELATIVE (default: the one shown) has one above it in this pane."""
        return self.root is not None and bool(files.parts(self.relative if relative is None else relative))

    def go(self, relative, then=None):
        relative = "/".join(files.parts(relative))
        if relative != self.relative:
            self.table.clearSelection()  # names selected in another folder mean other files here
        self.relative = relative
        return self.list(then)

    def up(self):
        """The folder above; the folder come from is the current row there."""
        if not self.can_go_up():
            return
        components = files.parts(self.relative)
        self.go("/".join(components[:-1]), then=lambda listed: listed and self.focus_name(components[-1]))

    def focus_name(self, name):
        """Select the row of NAME, make it the current one and scroll to it."""
        self.select([name])
        row = find_row(self.table, name)
        if row >= 0:
            index = self.table.model().index(row, 0)
            self.table.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollTo(index)

    def home(self):
        self.go(host_relative(os.path.realpath(os.path.expanduser("~"))))

    def go_path(self, text=None):
        """Show the folder typed in the path bar (absolute, or relative to the folder shown)."""
        text = (self.path.text() if text is None else text).strip()
        if self.root is None or not text:
            return False
        try:
            if self.side == "host":
                path = os.path.expanduser(text)
                path = os.path.normpath(path if os.path.isabs(path) else os.path.join(self.path_text(), path))
                real = os.path.realpath(path)
                if real != path and self.page.below_sandbox(path):
                    raise files.Refused(tr("{path} goes through a symbolic link in a sandbox's folder; it is not "
                                           "followed.", path=printable(path)))
                return self.go(host_relative(real))
            else:
                base = self.root.path.rstrip("/")
                if text == base or text.startswith(base + "/"):
                    relative = text[len(base):]
                elif text.startswith("/"):
                    raise files.Refused(tr("Not a path below the folder: {path}", path=printable(text)))
                else:
                    relative = f"{self.relative}/{text}" if self.relative else text
                return self.go(relative.strip("/"))
        except (OSError, ValueError) as exc:
            self.page.message.setText(printable(exc))
            self.path.setText(self.path_text())
            return False

    # -- the sandbox side's roots --

    def refresh_roots(self, select=None, then=None):
        """The folders of sandboxes, sessions and projects, read in the background (sandbox-ai's registry may ask
        Podman); the list changes when they are known. SELECT: the folder to show then; THEN() after that."""
        if self.roots is None:
            return
        self.roots_token, self.roots_reading = getattr(self, "roots_token", 0) + 1, True
        token, keep = self.roots_token, select or self.roots.currentData()
        extra = list(self.page.projects()) + self.page.others
        background(lambda: self.page.sai.file_roots(extra=extra), owner=self.page, timeout=60, work=False,
                   done=lambda roots, error: token == self.roots_token and self.show_roots(
                       roots if error is None else [entry for entry in extra if os.path.isdir(entry[2])], keep, then))

    def show_roots(self, roots, keep, then=None):
        self.roots_reading = False
        self.roots.blockSignals(True)
        self.roots.clear()
        for kind, label, path in roots:
            self.roots.addItem(root_title(kind, label, path), path)
        remote = self.page.remote_hosts()
        for name in remote:
            self.roots.addItem(tr("Sandboxes on {host}: not available here yet", host=printable(name)), None)
            model = self.roots.model()  # a QStandardItemModel: its items can be disabled
            item = model.item(self.roots.count() - 1) if hasattr(model, "item") else None
            if item is not None:
                item.setEnabled(False)
        self.note.setText(tr("The files of sandboxes on other hosts cannot be shown here yet; a command line for "
                             "them comes later.") if remote else "")
        self.note.setVisible(bool(remote))
        index = self.roots.findData(keep) if keep else -1
        self.roots.setCurrentIndex(index if index >= 0 else 0)
        self.roots.blockSignals(False)
        self.choose_root()
        if then is not None:
            then()

    def add_other(self):
        path = choose_folder(self, tr("Folder to browse"), "browse")
        if path:
            self.page.others.append(("other", "", path))
            self.refresh_roots(select=os.path.realpath(path))

    def choose_root(self):
        path = self.roots.currentData() if self.roots is not None else None
        self.root, self.relative, self.shown = None, "", None
        self.table.clearSelection()
        if path:
            try:
                self.root = files.Root(path)
            except OSError as exc:
                self.page.message.setText(tr("Cannot open {path}: {error}", path=printable(path),
                                             error=printable(exc)))
        self.list()

    # -- the list --

    def list(self, then=None):
        """List the folder shown, in a thread, its rows made there too (a folder of 20,000 entries); shown when it comes
        (the newest listing only). True when the listing began, False without a root (nothing to list). THEN(listed)
        once it is shown: listed is False when the folder could not be listed (the root's own folder, or HOME, is
        shown then). No local event loop: a click during a listing is handled as any other, in turn."""
        self.list_token += 1
        token = self.list_token
        if self.root is None:
            self.entries, self.by_name, self.listing = [], {}, None
            self.path.setText("")
            fill(self.table, [])
            if then is not None:
                then(False)
            return False
        root, relative = self.root, self.relative
        fallback = "" if self.side == "sandbox" else host_relative(os.path.realpath(os.path.expanduser("~")))
        ranges, side = self.page.ranges, self.side
        folders = [] if side == "sandbox" else self.page.sandbox_folders()  # (widgets: read here, not in the thread)

        def work():
            try:
                entries, shown, problem = root.list(relative), relative, None
            except (OSError, ValueError) as exc:
                try:
                    entries, shown, problem = root.list(fallback), fallback, exc
                except (OSError, ValueError):
                    entries, shown, problem = [], fallback, exc
            # Whether an agent may have written there (Pane.untrusted), for the folder actually listed.
            inside = side == "sandbox" or any(below("/" + shown, folder) for folder in folders)
            return entries, shown, problem, entry_rows(entries, ranges, inside), inside
        self.listing = token
        QTimer.singleShot(LIST_WAIT, self, lambda: self.listing == token and self.page.message.setText(
            tr("Listing {path}…", path=printable(self.path_text()))))
        background(work, timeout=LIST_TIMEOUT, owner=self,  # (a late listing never reaches a pane deleted meanwhile)
                   done=lambda result, error: self.listed(token, root, result, error, then))
        return True

    def listed(self, token, root, result, error, then=None):
        """Show what the listing of TOKEN found (dropped when another began since, or another root was chosen)."""
        if token != self.list_token or root is not self.root:
            return None
        self.listing = None
        if error is not None:  # the thread itself failed (or never came back)
            self.page.message.setText(printable(error))
            self.entries, self.by_name = [], {}
            fill(self.table, [])
            if then is not None:
                then(False)
            return False
        self.entries, relative, problem, (rows, colors), inside = result
        self.by_name = {entry.name: entry for entry in self.entries}
        listed = problem is None
        if not listed:
            self.page.message.setText(printable(problem))
            self.relative = relative
            self.table.clearSelection()
        elif self.page.message.text().startswith(tr("Listing {path}…", path="").rstrip("…")):
            self.page.message.setText("")
        self.path.setText(printable(self.path_text()))
        keys = [entry.name for entry in self.entries]
        if self.can_go_up(relative):  # ".." first: the folder above (not an entry: chosen() never has it)
            rows, colors, keys = [[UP, "", "", "", "", "", ""], *rows], [None, *colors], [UP, *keys]
        moved = relative != self.shown
        # One paint for the new rows (the model is reset, the columns fitted, the list scrolled): nothing in between
        # shows for a moment.
        self.table.setUpdatesEnabled(False)
        try:
            if moved:  # (selected in the rows of the folder before: other files here)
                self.table.clearSelection()
            fill(self.table, rows, colors, keys=keys)
            if moved:  # another folder: from its top (a folder listed again keeps where it was scrolled to)
                self.table.scrollToTop()
                if rows:
                    self.table.selectionModel().setCurrentIndex(self.table.model().index(0, 0),
                                                                QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self.table.setUpdatesEnabled(True)
        self.shown = relative
        if self.side == "host":
            self.note.setText(tr("This folder belongs to a sandbox: what is copied from here is treated like the "
                                 "agent's files (never executable, links not followed).") if inside else "")
            self.note.setVisible(inside)
        if then is not None:
            then(listed)
        return listed

    def path_of(self, entry):
        return f"{self.relative}/{entry.name}" if self.relative else entry.name

    def changing(self):
        """True while the rows are another folder's: go() (or another root) takes the new folder at once, its rows
        come with its listing - until then no row is an entry of the folder RELATIVE (none is chosen, none opens)."""
        return self.listing is not None and self.shown != self.relative

    def waiting(self):
        """CHANGING(), said in the page's message: an action on entries waits for the listing."""
        if not self.changing():
            return False
        self.page.message.setText(tr("Wait until {path} is listed: the list still shows the folder before.",
                                     path=printable(self.path_text())))
        return True

    def entry_at(self, row):
        """The entry shown in the visual ROW (found by its name: the table may be sorted), or None."""
        return None if self.changing() else self.by_name.get(row_key(self.table, row))

    def chosen(self):
        return [entry for entry in (self.entry_at(row) for row in selected_rows(self.table)) if entry is not None]

    def current(self):
        """The entry of the current row, or the first one selected."""
        entry = self.entry_at(self.table.currentRow()) if self.table.currentRow() >= 0 else None
        chosen = self.chosen()
        return entry if entry is not None and (not chosen or entry in chosen) else (chosen[0] if chosen else None)

    def is_up(self, row):
        return row >= 0 and not self.changing() and row_key(self.table, row) == UP

    def open_row(self, row, _column=0):
        """A double click or Enter on ROW: ".." goes up, a folder is entered, a file opens in the editor."""
        if self.waiting():
            return
        if self.is_up(row):
            self.up()
            return
        entry = self.entry_at(row)
        if entry is not None:
            self.open_entry(entry)

    def open_entry(self, entry, mode="auto", read_only=False):
        """Enter the folder ENTRY, or open the file ENTRY in the editor (MODE "auto", "text" or "hex")."""
        if entry.kind == "dir":
            if mode == "auto":
                self.go(self.path_of(entry))
            else:
                self.page.message.setText(tr("{name} is a folder.", name=printable(entry.name)))
        elif entry.kind == "file":
            self.page.open_editor(self.root, self.path_of(entry), self.path_of_entry(entry), mode, read_only)
        elif entry.kind == "link":
            self.open_link(entry, mode, read_only)
        else:
            self.page.message.setText(tr("{name} is not a regular file.", name=printable(entry.name)))

    def open_link(self, entry, mode="auto", read_only=False):
        """A link is followed only on the host and out of every sandbox's folder (where it lies and where it points);
        it opens the folder or the file it points to."""
        not_followed = tr("{name} is a symbolic link to {target}; it is not followed.", name=printable(entry.name),
                          target=printable(entry.target))
        if self.untrusted():
            self.page.message.setText(not_followed)
            return
        real = os.path.realpath(os.path.join(self.path_text(), entry.name))
        if self.page.below_sandbox(real):
            self.page.message.setText(not_followed)
        elif os.path.isdir(real) and mode == "auto":
            self.go(host_relative(real))
        elif os.path.isfile(real) and self.page.right.root is not None:
            self.page.open_editor(self.page.right.root, host_relative(real), real, mode, read_only)
        else:
            self.page.message.setText(not_followed)

    def select(self, names):
        select_keys(self.table, list(names))


class OwnerDialog(QDialog):
    """Owner and group for chown: only you, and only one of your groups."""

    def __init__(self, parent, count, folders):
        super().__init__(parent)
        self.setWindowTitle(tr("Owner and group"))
        form = QFormLayout(self)
        form.addRow(plain_label(tr("Owner and group of {count} item(s):", count=count)))
        self.owner = QComboBox()
        self.owner.addItem(tr("unchanged"), -1)
        self.owner.addItem(f"{printable(user_name(os.getuid()))} ({os.getuid()})", os.getuid())
        self.group = QComboBox()
        self.group.addItem(tr("unchanged"), -1)
        for gid in files.user_groups():
            self.group.addItem(f"{printable(group_name(gid))} ({gid})", gid)
        self.recursive = QCheckBox(tr("Also everything in the folders"))
        self.recursive.setEnabled(folders)
        form.addRow(tr("Owner"), self.owner)
        form.addRow(tr("Group"), self.group)
        form.addRow(self.recursive)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.owner.currentData(), self.group.currentData(), self.recursive.isChecked()


class FilesPage(QWidget):
    """Two panes: a sandbox's folder and this machine (see the module's docstring). The attributes of the old
    one-pane page (roots, table, relative, entries, go, up, list, open_row, chosen, download, upload, replace)
    stand for the sandbox pane."""

    def __init__(self, sai, projects=lambda: []):
        super().__init__()
        self.sai, self.projects = sai, projects
        self.others, self.last_report = [], []
        self.ranges = files.subordinate_ranges()
        self.message = plain_label()
        # Copied or cut items (Ctrl+C, Ctrl+X), what is dragged now, the editors and dialogs open.
        self.clipboard, self.drag, self.editors, self.dialogs = None, None, [], []
        self.left = Pane(self, "sandbox")
        self.right = Pane(self, "host")
        self.active = self.left
        layout = QVBoxLayout(self)
        # The two panes half and half (a width the human dragged, or the one the app kept from before, stays).
        splitter = self.split = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1, 1])
        self.keep_split = False  # True: a position moved by the human or kept by the app (state.splitter) wins
        splitter.splitterMoved.connect(lambda *_: setattr(self, "keep_split", True))
        layout.addWidget(splitter, 1)
        self.buttons = {
            "copy": button(tr("Copy (F5)"), tr("Copy the selected items into the other pane's folder"),
                           lambda: self.copy_selected()),
            "move": button(tr("Move (F6)"), tr("Move the selected items into the other pane's folder: copied, the "
                                               "source removed only when the copy is complete"),
                           lambda: self.copy_selected(move=True)),
            "mkdir": button(tr("New folder (F7)"), tr("A new folder in the active pane"), lambda: self.mkdir_dialog()),
            "delete": button(tr("Delete (F8)"), tr("Delete the selected items (asks first)"),
                             lambda: self.delete_selected(), role="danger"),
            "rename": button(tr("Rename (F2)"), tr("Rename the selected item"), lambda: self.rename_dialog()),
            "chmod": button(tr("Permissions…"), tr("Change the permission bits (never set-ID or sticky bits in a "
                                                   "sandbox's folder)"), lambda: self.chmod_dialog()),
            "chown": button(tr("Owner…"), tr("Give the selected items to you or one of your groups"),
                            lambda: self.chown_dialog()),
            "own": button(tr("Take ownership…"), tr("Take back what a rootless Podman sandbox wrote as another user "
                                                    "(podman unshare chown)"), lambda: self.own_selected()),
        }
        self.replace = QCheckBox(tr("Replace existing files"))
        self.replace.setToolTip(tr("Without it, names that exist in the target are left alone; with it, you are "
                                   "asked before they are replaced"))
        for widget in self.buttons.values():  # a toolbar's actions: no focus from a click (it would look pressed)
            widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        layout.addWidget(flow(*self.buttons.values(), self.replace))
        # Copies (up to 1 GiB), moves, deletes and owner changes run in a thread with Cancel; one at a time.
        self.task = Task()
        self.task.changed.connect(self.running)
        layout.addWidget(self.task)
        layout.addWidget(self.message)
        note = plain_label(tr("Symbolic links are shown but never followed or copied; copies stop at {limit} MiB.",
                              limit=files.LIMIT >> 20))
        note.setEnabled(False)
        layout.addWidget(note)
        self.progress, self.progress_text = None, ""
        self.timer = QTimer(self)
        self.timer.setInterval(PROGRESS_INTERVAL)
        self.timer.timeout.connect(self.show_progress)
        self.refresh_roots()
        self.right.list()
        self.mark_active()

    # -- the old page's names: the sandbox pane --

    @property
    def roots(self):
        return self.left.roots

    @property
    def table(self):
        return self.left.table

    @property
    def root(self):
        return self.left.root

    @property
    def relative(self):
        return self.left.relative

    @property
    def entries(self):
        return self.left.entries

    def go(self, relative, then=None):
        return self.left.go(relative, then)

    def up(self):
        self.left.up()

    def list(self, then=None):
        return self.left.list(then)

    def open_row(self, row, column=0):
        self.left.open_row(row, column)

    def entry_at(self, row):
        return self.left.entry_at(row)

    def chosen(self):
        return self.left.chosen()

    def path_of(self, entry):
        return self.left.path_of(entry)

    def choose_root(self):
        self.left.choose_root()

    def add_other(self):
        self.left.add_other()

    def showEvent(self, event):  # noqa: N802
        """Shown with its real width at last: the panes half and half, unless their division was moved or kept."""
        super().showEvent(event)
        if not self.keep_split:
            QTimer.singleShot(0, self, self.halve)  # once the layout gave the splitter its width

    def halve(self):
        total = sum(self.split.sizes())
        if not self.keep_split and total > 0:
            self.split.setSizes([total - total // 2, total // 2])

    def refresh_roots(self, select=None, then=None):
        self.left.refresh_roots(select, then)

    @property
    def roots_reading(self):
        """The sandbox side's folders are being read (in the background)."""
        return getattr(self.left, "roots_reading", False)

    def set_project(self, path):
        """Show the current project's folder."""
        self.refresh_roots(select=os.path.realpath(path))

    # -- the panes --

    def other(self, pane):
        return self.right if pane is self.left else self.left

    def sandbox_folders(self):
        return [path for path in (self.left.roots.itemData(i) for i in range(self.left.roots.count())) if path]

    def below_sandbox(self, path):
        return bool(path) and any(below(path, folder) for folder in self.sandbox_folders())

    def remote_hosts(self):
        """The names of the other hosts (`sandbox-ai hosts`), as kept on this machine (no SSH)."""
        try:
            return sorted(self.sai.host_statuses(cached=True) or {})
        except Exception:  # noqa: BLE001 - an older sandbox-ai, no hosts: none shown
            return []

    def mark_active(self):
        for pane in (self.left, self.right):
            font = pane.title.font()
            font.setBold(pane is self.active)
            pane.title.setFont(font)
        set_enabled(self.buttons["own"], self.active.side == "sandbox",
                    tr("Taking ownership works only below a sandbox's folder."))

    def activate(self, pane):
        self.active = pane
        self.mark_active()

    def eventFilter(self, watched, event):  # noqa: N802
        # Events reach the filter while the panes are still being built: the panes are looked up, not assumed.
        pane = next((pane for pane in (getattr(self, "left", None), getattr(self, "right", None))
                     if pane is not None and watched is pane.table), None)
        if pane is None:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.FocusIn:
            self.activate(pane)
        elif event.type() == QEvent.Type.ShortcutOverride and self.is_key(event.key(), event.modifiers()):
            event.accept()  # the list's keys (Ctrl+C copies files here): before any shortcut of the window
            return True
        elif event.type() == QEvent.Type.KeyPress and self.key(pane, event.key(), event.modifiers()):
            return True
        return False

    def is_key(self, key, modifiers):
        """Whether KEY with MODIFIERS means something in a pane's list (see key())."""
        Key, plain = Qt.Key, modifiers & ~Qt.KeyboardModifier.KeypadModifier
        control = bool(plain & Qt.KeyboardModifier.ControlModifier)
        if control:
            return key in (Key.Key_A, Key.Key_C, Key.Key_R, Key.Key_V, Key.Key_X)
        if key == Key.Key_F10:
            return bool(plain & Qt.KeyboardModifier.ShiftModifier)  # (F10 alone: the window's)
        return key in (Key.Key_F2, Key.Key_F3, Key.Key_F4, Key.Key_F5, Key.Key_F6, Key.Key_F7, Key.Key_F8,
                       Key.Key_Delete, Key.Key_Backspace, Key.Key_Return, Key.Key_Enter)

    def key(self, pane, key, modifiers=Qt.KeyboardModifier.NoModifier):
        """Do what KEY means in PANE's list; True if it meant something."""
        Key = Qt.Key
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        self.activate(pane)
        changing = (Key.Key_F2, Key.Key_F5, Key.Key_F6, Key.Key_F7, Key.Key_F8, Key.Key_Delete)
        if self.task.running and (key in changing or (control and key == Key.Key_V)):
            # Its buttons are greyed out meanwhile: so are its keys (a rename or a new folder in the middle of a copy
            # or move would change what it works on).
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
            return True
        entries = (Key.Key_Return, Key.Key_Enter, Key.Key_F2, Key.Key_F3, Key.Key_F4, Key.Key_F5, Key.Key_F6,
                   Key.Key_F8, Key.Key_Delete)
        if (key in entries or (control and key in (Key.Key_C, Key.Key_X))) and pane.waiting():
            return True  # the rows are the folder's before (the new one's listing is on its way)
        if key in (Key.Key_Tab, Key.Key_Backtab) and not control:
            self.other(pane).table.setFocus(Qt.FocusReason.TabFocusReason)
            self.activate(self.other(pane))
        elif key in (Key.Key_Return, Key.Key_Enter) and alt:
            self.properties(pane)
        elif key in (Key.Key_Return, Key.Key_Enter):
            pane.open_row(pane.table.currentRow())
        elif key == Key.Key_Backspace:
            pane.up()
        elif key == Key.Key_F3:
            self.open_current(pane, read_only=True)
        elif key == Key.Key_F4:
            self.open_current(pane)
        elif key == Key.Key_F5:
            self.copy_selected(pane)
        elif key == Key.Key_F6:
            self.copy_selected(pane, move=True)
        elif key == Key.Key_F7:
            self.mkdir_dialog(pane)
        elif key in (Key.Key_F8, Key.Key_Delete):
            self.delete_selected(pane)
        elif key == Key.Key_F2:
            self.rename_dialog(pane)
        elif key == Key.Key_F10 and shift:
            row = pane.table.currentRow()
            rect = pane.table.visualRect(pane.table.model().index(row, 0)) if row >= 0 else None
            self.context_menu(pane, rect.center() if rect is not None else QPoint(8, 8))
        elif key == Key.Key_A and control:
            pane.table.selectAll()
        elif key == Key.Key_R and control:
            pane.list()
        elif key == Key.Key_C and control:
            self.copy_items(pane)
        elif key == Key.Key_X and control:
            self.copy_items(pane, cut=True)
        elif key == Key.Key_V and control:
            self.paste(pane)
        else:
            return False
        return True

    def refresh_panes(self, pane=None, select=()):
        """List both panes again; SELECT (names) in PANE once its listing is shown."""
        for side in (self.left, self.right):
            side.list(then=(lambda _listed, side=side: side.select(select)) if side is pane and select else None)

    # -- background work --

    def running(self, busy):
        for widget in self.buttons.values():
            widget.setEnabled(not busy)
        if not busy:
            self.timer.stop()
            self.progress = None
            self.mark_active()

    def follow(self, started, progress, text):
        """Show PROGRESS of the job that STARTED with TEXT in the task's line."""
        if started:
            self.progress, self.progress_text = progress, text
            self.timer.start()
        else:
            self.message.setText(tr("Another copy still runs."))
        return started

    def show_progress(self):
        progress = self.progress
        if progress is None or not self.task.running or self.task.cancel_event.is_set():
            return
        self.task.label.setText(tr("{text} {count} file(s) and folder(s), {size}", text=self.progress_text,
                                   count=progress.count, size=files.size_text(progress.bytes)))

    # -- copy and move --

    def copy_selected(self, pane=None, move=False):
        """Copy (MOVE: move) what is selected in PANE (the active one) into the other pane's folder."""
        source = pane or self.active
        entries = source.chosen()
        if not entries:
            source.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        return self.transfer(source, entries, self.other(source), move)

    def transfer(self, source, entries, target, move=False):
        """Copy (MOVE: move) ENTRIES of the SOURCE pane's folder into the TARGET pane's folder, in a thread."""
        if source.root is None or target.root is None:
            self.message.setText(tr("Choose a folder in both panes first."))
            return False
        clip = Clip(source.root, [(entry.name, source.path_of(entry), None) for entry in entries], source.untrusted(),
                    source.path_text())
        return self.transfer_items(clip, target, target.relative, move)

    def transfer_items(self, clip, target, target_dir, move=False):
        """Copy (MOVE: move) the items of CLIP into the folder TARGET_DIR of the TARGET pane, in a thread: through
        files.copy_entry and files.move_entry (links not followed, budgets, Cancel; replacing asks first)."""
        if target.root is None:
            self.message.setText(tr("Choose a folder in both panes first."))
            return False
        src_root, dst_root, untrusted, items = clip.root, target.root, clip.untrusted, list(clip.items)
        refused = list(clip.problems)  # (paths refused before: named in the report too)
        target_text = target.path_at(target_dir)
        target_untrusted = target.untrusted_at(target_dir)  # (before the question, as the rest)
        out = untrusted and not target_untrusted
        overwrite = self.ask_replace([new or name for name, _, new in items], clip.folder_text, dst_root, target_dir,
                                     target_text)
        if overwrite is None:
            return False
        progress = files.Progress()

        def work(cancelled):
            done, problems = [], list(refused)
            for name, path, new in items:
                try:
                    if move:
                        done.append(files.move_entry(src_root, path, dst_root, target_dir, overwrite=overwrite,
                                                     untrusted=untrusted, target_untrusted=target_untrusted,
                                                     cancelled=cancelled, progress=progress))
                    else:
                        done.append(files.copy_entry(src_root, path, dst_root, target_dir, overwrite=overwrite,
                                                     untrusted=untrusted, name=new, cancelled=cancelled,
                                                     progress=progress))
                except files.Cancelled as exc:
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems
        text = (tr("Moving {count} item(s) to {target}…", count=len(items), target=target_text) if move
                else tr("Copying {count} item(s) to {target}…", count=len(items), target=target_text))
        started = self.task.thread(work, lambda result, error: self.transferred(result, error, out, move), text,
                                   cancellable=True)
        return self.follow(started, progress, text)

    def ask_replace(self, names, source_text, target_root, target_dir, target_text):
        """True: replace what exists (asked first), False: leave it (those copies are refused), None: stop."""
        if not self.replace.isChecked():
            return False
        try:
            existing = files.exists_in(target_root, target_dir, names)
        except (OSError, ValueError) as exc:
            self.message.setText(printable(exc))
            return None
        if not existing:
            return False
        if ask_first(self, "files-replace", tr("Replace existing files?"),
                     tr("{count} of the {total} item(s) from {source} already exist in {target}. Replace them? Files "
                        "are replaced, folders are filled with what is copied.", count=len(existing), total=len(names),
                        source=printable(source_text), target=printable(target_text)),
                     tr("Replace"), details="\n".join(printable(name) for name in existing)):
            return True
        return None

    def transferred(self, result, error, out, move):
        done, problems = result if error is None else ([], [str(error)])
        self.refresh_panes()
        self.report(done, problems, copied_out=out, moved=move)

    def report(self, done, problems, copied_out=False, moved=False):
        lines = []
        if done:
            values = dict(files=sum(r.files for r in done), folders=sum(r.directories for r in done),
                          size=files.size_text(sum(r.bytes for r in done)))
            lines.append(tr("{files} file(s) in {folders} folder(s), {size} moved", **values) if moved else
                         tr("{files} file(s) in {folders} folder(s), {size} copied", **values))
        skipped = [p for r in done for p in r.skipped]
        if skipped:
            lines.append(tr("skipped: {list}", list=", ".join(f"{printable(p)} ({printable(why)})"
                                                               for p, why in skipped[:10])))
        lines += [printable(r.kept) for r in done if r.kept]
        lines += [printable(problem) for problem in problems]
        risky = [path for r in done for path in r.risky] if copied_out else []
        executable = [path for r in done for path in r.executable] if copied_out else []
        if executable:
            lines.append(tr("Copied without the executable bit (make a file executable yourself once you trust it): "
                            "{list}", list=", ".join(printable(path) for path in executable[:20])
                            + (" …" if len(executable) > 20 else "")))
        if risky:
            listed = ", ".join(printable(path) for path in risky[:20]) + (" …" if len(risky) > 20 else "")
            warning = tr("Careful: the agent could have written these files, and tools run them or read them as "
                         "configuration when you use the copy on the host (git, direnv, shells, editors, make): "
                         "{list}. Look at them before you use the copied folder. Copied files are never "
                         "executable on the host.", list=listed)
            lines.append(warning)
            warn(self, tr("Files that run or configure tools"), warning)
        self.message.setText("\n".join(lines))
        self.last_report = lines
        return lines

    # -- the old page's copies: the sandbox pane and a host folder chosen in a dialog --

    def host(self):
        return self.right.root

    def download(self, entries, destination):
        """Copy ENTRIES (of the sandbox pane's folder) into the host folder DESTINATION, in a thread."""
        destination = os.path.realpath(os.fspath(destination))
        root, host, items = self.left.root, self.host(), [(entry.name, self.left.path_of(entry)) for entry in entries]
        target = host_relative(destination)
        if root is None:
            return False
        overwrite = self.ask_replace([name for name, _ in items], self.left.path_text(), host, target, destination)
        if overwrite is None:
            return False
        progress = files.Progress()

        def work(cancelled):
            done, problems = [], []
            for name, path in items:
                try:
                    done.append(files.copy_entry(root, path, host, target, overwrite=overwrite, untrusted=True,
                                                 cancelled=cancelled, progress=progress))
                except files.Cancelled as exc:
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems
        text = tr("Copying to the host…")
        started = self.task.thread(work, lambda result, error: self.transferred(result, error, True, False), text,
                                   cancellable=True)
        return self.follow(started, progress, text)

    def upload(self, paths):
        """Copy the host files or folders PATHS into the sandbox pane's folder, in a thread. A link given as a
        path is refused (its folder is resolved, the link itself is not)."""
        if self.left.root is None:
            return False
        return self.copy_paths(paths, self.left, self.left.relative)

    def host_items(self, paths):
        """(Clip of the host's Root("/"), problems) for the host PATHS (a drop, the clipboard of another program, a
        file dialog): each one's folder resolved, the entry itself not (a link is refused when it is copied). A path
        that goes through a symbolic link in a sandbox's folder is refused, as in the path bar; one in a sandbox's
        folder is copied as the agent's (never executable on the host)."""
        items, problems, untrusted = [], [], False
        for path in paths:
            path = os.path.normpath(os.path.abspath(os.fspath(path)))
            name, folder = os.path.basename(path), os.path.dirname(path)
            real = os.path.realpath(folder)
            if not name or path == "/":
                problems.append(tr("Choose a file or folder to copy."))
                continue
            if real != folder and self.below_sandbox(folder):
                problems.append(tr("{path} goes through a symbolic link in a sandbox's folder; it is not followed.",
                                   path=printable(path)))
                continue
            whole = os.path.join(real, name)
            untrusted = untrusted or self.below_sandbox(whole)
            items.append((name, host_relative(whole), None))
        root = self.right.root if self.right.root is not None else files.Root("/", sandbox=False)
        clip = Clip(root, items, untrusted, tr("this machine"))
        clip.problems = problems
        return clip, problems

    def copy_paths(self, paths, target, target_dir):
        """Copy the host PATHS into the folder TARGET_DIR of the TARGET pane, in a thread (host_items)."""
        clip, problems = self.host_items(paths)
        if problems:
            self.message.setText("\n".join(problems))
        if not clip.items:
            return False
        return self.transfer_items(clip, target, target_dir)

    def download_dialog(self):
        entries = self.left.chosen()
        if not entries or self.left.root is None:
            self.left.waiting() or self.message.setText(tr("Select files or folders first."))
            return
        destination = choose_folder(self, tr("Copy to which folder on the host?"), "copy-out")
        if destination:
            self.download(entries, destination)

    def upload_dialog(self):
        if self.left.root is None:
            return
        paths = choose_files(self, tr("Files to copy into {path}", path=self.left.path_text()), "copy-in")
        if paths:
            self.upload(paths)

    # -- opening: the editor, the properties --

    def open_current(self, pane, mode="auto", read_only=False):
        """F3 (READ_ONLY) and F4: the current file in the editor."""
        entry = pane.current()
        if entry is None:
            pane.waiting() or self.message.setText(tr("Select a file first."))
            return False
        if entry.kind == "dir":
            self.message.setText(tr("{name} is a folder.", name=printable(entry.name)))
            return False
        pane.open_entry(entry, mode, read_only)
        return True

    def open_editor(self, root, relative, shown, mode="auto", read_only=False):
        """The file RELATIVE of ROOT (SHOWN: its path) in an editor window of its own (file_editor.py)."""
        from .file_editor import EditorWindow
        window = EditorWindow(self, root, relative, shown, mode, read_only, saved=self.refresh_panes)
        self.keep(self.editors, window)
        window.show()
        return window

    @staticmethod
    def keep(windows, window):
        windows.append(window)
        window.destroyed.connect(lambda *_: window in windows and windows.remove(window))

    def properties(self, pane, entries=None):
        """The Properties dialog of ENTRIES (default: those selected; none: the folder shown) of PANE."""
        from .file_properties import PropertiesDialog
        if pane.root is None:
            return None
        entries = pane.chosen() if entries is None else entries
        items = [(entry.name, pane.path_of(entry)) for entry in entries] or [("", pane.relative)]
        relative, text, untrusted, sandbox = pane.relative, pane.path_text(), pane.untrusted(), pane.side == "sandbox"
        dialog = PropertiesDialog(self, pane.root, items, text, untrusted, changed=self.refresh_panes,
                                  owner=lambda root, shown, folders: self.ask_owner(Chosen(
                                      root, shown, relative, text, untrusted, sandbox, folders)))
        self.keep(self.dialogs, dialog)
        dialog.show()
        return dialog

    def copy_path(self, pane, entries=None):
        """The paths of ENTRIES (none: of the folder shown) as text on the clipboard; control characters in a name are
        shown escaped (a pasted path never carries a line break or an escape sequence into a terminal)."""
        entries = pane.chosen() if entries is None else entries
        paths = [pane.path_of_entry(entry) for entry in entries] or [pane.path_text()]
        text = "\n".join(printable(path) for path in paths)
        QGuiApplication.clipboard().setText(text)
        self.message.setText(tr("Copied to the clipboard: {paths}", paths=text if len(paths) == 1 else
                                tr("{count} paths", count=len(paths))))
        return text

    # -- the context menu --

    def context_menu(self, pane, point):
        """The menu of a right click at POINT of PANE's list: for the rows selected, or for the folder shown when the
        click is on no row (or on "..")."""
        self.activate(pane)
        menu = self.menu(pane, at_row=pane.table.indexAt(point).isValid() and not pane.is_up(
            pane.table.indexAt(point).row()))
        self.exec_menu(menu, pane.table.viewport().mapToGlobal(point))
        return menu

    def exec_menu(self, menu, where):
        menu.exec(where)
        menu.deleteLater()

    def menu(self, pane, at_row=True):
        """The context menu of PANE (AT_ROW: for its selection, else for the folder shown)."""
        menu = QMenu(self)
        menu.setAccessibleName(tr("File actions"))
        entries = pane.chosen() if at_row else []
        one = entries[0] if len(entries) == 1 else None
        busy = self.task.running
        has_root = pane.root is not None

        def add(text, slot, shortcut=None, enabled=True, tip=None):
            action = menu.addAction(text)
            if shortcut:
                action.setShortcut(shortcut)
                action.setShortcutVisibleInContextMenu(True)
            action.setEnabled(bool(enabled))
            if tip:
                action.setToolTip(tip)
            action.triggered.connect(lambda *_: slot())
            return action
        menu.setToolTipsVisible(True)
        if entries:
            file_like = one is not None and one.kind in ("file", "link")
            add(tr("Open"), lambda: pane.open_entry(one), "Return", one is not None)
            add(tr("View"), lambda: pane.open_entry(one, read_only=True), "F3", file_like)
            add(tr("Edit"), lambda: pane.open_entry(one, "text"), "F4", file_like)
            add(tr("Open with the hex editor"), lambda: pane.open_entry(one, "hex"), None, file_like)
            menu.addSeparator()
            add(tr("Copy"), lambda: self.copy_items(pane, entries), "Ctrl+C")
            add(tr("Cut"), lambda: self.copy_items(pane, entries, cut=True), "Ctrl+X")
        folder = one if one is not None and one.kind == "dir" else None
        add(tr("Paste into {name}", name=printable(folder.name)) if folder else tr("Paste"),
            lambda: self.paste(pane, pane.path_of(folder) if folder else None), "Ctrl+V",
            has_root and not busy and self.can_paste())
        if entries:
            other = self.other(pane)
            add(tr("Copy to the other pane"), lambda: self.transfer(pane, entries, other), "F5",
                not busy and other.root is not None)
            add(tr("Move to the other pane"), lambda: self.transfer(pane, entries, other, move=True), "F6",
                not busy and other.root is not None)
            menu.addSeparator()
            add(tr("Rename…"), lambda: self.rename_dialog(pane, one), "F2", one is not None and not busy)
            add(tr("Delete…"), lambda: self.delete_selected(pane, entries), "Del", not busy)
        menu.addSeparator()
        add(tr("New folder…"), lambda: self.mkdir_dialog(pane), "F7", has_root and not busy)
        add(tr("Copy path"), lambda: self.copy_path(pane, entries), None, has_root)
        if not entries:
            add(tr("Refresh"), lambda: pane.list(), "Ctrl+R", has_root)
        add(tr("Properties…"), lambda: self.properties(pane, entries), "Alt+Return", has_root,
            None if entries else tr("Of the folder shown"))
        return menu

    # -- the clipboard: Ctrl+C, Ctrl+X, Ctrl+V --

    def copy_items(self, pane, entries=None, cut=False):
        """Copy (CUT: cut) ENTRIES (default: those selected) of PANE: Paste copies (moves) them into a folder of
        either pane. The system clipboard gets their paths as text, and as files where another program may take them
        (export_urls)."""
        entries = pane.chosen() if entries is None else entries
        if not entries or pane.root is None:
            pane.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        clip = Clip(pane.root, [(entry.name, pane.path_of(entry), None) for entry in entries], pane.untrusted(),
                    pane.path_text(), cut)
        self.clipboard = clip
        mime = QMimeData()
        mime.setData(MIME, clip.token.encode("ascii"))
        mime.setText("\n".join(printable(pane.path_of_entry(entry)) for entry in entries))
        urls = self.export_urls(pane, entries)
        if urls:
            mime.setUrls(urls)
        QGuiApplication.clipboard().setMimeData(mime)
        self.message.setText(tr("{count} item(s) cut: Paste (Ctrl+V) moves them into the folder shown.",
                                count=len(entries)) if cut else
                             tr("{count} item(s) copied: Paste (Ctrl+V) copies them into the folder shown.",
                                count=len(entries)))
        return True

    def can_paste(self):
        mime = QGuiApplication.clipboard().mimeData()
        return self.clipboard is not None or bool(mime is not None and any(url.isLocalFile() for url in mime.urls()))

    def paste(self, pane, target_dir=None):
        """Paste into the folder TARGET_DIR (default: the one shown) of PANE: what was copied or cut here (a cut is
        moved, once), else the local files another program put on the clipboard (copied)."""
        if pane.root is None:
            return False
        target_dir = pane.relative if target_dir is None else target_dir
        mime = QGuiApplication.clipboard().mimeData()
        clip = self.clipboard
        if clip is not None and (clip.owns(mime) or mime is None or not mime.formats()):
            return self.paste_clip(clip, pane, target_dir)
        paths = [url.toLocalFile() for url in (mime.urls() if mime is not None else []) if url.isLocalFile()]
        if paths:
            return self.copy_paths(paths, pane, target_dir)
        if clip is not None and not (mime.hasText() or mime.hasUrls()):
            return self.paste_clip(clip, pane, target_dir)
        self.message.setText(tr("Nothing to paste: copy or cut files first (Ctrl+C, Ctrl+X)."))
        return False

    def paste_clip(self, clip, pane, target_dir):
        same = clip.folder_text == pane.path_at(target_dir) and (clip.root.path, clip.root.sandbox) == (
            pane.root.path, pane.root.sandbox)
        if same and clip.cut:
            self.message.setText(tr("They are in this folder already."))
            return False
        if same:  # a copy beside the original, under a name of its own
            try:
                taken = set(files.exists_in(pane.root, target_dir, [copy_name(name, n) for name, _, _ in clip.items
                                                                    for n in range(1, 21)]))
            except (OSError, ValueError) as exc:
                self.message.setText(printable(exc))
                return False
            items = []
            for name, path, _ in clip.items:
                new = next((copy_name(name, n) for n in range(1, 21) if copy_name(name, n) not in taken), None)
                if new is None:
                    self.message.setText(tr("{name}: too many copies here already.", name=printable(name)))
                    return False
                items.append((name, path, new))
            clip = Clip(clip.root, items, clip.untrusted, clip.folder_text)
        started = self.transfer_items(clip, pane, target_dir, move=clip.cut)
        if started and clip.cut:
            self.clipboard = None  # moved: a second paste has nothing to move
        return started

    def export_urls(self, pane, entries):
        """The file URLs of ENTRIES that other programs may get (a drag out, the clipboard): never a link or a special
        file; from a sandbox's folder only plain files that are not executable (a folder could hold hooks or
        programs, which another program would copy as they are, executable bits and all)."""
        urls = []
        untrusted = pane.untrusted()
        for entry in entries:
            if entry.kind not in ("file", "dir"):
                continue
            if untrusted and (entry.kind != "file" or entry.mode & (0o111 | files.SPECIAL_BITS)):
                continue
            urls.append(QUrl.fromLocalFile(pane.path_of_entry(entry)))
        return urls

    # -- drag and drop --

    def start_drag(self, pane):
        """Drag what is selected in PANE: to the other pane or onto a folder (the drop copies; with Shift it moves),
        or to another program (export_urls)."""
        entries = pane.chosen()
        if not entries or pane.root is None:
            return None
        clip = Clip(pane.root, [(entry.name, pane.path_of(entry), None) for entry in entries], pane.untrusted(),
                    pane.path_text())
        clip.pane, clip.folder = pane, pane.relative
        mime = QMimeData()
        mime.setData(MIME, clip.token.encode("ascii"))
        urls = self.export_urls(pane, entries)
        if urls:
            mime.setUrls(urls)
        drag = QDrag(pane.table)
        drag.setMimeData(mime)
        drag.setPixmap(self.drag_picture(entries))
        self.drag = clip
        try:
            return self.exec_drag(drag)
        finally:
            self.drag = None
            self.drop_hint(None)

    def exec_drag(self, drag):
        return drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.CopyAction)

    def drag_picture(self, entries):
        """What moves with the mouse: the first name, and how many there are."""
        text = printable(entries[0].name) if len(entries) == 1 else tr("{count} items", count=len(entries))
        metrics = self.fontMetrics()
        text = metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, 320)
        picture = QPixmap(metrics.horizontalAdvance(text) + 16, metrics.height() + 8)
        picture.fill(Qt.GlobalColor.transparent)
        painter = QPainter(picture)
        painter.setBrush(self.palette().color(QPalette.ColorRole.Highlight))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(picture.rect(), 4, 4)
        painter.setPen(QColor(self.palette().color(QPalette.ColorRole.HighlightedText)))
        painter.drawText(picture.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return picture

    def drop_plan(self, pane, mime, point, modifiers):
        """What a drop of MIME at POINT of PANE's list does (DropPlan), or None where it does nothing: into the folder
        of the row at POINT (".." the folder above), else into the folder shown. Items dragged in this window are
        copied, with Shift moved; local files of another program are copied."""
        if pane.root is None or mime is None or pane.changing():
            return None  # (while the rows are another folder's: no row to drop onto)
        index = pane.table.indexAt(point)
        target_dir, row = pane.relative, -1
        if index.isValid():
            entry = pane.entry_at(index.row())
            if pane.is_up(index.row()):
                target_dir, row = "/".join(files.parts(pane.relative)[:-1]), index.row()
            elif entry is not None and entry.kind == "dir":
                target_dir, row = pane.path_of(entry), index.row()
        clip = self.drag if self.drag is not None and self.drag.owns(mime) else None
        if clip is not None:
            if getattr(clip, "pane", None) is pane:
                if target_dir == clip.folder:
                    return None  # where they are
                if any(target_dir == path or target_dir.startswith(path + "/") for _, path, _ in clip.items):
                    return None  # into itself
            move = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            return DropPlan(pane, target_dir, row, Qt.DropAction.MoveAction if move else Qt.DropAction.CopyAction,
                            clip=clip)
        if mime.hasFormat(MIME):
            return None  # another window's, or a drag that has ended
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()] if mime.hasUrls() else []
        if not paths:
            return None
        return DropPlan(pane, target_dir, row, Qt.DropAction.CopyAction, paths=paths)

    def drop_hint(self, plan):
        """While something is dragged over a list: what a drop there does."""
        if plan is not None:
            self.message.setText(plan.text())
        elif self.message.text().startswith(tr("Drop: {rest}", rest="")):
            self.message.setText("")

    def run_drop(self, plan):
        """Do what PLAN says (after the drop has ended)."""
        if plan.pane.root is None:
            return False
        if plan.clip is not None:
            return self.transfer_items(plan.clip, plan.pane, plan.target_dir, move=plan.move)
        return self.copy_paths(plan.paths, plan.pane, plan.target_dir)

    # -- delete --

    def delete_selected(self, pane=None, entries=None):
        pane = pane or self.active
        entries = pane.chosen() if entries is None else entries
        if not entries or pane.root is None:
            pane.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        chosen = Chosen.of(pane, entries)
        if not ask_first(self, "files-delete", tr("Delete?"),
                         tr("Delete {count} item(s) in {folder}? Folders are deleted with everything in them; links "
                            "are removed, not what they point to. This cannot be undone.", count=len(chosen.items),
                            folder=printable(chosen.folder_text)),
                         tr("Delete"), details="\n".join(printable(name) for name, _ in chosen.items)):
            return False
        return self.delete_items(chosen)

    def delete_items(self, chosen):
        """Delete the items of CHOSEN (whatever the pane shows by now), in a thread."""
        root, items = chosen.root, chosen.items
        progress = files.Progress()

        def work(cancelled):
            removed, problems = 0, []
            for name, path in items:
                try:
                    removed += root.delete_entry(path, cancelled=cancelled, progress=progress).removed
                except files.Cancelled as exc:
                    removed += getattr(getattr(exc, "partial", None), "removed", 0)
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    removed += getattr(getattr(exc, "partial", None), "removed", 0)
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return removed, problems
        text = tr("Deleting {count} item(s)…", count=len(items))
        started = self.task.thread(work, self.deleted, text, cancellable=True)
        return self.follow(started, progress, text)

    def deleted(self, result, error):
        removed, problems = result if error is None else (0, [str(error)])
        self.refresh_panes()
        self.last_report = [tr("{count} file(s) and folder(s) deleted.", count=removed),
                            *(printable(problem) for problem in problems)]
        self.message.setText("\n".join(self.last_report))

    # -- rename, new folder, permissions --

    def rename_dialog(self, pane=None, entry=None):
        pane = pane or self.active
        entry = entry or pane.current()
        if entry is None:
            pane.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        chosen = Chosen.of(pane, [entry])
        new, ok = QInputDialog.getText(self, tr("Rename"), shown_as_text(tr("New name for {name}:",
                                                                            name=printable(entry.name))),
                                       QLineEdit.EchoMode.Normal, entry.name)
        return bool(ok and new) and self.rename(pane, entry, new, chosen)

    def rename(self, pane, entry, new_name, chosen=None):
        """ENTRY (of the folder CHOSEN was taken in; default: the one PANE shows) renamed NEW_NAME."""
        chosen = chosen or Chosen.of(pane, [entry])
        try:
            chosen.root.rename_entry(chosen.items[0][1], new_name)
        except (OSError, ValueError) as exc:
            self.message.setText(printable(exc))
            return False
        self.refresh_panes(pane if chosen.shown_in(pane) else None, [new_name])
        self.message.setText(tr("{name} renamed to {new}.", name=printable(entry.name), new=printable(new_name)))
        return True

    def mkdir_dialog(self, pane=None):
        pane = pane or self.active
        if pane.root is None:
            return False
        where = Chosen.of(pane, [])
        name, ok = QInputDialog.getText(self, tr("New folder"), shown_as_text(tr(
            "Name of the new folder in {folder}:", folder=printable(where.folder_text))))
        return bool(ok and name) and self.make_folder(pane, name, where)

    def make_folder(self, pane, name, where=None):
        """A folder NAME in the folder WHERE (a Chosen, taken before the name was asked; default: the one PANE
        shows)."""
        where = where or Chosen.of(pane, [])
        try:
            where.root.make_folder(where.relative, name)
        except (OSError, ValueError) as exc:
            self.message.setText(printable(exc))
            return False
        self.refresh_panes(pane if where.shown_in(pane) else None, [name])
        self.message.setText(tr("Folder {name} created.", name=printable(name)))
        return True

    def chmod_dialog(self, pane=None):
        pane = pane or self.active
        entries = pane.chosen()
        if not entries:
            pane.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        chosen = Chosen.of(pane, entries)
        text, ok = QInputDialog.getText(self, tr("Permissions"),
                                        shown_as_text(tr("Mode (octal, e.g. 644 or 755) for {count} item(s):",
                                                         count=len(entries))),
                                        QLineEdit.EchoMode.Normal, f"{stat.S_IMODE(entries[0].mode):o}")
        if not ok or not text.strip():
            return False
        try:
            mode = int(text.strip(), 8)
        except ValueError:
            self.message.setText(tr("Not a permission mode: {mode}", mode=printable(text)))
            return False
        return self.chmod_items(chosen, mode)

    def chmod(self, pane, entries, mode):
        return self.chmod_items(Chosen.of(pane, entries), mode)

    def chmod_items(self, chosen, mode):
        """MODE for the items of CHOSEN (no set-ID or sticky bits where an agent may have written)."""
        changed, problems = 0, []
        for name, path in chosen.items:
            try:
                chosen.root.chmod_entry(path, mode, special=not chosen.untrusted)
                changed += 1
            except (OSError, ValueError) as exc:
                problems.append(f"{printable(name)}: {printable(exc)}")
        self.refresh_panes()
        self.last_report = [tr("Permissions of {count} item(s) changed.", count=changed)] + problems
        self.message.setText("\n".join(self.last_report))
        return changed == len(chosen.items)

    # -- owners --

    def chown_dialog(self, pane=None, entries=None):
        pane = pane or self.active
        entries = pane.chosen() if entries is None else entries
        if not entries or pane.root is None:
            pane.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        return self.ask_owner(Chosen.of(pane, entries))

    def ask_owner(self, chosen):
        """The owner and group for the items of CHOSEN (taken before the dialog opened), then chown_items."""
        dialog = OwnerDialog(self, len(chosen.items), chosen.folders)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        uid, gid, recursive = dialog.values()
        if uid == -1 and gid == -1:
            return False
        return self.chown_items(chosen, uid, gid, recursive)

    def chown(self, pane, entries, uid=-1, gid=-1, recursive=False):
        """Give ENTRIES of PANE to UID and GID (yours, -1: unchanged), in a thread."""
        return self.chown_items(Chosen.of(pane, entries), uid, gid, recursive)

    def chown_items(self, chosen, uid=-1, gid=-1, recursive=False):
        root, items = chosen.root, chosen.items
        progress, sandbox = files.Progress(), chosen.sandbox

        def work(cancelled):
            done, problems = [], []
            for name, path in items:
                try:
                    done.append(root.chown_entry(path, uid, gid, recursive=recursive, cancelled=cancelled,
                                                 progress=progress))
                except files.Cancelled as exc:
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems
        text = tr("Changing the owner of {count} item(s)…", count=len(items))
        started = self.task.thread(work, lambda result, error: self.owned(result, error, sandbox), text,
                                   cancellable=True)
        return self.follow(started, progress, text)

    def owned(self, result, error, sandbox):
        done, problems = result if error is None else ([], [str(error)])
        self.refresh_panes()
        lines = [tr("{count} file(s) and folder(s) changed.", count=sum(r.files for r in done))]
        skipped = [p for r in done for p in r.skipped]
        if skipped:
            lines.append(tr("not changed: {list}", list=", ".join(f"{printable(p)} ({printable(why)})"
                                                                   for p, why in skipped[:10])))
            if sandbox:
                lines.append(tr("What a rootless Podman sandbox wrote as another user can be taken back with "
                                "“Take ownership”."))
        lines += [printable(problem) for problem in problems]
        self.last_report = lines
        self.message.setText("\n".join(lines))

    def own_selected(self, pane=None):
        """Make you the owner of what is selected in the sandbox pane, also of what the sandbox wrote as one of
        your subordinate UIDs (files.take_ownership: podman unshare chown), in a thread."""
        pane = pane or self.active
        if pane.side != "sandbox" or pane.root is None:
            self.message.setText(tr("Taking ownership works only below a sandbox's folder."))
            return False
        entries = pane.chosen()
        if not entries:
            pane.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        chosen = Chosen.of(pane, entries)
        if not confirm(self, tr("Take ownership"),
                       tr("Make you the owner of {count} item(s) in {folder} and of everything in them? What the "
                          "sandbox wrote as another user (one of your subordinate UIDs) becomes yours again "
                          "(podman unshare chown); links are changed themselves, never followed.",
                          count=len(chosen.items), folder=printable(chosen.folder_text)),
                       tr("Take ownership"), details="\n".join(printable(name) for name, _ in chosen.items)):
            return False
        root, items = chosen.root, chosen.items

        def work(cancelled):
            done, problems = [], []
            for name, path in items:
                try:
                    done.append(files.take_ownership(root, path, cancelled=cancelled))
                except files.Cancelled as exc:
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems
        text = tr("Taking ownership of {count} item(s)…", count=len(items))
        started = self.task.thread(work, lambda result, error: self.owned(result, error, False), text,
                                   cancellable=True)
        return self.follow(started, None, text)
