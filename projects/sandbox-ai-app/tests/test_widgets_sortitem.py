"""widgets.SortItem: the user's log 2026-11-22 (app 0.1.193, PySide6 6.12, Python 3.14) - the Approvals page raised
"RecursionError: Error calling Python override of QTableWidgetItem::__lt__(): maximum recursion depth exceeded"
every second: SortItem.__lt__ fell back to super().__lt__(), which on that PySide dispatched back into the Python
override. It never calls the base class now; without comparable sort values it compares the texts itself."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import widgets  # noqa: E402


def item(text, value=None):
    made = widgets.SortItem(text)
    if value is not None:
        made.setData(widgets.SORT_ROLE, value)
    return made


def test_the_base_class_is_never_called(qapp, monkeypatch):
    monkeypatch.setattr(QtWidgets.QTableWidgetItem, "__lt__", lambda self, other: pytest.fail("base __lt__ called"))
    assert item("b", 1) < item("a", 2), "numbers by their sort value"
    assert item("a") < item("b") and not item("b") < item("a"), "no sort value: by text"
    assert item("a", 5) < item("b"), "one without a sort value: by text"
    assert item("a", "x") < item("b", 3), "values that do not compare: by text"


def test_a_sorted_table_takes_new_texts_without_recursion(qapp):
    table = QtWidgets.QTableWidget(3, 1)
    for row, (text, value) in enumerate((("3 s", 3), ("?", None), ("1 s", 1))):
        table.setItem(row, 0, item(text, value))
    table.setSortingEnabled(True)
    table.sortItems(0)
    for row in range(table.rowCount()):  # as ApprovalsPage.show_open does every second
        cell = table.item(row, 0)
        cell.setText("2 s")
        cell.setData(widgets.SORT_ROLE, 2)
    assert sorted(table.item(row, 0).text() for row in range(3)) == ["2 s"] * 3
