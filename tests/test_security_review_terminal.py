"""Security review SAI-08: what the sandbox writes reaches the human's terminal only through runtime.TerminalFilter.

`sandbox-ai attach` and `sandbox-ai shell` were byte pipes to the terminal of the human: a process of the sandbox
could ask that terminal for its clipboard (OSC 52 "?") or its title, set titles, links, notifications and the
directory of the next tab, move the window, and feed DCS/APC parsers. The filter parses the stream as a terminal
does and passes text, the drawing sequences of a known shape, colours and - unless strict - the queries answered
with numbers only; everything else is dropped whole. Unit tests of the shapes, the same stream cut at every byte, a
fuzz against an independent reading of the output, and the two commands through ./sandbox-ai on a real pty."""
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import select
import struct
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_fakes  # noqa: E402


def load(name, as_name):
    spec = importlib.util.spec_from_file_location(as_name, ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime", "sandbox_runtime_review_terminal_test")
launcher = load("agent_sandbox", "sandbox_launcher_review_terminal_test")

ESC = b"\x1b"
ST = ESC + b"\\"


def passed(data, strict=False):
    screen = rt.TerminalFilter(strict=strict)
    return screen.feed(data), dict(screen.dropped)


# --- what passes -----------------------------------------------------------------------------------------------------

DRAWING = [
    b"plain text\r\n\ttabbed\x08\x07", "héllo 世界 \U0001f600".encode(),
    ESC + b"[0m", ESC + b"[1;31;48;2;10;20;30m", ESC + b"[38:5:196m", ESC + b"[H", ESC + b"[12;40H", ESC + b"[2J",
    ESC + b"[K", ESC + b"[?25l", ESC + b"[?25h", ESC + b"[?1049h", ESC + b"[?1049l", ESC + b"[?2004h",
    ESC + b"[?1000;1006h", ESC + b"[?2026h", ESC + b"[?2026l", ESC + b"[1;24r", ESC + b"[s", ESC + b"[u",
    ESC + b"[4 q", ESC + b"[!p", ESC + b"[>4;2m", ESC + b"[>1u", ESC + b"[<u", ESC + b"[=1;1u", ESC + b"[22;0;0t",
    ESC + b"[23;0;0t", ESC + b"[3A", ESC + b"[10b", ESC + b"[5S", ESC + b"[2T", ESC + b"[4h", ESC + b"[?1J",
    ESC + b"7", ESC + b"8", ESC + b"M", ESC + b"=", ESC + b">", ESC + b"c", ESC + b"(B", ESC + b"(0", ESC + b")0",
    ESC + b"#8", b"\x0e\x0f",
]


@pytest.mark.parametrize("sequence", DRAWING)
def test_text_and_drawing_sequences_pass_unchanged(sequence):
    for strict in (False, True):
        assert passed(sequence, strict) == (sequence, {})


def test_colours_are_set_and_reset_and_a_clipboard_text_is_set():
    for osc in (b"4;1;rgb:ff/00/00", b"10;#ffffff", b"11;rgb:0000/0000/0000", b"12;red", b"104", b"104;1", b"110",
                b"111", b"112"):
        for end in (b"\x07", ST):
            assert passed(ESC + b"]" + osc + end, strict=True) == (ESC + b"]" + osc + ST, {})  # (one end: ST)
    # A clipboard text set (tmux's copy mode): your terminal, or the desktop app (which asks), decides. Not in strict.
    assert passed(ESC + b"]52;c;aGVsbG8=\x07") == (ESC + b"]52;c;aGVsbG8=" + ST, {})
    assert passed(ESC + b"]52;;aGVsbG8=" + ST)[0] == ESC + b"]52;;aGVsbG8=" + ST
    assert passed(ESC + b"]52;c;aGVsbG8=\x07", strict=True) == (b"", {"clipboard": 1})
    assert passed(ESC + b"]52;c;not base64!\x07") == (b"", {"clipboard": 1})


QUERIES = [ESC + b"[6n", ESC + b"[5n", ESC + b"[?6n", ESC + b"[c", ESC + b"[0c", ESC + b"[>c", ESC + b"[>0q",
           ESC + b"[?2026$p", ESC + b"[4$p", ESC + b"[?u", ESC + b"[14t", ESC + b"[18t", ESC + b"[?996n",
           ESC + b"]11;?" + ST, ESC + b"]10;?" + ST, ESC + b"]4;1;?" + ST]


@pytest.mark.parametrize("query", QUERIES)
def test_queries_answered_with_numbers_or_the_terminals_name_pass_unless_strict(query):
    """Interfaces wait for these (the cursor's position, device attributes, a mode's state, sizes, a colour); nothing
    of the human's is in their answers. Strict: none passes."""
    assert passed(query) == (query, {})
    assert passed(query, strict=True) == (b"", {"query": 1})


# --- what never passes -----------------------------------------------------------------------------------------------

REFUSED = [
    (ESC + b"]52;c;?\x07", "clipboard read"), (ESC + b"]52;p;?" + ST, "clipboard read"), (ESC + b"]52;?" + ST, "clipboard read"),
    (ESC + b"[21t", "window"), (ESC + b"[20t", "window"),          # the title and icon label reported
    (ESC + b"[8;50;200t", "window"), (ESC + b"[2t", "window"), (ESC + b"[3;0;0t", "window"),
    (ESC + b"]0;rm -rf ~\x07", "osc"), (ESC + b"]2;title" + ST, "osc"), (ESC + b"]1;icon\x07", "osc"),
    (ESC + b"]8;;https://evil.example/" + ST, "osc"),               # a hyperlink
    (ESC + b"]7;file://host/etc\x07", "osc"),                       # the directory of the next tab
    (ESC + b"]9;notification\x07", "osc"), (ESC + b"]777;notify;a;b\x07", "osc"), (ESC + b"]99;;x" + ST, "osc"),
    (ESC + b"]1337;File=name=eA==;inline=0:AAAA\x07", "osc"),       # iTerm2: a file put on the host
    (ESC + b"]50;?\x07", "osc"), (ESC + b"]50;xterm\x07", "osc"),   # the font (CVE-2022-45063)
    (ESC + b"]133;A\x07", "osc"), (ESC + b"]xyz\x07", "osc"), (ESC + b"]\x07", "osc"),
    (ESC + b"P$qm" + ST, "string"), (ESC + b"P+q544e" + ST, "string"),   # DECRQSS, XTGETTCAP
    (ESC + b"Ptmux;" + ESC + ESC + b"]52;c;?\x07" + ST, None),      # tmux passthrough around a clipboard query
    (ESC + b"Pq#0;2;0;0;0#0~~@@vv@@~~$" + ST, "string"),            # Sixel
    (ESC + b"_Gf=100,a=T;AAAA" + ST, "string"), (ESC + b"^pm" + ST, "string"), (ESC + b"Xsos" + ST, "string"),
    (ESC + b"[5i", "report"), (ESC + b"[4i", "report"), (ESC + b"[?5i", "report"),   # the printer
    (ESC + b"[x", "report"), (ESC + b"[1x", "report"), (ESC + b"[4;1y", "report"), (ESC + b"[?4m", "report"),
    (ESC + b"[?1;1;0S", "report"), (ESC + b"[=c", "report"), (ESC + b"[1$w", "report"), (ESC + b"[&u", "report"),
    (ESC + b'[1;1;1;1;1"v', "report"), (ESC + b"[#|", "report"), (ESC + b"[#R", "report"),
    (ESC + b"[1;1;1;1*y", "report"),                                # DECRQCRA: a checksum of the screen
    (ESC + b"Z", "escape"), (ESC + b" G", "escape"), (ESC + b" F", "escape"), (ESC + b"%@", "escape"),
    (ESC + b"n", "escape"), (ESC + b"~", "escape"), (ESC + b"l", "escape"), (ESC + b"V", "escape"),
    (ESC + b"ktitle of your own tmux window" + ST, "string"),      # screen's and tmux's title
    (ESC + b"%G", "escape"), (ESC + b"$(A", "escape"),
    (b"\x05", "control"), (b"\x00", "control"), (b"\x11", "control"),
    (b"\xc2\x9b6n", "control"), (b"\xc2\x9d52;c;?\xc2\x9c", "control"), (b"\xc2\x90$qm\xc2\x9c", "control"),
    (b"\x9b6n", "bytes"), (b"\x9d0;t\x9c", "bytes"), (b"\xff\xfe", "bytes"), (b"\xc0\xaf", "bytes"),
    (b"\xed\xa0\x80", "bytes"),
    (ESC + b"[6?n", "malformed"), (ESC + b"[1;?6n", "malformed"), (ESC + b"[>1;>2m", "malformed"),
]


@pytest.mark.parametrize("sequence,reason", REFUSED)
def test_what_reaches_beyond_the_screen_or_is_answered_with_the_humans_data_is_dropped(sequence, reason):
    for strict in (False, True):
        out, dropped = passed(b"before " + sequence + b" after", strict)
        # Nothing of the sequence's control part is left: what remains is plain text (a C1 control's tail, "6n").
        assert ESC not in out and b"\x07" not in out and b"\x05" not in out and b"\x9b" not in out
        assert out.startswith(b"before ") and out.endswith(b" after")
        assert sum(dropped.values()) >= 1 and (reason is None or reason in dropped), dropped


def test_a_string_is_dropped_to_its_end_whatever_ends_it():
    # ST and (for an OSC) BEL end a string; CAN, SUB and another escape sequence end it without one.
    assert passed(ESC + b"]0;a" + ST + b"x")[0] == b"x"
    assert passed(ESC + b"]0;a\x07x")[0] == b"x"
    assert passed(ESC + b"]0;a\x18x")[0] == b"x"
    assert passed(ESC + b"]0;a\x1ax")[0] == b"x"
    assert passed(ESC + b"]0;a" + ESC + b"[31mx")[0] == ESC + b"[31mx"
    assert passed(ESC + b"Pdcs\x07still dcs" + ST + b"x")[0] == b"x"  # (BEL ends an OSC only)
    assert passed(ESC + b"Pdcs" + ESC + b"]52;c;?\x07x")[0] == b"x"   # a string begun inside a string
    assert passed(ESC + b"]11;rgb:00/00/00" + ESC + b"[31mx")[0] == ESC + b"[31mx"  # not ended: never passed
    # A string that never ends takes what follows with it, as in a terminal: nothing of it is written.
    screen = rt.TerminalFilter()
    assert screen.feed(ESC + b"Pnever ends") == b"" and screen.feed(b"more" * 1000) == b""
    assert screen.feed(ST + b"shown") == b"shown"


def test_sequences_are_bounded():
    assert passed(ESC + b"[" + b"1;" * 200 + b"m") == (b"", {"too long": 1})
    assert passed(ESC + b"[" + b"1;" * 100 + b"m")[0] == ESC + b"[" + b"1;" * 100 + b"m"
    # Cut off and too long: dropped to its final byte, in pieces too, and the text after it is shown.
    screen = rt.TerminalFilter()
    assert screen.feed(ESC + b"[" + b"9" * 5000) == b"" and screen.feed(b"9" * 5000) == b""
    assert screen.feed(b"nvisible") == b"visible" and screen.dropped["too long"] == 1
    # An OSC that may pass is kept to its end, up to the limit; a longer one is dropped whole.
    text = b"52;c;" + b"QUJD" * ((rt.FILTER_OSC_LIMIT // 4) - 10)
    assert passed(ESC + b"]" + text + b"\x07")[0] == ESC + b"]" + text + ST
    assert passed(ESC + b"]52;c;" + b"QUJD" * (rt.FILTER_OSC_LIMIT // 4 + 1) + b"\x07x") == (b"x", {"too long": 1})
    screen = rt.TerminalFilter()
    for _ in range(64):
        assert screen.feed(ESC + b"]52;c;" + b"A" * (1 << 16)) == b""  # (each begins a new string: the one before is
    assert len(screen.held or b"") <= rt.FILTER_OSC_LIMIT               # dropped) - memory stays bounded


def test_a_control_inside_a_sequence_does_not_smuggle_it_through():
    """A terminal executes a C0 control met inside a CSI sequence and goes on with the sequence; the filter drops the
    sequence and shows the rest as text - in both, no query was made."""
    assert passed(ESC + b"[6\x08n")[0] == b"\x08n"
    assert passed(ESC + b"[\n21t")[0] == b"\n21t"
    assert passed(ESC + b"[6" + ESC + b"[31mn")[0] == ESC + b"[31mn"
    assert passed(ESC + ESC + b"]52;c;?\x07" + ESC + b"\x07")[0] == b"\x07"
    assert passed(ESC + b"\x9b6n")[0] == b"6n"


STREAM = (b"start " + ESC + b"[1;31mred" + ESC + b"]52;c;?\x07" + ESC + b"[0m " + "世界".encode()
          + ESC + b"Ptmux;" + ESC + ESC + b"[21t" + ST + ESC + b"]52;c;aGk=" + ST + ESC + b"[?1049h" + ESC + b"(B"
          + b"\xc2\x9b21t" + ESC + b"]0;title\x07" + ESC + b"[6n" + ESC + b"]8;;http://x/" + ST + b"link"
          + ESC + b"]8;;" + ST + ESC + b"[21t" + ESC + b"[" + b"7" * 300 + b"m" + "\U0001f600 end".encode())


def test_the_stream_cut_at_any_byte_is_filtered_the_same():
    """Fragmented: every split of a stream into two pieces, and into single bytes, gives what the whole gives."""
    for strict in (False, True):
        whole, counted = passed(STREAM, strict)
        assert b"52;c;?" not in whole and b"[21t" not in whole and b"]0;" not in whole and b"]8;" not in whole
        assert whole.startswith(b"start " + ESC + b"[1;31mred" + ESC + b"[0m ") and whole.endswith(" end".encode())
        for cut in range(len(STREAM) + 1):
            screen = rt.TerminalFilter(strict=strict)
            assert screen.feed(STREAM[:cut]) + screen.feed(STREAM[cut:]) == whole, cut
            assert dict(screen.dropped) == counted, cut
        screen = rt.TerminalFilter(strict=strict)
        assert b"".join(screen.feed(bytes([byte])) for byte in STREAM) == whole


# An independent reading of what was written, as a terminal's parser would take it (ECMA-48's shapes): each piece is
# text, a CSI sequence, an OSC string or another escape sequence; anything else is a fault of the filter.
READ = re.compile(rb"(?P<csi>\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e])|(?P<osc>\x1b\][^\x07\x1b]*\x1b\\)"
                  rb"|(?P<esc>\x1b[\x20-\x2f]*[\x30-\x7e])|(?P<text>[^\x1b]+)")
ALLOWED_OSC = re.compile(rb"\x1b\](?:52;[a-z0-7]*;[A-Za-z0-9+/=]*|(?:4|10|11|12);[\x20-\x7e]+|(?:104|110|111|112)"
                         rb"(?:;[\x20-\x3e\x40-\x7e]*)?)\x1b\\")
NEVER_CSI = re.compile(rb"\x1b\[(?:[0-9;:]*[xiy]|(?:20|21|[1-9]|10)(?:;[0-9;]*)?t|\?[0-9;]*[imS]|=[0-9;]*c"
                       rb"|[0-9;:]*[\x20-\x2f]*[\"&*'#][vuy|R]|[0-9;]*\$[uw])")


def read_as_a_terminal(written, strict):
    position = 0
    while position < len(written):
        piece = READ.match(written, position)
        assert piece is not None, written[position:position + 40]
        kind, text = piece.lastgroup, piece.group()
        position = piece.end()
        if kind == "text":
            text.decode("utf-8")  # well-formed
            assert not re.search(rb"[\x00-\x06\x10-\x1a\x1c-\x1f]|\xc2[\x80-\x9f]", text), text
        elif kind == "osc":
            assert ALLOWED_OSC.fullmatch(text), text
            assert not (strict and (text.startswith(b"\x1b]52") or b"?" in text)), text
        elif kind == "csi":
            assert len(text) <= rt.FILTER_CSI_LIMIT and NEVER_CSI.match(text) is None, text
            if strict:  # no query of any kind
                assert not re.fullmatch(rb"\x1b\[[?>]?[0-9;]*\$?[cnpqu]", text) or text[2:3] in b"><=" and text[-1:] in b"um" \
                    or re.fullmatch(rb"\x1b\[[0-9;]*[upq]", text) or text.endswith((b" q", b"!p", b'"p', b'"q')), text
                assert not re.fullmatch(rb"\x1b\[(?:1[1345689])(?:;[0-9;]*)?t", text), text
        else:
            assert text[1:2] in b"78=>DEHMNOc" if len(text) == 2 else text[1:2] in b"()*+-./#" and len(text) == 3, text


PIECES = [piece for piece, _ in REFUSED] + DRAWING + QUERIES + [
    ESC, ESC + b"[", ESC + b"]", ESC + b"P", ESC + b"]52;c;", b"?", b"\x07", ST, b"\x18", b"\x1a", b"\x9c", b"\xc2",
    b"\x9b", b"52;c;?", b"aGk=", b";", b"21t", b"6n", b"\xe4\xb8", b"\x96", b"\xf0\x9f\x98", b"\x80", b" text ",
    ESC + b"]52;c;aGVsbG8=\x07", ESC + b"]11;rgb:1/2/3" + ST, ESC + b"[" + b"1" * 400,
]


def test_fuzz_nothing_but_the_allowed_shapes_is_ever_written():
    """Fuzz: thousands of streams made of hostile, harmless and broken pieces and random bytes, fed in random cuts:
    what is written always reads as text, an allowed CSI or OSC, or a harmless escape - never a string of another
    kind, a clipboard or title query, a C1 control or malformed UTF-8 - and is the same however the stream is cut."""
    chance = random.Random(20261118)
    for round_ in range(3000):
        strict = round_ % 2 == 1
        parts = []
        for _ in range(chance.randint(1, 12)):
            parts.append(chance.choice(PIECES) if chance.random() < 0.85
                         else bytes(chance.getrandbits(8) for _ in range(chance.randint(1, 24))))
        stream = b"".join(parts)
        whole = rt.TerminalFilter(strict=strict).feed(stream)
        screen, written, position = rt.TerminalFilter(strict=strict), [], 0
        while position < len(stream):
            step = chance.randint(1, 9)
            written.append(screen.feed(stream[position:position + step]))
            position += step
        assert b"".join(written) == whole, stream
        # What the stream left open (a string, a sequence) is closed by what a relay sends at its end.
        tail = rt.TerminalFilter(strict=strict)
        closed = tail.feed(stream) + tail.feed(b"\x18" + ST)
        read_as_a_terminal(closed, strict)
        # Filtering what was filtered changes nothing: all of it was allowed.
        assert rt.TerminalFilter(strict=strict).feed(closed) == closed, stream


# --- where the filter is used --------------------------------------------------------------------------------------

def test_the_filter_is_for_terminals_and_chosen_by_the_environment():
    master, slave = os.openpty()
    read_end, write_end = os.pipe()
    try:
        assert isinstance(rt.terminal_filter(slave, {}), rt.TerminalFilter)
        assert rt.terminal_filter(slave, {}).strict is False
        assert rt.terminal_filter(slave, {rt.TERMINAL_FILTER_ENV: "on"}).strict is False
        assert rt.terminal_filter(slave, {rt.TERMINAL_FILTER_ENV: " Strict "}).strict is True
        assert rt.terminal_filter(slave, {rt.TERMINAL_FILTER_ENV: "ofF"}) is None
        # A mistyped choice never opens more than was meant.
        assert rt.terminal_filter(slave, {rt.TERMINAL_FILTER_ENV: "of"}).strict is True
        # A file or a pipe gets the bytes as they are (sandbox-ai shell NAME -- cat FILE > COPY).
        assert rt.terminal_filter(write_end, {}) is None
        assert rt.terminal_filter(-1, {}) is None
    finally:
        for fd in (master, slave, read_end, write_end):
            os.close(fd)
    assert rt.TERMINAL_FILTERS == ("on", "strict", "off")


def test_the_note_names_clipboard_requests_only():
    screen = rt.TerminalFilter()
    screen.feed(ESC + b"]0;t\x07" + ESC + b"[21t")
    assert rt.filter_note([screen, None]) is None
    screen.feed(ESC + b"]52;c;?\x07" + ESC + b"]52;p;?\x07")
    assert rt.filter_note([None, screen]) == ("agent-sandbox: the sandbox asked your terminal for the clipboard's "
                                              "text 2 times (OSC 52); the request was not passed on")


def test_the_macs_clients_get_the_humans_choice(monkeypatch):
    """macOS: the attach and shell clients run in the container (podman exec), where they filter: the choice made on
    the Mac goes with them; anything that is no choice does not."""
    record = {"pod": "agent-codex-1", "run": "codex-1"}
    monkeypatch.setattr(launcher, "run_container", lambda record: "container")
    monkeypatch.delenv(rt.TERMINAL_FILTER_ENV, raising=False)
    assert "--env" not in launcher.machine_exec(record, ["--internal-attach-client", rt.RELAY_ATTACH], tty=True)
    monkeypatch.setenv(rt.TERMINAL_FILTER_ENV, "strict")
    command = launcher.machine_exec(record, ["--internal-attach-client", rt.RELAY_ATTACH], tty=True)
    assert command[command.index("--env") + 1] == f"{rt.TERMINAL_FILTER_ENV}=strict"
    assert command.index("--env") < command.index("container")
    monkeypatch.setenv(rt.TERMINAL_FILTER_ENV, "x; rm -rf /")
    assert "--env" not in launcher.machine_exec(record, ["--internal-attach-client", rt.RELAY_ATTACH], tty=True)


# --- through the real entry point, on a real pty -------------------------------------------------------------------

HOSTILE = (b"shown " + ESC + b"[32mgreen" + ESC + b"[0m" + ESC + b"]52;c;?\x07" + ESC + b"[21t" + ESC + b"]0;owned\x07"
           + ESC + b"Ptmux;" + ESC + ESC + b"]52;c;?\x07" + ST + b"\xc2\x9b21t" + ESC + b"]8;;http://evil/" + ST
           + b" done")


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class Session:
    """A running session NAME of the Host with sealed relays (its record says so): its supervisor a real process, its
    shell relay runtime.py's own in this process, its attach relay one that runs COMMAND instead of tmux."""

    def __init__(self, host, name, attach_command):
        self.dir = host.home / ".local/state" / "sandbox-ai" / "sessions" / name
        self.relay = self.dir / "tmux" / "relay"
        for path in (self.dir / "home", self.relay):
            path.mkdir(parents=True, exist_ok=True)
        self.work = host.directory / f"work-{name}"
        self.work.mkdir(exist_ok=True)
        self.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "agent_sandbox.py",
                                         "--tmux-socket-dir", str(self.dir / "tmux")])
        (self.dir / "session.json").write_text(json.dumps({
            "agent": "claude", "options": ["--backend", "podman"], "agent_args": [], "backend": "podman",
            "workspace": str(host.workspace), "cwd": str(host.workspace), "created": "2026-11-01T10:00:00Z",
            "pod": f"agent-session-{name}", "name": name, "pid": self.process.pid, "mode": "tmux",
            "started": "2026-11-02T09:30:00Z", "shell_relay": 2}))
        threading.Thread(target=rt.shell_relay, args=(str(self.relay / "shell.sock"), str(self.work)),
                         daemon=True).start()
        threading.Thread(target=rt.attach_relay, args=(str(self.relay / "attach.sock"), attach_command),
                         daemon=True).start()
        assert wait_for(lambda: (self.relay / "shell.sock").exists() and (self.relay / "attach.sock").exists(), 5)


@pytest.fixture
def host(tmp_path):
    made = live_fakes.Host(tmp_path / "host")
    sessions = []
    made.session = lambda name, command: sessions.append(Session(made, name, command)) or sessions[-1]
    yield made
    for session in sessions:
        session.process.kill()
        session.process.wait()


def on_a_terminal(command, host, env=None, seconds=60):
    """COMMAND with a pty as its terminal (input, output and error output): (exit status, everything the terminal
    was sent)."""
    master, slave = os.openpty()
    process = subprocess.Popen(command, cwd=host.workspace, env={**host.env, **(env or {})}, stdin=slave,
                               stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    seen, deadline = b"", time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break  # (EIO: the command closed the terminal)
                if not chunk:
                    break
                seen += chunk
            elif process.poll() is not None:
                break
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        os.close(master)
    return process.returncode, seen


def test_journey_a_shells_output_on_a_terminal_is_filtered_and_into_a_file_it_is_not(host):
    session = host.session("web", ["true"])
    (session.work / "hostile.bin").write_bytes(HOSTILE)
    status, seen = on_a_terminal(host.command("shell", "web", "--", "cat", "hostile.bin"), host)
    assert status == 0
    assert seen.startswith(b"shown " + ESC + b"[32mgreen" + ESC + b"[0m") and b" done" in seen
    for forbidden in (b"]52", b"[21t", b"]0;", b"owned", b"\x1bP", b"\xc2\x9b", b"]8;", b"evil"):
        assert forbidden not in seen, forbidden
    assert b"asked your terminal for the clipboard's text 2 times (OSC 52)" in seen  # (one inside the passthrough)
    # The same command into a pipe: the file's bytes, unchanged (the tmux passthrough's query is plain data there).
    done = subprocess.run(host.command("shell", "web", "--", "cat", "hostile.bin"), cwd=host.workspace, env=host.env,
                          capture_output=True, timeout=60)
    assert done.returncode == 0 and done.stdout == HOSTILE and done.stderr == b""
    # Strict: no clipboard text either; off: the bytes as they come, for a terminal you trust with them.
    (session.work / "copy.bin").write_bytes(b"a" + ESC + b"]52;c;aGk=\x07" + ESC + b"[6n" + b"b")
    status, seen = on_a_terminal(host.command("shell", "web", "--", "cat", "copy.bin"), host)
    assert (status, seen) == (0, b"a" + ESC + b"]52;c;aGk=" + ST + ESC + b"[6n" + b"b")
    status, seen = on_a_terminal(host.command("shell", "web", "--", "cat", "copy.bin"), host,
                                 env={rt.TERMINAL_FILTER_ENV: "strict"})
    assert (status, seen) == (0, b"ab")
    status, seen = on_a_terminal(host.command("shell", "web", "--", "cat", "hostile.bin"), host,
                                 env={rt.TERMINAL_FILTER_ENV: "off"})
    assert status == 0 and HOSTILE in seen


def test_journey_an_attached_terminal_gets_the_filtered_screen(host):
    """`sandbox-ai attach`: the relay's client writes what a process of the sandbox printed - here in place of tmux -
    and the terminal gets the text and colours, not the clipboard's query nor the title."""
    hostile = host.directory / "hostile.bin"
    hostile.write_bytes(HOSTILE)
    host.session("web", ["sh", "-c", f"stty raw -echo; cat {hostile}; sleep 0.3"])
    status, seen = on_a_terminal(host.command("attach", "web"), host)
    assert b"shown " in seen and b"green" in seen and b" done" in seen, (status, seen)
    for forbidden in (b"]52", b"[21t", b"]0;", b"owned", b"\x1bP", b"\xc2\x9b", b"]8;", b"evil"):
        assert forbidden not in seen, forbidden
    assert b"asked your terminal for the clipboard's text" in seen
