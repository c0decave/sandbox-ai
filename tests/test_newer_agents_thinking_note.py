"""The agents of 0.1.97 (OpenCode 2, Pi, OpenHands, nanobot, Letta Code; Qwen Code again): --show-thinking through
each one's real switch, the run's network note in each one's real instruction place, their harness keys in the
model x harness profiles.

User: "--show-thinking for the five newest agents ... find each harness's REAL switch ... where a harness truly has
no switch, keep it refused with the reason", "OpenCode 2, nanobot and Letta Code only get the network note through
/sandbox/session/network.md that their skill names; map each harness's real instruction place", "an unknown agent
never crashes".

What the pinned releases have (read in their sources, agent_sandbox.AGENT_THINKING and THINKING_NONE):
  OpenCode 2.2.5  cli.json session.thinking show|hide (TUI, default hide) and mini.thinking (default show)
  Pi 1.4.0        settings.json hideThinkingBlock (default false: shown)
  nanobot 0.4.0   channels.showReasoning (default true; the websocket channel's own key wins for its clients)
  OpenHands       Agent Canvas 1.34.0: every message's thinking folded, a click unfolds it - no setting
  Letta Code      0.34.36: folded, Ctrl+T unfolds it in the session - no setting
  Qwen Code       0.26.4: folded, Ctrl+O unfolds it - no setting (unchanged)
And where each reads instructions: OpenCode 2 the instructions of every configuration layer (a Set over them, so
yours stay), nanobot an always-loaded skill of its workspace (its system prompt every turn), Letta Code a mod of its
global mods folder (turn_start: a system reminder before the first user message of each conversation).

Unit tests of each mapping, the start script and the mod (run by node where it is there), then the real ./sandbox-ai
(tests/live_fakes.py: only Podman stood in for)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import home_guard  # noqa: E402

home_guard.ensure()
from live_fakes import Host  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("sandbox_newer_thinking_note", "sandbox/agent_sandbox.py")
letta = load("sandbox_letta_newer_thinking_note", "sandbox/letta.py")
nanobot = load("sandbox_nanobot_newer_thinking_note", "sandbox/nanobot.py")
opencode2 = load("sandbox_opencode2_newer_thinking_note", "sandbox/opencode2.py")
mp = load("sandbox_profiles_newer_thinking_note", "sandbox/model_profiles.py")
v = s.load_vpn_module()

NEWER = ("opencode2", "pi", "openhands", "nanobot", "letta")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "cfg"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")


def parsed(agent, *options):
    return s.parse_args([agent, "--workspace", "/tmp", *options])


# --- --show-thinking: each one's real switch --------------------------------------------------------------------------

def test_the_newer_agents_have_their_switch_or_the_reason_why_not():
    assert {agent: s.AGENT_THINKING[agent][0] for agent in ("opencode2", "pi", "nanobot")} == {
        "opencode2": "off", "pi": "on", "nanobot": "on"}
    assert set(s.THINKING_NONE) == {"qwen", "openhands", "letta"}
    for agent, words in (("qwen", "Ctrl+O"), ("openhands", "a click unfolds it"), ("letta", "Ctrl+T")):
        assert "has no setting for it" in s.THINKING_NONE[agent] and words in s.THINKING_NONE[agent]
        assert "yet" not in s.THINKING_NONE[agent]  # checked in the source, not "not mapped yet"
        with pytest.raises(ValueError, match="no setting for it"):
            s.validate(parsed(agent, "--show-thinking", "on"))


@pytest.mark.parametrize("value, mode", [("on", "show"), ("off", "hide")])
def test_opencode2_gets_session_and_mini_thinking_in_its_cli_json(tmp_path, value, mode):
    home = tmp_path / "home"
    (home / ".config/opencode").mkdir(parents=True)
    cli = home / ".config/opencode/cli.json"
    cli.write_text(json.dumps({"$schema": "x", "theme": {"name": "nord"}, "session": {"sidebar": "auto"}}))
    s.home_thinking(home, "opencode2", value == "on")
    assert json.loads(cli.read_text()) == {"$schema": "x", "theme": {"name": "nord"},
                                           "session": {"sidebar": "auto", "thinking": mode},
                                           "mini": {"thinking": mode}}


@pytest.mark.parametrize("value, hidden", [("on", False), ("off", True)])
def test_pi_gets_hide_thinking_block_in_its_settings(tmp_path, value, hidden):
    home = tmp_path / "home"
    (home / ".pi/agent").mkdir(parents=True)
    settings = home / ".pi/agent/settings.json"
    settings.write_text(json.dumps({"httpIdleTimeoutMs": 3600000, "hideThinkingBlock": "x"}))
    s.home_thinking(home, "pi", value == "on")
    assert json.loads(settings.read_text()) == {"httpIdleTimeoutMs": 3600000, "hideThinkingBlock": hidden}


def test_a_settings_file_that_cannot_be_read_is_left_alone_and_said(tmp_path, capsys):
    home = tmp_path / "home"
    (home / ".pi/agent").mkdir(parents=True)
    (home / ".pi/agent/settings.json").write_text("{ not json")
    s.home_thinking(home, "pi", True)
    assert (home / ".pi/agent/settings.json").read_text() == "{ not json"
    assert "left sandbox ~/.pi/agent/settings.json unchanged" in capsys.readouterr().err


def test_without_the_option_the_agents_own_default_holds():
    for agent in ("opencode2", "pi", "nanobot"):
        assert s.thinking_shown(parsed(agent)) is None and s.thinking_args(parsed(agent)) == []


@pytest.mark.parametrize("value, shown", [("on", True), ("off", False)])
def test_nanobot_gets_show_reasoning_for_every_channel(value, shown):
    config = nanobot.managed_config(parsed("nanobot", "--show-thinking", value))
    assert config["channels"]["showReasoning"] is shown
    assert config["channels"]["websocket"] == {"host": nanobot.LOOPBACK_HOST, "websocketRequiresToken": True,
                                               "showReasoning": shown}, "the websocket channel's own key wins there"
    assert "showReasoning" not in nanobot.managed_config(parsed("nanobot"))["channels"]
    helper = parsed("nanobot", "--show-thinking", value)
    helper.helper_of = "lead"
    assert "showReasoning" not in nanobot.managed_config(helper)["channels"], "a helper keeps its own default"


# --- the network note: each one's real instruction place --------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(tmp_path / sub))
    return tmp_path


def tor_start(agent):
    v.main(["add", "anon", "--tor", "--force"])
    a = s.parse_args([agent, "--backend", "podman", "--vpn", "anon"])
    v.validate(a)
    a.run_id = f"{agent}-note"
    return a


def test_opencode2_reads_the_note_as_one_more_instruction_file(home):
    a = tor_start("opencode2")
    content = json.loads(s.agent_environment(a)["OPENCODE_CONFIG_CONTENT"])
    assert content["instructions"] == [v.NOTE_PATH]
    assert "OPENCODE_CONFIG_CONTENT" in s.NOTE_PLACES["opencode2"]
    assert "instructions" not in opencode2.config_content(s.parse_args(["opencode2"]))


def test_nanobot_gets_an_always_loaded_skill_beside_the_kits_skills(tmp_path):
    staging = tmp_path / "kits"
    staging.mkdir()
    staged = s.nanobot_note_skill(staging, [], "## Network of this run\n- Tor")
    assert staged == [(staging / "skills", "/root/.nanobot/workspace/skills")]
    text = (staging / "skills/sandbox-ai-network/SKILL.md").read_text()
    front, _, body = text[4:].partition("\n---\n")
    assert text.startswith("---\n") and "\nalways: true" in "\n" + front and "name: sandbox-ai-network" in front
    assert body.strip() == "## Network of this run\n- Tor"
    # The kits' skills already staged: the same mount, the note's skill beside theirs (a planted one replaced).
    again = tmp_path / "again"
    (again / "skills/mine").mkdir(parents=True)
    (again / "skills/sandbox-ai-network").mkdir()
    (again / "skills/sandbox-ai-network/old").write_text("x")
    mounts = [(again / "skills", "/root/.nanobot/workspace/skills")]
    assert s.nanobot_note_skill(again, mounts, "note") == mounts
    assert sorted(p.name for p in (again / "skills/sandbox-ai-network").iterdir()) == ["SKILL.md"]
    assert (again / "skills/mine").is_dir()


def test_letta_gets_the_note_file_for_its_mod(home):
    a = tor_start("letta")
    config = json.loads(s.agent_files(a)["letta/sandbox-ai-start.json"])
    assert config["note"] == v.NOTE_PATH
    assert json.loads(s.agent_files(s.parse_args(["letta"]))["letta/sandbox-ai-start.json"])["note"] is None


@pytest.fixture
def start(tmp_path):
    """Letta Code's start script with its configuration at TMP/start.json and a letta that records its arguments."""
    script = tmp_path / "start.py"
    script.write_text(letta.START_SCRIPT.replace(letta.START_CONFIG, str(tmp_path / "start.json")))
    fake = tmp_path / "letta"
    fake.write_text(f"#!{sys.executable}\nimport sys\nprint(sys.argv[1:])\n")
    fake.chmod(0o755)
    home = tmp_path / "home"

    def run(config, env=None):
        (tmp_path / "start.json").write_text(json.dumps(config))
        result = subprocess.run([sys.executable, "-I", str(script), str(fake), "--new"], capture_output=True,
                                text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin", **(env or {})},
                                timeout=60)
        assert result.returncode == 0 and "['--new']" in result.stdout, result.stderr
        return result
    return run


