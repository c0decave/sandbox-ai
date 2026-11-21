"""The Approvals page as one table (user 2026-11-17: "blockiert/past nicht in eigener Tabelle darunter, sondern Tab
in der oberen Tabelle: allowed/denied/timeout"; "wenn man schon auf freigeben und blockieren geklickt hat, nicht
nochmal in einem weiteren Fenster nachfragen, sondern machen"; an answer's error "proxy.example.org has no address
..." came as a box with OK): tabs Open | Allowed | Refused | Timed out | All with their counts, a click answers at once
(no dialog) with Undo for a few seconds, a question that timed out is still answered, an answered one can be changed,
several rows at once, and what failed is a short notice in the error colour (and the note line), never a box."""
import json
import os
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from sandbox_ai_app import backend, pages, short_notices, widgets  # noqa: E402

URL = "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(short_notices, "SHOWN", [])
    return tmp_path


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fake_run(env, name="opencode-1"):
    directory = env / "runtime" / "sandbox-ai" / "runs" / name
    (directory / "approvals").mkdir(parents=True)
    (env / "project").mkdir(exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run": name, "agent": "opencode", "backend": "podman", "workspace": str(env / "project"), "pid": os.getpid(),
        "session": None, "session_dir": str(env / "s" / name), "started": "2026-11-17T04:00:00Z", "proxy": True,
        "ask": "on", "ask_timeout": 60}))
    return directory / "approvals"


def question(directory, ident, state=None, done=None, **fields):
    (directory / f"{ident}.json").write_text(json.dumps({"id": ident, "time": now(), "timeout": 60, **fields}))
    if state:
        (directory / f"{ident}.state").write_text(json.dumps({"state": state, "time": now()}))
    if done:
        (directory / f"{ident}.done").write_text(json.dumps(done))


CONNECT = {"kind": "connect", "destination": "192.0.2.7:8443", "address": "192.0.2.7", "port": 8443}
HTTP = {"kind": "http", "method": "POST", "url": URL, "rule": "writes", "ruleset": "rules.json"}


@pytest.fixture
def page(qapp, env):
    sai = backend.SandboxAI()
    made = pages.ApprovalsPage(sai, interval=0, remote_interval=0)
    yield made
    made.timer.stop()
    made.blocked.timer.stop()
    made.deleteLater()


def tab_texts(page):
    return [page.state_tabs.tabText(i) for i in range(page.state_tabs.count())]


def choose(page, key):
    page.state_tabs.setCurrentIndex([page.state_tabs.tabData(i) for i in range(page.state_tabs.count())].index(key))


def shown_ids(page):
    return [pages.row_key(page.open, row)[2] for row in range(page.open.rowCount())]


def fill(env):
    directory = fake_run(env)
    question(directory, "1762855200-7-1", **CONNECT)
    question(directory, "1762855200-7-2", state="timeout", **HTTP)
    question(directory, "1762855200-7-3", done={"decision": "allow", "scope": "run", "reason": "answered"},
             **{**CONNECT, "destination": "192.0.2.8:443", "address": "192.0.2.8", "port": 443})
    question(directory, "1762855200-7-4", done={"decision": "deny", "scope": "run", "reason": "answered"},
             **{**CONNECT, "destination": "192.0.2.9:443", "address": "192.0.2.9", "port": 443})
    return directory


def test_one_table_with_tabs_and_their_counts(env, qapp, page):
    fill(env)
    page.poll()
    assert tab_texts(page) == ["Open (1)", "Allowed (1)", "Refused (1)", "Timed out (1)", "All (4)"]
    assert shown_ids(page) == ["1762855200-7-1"]
    choose(page, "timeout")
    assert shown_ids(page) == ["1762855200-7-2"]
    assert page.open.item(0, 4).text() == "timed out - still answerable"
    choose(page, "refused")
    assert shown_ids(page) == ["1762855200-7-4"] and page.open.item(0, 4).text() == "refused (for this run)"
    choose(page, "all")
    assert set(shown_ids(page)) == {f"1762855200-7-{n}" for n in range(1, 5)}
    # No section of its own for Blocked or History any more; the waiting count is the open ones only.
    titles = [page.sections.tabText(i) for i in range(page.sections.count())]
    assert not any(t.startswith(("Blocked", "History")) for t in titles), titles
    counts = []
    page.pending_changed.connect(lambda count, new: counts.append(count))
    page.show_all()
    assert counts[-1] == 1


