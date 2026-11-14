"""Models on the server…: the dialog of the LLM servers page that shows what one server has loaded now and loads or
unloads models there (llm_loaded.py has the arguments and the checks; sandbox-ai's `llm loaded|load|unload` does the
work, on this machine or on a host of sandbox-ai hosts as `sandbox-ai hosts run HOST -- llm ...`, and through the
server's VPN where it has one).

The table: model, instance, context, memory (as the server reports it), how it stays loaded (JIT with an idle TTL,
explicit, Ollama's keep_alive, ...), when it expires and the running sandboxes that use it. Load… chooses a model of
`llm models NAME` and its context (by default num_ctx of its parameters, `llm show NAME --json`; Ollama also a time);
the form is checked before it closes and stays open with a refusal of sandbox-ai's. Unload… asks first, naming the
running sandboxes that use the model (then --force). Keep loaded loads a JIT (or timed) model explicitly: LM Studio
replaces the JIT instance, unloaded first, so the model is never in memory twice; asked first while a running sandbox
uses it. Refresh reads again. Every command runs in the background (never on the GUI thread); a host whose sandbox-ai
has no `llm loaded` yet is said."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout,
)

from .files import printable, printable_text
from .host_choice import on_host
from .i18n import tr
from . import llm_loaded as LL
from . import wizard_llm as WL
from .model_combo import searchable
from .widgets import (
    ask_first, background, fill, outcome, plain_label, row_key, safe_tooltip, set_enabled, table,
)


def failure(status, out, err):
    text = "\n".join(printable(line) for line in ((err or out) or "").strip()[-1500:].splitlines())
    return text or tr("exit status {status}", status=status)


class LoadDialog(QDialog):
    """Load…: a model of MODELS (wizard_llm.parse_models) and the context it is loaded with (empty: num_ctx of its
    parameters, DEFAULTS(model), else the server's own), for Ollama the seconds it stays when idle. OK only for valid
    values; ON_SAVE(argv) loads and calls finish() or failed()."""

    def __init__(self, name, kind, models, defaults, host_label="", parent=None):
        super().__init__(parent)
        self.name, self.kind, self.defaults, self.saved, self.on_save = name, kind, defaults, False, None
        self.setWindowTitle(tr("Load a model on {name}", name=printable(name)))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("The model is loaded explicitly: it stays until it is unloaded, and LM "
                                        "Studio's Auto-Evict leaves it alone."), role="secondary"))
        if host_label:
            layout.addWidget(plain_label(tr("on {host}", host=host_label), role="secondary"))
        form = QFormLayout()
        self.model = searchable(QComboBox())
        self.model.setEditable(True)
        self.model.setAccessibleName(tr("Model"))
        self.model.setToolTip(tr("The model to load (sandbox-ai llm models lists them)"))
        for entry in models:
            label = entry["name"] + (f"  ({tr('loaded')})" if entry.get("loaded") else "")
            self.model.addItem(printable(label), entry["name"])
        self.model.setCurrentIndex(-1)
        self.model.setEditText("")
        self.model.currentIndexChanged.connect(lambda *_: self.model_chosen())
        self.model.editTextChanged.connect(lambda *_: self.validate())
        form.addRow(tr("Model"), self.model)
        self.context = QLineEdit()
        self.context.setAccessibleName(tr("Context"))
        self.context.setToolTip(tr("Tokens the model is loaded with; empty: num_ctx of its parameters (Parameters…), "
                                   "else the server's own"))
        self.context.textChanged.connect(lambda *_: self.validate())
        self.context.setEnabled(kind in LL.CONTEXT_KINDS)
        form.addRow(tr("Context"), self.context)
        self.ttl = QLineEdit()
        self.ttl.setAccessibleName(tr("Unload after idle (seconds)"))
        self.ttl.setPlaceholderText(tr("never (until unloaded)"))
        self.ttl.setToolTip(tr("Ollama unloads the model after this many seconds without a request; empty: it stays "
                               "until it is unloaded"))
        self.ttl.textChanged.connect(lambda *_: self.validate())
        if kind in LL.TTL_KINDS:
            form.addRow(tr("Unload after idle (seconds)"), self.ttl)
        else:
            self.ttl.hide()
        layout.addLayout(form)
        self.message = plain_label(role="secondary", selectable=True, log="llm")
        self.message.setVisible(False)
        layout.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Load"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.validate()

    def chosen(self):
        index = self.model.currentIndex()
        typed = self.model.currentText().strip()
        if index >= 0 and self.model.itemText(index) == typed:
            return self.model.itemData(index)
        found = self.model.findData(typed)
        return typed if found >= 0 or WL.valid_model(typed) else None

    def model_chosen(self):
        model = self.chosen()
        default = self.defaults(model) if model else None
        self.context.setPlaceholderText(str(default) if default else tr("the server's own"))
        self.validate()

    def problems(self):
        found = []
        if WL.valid_model(self.chosen()) is None:
            found.append(tr("Choose a model first."))
        for check, field in ((LL.check_context, self.context), (LL.check_ttl, self.ttl)):
            try:
                check(field.text())
            except ValueError as exc:
                found.append(str(exc))
        return found

    def validate(self):
        problems = self.problems()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not problems)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setToolTip(safe_tooltip("\n".join(problems)))
        return not problems

    def request(self):
        """The argv of `llm load` for what the form holds; ValueError says what is wrong."""
        problems = self.problems()
        if problems:
            raise ValueError("\n".join(problems))
        model = self.chosen()
        # (llama.cpp's router loads with its own arguments: no context there, not even the default)
        context = LL.check_context(self.context.text()) or self.defaults(model) if self.kind in LL.CONTEXT_KINDS \
            else None
        ttl = LL.check_ttl(self.ttl.text()) if self.kind in LL.TTL_KINDS else None
        return LL.load_argv(self.name, model, context, ttl)

    def accept(self):
        try:
            argv = self.request()
        except ValueError as exc:
            self.failed(printable_text(exc))
            return
        if self.on_save is None:
            self.finish()
            return
        self.buttons.setEnabled(False)
        self.message.setText(tr("Loading {model}…", model=argv[3]))
        self.message.setVisible(True)
        self.on_save(argv)

    def failed(self, text):
        self.buttons.setEnabled(True)
        self.message.setText(printable(text))
        self.message.setVisible(True)
        self.validate()

    def finish(self):
        self.saved = True
        super().accept()


class LoadedDialog(QDialog):
    """Models on the server…: what server NAME (of KIND) has loaded, read with RUN(*argv, timeout=...) - the page's
    `sandbox-ai [hosts run HOST --] ...` - in the background, and Load…, Unload…, Keep loaded, Refresh."""

    def __init__(self, run, name, kind, host_label="", host="local", parent=None):
        super().__init__(parent)
        self.run, self.name, self.kind, self.host_label, self.host = run, name, kind, host_label, host
        self.entry, self.models, self.view, self.asked, self.load_dialog = None, [], {}, 0, None
        self.setWindowTitle(tr("Models on the server {name}", name=printable(name)))
        self.resize(900, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr(
            "What {name} has loaded now, as its API reports it, and the running sandboxes that use each model. A JIT "
            "model (LM Studio) may be unloaded when another sandbox's model is loaded: Keep loaded loads it "
            "explicitly.", name=printable(name)), role="secondary"))
        if host_label:
            layout.addWidget(plain_label(tr("on {host}", host=host_label), role="secondary"))
        self.table = table([tr(title) for title in LL.COLUMNS], stretch=0, compact=range(2, len(LL.COLUMNS)))
        self.table.setAccessibleName(tr("Loaded models"))
        self.table.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.table, 1)
        self.note = plain_label(role="secondary", selectable=True)
        self.note.setAccessibleName(tr("What the server may unload"))
        self.note.setVisible(False)
        layout.addWidget(self.note)
        row = QHBoxLayout()
        self.load_button = QPushButton(tr("Load…"))
        self.unload_button = QPushButton(tr("Unload…"))
        self.keep_button = QPushButton(tr("Keep loaded"))
        self.refresh_button = QPushButton(tr("Refresh"))
        for button, label, tip in (
                (self.load_button, tr("Load a model"), tr("Load a model explicitly (sandbox-ai llm load): it stays "
                                                          "until it is unloaded")),
                (self.unload_button, tr("Unload the model"), tr("Unload the selected model or instance (sandbox-ai "
                                                                "llm unload); asked first")),
                (self.keep_button, tr("Keep loaded"), tr("Load the selected JIT model explicitly, so that the server "
                                                         "does not unload it for another model (the JIT instance "
                                                         "goes first: never twice in memory)")),
                (self.refresh_button, tr("Refresh"), tr("Ask the server again"))):
            button.setAccessibleName(label)
            button.setToolTip(safe_tooltip(tip))
            row.addWidget(button)
        row.addStretch(1)
        from .widgets import list_menu
        list_menu(self.table, lambda: [self.unload_button, self.keep_button, None, self.load_button,
                                       self.refresh_button])
        self.load_button.clicked.connect(lambda _=False: self.load())
        self.unload_button.clicked.connect(lambda _=False: self.unload())
        self.keep_button.clicked.connect(lambda _=False: self.keep())
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        layout.addLayout(row)
        self.status = plain_label(role="secondary", selectable=True, log="llm")
        self.status.setAccessibleName(tr("Status"))
        layout.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.update_buttons()
        self.refresh()
        self.read_choices()

    # -- reading --

    def refresh(self):
        self.asked += 1
        ticket = self.asked
        self.status.setText(tr("Asking {name}…", name=self.name))
        background(lambda: self.run(*LL.loaded_argv(self.name), timeout=LL.LOADED_SECONDS), owner=self,
                   source=self.refresh_button, timeout=LL.LOADED_SECONDS + 15,
                   done=lambda result, error: self.listed(ticket, result, error))

    def listed(self, ticket, result, error):
        if ticket != self.asked:
            return  # asked again meanwhile: only the newest answer counts
        status, out, err = outcome(result, error)
        if status != 0:
            self.status.setText(tr("The sandbox-ai of {host} has no llm loaded yet: update it there (Hosts › "
                                   "Update).", host=self.host_label) if LL.too_old(status, err) else
                                tr("Could not read what {name} has loaded: {error}", name=self.name,
                                   error=failure(status, out, err)))
            return
        try:
            self.entry = LL.parse_loaded(out)
        except ValueError as exc:
            self.status.setText(tr("Could not read what {name} has loaded: {error}", name=self.name,
                                   error=printable_text(exc)))
            return
        if self.kind is None and self.entry["kind"]:
            self.kind = self.entry["kind"]  # (the page's list did not say it)
        found = LL.rows(self.entry)
        fill(self.table, [cells for cells, _, _ in found], tooltips=[tip for _, _, tip in found],
             sort=[sorts for _, sorts, _ in found], icons=["warn" if item.get("kept") == "jit" else None
                                                            for item in self.entry["instances"] or []],
             keys=[item["instance"] for item in self.entry["instances"] or []])
        jit = LL.jit_instances(self.entry)
        self.note.setText(tr("LM Studio may unload {models} when another sandbox's model is loaded: Auto-Evict; see "
                             "Help > Keeping models loaded. Keep loaded loads it explicitly.",
                             models=", ".join(item["model"] for item in jit)) if jit else "")
        self.note.setVisible(bool(jit))
        if self.entry["instances"] is None:
            self.status.setText(tr("{name} does not report what it has loaded.", name=self.name))
        else:
            self.status.setText(tr("Loaded on {name}: {count}", name=self.name, count=len(self.entry["instances"])))
        self.update_buttons()

    def read_choices(self):
        """The models Load… offers (`llm models NAME --json`) and the parameters its context comes from (`llm show
        NAME --json`), in the background."""
        def work():
            models, view = [], {}
            status, out, err = self.run(*WL.models_argv(self.name), timeout=LL.LOADED_SECONDS)
            if status == 0:
                try:
                    models = WL.parse_models(out)
                except ValueError:
                    pass
            status, out, err = self.run("llm", "show", self.name, "--json", timeout=LL.LOADED_SECONDS)
            if status == 0:
                try:
                    view = WL.parse_show(out)
                except ValueError:
                    pass
            return models, view

        def done(result, error):
            if error is None:
                self.models, self.view = result
            self.update_buttons()
        background(work, owner=self, source=self.load_button, timeout=2 * LL.LOADED_SECONDS + 15, done=done)

    # -- what is selected --

    def selected(self):
        rows = [index.row() for index in self.table.selectionModel().selectedRows()] \
            if self.table.selectionModel() else []
        key = row_key(self.table, rows[0]) if rows else None
        return next((item for item in (self.entry or {}).get("instances") or [] if item["instance"] == key), None)

    def users(self, item):
        return (self.entry or {}).get("used_by", {}).get(item["model"], []) if item else []

    def update_buttons(self):
        entry, item = self.entry or {}, self.selected()
        can_load = self.kind in LL.KINDS_LOADING and (entry.get("load") if entry else True)
        set_enabled(self.load_button, bool(can_load and self.models),
                    tr("This server's API loads no model.") if not can_load else tr("Reading its models…"))
        set_enabled(self.unload_button, bool(entry.get("unload") and item),
                    tr("This server's API unloads no model.") if entry and not entry.get("unload") else
                    tr("Select a loaded model first."))
        keepable = bool(item and entry.get("load") and item.get("kept") in ("jit", "timed"))
        set_enabled(self.keep_button, keepable,
                    tr("Select a loaded model first.") if not item else
                    tr("It is loaded explicitly already.") if item.get("kept") == "explicit" else
                    tr("This server's API loads no model."))

    # -- load, unload, keep --

    def load(self):
        dialog = LoadDialog(self.name, self.kind, self.models, lambda model: LL.default_context(self.view, model),
                            self.host_label, self)
        self.load_dialog = dialog
        dialog.on_save = lambda argv: self.do_load(argv, dialog)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and not dialog.saved:
                dialog.accept()  # an exec() that stood in for the human (the tests): OK as they would press it
        finally:
            self.load_dialog = None
            dialog.deleteLater()

    def do_load(self, argv, dialog=None):
        model = argv[3]
        users = (self.entry or {}).get("used_by", {}).get(model, [])
        loaded = [item for item in (self.entry or {}).get("instances") or [] if item["model"] == model]
        context = int(argv[argv.index("--context") + 1]) if "--context" in argv else None
        # What sandbox-ai refuses without --force: LM Studio's JIT instance replaced, Ollama's reloaded with another
        # context.
        replaces = any(item.get("kept") == "jit" for item in loaded) if self.kind == "lmstudio" else \
            any(context and item.get("context") and item["context"] != context for item in loaded)
        if users and replaces:
            if not ask_first(self, "llm-unload", tr("Load"), tr(
                    "{model} is loaded and the running sandboxes {names} use it: loading it again takes it from them "
                    "for a moment (their next request waits for it or fails). Load it again?", model=model,
                    names=", ".join(users)), tr("Load again")):
                if dialog is not None:
                    dialog.failed(tr("Not loaded."))
                return
            argv = argv + ["--force"]
        self.status.setText(tr("Loading {model}…", model=model))
        background(lambda: self.run(*argv, timeout=LL.LOAD_SECONDS), owner=self, work=True,
                   source=[self.load_button, self.keep_button], timeout=LL.LOAD_SECONDS + 15,
                   done=lambda result, error: self.loaded(model, dialog, result, error))

    def loaded(self, model, dialog, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            text = tr("sandbox-ai llm load refused it: {error}", error=failure(status, out, err))
            self.status.setText(text)
            if dialog is not None and self.load_dialog is dialog:
                dialog.failed(text)
            return
        self.status.setText("\n".join(printable(line) for line in out.strip().splitlines()[-3:]) or
                            tr("{model} is loaded.", model=model))
        if dialog is not None and self.load_dialog is dialog:
            dialog.finish()
        self.refresh()

    def keep(self):
        """Keep loaded: the selected JIT (or timed) model loaded explicitly, with the context it has."""
        item = self.selected()
        if item is None or item.get("kept") not in ("jit", "timed"):
            return
        try:
            argv = LL.load_argv(self.name, item["model"], item.get("context") if self.kind in LL.CONTEXT_KINDS
                                else None)
        except ValueError as exc:
            self.status.setText(printable_text(exc))
            return
        self.do_load(argv)

    def unload(self):
        item = self.selected()
        if item is None:
            return
        users = self.users(item)
        target = item["model"] if item["instance"] == item["model"] else item["instance"]
        text = tr("Unload {model} from {name}? It leaves the server's memory; a later request loads it again.",
                  model=target, name=self.name)
        if users:
            text += "\n\n" + tr("The running sandboxes {names} use it: their next request waits for it to load again "
                                "(or fails).", names=", ".join(users))
        if not ask_first(self, "llm-unload", tr("Unload"), on_host(self.host, text), tr("Unload")):
            self.status.setText(tr("Not unloaded."))
            return
        try:
            args = LL.unload_args(self.name, target, force=bool(users))
        except ValueError as exc:
            self.status.setText(printable_text(exc))
            return
        self.status.setText(tr("Unloading {model}…", model=target))
        background(lambda: self.run("llm", "unload", *args, timeout=LL.UNLOAD_SECONDS), owner=self, work=True,
                   source=self.unload_button, timeout=LL.UNLOAD_SECONDS + 15,
                   done=lambda result, error: self.unloaded(target, result, error))

    def unloaded(self, target, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            self.status.setText(tr("Could not unload {model}: {error}", model=target, error=failure(status, out, err)))
            return
        self.status.setText(tr("{model} is unloaded.", model=target))
        self.refresh()
