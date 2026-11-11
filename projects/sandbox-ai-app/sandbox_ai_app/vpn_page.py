"""VPN profiles (sandbox-ai vpn) on this machine or a host of `sandbox-ai hosts`: the list, adding one per kind
(WireGuard, OpenVPN, Mullvad, Tailscale, Tor) with the advanced options of `vpn add`, showing, checking and removing.

Keys, passwords, account numbers and auth keys never reach a command line or a file of the app: typed in, they are
kept as a secret of sandbox-ai on that host (`sandbox-ai secrets set NAME`, the value on standard input, over SSH for
a host), and the profile names that secret. A WireGuard or OpenVPN configuration, and Tor's bridge lines, are files
of that host (for another host: a path there); sandbox-ai copies what it needs. Every value that goes back as an
argument is checked first and passed as --option=VALUE, so none of them is read as an option."""
from __future__ import annotations

import ipaddress
import os
import re

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QMessageBox,
    QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from .files import printable
from . import widgets
from .i18n import N_, tr
from .host_choice import on_host
from .name_fields import NameField, publish
from .widgets import (
    ask_first, background, delete_key, disclosure, fill, multi_select, one_only, outcome, plain_label, report_each,
    run_each, selected, selected_keys, several, show_text, table, shown_as_text,
)

LOCAL = "local"
KINDS = (("wireguard", N_("WireGuard (a wg-quick file: Proton, IVPN, AirVPN, your own server)")),
         ("openvpn", N_("OpenVPN (an .ovpn file)")),
         ("mullvad", N_("Mullvad (your account number)")),
         ("tailscale", N_("Tailscale or Headscale (an auth key)")),
         ("tor", N_("Tor (every connection through Tor, .onion names work)")))
# What each kind asks for - fields not named are hidden for that kind.
FIELDS = {"wireguard": ("file",), "openvpn": ("file", "secret", "value", "split"),
          "mullvad": ("secret", "value", "location", "ipv6"),
          "tailscale": ("secret", "value", "exit_node", "login_server", "hostname", "ephemeral", "tailnet_allow"),
          "tor": ("bridges", "bridge_secret", "exit_country", "socks")}
SECRET_LABELS = {"openvpn": N_("Secret for USER:PASSWORD (optional)"), "mullvad": N_("Secret for the account number"),
                 "tailscale": N_("Secret for the auth key (optional: else log in in a terminal)")}
SECRET_OPTIONS = {"openvpn": "--auth-secret", "mullvad": "--account-secret", "tailscale": "--auth-key-secret"}
IPV6 = (("", N_("IPv6 in the tunnel (the default)")), ("no-ipv6", N_("no IPv6 in the tunnel (IPv6 is blocked)")),
        ("endpoint-ipv6", N_("reach the relay over IPv6")))
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # vpn.py: a profile's name
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
LOCATION = re.compile(r"[a-z]{2}(?:-[a-z0-9-]{1,20})?")
EXIT_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}")
LOGIN_SERVER = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?")
HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
COUNTRIES = re.compile(r"[a-z]{2}(?:,[a-z]{2})*")
CHECK_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/?=&%-]*)?")
NAME_EXAMPLE = "home"  # the same in every language
SECRET_EXAMPLE = "VPN_HOME"
LOCATION_EXAMPLE = "se-sto"
EXIT_NODE_EXAMPLE = "exit-node-1"
LOGIN_SERVER_EXAMPLE = "https://headscale.example.org"
DNS_EXAMPLE = "10.64.0.1 2a07:a880::1"
CHECK_EXAMPLE = "https://am.i.mullvad.net/json"
CIDR_EXAMPLE = "100.64.0.7/32 fd7a:115c:a1e0::/48"
BRIDGE_SECRET_EXAMPLE = "TOR_BRIDGES"
COUNTRY_EXAMPLE = "de,nl"
PROVIDER_EXAMPLE = "mullvad"
LIST_SECONDS, ADD_SECONDS, CHECK_SECONDS, SHOW_SECONDS, REMOVE_SECONDS = 90, 240, 240, 60, 90


def words(text):
    return [part for part in re.split(r"[\s,]+", text or "") if part]


