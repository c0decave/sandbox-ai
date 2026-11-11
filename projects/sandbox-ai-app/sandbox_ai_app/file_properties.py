"""The Files page's Properties dialog: name, path, type, size (a folder's on request, counted in a thread, bounded and
cancellable, links never followed), owner and group, the permission bits as checkboxes (changed where they may be: never
on a link, no set-ID or sticky bit below a sandbox's folder), the times and where a link points (shown, not followed).

Everything is read through the pane's files.Root (lstat relative to open directories) and changed through it
(Root.chmod_entry). The grid of checkboxes follows the idea of axross's ui/permissions_dialog.py
(github.com/c0decave/axross, Apache-2.0); the code here is this app's own.
"""
from __future__ import annotations

import stat
import threading
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from . import files
from .files import printable
from .i18n import N_, tr
from .widgets import background, button, plain_label, set_enabled

KINDS = {"dir": N_("folder"), "file": N_("file"), "link": N_("symbolic link"), "other": N_("special file")}
WHO = (N_("Owner"), N_("Group"), N_("Others"))
WHAT = (N_("Read"), N_("Write"), N_("Execute"))
BITS = (stat.S_IRUSR, stat.S_IWUSR, stat.S_IXUSR, stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP,
        stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH)
SPECIAL = ((stat.S_ISUID, N_("Set user ID")), (stat.S_ISGID, N_("Set group ID")), (stat.S_ISVTX, N_("Sticky")))


def when(seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


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

    def __init__(self, parent, root, items, folder_text, untrusted, changed=None, owner=None):
        super().__init__(parent)
        self.root, self.items, self.untrusted, self.on_changed, self.owner_dialog = root, items, untrusted, changed, owner
        self.cancel_event = threading.Event()
        self.counting = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(480)
        self.infos, problems = [], []
        for name, relative in items:
            try:
                info = root.stat_entry(relative)
                target = root.readlink(relative) if stat.S_ISLNK(info.st_mode) else None
                self.infos.append((name, relative, info, target))
            except (OSError, ValueError) as exc:
                problems.append(f"{printable(name or relative or '/')}: {printable(exc)}")
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
        plain = sum(info.st_size for _, _, info, _ in self.infos if stat.S_ISREG(info.st_mode))
        self.folders = [relative for _, relative, info, _ in self.infos if stat.S_ISDIR(info.st_mode)]
        self.size = value_label(self.size_text(plain, None))
        size_row.addWidget(self.size, 1)
        self.count_button = button(tr("Compute size"), tr("Count what the folders hold (links are not followed; at "
                                                           "most {count} entries)", count=files.WALK_ENTRIES),
                                   lambda: self.count())
        self.count_button.setVisible(bool(self.folders))
        size_row.addWidget(self.count_button)
        form.addRow(tr("Size"), size_row)
        if self.infos:
            info = first[2]
            owners = sorted({(i.st_uid, i.st_gid) for _, _, i, _ in self.infos})
            from .files_page import group_name, user_name
            form.addRow(tr("Owner"), value_label(", ".join(f"{printable(user_name(uid))} ({uid})" for uid, _ in owners)))
            form.addRow(tr("Group"), value_label(", ".join(sorted({f"{printable(group_name(gid))} ({gid})"
                                                                     for _, gid in owners}))))
            if single:
                form.addRow(tr("Modified"), value_label(when(info.st_mtime)))
                form.addRow(tr("Accessed"), value_label(when(info.st_atime)))
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
        for _, check in self.specials:
            check.setEnabled(self.changeable and not untrusted)
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
        self.message = plain_label()
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

    def size_text(self, plain, counted, partial=False):
        if counted is None:
            text = tr("{size} ({bytes} bytes)", size=files.size_text(plain), bytes=f"{plain:,}")
            if self.folders:
                text += "; " + tr("folders: not counted yet")
            return text
        bytes_ = plain + counted.bytes
        others = sum(1 for _, _, info, _ in self.infos if not stat.S_ISDIR(info.st_mode))
        text = tr("{size} ({bytes} bytes) in {files} file(s) and {folders} folder(s)", size=files.size_text(bytes_),
                  bytes=f"{bytes_:,}", files=counted.files + others, folders=counted.directories)
        return tr("at least {text}", text=text) if partial else text

    def chosen_mode(self):
        mode = sum(bit for bit, check in zip(BITS, self.checks) if check.isChecked())
        return mode | sum(bit for bit, check in self.specials if check.isChecked())

    def update_mode(self, *_):
        mode = self.chosen_mode()
        self.octal.setText(tr("Mode {octal} ({text})", octal=f"{mode:04o}", text=stat.filemode(stat.S_IFREG | mode)[1:]))
        set_enabled(self.apply_button, self.changeable and mode != self.mode)

    def apply(self):
        """Set the chosen bits on every item (Root.chmod_entry: never a link, no set-ID or sticky bit below a sandbox's
        folder)."""
        if not self.changeable:
            return False
        mode, problems, done = self.chosen_mode(), [], 0
        for name, relative, _, _ in self.infos:
            try:
                self.root.chmod_entry(relative, mode, special=not self.untrusted)
                done += 1
            except (OSError, ValueError) as exc:
                problems.append(f"{printable(name)}: {printable(exc)}")
        if done:
            self.mode = mode
        self.message.setText("\n".join([tr("Permissions of {count} item(s) changed.", count=done)] + problems))
        self.update_mode()
        if self.on_changed is not None:
            self.on_changed()
        return not problems

    def count(self):
        """Count the folders' contents in a thread (Root.measure: bounded, cancellable, links not followed)."""
        if self.counting or not self.folders:
            return False
        self.counting = True
        set_enabled(self.count_button, False)
        self.size.setText(tr("Counting…"))
        root, folders, cancelled = self.root, list(self.folders), self.cancel_event.is_set
        plain = sum(info.st_size for _, _, info, _ in self.infos if stat.S_ISREG(info.st_mode))

        def work():
            total, partial = files.Result(), False
            for relative in folders:
                try:
                    done = root.measure(relative, cancelled=cancelled)
                except (OSError, ValueError) as exc:
                    done, partial = getattr(exc, "partial", files.Result()), str(exc)
                total.bytes += done.bytes
                total.files += done.files
                total.directories += done.directories
                if partial:
                    break
            return total, partial
        background(work, owner=self, timeout=600, done=lambda result, error: self.counted(plain, result, error))
        return True

    def counted(self, plain, result, error):
        self.counting = False
        if error is not None:
            self.size.setText(printable(error))
            return
        total, partial = result
        self.size.setText(self.size_text(plain, total, bool(partial)))
        if partial:
            self.message.setText(printable(partial))

    def done(self, code):
        self.cancel_event.set()
        super().done(code)

    def closeEvent(self, event):  # noqa: N802
        self.cancel_event.set()
        super().closeEvent(event)
