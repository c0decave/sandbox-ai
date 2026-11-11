"""The Kits & plugins page and the Board's kits node: against a fake inventory (what `sandbox-ai kits list
--json` prints), and end to end against the real `sandbox-ai kits` command when the checkout has it."""
import copy
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, kits_page, widgets  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures


@pytest.fixture
def wait(qapp, window):
    """conftest's wait, and the Kits page's change (a Task: one at a time) has ended too: the next action may go."""
    from conftest import wait_until
    return lambda condition, timeout=10.0: wait_until(qapp, lambda: condition() and not window.kits.task.running,
                                                      timeout)

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def agent_entry(**values):
    entry = {"path": "/k", "skills": [], "agents": [], "commands": [], "prompts": [], "memory": None,
             "marketplaces": {}, "plugins": [], "notes": [], "error": None}
    entry.update(values)
    return entry


SANDBOX_AI = {"source": "directory", "path": "/sandbox/marketplace"}
INVENTORY = {
    "version": 1, "kits_dir": "/home/u/.local/share/sandbox-ai/kits", "store": "/home/u/.local/share/sandbox-ai/plugins/claude",
    "kits": [
        {"name": "built-in", "kind": "always", "path": "/sai/builtin", "editable": False, "replaced": [],
         "description": None, "agents": {"claude": agent_entry(skills=["sandbox-ai"])}},
        {"name": "default", "kind": "default", "path": "/home/u/.local/share/sandbox-ai/home", "editable": False,
         "replaced": [], "description": None,
         "agents": {"claude": agent_entry(skills=["sandbox-example"], memory="CLAUDE.md")}},
        {"name": "egm", "kind": "builtin", "path": "/sai/builtin/kits/egm", "editable": False, "replaced": [],
         "description": "Built-in kit 'egm': the Elephant-Goldfish Model plugin",
         "agents": {"claude": agent_entry(marketplaces={"sandbox-ai": SANDBOX_AI}, plugins=["egm@sandbox-ai"])}},
        {"name": "mine", "kind": "user", "path": "/home/u/.local/share/sandbox-ai/kits/mine", "editable": True,
         "replaced": [], "description": None,
         "agents": {"claude": agent_entry(skills=["review"], plugins=["sandcastle@sandbox-ai"],
                                          marketplaces={"sandbox-ai": SANDBOX_AI}),
                    "codex": agent_entry(skills=["review"], error="skills/review: a symlink out of its kit entry")}},
    ],
    "marketplace": {"name": "sandbox-ai", "path": "/sai/marketplace", "target": "/sandbox/marketplace", "plugins": [
        {"name": "sandcastle", "id": "sandcastle@sandbox-ai", "description": "Demo: robots build sandcastles",
         "version": "1.0.0", "source": "local", "homepage": None, "kits": ["mine"]},
        {"name": "egm", "id": "egm@sandbox-ai", "description": "Elephant-Goldfish Model: design reviews",
         "version": None, "source": "github:google/elephant-goldfish-model", "homepage": "https://egm.dev",
         "kits": ["egm"]}]},
    "installed": {"marketplaces": ["sandbox-ai"], "plugins": ["sandcastle@sandbox-ai"]},
}
# (what `sandbox-ai plugins marketplaces --json` prints, small: the marketplace browser's own tests have the rest)
MARKETPLACES = {"version": 1, "store": "/s", "errors": [],
                "kits": [{"name": "default", "kind": "default", "editable": True, "agents": ["claude"]},
                         {"name": "egm", "kind": "builtin", "editable": False, "agents": ["claude"]},
                         {"name": "mine", "kind": "user", "editable": True, "agents": ["claude", "codex"]}],
                "agents": {"claude": {"marketplaces": True, "kits": True, "config": None, "about": None, "notes": []}},
                "marketplaces": [{"name": "sandbox-ai", "agent": "claude", "builtin": True, "origins": ["sandbox-ai"],
                                  "description": None, "source": None, "source_value": "sandbox-ai",
                                  "importable": True, "problem": None, "import": None, "kits": [], "store": None,
                                  "agent_config": None, "listed_from": "sandbox-ai", "plugins": [
                                      {"name": "egm", "id": "egm@sandbox-ai", "listed": True, "kits": [],
                                       "store": None, "agent": {"installed": False, "enabled": None}}]}]}


