#!/usr/bin/env python3
"""The hidden-text checker: what text that steers an agent may hide from the human who reads it.

One module for every place sandbox-ai reads such text before an agent does (README "Hidden text"):

  kits         kits add-skill, kits host-import, kits attach, plugins attach: the copy is refused with the
               findings listed, unless --allow-findings
  instructions the workspace's agent instruction files (CLAUDE.md, AGENTS.md, .cursorrules ...) at every start:
               a warning and a safeguard record before the agent reads them
  MCP          the tool definitions a server lists (name, description, schema): a finding is asked about
  pushes       invisible and bidirectional characters in what a push publishes: a warning of the privacy check

What it finds (each finding: file, line, rule, reason, the line escaped, and hidden text decoded):

  hidden-text      Unicode tag characters (U+E0000-E007F) outside an emoji flag: ASCII the eye cannot see,
                   decoded and shown ("ASCII smuggling")
  invisible        zero-width and other invisible characters (U+200B, U+2060, U+FEFF inside a file, Hangul
                   fillers, variation selectors in runs or after plain ASCII - GlassWorm's encoding)
  bidi             bidirectional overrides and isolates (U+202A-202E, U+2066-2069: "Trojan Source"), and
                   direction marks in a line without right-to-left text
  control          control characters other than tab and line ends (an escape sequence can redraw a terminal)
  injection        instruction phrasing aimed at the model: ignore previous instructions, without telling the
                   user, silently send, before using this tool run ..., disable the sandbox
  credential-path  credential stores and commands: ~/.ssh, ~/.aws, ~/.kube, .netrc, gh auth token, id_rsa
  pipe-to-shell    curl ... | sh and its relatives
  raw-address      a URL to a bare IP address
  encoded          a long base64 run (400 characters or more)

Deterministic, local, standard library only: nothing is sent anywhere. Emoji sequences (ZWJ, U+FE0F after a
symbol, flag tags) and the joiners of scripts that need them are no findings.

  sandbox-ai scan PATH ... [--json]   the findings of files or folders, for a look by hand before an agent reads
                                     them (exit 0: none, 1: findings, 2: a path not readable)
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata

FILE_LIMIT = 1 << 20      # bytes of one file that are checked
TREE_FILES = 5000         # files of one folder that are checked
TREE_DEPTH = 12
SHOWN = 20                # findings listed in a refusal or warning
TEXT_WIDTH = 160

RULES = {
    "hidden-text": "Unicode tag characters: text the eye cannot see",
    "invisible": "invisible characters",
    "bidi": "bidirectional control characters (the text displays in another order than it reads)",
    "control": "control characters",
    "injection": "instruction phrasing aimed at the model",
    "credential-path": "a credential store or command",
    "pipe-to-shell": "a download piped into a shell",
    "raw-address": "a URL to a bare IP address",
    "encoded": "a long base64 run",
}
HIDDEN_RULES = ("hidden-text", "invisible", "bidi", "control")

# What the privacy check of pushes warns about (privacy.py keeps a copy of this regex: it runs alone in the
# sandbox and in the proxy; tests/test_lint.py keeps both the same): tags outside a flag, zero-width characters,
# bidi overrides and isolates, the variation selectors' supplement, and runs of variation selectors.
PUSH_REGEX = (r"(?<![\U0001F3F4\U000E0000-\U000E007F])[\U000E0000-\U000E007F]"
              r"|[\u200b\u2060-\u2064\u180e\u115f\u1160\u3164\uffa0\u202a-\u202e\u2066-\u2069]"
              r"|(?<=.)\ufeff|[\U000E0100-\U000E01EF]|[\ufe00-\ufe0f]{2,}")

ZERO_WIDTH = set("\u200b\u2060\u2061\u2062\u2063\u2064\u180e\u115f\u1160\u3164\uffa0\u034f\u17b4\u17b5\u2028\u2029")
BIDI_CONTROLS = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
BIDI_MARKS = set("\u200e\u200f\u061c")
JOINERS = set("\u200c\u200d")
TAG_FLAG = "\U0001F3F4"
KEYCAP_BASES = set("0123456789#*")

_I = re.IGNORECASE
PATTERNS = [
    ("injection", re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)?(?:of\s+)?(?:your\s+|the\s+)?"
        r"(?:previous|prior|above|earlier|preceding|system|original)\s+(?:instructions?|prompts?|rules|guidelines|"
        r"directions|messages?|context)", _I)),
    ("injection", re.compile(
        r"\b(?:do\s+not|don'?t|never|without)\s+(?:ever\s+)?(?:tell(?:ing)?|inform(?:ing)?|notify(?:ing)?|"
        r"ask(?:ing)?|mention(?:ing)?|reveal(?:ing)?|show(?:ing)?|alert(?:ing)?|warn(?:ing)?|consult(?:ing)?)"
        r"\s+(?:this\s+to\s+|it\s+to\s+|anything\s+to\s+|about\s+(?:this|it)\s+(?:to\s+)?)?(?:the\s+)?"
        r"(?:user|human|developer|operator|owner)", _I)),
    ("injection", re.compile(
        r"\bwithout\s+the\s+(?:user|human|developer|owner)(?:'s)?\s+(?:knowledge|knowing|noticing|consent|"
        r"permission|approval|seeing)", _I)),
    ("injection", re.compile(
        r"\b(?:silently|secretly|quietly|covertly|stealthily|discreetly)\s+(?:send|upload|post|exfiltrate|forward|"
        r"transmit|copy|run|execute|install|curl|fetch|read|collect|include|add|pass)", _I)),
    ("injection", re.compile(
        r"\bbefore\s+(?:using|calling|invoking|running)\s+(?:this|any|the|these)\s+tools?\b.{0,60}?"
        r"\b(?:run|execute|read|call|send|fetch|cat|pass|include)\b", _I)),
    ("injection", re.compile(
        r"\b(?:you\s+are\s+now\s+(?:in\s+)?(?:developer|admin|god|unrestricted|jailbreak)|new\s+system\s+prompt|"
        r"system\s+override|ignore\s+(?:all\s+)?safety)", _I)),
    ("injection", re.compile(
        r"\b(?:disable|turn\s+off|bypass|circumvent|evade|escape|get\s+around)\s+(?:the\s+)?(?:sandbox|firewall|"
        r"safeguards?|security\s+(?:checks?|mode)|guardrails?|approvals?|privacy\s+check|proxy)", _I)),
    ("injection", re.compile(r"\bexfiltrat\w*", _I)),
    ("credential-path", re.compile(
        r"(?:~|\$HOME|\$\{HOME\}|/home/[^/\s]+|/root)/\.(?:ssh|aws|kube|gnupg|docker/config\.json|netrc|npmrc|pypirc|"
        r"git-credentials|config/gh|config/gcloud|azure)\b")),
    ("credential-path", re.compile(r"\bgh\s+auth\s+token\b|\bid_(?:rsa|ed25519|ecdsa|dsa)\b(?!\.pub)|"
                                   r"\.aws/credentials\b|/etc/shadow\b|\bsecurity\s+find-generic-password\b")),
    ("pipe-to-shell", re.compile(
        r"\b(?:curl|wget|fetch|iwr|Invoke-WebRequest)\b[^|\n]{0,300}\|\s*(?:sudo\s+(?:-\S+\s+)*)?"
        r"(?:ba|z|da|k|fi)?sh\b|\b(?:ba)?sh\s+(?:-c\s+)?[\"']?\$?\(\s*(?:curl|wget)\b|"
        r"\b(?:ba)?sh\s+<\(\s*(?:curl|wget)\b|\|\s*(?:iex|Invoke-Expression)\b", _I)),
    ("raw-address", re.compile(r"\b(?:https?|ftp|wss?)://(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")),
    ("encoded", re.compile(r"[A-Za-z0-9+/]{400,}={0,2}")),
]


def pattern_data():
    """{"patterns": [[rule, regex, ignore case]], "reasons": RULES}: the phrasing rules as data, for a checker that
    cannot load this module (the run's MCP relay: runtime.HiddenText)."""
    return {"patterns": [[rule, pattern.pattern, bool(pattern.flags & re.IGNORECASE)] for rule, pattern in PATTERNS],
            "reasons": dict(RULES)}


def name(char):
    return unicodedata.name(char, f"U+{ord(char):04X}")


def code(char):
    point = ord(char)
    return f"\\u{point:04x}" if point <= 0xFFFF else f"\\U{point:08x}"


def is_ideograph(char):
    point = ord(char)
    return 0x3400 <= point <= 0x4DBF or 0x4E00 <= point <= 0x9FFF or 0xF900 <= point <= 0xFAFF \
        or 0x20000 <= point <= 0x3134F


def has_rtl(line):
    return any(unicodedata.bidirectional(c) in ("R", "AL") for c in line)


def kind(line, index, first_line=False):
    """The rule CHAR at INDEX of LINE breaks (hidden-text, invisible, bidi, control), or None."""
    char = line[index]
    point = ord(char)
    if point < 0x20 or 0x7F <= point <= 0x9F:
        if char == "\t" or (char in "\r\f\v" and index == len(line) - 1):
            return None
        return "control"
    if point < 0x80:
        return None
    before = line[index - 1] if index else ""
    after = line[index + 1] if index + 1 < len(line) else ""
    if 0xE0000 <= point <= 0xE007F:
        back = index - 1
        while back >= 0 and 0xE0000 <= ord(line[back]) <= 0xE007F:
            back -= 1
        return None if back >= 0 and line[back] == TAG_FLAG else "hidden-text"
    if char in BIDI_CONTROLS:
        return "bidi"
    if char in BIDI_MARKS or 0x600 <= point <= 0x605 or point in (0x6DD, 0x70F, 0x8E2):
        return None if has_rtl(line) else "bidi"
    if char in ZERO_WIDTH:
        return "invisible"
    if char == "\ufeff":
        return None if index == 0 and first_line else "invisible"
    if char in JOINERS:
        # Emoji sequences and the scripts that need joiners: none between two plain ASCII characters.
        return "invisible" if (not before or ord(before) < 0x80) and (not after or ord(after) < 0x80) else None
    if 0xFE00 <= point <= 0xFE0F:
        if before and 0xFE00 <= ord(before) <= 0xFE0F or after and 0xFE00 <= ord(after) <= 0xFE0F:
            return "invisible"  # a run: data, not a presentation choice
        return "invisible" if not before or (ord(before) < 0x80 and before not in KEYCAP_BASES) else None
    if 0xE0100 <= point <= 0xE01EF:
        return None if before and is_ideograph(before) and not (after and 0xE0100 <= ord(after) <= 0xE01EF) \
            else "invisible"
    if unicodedata.category(char) == "Cf" and char != "\u00ad":
        return "invisible"
    if unicodedata.category(char) in ("Co", "Cn") and 0xE000 <= point <= 0xF8FF:
        return None  # private use (icon fonts): visible as a glyph or a box
    return None


def escaped(text, limit=TEXT_WIDTH):
    """TEXT for a terminal: every character a finding could hide behind as its escape, shortened."""
    shown = []
    for index, char in enumerate(text):
        if kind(text, index) is not None or not char.isprintable() and char != " ":
            shown.append(code(char))
        else:
            shown.append(char)
    line = "".join(shown).strip()
    return line if len(line) <= limit else line[:limit - 1] + "\u2026"


def decoded_tags(line, start):
    """(text, end): the run of tag characters from START decoded to the ASCII it stands for."""
    end, out = start, []
    while end < len(line) and 0xE0000 <= ord(line[end]) <= 0xE007F:
        point = ord(line[end]) - 0xE0000
        out.append(chr(point) if 0x20 <= point < 0x7F else "")
        end += 1
    return "".join(out), end


def check_line(line, path="", number=1, first_line=None):
    """[finding] of one LINE (see check_text)."""
    found, seen = [], set()
    first = number == 1 if first_line is None else first_line
    index = 0
    while index < len(line):
        rule = kind(line, index, first)
        if rule is None:
            index += 1
            continue
        if rule == "hidden-text":
            text, end = decoded_tags(line, index)
            found.append({"file": path, "line": number, "rule": rule, "reason": RULES[rule],
                          "chars": f"U+{ord(line[index]):04X}..U+{ord(line[end - 1]):04X} ({end - index})",
                          "hidden": text[:500], "text": escaped(line)})
            for item in check_phrasing(text, path, number):
                item["hidden"] = text[:500]
                found.append(item)
            index = end
            continue
        if rule not in seen:
            seen.add(rule)
            found.append({"file": path, "line": number, "rule": rule, "reason": RULES[rule],
                          "chars": f"U+{ord(line[index]):04X} {name(line[index])}", "text": escaped(line)})
        index += 1
    found += check_phrasing(line, path, number)
    return found


def check_phrasing(line, path, number):
    found, seen = [], set()
    for rule, pattern in PATTERNS:
        if rule in seen:
            continue
        match = pattern.search(line)
        if match:
            seen.add(rule)
            found.append({"file": path, "line": number, "rule": rule, "reason": RULES[rule],
                          "match": escaped(match.group(0), 80), "text": escaped(line)})
    return found


def check_text(text, path=""):
    """[{"file", "line", "rule", "reason", "text" (escaped), "chars"|"match", "hidden" (decoded tags)}] of TEXT."""
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        found += check_line(line, path, number)
    return found


def is_text(data):
    return b"\0" not in data[:8192]


def check_bytes(data, path=""):
    """The findings of a file's DATA (its first FILE_LIMIT bytes); none for a binary file."""
    if not is_text(data):
        return []
    return check_text(data[:FILE_LIMIT].decode("utf-8", "replace"), path)


def check_files(files):
    """The findings of FILES ([(path, bytes, mode)], hostkits' parts)."""
    found = []
    for path, data, *_ in files:
        found += check_bytes(data, path)
    return found


def read_limited(path, limit=FILE_LIMIT):
    """The first LIMIT bytes of the regular file PATH (never through a symlink), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, size = [], 0
        while size < limit:
            chunk = os.read(fd, min(1 << 20, limit - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def check_tree(root, label=None):
    """The findings of the file or folder ROOT, symlinks never followed, at most TREE_FILES files; paths relative
    to ROOT (LABEL/relative when LABEL is given)."""
    root = Path(root)
    found, files = [], 0
    try:
        info = os.lstat(root)
    except OSError:
        return []
    if stat.S_ISREG(info.st_mode):
        data = read_limited(root)
        return check_bytes(data, label or root.name) if data is not None else []
    if not stat.S_ISDIR(info.st_mode):
        return []
    for directory, dirs, names, in walk(root):
        for item in sorted(names):
            files += 1
            if files > TREE_FILES:
                return found
            path = Path(directory) / item
            data = read_limited(path)
            if data is None:
                continue
            relative = path.relative_to(root).as_posix()
            found += check_bytes(data, f"{label}/{relative}" if label else relative)
    return found


def walk(root):
    top = len(Path(root).parts)
    for directory, dirs, names in os.walk(root, followlinks=False):
        depth = len(Path(directory).parts) - top
        dirs[:] = sorted(d for d in dirs if d != ".git" and depth < TREE_DEPTH)
        yield directory, dirs, names


def hidden_only(findings):
    return [f for f in findings if f["rule"] in HIDDEN_RULES]


def line_of(finding):
    """One line for a terminal: FILE:LINE [rule] reason: the evidence."""
    where = f"{finding['file']}:{finding['line']}" if finding.get("file") else f"line {finding['line']}"
    evidence = finding.get("chars") or finding.get("match") or ""
    text = f"{where} [{finding['rule']}] {finding['reason']}"
    if evidence:
        text += f" ({evidence})"
    if finding.get("hidden"):
        text += f"; hidden text: {escaped(finding['hidden'], 120)!r}"
    elif finding.get("text"):
        text += f": {finding['text']}"
    return text


def lines(findings, limit=SHOWN):
    shown = [line_of(f) for f in findings[:limit]]
    if len(findings) > limit:
        shown.append(f"... and {len(findings) - limit} more")
    return shown


def summary(findings):
    """'3 findings: hidden-text, injection' (the rules found, in RULES' order)."""
    rules = [rule for rule in RULES if any(f["rule"] == rule for f in findings)]
    return f"{len(findings)} finding{'s' if len(findings) != 1 else ''}: {', '.join(rules)}"


def refusal(what, findings, flag="--allow-findings"):
    """The text of a refused copy of WHAT with FINDINGS."""
    return (f"{what} holds text an agent would read that may hide instructions from you ({summary(findings)}) - "
            f"nothing was copied. Look at it (the escapes show what is invisible), take it out, or repeat with "
            f"{flag} if you trust it:\n  " + "\n  ".join(lines(findings)))


# --- the workspace's agent instruction files --------------------------------------------------------------------------

# What agents read on their own from a workspace (their project instructions, rules, skills and commands).
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENT.md", "GEMINI.md", "QWEN.md", ".goosehints",
                     ".cursorrules", ".windsurfrules", ".clinerules", ".github/copilot-instructions.md",
                     ".hermes.md", "HERMES.md", ".letta/settings.json")
INSTRUCTION_TREES = (".cursor/rules", ".claude/skills", ".claude/commands", ".claude/agents", ".github/instructions",
                     ".github/prompts", ".clinerules", ".windsurf/rules", ".agents/skills", ".opencode/agent",
                     ".opencode/command", ".gemini/commands", ".qwen/commands", ".codex/prompts")
INSTRUCTION_LIMIT = 200  # files of one workspace that are checked at a start


def instruction_files(workspace):
    """[(relative path, Path)] of WORKSPACE's agent instruction files (regular files, no symlink followed)."""
    workspace = Path(workspace)
    found = []
    for relative in INSTRUCTION_FILES:
        path = workspace / relative
        try:
            if not any(p.is_symlink() for p in (path, *path.parents) if workspace in p.parents) \
                    and stat.S_ISREG(os.lstat(path).st_mode):
                found.append((relative, path))
        except OSError:
            continue
    for relative in INSTRUCTION_TREES:
        tree = workspace / relative
        try:
            if tree.is_symlink() or not tree.is_dir():
                continue
        except OSError:
            continue
        for directory, _, names in walk(tree):
            for item in sorted(names):
                if len(found) >= INSTRUCTION_LIMIT:
                    return found
                path = Path(directory) / item
                if path.suffix.lower() not in (".md", ".mdc", ".txt", ".toml", ".json", ".yaml", ".yml", "") \
                        and item != "SKILL.md":
                    continue
                found.append((path.relative_to(workspace).as_posix(), path))
    return list(dict.fromkeys(found))


def check_instructions(workspace):
    """The findings of WORKSPACE's agent instruction files, paths relative to it."""
    found = []
    for relative, path in instruction_files(workspace):
        data = read_limited(path)
        if data is not None:
            found += check_bytes(data, relative)
    return found


def parser():
    p = argparse.ArgumentParser(
        prog="sandbox-ai scan", allow_abbrev=False,
        description="The hidden-text checker on files or folders: Unicode tag characters (decoded), zero-width, "
                    "bidirectional and control characters, instructions aimed at the model, credential paths, curl | "
                    "sh, bare-IP URLs, long base64. Local and deterministic; links are never followed, at most "
                    f"{TREE_FILES} files and {FILE_LIMIT >> 20} MiB of each are read. Exit 0: no findings, 1: "
                    "findings, 2: a path that cannot be read.")
    p.add_argument("paths", nargs="+", metavar="PATH", help="a file or folder (a skill, a plugin, a repository)")
    p.add_argument("--json", action="store_true", help="as JSON: {\"paths\": [{\"path\", \"findings\"}], "
                                                       "\"findings\": COUNT}")
    return p


def main(argv, out=print):
    a = parser().parse_args(argv)
    results, missing = [], []
    for value in a.paths:
        path = Path(value).expanduser()
        try:
            info = os.stat(path)  # the path you gave may be a link; nothing below it is followed
        except OSError as exc:
            missing.append(value)
            print(f"sandbox-ai scan: {value}: {exc.strerror or exc}", file=sys.stderr)
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            missing.append(value)
            print(f"sandbox-ai scan: {value}: neither a file nor a folder", file=sys.stderr)
            continue
        real = path.resolve()
        # A folder's findings name its files relative to it, a file's its path as given.
        findings = check_tree(real, None if stat.S_ISDIR(info.st_mode) else value)
        results.append({"path": value, "findings": findings})
    total = sum(len(item["findings"]) for item in results)
    if a.json:
        out(json.dumps({"paths": results, "findings": total, "unreadable": missing}, ensure_ascii=True))
    else:
        for item in results:
            if item["findings"]:
                out(f"{item['path']}: {summary(item['findings'])}")
                for line in lines(item["findings"], limit=len(item["findings"])):
                    out(f"  {line}")
            else:
                out(f"{item['path']}: no findings")
    return 2 if missing else 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
