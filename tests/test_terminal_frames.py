"""Whole frames on the terminal (runtime.py): the tmux options of every session (escape-time, the synchronized update
for every terminal) and the terminal relay's bursts - a redraw that tmux 3.3 passes on in pieces reaches the
attached terminal as one synchronized update, never with its rows erased and not yet written again (the flicker of
Qwen Code, and of every Ink interface, in tmux)."""
import base64
import importlib.util
import json
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_runtime_frames", ROOT / "sandbox/runtime.py")
rt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt)

BEGIN, END = b"\x1b[?2026h", b"\x1b[?2026l"
RECORDED = ROOT / "tests" / "golden" / "qwen_tmux_redraw.json"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def recorded():
    """(pieces, gaps in s, frame numbers) of the recorded tmux 3.3a redraw."""
    doc = json.loads(RECORDED.read_text())
    data = zlib.decompress(base64.b64decode(doc["data"]))
    pieces, at = [], 0
    for size in doc["pieces"]:
        pieces.append(data[at:at + size])
        at += size
    assert at == len(data)
    return pieces, [gap / 1000 for gap in doc["gaps"]], range(doc["frames"][0], doc["frames"][1] + 1)


def unwrapped(data):
    return re.sub(rb"\x1b\[\?2026[hl]", b"", data)


def torn(deliveries):
    """Deliveries that end inside a frame (after its first row, before its last) with no synchronized update open:
    a terminal may paint there - a frame half erased, the flicker."""
    data = b"".join(deliveries)
    spans = [(m.start(), data.index(b"END" + m.group(1))) for m in re.finditer(rb"TOP([0-9]{5})", data)
             if b"END" + m.group(1) in data]
    count, at = 0, 0
    for delivery in deliveries[:-1]:
        at += len(delivery)
        head = data[:at]
        if any(top < at <= end for top, end in spans) and not head.rfind(BEGIN) > head.rfind(END):
            count += 1
    return count


def burst_all(pieces, gaps):
    """The recorded pieces through TerminalBursts as the relay's loop drives it, on a clock of their gaps."""
    clock = Clock()
    bursts, sent = rt.TerminalBursts(clock), []

    def drain(until):
        while (due := bursts.due()) is not None and clock.now + due <= until:
            clock.now += due
            out = bursts.take()
            if out:
                sent.append(out)
    for piece, gap in zip(pieces, gaps):
        arrives = clock.now + gap
        drain(arrives)
        clock.now = arrives
        bursts.add(piece)
    drain(clock.now + 1)
    sent.append(bursts.finish())
    return [out for out in sent if out]


# -- tmux's options --


def test_tmux_options_come_before_the_session():
    cmd = rt.tmux_command(["qwen"])
    options = list(rt.TMUX_OPTIONS)
    assert cmd[:2 + len(options)] == ["tmux", "-u", *options]
    assert cmd[2 + len(options)] == "new-session"
    assert ["set-option", "-s", "escape-time", "10", ";"] == options[:5]
    sync = options[options.index("terminal-overrides") + 1]
    # tmux 3.4's "sync" feature, with the final ";" escaped (unescaped it would end tmux's command there)
    assert sync == ",*:Sync=\\E[?2026%?%p1%{1}%-%tl%eh%\\;"
    detached = rt.tmux_command(["qwen"], detached=True)
    assert rt.shlex.join(["tmux", "-u", *options, "new-session", "-d"]) in detached[2]


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_tmux_takes_the_options_and_a_tmux_conf_cannot_undo_them(tmp_path):
    """A real tmux: the options are set after its configuration files, the override reads as tmux 3.4's sync."""
    socket_path, conf = tmp_path / "s", tmp_path / "tmux.conf"
    conf.write_text("set -s escape-time 777\n")
    base = ["tmux", "-S", str(socket_path), "-f", str(conf)]
    cmd = rt.tmux_command(["sleep", "30"])
    session = cmd[cmd.index("new-session"):]
    session[session.index("new-session") + 1:session.index("new-session") + 1] = ["-d"]
    session = [part.replace("/work", str(tmp_path)) for part in session]
    try:
        subprocess.run(base + cmd[1:cmd.index("new-session")] + session, check=True, timeout=20)
        shown = subprocess.run(base + ["show-options", "-s"], capture_output=True, text=True, timeout=20).stdout
        assert "escape-time 10" in shown
        assert '*:Sync=\\\\E[?2026%?%p1%{1}%-%tl%eh%;' in shown
        windows = subprocess.run(base + ["list-windows", "-F", "#W"], capture_output=True, text=True,
                                 timeout=20).stdout.split()
        assert windows == ["agent", "shell"]
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, timeout=20)


