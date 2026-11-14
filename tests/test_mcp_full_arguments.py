"""The questions about MCP tool calls show the human the call's arguments in full (found by the independent review of
the bluesky server: a long post or thread was approved from its first 300 characters).

The question's line keeps its short form; its details carry the complete arguments - pretty-printed JSON of exactly
the value the relay passes on, credentials redacted, bounded at 64 KiB with a marker beyond - and every reader shows
them: `approvals list --json` ("details"), `approvals show ID`, a desktop notification that cannot show them whole
offers no Allow. An answer stays bound to the call its question showed: a call with other arguments, made while
one is asked, gets a question of its own."""
import io
import json
from pathlib import Path
import threading
import time

import pytest

import test_rules as tr

rt, approvals = tr.rt, tr.approvals

POST = {"repo": "me.bsky.social", "text": "Erster Teil des Threads: " + "lang " * 400 + "- ENDE, der letzte Satz",
        "reply": {"root": {"uri": "at://x/app.bsky.feed.post/1", "cid": "bafy"}}, "langs": ["de"]}


def test_full_arguments_are_the_exact_value_pretty_printed():
    args = {"text": "Zeile 1\nZeile 2 äöü \"quoted\"", "n": 3, "f": 0.1, "big": 2 ** 60, "list": [1, None, True],
            "nested": {"z": 1, "a": 2}}
    text = rt.full_arguments(args)
    assert json.loads(text) == args and list(json.loads(text)["nested"]) == ["z", "a"]  # the order as sent
    assert text.startswith('{\n  "text": ') and "äöü" in text  # readable, one key per line
    assert rt.full_arguments({}) == "{}"
    # A lone surrogate (JSON allows "\ud800") stays its escape: the text is still the same JSON value.
    assert json.loads(rt.full_arguments({"s": "a\ud800b"})) == {"s": "a\ud800b"}


def test_full_arguments_redact_credentials():
    text = rt.full_arguments({"api_key": "k-123456789", "text": "hello", "headers": {"Authorization": "Bearer abcdefghijkl"}})
    assert "k-123456789" not in text and "abcdefghijkl" not in text and '"[redacted]"' in text and "hello" in text


def test_long_arguments_are_cut_with_a_marker_never_silently():
    args = {"text": "x" * (200 << 10)}
    text = rt.full_arguments(args)
    whole = json.dumps(args, indent=2, ensure_ascii=False).encode()
    kept, _, marker = text.rpartition("\n... truncated: ")
    assert len(kept.encode()) == rt.FULL_ARGUMENTS_LIMIT and whole.startswith(kept.encode())
    more = int(marker.split(" ", 1)[0])
    assert more == len(whole) - len(kept.encode()) and "more bytes" in marker
    # A credential across the cut is redacted whole, not shown up to the cut.
    secret = "s3cr3t-" + "v" * 40
    cut = {"pad": "p" * (rt.FULL_ARGUMENTS_LIMIT - 40), "client_secret": secret}
    assert "s3cr3t" not in rt.full_arguments(cut)
    # Just at the limit: nothing cut, no marker.
    exact = {"t": "y" * (rt.FULL_ARGUMENTS_LIMIT - 13)}
    assert len(json.dumps(exact, indent=2).encode()) == rt.FULL_ARGUMENTS_LIMIT
    assert "truncated" not in rt.full_arguments(exact) and json.loads(rt.full_arguments(exact)) == exact


def asks_of(relay):
    return [json.loads(line.split(" ", 1)[1]) for line in relay.out.getvalue().splitlines()
            if line.startswith("sandbox-ai-mcp-ask ")]


def test_a_long_post_is_asked_with_all_of_it_and_sent_as_shown(tmp_path):
    """The relay's own questions (an image server's, through the log reader): the question's details hold the whole
    post; what the server then gets is exactly that value."""
    answers = tmp_path / "approvals"
    answers.mkdir()
    rules = {"version": 1, "mcp": [tr.rule("posts", "ask", server="files", tool="delete_file")]}
    relay = tr.Relay({"server": "files", "rules": [tr.checked(rules, "rules.json")], "approvals": str(answers),
                      "timeout": 20})

    def answer():
        for _ in range(500):
            asks = asks_of(relay)
            if asks:
                (answers / f"{asks[0]['id']}.answer").write_text(json.dumps({"decision": "allow", "scope": "once"}))
                return
            time.sleep(0.02)
    try:
        threading.Thread(target=answer, daemon=True).start()
        reply = relay.session(tr.call("delete_file", POST))[0]
        assert reply["result"]["isError"] is False
        question = asks_of(relay)[0]
        assert "ENDE, der letzte Satz" not in question["arguments"]  # the short form is shortened ...
        assert json.loads(question["arguments_full"]) == POST  # ... the details are the call, whole
        sent = json.loads(relay.ran_file.read_text().splitlines()[0])
        assert sent["arguments"] == json.loads(question["arguments_full"])
        # The host shows them: the line says it is shortened and where the rest is; the details are the whole.
        question = {**question, "server": "files"}
        assert not approvals.arguments_whole(question)
        assert f"(arguments shortened here: the question's details show them in full, sandbox-ai approvals show {question['id']}) with" \
            in approvals.describe(question)
        assert json.loads(approvals.question_details(question)) == POST
    finally:
        relay.close()


