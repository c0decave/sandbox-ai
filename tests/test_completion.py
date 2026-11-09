"""sandbox-ai completion: the candidates (sandbox/completion.py, from the commands' own argparse parsers), and the
scripts in real shells: bash (always), zsh and fish where they are installed (SANDBOX_AI_TEST_ZSH and
SANDBOX_AI_TEST_FISH name other binaries)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("completion_for_tests", ROOT / "sandbox" / "completion.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                      ("XDG_RUNTIME_DIR", "run")):
        (tmp_path / sub).mkdir(mode=0o700)
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SANDBOX_AI_NO_DEPRECATION", "1")
    return tmp_path


@pytest.mark.parametrize("words, expected, absent", [
    ([""], {"claude", "codex", "opencode", "net", "approvals", "build", "completion"}, {"__complete"}),
    (["cl"], {"claude"}, {"codex"}),
    (["claude", "--mo"], {"--mode", "--mount"}, {"--memory"}),
    (["claude", "--mode", ""], {"lax", "low", "medium", "high", "paranoid"}, set()),
    (["claude", "--mode", "h"], {"high"}, {"lax"}),
    (["claude", "--mode=p"], {"--mode=paranoid"}, set()),
    (["claude", "--backend", ""], {"podman", "bwrap"}, set()),
    (["claude", "-w", ""], {c.DIRS}, set()),
    (["claude", "--rules", ""], {c.FILES}, set()),
    (["claude", "--", ""], {c.FILES}, set()),
    (["net", ""], {"allow", "revoke", "show", "list", "stop", "untaint"}, set()),
    (["net", "allow", "--allow-g"], {"--allow-github", "--allow-gitlab"}, {"--allow-host"}),
    (["approvals", "allow", "--scope", ""], {"once", "run", "always"}, set()),
    (["doctor", "--"], {"--json", "--backend", "--no-color"}, set()),
    (["doctor", "--backend", ""], {"podman", "bwrap", "both"}, set()),
    (["llm", "add", "x", "--kind", ""], {"ollama", "lmstudio", "vllm"}, set()),
    (["kits", ""], {"show", "new", "add-skill", "trust-hooks"}, set()),
    (["list", "--"], {"--all", "--json", "--history", "--flat", "--no-color", "--color"}, set()),
    (["ps", "--"], {"--all", "--json", "--history", "--flat", "--no-color", "--color"}, set()),
    (["ps", "--color", ""], {"auto", "always", "never"}, set()),
    (["list", "--color="], {"--color=auto", "--color=always", "--color=never"}, set()),
    (["completion", ""], {"bash", "zsh", "fish", "install"}, set()),
    (["nothing", ""], {c.FILES}, set()),
])
def test_candidates(words, expected, absent):
    found = set(c.complete(words))
    assert expected <= found, found
    assert not (absent & found), found


def test_the_names_you_have(home, monkeypatch):
    (home / "config/sandbox-ai").mkdir()
    (home / "config/sandbox-ai/mcp.json").write_text(json.dumps({"tools": {"command": ["python3", "-V"]}}))
    assert "tools" in c.complete(["mcp", "stop", ""]) or "tools" in c.complete(["claude", "--mcp", ""])
    assert "tools" in c.complete(["claude", "--mcp", ""])
    monkeypatch.setitem(c.NAMED, ("attach", None, "name"), lambda: ["web", "api", "worker"])
    assert c.complete(["attach", "w"]) == ["web", "worker"]
    assert c.complete(["claude", "--with", ""]) == ["claude", "codex", "opencode"]


def test_a_failing_lookup_completes_nothing(monkeypatch):
    """A broken state file, a missing module: the Tab completes nothing rather than printing a traceback."""
    original = c.module

    def broken(name):
        if name in ("starts", "net", "mcp"):
            raise RuntimeError("no state")
        return original(name)
    monkeypatch.setattr(c, "module", broken)
    assert c.start_names() == [] and c.run_names() == [] and c.mcp_names() == []
    assert c.complete(["attach", ""]) == [] or c.complete(["attach", ""]) == [c.FILES]


def test_the_command_line(home):
    main = ROOT / "sandbox" / "main.py"
    done = subprocess.run([sys.executable, "-I", str(main), "__complete", "--", "net", "a"], capture_output=True,
                          text=True, timeout=300)
    assert done.stdout.split() == ["allow"], done.stderr
    for shell in ("bash", "zsh", "fish"):
        script = subprocess.run([sys.executable, "-I", str(main), "completion", shell], capture_output=True,
                                text=True, timeout=300).stdout
        assert "__complete" in script and "sandbox-ai" in script
    done = subprocess.run([sys.executable, "-I", str(main), "completion", "install"], capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, done.stderr
    for path in c.targets().values():
        assert path.read_text() and str(path).startswith(str(home)), path
    assert subprocess.run([sys.executable, "-I", str(main), "completion", "install", "tcsh"], capture_output=True,
                          timeout=300).returncode == 2


@pytest.fixture
def on_path(tmp_path, monkeypatch):
    """`sandbox-ai` on PATH, as install.sh puts it there."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sandbox-ai").write_text(f"#!/bin/sh\nexec {sys.executable} -I {ROOT / 'sandbox/main.py'} \"$@\"\n")
    (bin_dir / "sandbox-ai").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    (tmp_path / "work").mkdir()
    (tmp_path / "work/src").mkdir()
    (tmp_path / "work/notes.txt").write_text("x")
    return tmp_path


