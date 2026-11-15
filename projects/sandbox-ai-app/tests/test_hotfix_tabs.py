"""Hotfix 0.1.96: arrows at the right end of every tab row.

User: "Bei den Terminal-Tabs und auch anderen soll rechts am Ende (wenn ein Ende erreicht wurde) ← → Pfeile sein,
sodass man auch so durchklicken kann."

Every tab row of the app is a widgets.ArrowTabBar: when its tabs no longer fit, ← → (and ▾, the list of all tabs)
appear together at the row's right end, in every theme and at large fonts; a click selects the tab before or after
the current one (held: it repeats), and each is disabled at its end. Titles keep their width (only a very long one
is shortened, in the middle), so the row scrolls instead of shrinking every title to a few letters.

The real widgets offscreen: the shared tab bar in a narrow tab widget per theme, and the app's own window (its
project tabs, every page's and the Settings' tab rows)."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from sandbox_ai_app import backend, theme, widgets  # noqa: E402

from test_projects import close, env, make_window, project, window  # noqa: E402,F401 - the window's fixtures

NEEDS_HOME = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
TITLES = [f"Terminal {i}" for i in range(16)]


@pytest.fixture
def themed(qapp):
    """theme.apply for one test; the theme and the font of before come back afterwards."""
    before, font = theme.current(), QFont(qapp.font())
    yield lambda mode: theme.apply(mode, qapp)
    theme.apply(before, qapp)
    qapp.setFont(font)


def row(titles=TITLES, width=420, closable=False):
    """A shown tab widget WIDTH pixels wide with the app's shared tab bar and a tab per title."""
    tabs = QtWidgets.QTabWidget()
    bar = widgets.inset_close_tabs(tabs) if closable else widgets.arrow_tabs(tabs)
    tabs.setTabsClosable(closable)
    for title in titles:
        tabs.addTab(QtWidgets.QWidget(), title)
    tabs.resize(width, 240)
    tabs.show()
    QtWidgets.QApplication.processEvents()
    return tabs, bar


def done(tabs):
    tabs.hide()
    tabs.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def at_the_right_end(bar):
    """The arrows are shown, lie together inside the bar at its right end, ← left of →, ▾ last."""
    arrows = bar.arrows
    assert not arrows.isHidden() and arrows.isVisible(), "the tabs do not fit: the arrows are there"
    left, right, listed = arrows.left, arrows.right, arrows.listed
    box = arrows.geometry()
    assert box.right() == bar.rect().right() and box.left() > bar.width() // 2, (box, bar.rect())
    assert box.top() >= 0 and box.bottom() <= bar.rect().bottom()
    assert left.geometry().right() < right.geometry().left() < listed.geometry().left()
    for button in (left, right, listed):
        assert button.isVisible() and not button.icon().isNull()
        size = button.size()
        icon = button.iconSize()
        assert size.width() >= 18 and size.height() >= 18, (size, theme.current())
        assert icon.width() >= 12 and icon.width() <= size.width() and icon.height() <= size.height()
        drawn = max(s.width() for s in button.icon().availableSizes())
        assert drawn >= 2 * icon.width(), "drawn at twice its size: sharp on a HiDPI screen"
    return box


