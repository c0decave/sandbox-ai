"""Every option of every command's --help is in README.md and README_DE.md, and every command `sandbox-ai --help`
lists is named there as `sandbox-ai COMMAND`. Found missing (2026-11-16): `net allow --public-only`, `bridge add
--limit KEY=VALUE`, `images search --limit N`, `approvals blocked --limit`, `mcp catalog/add --catalog`, `report
--format/--output`, and `sandbox-ai help`. The parsers are the commands' own (completion.parser_of: each script runs
up to its parse_args, nothing after), the agents' start options included; an option or subcommand hidden from --help
(argparse.SUPPRESS) is not asked for. A deliberate omission goes into ALLOWED with why."""
import argparse
import importlib.util
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_completion_docs", ROOT / "sandbox/completion.py")
completion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(completion)
READMES = ("README.md", "README_DE.md")
# (command path, option) left out of the READMEs on purpose - none so far. Each entry with why, e.g.
#   (("images", "prep"), "--json"): "an internal step of sandbox-ai build"
ALLOWED = {}


def visible_subparsers(parser):
    """{name: parser} of PARSER's subcommands that its --help shows (not those added with help=SUPPRESS)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            shown = {choice.dest for choice in action._choices_actions if choice.help != argparse.SUPPRESS}
            hidden = {choice.dest for choice in action._choices_actions if choice.help == argparse.SUPPRESS}
            return {name: sub for name, sub in action.choices.items()
                    if name in shown or (name not in hidden and not action._choices_actions)}
    return {}


def every_option():
    """{(command path, --option)} of every command sandbox-ai runs (main.PYTHON_COMMANDS), its subcommands below."""
    hidden = completion.module("main").HIDDEN
    found = set()

    def walk(parser, path):
        for action in parser._actions:
            if action.help == argparse.SUPPRESS:
                continue
            for option in action.option_strings:
                if option.startswith("--") and option != "--help":
                    found.add((path, option))
        for name, sub in visible_subparsers(parser).items():
            walk(sub, (*path, name))
    for command in sorted(completion.commands()):
        if command in hidden:
            continue
        parser = completion.parser_of(command)
        assert parser is not None, f"sandbox-ai {command}: its parser was not found ({completion.NOT_FOUND.get(command)})"
        walk(parser, (command,))
    return found


@pytest.fixture(scope="module")
def options():
    return every_option()


def test_the_walk_finds_the_options_it_should(options):
    """The check itself: the agents' start options, nested subcommands, and nothing hidden."""
    assert (("claude",), "--llm") in options and (("net", "allow"), "--public-only") in options
    assert (("bridge", "add"), "--limit") in options and (("images", "search"), "--limit") in options
    assert not any(path[:2] == ("images", "layer") for path, _ in options), "hidden: sandbox-ai build's own step"
    assert len(options) > 1000


@pytest.mark.parametrize("name", READMES)
def test_every_option_of_every_command_is_in_the_readme(name, options):
    text = (ROOT / name).read_text()
    missing = sorted(" ".join(path) + " " + option for path, option in options
                     if (path, option) not in ALLOWED and not re.search(re.escape(option) + r"(?![\w-])", text))
    assert not missing, f"{name} does not name these options (document them, or add them to ALLOWED with why): " \
                        + ", ".join(missing)


@pytest.mark.parametrize("name", READMES)
def test_every_command_of_the_help_is_in_the_readme(name):
    text = (ROOT / name).read_text()
    commands = [command.split(",")[0] for title, rows in completion.module("main").HELP
                if title != "Global Options" for command, _ in rows]
    assert "help" in commands and "pause" in commands
    missing = [c for c in commands if not re.search(r"sandbox-ai " + re.escape(c) + r"(?![\w-])", text)]
    assert not missing, f"{name} never shows these commands as `sandbox-ai COMMAND`: {missing}"


def test_an_allowed_omission_is_still_an_option_and_still_missing(options):
    """ALLOWED stays honest: an entry whose option is gone, or that a README now names, is removed."""
    for (path, option), why in ALLOWED.items():
        assert why and (path, option) in options, (path, option)
        assert any(not re.search(re.escape(option) + r"(?![\w-])", (ROOT / name).read_text()) for name in READMES)
