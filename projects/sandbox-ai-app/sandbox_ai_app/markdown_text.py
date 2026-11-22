"""What GitHub shows in a Markdown file beyond Qt's own parser (md4c), as Markdown Qt's parser shows well - for the
Files page's viewer (markdown_view.py). No Qt here.

- front matter (YAML between "---" lines at the start, TOML between "+++"): a code block of its language - Qt read the
  "---" as a rule and the lines as a heading (an agent's SKILL.md showed "name: x description: y" in the outline);
- GitHub's alerts (a quote whose first line is "[!NOTE]", TIP, IMPORTANT, WARNING or CAUTION): their title, in the
  app's language, on a line of its own (markdown_view colours it);
- math - "$...$" in a line, "$$...$$" (also over several lines) and a "math" fence: text people read, the common TeX
  commands as their Unicode signs (math_text) - shown, never run: what is not understood stays as written, a
  formula longer than MATH_CHARS or deeper than MATH_DEPTH too.

Nothing changes inside code (fenced, indented, in backticks), and every pass is linear in the length of the text: a
file is untrusted (an agent wrote it). The ideas come from axross-markdown (Apache-2.0, the review in
docs/AXROSS_MARKDOWN_REVIEW.md); the code is the app's own.
"""
from __future__ import annotations

import re
import unicodedata

from .i18n import N_, tr

MATH_CHARS = 2000  # characters of one formula (a longer one is shown as written)
MATH_DEPTH = 40  # groups inside each other in one formula
MATH_LINES = 200  # lines of a $$ ... $$ block
FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
# What a line of a quote or a list item starts with ("> ", "- ", "1. ") - the prefix a fence or a formula keeps.
CONTAINERS = re.compile(r"^(?:[ \t]*(?:>|[-+*]|\d{1,9}[.)])(?=[ \t]|$))*[ \t]*")
LIST_ITEM = re.compile(r"^[ \t]*(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)")
BACKTICKS = re.compile(r"`+")
INLINE_MATH = re.compile(r"(?<![\\$\w])\$(?=[^\s$])([^$\n]{1,%d}?)(?<=[^\s\\])\$(?![\w$])" % MATH_CHARS)
PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")

ALERTS = {"NOTE": ("ℹ", N_("alert|Note")), "TIP": ("★", N_("alert|Tip")), "IMPORTANT": ("‼", N_("alert|Important")),
          "WARNING": ("⚠", N_("alert|Warning")), "CAUTION": ("✖", N_("alert|Caution"))}
ALERT = re.compile(r"^( {0,3}> ?)\[!(\w+)\][ \t]*$")


def alert_titles():
    """{the title line of each alert, as rendered: its kind} (markdown_view colours those lines)."""
    return {f"{sign} {tr(key)}": kind for kind, (sign, key) in ALERTS.items()}


def escaped(text):
    """TEXT with Markdown's punctuation escaped: shown as it is (no emphasis, link or tag comes out of it)."""
    return PUNCTUATION.sub(r"\\\1", text)


