"""--telemetry: what the agents report to their makers is always off, for every agent (since 0.1.95 there is no level
on). off (the default) sets the agents' own switches against usage statistics, error reports, feedback uploads,
session sharing, OpenTelemetry exports and update checks; strict also every other call that is not the model's. An old
on - an option, defaults.json, a project file, a preset - is ignored with one note, and nothing you pass (--env,
--secret-env, Codex's -c, its own config.toml) undoes the opt-outs."""
import json
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10: the same parser as a package
    import tomli as tomllib

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

s = fm.s
OTHERS = {"claude": ["codex", "opencode"], "codex": ["claude", "opencode"], "opencode": ["claude", "codex"]}
NOTE = "telemetry is always off since 0.1.95; 'on' is ignored"


@pytest.fixture(autouse=True)
def note_again(monkeypatch):
    """The note is said once per process: once per test here."""
    monkeypatch.setattr(s, "_telemetry_retired_noted", False)


def with_options(agents):
    return [x for agent in agents for x in ("--with", agent)]


def codex_config(run):
    return tomllib.loads(run.home_text.get(".codex/config.toml", ""))


def opencode_config(run):
    return json.loads(run.env.get("OPENCODE_CONFIG_CONTENT", "{}"))


def assert_level(run, agents, level):
    """RUN's sandbox has each of AGENTS' switches for LEVEL (off or strict), and none of a stricter one."""
    assert level in ("off", "strict")
    for agent in agents:
        for key, value in s.TELEMETRY_ENV[agent]["off"].items():
            assert run.env.get(key) == value, (agent, key)
        for key, value in s.TELEMETRY_ENV[agent]["strict"].items():
            assert run.env.get(key) == (value if level == "strict" else None), (agent, key)
    if "codex" in agents:
        config = codex_config(run)
        assert config["analytics"]["enabled"] is False and config["feedback"]["enabled"] is False
        assert config["otel"] == {"metrics_exporter": "none", "exporter": "none", "trace_exporter": "none"}
        assert config["check_for_update_on_startup"] is False
        assert (config.get("features", {}).get("plugins") is False) == (level == "strict")
    if "opencode" in agents:
        config = opencode_config(run)
        assert config.get("share") == "disabled" and config.get("autoupdate") is False


def test_off_is_the_default_for_every_agent(m):  # noqa: F811
    run = m.run()
    assert_level(run, [m.agent], "off")
    assert m.plan()["telemetry"]["level"] == "off" and m.plan()["telemetry"]["origin"] == "default"
    assert NOTE not in run.err


def test_strict(m):  # noqa: F811
    assert_level(m.run("--telemetry", "strict"), [m.agent], "strict")
    assert m.plan("--telemetry", "strict")["telemetry"]["origin"] == "--telemetry"


def test_there_is_no_level_on():
    assert s.TELEMETRY_LEVELS == ("off", "strict")
    action = next(x for x in s.build_parser()._actions if "--telemetry" in x.option_strings) \
        if hasattr(s, "build_parser") else None
    if action is not None:
        assert list(action.choices) == ["off", "strict"], "--help, completions and errors name off and strict"


@pytest.mark.parametrize("options", [["--telemetry", "on"], ["--telemetry=on"], ["-tm", "on"]])
def test_an_old_on_on_the_command_line_is_ignored_with_a_note(m, options):  # noqa: F811
    run = m.run(*options)
    assert_level(run, [m.agent], "off")
    assert run.err.count(NOTE) == 1
    plan = m.plan(*options)["telemetry"]
    assert plan["level"] == "off" and plan["origin"] == "default"


def test_an_unknown_level_is_still_refused(m):  # noqa: F811
    with pytest.raises(SystemExit):
        m.run("--telemetry", "maybe")
    assert "off" in m.capsys.readouterr().err


def test_the_agents_with_the_lead_get_it_too(m):  # noqa: F811
    run = m.run(*with_options(OTHERS[m.agent]))
    assert_level(run, ["claude", "codex", "opencode"], "off")
    plan = m.plan(*with_options(OTHERS[m.agent]), "--telemetry", "strict")["telemetry"]
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in plan["environment"]
    assert "features.plugins" in plan["codex_config"] and plan["opencode_config"] == s.OPENCODE_TELEMETRY_CONFIG


def test_your_defaults_file_chooses_and_the_option_wins(m):  # noqa: F811
    path = s.defaults_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "telemetry": "strict"}))
    assert m.plan()["telemetry"] == {**m.plan()["telemetry"], "level": "strict", "origin": str(path)}
    assert_level(m.run(), [m.agent], "strict")
    assert_level(m.run("--telemetry", "off"), [m.agent], "off")
    # An old on given at the start counts as not given: the file's strict holds.
    assert_level(m.run("--telemetry", "on"), [m.agent], "strict")
    path.write_text(json.dumps({"version": 1, "telemetry": "maybe"}))
    with pytest.raises(ValueError, match="telemetry is one of off, strict"):
        m.run()


