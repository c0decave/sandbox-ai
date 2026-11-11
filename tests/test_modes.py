"""Security modes (sandbox/modes.py): the plan of each mode for every agent on every backend, options that narrow a
mode (accepted) or widen it (refused, naming the mode and a laxer one), where the mode comes from (--mode, the
project file, defaults.json, the policy's min_mode), medium as exactly the start without a mode, the wizard and
doctor. The fixture `m` is the feature matrix's (tests/test_feature_matrix.py)."""
import json
import os
from pathlib import Path
import random
import re
import secrets
import uuid

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

s = fm.s
MODES = s.MODES
c = s.load_canary_module()


def provider(m):
    return s.PROVIDERS[m.agent]


def defaults_json(document):
    path = Path(os.environ["HOME"]) / ".config/sandbox-ai/defaults.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, **document}))
    return path


# --- the plan of each mode ---------------------------------------------------------------------------------------

def test_mode_names_and_table():
    assert MODES.NAMES == ("lax", "low", "medium", "high", "paranoid") and MODES.DEFAULT == "medium"
    assert [line.split(":")[0] for line in MODES.summaries()] == list(MODES.NAMES)
    assert MODES.MODES["medium"].keys() == {"summary"}  # medium sets nothing: the launcher's own defaults
    assert MODES.taint_settings("medium") == {} and MODES.taint_settings("lax") == {}  # no escalation
    assert [rule["id"] for rule in MODES.taint_settings("high")["mcp"]] == ["mode-tainted-mcp"]
    assert MODES.taint_settings("paranoid")["tool"][0]["when"] == {"tainted": True}
    with pytest.raises(ValueError, match="one of lax, low, medium, high, paranoid"):
        MODES.check_name("strict", "x")


def test_plan_lax(m):
    plan = m.plan("--mode", "lax")
    assert plan["mode"] == {"name": "lax", "origin": "--mode", "options": ["--egress-all-allow", "--ask=off",
                                                                           "--log-debug"],
                            "approval_scope": "always", "other_workspaces_read_only": False, "rules": False}
    assert plan["egress_all_allow"] and plan["log_debug"] and plan["nameservers"] == s.DEFAULT_NAMESERVERS
    assert plan["proxy"]["enabled"]  # everything is open, and every connection is logged
    run = m.run("--mode", "lax")
    assert run.config["ask"] is False and run.config["log_debug"]


def test_plan_low(m):
    plan = m.plan("--mode", "low")
    assert plan["mode"]["options"] == ["--profile", "development", "--allow-npm"]
    for host in provider(m) + s.PRESETS["pypi"] + s.PRESETS["github"] + s.PRESETS["npm"] + s.FRONTIER:
        assert host in plan["https_hosts"]
    assert not plan["egress_all_allow"] and m.run("--mode", "low").config["ask"]


def test_plan_medium_is_the_default(m):
    plain, medium = m.plan(), m.plan("--mode", "medium")
    assert plain.pop("mode") == {"name": "medium", "origin": "default", "options": [], "approval_scope": "always",
                                 "other_workspaces_read_only": False, "rules": False}
    assert medium.pop("mode")["origin"] == "--mode"
    # Every start gets a name of its own (a codename such as brisk-otter-lantern, codenames.py).
    assert re.fullmatch(r"[a-z]{3,8}-[a-z]{3,8}-[a-z]{3,8}", plain.pop("name")) and medium.pop("name")
    # The only ruleset: the proxy's part of --telemetry off (test_telemetry.py).
    assert plain == medium and plain["https_hosts"] == provider(m) and [r["name"] for r in plain["rulesets"]] == (
        ["telemetry"] if m.agent in s.TELEMETRY_PATHS else [])


