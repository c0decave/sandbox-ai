"""sandbox-ai's shell scripts need bash 4 or later: on PATH, else Homebrew's (macOS ships bash 3.2)."""
import importlib.util
import os
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_main_for_bash", ROOT / "sandbox/main.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def test_the_bash_on_path_when_it_is_new_enough(monkeypatch):
    monkeypatch.setattr(m.os, "access", lambda path, mode: True)
    assert m.find_bash(lambda name: "/usr/bin/bash", lambda path: (5, 2)) == "/usr/bin/bash"


def test_macos_bash_3_gives_way_to_homebrews(monkeypatch):
    monkeypatch.setattr(m.os, "access", lambda path, mode: True)
    versions = {"/bin/bash": (3, 2), "/opt/homebrew/bin/bash": (5, 2)}
    assert m.find_bash(lambda name: "/bin/bash", versions.get) == "/opt/homebrew/bin/bash"
    versions = {"/bin/bash": (3, 2), "/usr/local/bin/bash": (5, 1)}  # Intel Homebrew
    assert m.find_bash(lambda name: "/bin/bash", versions.get) == "/usr/local/bin/bash"


def test_without_a_new_enough_bash_it_says_what_to_install(monkeypatch):
    monkeypatch.setattr(m.os, "access", lambda path, mode: path == "/bin/bash")
    with pytest.raises(SystemExit, match="brew install bash"):
        m.find_bash(lambda name: "/bin/bash", lambda path: (3, 2))
    with pytest.raises(SystemExit, match="bash is required"):
        m.find_bash(lambda name: None, lambda path: None)


def test_the_real_bash_here():
    executable = shutil.which("bash")
    if not executable:
        pytest.skip("no bash")
    assert m.bash_version(executable) >= (4, 0) and m.find_bash() == m.find_bash()
    assert m.bash_version("/nonexistent/bash") is None


def test_build_all_builds_every_image_and_goes_on_after_a_failure(tmp_path, monkeypatch):
    """`sandbox-ai build all` builds ALL of sandbox-ai's images - the seven agents, team, Cairn, browser, VPN, Tor and
    every variant of yours - each on its own, so one failure stops none of the others."""
    import json
    import subprocess
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    variant = tmp_path / "sandbox-ai" / "images" / "tor"
    variant.mkdir(parents=True)
    (variant / "image.json").write_text(json.dumps({"version": 1, "agent": "dsh", "apt": [], "npm": [], "pip": [],
                                                    "files": []}))
    monkeypatch.setattr(m, "find_bash", lambda: "/bin/bash")
    calls, said = [], []

    def run(command, check=False, env=None):
        calls.append(command[2:])
        assert (env or {}).get("SANDBOX_AI_BUILD_CGROUP_SAID") == (None if len(calls) == 1 else "1")
        return subprocess.CompletedProcess(command, 1 if "qwen" in command else 0)
    assert m.build_all_wanted(["-pu", "unix:///x", "all", "--no-cache"])
    assert not m.build_all_wanted(["qwen"]) and not m.build_all_wanted(["--variant", "all"])
    assert m.build_all(["-pu", "unix:///x", "--uv", "0.9", "all", "--no-cache"], run=run, out=said.append) == 1
    names = [call[call.index("--variant") + 1] if "--variant" in call else call[-2] for call in calls]
    assert names == ["codex", "claude", "opencode", "hermes", "dsh", "qwen", "goose", "team", "cairn", "browser",
                     "vpn", "tor", "tor"]
    assert all(call[-1] == "--no-cache" for call in calls)
    assert calls[0][:4] == ["--podman-url", "unix:///x", "--uv", "0.9"]
    assert calls[9] == ["browser", "--no-cache"]  # the browser's build takes none of those options
    assert calls[12][-3:] == ["--variant", "tor", "--no-cache"]
    assert "12 of 13 built; failed: qwen" in said[-1]
    assert m.build_all(["--pin", "1.2.3", "all"], run=run, out=said.append) == 2
