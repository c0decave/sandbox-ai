"""The questions about the agent's own tools (kind "tool": a rule says ask for Bash, Edit, Write ...) show the human
the call's arguments in full, as the MCP calls' do (tests/test_mcp_full_arguments.py): a long Bash command or file
content was approved from its first 300 characters, and a question open for one call answered another call of the
same tool with other arguments.

The question's line keeps its short form; its details ("arguments_full") carry the arguments exactly as the agent
will run them - pretty-printed JSON computed by the hook from the agent's own message, credentials and the run's
secret values redacted - and every reader shows them (approvals list --json, approvals show ID, the app, the web, a
notification without Allow when it cannot show them). A call larger than a question shows in full (64 KiB) is
refused without asking, never shown cut. A pending question - and the refusal while nobody answers it - is bound to
a keyed digest of the exact arguments: a call with other arguments made meanwhile gets a question of its own."""
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time

import pytest

import test_hooks as th

rt, ap = th.rt, th.ap

ASK_BASH = {"version": 1, "tool": [{"id": "bash-asks", "tool": ["Bash", "Write"], "decision": "ask"}]}
LONG = "echo start && " + "true && " * 120 + "rm -rf /work/important # the end of the command"


@pytest.fixture
def asked(tmp_path):
    """A HookServer whose ask rule goes to a real ApprovalBroker (questions in DIRECTORY/a), on a real socket."""
    directory = Path(tempfile.mkdtemp(prefix="hooks-", dir="/tmp"))
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda *_: None), timeout=30, poll=0.02)
    stop = threading.Event()
    thread = rt.start_hook_server({"agent": "claude", "rules": [{"name": "rules.json", **ASK_BASH}]}, broker,
                                  rt.Taint(), ["sk-secret-value-1"], path=directory / "decide.sock",
                                  log=tmp_path / "tools.jsonl", stop=stop)
    yield {"path": directory / "decide.sock", "questions": tmp_path / "a", "broker": broker}
    stop.set()
    thread.join(5)
    shutil.rmtree(directory, ignore_errors=True)


