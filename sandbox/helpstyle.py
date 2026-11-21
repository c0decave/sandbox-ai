"""The layout of every --help of sandbox-ai, the way Docker lays out its own (`docker network --help`,
`docker run --help`):

    Usage:  sandbox-ai net allow [OPTIONS] [RUN]

    Open more for a running sandbox, at once.

    Arguments:
      RUN                        The run (sandbox-ai net list); the only one when one runs

    Options:
      -A, --allow-host HOST      Pin HOST in the sandbox's /etc/hosts and allow TCP/443 to it
      -t, --allow-tcp IP:PORT    Allow TCP to IP:PORT (repeatable)
      ...

    (the command's notes, its epilog: prose filled to the width, what is laid out by hand as written)

    Examples:
      sandbox-ai net allow web -A pypi.org      pypi.org for the start web
      ...

    Run 'sandbox-ai net --help' for the other net commands.

A command with subcommands lists them under "Commands:", one line each (its aliases beside it: "delete, remove"),
and ends with "Run '... COMMAND --help' for more information on a command." Each option is one line - its short
and long form and its value once ("-b, --backend {podman,bwrap}"), the help text beside it, wrapped under itself;
the options of an argument group under the group's title. A subcommand without a description of its own gets the
line its command lists it with. Colour only on a terminal, added as the page is printed (install_colour():
textout.help_page - the headings, the command, options and placeholders, the commands of a list, the examples'
command lines); the text itself is the same: in a pipe, with NO_COLOR or --no-color the page reads as it is.

Every parser of sandbox-ai gets it: main.py calls install() before it runs a command's script (style() then sets
this formatter on each parser as it parses, the parsers that chose one of their own are kept), the launcher passes
Formatter itself. An error message keeps argparse's usage line ("usage: sandbox-ai net [-h] {list,show,...}").

Standard library only; Python 3.9 or later.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import shutil
import sys
import textwrap

WIDTH = 100  # the widest a help page gets, on any terminal
PREFIX = re.compile(r"^sandbox-ai [a-z][\w-]*(?: / [a-z][\w-]*)?:\s+")  # "sandbox-ai net: ", "sandbox-ai devices / limits: "
HEADINGS = {"positional arguments": "Arguments", "optional arguments": "Options", "options": "Options"}
MARK = "sandbox_ai_style"  # a formatter class with this attribute is one of these (also from another copy of the module)

# Examples at the end of a command's help: (command line, what it does). tests/test_main_help.py parses each line with
# its command's parser.
EXAMPLES = {
    "sandbox-ai net allow": (
        ("sandbox-ai net allow web -A pypi.org", "pypi.org (HTTPS) for the running start web"),
        ("sandbox-ai net allow -y -g", "PyPI and GitHub for the only running sandbox"),
        ("sandbox-ai net allow web -t 192.168.1.20:11434", "one TCP port, e.g. a local LLM server"),
        ("sandbox-ai net allow web -A api.example.com -k", "and keep it in the project's .sandbox-ai.json")),
    "sandbox-ai net revoke": (
        ("sandbox-ai net revoke web -A pypi.org", "take pypi.org back from the start web"),),
    "sandbox-ai net cut": (
        ("sandbox-ai net cut web api", "the start web reaches only its model API"),
        ("sandbox-ai net cut --all-runs all", "every running sandbox: everything cut"),
        ("sandbox-ai net cut web off", "lift the cut")),
    "sandbox-ai net show": (
        ("sandbox-ai net show web", "what the start web was allowed during its run"),),
    "sandbox-ai net list": (
        ("sandbox-ai net list", "what each running sandbox may reach, in short"),
        ("sandbox-ai net list --wide", "with each run's id and workspace"),
        ("sandbox-ai net list --json", "for scripts")),
    "sandbox-ai list": (
        ("sandbox-ai list", "what runs and what is kept"),
        ("sandbox-ai list --no-trunc", "with nothing shortened"),
        ("sandbox-ai list -H web", "what happened to web"),
        ("sandbox-ai list --all-users", "as root: every user's starts on this host")),
    "sandbox-ai session list": (
        ("sandbox-ai session list", "every session, running ones first"),),
    "sandbox-ai llm add": (
        ("sandbox-ai llm add studio -k lmstudio -u http://10.0.0.5:1234", "LM Studio on your network"),
        ("sandbox-ai llm add local -k ollama -u http://127.0.0.1:11434", "Ollama on this host")),
    "sandbox-ai vpn add": (
        ("sandbox-ai vpn add home --wireguard ~/wg0.conf", "a WireGuard configuration of yours"),
        ("sandbox-ai vpn add mullvad-se --mullvad --location se", "a new Mullvad key, exits in Sweden"),
        ("sandbox-ai vpn add tor --tor", "every connection through Tor")),
    "sandbox-ai mcp start": (
        ("sandbox-ai mcp start github", "start the MCP server github in its sandbox"),
        ("sandbox-ai mcp start github --for web", "for the start web, attached to it")),
    "sandbox-ai doctor": (
        ("sandbox-ai doctor", "check this host"),
        ("sandbox-ai doctor --backend bwrap --plain", "only Bubblewrap, one line per check")),
    "sandbox-ai report": (
        ("sandbox-ai report web -o web.html", "one report of the start web as HTML"),
        ("sandbox-ai report --all --since 2h", "every run of the last two hours")),
    "sandbox-ai gc": (
        ("sandbox-ai gc --dry-run", "what would be removed"),
        ("sandbox-ai gc --older-than 30", "also kept starts unused for 30 days")),
    "sandbox-ai completion": (
        ("sandbox-ai completion install", "tab completion for your shells, in their rc files"),
        ("sandbox-ai completion bash", "the bash script, to source it yourself")),
    "sandbox-ai login": (
        ("sandbox-ai login claude", "log in to Claude inside a sandbox"),
        ("sandbox-ai login --status", "which login each agent's sandboxes get")),
    "sandbox-ai plugins": (
        ("sandbox-ai plugins claude", "install the plugins the kits declare for Claude"),
        ("sandbox-ai plugins --status", "declared and installed plugins")),
    "sandbox-ai stop": (
        ("sandbox-ai stop web", "end the start web; its HOME stays"),
        ("sandbox-ai stop --all", "every start of yours on this host (asked first)"),
        ("sandbox-ai stop alice:web", "as root: alice's start web, stopped as alice")),
    "sandbox-ai resume": (
        ("sandbox-ai resume web", "start the kept start web again"),
        ("sandbox-ai resume web -w ~/src/lib", "with one more workspace, kept for later")),
    "sandbox-ai attach": (
        ("sandbox-ai attach web", "this terminal into the start web (detach: Ctrl-b d)"),),
    "sandbox-ai approvals allow": (
        ("sandbox-ai approvals allow 1763280000-41-3", "allow what that question asks, once"),
        ("sandbox-ai approvals allow 1763280000-41-3 -s run", "and the same until the sandbox ends")),
}


def capitalized(text):
    return text[:1].upper() + text[1:] if text[:1].islower() else text


def sentence(text):
    """An option's TEXT with its first word capitalized when it is an ordinary word ("allow TCP" -> "Allow TCP"), not
    a name or value ("pypi.org, ...", "md, html or pdf", "podman (default): ...")."""
    return capitalized(text) if re.match(r"[a-z]+ (?![(])", text) else text


def described(text):
    """The line a command lists a subcommand with, as the subcommand's description: capitalized, a full stop."""
    text = capitalized(" ".join(text.split()))
    return text if text[-1:] in (".", "!", "?", ":") else text + "."


