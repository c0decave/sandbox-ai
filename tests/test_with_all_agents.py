"""--with for every agent (0.1.98): Hermes, DSH, Qwen Code, Goose, OpenHands, nanobot, Letta Code, OpenCode 2 and Pi
join a start as companions too, in the team image with their layers (team_image.py), each logged in and under the
same rules as in its own image: its managed configuration (MCP servers, limits, the network note), its variables,
its telemetry guard and the proxy's telemetry rules, the rules' refusals. The lead calls each from the shell in its
one-shot mode. Each of them can lead with companions as well."""
import json
from pathlib import Path
import shlex
import sys

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m, hm, qm  # noqa: F401  (fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

ROOT = Path(__file__).resolve().parents[1]
s = fm.s
NINE = ("hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "opencode2", "pi")
MANAGED = {"hermes": "/etc/hermes", "dsh": "/etc/dsh", "qwen": "/etc/qwen-code", "goose": "/etc/goose",
           "openhands": "/etc/openhands", "nanobot": "/etc/nanobot", "letta": "/etc/letta",
           "opencode2": "/etc/opencode2", "pi": "/etc/pi"}


def ns(*argv):
    return s.parse_args(list(argv))


# --- the parser and the image ----------------------------------------------------------------------------------

def test_every_agent_can_be_a_companion():
    assert s.NOT_IN_TEAM == ()
    for agent in NINE:
        assert s.companions(ns("claude", "--with", agent)) == [agent]
    assert s.COMPANION_AGENTS == tuple(sorted(s.AUTH_TARGETS))


def test_the_image_holds_the_runs_agents():
    assert ns("claude", "--with", "codex").image == s.TEAM_IMAGE
    assert ns("claude", "--with", "hermes").image == "localhost/team-hermes-sandbox:latest"
    assert ns("hermes", "--with", "claude").image == "localhost/team-hermes-sandbox:latest"
    assert ns("qwen", "--with", "pi", "--with", "codex").image == "localhost/team-pi-qwen-sandbox:latest"
    assert ns("pi").image == "localhost/pi-sandbox:latest", "no --with: its own image"
    assert ns("claude", "--with", "qwen", "--image", "x/y:1").image == "x/y:1"


def test_a_missing_team_image_is_built_with_its_layers():
    a = ns("claude", "--with", "qwen", "--with", "hermes")
    assert s.image_fix(a, "claude", a.image) == "sandbox-ai build team --with hermes,qwen"
    assert s.image_fix(a, "claude", s.TEAM_IMAGE) == "sandbox-ai build team"


def test_team_image_for_happy_sad_edge():
    assert s.team_image_for(["claude", "dsh"]) == "localhost/team-dsh-sandbox:latest"
    assert s.team_image_for(["codex"]) == s.TEAM_IMAGE  # edge: the base alone
    with pytest.raises(ValueError):
        s.team_image_for(["gemini"])


def test_team_image_agents_happy_sad_edge():
    assert s.team_image_agents("localhost/team-goose-pi-sandbox:latest") == ["claude", "codex", "opencode", "goose",
                                                                              "pi"]
    assert s.team_image_agents("localhost/claude-sandbox:latest") is None
    assert s.team_image_agents(s.TEAM_IMAGE) == ["claude", "codex", "opencode"]


def test_pick_team_image_happy_sad_edge():
    wanted = "localhost/team-hermes-sandbox:latest"
    bigger = "localhost/team-hermes-qwen-sandbox:latest"
    biggest = "localhost/team-goose-hermes-qwen-sandbox:latest"
    assert s.pick_team_image(wanted, [wanted, bigger]) == wanted  # there: taken
    assert s.pick_team_image(wanted, [biggest, bigger, "localhost/team-pi-sandbox:latest"]) == bigger  # the smallest
    assert s.pick_team_image(wanted, ["localhost/team-qwen-sandbox:latest", s.TEAM_IMAGE]) == wanted  # none: built
    assert s.pick_team_image(s.TEAM_IMAGE, [bigger]) == bigger  # the base alone: any team image holds it
    assert s.pick_team_image("x/y:1", [bigger]) == "x/y:1"  # no team image: as it is


def test_image_agents_happy_sad_edge():
    assert s.image_agents(ns("claude", "--with", "hermes")) == ["claude", "hermes", "codex", "opencode"]
    assert s.image_agents(ns("pi")) == ["pi"]
    assert s.image_agents(ns("claude", "--with", "hermes", "--backend", "bwrap")) == ["claude", "hermes"]


def test_the_policy_takes_a_team_image_as_the_default_of_with():
    a = ns("claude", "--with", "hermes")
    s.check_policy(a, {"forbid": ["--image"]})
    a.image = "localhost/team-hermes-qwen-sandbox:latest"  # a bigger one the start picked
    s.check_policy(a, {"forbid": ["--image"]})
    with pytest.raises(ValueError):
        s.check_policy(ns("claude", "--with", "hermes", "--image", "x/y:1"), {"forbid": ["--image"]})


# --- what a companion gets ----------------------------------------------------------------------------------------

def test_companion_view_happy_sad_edge():
    a = ns("claude", "--with", "qwen", "--model", "opus", "--effort", "high", "--", "-p", "hi")
    view = s.companion_view(a, "qwen")
    assert view.agent == "qwen" and view.companions == [] and view.agent_args == []
    assert view.model is None and view.effort is None and view.companion_of == "claude"
    assert a.agent == "claude" and a.model == "opus" and a.agent_args == ["-p", "hi"], "the start is unchanged"
    assert view.allow_host is a.allow_host, "what a companion's features open is the run's"
    with pytest.raises(ValueError):
        s.companion_view(a, "pi")  # not one of its companions


@pytest.mark.parametrize("agent", NINE)
def test_a_companion_gets_its_managed_configuration_variables_and_guard(m, agent):  # noqa: F811
    if m.agent == "opencode" and agent == "opencode2":
        m.refused("--with", agent, match="OpenCode 2 and OpenCode")  # (one of them per sandbox)
        return
    run = m.run("--with", agent)
    assert run.env["SANDBOX_AI_COMPANIONS"] == agent
    files = run.hermes if agent == "hermes" else run.managed[agent]
    assert files, f"{agent}'s managed configuration in the session"
    assert MANAGED[agent] in run.joined, f"{agent}'s managed configuration mounted"
    view = s.companion_view(run.a, agent)
    own = s.own_environment(view)
    for name, value in own.items():
        if name not in s.telemetry_environment(run.a):
            assert run.env.get(name) == value, (agent, name)
    if run.proxy is not None and agent in s.UNSWITCHABLE_TELEMETRY:
        refused = [rule["id"] for source in run.proxy["rules"] if source["name"] == "telemetry"
                   for rule in source["http"] if rule["decision"] == "deny"]
        assert any(rule.startswith(f"telemetry-{agent}-") for rule in refused), agent
    if agent in s.TELEMETRY_GUARD.AGENTS:
        assert agent in s.telemetry_guarded(run.a)
    assert set(s.PROVIDERS[agent]) <= set(s.provider_hosts(run.a))


def test_hermes_beside_the_lead_answers_its_first_question_before_the_lead_starts(m):  # noqa: F811
    run = m.run("--with", "hermes")
    command = run.config["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    hermes = s.load_hermes_module()
    assert f"{hermes.MANAGED_DIR}/{hermes.TELEMETRY_ANSWER}" in command[2]
    assert command[2].endswith('exec "$0" "$@"')
    assert s.hermes_guarded(run.a)
    if m.backend == "podman":
        assert hermes.SITE_PACKAGES in run.joined, "Hermes' telemetry guard in its site-packages"


def test_letta_beside_the_lead_gets_the_runs_mcp_servers_laid_in(m):  # noqa: F811
    plain = m.run("--with", "letta")
    letta = s.load_letta_module()
    # (a Codex lead's own command may come after its device sign-in - /bin/sh -c ... - never after Letta's script here)
    assert str(letta.START) not in " ".join(map(str, plain.config["command"])), "nothing to lay in without --mcp"
    assert (plain.config["command"][:2] == ["/bin/sh", "-c"]) == (m.agent == "codex")
    a = ns(m.agent, "--with", "letta")
    a.mcp = ["github"]
    lines = s.companion_prelude(a)
    assert lines == [f"python3 -I {letta.START} /bin/true || true"]


def test_qwen_beside_the_lead_has_the_runs_mcp_servers_in_its_system_settings():
    a = ns("claude", "--with", "qwen")
    a.mcp = ["github"]
    files = s.companion_files(a)
    settings = json.loads(files["qwen/settings.json"])
    assert list(settings["mcpServers"]) == ["github"]
    assert settings["mcpServers"]["github"]["command"] == "python3"


def test_companion_prelude_happy_sad_edge():
    assert s.companion_prelude(ns("claude", "--with", "codex")) == []
    hermes = s.load_hermes_module()
    assert hermes.TELEMETRY_ANSWER in s.companion_prelude(ns("claude", "--with", "hermes"))[0]
    assert s.companion_prelude(ns("hermes", "--with", "claude")) == [], "the lead's own start answers it already"
    assert s.companion_prelude(ns("claude", "--with", "hermes", "--binary", "bash")) == []


def test_companion_files_and_mounts_happy_sad_edge(tmp_path):
    a = ns("claude", "--with", "goose", "--with", "pi")
    files = s.companion_files(a)
    assert any(name.startswith("goose/") for name in files) and any(name.startswith("pi/") for name in files)
    assert sorted(target for _, target in s.companion_managed_mounts(a, tmp_path)) == ["/etc/goose", "/etc/pi"]
    trio = ns("claude", "--with", "codex")
    assert s.companion_files(trio) == {} and s.companion_managed_mounts(trio, tmp_path) == []
    probe = ns("claude", "--with", "goose", "--probe")
    assert s.companion_files(probe) == {}


def test_a_logged_in_companion_brings_its_login_and_one_without_a_login_none(m):  # noqa: F811
    logins = m.tmp / "logins"
    logins.mkdir()
    sources = {}
    for agent in ("hermes", "qwen", "goose"):
        path = logins / f"{agent}.json"
        path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "agent": agent}))
        path.chmod(0o600)
        sources[agent] = path
    original = s.auth_sources
    m.monkeypatch.setattr(s, "auth_sources", lambda name: [(sources[name], s.AUTH_TARGETS[name])]
                          if name in sources else original(name))
    run = m.run("--with", "hermes", "--with", "qwen", "--with", "goose")
    assert s.AUTH_TARGETS["hermes"] in run.home_files and s.AUTH_TARGETS["qwen"] in run.home_files
    assert s.AUTH_TARGETS["goose"] not in run.home_files, "Goose's secrets.yaml (every key of yours) never"
    assert "--with goose: Goose has no login" in run.err


