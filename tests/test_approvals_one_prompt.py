"""One question, one prompt: while the desktop app or the web interface watches the questions (a heartbeat in the
state directory), the launcher shows no notification with buttons of its own; and the answers every prompt offers
(approvals.choices), with the caps of the policy, the run's security mode and the kind of question."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_approvals_one_prompt_tests", ROOT / "sandbox/approvals.py")
ap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ap)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    return tmp_path / "state" / "sandbox-ai" / "approvals-watchers"


def test_a_heartbeat_marks_a_watcher_until_it_ends_or_goes_stale(state):
    assert not ap.watched()
    path = ap.heartbeat("app")
    assert path == state / f"{os.getpid()}.json" and path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text())
    assert record["pid"] == os.getpid() and record["kind"] == "app" and abs(record["time"] - time.time()) < 5
    assert ap.watched()
    assert not ap.watched(now=time.time() + ap.WATCHER_STALE + 1), "stale without a refresh"
    ap.heartbeat("app")  # a refresh
    assert ap.watched()
    ap.end_heartbeat()
    assert not path.exists() and not ap.watched()
    ap.end_heartbeat()  # twice is fine


def test_a_watcher_whose_process_is_gone_counts_not(state):
    dead = subprocess.Popen(["true"])
    dead.wait()
    ap.heartbeat("web", pid=dead.pid)
    assert not ap.watched()
    state.mkdir(parents=True, exist_ok=True)
    (state / "123.json").write_text("not json")
    (state / "x.json").write_text(json.dumps({"pid": os.getpid(), "kind": "app", "time": time.time()}))
    assert not ap.watched(), "only PID.json files whose pid is the one inside count"
    with pytest.raises(ValueError):
        ap.heartbeat("someone")


def ask(directory, ident, **question):
    (directory / f"{ident}.json").write_text(json.dumps({"id": ident, "time": "now", **question}))


def watch_once(directory, buttons, notes, seconds=1.0):
    stop = threading.Event()
    thread = threading.Thread(target=ap.watch, args=(directory, stop), kwargs={
        "notify": lambda title, text: notes.append(text), "interval": 0.05, "buttons": buttons})
    thread.start()
    time.sleep(seconds)
    stop.set()
    thread.join(5)


def test_watch_leaves_the_question_to_the_app_while_it_watches(state, tmp_path):
    directory = tmp_path / "run" / "approvals"
    directory.mkdir(parents=True)
    asked, notes = [], []

    def buttons(title, text, ident, **kwargs):
        asked.append((ident, kwargs.get("question", {}).get("kind")))
    ap.heartbeat("app")
    ask(directory, "1-1-1", kind="connect", destination="10.0.2.15:1234", address="10.0.2.15", port=1234,
        protocol="http")
    watch_once(directory, buttons, notes)
    assert asked == [] and notes == [], "the app prompts: no second prompt from the launcher"
    ap.end_heartbeat()
    (directory / "1-1-1.done").write_text("{}")  # answered in the app
    ask(directory, "1-1-2", kind="dns", name="pypi.org")
    watch_once(directory, buttons, notes)
    assert asked == [("1-1-2", "dns")], "nobody watches: the launcher's buttons ask"
    # Without buttons (an old notify-send) the plain notification is left out too while the app watches.
    asked.clear()
    (directory / "1-1-2.done").write_text("{}")
    ap.heartbeat("web")
    ask(directory, "1-1-3", kind="dns", name="example.org")
    watch_once(directory, None, notes)
    assert notes == []


def test_the_notification_with_buttons_steps_aside_while_the_app_watches(state):
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return type("Done", (), {"stdout": "once\n"})()
    ap.heartbeat("app")
    assert ap.desktop_question("t", "x", "1-1-1", runner=runner, answer_with=lambda *a, **k: None) is None
    assert ap.desktop_question("t", "https://x.org/", "1-1-2", ap.BROWSER_ACTIONS, runner=runner,
                               answer_with=lambda *a, **k: None) is None
    assert commands == []
    # A canary's alarm (the sandbox is paused) is shown all the same.
    canary = (("continue", "allow", "once", "Continue"), ("stop", "deny", "run", "Stop the sandbox"))
    assert ap.desktop_question("t", "a decoy was read", "1-1-3", canary, runner=runner,
                               answer_with=lambda *a, **k: None) is None
    assert len(commands) == 1 and "--action=continue=Continue" in commands[0]


CONNECT = {"id": "1-1-1", "kind": "connect", "destination": "10.0.2.15:1234", "address": "10.0.2.15", "port": 1234,
           "protocol": "http", "remembers": {"tcp": "10.0.2.15:1234", "verified": None,
                                             "why": "the agent named no host"}}


def table(found):
    return {(c["decision"], c["scope"]): c for c in found}


def test_every_answer_is_offered(state):
    found = ap.choices(CONNECT, {})
    assert [(c["decision"], c["scope"], c["label"]) for c in found] == [
        ("allow", "once", "Allow once"), ("allow", "run", "Allow for this run"), ("allow", "always", "Allow always"),
        ("deny", "run", "Block"), ("deny", "always", "Block always")]
    assert all(c["enabled"] for c in found)
    always = table(found)["allow", "always"]
    assert always["reason"] == "remembers" and "10.0.2.15:1234 (address only" in always["why"], \
        "what an always for a bare address remembers"


@pytest.mark.parametrize("mode, cap", [("high", "run"), ("paranoid", "once")])
def test_the_security_mode_caps_the_answers(state, mode, cap):
    found = table(ap.choices(CONNECT, {"mode": {"name": mode, "approval_scope": cap}}))
    always = found["allow", "always"]
    assert not always["enabled"] and always["why"] == f"capped by the security mode {mode}" and always["reason"] == "mode"
    assert found["allow", "run"]["enabled"] == (cap == "run")
    assert not found["deny", "always"]["enabled"] and found["deny", "always"]["reason"] == "mode"
    assert found["allow", "once"]["enabled"] and found["deny", "run"]["enabled"]


def test_the_policy_caps_the_answers(state):
    found = table(ap.choices(CONNECT, {}, widest="once"))
    assert not found["allow", "run"]["enabled"] and found["allow", "run"]["reason"] == "policy"
    assert "administrator's policy" in found["allow", "always"]["why"]
    assert not found["deny", "always"]["enabled"]
    assert found["allow", "once"]["enabled"] and found["deny", "run"]["enabled"]


@pytest.mark.parametrize("kind, reason", [("push", "push"), ("http", "request"), ("mcp", "request"),
                                          ("tool", "request"), ("device", "device")])
def test_the_kind_of_question_caps_the_answers(state, kind, reason):
    found = table(ap.choices({"id": "1-1-1", "kind": kind}, {}))
    assert not found["allow", "always"]["enabled"] and found["allow", "always"]["reason"] == reason
    assert found["allow", "run"]["enabled"]
    assert not found["deny", "always"]["enabled"] and found["deny", "always"]["reason"] == "block-kind"


def test_a_name_lookup_stays_pinned_for_the_run(state):
    found = table(ap.choices({"id": "1-1-1", "kind": "dns", "name": "pypi.org"}, {}))
    assert found["allow", "once"]["enabled"] and found["allow", "once"]["reason"] == "dns-pinned"
    assert found["deny", "always"]["enabled"] and found["allow", "always"]["enabled"]


@pytest.mark.parametrize("mode, cap", [(None, "once"), ("paranoid", "once")])
def test_a_name_cannot_be_allowed_where_only_once_is(state, mode, cap):
    """A name is pinned for the run: where the policy or the mode allows only once, no allow is offered (answer()
    refuses it), the blocks are."""
    record = {"mode": {"name": mode, "approval_scope": cap}} if mode else {}
    found = table(ap.choices({"id": "1-1-1", "kind": "dns", "name": "pypi.org"}, record,
                             widest="always" if mode else "once"))
    assert not any(found["allow", scope]["enabled"] for scope in ap.SCOPES)
    assert found["allow", "once"]["reason"] == "dns-once" and "pinned for the run" in found["allow", "once"]["why"]
    assert found["deny", "run"]["enabled"]


def test_browser_and_canary_questions_have_their_own_answers(state):
    assert [(c["decision"], c["scope"], c["label"]) for c in ap.choices({"kind": "browser", "url": "https://x.org/"})] \
        == [("allow", "once", "Open"), ("deny", "once", "Ignore")]
    assert [(c["decision"], c["scope"], c["label"]) for c in ap.choices({"kind": "canary"})] \
        == [("allow", "once", "Continue"), ("deny", "run", "Stop the sandbox")]


def test_the_notification_offers_what_is_possible_and_says_why_not(state):
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return type("Done", (), {"stdout": "run\n"})()
    answered = []
    chosen = ap.desktop_question("t", "connect to 10.0.2.15:1234", "1-1-1", runner=runner,
                                 answer_with=lambda ident, decision, scope, out: answered.append((decision, scope)),
                                 question=CONNECT, record={"mode": {"name": "high", "approval_scope": "run"}})
    assert chosen == "run" and answered == [("allow", "run")]
    actions = [part for part in commands[0] if part.startswith("--action=")]
    assert actions == ["--action=once=Allow once", "--action=run=Allow for this run", "--action=deny=Block"]
    assert "Allow always: capped by the security mode high" in commands[0][-1]
    assert "Block always: capped by the security mode high" in commands[0][-1]
