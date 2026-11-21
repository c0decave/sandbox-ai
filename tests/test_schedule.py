"""sandbox-ai schedule: the time table (--every, --at, --cron, DST, missed runs), the lock and the time limit, the
timer's unit files (made, never installed here), a tick starting what is due, the report and history, the summary
of what the agent changed (Git on the host without the repository's programs), and the whole through ./sandbox-ai
with a fake agent (Podman faked: live_fakes) and a fake clock (SANDBOX_AI_SCHEDULE_NOW)."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import live_fakes as F  # noqa: E402

SPEC = importlib.util.spec_from_file_location("sandbox_schedule_under_test", ROOT / "sandbox" / "schedule.py")
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)
BERLIN = S.ZoneInfo("Europe/Berlin")
NEW_YORK = S.ZoneInfo("America/New_York")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SANDBOX_AI_SCHEDULE_NOW", raising=False)
    return tmp_path


def schedule(**fields):
    base = {"version": 1, "name": "nightly", "created": utc(2026, 11, 1, 12, 0), "timing": None, "on_push": None,
            "agent": "claude", "options": [], "agent_args": [], "prompt": "Review", "preset": None,
            "preset_values": [], "board": None, "workdir": "/tmp", "ask": "off", "limit": 3600, "missed": "once",
            "keep_starts": 5, "commit": None, "push": None, "push_url": None, "notify": False, "paused": False}
    base.update(fields)
    return base


# -- parsing ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, seconds", [("30m", 1800), ("2h", 7200), ("1d", 86400), ("1w", 604800),
                                           ("1h30m", 5400), ("1M".lower(), 60)])
def test_every_takes_durations(text, seconds):
    assert S.parse_every(text) == seconds


@pytest.mark.parametrize("text", ["", "30", "30s", "0m", "1x", "1h 30m", "400d", "m30", "-1h"])
def test_every_refuses_what_is_no_duration(text):
    with pytest.raises(ValueError):
        S.parse_every(text)


def test_at_daily_and_once():
    assert S.parse_at("02:30") == {"daily": (2, 30)}
    assert S.parse_at("7:05") == {"daily": (7, 5)}
    assert S.parse_at("2026-11-20 02:30") == {"once": datetime(2026, 11, 20, 2, 30)}
    assert S.parse_at("2026-11-20T02:30") == {"once": datetime(2026, 11, 20, 2, 30)}
    for bad in ("24:00", "2:60", "tomorrow", "2026-11-20 02:30+01:00"):
        with pytest.raises(ValueError):
            S.parse_at(bad)


def test_cron_fields_lists_ranges_steps_names():
    cron = S.Cron("*/15 9-17 * jan,jul mon-fri")
    assert cron.minutes == [0, 15, 30, 45] and cron.hours == list(range(9, 18)) and cron.months == [1, 7]
    assert cron.weekdays == {1, 2, 3, 4, 5}
    assert S.Cron("0 0 * * 7").weekdays == {0}  # 7 is Sunday too
    assert S.Cron("@daily").text == "0 0 * * *" and S.Cron("@hourly").minutes == [0]
    assert S.Cron("5/20 * * * *").minutes == [5, 25, 45]
    for bad in ("* * * *", "61 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "a * * * *",
                "5-1 * * * *", "*/0 * * * *", "0 0 30 feb *"):
        with pytest.raises(ValueError):
            S.Cron(bad)


def test_cron_day_of_month_or_day_of_week_when_both_are_given():
    """As cron: both restricted - either matches; one of them * - only the other counts."""
    tz = timezone.utc
    both = S.Cron("0 12 13 * fri")  # the 13th, and every Friday
    first = both.next_after(utc(2026, 11, 1, 0, 0), tz)
    assert datetime.fromtimestamp(first, tz).date().isoformat() == "2026-11-06"  # a Friday before the 13th
    only_week = S.Cron("0 12 * * fri")
    assert datetime.fromtimestamp(only_week.next_after(utc(2026, 11, 7), tz), tz).day == 13  # Friday the 13th


def test_cron_next_after_a_leap_day():
    cron = S.Cron("0 0 29 2 *")
    due = cron.next_after(utc(2026, 3, 1), timezone.utc)
    assert datetime.fromtimestamp(due, timezone.utc).date().isoformat() == "2028-02-29"


# -- DST edges --------------------------------------------------------------------------------------------------------

def test_a_time_the_clock_jumps_over_runs_at_the_jump():
    """Europe/Berlin, 2026-03-29: 02:00 CET becomes 03:00 CEST; 02:30 is not on the clock that night."""
    s = schedule(timing={"kind": "at", "value": "02:30"})
    due = S.next_due(s, utc(2026, 3, 28, 12, 0), BERLIN)
    assert due == utc(2026, 3, 29, 1, 0)  # 03:00 CEST: the moment of the jump
    assert S.local(due, BERLIN).strftime("%H:%M") == "03:00"
    nxt = S.next_due(s, due, BERLIN)
    assert nxt == utc(2026, 3, 30, 0, 30)  # 02:30 CEST the next day


def test_a_time_that_is_there_twice_runs_once():
    """Europe/Berlin, 2026-10-25: 03:00 CEST becomes 02:00 CET; 02:30 is on the clock twice - one run, the first."""
    s = schedule(timing={"kind": "cron", "value": "30 2 * * *"})
    due = S.next_due(s, utc(2026, 10, 24, 12, 0), BERLIN)
    assert due == utc(2026, 10, 25, 0, 30)  # 02:30 CEST, the first
    again = S.next_due(s, due, BERLIN)
    assert again == utc(2026, 10, 26, 1, 30)  # not 02:30 CET the same night (01:30 UTC on the 25th)
    count, _ = S.fires_between(s, utc(2026, 10, 24, 12, 0), utc(2026, 10, 25, 12, 0), BERLIN)
    assert count == 1


def test_every_counts_real_time_across_dst():
    s = schedule(timing={"kind": "every", "value": "1d"}, created=utc(2026, 3, 28, 1, 0))  # 02:00 CET
    due = S.next_due(s, utc(2026, 3, 28, 1, 0), BERLIN)
    assert due == utc(2026, 3, 29, 1, 0)  # 24 hours later: 03:00 CEST on the wall
    assert S.local(due, BERLIN).hour == 3


def test_new_york_spring_forward_with_cron_every_quarter_hour_in_the_gap():
    """America/New_York, 2026-03-08: 02:00 EST becomes 03:00 EDT. 02:00, 02:15, 02:30, 02:45 are not on the clock:
    one run at the jump, then 03:00 (the same moment) is not run twice."""
    s = schedule(timing={"kind": "cron", "value": "*/15 2-3 8 3 *"})
    start = utc(2026, 3, 8, 6, 0)  # 01:00 EST
    count, last = S.fires_between(s, start, utc(2026, 3, 8, 9, 0), NEW_YORK)
    times = []
    at = start
    while True:
        due = S.next_due(s, at, NEW_YORK)
        if due is None or due > utc(2026, 3, 8, 9, 0):
            break
        times.append(S.local(due, NEW_YORK).strftime("%H:%M"))
        at = due
    assert times == ["03:00", "03:15", "03:30", "03:45"]
    assert count == 4


def test_once_at_and_then_never_again():
    s = schedule(timing={"kind": "at", "value": "2026-11-20 02:30"})
    due = S.next_due(s, utc(2026, 11, 1), BERLIN)
    assert due == utc(2026, 11, 20, 1, 30)
    assert S.next_due(s, due, BERLIN) is None


# -- the tick's decision: due, missed, paused, push -------------------------------------------------------------------

def test_due_on_time_and_missed_while_asleep():
    s = schedule(timing={"kind": "every", "value": "1h"}, created=utc(2026, 11, 17, 0, 0))
    reason, due, notes, state = S.decide(s, {"last_due": utc(2026, 11, 17, 0, 0)}, utc(2026, 11, 17, 1, 0, 30),
                                         BERLIN, busy=lambda: False)
    assert (reason, due, notes) == ("due", utc(2026, 11, 17, 1, 0), [])
    assert state["last_due"] == due
    # the machine slept from 01:30 to 06:10: five times due, one run on wake
    reason, due, notes, state = S.decide(s, state, utc(2026, 11, 17, 6, 10), BERLIN, busy=lambda: False)
    assert reason == "missed" and due == utc(2026, 11, 17, 6, 0) and notes == ["5 times due since the last tick: one run"]
    # nothing more until the next hour
    assert S.decide(s, state, utc(2026, 11, 17, 6, 20), BERLIN, busy=lambda: False)[0] is None


def test_missed_skip_skips_what_was_missed_but_runs_what_is_on_time():
    s = schedule(timing={"kind": "every", "value": "1h"}, created=utc(2026, 11, 17, 0, 0), missed="skip")
    reason, _due, notes, state = S.decide(s, {"last_due": utc(2026, 11, 17, 0, 0)}, utc(2026, 11, 17, 3, 30),
                                          BERLIN, busy=lambda: False)
    assert reason is None and "skipped (--missed skip)" in notes[0]
    assert state["last_due"] == utc(2026, 11, 17, 3, 0)
    reason, *_ = S.decide(s, state, utc(2026, 11, 17, 4, 1), BERLIN, busy=lambda: False)
    assert reason == "due"


def test_a_running_run_skips_the_next_and_a_paused_schedule_does_nothing():
    s = schedule(timing={"kind": "every", "value": "1h"}, created=utc(2026, 11, 17, 0, 0))
    reason, _due, notes, state = S.decide(s, {"last_due": utc(2026, 11, 17, 0, 0)}, utc(2026, 11, 17, 1, 1),
                                          BERLIN, busy=lambda: True)
    assert reason is None and notes == ["the previous run still runs: this one is skipped"]
    assert state["last_due"] == utc(2026, 11, 17, 1, 0)
    paused = {**s, "paused": True}
    assert S.decide(paused, {"last_due": 0}, utc(2026, 11, 17, 9), BERLIN, busy=lambda: False) == \
        (None, None, [], {"last_due": 0})


def git(repo, *args, **kwargs):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.org"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env,
                          **kwargs).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("one\ntwo\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "first")
    return path


def test_on_push_runs_when_a_branch_moves_and_waits_for_a_running_run(repo):
    s = schedule(on_push=str(repo))
    reason, _due, _notes, state = S.decide(s, {}, utc(2026, 11, 17, 1), BERLIN, busy=lambda: False)
    assert reason is None and state["push_seen"]  # the first look: what is there now
    assert S.decide(s, state, utc(2026, 11, 17, 2), BERLIN, busy=lambda: False)[0] is None
    (repo / "b.txt").write_text("b")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "second")
    reason, _due, _notes, waiting = S.decide(s, state, utc(2026, 11, 17, 3), BERLIN, busy=lambda: True)
    assert reason is None and waiting["push_seen"] == state["push_seen"]  # tried again at the next tick
    reason, due, _notes, after = S.decide(s, waiting, utc(2026, 11, 17, 4), BERLIN, busy=lambda: False)
    assert reason == "push" and due == utc(2026, 11, 17, 4) and after["push_seen"] != state["push_seen"]


# -- the lock and the time limit --------------------------------------------------------------------------------------

def test_the_lock_is_one_holders(tmp_path):
    with S.Lock(tmp_path / "lock") as first:
        assert first.got
        second = S.Lock(tmp_path / "lock")
        assert not second.got
        second.release()
    third = S.Lock(tmp_path / "lock")
    assert third.got
    third.release()


def save(home, **fields):
    workdir = home / "ws"
    workdir.mkdir(exist_ok=True)
    document = schedule(workdir=str(workdir), timing={"kind": "every", "value": "1h"}, **fields)
    S.save(document)
    return document


class FakeProcess:
    def __init__(self, status=0, seconds=0.0, pid=4242):
        self.status, self.seconds, self.pid, self.ended = status, seconds, pid, None

    def wait(self, timeout=None):
        if self.ended is not None:
            return self.ended
        if self.seconds > (timeout or 0):
            raise subprocess.TimeoutExpired("sandbox-ai", timeout)
        self.ended = self.status
        return self.status

    def poll(self):
        return self.ended


def test_a_run_while_one_runs_is_skipped_and_recorded(home):
    save(home)
    with S.Lock(S.state_dir("nightly") / "lock"):
        entry = S.run_once("nightly", "due", clock=lambda: utc(2026, 11, 17, 2))
        assert S.running("nightly")
    assert entry["status"] == "skipped" and entry["note"] == "the previous run still runs"
    assert S.history("nightly")[-1]["status"] == "skipped"
    assert not S.running("nightly")


def test_a_run_past_its_limit_is_stopped(home, monkeypatch):
    save(home, limit=10)
    stopped, killed = [], []
    process = FakeProcess(seconds=99)

    def run(argv, **kwargs):
        if argv[3:4] == ["stop"]:
            stopped.append(argv[4])
        return SimpleNamespace(returncode=1, stdout="", stderr="no run")
    monkeypatch.setattr(S.os, "killpg", lambda pid, sig: (killed.append((pid, sig)),
                                                          setattr(process, "ended", -sig)))
    entry = S.run_once("nightly", "due", clock=lambda: utc(2026, 11, 17, 2), run=run, popen=lambda *a, **k: process)
    assert entry["status"] == "timeout" and "time limit of 0 min" in entry["note"]
    assert stopped == [entry["start"]] and killed[0][0] == 4242
    assert "stopped after its time limit" in entry["note"]


# -- a run: start command, summary, report, history ------------------------------------------------------------------

def test_a_run_starts_the_agent_one_shot_with_the_schedules_ask_and_writes_its_history(home, monkeypatch):
    document = save(home, ask="off", options=["--offline"])
    seen = {}

    def popen(argv, cwd=None, env=None, stdout=None, **kwargs):
        seen.update(argv=argv, cwd=cwd, env=env, kwargs=kwargs)
        os.write(stdout, b"the agent's answer\nAPI_KEY=sk-ant-api03-" + b"x" * 40 + b"\n")
        return FakeProcess(0)

    def run(argv, **kwargs):
        if "report" in argv:
            Path(argv[argv.index("-o") + 1]).write_text("# sandbox-ai report: x\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    told = []
    monkeypatch.setattr(S, "notify_end", lambda schedule, entry: told.append(entry["status"]) or [])
    entry = S.run_once("nightly", "due", due=utc(2026, 11, 17, 2), clock=lambda: utc(2026, 11, 17, 2, 0, 5),
                       run=run, popen=popen)
    words = seen["argv"][3:]
    assert words[0] == "claude" and "--offline" in words
    assert words[words.index("--ask") + 1] == "off" and "--no-tmux" in words
    stamp = datetime.fromtimestamp(utc(2026, 11, 17, 2, 0, 5)).strftime("%m%d%H%M%S")  # the local time of the run
    assert words[words.index("--name") + 1] == entry["start"] == f"nightly-{stamp}"
    assert words[-3:] == ["-p", "--", "Review"]
    assert seen["cwd"] == document["workdir"] and seen["env"]["SANDBOX_AI_SCHEDULE"] == "nightly"
    assert seen["kwargs"]["start_new_session"] is True
    assert entry["status"] == "ok" and entry["exit"] == 0 and told == ["ok"]
    assert entry["report"] == f"runs/{entry['run']}/report.md" and entry["changes"] is None
    [kept] = S.history("nightly")
    assert kept["run"] == entry["run"] and kept["status"] == "ok"
    summary = (S.state_dir("nightly") / kept["summary"]).read_text()
    assert "| Status | ok (exit status 0) |" in summary and "the agent's answer" in summary
    assert "sk-ant-api03-xxxx" not in summary, "secrets in the output are redacted"
    assert "no Git repository" in summary
    entry_, text = S.report_text("nightly")
    assert entry_["run"] == entry["run"] and "# Scheduled run nightly" in text and "# sandbox-ai report: x" in text
    assert (S.state_dir("nightly") / "runs" / entry["run"]).stat().st_mode & 0o077 == 0


def test_agent_args_of_every_agent_and_an_agent_without_one_shot_is_refused(monkeypatch):
    for agent, make in S.ONESHOT.items():
        args = S.agent_args_of(schedule(agent=agent, prompt="-x task"))
        assert any("-x task" in word for word in args), agent
        assert S.oneshot_given(agent, args), f"{agent}: its own one-shot form is recognised"
    with pytest.raises(ValueError, match="needs a task"):
        S.agent_args_of(schedule(prompt=None, agent_args=["--model", "opus"]))
    assert S.agent_args_of(schedule(prompt=None, agent_args=["--model", "opus", "-p", "go"])) == \
        ["--model", "opus", "-p", "go"]
    monkeypatch.delitem(S.ONESHOT, "letta")
    with pytest.raises(ValueError, match="no one-shot mode"):
        S.agent_args_of(schedule(agent="letta"))


def test_every_agent_of_the_launcher_has_a_one_shot_mode():
    launcher = S.launcher()
    assert set(S.ONESHOT) == set(launcher.AUTH_TARGETS) == set(launcher.COMPANION_COMMANDS)


def test_the_start_refuses_options_the_schedule_sets_itself(home):
    for options in (["--name", "x"], ["-N", "x"], ["--ask", "on"], ["-Q", "on"], ["--tmux"], ["--session", "x"]):
        with pytest.raises(ValueError, match="not for a schedule's start"):
            S.check_options("claude", options)
    with pytest.raises(ValueError, match="unrecognized"):
        S.check_options("claude", ["--no-such-option"])
    assert S.check_options("claude", ["-D"]) == ["--dangerously-skip-permissions"]


# -- git: the summary of the changes, commit to a branch, push ------------------------------------------------------

def test_changes_between_snapshots_without_the_repositorys_programs(repo, tmp_path):
    marker = tmp_path / "filter-ran"
    (repo / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")
    git(repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    git(repo, "config", "diff.evil.textconv", f"touch {marker}; cat")
    git(repo, "config", "core.fsmonitor", f"touch {marker}; echo")
    hooks = repo / ".git" / "hooks"
    (hooks / "post-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hooks / "post-commit").chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = S.snapshot(repo, scratch)
    (repo / "a.txt").write_text("one\nTWO\nthree\n")
    (repo / "new.txt").write_text("new\n")
    (repo / "ignored.log").write_text("x")
    (repo / ".gitignore").write_text("*.log\n")
    after = S.snapshot(repo, scratch)
    found = S.changes(repo, before, after)
    assert found["files"] == 3 and found["commits"] == 0  # a.txt, new.txt, .gitignore (.gitattributes: before)
    assert "a.txt" in found["stat"] and "new.txt" in found["stat"] and "ignored.log" not in found["stat"]
    commit = S.commit_changes(repo, after, "nightly-result", "sandbox-ai schedule nightly")
    assert not marker.exists(), "no filter, textconv, fsmonitor or hook of the repository ran"
    assert (repo / "a.txt").read_text() == "one\nTWO\nthree\n", "the work tree stays as it is"
    assert git(repo, "rev-parse", "refs/heads/nightly-result") == commit
    assert git(repo, "cat-file", "-p", f"{commit}:a.txt") == "one\nTWO\nthree"
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert list(scratch.iterdir()) == [], "the snapshot's own index is gone"
    with pytest.raises(ValueError, match="checked out"):
        S.commit_changes(repo, after, "main", "x")


def test_new_commits_of_the_agent_are_counted(repo, tmp_path):
    before = S.snapshot(repo, tmp_path)
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "by the agent")
    after = S.snapshot(repo, tmp_path)
    found = S.changes(repo, before, after)
    assert found["commits"] == 1 and found["files"] == 1
    assert S.commit_changes(repo, after, "result", "x") == git(repo, "rev-parse", "HEAD")


def test_push_only_to_the_recorded_address_after_the_privacy_check(repo, tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    url = S.remote_url(repo, "origin")
    git(repo, "branch", "result")
    checks = []

    def run(argv, **kwargs):
        if "privacy-check" in argv:
            checks.append(argv)
            return SimpleNamespace(returncode=checks_status[0], stdout="1 blocking finding", stderr="")
        return subprocess.run(argv, **kwargs)
    checks_status = [1]
    with pytest.raises(ValueError, match="privacy check stopped the push"):
        S.push_branch(repo, "origin", url, "result", run)
    checks_status[0] = 0
    assert S.push_branch(repo, "origin", url, "result", run) == "pushed"
    assert git(bare, "rev-parse", "refs/heads/result") == git(repo, "rev-parse", "result")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "elsewhere.git"))
    with pytest.raises(ValueError, match="points elsewhere"):
        S.push_branch(repo, "origin", url, "result", run)
    git(repo, "remote", "set-url", "origin", str(bare))
    git(repo, "config", "credential.helper", "!touch /tmp/x")
    with pytest.raises(ValueError, match="credential.helper"):
        S.push_branch(repo, "origin", url, "result", run)
    git(repo, "config", "--unset", "credential.helper")
    for key, value in (("submodule.recurse", "true"), ("url.https://evil.example/.insteadOf", str(bare)),
                       ("core.sshCommand", "touch /tmp/x; ssh")):
        git(repo, "config", key, value)
        with pytest.raises(ValueError, match="not pushed from the host"):
            S.push_branch(repo, "origin", url, "result", run)
        git(repo, "config", "--unset", key)
    assert S.push_branch(repo, "origin", url, "result", run) == "pushed"


# -- the timer's files ------------------------------------------------------------------------------------------------

def test_systemd_units_run_the_tick_every_minute_and_let_runs_outlive_it():
    units = S.systemd_units(["/usr/bin/python3", "-I", "/opt/sandbox ai/main.py", "schedule", "tick"],
                            "/usr/bin:/bin:/home/u/100%/$x")
    service, timer = units["sandbox-ai-schedule.service"], units["sandbox-ai-schedule.timer"]
    assert 'ExecStart="/usr/bin/python3" "-I" "/opt/sandbox ai/main.py" "schedule" "tick"' in service
    assert 'Environment="PATH=/usr/bin:/bin:/home/u/100%%/$$x"' in service
    assert "Type=oneshot" in service and "KillMode=process" in service
    assert "OnCalendar=*-*-* *:*:00" in timer and "Persistent=true" in timer and "WantedBy=timers.target" in timer


def test_launchd_plist(tmp_path):
    data = plistlib.loads(S.launchd_plist(["/usr/bin/python3", "main.py", "schedule", "tick"], "/opt/homebrew/bin:/usr/bin",
                                          tmp_path / "tick.log"))
    assert data["Label"] == "ai.sandbox-ai.schedule" and data["StartInterval"] == 60 and data["RunAtLoad"] is True
    assert data["AbandonProcessGroup"] is True and data["ProgramArguments"][-2:] == ["schedule", "tick"]
    assert data["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}


def test_enable_and_disable_without_installing_anything_real(home, monkeypatch):
    calls = []
    monkeypatch.setattr(S.shutil, "which", lambda name: f"/usr/bin/{name}")
    run = lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")  # noqa
    said = []
    assert S.enable(dry_run=True, run=run, out=said.append) == 0
    assert calls == [] and not (S.systemd_dir() / "sandbox-ai-schedule.timer").exists()
    assert "OnCalendar" in "\n".join(said)
    assert S.enable(run=run, out=said.append) == 0
    assert (S.systemd_dir() / "sandbox-ai-schedule.timer").is_file()
    assert calls == [["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", "sandbox-ai-schedule.timer"]]
    assert S.disable(run=run, out=said.append) == [str(S.systemd_dir() / "sandbox-ai-schedule.timer"),
                                                    str(S.systemd_dir() / "sandbox-ai-schedule.service")]
    assert not (S.systemd_dir() / "sandbox-ai-schedule.service").exists()
    monkeypatch.setattr(S.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(home))
    calls.clear()
    assert S.enable(run=run, out=said.append) == 0
    assert plistlib.loads(S.launchd_path().read_bytes())["Label"] == "ai.sandbox-ai.schedule"
    assert calls[-1][:2] == ["launchctl", "bootstrap"]
    S.disable(run=run, out=said.append)
    assert not S.launchd_path().exists()


# -- tick -------------------------------------------------------------------------------------------------------------

def test_tick_starts_what_is_due_once(home):
    save(home, created=utc(2026, 11, 17, 0, 0))
    S.save_state("nightly", {"last_due": utc(2026, 11, 17, 0, 0)})
    started = []
    starter = lambda name, reason, due: started.append((name, reason, due))  # noqa: E731
    assert S.tick(clock=lambda: utc(2026, 11, 17, 0, 30), starter=starter, tz=BERLIN) == []
    assert S.tick(clock=lambda: utc(2026, 11, 17, 1, 0, 20), starter=starter, tz=BERLIN) == [("nightly", "due")]
    assert S.tick(clock=lambda: utc(2026, 11, 17, 1, 1, 20), starter=starter, tz=BERLIN) == []
    assert started == [("nightly", "due", utc(2026, 11, 17, 1, 0))]
    assert S.last_tick() == utc(2026, 11, 17, 1, 1, 20)


def test_a_damaged_schedule_holds_up_no_other(home):
    save(home, created=utc(2026, 11, 17, 0, 0))
    S.private_dir(S.config_dir())
    bad = S.config_dir() / "broken.json"
    bad.write_text("{")
    bad.chmod(0o600)
    started = []
    S.tick(clock=lambda: utc(2026, 11, 17, 1, 0, 20), starter=lambda *a: started.append(a), tz=BERLIN)
    assert [name for name, *_ in started] == ["nightly"]


# -- the form of the app and the web interface ------------------------------------------------------------------------

def test_add_arguments_are_what_the_command_takes(home):
    (home / "ws").mkdir()
    args = S.add_arguments({"name": "nightly", "cron": "0 3 * * 1-5", "agent": "claude",
                            "options": "--offline -w ~/src", "prompt": "-review this", "ask": "allow",
                            "limit": "30m", "keep_starts": 3, "workdir": str(home / "ws"), "notify": False})
    assert args == ["schedule", "add", "--cron=0 3 * * 1-5", "--prompt=-review this", f"--workdir={home / 'ws'}",
                    "--ask=allow", "--limit=30m", "--missed=once", "--keep-starts=3", "--no-notify", "nightly", "--",
                    "claude", "--offline", "-w", "~/src"]
    for bad in ({"name": "../x", "every": "1h", "agent": "claude"}, {"name": "x", "agent": "claude"},
                {"name": "x", "every": "1h", "at": "02:00", "agent": "claude"},
                {"name": "x", "every": "1h", "agent": "nope"}, {"name": "x", "every": "1h", "agent": "claude",
                                                               "ask": "maybe"},
                {"name": "x", "every": "1h", "agent": "claude", "options": ["--", "x"]},
                {"name": "x", "every": "1h", "agent": "claude", "prompt": "a\x1bb"}):
        with pytest.raises(ValueError):
            S.add_arguments(bad)
    said = []
    assert S.main(args[1:], out=said.append) == 0, said
    stored = S.load("nightly")
    assert stored["prompt"] == "-review this" and stored["ask"] == "allow" and stored["limit"] == 1800
    assert stored["options"] == ["--offline", "--workspace", "~/src"] and stored["notify"] is False


def test_a_form_without_a_name_is_told_so_not_none(home):
    """The app's Add schedule with the name left empty said "a schedule's name is ...: 'None'" (a name nobody typed):
    an empty name is said to be missing (app audit 2026-11-20)."""
    for name in ("", None, "  "):
        with pytest.raises(ValueError) as caught:
            S.add_arguments({"name": name, "every": "1h", "agent": "claude"})
        assert "None" not in str(caught.value) and "needs a name" in str(caught.value), caught.value


# -- through ./sandbox-ai: a fake agent, a fake clock ----------------------------------------------------------------

def wait_for(predicate, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.2)
    return predicate()


def test_end_to_end_a_tick_runs_a_due_schedule_with_a_report(tmp_path):
    host = F.Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    git(host.workspace, "init", "-q", "-b", "main")
    (host.workspace / "README").write_text("x\n")
    git(host.workspace, "add", "README")
    git(host.workspace, "commit", "-q", "-m", "first")
    created = utc(2026, 11, 17, 1, 0)
    clock = {"SANDBOX_AI_SCHEDULE_NOW": str(created), "FAKE_PODMAN_RUN": "0:0"}
    done = host.run("schedule", "add", "nightly", "--every", "1h", "--prompt", "Review the commits", "--no-notify",
                    "--", "claude", "--offline", env=clock)
    assert done.returncode == 0, done.stderr
    assert "next run" in done.stdout and "--ask off (refused, nobody answers)" in done.stdout
    listed = json.loads(host.run("schedule", "list", "--json", env=clock).stdout)
    assert [row["name"] for row in listed["schedules"]] == ["nightly"]
    assert listed["schedules"][0]["next"] == created + 3600 and listed["timer"]["installed"] is False
    early = host.run("schedule", "tick", env={**clock, "SANDBOX_AI_SCHEDULE_NOW": str(created + 1800)})
    assert early.returncode == 0, early.stderr
    assert json.loads(host.run("schedule", "history", "nightly", "--json").stdout)["runs"] == []
    due = host.run("schedule", "tick", env={**clock, "SANDBOX_AI_SCHEDULE_NOW": str(created + 3600 + 20)})
    assert due.returncode == 0, due.stderr

    def ended():
        runs = json.loads(host.run("schedule", "history", "nightly", "--json").stdout)["runs"]
        return runs if runs and runs[-1]["status"] != "running" else None
    runs = wait_for(ended)
    assert runs, "the run the tick started has ended"
    [entry] = runs
    assert entry["status"] == "ok", entry
    assert entry["reason"] == "due" and entry["due"] == created + 3600
    assert entry["changes"]["files"] == 0 and entry["report"]
    pod = [call for call in host.calls() if call[:2] == ["pod", "create"]]
    assert pod, "the real launcher made its pod"
    report = host.run("schedule", "report", "nightly")
    assert report.returncode == 0, report.stderr
    assert "# Scheduled run nightly" in report.stdout and "Nothing changed in the workspace." in report.stdout
    assert f"# sandbox-ai report: {entry['start']}" in report.stdout
    assert "off: what is not allowed was refused" in report.stdout
    again = host.run("schedule", "tick", env={**clock, "SANDBOX_AI_SCHEDULE_NOW": str(created + 3600 + 80)})
    assert again.returncode == 0
    time.sleep(1)
    assert len(json.loads(host.run("schedule", "history", "nightly", "--json").stdout)["runs"]) == 1, \
        "one run per due time"


def test_end_to_end_run_now_past_its_limit_and_pause(tmp_path):
    host = F.Host(tmp_path / "host")
    host.add_image("localhost/codex-sandbox:latest")
    env = {"FAKE_PODMAN_RUN": "0:60"}
    done = host.run("schedule", "add", "slow", "--every", "1d", "--limit", "10s", "--no-notify", "--", "codex",
                    "--", "exec", "do it", env=env)
    assert done.returncode == 0, done.stderr
    began = time.monotonic()
    ran = host.run("schedule", "run-now", "slow", "--wait", env=env, timeout=200)
    assert ran.returncode == 1 and "timeout" in ran.stdout, ran.stdout + ran.stderr
    assert time.monotonic() - began < 150
    [entry] = json.loads(host.run("schedule", "history", "slow", "--json").stdout)["runs"]
    assert entry["status"] == "timeout" and entry["reason"] == "now"
    assert host.run("schedule", "pause", "slow").returncode == 0
    assert json.loads(host.run("schedule", "show", "slow", "--json").stdout)["state"] == "paused"
    shown = host.run("schedule", "show", "slow").stdout
    assert "exec 'do it'" in shown and "--ask off" in shown
    assert host.run("schedule", "remove", "slow").returncode == 0
    assert "no schedules" in host.run("schedule", "list").stdout


def test_the_end_of_a_run_is_told_on_the_desktop_and_to_the_notify_channels(monkeypatch):
    """The hook point: approvals.desktop_notification, and notify.tell when this sandbox-ai has notify.py (it does
    now) - run-ended for ok, run-failed otherwise; nothing with --no-notify; a failing module never fails the run."""
    desktop, phone, finished = [], [], []
    fakes = {"approvals": SimpleNamespace(desktop_notification=lambda title, text, urgency="critical", alarm=False:
                                          desktop.append((title, text, urgency))),
             "notify": SimpleNamespace(tell=lambda event, run=None, what=None, detail=None, watched=None:
                                       phone.append((event, run, what)), finish=lambda seconds=5: finished.append(1))}
    monkeypatch.setattr(S, "load_module", lambda name, alias=None: fakes[name])
    assert (ROOT / "sandbox" / "notify.py").is_file()
    entry = {"run": "20261117-020000-abcd", "start": "nightly-1117020000", "status": "ok",
             "changes": {"files": 2, "insertions": 1, "deletions": 0, "commits": 0}}
    assert S.notify_end(schedule(notify=True), entry) == ["desktop", "notify"]
    assert phone == [("run-ended", "nightly-1117020000", "scheduled run nightly: ok, 2 file(s) changed")]
    assert desktop[0][2] == "normal" and "schedule report nightly 20261117-020000-abcd" in desktop[0][1]
    assert S.notify_end(schedule(notify=True), {**entry, "status": "timeout"}) == ["desktop", "notify"]
    assert phone[-1][0] == "run-failed" and desktop[-1][2] == "critical" and finished == [1, 1]
    assert S.notify_end(schedule(notify=False), entry) == []

    def broken(name, alias=None):
        raise OSError("gone")
    monkeypatch.setattr(S, "load_module", broken)
    assert S.notify_end(schedule(notify=True), entry) == []


def test_disable_removes_only_the_timer_files_sandbox_ai_wrote(home, monkeypatch):
    """A unit or LaunchAgent of the same name that someone else wrote (or a symlink) is never touched, nor stopped."""
    calls = []
    monkeypatch.setattr(S.shutil, "which", lambda name: f"/usr/bin/{name}")
    run = lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")  # noqa
    S.private_dir(S.systemd_dir())
    foreign = S.systemd_dir() / "sandbox-ai-schedule.timer"
    foreign.write_text("[Timer]\nOnCalendar=daily\n")
    (S.systemd_dir() / "sandbox-ai-schedule.service").symlink_to(foreign)
    assert S.timer_files("linux") == []
    said = []
    assert S.disable(run=run, out=said.append) == [] and calls == []
    assert foreign.read_text() == "[Timer]\nOnCalendar=daily\n" and "left as it is" in "\n".join(said)
    monkeypatch.setattr(S.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(home))
    S.launchd_path().parent.mkdir(parents=True)
    S.launchd_path().write_bytes(plistlib.dumps({"Label": "ai.sandbox-ai.schedule", "ProgramArguments": ["/bin/true"]}))
    assert S.timer_files("darwin") == [] and S.disable(run=run, out=said.append) == [] and S.launchd_path().exists()
    S.launchd_path().write_bytes(S.launchd_plist())
    assert S.timer_files("darwin") == [str(S.launchd_path())]
    assert S.disable(run=run, out=said.append) == [str(S.launchd_path())] and not S.launchd_path().exists()
    assert calls == [["launchctl", "bootout", f"gui/{os.getuid()}/ai.sandbox-ai.schedule"]]


def test_the_start_of_a_run_that_was_killed_is_deleted_with_the_old_ones(home, monkeypatch):
    """Review 3x-2: the kept starts of a schedule beyond --keep-starts are deleted after each run - only those its
    state names, and a run's start was named only once the run had ended. A run killed mid-way (the machine shut
    down, the user's session ended) left its kept start, HOME and all, for good. It is named before it starts."""
    save(home, keep_starts=1)
    monkeypatch.setattr(S, "notify_end", lambda schedule, entry: [])

    class Killed(FakeProcess):
        def wait(self, timeout=None):
            raise KeyboardInterrupt  # (the run's process ends here: nothing after this runs)
    with pytest.raises(KeyboardInterrupt):
        S.run_once("nightly", "due", clock=lambda: utc(2026, 11, 17, 2), popen=lambda *a, **k: Killed())
    first = f"nightly-{datetime.fromtimestamp(utc(2026, 11, 17, 2)).strftime('%m%d%H%M%S')}"
    assert S.load_state("nightly")["starts"] == [first]
    deleted = []

    def run(argv, **kwargs):
        if "delete" in argv:
            deleted.append(argv[argv.index("delete") + 1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    entry = S.run_once("nightly", "due", clock=lambda: utc(2026, 11, 17, 3), run=run, popen=lambda *a, **k: FakeProcess())
    assert deleted == [first] and S.load_state("nightly")["starts"] == [entry["start"]]


def test_a_run_killed_mid_way_is_in_the_history_after_the_next(home, monkeypatch):
    """Review 3x-2: a run whose process was killed (SIGKILL, the machine off) wrote no history entry: History… did
    not show it at all. The next run finds its mark (current.json, under the lock nobody holds) and records it."""
    save(home)
    monkeypatch.setattr(S, "notify_end", lambda schedule, entry: [])
    killed = S.private_dir(S.state_dir("nightly") / "runs" / "20261117-020000-abcd")
    S.write_private(killed / "output.log", "agent-sandbox: preparing the kits\n")
    S.write_private(S.state_dir("nightly") / "current.json",
                    json.dumps({"run": "20261117-020000-abcd", "pid": 999999, "start": "nightly-1117020000",
                                "started": utc(2026, 11, 17, 2)}))
    entry = S.run_once("nightly", "due", clock=lambda: utc(2026, 11, 17, 3), popen=lambda *a, **k: FakeProcess(),
                       run=lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""))
    first, second = S.history("nightly")
    assert first["run"] == "20261117-020000-abcd" and first["status"] == "error"
    assert first["start"] == "nightly-1117020000" and "ended mid-way" in first["note"]
    assert second["run"] == entry["run"] and entry["status"] == "ok"
    assert not (S.state_dir("nightly") / "current.json").exists()
    _entry, text = S.report_text("nightly", "20261117-020000-abcd")
    assert "| Status | error |" in text and "preparing the kits" in text and "ended mid-way" in text


# -- review 3x-3 ------------------------------------------------------------------------------------------------------

def test_a_once_time_that_has_passed_is_refused_not_kept_as_a_schedule_that_never_runs(tmp_path):
    """--at "DATE TIME" in the past made a schedule whose first look was already "done": added without a word and
    never run. The command (and so the app's and the web's form: add_arguments runs it) refuses it."""
    host = F.Host(tmp_path / "host")
    clock = {"SANDBOX_AI_SCHEDULE_NOW": str(utc(2026, 11, 17, 12, 0))}
    past = host.run("schedule", "add", "late", "--at", "2026-11-01 10:00", "--prompt", "x", "--no-notify", "--",
                    "claude", env=clock)
    assert past.returncode != 0 and "has passed" in past.stderr, past.stdout + past.stderr
    assert "no schedules" in host.run("schedule", "list", env=clock).stdout
    future = host.run("schedule", "add", "later", "--at", "2026-12-01 10:00", "--prompt", "x", "--no-notify", "--",
                      "claude", env=clock)
    assert future.returncode == 0, future.stderr
    daily = host.run("schedule", "add", "daily", "--at", "02:30", "--prompt", "x", "--no-notify", "--", "claude",
                     env=clock)
    assert daily.returncode == 0, daily.stderr  # a time of day comes again tomorrow


def test_resume_does_not_make_up_a_push_that_came_while_paused(home, repo):
    """`schedule pause|resume`: what was due while paused is not made up - a push to --on-push's repository while
    paused was: the first tick after resume saw the moved branch and started a run for it."""
    s = schedule(on_push=str(repo), workdir=str(repo))
    S.save(s)
    reason, _due, _notes, state = S.decide(s, {}, utc(2026, 11, 17, 1), BERLIN, busy=lambda: False)
    S.save_state("nightly", state)
    S.set_paused("nightly", True, out=lambda line: None)
    (repo / "b.txt").write_text("b")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "pushed while paused")
    paused = S.load("nightly")
    assert S.decide(paused, S.load_state("nightly"), utc(2026, 11, 17, 2), BERLIN, busy=lambda: False)[0] is None
    S.set_paused("nightly", False, out=lambda line: None)
    reason, _due, _notes, _state = S.decide(S.load("nightly"), S.load_state("nightly"), utc(2026, 11, 17, 3), BERLIN,
                                            busy=lambda: False)
    assert reason is None, "a push while paused is not made up"
    (repo / "c.txt").write_text("c")
    git(repo, "add", "c.txt")
    git(repo, "commit", "-q", "-m", "pushed after resume")
    assert S.decide(S.load("nightly"), S.load_state("nightly"), utc(2026, 11, 17, 4), BERLIN,
                    busy=lambda: False)[0] == "push"


