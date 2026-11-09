"""A run whose launcher (its supervisor on the host) was killed while its Podman pod runs on: its run directory must
stay, `sandbox-ai list` shows it "orphaned", `stop` ends its pod and records the end, and `resume` works after that
(liveness.py, net.running, sessions.py, starts.py). Podman is faked: nothing here talks to a real one."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_starts_liveness_test", ROOT / "sandbox/starts.py")
st = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(st)
sessions = st.module("sessions")
net = st.module("net")
live = st.module("liveness")
launcher = st.launcher()
DEAD_PID = 2 ** 22 + 7
INSIDE_SANDBOX = live.inside_sandbox


class FakePodman:
    """`podman pod ps --format json`, `pod stop`, `pod rm` over a dict of pods; every command is kept."""

    def __init__(self):
        self.pods, self.calls, self.answer = {}, [], True

    def add(self, name, status="Running", live=1, labels=None, infra=True):
        self.pods[name] = {"status": status, "live": live, "labels": labels or {}, "infra": infra}

    def __call__(self, cmd, **kwargs):
        assert cmd[0] == "podman" and kwargs.get("timeout"), cmd
        args = cmd[1:]
        if args[:1] == ["--remote"]:
            args = args[3:]
        self.calls.append(args)
        if not self.answer:
            return SimpleNamespace(returncode=125, stdout="", stderr="Error: cannot connect to Podman")
        if args[:2] == ["pod", "ps"]:
            listed = [{"Name": name, "Status": pod["status"], "InfraId": f"infra-{name}", "Labels": pod["labels"],
                       "Containers": [{"Id": f"infra-{name}", "Status": "running" if pod["infra"] else "exited"}]
                       + [{"Id": f"{name}-{i}", "Status": "running"} for i in range(pod["live"])]}
                      for name, pod in self.pods.items()]
            return SimpleNamespace(returncode=0, stdout=json.dumps(listed), stderr="")
        pod = args[-1]
        if pod not in self.pods:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"Error: no such pod {pod}")
        if args[:2] == ["pod", "stop"]:
            self.pods[pod].update(status="Exited", live=0, infra=False)
            return SimpleNamespace(returncode=0, stdout=pod, stderr="")
        if args[:2] == ["pod", "rm"]:
            if self.pods[pod]["live"] and "--force" not in args:
                return SimpleNamespace(returncode=125, stdout="", stderr="Error: pod has running containers")
            del self.pods[pod]
            return SimpleNamespace(returncode=0, stdout=pod, stderr="")
        raise AssertionError(args)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(sessions, "PROC", tmp_path / "proc")
    monkeypatch.setattr(net, "USER_RUNTIME", tmp_path / "run-user")
    monkeypatch.setattr(sessions.sys.stdin, "isatty", lambda: False)
    # As on the host (these tests may themselves run inside a sandbox): the test that leaves this says so.
    monkeypatch.setattr(live, "inside_sandbox", lambda *a, **k: False)

    def real_podman(*args, **kwargs):
        raise AssertionError(f"a real podman was asked: {args}")
    monkeypatch.setattr(live, "RUNNER", real_podman)  # the podman fixture gives a fake one
    live._cache.clear()


@pytest.fixture
def podman(monkeypatch):
    fake = FakePodman()
    monkeypatch.setattr(live, "RUNNER", fake)
    return fake


def register(run, pid=DEAD_PID, browser=True, **fields):
    """A run.json as a launcher writes it (PID: its launcher; DEAD_PID is gone), and its browser directory."""
    directory = launcher.runs_dir() / run
    directory.mkdir(parents=True)
    record = {"run": run, "agent": "claude", "backend": "podman", "workspace": "/srv/app", "pid": pid,
              "started": "2026-11-07T17:02:41Z", "session": None, "pod": f"agent-{run}", **fields}
    (directory / "run.json").write_text(json.dumps(record))
    if browser:
        directory.with_name(run + ".browser").mkdir()
    return directory


def make_session(key, run="claude-3123c2f0", pid=DEAD_PID, keep=True, tmp_path=None, **fields):
    """A session as sessions.launch leaves it: its supervisor PID (DEAD_PID: gone), its run and pod."""
    directory = sessions.session_dir(key)
    for path in (directory, directory / "home", directory / "tmux"):
        path.mkdir(parents=True, exist_ok=True)
    data = {"agent": "claude", "options": ["--backend", "podman"], "agent_args": [], "backend": "podman",
            "workspace": "/srv/app", "cwd": "/", "created": "2026-11-07T17:02:41Z", "started": "2026-11-07T17:02:41Z",
            "name": key, "keep": keep, "mode": "tmux", "ready": True, "run": run, "pod": f"agent-{run}", "pid": pid,
            **fields}
    if pid != DEAD_PID and tmp_path is not None:
        entry = tmp_path / "proc" / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"python3\0agent_sandbox.py\0--tmux-socket-dir\0"
                                        + os.fsencode(str(directory / "tmux")) + b"\0")
        (entry / "stat").write_text(f"{pid} (python3) S 1 1 1 0 -1\n")
    sessions.save(key, data)
    return data


def session_label(key):
    return {live.SESSION_LABEL: str((sessions.session_dir(key) / "tmux").resolve()),
            live.RUN_LABEL: "claude-3123c2f0"}


# --- the cause -------------------------------------------------------------------------------------------------


def test_a_killed_launcher_no_longer_costs_a_running_sandbox_its_run_directory(podman):
    """The bug: the launcher of claude-3123c2f0 was gone (killed without its cleanup) while Podman's conmon kept its
    container running; the next `sandbox-ai list` (the app asks every 10 s) took the missing PID for the end of the run
    and removed its run directory, the sandbox's control socket with it. Now its directory stays, and the run is listed
    as orphaned."""
    directory = register("claude-3123c2f0")
    podman.add("agent-claude-3123c2f0", live=1)
    orphans = {}
    assert net.running(orphans) == {}  # no launcher: nothing to send net allow or workspace add to
    assert directory.is_dir() and directory.with_name("claude-3123c2f0.browser").is_dir()
    assert orphans["claude-3123c2f0"]["orphaned"] is True and orphans["claude-3123c2f0"]["directory"] == str(directory)
    found = {entry["id"]: entry for entry in st.entries()}
    assert found["claude-3123c2f0"]["state"] == "orphaned" and found["claude-3123c2f0"]["attach_why"] == "orphaned"
    assert not [call for call in podman.calls if call[:2] != ["pod", "ps"]]  # looked, changed nothing


def test_an_ended_run_goes_with_its_browser_directory_pod_and_a_recorded_end(podman):
    """Positive evidence: the launcher is gone and no container of its pod runs. Then its directories go (the browser
    directory too, which no one removed before), the pod Podman still lists, and the end is noted once."""
    make_session("claude-axross-de39", run="claude-0badc0de")
    directory = register("claude-0badc0de", start="claude-axross-de39", name="claude-axross-de39")
    podman.add("agent-claude-0badc0de", status="Exited", live=0, infra=False)
    podman.add("agent-claude-1111aaaa", status="Running", live=1)  # someone else's run: untouched
    assert net.running() == {}
    assert not directory.exists() and not directory.with_name("claude-0badc0de.browser").exists()
    assert ["pod", "rm", "agent-claude-0badc0de"] in podman.calls and "agent-claude-1111aaaa" in podman.pods
    ended = json.loads((sessions.session_dir("claude-axross-de39") / "exit.json").read_text())
    assert ended["run"] == "claude-0badc0de" and ended["status"] is None and ended["ended"]
    assert [e["event"] for e in st.history("claude-axross-de39")] == ["exit"]


def test_nothing_is_removed_where_the_launcher_cannot_be_judged(podman, monkeypatch):
    """Inside a sandbox the host's PIDs are not visible; in another PID namespace neither; and Podman that does not
    answer cannot say whether the pod runs: the directory stays in each case (and is not listed as running)."""
    old = register("claude-00000001", backend="bwrap", pod=None)
    monkeypatch.setattr(live, "inside_sandbox", lambda *a, **k: True)
    assert net.running() == {} and old.is_dir()
    other = register("claude-00000002", backend="bwrap", pod=None, pid_ns="pid:[1]")
    assert net.running() == {} and other.is_dir()
    # A record of this PID namespace is judged even in a container (where the launcher runs too: a toolbox).
    own = register("claude-00000003", backend="bwrap", pod=None, pid_ns=live.pid_namespace())
    net.running()
    assert not own.exists() and old.is_dir() and other.is_dir()
    monkeypatch.setattr(live, "inside_sandbox", lambda *a, **k: False)
    old.joinpath("run.json").write_text(json.dumps({**json.loads(old.joinpath("run.json").read_text()),
                                                   "backend": "podman", "pod": "agent-claude-00000001"}))
    podman.answer = False
    orphans = {}
    assert net.running(orphans) == {} and orphans == {} and old.is_dir()
    podman.answer = True
    live._cache.clear()
    net.running()
    assert not old.exists() and other.is_dir()  # no such pod: ended; the other namespace's still stays


def test_verdict_and_pod_names():
    assert live.pod_of({"run": "claude-1", "pod": "agent-mcp-search"}) == "agent-mcp-search"
    assert live.pod_of({"run": "claude-1", "pod": "evil;rm"}) == "agent-claude-1"
    assert live.pod_of({"run": "../x"}) is None
    assert live.verdict({"run": "claude-1", "backend": "bwrap"}) == "dead"
    assert live.verdict({"run": "../x", "backend": "podman"}) == "unknown"
    assert INSIDE_SANDBOX({"SANDBOX_AI_RUN": "claude-1"}, Path("/nonexistent")) is True
    assert INSIDE_SANDBOX({}, Path("/nonexistent")) is False


def test_no_podman_program_means_no_pod_runs(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(live, "RUNNER", missing)
    assert live.pods() == {} and live.pod_live("agent-claude-1") is False


# --- sessions: orphaned, stop, resume --------------------------------------------------------------------------


def test_a_session_whose_supervisor_died_is_orphaned_attachable_and_resumed_after_its_pod(podman, monkeypatch,
                                                                                         capsys):
    """The user's case: the session's supervisor is gone (and an older version removed its run directory), its pod
    still runs. list shows it orphaned with its run (attachable: its terminal relay runs in the sandbox); resume, as
    asked, ends that pod first (said), records the end and starts it again with its HOME and conversation."""
    make_session("claude-axross-public-cbcf")
    podman.add("agent-claude-3123c2f0", live=1, labels=session_label("claude-axross-public-cbcf"))
    data = sessions.load("claude-axross-public-cbcf")
    assert sessions.status("claude-axross-public-cbcf", data) == "orphaned"
    entry = {e["id"]: e for e in st.entries()}["claude-axross-public-cbcf"]
    assert (entry["state"], entry["run"], entry["pid"], entry["attachable"], entry["attach_why"]) == \
        ("orphaned", "claude-3123c2f0", None, True, None)
    assert all(call[:2] == ["pod", "ps"] for call in podman.calls)  # listing touches nothing
    launched = []
    monkeypatch.setattr(sessions, "launch", lambda name, data, args, a=None, event="rebirth": launched.append(
        (name, data["run"], args)) or 0)
    monkeypatch.setattr(sessions, "review_home", lambda name, data: None)
    monkeypatch.setattr(sessions, "effective", lambda data: (None, None))
    assert sessions.resume("claude-axross-public-cbcf") == 0
    assert ["pod", "stop", "--time", "30", "agent-claude-3123c2f0"] in podman.calls
    assert "agent-claude-3123c2f0" not in podman.pods
    assert launched == [("claude-axross-public-cbcf", "claude-3123c2f0", ["--continue"])]
    assert "ending its pod first, then it starts again" in capsys.readouterr().err


def test_stop_ends_the_orphaned_pod_records_the_end_and_resume_works(podman, monkeypatch, capsys):
    make_session("claude-axross-public-cbcf")
    left = register("claude-3123c2f0", start="claude-axross-public-cbcf")  # a newer version kept it
    podman.add("agent-claude-3123c2f0", live=1, labels=session_label("claude-axross-public-cbcf"))
    assert st.main(["stop", "claude-axross-public-cbcf"]) == 0
    assert ["pod", "stop", "--time", "30", "agent-claude-3123c2f0"] in podman.calls
    assert "agent-claude-3123c2f0" not in podman.pods
    assert not left.exists() and not left.with_name("claude-3123c2f0.browser").exists()
    ended = json.loads((sessions.session_dir("claude-axross-public-cbcf") / "exit.json").read_text())
    assert ended["run"] == "claude-3123c2f0"
    assert [e["event"] for e in st.history("claude-axross-public-cbcf")] == ["stop", "exit"]
    err = capsys.readouterr().err
    assert "continue it with: sandbox-ai resume claude-axross-public-cbcf" in err
    data = sessions.load("claude-axross-public-cbcf")
    assert sessions.status("claude-axross-public-cbcf", data) == "stopped"
    # resume starts it again (the agent continues its conversation); the HOME is the same.
    launched = []
    monkeypatch.setattr(sessions, "launch", lambda name, data, args, a=None, event="rebirth": launched.append(
        (name, args)) or 0)
    monkeypatch.setattr(sessions, "review_home", lambda name, data: None)
    monkeypatch.setattr(sessions, "effective", lambda data: (None, None))
    assert sessions.resume("claude-axross-public-cbcf") == 0
    assert launched == [("claude-axross-public-cbcf", ["--continue"])]
    assert (sessions.session_dir("claude-axross-public-cbcf") / "home").is_dir()


def test_stop_warns_about_a_pod_without_this_sessions_label_and_stops_it(podman, capsys):
    """The human named the start: its recorded pod is stopped even when its label is another's (said)."""
    make_session("claude-axross-public-cbcf")
    podman.add("agent-claude-3123c2f0", live=1, labels={live.SESSION_LABEL: "/elsewhere/tmux"})
    assert sessions.stop("claude-axross-public-cbcf") == 0
    assert "agent-claude-3123c2f0" not in podman.pods
    assert "does not carry claude-axross-public-cbcf's label" in capsys.readouterr().err


