"""The privacy check (sandbox/privacy.py): rules, remotes, what a push would publish, hooks, command line.

Credential-shaped fixtures are assembled at run time so that no scanner takes this file for a leak.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_privacy_tests", ROOT / "sandbox/privacy.py")
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)

GH_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5
PEM = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
OWN_TOKEN = "Zq81-own-token-" + "x" * 12

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    home = tmp_path / "host-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    return home


def git(repo, *args, env=None, check=True):
    environment = {**os.environ, "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.org",
                   "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.org", **(env or {})}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=environment,
                          check=check)


def rules(*extra):
    result = p.builtin_rules()
    for id_, regex, severity in extra:
        result.patterns.append(p.Pattern(id_, p.re.compile(regex), severity, f"{id_} reason"))
    return result


def ids(report):
    return sorted((f.where, f.pattern.id) for f in report.findings)


# --- rules -----------------------------------------------------------------------------------------

def test_builtin_rules_find_credentials_and_mask_them():
    built = p.builtin_rules()
    found = p.scan_text(f"token = '{GH_TOKEN}'\n{PEM}\nplain text\n", built, "x.py", lambda n: f"x.py:{n}")
    assert [(f.where, f.pattern.id) for f in found] == [("x.py:1", "github-token"), ("x.py:2", "private-key")]
    assert GH_TOKEN not in found[0].render(built) and "[github-token]" in found[0].render(built)


def test_derived_rules_name_what_is_yours():
    context = {"homes": ["/home/ada"], "email": "ada@mail.example", "hostname": "host7",
               "addresses": ["192.168.88.7", "127.0.0.1", "203.0.113.9"],
               "servers": [{"host": "localhost", "port": 3080, "user": "ada42"},
                           {"host": "github.com", "port": 443, "user": "public-handle"},
                           {"host": "git.lan", "port": 443, "user": "oauth2"}],
               "tokens": [OWN_TOKEN]}
    derived = p.derived_rules(context)
    text = "\n".join(["/home/ada/src/x", "/home/adams/other", "mail ada@MAIL.example", "on host7.",
                      "http://192.168.88.7:1234", "192.168.88.70", "127.0.0.1", "203.0.113.9",
                      "http://localhost:3080/ada42/repo", "localhost:30801", "github.com/public-handle",
                      "git.lan/x", "oauth2", f"secret={OWN_TOKEN}"])
    found = {(f.where, f.pattern.id, f.pattern.severity) for f in p.scan_text(text, derived, "", str)}
    assert found == {("1", "host-home", "block"), ("3", "git-email", "warn"), ("4", "host-name", "warn"),
                     ("5", "private-address", "block"), ("9", "own-git-server", "warn"),
                     ("9", "own-git-account", "block"), ("12", "own-git-server", "block"),
                     ("14", "own-token", "block")}
    assert OWN_TOKEN not in p.shown(f"secret={OWN_TOKEN}", derived)
    assert p.private_hosts(context) == ["git.lan", "localhost"]


def test_derived_rules_skip_generic_and_public_values():
    derived = p.derived_rules({"homes": [], "email": "1+x@users.noreply.github.com", "hostname": "debian",
                               "servers": [{"host": "github.com", "port": 443, "user": "someone"}],
                               "tokens": ["short"]})
    assert derived.patterns == []


def test_rule_files_in_scrub_guard_format(tmp_path):
    if p._toml_module() is None:
        pytest.skip("no TOML reader")
    (tmp_path / "global.rules.toml").write_text(
        "[meta]\nversion = 1\n\n[[pattern]]\nid = \"codename\"\nregex = '(?i)\\bbluefin\\b'\n"
        "severity = \"block\"\nreason = \"codename\"\nreplacement = \"x\"\n\n"
        "[[allow]]\nid = \"doc\"\nregex = 'bluefin-docs'\npath_glob = \"docs/*\"\nreason = \"public name\"\n\n"
        "[[exclude]]\nid = \"e\"\npath_glob = \"private/*\"\nreason = \"export only\"\n")
    (tmp_path / "proj.rules.json").write_text(json.dumps({"pattern": [
        {"id": "ticket", "regex": "INT-[0-9]+", "severity": "warn", "reason": "internal ticket"}]}))
    loaded = p.user_rules(tmp_path, "proj")
    assert [x.id for x in loaded.patterns] == ["codename", "ticket"] and len(loaded.sources) == 2
    assert [f.pattern.id for f in p.scan_text("Bluefin INT-7", loaded, "a", str)] == ["codename", "ticket"]
    assert p.scan_text("bluefin-docs", loaded, "docs/a.md", str) == []
    assert len(p.scan_text("bluefin-docs", loaded, "src/a.md", str)) == 1
    assert [x.id for x in p.user_rules(tmp_path, "other").patterns] == ["codename"]


@pytest.mark.parametrize("files, message", [
    ({"global.rules.json": "{"}, "invalid JSON"),
    ({"global.rules.json": '{"patterns": []}'}, "unknown section"),
    ({"global.rules.json": '{"pattern": [{"id": "a", "regex": "x", "severity": "high", "reason": "r"}]}'},
     "unknown severity"),
    ({"global.rules.json": '{"pattern": [{"id": "a", "regex": "(", "severity": "warn", "reason": "r"}]}'},
     "uncompilable"),
    ({"global.rules.json": '{"pattern": [{"id": "a", "severity": "warn", "reason": "r"}]}'}, "missing 'regex'"),
    ({"global.rules.json": "{}", "global.rules.toml": ""}, "keep one"),
])
def test_broken_rule_files_are_refused(tmp_path, files, message):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    with pytest.raises(p.RulesUnavailable, match=message):
        p.user_rules(tmp_path, "proj")


def test_rule_files_must_be_real(tmp_path):
    with pytest.raises(p.RulesUnavailable, match="does not exist"):
        p.user_rules(tmp_path / "missing", "proj")
    (tmp_path / "elsewhere.json").write_text("{}")
    (tmp_path / "global.rules.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(p.RulesUnavailable, match="symlink"):
        p.user_rules(tmp_path, "proj")


def test_toml_without_a_reader_is_refused_not_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_toml_module", lambda: None)
    (tmp_path / "global.rules.toml").write_text("")
    with pytest.raises(p.RulesUnavailable, match="global.rules.json"):
        p.user_rules(tmp_path, "proj")


# --- remotes ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("url, public", [
    ("https://github.com/o/r.git", True), ("git@github.com:o/r.git", True), ("ssh://git@gitlab.com/o/r", True),
    ("https://git.example.com/o/r", True), ("http://localhost:3000/o/r.git", False),
    ("http://alice@127.0.0.1:3000/o/r", False), ("http://[::1]:3000/r", False), ("https://192.168.1.5/r", False),
    ("git@nas.lan:r.git", False), ("gitea:r.git", False), ("file:///srv/r.git", False), ("/srv/r.git", False),
    ("../r.git", False), ("https://git.mine.example/r", False),
])
def test_remote_is_public(url, public):
    assert p.remote_is_public(url, ["git.mine.example"]) == public


# --- what a push would publish ---------------------------------------------------------------------

def published_repo(tmp_path):
    """A repository with one pushed commit (holding an old finding) and an origin."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "old.txt").write_text("codename bluefin, already public\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return repo


