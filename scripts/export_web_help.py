#!/usr/bin/env python3
"""The web page's help (sandbox/web/help.json) from the desktop app's (projects/sandbox-ai-app/sandbox_ai_app/
help_content.py): its sections about sandbox-ai itself ("concepts"), in English and German, as the page shows them
next to each page's (?) link. The app's own pages ("app") describe the desktop app's controls and stay out.

  python3 scripts/export_web_help.py           write sandbox/web/help.json
  python3 scripts/export_web_help.py --check   exit 1 when help.json is not what the app's help gives now

One source for both: a change of the app's help is exported here again (tests/test_web_i18n.py fails until it is).
Standard library only; nothing of Qt is loaded (help_content.py and tours.py have none).
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "projects" / "sandbox-ai-app"
TARGET = ROOT / "sandbox" / "web" / "help.json"
GROUP = "concepts"


def sections():
    """The app's help sections (help_content.SECTIONS), imported from its package without the rest of the app."""
    sys.path.insert(0, str(APP))
    sys.dont_write_bytecode = True  # nothing written into the app's tree
    try:
        from sandbox_ai_app import help_content  # noqa: PLC0415 - only here: the app is not on the path otherwise
    finally:
        sys.path.remove(str(APP))
    return help_content.SECTIONS


def export(found=None):
    """{"source", "sections": [{"id", "title": {en, de}, "body": {en, de}, "cli": [...], "see": [ids]}]} of the
    sections of GROUP, in the app's order; "see" only names sections that are exported too."""
    chosen = [s for s in (found if found is not None else sections()) if s.group == GROUP]
    ids = {s.id for s in chosen}
    return {"source": "projects/sandbox-ai-app/sandbox_ai_app/help_content.py (scripts/export_web_help.py)",
            "sections": [{"id": s.id, "title": {"en": s.title.en, "de": s.title.de},
                          "body": {"en": s.body.en, "de": s.body.de}, "cli": list(s.cli),
                          "see": [other for other in s.see if other in ids]} for s in chosen]}


def text(found=None):
    return json.dumps(export(found), indent=1, ensure_ascii=False) + "\n"


def main(argv):
    if argv not in ([], ["--check"]):
        print("usage: scripts/export_web_help.py [--check]   (writes sandbox/web/help.json; --check only compares)",
              file=sys.stderr)
        return 2
    wanted = text()
    if argv[:1] == ["--check"]:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != wanted:
            print(f"{TARGET} is not current: run python3 scripts/export_web_help.py", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(wanted, encoding="utf-8")
    print(f"wrote {TARGET} ({len(export()['sections'])} sections)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
