"""The web interface's routes for editing credentials and giving them to sandboxes that run (2026-11-19): each asks
first (its policy key), checks every value before anything runs, and passes values as --flag=VALUE or after "--" so
none is read as another option. Through the real Service and Api down to the command line (test_web_matrix: only the
process is faked); the command lines that work are in test_web_matrix.ACTIONS."""
import json

import pytest

from test_web_matrix import Web, home  # noqa: F401 - the isolated XDG folders of the web's tests


def post(path, body, host="local"):
    web = Web()
    status, _, data = web.request("POST", f"/api/hosts/{host}{path}", body, web.headers("full"))
    return status, json.loads(data), web.processes.args()


@pytest.mark.parametrize("path, body, key", [
    ("/git-logins/edit", {"url": "https://github.com", "user": "bob"}, "login-edit"),
    ("/publish-logins/pypi/edit", {"secret": "PYPI2"}, "login-edit"),
    ("/secrets/GH_TOKEN/rename", {"new": "GH_PUSH"}, "secret-rename"),
    ("/secrets/GH_TOKEN/send", {"host": "ct1"}, "secret-send"),
    ("/session-logins/give", {"run": "web", "fingerprint": "abcdef12"}, "login-give"),
    ("/vpn/mv/set", {"settings": {"provider": "office"}}, "vpn-replace"),
])
def test_each_change_is_refused_until_it_is_confirmed_and_names_its_policy_key(path, body, key):
    status, answer, ran = post(path, body)
    assert status == 400 and answer.get("confirm") == key and ran == [], answer
    status, answer, ran = post(path, {**body, "confirm": True})
    assert status == 200 and len(ran) == 1, answer


def test_a_secret_that_replaces_one_on_the_other_host_is_asked_about_twice():
    """`secrets send --replace` replaces a value on another machine: its own key (secret-replace) on top of the
    send's - both are the one {"confirm": true} of the page's second question, which names it."""
    status, answer, ran = post("/secrets/GH_TOKEN/send", {"host": "ct1", "replace": True, "confirm": True})
    assert status == 200 and ran == [("local", ["secrets", "send", "GH_TOKEN", "ct1", "--yes", "--replace"])]
    status, answer, ran = post("/secrets/GH_TOKEN/send", {"host": "ct1", "replace": True})
    assert status == 400 and ran == []


@pytest.mark.parametrize("path, body, reason", [
    ("/git-logins/edit", {"url": "https://github.com", "confirm": True}, "nothing to change"),
    ("/git-logins/edit", {"url": "https://github.com", "new_url": "ftp://x", "confirm": True}, ""),
    ("/git-logins/edit", {"url": "https://github.com", "user": "a b", "confirm": True}, "user is"),
    ("/git-logins/edit", {"url": "https://github.com", "secret": "--keep", "confirm": True}, ""),
    ("/git-logins/edit", {"url": "--help", "user": "bob", "confirm": True}, ""),
    ("/publish-logins/pypi/edit", {"confirm": True}, "nothing to change"),
    ("/publish-logins/nokind/edit", {"secret": "X", "confirm": True}, ""),
    ("/publish-logins/pypi/edit", {"url": "http://plain.example.org", "confirm": True}, ""),
    ("/secrets/GH_TOKEN/rename", {"new": "GH_TOKEN", "confirm": True}, "another name"),
    ("/secrets/GH_TOKEN/rename", {"new": "9bad", "confirm": True}, ""),
    ("/secrets/GH_TOKEN/rename", {"confirm": True}, ""),
    ("/secrets/GH_TOKEN/send", {"host": "local", "confirm": True}, "another host"),
    ("/secrets/GH_TOKEN/send", {"host": "--yes", "confirm": True}, ""),
    ("/secrets/GH_TOKEN/send", {"host": "ct1", "as": "bad name", "confirm": True}, ""),
    ("/session-logins/give", {"run": "web", "confirm": True}, "fingerprint"),
    ("/session-logins/give", {"run": "web", "fingerprint": "nothex!!", "confirm": True}, "fingerprint"),
    ("/session-logins/give", {"run": "web", "agent": "bash", "fingerprint": "abcdef12", "confirm": True}, ""),
    ("/session-logins/give", {"run": "--host", "fingerprint": "abcdef12", "confirm": True}, ""),
    ("/session-logins/give", {"run": "web", "from_session": "a b", "fingerprint": "abcdef12", "confirm": True}, ""),
    ("/vpn/mv/set", {"settings": {}, "confirm": True}, "settings is"),
    ("/vpn/mv/set", {"settings": {"wireguard": "x"}, "confirm": True}, "settings is"),
    ("/vpn/mv/set", {"settings": {"provider": "two\nlines"}, "confirm": True}, "one line"),
    ("/vpn/mv/set", {"settings": {"exit-node": ""}, "confirm": True}, "one line"),
])
def test_a_value_that_is_none_is_refused_before_anything_runs(path, body, reason):
    status, answer, ran = post(path, body)
    assert status == 400 and ran == [], (answer, ran)
    assert reason in json.dumps(answer), answer


def test_a_login_is_shown_without_a_question_and_given_only_with_the_fingerprint_shown():
    """check_only runs the dry run (it changes nothing: no question); the give carries --yes and --expect, so only
    the login that was shown is given - on the host of the page."""
    status, _, ran = post("/session-logins/give", {"run": "web", "agent": "codex", "check_only": True}, host="ct1")
    assert status == 200 and ran == [("ct1", ["login", "give", "web", "codex", "--dry-run", "--json"])]
    status, _, ran = post("/session-logins/give", {"run": "web", "agent": "codex", "fingerprint": "0123abcd",
                                                   "confirm": True}, host="ct1")
    assert status == 200 and ran == [("ct1", ["login", "give", "web", "codex", "--yes", "--expect=0123abcd",
                                              "--json"])]


def test_vpn_settings_go_in_a_fixed_order_and_the_name_after_two_dashes():
    status, _, ran = post("/vpn/mv/set", {"settings": {"hostname": "box", "provider": "office",
                                                       "bootstrap-attempts": "default"}, "confirm": True})
    assert status == 200 and ran == [("local", ["vpn", "set", "--provider=office", "--hostname=box",
                                                "--bootstrap-attempts=default", "--", "mv"])]
