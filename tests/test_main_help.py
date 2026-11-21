"""`sandbox-ai --help` lists every command of the dispatcher once, in Docker's groups (Agents, Common Commands,
Management Commands, Commands, Global Options), one short line each; the help of every command with subcommands is
laid out as `docker network --help` is, every other one as `docker run --help` (sandbox/helpstyle.py)."""
import argparse
import ast
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"
sys.path.insert(0, str(SANDBOX))
import completion  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_help_test", SANDBOX / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = load("main")
helpstyle = load("helpstyle")
SECTIONS = dict(main.HELP)


def names(section):
    return [word for name, _ in SECTIONS[section] for word in name.split(", ")]


def listed():
    return [word for title in SECTIONS if title != "Global Options" for word in names(title)]


def sandbox_ai(*words, columns="100"):
    env = {**os.environ, "COLUMNS": columns, "NO_COLOR": "1"}
    return subprocess.run([sys.executable, "-I", str(SANDBOX / "main.py"), *words], capture_output=True, text=True,
                          env=env, timeout=120)


def test_every_command_of_the_dispatcher_is_listed_once():
    known = (set(main.PYTHON_COMMANDS) | {"build", "update", "version", "help"}) - main.HIDDEN
    assert sorted(listed()) == sorted(set(listed())), "a command listed twice"
    assert set(listed()) == known, ("not listed:", known - set(listed()), "not a command:", set(listed()) - known)
    assert not set(listed()) & set(main.REMOVED)
    assert names("Agents") == list(completion.AGENTS)
    assert names("Global Options") == ["-h", "--help", "-V", "--version", "--color WHEN", "-nc", "--no-color"]


def test_the_help_is_docker_like_and_fits_80_columns():
    result = sandbox_ai("--help")
    assert result.returncode == 0 and result.stdout == main.help_text() + "\n"
    text = result.stdout
    assert text.startswith("Usage:  sandbox-ai [OPTIONS] COMMAND\n        sandbox-ai AGENT [OPTIONS] [-- AGENT ARGS]\n")
    assert [line[:-1] for line in text.splitlines() if re.fullmatch(r"[A-Z][\w ]+:", line)] == list(SECTIONS)
    assert text.rstrip().endswith("Run 'sandbox-ai COMMAND --help' for more information on a command.")
    column = None
    for line in text.splitlines():
        assert len(line) <= 80, line
        found = re.fullmatch(r"  (\S+(?:, \S+)*(?: [A-Z]+)?)( +)([A-Z].*)", line)  # (an option: with its value)
        if line.startswith("  ") and not line.startswith("    "):
            assert found, f"not one name and its description: {line!r}"
            start = len(line) - len(found.group(3))
            assert column in (None, start), f"not aligned: {line!r}"
            column = start
            assert "|" not in found.group(3) and "[" not in found.group(3), f"syntax in the overview: {line!r}"
    alone = sandbox_ai()
    assert alone.returncode == 2 and alone.stdout == text


def test_management_commands_are_the_ones_with_subcommands():
    for command in sorted(set(listed()) - {"build", "update", "version", "help"}):
        parser = completion.parser_of(command)
        assert parser is not None, command
        has_commands = any(isinstance(a, argparse._SubParsersAction) for a in parser._actions)
        assert has_commands == (command in names("Management Commands")), command


@pytest.mark.parametrize("command", [name for name, _ in dict(main.HELP)["Management Commands"]])
def test_a_management_command_s_help_is_laid_out_like_docker_network_help(command):
    parser = completion.parser_of(command)
    subcommands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    result = sandbox_ai(command, "--help")
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert re.match(rf"Usage:  sandbox-ai {command}( \[OPTIONS\])? \[?COMMAND\]?\n\n[A-Z]", text), text[:200]
    assert not text.split("\n\n")[1].startswith("sandbox-ai"), "the description repeats the command"
    assert "\nCommands:\n" in text and "\nOptions:\n" in text and "positional arguments" not in text
    assert text.rstrip().endswith(f"Run 'sandbox-ai {command} COMMAND --help' for more information on a command.")
    commands = text.split("\nCommands:\n")[1].split("\n\n")[0].splitlines()
    entries = helpstyle.command_entries(subcommands)
    assert len(commands) == len(entries), "one line for each subcommand (at 100 columns)"
    for line, (name, help_text) in zip(commands, entries):
        assert help_text and help_text[0].isupper(), (command, name)
        assert re.fullmatch(rf"  {re.escape(name)} +{re.escape(help_text)}", line), line
    hidden = {c.dest for c in subcommands._choices_actions if c.help == argparse.SUPPRESS}
    shown = {word for name, _ in entries for word in name.split(", ")}
    assert shown | hidden == set(subcommands.choices), "every subcommand is listed (or hidden on purpose)"


