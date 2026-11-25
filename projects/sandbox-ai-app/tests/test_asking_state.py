"""The Sessions page's State column says "asking" only while a start has a question that still waits (user
2026-11-19: "läuft, fragt" was shown when nothing was asked). The Approvals page keeps every question it lists - timed
out, allowed, refused - so the mark and the Hosts page's counts take only the open ones."""
import types

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import ui  # noqa: E402
from sandbox_ai_app.host_choice import LOCAL  # noqa: E402


def marks(questions):
    seen = {}
    pages = {
        "approvals": types.SimpleNamespace(questions=questions),
        "sandboxes": types.SimpleNamespace(set_asking=lambda asking: seen.update(asking=set(asking))),
        "hosts": types.SimpleNamespace(set_questions=lambda counts: seen.update(counts=dict(counts))),
    }
    window = types.SimpleNamespace(built=pages.get)
    ui.MainWindow.show_asking(window)
    return seen["asking"], seen["counts"]


def test_only_open_questions_mark_a_start_as_asking():
    questions = {
        (LOCAL, "claude-open", "1"): ("claude-open", {"id": "1", "state": "open"}, LOCAL),
        (LOCAL, "claude-timed-out", "2"): ("claude-timed-out", {"id": "2", "state": "timeout"}, LOCAL),
        (LOCAL, "claude-allowed", "3"): ("claude-allowed", {"id": "3", "result": {"decision": "allow"}}, LOCAL),
        (LOCAL, "claude-refused", "4"): ("claude-refused", {"id": "4", "result": {"decision": "deny"}}, LOCAL),
        ("box", "codex-open", "5"): ("codex-open", {"id": "5"}, "box"),
        ("box", "codex-old", "6"): ("codex-old", {"id": "6", "result": {"decision": "deny",
                                                                         "reason": "no answer in 60 s"}}, "box"),
    }
    asking, counts = marks(questions)
    assert asking == {(LOCAL, "claude-open"), ("box", "codex-open")}
    assert counts == {LOCAL: 1, "box": 1}


def test_no_questions_no_marks():
    assert marks({}) == (set(), {})
