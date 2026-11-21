"""What a dialog shows when it is opened again, and what it keeps of what was typed (one rule for the whole app).

A dialog that **changes something that exists** - what a run may reach (Allow…, Revoke…), its limits, devices,
workspaces and MCP servers, the kill switch, a server's settings, an identity, a host - shows that thing as it is
now: read from sandbox-ai's JSON output on the thing's host each time it opens (in the background), never from what
the dialog showed the last time. What it has already is shown as such (ticked and greyed, marked, or listed above);
only what is new or changed is sent. Such a dialog keeps no draft: opened again, it shows the thing as it is then.

A dialog that **adds something new** keeps what was typed and not sent - Cancel, Esc, the window closed, a "no" to
the question that follows, or sandbox-ai refused it - as a draft: filled in again when the same dialog opens for the
same target (the same run on the same host, the same host's list), with a line that says so and *Empty the form*.
The draft lives in this app's memory only: never written to a file, gone when the app quits; it is dropped once what
it held was sent and taken (drop()).

Never kept, whatever the dialog: a secret value, password, token or key typed - every field that holds a secret's
value (widgets.secret_value / widgets.mark_secret: also while "Show value" shows it), every field shown as dots
(QLineEdit.Password, PasswordEchoOnEdit) and every field marked with mark_secret() here - the key of a VPN profile or
of an LLM server, a login's token, a token typed in the wizard. The *names* of secrets, variables and files that hold
a token are no secret and are kept. Nor kept: a box that replaces what exists (Replace a profile or server of that
name) or lets plain http leave the local networks - chosen anew each time, never ticked by a draft; a dialog leaves
such a box out of its FIELDS.

Draft(dialog, key, fields) does it for one dialog: FIELDS {name: widget} (QLineEdit, QPlainTextEdit, a checkable
button or group box, QComboBox - also an editable one such as name_fields.NameField -, QSpinBox, or a field of its own with
draft_value() and set_draft_value(value): ui.MountRows), KEY a tuple naming the dialog and its target. Drafts are kept
by: Allow… and Revoke… (per run and host), Add device… and Add workspace… (per run and host), New VPN profile, New
LLM server, New Git login, New publish login (per host), Add a host, a team's task (per team and host), Resume with…
(per start and host), Start a team (per host), New bridge (per pair chosen), the kits' New kit, New skill, Add plugin,
Add marketplace and Add hook (per host and kit), the images' New variant, Variant from a recipe, Add an image from a
registry (per reference) and from a Containerfile (per host). The wizard keeps its pages on the board instead
(wizard.Page.cleanupPage: Back and Next again; another model changes only what depends on the model:
wizard.StartPage.keep_model), and the web page keeps its cards' drafts the same way (sandbox/web/app.js,
cardDraft)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from .i18n import tr

DRAFTS = {}  # KEY -> {field name: value}: this app session's drafts, in memory only
LIMIT = 64  # drafts kept at most (the oldest go first)


def mark_secret(widget):
    """WIDGET holds a secret (a key, a password, a token): never kept in a draft, whatever its echo mode."""
    widget.setProperty("secret", True)
    return widget


def secret(widget):
    if widget.property("secret") or widget.property("secret_value"):  # (widgets.mark_secret: shown or not)
        return True
    return isinstance(widget, QLineEdit) and widget.echoMode() != QLineEdit.EchoMode.Normal


def value_of(widget):
    if callable(getattr(widget, "draft_value", None)):  # a field of its own (ui.MountRows): its rows as data
        return widget.draft_value()
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, QPlainTextEdit):
        return widget.toPlainText()
    if isinstance(widget, QAbstractButton) or (isinstance(widget, QGroupBox) and widget.isCheckable()):
        return widget.isChecked()
    if isinstance(widget, QSpinBox):
        return widget.value()
    if isinstance(widget, QComboBox):
        if widget.isEditable():
            return widget.currentText()
        data = widget.currentData()
        return ["data", data] if data is not None else ["index", widget.currentIndex()]
    return None


def set_value(widget, value):
    if callable(getattr(widget, "set_draft_value", None)):
        widget.set_draft_value(value)
    elif isinstance(widget, QLineEdit) and isinstance(value, str):
        widget.setText(value)
    elif isinstance(widget, QPlainTextEdit) and isinstance(value, str):
        widget.setPlainText(value)
    elif isinstance(widget, (QAbstractButton, QGroupBox)) and isinstance(value, bool):
        widget.setChecked(value)
    elif isinstance(widget, QSpinBox) and isinstance(value, int):
        widget.setValue(value)
    elif isinstance(widget, QComboBox):
        if widget.isEditable() and isinstance(value, str):
            if hasattr(widget, "setText"):  # (a NameField: its own text, as a QLineEdit's)
                widget.setText(value)
            else:
                widget.setEditText(value)
        elif isinstance(value, list) and len(value) == 2:
            index = widget.findData(value[1]) if value[0] == "data" else value[1]
            if isinstance(index, int) and 0 <= index < widget.count():
                widget.setCurrentIndex(index)


def capture(fields):
    """{name: value} of FIELDS, secrets left out (and a field gone with its row)."""
    found = {}
    for name, widget in fields.items():
        try:
            if widget is not None and not secret(widget):
                found[name] = value_of(widget)
        except RuntimeError:  # deleted (a row taken out of the form meanwhile)
            continue
    return found


def keep(key, values):
    DRAFTS.pop(key, None)
    DRAFTS[key] = values
    while len(DRAFTS) > LIMIT:
        DRAFTS.pop(next(iter(DRAFTS)))


def drop(key):
    """What the draft KEY held was sent and taken: it is not offered again."""
    DRAFTS.pop(key, None)


class Draft:
    """The draft of one dialog (see the module's docstring): restored when the dialog is made, kept when it closes
    (whatever its answer: an OK may still be refused afterwards - the caller drops it once sent), a line with
    *Empty the form* while it shows a draft."""

    def __init__(self, dialog, key, fields, notice=True):
        self.dialog, self.key, self.fields = dialog, key, dict(fields)
        self.initial = capture(self.fields)
        self.restored = False
        self.notice = QWidget(dialog)
        row = QHBoxLayout(self.notice)
        row.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(tr("Filled in with what you typed last time and did not send."))
        self.label.setWordWrap(True)
        self.label.setProperty("role", "notice")
        self.empty_button = QPushButton(tr("Empty the form"))
        self.empty_button.setToolTip(tr("Back to an empty form; the draft is forgotten"))
        self.empty_button.clicked.connect(lambda _=False: self.empty())
        row.addWidget(self.label, 1)
        row.addWidget(self.empty_button)
        self.notice.hide()
        if notice:
            layout = dialog.layout()
            if isinstance(layout, QFormLayout):
                layout.insertRow(0, self.notice)
            elif isinstance(layout, QVBoxLayout):
                layout.insertWidget(0, self.notice)
        kept = DRAFTS.get(key)
        if kept:
            for name, widget in self.fields.items():
                if widget is not None and name in kept and not secret(widget) and widget.isEnabled():
                    set_value(widget, kept[name])
            self.restored = capture(self.fields) != self.initial
            self.notice.setVisible(self.restored)
        dialog.finished.connect(lambda _result: self.keep())

    def keep(self):
        values = capture(self.fields)
        if values == self.initial:
            drop(self.key)
        else:
            keep(self.key, values)

    def empty(self):
        for name, widget in self.fields.items():
            if widget is not None and name in self.initial:
                set_value(widget, self.initial[name])
        drop(self.key)
        self.restored = False
        self.notice.hide()

    def sent(self):
        drop(self.key)