def columns():
    """The width of a help page: the terminal's (COLUMNS), at most WIDTH."""
    return min(shutil.get_terminal_size((WIDTH, 24)).columns, WIDTH)


def fill(text, width, indent=""):
    """TEXT as one paragraph filled to WIDTH, never broken at a hyphen (sandbox-ai, --allow-host)."""
    return textwrap.fill(" ".join(text.split()), width, initial_indent=indent, subsequent_indent=indent,
                         break_on_hyphens=False, break_long_words=False)


def by_hand(paragraph):
    """Whether PARAGRAPH is laid out by hand (indented lines, a list, a table): shown as written."""
    lines = paragraph.splitlines()
    return any(line[:1] in (" ", "\t", "-", "*", "•") or re.match(r"\d+\. ", line) for line in lines[1:]) or \
        any(line[:1] in (" ", "\t") for line in lines[:1])


GAP = re.compile(r"(\s*\S.*?\S)(\s{2,})(?=\S)")  # a hand-made table's first cell and the gap after it
ITEM = re.compile(r"\s*(?:\d+\.|[-*\u2022])\s+(?=\S)")  # an item of a list: "  1. ", "- "
GLUE = "\x00"  # a space inside [...] while a synopsis is wrapped: "[--add-os PKG]" stays on one line


