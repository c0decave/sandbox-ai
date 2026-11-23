"""OpenCode 2 end to end on a user's host: the real ./sandbox-ai entry point as a subprocess with only Podman, tmux and
npm faked (tests/live_fakes.py). Its dry run, its refusals, its build, a start up to the pod it makes (the HOME it
prepares: the shared background service off), its presets, and OpenCode 1 left as it was."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from live_fakes import Host  # noqa: E402


@pytest.fixture
def host():
    directory = tempfile.mkdtemp(prefix="oc2-e2e-", dir=os.environ.get("TMPDIR") or "/tmp")
    found = Host(directory)
    found.add_image("localhost/opencode2-sandbox:latest", "localhost/opencode-sandbox:latest")
    yield found
    subprocess.run(["rm", "-rf", directory], check=False)


def refused(host, *args):
    done = host.run(*args, timeout=180)
    assert done.returncode == 1, (args, done.stdout[-500:], done.stderr[-1500:])
    assert host.stopped_at() is None, "refused before anything starts"
    return done.stderr


def plan(host, *args):
    done = host.run(*args, "--dry-run", timeout=180)
    assert done.returncode == 0, (args, done.stderr[-1500:])
    return json.loads(done.stdout)


def test_the_dry_run_of_opencode2_and_opencode_1_beside_it(host):
    found = plan(host, "opencode2")
    assert found["agent"] == "opencode2" and found["image"] == "localhost/opencode2-sandbox:latest"
    assert found["https_hosts"] == ["opencode.ai"]
    section = found["opencode2"]
    assert section["version"] == "2.2.5" and "experimental" not in section and section["telemetry"]["switchable"] is False
    assert section["background_service"] is False and section["never"] == ["opentunnel.xyz"]
    one = plan(host, "opencode")
    assert one["image"] == "localhost/opencode-sandbox:latest" and one.get("opencode2") is None
    found = plan(host, "opencode2", "-oe", "plugins,models-catalog")
    assert set(found["https_hosts"]) == {"opencode.ai", "registry.npmjs.org", "models.opencode.ai"}
    assert [entry["feature"] for entry in found["opencode2"]["enabled"]] == ["plugins", "models-catalog"]


@pytest.mark.parametrize("args,why", [
    (["opencode2", "--", "upgrade"], "new review"),
    (["opencode2", "--", "pair", "--remote", "--yes"], "internet"),
    (["opencode2", "--", "plugin", "add", "opencode-acme"], "--opencode2-enable plugins"),
    (["opencode2", "--", "serve", "--hostname", "0.0.0.0"], "loopback only"),
    (["opencode2", "--opencode2-enable", "telemetry"], "not an OpenCode 2 feature"),
    (["opencode2", "--allow-host", "bun.report"], "telemetry is always off"),
    (["opencode2", "--allow-host", "opentunnel.xyz"], "telemetry is always off"),
    (["opencode", "--opencode2-enable", "updates"], "is for OpenCode 2"),
    (["opencode2", "--effort", "high"], "a model's variants"),
])
def test_what_a_sandbox_refuses(host, args, why):
    at = args.index("--") if "--" in args else len(args)
    assert why in refused(host, *args[:at], "--dry-run", *args[at:])


def test_the_mode_paranoid_holds_its_tool_rules_through_sandbox_ai_s_plugin(host):
    """Through ./sandbox-ai: the mode paranoid, whose ruleset has rules for the agent's own tools, starts OpenCode 2
    (no refusal any more)."""
    found = plan(host, "opencode2", "--mode", "paranoid", "--no-keep")
    assert found["agent"] == "opencode2" and found["mode"]["name"] == "paranoid"


def test_opencode2_is_a_companion_in_the_team_image_with_its_layer(host):
    """Since 0.1.98: --with opencode2 takes the team image with OpenCode 2's layer (built when it is missing)."""
    done = host.run("claude", "--with", "opencode2", "--dry-run", timeout=120,
                    env={"SANDBOX_AI_NO_AUTO_BUILD": "1"})
    assert done.returncode != 0 and "sandbox-ai build team --with opencode2" in done.stderr, done.stderr[-2000:]
    host.add_image("localhost/team-opencode2-sandbox:latest")
    done = host.run("claude", "--with", "opencode2", "--dry-run", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    plan = json.loads(done.stdout)
    assert plan["image"] == "localhost/team-opencode2-sandbox:latest" and plan["opencode2"] is not None
    refused = host.run("opencode", "--with", "opencode2", "--dry-run", timeout=120)
    assert refused.returncode != 0 and "OpenCode 2 and OpenCode" in refused.stderr


def test_a_start_prepares_its_home_and_gets_as_far_as_its_pod(host):
    state = host.directory / "state"
    done = host.run("opencode2", "--no-tmux", "--state-dir", str(state), "-D", "--model", "anthropic/claude-x",
                    timeout=240)
    stopped = host.stopped_at()
    assert stopped is not None and "create" in stopped, (done.returncode, done.stderr[-2000:])
    assert "localhost/opencode2-sandbox:latest" in stopped
    assert json.loads((state / ".config/opencode/service.json").read_text()) == {"disabled": True}
    assert "live-tested" not in done.stderr


def test_build_opencode2_takes_the_reviewed_release(host):
    done = host.run("build", "opencode2", timeout=240)
    assert done.returncode == 0, done.stderr[-2000:]
    build = host.builds()[-1]
    assert "OPENCODE2_VERSION=2.2.5" in build and any(arg.endswith("Containerfile.opencode2") for arg in build)
    refused_build = host.run("build", "--pin", "2.3.0", "opencode2", timeout=120)
    assert refused_build.returncode == 2 and "reviewed, 2.2.5" in refused_build.stderr


def test_the_presets_and_the_help(host):
    for name in ("default-opencode2", "coding-opencode2"):
        found = plan(host, "opencode2", "--preset", name)
        assert found["agent"] == "opencode2" and "opencode.ai" in found["https_hosts"], name
    assert "sandbox-ai llm add" in refused(host, "opencode2", "--preset", "opencode2-local-llm", "--dry-run")
    assert host.run("llm", "add", "ollama", "--kind", "ollama", "--url", "http://127.0.0.1:11434").returncode == 0
    found = plan(host, "opencode2", "--preset", "opencode2-local-llm", "--preset-value", "llm=ollama:qwen3-coder")
    assert found["https_hosts"] == [] and found["llm"]["config"] == "/etc/opencode2/sandbox-ai-llm.json"
    helped = host.run("opencode2", "--help", timeout=120)
    assert helped.returncode == 0 and "--opencode2-enable" in helped.stdout and "-oe" in helped.stdout