def test_an_old_on_in_your_defaults_file_is_ignored_with_a_note(m):  # noqa: F811
    path = s.defaults_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "telemetry": "on"}))
    run = m.run()
    assert_level(run, [m.agent], "off")
    assert run.err.count(NOTE) == 1
    assert m.plan()["telemetry"]["origin"] == "default"


def test_the_security_modes(m):  # noqa: F811
    plan = m.plan("--mode", "paranoid")["telemetry"]
    assert plan["level"] == "strict" and plan["origin"] == "--mode paranoid"
    m.refused("--mode", "paranoid", "--telemetry", "off", match="--telemetry off widens the security mode paranoid")
    assert m.plan("--mode", "high")["telemetry"]["level"] == "off"
    # An old on never fails a start, not even in a strict mode: it is ignored (paranoid: strict, its floor).
    assert m.plan("--mode", "high", "--telemetry", "on")["telemetry"]["level"] == "off"
    assert m.plan("--mode", "paranoid", "--telemetry", "on")["telemetry"]["level"] == "strict"
    path = s.defaults_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "telemetry": "on"}))
    assert m.plan("--mode", "paranoid")["telemetry"]["level"] == "strict"
    assert m.plan("--mode", "lax")["telemetry"]["level"] == "off"


@pytest.mark.parametrize("options", [["--telemetry", "on"], ["--telemetry=on"], ["-tm", "on"]])
def test_an_old_on_in_a_project_file_is_ignored_with_a_note(m, options):  # noqa: F811
    m.project_file({"options": options})
    run = m.run()
    assert_level(run, [m.agent], "off")
    assert run.err.count(NOTE) == 1


def test_a_project_file_may_make_it_stricter(m):  # noqa: F811
    m.project_file({"options": ["--telemetry", "strict"]})
    assert m.plan()["telemetry"]["level"] == "strict"


def test_an_old_on_in_a_preset_is_ignored_with_a_note(m):  # noqa: F811
    presets = s.load_presets_module()
    directory = Path(presets.user_dir()) if hasattr(presets, "user_dir") else None
    if directory is None:
        pytest.skip("no user preset directory in this presets module")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "old-on.json").write_text(json.dumps({
        "format": "sandbox-ai preset", "version": 1, "id": "old-on", "title": "Old on", "description": "x",
        "options": ["--telemetry", "on"]}))
    run = m.run("--preset", "old-on")
    assert_level(run, [m.agent], "off")
    assert run.err.count(NOTE) == 1


def test_a_preset_that_is_not_yours_with_an_old_on_is_harmless():
    presets = s.load_presets_module()
    assert presets.own_values_in(["--telemetry", "on"]) == [], "ignored by the launcher, so nothing to refuse"
    assert presets.own_values_in(["--llm-traffic", "all"]) == ["--llm-traffic all"]


@pytest.mark.parametrize("name", ["DISABLE_TELEMETRY", "DO_NOT_TRACK", "OTEL_SDK_DISABLED", "OTEL_METRICS_EXPORTER"])
def test_what_you_pass_yourself_cannot_turn_it_on(m, name):  # noqa: F811
    m.monkeypatch.setenv(name, "0")
    if name not in s.TELEMETRY_ENV[m.agent]["off"]:
        pytest.skip(f"{name} is not one of {m.agent}'s switches")
    run = m.run("--env", name)
    assert run.env[name] == s.TELEMETRY_ENV[m.agent]["off"][name]
    assert f"{name} is set by sandbox-ai, not passed (telemetry is always off)" in run.err
    assert name not in run.config["secret_names"], "the opt-out's value is not redacted as a secret"


def test_claude_codes_opentelemetry_export_cannot_be_turned_on(m):  # noqa: F811
    m.monkeypatch.setenv("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
    run = m.run("--with", "claude") if m.agent != "claude" else m.run("--env", "CLAUDE_CODE_ENABLE_TELEMETRY")
    assert run.env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "0"
    for name in ("OTEL_SDK_DISABLED", "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER"):
        assert run.env[name] in ("true", "none"), name


def test_a_secret_cannot_turn_it_on(m, monkeypatch):  # noqa: F811
    monkeypatch.setattr(s, "secret_environment", lambda a: {"DO_NOT_TRACK": "0"} if a.secret_env else {})
    run = m.run("--secret-env", "DO_NOT_TRACK")
    assert run.env["DO_NOT_TRACK"] == "1"


def test_opencodes_config_from_env_cannot_turn_sharing_on(m):  # noqa: F811
    m.monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"share": "auto", "autoupdate": True}))
    run = m.run("--with", "opencode") if m.agent != "opencode" else m.run("--env", "OPENCODE_CONFIG_CONTENT")
    assert opencode_config(run)["share"] == "disabled" and opencode_config(run)["autoupdate"] is False