@pytest.fixture
def fake(window, monkeypatch, wait):
    """The window with the fake inventory loaded on the Kits page; kit commands are recorded."""
    calls = []
    monkeypatch.setattr(window.sai, "has_kits_command", lambda: True)
    monkeypatch.setattr(window.sai, "kits_inventory", lambda: copy.deepcopy(INVENTORY))
    monkeypatch.setattr(window.sai, "has_marketplaces", lambda: True)
    monkeypatch.setattr(window.sai, "marketplaces", lambda: copy.deepcopy(MARKETPLACES))
    monkeypatch.setattr(window.sai, "kit_command",
                        lambda action, name, *args, options=(): calls.append((action, name, *args, *options)) or
                        (0, "", ""))
    window.kits.refresh()
    assert wait(lambda: window.kits.table.rowCount() == 4 and window.kits.market.rowCount() > 0, 10)
    window.calls = calls
    return window


def select_kit(page, kind, name):
    page.table.selectRow(widgets.find_row(page.table, f"{kind}:{name}"))


def test_the_page_lists_every_kit_what_it_brings_and_the_marketplace(fake):
    page = fake.kits
    kinds = {widgets.row_key(page.table, r): page.table.item(r, 1).text() for r in range(page.table.rowCount())}
    assert kinds == {"always:built-in": "always (sandbox-ai)", "default:default": "always (default kit)",
                     "builtin:egm": "built-in", "user:mine": "yours"}
    row = widgets.find_row(page.table, "user:mine")
    assert page.table.item(row, 2).text() == "claude, codex"
    assert page.table.item(row, 3).text() == "skills: review · plugins: sandcastle@sandbox-ai"
    assert page.table.item(widgets.find_row(page.table, "builtin:egm"), 3).text() == "plugins: egm@sandbox-ai"
    assert page.table.item(row, 0).foreground().color().name() == widgets.QColor(widgets.theme.color("bad")).name(), \
        "a kit with a problem stands out"
    select_kit(page, "user", "mine")
    tree = [page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount())]
    texts = {top.text(0): [top.child(c).text(0) for c in range(top.childCount())] for top in tree}
    assert "Skills: 1" in texts["claude"] and "Plugins: 1" in texts["claude"]
    assert any(t.startswith("✘") and "symlink" in t for t in texts["codex"])
    assert "sandcastle@sandbox-ai" in page.summary.toolTip(), "the plugin store's plugins"
    assert widgets.find_row(page.market, "claude:sandbox-ai") >= 0, "the marketplace browser (its own tests below)"


def test_only_your_kits_can_be_changed_and_why(fake, env):
    page = fake.kits
    select_kit(page, "builtin", "egm")
    assert not any(b.isEnabled() for b in page.change_buttons)
    assert "read-only" in page.remove_button.toolTip() and "read-only" in page.delete_button.toolTip()
    assert all(b.isEnabled() for b in page.add_buttons), "adding goes to a kit of yours, chosen first"
    assert "read-only" in page.add_hint.text() and "a kit of yours" in page.add_hint.text()
    assert not page.use_button.isEnabled() and "Choose a project first" in page.use_button.toolTip()
    fake.set_project(project(env, "p"))
    select_kit(page, "builtin", "egm")
    assert page.use_button.isEnabled()
    select_kit(page, "default", "default")
    assert page.remove_button.isEnabled(), "the default kit's plugins can be removed"
    assert not page.delete_button.isEnabled() and "not deleted" in page.delete_button.toolTip()
    assert "edited in its folder" in page.add_hint.text() and not page.use_button.isEnabled()
    select_kit(page, "user", "mine")
    assert all(b.isEnabled() for b in page.edit_buttons) and page.use_button.isEnabled()
    assert page.add_hint.text() == ""


def fake_target(name, new):
    class Target:
        def __init__(self, parent, why, kits):
            Target.why, Target.kits = why, kits

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def chosen(self):
            return name, new
    return Target


