"""A team start meets a team of that name as a single start meets its session (user 2026-11-19: "Team starts get the
same as single starts since 0.1.156"): one that runs is attached to (its lead), one whose members are stopped or
paused continues them, one of other members - or one whose member is gone - is refused with what is seen there; a
member's name another start has is refused naming what that start is; and the logins the members' agents lack are
asked for before the first member starts (on a terminal; named elsewhere), Codex's device sign-in included."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


team = load("team_for_existing_tests", "team.py")


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    # every login there unless a test says otherwise
    monkeypatch.setattr(team, "login_missing", lambda data, agents=None: [])
    return tmp_path


def ok(argv, **kwargs):
    return subprocess.CompletedProcess(argv, 0, "", "")


def made(tmp_path, members=("claude", "codex"), options=()):
    """Team app as a start made it (its record), nothing started."""
    data = team.prepare("app", list(members), tmp_path, list(options), lambda line: None, taken=[])
    (team.teams_dir() / "app" / "board").mkdir(parents=True)
    team.write_record("app", data)
    return data


def seen(monkeypatch, states, attachable=True):
    """The members' sessions as STATES {agent: state or None (no session)} say."""
    def member(session):
        agent = session.rsplit("-", 1)[1]
        state = states.get(agent)
        if state is None:
            return None
        return {"name": session, "id": session, "agent": agent, "state": state, "foreground": False,
                "terminal": "ready" if state == "running" else None,
                "attachable": attachable and state == "running",
                "seen": f"a session of {agent}, in tmux, its supervisor {'runs' if state == 'running' else 'does not run'}",
                "login": False}
    monkeypatch.setattr(team, "member_seen", member)


def test_a_stopped_team_continues_its_members_with_the_options_they_were_made_with(tmp_path, monkeypatch):
    made(tmp_path, options=["--allow-pypi"])
    seen(monkeypatch, {"claude": "stopped", "codex": "paused"})
    calls, lines = [], []

    def run(argv, **kwargs):
        calls.append(argv[3:])
        return ok(argv)
    assert team.start("app", ["claude", "codex"], tmp_path, ["--allow-npm"], lines.append, run, tty=lambda: True) == 0
    assert calls == [["resume", "app-claude"], ["resume", "app-codex"]], "each continues, the lead first"
    assert any("keeps the options it was made with" in line and "--allow-npm" in line for line in lines)
    assert lines[-1] == "sandbox-ai team: talk to the lead with: sandbox-ai attach app-claude"
    assert team.load("app")["options"] == ["--allow-pypi"], "the record stays as it was made"


def test_a_running_team_is_attached_to_its_lead_on_a_terminal_and_refused_with_what_is_seen_elsewhere(
        tmp_path, monkeypatch):
    made(tmp_path)
    seen(monkeypatch, {"claude": "running", "codex": "running"})
    calls, lines = [], []

    def run(argv, **kwargs):
        calls.append((argv[3:], kwargs))
        return ok(argv)
    assert team.start("app", ["claude", "codex"], tmp_path, [], lines.append, run, tty=lambda: True) == 0
    assert calls == [(["attach", "app-claude"], {})], "its lead's terminal, interactive (nothing captured)"
    assert "runs already: attaching to its lead app-claude" in lines[0]
    calls.clear()
    with pytest.raises(team.Exists) as refused:
        team.start("app", ["claude", "codex"], tmp_path, [], lines.append, run, tty=lambda: False)
    text = str(refused.value)
    assert "runs already" in text and "sandbox-ai attach app-claude" in text and "sandbox-ai team stop app" in text
    assert "app-codex: running (a session of codex, in tmux" in text, "what is seen of every member"
    with pytest.raises(team.Exists):
        team.start("app", ["claude", "codex"], tmp_path, [], lines.append, run, dry_run=True, tty=lambda: True)
    assert not calls, "nothing runs for a refusal or a dry run"


