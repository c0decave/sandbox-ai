"""Regression from live use (2026-11-15): how is Tor used in a Tor run, and does the agent know it from the start?

User: "Bitte auch prüfen, WIE Tor per Default in unseren Tor-Setups genutzt werden kann. Gibt es eine direkte Route
oder immer nur 127.0.0.1:9050? Ich denke, wenn Tor aktiviert ist, sollte in den Default-Prompt schon mal rein, wie
Tor genutzt werden kann."

How it is (vpn_runtime.tor_rules, vpn.validate_tor, the torrc): transparent. Every TCP connection of the run - the
agent's, its tools', its MCP servers', the session browser's - is handed to tor's TransPort (after the connection
proxy, whose allow-list, approvals and log still apply); every lookup on port 53 to tor's DNSPort, `.onion` names map
into 10.192.0.0/10 (every one with --dns, else those on the allow-list); other UDP and IPv6 are dropped. Plain curl,
git and pip just work, through Tor, with no proxy settings. 127.0.0.1:9050 (the SocksPort) exists only when the
profile has --socks, for programs that want SOCKS, and passes the connection proxy by.

Before, an agent learned this only if it read the sandbox-ai skill. Now every agent (claude, codex, opencode, hermes,
qwen, dsh, goose) gets a short note with THIS run's facts from its start - in the place each one reads its system
instructions - and the dry-run plan shows it. Non-Tor VPNs get a two-line note.
"""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_tor_note_test", "agent_sandbox.py")
v = s.load_vpn_module()

AGENTS = ("claude", "codex", "opencode", "hermes", "qwen", "dsh", "goose")
# The agents of 0.1.97 (merged after this test): OpenHands and Pi read it where they read the loop guard's text,
# OpenCode 2 as one more instruction file, nanobot as an always-loaded skill (staged with the kits' skills: the start,
# below and in test_newer_agents_thinking_note.py), Letta Code through a mod its start script writes.
NEWER = ("opencode2", "openhands", "nanobot", "letta", "pi")


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def tor_start(agent="codex", *extra, socks=False):
    """A Tor start whose note is the network note alone (--no-intro: the agent's intro joins the note by default -
    tests/test_intro.py has it, alone and joined after this one)."""
    v.main(["add", "anon", "--tor", "--force", *(["--socks"] if socks else [])])
    a = s.parse_args([agent, "--backend", "podman", "--vpn", "anon", "--no-intro", *extra])
    v.validate(a)
    return a


# --- the note: this run's facts -----------------------------------------------------------------------------------

def test_a_plain_tor_run_is_transparent_and_has_no_socks_port(home):
    note = v.agent_note(tor_start())
    lines = note.splitlines()
    assert lines[0] == "## Network of this run (sandbox-ai): Tor (profile anon)" and len(lines) <= 12
    assert "Every TCP connection here goes through Tor by itself" in note
    assert "no proxy settings or torsocks needed" in note and "allow-list, approvals and the connection log" in note
    assert '"IsTor": true' in note and "ends the run" in note
    assert "only those on the allow-list (no --dns at the start)" in note
    assert "No UDP but DNS, no IPv6: no ping" in note
    assert "No SocksPort in this run (127.0.0.1:9050 is closed)" in note
    assert "socks5h://" not in note and "Past Tor" not in note
    assert lines[-1] == "- Nothing leaves past Tor (kill switch). More: the sandbox-ai skill."


def test_the_note_says_this_runs_socks_port_onion_lookups_bypass_and_check(home):
    a = tor_start("codex", "--dns", "--vpn-bypass-tcp", "192.168.1.20:11434", "--vpn-check", "warn",
                  "--allow-host-port", "3000", "--vpn-bypass-host-ports", socks=True)
    note = v.agent_note(a)
    assert len(note.splitlines()) <= 12
    assert "socks5h://127.0.0.1:9050, only for programs that want SOCKS" in note
    assert "passes the connection proxy by (no allow-list, approvals or log)" in note
    assert ".onion names resolve (to 10.192.0.0/10" in note and "every one (--dns)" in note
    assert "only TCP to 192.168.1.20:11434 (--vpn-bypass-tcp" in note
    assert "The host's loopback port 3000 lies past Tor" in note
    assert "is reported to the human (--vpn-check warn)" in note
    assert note.splitlines()[-1].startswith("- Nothing else leaves past Tor (kill switch).")


