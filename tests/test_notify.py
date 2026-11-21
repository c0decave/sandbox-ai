"""sandbox-ai notify: questions and alarms to the phone (ntfy, Telegram, WhatsApp, a webhook), sent from the host.

Every channel is checked against a fake server of its own on 127.0.0.1 (no internet): the exact request (URL, headers,
body); the rate limit that sums up what follows; quiet hours; the minimal detail; a token that never reaches the
settings file, the log or a message (a canary value scanned for); text a sandbox chose made harmless; a server that
does not answer never holding up the caller; a redirect never followed; and end to end a real question of a run's
approvals directory, watched as the launcher watches it, reaching the fake ntfy server as one message."""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


N = load("sandbox_ai_notify_test", "notify.py")
TOKEN = "tok-CANARY-7f3a9e2b1c-secret-value"
BOT = "123456789:AAH-CANARY_bot_token_value_0123456789"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_SECRETS_PLAIN", "1")
    monkeypatch.setattr(N, "HOST_NAME", "box")
    return tmp_path


def keep(name, value):
    N.secret_store().FileStore().set(name, value, plain=True)


class Fake:
    """A fake HTTP server: every request kept as {"method", "path", "headers", "body"}; answers STATUS with BODY."""

    def __init__(self, status=200, body=b'{"ok": true}', redirect=None, hold=False):
        self.requests, self.status, self.body, self.redirect, self.hold = [], status, body, redirect, hold
        self.got = threading.Event()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers),
                                      "body": self.rfile.read(length)})
                fake.got.set()
                if fake.hold:
                    time.sleep(3)
                if fake.redirect:
                    self.send_response(302)
                    self.send_header("Location", fake.redirect)
                    self.end_headers()
                    return
                self.send_response(fake.status)
                self.send_header("Content-Length", str(len(fake.body)))
                self.end_headers()
                self.wfile.write(fake.body)

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def json(self, index=0):
        return json.loads(self.requests[index]["body"])

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    servers = []

    def make(**kwargs):
        server = Fake(**kwargs)
        servers.append(server)
        return server
    yield make
    for server in servers:
        server.close()


def run(*argv):
    lines = []
    return N.main(list(argv), out=lines.append), "\n".join(lines)


def event(kind="questions", run="brisk-otter", what="wants a network connection", detail="github.com:443"):
    return {"event": kind, "run": run, "what": what, "detail": detail, "time": time.time()}


# -- the exact requests -----------------------------------------------------------------------------------------------

def test_ntfy_request(fake):
    server = fake()
    keep("NTFY_TOKEN", TOKEN)
    assert run("add", "phone", "--ntfy", f"{server.url}/sandbox-alerts", "--token-secret", "NTFY_TOKEN")[0] == 0
    channel = N.load()["channels"][0]
    assert N.send(channel, [event()]) is None
    request = server.requests[0]
    assert request["path"] == "/" and request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["Content-Type"] == "application/json"
    body = server.json()
    assert body["topic"] == "sandbox-alerts" and body["title"] == "sandbox-ai: questions" and body["priority"] == 4
    assert body["message"].startswith("brisk-otter: wants a network connection\non box at ")
    assert "github.com" not in body["message"] and "click" not in body  # normal detail; no link set


def test_ntfy_without_a_server_is_ntfy_sh_and_a_link_is_the_click():
    assert run("add", "p", "--ntfy", "only-a-topic-long-enough-1234")[0] == 0
    channel = N.load()["channels"][0]
    assert channel["url"] == "https://ntfy.sh" and channel["topic"] == "only-a-topic-long-enough-1234"
    assert run("link", "https://web.example.org:8443")[0] == 0
    url, headers, body, _ = N.request_of(channel, "t", "x", 5, link=N.load()["link"])
    assert url == "https://ntfy.sh/" and json.loads(body)["click"] == "https://web.example.org:8443"
    assert json.loads(body)["tags"] == ["rotating_light"] and "Authorization" not in headers


