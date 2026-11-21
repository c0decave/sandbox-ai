"""Rules for the newer agents' own tools end to end: the real ./sandbox-ai entry point with Podman faked
(tests/live_fakes.py). A start with a ruleset that has tool rules gets as far as its sandbox, and the session
directory it hands the sandbox holds the agent's hooks in the agent's own configuration, mounted read-only where the
agent reads it; OpenHands and nanobot are refused before anything starts, saying what their release lacks.

What these cannot show (no image here): the agent itself running the hook and a refused call reaching it - see the
live tests listed in docs/releases."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_fakes import Host  # noqa: E402

RULES = {"version": 1, "tool": [{"id": "no-curl", "tool": "Bash", "args": {"command": {"pattern": r".*\bcurl\b.*"}},
                                 "decision": "deny"}]}
# Each agent: (the file of its configuration, in the session directory, that names its rule hook; where the
# sandbox reads that directory).
INSTALLED = {
    "qwen": ("qwen/settings.json", "/etc/qwen-code"),
    "hermes": ("hermes/config.yaml", "/etc/hermes"),
    "pi": ("pi/sandbox-ai.js", "/etc/pi"),
    "opencode2": ("opencode2/sandbox-ai-hooks/server.js", "/etc/opencode2"),
    "dsh": ("dsh/sandbox-ai-hooks.mjs", "/etc/dsh"),
    "letta": ("letta/sandbox-ai-start.json", "/etc/letta"),
    "goose": ("goose/sandbox-ai-hooks/hooks/hooks.json", "/etc/goose"),
}


@pytest.fixture
def host():
    directory = tempfile.mkdtemp(prefix="rules-e2e-", dir=os.environ.get("TMPDIR") or "/tmp")
    found = Host(directory)
    found.add_image(*(f"localhost/{agent}-sandbox:latest" for agent in (*INSTALLED, "openhands", "nanobot")))
    rules = Path(directory) / "rules.json"
    rules.write_text(json.dumps(RULES))
    rules.chmod(0o600)
    found.rules = rules
    yield found
    subprocess.run(["rm", "-rf", directory], check=False)


@pytest.mark.parametrize("agent", sorted(INSTALLED))
def test_a_start_with_tool_rules_installs_the_agents_hooks(host, agent):
    kept = host.directory / "kept"
    done = host.run(agent, "--rules", host.rules, "--no-tmux", "--no-keep", timeout=300,
                    env={"FAKE_PODMAN_RUN": "0", "FAKE_PODMAN_KEEP": str(kept)})
    assert "cannot be enforced" not in done.stderr and "own tools" not in done.stderr, done.stderr[-2000:]
    session = kept / "session"
    assert session.is_dir(), (done.returncode, done.stderr[-2000:])
    config = json.loads((session / "config.json").read_text())
    assert config["hooks"]["agent"] == agent and config["hooks"]["rules"][0]["tool"][0]["id"] == "no-curl"
    path, target = INSTALLED[agent]
    text = (session / path).read_text()
    assert f"--internal-hook {agent}" in text or '"--internal-hook", "' + agent + '"' in text
    runs = [call for call in host.calls() if call[:1] == ["run"] and "--pod" in " ".join(call)]
    mounts = " ".join(" ".join(call) for call in runs)
    assert f"dst={target},ro" in mounts, f"{target} read-only"


@pytest.mark.parametrize("agent,missing", [("openhands", "takes hooks only with each conversation"),
                                           ("nanobot", "hooks only for Python programs")])
def test_agents_without_a_mechanism_are_refused_before_anything_starts(host, agent, missing):
    done = host.run(agent, "--rules", host.rules, "--no-tmux", timeout=180)
    assert done.returncode == 1 and missing in done.stderr and host.stopped_at() is None


def test_the_mode_paranoid_starts_the_newer_agents(host):
    """The mode paranoid's ruleset holds rules for the agent's own tools: its dry run no longer refuses them."""
    for agent in INSTALLED:
        done = host.run(agent, "--mode", "paranoid", "--no-keep", "--dry-run", timeout=180)
        assert done.returncode == 0, (agent, done.stderr[-1500:])


def test_a_shell_in_a_newer_agents_image_is_refused_while_rules_hold(host):
    """With --binary the start gets none of the agent's configuration: an agent typed in that shell would run
    without the rules, so such a start is refused (Claude Code and Codex read theirs from the image's paths)."""
    for agent in ("qwen", "goose"):
        done = host.run(agent, "--rules", host.rules, "--binary", "/bin/bash", "--no-tmux", timeout=180)
        assert done.returncode == 1 and "which --binary does not get" in done.stderr, done.stderr[-1500:]
        assert host.stopped_at() is None


def test_the_hook_refuses_a_call_that_names_one_argument_twice():
    """Regression (security review 1), through the hook's real entry point (what each agent's hook runs in the
    sandbox): Pi's write with its own `path` and Claude Code's `file_path` beside it is refused before any rule is
    asked - the rules saw file_path, Pi wrote path."""
    root = Path(__file__).resolve().parents[1]
    call = {"tool": "write", "args": {"path": "/etc/x", "file_path": "/work/ok", "content": "y"}, "cwd": "/work"}
    done = subprocess.run([sys.executable, "-I", str(root / "sandbox" / "runtime.py"), "--internal-hook", "pi",
                           "PreToolUse"], input=json.dumps(call), capture_output=True, text=True, timeout=60)
    assert done.returncode == 2, done.stderr
    assert "the tool call is refused" in done.stderr and "file_path twice" in done.stderr
