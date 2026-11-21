"""Analysis round 2 of 0.1.97 in the app (user journeys end to end): what a user ran into on the new pages and
dialogs, driven like a user (selection, right click, buttons). Each test is named after what was broken or
misleading, and stays as a regression test.

The rule (user, 2026-11-14): every list's buttons' commands are also on its right-click menu, enabled as the buttons
are, a disabled one with the same reason."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, package_search as ps, registry_browser as rb, widgets  # noqa: E402
from test_bridges_app import ROWS, bridge_page  # noqa: E402
from test_images_browse import FakeSai, Module, Web, quiet  # noqa: E402,F401 - the browse tests' fixture
from test_ui import env, window  # noqa: E402,F401 - the fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def entries(view):
    """(text, enabled) of VIEW's right-click menu now, without Copy."""
    assert getattr(view, "menu_entries", None) is not None, "no right-click menu with the list's commands"
    return widgets.menu_entries(widgets.menu_of(view))


def test_the_bridges_list_had_no_right_click_menu(window, env, monkeypatch, wait):
    """Sandboxes & sessions, Bridges: New bridge…, Show, Revoke… and Resume… were buttons only; a right click on a
    bridge offered nothing but Copy. Its menu has them, enabled as the buttons are (Resume only for a suspended
    bridge), and choosing one does what its button does."""
    page = bridge_page(window, env, monkeypatch, wait)
    box = page.bridges_box
    box.table.selectRow(widgets.find_row(box.table, "1a2b3c4d"))
    found = dict(entries(box.table))
    assert list(found) == [widgets.button_text(b) for b in box.buttons.values()], found
    assert found[widgets.button_text(box.buttons["show"])] is True
    assert found[widgets.button_text(box.buttons["resume"])] is False
    resume = next(a for a in widgets.menu_of(box.table).actions()
                  if a.text() == widgets.button_text(box.buttons["resume"]))
    assert resume.toolTip() == box.buttons["resume"].toolTip()
    box.table.selectRow(widgets.find_row(box.table, "5e6f7a8b"))
    assert dict(entries(box.table))[widgets.button_text(box.buttons["resume"])] is True
    shown = []
    monkeypatch.setattr(box, "show_bridge", lambda *a: shown.append("show"))
    box.buttons["show"].clicked.disconnect()
    box.buttons["show"].clicked.connect(lambda _=False: box.show_bridge())
    next(a for a in widgets.menu_of(box.table).actions()
         if a.text() == widgets.button_text(box.buttons["show"])).trigger()
    assert shown == ["show"]
    assert ROWS  # (the rows of the page's bridges)


def test_the_package_search_results_had_no_right_click_menu(quiet, monkeypatch):  # noqa: F811
    """Images, Extension tab, a package list's search: the packages found had Add as a button only (and a double
    click). Their right-click menu has Add, enabled for a selection as the button is, and adds."""
    from sandbox_ai_app.images_page import ImagesPage
    page = ImagesPage(FakeSai(), lambda *a, **kw: None)
    page._module = Module()
    monkeypatch.setattr(ps, "http_get", Web({"https://registry.npmjs.org/": {"objects": [
        {"package": {"name": "typescript", "version": "5.7.2", "description": "TypeScript"}}]}}))
    browser = page.lists["npm"].browser
    browser.query.setText("typescript")
    browser.search()
    assert entries(browser.results) == [(widgets.button_text(browser.add_button), False)]
    page.set_editing(True)  # (an image's extension is open)
    browser.results.selectRow(0)
    assert entries(browser.results) == [(widgets.button_text(browser.add_button), True)]
    widgets.menu_of(browser.results).actions()[0].trigger()
    assert page.lists["npm"].values() == ["typescript"]
    page.deleteLater()


def test_the_derive_dialogs_package_list_had_no_right_click_menu(quiet):  # noqa: F811
    """Your images, Add OS packages…: the packages to add had Remove as a button only. Its right-click menu has
    Remove and removes the selected one."""
    from sandbox_ai_app.images_page import ImagesPage
    page = ImagesPage(FakeSai(), lambda *a, **kw: None)
    dialog = ps.DeriveDialog(None, page, "mine", "Alpine Linux 3.20 (apk)")
    dialog.add("curl")
    dialog.add("jq")
    dialog.chosen.setCurrentRow(0)
    assert [text for text, _ in entries(dialog.chosen)] == ["Remove"]
    widgets.menu_of(dialog.chosen).actions()[0].trigger()
    assert dialog.packages() == ["jq"]
    dialog.deleteLater()
    page.deleteLater()


