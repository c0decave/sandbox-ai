"""Teams on the Board (0.1.98): what `sandbox-ai team start` takes for a team designed on the app's Board, the web's
Teams page or the command line - each member's role (--role AGENT=TEXT, added to its first prompt) and its own model,
LLM server and security mode (--member-model, --member-llm, --member-mode: in place of the team's --model, --llm and
--mode for that member), --no-config for every member (the Board starts exactly the board, as for one sandbox), and
`team start --dry-run [--json]`: every check of a start, then each member's start as the launcher's dry run, nothing
made - the one check the Board and the web ask (team.py validates; the front ends do not copy its rules).

Unit tests, then the real ./sandbox-ai (tests/live_fakes.py: only Podman, tmux and ssh stood in for)."""
import importlib.util
import json
from pathlib import Path
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


team = load("team_for_board_teams", "sandbox/team.py")
web = load("webui_for_board_teams", "sandbox/webui.py")


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def ok(argv, **_kw):
    return subprocess.CompletedProcess(argv, 0, "", "")


def started(tmp_path, options=(), **kwargs):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return ok(argv)
    assert team.start("bt", ["claude", "codex", "goose"], tmp_path, list(options), lambda line: None, run, taken=[],
                      **kwargs) == 0
    return {argv[3]: argv for argv in calls}


def before_prompt(argv):
    return argv[:argv.index("--")]


def test_a_role_goes_into_its_members_first_prompt_and_the_record(tmp_path):
    calls = started(tmp_path, roles={"claude": "plans and merges", "codex": "writes the tests first"})
    assert calls["claude"][-1].endswith("Your role in the team: plans and merges")
    assert "You lead the team bt" in calls["claude"][-1]
    assert calls["codex"][-1].endswith("Your role in the team: writes the tests first")
    assert "Your role" not in calls["goose"][-1], "no role: the prompt as before"
    data = team.load("bt")
    assert data["roles"] == {"claude": "plans and merges", "codex": "writes the tests first"}


def test_a_members_own_model_llm_and_mode_replace_the_teams_for_it_only(tmp_path):
    calls = started(tmp_path, ["--model", "opus", "-mo", "high", "--allow-pypi"],
                    member_options={"codex": {"--model": "gpt-5.3-codex"}, "goose": {"--llm": "lab:qwen3", "--mode": "medium"}})
    claude, codex, goose = (before_prompt(calls[a]) for a in ("claude", "codex", "goose"))
    assert claude[claude.index("--model") + 1] == "opus" and "high" in claude
    assert codex.count("--model") == 1 and codex[codex.index("--model") + 1] == "gpt-5.3-codex"
    assert codex[codex.index("--mode") + 1] == "high", "the team's mode stays where the member has none"
    assert goose[goose.index("--llm") + 1] == "lab:qwen3" and goose.count("--mode") == 1
    assert goose[goose.index("--mode") + 1] == "medium" and "--allow-pypi" in goose
    assert team.load("bt")["member_options"] == {"codex": {"--model": "gpt-5.3-codex"},
                                                 "goose": {"--llm": "lab:qwen3", "--mode": "medium"}}


@pytest.mark.parametrize("kwargs, message", [
    ({"roles": {"opencode": "x"}}, "--role opencode=...: opencode is no member"),
    ({"roles": {"codex": "two\nlines"}}, "one line of text"),
    ({"roles": {"codex": "x" * 1001}}, "at most 1000"),
    ({"roles": {"codex": "  "}}, "one line of text"),
    ({"member_options": {"qwen": {"--model": "m"}}}, "qwen is no member"),
    ({"member_options": {"codex": {"--mode": "loose"}}}, "a mode is one of"),
    ({"member_options": {"codex": {"--model": "a b"}}}, "letters, digits"),
    ({"member_options": {"codex": {"--model": "--offline"}}}, "letters, digits"),
    ({"member_options": {"codex": {"--env": "A=1"}}}, "own option is one of"),
])
def test_roles_and_member_options_are_checked_before_anything_starts(tmp_path, kwargs, message):
    run = lambda argv, **kw: (_ for _ in ()).throw(AssertionError("started"))  # noqa: E731
    with pytest.raises(ValueError, match=message):
        team.start("bt", ["claude", "codex"], tmp_path, [], print, run, taken=[], **kwargs)
    assert not (team.teams_dir() / "bt").exists()


