"""Help, tours and German texts (review round 4): every button of the Start page and of the Sandboxes page's "While it
runs" and Session boxes is explained in the help (English and German); the German texts address the user as Sie, never
du; they say Sitzung and Sandboxen, Neu laden for Refresh and Aktualisieren for Update."""
import dataclasses
import os
import re

import pytest

from sandbox_ai_app import help_content, help_texts, i18n, tours

LINT_WORDS = re.compile(r"\b(du|Du|dein\w*|Dein\w*|dir|Dir|dich|Dich)\b")
ANGLICISMS = re.compile(r"\w*(session|sandboxes|sandboxs)\w*", re.IGNORECASE)


def german_texts():
    """(where, text) of every German text the app shows: i18n.py, the Help sections, the (?) texts and the tours."""
    for key, entry in i18n.TEXTS.items():
        yield f"i18n {key[:60]!r}", entry.get("de", "")
    yield from texts_of("de")


def texts_of(language):
    for section in help_content.SECTIONS:
        yield f"help {section.id}", getattr(section.title, language)
        yield f"help {section.id}", getattr(section.body, language)
    for tip in help_texts.TIPS.values():
        for text in (tip.title, tip.what, tip.does):
            if text is not None:
                yield f"tip {tip.id}", getattr(text, language)
    for page, steps in tours.AREA_TOURS.items():
        for step in steps:
            for text in (step.title, step.text, step.try_):
                yield f"tour {page}", getattr(text, language)
    for ident, tour in tours.TASK_TOURS.items():
        for item in (tour, *tour.steps):
            for field in dataclasses.fields(item):
                value = getattr(item, field.name)
                if isinstance(value, tours.Text):
                    yield f"task {ident}", getattr(value, language)


def prose(text):
    """TEXT without what is not German prose: `code`, option names, {placeholders}, paths and URLs."""
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"(?<![\w-])--?[A-Za-z][\w-]*", " ", text)
    text = re.sub(r"\{[^}]*\}", " ", text)
    return re.sub(r"\S*/\S*", " ", text)


def test_the_lint_finds_du_and_leaves_options_code_and_paths_alone():
    for bad in ("Was du gegeben hast", "in deinem Browser", "Deine MCP-Server", "nicht von dir", "z. B. du@example.org"):
        assert LINT_WORDS.search(prose(bad)), bad
    for good in ("--log-dir DIR", "`sandbox-ai logs --log-dir`", "~/du/dein", "Ihre MCP-Server", "Durchsuchen",
                 "direkt", "Dienst", "{dir}"):
        assert not LINT_WORDS.search(prose(good)), good


def test_the_german_texts_say_sie_never_du():
    found = [f"{where}: {match.group(0)!r} in {text[:120]!r}" for where, text in german_texts()
             for match in LINT_WORDS.finditer(prose(text))]
    assert not found, "du-forms in German texts (Sie everywhere):\n" + "\n".join(found)


def test_the_german_texts_say_sitzung_and_sandboxen():
    found = [f"{where}: {match.group(0)!r}" for where, text in german_texts()
             for match in ANGLICISMS.finditer(prose(text))]
    assert not found, "Sitzung(en) and Sandboxen in German texts:\n" + "\n".join(found)


def test_refresh_is_neu_laden_and_update_is_aktualisieren():
    assert i18n.TEXTS["Refresh"]["de"] == "Neu laden"
    assert i18n.TEXTS["Update"]["de"] == "Aktualisieren"
    for key, entry in i18n.TEXTS.items():
        de = entry["de"].lower()
        if re.search(r"\bRefresh\b", key):
            # "Neu laden", or the verb split around its object ("Laden Sie zuerst neu")
            assert re.search(r"neu laden|\blade\w*\b.*\bneu\b", de) and "aktualisier" not in de, (key, entry["de"])
        if re.fullmatch(r"Update( all| plugins| sandbox-ai| the host| with sudo)?", key):
            assert "aktualisier" in de, (key, entry["de"])


# -- every button of the Start page and the Sandboxes page's boxes is in the help --

