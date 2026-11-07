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
