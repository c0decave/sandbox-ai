"""Every dialog of the app's own can be used from the keyboard (round 2, 2026-11-22, the user's rule for every page
and dialog): it has a default button - Enter presses it - and Esc closes it (Cancel, Close). The dialogs as
test_dialog_sizes makes them (dialogs, more_dialogs: every dialog class with realistic content), shown as the app
shows them (widgets.fitting_dialogs), in English and German."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from shiboken6 import isValid  # noqa: E402

from sandbox_ai_app import backend, i18n, widgets  # noqa: E402

from test_dialog_sizes import dialogs, more_dialogs, screens  # noqa: E402,F401
from test_ui import env, window  # noqa: E402,F401 - the isolated home and the window of the UI tests

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


def default_buttons(dialog):
    return [button for button in dialog.findChildren(QtWidgets.QPushButton)
            if button.isDefault() and button.isVisibleTo(dialog)]


def key_problems(what, dialog):
    """What keeps DIALOG (shown) from the keyboard: no default button, Esc that does not close it."""
    app = QtWidgets.QApplication.instance()
    dialog.show()
    for _ in range(3):
        app.processEvents()
    found = []
    name = f"{what} ({type(dialog).__name__} {dialog.windowTitle()!r})"
    # (a reader or an editor whose buttons take no Enter - Help, a chat, the editor's toggles - has none to press)
    takes = [button for button in dialog.findChildren(QtWidgets.QPushButton)
             if button.isVisibleTo(dialog) and button.autoDefault()]
    if not isinstance(dialog, QtWidgets.QWizard) and takes and not default_buttons(dialog):
        found.append(f"{name}: no default button (Enter does nothing)")
    dialog.activateWindow()
    QTest.keyClick(dialog.focusWidget() or dialog, Qt.Key.Key_Escape)
    for _ in range(3):
        app.processEvents()
    if isValid(dialog) and dialog.isVisible():  # (one that deletes itself on close: gone)
        found.append(f"{name}: Esc does not close it")
        dialog.reject()
        dialog.hide()
    return found


@pytest.mark.parametrize("language", ["en", "de"])
def test_every_dialog_has_a_default_button_and_closes_on_esc(window, screens, monkeypatch, language):  # noqa: F811
    i18n.set_language(language)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    found = []
    for what, make in [*dialogs(window), *more_dialogs(window)]:
        dialog = make()
        if isinstance(dialog, QtWidgets.QDialog):
            found += key_problems(what, dialog)
        if isValid(dialog):
            dialog.deleteLater()
    assert found == [], "\n".join(found)
