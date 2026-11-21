"""Hotfix 0.1.96: the web interface's team start at the app's level.

Commit "Teams and VPNs" gave `sandbox-ai team start --vpn PROFILE`, `--member-vpn AGENT=PROFILE` and
`--allow-shared-openvpn`, and made hermes, qwen, dsh and goose team members; the app's team dialog got a VPN picker
(each profile with its type), "Per member (advanced)" and the OpenVPN confirmation. The web interface's team start was
left behind: it took only claude, codex and opencode (two or three of them), and its only way to a VPN was the free
options field. Now:
  - the members are team.AGENTS (read from team.py, not a copy of its own), two or more of them;
  - GET /api/hosts/HOST/team-choices: those agents and the host's VPN profiles (`vpn list --json` there, the source
    the app's dialog lists), each with its type;
  - the terminal's team-start takes vpn, member_vpn ({agent: profile}) and shared_openvpn; each profile is checked
    against the host's listing, each agent against the team's members, nothing starts with "-", and the argv is a
    list (--vpn=P, --member-vpn=AGENT=P: never read as an option);
  - one profile in several members is the launcher's to judge: WireGuard/Mullvad there refused with its own reason,
    which the terminal shows (the rule is not repeated in the page).

Unit tests of the handler, the page in node, then the real web service in front of the real ./sandbox-ai
(tests/live_fakes.py: only Podman, tmux and ssh stood in for)."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.parse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import home_guard  # noqa: E402

home_guard.ensure()
from live_fakes import Host  # noqa: E402
import test_web_matrix as matrix  # noqa: E402  (the service with a login, as the matrix drives it)

w = matrix.w


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


team = load("team_for_web_team_vpn", "sandbox/team.py")

PROFILES = [{"profile": "anon", "type": "tor", "provider": "generic", "endpoints": ["the Tor network"]},
            {"profile": "wg1", "type": "wireguard", "provider": "mullvad", "endpoints": ["198.51.100.7:51820"]},
            {"profile": "Home-2", "type": "wireguard", "provider": "generic"},
            {"profile": "office", "type": "openvpn", "provider": "generic"},
            {"profile": "ts", "type": "tailscale", "provider": "generic", "ephemeral": True},
            {"profile": "-x", "type": "tor"}, {"profile": 7, "type": "tor"}, "garbage"]


class Runner:
    """Api.runner's form: (status, out, err) of `sandbox-ai ARGS` on HOST, recorded."""

    def __init__(self, listing=PROFILES, status=0, err=""):
        self.calls, self.listing, self.status, self.err = [], listing, status, err

    def __call__(self, host, args, timeout=120, input=None):
        self.calls.append((host, list(args)))
        if args[:2] == ["vpn", "list"]:
            return self.status, json.dumps(self.listing) if self.listing is not None else "", self.err
        return 0, "", ""


def team_start(runner=None, **query):
    return w.tool_command("team-start", {"host": "local", "team": "t1", "agents": "claude,codex",
                                         "workspace": "/srv/w", **query}, runner)


# --- the members: team.AGENTS ------------------------------------------------------------------------------------

def test_the_web_takes_every_member_team_py_takes_from_team_py_itself():
    assert tuple(w.team_agents()) == team.AGENTS and len(team.AGENTS) == 12  # every agent (0.1.98)
    source = (ROOT / "sandbox/webui.py").read_text()
    assert "TEAM_AGENTS" not in source, "no copy of team.AGENTS of its own"
    argv = team_start(agents=",".join(reversed(team.AGENTS)))
    assert argv[:3] == ["team", "start", "--agents=" + ",".join(reversed(team.AGENTS))]
    for agents in ("claude", "claude,claude", "claude,bash", "claude,-x", "claude,,codex", ""):
        with pytest.raises(w.Refused, match="two or more of " + ", ".join(team.AGENTS)):
            team_start(agents=agents)


def test_a_team_of_the_new_members_is_listed_and_posted_to():
    """team list's lines with hermes and goose (TEAM_LINE knew only the three: such a team was not listed)."""
    runner = Runner()
    api = w.Api(lambda host, args, timeout=120, input=None:
                (0, "duo                  hermes, goose            /srv/p\n", "") if args == ["team", "list"]
                else runner(host, args, timeout, input))
    assert api.teams({"host": "local"}, {}) == [{"name": "duo", "members": ["hermes", "goose"], "workspace": "/srv/p"}]
    assert api.team_post({"host": "local", "name": "duo"}, {"agent": "goose", "task": "x"}) == {"output": ""}
    assert runner.calls[-1] == ("local", ["team", "post", "--", "duo", "goose", "x"])
    with pytest.raises(w.Refused, match="goose"):
        api.team_post({"host": "local", "name": "duo"}, {"agent": "bash", "task": "x"})


