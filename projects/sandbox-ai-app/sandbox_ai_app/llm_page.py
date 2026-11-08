"""The LLM servers page (sandbox-ai llm): the endpoints kept on this machine or on a host of `sandbox-ai hosts`,
with Add, Check, Models and Remove, as the web interface's LLM page offers them.

Every command runs in the background (`sandbox-ai llm ...` here, `sandbox-ai hosts run HOST -- llm ...` for a
host), only while the page is shown or a button asks: no command at every start of the app.

An API key typed in for this machine is kept as a secret of sandbox-ai first (`sandbox-ai secrets set NAME`, the
value on standard input), and the endpoint only names that secret (--api-key-secret NAME): the key is never on a
command line, in a file of the app or in sandbox-ai's llm.json. For another host the app has no way to hand a value
to that host's secrets without a command line, so the dialog asks for the name of a secret kept there already.

What the CLI or a host answers is shown through printable(); names that go back as arguments are checked first
(never one that reads as an option)."""
from __future__ import annotations

import json
import re

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from .files import printable
from .i18n import N_, tr
from .widgets import background, confirm, fill, outcome, plain_label, selected_keys, set_enabled, show_text, table, warn

# As sandbox-ai llm checks them (llm.py: NAME, SECRET, MODEL_NAME, VPN_PROFILE, URL).
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
MODEL = re.compile(r"[\w.:/@+-]{1,200}", re.ASCII)
VPN = NAME
URL = re.compile(r"https?://(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(?::[0-9]{1,5})?(/[A-Za-z0-9._~/-]{0,200})?")
LOCAL = "local"  # this machine, as sandbox-ai hosts names it
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
KINDS = (("lmstudio", N_("LM Studio")), ("ollama", N_("Ollama")), ("llamacpp", N_("llama.cpp server")),
         ("vllm", N_("vLLM")), ("openai-compatible", N_("OpenAI-compatible server (a gateway, LiteLLM, …)")))
AGENTS = ("claude", "codex", "opencode")
NAME_EXAMPLE = "lmstudio"  # the same in every language
URL_EXAMPLE = "http://192.168.1.20:1234"
MODEL_EXAMPLE = "qwen2.5-coder:7b"
SECRET_EXAMPLE = "LLM_KEY"
VPN_EXAMPLE = "home"
LIST_SECONDS, ADD_SECONDS, CHECK_SECONDS, MODELS_SECONDS, REMOVE_SECONDS = 60, 90, 150, 120, 60


def valid_name(name):
    """NAME if it may go back to sandbox-ai as an endpoint's name (never one that reads as an option), else None."""
    return name if isinstance(name, str) and not name.startswith("-") and NAME.fullmatch(name) else None


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


def size_text(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "?"
    for unit, factor in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if value >= factor:
            return f"{value / factor:.1f} {unit}"
    return f"{value} B"


def models_text(models):
    """`sandbox-ai llm models --json` ([{name, size, loaded, details}]) as aligned lines."""
    loaded = {True: tr("yes"), False: tr("no")}
    rows = [[tr("Name"), tr("Size"), tr("Loaded"), tr("Details")]]
    rows += [[text_of(m.get("name"), 200), size_text(m.get("size")),
              loaded[m["loaded"]] if isinstance(m.get("loaded"), bool) else "?",
              text_of(m.get("details"), 120)]
             for m in models if isinstance(m, dict)]
    if len(rows) == 1:
        return tr("The server lists no models.")
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in rows)