# --- the same rules ---------------------------------------------------------------------------------------------

HOOKED = {"qwen": "qwen/settings.json", "hermes": "hermes/config.yaml", "pi": "pi/sandbox-ai.js",
          "opencode2": "opencode2/sandbox-ai-hooks/server.js", "dsh": "dsh/sandbox-ai-hooks.mjs",
          "letta": "letta/sandbox-ai-start.json", "goose": "goose/sandbox-ai-hooks/hooks/hooks.json"}


def test_tool_rules_are_refused_for_a_companion_that_cannot_enforce_them(m):  # noqa: F811
    rules = m.private_file("rules.json", json.dumps({"version": 1, "defaults": {"tool": "ask"}}))
    m.refused("--rules", str(rules), "--with", "openhands", match="cannot be enforced for OpenHands")
    m.refused("--rules", str(rules), "--with", "nanobot", match="cannot be enforced for nanobot")


@pytest.mark.parametrize("agent", sorted(HOOKED))
def test_tool_rules_hold_for_a_companion_through_its_own_hooks(m, agent):  # noqa: F811
    """The rules for the agents' own tools: a companion's rule hook in its own configuration (as a start of it lays
    it), naming that agent to the supervisor; the lead's kit hooks stay the lead's."""
    if m.agent == "opencode" and agent == "opencode2":
        return
    rules = m.private_file("rules.json", json.dumps({"version": 1, "defaults": {"tool": "ask"}}))
    a = ns(m.agent, "--rules", str(rules), "--with", agent)
    files = s.companion_files(a)
    path = HOOKED[agent]
    assert f"--internal-hook {agent}" in files[path] or f'"--internal-hook", "{agent}"' in files[path], path
    assert f"--internal-hook {m.agent}" not in files[path]
    view = s.companion_view(a, agent)
    assert view.kit_hooks == [] and view.plugin_hooks == []
    hook_files = s.hook_files(a)
    assert not any(name.startswith(agent + "/") for name in hook_files), "not a Claude/Codex/OpenCode hook file"
    if agent == "letta":
        assert s.companion_prelude(a)[-1].endswith("/bin/true || exit 1"), "never without its hooks"


