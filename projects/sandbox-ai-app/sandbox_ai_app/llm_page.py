"""The LLM servers page (sandbox-ai llm): the endpoints kept on this machine or on a host of `sandbox-ai hosts`,
with Add, Edit, Check, Models and Remove, as the web interface's LLM page offers them.

Every command runs in the background (`sandbox-ai llm ...` here, `sandbox-ai hosts run HOST -- llm ...` for a
host), only while the page is shown or a button asks: no command at every start of the app.

An API key typed in is kept as a secret of sandbox-ai on the server's host (`sandbox-ai secrets set NAME`, over SSH for
another host, the value on standard input) after `llm add` accepted the server, and the endpoint only names that
secret (--api-key-secret NAME): the key is never on a command line, in a file of the app or in sandbox-ai's llm.json.

The dialog (new or Edit…, a double-click) checks the name as it is typed (the character that cannot be in it, a
name to use instead; OK only for a valid one) and stays open with everything typed whatever goes wrong: a field the
CLI would refuse, a question answered No, `llm add` refusing (its message shown in the dialog). Test connection
lists the models of a server not saved yet (`sandbox-ai wizard models --kind --url --json`: nothing is stored; a
secret named goes as --api-key-secret where that host knows it, and when sandbox-ai refuses to send it over plain http
to an address outside the local networks the dialog asks (llm-insecure, naming the host and the URL) and tests again
with --insecure-http, which the server is then saved with; an OpenAI-compatible server cannot be asked that way, so
`llm check` runs once it is saved; an edited server whose address and key are as saved: `llm models NAME`) and fills
the Default model choice. Edit prefills from
`llm show NAME --json` and saves with --replace, every setting kept - the CA it has stays by itself, and only a file
named in the dialog replaces it - after a yes naming what changes (llm-replace).

Models lists a server's models in a table (the columns the server has values for, loaded first, a filter) where one
becomes the server's default model (Use as default: `llm add --replace --model`, its other settings kept, asked
first: llm-replace) or is copied as NAME:MODEL for --llm.

What the CLI or a host answers is shown through printable(); names that go back as arguments are checked first
(never one that reads as an option). The dialog also offers the further options of `llm add`: headers the proxy adds
from secrets (--header-secret), a CA (--ca, a file of that host), plain http to a public address (--insecure-http,
asked first: llm-insecure) and replacing an endpoint of the same name (--replace); Show runs `llm show`."""
from __future__ import annotations

import functools
import json
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .files import printable
from .host_choice import follow_hosts, on_host
from . import backend, theme, widgets
from . import wizard_llm as WL
from .i18n import N_, tr
from .widgets import (
    ask_first, background, fill, flow, outcome, plain_label, row_key, safe_tooltip, selected_keys, set_enabled,
    show_text, table, warn,
)

# As sandbox-ai llm checks them (llm.py: NAME, SECRET, MODEL_NAME, VPN_PROFILE, URL).
NAME = WL.NAME
SECRET = WL.SECRET
MODEL = WL.MODEL
VPN = NAME
HEADER = WL.HEADER
RESERVED_HEADERS = WL.RESERVED_HEADERS
SORTS = (("loaded", N_("loaded first")), ("name", N_("by name")), ("size", N_("by size")))
HEADER_EXAMPLE = "X-Gateway-Key=GATEWAY_KEY"
URL = WL.URL
LOCAL = "local"  # this machine, as sandbox-ai hosts names it
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
KINDS = (("lmstudio", N_("LM Studio")), ("ollama", N_("Ollama")), ("llamacpp", N_("llama.cpp server")),
         ("vllm", N_("vLLM")), ("openai-compatible", N_("OpenAI-compatible server (a gateway, LiteLLM, …)")))
AGENTS = ("claude", "codex", "opencode")
NAME_EXAMPLE = WL.NAME_EXAMPLE
URL_EXAMPLE = "http://192.168.1.20:1234"
MODEL_EXAMPLE = "qwen2.5-coder:7b"
SECRET_EXAMPLE = "LLM_KEY"
VPN_EXAMPLE = "home"
LIST_SECONDS, ADD_SECONDS, CHECK_SECONDS, MODELS_SECONDS, REMOVE_SECONDS = 60, 90, 150, 120, 60
SHOW_SECONDS = 60
CONNECTION = ("kind", "url", "secret", "headers", "ca")  # what decides how the server is reached


def valid_name(name):
    """NAME if it may go back to sandbox-ai as an endpoint's name (never one that reads as an option), else None."""
    return WL.valid_name(name)


def valid_host(name):
    return name if name == "local" or (isinstance(name, str) and not name.startswith("-") and HOST.fullmatch(name)) \
        else None


def endpoints_of(out):
    """([endpoint dict], [line]) of `sandbox-ai llm list --json`: an endpoint the CLI found unusable is reported in a
    line of its own before the JSON. ValueError: no list in the answer."""
    text, notes = out or "", []
    try:
        data = json.loads(text)
    except ValueError:
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("[")), None)
        if start is None:
            raise ValueError(text.strip()[-500:] or "no answer") from None
        notes = [line for line in lines[:start] if line.strip()]
        try:
            data = json.loads("\n".join(lines[start:]))
        except ValueError:
            raise ValueError(text.strip()[-500:]) from None
    if not isinstance(data, list):
        raise ValueError("not a list of endpoints")
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("name"), str)], notes


def text_of(value, limit=300):
    """A value of the CLI or a host as one line for the screen."""
    if value is None or value is False:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    return printable(str(value))[:limit]


def lines_of(text, limit=6000):
    """Output of the CLI or a host for a text view: each line printable, the end kept."""
    return "\n".join(printable(line) for line in str(text or "").strip()[-limit:].splitlines())


def failure(result, error):
    """The message of a failed command (its error output, else its output)."""
    status, out, err = outcome(result, error)
    return lines_of((err or out).strip()[-1500:]) or tr("exit status {status}", status=status)


def add_argv(fields):
    """The arguments of `sandbox-ai llm add` for FIELDS (see LlmDialog.request), each value checked as sandbox-ai
    llm checks it and none that reads as an option; ValueError says what is wrong. Keys are not among them."""
    return WL.server_argv(fields)


def colored(label, status):
    """LABEL's text in a status colour ("ok", "bad"; None: the usual one)."""
    label.setStyleSheet(f"color: {theme.color(status)};" if status else "")