# --- Codex: its config.toml forced, and -c overrides on the lead's command line -------------------------------

KEYS = s.codex_telemetry_keys("off")
OFF = {"check_for_update_on_startup": False, "analytics": {"enabled": False}, "feedback": {"enabled": False},
       "otel": {"metrics_exporter": "none", "exporter": "none", "trace_exporter": "none"}}


def merged(text, keys=KEYS):
    out, replaced = s.codex_config_with(text, keys, tomllib)
    return out, tomllib.loads(out), replaced


def test_codex_an_empty_file():
    out, doc, replaced = merged("")
    assert doc == OFF and replaced == []


def test_codex_a_file_with_tables_of_its_own():
    text = ('model = "o4"\n\n[analytics]  # mine\nfoo = 1\n\n[otel]\nexporter = "none"\n\n'
            '[mcp_servers.docs]\ncommand = "x"\nwhen = 1979-05-27T07:32:00Z\n')
    out, doc, replaced = merged(text)
    assert doc["analytics"] == {"foo": 1, "enabled": False} and doc["otel"] == OFF["otel"]
    assert doc["model"] == "o4" and doc["mcp_servers"]["docs"]["command"] == "x" and replaced == []
    assert str(doc["mcp_servers"]["docs"]["when"]).startswith("1979-05-27")
    assert out.endswith("[feedback]\nenabled = false\n") and "foo = 1" in out


def test_codex_what_your_file_turns_on_is_replaced_and_said():
    text = ('check_for_update_on_startup = true\nanalytics.enabled = true\nmodel = "o4"\n[otel]\n'
            'metrics_exporter = "statsig"  # mine\nexporter = { otlp-http = { endpoint = "https://x" } }\n'
            '[feedback]\n"enabled" = true\n')
    out, doc, replaced = merged(text)
    assert {key: doc[key] for key in OFF} == OFF and doc["model"] == "o4"
    assert sorted(replaced, key=lambda item: item[0]) == [
        ("analytics.enabled", True), ("check_for_update_on_startup", True), ("feedback.enabled", True),
        ("otel.exporter", {"otlp-http": {"endpoint": "https://x"}}), ("otel.metrics_exporter", "statsig")]
    assert merged(out)[2] == [] and merged(out)[0] == out, "a second start changes nothing"


@pytest.mark.parametrize("text", [
    'analytics = { foo = 1 }\n',                      # an inline table cannot be added to
    'analytics.foo = 1\n',                            # a table made by dotted keys has no header
    '[analytics]\nfoo = 1\n[analytics.sub]\nx = 1\n[profiles]\nnote = """\n[analytics]\n"""\n',  # two headers
    'analytics = 3\n',                                # not a table
    '[otel.exporter.otlp-http]\nendpoint = "https://x"\n',  # a key set as a table of its own: no line to rewrite
    '[otel]\nexporter = [\n"none"]\n',                   # a value over several lines
])
def test_codex_a_file_that_cannot_be_changed_safely(text):
    with pytest.raises(ValueError):
        s.codex_config_with(text, KEYS, tomllib)


def test_codex_left_as_it_is_with_a_warning(tmp_path, capsys):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text("analytics = { foo = 1 }\n")
    s.codex_telemetry_defaults(home, "off")
    assert (home / ".codex/config.toml").read_text() == "analytics = { foo = 1 }\n"
    assert "Codex's ~/.codex/config.toml is left as it is" in capsys.readouterr().err
    (home / ".codex/config.toml").write_text("not = [toml\n")
    s.codex_telemetry_defaults(home, "strict")
    assert (home / ".codex/config.toml").read_text() == "not = [toml\n"
    (home / ".codex/config.toml").write_text("analytics.enabled = true\n")
    s.codex_telemetry_defaults(home, "off")
    assert "analytics.enabled = true in ~/.codex/config.toml is replaced (telemetry is always off)" \
        in capsys.readouterr().err
    assert tomllib.loads((home / ".codex/config.toml").read_text()) == OFF
    before = (home / ".codex/config.toml").read_text()
    s.codex_telemetry_defaults(home, "off")
    assert (home / ".codex/config.toml").read_text() == before, "a second start changes nothing"
    assert "replaced" not in capsys.readouterr().err


