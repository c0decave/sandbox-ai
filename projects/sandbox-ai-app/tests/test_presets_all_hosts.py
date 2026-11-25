"""The presets window over every host (todos 2026-11-24): "warum habe ich mehr presets auf coding-101" - a host with
presets of its own there (the private presets installed in ~/.config/sandbox-ai/presets, a preset directory) lists
more; "presets sollten im grunde zusammengefasst angezeigt werden, gerne kann man host auch bei presets auswählen, per
default alle zusammen und per default sollten presets auch wenn sie z.b. nur auf 101 existieren auf anderen hosts
ausfuehrbar sein" - one list over all hosts (each preset once, where it comes from on its card), a host filter, a
preset of one host made into a board on another (its ruleset read from its host; what the board's host cannot do
refused with why); "das preset fenster sollte fast so groß sein wie das hauptfenster und zentriert";
"kategorienueberschriften auf der rechten seite unterstreichen"; "beim durchklicken ... verrutscht auch die rechte
seite obwohl man gar nicht hoch oder runter geht"."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import backend, presets as PR, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

PRIVATE = {"format": "sandbox-ai preset", "version": 1, "id": "tor-researcher", "title": "Tor researcher",
           "description": "Research in the Tor network", "category": "privacy", "agents": ["claude"],
           "options": ["--offline"], "rules": "tor-researcher.rules.json", "origin": "yours"}
TEAM = {"format": "sandbox-ai preset", "version": 1, "id": "team-tool", "title": "Team tool", "description": "d",
        "category": "development", "options": ["--allow-pypi"], "origin": "extra", "directory": "/srv/team-presets"}


@pytest.fixture
def sai(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(PR, "HOST_DOCUMENTS", {})
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    sai = backend.SandboxAI()
    sai.asked, sai.answers = [], {}

    def host_run(name, *args, timeout=120, input=None):
        sai.asked.append((name, *args))
        return sai.answers.get((name, *args[:2]), (255, "", f"ssh: {name}: no route to host"))
    monkeypatch.setattr(sai, "host_run", host_run)
    monkeypatch.setattr(sai, "hosts_supported", lambda: True)
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"},
                                                      {"name": "coding-101", "kind": "ssh"},
                                                      {"name": "ct2", "kind": "ssh"}])
    local = {o for action in sai.parser_actions() for o in action.option_strings}
    sai.known = {"coding-101": local, "ct2": local - {"--offline"}}
    monkeypatch.setattr(sai, "host_launch_options", lambda name, agent: sai.asked.append((name, "options")) or
                        sai.known[name])
    from sandbox_ai_app import preset_gallery
    monkeypatch.setattr(preset_gallery, "background", at_once)
    published = [{k: v for k, v in d.items() if not k.startswith("_")} | {"origin": "built-in"}
                 for d in PR.local_documents(sai)[0]]
    sai.answers[("coding-101", "presets", "list")] = (0, json.dumps(published + [PRIVATE, TEAM]), "")
    sai.answers[("ct2", "presets", "list")] = (0, json.dumps(published), "")
    return sai


def at_once(function, *args, done, **kw):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        done(None, exc)
        return
    done(result, None)


def gallery_of(sai, host="local", parent=None):
    from sandbox_ai_app.preset_gallery import PresetGallery
    return PresetGallery(sai, None, "claude", {}, None, parent, host=host)


def card(gallery, name):
    return next(c for c in gallery.cards if c.preset.name == name)


def test_why_a_host_lists_more_presets_its_own_there_are_shown_as_its_own(sai):
    """coding-101 has presets of its own (yours there, a preset directory): one list over all hosts shows them once,
    each card saying where it comes from; the built-in ones are on every host."""
    gallery = gallery_of(sai)
    assert ("coding-101", "presets", "list", "--json") in sai.asked and ("ct2", "presets", "list", "--json") in sai.asked
    keys = [c.preset.key for c in gallery.cards]
    assert len(keys) == len(set(keys)), "each preset once"
    private, team = card(gallery, "tor-researcher"), card(gallery, "team-tool")
    assert PR.from_hosts(private.preset) == ("coding-101",)
    assert private.where.text() == "On coding-101 (yours there)"
    assert team.where.text() == "On coding-101 (from /srv/team-presets)"
    built_in = card(gallery, "mini-ctf")
    assert PR.from_hosts(built_in.preset) == ("local", "coding-101", "ct2")
    assert built_in.where.text() == "On this computer, coding-101, ct2"
    assert next(c for c in gallery.cards if c.preset.id == "locked").where.text() == "The app's own"
    headings = [h.text() for h, _, _, _ in gallery.sections]
    assert not any("coding-101" in heading for heading in headings), "one list: no section per host"
    # Opened again: what each host said last at once; an answer that is the same makes no card anew.
    from sandbox_ai_app import preset_gallery
    made = []
    original = preset_gallery.PresetGallery.populate
    try:
        preset_gallery.PresetGallery.populate = lambda self, *a, **k: made.append(a) or original(self, *a, **k)
        again = gallery_of(sai)
    finally:
        preset_gallery.PresetGallery.populate = original
    assert len(made) == 1 and card(again, "tor-researcher").where.text() == "On coding-101 (yours there)"


def test_the_host_filter_narrows_the_list_all_hosts_by_default(sai):
    gallery = gallery_of(sai)
    assert gallery.host_filter.currentData() == "" and gallery.host_filter.currentText() == "All hosts"
    every = {c.preset.key for c in gallery.visible_cards()}
    gallery.host_filter.setCurrentIndex(gallery.host_filter.findData("local"))
    here = {c.preset.key for c in gallery.visible_cards()}
    assert "sandbox-ai:sai-tor-researcher" not in here and "app:locked" in here and here < every
    gallery.host_filter.setCurrentIndex(gallery.host_filter.findData("coding-101"))
    there = {c.preset.key for c in gallery.visible_cards()}
    assert {"sandbox-ai:sai-tor-researcher", "sandbox-ai:sai-team-tool", "sandbox-ai:sai-mini-ctf"} <= there
    assert "app:locked" not in there, "the app's own and yours belong to this computer"
    assert gallery.selected in gallery.visible_cards()
    counts = [gallery.categories.item(row).text() for row in range(gallery.categories.count())
              if not gallery.categories.item(row).isHidden()]
    assert counts and all("(0)" not in text for text in counts)


def test_a_preset_of_one_host_starts_on_another_its_ruleset_read_from_its_host(sai):
    """The board of this computer from coding-101's own preset: its definition is taken along, its ruleset read from
    coding-101 (`presets show --json` there carries it); secrets are only named, never copied."""
    ruleset = {"version": 1, "description": "each fetch asked",
               "http": [{"id": "ask", "host": "*", "decision": "ask"}]}
    sai.answers[("coding-101", "presets", "show")] = (0, json.dumps({**PRIVATE, "ruleset": ruleset}), "")
    gallery = gallery_of(sai)
    gallery.choose(card(gallery, "tor-researcher"))
    assert "Not on this computer: the board takes its definition along" in gallery.where.text()
    assert "read from coding-101 when the board is made" in gallery.notes.text()
    assert gallery.create() is True
    preset, values = gallery.result_
    assert preset.rules == ruleset and not preset.rules_file and values == {}
    assert ("coding-101", "presets", "show", "--json", "tor-researcher") in sai.asked
    assert not any(entry[1:] == ("options",) for entry in sai.asked), "this computer's options are checked anyway"
    board = PR.build(sai, preset, None, agent="claude", values=values)
    assert board.first("approvals").props["rules"] == ruleset and "--offline" in PR.B.to_argv(sai, board)


def test_a_host_too_old_to_hand_over_the_ruleset_is_said_not_left_out(sai):
    sai.answers[("coding-101", "presets", "show")] = (0, json.dumps(PRIVATE), "")  # (no "ruleset": before 0.1.243)
    gallery = gallery_of(sai)
    gallery.choose(card(gallery, "tor-researcher"))
    assert gallery.create() is False and gallery.result_ is None
    said = " ".join(text for _, text, *_ in widgets.SHOWN)
    assert "The sandbox-ai on coding-101 is too old to hand over the ruleset of tor-researcher: update it" in said
    assert gallery.create_button.isEnabled() and not gallery.status.isVisible()


def test_what_the_boards_host_cannot_do_is_refused_with_why(sai):
    """A board on ct2, whose launcher does not know --offline (an older sandbox-ai): coding-101's preset with it is
    refused, saying to update ct2; one ct2 has itself is made as always; one that ct2 can start is made."""
    sai.answers[("coding-101", "presets", "show")] = (0, json.dumps({**PRIVATE, "ruleset": PR.RESEARCH_RULES}), "")
    gallery = gallery_of(sai, host="ct2")
    gallery.choose(card(gallery, "tor-researcher"))
    assert gallery.create() is False
    said = " ".join(text for _, text, *_ in widgets.SHOWN)
    assert "ct2 cannot start Tor researcher: its sandbox-ai does not know --offline. Update it" in said
    widgets.SHOWN.clear()
    gallery.choose(card(gallery, "team-tool"))  # coding-101's, with an option ct2 knows
    assert gallery.create() is True and gallery.result_[0].name == "team-tool"
    assert ("ct2", "options") in sai.asked


def test_the_window_is_almost_as_large_as_the_main_window_and_centred(sai, monkeypatch):
    for cls in (QtWidgets.QDialog, QtWidgets.QMessageBox):
        monkeypatch.setattr(cls, "showEvent", cls.showEvent)
    monkeypatch.setattr(QtWidgets.QDialog, "hideEvent", QtWidgets.QDialog.hideEvent)
    monkeypatch.setattr(widgets, "PLACES", {})
    widgets.fitting_dialogs()
    screen = QRect(0, 0, 1920, 1080)
    monkeypatch.setattr(widgets, "screen_area", lambda _widget: QRect(screen))
    window = QtWidgets.QMainWindow()
    window.setGeometry(QRect(100, 60, 1500, 940))
    window.show()
    QtWidgets.QApplication.processEvents()
    gallery = gallery_of(sai, parent=window)
    gallery.show()
    QtWidgets.QApplication.processEvents()
    size, frame = QtWidgets.QWidget.size(gallery), window.frameGeometry()
    assert abs(size.width() - int(1500 * widgets.WINDOW_SHARE)) <= 2, size
    assert abs(size.height() - int(940 * widgets.WINDOW_SHARE)) <= 2, size
    placed = QRect(QtWidgets.QWidget.pos(gallery), size)
    assert abs(placed.center().x() - frame.center().x()) <= 2 and abs(placed.center().y() - frame.center().y()) <= 2
    gallery.close()
    window.close()


def test_the_section_headings_on_the_right_are_underlined(sai):
    gallery = gallery_of(sai)
    for heading, _box, _cards, _key in gallery.sections:
        assert heading.font().underline() and "underline" in heading.styleSheet(), heading.text()


def test_clicking_along_a_row_moves_nothing_on_the_right(sai, monkeypatch):
    """Regression (todos 2026-11-24): a click selects a card where it is - the cards are never scrolled by it and a
    card's size does not change with its selection (it is drawn over the card); another preset's details start at
    their top, the same one clicked again changes nothing."""
    monkeypatch.setattr(widgets, "screen_area", lambda _widget: QRect(0, 0, 1600, 1000))
    gallery = gallery_of(sai)
    gallery.resize(1400, 820)
    gallery.show()
    gallery.finish_cards()
    for _ in range(3):
        QtWidgets.QApplication.processEvents()
    holder, bar = gallery.scroll.widget(), gallery.scroll.verticalScrollBar()
    rows = {}
    for card_ in gallery.visible_cards():
        rows.setdefault((id(card_.parentWidget()), card_.y()), []).append(card_)
    tried = 0
    for row in [row for row in rows.values() if len(row) >= 3][1:8]:
        top = row[0].mapTo(holder, QPoint(0, 0)).y()
        bar.setValue(max(0, top - gallery.scroll.viewport().height() + 60))  # the row cut at the bottom
        QtWidgets.QApplication.processEvents()
        before = bar.value()
        places = [QRect(c.mapTo(holder, QPoint(0, 0)), c.size()) for c in gallery.cards]
        for card_ in row + row[-2::-1]:  # (never one card twice in a row: a double click creates)
            QTest.mouseClick(card_, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
            QtWidgets.QApplication.processEvents()
            assert gallery.selected is card_
        assert gallery.result_ is None and gallery.isVisible()
        assert bar.value() == before, "a click never scrolls the cards"
        assert [QRect(c.mapTo(holder, QPoint(0, 0)), c.size()) for c in gallery.cards] == places, "nothing moved"
        tried += 1
    assert tried >= 3
    # In every theme (a card's border differs: none, 1 px, 2 px, a bevel) its selection keeps its size.
    from sandbox_ai_app import theme
    card_ = gallery.visible_cards()[2]
    try:
        for mode in ("light", "win311", "unicorn", "hacker"):
            theme.apply(mode, QtWidgets.QApplication.instance())
            QtWidgets.QApplication.processEvents()
            card_.select(False)
            QtWidgets.QApplication.processEvents()
            size = (card_.size(), card_.sizeHint())
            card_.select(True)
            QtWidgets.QApplication.processEvents()
            assert (card_.size(), card_.sizeHint()) == size, mode
    finally:
        theme.apply("system", QtWidgets.QApplication.instance())
    # The keyboard shows the whole row of the card it moves to, and only as far as needed.
    bar.setValue(0)
    gallery.choose(gallery.visible_cards()[0])
    details = gallery.details.verticalScrollBar()
    details.setRange(0, 500)
    details.setValue(300)
    gallery.choose(gallery.selected)
    assert details.value() == 300, "the same preset again: nothing changes"
    gallery.choose(gallery.visible_cards()[1])
    assert details.value() == 0, "another preset: its details from the top"
    last = gallery.visible_cards()[-1]
    gallery.choose(last)
    QtWidgets.QApplication.processEvents()
    top = last.mapTo(holder, QPoint(0, 0)).y()
    assert bar.value() <= top <= bar.value() + gallery.scroll.viewport().height(), "revealed"
    gallery.close()


def test_the_start_page_starts_a_preset_of_another_host_from_its_definition(sai, monkeypatch):
    """The Start page (without the Board): sandbox-ai's preset of its own host is --preset NAME there; one that only
    another host has is taken over as its options (`--preset` there would not find it)."""
    from sandbox_ai_app import ui
    monkeypatch.setattr(ui, "background", lambda *a, **k: None)
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    page.link = None
    catalogue = PR.combined(sai, "local", {"coding-101": PR.host_documents(sai, "coding-101")})
    foreign = catalogue.find("team-tool")
    assert page.apply_preset(foreign, {})
    assert page.form.values().get("--preset") in ("", None)
    assert page.form.values().get("--allow-pypi")
    here = catalogue.find("mini-ctf")
    assert page.apply_preset(here, {})
    assert page.form.values().get("--preset") == "mini-ctf"