def prepared(text):
    """TEXT for Qt's parser with front matter, alerts and math as GitHub shows them (see the module)."""
    text = front_matter(text)
    if "[!" not in text and "$" not in text and "math" not in text:
        return text  # (most texts: nothing else to do)
    out, lines = [], text.split("\n")
    fence, number, previous_blank, in_list, code_lines, quoted = None, 0, True, False, False, False
    while number < len(lines):
        line = lines[number]
        number += 1
        body = CONTAINERS.match(line)
        prefix, rest = line[:body.end()], line[body.end():]
        if fence is not None:
            closing = FENCE.match(rest)
            if closing and closing.group(2)[0] == fence[0] and len(closing.group(2)) >= len(fence) and \
                    not closing.group(3).strip():
                fence = None
            out.append(line)
            continue
        opened = FENCE.match(rest)
        if opened:
            if opened.group(3).strip().lower() == "math" and "`" not in opened.group(3):
                end = closed_fence(lines, number, opened.group(2), prefix)
                if end is not None:
                    out.append(math_block("\n".join(item[len(prefix):] for item in lines[number:end]), prefix))
                    number = end + 1
                    previous_blank = False
                    continue
            fence = opened.group(2)
            out.append(line)
            continue
        indented = line.startswith(("    ", "\t")) and line.strip() and not in_list and (previous_blank or code_lines)
        code_lines = bool(indented)
        if indented:
            out.append(line)
            continue
        if line.strip():
            if LIST_ITEM.match(line):
                in_list = True
            elif line[:1] not in " \t>":
                in_list = False
        is_quote = line.lstrip(" ").startswith(">")
        alert = ALERT.match(line) if is_quote and not quoted and not in_list else None
        quoted = is_quote
        previous_blank = not line.strip()
        if alert and alert.group(2).upper() in ALERTS:
            sign, key = ALERTS[alert.group(2).upper()]
            out += [f"{alert.group(1)}**{sign} {escaped(tr(key))}**", alert.group(1).rstrip()]
            continue
        if "$" not in rest:
            out.append(line)
            continue
        if rest.startswith("$$"):
            single = rest.strip()
            if len(single) > 4 and single.endswith("$$") and "$$" not in single[2:-2]:
                out.append(math_block(single[2:-2], prefix))
                continue
            end = closed_math(lines, number, prefix) if single == "$$" else None
            if end is not None:
                out.append(math_block("\n".join(item[len(prefix):] for item in lines[number:end]), prefix))
                number = end + 1
                continue
        out.append(prefix + outside_code_spans(rest, inline_math))
    return "\n".join(out)


def closed_fence(lines, start, fence, prefix):
    """The number of the line that closes FENCE (opened before line START), or None (within MATH_LINES)."""
    for number in range(start, min(len(lines), start + MATH_LINES)):
        closing = FENCE.match(lines[number][len(prefix):]) if lines[number].startswith(prefix) else None
        if closing and closing.group(2)[0] == fence[0] and len(closing.group(2)) >= len(fence) and \
                not closing.group(3).strip():
            return number
    return None


def closed_math(lines, start, prefix):
    """The number of the line "$$" that closes a display formula opened before line START, or None."""
    for number in range(start, min(len(lines), start + MATH_LINES)):
        line = lines[number]
        if not line.startswith(prefix.rstrip()) or not line.strip(" \t>"):
            return None  # (a paragraph ends: no formula)
        if line[len(prefix):].strip() == "$$":
            return number
    return None


def front_matter(text):
    """TEXT with its front matter (at its very start) as a fenced code block of its language."""
    if not text.startswith(("---", "+++")):
        return text
    first, _, rest = text.partition("\n")
    mark = first.rstrip(" \t")
    if mark not in ("---", "+++") or not rest:
        return text
    lines = rest.split("\n")
    for number, line in enumerate(lines):
        if line.rstrip(" \t") == mark or (mark == "---" and line.rstrip(" \t") == "..."):
            inside = "\n".join(lines[:number])
            runs = [len(run) for run in BACKTICKS.findall(inside)]
            fence = "`" * max(3, max(runs, default=0) + 1)
            language = "yaml" if mark == "---" else "toml"
            after = "\n".join(lines[number + 1:])
            return f"{fence}{language}\n{inside}\n{fence}\n" + after
    return text