def add_args(name, kind, fields):
    """The arguments of `sandbox-ai vpn add` for a profile NAME of KIND with FIELDS (see VpnDialog.request), each
    value checked as vpn.py checks it and passed as --option=VALUE (never read as an option). Keys and passwords are
    not among them: a secret names where sandbox-ai keeps one. ValueError says what is wrong."""
    if not (isinstance(name, str) and NAME.fullmatch(name)):
        raise ValueError(tr("A profile name is 1-32 lower-case letters, digits, '_' or '-'."))
    if kind not in FIELDS:
        raise ValueError(tr("Choose the kind of profile."))
    wanted = set(FIELDS[kind])
    unknown = [key for key, value in fields.items() if value and key in ALL_KIND_FIELDS and key not in wanted]
    if unknown:
        raise ValueError(tr("{option} does not fit this profile.", option=", ".join(sorted(unknown))))
    args = ["vpn", "add", name]
    if kind in ("wireguard", "openvpn"):
        file = os.path.expanduser(fields.get("file") or "")
        if not file.startswith("/") or "\0" in file or "\n" in file:
            raise ValueError(tr("The configuration is a file with its absolute path."))
        args.append(f"--{kind}={file}")
    else:
        args.append(f"--{kind}")
    secret = fields.get("secret")
    if secret:
        if not SECRET.fullmatch(secret):
            raise ValueError(tr("A secret's name is letters, digits and '_', not starting with a digit."))
        if kind not in SECRET_OPTIONS:
            raise ValueError(tr("A WireGuard configuration holds its own key: no secret."))
        args.append(f"{SECRET_OPTIONS[kind]}={secret}")
    for key, option, pattern in (("location", "--location", LOCATION), ("exit_node", "--exit-node", EXIT_NODE),
                                 ("login_server", "--login-server", LOGIN_SERVER), ("hostname", "--hostname", HOSTNAME),
                                 ("exit_country", "--exit-country", COUNTRIES), ("bridge_secret", "--bridge-secret",
                                                                                  SECRET)):
        value = fields.get(key)
        if value:
            if not pattern.fullmatch(value):
                raise ValueError(tr("{option} does not fit this profile: {value}", option=option,
                                    value=printable(value)))
            args.append(f"{option}={value}")
    if fields.get("bridges"):
        if fields.get("bridge_secret"):
            raise ValueError(tr("Bridges come from a file or from a secret, not both."))
        path = os.path.expanduser(fields["bridges"])
        if not path.startswith("/") or "\0" in path or "\n" in path:
            raise ValueError(tr("The bridge lines are a file with its absolute path (mode 0600)."))
        args.append(f"--bridges={path}")
    for network in words(fields.get("tailnet_allow")):
        try:
            args.append(f"--tailnet-allow={ipaddress.ip_network(network, strict=False)}")
        except ValueError:
            raise ValueError(tr("Not a network: {value}", value=printable(network))) from None
    ipv6 = fields.get("ipv6") or ""
    if ipv6 not in dict(IPV6):
        raise ValueError(tr("Choose how IPv6 goes."))
    if ipv6:
        args.append(f"--{ipv6}")
    for key, option in (("split", "--split"), ("ephemeral", "--ephemeral"), ("socks", "--socks")):
        if fields.get(key):
            args.append(option)
    # The advanced options of every kind.
    for value in words(fields.get("dns")):
        try:
            args.append(f"--dns={ipaddress.ip_address(value)}")
        except ValueError:
            raise ValueError(tr("Not an IP address: {value}", value=printable(value))) from None
    for url in words(fields.get("check_urls")):
        if not CHECK_URL.fullmatch(url):
            raise ValueError(tr("A check URL is https://HOST[/PATH]: {value}", value=printable(url)))
        args.append(f"--check-url={url}")
    if fields.get("no_dns_probe"):
        args.append("--no-dns-probe")
    provider = fields.get("provider")
    if provider:
        if not PROVIDER.fullmatch(provider):
            raise ValueError(tr("Not a provider's name: {value}", value=printable(provider)))
        args.append(f"--provider={provider}")
    if fields.get("force"):
        args.append("--force")
    return args


ALL_KIND_FIELDS = {field for fields in FIELDS.values() for field in fields} - {"file", "secret", "value"}


