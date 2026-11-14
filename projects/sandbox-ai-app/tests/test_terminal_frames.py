"""Whole frames in the terminal tabs (pty_bridge.Frames, web/terminal.js): an interface's redraw that reaches the
app in pieces goes to xterm.js as one write and is painted once - never with its rows erased and not yet written
again (the flicker of Qwen Code in tmux). The stream is a recording of tmux 3.3a passing on Qwen-like redraws
(tests/recorded/qwen_tmux_redraw.json, made with the core's tests/golden copy)."""
import base64
import json
from pathlib import Path
import re
import sys
import zlib

import pytest

from sandbox_ai_app import pty_bridge
from sandbox_ai_app.pty_bridge import Frames

HERE = Path(__file__).resolve().parent
RECORDED = HERE / "recorded" / "qwen_tmux_redraw.json"
BEGIN, END = b"\x1b[?2026h", b"\x1b[?2026l"


class Clock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        return self.now


def recorded():
    """(pieces, gaps in s, frame numbers, rows, columns) of the recorded redraw."""
    doc = json.loads(RECORDED.read_text())
    data = zlib.decompress(base64.b64decode(doc["data"]))
    pieces, at = [], 0
    for size in doc["pieces"]:
        pieces.append(data[at:at + size])
        at += size
    return (pieces, [gap / 1000 for gap in doc["gaps"]], range(doc["frames"][0], doc["frames"][1] + 1),
            doc["rows"], doc["columns"])


def frames_of(pieces):
    """The recorded redraw cut into its frames (each from its first row to its last)."""
    data = b"".join(pieces)
    starts = [m.start() for m in re.finditer(rb"\x1b\[H\x1b\[[0-9;]*mTOP", data)]
    return [data[start:(starts[i + 1] if i + 1 < len(starts) else len(data))] for i, start in enumerate(starts)]


def torn(deliveries):
    """Deliveries that end inside a frame with no synchronized update open: a paint there shows it half erased."""
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


def through(frames, pieces, gaps):
    """PIECES arriving after GAPS (s) through FRAMES on its clock: what goes to the page, write by write."""
    sent = []

    def drain(until):
        while (due := frames.due()) is not None and frames.clock.now + due <= until:
            frames.clock.now += due
            sent.append(frames.take())
    for piece, gap in zip(pieces, gaps):
        arrives = frames.clock.now + gap
        drain(arrives)
        frames.clock.now = arrives
        frames.add(piece)
    drain(frames.clock.now + 1)
    return sent


# -- Frames on a clock --


def test_frames_wait_for_quiet_hold_at_most_and_send_big_output_at_once():
    frames = Frames(Clock())
    assert frames.due() is None
    frames.add(b"a")
    assert frames.due() == pytest.approx(pty_bridge.FRAME_QUIET)
    for _ in range(30):  # never quiet: it goes FRAME_HOLD after its first byte all the same
        frames.clock.now += pty_bridge.FRAME_QUIET / 2
        frames.add(b"b")
    assert frames.due() == 0 and frames.take() == b"a" + b"b" * 30 and frames.due() is None
    frames.add(b"x" * pty_bridge.FRAME_LIMIT)
    assert frames.due() == 0


def test_an_open_synchronized_update_is_held_until_its_end():
    frames = Frames(Clock())
    frames.add(b"text" + BEGIN + b"\x1b[2K")
    frames.clock.now += pty_bridge.FRAME_HOLD  # quiet and past the usual hold: still waits for the update's end
    assert frames.synced and frames.due() == pytest.approx(pty_bridge.SYNC_HOLD - pty_bridge.FRAME_HOLD)
    frames.add(b"row" + END[:4])  # the end marker split over two reads
    frames.add(END[4:])
    assert not frames.synced and frames.due() == 0  # ended, and its hold is over: it goes at once
    assert frames.take() == b"text" + BEGIN + b"\x1b[2Krow" + END


def test_an_update_that_never_ends_is_over_after_the_sync_hold():
    frames = Frames(Clock())
    frames.add(BEGIN + b"a")
    frames.clock.now += pty_bridge.SYNC_HOLD
    assert frames.due() == 0 and frames.take() == BEGIN + b"a"
    frames.add(b"b")  # as xterm.js after its own wait: the next output is not held for it again
    assert frames.due() == pytest.approx(pty_bridge.FRAME_QUIET)


