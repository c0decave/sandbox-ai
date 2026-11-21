"""Hotfix 0.1.96: teams and VPNs, and one VPN profile in several sandboxes at once.

The approved task: "sandbox/team.py starts one sandbox per member; options after `--` reach every member, so `-- --vpn
PROFILE` gives each member its own VPN container" - so one profile in a team of three was three tunnels of it at
once. Now:
  - `team start NAME --agents ... --vpn PROFILE` and `--member-vpn AGENT=PROFILE`, kept in the team's record (each
    member a session that keeps its --vpn when it is restarted);
  - one profile in several members: Tor allowed ("each member its own Tor", said); WireGuard/Mullvad refused with the
    reason (the same key twice gets the same tunnel address, the server sends to whichever spoke last) and what to do
    instead (one profile per member - Mullvad allows several device keys - or Tor); OpenVPN only after an explicit
    confirmation (--allow-shared-openvpn); a persistent Tailscale node once, an --ephemeral one in parallel;
  - the SAME rule for any two sandboxes: a second start (a session, an MCP server's own VPN, a team member) with a
    WireGuard/Mullvad profile that is up already is refused as Tailscale's was, naming the run that holds it - but a
    start's own networked MCP servers are no second start: they borrow its tunnel (tests/test_hotfix_mcp_lent_vpn.py);
  - team members may be hermes, qwen, dsh and goose too, each with its first prompt in its own interactive form.

Unit tests, then the real ./sandbox-ai (tests/live_fakes.py: only Podman, tmux and ssh stood in for)."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

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


team = load("team_for_hotfix_vpn", "sandbox/team.py")
v = team.vpn_module()
presets = load("presets_for_hotfix_vpn", "sandbox/presets.py")
dsh = load("dsh_for_hotfix_vpn", "sandbox/dsh.py")
chat = load("dsh_chat_for_hotfix_vpn", "sandbox/dsh_chat.py")
m = load("mcp_for_hotfix_vpn", "sandbox/mcp.py")

PRIVATE = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="
PEER = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
WIREGUARD = f"""[Interface]
PrivateKey = {PRIVATE}
Address = 10.66.1.2/32
DNS = 10.66.0.1

[Peer]
PublicKey = {PEER}
AllowedIPs = 0.0.0.0/0
Endpoint = 198.51.100.7:51820
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    return tmp_path


def profile(name, kind, **extra):
    """A profile as `vpn add` keeps it (the fields each type's reader takes)."""
    sections = {"wireguard": {"addresses": ["10.66.1.2/32"], "private_key_secret": f"VPN_{name.upper()}_KEY",
                              "preshared_key_secrets": {}, "peers": []},
                "openvpn": {"config_file": "/dev/null", "remotes": [], "auth": False},
                "tailscale": {"ephemeral": False}, "tor": {}}
    found = {"version": 1, "name": name, "type": kind, "provider": extra.pop("provider", None),
             kind: {**sections[kind], **extra}}
    v.save_profile(found)
    return v.load_profile(name)


# --- the rule: which profile may run in several sandboxes at once ------------------------------------------------

def test_each_profile_type_says_whether_it_may_run_twice(home):
    assert v.sharing(profile("tor", "tor")) == "shared"
    assert v.sharing(profile("tse", "tailscale", ephemeral=True)) == "shared"
    assert v.sharing(profile("ts", "tailscale")) == "once"
    assert v.sharing(profile("wg", "wireguard")) == "once"
    assert v.sharing(profile("mv", "wireguard", provider="mullvad")) == "once"
    assert v.sharing(profile("ovpn", "openvpn")) == "ask"
    why, fix = v.sharing_text(v.load_profile("wg"))
    assert "same tunnel address" in why and "whichever tunnel spoke last" in why
    assert "Mullvad allows several devices" in fix and "Tor" in fix


