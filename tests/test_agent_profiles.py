"""Agent image selection and network separation, without host credentials."""
import importlib.util
import ipaddress
import json
import shutil
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_profiles", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)
HOSTS = {
    "codex": ["api.openai.com", "chatgpt.com", "auth.openai.com"],
    "claude": ["api.anthropic.com", "console.anthropic.com", "claude.ai", "claude.com", "platform.claude.com"],
    "opencode": ["opencode.ai"],
}
PACKAGES = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code", "opencode": "opencode-ai"}


@pytest.mark.parametrize("agent", HOSTS)
def test_launcher_selects_agent_image_and_preserves_explicit_override(agent):
    args = s.parse_args([agent])
    assert args.image == f"localhost/{agent}-sandbox:latest"
    create, run = s.pod_commands(args, "owned", Path("/tmp/session"), Path("/tmp/home"))
    assert create[create.index("--infra-image") + 1] == args.image
    assert run[run.index("--entrypoint=/usr/bin/python3") + 1] == args.image
    assert s.parse_args([agent, "--image", "localhost/custom:reviewed"]).image == "localhost/custom:reviewed"


@pytest.mark.parametrize("agent", HOSTS)
def test_generated_firewall_and_hosts_only_include_selected_agent(agent, tmp_path, monkeypatch):
    # Distinct fixture addresses make accidental cross-provider permissions visible.
    addresses = {host: str(ipaddress.ip_address("203.0.113.1") + i)
                 for i, host in enumerate(host for hosts in HOSTS.values() for host in hosts)}
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(s, "prepare_home", lambda *args: [])
    def resolve(args, hosts):
        assert hosts == HOSTS[agent]
        assert args.image == f"localhost/{agent}-sandbox:latest"
        return {host: [addresses[host]] for host in hosts}
    monkeypatch.setattr(s, "resolve_pod_hosts", resolve)
    def run(args, session, home):
        rules = (session / "firewall.nft").read_text()
        pinned = (session / "hosts").read_text()
        for host, address in addresses.items():
            assert (f" {host}\n" in pinned) == (host in HOSTS[agent])
            assert (f"ip daddr {address} tcp dport 443 accept" in rules) == (host in HOSTS[agent])
        assert rules.count("policy drop") == 3
        return 0
    monkeypatch.setattr(s, "run_pod", run)
    assert s.main([agent, "--workspace", str(tmp_path / "work"), "--no-auth"]) == 0


@pytest.mark.parametrize("agent", HOSTS)
def test_explicit_host_extension_is_local_to_invocation(agent):
    args = s.parse_args([agent, "--allow-host", "extra.example.org", "--allow-host", HOSTS[agent][0]])
    assert s.allowed_hosts(args) == [*HOSTS[agent], "extra.example.org"]
    for other in HOSTS:
        assert s.allowed_hosts(s.parse_args([other])) == HOSTS[other]
    assert s.allowed_hosts(s.parse_args([agent, "--no-provider-defaults", "--allow-host", "extra.example.org"])) == ["extra.example.org"]