@needs_git
def test_scan_reports_only_what_the_next_push_adds(tmp_path):
    repo = published_repo(tmp_path)
    (repo / "added.txt").write_text(f"x\nkey={GH_TOKEN}\n")
    git(repo, "add", "added.txt")
    git(repo, "commit", "-qm", "add a file\n\nsee bluefin")
    (repo / "added.txt").write_text("cleaned up\n")  # the commit still holds the token
    git(repo, "add", "added.txt")
    git(repo, "commit", "-qm", "clean up")
    (repo / "staged.txt").write_text("staged bluefin\n")
    git(repo, "add", "staged.txt")
    (repo / "old.txt").write_text("codename bluefin, already public\nnow bluefin again\n")
    (repo / "new.txt").write_text("bluefin untracked\n")
    (repo / "ignored.txt").write_text("bluefin ignored\n")
    report = p.scan_repository(repo, rules(("codename", r"\bbluefin\b", "block")))
    found = ids(report)
    commit = next(where for where, id_ in found if id_ == "github-token").split("(commit ")[1].rstrip(")")
    assert found == sorted([(f"added.txt:2 (commit {commit})", "github-token"),
                            (f"commit {commit} message", "codename"),
                            ("new.txt:1 (untracked)", "codename"), ("old.txt:2 (not staged)", "codename"),
                            ("staged.txt:1 (staged)", "codename")])
    assert len(report.blocking) == 5
    everything = p.scan_repository(repo, rules(("codename", r"\bbluefin\b", "block")), everything=True)
    assert ("old.txt:1 (not staged)", "codename") in ids(everything)


