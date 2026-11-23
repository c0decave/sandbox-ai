"""Build toolchains in the app, offscreen: the Board's part Toolchains (--toolchain in the start's arguments, kept in
the board's state, said in the review), and for a running sandbox Add toolchain… and Remove toolchain… on the
Sandboxes page - its buttons, its row's right-click menu, and a terminal tab's menu under While it runs - each asked
first, against a stand-in that records what was asked of the host (`sandbox-ai toolchain ...`): nothing is mounted."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, board as B, help_content, help_texts, i18n  # noqa: E402

from test_live_mcp_ui import page_on, ran, sai, ui  # noqa: E402,F401 - shared fixtures
from test_projects import env, window  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

GO = {"name": "go", "version": "1.27.3", "commands": ["go", "gofmt"], "hosts": ["proxy.golang.org"],
      "allow": ["github"], "size": 250 << 20, "signed_by": "SHA256:x", "used_by": []}
JDK = {"name": "jdk", "version": "21.0.13+9", "commands": ["java", "mvn"], "hosts": ["repo.maven.apache.org"],
       "allow": [], "size": 500 << 20}


def catalog(*rows, state="good"):
    return 0, json.dumps({"toolchains": list(rows), "signature": {"state": state, "reason": "no signature"},
                          "problems": [], "arch": "x86_64"}), ""


def running(host="local"):
    more = {"host": host} if host != "local" else {}
    return backend.Instance("session", "session:api", "api", run="claude-1", session="api", state="running",
                            agent="claude", record={}, **more)


@pytest.fixture
def picked(ui, monkeypatch):
    """QInputDialog.getItem answered with the first item (or the one whose text starts with picked.want)."""
    seen = []

    def get_item(parent, title, label, items, current=0, editable=False):
        seen.append((title, list(items)))
        wanted = [item for item in items if item.startswith(get_item.want)] if get_item.want else list(items)
        return (wanted[0], True) if wanted and get_item.ok else ("", False)
    get_item.want, get_item.ok = None, True
    monkeypatch.setattr(ui.QInputDialog, "getItem", get_item)
    notices = []
    monkeypatch.setattr(ui, "report", lambda parent, title, result, error: notices.append((title, result, error)) or 0)
    monkeypatch.setattr(ui, "inform", lambda parent, title, text: notices.append((title, text)))
    monkeypatch.setattr(ui, "short_notice", lambda parent, title, text: notices.append((title, text)))
    monkeypatch.setattr(ui, "warn", lambda parent, title, text, **more: notices.append((title, text)))
    get_item.seen, get_item.notices = seen, notices
    return get_item


# --- the Board ---------------------------------------------------------------------------------------------------


def test_the_board_has_a_toolchains_part_with_help_in_both_languages():
    kind = B.KINDS["toolchains"]
    assert kind.options == ("--toolchain", "--toolchain-offline") and kind.single
    assert ("toolchains", "agent") in B.EDGES and B.OWNER["--toolchain"] == "toolchains"
    assert any("toolchains" in column for column in B.Board.FLOW)
    tip = help_texts.tips()["kind.toolchains"] if hasattr(help_texts, "tips") else None
    assert help_texts.KIND_SECTIONS["toolchains"] == "toolchains" and len(help_texts.KIND_DOES["toolchains"]) == 2
    section = next(s for s in help_content.SECTIONS if s.id == "toolchains")
    assert "/opt/toolchains/NAME" in section.body.en and "nur lesbar" in section.body.de
    assert "sandbox-ai toolchain attach" in section.cli
    for text in (kind.title, kind.help, "Add toolchain…", "Remove toolchain…"):
        assert i18n.TEXTS[text]["de"]
    assert tip is None or tip.cli.startswith("--toolchain")


def test_a_board_with_the_part_starts_with_its_toolchains_and_keeps_them(sai, tmp_path):
    """--toolchain on a command line (a preset's options: presets.build goes through from_argv) becomes the part
    Toolchains, connected to the agent; the board's start has the options again, and its saved state keeps them."""
    board = B.from_argv(sai, "claude", ["--toolchain", "go", "--toolchain", "jdk@21.0.13+9"], [], [str(tmp_path)])
    node = board.first("toolchains")
    assert node is not None and node.props["--toolchain"] == ["go", "jdk@21.0.13+9"]
    assert (node.id, board.first("agent").id) in board.edges
    argv = B.check_argv(sai, board)
    pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] == "--toolchain"]
    assert pairs == [("--toolchain", "go"), ("--toolchain", "jdk@21.0.13+9")]
    again = B.Board.from_state(json.loads(json.dumps(board.state())))
    assert again.first("toolchains").props["--toolchain"] == ["go", "jdk@21.0.13+9"]
    node.props["--toolchain-offline"] = True
    assert "--toolchain-offline" in B.check_argv(sai, board)
    fresh = B.from_argv(sai, "claude", [], [], [str(tmp_path)])
    assert fresh.first("toolchains") is None  # only on a board that names one (or where you add the part)
    added = fresh.add("toolchains")
    assert (added.id, fresh.first("agent").id) in fresh.edges


