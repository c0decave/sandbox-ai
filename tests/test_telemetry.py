"""--telemetry: what Claude Code, Codex and OpenCode report to their makers is off unless you turn it on. off (the
default) sets the agents' own switches against usage statistics, error reports, feedback uploads, session sharing
and update checks; strict also every other call that is not the model's; on leaves the agents' settings alone."""
import json
from pathlib import Path
import tomllib

import pytest

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

s = fm.s
OTHERS = {"claude": ["codex", "opencode"], "codex": ["claude", "opencode"], "opencode": ["claude", "codex"]}


def with_options(agents):
    return [x for agent in agents for x in ("--with", agent)]


def codex_config(run):
    return tomllib.loads(run.home_text.get(".codex/config.toml", ""))


def opencode_config(run):
    return json.loads(run.env.get("OPENCODE_CONFIG_CONTENT", "{}"))


def assert_level(run, agents, level):
    """RUN's sandbox has each of AGENTS' switches for LEVEL, and none of a stricter one."""
    for agent in agents:
        for key, value in s.TELEMETRY_ENV[agent]["off"].items():
            assert run.env.get(key) == (value if level != "on" else None), (agent, key)
        for key, value in s.TELEMETRY_ENV[agent]["strict"].items():
            assert run.env.get(key) == (value if level == "strict" else None), (agent, key)
    if "codex" in agents:
        config = codex_config(run)
        off = level != "on"
        assert (config.get("analytics", {}).get("enabled") is False) == off
        assert (config.get("feedback", {}).get("enabled") is False) == off
        assert (config.get("otel", {}).get("metrics_exporter") == "none") == off
        assert (config.get("check_for_update_on_startup") is False) == off
        assert (config.get("features", {}).get("plugins") is False) == (level == "strict")
    if "opencode" in agents:
        config = opencode_config(run)
        assert (config.get("share") == "disabled" and config.get("autoupdate") is False) == (level != "on")


def test_off_is_the_default_for_every_agent(m):  # noqa: F811
    run = m.run()
    assert_level(run, [m.agent], "off")
    assert m.plan()["telemetry"]["level"] == "off" and m.plan()["telemetry"]["origin"] == "default"


@pytest.mark.parametrize("level", ["strict", "on"])
def test_the_levels(m, level):  # noqa: F811
    assert_level(m.run("--telemetry", level), [m.agent], level)
    assert m.plan("--telemetry", level)["telemetry"]["origin"] == "--telemetry"


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
    assert_level(m.run("--telemetry", "on"), [m.agent], "on")
    path.write_text(json.dumps({"version": 1, "telemetry": "maybe"}))
    with pytest.raises(ValueError, match="telemetry is one of off, strict, on"):
        m.run()


def test_the_security_modes(m):  # noqa: F811
    plan = m.plan("--mode", "paranoid")["telemetry"]
    assert plan["level"] == "strict" and plan["origin"] == "--mode paranoid"
    m.refused("--mode", "high", "--telemetry", "on", match="--telemetry on widens the security mode high")
    m.refused("--mode", "paranoid", "--telemetry", "off", match="--telemetry off widens the security mode paranoid")
    assert m.plan("--mode", "high")["telemetry"]["level"] == "off"
    # A default of yours below the mode's floor is raised to it, not refused.
    path = s.defaults_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "telemetry": "on"}))
    assert m.plan("--mode", "paranoid")["telemetry"]["level"] == "strict"
    assert m.plan("--mode", "lax")["telemetry"]["level"] == "on"


@pytest.mark.parametrize("options", [["--telemetry", "on"], ["--telemetry=on"], ["-tm", "on"]])
def test_a_project_file_cannot_turn_it_on(m, options):  # noqa: F811
    m.project_file({"options": options})
    m.refused(match="--telemetry on is not allowed in a project configuration")


def test_a_project_file_may_make_it_stricter(m):  # noqa: F811
    m.project_file({"options": ["--telemetry", "strict"]})
    assert m.plan()["telemetry"]["level"] == "strict"


def test_what_you_pass_yourself_wins(m):  # noqa: F811
    key = next(iter(s.TELEMETRY_ENV[m.agent]["off"]), None) or "DISABLE_TELEMETRY"
    m.monkeypatch.setenv(key, "0")
    assert m.run("--env", key).env[key] == "0"


# --- Codex's config.toml: added to, never rewritten ------------------------------------------------------------

KEYS = s.codex_telemetry_keys("off")


def merged(text, keys=KEYS):
    out, kept = s.codex_config_with(text, keys, tomllib)
    return out, tomllib.loads(out), kept


def test_codex_an_empty_file():
    out, doc, kept = merged("")
    assert doc == {"check_for_update_on_startup": False, "analytics": {"enabled": False},
                   "feedback": {"enabled": False}, "otel": {"metrics_exporter": "none"}} and kept == []