def test_a_skill_added_with_a_built_in_kit_selected_goes_to_a_kit_of_yours(fake, env, monkeypatch, wait):
    """The complaint: with sandbox-ai's own kits only "Use in the current project" worked. Now adding asks
    which kit of yours receives it, and creates a new one first when wanted."""
    page, calls = fake.kits, fake.calls
    skill = env / "skill"
    skill.mkdir()
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: [str(skill)])
    target = fake_target("team", True)
    monkeypatch.setattr(kits_page, "TargetKitDialog", target)
    select_kit(page, "builtin", "egm")
    page.add_skill()
    assert wait(lambda: calls[-2:] == [("new", "team"), ("add-skill", "team", str(skill))], 10)
    assert "read-only" in target.why and target.kits == ["mine"]
    # An existing kit of yours: nothing is created.
    monkeypatch.setattr(kits_page, "TargetKitDialog", fake_target("mine", False))
    del calls[:]
    page.table.clearSelection()
    page.add_skill()
    assert wait(lambda: calls == [("add-skill", "mine", str(skill))], 10)
    # A name of yours typed as new is not created again.
    monkeypatch.setattr(kits_page, "TargetKitDialog", fake_target("mine", True))
    del calls[:]
    select_kit(page, "builtin", "egm")
    page.add_skill()
    assert wait(lambda: calls == [("add-skill", "mine", str(skill))], 10)
    # When creating the kit fails, nothing else runs and the reason is shown.
    monkeypatch.setattr(kits_page, "TargetKitDialog", fake_target("other", True))
    del calls[:]
    fake.sai.kit_command = lambda action, name, *a, options=(): (calls.append((action, name)) or
                                                                 (1, "", "sandbox-ai kits: no space left"))
    select_kit(page, "builtin", "egm")
    page.add_skill()
    assert wait(lambda: "no space left" in page.message.text(), 10), "said in the page's message area"
    assert page.message.property("role") == "bad" and page.message.isVisibleTo(page)
    assert calls == [("new", "other")]


def test_a_new_skill_for_a_new_kit_offers_every_agent(fake, monkeypatch, wait):
    page, calls = fake.kits, fake.calls
    monkeypatch.setattr(kits_page, "TargetKitDialog", fake_target("fresh", True))
    seen = {}

    class Skill:
        def __init__(self, parent, agents):
            seen["agents"] = agents
            self.name, self.description = QtWidgets.QLineEdit("lint"), QtWidgets.QLineEdit("when linting")
            self.agents = type("A", (), {"chosen": lambda self: ["claude"]})()

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(kits_page, "NewSkillDialog", Skill)
    select_kit(page, "always", "built-in")
    page.new_skill()
    assert wait(lambda: calls[-2:] == [("new", "fresh"), ("new-skill", "fresh", "lint", "--agent=claude",
                                                          "--description=when linting")], 10)
    assert seen["agents"] == list(kits_page.AGENTS)


def test_the_target_dialog_lists_your_kits_and_a_new_one(window):
    dialog = kits_page.TargetKitDialog(window, "why", ["a", "b"])
    assert [dialog.kit.itemText(i) for i in range(dialog.kit.count())] == ["a", "b", "A new kit…"]
    assert dialog.chosen() == ("a", False) and not dialog.name.isEnabled()
    dialog.kit.setCurrentIndex(2)
    dialog.name.setText(" team ")
    assert dialog.name.isEnabled() and dialog.chosen() == ("team", True)
    empty = kits_page.TargetKitDialog(window, "why", [])
    assert empty.chosen() == ("my-kit", True), "without a kit of yours, a new one is proposed"


