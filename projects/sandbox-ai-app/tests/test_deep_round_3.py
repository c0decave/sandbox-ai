"""Deep test round 3 (the app): chaos, concurrency and hostile input across the features of 0.1.93-0.1.95, cut by
failure mode (two at once, interrupted, exhausted, damaged or hostile input, time). One test per bug, named after it.
"""
import pytest

QtNetwork = pytest.importorskip("PySide6.QtNetwork", reason="PySide6 is not installed")
from PySide6.QtNetwork import QLocalSocket  # noqa: E402

from sandbox_ai_app import single_instance  # noqa: E402


def spin(qapp, condition, seconds=5):
    import time
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        qapp.processEvents()
    return condition()


class NobodyYet:
    """A connection made before the first app listened (two starts at the same moment): nobody answers it. (Not a
    QLocalSocket of Python's: collected later, one crashed the collector.)"""

    def connectToServer(self, name):
        pass

    def waitForConnected(self, msecs=30000):
        return False


# --- (a) two at once ----------------------------------------------------------------------------------------------

def test_two_app_starts_at_the_same_moment_make_one_app(qapp, tmp_path, monkeypatch):
    """Two starts of the app at the same moment (the login's autostart and a click, a double click on the menu entry):
    both found nobody listening, the later removed the earlier's socket as "left by a killed app" and listened
    itself - two apps writing the same state file, the one the single-instance guard is for. The first start holds
    a lock as long as it runs; a start that cannot take it is a second one."""
    path = tmp_path / "run" / "app.sock"
    first = single_instance.Instance(path)
    assert first.claim() is True
    asked = []
    first.asked.connect(asked.append)
    monkeypatch.setattr(single_instance, "QLocalSocket", NobodyYet)
    monkeypatch.setattr(single_instance, "ASK_FOR", 0.3, raising=False)
    second = single_instance.Instance(path)
    assert second.claim() is False, "a second app"
    monkeypatch.undo()
    third = single_instance.Instance(path)
    assert third.claim() is False  # the first one's socket is still there and it answers
    assert spin(qapp, lambda: asked == ["show"])
    spin(qapp, lambda: False, 0.3)  # (the first one's side of the connection ends before the next test)
    first.release()
    fourth = single_instance.Instance(path)
    assert fourth.claim() is True, "once the first is gone, the next start is the app"
    fourth.release()