def test_the_recorded_redraw_goes_frame_by_frame():
    """Measured on the recording: tmux 3.3a's 19 pieces of 8 frames, 11 of them ending inside a frame; through Frames
    one write per frame and none torn (the pieces of one follow within 3 ms, the frames 50 ms apart)."""
    pieces, gaps, numbers, _, _ = recorded()
    assert len(pieces) == 19 and torn(pieces) == 11
    sent = through(Frames(Clock()), pieces, gaps)
    assert len(sent) == len(numbers) and torn(sent) == 0
    assert b"".join(sent) == b"".join(pieces)


# -- the bridge, with a real pseudo-terminal --


def replay(tmp_path, pieces, gaps, acked=()):
    """A command that writes PIECES after GAPS (s), unchanged (raw mode), then waits. After each piece whose index is
    in ACKED it waits for one byte of input (collect's acks): what follows cannot reach the terminal before the test
    has seen all up to there, however late a loaded machine runs the reader."""
    data = tmp_path / "pieces.json"
    data.write_text(json.dumps({"pieces": [base64.b64encode(p).decode() for p in pieces], "gaps": gaps,
                                "acked": sorted(acked)}))
    script = tmp_path / "replay.py"
    script.write_text("import base64, json, os, sys, time, tty\n"
                      "tty.setraw(1)\n"
                      f"doc = json.load(open({str(data)!r}))\n"
                      "acked = set(doc['acked'])\n"
                      "time.sleep(0.3)\n"
                      "for index, (piece, gap) in enumerate(zip(doc['pieces'], doc['gaps'])):\n"
                      "    time.sleep(gap)\n"
                      "    os.write(1, base64.b64decode(piece))\n"
                      "    if index in acked:\n"
                      "        os.read(0, 1)\n"
                      "print('DONE', flush=True)\n"
                      "time.sleep(30)\n")
    return [sys.executable, str(script)]


def relayed(pieces, piece_size=4096, gap=0.006, ends=None):
    """The recording as the terminal relay sends it (each frame one synchronized update), arriving in pieces
    PIECE_SIZE apart by GAP s - longer than FRAME_QUIET: only the update keeps a frame together. ENDS, a list, gets
    the index of each frame's last piece."""
    out, gaps = [], []
    for frame in frames_of(pieces):
        wrapped = BEGIN + frame + END
        for at in range(0, len(wrapped), piece_size):
            out.append(wrapped[at:at + piece_size])
            gaps.append(0.04 if at == 0 else gap)
        if ends is not None:
            ends.append(len(out) - 1)
    return out, gaps


def collect(bridge_class, argv, wait, stream=None, acked=()):
    """What the bridge sends of ARGV (decoded chunks). With ACKED (indices into STREAM, as given to replay): once all
    bytes up to the end of such a piece have come, the command is told to go on (one byte of input)."""
    bridge = bridge_class(argv, cols=200, rows=51)
    out = []
    marks = sorted(sum(len(piece) for piece in stream[:index + 1]) for index in acked)
    seen = [0]

    def received(chunk):
        out.append(chunk)
        seen[0] += len(base64.b64decode(chunk))
        while marks and seen[0] >= marks[0]:
            marks.pop(0)
            bridge.write(base64.b64encode(b"k").decode())
    bridge.output.connect(received)
    bridge.ready()
    try:
        assert wait(lambda: b"DONE" in b"".join(base64.b64decode(c) for c in out), 30)
    finally:
        bridge.close()
    return [base64.b64decode(c) for c in out]


@pytest.fixture
def bridge_class(qapp):
    pytest.importorskip("PySide6.QtCore")
    return pty_bridge.PtyBridge


