"""Every field that takes the NAME of a stored secret or of a VPN profile offers the chosen host's names (name_fields):
an editable combo box (or a "+" menu beside a field of one value per line) filled in the background from that host's
`secrets list` (names only, never a value) or `vpn list --json`; a name the host lacks is typed and kept; another host
chosen reads its names (a late answer for the one before is dropped); a host that cannot list them leaves the field
typed in; a list read by a page (the Credentials page, the VPN box) reaches every field at once."""
import os
import threading

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import backend, name_fields as NF, widgets  # noqa: E402

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

HOSTS = {
    "local": {"secrets": [("VPN_HOME", "keyring"), ("TS_KEY", "file"), ("bad name", "file")],
              "vpn": [{"profile": "home", "type": "wireguard"}, {"profile": "tor", "type": "tor"}, {"x": 1}]},
    "ct1": {"secrets": [("REMOTE_KEY", "file")], "vpn": [{"profile": "far", "type": "tailscale"}]},
    # an older host: no `vpn list --json`
    "old": {"secrets": [("OLD_KEY", "file")], "vpn": ValueError("sandbox-ai vpn list: unrecognized arguments: --json")},
}


class FakeSai:
    """The app's backend as far as the fields use it: the names of a host's secrets and VPN profiles. Whatever
    would read a secret's value fails the test."""

    def __init__(self, hosts=HOSTS):
        self.hosts, self.calls, self.gates = hosts, [], {}

    def answer(self, host, kind):
        self.calls.append((kind, host))
        gate = self.gates.get(host)
        if gate is not None:
            gate.wait(10)
        value = self.hosts[host][kind]
        if isinstance(value, Exception):
            raise value
        return value

    def host_secrets(self, host):
        return "file", list(self.answer(host, "secrets"))

    def host_vpn_profiles(self, host):
        return list(self.answer(host, "vpn"))

    def vpn_profiles(self):
        return self.host_vpn_profiles("local")

    # what reads or changes a value: never called by a field
    def secret_value(self, *args, **kwargs):
        raise AssertionError("a secret's value was asked for")

    get_secret = set_secret = delete_secret = secret_value

    def valid_secret(self, name, value=None):
        return True


@pytest.fixture(autouse=True)
def fresh():
    NF._kept.clear()
    NF._waiting.clear()
    yield
    NF._kept.clear()
    NF._waiting.clear()


@pytest.fixture
def values_never_read(monkeypatch):
    """sandbox-ai's own secret store: reading a value fails the test (the names are all a field may read)."""
    sai = backend.SandboxAI()
    store = sai.module("secret_store")

    def refuse(*args, **kwargs):
        raise AssertionError("a secret's value was read")
    monkeypatch.setattr(store, "get", refuse)
    monkeypatch.setattr(store.FileStore, "get", refuse)
    monkeypatch.setattr(backend.SandboxAI, "set_secret", refuse)
    monkeypatch.setattr(backend.SandboxAI, "delete_secret", refuse)
    return sai


def offered(field):
    return field.offered() if isinstance(field, NF.NameField) else [a.text() for a in field.menu().actions()]


def test_a_field_offers_the_hosts_names_takes_a_new_one_and_follows_the_host(qapp, wait):
    sai = FakeSai()
    field = NF.NameField("secret", sai, "local", placeholder="GH_PUSH")
    assert field.isEditable() and field.placeholderText() == "GH_PUSH"
    assert wait(lambda: field.offered() == ["TS_KEY", "VPN_HOME"]), "valid names only, sorted"
    assert "2 on this computer" in field.toolTip()
    QTest.keyClicks(field.lineEdit(), "NEW_ONE")
    assert field.text() == "NEW_ONE" and field.offered() == ["TS_KEY", "VPN_HOME"], "typed, not added to the list"
    field.setCurrentIndex(2)
    assert field.text() == "VPN_HOME"
    field.setText("NOT_THERE_YET")
    field.set_host("ct1")
    assert field.offered() == [], "this machine's names are not offered for another host, not even meanwhile"
    assert wait(lambda: field.offered() == ["REMOTE_KEY"]) and field.text() == "NOT_THERE_YET"
    assert "1 on ct1" in field.toolTip()
    assert sai.calls == [("secrets", "local"), ("secrets", "ct1")], "names only, each host once"


