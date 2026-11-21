"""The one chooser of a folder or file for a path that is used on a host: this machine's file dialog for this machine
(state.choose_folder / choose_file), and for a host of `sandbox-ai hosts` a dialog that browses that host over SSH
and SFTP (remote.RemoteRoot, the Files page's own layer) - the path it gives back is that host's, as it is there:
never resolved, checked or opened on this machine.

The remote dialog connects and lists in the background (a host that does not answer never holds up the window),
starts in the field's current folder or else the remote HOME, has a path bar (a path typed is taken as it is, also
when the host cannot be reached: the error says so), lists folders - and files where a file is asked for - and shows
symbolic links as links: one is never followed (remote.py walks every path with LSTAT), its target is named. An
unknown host key is shown with its fingerprints and trusted only after a yes (as on the Files page); a changed key is
refused. The folders chosen last are kept per host (state.json, "remote_folders") and offered under Recent.

New folder… (its button, the list's menu, Ctrl+Shift+N) asks a name in a small dialog of the app's own (a name not
taken yet offered, selected), checks it (folder_name_problem: one name - no "/", not "." or "..", no control
characters, at most 255 bytes, none listed there already) and makes it over SFTP in the folder listed - never a path
typed elsewhere, never through a link (remote.RemoteRoot.make_folder walks with LSTAT) - in the background; then the
folder is listed again with the new one selected, so Choose takes it. The bar's buttons are icons (widgets.nav_button:
the text stays their accessible name, the tooltip says what they do and their key)."""
from __future__ import annotations

import posixpath
import unicodedata

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QStyle,
    QVBoxLayout,
)

from . import applog, widgets
from .files import Refused, printable, printable_text
from .i18n import tr
from .widgets import background, confirm, explanation, list_menu, nav_button, plain_label, repaint_glyphs, warn

LOCAL = "local"  # this machine, as `sandbox-ai hosts` names it
# ssh and its files (tests: SSH a program that speaks SFTP on stdin and stdout, given as a list - fake_sftp_server.py)
SSH, KNOWN_HOSTS, SSH_CONFIG = "ssh", None, None
RECENT = 10  # folders kept per host
CONNECT_WAIT = 120  # s: connecting as a whole (ssh's ConnectTimeout and each request have their own)
LIST_WAIT = 90  # s: one listing
MKDIR_WAIT = 60  # s: making one folder
NAME_BYTES = 255  # the longest name of a file or folder (bytes, UTF-8): NAME_MAX of Linux, macOS and the BSDs


def alive(widget):
    """Whether WIDGET's C++ object still exists (a late result must not touch a deleted one)."""
    try:
        from shiboken6 import isValid
        return isValid(widget)
    except ImportError:  # pragma: no cover
        return True


def key_text(keys):
    """KEYS ("Ctrl+R") as this system writes them (macOS: ⌘R)."""
    return QKeySequence(keys).toString(QKeySequence.SequenceFormat.NativeText)


def is_local(host):
    return host in (None, "", LOCAL)


def find_sai(widget):
    """The SandboxAI of WIDGET's page: the first of it and its parents that has one (.sai)."""
    while widget is not None:
        sai = getattr(widget, "sai", None)
        if sai is not None:
            return sai
        try:
            widget = widget.parentWidget()
        except (AttributeError, RuntimeError):
            return None
    return None


def host_target(sai, host):
    """The remote.Target of HOST (a name of `sandbox-ai hosts` reached over SSH: its user@host, port and key), as the
    Files page connects to it. Refused (in words) for a host that is not such a host here."""
    from . import remote
    try:
        entries = sai.host_entries() if sai is not None else []
    except Exception as exc:  # noqa: BLE001 - an older sandbox-ai, a damaged hosts file: no host to reach
        applog.swallowed("remote_chooser", "host_target: the hosts could not be read", exc)
        entries = []
    entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == host), None)
    if entry is None or entry.get("kind") != "ssh" or not entry.get("ssh"):
        raise Refused(tr("{host} is no host of sandbox-ai reached over SSH here: its folders cannot be listed.",
                         host=printable(host)))
    target = remote.Target.parse(entry["ssh"], entry.get("identity") or "")
    try:
        if entry.get("port") and int(entry["port"]) != 22:
            target.port = int(entry["port"])
    except (TypeError, ValueError):
        pass
    return target


