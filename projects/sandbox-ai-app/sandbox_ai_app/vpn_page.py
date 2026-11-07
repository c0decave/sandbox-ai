"""VPN profiles (sandbox-ai vpn): the list, adding one per kind, checking and removing. Keys, passwords, account
numbers and auth keys never reach a command line or a file of the app: typed in, they are kept as a secret of
sandbox-ai (standard input), and the profile names that secret. A WireGuard or OpenVPN configuration is the user's
own file; sandbox-ai copies what it needs."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from .files import printable
from .i18n import N_, tr
from .widgets import background, fill, outcome, plain_label, selected, table

KINDS = (("wireguard", N_("WireGuard (a wg-quick file: Proton, IVPN, AirVPN, your own server)")),
         ("openvpn", N_("OpenVPN (an .ovpn file)")),
         ("mullvad", N_("Mullvad (your account number)")),
         ("tailscale", N_("Tailscale or Headscale (an auth key)")))
# What each kind asks for: (field, label) - fields not named are hidden for that kind.
FIELDS = {"wireguard": ("file",), "openvpn": ("file", "secret", "value"),
          "mullvad": ("secret", "value", "location"), "tailscale": ("secret", "value", "exit_node", "login_server")}
SECRET_LABELS = {"openvpn": N_("Secret for USER:PASSWORD (optional)"), "mullvad": N_("Secret for the account number"),
                 "tailscale": N_("Secret for the auth key (optional: else log in in a terminal)")}
NAME_EXAMPLE = "home"  # the same in every language
SECRET_EXAMPLE = "VPN_HOME"
LOCATION_EXAMPLE = "se-sto"
EXIT_NODE_EXAMPLE = "exit-node-1"
LOGIN_SERVER_EXAMPLE = "https://headscale.example.org"


class VpnDialog(QDialog):
    """A new VPN profile: its name, its kind and what that kind needs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("New VPN profile"))
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("Keys and passwords are kept as secrets of sandbox-ai and reach only the "
                                        "VPN container, never the agent."), role="secondary"))
        self.form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText(NAME_EXAMPLE)
        self.kind = QComboBox()
        for key, label in KINDS:
            self.kind.addItem(tr(label), key)
        self.file = QLineEdit()
        browse = QPushButton(tr("File…"))
        browse.clicked.connect(self.choose)
        file_row = QWidget()
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.addWidget(self.file, 1)
        file_layout.addWidget(browse)
        self.secret = QLineEdit()
        self.secret.setPlaceholderText(SECRET_EXAMPLE)
        self.value = QLineEdit()
        self.value.setEchoMode(QLineEdit.EchoMode.Password)
        self.value.setPlaceholderText(tr("typed here: kept as that secret (or leave it empty if it is kept already)"))
        self.location = QLineEdit()
        self.location.setPlaceholderText(LOCATION_EXAMPLE)
        self.exit_node = QLineEdit()
        self.exit_node.setPlaceholderText(EXIT_NODE_EXAMPLE)
        self.login_server = QLineEdit()
        self.login_server.setPlaceholderText(LOGIN_SERVER_EXAMPLE)
        self.rows = {"file": (tr("Configuration"), file_row), "secret": (tr("Secret"), self.secret),
                     "value": (tr("Its value"), self.value), "location": (tr("Location"), self.location),
                     "exit_node": (tr("Exit node"), self.exit_node), "login_server": (tr("Login server"), self.login_server)}
        self.form.addRow(tr("Name"), self.name)
        self.form.addRow(tr("Kind"), self.kind)
        for label, widget in self.rows.values():
            self.form.addRow(label, widget)
        layout.addLayout(self.form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self.show_fields)
        self.show_fields()

    def show_fields(self):
        kind = self.kind.currentData()
        for field, (_, widget) in self.rows.items():
            visible = field in FIELDS[kind]
            self.form.setRowVisible(widget, visible)
        if kind in SECRET_LABELS:
            label = self.form.labelForField(self.secret)
            if label is not None:
                label.setText(tr(SECRET_LABELS[kind]))

    def choose(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, tr("VPN configuration"))
        if path:
            self.file.setText(path)

    def request(self):
        """(name, kind, secret value or None, fields for SandboxAI.vpn_add)."""
        kind = self.kind.currentData()
        wanted = FIELDS[kind]
        fields = {"file": self.file.text().strip() or None if "file" in wanted else None,
                  "secret": self.secret.text().strip() or None if "secret" in wanted else None,
                  "location": self.location.text().strip() or None if "location" in wanted else None,
                  "exit_node": self.exit_node.text().strip() or None if "exit_node" in wanted else None,
                  "login_server": self.login_server.text().strip() or None if "login_server" in wanted else None}
        value = self.value.text() if "value" in wanted and self.value.text() else None
        return self.name.text().strip(), kind, value, {k: v for k, v in fields.items() if v is not None}