def test_the_bridge_sends_each_relayed_frame_in_one_write(bridge_class, wait, tmp_path, monkeypatch):
    """Deterministic on a loaded machine: the next frame (now) or piece (before) is written only once the test has
    received everything before it, so no two of them can reach one read of the terminal however late the reader runs.
    The synchronized update keeps a frame together; SYNC_HOLD, the cap for one that never ends (tested with a clock
    above), is set out of reach: a stalled writer must not cut a frame that does end."""
    pieces, _, numbers, _, _ = recorded()
    ends = []
    stream, gaps = relayed(pieces, ends=ends)
    monkeypatch.setattr(pty_bridge, "SYNC_HOLD", 30)
    sent = collect(bridge_class, replay(tmp_path, stream, gaps, ends), wait, stream, ends)
    with_frames = [chunk for chunk in sent if b"TOP" in chunk]
    assert len(with_frames) == len(numbers) and all(chunk.count(b"TOP") == 1 for chunk in with_frames)
    assert all(chunk.startswith(BEGIN) and END in chunk for chunk in with_frames)
    # before: every read went to the page on its own - several writes per frame
    for name in ("FRAME_QUIET", "FRAME_HOLD", "SYNC_HOLD"):
        monkeypatch.setattr(pty_bridge, name, 0)
    every = range(len(stream))
    before = [chunk for chunk in collect(bridge_class, replay(tmp_path, stream, gaps, every), wait, stream, every)
              if re.search(rb"TOP|END|r[0-9]{2}\.", chunk)]
    assert len(before) >= 2 * len(numbers)


# -- xterm.js itself (vendored): what is painted --

PAINTS = r"""
(function () {
  window.paints = { all: 0, torn: 0, whole: 0 };
  window.sandboxTerminal.rendered(function () {
    const rows = window.sandboxTerminal.text().split("\n").slice(0, %d);
    const tags = rows.map(function (row) {
      const found = /(?:TOP|END|r[0-9]{2}\.)([0-9]{5})/.exec(row);
      return found ? found[1] : null;
    });
    if (tags.indexOf(null) === -1 && new Set(tags).size === 1) { window.paints.whole++; }
    else if (tags.some(function (tag) { return tag !== null; })) { window.paints.torn++; }
    window.paints.all++;
  });
})();
"""


@pytest.mark.parametrize("held", [True, False], ids=["now", "before"])
def test_xterm_paints_every_frame_whole(qapp, wait, tmp_path, monkeypatch, held):
    """The real xterm.js: every paint of the relayed recording shows one frame whole. Measured before (no frames
    in the bridge, no synchronized update from the relay: the pieces as tmux 3.3a gave them): torn paints."""
    from sandbox_ai_app import terminal
    from test_terminal_tools import js, settings  # noqa: F401
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    ready, why = terminal.available()
    if not ready:
        pytest.skip(f"needs xterm.js vendored by scripts/vendor_xterm.py: {why}")
    pieces, gaps, numbers, rows, columns = recorded()
    # a frame's pieces 25 ms apart (a busy machine, a slow link), so that a paint can fall between them
    if held:
        stream, stream_gaps = relayed(pieces, gap=0.025)
    else:
        for name in ("FRAME_QUIET", "FRAME_HOLD", "SYNC_HOLD"):
            monkeypatch.setattr(pty_bridge, name, 0)
        stream, stream_gaps = pieces, [0.06 if gap > 0.02 else 0.025 for gap in gaps]
    view = terminal.TerminalView(replay(tmp_path, stream, stream_gaps), web=terminal.WEB)
    view.resize(1900, 1100)
    view.show()
    try:
        assert wait(lambda: view.bridge.ready_seen, 30), "the page never connected"
        assert wait(lambda: js(view, wait, "typeof window.sandboxTerminal") == "object", 20)
        size = json.loads(js(view, wait, "JSON.stringify(window.sandboxTerminal.size())"))
        if size[0] < columns or size[1] < rows + 1:
            pytest.skip(f"the offscreen view has {size}, the recording needs {columns}x{rows + 1}")
        js(view, wait, PAINTS % rows)
        assert wait(lambda: "DONE" in (js(view, wait, "window.sandboxTerminal.text()", 2) or ""), 30)
        wait(lambda: False, 0.3)
        paints = json.loads(js(view, wait, "JSON.stringify(window.paints)"))
        print(f"\npaints ({'now' if held else 'before'}): {paints} for {len(numbers)} frames")
        assert paints["whole"] >= 1
        if held:
            assert paints["torn"] == 0, paints
    finally:
        view.close_terminal()
