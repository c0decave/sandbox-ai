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


# --- after an update: the start learns which app runs, and an older one makes way (2026-11-18: "I ran install.sh
# --from . several times, but sandbox-ai-app says 0.1.97" - the running 0.1.97 kept answering every start with its
# window). Old and new apps on both ends of the socket: the running app in a process of its own (it answers on its
# GUI thread while this start waits on its own). ---

from pathlib import Path  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

from sandbox_ai_app import instance_probe  # noqa: E402

APP_ROOT = Path(__file__).resolve().parents[1]  # projects/sandbox-ai-app
CURRENT = instance_probe.__version__

# A running app of the new protocol: single_instance.Instance as the app has it, VERSION and LOCATION as given, its
# quit_handler as the window's (MODE quit: it quits; decline: the human kept the window; linger: says it quits and
# never ends; busy: quits, but its GUI thread is busy for 3.5 s first, as an app that has just started).
NEW_APP = """
import sys
import time
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QCoreApplication, QTimer
from sandbox_ai_app import single_instance
app = QCoreApplication([])
instance = single_instance.Instance(sys.argv[2], version=sys.argv[3], location=sys.argv[4])
assert instance.claim() is True
mode = sys.argv[5]
instance.asked.connect(lambda word: print("asked", word, flush=True))
def handler():
    if mode == "decline":
        return False
    if mode in ("quit", "busy"):
        QTimer.singleShot(0, app.quit)
    return True
instance.quit_handler = handler
if mode == "busy":
    QTimer.singleShot(0, lambda: time.sleep(3.5))
print("ready", flush=True)
app.exec()
instance.release()
print("ended", flush=True)
"""

# A running app before 0.1.99, its socket as single_instance.py had it then: "show" and "hidden", anything else
# ignored and the connection closed; started as the launcher starts it (the app's folder an argument).
OLD_APP = """
import sys
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalServer
app = QCoreApplication([])
server = QLocalServer()
server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
assert server.listen(sys.argv[2])
def read(client):
    word = bytes(client.readAll().data()).strip()[:16]
    if word in (b"show", b"hidden"):
        print("asked", word.decode(), flush=True)
    client.disconnectFromServer()
def connected():
    while server.hasPendingConnections():
        client = server.nextPendingConnection()
        client.readyRead.connect(lambda c=client: read(c))
        client.disconnected.connect(client.deleteLater)
server.newConnection.connect(connected)
print("ready", flush=True)
app.exec()
"""