def test_plan_high(m):
    other = m.tmp / "other"
    other.mkdir()
    plan = m.plan("--mode", "high", "--workspace", str(other))
    assert plan["mode"]["options"] == ["--privacy-check=always", "--memory", "4g", "--pids", "2048"]
    assert plan["mode"]["approval_scope"] == "run" and plan["mode"]["other_workspaces_read_only"]
    assert plan["https_hosts"] == provider(m) and plan["privacy_check"] == "always"
    assert plan["limits"] == {"memory": "4g", "cpus": "4", "pids": 2048}
    ruleset, *telemetry = plan["rulesets"]  # and the proxy's part of --telemetry off (test_telemetry.py)
    assert [r["name"] for r in telemetry] == (["telemetry"] if m.agent in s.TELEMETRY_PATHS else [])
    assert ruleset["name"] == "mode high" and ruleset["http"] == ["mode-provider", "mode-writes"]
    assert ruleset["defaults"]["http"] == "allow" and ruleset["mcp"] == ["mode-tainted-mcp"] and ruleset["tool"] == []
    run = m.run("--mode", "high", "--workspace", str(other))
    assert run.config["ask"] and run.config["ask_max_scope"] == "run"
    if m.backend == "podman":
        assert f"src={m.work},dst=/work,rw" in run.joined and f"src={other},dst=/workspaces/other,ro" in run.joined
    else:
        assert f"--bind {m.work} /work" in run.joined and f"--ro-bind {other} /workspaces/other" in run.joined
    assert run.proxy["push_check"]
    rules = [source for source in run.proxy["rules"] if source["name"] == "mode high"][0]
    writes = [rule for rule in rules["http"] if rule["id"] == "mode-writes"][0]
    assert writes["decision"] == "ask" and writes["methods"] == MODES.WRITE_METHODS and writes["host"] == ["*"]
    assert [rule for rule in rules["http"] if rule["id"] == "mode-provider"][0]["host"] == provider(m)
    # Taint: content from any host but the provider taints the run; the agent's own tools have no rules (no hooks).
    assert [entry["id"] for entry in rules["untrusted"]] == ["mode-hosts"] and run.config["hooks"] is None
    # The privacy check looks at the writable workspace only.
    assert s.privacy_workspaces(run.a) == [(m.work.resolve(), m.work.name)]


def test_plan_paranoid(m):
    plan = m.plan("--mode", "paranoid")
    assert plan["mode"]["options"] == ["--privacy-check=always", "--memory", "2g", "--cpus", "2", "--pids", "1024",
                                       "--workspace-ro", "--log-debug"]
    assert plan["mode"]["approval_scope"] == "once" and plan["workspace_mode"] == "ro" and plan["log_debug"]
    assert plan["https_hosts"] == provider(m) and plan["limits"] == {"memory": "2g", "cpus": "2", "pids": 1024}
    assert plan["rulesets"][0]["name"] == "mode paranoid" and plan["rulesets"][0]["defaults"]["http"] == "deny"
    assert plan["rulesets"][0]["tool"] == ["mode-tainted-tools"]
    run = m.run("--mode", "paranoid")
    assert run.config["ask_max_scope"] == "once" and run.config["log_debug"]
    # Taint escalates through the agent's hooks too: its web tools taint, its shell, edits and fetches then ask.
    (hooks,) = run.config["hooks"]["rules"]
    assert hooks["name"] == "mode paranoid" and [e["id"] for e in hooks["untrusted"]] == ["mode-agent-web"]
    if m.backend == "podman":
        assert f"src={m.work},dst=/work,ro" in run.joined
    else:
        assert f"--ro-bind {m.work} /work" in run.joined
    # Offline is narrower still: the rules need no proxy then.
    offline = m.plan("--mode", "paranoid", "--offline")
    assert offline["offline"] and not offline["proxy"]["enabled"]


def normalized(run):
    """What a start hands the sandbox, without what differs from run to run (temporary paths, run id, time)."""
    text = json.dumps({"command": run.joined, "files": run.files, "config": run.config, "proxy": run.proxy,
                       "home": run.home_files, "env": run.env}, sort_keys=True)
    text = re.sub(r"agent-sandbox-(proxy-)?[A-Za-z0-9_]{8}", "TMP", text)
    text = re.sub(r"connections-\d{8}-\d{6}", "connections-TIME", text)
    return text