def test_every_change_runs_sandbox_ai_kits(fake, env, monkeypatch, wait):
    page, calls = fake.kits, fake.calls
    skill = env / "skill"
    skill.mkdir()

    def dialog(**fields):
        class Fake:
            def __init__(self, *args):
                self.args = args
                for name, value in fields.items():
                    setattr(self, name, value)

            def exec(self):
                return QtWidgets.QDialog.DialogCode.Accepted
        return Fake

    def edit(text):
        line = QtWidgets.QLineEdit(text)
        line.currentText = line.text  # a combo box's way
        return line

    def agents(*names):
        return type("A", (), {"chosen": lambda self: list(names)})()
    monkeypatch.setattr(kits_page, "NewKitDialog", dialog(name=edit("team"), agents=agents("claude", "codex")))
    page.new_kit()
    assert wait(lambda: calls and calls[-1] == ("new", "team", "--agent=claude", "--agent=codex"), 10)
    select_kit(page, "user", "mine")
    monkeypatch.setattr("sandbox_ai_app.state.run_dialog", lambda dialog: [str(skill)])
    page.add_skill()
    assert wait(lambda: calls[-1] == ("add-skill", "mine", str(skill)), 10)
    monkeypatch.setattr(kits_page, "NewSkillDialog", dialog(name=edit("checks"), description=edit("-when reviewing"),
                                                            agents=agents("claude")))
    select_kit(page, "user", "mine")
    page.new_skill()
    assert wait(lambda: calls[-1] == ("new-skill", "mine", "checks", "--agent=claude",
                                      "--description=-when reviewing"), 10)
    monkeypatch.setattr(kits_page, "PluginDialog", dialog(plugin=edit("egm@sandbox-ai"), agent=edit("claude"),
                                                          source=edit("")))
    select_kit(page, "user", "mine")
    page.add_plugin()
    assert wait(lambda: calls[-1] == ("add-plugin", "mine", "egm@sandbox-ai", "--agent=claude"), 10)
    monkeypatch.setattr(kits_page, "MarketplaceDialog", dialog(name=edit("local"), source=edit(str(skill))))
    select_kit(page, "user", "mine")
    page.add_marketplace()
    assert wait(lambda: calls[-1] == ("add-marketplace", "mine", "local", str(skill)), 10), \
        "a folder goes to sandbox-ai, which refuses it with its reason"

    class Remove(dialog()):
        def chosen(self):
            return ("skill", "codex", "review")
    monkeypatch.setattr(kits_page, "RemoveDialog", Remove)
    select_kit(page, "user", "mine")
    page.remove()
    assert wait(lambda: calls[-1] == ("remove-skill", "mine", "review", "--agent=codex"), 10)
    select_kit(page, "user", "mine")
    page.delete()
    assert wait(lambda: calls[-1] == ("delete", "mine", "--yes"), 10)
    assert widgets.SHOWN[-1][0] == "Delete kit" and "mine" in widgets.SHOWN[-1][1], "asked first"
    # With the default kit selected, a plugin goes into it (no kit of yours asked for).
    monkeypatch.setattr(kits_page, "TargetKitDialog", None)
    select_kit(page, "default", "default")
    page.add_plugin()
    assert wait(lambda: calls[-1] == ("add-plugin", "default", "egm@sandbox-ai", "--agent=claude"), 10)
    assert "the kit default" in widgets.SHOWN[-1][1] and "On this computer" in widgets.SHOWN[-1][1], "asked first"
    # sandbox-ai's refusal is shown as it is.
    fake.sai.kit_command = lambda *a, **k: (1, "", "sandbox-ai kits: a marketplace from a folder is not supported")
    select_kit(page, "user", "mine")
    page.add_marketplace()
    assert wait(lambda: "folder is not supported" in page.message.text(), 10)


def test_names_are_checked_before_anything_runs(fake, monkeypatch):
    page = fake.kits
    page.valid_name("-x")
    assert widgets.SHOWN[-1][1] == "-x is not a valid name."
    assert not page.valid_name("Bad Name", backend.SKILL_NAME) and page.valid_name("good-name", backend.SKILL_NAME)
    assert fake.sai.__class__.kit_command(fake.sai, "new", "-rf")[0] == 2
    assert fake.sai.__class__.kit_command(fake.sai, "rm", "mine")[0] == 2
    assert fake.sai.__class__.kit_command(fake.sai, "add-skill", "mine", "")[0] == 2