def test_a_companions_telemetry_is_never_opened(m):  # noqa: F811
    hermes = s.load_hermes_module()
    host = hermes.TELEMETRY_HOSTS[0].lstrip("*.")
    m.refused("--with", "hermes", "--allow-host", host, match="Hermes' telemetry is always off")


def test_a_companions_features_are_its_own_switches(m):  # noqa: F811
    plan = m.plan("--with", "qwen", "--qwen-enable", "model-catalog") if "model-catalog" in \
        s.load_qwen_module().FEATURES else None
    if plan is not None:
        assert plan["qwen"]["enabled"][0]["feature"] == "model-catalog"
    m.refused("--qwen-enable", next(iter(s.load_qwen_module().FEATURES)), match="is for Qwen Code")


def test_opencode_and_opencode2_share_their_folders_so_one_per_sandbox(m):  # noqa: F811
    if m.agent == "opencode":
        m.refused("--with", "opencode2", match="OpenCode 2 and OpenCode")
    else:
        m.refused("--with", "opencode", "--with", "opencode2", match="OpenCode 2 and OpenCode")


def test_the_dry_run_names_each_companions_plan(m):  # noqa: F811
    plan = m.plan("--with", "hermes", "--with", "goose")
    assert plan["companions"] == ["hermes", "goose"]
    assert plan["hermes"]["version"] == s.load_hermes_module().VERSION and plan["goose"] is not None
    assert plan["qwen"] is None
    for agent in NINE:
        if m.agent == "opencode" and agent == "opencode2":
            continue
        plan = m.plan("--with", agent)
        assert plan[agent]["version"] and plan["companions"] == [agent], agent
        if m.backend == "podman":
            assert plan["image"] == f"localhost/team-{agent}-sandbox:latest"