def test_telegram_request_plain_text(fake):
    server = fake()
    keep("TG_BOT", BOT)
    assert run("add", "tg", "--telegram", "--bot-token-secret", "TG_BOT", "--chat", "-1001234", "--api",
               server.url, "--detail", "full")[0] == 0
    channel = N.load()["channels"][0]
    assert N.send(channel, [event(what="wants <b>x</b>", detail="*evil* `x` [a](http://e)")]) is None
    request = server.requests[0]
    assert request["path"] == f"/bot{BOT}/sendMessage" and "Authorization" not in request["headers"]
    body = server.json()
    assert body["chat_id"] == -1001234 and body["disable_web_page_preview"] is True
    assert "parse_mode" not in body  # plain text: nothing in it is read as Markdown or HTML
    assert body["text"].startswith("sandbox-ai: questions\nbrisk-otter: wants (b)x(/b): ")
    assert "<" not in body["text"] and "`" not in body["text"]


def test_whatsapp_template_and_text(fake):
    server = fake()
    keep("WA_TOKEN", TOKEN)
    assert run("add", "wa", "--whatsapp", "--phone-id", "106540352242922", "--token-secret", "WA_TOKEN", "--recipient",
               "+4917612345678", "--template", "sandbox_alert", "--language", "de", "--api", server.url)[0] == 0
    channel = N.load()["channels"][0]
    assert N.send(channel, [event(run="run_*bold*")]) is None
    request = server.requests[0]
    assert request["path"] == "/106540352242922/messages"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    body = server.json()
    assert body["messaging_product"] == "whatsapp" and body["to"] == "4917612345678" and body["type"] == "template"
    assert body["template"]["name"] == "sandbox_alert" and body["template"]["language"] == {"code": "de"}
    text = body["template"]["components"][0]["parameters"][0]["text"]
    assert body["template"]["components"][0]["type"] == "body"
    assert "\n" not in text and "    " not in text and "*" not in text and "_" not in text  # one line, no markup
    assert text.startswith("sandbox-ai: questions - run--bold-: wants a network connection - on box at ")
    status, said = run("add", "wa", "--whatsapp", "--phone-id", "106540352242922", "--token-secret", "WA_TOKEN",
                       "--recipient", "+4917612345678", "--api", server.url)
    assert status == 0 and "only within 24 hours" in said and "replaced" in said
    assert N.send(N.load()["channels"][0], [event()]) is None
    body = server.json(1)
    assert body["type"] == "text" and body["text"]["preview_url"] is False
    assert body["text"]["body"].startswith("sandbox-ai: questions\nbrisk-otter: wants a network connection")


def test_webhook_request_and_a_url_that_is_a_secret(fake):
    server = fake()
    keep("HOOK_TOKEN", TOKEN)
    assert run("add", "hook", "--webhook", f"{server.url}/hooks/in", "--token-secret", "HOOK_TOKEN", "--events",
               "all", "--when", "always", "--rate", "0")[0] == 0
    channel = N.load()["channels"][0]
    assert channel["events"] == list(N.EVENTS) and channel["when"] == "always" and channel["rate"] == 0
    assert N.send(channel, [event("alarms", what="a canary went off")]) is None
    request = server.requests[0]
    assert request["path"] == "/hooks/in" and request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    body = server.json()
    assert body["source"] == "sandbox-ai" and body["host"] == "box" and body["title"] == "sandbox-ai: alarms"
    assert body["events"][0]["event"] == "alarms" and body["events"][0]["run"] == "brisk-otter"
    assert "detail" not in body["events"][0]
    keep("HOOK_URL", f"{server.url}/T000/B000/{TOKEN}")
    assert run("add", "slack", "--webhook-secret", "HOOK_URL")[0] == 0
    assert TOKEN not in N.path().read_text()
    secret_channel = [c for c in N.load()["channels"] if c["name"] == "slack"][0]
    assert N.send(secret_channel, [event()]) is None
    assert server.requests[1]["path"] == f"/T000/B000/{TOKEN}" and "Authorization" not in server.requests[1]["headers"]
    assert TOKEN not in N.where(secret_channel)


