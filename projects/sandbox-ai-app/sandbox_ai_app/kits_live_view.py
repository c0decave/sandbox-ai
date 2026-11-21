"""Skills, subagents, commands and plugins for a RUNNING sandbox (sandbox-ai kits|plugins attach|detach, kits
restart-agent): the Kits page's "Give to a running sandbox…" (a kit, a skill or a plugin chosen there, the start chosen
here among the running ones of this machine and the hosts) and the Sandboxes page's "Add skills/plugins…" (its start
given, what to give chosen here). Each is asked first (kits-attach, kits-detach); afterwards what the agent needs to
see it is said - nothing, a command to type in it, or a restart of the agent inside the sandbox, offered then and asked
first too (agent-restart: the sandbox, network and MCP servers stay, the conversation continues)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QPushButton,
    QWidget,
)

from . import applog, backend
from .files import printable, printable_text
from .host_choice import LOCAL, HostChooser, on_host
from .i18n import N_, tr
from .short_notices import short_notice
from .widgets import ask_first, background, explanation, plain_label, warn

KINDS = (("kit", N_("A kit")), ("skill", N_("A skill folder")), ("command", N_("A command file")),
         ("agent_file", N_("A subagent file")), ("plugin", N_("A plugin (NAME@MARKETPLACE)")))


class GiveDialog(QDialog):
    """What to give (KIND, VALUE) to which running start: with START given (the Sandboxes page) only what; else (the
    Kits page, what given) the host and one of its running starts, read in the background (host_running_starts)."""

    def __init__(self, parent, sai, host=LOCAL, start=None, kind="kit", value=""):
        super().__init__(parent)
        self.sai, self.fixed_start, self.token = sai, start, 0
        self.setWindowTitle(tr("Give to a running sandbox"))
        form = QFormLayout(self)
        self.hosts = None
        self.starts = QComboBox()
        self.starts.setAccessibleName(tr("Running start"))
        if start is None:
            self.hosts = HostChooser(sai, tr("The running starts of this machine, or of a host (over SSH)"))
            self.hosts.set_host(host)
            self.hosts.changed.connect(lambda _host: self.load_starts())
            form.addRow(self.hosts)
            form.addRow(tr("Running start"), self.starts)
            self.starts_note = plain_label(role="secondary")
            form.addRow(self.starts_note)
        else:
            self.fixed_host = host
            form.addRow(tr("Start"), plain_label(on_host(host, printable(start)), selectable=True))
        self.kind = QComboBox()
        self.kind.setAccessibleName(tr("What"))
        for key, text in KINDS:
            self.kind.addItem(tr(text), key)
        self.kind.setCurrentIndex(max(self.kind.findData(kind), 0))
        form.addRow(tr("What"), self.kind)
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        self.value = QLineEdit(value)
        self.value.setAccessibleName(tr("Which"))
        self.value.setPlaceholderText(tr("a kit's name, a path on that host, or NAME@MARKETPLACE"))
        line.addWidget(self.value, 1)
        self.browse = QPushButton(tr("Choose…"))
        self.browse.setToolTip(tr("A folder or file of this machine"))
        self.browse.clicked.connect(lambda _=False: self.choose())
        line.addWidget(self.browse)
        form.addRow(tr("Which"), row)
        self.keep = QCheckBox(tr("Also for its later starts"))
        self.keep.setToolTip(tr("A kit: --kit KIT in the start's options; a skill, command, subagent or plugin: into "
                                "the kit start-NAME of yours, which the start then names (--keep)"))
        form.addRow(self.keep)
        form.addRow(explanation(tr("Given to its agent now, read-only, with the checks of a start; a kit's hooks are "
                                   "not given while it runs. Whether the agent sees it by itself, after a command or "
                                   "after it started again is said afterwards.")))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        self.kind.currentIndexChanged.connect(lambda _=0: self.kind_changed())
        self.kind_changed()
        self.resize(560, 0)
        if start is None:
            self.load_starts()

    def host(self):
        return self.hosts.host() if self.hosts is not None else self.fixed_host

    def kind_changed(self):
        local = self.host() in (None, LOCAL)
        self.browse.setEnabled(local and self.kind.currentData() in ("skill", "command", "agent_file"))

    def choose(self):
        kind = self.kind.currentData()
        path = QFileDialog.getExistingDirectory(self, tr("Skill folder")) if kind == "skill" else \
            QFileDialog.getOpenFileName(self, tr("File"))[0]
        if path:
            self.value.setText(path)

    def load_starts(self):
        """The running starts of the host chosen, in the background (over SSH for a host)."""
        self.token += 1
        token, host = self.token, self.host()
        self.starts.clear()
        self.starts_note.setText(tr("Reading the running starts…"))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.kind_changed()
        background(lambda: self.sai.host_running_starts(host), owner=self, timeout=90,
                   done=lambda rows, error: self.starts_loaded(token, rows, error))

    def starts_loaded(self, token, rows, error):
        if token != self.token:
            return
        if error is not None:
            self.starts_note.setText(tr("The running starts could not be read: {error}",
                                        error=printable_text(error)[:300]))
            return
        for row in rows or []:
            name = str(row.get("name") or "")
            if backend.valid_run(name):
                self.starts.addItem(printable(f"{name} ({row.get('agent') or '?'})"), name)
        self.starts_note.setText("" if self.starts.count() else tr("No kept start runs there now."))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.starts.count()))

    def values(self):
        """(host, start, kind, value, keep)."""
        start = self.fixed_start if self.fixed_start is not None else self.starts.currentData()
        return self.host(), start, self.kind.currentData(), self.value.text().strip(), self.keep.isChecked()


def give(parent, sai, host, start, kind, value, keep, source=None, after=None, allow_findings=False):
    """Asked first (kits-attach), then given in the background; what came of it said (offer_restart). Refused for
    hidden text (the hidden-text checker's findings), the findings are shown and giving it anyway is asked
    (ALLOW_FINDINGS: --allow-findings)."""
    title = tr("Give to a running sandbox")
    try:
        args = backend.kits_live_args(start, kind, value, keep=keep, allow_findings=allow_findings)
    except ValueError as exc:
        warn(parent, title, printable_text(exc))
        return
    text = tr("Give the plugin {what} to {start} now, while it runs? It is installed into the plugin store if needed; "
              "its code runs as the agent, its hooks only once approved.", what=printable(value), start=printable(start)) \
        if kind == "plugin" else tr("Give {what} to {start} now, while it runs? Its agent gets it read-only at once; a "
                                    "kit's hooks are not given.", what=printable(value), start=printable(start))
    if not allow_findings and not ask_first(parent, "kits-attach", title, on_host(host, text), tr("Give it now")):
        return
    applog.log("INFO", "kits", f"give {kind} {printable(value)} to {printable(start)} on {printable(host)}")
    retry = None if allow_findings else (
        lambda: give(parent, sai, host, start, kind, value, keep, source, after, allow_findings=True))
    background(lambda: sai.host_kits_attach(host, args), owner=parent, work=True, source=source, timeout=990,
               done=lambda result, error: given(parent, sai, host, start, title, result, error, source, after,
                                                retry=retry))


def take_out(parent, sai, host, start, value, source=None, after=None):
    """What was given (its name, a kit's name, or a plugin) out of START again, asked first (kits-detach)."""
    title = tr("Take out of a running sandbox")
    try:
        args = backend.kits_detach_args(start, value)
    except ValueError as exc:
        warn(parent, title, printable_text(exc))
        return
    if not ask_first(parent, "kits-detach", title, on_host(host, tr(
            "Take {what} out of {start} now (and out of its later starts where it was kept)?", what=printable(value),
            start=printable(start))), tr("Take it out")):
        return
    background(lambda: sai.host_kits_detach(host, args), owner=parent, work=True, source=source, timeout=150,
               done=lambda result, error: given(parent, sai, host, start, title, result, error, source, after))


def given(parent, sai, host, start, title, result, error, source=None, after=None, retry=None):
    """What sandbox-ai said; where the agent reads it only when it starts, the offer to start it again inside the
    sandbox (asked first: agent-restart). Refused for hidden text: the findings shown, and RETRY (giving it with
    --allow-findings) offered."""
    if after is not None:
        after()
    applog.log("INFO" if error is None else "WARNING", "kits", f"{title} {printable(start)} on {printable(host)}: "
               + ("done" if error is None else f"not done: {applog.error_text(error)}"))
    if error is not None and retry is not None and "--allow-findings" in str(error):
        if ask_first(parent, "kits-attach", title, tr(
                "It holds text its agent would read that may hide instructions from you:\n\n{findings}\n\nGive it "
                "anyway?", findings=printable_text(error)[:1500]), tr("Give it anyway")):
            retry()
        return
    if error is not None:
        warn(parent, title, tr("Not done: {reason}", reason=printable_text(error)[:1500]), modal=False)
        return
    text = "\n\n".join([result.get("message") or "", *result.get("notes", [])]).strip()
    reloads = result.get("reload") or []
    if any(item.get("reload") == "restart" for item in reloads) and not result.get("restarted"):
        if ask_first(parent, "agent-restart", title, text + "\n\n" + tr(
                "Start the agent again inside its sandbox now? What it does now ends; its conversation continues, the "
                "sandbox, its network and MCP servers stay."), tr("Start the agent again")):
            background(lambda: sai.host_agent_restart(host, start), owner=parent, work=True, source=source,
                       timeout=150, done=lambda done_, error_: restarted(parent, title, done_, error_))
        return
    short_notice(parent, title, text)


def restarted(parent, title, result, error):
    if error is not None:
        warn(parent, title, tr("The agent was not started again: {reason}", reason=printable_text(error)[:1500]),
             modal=False)
        return
    short_notice(parent, title, (result or {}).get("message") or tr("The agent was started again."))
