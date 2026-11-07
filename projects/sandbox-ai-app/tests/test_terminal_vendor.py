"""The Terminals page fetches a missing xterm.js on request and works without a restart."""
import sys

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import terminal  # noqa: E402


def test_download_button_fetches_xterm_and_the_page_follows(qapp, tmp_path, monkeypatch, wait):
    web = tmp_path / "web"
    web.mkdir()
    script = tmp_path / "vendor_xterm.py"
    # A stand-in for scripts/vendor_xterm.py: writes the three files into --into.
    script.write_text("import sys, pathlib\n"
                      "target = pathlib.Path(sys.argv[sys.argv.index('--into') + 1]); target.mkdir(parents=True)\n"
                      "[(target / n).write_text('x') for n in ('xterm.js', 'xterm.css', 'addon-fit.js')]\n")
    monkeypatch.setattr(terminal, "MISSING", None)
    monkeypatch.setattr(terminal, "VENDOR_SCRIPT", script)
    page = terminal.TerminalsPage(web)
    assert not page.fetch.isHidden() and "not vendored" in page.hint.text()
    page.vendor()
    assert wait(lambda: page.command is not None and not page.command.running, 30)
    assert terminal.available(web) == (True, "") and page.fetch.isHidden()
    assert "opens here as a tab" in page.hint.text()


def test_a_failed_download_says_why_and_can_be_retried(qapp, tmp_path, monkeypatch, wait):
    script = tmp_path / "vendor_xterm.py"
    script.write_text("import sys\nprint('vendor_xterm: registry.npmjs.org: name not resolved', file=sys.stderr)\n"
                      "sys.exit(1)\n")
    monkeypatch.setattr(terminal, "MISSING", None)
    monkeypatch.setattr(terminal, "VENDOR_SCRIPT", script)
    page = terminal.TerminalsPage(tmp_path / "web")
    page.vendor()
    assert wait(lambda: page.command is not None and not page.command.running, 30)
    assert "name not resolved" in page.hint.text() and page.fetch.isEnabled() and not page.fetch.isHidden()


def test_without_qtwebengine_the_hint_names_the_fix(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "MISSING", "No module named 'PySide6.QtWebEngineWidgets'")
    page = terminal.TerminalsPage(tmp_path / "web")
    assert "sandbox-ai-update --force" in page.hint.text() and page.fetch.isHidden()
