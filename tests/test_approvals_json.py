"""`sandbox-ai approvals list --json`'s timing and scope fields, which the web interface and the app count down from
and offer scopes by (on any host: computed where the question is, so another machine's clock does not matter)."""
import calendar
import importlib.util
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("sandbox_approvals_json_tests", ROOT / "sandbox/approvals.py")
ap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ap)


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def test_seconds_left_count_from_the_questions_own_time_in_utc():
    now = calendar.timegm((2026, 7, 1, 12, 0, 0, 0, 0, 0))  # summer: a local-time parse would be off by the DST hour
    assert ap.question_times({"time": stamp(now - 15), "timeout": 60}, now) == {"timeout": 60, "seconds_left": 45}
    assert ap.question_times({"time": stamp(now - 90)}, now) == {"timeout": ap.QUESTION_SECONDS, "seconds_left": 0}
    assert ap.question_times({"time": "yesterday", "timeout": 30}, now) == {"timeout": 30, "seconds_left": None}
    for odd in (-1, 0, "60", 10 ** 9, True and None):
        assert ap.question_times({"time": stamp(now), "timeout": odd}, now)["timeout"] == ap.QUESTION_SECONDS


def test_max_scope_is_the_narrowest_of_policy_mode_and_kind():
    assert ap.widest_answer({"kind": "dns"}, {}) == "always"
    assert ap.widest_answer({"kind": "push"}, {}) == "run"
    assert ap.widest_answer({"kind": "dns"}, {"mode": {"approval_scope": "once"}}) == "once"
    assert ap.widest_answer({"kind": "dns"}, {}, widest="run") == "run"
    assert ap.widest_answer({"kind": "dns"}, {"mode": "junk"}, widest="nonsense") == "always"


def test_the_json_carries_them(monkeypatch):
    now = time.time()
    question = {"id": "q1", "kind": "browser", "url": "https://login.example.org/x", "time": stamp(now - 5),
                "timeout": 60}
    monkeypatch.setattr(ap, "questions", lambda runs=None, answered=False: [("r1", {"run": "r1"}, question)])
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    lines = []
    assert ap.main(["list", "--json"], out=lines.append) == 0
    item = __import__("json").loads(lines[0])[0]
    assert item["timeout"] == 60 and 50 <= item["seconds_left"] <= 55
    assert item["max_scope"] == "always" and item["url"] == "https://login.example.org/x"