def test_the_command_line_parses_the_per_member_values():
    seen = []
    original = team.start
    try:
        team.start = lambda *args, **kw: seen.append((args, kw)) or 0
        assert team.main(["start", "bt", "--agents", "claude,codex", "-w", "/w", "--role", "codex=reviews",
                          "--member-model", "codex=gpt-5", "--member-llm", "claude=lab:qwen3", "--member-mode",
                          "codex=high", "--", "--allow-pypi"]) == 0
    finally:
        team.start = original
    _args, kw = seen[0]
    assert kw["roles"] == {"codex": "reviews"} and kw["dry_run"] is False
    assert kw["member_options"] == {"codex": {"--model": "gpt-5", "--mode": "high"}, "claude": {"--llm": "lab:qwen3"}}
    for bad in (["--role", "codex"], ["--member-model", "gemini=x"], ["--role", "codex=a", "--role", "codex=b"]):
        assert team.main(["start", "bt", "--agents", "claude,codex", *bad]) == 1


def test_old_records_without_roles_still_load_and_damaged_ones_are_said(tmp_path):
    started(tmp_path)
    path = team.record_path("bt")
    data = json.loads(path.read_text())
    for key in ("roles", "member_options"):
        data.pop(key)
    path.write_text(json.dumps(data))
    assert team.load("bt")["members"] == ["claude", "codex", "goose"]
    argv = team.launcher_argv(team.load("bt"), "codex")
    assert "Your role" not in argv[-1]
    for damaged in ({"roles": {"opencode": "x"}}, {"roles": {"codex": 1}}, {"member_options": {"codex": {"--env": "x"}}},
                    {"member_options": {"codex": ["--model", "x"]}}):
        path.write_text(json.dumps({**data, **damaged}))
        with pytest.raises(ValueError, match="damaged"):
            team.load("bt")


def test_no_config_is_the_teams_to_choose_config_stays_refused(tmp_path):
    calls = started(tmp_path, ["--no-config"])
    assert all("--no-config" in before_prompt(argv) for argv in calls.values())
    for options in (["--config", "/c.json"], ["-c", "/c.json"]):
        with pytest.raises(ValueError, match="chooses these itself"):
            team.start("bt2", ["claude", "codex"], tmp_path, options, print, ok, taken=[])


def test_a_dry_run_checks_every_member_with_the_launcher_and_makes_nothing(tmp_path):
    calls, lines = [], []

    def run(argv, **kw):
        calls.append(argv)
        return ok(argv)
    assert team.start("bt", ["claude", "codex"], tmp_path, ["--allow-pypi"], lines.append, run, taken=[],
                      dry_run=True, member_options={"codex": {"--model": "gpt-5"}}) == 0
    assert not (team.teams_dir() / "bt").exists() and team.names() == []
    for argv, agent in zip(calls, ("claude", "codex")):
        options = before_prompt(argv)
        assert argv[3] == agent and "--dry-run" in options and "--allow-pypi" in options
        assert "--mcp" not in options, "the board is made by the start: a dry run does not know it"
        assert options[options.index("--session") + 1] == f"bt-{agent}"
    assert "gpt-5" in calls[1]
    assert lines == ["sandbox-ai team: bt-claude would start (leads)", "sandbox-ai team: bt-codex would start"]


def test_a_dry_run_names_the_member_the_launcher_refuses(tmp_path):
    def run(argv, **kw):
        refused = argv[3] == "codex"
        return subprocess.CompletedProcess(argv, 2 if refused else 0, "", "agent-sandbox: unknown LLM server lab\n"
                                           if refused else "")
    with pytest.raises(ValueError, match="bt-codex: unknown LLM server lab"):
        team.start("bt", ["claude", "codex"], tmp_path, [], print, run, taken=[], dry_run=True)
    assert not (team.teams_dir() / "bt").exists()