def answers_to(page, monkeypatch, status=0, out="opencode-1: allow (once): done", err=""):
    calls = []

    def answer(ident, decision, scope="once"):
        calls.append((ident, decision, scope))
        return status, out, err
    monkeypatch.setattr(page.sai, "answer", answer)
    return calls


def test_allow_answers_at_once_without_a_dialog_and_undo_takes_it_back(env, qapp, page, wait, monkeypatch):
    fill(env)
    page.poll()
    calls = answers_to(page, monkeypatch)
    asked = []
    monkeypatch.setattr(pages, "ask_first", lambda *a, **k: asked.append(a) or False)
    monkeypatch.setattr(pages, "confirm", lambda *a, **k: asked.append(a) or False)
    page.open.selectRow(0)
    assert page.allow_always_button.text() == "Always, also for later sessions", "no … : nothing is asked first"
    page.allow_always_button.click()
    assert wait(lambda: calls and not page.answers)
    assert calls == [("1762855200-7-1", "allow", "always")] and asked == [] and widgets.SHOWN == []
    assert page.undo_button.isVisibleTo(page) and page.undo_timer.isActive()
    # Undo: the run forgets the answer (asked again), at once too.
    page.undo_button.click()
    assert wait(lambda: len(calls) == 2 and not page.answers)
    assert calls[-1] == ("1762855200-7-1", "ask-again", None)
    assert not page.undo_button.isVisibleTo(page)
    assert "“always” stays remembered for the project" in page.message.text()


def test_block_always_acts_at_once_too(env, qapp, page, wait, monkeypatch):
    fill(env)
    page.poll()
    calls = answers_to(page, monkeypatch)
    monkeypatch.setattr(pages, "ask_first", lambda *a, **k: pytest.fail("asked first"))
    page.open.selectRow(0)
    page.block_button.click()
    assert wait(lambda: calls and not page.answers)
    assert calls == [("1762855200-7-1", "deny", "always")] and widgets.SHOWN == []


def test_a_question_that_timed_out_is_still_answered(env, qapp, page, wait, monkeypatch):
    fill(env)
    page.poll()
    calls = answers_to(page, monkeypatch)
    choose(page, "timeout")
    page.open.selectRow(0)
    assert page.allow_button.isEnabled() and not page.ask_again_button.isEnabled()
    page.allow_button.click()
    assert wait(lambda: calls and not page.answers)
    assert calls == [("1762855200-7-2", "allow", "once")]


def test_an_answered_question_is_changed_from_its_row(env, qapp, page, wait, monkeypatch):
    fill(env)
    page.poll()
    calls = answers_to(page, monkeypatch)
    choose(page, "refused")
    page.open.selectRow(0)
    assert page.allow_run_button.isEnabled() and page.ask_again_button.isEnabled()
    assert not page.allow_button.isEnabled(), "answered: no Allow once (the run decided it already)"
    assert not page.block_button.isEnabled() and not page.deny_once_button.isEnabled()
    page.allow_run_button.click()
    assert wait(lambda: calls and not page.answers)
    assert calls == [("1762855200-7-4", "allow", "run")]
    choose(page, "allowed")
    page.open.selectRow(0)
    page.ask_again_button.click()
    assert wait(lambda: len(calls) == 2 and not page.answers)
    assert calls[-1] == ("1762855200-7-3", "ask-again", None)
    # Undo of an ask-again: the answer it had again (allowed, for the run).
    page.undo_button.click()
    assert wait(lambda: len(calls) == 3 and not page.answers)
    assert calls[-1] == ("1762855200-7-3", "allow", "run")


