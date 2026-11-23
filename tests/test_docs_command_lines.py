"""What the user reads is what sandbox-ai does: the command lines and tables of the docs and of the app's Help,
checked against the program itself (docs round 2, 2026-11-22).

Found then, each now a test below:
- README_DE's `llm set` block had a doubled backslash (`\\\\` at a line's end): copied, the line breaks there;
- ten Markdown table rows (README, README_DE, RELEASING, four reviews) had an unescaped `|` inside a code span
  (`-n|--limit`, `curl … | bash`, `search|download`): GitHub ends the cell there and the row falls apart;
- README_DE lacked a whole table row (`approvals relay`) and half of "A board or session as one file" (the
  `board export --images` and `board show --check` lines, the list of what an import refuses);
- the command table started `sandbox-ai claude|codex|opencode|hermes|qwen|dsh|goose` (five agents missing) and
  `sandbox-ai login claude|codex|opencode|hermes|qwen` (pi missing).

The command lines are parsed by each command's own parser (tests/test_skill_sync.parse_error: the parser the
script builds, nothing after its parse runs); a synopsis is read as one example of it: `[...]` optional parts taken,
`a|b` its first alternative, a typed placeholder (`--limit N`) given a value of its type.
"""
import argparse
from pathlib import Path
import re
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_skill_sync as sync  # noqa: E402

ROOT = sync.ROOT
completion, s = sync.completion, sync.s
session_login = sync.load("session_login_for_docs_lines", "sandbox/session_login.py")
plugins = sync.load("plugins_for_docs_lines", "sandbox/plugins.py")
login = sync.load("login_for_docs_lines", "sandbox/login.py")

# The docs whose code blocks hold sandbox-ai command lines a user copies (the release note of this line too).
DOCS = ("README.md", "README_DE.md", "QUICKSTART.md", "QUICKSTART_DE.md", "INTEGRATIONS.md", "INTEGRATIONS_DE.md",
        "PROXMOX_LXC.md", "PROXMOX_LXC_DE.md", "SANDBOX.md", "docs/HELPFUL_FIXES.md", "docs/TOOLCHAINS_DESIGN.md",
        "docs/CAIRN_REVIEW.md", "docs/LIVE_CHECK.md",
        # the release note of this line: the newest (renamed with every bump, so found, never named)
        max((p.relative_to(ROOT).as_posix() for p in (ROOT / "docs/releases").glob("v*.md")),
            key=lambda name: tuple(int(part) for part in re.findall(r"\d+", name))))
FENCE = re.compile(r"^\s*```[^\n]*\n(.*?)^\s*```", re.S | re.M)
OPTION = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*[a-z0-9]")
# Taken by main.py before any command (and after it): never a command's own option.
GLOBAL_OPTIONS = {"--no-color", "--no-colour", "--color", "-nc"}
CHECKPOINT_COMMANDS = ("create", "list", "show", "diff", "restore", "delete")


def first_alternative(words):
    """A synopsis as one command line: each `[...]` group's content (its first alternative when it has ` | `),
    `a|b|c` as `a`, `X...` as `X`."""
    out, depth, skipping = [], 0, []
    for word in words:
        opened = len(word) - len(word.lstrip("["))
        word = re.sub(r"(\.\.\.|…)$", "", word.lstrip("["))
        closed = len(word) - len(word.rstrip("]"))
        word = re.sub(r"(\.\.\.|…)$", "", word.rstrip("]"))
        depth += opened
        if word == "|":
            skipping.append(depth)  # the other alternatives of this group
        elif not skipping and word:
            alternative = "|" in word and "://" not in word
            out.append(word.split("|")[0] if alternative else word)
            if alternative and depth and not closed:
                skipping.append(depth)  # [RUN|--session NAME]: the rest of the group is the other alternative's
        depth -= closed
        while skipping and depth < skipping[-1]:
            skipping.pop()
    return out