def outside_code_spans(line, change):
    """LINE with CHANGE(text) applied to what lies outside its code spans (a run of backticks to the next run of the
    same length; the runs are found once, so a line full of unmatched ones stays linear)."""
    if "`" not in line:
        return change(line)
    runs = {}
    for found in BACKTICKS.finditer(line):
        runs.setdefault(len(found.group()), []).append(found.start())
    taken = {length: 0 for length in runs}
    out, at, plain = [], 0, 0
    while at < len(line):
        found = BACKTICKS.search(line, at)
        if found is None:
            break
        length, starts = len(found.group()), runs[len(found.group())]
        index = taken[length]
        while index < len(starts) and starts[index] <= found.start():
            index += 1
        taken[length] = index
        if index < len(starts) and (found.start() == 0 or line[found.start() - 1] != "\\"):
            close = starts[index]
            taken[length] = index + 1
            out += [change(line[plain:found.start()]), line[found.start():close + length]]
            at = plain = close + length
        else:
            at = found.end()
    out.append(change(line[plain:]))
    return "".join(out)


def inline_math(text):
    """TEXT's formulas in a line as text - not in a link's address ("](https://x.org/$a$)": its "$" stay)."""
    def formula(found):
        near = max(0, found.start() - 2000)  # (looked back a bounded way: a line of 20,000 formulas stays linear)
        if text.rfind("](", near, found.start()) > text.rfind(")", near, found.start()):
            return found.group(0)
        return math_inline(found.group(1), found.group(0))

    return INLINE_MATH.sub(formula, text)


def math_inline(tex, written):
    shown = math_text(tex)
    return escaped(written) if shown is None else escaped(shown.replace("\n", " "))


def math_block(tex, prefix=""):
    """A display formula as a paragraph of its own (its lines kept: a hard break each), PREFIX before each line."""
    shown = math_text(tex.strip())
    lines = (tex.strip() if shown is None else shown).split("\n")
    body = ("\\\n" + prefix).join(escaped(line).replace("  ", "\u00a0\u00a0") for line in lines if line.strip())
    bare = prefix.rstrip()
    return f"{bare}\n{prefix}{body}\n{bare}"


# -- TeX as text --

GREEK = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon "
         "phi chi psi omega").split()
SYMBOLS = {name: chr(0x3B1 + index + (1 if index >= 17 else 0)) for index, name in enumerate(GREEK)}
SYMBOLS.update({name.capitalize(): chr(0x391 + index + (1 if index >= 17 else 0)) for index, name in enumerate(GREEK)})
SYMBOLS.update({
    "varepsilon": "ε", "vartheta": "ϑ", "varphi": "φ", "varpi": "ϖ", "varrho": "ϱ", "varsigma": "ς", "phi": "ϕ",
    "epsilon": "ϵ", "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
    "partial": "∂", "nabla": "∇", "infty": "∞", "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "⋅",
    "cdots": "⋯", "ldots": "…", "dots": "…", "vdots": "⋮", "ddots": "⋱", "circ": "∘", "ast": "∗", "star": "⋆",
    "bullet": "∙", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡",
    "sim": "∼", "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫", "in": "∈", "notin": "∉", "ni": "∋",
    "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩", "setminus": "∖",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃", "nexists": "∄", "neg": "¬", "lnot": "¬",
    "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨", "oplus": "⊕", "otimes": "⊗", "perp": "⊥", "parallel": "∥",
    "mid": "∣", "angle": "∠", "triangle": "△", "prime": "′", "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ",
    "aleph": "ℵ", "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "implies": "⟹", "iff": "⟺", "mapsto": "↦",
    "uparrow": "↑", "downarrow": "↓", "longrightarrow": "⟶", "longleftarrow": "⟵", "langle": "⟨", "rangle": "⟩",
    "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋", "lbrace": "{", "rbrace": "}", "vert": "|",
    "Vert": "‖", "|": "‖", "{": "{", "}": "}", "$": "$", "%": "%", "&": "&", "#": "#", "_": "_", ",": "\u2009",
    ":": "\u205f", ";": "\u2004", "!": "", " ": " ", "quad": "\u2003", "qquad": "\u2003\u2003", "colon": ":",
    "deg": "deg", "top": "⊤", "bot": "⊥", "models": "⊨", "vdash": "⊢", "dagger": "†", "checkmark": "✓",
})
FUNCTIONS = set("sin cos tan cot sec csc sinh cosh tanh arcsin arccos arctan log ln lg exp lim limsup liminf max min "
                "det dim gcd lcm sup inf arg ker Pr mod bmod".split())
