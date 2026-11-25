"""sandbox-ai updated from the app: "Update now…" beside the update check's text in the status line (and Help > Update
sandbox-ai…) where this copy can update itself, asked first, run in a terminal - `sudo sandbox-ai update` for a system
installation - and "Restart the app" once it has worked."""
import functools
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6 import QtCore, QtGui  # noqa: E402
from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
# (`update --check` says the signature too: only a release signed by a trusted key is offered)
NEWER = "installed: 0.9.0 (/x)\navailable: 0.10.1 (https://example.org)\nsignature: good, by the trusted key SHA256:x\n"


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

@pytest.fixture
def outside_homes(tmp_path):
    """A folder for a system installation (PREFIX/lib/sandbox-ai) outside every home: tmp_path, unless TMPDIR put it
    below one (TMPDIR=/root/..., /home/...), where an installation is never a system one (SYSTEM_HOMES) - then a
    folder of the system's own temporary folder, removed afterwards."""
    if not str(tmp_path).startswith(backend.SYSTEM_HOMES):
        yield tmp_path
        return
    base = next((folder for folder in ("/tmp", "/var/tmp", "/dev/shm") if os.access(folder, os.W_OK)), None)
    if base is None:
        pytest.skip("no folder outside the homes to place a system installation in")
    folder = Path(tempfile.mkdtemp(prefix="sai-system-", dir=base))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def test_the_route_of_each_kind_of_copy(tmp_path, outside_homes, monkeypatch):
    user = installation(tmp_path / "home" / ".local" / "share" / "sandbox-ai")
    assert backend.update_route(user, euid=1000, writable=lambda p: True)[0] == "user"
    system = installation(outside_homes / "usr" / "lib" / "sandbox-ai")
    assert backend.update_route(system, euid=1000, writable=lambda p: False) == \
        ("root", str(outside_homes / "usr" / "lib" / "sandbox-ai"))
    assert backend.update_route(system, euid=1000, writable=lambda p: True)[0] == "user"  # this user may write it
    assert backend.update_route(system, euid=0, writable=lambda p: False)[0] == "user"  # the app runs as root
    # The same tree below a home (as sandbox/main.py's HOMES) is never a system installation: the user's own
    monkeypatch.setattr(backend, "SYSTEM_HOMES", (f"{outside_homes}/",))
    assert backend.update_route(system, euid=1000, writable=lambda p: False)[0] == "user"
    monkeypatch.undo()
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


def test_the_route_of_an_installation_the_app_finds_through_its_current_link(tmp_path, outside_homes, monkeypatch):
    """The app finds install.sh's installation through DATA/current (the link is kept, so that an update is picked up):
    its sandbox/ folder is DATA/current/sandbox, not DATA/versions/X/sandbox - a system installation this user cannot
    write is still root's to update (sudo), and "Restart the app" still starts the installation's stable launcher."""
    data = outside_homes / "usr" / "lib" / "sandbox-ai"
    installation(data)
    os.symlink("versions/0.9.0", data / "current")
    (data / "versions" / "0.9.0" / "sandbox-ai-app").write_text("#!/bin/sh\n")
    through_link = data / "current" / "sandbox"
    assert backend.update_route(through_link, euid=1000, writable=lambda p: False) == ("root", str(data))
    assert backend.update_route(through_link, euid=1000, writable=lambda p: True) == ("user", str(data))
    assert backend.update_route(through_link, euid=0, writable=lambda p: False)[0] == "user"
    monkeypatch.setattr(backend, "installed_launcher", lambda environ=None: None)
    assert backend.restart_command(7, through_link, which=lambda name: None) == \
        [str(data / "current" / "sandbox-ai-app"), "--wait-for-pid=7"]
    own = tmp_path / "home" / ".local" / "share" / "sandbox-ai"
    installation(own)
    os.symlink("versions/0.9.0", own / "current")
    assert backend.update_route(own / "current" / "sandbox", euid=1000, writable=lambda p: False) == ("user", str(own))


SANDBOX = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3]) / "sandbox"
needs_updater = pytest.mark.skipif(not (SANDBOX / "package_update.py").is_file(),
                                   reason="this sandbox-ai has no package_update.py")