def entries(lines):
    """The LINES of a paragraph laid out by hand as [{"indent", "head", "under", "column", "texts"}]:
    - a line with a gap of two spaces is a table row: HEAD up to its text COLUMN, its text after it; the lines after
      it indented to that column continue its text;
    - a line without a gap is a head of its own (a synopsis: "sandbox-ai llm add NAME --kind KIND ..."); the lines
      after it that go on with options ("[--ca FILE] ...", indented deeper) belong to it (UNDER: their indent), the
      deeper ones that are text are its text, below it;
    - lines at the left margin are prose: one entry, filled."""
    found = []
    for line in lines:
        line = line.rstrip()
        indent = len(line) - len(line.lstrip())
        rest = line[indent:]
        last = found[-1] if found else None
        if last is not None and line and not GAP.match(rest):
            if last["column"] is not None and indent == last["column"] and last["texts"]:
                last["texts"].append(rest)  # a row's text, continued
                continue
            if last["head"] and not last["texts"] and indent > last["indent"] and rest[:1] in "[(|-":
                last["head"] += " " + rest  # a synopsis, continued
                last["under"] = last["under"] or indent
                continue
            if last["head"] and last["column"] is None and indent > last["indent"]:
                last["column"] = indent  # its text, below it
                last["texts"].append(rest)
                continue
            if last["column"] is not None and indent == last["column"] and not last["texts"]:
                last["texts"].append(rest)
                continue
        if last is not None and indent == 0 and line and last["indent"] == 0 and not last["head"]:
            last["texts"].append(rest)  # prose, continued
            continue
        entry = {"indent": indent, "head": line, "under": None, "column": None, "texts": []}
        gap = GAP.match(line) if indent or line[:1] not in ("-", "*") else None
        item = ITEM.match(line)
        if item:  # "  1. A service ...", "- a point": the text under its first word
            entry.update(head=item.group(0).rstrip(), column=len(item.group(0)), texts=[line[len(item.group(0)):]])
        elif gap and indent < len(gap.group(1)):
            column = len(gap.group(1)) + len(gap.group(2))
            entry.update(head=line[:column].rstrip(), column=column, texts=[line[column:].strip()])
        elif indent == 0 and line[:1] not in ("-", "*", "\u2022") and not re.match(r"\d+\. ", line):
            entry.update(head="", texts=[rest])
        found.append(entry)
    return found


def synopsis(line, width, under=None):
    """A LINE without a text column ("  sandbox-ai images extend AGENT [--add-os PKG] ...") within WIDTH: wrapped
    between its words, never inside [...] or (...), the continuation UNDER that column (default: under the word
    after the command)."""
    if len(line) <= width:
        return [line]
    found = re.match(r"\s*sandbox-ai [\w-]+ (?:[a-z][\w-]* )?", line)  # (the command and its subcommand)
    indent = under or (len(found.group(0)) if found else len(line) - len(line.lstrip()) + 4)
    glued, depth = [], 0
    for char in line.strip():
        depth += 1 if char in "[(" else -1 if char in "])" else 0
        glued.append(GLUE if char == " " and depth > 0 else char)
    lead = " " * (len(line) - len(line.lstrip()))
    lines = textwrap.wrap("".join(glued), width, initial_indent=lead, subsequent_indent=" " * indent,
                          break_on_hyphens=False, break_long_words=False)
    return [piece.replace(GLUE, " ") for piece in lines]


def by_hand_lines(text, width):
    """A paragraph laid out by hand within WIDTH (entries()): each table row's text filled anew under its column (a
    head too wide for it: the text below), each synopsis wrapped (synopsis()), prose filled."""
    out = []
    for entry in entries(text.splitlines()):
        head, column, texts = entry["head"], entry["column"], " ".join(entry["texts"])
        if not head:
            out += textwrap.wrap(texts, width, break_on_hyphens=False, break_long_words=False) or [""]
            continue
        if column is None:
            out += synopsis(head, width, entry["under"])
            continue
        if column > width - 30:  # (a head too wide for a text beside it: the text below, less indented)
            column = entry["indent"] + 8
        lines = textwrap.wrap(texts, width - column, break_on_hyphens=False, break_long_words=False) or [""]
        if len(head) + 1 <= column and "\n" not in head:
            out.append(head.ljust(column) + lines[0])
        else:
            out += synopsis(head, width, entry["under"])
            out.append(" " * column + lines[0])
        out += [" " * column + line for line in lines[1:]]
    return out