def test_a_late_answer_for_the_host_chosen_before_is_dropped(qapp, wait):
    sai = FakeSai()
    sai.gates["local"] = gate = threading.Event()
    field = NF.NameField("vpn", sai, "local")
    field.set_host("ct1")
    assert wait(lambda: field.offered() == ["far"])
    gate.set()
    assert wait(lambda: not NF._waiting)
    for _ in range(20):
        qapp.processEvents()
    assert field.offered() == ["far"] and field.host == "ct1"


def test_an_older_host_without_vpn_list_json_leaves_the_field_typed_in(qapp, wait):
    field = NF.NameField("vpn", FakeSai(), "old")
    assert wait(lambda: not field.reading)
    assert field.offered() == [] and "Could not be read" in field.toolTip() and "--json" in field.toolTip()
    QTest.keyClicks(field.lineEdit(), "home")
    assert field.text() == "home"


def test_one_read_for_every_field_and_a_listed_change_reaches_them_all(qapp, wait):
    sai = FakeSai()
    first, second = NF.NameField("secret", sai, "local"), NF.NameField("secret", sai, "local")
    assert wait(lambda: first.offered() and second.offered())
    assert sai.calls == [("secrets", "local")], "one read shared by the fields waiting for it"
    NF.publish(sai, "secret", "local", ["A_NEW_ONE", "TS_KEY", "VPN_HOME"])  # the Credentials page listed them
    assert first.offered() == second.offered() == ["A_NEW_ONE", "TS_KEY", "VPN_HOME"]
    assert sai.calls == [("secrets", "local")], "taken as listed, not read again"
    NF.forget("secret", "local")  # one was removed (nobody listed them since): read again
    assert wait(lambda: first.offered() == ["TS_KEY", "VPN_HOME"] == second.offered())
    assert len(sai.calls) == 2
    other = NF.NameField("secret", sai, "ct1")
    NF.publish(sai, "secret", "local", ["ONLY_HERE"])
    assert wait(lambda: other.offered() == ["REMOTE_KEY"]) and first.offered() == ["ONLY_HERE"], "per host"


def test_a_plus_menu_adds_a_name_to_a_field_of_lines(qapp, wait):
    button = NF.NameButton("secret", FakeSai(), "ct1")
    assert wait(lambda: offered(button) == ["REMOTE_KEY"])
    picked = []
    button.picked.connect(picked.append)
    button.menu().actions()[0].trigger()
    assert picked == ["REMOTE_KEY"] and "ct1" in button.toolTip()
    assert NF.add_line("", "GH", "--secret-env") == "GH"
    assert NF.add_line("api.openai.com=", "OPENAI", "--api-key") == "api.openai.com=OPENAI", "a HOST= line completed"
    assert NF.add_line("GH", "OPENAI", "--api-key") == "GH\nHOST=OPENAI", "HOST to be typed over"
    assert NF.add_line("", "GH", "--git-token-secret") == "https://HOST=GH"
    assert NF.add_line("GH", "GH", "--secret-env") == "GH", "once"


# -- the fields of the pages and dialogs --


class Parent(QtWidgets.QWidget):
    def __init__(self, sai, host="local"):
        super().__init__()
        self.sai, self.host = sai, host


def test_the_vpn_dialog_offers_the_hosts_secrets(qapp, wait):
    from sandbox_ai_app.vpn_page import VpnDialog
    sai = FakeSai()
    dialog = VpnDialog(sai=sai, host="ct1", remote=True)
    for field in (dialog.secret, dialog.bridge_secret):
        assert isinstance(field, NF.NameField)
        assert wait(lambda f=field: f.offered() == ["REMOTE_KEY"])
    dialog.kind.setCurrentIndex(dialog.kind.findData("tailscale"))
    dialog.secret.setText("TS_NEW")
    assert dialog.request()[3]["secret"] == "TS_NEW", "a name it lacks yet is taken (its value typed below)"
    assert {kind for kind, _ in sai.calls} == {"secrets"} and {host for _, host in sai.calls} == {"ct1"}


def test_the_vpn_box_hands_its_profiles_to_every_field(qapp, wait):
    from sandbox_ai_app.vpn_page import VpnBox
    sai = FakeSai()
    field = NF.NameField("vpn", sai, "local")
    assert wait(lambda: field.offered() == ["home", "tor"])
    box = VpnBox(sai)
    box.shown("local", box.token, [{"profile": "home"}, {"profile": "added"}])
    assert field.offered() == ["added", "home"]