def line_argv(line):
    """The words after `sandbox-ai` of LINE (one line of a code block), or None when it is no sandbox-ai command
    line: `$ ` and environment assignments before it left out, a comment and a description column (two spaces and
    words, as a --help's examples) cut, a pipe or `&&` after it cut."""
    line = line.strip()
    line = line[2:] if line.startswith("$ ") else line
    line = re.sub(r"^(?:[A-Z_]+=\S+ )+", "", line)
    match = re.match(r"^(?:\./)?sandbox-ai ([a-z].*)$", line)
    if not match:
        return None
    command = re.split(r"\s{2,}(?=[^\s\[-])", re.sub(r"\s+#.*$", "", match[1]))[0]
    words = shlex.split(command)
    for stop in ("&&", ";", "||", ">", "2>&1", ">/dev/null", "2>/dev/null"):
        if stop in words:
            words = words[:words.index(stop)]
    if "|" in words and "[" not in command:
        words = words[:words.index("|")]
    return [word for word in first_alternative(words) if word not in GLOBAL_OPTIONS]


def block_lines(name):
    """[(line, argv)] of every sandbox-ai command line in NAME's fenced code blocks (continued lines joined)."""
    found = []
    for block in FENCE.findall((ROOT / name).read_text()):
        for line in re.sub(r"\\\n\s*", " ", block).splitlines():
            argv = line_argv(line)
            if argv:
                found.append((line.strip(), argv))
    return found


def parse_error(argv, tries=8):
    """None when sandbox-ai takes ARGV (the words after `sandbox-ai`), else what its parser said. A placeholder in
    capitals the parser refuses for its type or choices is given a value it takes, and parsed again."""
    error = parse_once(argv)
    typed = re.search(r"argument \S+: invalid (?:int|float) value: '([^']*)'", error or "")
    choice = re.search(r"argument \S+: invalid choice: '([^']*)' \(choose from '?([^',)]*)", error or "")
    if tries and (typed or choice):
        bad, sample = (typed[1], "1") if typed else (choice[1], choice[2])
        if bad.upper() == bad and bad in argv:
            at = argv.index(bad)
            return parse_error([*argv[:at], sample, *argv[at + 1:]], tries - 1)
    return error


def parse_once(argv):
    if any(word in ("-h", "--help") for word in argv):  # its help: the words before it must be a command
        argv = [word for word in argv if word not in ("-h", "--help")]
        if argv[:1] in (["build"], ["update"]):
            return None
    if argv[:1] == ["version"]:
        return None if set(argv[1:]) <= {"--verbose", "--json", "-v"} else f"sandbox-ai version takes {argv[1:]}?"
    # The words a command's own main() hands on before its parser (login.py, plugins.py, checkpoints.py).
    if argv[:2] == ["login", "give"]:
        return sync.parse_only(lambda: session_login.give_parser().parse_args(argv[2:]))
    if argv[:2] == ["plugins", "marketplaces"]:
        return sync.parse_only(lambda: plugins.marketplaces_parser().parse_args(argv[2:]))
    if argv[:1] == ["plugins"] and argv[2:3] == ["trust-hooks"]:
        return sync.parse_only(lambda: plugins.trust_hooks(argv[3:], agent=argv[1]))
    if argv[:1] == ["checkpoint"] and argv[1:2] and not argv[1].startswith("-") and \
            argv[1] not in CHECKPOINT_COMMANDS:
        argv = ["checkpoint", "create", *argv[1:]]  # sandbox-ai checkpoint RUN
    return sync.parse_error(argv)


@pytest.mark.parametrize("name", DOCS)
def test_every_command_line_of_a_code_block_runs_as_shown(name):
    """Each `sandbox-ai ...` line of the doc's code blocks is one the program takes, as shown: no option renamed or
    gone, no value it refuses, no subcommand that is not there."""
    lines = block_lines(name)
    assert lines or name.startswith(("docs/", "SANDBOX", "PROXMOX", "INTEGRATIONS")), "no command lines found"
    failed = [(line, error) for line, argv in lines for error in [parse_error(argv)] if error]
    assert not failed, "\n".join(f"{name}: {line}\n    -> {error.splitlines()[-1]}" for line, error in failed)