# -- the folders chosen last, per host --

def recent_folders(host):
    """The folders chosen last on HOST (newest first; remote paths, never looked at here)."""
    from . import state as state_module
    current = state_module.current()
    if current is None:
        return []
    found = current.section("remote_folders").get(str(host))
    return [p for p in found if isinstance(p, str) and p.startswith("/") and p.isprintable()][:RECENT] \
        if isinstance(found, list) else []


def remember_folder(host, folder):
    from . import state as state_module
    current = state_module.current()
    if current is None or not folder or not str(folder).startswith("/"):
        return
    kept = [folder] + [p for p in recent_folders(host) if p != folder]
    current.section("remote_folders")[str(host)] = kept[:RECENT]
    current.schedule()


# -- choosing --

def choose(parent, host, title, mode="folder", key="folder", start="", sai=None, name_filter=""):
    """A path on HOST chosen by the human ("" when cancelled): MODE "folder" or "file". This machine: its file dialog
    (starting where the last one of KEY ended). Another host: RemoteChooser on it, starting at START (the field's
    value) when that is an absolute path, else in the remote HOME. The path is that host's, as it is there."""
    if is_local(host):
        from . import state as state_module
        return state_module.choose_folder(parent, title, key) if mode == "folder" else \
            state_module.choose_file(parent, title, key, name_filter)
    sai = sai if sai is not None else find_sai(parent)
    # (a child of the window, not of the field: a form rebuilt meanwhile - the Board's inspector - deleted the field,
    # and the dialog with it, while the human was choosing)
    window = parent.window() if parent is not None else None
    dialog = RemoteChooser(window, sai, host, title, mode, start)
    path = run_dialog(dialog)
    if path:
        remember_folder(host, path if mode == "folder" else posixpath.dirname(path) or "/")
    return path


def choose_folder(parent, host, title, key="folder", start="", sai=None):
    return choose(parent, host, title, "folder", key, start, sai)


def choose_file(parent, host, title, key="file", start="", sai=None, name_filter=""):
    return choose(parent, host, title, "file", key, start, sai, name_filter)


def folder_name_problem(name, existing=()):
    """What is wrong with NAME as the name of a new folder in a folder whose entries are EXISTING ("": nothing): one
    name (no "/": it is made in the folder listed, never somewhere a path points to), not "." or "..", no control
    or invisible characters (NUL, a line break, a tab, an escape, a direction mark), at most NAME_BYTES bytes, and
    no entry of that name there already (a file or a link too)."""
    name = str(name)
    if not name.strip():
        return tr("Type a name for the new folder.")
    if "/" in name:
        return tr("A folder's name has no '/': it is made in the folder listed (go to another folder first to make it "
                  "there).")
    if name in (".", ".."):
        return tr("'.' and '..' are not names of a folder.")
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in name):
        return tr("A name with control characters (a line break, a tab, NUL) or invisible ones is not made: {name}",
                  name=printable(name))
    size = len(name.encode("utf-8", "surrogatepass"))
    if size > NAME_BYTES:
        return tr("That name is too long: at most {limit} bytes, it has {count}.", limit=NAME_BYTES, count=size)
    if name in existing:
        return tr("{name} already exists in this folder.", name=printable(name))
    return ""


def free_name(existing):
    """The name offered for a new folder: "New folder", else "New folder 2", 3, … - the first not in EXISTING."""
    base = tr("New folder")
    number = 2
    name = base
    while name in existing:
        name, number = f"{base} {number}", number + 1
    return name


