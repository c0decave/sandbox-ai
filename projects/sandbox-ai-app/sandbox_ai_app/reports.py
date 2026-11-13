"""Reports (sandbox-ai report): what was asked of a run, a session or a log folder - its statistics, every
destination and why it was allowed or refused, and the security report - as PDF, HTML or Markdown. A dialog asks
for the format and the file (it blocks no window: widgets.nonblocking_dialogs), the report is written in the
background by sandbox-ai itself, and opened when it is done."""
from __future__ import annotations

import os
from pathlib import Path
import re
import time

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from . import state as app_state
from .files import printable
from .i18n import N_, tr
from .widgets import ask_first, background, inform, plain_label, warn

FORMATS = (("pdf", N_("PDF: bookmarks and links, to print or pass on")),
           ("html", N_("HTML: one page for a browser, light and dark")),
           ("md", N_("Markdown: text with links, for notes and tickets")))
FOLDER_KEY = "reports"  # the folder the last report went to (state.choose_path keeps it)


def opener(path):
    """Open PATH with the program the desktop has for it (tests replace this)."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def file_name(label, kind):
    """sandbox-ai-report-LABEL-YYYYMMDD-HHMMSS.KIND, LABEL made of the characters a file name takes everywhere (to
    the second: two reports of one start within a minute had the same name)."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label or "")).strip("-.")[:60] or "runs"
    return f"sandbox-ai-report-{label}-{time.strftime('%Y%m%d-%H%M%S')}.{kind}"


class ReportDialog(QDialog):
    """The format, the file, the period, whether the parts of a start go with it, and whether to open it."""

    def __init__(self, parent, title, what, label, parts=True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.label = label
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(what))
        form = QFormLayout()
        self.format = QComboBox()
        self.format.setAccessibleName(tr("Format"))
        for key, text in FORMATS:
            self.format.addItem(tr(text), key)
        self.format.currentIndexChanged.connect(lambda _=0: self.format_changed())
        form.addRow(tr("Format"), self.format)
        self.path = QLineEdit()
        self.path.setAccessibleName(tr("File"))
        folder = ""
        current = app_state.current()
        if current is not None:
            folder = current.folder(FOLDER_KEY)
        folder = folder or str(Path.home())
        self.path.setText(os.path.join(folder, file_name(label, "pdf")))
        choose = QPushButton(tr("Choose…"))
        choose.setAccessibleName(tr("Choose the file"))
        choose.clicked.connect(lambda _=False: self.choose())
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.path, 1)
        row_layout.addWidget(choose)
        form.addRow(tr("File"), row)
        self.since = QLineEdit()
        self.since.setAccessibleName(tr("From when"))
        self.since.setPlaceholderText(tr("everything recorded (or: 2h, 3d, 2026-11-10, 14:30)"))
        form.addRow(tr("From when"), self.since)
        layout.addLayout(form)
        self.parts = QCheckBox(tr("With its MCP servers, helpers and team"))
        self.parts.setChecked(True)
        layout.addWidget(self.parts)
        self.parts.setVisible(parts)
        self.ask_parts = parts
        self.open_after = QCheckBox(tr("Open it when it is written"))
        self.open_after.setChecked(True)
        layout.addWidget(self.open_after)
        layout.addWidget(plain_label(tr("The report holds no secret, but it names hosts, paths and commands of the "
                                        "runs: share it with care."), role="secondary"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Write the report"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(620, 300)

    def kind(self):
        return self.format.currentData()

    def format_changed(self):
        """The file's suffix follows the format."""
        path = self.path.text().strip()
        stem, suffix = os.path.splitext(path)
        if suffix.lower() in (".pdf", ".html", ".htm", ".md", ".markdown"):
            self.path.setText(f"{stem}.{self.kind()}")
        elif path:
            self.path.setText(f"{path}.{self.kind()}")

    def choose(self):
        name = os.path.basename(self.path.text().strip()) or file_name(self.label, self.kind())
        paths = app_state.choose_path(self, tr("Save the report"), FOLDER_KEY, mode="save", name=name)
        if paths:
            self.path.setText(paths[0])
            self.format_changed()

    def values(self):
        """{"kind", "path", "since", "parts", "open"}; path absolute (~ expanded)."""
        path = os.path.expanduser(self.path.text().strip())
        return {"kind": self.kind(), "path": path, "since": self.since.text().strip() or None,
                "parts": self.parts.isChecked() or not self.ask_parts, "open": self.open_after.isChecked()}


def make_report(parent, sai, label, what, names=(), log_dir=None, parts=True, owner=None, source=None,
                notice=None):
    """Ask (ReportDialog), then write the report of the starts NAMES (or of every run of the log folder LOG_DIR) in
    the background and open it. NOTICE(level, title, text): how a page tells that it is written (else a message)."""
    title = tr("Report of {name}", name=printable(label))
    if not sai.has_report():
        inform(parent, title, tr("This sandbox-ai writes no reports yet: update it (sandbox-ai update)."), modal=False)
        return None
    dialog = ReportDialog(parent, title, what, label, parts)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        values = dialog.values()
    finally:
        dialog.deleteLater()
    path = values["path"]
    if not path or not os.path.isabs(path) or os.path.isdir(path) or not os.path.isdir(os.path.dirname(path)):
        warn(parent, title, tr("Not a file in an existing folder: {path}", path=printable(path or "-")), modal=False)
        return None
    # A file typed in is never replaced unasked (Choose… asks itself; a name typed or kept did not).
    if os.path.lexists(path) and not ask_first(parent, "files-replace", title,
                                               tr("{path} exists already. Replace it with the report?",
                                                  path=printable(path)), tr("Replace")):
        return None
    current = app_state.current()
    if current is not None:
        current.remember_folder(FOLDER_KEY, path)

    def done(result, error):
        if error is not None:
            warn(parent, title, tr("The report was not written: {error}", error=printable(error)), modal=False)
            return
        text = tr("Written to {path}.", path=printable(result))
        if notice is not None:
            notice("success", title, text)
        elif not values["open"]:
            inform(parent, title, text, modal=False)
        if values["open"]:
            opener(result)
    return background(sai.report, path, values["kind"], list(names), log_dir, values["parts"] if parts else True,
                      values["since"], owner=owner, source=source, timeout=630, work=True, done=done)
