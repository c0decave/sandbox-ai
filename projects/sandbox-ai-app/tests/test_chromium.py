"""Chromium's sandbox for the terminal tabs (chromium.py): the check before each web view, the terminal window instead
when it fails (Chromium's FATAL check ended the whole app: "credentials.cc ... Cannot allocate memory (12)"), the
setting that switches the sandbox off at the next start, and the Chromium flags. No real web view is made here."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import Signal  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from sandbox_ai_app import applog, backend, chromium, i18n, widgets  # noqa: E402
from conftest import open_tab  # noqa: E402

FAILED = chromium.Probe(False, True, "ENOMEM", "unshare: ENOMEM")
FINE = chromium.Probe(True, True, "", "namespaces created")


def done(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def runner(*results):
    """A stand-in for subprocess.run: the RESULTS in turn (an exception is raised), and the argvs it was given."""
    calls, queue = [], list(results)

    def run(argv, **kwargs):
        calls.append(argv)
        result = queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result
    return run, calls


# -- the probe --

def test_the_probe_in_a_child_process(monkeypatch):
    monkeypatch.delenv("QTWEBENGINE_DISABLE_SANDBOX", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    run, calls = runner(done(0, "ok\n"))
    assert chromium.probe(run=run, platform="linux") == chromium.Probe(True, True, "", "namespaces created")
    assert calls[0][1:4] == ["-I", "-S", "-c"] and "0x10000000" in calls[0][4]  # CLONE_NEWUSER, in a child
    run, _ = runner(done(1, "unshare ENOMEM\n"))
    assert chromium.probe(run=run, platform="linux") == chromium.Probe(False, True, "ENOMEM", "unshare: ENOMEM")
    run, _ = runner(done(1, "fork-in-namespace ENOMEM\n"))
    assert chromium.probe(run=run, platform="linux").error == "ENOMEM"
    run, _ = runner(done(1, "unshare ENOSPC\n"))
    assert chromium.probe(run=run, platform="linux").error == "ENOSPC"
    run, _ = runner(subprocess.TimeoutExpired("python", 10))
    result = chromium.probe(run=run, platform="linux")
    assert not result.ok and result.error == "timeout"


def test_without_a_python_child_the_unshare_command_checks(monkeypatch):
    monkeypatch.delenv("QTWEBENGINE_DISABLE_SANDBOX", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    monkeypatch.setattr(chromium.shutil, "which", lambda name: "/usr/bin/unshare" if name == "unshare" else None)
    run, calls = runner(done(2, "", "ModuleNotFoundError: ctypes"),
                        done(1, "", "unshare: unshare failed: Cannot allocate memory\n"))
    result = chromium.probe(run=run, platform="linux")
    assert not result.ok and result.error == "ENOMEM" and calls[1][:2] == ["/usr/bin/unshare", "--user"]
    run, _ = runner(OSError("no interpreter"), done(0))
    assert chromium.probe(run=run, platform="linux").ok
    # Nothing that can check: as before (a web view), and said in the log.
    monkeypatch.setattr(chromium.shutil, "which", lambda name: None)
    run, _ = runner(done(2))
    result = chromium.probe(run=run, platform="linux")
    assert result.ok and not result.checked and "could not be checked" in result.detail


def test_nothing_to_check_without_the_sandbox_or_off_linux(monkeypatch):
    run, calls = runner()
    assert chromium.probe(run=run, platform="darwin").ok
    assert chromium.probe(run=run, platform="linux", environ={"QTWEBENGINE_DISABLE_SANDBOX": "1"}).ok
    assert chromium.probe(run=run, platform="linux", environ={"QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox"}).ok
    assert calls == []
    assert not chromium.sandbox_off({}) and not chromium.sandbox_off({"QTWEBENGINE_DISABLE_SANDBOX": "0"})
    assert not chromium.sandbox_off({"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu"})


def test_the_real_probe_runs_its_child(monkeypatch):
    """The child's script itself (here: a user namespace may or may not be allowed; either answer is a real one)."""
    monkeypatch.delenv("QTWEBENGINE_DISABLE_SANDBOX", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux only")
    result = chromium.probe(timeout=30)
    assert result.checked and (result.ok or result.error.startswith("E") or result.error == "timeout"), result


def test_the_result_is_logged_without_anything_else(monkeypatch):
    lines = []
    monkeypatch.setattr(applog, "write", lambda text, stream=None: lines.append(text))
    monkeypatch.setattr(chromium, "probe", lambda timeout=10, **_: FAILED)
    monkeypatch.setattr(chromium, "limits", lambda proc="/proc/sys": "max_user_namespaces=7")
    assert chromium.check() is FAILED
    assert len(lines) == 1 and "WARNING [webengine]" in lines[0] and "unshare: ENOMEM" in lines[0]
    assert "max_user_namespaces=7" in lines[0] and "terminal window" in lines[0]
    lines.clear()
    monkeypatch.setattr(chromium, "probe", lambda timeout=10, **_: FINE)
    assert chromium.check().ok and len(lines) == 1 and "INFO [webengine]" in lines[0]
    assert "namespaces created" in lines[0]


def test_limits_reads_the_kernel_values(tmp_path):
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "max_user_namespaces").write_text("0\n")
    assert chromium.limits(str(tmp_path)) == "max_user_namespaces=0"