def test_no_vpn_no_note_and_another_vpn_two_lines(home):
    assert v.agent_note(s.parse_args(["codex", "--backend", "podman"])) is None
    a = s.parse_args(["codex", "--backend", "podman"])
    a.vpn, a.vpn_profile = "work", {"name": "work", "type": "wireguard"}
    note = v.agent_note(a)
    assert note.splitlines() == [
        "## Network of this run (sandbox-ai): the VPN work (wireguard)",
        "- everything leaves through its tunnel, a kill switch blocks the rest (IPv6 only if the VPN carries it), and "
        "lookups go to the VPN's resolvers; the allow-list and approvals apply as always. More: the sandbox-ai skill."]
    a.vpn_profile = {"name": "work", "type": "tailscale", "tailscale": {"exit_node": None}}
    assert "only its tailnet goes through the tunnel" in v.agent_note(a)


# --- every agent sees it from its start ---------------------------------------------------------------------------

def session_files(a):
    """What the start writes into the session directory for the agent (the part these tests look at)."""
    files = s.note_files(a)
    if s.hermes_managed(a):
        files.update(s.hermes_files(a))
    if s.agent_managed(a):
        files.update(s.agent_files(a))
    return files


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_gets_the_note_where_it_reads_its_instructions(home, agent):
    a = tor_start(agent)
    a.run_id = f"{agent}-r1"
    note = v.agent_note(a)
    files = session_files(a)
    assert files[v.NOTE_FILE] == note  # /sandbox/session/network.md, for the skill and every agent
    command = s.agent_command(a, agent)
    env = s.agent_environment(a)
    if agent == "claude":  # Claude Code's managed CLAUDE.md, mounted read-only at /etc/claude-code
        assert files["claude-code/CLAUDE.md"] == note
        assert s.hook_mount(a, Path("/s")) == (Path("/s/claude-code"), "/etc/claude-code")
    elif agent == "codex":  # a developer message of its own
        assert command[command.index("developer_instructions=" + s.toml_string(note)) - 1] == "-c"
    elif agent == "opencode":  # one of OpenCode's instruction files (its own AGENTS.md stays)
        assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["instructions"] == [v.NOTE_PATH]
    elif agent == "qwen":
        assert command[command.index("--append-system-prompt") + 1] == note
    elif agent == "dsh":
        rows = json.loads(files["dsh/sandbox-ai.patch.yml"])
        prompt = next(row for row in rows if row.get("id") == "system-prompt")["config"]["personaPrefix"]
        assert prompt.endswith("\n\n" + note) and prompt.startswith("You are a coding agent")
    elif agent == "goose":  # Top Of Mind: every turn
        goose = s.load_goose_module()
        assert env["GOOSE_MOIM_MESSAGE_FILE"] == goose.LOOP_FILE
        assert files[f"goose/{goose.LOOP_FILE.rsplit('/', 1)[1]}"] == note
    else:  # Hermes: an auto-loaded skill of sandbox-ai's own
        hermes = s.load_hermes_module()
        config = json.loads(files["hermes/config.yaml"])
        assert hermes.NOTE_SKILL in config["skills"]["auto_load"]
        assert hermes.SKILLS_DIR in config["skills"]["external_dirs"]
        assert files[f"hermes/skills/{hermes.NOTE_SKILL}/SKILL.md"].endswith(note)


@pytest.mark.parametrize("agent", NEWER)
def test_the_newer_agents_get_the_note_too(home, agent):
    """Merging 0.1.96 into the agents of 0.1.97: a Tor start of one of them ended in a KeyError (NOTE_PLACES had none
    of them). Each gets network.md; OpenHands it in its system message's suffix, Pi as a section of its system
    prompt, each after the loop guard's text; the skill names network.md."""
    a = tor_start(agent)
    a.run_id = f"{agent}-r3"
    note = v.agent_note(a)
    files = session_files(a)
    assert files[v.NOTE_FILE] == note
    assert s.NOTE_PLACES[agent]
    if agent == "openhands":
        settings = json.loads(files["openhands/settings.json"])
        assert settings["agent_settings_diff"]["agent_context"]["system_message_suffix"] == note
    elif agent == "pi":
        assert json.loads(files["pi/sandbox-ai.json"])["instructions"] == note
    elif agent == "opencode2":
        assert json.loads(s.agent_environment(a)["OPENCODE_CONFIG_CONTENT"])["instructions"] == [v.NOTE_PATH]
    elif agent == "letta":
        assert json.loads(files["letta/sandbox-ai-start.json"])["note"] == v.NOTE_PATH
    skill = (ROOT / "sandbox" / "builtin" / agent / "skills" / "sandbox-ai" / "SKILL.md").read_text()
    assert "/sandbox/session/network.md" in skill


