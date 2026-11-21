"""sandbox/textout.py: aligned tables, wrapping, colour only where it belongs."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("textout_test", ROOT / "sandbox" / "textout.py")
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def test_columns_line_up_and_the_last_wraps(monkeypatch):
    monkeypatch.setattr(t, "width", lambda: 60)
    lines = []
    t.table(["A", "LONGER", "TEXT"], [["x", "y", "one two three four five six seven eight nine ten eleven"],
                                      ["xxxx", "", "short"]], lines.append, indent=0, colour=False)
    assert lines[0].index("LONGER") == lines[1].index("y")
    assert lines[0].index("TEXT") == lines[1].index("one")
    assert len(lines) == 4 and lines[2].startswith(" " * lines[0].index("TEXT")) and lines[2].strip()
    assert all(len(line) <= 60 for line in lines)


def test_empty_table_says_so():
    lines = []
    t.table(["A"], [], lines.append, indent=0, empty="nothing here", colour=False)
    assert lines == ["nothing here"]


def test_colour_rules(monkeypatch):
    class Tty:
        def isatty(self):
            return True
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("NO_COLOR", "1")
    assert not t.colour_enabled(Tty())
    monkeypatch.delenv("NO_COLOR")
    assert t.colour_enabled(Tty()) and not t.colour_enabled(object())
    monkeypatch.setenv("TERM", "dumb")
    assert not t.colour_enabled(Tty())
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert t.colour_enabled(object())
    coloured = t.style("ok", "ok", True)
    assert coloured != "ok" and t.visible_len(coloured) == 2 and t.style("ok", "ok", False) == "ok"


def test_the_last_column_cut_to_the_width_with_an_ellipsis(monkeypatch):
    """TRUNCATE: the last column cut instead of wrapped - "left" keeps a path's end; a coloured cell is never cut."""
    monkeypatch.setattr(t, "width", lambda: 40)

    class Utf8:
        encoding = "utf-8"
    path = "/srv/projects/a-very-long-project/with/a/deep/path"
    for how, check in (("left", lambda cell: cell.startswith("\u2026") and cell.endswith("deep/path")),
                       ("right", lambda cell: cell.endswith("\u2026") and cell.startswith("/srv/projects"))):
        lines = []
        t.table(["NAME", "WORKSPACE"], [["web", path]], lines.append, indent=0, colour=False, truncate=how,
                stream=Utf8())
        assert len(lines) == 2 and all(len(line) <= 40 for line in lines), lines
        assert check(lines[1].split("  ")[-1].strip()), lines[1]
    lines = []
    t.table(["NAME", "WORKSPACE"], [["web", path]], lines.append, indent=0, colour=False)
    assert len(lines) == 2 and lines[1].endswith(path), "a path without spaces is not wrapped, nor cut"
    assert t.shortened("abcdef", 4, stream=type("Ascii", (), {"encoding": "ascii"})()) == "a..."
    assert t.shortened("abc", 4) == "abc"
    # a path with a note after two spaces: the path is cut, the note kept (while there is room for both)
    utf8 = type("Utf8", (), {"encoding": "utf-8"})()
    assert t.shortened("/srv/state/mcp/fetch/work  (also serves web)", 30, True, utf8) == "\u2026fetch/work  (also serves web)"
    assert t.shortened("/srv/x  (a note far too long for the room)", 20, True, utf8) == "\u2026 long for the room)"


def test_home_short_ago_and_utc_seconds():
    assert t.home_short("/home/me/src/app", "/home/me") == "~/src/app" and t.home_short("/home/me", "/home/me/") == "~"
    assert t.home_short("/home/meadow/x", "/home/me") == "/home/meadow/x" and t.home_short("/srv/x", "") == "/srv/x"
    now = t.utc_seconds("2026-11-16T12:00:00Z")
    assert now == 1794830400
    assert t.utc_seconds("2000-02-29T23:59:59.5+00:00") == 951868799 and t.utc_seconds("1970-01-01 00:00:00") == 0
    for bad in ("now", "", None, 5, "2026-13-01T00:00:00Z", "2026-11-16"):
        assert t.utc_seconds(bad) is None and t.ago(bad, now) == "-"
    said = {seconds: t.ago(f"2026-11-16T{12 - seconds // 3600:02d}:00:00Z" if seconds < 43200 else
                            "2026-11-16T12:00:00Z", now) for seconds in (0, 3600, 7200)}
    assert said == {0: "just now", 3600: "an hour ago", 7200: "2 hours ago"}
    for seconds, text in ((30, "just now"), (60, "a minute ago"), (59 * 60, "59 minutes ago"),
                          (47 * 3600, "47 hours ago"), (2 * 86400, "2 days ago"), (13 * 86400, "13 days ago"),
                          (14 * 86400, "2 weeks ago"), (70 * 86400, "2 months ago"), (800 * 86400, "2 years ago")):
        assert t.ago("2026-11-16T12:00:00Z", now + seconds) == text, seconds
    assert t.ago("2026-11-16T12:00:00Z", now - 30) == "just now"  # (a clock behind: not "in the future")


def test_grid_aligns_sizes_right_and_adds_a_total(monkeypatch):
    monkeypatch.setattr(t, "width", lambda: 70)
    lines = []
    t.grid(["CATEGORY", "SIZE", "HOW"], [["Starts", "26 GiB", "sandbox-ai gc --caches"],
                                         ["Logs", "512 MiB",
                                          "one two three four five six seven eight nine ten eleven 12.0 KiB more"]],
           right={1}, out=lines.append, indent=0, colour=False, footer=["Total", "27 GiB", ""])
    monkeypatch.setattr(t, "width", lambda: 70)
    assert lines[0].split() == ["CATEGORY", "SIZE", "HOW"]
    # SIZE right-aligned: every size ends in the same column, the header's too
    ends = {line.index("GiB") + 3 for line in lines if "GiB" in line} | {lines[0].index("SIZE") + 4}
    assert len(ends) == 1 and lines[2].index("MiB") + 3 in ends
    assert lines[3].strip() and lines[3].startswith(" " * lines[0].index("HOW"))  # the last column wrapped
    assert set(lines[-2]) == {"-"} and lines[-1].startswith("Total")
    assert any("12.0 KiB" in line for line in lines)  # a size is never split
    assert all(len(line) <= 70 for line in lines)


