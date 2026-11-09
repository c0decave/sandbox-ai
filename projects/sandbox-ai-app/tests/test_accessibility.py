"""Review round 5 (M9): every interactive widget of every page has a name a screen reader says - an accessible name, a
buddy label or its label in a form (a button its text); every table and list its own name."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QAbstractItemView, QAbstractSpinBox, QComboBox, QFormLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QTextEdit,
)

from PySide6.QtCore import Qt  # noqa: E402

from sandbox_ai_app import backend, i18n, ui, widgets  # noqa: E402

from test_ui import env  # noqa: E402,F401 - the isolated home of the UI tests


def form_label(widget):
    """WIDGET's label in a QFormLayout of one of its ancestors (a field inside a row's widget counts too)."""
    child, parent = widget, widget.parentWidget()
    while parent is not None:
        layout = parent.layout()
        if isinstance(layout, QFormLayout) and layout.labelForField(child) is not None:
            return layout.labelForField(child)
        child, parent = parent, parent.parentWidget()
    return None


def unnamed(top):
    """What under TOP a screen reader could only call by its kind."""
    buddies = {id(label.buddy()) for label in QtWidgets.QWidget.window(top).findChildren(QLabel)
               if label.buddy() is not None}
    found = []
    for widget in top.findChildren(QAbstractButton):
        if widget.isVisibleTo(top) and not widget.text().strip("…+ ?") and not widget.accessibleName() \
                and not widget.toolTip():
            found.append(f"button {type(widget).__name__} in {type(widget.parentWidget()).__name__}")
    for kind in (QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QAbstractSpinBox):
        for widget in top.findChildren(kind):
            if not widget.isVisibleTo(top) or widget.accessibleName() or id(widget) in buddies or form_label(widget):
                continue
            parent = widget.parentWidget()
            if isinstance(widget, QLineEdit) and isinstance(parent, (QComboBox, QAbstractSpinBox)):
                continue  # the editor of a combo or spin box: named through it
            if isinstance(widget, QTextEdit) and isinstance(parent, QComboBox):
                continue
            hint = getattr(widget, "placeholderText", lambda: "")()
            found.append(f"field {type(widget).__name__} in {type(parent).__name__} placeholder={hint!r} "
                         f"tip={widget.toolTip()[:60]!r}")
    for widget in top.findChildren(QAbstractItemView):
        if isinstance(widget, QHeaderView):
            continue  # a table's header: the table's name says it
        parent = widget.parentWidget()
        if widget.isVisibleTo(top) and not widget.accessibleName() and not isinstance(parent, QComboBox) \
                and not isinstance(widget.parentWidget().parentWidget(), QComboBox):
            model = widget.model()
            heads = [model.headerData(c, Qt.Orientation.Horizontal) for c in range(3)] if model is not None else []
            found.append(f"table/list {type(widget).__name__} in {type(parent).__name__} {heads}")
    return found


@pytest.mark.parametrize("language", ["en", "de"])
def test_every_interactive_widget_of_every_page_has_a_name(qapp, env, monkeypatch, language):  # noqa: F811
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    i18n.set_language(language)
    try:
        window = ui.MainWindow(backend.SandboxAI())
        window.show()
        found = []
        for key, _ in ui.PAGES:
            window.show_page(key, raise_window=False)
            qapp.processEvents()
            found += [f"page {key}: {item}" for item in unnamed(getattr(window, key))]
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.processEvents()
        assert found == [], "\n".join(found)
    finally:
        i18n.set_language("en")
