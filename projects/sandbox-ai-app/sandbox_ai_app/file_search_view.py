"""The Files page's search window (user 2026-11-19: "brauchen im Dateimanager auch noch eine generelle Suche mit
Wildcard-Support"): what lies BELOW the folder a pane shows - this machine's, a sandbox's, a remote system's -, found by
file_search.py in a thread of its own (the page's copies and deletes go on meanwhile), its progress shown, Stop (and
closing the window) cancelling it.

Names: a pattern (* ? [..], ** for any depth; letter case ignored unless Match case; no wildcard: a part of the name);
Content contains: optional, text files only (up to file_search.CONTENT_LIMIT each). The results: the path below the
folder searched, size, date, kind (a content match's line in its tooltip); how it ended - every limit reached, the
folders and files not searched (counted, named; Not searched… lists them all with why).

A double click (or Enter) shows the entry selected in its pane (its folder listed there; the pane's quick filter
emptied when it would hide it). Below the list and on its right click: Open (a file in the app's editor as the pane
opens it, a folder in the pane, a link as the pane takes it - followed only where the pane follows one), Show in folder,
Copy path (as the pane shows paths), Copy to the other pane (the page's copy: its questions, the agent's files never
executable where they go out of a sandbox's folder, as from the pane) and Delete… (asked first with every full path,
the page's delete). The actions go through the pane that was searched, and only while it still shows that place: one
chosen there since is said, nothing is done. Everything shown of a name is escaped (files.printable): an agent chose it.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QProgressBar, QVBoxLayout,
)

from shiboken6 import isValid

from . import file_search, files
from .files import printable, printable_text
from .i18n import N_, tr
from .widgets import (
    ask_first, background, button, delete_key, fill, list_menu, plain_label, row_key, safe_tooltip, selected_rows,
    set_enabled, show_text_window, table,
)

POLL = 250  # ms: how often the results and the progress are shown while it runs
KINDS = {"dir": N_("folder"), "file": N_("file"), "link": N_("symbolic link"), "other": N_("special file")}
COLUMNS = (N_("Path"), N_("Size"), N_("Modified"), N_("Type"))


class SearchDialog(QDialog):
    """The search below the folder PANE (a files_page.Pane of PAGE) shows when it is opened (aim)."""

    def __init__(self, page, pane, parent=None):
        super().__init__(parent if parent is not None else page)
        self.page, self.pane = page, pane
        self.root, self.start, self.base, self.where_text = None, "", "", ""
        self.search, self.shown_count, self.gone, self.hits = None, 0, set(), {}
        self.cancel = threading.Event()
        self.busy = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        self.where = plain_label(selectable=True)
        self.where.setAccessibleName(tr("Searched below"))
        layout.addWidget(self.where)
        form = QFormLayout()
        self.names = QLineEdit()
        self.names.setAccessibleName(tr("Names"))
        self.names.setPlaceholderText(tr("*.txt, src/**/*.py, report - empty: every name"))
        self.names.setToolTip(tr("* any characters, ? one, [abc] one of them, [!abc] none of them, ** any number of "
                                 "folders (with a / the path below the folder is matched, else the name); without a "
                                 "wildcard a part of the name; a / at the end: folders only"))
        self.names.returnPressed.connect(self.begin)
        form.addRow(tr("Names:"), self.names)
        self.text = QLineEdit()
        self.text.setAccessibleName(tr("Content contains"))
        self.text.setPlaceholderText(tr("Optional: only text files that hold this"))
        self.text.setToolTip(safe_tooltip(tr("Looked for in text files of up to {size} (files with NUL bytes are "
                                             "binary and left out; links are never read)",
                                             size=files.size_text(file_search.CONTENT_LIMIT))))
        self.text.returnPressed.connect(self.begin)
        form.addRow(tr("Content contains:"), self.text)
        self.case = QCheckBox(tr("Match case"))
        self.case.setToolTip(tr("Letter case counts in the names and the content"))
        form.addRow("", self.case)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.go_button = button(tr("Search"), tr("Search below the folder (Enter)"), self.begin)
        self.stop_button = button(tr("Stop"), tr("Stop the search: what was found until then stays listed"),
                                  self.stop)
        self.stop_button.setEnabled(False)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setMaximumWidth(120)
        self.bar.setMaximumHeight(12)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName(tr("Searching"))
        self.bar.hide()
        self.state = plain_label(role="secondary")
        row.addWidget(self.go_button)
        row.addWidget(self.stop_button)
        row.addWidget(self.bar)
        row.addWidget(self.state, 1)
        layout.addLayout(row)
        self.table = table([tr(column) for column in COLUMNS], multi=True, stretch=0, many=True, compact=(1, 2, 3))
        self.table.setAccessibleName(tr("Search results"))
        self.table.setMinimumHeight(220)
        self.table.cellDoubleClicked.connect(lambda row, _column=0: self.show_in_folder(row))
        self.table.installEventFilter(self)  # Enter: Show in folder
        layout.addWidget(self.table, 1)
        self.actions = {
            "open": button(tr("Open"), tr("A file in the editor, a folder in the pane (as the pane opens it)"),
                           lambda: self.open_hit()),
            "show": button(tr("Show in folder"), tr("Its folder in the pane, the entry selected (double click, Enter)"),
                           lambda: self.show_in_folder()),
            "path": button(tr("Copy path"), tr("The full paths of the selected entries, one a line"),
                           lambda: self.copy_paths()),
            "copy": button(tr("Copy to the other pane"), tr("Copy the selected entries into the other pane's folder"),
                           lambda: self.copy_to_other()),
            "delete": button(tr("Delete…"), tr("Delete the selected entries (asks first, with their full paths)"),
                             lambda: self.delete_hits(), role="danger"),
        }
        actions = QHBoxLayout()
        for widget in self.actions.values():
            actions.addWidget(widget)
        actions.addStretch(1)
        layout.addLayout(actions)
        list_menu(self.table, lambda: [self.actions["open"], self.actions["show"], None, self.actions["path"],
                                       self.actions["copy"], None, self.actions["delete"]])
        delete_key(self.table, self.delete_hits, tr("Delete…"))
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.enable())
        self.summary = plain_label(selectable=True)
        self.summary.setAccessibleName(tr("How the search ended"))
        layout.addWidget(self.summary)
        self.not_searched_button = button(tr("Not searched…"), tr("Every folder and file that was not searched, and "
                                                                  "why"), lambda: self.show_not_searched())
        self.not_searched_button.hide()
        closing = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        closing.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.not_searched_button)
        bottom.addStretch(1)
        bottom.addWidget(closing)
        layout.addLayout(bottom)
        for widget in (self.go_button, self.stop_button, self.not_searched_button, *self.actions.values(),
                       *closing.buttons()):
            widget.setAutoDefault(False)  # (Enter in the list shows the entry; in a field it searches)
            widget.setDefault(False)
        self.timer = QTimer(self)
        self.timer.setInterval(POLL)
        self.timer.timeout.connect(self.poll)
        self.aim(pane)
        self.enable()

    # -- where --

    def aim(self, pane):
        """Search below the folder PANE shows now (not while a search runs: it keeps its folder)."""
        if self.running:
            return
        self.pane = pane
        self.root, self.start = pane.root, pane.relative
        self.base = pane.path_at(pane.relative) if pane.root is not None else ""
        place = pane.roots.currentText() if hasattr(pane, "roots") else ""
        if getattr(self.root, "remote", False):
            self.where_text = self.root.display(self.start)
        else:
            self.where_text = self.base
        self.where.setText(tr("Below {path} ({place})", path=printable(self.where_text), place=printable(place))
                           if place else tr("Below {path}", path=printable(self.where_text)))
        self.setWindowTitle(tr("Search below {name}", name=printable(self.where_text.rstrip("/").rsplit("/", 1)[-1]
                                                                     or self.where_text)))

    @property
    def running(self):
        """From Search until its end is shown (ended(), on the GUI thread)."""
        return self.busy

    def full_path(self, hit):
        """HIT's path as the pane shows paths (its path bar: this machine's, a sandbox's, a remote system's)."""
        return f"{self.base.rstrip('/')}/{hit.relative}" if self.base else hit.relative

    def root_relative(self, hit):
        return f"{self.start}/{hit.relative}" if self.start else hit.relative

    # -- the search --

    def begin(self):
        """Search with what the fields say (False when it cannot: a search runs, no folder, no pattern)."""
        if self.running:
            self.state.setText(tr("A search runs: Stop it first."))
            return False
        if self.root is None:
            self.state.setText(tr("Choose a folder in the pane first."))
            return False
        cancel = threading.Event()
        try:
            search = file_search.Search(self.names.text(), self.text.text(), self.case.isChecked(), cancel.is_set,
                                        max_results=file_search.MAX_RESULTS)
        except ValueError as exc:
            self.state.setText(printable_text(exc))
            return False
        root, start = self.root, self.start
        self.search, self.shown_count, self.gone, self.busy = search, -1, set(), True
        fill(self.table, [])
        self.summary.setText("")
        self.not_searched_button.hide()
        self.state.setText(tr("Searching…"))
        self.go_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.bar.show()
        self.cancel = cancel
        background(lambda: file_search.run(root, start, search), done=lambda _result, error: self.ended(search, error),
                   owner=self, what=f"search below {printable(self.where_text)}")
        self.timer.start()
        return True

    def stop(self):
        if self.running:
            self.cancel.set()
            self.stop_button.setEnabled(False)
            self.state.setText(tr("Stopping…"))

    def poll(self):
        search = self.search
        if search is None:
            return
        if len(search.hits) != self.shown_count:
            self.show_hits()
        if not search.done:
            self.state.setText(tr("Searching… {progress}", progress=search.progress()))

    def show_hits(self):
        """The results so far in the list (those deleted since left out); one paint."""
        search = self.search
        # (in path order: this machine's walk goes folder by folder, a remote one's level by level)
        hits = sorted((hit for hit in search.hits[:] if hit.relative not in self.gone),
                      key=lambda hit: (hit.relative.casefold(), hit.relative))
        self.shown_count = len(search.hits)
        rows, tips, sorts, keys = [], [], [], []
        for hit in hits:
            shown = printable(hit.relative) + ("/" if hit.kind == "dir" else "")
            rows.append([shown, "" if hit.kind == "dir" else files.size_text(hit.size), files.time_text(hit.mtime),
                         tr(KINDS.get(hit.kind, KINDS["other"]))])
            tips.append(tr("{path}\nline {line}: {text}", path=printable(self.full_path(hit)), line=hit.line,
                           text=hit.text) if hit.line else printable(self.full_path(hit)))
            sorts.append([None, hit.size, hit.mtime, None])
            keys.append(hit.relative)
        self.hits = {hit.relative: hit for hit in hits}
        self.table.setUpdatesEnabled(False)
        try:
            fill(self.table, rows, tooltips=tips, sort=sorts, keys=keys)
        finally:
            self.table.setUpdatesEnabled(True)
        self.enable()

    def ended(self, search, error):
        if not isValid(self) or search is not self.search:
            return
        self.timer.stop()
        self.busy = False
        self.show_hits()
        self.bar.hide()
        self.go_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if error is not None:
            self.state.setText(tr("Not searched: {error}", error=printable_text(error)))
            return
        took = search.ended_after or 0
        self.state.setText(tr("{count} found in {seconds} s ({progress}).", count=len(search.hits),
                              seconds=f"{took:.1f}", progress=search.progress()))
        self.summary.setText("\n".join(search.summary()))
        self.not_searched_button.setVisible(bool(search.not_searched()))

    def show_not_searched(self):
        if self.search is not None:
            show_text_window(self, tr("Not searched"), "\n".join(self.search.not_searched()))

    # -- the results --

    def chosen(self):
        """The hits of the rows selected."""
        hits = self.hits
        return [hits[key] for key in (row_key(self.table, row) for row in selected_rows(self.table)) if key in hits]

    def enable(self):
        chosen = self.chosen()
        one = len(chosen) == 1
        for key, enabled, why in (("open", one, tr("Select one entry")), ("show", one, tr("Select one entry")),
                                  ("path", bool(chosen), tr("Select entries first")),
                                  ("copy", bool(chosen), tr("Select entries first")),
                                  ("delete", bool(chosen), tr("Select entries first"))):
            set_enabled(self.actions[key], enabled, why)

    def same_place(self):
        """Whether the pane still shows the place searched (else it is said and nothing is done)."""
        if self.pane.root is self.root and self.root is not None:
            return True
        self.state.setText(tr("The pane shows another place now: choose {place} there again, or search again.",
                              place=printable(self.where_text)))
        return False

    def go_to(self, hit, then=None):
        """HIT's folder in the pane, HIT selected and current there; THEN(entry) once it is listed."""
        if not self.same_place():
            return False
        pane, name = self.pane, hit.name
        folder = f"{self.start}/{hit.folder}" if self.start and hit.folder else (self.start or hit.folder)
        if pane.quick.text() and not pane.quick_matches(name):
            pane.quick.clear()  # (the quick filter would hide it)

        def listed(ok):
            if not ok:
                self.state.setText(tr("{path} could not be listed.", path=printable(self.full_path(hit))))
                return
            if pane.quick.text() and not pane.quick_matches(name):
                pane.quick.clear()
            pane.focus_name(name)
            pane.table.setFocus(Qt.FocusReason.OtherFocusReason)
            self.page.activate(pane)
            entry = pane.by_name.get(name)
            if entry is None:
                self.state.setText(tr("{path} is no longer there.", path=printable(self.full_path(hit))))
            elif then is not None:
                then(entry)
        pane.go(folder, then=listed)
        return True

    def hit_at(self, row=None):
        if row is not None and row >= 0:
            return self.hits.get(row_key(self.table, row))
        chosen = self.chosen()
        return chosen[0] if len(chosen) == 1 else None

    def show_in_folder(self, row=None):
        hit = self.hit_at(row)
        if hit is None:
            self.state.setText(tr("Select one entry"))
            return False
        return self.go_to(hit)

    def open_hit(self):
        """A file in the editor (through the root searched, as the pane opens one), a folder in the pane, a link as the
        pane takes it (in its folder)."""
        hit = self.hit_at()
        if hit is None:
            self.state.setText(tr("Select one entry"))
            return False
        if hit.kind == "file":
            self.page.open_editor(self.root, self.root_relative(hit), self.full_path(hit))
            return True
        if hit.kind == "dir":
            if not self.same_place():
                return False
            self.pane.go(self.root_relative(hit))
            self.page.activate(self.pane)
            return True
        return self.go_to(hit, then=lambda entry: self.pane.open_entry(entry))

    def copy_paths(self):
        chosen = self.chosen()
        if not chosen:
            return None
        text = "\n".join(printable(self.full_path(hit)) for hit in chosen)
        QGuiApplication.clipboard().setText(text)
        self.state.setText(tr("Copied to the clipboard: {paths}", paths=text if len(chosen) == 1 else
                              tr("{count} paths", count=len(chosen))))
        return text

    def untrusted(self, chosen):
        """Whether an agent may have written any of CHOSEN: as the pane takes each one's folder (a sandbox's folder,
        a host folder in one, a host of sandbox-ai's)."""
        folders = {f"{self.start}/{hit.folder}".strip("/") if self.start else hit.folder for hit in chosen}
        return any(self.pane.untrusted_at(folder) for folder in sorted(folders))

    def copy_to_other(self):
        """The selected entries into the other pane's folder: the page's copy (its questions and rules)."""
        from .files_page import Clip
        chosen = self.chosen()
        if not chosen or not self.same_place():
            return False
        other = self.page.other(self.pane)
        clip = Clip(self.root, [(hit.name, self.root_relative(hit), None) for hit in chosen], self.untrusted(chosen),
                    self.base)
        started = self.page.transfer_items(clip, other, other.relative)
        if not started:
            self.state.setText(printable(self.page.message.text()))
        return started

    def delete_hits(self):
        """Delete the selected entries - asked first, with their full paths (the pane's question) - as the page's job;
        their rows go once they are gone."""
        from .files_page import Chosen, delete_question, full_paths
        chosen = self.chosen()
        if not chosen or not self.same_place():
            return False
        if self.page.task.running:
            self.state.setText(tr("Wait until the copy, move or delete running now has finished (or cancel it)."))
            return False
        items = Chosen(self.root, [(hit.relative, self.root_relative(hit)) for hit in chosen], self.start, self.base,
                       self.untrusted(chosen), self.pane.mode() == "sandbox", any(hit.kind == "dir" for hit in chosen),
                       [hit.kind for hit in chosen])
        if not ask_first(self, "files-delete", tr("Delete?"), delete_question(items), tr("Delete"),
                         details="\n".join(printable(path) for path in full_paths(items))):
            return False
        started = self.page.delete_items(items)
        if started:
            root, wanted = self.root, [(hit.relative, self.root_relative(hit)) for hit in chosen]

            def ended(busy):
                if busy:
                    return
                self.page.task.changed.disconnect(ended)
                def looked():  # (in a thread: a remote root's LSTATs, a local file system that hangs)
                    gone = []
                    for shown, path in wanted:
                        try:
                            root.lstat(path)
                        except FileNotFoundError:
                            gone.append(shown)
                        except (OSError, ValueError):
                            pass  # (not known: the row stays)
                    return gone
                if isValid(self):
                    background(looked, done=lambda gone, error: self.forget(gone or []), owner=self)
            self.page.task.changed.connect(ended)
        return started

    def forget(self, gone):
        """The rows of GONE (deleted) and of what was below them go."""
        if not gone or self.search is None:
            return
        below = tuple(f"{path}/" for path in gone)
        self.gone |= {hit.relative for hit in self.search.hits if hit.relative in gone or hit.relative.startswith(below)}
        self.show_hits()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.table and event.type() == QEvent.Type.KeyPress and \
                event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & ~(
                    Qt.KeyboardModifier.KeypadModifier):
            self.show_in_folder(self.table.currentRow())
            return True
        return super().eventFilter(watched, event)

    # -- closing --

    def done(self, result):  # noqa: D401 - QDialog.done: closing stops the search
        self.stop()
        self.timer.stop()
        super().done(result)

    def closeEvent(self, event):  # noqa: N802
        self.stop()
        super().closeEvent(event)