def paragraphs(text, width, indent=""):
    """TEXT's paragraphs (blank-line separated): prose filled to WIDTH, the ones laid out by hand kept in their
    layout (by_hand_lines)."""
    parts = [part.strip("\n") for part in re.split(r"\n[ \t]*\n", text.strip("\n"))]
    return "\n\n".join("\n".join(indent + line for line in by_hand_lines(part, width - len(indent)))
                       if by_hand(part) else fill(part, width, indent) for part in parts if part.strip())


def command_entries(action):
    """[(name with its aliases, help)] of a subparsers ACTION, in the order they were added: those added without
    help= too (with the first sentence of their description), none hidden with help=argparse.SUPPRESS."""
    helps = {choice.dest: choice for choice in action._choices_actions}
    entries, seen = [], set()
    for name, parser in action.choices.items():
        if id(parser) in seen:
            continue
        seen.add(id(parser))
        choice = helps.get(name)
        if choice is not None and choice.help == argparse.SUPPRESS:
            continue
        aliases = [other for other, sub in action.choices.items() if sub is parser and other != name]
        text = choice.help if choice is not None else (parser.description or "").split("\n\n")[0]
        entries.append((", ".join([name, *aliases]), capitalized(" ".join((text or "").split()))))
    return entries


def parent_hint(prog):
    """The last line of a command's help without subcommands: where its siblings are."""
    words = prog.split()
    if not words or words[0] != "sandbox-ai":
        return None
    if len(words) <= 2:  # (a command, or the agents' launcher)
        return "Run 'sandbox-ai --help' for all commands."
    return f"Run '{' '.join(words[:-1])} --help' for the other {' '.join(words[1:-1])} commands."