@pytest.fixture
def fake_podman(tmp_path, monkeypatch):
    executable = tmp_path / "podman"
    log = tmp_path / "build.json"
    executable.write_text("#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ['BUILD_LOG']).write_text(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":/usr/bin:/bin")
    monkeypatch.setenv("BUILD_LOG", str(log))
    return log


@pytest.mark.parametrize("agent", HOSTS)
@pytest.mark.parametrize("remote", [False, True])
def test_builder_selects_one_recipe_and_image(agent, remote, fake_podman):
    options = ["--podman-url", "unix:///tmp/test-engine.sock"] if remote else []
    version = f"{agent.upper()}_VERSION=1.2.3"
    result = subprocess.run([str(ROOT / "sandbox-build-image"), *options, agent, "--build-arg", version], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(fake_podman.read_text()) == [
        *(["--remote", "--url", "unix:///tmp/test-engine.sock"] if remote else []),
        "build", "--tag", f"localhost/{agent}-sandbox:latest", "--file",
        str(ROOT / "sandbox" / f"Containerfile.{agent}"), "--pull=newer", "--label", "sandbox-ai.agent-version=1.2.3",
        "--build-arg", version, str(ROOT / "sandbox")]
    recipe = (ROOT / "sandbox" / f"Containerfile.{agent}").read_text()
    assert f'"{PACKAGES[agent]}@${{{agent.upper()}_VERSION}}"' in recipe
    assert f"RUN {agent} --version" in recipe
    for other, package in PACKAGES.items():
        if other != agent:
            assert package not in recipe
            assert f"{other.upper()}_VERSION" not in recipe
    assert "build-essential" not in recipe
    assert "ARG NODE_IMAGE=docker.io/library/node:lts-bookworm-slim" in recipe
    assert "FROM ${NODE_IMAGE}" in recipe and "node:22" not in recipe
    assert "python3-pip python3-venv" in recipe
    for package in ("xvfb", "x11vnc", "x11-utils", "fonts-liberation", "libnss3", "libgbm1", "libgtk-3-0", "libasound2"):
        assert f" {package} " in recipe, package
    for package in ("git", "vim", "netcat-traditional", "bind9-dnsutils", "iputils-ping", "iproute2", "traceroute",
                    "python3", "curl", "wget", "openssh-client", "jq", "openssl"):
        assert f" {package} " in recipe, package
    # gh from GitHub's signed apt repository, checked at build time.
    assert "signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages" in recipe
    assert "apt-get install -y --no-install-recommends gh " in recipe and "RUN gh --version" in recipe
    assert "FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv" in recipe
    assert "COPY --from=uv /uv /uvx /usr/local/bin/" in recipe
    assert "RUN python3 -m pip --version && uv --version" in recipe
    assert not (ROOT / "sandbox/Containerfile").exists()


@pytest.mark.parametrize("options", [[], ["unknown"], ["--build-arg", "CODEX_VERSION=latest"], ["--podman-url"],
                                     ["--podman-url", "unix:///tmp/engine"], ["--pin"], ["--pin", "1.2.3", "all"],
                                     ["--pin", "1.2; rm -rf /", "codex"], ["--uv", "x y", "codex"],
                                     ["codex", "--build-arg", "CODEX_VERSION=latest"],
                                     ["codex", "--build-arg=CODEX_VERSION="],
                                     ["--pin", "1.0.0", "codex", "--build-arg", "CODEX_VERSION=2.0.0"]])
def test_builder_rejects_missing_or_unknown_agent_before_podman(options, fake_podman):
    result = subprocess.run([str(ROOT / "sandbox-build-image"), *options], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Usage:" in result.stderr or "sandbox-build-image:" in result.stderr
    assert not fake_podman.exists()


@pytest.fixture
def fake_npm(tmp_path):
    executable = tmp_path / "npm"
    executable.write_text("#!/usr/bin/python3\nimport sys\nprint({'@openai/codex': '9.1.0', '@anthropic-ai/claude-code': '9.2.0', 'opencode-ai': '9.3.0'}[sys.argv[2]])\n")
    executable.chmod(0o700)


@pytest.mark.parametrize("agent", HOSTS)
def test_builder_pins_latest_release_and_refreshes_base(agent, fake_podman, fake_npm):
    result = subprocess.run([str(ROOT / "sandbox-build-image"), agent], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    version = {"codex": "9.1.0", "claude": "9.2.0", "opencode": "9.3.0"}[agent]
    assert json.loads(fake_podman.read_text()) == [
        "build", "--tag", f"localhost/{agent}-sandbox:latest", "--file",
        str(ROOT / "sandbox" / f"Containerfile.{agent}"), "--pull=newer",
        "--build-arg", f"{agent.upper()}_VERSION={version}", "--label", f"sandbox-ai.agent-version={version}",
        str(ROOT / "sandbox")]


def test_builder_all_builds_every_image(tmp_path, fake_npm, monkeypatch):
    log = tmp_path / "builds.log"
    (tmp_path / "podman").write_text(f"#!/bin/sh\necho \"$*\" >> {log}\n")
    (tmp_path / "podman").chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":/usr/bin:/bin")
    result = subprocess.run([str(ROOT / "sandbox-build-image"), "all"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = log.read_text().splitlines()
    assert [line.split()[2] for line in lines] == [f"localhost/{a}-sandbox:latest" for a in ("codex", "claude", "opencode")]
    assert "CLAUDE_VERSION=9.2.0" in lines[1]


def test_builder_without_npm_queries_registry_with_python(tmp_path, monkeypatch):
    log = tmp_path / "build.json"
    (tmp_path / "podman").write_text("#!/usr/bin/python3\nimport json,os,sys\nopen(os.environ['BUILD_LOG'],'w').write(json.dumps(sys.argv[1:]))\n")
    # Stand-in python3: answers only for the exact unencoded registry URL.
    (tmp_path / "python3").write_text("#!/bin/sh\n[ \"$4\" = https://registry.npmjs.org/@anthropic-ai/claude-code/latest ] && echo 7.7.7\n")
    for name in ("podman", "python3"):
        (tmp_path / name).chmod(0o700)
    # Only the tools the script needs: on usr-merged hosts /bin is /usr/bin and
    # would expose a real npm, bypassing the registry fallback under test.
    for tool in ("readlink", "dirname"):
        found = shutil.which(tool)
        assert found, f"test needs {tool} on PATH"
        (tmp_path / tool).symlink_to(found)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("BUILD_LOG", str(log))
    result = subprocess.run(["/bin/bash", str(ROOT / "sandbox-build-image"), "claude"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "CLAUDE_VERSION=7.7.7" in json.loads(log.read_text())


def test_builder_rejects_unexpected_version_before_building(tmp_path, monkeypatch, fake_podman):
    (tmp_path / "npm").write_text("#!/bin/sh\necho '1.0.0; rm -rf /'\n")
    (tmp_path / "npm").chmod(0o700)
    result = subprocess.run([str(ROOT / "sandbox-build-image"), "codex"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "unexpected version" in result.stderr
    assert not fake_podman.exists()


def test_builder_all_resolves_every_version_before_building(tmp_path, fake_podman):
    (tmp_path / "npm").write_text("#!/bin/sh\n[ \"$2\" = @anthropic-ai/claude-code ] && exit 1\necho 1.2.3\n")
    (tmp_path / "npm").chmod(0o700)
    result = subprocess.run([str(ROOT / "sandbox-build-image"), "all"], capture_output=True, text=True)
    assert result.returncode != 0
    assert not fake_podman.exists(), "no image may be rebuilt when any version lookup fails"


def test_builder_refuses_removed_update_option(fake_podman):
    """--update was a no-op kept for compatibility; it is gone like any unknown option."""
    result = subprocess.run([str(ROOT / "sandbox-build-image"), "--update", "claude"], capture_output=True, text=True)
    assert result.returncode == 2 and "codex|claude|opencode" in result.stderr
    assert "--update" not in subprocess.run([str(ROOT / "sandbox-build-image"), "--help"],
                                            capture_output=True, text=True).stdout
    assert not fake_podman.exists()


def test_builder_help_does_not_build(fake_podman):
    result = subprocess.run([str(ROOT / "sandbox-build-image"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "codex|claude|opencode" in result.stdout
    assert not fake_podman.exists()


@pytest.mark.parametrize("scenario", ["latest", "pinned", "bases"])
def test_builder_defaults_to_newest_and_pins_on_request(scenario, fake_podman, fake_npm):
    """Without flags every build resolves the newest agent; --pin/--uv/--node fix versions."""
    options = {"latest": ["claude"], "pinned": ["--pin", "1.0.42", "claude"],
               "bases": ["--uv", "0.5.11", "--node", "docker.io/library/node@sha256:abc", "claude"]}[scenario]
    result = subprocess.run([str(ROOT / "sandbox-build-image"), *options], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    args = json.loads(fake_podman.read_text())
    version = "1.0.42" if scenario == "pinned" else "9.2.0"
    assert f"CLAUDE_VERSION={version}" in args and f"sandbox-ai.agent-version={version}" in args
    assert ("(pinned)" in result.stderr) == (scenario == "pinned")
    if scenario == "bases":
        assert "UV_VERSION=0.5.11" in args and "NODE_IMAGE=docker.io/library/node@sha256:abc" in args