def test_stop_by_name_ends_a_running_pod_no_start_knows(podman, capsys):
    """A pod whose launcher is gone and whose run directory an older version removed: gc names it, and
    `sandbox-ai stop` with its pod name (or run id) ends it, as asked."""
    podman.add("agent-claude-feedf00d", live=1)
    lines = []
    st.show_gc(st.gc(dry_run=True), out=lines.append)
    assert any("end it with: sandbox-ai stop agent-claude-feedf00d" in line for line in lines)
    assert st.act("stop", "claude-feedf00d", out=lines.append, wait=5) == 0
    assert "agent-claude-feedf00d" not in podman.pods and lines[-1] == "agent-claude-feedf00d: stopped"
    assert "no start knows agent-claude-feedf00d" in capsys.readouterr().err
    with pytest.raises(ValueError, match="nothing named nothing-here"):
        st.act("stop", "nothing-here", out=lines.append)


def test_a_stopped_session_with_a_recorded_end_asks_podman_nothing(podman):
    make_session("claude-done")
    (sessions.session_dir("claude-done") / "exit.json").write_text(json.dumps({"run": "claude-3123c2f0", "status": 0,
                                                                                "ended": "2026-11-08T00:00:00Z"}))
    assert sessions.status("claude-done", sessions.load("claude-done")) == "stopped" and podman.calls == []


