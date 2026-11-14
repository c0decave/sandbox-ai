"""Independent feature review 2 of sandbox-ai 0.1.95: each test failed before its fix (docs/releases/v0.1.95.md,
"Feature review 2")."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("launcher_review2", "sandbox/agent_sandbox.py")
traffic = load("llm_traffic_review2", "sandbox/llm_traffic.py")
presets = load("presets_review2", "sandbox/presets.py")
dsh = load("dsh_review2", "sandbox/dsh.py")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


# --- 1. the LLM traffic log shows what the agent sent, whatever its shape --------------------------------------------

def record(n, request, api="chat", response=None):
    return {"version": 1, "n": n, "time": "2026-11-13T10:00:00Z", "run": "r1", "api": api, "model": "m",
            "status": 200, "request": request, "response": response if response is not None else {"text": "ok"}}


HOSTILE = [
    # the agent's own request: a message's tool_calls that is no list, a thinking block that is no text, a content
    # nested deep (json.loads reads it; the viewer recursed two frames per level)
    record(1, {"messages": [{"role": "assistant", "tool_calls": 5}]}),
    record(2, {"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": 5}]}]}, api="messages"),
    record(3, {"messages": [{"role": "user", "content": json.loads("[" * 450 + '"deep"' + "]" * 450)}]}),
    record(4, {"messages": [{"role": "assistant", "tool_calls": True, "content": None}]}),
    record(5, {"input": [{"type": "reasoning", "summary": {"text": 7}}]}, api="responses"),
    record(6, {"messages": [{"role": "user", "content": "hi"}]}, response={"text": 5, "reasoning": [1]}),
]


@pytest.mark.parametrize("hostile", HOSTILE, ids=[f"request-{r['n']}" for r in HOSTILE])
def test_llm_traffic_shows_a_request_of_any_shape(hostile):
    """The request in the log is the agent's own JSON: a prompt-injected agent sending a message whose tool_calls is
    a number (or a thinking block that is no text, or a content nested a few hundred levels) made `sandbox-ai llm
    traffic` (its chat, --full, --markdown, --request N --json, which the app's page reads) end in a TypeError or
    RecursionError traceback - its own requests and every later one out of sight of the human."""
    for full in (False, True):
        lines = traffic.transcript(hostile, full=full)
        assert lines and all(isinstance(line, str) for line in lines)
    assert traffic.markdown([hostile], full=True).startswith("# LLM traffic")
    shown = traffic.view(hostile)
    assert all(isinstance(item["text"], str) for item in shown["messages"] + shown["answer"])
    json.dumps(shown)
    assert traffic.summary_line(traffic.summary(hostile), False).startswith("#")


def test_llm_traffic_goes_on_after_a_record_it_cannot_lay_out(tmp_path, capsys):
    """One record the viewer cannot lay out never hides the requests after it: the command shows them all."""
    path = tmp_path / "llm-traffic-20261113-100000-r1.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in [*HOSTILE, record(7, {"messages": [
        {"role": "user", "content": "the last one"}]})]))
    assert traffic.cli(["--log-dir", str(tmp_path), "r1", "--full"]) == 0
    out = capsys.readouterr().out
    assert "#7 " in out and "the last one" in out
    assert traffic.cli(["--log-dir", str(tmp_path), "r1", "--request", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["view"]["messages"]


def test_a_summary_line_of_a_record_without_its_number():
    assert traffic.summary_line(traffic.summary({"version": 1, "request": {}, "response": {}}), False).startswith("#?")


# --- 2. a preset that is not yours never turns telemetry on or records the cloud provider's traffic ------------------

def write_defaults(directory, **values):
    path = directory.parent.parent / "sandbox-ai" / "defaults.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, **values}))


def preset(ident, options, **more):
    return {"format": "sandbox-ai preset", "version": 1, "id": ident, "title": ident, "description": "d",
            "options": options, **more}


@pytest.mark.parametrize("options", [["--llm-traffic", "all"], ["--llm-traffic=all"], ["-lt", "all"],
                                     ["--llm-traffic", "{level}"]])
def test_a_preset_of_a_shared_directory_cannot_record_all_traffic(tmp_path, options):
    """A project's file may not set --llm-traffic all ("only your command line or defaults.json"), but a preset -
    meant to be passed on, from a team's checkout among the preset directories - could: the cloud provider's whole
    conversations recorded after TLS inspection, from a file someone else wrote. (The agents' telemetry is always off:
    an old --telemetry on is ignored by the launcher wherever it comes from.)"""
    shared = tmp_path / "shared"
    shared.mkdir()
    values = {"level": {"label": "Level", "kind": "text", "default": "all"}} if "{level}" in options else {}
    (shared / "theirs.json").write_text(json.dumps(preset("theirs", options, values=values)))
    write_defaults(tmp_path / "xdg_config_home" / "sandbox-ai" / "presets", preset_dirs=[str(shared)])
    problems = []
    listed = presets.catalog(problems)
    with pytest.raises(ValueError) as refused:
        presets.apply(["claude", "--preset", "theirs"], canonical=s.canonical_options)
    # Written out, it is a problem of the file (not listed, the problem says why); filled in from a value, the start
    # refuses it.
    assert "is not allowed in a preset that is not yours" in str(refused.value) or (
        "theirs" not in listed and any("is not allowed in a preset that is not yours" in p for p in problems))
    # The same values are fine where nothing is widened, and in a preset of your own (it is your choice).
    (shared / "fine.json").write_text(json.dumps(preset("fine", ["--telemetry", "strict", "--llm-traffic", "off"])))
    assert presets.apply(["claude", "--preset", "fine"], canonical=s.canonical_options)[:5] == \
        ["claude", "--telemetry", "strict", "--llm-traffic", "off"]
    own = tmp_path / "xdg_config_home" / "sandbox-ai" / "presets"
    own.mkdir(parents=True, exist_ok=True)
    (own / "mine.json").write_text(json.dumps(preset("mine", options, values=values)))
    assert presets.apply(["claude", "--preset", "mine"], canonical=s.canonical_options)[0] == "claude"


def test_a_literal_widening_is_named_among_a_shared_preset_s_problems():
    assert any("--llm-traffic all is not allowed in a preset" in p for p in presets.problems(
        preset("x", ["--llm-traffic=all"]), origin="built-in"))
    assert presets.problems(preset("x", ["--llm-traffic", "all"]), origin="yours") == []
    # An old --telemetry on is no problem of a preset: the launcher ignores it (telemetry is always off).
    assert presets.problems(preset("x", ["--telemetry", "on"]), origin="extra") == []


# --- 3. DSH's profile in one word still gets sandbox-ai's patch ----------------------------------------------------

def test_dsh_profile_given_in_one_word_gets_the_patch():
    """`sandbox-ai dsh -- --profile=headless ...` was passed on without sandbox-ai's patch (its model, MCP servers and
    telemetry rows): only "--profile NAME" in two words was read as a profile. And --profile=desktop was not refused
    as --profile desktop is."""
    assert dsh.command_args(["--profile=headless", "task"]) == ["--profile", "headless", "--patch", dsh.PATCH, "task"]
    assert dsh.command_args(["--profile", "headless", "task"]) == ["--profile", "headless", "--patch", dsh.PATCH,
                                                                   "task"]
    assert dsh.command_args(["--version"]) == ["--version"]
    for args in (["--profile=desktop"], ["--profile=plugin", "x"], ["--profile", "desktop"]):
        with pytest.raises(ValueError, match="not in a sandbox"):
            dsh.check(argparse.Namespace(agent="dsh", agent_args=args, allow_host=[], no_inspect=[]))
    assert dsh.plan(argparse.Namespace(agent_args=["--profile=acp"]))["profile"] == "acp"


# --- 4. --binary in an agent's image: its telemetry stays off --------------------------------------------------------

@pytest.mark.parametrize("agent", ["hermes", "qwen", "dsh", "goose"])
def test_a_start_with_binary_keeps_the_agent_s_telemetry_off(agent, tmp_path):
    """`sandbox-ai goose --binary bash` (a shell in Goose's image, the live checks' way in too) set none of the
    agent's telemetry opt-outs and the proxy refused none of its telemetry hosts: Goose started from that shell asks
    for its PostHog usage data with "Yes" preselected, Qwen Code sends its usage statistics (on upstream) - and with
    --egress-all-allow nothing refused them. Telemetry of these agents is never switchable."""
    a = s.parse_args([agent, "--workspace", str(tmp_path), "--binary", "/bin/sh"])
    a.telemetry_default = None
    module = s.load_hermes_module() if agent == "hermes" else s.agent_module(agent)
    rules = s.telemetry_rules(a) or {"http": []}
    assert set(module.TELEMETRY_HOSTS) <= {rule["host"] for rule in rules["http"]}
    env = s.agent_environment(a)
    assert {key: env.get(key) for key in module.TELEMETRY_ENV} == module.TELEMETRY_ENV
    # Your own --env cannot undo them either.
    a = s.parse_args([agent, "--workspace", str(tmp_path), "--binary", "/bin/sh", "--telemetry", "on"])
    a.telemetry_default = None
    assert {key: s.agent_environment(a).get(key) for key in module.TELEMETRY_ENV} == module.TELEMETRY_ENV
    assert set(module.TELEMETRY_HOSTS) <= {rule["host"] for rule in (s.telemetry_rules(a) or {"http": []})["http"]}


def test_a_start_with_binary_of_another_agent_is_as_before(tmp_path):
    a = s.parse_args(["claude", "--workspace", str(tmp_path), "--binary", "/bin/sh"])
    a.telemetry_default = None
    assert s.telemetry_rules(a) is None and "DISABLE_TELEMETRY" not in s.agent_environment(a)


# --- 5. a password's usual short names are a secret's names too ------------------------------------------------------

mcp = load("mcp_review2", "sandbox/mcp.py")
IMAGE = "ghcr.io/example/bsky@sha256:" + "a" * 64


def test_a_password_under_a_short_name_is_never_kept_as_a_plain_value():
    """"Never anything of a secret's name" - but BSKY_APP_PW (the Bluesky server's app password), DB_PWD, ADMIN_PASS or
    AUTHTOKEN passed as plain values: the old form of a definition takes such a variable from your environment or the
    secret of its name ("env"), and `--mcp-env bsky:BSKY_APP_PW=...` (or `mcp add|set -e` without a catalog that marks
    it) kept the password in the instance's definition, its state and a saved board."""
    definition = {"image": IMAGE, "env": ["BSKY_APP_PW"]}
    with pytest.raises(ValueError, match="name of a secret"):
        mcp.with_start_env("bsky", definition, {"BSKY_APP_PW": ("value", "hunter2hunter2")})
    for name in ("BSKY_APP_PW", "DB_PWD", "ADMIN_PASS", "MY_AUTHTOKEN", "CLIENTSECRET", "SMTP_PASSCODE",
                 "ROOTPASSWORD"):
        assert mcp.secret_looking(name, "x"), name
    # Names that only resemble one stay plain values.
    for name in ("MAX_TOKENS", "PASSTHROUGH", "BYPASS_CACHE", "ALLOW_PRIVATE_TARGETS", "PWM_PIN", "LOG_LEVEL",
                 "TOKENIZER", "SECRETARY_NAME"):
        assert mcp.secret_looking(name, "1") is None, name
    assert mcp.with_start_env("bsky", definition, {"BSKY_APP_PW": ("secret", "bsky")})["env_secrets"] == {
        "BSKY_APP_PW": "bsky"}


# --- 6. a telemetry host opened by --allow-domain: the refusal names --allow-domain -----------------------------------

@pytest.mark.parametrize("agent,domain,host", [("hermes", "nousresearch.com", "telemetry.nousresearch.com"),
                                               ("qwen", "aliyuncs.com", "x.rum.aliyuncs.com"),
                                               ("dsh", "deepseeksvc.com", "dsh-otel-collector.deepseeksvc.com"),
                                               ("goose", "posthog.com", "us.i.posthog.com")])
def test_a_telemetry_host_opened_by_allow_domain_is_refused_naming_it(agent, domain, host, tmp_path):
    """`sandbox-ai goose --allow-domain posthog.com` was refused with "drop that --allow-host": an option the user had
    not given (--allow-domain becomes --allow-host DOMAIN '*.DOMAIN' right after the options are read)."""
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match=f"drop --allow-domain {domain}"):
        s.launch([agent, "--workspace", str(work), "--dry-run", "--allow-domain", domain])
    with pytest.raises(ValueError, match="drop that --allow-host"):
        s.launch([agent, "--workspace", str(work), "--dry-run", "--allow-host", host])


