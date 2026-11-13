"""Performance round 1 of 0.1.94 (the core): what was made faster gives what it gave before. The proxy's redaction of
every body it logs (patterns skipped where they cannot match, the names pattern tried only before a "="), a log's
lines parsed without json.loads's look for other encodings, a report's texts scrubbed once each, the Blocked list's
questions and canary names read once, the MCP catalogs downloaded at once."""
import importlib.util
import json
import random
import re
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_test_performance_core", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime")
tails = load("jsonltail")


# -- the proxy's redaction --

# The names pattern as it was (SECRET_TEXT[1] without its lookahead): every match the same.
NAMES_BEFORE = re.compile(r"(\b(?:[a-z_]*token|secret|client_secret|password|api_?key|code|code_verifier|signature|sig)"
                          r"=)[^&\s\"']+", re.I)
PIECES = ["token", "TOKEN", "Token", "secret", "client_secret", "password", "api_key", "apikey", "APIKEY", "code",
          "code_verifier", "signature", "sig", "SIG", "=", "==", "&", " ", "\t", "\n", '"', "'", "_", "x", "ab", "9",
          "-", ".", "\u017f", "\u212a", "\u0130", "\u0131", "\u00e9", "\u00df", "access_token", "my_token", "?", "/",
          "\u017fecret", "toKen", "_token", "tok", "en", "y", "api", "://", "@", "user:pw", "-u", "--user", "Bearer ",
          "abcdefghijk", "sk-" + "a" * 20, "https://", "ghp_" + "b" * 24,
          # round 2: every pattern has a need (a quote and a colon, a white space, a "-", a token form's start)
          ":", '"password"', '": "', "\u00a0", "\u2003", "\x1c", "\x0b", "\r", "github_pat_" + "c" * 22,
          "glpat-" + "d" * 18, "xoxb-" + "e" * 12, "AKIA" + "F" * 16, "eyJ" + "g" * 9 + ".h" + "h" * 9 + ".i", "--token",
          "BASIC", "ba\u017fic", "Bearer\u00a0", "x-api-key", "-"]


def redacted_before(text):
    """redact_text as it was: every pattern over every text."""
    for index, (pattern, replacement) in enumerate(rt.SECRET_TEXT):
        text = (NAMES_BEFORE if index == 1 else pattern).sub(replacement, text)
    return text


def test_each_pattern_has_what_it_needs_listed():
    assert len(rt.SECRET_TEXT_NEEDS) == len(rt.SECRET_TEXT)
    assert rt.SECRET_TEXT[1][0].pattern.replace("(?=[a-z_]*=)", "") == NAMES_BEFORE.pattern


def test_a_pattern_searches_only_texts_that_have_what_it_needs():
    """Round 2: a text that fails a pattern's need holds no match of it (header values are searched by no pattern)."""
    chosen = random.Random(2)
    for _ in range(20000):
        text = "".join(chosen.choice(PIECES) for _ in range(chosen.randint(1, 10)))
        for index, ((pattern, _), needs) in enumerate(zip(rt.SECRET_TEXT, rt.SECRET_TEXT_NEEDS)):
            if not needs(text):
                assert (NAMES_BEFORE if index == 1 else pattern).search(text) is None, (index, text)
    for character in map(chr, range(0x3000)):  # white space beyond ASCII's: \s takes it
        text = f"Bearer{character}abcdefghij"
        assert rt.SECRET_TEXT_NEEDS[2](text) or rt.SECRET_TEXT[2][0].search(text) is None, hex(ord(character))
    assert [rt.redact_text(value) for value in ("application/json", "*/*", "2023-06-01", "example.test")] == \
        ["application/json", "*/*", "2023-06-01", "example.test"]


def test_redaction_is_the_same_as_with_every_pattern_over_every_text():
    chosen = random.Random(1)
    for _ in range(20000):
        text = "".join(chosen.choice(PIECES) for _ in range(chosen.randint(1, 14)))
        assert rt.redact_text(text) == redacted_before(text), text