def questions_in(directory, count, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = sorted(directory.glob("*.json"))
        if len(found) >= count:
            return [json.loads(path.read_text()) for path in found]
        time.sleep(0.02)
    raise AssertionError(f"not {count} questions")


def answer(directory, ident, decision="allow", scope="once"):
    (directory / f"{ident}.answer").write_text(json.dumps({"decision": decision, "scope": scope}))


def call_in_thread(asked, results, name, message, agent="claude"):
    def run():
        results[name] = th.run_hook(asked, agent, "PreToolUse", message, timeout=40)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_long_command_is_asked_with_all_of_it(asked):
    results = {}
    thread = call_in_thread(asked, results, "long", th.claude("Bash", command=LONG + " sk-secret-value-1",
                                                             description="clean up"))
    question = questions_in(asked["questions"], 1)[0]
    assert question["kind"] == "tool" and question["tool"] == "Bash"
    full = json.loads(question["arguments_full"])
    assert full == {"command": LONG + " [redacted]", "description": "clean up"}  # the whole command, as it runs
    assert "sk-secret-value-1" not in json.dumps(question)
    assert "the end of the command" in ap.question_details(question)
    assert not ap.arguments_whole(question)
    assert f"(arguments shortened here: the question's details show them in full, sandbox-ai approvals show " \
           f"{question['id']}) with" in ap.describe(question)
    answer(asked["questions"], question["id"])
    thread.join(20)
    assert results["long"][0] == 0


def test_the_answer_is_bound_to_the_call_its_question_showed(asked):
    """Two Bash calls with other arguments while the first is asked: each gets its question; "allow once" for the
    harmless one never lets the other run (before, the second waited for the first's question and took its answer)."""
    results = {}
    first = call_in_thread(asked, results, "harmless", th.claude("Bash", command="ls"))
    harmless = questions_in(asked["questions"], 1)[0]
    second = call_in_thread(asked, results, "other", th.claude("Bash", command="rm -rf /work"))
    found = questions_in(asked["questions"], 2)
    other = next(q for q in found if q["id"] != harmless["id"])
    assert json.loads(harmless["arguments_full"]) == {"command": "ls"}
    assert json.loads(other["arguments_full"]) == {"command": "rm -rf /work"}
    assert harmless["call_digest"] != other["call_digest"] and "ls" not in harmless["call_digest"]
    answer(asked["questions"], harmless["id"])
    first.join(20)
    assert results["harmless"][0] == 0
    time.sleep(0.3)
    assert "other" not in results  # still asked: the first answer was not its
    answer(asked["questions"], other["id"], "deny")
    second.join(20)
    assert results["other"][0] == 2


def test_the_same_call_again_waits_for_the_same_question(asked):
    results = {}
    one = call_in_thread(asked, results, "one", th.claude("Bash", command="make"))
    questions_in(asked["questions"], 1)
    two = call_in_thread(asked, results, "two", th.claude("Bash", command="make"))
    time.sleep(0.3)
    found = questions_in(asked["questions"], 1)
    assert len(found) == 1
    answer(asked["questions"], found[0]["id"])
    one.join(20)
    two.join(20)
    assert results["one"][0] == 0 and results["two"][0] == 0


def test_an_answer_for_the_run_still_covers_the_tool(asked):
    results = {}
    thread = call_in_thread(asked, results, "a", th.claude("Bash", command="make a"))
    question = questions_in(asked["questions"], 1)[0]
    answer(asked["questions"], question["id"], scope="run")
    thread.join(20)
    assert results["a"][0] == 0
    assert th.run_hook(asked, "claude", "PreToolUse", th.claude("Bash", command="make b"))[0] == 0
    assert len(list(asked["questions"].glob("*.json"))) == 1


def test_a_call_too_large_to_show_is_refused_not_asked(asked):
    """A call whose arguments are more than a question shows in full (64 KiB) is refused at once, never asked: an
    "Allow once" would allow what the human never saw (here a command whose rm -rf comes after 64 KiB of harmless
    text; before, its question showed the first 64 KiB and a "truncated" line). A string over HOOK_VALUE_LIMIT (the
    rules know only its length) is refused the same way."""
    hidden = "echo ok && " + "true && " * ((rt.FULL_ARGUMENTS_LIMIT // 8) + 10) + "rm -rf /work/important"
    status, err = th.run_hook(asked, "claude", "PreToolUse", th.claude("Bash", command=hidden))
    assert status == 2 and "64 KiB a question shows in full: refused without asking" in err
    big = "A" * (rt.HOOK_VALUE_LIMIT + 10)
    status, err = th.run_hook(asked, "claude", "PreToolUse", th.claude("Write", file_path="/work/x", content=big))
    assert status == 2 and "refused without asking" in err
    assert not list(asked["questions"].glob("*.json"))  # nothing was asked
    records = [json.loads(line) for line in (asked["questions"].parent / "tools.jsonl").read_text().splitlines()]
    assert [r["decision"] for r in records] == ["ask", "ask"] and all("refused without asking" in r["reason"]
                                                                      for r in records)
    # One just at the bound is asked, and shown whole.
    fits = {"command": "x" * (rt.FULL_ARGUMENTS_LIMIT - 20)}
    assert len(json.dumps(fits, indent=2).encode()) <= rt.FULL_ARGUMENTS_LIMIT
    results = {}
    thread = call_in_thread(asked, results, "fits", th.claude("Bash", **fits))
    question = questions_in(asked["questions"], 1)[0]
    assert json.loads(question["arguments_full"]) == fits and "truncated" not in question["arguments_full"]
    answer(asked["questions"], question["id"], scope="run")
    thread.join(20)
    assert results["fits"][0] == 0
    # The human allowed Bash for the run: its scope covers a call too large to show as well (as before).
    assert th.run_hook(asked, "claude", "PreToolUse", th.claude("Bash", command=hidden))[0] == 0


def test_a_forged_window_cannot_hide_a_value_too_large():
    """A window too long to take (sent to the socket by hand) falls back to the arguments as the rules see them; one
    whose value they know only by its length cannot be shown, so it is refused rather than asked as a placeholder."""
    server = rt.HookServer(th.ruleset(ASK_BASH), th.Broker("allow"))
    message = {"agent": "claude", "event": "PreToolUse", "tool": "Bash",
               "input": {"command": {"sandbox-ai": "too large", "length": 300000}},
               "input_window": "x" * (rt.FULL_ARGUMENTS_LIMIT * 2), "input_size": 3}
    full, _, more = server.call(message, message["input"])
    assert more >= 300000 and "truncated: " in full
    server.decide(message)
    assert server.broker.asked[-1][1]["too_large"] == more


def test_the_broker_refuses_a_too_large_call_but_keeps_the_run_scope(tmp_path):
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda *_: None), timeout=5, poll=0.02)
    details = {"agent": "claude", "tool": "Bash", "rule": "r", "ruleset": "rules.json", "arguments": "{}",
               "arguments_full": "{}", "call_digest": "1" * 32, "too_large": 5}
    for kind in ("tool", "mcp"):
        answer_ = broker.ask(kind, {**details, "server": "s"})
        assert answer_["decision"] == "deny" and "refused without asking" in answer_["reason"]
    assert not list((tmp_path / "a").glob("*.json"))
    broker.decided[broker.key("tool", details)] = {"decision": "allow", "scope": "run", "reason": "answered"}
    assert broker.ask("tool", details)["decision"] == "allow"


def test_an_mcp_call_too_large_to_show_is_refused(tmp_path):
    asked = []
    gateway = rt.McpGateway({"server": "files", "rules": []}, out=open("/dev/null", "w"),
                            asker=lambda kind, details: asked.append(details) or
                            ({"decision": "deny", "reason": too_large_reason(details)}))
    verdict = {"decision": "ask", "rule": "r", "ruleset": "rules.json", "reason": "asks"}
    big = {"text": "harmless " * (rt.FULL_ARGUMENTS_LIMIT // 9) + "and then the tail"}
    gateway.ask("write", "{}", verdict, full=big)
    assert asked[-1]["too_large"] > 0 and "the tail" not in asked[-1]["arguments_full"]
    gateway.ask("write", "{}", verdict, full={"text": "small"})
    assert "too_large" not in asked[-1]
    # Without the run's broker (a server's own log reader asks): refused at once, nothing emitted.
    alone = rt.McpGateway({"server": "files", "rules": [], "approvals": str(tmp_path)}, out=open("/dev/null", "w"))
    refused = alone.ask("write", "{}", verdict, full=big)
    assert refused["decision"] == "deny" and "refused without asking" in refused["reason"] and alone.asked == 0


def too_large_reason(details):
    return rt.too_large_refusal(details["too_large"]) if details.get("too_large") else "answered"


def test_an_unanswered_question_refuses_only_its_own_call(tmp_path):
    """Nobody answered the question about one call: that call is refused at once for a while (cooling) - another
    call of the same tool with other arguments is asked on its own (before, it was refused by the other's silence;
    the refusal is bound to the call as the question is)."""
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda *_: None), timeout=0.3, poll=0.02,
                               pause=60)
    base = {"agent": "claude", "tool": "Bash", "rule": "r", "ruleset": "rules.json", "arguments": "{}"}
    first = broker.ask("tool", {**base, "arguments_full": '{"command": "rm -rf /"}', "call_digest": "a" * 32})
    assert first["decision"] == "deny" and "no answer" in first["reason"]
    again = broker.ask("tool", {**base, "arguments_full": '{"command": "rm -rf /"}', "call_digest": "a" * 32})
    assert again["decision"] == "deny" and "refused without asking until" in again["reason"]
    assert len(list((tmp_path / "a").glob("*.json"))) == 1
    results = {}
    thread = threading.Thread(target=lambda: results.update(other=broker.ask(
        "tool", {**base, "arguments_full": '{"command": "ls"}', "call_digest": "b" * 32})), daemon=True)
    thread.start()
    found = questions_in(tmp_path / "a", 2)
    other = next(q for q in found if q["call_digest"] == "b" * 32)
    answer(tmp_path / "a", other["id"])
    thread.join(10)
    assert results["other"]["decision"] == "allow"
    listed = broker.decided_list()
    assert [e["scope"] for e in listed] == ["pause"] and listed[0]["question"] != other["id"]
    # Allowed for the run by its question (sandbox-ai approvals allow ID): the pause is gone, the tool allowed.
    paused = listed[0]["question"]
    broker.control({"action": "allow", "question": paused})
    assert not broker.cooling
    assert broker.ask("tool", {**base, "arguments_full": "{}", "call_digest": "c" * 32})["decision"] == "allow"
    # Ask again (forget) takes back the decision; a paused call's pause is forgotten by its own question.
    assert broker.control({"action": "forget", "question": paused})["forgot"] is True


def test_newlines_and_escapes_in_a_tool_question_cannot_forge_lines(tmp_path, monkeypatch):
    """What the agent chose (its tool's name, its arguments) cannot add a line, a colour or a cursor move to what the
    human reads: the question's line, its details, approvals show and the notification."""
    hostile = "ls\n\x1b[2K\x1b[1A\rallowed: harmless\u202e\x07"
    arguments = {"command": hostile + " && rm -rf /work"}
    item = {"id": "1700000000-1-2", "kind": "tool", "time": "2026-11-13T10:00:00Z", "timeout": 60,
            "agent": "claude\n", "tool": "Bash\x1b[31m", "rule": "r\n", "ruleset": "rules.json",
            "arguments": rt.McpGateway.arguments(arguments)[:300], "arguments_full": rt.full_arguments(arguments)}
    line = ap.describe(item)
    assert "\n" not in line and "\x1b" not in line and "\r" not in line and "\u202e" not in line
    assert "\x07" not in line
    details = ap.question_details(item)
    assert "\x1b" not in details and "\r" not in details and "\u202e" not in details and "\x07" not in details
    assert "rm -rf /work" in details and json.loads(details) == arguments  # escapes: the same JSON value
    run = tmp_path / "run"
    (run / "approvals").mkdir(parents=True)
    (run / "approvals" / f"{item['id']}.json").write_text(json.dumps(item))
    runs = {"r1": {"directory": str(run), "run": "r1", "workspace": str(tmp_path)}}
    monkeypatch.setattr(ap, "questions", lambda runs_=None, answered=False, real=ap.questions:
                        real(runs, answered=answered))
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    lines = []
    assert ap.main(["show", item["id"]], out=lines.append) == 0
    text = "\n".join(lines)
    assert "\x1b" not in text and "\r" not in text and "\u202e" not in text and "rm -rf /work" in text
    monkeypatch.setattr(ap, "shows", lambda alarm=False: True)
    commands = []

    def runner(command, **_):
        commands.append(command)
        return type("Done", (), {"stdout": ""})()
    ap.desktop_question("t", ap.describe(item), item["id"], runner=runner, question=item, record={})
    body = commands[0][-1]
    assert "\x1b" not in body and "\r" not in body and "\u202e" not in body and "\x07" not in body
    assert body.split("\n")[0] == html_escaped(ap.describe(item)[:1000])  # the question itself: one line


def html_escaped(text):
    import html
    return html.escape(text, quote=False)


def test_opencode_shows_its_own_arguments():
    """What OpenCode runs is its own message (filePath, not the rules' file_path)."""
    server = rt.HookServer(th.ruleset(ASK_BASH), th.Broker("deny"))
    window, size = rt.arguments_window({"filePath": "/work/x", "content": "y"})
    server.decide({"agent": "opencode", "event": "PreToolUse", "tool": "Write",
                   "input": {"file_path": "/work/x", "content": "y"}, "input_window": window, "input_size": size,
                   "input_digest": "d"})
    details = server.broker.asked[-1][1]
    assert json.loads(details["arguments_full"]) == {"filePath": "/work/x", "content": "y"}


def test_a_forged_window_is_another_call():
    """Sent to the socket by hand: a window that differs from the input names another call (its own digest), and a
    window that is too long or no text is not taken (the input's own is shown)."""
    server = rt.HookServer(th.ruleset(ASK_BASH), th.Broker("deny"))
    base = {"agent": "claude", "event": "PreToolUse", "tool": "Bash", "input": {"command": "ls"}}
    digests = []
    for window in ('{\n  "command": "ls"\n}', '{\n  "command": "harmless"\n}'):
        server.decide({**base, "input_window": window, "input_size": len(window), "input_digest": "x"})
        digests.append(server.broker.asked[-1][1]["call_digest"])
    assert digests[0] != digests[1]
    for bad in ({"input_window": 5, "input_size": 1}, {"input_window": "x" * (rt.FULL_ARGUMENTS_LIMIT * 2),
                                                        "input_size": 3}, {}):
        server.decide({**base, **bad})
        assert json.loads(server.broker.asked[-1][1]["arguments_full"]) == {"command": "ls"}


def test_the_hook_client_sends_the_exact_arguments_window():
    sent = []

    class Recording(rt.HookServer):
        def decide(self, message):
            sent.append(message)
            return {"decision": "allow"}
    directory = Path(tempfile.mkdtemp(prefix="hooks-", dir="/tmp"))
    server, stop = Recording(th.ruleset(ASK_BASH)), threading.Event()
    thread = threading.Thread(target=server.serve, args=(directory / "decide.sock", stop), daemon=True)
    thread.start()
    server.ready.wait(5)
    try:
        message = th.claude("Bash", command="x" * (rt.HOOK_VALUE_LIMIT + 1))
        assert th.run_hook({"path": directory / "decide.sock"}, "claude", "PreToolUse", message)[0] == 0
    finally:
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)
    request = sent[0]
    assert request["input"]["command"] == {"sandbox-ai": "too large", "length": rt.HOOK_VALUE_LIMIT + 1}
    assert request["input_window"].startswith('{\n  "command": "xxxx')
    assert len(request["input_window"].encode()) <= rt.FULL_ARGUMENTS_LIMIT + rt.FULL_ARGUMENTS_MARGIN
    assert request["input_size"] == len(json.dumps(message["tool_input"], indent=2).encode())
    assert len(request["input_digest"]) == 64


