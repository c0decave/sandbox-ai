"""VPN profiles in the app: the arguments it passes, the dialog per kind, and a real profile added to and removed
from sandbox-ai (a WireGuard configuration with a real key; the key goes to sandbox-ai's secrets, never the app)."""
import json
import os
from pathlib import Path

import pytest

from sandbox_ai_app import backend  # noqa: E402

PEER = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
CONFIG = """[Interface]
PrivateKey = {key}
Address = 10.66.1.2/32
DNS = 10.66.0.1
[Peer]
PublicKey = {peer}
AllowedIPs = 0.0.0.0/0
Endpoint = vpn.example.org:51820
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def test_the_arguments_of_each_kind_are_checked(env):
    sai = backend.SandboxAI()
    assert sai.vpn_add_argv("home", "wireguard", file="/etc/wg/home.conf") == \
        ["vpn", "add", "home", "--wireguard=/etc/wg/home.conf"]
    assert sai.vpn_add_argv("work", "openvpn", file="/x/w.ovpn", secret="WORK_VPN") == \
        ["vpn", "add", "work", "--openvpn=/x/w.ovpn", "--auth-secret=WORK_VPN"]
    assert sai.vpn_add_argv("mv", "mullvad", secret="MULLVAD", location="se-sto") == \
        ["vpn", "add", "mv", "--mullvad", "--account-secret=MULLVAD", "--location=se-sto"]
    assert sai.vpn_add_argv("ts", "tailscale", secret="TS_KEY", exit_node="exit-1",
                            login_server="https://hs.example.org") == \
        ["vpn", "add", "ts", "--tailscale", "--auth-key-secret=TS_KEY", "--exit-node=exit-1",
         "--login-server=https://hs.example.org"]
    for args, text in ((("Home", "wireguard"), "profile name"), (("-x", "wireguard"), "profile name"),
                       (("a", "ipsec"), "the kind"), (("a", "wireguard"), "absolute path"),
                       (("a", "wireguard", "rel.conf"), "absolute path"),
                       (("a", "wireguard", "/x.conf", "KEY"), "holds its own key"),
                       (("a", "mullvad", None, "bad name"), "secret's name"),
                       (("a", "tailscale", None, None, "se"), "--location does not fit"),
                       (("a", "mullvad", None, None, "-x"), "--location does not fit"),
                       (("a", "tailscale", None, None, None, None, "http://hs"), "--login-server")):
        with pytest.raises(ValueError, match=text):
            sai.vpn_add_argv(*args)


def test_a_typed_key_is_kept_as_a_secret_first_never_on_a_command_line(env, monkeypatch):
    sai = backend.SandboxAI()
    calls = []
    monkeypatch.setattr(sai, "call", lambda *args, timeout=120, input=None: calls.append((args, input)) or (0, "", ""))
    assert sai.vpn_add("ts", "tailscale", secret_value="tskey-auth-abc123", secret="TS_KEY")[0] == 0
    (secret_args, secret_input), (add_args, add_input) = calls
    assert secret_args == ("secrets", "set", "TS_KEY") and secret_input == "tskey-auth-abc123\n"
    assert add_args == ("vpn", "add", "ts", "--tailscale", "--auth-key-secret=TS_KEY") and add_input is None
    assert "tskey-auth-abc123" not in json.dumps(add_args)
    with pytest.raises(ValueError, match="needs the name of the secret"):
        sai.vpn_add("ts", "tailscale", secret_value="tskey-x")
    calls.clear()
    monkeypatch.setattr(sai, "set_secret", lambda name, value: (1, "", "no keyring"))
    assert sai.vpn_add("ts", "tailscale", secret_value="tskey-x", secret="TS_KEY") == (1, "", "no keyring")
    assert calls == [], "no profile without its secret"


def test_the_dialog_asks_what_each_kind_needs(qapp, env):
    from sandbox_ai_app.vpn_page import VpnDialog
    dialog = VpnDialog()
    shown = lambda widget: dialog.form.isRowVisible(widget)  # noqa: E731
    dialog.kind.setCurrentIndex(dialog.kind.findData("wireguard"))
    assert shown(dialog.rows["file"][1]) and not shown(dialog.secret) and not shown(dialog.value)
    dialog.kind.setCurrentIndex(dialog.kind.findData("tailscale"))
    assert not shown(dialog.rows["file"][1]) and shown(dialog.secret) and shown(dialog.exit_node)
    assert dialog.form.labelForField(dialog.secret).text().startswith("Secret for the auth key")
    dialog.name.setText("ts")
    dialog.secret.setText("TS_KEY")
    dialog.value.setText("tskey-auth-1")
    dialog.exit_node.setText("exit-1")
    dialog.file.setText("/ignored/for/tailscale")
    assert dialog.request() == ("ts", "tailscale", "tskey-auth-1", {"secret": "TS_KEY", "exit_node": "exit-1"})
    assert dialog.value.echoMode() == dialog.value.EchoMode.Password
    dialog.deleteLater()


def test_a_real_wireguard_profile_is_added_listed_and_removed(env):
    """End to end against sandbox-ai itself (SANDBOX_AI_HOME): the app's call adds the profile, `vpn list` shows
    it without its key, the key is kept in sandbox-ai's secrets, and remove takes it away."""
    sai = backend.SandboxAI()
    if not hasattr(sai, "vpn_add"):
        pytest.skip("no vpn support")
    key = sai.module("vpn").generate_private_key()
    source = env / "home.conf"
    source.write_text(CONFIG.format(key=key, peer=PEER))
    source.chmod(0o600)
    status, out, err = sai.vpn_add("home", "wireguard", file=str(source))
    assert status == 0, err or out
    profiles = sai.vpn_profiles()
    assert [p["profile"] for p in profiles] == ["home"] and profiles[0].get("type") == "wireguard"
    assert key not in json.dumps(profiles), "a listing never holds a key"
    stored = [p for p in (env / "config").rglob("*") if p.is_file()] + [p for p in (env / "data").rglob("*") if p.is_file()]
    assert stored and not any(key in p.read_text(errors="replace") for p in stored if "secret" not in str(p)), \
        "the key lives only in the secrets"
    status, out, err = sai.vpn_remove("home")
    assert status == 0, err or out
    assert sai.vpn_profiles() == []


