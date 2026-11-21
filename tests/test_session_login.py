"""Agent logins of sessions (session_login.py): `sandbox-ai login --status --sessions`, `sandbox-ai login AGENT
--from-session NAME` and the start option --auth-from-session NAME. Fixture HOMEs per agent the central store takes
(claude, codex, opencode, qwen, pi, hermes), hostile ones (a link out, a FIFO, a huge file, escape sequences, deep
nesting, broken JSON, a HOME that is a link), the write-back into an ended session only, and the real entry point
(./sandbox-ai login ...). No token value may appear in any output, log or JSON."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_session_login_test", ROOT / "sandbox/session_login.py")
sl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sl)
WORKSPACE = "/srv/project"
FUTURE_MS = int((time.time() + 6 * 3600) * 1000)
SECRET = "SECRET-TOKEN-VALUE"  # every token of the fixtures holds it: it may appear nowhere


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def jwt(claims):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.{SECRET}-signature"


def claude_files(account="acc-1", refresh="1", email="me@example.com"):
    credentials = {"claudeAiOauth": {"accessToken": f"{SECRET}-access-{refresh}", "refreshToken":
                                     f"{SECRET}-refresh-{refresh}", "expiresAt": FUTURE_MS, "scopes": ["user:inference"],
                                     "subscriptionType": "max"},
                   "mcpOAuth": {"server": {"accessToken": f"{SECRET}-mcp"}}}
    state = {"oauthAccount": {"emailAddress": email, "organizationName": "Example Org", "accountUuid": account,
                              "organizationUuid": "org-1"},
             "userID": "user-1", "hasCompletedOnboarding": True, "projects": {"/work": {"history": ["x"]}},
             "mcpServers": {"x": {"env": {"API_KEY": f"{SECRET}-mcp-env"}}}}
    return {".claude/.credentials.json": json.dumps(credentials), ".claude.json": json.dumps(state)}


def codex_files(account="acct-codex-1", refresh="1"):
    document = {"tokens": {"id_token": jwt({"email": "dev@example.com",
                                            "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}),
                           "access_token": jwt({"exp": int(time.time()) + 3600, "x": SECRET}),
                           "refresh_token": f"{SECRET}-refresh-{refresh}", "account_id": account},
                "last_refresh": "2026-11-17T10:00:00Z"}
    return {".codex/auth.json": json.dumps(document)}


FIXTURES = {
    "claude": claude_files,
    "codex": codex_files,
    "opencode": lambda: {".local/share/opencode/auth.json": json.dumps({
        "anthropic": {"type": "oauth", "access": f"{SECRET}-a", "refresh": f"{SECRET}-r", "expires": FUTURE_MS,
                      "accountId": "oc-1"},
        "openai": {"type": "api", "key": f"{SECRET}-key"}})},
    "pi": lambda: {".pi/agent/auth.json": json.dumps({
        "anthropic": {"type": "oauth", "access": f"{SECRET}-a", "refresh": f"{SECRET}-r", "expires": FUTURE_MS}})},
    "qwen": lambda: {".qwen/oauth_creds.json": json.dumps({
        "access_token": f"{SECRET}-a", "refresh_token": f"{SECRET}-r", "expiry_date": FUTURE_MS,
        "resource_url": "portal.qwen.ai"})},
    "hermes": lambda: {".hermes/auth.json": json.dumps({
        "version": 1, "providers": {"nous": {"access_token": f"{SECRET}-a", "refresh_token": f"{SECRET}-r"}}})},
}


def put(root, files):
    for relative, data in files.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())


def kept(tmp_path, key, agent="claude", options=(), files=None, keep=True):
    """A start KEY of AGENT that ended: its HOME kept (with FILES in it), or not (KEEP False)."""
    directory = tmp_path / "state" / "sandbox-ai" / "sessions" / key
    directory.mkdir(parents=True)
    if keep:
        (directory / "home").mkdir(mode=0o700)
        put(directory / "home", FIXTURES[agent]() if files is None else files)
    data = {"agent": agent, "options": ["--workspace", WORKSPACE, *options], "agent_args": [], "backend": "podman",
            "workspace": WORKSPACE, "cwd": "/", "created": "2026-11-18T00:00:00Z", "keep": keep}
    (directory / "session.json").write_text(json.dumps(data))
    return directory / "home"


@pytest.fixture
def running(monkeypatch):
    """Make the start KEY count as running (a real running supervisor needs Podman): its entry's state."""
    starts = sl.inspect().module("starts")
    real = starts.entries

    def make(*keys):
        def entries(include_all=False, prune=True):
            found = real(include_all, prune)
            for entry in found:
                if entry.get("id") in keys:
                    entry["state"], entry["run"] = "running", None
            return found
        monkeypatch.setattr(starts, "entries", entries)
    return make