def test_details_list_and_show_for_a_tool_question(tmp_path, monkeypatch):
    run = tmp_path / "run"
    (run / "approvals").mkdir(parents=True)
    arguments = {"command": LONG}
    item = {"id": "1700000000-1-2", "kind": "tool", "time": "2026-11-13T10:00:00Z", "timeout": 60, "agent": "claude",
            "tool": "Bash", "rule": "bash-asks", "ruleset": "rules.json",
            "arguments": rt.McpGateway.arguments(arguments)[:300], "arguments_full": rt.full_arguments(arguments)}
    (run / "approvals" / f"{item['id']}.json").write_text(json.dumps(item))
    runs = {"r1": {"directory": str(run), "run": "r1", "workspace": str(tmp_path)}}
    monkeypatch.setattr(ap, "questions", lambda runs_=None, answered=False, real=ap.questions:
                        real(runs, answered=answered))
    monkeypatch.setattr(ap, "policy_scope", lambda: "always")
    lines = []
    assert ap.main(["list", "--json"], out=lines.append) == 0
    listed = json.loads(lines[0])[0]
    assert json.loads(listed["details"]) == arguments and listed["details_whole"] is False
    lines = []
    assert ap.main(["show", item["id"]], out=lines.append) == 0
    assert "the end of the command" in "\n".join(lines)
    # A notification that cannot show them whole offers no Allow.
    monkeypatch.setattr(ap, "shows", lambda alarm=False: True)
    commands = []

    def runner(command, **_):
        commands.append(command)
        return type("Done", (), {"stdout": ""})()
    ap.desktop_question("t", ap.describe(item), item["id"], runner=runner, question=item, record={})
    assert not any("Allow" in a for a in commands[0] if a.startswith("--action="))


def test_a_refused_question_with_its_arguments_in_full_is_still_listed(tmp_path):
    """`approvals blocked` (the app's Blocked list) read a question file up to 64 KiB: one with 64 KiB of arguments
    in full did not parse and its refusal was missing."""
    run = tmp_path / "run"
    (run / "approvals").mkdir(parents=True)
    arguments = {"content": "ä" * 70000}
    item = {"id": "1700000000-1-2", "kind": "tool", "time": "2026-11-13T10:00:00Z", "agent": "claude", "tool": "Write",
            "rule": "writes", "ruleset": "rules.json", "arguments": "{}", "arguments_full": rt.full_arguments(arguments)}
    (run / "approvals" / f"{item['id']}.json").write_text(json.dumps(item))
    assert (run / "approvals" / f"{item['id']}.json").stat().st_size > 1 << 16
    (run / "approvals" / f"{item['id']}.done").write_text(json.dumps({"decision": "deny", "reason": "answered"}))
    found = ap.blocked_of_run("r1", {"directory": str(run)})
    assert any("use its tool Write" in json.dumps(entry) for entry in found)