def test_grid_empty_colour_and_coloured_cells():
    lines = []
    t.grid(["A", "B"], [], out=lines.append, indent=0, empty="nothing", colour=False)
    assert lines == ["nothing"]
    lines = []
    t.grid(["A", "B"], [[t.style("x", "bad", True), "1"]], right={1}, out=lines.append, indent=0, colour=True)
    assert "\033[1m" in lines[0] and t.visible_len(lines[1]) == len("A  B")
    plain = []
    t.grid(["A", "B"], [["x", "1"]], right={1}, out=plain.append, indent=0, colour=False)
    assert all("\033" not in line for line in plain)
def plain(text):
    """TEXT without its colour codes."""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def test_semantic_styles_exist_and_say_nothing_without_colour():
    for kind in ("heading", "command", "option", "metavar", "error", "warning", "note", "number"):
        assert kind in t.CODES, kind
        assert t.style("x", kind, False) == "x" and t.style("x", kind, True) != "x"


def test_messages_errors_red_warnings_yellow_notes_marked():
    red, yellow = "\033[" + t.CODES["error"] + "m", "\033[" + t.CODES["warning"] + "m"
    error = t.message("sandbox-ai net: no run named 'web'", True)
    assert red in error and plain(error) == "sandbox-ai net: no run named 'web'"
    assert red in t.message("sandbox-ai net allow: error: argument -A: expected one argument", True)
    assert red in t.message("agent-sandbox: the image could not be found", True)
    warning = t.message("agent-sandbox: warning: /srv is shared read-write", True)
    assert yellow in warning and red not in warning and plain(warning) == "agent-sandbox: warning: /srv is shared read-write"
    note = t.message("sandbox-ai build: note: built as root", True)
    assert red not in note and yellow not in note and note != plain(note)
    progress = t.message("sandbox-ai build: all (2/5): codex", True)
    assert red not in progress and plain(progress) == "sandbox-ai build: all (2/5): codex"
    # not a message of sandbox-ai (a tool's own output), several lines, no colour: as it is
    assert t.message("podman: error: x", True) == "podman: error: x"
    assert t.message("hello\n", True) == "hello\n"
    two = t.message("sandbox-ai x: failed\nsandbox-ai x: warning: y\n", True)
    assert plain(two) == "sandbox-ai x: failed\nsandbox-ai x: warning: y\n" and red in two and yellow in two
    assert t.message("sandbox-ai net: no run named 'web'", False) == "sandbox-ai net: no run named 'web'"


HELP_PAGE = """Usage:  sandbox-ai net allow [OPTIONS] [RUN]

Open more for a running sandbox, at once.

Arguments:
  RUN                        The run (sandbox-ai net list)

Options:
  -A, --allow-host HOST      Pin HOST in the sandbox's /etc/hosts
                             and allow TCP/443 to it
  -h, --help                 Show this help

Commands:
  list, ls                   The runs

Examples:
  sandbox-ai net allow web -A pypi.org      pypi.org for the start web

Run 'sandbox-ai net --help' for the other net commands."""


def test_a_help_page_coloured_on_a_terminal_and_the_same_text():
    coloured = t.help_page(HELP_PAGE, True)
    assert plain(coloured) == HELP_PAGE, "only colour is added: the layout stays"
    assert t.help_page(HELP_PAGE, False) == HELP_PAGE
    lines = dict(zip(HELP_PAGE.splitlines(), coloured.splitlines()))

    def styled(kind, text):
        return t.style(text, kind, True)
    assert lines["Options:"] == styled("heading", "Options:") and lines["Examples:"] == styled("heading", "Examples:")
    assert styled("heading", "Usage:") in lines["Usage:  sandbox-ai net allow [OPTIONS] [RUN]"]
    assert styled("command", "sandbox-ai net allow") in lines["Usage:  sandbox-ai net allow [OPTIONS] [RUN]"]
    option = lines["  -A, --allow-host HOST      Pin HOST in the sandbox's /etc/hosts"]
    assert styled("option", "-A") in option and styled("option", "--allow-host") in option
    assert styled("metavar", "HOST") in option and option.endswith("Pin HOST in the sandbox's /etc/hosts")
    assert styled("metavar", "RUN") in lines["  RUN                        The run (sandbox-ai net list)"]
    assert styled("command", "list") in lines["  list, ls                   The runs"]
    assert styled("command", "ls") in lines["  list, ls                   The runs"]
    example = lines["  sandbox-ai net allow web -A pypi.org      pypi.org for the start web"]
    assert styled("command", "sandbox-ai net allow") in example and styled("option", "-A") in example
    assert styled("command", "sandbox-ai net --help") in lines["Run 'sandbox-ai net --help' for the other net commands."]


def test_a_message_said_as_an_error():
    red = "\033[" + t.CODES["error"] + "m"
    assert red not in t.message("agent-sandbox: the image x is gone", True)
    assert red in t.message("agent-sandbox: the image x is gone", True, error=True)
    assert red not in t.message("agent-sandbox: warning: x", True, error=True)
    assert t.message("agent-sandbox: the image x is gone", False, error=True) == "agent-sandbox: the image x is gone"