def test_install_plugins_in_the_background_with_the_projects_built_in_kits(fake, env, monkeypatch, wait):
    """User report: installing opened a terminal tab that only showed the output. `sandbox-ai plugins claude` asks
    nothing: it runs in the background, its output goes to the log window, and a notification says how it went (a
    failure with the way to the log)."""
    folder = project(env, "p")
    (folder / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude",
                                                         "options": ["--kit", "egm", "--kit", "mine"]}))
    fake.set_project(folder)
    page = fake.kits
    assert page.project_kits == ["egm", "mine"]
    assert page.table.item(widgets.find_row(page.table, "builtin:egm"), 4).text() == "✔"
    assert page.table.item(widgets.find_row(page.table, "user:mine"), 4).text() == "✔"
    page.table.clearSelection()
    ran, notes = [], []
    answers = [(0, "", "sandbox-ai plugins: 1 plugin(s) in /x/plugins/claude; every Claude sandbox enables those "
                       "its kits list\n"),
               (1, "", "sandbox-ai plugins: not installed: sandcastle@sandbox-ai\n")]
    call = fake.sai.call
    monkeypatch.setattr(fake.sai, "call", lambda *args, **kw: (ran.append(args) or answers.pop(0))
                        if args[:2] == ("plugins", "claude") else call(*args, **kw))
    monkeypatch.setattr(fake, "notify", lambda title, text, level="warning", action=None: notes.append(
        (level, title, text, action)))
    started = len(fake.started)
    page.install(update=True)
    assert wait(lambda: notes and not page.task.running, 20)
    assert len(fake.started) == started, "no terminal"
    supports = "--kit" in (fake.sai.directory / "plugins.py").read_text()
    assert ran[-1] == (("plugins", "claude", "--update") + (("--kit", "egm") if supports else ())), \
        "only built-in kits, only where it can"
    assert notes[-1][:3] == ("success", "Plugins updated", "sandbox-ai plugins: 1 plugin(s) in /x/plugins/claude; "
                             "every Claude sandbox enables those its kits list")
    assert wait(lambda: "1 plugin(s) in /x/plugins/claude" in fake.log_panel.text.toPlainText(), 5), \
        "the output is in the log window"
    page.install(update=False)
    assert wait(lambda: len(notes) == 2 and not page.task.running, 20)
    level, title, text, action = notes[-1]
    assert (level, title, text) == ("error", "Plugins not installed",
                                    "sandbox-ai plugins: not installed: sandcastle@sandbox-ai")
    assert action[0] == "Show the log"
    action[1]()
    assert not fake.log_fold.is_folded()
    assert ran[-1][:2] == ("plugins", "claude") and "--update" not in ran[-1]


def test_the_page_s_actions_are_above_its_lists_grouped_and_on_their_context_menus(fake):
    """User report: the important buttons were at the bottom, out of sight, and "Add plugin" beside "Install plugins"
    did not say which does what. The plugin store's actions come first; each list's actions are above it; the
    additions to a kit are one menu; each list has them on its context menu too."""
    from sandbox_ai_app import kits_page
    page = fake.kits
    layout = page.layout()
    order = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget() is not None]
    store = next(w for w in order if isinstance(w, QtWidgets.QGroupBox))
    assert order.index(store) < order.index(page.tabs), "the plugin store before the lists"
    assert store.isAncestorOf(page.install_button) and page.install_button.text() == "Install the kits' plugins"
    assert "only declares" in page.store_hint.text()
    table_top = page.table.mapTo(page, page.table.rect().topLeft()).y()
    for button in (page.use_button, page.add_menu_button, page.remove_button, page.delete_button):
        assert button.mapTo(page, button.rect().topLeft()).y() < table_top or not page.isVisible()
    assert [action.text() for action in page.add_menu.actions()] == [
        "Skill from a folder…", "New skill…", "Plugin by its name…", "Marketplace…", "Hook…"]
    assert "Nothing is downloaded" in page.plugin_button.toolTip()
    select_kit(page, "user", "mine")
    menu = kits_page.context_menu(page.table, [page.use_button, page.add_menu, page.remove_button, None,
                                               page.delete_button])
    texts = [action.text() for action in menu.actions() if not action.isSeparator()]
    assert texts == ["Use in the current project…", "Add to the kit", "Remove from the kit…", "Delete kit…"]
    delete = next(action for action in menu.actions() if action.text() == "Delete kit…")
    assert delete.isEnabled() == page.delete_button.isEnabled()
    assert page.table.contextMenuPolicy() == QtWidgets.QWidget().contextMenuPolicy().CustomContextMenu
    assert page.market.contextMenuPolicy() == QtWidgets.QWidget().contextMenuPolicy().CustomContextMenu
    assert page.plugins.contextMenuPolicy() == QtWidgets.QWidget().contextMenuPolicy().CustomContextMenu


def test_using_a_kit_writes_the_project_file_after_review_and_trusts_it(fake, env, wait):
    folder = project(env, "p")
    (folder / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "options": ["--offline"]}))
    fake.set_project(folder)
    page = fake.kits
    select_kit(page, "builtin", "egm")
    page.use()
    assert wait(lambda: "egm" in (folder / ".sandbox-ai.json").read_text(), 20)
    document = json.loads((folder / ".sandbox-ai.json").read_text())
    options = document["options"]
    assert options[options.index("--kit") + 1] == "egm" and "--offline" in options
    title, text, details = next(s for s in widgets.SHOWN if s[0] == "Write and trust")
    # Review 5 (H2): the file in the folder was never trusted, so the whole file is shown (not a diff against what
    # whoever could write the folder put there), "--kit egm" in it.
    assert "Nothing was trusted for this file before" in details and "egm" in details, "shown before it was written"
    assert wait(lambda: fake.sai.project_state(folder)[0] == "trusted", 10)
    assert wait(lambda: not fake.board.dirty() and page.project_kits == ["egm"], 10), "applied: the board is clean"
    select_kit(page, "builtin", "egm")
    assert not page.use_button.isEnabled() and "uses it already" in page.use_button.toolTip()


