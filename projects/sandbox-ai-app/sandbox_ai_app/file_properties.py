"""The Files page's Properties dialog: name, path, type, size (a folder's - of several items the sum - counted at once
in a thread with its progress shown, bounded like a delete, links never followed; Stop, and closing the dialog, cancel
it), owner and group, the permission bits as checkboxes (changed where they may be: never on a link, no set-ID or
sticky bit below a sandbox's folder), the times and where a link points (shown, not followed).

Everything is read through the pane's files.Root (lstat relative to open directories) and changed through it
(Root.chmod_entry). The grid of checkboxes follows the idea of axross's ui/permissions_dialog.py
(github.com/c0decave/axross, Apache-2.0); the code here is this app's own.
"""
from __future__ import annotations

import stat
import threading

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from . import files
from .files import printable
from .i18n import N_, tr
from .widgets import background, button, number, plain_label, set_enabled

from shiboken6 import isValid

PROGRESS_INTERVAL = 250  # ms: how often a count's progress is shown
KINDS = {"dir": N_("folder"), "file": N_("file"), "link": N_("symbolic link"), "other": N_("special file")}
WHO = (N_("Owner"), N_("Group"), N_("Others"))
WHAT = (N_("Read"), N_("Write"), N_("Execute"))
BITS = (stat.S_IRUSR, stat.S_IWUSR, stat.S_IXUSR, stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP,
        stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH)
SPECIAL = ((stat.S_ISUID, N_("Set user ID")), (stat.S_ISGID, N_("Set group ID")), (stat.S_ISVTX, N_("Sticky")))


def when(seconds):
    return files.time_text(seconds, "%Y-%m-%d %H:%M:%S")


