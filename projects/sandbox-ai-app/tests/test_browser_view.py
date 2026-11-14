"""The live view of a sandbox's own browser (--browser) offscreen: the app's VNC client (vnc_view.RfbClient) speaking
RFB with a server played byte by byte - the handshake, Raw, CopyRect, DesktopSize, the clipboard, and what it refuses
(another version, a password, a rectangle outside the display, a display too large, a clipboard text too long) - the
socket it refuses (not yours, not a socket, a folder others can open), the keyboard and mouse it sends, the window
(its [x], plain Esc closes it, Shift+Esc goes to the browser, the clipboard only when shared), the Browser button of
the Sandboxes page and of a tab's menu, and the board's Browser part. With Xvfb and x11vnc on this machine, the view
also connects to a real x11vnc on a Unix socket, as the browser's container serves it."""
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent  # noqa: E402

from sandbox_ai_app import backend, board as B, vnc_view as V, widgets  # noqa: E402


class Sent(V.RfbClient):
    """A client whose output is recorded and whose input is fed by the test (no socket)."""

    def __init__(self):
        super().__init__()
        self.out, self.failures, self.cuts = [], [], []
        self.failed.connect(self.failures.append)
        self.cut_text.connect(self.cuts.append)
        self.parser, self.buffer = self.protocol(), bytearray()
        self.need = next(self.parser)

    def send(self, data):
        self.out.append(bytes(data))


def server_init(width=4, height=2, name=b"browser"):
    return struct.pack(">HH", width, height) + b"\0" * 16 + struct.pack(">I", len(name)) + name


def handshake(client, width=4, height=2):
    client.feed(b"RFB 003.008\n")
    client.feed(b"\x01\x01")  # one security type: None
    client.feed(struct.pack(">I", 0))  # SecurityResult: OK
    client.feed(server_init(width, height))


def raw(x, y, w, h, pixel=b"\x10\x20\x30\x00"):
    return struct.pack(">HHHHi", x, y, w, h, 0) + pixel * (w * h)


def test_the_handshake_and_what_the_viewer_asks_for(qapp):
    client = Sent()
    connected = []
    client.connected.connect(lambda w, h, name: connected.append((w, h, name)))
    handshake(client)
    assert connected == [(4, 2, "browser")] and client.ready and not client.failures
    assert client.out[0] == b"RFB 003.008\n" and client.out[1] == b"\x01" and client.out[2] == b"\x01"
    pixel_format = client.out[3]
    assert pixel_format[:4] == b"\0\0\0\0" and pixel_format[4] == 32 and pixel_format[7] == 1  # true colour
    encodings = client.out[4]
    assert struct.unpack(">BxH", encodings[:4]) == (2, 3)
    assert [struct.unpack(">i", encodings[i:i + 4])[0] for i in (4, 8, 12)] == [0, 1, -223]
    assert client.out[5] == struct.pack(">BBHHHH", 3, 0, 0, 0, 4, 2)  # a full update first


def test_raw_copyrect_desktop_size_and_the_clipboard(qapp):
    client = Sent()
    handshake(client)
    client.out.clear()
    client.feed(b"\x00\x00" + struct.pack(">H", 2) + raw(0, 0, 1, 1) +
                struct.pack(">HHHHi", 3, 1, 1, 1, 1) + struct.pack(">HH", 0, 0))
    assert client.image.pixel(0, 0) & 0xFFFFFF == 0x302010 and client.image.pixel(3, 1) & 0xFFFFFF == 0x302010
    assert client.out[-1] == struct.pack(">BBHHHH", 3, 1, 0, 0, 4, 2)  # then incremental ones
    client.feed(b"\x00\x00" + struct.pack(">H", 1) + struct.pack(">HHHHi", 0, 0, 8, 6, -223))
    assert (client.image.width(), client.image.height()) == (8, 6)
    assert client.image.pixel(0, 0) & 0xFFFFFF == 0x302010, "what was there stays"
    client.feed(b"\x02")  # a bell: nothing
    client.feed(b"\x03\0\0\0" + struct.pack(">I", 5) + b"hello")
    assert client.cuts == ["hello"] and not client.failures
    client.feed(b"\x01\0" + struct.pack(">HH", 0, 1) + b"\0" * 6)  # a colour map: skipped
    assert not client.failures