def test_the_check_of_a_code_block_catches_what_it_should():
    assert parse_error(line_argv("sandbox-ai approvals blocked [RUN] [--limit N] [--json]")) is None
    assert parse_error(line_argv("sandbox-ai approvals blocked [RUN] [--limits N]")) is not None
    assert parse_error(line_argv("sandbox-ai llm params [NAME] [--kind KIND] [--json]")) is None  # a choice
    assert parse_error(line_argv("sandbox-ai llm share NAME [--key-to HOST,... | --no-key]")) is None
    assert parse_error(line_argv("sandbox-ai login give web --host box --dry-run  # shown")) is None
    assert parse_error(line_argv("sandbox-ai plugins codex trust-hooks superpowers@sandbox-ai")) is None
    assert parse_error(line_argv("sandbox-ai checkpoint web --label 'before'")) is None
    assert parse_error(line_argv("sandbox-ai net allow web -A pypi.org      pypi.org for web")) is None
    assert parse_error(line_argv("sandbox-ai net list --no-color")) is None
    assert parse_error(line_argv("sandbox-ai claude --no-such-option")) is not None
    assert line_argv("sandbox-ai llm set NAME \\\\") == ["llm", "set", "NAME", "\\"]  # the doubled backslash
    assert parse_error(["llm", "set", "NAME", "\\"]) is not None


# --- the app's Help ------------------------------------------------------------------------------------------------

def help_sections():
    sys.path.insert(0, str(ROOT / "projects" / "sandbox-ai-app"))
    sys.dont_write_bytecode = True
    try:
        from sandbox_ai_app import help_content  # noqa: PLC0415 - no Qt in it (export_web_help.py loads it too)
    finally:
        sys.path.remove(str(ROOT / "projects" / "sandbox-ai-app"))
    return help_content.SECTIONS


def test_every_command_line_of_the_app_s_help_runs_as_shown():
    """The command lines each Help section names (its "Command line" box, also the web page's help) parse; each
    option it names (also a bare `--memory`) is one of sandbox-ai's."""
    known = sync.every_option() | GLOBAL_OPTIONS
    failed, checked = [], 0
    for section in help_sections():
        for line in section.cli:
            for option in OPTION.findall(line):
                if option not in known and option not in sync.FOREIGN_OPTIONS:
                    failed.append(f"{section.id}: {line}: no option {option}")
            argv = line_argv(re.split(r" && ", line)[0])
            if argv:
                checked += 1
                error = parse_error(argv)
                if error:
                    failed.append(f"{section.id}: {line}\n    -> {error.splitlines()[-1]}")
    assert checked > 300 and not failed, "\n".join(failed)


# --- options named in the text -------------------------------------------------------------------------------------

_options = {}


def options_of(command):
    """Every option the parser of COMMAND (and of each subcommand below it) takes."""
    if command not in _options:
        found = set(GLOBAL_OPTIONS)

        def walk(parser):
            for action in parser._actions:
                found.update(action.option_strings)
                if isinstance(action, argparse._SubParsersAction):
                    for sub in action.choices.values():
                        walk(sub)
        if command in completion.AGENTS or command == "resume":  # resume hands start options on
            walk(s.build_parser())
        if command in ("build", "update"):
            found.update(sync.every_option())  # (main.py's own: only their --help says them)
        elif command not in completion.AGENTS:
            walk(sync.parser(command))
        if command == "login":
            walk(session_login.give_parser())
        if command == "plugins":
            walk(plugins.marketplaces_parser())
        _options[command] = found
    return _options[command]


