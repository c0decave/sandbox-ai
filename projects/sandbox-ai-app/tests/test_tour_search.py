"""Tour about…: a topic in English or German finds the tours and Help sections about it, best first (tour_search.py),
and the dialog from the Help menu (Ctrl+F1) and the Help page starts the tour or opens the section."""
import pytest

from sandbox_ai_app import help_content, i18n, tour_search as T, tours


def idents(query, count=5):
    return [hit.ident for hit in T.search(query)[:count]]


@pytest.mark.parametrize("query, wanted", [
    ("network", "approvals-network"),
    ("Netzwerk", "approvals-network"),          # German
    ("give me a tour on approvals", "approvals-network"),  # filler words ignored
    ("zeig mir eine Tour über Freigaben", "approvals-network"),
    ("netwrok", "approvals-network"),           # a near miss
    ("preset gallery", "presets-gallery"),
    ("presets", {"presets-gallery", "own-construct"}),  # plural and singular; both tours are about presets
    ("Vorlagen", {"presets-gallery", "own-construct"}),
    ("kits", "area:kits"),
    ("mcp server", "area:mcp"),
    ("first start", "first-start"),
    ("Dateien", "area:files"),
])
def test_a_topic_finds_its_tour_first_among_tours(query, wanted):
    tours_found = [hit.ident for hit in T.search(query) if hit.is_tour]
    wanted = wanted if isinstance(wanted, set) else {wanted}
    assert tours_found and tours_found[0] in wanted, (query, idents(query, 8))


def test_sections_are_found_too_and_lend_their_score_to_their_tours():
    hits = T.search("canary")
    assert hits[0].ident == "canaries" and hits[0].kind == "section"
    named = set(help_content.BY_ID["canaries"].tours)
    assert not named or named & {hit.ident for hit in hits}
    assert "canaries" in idents("Köderdateien") or "canaries" in idents("Köder"), "umlauts and German words"
    assert T.fold("Köder ÄRGER Straße café") == "koeder aerger strasse cafe"


def test_more_matching_words_rank_higher_and_nothing_matches_nothing():
    both = T.search("preset gallery")
    one = T.search("gallery")
    assert both[0].score > one[0].score
    assert T.search("") == [] and T.search("   ") == [] and T.search("zzzz-qqqq") == []
    T.search("the a of")  # only filler words: searched as they are, never an error


def test_every_result_has_both_languages_and_a_known_target():
    for query in ("network", "kits", "hooks", "terminal", "vpn", "Freigaben"):
        for hit in T.search(query):
            assert hit.title.en and hit.title.de and hit.summary.en and hit.summary.de, hit
            if hit.kind == "task":
                assert hit.ident in tours.TASK_TOURS
            elif hit.kind == "area":
                assert hit.ident[len("area:"):] in tours.AREA_TOURS
            else:
                assert hit.ident in help_content.BY_ID
    assert len(T.search("sandbox", limit=3)) == 3


QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import help_view  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the window of the UI tests


def test_the_dialog_lists_tours_and_sections_and_starts_them(window, qapp):
    started, opened = [], []
    dialog = help_view.TourSearchDialog(window, "netzwerk")
    dialog.tour_requested.connect(started.append)
    dialog.section_requested.connect(opened.append)
    first = dialog.results.item(0)
    assert dialog.results.count() > 1 and "approvals-network" == first.data(0x0100)[1]
    assert first.text().startswith("Tour through a task:") and dialog.start_button.isEnabled()
    dialog.query.setText("zzzz-qqqq")
    assert dialog.results.count() == 0 and "zzzz-qqqq" in dialog.status.text() and not dialog.start_button.isEnabled()
    dialog.query.setText("canary")
    assert list(dialog.results.currentItem().data(0x0100)) == ["section", "canaries"]
    assert dialog.start_button.text() == "Open the section"
    dialog.start_current()
    assert opened == ["canaries"] and started == []
    # In German the labels and titles follow.
    i18n.set_language("de")
    try:
        german = help_view.TourSearchDialog(window, "Freigaben")
        assert german.windowTitle() == "Tour zu…" and german.results.item(0).text().startswith("Tour durch eine Aufgabe:")
        german.close()
    finally:
        i18n.set_language("en")


def test_tour_about_from_the_menu_and_the_help_page(window, qapp):
    assert [k.toString() for k in window.actions["tour-about"].shortcuts()] == ["Ctrl+F1"]
    window.actions["tour-about"].trigger()
    dialog = window.tour_search
    dialog.query.setText("approvals network")
    dialog.start_current()
    assert window.task_tour.tour.id == "approvals-network"
    dialog = window.tour_about("canary")
    dialog.start_current()
    assert window.current_page() == "help" and window.help.shown == "canaries"
    page = window.show_help()
    page.search.setText("kits")
    dialog = page.tour_about()
    assert dialog.query.text() == "kits" and list(dialog.results.item(0).data(0x0100)) == ["area", "area:kits"]
    dialog.start_current()
    assert window.area_tour.page == "kits"