class VpnDialog(QDialog):
    """A new VPN profile: its name, its kind, what that kind needs and the advanced options. REMOTE: the files are
    paths on that host (typed; no file dialog of this machine)."""

    def __init__(self, parent=None, host_label="", remote=False, sai=None, host=LOCAL):
        super().__init__(parent)
        self.remote = remote
        self.setWindowTitle(tr("New VPN profile on {host}", host=host_label) if host_label else tr("New VPN profile"))
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
        self.browse = QPushButton(tr("File…"))
        self.browse.clicked.connect(lambda _=False: self.choose(self.file))
        file_row = self.with_browse(self.file, self.browse)
        # The secrets that host keeps, to choose from (names only; a name it lacks is typed, its value below)
        self.secret = NameField("secret", sai, host, placeholder=SECRET_EXAMPLE)
        self.value = QLineEdit()
        self.value.setPlaceholderText(tr("typed here: kept as that secret (or leave it empty if it is kept already)"))
        self.split = QCheckBox(tr("Only the server's routes through the tunnel (the rest stays blocked)"))
        self.location = QLineEdit()
        self.location.setPlaceholderText(LOCATION_EXAMPLE)
        self.ipv6 = QComboBox()
        for key, label in IPV6:
            self.ipv6.addItem(tr(label), key)
        self.exit_node = QLineEdit()
        self.exit_node.setPlaceholderText(EXIT_NODE_EXAMPLE)
        self.login_server = QLineEdit()
        self.login_server.setPlaceholderText(LOGIN_SERVER_EXAMPLE)
        self.hostname = QLineEdit()
        self.hostname.setPlaceholderText(tr("default: sandbox-ai-NAME"))
        self.ephemeral = QCheckBox(tr("A new ephemeral node per start (needs an auth key; parallel starts)"))
        self.tailnet_allow = QLineEdit()
        self.tailnet_allow.setPlaceholderText(CIDR_EXAMPLE)
        self.tailnet_allow.setToolTip(tr("What of the tailnet the agent may reach (default: the whole tailnet)"))
        self.bridges = QLineEdit()
        self.bridges.setPlaceholderText(tr("a private file (mode 0600) with obfs4 or plain bridge lines"))
        bridges_browse = QPushButton(tr("File…"))
        bridges_browse.clicked.connect(lambda _=False: self.choose(self.bridges))
        self.bridges_browse = bridges_browse
        bridges_row = self.with_browse(self.bridges, bridges_browse)
        self.bridge_secret = NameField("secret", sai, host, placeholder=BRIDGE_SECRET_EXAMPLE)
        self.exit_country = QLineEdit()
        self.exit_country.setPlaceholderText(COUNTRY_EXAMPLE)
        self.exit_country.setToolTip(tr("Tor exits only in these countries (two-letter codes)"))
        self.socks = QCheckBox(tr("A SOCKS port 127.0.0.1:9050 in the sandbox (Tor Browser, Playwright)"))
        self.socks.setToolTip(tr("What goes through it passes the connection proxy by"))
        self.rows = {"file": (tr("Configuration"), file_row), "secret": (tr("Secret"), self.secret),
                     "value": (tr("Its value"), widgets.secret_value(self.value)), "split": ("", self.split),
                     "location": (tr("Location"), self.location), "ipv6": (tr("IPv6"), self.ipv6),
                     "exit_node": (tr("Exit node"), self.exit_node), "login_server": (tr("Login server"), self.login_server),
                     "hostname": (tr("Node name"), self.hostname), "ephemeral": ("", self.ephemeral),
                     "tailnet_allow": (tr("Tailnet the agent may reach"), self.tailnet_allow),
                     "bridges": (tr("Bridges file"), bridges_row), "bridge_secret": (tr("Or a secret with bridges"),
                                                                                       self.bridge_secret),
                     "exit_country": (tr("Exit countries"), self.exit_country), "socks": ("", self.socks)}
        self.form.addRow(tr("Name"), self.name)
        self.form.addRow(tr("Kind"), self.kind)
        for label, widget in self.rows.values():
            self.form.addRow(label, widget)
        layout.addLayout(self.form)
        # The options every kind has, folded away.
        self.more = QToolButton()
        self.more.setText(tr("Advanced options"))
        disclosure(self.more)  # an arrow that opens, not a button that stays pressed
        self.more.setAccessibleName(tr("Advanced options"))
        layout.addWidget(self.more)
        self.advanced = QWidget()
        advanced = QFormLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.dns = QLineEdit()
        self.dns.setPlaceholderText(DNS_EXAMPLE)
        self.dns.setToolTip(tr("Resolvers to use instead of the configuration's"))
        self.check_urls = QLineEdit()
        self.check_urls.setPlaceholderText(CHECK_EXAMPLE)
        self.check_urls.setToolTip(tr("Address echo services for the checks (https); they replace the defaults"))
        self.no_dns_probe = QCheckBox(tr("Do not ask whoami.akamai.net where DNS leaves"))
        self.provider = QLineEdit()
        self.provider.setPlaceholderText(PROVIDER_EXAMPLE)
        self.provider.setToolTip(tr("The provider's name (mullvad enables its own check)"))
        self.force = QCheckBox(tr("Replace a profile of that name"))
        advanced.addRow(tr("Resolvers"), self.dns)
        advanced.addRow(tr("Check URLs"), self.check_urls)
        advanced.addRow("", self.no_dns_probe)
        advanced.addRow(tr("Provider"), self.provider)
        advanced.addRow("", self.force)
        self.advanced.setVisible(False)
        self.more.toggled.connect(self.advanced.setVisible)
        layout.addWidget(self.advanced)
        if remote:
            layout.addWidget(plain_label(tr("Files are paths on that host; a key typed here is kept as a secret of "
                                            "sandbox-ai there (standard input over SSH)."), role="secondary"))
            self.browse.setVisible(False)
            self.bridges_browse.setVisible(False)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self.show_fields)
        self.show_fields()

    @staticmethod
    def with_browse(edit, browse):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        layout.addWidget(browse)
        return row

    def show_fields(self):
        kind = self.kind.currentData()
        for field, (_, widget) in self.rows.items():
            self.form.setRowVisible(widget, field in FIELDS[kind])
        if kind in SECRET_LABELS:
            label = self.form.labelForField(self.secret)
            if label is not None:
                label.setText(tr(SECRET_LABELS[kind]))

    def choose(self, edit):
        from .state import choose_file
        path = choose_file(self, tr("VPN configuration"), "vpn")
        if path:
            edit.setText(path)

    def request(self):
        """(name, kind, secret value or None, fields for add_args): only what was given, for the kind chosen."""
        kind = self.kind.currentData()
        wanted = FIELDS[kind]
        texts = {"file": self.file, "secret": self.secret, "location": self.location, "exit_node": self.exit_node,
                 "login_server": self.login_server, "hostname": self.hostname, "tailnet_allow": self.tailnet_allow,
                 "bridges": self.bridges, "bridge_secret": self.bridge_secret, "exit_country": self.exit_country}
        fields = {key: edit.text().strip() for key, edit in texts.items() if key in wanted and edit.text().strip()}
        fields.update({key: True for key, box in (("split", self.split), ("ephemeral", self.ephemeral),
                                                    ("socks", self.socks)) if key in wanted and box.isChecked()})
        if "ipv6" in wanted and self.ipv6.currentData():
            fields["ipv6"] = self.ipv6.currentData()
        fields.update({key: edit.text().strip() for key, edit in (("dns", self.dns), ("check_urls", self.check_urls),
                                                                  ("provider", self.provider)) if edit.text().strip()})
        fields.update({key: True for key, box in (("no_dns_probe", self.no_dns_probe), ("force", self.force))
                       if box.isChecked()})
        value = self.value.text() if "value" in wanted and self.value.text() else None
        return self.name.text().strip(), kind, value, fields