def inline_commands(name):
    """[(command, the span)] of NAME's inline code spans (outside the code blocks) that name `sandbox-ai COMMAND`
    and an option after it (`\\|` of a table as `|`)."""
    text = " ".join(FENCE.sub("", (ROOT / name).read_text()).split())
    found = []
    for span in re.findall(r"`([^`]+)`", text):
        span = span.replace("\\|", "|")
        for match in re.finditer(r"sandbox-ai ([a-z][a-z0-9-]*)(?![,)\w-])", span):
            rest = re.split(r"sandbox-ai |\s--(?:\s|$)", span[match.end():])[0]
            if match[1] in completion.commands() or match[1] in ("build", "update"):
                found.append((match[1], span, set(OPTION.findall(rest))))
    return found


@pytest.mark.parametrize("name", DOCS)
def test_every_option_named_after_a_command_is_that_command_s(name):
    """`sandbox-ai CMD ... --option` in the text: CMD (one of its subcommands) takes --option."""
    failed = [f"{span}: sandbox-ai {command} has no {sorted(named - options_of(command))}"
              for command, span, named in inline_commands(name) if named - options_of(command)]
    assert not failed, "\n".join(failed)


def test_the_option_check_catches_a_wrong_one():
    assert options_of("net") >= {"--allow-host"} and "--allow-hosts" not in options_of("net")
    assert {"--restart", "--from-session"} <= options_of("login")  # login give's own
    assert "--agent" in options_of("plugins")  # plugins marketplaces' own


# --- the agents' names ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ("README.md", "README_DE.md", "QUICKSTART.md", "QUICKSTART_DE.md"))
def test_a_command_of_every_agent_names_every_agent(name):
    """`sandbox-ai claude|codex|...` (a start of any agent) lists each agent of the launcher, `sandbox-ai login
    a|b|...` each agent `sandbox-ai login` takes."""
    text = (ROOT / name).read_text().replace("\\|", "|")
    agents = "(?:%s)(?![\\w-])" % "|".join(sorted(completion.AGENTS, key=len, reverse=True))
    starts = re.findall(r"sandbox-ai (%s(?:\|%s)+)" % (agents, agents), text)
    logins = re.findall(r"sandbox-ai login (%s(?:\|%s)+)" % (agents, agents), text)
    assert (starts and logins) or name.startswith("QUICKSTART")
    for listed in starts:
        assert set(listed.split("|")) == set(completion.AGENTS), listed
    for listed in logins:
        assert set(listed.split("|")) == set(login.AGENTS), listed


# --- Markdown tables -----------------------------------------------------------------------------------------------

def markdown_files():
    return sorted([*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md"), *(ROOT / "docs" / "releases").glob("*.md"),
                   *(ROOT / "projects" / "sandbox-ai-app").glob("*.md")])


def broken_rows(text):
    """[(line number, cells, the header's cells, the row)] of the table rows of TEXT whose number of cells is not
    their header's: an unescaped `|` - in a code span too - ends a cell on GitHub."""
    found, header, fenced = [], None, False
    for number, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            fenced, header = not fenced, None
            continue
        if fenced or not line.startswith("|"):
            header = None
            continue
        cells = len(re.findall(r"(?<!\\)\|", line))
        if header is None:
            header = cells
        elif cells != header:
            found.append((number, cells, header, line))
    return found


@pytest.mark.parametrize("path", markdown_files(), ids=lambda path: str(path.relative_to(ROOT)))
def test_every_table_row_has_its_header_s_cells(path):
    rows = broken_rows(path.read_text())
    assert not rows, "\n".join(f"{path.relative_to(ROOT)}:{number}: {cells - 1} cells, the header {header - 1} (a `|` not "
                               f"written `\\|`?): {line[:120]}" for number, cells, header, line in rows)


def test_the_table_check_finds_a_pipe_in_a_code_span():
    assert broken_rows("| a | b |\n|---|---|\n| `x|y` | z |\n")[0][0] == 3
    assert not broken_rows("| a | b |\n|---|---|\n| `x\\|y` | z |\n")