def test_the_agents_environments_never_contradict_each_other():
    """Two agents of one sandbox that set the same variable set it alike (OpenCode and OpenCode 2 never share one)."""
    seen = {}
    for agent in sorted(s.AUTH_TARGETS):
        if agent in ("claude", "codex", "opencode"):
            continue
        a = ns("claude", "--with", agent)
        for name, value in s.own_environment(s.companion_view(a, agent)).items():
            if name in seen and seen[name][1] != value:
                pytest.fail(f"{name}: {seen[name][0]} sets {seen[name][1]!r}, {agent} {value!r}")
            seen.setdefault(name, (agent, value))


# --- a lead of the nine with companions ---------------------------------------------------------------------------

def test_hermes_leads_with_claude_beside_it(hm):  # noqa: F811
    run = hm.run("--with", "claude")
    assert run.env["SANDBOX_AI_COMPANIONS"] == "claude"
    assert run.hermes and (hm.backend == "bwrap" or run.a.image == "localhost/team-hermes-sandbox:latest")


def test_qwen_leads_with_hermes_beside_it(qm):  # noqa: F811
    run = qm.run("--with", "hermes")
    assert run.managed["qwen"] and run.hermes
    assert run.env["SANDBOX_AI_COMPANIONS"] == "hermes"


# --- what the lead is told -----------------------------------------------------------------------------------------

def test_every_agents_skill_names_each_companions_one_shot_command():
    for skill in sorted((ROOT / "sandbox/builtin").glob("*/skills/sandbox-ai/SKILL.md")):
        text = " ".join(skill.read_text().split())
        for agent, command in s.COMPANION_COMMANDS.items():
            assert f"`{command}`" in text, (skill, agent)


