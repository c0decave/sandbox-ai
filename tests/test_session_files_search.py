"""A search in a start's session files (user 2026-11-19: the file manager's search of 0.1.147 for the web's file
views; the web has no file manager, so its session files view): `sandbox-ai session files NAME --search PATTERN
[--text TEXT] [--case]` - the app's patterns (*, ?, [abc], ** for any folders, a part of the name without a
wildcard), the text with its line, bounded, a login or key never looked into, over a host where the start is."""
import ast
import gzip
import json
import re
import subprocess

import pytest

from test_session_inspect import CLAUDE_LINES, ROOT, cli, jsonl, kept, put, si  # noqa: F401
from test_session_inspect import isolated, running  # noqa: F401 - the fixtures

APP_SEARCH = ROOT / "projects" / "sandbox-ai-app" / "sandbox_ai_app" / "file_search.py"


def app_translate():
    """The desktop app's file_search.translate, read from its source (it needs no Qt)."""
    tree = ast.parse(APP_SEARCH.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "translate")
    found = {"re": re}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(APP_SEARCH), "exec"), found)  # noqa: S102
    return found["translate"]


@pytest.mark.parametrize("pattern", ["*.jsonl", "src/**/test_*.py", "**/x", "a?c", "[abc]*", "[!a-c]x", "[]x]",
                                     "[!]]", "[a", "x[\\]y]", "a&&b[&&]", "**", "***/y", "rollout-*.jsonl.gz"])
def test_the_patterns_are_the_file_managers(pattern):
    assert si.glob_regex(pattern) == app_translate()(pattern)


def files(home):
    put(home, ".claude/projects/-work/s1.jsonl", jsonl(CLAUDE_LINES))
    put(home, ".claude/projects/-work/Other.jsonl", json.dumps({"type": "user", "message": {
        "role": "user", "content": "deploy Straße"}}, ensure_ascii=False) + "\n")
    put(home, ".claude/projects/-old/s0.jsonl.gz", gzip.compress(jsonl([{"type": "user", "message": {
        "role": "user", "content": "the old needle"}}]).encode()))
    put(home, ".claude/projects/-work/bin.jsonl", b"needle\0binary")
    put(home, ".claude/projects/-work/secret-notes.jsonl", "needle in a login file\n")


def names(result):
    return sorted(item["name"] for item in result["files"])


def test_by_name_path_and_case(tmp_path, running):
    home = tmp_path / "home"
    files(home)
    running(home)
    assert names(si.search("web", "s?.jsonl*")) == [".claude/projects/-old/s0.jsonl.gz",
                                                   ".claude/projects/-work/s1.jsonl"]
    assert names(si.search("web", "other")) == [".claude/projects/-work/Other.jsonl"], "a part of the name, any case"
    assert names(si.search("web", "other", case=True)) == []
    assert names(si.search("web", ".claude/**/-work/s*.jsonl")) == [".claude/projects/-work/s1.jsonl",
                                                                   ".claude/projects/-work/secret-notes.jsonl"]
    assert len(si.search("web", "")["files"]) == 5 and si.search("web", "projects/")["files"] == []
    with pytest.raises(ValueError, match="not a pattern"):
        si.Matcher("[z-a]*")


def test_by_text_with_its_line_bounded_and_without_logins(tmp_path, running):
    home = tmp_path / "home"
    files(home)
    running(home)
    found = si.search("web", "", "NEEDLE")
    assert names(found) == [".claude/projects/-old/s0.jsonl.gz"], "a compacted transcript is read as it was"
    hit = found["files"][0]
    assert hit["line"] == 1 and "the old needle" in hit["excerpt"]
    report = found["search"]
    assert report["binary"] == 1 and report["secret"] == 1, "binary: not looked into; a login or key: never"
    assert si.search("web", "", "NEEDLE", case=True)["files"] == []
    assert names(si.search("web", "*.jsonl", "strasse")) == [".claude/projects/-work/Other.jsonl"], "case folded"
    tool = si.search("web", "s1.jsonl", '"name": "bash"')["files"][0]
    assert tool["line"] == 2 and '"name": "Bash"' in tool["excerpt"] and len(tool["excerpt"]) <= 202