def test_the_credentials_page_shows_the_profiles(qapp, env, monkeypatch, wait):
    from sandbox_ai_app.vpn_page import VpnBox
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "vpn_profiles", lambda: [{"profile": "home", "type": "wireguard", "provider": "proton"}])
    box = VpnBox(sai)
    box.refresh()
    assert wait(lambda: box.profiles.rowCount() == 1)
    assert [box.profiles.item(0, c).text() for c in range(3)] == ["home", "wireguard", "proton"]
    box.deleteLater()


def test_a_typed_key_does_not_stay_behind_in_the_dialog(qapp, env, monkeypatch, wait):
    """Regression: the dialog of a new profile lived on as a child of the Credentials page after Add, holding the
    typed account number or auth key in its field until the window closed. The value is passed on, then cleared."""
    from PySide6.QtWidgets import QDialog, QLineEdit, QWidget
    from sandbox_ai_app import vpn_page
    sai = backend.SandboxAI()
    calls = []
    monkeypatch.setattr(sai, "call", lambda *args, timeout=120, input=None: calls.append((args, input)) or (0, "", ""))
    parent = QWidget()
    dialogs = []

    def run(dialog, cancel=False):
        dialogs.append(dialog)
        dialog.name.setText("ts")
        dialog.kind.setCurrentIndex(dialog.kind.findData("tailscale"))
        dialog.secret.setText("TS_KEY")
        dialog.value.setText("tskey-auth-kept-nowhere")
        return QDialog.DialogCode.Rejected if cancel else QDialog.DialogCode.Accepted
    names = []
    for cancel in (False, True):
        monkeypatch.setattr(vpn_page.VpnDialog, "exec", lambda dialog, c=cancel: run(dialog, c))
        vpn_page.add_profile(parent, sai, names.append)
    assert wait(lambda: names == ["ts"])
    assert calls[0] == (("secrets", "set", "TS_KEY"), "tskey-auth-kept-nowhere\n")
    assert [d.value.text() for d in dialogs] == ["", ""], "added or cancelled: the typed key is gone"
    assert not any(e.text() == "tskey-auth-kept-nowhere" for e in parent.findChildren(QLineEdit))
    parent.deleteLater()


def test_removing_a_profile_asks_with_cancel_as_the_default(qapp, env, monkeypatch, wait):
    """Regression: Remove asked with QMessageBox.question, whose default is Yes: Enter removed the profile."""
    from sandbox_ai_app import widgets
    from sandbox_ai_app.vpn_page import VpnBox
    sai = backend.SandboxAI()
    removed = []
    monkeypatch.setattr(sai, "vpn_profiles", lambda: [{"profile": "home", "type": "wireguard", "provider": ""}])
    monkeypatch.setattr(sai, "vpn_remove", lambda name: removed.append(name) or (0, "", ""))
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # Cancel
    box = VpnBox(sai)
    box.refresh()
    assert wait(lambda: box.profiles.rowCount() == 1)
    box.profiles.selectRow(0)
    box.remove()
    assert widgets.SHOWN[-1][:2] == ("Remove", "Remove the VPN profile home?") and removed == []
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    box.remove()
    assert wait(lambda: removed == ["home"])
    box.deleteLater()