@pytest.mark.parametrize("agent", sorted(s.COMPANION_COMMANDS))
def test_the_companion_commands_are_the_helpers_one_shot_runs(agent):
    """What the lead types is what a helper runs for a task (agent_mcp.command), without the helper's own flags."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_mcp_for_with", ROOT / "sandbox/agent_mcp.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    words = shlex.split(s.COMPANION_COMMANDS[agent].replace("TASK", "do it"))
    ran = helper.command(agent, "do it")
    if agent == "dsh":
        assert words[-3:] == ["dsh", "headless", "-"] and ran[:2] == ["dsh", "headless"]
        return
    if agent == "nanobot":
        assert words[:3] == ran[:2] + ["--no-markdown"] and words[-1] == "--message=do it"
        return
    assert words[0] == ran[0] and words[-1] in (ran[-1], "--", "do it")
    assert set(words) <= set(ran) | {"--output-format", "json"}, (words, ran)


# --- end to end: ./sandbox-ai with the fake Podman (tests/live_fakes.py) ------------------------------------------

@pytest.fixture
def host(tmp_path):
    import live_fakes
    found = live_fakes.Host(tmp_path)
    found.add_image("localhost/claude-sandbox:latest", "localhost/hermes-sandbox:latest")
    return found


def test_a_missing_team_image_names_its_build(host):
    done = host.run("claude", "--with", "hermes", "--dry-run", env={"SANDBOX_AI_NO_AUTO_BUILD": "1"}, timeout=180)
    assert done.returncode != 0, done.stdout[-500:]
    assert "localhost/team-hermes-sandbox:latest" in done.stderr
    assert "sandbox-ai build team --with hermes" in done.stderr, done.stderr[-2000:]


def test_a_built_team_image_that_holds_more_agents_is_taken(host):
    host.add_image("localhost/team-goose-hermes-qwen-sandbox:latest", "localhost/team-hermes-qwen-sandbox:latest")
    done = host.run("claude", "--with", "hermes", "--dry-run", env={"SANDBOX_AI_NO_AUTO_BUILD": "1"}, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    plan = json.loads(done.stdout)
    assert plan["image"] == "localhost/team-hermes-qwen-sandbox:latest", "the smallest that holds them"
    assert "taking localhost/team-hermes-qwen-sandbox:latest" in done.stderr
    # Its other agents' telemetry is off as well: they can be typed in its shell.
    assert "qwen" in plan["telemetry"]["guarded"] and "hermes" in plan["companions"]


def test_a_start_builds_the_team_image_it_needs_and_gets_as_far_as_its_pod(host):
    done = host.run("hermes", "--with", "claude", "--no-tmux", timeout=300)
    stopped = host.stopped_at()
    assert stopped is not None and "create" in stopped, (done.returncode, done.stderr[-3000:])
    assert "localhost/team-hermes-sandbox:latest" in stopped
    tags = [call[call.index("--tag") + 1] for call in host.builds()]
    assert tags == ["localhost/team-sandbox:latest", "localhost/team-hermes-sandbox:latest"], tags
    assert "sandbox-ai build team --with hermes" in done.stderr


def test_a_companions_configuration_reaches_the_sandbox(host):
    """A whole start (the fake Podman runs the sandbox for a moment): Qwen Code's system settings and Hermes'
    configuration are in the session, mounted read-only where their images have them."""
    host.add_image("localhost/team-hermes-qwen-sandbox:latest")
    keep = host.directory / "kept"
    done = host.run("claude", "--with", "qwen", "--with", "hermes", "--no-tmux", timeout=300,
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(keep)})
    session = keep / "session"
    assert session.is_dir(), done.stderr[-3000:]
    assert (session / "qwen/settings.json").is_file() and (session / "hermes/config.yaml").is_file()
    config = json.loads((session / "config.json").read_text())
    assert config["env"]["SANDBOX_AI_COMPANIONS"] == "qwen hermes"
    assert config["command"][:2] == ["/bin/sh", "-c"] and config["command"][3] == "claude"
    calls = " ".join(" ".join(call) for call in host.calls())
    for target in ("dst=/etc/qwen-code,ro", "dst=/etc/hermes,ro"):
        assert target in calls, target


def test_gc_keeps_the_team_image_a_kept_start_needs(tmp_path, monkeypatch):
    """sandbox-ai gc --images: a kept start with --with wants its team image - the one with its agents' layers."""
    import importlib.util
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for name, agent, options in (("one", "claude", ["--with", "hermes", "--with=qwen"]),
                                 ("two", "pi", ["--with", "codex"]), ("three", "codex", [])):
        folder = tmp_path / "sandbox-ai/sessions" / name
        folder.mkdir(parents=True)
        (folder / "session.json").write_text(json.dumps({"agent": agent, "options": options}))
    spec = importlib.util.spec_from_file_location("starts_for_with_tests", ROOT / "sandbox/starts.py")
    starts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(starts)
    wanted = starts.images_wanted()
    assert {"localhost/team-hermes-qwen-sandbox:latest", "localhost/team-pi-sandbox:latest",
            "localhost/codex-sandbox:latest"} <= wanted
    assert "localhost/team-sandbox:latest" not in wanted


