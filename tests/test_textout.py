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
