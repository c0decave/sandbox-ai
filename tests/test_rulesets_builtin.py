"""Audit mode for rulesets ("mode": "audit": record what the rules would decide, decide nothing; sandbox-ai rules audit
reports it) and sandbox-ai's own rulesets, opt-in with --rules builtin:NAME (sandbox/rulesets: github-persistence,
destructive, mcp-no-credentials, secret-files; sandbox-ai rules builtin).

End to end: the agent's hook through the real hook server (start_hook_server, hook_client), the CLI through
./sandbox-ai. The permanent regression tests: an audit ruleset never changes a decision, never hides a tool, never
taints; each built-in ruleset asks for what it names and lets the ordinary through."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("sandbox_runtime_builtin_rules", "sandbox/runtime.py")
BUILTIN = ROOT / "sandbox/rulesets"
GH_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # assembled at run time: no scanner takes it for real


def builtin(name, audit=False):
    document = json.loads((BUILTIN / f"{name}.json").read_text())
    if audit:
        document["mode"] = "audit"
    return {"name": f"builtin:{name}", **rt.Ruleset.validated(document, name)}


def ruleset(*sources):
    return rt.Ruleset(list(sources))


def request(method, host, path, body=None):
    headers = [["Host", host]] + ([["Content-Type", "application/json"]] if body is not None else [])
    facts = rt.Ruleset.request(host, method, path, headers)
    if body is not None:
        facts["body"] = body
    return facts


# --- audit mode --------------------------------------------------------------------------------------------------------

DENY_CURL = {"version": 1, "tool": [{"id": "no-curl", "tool": "Bash", "args": {"command": {"pattern": "(?s).*curl.*"}},
                                     "decision": "deny"}],
             "mcp": [{"id": "no-writes", "server": "files", "tool": "write_file", "decision": "deny"}],
             "http": [{"id": "no-gists", "host": "api.github.com", "path": "/gists/**", "decision": "deny"}]}


def test_an_audit_ruleset_records_and_decides_nothing():
    audit = {"name": "trial", **rt.Ruleset.validated({**DENY_CURL, "mode": "audit"}, "trial")}
    rules = ruleset(audit)
    verdict = rules.agent_tool("claude", "Bash", {"command": "curl x"})
    assert verdict["decision"] == "allow"
    assert verdict["audit"] == [{"would": "deny", "rule": "no-curl", "ruleset": "trial",
                                 "reason": "the tool Bash of claude is denied by rule no-curl (trial)"}]
    assert rules.agent_tool("claude", "Bash", {"command": "ls"}) == {"decision": "allow", "rule": None,
                                                                     "ruleset": None, "reason": None}
    assert rules.mcp("files", "write_file", {})["decision"] == "allow" and rules.mcp("files", "write_file", {})["audit"]
    assert not rules.hidden("files", "write_file"), "an audit ruleset never takes a tool out of the list"
    assert not rules.mcp_asks()
    gist = rules.http(request("POST", "api.github.com", "/gists"))
    assert gist["decision"] == "allow" and gist["audit"][0]["rule"] == "no-gists"
    # beside an enforcing ruleset: that one decides, the audit is recorded beside it
    both = ruleset({"name": "real", **rt.Ruleset.validated(DENY_CURL, "real")}, audit)
    verdict = both.agent_tool("claude", "Bash", {"command": "curl x"})
    assert verdict["decision"] == "deny" and verdict["ruleset"] == "real" and verdict["audit"][0]["ruleset"] == "trial"


@pytest.mark.parametrize("document, message", [
    ({"version": 1, "mode": "dry"}, '"mode" is "enforce"'),
    ({"version": 1, "mode": "audit", "untrusted": [{"id": "w", "host": "*"}]}, "act on the run"),
    ({"version": 1, "mode": "audit", "canary": {"on_read": "ask"}}, "act on the run"),
])
def test_an_audit_ruleset_is_checked(document, message):
    with pytest.raises(rt.RuleError, match=message):
        rt.Ruleset.validated(document, "trial")


# --- the built-in rulesets ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, decision", [
    ("POST", "/repos/o/r/keys", "ask"), ("POST", "/user/keys", "ask"), ("POST", "/user/gpg_keys", "ask"),
    ("POST", "/repos/o/r/hooks", "ask"), ("PATCH", "/repos/o/r/hooks/12", "ask"), ("POST", "/orgs/o/hooks", "ask"),
    ("PUT", "/repos/o/r/contents/.github/workflows/ci.yml", "ask"), ("PUT", "/repos/o/r/actions/secrets/TOKEN", "ask"),
    ("PUT", "/repos/o/r/collaborators/mallory", "ask"), ("DELETE", "/repos/o/r/branches/main/protection", "ask"),
    ("PUT", "/user/installations/1/repositories/2", "ask"),
    ("GET", "/repos/o/r/keys", "allow"), ("POST", "/repos/o/r/issues", "allow"), ("GET", "/repos/o/r/hooks", "allow"),
    ("PUT", "/repos/o/r/contents/src/main.py", "allow"), ("POST", "/repos/o/r/pulls", "allow"),
])
def test_github_persistence(method, path, decision):
    verdict = ruleset(builtin("github-persistence")).http(request(method, "api.github.com", path))
    assert verdict["decision"] == decision, verdict


@pytest.mark.parametrize("command, decision", [
    ("rm -rf /work/src", "ask"), ("rm -r build", "ask"), ("cd /work && rm -fr .", "ask"), ("rm --recursive x", "ask"),
    ("rm -rf /tmp/build", "allow"), ("rm notes.txt", "allow"), ("rm -f out.log", "allow"),
    ("git push --force origin main", "ask"), ("git push -f", "ask"), ("git push origin +main", "ask"),
    ("git push origin main", "allow"), ("git reset --hard HEAD~3", "ask"), ("git clean -fdx", "ask"),
    ("git checkout -- .", "ask"), ("git stash drop", "ask"), ("git branch -D feature", "ask"),
    ("git status", "allow"), ("git reset HEAD file", "allow"),
    ("psql -c 'DROP TABLE users'", "ask"), ("sqlite3 db 'truncate table logs'", "ask"),
    ("psql -c 'DELETE FROM users;'", "ask"), ("psql -c 'DELETE FROM users WHERE id = 3'", "allow"),
    ("terraform destroy -auto-approve", "ask"), ("terraform plan", "allow"), ("kubectl delete pod web", "ask"),
    ("kubectl get pods", "allow"), ("docker system prune -af", "ask"), ("mkfs.ext4 /dev/sdb1", "ask"),
    ("dd if=image.iso of=/dev/sdb bs=4M", "ask"), ("dd if=/dev/zero of=file.img bs=1M count=1", "allow"),
    ("find . -name '*.pyc' -delete", "ask"), ("ls -la", "allow"), ("pytest -q", "allow"),
])
def test_destructive(command, decision):
    verdict = ruleset(builtin("destructive")).agent_tool("claude", "Bash", {"command": command})
    assert verdict["decision"] == decision, (command, verdict)


@pytest.mark.parametrize("arguments, decision", [
    ({"token": GH_TOKEN}, "ask"), ({"options": {"auth": [f"Bearer {GH_TOKEN}"]}}, "ask"),
    ({"note": "key -----BEGIN OPENSSH " + "PRIVATE KEY----- x"}, "ask"), ({GH_TOKEN: 1}, "ask"),
    ({"repo": "o/r", "count": 3, "draft": True, "labels": None}, "allow"), ({}, "allow"),
    ({"text": "ghp_ is the prefix of GitHub tokens"}, "allow"),
])
def test_mcp_no_credentials(arguments, decision):
    verdict = ruleset(builtin("mcp-no-credentials")).mcp("github", "star_repo", arguments)
    assert verdict["decision"] == decision, (arguments, verdict)


@pytest.mark.parametrize("tool, arguments, decision", [
    ("Read", {"file_path": "/work/.env"}, "ask"), ("Read", {"file_path": "/work/app/.env.local"}, "ask"),
    ("Read", {"file_path": "/root/.aws/credentials"}, "ask"), ("Read", {"file_path": "/work/certs/server.pem"}, "ask"),
    ("Read", {"file_path": "/root/.ssh/id_ed25519"}, "ask"), ("Grep", {"pattern": "KEY", "path": "/work/.env"}, "ask"),
    ("Read", {"file_path": "/work/src/main.py"}, "allow"), ("Read", {"file_path": "/work/src/env.py"}, "allow"),
    ("Read", {"file_path": "/work/.env.example.md"}, "ask"),
    ("Bash", {"command": "cat .env"}, "ask"), ("Bash", {"command": "source .env && make"}, "ask"),
    ("Bash", {"command": "cat ~/.aws/credentials"}, "ask"), ("Bash", {"command": "ls -la"}, "allow"),
    ("Bash", {"command": "cat README.md"}, "allow"), ("Bash", {"command": "cat ~/.ssh/id_ed25519.pub"}, "allow"),
])
def test_secret_files(tool, arguments, decision):
    facts = rt.HookServer.facts(tool, arguments, "/work")
    verdict = ruleset(builtin("secret-files")).agent_tool("claude", tool, facts)
    assert verdict["decision"] == decision, (tool, arguments, verdict)


def test_every_builtin_ruleset_reads_as_a_rules_file():
    launcher = load("agent_sandbox_builtin_rules", "sandbox/agent_sandbox.py")
    assert launcher.builtin_ruleset_names() == ["destructive", "github-persistence", "mcp-no-credentials",
                                                "secret-files"]
    for name in launcher.builtin_ruleset_names():
        path = launcher.rules_file(f"builtin:{name}")
        assert launcher.ruleset_label(path) == f"builtin:{name}"
        source = launcher.read_ruleset(path, f"builtin:{name}")
        assert source.get("mode") != "audit"
        decisions = {rule["decision"] for section in ("http", "mcp", "tool") for rule in source[section]}
        assert decisions == {"ask"}, "the human decides: every built-in rule asks"
    with pytest.raises(Exception, match="no built-in ruleset"):
        launcher.rules_file("builtin:nope")


# --- end to end: the agent's hook, and the audit report ----------------------------------------------------------------

class Broker:
    def __init__(self):
        self.asked = []

    def ask(self, kind, details, wait=True):
        self.asked.append((kind, details))
        return {"decision": "deny", "scope": "once", "reason": "answered"}


def serve(tmp_path, rules):
    directory = Path(tempfile.mkdtemp(prefix="hooks-", dir="/tmp"))
    broker, log, stop = Broker(), tmp_path / "logs" / "tools.jsonl", threading.Event()
    log.parent.mkdir(exist_ok=True)
    thread = rt.start_hook_server({"agent": "claude", "rules": rules}, broker, rt.Taint(), [],
                                  path=directory / "decide.sock", log=log, stop=stop)

    def hook(command):
        message = {"session_id": "s1", "tool_use_id": "t1", "hook_event_name": "PreToolUse", "cwd": "/work",
                   "tool_name": "Bash", "tool_input": {"command": command}}
        stderr = io.StringIO()
        status = rt.hook_client("claude", "PreToolUse", stdin=io.BytesIO(json.dumps(message).encode()), stderr=stderr,
                                socket_path=directory / "decide.sock", timeout=20)
        return status, stderr.getvalue()

    def done():
        stop.set()
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)
    return hook, broker, log, done


def test_a12_rm_rf_through_the_hook_asks_under_destructive(tmp_path):
    """The report's A12: `rm -rf /work/src` through the Bash tool hook asks under the destructive ruleset."""
    hook, broker, log, done = serve(tmp_path, [builtin("destructive")])
    try:
        status, err = hook("rm -rf /work/src")
        assert status == 2 and "a recursive rm outside /tmp" in err
        assert broker.asked[0][0] == "tool" and broker.asked[0][1]["rule"] == "rm-recursive"
        assert hook("ls /work") == (0, "")
    finally:
        done()


