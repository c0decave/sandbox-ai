"""The Board's toolbar (user wish 2026-11-18): when the row wraps, buttons that belong together stay together -
Undo and Redo, then zoom out and in, then Arrange beside Fit ("Zentrieren") - so Redo never starts a row of its own
apart from Undo, the zoom buttons come right before Arrange, and Arrange and Fit are always side by side."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from test_board_view import page  # noqa: E402,F401 - the board page fixture

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def toolbar_items(page):
    """The toolbar's flow layout and its items in order, each a list of the button texts it holds."""
    flow = page.arrange_button.parentWidget()
    while flow is not None and not isinstance(flow.layout(), widgets.FlowLayout):
        flow = flow.parentWidget()
    assert flow is not None, "the toolbar is a flow row"
    layout = flow.layout()
    items = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        buttons = [widget] if isinstance(widget, QtWidgets.QAbstractButton) else \
            widget.findChildren(QtWidgets.QAbstractButton)
        items.append([b.text() for b in buttons])
    return flow, items


def test_the_buttons_that_belong_together_are_one_item_of_the_row(page):
    _flow, items = toolbar_items(page)
    assert ["Undo", "Redo"] in items, "Undo and Redo wrap together"
    assert ["−", "+"] in items, "zoom out and in wrap together"
    assert ["Arrange", "Fit"] in items, "Arrange and Fit side by side"
    order = [texts for texts in items if texts in (["Undo", "Redo"], ["−", "+"], ["Arrange", "Fit"])]
    assert order == [["Undo", "Redo"], ["−", "+"], ["Arrange", "Fit"]], "zoom before Arrange and Fit"


def test_at_any_width_arrange_and_fit_share_a_row_and_so_do_undo_and_redo(page):
    flow, _items = toolbar_items(page)
    page.resize(1400, 700)
    page.show()
    for width in (1400, 1000, 800, 640, 520, 420, 360):
        flow.setFixedWidth(width)
        flow.layout().setGeometry(flow.rect())
        for first, second in (("Arrange", "Fit"), ("Undo", "Redo"), ("−", "+")):
            a, b = (next(button for button in flow.findChildren(QtWidgets.QAbstractButton) if button.text() == text)
                    for text in (first, second))
            top_a, top_b = a.mapTo(flow, a.rect().topLeft()).y(), b.mapTo(flow, b.rect().topLeft()).y()
            assert top_a == top_b, f"{first} and {second} on one row at {width} px"
            assert a.mapTo(flow, a.rect().topLeft()).x() < b.mapTo(flow, b.rect().topLeft()).x()