def test_the_json_dry_run_gives_the_plan_or_the_refusal_with_its_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(team, "existing_sessions", lambda name, agents: [])
    monkeypatch.setattr(team.subprocess, "run", ok)
    out = []
    assert team.main(["start", "bt", "--agents", "codex,claude", "-w", str(tmp_path), "--role", "claude=reviews",
                      "--dry-run", "--json", "--", "--allow-pypi"], out.append) == 0
    plan = json.loads(out[-1])
    assert plan["ok"] is True and plan["lead"] == "codex" and plan["roles"] == {"claude": "reviews"}
    assert [(m["agent"], m["session"], m["lead"]) for m in plan["plan"]] == [("codex", "bt-codex", True),
                                                                              ("claude", "bt-claude", False)]
    assert plan["plan"][1]["prompt"].endswith("Your role in the team: reviews")
    assert "--allow-pypi" in plan["plan"][0]["options"] and team.names() == []
    (team.teams_dir() / "bt").mkdir(parents=True)
    team.write_record("bt", {"name": "bt"})
    out.clear()
    assert team.main(["start", "bt", "--agents", "codex,claude", "--dry-run", "--json"], out.append) == 1
    refused = json.loads(out[-1])
    assert refused["ok"] is False and refused["code"] == "exists" and "team bt exists" in refused["error"]
    out.clear()
    assert team.main(["start", "bt2", "--agents", "codex", "--dry-run", "--json"], out.append) == 1
    assert json.loads(out[-1])["code"] is None and "--agents" in json.loads(out[-1])["error"]
    assert team.main(["start", "bt3", "--agents", "codex,claude", "--json"], out.append) == 1
    assert "--json goes with --dry-run" in capsys.readouterr().err


# --- the web's team start -------------------------------------------------------------------------------------------

def test_the_web_team_start_passes_roles_and_member_options_through_the_real_command_line(monkeypatch):
    argv = web.tool_command("team-start", {
        "host": "local", "team": "bt", "agents": "claude,codex", "workspace": "/srv/app", "options": "[]",
        "roles": json.dumps({"codex": "reviews every change"}),
        "member_options": json.dumps({"codex": {"model": "gpt-5", "mode": "high"}, "claude": {"llm": "lab:qwen3"}})})
    assert "--role=codex=reviews every change" in argv and "--member-model=codex=gpt-5" in argv
    assert "--member-mode=codex=high" in argv and "--member-llm=claude=lab:qwen3" in argv
    got = []
    monkeypatch.setattr(team, "start", lambda *args, **kw: got.append(kw) or 0)
    assert team.main(argv[1:]) == 0
    assert got[0]["roles"] == {"codex": "reviews every change"}
    assert got[0]["member_options"] == {"codex": {"--model": "gpt-5", "--mode": "high"}, "claude": {"--llm": "lab:qwen3"}}
    for bad in ({"roles": json.dumps({"qwen": "x"})}, {"roles": "[]"}, {"member_options": json.dumps({"codex": {"env": "x"}})},
                {"member_options": json.dumps({"codex": {"model": 3}})}, {"roles": json.dumps({"codex": "a\nb"})}):
        with pytest.raises(web.Refused):
            web.tool_command("team-start", {"host": "local", "team": "bt", "agents": "claude,codex",
                                            "workspace": "/srv/app", "options": "[]", **bad})


def test_an_older_host_is_told_before_the_per_member_values_reach_it():
    argv = ["team", "start", "--agents=claude,codex", "--workspace=/w", "--role=codex=x", "bt", "--"]
    assert any(since == (0, 1, 98) and test(argv) for since, _what, test in web.NEWER)
    plain = ["team", "start", "--agents=claude,codex", "--workspace=/w", "bt", "--"]
    assert not any(since == (0, 1, 98) and test(plain) for since, _what, test in web.NEWER)


# --- the real ./sandbox-ai ---------------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    found = Host(tmp_path / "host")
    found.env["SANDBOX_AI_SECRETS"] = "file"
    return found