class LlmDialog(QDialog):
    """A new LLM server: its name, kind, URL, default model, VPN profile and key. REMOTE: the key is a secret of
    that host, named only (the value cannot reach it without a command line)."""

    def __init__(self, parent=None, host_label="", remote=False):
        super().__init__(parent)
        self.remote = remote
        self.setWindowTitle(tr("New LLM server on {host}", host=host_label))
        layout = QVBoxLayout(self)
        self.form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText(NAME_EXAMPLE)
        self.kind = QComboBox()
        for key, label in KINDS:
            self.kind.addItem(tr(label), key)
        self.url = QLineEdit()
        self.url.setPlaceholderText(URL_EXAMPLE)
        self.url.setToolTip(tr("The server's root, http(s)://HOST[:PORT][/PREFIX]; each agent gets the API path it "
                               "needs. Plain http only to this host, private or Tailscale addresses."))
        self.model = QLineEdit()
        self.model.setPlaceholderText(MODEL_EXAMPLE)
        self.model.setToolTip(tr("The model a start uses when --llm names none (optional)"))
        self.vpn = QLineEdit()
        self.vpn.setPlaceholderText(VPN_EXAMPLE)
        self.vpn.setToolTip(tr("The VPN profile the server is reached through (optional)"))
        self.anthropic = QCheckBox(tr("It also offers the Anthropic Messages API (Claude Code)"))
        self.secret = QLineEdit()
        self.secret.setPlaceholderText(SECRET_EXAMPLE)
        self.value = QLineEdit()
        self.value.setEchoMode(QLineEdit.EchoMode.Password)
        self.value.setPlaceholderText(tr("typed here: kept as that secret (or leave it empty if it is kept already)"))
        self.form.addRow(tr("Name"), self.name)
        self.form.addRow(tr("Kind"), self.kind)
        self.form.addRow(tr("URL"), self.url)
        self.form.addRow(tr("Model"), self.model)
        self.form.addRow(tr("VPN profile"), self.vpn)
        self.form.addRow("", self.anthropic)
        self.form.addRow(tr("Secret for the API key (optional)"), self.secret)
        self.form.addRow(tr("Its value"), self.value)
        layout.addLayout(self.form)
        if remote:
            note = tr("On another host the key is a secret of sandbox-ai there: keep it on that host first (sandbox-ai "
                      "secrets set NAME), then name it here. The app does not send a key to another host.")
        else:
            note = tr("A key typed here is kept as a secret of sandbox-ai (standard input, never a command line); the "
                      "server's entry only names that secret, and the proxy adds the key on the wire.")
        layout.addWidget(plain_label(note, role="secondary"))
        self.form.setRowVisible(self.value, not remote)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self.show_fields)
        self.show_fields()

    def show_fields(self):
        # Ollama, LM Studio, llama.cpp and vLLM offer the Messages API themselves (sandbox-ai llm knows since when).
        self.form.setRowVisible(self.anthropic, self.kind.currentData() == "openai-compatible")

    def request(self):
        """{name, kind, url, model, vpn, anthropic, secret} as typed, and the typed key (None: none)."""
        fields = {"name": self.name.text().strip(), "kind": self.kind.currentData(), "url": self.url.text().strip(),
                  "model": self.model.text().strip(), "vpn": self.vpn.text().strip(),
                  "anthropic": self.anthropic.isChecked() and self.kind.currentData() == "openai-compatible",
                  "secret": self.secret.text().strip()}
        value = None if self.remote else (self.value.text() or None)
        return fields, value


def add_argv(fields):
    """The arguments of `sandbox-ai llm add` for FIELDS (see LlmDialog.request), each value checked as sandbox-ai
    llm checks it and none that reads as an option; ValueError says what is wrong. Keys are not among them."""
    name = valid_name(fields.get("name"))
    if name is None:
        raise ValueError(tr("A server's name is 1-32 letters, digits, '_' or '-', not starting with '-'."))
    kind = fields.get("kind")
    if kind not in dict(KINDS):
        raise ValueError(tr("Choose the kind of server."))
    url = fields.get("url") or ""
    if not URL.fullmatch(url):
        raise ValueError(tr("The URL is http(s)://HOST[:PORT][/PREFIX], without user, query or fragment."))
    args = ["llm", "add", name, "--kind", kind, "--url", url]
    model = fields.get("model")
    if model:
        if model.startswith("-") or not MODEL.fullmatch(model):
            raise ValueError(tr("Not a model name: {model}", model=printable(model)))
        args += ["--model", model]
    vpn = fields.get("vpn")
    if vpn:
        if valid_name(vpn) is None:
            raise ValueError(tr("Not a VPN profile's name: {name}", name=printable(vpn)))
        args += ["--vpn", vpn]
    secret = fields.get("secret")
    if secret:
        if not SECRET.fullmatch(secret):
            raise ValueError(tr("A secret's name is letters, digits and '_', not starting with a digit."))
        args += ["--api-key-secret", secret]
    if fields.get("anthropic") and kind == "openai-compatible":
        args.append("--anthropic")
    return args