def visible_pixels(button):
    """How many pixels of BUTTON's icon area differ from its background: an arrow that is drawn."""
    window = button.window()
    image = window.grab(QRect(button.mapTo(window, QPoint(0, 0)), button.size())).toImage()
    ground = image.pixelColor(1, image.height() // 2)
    width, height = image.width(), image.height()
    differ = 0
    for x in range(width // 4, 3 * width // 4):
        for y in range(height // 4, 3 * height // 4):
            color = image.pixelColor(x, y)
            if abs(color.lightness() - ground.lightness()) > 40:
                differ += 1
    return differ


# -- the user's report: arrows at the right end, clicking goes through the tabs, in every theme --


@pytest.mark.parametrize("mode", theme.THEMES)
def test_arrows_at_the_right_end_click_through_the_tabs_in_every_theme(qapp, themed, mode):
    """User: "rechts am Ende (wenn ein Ende erreicht wurde) ← → Pfeile ..., sodass man auch so durchklicken kann"."""
    themed(mode)
    tabs, bar = row()
    try:
        box = at_the_right_end(bar)
        assert visible_pixels(bar.arrows.right) > 6, f"the → is drawn and readable in {mode}"
        assert tabs.currentIndex() == 0 and not bar.arrows.left.isEnabled(), "at the first tab ← is off"
        assert bar.arrows.right.isEnabled()
        for expected in range(1, len(TITLES)):
            QTest.mouseClick(bar.arrows.right, Qt.MouseButton.LeftButton)
            assert tabs.currentIndex() == expected, "each click on → selects the next tab"
            shown = bar.tabRect(expected)
            assert shown.left() >= 0 and shown.right() <= box.left(), "the chosen tab scrolled into view"
        assert not bar.arrows.right.isEnabled() and bar.arrows.left.isEnabled(), "at the last tab → is off"
        QTest.mouseClick(bar.arrows.left, Qt.MouseButton.LeftButton)
        assert tabs.currentIndex() == len(TITLES) - 2
        assert bar.arrows.left.autoRepeat() and bar.arrows.right.autoRepeat(), "held, they go on"
    finally:
        done(tabs)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_titles_keep_their_width_instead_of_shrinking_away(qapp, themed, mode):
    """Before, ElideMiddle shrank every title to a few letters first, so the arrows hardly ever came."""
    themed(mode)
    tabs, bar = row()
    try:
        for index in range(bar.count()):
            assert bar.tabRect(index).width() >= bar.tabSizeHint(index).width(), bar.tabText(index)
        metrics = bar.fontMetrics()
        assert bar.tabRect(0).width() > metrics.horizontalAdvance("Terminal 0"), "the whole title fits"
    finally:
        done(tabs)


def test_only_a_very_long_title_is_shortened_and_says_itself_on_hover(qapp, themed):
    themed("light")
    long_title = "a-session-with-a-very-long-name-from-the-project-folder (claude, podman, tor)"
    tabs, bar = row([long_title, "short"], width=900)
    try:
        metrics = bar.fontMetrics()
        assert bar.tabRect(0).width() < metrics.horizontalAdvance(long_title), "a very long title is shortened"
        assert bar.tabRect(0).width() >= metrics.averageCharWidth() * widgets.TAB_TITLE_CHARS
        assert bar.full_title_tip(0) == long_title, "its whole title on hover"
        assert bar.full_title_tip(1) is None, "a title shown whole needs none"
        assert bar.arrows.isHidden(), "two tabs fit: no arrows"
    finally:
        done(tabs)


def test_no_arrows_while_the_tabs_fit_and_they_come_when_the_row_gets_narrow(qapp, themed):
    themed("dark")
    tabs, bar = row(TITLES[:3], width=700)
    try:
        assert bar.arrows.isHidden()
        tabs.resize(160, 200)
        QtWidgets.QApplication.processEvents()
        at_the_right_end(bar)
        tabs.resize(700, 200)
        QtWidgets.QApplication.processEvents()
        assert bar.arrows.isHidden()
    finally:
        done(tabs)


@pytest.mark.parametrize("mode", ["light", "dark", "win311", "hacker"])
def test_large_fonts_get_large_arrows(qapp, themed, mode):
    themed(mode)
    big = QFont(qapp.font())
    big.setPointSize(22)
    qapp.setFont(big)
    tabs, bar = row(width=520)
    try:
        at_the_right_end(bar)
        height = bar.fontMetrics().height()
        assert bar.arrows.right.iconSize().height() >= height * 0.6, "the arrows grow with the font"
        assert bar.arrows.right.height() >= height
        QTest.mouseClick(bar.arrows.right, Qt.MouseButton.LeftButton)
        assert tabs.currentIndex() == 1
    finally:
        done(tabs)


def test_the_list_button_names_every_tab_and_goes_there(qapp, themed):
    themed("light")
    tabs, bar = row()
    try:
        menu = bar.arrows.menu()
        names = [action.text() for action in menu.actions()]
        assert names == TITLES
        assert [a.isChecked() for a in menu.actions()].index(True) == 0, "the current tab is marked"
        menu.actions()[11].trigger()
        assert tabs.currentIndex() == 11
        assert bar.tabRect(11).right() <= bar.arrows.geometry().left()
        menu.deleteLater()
    finally:
        done(tabs)


def test_disabled_tabs_are_stepped_over(qapp, themed):
    themed("light")
    tabs, bar = row()
    try:
        tabs.setTabEnabled(1, False)
        QTest.mouseClick(bar.arrows.right, Qt.MouseButton.LeftButton)
        assert tabs.currentIndex() == 2
        QTest.mouseClick(bar.arrows.left, Qt.MouseButton.LeftButton)
        assert tabs.currentIndex() == 0
    finally:
        done(tabs)


def test_closable_tabs_get_the_arrows_too(qapp, themed):
    """The Terminals page's and the browser's tab rows (inset_close_tabs)."""
    themed("dark")
    tabs, bar = row(closable=True)
    try:
        assert isinstance(bar, widgets.ArrowTabBar)
        at_the_right_end(bar)
        QTest.mouseClick(bar.arrows.right, Qt.MouseButton.LeftButton)
        assert tabs.currentIndex() == 1
    finally:
        done(tabs)


def test_the_keyboard_still_goes_through_the_tabs(qapp, themed):
    themed("light")
    tabs, bar = row()
    try:
        tabs.currentWidget().setFocus()
        for key, modifiers, expected in ((Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier, 1),
                                         (Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier, 2),
                                         (Qt.Key.Key_PageUp, Qt.KeyboardModifier.ControlModifier, 1),
                                         (Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier, 2)):
            QTest.keyClick(tabs.currentWidget(), key, modifiers)  # (the page leaves it to its tab widget)
            assert tabs.currentIndex() == expected, (key, modifiers)
        assert bar.tabRect(2).right() <= bar.arrows.geometry().left()
        tabs.setCurrentIndex(0)
        QTest.keyClick(tabs.currentWidget(), Qt.Key.Key_PageUp, Qt.KeyboardModifier.ControlModifier)
        assert tabs.currentIndex() == len(TITLES) - 1, "the keys go round, as Ctrl+Tab does"
        assert bar.tabRect(len(TITLES) - 1).right() <= bar.arrows.geometry().left()
        bar.setFocus()
        QTest.keyClick(bar, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier)
        assert tabs.currentIndex() == 0, "on the focused row itself too"
        assert not bar.arrows.left.hasFocus() and bar.arrows.left.focusPolicy() == Qt.FocusPolicy.NoFocus
    finally:
        done(tabs)


def test_a_new_theme_redraws_the_arrows(qapp, themed):
    themed("light")
    tabs, bar = row()
    try:
        before = bar.arrows.right.icon().cacheKey()
        themed("dracula")
        QtWidgets.QApplication.processEvents()
        assert bar.arrows.right.icon().cacheKey() != before
        at_the_right_end(bar)
    finally:
        done(tabs)


# -- the app itself --


def all_tab_bars(window):
    window.build_pages()
    QtWidgets.QApplication.processEvents()
    return window.findChildren(QtWidgets.QTabBar)


@NEEDS_HOME
def test_every_tab_row_of_the_app_is_the_shared_one(window, qapp):
    """"Bei den Terminal-Tabs und auch anderen": the window's tabs, every page's and the Settings' are one kind."""
    bars = all_tab_bars(window)
    assert len(bars) >= 10
    plain = [f"{type(bar).__name__} in {type(bar.parentWidget()).__name__}" for bar in bars
             if not isinstance(bar, widgets.ArrowTabBar)]
    assert not plain, plain
    from sandbox_ai_app.settings import SettingsDialog
    dialog = SettingsDialog(window.sai, window)
    try:
        assert all(isinstance(bar, widgets.ArrowTabBar) for bar in dialog.findChildren(QtWidgets.QTabBar))
    finally:
        dialog.deleteLater()


@NEEDS_HOME
def test_the_windows_project_tabs_click_through_with_the_arrows(window, env, qapp):
    names = [f"project-{i:02d}" for i in range(12)]
    for name in names:
        window.set_project(project(env, name))
    window.resize(820, 600)
    window.show()
    QtWidgets.QApplication.processEvents()
    bar = window.tabs
    window.tabs.setCurrentIndex(0)
    QtWidgets.QApplication.processEvents()
    at_the_right_end(bar)
    arrows_end = bar.arrows.mapTo(window, QPoint(bar.arrows.width(), 0)).x()
    assert arrows_end <= window.add_tab_button.mapTo(window, QPoint(0, 0)).x(), "the row's end, before its +"
    QTest.mouseClick(bar.arrows.right, Qt.MouseButton.LeftButton)
    assert window.project == str(env / names[1]), "the arrow chooses the next project, the pages follow"
    for _ in range(20):
        QTest.mouseClick(bar.arrows.right, Qt.MouseButton.LeftButton)
    assert window.project == str(env / names[-1]) and not bar.arrows.right.isEnabled()
    assert bar.tabRect(bar.currentIndex()).right() <= bar.arrows.geometry().left()