@pytest.mark.parametrize("agent", ["openhands", "pi"])
def test_the_loop_guards_text_and_the_note_share_their_place(home, monkeypatch, agent):
    llm = s.load_llm_module()
    monkeypatch.setattr(s, "load_llm_module", lambda: llm)
    monkeypatch.setattr(llm, "openhands_loop", lambda a: {"steps": 40, "instructions": "LOOP"})
    monkeypatch.setattr(llm, "openhands_llm", lambda a: {"model": "openai/m"})
    monkeypatch.setattr(llm, "pi_llm", lambda a: {"provider": None, "instructions": "LOOP", "steps": 40})
    a = tor_start(agent)
    a.llm = "x"
    note = v.agent_note(a)
    files = s.agent_files(a)
    if agent == "openhands":
        settings = json.loads(files["openhands/settings.json"])
        assert settings["agent_settings_diff"]["agent_context"]["system_message_suffix"] == "LOOP\n\n" + note
        assert settings["conversation_settings_diff"]["max_iterations"] == 40
    else:
        config = json.loads(files["pi/sandbox-ai.json"])
        assert config["instructions"] == "LOOP\n\n" + note and config["steps"] == 40


@pytest.mark.parametrize("agent", AGENTS + NEWER)
def test_without_a_vpn_no_agent_gets_a_note(home, agent):
    a = s.parse_args([agent, "--backend", "podman", "--no-intro"])
    a.run_id = f"{agent}-r2"
    assert s.network_note(a) is None and s.note_files(a) == {}
    command = " ".join(s.agent_command(a, agent))
    assert "developer_instructions" not in command and "Network of this run" not in command
    assert "instructions" not in json.loads(s.agent_environment(a).get("OPENCODE_CONFIG_CONTENT") or "{}")
    if agent == "claude" and not s.managed_hooks(a):
        assert s.hook_mount(a, Path("/s")) is None


def test_qwen_and_goose_keep_the_loop_guards_text_beside_the_note(home, monkeypatch):
    """Qwen Code takes one --append-system-prompt, Goose one Top Of Mind file: the loop guard's text (--llm) and the
    note share it."""
    llm = s.load_llm_module()
    monkeypatch.setattr(s, "load_llm_module", lambda: llm)
    monkeypatch.setattr(llm, "agent_arguments", lambda a: ["--append-system-prompt", "LOOP"])
    monkeypatch.setattr(llm, "goose_loop_text", lambda a: "LOOP")
    for agent in ("qwen", "goose"):
        a = tor_start(agent)
        a.llm = "x"
        note = v.agent_note(a)
        if agent == "qwen":
            command = s.agent_command(a, "qwen")
            assert command.count("--append-system-prompt") == 1
            assert command[command.index("--append-system-prompt") + 1] == "LOOP\n\n" + note
        else:
            goose = s.load_goose_module()
            monkeypatch.setattr(goose, "managed_config", lambda a, mcp=None: {})
            assert s.agent_files(a)[f"goose/{goose.LOOP_FILE.rsplit('/', 1)[1]}"] == "LOOP\n\n" + note


# --- end to end: the real ./sandbox-ai ------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.add_image("localhost/sandbox-ai-tor:latest", "localhost/claude-sandbox:latest")
    added = made.run("vpn", "add", "tor-test", "--tor", "--socks")
    assert added.returncode == 0, added.stderr
    return made


@pytest.mark.parametrize("agent", AGENTS + NEWER)
def test_e2e_the_dry_run_plan_shows_the_note_for_every_agent(host, agent):
    result = host.run(agent, "--vpn", "tor-test", "--dns", "--dry-run")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout[result.stdout.index("{"):])
    note = plan["network_note"]
    assert note["file"] == "/sandbox/session/network.md" and note["agent_reads_it"]
    assert "Every TCP connection here goes through Tor by itself" in note["text"]
    assert "socks5h://127.0.0.1:9050" in note["text"] and "every one (--dns)" in note["text"]
    plain = host.run(agent, "--dry-run")
    assert json.loads(plain.stdout[plain.stdout.index("{"):])["network_note"] is None


def test_e2e_a_claude_start_through_tor_hands_claude_the_note(host):
    """The start itself: the sandbox's session directory holds network.md and Claude Code's managed CLAUDE.md, and
    /etc/claude-code is mounted read-only from it."""
    keep = host.directory / "kept"
    result = host.run("claude", "--vpn", "tor-test", "--no-intro", env={
        "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(keep),
        "FAKE_TOR_SCHEDULE": json.dumps([[[0, 100, "Done"]]])})
    assert result.returncode == 0, result.stderr
    session = keep / "session"
    note = (session / "network.md").read_text()
    assert note.startswith("## Network of this run (sandbox-ai): Tor (profile tor-test)")
    assert "socks5h://127.0.0.1:9050" in note and "only those on the allow-list" in note
    assert (session / "claude-code" / "CLAUDE.md").read_text() == note
    runs = [call for call in host.calls() if call[:1] == ["run"] and "--pod" in call]
    assert any(",dst=/etc/claude-code,ro," in arg for call in runs for arg in call)
