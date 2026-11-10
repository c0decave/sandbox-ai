"""sandbox-ai updated from the app: "Update now…" beside the update check's text in the status line (and Help > Update
sandbox-ai…) where this copy can update itself, asked first, run in a terminal - `sudo sandbox-ai update` for a system
installation - and "Restart the app" once it has worked."""
import functools
import os
from pathlib import Path
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
NEWER = "installed: 0.9.0 (/x)\navailable: 0.10.1 (https://example.org)\n"


def installation(root, version="0.9.0"):
    """An install.sh tree DATA/versions/VERSION with its sandbox/ folder and its sandbox-ai command."""
    tree = root / "versions" / version
    (tree / "sandbox").mkdir(parents=True)
    (tree / "install.sh").write_text("#!/bin/sh\n")
    command = tree / "sandbox-ai"
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    return tree / "sandbox"


# -- which copy updates how (sandbox/main.py's update and installation_mode) --

def test_the_route_of_each_kind_of_copy(tmp_path):
    user = installation(tmp_path / "home" / ".local" / "share" / "sandbox-ai")
    assert backend.update_route(user, euid=1000, writable=lambda p: True)[0] == "user"
    system = installation(tmp_path / "usr" / "lib" / "sandbox-ai")
    assert backend.update_route(system, euid=1000, writable=lambda p: False) == \
        ("root", str(tmp_path / "usr" / "lib" / "sandbox-ai"))
    assert backend.update_route(system, euid=1000, writable=lambda p: True)[0] == "user"  # this user may write it
    assert backend.update_route(system, euid=0, writable=lambda p: False)[0] == "user"  # the app runs as root
    package = tmp_path / "appimage" / "sandbox"
    package.mkdir(parents=True)
    (package.parent / "install.sh").write_text("")
    (package.parent / "PACKAGING").write_text("appimage\n")
    assert backend.update_route(package) == ("package", "appimage")
    checkout = tmp_path / "checkout" / "sandbox"
    checkout.mkdir(parents=True)
    (checkout.parent / "install.sh").write_text("")
    (checkout.parent / ".git").write_text("gitdir: elsewhere\n")  # a worktree's .git is a file
    assert backend.update_route(checkout)[0] == "checkout"
    pip = tmp_path / "site-packages" / "sandbox_ai"
    pip.mkdir(parents=True)
    assert backend.update_route(pip)[0] == "other"


def test_the_update_command_with_and_without_sudo(tmp_path):
    from types import SimpleNamespace
    directory = installation(tmp_path / "usr" / "lib" / "sandbox-ai")
    sai = SimpleNamespace(directory=directory, command=lambda *args: ["python3", "-I", "main.py", *args])
    assert backend.self_update_command(sai, "user") == ["python3", "-I", "main.py", "update"]
    command = str(directory.parent / "sandbox-ai")  # the installation's own command (root's files), not the app's Python
    assert backend.self_update_command(sai, "root", which=lambda name: "/usr/bin/" + name) == \
        ["/usr/bin/sudo", "--", command, "update"]
    with pytest.raises(backend.NoSudo) as refused:
        backend.self_update_command(sai, "root", which=lambda name: None)
    assert str(refused.value) == f"{command} update"
    for route in ("package", "checkout", "other"):
        with pytest.raises(ValueError):
            backend.self_update_command(sai, route)


def test_the_restart_command_waits_for_this_app(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "installed_launcher", lambda environ=None: None)
    data = tmp_path / "share" / "sandbox-ai"
    directory = installation(data)
    (data / "current").mkdir()
    (data / "current" / "sandbox-ai-app").write_text("#!/bin/sh\n")  # the stable launcher: the new version after it
    assert backend.restart_command(4321, directory) == [str(data / "current" / "sandbox-ai-app"), "--wait-for-pid=4321"]
    assert backend.restart_command(7, tmp_path / "x" / "sandbox", which=lambda name: "/bin/" + name) == \
        ["/bin/sandbox-ai-app", "--wait-for-pid=7"]
    plain = backend.restart_command(7, tmp_path / "x" / "sandbox", which=lambda name: None)
    assert plain[-1] == "--wait-for-pid=7" and "--hidden" not in plain and plain[1] == "-I"
    monkeypatch.setattr(backend, "installed_launcher", lambda environ=None: "/data/current/sandbox-ai-app")
    assert backend.restart_command(7, directory) == ["/data/current/sandbox-ai-app", "--wait-for-pid=7"]