def self_updating_copy(root, kind):
    """A package (KIND: its PACKAGING) or a pip installation ("pip") with sandbox-ai's own package_update.py in it:
    the folder with its modules."""
    import shutil
    directory = root / ("sandbox_ai" if kind == "pip" else "sandbox")
    directory.mkdir(parents=True)
    for name in ("package_update.py", "release_assets.py"):
        shutil.copy2(SANDBOX / name, directory / name)
    if kind == "pip":
        (root / "sandbox_ai_app-1.0.0.dist-info").mkdir()
        (root / "sandbox_ai_app-1.0.0.dist-info" / "INSTALLER").write_text("pip\n")
    else:
        (root / "install.sh").write_text("")
        (root / "PACKAGING").write_text(kind + "\n")
    return directory


@needs_updater
def test_a_package_or_a_pip_installation_that_can_be_written_updates_itself(tmp_path, monkeypatch):
    """The AppImage, the .pyz, the macOS app and a pip installation replace themselves (sandbox-ai update): the
    route "self", with the file or folder that is replaced; one this user cannot write keeps the hint, and the hint
    is the exact command."""
    image = tmp_path / "Applications" / "sandbox-ai-x86_64.AppImage"
    image.parent.mkdir()
    image.write_bytes(b"\x7fELF")
    appimage = self_updating_copy(tmp_path / "cache" / "appimage" / "1.0.0-abc" / "sandbox-ai", "appimage")
    environ = {"SANDBOX_AI_PACKAGE": str(image)}
    monkeypatch.setattr(backend.package_updater(appimage).load("release_assets"), "arch_of",
                        lambda kind, machine=None: "x86_64")
    assert backend.update_route(appimage, environ=environ) == ("self", str(image))
    assert backend.update_blocker(appimage, environ=environ) is None
    no = lambda path, mode: False  # noqa: E731
    assert backend.update_route(appimage, environ=environ, access=no) == ("package", "appimage")
    why = backend.update_blocker(appimage, environ=environ, access=no)
    assert f"{image.parent} is not writable for you" in why and f"sudo -H {image} cli update" in why
    assert backend.update_route(appimage, environ={}) == ("package", "appimage"), "not started from its file"
    bundle = tmp_path / "Applications" / "sandbox-ai.app"
    app = self_updating_copy(bundle / "Contents" / "Resources" / "sandbox-ai", "macos-app")
    assert backend.update_route(app, environ={}) == ("self", str(bundle))
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    pip = self_updating_copy(site, "pip")
    monkeypatch.setattr(backend.package_updater(pip).importlib.util, "find_spec", lambda name: object())
    assert backend.update_route(pip, environ={}) == ("self", str(site))
    assert backend.update_route(pip, environ={}, access=no) == ("other", str(site))
    assert "sudo " in backend.update_blocker(pip, environ={}, access=no)
    sai = __import__("types").SimpleNamespace(directory=pip, command=lambda *args: ["python3", "-I", "main.py", *args])
    assert backend.self_update_command(sai, "self") == ["python3", "-I", "main.py", "update"]


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


def test_update_now_for_a_copy_that_replaces_itself_and_the_command_for_one_that_cannot(window, monkeypatch):
    route(monkeypatch, "self", "/home/u/Applications/sandbox-ai-x86_64.AppImage")
    window.update_checked(0, NEWER, "")
    assert not window.update_now_button.isHidden() and window.actions["self-update"].isEnabled()
    assert "replaces /home/u/Applications/sandbox-ai-x86_64.AppImage" in window.update_now_button.toolTip()
    runs = []
    monkeypatch.setattr(window, "in_terminal", lambda argv, title, ended=None: runs.append(argv) or True)
    window.update_now()
    assert runs == [window.sai.command("update")]
    window.update_running = False
    route(monkeypatch, "package", "appimage")
    monkeypatch.setattr(backend, "update_blocker", lambda directory: "/opt is not writable for you; run the update as "
                        "someone who may write it: sudo -H /opt/sandbox-ai-x86_64.AppImage cli update")
    window.update_checked(0, NEWER, "")
    assert window.update_now_button.isHidden()
    assert "sudo -H /opt/sandbox-ai-x86_64.AppImage cli update" in window.update_label.toolTip()


def test_update_now_asks_and_runs_in_a_terminal(window, monkeypatch):
    runs = []
    monkeypatch.setattr(window, "in_terminal", lambda argv, title, ended=None: runs.append((argv, title, ended)) or True)
    route(monkeypatch, "user")
    window.update_checked(0, NEWER, "")
    assert [told[0] for told in window.notified] == ["sandbox-ai 0.10.1 is available"]  # (told once)
    window.notified.clear()
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