def test_the_llm_dialog_offers_vpn_profiles_and_secrets_of_its_host(qapp, wait):
    from sandbox_ai_app.llm_page import LlmDialog
    sai = FakeSai()
    dialog = LlmDialog(host_label="ct1", remote=True, sai=sai, host="ct1")
    assert wait(lambda: dialog.vpn.offered() == ["far"] and dialog.secret.offered() == ["REMOTE_KEY"])
    assert dialog.vpn.accessibleName() == "VPN profile"
    dialog.prefill({"name": "gpu", "kind": "vllm", "url": "http://100.64.0.2:8000", "vpn": "far", "secret": "NEW_KEY"})
    fields, value = dialog.request()
    assert fields["vpn"] == "far" and fields["secret"] == "NEW_KEY" and value is None
    old = LlmDialog(host_label="old", remote=True, sai=sai, host="old")
    assert wait(lambda: not old.vpn.reading and old.secret.offered() == ["OLD_KEY"])
    assert old.vpn.offered() == [] and "Could not be read" in old.vpn.toolTip()


def test_the_mcp_dialogs_offer_the_hosts_vpn_profiles(qapp, wait):
    from sandbox_ai_app import ui
    sai = FakeSai()
    parent = Parent(sai)  # kept: the dialogs are its children
    start = ui.McpStartDialog(parent, "fetch", [], "ct1")
    add = ui.AddServerDialog(parent, {"name": "fetch"}, "local")
    assert wait(lambda: start.vpn.offered() == ["far"] and add.vpn.offered() == ["home", "tor"])
    start.vpn.setText("typed")
    assert start.values()["vpn"] == "typed" and add.values("fetch")["vpn"] is None


def test_the_login_dialog_offers_the_stored_secrets_and_takes_one_typed(qapp, wait):
    from sandbox_ai_app.credentials_page import LoginDialog
    sai = FakeSai()
    parent = Parent(sai)
    dialog = LoginDialog(parent, "New Git login", ["KNOWN"], [("Server", "url", "https://HOST")], host="ct1")
    assert dialog.stored.isEnabled(), "a name can be typed even where none is known"
    dialog.stored.setChecked(True)
    assert dialog.secret_choice.offered() == ["KNOWN"] and dialog.values()["secret"] == "KNOWN", "the first, as before"
    NF.publish(sai, "secret", "ct1", ["KNOWN", "REMOTE_KEY"])  # the page listed them again (one was added)
    assert dialog.secret_choice.offered() == ["KNOWN", "REMOTE_KEY"]
    assert sai.calls == [], "the page's list: not read again for the dialog"
    dialog.secret_choice.setText("NOT_YET")
    assert dialog.values()["secret"] == "NOT_YET" and dialog.values()["value"] is None


def test_the_identity_dialog_offers_the_hosts_secrets(qapp, wait):
    from sandbox_ai_app.identities_page import IdentityDialog
    sai = FakeSai()
    identity = {"git": [{"url": "https://github.com", "user": "x", "secret": "GH_WORK"}], "publish": []}
    parent = Parent(sai, "ct1")
    dialog = IdentityDialog(parent, "work", identity, ["GH_WORK"], {"work"})
    cell = dialog.git.cellWidget(0, 2)
    assert isinstance(cell, NF.NameField) and cell.text() == "GH_WORK" and cell.offered() == ["GH_WORK"]
    dialog.add_publish("pypi", "", "")
    publish = dialog.publish.cellWidget(0, 1)
    NF.forget("secret", "ct1")  # one was added on that host: read again
    assert wait(lambda: cell.offered() == ["REMOTE_KEY"] == publish.offered())
    assert sai.calls == [("secrets", "ct1")], "once for both"

    publish.setText("PYPI_TOKEN")
    assert dialog.result_identity()[1]["publish"] == [{"kind": "pypi", "secret": "PYPI_TOKEN"}]


@needs_sai
def test_the_wizards_server_dialog_offers_this_machines_secrets(qapp, wait, values_never_read, monkeypatch):
    from sandbox_ai_app.wizard import ServerDialog
    sai = values_never_read
    monkeypatch.setattr(sai, "host_secrets", lambda host: ("file", [("LMSTUDIO_KEY", "file")]))
    monkeypatch.setattr(sai, "vpn_profiles", lambda: [])
    dialog = ServerDialog(sai, ())
    assert wait(lambda: dialog.secret.offered() == ["LMSTUDIO_KEY"])
    assert dialog.request()[0]["secret"] == ""