def test_medium_starts_byte_identical_to_a_start_without_mode(m):
    m.monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=7))

    def run(*options):
        # The canary files' tokens are random: the same ones for each start compared here.
        chance = random.Random(7)
        m.monkeypatch.setattr(secrets, "choice", chance.choice)
        m.monkeypatch.setattr(secrets, "randbelow", chance.randrange)
        # The same name for each, and a HOME that goes with the run: a kept one would be a new directory each time.
        return normalized(m.run("--no-keep", "--name", "compared", *options))
    plain = run()
    assert run("--mode", "medium") == plain
    # And to a start with the mode machinery taken out entirely, as before modes existed.
    m.monkeypatch.setattr(MODES, "apply", lambda l, argv, a, *rest: a)
    m.monkeypatch.setattr(MODES, "check", lambda l, a, policy: None)
    assert run() == plain


# --- narrowing is accepted, widening refused ---------------------------------------------------------------------

def widening_options(m):
    """(options, the modes that refuse them, the laxer mode the refusal names)."""
    data = m.tmp / "data"
    data.mkdir(exist_ok=True)
    return [
        (["--egress-all-allow"], {"high", "paranoid"}, "medium"),
        (["-E"], {"high", "paranoid"}, "medium"),
        (["--profile", "development"], {"high", "paranoid"}, "medium"),
        (["--profile", "frontier"], {"high", "paranoid"}, "medium"),
        (["--profile", "dns"], {"high", "paranoid"}, "medium"),
        (["--allow-github"], {"high", "paranoid"}, "medium"),
        (["--allow-npm"], {"high", "paranoid"}, "medium"),
        (["--frontier"], {"high", "paranoid"}, "medium"),
        (["--allow-host", "example.org"], {"high", "paranoid"}, "medium"),
        (["--allow", "192.0.2.0/24"], {"high", "paranoid"}, "medium"),
        (["--allow-tcp", "192.0.2.7:8080"], {"high", "paranoid"}, "medium"),
        (["--allow-host-port", "3000"], {"high", "paranoid"}, "medium"),
        (["--dns"], {"high", "paranoid"}, "medium"),
        (["--nameserver", "9.9.9.9"], {"high", "paranoid"}, "medium"),
        (["--no-proxy"], {"high", "paranoid"}, "medium"),
        (["--no-inspect", "example.org"], {"high", "paranoid"}, "medium"),
        (["--ask", "off"], {"high", "paranoid"}, "medium"),
        (["--privacy-check", "public"], {"high", "paranoid"}, "medium"),
        (["--privacy-check", "off"], {"high", "paranoid"}, "medium"),
        (["--device", "/dev/kvm"], {"high", "paranoid"}, "medium"),
        (["--gpu", "dri"], {"high", "paranoid"}, "medium"),
        (["--nested-containers"], {"high", "paranoid"}, "medium"),
        (["--gui-wayland"], {"high", "paranoid"}, "medium"),
        (["--mount", f"{data}:/mnt/data:rw"], {"high", "paranoid"}, "medium"),
        (["--memory", "none"], {"high", "paranoid"}, "medium"),
        (["--memory", "8g"], {"high", "paranoid"}, "medium"),
        (["--memory", "3g"], {"paranoid"}, "high"),
        (["--pids", "4096"], {"high", "paranoid"}, "medium"),
        (["--pids", "1500"], {"paranoid"}, "high"),
        (["--cpus", "none"], {"high", "paranoid"}, "medium"),
        (["--cpus", "3"], {"paranoid"}, "high"),
        (["--dangerously-skip-permissions"], {"paranoid"}, "high"),
        (["-D"], {"paranoid"}, "high"),
        (["--gui"], {"paranoid"}, "high"),
        (["--gui-vnc", str(m.tmp / "vnc")], {"paranoid"}, "high"),
        (["--no-canaries"], {"high", "paranoid"}, "medium"),
    ]


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_widening_is_refused(m, mode):
    for options, refusing, laxer in widening_options(m):
        if mode in refusing:
            m.refused("--mode", mode, *options,
                      match=rf"widens the security mode {mode} \(--mode\): .*choose a laxer mode: --mode {laxer}")
    # A project file widens like an option.
    m.project_file({"mode": mode, "options": ["--allow-pypi"]})
    m.refused(match=rf"--allow-pypi widens the security mode {mode} \(the project file .*\)")


