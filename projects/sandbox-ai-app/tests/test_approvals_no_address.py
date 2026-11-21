"""The Approvals page for a lookup whose name does not exist (user 2026-11-20: `sandbox-ai approvals allow` said
"proxy.example has no address ...: there is nothing to allow ... Deny it, or leave it" - "und doch gibt es das als
Freigabe-Frage, wie gehen wir damit um?"): sandbox-ai closes such a question when it is allowed (denied for the run,
nothing opened); the page says so in one line and a notice that goes by itself - no error, no dialog, nothing to undo -
and the question is gone from the open ones. A lookup that failed for now stays an error that says try again."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from sandbox_ai_app import approvals_sections, pages, short_notices, widgets  # noqa: E402

from test_approvals_rework import answers_to, env, fake_run, page, question, shown_ids  # noqa: E402,F401

CLOSED = ("opencode-1: deny (run): lookup of gone.example.org\n  denied for the run: gone.example.org does not exist "
          "(Name or service not known) - nothing was opened and the question is closed (take it back once the name "
          "exists: sandbox-ai approvals ask-again 1763600000-7-1)\n")


def notices(monkeypatch):
    said = []
    real = short_notices.short_notice

    def short_notice(parent, title, text, *args, **kwargs):
        said.append((title, text, bool(kwargs.get("error"))))
        return real(parent, title, text, *args, **kwargs)
    monkeypatch.setattr(short_notices, "short_notice", short_notice)
    return said


def test_the_marker_is_sandbox_ais_own(page):
    assert pages.CLOSED_NOTE == page.sai.module("approvals").CLOSED_NOTE
    assert pages.closed_note(CLOSED) == page.sai.module("approvals").closed_note(CLOSED)
    assert pages.closed_note("opencode-1: allow (run): lookup of pypi.org\n  pypi.org -> 151.101.0.223") is None


def test_an_allow_that_closed_the_question_is_said_in_one_line(env, qapp, page, wait, monkeypatch):
    directory = fake_run(env)
    question(directory, "1763600000-7-1", kind="dns", name="gone.example.org")
    page.poll()
    calls = answers_to(page, monkeypatch, status=0, out=CLOSED)
    said = notices(monkeypatch)
    page.open.selectRow(0)
    page.allow_run_button.click()
    assert wait(lambda: calls and not page.answers)
    assert calls == [("1763600000-7-1", "allow", "run")]
    text = page.message.text()
    assert text.startswith("Closed, not allowed: gone.example.org does not exist (Name or service not known)"), text
    assert "allowed in this run" not in text.lower()
    assert said and said[-1][0] == "Question closed" and said[-1][2] is False, "a notice, not an error"
    assert widgets.SHOWN == [], "no dialog"
    assert not page.undo_button.isVisibleTo(page), "nothing to undo: nothing was allowed"


def test_e2e_a_reserved_name_is_closed_through_the_real_command(env, qapp, page, wait, monkeypatch):
    """The page's Allow runs ./sandbox-ai approvals allow (no stand-in): proxy.example is a reserved name - never
    looked up - and its question closes; the page says so and lists it open no more."""
    directory = fake_run(env)
    question(directory, "1763600000-7-2", kind="dns", name="proxy.example")
    page.poll()
    assert shown_ids(page) == ["1763600000-7-2"]
    said = notices(monkeypatch)
    page.open.selectRow(0)
    page.allow_button.click()
    assert wait(lambda: not page.answers, 60)
    assert page.message.text().startswith("Closed, not allowed: proxy.example is a reserved name"), \
        page.message.text()
    assert said and said[-1][2] is False
    assert (directory / "1763600000-7-2.answer").exists()
    page.poll()
    assert shown_ids(page) == [], "not open any more"


def test_a_lookup_that_failed_for_now_stays_an_error_that_says_try_again(env, qapp, page, wait, monkeypatch):
    directory = fake_run(env)
    question(directory, "1763600000-7-3", kind="dns", name="flaky.example.org")
    page.poll()
    why = ("sandbox-ai approvals: flaky.example.org could not be looked up (on this host: Temporary failure in name "
           "resolution): nothing was allowed, the question stays open - try again (sandbox-ai approvals allow "
           "1763600000-7-3) once the lookup works")
    answers_to(page, monkeypatch, status=1, out="", err=why)
    page.open.selectRow(0)
    page.allow_run_button.click()
    assert wait(lambda: short_notices.SHOWN and not page.answers)
    assert short_notices.SHOWN[-1][0] == "Not answered" and "try again" in short_notices.SHOWN[-1][1]
    assert widgets.SHOWN == []


def test_the_blocked_list_says_why_in_the_apps_words():
    closed = {"kind": "denied", "detail": "gone.example.org does not exist (Name or service not known)"}
    assert approvals_sections.why_text(closed).startswith("Denied for this run: gone.example.org does not exist")
    assert "Ask again" in approvals_sections.why_text(closed)
    reserved = {"kind": "other", "what": "lookup of collect.r5.test",
                "reason": "a reserved name (RFC 2606/6761): it never exists, so its lookup is refused without a "
                          "question - there is nothing to allow"}
    assert "never exists" in approvals_sections.why_text(reserved)
    assert approvals_sections.why_text({"kind": "denied"}).startswith("You denied it for this run")