def test_prune_never_removes_the_home_of_a_sandbox_that_may_still_run(podman):
    """A start that keeps no HOME is removed once it ended: not while its pod runs on (orphaned), not when Podman does
    not answer, not when its supervisor ran in another PID namespace; once its pod is gone, it is."""
    make_session("claude-tmp", keep=False)
    podman.add("agent-claude-3123c2f0", live=1)
    assert sessions.prune("claude-tmp") is False and sessions.session_dir("claude-tmp").is_dir()
    podman.answer = False
    live._cache.clear()
    assert sessions.prune("claude-tmp") is False and sessions.session_dir("claude-tmp").is_dir()
    podman.answer = True
    del podman.pods["agent-claude-3123c2f0"]
    live._cache.clear()
    sessions.save("claude-tmp", {**sessions.load("claude-tmp"), "pid_ns": "pid:[1]"})
    assert sessions.prune("claude-tmp") is False
    sessions.save("claude-tmp", {**sessions.load("claude-tmp"), "pid_ns": live.pid_namespace()})
    assert sessions.prune("claude-tmp") is True and not sessions.session_dir("claude-tmp").exists()


def test_a_running_supervisor_is_running_whatever_podman_says(podman, tmp_path):
    make_session("claude-live", pid=4242, tmp_path=tmp_path)
    assert sessions.status("claude-live", sessions.load("claude-live")) == "running" and podman.calls == []