def test_several_rows_are_answered_together(env, qapp, page, wait, monkeypatch):
    directory = fake_run(env)
    question(directory, "1762855200-7-1", **CONNECT)
    question(directory, "1762855200-7-5", **{**CONNECT, "destination": "192.0.2.10:443", "address": "192.0.2.10",
                                              "port": 443})
    page.poll()
    calls = answers_to(page, monkeypatch)
    page.open.selectAll()
    page.deny_button.click()
    assert wait(lambda: len(calls) == 2 and not page.answers)
    assert sorted(calls) == [("1762855200-7-1", "deny", "run"), ("1762855200-7-5", "deny", "run")]


def test_an_answer_that_fails_is_a_short_notice_never_a_box(env, qapp, page, wait, monkeypatch):
    """User 2026-11-17: the error of an answer ("proxy.example.org has no address ...") came in a box with OK."""
    directory = fake_run(env)
    question(directory, "1762855200-7-6", kind="dns", name="proxy.example.org")
    page.poll()
    why = ("sandbox-ai approvals: proxy.example.org has no address (on this host: No address associated with "
           "hostname): there is nothing to allow")
    answers_to(page, monkeypatch, status=1, out="", err=why)
    page.open.selectRow(0)
    page.allow_run_button.click()
    assert wait(lambda: short_notices.SHOWN and not page.answers)
    assert widgets.SHOWN == [], "no dialog"
    assert short_notices.SHOWN[-1][0] == "Not answered" and "has no address" in short_notices.SHOWN[-1][1]
    assert "has no address" in page.message.text(), "kept in the note line, to read again"
    assert not page.undo_button.isVisibleTo(page)
    # It goes by itself: at least a second and a half, longer for a longer text.
    assert short_notices.lifetime("x", error=True) == short_notices.ERROR_MS
    assert short_notices.lifetime(why, error=True) > short_notices.lifetime("x", error=True)


def test_no_confirmation_is_kept_for_answers():
    assert "approval-allow-always" not in backend.NEEDS_CONFIRM
    assert "approval-deny-always" not in backend.NEEDS_CONFIRM
    assert "approval-forget" in backend.NEEDS_CONFIRM


def test_the_blocked_words_and_the_quit_question_say_an_unanswered_question_stays_open(qapp):
    """Review 24h B: since the rework nothing is refused for 2 minutes after a question nobody answered - the try
    fails, the question stays open and its answer counts for the next try (sandbox-ai's own blocked_why says so).
    The app's Blocked list still said "refused for 2 minutes, then asked again", in English and German, and the
    quit question "each is refused after 60 seconds"."""
    from sandbox_ai_app import approvals_sections, i18n
    import inspect
    from sandbox_ai_app import ui
    for language in ("en", "de"):
        i18n.set_language(language)
        try:
            for entry in ({"kind": "unanswered"}, {"kind": "unanswered", "rule": "r1", "ruleset": "web"}):
                text = approvals_sections.why_text(entry)
                assert "2 minutes" not in text and "2 Minuten" not in text, text
                assert ("stays open" if language == "en" else "bleibt offen") in text, text
        finally:
            i18n.set_language("en")
    source = inspect.getsource(ui.MainWindow.questions_refused_without_window)
    assert "is refused after {seconds} seconds" not in source


def test_an_answer_does_not_repeat_the_question_below_the_table(env, qapp, page, wait, monkeypatch):
    """User 2026-11-18: after Allow or Block the whole question (a long login URL and all) stood once more as text below
    the window - the table shows the answer already. The note line says only what was done, in a few words."""
    fill(env)
    page.poll()
    long = ("claude-16ecaaaf: deny (once): open https://claude.com/cai/oauth/authorize?code=true&client_id=x"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A40067%2Fcallback&state=abc in your browser (host claude.com); its "
            "login callback goes to http://localhost:40067/callback in the sandbox")
    calls = answers_to(page, monkeypatch, out=long)
    monkeypatch.setattr(pages, "ask_first", lambda *a, **k: True)
    page.open.selectRow(0)
    page.block_button.click()
    assert wait(lambda: calls and not page.answers)
    note = page.message.text()
    assert "oauth" not in note and "callback" not in note and len(note) < 80, note
    assert note == "Blocked always."