# -- that a newer release is there: seen (user 2026-11-22: "i get info in app that there is an update") ---------------

KEY = "SHA256:4Of0GsID4w0KNilg2VbymVRcyopNJbbq7K1P1qXg+Ug"
# What `sandbox-ai update --check` printed for a .pyz of 0.1.180 against the real GitHub release 0.1.188 (2026-11-22).
OLDER_CHECK = ("installed: 0.1.180 (/home/u/bin/sandbox-ai.pyz)\n"
               "available: 0.1.188 (https://github.com/c0decave/sandbox-ai)\n"
               f"signature: good, by the trusted key {KEY}\n"
               f"trusted keys: {KEY} (this copy's sandbox/release-signers)\n"
               "update: sandbox-ai update (it replaces this copy; the version before is kept: sandbox-ai update "
               "--rollback)\n")
CURRENT_CHECK = OLDER_CHECK.replace("installed: 0.1.180", "installed: 0.1.188")


def test_a_newer_signed_release_is_in_the_status_line_and_notified_once(window, monkeypatch):
    route(monkeypatch, "self", "/home/u/bin/sandbox-ai.pyz")
    window.update_checked(0, OLDER_CHECK, "")
    assert window.update_label.text() == "sandbox-ai 0.1.188 is available (installed: 0.1.180)"
    assert not window.update_notice.isHidden() and not window.update_now_button.isHidden()
    (title, text, level, action), = window.notified
    assert title == "sandbox-ai 0.1.188 is available" and "installed: 0.1.180" in text and level == "info"
    assert action == ("Update now…", window.update_now)
    window.update_checked(0, OLDER_CHECK, "")  # the next check (the next day, the next start): not told again
    assert len(window.notified) == 1 and not window.update_now_button.isHidden()
    window.update_checked(0, CURRENT_CHECK, "")
    assert window.update_notice.isHidden() and "sandbox-ai 0.1.188 is up to date" in window.update_said
    assert len(window.notified) == 1, "nothing told when it is up to date"


def test_a_newer_release_for_a_copy_that_cannot_replace_itself_is_told_with_the_way(window, monkeypatch):
    route(monkeypatch, "checkout", "/home/u/src/sandbox-ai")
    window.update_checked(0, OLDER_CHECK, "")
    assert window.update_now_button.isHidden() and "0.1.188 is available" in window.update_label.text()
    (title, text, level, action), = window.notified
    assert "0.1.188 is available" in title and "git pull" in text and action is None
    assert "<p" not in text, "plain text, not the tooltip's markup"


@pytest.mark.parametrize("line, said", [
    ("signature: BAD: the signature does not match SHA256SUMS (Signature verification failed: incorrect signature)",
     "its signature is BAD"),
    ("signature: BAD: not signed by a trusted release key", "its signature is BAD"),
    ("signature: none (the release has no SHA256SUMS.sig)", "it is not signed"),
    ("signature: not checked: no release key is trusted yet", "it is not signed by a trusted key"),
])
def test_a_newer_release_that_is_not_signed_by_a_trusted_key_is_never_offered(line, said, window, monkeypatch):
    """What `update --check` found for a release whose SHA256SUMS was changed or has no signature (a local fake of
    the real 0.1.188, 2026-11-22): the status line says why it is not offered; no Update now…, no notification."""
    route(monkeypatch, "self", "/home/u/bin/sandbox-ai.pyz")
    window.update_checked(0, OLDER_CHECK.replace(f"signature: good, by the trusted key {KEY}", line), "")
    assert window.update_now_button.isHidden() and not window.actions["self-update"].isEnabled()
    assert window.update_label.text() == f"sandbox-ai 0.1.188 is there, but {said}: not offered"
    assert not window.update_notice.isHidden() and window.notified == []
    unsigned = NEWER.replace("signature: good, by the trusted key SHA256:x\n", "")
    window.update_checked(0, unsigned, "")  # (a check without a signature line - an older sandbox-ai's: not offered)
    assert window.update_now_button.isHidden()