@pytest.mark.parametrize("messages, why", [
    ([b"HTTP/1.1 200 OK\n"[:12]], "not a VNC server"),
    ([b"RFB 003.008\n", b"\x01\x02"], "asks for a password"),  # VNC authentication only
    ([b"RFB 003.003\n", struct.pack(">I", 2)], "asks for a password"),
    ([b"RFB 003.008\n", b"\x00", struct.pack(">I", 4) + b"nope"], "refused: nope"),
    ([b"RFB 003.008\n", b"\x01\x01", struct.pack(">I", 1), struct.pack(">I", 3) + b"bad"], "refused: bad"),
    ([b"RFB 003.008\n", b"\x01\x01", struct.pack(">I", 0), server_init(9000, 10)], "display of 9000x10"),
])
def test_what_the_viewer_refuses_in_the_handshake(qapp, messages, why):
    client = Sent()
    for message in messages:
        client.feed(message)
    assert client.failures and why in client.failures[0]


@pytest.mark.parametrize("message, why", [
    (b"\x00\x00\x00\x01" + raw(3, 1, 2, 1), "outside its display"),
    (b"\x00\x00\x00\x01" + struct.pack(">HHHHi", 0, 0, 1, 1, 1) + struct.pack(">HH", 4, 0), "copies from outside"),
    (b"\x00\x00\x00\x01" + struct.pack(">HHHHi", 0, 0, 1, 1, 16), "did not ask for (16)"),  # ZRLE: not asked for
    (b"\x03\0\0\0" + struct.pack(">I", (1 << 20) + 1), "clipboard text is too long"),
    (b"\x07", "does not know (7)"),
])
def test_what_the_viewer_refuses_later(qapp, message, why):
    client = Sent()
    handshake(client)
    client.feed(message)
    assert client.failures and why in client.failures[0]
    client.feed(raw(0, 0, 1, 1))  # nothing more is read once it failed
    assert len(client.failures) == 1


def test_versions_3_3_and_3_7(qapp):
    old = Sent()
    old.feed(b"RFB 003.003\n" + struct.pack(">I", 1) + server_init())
    assert old.ready and old.out[0] == b"RFB 003.003\n"
    seven = Sent()
    seven.feed(b"RFB 003.007\n" + b"\x01\x01" + server_init())  # no SecurityResult with None in 3.7
    assert seven.ready and seven.out[0] == b"RFB 003.007\n"
    newer = Sent()
    newer.feed(b"RFB 003.889\n")  # Apple's: answered as 3.8
    assert newer.out[0] == b"RFB 003.008\n"


def test_keys_pointer_and_cut_sent(qapp):
    client = Sent()
    handshake(client)
    client.out.clear()
    client.key(0xFF0D, True)
    client.pointer(10, -3, 1)  # clamped into the display
    client.cut("é")
    assert client.out == [struct.pack(">BBxxI", 4, 1, 0xFF0D), struct.pack(">BBHH", 5, 1, 3, 0),
                          struct.pack(">BxxxI", 6, 1) + "é".encode("latin-1")]
    assert V.keysym(Qt.Key.Key_A, "a") == 0x61 and V.keysym(Qt.Key.Key_Escape, "") == 0xFF1B
    assert V.keysym(Qt.Key.Key_C, "") == 0x63, "Ctrl+C: Qt gives no text"
    assert V.keysym(0, "€") == 0x01000000 | 0x20AC and V.keysym(0, "") is None
    assert V.keysym(Qt.Key.Key_F5, "") == 0xFFC2


# -- the socket --