def test_the_boards_kits_node_offers_the_kits_by_name(fake, env):
    board = fake.board
    assert board.kit_inventory is not None and len(board.kit_inventory["kits"]) == 4, "the Kits page shared it"
    board.add_node("kits")
    node = board.board.first("kits")
    board.inspect(node)
    boxes = {b.text(): b for b in board.inspector_holder.findChildren(QtWidgets.QCheckBox)}
    assert "egm (built-in)" in boxes and "mine" in boxes and "built-in" not in boxes and "default" not in boxes
    assert "Elephant-Goldfish" in boxes["egm (built-in)"].toolTip()
    boxes["egm (built-in)"].setChecked(True)
    assert board.board.first("kits").props["--kit"] == ["egm"]
    assert "--kit egm" in board.preview.toPlainText()
    # For Codex, egm (Claude only) cannot be chosen; a kit name that does not exist is shown as a problem.
    board.set_prop(board.board.first("agent").id, "agent", "codex")
    board.set_prop(board.board.first("kits").id, "--kit", ["nope"])
    board.inspect(board.board.first("kits"))
    boxes = {b.text(): b for b in board.inspector_holder.findChildren(QtWidgets.QCheckBox)}
    assert not boxes["egm (built-in)"].isEnabled() and "only for claude" in boxes["egm (built-in)"].toolTip()
    assert boxes["nope (not found)"].isChecked()
    boxes["nope (not found)"].setChecked(False)
    assert board.board.first("kits").props.get("--kit") in (None, [])


def test_the_kits_node_without_a_kits_command(window, monkeypatch, wait):
    monkeypatch.setattr(window.sai, "has_kits_command", lambda: False)
    board = window.board
    board.add_node("kits")
    board.inspect(board.board.first("kits"))
    assert wait(lambda: board.kit_inventory == {}, 10)
    board.inspect(board.board.first("kits"))
    labels = [w.text() for w in board.inspector_holder.findChildren(QtWidgets.QLabel)]
    assert any("type their names below" in t for t in labels)
    assert any(isinstance(w, QtWidgets.QPlainTextEdit) for w in board.inspector_holder.findChildren(QtWidgets.QWidget)), \
        "--kit can still be typed"
    window.kits.refresh()
    assert "no kits command" in window.kits.summary.text()


# -- end to end: the real sandbox-ai kits --


def test_kits_end_to_end_with_sandbox_ai(window, env, wait):
    sai = window.sai
    if not sai.has_kits_command():
        pytest.skip("this sandbox-ai has no kits command")
    skill = env / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: skill\ndescription: test\n---\n")
    (skill / "secret-link").symlink_to("/etc/hostname")
    assert sai.kit_command("new", "e2e", options=["--agent=claude"])[0] == 0
    status, out, err = sai.kit_command("add-skill", "e2e", str(skill))
    assert status == 0, err
    assert sai.kit_command("new-skill", "e2e", "checks", options=["--description=-when reviewing"])[0] == 0
    status, out, err = sai.kit_command("add-plugin", "e2e", "sandcastle@sandbox-ai", options=["--agent=claude"])
    assert status == 0, err
    status, out, err = sai.kit_command("add-marketplace", "e2e", "local", str(skill))
    assert status != 0 and "folder" in (out + err).lower(), "a folder marketplace is refused with the reason"
    inventory = sai.kits_inventory()
    kit = next(k for k in inventory["kits"] if k["name"] == "e2e")
    claude = kit["agents"]["claude"]
    assert kit["kind"] == "user" and sorted(claude["skills"]) == ["checks", "skill"]
    assert claude["plugins"] == ["sandcastle@sandbox-ai"] and "sandbox-ai" in claude["marketplaces"]
    copied = env / "data" / "sandbox-ai" / "kits" / "e2e" / "claude" / "skills" / "skill"
    assert (copied / "SKILL.md").is_file() and not os.path.lexists(copied / "secret-link"), "links are not copied"
    assert "sandcastle@sandbox-ai" in {p["id"] for p in inventory["marketplace"]["plugins"]}
    window.kits.refresh()
    assert wait(lambda: widgets.find_row(window.kits.table, "user:e2e") >= 0, 30)
    assert sai.kit_command("delete", "e2e", options=["--yes"])[0] == 0
    assert not (env / "data" / "sandbox-ai" / "kits" / "e2e").exists()