def test_the_other_agents_of_a_team_image_keep_their_telemetry_refused_but_not_their_features_gates():
    """claude --with codex in a team image that holds Pi and Goose too (the start took a bigger one): their telemetry
    hosts are refused as in every run of their image - but not the gists or the model catalog their features gate,
    which Claude Code and Codex may use."""
    a = ns("claude", "--with", "codex", "--no-tmux")
    a.image = "localhost/team-goose-pi-sandbox:latest"
    a.proxy = True
    ids = [rule["id"] for rule in (s.telemetry_rules(a) or {}).get("http", [])] if s.proxy_enabled(a) else []
    if not ids:
        pytest.skip("no proxy in this setup")
    assert any(rule.startswith("telemetry-pi-") for rule in ids) and any(r.startswith("telemetry-goose-") for r in ids)
    assert "pi-share" not in ids and "goose-model-catalog" not in ids
    with_pi = ns("claude", "--with", "pi")
    assert "pi-share" in [rule["id"] for rule in (s.telemetry_rules(with_pi) or {}).get("http", [])], \
        "Pi beside the lead: its own rules"


@pytest.mark.parametrize("agent", ["qwen", "goose", "letta"])
def test_a_start_with_tool_rules_installs_a_companions_hooks_end_to_end(host, agent):
    """Through ./sandbox-ai: claude with rules and an agent --with gets as far as its sandbox, the companion's hooks in
    its configuration, mounted read-only where it reads it; OpenHands --with is refused before anything starts."""
    rules = host.directory / "rules.json"
    rules.write_text(json.dumps({"version": 1, "tool": [{"id": "no-curl", "tool": "Bash", "args": {
        "command": {"pattern": r".*\bcurl\b.*"}}, "decision": "deny"}]}))
    rules.chmod(0o600)
    host.add_image(f"localhost/team-{agent}-sandbox:latest")
    kept = host.directory / "kept"
    done = host.run("claude", "--rules", str(rules), "--with", agent, "--no-tmux", "--no-keep", timeout=300,
                    env={"FAKE_PODMAN_RUN": "0", "FAKE_PODMAN_KEEP": str(kept)})
    session = kept / "session"
    assert session.is_dir(), (done.returncode, done.stderr[-2000:])
    text = (session / HOOKED[agent]).read_text()
    assert f"--internal-hook {agent}" in text or f'"--internal-hook", "{agent}"' in text
    calls = " ".join(" ".join(call) for call in host.calls())
    assert f"dst={MANAGED[agent]},ro" in calls


def test_a_companion_without_a_hook_layer_is_refused_under_tool_rules_end_to_end(host):
    rules = host.directory / "rules.json"
    rules.write_text(json.dumps({"version": 1, "defaults": {"tool": "ask"}}))
    rules.chmod(0o600)
    host.add_image("localhost/team-openhands-sandbox:latest")
    done = host.run("claude", "--rules", str(rules), "--with", "openhands", "--no-tmux", timeout=180)
    assert done.returncode == 1 and "cannot be enforced for OpenHands" in done.stderr, done.stderr[-1500:]
    assert host.stopped_at() is None