def test_commit_holds_what_the_agent_changed_never_what_was_uncommitted_before(repo, tmp_path):
    """Review 3x-3: --commit BRANCH committed the whole work tree as the run left it on top of HEAD - with the
    human's own uncommitted work of before the run (the summary said "1 file changed": the agent's), and --push
    pushed it. The commit holds HEAD and what changed between the snapshots, as the run left it."""
    (repo / "wip.txt").write_text("my private work in progress\n")  # there before the run, not committed
    (repo / "a.txt").write_text("one\ntwo\nmine\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = S.snapshot(repo, scratch)
    (repo / "agent.txt").write_text("by the agent\n")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "a commit of the agent")  # (on top of the work tree's HEAD)
    after = S.snapshot(repo, scratch)
    assert S.changes(repo, before, after)["files"] == 2  # agent.txt, b.txt
    commit = S.commit_changes(repo, after, "result", "x", before=before, scratch=scratch)
    files = sorted(git(repo, "ls-tree", "-r", "--name-only", commit).split())
    assert files == ["a.txt", "agent.txt", "b.txt"], files
    assert git(repo, "cat-file", "-p", f"{commit}:a.txt") == "one\ntwo", "a.txt as committed, not the human's edit"
    assert git(repo, "rev-parse", f"{commit}^") == git(repo, "rev-parse", "HEAD")
    assert (repo / "wip.txt").exists() and list(scratch.iterdir()) == []
    unchanged = S.snapshot(repo, scratch)  # a run that changed nothing beyond its own commits: HEAD itself
    assert S.commit_changes(repo, unchanged, "result", "x", before=after, scratch=scratch) == \
        git(repo, "rev-parse", "HEAD")