def test_codex_a_symlinked_config_is_refused(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (tmp_path / "elsewhere.toml").write_text("")
    (home / ".codex/config.toml").symlink_to(tmp_path / "elsewhere.toml")
    with pytest.raises(ValueError):
        s.codex_telemetry_defaults(home, "off")
    assert (tmp_path / "elsewhere.toml").read_text() == ""


def test_codex_gets_the_keys_as_overrides(m):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex's command line")
    command = m.run().config["command"]
    for value in ("check_for_update_on_startup=false", "analytics.enabled=false", "feedback.enabled=false",
                  'otel.metrics_exporter="none"', 'otel.exporter="none"', 'otel.trace_exporter="none"'):
        assert command[command.index(value) - 1] == "-c", value
    assert "features.plugins=false" not in command
    assert "features.plugins=false" in m.run("--telemetry", "strict").config["command"]


@pytest.mark.parametrize("args", [["-c", "analytics.enabled=true"], ["--config", "otel.exporter=otlp-http"],
                                  ["-ccheck_for_update_on_startup=true"], ["--config=feedback={enabled=true}"]])
def test_codex_arguments_cannot_turn_it_on(m, args):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex's arguments")
    with pytest.raises(ValueError, match="Codex's telemetry is always off"):
        m.run(agent_args=args)


def test_codex_other_overrides_pass(m):  # noqa: F811
    if m.agent != "codex":
        pytest.skip("Codex's arguments")
    assert "model=o4" in m.run(agent_args=["-c", "model=o4"]).config["command"]


def test_the_names_are_the_agents_own():
    """The switches as the sources name them (README, Privacy: telemetry)."""
    assert set(s.TELEMETRY_ENV["claude"]["off"]) - set(s.TELEMETRY_TOOLS) == {
        "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING", "DISABLE_BUG_COMMAND", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
        "DISABLE_AUTOUPDATER", "CLAUDE_CODE_ENABLE_TELEMETRY",
        # its beta tracing exports to BETA_TRACING_ENDPOINT whatever CLAUDE_CODE_ENABLE_TELEMETRY says (2.1.322)
        "ENABLE_BETA_TRACING_DETAILED"}
    assert [(t, k) for t, k, _, _ in s.codex_telemetry_keys("strict")] == [
        (None, "check_for_update_on_startup"), ("analytics", "enabled"), ("feedback", "enabled"),
        ("otel", "metrics_exporter"), ("otel", "exporter"), ("otel", "trace_exporter"), ("features", "plugins")]
    assert s.codex_telemetry_keys("on") == s.codex_telemetry_keys("off")
    readme = (Path(s.__file__).resolve().parents[1] / "README.md").read_text()
    assert s.TELEMETRY_TOOLS["DO_NOT_TRACK"] == "1" and s.TELEMETRY_TOOLS["OPENSPEC_TELEMETRY"] == "0"
    for name in [*s.TELEMETRY_ENV["claude"]["off"], *s.TELEMETRY_ENV["opencode"]["off"], "analytics.enabled"]:
        assert name in readme, name


# --- the proxy refuses telemetry to the hosts the agents need anyway ------------------------------------------

def proxy_says(run, host, path, method="POST"):
    rt = s.load_runtime_module() if hasattr(s, "load_runtime_module") else s.RUNTIME
    rules = rt.Ruleset([source for source in run.a.rulesets])
    return rules.http(rt.Ruleset.request(host, method, path, []))["decision"]


def test_the_proxy_refuses_what_an_agent_sends_anyway(m):  # noqa: F811
    run = m.run(*with_options(OTHERS[m.agent]))
    assert proxy_says(run, "api.anthropic.com", "/api/event_logging/batch") == "deny"
    assert proxy_says(run, "chatgpt.com", "/backend-api/codex/analytics-events/events") == "deny"
    assert proxy_says(run, "api.anthropic.com", "/v1/messages") == "allow", "the model's calls pass"
    assert proxy_says(run, "chatgpt.com", "/backend-api/codex/responses") == "allow"
    old = m.run(*with_options(OTHERS[m.agent]), "--telemetry", "on")  # an old on: refused all the same
    assert proxy_says(old, "api.anthropic.com", "/api/event_logging/batch") == "deny"
    assert proxy_says(old, "chatgpt.com", "/backend-api/codex/analytics-events/events") == "deny"


def test_only_for_the_agents_of_the_run_and_never_against_no_inspect(m):  # noqa: F811
    names = [rule["id"] for source in m.run().a.rulesets if source["name"] == "telemetry" for rule in source["http"]]
    assert names == [f"telemetry-{m.agent}-1"] if m.agent in s.TELEMETRY_PATHS else names == []
    if m.agent in s.TELEMETRY_PATHS:
        host = s.TELEMETRY_PATHS[m.agent][0][0]
        run = m.run("--no-inspect", host)  # no rule the proxy could not hold: the start is not refused
        assert "telemetry" not in [source["name"] for source in run.a.rulesets]
