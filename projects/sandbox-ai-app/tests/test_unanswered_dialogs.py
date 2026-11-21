"""A dialog no test answers is cancelled after conftest.UNANSWERED_LIMIT seconds, and its test fails naming it.

CI 0.1.187: the app job hung at 80 % (test_remote_session_start.py) until its timeout killed it, without a word of
where. A dialog run with exec() that nobody answers - Qt's QMessageBox statics and a dialog of the app's own are not
covered by widgets.AUTO_ANSWER - waits in its own event loop for ever; offscreen, nobody clicks."""
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

import conftest  # noqa: E402


def test_an_unanswered_message_box_is_cancelled_and_named(qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox
    assert getattr(QDialog.exec, "watched", False) and QDialog.exec.nonblocking, "the watch is on every exec()"
    monkeypatch.setattr(conftest, "UNANSWERED_LIMIT", 0.3)
    began = time.monotonic()
    # Qt's own static: no AUTO_ANSWER answers it (it used to wait here for ever)
    answer = QMessageBox.question(None, "Stop q1?", "Its agent ends.")
    assert answer != QMessageBox.StandardButton.Yes  # (cancelled: never the yes)
    assert time.monotonic() - began < 30
    assert conftest.UNANSWERED == ["QMessageBox 'Stop q1?': Its agent ends."]
    del conftest.UNANSWERED[:]  # (this test means it: the fixture would fail it)


def test_a_dialog_answered_in_time_is_left_alone(qapp, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(conftest, "UNANSWERED_LIMIT", 5)
    dialog = QDialog()
    QTimer.singleShot(50, dialog.accept)
    assert dialog.exec() == QDialog.DialogCode.Accepted
    assert conftest.UNANSWERED == []
    dialog.deleteLater()
