"""Regression (2026-11-22, user, live): "hier hat die Anmeldung funktioniert, solche Fenster dann nach 30s selber
schließen, müssen ja nicht offen bleiben". A `sandbox-ai login AGENT` the app runs in one of its terminal tabs and
that ends well (exit status 0) closes its tab after HELPER_CLOSE_SECONDS, counted down above the terminal with "Keep
it open"; a failed login stays; a shell or another command's tab never closes by itself."""
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sandbox_ai_app import backend, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

from test_ui import env, row_of, window  # noqa: E402,F401 - the page's fixtures

HERE = Path(__file__).resolve().parent


def tabs_in_the_app(window, monkeypatch, tmp_path):  # noqa: F811
    """Real (offscreen) terminal tabs in WINDOW, and a stand-in sandbox-ai whose `login codex` fails (exit status 1)
    and every other login ends well (exit status 0)."""
    from sandbox_ai_app import terminal
    if terminal.MISSING:
        pytest.skip(f"QtWebEngine is not installed: {terminal.MISSING}")
    web = tmp_path / "web"
    (web / "vendor").mkdir(parents=True)
    for name in ("terminal.html", "terminal.js"):
        shutil.copy(HERE.parent / "sandbox_ai_app" / "web" / name, web)
    for name in ("xterm.js", "addon-fit.js", "xterm.css"):
        shutil.copy(HERE / "fake_xterm" / name, web / "vendor")
    monkeypatch.setattr(terminal, "available", lambda w=None: (True, ""))
    window.terminals.web = web
    script = tmp_path / "bin" / "sandbox-ai"
    script.parent.mkdir()
    script.write_text('#!/bin/sh\ncase "$*" in *codex*) echo "login failed"; exit 1;; '
                      '*) echo "logged in"; exit 0;; esac\n')
    script.chmod(0o755)
    return script


def login_tab(window, wait, agent, logout=False):  # noqa: F811
    """Log in… (or Log out) of AGENT on the Credentials page: the tab it opened, once its command ended."""
    page, before = window.credentials, set(window.terminals.views())
    page.logins.selectRow(row_of(page.logins, agent))
    page.login(logout)
    assert wait(lambda: set(window.terminals.views()) - before, 30), "the login's tab never came"
    view = (set(window.terminals.views()) - before).pop()
    assert wait(lambda: not view.running, 15)
    return view


def stays(window, wait, view, seconds):  # noqa: F811
    """VIEW's tab is still there SECONDS later."""
    wait(lambda: window.terminals.tabs.indexOf(view) < 0, seconds)
    return window.terminals.tabs.indexOf(view) >= 0


def test_a_login_tab_that_ended_well_closes_after_a_countdown_and_a_failed_one_stays(window, env, monkeypatch,  # noqa: F811
                                                                                   wait, tmp_path):
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "HELPER_CLOSE_SECONDS", 2, raising=False)
    script = tabs_in_the_app(window, monkeypatch, tmp_path)
    page = window.credentials
    page.refresh()  # (in a thread)
    assert wait(lambda: not widgets.busy(page), 60)
    monkeypatch.setattr(window.sai, "command", lambda *args: [str(script), *args])
    # A login that ended well: the countdown above its terminal, then the tab goes by itself.
    ok = login_tab(window, wait, "claude")
    assert wait(lambda: "closes in" in ok.bar.text(), 5), ok.bar.text()
    assert "2 s" in ok.bar.text() and "Keep it open" in ok.bar.choices(), (ok.bar.text(), ok.bar.choices())
    assert wait(lambda: window.terminals.tabs.indexOf(ok) < 0, 10), "a login that ended well closes its tab"
    # Its logout too (a one-shot helper as well).
    out = login_tab(window, wait, "claude", logout=True)
    assert wait(lambda: "closes in" in out.bar.text(), 5), out.bar.text()
    assert wait(lambda: window.terminals.tabs.indexOf(out) < 0, 10), "a logout that ended well closes its tab"
    # A failed login stays, marked, without a countdown.
    bad = login_tab(window, wait, "codex")
    assert stays(window, wait, bad, 3.5), "a failed login stays open"
    assert "closes in" not in bad.bar.text()
    assert "failed" in window.terminals.tabs.tabText(window.terminals.tabs.indexOf(bad))
    # Keep it open: the tab stays.
    kept = login_tab(window, wait, "claude")
    assert wait(lambda: "Keep it open" in kept.bar.choices(), 5)
    assert kept.bar.choose("Keep it open")
    assert "closes in" not in kept.bar.text()
    assert stays(window, wait, kept, 3.5), "Keep it open keeps the tab"
    # Esc in the bar (or its ×) keeps it open as well.
    escaped = login_tab(window, wait, "claude")
    assert wait(lambda: "closes in" in escaped.bar.text(), 5)
    escaped.bar.dismiss()
    assert stays(window, wait, escaped, 3.5), "the countdown dismissed keeps the tab"
    # Close now: at once.
    now = login_tab(window, wait, "claude")
    assert wait(lambda: "Close now" in now.bar.choices(), 5)
    assert now.bar.choose("Close now")
    assert window.terminals.tabs.indexOf(now) < 0, "Close now closes the tab at once"
    window.terminals.close_all()


def test_a_shell_or_another_command_that_ended_well_never_closes_by_itself(window, env, monkeypatch,  # noqa: F811
                                                                           wait, tmp_path):
    from sandbox_ai_app import terminal
    monkeypatch.setattr(terminal, "HELPER_CLOSE_SECONDS", 1, raising=False)
    tabs_in_the_app(window, monkeypatch, tmp_path)
    before = set(window.terminals.views())
    assert window.in_terminal(["sh", "-c", "echo done; exit 0"], "a command")
    assert wait(lambda: set(window.terminals.views()) - before, 30)
    view = (set(window.terminals.views()) - before).pop()
    assert wait(lambda: not view.running, 15)
    assert stays(window, wait, view, 2.5), "only the app's one-shot helpers close by themselves"
    assert "closes in" not in view.bar.text()
    assert not terminal.closes_when_done(["bash", "-l"])
    assert not terminal.closes_when_done(["/usr/bin/sandbox-ai", "claude", "--workspace", "/w"])
    assert not terminal.closes_when_done(["/usr/bin/sandbox-ai", "session", "attach", "work"])
    assert not terminal.closes_when_done(["/usr/bin/sandbox-ai", "login", "--status"])
    assert terminal.closes_when_done(["/usr/bin/sandbox-ai", "login", "codex"])
    assert terminal.closes_when_done(["/usr/bin/sandbox-ai", "login", "codex", "--logout"])
    window.terminals.close_all()