# -- review 3x-1 (security): what a sandbox that can write the workspace and its .git must not get out of a run -----

def test_snapshot_never_reads_through_a_folder_the_agent_made_a_link(repo, tmp_path):
    """Review 3x-1: HEAD names a/key, the agent replaced the folder a by a link to a folder of the host: the
    snapshot hashed the host's file into the workspace's .git/objects (git hash-object -w follows a link on the way
    to a path) - the next run read it there. A path beyond a link is gone, as Git itself sees it; nothing outside
    the workspace is read."""
    host = tmp_path / "hosthome"
    host.mkdir()
    (host / "key").write_text("TOP-SECRET-KEY\n")
    (repo / "a").mkdir()
    (repo / "a" / "key").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "a/key")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = S.snapshot(repo, scratch)
    (repo / "a" / "key").unlink()
    (repo / "a").rmdir()
    os.symlink(host, repo / "a")
    secret = git(repo, "hash-object", str(host / "key"))
    after = S.snapshot(repo, scratch)
    held = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", secret], capture_output=True)
    assert held.returncode != 0, "the host's file is in the workspace's objects"
    listed = git(repo, "ls-tree", "-r", after["tree"])
    assert "120000" in listed and "a/key" not in listed  # the link itself, as a link; the file beyond it: gone
    assert S.changes(repo, before, after)["files"] == 2  # a/key deleted, a (the link) added
    # a link that is a file of the work tree: its target as text, never the file it leads to
    os.symlink(host / "key", repo / "link")
    again = S.snapshot(repo, scratch)
    assert git(repo, "cat-file", "-p", f"{again['tree']}:link") == str(host / "key")
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", secret], capture_output=True).returncode != 0