# --- the choices: the members and the host's VPN profiles ---------------------------------------------------------

def test_the_team_choices_are_the_members_and_the_hosts_vpn_profiles_with_their_type():
    runner = Runner()
    found = w.Api(runner).team_choices({"host": "ct1"}, {})
    assert runner.calls == [("ct1", ["vpn", "list", "--json"])], "the source the app's team dialog lists"
    assert found["agents"] == list(team.AGENTS) and found["error"] is None
    assert found["profiles"] == [
        {"profile": "anon", "type": "tor", "provider": "generic", "ephemeral": False},
        {"profile": "wg1", "type": "wireguard", "provider": "mullvad", "ephemeral": False},
        {"profile": "Home-2", "type": "wireguard", "provider": "generic", "ephemeral": False},
        {"profile": "office", "type": "openvpn", "provider": "generic", "ephemeral": False},
        {"profile": "ts", "type": "tailscale", "provider": "generic", "ephemeral": True}], \
        "a name that is no profile name (a leading '-', not a string) is left out"


def test_a_listing_that_fails_is_said_and_the_team_still_has_its_members():
    for runner in (Runner(status=1, listing=None, err="sandbox-ai vpn: boom"), Runner(listing={"not": "a list"})):
        found = w.Api(runner).team_choices({"host": "local"}, {})
        assert found["agents"] == list(team.AGENTS) and found["profiles"] == []
        assert "could not be listed" in found["error"]
    assert "boom" in w.Api(Runner(status=1, listing=None, err="sandbox-ai vpn: boom")).team_choices(
        {"host": "local"}, {})["error"]

    def no_host(host, args, timeout=120, input=None):
        raise ValueError(f"no host {host}")
    with pytest.raises(w.Refused) as refused:
        w.Api(no_host).team_choices({"host": "nope"}, {})
    assert refused.value.status == 404, "no such host: said as that, not as an empty picker"
    with pytest.raises(w.Refused, match="no host nope"):
        team_start(no_host, host="nope", vpn="anon")


# --- the terminal's team start: vpn, member_vpn, shared_openvpn ---------------------------------------------------

def test_the_team_start_passes_the_vpns_as_options_with_their_values():
    runner = Runner()
    argv = team_start(runner, agents="claude,codex,qwen", vpn="wg1", member_vpn=json.dumps({"codex": "Home-2",
                                                                                            "qwen": "anon"}),
                      shared_openvpn="1", options=json.dumps(["--mode", "high"]))
    assert argv == ["team", "start", "--agents=claude,codex,qwen", "--workspace=/srv/w", "--vpn=wg1",
                    "--member-vpn=codex=Home-2", "--member-vpn=qwen=anon", "--allow-shared-openvpn", "t1", "--",
                    "--mode", "high"]
    assert runner.calls == [("local", ["vpn", "list", "--json"])], "checked against the host's profiles"
    # without a VPN: nothing is asked, nothing is added
    runner = Runner()
    assert team_start(runner, vpn="", member_vpn="", shared_openvpn="0") == \
        ["team", "start", "--agents=claude,codex", "--workspace=/srv/w", "t1", "--"]
    assert runner.calls == []
    # only one member's own profile
    assert team_start(Runner(), member_vpn=json.dumps({"codex": "anon"}))[4:6] == ["--member-vpn=codex=anon", "t1"]


@pytest.mark.parametrize("query, text", [
    ({"vpn": "-x"}, "profile name"), ({"vpn": "--vpn"}, "profile name"), ({"vpn": "../x"}, "profile name"),
    ({"vpn": "a b"}, "profile name"), ({"vpn": "nope"}, "no VPN profile nope on local"),
    ({"member_vpn": json.dumps({"codex": "nope"})}, "no VPN profile nope on local"),
    ({"member_vpn": json.dumps({"codex": "-x"})}, "profile name"),
    ({"member_vpn": json.dumps({"goose": "anon"})}, "goose is no member of this team"),
    ({"member_vpn": json.dumps({"--vpn": "anon"})}, "is no member of this team"),
    ({"member_vpn": json.dumps({"codex": 1})}, "member_vpn"),
    ({"member_vpn": json.dumps(["codex=anon"])}, "member_vpn"),
    ({"member_vpn": "{"}, "member_vpn"), ({"member_vpn": json.dumps({"codex": ["anon"]})}, "member_vpn"),
    ({"shared_openvpn": "yes"}, "shared_openvpn is 1 or 0"),
])
def test_every_vpn_field_is_checked_before_a_terminal_opens(query, text):
    with pytest.raises(w.Refused, match=text) as refused:
        team_start(Runner(), **query)
    assert refused.value.status == 400