def test_widening_is_refused_only_where_the_mode_refuses(m):
    """paranoid refuses -D, high takes it; lax, low and medium take everything (medium is today's launcher)."""
    for options, refusing, _ in widening_options(m):
        for mode in ("lax", "low", "medium", "high"):
            if mode in refusing:
                continue
            try:
                m.plan("--mode", mode, *options)
            except ValueError as exc:  # the launcher's own checks may refuse it, never the mode
                assert "security mode" not in str(exc), (mode, options)


def test_narrowing_is_accepted(m):
    data = m.tmp / "data"
    data.mkdir()
    narrower = [["--offline"], ["--profile", "offline"], ["--no-provider-defaults"], ["--ask", "on"],
                ["--privacy-check", "always"], ["--memory", "1g"], ["--memory", "512m"], ["--cpus", "1"],
                ["--pids", "256"], ["--workspace-ro"], ["--mount", f"{data}:/mnt/data:ro"],
                ["--allow-http", f"{provider(m)[0]} GET /**"], ["--log-debug"]]
    for mode in ("high", "paranoid"):
        for options in narrower:
            plan = m.plan("--mode", mode, *options)
            assert plan["mode"]["name"] == mode
    assert m.plan("--mode", "high", "--memory", "1g")["limits"]["memory"] == "1g"
    assert m.plan("--mode", "high", "-D")["dangerously_skip_permissions"]
    assert m.plan("--mode", "high", "--gui")["gui"]
    # lax and low: a network choice replaces the mode's network, other network options add to it.
    narrowed = m.plan("--mode", "lax", "--profile", "development")
    assert not narrowed["egress_all_allow"] and "pypi.org" in narrowed["https_hosts"]
    assert m.plan("--mode", "lax", "--offline")["offline"]
    assert m.plan("--mode", "lax", "--allow-host", "example.org")["egress_all_allow"]
    assert m.plan("--mode", "low", "--offline")["https_hosts"] == []
    assert "example.org" in m.plan("--mode", "low", "--allow-host", "example.org")["https_hosts"]
    assert m.run("--mode", "lax", "--ask", "on").config["ask"]


def test_git_tokens_open_their_server_and_widen_too(m):
    m.monkeypatch.setenv("GH_PAT", fm.TOKEN)
    m.refused("--mode", "high", "--git-token-env", "https://github.com=GH_PAT", match="--allow-github widens")
    m.refused("--mode", "high", "--git-token-env", "https://git.example.org=GH_PAT",
              match="--allow-host git.example.org widens")
    assert m.plan("--mode", "high", "--git-identity")["git_identity"]


def test_mcp_servers_with_network_in_paranoid(m):
    m.project_file({"mode": "paranoid", "mcp_servers": {
        "net": {"command": ["python3", "-c", "pass"], "options": ["--allow-github"]},
        "quiet": {"command": ["python3", "-c", "pass"], "options": ["-o"]},
        "box": {"image": "ghcr.io/x/y@sha256:" + "0" * 64, "network": "full"}}})
    m.refused("--mcp", "net", match=r"--mcp net widens the security mode paranoid .*past the agent's firewall")
    m.refused("--mcp", "box", match="--mcp box widens")
    assert [s["name"] for s in m.plan("--mcp", "quiet", "--mcp", "demo")["mcp"]] == ["quiet", "demo"]
    m.project_file({"mode": "high", "mcp_servers": {
        "net": {"command": ["python3", "-c", "pass"], "options": ["--allow-github"]}}})
    assert m.plan("--mcp", "net")["mcp"][0]["name"] == "net"


def test_rules_without_launcher_options():
    """--llm, --keep and --session: checked on the parsed options."""
    l = s.launcher_namespace()
    a = s.parse_args(["codex", "--no-config"])
    a.llm = "box:model"
    assert MODES.widening(l, a, "high")[0][0] == "--llm box:model"
    assert MODES.widening(l, a, "medium") == []
    a.llm, a.keep = None, True
    a.privacy_check, a.memory, a.pids = "always", "1g", "100"
    assert [given for given, _ in MODES.widening(l, a, "paranoid")] == ["--keep"]
    assert MODES.widening(l, a, "high") == []
    a.keep, a.session = None, "web"
    assert [given for given, _ in MODES.widening(l, a, "paranoid")] == ["--session web"]
    assert MODES.widening(l, a, "high") == []