def test_the_registry_browser_lists_had_no_right_click_menu(quiet, monkeypatch):  # noqa: F811
    """Your images, Browse registries…: a repository found and its tags had Add… as the dialog's button only. Both
    lists' right-click menus have Add…, enabled as the button is."""
    from sandbox_ai_app import state
    monkeypatch.setattr(state, "current", lambda: type("S", (), {"get": lambda s, k, d=None: d,
                                                                 "set": lambda s, k, v: None})())
    web = Web({"https://hub.docker.com/v2/search/repositories/": {"results": [
        {"repo_name": "redis", "short_description": "Redis", "star_count": 13000, "is_official": True}]},
        "https://hub.docker.com/v2/repositories/library/redis/tags": {"results": [{"name": "7-alpine"},
                                                                                {"name": "latest"}]}})
    dialog = rb.RegistryBrowser(None, web)
    dialog.query.setText("redis")
    dialog.search()
    add = widgets.button_text(dialog.ok)
    assert entries(dialog.results) == [(add, True)] and entries(dialog.tags) == [(add, True)]
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(dialog.reference()))
    dialog.tags.setCurrentRow(0)
    widgets.menu_of(dialog.tags).actions()[0].trigger()
    assert accepted == ["docker.io/library/redis:7-alpine"]
    dialog.deleteLater()


def test_a_presets_service_left_to_fill_in_was_said_to_be_unreadable(tmp_path, monkeypatch):
    """Gallery: "Database work" with Fill in later (or Create without filling): its services' images are the preset's
    values still. Each service had two errors - "Fill in “Your PostgreSQL image …”" and "The service db cannot be
    read: --service db: image='<text:postgres>' is the name of one of your images" - the second wrong (it is only
    not filled in yet) and showing the placeholder's inner form. Only the first is said; filled in, the part is read
    and checked as ever; a service the parser refuses for another reason is still said."""
    from sandbox_ai_app import board as B, presets as P
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                      ("XDG_CACHE_HOME", "cache"), ("HOME", "home")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "home").mkdir()
    sai = backend.SandboxAI()
    preset = next(p for p in P.catalogue(sai).presets if p.id.endswith("database-services"))
    board = P.build(sai, preset, directory=tmp_path)
    texts = [p.text for p in B.problems(sai, board) if p.node is not None
             and board.node(p.node).kind == "service"]
    assert texts and all(text.startswith("Fill in") for text in texts), texts
    assert not any("cannot be read" in text for text in texts), texts
    B.fill(board, {"<text:postgres>": "postgres", "<text:redis>": "redis"}, sai)
    texts = [p.text for p in B.problems(sai, board, user_images=["postgres"]) if p.node is not None
             and board.node(p.node).kind == "service"]
    assert any("redis is none of your images" in text for text in texts), texts
    broken = B.from_argv(sai, "claude", ["--workspace", str(tmp_path), "--service", "db:image=x,port=notaport"])
    assert any("cannot be read" in p.text for p in B.problems(sai, broken)), "another refusal is still said"


def test_the_llm_part_beside_pi_said_it_offers_all_the_servers_models(tmp_path):
    """Board: an LLM part beside Pi (Letta Code, nanobot, OpenHands) said "all its models" - these agents are given
    only the start's model (sandbox-ai's llm.ONE_MODEL). It says so; beside OpenCode 2 it still says all."""
    from sandbox_ai_app import board as B, i18n
    from sandbox_ai_app.board_view import summary
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("llm_for_app_test", Path(os.environ["SANDBOX_AI_HOME"])
                                                  / "sandbox/llm.py")
    llm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(llm)
    assert set(B.ONE_MODEL_AGENTS) == set(llm.ONE_MODEL), "the app's list is the launcher's"
    sai = backend.SandboxAI()
    for agent in ("pi", "letta", "nanobot", "openhands"):
        board = B.from_argv(sai, agent, ["--workspace", str(tmp_path), "--llm", "lm:qwen3-coder"])
        text = summary(board.first("llm"), agent)
        assert "all its models" not in text and "the start's model only" in text, (agent, text)
    board = B.from_argv(sai, "opencode2", ["--workspace", str(tmp_path), "--llm", "lm:qwen3-coder"])
    assert "all its models" in summary(board.first("llm"), "opencode2")
    assert i18n.TEXTS["the start's model only"]["de"]
