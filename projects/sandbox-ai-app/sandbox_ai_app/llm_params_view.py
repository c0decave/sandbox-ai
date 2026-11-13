"""The form of an LLM server's sampling parameters (llm_params.py has the checks and the arguments).

ParamsFields is the form (one row per parameter: a value, empty for not set, and Force), used by the LLM servers
page's Parameters… dialog (ParamsDialog: a scope - every model or one model -, the loop guard and whether the server
may unload the other models) and by the board's LLM server node (this board's values, passed as --llm-param KEY=VALUE:
forced for that start). LoopFields is the loop guard's layers (empty: what holds without them, shown in the field),
in the dialog (the server's own), on the board (this board's) and in the Settings (yours, for every server)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .files import printable
from .i18n import tr
from .llm_params import (
    EVICT, EVICT_NOTES, LAYER_HELP, MODEL, SCOPE_ALL, check_layer, check_value, layer_text, loop_changes, scope_of,
    set_argv, value_text,
)
from .mcp_fields import mark
from .widgets import plain_label, safe_tooltip


class ParamsFields(QWidget):
    """One row per parameter of CATALOGUE: its value (empty: not set; DEFAULTS, {key: value}, shown in the empty
    field) and, with FORCE, a Force box; a value typed in is checked at once (the rule next to it, the field marked).
    changed: the text changed; committed: a field was left or reset."""
    changed = Signal()
    committed = Signal()

    def __init__(self, catalogue, values=None, forced=None, force=True, defaults=None, default_text=None,
                 parent=None):
        super().__init__(parent)
        self.catalogue = list(catalogue or [])
        self.entries = {entry["name"]: entry for entry in self.catalogue}
        self.default_text = default_text
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        self.fields, self.forces, self.reasons = {}, {}, {}
        values, forced = dict(values or {}), set(forced or ())
        rows = [entry["name"] for entry in self.catalogue]
        for row, key in enumerate(rows):
            entry = self.entries[key]
            if entry["type"] == "bool":
                field = QComboBox()
                field.addItem(tr("not set"), "")
                for word in ("true", "false"):  # the values themselves, in every language
                    field.addItem(word, word)
                current = values.get(key)
                field.setCurrentIndex(field.findData(value_text(current)) if current is not None and
                                      not isinstance(current, str) else max(field.findData(str(current or "")), 0))
                field.currentIndexChanged.connect(lambda _i, k=key: self.check(k))
                field.currentIndexChanged.connect(lambda _i: self.committed.emit() if self.valid() else None)
            else:
                current = values.get(key)
                field = QLineEdit("" if current is None else current if isinstance(current, str) else
                                  value_text(current))
                field.textChanged.connect(lambda _text, k=key: self.check(k))
                field.editingFinished.connect(self.commit)
            field.setAccessibleName(key)
            caption = QLabel(key)
            caption.setBuddy(field)
            tip = entry["help"] + ("\n" + tr("A ceiling: the agent's own lower value stays.") if entry["ceiling"]
                                   else "") + ("\n" + tr("Never sent: the agents' configuration gets it (the "
                                                          "context window).") if entry["never_sent"] else "")
            rule = tr("true or false") if entry["type"] == "bool" else \
                tr("from {low} to {high}", low=entry["min"], high=entry["max"])
            field.setToolTip(safe_tooltip(f"{tip}\n{rule}"))
            caption.setToolTip(safe_tooltip(f"{tip}\n{rule}"))
            reason = plain_label(role="notice-error")
            reason.setVisible(False)
            grid.addWidget(caption, row * 2, 0)
            grid.addWidget(field, row * 2, 1)
            if force:
                box = QCheckBox(tr("Force"))
                box.setToolTip(tr("In place of the agent's own value too (without: set only where the agent's request "
                                  "has none)"))
                box.setChecked(key in forced)
                box.toggled.connect(lambda _on: self.committed.emit() if self.valid() else None)
                grid.addWidget(box, row * 2, 2)
                self.forces[key] = box
            grid.addWidget(reason, row * 2 + 1, 1, 1, 2)
            self.fields[key], self.reasons[key] = field, reason
        self.reset_button = QPushButton(tr("Reset to defaults"))
        self.reset_button.setToolTip(tr("Empty every field: nothing set, the agent's and the server's own values hold"))
        self.reset_button.clicked.connect(self.reset)
        grid.addWidget(self.reset_button, len(rows) * 2, 1)
        self.set_defaults(defaults)
        for key in self.fields:
            self.check(key)

    def text(self, key):
        field = self.fields[key]
        return (field.currentData() or "") if isinstance(field, QComboBox) else field.text().strip()

    def set_defaults(self, defaults):
        """DEFAULTS ({key: value}: what holds when the field is empty, e.g. every model's) shown in the fields."""
        self.defaults = dict(defaults or {})
        for key, field in self.fields.items():
            if isinstance(field, QLineEdit):
                value = self.defaults.get(key)
                field.setPlaceholderText(tr("default: {value}", value=printable(value_text(value)))
                                         if value not in (None, "") else self.default_text or tr("not set"))

    def reason(self, key):
        """Why the value of field KEY is not valid (None: it is, or it is empty)."""
        text = self.text(key)
        if not text:
            return None
        try:
            check_value(self.entries[key], text)
        except ValueError as exc:
            return tr("Not valid: {rule}", rule=str(exc))
        return None

    def check(self, key):
        reason = self.reason(key)
        self.reasons[key].setText(reason or "")
        self.reasons[key].setVisible(bool(reason))
        mark(self.fields[key], reason)
        self.changed.emit()

    def valid(self):
        return all(self.reason(key) is None for key in self.fields)

    def problems(self):
        """[(key, why)] of the fields that are not valid."""
        return [(key, self.reason(key)) for key in self.fields if self.reason(key)]

    def values(self):
        """{key: value} of the fields set and valid."""
        found = {}
        for key in self.fields:
            text = self.text(key)
            if not text or self.reason(key):
                continue
            found[key] = check_value(self.entries[key], text)
        return found

    def texts(self):
        """{key: text} of the fields set (as typed)."""
        return {key: self.text(key) for key in self.fields if self.text(key)}

    def forced(self):
        return {key for key, box in self.forces.items() if box.isChecked()}

    def commit(self):
        if self.valid():
            self.committed.emit()

    def reset(self):
        for key, field in self.fields.items():
            if isinstance(field, QComboBox):
                field.setCurrentIndex(0)
            else:
                field.clear()
            if key in self.forces:
                self.forces[key].setChecked(False)
        self.committed.emit()


class LoopFields(QWidget):
    """The loop guard's layers of SPEC (llm_params.loop_spec: their defaults and ranges): a switch is "on", "off" or
    empty, a count a number (0: off) or empty; empty leaves INHERITED's value ({layer: value}: what holds without
    this one, shown in the field). VALUES: the layers set here. changed / committed as ParamsFields'."""
    changed = Signal()
    committed = Signal()

    def __init__(self, spec, values=None, inherited=None, parent=None):
        super().__init__(parent)
        self.spec = spec
        defaults, _ranges = spec
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        self.fields, self.reasons = {}, {}
        values = dict(values or {})
        for row, key in enumerate(defaults):
            if isinstance(defaults[key], bool):
                field = QComboBox()
                field.addItem("", "")
                for word in ("on", "off"):  # the values themselves, in every language
                    field.addItem(word, word)
                current = values.get(key)
                field.setCurrentIndex(max(field.findData(layer_text(current) if isinstance(current, bool) else
                                                         str(current or "")), 0))
                field.currentIndexChanged.connect(lambda _i, k=key: self.check(k))
                field.currentIndexChanged.connect(lambda _i: self.commit())
            else:
                current = values.get(key)
                field = QLineEdit("" if current is None else str(current))
                field.textChanged.connect(lambda _text, k=key: self.check(k))
                field.editingFinished.connect(self.commit)
            field.setAccessibleName(key)
            help_text = LAYER_HELP.get(key)
            if help_text:
                field.setToolTip(safe_tooltip(tr(help_text)))
            caption = QLabel(key)
            caption.setBuddy(field)
            if help_text:
                caption.setToolTip(safe_tooltip(tr(help_text)))
            reason = plain_label(role="notice-error")
            reason.setVisible(False)
            grid.addWidget(caption, row * 2, 0)
            grid.addWidget(field, row * 2, 1)
            grid.addWidget(reason, row * 2 + 1, 1)
            self.fields[key], self.reasons[key] = field, reason
        self.reset_button = QPushButton(tr("Reset to defaults"))
        self.reset_button.setToolTip(tr("Empty every layer: the values they inherit hold"))
        self.reset_button.clicked.connect(self.reset)
        grid.addWidget(self.reset_button, len(defaults) * 2, 1)
        self.set_inherited(inherited)
        for key in self.fields:
            self.check(key)

    def set_inherited(self, inherited):
        """INHERITED ({layer: value}, else sandbox-ai's own defaults) shown in the empty fields."""
        inherited = {**self.spec[0], **(inherited or {})}
        for key, field in self.fields.items():
            shown = tr("default: {value}", value=printable(layer_text(inherited.get(key))))
            if isinstance(field, QComboBox):
                field.setItemText(0, shown)
            else:
                field.setPlaceholderText(shown)

    def text(self, key):
        field = self.fields[key]
        return (field.currentData() or "") if isinstance(field, QComboBox) else field.text().strip()

    def reason(self, key):
        text = self.text(key)
        if not text:
            return None
        try:
            check_layer(key, text, self.spec)
        except ValueError as exc:
            return tr("Not valid: {rule}", rule=str(exc))
        return None

    def check(self, key):
        reason = self.reason(key)
        self.reasons[key].setText(reason or "")
        self.reasons[key].setVisible(bool(reason))
        mark(self.fields[key], reason)
        self.changed.emit()

    def valid(self):
        return all(self.reason(key) is None for key in self.fields)

    def problems(self):
        return [(key, self.reason(key)) for key in self.fields if self.reason(key)]

    def values(self):
        """{layer: value} of the layers set and valid."""
        return {key: check_layer(key, self.text(key), self.spec) for key in self.fields
                if self.text(key) and not self.reason(key)}

    def texts(self):
        return {key: self.text(key) for key in self.fields if self.text(key)}

    def commit(self):
        if self.valid():
            self.committed.emit()

    def reset(self):
        for field in self.fields.values():
            if isinstance(field, QComboBox):
                field.setCurrentIndex(0)
            else:
                field.clear()
        self.committed.emit()


class ParamsDialog(QDialog):
    """The parameters of the LLM server VIEW (`llm show NAME --json`) for SCOPE (every model, or one of MODELS),
    its loop guard and whether the server may unload its other models: OK only once every field is valid; ON_SAVE
    (a list of `llm set` argument lists, one per scope changed) does the saving and calls finish() or failed()."""

    def __init__(self, view, catalogue, scope=SCOPE_ALL, models=(), host_label="", spec=None, parent=None):
        super().__init__(parent)
        self.view, self.catalogue, self.saved, self.on_save = view, list(catalogue), False, None
        self.spec = spec  # the loop guard's layers (llm_params.loop_spec); None: a sandbox-ai without them
        self.name = str(view.get("name") or "")
        self.setWindowTitle(tr("Parameters of {name}", name=printable(self.name)))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr(
            "Sampling parameters the connection proxy applies to this server's requests only, whatever agent sends "
            "them: set where the agent's request has none, forced ones in place of the agent's own; max_tokens is a "
            "ceiling, num_ctx the context window the agents are told."), role="secondary"))
        if host_label:
            layout.addWidget(plain_label(tr("on {host}", host=host_label), role="secondary"))
        form = QFormLayout()
        self.scope = QComboBox()
        self.scope.setAccessibleName(tr("Model"))
        self.scope.addItem(tr("every model"), SCOPE_ALL)
        params = view.get("params") if isinstance(view.get("params"), dict) else {}
        for model in dict.fromkeys([*models, *[k for k in params if k != SCOPE_ALL]]):
            if isinstance(model, str) and MODEL.fullmatch(model):
                self.scope.addItem(printable(model), model)
        self.scope.setCurrentIndex(max(self.scope.findData(scope), 0))
        self.scope.setToolTip(tr("Whose parameters: every model's, or one model's (over every model's)"))
        form.addRow(tr("Model"), self.scope)
        self.context = plain_label(role="secondary")
        form.addRow(tr("Context window"), self.context)
        layout.addLayout(form)
        self.holder = QVBoxLayout()
        layout.addLayout(self.holder)
        self.pending = {}  # scope: (texts, forced) edited and not saved yet
        self.fields, self.current = None, None
        other = QFormLayout()
        self.guard, self.own = None, {}
        if spec is not None:
            own = view.get("loop_guard_own") if isinstance(view.get("loop_guard_own"), dict) else {}
            self.own = {key: value for key, value in own.items() if key in spec[0]}
            applied = view.get("loop_guard") if isinstance(view.get("loop_guard"), dict) else {}
            # Empty: what holds without the server's own layers (yours and sandbox-ai's; the applied value where
            # the server sets none).
            inherited = {key: value for key, value in applied.items() if key not in self.own}
            self.guard = LoopFields(spec, self.own, inherited)
            self.guard.setAccessibleName(tr("Loop guard"))
            self.guard.setToolTip(tr("The server's own layers of the loop guard; empty: from your defaults (Settings) "
                                     "or sandbox-ai's"))
            self.guard.changed.connect(self.validate)
            other.addRow(tr("Loop guard"), self.guard)
        self.evict = QComboBox()
        self.evict.setAccessibleName(tr("Other models"))
        for key, label in EVICT:
            self.evict.addItem(tr(label), key)
        self.evict.setCurrentIndex(max(self.evict.findData(view.get("evict") or "never"), 0))
        other.addRow(tr("Other models"), self.evict)
        note = EVICT_NOTES.get(str(view.get("kind") or ""))
        if note:
            other.addRow("", plain_label(tr(note), role="secondary"))
        layout.addLayout(other)
        self.message = plain_label(role="notice-error", selectable=True)
        self.message.setVisible(False)
        layout.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.scope.currentIndexChanged.connect(lambda _i: self.show_scope())
        self.show_scope()

    def show_scope(self):
        """The fields of the scope chosen now (what was typed for another scope kept until OK or Cancel)."""
        if self.fields is not None:
            self.pending[self.current] = (self.fields.texts(), self.fields.forced())
            self.fields.setParent(None)
            self.fields.deleteLater()
        scope = self.scope.currentData() or SCOPE_ALL
        values, forced = self.pending.get(scope) or scope_of(self.view, scope)
        defaults = scope_of(self.view, SCOPE_ALL)[0] if scope != SCOPE_ALL else {}
        self.fields = ParamsFields(self.catalogue, values, forced, defaults=defaults,
                                   default_text=tr("not set"))
        self.fields.changed.connect(self.validate)
        self.holder.addWidget(self.fields)
        self.current = scope
        contexts = self.view.get("contexts") if isinstance(self.view.get("contexts"), dict) else {}
        reported = contexts.get(scope) if scope != SCOPE_ALL else None
        self.context.setText(tr("{tokens} tokens, as the server reports it", tokens=reported)
                             if isinstance(reported, int) and not isinstance(reported, bool) else
                             tr("as the server reports it per model (num_ctx sets it)"))
        self.validate()

    def problems(self):
        found = [f"{key}: {why}" for key, why in self.fields.problems()] if self.fields is not None else []
        for scope, (texts, _forced) in self.pending.items():
            if scope == self.current:
                continue
            for key, text in texts.items():
                entry = next((e for e in self.catalogue if e["name"] == key), None)
                try:
                    if entry is None:
                        raise ValueError(tr("not a parameter sandbox-ai knows"))
                    check_value(entry, text)
                except ValueError as exc:
                    found.append(f"{scope}: {key}: {exc}")
        if self.guard is not None:
            found += [f"{tr('Loop guard')}: {key}: {why}" for key, why in self.guard.problems()]
        return found

    def validate(self):
        problems = self.problems()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not problems)
        return not problems

    def requests(self):
        """The `llm set` argument lists of the scopes changed (the loop guard and Other models with the first, or
        alone when no scope changed); ValueError for a field that is not valid."""
        self.pending[self.current] = (self.fields.texts(), self.fields.forced())
        argvs = []
        for scope, (texts, forced) in self.pending.items():
            entries = {e["name"]: e for e in self.catalogue}
            values = {key: check_value(entries[key], text) for key, text in texts.items() if key in entries}
            stored, stored_forced = scope_of(self.view, scope)
            if values == stored and set(forced) & set(values) == stored_forced:
                continue
            argvs.append(set_argv(self.name, scope, values, set(forced) & set(values), catalogue=self.catalogue))
        evict = self.evict.currentData() or "never"
        extra = []
        for value in loop_changes(self.own, self.guard.values()) if self.guard is not None else []:
            extra += ["--loop-guard", value]
        if evict != (self.view.get("evict") or "never"):
            extra += ["--evict", evict]
        if extra:
            if argvs:
                argvs[0] += extra
            else:
                argvs.append(["llm", "set", self.name, *extra])
        return argvs

    def accept(self):
        if not self.validate():
            self.failed("\n".join(self.problems()))
            return
        try:
            argvs = self.requests()
        except ValueError as exc:
            self.failed(printable(exc))
            return
        if not argvs:
            self.finish()
            return
        if self.on_save is None:
            self.finish()
            return
        self.buttons.setEnabled(False)
        self.on_save(argvs)

    def failed(self, text):
        self.buttons.setEnabled(True)
        self.message.setText(printable(text))
        self.message.setVisible(True)
        self.validate()

    def finish(self):
        self.saved = True
        super().accept()
