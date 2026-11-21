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
    (["llm", "st"], {"status"}, {"show"}),
    (["llm", "status", "--"], {"--json", "--watch"}, set()),
    # the use cases of the local-model research (model_usecases.json): llm recommend's and a start's --use-case
    (["llm", "recommend", "--"], {"--use-case", "--server", "--llm", "--agent", "--host", "--json"}, set()),
    (["llm", "recommend", "untr"], {"untrusted-input"}, set()),
    (["llm", "recommend", "--use-case", ""], {"coding", "sandbox", "code-review", "vision"}, set()),
    (["claude", "--use-case", "co"], {"coding", "code-review"}, {"vision"}),
    (["llm", "set", "x", "--"], {"--memory"}, set()),
    (["kits", ""], {"show", "new", "add-skill", "trust-hooks"}, set()),
    (["list", "--"], {"--all", "--json", "--history", "--flat", "--no-color", "--color"}, set()),
    (["ps", "--"], {"--all", "--json", "--history", "--flat", "--no-color", "--color"}, set()),
    (["ps", "--color", ""], {"auto", "always", "never"}, set()),
    (["list", "--color="], {"--color=auto", "--color=always", "--color=never"}, set()),
    (["completion", ""], {"bash", "zsh", "fish", "install"}, set()),
    (["build", ""], {"claude", "pi", "opencode2", "all", "team", "cairn", "vpn", "tor", "browser"}, {c.FILES}),
    (["build", "cl"], {"claude"}, {"codex", "all"}),
    (["build", "claude", ""], {"codex", "team", "all"}, {c.FILES}),
    (["build", "--"], {"--pin", "--uv", "--node", "--podman-url", "--extension-only", "--variant"}, set()),
    (["build", "-"], {"-p", "-u", "-n", "-e", "-pu"}, set()),
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
    # Every agent (0.1.98: the others are layers of the team image), as the launcher's --with takes them.
    assert c.complete(["claude", "--with", ""]) == sorted(c.AGENTS)
    assert c.complete(["claude", "--with", "o"]) == ["opencode", "opencode2", "openhands"]
    # a comma list (--with hermes,qwen): the next name after the last comma, never one already named
    assert c.complete(["claude", "--with", "hermes,q"]) == ["hermes,qwen"]
    assert "hermes,hermes" not in c.complete(["claude", "--with", "hermes,"])
    assert "hermes,codex" in c.complete(["claude", "--with", "hermes,"])


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


# --- every position of every parser has a completion -------------------------------------------------------------

def every_parser():
    """[(command, subcommand or None, parser)] of every command of sandbox-ai, its subcommands too."""
    import argparse
    found = []

    def walk(command, sub, parser):
        found.append((command, sub, parser))
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                hidden = {choice.dest for choice in action._choices_actions if choice.help == argparse.SUPPRESS}
                seen = set()
                for name, child in action.choices.items():
                    if id(child) not in seen and name not in hidden:
                        seen.add(id(child))
                        walk(command, name, child)
    for command in sorted(c.commands()):
        if command in c.INTERNAL_COMMANDS or command.startswith("_"):
            continue
        parser = c.parser_of(command)
        assert parser is not None, f"sandbox-ai {command}: its parser cannot be read"
        walk(command, None, parser)
    for (command, word), make in c.OWN_PARSERS.items():
        walk(command, word, make())
    return found


def test_every_parser_position_has_a_completion():
    """Each positional and each option that takes a value: a provider (the names you have, choices, a path) or an
    entry in completion.FREE_TEXT saying why nothing can be offered - so a new command cannot miss its completion."""
    missing = []
    for command, sub, parser in every_parser():
        for action in parser._actions:
            if c.provider(command, sub, action) is None:
                name = action.option_strings[-1] if action.option_strings else action.dest
                missing.append(f"{command} {sub or ''} {name} ({action.metavar})")
    assert not missing, "no completion (add a provider, or say why in FREE_TEXT):\n" + "\n".join(missing)