def add_profile(parent, sai, done=None):
    """Ask for a new profile and add it in the background; DONE(name or None) afterwards."""
    dialog = VpnDialog(parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        dialog.value.clear()
        dialog.deleteLater()
        return
    name, kind, value, fields = dialog.request()
    # The typed key or password is passed on below and kept nowhere else: not in a hidden dialog that lives on as
    # a child of the page until the window closes.
    dialog.value.clear()
    dialog.deleteLater()
    try:
        sai.vpn_add_argv(name, kind, **fields)  # checked before anything is kept
        if value and not fields.get("secret"):
            raise ValueError(tr("A typed key or password needs the name of the secret to keep it as."))
    except ValueError as exc:
        QMessageBox.warning(parent, tr("New VPN profile"), printable(exc))
        return

    def added(result, error):
        status, out, err = outcome(result, error)
        if status != 0:
            QMessageBox.warning(parent, tr("New VPN profile"), printable((err or out).strip()[-1500:]))
        if done:
            done(name if status == 0 else None)
    background(lambda: sai.vpn_add(name, kind, secret_value=value, **fields), owner=parent, timeout=240, done=added)


class VpnBox(QGroupBox):
    """The VPN profiles of sandbox-ai vpn, with Add, Check and Remove."""

    def __init__(self, sai):
        super().__init__(tr("VPN profiles (sandbox-ai vpn)"))
        self.sai = sai
        layout = QVBoxLayout(self)
        self.profiles = table([tr("Profile"), tr("Kind"), tr("Provider")])
        layout.addWidget(self.profiles)
        row = QHBoxLayout()
        self.add_button, self.check_button, self.remove_button = \
            QPushButton(tr("Add…")), QPushButton(tr("Check")), QPushButton(tr("Remove…"))
        self.add_button.clicked.connect(lambda: add_profile(self, self.sai, lambda name: self.refresh()))
        self.check_button.clicked.connect(self.check)
        self.remove_button.clicked.connect(self.remove)
        for widget in (self.add_button, self.check_button, self.remove_button):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        self.message = plain_label()
        layout.addWidget(self.message)

    def refresh(self):
        background(self.sai.vpn_profiles, owner=self, timeout=90, done=self.shown)

    def shown(self, result, error):
        if error is not None:
            self.message.setText(printable(error))
            return
        fill(self.profiles, [[e.get("profile", ""), e.get("type", ""), e.get("provider", "")] for e in result or []])

    def check(self):
        name = selected(self.profiles)
        if not name:
            return
        self.message.setText(tr("Checking {name}…", name=name))
        background(lambda: self.sai.vpn_check(name), owner=self, timeout=240,
                   done=lambda result, error: self.message.setText(printable("\n".join(
                       part for part in outcome(result, error)[1:] if part).strip()[-1500:])))

    def remove(self):
        name = selected(self.profiles)
        if not name or QMessageBox.question(self, tr("Remove"), tr("Remove the VPN profile {name}?", name=name)) \
                != QMessageBox.StandardButton.Yes:
            return
        background(lambda: self.sai.vpn_remove(name), owner=self, timeout=90, done=lambda *_: self.refresh())