@pytest.mark.parametrize("err, said", [
    ("sandbox-ai update: api.github.com limits the requests to its API (HTTP 403); try again after 10:15; GitHub "
     "allows 60 API requests an hour per address without a token: SANDBOX_AI_TOKEN with any token (no scopes needed "
     "for a public repository) raises that to 5000",
     "No update information: api.github.com limits the requests to its API (HTTP 403)"),
    ("sandbox-ai update: cannot reach api.github.com: [Errno 111] Connection refused",
     "No update information: cannot reach api.github.com: [Errno 111] Connection refused"),
    ("install.sh: cannot reach api.github.com: [Errno -3] Temporary failure in name resolution",
     "No update information: cannot reach api.github.com"),
])
def test_a_check_that_failed_says_why(err, said, window, monkeypatch):
    route(monkeypatch, "self", "/home/u/bin/sandbox-ai.pyz")
    window.update_checked(1, "installed: 0.1.180 (/home/u/bin/sandbox-ai.pyz)\n", err)
    assert window.update_said.startswith(said) and window.notified == []


def test_the_check_is_asked_again_while_the_app_runs(window, monkeypatch):
    """notify's check once a day also when the app runs for days: the window asks every hour whether it is due (the
    update mode's last check), as at its start."""
    from sandbox_ai_app import ui
    assert ui.UPDATE_EVERY_SECONDS == 3600
    asked, checks = [], []
    monkeypatch.setattr(window, "start_update_check", lambda: asked.append(1))
    window.update_timer.timeout.emit()
    assert asked == [1]
    monkeypatch.setattr(window, "check_updates", lambda: checks.append(1))
    day = 1_800_000_000
    data = {"mode": "notify", "installed": "0.1.180", "last_check": day, "result": {
        "installed": "0.1.180", "available": "0.1.188", "signature": "good", "signer": KEY, "newer": True}}
    window.update_clock = lambda: day + 3600
    window.update_mode_read(data, None)
    assert checks == [] and "0.1.188 is available" in window.update_label.text()
    assert len(window.notified) == 1, "the last check's newer release is told (once)"
    window.update_clock = lambda: day + 86400 + 60
    window.update_mode_read(data, None)
    assert checks == [1] and len(window.notified) == 1


UP_TO_DATE = "installed: 0.10.1 (/x)\navailable: 0.10.1 (https://example.org)\nsignature: good, by the trusted key SHA256:x\n"


def test_a_check_asked_for_by_a_click_answers_where_you_are(window, monkeypatch):
    """The user 2026-11-22: "wenn ich auf updates pruefen klicke passiert nichts" - Help > Check for updates wrote what
    it found only into the System page's line, so on any other page nothing showed; and a click while a check still
    ran (up to 60 s against GitHub) did nothing at all. A check you ask for answers in the window's status bar: that
    it runs, then what it found (up to date, available, why it failed), and that one is still running."""
    started = []
    monkeypatch.setattr(window.update_check, "start", lambda argv: started.append(argv) or len(started) == 1)
    window.check_updates()
    assert window.statusBar().currentMessage() == "Checking for a newer sandbox-ai…"
    window.check_updates()  # (the first still runs)
    assert "still running" in window.statusBar().currentMessage(), window.statusBar().currentMessage()
    window.update_checked(0, UP_TO_DATE, "")
    assert window.statusBar().currentMessage() == "sandbox-ai 0.10.1 is up to date."
    started.clear()
    window.check_updates()
    window.update_checked(1, "", "cannot reach api.github.com: [Errno -3] Temporary failure in name resolution")
    assert window.statusBar().currentMessage().startswith("No update information: "), \
        window.statusBar().currentMessage()


def test_the_daily_check_stays_quiet_in_the_status_bar(window):
    """The check nobody clicked (at the start, the hourly tick) keeps to the System page and the status line's
    notice of a newer release: no status-bar message for "up to date" or a failed check."""
    window.statusBar().showMessage("before")
    window.update_checked(0, UP_TO_DATE, "")
    window.update_checked(1, "", "offline")
    assert window.statusBar().currentMessage() == "before"