# -- hooks: shown, added, removed; approved only in a terminal, by the human --

HOOKS = {"path": "/k/claude/hooks", "sha256": "ab" * 32, "trusted": False, "notes": [], "errors": [],
         "hooks": [{"event": "PreToolUse", "matcher": "Bash", "command": "${HOOK_DIR}/check.sh", "timeout": None,
                    "files": ["check.sh"]}],
         "files": [{"path": "hooks.json", "size": 90, "sha256": "cd" * 32, "executable": False},
                   {"path": "check.sh", "size": 27, "sha256": "ef" * 32, "executable": True}]}


@pytest.fixture
def hooked(window, monkeypatch, wait):
    """The fake inventory with an unapproved hook in your kit mine (claude)."""
    inventory = copy.deepcopy(INVENTORY)
    inventory["kits"][3]["agents"]["claude"]["hooks"] = HOOKS
    calls = []
    monkeypatch.setattr(window.sai, "has_kits_command", lambda: True)
    monkeypatch.setattr(window.sai, "has_kit_hooks", lambda: True)
    monkeypatch.setattr(window.sai, "kits_inventory", lambda: copy.deepcopy(inventory))
    monkeypatch.setattr(window.sai, "has_marketplaces", lambda: True)
    monkeypatch.setattr(window.sai, "marketplaces", lambda: copy.deepcopy(MARKETPLACES))
    monkeypatch.setattr(window.sai, "kit_command",
                        lambda action, name, *args, options=(): calls.append((action, name, *args, *options)) or
                        (0, "", ""))
    window.kits.refresh()
    assert wait(lambda: window.kits.table.rowCount() == 4, 10)
    window.calls = calls
    return window


def test_a_kits_hooks_are_shown_with_their_approval(hooked):
    page = hooked.kits
    row = widgets.find_row(page.table, "user:mine")
    assert "hooks: 1 (not approved)" in page.table.item(row, 3).text()
    select_kit(page, "user", "mine")
    claude = next(page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount())
                  if page.tree.topLevelItem(i).text(0) == "claude")
    node = next(claude.child(c) for c in range(claude.childCount()) if claude.child(c).text(0).startswith("Hooks"))
    assert node.text(0) == "Hooks: 1 (not approved: they do not run)"
    assert node.child(0).text(0) == "PreToolUse · Bash: ${HOOK_DIR}/check.sh"
    assert page.trust_button.isEnabled() and page.hook_button.isEnabled()
    select_kit(page, "builtin", "egm")
    assert not page.trust_button.isEnabled() and page.trust_button.toolTip() == widgets.safe_tooltip("The kit has no hooks.")


def test_approving_hooks_opens_trust_hooks_in_a_terminal_without_yes(hooked, monkeypatch):
    page = hooked.kits
    select_kit(page, "user", "mine")
    # Trusted hooks run in every later start with the kit (kit-hooks-trust): asked first, Cancel opens nothing.
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    started = len(hooked.started)
    page.trust_hooks()
    assert widgets.SHOWN[-1][0] == "Review and approve hooks…" and "every later start" in widgets.SHOWN[-1][1]
    assert len(hooked.started) == started, "not trusted: no terminal"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.trust_hooks()
    argv = hooked.started[-1]
    assert argv[0] == "term" and argv[4:] == ["kits", "trust-hooks", "--", "mine"], "the review and the yes are there"
    assert "--yes" not in argv and hooked.calls == [], "the app approves nothing itself"
    assert "then Refresh" in page.summary.text()
    with pytest.raises(ValueError):
        hooked.sai.__class__.trust_hooks_argv(hooked.sai, "-x")