def test_a_running_team_whose_lead_has_no_terminal_is_refused(tmp_path, monkeypatch):
    made(tmp_path)
    seen(monkeypatch, {"claude": "running", "codex": "running"}, attachable=False)
    with pytest.raises(team.Exists, match="without a terminal to attach to"):
        team.start("app", ["claude", "codex"], tmp_path, [], print, ok, tty=lambda: True)


def test_a_team_of_other_members_or_with_a_member_gone_is_refused(tmp_path, monkeypatch):
    made(tmp_path)
    seen(monkeypatch, {"claude": "stopped", "codex": "stopped"})
    started = lambda argv, **kw: (_ for _ in ()).throw(AssertionError("started"))  # noqa: E731
    with pytest.raises(team.Exists, match=r"exists with claude, codex \(lead claude\), not codex, claude"):
        team.start("app", ["codex", "claude"], tmp_path, [], print, started)
    seen(monkeypatch, {"claude": "stopped", "codex": None})
    with pytest.raises(team.Exists, match="app-codex cannot start again.*team delete app --yes"):
        team.start("app", ["claude", "codex"], tmp_path, [], print, started)


def test_a_members_name_in_use_by_another_start_says_what_that_start_is(tmp_path, monkeypatch):
    seen(monkeypatch, {"claude": "running"})
    with pytest.raises(team.Taken) as refused:
        team.start("app", ["claude", "codex"], tmp_path, [], print, ok, taken=["app-claude"])
    assert "in use already: app-claude (seen: a session of claude, in tmux" in str(refused.value)
    assert refused.value.seen["app-claude"]["state"] == "running"


def test_the_json_dry_run_says_what_is_there(tmp_path, monkeypatch):
    made(tmp_path)
    out = []
    seen(monkeypatch, {"claude": "stopped", "codex": "stopped"})
    assert team.main(["start", "app", "--agents", "claude,codex", "--dry-run", "--json"], out.append) == 0
    plan = json.loads(out[-1])
    assert plan["ok"] is True and plan["continues"] == ["claude", "codex"] and plan["team"]["verdict"] == "continue"
    assert [m["session"] for m in plan["plan"]] == ["app-claude", "app-codex"] and plan["logins"] == []
    seen(monkeypatch, {"claude": "running", "codex": "running"})
    out.clear()
    assert team.main(["start", "app", "--agents", "claude,codex", "--dry-run", "--json"], out.append) == 1
    refused = json.loads(out[-1])
    assert refused["code"] == "exists" and refused["team"]["verdict"] == "attach" and refused["team"]["same"] is True
    assert refused["team"]["seen"]["claude"]["attachable"] is True
    monkeypatch.setattr(team, "existing_sessions", lambda name, agents: ["new-claude"])
    out.clear()
    assert team.main(["start", "new", "--agents", "claude,codex", "--dry-run", "--json"], out.append) == 1
    refused = json.loads(out[-1])
    assert refused["code"] == "taken" and refused["sessions"]["new-claude"]["agent"] == "claude"


def test_the_logins_the_members_lack_are_asked_for_before_the_first_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(team, "login_missing", lambda data, agents=None: [
        (agent, "missing", "store") for agent in (agents or data["members"]) if agent == "codex"])
    asked, logged, calls, lines = [], [], [], []

    def run(argv, **kwargs):
        calls.append(argv[3])
        assert logged, "the login comes before the first member"
        return ok(argv)
    assert team.start("app", ["claude", "codex"], tmp_path, [], lines.append, run, taken=[], tty=lambda: True,
                      ask=lambda text: asked.append(text) or "", log_in=lambda agent: logged.append(agent) or 0) == 0
    assert logged == ["codex"] and calls == ["claude", "codex"]
    assert "has no codex login: app-codex would start without one" in asked[0] and "[Y/n]" in asked[0]


@pytest.mark.parametrize("where", ["no terminal", "--no-login-question", "answered no"])
def test_a_missing_login_is_named_where_it_is_not_asked_for(tmp_path, monkeypatch, where):
    monkeypatch.setattr(team, "login_missing", lambda data, agents=None: [("codex", "missing", "host")])
    lines = []
    team.start("app", ["claude", "codex"], tmp_path, [], lines.append, ok, taken=[],
               tty=lambda: where != "no terminal", ask=lambda text: "n",
               log_in=lambda agent: pytest.fail("logged in unasked"),
               login_question=where != "--no-login-question")  # (the desktop app asked before it opened the tab)
    expected = "starts without a login" if where == "answered no" else "this machine has no codex login: app-codex asks"
    assert any(expected in line for line in lines), lines