def test_free_text_lists_only_what_exists():
    """FREE_TEXT names no position that is gone (a stale entry would hide nothing, but lies)."""
    options, positionals = set(), set()
    for command, sub, parser in every_parser():
        for action in parser._actions:
            if action.option_strings:
                options.update(action.option_strings)
            else:
                positionals.update({(command, sub, action.dest), (command, "*", action.dest)})
    stale = [key for key in c.FREE_TEXT if (key not in options if isinstance(key, str) else key not in positionals)]
    assert not stale, stale


@pytest.mark.parametrize("words, expected, absent", [
    (["net", "cut", ""], {"remote", "local", "api", "all", "off"}, set()),
    (["net", "cut", "web", ""], {"remote", "local", "api", "all", "off"}, set()),
    (["workspace", "add", ""], {c.FILES}, set()),
    (["workspace", "add", "web", ""], {c.FILES}, set()),
    (["workspace", "remove", ""], {c.FILES}, set()),
    (["devices", "add", ""], {c.FILES}, set()),
    (["completion", "install", ""], {"bash", "zsh", "fish"}, {"install"}),
    (["completion", "install", "bash", "z"], {"zsh"}, {"bash"}),
    (["images", "extend", ""], {"claude", "codex", "pi"}, set()),
    (["--no-color", "net", ""], {"allow", "list"}, set()),
    (["--color", ""], {"auto", "always", "never"}, set()),
    (["--"], {"--help", "--version", "--no-color", "--color"}, set()),
    (["net", "list", "--no-c"], {"--no-color"}, set()),
    (["claude", "--with", ""], {"claude", "codex", "opencode", "hermes", "pi", "qwen"}, set()),  # every agent (0.1.98)
    (["llm", "profiles", "qwen"], {"qwen/qwen3-coder-next"}, set()),
])
def test_the_gaps_found_on_2026_11_16(words, expected, absent):
    found = set(c.complete(words))
    assert expected <= found, found
    assert not (absent & found), found


def test_the_names_of_the_gaps(monkeypatch):
    monkeypatch.setattr(c, "start_names", lambda: ["web", "api"])
    monkeypatch.setattr(c, "run_names", lambda: ["web"])
    monkeypatch.setattr(c, "host_names", lambda: ["box"])
    monkeypatch.setattr(c, "bridge_ids", lambda: ["b-1234", "b-5678"])
    monkeypatch.setattr(c, "identity_names", lambda: ["work", "private"])
    monkeypatch.setattr(c, "git_login_urls", lambda: ["https://github.com/me"])
    monkeypatch.setattr(c, "web_users", lambda: ["alice"])
    monkeypatch.setattr(c, "variant_names", lambda: ["slim"])
    assert set(c.complete(["bridge", "add", ""])) == {"web", "api", "box:"}
    assert set(c.complete(["bridge", "add", "web", ""])) == {"web", "api", "box:"}
    assert c.complete(["bridge", "show", "b-1"]) == ["b-1234"]
    assert set(c.complete(["bridge", "revoke", ""])) == {"b-1234", "b-5678"}
    assert set(c.complete(["bridge", "resume", ""])) == {"b-1234", "b-5678"}
    assert set(c.complete(["workspace", "add", ""])) == {"web", c.FILES}
    assert c.complete(["workspace", "add", "web", ""]) == [c.FILES]
    assert set(c.complete(["net", "cut", ""])) == {"web", "remote", "local", "api", "all", "off"}
    assert "web" not in c.complete(["net", "cut", "web", ""])
    assert set(c.complete(["devices", "add", ""])) == {"web", c.FILES}
    assert c.complete(["identities", "show", ""]) == ["private", "work"] or \
        set(c.complete(["identities", "show", ""])) == {"work", "private"}
    assert set(c.complete(["claude", "--identity", ""])) == {"work", "private"}
    assert c.complete(["git-logins", "remove", ""]) == ["https://github.com/me"]
    assert c.complete(["git-logins", "give", ""]) == ["web"]
    assert c.complete(["web", "user", "reset", ""]) == ["alice"]
    assert c.complete(["secrets", "give", ""]) == ["web"]
    assert c.complete(["session", "move", "web", ""]) == ["box"]
    assert set(c.complete(["session", "stop", ""])) == {"web", "api"}
    assert "slim" in c.complete(["claude", "--image-variant", ""])
    assert "slim" in c.complete(["images", "check", ""])
    assert c.complete(["list", "--history", "w"]) == ["web"]
    assert c.complete(["secrets", "list", "--run", ""]) == ["web"]