def test_snapshot_holds_modes_new_files_and_what_was_deleted(repo, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (repo / "sub" / "deep").mkdir(parents=True)
    (repo / "sub" / "deep" / "tool.sh").write_text("#!/bin/sh\n")
    (repo / "sub" / "deep" / "tool.sh").chmod(0o755)
    (repo / "big.bin").write_bytes(b"\0" * (3 << 20) + b"end")
    (repo / "empty").write_text("")
    (repo / "a.txt").unlink()
    found = S.snapshot(repo, scratch)
    listed = git(repo, "ls-tree", "-r", found["tree"])
    assert "100755" in listed and "sub/deep/tool.sh" in listed and "a.txt" not in listed
    assert git(repo, "cat-file", "-s", f"{found['tree']}:big.bin") == str((3 << 20) + 3)
    assert git(repo, "cat-file", "-s", f"{found['tree']}:empty") == "0"
    assert git(repo, "fsck", "--no-dangling") == ""
    assert list(scratch.iterdir()) == []


def test_push_reads_nothing_of_the_workspaces_git_configuration(repo, tmp_path):
    """Review 3x-1: `git config --local` does not list .git/config.worktree, which Git reads once
    extensions.worktreeConfig is set - a credential helper there (!command) ran on the host at the push, a
    url.insteadOf there sent the branch elsewhere. The push runs from a repository of sandbox-ai's own that only
    borrows the workspace's objects: no configuration the agent can write is read."""
    bare, evil = tmp_path / "remote.git", tmp_path / "evil.git"
    for path in (bare, evil):
        git(tmp_path, "init", "-q", "--bare", str(path))
    git(repo, "remote", "add", "origin", str(bare))
    url = S.remote_url(repo, "origin")
    git(repo, "branch", "result")
    marker = tmp_path / "ran-on-the-host"

    def run(argv, **kwargs):
        if "privacy-check" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return subprocess.run(argv, **kwargs)
    git(repo, "config", "extensions.worktreeConfig", "true")
    (repo / ".git" / "config.worktree").write_text(
        f'[url "{evil}"]\n\tinsteadOf = {bare}\n[credential]\n\thelper = "!touch {marker}; :"\n'
        f'[core]\n\tsshCommand = touch {marker}\n\thooksPath = {tmp_path / "hooks"}\n')
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "pre-push").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (tmp_path / "hooks" / "pre-push").chmod(0o755)
    try:
        said = S.push_branch(repo, "origin", url, "result", run)
    except ValueError:
        said = "refused"  # (refusing it is right as well: never pushed elsewhere, nothing run)
    assert not marker.exists(), "a program the workspace's configuration names ran on the host"
    assert git(evil, "branch", "--list") == "", "the branch went to another address than the recorded one"
    if said == "pushed":
        assert git(bare, "rev-parse", "refs/heads/result") == git(repo, "rev-parse", "result")
    assert not [p for p in Path(S.push_folder()).iterdir()] if Path(S.push_folder()).is_dir() else True