def central_store(tmp_path, agent):
    return tmp_path / "data" / "sandbox-ai" / "auth" / agent


def no_secret(*texts):
    for text in texts:
        assert SECRET not in str(text), "a token value leaked"


# --- what a login says about itself -----------------------------------------------------------------------------------

@pytest.mark.parametrize("agent", sorted(FIXTURES))
def test_every_agent_login_is_described_without_a_token(agent):
    files = sl.stored_form(agent, {k: v.encode() for k, v in FIXTURES[agent]().items()})
    info = sl.describe(agent, files)
    assert sl.FINGERPRINT.fullmatch(info["fingerprint"]) and info["agent"] == agent
    no_secret(json.dumps(sl.public(info)), "\n".join(sl.info_lines(info)))
    expected = {"claude": "subscription", "codex": "subscription", "opencode": "subscription+api-key",
                "pi": "subscription", "qwen": "subscription", "hermes": "subscription"}
    assert info["kind"] == expected[agent]
    if agent == "claude":
        assert info["account"]["email"] == "me@example.com" and info["account"]["plan"] == "max"
        assert info["account"]["organization"] == "Example Org" and info["expires"].endswith("Z")
        assert "as the file says" in "\n".join(sl.info_lines(info))
        stored = json.loads(files[".claude/.credentials.json"])
        assert set(stored) == {"claudeAiOauth"}, "the MCP servers' tokens stay out"
        assert set(json.loads(files[".claude.json"])) == {"oauthAccount", "userID", "hasCompletedOnboarding"}
    if agent == "codex":
        assert info["account"] == {"email": "dev@example.com", "organization": None, "plan": "pro",
                                   "account_id": "acct-codex-1", "providers": []}
        assert info["expires"] and info["last_refresh"] == "2026-11-17T10:00:00Z"
    if agent == "opencode":
        assert info["account"]["providers"] == ["anthropic (oauth)", "openai (api)"]