def test_with_follows_the_team_image():
    """--with: the agents of the team image, as the launcher's NOT_IN_TEAM says (never a list of its own)."""
    launcher = c.module("agent_sandbox")
    assert set(c.VALUES["--with"]()) == {a for a in c.AGENTS if a not in launcher.NOT_IN_TEAM}


def test_completion_install_adds_a_block_and_says_how_a_running_shell_gets_it(home, monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(c.sys, "platform", "linux")
    (home / ".bashrc").write_text("# mine\n")
    assert c.main(["install"]) == 0
    said = capsys.readouterr().out
    bashrc = (home / ".bashrc").read_text()
    assert bashrc.startswith("# mine\n# >>> sandbox-ai completion >>>\n") and bashrc.count(">>> sandbox-ai") == 1
    assert "source ~/.local/share/bash-completion/completions/sandbox-ai" in said.replace(str(home), "~") or \
        "source " in said
    assert "cannot change a shell that already runs" in said
    found = subprocess.run(["bash", "-i", "-c", "complete -p sandbox-ai"], capture_output=True, text=True,
                           timeout=60, env={**os.environ, "HOME": str(home)})
    assert "_sandbox_ai sandbox-ai" in found.stdout, found.stderr
    assert c.main(["install"]) == 0 and (home / ".bashrc").read_text() == bashrc, "once"
    # install.sh's own block sources it already; --no-rc leaves the files alone
    (home / ".bashrc").write_text("# >>> sandbox-ai >>>\n# <<< sandbox-ai <<<\n")
    assert c.main(["install"]) == 0 and "completion >>>" not in (home / ".bashrc").read_text()
    (home / ".bashrc").write_text("# mine\n")
    assert c.main(["install", "--no-rc"]) == 0 and (home / ".bashrc").read_text() == "# mine\n"
    assert c.main(["install", "--no-rc", "bash"]) == 0


def test_board_show_and_delete_offer_the_kept_boards(home):
    boards = home / "config/sandbox-ai/boards"
    boards.mkdir(parents=True)
    for name in ("review-night", "web-app"):
        (boards / f"{name}.json").write_text("{}")
    (boards / "Not A Board.json").write_text("{}")  # (no board's name: not offered)
    assert c.complete(["board", "show", ""]) == ["review-night", "web-app"]
    assert c.complete(["board", "delete", "w"]) == ["web-app"]
    assert c.complete(["board", "export", "--session", "x", ""]) == [c.FILES]


def test_update_and_its_mode_complete():
    """update has no argparse parser (install.sh takes most of its options): its options, `mode` and the modes."""
    assert c.complete(["update", ""]) == ["mode"]
    assert {"--apply", "--install", "--rollback", "--check", "--while-running"} <= set(c.complete(["update", "--"]))
    assert c.complete(["update", "--ro"]) == ["--rollback"]
    assert c.complete(["update", "mode", ""]) == ["off", "notify", "download", "auto"]
    assert c.complete(["update", "mode", "auto", "--"]) == ["--at", "--source", "--json", "--help"]
    assert c.complete(["update", "mode", "auto", ""]) == []
    assert "--mode" in c.complete(["hosts", "update", "ct1", "--mo"])


def test_workspace_review_completes_the_runs_whose_review_is_kept(tmp_path, monkeypatch):
    """`workspace review RUN --keep|--restore PATH`: the runs whose before-state sandbox-ai keeps (ended ones too)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    folder = tmp_path / "state" / "sandbox-ai" / "workspace-review" / "logs-0123456789abcdef"
    folder.mkdir(parents=True)
    for name in ("claude-1a2b3c4d.json", "codex-5e6f7a8b.json", ".claude-1a2b3c4d.json.tmp"):
        (folder / name).write_text("{}")
    assert set(c.complete(["workspace", "review", ""])) == {"claude-1a2b3c4d", "codex-5e6f7a8b"}
    assert c.complete(["workspace", "review", "co"]) == ["codex-5e6f7a8b"]
