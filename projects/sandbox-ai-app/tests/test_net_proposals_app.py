"""The network proposals of 2026-11-24 in the app (docs/design/network-allowances.md, section 5): F - --allow-net is
the documented name of --allow, the app keeps --allow (what saved options and older hosts know) and reads both;
A - the host sets are called so; D - Git logins and API keys during a run come from Credentials; G - an MCP
server's network in the agents' words."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from sandbox_ai_app import backend  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def test_the_app_keys_allow_net_as_allow_and_reads_both():
    sai = backend.SandboxAI()
    names = {option.long for option in sai.options()}
    assert "--allow" in names and "--allow-net" not in names
    for argv in (["--allow-net", "192.0.2.0/24"], ["--allow", "192.0.2.0/24"], ["-a", "192.0.2.0/24"]):
        values, unknown = backend.values_from_argv(sai.options(), argv, backend.canonical_of(sai))
        assert values == {"--allow": ["192.0.2.0/24"]} and not unknown, argv


# -- G: an MCP server's network in the agents' words --


def test_mcp_networks_are_shown_in_the_agents_words_and_sent_as_every_host_knows_them(qapp):
    from sandbox_ai_app import mcp_fields
    box = mcp_fields.network_choice("full", ["a.example.com:443"])
    texts = [box.itemText(i) for i in range(box.count())]
    assert texts == ["egress-all (the internet, not your LAN)", "allowlist (only its hosts)",
                     "lan (the internet and your LAN; a warning)", "offline"]
    assert [box.itemData(i) for i in range(box.count())] == ["full", "allowlist", "lan", "none"], \
        "sent as every sandbox-ai knows them"
    assert box.currentData() == "full"
    # A host (or a catalog) that says it in the new words: read as the same.
    assert mcp_fields.network_choice("egress-all").currentData() == "full"
    assert mcp_fields.network_choice("offline").currentData() == "none"
    for word, stored in (("offline", "none"), ("egress-all", "full"), ("none", "none"), ("lan", "lan")):
        assert backend.mcp_network(word) == stored
        assert backend.mcp_network_word(stored) in ("offline", "egress-all", "allowlist", "lan")
    assert "--network=full" in backend.mcp_set_args("x", network="egress-all")


def test_the_catalog_lines_in_either_words_are_read_alike(monkeypatch):
    sai = backend.SandboxAI()
    lines = ("hub:web-fetch  [egress-all]  Fetch  signed\n"
             "hub:duckduckgo  [allowlist html.duckduckgo.com:443]  DuckDuckGo  signed\n"
             "hub:old  [full]  Old  unsigned\n")
    # (a host older than 0.1.95 answers with its lines; a newer one with --json, in either words too)
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=150: (
        2, "", "unrecognized arguments: --json") if "--json" in args else (0, lines, ""))
    found, _ = sai.host_mcp_catalog("ct1")
    networks = {entry["name"]: entry["network"] for _, entry in found}
    assert networks == {"web-fetch": "full", "duckduckgo": "allowlist", "old": "full"}

    _, entry = sai.remote_catalog_entry({"name": "web-fetch", "prefix": "hub", "network": "egress-all"}, ())
    assert entry["network"] == "full"


# -- D: Git logins and API keys during a run from Credentials; A: the host sets in the Allow… dialog --


def test_the_allow_dialog_has_no_login_fields_and_leads_to_credentials(qapp):
    from sandbox_ai_app.ui import NetworkDialog
    dialog = NetworkDialog(None, {"github": ["github.com", "api.github.com"]}, "Allow",
                           {"--allow-host", "--git-token-env", "--api-key", "--keep"})
    assert not hasattr(dialog, "git_url") and not hasattr(dialog, "git_ref")
    form = dialog.layout()
    labels = [form.itemAt(i, form.ItemRole.LabelRole) for i in range(form.rowCount())]
    texts = [item.widget().text() for item in labels if item is not None and item.widget() is not None]
    headings = [label.text() for label in dialog.findChildren(QtWidgets.QLabel)]
    assert "Host sets (TCP/443)" in headings and not any("preset" in text.lower() for text in texts + headings), texts
    assert "Addresses or networks, every port (--allow-net)" in texts
    assert dialog.credentials_link.text() == "Git login or API key…"
    assert "Credentials" in dialog.credentials_link.toolTip()
    dialog.show()
    dialog.credentials_link.click()
    assert dialog.wants_credentials and dialog.result() != dialog.DialogCode.Accepted and not dialog.isVisible()
    dialog.deleteLater()


def test_a_git_login_from_a_variable_or_file_is_given_from_credentials(qapp, monkeypatch):
    """What the Allow… dialog's Git login row did (a token in a variable, a secret or a file, never the token) is
    Credentials › Git logins › Give to a running sandbox… with no login chosen: nothing the user could do is lost."""
    from sandbox_ai_app import credentials_page as CP
    args = CP.give_login_args("web", {"url": "https://git.example.org", "env": "GITEA_TOKEN"}, keep=False)
    assert args == ["--env", "GITEA_TOKEN", "--", "web", "https://git.example.org"]
    args = CP.give_login_args("web", {"url": "https://git.example.org/", "file": "/home/u/token", "user": "u"},
                              keep=True)
    assert args == ["--file", "/home/u/token", "--user", "u", "--keep", "--", "web", "https://git.example.org"]
    dialog = CP.OtherLoginDialog(None)
    dialog.url.setText("https://git.example.org")
    dialog.kind.setCurrentIndex(dialog.kind.findData("file"))
    dialog.ref.setText("/home/u/token")
    assert dialog.entry() == {"url": "https://git.example.org", "file": "/home/u/token", "user": ""}
    dialog.ref.setText("")
    assert dialog.entry() is None, "the server and where its token is: both"
    dialog.deleteLater()
