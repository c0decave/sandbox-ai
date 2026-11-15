"""What a failed CI step says where it can be read from everywhere: GitHub's annotations.

A job's log is fetched from a blob store that not every network reaches; its annotations come with the check run,
through the API. A step that failed printed one annotation before: the first 60 lines of pytest's FAILED/SKIPPED and
"E" lines, each cut at 300 characters - which named no test (-rs lists only the skipped ones) and ended in the middle
of the first assertion. With this module it prints, as workflow commands on standard output:

  - one error with the outcome: pytest's FAILED/ERROR/SKIPPED lines and its last line (or, where pytest wrote no
    summary - a crash, a timeout - the output's last lines);
  - each failed test's assertion (pytest's "E" lines) in full, in parts of at most LIMIT bytes, as errors; the same
    words once (a module's fixture that failed fails every test of the module with them);
  - the reports the tests left in $SANDBOX_AI_CI_REPORTS (report(name, text): what a probe saw - the rules loaded,
    the routes, a sandbox's whole output), as warnings and, when those are used up, as notices.

GitHub keeps at most PER_LEVEL annotations of each level per step: each failure and each report gets its first part
before any gets a second one, and a part's title says which of how many it is ("2/7": the rest was not shown).
Standard library only; runs on every Python the tests run on.

Usage: python tests/ci_annotations.py TITLE PYTEST_OUTPUT_FILE [REPORT_DIRECTORY]   (exit status 0)"""
import json
import os
from pathlib import Path
import re
import sys
import textwrap

LIMIT = 4000     # bytes of one annotation's message as it is printed (escaped): GitHub cuts longer ones
PER_LEVEL = 10   # annotations of one level GitHub keeps per step
REPORTS = "SANDBOX_AI_CI_REPORTS"
SECTION = re.compile(r"^={5,} (.+?) ={5,}$")
LAST = re.compile(r"^=+ (.*\b(?:passed|failed|errors?|skipped|deselected|no tests ran)\b.*?) =+$")
BLOCK = re.compile(r"^_{3,} (.+?) _{3,}$")
OUTCOME = re.compile(r"^(FAILED|ERROR|SKIPPED|XPASS) ")


def escape(text):
    """TEXT as the message of a workflow command (its line ends would end the command)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text):
    """TEXT as a workflow command's property (title=...): a colon or a comma would end it."""
    return escape(text).replace(":", "%3A").replace(",", "%2C")


def size(text):
    return len(escape(text).encode("utf-8", "replace"))


def cut(line, room):
    """(the longest beginning of LINE that takes at most ROOM escaped bytes, the rest)."""
    used = 0
    for index, character in enumerate(line):
        used += size(character)
        if used > room:
            return line[:index], line[index:]
    return line, ""