def test_a_second_start_with_a_wireguard_profile_up_is_refused_naming_the_run_that_holds_it(home):
    """"The SAME rule for any two sandboxes": a WireGuard (and Mullvad) profile is locked as Tailscale's was."""
    for name, extra in (("wg", {}), ("mv", {"provider": "mullvad"})):
        wg = profile(name, "wireguard", **extra)
        assert v.profile_holder(wg) is None
        with v.profile_lock(wg, "the session review-claude"):
            assert v.profile_holder(wg) == f"the session review-claude, PID {os.getpid()}"
            with pytest.raises(ValueError) as refused:
                with v.profile_lock(wg, "the session other"):
                    pass
            text = str(refused.value)
            assert f"VPN profile {name} is in use by another sandbox (the session review-claude, PID" in text
            assert "same tunnel address" in text and "Mullvad" in text and "Tor" in text
        assert v.profile_holder(wg) is None, "released at the end of the run"
        with v.profile_lock(wg, "the session other"):
            pass


def test_the_lock_holds_across_processes(home):
    """The holder is another process (another start): the second start in this one is refused."""
    wg = profile("wg", "wireguard")
    script = textwrap.dedent(f"""
        import importlib.util, sys, time
        spec = importlib.util.spec_from_file_location("v", {str(ROOT / "sandbox/vpn.py")!r})
        v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)
        with v.profile_lock(v.load_profile("wg"), "an MCP server fetch"):
            print("held", flush=True)
            sys.stdin.readline()
    """)
    child = subprocess.Popen([sys.executable, "-I", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, env=os.environ)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(ValueError, match=r"in use by another sandbox \(an MCP server fetch, PID \d+\)"):
            with v.profile_lock(wg, "a session"):
                pass
    finally:
        child.communicate("\n", timeout=30)
    with v.profile_lock(wg, "a session"):
        pass


def test_openvpn_twice_warns_tor_and_an_ephemeral_node_are_each_their_own(home):
    ovpn = profile("ovpn", "openvpn")
    said = []
    with v.profile_lock(ovpn, "the session a"):
        with v.profile_lock(ovpn, "the session b", say=said.append):
            pass
    assert len(said) == 1 and "in use by another sandbox (the session a" in said[0] and "duplicate-cn" in said[0]
    for name, kind, extra in (("tor", "tor", {}), ("tse", "tailscale", {"ephemeral": True})):
        shared = profile(name, kind, **extra)
        with v.profile_lock(shared, "one"), v.profile_lock(shared, "two"):
            assert v.profile_holder(shared) is None
    with v.profile_lock(profile("ts", "tailscale"), "one"):
        with pytest.raises(ValueError, match="a Tailscale node runs once at a time.*--ephemeral"):
            with v.profile_lock(v.load_profile("ts"), "two"):
                pass


def test_every_lock_names_what_holds_it():
    """Each place that brings a tunnel up hands the lock its holder (the session, the MCP server, the check)."""
    text = (ROOT / "sandbox/vpn.py").read_text()
    assert "profile_lock(profile)" not in text
    assert "profile_lock(profile, holder_of(a), say)" in text
    assert v.holder_of(argparse.Namespace(session="review-claude", name=None, helper_of=None)) == \
        "the session review-claude"
    assert v.holder_of(argparse.Namespace(session=None, name=None, helper_of="h1")) == "the helper h1"
    assert v.holder_of(argparse.Namespace(session=None, name=None, helper_of=None, pod_name="agent-mcp-fetch")) == \
        "the MCP server fetch"


def test_an_mcp_server_behind_the_starts_wireguard_vpn_borrows_its_tunnel_instead_of_a_second_one(home, monkeypatch):
    """A networked MCP server behind the start's --vpn would have had a tunnel of its own: with a WireGuard profile
    the same key twice, so this hotfix first refused it ("would need a tunnel of its own") - and `--vpn mullvad
    --mcp fetch` no longer started. Now the start lends it its tunnel: one key, one tunnel (tests/
    test_hotfix_mcp_lent_vpn.py holds the rest, the real ./sandbox-ai included)."""
    wg = profile("wg", "wireguard")
    profile("wg2", "wireguard")
    fetch = {"command": ["uvx", "mcp-server-fetch"], "options": ["--egress-all-allow"]}
    servers = {"fetch": fetch, "offline": {"command": ["x"], "options": ["--offline"]},
               "other": {"command": ["x"], "options": ["--vpn", "wg2"]},
               "own": {"command": ["x"], "options": ["--vpn", "wg"]}}
    monkeypatch.setattr(m, "definitions", lambda project=None: servers)
    a = argparse.Namespace(vpn="wg", vpn_profile=wg, project_document=None, run_id="claude-0a1b2c3d")
    found = m.vpn_for_servers(a, list(servers))
    assert m.lent_of(found["fetch"]) == (True, "claude-0a1b2c3d")
    assert m.lent_of(found["own"]) == (True, "claude-0a1b2c3d"), "its own definition naming the start's profile too"
    assert found["offline"] is None and found["other"] is None, "no network, or a tunnel of its own of another"
    problems, _, _ = m.start_problems(a, ["fetch", "own"], servers)
    assert not any("would need a tunnel of its own" in problem for problem in problems), problems
    tor = profile("tor", "tor")
    found = m.vpn_for_servers(argparse.Namespace(vpn="tor", vpn_profile=tor, project_document=None, run_id="r"),
                              ["fetch"])
    assert found["fetch"] == "tor" and not m.lent_of(found["fetch"])[0], "each sandbox its own Tor"


# --- team start --vpn / --member-vpn ---------------------------------------------------------------------------

def started(home, *argv, holder=lambda profile: None):
    """team.main(start ...) with each member's launcher recorded instead of run: (status, said, [argv], stderr)."""
    said, runs = [], []

    class Done:
        returncode, stderr = 0, ""
    original = team.start

    def start(*args, **kw):
        return original(*args, **{**kw, "run": lambda argv, **_: runs.append(argv) or Done(), "taken": [],
                                  "holder": holder})
    team.start = start
    try:
        status = team.main(["start", *argv], out=said.append)
    finally:
        team.start = original
    return status, said, runs


def vpn_of(argv):
    options = argv[:argv.index("--")]
    return [options[i + 1] for i, token in enumerate(options) if token == "--vpn"]


def test_tor_for_every_member_is_allowed_and_said(home, capsys):
    profile("tor", "tor")
    status, said, runs = started(home, "t1", "--agents", "claude,codex,opencode", "-w", str(home), "--vpn", "tor")
    assert status == 0, capsys.readouterr().err
    assert [vpn_of(argv) for argv in runs] == [["tor"], ["tor"], ["tor"]]
    assert any("each sandbox starts a Tor of its own" in line and "each member gets its own" in line for line in said)
    record = team.load("t1")
    assert record["vpn"] == "tor" and record["member_vpn"] == {}
    # A restart reads the record again: the same VPN in front of each member.
    assert [vpn_of(team.launcher_argv(record, agent)) for agent in record["members"]] == [["tor"]] * 3


@pytest.mark.parametrize("extra, label", [({}, "wg (wireguard)"), ({"provider": "mullvad"}, "wg (mullvad)")])
def test_one_wireguard_or_mullvad_profile_for_several_members_is_refused_with_reason_and_fix(home, capsys, extra,
                                                                                              label):
    profile("wg", "wireguard", **extra)
    status, _said, runs = started(home, "t2", "--agents", "claude,codex", "-w", str(home), "--vpn", "wg")
    err = capsys.readouterr().err
    assert status == 1 and runs == [], "nothing starts"
    assert f"VPN profile {label} would run in 2 members at once (claude, codex)" in err
    assert "same tunnel address" in err and "whichever tunnel spoke last" in err
    assert "Mullvad allows several devices" in err and "--member-vpn AGENT=PROFILE" in err and "Tor" in err
    assert not (team.teams_dir() / "t2").exists(), "no record of a team that did not start"


def test_the_options_after_the_dashes_are_checked_the_same_way(home, capsys):
    """`-- --vpn PROFILE` (the way it was given before) is the team's --vpn: checked, and kept as it."""
    profile("wg", "wireguard")
    status, _, runs = started(home, "t3", "--agents", "claude,codex", "-w", str(home), "--", "--allow-pypi",
                              "-vp", "wg")
    assert status == 1 and runs == [] and "would run in 2 members at once" in capsys.readouterr().err
    profile("tor", "tor")
    status, _, runs = started(home, "t3", "--agents", "claude,codex", "-w", str(home), "--", "--allow-pypi",
                              "--vpn=tor")
    assert status == 0 and [vpn_of(argv) for argv in runs] == [["tor"], ["tor"]]
    record = team.load("t3")
    assert record["vpn"] == "tor" and record["options"] == ["--allow-pypi"]


def test_one_wireguard_profile_per_member_is_fine(home, capsys):
    for name in ("wg1", "wg2", "wg3"):
        profile(name, "wireguard", provider="mullvad")
    status, _, runs = started(home, "t4", "--agents", "claude,codex,qwen", "-w", str(home), "--vpn", "wg1",
                              "--member-vpn", "codex=wg2", "--member-vpn", "qwen=wg3")
    assert status == 0, capsys.readouterr().err
    assert [vpn_of(argv) for argv in runs] == [["wg1"], ["wg2"], ["wg3"]]
    record = team.load("t4")
    assert record["vpn"] == "wg1" and record["member_vpn"] == {"codex": "wg2", "qwen": "wg3"}
    assert "vpn wg2" in "\n".join(_show(record["name"]))


def _show(name):
    lines = []
    team.member_states = lambda data: {agent: "running" for agent in data["members"]}
    team.show(name, lines.append)
    return lines


def test_openvpn_for_several_members_only_after_an_explicit_confirmation(home, capsys):
    profile("ovpn", "openvpn")
    status, _, runs = started(home, "t5", "--agents", "claude,codex", "-w", str(home), "--vpn", "ovpn")
    err = capsys.readouterr().err
    assert status == 1 and runs == [] and "duplicate-cn" in err and "--allow-shared-openvpn" in err
    status, said, runs = started(home, "t5", "--agents", "claude,codex", "-w", str(home), "--vpn", "ovpn",
                                 "--allow-shared-openvpn")
    assert status == 0 and len(runs) == 2
    assert any("confirmed with --allow-shared-openvpn" in line for line in said)
    assert team.load("t5")["shared_openvpn"] is True


def test_tailscale_runs_once_an_ephemeral_profile_in_parallel(home, capsys):
    profile("ts", "tailscale")
    profile("tse", "tailscale", ephemeral=True)
    status, _, runs = started(home, "t6", "--agents", "claude,codex", "-w", str(home), "--vpn", "ts")
    assert status == 1 and runs == [] and "--ephemeral" in capsys.readouterr().err
    status, said, runs = started(home, "t6", "--agents", "claude,codex", "-w", str(home), "--vpn", "tse")
    assert status == 0 and len(runs) == 2 and any("new node in each sandbox" in line for line in said)


def test_a_profile_another_sandbox_holds_is_refused_before_the_first_member_starts(home, capsys):
    profile("wg", "wireguard")
    profile("wg2", "wireguard")
    status, _, runs = started(home, "t7", "--agents", "claude,codex", "-w", str(home), "--vpn", "wg",
                              "--member-vpn", "codex=wg2",
                              holder=lambda p: "the session work-claude, PID 4711" if p["name"] == "wg" else None)
    err = capsys.readouterr().err
    assert status == 1 and runs == []
    assert "VPN profile wg (wireguard) is in use by the session work-claude, PID 4711" in err


def test_member_vpn_names_a_member_and_a_profile(home, capsys):
    profile("tor", "tor")
    for wrong, text in ((["--member-vpn", "gemini=tor"], "AGENT=PROFILE"), (["--member-vpn", "codex"], "AGENT=PROFILE"),
                        (["--member-vpn", "goose=tor"], "goose is no member of this team"),
                        (["--member-vpn", "codex=../x"], "")):
        status, _, runs = started(home, "t8", "--agents", "claude,codex", "-w", str(home), *wrong)
        assert status == 1 and runs == [] and text in capsys.readouterr().err
    status, _, _ = started(home, "t8", "--agents", "claude,codex", "-w", str(home), "--vpn", "nope")
    assert status == 1 and "no VPN profile nope" in capsys.readouterr().err


def test_an_older_record_without_vpn_fields_still_loads(home):
    directory = team.teams_dir() / "old"
    directory.mkdir(parents=True)
    (directory / "team.json").write_text(json.dumps({"version": 1, "name": "old", "members": ["claude", "codex"],
                                                     "lead": "claude", "workspace": "/w", "options": ["--vpn", "t"]}))
    data = team.load("old")
    assert team.member_vpns(data) == {"claude": None, "codex": None}
    assert vpn_of(team.launcher_argv(data, "codex")) == ["t"], "its options are passed as they were"


# --- hermes, qwen, dsh and goose as members ------------------------------------------------------------------

def test_every_agent_can_be_a_member_with_its_first_prompt_in_its_own_form(home):
    # Every agent of sandbox-ai (0.1.98: OpenHands, nanobot, Letta Code, OpenCode 2 and Pi too).
    assert set(team.AGENTS) == set(team.load_module("launcher_for_team_agents", "agent_sandbox.py").AUTH_TARGETS)
    for agent in team.AGENTS:
        if agent != "dsh":  # the same forms as a preset's first task (presets.prompt_args)
            assert team.prompt_args(agent, "-x task") == presets.prompt_args({"prompt": "-x task"}, agent), agent
    assert team.prompt_args("dsh", "-x task") == ["chat", "--prompt=-x task"]
    data = {"name": "all", "members": list(team.AGENTS), "lead": "claude", "workspace": "/w", "options": []}
    for agent in team.AGENTS:
        argv = team.launcher_argv(data, agent)
        assert argv[argv.index("--mcp") + 1] == "team-all" and argv[3] == agent


def test_the_dsh_chat_sends_the_members_prompt_first_and_stays():
    """DSH has no chat of its own: sandbox-ai's chat takes the member's role as its first message (chat --prompt)."""
    session, first = dsh.chat_options(["chat", "--prompt=wait for tasks", "--session-id", "s-1"])
    assert (session, first) == ("s-1", "wait for tasks")
    ns = argparse.Namespace(agent="dsh", agent_args=["chat", "--prompt=wait for tasks"], show_thinking=None)
    command = dsh.command(ns, "dsh", ["chat", "--prompt=wait for tasks"])
    assert command[command.index("--first") + 1] == "wait for tasks"
    with pytest.raises(ValueError, match="not an option of the chat"):
        dsh.chat_options(["chat", "--prompt"])


def test_the_dsh_chat_runs_the_first_message_before_it_reads_yours(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    sent = []
    monkeypatch.setattr(chat, "read_message", lambda out, history, path: sent.append("asked") or "/exit")
    monkeypatch.setattr(chat.Chat, "send", lambda self, message, retry=True: sent.append(message))
    assert chat.main(["--first", "You are codex in the team", "--", "dsh", "headless"]) == 0
    assert sent == ["You are codex in the team", "asked"]
    assert "> You are codex in the team" in capsys.readouterr().out


# --- the real ./sandbox-ai ---------------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    found = Host(tmp_path / "host")
    found.env["SANDBOX_AI_SECRETS"] = "file"
    return found


def add_wireguard(host, name):
    conf = host.directory / f"{name}.conf"
    conf.write_text(WIREGUARD)
    conf.chmod(0o600)
    done = host.run("vpn", "add", name, "--wireguard", str(conf))
    assert done.returncode == 0, done.stderr


def test_e2e_team_start_refuses_one_wireguard_profile_for_two_members(host):
    add_wireguard(host, "wg")
    for vpn in (["--vpn", "wg"], ["--", "--vpn", "wg"]):
        done = host.run("team", "start", "e2e", "--agents", "claude,codex", "-w", str(host.workspace), *vpn)
        assert done.returncode == 1, done.stdout
        assert "would run in 2 members at once (claude, codex)" in done.stderr
        assert "same tunnel address" in done.stderr and "Mullvad allows several devices" in done.stderr
        assert host.calls() == [], "no member was started"
    assert host.run("team", "list").stdout.strip() == ""


def launcher_log(host):
    return (host.home / ".local/state/sandbox-ai/launcher.log").read_text()


def test_e2e_team_start_with_tor_says_each_member_gets_its_own_and_keeps_it(host):
    done = host.run("vpn", "add", "tor", "--tor")
    assert done.returncode == 0, done.stderr
    host.add_image("localhost/claude-sandbox:latest", "localhost/codex-sandbox:latest", "localhost/sandbox-ai-tor:latest")
    done = host.run("team", "start", "e2t", "--agents", "claude,goose", "-w", str(host.workspace), "--vpn", "tor",
                    "--", "--no-auth")
    assert "each sandbox starts a Tor of its own" in done.stdout and "each member gets its own" in done.stdout
    # The lead's start (the fake Podman stops it at the board's pod) took the VPN in front of it.
    assert "agent claude" in launcher_log(host) and "MCP servers team-e2t, VPN tor, session, name e2t-claude" in \
        launcher_log(host)
    shown = json.loads(host.run("team", "show", "--json", "e2t").stdout)
    assert shown["vpn"] == "tor" and shown["members"] == ["claude", "goose"]


@pytest.mark.parametrize("agent", ["hermes", "qwen", "dsh", "goose"])
def test_e2e_hermes_qwen_dsh_and_goose_start_as_members_with_their_prompt(host, agent):
    """The launcher takes each one's member start as team.py builds it (its prompt form, the board as --mcp)."""
    host.add_image(f"localhost/{agent}-sandbox:latest", "localhost/codex-sandbox:latest")
    done = host.run("team", "start", f"e2{agent}", "--agents", f"{agent},claude", "-w", str(host.workspace), "--",
                    "--no-auth")
    log = launcher_log(host)
    assert f"start: checked: agent {agent}," in log and f"MCP servers team-e2{agent}" in log, done.stderr[-2000:]
    assert "exit status 99" in done.stdout, "it got as far as a pod (where the fake Podman stops every start)"


def test_e2e_a_session_start_with_a_wireguard_profile_up_elsewhere_is_refused_naming_the_holder(host):
    add_wireguard(host, "wg")
    host.add_image("localhost/claude-sandbox:latest", "localhost/sandbox-ai-vpn:latest")
    script = textwrap.dedent(f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("v", {str(ROOT / "sandbox/vpn.py")!r})
        v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)
        with v.profile_lock(v.load_profile("wg"), "the session work-claude"):
            print("held", flush=True)
            sys.stdin.readline()
    """)
    holder = subprocess.Popen([sys.executable, "-I", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, env=host.env)
    try:
        assert holder.stdout.readline().strip() == "held"
        done = host.run("claude", "--no-tmux", "--no-auth", "--vpn", "wg")
        assert done.returncode != 0
        assert "VPN profile wg is in use by another sandbox (the session work-claude, PID" in done.stderr, \
            done.stderr[-2000:]
        assert host.stopped_at() is None, "no pod was made"
    finally:
        holder.communicate("\n", timeout=30)
