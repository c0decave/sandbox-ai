"""The agents' telemetry cannot be turned on from inside the sandbox (sandbox/telemetry_guard.py; agent_sandbox:
telemetry_guarded, telemetry_guard_files, telemetry_guard_mounts; the user's rule "Agents: telemetry ALWAYS off -
never switchable, not even from inside the sandbox").

Regression (2026-11-15, found in review after Hermes' guard): the launcher pinned telemetry off for the agent it
starts, but the agent could start a second copy of itself from its shell without the launcher's variables and options:
`qwen --telemetry --telemetry-target local --telemetry-outfile F` and `QWEN_USAGE_STATISTICS_ENABLED=true qwen` (flags
and variables win over Qwen Code's system settings), `env -u GOOSE_TELEMETRY_OFF GOOSE_TELEMETRY_ENABLED=true goose`
and `env -u OTEL_SDK_DISABLED OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 goose` (measured: Goose 1.57.0 exports
logs, traces and metrics there), `dsh` typed without the launcher's patch, `codex -c analytics.enabled=true` or an
edited ~/.codex/config.toml, `CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_METRICS_EXPORTER=console claude -p ...` or an "env"
block in ~/.claude/settings.json, `ENABLE_BETA_TRACING_DETAILED=1 BETA_TRACING_ENDPOINT=... claude` (measured: Claude
Code 2.1.322 exports traces and logs there with every launcher pin set), `OTEL_EXPORTER_OTLP_ENDPOINT=... opencode`
(measured: OpenCode 1.19.5 exports with OTEL_SDK_DISABLED=true) and `env -u OPENCODE_CONFIG_CONTENT opencode`.

Now each agent's command in its image is guarded (one message, exit status 2; the opt-out variables pinned again) and
its own managed configuration, which wins over every file, variable and option of its own, pins the switches."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import http.server
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load("telemetry_guard_tests", "sandbox/telemetry_guard.py")
NODE = shutil.which("node")


def message(name):
    return f"sandbox-ai: {name} telemetry is always off in sandbox-ai and cannot be turned on"


# --- the guard's decisions --------------------------------------------------------------------------------------------

def test_the_message_is_the_one_the_user_reads():
    assert [guard.message(agent) for agent in guard.AGENTS] == [
        message("Claude Code's"), message("Codex's"), message("OpenCode's"), message("Qwen Code's"),
        message("Goose's"), message("DeepSeek Harness'")]
    assert guard.KINDS == {"claude": "exec", "codex": "node", "opencode": "exec", "qwen": "node", "goose": "exec",
                           "dsh": "node"}


def test_the_guard_pins_what_the_launcher_sets():
    """Every opt-out the launcher gives an agent is one its guard pins, with the same value (a guarded command of the
    launcher's own start is never refused), and no two agents pin a variable differently."""
    s = load("telemetry_guard_launcher", "sandbox/agent_sandbox.py")
    for agent in guard.AGENTS:
        pins = guard.pinned(agent)
        assert {name: pins.get(name) for name in s.TELEMETRY_ENV[agent]["off"]} == s.TELEMETRY_ENV[agent]["off"], agent
    for agent, module in (("qwen", "qwen"), ("goose", "goose"), ("dsh", "dsh")):
        own = load(f"telemetry_guard_{module}", f"sandbox/{module}.py").TELEMETRY_ENV
        assert {name: guard.pinned(agent).get(name) for name in own} == own, agent
    qwen = load("telemetry_guard_qwen_base", "sandbox/qwen.py")
    assert guard.pinned("qwen")["QWEN_CODE_SYSTEM_SETTINGS_PATH"] == qwen.BASE_ENV["QWEN_CODE_SYSTEM_SETTINGS_PATH"]
    assert guard.DSH_PATCH == load("telemetry_guard_dsh_patch", "sandbox/dsh.py").PATCH
    hermes = load("telemetry_guard_hermes", "sandbox/hermes_guard.py")
    assert {name: guard.accepted()[name] for name in hermes.PINNED_ENV} == hermes.PINNED_ENV
    seen = {}
    for agent in guard.AGENTS:
        for name, value in guard.pinned(agent).items():
            assert seen.setdefault(name, value) == value, name
    assert s.TELEMETRY_TOOLS == guard.TOOLS
    codex = {".".join(filter(None, (table, key))): literal.strip('"')
             for table, key, _, literal in s.codex_telemetry_keys("off")}
    assert codex == guard.CODEX_PINNED


# (agent, environment, what is refused or None)
ENVIRONMENTS = [
    ("qwen", {}, None),
    ("qwen", {"QWEN_USAGE_STATISTICS_ENABLED": "true"}, "QWEN_USAGE_STATISTICS_ENABLED=true"),
    ("qwen", {"QWEN_TELEMETRY_ENABLED": "1"}, "QWEN_TELEMETRY_ENABLED=1"),
    ("qwen", {"QWEN_TELEMETRY_OUTFILE": "/tmp/t"}, "QWEN_TELEMETRY_OUTFILE=/tmp/t"),
    ("qwen", {"QWEN_CODE_SYSTEM_SETTINGS_PATH": "/tmp/none.json"}, "QWEN_CODE_SYSTEM_SETTINGS_PATH=/tmp/none.json"),
    ("qwen", {"QWEN_USAGE_STATISTICS_ENABLED": "", "OTEL_SDK_DISABLED": " "}, None),  # empty: as unset, pinned again
    ("qwen", {"QWEN_USAGE_STATISTICS_ENABLED": "false", "DISABLE_TELEMETRY": "1"}, None),  # another agent's pins
    ("goose", {"GOOSE_TELEMETRY_ENABLED": "true"}, "GOOSE_TELEMETRY_ENABLED=true"),
    ("goose", {"GOOSE_TELEMETRY_OFF": "0"}, "GOOSE_TELEMETRY_OFF=0"),
    ("goose", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}, "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318"),
    ("goose", {"OTEL_SDK_DISABLED": "false"}, "OTEL_SDK_DISABLED=false"),
    ("goose", {"GOOSE_MODEL": "m", "PATH": "/bin"}, None),
    ("dsh", {"DSH_TELEMETRY_DISABLED": "0"}, "DSH_TELEMETRY_DISABLED=0"),
    ("dsh", {"DSH_TELEMETRY_OTLP_URL": "http://x/v1/logs"}, "DSH_TELEMETRY_OTLP_URL=http://x/v1/logs"),
    ("dsh", {"DSH_PRODUCT_ANALYTICS_OTLP_URL": "http://x"}, "DSH_PRODUCT_ANALYTICS_OTLP_URL=http://x"),
    ("codex", {"CODEX_DAEMON_TELEMETRY_HANDOFF": "1"}, None),  # Codex's own hand-over, no switch
    ("codex", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x"}, "OTEL_EXPORTER_OTLP_ENDPOINT=http://x"),
    ("claude", {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}, "CLAUDE_CODE_ENABLE_TELEMETRY=1"),
    ("claude", {"OTEL_METRICS_EXPORTER": "console"}, "OTEL_METRICS_EXPORTER=console"),
    ("claude", {"ENABLE_BETA_TRACING_DETAILED": "1"}, "ENABLE_BETA_TRACING_DETAILED=1"),
    ("claude", {"BETA_TRACING_ENDPOINT": "http://127.0.0.1:1"}, "BETA_TRACING_ENDPOINT=http://127.0.0.1:1"),
    ("claude", {"ANT_OTEL_LOGS_EXPORTER": "otlp"}, "ANT_OTEL_LOGS_EXPORTER=otlp"),
    ("claude", {"CLAUDE_CODE_BYOC_ENABLE_DATADOG": "1"}, "CLAUDE_CODE_BYOC_ENABLE_DATADOG=1"),
    ("claude", {"CLAUDE_CODE_ENVIRONMENT_KIND": "byoc"}, "CLAUDE_CODE_ENVIRONMENT_KIND=byoc"),
    ("claude", {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}, "CLAUDE_CODE_ENTRYPOINT=claude-desktop"),
    ("claude", {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDECODE": "1"}, None),  # what Claude Code gives its shells
    ("claude", {"DO_NOT_TRACK": "0"}, "DO_NOT_TRACK=0"),
    ("opencode", {"OPENCODE_TEST_MANAGED_CONFIG_DIR": "/tmp/x"}, "OPENCODE_TEST_MANAGED_CONFIG_DIR=/tmp/x"),
    ("opencode", {"OTEL_EXPORTER_OTLP_HEADERS": "a=b"}, "OTEL_EXPORTER_OTLP_HEADERS=a=b"),
    ("opencode", {"OPENCODE_CONFIG_CONTENT": "{}", "OPENCODE_DISABLE_AUTOUPDATE": "1"}, None),
]

# (agent, arguments, what is refused or None)
ARGUMENTS = [
    ("qwen", ["--telemetry", "--telemetry-target", "local"], "--telemetry"),
    ("qwen", ["-p", "hi", "--telemetry-outfile=/tmp/t"], "--telemetry-outfile=/tmp/t"),
    ("qwen", ["--telemetryTarget", "gcp"], "--telemetryTarget"),
    ("qwen", ["--no-telemetry"], "--no-telemetry"),
    ("qwen", ["-p", "about --telemetry"], None),  # a prompt that names it
    ("qwen", ["--", "--telemetry"], None),
    ("qwen", ["--yolo", "-p", "x"], None),
    ("codex", ["-c", "analytics.enabled=true"], "-c analytics.enabled=true"),
    ("codex", ["--config", "otel.exporter=otlp-http"], "--config otel.exporter=otlp-http"),
    ("codex", ["-ccheck_for_update_on_startup=true"], "-ccheck_for_update_on_startup=true"),
    ("codex", ["--config=feedback={enabled=true}"], "--config=feedback={enabled=true}"),
    ("codex", ["-c", "otel.environment=dev"], "-c otel.environment=dev"),
    ("codex", ["-c", "profiles.p.analytics.enabled=true"], "-c profiles.p.analytics.enabled=true"),
    ("codex", ["-c", ' analytics . enabled = "false" '], None),
    ("codex", ["-c", "model=o4", "exec", "hi"], None),
    ("codex", ["exec", "--", "-c", "analytics.enabled=true"], None),
    ("goose", ["session"], None), ("claude", ["-p", "hi"], None), ("opencode", ["run", "hi"], None),
    ("dsh", ["headless", "x"], None),
]


@pytest.mark.parametrize("agent, environ, what", ENVIRONMENTS)
def test_variables_that_turn_telemetry_on_or_take_the_pins_away_are_refused(agent, environ, what):
    found = guard.refused(agent, [], environ)
    assert (found[0] if found else None) == what
    if what:
        assert guard.refused_names(agent, environ) == [what.split("=", 1)[0]]


@pytest.mark.parametrize("agent, args, what", ARGUMENTS)
def test_arguments_that_turn_telemetry_on_are_refused(agent, args, what):
    found = guard.refused(agent, args, {})
    assert (found[0] if found else None) == what


def test_the_launchers_own_codex_overrides_pass():
    s = load("telemetry_guard_codex_args", "sandbox/agent_sandbox.py")
    for level in ("off", "strict"):
        assert guard.refused("codex", s.codex_telemetry_args(level), {}) is None


def test_qwen_relaunch_arguments_count_too():
    """Qwen Code's entry reads its arguments from QWEN_CODE_RELAUNCH_ARGS after an update (cli-entry.js)."""
    assert guard.refused("qwen", [], {"QWEN_CODE_RELAUNCH_ARGS": '["--telemetry"]'})[0] == "--telemetry"
    assert guard.refused("qwen", [], {"QWEN_CODE_RELAUNCH_ARGS": '["-p", "x"]'}) is None
    assert guard.refused("qwen", [], {"QWEN_CODE_RELAUNCH_ARGS": "junk"}) is None


def test_the_variables_are_pinned_and_empty_telemetry_variables_dropped():
    env = guard.pinned_environment("goose", {"PATH": "/bin", "OTEL_EXPORTER_OTLP_ENDPOINT": "", "GOOSE_TELEMETRY_OFF": "",
                                             "GOOSE_MODEL": "", "DO_NOT_TRACK": "1"})
    assert env["PATH"] == "/bin" and env["GOOSE_MODEL"] == "" and "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert {name: env[name] for name in guard.pinned("goose")} == guard.pinned("goose")
    assert "CLAUDE_CODE_ENVIRONMENT_KIND" not in guard.pinned_environment("claude", {"CLAUDE_CODE_ENVIRONMENT_KIND": ""})


@pytest.mark.parametrize("args, expected", [
    (["headless", "x"], ["headless", "--patch", "P", "x"]),
    (["--profile", "web", "--port", "1"], ["--profile", "web", "--patch", "P", "--port", "1"]),
    (["--profile=acp"], ["--profile=acp", "--patch", "P"]),
    (["headless", "--patch", "P", "x"], ["headless", "--patch", "P", "x"]),  # the launcher's own: once
    (["--version"], ["--version"]), (["plugin", "--profile", "web", "add", "x"], ["plugin", "--profile", "web", "add", "x"]),
    (["web", "--dump-default-config"], ["web", "--dump-default-config"]), ([], []),
])
def test_dsh_gets_sandbox_ais_patch_as_its_last_overlay(args, expected):
    assert guard.dsh_args(args, patch="P", exists=lambda path: True) == expected
    assert guard.dsh_args(args, patch="P", exists=lambda path: False) == args  # not in this sandbox: nothing to add


# --- the same decisions in the JavaScript guard (Codex, Qwen Code, DSH) -----------------------------------------------

def node_guard(tmp_path, agent, args, environ, patch=None):
    """Run AGENT's entry stand-in that imports the generated guard (as the image's entry does) and prints what the
    agent would see: its arguments and environment."""
    js = guard.javascript()
    if patch:
        js = js.replace(json.dumps(guard.DSH_PATCH), json.dumps(str(patch)), 1)
    (tmp_path / "telemetry-guard.mjs").write_text(js)
    entry = tmp_path / f"{agent}-entry.mjs"
    entry.write_text("#!/usr/bin/env node\n" + guard.import_line(agent, str(tmp_path / "telemetry-guard.mjs"))
                     + "\nconsole.log(JSON.stringify({argv: process.argv.slice(2), env: process.env}));\n")
    return subprocess.run([NODE, str(entry), *args], capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ["PATH"], **environ})


@pytest.mark.skipif(not NODE, reason="node")
@pytest.mark.parametrize("agent, environ, what", [case for case in ENVIRONMENTS if guard.KINDS[case[0]] == "node"])
def test_the_javascript_guard_refuses_the_same_variables(tmp_path, agent, environ, what):
    done = node_guard(tmp_path, agent, [], environ)
    if what is None:
        assert done.returncode == 0, done.stderr
        seen = json.loads(done.stdout)["env"]
        assert {name: seen[name] for name in guard.pinned(agent)} == guard.pinned(agent)
        assert {name: seen.get(name) for name in environ if environ[name].strip()} == \
            {name: value for name, value in guard.pinned_environment(agent, environ).items() if name in environ
             and environ[name].strip()}
    else:
        assert done.returncode == 2 and done.stdout == ""
        assert done.stderr.startswith(f"{guard.message(agent)} ({what} is refused: "), done.stderr


@pytest.mark.skipif(not NODE, reason="node")
@pytest.mark.parametrize("agent, args, what", [case for case in ARGUMENTS if guard.KINDS[case[0]] == "node"])
def test_the_javascript_guard_refuses_the_same_arguments(tmp_path, agent, args, what):
    done = node_guard(tmp_path, agent, args, {})
    if what is None:
        assert done.returncode == 0, done.stderr
    else:
        assert done.returncode == 2 and done.stderr.startswith(f"{guard.message(agent)} ({what} is refused: ")


@pytest.mark.skipif(not NODE, reason="node")
def test_the_javascript_guard_reads_qwens_relaunch_arguments_and_adds_dshs_patch(tmp_path):
    done = node_guard(tmp_path, "qwen", [], {"QWEN_CODE_RELAUNCH_ARGS": '["--telemetry"]'})
    assert done.returncode == 2 and "(--telemetry is refused: " in done.stderr
    patch = tmp_path / "sandbox-ai.patch.yml"
    patch.write_text("[]\n")
    for args, expected in (([], []), (["headless", "x"], ["headless", "--patch", str(patch), "x"]),
                           (["--profile", "web"], ["--profile", "web", "--patch", str(patch)]),
                           (["--version"], ["--version"])):
        done = node_guard(tmp_path, "dsh", args, {}, patch=patch)
        assert done.returncode == 0 and json.loads(done.stdout)["argv"] == expected, (args, done.stderr)
    done = node_guard(tmp_path, "dsh", ["headless", "x"], {}, patch=tmp_path / "missing.yml")
    assert json.loads(done.stdout)["argv"] == ["headless", "x"]


@pytest.mark.skipif(not NODE, reason="node")
def test_an_unknown_agent_is_refused_by_both_guards(tmp_path, capsys):
    (tmp_path / "telemetry-guard.mjs").write_text(guard.javascript())
    entry = tmp_path / "entry.mjs"
    entry.write_text(f'import "file://{tmp_path}/telemetry-guard.mjs?agent=nobody";\nconsole.log("ran");\n')
    done = subprocess.run([NODE, str(entry)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "the telemetry guard knows no agent" in done.stderr and done.stdout == ""
    assert guard.run("nobody", []) == 2 and "knows no agent 'nobody'" in capsys.readouterr().err


# --- the exec form (Claude Code, OpenCode, Goose) ---------------------------------------------------------------------

def test_the_exec_guard_becomes_the_agent_with_the_pins(tmp_path):
    calls = []
    status = guard.run("goose", ["session"], environ={"PATH": "/bin", "GOOSE_TELEMETRY_OFF": ""},
                       execve=lambda *call: calls.append(call), real_dir=str(tmp_path))
    assert status == 0 and calls[0][0] == f"{tmp_path}/goose" and calls[0][1] == ["goose", "session"]
    assert calls[0][2]["GOOSE_TELEMETRY_OFF"] == "1" and calls[0][2]["PATH"] == "/bin"


def test_the_exec_guard_refuses_before_anything_runs(tmp_path, capsys):
    calls = []
    status = guard.run("claude", ["-p", "hi"], environ={"CLAUDE_CODE_ENABLE_TELEMETRY": "1"},
                       execve=lambda *call: calls.append(call), real_dir=str(tmp_path))
    assert status == 2 and calls == []
    assert capsys.readouterr().err == (f"{guard.message('claude')} (CLAUDE_CODE_ENABLE_TELEMETRY=1 is refused: "
                                       "it stays 0)\n")


def test_a_missing_agent_says_so(tmp_path, capsys):
    assert guard.run("opencode", [], environ={}, real_dir=str(tmp_path)) == 127
    assert "OpenCode cannot be started" in capsys.readouterr().err


# --- the image's build: --install --------------------------------------------------------------------------------------

def test_install_guards_a_node_entry_once(tmp_path):
    package = tmp_path / "opt/qwen/node_modules/@qwen-code/qwen-code"
    package.mkdir(parents=True)
    entry = package / "cli-entry.js"
    entry.write_text("#!/usr/bin/env node\nconsole.log('qwen');\n")
    entry.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "qwen").symlink_to(entry)
    guard_dir = tmp_path / "lib/sandbox-ai"
    assert guard.install("qwen", command_dir=bin_dir, guard_dir=guard_dir) == entry
    guard.install("qwen", command_dir=bin_dir, guard_dir=guard_dir)  # a second build step: nothing twice
    line = guard.import_line("qwen", str(guard_dir / "telemetry-guard.mjs"))
    assert entry.read_text() == f"#!/usr/bin/env node\n{line}\nconsole.log('qwen');\n"
    assert entry.stat().st_mode & 0o777 == 0o755 and (bin_dir / "qwen").is_symlink()
    assert (guard_dir / "telemetry-guard.mjs").read_text() == guard.javascript()


@pytest.mark.parametrize("linked", [True, False])
def test_install_puts_the_shim_where_an_exec_agents_command_was(tmp_path, linked):
    """Claude Code and OpenCode: /usr/local/bin/AGENT is npm's link to the program; Goose: the program itself."""
    bin_dir, guard_dir = tmp_path / "bin", tmp_path / "lib/sandbox-ai"
    bin_dir.mkdir()
    program = tmp_path / "program"
    program.write_text("#!/bin/sh\necho real \"$@\"\n")
    program.chmod(0o755)
    if linked:
        (bin_dir / "claude").symlink_to(program)
    else:
        shutil.copy2(program, bin_dir / "claude")
    real = guard.install("claude", command_dir=bin_dir, guard_dir=guard_dir)
    guard.install("claude", command_dir=bin_dir, guard_dir=guard_dir)
    assert real == guard_dir / "real/claude" and real.is_symlink() == linked
    assert (real.resolve() == program) if linked else real.read_text() == program.read_text()
    shim = bin_dir / "claude"
    assert not shim.is_symlink() and shim.stat().st_mode & 0o777 == 0o755
    assert shim.read_text() == guard.shim("claude", str(guard_dir / "telemetry_guard.py"))
    assert shim.read_text().splitlines()[-1] == (f"exec /usr/bin/python3 -IS {guard_dir}/telemetry_guard.py "
                                                 'claude "$@"')


@pytest.mark.skipif(not Path(guard.PYTHON).exists(), reason="Debian's python3")
def test_the_shim_runs_the_guard_and_becomes_the_agent(tmp_path):
    """The whole exec form as the image has it: the shim, the guard under python3 -IS, the program in REAL_DIR."""
    bin_dir, guard_dir = tmp_path / "bin", tmp_path / "lib/sandbox-ai"
    bin_dir.mkdir()
    program = tmp_path / "goose-binary"
    program.write_text('#!/bin/sh\necho "goose $* GOOSE_TELEMETRY_OFF=$GOOSE_TELEMETRY_OFF"\n')
    program.chmod(0o755)
    shutil.copy2(program, bin_dir / "goose")
    guard_dir.mkdir(parents=True)
    (guard_dir / "telemetry_guard.py").write_text((ROOT / "sandbox/telemetry_guard.py").read_text().replace(
        'REAL_DIR = GUARD_DIR + "/real"', f'REAL_DIR = "{guard_dir}/real"', 1))
    guard.install("goose", command_dir=bin_dir, guard_dir=guard_dir)
    env = {"PATH": "/usr/bin:/bin"}
    done = subprocess.run([str(bin_dir / "goose"), "--version"], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0 and done.stdout == "goose --version GOOSE_TELEMETRY_OFF=1\n", done.stderr
    done = subprocess.run([str(bin_dir / "goose"), "session"], capture_output=True, text=True, timeout=60,
                          env={**env, "GOOSE_TELEMETRY_ENABLED": "true"})
    assert done.returncode == 2 and done.stdout == ""
    assert done.stderr == f"{guard.message('goose')} (GOOSE_TELEMETRY_ENABLED=true is refused: it stays false)\n"


@pytest.mark.parametrize("name, agents", [
    ("claude", ["claude"]), ("codex", ["codex"]), ("opencode", ["opencode"]), ("qwen", ["qwen"]),
    ("goose", ["goose"]), ("dsh", ["dsh"]), ("team", ["claude", "codex", "opencode"])])
def test_every_agent_image_guards_its_commands(name, agents):
    """sandbox-ai build AGENT: the guard copied in and --install run after the agent is installed, before the image's
    own check of its command (which then runs through the guard)."""
    text = (ROOT / f"sandbox/Containerfile.{name}").read_text()
    copy = "COPY telemetry_guard.py /usr/local/lib/sandbox-ai/telemetry_guard.py\n"
    run = f"RUN python3 -IS /usr/local/lib/sandbox-ai/telemetry_guard.py --install {' '.join(agents)}\n"
    assert copy in text and run in text and text.index(copy) < text.index(run)
    installed = max(text.find(marker) for marker in ("npm install --global", "npm ci", "install -m 0755 goose"))
    assert 0 < installed < text.index(run)
    checks = [line for line in text.splitlines() if "--version" in line and line.startswith("RUN")]
    assert checks and all(text.index(line) > text.index(run) for line in checks[-1:])


def test_the_hermes_image_keeps_its_own_guard():
    assert "telemetry_guard" not in (ROOT / "sandbox/Containerfile.hermes").read_text()
    assert "hermes" not in guard.AGENTS


# --- the launcher's part ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def s():
    return load("telemetry_guard_launcher_part", "sandbox/agent_sandbox.py")


def test_which_sandboxes_have_which_commands(s):
    assert s.telemetry_guarded(s.parse_args(["qwen"])) == ["qwen"]
    assert s.telemetry_guarded(s.parse_args(["goose", "--binary", "/bin/bash"])) == ["goose"]  # typed in its shell
    assert s.telemetry_guarded(s.parse_args(["codex", "--with", "claude"])) == ["codex", "claude", "opencode"]
    assert s.telemetry_guarded(s.parse_args(["hermes"])) == []  # its own guard (hermes_guard.py)
    assert s.telemetry_guarded(s.parse_args(["claude", "--probe"])) == []


def test_the_managed_configurations_pin_telemetry_off(s):
    a = s.parse_args(["codex", "--with", "claude", "--telemetry", "strict"])
    files = s.telemetry_guard_files(a, {"claude-code/managed-settings.json": json.dumps(
        {"allowManagedHooksOnly": True, "hooks": {"PreToolUse": []}})})
    claude = json.loads(files["claude-code/managed-settings.json"])
    assert claude["allowManagedHooksOnly"] is True and claude["hooks"] == {"PreToolUse": []}  # the hooks stay
    assert claude["env"] == {**s.TELEMETRY_ENV["claude"]["off"], **s.TELEMETRY_ENV["claude"]["strict"]}
    assert claude["env"]["ENABLE_BETA_TRACING_DETAILED"] == "0" and claude["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "0"
    codex = tomllib.loads(files["codex/managed_config.toml"])
    assert codex == {"check_for_update_on_startup": False, "analytics": {"enabled": False},
                     "feedback": {"enabled": False}, "features": {"plugins": False},
                     "otel": {"metrics_exporter": "none", "exporter": "none", "trace_exporter": "none"}}
    assert json.loads(files["opencode/opencode.json"]) == {"share": "disabled", "autoupdate": False,
                                                          "experimental": {"openTelemetry": False}}
    assert files["telemetry-guard/telemetry_guard.py"] == (ROOT / "sandbox/telemetry_guard.py").read_text()
    assert files["telemetry-guard/telemetry-guard.mjs"] == guard.javascript()
    off = tomllib.loads(s.telemetry_guard_files(s.parse_args(["codex"]), {})["codex/managed_config.toml"])
    assert "features" not in off
    assert set(s.telemetry_guard_files(s.parse_args(["qwen"]), {})) == {
        "telemetry-guard/telemetry_guard.py", "telemetry-guard/telemetry-guard.mjs"}  # its system settings: qwen.py


def test_each_is_mounted_once_read_only(s, monkeypatch):
    a = s.parse_args(["claude", "--with", "codex"])
    mounts = dict((target, source) for source, target in s.telemetry_guard_mounts(a, "/S"))
    assert mounts == {guard.GUARD_PY: Path("/S/telemetry-guard/telemetry_guard.py"),
                      guard.GUARD_JS: Path("/S/telemetry-guard/telemetry-guard.mjs"),
                      "/etc/claude-code": Path("/S/claude-code"), "/etc/codex": Path("/S/codex"),
                      "/etc/opencode": Path("/S/opencode")}
    b = s.parse_args(["claude", "--backend", "bwrap"])
    assert [target for _, target in s.telemetry_guard_mounts(b, "/S")] == ["/etc/claude-code"]  # the host's claude
    monkeypatch.setattr(s, "hook_mount", lambda a, session: (Path(session) / "claude-code", "/etc/claude-code"))
    assert "/etc/claude-code" not in dict((t, src) for src, t in s.telemetry_guard_mounts(a, "/S"))  # the hooks' own


def test_the_launcher_does_not_pass_what_a_guard_would_refuse(s, monkeypatch, capsys):
    """--env OTEL_EXPORTER_OTLP_ENDPOINT (or CLAUDE_CODE_ENVIRONMENT_KIND) would make every start of the agent a
    refused one: not passed, said so; what a guard pins wins over what is passed (as before)."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("CLAUDE_CODE_ENVIRONMENT_KIND", "byoc")
    monkeypatch.setenv("MY_TOOL", "x")
    env = s.agent_environment(s.parse_args(["claude", "--env", "OTEL_EXPORTER_OTLP_ENDPOINT", "--env",
                                            "CLAUDE_CODE_ENVIRONMENT_KIND", "--env", "MY_TOOL"]))
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env and "CLAUDE_CODE_ENVIRONMENT_KIND" not in env
    assert env["MY_TOOL"] == "x" and guard.refused("claude", [], env) is None
    assert ("CLAUDE_CODE_ENVIRONMENT_KIND, OTEL_EXPORTER_OTLP_ENDPOINT are not passed (telemetry is always off)"
            in capsys.readouterr().err)
    for agent in ("codex", "opencode", "qwen", "goose", "dsh"):
        assert guard.refused(agent, [], s.agent_environment(s.parse_args([agent]))) is None, agent


def test_the_plan_names_each_guarded_command(s):
    plan = s.telemetry_plan(s.parse_args(["opencode"]))
    assert plan["guarded"] == {"opencode": {"command": "/usr/local/bin/opencode",
                                            "managed": "/etc/opencode/opencode.json"}}
    assert s.telemetry_plan(s.parse_args(["goose"]))["guarded"] == {"goose": {"command": "/usr/local/bin/goose"}}


# --- end to end: the real ./sandbox-ai (only Podman stood in for, live_fakes.py) -----------------------------------

def mounts_of(host):
    run = [call for call in host.calls() if call[:1] == ["run"] or "run" in call[:3] and "--pod" in call]
    assert run, host.calls()
    return {arg.split(",dst=", 1)[1].split(",", 1)[0]: arg for arg in run[-1] if arg.startswith("type=bind,")}


@pytest.mark.parametrize("args, image, managed", [
    (["claude"], "localhost/claude-sandbox:latest", {"/etc/claude-code": "claude-code/managed-settings.json"}),
    (["codex"], "localhost/codex-sandbox:latest", {"/etc/codex": "codex/managed_config.toml"}),
    (["opencode"], "localhost/opencode-sandbox:latest", {"/etc/opencode": "opencode/opencode.json"}),
    (["qwen"], "localhost/qwen-sandbox:latest", {}),
    (["goose", "--binary", "/bin/bash"], "localhost/goose-sandbox:latest", {}),
    (["dsh"], "localhost/dsh-sandbox:latest", {}),
    (["claude", "--with", "codex"], "localhost/team-sandbox:latest",
     {"/etc/claude-code": "claude-code/managed-settings.json", "/etc/codex": "codex/managed_config.toml",
      "/etc/opencode": "opencode/opencode.json"}),
])
def test_e2e_every_agent_sandbox_gets_the_guard_read_only(tmp_path, args, image, managed):
    """`sandbox-ai AGENT` (a shell with --binary, a team): the guard's files are mounted read-only over the image's
    copies from the session directory, each managed configuration read-only at its place; run as the image runs them,
    the session's guard refuses what would turn telemetry on with the message."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image(image)
    done = host.run(*args, "--no-tmux", "--no-auth",
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(tmp_path / "kept")})
    assert done.returncode == 0, done.stderr
    mounts, session = mounts_of(host), tmp_path / "kept/session"
    for target, name in ((guard.GUARD_PY, "telemetry-guard/telemetry_guard.py"),
                         (guard.GUARD_JS, "telemetry-guard/telemetry-guard.mjs"), *managed.items()):
        assert target in mounts and mounts[target].endswith(",ro,nodev"), mounts.get(target)
        assert f"/{name.split('/')[0]}" in mounts[target].split(",dst=")[0], mounts[target]
        assert (session / name).exists(), name
    assert (session / "telemetry-guard/telemetry_guard.py").read_text() == (ROOT / "sandbox/telemetry_guard.py").read_text()
    if "/etc/claude-code" in managed:
        assert json.loads((session / "claude-code/managed-settings.json").read_text())["env"][
            "CLAUDE_CODE_ENABLE_TELEMETRY"] == "0"
    agent = args[0]
    if guard.KINDS[agent] == "exec":
        kept = tmp_path / "guard.py"
        kept.write_text((session / "telemetry-guard/telemetry_guard.py").read_text().replace(
            'REAL_DIR = GUARD_DIR + "/real"', f'REAL_DIR = "{tmp_path}"', 1))
        (tmp_path / agent).write_text("#!/bin/sh\necho ran\n")
        (tmp_path / agent).chmod(0o755)
        refused = subprocess.run([sys.executable, "-IS", str(kept), agent], capture_output=True, text=True,
                                 env={"PATH": "/usr/bin:/bin", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"})
        assert refused.returncode == 2 and refused.stderr.startswith(guard.message(agent)), refused.stderr
        ran = subprocess.run([sys.executable, "-IS", str(kept), agent], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin"})
        assert ran.returncode == 0 and ran.stdout == "ran\n", ran.stderr
    elif NODE:
        entry = tmp_path / "entry.mjs"
        entry.write_text(guard.import_line(agent, str(session / "telemetry-guard/telemetry-guard.mjs")) +
                         '\nconsole.log("ran");\n')
        refused = subprocess.run([NODE, str(entry)], capture_output=True, text=True,
                                 env={"PATH": os.environ["PATH"], "OTEL_EXPORTER_OTLP_ENDPOINT": "http://x"})
        assert refused.returncode == 2 and refused.stderr.startswith(guard.message(agent)), refused.stderr


def test_e2e_the_launcher_leaves_out_what_the_guard_would_refuse(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image("localhost/opencode-sandbox:latest")
    done = host.run("opencode", "--no-tmux", "--no-auth", "--env", "OTEL_EXPORTER_OTLP_ENDPOINT",
                    env={"FAKE_PODMAN_RUN": "0:0", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"})
    assert done.returncode == 0, done.stderr
    assert "OTEL_EXPORTER_OTLP_ENDPOINT is not passed (telemetry is always off)" in done.stderr
    assert "collector:4318" not in json.dumps(host.calls())


# --- with the agents themselves (opt-in: their pinned versions, installed outside the repository) ---------------------

class Collector(http.server.BaseHTTPRequestHandler):
    """A local OTLP collector: what reaches it, by path."""
    received = []

    def do_POST(self):  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("content-length") or 0))
        Collector.received.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def collector():
    Collector.received = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", Collector.received
    server.shutdown()


def exec_guard(tmp_path, agent, program):
    """The exec form with PROGRAM (the real agent) as REAL_DIR/AGENT: [argv prefix] of the guarded command."""
    real = tmp_path / "real"
    real.mkdir(exist_ok=True)
    if not (real / agent).is_symlink():
        (real / agent).symlink_to(program)
    kept = tmp_path / "telemetry_guard.py"
    kept.write_text((ROOT / "sandbox/telemetry_guard.py").read_text().replace(
        'REAL_DIR = GUARD_DIR + "/real"', f'REAL_DIR = "{real}"', 1))
    return [sys.executable, "-IS", str(kept), agent]


def node_entry(tmp_path, agent, entry):
    """A copy of AGENT's real ENTRY beside it (its relative imports hold) with the guard's import, as --install does
    in the image: (the copy's path, cleanup)."""
    (tmp_path / "telemetry-guard.mjs").write_text(guard.javascript())
    copy = Path(entry).with_name(f"sandbox-ai-test-{os.getpid()}-{Path(entry).name}")
    first, _, rest = Path(entry).read_text().partition("\n")
    copy.write_text(f"{first}\n{guard.import_line(agent, str(tmp_path / 'telemetry-guard.mjs'))}\n{rest}")
    return copy


CLAUDE = os.environ.get("SANDBOX_AI_TEST_CLAUDE")  # Claude Code's binary (claude.exe)


def claude_env(tmp_path, guarded=False, **extra):
    """Claude Code with a HOME of its own and a closed API port; unguarded, it exports every half second (the
    guard refuses those OpenTelemetry variables too)."""
    home = tmp_path / "claude-home"
    home.mkdir(exist_ok=True)
    quick = {} if guarded else {"OTEL_METRIC_EXPORT_INTERVAL": "500", "OTEL_LOGS_EXPORT_INTERVAL": "500"}
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
            "CLAUDE_CODE_MAX_RETRIES": "0", **quick, **extra}


@pytest.mark.skipif(not CLAUDE, reason="SANDBOX_AI_TEST_CLAUDE: Claude Code's binary (2.1.322)")
def test_with_claude_code_the_beta_tracing_is_refused_and_the_managed_env_wins(tmp_path, collector):
    """Claude Code itself: its beta tracing exports to a collector of the agent's with every launcher pin set (the
    gap); the guard refuses it. A settings file's "env" - as the managed settings carry the pins - wins over the
    variables Claude Code was started with (measured with --settings, the layer just below the managed one)."""
    url, received = collector
    pins = {**guard.pinned("claude")}
    beta = {"ENABLE_BETA_TRACING_DETAILED": "1", "BETA_TRACING_ENDPOINT": url}

    def run(command, env):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL,
                                  env=env)
        except subprocess.TimeoutExpired as stopped:  # still trying the closed API port: what it wrote so far
            output = [part.decode() if isinstance(part, bytes) else part or "" for part in (stopped.stdout, stopped.stderr)]
            return subprocess.CompletedProcess(command, None, *output)
    run([CLAUDE, "-p", "hi"], claude_env(tmp_path, **{**pins, **beta}))
    assert received, "Claude Code no longer exports its beta tracing: the pin may be dropped"
    received.clear()
    done = run([*exec_guard(tmp_path, "claude", CLAUDE), "-p", "hi"], claude_env(tmp_path, True, **beta))
    assert done.returncode == 2 and received == []
    assert done.stderr == f"{guard.message('claude')} (BETA_TRACING_ENDPOINT={url} is refused: telemetry is always off)\n"
    managed_env = json.dumps({"env": pins})
    shown = run([CLAUDE, "--settings", managed_env, "-p", "hi"],
                claude_env(tmp_path, CLAUDE_CODE_ENABLE_TELEMETRY="1", OTEL_METRICS_EXPORTER="console"))
    assert "claude_code.session.count" not in shown.stdout + shown.stderr
    shown = run([CLAUDE, "-p", "hi"], claude_env(tmp_path, CLAUDE_CODE_ENABLE_TELEMETRY="1",
                                                 OTEL_METRICS_EXPORTER="console"))
    assert "claude_code.session.count" in shown.stdout + shown.stderr  # the control: on without the settings
    done = run([*exec_guard(tmp_path, "claude", CLAUDE), "--version"], claude_env(tmp_path, True))
    assert done.returncode == 0 and "Claude Code" in done.stdout, done.stderr


QWEN = os.environ.get("SANDBOX_AI_TEST_QWEN_PACKAGE")  # node_modules/@qwen-code/qwen-code at 0.26.4


@pytest.mark.skipif(not (QWEN and NODE), reason="SANDBOX_AI_TEST_QWEN_PACKAGE: Qwen Code 0.26.4's package")
def test_with_qwen_code_its_telemetry_stays_off(tmp_path):
    """Qwen Code itself: ~/.qwen/settings.json turning telemetry on (target local, an outfile) writes its OpenTelemetry
    events there without the guard; through the guarded entry nothing is written, its options and variables are
    refused, and it still runs."""
    home = tmp_path / "home"
    (home / ".qwen").mkdir(parents=True)
    outfile = tmp_path / "telemetry.log"
    (home / ".qwen/settings.json").write_text(json.dumps({"telemetry": {"enabled": True, "target": "local",
                                                                        "outfile": str(outfile)},
                                                          "security": {"auth": {"selectedType": "openai"}}}))
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "OPENAI_API_KEY": "x", "OPENAI_MODEL": "m",
           "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
    run = lambda entry, *args, **extra: subprocess.run(  # noqa: E731
        [NODE, str(entry), *args], capture_output=True, text=True, timeout=120, env={**env, **extra})
    entry = Path(QWEN) / "cli-entry.js"
    run(entry, "-p", "hi")
    assert outfile.exists(), "Qwen Code no longer writes its telemetry from the user's settings"
    outfile.unlink()
    guarded = node_entry(tmp_path, "qwen", entry)
    try:
        run(guarded, "-p", "hi")
        assert not outfile.exists()
        for args, extra in ((["--telemetry", "-p", "hi"], {}), (["-p", "hi"], {"QWEN_USAGE_STATISTICS_ENABLED": "true"})):
            done = run(guarded, *args, **extra)
            assert done.returncode == 2 and done.stderr.startswith(guard.message("qwen")), done.stderr
        done = run(guarded, "--version")
        assert done.returncode == 0 and done.stdout.strip() == "0.26.4", done.stderr
    finally:
        guarded.unlink()


CODEX = os.environ.get("SANDBOX_AI_TEST_CODEX_PACKAGE")  # node_modules/@openai/codex


@pytest.mark.skipif(not (CODEX and NODE), reason="SANDBOX_AI_TEST_CODEX_PACKAGE: Codex's npm package")
def test_with_codex_its_telemetry_overrides_are_refused(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home)}
    guarded = node_entry(tmp_path, "codex", Path(CODEX) / "bin/codex.js")
    try:
        run = lambda *args: subprocess.run([NODE, str(guarded), *args], capture_output=True, text=True,  # noqa: E731
                                           timeout=120, env=env)
        assert run("--version").returncode == 0
        for args in (["-c", "analytics.enabled=true", "--version"], ["-c", 'otel.exporter="otlp-http"', "--version"]):
            done = run(*args)
            assert done.returncode == 2 and done.stderr.startswith(guard.message("codex")), done.stderr
        assert run("-c", "analytics.enabled=false", "-c", 'otel.exporter="none"', "--version").returncode == 0
    finally:
        guarded.unlink()


DSH = os.environ.get("SANDBOX_AI_TEST_DSH_PACKAGE")  # node_modules/@deepseek-ai/dsh at 0.2.1-rc.5


@pytest.mark.skipif(not (DSH and NODE), reason="SANDBOX_AI_TEST_DSH_PACKAGE: DSH 0.2.1-rc.5's package")
def test_with_dsh_a_typed_dsh_gets_the_patch_and_the_pins(tmp_path):
    """DSH itself: `dsh headless` typed without the launcher's patch composes its telemetry rows enabled; through the
    guarded entry the patch is its last overlay (both rows disabled) and a variable of the collector is refused."""
    home = tmp_path / "home"
    home.mkdir()
    patch = tmp_path / "sandbox-ai.patch.yml"
    dsh = load("telemetry_guard_dsh_rows", "sandbox/dsh.py")
    patch.write_text(json.dumps(dsh.TELEMETRY_PINNED))
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "DSH_HOME": str(home / ".dsh")}
    entry = Path(DSH) / "lib/bin.js"
    plain = subprocess.run([NODE, str(entry), "headless", "--dump-config"], capture_output=True, text=True,
                           timeout=120, env=env)
    assert plain.returncode == 0 and "disabled: true" not in plain.stdout.split("- id: otel", 1)[1].split("- id:", 2)[1]
    guarded = node_entry(tmp_path, "dsh", entry)
    (tmp_path / "telemetry-guard.mjs").write_text(
        guard.javascript().replace(json.dumps(guard.DSH_PATCH), json.dumps(str(patch)), 1))
    try:
        done = subprocess.run([NODE, str(guarded), "headless", "--dump-config"], capture_output=True, text=True,
                              timeout=120, env=env)
        assert done.returncode == 0, done.stderr
        rows = done.stdout.split("- id: otel\n", 1)[1]
        assert rows.split("- id:", 1)[0].strip().endswith("disabled: true")  # the OTel service the rows need
        telemetry = re.split(r"\n(?=- |#)", rows.split("- id: session-telemetry-otel\n", 1)[1])[0]
        assert telemetry.rstrip().endswith("\n  disabled: true")
        done = subprocess.run([NODE, str(guarded), "headless", "x"], capture_output=True, text=True, timeout=120,
                              env={**env, "DSH_TELEMETRY_OTLP_URL": "http://127.0.0.1:4318/v1/logs"})
        assert done.returncode == 2 and done.stderr.startswith(guard.message("dsh")), done.stderr
    finally:
        guarded.unlink()


OPENCODE = os.environ.get("SANDBOX_AI_TEST_OPENCODE")  # OpenCode 1's binary (opencode.exe)


@pytest.mark.skipif(not OPENCODE, reason="SANDBOX_AI_TEST_OPENCODE: OpenCode 1.19.5's binary")
def test_with_opencode_its_managed_configuration_wins_and_otlp_is_refused(tmp_path, collector):
    """OpenCode itself: a collector of the agent's gets its logs and traces from OTEL_EXPORTER_OTLP_ENDPOINT, with
    OTEL_SDK_DISABLED=true (the gap); the guard refuses it and the variable that would move its managed directory.
    The managed opencode.json the launcher writes wins over OPENCODE_CONFIG_CONTENT (read here through that variable,
    the one way to point OpenCode at another directory - which is why it is refused)."""
    url, received = collector
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
           "XDG_STATE_HOME": str(home / ".local/state"), "OPENCODE_DISABLE_MODELS_FETCH": "1",
           "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    run = lambda command, timeout=120, **extra: subprocess.run(  # noqa: E731
        command, capture_output=True, text=True, timeout=timeout, cwd=work, env={**env, **extra},
        stdin=subprocess.DEVNULL)
    provider = json.dumps({"provider": {"local": {"npm": "@ai-sdk/openai-compatible", "models": {"m": {}},
                                                  "options": {"baseURL": "http://127.0.0.1:9/v1", "apiKey": "x"}}}})
    try:
        run([OPENCODE, "run", "-m", "local/m", "hi"], timeout=40, OTEL_SDK_DISABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT=url, OPENCODE_CONFIG_CONTENT=provider)
    except subprocess.TimeoutExpired:
        pass  # it retries the closed model's port; the export has happened by then
    assert received, "OpenCode no longer exports to OTEL_EXPORTER_OTLP_ENDPOINT"
    guarded = exec_guard(tmp_path, "opencode", OPENCODE)
    for extra in ({"OTEL_EXPORTER_OTLP_ENDPOINT": url}, {"OPENCODE_TEST_MANAGED_CONFIG_DIR": str(tmp_path)}):
        done = run([*guarded, "--version"], **extra)
        assert done.returncode == 2 and done.stderr.startswith(guard.message("opencode")), done.stderr
    assert run([*guarded, "--version"]).stdout.strip() == "1.19.5"
    s = load("telemetry_guard_opencode_managed", "sandbox/agent_sandbox.py")
    managed = tmp_path / "etc-opencode"
    managed.mkdir()
    (managed / "opencode.json").write_text(s.telemetry_guard_files(s.parse_args(["opencode"]), {})[
        "opencode/opencode.json"])
    shown = run([OPENCODE, "debug", "config"], OPENCODE_TEST_MANAGED_CONFIG_DIR=str(managed),
                OPENCODE_CONFIG_CONTENT=json.dumps({"autoupdate": True, "experimental": {"openTelemetry": True}}))
    config = json.loads(shown.stdout[shown.stdout.index("{"):])
    assert (config["share"], config["autoupdate"], config["experimental"]["openTelemetry"]) == ("disabled", False, False)


GOOSE = os.environ.get("SANDBOX_AI_TEST_GOOSE")  # Goose 1.57.0's binary


@pytest.mark.skipif(not GOOSE, reason="SANDBOX_AI_TEST_GOOSE: Goose 1.57.0's binary")
def test_with_goose_an_otlp_endpoint_of_its_own_exports_nothing(tmp_path, collector):
    """Goose itself: its ~/.config/goose/config.yaml naming a collector gets logs, traces and metrics unless
    OTEL_SDK_DISABLED=true; through the guard (which pins it) nothing arrives, and its telemetry switches as
    variables are refused."""
    url, received = collector
    home = tmp_path / "home"
    (home / ".config/goose").mkdir(parents=True)
    (home / ".config/goose/config.yaml").write_text(f"otel_exporter_otlp_endpoint: {url}\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "GOOSE_DISABLE_KEYRING": "1", "GOOSE_TELEMETRY_OFF": "1",
           "GOOSE_PROVIDER": "openai", "GOOSE_MODEL": "m", "OPENAI_API_KEY": "x", "OPENAI_HOST": "http://127.0.0.1:9"}
    run = lambda command, **extra: subprocess.run(command, capture_output=True, text=True, timeout=120,  # noqa: E731
                                                  env={**env, **extra}, stdin=subprocess.DEVNULL)
    run([GOOSE, "run", "--no-session", "-t", "hi"])
    assert received, "Goose no longer exports to its configured OTLP endpoint"
    received.clear()
    guarded = exec_guard(tmp_path, "goose", GOOSE)
    run([*guarded, "run", "--no-session", "-t", "hi"])
    assert received == []
    for extra in ({"GOOSE_TELEMETRY_ENABLED": "true"}, {"OTEL_EXPORTER_OTLP_ENDPOINT": url}):
        done = run([*guarded, "--version"], **extra)
        assert done.returncode == 2 and done.stderr.startswith(guard.message("goose")), done.stderr
    assert run([*guarded, "--version"]).stdout.strip() == "1.57.0"
