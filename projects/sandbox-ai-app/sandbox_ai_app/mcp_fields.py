"""An MCP server's limits (memory, CPUs, processes) and folders as fields that say at once what is wrong, and the
Properties dialog of a server defined here (sandbox-ai mcp set). The values are checked by sandbox-ai's own rules -
mcp.py's checked_memory, checked_cpus, checked_pids and mount_host, the rules of `mcp add`, `mcp set` and
--mcp-limits - never by a copy of them: a field shows the reason next to it while it is typed, and OK takes nothing
that is not valid (a dialog stays open until it is, or until sandbox-ai took it)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QWidget,
)

from . import backend
from .files import printable
from .i18n import N_, tr
from .widgets import background, plain_label

LIMIT_FIELDS = (
    ("memory", N_("Memory"), N_("a whole number with m or g, like 512m or 2g"), "--memory"),
    ("cpus", N_("CPUs"), N_("a number of CPUs from 0.01 to 1024, like 2 or 1.5"), "--cpus"),
    ("pids", N_("Processes"), N_("a whole number from 16 to 4096 (processes and threads)"), "--pids"),
    ("tmp", N_("/tmp"), N_("a whole number with m or g, like 64m or 1g (memory: at most the memory limit)"), "--tmp"),
)
CHECKS = {"memory": "checked_memory", "cpus": "checked_cpus", "pids": "checked_pids", "tmp": "checked_tmp"}
FITS = "check_tmp_fits"  # /tmp (a tmpfs) at most the memory limit


def limit_checks(mcp):
    """{key: check(value) -> the value in its usual form, ValueError when it is not valid} of sandbox-ai's MCP module
    MCP; {} for one without them (an older sandbox-ai: nothing is checked here, the command has the last word)."""
    found = {}
    for key, name in CHECKS.items():
        function = getattr(mcp, name, None) if mcp is not None else None
        if callable(function):
            found[key] = function
    fits = getattr(mcp, FITS, None) if mcp is not None else None
    if callable(fits):
        found["_fits"] = fits
    return found


def mcp_module(sai):
    """sandbox-ai's MCP module of SAI (the app's own installation), or None."""
    try:
        return sai.module("mcp") if sai is not None else None
    except Exception:  # noqa: BLE001 - a sandbox-ai that does not load: nothing checked here
        return None


def mark(widget, bad):
    """WIDGET drawn as holding a value that is not valid (the theme's QLineEdit[invalid="true"])."""
    if widget.property("invalid") != bool(bad):
        widget.setProperty("invalid", bool(bad))
        widget.style().unpolish(widget)
        widget.style().polish(widget)


class LimitsFields(QWidget):
    """Memory, CPUs, processes and /tmp: each field empty for the default (DEFAULTS, shown in it as such: "default:
    512m"), a value typed in checked at once (its reason next to it, the field marked), Reset to defaults empties them
    all.
    changed: the text changed (valid or not); committed: a field was left or reset, and everything is valid."""
    changed = Signal()
    committed = Signal()

    def __init__(self, checks, defaults=None, values=None, default_text=None, parent=None):
        super().__init__(parent)
        self.checks, self.default_text = dict(checks or {}), default_text
        self.fits = self.checks.pop("_fits", None)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        self.fields, self.reasons = {}, {}
        for row, (key, label, _rule, _option) in enumerate(LIMIT_FIELDS):
            edit = QLineEdit(str((values or {}).get(key, "") or ""))
            edit.setAccessibleName(tr(label))
            edit.textChanged.connect(lambda _text, k=key: self.check(k))
            edit.editingFinished.connect(self.commit)
            reason = plain_label(role="notice-error")
            reason.setVisible(False)
            caption = QLabel(tr(label))
            caption.setBuddy(edit)
            grid.addWidget(caption, row * 2, 0)
            grid.addWidget(edit, row * 2, 1)
            grid.addWidget(reason, row * 2 + 1, 1)
            self.fields[key], self.reasons[key] = edit, reason
        self.reset_button = QPushButton(tr("Reset to defaults"))
        self.reset_button.setToolTip(tr("Empty every field: the defaults hold"))
        self.reset_button.clicked.connect(self.reset)
        grid.addWidget(self.reset_button, len(LIMIT_FIELDS) * 2, 1)
        self.set_defaults(defaults)
        for key in self.fields:
            self.check(key)

    def set_defaults(self, defaults):
        """DEFAULTS ({memory, cpus, pids}, or None: not known) shown in the empty fields."""
        self.defaults = defaults if isinstance(defaults, dict) else None
        for key, edit in self.fields.items():
            value = (self.defaults or {}).get(key)
            edit.setPlaceholderText(tr("default: {value}", value=tr("no limit") if value == "none" else printable(value))
                                    if value not in (None, "") else self.default_text or tr("default"))

    def reason(self, key):
        """Why the value of field KEY is not valid (None: it is, or it is empty)."""
        text = self.fields[key].text().strip()
        if not text or key not in self.checks:
            return None
        try:
            self.checks[key](text)
        except ValueError:
            rule = next(rule for k, _label, rule, _option in LIMIT_FIELDS if k == key)
            return tr("Not valid: {rule}", rule=tr(rule))
        if key == "tmp" and self.fits is not None:
            memory = self.fields["memory"].text().strip() or (self.defaults or {}).get("memory")
            try:
                self.fits({"tmp": text.lower(), "memory": str(memory or "").lower()})
            except ValueError:
                return tr("Not valid: /tmp is memory (a tmpfs) and may not be more than the memory limit {memory}",
                          memory=printable(memory))
        return None

    def check(self, key):
        if key == "memory" and "tmp" in self.fields and key != "tmp":
            self.check("tmp")  # the memory limit decides how large /tmp may be
        reason = self.reason(key)
        self.reasons[key].setText(reason or "")
        self.reasons[key].setVisible(bool(reason))
        mark(self.fields[key], reason)
        self.changed.emit()

    def problems(self):
        """{key: reason} of the fields whose values are not valid."""
        return {key: reason for key in self.fields if (reason := self.reason(key))}

    def valid(self):
        return not self.problems()

    def values(self):
        """{memory, cpus, pids} of the fields with a valid value, in their usual form ("2g", "1.5", 512); empty ones
        (the defaults) left out."""
        found = {}
        for key, edit in self.fields.items():
            text = edit.text().strip()
            if text and self.reason(key) is None:
                found[key] = self.checks[key](text) if key in self.checks else text
        return found

    def set_values(self, values):
        for key, edit in self.fields.items():
            edit.setText(str((values or {}).get(key, "") or ""))

    def reset(self):
        self.set_values({})
        self.commit()

    def commit(self):
        if self.valid():
            self.committed.emit()

    def focus_first_problem(self):
        for key in self.problems():
            self.fields[key].setFocus()
            return True
        return False


def mount_reason(mcp, target, host, mode):
    """Why the folder HOST (of this machine) cannot be the server's TARGET with MODE (mcp.mount_host: a read-only one
    must be there, a writable one is created at OK), or None. Nothing to check without that rule."""
    function = getattr(mcp, "mount_host", None) if mcp is not None else None
    if not callable(function) or not host:
        return None
    try:
        function(f"{target}={host}:{mode}", host, mode, create=False)
    except ValueError as exc:
        return printable(str(exc))
    return None


class ServerPropertiesDialog(QDialog):
    """Properties of an MCP server defined here (`sandbox-ai mcp set NAME`): its limits for every start (empty: the
    built-in default), its network, VPN, folders (the targets its catalog entry takes) and variables. Each value is
    checked while it is typed (limit_checks, mount_reason; another host's folders by that host at OK); OK runs `mcp
    set` with the dialog open and closes it only once sandbox-ai took the change - a refusal is shown here, to be
    corrected. INFO: the server as `mcp list --json` lists it; ENTRY: its catalog entry, if loaded (the folders and
    variables it takes); SUBMIT(arguments, done): runs `sandbox-ai ARGUMENTS` and calls done(status, out, err)."""

    def __init__(self, parent, sai, info, entry=None, host="local", submit=None):
        super().__init__(parent)
        self.sai, self.info, self.entry, self.host, self.submit = sai, dict(info), entry, host, submit
        self.mcp = mcp_module(sai)
        name = str(info.get("name", ""))
        self.setWindowTitle(tr("Properties of {name}", name=printable(name)) if host == "local" else
                            tr("Properties of {name} on {host}", name=printable(name), host=printable(host)))
        form = self.form = QFormLayout(self)
        own, kind = info.get("own", True), info.get("kind", "image")
        form.addRow(plain_label(tr("The defaults of every start that uses it; a board can set other limits for "
                                   "its own starts (its MCP node)."), role="secondary"))
        definition = info.get("definition") if isinstance(info.get("definition"), dict) else {}
        current = {key: definition.get(key) for key in ("memory", "cpus", "pids", "tmp")
                   if definition.get(key) not in (None, "")}
        if kind == "command":
            current = {key: value for key, value in (info.get("limits") or {}).items()
                       if value not in (None, "") and option_given(definition, key)}
        builtin = info.get("builtin_defaults") if isinstance(info.get("builtin_defaults"), dict) else None
        self.limits = LimitsFields(limit_checks(self.mcp), builtin, current, default_text=tr("default"))
        self.limits.changed.connect(self.update_state)
        form.addRow(tr("Limits"), self.limits)
        below = info.get("below_recommended") if isinstance(info.get("below_recommended"), dict) else {}
        if below and own:
            form.addRow(plain_label(tr("Below the limits its catalog recommends: {limits}.", limits=printable(", ".join(
                f"{key} {value}" for key, value in below.items()))), role="notice-warn"))
            recommended = QPushButton(tr("Use the recommended limits"))
            recommended.setToolTip(tr("Fill in what its catalog entry recommends (sandbox-ai mcp set NAME --recommended)"))
            recommended.clicked.connect(lambda _=False: self.limits.set_values(
                {**self.limits.values(), **{key: str(value) for key, value in below.items()}}))
            form.addRow("", recommended)
            self.recommended_button = recommended
        problem = info.get("last_problem") if isinstance(info.get("last_problem"), dict) else None
        if problem:
            form.addRow(tr("Last problem"), plain_label(printable(
                f"{problem.get('time', '')}: {problem.get('what', '')}. {problem.get('why', '')}. "
                + tr("Fix: {fix}", fix=problem.get("fix", ""))), role="notice-error", selectable=True))
        self.network = QComboBox()
        self.network.addItems(["none", "full"])
        self.network.setCurrentText(str(definition.get("network", "none")))
        form.addRow(tr("Network"), self.network)
        from .name_fields import NameField
        self.vpn = NameField("vpn", sai, host, placeholder=tr("a VPN profile (sandbox-ai vpn list); needs the full "
                                                              "network"))
        self.vpn.setAccessibleName(tr("VPN"))
        self.vpn.setText(str(definition.get("vpn") or ""))
        form.addRow(tr("VPN"), self.vpn)
        self.mounts, self.mount_reasons = {}, {}
        given = {m.get("target"): m for m in definition.get("mounts", []) if isinstance(m, dict)}
        targets = [m.get("target") for m in (entry or {}).get("mounts", []) if isinstance(m, dict)] or list(given)
        required = {m.get("target") for m in (entry or {}).get("mounts", []) if isinstance(m, dict) and m.get("required")}
        for target in targets:
            edit = QLineEdit(str((given.get(target) or {}).get("host", "")))
            edit.setPlaceholderText(tr("a folder on this machine") if host == "local" else
                                    tr("a folder on {host}", host=printable(host)))
            edit.setAccessibleName(tr("Folder for {target}", target=target))
            writable = QCheckBox(tr("writable"))
            writable.setChecked((given.get(target) or {}).get("mode") == "rw")
            reason = plain_label(role="notice-error")
            reason.setVisible(False)
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(writable)
            holder = QWidget()
            holder.setLayout(row)
            label = tr("{target} in the container", target=target) + (f" ({tr('required')})" if target in required
                                                                      else "")
            form.addRow(label, holder)
            form.addRow("", reason)
            edit.textChanged.connect(lambda _t, t=target: self.check_mount(t))
            writable.toggled.connect(lambda _on, t=target: self.check_mount(t))
            self.mounts[target], self.mount_reasons[target] = (edit, writable), reason
        if entry is None and kind != "command":
            form.addRow(plain_label(tr("Load the catalogs to set the folders its catalog entry takes besides these."),
                                    role="secondary"))
        self.env = QLineEdit(" ".join(map(str, definition.get("env", []))))
        self.env.setPlaceholderText(tr("variables of your environment, separated by spaces"))
        form.addRow(tr("Variables"), self.env)
        if kind == "command":
            for widget in (self.network, self.vpn, self.env):
                widget.setEnabled(False)
            form.addRow(plain_label(tr("A server that runs a command: its network, folders and VPN are its options in "
                                       "mcp.json; here only its limits."), role="secondary"))
        self.stop = QCheckBox(tr("Stop it now: its next start uses the change (agents that use it lose it until "
                                 "then)"))
        running = info.get("status") == "running"
        if not running:  # (shown only once it is in the form: a widget shown without a parent is a window of its own)
            self.stop.setVisible(False)
        else:
            form.addRow(plain_label(tr("It runs: the change holds from its next start, and a start refuses it until it "
                                       "was stopped."), role="notice-warn"))
            form.addRow(self.stop)
        self.error = plain_label(role="notice-error", selectable=True)
        self.error.setVisible(False)
        form.addRow(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.try_accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        if not own:
            for widget in (self.limits, self.network, self.vpn, self.env, *[w for pair in self.mounts.values()
                                                                             for w in pair]):
                widget.setEnabled(False)
            self.show_error(tr("{name} is not a server of yours (built in, a team's board or a project's): it is not "
                               "changed here.", name=printable(name)))
        self.update_state()

    def check_mount(self, target):
        edit, writable = self.mounts[target]
        reason = mount_reason(self.mcp, target, edit.text().strip(), "rw" if writable.isChecked() else "ro") \
            if self.host == "local" and edit.text().strip() else None
        self.mount_reasons[target].setText(reason or "")
        self.mount_reasons[target].setVisible(bool(reason))
        mark(edit, reason)
        self.update_state()

    def problems(self):
        found = list(self.limits.problems().values())
        found += [reason.text() for reason in self.mount_reasons.values() if reason.text()]
        return found

    def update_state(self):
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok) if hasattr(self, "buttons") else None
        if ok is not None:
            ok.setEnabled(self.info.get("own", True) and not self.problems())

    def show_error(self, text):
        self.error.setText(str(text or ""))
        self.error.setVisible(bool(text))

    def changes(self):
        """The keywords of backend.mcp_set_args for what differs from the definition (ValueError: not valid)."""
        if self.problems():
            raise ValueError("; ".join(self.problems()))
        definition = self.info.get("definition") if isinstance(self.info.get("definition"), dict) else {}
        kind = self.info.get("kind", "image")
        values = self.limits.values()
        changes = {}
        for key in ("memory", "cpus", "pids", "tmp"):
            before = definition.get(key) if kind != "command" else \
                ((self.info.get("limits") or {}).get(key) if option_given(definition, key) else None)
            now = values.get(key)
            if str(now if now is not None else "") != str(before if before is not None else ""):
                changes[key] = now if now is not None else "default"
        if kind != "command":
            if self.network.currentText() != definition.get("network", "none"):
                changes["network"] = self.network.currentText()
            vpn = self.vpn.text().strip()
            if vpn != (definition.get("vpn") or ""):
                changes["vpn"] = vpn or None
            mounts = [(target, edit.text().strip(), "rw" if writable.isChecked() else "ro")
                      for target, (edit, writable) in self.mounts.items() if edit.text().strip()]
            before = [(m.get("target"), m.get("host"), m.get("mode", "ro")) for m in definition.get("mounts", [])
                      if isinstance(m, dict)]
            if sorted(mounts) != sorted(before):
                changes["mounts"] = mounts
            env = self.env.text().split()
            if env != list(definition.get("env", [])):
                changes["env"] = env
        return changes

    def arguments(self):
        """The words after `sandbox-ai` of the `mcp set` this dialog asks for (None: nothing changed)."""
        changes = self.changes()
        if not changes:
            return None
        return backend.mcp_set_args(self.info.get("name"), stop=self.stop.isChecked() and self.stop.isVisible(),
                                    **changes)

    def try_accept(self):
        """OK: nothing that is not valid is taken (the dialog stays open with the reason); a change runs `mcp set`
        with the dialog open, and only its success closes it."""
        try:
            arguments = self.arguments()
        except ValueError as exc:
            self.show_error(printable(str(exc)))
            self.limits.focus_first_problem()
            return
        if arguments is None:
            self.accept()
            return
        if self.submit is None:
            self.accepted_arguments = arguments
            self.accept()
            return
        self.show_error("")
        self.buttons.setEnabled(False)
        self.submitting = True
        self.submit(arguments, self.submitted)

    def reject(self):
        """Cancel, Esc, the [x] - not while sandbox-ai takes the change: its answer (a refusal to correct) comes here,
        and the page lists the servers again only after it."""
        if getattr(self, "submitting", False):
            return
        super().reject()

    def submitted(self, status, out, err):
        self.submitting = False
        self.buttons.setEnabled(True)
        if status != 0:
            self.show_error((printable((err or out).strip()) or tr("exit status {status}", status=status)))
            self.update_state()
            return
        self.result_text = printable((out or err).strip())
        self.accept()


def option_given(definition, key):
    """Whether a command server's options set limit KEY themselves (--memory, --cpus, --pids)."""
    option = {"tmp": "--tmp-size"}.get(key, f"--{key}")
    return any(token == option or str(token).startswith(option + "=") for token in definition.get("options", []))


def run_submit(sai, host, owner=None):
    """SUBMIT for ServerPropertiesDialog: `sandbox-ai ARGUMENTS` on HOST in the background (sai.host_run)."""
    def submit(arguments, done):
        background(lambda: sai.host_run(host, *arguments, timeout=300), owner=owner, work=True, timeout=330,
                   done=lambda result, error: done(*(result if error is None else (1, "", str(error)))))
    return submit
