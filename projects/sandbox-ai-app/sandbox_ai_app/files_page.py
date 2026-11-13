"""The Files page: a two-pane file manager. Left: the workspace or HOME of a sandbox or session (or another
folder); right: this machine, starting at HOME. Files go back and forth between the panes (copy, move) and
are deleted, renamed, given new folders, permissions and owners in either.

Everything goes through files.py: relative to open directory descriptors, never through a symbolic link (one
in the middle of a path or as the entry itself), never above the root, within budgets, cancellable, and a copy
cut short leaves nothing behind. Below a sandbox's folder - and in a folder of the right pane that lies in one, by its
path or as that folder itself reached under another path (a bind mount of it: holding_folder) - an agent may have
written everything, so what is copied out of it is never executable, files that tools run are
named, links are not followed even to open a folder, and no set-ID or sticky bit is set. A sandbox's folder that is an
entry of a host folder, or lies inside the folder copied, is copied the same way where it goes out (files.copy_entry's
agents), and never handed to other programs by a drag or the clipboard. Replacing and deleting
ask first. Copies, moves, deletes and owner changes run in a thread with progress and Cancel.

Every folder of both panes begins with "." and "..", whatever the list is sorted by. "." is the folder shown: a double
click or Enter lists it again, a drop on it or a paste from its context menu goes into it, Properties shows the folder
itself. ".." is the folder above (double click, Enter or Backspace); at the top of a pane (a sandbox's folder, or "/")
it is greyed out and its tooltip says why - nothing above the top is ever shown. Neither can be selected (FileRows), so
no copy, move, delete, rename or drag ever takes one, and Ctrl+A selects every entry but them. Both panes list folders
first, then by name as the locale collates names (name_order: letter case ignored, "é" beside "e", NFC and NFD alike) -
and so does a click on Name, either way (folders first in both directions); a sort chosen stays when the folder is
listed again.

Recent (the arrow at the end of each path bar, Alt+Down in it) lists the folders the pane showed lately (the latest
first, each once, at most RECENT): the host pane's are kept in the app's state across restarts, a sandbox pane's only
while its folder is in the chooser. Choosing one goes there through the path bar's own checks (go_path). Below each
list, the free space of the folder's file system (Root.free_space: os.fstatvfs of the open folder), read with every
listing - so after a change of folder and after every copy, move or delete.

A file opens in the app's own editor (file_editor.py: text, or a hex editor for other bytes; a Markdown file
rendered, markdown_view.py) with a double click or Enter; it is read and written through the pane's Root like
everything else and never run. The context menu (a right click) and the keys offer Open, View, Edit, the hex editor,
View as Markdown, Copy, Cut, Paste (an internal clipboard of the items, their
paths as text on the system clipboard too), copy or move to the other pane, Rename, Delete, New folder, New file (an
empty file, Root.make_file, then opened in the editor), Copy path, Copy relative path (Ctrl+Shift+C) and Copy path in
the sandbox (path_forms: relative to the folder of the running sandbox or session that holds it, and as its agent
sees it - backend.sandbox_paths, /work, /workspaces/NAME, /root; one path a line, also quoted for the shell) and
Properties (file_properties.py).

SSH… shows a remote system in the sandbox pane (remote.RemoteRoot, a Root like the others: the same code path for
listing, copies both ways, the editor and Properties): connect_remote in the background, an unknown host's keys trusted
only after a yes (trust_host), a locked key with a terminal offered; its root is the remote "/", the pane starts in the
remote HOME, its links are named and never followed, its owners shown as numbers.

Drag and drop: between the panes and onto a folder of the same pane a drop copies, with Shift it moves - through the
same copy and move as F5 and F6 (asking before anything is replaced). Files dropped from other programs (local files
only) are copied in - into an agent's folder, or to a remote system that is one of sandbox-ai's hosts, only after a
question that names them; a path that goes through a symbolic link in a sandbox's folder is refused, as in the path bar.
Dragged out to other programs go the host pane's files and folders; from a sandbox's folder only plain files that are
not executable (a program dropped on would get folders with hooks or executables as they are, executable bits and all).

Keys (in a list): Tab the other pane, Enter open, Backspace up, F3 view, F4 edit, Shift+F4 new file (empty, then
edited), F5 copy, F6 move, F7 new folder, F8 or Delete delete, F2 rename, Ctrl+C copy, Ctrl+X cut, Ctrl+V paste,
Alt+Enter properties, Ctrl+A select all, Ctrl+Shift+C copy relative paths, Ctrl+R refresh, the menu key (or Shift+F10)
the context menu.
"""
from __future__ import annotations

import grp
import os
import posixpath
import pwd
import secrets
import shlex
import stat
import sys
import threading
import unicodedata

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, QMimeData, QPoint, QSize, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QDrag, QGuiApplication, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMenu, QPushButton, QSplitter, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from . import files, state as app_state
from .files import printable
from .i18n import N_, language, tr
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
# The first two rows of every folder (never the name of an entry, never selected): the folder shown and the one above.
HERE, UP = ".", ".."
# What a drag between the panes and the page's clipboard carry besides the paths: a token of this app's own (what was
# dragged or copied is kept in the page, never read back from the data, which another program could forge).
MIME = "application/x-sandbox-ai-app-files"
# The widths the columns start with (fitted to rows of this kind before the first folder is shown): a folder with
# longer texts makes a column wider, never narrower again, so the columns do not jump from folder to folder.
SEED_ROW = ("a-file-name-of-some-length.txt", "", "1023.9 KiB", "2026-12-31 23:59", "→ ../a/link/target",
            "username:groupname", "drwxrwxrwx 0777")
RECENT = 15  # folders a pane's Recent menu offers
RECENT_KEY = "files-recent-host"  # the host pane's recent folders in the app's state (state.json)
# macOS's file systems ignore the case of names (APFS also their Unicode normalisation; HFS+ stores NFD): /users/me/ws
# is the folder /Users/me/ws there. Whether a folder lies in a sandbox's is decided with names folded so (in the safe
# direction: on a volume that tells cases apart, a folder of another case counts as the agent's too).
FOLDED_PATHS = sys.platform == "darwin"


def root_title(kind, label, path):
    template = ROOT_KINDS.get(kind, "Chosen folder")
    return f"{tr(template, name=printable(label))}  ({printable(path)})"


def host_relative(path):
    """PATH (absolute, without links) relative to the host's Root("/")."""
    return os.path.normpath(path).lstrip("/")