# --- 8. an MCP call's question and record hide the run's secret values, as a tool call's do --------------------------

def test_an_mcp_call_s_question_and_record_hide_the_run_s_secret_values():
    """A question about the agent's own tool shows its arguments with the run's secret values (--secret-env, --env:
    the agent has them) as [redacted], and tools.jsonl records them so. An MCP tool call carrying the same
    value - a password of no known shape, passed to a server - showed it in the question's details (the app, the web
    page, approvals show, the question file kept with the run) and recorded it in mcp-calls.jsonl."""
    import io
    import test_rules as tr
    rt = tr.rt
    value = "correct-horse-battery-staple"
    asked = {}

    def asker(kind, details):
        asked.update(details)
        return {"decision": "deny", "scope": "once", "reason": "no"}
    rules = {"version": 1, "mcp": [tr.rule("posts", "ask", server="bsky", tool="post")]}
    out = io.StringIO()
    gateway = rt.McpGateway({"server": "bsky", "rules": [tr.checked(rules, "rules.json")]}, out=out, asker=asker,
                            plain=True, values=[value])
    gateway.decide("post", {"text": f"my password is {value}"}, 1)
    assert value not in asked["arguments_full"] and "[redacted]" in asked["arguments_full"]
    assert value not in asked["arguments"] and value not in out.getvalue()
    # The relays of a run hand the values to every gateway they open.
    relays = rt.McpRelays(secret_values=[value, "short"])
    assert relays.values == [value]


