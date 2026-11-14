"""--allow-domain DOMAIN in the app: the Board's network part (the option, its inspector field and its summary, what
it reaches, the policy's allow_hosts, offline), the guide, the wizard's network page, the network dialog of a
running sandbox and the option's label and group. sandbox-ai turns it into --allow-host DOMAIN '*.DOMAIN'; the app
passes it on as given and reads it as that."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, guide, labels  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


def test_the_option_is_the_launchers_in_the_network_part(sai):
    option = next(o for o in sai.options() if o.long == "--allow-domain")
    assert option.kind == "list" and option.short == "-ad"
    assert "--allow-domain" in backend.GROUPS["Network"] and "--allow-domain" in B.KINDS["network"].options
    assert labels.OPTION_LABELS["--allow-domain"][0].startswith("Allowed domains")
    board = B.from_argv(sai, "claude", ["-ad", "example.org", "--allow-domain", "example.com", "-A", "x.example"])
    assert board.first("network").props == {"--allow-domain": ["example.org", "example.com"],
                                            "--allow-host": ["x.example"]}
    argv = B.to_argv(sai, board)
    assert argv[argv.index("--allow-domain") + 1] == "example.org" and argv.count("--allow-domain") == 2
    again = B.from_argv(sai, "claude", argv)
    assert again.first("network").props == board.first("network").props


def test_what_it_reaches_and_the_policy(sai):
    board = B.from_argv(sai, "claude", ["--allow-domain", "*.Example.org."])
    _, badges = B.badges(board)
    assert any(b.level == "bad" and b.text == "example.org and every host below it" for b in badges), \
        [b.text for b in badges]
    policy = {"version": 1, "allow_within": ["10.0.0.0/8"], "allow_hosts": ["example.org"]}

    def errors(argv, policy=policy):
        return [p.text for p in B.problems(sai, B.from_argv(sai, "claude", argv), policy) if p.level == "error"]
    # The policy lists the name but not the names below it: *.example.org is outside, as sandbox-ai says.
    assert any("*.example.org" in t and "allow_hosts" in t for t in errors(["--allow-domain", "example.org"]))
    assert errors(["--allow-domain", "example.org"], {**policy, "allow_hosts": ["example.org", "*.example.org"]}) == []
    assert any("Offline contradicts" in t and "--allow-domain" in t
               for t in errors(["--offline", "--allow-domain", "example.org"], {}))
    reaches, reached, _ = B.server_network({"command": ["x"], "options": ["--allow-domain", "example.org"]})
    assert reaches and reached == ["example.org, *.example.org"]


def test_the_parts_summary_shows_what_it_stands_for(qapp, sai):
    from sandbox_ai_app import board_view
    board = B.from_argv(sai, "claude", ["--allow-domain", "example.org", "-A", "x.example"])
    assert board_view.summary(board.first("network")) == "x.example, example.org, *.example.org"


def test_the_guide_asks_for_domains(sai):
    question = next(q for q in guide.questions("network") if q.key == "--allow-domain")
    assert question.form == "list"
    assert guide.props_of("network", {"--allow-domain": ["example.org"]}) == {"--allow-domain": ["example.org"]}
    assert guide.props_of("network", {"--allow-domain": []}) == {"--allow-domain": None}


def test_the_network_dialog_takes_domains_where_sandbox_ai_does(qapp):
    from sandbox_ai_app.ui import NetworkDialog
    new = NetworkDialog(None, {}, "Allow", {"--allow-host", "--allow-domain"})
    new.hosts.setText("x.example")
    new.domains.setText("example.org example.com")
    assert new.arguments() == ["--allow-host", "x.example", "--allow-domain", "example.org", "--allow-domain",
                               "example.com"]
    old = NetworkDialog(None, {}, "Allow", {"--allow-host"})  # a sandbox-ai without it: not offered, not passed
    old.domains.setText("example.org")
    assert old.arguments() == []
    labels_ = [old.layout().itemAt(i, old.layout().ItemRole.LabelRole) for i in range(old.layout().rowCount())]
    texts = [item.widget().text() for item in labels_ if item is not None and item.widget() is not None]
    assert "Domains with every name below (TCP/443)" not in texts
    for dialog in (new, old):
        dialog.deleteLater()


def test_the_wizards_network_page(qapp, tmp_path, monkeypatch):
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
        page.node("network").props["--allow-domain"] = ["example.org"]
        page.initializePage()
        assert page.domains.values() == ["example.org"]
        page.domains.setPlainText("example.org\nexample.com")
        assert page.validatePage()
        assert page.node("network").props["--allow-domain"] == ["example.org", "example.com"]
        page.domains.setPlainText("")
        assert page.validatePage() and "--allow-domain" not in page.node("network").props
    finally:
        wizard.deleteLater()


def test_a_policy_that_forbids_it_greys_the_wizards_field_out(qapp, tmp_path, monkeypatch):
    from sandbox_ai_app import widgets
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "forbidden", lambda: {"--allow-domain": {None}})
    from sandbox_ai_app.wizard import SandboxWizard
    wizard = SandboxWizard(sai, lambda: {"default_agent": "claude", "default_template": "development"})
    try:
        page = wizard.network_page
        assert not page.domains.isEnabled()
        page.node("network").props["--allow-domain"] = ["example.org"]
        page.initializePage()
        assert page.validatePage() and page.node("network").props["--allow-domain"] == ["example.org"]
    finally:
        wizard.deleteLater()
