"""2026-11-12: sandbox-ai's presets in the gallery by sandbox-ai's own categories (one list, its order, English and
German titles and descriptions; its presets' German titles), the new presets there (Claude and Codex without questions
on their workspace with a brain, the typical ones that were missing), a preset with a brain folder asks for it and gives
it to its MCP server; and a board saved as a preset of yours with everything it has (values, folders as defaults,
services, MCP limits and folders, LLM parameters, rules, notes, agent arguments, positions), updated, renamed - round
trip board -> preset -> gallery -> board."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import backend, board as B, i18n, presets as PR, widgets  # noqa: E402
from sandbox_ai_app.board_view import PathEdit  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
NEW = ("coding-memory", "research-memory", "rust", "go", "jvm", "dotnet", "database-services", "web-frontend",
       "infrastructure", "dependency-upgrade", "pentest-lab", "paranoid")


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "project").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(PR, "HOST_DOCUMENTS", {})
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "vpn_profiles", lambda: [])
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [])
    try:
        categories = sai.module("presets").CATEGORY_INFO
    except Exception:  # noqa: BLE001
        pytest.skip("this sandbox-ai has no preset categories")
    yield sai, tmp_path, categories
    i18n.set_language("en")


def gallery_of(sai, root, board=None):
    from sandbox_ai_app.preset_gallery import PresetGallery
    return PresetGallery(sai, str(root / "project"), "claude", {}, board)


def test_sandbox_ais_presets_are_grouped_by_its_categories_in_their_order(env):
    sai, root, categories = env
    gallery = gallery_of(sai, root)
    keys = [key for _, _, _, key in gallery.sections]
    sai_keys = [key for key in keys if key.startswith(PR.SAI_CATEGORY + ":")]
    order = list(categories)
    assert sai_keys == [f"{PR.SAI_CATEGORY}:{key}" for key in order if f"{PR.SAI_CATEGORY}:{key}" in sai_keys]
    assert keys.index(sai_keys[0]) > keys.index("teams") and keys[-1] == "mine", "the app's first, yours last"
    assert PR.SAI_CATEGORY not in keys, "no one section of all of sandbox-ai's any more"
    by_key = {key: [c.preset.name for c in cards] for _, _, cards, key in gallery.sections}
    for name, category in (("coding-memory", "development"), ("research-memory", "research"),
                           ("pentest-lab", "security"), ("paranoid", "privacy"), ("forensics", "forensics"),
                           ("software-analysis", "forensics"), ("mobile-android", "hardware"), ("ollama", "llm"),
                           ("docs-writer", "writing"), ("sand-chess", "fun"), ("chaos-monkey", "testing")):
        assert name in by_key[f"{PR.SAI_CATEGORY}:{category}"], (name, category)
    every = {name for names in by_key.values() for name in names}
    assert set(NEW) <= every
    # Each section says what its category is: under its heading, and as the category's tooltip on the left.
    heading = next(h for h, _, _, key in gallery.sections if key == "sandbox-ai:forensics")
    assert heading.text() == "sandbox-ai: Forensics & reverse engineering"
    assert gallery.section_notes["sandbox-ai:forensics"].text() == categories["forensics"]["description"]["en"]
    item = next(gallery.categories.item(r) for r in range(gallery.categories.count())
                if gallery.categories.item(r).data(Qt.ItemDataRole.UserRole) == "sandbox-ai:forensics")
    assert categories["forensics"]["description"]["en"] in item.toolTip() and item.text().endswith("(2)")
    # The search finds a category by its name; its description goes with its section.
    gallery.search.setText("reverse engineering")
    shown = {c.preset.name for c in gallery.visible_cards()}
    assert {"forensics", "software-analysis"} <= shown
    assert gallery.section_notes["sandbox-ai:forensics"].isVisibleTo(gallery) or not gallery.isVisible()
    gallery.search.setText("zzz-nothing")
    assert all(note.isHidden() for note in gallery.section_notes.values())
    card = next(c for c in gallery.cards if c.preset.name == "rust")
    gallery.search.setText("")
    gallery.choose(card)
    assert gallery.meta.text().startswith("Development · ")
    gallery.deleteLater()


def test_in_german_the_categories_and_sandbox_ais_titles_are_german(env):
    sai, root, categories = env
    i18n.set_language("de")
    gallery = gallery_of(sai, root)
    headings = {key: h.text() for h, _, _, key in gallery.sections}
    assert headings["sandbox-ai:development"] == "sandbox-ai: Entwicklung"
    assert headings["sandbox-ai:forensics"] == "sandbox-ai: Forensik & Reverse Engineering"
    assert gallery.section_notes["sandbox-ai:llm"].text() == categories["llm"]["description"]["de"]
    card = next(c for c in gallery.cards if c.preset.name == "coding-memory")
    assert card.preset.text(card.preset.title) == "Programmieren mit Gedächtnis"
    gallery.choose(card)
    assert gallery.title.text() == "Programmieren mit Gedächtnis" and "Brain-Ordner" in gallery.description.text()
    assert gallery.meta.text().startswith("Entwicklung · ")
    gallery.search.setText("Gedächtnis")  # the German title is searched, and the English one
    assert "coding-memory" in {c.preset.name for c in gallery.visible_cards()}
    gallery.search.setText("Coding with memory")
    assert "coding-memory" in {c.preset.name for c in gallery.visible_cards()}
    gallery.deleteLater()


def test_a_sandbox_ai_without_categories_has_its_presets_in_one_section(env, monkeypatch):
    sai, root, _ = env
    monkeypatch.setattr(PR, "sai_categories", lambda sai: [])
    catalogue = PR.catalogue(sai)
    keys = [key for key, _, presets in catalogue.groups() if presets]
    assert PR.SAI_CATEGORY in keys and not [k for k in keys if k.startswith(PR.SAI_CATEGORY + ":")]
    found = {p.name: p for p in catalogue.own()}
    assert found["coding-memory"].group == "development", "the category is kept for a newer one to group by"


def test_a_preset_with_a_brain_folder_asks_for_it_and_gives_it_to_its_server(env, tmp_path):
    """coding-memory: the form asks for the brain folder (a folder field, no default); filled in, the board starts with
    -D and --mcp-mount engram:/data=FOLDER:rw, and preparing the start adds engram with that folder."""
    sai, root, _ = env
    from sandbox_ai_app.preset_gallery import PlaceholderForm
    from sandbox_ai_app.prerequisites import mcp_add_args
    preset = PR.catalogue(sai).find("sandbox-ai:sai-coding-memory")
    assert [p.marker for p in preset.placeholders] == ["<folder:brain>"] and not preset.placeholders[0].default
    form = PlaceholderForm(sai, preset)
    field = form.fields["<folder:brain>"]
    assert isinstance(field, PathEdit) and field.edit.text() == ""
    brain = tmp_path / "brains" / "app"
    form.set_value("<folder:brain>", str(brain))
    values = form.values()
    assert values == {"<folder:brain>": str(brain)}
    board = PR.build(sai, preset, root / "project", agent="codex", values=values)
    argv = B.to_argv(sai, board)
    assert "--dangerously-skip-permissions" in argv and f"engram:/data={brain}:rw" in argv
    assert [n.props["name"] for n in board.of_kind("mcp")] == ["engram"] and not B.placeholders(board)
    assert mcp_add_args(argv, "engram") == ("mcp", "add", "engram", "--mount", f"/data={brain}:rw")
    assert mcp_add_args(argv, "web-fetch") == ("mcp", "add", "web-fetch")
    assert mcp_add_args(["--mcp-mount", "engram:/data=<folder:brain>:rw"], "engram") == ("mcp", "add", "engram")
    unfilled = PR.build(sai, preset, root / "project", agent="codex")
    assert [m for m, _ in B.placeholders(unfilled)] == ["<folder:brain>"], "left: shown as to fill in"
    form.deleteLater()


# --- a board saved as a preset of yours, with everything it has -------------------------------------------------

def rich_board(sai, root):
    """A board with workspaces, a mount, an MCP server with limits and a folder of its own, a service, an LLM
    parameter, a ruleset and the agent's arguments."""
    for name in ("project", "lib", "data", "brain"):
        (root / name).mkdir(exist_ok=True)
    argv = ["--workspace", str(root / "lib"), "--mount", f"{root / 'data'}:/mnt/data:ro", "--allow-pypi",
            "--mcp", "engram", "--mcp-limits", "engram:memory=1g", "--mcp-mount", f"engram:/data={root / 'brain'}:rw",
            "--service", "db:image=postgres,port=5432,env=POSTGRES_PASSWORD=dev", "--memory", "4g"]
    board = B.from_argv(sai, "claude", argv, ["--model", "sonnet"], [str(root / "project")])
    (board.first("approvals") or board.add("approvals")).props["rules"] = dict(PR.GITHUB_RULES)
    board.layout(20)
    return board


