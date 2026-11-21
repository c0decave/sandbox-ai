"""Settings > Notifications: the phone channels of this machine or of a host (`sandbox-ai notify` there).

A channel sends a sandbox's questions and alarms - and, when chosen, safeguard events, limits reached and a run's end
or failure - to ntfy, Telegram, WhatsApp or a webhook while you are away; the messages go out from that host (its
launcher, a session's supervisor), never from inside a sandbox. The table lists them with where each sends, its
events, when and how much a message says, and how its last message went; Add…, Edit…, Test, Remove… and Refresh are
beside it and on its right-click menu (Delete removes). Tokens are only the names of that host's secrets (Credentials
> Secrets): no token is ever typed or shown here. Adding (or changing) and removing a channel are asked first
(notify-add, notify-remove); every command runs in the background, on another host through `sandbox-ai hosts run`.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from .files import printable, printable_text
from .host_choice import on_host
from .i18n import N_, tr
from .widgets import (
    ask_first, background, delete_key, drop_after_exec, fill, list_menu, outcome, plain_label, selected_keys, table,
    warn,
)

LOCAL = "local"
KINDS = ("ntfy", "telegram", "whatsapp", "webhook")
EVENTS = (("questions", N_("Questions")), ("alarms", N_("Alarms (always, also in quiet hours)")),
          ("safeguards", N_("Safeguards")), ("limits", N_("Limits reached")), ("run-ended", N_("Run ended")),
          ("run-failed", N_("Run failed")), ("budget", N_("Budget nearly or all used")))
DEFAULT_EVENTS = ("questions", "alarms")
WHEN = (("away", N_("away: while neither the app nor the web interface watches")), ("always", N_("always")))
LINK_EXAMPLE = "https://web.example.org:8443"  # (an address: the same in every language)
# Each kind's own fields: (notify.json's name, label, example)
FIELDS = {
    "ntfy": (("url", N_("Server"), "https://ntfy.sh"), ("topic", N_("Topic"), "a-long-random-topic-4f9c2"),
             ("token_secret", N_("Token (a secret's name, optional)"), "NTFY_TOKEN")),
    "telegram": (("bot_token_secret", N_("Bot token (a secret's name)"), "TG_BOT"),
                 ("chat", N_("Chat ID"), "123456789"),
                 ("api", N_("Another API URL (optional)"), "https://api.telegram.org")),
    "whatsapp": (("phone_id", N_("Phone-number ID"), "106540352242922"),
                 ("token_secret", N_("Access token (a secret's name)"), "WA_TOKEN"),
                 ("to", N_("Recipient"), "+4917612345678"),
                 ("template", N_("Approved template (optional)"), "sandbox_alert"),
                 ("language", N_("Its language"), "en"),
                 ("api", N_("Another API URL (optional)"), "https://graph.facebook.com/v23.0")),
    "webhook": (("url", N_("URL"), "https://hooks.example.org/in"),
                ("url_secret", N_("Or: the URL is the secret"), "HOOK_URL"),
                ("token_secret", N_("Token (a secret's name, optional)"), "HOOK_TOKEN")),
}
NOTES = {
    "ntfy": N_("On ntfy.sh whoever knows the topic reads its messages: take a long random one, or a server of your "
               "own with a token."),
    "telegram": N_("Create a bot with @BotFather, keep its token as a secret, send the bot a message; your chat ID: "
                   "@userinfobot. Sent as plain text."),
    "whatsapp": N_("WhatsApp Business Cloud API: a business may start a conversation only with an approved template "
                   "whose body has one variable. Without a template, plain text arrives only within 24 hours "
                   "after the recipient last wrote to that number."),
    "webhook": N_("An HTTPS POST of JSON. A URL that carries its key (Slack, Discord): keep it as a secret."),
}


class ChannelDialog(QDialog):
    """One channel: its name (fixed when editing), kind, the kind's fields, events, when, detail, quiet hours and
    rate. values() gives notify.json's fields; CHECK(fields) (the command's own check) runs before it closes."""

    def __init__(self, check, channel=None, parent=None):
        super().__init__(parent)
        self.check, self.inputs, self.rows = check, {}, {}
        self.setWindowTitle(tr("Edit phone channel") if channel else tr("Add phone channel"))
        channel = channel or {}
        layout = QVBoxLayout(self)
        self.form = form = QFormLayout()
        layout.addLayout(form)
        self.name = QLineEdit(channel.get("name", ""))
        self.name.setPlaceholderText(tr("phone"))
        self.name.setReadOnly(bool(channel))
        form.addRow(tr("Name"), self.name)
        self.kind = QComboBox()
        for kind in KINDS:
            self.kind.addItem(kind, kind)
        self.kind.setCurrentIndex(max(0, self.kind.findData(channel.get("kind", "ntfy"))))
        form.addRow(tr("Kind"), self.kind)
        for kind, fields in FIELDS.items():
            for field, label, example in fields:
                key = (kind, field)
                edit = QLineEdit(str(channel.get(field) or "") if channel.get("kind") == kind else "")
                edit.setPlaceholderText(example)
                edit.setAccessibleName(tr(label))
                self.inputs[key] = edit
                form.addRow(tr(label), edit)
                self.rows[key] = edit
        self.note = plain_label("", role="secondary")
        form.addRow(self.note)
        events = channel.get("events") or list(DEFAULT_EVENTS)
        self.events = {}
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        for key, label in EVENTS:
            check_box = QCheckBox(tr(label))
            check_box.setChecked(key in events)
            self.events[key] = check_box
            column.addWidget(check_box)
        form.addRow(tr("Events"), box)
        self.when = QComboBox()
        for key, text in WHEN:
            self.when.addItem(tr(text), key)
        self.when.setCurrentIndex(max(0, self.when.findData(channel.get("when", "away"))))
        form.addRow(tr("When"), self.when)
        self.detail = QComboBox()
        for key, text in (("minimal", N_("minimal: only that something waits")),
                          ("normal", N_("normal: kind, run, what, time")),
                          ("full", N_("full: also destinations and paths"))):
            self.detail.addItem(tr(text), key)
        self.detail.setCurrentIndex(max(0, self.detail.findData(channel.get("detail", "normal"))))
        form.addRow(tr("A message says"), self.detail)
        self.quiet = QLineEdit(channel.get("quiet") or "")
        self.quiet.setPlaceholderText("22:00-07:00")
        form.addRow(tr("Quiet hours (alarms still go)"), self.quiet)
        self.rate = QSpinBox()
        self.rate.setRange(0, 86400)
        self.rate.setSuffix(" s")
        self.rate.setValue(channel.get("rate", 60) if isinstance(channel.get("rate"), int) else 60)
        form.addRow(tr("At most one message per"), self.rate)
        self.problem = plain_label("", role="notice-error", selectable=True)
        self.problem.hide()
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("Save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Cancel"))
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(lambda _: self.show_kind())
        self.show_kind()

    def show_kind(self):
        kind = self.kind.currentData()
        for (owner, _field), edit in self.rows.items():
            self.form.setRowVisible(edit, owner == kind)
        self.note.setText(tr(NOTES[kind]))

    def values(self):
        kind = self.kind.currentData()
        fields = {"name": self.name.text().strip(), "kind": kind,
                  "events": [key for key, box in self.events.items() if box.isChecked()],
                  "when": self.when.currentData(), "detail": self.detail.currentData(), "rate": self.rate.value()}
        for field, _label, _example in FIELDS[kind]:
            text = self.inputs[(kind, field)].text().strip()
            if text:
                fields[field] = text
        if self.quiet.text().strip():
            fields["quiet"] = self.quiet.text().strip()
        return fields

    def save(self):
        try:
            self.check(self.values())
        except (ValueError, TypeError, AttributeError) as exc:
            self.problem.setText(printable_text(str(exc))[:500])
            self.problem.show()
            return
        self.accept()


class PhoneChannels(QWidget):
    """The phone channels of the host chosen: a table, Add…, Edit…, Test, Remove…, Refresh (also on the right-click
    menu), and the link to the web interface the messages carry."""

    def __init__(self, sai, parent=None):
        super().__init__(parent)
        from .settings import host_row
        self.sai, self.channels, self.token, self.link_now = sai, [], 0, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.hosts = host_row(sai, self)
        layout.addWidget(self.hosts)
        self.list = table([tr("Name"), tr("Kind"), tr("Sends to"), tr("Events"), tr("When, detail"),
                           tr("Last message")], stretch=2, compact=(0, 1))
        self.list.setAccessibleName(tr("Phone channels"))
        self.list.setMinimumHeight(140)
        self.list.itemSelectionChanged.connect(self.update_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.edit())
        layout.addWidget(self.list)
        row = QHBoxLayout()
        self.add_button, self.edit_button = QPushButton(tr("Add…")), QPushButton(tr("Edit…"))
        self.test_button, self.remove_button = QPushButton(tr("Test")), QPushButton(tr("Remove…"))
        self.refresh_button = QPushButton(tr("Refresh"))
        self.test_button.setToolTip(tr("sandbox-ai notify test: send a test message now and say how it went"))
        for button, slot in ((self.add_button, self.add), (self.edit_button, self.edit), (self.test_button, self.test),
                             (self.remove_button, self.remove), (self.refresh_button, self.refresh)):
            button.clicked.connect(lambda _=False, slot=slot: slot())
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        list_menu(self.list, lambda: [self.test_button, self.edit_button, self.remove_button, None, self.add_button,
                                      self.refresh_button])
        delete_key(self.list, self.remove)
        link_row = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setPlaceholderText(LINK_EXAMPLE)
        self.link.setAccessibleName(tr("Link in the messages"))
        self.link_button = QPushButton(tr("Save link"))
        self.link_button.clicked.connect(lambda _=False: self.save_link())
        link_row.addWidget(plain_label(tr("Link in the messages")))
        link_row.addWidget(self.link, 1)
        link_row.addWidget(self.link_button)
        layout.addLayout(link_row)
        self.status = plain_label(role="secondary", selectable=True, log="settings")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        if not sai.has_notify():
            self.status.setText(tr("This sandbox-ai has no phone notifications yet; update it."))
            for button in self.buttons():
                button.setEnabled(False)
        else:
            self.update_buttons()
            self.refresh()

    def buttons(self):
        return [self.add_button, self.edit_button, self.test_button, self.remove_button, self.refresh_button,
                self.link_button]

    def host(self):
        return self.hosts.host() or LOCAL

    def where(self, host=None):
        return self.hosts.label(host or self.host())

    def refresh(self):
        host = self.host()
        self.token += 1
        token = self.token
        self.status.setText(tr("Asking {host}…", host=self.where(host)))
        background(lambda: self.sai.notify_channels(host), owner=self, source=self.buttons(), timeout=120,
                   done=lambda result, error: self.loaded(token, host, result, error))

    def loaded(self, token, host, result, error):
        if token != self.token:
            return
        if error is not None:
            self.channels = []
            fill(self.list, [])
            self.status.setText(
                tr("The sandbox-ai of {host} has no phone notifications yet; update it (Hosts page).",
                   host=self.where(host))
                if "unknown command 'notify'" in str(error) else
                tr("Cannot list the phone channels: {error}", error=printable_text(str(error))[:400]))
            self.update_buttons()
            return
        self.channels = result.get("channels", [])
        self.link_now = result.get("link")
        self.link.setText(result.get("link") or "")
        rows, colors = [], []
        for channel in self.channels:
            status = channel.get("status") if isinstance(channel.get("status"), dict) else None
            said = "" if status is None else tr("sent") if status.get("ok") else \
                tr("failed: {error}", error=printable(status.get("error", "")))
            how = f"{channel.get('when', '')}, {channel.get('detail', '')}" + (
                f", {tr('quiet {hours}', hours=printable(channel['quiet']))}" if channel.get("quiet") else "")
            rows.append([printable(channel["name"]), printable(channel.get("kind", "")),
                         printable(channel.get("where", "")), ", ".join(printable(e) for e in channel.get("events", [])),
                         how, said])
            colors.append(None if status is None else "ok" if status.get("ok") else "bad")
        fill(self.list, rows, colors, keys=[channel["name"] for channel in self.channels])
        self.status.setText(tr("{count} phone channels on {host}. The messages go out from that host, never from "
                               "inside a sandbox; tokens are the names of its secrets.", count=len(self.channels),
                               host=self.where(host)) if self.channels else
                            tr("No phone channels on {host}. Add one: questions and alarms reach your phone while "
                               "you are away.", host=self.where(host)))
        self.update_buttons()

    def chosen(self):
        keys = selected_keys(self.list)
        return next((c for c in self.channels if keys and c["name"] == keys[0]), None)

    def update_buttons(self):
        if not self.sai.has_notify():
            return
        one = self.chosen() is not None
        for button in (self.edit_button, self.test_button, self.remove_button):
            button.setEnabled(one)

    def check(self, fields):
        return self.sai.module("notify").add_arguments(fields)

    def add(self):
        self.open_dialog(None)

    def edit(self):
        channel = self.chosen()
        if channel is not None:
            self.open_dialog(channel)

    def open_dialog(self, channel):
        host = self.host()
        dialog = ChannelDialog(self.check, channel, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        fields = dialog.values() if accepted else None
        drop_after_exec(dialog)  # (made anew each time)
        if not accepted:
            return
        if not ask_first(self, "notify-add", tr("Phone notifications"), on_host(host, tr(
                "Send the questions and alarms of this host to {name} ({kind})? The service receives what the detail "
                "allows: the kind, the run's name, a short what, and with full also destinations and paths.",
                name=printable(fields["name"]), kind=printable(fields["kind"]))), tr("Save")):
            return
        said = tr("Saved: {name}", name=printable(fields["name"]))
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.notify_add(host, fields), owner=self, work=True, source=self.buttons(),
                   timeout=120, done=lambda result, error: self.done(said, result, error))

    def test(self):
        channel, host = self.chosen(), self.host()
        if channel is None:
            return
        name = channel["name"]
        self.status.setText(tr("Sending a test message on {name}…", name=printable(name)))
        background(lambda: self.sai.notify_test(host, name), owner=self, work=True, source=self.buttons(),
                   timeout=120, done=lambda result, error: self.tested(name, result, error))

    def tested(self, name, result, error):
        status, out, err = outcome(result, error)
        text = printable_text((out + err).strip())[-1000:]
        self.status.setText(tr("Sent: {name}", name=printable(name)) if status == 0 else
                            tr("Not sent: {name}: {error}", name=printable(name), error=text))
        self.refresh()

    def remove(self):
        channel, host = self.chosen(), self.host()
        if channel is None:
            return
        name = channel["name"]
        if not ask_first(self, "notify-remove", tr("Phone notifications"), on_host(host, tr(
                "Remove the phone channel {name}? Its questions and alarms no longer reach you there.",
                name=printable(name))), tr("Remove")):
            return
        said = tr("Removed: {name}", name=printable(name))
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.notify_remove(host, name), owner=self, work=True, source=self.buttons(),
                   timeout=120, done=lambda result, error: self.done(said, result, error))

    def save_link(self):
        host, url = self.host(), self.link.text().strip() or None
        said = tr("Link saved") if url else tr("No link in the messages")
        self.status.setText(tr("Working…"))
        background(lambda: self.sai.notify_link(host, url), owner=self, work=True, source=self.buttons(),
                   timeout=120, done=lambda result, error: self.done(said, result, error))

    def done(self, said, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            warn(self, tr("Phone notifications"), printable_text((out + err).strip())[-1500:] or
                 tr("Failed (exit status {status}).", status=status))
            self.status.setText(tr("Not done."))
        else:
            self.status.setText(said)
        self.refresh()