def test_the_message_says_why_and_what_to_do():
    text = chromium.unavailable_text(FAILED)
    assert "ENOMEM" in text and "user.max_user_namespaces reached or out of memory" in text
    assert "sysctl user.max_user_namespaces" in text and "Use web views without Chromium's sandbox" in text
    assert "unprivileged" in chromium.unavailable_text(chromium.Probe(False, True, "EPERM", ""))
    assert "in time" in chromium.unavailable_text(chromium.Probe(False, True, "timeout", ""))
    try:
        i18n.set_language("de")
        german = chromium.unavailable_text(FAILED)
        assert "Was Sie tun können" in german and "ENOMEM" in german and "Terminalfenster" in german
    finally:
        i18n.set_language("en")


# -- the setting and the flags at the start --

def test_the_setting_switches_the_sandbox_off_at_the_start():
    from sandbox_ai_app import main
    assert backend.SETTINGS["web_without_sandbox"] is False  # off unless you choose it
    environ = {}
    assert main.prepare_chromium(environ, {}) is False
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in environ
    assert environ["QTWEBENGINE_CHROMIUM_FLAGS"] == " ".join(main.NO_GPU) + " --log-level=2"
    environ = {}
    assert main.prepare_chromium(environ, {"web_without_sandbox": True}) is True
    assert environ["QTWEBENGINE_DISABLE_SANDBOX"] == "1"
    environ = {"QTWEBENGINE_DISABLE_SANDBOX": "0"}  # yours stays as it is
    assert main.prepare_chromium(environ, {"web_without_sandbox": True}) is False
    assert environ["QTWEBENGINE_DISABLE_SANDBOX"] == "0"
    assert main.prepare_chromium({"QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox"}, {}) is True