def same(sai, one, other):
    positions = lambda board: sorted((n.kind, n.x, n.y) for n in board.nodes)  # noqa: E731
    assert B.to_argv(sai, one) == B.to_argv(sai, other)
    assert B.values(one)[2] == B.values(other)[2] and B.project_rules(one) == B.project_rules(other)
    assert positions(one) == positions(other), "the layout kept"


def test_a_saved_board_comes_back_the_same_through_the_gallery(env):
    """Round trip: board -> Save as preset (folders and values kept) -> the gallery -> Create (the form offers every
    folder with its path of then) -> the same options, values, ruleset, agent arguments and positions."""
    sai, root, _ = env
    board = rich_board(sai, root)
    choices = {"title": "My memory board", "description": "Line one\nline two", "category": "development",
               "notes": ["Ask Jo for the brain", "Postgres from images pull"], "keep": True}
    from sandbox_ai_app.preset_gallery import save_board_preset
    preset = save_board_preset(None, sai, board, choices)
    assert preset is not None and preset.id == "my-my-memory-board" and preset.description == "Line one\nline two"
    folders = {p.marker: p.default for p in preset.placeholders}
    assert folders == {"<folder:workspace>": str(root / "project"), "<folder:workspace-2>": str(root / "lib"),
                       "<folder:mount>": str(root / "data"), "<folder:engram-data>": str(root / "brain")}
    assert "--workspace" in preset.options and str(root) not in " ".join(preset.options), "no path wired in"
    assert preset.notes[:2] == ("Ask Jo for the brain", "Postgres from images pull")
    assert "paths of this computer" in preset.notes[2] and str(root / "brain") in preset.notes[2]
    stored = json.loads(PR.store_path().read_text())["presets"][0]
    assert stored["category"] == "development" and stored["notes"][0] == "Ask Jo for the brain"
    gallery = gallery_of(sai, root)
    card = next(c for c in gallery.cards if c.preset.key == preset.key)
    gallery.choose(card)
    assert gallery.meta.text().startswith("Development · ") and gallery.rename_button.isEnabled()
    assert gallery.create() and gallery.result_[0].key == preset.key
    again = PR.build(sai, gallery.result_[0], root / "project", agent="claude", values=gallery.result_[1])
    same(sai, board, again)
    assert {n.props.get("path") for n in again.of_kind("workspace")} == {str(root / "project"), str(root / "lib")}
    # Another project: its form offers the folders of then, to change; the gallery's project is not added to them.
    other = root / "other"
    other.mkdir()
    moved = PR.build(sai, preset, other, agent="claude", values={**folders, "<folder:workspace>": str(other)})
    assert [n.props.get("path") for n in moved.of_kind("workspace")] == [str(other), str(root / "lib")]
    gallery.deleteLater()