def test_codex_a_file_with_tables_of_its_own():
    text = ('model = "o4"\n\n[analytics]  # mine\nfoo = 1\n\n[otel]\nexporter = "none"\n\n'
            '[mcp_servers.docs]\ncommand = "x"\nwhen = 1979-05-27T07:32:00Z\n')
    out, doc, kept = merged(text)
    assert doc["analytics"] == {"foo": 1, "enabled": False} and doc["otel"] == {"exporter": "none",
                                                                                 "metrics_exporter": "none"}
    assert doc["model"] == "o4" and doc["mcp_servers"]["docs"]["command"] == "x" and kept == []
    assert str(doc["mcp_servers"]["docs"]["when"]).startswith("1979-05-27")
    assert out.endswith("[feedback]\nenabled = false\n") and "foo = 1" in out


def test_codex_what_your_file_sets_stays_and_is_said():
    text = 'check_for_update_on_startup = true\nanalytics.enabled = true\n[otel]\nmetrics_exporter = "statsig"\n'
    out, doc, kept = merged(text)
    assert doc["check_for_update_on_startup"] is True and doc["analytics"] == {"enabled": True}
    assert doc["otel"]["metrics_exporter"] == "statsig" and doc["feedback"] == {"enabled": False}
    assert sorted(kept) == [("analytics.enabled", True), ("check_for_update_on_startup", True),
                            ("otel.metrics_exporter", "statsig")]


@pytest.mark.parametrize("text", [
    'analytics = { foo = 1 }\n',                      # an inline table cannot be added to
    'analytics.foo = 1\n',                            # a table made by dotted keys has no header
    '[analytics]\nfoo = 1\n[analytics.sub]\nx = 1\n[profiles]\nnote = """\n[analytics]\n"""\n',  # two headers
    'analytics = 3\n',                                # not a table
])
def test_codex_a_file_that_cannot_be_added_to_safely(text):
    with pytest.raises(ValueError):
        s.codex_config_with(text, KEYS, tomllib)


def test_codex_left_as_it_is_with_a_warning(tmp_path, capsys):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text("analytics = { foo = 1 }\n")
    s.codex_telemetry_defaults(home, "off")
    assert (home / ".codex/config.toml").read_text() == "analytics = { foo = 1 }\n"
    assert "Codex's telemetry is left as its ~/.codex/config.toml says" in capsys.readouterr().err
    (home / ".codex/config.toml").write_text("not = [toml\n")
    s.codex_telemetry_defaults(home, "strict")
    assert (home / ".codex/config.toml").read_text() == "not = [toml\n"
    (home / ".codex/config.toml").write_text("analytics.enabled = true\n")
    s.codex_telemetry_defaults(home, "off")
    assert "analytics.enabled = true in ~/.codex/config.toml stays" in capsys.readouterr().err
    assert tomllib.loads((home / ".codex/config.toml").read_text())["feedback"] == {"enabled": False}
    before = (home / ".codex/config.toml").read_text()
    s.codex_telemetry_defaults(home, "off")
    assert (home / ".codex/config.toml").read_text() == before, "a second start changes nothing"


def test_codex_a_symlinked_config_is_refused(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (tmp_path / "elsewhere.toml").write_text("")
    (home / ".codex/config.toml").symlink_to(tmp_path / "elsewhere.toml")
    with pytest.raises(ValueError):
        s.codex_telemetry_defaults(home, "off")
    assert (tmp_path / "elsewhere.toml").read_text() == ""


def test_the_names_are_the_agents_own():
    """The switches as the sources name them (README, Privacy: telemetry)."""
    assert set(s.TELEMETRY_ENV["claude"]["off"]) - set(s.TELEMETRY_TOOLS) == {"DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
                                                     "DISABLE_BUG_COMMAND", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
                                                     "DISABLE_AUTOUPDATER"}
    assert [(t, k) for t, k, _, _ in s.codex_telemetry_keys("strict")] == [
        (None, "check_for_update_on_startup"), ("analytics", "enabled"), ("feedback", "enabled"),
        ("otel", "metrics_exporter"), ("features", "plugins")]
    assert s.codex_telemetry_keys("on") == []
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
    on = m.run(*with_options(OTHERS[m.agent]), "--telemetry", "on")
    assert proxy_says(on, "api.anthropic.com", "/api/event_logging/batch") == "allow"
    assert "telemetry" not in [source["name"] for source in on.a.rulesets]


def test_only_for_the_agents_of_the_run_and_never_against_no_inspect(m):  # noqa: F811
    names = [rule["id"] for source in m.run().a.rulesets if source["name"] == "telemetry" for rule in source["http"]]
    assert names == [f"telemetry-{m.agent}-1"] if m.agent in s.TELEMETRY_PATHS else names == []
    if m.agent in s.TELEMETRY_PATHS:
        host = s.TELEMETRY_PATHS[m.agent][0][0]
        run = m.run("--no-inspect", host)  # no rule the proxy could not hold: the start is not refused
        assert "telemetry" not in [source["name"] for source in run.a.rulesets]