# --- a run without a kept start -----------------------------------------------------------------------------------


def test_stop_ends_an_orphaned_run(podman):
    directory = register("codex-12345678", agent="codex", name="quiet-run")
    podman.add("agent-codex-12345678", live=1)
    lines = []
    with pytest.raises(ValueError, match="end it with: sandbox-ai stop quiet-run"):
        st.act("pause", "quiet-run", out=lines.append)
    assert st.act("stop", "quiet-run", out=lines.append, wait=5) == 0
    assert ["pod", "stop", "--time", "5", "agent-codex-12345678"] in podman.calls
    assert "agent-codex-12345678" not in podman.pods and not directory.exists()
    assert [e["event"] for e in st.history("quiet-run")] == ["stop", "exit"]
    assert lines[-1] == "quiet-run: stopped"


# --- gc: leftover pods --------------------------------------------------------------------------------------------


def test_gc_removes_only_sandbox_ais_ended_pods(podman):
    podman.add("agent-claude-0123abcd", status="Exited", live=0, infra=False)        # a reboot's leftover
    podman.add("agent-mcp-fetch", status="Exited", live=0, infra=False)
    podman.add("agent-other", status="Exited", live=0, infra=False, labels={live.RUN_LABEL: "other"})
    podman.add("agent-codex-89abcdef", status="Created", live=0)                    # a start in progress
    podman.add("agent-mcp-search", status="Running", live=1)                        # runs: never
    register("mcp-search", pid=os.getpid(), kind="mcp", pod="agent-mcp-search", browser=False)  # its server
    podman.add("agent-claude-feedf00d", status="Running", live=1)                   # runs, no start knows it
    podman.add("sd-db", status="Exited", live=0, infra=False)                       # the user's own
    podman.add("lebara-x", status="Exited", live=0, infra=False)
    podman.add("agent-mine", status="Exited", live=0, infra=False)                  # agent-*, but not sandbox-ai's
    preview = st.gc(dry_run=True)
    assert preview["pods"] == ["agent-claude-0123abcd", "agent-mcp-fetch", "agent-other"]
    assert preview["orphaned_pods"] == ["agent-claude-feedf00d"] and len(podman.pods) == 9
    lines = []
    st.show_gc(preview, out=lines.append)
    assert "would remove 3 Podman pod(s) of ended runs: agent-claude-0123abcd, agent-mcp-fetch, agent-other" in lines
    assert any("end it with: sandbox-ai stop agent-claude-feedf00d" in line for line in lines)
    result = st.gc()
    assert result["pods"] == ["agent-claude-0123abcd", "agent-mcp-fetch", "agent-other"]
    assert sorted(podman.pods) == ["agent-claude-feedf00d", "agent-codex-89abcdef", "agent-mcp-search", "agent-mine",
                                   "lebara-x", "sd-db"]
    assert not any("--force" in call for call in podman.calls)


