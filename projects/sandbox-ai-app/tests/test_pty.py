"""The terminal bridge, with a real pseudo-terminal: output, input, resize, exit and hang-up."""
import base64
import os
import signal
import sys
import time

import pytest

from sandbox_ai_app import pty_bridge
from sandbox_ai_app.pty_bridge import Pty


def read_until(pty, needle, timeout=10):
    data, deadline = b"", time.monotonic() + timeout
    while needle not in data and time.monotonic() < deadline:
        chunk = pty.read()
        if chunk is None:
            break
        data += chunk
        if not chunk:
            time.sleep(0.01)
    return data


def test_output_input_and_exit_status():
    pty = Pty(["sh", "-c", "echo hi; read x; echo got $x; exit 3"])
    try:
        assert b"hi" in read_until(pty, b"hi")
        assert pty.write(b"abc\n") == 0
        assert b"got abc" in read_until(pty, b"got abc")
        read_until(pty, b"never")  # to the end
        assert pty.wait(5) == 3 and pty.read() is None
    finally:
        pty.close()


def test_it_is_a_real_terminal_with_a_size():
    pty = Pty(["sh", "-c", "test -t 0 && echo tty; stty size; read x; stty size"], cols=100, rows=30)
    try:
        assert b"tty" in read_until(pty, b"30 100")
        assert pty.resize(120, 40) == (120, 40) and pty.size() == (120, 40)
        pty.write(b"\n")
        assert b"40 120" in read_until(pty, b"40 120")
        assert pty.resize(0, 99999) == (1, pty_bridge.MAX_SIZE)
    finally:
        pty.close()


def test_resize_sends_sigwinch():
    pty = Pty(["sh", "-c", "trap 'echo winch' WINCH; echo ready; while :; do sleep 0.05; done"])
    try:
        read_until(pty, b"ready")
        pty.resize(90, 20)
        assert b"winch" in read_until(pty, b"winch")
    finally:
        pty.close()
        pty.kill()


def test_close_hangs_up_and_kill_ends_the_rest():
    pty = Pty(["sh", "-c", "echo up; sleep 60"])
    read_until(pty, b"up")
    pid = pty.pid
    pty.close()
    try:
        status = pty.wait(5)
    except Exception:  # noqa: BLE001
        pty.kill()
        status = pty.wait(5)
    assert status in (-signal.SIGHUP, -signal.SIGKILL, 128 + signal.SIGHUP)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # A command that ignores SIGHUP is killed.
    stubborn = Pty(["sh", "-c", "trap '' HUP; echo up; while :; do sleep 0.05; done"])
    read_until(stubborn, b"up")
    stubborn.close()
    time.sleep(0.2)
    assert stubborn.poll() is None
    stubborn.kill()
    assert stubborn.wait(5) == -signal.SIGKILL


def test_no_shell_and_environment():
    with pytest.raises(ValueError):
        Pty("echo hi")
    with pytest.raises(ValueError):
        Pty([])
    pty = Pty(["sh", "-c", "echo TERM=$TERM; echo \"$1\"", "sh", "a; echo injected"], env={"PATH": os.environ["PATH"]})
    try:
        out = read_until(pty, b"injected")
        assert b"TERM=xterm-256color" in out and b"a; echo injected" in out
        assert b"\r\ninjected" not in out
    finally:
        pty.close()


def test_a_missing_program_ends_with_an_error():
    pty = Pty(["/nonexistent/program"])
    try:
        out = read_until(pty, b"never", timeout=5)
        assert pty.wait(5) == 127 and b"cannot run" in out and b"nonexistent" in out
    finally:
        pty.close()


# -- the Qt bridge --


@pytest.fixture
def bridge_class(qapp):
    pytest.importorskip("PySide6.QtCore")
    return pty_bridge.PtyBridge


def decode(chunks):
    return b"".join(base64.b64decode(c) for c in chunks)


def test_bridge_backlog_until_ready_then_input_and_exit(bridge_class, wait):
    bridge = bridge_class(["sh", "-c", "echo hi; read x; echo got $x"], exit_text="[exit status {status}]")
    out, statuses = [], []
    bridge.output.connect(out.append)
    bridge.exited.connect(statuses.append)
    assert wait(lambda: bridge.backlog)  # output before the page is ready is kept
    assert out == []
    bridge.ready()
    assert b"hi" in decode(out)
    bridge.write(base64.b64encode(b"abc\r").decode())
    assert wait(lambda: b"got abc" in decode(out))
    assert wait(lambda: statuses == [0])
    assert b"[exit status 0]" in decode(out) and not bridge.running
    bridge.close()


def test_bridge_resize_bad_input_and_close(bridge_class, wait):
    bridge = bridge_class(["sh", "-c", "read x; stty size; sleep 60"], kill_after=500)
    out, statuses = [], []
    bridge.output.connect(out.append)
    bridge.exited.connect(statuses.append)
    bridge.ready()
    bridge.resize(132, 43)
    bridge.write("not base64!!")  # ignored
    bridge.write(base64.b64encode(b"x" * (pty_bridge.INPUT_LIMIT + 1)).decode())  # too much at once: ignored
    bridge.write(base64.b64encode(b"\r").decode())
    assert wait(lambda: b"43 132" in decode(out))
    assert bridge.theme == "dark"
    bridge.close()
    assert wait(lambda: statuses, timeout=5) and statuses[0] < 0


def test_bridge_stops_reading_while_the_page_does_not_keep_up(bridge_class, wait, monkeypatch):
    """Regression: a flood of output was sent to the page as fast as it could be read (1 MiB per pass), with
    nothing to hold it back: it piled up in the page's queue without limit. The page acknowledges each chunk;
    while more than ACK_LIMIT characters are not acknowledged, the terminal is not read (the command waits),
    and reading goes on after acks - nothing is lost."""
    limit = 64 * 1024
    monkeypatch.setattr(pty_bridge, "ACK_LIMIT", limit, raising=False)
    total = 3 << 20  # far more than the limit and than the terminal's own buffer
    script = (f"import sys; data = bytes(range(32, 127)) * ({total} // 95 + 1); "
              f"sys.stdout.buffer.write(data[:{total}]); sys.stdout.flush(); print(); print('END')")
    bridge = bridge_class([sys.executable, "-c", "import tty, sys; tty.setraw(0); " + script])
    out = []
    bridge.output.connect(out.append)
    bridge.ready()
    assert wait(lambda: getattr(bridge, "paused", False), 10), "a page that does not acknowledge stops the reading"
    sent = sum(len(c) for c in out)
    wait(lambda: False, 0.5)
    assert sum(len(c) for c in out) == sent, "nothing more is sent while it is paused"
    assert sent <= limit + 65536 * 4 // 3 + 4, "at most the limit and one chunk more"
    assert bridge.running and not bridge.pty.eof, "the command waits: its output is still to come"
    acked = 0

    def acknowledge():
        nonlocal acked
        pending = sum(len(c) for c in out) - acked
        if pending:
            bridge.ack(pending)
            acked += pending
        return b"END" in decode(out[-4:])
    assert wait(acknowledge, 60), "acknowledged, it goes on to the end"
    data = decode(out)
    expected = (bytes(range(32, 127)) * (total // 95 + 1))[:total]
    assert data[:total] == expected and b"END" in data[total:], "nothing lost: every byte, in order"
    bridge.close()