def test_paranoid_keeps_no_home(m, tmp_path, monkeypatch):
    """paranoid: every start discards its HOME (as --no-keep), --keep and --session widen it, and a kept HOME is
    not brought back under it; high keeps HOMEs like the laxer modes."""
    assert m.plan("--mode", "paranoid")["keep"] is False and m.plan("--mode", "high")["keep"] is True
    run = m.run("--mode", "paranoid")
    assert run.run_record["start"] is None and run.run_record["mode"]["name"] == "paranoid"
    assert m.run("--mode", "high").run_record["start"]  # kept: a start directory of its own
    m.refused("--mode", "paranoid", "--keep", match="--keep widens the security mode paranoid")
    m.refused("--mode", "paranoid", "--session", "web", match="--session web widens the security mode paranoid")
    m.project_file({"mode": "paranoid", "keep": True})
    m.refused(match="--keep widens the security mode paranoid")
    # The supervisor of a kept session whose mode became paranoid (its options, project file or defaults).
    root = s.load_sessions_module().root()
    b = s.parse_args(["codex", "--tmux-socket-dir", str(root / "web/tmux"), "--start-id", "web",
                      "--state-dir", str(root / "web/home")])
    b.mode = "paranoid"
    with pytest.raises(ValueError, match="paranoid keeps no HOME beyond a run"):
        s.resolve_keep(b)
    b.state_dir = None  # a session that keeps no HOME: nothing to refuse
    assert s.resolve_keep(b) is None and b.keep_home is False


# --- where the mode comes from -----------------------------------------------------------------------------------

def test_precedence(m):
    path = defaults_json({"mode": "high"})
    assert m.plan()["mode"]["name"] == "high"
    assert m.plan()["mode"]["origin"] == f"your defaults {path}"
    config = m.project_file({"mode": "low"})
    plan = m.plan()
    assert plan["mode"]["name"] == "low" and plan["mode"]["origin"] == f"the project file {config}"
    assert m.plan("--mode", "paranoid")["mode"]["name"] == "paranoid"
    assert m.plan("-mo", "lax")["mode"]["name"] == "lax"  # the short form
    assert m.plan("--no-config")["mode"]["name"] == "high"


def test_policy_min_mode(m):
    m.policy(min_mode="high")
    plan = m.plan()
    assert plan["mode"]["name"] == "high" and "min_mode" in plan["mode"]["origin"]
    assert m.plan("--mode", "paranoid")["mode"]["name"] == "paranoid"
    m.refused("--mode", "medium", match=r"The security mode medium \(--mode\) is below the administrator's minimum, "
                                        "high .*choose high or paranoid")
    m.project_file({"mode": "low"})
    m.refused(match=r"The security mode low \(the project file .*\) is below")
    m.project_file({"mode": "high", "options": ["--allow-pypi"]})
    m.refused(match="requires at least the mode high, so it cannot be used")
    m.project_file({})
    defaults_json({"mode": "lax"})
    m.refused(match=r"The security mode lax \(your defaults .*\) is below")
    # A floor of medium or below changes nothing for a start without a choice.
    m.policy(min_mode="low")
    assert m.plan("--no-config", "--mode", "low")["mode"]["name"] == "low"


def test_policy_binds_what_the_mode_sets(m):
    m.policy(forbid=["--egress-all-allow"])
    m.refused("--mode", "lax", match=r"--egress-all-allow is not allowed .*the security mode lax \(--mode\) sets it")
    assert m.plan("--mode", "lax", "--offline")["offline"]
    m.policy(max_approval_scope="once")
    assert m.run("--mode", "high").config["ask_max_scope"] == "once"  # the narrower of policy and mode
    m.policy(max_approval_scope="run")
    assert m.run("--mode", "paranoid").config["ask_max_scope"] == "once"


def test_remembered_approvals_stay_out_in_the_strict_modes(m):
    approved = Path(os.environ["HOME"]) / ".config/sandbox-ai/approved.json"
    approved.parent.mkdir(parents=True)
    approved.write_text(json.dumps({str(m.work): {"hosts": ["remembered.example"], "tcp": []}}))
    approved.chmod(0o600)
    assert "remembered.example" in m.plan()["https_hosts"]
    assert "remembered.example" not in m.plan("--mode", "high")["https_hosts"]