def test_e2e_a_dry_run_of_a_team_with_roles_and_member_options(host):
    done = host.run("team", "start", "e2bt", "--agents", "claude,codex", "-w", str(host.workspace),
                    "--role", "codex=reviews every change", "--member-model", "codex=gpt-5", "--member-mode",
                    "claude=high", "--dry-run", "--json", "--", "--no-auth", "--no-config")
    assert done.returncode == 0, done.stdout + done.stderr
    plan = json.loads(done.stdout)
    assert plan["ok"] and [m["session"] for m in plan["plan"]] == ["e2bt-claude", "e2bt-codex"]
    assert plan["member_options"] == {"codex": {"--model": "gpt-5"}, "claude": {"--mode": "high"}}
    assert host.run("team", "list").stdout.strip() == "", "nothing was made"
    assert not [c for c in host.calls() if c[:1] not in (["image"],)], "nothing was started"


def test_e2e_a_dry_run_says_what_the_launcher_refuses_for_a_member(host):
    done = host.run("team", "start", "e2bt", "--agents", "claude,codex", "-w", str(host.workspace),
                    "--member-llm", "codex=nosuchserver", "--dry-run", "--json", "--", "--no-auth")
    assert done.returncode == 1
    refused = json.loads(done.stdout)
    assert refused["ok"] is False and refused["error"].startswith("e2bt-codex: ") and "nosuchserver" in refused["error"]
    assert host.run("team", "list").stdout.strip() == ""


# --- the web's Teams page (node) -------------------------------------------------------------------------------------

def web_driver():
    from test_web import FAKE_DOM
    return FAKE_DOM + r"""
const buttonIn = (node, text) => node.all().find(n => n.tagName === "button" && n.textContent === text);
const byId = (id) => body.all().find(n => n.id === id);
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c", terminals: true }; state.host = "ct1"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }]; var terminalsOpened = []; openTerminal = (params, title) => { terminalsOpened.push(params); };', context);
  const choices = { agents: ["claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose"], error: null, profiles: [] };
  answer = (method, path) => (path.endsWith("/teams") ? [] : path.endsWith("/team-choices") ? choices : {});
  vm.runInContext('state.page = "teams";', context);
  await vm.runInContext("pages.teams(__main)", context); await settle();
  result.modes = byId("team-mode-codex").children.map(o => o.value);
  byId("team-name").value = "duo"; byId("team-lead").value = "claude"; byId("team-lead").fire("change");
  byId("team-agent-codex").checked = true; byId("team-agent-codex").fire("change");
  byId("team-workspace").value = "/srv/x"; byId("team-options").value = "";
  byId("team-role-codex").value = " reviews every change ";
  byId("team-model-codex").value = "gpt-5";
  byId("team-mode-codex").value = "high";
  byId("team-llm-claude").value = "lab:qwen3";
  byId("team-role-qwen").value = "not a member";
  buttonIn(main, "Start the team in a terminal").fire("click");
  result.started = vm.runInContext("terminalsOpened.slice()", context);
  vm.runInContext('clear(__main); setLanguage("de");', context);
  await vm.runInContext("pages.teams(__main)", context); await settle();
  result.german = [byId("team-role-codex").attributes.placeholder, byId("team-mode-codex").children[0].textContent];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="needs node")
def test_the_web_page_sends_each_members_role_and_own_options(tmp_path):
    from test_web import run_driver
    result = run_driver(tmp_path, web_driver(), "board_teams")
    assert result["modes"] == ["", "lax", "low", "medium", "high", "paranoid"]
    started = result["started"][0]
    assert json.loads(started["roles"]) == {"codex": "reviews every change"}, "members of the team only, trimmed"
    assert json.loads(started["member_options"]) == {"claude": {"llm": "lab:qwen3"},
                                                     "codex": {"model": "gpt-5", "mode": "high"}}
    argv = web.tool_command("team-start", {**started, "host": "local"})
    assert "--role=codex=reviews every change" in argv and "--member-mode=codex=high" in argv
    assert result["german"] == ["Rolle, z. B. prüft jede Änderung", "Modus (der des Teams)"]
