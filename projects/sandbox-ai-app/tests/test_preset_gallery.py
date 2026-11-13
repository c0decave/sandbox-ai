"""The preset gallery offscreen: cards by category with pictures and scores, the search, Enter and double-click
create, the short form for placeholders (offering what sandbox-ai vpn list / llm list say), the user's own
presets saved, exported and imported (secrets refused), the Board page and the wizard using the same library."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QColor, QKeyEvent  # noqa: E402

from sandbox_ai_app import backend, board as B, presets as PR, theme, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
PROFILES = [{"profile": "mv", "type": "wireguard", "provider": "mullvad"},
            {"profile": "ts", "type": "tailscale", "provider": "tailscale"}]
ENDPOINTS = [{"name": "gpu", "kind": "vllm", "url": "http://100.100.1.5:8000", "vpn": "ts"},
             {"name": "ollama", "kind": "ollama", "url": "http://127.0.0.1:11434", "vpn": None}]


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "project").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "vpn_profiles", lambda: PROFILES)
    monkeypatch.setattr(sai, "llm_endpoints", lambda: ENDPOINTS)
    return sai, tmp_path


@pytest.fixture
def gallery(env):
    from sandbox_ai_app.preset_gallery import PresetGallery
    sai, root = env
    board = PR.build(sai, PR.find("vpn-mcp"), root / "project", values={"<vpn:search>": "mv"})
    dialog = PresetGallery(sai, str(root / "project"), "claude", {}, board)
    yield dialog
    dialog.deleteLater()


def shown(gallery):
    return [c.preset.id for c in gallery.visible_cards()]


def test_the_gallery_lists_every_preset_by_category_with_a_picture_and_its_score(gallery):
    own = [p.id for p in PR.sandbox_ai_presets(gallery.sai)[0]]
    # One catalogue: an app's own preset that sandbox-ai has too (tor, local-api, development) is shown once, as
    # sandbox-ai's.
    replaced = {p.id for p in PR.PRESETS} & {ident[len("sai-"):] for ident in own}
    # sandbox-ai's presets by sandbox-ai's own categories, in their order (presets.CATEGORY_INFO), each a section.
    sai = PR.sandbox_ai_presets(gallery.sai)[0]
    order = [key for key, _ in PR.sai_categories(gallery.sai)]
    by_category = [p.id for key in order for p in sai if p.group == key]
    assert shown(gallery) == [p.id for key, _ in PR.CATEGORIES for p in PR.PRESETS
                              if p.category == key and p.id not in replaced] + by_category
    assert sorted(by_category) == sorted(own)
    headings = [h.text() for h, _, _, _ in gallery.sections]
    used = [key for key in order if any(p.group == key for p in sai)]
    assert headings == ["Simple", "Development", "Research", "Local & remote LLM", "Privacy & VPN", "Teams & policy",
                        *[f"sandbox-ai: {dict(PR.sai_categories(gallery.sai))[key]['title']['en']}" for key in used],
                        "My presets"]
    assert used[:3] == ["development", "research", "llm"] and "testing" in used and "other" not in used
    card = gallery.cards[0]
    picture = card.picture.pixmap()
    assert not picture.isNull() and picture.width() >= 200
    assert picture.toImage().pixelColor(1, 1) == QColor(theme.colors()["canvas"]), "the theme's canvas"
    assert "score 100" in card.meta.text() and gallery.selected is card
    assert gallery.title.text() == "Locked down (offline)" and "--offline" in gallery.command.toPlainText()
    remote = next(c for c in gallery.cards if c.preset.id == "remote-llm")
    assert "2 to fill in" in remote.meta.text()


def test_search_filters_and_arrows_move(gallery):
    gallery.search.setText("vpn")
    assert set(shown(gallery)) >= {"vpn-all", "vpn-mcp", "remote-llm"} and "locked" not in shown(gallery)
    assert gallery.selected.preset.id == shown(gallery)[0]
    hidden = [h for h, box, cards, key in gallery.sections if not h.isVisible() and key == "simple"]
    assert hidden or not gallery.isVisible()
    first = gallery.selected
    gallery.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
    assert gallery.selected is not first and gallery.selected.preset.id == shown(gallery)[1]
    gallery.search.setText("nothing matches this")
    assert shown(gallery) == [] and gallery.title.text() == "No preset matches"
    assert not gallery.create_button.isEnabled() and not gallery.create()


def test_enter_and_double_click_create(gallery):
    gallery.search.setText("strict")
    gallery.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
    assert gallery.result_[0].id == "strict-rules" and gallery.result_[1] == {}
    gallery.result_ = None
    gallery.search.setText("")
    card = next(c for c in gallery.cards if c.preset.id == "tailnet")
    card.mouseDoubleClickEvent(None)
    preset, values = gallery.result_
    assert preset.id == "tailnet" and values == {}, "nothing typed: the placeholder stays for the board"


def test_the_form_offers_what_sandbox_ai_knows(env, wait):
    from sandbox_ai_app.preset_gallery import PlaceholderForm
    sai, root = env
    form = PlaceholderForm(sai, PR.find("remote-llm"))
    llm, vpn = form.fields["<llm:remote>"], form.fields["<vpn:tailnet>"]
    assert wait(lambda: llm.count() == 3 and vpn.count() == 3), "the listings arrive in the background"
    form.set_value("<llm:remote>", "gpu")
    assert vpn.currentText() == "ts", "the server's own VPN profile"
    assert form.values() == {"<llm:remote>": "gpu", "<vpn:tailnet>": "ts"}
    form.fill_later()
    assert form.values() is None
    folders = PlaceholderForm(sai, PR.find("binary-analysis"))
    folders.set_value("<folder:samples>", str(root))
    assert folders.values() == {"<folder:samples>": str(root)}, "optional servers left empty are left out"


def test_own_presets_save_export_import_delete(gallery, env, tmp_path):
    sai, _ = env
    preset = gallery.save_board("My search", "behind Mullvad", "privacy")
    assert preset is not None and gallery.selected.preset.id == "my-my-search"
    mine = [c.preset.id for _, _, cards, key in gallery.sections if key == "mine" for c in cards]
    assert mine == ["my-my-search"] and gallery.delete_button.isEnabled()
    path = tmp_path / "out.json"
    assert gallery.export_preset(str(path)) == str(path)
    document = json.loads(path.read_text())
    assert document["title"] == "My search" and "<vpn:fetch>" in json.dumps(document)
    leaky = tmp_path / "leaky.json"
    leaky.write_text(json.dumps({**document, "id": "leaky", "agent_args": ["--token", "ghp_0123456789abcdef"]}))
    assert gallery.import_preset(str(leaky)) is None
    assert widgets.SHOWN[-1][0] == "Not imported" and "secret" in widgets.SHOWN[-1][1]
    path.write_text(json.dumps({**document, "id": "shared", "title": "Shared"}))
    assert gallery.import_preset(str(path)).id == "shared"
    assert sorted(p.id for p in PR.load_mine(sai)[0]) == ["my-my-search", "shared"]
    assert gallery.delete_preset() and [p.id for p in PR.load_mine(sai)[0]] == ["my-my-search"]
    gallery.current_board = B.from_argv(sai, "claude", [], ["--api-key", "sk-ant-api03-xyz"])
    assert gallery.save_board("Leaky") is None and widgets.SHOWN[-1][0] == "Not saved"


def test_the_board_page_creates_from_a_preset(env):
    from sandbox_ai_app.board_view import BoardPage
    sai, root = env
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    page.directory = str(root / "project")
    board = page.new_from_preset("vpn-all")
    assert page.board is board and board.first("vpn").props["--vpn"] == "<vpn:wireguard>"
    assert "To fill in: WireGuard profile" in page.notice.text()  # by the preset's label
    vpn = board.first("vpn")
    assert page.items[vpn.id].pen().color().name() == theme.color("bad"), "outlined until filled in"
    assert not page.apply() and not page.start(), "a placeholder stops Apply and Start"
    page.set_prop(vpn.id, "--vpn", "home")
    assert not [p for p in page.errors()]
    page.new_from_preset("research", {})
    nodes = {n.props["name"]: n for n in page.board.of_kind("mcp")}
    assert set(nodes) == {"search", "fetch"} and nodes["fetch"].props["project"]
    position = [(n.x, n.y) for n in page.board.nodes]
    assert len(set(position)) == len(position), "laid out by the preset, not on top of each other"
    page.deleteLater()


def test_the_wizard_offers_the_same_library(env):
    from sandbox_ai_app.wizard import SandboxWizard
    sai, root = env
    wizard = SandboxWizard(sai, lambda: {"default_template": "vpn-mcp"})
    wizard.directory = str(root / "project")
    page = wizard.start_page
    page.initializePage()
    assert page.choice() == "app:vpn-mcp" and page.chosen_preset().id == "vpn-mcp", "a key of the catalogue"
    items = [page.preset.itemText(i).strip() for i in range(page.preset.count())]
    assert "Privacy & VPN" in items and "Strict rules for GitHub" in items
    heading = page.preset.findText("Privacy & VPN")
    assert not page.preset.model().item(heading).isEnabled()
    assert page.validatePage()
    names = sorted(n.props["name"] for n in wizard.board.of_kind("mcp"))
    assert names == ["fetch", "search"] and [m for m, _ in B.placeholders(wizard.board)] == ["<vpn:search>",
                                                                                            "<vpn:fetch>"]
    wizard.mcp_page.initializePage()
    texts = [wizard.mcp_page.servers.item(i).text() for i in range(wizard.mcp_page.servers.count())]
    assert "search (defined in the project)" in texts
    assert wizard.mcp_page.validatePage()
    assert sorted(n.props["name"] for n in wizard.board.of_kind("mcp") if wizard.board.connected(n)) == names
    page.select_preset("mixed-llm")
    assert page.validatePage() and B.values(wizard.board)[0] == "opencode", "the preset's own agent"
    wizard.deleteLater()


def test_the_categories_and_the_description_are_on_the_left(gallery, qapp):
    """Feedback 2026-11-11: the categories above the description, both on the left; a click on a category shows its
    section of the cards without scrolling down to it."""
    split = gallery.categories.parentWidget().parentWidget()
    assert isinstance(split, QtWidgets.QSplitter)
    assert split.widget(0).isAncestorOf(gallery.categories) and split.widget(0).isAncestorOf(gallery.description)
    assert split.widget(1) is gallery.scroll
    side = split.widget(0).layout()
    order = [side.itemAt(i).widget() for i in range(side.count()) if side.itemAt(i).widget() is not None]
    assert order.index(gallery.categories) < order.index(gallery.description)
    assert gallery.categories.count() == len(gallery.sections) >= 2
    gallery.resize(1180, 500)
    gallery.show()
    qapp.processEvents()
    last = gallery.categories.item(gallery.categories.count() - 1)
    heading = next(h for h, _b, _c, key in gallery.sections if key == last.data(Qt.ItemDataRole.UserRole))
    gallery.show_category(last)
    bar = gallery.scroll.verticalScrollBar()
    assert bar.value() == min(heading.y(), bar.maximum()) and (bar.maximum() == 0 or bar.value() > 0)
    gallery.search.setText("tor")
    hidden = [gallery.categories.item(row).isHidden() for row in range(gallery.categories.count())]
    assert any(hidden) and not all(hidden)  # categories without a match go from the list
    gallery.close()
