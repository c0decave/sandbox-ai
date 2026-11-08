"""English and German: every text in both, the same placeholders, no visible string left untranslated."""
import ast
from pathlib import Path
import re
import string

import pytest

from sandbox_ai_app import i18n

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"
# Calls whose string arguments are shown to the user and must therefore go through tr().
VISIBLE = {"QPushButton", "QLabel", "QGroupBox", "QCheckBox", "QToolButton", "plain_label", "setText",
           "setPlaceholderText", "setToolTip", "setWindowTitle", "addTab", "addItem", "addRow", "showMessage",
           "information", "warning", "question", "critical", "getText", "getExistingDirectory", "getOpenFileName",
           "getOpenFileNames", "show_text", "setTabText", "table"}
# Literal strings that are the same in every language (names, examples, symbols).
SAME = {"…", "+", "×", "English", "Deutsch", "sandbox-ai", "API_TOKEN", "GIT_TOKEN", "api.example.org pypi.org", "api.example.org *.example.net pypi.org",
        "192.0.2.10:5432", "192.0.2.0/24", "512m", "podman", "bwrap", "both"}


def placeholders(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)


def literal_calls():
    """(file, line, function, literal or tr key, translated?) of every call in the package."""
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else \
                node.func.attr if isinstance(node.func, ast.Attribute) else None
            yield path.name, node, name


def test_every_text_has_both_languages_with_the_same_placeholders():
    assert len(i18n.TEXTS) > 250
    for key, entry in i18n.TEXTS.items():
        assert set(entry) == set(i18n.LANGUAGES), key
        assert all(isinstance(entry[lang], str) and entry[lang].strip() for lang in i18n.LANGUAGES), key
        assert entry["en"] == key
        assert placeholders(entry["en"]) == placeholders(entry["de"]), key


def test_every_tr_key_in_the_code_has_an_entry():
    used = set()
    for _, node, name in literal_calls():
        if name in ("tr", "N_") and node.args and isinstance(node.args[0], ast.Constant):
            used.add(node.args[0].value)
            assert node.args[0].value in i18n.TEXTS, f"no translation for {node.args[0].value!r}"
    unused = set(i18n.TEXTS) - used
    assert not unused, f"translations nobody uses: {sorted(unused)[:10]}"


def test_no_visible_string_is_left_untranslated():
    found = []
    for file, node, name in literal_calls():
        if name not in VISIBLE:
            continue
        values = list(node.args) + [k.value for k in node.keywords if k.arg in ("text",)]
        for value in values:
            items = value.elts if isinstance(value, ast.List) else [value]
            for item in items:
                if isinstance(item, ast.Constant) and isinstance(item.value, str) and re.search("[A-Za-z]", item.value) \
                        and item.value not in SAME and not item.value.startswith(("--", "#")):
                    found.append(f"{file}:{node.lineno} {name}({item.value!r})")
                if isinstance(item, ast.JoinedStr) and any(isinstance(v, ast.Constant) and re.search("[A-Za-z]{3}", v.value)
                                                          and not v.value.strip().startswith(("--", "sandbox-ai", "session", "mcp", "net"))
                                                          for v in item.values):
                    found.append(f"{file}:{node.lineno} {name}(f-string)")
    assert not found, "\n".join(found)


def test_language_choice():
    assert i18n.system_language({"LANG": "de_DE.UTF-8"}) == "de"
    assert i18n.system_language({"LC_ALL": "en_GB.UTF-8", "LANG": "de_DE.UTF-8"}) == "en"
    assert i18n.system_language({"LANGUAGE": "de:en"}) == "de" and i18n.system_language({}) == "en"
    assert i18n.system_language({"SANDBOX_AI_APP_LANGUAGE": "de", "LANG": "C"}) == "de"
    assert i18n.set_language("de") == "de"
    assert i18n.tr("Approvals") == "Freigaben" and i18n.tr("Delete the secret {name}?", name="X") == \
        "Das Geheimnis X löschen?"
    assert i18n.tr("not a key {x}", x=1) == "not a key 1"
    i18n.set_language("en")
    assert i18n.tr("Approvals") == "Approvals"


@pytest.mark.parametrize("language", i18n.LANGUAGES)
def test_every_text_formats(language):
    i18n.set_language(language)
    for key in i18n.TEXTS:
        i18n.tr(key, **{name: "x" for name in placeholders(key)})