def test_a_profile_cannot_be_checked_when_the_listing_fails():
    with pytest.raises(w.Refused, match="VPN profiles of local could not be listed: .*boom"):
        team_start(Runner(status=1, listing=None, err="boom"), vpn="anon")


def test_one_profile_in_several_members_is_the_launchers_to_judge():
    """The web does not repeat vpn.sharing: one WireGuard profile for every member goes to team start, which refuses
    it with its own reason (shown in the terminal); the page only shows the OpenVPN box."""
    argv = team_start(Runner(), vpn="wg1")
    assert argv[4:6] == ["--vpn=wg1", "t1"]
    source = (ROOT / "sandbox/web/app.js").read_text()
    assert "same tunnel address" not in source and "whichever tunnel spoke last" not in source


def test_the_argv_of_a_team_start_parses_with_the_real_command_line(monkeypatch):
    argv = team_start(Runner(), agents="goose,dsh", vpn="anon", member_vpn=json.dumps({"dsh": "wg1"}),
                      shared_openvpn="1", options=json.dumps(["--allow-pypi"]))
    got = []
    monkeypatch.setattr(team, "start", lambda *args, **kw: got.append((args, kw)) or 0)
    assert argv[0] == "team" and team.main(argv[1:]) == 0
    (name, agents, workspace, options, _out), kw = got[0]
    assert (name, agents, workspace, options) == ("t1", ["goose", "dsh"], "/srv/w", ["--allow-pypi"])
    # (0.1.98: each member's role and own options too - none here; tests/test_board_teams.py has them)
    assert kw == {"vpn": "anon", "member_vpn": {"dsh": "wg1"}, "shared_openvpn": True, "roles": {},
                  "member_options": {}, "dry_run": False, "login_question": True}  # (the web's terminal asks)


def test_a_host_older_than_the_vpn_options_is_told_to_update(monkeypatch):
    hosts = w.module("hosts")
    monkeypatch.setattr(hosts, "older_than", lambda host, since: "0.1.95" if since >= (0, 1, 96) else None)
    args = team_start(Runner(), vpn="anon")
    refused = w.newer_needed("ct1", args)
    assert refused is not None and refused.status == 409 and "team start --vpn" in str(refused)
    refused = w.newer_needed("ct1", team_start(agents="claude,hermes"))
    assert refused is not None and "hermes, dsh, qwen and goose" in str(refused)
    assert w.newer_needed("ct1", team_start()) is None, "a team of the three as before"


# --- the page (node) ---------------------------------------------------------------------------------------------