@pytest.fixture
def short(tmp_path):
    """A folder with a short path (a Unix socket's path is at most 107 bytes), removed afterwards."""
    import tempfile
    folder = Path(tempfile.mkdtemp(prefix="sai-vnc-", dir="/tmp"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def unix_socket(path):
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)
    return server


def test_the_socket_must_be_yours_alone(short):
    folder = short / "claude-1.browser-vnc"
    folder.mkdir(mode=0o700)
    path = folder / "vnc.sock"
    assert V.socket_problem(path) == "it is not there (yet)"
    path.write_text("x")
    assert V.socket_problem(path) == "not a socket"
    path.unlink()
    server = unix_socket(path)
    try:
        assert V.socket_problem(path) is None
        folder.chmod(0o755)
        assert V.socket_problem(path) == "its folder is not yours alone"
        folder.chmod(0o700)
        assert V.socket_problem(Path("relative/vnc.sock")) == "not an absolute path"
    finally:
        server.close()


# -- the view and the window --

def key_event(kind, key, modifiers=Qt.KeyboardModifier.NoModifier, text=""):
    return QKeyEvent(kind, key, modifiers, text)


def test_the_view_sends_keys_and_the_mouse_and_esc_closes_the_window(qapp, tmp_path):
    window = V.BrowserWindow("Browser of api", tmp_path / "none" / "vnc.sock")
    client = window.client
    sent = []
    client.send = sent.append
    client.parser, client.buffer = client.protocol(), bytearray()
    client.need, client.dead = next(client.parser), False
    handshake(client, 100, 50)
    window.show()
    window.resize(400, 300)
    qapp.processEvents()
    assert window.findChild(widgets.CloseCorner, "closeCorner") is not None, "an [x] at its top right"
    view = window.view
    sent.clear()
    QtWidgets.QApplication.sendEvent(view, key_event(QEvent.Type.KeyPress, Qt.Key.Key_A, text="a"))
    QtWidgets.QApplication.sendEvent(view, key_event(QEvent.Type.KeyRelease, Qt.Key.Key_A, text="a"))
    assert sent == [struct.pack(">BBxxI", 4, 1, 0x61), struct.pack(">BBxxI", 4, 0, 0x61)]
    sent.clear()
    QtWidgets.QApplication.sendEvent(view, key_event(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                                     Qt.KeyboardModifier.ShiftModifier))
    assert sent == [struct.pack(">BBxxI", 4, 1, 0xFF1B)], "Shift+Esc goes to the browser"
    sent.clear()
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(20, 10), QPointF(20, 10), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(view, press)
    area = view.target()  # the display scaled into the view, its aspect kept
    assert area.width() / area.height() == pytest.approx(2.0)
    x, y = view.to_display(QPointF(20, 10))
    assert x == pytest.approx((20 - area.x()) * 100 / area.width())
    assert sent[-1] == struct.pack(">BBHH", 5, 1, max(0, min(int(x), 99)), max(0, min(int(y), 49)))
    view.setFocus()
    QtWidgets.QApplication.sendEvent(view, key_event(QEvent.Type.KeyPress, Qt.Key.Key_Escape))
    qapp.processEvents()
    assert not window.isVisible(), "plain Esc closes it, as every extra window"


def test_a_browser_behind_tor_says_in_its_tab_that_it_is_not_tor_browser(qapp, tmp_path):
    """Verification of the browser behind Tor (0.1.95): the start said it once in its terminal; the tab and window
    where you browse now say it too (run.json "browser": {"tor": true})."""
    record = {"browser": {"kind": "firefox", "control": "human", "vnc": str(tmp_path / "x" / "vnc.sock"),
                          "tor": True}}
    browser = V.browser_record(type("Instance", (), {"record": record})())
    assert browser["tor"] is True and V.tor_of(browser) == "firefox"
    window = V.BrowserWindow("Browser", tmp_path / "none" / "vnc.sock", tor=V.tor_of(browser))
    assert "Behind Tor this is Firefox ESR, not Tor Browser" in window.note.text()
    assert "Use Tor Browser for anonymity" in window.note.text()
    plain = V.browser_record(type("Instance", (), {"record": {"browser": {**record["browser"], "tor": False}}})())
    assert V.tor_of(plain) is None
    window = V.BrowserWindow("Browser", tmp_path / "none" / "vnc.sock", tor=V.tor_of(plain))
    assert "Tor" not in window.note.text()
    panel = V.BrowserPanel("Browser", tmp_path / "none" / "vnc.sock", tor="chromium")
    assert "Behind Tor this is Chromium, not Tor Browser" in panel.note.text()


def test_the_clipboard_only_when_shared(qapp, tmp_path):
    window = V.BrowserWindow("Browser", tmp_path / "none" / "vnc.sock", control="both")
    client = window.client
    sent = []
    client.send = sent.append
    client.parser, client.buffer = client.protocol(), bytearray()
    client.need, client.dead = next(client.parser), False
    handshake(client)
    clipboard = QGuiApplication.clipboard()
    clipboard.setText("mine")
    qapp.processEvents()
    assert not window.share.isChecked() and not any(m[:1] == b"\x06" for m in sent), "off by default"
    client.feed(b"\x03\0\0\0" + struct.pack(">I", 6) + b"theirs")
    assert clipboard.text() == "mine", "the browser's clipboard does not reach yours unasked"
    window.share.setChecked(True)
    assert any(m == struct.pack(">BxxxI", 6, 4) + b"mine" for m in sent)
    before = len(sent)
    client.feed(b"\x03\0\0\0" + struct.pack(">I", 6) + b"theirs")
    qapp.processEvents()
    assert clipboard.text() == "theirs"
    assert not any(m[:1] == b"\x06" for m in sent[before:]), "the browser's own text is not sent back to it (review r1)"
    clipboard.setText("mine again")
    qapp.processEvents()
    assert sent[-1] == struct.pack(">BxxxI", 6, 10) + b"mine again"
    assert "the agent can use" in window.note.text()
    window.close()


def test_the_window_waits_for_the_socket_and_refuses_a_foreign_one(qapp, short, monkeypatch):
    monkeypatch.setattr(V, "RETRY_MS", 10)
    folder = short / "run.browser-vnc"
    folder.mkdir(mode=0o700)
    window = V.BrowserWindow("Browser", folder / "vnc.sock")
    assert "Waiting" in window.status.text() and window.timer.isActive()
    folder.chmod(0o755)
    server = unix_socket(folder / "vnc.sock")
    try:
        window.connect_now()
        assert "refused" in window.status.text() and "not yours alone" in window.status.text()
    finally:
        server.close()
        window.close()


def test_why_not_and_one_window_per_browser(qapp, tmp_path, monkeypatch):
    def instance(state="running", host="local", browser=None):
        return backend.Instance("session", "session:api", "api", run="claude-1", session="api", state=state,
                                agent="claude", host=host, record={"browser": browser} if browser else {})
    shown = {"kind": "chromium", "control": "human", "vnc": str(tmp_path / "x.browser-vnc" / "vnc.sock")}
    assert V.why_not(None) == "Select a start first."
    assert "no browser of its own" in V.why_not(instance())
    assert "only a browser of this machine" in V.why_not(instance(host="ct1", browser=shown))
    assert "Only the agent drives" in V.why_not(instance(browser={**shown, "control": "agent", "vnc": None}))
    assert V.why_not(instance(state="paused", browser=shown)) == "It does not run."
    assert "no browser" in V.why_not(instance(browser={**shown, "kind": "opera"})), "a damaged record: none"
    assert V.why_not(instance(browser={**shown, "vnc": "relative/vnc.sock"})).startswith("Only the agent")
    assert V.why_not(instance(browser=shown)) is None
    opened = []
    monkeypatch.setattr(V.BrowserWindow, "connect_now", lambda self: opened.append(self.path))
    first = V.open_browser(instance(browser=shown))
    second = V.open_browser(instance(browser=shown))
    assert first is second and opened == [shown["vnc"]] and first.windowTitle() == "Browser of api"
    first.close()
    assert V.open_browser(instance()) is None


# -- the Sandboxes page, the tab's menu, the board --

@pytest.fixture
def sai(tmp_path, monkeypatch):
    if not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"):
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    return backend.SandboxAI()


def test_the_browser_button_of_the_sandboxes_page(qapp, sai, tmp_path, monkeypatch):
    from sandbox_ai_app import ui
    page = ui.SandboxesPage(sai, lambda *a, **k: None)
    browser = {"kind": "chromium", "control": "both", "vnc": str(tmp_path / "x.browser-vnc" / "vnc.sock")}
    with_browser = backend.Instance("session", "session:api", "api", run="claude-1", session="api", state="running",
                                    agent="claude", record={"browser": browser})
    without = backend.Instance("session", "session:web", "web", run="claude-2", session="web", state="running",
                               agent="claude", record={})
    remote = backend.Instance("session", "remote:ct1:api", "api", host="ct1", state="running", agent="claude",
                              record={"browser": browser})
    # A start with a browser can be clicked also when it cannot be shown now (live use 2026-11-14: a greyed-out entry
    # did nothing): the tooltip says what the click will say. A start without one stays greyed out.
    for instance, enabled, why in ((with_browser, True, ""), (without, False, "no browser of its own"),
                                   (remote, True, "Not now: Its browser's display is a socket on that host")):
        monkeypatch.setattr(page, "current", lambda i=instance: i)
        page.update_buttons()
        button = page.run_named["browser"]
        assert button.isEnabled() is enabled, instance.key
        assert why in button.toolTip()
    opened = []
    monkeypatch.setattr(V, "open_browser", lambda instance, parent=None: opened.append(instance.key) or "window")
    monkeypatch.setattr(page, "current", lambda: with_browser)
    assert page.open_browser() == "window" and opened == ["session:api"]
    monkeypatch.setattr(page, "current", lambda: without)
    monkeypatch.setattr(ui, "inform", lambda parent, title, text: opened.append(text))
    assert page.open_browser() is None and "no browser of its own" in opened[-1]


def test_the_board_has_a_browser_part(sai):
    assert B.KINDS["browser"].options == ("--browser", "--browser-control", "--browser-profile")
    assert B.OWNER["--browser-control"] == "browser"
    board = B.from_argv(sai, "claude", ["--browser", "--gui"])
    node = board.first("browser")
    assert node is not None and node.props.get("--browser") == "chromium", "--browser alone: Chromium"
    assert board.first("display").props.get("--gui") is True, "the next option is not taken for its KIND"
    argv = B.to_argv(sai, board, workspaces=False)
    assert "--browser" in argv and argv[argv.index("--browser") + 1] == "chromium"
    board = B.from_argv(sai, "claude", ["--browser", "firefox", "--browser-control", "both", "--browser-profile",
                                        "/srv/profile"])
    _, values, _ = B.values(board)
    assert values["--browser"] == "firefox" and values["--browser-control"] == "both"
    _, found = B.badges(board)
    joined = " ".join(badge.text for badge in found)
    assert "The agent drives the browser" in joined and "the agent can use" in joined and "/srv/profile" in joined
    plain = B.from_argv(sai, "claude", [])
    assert plain.first("browser") is None


def test_a_preset_carries_the_browser_and_builds_its_part(sai):
    from sandbox_ai_app import presets as PR
    document = {**PR.to_document(PR.find("strict-rules")), "id": "web-agent",
                "options": ["--browser", "--browser-control", "agent"]}
    preset = PR.from_document(sai, document)
    board = PR.build(sai, preset)
    node = board.first("browser")
    assert node is not None and node.props["--browser"] == "chromium" and node.props["--browser-control"] == "agent"
    assert "--browser-control" in B.to_argv(sai, board, workspaces=False)


def test_the_guided_setup_asks_for_the_browser():
    from sandbox_ai_app import guide
    questions = guide.NODE_QUESTIONS["browser"]
    assert [question.key for question in questions] == ["--browser", "--browser-control", "--browser-profile"]
    assert dict(questions[1].choices)["agent"] == "the agent" and set(B.KINDS["browser"].options) == {
        question.key for question in questions}


# -- a real x11vnc on a Unix socket, as the browser's container serves it --

@pytest.mark.skipif(not (shutil.which("Xvfb") and shutil.which("x11vnc")), reason="needs Xvfb and x11vnc")
def test_the_view_connects_to_a_real_x11vnc_on_a_unix_socket(qapp, short):
    folder = short / "run.browser-vnc"
    folder.mkdir(mode=0o700)
    path = folder / "vnc.sock"
    display = f":{60 + os.getpid() % 300}"
    processes = [subprocess.Popen(["Xvfb", display, "-screen", "0", "320x200x24", "-nolisten", "tcp", "-nolisten",
                                   "local", "-extension", "MIT-SHM"], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)]
    try:
        deadline = time.monotonic() + 10
        while not Path(f"/tmp/.X11-unix/X{display[1:]}").exists():
            assert time.monotonic() < deadline, "Xvfb did not start"
            time.sleep(0.05)
        processes.append(subprocess.Popen(["x11vnc", "-display", display, "-unixsock", str(path), "-rfbport", "0",
                                           "-forever", "-shared", "-nopw", "-quiet", "-noshm"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        while not path.exists():
            assert time.monotonic() < deadline, "x11vnc did not listen"
            time.sleep(0.05)
        client = V.RfbClient()
        seen, failures, updates = [], [], []
        client.connected.connect(lambda w, h, name: seen.append((w, h)))
        client.failed.connect(failures.append)
        client.updated.connect(updates.append)
        assert client.open(path)
        deadline = time.monotonic() + 10
        while not (seen and updates) and not failures and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert not failures and seen == [(320, 200)] and updates, (failures, seen)
        client.pointer(10, 10, 0)
        client.key(0x61, True)
        client.key(0x61, False)
        for _ in range(20):
            qapp.processEvents()
            time.sleep(0.01)
        assert not failures
        client.close()
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()


def test_an_empty_copyrect_still_carries_its_source_and_the_stream_stays_in_step(qapp):
    """A CopyRect rectangle always carries its 4-byte source position, also when it is 0 wide or high: the viewer
    reads it, and the message after it is read as one (a skipped source would be read as the next message)."""
    client = Sent()
    handshake(client)
    client.feed(b"\x00\x00" + struct.pack(">H", 2) + struct.pack(">HHHHi", 0, 0, 0, 1, 1) + struct.pack(">HH", 0, 0)
                + raw(1, 1, 1, 1, b"\x40\x50\x60\x00"))
    assert not client.failures and client.image.pixel(1, 1) & 0xFFFFFF == 0x605040
    client.feed(b"\x03\0\0\0" + struct.pack(">I", 2) + b"ok")
    assert client.cuts == ["ok"] and not client.failures