def value_label(text):
    """A field's value: plain text (names come from the agent), selectable, wrapped."""
    label = plain_label(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class PropertiesDialog(QDialog):
    """ITEMS: [(name, relative path below ROOT)] of one folder (FOLDER_TEXT: where they are, shown), or [("", RELATIVE)]
    for the folder itself. UNTRUSTED: an agent may have written there (no set-ID or sticky bits). CHANGED() after a
    change (the page lists its panes again). OWNER(ROOT, ITEMS, FOLDERS?): Owner… for the items shown here (never the
    pane's folder of the moment: it may show another one by then)."""

    def __init__(self, parent, root, items, folder_text, untrusted, changed=None, owner=None, infos=None):
        super().__init__(parent)
        self.root, self.items, self.untrusted, self.on_changed, self.owner_dialog = root, items, untrusted, changed, owner
        self.cancel_event = threading.Event()  # set when the dialog closes: every count ends
        self.stop_event = threading.Event()  # the count running now (Stop)
        self.counting, self.progress, self.applying = False, None, False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(480)
        # INFOS: what files.entry_infos found (read before: a remote root's in a thread; else here, for a local root,
        # relative to open folders - never a remote one on the GUI thread)
        from .files_page import on_root
        if infos is None and not getattr(root, "remote", False):
            infos = on_root(root, lambda: files.entry_infos(root, items),
                            lambda found, error: found if error is None else ([], [printable(error)]))
        if not isinstance(infos, tuple):  # (a remote root; or a local file system slower than a moment: on_root
            # gave True - still reading - and the dialog took it for what was read, a TypeError)
            infos = ([], [tr("Read in the background first (files_page.on_root).")])
        self.infos, problems = list(infos[0]), list(infos[1])
        single = len(self.infos) == 1
        first = self.infos[0] if self.infos else None
        title = printable(first[0] or folder_text) if single else tr("{count} items", count=len(self.infos))
        self.setWindowTitle(tr("Properties of {name}", name=title))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        kinds = [files.kind_of(info.st_mode) for _, _, info, _ in self.infos]
        if single:
            name, relative, info, target = first
            form.addRow(tr("Name"), value_label(printable(name or folder_text.rstrip("/").rsplit("/", 1)[-1] or "/")))
            form.addRow(tr("Folder") if name else tr("Path"), value_label(printable(folder_text)))
            form.addRow(tr("Type"), value_label(tr(KINDS[kinds[0]])))
            if target is not None:
                form.addRow(tr("Points to (not followed)"), value_label(printable(target)))
        else:
            form.addRow(tr("Folder"), value_label(printable(folder_text)))
            counted = {kind: kinds.count(kind) for kind in KINDS if kinds.count(kind)}
            form.addRow(tr("Type"), value_label(", ".join(f"{count} × {tr(KINDS[kind])}"
                                                          for kind, count in counted.items())))
        size_row = QHBoxLayout()
        plain = self.plain_size()
        self.folders = [relative for _, relative, info, _ in self.infos if stat.S_ISDIR(info.st_mode)]
        self.size = value_label(self.size_text(plain, None))
        size_row.addWidget(self.size, 1)
        self.count_button = button(tr("Compute size"), tr("Count what the folders hold (links are not followed; at "
                                                           "most {count} entries)", count=files.WALK_ENTRIES),
                                   lambda: self.count())
        self.count_button.hide()  # (after a count was stopped or cut short)
        size_row.addWidget(self.count_button)
        self.stop_button = button(tr("Stop"), tr("Stop counting (what was counted so far is shown)"),
                                  lambda: self.stop())
        self.stop_button.hide()
        size_row.addWidget(self.stop_button)
        self.timer = QTimer(self)
        self.timer.setInterval(PROGRESS_INTERVAL)
        self.timer.timeout.connect(self.show_progress)
        form.addRow(tr("Size"), size_row)
        if self.infos:
            info = first[2]
            owners = sorted({(i.st_uid, i.st_gid) for _, _, i, _ in self.infos})
            from .files_page import group_name, user_name
            remote = getattr(root, "remote", False)
            if remote:  # (a remote system's IDs: this machine's names for them would name other users)
                form.addRow(tr("Owner"), value_label(", ".join(sorted({str(uid) for uid, _ in owners}))))
                form.addRow(tr("Group"), value_label(", ".join(sorted({str(gid) for _, gid in owners}))))
            else:
                form.addRow(tr("Owner"), value_label(", ".join(sorted({f"{printable(user_name(uid))} ({uid})"
                                                                         for uid, _ in owners}))))
                form.addRow(tr("Group"), value_label(", ".join(sorted({f"{printable(group_name(gid))} ({gid})"
                                                                         for _, gid in owners}))))
            if single:
                form.addRow(tr("Modified"), value_label(when(info.st_mtime)))
                form.addRow(tr("Accessed"), value_label(when(info.st_atime)))
                if not remote:  # (SFTP tells no status change time)
                    form.addRow(tr("Status changed"), value_label(when(info.st_ctime)))
        # The permission bits: of the first item; Apply sets them on all.
        self.mode = stat.S_IMODE(first[2].st_mode) if first else 0
        group = QGroupBox(tr("Permissions"))
        grid = QGridLayout(group)
        for column, what in enumerate(WHAT):
            grid.addWidget(QLabel(tr(what)), 0, column + 1, alignment=Qt.AlignmentFlag.AlignCenter)
        self.checks = []
        for row, who in enumerate(WHO):
            grid.addWidget(QLabel(tr(who)), row + 1, 0)
            for column, what in enumerate(WHAT):
                check = QCheckBox()
                check.setAccessibleName(tr("{who}: {what}", who=tr(who), what=tr(what)))
                check.setChecked(bool(self.mode & BITS[row * 3 + column]))
                check.toggled.connect(self.update_mode)
                grid.addWidget(check, row + 1, column + 1, alignment=Qt.AlignmentFlag.AlignCenter)
                self.checks.append(check)
        specials = QWidget()
        line = QHBoxLayout(specials)
        line.setContentsMargins(0, 0, 0, 0)
        self.specials = []
        for bit, text in SPECIAL:
            check = QCheckBox(tr(text))
            check.setChecked(bool(self.mode & bit))
            check.toggled.connect(self.update_mode)
            line.addWidget(check)
            self.specials.append((bit, check))
        grid.addWidget(specials, 4, 0, 1, 4)
        self.octal = value_label("")
        grid.addWidget(self.octal, 5, 0, 1, 4)
        layout.addWidget(group)
        # Changeable: not a link (it has no bits of its own; what it points to is not changed), not the root of a
        # sandbox's folder (only what is below it); set-ID and sticky bits never below a sandbox's folder.
        links = any(kind == "link" for kind in kinds)
        root_itself = any(not files.parts(relative) for _, relative, _, _ in self.infos)
        self.changeable = bool(self.infos) and not links and not root_itself
        for check in self.checks:
            check.setEnabled(self.changeable)
        for _, check in self.specials:  # (one set already may go in a sandbox's folder too, or stay)
            check.setEnabled(self.changeable and (not untrusted or check.isChecked()))
        why = []
        if links:
            why.append(tr("A symbolic link has no permissions of its own; what it points to is not changed here."))
        if root_itself:
            why.append(tr("The folder shown at the top of a pane is changed from the folder above it."))
        if untrusted:
            why.append(tr("Set-user-ID, set-group-ID and sticky bits are never set in a sandbox's folder."))
        self.note = plain_label("\n".join(why + problems), role="secondary")
        layout.addWidget(self.note)
        self.note.setVisible(bool(why or problems))  # (in the dialog first: no window of its own for a moment)
        self.message = plain_label(selectable=True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        buttons.addStretch()
        if owner is not None and self.infos and not root_itself:
            buttons.addWidget(button(tr("Owner…"), tr("Give the items to you or one of your groups"),
                                     lambda: owner(self.root, [(name, relative) for name, relative, _, _ in self.infos],
                                                   bool(self.folders))))
        self.apply_button = button(tr("Apply"), tr("Set these permission bits"), lambda: self.apply())
        buttons.addWidget(self.apply_button)
        buttons.addWidget(button(tr("Close"), tr("Close the window"), self.close))
        layout.addLayout(buttons)
        self.update_mode()
        if self.folders:  # a folder's size: counted at once, in a thread (Stop, or closing the dialog, ends it)
            self.count()

    def first_names(self):
        """{(st_dev, st_ino)} of the files chosen that have other names (hard links): a count of the folders chosen
        counts their bytes no second time."""
        return {(info.st_dev, info.st_ino) for _, _, info, _ in self.infos
                if stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) > 1}

    def plain_size(self):
        """The bytes of the files chosen, each file once (two names of one file chosen: once)."""
        seen, total = set(), 0
        for _, _, info, _ in self.infos:
            if not stat.S_ISREG(info.st_mode):
                continue
            if getattr(info, "st_nlink", 1) > 1:
                if (info.st_dev, info.st_ino) in seen:
                    continue
                seen.add((info.st_dev, info.st_ino))
            total += info.st_size
        return total

    def size_text(self, plain, counted, partial=False):
        if counted is None:
            text = tr("{size} ({bytes} bytes)", size=files.size_text(plain), bytes=number(plain))
            if self.folders:
                text += "; " + tr("folders: not counted yet")
            return text
        bytes_ = plain + counted.bytes
        others = sum(1 for _, _, info, _ in self.infos if not stat.S_ISDIR(info.st_mode))
        text = tr("{size} ({bytes} bytes) in {files} file(s) and {folders} folder(s)", size=files.size_text(bytes_),
                  bytes=number(bytes_), files=counted.files + others, folders=counted.directories)
        return tr("at least {text}", text=text) if partial else text

    def chosen_mode(self):
        mode = sum(bit for bit, check in zip(BITS, self.checks) if check.isChecked())
        return mode | sum(bit for bit, check in self.specials if check.isChecked())

    def update_mode(self, *_):
        mode = self.chosen_mode()
        self.octal.setText(tr("Mode {octal} ({text})", octal=f"{mode:04o}", text=stat.filemode(stat.S_IFREG | mode)[1:]))
        set_enabled(self.apply_button, self.changeable and mode != self.mode and not self.applying)

    def apply(self):
        """Set the bits changed in the boxes on every item (Root.chmod_entry: never a link, no set-ID or sticky bit below
        a sandbox's folder). The boxes show the first item's bits: each item keeps its own others (every item was given
        the first one's whole mode - a tick for the group's write on a file and a folder made the folder 0664, which
        nobody could enter any more)."""
        if not self.changeable or self.applying:  # (one at a time: on a slow file system it goes on in a thread)
            return False
        self.applying = True
        set_enabled(self.apply_button, False)
        mode, root, items, special = self.chosen_mode(), self.root, list(self.infos), not self.untrusted
        changed = mode ^ self.mode  # the bits whose boxes were changed
        modes = dict(getattr(self, "modes", None) or {relative: stat.S_IMODE(info.st_mode)
                                                       for _, relative, info, _ in self.infos})

        def work():
            problems, done, set_ = [], 0, {}
            for name, relative, _, _ in items:
                wanted = (modes[relative] & ~changed) | (mode & changed)
                try:
                    root.chmod_entry(relative, wanted, special=special)
                    set_[relative] = wanted
                    done += 1
                except (OSError, ValueError) as exc:
                    problems.append(f"{printable(name)}: {printable(exc)}")
            return done, problems, set_

        def applied(result, error):
            done, problems, set_ = result if error is None else (0, [printable(error)], {})
            if not isValid(self):
                return False  # (closed meanwhile: a remote root's bits are set in a thread)
            self.applying = False
            self.modes = {**modes, **set_}
            if done:
                self.mode = mode
            self.message.setText("\n".join([tr("Permissions of {count} item(s) changed.", count=done)] + problems))
            self.update_mode()
            if self.on_changed is not None:
                self.on_changed()
            return not problems
        # (a remote root's in a thread: on_root)
        from .files_page import on_root
        return on_root(root, work, applied, owner=self)

    def count(self):
        """Count the folders' contents in a thread (Root.measure: bounded, links not followed), its progress shown
        while it counts; Stop or closing the dialog cancels it (what was counted is shown as "at least")."""
        if self.counting or not self.folders or self.cancel_event.is_set():
            return False
        self.counting, self.progress = True, files.Progress()
        self.stop_event = threading.Event()
        self.count_button.hide()
        set_enabled(self.stop_button, True)
        self.stop_button.show()
        self.size.setText(tr("Counting…"))
        self.timer.start()
        root, folders, progress = self.root, list(self.folders), self.progress
        stop, closed = self.stop_event, self.cancel_event

        def cancelled():
            return stop.is_set() or closed.is_set()
        plain, seen = self.plain_size(), self.first_names()

        def work():
            total, partial = files.Result(), False
            for relative in folders:
                try:
                    done = root.measure(relative, cancelled=cancelled, progress=progress, seen=seen)
                except (OSError, ValueError) as exc:
                    done, partial = getattr(exc, "partial", files.Result()), str(exc)
                total.bytes += done.bytes
                total.files += done.files
                total.directories += done.directories
                total.skipped += done.skipped
                if partial:
                    break
            return total, partial
        background(work, owner=self, timeout=600, done=lambda result, error: self.counted(plain, result, error))
        return True

    def show_progress(self):
        progress = self.progress
        if not self.counting or progress is None:
            return
        self.size.setText(tr("Counting… {count} file(s) and folder(s), {size} so far", count=progress.count,
                             size=files.size_text(progress.bytes)))

    def stop(self):
        """Stop the count running now (what it counted so far is shown)."""
        if not self.counting:
            return False
        self.stop_event.set()
        set_enabled(self.stop_button, False)
        return True

    def counted(self, plain, result, error):
        self.counting = False
        self.timer.stop()
        self.stop_button.hide()
        if error is not None:
            self.size.setText(printable(error))
            self.count_button.show()
            return
        total, partial = result
        self.size.setText(self.size_text(plain, total, bool(partial)))
        self.count_button.setVisible(bool(partial))  # stopped or cut short: it may be counted again
        lines = [printable(partial)] if partial else []
        if total.skipped:  # (a file system mounted in a folder: not counted, as du -x)
            lines.append(tr("Not counted: {list}", list=", ".join(f"{printable(path)} ({printable(why)})"
                                                                 for path, why in total.skipped[:10])))
        if lines:
            self.message.setText("\n".join(lines))

    def done(self, code):
        self.cancel_event.set()
        super().done(code)

    def closeEvent(self, event):  # noqa: N802
        self.cancel_event.set()
        super().closeEvent(event)
