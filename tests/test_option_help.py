"""Every option of every command has a help text of its own (also the sub-commands' and --json)."""
import argparse
import importlib.util
from pathlib import Path
import sys
import unittest

SANDBOX = Path(__file__).resolve().parent.parent / "sandbox"
sys.path.insert(0, str(SANDBOX))
import completion  # noqa: E402


def walk(parser, path):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from walk(sub, path + [name])
        elif action.option_strings and action.help is None and not isinstance(action, argparse._HelpAction):
            yield " ".join(path), "/".join(action.option_strings)


class OptionHelp(unittest.TestCase):
    def test_every_option_has_help(self):
        missing = []
        for command in sorted(completion.commands()):
            parser = completion.parser_of(command)
            if parser is None:
                continue
            missing += [f"{where}: {option}" for where, option in walk(parser, ["sandbox-ai", command])]
        self.assertEqual(missing, [], "options without help text")

    def test_no_add_argument_without_help_in_the_sources(self):
        """The parsers the walk cannot reach (a command that builds its parser inside its main) too."""
        import ast
        missing = []
        for path in sorted(SANDBOX.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "add_argument"):
                    continue
                options = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                           and a.value.startswith("-")]
                keywords = {k.arg: k.value for k in node.keywords}
                action = keywords.get("action")
                own = isinstance(action, ast.Constant) and action.value == "version" or (
                    isinstance(action, ast.Name) and action.id == "LauncherVersion")  # (they say what they do)
                if options and "help" not in keywords and not own:
                    missing.append(f"{path.name}:{node.lineno} {'/'.join(options)}")
        self.assertEqual(missing, [], "add_argument of an option without help=")

    def test_the_walk_sees_the_commands(self):
        self.assertGreater(len(list(completion.commands())), 20)
        parser = completion.parser_of("llm")
        self.assertIsNotNone(parser)


if __name__ == "__main__":
    unittest.main()
