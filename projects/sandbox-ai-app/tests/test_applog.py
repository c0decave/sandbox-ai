"""The app's log: each entry says when, where (component, file and line) and in which versions."""
import os
import re
import sys
import threading

import pytest

from sandbox_ai_app import applog


class ImagesPage:
    def edited(self):
        self.check_timer.start()


def raised():
    try:
        ImagesPage().edited()
    except AttributeError:
        return sys.exc_info()
    raise AssertionError("no exception")


def test_an_entry_names_time_component_place_and_versions(monkeypatch):
    applog.set_sandbox_ai("0.1.50", "/opt/sandbox-ai")
    text = applog.entry(*raised(), delivered="MainWindow / ImagesPage / CodeEditor")
    first = text.splitlines()[0]
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d ERROR \[test_applog\.ImagesPage\.edited\]", first)
    assert "at test_applog.py:" in first and "delivered by MainWindow / ImagesPage / CodeEditor" in first
    assert f"pid {__import__('os').getpid()}" in first and "thread MainThread" in first
    assert "app " in first and "sandbox-ai 0.1.50" in first and "python " in first
    assert "  AttributeError: 'ImagesPage' object has no attribute 'check_timer'" in text
    assert "  | Traceback (most recent call last):" in text  # the whole traceback, marked as part of this entry


def test_the_component_is_the_apps_innermost_frame_not_a_librarys(monkeypatch):
    monkeypatch.setattr(applog, "APP_PACKAGE", "tests")  # this file stands for the app's code
    def library_call(callback):
        callback()
    try:
        library_call(lambda: ImagesPage().edited())
    except AttributeError:
        name, place = applog.component(sys.exc_info()[2])
    assert name == "test_applog.ImagesPage.edited" and place.startswith("test_applog.py:")


def test_hooks_write_entries_and_the_app_goes_on(capsys):
    applog.excepthook(*raised())
    error = capsys.readouterr().err
    assert " ERROR [test_applog.ImagesPage.edited]" in error

    def fails():
        raise ValueError("in a worker")
    worker = threading.Thread(target=fails, name="images-worker")
    previous, threading.excepthook = threading.excepthook, applog.thread_excepthook
    try:
        worker.start()
        worker.join()
    finally:
        threading.excepthook = previous
    error = capsys.readouterr().err
    assert "thread images-worker" in error and "delivered by a thread" in error and "ValueError: in a worker" in error


def test_qt_messages_get_the_same_header(capsys):
    QtCore = pytest.importorskip("PySide6.QtCore")

    class Context:
        category, file, line = "qt.qpa.xcb", "qxcbwindow.cpp", 42
    applog.qt_message(QtCore.QtMsgType.QtWarningMsg, Context(), "could not set geometry")
    error = capsys.readouterr().err
    assert re.search(r"\d\d:\d\d\.\d{3}[+-]\d\d:\d\d WARNING \[qt\.qpa\.xcb qxcbwindow\.cpp:42\]", error)
    assert "  could not set geometry" in error
    applog.qt_message(QtCore.QtMsgType.QtDebugMsg, Context(), "noise")
    assert capsys.readouterr().err == ""  # debug only with --debug