IGNORED = set("left right big Big bigg Bigg bigl bigr Bigl Bigr displaystyle textstyle scriptstyle limits nolimits "
              "mathrm_ middle".split())
PLAIN = set("text textrm textit textbf mathrm mathit mathbf mathsf mathtt operatorname boldsymbol mbox".split())
ACCENTS = {"vec": "\u20d7", "hat": "\u0302", "widehat": "\u0302", "bar": "\u0304", "overline": "\u0305",
           "tilde": "\u0303", "widetilde": "\u0303", "dot": "\u0307", "ddot": "\u0308", "underline": "\u0332"}
DOUBLE = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}
SUPER = dict(zip("0123456789+-=()niabcdefghjklmoprstuvwxyzABDEGHIJKLMNOPRTUVW",
                 "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃᵇᶜᵈᵉᶠᵍʰʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ"))
SUPER.update({"′": "′", "∗": "*", "*": "*"})
SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))
PAD = "\ue000"  # (an environment's columns: spaces that stay)
TOKEN = re.compile(r"\\([A-Za-z]+|.)|([{}^_&~])|(\s+)|([^\\{}^_&~\s]+)", re.DOTALL)


class Unclear(Exception):
    """The formula is not understood well enough: shown as written."""


def math_text(tex):
    """TeX math as text people read - or None: too long, too deep or unbalanced (shown as written then)."""
    if len(tex) > MATH_CHARS:
        return None
    depth = deepest = 0
    for char in tex:  # (one pass: the depth of its groups)
        depth += 1 if char == "{" else -1 if char == "}" else 0
        deepest = max(deepest, depth)
        if depth < 0:
            return None
    if depth or deepest > MATH_DEPTH:
        return None
    tokens = [(found.group(1), found.group(2), found.group(3), found.group(4)) for found in TOKEN.finditer(tex)]
    try:
        shown, _ = Reader(tokens).sequence(0)
    except (Unclear, IndexError, RecursionError):
        return None
    lines = [re.sub(r" {2,}", " ", line).strip(" ").replace(PAD, " ") for line in shown.replace("\t", " ").split("\n")]
    return "\n".join(lines).strip("\n")