# -- where a burst may end --


@pytest.mark.parametrize("data, cut", [
    (b"plain text", 10),
    (b"text\x1b[12;3", 4),             # CSI without its final byte
    (b"text\x1b[12;34H", 12),
    (b"text\x1b", 4),
    (b"text\x1b(", 4), (b"text\x1b(B", 7),
    (b"text\x1b]0;title", 4), (b"text\x1b]0;title\x07", 14),
    (b"text\x1b]52;c;YWJj\x1b\\", 17),   # ST: the last ESC is the end
    (b"text\x1bPtmux;", 4),            # DCS: only ST ends it
    (b"text\x1b7", 6),
    ("ab€".encode(), 5), ("ab€".encode()[:-1], 2), ("ab€".encode()[:-2], 2),
    ("a😀".encode()[:-1], 1), ("a😀".encode(), 5),
    (b"\x1b[31m\xc3", 5),
])
def test_safe_cut(data, cut):
    assert rt.safe_cut(bytearray(data)) == cut


# -- bursts --


def test_a_burst_waits_for_quiet_and_goes_as_one_synchronized_update():
    clock = Clock()
    bursts = rt.TerminalBursts(clock)
    assert bursts.due() is None
    bursts.add(b"\x1b[H\x1b[2Kfirst")
    assert bursts.due() == pytest.approx(rt.TERMINAL_QUIET)
    clock.now += 0.001
    bursts.add(b" second")
    assert bursts.due() == pytest.approx(rt.TERMINAL_QUIET)  # each piece starts the quiet again
    clock.now += rt.TERMINAL_QUIET
    assert bursts.due() == 0
    assert bursts.take() == BEGIN + b"\x1b[H\x1b[2Kfirst second" + END
    assert bursts.due() is None and bursts.finish() == b""


def test_steady_output_still_flows():
    clock = Clock()
    bursts = rt.TerminalBursts(clock)
    sent = []
    for _ in range(100):  # a piece every 2 ms, never quiet
        bursts.add(b"x" * 100)
        clock.now += 0.002
        if bursts.due() == 0:
            sent.append(bursts.take())
    assert len(sent) >= 100 * 0.002 / rt.TERMINAL_HOLD - 1
    assert all(len(unwrapped(out)) <= 100 * (rt.TERMINAL_HOLD / 0.002 + 1) for out in sent)
    bursts.add(b"y" * rt.TERMINAL_BURST)
    assert bursts.due() == 0  # a big one goes at once


def test_an_unfinished_sequence_waits_for_the_next_burst():
    clock = Clock()
    bursts = rt.TerminalBursts(clock)
    euro = "€".encode()
    bursts.add(b"row one " + euro[:2])  # a character in two reads
    clock.now += rt.TERMINAL_HOLD
    assert bursts.take() == BEGIN + b"row one " + END
    assert bytes(bursts.pending) == euro[:2]
    assert bursts.take() == b"" and bursts.stuck  # all that waits is unfinished: it waits for more
    assert bursts.due() == pytest.approx(0)  # (its time is up: the next take would send it as it is)
    bursts.add(euro[2:] + b"\x1b[12;3")  # and a sequence in two
    clock.now += rt.TERMINAL_QUIET
    assert bursts.take() == BEGIN + euro + END
    bursts.add(b"4H")
    clock.now += rt.TERMINAL_QUIET
    assert bursts.take() == BEGIN + b"\x1b[12;34H" + END