class LlmDialog(QDialog):
    """A new LLM server, or EXISTING (its fields as wizard_llm.fields_of gives them) edited: its name, kind, URL,
    default model, VPN profile and key. REMOTE: the key is a secret of that host, named only (the value cannot reach
    it without a command line). RUN(*args, timeout=...) runs a sandbox-ai command on that host (blocking: in the
    background) for Test connection; TAKEN: the names registered there. REQUIRE(options) raises backend.HostTooOld
    when that host's `wizard models` does not know OPTIONS (backend.require_options; None: this machine's own).

    OK hands the fields to ON_SAVE(fields, value) - the page saves while the dialog stays open - and the dialog
    closes only once it saved (finish); failed(message) keeps it open with everything typed."""

    def __init__(self, parent=None, host_label="", remote=False, existing=None, run=None, taken=(), require=None):
        super().__init__(parent)
        self.remote, self.existing, self.run, self.taken = remote, existing, run, set(taken or ())
        self.host_label, self.require = host_label, require
        self.insecure_confirmed = None  # the URL plain http outside the local networks was allowed for (Test)
        self.on_save, self.saved, self.saving, self.check_after, self.tests = None, False, False, False, 0
        self.suggestion = None
        editing = existing is not None
        self.title_text = tr("Edit LLM server") if editing else tr("New LLM server")
        self.setWindowTitle(tr("Edit the LLM server {name} on {host}", name=printable(existing.get("name", "")),
                               host=host_label) if editing else tr("New LLM server on {host}", host=host_label))
        layout = QVBoxLayout(self)
        self.form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText(NAME_EXAMPLE)
        self.name.setAccessibleName(tr("Name"))
        self.name.setToolTip(tr("1-32 letters A-Z or a-z, digits, '_' or '-'; a digit may come first, '-' and '_' "
                                "not. It is what --llm NAME names."))
        self.name_hint = plain_label()
        self.name_hint.setAccessibleName(tr("What is wrong with the name"))
        self.suggest_button = QPushButton()
        self.suggest_button.setToolTip(tr("Take this name instead (the characters a name cannot hold as '-')"))
        self.suggest_button.clicked.connect(lambda _=False: self.name.setText(self.suggestion or self.name.text()))
        self.hint_row = QWidget()
        hint = QHBoxLayout(self.hint_row)
        hint.setContentsMargins(0, 0, 0, 0)
        hint.addWidget(self.name_hint, 1)
        hint.addWidget(self.suggest_button)
        self.kind = QComboBox()
        for key, label in KINDS:
            self.kind.addItem(tr(label), key)
        self.url = QLineEdit()
        self.url.setPlaceholderText(URL_EXAMPLE)
        self.url.setToolTip(tr("The server's root, http(s)://HOST[:PORT][/PREFIX]; each agent gets the API path it "
                               "needs. Plain http only to this host, private or Tailscale addresses."))
        self.test_button = QPushButton(tr("Test connection"))
        self.test_button.setAccessibleName(tr("Test connection"))
        self.test_button.setToolTip(tr("Ask the server for its models now, without saving it (sandbox-ai wizard "
                                       "models), and offer them as the default model"))
        self.test_button.clicked.connect(lambda _=False: self.test())
        self.test_result = plain_label(role="secondary")
        self.test_result.setAccessibleName(tr("Result of the test"))
        self.test_row = QWidget()
        test = QHBoxLayout(self.test_row)
        test.setContentsMargins(0, 0, 0, 0)
        test.addWidget(self.test_button)
        test.addWidget(self.test_result, 1)
        self.model_box = QComboBox()
        self.model_box.setEditable(True)
        self.model_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_box.setAccessibleName(tr("Default model"))
        self.model = self.model_box.lineEdit()  # what is typed or chosen
        self.model.setPlaceholderText(MODEL_EXAMPLE)
        self.model_box.setToolTip(tr("The model a start uses when --llm names none (optional): type it, or choose one "
                                     "Test connection listed"))
        self.vpn = QLineEdit()
        self.vpn.setPlaceholderText(VPN_EXAMPLE)
        self.vpn.setToolTip(tr("The VPN profile the server is reached through (optional)"))
        self.anthropic = QCheckBox(tr("It also offers the Anthropic Messages API (Claude Code)"))
        self.secret = QLineEdit()
        self.secret.setPlaceholderText(SECRET_EXAMPLE)
        self.value = QLineEdit()
        self.value.setPlaceholderText(tr("typed here: kept as that secret (or leave it empty if it is kept already)"))
        self.headers = QLineEdit()
        self.headers.setPlaceholderText(HEADER_EXAMPLE)
        self.headers.setToolTip(tr("Headers the proxy adds with the value of a secret (HEADER=SECRET, several "
                                   "separated by spaces), e.g. for a gateway"))
        self.ca = QLineEdit()
        self.ca.setPlaceholderText(tr("a PEM file on that host (optional)"))
        self.ca.setToolTip(tr("The CA that signed the server's certificate (copied)"))
        self.no_ca = QCheckBox(tr("Drop the CA it has"))
        self.no_ca.setToolTip(tr("sandbox-ai llm add --replace --no-ca: the server is reached with the system's CAs "
                                 "again"))
        if (existing or {}).get("has_ca"):
            self.ca.setPlaceholderText(tr("a CA is kept and stays; name a PEM file of that host to replace it"))
            self.ca.textChanged.connect(lambda text: self.no_ca.setEnabled(not text.strip()))
        self.insecure = QCheckBox(tr("Allow plain http to an address outside the local networks (unencrypted)"))
        self.replace = QCheckBox(tr("Replace an LLM server of that name"))
        self.form.addRow(tr("Name"), self.name)
        self.form.addRow("", self.hint_row)
        self.form.addRow(tr("Kind"), self.kind)
        self.form.addRow(tr("URL"), self.url)
        self.form.addRow("", self.test_row)
        self.form.addRow(tr("Default model"), self.model_box)
        self.form.addRow(tr("VPN profile"), self.vpn)
        self.form.addRow("", self.anthropic)
        self.form.addRow(tr("Secret for the API key (optional)"), self.secret)
        self.value_row = widgets.secret_value(self.value)  # the field with its "Show value" toggle
        self.form.addRow(tr("Its value"), self.value_row)
        self.form.addRow(tr("Headers from secrets"), self.headers)
        self.form.addRow(tr("CA certificate"), self.ca)
        self.form.addRow("", self.no_ca)
        self.form.setRowVisible(self.no_ca, bool((existing or {}).get("has_ca")))
        self.form.addRow("", self.insecure)
        self.form.addRow("", self.replace)
        layout.addLayout(self.form)
        note = tr("A key typed here is kept as a secret of sandbox-ai on the server's host (standard input, over SSH "
                  "for another host, never a command line); the server's entry only names that secret, and the proxy "
                  "adds the key on the wire.")
        layout.addWidget(plain_label(note, role="secondary"))
        self.note = plain_label()
        self.note.setAccessibleName(tr("Status"))
        self.note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.note.hide()
        layout.addWidget(self.note)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(tr("Save") if editing else tr("Add"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.kind.currentIndexChanged.connect(self.show_fields)
        self.kind.currentIndexChanged.connect(lambda *_: self.forget_test())
        self.url.textChanged.connect(lambda *_: self.forget_test())
        self.name.textChanged.connect(lambda *_: self.check_name())
        self.replace.toggled.connect(lambda *_: self.check_name())
        if editing:
            self.prefill(existing)
            self.name.setReadOnly(True)
            self.name.setToolTip(tr("A server keeps its name: add one under another name, then remove this one"))
            self.form.setRowVisible(self.replace, False)
        self.show_fields()
        self.check_name()

    def prefill(self, fields):
        """The dialog's fields as FIELDS (wizard_llm.fields_of) say."""
        self.name.setText(fields.get("name") or "")
        self.kind.setCurrentIndex(max(self.kind.findData(fields.get("kind")), 0))
        self.url.setText(fields.get("url") or "")
        self.model.setText(fields.get("model") or "")
        self.vpn.setText(fields.get("vpn") or "")
        self.anthropic.setChecked(bool(fields.get("anthropic")))
        self.secret.setText(fields.get("secret") or "")
        self.headers.setText(fields.get("headers") or "")
        self.ca.setText(fields.get("ca") or "")
        self.insecure.setChecked(bool(fields.get("insecure")))
        self.replace.setChecked(True)

    def show_fields(self):
        # Ollama, LM Studio, llama.cpp and vLLM offer the Messages API themselves (sandbox-ai llm knows since when).
        self.form.setRowVisible(self.anthropic, self.kind.currentData() == "openai-compatible")

    def request(self):
        """{name, kind, url, model, vpn, anthropic, secret, headers, ca, insecure, replace} as typed, and the typed
        key (None: none)."""
        fields = {"name": self.name.text().strip(), "kind": self.kind.currentData(), "url": self.url.text().strip(),
                  "model": self.model.text().strip(), "vpn": self.vpn.text().strip(),
                  "anthropic": self.anthropic.isChecked() and self.kind.currentData() == "openai-compatible",
                  "secret": self.secret.text().strip(), "headers": self.headers.text().strip(),
                  "ca": self.ca.text().strip(), "insecure": self.insecure.isChecked(),
                  "replace": self.replace.isChecked() or self.existing is not None,
                  "no_ca": bool((self.existing or {}).get("has_ca")) and self.no_ca.isChecked()
                  and not self.ca.text().strip()}
        value = self.value.text() or None
        return fields, value

    # -- the name, as it is typed --

    def check_name(self):
        """The hint below the name: what cannot be in it (red) and a name to use instead, or that a server of that
        name is there already; OK only for a valid name."""
        name = self.name.text().strip()
        problem = WL.name_problem(name)
        self.suggestion = WL.suggest_name(name) if problem else None
        text, status = "", None
        if problem and name:
            text, status = problem, "bad"
        elif not problem and self.existing is None and name in self.taken and not self.replace.isChecked():
            text, status = tr("A server named {name} is registered on this host already: check Replace below, or "
                              "cancel and use Edit… in the list.", name=name), "warn"
        self.name_hint.setText(text)
        colored(self.name_hint, status)
        self.suggest_button.setText(tr("Use {name}", name=self.suggestion) if self.suggestion else "")
        self.suggest_button.setVisible(bool(self.suggestion))
        self.form.setRowVisible(self.hint_row, bool(text))
        set_enabled(self.ok_button, problem is None and not self.saving, problem or tr("Saving…"))
        return problem

    # -- Test connection --

    def forget_test(self):
        """Another kind or URL: an earlier test says nothing about it."""
        self.tests += 1
        self.check_after = False
        self.test_result.setText("")
        colored(self.test_result, None)

    def test(self, insecure=False):
        """The models of the server as typed, without saving it: `sandbox-ai wizard models` for a server not saved
        yet (plain http, no prefix), `llm models NAME` for an edited one reached as it was saved. Neither: said so,
        and `llm check` runs once it is saved. A secret named (and no new key typed, which is not kept yet) goes with
        the request as on the web page, where that host's sandbox-ai knows it; plain http to a public address with it
        is refused there unless INSECURE (--insecure-http, sent only after the llm-insecure question: ask_insecure)."""
        fields, value = self.request()
        self.tests += 1
        ticket = self.tests
        same = self.existing is not None and all((self.existing.get(k) or "") == (fields.get(k) or "")
                                                 for k in CONNECTION)
        secret = fields["secret"] if fields.get("secret") and not value else None
        if same and valid_name(fields["name"]):
            args, probe, secret = ["llm", "models", fields["name"], "--sort", "loaded", "--json"], False, None
        else:
            try:
                args, probe = WL.probe_argv(fields["kind"], fields["url"], secret, insecure), True
            except ValueError as exc:
                self.check_after = True
                self.test_result.setText(tr("{reason} It is checked with sandbox-ai llm check (its key, TLS and CA) "
                                            "once it is saved.", reason=printable(exc)))
                colored(self.test_result, "warn")
                return
        if self.run is None:
            return
        self.check_after = False
        self.test_result.setText(tr("Asking the server…"))
        colored(self.test_result, None)
        run, require = self.run, self.require

        def work():
            sent = bool(probe and secret)
            if probe and require is not None:
                require(["--url"])  # 0.1.77 and older: "update the host" instead of argparse's error there
                if sent:
                    try:  # a host that does not know them is asked without the key, as before
                        require(["--api-key-secret", "--insecure-http"])
                    except backend.HostTooOld:
                        sent = False
            argv = args if sent or not probe else WL.probe_argv(fields["kind"], fields["url"])
            return run(*argv, timeout=WL.PROBE_SECONDS), sent

        background(work, owner=self, source=self.test_button, timeout=WL.PROBE_SECONDS + 75,
                   done=lambda result, error: self.tested(ticket, probe, fields, result, error, insecure))

    def tested(self, ticket, probe, fields, result, error, insecure=False):
        """RESULT: (the command's (status, out, err), whether the key went with it)."""
        if ticket != self.tests:
            return  # the kind or URL changed meanwhile: that answer is about another server
        result, sent = result if error is None else (None, False)
        if isinstance(error, backend.HostTooOld):
            self.test_result.setText(printable(error))
            colored(self.test_result, "warn")
            return
        status, out, err = outcome(result, error)
        if status != 0 and sent and not insecure and WL.plain_http_refused(err):
            self.ask_insecure(fields)  # only that refusal: asked, then tested again with --insecure-http
            return
        try:
            if status != 0:
                raise ValueError(failure(result, error))
            models = WL.parse_models(out)
        except ValueError as exc:
            text = tr("Not reachable: {error}", error=printable(exc))
            if probe and ((fields.get("secret") and not sent) or fields.get("headers")):
                text += "\n" + tr("(Tested without its key: a server that needs one refuses this test; llm check "
                                  "uses the key once it is saved.)")
            self.test_result.setText(text)
            colored(self.test_result, "bad")
            return
        usable = [m["name"] for m in models]
        loaded = sum(1 for m in models if m.get("loaded") is True)
        typed = self.model.text()
        self.model_box.clear()
        self.model_box.addItems(usable)
        self.model_box.setEditText(typed or (usable[0] if len(usable) == 1 else ""))
        if not models:
            text = tr("Reachable, but the server lists no models: load or pull one first.")
        else:
            text = tr("Reachable: {count} models, {loaded} loaded. Choose the default model below (or leave it "
                      "empty).", count=len(models), loaded=loaded)
        self.test_result.setText(text)
        colored(self.test_result, "ok")

    def ask_insecure(self, fields):
        """sandbox-ai refused to send the key over plain http to an address outside the local networks: asked as the
        web page asks (llm-insecure, naming the host and the URL). Yes: tested again with --insecure-http, and the
        server is saved with it (the box ticked; Add asks no second time for that URL). No: not tested, said."""
        url = fields["url"]
        if not ask_first(self, "llm-insecure", self.title_text, tr(
                "Send the prompts to {url} over plain http, unencrypted, from {host}? Whoever is on the way can read "
                "them, the answers and the key.", url=printable(url), host=self.host_label), tr("Allow")):
            self.test_result.setText(tr("Not tested: the key is not sent over plain http outside the local networks "
                                        "(use https, or allow it)."))
            colored(self.test_result, "warn")
            return
        self.insecure_confirmed = url
        self.insecure.setChecked(True)
        self.test(insecure=True)

    # -- OK: saved while the dialog stays open --

    def problem(self, text, status="bad", quiet=False):
        """TEXT below the fields (empty: none). A dialog that is not on screen (never shown) tells in a message
        unless QUIET (a question the human answered No: nothing to tell)."""
        self.note.setText(text)
        colored(self.note, status if text else None)
        self.note.setVisible(bool(text))
        if text and status == "bad" and not quiet and not self.isVisible():
            warn(self.parentWidget() or self, self.title_text, text, modal=False)

    def accept(self):
        if self.saving:
            return
        if self.check_name() is not None:
            self.problem(WL.name_problem(self.name.text().strip()))
            return
        fields, value = self.request()
        try:
            add_argv(fields)  # checked as sandbox-ai llm checks it, before anything runs
            if value and not fields["secret"]:
                raise ValueError(tr("A typed key needs the name of the secret to keep it as."))
        except ValueError as exc:
            self.problem(printable(exc))
            return
        if self.on_save is None:
            self.finish()
            return
        self.problem("")
        self.on_save(fields, value)

    def busy(self, text):
        self.saving = True
        self.problem(text, status=None)
        self.check_name()

    def failed(self, text, quiet=False):
        """Not saved: TEXT (the CLI's message; QUIET: a question answered No) shown, everything typed kept."""
        self.saving = False
        self.check_name()
        self.problem(text, quiet=quiet)

    def finish(self):
        self.saving, self.saved = False, True
        self.problem("")
        super().accept()


class LlmPage(QWidget):
    """The endpoints of `sandbox-ai llm` on this machine or a host of `sandbox-ai hosts`."""

    def __init__(self, sai):
        super().__init__()
        self.sai, self.entries, self.asked, self.loaded_host = sai, [], 0, None
        self.dialog = None  # the dialog open now (what a save's answer goes to)
        self.kept_status = ""  # what a change said: the list asked again after it says it first
        self.models_of, self.model_list, self.models_default = None, [], ""
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("LLM servers (sandbox-ai llm) the agents can use: start one with --llm NAME. "
                                        "Keys stay secrets of sandbox-ai; the connection proxy adds them on the wire, "
                                        "so an agent only sees a placeholder."), role="secondary"))
        top = QHBoxLayout()
        top.addWidget(plain_label(tr("Host"), word_wrap=False))
        self.host_box = QComboBox()
        self.host_box.setAccessibleName(tr("Host"))
        self.host_box.setToolTip(tr("This machine, or a host of sandbox-ai hosts (over SSH)"))
        self.host_box.currentIndexChanged.connect(lambda *_: self.refresh() if self.isVisible() else None)
        top.addWidget(self.host_box, 1)
        layout.addLayout(top)
        self.servers = table([tr("Name"), tr("Kind"), tr("URL"), tr("Model"), tr("Key"), tr("VPN"), tr("Agents")],
                             stretch=2)
        self.servers.setAccessibleName(tr("LLM servers"))
        self.servers.itemSelectionChanged.connect(self.update_buttons)
        self.servers.cellDoubleClicked.connect(lambda *_: self.edit())
        layout.addWidget(self.servers, 2)
        self.add_button = QPushButton(tr("Add…"))
        self.edit_button = QPushButton(tr("Edit…"))
        self.check_button = QPushButton(tr("Check"))
        self.agent = QComboBox()
        self.agent.addItem(tr("every agent"), "")
        for agent in AGENTS:
            self.agent.addItem(agent, agent)
        self.agent.setAccessibleName(tr("Agent"))
        self.agent.setToolTip(tr("Check the API this agent needs (or every agent's)"))
        self.models_button = QPushButton(tr("Models"))
        self.sort = QComboBox()
        for key, label in SORTS:
            self.sort.addItem(tr(label), key)
        self.sort.setAccessibleName(tr("Sort the models"))
        self.sort.setToolTip(tr("How Models sorts the list (sandbox-ai llm models --sort)"))
        self.show_button = QPushButton(tr("Show"))
        self.show_button.setAccessibleName(tr("Show"))
        self.show_button.setToolTip(tr("sandbox-ai llm show: the endpoint with what the agents get (no key)"))
        self.show_button.clicked.connect(lambda _=False: self.show_server())
        self.remove_button = QPushButton(tr("Remove…"))
        self.refresh_button = QPushButton(tr("Refresh"))
        for button, name, tip in (
                (self.add_button, tr("Add a server"), tr("Add an LLM server on the chosen host")),
                (self.edit_button, tr("Edit the server"), tr("Change the server's settings, the others kept (sandbox-ai "
                                                             "llm add --replace); a double-click does it too")),
                (self.check_button, tr("Check"), tr("Reachability, TLS, the key, the models and the agents' APIs")),
                (self.models_button, tr("Models"), tr("The models the server offers now: choose its default model "
                                                      "there")),
                (self.remove_button, tr("Remove"), tr("Forget the server (its secrets stay in sandbox-ai secrets)")),
                (self.refresh_button, tr("Refresh"), tr("Ask the chosen host again"))):
            button.setAccessibleName(name)
            button.setToolTip(safe_tooltip(tip))
        self.add_button.clicked.connect(self.add)
        self.edit_button.clicked.connect(lambda _=False: self.edit())
        self.check_button.clicked.connect(self.check)
        self.models_button.clicked.connect(self.models)
        self.remove_button.clicked.connect(self.remove)
        self.refresh_button.clicked.connect(self.refresh)
        # A row that wraps when the window is narrow (it made the window 1300 px wide in German before).
        layout.addWidget(flow(self.add_button, self.edit_button, self.check_button, self.agent, self.models_button,
                              self.sort, self.show_button, self.remove_button, self.refresh_button))
        layout.addWidget(self.build_models(), 3)
        self.status = plain_label(role="secondary")
        self.status.setAccessibleName(tr("Status"))
        layout.addWidget(self.status)
        self.load_hosts()
        follow_hosts(self, self.load_hosts)  # a host added, renamed or removed on the Hosts page: here at once
        self.update_buttons()

    def build_models(self):
        """The models of one server (Models): a filter, the table, Use as default and Copy NAME:MODEL."""
        self.models_box = QWidget()
        box = QVBoxLayout(self.models_box)
        box.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.models_title = plain_label(role="section")
        head.addWidget(self.models_title, 1)
        self.model_filter = QLineEdit()
        self.model_filter.setPlaceholderText(tr("Filter the models"))
        self.model_filter.setAccessibleName(tr("Filter the models"))
        self.model_filter.setToolTip(tr("Show only the models whose name holds this text"))
        self.model_filter.setClearButtonEnabled(True)
        self.model_filter.textChanged.connect(lambda *_: self.apply_filter())
        head.addWidget(self.model_filter, 1)
        box.addLayout(head)
        self.model_table = table([tr(title) for _, title in WL.MODEL_COLUMNS], stretch=0)
        self.model_table.setAccessibleName(tr("Models"))
        self.model_table.itemSelectionChanged.connect(self.update_model_buttons)
        self.model_table.horizontalHeader().sortIndicatorChanged.connect(lambda *_: self.apply_filter())
        box.addWidget(self.model_table, 1)
        buttons = QHBoxLayout()
        self.default_button = QPushButton(tr("Use as default"))
        self.default_button.setAccessibleName(tr("Use as default"))
        self.default_button.setToolTip(tr("Make the selected model the one a start uses when --llm names none "
                                          "(sandbox-ai llm add --replace --model, the other settings kept)"))
        self.default_button.clicked.connect(lambda _=False: self.use_model())
        self.copy_button = QPushButton(tr("Copy NAME:MODEL"))
        self.copy_button.setAccessibleName(tr("Copy NAME:MODEL"))
        self.copy_button.setToolTip(tr("Copy the value for --llm that starts with the selected model"))
        self.copy_button.clicked.connect(lambda _=False: self.copy_model())
        buttons.addWidget(self.default_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        box.addLayout(buttons)
        self.models_box.hide()
        self.update_model_buttons()
        return self.models_box

    # -- hosts --

    def load_hosts(self):
        """This machine, and each host of sandbox-ai hosts (a small local file: no SSH here)."""
        current = self.host()
        entries = []
        if self.sai.hosts_supported():
            try:
                entries = [e for e in self.sai.host_entries() if isinstance(e, dict)]
            except (OSError, ValueError) as exc:
                self.status.setText(printable(exc))
        self.host_box.blockSignals(True)
        self.host_box.clear()
        self.host_box.addItem(tr("{name} (this machine)", name=LOCAL), LOCAL)
        for entry in entries:
            name = entry.get("name")
            if entry.get("kind") == "local" or name == "local" or valid_host(name) is None:
                continue
            self.host_box.addItem(printable(f"{name}  {entry.get('ssh', '')}".strip()), name)
        index = self.host_box.findData(current) if current else 0
        self.host_box.setCurrentIndex(max(index, 0))
        self.host_box.blockSignals(False)
        if current and index < 0 and self.isVisible():
            self.refresh()  # its host left the list: this machine's servers, never that host's rows

    def host(self):
        return self.host_box.currentData()

    def host_label(self, host=None):
        host = host or self.host()
        return tr("{name} (this machine)", name="local") if host == "local" else printable(host)

    def showEvent(self, event):  # noqa: N802 - Qt's name
        """Asked when shown, not when the window is built: no command (and no SSH) for a page nobody opened."""
        super().showEvent(event)
        if not event.spontaneous():
            self.load_hosts()
            self.refresh()

    # -- the list --

    def refresh(self):
        host = valid_host(self.host())
        if host is None:
            return
        self.asked += 1
        ticket = self.asked
        if host != self.loaded_host:
            self.entries = []
            fill(self.servers, [], keys=[])
            self.update_buttons()
        if self.models_of and self.models_of[0] != host:
            self.models_box.hide()
            self.models_of = None
        self.status.setText(tr("Asking {host}…", host=self.host_label(host)))
        background(lambda: self.sai.host_run(host, "llm", "list", "--json", timeout=LIST_SECONDS), owner=self,
                   source=self.refresh_button, timeout=LIST_SECONDS + 15,
                   done=lambda result, error: self.listed(host, ticket, result, error))

    def listed(self, host, ticket, result, error):
        if ticket != self.asked or host != self.host():
            return  # another host was chosen meanwhile, or it was asked again: only the newest answer counts
        status, out, err = outcome(result, error)
        try:
            entries, notes = endpoints_of(out)
        except ValueError:
            self.status.setText(tr("Could not list the LLM servers on {host}: {error}", host=self.host_label(host),
                                   error=failure(result, error)))
            return
        self.entries, self.loaded_host = entries, host
        rows = [[text_of(e.get("name"), 64), text_of(e.get("kind"), 40), text_of(e.get("url")),
                 text_of(e.get("model")), text_of(e.get("api_key_secret"), 64), text_of(e.get("vpn"), 64),
                 text_of(e.get("agents"))] for e in entries]
        fill(self.servers, rows, keys=[e["name"] for e in entries])
        lines = [self.kept_status] if self.kept_status else []
        self.kept_status = ""
        lines += [tr("LLM servers on {host}: {count}", count=len(entries), host=self.host_label(host))
                  if entries else tr("No LLM server yet on {host}.", host=self.host_label(host))]
        lines += [lines_of(note) for note in notes]
        self.status.setText("\n".join(lines))
        self.update_buttons()

    def chosen(self):
        """The selected server's name, checked; None (and the status says why) otherwise."""
        keys = selected_keys(self.servers)
        if not keys:
            self.status.setText(tr("Select a server first."))
            return None
        name = valid_name(keys[0])
        if name is None:
            self.status.setText(tr("Refused: {name} is not a server's name that can be passed on.",
                                   name=printable(keys[0])))
        return name

    def update_buttons(self):
        keys = selected_keys(self.servers)
        for button in (self.edit_button, self.check_button, self.models_button, self.show_button, self.remove_button):
            set_enabled(button, bool(keys), tr("Select a server first."))
        if keys and self.models_of and self.models_of != (self.host(), keys[0]):
            self.models_box.hide()  # the models shown are another server's
            self.models_of = None
            self.update_model_buttons()

    # -- add and edit: the dialog stays open until the server is saved --

    def runner(self, host):
        """A command on HOST for the dialog's Test connection (blocking: the dialog runs it in the background)."""
        return functools.partial(self.sai.host_run, host)

    def requirement(self, host):
        """For the dialog's Test connection: HostTooOld when HOST's `wizard models` lacks the options named (the app's
        known-options check, backend.require_options; nothing to check on this machine)."""
        return None if host == "local" else functools.partial(self.sai.require_options, host, ("wizard", "models"))

    def add(self):
        host = valid_host(self.host())
        if host is None:
            return
        taken = [e["name"] for e in self.entries] if host == self.loaded_host else []
        self.open_dialog(LlmDialog(self, self.host_label(host), remote=host != "local", run=self.runner(host),
                                   taken=taken, require=self.requirement(host)), host, None)

    def edit(self):
        """The dialog prefilled from `llm show NAME --json` (and the CA's path from `llm show NAME`)."""
        host, name = valid_host(self.host()), self.chosen()
        if host is None or name is None:
            return
        self.status.setText(tr("Reading {name}…", name=name))
        self.server_fields(host, name, self.edit_button, lambda old: self.open_dialog(
            LlmDialog(self, self.host_label(host), remote=host != "local", existing=old, run=self.runner(host),
                      require=self.requirement(host)),
            host, old))

    def server_fields(self, host, name, source, then):
        """THEN(fields) with the settings of server NAME on HOST as the dialog has them (wizard_llm.fields_of), read
        in the background; the status says why when they cannot be read."""
        def work():
            status, out, err = self.sai.host_run(host, "llm", "show", name, "--json", timeout=SHOW_SECONDS)
            if status != 0:
                raise ValueError((err or out).strip()[-1500:] or tr("exit status {status}", status=status))
            return WL.fields_of(WL.parse_show(out))

        def done(fields, error):
            if error is not None:
                self.status.setText(tr("Could not read {name}: {error}", name=name, error=lines_of(str(error))))
                return
            self.status.setText("")
            then(fields)
        background(work, owner=self, source=source, timeout=SHOW_SECONDS + 15, done=done)

    def open_dialog(self, dialog, host, old):
        """DIALOG until it saved or was cancelled; the typed key is kept nowhere afterwards."""
        self.dialog = dialog
        dialog.on_save = lambda fields, value: self.save(dialog, host, fields, value, old)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and not dialog.saved:
                dialog.accept()  # an exec() that stood in for the human (the tests): OK as they would press it
        finally:
            # The typed key is passed on and kept nowhere else (not in a hidden dialog of the page).
            dialog.value.clear()
            self.dialog = None
            dialog.deleteLater()

    def save(self, dialog, host, fields, value, old=None):
        """`llm add` for the dialog's FIELDS (OLD: the server's settings when it is edited; --replace), after the
        questions it needs; whatever stops it is said in the dialog, which keeps what was typed."""
        name = fields["name"]
        try:
            args = add_argv(fields)
        except ValueError as exc:
            dialog.failed(printable(exc))
            return
        title = dialog.title_text
        changed = WL.changes(old, fields) if old is not None else []
        if old is not None and not changed and not value:
            self.status.setText(tr("{name}: nothing changed.", name=name))
            dialog.finish()
            return
        # Plain http to a public address: prompts, answers and the key unencrypted on the way - asked first (an
        # edited server that had it already keeps it without a question; so does one whose Test connection asked
        # that question for this URL and was answered yes).
        if fields.get("insecure") and not (old or {}).get("insecure") and \
                getattr(dialog, "insecure_confirmed", None) != fields["url"] and not ask_first(
                dialog, "llm-insecure", title, on_host(host, tr(
                    "Allow plain http to {url} even outside your local networks? Prompts, answers, code and the key "
                    "cross the network unencrypted.", url=printable(fields["url"]))), tr("Allow")):
            self.status.setText(tr("Not added."))
            dialog.failed(tr("Not saved: plain http outside the local networks was not allowed."), quiet=True)
            return
        # --replace: a server of that name changes for every start that uses it - asked first (llm-replace), naming
        # what changes when it is an edit.
        if old is not None and changed:
            if not ask_first(dialog, "llm-replace", title, on_host(host, tr(
                    "Change the LLM server {name}? Every start that uses it gets the new settings:\n{changes}",
                    name=name, changes="\n".join(changed))), tr("Replace")):
                self.status.setText(tr("Not changed."))
                dialog.failed(tr("Not saved."), quiet=True)
                return
        elif old is None and fields.get("replace") and not ask_first(dialog, "llm-replace", title, on_host(host, tr(
                "Replace an LLM server named {name} if there is one? Every start that uses it sends its prompts to "
                "the new address.", name=printable(name))), tr("Replace")):
            self.status.setText(tr("Not added."))
            dialog.failed(tr("Not saved."), quiet=True)
            return
        check = dialog.check_after
        if not value:
            self.start_add(host, fields, args, None, dialog=dialog, check=check)
            return
        # A typed key replaces the value of a secret of that name: whether there is one is looked at first.
        secret = fields["secret"]
        dialog.busy(tr("Looking for the secret {name}…", name=secret))
        self.status.setText(tr("Looking for the secret {name}…", name=secret))
        background(lambda: self.sai.secret_exists(secret, host), owner=self, source=self.add_button, timeout=90,
                   done=lambda exists, error: self.start_add(host, fields, args, value,
                                                              exists if error is None else None, dialog, check))

    def start_add(self, host, fields, args, value, exists=False, dialog=None, check=False):
        """`sandbox-ai llm add` first (it checks the name, the URL and every field; a secret not kept yet is only a
        note there), then the typed key VALUE as its secret (standard input): a server refused keeps no key. A secret
        of that name that EXISTS (None: not known) is replaced only after a yes."""
        secret = fields["secret"]
        if value:
            text = tr("A secret {name} is kept already: the key typed replaces its value, for everything that names "
                      "it. Replace it?", name=secret) if exists else \
                tr("Whether a secret {name} is kept already could not be told: the key typed would replace its "
                   "value. Go on?", name=secret)
            if not ask_first(dialog if self.dialog is dialog and dialog is not None else self, "secret-replace",
                             tr("New LLM server"), text, tr("Replace"), detail={"exists": exists}):
                self.status.setText(tr("Not added: the secret {name} stays as it is.", name=secret))
                if dialog is not None and self.dialog is dialog:
                    dialog.failed(self.status.text(), quiet=True)
                return

        def work():
            status, out, err = self.sai.host_run(host, *args, timeout=ADD_SECONDS)
            if status != 0 or not value:
                return (status, out, err), None
            kept = self.sai.set_secret(secret, value, host=host)
            return (status, out, err), None if kept[0] == 0 else (kept[2] or kept[1]).strip() or "?"

        self.status.setText(tr("Adding {name} on {host}…", name=fields["name"], host=self.host_label(host)))
        sources = [self.add_button, self.edit_button]
        if dialog is not None and self.dialog is dialog:
            dialog.busy(self.status.text())
            sources.append(dialog.test_button)
        background(work, owner=self, source=sources, timeout=ADD_SECONDS + 90,
                   done=lambda result, error: self.added(host, fields["name"], result, error, secret, dialog, check))

    def added(self, host, name, result, error, secret="", dialog=None, check=False):
        """RESULT: (the outcome of llm add, why the key was not kept or None). An open DIALOG closes once the server
        is saved and shows the CLI's message otherwise (everything typed kept); CHECK: `llm check` it then."""
        result, problem = result if error is None else (None, None)
        status, out, err = outcome(result, error)
        open_ = dialog is not None and self.dialog is dialog
        if status == 0 and problem is not None:
            self.status.setText(tr("{name} added, but its key was not kept as the secret {secret}: {error}",
                                   name=name, secret=secret, error=printable(problem)[:300]))
            if open_:
                dialog.finish()  # the server is saved: only its key is not
            warn(self, tr("New LLM server"), self.status.text(), modal=False)
        elif status != 0:
            self.status.setText(tr("Could not add {name} on {host}.", name=name, host=self.host_label(host)))
            if open_:
                dialog.failed(tr("sandbox-ai llm add refused it: {error}", error=failure(result, error)))
            else:
                warn(self, tr("New LLM server"), failure(result, error), modal=False)
        else:
            self.status.setText(lines_of(out.strip()[-1500:]) or tr("{name} added.", name=name))
            if open_:
                dialog.finish()
        if host == self.host():
            self.kept_status = self.status.text()
            self.refresh()
        if status == 0 and check:
            self.run_check(host, name, None)

    # -- check, models, show, remove --

    def check(self):
        host, name = valid_host(self.host()), self.chosen()
        if host is None or name is None:
            return
        self.run_check(host, name, self.agent.currentData())

    def run_check(self, host, name, agent):
        args = ["llm", "check", name] + (["--agent", agent] if agent in AGENTS else [])
        self.status.setText(tr("Checking {name}…", name=name))
        background(lambda: self.sai.host_run(host, *args, timeout=CHECK_SECONDS), owner=self,
                   source=self.check_button, timeout=CHECK_SECONDS + 30,
                   done=lambda result, error: self.checked(host, name, result, error))

    def checked(self, host, name, result, error):
        status, out, err = outcome(result, error)
        verdict = tr("the server works for it") if status == 0 else tr("the check found problems")
        self.status.setText(f"{name}: {verdict}")
        show_text(self, tr("Check of {name} on {host}", name=name, host=self.host_label(host)),
                  verdict + "\n\n" + lines_of((out + err).strip()), modal=False)

    def models(self):
        host, name = valid_host(self.host()), self.chosen()
        if host is None or name is None:
            return
        self.status.setText(tr("Asking {host}…", host=self.host_label(host)))
        sort = self.sort.currentData() if self.sort.currentData() in dict(SORTS) else "loaded"
        background(lambda: self.sai.host_run(host, "llm", "models", name, "--sort", sort, "--json",
                                             timeout=MODELS_SECONDS),
                   owner=self, source=self.models_button, timeout=MODELS_SECONDS + 30,
                   done=lambda result, error: self.listed_models(host, name, result, error))

    def listed_models(self, host, name, result, error):
        status, out, err = outcome(result, error)
        try:
            if status != 0:
                raise ValueError("failed")
            models = WL.parse_models(out)
        except ValueError:
            self.status.setText(tr("Could not list the models of {name}: {error}", name=name,
                                   error=failure(result, error)))
            return
        entry = next((e for e in self.entries if e.get("name") == name), {})
        self.models_of, self.model_list = (host, name), models
        self.models_default = entry.get("model") if isinstance(entry.get("model"), str) else ""
        self.status.setText(tr("Models listed: {count}", count=len(models)))
        self.show_models()

    def show_models(self):
        """The models of self.models_of in the table: the columns with values, the default model marked."""
        host, name = self.models_of
        models = self.model_list
        loaded = sum(1 for m in models if m.get("loaded") is True)
        title = tr("Models of {server}", server=f"{name} ({self.host_label(host)})")
        self.models_title.setText(f"{title}: {len(models)}, {tr('{count} loaded', count=loaded)}" if models
                                  else f"{title}: {tr('The server lists no models.')}")
        columns = WL.model_columns(models)
        widget = self.model_table
        widget.setColumnCount(len(columns))
        widget.setHorizontalHeaderLabels([tr(t) for _, t in columns])
        rows, sorts, tips, icons = [], [], [], []
        for model in models:
            cells = [WL.model_cell(model, key) for key, _ in columns]
            rows.append([text for text, _ in cells])
            sorts.append([value for _, value in cells])
            default = model["name"] == self.models_default
            tips.append("\n".join(part for part in (
                model["name"], model.get("details") or "", model.get("arch") or "",
                tr("the default model of {name}", name=name) if default else "") if part))
            icons.append("ok" if default else None)
        fill(widget, rows, tooltips=tips, icons=icons, sort=sorts, keys=[m["name"] for m in models])
        self.models_box.show()
        self.apply_filter()
        self.update_model_buttons()

    def apply_filter(self):
        text = self.model_filter.text().strip().lower()
        widget = self.model_table
        for row in range(widget.rowCount()):
            hidden = bool(text) and text not in str(row_key(widget, row) or "").lower()
            widget.setRowHidden(row, hidden)
            if hidden and widget.item(row, 0) is not None and widget.item(row, 0).isSelected():
                widget.clearSelection()
        self.update_model_buttons()

    def chosen_model(self):
        widget = self.model_table
        rows = [index.row() for index in widget.selectionModel().selectedRows()] if widget.selectionModel() else []
        rows = [row for row in rows if not widget.isRowHidden(row)]
        model = row_key(widget, rows[0]) if rows else None
        return model if self.models_of and WL.valid_model(model) else None

    def update_model_buttons(self):
        model = self.chosen_model() if hasattr(self, "model_table") else None
        set_enabled(self.copy_button, model is not None, tr("Select a model first."))
        set_enabled(self.default_button, model is not None and model != self.models_default,
                    tr("It is the default model already.") if model else tr("Select a model first."))

    def copy_model(self):
        model = self.chosen_model()
        if model is None:
            return
        value = WL.llm_value(self.models_of[1], model)
        QApplication.clipboard().setText(value)
        self.status.setText(tr("Copied {value}: start with --llm {value}", value=value))

    def use_model(self):
        """The selected model as the server's default: `llm add NAME ... --model MODEL --replace` with every other
        setting of `llm show NAME --json`, after a yes naming the change (llm-replace)."""
        model = self.chosen_model()
        if model is None or self.models_of is None:
            return
        host, name = self.models_of
        if valid_host(host) is None or valid_name(name) is None:
            return
        self.status.setText(tr("Reading {name}…", name=name))
        self.server_fields(host, name, self.default_button, lambda old: self.set_default(host, name, model, old))

    def set_default(self, host, name, model, old):
        new = dict(old, model=model)
        changed = WL.changes(old, new)
        if not changed:
            self.status.setText(tr("{model} is the default model of {name} already.", model=model, name=name))
            return
        try:
            args = add_argv(new)
        except ValueError as exc:
            self.status.setText(tr("Could not change {name}: {error}", name=name, error=printable(exc)))
            return
        if not ask_first(self, "llm-replace", tr("Use as default"), on_host(host, tr(
                "Change the LLM server {name}? Every start that uses it gets the new settings:\n{changes}",
                name=name, changes="\n".join(changed))), tr("Replace")):
            self.status.setText(tr("Not changed."))
            return
        self.status.setText(tr("Changing {name}…", name=name))
        background(lambda: self.sai.host_run(host, *args, timeout=ADD_SECONDS), owner=self,
                   source=[self.default_button, self.edit_button], timeout=ADD_SECONDS + 15,
                   done=lambda result, error: self.default_set(host, name, model, result, error))

    def default_set(self, host, name, model, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            self.status.setText(tr("Could not change {name}: {error}", name=name, error=failure(result, error)))
            return
        self.status.setText(tr("{model} is the default model of {name} now.", model=model, name=name))
        if self.models_of == (host, name):
            self.models_default = model
            self.show_models()
        if host == self.host():
            self.kept_status = self.status.text()
            self.refresh()

    def show_server(self):
        """`sandbox-ai llm show NAME` on the host chosen, as it says it."""
        host, name = valid_host(self.host()), self.chosen()
        if host is None or name is None:
            return
        background(lambda: self.sai.host_run(host, "llm", "show", name, timeout=SHOW_SECONDS), owner=self,
                   source=self.show_button, timeout=SHOW_SECONDS + 15,
                   done=lambda result, error: self.server_shown(host, name, result, error))

    def server_shown(self, host, name, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            self.status.setText(tr("Could not show {name}: {error}", name=name, error=failure(result, error)))
            return
        show_text(self, tr("LLM server {name} on {host}", name=name, host=self.host_label(host)), lines_of(out, 12000),
                  modal=False)

    def remove(self):
        host, name = valid_host(self.host()), self.chosen()
        if host is None or name is None:
            return
        if not ask_first(self, "llm-remove", tr("Remove"), tr(
                "Forget the LLM server {name} on {host}? Starts with {option} cannot start until it is added again.",
                name=name, host=self.host_label(host), option=f"--llm {name}"), tr("Remove")):
            return
        background(lambda: self.sai.host_run(host, "llm", "remove", name, timeout=REMOVE_SECONDS), owner=self,
                   source=self.remove_button, timeout=REMOVE_SECONDS + 15,
                   done=lambda result, error: self.removed(host, name, result, error))

    def removed(self, host, name, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            self.status.setText(tr("Could not forget {name} on {host}: {error}", name=name, host=self.host_label(host),
                                   error=failure(result, error)))
        else:
            self.status.setText(tr("{name} removed.", name=name))
            if self.models_of == (host, name):
                self.models_box.hide()
                self.models_of = None
        if host == self.host():
            self.refresh()