def test_without_its_folders_a_saved_board_asks_for_them_anew(env):
    sai, root, _ = env
    board = rich_board(sai, root)
    preset = PR.from_board(sai, board, "Anywhere", keep=False)
    assert "--workspace" not in preset.options
    assert {p.marker: p.default for p in preset.placeholders} == {"<folder:mount>": "", "<folder:engram-data>": ""}
    assert not any("paths of this computer" in note for note in preset.notes)
    built = PR.build(sai, preset, root / "other")
    assert [n.props.get("path") for n in built.of_kind("workspace")] == [str(root / "other")]
    assert {m for m, _ in B.placeholders(built)} == {"<folder:mount>", "<folder:engram-data>"}
    assert "--service" in B.to_argv(sai, built) and "engram:memory=1g" in B.to_argv(sai, built)


def test_a_placeholder_left_on_the_board_keeps_its_name_and_label(env):
    """A board with a placeholder still to fill in (a preset's, with its label) and a folder filled in: the saved preset
    keeps the first with the preset's label, and the folder gets a name of its own (never the same marker)."""
    sai, root, _ = env
    (root / "real").mkdir()
    board = B.from_argv(sai, "claude", ["--mount", "<folder:mount>:/mnt/a:ro", "--mount", f"{root / 'real'}:/mnt/b:ro"])
    board.labels = {"<folder:mount>": ["Samples of the case", "a folder of yours"]}
    preset = PR.from_board(sai, board, "Two mounts", keep=True)
    marks = {p.marker: (p.label, p.hint, p.default) for p in preset.placeholders}
    assert marks["<folder:mount>"] == ("Samples of the case", "a folder of yours", "")
    assert marks["<folder:mount-2>"] == ("Folder mounted at /mnt/b", "", str(root / "real"))
    assert "<folder:mount>:/mnt/a:ro" in preset.options and "<folder:mount-2>:/mnt/b:ro" in preset.options