def test_the_chromium_flags_switch_vulkan_off_with_the_gpu():
    """--disable-vulkan is no Chromium switch: Vulkan goes with its features (and ANGLE's Vulkan backend)."""
    from sandbox_ai_app.main import chromium_flags
    flags = chromium_flags({}).split()
    assert flags == ["--disable-gpu", "--disable-gpu-compositing",
                     "--disable-features=Vulkan,DefaultANGLEVulkan,VulkanFromANGLE", "--log-level=2"]
    assert "--disable-vulkan" not in flags
    assert chromium_flags({"SANDBOX_AI_APP_GPU": "1"}) == "--log-level=2"


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                    reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
def test_the_settings_dialog_offers_it_off_by_default(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    from sandbox_ai_app.settings import SettingsDialog
    sai = backend.SandboxAI()
    dialog = SettingsDialog(sai)
    box = dialog.fields["web_without_sandbox"]
    assert not box.isChecked() and "QTWEBENGINE_DISABLE_SANDBOX" in box.text()
    box.setChecked(True)
    assert sai.settings()["web_without_sandbox"] is True
    assert main_env_after_start({"web_without_sandbox": True})["QTWEBENGINE_DISABLE_SANDBOX"] == "1"
    dialog.deleteLater()


def main_env_after_start(settings):
    from sandbox_ai_app import main
    environ = {}
    main.prepare_chromium(environ, settings)
    return environ


# -- the fallback: no web view when the check fails --

class FakeView(QWidget):
    """Stands in for TerminalView (a real one makes a QWebEngineView)."""
    exited = Signal(int)
    made = []

    def __init__(self, argv, web, cwd, parent):
        super().__init__(parent)
        self.argv, self.running, self.view = list(argv), True, self
        FakeView.made.append(self)


@pytest.fixture
def terminal(qapp, monkeypatch):
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    monkeypatch.setattr(terminal.TerminalsPage, "told", False)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(applog, "write", lambda text, stream=None: None)
    FakeView.made = []
    return terminal


def test_a_failed_check_makes_no_web_view(terminal, monkeypatch):
    def no_view(*args, **kwargs):
        raise AssertionError("a web view was made although Chromium's sandbox cannot start")
    monkeypatch.setattr(terminal, "TerminalView", no_view)
    monkeypatch.setattr(chromium, "probe", lambda timeout=10, **_: FAILED)
    page = terminal.TerminalsPage()
    try:
        assert open_tab(page, ["true"], "build") is None and page.tabs.count() == 0
        assert "user.max_user_namespaces" in page.hint.text() and "terminal window" in page.hint.text()
        assert len(widgets.SHOWN) == 1 and widgets.SHOWN[0][0] == "A terminal window instead of a tab"
        assert open_tab(page, ["true"], "again") is None
        assert len(widgets.SHOWN) == 1, "the dialog once per run; the page keeps saying it"
    finally:
        page.deleteLater()


def test_the_check_runs_again_for_the_next_terminal(terminal, monkeypatch):
    answers = [FAILED, FINE]
    calls = []
    monkeypatch.setattr(chromium, "probe", lambda timeout=10, **_: calls.append(1) or answers.pop(0))
    monkeypatch.setattr(terminal, "TerminalView", FakeView)
    page = terminal.TerminalsPage()
    try:
        assert open_tab(page, ["true"], "first") is None
        view = open_tab(page, ["true"], "second")
        assert view is FakeView.made[0] and len(calls) == 2
        assert "user.max_user_namespaces" not in page.hint.text(), "the reason goes once a tab works again"
    finally:
        page.tabs.clear()
        page.deleteLater()


def test_the_window_opens_a_terminal_window_instead(qapp, monkeypatch):
    """MainWindow.in_terminal: no tab (the check failed) - the command runs in a terminal window."""
    from sandbox_ai_app import terminal, ui
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    started = []
    monkeypatch.setattr(ui.QProcess, "startDetached", lambda program, args: started.append([program, *args]) or True)
    shown = []
    window = SimpleNamespace(
        sai=SimpleNamespace(settings=lambda: {"terminal": "app"},
                            in_terminal=lambda argv, title: ["xterm", "-e", *argv]),
        terminals=SimpleNamespace(open=lambda argv, title, directory, done: done(None)),
        show_page=shown.append, own_starts=[], project=None)
    window.terminal_window = lambda argv, title: ui.MainWindow.terminal_window(window, argv, title)
    assert ui.MainWindow.in_terminal(window, ["sandbox-ai", "plugins", "claude"], "plugins") is True
    assert started == [["xterm", "-e", "sandbox-ai", "plugins", "claude"]] and shown == []
    # A tab when the check passes: no terminal window.
    window.terminals = SimpleNamespace(open=lambda argv, title, directory, done: done(object()))
    started.clear()
    assert ui.MainWindow.in_terminal(window, ["sandbox-ai", "plugins", "claude"], "plugins") is True
    assert started == [] and shown == ["terminals"]