# --- README and README_DE say the same -----------------------------------------------------------------------------

def sections(name):
    """[(heading, text)] of NAME: its headings outside code blocks, the text up to the next one."""
    found, heading, lines, fenced = [], "", [], False
    for line in (ROOT / name).read_text().splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and re.match(r"#{1,6} ", line):
            found.append((heading, "\n".join(lines)))
            heading, lines = line, []
        else:
            lines.append(line)
    return [*found, (heading, "\n".join(lines))]


def subcommands(command):
    """The names of COMMAND's subcommands (those of its parser's first level)."""
    found = sync.parser(command) if command in completion.commands() and command not in completion.AGENTS else None
    return {name for action in (found._actions if found else ()) if isinstance(action, argparse._SubParsersAction)
            for name in action.choices}


def block_commands(block):
    """The sandbox-ai command lines of a code block, each as its command words and options (what is translated -
    a comment, a placeholder, an example's name - left out)."""
    found = []
    for line in re.sub(r"\\\n\s*", " ", block).splitlines():
        match = re.match(r"\s*(?:\$ )?(?:\./)?sandbox-ai ([a-z][^#]*)", line)
        if match:
            words = match[1].split()
            command = words[:1] + [word for word in words[1:2] if word in subcommands(words[0])]
            found.append((tuple(command), tuple(sorted(set(OPTION.findall(match[1]))))))
    return found


def test_the_two_readmes_have_the_same_sections():
    english, german = sections("README.md"), sections("README_DE.md")
    assert [heading.split()[:1] for heading, _ in english] == [heading.split()[:1] for heading, _ in german]


@pytest.mark.parametrize("index", range(len(sections("README.md"))))
def test_each_section_of_the_two_readmes_says_the_same(index):
    """Section by section: the same options, the same number of table rows and code blocks, and in each code
    block the same command lines (README_DE lacked a row and half a section, found this way)."""
    (heading, english), (_, german) = sections("README.md")[index], sections("README_DE.md")[index]
    assert set(OPTION.findall(english)) == set(OPTION.findall(german)), \
        (heading, sorted(set(OPTION.findall(english)) ^ set(OPTION.findall(german))))
    rows = [len(re.findall(r"^\|.*\|\s*$", text, re.M)) for text in (english, german)]
    assert rows[0] == rows[1], (heading, "table rows", rows)
    blocks = [FENCE.findall(text) for text in (english, german)]
    assert len(blocks[0]) == len(blocks[1]), (heading, "code blocks")
    for english_block, german_block in zip(*blocks):
        assert block_commands(english_block) == block_commands(german_block), (heading, english_block)


# --- links within a page -------------------------------------------------------------------------------------------

def anchors(text):
    """The ids GitHub gives TEXT's headings (lower case, punctuation but - and _ dropped, spaces as -, a repeated
    one numbered) and its own `<a id="...">`."""
    found, seen = set(re.findall(r'<a id="([^"]+)"', text)), {}
    for heading in re.findall(r"^#{1,6} (.*)$", FENCE.sub("", text), re.M):
        heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.replace("`", ""))
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        seen[slug] = seen.get(slug, -1) + 1
        found.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
    return found


@pytest.mark.parametrize("name", DOCS)
def test_every_link_within_the_page_reaches_a_heading(name):
    text = (ROOT / name).read_text()
    links = set(re.findall(r"\]\(#([^)\s]+)\)", FENCE.sub("", text)))
    assert not links - anchors(text), sorted(links - anchors(text))


def test_the_anchors_are_github_s():
    assert anchors("## Other hosts\n## Other hosts\n### `sandbox-ai net`: the firewall, live\n") == {
        "other-hosts", "other-hosts-1", "sandbox-ai-net-the-firewall-live"}
    assert "für-alle-hosts" in anchors("## Für alle Hosts\n")