@needs_git
def test_scan_checks_author_and_committer(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a").write_text("a\n")
    git(repo, "add", "a")
    git(repo, "commit", "-qm", "one", env={"GIT_AUTHOR_EMAIL": "ada@mail.example"})
    derived = p.derived_rules({"email": "ada@mail.example"})
    assert [(f.where.split()[-1], f.pattern.id) for f in p.scan_repository(repo, derived).findings] == \
        [("author", "git-email")]


@needs_git
def test_scan_follows_no_symlink_and_runs_nothing_from_the_repository(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    secret = tmp_path / "outside.txt"
    secret.write_text(f"{GH_TOKEN}\n")
    (repo / "link.txt").symlink_to(secret)
    (repo / "dir").symlink_to(tmp_path)
    marker = tmp_path / "ran"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    script.chmod(0o755)
    (repo / ".gitattributes").write_text("* filter=evil diff=evil\n")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt", ".gitattributes")
    git(repo, "commit", "-qm", "one")
    for key in ("core.fsmonitor", "filter.evil.clean", "filter.evil.smudge", "diff.evil.textconv",
                "core.pager", "diff.external"):
        git(repo, "config", key, str(script))
    (repo / "a.txt").write_text("b\n")
    report = p.scan_repository(repo, p.builtin_rules())
    assert report.findings == [] and not marker.exists()


@needs_git
def test_scan_refuses_what_is_not_a_repository(tmp_path):
    with pytest.raises(p.RulesUnavailable, match="not a Git repository"):
        p.scan_repository(tmp_path, p.builtin_rules())


@needs_git
def test_check_after_run_only_for_public_remotes(tmp_path):
    repo = published_repo(tmp_path)
    (repo / "new.txt").write_text("bluefin\n")
    lines = []
    check = rules(("codename", r"\bbluefin\b", "block"))
    p.check_after_run([(repo, "repo")], lambda project: check, [], "public", lines.append)
    assert lines == []  # origin is a local path: nothing is published
    p.check_after_run([(repo, "repo")], lambda project: check, [], "always", lines.append)
    assert "1 blocking" in lines[0] and "new.txt:1 (untracked)" in lines[1]
    lines.clear()
    git(repo, "remote", "add", "public", "https://github.com/o/r.git")
    p.check_after_run([(repo, "repo"), (tmp_path / "not-a-repo", "x")], lambda project: check, [], "public",
                      lines.append)
    assert "(remote github.com)" in lines[0] and len(lines) == 3


# --- hooks in the sandbox --------------------------------------------------------------------------

def hooked_repo(tmp_path):
    engine = tmp_path / "engine"
    p.write_hooks(engine, engine / "privacy.py", p.derived_rules({"homes": ["/home/ada"]}), ["git.lan"])
    repo = published_repo(tmp_path)
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(engine / "hooks")}
    own = repo / ".git" / "hooks" / "pre-push"
    own.write_text(f"#!/bin/sh\necho \"own $*\" > {tmp_path / 'own.txt'}\ncat >> {tmp_path / 'own.txt'}\n")
    own.chmod(0o755)
    return engine, repo, env


@needs_git
def test_pre_commit_warns_and_commits(tmp_path):
    _, repo, env = hooked_repo(tmp_path)
    (repo / "conf.py").write_text("ROOT = '/home/ada/work'\n")
    git(repo, "add", "conf.py")
    result = git(repo, "commit", "-m", "conf", env=env)
    assert result.returncode == 0 and "conf.py:1 [host-home] block" in result.stderr
    assert "a push of it to a public remote will be refused" in result.stderr


@needs_git
def test_pre_push_to_a_public_remote_refuses_blocking_findings(tmp_path):
    engine, repo, env = hooked_repo(tmp_path)
    (repo / "conf.py").write_text(f"ROOT = '/home/ada/work'\nT = '{GH_TOKEN}'\n")
    git(repo, "add", "conf.py")
    git(repo, "commit", "-qm", "conf")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    pushed = git(repo, "rev-parse", "origin/main").stdout.strip()
    line = f"refs/heads/main {head} refs/heads/main {pushed}\n"

    def hook(url):
        return subprocess.run([str(engine / "hooks" / "pre-push"), "origin", url], input=line, cwd=repo,
                              capture_output=True, text=True, env={**os.environ, **env})
    refused = hook("https://github.com/o/r.git")
    assert refused.returncode == 1 and "Push refused" in refused.stderr
    assert "conf.py:1 (commit" in refused.stderr and GH_TOKEN not in refused.stderr
    assert not (tmp_path / "own.txt").exists()
    # Your own server and this machine are no publication; the repository's own hook still runs, with stdin.
    for url in ("https://git.lan/o/r.git", str(tmp_path / "origin.git")):
        allowed = hook(url)
        assert allowed.returncode == 0 and allowed.stderr == ""
        assert (tmp_path / "own.txt").read_text() == f"own origin {url}\n{line}"
    # A real push to the local origin passes through git itself.
    assert git(repo, "push", "-q", "origin", "main", env=env).returncode == 0


@needs_git
def test_pre_push_fails_closed_for_a_public_remote(tmp_path):
    engine, repo, env = hooked_repo(tmp_path)
    (engine / "hook.json").write_text("{")
    result = subprocess.run([str(engine / "hooks" / "pre-push"), "origin", "https://github.com/o/r.git"],
                            input="", cwd=repo, capture_output=True, text=True, env={**os.environ, **env})
    assert result.returncode == 1 and "the check failed" in result.stderr


# --- command line ----------------------------------------------------------------------------------

@needs_git
def test_privacy_check_command(tmp_path, capsys):
    repo = published_repo(tmp_path)
    assert p.main(["check", str(repo)]) == 0
    (repo / "new.txt").write_text(f"{PEM}\n")
    assert p.main([str(repo)]) == 1
    out = capsys.readouterr().out
    assert "1 blocking" in out and "new.txt:1 (untracked) [private-key]" in out
    assert p.main(["check", "--json", str(repo)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result[0]["findings"][0]["id"] == "private-key"
    assert p.main(["check", str(tmp_path / "missing")]) == 2
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "repo.rules.json").write_text(json.dumps({"pattern": [
        {"id": "codename", "regex": r"\bbluefin\b", "severity": "block", "reason": "codename"}]}))
    assert p.main(["check", "--all", "--rules-dir", str(rules_dir), str(repo)]) == 1
    assert "old.txt:1 (not staged) [codename]" in capsys.readouterr().out


def test_module_parses_as_python_3_9():
    """The launcher supports Python 3.9; the engine also runs inside the agent's image."""
    import ast
    ast.parse((ROOT / "sandbox/privacy.py").read_text(), feature_version=(3, 9))


# --- push check for the connection proxy ------------------------------------------------------------

def repo_with_commits(tmp_path, *files):
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    ids_ = []
    for name, content in files:
        (repo / name).write_text(content)
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", f"add {name}")
        ids_.append(git(repo, "rev-parse", "HEAD").stdout.strip())
    return repo, ids_


def config_for(repo, mode="public"):
    return {"mode": mode, "own_hosts": ["git.home.example"],
            "workspaces": [{"path": str(repo), "rules": p.rules_document(p.Rules())}]}


@needs_git
def test_push_verdict_checks_only_what_the_remote_lacks(tmp_path):
    repo, (first, second) = repo_with_commits(tmp_path, ("old.txt", f"token {GH_TOKEN}\n"), ("new.txt", "fine\n"))
    url, zero = "https://github.com/ada/x.git", "0" * 40
    # The remote has the first commit already (advertised): only the second, clean one is new.
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [[first, second, "refs/heads/main"]],
                                                 "advertised": [first]})
    assert verdict["ok"], verdict
    # A new branch on an empty remote publishes both.
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [[zero, second, "refs/heads/main"]]})
    assert not verdict["ok"] and "old.txt" in verdict["message"] and GH_TOKEN not in verdict["message"]
    # Own and private servers are not publications (mode public), unless the mode is always.
    for own in ("https://git.home.example/x.git", "http://127.0.0.1:3000/x.git"):
        assert p.push_verdict(config_for(repo), {"url": own, "commands": [[zero, second, "refs/heads/main"]]})["ok"]
    assert not p.push_verdict(config_for(repo, "always"), {"url": "http://127.0.0.1:3000/x.git",
                                                            "commands": [[zero, second, "refs/heads/main"]]})["ok"]