def test_the_limits_are_said(tmp_path, running, monkeypatch):
    home = tmp_path / "home"
    files(home)
    put(home, ".claude/projects/-work/huge.jsonl", "x" * 5000 + "\nlate needle\n")
    running(home)
    monkeypatch.setattr(si, "SEARCH_FILE", 1000)
    found = si.search("web", "huge*", "needle")
    assert found["files"] == [] and found["search"]["partly"] == 1, "only its start: said"
    monkeypatch.setattr(si, "SEARCH_RESULTS", 2)
    found = si.search("web", "*.jsonl*")
    assert len(found["files"]) == 2 and "only the first 2 files found are listed" in found["search"]["limits"][0]
    ticks = iter(range(0, 10000, 100))
    found = si.search("web", "", "needle", clock=lambda: next(ticks))
    assert any("stopped after 60 s" in limit for limit in found["search"]["limits"])


def test_a_hostile_file_is_text_never_a_sequence(tmp_path, running):
    home = tmp_path / "home"
    put(home, ".claude/projects/-work/evil.jsonl", "pre \x1b]52;c;ZXZpbA==\x07 needle \x1b[2J post\n")
    running(home)
    hit = si.search("web", "", "needle")["files"][0]
    assert "\x1b" not in hit["excerpt"] and "\\x1b" in hit["excerpt"]


def test_cli_end_to_end(tmp_path):
    home = kept(tmp_path, "web", running=True)
    files(home)
    done = cli("files", "web", "--search", "*.jsonl*", "--text", "needle", "--json")
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert names(result) == [".claude/projects/-old/s0.jsonl.gz"] and result["search"]["secret"] == 1
    text = cli("files", "web", "--text", "needle").stdout
    assert ".claude/projects/-old/s0.jsonl.gz:1: " in text and "marked as a login or key, found by name only" in text
    assert cli("files", "web", "--search", "*", "--file", "x").returncode == 1
    refused = cli("files", "web", "--case")
    assert refused.returncode == 1 and "--case goes with --search or --text" in refused.stderr


def test_a_hosts_start_is_searched_there(monkeypatch):
    calls = []

    def run(host, args, timeout=120, **kwargs):
        calls.append(args)
        return 0, json.dumps({"name": "web", "id": "web", "agent": "claude", "state": "running", "home": "/h",
                              "files": [], "hidden": 0, "search": {"pattern": "*.jsonl", "text": "x", "case": True,
                                                                   "looked_into": 0, "binary": 0, "partly": 0,
                                                                   "secret": 0, "unreadable": [], "limits": []}}), ""
    monkeypatch.setattr(si.module("hosts"), "run", run)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent")
    args = type("A", (), {"name": "web", "file": None, "full": False, "raw": False, "json": True, "host": "ct1",
                          "command": "files", "hide_secrets": False, "show_secret": False, "search": "*.jsonl",
                          "text": "x", "case": True})()
    assert si.run(args, lambda line: None) == 0
    assert calls == [["session", "files", "--json", "web", "--search=*.jsonl", "--text=x", "--case"]]
    monkeypatch.setattr(si.module("hosts"), "run", lambda *a, **k: (0, json.dumps({"name": "web", "files": []}), ""))
    with pytest.raises(ValueError, match="does not search session files yet"):
        si.run(args, lambda line: None)


def test_help_names_the_options():
    done = subprocess.run([str(ROOT / "sandbox-ai"), "session", "files", "--help"], capture_output=True, text=True,
                          timeout=60)
    assert "--search PATTERN" in done.stdout and "--text TEXT" in done.stdout and "--case" in done.stdout