def test_bash(on_path):
    script = c.BASH + '''
cd "$1"
COMP_WORDS=(sandbox-ai claude --mo); COMP_CWORD=2; _sandbox_ai; echo "A:${COMPREPLY[*]}"
COMP_WORDS=(sandbox-ai net al); COMP_CWORD=2; _sandbox_ai; echo "B:${COMPREPLY[*]}"
COMP_WORDS=(sandbox-ai claude -w work/s); COMP_CWORD=3; _sandbox_ai; echo "C:${COMPREPLY[*]}"
COMP_WORDS=(sandbox-ai ''); COMP_CWORD=1; _sandbox_ai; echo "D:${#COMPREPLY[@]}"
complete -p sandbox-ai
'''
    done = subprocess.run(["bash", "-c", script, "bash", str(on_path)], capture_output=True, text=True, timeout=120)
    lines = done.stdout.splitlines()
    assert lines[0] == "A:--mode --mode-at-least --model --mount", done.stderr
    assert lines[1] == "B:allow"
    assert lines[2] == "C:work/src", "directories for -w"
    assert int(lines[3][2:]) > 25, "every command"
    assert lines[4].startswith("complete -o default -F _sandbox_ai sandbox-ai")


def shell(variable, name, *candidates):
    found = os.environ.get(variable) or shutil.which(name) or next(
        (str(p) for p in map(Path, candidates) if p.is_file()), None)
    if not found:
        pytest.skip(f"{name} is not installed ({variable} names one)")
    return found


def test_zsh(on_path):
    zsh = shell("SANDBOX_AI_TEST_ZSH", "zsh")
    # compadd, _files and compdef stand in for zsh's completion system: they show what the function asks for.
    script = 'compdef() { echo "compdef $*"; }\n' + c.ZSH + '''
compadd() { shift; echo "compadd ${(j: :)@}"; }
_files() { echo "_files $*"; }
cd "$1"
words=(sandbox-ai claude --mo); CURRENT=3; _sandbox_ai
words=(sandbox-ai claude --mode h); CURRENT=4; _sandbox_ai
words=(sandbox-ai claude -w ''); CURRENT=4; _sandbox_ai
words=(sandbox-ai claude --rules ''); CURRENT=4; _sandbox_ai
'''
    done = subprocess.run([zsh, "-f", "-c", script, "zsh", str(on_path)], capture_output=True, text=True, timeout=120)
    assert done.stdout.splitlines() == ["compdef _sandbox_ai sandbox-ai", "compadd -- --mode --mount",
                                        "compadd -- high", "_files -/", "_files "], done.stderr


def test_fish(on_path, tmp_path):
    fish = shell("SANDBOX_AI_TEST_FISH", "fish")
    completions = tmp_path / "completions.fish"
    completions.write_text(c.FISH)
    script = f'''source {completions}
cd {on_path}
complete -C "sandbox-ai claude --mo"
echo ---
complete -C "sandbox-ai net al"
echo ---
complete -C "sandbox-ai claude -w work/s"
'''
    done = subprocess.run([fish, "--no-config", "-c", script], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "fish-config")})
    parts = [p.strip().splitlines() for p in done.stdout.split("---")]
    assert [line.split("\t")[0] for line in parts[0]] == ["--mode", "--mount"], (done.stdout, done.stderr)
    assert [line.split("\t")[0] for line in parts[1]] == ["allow"]
    assert [line.split("\t")[0] for line in parts[2]] == ["work/src/"]