def parts(text, limit=LIMIT):
    """TEXT in parts of at most LIMIT bytes (escaped, as printed), cut at line ends; only a line longer than a part
    is cut inside (a report's JSON on one line of 20 KB), and fills what is left of the part before it."""
    found, lines, used = [], [], 0

    def close():
        nonlocal lines, used
        if lines:
            found.append("\n".join(lines))
        lines, used = [], 0

    for line in text.splitlines():
        while True:
            joint = size("\n") if lines else 0
            room, cost = limit - used - joint, size(line)
            if cost <= room:
                lines.append(line)
                used += joint + cost
                break
            if lines and (cost <= limit or room < limit // 8):
                close()  # it fits a part of its own, or what is left here is not worth a cut
                continue
            head, line = cut(line, room)
            if not head:  # (one character larger than a part: never with a limit of use)
                head, line = line[:1], line[1:]
            lines.append(head)
            close()
            if not line:
                break
    close()
    return found


def command(level, title, text):
    """One workflow command: an annotation of LEVEL ("error", "warning", "notice")."""
    return f"::{level} title={escape_property(title)}::{escape(text)}"


def outcome(output, lines=40, width=300):
    """pytest's own account of the run: its FAILED/ERROR/SKIPPED lines and its last line; without a summary (a
    crash, a step that was killed) the output's last lines."""
    found = [line[:width] for line in output.splitlines() if OUTCOME.match(line)]
    last = [LAST.match(line).group(1)[:width] for line in output.splitlines() if LAST.match(line)][-1:]
    if not found:
        return "\n".join(["no summary of pytest's; the output's last lines:"]
                         + [line[:width] for line in output.splitlines()[-15:]])
    more = [f"... and {len(found) - lines} more"] if len(found) > lines else []
    return "\n".join(found[:lines] + more + last)


def failures(output):
    """[(test, its assertion in full)] of pytest's FAILURES and ERRORS sections: each block's "E" lines without
    their prefix, or - a block without any - its last lines."""
    found, name, block, inside = [], None, [], False

    def close():
        if name is not None:
            said = [line[1:] for line in block if line == "E" or line.startswith("E ")]
            found.append((name, textwrap.dedent("\n".join(said or block[-30:])).strip("\n")))

    for line in output.splitlines():
        section = SECTION.match(line)
        if section:
            close()
            name, block, inside = None, [], section.group(1) in ("FAILURES", "ERRORS")
            continue
        if not inside:
            continue
        header = BLOCK.match(line)
        if header:
            close()
            name, block = header.group(1), []
        elif name is not None:
            block.append(line)
    close()
    return found


def merged(found):
    """FOUND ([(name, text)]) with the same words once: [(the first name and how many more, text)]."""
    names = {}
    for name, text in found:
        names.setdefault(text, []).append(name)
    return [(f"{who[0]} (and {len(who) - 1} more with the same words)" if len(who) > 1 else who[0], text)
            for text, who in names.items()]


def spread(entries, room, limit=LIMIT):
    """[(title, part)] of ENTRIES ([(name, text)]) for ROOM annotations: every entry's first part is taken before
    any entry's second one, and so on; they are listed entry by entry, each titled "name (part/parts)"."""
    divided = [(name, parts(text, limit)) for name, text in entries]
    chosen, depth = [], 0
    while len(chosen) < room and any(len(pieces) > depth for _, pieces in divided):
        for index, (name, pieces) in enumerate(divided):
            if len(pieces) > depth and len(chosen) < room:
                chosen.append((index, depth, f"{name} ({depth + 1}/{len(pieces)})", pieces[depth]))
        depth += 1
    return [(title, piece) for _, _, title, piece in sorted(chosen)]


def read_reports(directory):
    """[(name, text)] of the reports in DIRECTORY (report() wrote them), by name."""
    if not directory or not os.path.isdir(directory):
        return []
    found = []
    for path in sorted(Path(directory).glob("*.txt")):
        try:
            found.append((path.stem, path.read_text(errors="replace")))
        except OSError as exc:
            found.append((path.stem, f"not readable: {exc}"))
    return found


def annotations(title, output, reports=(), limit=LIMIT, per_level=PER_LEVEL):
    """The workflow commands of a failed step: its outcome and each failed test's assertion as errors, the tests'
    REPORTS ([(name, text)]) as warnings and then notices."""
    lines = [command("error", title, (parts(outcome(output), limit) or ["(no output)"])[0])]
    lines += [command("error", f"{title}: {name}", piece)
              for name, piece in spread(merged(failures(output)), per_level - 1, limit)]
    told = spread([(name, text) for name, text in reports if text.strip()], 2 * per_level, limit)
    lines += [command("warning" if index < per_level else "notice", f"{title}: {name}", piece)
              for index, (name, piece) in enumerate(told)]
    return lines


def report(name, text, directory=None):
    """Keep TEXT as the report NAME for a failed step's annotations: $SANDBOX_AI_CI_REPORTS/NAME.txt. Without the
    variable (every run but CI's) nothing is written. Returns the file's path or None; never raises - a report that
    cannot be written must not fail the test that wrote it."""
    directory = directory or os.environ.get(REPORTS)
    if not directory:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        path = Path(directory) / (re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "report")
        path = path.with_name(path.name + ".txt")
        path.write_text(text if isinstance(text, str) else repr(text), errors="replace")
        return str(path)
    except OSError:
        return None


def sections(document, order=()):
    """A report's text from DOCUMENT ({heading: a string, or anything JSON can write}): the headings in ORDER first,
    then the others as they come - what matters most where the first annotation shows it."""
    keys = [key for key in order if key in document] + [key for key in document if key not in order]
    text = []
    for key in keys:
        value = document[key]
        shown = value if isinstance(value, str) else json.dumps(value, default=str)
        text.append(f"== {key} ==\n{shown.rstrip()}")
    return "\n".join(text) + "\n"


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    try:
        output = Path(argv[1]).read_text(errors="replace")
    except OSError as exc:
        output = f"the step's output could not be read: {exc}"
    for line in annotations(argv[0], output, read_reports(argv[2] if len(argv) == 3 else os.environ.get(REPORTS))):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