def test_invalid_and_misplaced_modes(m):
    defaults_json({"mode": "strict"})
    m.refused(match="defaults.json: mode: the security mode is one of")
    defaults_json({})
    m.project_file({"mode": "strict"})
    m.refused(match=r"\.sandbox-ai\.json: mode: the security mode is one of")
    m.project_file({"options": ["--mode", "lax"]})
    m.refused(match=r"option --mode is not allowed in a project configuration \(the key \"mode\" chooses")


def test_mcp_server_sandbox_ignores_your_default_mode(m):
    """A server's own sandbox (started with --mcp-socket-dir) follows its definition; the policy's floor holds."""
    defaults_json({"mode": "high"})
    sock = m.tmp / "sock"
    assert m.plan("--mcp-socket-dir", str(sock), "--allow-github")["mode"]["name"] == "medium"
    m.policy(min_mode="high")
    m.refused("--mcp-socket-dir", str(sock), "--allow-github", match="--allow-github widens the security mode high")


# --- help, doctor and the wizard ---------------------------------------------------------------------------------

def test_help_lists_the_modes():
    text = s.build_parser().format_help()
    assert "Security mode:" in text and "-mo, --mode MODE" in text
    for name in MODES.NAMES:
        assert f"{name}:" in " ".join(text.split())
    assert s.parse_args(["codex", "-mo", "high"]).mode == "high"
    assert s.canonical_options(["-mo", "high", "-an"]) == ["--mode", "high", "--allow-npm"]


def test_doctor_names_the_mode(tmp_path, monkeypatch, capsys):
    doctor = fm.importlib.util.module_from_spec(fm.importlib.util.spec_from_file_location(
        "doctor_for_modes", fm.ROOT / "sandbox/doctor.py"))
    doctor.__spec__.loader.exec_module(doctor)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    work = tmp_path / "work"
    work.mkdir()
    status, title, detail, _ = doctor.check_mode(work)
    assert (status, title) == ("ok", "Security mode") and detail.startswith("medium (default); modes: lax, low")
    (tmp_path / "config/sandbox-ai").mkdir(parents=True)
    (tmp_path / "config/sandbox-ai/defaults.json").write_text('{"version": 1, "mode": "paranoid"}')
    assert doctor.check_mode(work)[2].startswith("paranoid (your defaults")
    monkeypatch.setattr(doctor, "checks", lambda *a: [("ok", "x", "y", "")])
    assert doctor.cli([]) == 0
    assert "high: the provider only" in capsys.readouterr().out


def test_policy_names_the_mode_behind_an_implied_option(m):
    m.policy(forbid=["--allow-github"])
    m.refused("--mode", "low", match=r"--allow-github is not allowed .*the security mode low \(--mode\) sets it")
    with pytest.raises(ValueError) as refused:
        m.run("--mode", "low", "--allow-github")  # chosen by the options: no word about the mode
    assert "security mode" not in str(refused.value)


def test_canary_files_per_mode(m):
    """lax only reports; low and medium are the launcher's own (credentials and home; a read reported, a token
    leaving stops the run); high and paranoid ask about (high) or stop at (paranoid) a read."""
    both = ["credentials", "home"]
    expected = {"lax": (both, "report", "report"), "low": (both, "report", "stop"),
                "medium": (both, "report", "stop"), "high": (["credentials", "home"], "ask", "stop"),
                "paranoid": (["credentials", "home"], "stop", "stop")}
    for mode, (categories, on_read, on_send) in expected.items():
        canaries = m.plan("--mode", mode)["canaries"]
        assert (canaries["categories"], canaries["on_read"], canaries["on_send"]) == (categories, on_read, on_send)
    # A narrower choice is taken; the strict modes keep their categories as a floor.
    assert m.plan("--mode", "high", "--canaries", "workspace")["canaries"]["categories"] == list(c.CATEGORIES)
    assert m.plan("--mode", "lax", "--no-canaries")["canaries"]["categories"] == []
    # A ruleset may be stricter than the mode, never laxer.
    rules = m.tmp / "canary-rules.json"
    rules.write_text(json.dumps({"version": 1, "canary": {"on_send": "block", "on_read": "report"}}))
    lax = m.plan("--mode", "lax", "--rules", str(rules))["canaries"]
    high = m.plan("--mode", "high", "--rules", str(rules))["canaries"]
    assert (lax["on_send"], high["on_send"], high["on_read"]) == ("block", "stop", "ask")