def add_profile(parent, sai, done=None, host=LOCAL, host_label=""):
    """Ask for a new profile on HOST and add it in the background; DONE(name or None) afterwards. A typed key is kept
    as its secret there after the profile was added (Mullvad's before: it is read while added), on standard input; a
    secret of that name is replaced only after a yes, a SOCKS port only after one (vpn-socks)."""
    dialog = VpnDialog(parent, host_label, remote=host != LOCAL, sai=sai, host=host)
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
        args = add_args(name, kind, fields)  # checked before anything is kept
        if value and not fields.get("secret"):
            raise ValueError(tr("A typed key or password needs the name of the secret to keep it as."))
    except ValueError as exc:
        QMessageBox.warning(parent, tr("New VPN profile"), shown_as_text(printable(exc)))
        return
    if fields.get("socks") and not ask_first(parent, "vpn-socks", tr("New VPN profile"), on_host(host, tr(
            "A SOCKS port at 127.0.0.1:9050 in every sandbox with {name}: what goes through it passes the connection "
            "proxy by (no allow-list, no questions, no log of its requests). Add it?", name=name)), tr("Add")):
        return
    # --force: a profile of that name changes the tunnel of every start that uses it - asked first (vpn-replace).
    if fields.get("force") and not ask_first(parent, "vpn-replace", tr("New VPN profile"), on_host(host, tr(
            "Replace the VPN profile {name} if there is one? Every start that uses it gets the new tunnel.",
            name=name)), tr("Replace")):
        return

    def added(result, error):
        result, problem = result if error is None else (None, None)
        status, out, err = outcome(result, error)
        # (a background job's outcome: shown without waiting for its OK - widgets.inform)
        if status != 0:
            widgets.warn(parent, tr("New VPN profile"), printable((err or out).strip()[-1500:]))
        elif problem is not None:
            widgets.warn(parent, tr("New VPN profile"), tr(
                "{name} added, but its key was not kept as the secret {secret}: {error}", name=name,
                secret=fields.get("secret") or "", error=printable(problem)[:300]))
        if done:
            done(name if status == 0 else None)

    def work():
        """`sandbox-ai vpn add` first (it checks every field), then the typed key as its secret: a profile refused
        keeps no key. Only Mullvad reads its account number while it is added: that one is kept first."""
        first = bool(value) and kind == "mullvad"
        if first:
            kept = sai.set_secret(fields["secret"], value, host=host)
            if kept[0] != 0:
                return kept, None
        status, out, err = sai.host_run(host, *args, timeout=ADD_SECONDS)
        if status != 0 or not value or first:
            return (status, out, err), None
        kept = sai.set_secret(fields["secret"], value, host=host)
        return (status, out, err), None if kept[0] == 0 else (kept[2] or kept[1]).strip() or "?"

    def go():
        background(work, owner=parent, work=True, timeout=ADD_SECONDS + 90, done=added)
    if not value:
        go()
        return
    # A typed key replaces the value of a secret of that name (for everything that names it): asked first.
    secret = fields["secret"]

    def looked(exists, error):
        exists = exists if error is None else None
        text = tr("A secret {name} is kept already: the key typed replaces its value, for everything that names it. "
                  "Replace it?", name=printable(secret)) if exists else \
            tr("Whether a secret {name} is kept already could not be told: the key typed would replace its value. "
               "Go on?", name=printable(secret))
        if ask_first(parent, "secret-replace", tr("New VPN profile"), on_host(host, text), tr("Replace"),
                     detail={"exists": exists}):
            go()
    background(lambda: sai.secret_exists(secret, host), owner=parent, timeout=90, done=looked)