def test_a_sequence_that_never_ends_is_sent_as_it_is():
    clock = Clock()
    bursts = rt.TerminalBursts(clock)
    bursts.add(b"\x1b]52;c;" + b"A" * 100)
    clock.now += rt.TERMINAL_QUIET
    assert bursts.take() == b"" and bursts.due() == pytest.approx(rt.TERMINAL_HOLD - rt.TERMINAL_QUIET)
    clock.now += rt.TERMINAL_HOLD
    assert bursts.take() == b"\x1b]52;c;" + b"A" * 100
    bursts.add(b"more")  # the stream's state is unknown now: nothing is wrapped any more
    clock.now += rt.TERMINAL_QUIET
    assert bursts.take() == b"more"
    huge = rt.TerminalBursts(clock)
    huge.add(b"\x1b]52;c;" + b"A" * rt.TERMINAL_RAW)
    assert huge.due() == 0 and huge.take().startswith(b"\x1b]52;c;AAAA")


def test_the_streams_own_synchronized_updates_merge_into_the_bursts():
    clock = Clock()
    bursts = rt.TerminalBursts(clock)
    bursts.add(BEGIN + b"one" + END + BEGIN + b"two" + END + b"three")
    clock.now += rt.TERMINAL_QUIET
    assert bursts.take() == BEGIN + b"onetwothree" + END  # one update: no paint between one and two
    bursts.add(b"four" + BEGIN + b"fi")  # ends inside the stream's own update: the burst's stays open
    clock.now += rt.TERMINAL_QUIET
    assert bursts.take() == BEGIN + b"fourfi"
    bursts.add(b"ve" + END)
    clock.now += rt.TERMINAL_QUIET
    assert bursts.take() == BEGIN + b"ve" + END
    bursts.add(BEGIN + b"six")
    clock.now += rt.TERMINAL_QUIET
    assert bursts.take() == BEGIN + b"six"
    assert bursts.finish() == END  # never left open after the attach ends


def test_the_recorded_qwen_redraw_reaches_the_terminal_whole():
    """Measured: tmux 3.3a gave the 8 recorded frames in 19 pieces, 11 of them ending inside a frame with nothing
    to hold the paint. Through the bursts: one synchronized update per frame, none torn, every byte kept."""
    pieces, gaps, frames = recorded()
    assert torn(pieces) == 11 and len(pieces) == 19  # before: what the relay passed on, read by read
    sent = burst_all(pieces, gaps)
    assert len(sent) == len(frames) and torn(sent) == 0
    assert all(out.startswith(BEGIN) and out.endswith(END) and out.count(b"TOP") == 1 for out in sent)
    assert unwrapped(b"".join(sent)) == unwrapped(b"".join(pieces))


def test_the_relay_sends_the_recorded_redraw_frame_by_frame(tmp_path):
    """End to end through relay_terminal: a client replays the recorded pieces with their gaps on the relay's pty;
    the connection gets one synchronized update per frame."""
    pieces, gaps, frames = recorded()
    script = tmp_path / "replay.py"
    data = tmp_path / "pieces.json"
    data.write_text(json.dumps({"pieces": [base64.b64encode(p).decode() for p in pieces], "gaps": gaps}))
    script.write_text("import base64, json, os, sys, time, tty\n"
                      "tty.setraw(1)\n"
                      f"doc = json.load(open({str(data)!r}))\n"
                      "for piece, gap in zip(doc['pieces'], doc['gaps']):\n"
                      "    time.sleep(gap)\n"
                      "    os.write(1, base64.b64decode(piece))\n"
                      "time.sleep(0.2)\n")
    left, right = socket.socketpair()
    thread = threading.Thread(target=rt.relay_terminal, args=(left, [sys.executable, str(script)]), daemon=True)
    thread.start()
    right.sendall(rt.attach_frame(b"r", struct.pack("!HH", 51, 200)))
    right.settimeout(15)
    received = b""
    deliveries = []
    try:
        while chunk := right.recv(1 << 20):
            received += chunk
            deliveries.append(chunk)
    except (ConnectionResetError, socket.timeout):
        pass
    thread.join(10)
    assert unwrapped(received) == unwrapped(b"".join(pieces))
    updates = re.findall(re.escape(BEGIN) + rb"(.*?)" + re.escape(END), received, re.S)
    assert all(update.count(b"TOP") <= 1 for update in updates)
    assert sum(update.count(b"TOP") for update in updates) == len(frames)
    assert torn(deliveries) == 0