def test_an_error_keeps_argparse_s_usage_line_with_every_subcommand():
    result = sandbox_ai("net", "bogus")
    assert result.returncode == 2
    assert result.stderr.startswith("usage: sandbox-ai net [-h] {list,show,")
    assert "invalid choice: 'bogus'" in result.stderr


def test_help_of_a_command_and_the_subcommands_own_help():
    net = sandbox_ai("help", "net")
    assert net.returncode == 0 and net.stdout.startswith("Usage:  sandbox-ai net COMMAND\n")
    allow = sandbox_ai("net", "allow", "--help")
    assert allow.returncode == 0
    assert allow.stdout.startswith("Usage:  sandbox-ai net allow [OPTIONS] [RUN]\n\nAllow destinations while the "
                                   "sandbox runs.\n\nOptions:\n")
    assert "\n  -A, --allow-host HOST " in allow.stdout and "-A HOST, --allow-host HOST" not in allow.stdout
    # the app reads the options of a host's `net allow --help` (help_options): every one is found
    parser = completion.parser_of("net")
    allow_parser = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["allow"]
    expected = {o for a in allow_parser._actions if a.help != argparse.SUPPRESS for o in a.option_strings
                if o.startswith("--")}
    assert expected <= app_help_options(allow.stdout), expected - app_help_options(allow.stdout)
    assert allow.stdout.rstrip().endswith("Run 'sandbox-ai net --help' for the other net commands.")
    assert "\nExamples:\n  sandbox-ai net allow web -A pypi.org " in allow.stdout


def formatted(parser):
    return parser.format_help()


def test_the_formatter_itself(monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    parser = argparse.ArgumentParser(prog="sandbox-ai thing", formatter_class=helpstyle.CommandsFormatter,
                                     description="sandbox-ai thing: what a thing\n   does, in one line (100%% of %(prog)s).",
                                     epilog="  sandbox-ai thing add NAME     laid out\n  by hand")
    parser.add_argument("--json", action="store_true", help="as JSON")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("add", help="add a thing").add_argument("name")
    commands.add_parser("delete", aliases=["remove"], help="forget a thing")
    commands.add_parser("plain", description="A subcommand without help. Its description's first paragraph.\n\nMore.")
    commands.add_parser("internal", help=argparse.SUPPRESS)
    text = formatted(parser)
    assert text.startswith("Usage:  sandbox-ai thing [OPTIONS] [COMMAND]\n\nWhat a thing does, in one line (100% of sandbox-ai thing).\n\n")
    assert "  add             Add a thing\n" in text and "  delete, remove  Forget a thing\n" in text
    assert "  plain           A subcommand without help. Its description's first paragraph.\n" in text
    assert "internal" not in text and "More." not in text
    assert "  --json          As JSON\n" in text
    assert "  sandbox-ai thing add NAME     laid out\n  by hand\n\nRun 'sandbox-ai thing COMMAND --help'" in text
    assert parser.format_usage() == "usage: sandbox-ai thing [-h] [--json] {add,delete,remove,plain,internal} ...\n"
    # the subparsers' prog is argparse's own; their help keeps argparse's layout until style() gives them this one
    assert commands.choices["add"].prog == "sandbox-ai thing add"
    assert commands.choices["add"].format_help().startswith("usage: sandbox-ai thing add [-h] name\n")
    helpstyle.style(parser)
    add = commands.choices["add"]
    assert add.format_help() == ("Usage:  sandbox-ai thing add NAME\n\nAdd a thing.\n\nOptions:\n"
                                 "  -h, --help  Show this help message and exit\n\n"
                                 "Run 'sandbox-ai thing --help' for the other thing commands.\n")
    assert add.format_usage() == "usage: sandbox-ai thing add [-h] name\n"  # (an error's: argparse's own)
    assert commands.choices["plain"].description.startswith("A subcommand without help.")  # its own stays


def test_required_subcommands_and_arguments_beside_them(monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    parser = argparse.ArgumentParser(prog="sandbox-ai other", formatter_class=helpstyle.CommandsFormatter,
                                     description="sandbox-ai other: at 100% (a % of its own)")
    parser.add_argument("run", nargs="?", help="the run")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("go", help="go")
    text = formatted(parser)
    assert text.startswith("Usage:  sandbox-ai other [RUN] COMMAND\n\nAt 100% (a % of its own)\n\nCommands:\n  go")
    assert "\nArguments:\n  RUN" in text and "\nOptions:\n  -h, --help" in text


def test_a_version_option_prints_its_text_as_it_is(capsys):
    parser = argparse.ArgumentParser(prog="sandbox-ai thing", formatter_class=helpstyle.CommandsFormatter)
    parser.add_argument("-V", "--version", action="version", version="sandbox-ai 1.2.3")
    parser.add_subparsers(dest="command").add_parser("go", help="go")
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])
    assert capsys.readouterr().out == "sandbox-ai 1.2.3\n"