def test_hooks_are_added_and_removed_with_sandbox_ai_kits(hooked, env, monkeypatch, wait):
    page, calls = hooked.kits, hooked.calls
    script = env / "check.sh"
    script.write_text("#!/bin/sh\n")

    real = kits_page.HookDialog  # the dialog itself; the stand-in below replaces it in the module

    class Hook:
        def __init__(self, parent, agents):
            self.dialog = real(parent, agents)
            self.dialog.file.setText(str(script))
            self.dialog.matcher.setText("Write|Edit")
            self.dialog.event_box.setCurrentText("PostToolUse")
            self.dialog.agents.boxes["codex"].setChecked(False)
            self.file, self.agents, self.options = self.dialog.file, self.dialog.agents, self.dialog.options

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(kits_page, "HookDialog", Hook)
    select_kit(page, "user", "mine")
    page.add_hook()
    assert wait(lambda: calls and calls[-1] == ("add-hook", "mine", str(script), "--agent=claude",
                                                "--event=PostToolUse", "--matcher=Write|Edit"), 10)

    class Remove:
        def __init__(self, parent, entries):
            assert ("hook", "claude", "check.sh") in entries and ("hook", "claude", "hooks.json") not in entries

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def chosen(self):
            return ("hook", "claude", "check.sh")
    monkeypatch.setattr(kits_page, "RemoveDialog", Remove)
    select_kit(page, "user", "mine")
    page.remove()
    assert wait(lambda: calls[-1] == ("remove-hook", "mine", "check.sh", "--agent=claude"), 10)


def test_a_hook_file_of_definitions_takes_no_event_or_matcher(window):
    dialog = kits_page.HookDialog(window, ["claude"])
    dialog.file.setText("/x/hooks.json")
    dialog.matcher.setText("Bash")
    assert dialog.options() == ["--agent=claude"]
    dialog.file.setText("/x/check.sh")
    assert dialog.options() == ["--agent=claude", "--matcher=Bash"]


def test_kit_hooks_end_to_end_with_sandbox_ai(window, env):
    sai = window.sai
    if not sai.has_kit_hooks():
        pytest.skip("this sandbox-ai has no kit hooks")
    script = env / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    assert sai.kit_command("new", "e2e-hooks", options=["--agent=claude"])[0] == 0
    try:
        status, out, err = sai.kit_command("add-hook", "e2e-hooks", str(script), options=["--matcher=Bash"])
        assert status == 0, err
        kit = next(k for k in sai.kits_inventory()["kits"] if k["name"] == "e2e-hooks")
        hooks = kit["agents"]["claude"]["hooks"]
        assert hooks["trusted"] is False and hooks["hooks"][0]["matcher"] == "Bash"
        assert kits_page.hook_files(kit["agents"]["claude"]) == ["check.sh"]
        assert sai.kit_command("remove-hook", "e2e-hooks", "check.sh", options=["--agent=claude"])[0] == 0
    finally:
        sai.kit_command("delete", "e2e-hooks", options=["--yes"])


def test_several_kits_are_deleted_at_once_and_only_yours(fake, monkeypatch, wait):
    """Ctrl/Shift: several kits; Delete kit… (and the Delete key) deletes your own of them after one question naming
    them (sandbox-ai's own are left out, said); what acts on one kit waits."""
    page, calls = fake.kits, fake.calls
    inventory = copy.deepcopy(INVENTORY)
    ours = copy.deepcopy(inventory["kits"][-1])
    ours["name"], ours["path"] = "ours", "/home/u/.local/share/sandbox-ai/kits/ours"
    inventory["kits"].append(ours)
    monkeypatch.setattr(fake.sai, "kits_inventory", lambda: copy.deepcopy(inventory))
    monkeypatch.setattr(widgets, "SHOWN", [])
    page.refresh()
    assert wait(lambda: page.table.rowCount() == 5, 10)
    assert page.table.selectionMode() == QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
    widgets.select_keys(page.table, ["builtin:egm", "user:mine", "user:ours"])
    assert len(page.chosen_kits()) == 3
    assert page.delete_button.isEnabled()
    assert not page.use_button.isEnabled() and not page.remove_button.isEnabled() and \
        "3 are selected" in page.remove_button.toolTip()
    next(a for a in page.table.actions() if a.objectName() == "deleteAction").trigger()  # the Delete key
    question = widgets.SHOWN[0][1]
    assert "Delete these 2 kits" in question and "- mine\n- ours" in question and "Left out" in question
    assert wait(lambda: ("delete", "ours", "--yes") in calls, 10)
    assert [c for c in calls if c[0] == "delete"] == [("delete", "mine", "--yes"), ("delete", "ours", "--yes")]
