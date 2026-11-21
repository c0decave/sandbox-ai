"""Several items in one value of sandbox-ai's list options (--allow-host "pypi.org, *.example.com", --allow-tcp
10.0.0.5:22,10.0.0.6:22 ...) in the app: backend.LIST_OPTIONS is the launcher's, a Board field, the setup wizard's
network page, the guide and the network dialog of a running sandbox take them separated by commas or spaces (one or
more per line) and pass each item as an option of its own, once; a board read from a command line or a project file
with such a value has the items one by one. Values with a comma or a space by meaning (--api-key, --allow-http) are
never split."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, guide, i18n  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


def test_the_list_options_are_the_launchers(sai):
    assert tuple(backend.LIST_OPTIONS) == tuple(sai.launcher.LIST_OPTIONS)
    for option in sai.options():
        if option.long in backend.LIST_OPTIONS:
            assert option.kind == "list", option.long


@pytest.mark.parametrize("text, items", [
    ("pypi.org, files.pythonhosted.org *.example.com", ["pypi.org", "files.pythonhosted.org", "*.example.com"]),
    ("a\nb, c\n\n,,a", ["a", "b", "c"]), ("", []), (None, []),
    ("[2001:db8::1]:443,10.0.0.5:22", ["[2001:db8::1]:443", "10.0.0.5:22"]),
])
def test_list_items(text, items):
    assert backend.list_items(text) == items


def test_option_tokens_and_values_from_argv(sai):
    hosts = next(o for o in sai.options() if o.long == "--allow-host")
    assert hosts.tokens(["pypi.org, files.pythonhosted.org", "pypi.org", " "]) == [
        "--allow-host", "pypi.org", "--allow-host", "files.pythonhosted.org"]
    keys = next(o for o in sai.options() if o.long == "--api-key")
    assert keys.tokens(["api.example.com=EX, KEY"]) == ["--api-key", "api.example.com=EX, KEY"]  # never split
    values, unknown = backend.values_from_argv(sai.options(), ["--allow-host", "a.example,b.example", "-t",
                                                               "10.0.0.5:22 [2001:db8::1]:443", "--allow-host", "a.example",
                                                               "--allow-http", "h.example GET,HEAD /x"],
                                                 canonical=backend.canonical_of(sai))
    assert values["--allow-host"] == ["a.example", "b.example"] and unknown == []
    assert values["--allow-tcp"] == ["10.0.0.5:22", "[2001:db8::1]:443"]
    assert values["--allow-http"] == ["h.example GET,HEAD /x"]
    # Without the launcher's canonical_options (an older host's stand-in): the items one by one all the same.
    values, _ = backend.values_from_argv(sai.options(), ["--allow-host", "a.example b.example"])
    assert values["--allow-host"] == ["a.example", "b.example"]


def test_a_board_from_a_command_line_with_lists(sai):
    board = B.from_argv(sai, "claude", ["-A", "pypi.org, files.pythonhosted.org", "--allow-host-port", "8080,11434",
                                        "--allow", "10.0.0.0/8 192.168.1.0/24"])
    props = board.first("network").props
    assert props["--allow-host"] == ["pypi.org", "files.pythonhosted.org"]
    assert props["--allow-host-port"] == ["8080", "11434"]
    assert props["--allow"] == ["10.0.0.0/8", "192.168.1.0/24"]
    argv = B.to_argv(sai, board)
    assert argv.count("--allow-host") == 2 and argv.count("--allow-host-port") == 2


def test_the_boards_field_splits_a_list_options_lines(qapp):
    from sandbox_ai_app.board_view import LinesEdit
    edit = LinesEdit([], items=True)
    edit.setPlainText("pypi.org, files.pythonhosted.org\n*.example.com pypi.org\n,,")
    assert edit.values() == ["pypi.org", "files.pythonhosted.org", "*.example.com"]
    assert "commas or spaces" in edit.toolTip()
    plain = LinesEdit([])  # any other list: one value per line, kept whole
    plain.setPlainText("h.example GET,HEAD /x\n")
    assert plain.values() == ["h.example GET,HEAD /x"]
    for widget in (edit, plain):
        widget.deleteLater()


def test_the_guide_splits_hosts_and_domains():
    assert guide.props_of("network", {"--allow-host": "a.example, b.example\nc.example"}) == {
        "--allow-host": ["a.example", "b.example", "c.example"]}
    assert guide.props_of("network", {"--allow-domain": ["example.org example.net", "example.org"]}) == {
        "--allow-domain": ["example.org", "example.net"]}


def test_the_network_dialog_takes_lists(qapp):
    from sandbox_ai_app.ui import NetworkDialog
    dialog = NetworkDialog(None, {}, "Allow", {"--allow-host", "--allow-domain", "--allow-host-port", "--nameserver"})
    dialog.hosts.setText("pypi.org, files.pythonhosted.org *.example.com pypi.org")
    dialog.domains.setText("example.org,example.net")
    dialog.tcp.setText("10.0.0.5:22,[2001:db8::1]:443")
    dialog.nets.setText("10.0.0.0/8, 192.168.1.0/24")
    dialog.host_ports.setText("8080,,11434")
    dialog.nameservers.setText("192.0.2.53,192.0.2.54")
    assert dialog.arguments() == [
        "--allow-host", "pypi.org", "--allow-host", "files.pythonhosted.org", "--allow-host", "*.example.com",
        "--allow-domain", "example.org", "--allow-domain", "example.net",
        "--allow-tcp", "10.0.0.5:22", "--allow-tcp", "[2001:db8::1]:443", "--allow", "10.0.0.0/8",
        "--allow", "192.168.1.0/24", "--allow-host-port", "8080", "--allow-host-port", "11434",
        "--nameserver", "192.0.2.53", "--nameserver", "192.0.2.54"]
    dialog.deleteLater()


def test_the_wizards_network_page_takes_lists(qapp, tmp_path, monkeypatch):
    from sandbox_ai_app import widgets
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.wizard import SandboxWizard
    wizard = SandboxWizard(backend.SandboxAI(), lambda: {"default_agent": "claude", "default_template": "development"})
    try:
        page = wizard.network_page
        # (one field of kinds since 2026-11-19: list_fields; a value lists several, separated by commas or spaces)
        assert page.lists.add("pypi.org, files.pythonhosted.org *.example.com", key="--allow-host")
        assert page.lists.add("example.org example.net", key="--allow-domain")
        page.lists.choose_kind("--allow-host-port")
        page.lists.value_box.setEditText("8080,11434")  # typed, not added yet: Next takes it
        assert page.validatePage()
        props = page.node("network").props
        assert props["--allow-host"] == ["pypi.org", "files.pythonhosted.org", "*.example.com"]
        assert props["--allow-domain"] == ["example.org", "example.net"]
        assert props["--allow-host-port"] == ["8080", "11434"]
    finally:
        wizard.deleteLater()


def test_the_new_texts_are_german_too():
    for text in ("One or more, separated by commas or spaces", "One or more per line, separated by commas or spaces",
                 "Names separated by commas or spaces. *.DOMAIN opens every name below DOMAIN, each as it is looked up "
                 "(not for shared hosting such as *.github.io, nor public suffixes)."):
        assert i18n.TEXTS[text]["de"] and i18n.TEXTS[text]["de"] != text
