"""The Files page: a two-pane file manager. Left: the workspace or HOME of a sandbox or session (or another
folder); right: this machine, starting at HOME. Files go back and forth between the panes (copy, move) and
are deleted, renamed, given new folders, permissions and owners in either.

Everything goes through files.py: relative to open directory descriptors, never through a symbolic link (one
in the middle of a path or as the entry itself), never above the root, within budgets, cancellable, and a copy
cut short leaves nothing behind. Below a sandbox's folder - and in a folder of the right pane that lies in one -
an agent may have written everything, so what is copied out of it is never executable, files that tools run are
named, links are not followed even to open a folder, and no set-ID or sticky bit is set. Replacing and deleting
ask first. Copies, moves, deletes and owner changes run in a thread with progress and Cancel.

Keys (in a list): Tab the other pane, Enter open, Backspace up, F5 copy, F6 move, F7 new folder, F8 or Delete
delete, F2 rename, Ctrl+A select all, Ctrl+R refresh.
"""
from __future__ import annotations

import grp
import os
import pwd
import stat
import time

from PySide6.QtCore import QEvent, QEventLoop, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QSplitter, QVBoxLayout, QWidget,
)

from . import files
from .files import printable
from .i18n import N_, tr
from .state import choose_files, choose_folder
from .widgets import (
    Task, ask_first, background, button, confirm, fill, flow, plain_label, row_key, select_keys, selected_rows,
    set_enabled, table, warn, shown_as_text,
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
# A folder is listed in a thread (a slow or hung file system never holds up the window); the pane waits up to
# LIST_WAIT ms for it, the window answering meanwhile, so a fast folder shows at once - a slow one fills in later.
LIST_WAIT = 1500
LIST_TIMEOUT = 120  # s: a listing that never comes back is given up (the pane says so)


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


def mode_text(mode):
    return f"{stat.filemode(mode)} {stat.S_IMODE(mode):04o}" if mode else ""


def nav_button(text, tooltip, slot):
    """A button that goes somewhere (Home, Up, Refresh): an action, never a toggle, and it takes no focus from a
    click (a focused button looks pressed in some themes, as if Home stayed on); Tab still reaches it. Where the
    pane is shows in its path bar."""
    widget = button(text, tooltip, slot)
    widget.setCheckable(False)
    widget.setAutoDefault(False)
    widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    return widget


class Pane(QWidget):
    """One side: a folder below a root, its list and its path bar. SIDE "sandbox" (roots to choose from) or
    "host" (this machine, Root("/") walked down to the folder shown)."""

    def __init__(self, page, side):
        super().__init__()
        self.page, self.side = page, side
        self.root, self.relative, self.entries = None, "", []
        self.list_token = 0  # the newest listing: one that comes back after another began is dropped
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
        self.table = table([tr(column) for column in COLUMNS], multi=True, stretch=0,  # the Name takes the room
                           compact=(1, 2, 3, 5, 6))
        self.table.setAccessibleName(tr("Files of the sandbox") if side == "sandbox" else tr("Files of this machine"))
        self.table.setTabKeyNavigation(False)
        self.table.cellDoubleClicked.connect(self.open_row)
        self.table.installEventFilter(page)
        layout.addWidget(self.table, 1)
        self.note = plain_label(role="secondary")
        self.note.hide()
        layout.addWidget(self.note)

    # -- where --

    def path_text(self):
        if self.root is None:
            return ""
        if self.side == "host":
            return "/" + self.relative
        return self.root.path + ("/" + self.relative if self.relative else "")

    def untrusted(self):
        """True if an agent may have written what is shown: a sandbox root, or a host folder in one."""
        return self.side == "sandbox" or self.page.below_sandbox(self.path_text())

    def go(self, relative):
        relative = "/".join(files.parts(relative))
        if relative != self.relative:
            self.table.clearSelection()  # names selected in another folder mean other files here
        self.relative = relative
        return self.list()

    def up(self):
        if self.root is not None:
            self.go("/".join(files.parts(self.relative)[:-1]))

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
        self.root, self.relative = None, ""
        self.table.clearSelection()
        if path:
            try:
                self.root = files.Root(path)
            except OSError as exc:
                self.page.message.setText(tr("Cannot open {path}: {error}", path=printable(path),
                                             error=printable(exc)))
        self.list()

    # -- the list --

    def list(self):
        """List the folder shown, in a thread; True when it was listed, False if it could not be (the root's own
        folder, or HOME, is shown then), None while a slow one is still on its way (shown when it comes)."""
        self.list_token += 1
        token = self.list_token
        self.entries = []
        if self.root is None:
            self.path.setText("")
            fill(self.table, [])
            return False
        root, relative = self.root, self.relative
        fallback = "" if self.side == "sandbox" else host_relative(os.path.realpath(os.path.expanduser("~")))

        def work():
            try:
                return root.list(relative), relative, None
            except (OSError, ValueError) as exc:
                try:
                    return root.list(fallback), fallback, exc
                except (OSError, ValueError):
                    return [], fallback, exc
        outcome, waiting = {}, []

        def done(result, error):
            outcome["listed"] = self.listed(token, root, result, error)
            if waiting:  # only while list() still waits for it (a late answer never touches the loop)
                waiting[0].quit()
        background(work, timeout=LIST_TIMEOUT, done=done)
        if "listed" not in outcome:
            loop = QEventLoop()
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            waiting.append(loop)
            timer.start(LIST_WAIT)
            loop.exec()
            waiting.clear()
            timer.stop()
        if "listed" not in outcome:
            self.page.message.setText(tr("Listing {path}…", path=printable(self.path_text())))
            return None
        return outcome["listed"]

    def listed(self, token, root, result, error):
        """Show what the listing of TOKEN found (dropped when another began since, or another root was chosen)."""
        if token != self.list_token or root is not self.root:
            return None
        if error is not None:  # the thread itself failed (or never came back)
            self.page.message.setText(printable(error))
            self.entries = []
            fill(self.table, [])
            return False
        self.entries, relative, problem = result
        listed = problem is None
        if not listed:
            self.page.message.setText(printable(problem))
            self.relative = relative
            self.table.clearSelection()
        self.path.setText(printable(self.path_text()))
        ranges = self.page.ranges
        rows, colors = [], []
        for entry in self.entries:
            rows.append([printable(entry.name), tr(KINDS[entry.kind]),
                         files.size_text(entry.size) if entry.kind == "file" else "",
                         time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime)),
                         "→ " + printable(entry.target) if entry.kind == "link" else "",
                         owner_text(entry, ranges), mode_text(entry.mode)])
            colors.append("warn" if entry.kind == "link" or entry.mode & files.SPECIAL_BITS and self.untrusted()
                          else "muted" if entry.kind == "other" else None)
        fill(self.table, rows, colors, keys=[entry.name for entry in self.entries])
        if self.side == "host":
            inside = self.untrusted()
            self.note.setText(tr("This folder belongs to a sandbox: what is copied from here is treated like the "
                                 "agent's files (never executable, links not followed).") if inside else "")
            self.note.setVisible(inside)
        return listed

    def path_of(self, entry):
        return f"{self.relative}/{entry.name}" if self.relative else entry.name

    def entry_at(self, row):
        """The entry shown in the visual ROW (found by its name: the table may be sorted), or None."""
        name = row_key(self.table, row)
        return next((entry for entry in self.entries if entry.name == name), None)

    def chosen(self):
        return [entry for entry in (self.entry_at(row) for row in selected_rows(self.table)) if entry is not None]

    def current(self):
        """The entry of the current row, or the first one selected."""
        entry = self.entry_at(self.table.currentRow()) if self.table.currentRow() >= 0 else None
        chosen = self.chosen()
        return entry if entry is not None and (not chosen or entry in chosen) else (chosen[0] if chosen else None)

    def open_row(self, row, _column=0):
        entry = self.entry_at(row)
        if entry is None:
            return
        if entry.kind == "dir":
            self.go(self.path_of(entry))
        elif entry.kind == "link":
            if self.untrusted():
                self.page.message.setText(tr("{name} is a symbolic link to {target}; it is not followed.",
                                             name=printable(entry.name), target=printable(entry.target)))
                return
            real = os.path.realpath(os.path.join(self.path_text(), entry.name))
            if os.path.isdir(real) and not self.page.below_sandbox(real):
                self.go(host_relative(real))
            else:
                self.page.message.setText(tr("{name} is a symbolic link to {target}; it is not followed.",
                                             name=printable(entry.name), target=printable(entry.target)))

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

    def go(self, relative):
        return self.left.go(relative)

    def up(self):
        self.left.up()

    def list(self):
        return self.left.list()

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
        elif event.type() == QEvent.Type.KeyPress and self.key(pane, event.key(), event.modifiers()):
            return True
        return False

    def key(self, pane, key, modifiers=Qt.KeyboardModifier.NoModifier):
        """Do what KEY means in PANE's list; True if it meant something."""
        Key = Qt.Key
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        self.activate(pane)
        if self.task.running and key in (Key.Key_F2, Key.Key_F5, Key.Key_F6, Key.Key_F7, Key.Key_F8, Key.Key_Delete):
            # Its buttons are greyed out meanwhile: so are its keys (a rename or a new folder in the middle of a copy
            # or move would change what it works on).
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
            return True
        if key in (Key.Key_Tab, Key.Key_Backtab) and not control:
            self.other(pane).table.setFocus(Qt.FocusReason.TabFocusReason)
            self.activate(self.other(pane))
        elif key in (Key.Key_Return, Key.Key_Enter):
            pane.open_row(pane.table.currentRow())
        elif key == Key.Key_Backspace:
            pane.up()
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
        elif key == Key.Key_A and control:
            pane.table.selectAll()
        elif key == Key.Key_R and control:
            pane.list()
        else:
            return False
        return True

    def refresh_panes(self):
        for pane in (self.left, self.right):
            pane.list()

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
            self.message.setText(tr("Select files or folders first."))
            return False
        return self.transfer(source, entries, self.other(source), move)

    def transfer(self, source, entries, target, move=False):
        if source.root is None or target.root is None:
            self.message.setText(tr("Choose a folder in both panes first."))
            return False
        target_dir, names = target.relative, [entry.name for entry in entries]
        overwrite = self.ask_replace(names, source.path_text(), target.root, target_dir, target.path_text())
        if overwrite is None:
            return False
        src_root, dst_root, untrusted = source.root, target.root, source.untrusted()
        items = [(entry.name, source.path_of(entry)) for entry in entries]
        progress = files.Progress()

        def work(cancelled):
            done, problems = [], []
            for name, path in items:
                try:
                    if move:
                        done.append(files.move_entry(src_root, path, dst_root, target_dir, overwrite=overwrite,
                                                     untrusted=untrusted, cancelled=cancelled, progress=progress))
                    else:
                        done.append(files.copy_entry(src_root, path, dst_root, target_dir, overwrite=overwrite,
                                                     untrusted=untrusted, cancelled=cancelled, progress=progress))
                except files.Cancelled as exc:
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems
        out = untrusted and not target.untrusted()
        text = (tr("Moving {count} item(s) to {target}…", count=len(items), target=target.path_text()) if move
                else tr("Copying {count} item(s) to {target}…", count=len(items), target=target.path_text()))
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
        root, host, relative = self.left.root, self.host(), self.left.relative
        if root is None:
            return False
        items = []
        for path in paths:
            path = os.path.abspath(os.fspath(path))
            items.append((os.path.basename(path), host_relative(os.path.join(
                os.path.realpath(os.path.dirname(path)), os.path.basename(path)))))
        overwrite = self.ask_replace([name for name, _ in items], tr("this machine"), root, relative,
                                     self.left.path_text())
        if overwrite is None:
            return False
        progress = files.Progress()

        def work(cancelled):
            done, problems = [], []
            for name, path in items:
                try:
                    done.append(files.copy_entry(host, path, root, relative, overwrite=overwrite, untrusted=False,
                                                 cancelled=cancelled, progress=progress))
                except files.Cancelled as exc:
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems
        text = tr("Copying into the sandbox's folder…")
        started = self.task.thread(work, lambda result, error: self.transferred(result, error, False, False), text,
                                   cancellable=True)
        return self.follow(started, progress, text)

    def download_dialog(self):
        entries = self.left.chosen()
        if not entries or self.left.root is None:
            self.message.setText(tr("Select files or folders first."))
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

    # -- delete --

    def delete_selected(self, pane=None):
        pane = pane or self.active
        entries = pane.chosen()
        if not entries or pane.root is None:
            self.message.setText(tr("Select files or folders first."))
            return False
        if not ask_first(self, "files-delete", tr("Delete?"),
                         tr("Delete {count} item(s) in {folder}? Folders are deleted with everything in them; links "
                            "are removed, not what they point to. This cannot be undone.", count=len(entries),
                            folder=printable(pane.path_text())),
                         tr("Delete"), details="\n".join(printable(entry.name) for entry in entries)):
            return False
        root, items = pane.root, [(entry.name, pane.path_of(entry)) for entry in entries]
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

    def rename_dialog(self, pane=None):
        pane = pane or self.active
        entry = pane.current()
        if entry is None:
            self.message.setText(tr("Select files or folders first."))
            return False
        new, ok = QInputDialog.getText(self, tr("Rename"), shown_as_text(tr("New name for {name}:",
                                                                            name=printable(entry.name))),
                                       QLineEdit.EchoMode.Normal, entry.name)
        return bool(ok and new) and self.rename(pane, entry, new)

    def rename(self, pane, entry, new_name):
        try:
            pane.root.rename_entry(pane.path_of(entry), new_name)
        except (OSError, ValueError) as exc:
            self.message.setText(printable(exc))
            return False
        self.refresh_panes()
        pane.select([new_name])
        self.message.setText(tr("{name} renamed to {new}.", name=printable(entry.name), new=printable(new_name)))
        return True

    def mkdir_dialog(self, pane=None):
        pane = pane or self.active
        if pane.root is None:
            return False
        name, ok = QInputDialog.getText(self, tr("New folder"), shown_as_text(tr(
            "Name of the new folder in {folder}:", folder=printable(pane.path_text()))))
        return bool(ok and name) and self.make_folder(pane, name)

    def make_folder(self, pane, name):
        try:
            pane.root.make_folder(pane.relative, name)
        except (OSError, ValueError) as exc:
            self.message.setText(printable(exc))
            return False
        self.refresh_panes()
        pane.select([name])
        self.message.setText(tr("Folder {name} created.", name=printable(name)))
        return True

    def chmod_dialog(self, pane=None):
        pane = pane or self.active
        entries = pane.chosen()
        if not entries:
            self.message.setText(tr("Select files or folders first."))
            return False
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
        return self.chmod(pane, entries, mode)

    def chmod(self, pane, entries, mode):
        changed, problems = 0, []
        special = not pane.untrusted()
        for entry in entries:
            try:
                pane.root.chmod_entry(pane.path_of(entry), mode, special=special)
                changed += 1
            except (OSError, ValueError) as exc:
                problems.append(f"{printable(entry.name)}: {printable(exc)}")
        self.refresh_panes()
        self.last_report = [tr("Permissions of {count} item(s) changed.", count=changed)] + problems
        self.message.setText("\n".join(self.last_report))
        return changed == len(entries)

    # -- owners --

    def chown_dialog(self, pane=None):
        pane = pane or self.active
        entries = pane.chosen()
        if not entries:
            self.message.setText(tr("Select files or folders first."))
            return False
        dialog = OwnerDialog(self, len(entries), any(entry.kind == "dir" for entry in entries))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        uid, gid, recursive = dialog.values()
        if uid == -1 and gid == -1:
            return False
        return self.chown(pane, entries, uid, gid, recursive)

    def chown(self, pane, entries, uid=-1, gid=-1, recursive=False):
        """Give ENTRIES of PANE to UID and GID (yours, -1: unchanged), in a thread."""
        root, items = pane.root, [(entry.name, pane.path_of(entry)) for entry in entries]
        progress, sandbox = files.Progress(), pane.side == "sandbox"

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
            self.message.setText(tr("Select files or folders first."))
            return False
        if not confirm(self, tr("Take ownership"),
                       tr("Make you the owner of {count} item(s) in {folder} and of everything in them? What the "
                          "sandbox wrote as another user (one of your subordinate UIDs) becomes yours again "
                          "(podman unshare chown); links are changed themselves, never followed.",
                          count=len(entries), folder=printable(pane.path_text())),
                       tr("Take ownership"), details="\n".join(printable(entry.name) for entry in entries)):
            return False
        root, items = pane.root, [(entry.name, pane.path_of(entry)) for entry in entries]

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
