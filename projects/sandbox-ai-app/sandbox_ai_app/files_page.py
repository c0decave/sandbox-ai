"""The Files page: a two-pane file manager. Left: the workspace or HOME of a sandbox or session (or another
folder); right: this machine, starting at HOME. Files go back and forth between the panes (copy, move) and
are deleted, renamed, given new folders, permissions and owners in either.

Either pane can list somewhere else (Pane.mode: "sandbox", "host" - this machine, the human's own - or "remote"): its
chooser offers, after the left pane's sandbox folders, This machine (LOCAL: the page's one Root("/"), the folder shown
last there), sandbox-ai's hosts by name (HOST_PREFIX + name: connected when chosen - FilesPage.connect_host, the
connection of the same target reused, else connect_remote with its question about an unknown key; the pane stays where
it was until it is connected) and the remote systems connected with SSH… (beside both choosers; the same choices under
"Show in this pane" of the context menu). The left chooser also lists the folders of the sandboxes on each of
sandbox-ai's hosts (HOST_FOLDER: read with one call per host in a thread, FilesPage.read_host_folders - a host that does
not answer in time is marked, never waited for), opened over that host's connection in that folder; a heading divides
each section (this machine's sandboxes, the hosts, each host's sandboxes, the remote systems) once there is more than
this machine, and "Sandboxes of" (FilesPage.host_box) offers one host's alone - the Sandboxes page's host, followed
as the other pages follow it (ui.FOLLOWERS); the hosts file is followed too (follow_hosts). Both panes may show
remote systems at once - one connection shared when they
show the same - so copies and moves go every way through the same transfer_items: this machine -> host, host -> this
machine, host -> host (remote.relay: fetched into a staging folder of this machine, then sent on; the progress says
"through this machine"); a move within one connection is a rename there. Into one of sandbox-ai's hosts from anywhere
else only after a question that names what goes there (sandboxes run there; their agents may read it).

Everything goes through files.py: relative to open directory descriptors, never through a symbolic link (one
in the middle of a path or as the entry itself), never above the root, within budgets, cancellable, and a copy
cut short leaves nothing behind. Below a sandbox's folder - and in a folder of the right pane that lies in one, by its
path or as that folder itself reached under another path (a bind mount of it: holding_folder) - an agent may have
written everything, so what is copied out of it is never executable, files that tools run are
named, links are not followed even to open a folder, and no set-ID or sticky bit is set. A sandbox's folder that is an
entry of a host folder, or lies inside the folder copied, is copied the same way where it goes out (files.copy_entry's
agents), and never handed to other programs by a drag or the clipboard - nor is a host folder that holds one however deep,
under any name (holds_agents: also one moved there, or mounted there). A sandbox's folder moved or renamed on this
machine stays the agent's, also when the app is started again (its device and inode are kept in the app's state, those
not seen for KEPT_DAYS dropped: FilesPage.keep_identities). The sandboxes'
folders' real paths and identities are read in threads (with the list of folders, every listing and in the jobs), a
folder chosen is opened in one: a folder on a file system that hangs holds up no window - nor does trying again leave
one more thread waiting for good each time: a look, listing or hand-over of the same thing still on its way is waited
for, not started again, and only so many of each wait at once (LIST_STUCK, EXPORT_STUCK, LOCAL_STUCK). Replacing and
deleting ask first. Copies, moves, deletes and owner changes run in a thread with progress and Cancel.

Every folder of both panes begins with "." and "..", whatever the list is sorted by. "." is the folder shown: a double
click or Enter lists it again, a drop on it or a paste from its context menu goes into it, Properties shows the folder
itself. ".." is the folder above (double click, Enter or Backspace); at the top of a pane (a sandbox's folder, or "/")
it is greyed out and its tooltip says why - nothing above the top is ever shown. Neither can be selected (FileRows), so
no copy, move, delete, rename or drag ever takes one, and Ctrl+A selects every entry but them. Both panes list folders
first, then by name as the locale collates names (name_order: letter case ignored, "é" beside "e", NFC and NFD alike) -
and so does a click on Name, either way (folders first in both directions); a sort chosen stays when the folder is
listed again.

Recent (the arrow at the end of each path bar, Alt+Down in it) lists the folders the pane showed lately (the latest
first, each once, at most RECENT): this machine's as paths, elsewhere with the place they are in (a host's as
NAME:/path - choosing one connects again); the right pane's (this machine's and the hosts' by name) are kept in the
app's state across restarts, the others only while their place is in the chooser. Choosing one goes there through the
path bar's own checks (go_path). Below each list, the free space of the folder's file system (Root.free_space:
os.fstatvfs of the open folder), read with every listing - so after a change of folder and after every copy, move or
delete; a remote folder whose SFTP server does not tell (no statvfs@openssh.com) says it is not known.

A file opens in the app's own editor (file_editor.py: text, or a hex editor for other bytes; a Markdown file
rendered, markdown_view.py) with a double click or Enter; it is read and written through the pane's Root like
everything else and never run. The context menu (a right click) and the keys offer Open, View, Edit, the hex editor,
View as Markdown, Copy, Cut, Paste (an internal clipboard of the items, their
paths as text on the system clipboard too), copy or move to the other pane, Rename, Delete, New folder, New file (an
empty file, Root.make_file, then opened in the editor), Copy path, Copy relative path (Ctrl+Shift+C) and Copy path in
the sandbox (path_forms: relative to the folder of the running sandbox or session that holds it, and as its agent
sees it - backend.sandbox_paths, /work, /workspaces/NAME, /root; one path a line, also quoted for the shell) and
Properties (file_properties.py).

SSH… shows a remote system in its pane (remote.RemoteRoot, a Root like the others: the same code path for
listing, copies both ways, the editor and Properties): connect_remote in the background, an unknown host's keys trusted
only after a yes (trust_host), a locked key with a terminal offered; its root is the remote "/", the pane starts in the
remote HOME, its links are named and never followed, its owners shown as numbers. The files of one of sandbox-ai's
hosts (`sandbox-ai hosts`: sandboxes run there, and their agents may have written any file) count as an agent's
(RemoteRoot.agents): copied or moved to this machine they are never executable and files that tools run are named, and
no set-ID or sticky bit is set there. The box "An agent's files" below the list is that choice, for the connection: on
by default for sandbox-ai's hosts, off for any other system; turned off only after a question, kept when SSH… connects
again.

Drag and drop: between the panes and onto a folder of the same pane a drop copies, with Shift it moves - through the
same copy and move as F5 and F6 (asking before anything is replaced), whatever each pane shows (this machine, a
sandbox's folder, a remote system, a sandbox's folder on a host: two remote systems through this machine). Onto a
pane's path bar a drop goes into the folder shown; onto its chooser a menu asks where - its places and recent folders
(chooser_drop) -, the pane goes there and the drop follows once that folder is listed (Pane.pending_drop). Files dropped
onto an editor open each in an editor of its own (editor_drop). Where a drop cannot go it is not taken (the "no"
cursor) and the status line says why (DropPlan.refused); a drag that ends without a drop (Esc) says that nothing was
copied or moved. Files dropped from other programs (local files
only) are copied in - into an agent's folder, or to a remote system that is one of sandbox-ai's hosts, only after a
question that names them; a path that goes through a symbolic link in a sandbox's folder is refused, as in the path bar.
Dragged out to other programs go the host pane's files and folders - a remote system's not at all (they would have to be
fetched as the drag begins, holding up the window: they are copied to this machine first, as the status line says
while they are dragged - drag_out_note); from a sandbox's folder only plain files that are
not executable (a program dropped on would get folders with hooks or executables as they are, executable bits and all),
and those as copies made when they are dragged or copied, in a folder of the app's own (handed_out_folder: by their
path in the agent's folder, the agent could turn one into a link to a file of the human's before the other program
read it). No set-ID or sticky bit is set on a sandbox's folder itself either, also where a host folder lists it.

Keys (in a list): Tab the other pane, Enter open, Backspace up, F3 view, F4 edit (on a folder, or a link to one, both
say it is a folder: Enter opens it), Shift+F4 new file (empty, then edited), F5 copy, F6 move, F7 new folder, F8 or
Delete delete (macOS: Cmd+Backspace too, as in the Finder), F2 rename, Ctrl+C copy, Ctrl+X cut, Ctrl+V paste, Alt+Enter
properties, Ctrl+A select all, Ctrl+Shift+C copy relative paths, Ctrl+R refresh, the menu key (or Shift+F10) the
context menu. Ctrl+Shift+F (anywhere in the page; the magnifying glass beside each pane's Refresh; Search below this
folder in the context menu) searches below the folder a pane shows (file_search_view.py, file_search.py): names with
wildcards, optionally the content, in the background, never through a link, every limit said. On macOS Ctrl is the Cmd key (Qt's naming); the texts name each key as the system writes it (key_text).
"""
from __future__ import annotations

