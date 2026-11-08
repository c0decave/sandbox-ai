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
    assert ap.question_times({"time": stamp(now - 90), "timeout": 60}, now) == {"timeout": 60, "seconds_left": 0}
    assert ap.question_times({"time": "yesterday", "timeout": 30}, now) == {"timeout": 30, "seconds_left": None}
    assert ap.question_times({"kind": "canary", "time": stamp(now - 100), "timeout": 300}, now)["seconds_left"] == 200
    # No deadline of its own: no deadline (front ends must not show it as refused after a minute).
    for odd in (-1, 0, "60", 10 ** 9, True, None):
        assert ap.question_times({"time": stamp(now), "timeout": odd}, now) == {"timeout": None, "seconds_left": None}
    assert ap.question_times({"time": stamp(now - 90)}, now) == {"timeout": None, "seconds_left": None}


def test_browser_questions_have_no_deadline():
    """serve_browser writes them without a timeout and nothing refuses them: open until answered (or the run
    ends), whatever their age."""
    now = time.time()
    assert ap.question_times({"kind": "browser", "time": stamp(now - 3600)}, now) == \
        {"timeout": None, "seconds_left": None}
    assert ap.question_times({"kind": "browser", "time": stamp(now - 3600), "timeout": 60}, now)["timeout"] is None


def test_max_scope_is_the_narrowest_of_policy_mode_and_kind():
    assert ap.widest_answer({"kind": "dns"}, {}) == "always"
    assert ap.widest_answer({"kind": "push"}, {}) == "run"
    assert ap.widest_answer({"kind": "connect"}, {"mode": {"approval_scope": "once"}}) == "once"
    # A name is pinned for the run once allowed: with only "once" allowed it cannot be allowed (answer refuses it).
    assert ap.widest_answer({"kind": "dns"}, {"mode": {"approval_scope": "once"}}) is None
    assert ap.widest_answer({"kind": "dns"}, {}, widest="once") is None
    assert ap.widest_answer({"kind": "dns"}, {"mode": {"approval_scope": "run"}}) == "run"
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
    assert item["timeout"] is None and item["seconds_left"] is None, "a browser question has no deadline"
    assert item["max_scope"] == "always" and item["url"] == "https://login.example.org/x"
    assert item["url_host"] == "login.example.org" and "url_truncated" not in item


def listed(monkeypatch, *pending, widest="always"):
    monkeypatch.setattr(ap, "questions", lambda runs=None, answered=False: list(pending))
    monkeypatch.setattr(ap, "policy_scope", lambda: widest)
    lines = []
    assert ap.main(["list", "--json"], out=lines.append) == 0
    return __import__("json").loads(lines[0])


def test_browser_url_whole_checked_and_its_real_host(monkeypatch):
    """The whole URL (the host takes up to 4000 characters), escaped; only one an "allow" would open (URL: no user
    part, printable ASCII): https://trusted.example@other.tld/ is no link, and url_host names where it goes."""
    now = time.time()
    long = "https://login.example.org/" + "a" * 3000
    ok = {"id": "q1", "kind": "browser", "url": long, "time": stamp(now)}
    user = {"id": "q2", "kind": "browser", "url": "https://trusted.example@other.tld/", "time": stamp(now)}
    slash = {"id": "q3", "kind": "browser", "url": "https://a.example\\.b.example/", "time": stamp(now)}
    first, second, third = listed(monkeypatch, ("r1", {}, ok), ("r1", {}, user), ("r1", {}, slash))
    assert first["url"] == long and first["url_host"] == "login.example.org" and "url_truncated" not in first
    assert "url" not in second and second["url_refused"] and second["url_host"] == "other.tld"
    assert "other.tld" in second["question"], "the table names the real destination as well"
    assert third["url_host"] == "a.example", "a browser reads the backslash as a slash"
    longest = "https://x.example/" + "b" * (4000 - len("x.example/"))
    assert ap.browser_fields(longest)["url"] == longest, "as long as the host accepts: whole"
    assert "url" not in ap.browser_fields(longest + "b"), "longer: an allow would refuse it"


def test_dns_allow_under_a_once_cap_is_refused_not_widened(monkeypatch, tmp_path):
    """A security mode (or policy) capped at "once": a name would stay pinned for the run, so its allow is refused
    with why (list --json: max_scope null, allow_refused), never written as "run"."""
    now = time.time()
    record = {"directory": str(tmp_path), "workspace": str(tmp_path), "mode": {"name": "high", "approval_scope": "once"}}
    (tmp_path / "approvals").mkdir()
    question = {"id": "1-1-1", "kind": "dns", "name": "pypi.org", "time": stamp(now), "timeout": 60}
    item, = listed(monkeypatch, ("r1", record, question))
    assert item["max_scope"] is None and "pinned for the run" in item["allow_refused"]
    import pytest
    with pytest.raises(ValueError, match="pinned for the run"):
        ap.answer("1-1-1", "allow", "once", resolver=lambda name: ["151.101.0.223"], out=lambda line: None)
    assert not (tmp_path / "approvals" / "1-1-1.answer").exists()
    monkeypatch.setattr(ap, "policy_scope", lambda: "once")
    with pytest.raises(ValueError, match="pinned for the run"):
        ap.answer("1-1-1", "allow", "once", resolver=lambda name: ["151.101.0.223"], out=lambda line: None)
    assert ap.answer("1-1-1", "deny", "run", out=lambda line: None)["decision"] == "deny", "a deny still works"