def test_waiting_for_the_app_before():
    gone = subprocess.Popen(["true"])
    gone.wait()
    assert backend.wait_for_exit(gone.pid, limit=5)
    assert not backend.wait_for_exit(os.getpid(), limit=0.2)  # still there: given up after the limit


def test_main_waits_for_the_pid_first(monkeypatch):
    from sandbox_ai_app import main as app_main
    waited = []
    monkeypatch.setattr(backend, "wait_for_exit", waited.append)
    monkeypatch.setattr(app_main, "broken_pyside", lambda: "stop here")  # main ends right after the wait
    assert app_main.main(["sandbox-ai-app", "--wait-for-pid=4321"]) == 1
    assert waited == [4321]


# -- the window --

@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    from sandbox_ai_app import terminal
    from sandbox_ai_app.ui import MainWindow
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(terminal, "available", lambda web=None: (False, "not in this test"))
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    w = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if w.tray is not None:
        w.tray.hide()
        w.tray = None
    started = []
    w.sai.in_terminal = lambda argv, title="": ["term", *argv]
    monkeypatch.setattr("sandbox_ai_app.ui.QProcess.startDetached",
                        lambda program, args: started.append([program, *args]) or True)
    w.started = started
    w.notified = []
    w.notify = lambda title, text, level="warning", action=None: w.notified.append((title, text, level, action))
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def route(monkeypatch, kind, where="/x"):
    monkeypatch.setattr(backend, "update_route", lambda directory: (kind, where))


def test_update_now_only_with_an_update_and_a_copy_that_updates_itself(window, monkeypatch):
    route(monkeypatch, "user", "/home/u/.local/share/sandbox-ai")
    window.update_checked(0, NEWER, "")
    assert not window.update_notice.isHidden() and not window.update_now_button.isHidden()
    assert window.update_label.text() == "sandbox-ai 0.10.1 is available (installed: 0.9.0)"
    assert window.actions["self-update"].isEnabled() and window.update_now_button.accessibleName()
    window.update_checked(0, "installed: 0.10.1 (/x)\navailable: 0.10.1 (https://example.org)\n", "")
    assert window.update_notice.isHidden() and not window.actions["self-update"].isEnabled()
    for kind, said in (("package", "package"), ("checkout", "git pull"), ("other", "pip install -U")):
        route(monkeypatch, kind)
        window.update_checked(0, NEWER, "")
        assert not window.update_notice.isHidden() and window.update_now_button.isHidden()
        assert "update with: sandbox-ai update" in window.update_label.text()  # the hint stays
        assert said in window.update_label.toolTip() and not window.actions["self-update"].isEnabled()
    window.update_checked(1, "", "not installed by install.sh")  # no information: the last notice is left as it was
    route(monkeypatch, "root", "/usr/local/lib/sandbox-ai")
    window.update_checked(0, NEWER, "")
    assert not window.update_now_button.isHidden() and "sudo" in window.update_now_button.toolTip()


def test_update_now_asks_and_runs_in_a_terminal(window, monkeypatch):
    runs = []
    monkeypatch.setattr(window, "in_terminal", lambda argv, title, ended=None: runs.append((argv, title, ended)) or True)
    route(monkeypatch, "user")
    window.update_checked(0, NEWER, "")
    widgets.AUTO_ANSWER = False  # Cancel: nothing runs
    window.update_now()
    assert runs == [] and "from 0.9.0 to 0.10.1" in widgets.SHOWN[-1][1]
    widgets.AUTO_ANSWER = True
    window.actions["self-update"].trigger()  # the menu's entry does the same
    (argv, title, ended), = runs
    assert argv == window.sai.command("update") and "0.10.1" in title
    assert not window.update_now_button.isEnabled() and not window.actions["self-update"].isEnabled()
    window.update_now()  # once at a time
    assert len(runs) == 1
    ended(1)  # failed: said in the status line, and it may be tried again
    assert "failed (exit status 1)" in window.update_label.text() and window.update_now_button.isEnabled()
    assert window.notified == []
    window.update_now()
    runs[-1][2](0)
    assert "updated to 0.10.1: restart the app" in window.update_label.text()
    assert window.update_now_button.isHidden() and not window.restart_button.isHidden()
    (title, text, level, action), = window.notified
    assert level == "success" and "updated to 0.10.1" in text and action[0] == "Restart the app"
    assert action[1] == window.restart_app
    window.update_checked(0, "installed: 0.10.1 (/x)\navailable: 0.10.1 (https://example.org)\n", "")
    assert not window.restart_button.isHidden()  # the next check does not take the restart away


