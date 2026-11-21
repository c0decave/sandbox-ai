"""A browser question of another host whose login returns to the sandbox's loopback (sandbox-ai's oauth_relay.py):
"Open here and allow" allows it there with --no-open, then starts this machine's one-shot catcher (`sandbox-ai
approvals relay ID --url URL --host HOST`, forwarding over that host's SSH connection) before the page opens here.
The confirmation says what happens; a question without a callback starts nothing."""
import pytest

from sandbox_ai_app import backend, widgets

from test_ui import env, remote_question, window  # noqa: F401 - fixtures

URL = ("https://auth.openai.com/oauth/authorize?response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2F"
       "auth%2Fcallback&state=S")
CALLBACK = {"url": "http://127.0.0.1:1455/auth/callback", "port": 1455, "path": "/auth/callback"}


def test_the_callback_field_is_kept_only_in_its_checked_shape():
    kept = backend.RemoteQuestion("ct1", {"id": "1-1-1", "kind": "browser", "url": URL, "callback": CALLBACK})
    assert kept["callback"] == CALLBACK
    for bad in ({**CALLBACK, "port": "1455"}, {**CALLBACK, "port": True}, {**CALLBACK, "port": 70000}, "x", None):
        assert "callback" not in backend.RemoteQuestion("ct1", {"id": "1", "kind": "browser", "url": URL,
                                                                "callback": bad})
    assert "callback" not in backend.RemoteQuestion("ct1", {"id": "1", "kind": "dns", "callback": CALLBACK})


def test_host_answer_relaying_answers_there_then_starts_the_catcher_here(monkeypatch):
    sai = backend.SandboxAI()
    ran = []
    monkeypatch.setattr(sai, "host_answer", lambda host, argv, timeout=60: ran.append(("there", host, argv))
                        or (0, "ct1: allow (once): open ...", ""))
    monkeypatch.setattr(sai, "call", lambda *args, **kwargs: ran.append(("here", args))
                        or (0, "its login callback ... is relayed into the sandbox once\n", ""))
    status, out, _ = sai.host_answer_relaying("ct1", ["approvals", "allow", "1-1-1", "--scope", "once", "--no-open"],
                                              "1-1-1", URL)
    assert status == 0 and "relayed into the sandbox once" in out and out.startswith("ct1: allow")
    assert ran == [("there", "ct1", ["approvals", "allow", "1-1-1", "--scope", "once", "--no-open"]),
                   ("here", ("approvals", "relay", "1-1-1", "--url", URL, "--host", "ct1"))]
    # Not allowed there: nothing starts here.
    ran.clear()
    monkeypatch.setattr(sai, "host_answer", lambda host, argv, timeout=60: (1, "", "no open question"))
    assert sai.host_answer_relaying("ct1", ["approvals", "allow", "1-1-1"], "1-1-1", URL)[0] == 1 and ran == []
    # A busy port here: the answer stands, the note says what to do.
    monkeypatch.setattr(sai, "host_answer", lambda host, argv, timeout=60: (0, "allowed", ""))
    monkeypatch.setattr(sai, "call", lambda *args, **kwargs: (1, "port 1455 on this computer is busy; use the "
                                                                 "agent's device-code login instead", ""))
    status, out, _ = sai.host_answer_relaying("ct1", ["approvals", "allow", "1-1-1"], "1-1-1", URL)
    assert status == 0 and "device-code login" in out


@pytest.mark.parametrize("callback", [CALLBACK, None])
def test_open_here_relays_the_callback_of_another_hosts_question(window, monkeypatch, wait, callback):
    from sandbox_ai_app import pages
    question = remote_question(id="1700000000-1-2", kind="browser", question="open a page", url=URL,
                               **({"callback": callback} if callback else {}))
    ran, opened = [], []
    monkeypatch.setattr(window.sai, "remote_hosts", lambda: ["ct1"])
    monkeypatch.setattr(window.sai, "host_questions", lambda name, timeout=30: [question])
    monkeypatch.setattr(window.sai, "host_run", lambda name, *args, timeout=120: ran.append((name, args))
                        or (0, "ok", ""))
    real_call = window.sai.call

    def call(*args, **kwargs):
        if args[:2] == ("approvals", "relay"):
            ran.append(("here", args))
            return 0, "its login callback is relayed into the sandbox once", ""
        return real_call(*args, **kwargs)
    monkeypatch.setattr(window.sai, "call", call)
    monkeypatch.setattr(pages.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    page = window.approvals
    page.poll_remote()
    assert wait(lambda: page.open.rowCount() == 1, 15)
    page.open.selectRow(0)
    widgets.SHOWN.clear()
    page.open_here()  # no question first (since the approvals rework): the page's note says it
    assert ("relays that one request into the sandbox" in page.message.text()) == bool(callback)
    assert wait(lambda: page.answering is None and opened, 15)
    assert not [shown for shown in widgets.SHOWN if shown[0] == "Allow this request?"]
    assert ("ct1", ("approvals", "allow", "1700000000-1-2", "--scope", "once", "--no-open")) in ran
    relays = [entry for entry in ran if entry[0] == "here"]
    if callback:
        assert relays == [("here", ("approvals", "relay", "1700000000-1-2", "--url", URL, "--host", "ct1"))]
        assert ran.index(relays[0]) > ran.index(("ct1", ("approvals", "allow", "1700000000-1-2", "--scope", "once",
                                                         "--no-open")))
    else:
        assert relays == []
    assert opened and opened[0].startswith("https://auth.openai.com/oauth/authorize")