def test_a_crash_of_the_process_leaves_the_python_stack_in_the_log(tmp_path):
    """A segfault (as on a theme change in 0.1.46) used to leave nothing: faulthandler writes every thread's
    Python stack into app.log."""
    import os
    import subprocess
    script = ("import sys; sys.path.insert(0, %r)\n"
              "from sandbox_ai_app import applog\n"
              "applog.install()\n"
              "import ctypes; ctypes.string_at(0)\n") % str(__import__("pathlib").Path(applog.__file__).parents[1])
    done = subprocess.run([sys.executable, "-c", script], env={**os.environ, "XDG_STATE_HOME": str(tmp_path)},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    log = (tmp_path / "sandbox-ai" / "app.log").read_text()
    assert " INFO [start]" in log and "Fatal Python error: Segmentation fault" in log and "Current thread" in log


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGTRAP"), reason="no SIGTRAP here")
def test_a_trap_of_chromium_leaves_the_python_stack_in_the_log_too(tmp_path):
    """Chromium (QtWebEngine runs inside the app's process) ends the process on a failed check with a trap
    instruction: SIGTRAP, which faulthandler.enable() does not watch - the app was gone without a line in app.log.
    It is watched as well; the process still ends by the trap."""
    import os
    import signal
    import subprocess
    script = ("import sys; sys.path.insert(0, %r)\n"
              "from sandbox_ai_app import applog\n"
              "applog.install()\n"
              "import os, signal; os.kill(os.getpid(), signal.SIGTRAP)\n"
              "print('went on')\n") % str(__import__("pathlib").Path(applog.__file__).parents[1])
    done = subprocess.run([sys.executable, "-c", script], env={**os.environ, "XDG_STATE_HOME": str(tmp_path)},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == -signal.SIGTRAP and "went on" not in done.stdout
    log = (tmp_path / "sandbox-ai" / "app.log").read_text()
    assert "Current thread" in log and "<module>" in log, log


def test_the_app_keeps_its_own_log_rotated(tmp_path, monkeypatch):
    path = tmp_path / "app.log"
    path.write_text("x" * 100)
    applog.rotate(path, limit=50, keep=2)
    assert not path.exists() and (tmp_path / "app.log.1").read_text() == "x" * 100
    stream = applog.open_log(path)
    stream.write("line\n")
    stream.close()
    assert path.read_text() == "line\n" and oct(path.stat().st_mode & 0o777) == "0o600"
    link = tmp_path / "linked.log"
    link.symlink_to(tmp_path / "elsewhere")
    assert applog.open_log(link) is None  # never through a link


def test_the_log_is_rotated_while_the_app_runs(tmp_path, monkeypatch):
    """Stability review 2: app.log was rotated only at a start. An app that runs for weeks in the tray - with debug
    lines twice a second, or an error repeated every second - filled the disk with it. Now it is rotated again once
    it is larger than LOG_LIMIT; a second app writing the same log never makes it grow without bound either."""
    path = tmp_path / "app.log"
    monkeypatch.setattr(applog, "LOG_LIMIT", 1000)
    monkeypatch.setattr(applog, "_path", path)
    monkeypatch.setattr(applog, "_file", applog.open_log(path))
    other = applog.open_log(path)  # a second app's
    monkeypatch.setattr(sys, "stderr", open(tmp_path / "stderr", "w"))  # (not the test's output)
    line = "x" * 99 + "\n"
    for _ in range(100):
        applog.write(line)
        other.write(line)
        other.flush()
        if os.fstat(other.fileno()).st_size > applog.LOG_LIMIT:  # what keep_rotated does in the other app
            other.close()
            applog.rotate(path, applog.LOG_LIMIT, applog.LOG_KEEP)
            other = applog.open_log(path)
    sizes = [p.stat().st_size for p in tmp_path.glob("app.log*")]
    assert len(sizes) <= applog.LOG_KEEP + 1 and max(sizes) <= 2 * applog.LOG_LIMIT + 2 * len(line), sizes
    assert path.stat().st_size <= 2 * applog.LOG_LIMIT
    other.close()
    applog._file.close()


def test_debug_mode_logs_each_step_with_its_component(capsys, monkeypatch):
    monkeypatch.delenv("SANDBOX_AI_APP_DEBUG", raising=False)
    applog.set_debug(False)
    monkeypatch.setattr(applog, "_debug", False)
    applog.debug("page", key="images")
    assert capsys.readouterr().err == ""  # off: nothing
    applog.set_debug(True)
    try:
        applog.debug("page", key="images")
        err = capsys.readouterr().err
        assert " DEBUG [test_applog.test_debug_mode_logs_each_step_with_its_component]" in err
        assert "  page key='images'" in err
    finally:
        applog.set_debug(False)
        monkeypatch.delenv("SANDBOX_AI_APP_DEBUG", raising=False)
