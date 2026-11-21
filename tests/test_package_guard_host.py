"""The package guard on the host, beside its command: doctor's line about the offline copy, the sync that goes along
with an update, the approvals list's JSON (what the web interface and other hosts' apps read), the safeguard records
and the report's findings."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_kit  # noqa: E402
from test_packages_cmd import ADVISORIES, published  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doctor = load("sandbox_doctor_packages", "doctor.py")
updates = load("sandbox_updates_packages", "updates.py")
approvals = load("sandbox_approvals_packages_host", "approvals.py")
safeguards = load("sandbox_safeguards_packages_host", "safeguards.py")
report = load("sandbox_report_packages", "report.py")
pk = updates.load_module("packages")  # the module the update's sync uses

needs_ssh = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs ssh-keygen")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SANDBOX_AI_SOURCE", raising=False)
    return tmp_path


@pytest.fixture
def dump(tmp_path):
    folder = tmp_path / "osv"
    folder.mkdir()
    for entry in ADVISORIES:
        (folder / f"{entry['id']}.json").write_text(json.dumps(entry))
    return folder


# --- doctor ------------------------------------------------------------------------------------------------------

def test_doctor_shows_the_copys_age_and_warns_without_one_or_with_an_old_one(home, dump):
    status, title, detail, hint = doctor.check_packages()
    assert (status, title) == ("warn", "Package guard") and "no copy of the known malicious packages" in detail
    assert "release age and new names are still checked" in detail and hint.startswith("sandbox-ai packages sync")
    doctor.packages_module().sync_osv([dump], out=lambda line: None)
    status, _, detail, hint = doctor.check_packages()
    assert status == "ok" and hint == ""
    assert "known malicious packages: 8 entries, 0 days old (made 20" in detail
    assert "not signed (built here from OSV's dumps)" in detail
    info = doctor.packages_module().status()
    status, _, detail, hint = doctor.check_packages({**info, "age_days": 20, "stale": True, "signed": True})
    assert status == "warn" and "20 days old" in detail and "signed by a release key" in detail
    assert "new malware is missing" in detail and "sandbox-ai packages sync" in hint
    assert doctor.check_packages({**info, "age_days": 1})[2].count("1 day old") == 1
    assert doctor.check_packages({**info, "problem": "missing: go.list"})[0] == "warn"
    assert doctor.check_packages({"present": False, "problem": "not a copy"})[2].startswith("its copy")
    # one of doctor's checks, in the System group, on Linux and on macOS
    assert doctor.group_of("Package guard") == "System"
    source = (ROOT / "sandbox/doctor.py").read_text()
    assert source.count("check_updates(), check_packages(), check_user()") == 2


def test_doctor_json_through_the_real_command(home):
    import subprocess
    import os
    done = subprocess.run([str(ROOT / "sandbox-ai"), "doctor", "--json"], capture_output=True, text=True, timeout=300,
                          env={**os.environ, "XDG_DATA_HOME": str(home / "data"),
                               "XDG_CONFIG_HOME": str(home / "config"), "XDG_STATE_HOME": str(home / "state")})
    entry = next(e for e in json.loads(done.stdout) if e["check"] == "Package guard")
    assert entry["status"] == "warn" and entry["group"] == "System" and "sandbox-ai packages sync" in entry["hint"]


# --- the sync that goes along with an update -----------------------------------------------------------------------

@needs_ssh
def test_an_update_syncs_the_copy_along_and_never_fails_for_it(tmp_path, home, dump, monkeypatch):
    key = release_kit.new_key(tmp_path / "key")
    monkeypatch.setattr(pk.module("release_keys"), "load", lambda path=None, ring=None: [(key.kind, key.key)])
    said = []
    # a release source that publishes no copy: passed over without a word
    (tmp_path / "empty").mkdir()
    assert pk.sync_with_update(said.append, source=tmp_path / "empty") is None and said == []
    # the signed copy: taken, one line
    folder = published(tmp_path, dump, key)
    info = pk.sync_with_update(said.append, source=folder)
    assert info["signed"] and info["total"] == 8
    assert said == ["sandbox-ai: synced the known malicious packages: 8 entries, made 2026-11-10T00:00:00Z"]
    # at most once a day
    del said[:]
    assert pk.sync_with_update(said.append, source=folder) is None and said == []
    # a copy that cannot be trusted: one line, the copy here stays, nothing raises
    state = pk.data_dir() / pk.STATE
    state.write_text(json.dumps({**json.loads(state.read_text()), "synced": "2020-01-01T00:00:00Z"}))
    older = published(tmp_path, dump, key, "older", generated="2026-11-01T00:00:00Z")
    assert pk.sync_with_update(said.append, source=older) is None
    assert len(said) == 1 and "was not synced" in said[0] and "older" in said[0] and "sandbox-ai packages sync" in said[0]
    assert pk.status()["generated"] == "2026-11-10T00:00:00Z"
    # updates.py hands it the update's own source and swallows whatever goes wrong
    seen = []
    monkeypatch.setattr(pk, "sync_with_update", lambda out, base=None: seen.append(base) or "status")
    assert updates.sync_packages("https://git.example.org/me/sandbox-ai", said.append) == "status"
    assert seen == ["https://git.example.org/me/sandbox-ai"]
    monkeypatch.setattr(pk, "sync_with_update", lambda out, base=None: 1 / 0)
    del said[:]
    assert updates.sync_packages(None, said.append) is None and "was not synced" in said[0]
    assert pk.default_source("https://git.example.org/me/sandbox-ai") == \
        "https://git.example.org/me/sandbox-ai/releases/download/osv-malicious"


def test_the_update_command_syncs_along_once_a_day_and_its_functions_fetch_nothing(home, monkeypatch):
    """The sync is the command's (main.py), after the update went through: updates.py's own functions - which
    programs and tests call with sources of their own - never fetch anything else."""
    calls = []
    monkeypatch.setattr(updates, "sync_packages", lambda source=None, out=print: calls.append(source))
    at = [1795000000.0]
    clock = lambda: at[0]  # noqa: E731
    # one try a day, whatever came of it; the update's own source, else the one of update mode
    updates.sync_packages_along(["--install"], "https://git.example.org/me/sandbox-ai", clock=clock)
    updates.sync_packages_along(["--install"], "https://git.example.org/me/sandbox-ai", clock=clock)
    at[0] += 3600
    updates.sync_packages_along(["--scheduled"], None, clock=clock)
    assert calls == ["https://git.example.org/me/sandbox-ai"]
    at[0] += 86400
    updates.save_settings({"update_source": "https://git.example.org/other/fork"})
    updates.sync_packages_along(["--scheduled"], None, clock=clock)
    assert calls[1:] == ["https://git.example.org/other/fork"]
    # mode off: the timer's run asks nothing of the network; an update you ask for yourself still syncs
    updates.save_settings({"update_mode": "off"})
    at[0] += 2 * 86400
    updates.sync_packages_along(["--scheduled"], None, clock=clock)
    assert len(calls) == 2
    updates.sync_packages_along(["--install"], None, clock=clock)
    assert len(calls) == 3
    # the functions of updates.py themselves call none of it (their tests name sources that do not exist)
    source = (ROOT / "sandbox/updates.py").read_text()
    assert source.count("sync_packages(") == 2 and source.count("sync_packages_along(") == 1  # the definitions' own
    main = load("sandbox_main_packages", "main.py")
    assert main.source_option(["--force", "--source", "https://a.example/o/r"]) == "https://a.example/o/r"
    assert main.source_option(["--source=https://b.example/o/r"]) == "https://b.example/o/r"
    assert main.source_option(["--apply"]) is None and main.source_option(["--source"]) is None
    seen = []
    monkeypatch.setattr(main, "module", lambda name: type("U", (), {"sync_packages_along": staticmethod(
        lambda words, source, out: seen.append((list(words), source)))}))
    assert main.update_then_packages(lambda rest: 0, ["--install", "--source", "https://a.example/o/r"]) == 0
    assert seen == [(["--install", "--source", "https://a.example/o/r"], "https://a.example/o/r")]
    for status, rest in ((1, ["--apply"]), (3, ["--install"]), (0, ["mode", "auto"]), (0, ["--rollback"]),
                         (0, ["--check"])):
        assert main.update_then_packages(lambda rest, status=status: status, rest) == status
    assert len(seen) == 1  # a failed or busy update, the mode's own commands, a check, a rollback: nothing fetched


# --- approvals list --json, safeguards, report ------------------------------------------------------------------

MALICIOUS = {"id": "1795000000-1-1", "kind": "http", "method": "GET", "rule": "malicious", "ruleset": "package-guard",
             "url": "https://registry.npmjs.org/evil-pkg/-/evil-pkg-1.0.0.tgz", "time": "2026-11-18T12:00:00Z",
             "timeout": 60, "package": {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "age_days": None,
                                        "malicious": "MAL-2025-1"}}
YOUNG = {**MALICIOUS, "id": "1795000000-1-2", "rule": "min-age",
         "package": {"ecosystem": "npm", "name": "left-pad\x1b[2J", "version": "1.3.2", "age_days": 2,
                     "allow": "sandbox-ai packages allow npm:left-pad@1.3.2", "extra": {"nested": 1}}}


def test_the_approvals_list_carries_the_package_and_caps_a_malicious_one(monkeypatch):
    monkeypatch.setattr(approvals, "questions", lambda runs=None, answered=False, late=False: [
        ("r1", {"run": "r1"}, MALICIOUS), ("r1", {"run": "r1"}, YOUNG)])
    monkeypatch.setattr(approvals, "policy_scope", lambda: "always")
    lines = []
    assert approvals.main(["list", "--json"], out=lines.append) == 0
    first, second = json.loads(lines[0])
    assert first["package"] == {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "age_days": None,
                                "malicious": "MAL-2025-1"}
    assert first["max_scope"] == "once" and "allow" in first["actions"]
    assert first["question"].startswith("download the npm package evil-pkg 1.0.0: KNOWN MALICIOUS (advisory MAL-2025-1")
    assert second["max_scope"] == "run" and second["package"]["age_days"] == 2
    assert "\x1b" not in second["package"]["name"] and "extra" not in second["package"]  # escaped, known keys only
    assert second["package"]["allow"] == "sandbox-ai packages allow npm:left-pad@1.3.2"
    # the table says the same line
    lines = []
    assert approvals.main(["list"], out=lines.append) == 0
    assert "KNOWN MALICIOUS (advisory MAL-2025-1" in " ".join(" ".join(str(line) for line in lines).split())


def records():
    base = {"time": "2026-11-18T12:00:00Z", "method": "GET", "destination": "104.16.0.1:443",
            "process": {"comm": "npm", "pid": 41}}
    url = "https://registry.npmjs.org"
    return [
        {**base, "event": "denied", "url": f"{url}/evil-pkg/-/evil-pkg-1.0.0.tgz", "rule": "malicious",
         "ruleset": "package-guard", "decision": "deny",
         "reason": "not approved (not answered yet): package guard: npm evil-pkg 1.0.0 is a known malicious package "
                   "(MAL-2025-1, https://osv.dev/vulnerability/MAL-2025-1)",
         "approval": {"decision": "deny", "scope": "once", "reason": "not answered yet", "question": "1795000000-1-1"},
         "package": {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "age_days": None,
                     "malicious": "MAL-2025-1"}},
        {**base, "event": "denied", "url": f"{url}/left-pad/-/left-pad-1.3.2.tgz", "rule": "min-age",
         "ruleset": "package-guard", "decision": "ask", "reason": "not approved (asking is off): package guard: npm "
         "left-pad 1.3.2 was released 2 days ago (younger than 14 days)",
         "approval": {"decision": "deny", "scope": "once", "reason": "asking is off (--ask off or no approvals)"},
         "package": {"ecosystem": "npm", "name": "left-pad", "version": "1.3.2", "age_days": 2}},
        {**base, "event": "denied", "url": f"{url}/left-pad/-/left-pad-1.3.2.tgz", "rule": "min-age",
         "ruleset": "package-guard", "decision": "ask", "reason": "not approved (answered)",
         "approval": {"decision": "deny", "scope": "once", "reason": "answered"},
         "package": {"ecosystem": "npm", "name": "left-pad", "version": "1.3.2", "age_days": 2}},
        {**base, "event": "http", "status": 404, "url": f"{url}/nope",
         "package": {"ecosystem": "npm", "name": "nope", "metadata": "packument", "missing": True}},
        {**base, "event": "http", "status": 200, "url": f"{url}/left-pad/-/left-pad-1.3.0.tgz",
         "package": {"ecosystem": "npm", "name": "left-pad", "version": "1.3.0", "age_days": 800}},
        {**base, "event": "http", "status": 200, "url": f"{url}/x/-/x-1.0.0.tgz",
         "package": {"ecosystem": "npm", "name": "x", "version": "1.0.0", "age_days": 3, "reported": "min-age"}},
        {**base, "event": "denied", "url": f"{url}/a/..%2fb", "rule": "ambiguous-path", "ruleset": "package-guard",
         "decision": "deny", "reason": "package guard: a path on a package registry that servers read differently",
         "package": {"ecosystem": "npm"}},
    ]


def test_safeguard_records_say_what_was_held_why_and_the_exact_command(tmp_path):
    found = [safeguards.from_connection(entry, "run1", safeguards.Redactor()) for entry in records()]
    malicious, young, answered, missing, passed, reported, unclear = found
    assert answered is None and passed is None  # your own "deny" and a download that passed are no safeguard
    assert malicious["kind"] == "package" and malicious["level"] == "error" and malicious["stays"] is True
    assert malicious["what"] == "download of npm evil-pkg 1.0.0" and "MAL-2025-1" in malicious["why"]
    assert "sandbox-ai approvals allow 1795000000-1-1" in malicious["fix"] and "never a list" in malicious["fix"]
    assert malicious["details"]["malicious"] == "MAL-2025-1" and malicious["process"]
    assert young["level"] == "warning" and young["stays"] is False and young["rule"] == "min-age"
    assert "sandbox-ai packages allow npm:left-pad@1.3.2" in young["fix"]
    assert "--allow-package npm:left-pad@1.3.2" in young["fix"]
    assert missing["what"] == "npm nope: no such package" and "hallucinated or slopsquatted" in missing["why"]
    assert reported["rule"] == "min-age" and "recorded only" in reported["why"]
    assert unclear["stays"] is True and unclear["kind"] == "package"
    assert "package" in safeguards.KINDS and safeguards.KINDS["package"][0] == "warning"
    # `sandbox-ai safeguards --kind package` lists them
    path = tmp_path / safeguards.FILE
    assert safeguards.append(path, [entry for entry in found if entry])
    shown = []
    assert safeguards.cli(["--log-dir", str(tmp_path), "--kind", "package", "--json"], out=shown.append) == 0
    assert len(shown) == 5 and all(json.loads(line)["kind"] == "package" for line in shown)


def test_the_report_counts_them_with_a_malicious_version_as_high():
    found = [safeguards.from_connection(entry, "run1", safeguards.Redactor()) for entry in records()]
    malicious, young, _, missing, _, _, _ = found
    assert report.KINDS["package"][0] == "medium"
    high, medium, noted = (report.Gatherer.from_safeguard(entry, "run1") for entry in (malicious, young, missing))
    assert high["severity"] == "high" and high["verdict"] == "refused (it stays refused)" and high["kind"] == "package"
    assert medium["severity"] is None and medium["verdict"] == "refused"  # (the kind's own: medium)
    assert noted["verdict"] == "recorded"  # nothing was refused: a name the registry does not know


# --- the web interface (app.js in node) ----------------------------------------------------------------------------

WEB_DRIVER = r"""
(async () => {
  const result = {};
  if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
  vm.runInContext('var MAL = ' + JSON.stringify(__MALICIOUS__) + '; var YOUNG = ' + JSON.stringify(__YOUNG__) + ';', context);
  const text = (expr) => { const node = vm.runInContext(expr, context); return node ? [node.className || "", node.textContent] : null; };
  result.malicious = text("packageNote(MAL)");
  result.young = text("packageNote(YOUNG)");
  result.none = text('packageNote({ kind: "http", question: "POST https://x" })');
  result.hostile = text('packageNote({ package: "npm" })');
  result.run = vm.runInContext('scopeRefused(MAL, "run", false)', context);
  result.once = vm.runInContext('scopeRefused(MAL, "once", false)', context);
  result.youngRun = vm.runInContext('scopeRefused(YOUNG, "run", false)', context);
  result.youngAlways = vm.runInContext('scopeRefused(YOUNG, "always", false)', context);
  result.title = vm.runInContext('safeguardTitle("package")', context);
  console.log(JSON.stringify(result));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.parametrize("language", ["en", "de"])
def test_the_web_interface_says_what_a_package_question_means_and_caps_its_answers(tmp_path, language, monkeypatch):
    import subprocess
    from test_web import FAKE_DOM
    monkeypatch.setattr(approvals, "questions", lambda runs=None, answered=False, late=False: [
        ("r1", {"run": "r1"}, MALICIOUS), ("r1", {"run": "r1"}, {**YOUNG, "package": {
            **YOUNG["package"], "name": "left-pad"}})])
    monkeypatch.setattr(approvals, "policy_scope", lambda: "always")
    lines = []
    approvals.main(["list", "--json"], out=lines.append)
    malicious, young = json.loads(lines[0])  # what the web interface is sent: the real command's JSON
    driver = tmp_path / "packages.js"
    driver.write_text(FAKE_DOM + WEB_DRIVER.replace("__MALICIOUS__", json.dumps(malicious))
                      .replace("__YOUNG__", json.dumps(young)))
    run = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), language], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["none"] is None and result["hostile"] is None and result["once"] is None
    assert "status-bad" in result["malicious"][0] and "MAL-2025-1" in result["malicious"][1]
    assert "sandbox-ai packages allow npm:left-pad@1.3.2" in result["young"][1]
    assert result["youngRun"] is None and result["youngAlways"]  # a request: at most for the run
    if language == "en":
        assert result["run"].startswith("A known malicious package version is allowed for this one download at most")
        assert result["malicious"][1].startswith("Package guard: this version is on the list of known malicious")
        assert result["title"] == "package guard"
    else:
        assert result["run"].startswith("Eine bekannt bösartige Paketversion wird höchstens für diesen einen Download")
        assert result["malicious"][1].startswith("Paketwächter: Diese Version steht auf der Liste bekannt bösartiger")
        assert result["young"][1].startswith("Paketwächter: Eine so junge Version")
        assert result["title"] == "Paketwächter"