def normal(text):
    return re.sub(r"\s+", " ", text.replace("&", "").replace("…", " ")).strip().lower()


def corpus(language):
    parts = [text for _, text in texts_of(language)]
    parts += [" ".join(section.cli) for section in help_content.SECTIONS]
    return normal(" \n ".join(parts))


def mentioned(label, language):
    return re.search(r"(?<!\w)" + re.escape(normal(label)) + r"(?!\w)", corpus(language)) is not None


@pytest.fixture
def sai(tmp_path, monkeypatch):
    from sandbox_ai_app import backend
    if not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"):
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    sai = backend.SandboxAI()
    monkeypatch.setattr(sai, "host_run", lambda name, *args, timeout=120, input=None: (0, "", ""))
    monkeypatch.setattr(sai, "host_entries", lambda: [{"name": "local", "kind": "local"}])
    monkeypatch.setattr(sai, "llm_endpoints", lambda: [])
    return sai


def own_buttons(page, inside=None, skip=None):
    """The QPushButtons of PAGE (or of the widget INSIDE), not those of SKIP (the options form's own fields)."""
    from PySide6.QtWidgets import QPushButton
    found = []
    for button in (inside or page).findChildren(QPushButton):
        widget, skipped = button, False
        while widget is not None:
            if skip is not None and widget is skip:
                skipped = True
                break
            widget = widget.parentWidget()
        if not skipped and button.text().strip():
            found.append(button.text())
    return found


def missing(labels):
    absent = []
    for label in labels:
        if not mentioned(label, "en"):
            absent.append(f"en: {label!r}")
        german = i18n.TEXTS.get(label, {}).get("de")
        if german and not mentioned(german, "de"):
            absent.append(f"de: {german!r} ({label!r})")
    return absent


def test_every_button_of_the_start_page_is_in_the_help(qapp, sai):
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from sandbox_ai_app import ui
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    try:
        labels = own_buttons(page, skip=page.form)
        assert {"Dry run", "Start in the app", "Save as project configuration…"} <= set(labels)
        assert not missing(labels), "Start page buttons the help does not mention:\n" + "\n".join(missing(labels))
    finally:
        page.close()


def test_every_button_of_the_sandboxes_boxes_is_in_the_help(qapp, sai):
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from PySide6.QtWidgets import QGroupBox
    from sandbox_ai_app import ui
    page = ui.SandboxesPage(sai, lambda *a, **k: None)
    try:
        boxes = {box.title(): box for box in page.findChildren(QGroupBox)}
        live = own_buttons(page, inside=boxes[i18n.tr("While it runs")])
        assert {"Add workspace…", "Add MCP server…", "Remove MCP server…"} <= set(live)
        labels = live + own_buttons(page, inside=boxes[i18n.tr("Session")])
        assert "Resume with…" in labels
        assert not missing(labels), "Sandboxes page buttons the help does not mention:\n" + "\n".join(missing(labels))
    finally:
        page.timer.stop()
        page.remote_timer.stop()
        page.close()


def test_the_help_says_live_mcp_servers_are_kept_and_the_start_page_follows_its_host():
    live = help_texts.tip("sandboxes.live")
    assert "kept for the start's later starts" in live.does.en and "späteren Starts" in live.does.de
    launch = help_content.BY_ID["page-launch"].body.en
    for words in ("Host:", "Model:", "Preset", "Gallery…", "`--vpn`", "`--image-variant`", "`--gpu`",
                  "asking first"):
        assert words in launch, words
    gallery = help_content.BY_ID["app-presets-gallery"].body.en
    assert "one catalogue per host" in gallery and "only sandbox-ai's is shown" in gallery
    sandboxes = help_content.BY_ID["page-sandboxes"].body.en
    assert "as a tree" in sandboxes and "Resume with…" in sandboxes
    assert "Two panes" in help_content.BY_ID["page-files"].body.en
    assert [s.target for s in tours.AREA_TOURS["launch"]][:5] == [None, "agent", "host_choice", "model_choice",
                                                                  "profile"]
    assert "dry_button" in [s.target for s in tours.AREA_TOURS["launch"]]