# --- the help of every command (helpstyle.Formatter on every parser) ---------------------------------------------

def load_app_help_options():
    """The desktop app's help_options (projects/sandbox-ai-app/sandbox_ai_app/backend.py): how it reads the options a
    host's sandbox-ai takes from their --help - taken from its source, so this test reads them as the app does."""
    source = (ROOT / "projects/sandbox-ai-app/sandbox_ai_app/backend.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "help_options")
    scope = {"re": re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "backend.py", "exec"), scope)  # noqa: S102
    return scope["help_options"]


APP_HELP_OPTIONS = []


def app_help_options(text):
    if not APP_HELP_OPTIONS:
        APP_HELP_OPTIONS.append(load_app_help_options())
    return APP_HELP_OPTIONS[0](text)


def every_parser():
    """(path, parser) of every command and subcommand of sandbox-ai, laid out by helpstyle (as main.py does), and
    the agents' launcher for claude."""
    found = []

    def walk(parser, path):
        found.append((path, parser))
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                seen = set()
                for name, sub in action.choices.items():
                    if id(sub) not in seen:
                        seen.add(id(sub))
                        walk(sub, path + [name])
    for command in sorted(set(listed()) - {"build", "update", "version", "help"}):
        parser = completion.parser_of(command)
        if command not in main.PYTHON_COMMANDS or main.PYTHON_COMMANDS[command][0] == "agent_sandbox.py":
            continue
        walk(helpstyle.style(parser), [command])
    launcher = load("agent_sandbox")
    found.append((["claude"], launcher.build_parser("claude")))
    return found


@pytest.fixture(scope="module")
def pages():
    saved = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "200"  # a wide terminal: the pages still keep to 100 columns
    try:
        return [(path, parser, parser.format_help()) for path, parser in every_parser()]
    finally:
        if saved is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = saved


def test_every_help_page_is_docker_like_within_100_columns(pages):
    assert len(pages) > 150
    for path, parser, text in pages:
        where = "sandbox-ai " + " ".join(path)
        assert text.startswith("Usage:  "), where
        assert "positional arguments" not in text and "optional arguments" not in text and "\noptions:" not in text, where
        assert "\033" not in text, where  # no colour, in a pipe or not
        assert "\nOptions:\n" in text or "\nLogs and checks:\n" in text, where
        last = text.rstrip().splitlines()[-1]
        assert last.startswith("Run 'sandbox-ai "), (where, last)
        for line in text.splitlines():
            assert len(line) <= 100, (where, line)
        description = text.split("\n\n")[1]
        assert description[:1].isupper() and not description.startswith("sandbox-ai"), (where, description[:80])


def test_every_option_is_on_a_line_of_its_own_with_its_value_once(pages):
    for path, parser, text in pages:
        where = "sandbox-ai " + " ".join(path)
        lines = text.splitlines()
        found = app_help_options(text)
        for action in parser._actions:
            if not action.option_strings or action.help == argparse.SUPPRESS:
                continue
            names = ", ".join(action.option_strings)
            assert any(line.startswith("  " + names) for line in lines), (where, names)
            assert {o for o in action.option_strings if o.startswith("--")} <= found, (where, names)
            if action.choices and action.metavar is None:  # the app reads a choice's values: --ask {on,off,allow}
                long = max(action.option_strings, key=len)
                assert {f"{long}={value}" for value in action.choices} <= found, (where, long)
        for line in lines:  # "-b {podman,bwrap}, --backend {podman,bwrap}" no more
            head = line.strip().split("  ")[0]
            assert not re.match(r"-\w+ \S+, --", head), (where, line)


def test_the_agent_help_is_grouped_by_topic_with_examples_last(pages):
    text = next(text for path, _, text in pages if path == ["claude"])
    assert text.startswith("Usage:  sandbox-ai claude [OPTIONS] [-- AGENT ARGS]\n\nStart Claude Code in a sandbox: ")
    launcher = load("agent_sandbox")
    headings = [line[:-1] for line in text.splitlines() if re.fullmatch(r"[A-Z][^:]*(\([^)]*\))?:", line)]
    topics = [title for title, _ in launcher.HELP_GROUPS]
    assert headings[:len(topics)] == topics, headings
    assert launcher.OTHER_FEATURES in headings and headings[-1] == "Examples (more in QUICKSTART.md)"
    own = text.split("\nAgent:\n")[1].split("\n\n")[0]
    others = text.split(f"\n{launcher.OTHER_FEATURES}:\n")[1].split("\n\n")[0]
    assert "--hermes-enable" in others and "--hermes-enable" not in own  # (claude has none of its own)
    hermes = launcher.build_parser("hermes").format_help()
    assert "--hermes-enable" in hermes.split("\nAgent:\n")[1].split("\n\n")[0]
    assert "  sandbox-ai claude -n " in text and "sandbox-ai AGENT -n" not in text
    assert text.rstrip().endswith("Run 'sandbox-ai --help' for all commands.")
    # an error keeps argparse's usage line, naming the agent
    assert launcher.build_parser("claude").format_usage() == "usage: sandbox-ai claude [OPTIONS] [-- AGENT ARGS]\n"
    assert set(launcher.AGENT_TITLES) == set(completion.AGENTS)
    assert {f"Start {title} in a sandbox" for title in launcher.AGENT_TITLES.values()} == \
        {text for name, text in SECTIONS["Agents"]}


