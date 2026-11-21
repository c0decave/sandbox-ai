"""The web interface's "Open" of a browser question whose login returns to the sandbox's loopback (oauth_relay.py):
the catcher starts on the web's machine only for a browser on that machine (the page's client on its loopback), as
`sandbox-ai approvals relay ID --url URL [--host HOST]` there; a browser elsewhere is told to use the device-code
login. Nothing changes for any other question."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_web import FakeRunner, HEADERS, add_user, cookie_of, home, login, service  # noqa: E402,F401

URL = ("https://auth.openai.com/oauth/authorize?response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2F"
       "auth%2Fcallback&state=S")
QUESTION = {"id": "12-3-4", "kind": "browser", "url": URL,
            "callback": {"url": "http://127.0.0.1:1455/auth/callback", "port": 1455, "path": "/auth/callback"}}


def client(questions, address):
    secret = add_user()
    runner = FakeRunner({("approvals", "list"): (0, json.dumps(questions), ""),
                         ("approvals", "relay"): (0, "its login callback (http://127.0.0.1:1455/auth/callback) is "
                                                     "relayed into the sandbox once\n", "")})
    svc = service(runner)
    _, headers, body = login(svc, secret)
    ok = {**HEADERS, "Cookie": cookie_of(headers), "X-CSRF-Token": body["csrf"]}

    def call(method, path, data=None):
        status, _, out = svc.handle(method, path, ok, json.dumps(data).encode() if data is not None else b"", address)
        return status, json.loads(out)
    call.runner = runner
    return call


@pytest.mark.parametrize("host", ["local", "ct1"])
def test_a_browser_on_the_webs_machine_gets_the_catcher_there(host):
    call = client([QUESTION], "127.0.0.1")
    status, out = call("POST", f"/api/hosts/{host}/approvals/12-3-4",
                       {"decision": "allow", "scope": "once", "no_open": True})
    assert status == 200, out
    assert "relayed into the sandbox once" in out["output"]
    commands = [(h, args) for h, args in call.runner.calls if args[:1] == ["approvals"]]
    assert commands[-2] == (host, ["approvals", "allow", "12-3-4", "--scope", "once", "--no-open"])
    assert commands[-1] == ("local", ["approvals", "relay", "12-3-4", "--url", URL,
                                      *([] if host == "local" else ["--host", host])])


def test_a_browser_elsewhere_is_told_to_use_the_device_login():
    call = client([QUESTION], "192.0.2.1")
    # Not read again for it: what the page listed last (its poll of every host's questions) says.
    status, out = call("POST", "/api/hosts/local/approvals/12-3-4", {"decision": "allow", "no_open": True})
    assert status == 200 and "device-code login" not in out["output"]
    assert [args for _, args in call.runner.calls if args[:2] == ["approvals", "list"]] == []
    assert call("GET", "/api/approvals")[0] == 200
    status, out = call("POST", "/api/hosts/local/approvals/12-3-4", {"decision": "allow", "no_open": True})
    assert status == 200 and "device-code login" in out["output"]
    assert not [args for _, args in call.runner.calls if args[:2] == ["approvals", "relay"]]


def test_other_questions_and_answers_start_nothing():
    call = client([{**QUESTION, "callback": None}, {"id": "12-3-5", "kind": "dns", "name": "a.example"}],
                  "127.0.0.1")
    assert call("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "allow", "no_open": True})[0] == 200
    assert call("POST", "/api/hosts/ct1/approvals/12-3-5", {"decision": "allow", "scope": "run"})[0] == 200
    assert call("POST", "/api/hosts/ct1/approvals/12-3-4", {"decision": "deny"})[0] == 200
    assert not [args for _, args in call.runner.calls if args[:2] == ["approvals", "relay"]]