def test_the_start_script_writes_the_mod_and_takes_only_its_own_out(start, tmp_path):
    mod = tmp_path / "home/.letta/mods" / letta.NOTE_MOD_FILE
    start({"servers": [], "note": "/sandbox/session/network.md"})
    text = mod.read_text()
    assert text.startswith(letta.NOTE_MARK) and 'const NOTE = "/sandbox/session/network.md";' in text
    assert 'letta.events.on("turn_start"' in text and "@NOTE@" not in text
    start({"servers": [], "note": None})
    assert not mod.exists(), "a run without a note takes the mod out"
    mod.write_text("// a mod of the user's own with that name\n")
    start({"servers": [], "note": None})
    assert mod.read_text() == "// a mod of the user's own with that name\n", "never someone else's file"


def test_the_start_script_honours_letta_mods_dir_and_never_follows_a_symlink(start, tmp_path):
    folder = tmp_path / "my-mods"
    start({"servers": [], "note": "/n.md"}, env={"LETTA_MODS_DIR": str(folder)})
    assert (folder / letta.NOTE_MOD_FILE).read_text().startswith(letta.NOTE_MARK)
    elsewhere = tmp_path / "elsewhere.mjs"
    elsewhere.write_text("x")
    (tmp_path / "home/.letta/mods").mkdir(parents=True)
    os.symlink(elsewhere, tmp_path / "home/.letta/mods" / letta.NOTE_MOD_FILE)
    start({"servers": [], "note": "/n.md"})  # Letta Code starts all the same
    assert elsewhere.read_text() == "x"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_mod_puts_the_note_before_the_first_message_of_each_conversation(tmp_path):
    """The mod run by node with the API Letta Code 0.34.36 gives a mod (events.on returns its disposer; turn_start's
    event.input, MessageCreate and ApprovalCreate items)."""
    note = tmp_path / "network.md"
    note.write_text("## Network of this run (sandbox-ai): Tor\n")
    ns = {}
    exec(compile(letta.START_SCRIPT.replace('if __name__ == "__main__":', "if False:"), "start", "exec"), ns)
    mod = tmp_path / "mod.mjs"
    mod.write_text(ns["NOTE_MOD"].replace("@NOTE@", json.dumps(str(note))))
    driver = tmp_path / "driver.mjs"
    driver.write_text("""
const mod = await import(process.argv[2]);
const handlers = [];
const dispose = mod.default({capabilities: {events: {turns: true}},
  events: {on: (name, handler) => { handlers.push([name, handler]); return () => handlers.splice(0); }}});
const turn = (conversationId, input) => {
  const event = {agentId: "agent-1", conversationId, input};
  for (const [name, handler] of handlers) if (name === "turn_start") handler(event);
  return event.input;
};
const first = turn("c1", [{type: "message", role: "user", content: "hello"}]);
const second = turn("c1", [{type: "message", role: "user", content: "again"}]);
const parts = turn("c2", [{type: "approval"}, {role: "user", content: [{type: "text", text: "hi"}]}]);
console.log(JSON.stringify({first, second, parts, dispose: typeof dispose}));
""")
    done = subprocess.run(["node", str(driver), str(mod)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    reminder = "<system-reminder>\n## Network of this run (sandbox-ai): Tor\n</system-reminder>\n\n"
    assert seen["first"] == [{"type": "message", "role": "user", "content": reminder + "hello"}]
    assert seen["second"] == [{"type": "message", "role": "user", "content": "again"}], "once per conversation"
    assert seen["parts"] == [{"type": "approval"},
                             {"role": "user", "content": [{"type": "text", "text": reminder},
                                                          {"type": "text", "text": "hi"}]}]
    assert seen["dispose"] == "function"


def test_every_agent_names_where_it_reads_the_note():
    for agent in NEWER:
        assert "named by its sandbox-ai skill" not in s.NOTE_PLACES[agent]


# --- model x harness profiles: the newer harness keys ---------------------------------------------------------------

TABLE = {"schema": 1, "source": "test", "families": [{
    "id": "fam", "label": "Fam", "pattern": "fam-model", "examples": ["fam-model"],
    "all": {"params": {"temperature": 0.5}},
    "harnesses": {agent: {"status": "verified", "params": {"top_p": 0.9},
                          "evidence": {"derived": "scripts/profile_status.py", "rule": "test", "runs": 10,
                                       "run_ids": f"round 8: TASK__{agent}__fam__BLOCK"}}
                  for agent in ("opencode2", "pi", "openhands", "nanobot", "letta")}}]}


@pytest.mark.parametrize("agent", ["opencode2", "pi", "openhands", "nanobot", "letta"])
def test_the_profiles_know_the_newer_harness_keys(agent):
    assert agent in mp.HARNESSES
    mp.check_table(TABLE)  # a table with entries for them is valid
    profile = mp.match("fam-model", agent, TABLE)
    assert profile["pair"] and profile["status"] == "verified" and profile["params"] == {"temperature": 0.5,
                                                                                       "top_p": 0.9}
    shipped = mp.load()
    assert all(mp.match(example, agent, shipped) is not None
               for family in shipped["families"] for example in family["examples"])


@pytest.mark.parametrize("agent", ["nope", "", None, "OpenCode2"])
def test_an_unknown_agent_never_crashes_the_matching(agent):
    assert mp.match("fam-model", agent, TABLE) is None
    assert mp.match("qwen/qwen3-coder-next", agent, mp.load()) is None


# --- end to end: the real ./sandbox-ai ------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    return Host(tmp_path / "host")


@pytest.mark.parametrize("agent, default", [("opencode2", "off"), ("pi", "on"), ("nanobot", "on")])
def test_e2e_the_plan_shows_their_switch(host, agent, default):
    done = host.run(agent, "--show-thinking", "on", "--dry-run")
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout[done.stdout.index("{"):])["show_thinking"]
    assert plan["value"] == "on" and plan["agent_default"] == default and plan["switch"] and plan["none"] is None


@pytest.mark.parametrize("agent, words", [("openhands", "a click unfolds it"), ("letta", "Ctrl+T"),
                                          ("qwen", "Ctrl+O")])
def test_e2e_the_ones_without_a_switch_are_refused_with_the_reason(host, agent, words):
    done = host.run(agent, "--show-thinking", "off", "--dry-run")
    assert done.returncode != 0 and words in done.stderr and "no setting for it" in done.stderr, done.stderr


def kept_start(host, tmp_path, agent, *options, env=None):
    host.add_image(f"localhost/{agent}-sandbox:latest", "localhost/sandbox-ai-tor:latest")
    kept = tmp_path / f"kept-{agent}"
    done = host.run(agent, "--no-tmux", "--no-auth", *options,
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept), **(env or {})})
    assert done.returncode == 0, done.stderr
    return kept / "session"