def driver():
    from test_web import FAKE_DOM
    return FAKE_DOM + r"""
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const byId = (id) => body.all().find(n => n.id === id);
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; var terminalsOpened = []; openTerminal = (params, title) => { terminalsOpened.push(params); };', context);
  let choices = { agents: ["claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "opencode2", "pi"], error: null,
    profiles: [{ profile: "anon", type: "tor", provider: "generic" }, { profile: "wg1", type: "wireguard", provider: "mullvad" },
               { profile: "office", type: "openvpn", provider: "generic" }] };
  answer = (method, path) => (path.endsWith("/teams") ? [] : path.endsWith("/team-choices") ? choices : {});
  vm.runInContext('state.page = "teams";', context);
  await vm.runInContext("pages.teams(__main)", context); await settle();
  result.asked = calls.map(c => c.path);
  result.lead = byId("team-lead").children.map(o => o.value);
  result.members = main.all().filter(n => n.tagName === "input" && (n.id || "").startsWith("team-agent-")).map(n => n.value);
  result.vpn = byId("team-vpn").children.map(o => [o.value, o.textContent]);
  const advanced = byId("team-per-member");
  result.advanced = [advanced.tagName, advanced.attributes.open === undefined, byId("team-vpn-qwen").children.map(o => o.textContent)[0]];
  const shared = byId("team-shared-openvpn");
  const box = () => shared.parent.hidden;
  const rows = () => main.all().filter(n => (n.id || "").startsWith("team-vpn-row-") && !n.hidden).map(n => n.id.slice("team-vpn-row-".length));
  byId("team-name").value = "duo"; byId("team-lead").value = "claude"; byId("team-lead").fire("change");
  byId("team-agent-codex").checked = true; byId("team-agent-codex").fire("change");
  byId("team-agent-qwen").checked = true; byId("team-agent-qwen").fire("change");
  byId("team-workspace").value = "/srv/x"; byId("team-options").value = "";
  result.rows = rows();
  result.box = [box()];
  byId("team-vpn").value = "office"; byId("team-vpn").fire("change");
  result.box.push(box());
  byId("team-vpn").value = "wg1"; byId("team-vpn").fire("change");
  result.box.push(box());
  // one member's own OpenVPN profile: in one member only, no box
  byId("team-vpn").value = ""; byId("team-vpn").fire("change");
  byId("team-vpn-codex").value = "office"; byId("team-vpn-codex").fire("change");
  result.box.push(box());
  byId("team-vpn-qwen").value = "office"; byId("team-vpn-qwen").fire("change");
  result.box.push(box());
  shared.checked = true; shared.fire("change");
  buttonIn(main, "Start the team in a terminal").fire("click");
  // the team's profile, one member's own, the box hidden again: its value is not sent
  byId("team-vpn").value = "wg1"; byId("team-vpn").fire("change");
  byId("team-vpn-qwen").value = ""; byId("team-vpn-qwen").fire("change");
  result.box.push(box());
  buttonIn(main, "Start the team in a terminal").fire("click");
  result.started = vm.runInContext("terminalsOpened.slice()", context);
  // a host whose profiles cannot be listed: said, the team starts without a VPN
  vm.runInContext("clear(__main); terminalsOpened.length = 0", context);
  choices = { agents: ["claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose"], profiles: [], error: "the VPN profiles of ct1 could not be listed: boom" };
  await vm.runInContext("pages.teams(__main)", context); await settle();
  result.error = main.textContent.includes("the VPN profiles of ct1 could not be listed: boom");
  result.vpnAfterError = byId("team-vpn").children.map(o => o.value);
  // German
  vm.runInContext('clear(__main); setLanguage("de");', context);
  await vm.runInContext("pages.teams(__main)", context); await settle();
  result.german = [byId("team-per-member").children[0].textContent, byId("team-vpn").children[0].textContent,
    byId("team-shared-openvpn").parent.textContent];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_offers_every_member_the_hosts_vpn_profiles_per_member_and_the_openvpn_box(tmp_path):
    from test_web import run_driver
    result = run_driver(tmp_path, driver(), "team_vpn")
    assert "/api/hosts/ct1/team-choices" in result["asked"]
    assert result["lead"] == list(team.AGENTS) and result["members"] == list(team.AGENTS)
    assert result["vpn"] == [["", "(none)"], ["anon", "anon (tor)"], ["wg1", "wg1 (mullvad)"],
                             ["office", "office (openvpn)"]]
    assert result["advanced"] == ["details", True, "(the team's)"], "Per member (advanced): collapsed"
    assert result["rows"] == ["claude", "codex", "qwen"], "a row for each member of the team only"
    # hidden: no VPN; shown: the team's OpenVPN in 3 members; hidden: WireGuard (the launcher judges it); hidden:
    # OpenVPN in one member; shown: in two; hidden again with the team's WireGuard and one member's OpenVPN
    assert result["box"] == [True, False, True, True, False, True]
    first, second = result["started"]
    assert first == {"kind": "team-start", "host": "ct1", "team": "duo", "agents": "claude,codex,qwen",
                     "workspace": "/srv/x", "options": "[]", "member_vpn": '{"codex":"office","qwen":"office"}',
                     "shared_openvpn": "1"}
    assert second == {"kind": "team-start", "host": "ct1", "team": "duo", "agents": "claude,codex,qwen",
                      "workspace": "/srv/x", "options": "[]", "vpn": "wg1", "member_vpn": '{"codex":"office"}'}
    assert result["error"] and result["vpnAfterError"] == [""]
    assert result["german"][0] == "Pro Mitglied (erweitert)" and result["german"][1] == "(keines)"
    assert "OpenVPN-Profil in mehreren Mitgliedern erlauben" in result["german"][2]


# --- the real web service in front of the real ./sandbox-ai --------------------------------------------------------

@pytest.fixture
def service(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / "web" / sub))
    monkeypatch.setattr(w, "SCRYPT", matrix.FAST)
    host = Host(tmp_path / "host")
    host.env["SANDBOX_AI_SECRETS"] = "file"
    web = matrix.Web()

    def runner(name, args, timeout=120, input=None):
        assert name == "local"
        done = host.run(*args, timeout=timeout, input=input)
        return done.returncode, done.stdout, done.stderr
    web.svc.api.runner = runner  # every command of the service: the host's ./sandbox-ai
    web.host = host
    web.full = web.headers("full")  # one login (a code is taken once)
    return web


def add_wireguard(host, name):
    from test_hotfix_team_vpn import WIREGUARD
    conf = host.directory / f"{name}.conf"
    conf.write_text(WIREGUARD)
    conf.chmod(0o600)
    done = host.run("vpn", "add", name, "--wireguard", str(conf))
    assert done.returncode == 0, done.stderr


def terminal(web, **query):
    """The terminal the page opens for QUERY: its argv from the real handshake, then that argv run as the host's
    user (what the terminal shows: stdout and stderr)."""
    headers = web.full
    argv, _ = web.svc.websocket("/api/terminal?" + urllib.parse.urlencode({"host": "local", "csrf": web.csrf,
                                                                          **query}),
                                {"Host": matrix.HOST, "Origin": matrix.ORIGIN, **headers}, "192.0.2.1")
    assert argv[:3] == [sys.executable, "-I", str(ROOT / "sandbox" / "main.py")]
    return argv, subprocess.run(argv, cwd=web.host.workspace, env=web.host.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=300)


def test_e2e_the_web_lists_the_hosts_profiles_and_the_launcher_refuses_one_wireguard_key_twice(service):
    host = service.host
    add_wireguard(host, "wg")
    assert host.run("vpn", "add", "anon", "--tor").returncode == 0
    status, _, data = service.request("GET", "/api/hosts/local/team-choices", headers=service.full)
    assert status == 200, data
    found = json.loads(data)
    assert found["agents"] == list(team.AGENTS)
    assert [(p["profile"], p["type"]) for p in found["profiles"]] == [("anon", "tor"), ("wg", "wireguard")]
    workspace = str(host.workspace)
    argv, done = terminal(service, kind="team-start", team="e2w", agents="claude,goose", workspace=workspace, vpn="wg")
    assert argv[3:] == ["team", "start", "--agents=claude,goose", f"--workspace={workspace}", "--vpn=wg", "e2w", "--"]
    assert done.returncode == 1
    assert "VPN profile wg (wireguard) would run in 2 members at once (claude, goose)" in done.stderr
    assert "same tunnel address" in done.stderr and "--member-vpn AGENT=PROFILE" in done.stderr
    assert host.calls() == [], "no member was started"
    # a profile the host does not have: refused before any terminal
    with pytest.raises(w.Refused, match="no VPN profile nope on local"):
        terminal(service, kind="team-start", team="e2w", agents="claude,goose", workspace=workspace, vpn="nope")
    service.assert_nothing_leaked()


def test_e2e_a_team_from_the_web_with_tor_for_all_and_its_own_profile_for_one(service):
    host = service.host
    add_wireguard(host, "wg")
    assert host.run("vpn", "add", "anon", "--tor").returncode == 0
    # (codex's image too: the team's board is a command MCP server, run in codex's image - the start checks it)
    host.add_image("localhost/claude-sandbox:latest", "localhost/hermes-sandbox:latest", "localhost/codex-sandbox:latest",
                   "localhost/sandbox-ai-tor:latest", "localhost/sandbox-ai-vpn:latest")
    _, done = terminal(service, kind="team-start", team="e2t", agents="claude,hermes", workspace=str(host.workspace),
                       vpn="anon", member_vpn=json.dumps({"hermes": "wg"}), options=json.dumps(["--no-auth"]))
    assert "MCP servers team-e2t, VPN anon, session, name e2t-claude" in \
        (host.home / ".local/state/sandbox-ai/launcher.log").read_text(), done.stdout[-2000:] + done.stderr[-2000:]
    shown = json.loads(host.run("team", "show", "--json", "e2t").stdout)
    assert shown["vpn"] == "anon" and shown["member_vpn"] == {"hermes": "wg"}
    assert shown["members"] == ["claude", "hermes"]
