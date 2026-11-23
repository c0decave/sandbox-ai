"""Every "verified / tested" in what sandbox-ai ships points to its evidence, and missing evidence is no negative note
(the user, 2026-11-22: "how you know otherwise what works", "bitte alles sauber nacharbeiten"; earlier: "BITTE 'noch
nicht live getestet' überall entfernen!").

What a user or an agent reads - the READMEs and docs (not the release notes, the reviews and the research records),
the app's help and translations, the web page's texts and help, the presets, the skills and their source, and the
strings of the launcher's and the app's modules - says:
- no "not tested", "untested", "tested yet", "nicht getestet", "ungetestet": where nothing was measured it says "no data
  yet" (a neutral grey), never a warning (the ids "not tested" and "untested" of the data stay ids);
- a claim that something "is tested", "tested with/on/against", "verified on/with", "live-tested", "getestet mit/auf"
  names its evidence within the next lines: a test (tests/..., test_...), a CI job, the live check (a step of
  scripts/live_check.sh, docs/LIVE_CHECK.md), sandbox/model_evidence.json or sandbox/live_evidence.json;
- a quote of the user's own words (a source column, "the user, DATE: ...") is a quote, not a claim.
The model lights' statuses are held to their measured runs by tests/test_profile_status.py, the presets' live lights
to the live check's reports by tests/test_live_presets.py."""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
NEGATIVE = re.compile(r"\bnot tested\b|\buntested\b|\btested yet\b|nicht getestet|ungetestet|\bnot live-tested\b",
                      re.I)
POSITIVE = re.compile(r"\b(?:is|are|was|were|been) (?:\w+ )?tested\b|\btested (?:with|on|against|in|live)\b|"
                      r"\bverified (?:on|with)\b|\blive[- ]tested\b|\bgetestet (?:mit|auf|gegen)\b|"
                      r"\b(?:ist|sind|wurde|wurden) (?:\w+ )?getestet\b", re.I)
EVIDENCE = re.compile(r"tests/|test_\w+|\bCI\b|ci\.yml|e2e|live[ _-]check|Live-Prüfung|model_evidence|live_evidence|"
                      r"scripts/|LIVE_CHECK|--check|stand-ins?\b|nachgebildet", re.I)
QUOTE = re.compile(r"the user,? 20\d\d-|\(the user|„[^“]*getestet[^“]*“")
WINDOW = 3  # lines after a claim its evidence may stand on (a wrapped paragraph)
TOP = ("README.md", "README_DE.md", "QUICKSTART.md", "QUICKSTART_DE.md", "INTEGRATIONS.md", "INTEGRATIONS_DE.md",
       "SANDBOX.md", "RELEASING.md", "PROXMOX_LXC.md", "PROXMOX_LXC_DE.md")
APP = ROOT / "projects" / "sandbox-ai-app" / "sandbox_ai_app"
IDS = {"not tested", "untested"}  # the data's ids (model_usecases' status, model_profiles.UNTESTED): never shown


def texts():
    """The files of shipped text, read line by line."""
    files = [ROOT / name for name in TOP if (ROOT / name).is_file()]
    files += [path for path in sorted((ROOT / "docs").glob("*.md"))
              if not path.name.endswith("_REVIEW.md") and path.name != "AGENT_CANDIDATES.md"]
    files += [path for path in sorted((ROOT / "scripts" / "skill_source").rglob("*")) if path.is_file()]
    files += sorted(ROOT.glob("sandbox/builtin/*/skills/sandbox-ai/SKILL.md"))
    files += sorted((ROOT / "sandbox" / "presets").glob("*.json"))
    files += [ROOT / "sandbox/web/help.json", ROOT / "sandbox/web/i18n.js", APP / "help_content.py", APP / "i18n.py"]
    return files


def modules():
    return [*sorted((ROOT / "sandbox").glob("*.py")), *sorted(APP.glob("*.py")), *sorted((ROOT / "scripts").glob("*.py"))]


def shown_strings(path):
    """[(line, text)] of PATH's string literals but its docstrings (what a program prints, a page shows)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
                  and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


def test_no_shipped_text_says_not_tested_where_it_has_no_data():
    found = []
    for path in texts():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not QUOTE.search(line) and NEGATIVE.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:160]}")
    for path in modules():
        if path.parent == APP and path.name in ("i18n.py", "help_content.py"):
            continue  # (read as text above)
        for number, text in shown_strings(path):
            if text.strip().lower() not in IDS and not QUOTE.search(text) and NEGATIVE.search(text):
                found.append(f"{path.relative_to(ROOT)}:{number}: {text.strip()[:160]}")
    assert found == [], "say \"no data yet\" (neutral), never \"not tested\":\n" + "\n".join(found)


def test_every_tested_or_verified_claim_names_its_evidence():
    found = []
    for path in texts():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if QUOTE.search(line) or not POSITIVE.search(line):
                continue
            if not EVIDENCE.search(" ".join(lines[index:index + 1 + WINDOW])):
                found.append(f"{path.relative_to(ROOT)}:{index + 1}: {line.strip()[:160]}")
    assert found == [], "name the test, CI job, live check step or evidence file - or say what is known:\n" + \
        "\n".join(found)


def test_the_guard_catches_what_it_is_for(tmp_path):
    """The patterns themselves: a negative note, an unbacked claim, a backed one, a quote and the ids."""
    assert NEGATIVE.search("grey ? not tested") and NEGATIVE.search("Kombination ungetestet")
    assert NEGATIVE.search("noch nicht getestet") and not NEGATIVE.search("no data yet")
    assert POSITIVE.search("it is tested against a fake endpoint") and POSITIVE.search("live-tested on a Mac")
    assert EVIDENCE.search("(tests/test_login_keepalive.py)") and EVIDENCE.search("the CI job `macos`")
    assert not EVIDENCE.search("it works fine, trust me")
    assert QUOTE.search('| the user, 2026-11-22 ("all the harnesses have the (not tested yet)") |')
    source = tmp_path / "x.py"
    source.write_text('"""not tested here (a docstring)"""\nSTATUS = "not tested"\nprint("this is not tested yet")\n')
    assert [text for _, text in shown_strings(source) if NEGATIVE.search(text) and text not in IDS] == \
        ["this is not tested yet"]