# --- 7. messages that say what is wrong, not an Errno -----------------------------------------------------------------

def test_a_catalog_that_cannot_be_read_is_named_without_an_errno(tmp_path):
    """`sandbox-ai mcp catalog --json --catalog FILE` (the web page's Add form reads it) said "FILE: [Errno 2] No such
    file or directory: 'FILE'"."""
    _found, problems = mcp.catalog_entries([("c0", str(tmp_path / "gone.json"))])
    assert problems == [f"{tmp_path / 'gone.json'}: No such file or directory"]


def test_llm_traffic_of_a_run_that_has_no_log_there_says_so(tmp_path, capsys):
    """`sandbox-ai llm traffic RUN --log-dir DIR` of a run with no LLM traffic log in DIR (a typo, another directory)
    said "no requests", as if the run had sent none."""
    (tmp_path / "llm-traffic-20261113-100000-r1.jsonl").write_text(json.dumps(record(1, {"messages": []})) + "\n")
    with pytest.raises(ValueError, match=r"no LLM traffic log of the run r2 in .*: r1"):
        traffic.cli(["--log-dir", str(tmp_path), "r2"])
    assert traffic.cli(["--log-dir", str(tmp_path), "r1", "--summary"]) == 0
    assert "#1" in capsys.readouterr().out
