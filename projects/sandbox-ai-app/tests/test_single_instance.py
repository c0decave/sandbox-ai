"""One app per user: a second start asks the first to show its window and ends; a start for the tray asks nothing;
a socket left by a killed app is taken over; nothing but the two words is heard."""
import os
import stat

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


def test_the_second_start_shows_the_first_and_ends(qapp, tmp_path):
    path = tmp_path / "run" / "app.sock"
    first = single_instance.Instance(path)
    assert first.claim() is True
    asked = []
    first.asked.connect(asked.append)
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    second = single_instance.Instance(path)
    assert second.claim() is False
    assert spin(qapp, lambda: asked == ["show"])
    assert single_instance.Instance(path).claim(hidden=True) is False
    assert spin(qapp, lambda: asked == ["show", "hidden"])
    noise = QLocalSocket()
    noise.connectToServer(str(path))
    assert noise.waitForConnected(500)
    noise.write(b"rm -rf /")
    noise.flush()
    noise.waitForBytesWritten(500)
    spin(qapp, lambda: False, 0.3)
    assert asked == ["show", "hidden"], "anything else is ignored"
    first.release()
    third = single_instance.Instance(path)
    assert third.claim() is True, "once the first is gone, the next start is the app"
    third.release()


def test_a_socket_left_by_a_killed_app_is_taken_over(qapp, tmp_path):
    path = tmp_path / "app.sock"
    path.write_text("")  # what a killed app leaves: nobody answers on it
    guard = single_instance.Instance(path)
    assert guard.claim() is True and guard.server is not None
    guard.release()


def test_the_socket_lies_in_the_users_own_runtime_directory(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    assert single_instance.socket_path({"XDG_RUNTIME_DIR": str(runtime)}) == runtime / single_instance.NAME
    state = single_instance.socket_path({"XDG_STATE_HOME": str(tmp_path / "state")})
    assert state == tmp_path / "state" / "sandbox-ai" / single_instance.NAME