def test_the_report_of_a_run_through_the_real_command(tmp_path):
    """sandbox-ai report over a run's connection log (no safeguards.jsonl: the findings are made from the log): the
    package guard's findings are there, a known malicious version under High."""
    import os
    import subprocess
    logs = tmp_path / "logs"
    logs.mkdir()
    with open(logs / "connections-20261118-120000-claude-1a2b3c4d.jsonl", "w") as stream:
        for entry in records():
            stream.write(json.dumps(entry) + "\n")
    env = {**os.environ, **{name: str(tmp_path / name.lower()) for name in (
        "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME")}}
    for name in ("XDG_STATE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        (tmp_path / name.lower()).mkdir()
    done = subprocess.run([str(ROOT / "sandbox-ai"), "report", "--log-dir", str(logs), "-f", "md"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    text = done.stdout
    assert "package guard" in text and "evil-pkg 1.0.0" in text and "MAL-2025-1" in text
    assert "nope: no such package" in text and "left-pad 1.3.2" in text
    data = report.gather(report.resolve(log_dir=logs), live=False)
    found = [f for f in data["findings"] if f["kind"] == "package"]
    by_what = {f["what"]: f for f in found}
    assert by_what["download of npm evil-pkg 1.0.0"]["severity"] == "high"
    assert by_what["npm nope: no such package"]["severity"] == "medium"
    assert by_what["download of npm x 1.0.0"]["verdict"] == "recorded"  # it passed: a young version, only recorded
    assert by_what["npm nope: no such package"]["verdict"] == "recorded"
    assert len(found) == 5


def test_the_blocked_list_says_it_was_the_package_guard():
    entry = {"kind": "rule", "rule": "min-age", "ruleset": "package-guard", "question": None}
    assert approvals.blocked_why(entry) == ("the package guard (a version younger than the minimum release age) "
                                            "refuses this download; sandbox-ai packages says what would allow it")
    waiting = approvals.blocked_why({"kind": "unanswered", "rule": "malicious", "ruleset": "package-guard",
                                     "question": "1795000000-1-1"})
    assert waiting.startswith("the package guard (a known malicious package version) asks before it, and nobody "
                              "answered in time")
    assert approvals.PACKAGE_RULESET == approvals.runtime().PACKAGE_RULESET == safeguards.PACKAGE_RULESET
    assert "ruleset mine (rule r1)" in approvals.blocked_why({"kind": "rule", "rule": "r1", "ruleset": "mine"})


def test_rules_check_and_show_say_what_a_ruleset_has_the_guard_decide(tmp_path):
    import os
    import subprocess
    rules = tmp_path / "strict.json"
    rules.write_text(json.dumps({"version": 1, "packages": {"min_age_days": 30, "new_names": "ask"}}))
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"version": 1, "packages": {"min_age": "sometimes"}}))
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "NO_COLOR": "1"}
    done = subprocess.run([str(ROOT / "sandbox-ai"), "rules", "check", str(rules), str(broken)], capture_output=True,
                          text=True, env=env, timeout=120, cwd=tmp_path)
    said = done.stdout + done.stderr
    assert done.returncode != 0
    assert "package guard: min_age_days 30, new_names ask" in said
    assert '"packages" takes "malicious" (deny, block), "min_age" and "new_names" (report, ask, deny)' in said