def test_the_fingerprint_is_the_same_for_the_same_login_and_another_for_another():
    one = sl.describe("claude", sl.stored_form("claude", {k: v.encode() for k, v in claude_files().items()}))
    again = sl.describe("claude", sl.stored_form("claude", {k: v.encode() for k, v in claude_files().items()}))
    other = sl.describe("claude", sl.stored_form("claude", {k: v.encode() for k, v in claude_files(refresh="2").items()}))
    assert one["fingerprint"] == again["fingerprint"] != other["fingerprint"]
    assert one["fingerprint"] == hashlib.sha256(json.dumps(
        {"accessToken": f"{SECRET}-access-1", "expiresAt": FUTURE_MS, "refreshToken": f"{SECRET}-refresh-1"},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:8]


def test_codex_api_key_and_unverified_claims():
    files = {".codex/auth.json": json.dumps({"OPENAI_API_KEY": f"{SECRET}-key"}).encode()}
    info = sl.describe("codex", sl.stored_form("codex", files))
    assert info["kind"] == "api-key" and info["identity"] is None and info["account"]["email"] is None
    hostile = codex_files()
    document = json.loads(hostile[".codex/auth.json"])
    document["tokens"]["id_token"] = jwt({"email": "\x1b[31mevil\x1b]8;;http://x\x07@example.com‮"})
    info = sl.describe("codex", sl.stored_form("codex", {".codex/auth.json": json.dumps(document).encode()}))
    assert "\x1b" not in info["account"]["email"] and "‮" not in info["account"]["email"]
    assert "\\x1b" in info["account"]["email"]
    document["tokens"]["id_token"] = "not.a-jwt"
    assert sl.describe("codex", sl.stored_form("codex", {".codex/auth.json": json.dumps(document).encode()}))[
        "account"]["email"] is None


# --- hostile HOMEs ----------------------------------------------------------------------------------------------------

def test_a_login_file_that_is_a_link_is_not_followed(tmp_path):
    home = kept(tmp_path, "linked", files={})
    outside = tmp_path / "outside.json"
    outside.write_text(claude_files()[".claude/.credentials.json"])
    (home / ".claude").mkdir()
    os.symlink(outside, home / ".claude/.credentials.json")
    with pytest.raises(ValueError, match="link"):
        sl.read_login(home, "claude")
    folder = kept(tmp_path, "linkdir", agent="codex", files={})
    os.symlink(tmp_path, folder / ".codex")
    with pytest.raises(ValueError, match="link"):
        sl.read_login(folder, "codex")
    linked_home = tmp_path / "linked-home"
    os.symlink(home, linked_home)
    with pytest.raises((ValueError, OSError)):
        sl.read_login(linked_home, "claude")


def test_a_fifo_a_folder_and_a_huge_file_are_refused_without_hanging(tmp_path):
    home = kept(tmp_path, "fifo", agent="codex", files={})
    (home / ".codex").mkdir()
    os.mkfifo(home / ".codex/auth.json")
    with pytest.raises(ValueError, match="not a regular file"):
        sl.read_login(home, "codex")
    os.unlink(home / ".codex/auth.json")
    (home / ".codex/auth.json").mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        sl.read_login(home, "codex")
    (home / ".codex/auth.json").rmdir()
    with open(home / ".codex/auth.json", "wb") as stream:
        stream.truncate(sl.LOGIN_LIMIT + 1)
    with pytest.raises(ValueError, match="larger than"):
        sl.read_login(home, "codex")


@pytest.mark.parametrize("content", ["{", "[]", '{"tokens": {"access_token": 5}}', "[" * 100000 + "]" * 100000,
                                     '{"claudeAiOauth": {"accessToken": "x"}}'])
def test_broken_and_deep_json_is_no_login(tmp_path, content):
    for agent, target in (("codex", ".codex/auth.json"), ("claude", ".claude/.credentials.json")):
        home = tmp_path / f"h-{agent}"
        put(home, {target: content})
        with pytest.raises(ValueError):
            sl.read_login(home, agent)


def test_claude_account_state_keeps_only_the_account_subset_of_its_types(tmp_path):
    files = claude_files()
    state = json.loads(files[".claude.json"])
    state["oauthAccount"]["nested"] = {"deep": [1, 2]}
    state["theme"] = ["not", "a", "string"]
    state["userID"] = "x" * 10000
    files[".claude.json"] = json.dumps(state)
    stored = sl.stored_form("claude", {k: v.encode() for k, v in files.items()})
    kept_state = json.loads(stored[".claude.json"])
    assert set(kept_state) == {"oauthAccount", "hasCompletedOnboarding"}
    assert "nested" not in kept_state["oauthAccount"]


# --- login --status --sessions ----------------------------------------------------------------------------------------

def test_the_sessions_and_their_logins(tmp_path, running):
    kept(tmp_path, "alpha", "claude")
    kept(tmp_path, "beta", "codex", options=("--auth-from-session", "gamma"))
    kept(tmp_path, "gamma", "codex", options=("--no-auth",), files={})
    kept(tmp_path, "delta", "qwen", keep=False)
    kept(tmp_path, "omega", "goose", files={})  # (no login sandbox-ai takes: not listed)
    sl.login_module().write_store("claude", sl.stored_form("claude", {k: v.encode() for k, v in claude_files().items()}))
    running("alpha")
    result = sl.sessions()
    rows = {row["name"]: row for row in result["sessions"]}
    assert set(rows) == {"alpha", "beta", "gamma"}, "an ended start without its HOME is no session any more"
    assert rows["alpha"]["source"] == "store" and rows["alpha"]["shares_central"] and rows["alpha"]["login"]
    assert rows["alpha"]["state"] == "running"
    assert rows["beta"]["source"] == "session" and rows["beta"]["source_session"] == "gamma"
    assert rows["beta"]["info"]["account"]["account_id"] == "acct-codex-1" and not rows["beta"]["shares_central"]
    assert rows["beta"]["info"]["last_refresh"] == "2026-11-17T10:00:00Z"
    assert rows["gamma"]["source"] == "none" and rows["gamma"]["problem"] == "no login in its HOME"
    assert result["central"]["claude"]["fingerprint"] == rows["alpha"]["info"]["fingerprint"]
    assert "identity" not in json.dumps(result)
    lines = "\n".join(sl.sessions_lines(result))
    assert "from session gamma" in lines and "the same login as the central one" in lines
    no_secret(json.dumps(result), lines)


def test_where_a_login_came_from():
    assert sl.source_of("claude", ["--no-auth"]) == ("none", None)
    assert sl.source_of("claude", ["-na"]) == ("none", None)
    assert sl.source_of("claude", ["--auth-from-session=web"]) == ("session", "web")
    assert sl.source_of("claude", ["-as", "web"]) == ("session", "web")
    assert sl.source_of("codex", ["--auth-file", "/x/auth.json"]) == ("file", None)
    assert sl.source_of("codex", ["--copy", "/x/a.json:.codex/auth.json"]) == ("file", None)
    assert sl.source_of("codex", ["--llm", "box"]) == ("none", None)
    assert sl.source_of("codex", []) == ("host", None)


# --- login AGENT --from-session NAME ----------------------------------------------------------------------------------

def args(**values):
    base = {"agent": "claude", "from_session": "alpha", "yes": False, "replace": False, "dry_run": False,
            "expect": None, "json": False, "host": None}
    return argparse.Namespace(**{**base, **values})


def test_import_shows_the_account_asks_and_stores(tmp_path):
    kept(tmp_path, "alpha", "claude")
    said, asked = [], []
    assert sl.cli_import(args(), said.append, ask=lambda q: asked.append(q) or True) == 0
    text = "\n".join(said)
    assert "me@example.com (as the file says)" in text and "No central claude login is stored yet" in text
    assert asked and "central claude login" in asked[0]
    store = central_store(tmp_path, "claude")
    stored = json.loads((store / ".claude/.credentials.json").read_text())
    assert set(stored) == {"claudeAiOauth"} and stored["claudeAiOauth"]["refreshToken"] == f"{SECRET}-refresh-1"
    assert json.loads((store / ".claude.json").read_text())["oauthAccount"]["emailAddress"] == "me@example.com"
    assert oct((store / ".claude/.credentials.json").stat().st_mode & 0o777) == "0o600"
    no_secret(text)
    # the same login again: nothing to do
    said.clear()
    assert sl.cli_import(args(yes=True), said.append) == 0 and "already" in said[-1]


def test_import_refusals(tmp_path):
    kept(tmp_path, "alpha", "claude")
    kept(tmp_path, "other", "claude", files={k: v for k, v in claude_files(account="acc-2", refresh="9").items()})
    kept(tmp_path, "cx", "codex")
    assert sl.cli_import(args(), print, ask=lambda q: False) == 1  # said no
    assert not central_store(tmp_path, "claude").exists()
    with pytest.raises(ValueError, match="no terminal"):
        sl.cli_import(args())
    with pytest.raises(ValueError, match="codex session"):
        sl.cli_import(args(from_session="cx"))
    with pytest.raises(ValueError, match="nothing named"):
        sl.cli_import(args(from_session="nobody"))
    with pytest.raises(ValueError, match="not a start's name"):
        sl.cli_import(args(from_session="../x"))
    with pytest.raises(ValueError, match="changed since it was shown"):
        sl.cli_import(args(yes=True, expect="00000000"))
    assert sl.cli_import(args(yes=True)) == 0
    with pytest.raises(ValueError, match="another account.*--replace"):
        sl.cli_import(args(from_session="other", yes=True))
    assert sl.cli_import(args(from_session="other", yes=True, replace=True)) == 0
    stored = json.loads((central_store(tmp_path, "claude") / ".claude/.credentials.json").read_text())
    assert stored["claudeAiOauth"]["refreshToken"] == f"{SECRET}-refresh-9"


def test_import_of_the_same_account_needs_no_replace_and_dry_run_imports_nothing(tmp_path):
    kept(tmp_path, "one", "codex")
    kept(tmp_path, "two", "codex", files=codex_files(refresh="2"))
    said = []
    assert sl.cli_import(args(agent="codex", from_session="one", dry_run=True, json=True), said.append) == 0
    shown = json.loads(said[0])
    assert shown["login"]["account"]["account_id"] == "acct-codex-1" and shown["central"] is None
    assert not central_store(tmp_path, "codex").exists()
    assert sl.cli_import(args(agent="codex", from_session="one", yes=True)) == 0
    shown = sl.preview("codex", "two")
    assert shown["same_account"] and not shown["needs_replace"] and not shown["same"]
    assert sl.cli_import(args(agent="codex", from_session="two", yes=True)) == 0
    stored = json.loads((central_store(tmp_path, "codex") / ".codex/auth.json").read_text())
    assert stored["tokens"]["refresh_token"] == f"{SECRET}-refresh-2"
    no_secret(json.dumps(shown))


@pytest.mark.parametrize("agent", sorted(FIXTURES))
def test_every_agent_can_be_imported(tmp_path, agent):
    kept(tmp_path, "s1", agent)
    result = sl.import_login(agent, "s1", replace=True)
    assert result["imported"] and sl.central(agent)["fingerprint"] == result["login"]["fingerprint"]
    no_secret(json.dumps(result))


def test_import_on_another_host_asks_here_and_imports_only_what_was_shown(monkeypatch):
    calls = []
    preview = {"agent": "codex", "session": {"name": "web", "id": "web", "state": "stopped"},
               "login": {"agent": "codex", "kind": "subscription", "fingerprint": "abcdef12", "account": {
                   "email": "dev@example.com"}}, "central": None, "same": False, "same_account": False,
               "needs_replace": False, "running": False}

    def run(host, argv, timeout=120, **_):
        calls.append((host, argv))
        if "--dry-run" in argv:
            return 0, json.dumps(preview), ""
        return 0, json.dumps({"imported": True, "agent": "codex", "session": "web", "login": preview["login"],
                              "replaced": None, "notes": []}), ""
    monkeypatch.setattr(sl.inspect().module("hosts"), "run", run)
    said = []
    assert sl.cli_import(args(agent="codex", from_session="web", host="box"), said.append, ask=lambda q: True) == 0
    assert calls == [("box", ["login", "codex", "--from-session", "web", "--dry-run", "--json"]),
                     ("box", ["login", "codex", "--from-session", "web", "--yes", "--expect=abcdef12", "--json"])]
    assert "on box" in said[-1] and "dev@example.com" in "\n".join(said)


# --- --auth-from-session: copied in, written back into an ended session only ------------------------------------------

def start(agent, name, home):
    return argparse.Namespace(agent=agent, auth_from_session=name), Path(home)


def test_a_start_gets_the_sessions_login_and_a_warning(tmp_path):
    source = kept(tmp_path, "src", "codex")
    home = tmp_path / "new-home"
    home.mkdir()
    a, home = start("codex", "src", home)
    warned = []
    copied = sl.copy_into(a, home, warned.append)
    assert copied == [("session src: ~/.codex/auth.json", ".codex/auth.json")]
    assert json.loads((home / ".codex/auth.json").read_text()) == json.loads((source / ".codex/auth.json").read_text())
    assert oct((home / ".codex/auth.json").stat().st_mode & 0o777) == "0o600"
    assert len(warned) == 1 and "rotate their refresh tokens" in warned[0] and "--from-session src" in warned[0]
    assert a.session_login["name"] == "src"
    no_secret(json.dumps(a.session_login), warned)
    # a resumed start keeps its own login
    b, _ = start("codex", "src", home)
    assert sl.copy_into(b, home, warned.append) == [] and b.session_login is None


def test_a_start_from_a_session_of_another_agent_or_without_a_home_is_refused(tmp_path):
    kept(tmp_path, "src", "codex")
    kept(tmp_path, "gone", "codex", keep=False)
    home = tmp_path / "h"
    home.mkdir()
    with pytest.raises(ValueError, match="codex session"):
        sl.copy_into(*start("claude", "src", home))
    with pytest.raises(ValueError, match="--auth-from-session: nothing named gone runs or is kept"):
        sl.copy_into(*start("codex", "gone", home))


def refresh_codex(home, account="acct-codex-1"):
    document = json.loads((home / ".codex/auth.json").read_text())
    document["tokens"]["refresh_token"] = f"{SECRET}-refreshed"
    document["tokens"]["account_id"] = account
    document["last_refresh"] = "2026-11-18T12:00:00Z"
    (home / ".codex/auth.json").write_text(json.dumps(document))


def test_a_refresh_goes_back_into_the_ended_session(tmp_path):
    source = kept(tmp_path, "src", "codex")
    home = tmp_path / "h"
    home.mkdir()
    a, home = start("codex", "src", home)
    sl.copy_into(a, home, lambda line: None)
    assert sl.write_back(a, home) is None  # nothing refreshed
    refresh_codex(home)
    message = sl.write_back(a, home)
    assert message == "refreshed codex login written back to the HOME of session src"
    written = json.loads((source / ".codex/auth.json").read_text())
    assert written["tokens"]["refresh_token"] == f"{SECRET}-refreshed" and written["last_refresh"].startswith("2026-11-18")
    no_secret(message)


def test_a_running_session_is_never_written(tmp_path, running):
    source = kept(tmp_path, "src", "codex")
    before = (source / ".codex/auth.json").read_bytes()
    home = tmp_path / "h"
    home.mkdir()
    a, home = start("codex", "src", home)
    sl.copy_into(a, home, lambda line: None)
    refresh_codex(home)
    running("src")
    message = sl.write_back(a, home)
    assert "runs" in message and "never written" in message
    assert (source / ".codex/auth.json").read_bytes() == before


def test_no_write_back_of_another_account_or_over_a_changed_login(tmp_path):
    source = kept(tmp_path, "src", "codex")
    home = tmp_path / "h"
    home.mkdir()
    a, home = start("codex", "src", home)
    sl.copy_into(a, home, lambda line: None)
    refresh_codex(home, account="someone-else")
    before = (source / ".codex/auth.json").read_bytes()
    assert sl.write_back(a, home) is None and (source / ".codex/auth.json").read_bytes() == before
    refresh_codex(home)
    put(source, codex_files(refresh="other"))  # the source's own login changed meanwhile
    changed = (source / ".codex/auth.json").read_bytes()
    assert "changed since it was copied" in sl.write_back(a, home)
    assert (source / ".codex/auth.json").read_bytes() == changed


def test_a_write_back_never_follows_a_link_planted_in_the_source(tmp_path):
    source = kept(tmp_path, "src", "codex")
    home = tmp_path / "h"
    home.mkdir()
    a, home = start("codex", "src", home)
    sl.copy_into(a, home, lambda line: None)
    refresh_codex(home)
    outside = tmp_path / "outside.json"
    outside.write_text("keep me")
    os.unlink(source / ".codex/auth.json")
    os.symlink(outside, source / ".codex/auth.json")
    message = sl.write_back(a, home)
    assert "link" in message and outside.read_text() == "keep me"


def test_claude_write_back_needs_anthropic_to_name_the_same_account(tmp_path, monkeypatch):
    source = kept(tmp_path, "src", "claude")
    home = tmp_path / "h"
    home.mkdir()
    accounts = {f"{SECRET}-access-1": ("acc-1", "org-1"), f"{SECRET}-access-2": ("acc-1", "org-1"),
                f"{SECRET}-access-3": ("acc-9", "org-1")}
    monkeypatch.setattr(sl.launcher(), "claude_profile_account", lambda token, timeout=5: accounts[token])
    a, home = start("claude", "src", home)
    sl.copy_into(a, home, lambda line: None)
    assert set(json.loads((home / ".claude.json").read_text())) == {"oauthAccount", "userID", "hasCompletedOnboarding"}

    def refreshed(number, later=1000):
        document = json.loads((home / ".claude/.credentials.json").read_text())
        document["claudeAiOauth"].update(accessToken=f"{SECRET}-access-{number}",
                                         refreshToken=f"{SECRET}-refresh-{number}", expiresAt=FUTURE_MS + later)
        (home / ".claude/.credentials.json").write_text(json.dumps(document))
    refreshed(3)
    assert "could not be checked" in sl.write_back(a, home)
    refreshed(2)
    assert sl.write_back(a, home) == "refreshed claude login written back to the HOME of session src"
    written = json.loads((source / ".claude/.credentials.json").read_text())
    assert written["claudeAiOauth"]["refreshToken"] == f"{SECRET}-refresh-2"
    assert written["mcpOAuth"], "the rest of the source's file is kept"


# --- the launcher -----------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def launcher():
    return sl.launcher()


def test_the_start_option(launcher, tmp_path):
    a = launcher.parse_args(["codex", "--auth-from-session", "src", "--workspace", str(tmp_path)])
    assert a.auth_from_session == "src" and launcher.auth_mode(a) == "session"
    assert launcher.auth_plan(a) == [{"source": "session src", "target": "/root/.codex/auth.json"}]
    assert launcher.parse_args(["claude", "-as", "src"]).auth_from_session == "src"
    assert "--auth-from-session" in launcher.CONFIG_FORBIDDEN and "--auth-from-session" in launcher.AUTH_OPTIONS
    with pytest.raises(ValueError, match="Choose one of"):
        launcher.validate(launcher.parse_args(["codex", "--auth-from-session", "src", "--no-auth"]))
    with pytest.raises(ValueError, match="goose has no login sandbox-ai takes from a session"):
        launcher.validate(launcher.parse_args(["goose", "--auth-from-session", "src"]))
    with pytest.raises(ValueError, match="not a start's name"):
        launcher.validate(launcher.parse_args(["codex", "--auth-from-session", "../x"]))


# --- the real entry point ---------------------------------------------------------------------------------------------

def sandbox_ai(*argv, env=None):
    return subprocess.run([str(ROOT / "sandbox-ai"), *argv], capture_output=True, text=True, env=env, timeout=120)


def test_end_to_end(tmp_path):
    kept(tmp_path, "alpha", "claude")
    kept(tmp_path, "beta", "codex", options=("--auth-from-session", "gamma"))
    env = dict(os.environ)
    shown = sandbox_ai("login", "--status", "--sessions", env=env)
    assert shown.returncode == 0, shown.stderr
    assert "alpha  claude  stopped  login: this computer's login" in shown.stdout
    assert "beta  codex  stopped  login: from session gamma" in shown.stdout
    listed = sandbox_ai("login", "--status", "--sessions", "--json", env=env)
    rows = {row["name"]: row for row in json.loads(listed.stdout)["sessions"]}
    assert rows["alpha"]["info"]["account"]["email"] == "me@example.com"
    refused = sandbox_ai("login", "claude", "--from-session", "alpha", env=env)
    assert refused.returncode == 1 and "add --yes" in refused.stderr
    dry = sandbox_ai("login", "claude", "--from-session", "alpha", "--dry-run", env=env)
    assert dry.returncode == 0 and "me@example.com (as the file says)" in dry.stdout
    done = sandbox_ai("login", "claude", "--from-session", "alpha", "--yes", env=env)
    assert done.returncode == 0, done.stderr
    assert "as the central login" in done.stdout
    status = sandbox_ai("login", "--status", env=env)
    assert "claude    login store" in status.stdout
    again = sandbox_ai("login", "--status", "--sessions", env=env)
    assert "alpha  claude  stopped  login: central login" in again.stdout
    wrong = sandbox_ai("login", "--sessions", env=env)
    assert wrong.returncode == 2 and "--sessions goes with --status" in wrong.stderr
    no_secret(shown.stdout, shown.stderr, listed.stdout, refused.stderr, dry.stdout, done.stdout, done.stderr,
              again.stdout)
    dry_run = sandbox_ai("codex", "--auth-from-session", "beta", "--dry-run", "--workspace", str(tmp_path), env=env)
    assert dry_run.returncode == 0, dry_run.stderr
    assert "session beta" in dry_run.stdout + dry_run.stderr


def test_prepare_home_copies_the_sessions_login(launcher, tmp_path):
    kept(tmp_path, "src", "codex")
    workspace, home = tmp_path / "ws", tmp_path / "new-home"
    workspace.mkdir()
    home.mkdir()
    a = launcher.parse_args(["codex", "--auth-from-session", "src", "--workspace", str(workspace)])
    copied = launcher.prepare_home(a, home)
    assert ("session src: ~/.codex/auth.json", ".codex/auth.json") in copied
    assert json.loads((home / ".codex/auth.json").read_text())["tokens"]["account_id"] == "acct-codex-1"
    assert a.session_login["name"] == "src"
    assert launcher.store_writeback_state(a, home) is None, "never written to the central store"


# --- the web interface: what it passes on, and what it refuses --------------------------------------------------------

def test_the_web_routes_check_what_they_pass_on():
    spec = importlib.util.spec_from_file_location("webui_for_session_login", ROOT / "sandbox" / "webui.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    calls = []

    def runner(host, args, timeout=120, input=None):
        calls.append((host, args))
        return 0, json.dumps({"sessions": [], "central": {}} if "--sessions" in args else {"imported": True}), ""
    api = w.Api(runner)
    assert api.session_logins({"host": "ct1"}, {}) == {"sessions": [], "central": {}}
    body = {"agent": "codex", "name": "web", "fingerprint": "abcdef12"}
    for wrong in ({**body, "agent": "goose"}, {**body, "name": "../x"}, {**body, "fingerprint": "ABCDEF12"},
                  {**body, "fingerprint": "abc"}, {**body, "replace": "yes", "confirm": True}):
        with pytest.raises(w.Refused):
            api.import_session_login({"host": "ct1"}, wrong)
    with pytest.raises(w.Refused, match="confirm"):
        api.import_session_login({"host": "ct1"}, body)
    assert calls == [("ct1", ["login", "--status", "--sessions", "--json"])], "nothing imported unconfirmed"
    api.import_session_login({"host": "ct1"}, {**body, "confirm": True})
    assert calls[-1] == ("ct1", ["login", "codex", "--from-session", "web", "--yes", "--expect=abcdef12", "--json"])
    routes = {(method, pattern) for method, pattern, _, _ in api.routes}
    assert not any(method == "GET" and "import" in pattern for method, pattern in routes), "never from a GET"


def test_a_stopped_session_follows_the_central_login_it_was_imported_into(launcher, tmp_path):
    source = kept(tmp_path, "one", "codex")
    sl.import_login("codex", "one")
    store = central_store(tmp_path, "codex") / ".codex/auth.json"
    refreshed = json.loads(store.read_text())
    refreshed["tokens"]["refresh_token"] = f"{SECRET}-refreshed-elsewhere"
    store.write_text(json.dumps(refreshed, indent=2))  # another sandbox refreshed the central login
    a = launcher.parse_args(["codex", "--state-dir", str(source)])
    launcher.sync_store_login(a, source)
    assert json.loads((source / ".codex/auth.json").read_text())["tokens"]["refresh_token"] == \
        f"{SECRET}-refreshed-elsewhere"


def test_presets_mcp_servers_and_bundles_never_carry_it(launcher):
    def module(name):
        spec = importlib.util.spec_from_file_location(f"{name}_for_session_login", ROOT / "sandbox" / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded
    assert "--auth-from-session" in module("presets").REFUSED
    assert "--auth-from-session" in module("board_bundle").LOGIN_OPTIONS
    assert "--auth-from-session" in module("mcp").forbidden()
    assert "--auth-from-session" in module("modes").WEAKENING_OPTIONS