class LlmPage(QWidget):
    """The endpoints of `sandbox-ai llm` on this machine or a host of `sandbox-ai hosts`."""

    def __init__(self, sai):
        super().__init__()
        self.sai, self.entries, self.asked, self.loaded_host = sai, [], 0, None
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
        layout.addWidget(self.servers, 1)
        row = QHBoxLayout()
        self.add_button = QPushButton(tr("Add…"))
        self.check_button = QPushButton(tr("Check"))
        self.agent = QComboBox()
        self.agent.addItem(tr("every agent"), "")
        for agent in AGENTS:
            self.agent.addItem(agent, agent)
        self.agent.setAccessibleName(tr("Agent"))
        self.agent.setToolTip(tr("Check the API this agent needs (or every agent's)"))
        self.models_button = QPushButton(tr("Models"))
        self.remove_button = QPushButton(tr("Remove…"))
        self.refresh_button = QPushButton(tr("Refresh"))
        for button, name, tip in (
                (self.add_button, tr("Add a server"), tr("Add an LLM server on the chosen host")),
                (self.check_button, tr("Check"), tr("Reachability, TLS, the key, the models and the agents' APIs")),
                (self.models_button, tr("Models"), tr("The models the server offers now")),
                (self.remove_button, tr("Remove"), tr("Forget the server (its secrets stay in sandbox-ai secrets)")),
                (self.refresh_button, tr("Refresh"), tr("Ask the chosen host again"))):
            button.setAccessibleName(name)
            button.setToolTip(tip)
        self.add_button.clicked.connect(self.add)
        self.check_button.clicked.connect(self.check)
        self.models_button.clicked.connect(self.models)
        self.remove_button.clicked.connect(self.remove)
        self.refresh_button.clicked.connect(self.refresh)
        for widget in (self.add_button, self.check_button, self.agent, self.models_button, self.remove_button):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.status = plain_label(role="secondary")
        self.status.setAccessibleName(tr("Status"))
        layout.addWidget(self.status)
        self.load_hosts()
        self.update_buttons()

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
        lines = [tr("LLM servers on {host}: {count}", count=len(entries), host=self.host_label(host))
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
        chosen = bool(selected_keys(self.servers))
        for button in (self.check_button, self.models_button, self.remove_button):
            set_enabled(button, chosen, tr("Select a server first."))

    # -- actions --

    def add(self):
        host = valid_host(self.host())
        if host is None:
            return
        dialog = LlmDialog(self, self.host_label(host), remote=host != "local")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.value.clear()
            dialog.deleteLater()
            return
        fields, value = dialog.request()
        # The typed key is passed on below and kept nowhere else (not in a hidden dialog of the page).
        dialog.value.clear()
        dialog.deleteLater()
        try:
            args = add_argv(fields)
            if value and not fields["secret"]:
                raise ValueError(tr("A typed key needs the name of the secret to keep it as."))
        except ValueError as exc:
            warn(self, tr("New LLM server"), printable(exc))
            return
        secret = fields["secret"]

        def work():
            if value and host == "local":
                status, out, err = self.sai.set_secret(secret, value)
                if status != 0:
                    return status, out, err
            return self.sai.host_run(host, *args, timeout=ADD_SECONDS)

        self.status.setText(tr("Adding {name} on {host}…", name=fields["name"], host=self.host_label(host)))
        background(work, owner=self, source=self.add_button, timeout=ADD_SECONDS + 30,
                   done=lambda result, error: self.added(host, fields["name"], result, error))

    def added(self, host, name, result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            self.status.setText(tr("Could not add {name} on {host}.", name=name, host=self.host_label(host)))
            warn(self, tr("New LLM server"), failure(result, error), modal=False)
        else:
            self.status.setText(lines_of(out.strip()[-1500:]) or tr("{name} added.", name=name))
        if host == self.host():
            self.refresh()

    def check(self):
        host, name = valid_host(self.host()), self.chosen()
        if host is None or name is None:
            return
        agent = self.agent.currentData()
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
        background(lambda: self.sai.host_run(host, "llm", "models", name, "--json", timeout=MODELS_SECONDS),
                   owner=self, source=self.models_button, timeout=MODELS_SECONDS + 30,
                   done=lambda result, error: self.listed_models(host, name, result, error))

    def listed_models(self, host, name, result, error):
        status, out, err = outcome(result, error)
        try:
            models = json.loads(out)
            if not isinstance(models, list):
                raise ValueError("not a list")
        except ValueError:
            self.status.setText(tr("Could not list the models of {name}: {error}", name=name,
                                   error=failure(result, error)))
            return
        self.status.setText(tr("Models listed: {count}", count=len(models)))
        show_text(self, tr("Models of {server}", server=f"{name} ({self.host_label(host)})"), models_text(models),
                  modal=False)

    def remove(self):
        host, name = valid_host(self.host()), self.chosen()
        if host is None or name is None:
            return
        if not confirm(self, tr("Remove"), tr("Forget the LLM server {name} on {host}? Starts with {option} cannot "
                                              "start until it is added again.", name=name, host=self.host_label(host),
                                              option=f"--llm {name}"), tr("Remove"), destructive=True):
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
        if host == self.host():
            self.refresh()