@needs_sai
def test_a_sandbox_ai_presets_secret_value_offers_the_hosts_secrets(qapp, wait, values_never_read, monkeypatch):
    from sandbox_ai_app import presets as PR
    from sandbox_ai_app.preset_gallery import PlaceholderForm
    sai = values_never_read
    monkeypatch.setattr(sai, "host_secrets", lambda host: ("file", [(f"{host.upper()}_KEY", "file")]))
    preset = PR.from_sandbox_ai(sai, {"id": "keyed", "title": "Keyed", "description": "",
                                      "values": {"key": {"kind": "secret", "label": "API key"}},
                                      "options": ["--secret-env", "{key}"]}, "ct1")
    assert [p.names for p in preset.placeholders] == ["secret"]
    form = PlaceholderForm(sai, preset, host="ct1")
    field = form.fields[preset.placeholders[0].marker]
    assert isinstance(field, NF.NameField) and wait(lambda: field.offered() == ["CT1_KEY"])
    field.setText("TYPED")
    assert list(form.values().values()) == ["TYPED"]


@needs_sai
def test_the_start_pages_secret_options_offer_the_hosts_secrets(qapp, values_never_read, monkeypatch):
    sai = values_never_read
    read = []
    monkeypatch.setattr(sai, "host_secrets", lambda host: read.append(host) or ("file", [("GH_PUSH", "file")]))
    found, problems = sai.host_start_choices("ct1")
    assert found["--secret-env"] == found["--api-key"] == found["--git-token-secret"] == ["GH_PUSH"]
    assert read == ["ct1"], "the secrets' names read once for the three options"
    monkeypatch.setattr(sai, "host_secrets", lambda host: (_ for _ in ()).throw(ValueError("no answer")))
    found, problems = sai.host_start_choices("ct1")
    assert all("no answer" in problems[o] for o in ("--secret-env", "--api-key", "--git-token-secret"))
    from sandbox_ai_app import ui
    assert {"--secret-env", "--api-key", "--git-token-secret"} <= set(ui.OptionsForm.HOST_CHOICES)
    for option, typed, wanted in (("--secret-env", "", ["GH_PUSH"]), ("--api-key", "api.example.org=", ["api.example.org=GH_PUSH"]),
                                  ("--git-token-secret", "", ["https://HOST=GH_PUSH"])):
        field = ui.ListField(choices=True, option=option)
        field.edit.setPlainText(typed)
        field.set_choices(["GH_PUSH"], "ct1")
        field.pick.menu().actions()[0].trigger()
        assert field.value() == wanted, option


@needs_sai
def test_the_board_offers_vpn_profiles_and_secrets_of_its_host(qapp, wait, values_never_read, monkeypatch, tmp_path):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.board_view import BoardPage
    sai = values_never_read
    monkeypatch.setattr(sai, "host_secrets", lambda host: ("file", [("OPENAI_KEY", "file")]))
    monkeypatch.setattr(sai, "host_vpn_profiles", lambda host: [{"profile": "home"}])
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    page.add_node("vpn")
    page.inspect(page.board.first("vpn"))

    def field():  # the inspector is built again after a change: the field of now
        return next((f for f in page.inspector_holder.findChildren(NF.NameField) if f.kind == "vpn"), None)
    assert wait(lambda: field() is not None and field().offered() == ["home"])
    vpn = field()
    vpn.setText("home")
    vpn.editingFinished.emit()
    assert page.board.first("vpn").props["--vpn"] == "home"
    page.inspected = page.board.first("agent").id  # as when it is selected (the inspector is built again for it)
    page.inspect(page.board.first("agent"))

    def buttons():
        return [b for b in page.inspector_holder.findChildren(NF.NameButton) if b.kind == "secret"]
    assert wait(lambda: buttons() and all(offered(b) == ["OPENAI_KEY"] for b in buttons()))
    from sandbox_ai_app import guide_view
    guide = guide_view.NodeGuide(page.board.first("vpn"), (), page)
    question, field = guide.inputs["--vpn"]
    assert isinstance(field, NF.NameField) and wait(lambda: field.offered() == ["home"])
    assert guide.answers()["--vpn"] == "home"
    page.deleteLater()