@needs_git
def test_push_verdict_refuses_what_it_cannot_check(tmp_path):
    repo, (first,) = repo_with_commits(tmp_path, ("a.txt", "a\n"))
    url = "https://github.com/ada/x.git"
    unknown = p.push_verdict(config_for(repo), {"url": url, "commands": [["0" * 40, "c" * 40, "refs/heads/x"]]})
    assert not unknown["ok"] and "in no workspace" in unknown["message"]
    bad = p.push_verdict(config_for(repo), {"url": url, "commands": [["0" * 40, "not-an-id", "refs/heads/x"]]})
    assert not bad["ok"]
    assert p.push_verdict(config_for(repo), {"url": url, "commands": [[first, "0" * 40, "refs/heads/x"]]})["ok"]
    # An annotated tag is peeled to its commit.
    git(repo, "tag", "-a", "-m", "v1", "v1")
    tag = git(repo, "rev-parse", "v1").stdout.strip()
    assert p.present(p.Git(repo), [tag, "d" * 40]) == {tag: first}


@needs_git
def test_serve_answers_line_by_line_and_fails_closed(tmp_path):
    import io
    repo, (first,) = repo_with_commits(tmp_path, ("a.txt", "a\n"))
    (tmp_path / "push.json").write_text(json.dumps(config_for(repo)))
    requests = io.StringIO(json.dumps({"url": "https://github.com/a/b", "commands": [["0" * 40, first, "refs/heads/m"]]})
                           + "\nnot json\n")
    out = io.StringIO()
    assert p.serve(tmp_path / "push.json", requests, out) == 0
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answers[0]["ok"] and not answers[1]["ok"] and "failed" in answers[1]["message"]