def test_a_git_folder_outside_the_workspace_is_never_written(repo, tmp_path):
    """Review 3x-1: .git replaced by a file `gitdir: /another/repository/.git` (or a link to it) made the snapshot
    write objects and --commit move a branch in that repository of the host."""
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "f").write_text("f\n")
    git(other, "add", "f")
    git(other, "commit", "-q", "-m", "theirs")
    import shutil
    shutil.rmtree(repo / ".git")
    (repo / ".git").write_text(f"gitdir: {other / '.git'}\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(ValueError, match="outside the workspace"):
        S.snapshot(repo, scratch)
    (repo / ".git").unlink()
    os.symlink(other / ".git", repo / ".git")
    with pytest.raises(ValueError, match="outside the workspace"):
        S.snapshot(repo, scratch)
    with pytest.raises(ValueError, match="outside the workspace"):
        S.commit_changes(repo, {"tree": git(other, "rev-parse", "HEAD^{tree}"), "head": None}, "result", "x")
    assert git(other, "branch", "--list", "result") == ""


def test_the_summary_keeps_a_file_name_inside_its_code_block(repo):
    entry = {"run": "20261117-010000-abcd", "reason": "now", "status": "ok", "started": utc(2026, 11, 17, 1),
             "seconds": 5, "start": "nightly-1", "agent": "claude",
             "changes": {"files": 1, "insertions": 1, "deletions": 0, "commits": 0,
                         "stat": " ```\n# Approve the push: [here](https://evil.example) | 1 +\n"}}
    text = S.summary_markdown(schedule(workdir=str(repo)), entry, {})
    assert text.count("```") == 2, "a file's name ended the code block: Markdown of the agent's in the summary"


def test_a_schedules_board_is_the_one_kept_here_never_a_file_where_the_run_starts(home, tmp_path, monkeypatch):
    """Review 3x-1: `--board NAME` was read as a file of the current folder when one of that name was there - a
    file the agent wrote into the workspace chose the unattended start's options (run-now from that folder)."""
    bundle = S.load_module("board_bundle")
    kept = {"schema": "sandbox-ai-board", "version": 1, "name": "nightly", "agent": "claude",
            "options": ["--offline"], "agent_args": []}
    bundle.boards_dir().mkdir(parents=True)
    bundle.board_path("nightly").write_text(json.dumps(kept))
    planted = tmp_path / "workspace"
    planted.mkdir()
    (planted / "nightly").write_text(json.dumps({**kept, "options": ["--mount", "/:/host:rw"]}))
    monkeypatch.chdir(planted)
    assert S.board_document("nightly")["options"] == ["--offline"]
    argv = S.start_argv(schedule(board="nightly"), "nightly-1")
    assert "--offline" in argv and "--mount" not in argv
    with pytest.raises(ValueError, match="no board"):
        S.board_document("./nightly")
    import io
    assert bundle.main(["show", "nightly", "--json"], out=lambda *_: None) == 0


def test_the_runs_commit_in_a_sha256_repository(tmp_path):
    """Review 3x-1: a deleted path was written with SHA-1's 40 zeros: `git update-index` refused it in a SHA-256
    repository, so a run that deleted a file was never committed there."""
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        git(repo, "init", "-q", "-b", "main", "--object-format=sha256")
    except subprocess.CalledProcessError:
        pytest.skip("this Git has no SHA-256 repositories")
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "first")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = S.snapshot(repo, scratch)
    (repo / "a.txt").unlink()
    (repo / "new.txt").write_text("new\n")
    after = S.snapshot(repo, scratch)
    assert S.changes(repo, before, after)["files"] == 2
    commit = S.commit_changes(repo, after, "result", "x", before=before, scratch=scratch)
    assert git(repo, "ls-tree", "-r", "--name-only", commit).split() == ["b.txt", "new.txt"]