class Formatter(argparse.RawDescriptionHelpFormatter):
    """The Docker-like help of a sandbox-ai command (see the module's text)."""

    sandbox_ai_style = True
    raw_help = False  # True: the options' help texts keep their line breaks (a parser that chose RawTextHelpFormatter)

    def __init__(self, prog, indent_increment=2, max_help_position=36, width=None):
        super().__init__(prog, indent_increment, max_help_position, width or columns() - 2)
        self._in_sections = False  # a text before the first section is the description, after it the epilog
        self._commands = None  # the subparsers action, once its section is added
        self._page = False  # formatting a whole help page (not an error's usage line)

    # --- the usage line -------------------------------------------------------------------------------------

    def add_usage(self, usage, actions, groups, prefix=None):
        if prefix is not None:  # (argparse's own use: the prog of the subcommands)
            return super().add_usage(usage, actions, groups, prefix)
        self._add_item(self._usage_line, [usage, list(actions), groups])

    def _usage_line(self, usage, actions, groups):
        if not self._page:  # only the usage: an error message's, argparse's full one
            return self._format_usage(usage, actions, groups, None)
        if usage is not None:
            return f"Usage:  {usage % dict(prog=self._prog)}\n\n"
        words = [self._prog]
        if any(a.option_strings and a.help != argparse.SUPPRESS and not a.required and
               not isinstance(a, (argparse._HelpAction, argparse._VersionAction)) for a in actions):
            words.append("[OPTIONS]")
        for group in groups:  # a choice that must be made: "(--wireguard FILE | --openvpn FILE | ...)"
            shown = [a for a in group._group_actions if a.help != argparse.SUPPRESS]
            if group.required and shown:
                words.append("(" + " | ".join(self._invocation_short(a) for a in shown) + ")")
        in_groups = {id(a) for group in groups for a in group._group_actions}
        words += [self._invocation_short(a) for a in actions
                  if a.option_strings and a.required and id(a) not in in_groups and a.help != argparse.SUPPRESS]
        for action in actions:
            if action.option_strings or action.help == argparse.SUPPRESS:
                continue
            if isinstance(action, argparse._SubParsersAction):
                words.append("COMMAND" if action.required else "[COMMAND]")
            else:
                words.append(self._positional_usage(action))
        lead = "Usage:  "
        lines, line = [], lead + words[0]
        for word in words[1:]:
            if len(line) + 1 + len(word) > self._width:
                lines.append(line)
                indent = len(lead) + len(words[0]) + 1
                line = " " * (indent if indent + len(word) <= self._width else len(lead) + 2) + word
            else:
                line += " " + word
        return "\n".join(lines + [line]) + "\n\n"

    def _invocation_short(self, action):
        """"--wireguard FILE": an option's longest name with its value, for the usage line."""
        name = max(action.option_strings, key=len)
        if action.nargs == 0:
            return name
        if action.choices is not None and action.metavar is None:  # (its choices are in its line below)
            return f"{name} {action.dest.upper()}"
        return f"{name} {self._format_args(action, self._get_default_metavar_for_optional(action))}"

    def _positional_usage(self, action):
        metavar = self._get_default_metavar_for_positional(action)
        if isinstance(action.metavar, tuple):
            return self._format_args(action, metavar)
        metavar = self._metavar_formatter(action, metavar)(1)[0]
        return {None: metavar, argparse.OPTIONAL: f"[{metavar}]", argparse.ZERO_OR_MORE: f"[{metavar}...]",
                argparse.ONE_OR_MORE: f"{metavar}...", argparse.REMAINDER: f"[{metavar}...]"}.get(
            action.nargs, self._format_args(action, metavar))

    def _get_default_metavar_for_positional(self, action):
        if not self._page or action.choices is not None:  # (an error's usage line as argparse writes it; choices)
            return super()._get_default_metavar_for_positional(action)
        return action.dest.upper()

    # --- the description and the notes ---------------------------------------------------------------------

    def add_text(self, text):
        if text is None or text is argparse.SUPPRESS:
            return
        if self._in_sections or not self._root_section.items:  # the notes; a text alone (--version's) as it is
            return super().add_text(text)
        self._add_item(self._format_description, [text])

    def _format_description(self, text):
        """The description: without its "sandbox-ai net: ", capitalized; its first paragraph filled to the width,
        the others as _fill_text lays them out."""
        if "%(prog)" in text:  # (as argparse does: a text may hold a % of its own)
            text = text % dict(prog=self._prog)
        width = max(self._width - self._current_indent, 11)
        first, _, rest = text.strip("\n").partition("\n\n")
        first = fill(capitalized(PREFIX.sub("", " ".join(first.split()))), width)
        return first + ("\n\n" + paragraphs(rest, width) if rest.strip() else "") + "\n\n"

    def _fill_text(self, text, width, indent):
        if not self._page:  # (--version's text, alone: as it is)
            return super()._fill_text(text, width, indent)
        return paragraphs(text, width, indent)

    # --- the sections ----------------------------------------------------------------------------------------

    def start_section(self, heading):
        self._in_sections = True
        heading = HEADINGS.get(heading, capitalized(heading or ""))
        super().start_section(heading)

    def add_arguments(self, actions):
        commands = [a for a in actions if isinstance(a, argparse._SubParsersAction)]
        if not commands or self._current_section.heading != "Arguments":
            return super().add_arguments(actions)
        self._current_section.heading = "Commands"
        for action in commands:
            self.add_argument(action)
        others = [a for a in actions if a not in commands]
        if others:  # (the caller ends this section)
            self.end_section()
            self.start_section("Arguments")
            super().add_arguments(others)

    def add_argument(self, action):
        if isinstance(action, argparse._SubParsersAction):
            self._commands = action
            for name, _ in command_entries(action):
                self._action_max_length = max(self._action_max_length, len(name) + self._current_indent)
            self._add_item(self._format_commands, [action])
            return
        if not action.option_strings and not action.help:
            return  # a positional without a text of its own: the usage line names it
        return super().add_argument(action)

    def _format_action_invocation(self, action):
        """"-w, --workspace DIR": each name of the option, its value once; a positional by its name."""
        if not action.option_strings:
            if isinstance(action.metavar, tuple):
                return super()._format_action_invocation(action)
            return self._metavar_formatter(action, self._get_default_metavar_for_positional(action))(1)[0]
        names = ", ".join(action.option_strings)
        if action.nargs == 0:
            return names
        return f"{names} {self._format_args(action, self._get_default_metavar_for_optional(action))}"

    def _format_commands(self, action):
        # the names' own column (long options, as images' --podman-url PODMAN_URL, put theirs further right)
        longest = max([len(name) for name, _ in command_entries(action)] or [0]) + self._current_indent
        help_position = min(longest + 2, self._max_help_position)
        help_width = max(self._width - help_position, 11)
        lines = []
        for name, text in command_entries(action):
            name = " " * self._current_indent + name
            wrapped = self._split_lines(text, help_width) if text else []
            if not wrapped:
                lines.append(name)
                continue
            if len(name) + 2 <= help_position:
                lines.append(name.ljust(help_position) + wrapped[0])
            else:
                lines += [name, " " * help_position + wrapped[0]]
            lines += [" " * help_position + line for line in wrapped[1:]]
        return "\n".join(lines) + "\n"

    def _expand_help(self, action):
        text = sentence(super()._expand_help(action))
        return text + self._default_note(action, text)

    @staticmethod
    def _default_note(action, text):
        """" (default: 30)" for an option whose plain default its text does not name."""
        default = action.default
        if (not action.option_strings or type(action).__name__ != "_StoreAction" or isinstance(default, bool)
                or not isinstance(default, (str, int, float)) or default in ("", argparse.SUPPRESS)
                or "default" in text.lower() or str(default).lower() in text.lower()):
            return ""
        return f" (default: {default})"

    def _split_lines(self, text, width):
        if self.raw_help:
            return text.splitlines()
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False) or [""]

    # --- the end: examples and the last line ---------------------------------------------------------------

    def format_help(self):
        self._page = len(self._root_section.items) > 1
        if self._page and self._current_section is self._root_section:
            examples = EXAMPLES.get(self._prog)
            if examples:
                self._add_item(self._format_examples, [examples])
            if self._commands is not None:
                hint = f"Run '{self._prog} COMMAND --help' for more information on a command."
            else:
                hint = parent_hint(self._prog)
            if hint:
                self._add_item(lambda: f"\n{hint}\n", [])
            self._commands = None
        return super().format_help()

    def _format_examples(self, examples):
        column = min(max(len(line) for line, _ in examples) + 4, self._width - 30)
        lines = ["Examples:"]
        for line, note in examples:
            text = "  " + line
            if note and len(text) + 2 <= column:
                lines.append(text.ljust(column) + note)
            else:
                lines += [text] + ([" " * column + note] if note else [])
        return "\n".join(lines) + "\n"