def test_the_names_pattern_matches_any_character_as_before():
    """Case-insensitive matching folds more than ASCII (\u017f is an s, \u212a a k): [a-z_] in the lookahead takes
    every character the names take."""
    for character in [chr(c) for c in range(0x3000)] + ["\u212a", "\u1e9e", "\ufb00"]:
        for text in (f"{character}token=v", f"x{character}secret=v", f"{character}=v", f"sig{character}=v",
                     f"api{character}key=v a", f"{character}ode=v"):
            assert rt.SECRET_TEXT[1][0].sub(r"\1[redacted]", text) == NAMES_BEFORE.sub(r"\1[redacted]", text), text


def test_a_model_request_body_is_redacted_in_a_fraction_of_the_time():
    code = "\n".join(f"def f{i}(x):\n    return x * {i} + compute(count, value)  # comment {i}" for i in range(400))
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": code}], "api_key": "k" * 12,
                       "system": "You are a helpful assistant. " * 200})
    assert rt.redact_text(body) == redacted_before(body) and '"api_key": "[redacted]"' in rt.redact_text(body)

    def best(function):
        times = []
        for _ in range(5):
            started = time.perf_counter()
            function(body)
            times.append(time.perf_counter() - started)
        return min(times)
    assert best(rt.redact_text) < best(redacted_before)  # (about 0.2x here; a loaded CI runner gets room)


# -- a log's lines --

@pytest.mark.parametrize("line", [
    b'{"a": 1}\n', b'{"a": "\xc3\xa9"}', b'{"a": "\\ud800"}\n', b'{"a": "\xed\xa0\x80"}', b'{"a": 1} x', b"{",
    b'{"a": "\xff"}', b"[1, 2]", b"  {\"a\": 1}", b"\xef\xbb\xbf{\"a\": 1}", '{"a": 1}'.encode("utf-16-le"),
    '{"a": 1}'.encode("utf-16"), '{"a": 1}'.encode("utf-32-le"), b"{\x00", b"{\x00\x00\x00", b"", b"1", b"{}",
    b'{"a": [' * 50 + b"]" * 50, b'{"\xc3\xa9": {"x": null}}\r\n'])
def test_a_line_parses_as_json_loads_parses_it(line):
    def outcome(parse):
        try:
            return "value", parse(line)
        except (ValueError, RecursionError) as exc:
            return "error", type(exc)
    assert outcome(tails.loads) == outcome(json.loads)


def test_a_logs_records_are_the_same(tmp_path):
    logs = load("logs")
    path = tmp_path / "x.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": "\xc3\xa9"}\nnot json\n[1]\n{"c": "\xff"}\n{"d": 2}\n')
    assert list(logs.iter_records(path)) == [{"a": 1}, {"b": "\u00e9"}, {"d": 2}]


# -- a report --

def test_a_reports_texts_are_scrubbed_once_each_with_the_same_result(monkeypatch):
    report = load("report")
    token = "Zq8Xw3Lm9Pv2Rt5Yu7Io"
    scrub = report.Scrubber([token])
    calls = []
    inner = scrub.inner
    scrub.inner = lambda text, limit: calls.append(text) or inner(text, limit)
    texts = [f"GET https://h.example/?access_token={token}", f"look-up of {token.lower()}.example", "plain", None, 7,
             "a\x1b[31mred", "x" * 5000]
    first = [scrub(text, 120) for text in texts]
    again = [scrub(text, 120) for text in texts]
    assert first == again and len(calls) == len(texts)
    assert all(token.lower() not in result.lower() for result in first)
    assert scrub("plain", 3) != scrub("plain", 120)  # the limit is part of what is kept

    counted = []

    def redact(text, limit=300):
        counted.append(text)
        return f"<{text}:{limit}>"
    kept = report.Remembered(redact)
    assert kept("a") == kept("a") == "<a:300>" and kept("a", 5) == "<a:5>" and counted == ["a", "a"]
    assert kept(None) == kept(None) == "<None:300>" and counted[-2:] == [None, None]  # only texts are kept
    monkeypatch.setattr(report.Remembered, "LIMIT", 2)
    small = report.Remembered(redact)
    for text in ("x", "y", "z"):
        small(text)
    assert len(small.done) <= 2 and small("z") == "<z:300>"


# -- the Blocked list --