def test_the_run_records_its_mode_for_run_time_changes(m):
    """run.json (sandbox-ai net, approvals) and, for a strict mode, config.json (the supervisor) carry the mode,
    what it refuses at run time and its approval scope."""
    presets = [f"--allow-{p}" for p in s.PRESETS]
    run = m.run("--mode", "high")
    expected = {"name": "high", "origin": "--mode", "refuses": ["--allow", "--allow-host", "--allow-tcp", "--dns", "--allow-host-port", *presets],
                "approval_scope": "run"}
    assert run.run_record["mode"] == expected and run.config["mode"] == expected
    run = m.run("--mode", "paranoid", "--offline")
    assert run.config["mode"]["approval_scope"] == "once" and run.run_record["mode"]["name"] == "paranoid"
    run = m.run()  # medium: recorded, nothing refused; the supervisor's configuration is as before
    assert run.run_record["mode"] == {"name": "medium", "origin": "default", "refuses": [], "approval_scope": "always"}
    assert "mode" not in run.config
    assert "mode" not in m.run("--mode", "lax").config


# --- taint escalation in the strict modes -------------------------------------------------------------------

STATE = {"tainted": True, "since": "2026-11-04T10:00:00Z", "sources": 1,
         "source": {"layer": "proxy", "rule": "mode-hosts", "ruleset": "mode high", "what": "news.example"}}


def mode_rules(name, agent="claude"):
    a = s.parse_args([agent])
    document = MODES.ruleset(s, a, name)
    return s.RUNTIME.Ruleset([{"name": f"mode {name}", **document}], providers=s.PROVIDERS[agent])


def test_high_escalates_mcp_calls_once_a_host_tainted_the_run():
    rules = mode_rules("high")
    news = s.RUNTIME.Ruleset.request("news.example", "GET", "/", [["Host", "news.example"]])
    provider = s.RUNTIME.Ruleset.request("api.anthropic.com", "GET", "/", [["Host", "api.anthropic.com"]])
    assert rules.untrusted_http(news)["rule"] == "mode-hosts" and rules.untrusted_http(provider) is None
    assert rules.mcp("files", "write_file", {}, None)["decision"] == "allow"  # untainted: as before
    verdict = rules.mcp("files", "write_file", {}, STATE)
    assert verdict["decision"] == "ask" and "untrusted content from news.example" in verdict["reason"]
    assert rules.untrusted_tool("files", "read_file") is None and not rules.tool_active  # high: no hooks


def test_paranoid_escalates_the_agents_own_tools_too():
    rules = mode_rules("paranoid")
    assert rules.untrusted_tool("files", "read_file")["rule"] == "mode-mcp-results"
    assert rules.untrusted_agent_tool("claude", "WebSearch")["rule"] == "mode-agent-web"
    assert rules.agent_tool("claude", "Bash", {"command": "ls"}, None)["decision"] == "allow"
    for tool in ("Bash", "Write", "apply_patch", "WebFetch"):
        assert rules.agent_tool("codex", tool, {}, STATE)["decision"] == "ask"
    assert rules.agent_tool("claude", "Read", {"file_path": "/work/a"}, STATE)["decision"] == "allow"


def test_the_rules_for_tainted_runs_stay_out_of_the_shared_mcp_gateway(tmp_path, monkeypatch):
    """The server's own gateway knows no run's taint: those rules go to the run's relay, and a strict mode does not
    change the gateway configuration other agents share."""
    a = s.parse_args(["claude", "--mcp", "files"])
    a.rulesets = [{"name": "mode high", **s.RUNTIME.Ruleset.validated(MODES.ruleset(s, a, "high"), "mode high")}]
    assert s.mcp_gateway(a, "files") is None
    relay = s.mcp_relay_config(a, "files")
    assert [rule["id"] for rule in relay["rules"][0]["mcp"]] == ["mode-tainted-mcp"]