def test_snapshot_leaves_a_nested_repository_as_it_is(repo, tmp_path):
    """An untracked repository inside the work tree is listed as "folder/": neither read nor counted as deleted."""
    nested = repo / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "-q", "-b", "main")
    (nested / "x.c").write_text("x\n")
    git(nested, "add", "x.c")
    git(nested, "commit", "-q", "-m", "theirs")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = S.snapshot(repo, scratch)
    (nested / "x.c").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    after = S.snapshot(repo, scratch)
    found = S.changes(repo, before, after)
    assert found["files"] == 1 and "new.txt" in found["stat"] and "vendor" not in found["stat"]


def test_no_program_of_the_workspace_runs_on_the_host_through_path(repo, tmp_path, monkeypatch):
    """Review 3x-1: the timer keeps the PATH `schedule enable` was typed with - with an activated virtualenv or
    node_modules/.bin of a project in it, and relative entries (the run's launcher starts in the workspace). The
    agent writes those folders: a `git` planted there ran on the host at the next tick. What a schedule runs on the
    host is never looked for in its workspace."""
    marker = tmp_path / "ran-on-the-host"
    tools = repo / ".venv" / "bin"
    tools.mkdir(parents=True)
    for name in ("git", "podman"):
        (tools / name).write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        (tools / name).chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), ".", "node_modules/.bin", os.environ["PATH"]]))
    kept = S.safe_path(os.environ["PATH"], [repo]).split(os.pathsep)
    assert str(tools) not in kept and "." not in kept and "node_modules/.bin" not in kept and "/usr/bin" in kept
    assert S.safe_path(f"/usr/bin{os.pathsep}{os.pathsep}/bin", []) == f"/usr/bin{os.pathsep}/bin"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (repo / "new.txt").write_text("new\n")
    assert S.snapshot(repo, scratch)["files"] >= 2 and S.refs_digest(repo)
    assert not marker.exists(), "the workspace's own git ran on the host"
    env = S.run_environment(schedule(workdir=str(repo)))
    assert str(tools) not in env["PATH"].split(os.pathsep) and env["SANDBOX_AI_SCHEDULE"] == "nightly"


def test_add_says_at_once_that_commit_cannot_work_with_a_git_folder_elsewhere(home, tmp_path, capsys):
    """A linked worktree (its .git is a file that leads to the main repository): --commit is refused when the
    schedule is added, not found out at the first run."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / "a").write_text("a\n")
    git(main, "add", "a")
    git(main, "commit", "-q", "-m", "first")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "-q", str(linked), "-b", "side")
    status = S.main(["add", "nightly", "--every", "1d", "--prompt", "x", "--workdir", str(linked), "--commit", "result",
                     "--", "claude"], out=lambda *_: None)
    assert status == 1 and "outside the workspace" in capsys.readouterr().err
    assert S.main(["add", "nightly", "--every", "1d", "--prompt", "x", "--workdir", str(linked), "--", "claude"],
                  out=lambda *_: None) == 0