def test_the_blocked_list_reads_a_runs_canary_names_once(tmp_path, monkeypatch):
    approvals = load("approvals")
    directory, logs = tmp_path / "run", tmp_path / "logs"
    (directory / "approvals").mkdir(parents=True)
    logs.mkdir()
    (directory / approvals.CANARY_CONFIG).write_text(json.dumps({"names": [["zq8xw3lm9pv2", "token"]]}))
    with open(logs / "egress-denied.jsonl", "w") as stream:
        for index in range(50):
            stream.write(json.dumps({"time": "2026-11-11T10:00:00Z", "host": f"h{index}.example",
                                     "reason": "not in allowed hosts"}) + "\n")
        stream.write(json.dumps({"time": "2026-11-11T10:00:01Z", "host": "zq8xw3lm9pv2.evil.example",
                                 "reason": "not in allowed hosts"}) + "\n")
    for index, decision in enumerate(("deny", "allow", None)):
        ident = f"1762855200-7-{index}"
        (directory / "approvals" / f"{ident}.json").write_text(json.dumps({
            "id": ident, "kind": "dns", "name": f"q{index}.example", "time": "2026-11-11T10:00:02Z"}))
        if decision:
            (directory / "approvals" / f"{ident}.done").write_text(json.dumps({"decision": decision,
                                                                                 "reason": "answered"}))
    record = {"run": "claude-1", "directory": str(directory), "log_dir": str(logs)}
    reads = []
    names = approvals.canary_names
    monkeypatch.setattr(approvals, "canary_names", lambda record: reads.append(1) or names(record))
    found = approvals.blocked_of_run("claude-1", record)
    assert len(reads) == 1
    targets = {entry["what"]: entry["target"] for entry in found}
    assert targets["lookup of h3.example"] == {"host": "h3.example"}
    assert targets["lookup of zq8xw3lm9pv2.evil.example"] is None  # carries a canary: never offered to allow
    questions = [entry for entry in found if entry["source"] == "approvals"]
    assert [entry["question"] for entry in questions] == ["1762855200-7-0"]  # only the refused one
    assert approvals.canary_in_name(record, "x.ZQ8XW3LM9PV2.example.") is True
    assert approvals.canary_in_name(record, "harmless.example") is False
    assert approvals.carries_canary(None, "anything") is True and approvals.carries_canary(set(), "x") is False
    (directory / approvals.CANARY_CONFIG).write_text("not json")
    assert approvals.canary_in_name(record, "harmless.example") is True  # unreadable: every name counts


# -- the MCP catalogs --

def test_the_catalogs_are_downloaded_at_once_and_read_as_before():
    mcp = load("mcp")
    catalog = {"schema": "axross-mcp-catalog", "version": 1, "entries": [{"name": "demo", "image": "x"}]}
    files = {"https://a.example/catalog.json": json.dumps(catalog).encode(),
             "https://b.example/catalog.json": json.dumps({**catalog, "entries": [{"name": "other"}]}).encode()}
    asked, waiting, lock = [], [], threading.Lock()
    both = threading.Barrier(2, timeout=5)

    def fetch(url):
        with lock:
            asked.append(url)
        if url.endswith("/catalog.json"):
            both.wait()  # both catalogs are being downloaded at the same time, or this waits in vain
        if url not in files:
            raise OSError(f"{url}: not there")
        return files[url]
    found, problems = mcp.catalog_entries([("a", "https://a.example/catalog.json"),
                                           ("b", "https://b.example/catalog.json")], fetch=fetch)
    assert [(prefix, entry["name"]) for prefix, entry in found] == [("a", "demo"), ("b", "other")]
    assert all(entry["_signature"]["state"] == "unsigned" for _, entry in found) and problems == []
    assert sorted(asked) == sorted({f"https://{host}.example/{name}" for host in "ab" for name in (
        "catalog.json", "catalog.json.sig", "rootfs.json", "rootfs.json.sig")})  # each once

    def missing(url):
        raise OSError("unreachable")
    found, problems = mcp.catalog_entries([("a", "https://a.example/catalog.json")], fetch=missing)
    assert found == [] and problems == ["https://a.example/catalog.json: unreachable"]