@pytest.fixture
def short_dir():
    """A folder with a short path (a Unix socket's path has room for about 100 bytes)."""
    folder = Path(tempfile.mkdtemp(prefix="si-"))
    folder.chmod(0o700)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def fake_install(root, version):
    """ROOT/versions/VERSION/projects/sandbox-ai-app with a sandbox_ai_app/__init__.py of VERSION (an installation's
    folder of an older app, as the launcher passes it); the app folder."""
    app = root / "versions" / version / "projects" / "sandbox-ai-app"
    (app / "sandbox_ai_app").mkdir(parents=True)
    (app / "sandbox_ai_app" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    return app


class Running:
    def __init__(self, script, *args):
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        self.process = subprocess.Popen([sys.executable, "-I", "-c", script, *map(str, args)], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, env=env)
        line = self.process.stdout.readline()
        assert line.strip() == "ready", self.process.stderr.read() if self.process.poll() is not None else line

    def lines(self):
        """What it printed, once it has ended (ended within 10 s, else killed)."""
        try:
            out, _ = self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            out, _ = self.process.communicate()
        return out.split()

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        return self.lines()


def new_app(short_dir, version, mode="quit", location=None):
    return Running(NEW_APP, APP_ROOT, short_dir / "app.sock", version, location or instance_probe.HERE, mode)


def test_versions_are_compared_as_numbers():
    older = instance_probe.Running("info", version="0.1.97", path=str(instance_probe.HERE))
    assert instance_probe.verdict(older, False, "0.1.99") == "takeover"
    assert instance_probe.verdict(older, False, "0.1.100") == "takeover", "0.1.100 is newer than 0.1.97"
    assert instance_probe.verdict(older, True, "0.1.99") == "hidden", "a start for the tray never takes over"
    assert instance_probe.verdict(older, False, "0.1.9") == "newer"
    same = instance_probe.Running("info", version="0.1.99", path=str(instance_probe.HERE))
    assert instance_probe.verdict(same, False, "0.1.99") == "show"
    assert instance_probe.verdict(same, False, "0.1.99", "/usr/local/lib/sandbox-ai/versions/0.1.99") == "takeover", \
        "the same version from another installation: the one started now"
    assert instance_probe.verdict(instance_probe.Running("old"), False, "0.1.99") == "old"
    assert instance_probe.verdict(instance_probe.Running("old"), True, "0.1.99") == "hidden"
    assert instance_probe.verdict(instance_probe.Running("silent"), False, "0.1.99") == "show", \
        "nothing known (it did not answer in time): as before"


def test_a_new_app_says_what_it_is(qapp, short_dir):
    running = new_app(short_dir, "0.1.97", location="/somewhere/sandbox_ai_app")
    try:
        found = instance_probe.ask(short_dir / "app.sock")
        assert (found.protocol, found.version, found.path) == ("info", "0.1.97", "/somewhere/sandbox_ai_app")
        assert found.pid == running.process.pid
        assert instance_probe.ask(short_dir / "nobody.sock") is None
    finally:
        assert "asked" not in running.stop(), "info shows no window"


def test_an_old_start_is_answered_by_a_new_app_as_before(qapp, short_dir):
    running = new_app(short_dir, CURRENT)
    try:
        for word in (b"show", b"hidden"):  # what a start before 0.1.99 sends, and nothing else
            old = QLocalSocket()
            old.connectToServer(str(short_dir / "app.sock"))
            assert old.waitForConnected(500)
            old.write(word)
            old.flush()
            old.waitForBytesWritten(500)
            old.disconnectFromServer()
        time.sleep(1)
    finally:
        out = running.stop()
    assert out.count("asked") == 2 and "show" in out and "hidden" in out


def test_an_older_app_makes_way_for_the_new_start(qapp, short_dir):
    running = new_app(short_dir, "0.1.97")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim() is True, start.notice
        assert running.process.wait(10) == 0 and "ended" in running.lines()
        assert start.server is not None and start.lock_fd is not None, "the new start is the app now"
        assert start.notice is None
    finally:
        start.release()
        running.stop()


def test_an_older_app_busy_for_a_while_still_makes_way(qapp, short_dir):
    """An older app whose GUI thread is busy past the answer's 2 s (it has just started, as the AppImage's after an
    update: test_update_github_e2e) said nothing in time - "silent", so the new start only asked it to show its
    window and ended: the older version stayed. It is asked again for a while; once it answers, it makes way."""
    running = new_app(short_dir, "0.1.97", mode="busy")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim() is True, start.notice
        assert running.process.wait(10) == 0 and "ended" in running.lines()
        assert start.notice is None
    finally:
        start.release()
        running.stop()


def test_the_same_version_from_another_installation_makes_way_too(qapp, short_dir):
    running = new_app(short_dir, CURRENT, location="/usr/local/lib/sandbox-ai/versions/x/sandbox_ai_app")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim() is True
    finally:
        start.release()
        running.stop()


def test_a_start_for_the_tray_never_takes_over(qapp, short_dir):
    running = new_app(short_dir, "0.1.97")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim(hidden=True) is False and start.notice is None
        time.sleep(0.5)
        assert running.process.poll() is None, "the running app was not asked to quit"
    finally:
        out = running.stop()
    assert "hidden" in out


def test_the_human_keeps_the_older_app_and_is_told(qapp, short_dir):
    running = new_app(short_dir, "0.1.97", mode="decline", location="/opt/old/sandbox_ai_app")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim() is False
        assert running.process.poll() is None
        text = start.notice_text()
        assert "sandbox-ai-app 0.1.97 is still running (from /opt/old/sandbox_ai_app" in text
        assert f"quit it to use {CURRENT}" in text and start.alert is True
    finally:
        running.stop()


def test_an_older_app_that_does_not_end_in_time(qapp, short_dir, monkeypatch):
    monkeypatch.setattr(single_instance, "QUIT_WAIT", 1.5)
    running = new_app(short_dir, "0.1.97", mode="linger")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim() is False
        assert "0.1.97 is still running" in start.notice_text() and start.alert is True
    finally:
        running.stop()


def test_a_newer_app_is_shown(qapp, short_dir):
    running = new_app(short_dir, "9.0.0")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim() is False and start.alert is False
        assert "sandbox-ai-app 9.0.0" in start.notice_text()
        time.sleep(0.5)
    finally:
        out = running.stop()
    assert "show" in out


def test_an_app_too_old_to_ask_is_named_with_its_version_place_and_process(qapp, short_dir):
    """The real case (2026-11-18): the system installation's 0.1.97 runs, the user's own 0.1.99 starts. 0.1.97 knows
    no word to quit: its window comes to the front and the human is told which one runs, from where, how to end it."""
    app = fake_install(short_dir / "usr-local-lib", "0.1.97")
    running = Running(OLD_APP, app, short_dir / "app.sock")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim() is False
        text = start.notice_text()
        assert "sandbox-ai-app 0.1.97 is still running" in text, text
        assert str((app / "sandbox_ai_app").resolve()) in text and f"kill {running.process.pid}" in text
        assert f"to use {CURRENT}" in text and start.alert is True
        time.sleep(0.5)
    finally:
        out = running.stop()
    assert out.count("asked") == 1 and "show" in out, "its window to the front (and info was ignored, as then)"


def test_a_start_for_the_tray_leaves_an_old_app_alone(qapp, short_dir):
    running = Running(OLD_APP, fake_install(short_dir, "0.1.97"), short_dir / "app.sock")
    start = single_instance.Instance(short_dir / "app.sock")
    try:
        assert start.claim(hidden=True) is False and start.notice is None
        time.sleep(0.5)
    finally:
        out = running.stop()
    assert out == ["asked", "hidden"]


def test_install_sh_learns_what_runs(qapp, short_dir):
    """install.sh's note (the installed copy's instance_probe.main): an older app that runs, how to get the new one."""
    assert instance_probe.installed_note([short_dir / "app.sock"]) is None
    running = Running(OLD_APP, fake_install(short_dir, "0.1.97"), short_dir / "app.sock")
    try:
        note = instance_probe.installed_note([short_dir / "app.sock"])
        assert note.startswith("sandbox-ai-app 0.1.97 is running") and "too old" in note and "kill " in note
    finally:
        running.stop()
    running = new_app(short_dir, "0.1.97")
    try:
        note = instance_probe.installed_note([short_dir / "app.sock"])
        assert "0.1.97 is running" in note and "it takes over" in note
    finally:
        running.stop()
    running = new_app(short_dir, CURRENT)
    try:
        assert instance_probe.installed_note([short_dir / "app.sock"]) is None, "the app installed now runs"
    finally:
        running.stop()


def test_the_command_line_names_the_app_it_runs(tmp_path):
    app = fake_install(tmp_path, "0.1.97")
    launcher = ["/usr/bin/python3", "-I", "-c", "import runpy, sys\nsys.path.insert(0, sys.argv[1])", str(app),
                "--hidden"]
    assert instance_probe.package_of(launcher) == (app / "sandbox_ai_app").resolve()
    autostart = ["/usr/bin/python3", "-I", "-c", f"import sys; sys.path[0:0] = ['{app}']; from sandbox_ai_app.main "
                 "import main; sys.exit(main())", "--hidden"]
    assert instance_probe.package_of(autostart) == (app / "sandbox_ai_app").resolve()
    assert instance_probe.version_in(app / "sandbox_ai_app") == "0.1.97"
    assert instance_probe.package_of(["/usr/bin/python3", "-c", "pass"]) is None