def test_an_audit_copy_lets_it_through_and_the_report_names_it(tmp_path):
    hook, broker, log, done = serve(tmp_path, [builtin("destructive", audit=True)])
    try:
        assert hook("rm -rf /work/src") == (0, "")
        assert hook("git push --force") == (0, "")
        assert hook("ls") == (0, "")
        assert broker.asked == [], "an audit ruleset asks nothing"
    finally:
        done()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["decision"] for r in records] == ["allow", "allow", "allow"]
    assert records[0]["audit"][0] == {"would": "ask", "rule": "rm-recursive", "ruleset": "builtin:destructive",
                                      "reason": "a recursive rm outside /tmp"}
    assert "audit" not in records[2]
    done_ = subprocess.run([str(ROOT / "sandbox-ai"), "rules", "audit", "--log-dir", str(log.parent), "--json"],
                           capture_output=True, text=True, timeout=120)
    assert done_.returncode == 0, done_.stderr
    report = json.loads(done_.stdout)
    assert report["records"] == 2
    assert [(h["rule"], h["would"], h["count"], h["layer"]) for h in report["hits"]] == [
        ("git-force-push", "ask", 1, "tool"), ("rm-recursive", "ask", 1, "tool")]
    assert "rm -rf /work/src" in report["hits"][1]["examples"][0]["what"]
    text = subprocess.run([str(ROOT / "sandbox-ai"), "rules", "audit", "--log-dir", str(log.parent)],
                          capture_output=True, text=True, timeout=120).stdout
    assert "builtin:destructive / rm-recursive: would ask 1 tool action(s)" in text and "nothing of it was" in text


def test_the_cli_lists_prints_and_takes_builtin_rulesets(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}

    def sai(*args):
        done = subprocess.run([str(ROOT / "sandbox-ai"), *args], capture_output=True, text=True, timeout=120,
                              env=env, cwd=tmp_path)
        return done.returncode, done.stdout + done.stderr
    status, out = sai("rules", "builtin")
    assert status == 0 and "builtin:secret-files" in out and "--audit" in out
    status, out = sai("rules", "builtin", "destructive", "--audit")
    assert status == 0 and json.loads(out)["mode"] == "audit"
    saved = tmp_path / "trial.json"
    saved.write_text(out)
    status, out = sai("rules", "check", str(saved), "builtin:github-persistence")
    assert status == 0 and "ok     builtin:github-persistence" in out, out
    status, out = sai("rules", "show", "--", "--rules", "builtin:mcp-no-credentials", "--rules", str(saved))
    assert status == 0 and "builtin:mcp-no-credentials" in out and "trial.json" in out, out
    status, out = sai("claude", "--dry-run", "--rules", "builtin:nope")
    assert status != 0 and "no built-in ruleset 'nope'" in out