class FolderNameDialog(QDialog):
    """The name of a new folder in FOLDER on HOST: a name not taken yet offered and selected (typing replaces it),
    what is wrong with the one typed said below it at once, Create only for a name that may be made."""

    def __init__(self, parent, folder, host, existing=()):
        super().__init__(parent)
        self.existing = frozenset(existing)
        self.setWindowTitle(tr("New folder"))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("Name of the new folder in {folder} on {host}:", folder=printable(folder),
                                        host=printable(host))))
        self.name = QLineEdit(free_name(self.existing))
        self.name.setAccessibleName(tr("Name of the new folder"))
        layout.addWidget(self.name)
        self.problem = plain_label(role="notice-warn", selectable=True)
        layout.addWidget(self.problem)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = self.buttons.addButton(tr("Create"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok_button.setToolTip(tr("Make the folder on the host"))
        self.ok_button.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.name.textChanged.connect(lambda _text: self.check())
        self.check()
        self.name.selectAll()
        self.name.setFocus()
        self.resize(420, 0)

    def check(self):
        problem = folder_name_problem(self.name.text(), self.existing)
        self.problem.setText(problem)
        self.problem.setVisible(bool(problem))
        self.ok_button.setEnabled(not problem)
        return not problem


def run_name_dialog(dialog):
    """The name typed in DIALOG (a FolderNameDialog; "" when cancelled). Tests replace this; with widgets.AUTO_ANSWER
    set no dialog is shown."""
    if widgets.AUTO_ANSWER is not None:
        widgets.drop_after_exec(dialog)
        return ""
    try:
        return dialog.name.text() if widgets.exec_dropped(dialog) == QDialog.DialogCode.Accepted else ""
    except RuntimeError:  # deleted with its window meanwhile: cancelled
        return ""


def run_dialog(dialog):
    """The path chosen in DIALOG ("" when cancelled). Tests replace this; with widgets.AUTO_ANSWER set no dialog is
    shown (it would wait for a human)."""
    try:
        if widgets.AUTO_ANSWER is not None:
            return ""
        return dialog.chosen() if widgets.exec_dropped(dialog) == QDialog.DialogCode.Accepted else ""
    finally:
        dialog.disconnect_root()


class RemoteChooser(QDialog):
    """A folder (or file: MODE "file") on HOST, browsed over SSH (see the module). chosen() is the remote path."""

    def __init__(self, parent, sai, host, title, mode="folder", start=""):
        super().__init__(parent)
        self.sai, self.host, self.mode = sai, host, mode
        self.root, self.folder, self.token, self.target = None, "", 0, None
        self.names = frozenset()  # every entry of the folder listed (files and links too: a new folder's name is new)
        self.select_after = ""  # the folder just made: selected once the folder is listed again
        self.busy = False
        self.closed = False  # the dialog is done: a connection made after that is ended at once
        self.pending_problem = ""  # what went wrong with the folder asked for, shown with the one listed instead
        self.start = str(start or "").strip()
        self.setWindowTitle(tr("{title} on {host}", title=title, host=printable(host)))
        layout = QVBoxLayout(self)
        layout.addWidget(explanation(tr("A folder on {host}: listed over SSH; the path chosen is {host}'s.",
                                        host=printable(host)) if mode == "folder" else
                                     tr("A file on {host}: listed over SSH; the path chosen is {host}'s.",
                                        host=printable(host))))
        bar = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setAccessibleName(tr("Path on {host}", host=printable(host)))
        self.path.setPlaceholderText(tr("an absolute path on {host}", host=printable(host)))
        self.path.returnPressed.connect(lambda: self.go(self.path.text()))
        # Icons (the text: their accessible name and their entry on the list's menu); the tooltip names the key.
        self.go_button = nav_button(tr("Go"), tr("List the folder typed (Enter in the path bar)"),
                                    lambda: self.go(self.path.text()), "go")
        self.up_button = nav_button(tr("Up"), tr("The folder above ({shortcut})", shortcut=key_text("Alt+Up")),
                                    self.up, "up")
        self.home_button = nav_button(tr("Home"), tr("The home folder on {host} ({shortcut})", host=printable(host),
                                                     shortcut=key_text("Alt+Home")), self.home, "home")
        self.refresh_button = nav_button(tr("Refresh"), tr("List the folder again ({shortcut})",
                                                           shortcut=key_text("Ctrl+R")),
                                         lambda: self.go(self.folder), "refresh")
        self.new_folder_button = nav_button(tr("New folder…"), tr("A new folder in the folder listed ({shortcut})",
                                                                   shortcut=key_text("Ctrl+Shift+N")),
                                            self.new_folder, "folder-new")
        self.new_folder_button.setEnabled(False)  # (once a folder is listed)
        bar.addWidget(self.path, 1)
        for widget in (self.go_button, self.up_button, self.home_button, self.refresh_button,
                       self.new_folder_button):
            bar.addWidget(widget)
        layout.addLayout(bar)
        # (a key does what its button does, and only while the button may: not while the host is asked)
        for keys, widget in (("Alt+Up", self.up_button), ("Alt+Home", self.home_button),
                             ("Ctrl+R", self.refresh_button), ("Ctrl+Shift+N", self.new_folder_button)):
            QShortcut(QKeySequence(keys), self, activated=lambda w=widget: w.isEnabled() and w.click())
        self.recent = QComboBox()
        self.recent.setAccessibleName(tr("Folders chosen last on {host}", host=printable(host)))
        self.recent.addItem(tr("Recent folders on {host}…", host=printable(host)), "")
        for folder in recent_folders(host):
            self.recent.addItem(printable(folder), folder)
        self.recent.activated.connect(lambda index: self.go(self.recent.itemData(index)) if index > 0 else None)
        layout.addWidget(self.recent)
        self.recent.setVisible(self.recent.count() > 1)  # (in its place first: never a window of its own)
        self.list = QListWidget()
        self.list.setAccessibleName(tr("Contents of the folder"))
        self.list.itemActivated.connect(self.open_item)
        layout.addWidget(self.list, 1)
        self.status = plain_label(role="secondary", selectable=True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.choose_button = self.buttons.addButton(tr("Choose"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.choose_button.setToolTip(tr("Take the folder selected, else the path in the bar") if mode == "folder"
                                      else tr("Take the file selected, else the path in the bar"))
        self.choose_button.clicked.connect(self.take)
        self.choose_button.setAutoDefault(False)  # (Enter in the path bar lists the path typed; it chooses nothing)
        self.choose_button.setDefault(False)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        # Open is the list's own action (Enter, a double-click), on its menu with the buttons'
        list_menu(self.list, lambda: [(tr("Open"), self.entry_of(self.current()) is not None, None,
                                       lambda: self.open_item(self.current())),
                                      self.choose_button, None, self.up_button, self.home_button,
                                      self.refresh_button, None, self.new_folder_button], copy=False)
        self.resize(640, 480)
        if self.start.startswith("/"):
            self.path.setText(self.start)
        self.connect_host()

    # -- the connection --

    def say(self, text, role="secondary"):
        self.status.setText(text)
        self.status.setProperty("role", role)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def say_problem(self, text):
        self.say(text + "\n" + tr("You can type the path in the bar and choose it as it is."), "notice-warn")

    def connect_host(self):
        from . import remote
        try:
            self.target = host_target(self.sai, self.host)
        except Refused as exc:
            self.say_problem(printable_text(exc))
            return False
        target, ssh, known, config = self.target, SSH, KNOWN_HOSTS, SSH_CONFIG
        self.say(tr("Connecting to {host}…", host=printable(target.text())))
        self.set_busy(True)
        # (no owner: a connection made after the dialog went must still reach connected, which ends it)
        background(lambda: remote.RemoteRoot(target, "/", ssh=ssh, known_hosts=known, config=config),
                   timeout=CONNECT_WAIT, done=lambda root, error: self.connected(root, error))
        return True

    def connected(self, root, error):
        from . import remote
        if self.closed or not alive(self):
            if root is not None:
                background(root.close, timeout=30, done=lambda *_: None)
            return False
        self.set_busy(False)
        if isinstance(error, remote.HostKeyUnknown):
            background(self.scan_keys, self.target, owner=self, timeout=60, done=self.trust_host)
            return False
        if error is not None:
            if isinstance(error, TimeoutError):
                self.say_problem(tr("{host} did not answer.", host=printable(self.host)))
            elif isinstance(error, remote.AuthenticationNeeded):
                self.say_problem(printable_text(error) + "\n" + tr(
                    "Unlock your key first (ssh-add, or ssh {host} in a terminal), then open this again.",
                    host=printable(self.target.text())))
            else:
                self.say_problem(printable_text(error))
            if isinstance(error, remote.HostKeyChanged):
                warn(self, tr("Host key changed"), printable_text(error))
            return False
        self.root = root
        start = self.start if self.start.startswith("/") else root.home
        if self.mode == "file" and self.start.startswith("/"):
            start = posixpath.dirname(self.start.rstrip("/")) or "/"
        self.go(start, fallback=root.home)
        return True

    @staticmethod
    def scan_keys(target):
        from . import remote
        lines = remote.scan_host(target)
        return lines, remote.fingerprints(lines), KNOWN_HOSTS or remote.known_hosts_file(target, SSH_CONFIG)

    def trust_host(self, found, problem):
        """The host's keys, shown; added to known_hosts and connected again only after a yes (as the Files page)."""
        from . import remote
        if problem is not None:
            self.say_problem(printable_text(problem))
            return False
        lines, prints, known = found
        target = self.target
        if not confirm(self, tr("Trust this host?"), tr(
                "{host} is not among your known hosts. It shows these keys:\n\n{keys}\n\nCompare them with what "
                "whoever runs it tells you (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub there). Trust them and "
                "connect?", host=printable(target.text()), keys="\n".join(printable(k) for k in prints)),
                tr("Trust and connect")):
            self.say_problem(tr("Not connected: {host}'s keys were not trusted.", host=printable(target.text())))
            return False
        try:
            remote.add_known_host(lines, known, target)
        except (OSError, ValueError) as exc:
            self.say_problem(printable_text(exc))
            return False
        return self.connect_host()

    def disconnect_root(self):
        """The connection ends (in the background: ssh's end is waited for)."""
        self.closed = True
        root, self.root = self.root, None
        if root is not None:
            background(root.close, timeout=30, done=lambda *_: None)

    def done(self, result):  # noqa: D401 - QDialog.done
        super().done(result)
        self.disconnect_root()

    def changeEvent(self, event):  # noqa: N802
        """Another look (theme): the bar's icons in its text colour."""
        super().changeEvent(event)
        repaint_glyphs(self, event)

    def set_busy(self, busy):
        self.busy = bool(busy)
        for widget in (self.go_button, self.up_button, self.home_button, self.refresh_button):
            widget.setEnabled(not busy)
        self.new_folder_button.setEnabled(not busy and self.root is not None and bool(self.folder))

    # -- browsing --

    def go(self, path, fallback=None):
        """List the folder PATH (absolute, on the host) in the background; FALLBACK: where to go when it cannot be."""
        path = str(path or "").strip()
        if not path.startswith("/"):
            self.say_problem(tr("Not an absolute path: {path}", path=printable(path)))
            return False
        path = posixpath.normpath(path) if not path.startswith("//") else "/" + path.lstrip("/")
        if self.root is None:  # not connected: the path typed stays in the bar, to be chosen as it is
            self.path.setText(path)
            return False
        self.token += 1
        token, root = self.token, self.root
        self.set_busy(True)
        self.say(tr("Reading {path}…", path=printable(path)))
        background(lambda: root.list(path.lstrip("/")), owner=self, timeout=LIST_WAIT,
                   done=lambda listing, error: self.listed(token, path, listing, error, fallback))
        return True

    def listed(self, token, path, listing, error, fallback=None):
        if token != self.token:
            return False
        made, self.select_after = self.select_after, ""
        self.set_busy(False)
        if error is not None:
            if fallback and fallback != path:
                self.say_problem(printable_text(error))
                self.pending_problem = self.status.text()
                self.go(fallback)
                return False
            self.say_problem(tr("{host} did not answer.", host=printable(self.host))
                             if isinstance(error, TimeoutError) else printable_text(error))
            return False
        self.folder = path
        self.names = frozenset(entry.name for entry in listing)
        self.set_busy(False)  # (New folder: now that a folder is listed)
        self.path.setText(path)
        self.list.clear()
        style = self.style()
        icons = {"dir": style.standardIcon(QStyle.StandardPixmap.SP_DirIcon),
                 "link": style.standardIcon(QStyle.StandardPixmap.SP_FileLinkIcon),
                 "file": style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)}
        shown = 0
        for entry in listing:
            if entry.kind != "dir" and entry.kind != "link" and (self.mode != "file" or entry.kind != "file"):
                continue
            text = printable(entry.name) + ("/" if entry.kind == "dir" else "")
            if entry.kind == "link":
                text = tr("{name} → {target} (a link: not followed)", name=printable(entry.name),
                          target=printable(entry.target or "?"))
            item = QListWidgetItem(icons.get(entry.kind, icons["file"]), text)
            item.setData(Qt.ItemDataRole.UserRole, (entry.kind, entry.name, entry.target or ""))
            self.list.addItem(item)
            shown += 1
            if made and entry.kind == "dir" and entry.name == made:
                self.list.setCurrentItem(item)  # the folder just made: Choose takes it
        problem, self.pending_problem = self.pending_problem, ""
        if made and self.current() is not None:
            self.say(tr("Folder {name} created in {path} on {host}.", name=printable(made), path=printable(path),
                        host=printable(self.host)))
        elif problem:
            self.say(problem, "notice-warn")
        elif getattr(listing, "truncated", False):
            self.say(tr("Only the first {count} entries are listed.", count=shown), "notice-warn")
        else:
            self.say(tr("{path} on {host}", path=printable(path), host=printable(self.host)) if shown else
                     tr("{path} on {host}: no folders in it", path=printable(path), host=printable(self.host))
                     if self.mode == "folder" else
                     tr("{path} on {host}: empty", path=printable(path), host=printable(self.host)))
        return True

    def up(self):
        base = self.folder or self.path.text().strip()
        if base.startswith("/") and base != "/":
            self.go(posixpath.dirname(base.rstrip("/")) or "/")

    def home(self):
        if self.root is not None:
            self.go(self.root.home)

    def new_folder(self):
        """New folder…: its name asked (FolderNameDialog), then make_folder."""
        if self.root is None or not self.folder or self.busy:
            return self.make_folder("")
        name = run_name_dialog(FolderNameDialog(self, self.folder, self.host, self.names))
        return bool(name) and self.make_folder(name)

    def make_folder(self, name):
        """A folder NAME in the folder listed, made over SFTP in the background (never through a link: RemoteRoot
        walks with LSTAT; never a path: one name); then listed again with it selected. A name that may not be made,
        an error of the host or no answer: said in the status line, nothing waits."""
        if self.root is None or not self.folder:
            self.say(tr("Not connected to {host}: a folder is made once one of its folders is listed.",
                        host=printable(self.host)), "notice-warn")
            return False
        if self.busy:
            return False
        name = str(name)
        problem = folder_name_problem(name, self.names)
        if problem:
            self.say(problem, "notice-warn")
            return False
        root, folder = self.root, self.folder
        relative = folder.lstrip("/")
        self.token += 1
        token = self.token
        self.set_busy(True)
        self.say(tr("Creating the folder {name} in {path}…", name=printable(name), path=printable(folder)))
        background(lambda: root.make_folder(relative, name), owner=self, timeout=MKDIR_WAIT, work=True,
                   what=f"remote_chooser: a new folder in {folder}",
                   done=lambda _result, error: self.made(token, folder, name, error))
        return True

    def made(self, token, folder, name, error):
        if token != self.token or not alive(self):
            return False
        self.set_busy(False)
        if error is not None:
            self.say(tr("{host} did not answer.", host=printable(self.host)) if isinstance(error, TimeoutError)
                     else printable_text(error), "notice-warn")
            return False
        self.select_after = name
        return self.go(folder)

    def entry_of(self, item):
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return data if isinstance(data, tuple) and len(data) == 3 else None

    def open_item(self, item):
        """A folder: listed; a link: named, never followed; a file (choosing a file): taken."""
        entry = self.entry_of(item)
        if entry is None:
            return False
        kind, name, target = entry
        if kind == "link":
            self.say(tr("{name} is a symbolic link to {target}; it is not followed. Type the path to go there.",
                        name=printable(name), target=printable(target or "?")), "notice-warn")
            return False
        path = posixpath.join(self.folder or "/", name)
        if kind == "dir":
            return self.go(path)
        if self.mode == "file":
            self.path.setText(path)
            return self.take()
        return False

    def current(self):
        """The item selected (None: none)."""
        return self.list.currentItem() if self.list.selectedItems() else None

    def chosen(self):
        """The path chosen: the entry selected that fits (a folder, or a file when one is asked for), else the path
        in the bar (typed, or the folder listed). "" for none."""
        entry = self.entry_of(self.current())
        if entry is not None and self.folder and entry[0] == ("dir" if self.mode == "folder" else "file"):
            return posixpath.join(self.folder, entry[1])
        typed = self.path.text().strip()
        if not typed.startswith("/") or any(c in typed for c in "\0\n\r"):
            return ""
        if self.mode == "file" and typed.rstrip("/") == (self.folder or "").rstrip("/") and self.folder:
            return ""  # the folder listed is no file
        return typed

    def take(self):
        if self.chosen():
            self.accept()
            return True
        self.say_problem(tr("Choose a folder, or type an absolute path.") if self.mode == "folder" else
                         tr("Choose a file, or type its absolute path."))
        return False
