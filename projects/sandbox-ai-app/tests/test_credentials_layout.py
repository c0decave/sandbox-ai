"""The Credentials page's tabs share one layout (credential_tab.py; user report 2026-11-12: "every tab there has
another size, the text slides back and forth"): switching tabs never changes the page's or the tab widget's geometry,
the list of every tab starts at the same place, the button row and the message area are at the same place - in
English and German, at the window's default and at its minimum size. A message (short or long) moves nothing."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import QAbstractItemView, QLabel, QPushButton  # noqa: E402

from sandbox_ai_app import i18n, widgets  # noqa: E402
from sandbox_ai_app.credential_tab import CredentialTab, MessageArea, even_out  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests


def rect(widget, top):
    point = widget.mapTo(top, widget.rect().topLeft())
    return (point.x(), point.y(), widget.width(), widget.height())


def settle(qapp, rounds=8):
    for _ in range(rounds):
        qapp.processEvents()


def geometry(window, page, index):
    """What must not move when tab INDEX is shown: the page, the tab widget, the tab, its list's top and width,
    its button row, its message area."""
    tab = page.tabs.widget(index)
    lists = [w for w in tab.findChildren(QAbstractItemView) if w.isVisibleTo(tab) and w.parent() is not None
             and isinstance(w.parentWidget(), QtWidgets.QWidget) and w.parentWidget() is tab]
    assert lists, f"tab {page.tabs.tabText(index)} has no list"
    x, y, width, _height = rect(lists[0], window)
    return {"page": rect(page, window), "tabs": rect(page.tabs, window), "tab": rect(tab, window),
            "list top": (x, y, width), "buttons": rect(tab.button_bar, window), "message": rect(tab.message, window),
            "window": window.size().toTuple()}


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("size", ["default", "minimum"])
def test_switching_tabs_moves_nothing(qapp, window, wait, language, size):  # noqa: F811
    i18n.set_language(language)
    try:
        window.show()
        window.show_page("credentials", raise_window=False)
        page = window.credentials
        settle(qapp)
        assert wait(lambda: not widgets.busy(page) and not widgets.busy(page.vpn), 20)
        if size == "minimum":
            window.resize(window.minimumSizeHint())
        settle(qapp)
        assert page.tabs.count() >= 6
        assert all(isinstance(page.tabs.widget(i), CredentialTab) for i in range(page.tabs.count()))
        seen = {}
        for _ in range(2):  # twice round: what a first visit changed shows on the second
            for index in range(page.tabs.count()):
                page.tabs.setCurrentIndex(index)
                settle(qapp)
                seen.setdefault(index, []).append(geometry(window, page, index))
        first = seen[0][0]
        for index, found in seen.items():
            for shown in found:
                for key in ("page", "tabs", "tab", "buttons", "message", "window"):
                    assert shown[key] == first[key], (language, size, page.tabs.tabText(index), key, shown[key],
                                                      first[key])
                assert shown["list top"] == first["list top"], (language, size, page.tabs.tabText(index))
    finally:
        i18n.set_language("en")


def test_a_message_or_a_late_state_line_moves_nothing(qapp, window, wait):  # noqa: F811
    """A long refusal in one tab scrolls in its message area; a state line that arrives late (the secrets backend)
    makes every header as high as the highest again - the tabs' lists stay level."""
    window.show()
    window.show_page("credentials", raise_window=False)
    page = window.credentials
    assert wait(lambda: not widgets.busy(page), 20)
    window.resize(window.minimumSizeHint())
    page.tabs.setCurrentIndex(page.tab_of("git"))
    settle(qapp)
    before = geometry(window, page, page.tab_of("git"))
    page.git.message.setText("refused: " + "a very long reason " * 80)
    settle(qapp)
    assert geometry(window, page, page.tab_of("git")) == before
    assert page.git.message.text().startswith("refused:")
    assert isinstance(page.git.message, MessageArea)
    page.store_label.setText("Secrets backend: " + "a long store name " * 30)  # on the Secrets tab, now hidden
    settle(qapp)
    page.tabs.setCurrentIndex(page.tab_of("secrets"))
    settle(qapp)
    secrets = geometry(window, page, page.tab_of("secrets"))
    page.tabs.setCurrentIndex(page.tab_of("git"))
    settle(qapp)
    git = geometry(window, page, page.tab_of("git"))
    assert secrets["list top"] == git["list top"] and secrets["buttons"] == git["buttons"]


def test_even_out_makes_every_header_as_high_as_the_highest(qapp):
    tabs = [CredentialTab("Short"), CredentialTab("A title")]
    tabs[0].add_about("One line.")
    tabs[1].add_about("A text that wraps: " + "many words " * 40)
    for tab in tabs:
        tab.resize(400, 500)
        tab.add_buttons(QPushButton("Add"))
    even_out(tabs)
    assert tabs[0].header.height() == tabs[1].header.height() or \
        tabs[0].header.minimumHeight() == tabs[1].header.minimumHeight() == tabs[1].header_height(380)
    assert tabs[0].header.minimumHeight() > QLabel("x").sizeHint().height() * 3  # the long one's wrapped lines
    for tab in tabs:
        tab.deleteLater()


def test_the_message_area_has_a_fixed_height_and_keeps_the_label_api(qapp):
    area = MessageArea()
    height = area.height()
    area.setText("x " * 2000)
    assert area.text().startswith("x x") and area.maximumHeight() == area.minimumHeight() == height
    area.clear()
    assert area.text() == ""
    area.deleteLater()
