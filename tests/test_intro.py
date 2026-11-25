"""The agent's intro (sandbox/intro.py): what a sandbox has, told to its agent.

User (todos.txt, 2026-11-24, board): "wenn wir weitere ordner, arbeitsordner, toolchains etc.pp. dem board hinzufuegen,
sollte es per default (haken kann aber auch entfernt werden) eine info an den agenten geben, dass er zugriff auf diese
sachen hat, sowas wie ein Intro.md was on the fly geschrieben wird und der Agent per default intro liest".

A start writes /sandbox/session/INTRO.md - each folder, toolchain, MCP server, service and device with where it is in
the sandbox and how to use it, no secret, no host path - and every one of the twelve harnesses gets one line that
names it where it reads its instructions (never a file of the user's repository). On by default; --no-intro, or
"agent_intro": false in your defaults (--intro turns it on for one start). `toolchain attach`, `mcp attach --live`,
`devices add` and `workspace add` write the file again.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402
import test_toolchains as tt  # noqa: E402
from test_toolchains import data_home, running, signing_key  # noqa: E402,F401  (fixtures)
import toolchain_fakes as TF  # noqa: E402
import test_devices as td  # noqa: E402
from test_devices import world  # noqa: E402,F401  (fixture)
import test_workspaces as tw  # noqa: E402
from test_workspaces import run as workspace_run  # noqa: E402,F401  (fixture)


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_intro_test", "agent_sandbox.py")
I = s.load_intro_module()
v = s.load_vpn_module()
HARNESSES = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot",
             "letta", "pi")


@pytest.fixture(autouse=True)
def intro_as_a_user_has_it(monkeypatch):
    """sandbox-ai's own tests start with SANDBOX_AI_INTRO=0 (conftest.py: they compare each harness's configuration
    exactly); here every start is a user's: the variable is not set, the intro is on."""
    monkeypatch.delenv(s.INTRO_VARIABLE, raising=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def start(agent="claude", *options):
    a = s.parse_args([agent, "--backend", "podman", *options])
    a.run_id = f"{agent}-intro"
    return a


def session_files(a):
    """What the start writes into the session directory for the agent (the part these tests look at)."""
    files = s.note_files(a)
    if s.hermes_managed(a):
        files.update(s.hermes_files(a))
    if s.agent_managed(a):
        files.update(s.agent_files(a))
    return files


# --- the file: each resource, where it is, how to use it ----------------------------------------------------------

STATE = {"version": 1, "agent": "claude",
         "folders": [{"sandbox": "/work", "mode": "rw", "kind": "project"},
                     {"sandbox": "/workspaces/lib", "mode": "ro", "kind": "workspace"},
                     {"sandbox": "/mnt/data", "mode": "ro", "kind": "mount"}],
         "toolchains": [{"name": "go", "version": "1.27.3", "sandbox": "/opt/toolchains/go",
                         "commands": ["go", "gofmt"], "opened": ["proxy.golang.org", "--allow-github"], "closed": [],
                         "why_closed": None},
                        {"name": "rust", "version": "1.99.1", "sandbox": "/opt/toolchains/rust",
                         "commands": ["cargo", "rustc"], "opened": [], "closed": ["crates.io"],
                         "why_closed": "the security mode high"}],
         "mcp": [{"name": "fetch"}], "services": [{"name": "db", "reachable_at": ["localhost:5432"]}],
         "devices": [{"path": "/dev/ttyUSB0", "kind": "node"}], "gpu": "nvidia", "offline": False}


def test_the_file_lists_each_resource_with_where_it_is_and_how_to_use_it():
    text = I.text(STATE)
    assert text.startswith("# What this sandbox gives you (sandbox-ai)\n")
    assert "- `/work` - the project (you start here), read-write" in text
    assert "- `/workspaces/lib` - a further project folder, read-only" in text
    assert "- `/mnt/data` - an extra folder or file, read-only" in text
    assert "mounted read-only below `/opt/toolchains`; their commands are on your PATH" in text
    assert ("- go 1.27.3 at `/opt/toolchains/go`: go, gofmt. Registries open: proxy.golang.org, the preset github."
            in text)
    assert ("- rust 1.99.1 at `/opt/toolchains/rust`: cargo, rustc. Registries closed (the security mode high): "
            "crates.io - a download from there is asked about or refused" in text)
    assert "## MCP servers" in text and "\n- fetch\n" in text and "in your MCP configuration under its name" in text
    assert "- db: localhost:5432" in text and "`$SANDBOX_AI_SERVICES`" in text
    assert "- `/dev/ttyUSB0`\n- GPU: nvidia" in text
    assert "Only what sandbox-ai allows is reachable" in text
    assert len(text.splitlines()) < 45, "short: it is read at every start"


def test_a_section_without_anything_takes_no_room_and_offline_is_said():
    text = I.text({"folders": [{"sandbox": "/work", "mode": "rw", "kind": "project"}], "offline": True})
    assert [line for line in text.splitlines() if line.startswith("## ")] == ["## Folders", "## Network"]
    assert "No network at all (offline)." in text


def test_names_are_plain_text_in_the_file():
    """A name an attacker chose (a folder's, a server's) cannot start a block or hide a line of the file."""
    text = I.text({"folders": [{"sandbox": "/work/a`b\n## Network\nall open", "mode": "rw", "kind": "added"}]})
    assert "a'b?## Network?all open" in text and text.count("## Network") == 2  # (its own heading, and the name's)
    assert not any(line == "all open" for line in text.splitlines())


def test_a_starts_resources_name_sandbox_paths_only(home, tmp_path):
    work, lib, data = (tmp_path / name for name in ("project-of-mine", "lib", "private-data"))
    for folder in (work, lib, data):
        folder.mkdir()
    a = start("claude", "--workspace", f"{work},{lib}", "--workspace-ro", "--mount", f"{data}:/mnt/data:rw",
              "--mcp", "fetch", "--mcp", "fetch", "--secret-env", "API_TOKEN", "--env", "HOST_VALUE")
    a.services = [{"name": "db", "ports": [5432, 6379]}]
    state = I.resources(a, s.launcher_namespace())
    assert state["folders"] == [{"sandbox": "/work", "mode": "ro", "kind": "project"},
                                {"sandbox": "/workspaces/lib", "mode": "ro", "kind": "workspace"},
                                {"sandbox": "/mnt/data", "mode": "rw", "kind": "mount"}]
    assert state["mcp"] == [{"name": "fetch"}]
    assert state["services"] == [{"name": "db", "reachable_at": ["localhost:5432", "localhost:6379"]}]
    for shown in (json.dumps(state), I.text(state)):
        assert str(tmp_path) not in shown and "project-of-mine" not in shown and "private-data" not in shown
        assert "API_TOKEN" not in shown and "HOST_VALUE" not in shown, "no variable, no secret's name"


def test_a_starts_toolchains_say_which_registries_are_open_and_why_not(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    TF.install(tmp_path / "catalog", ["go"])
    try:
        a = start("claude", "--toolchain", "go")
        a.mode = "medium"
        s.plan_toolchains(a)
        item = I.resources(a, s.launcher_namespace())["toolchains"][0]
        assert item["sandbox"] == "/opt/toolchains/go" and item["commands"] == ["go", "gofmt"]
        assert item["opened"] == ["proxy.golang.org", "sum.golang.org", "--allow-github"] and item["closed"] == []
        strict = start("claude", "--toolchain", "go")
        strict.mode = "high"
        s.plan_toolchains(strict)
        item = I.resources(strict, s.launcher_namespace())["toolchains"][0]
        assert item["opened"] == [] and item["why_closed"] == "the security mode high"
        assert item["closed"] == ["proxy.golang.org", "sum.golang.org", "--allow-github"]
        offline = start("claude", "--toolchain", "go", "--toolchain-offline")
        offline.mode = "medium"
        s.plan_toolchains(offline)
        assert I.resources(offline, s.launcher_namespace())["toolchains"][0]["why_closed"] == \
            "started with --toolchain-offline"
    finally:
        TF.writable(tmp_path)


# --- every harness reads the line where it reads its instructions ---------------------------------------------------

def test_the_line_is_one_short_sentence_that_names_the_file():
    assert I.PATH == "/sandbox/session/INTRO.md" and I.PATH in I.LINE
    assert len(I.LINE) < 330 and "\n" not in I.LINE
    assert set(s.NOTE_PLACES) == set(HARNESSES), "every harness has a place for the start's note"


@pytest.mark.parametrize("agent", HARNESSES)
def test_every_harness_gets_the_line_where_it_reads_its_instructions(home, agent):
    a = start(agent)
    assert s.intro_on(a) and s.start_note(a) == I.LINE and s.network_note(a) is None
    files = session_files(a)
    assert files[I.FILE].startswith("# What this sandbox gives you") and files[I.NOTE_FILE] == I.LINE
    assert v.NOTE_FILE not in files, "network.md is the network note's alone"
    command = s.agent_command(a, agent)
    env = s.agent_environment(a)
    if agent == "claude":  # its managed CLAUDE.md, mounted read-only at /etc/claude-code: never the project's
        assert files["claude-code/CLAUDE.md"] == I.LINE
        assert s.hook_mount(a, Path("/s")) == (Path("/s/claude-code"), "/etc/claude-code")
    elif agent == "codex":
        assert command[command.index("developer_instructions=" + s.toml_string(I.LINE)) - 1] == "-c"
    elif agent in ("opencode", "opencode2"):  # one more instruction file: the user's AGENTS.md stays
        assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["instructions"] == [I.NOTE_PATH]
    elif agent == "qwen":
        assert command[command.index("--append-system-prompt") + 1] == I.LINE
    elif agent == "dsh":
        rows = json.loads(files["dsh/sandbox-ai.patch.yml"])
        prompt = next(row for row in rows if row.get("id") == "system-prompt")["config"]["personaPrefix"]
        assert prompt.endswith("\n\n" + I.LINE)
    elif agent == "goose":
        goose = s.load_goose_module()
        assert env["GOOSE_MOIM_MESSAGE_FILE"] == goose.LOOP_FILE
        assert files[f"goose/{goose.LOOP_FILE.rsplit('/', 1)[1]}"] == I.LINE
    elif agent == "hermes":
        hermes = s.load_hermes_module()
        assert hermes.NOTE_SKILL in json.loads(files["hermes/config.yaml"])["skills"]["auto_load"]
        assert files[f"hermes/skills/{hermes.NOTE_SKILL}/SKILL.md"].endswith(I.LINE)
    elif agent == "openhands":
        settings = json.loads(files["openhands/settings.json"])
        assert settings["agent_settings_diff"]["agent_context"]["system_message_suffix"] == I.LINE
    elif agent == "pi":
        assert json.loads(files["pi/sandbox-ai.json"])["instructions"] == I.LINE
    elif agent == "letta":  # its mod reads the file before the first message of each conversation
        assert json.loads(files["letta/sandbox-ai-start.json"])["note"] == I.NOTE_PATH
    else:  # nanobot: an always-loaded skill staged with the kits' skills (the start: the e2e test below)
        assert agent == "nanobot" and "always-loaded skill" in s.NOTE_PLACES[agent]
    assert I.NOTE_PATH in s.note_place(a) or "network.md" not in s.NOTE_PLACES[agent]


def test_nanobots_skill_holds_the_line(tmp_path):
    staging = tmp_path / "kits"
    staging.mkdir()
    s.nanobot_note_skill(staging, [], I.LINE)
    text = (staging / "skills/sandbox-ai-network/SKILL.md").read_text()
    assert "\nalways: true\n" in text and text.rstrip().endswith(I.LINE)


def test_with_a_vpn_the_network_note_comes_first_and_the_line_after_it(home):
    v.main(["add", "anon", "--tor", "--force"])
    a = start("claude", "--vpn", "anon")
    v.validate(a)
    note = v.agent_note(a)
    joined = note.rstrip("\n") + "\n\n" + I.LINE
    assert s.start_note(a) == joined
    files = session_files(a)
    assert files[v.NOTE_FILE] == note and files[I.NOTE_FILE] == joined and files["claude-code/CLAUDE.md"] == joined
    letta = start("letta", "--vpn", "anon")
    v.validate(letta)
    assert s.note_path(letta) == I.NOTE_PATH, "a harness that takes one file reads the notes joined"
    assert I.NOTE_PATH in s.note_place(letta) or "network.md" not in s.NOTE_PLACES["letta"]
    plain = start("letta", "--vpn", "anon", "--no-intro")
    v.validate(plain)
    assert s.note_path(plain) == v.NOTE_PATH and I.NOTE_FILE not in s.note_files(plain)


def test_no_intro_your_default_and_what_never_gets_one(home):
    off = start("claude", "--no-intro")
    assert not s.intro_on(off) and s.start_note(off) is None and s.note_files(off) == {} and s.note_path(off) is None
    assert s.hook_mount(off, Path("/s")) is None or s.managed_hooks(off)
    assert "developer_instructions" not in " ".join(s.agent_command(start("codex", "--no-intro"), "codex"))
    default_off = start("claude")
    default_off.intro_default = False  # "agent_intro": false in your defaults
    assert not s.intro_on(default_off)
    again = start("claude", "--intro")
    again.intro_default = False
    assert s.intro_on(again), "--intro wins over the default"
    assert start("claude", "--intro", "--no-intro").intro is False, "the last one given counts"
    probe = start("claude", "--probe")
    assert not s.intro_on(probe), "a probe starts no agent"
    own = start("claude", "--binary", "/bin/sh")
    assert not s.intro_on(own), "a program of your own is no agent to tell"


def test_the_environment_can_set_the_default_and_the_options_win(home, monkeypatch):
    """SANDBOX_AI_INTRO=0: no start of that environment tells its agent (a CI job, a script; sandbox-ai's own tests);
    =1: each does, also over a defaults file that says no. --intro and --no-intro win over both."""
    monkeypatch.setenv(s.INTRO_VARIABLE, "0")
    assert not s.intro_on(start("claude")) and s.note_files(start("claude")) == {}
    assert s.intro_on(start("claude", "--intro")), "the option wins"
    monkeypatch.setenv(s.INTRO_VARIABLE, "1")
    off_by_default = start("claude")
    off_by_default.intro_default = False
    assert s.intro_on(off_by_default), "the environment over your defaults file"
    assert not s.intro_on(start("claude", "--no-intro"))
    monkeypatch.setenv(s.INTRO_VARIABLE, "yes")  # (neither 0 nor 1: not a choice - your default counts)
    assert s.intro_on(start("claude")) and not s.intro_on(off_by_default)


def test_sandbox_ais_own_tests_start_without_it():
    """tests/conftest.py sets SANDBOX_AI_INTRO=0 for the suite (as it does for the other things a test's start must
    not do by itself): the older tests compare each harness's own configuration exactly. Kept honest here: the
    default is set there, and this file takes it away again for its own starts."""
    conftest = (ROOT / "tests" / "conftest.py").read_text()
    assert 'os.environ.setdefault("SANDBOX_AI_INTRO", "0")' in conftest
    assert s.INTRO_VARIABLE == "SANDBOX_AI_INTRO" and s.INTRO_VARIABLE not in os.environ


def test_codex_keeps_your_own_developer_instructions(tmp_path):
    """-c developer_instructions replaces config.toml's for the run: the note is joined after yours."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text('developer_instructions = "Answer in German."\nmodel = "gpt-x"\n')
    if s.load_privacy_module()._toml_module() is None:  # (Python before 3.11 without tomli: the note alone)
        assert s.codex_own_instructions(home) is None
        return
    assert s.codex_own_instructions(home) == "Answer in German."
    a = start("codex")
    a.codex_own_instructions = "Answer in German."
    command = s.agent_command(a, "codex")
    assert "developer_instructions=" + s.toml_string("Answer in German.\n\n" + I.LINE) in command
    (home / ".codex/config.toml").write_text("not = [valid")
    assert s.codex_own_instructions(home) is None
    assert s.codex_own_instructions(tmp_path / "no-home") is None


def test_agent_intro_is_a_default_of_yours(home):
    path = home / "config" / "sandbox-ai" / "defaults.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    assert "agent_intro" in s.DEFAULTS_KEYS
    path.write_text(json.dumps({"version": 1, "agent_intro": False}))
    assert s.load_user_defaults(start())["agent_intro"] is False
    path.write_text(json.dumps({"version": 1, "agent_intro": "no"}))
    with pytest.raises(ValueError, match="agent_intro is true or false"):
        s.load_user_defaults(start())


# --- kept up to date while the sandbox runs -------------------------------------------------------------------------

def with_intro(record, state=None):
    """The run RECORD as a start with an intro leaves it: intro.json in its directory, INTRO.md in its session."""
    state = state or {"version": 1, "agent": "claude",
                      "folders": [{"sandbox": "/work", "mode": "rw", "kind": "project"}], "toolchains": [], "mcp": [],
                      "services": [], "devices": [], "gpu": None, "offline": False}
    I.write_state(record["directory"], state)
    target = Path(record["session_dir"]) / I.FILE
    target.write_text(I.text(state))
    return target


def test_toolchain_attach_and_detach_write_the_intro_again(running):
    r = running
    target = with_intro(r.record)
    assert "## Toolchains" not in target.read_text()
    assert tt.t.attach(r.record, "go", **r.options) == ["go"]
    text = target.read_text()
    assert "- go 1.25.4 at `/opt/toolchains/go`: go, gofmt. Registries open: proxy.golang.org, sum.golang.org, the " \
           "preset github." in text
    assert any("the agent's intro lists what the sandbox has now: /sandbox/session/INTRO.md" in line
               for line in r.said)
    assert oct(target.stat().st_mode & 0o777) == "0o644" and not list(target.parent.glob(".INTRO*"))
    tt.t.detach(r.record, "go", out=r.said.append, runner=r.runner, finder=r.options["finder"],
                keeper=r.options["keeper"])
    assert "## Toolchains" not in target.read_text()


def test_an_attach_whose_registries_stay_closed_says_so_in_the_intro(running):
    r = running
    target = with_intro(r.record)
    r.net_main.status = 1  # the run's mode refuses the hosts (sandbox-ai net allow)
    tt.t.attach(r.record, "go", **r.options)
    assert "Registries closed (not opened when it was attached): proxy.golang.org, sum.golang.org, the preset " \
           "github" in target.read_text()


def test_a_run_without_an_intro_gets_none_from_an_attach(running):
    r = running
    assert tt.t.attach(r.record, "go", **r.options) == ["go"]
    assert not (Path(r.record["session_dir"]) / I.FILE).exists()
    assert not any("intro" in line for line in r.said)
    assert I.refresh({"directory": "/nonexistent", "session_dir": "/nonexistent"}) is False
    assert I.refresh({}) is False, "never an exception: an attach that worked is not failed by its note"


def test_devices_folders_and_mcp_servers_added_while_it_runs_are_listed(tmp_path):
    run, session = tmp_path / "run", tmp_path / "session"
    run.mkdir()
    session.mkdir()
    record = {"run": "claude-1", "directory": str(run), "session_dir": str(session)}
    target = with_intro(record, {**STATE, "toolchains": [], "mcp": [{"name": "fetch"}],
                                 "devices": [{"path": "/dev/kvm", "kind": "node"}], "gpu": None})
    devices, workspaces = I.module("devices"), I.module("workspaces")
    devices.save(record, [
        {"id": "1", "value": "/dev/kvm", "source": "start", "state": "attached", "nodes": {"/dev/kvm": [10, 232]}},
        {"id": "2", "value": "/dev/ttyUSB0", "source": "added", "state": "attached",
         "nodes": {"/dev/ttyUSB0": [188, 0]}},
        {"id": "3", "value": "/dev/ttyUSB1", "source": "added", "state": "removed",
         "nodes": {"/dev/ttyUSB1": [188, 1]}}])
    workspaces.save(record, [
        {"host": str(tmp_path / "host-side"), "sandbox": "/workspaces/extra", "mode": "ro", "state": "attached"},
        {"host": str(tmp_path / "gone"), "sandbox": "/workspaces/gone", "mode": "rw", "state": "removed"}])
    (run / "run.json").write_text(json.dumps({"run": "claude-1", "mcp_servers": ["fetch", "github"]}))
    assert I.refresh(record) is True
    text = target.read_text()
    assert "- `/dev/kvm`\n- `/dev/ttyUSB0`\n" in text and "ttyUSB1" not in text
    assert "- `/workspaces/extra` - added while you run, read-only" in text and "/workspaces/gone" not in text
    assert "\n- fetch\n- github\n" in text
    assert "host-side" not in text and str(tmp_path) not in text, "never a host path"
    assert "- `/workspaces/lib` - a further project folder, read-only" in text, "what the start gave stays"
    assert I.refresh(record) is False, "nothing changed: not written again"
    (run / "run.json").write_text(json.dumps({"run": "claude-1", "mcp_servers": ["github"]}))  # mcp detach --live
    assert I.refresh(record) is True and "\n- fetch\n" not in target.read_text()


def test_devices_add_and_remove_write_the_intro_again(world, monkeypatch):
    proc, record = world
    target = with_intro(record)
    monkeypatch.setattr(td.d, "checked_device", td.plain_device)
    monkeypatch.setattr(td.d.module("agent_sandbox"), "load_policy", lambda path=None: {})
    lines = []
    finder = lambda r: td.d.sandbox_processes(r, proc)  # noqa: E731
    runner = lambda op, found, path: (0, "")  # noqa: E731
    td.d.add(record, "/dev/ttyUSB0", out=lines.append, runner=runner, finder=finder)
    assert "## Devices" in target.read_text() and "- `/dev/ttyUSB0`" in target.read_text()
    assert any("the agent's intro lists what the sandbox has now" in line for line in lines)
    td.d.remove(record, "/dev/ttyUSB0", out=lines.append, runner=runner, finder=finder)
    assert "## Devices" not in target.read_text()


def test_workspace_add_and_remove_write_the_intro_again(workspace_run, monkeypatch):
    record, tmp = workspace_run
    monkeypatch.setattr(tw.w, "watch_file_log", lambda record, path, out=print: False)
    monkeypatch.setattr(tw.w, "copies", lambda found, path, proc=None, group=None: [])
    target = with_intro(record)
    lines = []
    runner = lambda op, found, name, *rest, **kw: (0, "")  # noqa: E731
    path = tw.make(tmp / "elsewhere" / "reference-material")
    sandbox = tw.w.add(record, path, read_only=True, out=lines.append, runner=runner, finder=tw.fake_found,
                       remember=lambda trees: None)
    text = target.read_text()
    assert f"- `{sandbox}` - added while you run, read-only" in text and "elsewhere" not in text
    assert any("the agent's intro lists what the sandbox has now" in line for line in lines)
    tw.w.remove(record, sandbox, out=lines.append, runner=runner, finder=tw.fake_found,
                using=lambda found, path, proc=None: [])
    assert sandbox not in target.read_text()


def test_mcp_attach_and_detach_live_write_the_intro_again(tmp_path):
    """`mcp attach NAME START --live` and `mcp detach --live` end with mcp.intro_refreshed(record, out): the servers
    are run.json's, which they change first (record_server)."""
    mcp = load("mcp_intro_test", "mcp.py")
    run, session = tmp_path / "run", tmp_path / "session"
    run.mkdir()
    session.mkdir()
    record = {"run": "claude-1", "directory": str(run), "session_dir": str(session)}
    target = with_intro(record)
    (run / "run.json").write_text(json.dumps({"run": "claude-1", "mcp_servers": []}))
    said = []
    mcp.record_server(record, "fetch")  # (as attach_live does, under the run's lock)
    assert mcp.intro_refreshed(record, said.append) is True
    assert "## MCP servers" in target.read_text() and "\n- fetch\n" in target.read_text()
    assert said == ["  the agent's intro lists what the sandbox has now: /sandbox/session/INTRO.md"]
    mcp.record_server(record, "fetch", add=False)  # (as detach --live does)
    assert mcp.intro_refreshed(record, said.append) is True and "## MCP servers" not in target.read_text()
    assert mcp.intro_refreshed({"directory": str(tmp_path / "none")}, said.append) is False
    source = (ROOT / "sandbox" / "mcp.py").read_text()
    attach = source[source.index("def attach_live("):source.index("def detach(")]
    detach = source[source.index("def detach("):source.index("class LauncherVersion")]
    assert "intro_refreshed(record, out)" in attach and "intro_refreshed(record, out)" in detach, \
        "both commands call it (they need a running sandbox: the live tests run them whole)"


# --- end to end: the real ./sandbox-ai ------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path / "host")
    made.env.pop(s.INTRO_VARIABLE, None)  # (a user's environment: see intro_as_a_user_has_it)
    return made


def plan_of(done):
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout[done.stdout.index("{"):])


@pytest.mark.parametrize("agent", HARNESSES)
def test_e2e_the_dry_run_shows_the_intro_for_every_harness(host, agent):
    intro = plan_of(host.run(agent, "--dry-run"))["intro"]
    assert intro["file"] == "/sandbox/session/INTRO.md" and intro["line"] == I.LINE and intro["agent_reads_it"]
    assert "- `/work` - the project (you start here), read-write" in intro["text"]
    assert str(host.directory) not in json.dumps(intro), "no host path"
    assert plan_of(host.run(agent, "--dry-run", "--no-intro"))["intro"] is None


def test_e2e_the_environments_default_and_the_option_over_it(host):
    off = {"SANDBOX_AI_INTRO": "0"}
    assert plan_of(host.run("claude", "--dry-run", env=off))["intro"] is None
    assert plan_of(host.run("claude", "--dry-run", "--intro", env=off))["intro"]["file"] == I.PATH
    assert host.run("defaults", "set", "agent_intro", "false").returncode == 0
    assert plan_of(host.run("claude", "--dry-run", env={"SANDBOX_AI_INTRO": "1"}))["intro"]["file"] == I.PATH
    assert plan_of(host.run("claude", "--dry-run", "--no-intro", env={"SANDBOX_AI_INTRO": "1"}))["intro"] is None


def test_e2e_your_default_turns_it_off_and_intro_turns_it_on_for_a_start(host):
    assert host.run("defaults", "set", "agent_intro", "false").returncode == 0
    assert plan_of(host.run("claude", "--dry-run"))["intro"] is None
    assert plan_of(host.run("claude", "--dry-run", "--intro"))["intro"]["file"] == I.PATH
    refused = host.run("defaults", "set", "agent_intro", "sometimes")
    assert refused.returncode != 0 and "agent_intro is true or false" in refused.stderr
    assert host.run("defaults", "unset", "agent_intro").returncode == 0
    assert plan_of(host.run("claude", "--dry-run"))["intro"] is not None


def kept_start(host, tmp_path, agent, *options):
    host.add_image(f"localhost/{agent}-sandbox:latest")
    kept = tmp_path / f"kept-{agent}"
    done = host.run(agent, "--no-tmux", "--no-auth", *options,
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
    assert done.returncode == 0, done.stderr
    return kept / "session"


def test_e2e_a_claude_start_writes_the_intro_and_its_managed_claude_md(host, tmp_path):
    data = tmp_path / "customer-data"
    data.mkdir()
    session = kept_start(host, tmp_path, "claude", "--mount", f"{data}:/mnt/data:ro")
    text = (session / "INTRO.md").read_text()
    assert "- `/work` - the project (you start here), read-write" in text
    assert "- `/mnt/data` - an extra folder or file, read-only" in text
    assert "customer-data" not in text and str(tmp_path) not in text
    assert (session / "claude-code" / "CLAUDE.md").read_text() == I.LINE
    runs = [call for call in host.calls() if call[:1] == ["run"] and "--pod" in call]
    assert any(",dst=/etc/claude-code,ro," in arg for call in runs for arg in call)
    assert not (host.workspace / "CLAUDE.md").exists() and not (host.workspace / "AGENTS.md").exists(), \
        "nothing is written into the project"
    quiet = kept_start(host, tmp_path / "quiet", "claude", "--no-intro")
    assert not (quiet / "INTRO.md").exists() and not (quiet / "claude-code" / "CLAUDE.md").exists()


def test_e2e_nanobot_letta_and_opencode2_read_it_in_their_place(host, tmp_path):
    session = kept_start(host, tmp_path, "nanobot")
    skill = (session / "kits/skills/sandbox-ai-network/SKILL.md").read_text()
    assert "\nalways: true\n" in skill and skill.rstrip().endswith(I.LINE)
    session = kept_start(host, tmp_path, "letta")
    assert json.loads((session / "letta/sandbox-ai-start.json").read_text())["note"] == I.NOTE_PATH
    assert (session / I.NOTE_FILE).read_text() == I.LINE and (session / "INTRO.md").is_file()
    session = kept_start(host, tmp_path, "opencode2")
    content = json.loads(json.loads((session / "config.json").read_text())["env"]["OPENCODE_CONFIG_CONTENT"])
    assert content["instructions"] == [I.NOTE_PATH]


def test_e2e_help_and_completion_know_the_options(host):
    done = host.run("claude", "--help")
    assert done.returncode == 0 and "--no-intro" in done.stdout and "--intro" in done.stdout
    assert "/sandbox/session/INTRO.md" in " ".join(done.stdout.split())