class VpnBox(QGroupBox):
    """The VPN profiles of sandbox-ai vpn on the host chosen (the Credentials page's), with Add, Show, Check and
    Remove."""

    def __init__(self, sai):
        super().__init__(tr("VPN profiles (sandbox-ai vpn)"))
        self.sai, self.entries, self.on_change, self.host, self.token = sai, [], None, LOCAL, 0
        self.host_label = lambda host: tr("{name} (this machine)", name=LOCAL) if host == LOCAL else printable(host)
        layout = QVBoxLayout(self)
        self.profiles = table([tr("Profile"), tr("Kind"), tr("Provider")], stretch=0, compact=(1,))
        layout.addWidget(self.profiles)
        row = QHBoxLayout()
        self.add_button, self.show_button, self.check_button, self.remove_button = \
            QPushButton(tr("Add…")), QPushButton(tr("Show")), QPushButton(tr("Check")), QPushButton(tr("Remove…"))
        self.show_button.setToolTip(tr("sandbox-ai vpn show: what the profile holds (no keys)"))
        self.add_button.clicked.connect(lambda _=False: add_profile(self, self.sai, lambda name: self.refresh(),
                                                                    host=self.host,
                                                                    host_label=self.host_label(self.host)))
        self.show_button.clicked.connect(lambda _=False: self.show_profile())
        self.check_button.clicked.connect(lambda _=False: self.check())
        self.remove_button.clicked.connect(lambda _=False: self.remove())
        for widget in (self.add_button, self.show_button, self.check_button, self.remove_button):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        self.message = plain_label()
        layout.addWidget(self.message)
        # several at once (Ctrl, Shift): Remove… and the Delete key take them all; Show and Check one
        multi_select(self.profiles)
        delete_key(self.profiles, self.remove)
        one_only(self.profiles, [self.show_button, self.check_button])

    def set_host(self, host):
        """Show HOST's profiles from now on (the Credentials page's host chooser)."""
        if host != self.host:
            self.host, self.entries = host, []
            fill(self.profiles, [])
            self.refresh()

    def refresh(self):
        host = self.host
        self.token += 1
        token = self.token
        if host == LOCAL:
            background(self.sai.vpn_profiles, owner=self, timeout=LIST_SECONDS,
                       done=lambda result, error: self.shown(host, token, result, error))
        else:
            background(lambda: self.sai.host_vpn_profiles(host), owner=self, timeout=LIST_SECONDS,
                       done=lambda result, error: self.shown(host, token, result, error))

    def shown(self, host, token, result, error=None):
        if token != self.token or host != self.host:
            return
        if error is not None:
            self.message.setText(tr("{host}: no answer", host=self.host_label(host)) + f" ({printable(error)[:300]})")
            self.entries = []
            fill(self.profiles, [])
        else:
            self.entries = [e for e in result or [] if isinstance(e, dict)]
            fill(self.profiles, [[e.get("profile", ""), e.get("type", ""), e.get("provider", "")] for e in self.entries])
            # what every field that takes a profile's name offers for that host from now on (one added or removed)
            publish(self.sai, "vpn", host, [e["profile"] for e in self.entries if isinstance(e.get("profile"), str)])
            if host != LOCAL:
                self.message.setText(tr("VPN profiles on {host}: {count}", host=self.host_label(host),
                                        count=len(self.entries)))
        if self.on_change is not None:
            self.on_change()

    def chosen(self):
        name = selected(self.profiles)
        return name if isinstance(name, str) and NAME.fullmatch(name) else None

    def check(self):
        name, host = self.chosen(), self.host
        if not name:
            return
        self.message.setText(tr("Checking {name}…", name=name))

        def work():
            if host == LOCAL:
                return self.sai.vpn_check(name)
            return self.sai.host_run(host, "vpn", "check", name, timeout=CHECK_SECONDS)
        background(work, owner=self, work=True, timeout=CHECK_SECONDS + 30,
                   done=lambda result, error: self.message.setText(printable("\n".join(
                       part for part in outcome(result, error)[1:] if part).strip()[-1500:])))

    def show_profile(self):
        name, host = self.chosen(), self.host
        if not name:
            return
        background(lambda: self.sai.host_run(host, "vpn", "show", name, timeout=SHOW_SECONDS), owner=self,
                   timeout=SHOW_SECONDS + 15, done=lambda result, error: self.profile_shown(host, name, result, error))

    def profile_shown(self, host, name, result, error):
        status, out, err = outcome(result, error)
        text = "\n".join(printable(line) for line in ((out if status == 0 else err or out) or "").strip()[-8000:]
                         .splitlines())
        show_text(self, tr("VPN profile {name} on {host}", name=name, host=self.host_label(host)),
                  text or tr("exit status {status}", status=status), modal=False)

    def remove_several(self, names):
        """Remove… with several profiles selected: one question naming them, then each (vpn remove NAME)."""
        host = self.host
        text = several(tr("Remove these {count} VPN profiles?", count=len(names)), names)
        if not ask_first(self, "vpn-remove", tr("Remove"), text if host == LOCAL else
                         tr("On {host}: {text}", host=printable(host), text=text), tr("Remove")):
            return

        def work(name):
            if host == LOCAL:
                return self.sai.vpn_remove(name)
            return self.sai.host_run(host, "vpn", "remove", name, timeout=REMOVE_SECONDS)

        def done(outcomes):
            failures = report_each(self, tr("Remove"), outcomes)
            self.message.setText(tr("Removed {count} of {total}.", count=len(names) - len(failures), total=len(names)))
            self.refresh()
        run_each(work, names, owner=self, source=self.remove_button, timeout=REMOVE_SECONDS * len(names), done=done)

    def remove(self):
        if widgets.held(self.remove_button):  # (the Delete key while one runs: it would report false failures)
            self.message.setText(tr("A removal is running: wait for it."))
            return
        names = [name for name in selected_keys(self.profiles) if isinstance(name, str) and NAME.fullmatch(name)]
        if len(names) > 1:
            self.remove_several(names)
            return
        name, host = self.chosen(), self.host
        if not name or not ask_first(self, "vpn-remove", tr("Remove"), tr("Remove the VPN profile {name}?",
                                                                          name=printable(name)) if host == LOCAL else
                                     tr("On {host}: {text}", host=printable(host), text=tr(
                                         "Remove the VPN profile {name}?", name=printable(name))), tr("Remove")):
            return

        def work():
            if host == LOCAL:
                return self.sai.vpn_remove(name)
            return self.sai.host_run(host, "vpn", "remove", name, timeout=REMOVE_SECONDS)
        background(work, owner=self, work=True, timeout=REMOVE_SECONDS, source=self.remove_button,
                   done=lambda *_: self.refresh())