# -- what goes into the file, and what is refused ---------------------------------------------------------------------

def test_the_file_is_0600_with_names_only_and_list_json():
    keep("NTFY_TOKEN", TOKEN)
    status, said = run("add", "phone", "--ntfy", "https://ntfy.example.org/x1", "--token-secret", "NTFY_TOKEN",
                       "--quiet", "22:00-07:00", "--detail", "minimal", "--events", "questions,limits")
    assert status == 0 and "added" in said and "notify test phone" in said
    assert oct(N.path().stat().st_mode & 0o777) == "0o600" and TOKEN not in N.path().read_text()
    status, text = run("list", "--json")
    data = json.loads(text)
    channel = data["channels"][0]
    assert channel["secrets"] == ["NTFY_TOKEN"] and channel["where"] == "https://ntfy.example.org/x1"
    assert channel["events"] == ["questions", "limits"] and channel["quiet"] == "22:00-07:00"
    assert data["default_events"] == ["questions", "alarms"] and set(data["events"]) == set(N.EVENTS)
    assert run("remove", "phone")[0] == 0 and N.load()["channels"] == []
    assert run("remove", "phone")[0] == 1


def test_a_short_topic_on_ntfy_sh_is_warned_about():
    status, said = run("add", "p", "--ntfy", "alerts")
    assert status == 0 and "whoever knows the topic reads its messages" in said


@pytest.mark.parametrize("argv, words", [
    (["add", "a", "--ntfy", "http://ntfy.example.org/x"], "http only to this machine"),
    (["add", "a", "--webhook", "https://h.example/x?key=1"], "no ?query"),
    (["add", "a", "--webhook", "https://user:pw@h.example/x"], "no user"),
    (["add", "a", "--ntfy", "https://ntfy.sh/bad topic"], "the topic is letters"),
    (["add", "a", "--telegram", "--bot-token-secret", "B", "--chat", "x y"], "chat ID"),
    (["add", "a", "--telegram", "--chat", "1"], "--bot-token-secret"),
    (["add", "a", "--whatsapp", "--phone-id", "1234567", "--token-secret", "T", "--recipient", "abc"], "country code"),
    (["add", "a", "--whatsapp", "--phone-id", "1234567", "--token-secret", "T", "--recipient", "+491234567",
      "--language", "de"], "--language goes with --template"),
    (["add", "a", "--ntfy", "t", "--chat", "1"], "takes no --chat"),
    (["add", "a", "--ntfy", "t", "--webhook", "https://h.example"], "give one of"),
    (["add", "a", "--ntfy", "t", "--events", "questions,nope"], "the events are"),
    (["add", "a", "--ntfy", "t", "--quiet", "25:00-07:00"], "quiet hours"),
    (["add", "a", "--ntfy", "t", "--rate", "-1"], "the rate"),
    (["add", "a", "--ntfy", "t", "--token-secret", "bad-name"], "secret's name"),
    (["add", "../x", "--ntfy", "t"], "name is letters"),
    (["link", "http://web.example"], "http only to this machine"),
])
def test_refused(argv, words, capsys):
    assert run(*argv)[0] == 1
    assert words in capsys.readouterr().err
    assert N.load()["channels"] == []