def test_gc_leaves_the_pod_of_an_orphaned_session(podman):
    make_session("claude-axross-public-cbcf")
    podman.add("agent-claude-3123c2f0", live=1, labels=session_label("claude-axross-public-cbcf"))
    result = st.gc(dry_run=True)
    assert result["pods"] == [] and result["orphaned_pods"] == []


def test_write_exit_never_overwrites_the_end_of_a_later_run():
    make_session("claude-again", run="claude-22222222")
    st.write_exit("claude-again", "claude-11111111", None)
    assert not (sessions.session_dir("claude-again") / "exit.json").exists()
    st.write_exit("claude-again", "claude-22222222", 0)
    assert json.loads((sessions.session_dir("claude-again") / "exit.json").read_text())["run"] == "claude-22222222"


# --- the launcher ------------------------------------------------------------------------------------------------


def test_the_launcher_records_its_pid_namespace_and_labels_its_pod(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    a = launcher.parse_args(["claude", "--workspace", str(tmp_path / "app")])
    launcher.validate(a)
    record = json.loads((launcher.register_run(a, "claude-1234abcd", tmp_path / "session") / "run.json").read_text())
    assert record["pid_ns"] == live.pid_namespace() and record["pid_ns"]
    create, _ = launcher.pod_commands(a, "agent-claude-1234abcd", tmp_path / "session", tmp_path / "home")
    assert create[create.index("--label") + 1] == "sandbox-ai.run=claude-1234abcd"
    assert launcher.RUN_LABEL == live.RUN_LABEL and launcher.SESSION_LABEL == live.SESSION_LABEL


def test_a_pod_the_launcher_could_not_remove_is_said(tmp_path, monkeypatch, capsys):
    """The end of a run removes its pod; when Podman refuses (an old Podman without --time: once more without it),
    the pod left behind is said with the command that removes it, never hidden."""
    a = launcher.parse_args(["codex", "--offline"])
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pod", "rm"]:
            return SimpleNamespace(returncode=125, stdout="", stderr="Error: some podman failure\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd: None)
    monkeypatch.setattr(launcher, "wait_child", lambda child: 0)
    assert launcher.run_pod(a, tmp_path, tmp_path) == 0
    removals = [cmd for cmd in calls if cmd[1:3] == ["pod", "rm"]]
    assert [cmd[3:-1] for cmd in removals] == [["--force", "--time", "0"], ["--force"]]
    assert f"could not remove the pod {removals[0][-1]} (Error: some podman failure); remove it with: podman pod rm " \
           f"--force {removals[0][-1]}" in capsys.readouterr().err
    calls.clear()

    def gone(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pod", "rm"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="Error: no such pod agent-codex-x\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(launcher.subprocess, "run", gone)
    assert launcher.run_pod(a, tmp_path, tmp_path) == 0
    assert len([cmd for cmd in calls if cmd[1:3] == ["pod", "rm"]]) == 1 and capsys.readouterr().err == ""


def test_stop_pod_reports_what_podman_said(podman):
    stopped, said = live.stop_pod("agent-claude-404", None, 5)
    assert stopped is True and "no such pod" in said  # nothing of it runs: stopped
    podman.add("agent-claude-1", live=1)
    assert live.stop_pod("agent-claude-1", None, 5) == (True, "")