def test_saving_a_new_preset_never_replaces_one_of_yours_unasked(env):
    """A preset's id came from its title alone: a second board saved under the same title (or one that reads the same:
    "Work!" and "work"; any title of only non-Latin letters: "my-preset") replaced the first without a word - also when
    "Replace" was left unticked. A new one gets an id of its own; only Replace (asked) keeps the old id."""
    sai, root, _ = env
    from sandbox_ai_app.preset_gallery import save_board_preset
    first = save_board_preset(None, sai, B.from_argv(sai, "claude", ["--allow-pypi"]), {"title": "Work", "keep": True})
    second = save_board_preset(None, sai, B.from_argv(sai, "codex", ["--allow-npm"]), {"title": "work!", "keep": True})
    russian = save_board_preset(None, sai, B.from_argv(sai, "claude", []), {"title": "Работа", "keep": True})
    greek = save_board_preset(None, sai, B.from_argv(sai, "claude", []), {"title": "Εργασία", "keep": True})
    ids = [p.id for p in (first, second, russian, greek)]
    assert len(set(ids)) == 4, ids
    assert ids[:2] == ["my-work", "my-work-2"]
    mine, _ = PR.load_mine(sai)
    assert sorted(p.id for p in mine) == sorted(ids), "every one kept"
    replaced = save_board_preset(None, sai, B.from_argv(sai, "claude", ["--allow-npm"]),
                                 {"title": "Work", "keep": True, "replace": True}, replacing=first)
    assert replaced.id == "my-work" and len(PR.load_mine(sai)[0]) == 4


def test_a_saved_board_never_keeps_a_secret_nor_a_workspace_wired_in(env):
    sai, root, _ = env
    from sandbox_ai_app.preset_gallery import save_board_preset
    board = B.from_argv(sai, "claude", ["--allow-pypi"], ["--api-key", "sk-ant-api03-xyz"])
    assert save_board_preset(None, sai, board, {"title": "Leaky", "keep": True}) is None
    assert widgets.SHOWN[-1][0] == "Not saved" and "secret" in widgets.SHOWN[-1][1]
    document = {**PR.to_document(PR.from_board(sai, rich_board(sai, root), "Fine", keep=True)), "id": "wired"}
    document["options"] = [*document["options"], "--workspace", "/etc"]
    with pytest.raises(ValueError, match="--workspace"):
        PR.from_document(sai, document)