@needs_git
def test_replace_objects_cannot_hide_a_commit_from_the_check(tmp_path):
    """`git replace` would show the check a clean commit while the push sends the real one."""
    repo, (secret, clean) = repo_with_commits(tmp_path, ("leak.txt", f"token {GH_TOKEN}\n"), ("ok.txt", "fine\n"))
    git(repo, "replace", secret, clean)
    verdict = p.push_verdict(config_for(repo), {"url": "https://github.com/a/b",
                                                 "commands": [["0" * 40, secret, "refs/heads/main"]]})
    assert not verdict["ok"] and "leak.txt" in verdict["message"]


@needs_git
@pytest.mark.parametrize("host", [True, False])
def test_repository_config_cannot_run_a_program_through_a_lazy_fetch(tmp_path, host, monkeypatch):
    """A partial clone fetches missing objects by running the promisor remote's upload-pack command,
    which the (agent-written) repository configuration names."""
    repo, (first,) = repo_with_commits(tmp_path, ("a.txt", "a\n"))
    marker = tmp_path / "PWNED"
    for key, value in (("core.repositoryformatversion", "1"), ("extensions.partialclone", "evil"),
                       ("remote.evil.promisor", "true"), ("remote.evil.url", str(tmp_path)),
                       ("remote.evil.uploadpack", f"touch {marker}; git-upload-pack")):
        git(repo, "config", key, value)
    monkeypatch.chdir(repo)  # the hooks' Git (host=False) works in the current directory
    g = p.Git(repo, host=host)
    assert p.present(g, ["e" * 40, first]) == {first: first}
    p.read_blobs(g, ["f" * 40], [1 << 20])
    assert not marker.exists()
    verdict = p.push_verdict(config_for(repo), {"url": "https://github.com/a/b",
                                                 "commands": [["0" * 40, "e" * 40, "refs/heads/x"]]})
    assert not verdict["ok"] and not marker.exists()


