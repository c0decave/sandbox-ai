"""One word for one thing on the pages added on 2026-11-22 (round 2's consistency pass): a secret or key that is kept
is "gespeichert" in German, as the Secrets page says it ("Kept the secret {name}." - "Das Geheimnis {name} ist
gespeichert."), never "gehalten" (the LLM share dialog) or "abgelegt" (the MCP copy dialog) - and the web page reads
the same (tests/test_web_i18n.py holds the texts both have to one German)."""
import ast
from pathlib import Path

from sandbox_ai_app import i18n

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"
TODAYS = ("mcp_copy_view.py", "llm_share_view.py", "cairn_workers_view.py")
OTHER_WORDS = ("gehalten", "halten Sie", "ablegen", "abgelegt", "hinterlegt")


def texts_of(name):
    """The English texts module NAME gives tr() or N_()."""
    found = set()
    for node in ast.walk(ast.parse((PACKAGE / name).read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("tr", "N_") and node.args and \
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            found.add(node.args[0].value)
    return found


def test_a_kept_secret_is_gespeichert_on_the_new_pages():
    wrong = {}
    for name in TODAYS:
        for text in texts_of(name):
            german = (i18n.TEXTS.get(text) or {}).get("de") or ""
            if any(word in german for word in OTHER_WORDS) and "synchron gehalten" not in german:
                wrong[text] = german
    assert not wrong, wrong
    assert i18n.TEXTS["Keep {name} on {host}…"]["de"] == "{name} auf {host} speichern…"
    assert i18n.TEXTS["Kept the secret {name}."]["de"] == "Das Geheimnis {name} ist gespeichert."


def test_the_new_pages_texts_all_have_their_german():
    missing = sorted(text for name in TODAYS for text in texts_of(name) if not (i18n.TEXTS.get(text) or {}).get("de"))
    assert not missing, missing