def test_the_board_page_saves_and_updates_the_preset_it_came_from(env, monkeypatch):
    """Save as preset… on the Board (toolbar, context menu, File menu): a board made from a preset of yours replaces it
    after asking (declined: nothing changes), unticked it is saved as a new one."""
    from sandbox_ai_app import preset_gallery
    from sandbox_ai_app.board_view import BoardPage
    sai, root, _ = env
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    page.directory = str(root / "project")
    assert page.save_preset_button.text() == "Save as preset…"
    window = (PR.Path(__file__).resolve().parents[1] / "sandbox_ai_app" / "ui.py").read_text()
    assert '"save-preset", tr("Save the board as a preset…"), lambda: self.board.save_as_preset()' in window
    page.board = rich_board(sai, root)
    first = page.save_as_preset({"title": "Mine", "description": "", "category": "testing", "notes": [], "keep": True})
    assert first is not None and [p.id for p in PR.load_mine(sai)[0]] == ["my-mine"]
    from PySide6.QtCore import QPointF
    menu = page.canvas_menu(QPointF(-5000, -5000))  # the canvas, where no part is
    assert "Save as preset…" in [action.text() for action in menu.actions()]
    page.new_from_preset(first, {p.marker: p.default for p in first.placeholders})
    assert page.own_preset is not None and page.own_preset.id == "my-mine"
    page.board.first("limits").props["--memory"] = "6g"
    asked = []
    monkeypatch.setattr(preset_gallery, "ask_first", lambda parent, action, *a, **k: asked.append(action) or False)
    choices = {"title": "Mine", "description": "more", "category": "testing", "notes": [], "keep": True,
               "replace": True}
    assert page.save_as_preset(choices) is None and asked == ["preset-replace"]
    assert "6g" not in json.dumps(PR.to_document(PR.load_mine(sai)[0][0])), "declined: as it was"
    monkeypatch.setattr(preset_gallery, "ask_first", lambda parent, action, *a, **k: True)
    updated = page.save_as_preset(choices)
    assert updated.id == "my-mine" and "6g" in updated.options and updated.description == "more"
    assert [p.id for p in PR.load_mine(sai)[0]] == ["my-mine"], "replaced, not added"
    copy = page.save_as_preset({**choices, "title": "Mine too", "replace": False})
    assert copy.id == "my-mine-too" and len(PR.load_mine(sai)[0]) == 2

    page.deleteLater()


def test_the_dialog_and_rename(env):
    from sandbox_ai_app.preset_gallery import SavePresetDialog, preset_categories
    sai, root, categories = env
    assert [key for key, _ in preset_categories(sai)] == list(categories), "sandbox-ai's categories"
    dialog = SavePresetDialog(None, sai)
    assert dialog.keep.isChecked() and dialog.replace.isHidden(), "values and folders kept by default"
    dialog.name.setText(" Two lines ")
    dialog.description.setPlainText("first\nsecond")
    dialog.notes.setPlainText("one\n\n two ")
    dialog.category.setCurrentIndex(dialog.category.findData("research"))
    assert dialog.choices() == {"title": "Two lines", "description": "first\nsecond", "category": "research",
                                "notes": ["one", "two"], "keep": True, "replace": False}
    preset = PR.from_board(sai, rich_board(sai, root), "Original", "d", "development", keep=True)
    PR.add_mine(sai, preset)
    for_it = SavePresetDialog(None, sai, preset)
    assert not for_it.replace.isHidden() and for_it.replace.isChecked() and for_it.name.text() == "Original"
    edit = SavePresetDialog(None, sai, preset, edit=True)
    assert edit.windowTitle() == "Change the preset"
    gallery = gallery_of(sai, root)
    gallery.choose(next(c for c in gallery.cards if c.preset.key == preset.key))
    renamed = gallery.rename_preset({"title": "Renamed", "description": "new\ntext", "category": "research",
                                     "notes": ["n"]})
    assert renamed.id == preset.id and renamed.title == "Renamed" and renamed.options == preset.options
    assert [p.title for p in PR.load_mine(sai)[0]] == ["Renamed"] and gallery.selected.preset.title == "Renamed"
    sai_card = next(c for c in gallery.cards if c.preset.source == "sandbox-ai")
    gallery.choose(sai_card)
    assert not gallery.rename_button.isEnabled() and gallery.rename_preset({"title": "x"}) is None
    for widget in (dialog, for_it, edit, gallery):
        widget.deleteLater()
