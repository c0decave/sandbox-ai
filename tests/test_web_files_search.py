"""The web interface's session files view searches (user 2026-11-19: the file manager's search of 0.1.147 for the
web's file views - the web has no file manager, so its session files view): a pattern (* ? [abc], ** for any
folders, a part of the name without a wildcard), a text with each file's line, Match case; Show all lists every file
again. app.js in node (English and German), then the service's route against the real `sandbox-ai session files
--search` of a start with transcripts, on this machine."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_web import FAKE_DOM, run_driver, w  # noqa: E402

LISTING = {"name": "web", "home": "/h", "files": [
    {"name": ".claude/projects/-w/s1.jsonl", "agent": "claude", "size": 10, "modified": "2026-11-19T10:00:00Z"},
    {"name": ".codex/sessions/2026/11/19/rollout-1.jsonl", "agent": "codex", "size": 20,
     "modified": "2026-11-19T09:00:00Z"}]}
FOUND = {"name": "web", "home": "/h", "files": [
    {"name": ".claude/projects/-w/s1.jsonl", "agent": "claude", "size": 10, "modified": "2026-11-19T10:00:00Z",
     "line": 7, "excerpt": "…\"text\": \"deploy <b>now</b>\"…"}],
    "search": {"pattern": "*.jsonl", "text": "deploy", "case": True, "looked_into": 3, "binary": 1, "partly": 0,
               "secret": 1, "unreadable": [], "limits": ["only the first 1000 files found are listed"]}}

DRIVER = FAKE_DOM + r"""
const byId = (id) => body.all().find(n => n.id === id);
(async () => {
  const result = {};
  if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "starts";', context);
  let found = __FOUND__;
  answer = (method, path, sent) => {
    if (path === "/api/hosts/ct1/starts/web/session-files") return __LISTING__;
    if (path === "/api/hosts/ct1/starts/web/session-files-search") return found;
    return [];
  };
  vm.runInContext('inspectCard(__main, { name: "web", host: "ct1", state: "running", kind: "session", keep: true }, "files")', context);
  await settle();
  const card = byId("session-files-card");
  const cells = () => card.all().filter(n => n.tagName === "tr").map(r => r.children.map(c => c.textContent));
  result.listed = cells();
  byId("files-search-pattern").value = " *.jsonl ";
  byId("files-search-text").value = "deploy";
  byId("files-search-case").checked = true;
  card.all().find(n => n.tagName === "form").fire("submit"); await settle();
  result.posted = calls.filter(c => c.method === "POST").map(c => [c.path, JSON.parse(c.body)]);
  result.found = cells();
  result.notes = card.all().filter(n => n.className === "hint").map(n => n.textContent);
  result.labels = [byId("files-search-pattern").attributes["aria-label"], byId("files-search-pattern").attributes.title,
    card.all().filter(n => n.tagName === "button").map(b => b.textContent)];
  // a file found opens as before (the conversation; an input's value is "" in a browser)
  card.all().filter(n => n.tagName === "input" && n.value === undefined).forEach(n => { n.value = ""; });
  card.all().find(n => n.tagName === "button" && n.textContent === ".claude/projects/-w/s1.jsonl").fire("click"); await settle();
  result.opened = calls.filter(c => c.method === "GET").map(c => c.path).pop();
  // Show all: the listing again
  card.all().find(n => n.tagName === "button" && (n.textContent === "Show all" || n.textContent === "Alle zeigen")).fire("click"); await settle();
  result.again = [cells().length, byId("files-search-pattern").value, byId("files-search-text").value];
  // a host that refuses (an older sandbox-ai): said in the list's place
  found = { __status: 502, __error: "sandbox-ai on ct1 is older: update it" };
  card.all().find(n => n.tagName === "form").fire("submit"); await settle();
  result.refused = card.all().some(n => (n.textContent || "").includes("older: update it"));
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
""".replace("__FOUND__", json.dumps(FOUND)).replace("__LISTING__", json.dumps(LISTING))


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_session_files_view_searches_by_pattern_and_text(tmp_path):
    result = run_driver(tmp_path, DRIVER, "files_search")
    assert [row[0] for row in result["listed"][1:]] == [f["name"] for f in LISTING["files"]]
    assert result["posted"] == [["/api/hosts/ct1/starts/web/session-files-search",
                                 {"pattern": "*.jsonl", "text": "deploy", "case": True}]]
    head, row = result["found"][0], result["found"][1]
    assert head == ["File", "Agent", "Line", "Found", "Size", "Modified", "Login or key"]
    assert row[:4] == [".claude/projects/-w/s1.jsonl", "claude", "7", "…\"text\": \"deploy <b>now</b>\"…"], \
        "the part of the line, as text"
    notes = result["notes"]
    assert "1 files named *.jsonl that hold deploy" in notes
    assert "1 files are not text (binary): not looked into." in notes
    assert "1 files marked as a login or key: found by their name only, never looked into." in notes
    assert "only the first 1000 files found are listed" in notes, "the host's limits, said"
    assert result["labels"][0] == "File name or pattern" and "** any folders" in result["labels"][1]
    assert {"Search", "Show all"} <= set(result["labels"][2])
    assert result["opened"] == "/api/hosts/ct1/starts/web/session-files/.claude%2Fprojects%2F-w%2Fs1.jsonl"
    assert result["again"] == [3, "", ""]
    assert result["refused"] is True


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_in_german(tmp_path):
    driver = tmp_path / "files_search_de.js"
    driver.write_text(DRIVER)
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox" / "web" / "app.js"), "de"], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["found"][0][:4] == ["Datei", "Agent", "Zeile", "Gefunden"]
    assert "1 Dateien namens *.jsonl, die deploy enthalten" in result["notes"]
    assert result["labels"][0] == "Dateiname oder Muster" and "Alle zeigen" in result["labels"][2]


def test_the_route_searches_with_the_real_command(tmp_path):
    """POST /api/hosts/local/starts/NAME/session-files-search against the real `sandbox-ai session files --search`:
    a kept start's transcripts on disk, found by pattern and by text, a login file never looked into."""
    home = tmp_path / "home"
    state = home / ".local/state/sandbox-ai/sessions/web"
    put = lambda relative, text: ((state / "home" / relative).parent.mkdir(parents=True, exist_ok=True),  # noqa: E731
                                  (state / "home" / relative).write_text(text))
    put(".claude/projects/-w/s1.jsonl", json.dumps({"type": "user", "message": {"role": "user",
                                                                                  "content": "please deploy it"}}) + "\n")
    put(".claude/projects/-w/s2.jsonl", json.dumps({"type": "user", "message": {"role": "user",
                                                                                  "content": "nothing"}}) + "\n")
    put(".claude/projects/-w/token-notes.jsonl", "deploy key\n")
    (state / "session.json").write_text(json.dumps({"agent": "claude", "options": [], "keep": True, "name": "web",
                                                    "workspace": "/srv/w", "cwd": "/", "backend": "podman",
                                                    "created": "2026-11-19T00:00:00Z"}))
    env = {**os.environ, "HOME": str(home)}
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        env.pop(name, None)

    def runner(host, args, timeout=120, input=None):
        done = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr
    api = w.Api(runner)
    path = "/api/hosts/local/starts/web/session-files-search"
    named = api.dispatch("POST", path, {"pattern": "s?.jsonl"})
    assert sorted(f["name"] for f in named["files"]) == [".claude/projects/-w/s1.jsonl", ".claude/projects/-w/s2.jsonl"]
    held = api.dispatch("POST", path, {"pattern": ".claude/**/*.jsonl", "text": "DEPLOY"})
    assert [(f["name"], f["line"]) for f in held["files"]] == [(".claude/projects/-w/s1.jsonl", 1)]
    assert "please deploy it" in held["files"][0]["excerpt"] and held["search"]["secret"] == 1
    assert api.dispatch("POST", path, {"pattern": "", "text": "DEPLOY", "case": True})["files"] == []
    for body, said in (({"pattern": "a\nb"}, "pattern"), ({"pattern": "*", "text": 5}, "text"),
                       ({"case": "yes"}, "case is true or false")):
        with pytest.raises(w.Refused, match=said) as refused:
            api.dispatch("POST", path, body)
        assert refused.value.status == 400
    with pytest.raises(w.Refused, match="not a pattern") as refused:
        api.dispatch("POST", path, {"pattern": "[z-a]"})
    assert refused.value.status == 502