def test_a_file_of_another_mode_or_unknown_fields_is_refused():
    assert run("add", "p", "--ntfy", "topic-long-enough-1234567")[0] == 0
    os.chmod(N.path(), 0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        N.load()
    os.chmod(N.path(), 0o600)
    document = json.loads(N.path().read_text())
    document["channels"][0]["token"] = "x"
    N.path().write_text(json.dumps(document))
    with pytest.raises(ValueError, match="unknown fields token"):
        N.load()


# -- what a message says ----------------------------------------------------------------------------------------------

def test_minimal_says_only_that_something_waits(fake):
    server = fake()
    channel = N.check_channel({"name": "m", "kind": "webhook", "url": server.url, "detail": "minimal"})
    title, text, _ = N.render(channel, [event()], link="https://web.example")
    assert (title, text) == ("sandbox-ai", "a question is waiting on box")
    title, text, _ = N.render(channel, [event(), event(), event("alarms")])
    assert text == "2 questions, 1 alarm on box"  # (not "1 an alarm")
    assert "brisk-otter" not in text and "github" not in text
    assert N.send(channel, [event()], link="https://web.example") is None
    body = server.json()
    assert body["events"] == [] and body["text"] == "sandbox-ai\na question is waiting on box"
    assert "web.example" not in server.requests[0]["body"].decode()


def test_full_adds_the_destination_normal_does_not():
    normal = N.check_channel({"name": "n", "kind": "ntfy", "topic": "t"})
    full = {**normal, "detail": "full"}
    assert "github.com:443" not in N.render(normal, [event()])[1]
    assert "brisk-otter: wants a network connection: github.com:443" in N.render(full, [event()])[1]


def test_text_from_a_sandbox_is_made_harmless():
    nasty = "evil\x1b[31m‮exe.txt​\nsecond line\t<script>`x`" + "a" * 500
    cleaned = N.clean(nasty, 120)
    assert "\x1b" not in cleaned and "‮" not in cleaned and "​" not in cleaned and "\n" not in cleaned
    assert "<" not in cleaned and "`" not in cleaned and len(cleaned) <= 120 and cleaned.endswith("...")
    full = N.check_channel({"name": "n", "kind": "ntfy", "topic": "t", "detail": "full"})
    title, text, _ = N.render(full, [event(run=nasty, what=nasty, detail=nasty)])
    assert all(c.isprintable() for line in text.split("\n") for c in line)
    assert len(text.split("\n")) == 2  # what, and where and when: nothing a sandbox chose adds a line


# -- how often, and when ----------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_the_rate_sums_up_what_follows():
    sent, clock = [], Clock()
    config = {"channels": [N.check_channel({"name": "p", "kind": "ntfy", "topic": "t", "rate": 60, "events": "all"})],
              "link": None}
    d = N.Dispatcher(sender=lambda channel, events, link: sent.append(events), clock=clock, threaded=False)
    assert d.tell("questions", "r1", "q1", config=config) == ["p"]
    assert len(sent) == 1 and len(sent[0]) == 1
    for index in range(5):
        clock.now += 5
        d.tell("questions", f"r{index}", "q", config=config)
    clock.now += 2
    d.tell("safeguards", "r9", "s", config=config)
    assert len(sent) == 1  # held: one message when the minute is over
    d.flush(config["channels"][0], None)
    assert len(sent) == 1  # not yet
    clock.now += 60
    d.flush(config["channels"][0], None)
    assert len(sent) == 2 and [e["event"] for e in sent[1]] == ["questions"] * 5 + ["safeguards"]
    title, text, _ = N.render(config["channels"][0], sent[1])
    assert title == "sandbox-ai: 6 events" and text.startswith("5 questions, 1 safeguard event on box")
    clock.now += 1
    d.tell("alarms", "r1", "a canary went off", config=config)
    assert len(sent) == 3  # an alarm goes at once


def test_held_ones_of_an_ended_process_go_with_the_next_message():
    sent, clock = [], Clock()
    config = {"channels": [N.check_channel({"name": "p", "kind": "ntfy", "topic": "t"})], "link": None}
    first = N.Dispatcher(sender=lambda c, e, l: sent.append(e), clock=clock, threaded=False)
    first.tell("questions", "r", "q", config=config)
    clock.now += 10
    first.tell("questions", "r", "q2", config=config)  # held; this process ends before its summary
    second = N.Dispatcher(sender=lambda c, e, l: sent.append(e), clock=clock, threaded=False)
    clock.now += 100
    second.tell("questions", "r", "q3", config=config)
    assert [len(batch) for batch in sent] == [1, 2]


def test_finish_sends_what_is_held():
    sent, clock = [], Clock()
    config = {"channels": [N.check_channel({"name": "p", "kind": "ntfy", "topic": "t"})], "link": None}
    d = N.Dispatcher(sender=lambda c, e, l: sent.append(e), clock=clock, threaded=False)
    d.tell("questions", "r", "q", config=config)
    clock.now += 1
    d.tell("questions", "r", "q2", config=config)
    d.finish(1, config=config)
    assert [len(batch) for batch in sent] == [1, 1]


def test_quiet_hours_and_away():
    assert N.in_quiet("22:00-07:00", time.mktime((2026, 11, 17, 23, 30, 0, 0, 0, -1)))
    assert N.in_quiet("22:00-07:00", time.mktime((2026, 11, 17, 6, 59, 0, 0, 0, -1)))
    assert not N.in_quiet("22:00-07:00", time.mktime((2026, 11, 17, 7, 0, 0, 0, 0, -1)))
    assert N.in_quiet("09:00-17:00", time.mktime((2026, 11, 17, 12, 0, 0, 0, 0, -1)))
    sent = []
    night = Clock(time.mktime((2026, 11, 17, 23, 30, 0, 0, 0, -1)))
    quiet = N.check_channel({"name": "q", "kind": "ntfy", "topic": "t", "quiet": "22:00-07:00", "rate": 0})
    d = N.Dispatcher(sender=lambda c, e, l: sent.append(e[0]["event"]), clock=night, threaded=False)
    assert d.tell("questions", config={"channels": [quiet]}) == []
    assert d.tell("alarms", config={"channels": [quiet]}) == ["q"]  # alarms go also in quiet hours
    away = N.check_channel({"name": "a", "kind": "ntfy", "topic": "t", "rate": 0})
    always = {**away, "name": "b", "when": "always"}
    watching = N.Dispatcher(sender=lambda c, e, l: sent.append(c["name"]), watched=lambda: True, threaded=False)
    assert watching.tell("questions", config={"channels": [away, always]}) == ["b"]
    assert watching.tell("alarms", config={"channels": [away, always]}) == ["a", "b"]
    assert watching.tell("limits", config={"channels": [away, always]}) == []  # not their events


# -- secrets, failures ------------------------------------------------------------------------------------------------

def test_a_token_never_reaches_the_file_the_log_or_a_message(fake, home):
    # Servers that echo the token they were sent in their error (some do, URL-encoded or not)
    echo = fake(status=401, body=f'{{"error": "bad token {TOKEN}"}}'.encode())
    bot = fake(status=401, body=f'{{"description": "Unauthorized {BOT} /bot{BOT.replace(":", "%3A")}"}}'.encode())
    keep("NTFY_TOKEN", TOKEN)
    keep("TG_BOT", BOT)
    assert run("add", "phone", "--ntfy", f"{echo.url}/t1", "--token-secret", "NTFY_TOKEN")[0] == 0
    assert run("add", "tg", "--telegram", "--bot-token-secret", "TG_BOT", "--chat", "42", "--api", bot.url)[0] == 0
    for name in ("phone", "tg"):
        status, _ = run("test", name)
        assert status == 1
    errors = [N.status_of(name)["error"] for name in ("phone", "tg")]
    assert all("401" in error for error in errors)
    N.LOG.flush()
    log = N.LOG.path().read_text()
    assert "the test message was not sent" in log  # (the scan below reads a log that has the failures)
    files = [N.path().read_text(), *(p.read_text() for p in N.state_dir().glob("*.json")), log, *errors,
             run("list")[1], run("list", "--json")[1]]
    for text in files:
        assert TOKEN not in text and BOT not in text and BOT.split(":")[1] not in text
    for request in echo.requests + bot.requests:  # where it must be: a header, Telegram's path - not the text
        assert TOKEN.encode() not in request["body"] and BOT.encode() not in request["body"]


def test_a_missing_or_locked_secret_says_so_without_asking(monkeypatch):
    assert run("add", "phone", "--ntfy", "https://ntfy.example.org/t", "--token-secret", "NOPE")[1].count(
        "not kept yet") == 1
    channel = N.load()["channels"][0]
    assert "is not kept" in N.send(channel, [event()])

    def locked(name):
        raise ValueError("locked: sandbox-ai secrets unlock")
    assert "cannot be read: locked" in N.send(channel, [event()], secret=locked)


def test_a_server_that_does_not_answer_holds_up_nothing(fake):
    server = fake(hold=True)
    channel = N.check_channel({"name": "slow", "kind": "webhook", "url": server.url, "rate": 0})
    d = N.Dispatcher(sender=lambda c, e, l: N.send(c, e, l, timeout=1))
    began = time.monotonic()
    assert d.tell("questions", "r", "q", config={"channels": [channel]}) == ["slow"]
    assert time.monotonic() - began < 0.5
    assert server.got.wait(5)
    d.finish(5)
    assert "timed out" in (N.status_of("slow") or {}).get("error", "")
    error = N.send(channel, [event()], timeout=1)
    assert error and "not sent" in error


def test_nothing_listening_and_a_redirect_are_failures_not_errors(fake):
    other = fake()
    server = fake(redirect=f"{other.url}/stolen")
    keep("T", TOKEN)
    channel = N.check_channel({"name": "r", "kind": "webhook", "url": server.url, "token_secret": "T"})
    error = N.send(channel, [event()])
    assert "302" in error and other.requests == []  # the token never follows a redirect
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    closed = N.check_channel({"name": "c", "kind": "webhook", "url": f"http://127.0.0.1:{port}"})
    assert N.send(closed, [event()]).startswith("not sent")


# -- end to end -------------------------------------------------------------------------------------------------------

def test_cli_test_through_the_real_entry_point(fake, home):
    server = fake()
    env = {**os.environ}
    status = subprocess.run([str(ROOT / "sandbox-ai"), "notify", "add", "phone", "--ntfy", f"{server.url}/e2e-topic",
                             "--rate", "0"], env=env, capture_output=True, text=True, timeout=60)
    assert status.returncode == 0, status.stderr
    status = subprocess.run([str(ROOT / "sandbox-ai"), "notify", "test", "phone"], env=env, capture_output=True,
                            text=True, timeout=60)
    assert status.returncode == 0 and "phone: sent" in status.stdout, status.stderr
    assert len(server.requests) == 1 and server.json()["topic"] == "e2e-topic"
    assert "a test message of sandbox-ai notify" in server.json()["message"]


def test_a_real_question_reaches_the_fake_ntfy_server_once(fake, home, monkeypatch):
    """A run's approvals directory as the launcher watches it (approvals.watch): a connection question appears;
    nobody watches; the fake ntfy server gets exactly one message, naming the run, without the destination."""
    approvals = load("sandbox_ai_approvals_notify_e2e", "approvals.py")
    monkeypatch.setitem(sys.modules, "sandbox_ai_notify", N)
    monkeypatch.setattr(N, "DISPATCHER", None)
    server = fake()
    assert run("add", "phone", "--ntfy", f"{server.url}/e2e-topic")[0] == 0
    control = home / "run-control"
    directory = control / "approvals"
    directory.mkdir(parents=True)
    (control / "run.json").write_text(json.dumps({"run": "claude-1a2b3c4d", "name": "brisk-otter-lantern",
                                                  "workspace": str(home / "project")}))
    desktop = []
    stop = threading.Event()
    watcher = threading.Thread(target=approvals.watch, args=(directory, stop),
                               kwargs={"notify": lambda title, text: desktop.append(title), "interval": 0.05},
                               daemon=True)
    monkeypatch.setattr(approvals, "watched", lambda now=None: False)
    watcher.start()
    question = {"id": "1800000000-42-1", "kind": "connect", "destination": "140.82.121.4:443", "host": "github.com",
                "protocol": "tcp", "time": "2026-11-17T10:00:00Z"}
    (directory / "1800000000-42-1.json").write_text(json.dumps(question))
    assert server.got.wait(10)
    time.sleep(0.5)
    stop.set()
    watcher.join(5)
    assert len(server.requests) == 1 and desktop  # the desktop notification as before, and one message
    body = server.json()
    assert body["topic"] == "e2e-topic" and body["title"] == "sandbox-ai: questions"
    assert body["message"].startswith("brisk-otter-lantern: wants a network connection\non box at ")
    assert "github" not in body["message"] and "140.82" not in body["message"]


# -- where the events come from ---------------------------------------------------------------------------------------

def test_question_words_never_carry_arguments_or_a_url_path():
    approvals = load("sandbox_ai_approvals_notify_words", "approvals.py")
    words = approvals.phone_words
    assert words({"kind": "connect", "destination": "1.2.3.4:443", "host": "github.com"}) == \
        ("wants a network connection", "github.com (1.2.3.4:443)")
    assert words({"kind": "dns", "name": "evil.example"}) == ("wants a name looked up", "evil.example")
    assert words({"kind": "http", "method": "POST", "url": "https://api.example/v1/x?token=SECRET"}) == \
        ("sent a request a rule holds", "POST api.example")
    assert words({"kind": "browser", "url": "https://login.example/cb?code=SECRET"})[1] == "login.example"
    assert words({"kind": "mcp", "server": "fs", "tool": "write_file", "arguments": "SECRET"}) == \
        ("wants to call an MCP tool", "fs: write_file")
    assert words({"kind": "push", "url": "https://github.com/me/repo.git", "refs": ["main"]})[1] == "github.com"
    assert words({"kind": "nope"}) == ("asks a question", None)
    assert approvals.run_name({"run": "claude-1", "name": "brisk"}) == "brisk"
    assert approvals.run_name({"run": "claude-1"}) == "claude-1" and approvals.run_name({}) is None


def test_safeguards_and_limits_go_to_the_phone_canaries_tell_their_own():
    safeguards = load("sandbox_ai_safeguards_notify", "safeguards.py")
    phoned, shown = [], []
    fake = type("Approvals", (), {"phone": staticmethod(lambda *args, **kw: phoned.append((args, kw))),
                                  "desktop_notification": staticmethod(lambda *args, **kw: shown.append(args))})
    send = safeguards.desktop_sender(fake)
    send({"level": "warning", "title": "t", "text": "x", "kind": "firewall", "run": "brisk", "count": 1,
          "entry": {"target": "evil.example:443", "what": "w"}})
    send({"level": "error", "title": "t", "text": "x", "kind": "oom", "run": "brisk", "count": 3, "entry": {}})
    send({"level": "error", "title": "t", "text": "x", "kind": "canary", "run": "brisk", "count": 1, "entry": {}})
    send({"level": "warning", "title": "t", "text": "x", "kind": "summary", "run": None, "count": 7,
          "kinds": ["dns", "firewall"]})
    assert phoned == [
        (("safeguards", "connection dropped by the firewall", "evil.example:443"), {"run": "brisk"}),
        (("limits", "out of memory: the kernel ended a process (3 times)", None), {"run": "brisk"}),
        (("safeguards", "7 more safeguard events", "dns, firewall"), {"run": None})]
    assert len(shown) == 4  # the desktop as before


def test_a_canary_alarm_goes_to_the_phone(tmp_path, monkeypatch):
    canary = load("sandbox_ai_canary_notify", "canary.py")
    phoned = []
    fake = type("Approvals", (), {
        "phone": staticmethod(lambda *args, **kw: phoned.append((args, kw))),
        "run_name": staticmethod(lambda record: record.get("name")),
        "run_record": staticmethod(lambda directory: {"name": "brisk"}),
        "desktop_notification": staticmethod(lambda *args, **kw: None)})
    plan = canary.Plan.__new__(canary.Plan)
    plan.control_dir, plan.run, plan.notify, plan.counter = tmp_path, "claude-1", lambda *a: None, 0
    monkeypatch.setattr(plan, "approvals", lambda: fake, raising=False)
    plan.notice({"summary": "the decoy ~/.aws/credentials was read", "canary": "aws"}, " The request was refused.",
                "block")
    assert phoned == [(("alarms", "a canary went off: the request was refused", "the decoy ~/.aws/credentials was "
                        "read"), {"run": "brisk"})]


def test_the_web_interface_checks_a_channel_before_anything_reaches_the_host(monkeypatch):
    """The web's notify routes: a channel is checked as the command checks it (a bad one never reaches the host);
    check_only answers without running anything; adding is asked first (notify-add)."""
    web = load("webui_for_notify_test", "webui.py")
    ran = []
    api = web.Api(runner=lambda host, args, timeout=120, input=None: ran.append((host, args)) or (0, "", ""))
    with pytest.raises(web.Refused) as refused:
        api.notify_add({"host": "local"}, {"name": "x", "kind": "webhook", "url": "http://evil.example/in"})
    assert "http only to this machine" in str(refused.value) and ran == []
    assert api.notify_add({"host": "local"}, {"name": "x", "kind": "ntfy", "topic": "t", "check_only": True}) == \
        {"ok": True} and ran == []
    with pytest.raises(web.Refused) as refused:
        api.notify_add({"host": "local"}, {"name": "x", "kind": "ntfy", "topic": "t"})
    assert "send {\"confirm\": true}" in str(refused.value) and ran == []
    with pytest.raises(web.Refused):
        api.notify_remove({"host": "local", "name": "../x"}, {"confirm": True})
    with pytest.raises(web.Refused):
        api.notify_link({"host": "local"}, {"url": "javascript:alert(1)"})
    assert ran == []


def test_parallel_adds_lose_nothing():
    """add and remove are read-modify-write under the file's lock: channels added at once are all kept."""
    threads = [threading.Thread(target=run, args=("add", f"p{index}", "--ntfy", f"topic-long-enough-{index:04d}"))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(c["name"] for c in N.load()["channels"]) == [f"p{index}" for index in range(8)]


def test_a_summary_counts_each_kind_in_words_of_its_own():
    """Review 3x-3: one event of a kind in a summary read "1 a safeguard acted", "1 a run ended" (the minimal
    message's start with a number before it); the help promises "3 questions, 1 safeguard event"."""
    channel = {"detail": "minimal"}
    for kind in N.EVENTS:
        _, text, _ = N.render(channel, [event("questions"), event("questions"), event(kind)], host="box")
        if kind == "questions":
            assert text == "3 questions on box"
            continue
        one = text.split(", ", 1)[1].removesuffix(" on box")
        assert one.startswith("1 ") and not one.startswith(("1 a ", "1 an ")), text
        assert one == f"1 {N.SINGULAR[kind]}" and N.EVENTS[kind][2].startswith(N.SINGULAR[kind].split()[0])
    assert "3 questions, 1 safeguard event" in N.__doc__.replace("\n", " ")


def test_what_was_held_is_not_sent_once_the_quiet_hours_began():
    """Review 3x-3: "Quiet hours hold back everything else; it is not sent later" - but questions held for a summary
    (--rate) shortly before the quiet hours were sent by the summary inside them. Dropped now; an alarm still goes."""
    sent = []
    clock = Clock(time.mktime((2026, 11, 17, 21, 59, 30, 0, 0, -1)))
    config = {"channels": [N.check_channel({"name": "night", "kind": "ntfy", "topic": "t", "rate": 60,
                                            "quiet": "22:00-07:00", "events": "all"})], "link": None}
    d = N.Dispatcher(sender=lambda channel, events, link: sent.append(events), clock=clock, threaded=False)
    d.tell("questions", "r1", "q1", config=config)
    clock.now += 10
    d.tell("questions", "r2", "q2", config=config)  # held for the summary
    assert len(sent) == 1
    clock.now += 60  # 22:00:40: the quiet hours began
    assert d.flush(config["channels"][0], None) is None and len(sent) == 1, "nothing in the quiet hours"
    clock.now = time.mktime((2026, 11, 18, 7, 30, 0, 0, 0, -1))
    d.flush(config["channels"][0], None, final=True)
    assert len(sent) == 1, "nor later"
    d.tell("alarms", "r3", "a canary went off", config=config)
    assert len(sent) == 2