def test_e2e_opencode2_and_pi_starts_write_their_switch_into_their_home(host, tmp_path):
    home = tmp_path / "oc2-home"
    kept_start(host, tmp_path, "opencode2", "--state-dir", str(home), "--show-thinking", "on")
    cli = json.loads((home / ".config/opencode/cli.json").read_text())
    assert cli["session"]["thinking"] == "show" and cli["mini"]["thinking"] == "show"
    home = tmp_path / "pi-home"
    kept_start(host, tmp_path, "pi", "--state-dir", str(home), "--show-thinking", "off")
    assert json.loads((home / ".pi/agent/settings.json").read_text())["hideThinkingBlock"] is True


def test_e2e_a_nanobot_start_carries_show_reasoning(host, tmp_path):
    session = kept_start(host, tmp_path, "nanobot", "--show-thinking", "off")
    settings = json.loads((session / "nanobot/sandbox-ai.json").read_text())
    assert settings["channels"]["showReasoning"] is False
    assert settings["channels"]["websocket"]["showReasoning"] is False


TOR = {"FAKE_TOR_SCHEDULE": json.dumps([[[0, 100, "Done"]]])}


@pytest.fixture
def tor_host(host):
    host.add_image("localhost/sandbox-ai-tor:latest")
    added = host.run("vpn", "add", "tor-test", "--tor")
    assert added.returncode == 0, added.stderr
    return host