def test_a_clicked_check_of_a_copy_newer_than_the_newest_release_says_so_plainly(window, monkeypatch):
    """The user's case (2026-11-22): a system installation of 0.1.193 against the newest release 0.1.188 -
    Help > Check for updates seemed to do nothing. What `update --check` printed there, and what the app says now."""
    route(monkeypatch, "root", "/usr/local/lib/sandbox-ai")
    said = ("installed: 0.1.193 (/usr/local/lib/sandbox-ai)\n"
            "available: 0.1.188 (https://github.com/c0decave/sandbox-ai)\n"
            "           older than the installed 0.1.193: an update refuses it without --allow-downgrade\n"
            f"signature: good, by the trusted key {KEY}\n"
            f"trusted keys: {KEY} (the installed tree)\n")
    window.update_asked = True
    window.update_checked(0, said, "")
    text = "sandbox-ai 0.1.193 is newer than the newest release 0.1.188: nothing to update"
    assert window.update_said == text and window.statusBar().currentMessage() == text
    assert window.update_notice.isHidden() and window.notified == [], "nothing offered, nothing notified"


# -- the status line: no text over another (the user 2026-11-24: "Updates suchen und finden ... unten links waren
# Texte überlappend als ein Update gefunden wurde") --

def overlaps(window):
    """[(a, b)] of what overlaps in the window's status bar now: two widgets shown, or a widget shown where QStatusBar
    paints its message (from the left up to its first permanent widget)."""
    bar = window.statusBar()
    shown = [w for w in bar.findChildren(QtWidgets.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
             if w.isVisible() and not isinstance(w, QtWidgets.QSizeGrip) and w.width() > 0]
    found = [(a.objectName() or type(a).__name__, b.objectName() or type(b).__name__)
             for i, a in enumerate(shown) for b in shown[i + 1:] if a.geometry().intersects(b.geometry())]
    if bar.currentMessage():
        permanent = [w for w in shown if w is not window.update_notice]
        right = min([w.geometry().left() for w in permanent] or [bar.width()])
        message = QtCore.QRect(0, 0, right, bar.height())
        found += [("message", w.objectName() or type(w).__name__) for w in shown
                  if w is window.update_notice and w.geometry().intersects(message)]
    return found


def test_an_update_found_never_writes_over_a_message(window, monkeypatch):
    """Offscreen, the window shown: the start's message (sandbox-ai VERSION from PATH) is in the status line when the
    daily check finds a newer release - its notice was shown over that text. Then a check you click (Checking…, then
    what it found), and a message of another page while the notice is shown."""
    route(monkeypatch, "self", "/home/u/bin/sandbox-ai.pyz")
    window.resize(1100, 700)
    window.show()
    QtWidgets.QApplication.processEvents()
    bar = window.statusBar()
    assert bar.currentMessage(), "the start's message is there"
    window.update_checked(0, NEWER, "")  # the daily check, nobody clicked
    QtWidgets.QApplication.processEvents()
    assert overlaps(window) == [], overlaps(window)
    assert window.update_notice.isVisible(), "the newer release is offered"
    window.update_checked(0, NEWER, "")  # (the next check: the same)
    started = []
    monkeypatch.setattr(window.update_check, "start", lambda argv: started.append(argv) or True)
    window.check_updates()  # clicked: Checking… while the notice is there
    QtWidgets.QApplication.processEvents()
    assert overlaps(window) == [], overlaps(window)
    window.update_checked(0, NEWER, "")
    QtWidgets.QApplication.processEvents()
    assert overlaps(window) == [], overlaps(window)
    bar.clearMessage()  # (its time is up)
    QtWidgets.QApplication.processEvents()
    assert window.update_notice.isVisible() and overlaps(window) == []
    bar.showMessage("The browser of web is open in Terminals", 8000)  # another page's message
    QtWidgets.QApplication.processEvents()
    assert overlaps(window) == [], overlaps(window)
    window.busy_label.setText("Running: Kits & plugins…")  # the bar laid out again (QStatusBar's own hiding is undone)
    QtWidgets.QApplication.processEvents()
    assert overlaps(window) == [], overlaps(window)
    bar.clearMessage()
    QtWidgets.QApplication.processEvents()
    assert window.update_notice.isVisible(), "back once the message is gone"
    # The update ran: Restart the app, over no message either.
    bar.showMessage("A terminal window instead of a tab", 20000)
    window.update_ended("0.10.1", 0)
    QtWidgets.QApplication.processEvents()
    assert overlaps(window) == [], overlaps(window)
    window.hide()


def test_the_starts_message_goes_after_a_while(window):
    """The start's own message (sandbox-ai VERSION from PATH) is not kept for ever: the version stays bottom right."""
    from sandbox_ai_app import ui
    assert ui.START_MESSAGE_MS and 0 < ui.START_MESSAGE_MS <= 30000
