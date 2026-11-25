"""Checkpoints… of the Sessions page (its button and a start's right-click menu): the workspaces of a start as they
were, and what an agent did undone (`sandbox-ai checkpoint` on the start's host).

The checkpoints are taken on the host, where the sandbox cannot reach them: one when a start begins (before its agent
writes), others every few minutes (--checkpoint-every) or here (Take one now). The dialog lists them, newest first;
Changes shows what changed in the workspaces since one; Restore… first shows what a restore would change (a dry run:
nothing is written), then asks (checkpoint-restore) and puts them back - a checkpoint of the state before is taken
first, so a restore is undone by restoring that one; Delete is asked too (checkpoint-delete). Every command runs in the
background; the window never waits for one. The buttons' commands are on the list's right-click menu as well."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractButton, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from . import applog
from . import widgets
from .files import printable, printable_text
from .i18n import N_, tr
from .name_fields import alive
from .widgets import ask_first, background, explanation, output_text, plain_label, safe_tooltip, set_enabled

KINDS = {"start": N_("at the start"), "manual": N_("taken by hand"), "periodic": N_("periodic"),
         "restore": N_("before a restore")}
SHOWN = 400  # paths per kind of change shown in the dialog (the counts are complete)


def size_text(value):
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def when(text):
    return printable(str(text or "")).replace("T", " ").replace("Z", "")


def changes_text(found):
    """The changes of a diff or a restore's preview, as lines: a summary, then per folder M (changed), A (added) and
    D (deleted) paths as the sandbox sees them."""
    counts = found.get("counts") if isinstance(found.get("counts"), dict) else {}
    lines = [tr("{changed} changed, {added} added, {deleted} deleted", changed=counts.get("changed", 0),
                added=counts.get("added", 0), deleted=counts.get("deleted", 0))]
    for root in found.get("roots") if isinstance(found.get("roots"), list) else []:
        if not isinstance(root, dict):
            continue
        base = printable(str(root.get("sandbox") or root.get("host") or ""))
        lines += ["", f"{base} ({printable(str(root.get('host') or ''))})"]
        for key, mark in (("changed", "M"), ("added", "A"), ("deleted", "D")):
            items = root.get(key) if isinstance(root.get(key), list) else []
            for item in items[:SHOWN]:
                rel, what = (item[0], item[1]) if isinstance(item, list) and len(item) == 2 else (item, None)
                path = f"{base.rstrip('/')}/{printable(str(rel))}" if rel else base
                lines.append(f"  {mark} {path}" + (f"  ({printable(str(what))})" if what and what != "content" else ""))
            more = max(0, len(items) - SHOWN) + int(((root.get("more") or {}).get(key) or 0)
                                                    if isinstance(root.get("more"), dict) else 0)
            if more:
                lines.append("  " + tr("… and {count} more", count=more))
    return "\n".join(lines)


class CheckpointsDialog(QDialog):
    """The checkpoints of start NAME on HOST: listed, compared, restored after a preview, taken, deleted."""

    def __init__(self, parent, sai, name, host="local", running=False, owner=None):
        super().__init__(parent)
        self.sai, self.name, self.host, self.running_start = sai, name, host or "local", running
        self.owner, self.busy, self.rows = owner, False, {}
        self.note = None  # what the last restore or take said (how to undo it): kept above the list's count
        where = "" if self.host == "local" else " " + tr("on {host}", host=printable(self.host))
        self.setWindowTitle(tr("Checkpoints of {name}", name=printable(name)) + where)
        layout = QVBoxLayout(self)
        layout.addWidget(explanation(tr(
            "Taken on the host, where the sandbox cannot reach them: one when a start begins (before its agent "
            "writes), others every few minutes or by hand. A restore shows what changes first and takes a checkpoint "
            "of the state now before it writes, so it can be undone.")))
        self.table = widgets.table([tr("Taken"), tr("Why"), tr("Files"), tr("Size"), tr("Label / folders"),
                                    tr("Checkpoint")], stretch=4, numeric=(2, 3), compact=(0, 1, 5))
        self.table.setAccessibleName(tr("Checkpoints"))
        for corner in self.table.findChildren(QAbstractButton):  # (the table's corner: it selects every row)
            corner.setAccessibleName(tr("Select all"))
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.doubleClicked.connect(lambda _index: self.changes())
        layout.addWidget(self.table, 2)
        row = QHBoxLayout()
        self.changes_button = QPushButton(tr("Changes"))
        self.changes_button.setToolTip(safe_tooltip(tr("What changed in the workspaces since the selected checkpoint "
                                                       "({command})", command="sandbox-ai checkpoint diff")))
        self.changes_button.clicked.connect(lambda _=False: self.changes())
        self.restore_button = QPushButton(tr("Restore…"))
        self.restore_button.setAccessibleName(tr("Restore"))
        self.restore_button.setToolTip(safe_tooltip(tr(
            "Put the workspaces back as the selected checkpoint holds them: what changes is shown and asked first "
            "({command})", command="sandbox-ai checkpoint restore")))
        self.restore_button.clicked.connect(lambda _=False: self.restore())
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setToolTip(safe_tooltip(tr("Remove the selected checkpoint and what only it holds "
                                                      "({command})", command="sandbox-ai checkpoint delete")))
        self.delete_button.clicked.connect(lambda _=False: self.delete())
        self.take_button = QPushButton(tr("Take one now"))
        self.take_button.setToolTip(safe_tooltip(tr("A checkpoint of the workspaces now, with the label given "
                                                    "({command})", command="sandbox-ai checkpoint NAME")))
        self.take_button.clicked.connect(lambda _=False: self.take())
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(lambda _=False: self.load())
        for button in (self.changes_button, self.restore_button, self.delete_button):
            row.addWidget(button)
        row.addStretch(1)
        self.label = QLineEdit()
        self.label.setPlaceholderText(tr("why (optional)"))
        self.label.setAccessibleName(tr("Label"))
        self.label.setMaxLength(500)
        row.addWidget(self.label)
        row.addWidget(self.take_button)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.paths = QLineEdit()
        self.paths.setPlaceholderText(tr("Restore only these paths (optional, separated by commas): src/app.py, "
                                         "/work/docs"))
        self.paths.setAccessibleName(tr("Restore only these paths"))
        layout.addWidget(self.paths)
        # A right click on a checkpoint: the buttons' commands, as they are enabled.
        widgets.list_menu(self.table, lambda: [self.changes_button, self.restore_button, self.delete_button, None,
                                               self.take_button, self.refresh_button])
        self.status = plain_label("", role="secondary", selectable=True)
        layout.addWidget(self.status)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setAccessibleName(tr("Changes"))
        layout.addWidget(self.detail, 3)
        buttons = QDialogButtonBox()
        close = QPushButton(tr("Close"))
        close.clicked.connect(lambda _=False: self.reject())
        buttons.addButton(close, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self.resize(820, 560)
        self.update_buttons()
        self.load()

    # --- what is selected ---------------------------------------------------------------------------------------

    def selected(self):
        keys = widgets.selected_keys(self.table)
        return self.rows.get(keys[0]) if keys else None

    def chosen_paths(self):
        return [part.strip() for part in self.paths.text().split(",") if part.strip()]

    def update_buttons(self):
        if not alive(self):
            return
        chosen = self.selected()
        why = tr("A command runs.") if self.busy else tr("Choose a checkpoint.") if chosen is None else None
        for button in (self.changes_button, self.restore_button, self.delete_button):
            set_enabled(button, why is None, why)
        set_enabled(self.take_button, not self.busy, tr("A command runs."))
        set_enabled(self.refresh_button, not self.busy, tr("A command runs."))

    def started(self, text, keep_note=False):
        if not keep_note:
            self.note = None
        self.busy = True
        self.status.setText(text)
        self.update_buttons()

    def ended(self):
        self.busy = False
        self.update_buttons()

    # --- the commands -------------------------------------------------------------------------------------------

    def load(self):
        self.started(tr("Reading…"), keep_note=True)
        background(self.sai.checkpoints, self.name, self.host, owner=self.owner, what="sandbox-ai checkpoint list",
                   done=self.loaded)

    def loaded(self, result, error):
        if not alive(self):
            return
        self.ended()
        if error is not None:
            self.status.setText(tr("Not read: {error}", error=printable_text(error)))
            return
        self.rows = {row["id"]: row for row in result}
        widgets.fill(self.table, [[when(row.get("created")), tr(KINDS.get(row.get("kind"), str(row.get("kind") or ""))),
                                   str(row.get("files") or 0), size_text(row.get("bytes")),
                                   printable(str(row.get("label") or ", ".join(
                                       str(root.get("sandbox") or root.get("host") or "")
                                       for root in row.get("roots") or [] if isinstance(root, dict)))),
                                   row["id"]] for row in result],
                     keys=[row["id"] for row in result])
        counted = (tr("No checkpoints of {name} yet.", name=printable(self.name)) if not result else
                   tr("{count} checkpoints; the newest first.", count=len(result)))
        self.status.setText(f"{self.note}\n{counted}" if self.note else counted)
        self.update_buttons()

    def changes(self):
        chosen = self.selected()
        if chosen is None or self.busy:
            return
        self.started(tr("Comparing…"))
        background(self.sai.checkpoint_diff, chosen["id"], self.host, owner=self.owner,
                   what="sandbox-ai checkpoint diff", done=lambda result, error: self.compared(chosen, result, error))

    def compared(self, chosen, result, error):
        if not alive(self):
            return
        self.ended()
        if error is not None:
            self.status.setText(tr("Not compared: {error}", error=printable_text(error)))
            return
        self.status.setText(tr("Changed since {id} ({time}):", id=chosen["id"], time=when(chosen.get("created"))))
        self.detail.setPlainText(output_text(changes_text(result)))

    def restore(self):
        """Restore…: first the preview (a dry run), then asked, then put back (confirm_restore)."""
        chosen = self.selected()
        if chosen is None or self.busy:
            return
        paths = self.chosen_paths()
        self.started(tr("Reading what a restore changes…"))
        background(self.sai.checkpoint_preview, chosen["id"], self.host, paths, owner=self.owner,
                   what="sandbox-ai checkpoint restore --dry-run",
                   done=lambda result, error: self.previewed(chosen, paths, result, error))

    def previewed(self, chosen, paths, result, error):
        if not alive(self):
            return
        self.ended()
        if error is not None:
            self.status.setText(tr("Not restored: {error}", error=printable_text(error)))
            return
        self.detail.setPlainText(output_text(changes_text(result)))
        counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
        if not any(counts.get(key) for key in ("changed", "added", "deleted")):
            self.status.setText(tr("Nothing to restore: the folders are as they were."))
            return
        self.confirm_restore(chosen, paths, counts)

    def confirm_restore(self, chosen, paths, counts):
        # (what happens to each kind, said with its count: "3 changed, 2 added and 1 deleted files are replaced or
        # removed" read as if the deleted ones went too - they come back)
        text = tr("Put the workspaces of {name} back as they were at {time}? Files changed since then: {changed} (their "
                  "content of then comes back); added since: {added} (removed); deleted since: {deleted} (put back) - "
                  "listed below. A checkpoint of the state now is taken first, so this can be undone.",
                  name=printable(self.name), time=when(chosen.get("created")),
                  changed=counts.get("changed", 0), added=counts.get("added", 0), deleted=counts.get("deleted", 0))
        if self.running_start:
            text += "\n\n" + tr("{name} runs: its agent may write while the restore runs - pause it first.",
                                name=printable(self.name))
        if not ask_first(self, "checkpoint-restore", tr("Restore {id}", id=chosen["id"]), text, tr("Restore")):
            self.status.setText(tr("Nothing changed."))
            return
        applog.log("INFO", "checkpoints", f"restore {chosen['id']} of {printable(self.name)}"
                   + (f" on {printable(self.host)}" if self.host != "local" else ""))
        self.started(tr("Restoring…"))
        background(self.sai.checkpoint_restore, chosen["id"], self.host, paths, owner=self.owner, work=True,
                   what="sandbox-ai checkpoint restore", timeout=None,
                   done=lambda result, error: self.restored(chosen, result, error))

    def restored(self, chosen, result, error):
        if not alive(self):
            return
        self.ended()
        if error is not None:
            self.status.setText(tr("Not restored: {error}", error=printable_text(error)))
            return
        self.note = tr("{id} restored; undo it by restoring {before}.", id=chosen["id"],
                       before=printable(str(result.get("before") or "?")))
        self.status.setText(self.note)
        self.load()

    def delete(self):
        chosen = self.selected()
        if chosen is None or self.busy:
            return
        if not ask_first(self, "checkpoint-delete", tr("Delete {id}", id=chosen["id"]),
                         tr("Delete the checkpoint {id} of {name} ({time})? What only it holds is gone for good.",
                            id=chosen["id"], name=printable(self.name), time=when(chosen.get("created"))),
                         tr("Delete")):
            return
        self.started(tr("Deleting…"))
        background(self.sai.checkpoint_delete, chosen["id"], self.host, owner=self.owner, work=True,
                   what="sandbox-ai checkpoint delete", done=lambda result, error: self.deleted(result, error))

    def deleted(self, result, error):
        if not alive(self):
            return
        self.ended()
        if error is not None:
            self.status.setText(tr("Not deleted: {error}", error=printable_text(error)))
            return
        self.detail.clear()
        self.load()

    def take(self):
        if self.busy:
            return
        label = self.label.text().strip() or None
        self.started(tr("Taking a checkpoint…"))
        background(self.sai.checkpoint_create, self.name, self.host, label, owner=self.owner, work=True,
                   what="sandbox-ai checkpoint create", done=self.taken)

    def taken(self, result, error):
        if not alive(self):
            return
        self.ended()
        if error is not None:
            self.status.setText(tr("Not taken: {error}", error=printable_text(error)))
            return
        self.label.clear()
        self.note = tr("Checkpoint {id} taken: {files} files.", id=printable(str(result.get("id") or "?")),
                       files=result.get("files") or 0)
        self.status.setText(self.note)
        self.load()