def test_the_broker_binds_an_answer_to_the_call_it_showed(tmp_path):
    """Two calls of one tool with other arguments while the first is asked: each gets its question; an "allow once"
    of the first never lets the second through."""
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda *_: None), timeout=30, poll=0.02)
    rules = {"version": 1, "mcp": [tr.rule("posts", "ask", server="bsky", tool="post")]}
    gateway = rt.McpGateway({"server": "bsky", "rules": [tr.checked(rules, "rules.json")]}, out=io.StringIO(),
                            asker=broker.ask)
    results = {}

    def call(name, arguments):
        results[name] = gateway.decide("post", arguments, name)
    first = threading.Thread(target=call, args=("harmless", {"text": "hello"}), daemon=True)
    first.start()

    def files(count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = sorted((tmp_path / "a").glob("*.json"))
            if len(found) >= count:
                return [json.loads(path.read_text()) for path in found]
            time.sleep(0.02)
        raise AssertionError(f"not {count} questions")
    harmless = files(1)[0]
    second = threading.Thread(target=call, args=("other", {"text": "delete my account"}), daemon=True)
    second.start()
    questions = files(2)
    other = next(q for q in questions if q["id"] != harmless["id"])
    assert json.loads(harmless["arguments_full"]) == {"text": "hello"}
    assert json.loads(other["arguments_full"]) == {"text": "delete my account"}
    assert harmless["call_digest"] != other["call_digest"] and "hello" not in harmless["call_digest"]
    (tmp_path / "a" / f"{harmless['id']}.answer").write_text(json.dumps({"decision": "allow", "scope": "once"}))
    first.join(10)
    assert results["harmless"][0] is True
    time.sleep(0.2)
    assert "other" not in results  # still asked: the first answer was not its
    (tmp_path / "a" / f"{other['id']}.answer").write_text(json.dumps({"decision": "deny", "scope": "once"}))
    second.join(10)
    assert results["other"][0] is False
    # The same call again while asked waits for the same question (one question per call, not per repeat).
    assert rt.ApprovalBroker.slot("mcp", {"call_digest": "d"}, ("mcp", "k")) == ("mcp", "k", "d")
    assert rt.ApprovalBroker.slot("dns", {"call_digest": "d"}, ("dns", "k")) == ("dns", "k")


def test_an_answer_for_the_run_still_covers_the_tool(tmp_path):
    """Allow for the run means the tool for the run (as before): later calls with other arguments are not asked."""
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda *_: None), timeout=30, poll=0.02)
    rules = {"version": 1, "mcp": [tr.rule("posts", "ask", server="bsky", tool="post")]}
    gateway = rt.McpGateway({"server": "bsky", "rules": [tr.checked(rules, "rules.json")]}, out=io.StringIO(),
                            asker=broker.ask)
    result = []
    thread = threading.Thread(target=lambda: result.append(gateway.decide("post", {"text": "a"}, 1)), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not list((tmp_path / "a").glob("*.json")) and time.monotonic() < deadline:
        time.sleep(0.02)
    ident = json.loads(next((tmp_path / "a").glob("*.json")).read_text())["id"]
    (tmp_path / "a" / f"{ident}.answer").write_text(json.dumps({"decision": "allow", "scope": "run"}))
    thread.join(10)
    assert result[0][0] is True and gateway.decide("post", {"text": "b"}, 2)[0] is True
    assert len(list((tmp_path / "a").glob("*.json"))) == 1
    # A question with its 64 KiB of details is still one the control op reads back (allow|deny|ask-again ID).
    big = tmp_path / "a" / "1-2-3.json"
    big.write_text(json.dumps({"id": "1-2-3", "kind": "mcp", "server": "bsky", "tool": "post", "rule": "posts",
                               "ruleset": "rules.json", "arguments_full": rt.full_arguments({"t": "ä" * 70000})}))
    assert big.stat().st_size > 1 << 16
    assert broker.question_of("1-2-3")[0] == "mcp"


def question(**fields):
    return {"id": "1700000000-1-2", "kind": "mcp", "time": "2026-11-13T10:00:00Z", "timeout": 60, "server": "bsky",
            "tool": "post", "rule": "posts", "ruleset": "rules.json", **fields}


def test_details_are_shown_escaped_with_their_lines():
    full = rt.full_arguments({"text": "a‮b​c", "x": 1})
    shown = approvals.question_details(question(arguments=json.dumps({"text": "a"}), arguments_full=full))
    assert "\n" in shown and "‮" not in shown and "​" not in shown and "\\u202e" in shown
    assert json.loads(shown) == {"text": "a‮b​c", "x": 1}  # escaped, still the same JSON value
    # Forged by the sandbox: control characters and more than the limit are no way around it.
    forged = approvals.question_details(question(arguments_full="\x1b[2J" + "z" * (approvals.DETAILS_LIMIT + 10)))
    assert "\x1b" not in forged and forged.endswith("\n... truncated here: 14 more bytes")
    assert approvals.question_details(question(kind="dns", arguments_full="x")) == ""
    assert approvals.question_details(question()) == "" and approvals.arguments_whole(question())  # an older sandbox's
    short = {"text": "hi"}
    whole = question(arguments=json.dumps(short), arguments_full=rt.full_arguments(short))
    assert approvals.arguments_whole(whole) and "shortened" not in approvals.describe(whole)


@pytest.fixture
def running(tmp_path, monkeypatch):
    """A running sandbox with one open question about a long post and one about a short call."""
    run = tmp_path / "run"
    (run / "approvals").mkdir(parents=True)
    long = question(arguments=rt.McpGateway.arguments(POST), arguments_full=rt.full_arguments(POST))
    short = question(id="1700000000-1-3", arguments='{"text": "hi"}', arguments_full=rt.full_arguments({"text": "hi"}))
    for item in (long, short):
        (run / "approvals" / f"{item['id']}.json").write_text(json.dumps(item))
    runs = {"r1": {"directory": str(run), "run": "r1", "workspace": str(tmp_path)}}
    monkeypatch.setattr(approvals, "questions", lambda runs_=None, answered=False, real=approvals.questions:
                        real(runs, answered=answered))
    monkeypatch.setattr(approvals, "policy_scope", lambda: "always")
    return long, short


def test_list_json_and_show_carry_the_arguments_in_full(running, capsys):
    long, short = running
    lines = []
    assert approvals.main(["list", "--json"], out=lines.append) == 0
    listed = {q["id"]: q for q in json.loads(lines[0])}
    assert json.loads(listed[long["id"]]["details"]) == POST and listed[long["id"]]["details_whole"] is False
    assert listed[short["id"]]["details_whole"] is True
    lines = []
    assert approvals.main(["show", long["id"]], out=lines.append) == 0
    text = "\n".join(lines)
    assert "ENDE, der letzte Satz" in text and "arguments in full, as the call will send them" in text
    assert f"answer: sandbox-ai approvals allow|deny {long['id']}" in text
    lines = []
    assert approvals.main(["list"], out=lines.append) == 0
    assert any("sandbox-ai approvals show ID" in line for line in lines)
    assert approvals.main(["show", "9-9-9"], out=lines.append) == 1
    assert "no question 9-9-9" in capsys.readouterr().err
    assert approvals.main(["show", "--", "-x"], out=lines.append) == 1


def test_a_notification_offers_no_allow_for_arguments_it_cannot_show(running, monkeypatch):
    long, short = running
    monkeypatch.setattr(approvals, "shows", lambda alarm=False: True)
    commands = []

    def runner(command, **_):
        commands.append(command)
        return type("Done", (), {"stdout": ""})()
    approvals.desktop_question("t", approvals.describe(long), long["id"], runner=runner, question=long, record={})
    approvals.desktop_question("t", approvals.describe(short), short["id"], runner=runner, question=short, record={})
    long_actions = [a for a in commands[0] if a.startswith("--action=")]
    short_actions = [a for a in commands[1] if a.startswith("--action=")]
    assert long_actions and not any("Allow" in a for a in long_actions)
    assert f"sandbox-ai approvals show {long['id']}" in commands[0][-1]
    assert any("Allow" in a for a in short_actions)
    # Without buttons: the notification says where the rest is.
    told = []
    stop = threading.Event()
    monkeypatch.setattr(approvals, "watched", lambda now=None: False)
    thread = threading.Thread(target=approvals.watch, args=(running_dir(long), stop), daemon=True,
                              kwargs={"notify": lambda title, text: told.append(text), "interval": 0.02})
    thread.start()
    deadline = time.monotonic() + 5
    while len(told) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(5)
    by_id = {next(i for i in (long["id"], short["id"]) if i in text): text for text in told}
    assert f"in full: sandbox-ai approvals show {long['id']}" in by_id[long["id"]]
    assert "approvals show" not in by_id[short["id"]]


def running_dir(item):
    """The approvals directory the fixture wrote ITEM into."""
    for _, record, found in approvals.questions():
        if found["id"] == item["id"]:
            return Path(record["directory"]) / "approvals"
    raise AssertionError("not written")