def test_a_system_installation_is_updated_with_sudo_or_not_at_all(window, monkeypatch):
    runs = []
    monkeypatch.setattr(window, "in_terminal", lambda argv, title, ended=None: runs.append(argv) or True)
    real = backend.self_update_command
    monkeypatch.setattr(backend, "self_update_command", functools.partial(real, which=lambda name: f"/usr/bin/{name}"))
    route(monkeypatch, "root", "/usr/local/lib/sandbox-ai")
    window.update_checked(0, NEWER, "")
    window.update_now()
    command = str(Path(window.sai.directory).parent / "sandbox-ai")
    assert runs == [["/usr/bin/sudo", "--", command, "update"]]
    assert "as root" in widgets.SHOWN[-1][1] and widgets.SHOWN[-1][2] == f"/usr/bin/sudo -- {command} update"
    window.update_running = False
    monkeypatch.setattr(backend, "self_update_command", functools.partial(real, which=lambda name: None))
    window.update_now()
    assert len(runs) == 1  # not started: how to run it as root is said instead
    title, text, _ = widgets.SHOWN[-1]
    assert "no sudo" in text and f"{command} update" in text and "/usr/local/lib/sandbox-ai" in text


def test_the_end_of_a_command_in_a_terminal_window_is_seen(window, wait):
    ended = []
    window.sai.save_setting("terminal", "window")
    assert window.in_terminal(["sh", "-c", "exit 3"], "x", ended=ended.append)
    term = window.started[-1]
    assert term[:3] == ["term", "sh", "-c"] and term[-3:] == ["sh", "-c", "exit 3"]
    subprocess.run(term[1:], check=False)  # what the terminal window would run
    assert wait(lambda: ended == [3], 10)
    assert not Path(term[5]).parent.exists()  # the status file's folder is gone


def test_the_end_of_a_command_in_a_terminal_tab_is_seen(monkeypatch):
    """In a tab of the app: its view's exited(status); a tab Chromium's sandbox refuses: a window, as always."""
    from types import SimpleNamespace
    from sandbox_ai_app import terminal, ui

    class View(QtCore.QObject):
        exited = QtCore.Signal(int)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))
    view, ended, windows = View(), [], []
    window = SimpleNamespace(sai=SimpleNamespace(settings=lambda: {"terminal": "app"}), show_page=lambda key: None,
                             own_starts=[], project=None,
                             terminals=SimpleNamespace(open=lambda argv, title, directory, done: done(view)),
                             terminal_window=lambda argv, title, ended=None: windows.append(ended) or True)
    assert ui.MainWindow.in_terminal(window, ["sandbox-ai", "update"], "update", ended=ended.append)
    view.exited.emit(0)
    assert ended == [0] and windows == []
    window.terminals = SimpleNamespace(open=lambda argv, title, directory, done: done(None))
    ui.MainWindow.in_terminal(window, ["sandbox-ai", "update"], "update", ended=ended.append)
    assert windows == [ended.append]


def test_restart_quits_the_usual_way_then_starts_the_app_again(window, monkeypatch):
    monkeypatch.setattr(backend, "restart_command", lambda pid, directory: ["/data/current/sandbox-ai-app",
                                                                            f"--wait-for-pid={pid}"])
    monkeypatch.setattr(window, "quit_app", lambda: False)  # the human kept the window: no restart
    window.restart_app()
    assert window.restart_argv is None
    window.start_again()
    assert window.started == []
    quits = []
    monkeypatch.setattr(window, "quit_app", lambda: quits.append(1) or True)
    window.restart_app()
    assert quits == [1] and window.started == []  # started once the window has really closed: finish()
    window.finish(QtGui.QCloseEvent())
    assert window.started == [["/data/current/sandbox-ai-app", f"--wait-for-pid={os.getpid()}"]]
    window.start_again()
    assert len(window.started) == 1