class RawHelpFormatter(Formatter):
    """For a parser that chose RawTextHelpFormatter: its options' texts keep their line breaks."""

    raw_help = True


CommandsFormatter = Formatter  # (the name the commands with subcommands give it)


def style(parser, description=None):
    """PARSER and the parsers below it laid out by Formatter: each that chose argparse's own layout (or none) gets
    it; a subcommand without a description gets DESCRIPTION, the line its command lists it with. Idempotent."""
    if description and not parser.description:
        parser.description = description
    chosen = parser.formatter_class
    if isinstance(chosen, type) and not getattr(chosen, MARK, False) and issubclass(chosen, argparse.HelpFormatter):
        parser.formatter_class = RawHelpFormatter if issubclass(chosen, argparse.RawTextHelpFormatter) else Formatter
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {choice.dest: choice.help for choice in action._choices_actions}
            for name, sub in action.choices.items():
                text = helps.get(name)
                style(sub, described(text) if text and text != argparse.SUPPRESS else None)
    return parser


def textout():
    """textout.py beside this file (the styles; colour only where it belongs): the one copy of this process."""
    loaded = sys.modules.get("sandbox_ai_textout")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_textout", Path(__file__).resolve().parent /
                                                      "textout.py")
        made = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(made)
        loaded = sys.modules.setdefault("sandbox_ai_textout", made)
    return loaded


def install_colour():
    """Every help page a parser prints (print_help, --help) coloured when it goes to a terminal (textout.help_page);
    format_help() itself, and a page printed anywhere else, stay without colour. Idempotent."""
    parser_class = argparse.ArgumentParser
    if getattr(parser_class.print_help, MARK, False):
        return

    def print_help(self, file=None):
        file = sys.stdout if file is None else file
        text = self.format_help()
        if text:
            self._print_message(textout().help_page(text, stream=file), file)
    print_help.sandbox_ai_style = True
    parser_class.print_help = print_help


def install():
    """Every argparse parser of this process laid out by Formatter (style()) from its first parse or help on, its
    help coloured on a terminal (install_colour)."""
    install_colour()
    parser_class = argparse.ArgumentParser
    if getattr(parser_class._get_formatter, MARK, False):
        return
    parse, formatter = parser_class.parse_known_args, parser_class._get_formatter

    def parse_known_args(self, *args, **kwargs):
        style(self)
        return parse(self, *args, **kwargs)

    def get_formatter(self, *args, **kwargs):
        style(self)
        return formatter(self, *args, **kwargs)
    parse_known_args.sandbox_ai_style = get_formatter.sandbox_ai_style = True
    parser_class.parse_known_args, parser_class._get_formatter = parse_known_args, get_formatter