def test_which_members_want_a_login_of_sandbox_ais(tmp_path, monkeypatch):
    """An agent `sandbox-ai login` takes, unless the team's options (or the member's own) bring it otherwise."""
    data = team.prepare("app", ["claude", "codex", "goose"], tmp_path, ["--allow-pypi"], lambda line: None, taken=[],
                        member_options={"codex": {"--llm": "lab"}})
    assert [team.login_wanted(data, a) for a in data["members"]] == [True, False, False]
    data = team.prepare("app", ["claude", "codex"], tmp_path, ["--env", "ANTHROPIC_API_KEY=x"], lambda line: None,
                        taken=[])
    assert team.login_wanted(data, "claude") is False


def test_login_missing_reads_the_launchers_login_state(tmp_path, monkeypatch):
    real = load("team_for_login_state_test", "team.py")
    data = real.prepare("app", ["claude", "codex"], tmp_path, [], lambda line: None, taken=[])
    launcher = real.cached_module("agent_sandbox_for_team", "agent_sandbox.py")
    monkeypatch.setattr(launcher, "login_state", lambda agent: ("store", None if agent == "claude" else "expired"))
    assert real.login_missing(data) == [("codex", "expired", "store")]
    assert real.login_words("codex", "expired", "store") == "sandbox-ai's login store has an expired codex login"


# --- end to end: the real command line, a team record and its members' sessions on disk --------------------------

def write_session(state, name, agent):
    directory = state / "sandbox-ai" / "sessions" / name
    (directory / "home").mkdir(parents=True)
    (directory / "session.json").write_text(json.dumps({"agent": agent, "name": name, "options": [], "keep": True,
                                                        "mode": "tmux", "ready": True}))


def test_the_command_line_continues_a_stopped_team_and_refuses_other_members(tmp_path):
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
           "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir()
    data = {"version": 1, "name": "app", "members": ["claude", "codex"], "lead": "claude",
            "workspace": str(tmp_path), "options": [], "vpn": None, "member_vpn": {}, "shared_openvpn": False,
            "roles": {}, "member_options": {}, "created": "2026-11-19T00:00:00Z"}
    record = tmp_path / "state" / "sandbox-ai" / "teams" / "app" / "team.json"
    (record.parent / "board").mkdir(parents=True)
    record.write_text(json.dumps(data))
    for agent in ("claude", "codex"):
        write_session(tmp_path / "state", f"app-{agent}", agent)

    def cli(*args):
        return subprocess.run([str(ROOT / "sandbox-ai"), "team", "start", *args], capture_output=True, text=True,
                              env=env, timeout=120, stdin=subprocess.DEVNULL)
    done = cli("app", "--agents", "claude,codex", "--dry-run", "--json")
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)
    assert plan["continues"] == ["claude", "codex"] and plan["team"]["states"] == {"claude": "stopped",
                                                                                   "codex": "stopped"}
    done = cli("app", "--agents", "codex,claude", "--dry-run")
    assert done.returncode == 1 and "exists with claude, codex (lead claude), not codex, claude" in done.stderr
    assert "app-claude: stopped (a session of claude" in done.stderr, "what is seen there, never only 'exists'"
    done = cli("app", "--agents", "claude,codex", "--dry-run")
    assert done.returncode == 0 and "app-claude would continue (stopped)" in done.stdout


def test_the_command_line_takes_no_login_question(monkeypatch):
    got = {}
    monkeypatch.setattr(team, "start", lambda *args, **kw: got.update(kw) or 0)
    assert team.main(["start", "app", "--agents", "claude,codex", "--no-login-question"]) == 0
    assert got["login_question"] is False
    assert team.main(["start", "app", "--agents", "claude,codex"]) == 0 and got["login_question"] is True
