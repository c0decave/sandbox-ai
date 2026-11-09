"""The dialog for long messages (widgets.MessageDialog): showing the details makes room for them, nothing lies over
anything else, and hiding them works and shrinks the dialog back."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sandbox_ai_app import widgets  # noqa: E402

LONG = ("sandbox-ai mcp: telegram: its catalog is not signed by a trusted key "
        "(https://raw.githubusercontent.com/c0decave/sandbox-ai-mcp/main/catalog.json.sig cannot be read), so nobody "
        "vouched that it is built from its pinned sources and works with sandbox-ai; --unsigned adds it anyway")


def rect_in(widget, dialog):
    return widget.geometry().translated(widget.parentWidget().mapTo(dialog, widget.parentWidget().rect().topLeft())) \
        if widget.parentWidget() is not dialog else widget.geometry()


def test_details_make_room_and_hide_again(qapp):
    dialog = widgets.MessageDialog(None, QMessageBox.Icon.Warning, "MCP", LONG, details="\n".join(
        f"line {n}: " + "x" * 120 for n in range(60)))
    dialog.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
    dialog.show()
    QApplication.processEvents()
    closed = dialog.size()
    assert dialog.details_button.isVisible() and not dialog.details.isVisible()
    dialog.details_button.click()
    QApplication.processEvents()
    assert dialog.details.isVisible() and dialog.details_button.text() == widgets.tr("Hide details…")
    text, details, button = (rect_in(w, dialog) for w in (dialog.text, dialog.details, dialog.details_button))
    assert not text.intersects(details), (text, details)
    assert not details.intersects(button), (details, button)
    assert dialog.rect().contains(button), "the button stays inside the dialog, where it can be clicked"
    assert dialog.height() > closed.height()
    dialog.details_button.click()
    QApplication.processEvents()
    assert not dialog.details.isVisible() and dialog.details_button.text() == widgets.tr("Show details…")
    assert dialog.height() <= closed.height() + 2
    dialog.close()