# --- a running sandbox: Add toolchain…, Remove toolchain… -----------------------------------------------------------


def test_add_toolchain_to_a_running_sandbox_from_the_store(ui, sai, picked, monkeypatch):
    page = page_on(ui, sai, monkeypatch, running())
    page.update_buttons()
    assert page.run_named["add-toolchain"].isEnabled() and page.run_named["remove-toolchain"].isEnabled()
    assert "sandbox-ai toolchain attach" in page.run_named["add-toolchain"].toolTip()
    sai.answers[("toolchain", "list", "--json")] = (0, json.dumps([GO]), "")
    sai.answers[("toolchain", "catalog")] = catalog(GO, JDK)
    sai.answers[("toolchain", "list", "--run")] = (0, "[]", "")
    sai.answers[("toolchain", "attach")] = (0, "attached go 1.27.3 to claude-1 at /opt/toolchains/go", "")
    page.run_named["add-toolchain"].click()
    title, items = picked.seen[0]
    assert title == "Add toolchain to claude-1" and len(items) == 2
    assert items[0] == "go 1.27.3: go, gofmt" and "downloaded from the signed catalog first" in items[1]
    action, text, accept = ui.asked[-1]
    assert action == "toolchain-add" and accept == "Add"
    assert "/opt/toolchains/go" in text and "proxy.golang.org, --allow-github" in text and "MiB" not in text
    assert ran(sai, "local", "toolchain", "attach") == ("local", "toolchain", "attach", "claude-1", "go")
    assert ran(sai, "local", "toolchain", "add") is None  # in the store: nothing is downloaded
    assert picked.notices[-1][1] == (0, "attached go 1.27.3 to claude-1 at /opt/toolchains/go", "")


def test_a_toolchain_of_the_catalog_is_downloaded_first_and_not_attached_when_that_fails(ui, sai, picked, monkeypatch):
    page = page_on(ui, sai, monkeypatch, running("box"))
    sai.answers[("toolchain", "list", "--json")] = (0, "[]", "")
    sai.answers[("toolchain", "catalog")] = catalog(JDK)
    sai.answers[("toolchain", "add")] = (0, "added jdk", "")
    page.add_toolchain()
    assert picked.seen[0][0] == "Add toolchain to api on box"
    assert "downloaded from the signed catalog into the store first (500 MiB)" in ui.asked[-1][1]
    assert ran(sai, "box", "toolchain", "add") == ("box", "toolchain", "add", "jdk")
    assert ran(sai, "box", "toolchain", "attach") == ("box", "toolchain", "attach", "api", "jdk")
    sai.asked.clear()
    sai.answers[("toolchain", "add")] = (1, "", "sandbox-ai toolchain: SHA-256 ... the signed catalog says ...")
    page.add_toolchain()
    assert ran(sai, "box", "toolchain", "attach") is None and picked.notices[-1][1][0] == 1


def test_what_is_attached_or_not_signed_is_not_offered(ui, sai, picked, monkeypatch):
    page = page_on(ui, sai, monkeypatch, running())
    sai.answers[("toolchain", "list", "--json")] = (0, json.dumps([GO]), "")
    sai.answers[("toolchain", "catalog")] = catalog(JDK, state="unsigned")  # nobody vouched: never offered
    sai.answers[("toolchain", "list", "--run")] = (0, json.dumps([{"name": "go", "version": "1.27.3",
                                                                  "source": "start"}]), "")
    page.add_toolchain()
    assert picked.seen == [] and "attached to it already" in picked.notices[-1][1]
    assert ran(sai, "local", "toolchain", "attach") is None
    ui.answer_yes = False  # asked, and answered no: nothing happens
    sai.answers[("toolchain", "list", "--run")] = (0, "[]", "")
    page.add_toolchain()
    assert ui.asked[-1][0] == "toolchain-add" and ran(sai, "local", "toolchain", "attach") is None