@needs_git
def test_push_check_reads_merges_and_tags_and_refuses_what_it_cannot_read(tmp_path, monkeypatch):
    repo, (base,) = repo_with_commits(tmp_path, ("a.txt", "a\n"))
    url = "https://github.com/a/b"
    # A token that only a merge commit brings in.
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "side")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    (repo / "merge.txt").write_text(f"token {GH_TOKEN}\n")
    git(repo, "add", "merge.txt")
    git(repo, "commit", "-q", "-m", "merge")
    merge = git(repo, "rev-parse", "HEAD").stdout.strip()
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [["0" * 40, merge, "refs/heads/main"]],
                                                 "advertised": [base]})
    assert not verdict["ok"] and "merge.txt" in verdict["message"]
    # A token in an annotated tag's message.
    git(repo, "tag", "-a", "-m", f"release {GH_TOKEN}", "v2", base)
    tag = git(repo, "rev-parse", "v2").stdout.strip()
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [["0" * 40, tag, "refs/tags/v2"]],
                                                 "advertised": [base]})
    assert not verdict["ok"] and "tag" in verdict["message"]
    # More than it reads: refused, not waved through.
    monkeypatch.setattr(p, "COMMITS_LIMIT", 1)
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [["0" * 40, merge, "refs/heads/main"]]})
    assert not verdict["ok"]


@needs_git
def test_large_binary_files_do_not_block_but_large_text_does(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "PUSH_FILE_LIMIT", 100)
    repo, (binary,) = repo_with_commits(tmp_path, ("image.png", "\x89PNG\x00" + "x" * 500))
    url = "https://github.com/a/b"
    assert p.push_verdict(config_for(repo), {"url": url, "commands": [["0" * 40, binary, "refs/heads/main"]]})["ok"]
    (repo / "big.txt").write_text("text\n" * 100)
    git(repo, "add", "big.txt")
    git(repo, "commit", "-q", "-m", "big")
    text = git(repo, "rev-parse", "HEAD").stdout.strip()
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [[binary, text, "refs/heads/main"]],
                                                 "advertised": [binary]})
    assert not verdict["ok"] and "cannot be checked completely" in verdict["message"] and "big.txt" in verdict["message"]


@needs_git
def test_push_check_reads_nested_tags_extra_headers_and_file_names(tmp_path):
    repo, (base,) = repo_with_commits(tmp_path, ("a.txt", "a\n"))
    url = "https://github.com/a/b"
    # A tag of a tag: the inner message is published as well.
    git(repo, "tag", "-a", "-m", f"inner {GH_TOKEN}", "inner", base)
    git(repo, "tag", "-a", "-m", "outer", "outer", "inner")
    outer = git(repo, "rev-parse", "outer").stdout.strip()
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [["0" * 40, outer, "refs/tags/outer"]],
                                                 "advertised": [base]})
    assert not verdict["ok"] and "tag" in verdict["message"]
    # An extra header line in a commit object.
    tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    raw = (f"tree {tree}\nparent {base}\nauthor Ada <ada@example.org> 0 +0000\ncommitter Ada <ada@example.org> 0 +0000\n"
           f"x-note {GH_TOKEN}\n\nplain message\n")
    odd = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--literally", "--stdin"],
                         input=raw, capture_output=True, text=True, check=True).stdout.strip()
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [[base, odd, "refs/heads/x"]],
                                                 "advertised": [base]})
    assert not verdict["ok"] and "header" in verdict["message"]
    # A token in a file name.
    (repo / f"key-{GH_TOKEN}.txt").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "named")
    named = git(repo, "rev-parse", "HEAD").stdout.strip()
    verdict = p.push_verdict(config_for(repo), {"url": url, "commands": [[base, named, "refs/heads/main"]],
                                                 "advertised": [base]})
    assert not verdict["ok"] and "file name" in verdict["message"]


@needs_git
def test_many_advertised_refs_do_not_break_the_check(tmp_path):
    repo, (first, second) = repo_with_commits(tmp_path, ("a.txt", "a\n"), ("b.txt", "b\n"))
    advertised = [first] * 60000 + ["9" * 40] * 60000  # more than a command line holds
    verdict = p.push_verdict(config_for(repo), {"url": "https://github.com/a/b",
                                                 "commands": [[first, second, "refs/heads/main"]], "advertised": advertised})
    assert verdict["ok"], verdict