def test_e2e_nanobot_through_tor_gets_the_note_as_an_always_loaded_skill(tor_host, tmp_path):
    session = kept_start(tor_host, tmp_path, "nanobot", "--vpn", "tor-test", env=TOR)
    note = (session / "network.md").read_text()
    skill = (session / "kits/skills/sandbox-ai-network/SKILL.md").read_text()
    assert "\nalways: true\n" in skill and skill.rstrip().endswith(note.strip())
    runs = [call for call in tor_host.calls() if call[:1] == ["run"] and "--pod" in call]
    assert any(",dst=/root/.nanobot/workspace/skills,ro" in arg for call in runs for arg in call)


def test_e2e_letta_and_opencode2_through_tor_get_the_note_in_their_place(tor_host, tmp_path):
    session = kept_start(tor_host, tmp_path, "letta", "--vpn", "tor-test", env=TOR)
    assert json.loads((session / "letta/sandbox-ai-start.json").read_text())["note"] == "/sandbox/session/network.md"
    session = kept_start(tor_host, tmp_path, "opencode2", "--vpn", "tor-test", env=TOR)
    content = json.loads(json.loads((session / "config.json").read_text())["env"]["OPENCODE_CONFIG_CONTENT"])
    assert content["instructions"] == ["/sandbox/session/network.md"]