def test_remove_toolchain_takes_one_added_during_the_run_and_asks_before_it_forces(ui, sai, picked, monkeypatch):
    page = page_on(ui, sai, monkeypatch, running())
    sai.answers[("toolchain", "list", "--run")] = (0, json.dumps([
        {"name": "go", "version": "1.27.3", "source": "start"}, {"name": "jdk", "version": "21", "source": "added"},
        {"name": "node", "version": "26", "source": "added"}]), "")
    picked.want = "node"
    busy = (1, "", "sandbox-ai toolchain: could not detach node: /opt/toolchains/node is in use in the sandbox (a "
                  "process runs from it or has a file of it open): --force detaches it anyway")
    sai.answers[("toolchain", "detach")] = lambda host, args: (0, "detached node", "") if "--force" in args else busy
    page.update_buttons()
    page.run_named["remove-toolchain"].click()
    assert picked.seen[0] == ("Remove toolchain from claude-1", ["jdk", "node"])  # the start's stays: not offered
    assert ui.asked[-1][0] == "toolchain-force" and "still in use" in ui.asked[-1][1]
    assert [call for call in sai.asked if call[1:3] == ("toolchain", "detach")] == [
        ("local", "toolchain", "detach", "claude-1", "node"),
        ("local", "toolchain", "detach", "claude-1", "node", "--force")]
    sai.answers[("toolchain", "list", "--run")] = (0, json.dumps([{"name": "go", "version": "1", "source": "start"}]),
                                                  "")
    page.remove_toolchain()
    assert "toolchains given at the start stay" in picked.notices[-1][1]


def test_the_backend_names_only_valid_runs_and_toolchains(sai):
    assert backend.valid_toolchain("go") and backend.valid_toolchain("jdk@21.0.13+9")
    for bad in ("Go", "bin", "go;rm", "--force", "go@", "", None):
        assert not backend.valid_toolchain(bad)
    assert sai.toolchain_attach("claude-1", "--offline")[0] == 2 and sai.toolchain_detach("x y", "go")[0] == 2
    assert sai.toolchain_add("../go")[0] == 2 and sai.asked == []
    sai.answers[("toolchain", "list", "--json")] = (0, json.dumps([GO, {"name": "BAD"}, "x"]), "")
    sai.answers[("toolchain", "catalog")] = (1, "", "sandbox-ai toolchain: not published there (yet)")
    found = sai.host_toolchains("local")
    assert [row["name"] for row in found["installed"]] == ["go"] and found["catalog"] == []
    assert "not published" in found["problem"]
    # A refusal of several lines keeps them (a message, printable_text), not "\\x0a"
    sai.answers[("toolchain", "catalog")] = (1, "", "sandbox-ai toolchain: not read:\n  - no network")
    assert "not read:\n  - no network" in sai.host_toolchains("local")["problem"]


def test_the_actions_are_on_the_rows_right_click_and_in_a_terminal_tabs_menu(window, monkeypatch):
    """The buttons of While it runs are what a row's menu and a terminal tab's While it runs submenu are made of."""
    page = window.sandboxes
    texts = [getattr(button, "label", "") or button.accessibleName() for button in page.run_buttons]
    assert "Add toolchain…" in texts and "Remove toolchain…" in texts
    instance = running()
    page.instances[instance.key] = instance
    monkeypatch.setattr(window, "select_quietly", lambda key: True)
    clicked = []
    monkeypatch.setattr(page, "target", lambda: clicked.append("add"))  # (add_toolchain asks for its target first)
    page.run_named["add-toolchain"].setEnabled(True)
    page.run_named["remove-toolchain"].setEnabled(False)
    menu = window.run_menu_for(window, instance)
    # (under Add… and Remove…, one level down since 0.1.234: the user's shorter menu)
    entries = {action.text(): action for top in menu.actions()
               for action in ([top] + (top.menu().actions() if top.menu() is not None else []))}
    assert entries["Add toolchain…"].isEnabled() and not entries["Remove toolchain…"].isEnabled()
    assert entries["Add toolchain…"].toolTip() == page.run_named["add-toolchain"].toolTip()  # with its reason
    # ... and the right click on a row of the Sandboxes page offers the same two buttons
    from sandbox_ai_app import widgets
    offered = [inner for entry in page.table.menu_entries()
               for inner in (entry.entries if isinstance(entry, widgets.MenuGroup) else [entry])]
    assert page.run_named["add-toolchain"] in offered and page.run_named["remove-toolchain"] in offered
    entries["Add toolchain…"].trigger()
    assert clicked == ["add"]


# --- versions (the user 2026-11-23: "add toolchain support for older toolchain versions") -------------------------


def jdk_row(version, toolchain=None, **more):
    row = {"name": "jdk", "version": version, "commands": ["java"], "hosts": [], "allow": [], "size": 300 << 20,
           **more}
    if toolchain:
        row["toolchain"] = toolchain
    return row


STORE_TWO = [jdk_row("17.0.21+9", "jdk@17"), jdk_row("21.0.13+9", "jdk")]
CATALOG_THREE = [jdk_row("21.0.13+9", "jdk"), jdk_row("17.0.21+9", "jdk@17"), jdk_row("11.0.33+10", "jdk@11")]