import fnmatch
import grp
import os
import posixpath
import pwd
import secrets
import shlex
import stat
import sys
import threading
import time
import unicodedata

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, QMimeData, QPoint, QTimer, QUrl, Qt
from PySide6.QtGui import (
    QColor, QCursor, QDrag, QGuiApplication, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMenu, QPushButton, QSplitter, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from shiboken6 import isValid

from . import applog
from . import files, state as app_state, widgets
from .file_editor import key_text
from .files import printable
from .host_choice import follow_hosts, follow_note, valid_host
from .i18n import N_, language, tr
from .state import choose_files, choose_folder
from .widgets import (
    ModelTable, RowModel, Task, ask_first, background, button, confirm, exec_dropped, explanation, fill, find_row, flow,
    nav_button, plain_label, repaint_glyphs, row_key, safe_tooltip, select_keys, selected_rows, set_enabled, table, warn,
    shown_as_text,
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
# A pane's listings still on their way after LIST_SLOW s (a file system that hangs: they may never end): past LIST_STUCK
# of them no other folder is listed until one has come (Pane.not_listed) - each folder of a hung mount typed or chosen
# left one more thread waiting for good. (This machine's only: a remote system's request without an answer ends its
# connection, and every listing with it.)
LIST_STUCK = 4
LIST_SLOW = 1.5
# A drop onto a chooser goes where a place or recent folder is chosen after it, once the pane shows that folder - not
# later than PENDING_DROP_WAIT s after it was chosen (a host that connects slowly: the drop is not made unasked later).
PENDING_DROP_WAIT = 60
# Files dropped onto an editor open each in an editor of its own: at most so many at once.
EDITOR_DROP_LIMIT = 10
DRAG_EVENTS = (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.DragLeave, QEvent.Type.Drop)
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
# Seconds a host of sandbox-ai's has to list its sandboxes' folders (FilesPage.read_host_folders).
HOST_FOLDERS_WAIT = 20
# Where a pane lists besides a sandbox's folders (the data of its chooser's entries; never a path beginning with "/":
# that is a sandbox's folder of this machine): this machine, one of sandbox-ai's hosts by name (connected
# over SSH when chosen: FilesPage.connect_host), or a remote system connected with SSH… (its RemoteRoot.key, ssh://...).
LOCAL = "local:"
HOST_PREFIX = "host:"
# A folder of a sandbox or session on one of sandbox-ai's hosts (the left chooser, FilesPage.host_sections): on:NAME:/path,
# opened over that host's connection (connect_host) starting in that folder - read with one call per host
# (backend.host_file_roots: `sandbox-ai list --folders --json` there), never a path of this machine (folder_sources).
HOST_FOLDER = "on:"
LOCAL_HOST = "local"  # this machine as the page's host filter names it (host_choice.LOCAL: whose sandboxes are offered)
# The chooser's headings (This machine's sandboxes, Hosts, a host's sandboxes, Remote systems): items of their own,
# neither enabled nor selectable, marked with this role (the context menu shows them as its sections).
HEADING_ROLE = Qt.ItemDataRole.UserRole + 1


def is_place(data):
    """Whether the chooser's DATA is a place (this machine, a host, a remote system) rather than a sandbox's folder."""
    return isinstance(data, str) and (data == LOCAL or data.startswith(HOST_PREFIX) or data.startswith("ssh://"))


def host_folder(name, path):
    """The chooser's data of the folder PATH of a sandbox on sandbox-ai's host NAME."""
    return f"{HOST_FOLDER}{name}:{path}"


def host_folder_parts(data):
    """(host name, absolute path) of a chooser's DATA made by host_folder, else None."""
    if not isinstance(data, str) or not data.startswith(HOST_FOLDER):
        return None
    name, separator, path = data[len(HOST_FOLDER):].partition(":")
    return (name, path) if separator and name and path.startswith("/") else None


def host_of(data):
    """Whose the chooser's entry DATA is, as the page's host filter names hosts: LOCAL_HOST for this machine and its
    sandboxes' folders, a host's name for sandbox-ai's host and the folders of its sandboxes, None for another remote
    system (SSH…) or nothing."""
    if not isinstance(data, str):
        return None
    if data == LOCAL or data.startswith("/"):
        return LOCAL_HOST
    if data.startswith(HOST_PREFIX):
        return data[len(HOST_PREFIX):]
    folder = host_folder_parts(data)
    return folder[0] if folder is not None else None


# macOS's file systems ignore the case of names (APFS also their Unicode normalisation; HFS+ stores NFD): /users/me/ws
# is the folder /Users/me/ws there. Whether a folder lies in a sandbox's is decided with names folded so (in the safe
# direction: on a volume that tells cases apart, a folder of another case counts as the agent's too).
FOLDED_PATHS = sys.platform == "darwin"
# macOS: Cmd+Backspace deletes what is selected, as in the Finder (asked first, as F8 and Delete; it went up a folder,
# as Backspace) - Qt names the Cmd key Ctrl there.
MACOS = sys.platform == "darwin"


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
    """{(st_dev, st_ino): folder} of FOLDERS (the sandboxes' folders that are there) - in a thread; each read in one of
    its own and waited for RESOLVE_WAIT at most (looked_up: on a file system that hangs, the identity read last)."""
    found = {}
    for folder in folders:
        if not str(folder).startswith("/"):  # (a remote system's key in the chooser, ssh://...: no folder here)
            continue
        identity = looked_up(("identity", str(folder)), lambda folder=folder: folder_identity(folder), RESOLVE_WAIT)
        if identity is not None:
            found.setdefault(identity, folder)
    return found


# The identities of the sandboxes' folders seen (FilesPage.keep_identities) are kept in the app's state across restarts
# (a sandbox's folder moved or renamed on this machine stays the agent's): [st_dev, st_ino, folder, day last seen] each;
# one not seen for KEPT_DAYS (the folder is gone - its inode may be a folder of the human's one day - or was not visited
# since) is dropped, and at most KEPT_IDENTITIES are kept (those seen last).
IDENTITIES_KEY = "files-agent-folders"
KEPT_DAYS = 90
KEPT_IDENTITIES = 200


def today():
    """The day (since 1970) - what the kept identities' days count in."""
    return int(time.time() // 86400)


def kept_identities():
    """({(st_dev, st_ino): folder}, {(st_dev, st_ino): day seen}) kept in the app's state (IDENTITIES_KEY): those seen
    within KEPT_DAYS, at most KEPT_IDENTITIES, those seen last; anything else in it is dropped."""
    kept = app_state.current()
    values = kept.get(IDENTITIES_KEY, [], list) if kept is not None else []
    found, oldest = [], today() - KEPT_DAYS

    def number(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    for value in values:
        if isinstance(value, list) and len(value) == 4 and number(value[0]) and number(value[1]) and \
                isinstance(value[2], str) and number(value[3]) and value[3] >= oldest:
            found.append(((value[0], value[1]), value[2], value[3]))
    found.sort(key=lambda item: -item[2])  # (stable: the order kept for those of one day)
    identities, days = {}, {}
    for identity, folder, day in found:
        if identity not in identities and len(identities) < KEPT_IDENTITIES:
            identities[identity], days[identity] = folder, day
    return identities, days


RESOLVE_WAIT = 0.3  # s: a folder's real path or identity is waited for no longer (a mount that hangs)
AGAIN_WAIT = 0.05  # s: ... when another thread reads it already (it may hang: the value read last then)
_looked, _looking, _looking_lock = {}, {}, threading.Lock()  # looked_up's values read last, and those on their way


def looked_up(key, read, wait, again=AGAIN_WAIT):
    """READ()'s value for KEY (a folder's real path, its identity), read in a thread of its own and waited for WAIT s
    at most - AGAIN s when another one reads it already: on a file system that hangs (sshfs whose server went away,
    NFS) the value read last for KEY (None: none yet) until it answers, one thread for KEY at a time. Read where it was
    needed - on the GUI thread, then in the listings' and the jobs' threads - one such folder held up the whole window,
    then every listing of the host pane (never shown), every copy (never ended, Cancel could not end it) and the list
    of folders, also where they had nothing to do with it."""
    with _looking_lock:
        running = _looking.get(key)
        mine = running is None
        if mine:
            running = _looking[key] = threading.Event()
    if mine:
        def run():
            try:
                _looked[key] = read()
            except (OSError, ValueError):
                pass
            finally:
                with _looking_lock:
                    _looking.pop(key, None)
                running.set()
        threading.Thread(target=run, name="files-look-up", daemon=True).start()
    running.wait(wait if mine else again)
    return _looked.get(key)


def given_path(path):
    """PATH as given (~ expanded, made absolute, normalised) - no file system asked."""
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))


def read_real(path):
    """The real path of PATH (~ expanded): what looked_up reads in its thread."""
    return os.path.realpath(os.path.expanduser(str(path)))


def real_folder(path, wait=RESOLVE_WAIT):
    """os.path.realpath of the folder PATH (a project's, one chosen with Other folder…) for the GUI thread: as it was
    read last (resolve_folders, in a thread), else read in a thread and waited for WAIT s at most (looked_up). On a
    file system that hangs PATH as given (given_path) until it answers - later calls get that at once."""
    path = str(path)
    found = _looked.get(("real", path))
    if found is not None:
        return found
    return looked_up(("real", path), lambda: read_real(path), wait, again=0) or given_path(path)


def resolve_folders(listed, known):
    """(the sandboxes' folders, their identities): LISTED (the chooser's: real paths already) and KNOWN (projects and
    folders chosen, as given) read on the file system - in a thread (Pane.list, Pane.refresh_roots, the jobs), each
    waited for RESOLVE_WAIT at most (looked_up: one on a file system that hangs as read last, or as given)."""
    real = [looked_up(("real", str(path)), lambda path=path: read_real(path), RESOLVE_WAIT) or given_path(path)
            for path in known]
    folders = list(dict.fromkeys([*listed, *real]))
    return folders, folder_identities(folders)


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


def through_link(path, folders, identities, limit=40):
    """Whether the absolute host PATH (as typed or given: not resolved) is resolved through a symbolic link that lies
    in one of the sandboxes' FOLDERS (holding_folder, IDENTITIES: also one reached under another path) - whatever leads
    there (a link to the sandbox's folder itself, a link of the host's to a link of the agent's). LIMIT: links followed
    at most (a loop counts as through one). File-system calls: for a thread (FilesPage.through_sandbox_link)."""
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
        if holding_folder(here, folders, identities) is not None:
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


UNKNOWN = object()  # (sandbox_holding: not known - what was looked at did not answer)


# s: file-system calls the GUI thread needs (where a typed path or a link leads, whether a host folder lies in a
# sandbox's) are made in a thread and waited for no longer - on a file system that hangs (sshfs whose server went away,
# a hard NFS mount) they held up the whole window, for good
ANSWER_WAIT = 0.5
_answering, _answering_lock = {}, threading.Lock()  # answered's looks on their way, by KEY


def answered(work, wait=None, key=None):
    """(True, WORK()) - WORK (file-system calls for the GUI thread) run in a thread and waited for WAIT s
    (ANSWER_WAIT) at most; (False, None) when it is not done by then: the caller takes what is safe (as an agent's,
    refused). WORK's OSError or ValueError is raised here. KEY (what WORK looks at, with everything it is given): while
    a look of that KEY is still on its way (it did not answer in time: a file system that hangs), it is waited for
    AGAIN_WAIT, never started anew - every F5, Properties or path typed again on a hard NFS mount left one more thread
    waiting for good, and held up the window for WAIT again."""
    with _answering_lock:
        running = _answering.get(key) if key is not None else None
        mine = running is None
        if mine:
            running = ({}, threading.Event())
            if key is not None:
                _answering[key] = running
    box, finished = running
    if mine:
        def run():
            try:
                box["result"] = work()
            except (OSError, ValueError) as exc:
                box["error"] = exc
            finally:
                with _answering_lock:
                    if key is not None and _answering.get(key) is running:
                        _answering.pop(key, None)
                finished.set()
        threading.Thread(target=run, name="files-answer", daemon=True).start()
    wait = ANSWER_WAIT if wait is None else wait
    if not finished.wait(wait if mine else min(wait, AGAIN_WAIT)):
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("result")


def answer_key(*given):
    """answered's KEY of a look at GIVEN (paths, the sandboxes' folders, their identities), lists and dicts as
    tuples."""
    def frozen(value):
        if isinstance(value, dict):
            return tuple(sorted((frozen(item), frozen(other)) for item, other in value.items()))
        if isinstance(value, (list, tuple)):
            return tuple(frozen(item) for item in value)
        return value
    return frozen(given)


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


def same_connection(one, other):
    """Whether ONE and OTHER are folders of one remote system's connection (a move there is a rename)."""
    return getattr(one, "remote", False) and getattr(other, "remote", False) and \
        getattr(one, "sftp", None) is getattr(other, "sftp", 0)


def item_text(root, relative):
    """The path of RELATIVE of ROOT as shown: user@host:/path for a remote system's, else this machine's path."""
    if getattr(root, "remote", False):
        return root.display(relative)
    return os.path.join(root.path, relative) if relative else root.path


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


def copy_name(name, number=1, folder=False):
    """The name of a copy of NAME beside it: "a (copy).txt", "a (copy 2).txt" (a FOLDER's, the whole name: "src.d
    (copy)" - it was "src (copy).d", cut at the dot as a file's extension)."""
    stem, dot, extension = name.rpartition(".")
    if not stem or "/" in extension or folder:  # no extension (".bashrc": the whole name)
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
# A file of an agent's handed to another program (a drag out, the clipboard) goes as a copy in a folder of the app's own
# (handed_out_folder), never by its path in the agent's folder - the agent could turn that name into a link to a file of
# the human's before the other program read it. At most EXPORT_LIMIT bytes at once, copied within EXPORT_WAIT s (the
# window waits for it); the copies of the last EXPORT_KEPT hand-overs stay (the other program may read them later), and
# those the system clipboard names (keep_handed_out); older ones and all of them at exit are removed.
EXPORT_LIMIT = 32 << 20
EXPORT_WAIT = 2.0
EXPORT_KEPT = 8
_handed_out = {"folder": None, "made": [], "kept": None}
_handed_out_lock = threading.Lock()


def handed_out_folder():
    """A new folder (0700) of its own for one hand-over, in the app's folder of copies handed to other programs (made
    once, in the system's temporary folder: TMPDIR; removed at exit); the oldest beyond EXPORT_KEPT are removed - never
    the one the system clipboard names (keep_handed_out: each drag made one more, also one between the panes, and after
    a few the copy of a file of an agent's copied with Ctrl+C was gone - a paste in another program found nothing)."""
    import atexit
    import shutil
    import tempfile
    with _handed_out_lock:
        if _handed_out["folder"] is None or not os.path.isdir(_handed_out["folder"]):
            _handed_out["folder"] = tempfile.mkdtemp(prefix="sandbox-ai-app-handed-out-")
            _handed_out["made"] = []
            atexit.register(shutil.rmtree, _handed_out["folder"], True)
        made = tempfile.mkdtemp(dir=_handed_out["folder"])
        _handed_out["made"].append(made)
        while len(_handed_out["made"]) > EXPORT_KEPT:
            oldest = next((folder for folder in _handed_out["made"] if folder != _handed_out["kept"]), None)
            if oldest is None:
                break
            _handed_out["made"].remove(oldest)
            shutil.rmtree(oldest, ignore_errors=True)
        return made


def keep_handed_out(paths=()):
    """The hand-over folder that holds PATHS (the copies the system clipboard names now) is kept while it does
    (handed_out_folder); none: the clipboard names none of them any more."""
    with _handed_out_lock:
        folders = {os.path.dirname(path) for path in paths}
        kept = folders.pop() if len(folders) == 1 else None
        _handed_out["kept"] = kept if kept in _handed_out["made"] else None


# At most EXPORT_STUCK of within's threads at once: on a file system that hangs (a hard NFS mount, sshfs whose server
# went away) a look never ends however it is given up - every drag or Ctrl+C there left one more thread waiting for
# good.
EXPORT_STUCK = 4
_within, _within_lock = {"running": 0, "by_key": {}}, threading.Lock()


def within(work, wait, name="files-export", key=None):
    """WORK(give_up) in a thread, waited for WAIT s (the GUI thread waits): its result, or None when it failed (OSError,
    ValueError) or was not done by then - GIVE_UP() is true from then on, and WORK stops at its next look. KEY (what
    WORK looks at, answer_key): while a WORK of that KEY is still on its way, it is waited for (WAIT at most -
    AGAIN_WAIT once the one who began it gave it up), never started again; and while EXPORT_STUCK of them run, none is
    started (None at once: what needs the look is not handed out)."""
    with _within_lock:
        running = _within["by_key"].get(key) if key is not None else None
        mine = running is None
        if mine:
            if _within["running"] >= EXPORT_STUCK:
                return None
            _within["running"] += 1
            running = (threading.Event(), {}, threading.Event())
            if key is not None:
                _within["by_key"][key] = running
    abandoned, box, finished = running
    if mine:
        def run():
            try:
                box["result"] = work(abandoned.is_set)
            except (OSError, ValueError):
                box["result"] = None
            finally:
                with _within_lock:
                    _within["running"] -= 1
                    if key is not None and _within["by_key"].get(key) is running:
                        _within["by_key"].pop(key, None)
                finished.set()
        threading.Thread(target=run, name=name, daemon=True).start()
    elif abandoned.is_set():
        # (given up by the one who began it: waited for a moment only - every drag held up the window for EXPORT_WAIT)
        wait = min(wait, AGAIN_WAIT)
    if not finished.wait(wait):
        if mine:
            abandoned.set()
        return None
    return box.get("result")


# A host folder handed to other programs is looked through for a sandbox's folder (holds_agents): at most EXPORT_FOLDERS
# folders (within EXPORT_WAIT) - one bigger is not handed out.
EXPORT_FOLDERS = 20000


def holds_agents(path, folders, identities, give_up=None, limit=None):
    """Whether the host folder PATH may hold an agent's files (in a thread: export_urls): it is one of the sandboxes'
    FOLDERS, lies in one or holds one by its path (below(), folded; holding_folder: also one reached under another
    path) - or a folder in it, however deep, is one of them by its identity (IDENTITIES: a sandbox's folder moved there,
    or mounted there - a bind mount of a workspace is that folder itself, the same device and inode; one mounted
    deep inside a folder dragged out went to the other program with it). Looked through without following a link, also
    into what is mounted there, at most LIMIT (EXPORT_FOLDERS) folders and until GIVE_UP() is true: a folder not looked
    through in full - or no folder any more - counts as one that holds one."""
    if any(below(folder, path, folded=True) for folder in folders):
        return True
    if holding_folder(path, folders, identities) is not None:
        return True
    own = folder_identity(path)
    if own is None or own in identities:
        return True
    limit = EXPORT_FOLDERS if limit is None else limit
    waiting, looked = [path], 0
    while waiting:
        looked += 1
        if looked > limit or (give_up is not None and give_up()):
            return True
        try:
            with os.scandir(waiting.pop()) as found:
                for entry in found:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if (info.st_dev, info.st_ino) in identities:
                        return True
                    waiting.append(entry.path)
        except OSError:
            continue  # (a folder the human may not read: no program of theirs reads it either)
    return False


# At most LOCAL_STUCK local steps wait past LOCAL_GRACE at once (a file system that hangs: they never end - every
# Properties, rename or new folder there, and every one cancelled and tried again, left one more thread waiting for
# good); past them a step is refused at once, saying why.
LOCAL_STUCK = 8
_stuck_steps, _stuck_lock = {"count": 0}, threading.Lock()


def local_step(work, done, owner=None, grace=None, slow=None):
    """WORK() of a local root, then DONE(result, error) on the GUI thread. In a thread, waited for LOCAL_GRACE: done by
    then - every step on a file system that answers - DONE's result is returned at once, as before; a file system that
    hangs (NFS, FUSE) holds up the window no longer than that, DONE comes when the step ends (True returned
    meanwhile). The local steps ran on the GUI thread, and a hung mount held up the whole window. SLOW(ended): the
    step goes on as a job of the page (its busy bar, its buttons greyed out, Cancel) - ENDED(cancelled) waits for it
    (files.Cancelled once CANCELLED() is true); SLOW returns False where it cannot (then as without it). While
    LOCAL_STUCK steps still wait so, none is started: DONE gets a files.Refused at once."""
    with _stuck_lock:
        stuck = _stuck_steps["count"]
    if stuck >= LOCAL_STUCK:
        return done(None, files.Refused(tr(
            "{count} steps still wait for a file system that does not answer (one that hangs?); nothing more is "
            "started until one of them has ended.", count=stuck)))
    box, finished, state = {}, threading.Event(), {"stuck": False, "ended": False}

    def run():
        try:
            box["result"] = work()
        except BaseException as exc:  # noqa: BLE001 - handed to DONE (OSError, ValueError) or raised (anything else)
            box["error"] = exc
        finally:
            with _stuck_lock:
                state["ended"] = True
                if state["stuck"]:
                    _stuck_steps["count"] -= 1
            finished.set()
    threading.Thread(target=run, name="files-local-step", daemon=True).start()
    if finished.wait(LOCAL_GRACE if grace is None else grace):
        error = box.get("error")
        if error is not None and not isinstance(error, (OSError, ValueError)):
            raise error
        return done(box.get("result"), error)
    with _stuck_lock:
        if not state["ended"]:
            state["stuck"] = True
            _stuck_steps["count"] += 1

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
    fit_steady = True  # fitted to typical rows first (Pane.seed_columns): a change of folder moves no column

    def __init__(self, headers, numeric=(), stretch=None):
        super().__init__(headers, numeric, stretch)
        # (after QTableView's own handler, which fits the column to its content: the human sized it)
        self.horizontalHeader().sectionHandleDoubleClicked.connect(self.sized_by_hand)
        self.pane = None  # set by its Pane
        self.press = None  # where the left button went down on a selected row
        self.drop_row = None  # framed while something is dragged over: a row, -1 the whole list, None nothing
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def sized_by_hand(self, column):
        """COLUMN was fitted by a double click on its edge: the human's width from now on (and kept)."""
        from .state import sized_by_hand
        sized_by_hand(self, column)
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
        if plan is not None and not plan.refused:
            event.setDropAction(plan.action)
        event.accept()  # (also where it cannot be dropped yet: the moves over the list follow)

    def dragMoveEvent(self, event):  # noqa: N802
        plan = self.plan(event)
        self.show_drop(plan)
        if plan is None or plan.refused:
            event.ignore()  # (the "no" cursor; the status line says why: drop_hint)
            return
        event.setDropAction(plan.action)
        event.accept()

    def dragLeaveEvent(self, event):  # noqa: N802
        self.show_drop(None)
        event.accept()

    def dropEvent(self, event):  # noqa: N802
        plan = self.plan(event)
        self.show_drop(None)
        if plan is None or plan.refused:
            if plan is not None:
                self.pane.page.refuse_drop(plan)
            event.ignore()
            return
        self.pane.page.drop_taken = True
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
        self.folders = frozenset()  # the names among them that were folders (copy_items: a copy beside, copy_name)
        self.files = frozenset()  # the names among them that were plain files (a drag: what an editor opens)

    def owns(self, mime):
        return mime is not None and mime.hasFormat(MIME) and bytes(mime.data(MIME)).decode("ascii", "replace") == \
            self.token


class DropPlan:
    """What a drop at one place does: copy or move (ACTION) CLIP's items, or copy the host PATHS, into TARGET_DIR of
    PANE; ROW is framed (-1: the list, None: no row). REFUSED: why nothing can be dropped there (the drop is not taken,
    the "no" cursor shows, the status line says it). PLACE: where it was dragged over - None the list, "path" the path
    bar (into the folder shown), "chooser" the chooser (where it goes is chosen after the drop:
    FilesPage.chooser_drop)."""

    def __init__(self, pane, target_dir, row, action, clip=None, paths=(), refused=None, place=None):
        self.pane, self.target_dir, self.action, self.clip, self.paths = pane, target_dir, action, clip, list(paths)
        self.row, self.refused, self.place = None if refused else row, refused, place

    @property
    def move(self):
        return self.action == Qt.DropAction.MoveAction

    def text(self):
        if self.refused:
            return tr("Cannot drop here: {why}", why=self.refused)
        count = len(self.clip.items) if self.clip is not None else len(self.paths)
        if self.place == "chooser":
            if self.move:
                return tr("Drop: choose where to move {count} item(s) - this pane's places and recent folders.",
                          count=count)
            if self.clip is not None:
                return tr("Drop: choose where to copy {count} item(s) - this pane's places and recent folders (with "
                          "Shift: move).", count=count)
            return tr("Drop: choose where to copy {count} item(s) - this pane's places and recent folders.",
                      count=count)
        where = printable(self.pane.path_at(self.target_dir))
        if self.move:
            return tr("Drop: move {count} item(s) into {folder}.", count=count, folder=where)
        if self.clip is not None:
            return tr("Drop: copy {count} item(s) into {folder} (with Shift: move).", count=count, folder=where)
        return tr("Drop: copy {count} item(s) into {folder}.", count=count, folder=where)


DELETE_LISTED = 10  # full paths named in the delete question itself (all of them in its details)


def full_paths(chosen):
    """The full path of each item of CHOSEN as the pane shows its folder (its path bar's text, a host's prefix too)."""
    folder = str(chosen.folder_text or "").rstrip("/")
    return [f"{folder}/{name}" for name, _ in chosen.items]


def delete_question(chosen):
    """The delete question (user 2026-11-18): WHAT goes, by its full path - never "N items in FOLDER", which read as
    if the folder itself were deleted. One item: its path and its kind; several: each path, at most DELETE_LISTED."""
    paths = [printable(path) for path in full_paths(chosen)]
    kinds = chosen.kinds if len(chosen.kinds) == len(paths) else [None] * len(paths)
    if len(paths) == 1:
        kind = kinds[0]
        if kind == "dir":
            return tr("Delete this folder with everything in it?\n\n{path}\n\nThis cannot be undone.", path=paths[0])
        if kind == "link":
            return tr("Delete this link? It is removed, not what it points to.\n\n{path}\n\nThis cannot be undone.",
                      path=paths[0])
        return tr("Delete this file?\n\n{path}\n\nThis cannot be undone.", path=paths[0])
    shown = "\n".join(paths[:DELETE_LISTED])
    if len(paths) > DELETE_LISTED:
        shown += "\n" + tr("… and {count} more (all of them under Details)", count=len(paths) - DELETE_LISTED)
    return tr("Delete these {count} items? Folders are deleted with everything in them; links are removed, not what "
              "they point to.\n\n{paths}\n\nThis cannot be undone.", count=len(paths), paths=shown)


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
                   pane.path_text(), pane.untrusted(), pane.mode() == "sandbox",
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
        # A drop onto the chooser waiting for the place chosen after it to be shown (FilesPage.drop_at_place), or None.
        self.pending_drop = None
        self.on_way = {}  # the listings on their way, by what they list: {key: {root, waiters, began}} (Pane.list)
        # What the chooser shows now (its data: a sandbox's folder, LOCAL, a host, a remote system) - a host chosen that
        # is not connected yet leaves it showing this until it is (choose_root); this machine's folder shown last.
        self.place, self.local_relative = None, host_relative(os.path.realpath(os.path.expanduser("~")))
        # The sandbox side's folders of this machine as read last ([(kind, label, path)]: refresh_roots) - what its
        # chooser lists under them, and what folder_sources takes for the sandboxes' folders (also those a host
        # filter does not list now: their files stay an agent's).
        self.sandbox_roots = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.title = QLabel(tr("Sandbox") if side == "sandbox" else tr("Files on"))
        top.addWidget(self.title)
        # Where the pane lists: the left one a sandbox's folder (or a project's, one chosen), the right one this
        # machine - and either of them this machine, one of sandbox-ai's hosts by name or a remote system (SSH…).
        self.roots = QComboBox()
        self.roots.setAccessibleName(tr("Folder of a sandbox") if side == "sandbox" else tr("Where this pane lists"))
        self.title.setBuddy(self.roots)
        # (its entries' length never widens the page: a host's long address made it wider than a small screen - both
        # panes' rows fit 900 px in German)
        self.roots.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.roots.setMinimumContentsLength(6)
        self.roots.setMinimumWidth(190 if side == "sandbox" else 100)
        self.roots.currentIndexChanged.connect(lambda _=0: self.choose_root())
        # Files dropped onto it go to a place chosen after the drop (FilesPage.chooser_drop: its places and recent
        # folders).
        self.roots.setAcceptDrops(True)
        top.addWidget(self.roots, 1)
        if side == "sandbox":
            top.addWidget(nav_button(tr("Refresh"), tr("Look for sandboxes and sessions again"), self.refresh_roots,
                                     "refresh"))
            top.addWidget(nav_button(tr("Other folder…"), tr("Browse another folder as an agent's"), self.add_other))
        top.addWidget(nav_button(tr("SSH…"), tr("Browse a remote system over SSH and SFTP in this pane (keys and your "
                                                 "ssh-agent only)"), lambda: self.page.remote_dialog(self)))
        if side == "host":
            self.root, self.relative, self.place = page.local, self.local_relative, LOCAL
            self.roots.blockSignals(True)
            self.fill()
            self.roots.setCurrentIndex(max(0, self.roots.findData(LOCAL)))
            self.roots.blockSignals(False)
        layout.addLayout(top)
        where = QHBoxLayout()
        where.addWidget(nav_button(tr("Up"), tr("The folder above (Backspace, Alt+Up)"), self.up, "up"))
        # (beside Up in both panes - where each lists: this machine's HOME, a remote system's, a sandbox folder's top)
        where.addWidget(nav_button(tr("Home"), tr("HOME of what the pane shows - this machine or a remote system -, or the top of a sandbox's folder "
                                       "(Alt+Home)"), self.home, "home"))
        self.path = QLineEdit()
        self.path.setAccessibleName(tr("Folder"))
        self.path.setToolTip(tr("Type a folder and press Enter; files dropped here go into the folder shown"))
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
        where.addWidget(nav_button(tr("Refresh"), tr("List the folder again ({shortcut})", shortcut=key_text("Ctrl+R")),
                                   self.list, "refresh"))
        # The search below the folder shown (file_search_view.py): names with wildcards, content (Ctrl+Shift+F).
        where.addWidget(nav_button(tr("Search…"), tr("Search below this folder: names with wildcards, optionally the "
                                                     "content ({shortcut})", shortcut=key_text("Ctrl+Shift+F")),
                                   lambda: self.page.search_dialog(self), "search"))
        layout.addLayout(where)
        # The Name takes the room. A ModelTable: a folder of 20,000 entries is shown in one step.
        self.table = table([tr(column) for column in COLUMNS], multi=True, stretch=0, many=True,
                           compact=(1, 2, 3, 5, 6), view=FileTable)
        self.table.pane = self
        self.table.setAccessibleName(tr("Files of the sandbox") if side == "sandbox" else tr("Files of the right pane"))
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
        # The quick filter (user 2026-11-19): the rows narrowed to the names holding what is typed (* and ?: wildcards);
        # what it leaves out is not in the list, so nothing acts on it. Ctrl+F gets here, Escape empties it.
        quick_row = QHBoxLayout()
        quick_row.setContentsMargins(0, 0, 0, 0)
        self.quick = QLineEdit()
        self.quick.setClearButtonEnabled(True)
        self.quick.setPlaceholderText(tr("Filter names…"))
        self.quick.setAccessibleName(tr("Filter names"))
        self.quick.setToolTip(safe_tooltip(tr("Only the names that hold this text ({shortcut}; * and ? as wildcards; "
                                              "Escape empties it)", shortcut=key_text("Ctrl+F"))))
        self.quick.textChanged.connect(lambda _text: self.show_rows())
        self.quick.installEventFilter(self)
        quick_row.addWidget(self.quick, 1)
        self.quick_count = plain_label(role="secondary", word_wrap=False)
        self.quick_count.hide()
        quick_row.addWidget(self.quick_count)
        layout.addLayout(quick_row)
        self.full_rows = None  # what the last listing found, before the quick filter: show_rows
        layout.addWidget(self.table, 1)
        # The free space of the file system of the folder shown (read with each listing: after a change of folder and
        # after every copy, move or delete).
        self.space = plain_label(role="secondary", word_wrap=False)
        self.space.setAccessibleName(tr("Free space"))
        layout.addWidget(self.space)
        # A remote system's files (either pane): an agent's or the human's own (RemoteRoot.agents) - on for
        # sandbox-ai's hosts, where sandboxes run and their agents may have written any file (FilesPage.connected);
        # turned off only after a question (agents_chosen).
        self.agents_box = QCheckBox(tr("An agent's files"))
        self.agents_box.setToolTip(tr("Treat the files of this remote system as an agent's: copied to this machine "
                                      "they are never executable and files that tools run are named; no set-ID or "
                                      "sticky bit is set there. On for sandbox-ai's hosts, where sandboxes run."))
        self.agents_box.setAccessibleName(tr("An agent's files"))
        self.agents_box.toggled.connect(self.agents_chosen)
        self.agents_box.hide()
        layout.addWidget(self.agents_box)
        self.note = plain_label(role="secondary")
        self.note.hide()
        layout.addWidget(self.note)
        # Up and Home by key anywhere in the pane (Backspace too, in its list).
        context = Qt.ShortcutContext.WidgetWithChildrenShortcut
        QShortcut(QKeySequence("Alt+Up"), self, context=context, activated=self.up)
        QShortcut(QKeySequence("Alt+Home"), self, context=context, activated=self.home)
        QShortcut(QKeySequence("Ctrl+F"), self, context=context, activated=self.focus_quick)
        # The pane the toolbar's buttons act on (bold): also the one whose path field, chooser or buttons were used last
        # (a folder typed in the right pane's path field, then New folder: it was made in the left pane, the one whose
        # list had the keyboard before).
        for widget in (self.path, self.roots):
            widget.installEventFilter(self)
        for widget in self.findChildren(QPushButton):
            widget.pressed.connect(lambda: self.page.activate(self))

    def changeEvent(self, event):  # noqa: N802
        """Another look (theme): the buttons' glyphs in its text colour."""
        super().changeEvent(event)
        repaint_glyphs(self, event)

    def seed_columns(self):
        """Fit the columns to rows of the widths they will mostly need (SEED_ROW, in the app's language) before the
        first folder is listed: later folders hardly ever widen a column, so the list does not jump."""
        kinds = max((tr(kind) for kind in KINDS.values()), key=len)
        try:
            group = group_name(os.getgid())
        except Exception as _exc:  # noqa: BLE001 - only a width
            applog.swallowed("files_page", "Pane.seed_columns: only a width", _exc)
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
        if self.mode() != "sandbox":
            return "/" + relative
        return self.root.path + ("/" + relative if relative else "")

    def remote(self):
        """Whether the pane shows a remote system (remote.RemoteRoot: its root is the remote "/")."""
        return bool(getattr(self.root, "remote", False))

    def mode(self):
        """What the pane shows: "remote" (a remote system: remote.RemoteRoot), "host" (this machine, the human's own:
        the page's Root("/"), walked down to the folder shown) or "sandbox" (a sandbox's folder, a project's, one
        chosen - an agent may have written everything below it). Without a root: as its side starts."""
        if self.remote():
            return "remote"
        if self.root is None:
            return "sandbox" if self.side == "sandbox" else "host"
        return "host" if self.root is getattr(self.page, "local", None) or not self.root.sandbox else "sandbox"

    def path_of_entry(self, entry):
        """The absolute path (shown) of ENTRY of the folder shown."""
        return os.path.join(self.path_text(), entry.name)

    def untrusted(self):
        """True if an agent may have written what is shown: a sandbox root, or a host folder in one."""
        return self.untrusted_at(self.relative)

    def untrusted_at(self, relative, unknown=True):
        """Whether an agent may have written in the folder RELATIVE; UNKNOWN when that could not be looked at (a host
        folder on a file system that hangs: sandbox_holding) - True where the agent's is the safe side (what is copied
        from there, set there), None for a target (transfer_items: as the agent's, what is copied out of a sandbox's
        folder into it kept its executable bits)."""
        if self.remote():
            # (the human's own system unless its files count as an agent's - a host of sandbox-ai's: RemoteRoot.agents;
            # what comes from any remote system into a sandbox's folder counts as the agent's)
            return bool(getattr(self.root, "agents", False))
        if self.mode() == "sandbox":
            return True
        if relative == self.shown and getattr(self, "inside", False):
            return True  # (as its listing found it, in its thread: also a sandbox's folder moved here)
        holder = self.page.sandbox_holding(self.path_at(relative), unknown=UNKNOWN)
        return unknown if holder is UNKNOWN else holder is not None

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
        """HOME: this machine's, a remote system's (where it starts), the top of a sandbox's folder."""
        mode = self.mode()
        if self.root is None:
            return False
        if mode == "remote":
            return self.go("/".join(files.parts(self.root.home.lstrip("/"))))
        return self.go(host_relative(os.path.realpath(os.path.expanduser("~"))) if mode == "host" else "")

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
            if self.mode() == "host":
                path = os.path.expanduser(text)
                path = os.path.normpath(path if os.path.isabs(path) else os.path.join(self.path_text(), path))
                # (where it leads, in a thread - answered: a path on a file system that hangs held up the window; one
                # that does not answer is gone to as typed, and its listing follows no link on its way)
                folders, identities = self.page.sandbox_folders(), dict(self.page.identities)

                def look():
                    real = os.path.realpath(path)
                    return real, real != path and through_link(path, folders, identities)
                done, found = answered(look, key=answer_key("go", path, folders, identities))
                if done and found[1]:
                    raise files.Refused(tr("{path} goes through a symbolic link in a sandbox's folder; it is not "
                                           "followed.", path=printable(path)))
                return self.go(host_relative(found[0] if done else path))
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

    def refresh_roots(self, select=None, then=None, hosts=True):
        """The folders of sandboxes, sessions and projects, read in the background (sandbox-ai's registry may ask
        Podman); the list changes when they are known. SELECT: the folder to show then; THEN() after that. HOSTS: those
        of sandbox-ai's hosts' sandboxes read again too (FilesPage.read_host_folders: one call per host, each in a
        thread of its own; not when a connection was just made). (The right pane: its places at once - this machine,
        the hosts, the remote systems connected.)"""
        if self.side == "host":
            self.show_places(select)
            if then is not None:
                then()
            return
        self.roots_token, self.roots_reading = getattr(self, "roots_token", 0) + 1, True
        token, keep = self.roots_token, select or self.roots.currentData()
        extra = list(self.page.projects()) + self.page.others
        known, sai = self.page.folder_sources()[1], self.page.sai

        def read():
            # (in the thread, all of it: the folders that are there when sandbox-ai cannot say - isdir on the GUI
            # thread held up the window for a folder on a file system that hangs -, and the folders' identities)
            # The projects and folders chosen are added here, each read for RESOLVE_WAIT at most (looked_up): sandbox-ai
            # read their paths itself, and one on a file system that hangs left the list without any sandbox for a
            # minute, then with the projects alone. (One that does not answer is listed as given: it opens in the
            # background when chosen.)
            try:
                roots = list(sai.file_roots(extra=()))
            except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai, Podman not answering: the folders chosen
                applog.swallowed("files_page", "refresh_roots.read: an older sandbox-ai, Podman not answering: …", _exc)
                roots = []
            seen = {path for _kind, _label, path in roots}
            for kind, label, path in extra:
                real = looked_up(("real", str(path)), lambda path=path: read_real(path), RESOLVE_WAIT) or \
                    given_path(path)
                there = looked_up(("folder", real), lambda real=real: os.path.isdir(real), RESOLVE_WAIT)
                if real not in seen and there is not False:
                    seen.add(real)
                    roots.append((kind, label, real))
            return roots, resolve_folders([path for _kind, _label, path in roots if str(path).startswith("/")],
                                          known)[1]

        def read_done(found, error):
            if token != self.roots_token:
                return None
            roots, identities = found if error is None else (list(extra), {})
            before = (self.page.folder_sources()[0], set(self.page.identities))
            self.page.keep_identities(identities)
            shown = self.show_roots(roots, keep, then)
            if (self.page.folder_sources()[0], set(self.page.identities)) != before:
                # (the host pane, listed before they were known, said nothing of a sandbox's folder it shows: no note,
                # its set-ID bits unmarked - until it was listed again by hand)
                for pane in (self.page.left, self.page.right):
                    if pane.mode() == "host":
                        pane.list()
            return shown
        background(read, owner=self.page, timeout=60, work=False, done=read_done)
        self.page.read_sandbox_paths()
        if hosts:
            self.page.read_host_folders()

    def show_roots(self, roots, keep, then=None):
        self.roots_reading = False
        self.sandbox_roots = list(roots)
        self.roots.blockSignals(True)
        self.fill()
        index = self.roots.findData(keep) if keep else -1
        if index < 0:
            index = self.shown_index()  # (this machine, a host or a host's sandbox's folder shown: it stays)
        self.roots.setCurrentIndex(index if index >= 0 else self.first_choice())
        self.roots.blockSignals(False)
        self.forget_roots()
        self.choose_root()
        if then is not None:
            then()

    def fill(self):
        """The chooser made anew (its signals held by the caller, who chooses what it shows), in sections: the sandbox
        side's folders of this machine (sandbox_roots), the hosts - this machine and sandbox-ai's by name (connected
        when chosen) -, the folders of each host's sandboxes (the sandbox side: FilesPage.host_sections) and the remote
        systems connected with SSH… (FilesPage.places). A heading above each section once there is more than this
        machine to tell apart (a host of sandbox-ai's, a remote system); the page's host filter keeps the sandboxes'
        sections to that host's."""
        page = self.page
        # (the host filter's host, and the host of what is shown: its section stays until something else is chosen)
        only = page.host_filter() if self.side == "sandbox" else None
        hosts = None if only is None else {only, host_of(self.place) if self.root is not None else only}
        places = [(text, data, tip, True) for text, data, tip in page.places()]
        sections = []
        if self.side == "sandbox" and (hosts is None or LOCAL_HOST in hosts):
            sections.append((tr("Sandboxes on this machine"), [(root_title(kind, label, path), path, None, True)
                                                              for kind, label, path in self.sandbox_roots]))
        sections.append((tr("Hosts"), [place for place in places if not place[1].startswith("ssh://")]))
        if self.side == "sandbox":
            sections += page.host_sections(hosts)
        sections.append((tr("Remote systems"), [place for place in places if place[1].startswith("ssh://")]))
        headed = any(place[1].startswith((HOST_PREFIX, "ssh://")) for place in places)
        self.roots.clear()
        for heading, items in sections:
            if not items:
                continue
            if headed:
                self.roots.addItem(heading)
                index = self.roots.count() - 1
                self.roots.setItemData(index, True, HEADING_ROLE)
                font = self.roots.font()
                font.setBold(True)
                self.roots.setItemData(index, font, Qt.ItemDataRole.FontRole)
                widgets.set_row_enabled(self.roots, index, False, selectable=False)
            for text, data, tip, enabled in items:
                self.roots.addItem(text, data)
                index = self.roots.count() - 1
                if tip:
                    self.roots.setItemData(index, tip, Qt.ItemDataRole.ToolTipRole)
                if not enabled:
                    widgets.set_row_enabled(self.roots, index, False)

    def first_choice(self):
        """The first entry of the chooser that can be chosen (no heading, no note), or -1."""
        for index in range(self.roots.count()):
            if self.roots.itemData(index) and widgets.row_enabled(self.roots, index):
                return index
        return -1

    def shown_index(self):
        """The entry of what the pane shows while it is not chosen anew: this machine or a host (is_place), or the
        folder of a host's sandbox - else, when that folder is no longer listed (its host answers no more, the host
        filter shows another host), its host's own entry (the same connection: it shows that host's files). -1: none."""
        if self.root is None:
            return -1
        index = self.roots.findData(self.place) if (is_place(self.place) or host_folder_parts(self.place)) else -1
        folder = host_folder_parts(self.place)
        if index < 0 and folder is not None:
            index = self.roots.findData(HOST_PREFIX + folder[0])
            if index >= 0:
                self.place = HOST_PREFIX + folder[0]
        return index

    def show_places(self, select=None):
        """The chooser's places made anew (a connection made, a host added, a host's sandboxes read); SELECT (its data)
        shown then - else what is shown stays, without being listed again (a pane that shows the other pane's host
        stays where it is)."""
        current = self.roots.currentData()
        self.roots.blockSignals(True)
        self.fill()
        index = self.roots.findData(select if select is not None else current) \
            if (select if select is not None else current) is not None else -1
        if index < 0 and current is not None:
            # (a remote system connected with SSH… that is one of sandbox-ai's hosts chosen by name since: its entry)
            name = next((name for name, key in self.page.host_keys.items() if key == current), None)
            index = self.roots.findData(HOST_PREFIX + name) if name is not None else -1
            if index >= 0:
                self.place = HOST_PREFIX + name
        if index < 0 and select is None:
            index = self.shown_index()
        chosen = index >= 0
        self.roots.setCurrentIndex(index if chosen else self.first_choice())
        self.roots.blockSignals(False)
        self.forget_roots()
        if select is not None or not chosen:
            self.choose_root()

    def show_place(self, data):
        """The chooser shows DATA again (nothing chosen, nothing listed): a host not connected yet stays unchosen."""
        index = self.roots.findData(data) if data is not None else -1
        self.roots.blockSignals(True)
        self.roots.setCurrentIndex(index)
        self.roots.blockSignals(False)

    def add_other(self):
        path = choose_folder(self, tr("Folder to browse"), "browse")
        if path:
            self.page.others.append(("other", "", path))
            self.refresh_roots(select=os.path.realpath(path))

    def choose_root(self):
        path = self.roots.currentData()
        if path is None and self.roots.currentIndex() >= 0:
            self.show_place(self.place)  # (a heading or a note: nothing to show - what is shown stays)
            return
        pending = self.pending_drop
        if pending is not None and pending["root"] != path:
            self.pending_drop = None  # (another place chosen meanwhile: the drop onto the chooser was for that one)
        folder = host_folder_parts(path)
        if (isinstance(path, str) and path.startswith(HOST_PREFIX)) or folder is not None:
            # One of sandbox-ai's hosts by name, or the folder of a sandbox there: the host's connection, or one made
            # now - meanwhile the pane stays where it was (a host whose key is not trusted, or that cannot be reached,
            # changes nothing here).
            name = folder[0] if folder is not None else path[len(HOST_PREFIX):]
            root = self.page.host_root(name)
            if root is None or not root.alive:
                self.show_place(self.place)
                self.page.connect_host(name, self, select=path)
                return
        if self.mode() == "host" and self.root is not None:
            self.local_relative = self.relative  # (this machine chosen again: the folder shown last)
        after, self.after_connect = getattr(self, "after_connect", None), None
        self.place = path
        self.root, self.relative, self.shown, self.opening = None, "", None, None
        self.open_token = token = getattr(self, "open_token", 0) + 1
        self.table.clearSelection()
        if path == LOCAL:
            self.root, self.relative = self.page.local, self.local_relative
        elif isinstance(path, str) and path.startswith(HOST_PREFIX):
            self.root = self.page.host_root(path[len(HOST_PREFIX):])
            # (Recent: a folder of a host not connected then - its folder, once it is; else its HOME)
            self.relative = after[1] if after is not None and after[0] == path else \
                "/".join(files.parts(self.root.home.lstrip("/")))
        elif folder is not None:
            # A sandbox's folder on that host: its files over the host's connection (an agent's, as all of the host's:
            # RemoteRoot.agents), from that folder - the pane may go above it, as in the host's own entry.
            self.root = self.page.host_root(folder[0])
            self.relative = after[1] if after is not None and after[0] == path else \
                "/".join(files.parts(folder[1].lstrip("/")))
        elif path in self.page.remotes:
            root = self.page.remotes[path]
            if root.alive:
                self.root, self.relative = root, "/".join(files.parts(root.home.lstrip("/")))
            else:
                self.page.message.setText(tr("The connection to {host} has ended; SSH… connects again.",
                                             host=printable(root.label)))
        elif path:
            # Opened in a thread (local_step: done at once on a file system that answers): a folder on one that hangs
            # (sshfs whose server went away) held up the whole window when it was chosen. Meanwhile the rows of the
            # folder before stand for no entry (changing()).
            self.opening = path

            def opened(root, error):
                if token != getattr(self, "open_token", 0):
                    return False  # (another folder chosen meanwhile)
                self.opening = None
                after, self.after_open = getattr(self, "after_open", None), None
                if error is not None:
                    self.page.message.setText(tr("Cannot open {path}: {error}", path=printable(path),
                                                 error=printable(error)))
                else:
                    self.root = root
                    if after is not None and after[0] == token:  # (Recent: its folder, not the top)
                        self.relative = after[1]  # (listed in its thread: one gone since falls back to the top)
                return self.root_chosen()
            local_step(lambda: files.Root(path), opened, owner=self)
            return
        self.root_chosen()

    def root_chosen(self):
        """The folder chosen is open (or could not be): the buttons, the notes, its listing - and, while the page's
        host filter offers one host's sandboxes, the chooser without the section of the host shown before."""
        if self.side == "sandbox" and self.page.host_filter() is not None and self.root is not None:
            self.show_places()
        if getattr(self.page, "buttons", None):  # (Take ownership: a sandbox's folder only, never a remote system)
            self.page.mark_active()
        if self.mode() != "host":  # (the note of a sandbox's folder on this machine: not under another place's files)
            self.note.setText("")
            self.note.hide()
        self.show_agents_box()
        self.list()
        return True

    def show_agents_box(self):
        """The choice of a remote system's pane: whether its files count as an agent's (RemoteRoot.agents)."""
        box = getattr(self, "agents_box", None)
        if box is None:
            return
        box.blockSignals(True)
        box.setChecked(self.remote() and bool(getattr(self.root, "agents", False)))
        box.blockSignals(False)
        box.setVisible(self.remote())

    def agents_chosen(self, on):
        """The box "An agent's files" of a remote system's pane: ON - its files count as an agent's here (copied to this
        machine never executable, files that tools run named, no set-ID or sticky bits set there); off only after a
        question (sandboxes of sandbox-ai's run on its hosts: their agents may have written any file there)."""
        root = self.root if self.remote() else None
        if root is None:
            return False
        if not on and root.agents and not confirm(
                self.page, tr("Not an agent's files?"),
                tr("Files of {host} are treated as an agent's: copied to this machine they are never executable, and "
                   "files that tools run are named. Sandboxes of sandbox-ai run on its hosts, and their agents may have "
                   "written any file there. Treat the files of this connection as your own?",
                   host=printable(root.label)), tr("Treat as my own")):
            self.show_agents_box()
            return False
        if not on and self.root is root and self.agents_box.isChecked():
            # (ticked again while the question was open - it blocks nothing: the box showed the files as an agent's,
            # and the answer made them the human's)
            return False
        root.agents = bool(on)
        self.list()
        return True

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
        side = self.mode()  # (what the folder is: "sandbox", "host" - this machine - or "remote")
        fallback = "" if side == "sandbox" else host_relative(os.path.realpath(os.path.expanduser("~")))
        remote = self.remote()
        if remote:
            fallback = "/".join(files.parts(root.home.lstrip("/")))
        ranges = self.page.ranges
        # (widgets read here, not in the thread; the folders' paths and identities are read there - on the GUI thread a
        # sandbox's folder on a file system that hangs held up the window with every listing)
        sources = ([], []) if side != "host" else self.page.folder_sources()
        kept = {} if side != "host" else dict(self.page.identities)
        agents = remote and bool(getattr(root, "agents", False))

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
            # sandbox's folder reached under another path: holding_folder; or moved here: those kept).
            identities = {}
            if remote:
                inside = agents
            elif side == "sandbox":
                inside = True
            else:
                folders, identities = resolve_folders(*sources)
                holder = holding_folder("/" + shown, folders, {**kept, **identities})
                inside = holder is not None
                met = folder_identity(holder) if inside and not any(holder == folder for folder in folders) else None
                if met in kept:  # (a sandbox's folder moved here, seen again: kept_identities keeps it on)
                    identities = {**identities, met: kept[met]}
            try:  # the free space of its file system (here: a hung file system holds up no window)
                space = root.free_space(shown)
            except (OSError, ValueError):
                space = None
            return entries, shown, problem, entry_rows(entries, ranges, inside, remote), inside, space, \
                entry_sorts(entries), identities
        # A listing of the same folder (with all it is made of) still on its way is not started again: on a file system
        # that hangs every Ctrl+R, Refresh or folder typed again left one more thread waiting for good. Asked for while
        # it was on its way, the folder is listed again once it has come (what it found may be from before a change).
        key = (id(root), answer_key(relative, fallback, side, remote, agents, sources, kept, ranges, language()))
        shared = self.on_way.get(key)
        waiter = {"token": token, "then": then, "answered": False, "again": shared is not None}
        if shared is None or shared["root"] is not root:
            # (a remote system's listings all end - a request without an answer ends its connection: a slow link's
            # listings of folders gone through quickly were refused as if the file system hung)
            now = time.monotonic()
            slow = 0 if remote else sum(1 for way in self.on_way.values()
                                        if not way["remote"] and now - way["began"] > LIST_SLOW)
            if slow >= LIST_STUCK:  # (and no more than LIST_STUCK of a pane that wait so)
                return self.not_listed(token, root, slow, then)
            waiter["again"] = False
            # (no time limit of its own: each one waiting for it has its own, LIST_TIMEOUT; a late listing never
            # reaches a pane deleted meanwhile)
            shared = self.start_listing(key, root, lambda done: background(work, owner=self, done=done))
        shared["waiters"].append(waiter)
        self.listing = token

        def still_listing():
            if self.listing == token:
                # (kept as it is said: found again by its text - by its English beginning it was never found in German,
                # "{path} wird aufgelistet…", and stayed)
                self.listing_note = tr("Listing {path}…", path=printable(self.path_text()))
                self.page.listing_says(self, self.listing_note)

        def expired():
            if not waiter["answered"]:
                waiter["answered"] = True
                self.listed(token, root, None, TimeoutError(tr("No answer within {seconds} s.",
                                                               seconds=int(LIST_TIMEOUT))), then)
        QTimer.singleShot(LIST_WAIT, self, still_listing)
        QTimer.singleShot(int(LIST_TIMEOUT * 1000), self, expired)
        return True

    def start_listing(self, key, root, begin):
        """BEGIN(done): Pane.list's work in a thread for the listing KEY - what it finds goes to every one waiting for
        it (those that asked for it once it was on its way get a listing made anew, in one thread for them all)."""
        shared = self.on_way[key] = {"root": root, "waiters": [], "began": time.monotonic(),
                                     "remote": bool(getattr(root, "remote", False))}

        def arrived(result, error):
            if self.on_way.get(key) is shared:
                self.on_way.pop(key, None)
            waiting = [waiter for waiter in shared["waiters"] if not waiter["answered"]]
            again = [waiter for waiter in waiting if waiter["again"]]
            for waiter in waiting:
                if not waiter["again"]:
                    waiter["answered"] = True
                    self.listed(waiter["token"], root, result, error, waiter["then"])
            if any(waiter["token"] == self.list_token for waiter in again) and root is self.root and \
                    key not in self.on_way:
                fresh = self.start_listing(key, root, begin)
                for waiter in again:
                    waiter["again"] = False
                    fresh["waiters"].append(waiter)
            else:
                for waiter in again:
                    waiter["answered"] = True
                    self.listed(waiter["token"], root, result, error, waiter["then"])
        begin(arrived)
        return shared

    def not_listed(self, token, root, count, then=None):
        """The listing of TOKEN is not started (COUNT of this pane's still wait for a file system that hangs): the pane
        stays on the folder it shows and says why (without one, as a listing that failed)."""
        text = tr("{path} was not listed: {count} listings of this pane still wait for a file system that does not "
                  "answer (one that hangs?). Try again once one of them has come.",
                  path=printable(self.path_text()), count=count)
        if self.shown is None:
            return self.listed(token, root, None, files.Refused(text), then)
        self.listing = None
        self.relative = self.shown
        self.path.setText(printable(self.path_text()))
        self.page.listing_says(self, text, ended=True)
        if then is not None:
            then(False)
        return False

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
        self.entries, relative, problem, (rows, colors), inside, space, sorts, identities = result
        self.page.keep_identities(identities)
        self.inside = inside and not self.remote()  # (untrusted_at: the folder shown, as its thread found it)
        self.by_name = {entry.name: entry for entry in self.entries}
        listed = problem is None
        self.show_space(space, unknown=self.remote())
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
        if moved and self.quick.text():  # another folder: from its top, unfiltered
            self.quick.blockSignals(True)
            self.quick.clear()
            self.quick.blockSignals(False)
        self.full_rows = (rows, colors, tips, keys, [None, None, *sorts])
        self.show_rows(moved)
        self.shown = relative
        if self.mode() == "host":
            self.note.setText(tr("This folder belongs to a sandbox: what is copied from here is treated like the "
                                 "agent's files (never executable, links not followed).") if inside else "")
            self.note.setVisible(inside)
        if then is not None:
            then(listed)
        if listed and self.pending_drop is not None and self.pending_shown():
            QTimer.singleShot(0, self, lambda: self.page.run_pending(self))  # (out of the listing's own call)
        return listed

    def quick_matches(self, name):
        """Whether NAME is shown under the quick filter's text (none: all; * and ?: wildcards; else held in it;
        letters' case aside)."""
        text = self.quick.text().strip().casefold()
        if not text:
            return True
        name = name.casefold()
        if "*" in text or "?" in text:
            return fnmatch.fnmatchcase(name, text) or fnmatch.fnmatchcase(name, f"*{text}*")
        return text in name

    def show_rows(self, moved=False):
        """The last listing's rows (full_rows) in the list, those the quick filter leaves out not among them ("." and
        ".." always); the count shown while it filters. One paint for the new rows."""
        if self.full_rows is None:
            return
        rows, colors, tips, keys, sorts = self.full_rows
        kept = [i for i, key in enumerate(keys) if key in (HERE, UP) or self.quick_matches(key)]
        filtering = len(kept) != len(keys)
        if filtering:
            rows, colors, tips, keys = ([seq[i] for i in kept] for seq in (rows, colors, tips, keys))
        total = len(self.full_rows[3]) - 2
        self.quick_count.setText(tr("{shown} of {total}", shown=len(keys) - 2, total=total) if filtering else "")
        self.quick_count.setVisible(bool(self.quick.text().strip()))
        self.table.setUpdatesEnabled(False)
        try:
            if moved:  # (selected in the rows of the folder before: other files here)
                self.table.clearSelection()
            fill(self.table, rows, colors, tooltips=tips, keys=keys, sort=sorts)
            if moved:  # another folder: from its top (a folder listed again keeps where it was scrolled to)
                self.table.scrollToTop()
                if rows:
                    self.table.selectionModel().setCurrentIndex(self.table.model().index(0, 0),
                                                                QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self.table.setUpdatesEnabled(True)

    def focus_quick(self):
        self.page.activate(self)
        self.quick.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.quick.selectAll()

    def pending_shown(self):
        """Whether the pane shows now where the drop onto its chooser waits to go (pending_drop)."""
        kind, value = self.pending_drop["wanted"]
        if kind == "place":
            return self.place == value
        return self.recent_key(self.relative) == value

    def path_of(self, entry):
        return f"{self.relative}/{entry.name}" if self.relative else entry.name

    def changing(self):
        """True while the rows are another folder's: go() (or another root) takes the new folder at once, its rows
        come with its listing - until then no row is an entry of the folder RELATIVE (none is chosen, none opens). So
        while a folder chosen is still being opened (choose_root)."""
        return getattr(self, "opening", None) is not None or self.listing is not None and self.shown != self.relative

    def waiting(self):
        """CHANGING(), said in the page's message: an action on entries waits for the listing."""
        if not self.changing():
            return False
        self.page.message.setText(tr("Wait until {path} is listed: the list still shows the folder before.",
                                     path=printable(getattr(self, "opening", None) or self.path_text())))
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

    def open_entry(self, entry, mode="auto", read_only=False, enter=True):
        """Enter the folder ENTRY, or open the file ENTRY in the editor (MODE "auto", "text" or "hex"). ENTER False
        (View, Edit: F3, F4): a folder - or a link to one - is not entered; the page says it is a folder."""
        if entry.kind == "dir":
            if mode == "auto" and enter:
                self.go(self.path_of(entry))
            else:
                self.page.message.setText(tr("{name} is a folder.", name=printable(entry.name)))
        elif entry.kind == "file":
            self.page.open_editor(self.root, self.path_of(entry), self.path_of_entry(entry), mode, read_only)
        elif entry.kind == "link":
            self.open_link(entry, mode, read_only, enter)
        else:
            self.page.message.setText(tr("{name} is not a regular file.", name=printable(entry.name)))

    def open_link(self, entry, mode="auto", read_only=False, enter=True):
        """A link is followed only on the host and out of every sandbox's folder (where it lies and where it points);
        it opens the folder (Open: ENTER) or the file it points to."""
        not_followed = tr("{name} is a symbolic link to {target}; it is not followed.", name=printable(entry.name),
                          target=printable(entry.target))
        if self.untrusted() or self.remote():  # (a remote system's links: named, never followed)
            self.page.message.setText(not_followed)
            return
        link = os.path.join(self.path_text(), entry.name)
        folders, identities = self.page.sandbox_folders(), dict(self.page.identities)

        def look():  # (in a thread - answered: a link to a file system that hangs held up the window)
            real = os.path.realpath(link)
            # (where it points, and every link on the way there: a host's link to a link of the agent's is not followed)
            agents = holding_folder(real, folders, identities) is not None or through_link(link, folders, identities)
            return real, agents, os.path.isdir(real), os.path.isfile(real)
        try:
            done, found = answered(look, key=answer_key("link", link, folders, identities))
        except (OSError, ValueError):
            done, found = True, (link, True, False, False)
        if not done:
            self.page.message.setText(tr("{path} does not answer (a file system that hangs?); nothing was opened.",
                                         path=printable(link)))
            return
        real, agents, folder, plain = found
        if agents:
            self.page.message.setText(not_followed)
        elif folder and mode == "auto" and enter:
            self.go(host_relative(real))
        elif folder:  # (View, Edit, the hex editor: as on the folder itself - F3 and F4 entered it, "not followed" was
            # said of a link Open follows)
            self.page.message.setText(tr("{name} is a folder.", name=printable(entry.name)))
        elif plain:
            self.page.open_editor(self.page.local, host_relative(real), real, mode, read_only)
        else:
            self.page.message.setText(not_followed)

    def select(self, names):
        select_keys(self.table, list(names))

    # -- the free space, the folders visited recently --

    def show_space(self, space, unknown=False):
        """SPACE: (free, total) bytes of the file system of the folder shown, or None (not known: said so where UNKNOWN -
        a remote system's SFTP server that does not tell -, else nothing)."""
        if space is None or space[1] <= 0:
            self.space.setText(tr("Free space: not known (the SFTP server does not tell)") if unknown else "")
            return
        free, total = space
        self.space.setText(tr("{free} free of {total}", free=files.size_text(free), total=files.size_text(total)))

    @staticmethod
    def kept_recent():
        """The host pane's recent folders kept in the app's state: this machine's (absolute paths) and those of
        sandbox-ai's hosts ([host:NAME, relative path]); anything else is dropped."""
        kept = app_state.current()
        values = kept.get(RECENT_KEY, [], list) if kept is not None else []
        found = []
        for value in values:
            if isinstance(value, str) and os.path.isabs(value) and len(value) < 4096 and "\0" not in value:
                found.append(value)
            elif isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, str) for v in value) and \
                    value[0].startswith(HOST_PREFIX) and len(value[0]) + len(value[1]) < 4096 and \
                    not any(c in "".join(value) for c in "\0\n\r") and ".." not in value[1].split("/"):
                found.append((value[0], value[1].strip("/")))
        return list(dict.fromkeys(found))[:RECENT]

    def recent_key(self, relative):
        """What the Recent menu keeps of the folder RELATIVE: this machine's absolute path; elsewhere (the place as the
        chooser has it - a sandbox's folder, a host by name, a remote system -, relative), kept while it is in the
        chooser (the hosts by name: always; so per host)."""
        if self.mode() == "host":
            return self.path_at(relative)
        folder = host_folder_parts(self.roots.currentData())
        if folder is not None:  # (a host's sandbox's folder: a folder of that host, relative to its "/")
            return (HOST_PREFIX + folder[0], relative)
        return (self.roots.currentData(), relative)

    def remember(self, relative):
        """The folder RELATIVE was shown: first in the Recent menu (once, at most RECENT)."""
        if self.root is None or (self.mode() != "host" and not self.roots.currentData()):
            return
        key = self.recent_key(relative)
        self.recent = [key, *(entry for entry in self.recent if entry != key)][:RECENT]
        if self.side == "host":
            kept = app_state.current()
            if kept is not None:  # (this machine's folders and the hosts' by name: a connection's ssh:// key is not)
                kept.set(RECENT_KEY, [entry if isinstance(entry, str) else list(entry) for entry in self.recent
                                      if isinstance(entry, str) or entry[0].startswith(HOST_PREFIX)])

    def forget_roots(self):
        """A sandbox's folders that left the chooser leave the Recent menu too (this machine's folders stay)."""
        listed = {self.roots.itemData(i) for i in range(self.roots.count())} | \
            {path for _kind, _label, path in self.sandbox_roots}  # (also those the host filter does not list now)
        self.recent = [entry for entry in self.recent if isinstance(entry, str) or entry[0] in listed]

    def recent_entries(self):
        """[(text shown, key)] of the Recent menu: the latest first, not the folder shown now; a sandbox's folders only
        while their root is in the chooser; a host's as NAME:/path."""
        here = self.recent_key(self.relative) if self.root is not None else None
        found = []
        for key in self.recent:
            if key == here:
                continue
            if isinstance(key, str):
                found.append((printable(key), key))
            elif key[0].startswith(HOST_PREFIX) and self.roots.findData(key[0]) >= 0:
                found.append((printable(f"{key[0][len(HOST_PREFIX):]}:/{key[1]}"), key))
            elif self.roots.findData(key[0]) >= 0:  # (a remote system's root ends in "/": ssh://you@host/)
                found.append((printable(key[0].rstrip("/") + "/" + key[1] if key[1] else key[0]), key))
        return found

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is getattr(self, "quick", None) and event.type() == QEvent.Type.KeyPress and \
                event.key() == Qt.Key.Key_Escape and not event.modifiers():
            self.quick.clear()
            self.table.setFocus(Qt.FocusReason.ShortcutFocusReason)
            return True
        if event.type() in DRAG_EVENTS and watched in (getattr(self, "path", None), getattr(self, "roots", None)):
            return self.page.bar_drag(self, watched, event)  # (onto the path bar or the chooser)
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
        if isinstance(key, str):
            if self.mode() != "host":  # (this machine's folder: this machine chosen first)
                index = self.roots.findData(LOCAL)
                if index < 0:
                    return False
                self.roots.setCurrentIndex(index)
            return self.go_path(key, exact=True)
        root_path, relative = key
        index = self.roots.findData(root_path)
        if index < 0:
            self.recent = [entry for entry in self.recent if entry != key]
            self.page.message.setText(tr("{path} is no longer in the list of folders.", path=printable(root_path)))
            return False
        if self.roots.currentIndex() != index:
            self.after_connect = (root_path, relative)  # (a host not connected yet: its folder once it is)
            self.roots.setCurrentIndex(index)  # (choose_root: that root, from its top)
            if self.roots.currentData() != root_path:
                return False  # (connecting: choose_root shows the folder once it is connected)
            self.after_connect = None
        if self.root is None:
            if self.opening is not None:
                # (a folder on a file system slower than a moment is opened in a thread: its folder RELATIVE is listed
                # once it is open - Recent gave up, and the folder's top was shown)
                self.after_open = (self.open_token, relative)
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
        self.workspace_request = None  # the window: (host, path) for a running session (MainWindow.add_workspace_from)
        self.others, self.last_report = [], []
        self.listing_notes, self.before_listing = {}, ""  # (listing_says: the panes' "Listing…" notes, what they replaced)
        # {host folder: its path in the sandbox} of the running sandboxes and sessions (backend.sandbox_paths, read in
        # the background with the list of folders): Copy relative path and Copy path in the sandbox.
        self.sandbox_paths = {}
        # {(st_dev, st_ino): folder} of the sandboxes' folders (keep_identities): read in threads - with the list of
        # folders and every listing -, never on the GUI thread (a folder on a file system that hangs held up the window);
        # those of the runs before kept in the app's state (kept_identities), with the day each was seen last
        self.identities, self.identity_days = kept_identities()
        # Connected remote systems ({key: remote.RemoteRoot}, in the sandbox pane's chooser) and how to reach them:
        # the ssh program (tests: a command that speaks SFTP), the known_hosts and ssh_config files (None: the user's).
        self.remotes, self.ssh_program, self.known_hosts, self.ssh_config = {}, "ssh", None, None
        # {name: the key of its connection in REMOTES} of sandbox-ai's hosts chosen by name in a pane (connect_host)
        self.host_keys = {}
        # The folders of the sandboxes on sandbox-ai's hosts (read_host_folders: one call per host, in threads): {name:
        # (state, [(kind, label, path)], why)} - "reading" (what was read before stays listed meanwhile), "listed",
        # "failed" (no answer in time: its section says so and why); a host whose sandbox-ai is older is left out (its
        # own entry still works). STALE: to be read once the page is shown (no SSH for a page nobody looks at).
        self.host_folders, self.host_tokens, self.hosts_stale = {}, {}, True
        self.pending_start = None  # (host, folder) a terminal tab's "File manager" waits for (show_start)
        # The left pane's host shown once its sandboxes' folders come ((host, the place shown then), or None): its host
        # filter set to a host whose folders were still on their way (follow_target) - unless another was chosen since.
        self.follow_waiting = None
        # Whose sandboxes the left pane offers: all hosts, this machine or one of sandbox-ai's hosts - the Sandboxes
        # page's host, followed as the other pages follow it (ui.FOLLOWERS, host_choice.HostFollow); shown once there is
        # a host of sandbox-ai's.
        self.host_box = QComboBox()
        self.host_box.setAccessibleName(tr("Sandboxes of"))
        self.host_box.setToolTip(tr("Whose sandboxes the left pane offers: of all hosts, of this machine or of one of "
                                    "sandbox-ai's hosts (their folders over SSH)"))
        self.host_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.host_box.addItem(tr("All hosts"), None)
        self.host_box.addItem(tr("This machine"), LOCAL_HOST)
        self.host_box.currentIndexChanged.connect(lambda _=0: self.host_filter_changed())
        self.follow_note = follow_note()
        # This machine (the human's own): what the right pane shows first, and either pane when "This machine" is chosen.
        self.local = files.Root("/", label=tr("This machine"), sandbox=False)
        self.ranges = files.subordinate_ranges()
        self.message = plain_label(selectable=True, log="files")
        # Copied or cut items (Ctrl+C, Ctrl+X), what is dragged now, the editors and dialogs open.
        self.clipboard, self.drag, self.editors, self.dialogs = None, None, [], []
        # While a drag of this window goes on: what the status line says of what other programs get of it (DRAG_NOTE),
        # why the last drop was refused (DROP_REFUSED), whether a drop here took it (DROP_TAKEN) - start_drag.
        self.drag_note, self.drop_refused, self.drop_taken = None, None, False
        self.left = Pane(self, "sandbox")
        self.right = Pane(self, "host")
        self.active = self.left
        layout = QVBoxLayout(self)
        self.host_row = QWidget()
        row = QHBoxLayout(self.host_row)
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(tr("Sandboxes of"))
        label.setBuddy(self.host_box)
        row.addWidget(label)
        row.addWidget(self.host_box)
        row.addWidget(self.follow_note, 1)
        layout.addWidget(self.host_row)
        self.fill_host_box()
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
        # The search windows ({pane side: file_search_view.SearchDialog}): Ctrl+Shift+F anywhere in the page, the
        # panes' search buttons and their context menus.
        self.searches = {}
        QShortcut(QKeySequence("Ctrl+Shift+F"), self, context=Qt.ShortcutContext.WidgetWithChildrenShortcut,
                  activated=lambda: self.search_dialog())
        self.task.cancel_button.clicked.connect(lambda *_: self.cancelling())
        self.job_roots = []  # the roots of the job running (Cancel interrupts a remote one's stalled request)
        layout.addWidget(self.task)
        layout.addWidget(self.message)
        layout.addWidget(explanation(tr("Symbolic links are shown but never followed or copied; copies stop at {limit} "
                                        "MiB.", limit=files.LIMIT >> 20)))
        self.progress, self.progress_text = None, ""
        self.timer = QTimer(self)
        self.timer.setInterval(PROGRESS_INTERVAL)
        self.timer.timeout.connect(self.show_progress)
        self.refresh_roots()
        self.right.list()
        self.mark_active()
        follow_hosts(self, self.hosts_changed)  # a host added, renamed or removed on the Hosts page: here at once

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
        """Shown with its real width at last: the panes half and half, unless their division was moved or kept. The
        hosts' sandboxes' folders read now if they were not while it was hidden."""
        super().showEvent(event)
        if not self.keep_split:
            QTimer.singleShot(0, self, self.halve)  # once the layout gave the splitter its width
        if self.hosts_stale:
            self.read_host_folders()

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

    def show_start(self, host, folder):
        """The left pane on a start's folder FOLDER (a terminal tab's "File manager"): on this machine its folder among
        the sandboxes' folders; on sandbox-ai's host HOST that folder of its sandboxes (`list --folders` there, read
        first when it has not been: selected once it came; a host that does not list it says so)."""
        if not folder:
            return False
        if host in (None, LOCAL_HOST):
            self.pending_start = None
            self.refresh_roots(select=real_folder(folder))
            return True
        _state, roots, _why = self.host_folders.get(host, (None, [], None))
        if any(root[2] == folder for root in roots):
            self.pending_start = None
            self.left.show_places(select=host_folder(host, folder))
            return True
        self.pending_start = (host, folder)
        self.message.setText(tr("Asking {host} for its sandboxes' folders…", host=printable(host)))
        self.read_host_folders()
        return True

    def set_project(self, path):
        """Show the current project's folder (its real path read as real_folder reads it: on a file system that hangs, the
        window waits no longer than a moment)."""
        self.refresh_roots(select=real_folder(path))

    # -- the panes --

    def other(self, pane):
        return self.right if pane is self.left else self.left

    def folder_sources(self):
        """(the sandbox side's folders, the projects' and folders chosen as given): what sandbox_folders and
        resolve_folders take - read from the widgets, no file system asked (the GUI thread)."""
        # (the folders read last, also those the host filter does not list now: their files stay an agent's)
        listed = [str(path) for _kind, _label, path in self.left.sandbox_roots if path and str(path).startswith("/")]
        try:
            known = [str(entry[2]) for entry in [*self.projects(), *self.others]
                     if isinstance(entry, (tuple, list)) and len(entry) > 2 and entry[2]]
        except Exception as _exc:  # noqa: BLE001 - the list alone, as before
            applog.swallowed("files_page", "FilesPage.folder_sources: the list alone, as before", _exc)
            known = []
        return listed, known

    def sandbox_folders(self):
        """The folders an agent may write: those of the sandbox side's list, and the projects and folders chosen,
        known at once (the list is read in the background: until it came, every host folder counted as the host's
        own - a workspace opened in the host pane meanwhile kept the agent's exec bits and followed its links). Their
        real paths as read last in a thread (real_folder: never held up by a folder on a file system that hangs)."""
        listed, known = self.folder_sources()
        return list(dict.fromkeys(listed + [real_folder(path) for path in known]))

    def keep_identities(self, found):
        """FOUND ({(st_dev, st_ino): folder} of the sandboxes' folders, read in a thread) kept for the GUI thread - and
        those seen before stay: a sandbox's folder moved or renamed on this machine (by a rename: the same folder) is
        still the agent's, under its new name (it became the human's own: its links followed, its programs copied
        executable) - also when the app is started again: kept in the app's state with the day each was seen last
        (kept_identities; a listing in a moved one sees it too)."""
        day, changed = today(), False
        for identity, folder in (found or {}).items():
            if identity not in self.identities:
                self.identities[identity], changed = folder, True
            if self.identity_days.get(identity) != day:
                self.identity_days[identity], changed = day, True
        kept = app_state.current()
        if kept is not None and (changed or id(kept) != getattr(self, "identities_saved", None)):
            self.identities_saved = id(kept)  # (in each state at least once: one made after the page too)
            latest = sorted(self.identities, key=lambda identity: -self.identity_days.get(identity, 0))
            kept.set(IDENTITIES_KEY, [[*identity, self.identities[identity], self.identity_days.get(identity, day)]
                                      for identity in latest[:KEPT_IDENTITIES]])

    def identities_reader(self):
        """A function for a thread: the identities of the sandboxes' folders read now (resolve_folders, with the
        sources taken here on the GUI thread) and those kept (keep_identities)."""
        sources, kept = self.folder_sources(), dict(self.identities)

        def read():
            return {**kept, **resolve_folders(*sources)[1]}
        return read

    def below_sandbox(self, path):
        return self.sandbox_holding(path) is not None

    def sandbox_holding(self, path, unknown=None):
        """The outermost sandbox's folder that holds the absolute host PATH, by name or as the folder itself under
        another path (holding_folder: a bind mount of it), as it lies on PATH's way; or None. The folders on PATH's way
        are looked at in a thread (answered; on the GUI thread they held up the window): for one that does not answer
        (a file system that hangs) UNKNOWN, by default PATH itself - as a folder that lies in one."""
        if not path:
            return None
        folders, identities = self.sandbox_folders(), dict(self.identities)
        by_name = holding_folder(path, folders)  # (by the names alone: no file system asked)
        if by_name is not None or not identities:
            return by_name
        done, found = answered(lambda: holding_folder(path, folders, identities),
                               key=answer_key("holding", path, folders, identities))
        return found if done else path if unknown is None else unknown

    def held_now(self, path):
        """Whether a sandbox's folder of now holds the host PATH: by its path (sandbox_folders), or under another path by
        its identity as read now (resolve_folders, in a thread: answered) - not by one kept from before (keep_identities:
        a folder moved since, its sandbox gone; or a device and inode number met again by chance - a USB stick in
        place of another, the disks numbered anew after a reboot, the first folder of every fresh ext4 is inode 12).
        Not known in time: False - what a target is taken for where the human's is the safe side (transfer_items)."""
        folders, sources = self.sandbox_folders(), self.folder_sources()
        if holding_folder(path, folders) is not None:
            return True

        def look():
            known, identities = resolve_folders(*sources)
            return holding_folder(path, list(dict.fromkeys([*folders, *known])), identities) is not None
        done, found = answered(look, key=answer_key("held", path, folders, sources))
        return bool(done and found)

    def through_sandbox_link(self, path, limit=40):
        """Whether the absolute host PATH (as typed or given: not resolved) is resolved through a symbolic link that lies
        in a sandbox's folder (through_link, in a thread: answered - one that does not answer counts as through one).
        Only the path as typed was compared with the sandboxes' folders: through another path to the same folder, the
        agent's link was followed."""
        folders, identities = self.sandbox_folders(), dict(self.identities)
        done, found = answered(lambda: through_link(path, folders, identities, limit),
                               key=answer_key("through", path, folders, identities, limit))
        return found if done else True

    def remote_hosts(self):
        """The names of the other hosts (`sandbox-ai hosts`), as kept on this machine (no SSH)."""
        try:
            return sorted(self.sai.host_statuses(cached=True) or {})
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai, no hosts: none shown
            applog.swallowed("files_page", "FilesPage.remote_hosts: an older sandbox-ai, no hosts: none shown", _exc)
            return []

    # -- the sandboxes of sandbox-ai's hosts: their folders in the left chooser, whose are offered --

    def sandbox_hosts(self):
        """The names of sandbox-ai's hosts whose sandboxes' folders may be listed (remote_hosts, this machine and a name
        that could read as an option left out)."""
        return [name for name in self.remote_hosts() if name != LOCAL_HOST and valid_host(name) == name]

    def host_filter(self):
        """Whose sandboxes the left pane offers: None (all hosts), LOCAL_HOST or a host's name."""
        box = getattr(self, "host_box", None)
        return box.currentData() if box is not None and box.count() else None

    def fill_host_box(self):
        """All hosts, this machine and each host of `sandbox-ai hosts`; the choice stays while its host does (one that
        left: all hosts). Shown only when there is a host of sandbox-ai's; filled again only when the hosts changed."""
        names, box = self.sandbox_hosts(), self.host_box
        shown = [box.itemData(index) for index in range(2, box.count())]
        if shown != names:
            chosen = box.currentData()
            box.blockSignals(True)
            while box.count() > 2:
                box.removeItem(2)
            for name in names:
                box.addItem(printable(name), name)
            index = box.findData(chosen) if chosen is not None else 0
            box.setCurrentIndex(max(index, 0) if names else 0)
            box.blockSignals(False)
            if box.currentData() != chosen:
                self.host_filter_changed()
        self.host_row.setVisible(bool(names))

    def host_filter_changed(self):
        """The left pane offers the sandboxes of the host chosen (by the human here, or as the Sandboxes page's host:
        HostFollow) - and shows that host's: what it shows stays when it is that host's, else that host's first
        sandbox's folder (this machine's: its first; a host's whose folders are on their way: the host itself until
        they come, then its first)."""
        self.follow_waiting = None
        left = getattr(self, "left", None)
        if left is None:
            return
        left.show_places(select=self.follow_target(self.host_filter()))

    def follow_target(self, host):
        """The entry of the left chooser to show for HOST chosen as whose sandboxes it offers: its first sandbox's
        folder. None: what it shows stays - HOST None (all hosts), what it shows is HOST's already, or HOST has no
        sandbox's folder (its section says so); while HOST's folders are on their way (or not read yet: the page was
        hidden), its first once they come, unless something else was chosen meanwhile (follow_waiting)."""
        left = self.left
        if host is None or host_of(left.place if left.root is not None else None) == host:
            return None
        if host == LOCAL_HOST:
            return left.sandbox_roots[0][2] if left.sandbox_roots else None
        state, roots, _why = self.host_folders.get(host, (None, [], None))
        if roots:
            return host_folder(host, roots[0][2])
        if state in (None, "reading") and host in self.sandbox_hosts():
            self.follow_waiting = (host, left.place)
        return None

    def read_host_folders(self):
        """The folders of the sandboxes on each of sandbox-ai's hosts, read again: one call per host
        (backend.host_file_roots), each in a thread of its own with a time limit - a host that does not answer holds up
        neither the window nor the list (its section says so once its time is up). What a host listed before stays
        listed while it is read anew. Only while the page is shown (hidden: once it is shown next)."""
        if not self.isVisible():
            self.hosts_stale = True
            return
        self.hosts_stale = False
        names = self.sandbox_hosts()
        reader = getattr(self.sai, "host_file_roots", None)
        for name in [name for name in self.host_folders if name not in names or reader is None]:
            del self.host_folders[name]
        for name in names if reader is not None else []:
            token = self.host_tokens[name] = self.host_tokens.get(name, 0) + 1
            _state, roots, _why = self.host_folders.get(name, (None, [], None))
            self.host_folders[name] = ("reading", roots, None)
            background(lambda name=name: reader(name, timeout=HOST_FOLDERS_WAIT), owner=self, work=False,
                       timeout=HOST_FOLDERS_WAIT * 1.5, what=f"the folders of {name}'s sandboxes",
                       done=lambda found, error, name=name, token=token: self.host_folders_read(name, token, found,
                                                                                               error))
        self.left.show_places()

    def host_folders_read(self, name, token, found, error):
        """Host NAME's sandboxes' folders came (FOUND), or not (ERROR): its section of the left chooser anew."""
        if self.host_tokens.get(name) != token or name not in self.host_folders:
            return False  # (read again meanwhile, or the host is gone)
        if error is None and found is None:
            del self.host_folders[name]  # (its sandbox-ai is older: the host's own entry only)
        elif error is not None:
            why = tr("no answer within {seconds} s", seconds=HOST_FOLDERS_WAIT) if isinstance(error, TimeoutError) \
                else printable(error)[:300]
            applog.log("INFO", "files", f"the folders of {name}'s sandboxes: not read ({applog.redact(why)})")
            self.host_folders[name] = ("failed", [], why)
        else:
            self.host_folders[name] = ("listed", list(found), None)
        waiting, self.follow_waiting = self.follow_waiting, None
        select = None
        pending = getattr(self, "pending_start", None)
        if pending is not None and pending[0] == name:  # a terminal tab's "File manager" waited for this host
            self.pending_start = None
            if any(root[2] == pending[1] for root in self.host_folders.get(name, (None, []))[1]):
                self.left.show_places(select=host_folder(name, pending[1]))
                self.message.setText("")
                return True
            self.message.setText(tr("{host} does not list {folder} among its sandboxes' folders.",
                                    host=printable(name), folder=printable(pending[1])))
        if waiting is not None and waiting[0] == name and self.host_filter() == name and \
                self.left.place == waiting[1] and self.host_folders.get(name, (None, []))[1]:
            select = host_folder(name, self.host_folders[name][1][0][2])  # (the host's first, as it was followed)
        elif waiting is not None and waiting[0] != name:
            self.follow_waiting = waiting
        self.left.show_places(select=select)
        return True

    def host_sections(self, hosts=None):
        """[(heading, [(text, data, tooltip, enabled)])] of the sandboxes' folders of each of sandbox-ai's hosts (HOSTS:
        a set of the hosts' whose alone; None: all) for the left chooser: "lab: Running sandbox NAME: workspace
        (/path)", or a note while they are read the first time, when none runs or is kept there, or when the host did
        not answer (and why)."""
        sections = []
        for name in sorted(self.host_folders):
            if hosts is not None and name not in hosts:
                continue
            state, roots, why = self.host_folders[name]
            host = printable(name)
            tip = tr("A sandbox's folder on {host}: its files over SSH and SFTP; they count as an agent's", host=host)
            items = [(tr("{host}: {folder}", host=host, folder=root_title(kind, label, path)), host_folder(name, path),
                      tip, True) for kind, label, path in roots]
            if state == "failed":
                items = [(tr("{host}: its sandboxes are not available ({why})", host=host, why=why), None, why, False)]
            elif not items:
                items = [(tr("{host}: reading its sandboxes…", host=host) if state == "reading" else
                          tr("{host}: no sandbox runs or is kept there", host=host), None, None, False)]
            sections.append((tr("Sandboxes on {host}", host=host), items))
        return sections

    def hosts_changed(self):
        """The list of sandbox-ai's hosts changed (the Hosts page, the hosts file): the host filter, both choosers'
        places and the hosts' sandboxes' folders anew (read now while the page is shown, else once it is)."""
        self.fill_host_box()
        self.right.show_places()
        self.read_host_folders()
        if not self.isVisible():
            names = self.sandbox_hosts()
            for name in [name for name in self.host_folders if name not in names]:
                del self.host_folders[name]
            self.left.show_places()

    def mark_active(self):
        for pane in (self.left, self.right):
            font = pane.title.font()
            font.setBold(pane is self.active)
            pane.title.setFont(font)
        # (not while a job runs: a focus change re-enabled it, which running() had switched off)
        busy = getattr(self, "task", None) is not None and self.task.running
        set_enabled(self.buttons["own"], self.active.mode() == "sandbox" and not busy,
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
            # but Ctrl+Shift+C, Copy relative path. (macOS: Cmd+Backspace, Delete.)
            if key == Key.Key_C and plain & Qt.KeyboardModifier.ShiftModifier and \
                    not plain & Qt.KeyboardModifier.AltModifier:
                return True
            return not plain & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier) and \
                key in (Key.Key_A, Key.Key_C, Key.Key_R, Key.Key_V, Key.Key_X, *((Key.Key_Backspace,) if MACOS else ()))
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
        if MACOS and control and not (shift or alt) and key == Key.Key_Backspace:
            key, control = Key.Key_Delete, False  # Cmd+Backspace: Delete, as in the Finder (it went up a folder)
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
        job began (one at a time, as copies are). Never while a job runs: a rename, a new folder or the permissions
        asked for before it began (their dialogs block nothing) changed what a copy or move was working on."""
        if self.task.running:
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
            return False
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
            # (whatever the job running is - a delete, a permission change of Properties: "Another copy still runs." was
            # said of a copy there was none of)
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
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

    def transfer_items(self, clip, target, target_dir, move=False, began=None, asked=False):
        """Copy (MOVE: move) the items of CLIP into the folder TARGET_DIR of the TARGET pane, in a thread: through
        files.copy_entry and files.move_entry (links not followed, budgets, Cancel; replacing asks first). BEGAN(): once
        the job has begun - after the question about what exists there, which may come after this returns (a remote
        target, a file system slower than a moment). Into one of sandbox-ai's hosts from anywhere else only after a
        question that names what goes there (ASKED: copy_paths asked it already); between two remote systems through
        this machine (remote.relay)."""
        if target.root is None:
            self.message.setText(tr("Choose a folder in both panes first."))
            return False
        if self.task.running:  # (a drop is neither a key nor a button: it asked, then found the job running)
            self.message.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
            return False
        src_root, dst_root, untrusted, items = clip.root, target.root, clip.untrusted, list(clip.items)
        refused = list(clip.problems)  # (paths refused before: named in the report too)
        target_text = target.path_at(target_dir)
        target_untrusted = target.untrusted_at(target_dir, unknown=None)  # (before the question, as the rest)
        if target_untrusted is None:  # (a host folder on a file system that hangs: neither the agent's nor the human's)
            self.message.setText(tr("{path} does not answer (a file system that hangs?); nothing was copied.",
                                    path=printable(target_text)))
            return False
        # (a remote system's files counted as an agent's since they were copied or cut - the box ticked meanwhile: they
        # came executable, as the choice of the moment of Ctrl+C had it; the stricter choice counts)
        untrusted = untrusted or bool(getattr(src_root, "agents", False))
        remote = getattr(src_root, "remote", False) or getattr(dst_root, "remote", False)
        if target_untrusted and not remote and target.mode() == "host":
            # (between this machine's folders, a target is an agent's only where a sandbox's folder of now holds it -
            # held_now: one kept from before may be a folder of the human's whose device and inode met one by chance,
            # and what came out of a sandbox's folder went in as within the sandboxes' folders, executable, unnamed)
            target_untrusted = self.held_now(target_text)
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
        # (read in the job's thread: identities_reader)
        agents = frozenset() if target_untrusted or getattr(src_root, "remote", False) else self.identities_reader()
        if not asked and getattr(dst_root, "remote", False) and not same_connection(src_root, dst_root) and \
                self.sandbox_host(dst_root):
            # Sandboxes run on sandbox-ai's hosts and their agents may read what is copied there (as a drop of another
            # program's files there asks): named, asked first.
            host = self.host_name(dst_root)
            if not confirm(
                    self, tr("Move to {host}?", host=host) if move else tr("Copy to {host}?", host=host),
                    tr("{count} item(s) from {source} would go into {folder} on {host}, a host of sandbox-ai's where "
                       "sandboxes run and agents may read them. Go ahead?", count=len(items),
                       source=printable(clip.folder_text), folder=printable(target_text), host=host),
                    tr("Move to {host}", host=host) if move else tr("Copy to {host}", host=host),
                    details="\n".join(printable(item_text(src_root, path)) for _name, path, _new in items)):
                self.message.setText(tr("Nothing was copied."))
                return False
            if target.root is not dst_root:
                # (another folder or system chosen while it asked: the answer was about this one)
                self.message.setText(tr("The folder shown changed meanwhile; nothing was done."))
                return False

        def then(overwrite):
            started = self.start_transfer(src_root, dst_root, items, target_dir, target_text, overwrite, untrusted,
                                          target_untrusted, refused, out, move, agents)
            if started and began is not None:
                began()
            return started
        return self.ask_replace([new or name for name, _, new in items], clip.folder_text, dst_root, target_dir,
                                target_text, then=then)

    def start_transfer(self, src_root, dst_root, items, target_dir, target_text, overwrite, untrusted,
                       target_untrusted, refused, out, move, agents=frozenset()):
        """The copy or move of transfer_items, once it is known whether to replace (OVERWRITE). AGENTS: the sandboxes'
        folders' identities, or a function that reads them (in the job's thread)."""
        progress = files.Progress()

        def work(cancelled):
            done, problems = [], list(refused)
            found = frozenset(agents()) if callable(agents) else agents
            for name, path, new in items:
                try:
                    if move:
                        done.append(files.move_entry(src_root, path, dst_root, target_dir, overwrite=overwrite,
                                                     untrusted=untrusted, target_untrusted=target_untrusted,
                                                     cancelled=cancelled, progress=progress, agents=found))
                    else:
                        done.append(files.copy_entry(src_root, path, dst_root, target_dir, overwrite=overwrite,
                                                     untrusted=untrusted, name=new, cancelled=cancelled,
                                                     progress=progress, agents=found))
                except files.Cancelled as exc:
                    problems.append(str(exc))
                    break
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems
        # (the folder's name as every other on the screen: an agent's line break or escape sequence shown escaped)
        if getattr(src_root, "remote", False) and getattr(dst_root, "remote", False) and \
                not (move and same_connection(src_root, dst_root)):
            # (between two remote systems: fetched to this machine first, then sent on - remote.relay; the progress
            # counts what came here)
            where = printable(dst_root.display(target_dir))
            text = (tr("Moving {count} item(s) to {target} through this machine…", count=len(items), target=where)
                    if move else tr("Copying {count} item(s) to {target} through this machine…", count=len(items),
                                    target=where))
        else:
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
        # (a move whose source was kept as a whole - something in it skipped or changed, or cancelled after the copy -
        # was copied only: it was counted as moved, "... moved", beside "The source was kept")
        whole = [r for r in done if not moved or r.moved or r.left]
        kept = [r for r in done if moved and not (r.moved or r.left)]
        for part, text in ((whole, N_("{files} file(s) in {folders} folder(s), {size} moved") if moved else
                            N_("{files} file(s) in {folders} folder(s), {size} copied")),
                           (kept, N_("{files} file(s) in {folders} folder(s), {size} copied (the source was kept)"))):
            if part:
                lines.append(tr(text, files=sum(r.files for r in part), folders=sum(r.directories for r in part),
                                size=files.size_text(sum(r.bytes for r in part))))
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
        """This machine's Root("/") (the old page's copies to the host, and files of another program: host_items)."""
        return self.local

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
        folders, identities = self.sandbox_folders(), dict(self.identities)
        given = [os.path.normpath(os.path.abspath(os.fspath(path))) for path in paths]

        def look():  # (in a thread - answered: files of a file system that hangs held up the window)
            items, problems, untrusted = [], [], False
            for path in given:
                name, folder = os.path.basename(path), os.path.dirname(path)
                if not name or path == "/":
                    problems.append(tr("Choose a file or folder to copy."))
                    continue
                real = os.path.realpath(folder)
                if real != folder and through_link(folder, folders, identities):
                    problems.append(tr("{path} goes through a symbolic link in a sandbox's folder; it is not "
                                       "followed.", path=printable(path)))
                    continue
                whole = os.path.join(real, name)
                untrusted = untrusted or holding_folder(whole, folders, identities) is not None
                items.append((name, host_relative(whole), None))
            return items, problems, untrusted
        try:
            done, found = answered(look, key=answer_key("items", given, folders, identities, language()))
        except (OSError, ValueError) as exc:
            done, found = True, ([], [printable(exc)], True)
        items, problems, untrusted = found if done else ([], [tr(
            "{path} does not answer (a file system that hangs?); nothing was copied.",
            path=", ".join(printable(path) for path in given[:3]) + (" …" if len(given) > 3 else ""))], True)
        clip = Clip(self.local, items, untrusted, tr("this machine"))
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
        lab = foreign and target.remote() and self.sandbox_host(target.root)
        if lab and not confirm(
                self, tr("Copy files of another program to {host}?", host=printable(target.root.label)),
                tr("{count} item(s) that another program put on the clipboard or dropped here would be copied into "
                   "{folder} on {host}, a host of sandbox-ai's where sandboxes run and agents may read them. An agent "
                   "that reaches your desktop can put any of your files there. Copy them?", count=len(clip.items),
                   folder=printable(target.path_at(target_dir)), host=printable(target.root.label)),
                tr("Copy to {host}", host=printable(target.root.label)),
                details="\n".join(printable(os.path.join("/", path)) for _name, path, _new in clip.items)):
            self.message.setText(tr("Nothing was copied."))
            return False
        # (a remote system whose files count as an agent's: asked as one - unless it was asked about as sandbox-ai's host)
        inside = target.untrusted_at(target_dir, unknown=None)
        if inside is None:  # (a host folder on a file system that hangs: said before the question, as transfer_items)
            self.message.setText(tr("{path} does not answer (a file system that hangs?); nothing was copied.",
                                    path=printable(target.path_at(target_dir))))
            return False
        if foreign and not lab and inside and not confirm(
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
        return self.transfer_items(clip, target, target_dir, asked=bool(lab))

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
        # (a link to a folder as the folder itself: F3 and F4 entered it, on the folder they say it is one)
        pane.open_entry(entry, mode, read_only, enter=False)
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
                                                                              True),
                              top=self.markdown_top(root, relative), dropped=self.editor_drop)
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
        relative, text, untrusted, sandbox = pane.relative, pane.path_text(), pane.untrusted(), pane.mode() == "sandbox"
        root = pane.root
        # (the sandboxes' folders' identities read in the thread too: identities_reader)
        reader = None if getattr(root, "remote", False) else self.identities_reader()
        return on_root(root, lambda: (files.entry_infos(root, items), reader() if reader is not None else {}),
                       lambda found, error: self.show_properties(pane, root, items, text, untrusted, sandbox, relative,
                                                                 *(found if error is None else (None, None)), error),
                       owner=self)

    def show_properties(self, pane, root, items, text, untrusted, sandbox, relative, found, agents, error):
        """The Properties dialog of ITEMS, with what files.entry_infos FOUND (in a thread for a remote root) and the
        sandboxes' folders' AGENTS (identities, read there too)."""
        from .file_properties import PropertiesDialog
        if error is not None:
            self.message.setText(printable(error))
            return None
        dialog = PropertiesDialog(self, root, items, text, untrusted, changed=self.refresh_panes, infos=found,
                                  agents=frozenset(agents or ()), busy=lambda: self.task.running, job=self.root_job,
                                  owner=None if getattr(root, "remote", False) else lambda root, shown, folders: self.ask_owner(Chosen(
                                      root, shown, relative, text, untrusted, sandbox, folders)))
        self.keep(self.dialogs, dialog)
        dialog.show()
        return dialog

    # -- the search --

    def search_dialog(self, pane=None):
        """The search below the folder PANE (the active one) shows: Ctrl+Shift+F, the pane's search button, its context
        menu. One window a pane, brought to the front again - aimed at the folder shown unless a search runs in it."""
        from .file_search_view import SearchDialog
        pane = pane or self.active
        if pane.root is None:
            self.message.setText(tr("Choose a folder first."))
            return None
        self.activate(pane)
        dialog = self.searches.get(pane.side)
        if dialog is None or not isValid(dialog):
            dialog = self.searches[pane.side] = SearchDialog(self, pane)
        else:
            dialog.aim(pane)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.names.setFocus(Qt.FocusReason.ShortcutFocusReason)
        dialog.names.selectAll()
        return dialog

    # -- remote systems (remote.py: SSH and SFTP) --

    def sandbox_targets(self):
        """[(name, remote.Target)] of sandbox-ai's hosts (`sandbox-ai hosts`: those reached over SSH)."""
        from . import remote
        found = []
        try:
            entries = self.sai.host_entries()
        except Exception as _exc:  # noqa: BLE001 - an older sandbox-ai, no hosts file: none
            applog.swallowed("files_page", "FilesPage.sandbox_targets: an older sandbox-ai, no hosts file: none", _exc)
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

    def places(self):
        """[(text, data, tooltip)] of where a pane may list besides a sandbox's folders: this machine, sandbox-ai's hosts
        by name (`sandbox-ai hosts`: connected over SSH when chosen), the remote systems connected with SSH…."""
        found = [(tr("This machine"), LOCAL, tr("This machine's files, from your HOME folder"))]
        targets = dict(self.sandbox_targets())
        for name in self.remote_hosts():
            if name == "local":
                continue
            target = targets.get(name)
            text = tr("Host {name} ({target})", name=printable(name), target=printable(target.text())) if target \
                else tr("Host {name}", name=printable(name))
            found.append((text, HOST_PREFIX + name, tr("A host of sandbox-ai's where sandboxes run: its files over SSH "
                                                       "and SFTP, from its HOME; they count as an agent's")))
        claimed = set(self.host_keys.values())
        for key, root in self.remotes.items():
            if key not in claimed:
                found.append((tr("Remote system {name}", name=printable(root.label)), key, None))
        return found

    def host_root(self, name):
        """The connection of sandbox-ai's host NAME made for a pane (connect_host), or None."""
        key = self.host_keys.get(name)
        return self.remotes.get(key) if key is not None else None

    def host_name(self, root):
        """The remote ROOT as a question names it: "NAME (user@host)" for one of sandbox-ai's hosts, else user@host."""
        label = printable(root.label)
        name = next((name for name, key in self.host_keys.items() if key == getattr(root, "key", None)), None)
        if name is None:
            name = next((name for name, target in self.sandbox_targets() if target == getattr(root, "target", None)),
                        None)
        return f"{printable(name)} ({label})" if name else label

    def connect_host(self, name, pane, select=None):
        """Sandbox-ai's host NAME for PANE: a connection to it that is there already (also one made with SSH…), else one
        made now in the background (connect_remote: its key trusted only after a yes) - PANE shows it once it is (SELECT:
        that entry of its chooser - a folder of a sandbox there -, else the host's own)."""
        target = dict(self.sandbox_targets()).get(name)
        if target is None:
            self.message.setText(tr("{name} has no SSH address among sandbox-ai's hosts (sandbox-ai hosts).",
                                    name=printable(name)))
            return False
        found = next((key for key, root in self.remotes.items() if root.alive and root.target == target), None)
        if found is not None:
            self.host_keys[name] = found
            self.other(pane).show_places()
            pane.refresh_roots(select=select or HOST_PREFIX + name, hosts=False)
            return True
        return self.connect_remote(target, pane, name, select)

    def remote_choices(self):
        """[(text shown, remote.Target)]: sandbox-ai's hosts, then the Host entries of ~/.ssh/config."""
        from . import remote
        found = [(tr("{name} (sandbox-ai host, {target})", name=printable(name), target=target.text()), target)
                 for name, target in self.sandbox_targets()]
        for alias in remote.ssh_config_hosts():
            found.append((tr("{name} (~/.ssh/config)", name=alias), remote.Target(alias)))
        return found

    def remote_dialog(self, pane=None):
        """SSH…: which remote system (a sandbox-ai host, a Host of ~/.ssh/config, or user@host[:port] typed), shown in
        PANE (default: the left one)."""
        choices = self.remote_choices()
        text, ok = QInputDialog.getItem(self, tr("Remote system"), shown_as_text(tr(
            "Connect to (user@host, host:port, or a host below). Keys and your ssh-agent only: no password is asked "
            "for or kept.")), [label for label, _ in choices], 0, True)
        if not ok or not text.strip():
            return False
        return self.connect_remote(dict(choices).get(text) or text.strip(), pane)

    def connect_remote(self, target, pane=None, name=None, select=None):
        """Connect to TARGET (a remote.Target or its text) in the background; PANE (default: the left one) shows it once
        it is - as sandbox-ai's host NAME where it was chosen by name (SELECT: that entry of its chooser then)."""
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
        # (no time limit as a whole: each step has its own - ssh's ConnectTimeout, REQUEST_TIMEOUT a request, 10 s for
        # each `ssh -G`; 120 s for all of them said "No answer" to a connection that was still being made, which nobody
        # took once it was: its ssh ran on unused)
        background(connect, owner=self, work=False,
                   done=lambda root, error: self.connected(target, root, error, pane, name, select))
        return True

    def connected(self, target, root, error, pane=None, name=None, select=None):
        from . import remote
        if isinstance(error, remote.HostKeyUnknown):
            background(self.scan_keys, target, self.known_hosts, self.ssh_config, owner=self, timeout=60, work=False,
                       done=lambda found, problem: self.trust_host(target, found, problem, pane, name, select))
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
        # Whether its files count as an agent's (RemoteRoot.agents): by default for one of sandbox-ai's hosts - its
        # sandboxes' agents may have written any file there (copied to this machine, a program kept its executable bit
        # and an .envrc went unnamed); the human's choice for the connection before stays with one made again.
        root.agents = bool(old.agents) if old is not None and old is not root else self.sandbox_host(root)
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
            for each in (self.left, self.right):  # (a pane that shows it: the same folder, through the new one)
                if each.root is old:
                    each.root = root
        self.remotes[root.key] = root
        if name:
            self.host_keys[name] = root.key
        self.message.setText(tr("Connected to {host}.", host=printable(root.label)))
        pane = pane or self.left
        self.other(pane).show_places()  # (offered there too; what it shows stays)
        pane.refresh_roots(select=select or (HOST_PREFIX + name if name else root.key), hosts=False)
        return True

    @staticmethod
    def scan_keys(target, known, config):
        """(known_hosts lines, their fingerprints, the known_hosts file they go to) of TARGET - in a thread (background):
        the file too, which `ssh -G` names (it ran on the GUI thread, after the yes, and held up the window)."""
        from . import remote
        lines = remote.scan_host(target)
        return lines, remote.fingerprints(lines), known or remote.known_hosts_file(target, config)

    def trust_host(self, target, found, problem, pane=None, name=None, select=None):
        """The host's keys, shown; added to known_hosts (FOUND: (lines, fingerprints, the known_hosts file), read in a
        thread) and connected again only after a yes (for PANE, as NAME: connect_remote)."""
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
        return self.connect_remote(target, pane, name, select)

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
        elif pane.mode() == "sandbox" and pane.root is not None:
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
        exec_dropped(menu, where)  # (made for this one use: freed once it closed - widgets.exec_dropped)

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
            add(tr("View"), lambda: pane.open_entry(one, read_only=True, enter=False), "F3", file_like)
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
            add(tr("Delete…"), lambda: self.delete_selected(pane, entries), "Ctrl+Backspace" if MACOS else "Del",
                not busy)
            # The buttons below the panes that have no entry above: as they are now (for this pane's selection).
            for key in ("chmod", "chown", "own"):
                found = self.buttons.get(key)
                if found is not None and widgets.shown_button(found, pane):
                    add(widgets.button_text(found), found.click, None, found.isEnabled(),
                        None if found.isEnabled() else found.toolTip())
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
        source = self.workspace_source(pane, one) if has_root and (not entries or one is not None) else None
        add(tr("Add to a running session…"), lambda: self.add_to_session(source), None,
            source is not None and getattr(self, "workspace_request", None) is not None,
            tr("Mount it into a session that runs: as a workspace, until the session ends (asked first)")
            if source is not None else tr("Choose one folder or file (or none: the folder shown)"))
        if not entries:
            add(tr("Refresh"), lambda: pane.list(), "Ctrl+R", has_root)
        add(tr("Properties…"), lambda: self.properties(pane, entries), "Alt+Return", has_root,
            None if entries else tr("Of the folder shown"))
        add(tr("Search below this folder…"), lambda: self.search_dialog(pane), "Ctrl+Shift+F", has_root)
        self.places_menu(menu, pane)
        return menu

    def workspace_source(self, pane, entry=None):
        """(host, path) for Add to a running session…: ENTRY (else the folder shown) as `sandbox-ai workspace add` of
        that host takes it - this machine's path for this machine's sessions, a sandbox-ai host's path for that host's,
        a remote system's as sshfs://[user@]machine[:port]/path for this machine's; None for nothing usable."""
        path = pane.path_of_entry(entry) if entry is not None else pane.path_text()
        if not path or not str(path).startswith("/"):
            return None
        if not pane.remote():
            return "local", path
        host = self.sandbox_host_name(pane.root)
        if host is not None:
            return host, path
        target = getattr(pane.root, "target", None)
        if target is None:
            return None
        who = f"{target.user}@" if target.user else ""
        port = f":{target.port}" if target.port else ""
        return "local", f"sshfs://{who}{target.host}{port}{path}"

    def sandbox_host_name(self, root):
        """The name in `sandbox-ai hosts` of the remote ROOT when it is one of sandbox-ai's hosts, else None."""
        if not self.sandbox_host(root):
            return None
        target = getattr(root, "target", None)
        endpoint = getattr(root, "endpoint", None)
        names = {target.host.lower(), *([endpoint[0]] if endpoint else [])} if target is not None else set()
        for name, known in self.sandbox_targets():
            if known.host.lower() in names or name.lower() in names:
                return name
        return None

    def add_to_session(self, source):
        if source is not None and getattr(self, "workspace_request", None) is not None:
            self.workspace_request(*source)

    def places_menu(self, menu, pane):
        """The pane's chooser and its buttons (Other folder…, SSH…) as a submenu of MENU: where PANE lists."""
        menu.addSeparator()
        where = menu.addMenu(tr("Show in this pane"))
        where.setToolTipsVisible(True)
        for index in range(pane.roots.count()):
            data = pane.roots.itemData(index)
            if pane.roots.itemData(index, HEADING_ROLE):
                where.addSection(pane.roots.itemText(index).replace("&", "&&"))  # (a heading of the chooser)
                continue
            action = where.addAction(pane.roots.itemText(index).replace("&", "&&"))
            action.setEnabled(bool(data) and widgets.row_enabled(pane.roots, index))
            tip = pane.roots.itemData(index, Qt.ItemDataRole.ToolTipRole)
            if tip:
                action.setToolTip(tip)
            action.triggered.connect(lambda *_, data=data: self.show_in(pane, data))
        where.addSeparator()
        if pane.side == "sandbox":
            where.addAction(tr("Other folder…")).triggered.connect(lambda *_: pane.add_other())
        where.addAction(tr("SSH…")).triggered.connect(lambda *_: self.remote_dialog(pane))
        return where

    def show_in(self, pane, data):
        """PANE lists DATA of its chooser (a host by name: once it is connected)."""
        index = pane.roots.findData(data)
        if index < 0:
            self.message.setText(tr("{path} is no longer in the list of folders.", path=printable(data)))
            return False
        if index == pane.roots.currentIndex():
            pane.choose_root()  # (chosen again: from its start, as a click on the chooser's entry does not)
        else:
            pane.roots.setCurrentIndex(index)
        return True

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
        clip.folders = frozenset(entry.name for entry in entries if entry.kind == "dir")
        self.clipboard = clip
        text = "\n".join(printable(pane.path_of_entry(entry)) for entry in entries)
        # (the files other programs may get: those known at once now, the others once looked at - export_later)
        ready, later = self.export_work(pane, entries)
        keep_handed_out(ready)  # (the copies of what the clipboard named before may go now)
        QGuiApplication.clipboard().setMimeData(self.clip_mime(clip, text, ready))
        if later is not None:
            self.export_later(clip, text, ready, later)
        # (the keys as the system writes them: on macOS Cmd+V, not the Ctrl+V the text said)
        self.message.setText(tr("{count} item(s) cut: Paste ({shortcut}) moves them into the folder shown.",
                                count=len(entries), shortcut=key_text("Ctrl+V")) if cut else
                             tr("{count} item(s) copied: Paste ({shortcut}) copies them into the folder shown.",
                                count=len(entries), shortcut=key_text("Ctrl+V")))
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
        self.message.setText(tr("Nothing to paste: copy or cut files first ({copy}, {cut}).", copy=key_text("Ctrl+C"),
                                cut=key_text("Ctrl+X")))
        return False

    def paste_clip(self, clip, pane, target_dir):
        same = clip.folder_text == pane.path_at(target_dir) and same_root(clip.root, pane.root)
        if same and clip.cut:
            self.message.setText(tr("They are in this folder already."))
            return False
        if same:  # a copy beside the original, under a name of its own (a remote system's: through this machine)
            # (on_root: done at once - or, on a file system that hangs, when the names are read: its result came back
            # as True then, and unpacking it failed)
            root, names = pane.root, [copy_name(name, n, name in clip.folders) for name, _, _ in clip.items
                                      for n in range(1, 21)]
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
        # (each name given is taken from then on: two long names that differ only near their end are cut to one name
        # for their copies, and the second copy was refused - "already exists there")
        items, taken = [], set(taken)
        for name, path, _ in clip.items:
            folder = name in clip.folders
            new = next((copy_name(name, n, folder) for n in range(1, 21) if copy_name(name, n, folder) not in taken),
                       None)
            if new is None:
                self.message.setText(tr("{name}: too many copies here already.", name=printable(name)))
                return False
            taken.add(new)
            items.append((name, path, new))
        return self.paste_items(Clip(clip.root, items, clip.untrusted, clip.folder_text), pane, target_dir)

    def paste_items(self, clip, pane, target_dir):
        def began():
            # (once the move has begun - not when the paste returned: the question whether to replace what exists may
            # come after that, and answered No, nothing was moved and the cut was gone all the same)
            if self.clipboard is clip:
                self.clipboard = None  # moved: a second paste has nothing to move
            # (nor from the system clipboard: it named the files where they were, and a second paste copied from there)
            if clip.owns(QGuiApplication.clipboard().mimeData()):
                QGuiApplication.clipboard().clear()
        return self.transfer_items(clip, pane, target_dir, move=clip.cut, began=began if clip.cut else None)

    def export_urls(self, pane, entries):
        """The file URLs of ENTRIES that other programs may get (a drag out, the clipboard): never a link or a special
        file; from a sandbox's folder only plain files that are not executable (a folder could hold hooks or
        programs, which another program would copy as they are, executable bits and all), and those as copies made now
        (handed_out_folder: what the agent does to its files afterwards reaches no other program). What export_work
        leaves to a thread is waited for EXPORT_WAIT (the GUI thread waits: a drag needs them as it begins)."""
        ready, later = self.export_work(pane, entries)
        found = None if later is None else within(later, EXPORT_WAIT, "files-export", key=getattr(later, "key", None))
        return [file_url(path) for path in (ready if found is None else found)]

    def export_work(self, pane, entries):
        """([paths of ENTRIES other programs may get at once], LATER(give_up) or None): LATER, for a thread, gives the
        paths of all that may go (export_urls), those at once among them, in the order of ENTRIES - looking until
        GIVE_UP() is true. LATER.key: what it looks at (within's KEY: a look of the same still on its way is waited for,
        not started again)."""
        if pane.remote() or pane.root is None:
            return [], None  # (a remote file is no local one another program could take)
        if not pane.untrusted():
            # Never a folder that is a sandbox's or holds one, however deep (holds_agents: ~/projects/ws listed in
            # ~/projects went out as it is; so did ~/projects with a workspace moved into it, or one mounted deep
            # inside) - looked through in a thread: what is not known by then stays.
            paths = [pane.path_of_entry(entry) for entry in entries]
            ready = [path for path, entry in zip(paths, entries) if entry.kind == "file"]
            folders = [path for path, entry in zip(paths, entries) if entry.kind == "dir"]
            if not folders:
                return ready, None
            sources, kept = self.folder_sources(), dict(self.identities)

            def plain_folders(give_up):
                known, identities = resolve_folders(*sources)
                identities = {**kept, **identities}
                free = {path for path in folders if not holds_agents(path, known, identities, give_up)}
                return [path for path, entry in zip(paths, entries)
                        if entry.kind == "file" or entry.kind == "dir" and path in free]
            plain_folders.key = answer_key("folders", paths, [entry.kind for entry in entries], sources, kept)
            return ready, plain_folders
        # An agent's: as they are now, not as they were listed - it may have made a file executable since, or put a link
        # to a folder in its place (lstat relative to the folder opened once, never through a link). In a thread: a file
        # system that does not answer in time gives nothing out.
        # (the URL named the file in the agent's folder: the agent could swap it for a link to a key of the human's
        # between the drop and the other program's read - a mail program attached the key)
        root, relative, names = pane.root, pane.relative, [entry.name for entry in entries if entry.kind == "file"]
        if not names:
            return [], None

        def plain_files(give_up):  # (in a thread: files.copy_aside reads and writes the files)
            return files.copy_aside(root, relative, names, handed_out_folder, EXPORT_LIMIT, give_up)
        plain_files.key = ("aside", id(root), answer_key(root.path, relative, names))
        return [], plain_files

    def export_later(self, clip, text, ready, later):
        """The system clipboard of CLIP (Ctrl+C, Ctrl+X) gets the files LATER(give_up) finds (export_work: in a thread,
        given up after EXPORT_WAIT) instead of those READY - once it is done, if the clipboard still holds CLIP then.
        The GUI thread waited for them: every Ctrl+C of a big folder held up the window, also for a paste in the other
        pane."""
        deadline = time.monotonic() + EXPORT_WAIT

        def look():
            # (through within, as a drag: on a file system that hangs the look never ends - one waiting for the same is
            # not started again, and no more than EXPORT_STUCK wait; this thread ends a moment after the deadline)
            return within(lambda give_up: later(lambda: give_up() or time.monotonic() > deadline), EXPORT_WAIT + 1,
                          "files-export", key=getattr(later, "key", None))

        def done(found, error):
            board = QGuiApplication.clipboard()
            if error is not None or not found or self.clipboard is not clip or not clip.owns(board.mimeData()):
                return  # (copied, cut or pasted again meanwhile, or another program's on the clipboard by now)
            if list(found) != list(ready):
                keep_handed_out(found)  # (an agent's files' copies: kept while the clipboard names them)
                board.setMimeData(self.clip_mime(clip, text, found))
        background(look, owner=self, timeout=EXPORT_WAIT + 30, done=done)

    @staticmethod
    def clip_mime(clip, text, paths=()):
        """What the system clipboard gets for CLIP: its token, the paths as TEXT, the file URLs of PATHS."""
        mime = QMimeData()
        mime.setData(MIME, clip.token.encode("ascii"))
        mime.setText(text)
        if paths:
            mime.setUrls([file_url(path) for path in paths])
        return mime

    # -- drag and drop --

    def start_drag(self, pane):
        """Drag what is selected in PANE: to the other pane, onto a folder, a chooser or a path bar (the drop copies;
        with Shift it moves), onto an editor (opens the files), or to another program (export_urls - what it does not
        get is said in the status line while the drag goes on: drag_out_note). A drag that ends without a drop (Esc,
        or let go where nothing takes it) says so - or why the drop was refused there."""
        entries = pane.chosen()
        if not entries or pane.root is None:
            return None
        clip = Clip(pane.root, [(entry.name, pane.path_of(entry), None) for entry in entries], pane.untrusted(),
                    pane.path_text())
        clip.pane, clip.folder = pane, pane.relative
        clip.folders = frozenset(entry.name for entry in entries if entry.kind == "dir")
        clip.files = frozenset(entry.name for entry in entries if entry.kind == "file")
        mime = QMimeData()
        mime.setData(MIME, clip.token.encode("ascii"))
        urls = self.export_urls(pane, entries)
        if urls:
            mime.setUrls(urls)
        drag = QDrag(pane.table)
        drag.setMimeData(mime)
        drag.setPixmap(self.drag_picture(entries))
        note = self.drag_out_note(pane, entries, urls)
        self.drag, self.drag_note, self.drop_refused, self.drop_taken = clip, note, None, False
        if note:
            self.message.setText(note)
        result = None
        try:
            result = self.exec_drag(drag)
            return result
        finally:
            self.drag = None
            self.drop_hint(None)
            self.drag_note = None
            if not self.drop_taken and result in (None, Qt.DropAction.IgnoreAction):
                self.message.setText(self.drop_refused or note or tr(
                    "The drag ended without a drop (Esc, or let go where nothing takes it): nothing was copied or "
                    "moved."))

    def exec_drag(self, drag):
        return drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.CopyAction)

    @staticmethod
    def drag_out_note(pane, entries, urls):
        """What the status line says while ENTRIES of PANE are dragged, when other programs get less than all of them
        (URLS: what they get), else None. A remote system's files go to no other program (a drag would have to fetch
        them before it began, holding up the window: they are copied to this machine first)."""
        if pane.remote():
            return tr("Other programs get nothing of a remote system's files: copy them to this machine first (F5 into "
                      "a folder of this machine), then drag them from there.")
        if len(urls) < len(entries):
            return tr("Other programs get {count} of the {total} item(s) dragged: never a link, a special file or a "
                      "folder that holds a sandbox's folder; out of an agent's folder only plain files that are not "
                      "executable (as copies).", count=len(urls), total=len(entries))
        return None

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

    def drop_source(self, mime):
        """What MIME brings: (the items dragged in this window - a Clip -, the local files of another program, why none
        of it can be dropped). (None, [], None): nothing for the page (text, say - the path bar's own)."""
        if mime is None:
            return None, [], None
        if self.drag is not None and self.drag.owns(mime):
            return self.drag, [], None
        if mime.hasFormat(MIME):
            # (another window's - or forged: what is dragged is kept in the page, never read back from the data)
            return None, [], tr("these items were dragged in another window of the app (or that drag has ended); copy "
                                "and paste them instead.")
        paths = [path for path in map(local_path, mime.urls()) if path] if mime.hasUrls() else []
        if paths:
            return None, paths, None
        if mime.hasUrls():
            return None, [], tr("only files of this machine can be dropped here (not web addresses or files of other "
                                "systems).")
        return None, [], None

    def plan_into(self, pane, target_dir, row, move=False, clip=None, paths=(), place=None):
        """The DropPlan of CLIP's items (MOVE: moved) or the host PATHS into TARGET_DIR of PANE - refused, with the
        reason, where it cannot go: no folder shown yet, a job running, the folder still being listed, where they are
        already, a folder into itself."""
        action = Qt.DropAction.MoveAction if move and clip is not None else Qt.DropAction.CopyAction

        def refused(why):
            return DropPlan(pane, target_dir, row, action, clip, paths, refused=why, place=place)
        if pane.root is None and place != "chooser":  # (onto the chooser: a place is chosen after the drop)
            return refused(tr("no folder is shown in this pane yet."))
        if self.task.running:  # (said while it is dragged: dropped, it asked nothing and said "Wait until…")
            return refused(tr("a copy, move or delete is running; drop once it has finished (or cancel it)."))
        if place != "chooser":
            if pane.changing():  # (while the rows are another folder's: no row to drop onto)
                return refused(tr("the folder is still being listed; drop once it is shown."))
            if clip is not None and same_root(clip.root, pane.root):
                if target_dir == getattr(clip, "folder", None):
                    return refused(tr("they are already in this folder."))
                if any(target_dir == path or target_dir.startswith(path + "/") for _, path, _ in clip.items):
                    return refused(tr("a folder cannot go into itself."))
        return DropPlan(pane, target_dir, row, action, clip, paths, place=place)

    def drop_plan(self, pane, mime, point, modifiers):
        """What a drop of MIME at POINT of PANE's list does (DropPlan - refused where it cannot be made, with the
        reason), or None where it is nothing for the list: into the folder of the row at POINT ("." the folder shown,
        ".." the one above - at the top of the pane: the folder shown), else into the folder shown. Items dragged in
        this window are copied, with Shift moved; local files of another program are copied."""
        clip, paths, why = self.drop_source(mime)
        if clip is None and not paths and why is None:
            return None
        target_dir, row = pane.relative, -1
        index = pane.table.indexAt(point) if pane.root is not None and not pane.changing() else None
        if index is not None and index.isValid():
            entry = pane.entry_at(index.row())
            if pane.is_up(index.row()) and pane.can_go_up():
                target_dir, row = "/".join(files.parts(pane.relative)[:-1]), index.row()
            elif pane.is_here(index.row()):
                row = index.row()  # "." is the folder shown
            elif entry is not None and entry.kind == "dir":
                target_dir, row = pane.path_of(entry), index.row()
        if why:
            return DropPlan(pane, target_dir, row, Qt.DropAction.CopyAction, refused=why)
        return self.plan_into(pane, target_dir, row, bool(modifiers & Qt.KeyboardModifier.ShiftModifier), clip, paths)

    def drop_hint(self, plan):
        """While something is dragged over a list, a path bar or a chooser: what a drop there does, or why it cannot be
        made there (None: it left - what the drag said of other programs again)."""
        if plan is not None:
            self.message.setText(plan.text())
            return
        text = self.message.text()
        if text.startswith(tr("Drop: {rest}", rest="")) or text.startswith(tr("Cannot drop here: {why}", why="")):
            self.message.setText(self.drag_note or "")

    def refuse_drop(self, plan):
        """A drop that cannot be made was let go: not taken, and the status line keeps saying why."""
        self.drop_refused = plan.text()
        self.message.setText(self.drop_refused)

    def run_drop(self, plan):
        """Do what PLAN says (after the drop has ended)."""
        if plan.pane.root is None:
            return False
        if plan.clip is not None:
            return self.transfer_items(plan.clip, plan.pane, plan.target_dir, move=plan.move)
        return self.copy_paths(plan.paths, plan.pane, plan.target_dir, foreign=True)

    # -- dropped onto a path bar or a chooser --

    def bar_drag(self, pane, widget, event):
        """A drag event over PANE's path bar (WIDGET: a drop goes into the folder shown) or its chooser (where it goes
        is chosen after the drop: chooser_drop). True when it was handled here; text dragged onto the path bar is
        the field's own (typed in)."""
        kind = event.type()
        if kind == QEvent.Type.DragLeave:
            self.mark_drop(widget, False)
            self.drop_hint(None)
            return False
        chooser = widget is pane.roots
        clip, paths, why = self.drop_source(event.mimeData())
        if clip is None and not paths and why is None:
            return False
        place = "chooser" if chooser else "path"
        if why:
            plan = DropPlan(pane, pane.relative, None, Qt.DropAction.CopyAction, refused=why, place=place)
        else:
            move = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            plan = self.plan_into(pane, pane.relative, None, move, clip, paths, place)
        if kind == QEvent.Type.Drop:
            self.mark_drop(widget, False)
            self.drop_hint(None)
            if plan.refused:
                self.refuse_drop(plan)
                event.ignore()
                return True
            self.drop_taken = True
            event.setDropAction(plan.action)
            event.accept()
            # (after the drop has ended: a menu or a question inside a drop handler would hold up where it came from)
            QTimer.singleShot(0, self, lambda: self.chooser_drop(plan) if chooser else self.run_drop(plan))
            return True
        self.mark_drop(widget, not plan.refused)
        self.drop_hint(plan)
        if plan.refused:
            if kind == QEvent.Type.DragEnter:
                event.accept()  # (the moves over it follow: each is refused, the "no" cursor shows)
            else:
                event.ignore()
            return True
        event.setDropAction(plan.action)
        event.accept()
        return True

    @staticmethod
    def mark_drop(widget, on):
        """WIDGET (a path bar, a chooser) framed in the look's highlight colour while a drop there would be taken."""
        if bool(widget.property("dropTarget")) == on:
            return
        widget.setProperty("dropTarget", on)
        if on:
            widget.setProperty("styleBeforeDrop", widget.styleSheet())
            color = widget.palette().color(QPalette.ColorRole.Highlight).name()
            kind = "QComboBox" if isinstance(widget, QComboBox) else "QLineEdit"
            widget.setStyleSheet(f"{kind} {{ border: 2px solid {color}; }}")
        else:
            widget.setStyleSheet(widget.property("styleBeforeDrop") or "")

    def chooser_drop(self, plan):
        """After a drop onto a pane's chooser: a menu of where it goes - the chooser's places (as the pane opens them:
        a sandbox's folder at its top, a host or a remote system at its HOME, this machine at the folder shown there
        last) and the pane's recent folders. Chosen, the pane goes there (a host connected first), and once it shows
        that folder the drop goes into it as onto its list (drop_at_place); closed with Esc, nothing happens."""
        pane = plan.pane
        count = len(plan.clip.items) if plan.clip is not None else len(plan.paths)
        menu = QMenu(self)
        menu.setAccessibleName(tr("Where the drop goes"))
        title = tr("Move {count} item(s) into…", count=count) if plan.move else \
            tr("Copy {count} item(s) into…", count=count)
        menu.addAction(title).setEnabled(False)
        picked = []

        def add(where, text, wanted, tip=None):
            action = where.addAction(text.replace("&", "&&"))
            if tip:
                action.setToolTip(tip)
            action.triggered.connect(lambda *_: picked.append(wanted) or self.drop_at_place(plan, wanted))
        for index in range(pane.roots.count()):
            data, text = pane.roots.itemData(index), pane.roots.itemText(index)
            if pane.roots.itemData(index, HEADING_ROLE):
                menu.addSection(text)
            elif data and widgets.row_enabled(pane.roots, index):
                add(menu, text, ("place", data), pane.roots.itemData(index, Qt.ItemDataRole.ToolTipRole))
        recent = pane.recent_entries()
        if recent:
            menu.addSection(tr("Recent folders"))
            for text, key in recent:
                add(menu, text, ("recent", key))
        self.exec_menu(menu, QCursor.pos())
        if not picked:
            self.message.setText(tr("Nothing was dropped: no place was chosen."))
            return False
        return True

    def drop_at_place(self, plan, wanted):
        """The drop of PLAN goes where WANTED is - ("place", the chooser's data) or ("recent", a Recent key): its pane
        goes there, and the drop follows once that folder is listed (Pane.listed: run_pending) - not when another place
        is chosen meanwhile, nor after PENDING_DROP_WAIT."""
        pane = plan.pane
        kind, value = wanted
        root = value if kind == "place" else LOCAL if isinstance(value, str) else value[0]
        pane.pending_drop = {"plan": plan, "wanted": wanted, "root": root, "began": time.monotonic()}
        if kind == "place":
            return self.show_in(pane, value)
        return pane.go_recent(value)

    def run_pending(self, pane):
        """PANE shows the folder a drop onto its chooser waited for: the drop goes into it now (asked, refused, done
        as one onto its list)."""
        pending, pane.pending_drop = pane.pending_drop, None
        if pending is None or pane.root is None:
            return False
        if time.monotonic() - pending["began"] > PENDING_DROP_WAIT:
            self.message.setText(tr("Nothing was dropped: {path} was shown too late.",
                                    path=printable(pane.path_text())))
            return False
        plan = pending["plan"]
        again = self.plan_into(pane, pane.relative, -1, plan.move, plan.clip, plan.paths)
        if again.refused:
            self.message.setText(again.text())
            return False
        return self.run_drop(again)

    # -- dropped onto an editor --

    def editor_drop(self, mime):
        """What a drop of MIME onto an editor window does: None - nothing of the page's (text: the editor's own) -, else
        (why the files cannot be opened, or None; OPEN(window): each file in an editor of its own, after the drop). A
        folder, a link or a special file is not opened (said in the window)."""
        clip, paths, why = self.drop_source(mime)
        if clip is None and not paths and why is None:
            return None
        if why:
            return why, None
        if clip is not None:
            chosen = [(name, path) for name, path, _new in clip.items if name in getattr(clip, "files", ())]
            others = [name for name, _path, _new in clip.items if name not in getattr(clip, "files", ())]
            if not chosen:
                return tr("only files open in the editor - {names}: a folder, a link or a special file.",
                          names=", ".join(printable(name) for name in others[:5])), None
            root, folder = clip.root, clip.folder_text

            def open_items(window):
                return self.open_dropped(window, root, [(path, os.path.join(folder, name)) for name, path in chosen],
                                         others)
            return None, open_items

        def open_paths(window):
            found, problems = self.host_items(paths)
            items = [("/" + relative, relative) for _name, relative, _new in found.items]

            def kinds():  # (in a thread - answered: a file system that hangs held up the window)
                found = []
                for path, _relative in items:
                    try:
                        found.append(stat.S_ISREG(os.lstat(path).st_mode))
                    except OSError:
                        found.append(False)
                return found
            done, regular = answered(kinds, key=answer_key("dropped-kinds", [path for path, _ in items]))
            if not done:
                regular = [False] * len(items)
            chosen = [(relative, path) for (path, relative), plain in zip(items, regular) if plain]
            others = [os.path.basename(path) for (path, _relative), plain in zip(items, regular) if not plain]
            return self.open_dropped(window, self.local, chosen, others, problems)
        return None, open_paths

    def open_dropped(self, window, root, chosen, others=(), problems=()):
        """The files CHOSEN [(relative, shown)] of ROOT dropped onto the editor WINDOW, each in an editor of its own (at
        most EDITOR_DROP_LIMIT); what was not opened - OTHERS, not files, and PROBLEMS - said in WINDOW."""
        said = list(problems)
        if others:
            said.append(tr("Only files open in the editor - {names}: a folder, a link or a special file.",
                           names=", ".join(printable(name) for name in list(others)[:5])))
        if len(chosen) > EDITOR_DROP_LIMIT:
            said.append(tr("{count} more file(s) not opened: at most {limit} at once.",
                           count=len(chosen) - EDITOR_DROP_LIMIT, limit=EDITOR_DROP_LIMIT))
        opened = [self.open_editor(root, relative, shown) for relative, shown in chosen[:EDITOR_DROP_LIMIT]]
        if said and window is not None and isValid(window):
            window.say("\n".join(said), True)
        return opened

    # -- delete --

    def delete_selected(self, pane=None, entries=None):
        pane = pane or self.active
        entries = pane.chosen() if entries is None else entries
        if not entries or pane.root is None:
            pane.waiting() or self.message.setText(tr("Select files or folders first."))
            return False
        chosen = Chosen.of(pane, entries)
        if not ask_first(self, "files-delete", tr("Delete?"), delete_question(chosen), tr("Delete"),
                         details="\n".join(printable(path) for path in full_paths(chosen))):
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
        # (a sandbox's folder listed in a host folder of the human's - ~/projects/ws seen from ~/projects - is the
        # agent's itself: it got set-ID and sticky bits, only a folder shown inside it was refused them)
        # (their identities read in the job's thread: identities_reader)
        reader = None if getattr(chosen.root, "remote", False) else self.identities_reader()
        # (a remote system whose files count as an agent's by now - the box ticked after they were chosen: as one)
        special = not chosen.untrusted and not getattr(chosen.root, "agents", False)

        def work(cancelled):
            changed, problems = 0, []
            agents = frozenset(reader()) if reader is not None else frozenset()
            for (name, path), kind in zip(chosen.items, kinds):
                if cancelled():
                    problems.append(tr("Cancelled."))
                    break
                try:
                    chosen.root.chmod_entry(path, searchable if mixed and kind == "dir" else mode, special=special,
                                            **({"agents": agents} if agents else {}))
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
        # (freed once answered: each Owner… left its dialog behind, a child of the page as long as the window lived)
        if exec_dropped(dialog) != QDialog.DialogCode.Accepted:
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
        if pane.mode() != "sandbox" or pane.root is None:
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