def test_every_example_is_a_command_line_its_parser_takes(pages):
    import shlex
    by_prog = {parser.prog: parser for path, parser, _ in pages if path != ["claude"]}
    assert helpstyle.EXAMPLES
    for prog, examples in helpstyle.EXAMPLES.items():
        assert prog in by_prog, prog
        for line, note in examples:
            words = shlex.split(line)
            assert " ".join(words[:len(prog.split())]) == prog, line
            assert note and len(note) < 60, note
            try:
                by_prog[prog].parse_args(words[len(prog.split()):])
            except SystemExit as exc:
                raise AssertionError(f"{line}: not taken ({exc})") from None
        text = next(text for path, parser, text in pages if parser.prog == prog)
        assert "\nExamples:\n" + "".join(f"  {line}" for line, _ in examples[:1]) in text, prog


def test_option_texts_capitalized_defaults_named(monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    parser = argparse.ArgumentParser(prog="sandbox-ai thing", formatter_class=helpstyle.Formatter)
    parser.add_argument("--wait", type=float, default=30, metavar="SECONDS", help="how long to wait")
    parser.add_argument("--said", default="x", help="its default is said (default: x)")
    parser.add_argument("--host", help="pypi.org, files.pythonhosted.org (TCP/443)")
    parser.add_argument("--format", choices=["md", "html"], help="md, html")
    parser.add_argument("--backend", default="podman", help="podman (default): a pod")
    text = parser.format_help()
    assert re.search(r"\n  --wait SECONDS +How long to wait \(default: 30\)\n", text)
    assert "Its default is said (default: x)\n" in text and "(default: x) (default" not in text
    assert "pypi.org, files.pythonhosted.org" in text and "Pypi.org" not in text
    assert re.search(r"\n  --format \{md,html\} +md, html\n", text) and "Podman (default)" not in text
    assert text.rstrip().endswith("Run 'sandbox-ai --help' for all commands.")


def test_notes_prose_filled_tables_and_lists_kept_within_the_width():
    text = ("RUN is the name of a sandbox; it may be left out\nwhen only one runs.\n\n"
            "  sandbox-ai net list                running sandboxes\n"
            "  sandbox-ai net cut [RUN] MODE [--only] | --all-runs MODE\n"
            "                                     the kill switch: MODE remote, local, api or all; off lifts it, and "
            "a long text goes on and on to the end of the line and further\n"
            "  sandbox-ai llm add NAME --kind KIND --url URL [--api-key-secret SECRET] [--header-secret HEADER=SECRET]\n"
            "                     [--ca FILE] [--vpn PROFILE]\n\n"
            "Hardware:\n  1. A service on the host, reached\n     over the network.\n  2. A device.")
    out = helpstyle.paragraphs(text, 80)
    lines = out.splitlines()
    assert lines[0] == "RUN is the name of a sandbox; it may be left out when only one runs."
    assert all(len(line) <= 80 for line in lines), out
    assert "  sandbox-ai net list                running sandboxes" in lines
    cut = lines.index("  sandbox-ai net cut [RUN] MODE [--only] | --all-runs MODE")
    assert lines[cut + 1].startswith(" " * 37 + "the kill switch: MODE remote, local,")
    assert lines[cut + 2].startswith(" " * 37) and lines[cut + 2][37] != " "  # its text filled under its column
    assert "  sandbox-ai llm add NAME --kind KIND --url URL [--api-key-secret SECRET]" in lines
    assert "                     [--header-secret HEADER=SECRET] [--ca FILE] [--vpn PROFILE]" in lines
    assert "  1. A service on the host, reached over the network." in lines and "  2. A device." in lines
    narrow = helpstyle.paragraphs(text, 60).splitlines()  # too narrow for the text beside its head: below it
    assert narrow[narrow.index("  sandbox-ai net list") + 1] == "          running sandboxes"