def test_every_version_is_offered_by_what_the_host_takes_for_it():
    choices = backend.toolchain_choices({"installed": STORE_TWO, "catalog": CATALOG_THREE})
    assert [(c["value"], c["version"], c["stored"]) for c in choices] == [
        ("jdk", "21.0.13+9", True), ("jdk@17", "17.0.21+9", True), ("jdk@11", "11.0.33+10", False)]
    # a toolchain attached is left out with all its versions (one version of a toolchain per sandbox)
    assert backend.toolchain_choices({"installed": STORE_TWO, "catalog": CATALOG_THREE}, attached={"jdk"}) == []
    # the catalog's newest while the store has an older one: never NAME alone (at a start that is the store's newest)
    only_17 = backend.toolchain_choices({"installed": [jdk_row("17.0.21+9", "jdk")], "catalog": CATALOG_THREE})
    assert [(c["value"], c["version"], c["stored"]) for c in only_17] == [
        ("jdk@17", "17.0.21+9", True), ("jdk@21", "21.0.13+9", False), ("jdk@11", "11.0.33+10", False)]
    # a host's sandbox-ai before versions names no "toolchain" and takes exact versions only: the store's newest by
    # its name, every other by its exact version
    old = backend.toolchain_choices({"installed": [jdk_row("21.0.9+10")],
                                     "catalog": [jdk_row("21.0.13+9"), jdk_row("17.0.21+9")]})
    assert [(c["value"], c["stored"]) for c in old] == [("jdk", True), ("jdk@21.0.13+9", False),
                                                        ("jdk@17.0.21+9", False)]
    assert [c["value"] for c in backend.toolchain_choices({"installed": [], "catalog": [jdk_row("21.0.13+9"),
                                                                                        jdk_row("17.0.21+9")]})] == \
        ["jdk", "jdk@17.0.21+9"]


def test_the_boards_toolchains_field_lists_every_version(sai, qapp, monkeypatch):
    from conftest import wait_until
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    monkeypatch.setattr(sai, "host_toolchains", lambda host="local": {
        "installed": STORE_TWO, "catalog": CATALOG_THREE, "signed": True, "problem": None})
    filled = []
    view.value_provider("--toolchain")(lambda items, note=None: filled.append((items, note)))
    wait_until(qapp, lambda: filled, 10.0)
    assert filled[0] == ([("jdk 21.0.13+9 (in the store)", "jdk"), ("jdk 17.0.21+9 (in the store)", "jdk@17"),
                          ("jdk 11.0.33+10 (add it first: sandbox-ai toolchain add jdk@11)", "jdk@11")], None)
    assert i18n.TEXTS["{name} {version} (add it first: sandbox-ai toolchain add {value})"]["de"] == \
        "{name} {version} (erst hinzufügen: sandbox-ai toolchain add {value})"
    view.deleteLater()


def test_add_toolchain_offers_each_version_and_attaches_the_one_chosen(ui, sai, picked, monkeypatch):
    page = page_on(ui, sai, monkeypatch, running())
    sai.answers[("toolchain", "list", "--json")] = (0, json.dumps(STORE_TWO), "")
    sai.answers[("toolchain", "catalog")] = catalog(*CATALOG_THREE)
    sai.answers[("toolchain", "list", "--run")] = (0, "[]", "")
    sai.answers[("toolchain", "attach")] = (0, "attached jdk 17.0.21+9", "")
    sai.answers[("toolchain", "add")] = (0, "added jdk 11.0.33+10", "")
    picked.want = "jdk 17.0.21+9"
    page.add_toolchain()
    assert picked.seen[0][1] == ["jdk 21.0.13+9: java", "jdk 17.0.21+9: java",
                                 "jdk 11.0.33+10 (downloaded from the signed catalog first)"]
    assert ran(sai, "local", "toolchain", "attach") == ("local", "toolchain", "attach", "claude-1", "jdk@17")
    assert ran(sai, "local", "toolchain", "add") is None
    sai.asked.clear()
    picked.want = "jdk 11"
    page.add_toolchain()
    assert ran(sai, "local", "toolchain", "add") == ("local", "toolchain", "add", "jdk@11")
    assert ran(sai, "local", "toolchain", "attach") == ("local", "toolchain", "attach", "claude-1", "jdk@11")
    # one version is attached: no other version of it is offered
    sai.answers[("toolchain", "list", "--run")] = (0, json.dumps([{"name": "jdk", "version": "17.0.21+9",
                                                                  "source": "added"}]), "")
    picked.seen.clear()
    page.add_toolchain()
    assert picked.seen == [] and "attached to it already" in picked.notices[-1][1]
