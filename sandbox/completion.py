#!/usr/bin/env python3
"""sandbox-ai completion: completion for bash, zsh and fish.

  sandbox-ai completion bash|zsh|fish      the script for that shell (install.sh puts it where the shell finds it)
  sandbox-ai completion install [SHELL]    write the scripts for your shells now (the same as install.sh does)

The scripts ask `sandbox-ai __complete WORDS...` for the candidates, so they always know the commands and options
of the sandbox-ai that runs: every command and subcommand, each option and its choices (--mode, --telemetry,
--backend ...), and the names you have: starts (attach, stop, resume ...), running sandboxes (net, devices, workspace,
canaries), hosts, MCP servers, LLM servers, VPN profiles, kits and secrets. Paths are left to the shell.

Standard library only.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve().parent
FILES, DIRS = "__files__", "__dirs__"  # tell the script to complete paths
AGENTS = ("claude", "codex", "opencode")
OTHER_COMMANDS = ("build", "update", "version", "help")
PATH_HINTS = ("FILE", "DIR", "PATH", "KEY", "CERT")


class Captured(Exception):
    def __init__(self, parser):
        super().__init__("parser")
        self.parser = parser


def module(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_completion", HERE / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def commands():
    """{command: (script, prefix...)} of `sandbox-ai`, as main.py runs them."""
    return dict(module("main").PYTHON_COMMANDS)


def parser_of(command):
    """The argparse parser COMMAND's script builds, caught when it parses: the script runs as it would, up to
    its parse_args (nothing after that), with its output thrown away."""
    table = commands()
    if command not in table:
        return None
    script, *prefix = table[command]
    if script == "agent_sandbox.py":
        return module("agent_sandbox").build_parser()
    original = argparse.ArgumentParser.parse_args, argparse.ArgumentParser.parse_known_args

    def capture(self, *args, **kwargs):
        raise Captured(self)
    argv = sys.argv
    argparse.ArgumentParser.parse_args = argparse.ArgumentParser.parse_known_args = capture
    sys.argv = [str(HERE / script), *prefix, "--help"]
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runpy.run_path(str(HERE / script), run_name="__main__")
    except Captured as found:
        parser = found.parser
        # A prefix (starts.py list, attach ...) is the script's own first word: complete below it.
        for word in prefix:
            sub = subparsers(parser).get(word)
            if sub is None:
                break
            parser = sub
        return parser
    except BaseException:  # noqa: BLE001 - a script that cannot be read gets no completion
        return None
    finally:
        argparse.ArgumentParser.parse_args, argparse.ArgumentParser.parse_known_args = original
        sys.argv = argv
    return None


def subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def options(parser):
    return {option: action for action in parser._actions for option in action.option_strings}


def positionals(parser):
    return [a for a in parser._actions if not a.option_strings and not isinstance(a, argparse._SubParsersAction)]


# --- the names you have ---------------------------------------------------------------------------------

def quietly(function):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return list(function())
    except Exception:  # noqa: BLE001 - completion never fails the shell
        return []


def start_names():
    return quietly(lambda: [e["name"] for e in module("starts").entries(prune=False)])


def run_names():
    def names():
        for run, record in module("net").running().items():
            yield run
            if record.get("name"):
                yield record["name"]
    return quietly(names)


def host_names():
    return quietly(lambda: [e["name"] for e in module("hosts").entries()])


def mcp_names():
    return quietly(lambda: sorted(module("mcp").definitions()))


def llm_names():
    return quietly(lambda: sorted(module("llm").load_store().get("endpoints", {})))


def vpn_names():
    return quietly(lambda: module("vpn").list_profiles())


def kit_names():
    return quietly(lambda: sorted(set(module("kits").available_kits())))


def question_ids():
    return quietly(lambda: [q.get("id") for _, _, q in module("approvals").questions() if q.get("id")])


def team_names():
    return quietly(lambda: sorted(p.name for p in module("team").teams_dir().iterdir() if p.is_dir()))


# (command, subcommand or None, positional dest) -> what it names
NAMED = {
    ("attach", None, "name"): start_names, ("pause", None, "name"): start_names,
    ("resume", None, "name"): start_names, ("stop", None, "name"): start_names,
    ("delete", None, "name"): start_names, ("rename", None, "old"): start_names,
    ("list", None, "history"): start_names,
    ("net", "*", "run"): run_names, ("devices", "*", "run"): run_names, ("workspace", "list", "run"): run_names,
    ("limits", None, "run"): run_names,
    ("canaries", "*", "run"): run_names, ("activity", None, "run"): run_names,
    ("hosts", "*", "name"): host_names, ("mcp", "*", "name"): mcp_names, ("llm", "*", "name"): llm_names,
    ("vpn", "*", "name"): vpn_names, ("kits", "*", "name"): kit_names,
    ("approvals", "allow", "id"): question_ids, ("approvals", "deny", "id"): question_ids,
    ("team", "*", "name"): team_names, ("approvals", "history", "run"): run_names,
    ("hosts", "update", "names"): host_names, ("mcp", "attach", "start"): start_names,
    ("mcp", "detach", "start"): start_names,
}
# Secrets are not listed: reading their names may ask the keyring to unlock, which a Tab must never do.
VALUES = {"--kit": kit_names, "--mcp": mcp_names, "--llm": llm_names, "--vpn": vpn_names,
          "--session": start_names, "--name": start_names, "--for": start_names, "--host": host_names, "--helper": lambda: list(AGENTS),
          "--with": lambda: list(AGENTS)}


def named(command, sub, dest):
    function = NAMED.get((command, sub, dest)) or NAMED.get((command, "*", dest))
    return function() if function else []


def value_candidates(action, option):
    if action.choices:
        return [str(c) for c in action.choices]
    if option in VALUES:
        return VALUES[option]()
    metavar = str(action.metavar or action.dest or "").upper()
    if action.type is Path or any(hint in metavar for hint in PATH_HINTS) or option in ("--workspace", "-w"):
        return [DIRS] if "DIR" in metavar or option in ("--workspace", "-w", "--state-dir", "--kit-dir") else [FILES]
    return []


def complete(words):
    """Candidates for the last of WORDS (the words after `sandbox-ai`; the last may be empty)."""
    *done, current = words or [""]
    if not done:
        return sorted(c for c in {*commands(), *OTHER_COMMANDS} if c.startswith(current) and not c.startswith("_"))
    command = done[0]
    if command == "completion":
        return [c for c in ("bash", "zsh", "fish", "install") if c.startswith(current)] if len(done) == 1 else []
    parser = parser_of(command)
    if parser is None:
        return [FILES]
    sub, used, expecting, after_dashes = None, 0, None, False
    for word in done[1:]:
        if expecting is not None:
            expecting = None
            continue
        if after_dashes:
            continue
        if word == "--":
            after_dashes = True
            continue
        if word.startswith("-"):
            action = options(parser).get(word.split("=", 1)[0])
            if action is not None and action.nargs != 0 and "=" not in word and not isinstance(
                    action, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._CountAction,
                             argparse._StoreConstAction, argparse._AppendConstAction, argparse._HelpAction)):
                expecting = action
            continue
        children = subparsers(parser)
        if children and word in children:
            parser, sub, used = children[word], word, 0
            continue
        used += 1
    if after_dashes:
        return [FILES]  # the agent's own arguments
    if expecting is not None:
        option = next(o for o in expecting.option_strings if o.startswith("--")) if any(
            o.startswith("--") for o in expecting.option_strings) else expecting.option_strings[0]
        return [c for c in value_candidates(expecting, option) if c in (FILES, DIRS) or c.startswith(current)]
    if current.startswith("-"):
        if "=" in current:
            option, _, partial = current.partition("=")
            action = options(parser).get(option)
            return [f"{option}={c}" for c in (value_candidates(action, option) if action else [])
                    if c not in (FILES, DIRS) and c.startswith(partial)]
        return sorted(o for o in options(parser) if o.startswith(current))
    children = subparsers(parser)
    if children:
        return sorted(c for c in children if c.startswith(current))
    candidates = []
    places = positionals(parser)
    if used < len(places):
        candidates = named(command, sub, places[used].dest) or value_candidates(places[used], places[used].dest)
    elif places and places[-1].nargs in ("*", "+", argparse.REMAINDER):
        candidates = named(command, sub, places[-1].dest)
    return [c for c in candidates if c in (FILES, DIRS) or c.startswith(current)]


# --- the shell scripts ------------------------------------------------------------------------------------

BASH = r'''# sandbox-ai completion for bash (sandbox-ai completion bash)
_sandbox_ai() {
    local current="${COMP_WORDS[COMP_CWORD]}" line
    local -a words=("${COMP_WORDS[@]:1:COMP_CWORD-1}" "$current")
    COMPREPLY=()
    while IFS= read -r line; do
        case "$line" in
            __files__) COMPREPLY+=($(compgen -f -- "$current")); compopt -o filenames 2>/dev/null ;;
            __dirs__) COMPREPLY+=($(compgen -d -- "$current")); compopt -o filenames 2>/dev/null ;;
            *) COMPREPLY+=("$line") ;;
        esac
    done < <(SANDBOX_AI_NO_DEPRECATION=1 command sandbox-ai __complete -- "${words[@]}" 2>/dev/null)
}
complete -o default -F _sandbox_ai sandbox-ai
'''

ZSH = r'''#compdef sandbox-ai
# sandbox-ai completion for zsh (sandbox-ai completion zsh)
_sandbox_ai() {
    local -a found words_before
    local line
    words_before=("${(@)words[2,CURRENT-1]}" "${words[CURRENT]}")
    found=("${(@f)$(SANDBOX_AI_NO_DEPRECATION=1 command sandbox-ai __complete -- "${words_before[@]}" 2>/dev/null)}")
    if (( ${found[(I)__dirs__]} )); then
        _files -/
    elif (( ${found[(I)__files__]} )); then
        _files
    elif (( ${#found} )) && [[ -n "${found[1]}" ]]; then
        compadd -Q -- "${found[@]}"
    fi
}
if [[ "${zsh_eval_context[-1]}" == loadautofunc ]]; then
    _sandbox_ai "$@"
else
    compdef _sandbox_ai sandbox-ai
fi
'''

FISH = r'''# sandbox-ai completion for fish (sandbox-ai completion fish)
function __sandbox_ai_complete
    set -l words (commandline -opc)[2..-1] (commandline -ct)
    for line in (env SANDBOX_AI_NO_DEPRECATION=1 sandbox-ai __complete -- $words 2>/dev/null)
        switch $line
            case __files__
                __fish_complete_path (commandline -ct)
            case __dirs__
                __fish_complete_directories (commandline -ct)
            case '*'
                echo $line
        end
    end
end
complete -c sandbox-ai -f -a '(__sandbox_ai_complete)'
'''

SCRIPTS = {"bash": BASH, "zsh": ZSH, "fish": FISH}


def data_home():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def targets():
    """Where each shell finds them without a line in its rc file (zsh needs its fpath: install.sh adds it)."""
    return {"bash": data_home() / "bash-completion/completions/sandbox-ai",
            "zsh": data_home() / "zsh/site-functions/_sandbox-ai",
            "fish": config_home() / "fish/completions/sandbox-ai.fish"}


def install(shells=None, out=print):
    written = []
    for shell, path in targets().items():
        if shells and shell not in shells:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError(f"{path} is a symlink; remove it first")
        path.write_text(SCRIPTS[shell])
        written.append(path)
        out(f"{shell}: {path}")
    return written


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["__complete"]:
        words = argv[2:] if argv[1:2] == ["--"] else argv[1:]
        for candidate in complete(words):
            print(candidate)
        return 0
    parser = argparse.ArgumentParser(prog="sandbox-ai completion", description=__doc__.split("\n\n")[0])
    parser.add_argument("shell", choices=["bash", "zsh", "fish", "install"])
    parser.add_argument("only", nargs="*", default=[], help="with install: these shells only")
    a = parser.parse_args(argv)
    if any(shell not in SCRIPTS for shell in a.only):
        parser.error("the shells are bash, zsh and fish")
    if a.shell == "install":
        try:
            install(a.only or None)
        except ValueError as exc:
            print(f"sandbox-ai completion: {exc}", file=sys.stderr)
            return 1
        if not a.only or "zsh" in a.only:
            print("zsh: needs fpath+=(~/.local/share/zsh/site-functions) before compinit in ~/.zshrc "
                  "(install.sh adds it)")
        return 0
    sys.stdout.write(SCRIPTS[a.shell])
    return 0


if __name__ == "__main__":
    sys.exit(main())
