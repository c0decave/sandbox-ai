"""Deep test round 2 (user journeys across 0.1.93-0.1.95): the paths a real user takes through several features in a
row - first start after install, privacy setup, power user's board, local models, recovery - driven through the real
./sandbox-ai entry point with only Podman, tmux and npm faked (live_fakes.py). Every friction point or bug found on
the way is a test named after it.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from unittest.mock import Mock

import pytest

from live_fakes import Host, ROOT


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path)
    h.env.pop("SANDBOX_AI_RUN", None)  # (this suite may run inside a sandbox-ai run)
    return h


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# == Journey 1: the first-time user - build all, a preset from the list, a session start, a failed start ============

def test_build_all_says_the_cgroupfs_note_once_not_per_image(host):
    """`sandbox-ai build all` on a host without a systemd user session (an LXC container, a login without systemd)
    said "no systemd user session: building with --cgroup-manager=cgroupfs (...)" twelve times, once per image, in
    between the lines that matter. Said once, by the first build; every build still uses cgroupfs."""
    if Path(f"/run/user/{os.getuid()}/bus").exists():
        pytest.skip("this host has a systemd user session: the note is never said")
    result = host.run("build", "all", timeout=600)
    assert result.stderr.count("no systemd user session") == 1, result.stderr
    builds = host.builds()
    assert len(builds) >= 10 and all("--cgroup-manager=cgroupfs" in call for call in builds)


def test_presets_list_keeps_a_space_after_a_long_preset_name(host):
    """`sandbox-ai presets list` printed "showcase-second-opinion Showcase: ..." - the name ran into its title, as the
    column was 22 characters wide and that id has 23. The column is as wide as the longest id."""
    result = host.run("presets", "list")
    assert result.returncode == 0, result.stderr
    rows = [line for line in result.stdout.splitlines() if line.startswith("  ")]
    columns = {len(line) - len(line[2:].split(" ", 1)[1].lstrip()) for line in rows}
    assert len(columns) == 1, f"the titles start in different columns: {sorted(columns)}"
    assert any(line.split()[0] == "showcase-second-opinion" for line in rows)


def test_a_failed_session_start_says_its_supervisors_lines_once(host):
    """A session start that failed (here: Podman refused the pod) showed every line of its supervisor twice: once as
    the start went on, then again under "Session failed to start (status 1):". The second time only what was not
    shown yet (Podman's own words, a line cut short), and nothing of an earlier start in the same log."""
    host.add_image("localhost/claude-sandbox:latest")
    first = host.run("claude", "--session", "twice")
    assert first.returncode != 0
    assert first.stderr.count("starting the sandbox: its firewall") == 1, first.stderr
    assert "Session failed to start (status 1)" in first.stderr
    assert "fake podman: the start stops here" in first.stderr, "Podman's own words are said"
    # The next start with the same name: its log is appended to the same supervisor.log; none of the first one's
    # lines are said again.
    host.stop_path.unlink()
    again = host.run("claude", "--session", "twice")
    assert again.returncode != 0
    assert again.stderr.count("starting the sandbox: its firewall") == 1, again.stderr
    assert again.stderr.count("fake podman: the start stops here") == 1, again.stderr


def test_a_session_start_says_its_notes_once(host):
    """A session start (the app's Start, `sandbox-ai qwen --session q1`) said its notes twice: "note: Qwen Code: not
    live-tested yet ...", "no systemd user session (podman: cgroupfs), so --memory ... are not enforced", the
    browser's and the devices' notes - once by the terminal's own check, once more by its supervisor (relayed). The
    supervisor says them; the terminal does not say them first."""
    host.add_image("localhost/qwen-sandbox:latest")
    result = host.run("qwen", "--session", "q1", "--memory", "2g")
    assert host.stopped_at() is not None, result.stderr
    assert result.stderr.count("not live-tested yet") == 1, result.stderr
    assert result.stderr.count("--pids are not enforced") <= 1, result.stderr
    # A dry run (the app's check) and a start in the foreground still say them, once.
    dry = host.run("qwen", "--memory", "2g", "--dry-run")
    assert dry.stderr.count("not live-tested yet") == 1, dry.stderr


def session_of_0_1_94(host, name, options):
    """A session kept by 0.1.94 and stopped (its session.json as 0.1.94's start_locked wrote it, its run ended)."""
    directory = host.home / ".local/state/sandbox-ai/sessions" / name
    (directory / "home").mkdir(parents=True, mode=0o700)
    (directory / "tmux").mkdir(mode=0o700)
    data = {"agent": "claude", "options": options, "agent_args": [], "backend": "podman", "podman_url": None,
            "workspace": None, "cwd": str(host.workspace), "created": "2026-11-13T09:00:00Z", "cwd_project": None,
            "ready": True, "name": name, "project": host.workspace.name, "keep": True, "mode": "tmux",
            "pod": "agent-claude-0123abcd", "pid": 2 ** 22 + 17, "pid_start": 1, "started": "2026-11-13T09:00:01Z"}
    (directory / "session.json").write_text(json.dumps(data))
    (directory / "exit.json").write_text(json.dumps({"status": 0, "ended": "2026-11-13T18:00:00Z"}))
    return directory


def test_resuming_a_session_of_0_1_94_with_telemetry_on_says_its_note_once(host):
    """Journey: a session started with --telemetry on under 0.1.94, resumed the next morning after the update. Every
    resume said "telemetry is always off since 0.1.95; 'on' is ignored" twice - once by the resume, once more by its
    supervisor (relayed). Once; the supervisor is started without the retired level; the start goes on."""
    host.add_image("localhost/claude-sandbox:latest")
    session_of_0_1_94(host, "day1", ["--telemetry", "on", "--allow-pypi"])
    listed = host.run("list", "--stopped")  # (the default shows what runs)
    assert "day1" in listed.stdout and "stopped" in listed.stdout, listed.stdout + listed.stderr
    resumed = host.run("resume", "day1")
    assert resumed.stderr.count("telemetry is always off since 0.1.95") == 1, resumed.stderr
    assert host.stopped_at() is not None, "the resume got to its pod: " + resumed.stderr
    saved = json.loads((host.home / ".local/state/sandbox-ai/sessions/day1/session.json").read_text())
    assert saved["options"][:2] == ["--telemetry", "on"], "the record itself is left as it was"


def test_doctor_names_damaged_settings_files_and_sessions(host):
    """Journey 6, recovery: llm.json and hosts.json damaged (a crash in an editor, a full disk), a session folder
    without a readable session.json. `llm list`, `hosts list` and `list` each said it, but `sandbox-ai doctor` - the
    command for "what is wrong here?" - said nothing of them. It names every damaged file and session, with the
    fix."""
    config = host.home / ".config/sandbox-ai"
    config.mkdir(parents=True)
    (config / "llm.json").write_text('{"endpoints": {')
    (config / "hosts.json").write_text("")
    (config / "mcp.json").write_text('{"servers": {}}')
    sessions = host.home / ".local/state/sandbox-ai/sessions"
    (sessions / "broken").mkdir(parents=True)
    (sessions / "broken" / "session.json").write_text("garbage")
    result = host.run("doctor", "--json")
    found = {row["check"]: row for row in json.loads(result.stdout)}
    assert "Settings files" in found, result.stdout[-2000:] + result.stderr[-2000:]
    row = found["Settings files"]
    assert row["status"] == "warn" and "llm.json" in row["detail"] and "hosts.json" in row["detail"], row
    assert "mcp.json" not in row["detail"], "a readable one is not named"
    assert found["Sessions"]["status"] == "warn" and "broken" in found["Sessions"]["detail"], found["Sessions"]
    assert str(sessions / "broken") in found["Sessions"]["hint"]


def test_a_session_start_whose_supervisor_was_killed_says_by_which_signal(tmp_path):
    """"Session failed to start (status -9)": Python's code for "ended by signal 9". Said in words."""
    sessions = load("sandbox_sessions_deep2", "sandbox/sessions.py")
    directory = tmp_path / "s"
    (directory / "tmux").mkdir(parents=True)
    (directory / "supervisor.log").write_text("agent-sandbox: starting the sandbox\n")
    with pytest.raises(RuntimeError) as failed:
        sessions.wait_ready(directory, Mock(poll=Mock(return_value=-9), returncode=-9), out=lambda text: None)
    assert "status -9" not in str(failed.value)
    assert "ended by SIGKILL" in str(failed.value), failed.value


# == Journey 5: the local-LLM user - llm add, its parameters, a start with --llm ======================================

def test_llm_params_takes_the_servers_name(host):
    """After `sandbox-ai llm add lms -k lmstudio ...` the user asked `sandbox-ai llm params lms` (which parameters
    can this server take?) and got argparse's "unrecognized arguments: lms". A server's name is taken: its kind's
    parameters, with where that server's own values are set."""
    added = host.run("llm", "add", "lms", "-k", "lmstudio", "-u", "http://127.0.0.1:1234")
    assert added.returncode == 0, added.stderr
    by_name, by_kind = host.run("llm", "params", "lms"), host.run("llm", "params", "--kind", "lmstudio")
    assert by_name.returncode == 0, by_name.stderr
    assert by_kind.stdout in by_name.stdout and by_kind.stdout.strip()
    assert "sandbox-ai llm set lms --param KEY=VALUE" in by_name.stdout
    as_json = host.run("llm", "params", "lms", "--json")
    assert json.loads(as_json.stdout) == json.loads(host.run("llm", "params", "-k", "lmstudio", "-j").stdout)
    unknown = host.run("llm", "params", "nope")
    assert unknown.returncode != 0 and "no LLM endpoint nope" in unknown.stderr, unknown.stderr
    assert "Traceback" not in unknown.stderr


def test_llm_set_loop_guard_says_when_a_layer_cannot_act(host):
    """The loop guard switched off for a server (`llm set lms --loop-guard off`), later `--loop-guard warn=2`: kept
    without a word, and the guard stayed off ("loop guard: off ..."). And `warn=5` with `block=3`: tool use is
    paused before the model was ever warned. Both kept as asked, each with a note that says so."""
    assert host.run("llm", "add", "lms", "-k", "lmstudio", "-u", "http://127.0.0.1:1234").returncode == 0
    assert host.run("llm", "set", "lms", "--loop-guard", "off").returncode == 0
    layer = host.run("llm", "set", "lms", "--loop-guard", "warn=2")
    assert layer.returncode == 0, layer.stderr
    assert "note: the loop guard of lms is off" in layer.stdout and "--loop-guard on" in layer.stdout, layer.stdout
    on = host.run("llm", "set", "lms", "--loop-guard", "on", "--loop-guard", "warn=5", "--loop-guard", "block=3")
    assert on.returncode == 0, on.stderr
    assert "is off" not in on.stdout
    assert "note: block (3) comes before warn (5)" in on.stdout, on.stdout
    fine = host.run("llm", "set", "lms", "--loop-guard", "warn=2")
    assert "note:" not in fine.stdout, fine.stdout


# == Journey 2: the privacy user looks at what happened - logs, activity, report ======================================

def test_logs_before_any_run_does_not_ask_for_an_option_every_start_has(host):
    """`sandbox-ai logs` in a project where nothing ran yet said "start the agent with --log-dir (the wizard's logging
    question) first" - but every start with the connection proxy (the default) keeps its log in exactly that
    directory; a user who then added --log-dir somewhere else found nothing with `sandbox-ai logs` either. It says
    that nothing has run here yet, and how to read a log kept elsewhere."""
    result = host.run("logs")
    assert result.returncode == 1
    assert "start the agent with --log-dir" not in result.stderr, result.stderr
    assert "nothing has run in" in result.stderr and str(host.workspace) in result.stderr, result.stderr
    assert "sandbox-ai logs DIR" in result.stderr


def test_the_kill_switch_with_only_a_run_name_says_the_mode_comes_last(host):
    """`sandbox-ai net cut tor-research` (the run, the mode forgotten) said "no kill switch mode tor-research: remote,
    local, api, all or off" - read as if the run's name were wrong. It says the mode comes last, with an example."""
    result = host.run("net", "cut", "tor-research")
    assert result.returncode != 0
    assert "no kill switch mode tor-research" in result.stderr
    assert "the mode comes last, e.g. net cut tor-research api" in result.stderr, result.stderr


def test_report_to_a_folder_or_into_a_missing_one_says_so_in_words(host):
    """`sandbox-ai report --all -f pdf -o ~/reports/x.pdf` with no ~/reports yet: "[Errno 2] No such file or
    directory: '.../reports/.report-ttnpo_5y'" (a temporary name the user never chose); `-o ~/ws` (a folder): "[Errno
    21] Is a directory: '.../.report-mrnf_bxc' -> '.../ws'". Both said in words, before the report is gathered."""
    missing = host.run("report", "--all", "-f", "pdf", "-o", host.directory / "reports" / "x.pdf")
    assert missing.returncode != 0
    assert "Errno" not in missing.stderr and ".report-" not in missing.stderr, missing.stderr
    assert f"the folder {host.directory / 'reports'} does not exist" in missing.stderr, missing.stderr
    folder = host.run("report", "--all", "-f", "html", "-o", host.workspace)
    assert folder.returncode != 0
    assert "Errno" not in folder.stderr and ".report-" not in folder.stderr, folder.stderr
    assert f"{host.workspace} is a folder" in folder.stderr and "report.html" in folder.stderr, folder.stderr
    assert not list(host.directory.glob(".report-*")) and not list(host.workspace.glob("*"))
    written = host.run("report", "--all", "-o", host.directory / "r.md")
    assert written.returncode == 0 and (host.directory / "r.md").exists(), written.stderr


def test_resuming_a_session_whose_agent_ends_at_once_says_so_not_failed_status_0(host):
    """A resume whose agent ended at once (its sandbox ended well, before its terminal was there) said "Session
    failed to start (status 0)" - a failure with a status that says none. It says what happened: the agent ended at
    once, the sandbox with it, and where its last words are."""
    host.add_image("localhost/claude-sandbox:latest")
    directory = session_of_0_1_94(host, "quick", ["--allow-pypi"])
    resumed = host.run("resume", "quick", env={"FAKE_PODMAN_RUN": "0"})
    assert resumed.returncode != 0
    assert "failed to start" not in resumed.stderr, resumed.stderr
    assert "ended at once" in resumed.stderr and "status 0" in resumed.stderr, resumed.stderr
    assert "fake podman: the sandbox runs" in resumed.stderr, "its last output is said"
    assert str(directory / "supervisor.log") in resumed.stderr


def test_a_start_whose_agent_exited_at_once_names_the_agents_status(tmp_path):
    """The guard's record of the end (tmux/ended) gives the agent's own status and time: said as such."""
    sessions = load("sandbox_sessions_deep2_ended", "sandbox/sessions.py")
    directory = tmp_path / "s"
    (directory / "tmux").mkdir(parents=True)
    (directory / "supervisor.log").write_text("agent-sandbox: starting the sandbox\nError: no API key\n")
    (directory / "tmux" / "ended").write_text('{"status": 3, "seconds": 1}')
    with pytest.raises(RuntimeError) as failed:
        sessions.wait_ready(directory, Mock(poll=Mock(return_value=0), returncode=0), out=lambda text: None)
    said = str(failed.value)
    assert "failed to start" not in said and "the agent exited with status 3 after 1 s" in said, said
    assert "Error: no API key" in said
    (directory / "tmux" / "ended").write_text("garbage")  # (written inside the sandbox: read as numbers only)
    with pytest.raises(RuntimeError) as failed:
        sessions.wait_ready(directory, Mock(poll=Mock(return_value=0), returncode=0), out=lambda text: None)
    assert "the agent ended at once" in str(failed.value) and "status 0" in str(failed.value)