def below(path, folder, folded=False):
    """Whether PATH is FOLDER or lies in it (FOLDED: compared as FOLDED_PATHS says the file system compares names)."""
    if folded and FOLDED_PATHS:
        path, folder = (unicodedata.normalize("NFC", text).casefold() for text in (path, folder))
    folder = folder.rstrip("/") or "/"
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def folder_identity(path):
    """(st_dev, st_ino) of the folder PATH itself (never through a link), or None."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None


def folder_identities(folders):
    """{(st_dev, st_ino): folder} of FOLDERS (the sandboxes' folders that are there)."""
    found = {}
    for folder in folders:
        if not str(folder).startswith("/"):  # (a remote system's key in the chooser, ssh://...: no folder here)
            continue
        identity = folder_identity(folder)
        if identity is not None:
            found.setdefault(identity, folder)
    return found


def holding_folder(path, folders, identities=None):
    """Where PATH (absolute) lies in one of FOLDERS: the outermost of them that holds it by name (below(), folded), else
    the outermost folder on PATH's way that is one of them itself (IDENTITIES: folder_identities(FOLDERS)) - reached
    under another path: a bind mount of a sandbox's folder (mount --bind ~/ws /mnt/ws), macOS's firmlinks
    (/System/Volumes/Data/Users/...). By name alone, /mnt/ws was the host's own: the agent's files kept their
    executable bits when copied from there, and its links were followed. The path of that folder on PATH's way, or
    None."""
    by_name = [folder for folder in folders if below(path, folder, folded=True)]
    if by_name:
        return min(by_name, key=lambda folder: len([part for part in folder.split("/") if part]))
    if not identities:
        return None
    current = "/"
    for part in [part for part in path.split("/") if part]:
        current = posixpath.join(current, part)
        identity = folder_identity(current)
        if identity is None:
            return None  # (not there, or a link: nothing below it is reached through it here)
        if identity in identities:
            return current
    return None


def relative_to(path, folder):
    """PATH relative to the FOLDER that holds it (below(path, folder, folded=True)), by their components: on macOS the
    two may differ in letter case (/users/me/ws typed, /Users/me/ws known), and os.path.relpath made that
    "../../../users/me/ws/a"."""
    inner, outer = [part for part in path.split("/") if part], [part for part in folder.split("/") if part]
    return "/".join(inner[len(outer):]) or "."


def shell_quoted(path):
    """PATH quoted for a shell (Copy quoted for the shell): shlex.quote; a path with control characters (or bytes that
    are no UTF-8) as $'...' with those as \\x escapes - it names the file exactly, and no line break or escape
    sequence reaches the terminal it is pasted into (quoted as shown, escaped, it named another file)."""
    if path.isprintable():
        return shlex.quote(path)
    quoted = []
    for character in path:
        code = ord(character)
        if 0xDC80 <= code <= 0xDCFF:  # (a byte that is no UTF-8: os.fsdecode's surrogate)
            quoted.append(f"\\x{code - 0xDC00:02x}")
        elif not character.isprintable():
            quoted.extend(f"\\x{byte:02x}" for byte in character.encode("utf-8", "surrogatepass"))
        elif character in "\\'":
            quoted.append("\\" + character)
        else:
            quoted.append(character)
    return "$'" + "".join(quoted) + "'"


def file_url(path):
    """The file URL of the local PATH with its bytes as they are (percent-encoded): QUrl.fromLocalFile takes a QString,
    which drops what is no UTF-8 (os.fsdecode's surrogates) - the URL of a\\xffb named ab, another file."""
    from urllib.parse import quote
    return QUrl.fromEncoded(b"file://" + quote(os.fsencode(path), safe="/").encode("ascii"))


def local_path(url):
    """The local path a file URL names, its bytes as they are (see file_url: url.toLocalFile() dropped what is no
    UTF-8), or None for another URL - and for one of another host (file://host/path: Qt's toLocalFile made it
    //host/path, which named the local /host/path). file://localhost/... is a local path."""
    from urllib.parse import unquote_to_bytes
    if not url.isLocalFile() or url.host() not in ("", "localhost"):
        return None
    return os.fsdecode(unquote_to_bytes(url.path(QUrl.ComponentFormattingOption.FullyEncoded)))


def same_root(one, other):
    """Whether the Roots ONE and OTHER are the same folder: a remote system's only itself (its "/" is not this
    machine's, nor another remote system's), this machine's by path and kind."""
    if getattr(one, "remote", False) or getattr(other, "remote", False):
        return one is other
    return (one.path, one.sandbox) == (other.path, other.sandbox)


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


def entry_rows(entries, ranges, untrusted, numeric=False):
    """(rows, colours) of the Files table for ENTRIES (in the listing's thread: a folder may have 20,000); UNTRUSTED:
    an agent may have written there (special mode bits are marked); NUMERIC: owners as numbers (a remote system's:
    this machine's names for them would mislead)."""
    rows, colors = [], []
    # The owner's text once per user:group of this listing: the user and group databases were asked twice per entry,
    # 70 % of the time of a folder's rows (most entries of a folder share one owner).
    owners = {}
    for entry in entries:
        owner = owners.get((entry.uid, entry.gid))
        if owner is None:
            owner = owners[(entry.uid, entry.gid)] = (f"{entry.uid}:{entry.gid}" if entry.uid >= 0 else "") if \
                numeric else owner_text(entry, ranges)
        rows.append([printable(entry.name), tr(KINDS[entry.kind]),
                     files.size_text(entry.size) if entry.kind == "file" else "",
                     files.time_text(entry.mtime),
                     "→ " + printable(entry.target) if entry.kind == "link" else "",
                     owner, mode_text(entry.mode)])
        colors.append("warn" if entry.kind == "link" or entry.mode & files.SPECIAL_BITS and untrusted
                      else "muted" if entry.kind == "other" else None)
    return rows, colors


def name_collator():
    """A collator of names as the human reads them (QCollator: ICU on Linux, the system's on macOS) - letter case
    ignored; of the system's locale, or of the app's language where the locale is C/POSIX."""
    from PySide6.QtCore import QCollator, QLocale
    locale = QLocale.system()
    if locale.language() == QLocale.Language.C:
        locale = QLocale("de_DE" if language() == "de" else "en_US")
    collator = QCollator(locale)
    collator.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    return collator


def name_order(entries, collator=None):
    """ENTRIES (a Listing) in the order of the Name column: folders first, then the names as the locale collates them,
    letter case ignored ("b.txt" between "A.txt" and "C.txt", "é" beside "e", not after "z"), one Unicode
    normalisation as good as another (macOS: a name of HFS+'s NFD among names typed NFC); names that collate alike by
    their own text. The lists were sorted by lower() and code points (accents after "z"), and a click on Name sorted by
    the shown text alone (folders among the files)."""
    collator = collator or name_collator()
    ordered = sorted(entries, key=lambda entry: entry.name)  # (ties: by their own text - the sort below is stable)
    keys = {id(entry): collator.sortKey(unicodedata.normalize("NFC", entry.name)) for entry in ordered}
    ordered.sort(key=lambda entry: (entry.kind != "dir", keys[id(entry)]))
    result = files.Listing(ordered)
    result.truncated = getattr(entries, "truncated", False)
    return result


def entry_sorts(entries):
    """What the rows of ENTRIES (in name_order) sort by, column by column (fill's SORT; None: the cell's text): the Name
    by (a file?, its place in name_order) - FileRows keeps folders first either way -, the Size by the bytes (a folder's
    before every file's), the Modified by the time - by their texts "1.5 KiB" came before "100 B", and a time past what
    the system can say ("... s after 1970") among the dates by its digits."""
    return [[(entry.kind != "dir", index), None, entry.size if entry.kind == "file" else -1, entry.mtime, None, None,
             None] for index, entry in enumerate(entries)]


def mode_text(mode):
    return f"{stat.filemode(mode)} {stat.S_IMODE(mode):04o}" if mode else ""


def copy_name(name, number=1):
    """The name of a copy of NAME beside it: "a (copy).txt", "a (copy 2).txt" (a folder's: "src (copy)")."""
    stem, dot, extension = name.rpartition(".")
    if not stem or "/" in extension:  # no extension (".bashrc": the whole name)
        stem, dot, extension = name, "", ""
    mark = tr("copy") if number == 1 else tr("copy {number}", number=number)
    # At most the 255 bytes of a name: the stem cut shorter where the mark would not fit (the names of copies beside
    # a long name were too long - "File name too long", and nothing was pasted).
    while stem and len(os.fsencode(f"{stem} ({mark}){dot}{extension}")) > 255:
        stem = stem[:-1]
    if not stem:  # (an extension longer than the room: the whole name is cut instead)
        stem, dot, extension = name, "", ""
        while stem and len(os.fsencode(f"{stem} ({mark})")) > 255:
            stem = stem[:-1]
    return f"{stem} ({mark}){dot}{extension}"


LOCAL_GRACE = 0.5  # s: a local root's step not done by then (a hung NFS or FUSE mount) goes on in a thread


def local_step(work, done, owner=None, grace=None, slow=None):
    """WORK() of a local root, then DONE(result, error) on the GUI thread. In a thread, waited for LOCAL_GRACE: done by
    then - every step on a file system that answers - DONE's result is returned at once, as before; a file system that
    hangs (NFS, FUSE) holds up the window no longer than that, DONE comes when the step ends (True returned
    meanwhile). The local steps ran on the GUI thread, and a hung mount held up the whole window. SLOW(ended): the
    step goes on as a job of the page (its busy bar, its buttons greyed out, Cancel) - ENDED(cancelled) waits for it
    (files.Cancelled once CANCELLED() is true); SLOW returns False where it cannot (then as without it)."""
    import threading
    box, finished = {}, threading.Event()

    def run():
        try:
            box["result"] = work()
        except BaseException as exc:  # noqa: BLE001 - handed to DONE (OSError, ValueError) or raised (anything else)
            box["error"] = exc
        finally:
            finished.set()
    threading.Thread(target=run, name="files-local-step", daemon=True).start()
    if finished.wait(LOCAL_GRACE if grace is None else grace):
        error = box.get("error")
        if error is not None and not isinstance(error, (OSError, ValueError)):
            raise error
        return done(box.get("result"), error)

    def ended(cancelled=None):
        while not finished.wait(None if cancelled is None else 0.1):
            if cancelled():
                raise files.Cancelled(tr("Cancelled: the step still waits for the file system; it may still take "
                                         "effect once that answers."))
        if "error" in box:
            raise box["error"]
        return box.get("result")
    if slow is not None and slow(ended):
        return True
    background(ended, owner=owner, timeout=24 * 3600, done=done)
    return True


def on_root(root, work, done, owner=None, timeout=120):
    """WORK() for ROOT, then DONE(result, error) on the GUI thread: for a local root at once (local_step: unless its
    file system hangs), in a thread for a remote one (remote.RemoteRoot: a server that stalls never holds up the
    window - the listings' model). Returns DONE's result for a local root, True for a remote one."""
    if getattr(root, "remote", False):
        background(work, owner=owner, timeout=timeout, done=done)
        return True
    return local_step(work, done, owner)


INTERRUPT_AFTER = 2000  # ms: a remote job that has not stopped this long after Cancel has its request interrupted


def nav_button(text, tooltip, slot, glyph=None):
    """A button that goes somewhere (Home, Up, Refresh): an action, never a toggle, and it takes no focus from a
    click (a focused button looks pressed in some themes, as if Home stayed on); Tab still reaches it. Where the
    pane is shows in its path bar. GLYPH (a key of theme.GLYPHS): an icon instead of TEXT, which stays its accessible
    name (TOOLTIP says what it does); paint_glyphs draws it in the look's text colour."""
    widget = button("" if glyph else text, tooltip, slot)
    widget.setCheckable(False)
    widget.setAutoDefault(False)
    widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    widget.setAccessibleName(text)
    if glyph:
        widget.glyph = glyph
        paint_glyph(widget)
    return widget


GLYPH_SIZE = 16


def paint_glyph(widget):
    """WIDGET's glyph in the text colour of the look now (again after a change of theme: readable in the dark and
    high-contrast looks), drawn sharp at twice the size too (a HiDPI screen takes that one)."""
    from . import theme
    tint = theme.colors()["text"]
    icon = theme.glyph(widget.glyph, tint, GLYPH_SIZE)
    icon.addPixmap(theme.glyph(widget.glyph, tint, 2 * GLYPH_SIZE).pixmap(2 * GLYPH_SIZE, 2 * GLYPH_SIZE))
    widget.setIcon(icon)
    widget.setIconSize(QSize(GLYPH_SIZE, GLYPH_SIZE))


class FileRows(RowModel):
    """A pane's rows: "." and ".." stay the first two rows, whatever column the list is sorted by. Neither can be
    selected (so no copy, move, delete or rename ever acts on one, Ctrl+A and a Shift+click take only entries); ".."
    at the top of a pane (UP_ENABLED False) is greyed out."""
    up_enabled = True

    @staticmethod
    def pinned(row):
        return row.index < 2 and bool(row.values) and row.values[0] in (HERE, UP)

    def sort_value(self, row, column):
        value = row.sort[column] if column == 0 and row.sort else None
        if isinstance(value, tuple):  # ((a file?, its place in name_order): the place, a number for Qt's SORT_ROLE)
            return value[1]
        return super().sort_value(row, column)

    def ordered(self, rows, column, order):
        if column == 0:
            # The Name: folders first in either direction, the names as name_order collates them (the shown text's
            # lower() put folders among the files and accents after "z").
            descending = order == Qt.SortOrder.DescendingOrder

            def key(row):
                value = row.sort[0] if row.sort else None
                if not isinstance(value, tuple):
                    return (2, 0, str(self.sort_value(row, 0)))
                return (int(value[0]), -value[1] if descending else value[1], "")
            found = sorted(rows, key=key)
        else:
            found = super().ordered(rows, column, order)
        first = sorted((row for row in found if self.pinned(row)), key=lambda row: row.index)
        return first + [row for row in found if not self.pinned(row)] if first else found

    def flags(self, index):
        row = self.rows[index.row()] if index.isValid() and 0 <= index.row() < len(self.rows) else None
        if row is not None and self.pinned(row):
            if row.values[0] == UP and not self.up_enabled:
                return Qt.ItemFlag.NoItemFlags
            return Qt.ItemFlag.ItemIsEnabled
        return super().flags(index)


class FileTable(ModelTable):
    """A pane's list. A row selected and dragged further than the drag distance drags the selection (to the other
    pane, onto a folder, to other programs: FilesPage.start_drag); files dropped on it go into the folder whose row they
    are dropped on (framed while dragged over), elsewhere into the folder shown (the list framed). A click on a row
    that is part of a selection keeps the selection until the button is released (Qt's own), so several are dragged."""
    model_class = FileRows
    # Every column's edge can be dragged (the Name's too: in Stretch mode, as the long column of other tables is, it
    # could not be); the Name column is given the room the others leave when the columns are fitted (tables.py); a
    # double click on an edge fits that column to its content. Widths the human set are kept per pane across restarts
    # (state.State.table: "files", "files-right-table").
    stretch_interactive = True

    def __init__(self, headers, numeric=(), stretch=None):
        super().__init__(headers, numeric, stretch)
        # (after QTableView's own handler, which fits the column to its content: the human sized it)
        self.horizontalHeader().sectionHandleDoubleClicked.connect(lambda _column: self.sized_by_hand())
        self.pane = None  # set by its Pane
        self.press = None  # where the left button went down on a selected row
        self.drop_row = None  # framed while something is dragged over: a row, -1 the whole list, None nothing
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def sized_by_hand(self):
        """A column was fitted by a double click on its edge: the human's width from now on (and kept)."""
        self.user_sized = True
        remember = getattr(self, "remember_state", None)
        if remember is not None:
            remember(True)

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

    def __init__(self, root, items, relative="", folder_text="", untrusted=True, sandbox=True, folders=False,
                 kinds=None):
        self.root, self.items, self.relative, self.folder_text = root, list(items), relative, folder_text
        self.untrusted, self.sandbox, self.folders = untrusted, sandbox, folders
        self.kinds = list(kinds) if kinds is not None else []  # (Entry.kind of each item, where known)

    @classmethod
    def of(cls, pane, entries):
        return cls(pane.root, [(entry.name, pane.path_of(entry)) for entry in entries], pane.relative,
                   pane.path_text(), pane.untrusted(), pane.side == "sandbox",
                   any(entry.kind == "dir" for entry in entries), [entry.kind for entry in entries])

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
            top.addWidget(nav_button(tr("Refresh"), tr("Look for sandboxes and sessions again"), self.refresh_roots,
                                     "refresh"))
            top.addWidget(nav_button(tr("Other folder…"), tr("Browse another folder as an agent's"), self.add_other))
            top.addWidget(nav_button(tr("SSH…"), tr("Browse a remote system over SSH and SFTP (keys and your "
                                                     "ssh-agent only)"), lambda: self.page.remote_dialog()))
        else:
            self.roots = None
            top.addStretch()
            top.addWidget(nav_button(tr("Home"), tr("Your HOME folder (Alt+Home)"), self.home, "home"))
            self.root = files.Root("/", label=tr("This machine"), sandbox=False)
            self.relative = host_relative(os.path.realpath(os.path.expanduser("~")))
        layout.addLayout(top)
        where = QHBoxLayout()
        where.addWidget(nav_button(tr("Up"), tr("The folder above (Backspace, Alt+Up)"), self.up, "up"))
        self.path = QLineEdit()
        self.path.setAccessibleName(tr("Folder"))
        self.path.setToolTip(tr("Type a folder and press Enter"))
        self.path.returnPressed.connect(self.go_path)
        # The folders visited lately, most recent first: SANDBOX [(root path, relative)], kept while that root is in
        # the chooser; HOST [absolute path], kept in the app's state across restarts. Their menu opens from the arrow
        # at the end of the path field or with Alt+Down in it (in the field: a button beside it made the page too
        # wide for a small screen in German).
        self.recent = [] if side == "sandbox" else self.kept_recent()
        self.recent_menu = QMenu(self.path)
        self.recent_menu.setAccessibleName(tr("Recently visited folders"))
        self.recent_menu.aboutToShow.connect(self.fill_recent_menu)
        self.recent_action = self.path.addAction(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown),
                                                 QLineEdit.ActionPosition.TrailingPosition)
        self.recent_action.setToolTip(tr("Folders visited recently in this pane, the latest first (Alt+Down)"))
        self.recent_action.triggered.connect(lambda *_: self.show_recent())
        for arrow in self.path.findChildren(QToolButton):  # (the field's own button: an icon, no text to cut off)
            if arrow.defaultAction() is self.recent_action:
                arrow.setAccessibleName(tr("Recent"))
        self.path.installEventFilter(self)  # Alt+Down
        where.addWidget(self.path, 1)
        where.addWidget(nav_button(tr("Refresh"), tr("List the folder again (Ctrl+R)"), self.list, "refresh"))
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
        # The free space of the file system of the folder shown (read with each listing: after a change of folder and
        # after every copy, move or delete).
        self.space = plain_label(role="secondary", word_wrap=False)
        self.space.setAccessibleName(tr("Free space"))
        layout.addWidget(self.space)
        self.note = plain_label(role="secondary")
        self.note.hide()
        layout.addWidget(self.note)
        # Up and Home by key anywhere in the pane (Backspace too, in its list).
        context = Qt.ShortcutContext.WidgetWithChildrenShortcut
        QShortcut(QKeySequence("Alt+Up"), self, context=context, activated=self.up)
        if side == "host":
            QShortcut(QKeySequence("Alt+Home"), self, context=context, activated=self.home)
        # The pane the toolbar's buttons act on (bold): also the one whose path field, chooser or buttons were used last
        # (a folder typed in the right pane's path field, then New folder: it was made in the left pane, the one whose
        # list had the keyboard before).
        for widget in (self.path, self.roots):
            if widget is not None:
                widget.installEventFilter(self)
        for widget in self.findChildren(QPushButton):
            widget.pressed.connect(lambda: self.page.activate(self))

    def changeEvent(self, event):  # noqa: N802
        """Another look (theme): the buttons' glyphs in its text colour."""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            for widget in self.findChildren(QPushButton):
                if getattr(widget, "glyph", None):
                    paint_glyph(widget)

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
        if self.side == "host" or self.remote():
            return "/" + relative
        return self.root.path + ("/" + relative if relative else "")

    def remote(self):
        """Whether the pane shows a remote system (remote.RemoteRoot: its root is the remote "/")."""
        return bool(getattr(self.root, "remote", False))

    def path_of_entry(self, entry):
        """The absolute path (shown) of ENTRY of the folder shown."""
        return os.path.join(self.path_text(), entry.name)

    def untrusted(self):
        """True if an agent may have written what is shown: a sandbox root, or a host folder in one."""
        return self.untrusted_at(self.relative)

    def untrusted_at(self, relative):
        if self.remote():
            return False  # (the human's own system; what comes from it into a sandbox's folder counts as the agent's)
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

    def go_path(self, text=None, exact=False):
        """Show the folder typed in the path bar (absolute, or relative to the folder shown); EXACT: TEXT is a path as
        it is (Recent), never stripped of spaces."""
        raw = self.path.text() if text is None else text
        self.path.setModified(False)  # (taken: the listing that follows shows the folder's path again)
        # The bar shows the folder's path escaped (printable: a line break as \x0a, a byte that is no UTF-8 as \udcff):
        # that text, or more typed after it, stands for the folder itself, as it is (Enter went to the escaped text's
        # folder, which does not exist, or - stripped - to another one for a name that ends in a space; the pane fell
        # back to its top or HOME).
        here = self.path_text() if self.root is not None else ""
        shown = printable(here)
        if here and (raw == shown or (shown != here and raw.startswith(shown.rstrip("/") + "/"))):
            text = here + raw[len(shown):]
        else:
            text = raw if exact else raw.strip()
        if self.root is None or not text:
            return False
        try:
            if self.side == "host":
                path = os.path.expanduser(text)
                path = os.path.normpath(path if os.path.isabs(path) else os.path.join(self.path_text(), path))
                real = os.path.realpath(path)
                if real != path and self.page.through_sandbox_link(path):
                    raise files.Refused(tr("{path} goes through a symbolic link in a sandbox's folder; it is not "
                                           "followed.", path=printable(path)))
                return self.go(host_relative(real))
            else:
                base = self.root.path.rstrip("/")
                if text == base or text.startswith(base + "/"):
                    relative = text[len(base):]
                elif text.startswith("/") and not self.remote() and below(os.path.normpath(text), base, folded=True):
                    # (macOS: /users/me/ws/sub typed is the folder /Users/me/ws/sub - it was "not below the folder")
                    relative = relative_to(os.path.normpath(text), base)
                elif text.startswith("/"):
                    raise files.Refused(tr("Not a path below the folder: {path}", path=printable(text)))
                else:
                    relative = f"{self.relative}/{text}" if self.relative else text
                # ".." typed goes up, as in the host pane - by the names alone (nothing here follows a link, so the
                # folder above a name is the one it was reached from); above the pane's top it stays refused (it was
                # refused anywhere: "..", "../other" in sub/deeper gave "“..” is not followed")
                relative = posixpath.normpath(relative.strip("/") or ".")
                return self.go("" if relative == "." else relative)  # (one ".." left: parts() refuses it)
        except (OSError, ValueError) as exc:
            self.page.message.setText(printable(exc))
            # (escaped, as every listing shows it: as it is, a byte that is no UTF-8 was dropped - a\xffb showed as "ab",
            # and Enter went to the folder ab)
            self.path.setText(printable(self.path_text()))
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
        self.page.read_sandbox_paths()

    def show_roots(self, roots, keep, then=None):
        self.roots_reading = False
        self.roots.blockSignals(True)
        self.roots.clear()
        for kind, label, path in roots:
            self.roots.addItem(root_title(kind, label, path), path)
        for key, root in self.page.remotes.items():
            self.roots.addItem(tr("Remote system {name}", name=printable(root.label)), key)
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
        self.forget_roots()
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
        if path in self.page.remotes:
            root = self.page.remotes[path]
            if root.alive:
                self.root, self.relative = root, "/".join(files.parts(root.home.lstrip("/")))
            else:
                self.page.message.setText(tr("The connection to {host} has ended; SSH… connects again.",
                                             host=printable(root.label)))
        elif path:
            try:
                self.root = files.Root(path)
            except OSError as exc:
                self.page.message.setText(tr("Cannot open {path}: {error}", path=printable(path),
                                             error=printable(exc)))
        if getattr(self.page, "buttons", None):  # (Take ownership: a sandbox's folder only, never a remote system)
            self.page.mark_active()
        if self.side == "sandbox":  # (the note about other hosts' sandboxes: not under a remote system's files)
            self.note.setVisible(bool(self.note.text()) and not self.remote())
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
            self.show_space(None)
            if then is not None:
                then(False)
            return False
        root, relative = self.root, self.relative
        fallback = "" if self.side == "sandbox" else host_relative(os.path.realpath(os.path.expanduser("~")))
        remote = self.remote()
        if remote:
            fallback = "/".join(files.parts(root.home.lstrip("/")))
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
            entries = name_order(entries)
            # Whether an agent may have written there (Pane.untrusted), for the folder actually listed (also a
            # sandbox's folder reached under another path: holding_folder).
            inside = not remote and (side == "sandbox" or holding_folder(
                "/" + shown, folders, folder_identities(folders)) is not None)
            try:  # the free space of its file system (here: a hung file system holds up no window)
                space = root.free_space(shown)
            except (OSError, ValueError):
                space = None
            return entries, shown, problem, entry_rows(entries, ranges, inside, remote), inside, space, \
                entry_sorts(entries)
        self.listing = token

        def still_listing():
            if self.listing == token:
                # (kept as it is said: found again by its text - by its English beginning it was never found in German,
                # "{path} wird aufgelistet…", and stayed)
                self.listing_note = tr("Listing {path}…", path=printable(self.path_text()))
                self.page.listing_says(self, self.listing_note)
        QTimer.singleShot(LIST_WAIT, self, still_listing)
        background(work, timeout=LIST_TIMEOUT, owner=self,  # (a late listing never reaches a pane deleted meanwhile)
                   done=lambda result, error: self.listed(token, root, result, error, then))
        return True

    def listed(self, token, root, result, error, then=None):
        """Show what the listing of TOKEN found (dropped when another began since, or another root was chosen)."""
        if token != self.list_token or root is not self.root:
            return None
        self.listing = None
        if error is not None:  # the thread itself failed (or never came back)
            self.page.listing_says(self, printable(error), ended=True)
            self.entries, self.by_name = [], {}
            fill(self.table, [])
            self.show_space(None)
            self.path.setText(printable(self.path_text()))  # (it named the folder before, whose rows were gone)
            if then is not None:
                then(False)
            return False
        self.entries, relative, problem, (rows, colors), inside, space, sorts = result
        self.by_name = {entry.name: entry for entry in self.entries}
        listed = problem is None
        self.show_space(space)
        if listed:
            self.remember(relative)
        if not listed:
            self.page.listing_says(self, printable(problem), ended=True)
            self.relative = relative
            self.table.clearSelection()
        elif listed and getattr(self.entries, "truncated", False):  # (Select all, Move, Delete: never the rest unsaid)
            self.page.listing_says(self, tr("This folder holds more than {count} entries: only {count} are listed, and "
                                            "what is selected here leaves the others out.", count=files.MAX_ENTRIES),
                                   ended=True)
        else:
            self.page.listing_says(self, None, ended=True)
        # (the folder listed again - after a copy, a save, a Refresh -: what the human is typing in the path bar stays;
        # it was replaced by the folder's path in the middle of the typing)
        if relative != self.shown or not (self.path.hasFocus() and self.path.isModified()):
            self.path.setText(printable(self.path_text()))
        # "." and ".." first, in every folder (not entries: chosen() never has them, and they cannot be selected);
        # at the top of the pane ".." is greyed out, its tooltip says why.
        top = not self.can_go_up(relative)
        keys = [HERE, UP, *(entry.name for entry in self.entries)]
        blank = ["", "", "", "", "", ""]
        rows, colors = [[HERE, *blank], [UP, *blank], *rows], [None, "muted" if top else None, *colors]
        tips = [tr("This folder: Enter or a double click lists it again; drop or paste here to put things into it"),
                tr("{path} is the top of this pane: nothing above it is shown here.", path=printable(
                    self.path_at(relative))) if top else tr("The folder above (Backspace)"),
                *([None] * len(self.entries))]
        self.table.rows_model.up_enabled = not top
        moved = relative != self.shown
        # One paint for the new rows (the model is reset, the columns fitted, the list scrolled): nothing in between
        # shows for a moment.
        self.table.setUpdatesEnabled(False)
        try:
            if moved:  # (selected in the rows of the folder before: other files here)
                self.table.clearSelection()
            fill(self.table, rows, colors, tooltips=tips, keys=keys, sort=[None, None, *sorts])
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

    def is_here(self, row):
        return row >= 0 and not self.changing() and row_key(self.table, row) == HERE

    def is_pinned(self, row):
        """Whether ROW is "." or ".." (no entry: never acted on as one)."""
        return self.is_here(row) or self.is_up(row)

    def select_all(self):
        """Ctrl+A: every entry, never "." or ".."."""
        self.table.clearSelection()
        model, count = self.table.model(), self.table.rowCount()
        rows = [row for row in range(count) if row_key(self.table, row) not in (HERE, UP)]
        if not rows:
            return
        runs = []  # (first, last) of rows next to each other: a folder of 20,000 is one or two ranges
        for row in rows:
            if runs and runs[-1][1] == row - 1:
                runs[-1][1] = row
            else:
                runs.append([row, row])
        selection = QItemSelection()
        for first, last in runs:
            selection.select(model.index(first, 0), model.index(last, model.columnCount() - 1))
        self.table.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.Select)

    def open_row(self, row, _column=0):
        """A double click or Enter on ROW: "." lists the folder again, ".." goes up (at the top of the pane it says
        so), a folder is entered, a file opens in the editor."""
        if self.waiting():
            return
        if self.is_here(row):
            self.list()
            return
        if self.is_up(row):
            if not self.can_go_up():
                self.page.message.setText(tr("{path} is the top of this pane: nothing above it is shown here.",
                                             path=printable(self.path_text())))
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
        if self.untrusted() or self.remote():  # (a remote system's links: named, never followed)
            self.page.message.setText(not_followed)
            return
        link = os.path.join(self.path_text(), entry.name)
        real = os.path.realpath(link)
        # (where it points, and every link on the way there: a host's link to a link of the agent's is not followed)
        if self.page.below_sandbox(real) or self.page.through_sandbox_link(link):
            self.page.message.setText(not_followed)
        elif os.path.isdir(real) and mode == "auto":
            self.go(host_relative(real))
        elif os.path.isfile(real) and self.page.right.root is not None:
            self.page.open_editor(self.page.right.root, host_relative(real), real, mode, read_only)
        else:
            self.page.message.setText(not_followed)

    def select(self, names):
        select_keys(self.table, list(names))

    # -- the free space, the folders visited recently --

    def show_space(self, space):
        """SPACE: (free, total) bytes of the file system of the folder shown, or None (not known)."""
        if space is None or space[1] <= 0:
            self.space.setText("")
            return
        free, total = space
        self.space.setText(tr("{free} free of {total}", free=files.size_text(free), total=files.size_text(total)))

    @staticmethod
    def kept_recent():
        """The host pane's recent folders kept in the app's state (absolute paths; anything else is dropped)."""
        kept = app_state.current()
        values = kept.get(RECENT_KEY, [], list) if kept is not None else []
        found = [value for value in values if isinstance(value, str) and os.path.isabs(value) and len(value) < 4096
                 and "\0" not in value]
        return list(dict.fromkeys(found))[:RECENT]

    def recent_key(self, relative):
        """What the Recent menu keeps of the folder RELATIVE: the host's absolute path; in a sandbox's folder (the
        root as the chooser has it, relative), the root only while it is in the chooser."""
        if self.side == "host":
            return self.path_at(relative)
        return (self.roots.currentData(), relative)

    def remember(self, relative):
        """The folder RELATIVE was shown: first in the Recent menu (once, at most RECENT)."""
        if self.root is None or (self.side == "sandbox" and not self.roots.currentData()):
            return
        key = self.recent_key(relative)
        self.recent = [key, *(entry for entry in self.recent if entry != key)][:RECENT]
        if self.side == "host":
            kept = app_state.current()
            if kept is not None:
                kept.set(RECENT_KEY, list(self.recent))

    def forget_roots(self):
        """A sandbox's folders that left the chooser leave the Recent menu too."""
        if self.roots is None:
            return
        listed = {self.roots.itemData(i) for i in range(self.roots.count())}
        self.recent = [entry for entry in self.recent if entry[0] in listed]

    def recent_entries(self):
        """[(text shown, key)] of the Recent menu: the latest first, not the folder shown now; a sandbox's folders only
        while their root is in the chooser."""
        here = self.recent_key(self.relative) if self.root is not None else None
        found = []
        for key in self.recent:
            if key == here:
                continue
            if self.side == "host":
                found.append((printable(key), key))
            elif self.roots.findData(key[0]) >= 0:  # (a remote system's root ends in "/": ssh://you@host/)
                found.append((printable(key[0].rstrip("/") + "/" + key[1] if key[1] else key[0]), key))
        return found

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.FocusIn and watched in (getattr(self, "path", None), getattr(self, "roots", None)) \
                and getattr(self.page, "buttons", None):
            self.page.activate(self)
        if watched is getattr(self, "path", None) and event.type() == QEvent.Type.KeyPress and \
                event.key() == Qt.Key.Key_Down and event.modifiers() & Qt.KeyboardModifier.AltModifier:
            self.show_recent()
            return True
        return super().eventFilter(watched, event)

    def show_recent(self):
        """The Recent menu, below the path field."""
        self.recent_menu.popup(self.path.mapToGlobal(self.path.rect().bottomLeft()))
        return self.recent_menu

    def fill_recent_menu(self):
        menu = self.recent_menu
        menu.clear()
        entries = self.recent_entries()
        for text, key in entries:
            action = menu.addAction(text.replace("&", "&&"))
            action.triggered.connect(lambda *_, key=key: self.go_recent(key))
        if not entries:
            menu.addAction(tr("No folders visited yet")).setEnabled(False)

    def go_recent(self, key):
        """Show the recent folder KEY through the path bar's own checks (go_path: a link in a sandbox's folder is
        refused, as when it is typed). In a sandbox's folder of another root, that root is chosen first."""
        if self.side == "host":
            return self.go_path(key, exact=True)
        root_path, relative = key
        index = self.roots.findData(root_path)
        if index < 0:
            self.recent = [entry for entry in self.recent if entry != key]
            self.page.message.setText(tr("{path} is no longer in the list of folders.", path=printable(root_path)))
            return False
        if self.roots.currentIndex() != index:
            self.roots.setCurrentIndex(index)  # (choose_root: that root, from its top)
        if self.root is None:
            return False
        return self.go_path(self.path_at(relative), exact=True)


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
        self.listing_notes, self.before_listing = {}, ""  # (listing_says: the panes' "Listing…" notes, what they replaced)
        # {host folder: its path in the sandbox} of the running sandboxes and sessions (backend.sandbox_paths, read in
        # the background with the list of folders): Copy relative path and Copy path in the sandbox.
        self.sandbox_paths = {}
        # Connected remote systems ({key: remote.RemoteRoot}, in the sandbox pane's chooser) and how to reach them:
        # the ssh program (tests: a command that speaks SFTP), the known_hosts and ssh_config files (None: the user's).
        self.remotes, self.ssh_program, self.known_hosts, self.ssh_config = {}, "ssh", None, None
        self.ranges = files.subordinate_ranges()
        self.message = plain_label(selectable=True)
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
            "touch": button(tr("New file (Shift+F4)"), tr("A new empty file in the active pane, opened in the editor"),
                            lambda: self.new_file_dialog()),
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
        self.task.cancel_button.clicked.connect(lambda *_: self.cancelling())
        self.job_roots = []  # the roots of the job running (Cancel interrupts a remote one's stalled request)
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
        """The folders an agent may write: those of the sandbox side's list, and the projects and folders chosen,
        known at once (the list is read in the background: until it came, every host folder counted as the host's
        own - a workspace opened in the host pane meanwhile kept the agent's exec bits and followed its links)."""
        listed = [path for path in (self.left.roots.itemData(i) for i in range(self.left.roots.count())) if path]
        try:
            known = [os.path.realpath(os.path.expanduser(str(entry[2]))) for entry in
                     [*self.projects(), *self.others] if isinstance(entry, (tuple, list)) and len(entry) > 2 and entry[2]]
        except Exception:  # noqa: BLE001 - the list alone, as before
            known = []
        return list(dict.fromkeys(listed + known))

    def below_sandbox(self, path):
        return self.sandbox_holding(path) is not None

    def sandbox_holding(self, path):
        """The outermost sandbox's folder that holds the absolute host PATH, by name or as the folder itself under
        another path (holding_folder: a bind mount of it), as it lies on PATH's way; or None."""
        if not path:
            return None
        folders = self.sandbox_folders()
        return holding_folder(path, folders, folder_identities(folders))

    def holds_sandbox(self, path):
        """Whether the absolute host PATH is a sandbox's folder, lies in one or holds one (by name, folded as
        below(); or as that folder itself under another path)."""
        folders = self.sandbox_folders()
        if any(below(folder, path, folded=True) for folder in folders):
            return True
        identities = folder_identities(folders)
        return holding_folder(path, folders, identities) is not None or folder_identity(path) in identities

    def through_sandbox_link(self, path, limit=40):
        """Whether the absolute host PATH (as typed or given: not resolved) is resolved through a symbolic link that lies
        in a sandbox's folder - whatever leads there (a link to the sandbox's folder itself, a link of the host's to a
        link of the agent's). Only the path as typed was compared with the sandboxes' folders: through another path
        to the same folder, the agent's link was followed. LIMIT: links followed at most (a loop counts as through
        one)."""
        components, current, followed = [part for part in path.split("/") if part not in ("", ".")], "/", 0
        while components:
            part = components.pop(0)
            if part == "..":
                current = os.path.dirname(current) or "/"
                continue
            here = os.path.join(current, part)
            if not os.path.islink(here):
                current = here
                continue
            if self.below_sandbox(here):
                return True
            followed += 1
            try:
                target = os.readlink(here)
            except OSError:
                return False
            if followed > limit:
                return True
            components = [p for p in target.split("/") if p not in ("", ".")] + components
            current = "/" if target.startswith("/") else current
        return False

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
        # (not while a job runs: a focus change re-enabled it, which running() had switched off)
        busy = getattr(self, "task", None) is not None and self.task.running
        set_enabled(self.buttons["own"], self.active.side == "sandbox" and not self.active.remote() and not busy,
                    tr("Taking ownership works only below a sandbox's folder."))
        set_enabled(self.buttons["chown"], not self.active.remote() and not busy,
                    tr("Owners are not changed on a remote system from here."))

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
            # Ctrl alone: with Shift or Alt it is the window's (Ctrl+Shift+X: the emergency stop of every sandbox) -
            # but Ctrl+Shift+C, Copy relative path.
            if key == Key.Key_C and plain & Qt.KeyboardModifier.ShiftModifier and \
                    not plain & Qt.KeyboardModifier.AltModifier:
                return True
            return not plain & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier) and \
                key in (Key.Key_A, Key.Key_C, Key.Key_R, Key.Key_V, Key.Key_X)
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
        if control and shift and not alt and key == Key.Key_C:
            self.activate(pane)
            if not pane.waiting():
                self.copy_path(pane, form="relative")
            return True
        if control and (shift or alt) and key in (Key.Key_A, Key.Key_C, Key.Key_R, Key.Key_V, Key.Key_X):
            return False  # the window's keys (Ctrl+Shift+X: the emergency stop), never the list's
        self.activate(pane)
        changing = (Key.Key_F2, Key.Key_F5, Key.Key_F6, Key.Key_F7, Key.Key_F8, Key.Key_Delete)
        new_file = key == Key.Key_F4 and shift  # Shift+F4: a new file (F4 alone edits the current one)
        if self.task.running and (key in changing or new_file or (control and key == Key.Key_V)):
            # Its buttons are greyed out meanwhile: so are its keys (a rename or a new folder in the middle of a copy
            # or move would change what it works on).
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
            return True
        entries = (Key.Key_Return, Key.Key_Enter, Key.Key_F2, Key.Key_F3, Key.Key_F4, Key.Key_F5, Key.Key_F6,
                   Key.Key_F8, Key.Key_Delete)
        if (key in entries and not new_file or (control and key in (Key.Key_C, Key.Key_X))) and pane.waiting():
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
        elif new_file:
            self.new_file_dialog(pane)
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
            pane.select_all()
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

    def listing_says(self, pane, text, ended=False):
        """What the listing of PANE says in the message line: TEXT ("Listing…" while it is on its way; once it ENDED, why
        it could not be listed, or None: nothing). The page's own message - the report of the job that just ended, which
        lists both panes again - stays: a "Listing…" note gives it back when it goes (it went with the note: what was
        skipped, what lost its executable bit, unsaid), and what a listing says comes after it (the other pane's
        folder, deleted, fell back to HOME, and its "No such file" took the report's place)."""
        notes, current = self.listing_notes, self.message.text()
        mine = notes.pop(pane, None)
        if current not in notes.values() and current != mine:
            self.before_listing = current  # (the page's own message, not a note)
        if not ended:
            notes[pane] = text
            self.message.setText(text)
            return
        if mine is not None and current != mine:
            mine = None  # (said something else meanwhile: it stays)
        if mine is None and text is None:
            return
        kept = [note for other, note in notes.items() if other.listing is not None]
        if mine is not None:
            base = kept[-1] if kept else self.before_listing
        else:
            base = current if current not in notes.values() else self.before_listing
        report = "\n".join(self.last_report)
        if text is None:
            self.message.setText(base)
        elif base and base == report:
            self.message.setText(f"{base}\n{text}")
        else:
            self.message.setText(text)

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

    def root_job(self, root, work, done, text):
        """WORK(cancelled) on ROOT, then DONE(result, error) on the GUI thread: at once for a local root, as the job of
        the page's Task for a remote one (its busy bar and Cancel; a request that stalls is interrupted once Cancel
        was pressed and INTERRUPT_AFTER passed). Returns DONE's result for a local root; for a remote one whether the
        job began (one at a time, as copies are)."""
        if not getattr(root, "remote", False):
            # (a step on a file system that hangs goes on as the page's job: its buttons greyed out - a copy or a delete
            # started meanwhile worked on what it changes - and Cancel, which gives the page back)
            stop = threading.Event()

            def slow(ended):
                return self.run_job([root], lambda cancelled: ended(lambda: cancelled() and (stop.set() or True)),
                                    done, text)
            return local_step(lambda: work(stop.is_set), done, self, slow=slow)
        started = self.run_job([root], work, done, text)
        if not started:
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
        return started

    def run_job(self, roots, work, done, text):
        """WORK(cancelled) as the page's Task (one at a time; Cancel), on ROOTS: those of them on a remote system have
        their stalled request interrupted after Cancel (cancelling). (Only root_job named its roots: Cancel of a copy,
        move or delete with a remote side waited for the request's timeout, or interrupted another job's connection.)"""
        started = self.task.thread(work, done, text, cancellable=True)
        if started:
            self.job_roots = [root for root in roots if root is not None]
        return started

    def cancelling(self):
        """Cancel was pressed: a remote job that has not stopped after INTERRUPT_AFTER has its stalled request
        interrupted (that connection ends; SSH… connects again)."""
        roots = [root for root in getattr(self, "job_roots", []) if getattr(root, "remote", False)]
        if not roots:
            return
        running = self.task.call

        def interrupt():
            if self.task.running and self.task.call is running:
                for root in roots:
                    root.sftp.interrupt()
        QTimer.singleShot(INTERRUPT_AFTER, self, interrupt)

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
        if self.task.running:  # (a drop is neither a key nor a button: it asked, then found the job running)
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
            return False
        src_root, dst_root, untrusted, items = clip.root, target.root, clip.untrusted, list(clip.items)
        refused = list(clip.problems)  # (paths refused before: named in the report too)
        target_text = target.path_at(target_dir)
        target_untrusted = target.untrusted_at(target_dir)  # (before the question, as the rest)
        remote = getattr(src_root, "remote", False) or getattr(dst_root, "remote", False)
        if getattr(src_root, "remote", False) and target_untrusted:
            untrusted = True  # what comes from a remote system into a sandbox's folder counts as the agent's
        out = untrusted and not target_untrusted
        # Between this machine's folders the agent's files are treated as such only where they go out of its folders
        # (OUT): a copy or a merging move within a sandbox's folders dropped every executable bit, unsaid. (A remote
        # side keeps its rule: what comes from it into a sandbox's folder went through a staging folder there.)
        untrusted = untrusted if remote else out
        # A sandbox's folder met in what goes where no agent writes - the folder copied itself, or one in it: an entry of
        # a host folder of the human's (~/projects/ws, or ~/projects with ws in it) went as the human's own, its
        # executables executable, its hooks unnamed (files.copy_entry's AGENTS: that part as the agent's).
        agents = frozenset() if target_untrusted or getattr(src_root, "remote", False) else \
            frozenset(folder_identities(self.sandbox_folders()))
        return self.ask_replace([new or name for name, _, new in items], clip.folder_text, dst_root, target_dir,
                                target_text, then=lambda overwrite: self.start_transfer(
                                    src_root, dst_root, items, target_dir, target_text, overwrite, untrusted,
                                    target_untrusted, refused, out, move, agents))

    def start_transfer(self, src_root, dst_root, items, target_dir, target_text, overwrite, untrusted,
                       target_untrusted, refused, out, move, agents=frozenset()):
        """The copy or move of transfer_items, once it is known whether to replace (OVERWRITE)."""
        progress = files.Progress()

        def work(cancelled):
            done, problems = [], list(refused)
            for name, path, new in items:
                try:
                    if move:
                        done.append(files.move_entry(src_root, path, dst_root, target_dir, overwrite=overwrite,
                                                     untrusted=untrusted, target_untrusted=target_untrusted,
                                                     cancelled=cancelled, progress=progress, agents=agents))
                    else:
                        done.append(files.copy_entry(src_root, path, dst_root, target_dir, overwrite=overwrite,
                                                     untrusted=untrusted, name=new, cancelled=cancelled,
                                                     progress=progress, agents=agents))
                except files.Cancelled as exc:
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems
        # (the folder's name as every other on the screen: an agent's line break or escape sequence shown escaped)
        text = (tr("Moving {count} item(s) to {target}…", count=len(items), target=printable(target_text)) if move
                else tr("Copying {count} item(s) to {target}…", count=len(items), target=printable(target_text)))
        started = self.run_job([src_root, dst_root], work,
                               lambda result, error: self.transferred(result, error, out, move), text)
        return self.follow(started, progress, text)

    def ask_replace(self, names, source_text, target_root, target_dir, target_text, then):
        """THEN(True): replace what exists (asked first), THEN(False): leave it (those copies are refused); nothing
        when stopped. Which of NAMES exist is read through root_job (a remote target: in a thread). Returns THEN's
        result (False when stopped); with a remote target True once the check began."""
        if not self.replace.isChecked():
            return then(False)

        def checked(existing, error):
            if error is not None:
                self.message.setText(printable(error))
                return False
            if not existing:
                return then(False)
            if ask_first(self, "files-replace", tr("Replace existing files?"),
                         tr("{count} of the {total} item(s) from {source} already exist in {target}. Replace them? "
                            "Files are replaced, folders are filled with what is copied.", count=len(existing),
                            total=len(names), source=printable(source_text), target=printable(target_text)),
                         tr("Replace"), details="\n".join(printable(name) for name in existing)):
                return then(True)
            return False
        return self.root_job(target_root, lambda _cancelled: files.exists_in(target_root, target_dir, names), checked,
                             tr("Looking for what already exists in {target}…", target=printable(target_text)))

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
        # (written while they were copied: the copy may hold part old, part new - the report said only "copied")
        changed = [path for r in done for path in r.changed]
        if changed:
            lines.append(tr("Changed while they were copied (the copy may not match the file; copy it again): {list}",
                            list=", ".join(printable(path) for path in changed[:20]) + (" …" if len(changed) > 20
                                                                                       else "")))
        lines += [printable(r.kept) for r in done if r.kept]
        lines += [printable(problem) for problem in problems]
        # (out of a sandbox's folder: whatever was copied; else what came from a sandbox's folder met in it)
        risky = [path for r in done for path in (r.risky if copied_out else files.risky(r.agent_paths))]
        # (wherever bits were dropped: out of a sandbox's folder, or from a remote system into one)
        executable = [path for r in done for path in r.executable]
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
        return self.ask_replace([name for name, _ in items], self.left.path_text(), host, target, destination,
                                then=lambda overwrite: self.start_download(root, host, items, target, overwrite))

    def start_download(self, root, host, items, target, overwrite):
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
        started = self.run_job([root, host], work,
                               lambda result, error: self.transferred(result, error, True, False), text)
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
            if real != folder and self.through_sandbox_link(folder):
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

    def copy_paths(self, paths, target, target_dir, foreign=False):
        """Copy the host PATHS into the folder TARGET_DIR of the TARGET pane, in a thread (host_items). FOREIGN: another
        program gave them (the system clipboard, a drop) - into an agent's folder only after a question that names
        them: an agent that reaches the desktop (--gui-wayland) can put the URL of any file of the user's there, and a
        Ctrl+V in the sandbox pane copied the user's keys into its folder."""
        if self.task.running:  # (said before the question: asked and answered, the copy was refused all the same)
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
            return False
        clip, problems = self.host_items(paths)
        if problems:
            self.message.setText("\n".join(problems))
        if not clip.items:
            return False
        root = target.root  # (what the questions name: a question blocks nothing, the pane may show another by the answer)
        if foreign and target.remote() and self.sandbox_host(target.root) and not confirm(
                self, tr("Copy files of another program to {host}?", host=printable(target.root.label)),
                tr("{count} item(s) that another program put on the clipboard or dropped here would be copied into "
                   "{folder} on {host}, a host of sandbox-ai's where sandboxes run and agents may read them. An agent "
                   "that reaches your desktop can put any of your files there. Copy them?", count=len(clip.items),
                   folder=printable(target.path_at(target_dir)), host=printable(target.root.label)),
                tr("Copy to {host}", host=printable(target.root.label)),
                details="\n".join(printable(os.path.join("/", path)) for _name, path, _new in clip.items)):
            self.message.setText(tr("Nothing was copied."))
            return False
        if foreign and target.untrusted_at(target_dir) and not confirm(
                self, tr("Copy files of another program into the agent's folder?"),
                tr("{count} item(s) that another program put on the clipboard or dropped here would be copied into "
                   "{folder}, where the agent reads them. An agent that reaches your desktop can put any of your files "
                   "there. Copy them?", count=len(clip.items), folder=printable(target.path_at(target_dir))),
                tr("Copy into the sandbox"),
                details="\n".join(printable(os.path.join("/", path)) for _name, path, _new in clip.items)):
            self.message.setText(tr("Nothing was copied."))
            return False
        if target.root is None or not same_root(target.root, root):
            # (another sandbox's folder or a remote system chosen while it asked: the files went there - into the folder
            # of that name -, asked about the one before; to a host of sandbox-ai's unasked)
            self.message.setText(tr("The folder shown changed meanwhile; nothing was done."))
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
        paths = choose_files(self, tr("Files to copy into {path}", path=printable(self.left.path_text())), "copy-in")
        if paths:
            self.upload(paths)

    # -- opening: the editor, the properties --

    def open_current(self, pane, mode="auto", read_only=False):
        """F3 (READ_ONLY) and F4: the current file in the editor (a Markdown file: F3 rendered, F4 its text)."""
        entry = pane.current()
        if entry is None:
            pane.waiting() or self.message.setText(tr("Select a file first."))
            return False
        if entry.kind == "dir":
            self.message.setText(tr("{name} is a folder.", name=printable(entry.name)))
            return False
        from .markdown_view import is_markdown
        if mode == "auto" and not read_only and is_markdown(entry.name):
            mode = "text"  # F4 edits
        pane.open_entry(entry, mode, read_only)
        return True

    def open_editor(self, root, relative, shown, mode="auto", read_only=False):
        """The file RELATIVE of ROOT (SHOWN: its path) in an editor window of its own (file_editor.py); a Markdown file
        opens rendered (markdown_view.py), and a Markdown file of the same root a link of it leads to opens in a
        viewer of its own."""
        from .file_editor import EditorWindow
        from .markdown_view import is_markdown
        if mode == "auto" and is_markdown(relative):
            mode = "markdown"
        # (a link opens through the window's root as it is then: connected() gives it the new connection after SSH…
        # connected again - the root of the moment the window opened was the old one, closed)
        window = EditorWindow(self, root, relative, shown, mode, read_only, saved=self.refresh_panes,
                              open_file=lambda other, where: self.open_editor(window.root, other, where, "markdown",
                                                                              True), top=self.markdown_top(root, relative))
        self.keep(self.editors, window)
        window.show()
        return window

    def markdown_top(self, root, relative):
        """The folder (relative to ROOT) a Markdown file RELATIVE shows images and opens links of: in the host's
        Root("/"), a file in a sandbox's folder only that folder's (the outermost one that holds it); else the whole
        root ("": a sandbox's root is its folder, a remote one the human's own system)."""
        if getattr(root, "remote", False) or getattr(root, "sandbox", True) or root.path != "/":
            return ""
        path = "/" + "/".join(files.parts(relative))
        outer = self.sandbox_holding(path)
        if outer is None:
            return ""
        return "/".join(files.parts(relative)[:len([part for part in outer.split("/") if part])])

    def may_quit(self):
        """Whether the app may quit as far as the editors are concerned: each one with changes not saved is shown and
        asked about (its own "Discard changes?" question); one kept open ("Keep editing") stops the quitting as a whole,
        before anything was shut down. The main window was shut down first and the editors asked only by Qt's quit
        afterwards: "Keep editing" left an editor open in an app whose window had gone and whose saves never came
        back. (Their changes are dropped only when the app really quits: drop_changes.)"""
        from shiboken6 import isValid
        for window in [window for window in self.editors if isValid(window) and window.isVisible() and window.dirty]:
            window.raise_()
            window.activateWindow()
            if not window.may_close():
                return False
        return True

    def drop_changes(self):
        """The app quits for good (everything was asked, or nobody is there to answer): the editors close without
        asking again."""
        from shiboken6 import isValid
        for window in self.editors:
            if isValid(window):
                window.dirty = False
                window.dropped = True  # (one still saving asks nothing either)

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
        root = pane.root
        return on_root(root, lambda: files.entry_infos(root, items),
                       lambda found, error: self.show_properties(pane, root, items, text, untrusted, sandbox, relative,
                                                                 found, error), owner=self)

    def show_properties(self, pane, root, items, text, untrusted, sandbox, relative, found, error):
        """The Properties dialog of ITEMS, with what files.entry_infos FOUND (in a thread for a remote root)."""
        from .file_properties import PropertiesDialog
        if error is not None:
            self.message.setText(printable(error))
            return None
        dialog = PropertiesDialog(self, root, items, text, untrusted, changed=self.refresh_panes, infos=found,
                                  owner=None if getattr(root, "remote", False) else lambda root, shown, folders: self.ask_owner(Chosen(
                                      root, shown, relative, text, untrusted, sandbox, folders)))
        self.keep(self.dialogs, dialog)
        dialog.show()
        return dialog

    # -- remote systems (remote.py: SSH and SFTP) --

    def sandbox_targets(self):
        """[(name, remote.Target)] of sandbox-ai's hosts (`sandbox-ai hosts`: those reached over SSH)."""
        from . import remote
        found = []
        try:
            entries = self.sai.host_entries()
        except Exception:  # noqa: BLE001 - an older sandbox-ai, no hosts file: none
            entries = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("kind") != "ssh" or not entry.get("ssh"):
                continue
            try:
                target = remote.Target.parse(entry["ssh"], entry.get("identity") or "")
                if entry.get("port") and int(entry["port"]) != 22:
                    target.port = int(entry["port"])
            except (files.Refused, TypeError, ValueError):
                continue
            found.append((str(entry.get("name", "")), target))
        return found

    def sandbox_host(self, root):
        """Whether the remote ROOT is one of sandbox-ai's hosts (by its host name and port, also as ssh resolves them:
        ROOT.endpoint and .sandbox_endpoints, read by connect_remote): sandboxes run there, their agents may read what
        is copied there. In doubt it is one: a name without a port whose port ssh could not tell is taken as any port
        (an alias of ~/.ssh/config with the Port of a sandbox-ai host's entry was "another host", and its files went
        there unasked)."""
        target = getattr(root, "target", None)
        if target is None:
            return False
        endpoint = getattr(root, "endpoint", None)
        if endpoint is not None and endpoint in getattr(root, "sandbox_endpoints", ()):
            return True
        names = {target.host.lower(), *([endpoint[0]] if endpoint else [])}
        ports = {target.port or 22, *([endpoint[1]] if endpoint else [])}
        any_port = not target.port and endpoint is None
        return any(known.host.lower() in names and (any_port or (known.port or 22) in ports)
                   for _name, known in self.sandbox_targets())

    def remote_choices(self):
        """[(text shown, remote.Target)]: sandbox-ai's hosts, then the Host entries of ~/.ssh/config."""
        from . import remote
        found = [(tr("{name} (sandbox-ai host, {target})", name=printable(name), target=target.text()), target)
                 for name, target in self.sandbox_targets()]
        for alias in remote.ssh_config_hosts():
            found.append((tr("{name} (~/.ssh/config)", name=alias), remote.Target(alias)))
        return found

    def remote_dialog(self):
        """SSH…: which remote system (a sandbox-ai host, a Host of ~/.ssh/config, or user@host[:port] typed)."""
        choices = self.remote_choices()
        text, ok = QInputDialog.getItem(self, tr("Remote system"), shown_as_text(tr(
            "Connect to (user@host, host:port, or a host below). Keys and your ssh-agent only: no password is asked "
            "for or kept.")), [label for label, _ in choices], 0, True)
        if not ok or not text.strip():
            return False
        return self.connect_remote(dict(choices).get(text) or text.strip())

    def connect_remote(self, target):
        """Connect to TARGET (a remote.Target or its text) in the background; the sandbox pane shows it once it is."""
        from . import remote
        try:
            target = target if isinstance(target, remote.Target) else remote.Target.parse(target)
        except files.Refused as exc:
            self.message.setText(printable(exc))
            return False
        ssh, known, config = self.ssh_program, self.known_hosts, self.ssh_config
        hosts = [known_target for _name, known_target in self.sandbox_targets()]
        self.message.setText(tr("Connecting to {host}…", host=printable(target.text())))

        def connect():
            root = remote.RemoteRoot(target, "/", ssh=ssh, known_hosts=known, config=config)
            # (where ssh really connects it and sandbox-ai's hosts to - an alias's HostName and Port: sandbox_host)
            root.endpoint = remote.endpoint_of(target, config, ssh)
            root.sandbox_endpoints = {found for found in (remote.endpoint_of(host, config, ssh) for host in hosts)
                                      if found is not None}
            return root
        background(connect, owner=self, timeout=120, work=False,
                   done=lambda root, error: self.connected(target, root, error))
        return True

    def connected(self, target, root, error):
        from . import remote
        if isinstance(error, remote.HostKeyUnknown):
            background(self.scan_keys, target, self.known_hosts, self.ssh_config, owner=self, timeout=60, work=False,
                       done=lambda found, problem: self.trust_host(target, found, problem))
            return False
        if isinstance(error, remote.AuthenticationNeeded):
            self.message.setText(printable(error))
            if confirm(self, tr("Unlock your key?"), tr(
                    "{error}\n\nOpen a terminal with ssh to {host}? There you can enter a passphrase (or run "
                    "ssh-add to keep the key in your agent); then connect again with SSH….",
                    error=printable(error), host=printable(target.text())), tr("Open a terminal")):
                self.open_terminal(remote.ssh_command(target, self.known_hosts), target.text())
            return False
        if error is not None:
            self.message.setText(printable(error))
            if isinstance(error, remote.HostKeyChanged):
                warn(self, tr("Host key changed"), printable(error))
            return False
        old = self.remotes.get(root.key)
        if old is not None and old is not root:
            background(old.close, owner=self, timeout=30, done=lambda *_: None)  # (ssh's end is waited for)
            # What is still open on the old connection goes on through the new one (the same system and folder): an
            # editor could save nothing any more, and what was typed in it was lost.
            for window in [*self.editors, *self.dialogs]:
                if getattr(window, "root", None) is old:
                    window.root = root
                    for view in (getattr(window, "preview", None), getattr(window, "markdown", None)):
                        source = getattr(view, "source", None)
                        if source is not None and getattr(source, "root", None) is old:
                            source.root = root
            for clip in (self.clipboard, self.drag):
                if clip is not None and clip.root is old:
                    clip.root = root
        self.remotes[root.key] = root
        self.message.setText(tr("Connected to {host}.", host=printable(root.label)))
        self.refresh_roots(select=root.key)
        return True

    @staticmethod
    def scan_keys(target, known, config):
        """(known_hosts lines, their fingerprints, the known_hosts file they go to) of TARGET - in a thread (background):
        the file too, which `ssh -G` names (it ran on the GUI thread, after the yes, and held up the window)."""
        from . import remote
        lines = remote.scan_host(target)
        return lines, remote.fingerprints(lines), known or remote.known_hosts_file(target, config)

    def trust_host(self, target, found, problem):
        """The host's keys, shown; added to known_hosts (FOUND: (lines, fingerprints, the known_hosts file), read in a
        thread) and connected again only after a yes."""
        from . import remote
        if problem is not None:
            self.message.setText(printable(problem))
            return False
        lines, prints, known = found
        if not confirm(self, tr("Trust this host?"), tr(
                "{host} is not among your known hosts. It shows these keys:\n\n{keys}\n\nCompare them with what "
                "whoever runs it tells you (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub there). Trust them and "
                "connect?", host=printable(target.text()), keys="\n".join(printable(k) for k in prints)),
                tr("Trust and connect")):
            self.message.setText(tr("Not connected: {host}'s keys were not trusted.", host=printable(target.text())))
            return False
        try:
            remote.add_known_host(lines, known, target)
        except (OSError, ValueError) as exc:
            self.message.setText(printable(exc))
            return False
        return self.connect_remote(target)

    def open_terminal(self, argv, title):
        """ARGV (ssh, interactive) in a terminal window of its own; without a terminal, the command to run."""
        import shlex
        from PySide6.QtCore import QProcess
        command = getattr(self.sai, "in_terminal", lambda *_a, **_k: None)(argv, f"ssh {title}")
        if not command or not QProcess.startDetached(command[0], command[1:]):
            self.message.setText(tr("Run this in a terminal: {command}", command=printable(shlex.join(argv))))
            return False
        return True

    def read_sandbox_paths(self):
        """backend.sandbox_paths in the background (it reads sandbox-ai's registry, which may ask Podman)."""
        reader = getattr(self.sai, "sandbox_paths", None)
        if reader is None:
            return
        background(reader, owner=self, timeout=60, work=False,
                   done=lambda found, error: setattr(self, "sandbox_paths", dict(found or {}) if error is None
                                                     else self.sandbox_paths))

    def sandbox_base(self, path):
        """(host folder, its path in the sandbox) of the innermost folder of a running sandbox or session that holds
        PATH (absolute, without links), or None."""
        if path.startswith("ssh://"):
            return None
        # (folded as the file system compares names: on macOS /users/me/ws is the folder /Users/me/ws)
        found = [(host, inside) for host, inside in self.sandbox_paths.items() if below(path, host, folded=True)]
        return max(found, key=lambda pair: len(pair[0])) if found else None

    def path_forms(self, pane, path):
        """{"absolute", "relative", "sandbox" (or None)} of PATH (absolute) in PANE: relative to the folder of the
        sandbox or session that holds it (its workspace - the agent's /work - or its HOME), in a sandbox's pane to
        the pane's folder, else to the folder shown; in the sandbox: the path the agent sees."""
        base = None if pane.remote() else self.sandbox_base(path)
        if pane.remote():
            relative = os.path.relpath(path, pane.path_text())
        elif base is not None:
            relative = relative_to(path, base[0])
        elif pane.side == "sandbox" and pane.root is not None:
            relative = os.path.relpath(path, pane.root.path)
        else:
            relative = os.path.relpath(path, pane.path_text())
        inside = None
        if base is not None:
            inside = base[1] if relative == "." else posixpath.join(base[1], relative)
        return {"absolute": path, "relative": relative, "sandbox": inside}

    def copy_path(self, pane, entries=None, form="absolute", quoted=False):
        """The paths of ENTRIES (none: of the folder shown) as text on the clipboard, one a line: FORM "absolute",
        "relative" (Ctrl+Shift+C) or "sandbox" (as the agent sees it, path_forms); QUOTED: each quoted for the shell.
        Control characters in a name are shown escaped (a pasted path never carries a line break or an escape
        sequence into a terminal)."""
        if pane.root is None:  # (no folder shown: Ctrl+Shift+C failed - "no path specified"; the menu greys it out)
            self.message.setText(tr("Choose a folder first."))
            return None
        entries = pane.chosen() if entries is None else entries
        paths = [pane.path_of_entry(entry) for entry in entries] or [pane.path_text()]
        chosen = [self.path_forms(pane, path)[form] for path in paths]
        if any(path is None for path in chosen):
            self.message.setText(tr("Not in a running sandbox or session: there is no path in the sandbox for it."))
            return None
        text = "\n".join(shell_quoted(path) for path in chosen) if quoted else \
            "\n".join(printable(path) for path in chosen)
        QGuiApplication.clipboard().setText(text)
        self.message.setText(tr("Copied to the clipboard: {paths}", paths=text if len(paths) == 1 else
                                tr("{count} paths", count=len(paths))))
        return text

    # -- the context menu --

    def context_menu(self, pane, point):
        """The menu of a right click at POINT of PANE's list: for the rows selected, or for the folder shown when the
        click is on no row (or on "." or "..": Paste, New folder, Properties of the folder shown)."""
        self.activate(pane)
        menu = self.menu(pane, at_row=pane.table.indexAt(point).isValid() and not pane.is_pinned(
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
        # The folder the menu is about: its actions find their items by name in the folder shown when they run, and
        # the folder may change while the menu is open (the list of folders read anew chose another one) - an item
        # of the same name there was renamed, deleted or copied instead.
        shown = (pane.root, pane.relative)

        def run(slot):
            if (pane.root, pane.relative) != shown:
                self.message.setText(tr("The folder shown changed meanwhile; nothing was done."))
                return
            slot()

        def add(text, slot, shortcut=None, enabled=True, tip=None):
            action = menu.addAction(text)
            if shortcut:
                action.setShortcut(shortcut)
                action.setShortcutVisibleInContextMenu(True)
            action.setEnabled(bool(enabled))
            if tip:
                action.setToolTip(tip)
            action.triggered.connect(lambda *_: run(slot))
            return action
        menu.setToolTipsVisible(True)
        if entries:
            file_like = one is not None and one.kind in ("file", "link")
            add(tr("Open"), lambda: pane.open_entry(one), "Return", one is not None)
            add(tr("View"), lambda: pane.open_entry(one, read_only=True), "F3", file_like)
            add(tr("Edit"), lambda: pane.open_entry(one, "text"), "F4", file_like)
            add(tr("Open with the hex editor"), lambda: pane.open_entry(one, "hex"), None, file_like)
            add(tr("View as Markdown"), lambda: pane.open_entry(one, "markdown", read_only=True), None, file_like)
            menu.addSeparator()
            add(tr("Copy"), lambda: self.copy_items(pane, entries), "Ctrl+C")
            add(tr("Cut"), lambda: self.copy_items(pane, entries, cut=True), "Ctrl+X")
        folder = one if one is not None and one.kind == "dir" else None
        # (an "&" of the name doubled: a menu takes one for its shortcut key - a&b showed as "ab")
        add(tr("Paste into {name}", name=printable(folder.name).replace("&", "&&")) if folder else tr("Paste"),
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
        add(tr("New file…"), lambda: self.new_file_dialog(pane), "Shift+F4", has_root and not busy)
        add(tr("Copy path"), lambda: self.copy_path(pane, entries), None, has_root)
        add(tr("Copy relative path"), lambda: self.copy_path(pane, entries, "relative"), "Ctrl+Shift+C", has_root,
            tr("Relative to the sandbox's folder (its workspace: the agent's /work, or its HOME)"))
        paths = [pane.path_of_entry(entry) for entry in entries] or ([pane.path_text()] if has_root else [])
        # (never for a remote system's file: its path may be that of a sandbox's folder here, and is none of it)
        inside = bool(paths) and not pane.remote() and all(self.sandbox_base(path) is not None for path in paths)
        add(tr("Copy path in the sandbox"), lambda: self.copy_path(pane, entries, "sandbox"), None, inside,
            tr("The path as the agent sees it (/work/…, its HOME /root/…)") if inside else
            tr("Only for a folder of a running sandbox or session"))
        quoted = menu.addMenu(tr("Copy quoted for the shell"))
        quoted.setEnabled(has_root)
        for text, form, enabled in ((tr("Path"), "absolute", True), (tr("Relative path"), "relative", True),
                                    (tr("Path in the sandbox"), "sandbox", inside)):
            action = quoted.addAction(text)
            action.setEnabled(enabled)
            action.triggered.connect(lambda *_, form=form: run(lambda: self.copy_path(pane, entries, form, True)))
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

    def pasted(self):
        """What a paste would take: ("clip", the items copied or cut here) - unless another program has put text or
        files on the clipboard since -, ("paths", the local files another program put there), or None."""
        mime = QGuiApplication.clipboard().mimeData()
        clip = self.clipboard
        if clip is not None and (clip.owns(mime) or mime is None or not mime.formats()):
            return "clip", clip
        paths = [path for path in map(local_path, mime.urls() if mime is not None else []) if path]
        if paths:
            return "paths", paths
        if clip is not None and not (mime.hasText() or mime.hasUrls()):
            return "clip", clip
        return None

    def can_paste(self):
        """Whether Paste has something (pasted(): the menu offered Paste for items copied here while another
        program's text was on the clipboard - and Paste said there was nothing to paste)."""
        return self.pasted() is not None

    def paste(self, pane, target_dir=None):
        """Paste into the folder TARGET_DIR (default: the one shown) of PANE: what was copied or cut here (a cut is
        moved, once), else the local files another program put on the clipboard (copied)."""
        if pane.root is None:
            return False
        target_dir = pane.relative if target_dir is None else target_dir
        found = self.pasted()
        if found is not None and found[0] == "clip":
            return self.paste_clip(found[1], pane, target_dir)
        if found is not None:
            return self.copy_paths(found[1], pane, target_dir, foreign=True)
        self.message.setText(tr("Nothing to paste: copy or cut files first (Ctrl+C, Ctrl+X)."))
        return False

    def paste_clip(self, clip, pane, target_dir):
        same = clip.folder_text == pane.path_at(target_dir) and same_root(clip.root, pane.root)
        if same and clip.cut:
            self.message.setText(tr("They are in this folder already."))
            return False
        if same and getattr(pane.root, "remote", False):
            self.message.setText(tr("Copies from one remote folder to another are not made here; move works within "
                                    "one remote system."))
            return False
        if same:  # a copy beside the original, under a name of its own (a local root: a remote one is refused above)
            # (on_root: done at once - or, on a file system that hangs, when the names are read: its result came back
            # as True then, and unpacking it failed)
            root, names = pane.root, [copy_name(name, n) for name, _, _ in clip.items for n in range(1, 21)]
            return on_root(root, lambda: set(files.exists_in(root, target_dir, names)),
                           lambda taken, error: self.paste_beside(clip, pane, target_dir, taken, error, root),
                           owner=self)
        return self.paste_items(clip, pane, target_dir)

    def paste_beside(self, clip, pane, target_dir, taken, error, root=None):
        """A copy of CLIP's items beside them, each under the first name of copy_name not TAKEN there (in ROOT: the
        pane's when the paste began)."""
        if error is not None:
            self.message.setText(printable(error))
            return False
        if root is not None and (pane.root is None or not same_root(pane.root, root)):
            # (a slow file system: the names were read in the background, and another sandbox's folder chosen meanwhile
            # got the copies - in its folder of that name)
            self.message.setText(tr("The folder shown changed meanwhile; nothing was done."))
            return False
        items = []
        for name, path, _ in clip.items:
            new = next((copy_name(name, n) for n in range(1, 21) if copy_name(name, n) not in taken), None)
            if new is None:
                self.message.setText(tr("{name}: too many copies here already.", name=printable(name)))
                return False
            items.append((name, path, new))
        return self.paste_items(Clip(clip.root, items, clip.untrusted, clip.folder_text), pane, target_dir)

    def paste_items(self, clip, pane, target_dir):
        started = self.transfer_items(clip, pane, target_dir, move=clip.cut)
        if started and clip.cut:
            self.clipboard = None  # moved: a second paste has nothing to move
            # (nor from the system clipboard: it named the files where they were, and a second paste copied from there)
            if clip.owns(QGuiApplication.clipboard().mimeData()):
                QGuiApplication.clipboard().clear()
        return started

    def export_urls(self, pane, entries):
        """The file URLs of ENTRIES that other programs may get (a drag out, the clipboard): never a link or a special
        file; from a sandbox's folder only plain files that are not executable (a folder could hold hooks or
        programs, which another program would copy as they are, executable bits and all)."""
        untrusted = pane.untrusted()
        if pane.remote() or pane.root is None:
            return []  # (a remote file is no local one another program could take)
        if not untrusted:
            # (never a folder that is a sandbox's or holds one: ~/projects/ws listed in ~/projects went out as it is)
            return [file_url(pane.path_of_entry(entry)) for entry in entries if entry.kind == "file" or
                    entry.kind == "dir" and not self.holds_sandbox(pane.path_of_entry(entry))]
        # An agent's: as they are now, not as they were listed - it may have made a file executable since, or put a link
        # to a folder in its place (lstat relative to the folder opened once, never through a link). In a thread, waited
        # for LOCAL_GRACE (the GUI thread waits for it): a file system that does not answer by then gives nothing out.
        root, relative, names = pane.root, pane.relative, [entry.name for entry in entries if entry.kind == "file"]

        def plain_files():
            found, folder = [], root.open_dir(relative)
            try:
                for name in names:
                    try:
                        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
                    except (OSError, ValueError):
                        continue
                    if stat.S_ISREG(info.st_mode) and not info.st_mode & (0o111 | files.SPECIAL_BITS):
                        found.append(name)
            finally:
                os.close(folder)
            return found
        box, finished = {}, threading.Event()

        def run():
            try:
                box["names"] = plain_files()
            except (OSError, ValueError):
                box["names"] = []
            finally:
                finished.set()
        threading.Thread(target=run, name="files-export-check", daemon=True).start()
        if not finished.wait(LOCAL_GRACE):
            return []
        return [file_url(os.path.join(pane.path_text(), name)) for name in box["names"]]

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
        of the row at POINT ("." the folder shown, ".." the one above - at the top of the pane: the folder shown), else
        into the folder shown. Items dragged in this window are copied, with Shift moved; local files of another
        program are copied."""
        if pane.root is None or mime is None or pane.changing():
            return None  # (while the rows are another folder's: no row to drop onto)
        index = pane.table.indexAt(point)
        target_dir, row = pane.relative, -1
        if index.isValid():
            entry = pane.entry_at(index.row())
            if pane.is_up(index.row()) and pane.can_go_up():
                target_dir, row = "/".join(files.parts(pane.relative)[:-1]), index.row()
            elif pane.is_here(index.row()):
                row = index.row()  # "." is the folder shown
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
        paths = [path for path in map(local_path, mime.urls()) if path] if mime.hasUrls() else []
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
        return self.copy_paths(plan.paths, plan.pane, plan.target_dir, foreign=True)

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
        started = self.run_job([root], work, self.deleted, text)
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
        # (Qt drops what is no UTF-8 from the name it shows - a\xffb shows as "ab": left as it was, it is no new name)
        if ok and new in (entry.name, "".join(c for c in entry.name if not "\ud800" <= c <= "\udfff")):
            return False
        return bool(ok and new) and self.rename(pane, entry, new, chosen)

    def rename(self, pane, entry, new_name, chosen=None):
        """ENTRY (of the folder CHOSEN was taken in; default: the one PANE shows) renamed NEW_NAME."""
        chosen = chosen or Chosen.of(pane, [entry])

        def renamed(_result, error):
            if error is not None:
                self.message.setText(printable(error))
                return False
            self.refresh_panes(pane if chosen.shown_in(pane) else None, [new_name])
            self.message.setText(tr("{name} renamed to {new}.", name=printable(entry.name), new=printable(new_name)))
            return True
        return self.root_job(chosen.root, lambda _cancelled: chosen.root.rename_entry(chosen.items[0][1], new_name),
                             renamed, tr("Renaming {name}…", name=printable(entry.name)))

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

        def made(_result, error):
            if error is not None:
                self.message.setText(printable(error))
                return False
            self.refresh_panes(pane if where.shown_in(pane) else None, [name])
            self.message.setText(tr("Folder {name} created.", name=printable(name)))
            return True
        return self.root_job(where.root, lambda _cancelled: where.root.make_folder(where.relative, name), made,
                             tr("Creating the folder {name}…", name=printable(name)))

    def new_file_dialog(self, pane=None):
        """Shift+F4: the name of a new empty file in the folder PANE (the active one) shows, then make_file."""
        pane = pane or self.active
        if pane.root is None:
            return False
        where = Chosen.of(pane, [])
        name, ok = QInputDialog.getText(self, tr("New file"), shown_as_text(tr(
            "Name of the new file in {folder}:", folder=printable(where.folder_text))))
        return bool(ok and name) and self.make_file(pane, name, where)

    def make_file(self, pane, name, where=None, edit=True):
        """An empty file NAME in the folder WHERE (a Chosen, taken before the name was asked; default: the one PANE
        shows): Root.make_file (O_EXCL, never through a link; 0600 where an agent may have written), selected once it
        is listed, and (EDIT) opened in the editor."""
        where = where or Chosen.of(pane, [])

        def made(_result, error):
            if error is not None:
                self.message.setText(printable(error))
                return False
            self.refresh_panes(pane if where.shown_in(pane) else None, [name])
            self.message.setText(tr("File {name} created.", name=printable(name)))
            if edit:
                relative = f"{where.relative}/{name}" if where.relative else name
                self.open_editor(where.root, relative, os.path.join(where.folder_text, name), "text")
            return True
        return self.root_job(where.root, lambda _cancelled: where.root.make_file(where.relative, name,
                                                                                untrusted=where.untrusted),
                             made, tr("Creating the file {name}…", name=printable(name)))

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
        """MODE for the items of CHOSEN (no set-ID or sticky bits where an agent may have written), through
        root_job. Typed for files and folders at once (as one octal mode: 644), a folder also gets the execute (search)
        bit wherever it may be read - chmod's X; the mode was set as it was, and nobody could enter the folders."""
        kinds = chosen.kinds if len(chosen.kinds) == len(chosen.items) else [None] * len(chosen.items)
        mixed = "dir" in kinds and any(kind != "dir" for kind in kinds)
        searchable = mode | (mode & 0o444) >> 2

        def work(cancelled):
            changed, problems = 0, []
            for (name, path), kind in zip(chosen.items, kinds):
                if cancelled():
                    problems.append(tr("Cancelled."))
                    break
                try:
                    chosen.root.chmod_entry(path, searchable if mixed and kind == "dir" else mode,
                                            special=not chosen.untrusted)
                    changed += 1
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return changed, problems

        def changed(result, error):
            count, problems = result if error is None else (0, [printable(error)])
            self.refresh_panes()
            self.last_report = [tr("Permissions of {count} item(s) changed.", count=count)] + problems
            if mixed and searchable != mode:
                self.last_report.append(tr("The folders got the execute (search) bit wherever they may be read "
                                           "({mode}): a mode for files and folders at once.", mode=f"{searchable:o}"))
            self.message.setText("\n".join(self.last_report))
            return count == len(chosen.items)
        return self.root_job(chosen.root, work, changed, tr("Changing the permissions of {count} item(s)…",
                                                            count=len(chosen.items)))

    # -- owners --

    def chown_dialog(self, pane=None, entries=None):
        pane = pane or self.active
        entries = pane.chosen() if entries is None else entries
        if not entries or pane.root is None:
            pane.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        if pane.remote():  # (said at once: the dialog offers this machine's user and groups, and was refused after OK)
            self.message.setText(tr("Owners are not changed on a remote system from here."))
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
        started = self.run_job([root], work, lambda result, error: self.owned(result, error, sandbox), text)
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
        if pane.side != "sandbox" or pane.root is None or pane.remote():
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
        started = self.run_job([root], work, lambda result, error: self.owned(result, error, False), text)
        return self.follow(started, None, text)