class Reader:
    """A formula's tokens (TOKEN: command, sign, space, text) read into text, one group at a time."""

    def __init__(self, tokens):
        self.tokens = tokens

    def sequence(self, at, until="}"):
        """(the text of the tokens from AT up to the closing UNTIL, where it ended)."""
        out = []
        while at < len(self.tokens):
            command, sign, space, text = self.tokens[at]
            if sign == until or (command == "end" and until == "end"):
                return "".join(out), at
            if sign in ("^", "_"):  # (a script of what came before)
                script, at = self.argument(at + 1)
                out.append(raised(script, sign))
                continue
            shown, at = self.atom(at)
            out.append(shown)
        if until == "}" and at >= len(self.tokens):
            return "".join(out), at
        raise Unclear()

    def argument(self, at):
        """(the next argument - a group or one token, spaces before it skipped - as text, after it)."""
        while at < len(self.tokens) and self.tokens[at][2]:
            at += 1
        if at >= len(self.tokens):
            raise Unclear()
        command, sign, space, text = self.tokens[at]
        if text and command is None:
            return text[0], at + 1 if len(text) == 1 else self.split(at, text)
        return self.atom(at)

    def split(self, at, text):
        """(the text token AT as its first character and the rest: "\\frac12" is 1 over 2)."""
        self.tokens[at] = (None, None, None, text[1:])
        return at

    def atom(self, at):
        command, sign, space, text = self.tokens[at]
        at += 1
        if space is not None:
            return " ", at
        if text is not None:
            return text, at
        if sign == "{":
            shown, end = self.sequence(at)
            return shown, end + 1
        if sign in ("^", "_"):  # (a script of nothing: as written)
            return sign, at
        if sign == "&":
            return "\t", at
        if sign == "~":
            return " ", at
        if sign == "}":
            raise Unclear()
        return self.command(command, at)

    def command(self, name, at):
        if name == "\\":
            return "\n", at
        if name in SYMBOLS:
            return SYMBOLS[name], at
        if name in FUNCTIONS:
            return name + " ", at
        if name in IGNORED:
            return "", at
        if name in PLAIN:
            shown, at = self.argument(at)
            return shown, at
        if name in ACCENTS:
            shown, at = self.argument(at)
            return "".join(char + ACCENTS[name] for char in shown) if len(shown) <= 3 else shown, at
        if name == "mathbb":
            shown, at = self.argument(at)
            return "".join(DOUBLE.get(char, char) for char in shown), at
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            top, at = self.argument(at)
            bottom, at = self.argument(at)
            return f"{grouped(top)}/{grouped(bottom)}", at
        if name in ("binom", "dbinom", "tbinom"):
            top, at = self.argument(at)
            bottom, at = self.argument(at)
            return f"C({top}, {bottom})", at
        if name == "sqrt":
            index = ""
            if at < len(self.tokens) and (self.tokens[at][3] or "").startswith("["):
                text = self.tokens[at][3]
                if "]" not in text:
                    raise Unclear()
                index, rest = text[1:].split("]", 1)
                self.tokens[at] = (None, None, None, rest) if rest else (None, None, " ", None)
            shown, at = self.argument(at)
            return f"{raised(index, '^') if index else ''}√{grouped(shown)}", at
        if name == "begin":
            return self.environment(at)
        # (an unknown command as written, its groups right after it with their braces: "\\unknown{x}")
        written = "\\" + name
        while at < len(self.tokens) and self.tokens[at][1] == "{":
            shown, end = self.sequence(at + 1)
            written, at = written + "{" + shown + "}", end + 1
        return written, at

    def environment(self, at):
        """\\begin{name} ... \\end{name}: its rows on lines of their own, their cells in columns."""
        kind, at = self.argument(at)
        inside, end = self.sequence(at, until="end")
        _, at = self.argument(end + 1)
        rows = [[cell.strip() for cell in row.split("\t")] for row in inside.split("\n") if row.strip()]
        widths = [max(len(row[column]) if column < len(row) else 0 for row in rows)
                  for column in range(max((len(row) for row in rows), default=0))]
        # (padded with PAD: math_text makes its runs of spaces one, never these)
        lines = [(PAD * 3).join(cell + PAD * (widths[column] - len(cell)) for column, cell in enumerate(row))
                 .rstrip(PAD) for row in rows]
        if kind.endswith("matrix") and lines:
            left, right = {"pmatrix": "()", "bmatrix": "[]", "vmatrix": "||", "Bmatrix": "{}"}.get(kind, "  ")
            lines = [f"{left}{PAD}{line}{PAD}{right}".strip() for line in lines]
        return "\n" + "\n".join(lines) + "\n", at


SCRIPTS = set(SUPER.values()) | set(SUB.values())


def grouped(text):
    """TEXT as an operand: in parentheses unless it is one symbol - its scripts and accents with it ("π²") - or a
    number ("2a" is two: 1/2a would read as divided by 2, times a)."""
    text = text.strip()
    bare = "".join(char for char in text if char not in SCRIPTS and not unicodedata.combining(char))
    return text if len(bare) <= 1 or bare.isdigit() else f"({text})"


def raised(text, kind):
    """TEXT as a superscript (KIND "^") or subscript ("_"): Unicode's small signs when it has them all, else
    "^(text)"."""
    table = SUPER if kind == "^" else SUB
    text = text.strip()
    if text and all(char in table for char in text):
        return "".join(table[char] for char in text)
    return f"{kind}{grouped(text)}" if text else ""
