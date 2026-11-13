"""Buttons are actions, toggles look like what they are (user report: after a click on "Home" of the Files page the
button looked pressed). Every page: no button is checkable but check boxes, radio buttons and disclosures (an
arrow that folds something open, widgets.disclosure); a click on a navigation or action button leaves it neither
checked nor down; a focused button never looks pressed (the themes draw focus as a border); the "+" menu's projects
are actions (an open one says so), the project switcher a radio choice."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from sandbox_ai_app import theme, vpn_page, widgets  # noqa: E402

from test_projects import env, project, window  # noqa: E402,F401 - shared fixtures


def toggles(root):
    """The checkable buttons below ROOT that are neither a check box, a radio button nor a disclosure."""
    return [b for b in root.findChildren(QtWidgets.QAbstractButton)
            if b.isCheckable() and not isinstance(b, (QtWidgets.QCheckBox, QtWidgets.QRadioButton))
            and not b.property("disclosure")]


def name(widget):
    return f"{type(widget).__name__} {widget.text()!r} in {type(widget.parent()).__name__}"


def test_no_page_has_a_button_that_stays_pressed(window):
    found = [name(b) for b in toggles(window)]
    dialog = vpn_page.VpnDialog(window)
    notice = widgets.Notice()
    found += [name(b) for b in toggles(dialog) + toggles(notice)]
    assert not found, "checkable buttons that are not check boxes, radio buttons or disclosures:\n" + "\n".join(found)
    for toggle in (dialog.more, notice.toggle):
        assert toggle.property("disclosure") and toggle.autoRaise(), "a disclosure is flat, with its arrow"
        assert toggle.arrowType() == Qt.ArrowType.RightArrow
        toggle.click()
        assert toggle.isChecked() and toggle.arrowType() == Qt.ArrowType.DownArrow
        toggle.click()
        assert toggle.arrowType() == Qt.ArrowType.RightArrow
    dialog.deleteLater()
    notice.deleteLater()


def test_a_clicked_navigation_button_is_neither_checked_nor_down(window, qapp):
    window.show_page("files")
    page = window.files
    buttons = [b for pane in (page.left, page.right) for b in pane.findChildren(QtWidgets.QPushButton)
               if b.accessibleName() in ("Home", "Up", "Refresh")]  # (icons: by their names)
    assert len(buttons) >= 4
    for widget in buttons:
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert not widget.isChecked() and not widget.isDown() and not widget.hasFocus(), name(widget)
        assert widget.focusPolicy() == Qt.FocusPolicy.TabFocus and not widget.autoDefault()
    assert all(b.focusPolicy() == Qt.FocusPolicy.TabFocus for b in page.buttons.values())


@pytest.mark.parametrize("look", list(theme.EXTRAS))
def test_focus_is_never_drawn_as_pressed(look):
    """A theme may mark the focused button (a border, an underline) but never paint it as pressed or hovered."""
    import re
    import collections
    colors = next(data["colors"] for data in theme.THEMES_DATA.values() if data.get("extra") == look) \
        if any(data.get("extra") == look for data in theme.THEMES_DATA.values()) else next(iter(theme.COLORS.values()))
    sheet = theme.EXTRAS[look](collections.defaultdict(lambda: "#000000", colors), collections.defaultdict(str))
    for selectors in re.findall(r"([^{}]*QPushButton:focus[^{}]*)\{", sheet):
        assert ":pressed" not in selectors and ":hover" not in selectors and "role" not in selectors, selectors


def test_the_project_menus_are_an_action_list_and_a_radio_choice(window, env, wait):
    one, two = project(env, "one"), project(env, "two")
    window.set_project(one)
    window.sai.remember_project(two)
    window.fill_add_tab_menu()
    assert wait(lambda: window.project_bar.known.reading is None)
    window.fill_add_tab_menu(read=False)
    projects = [a for a in window.add_tab_menu.actions() if a.text().startswith(("one", "two"))]
    assert len(projects) == 2 and not any(a.isCheckable() for a in projects), "actions, not toggles"
    assert next(a for a in projects if a.text().startswith("one")).text().endswith("(open)")
    assert not next(a for a in projects if a.text().startswith("two")).text().endswith("(open)")
    window.fill_switch_menu(read=False)
    choices = [a for a in window.switch_menu.actions() if a.isCheckable()]
    assert len(choices) == 2 and all(a.actionGroup() is not None and a.actionGroup().isExclusive() for a in choices)
    assert [a.text().startswith("one") for a in choices if a.isChecked()] == [True]
