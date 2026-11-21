"""The package guard's questions on the Approvals page (the real page, offscreen): the row names the package, its
version, its age and the advisory; a known malicious version can be allowed once only, with the reason on the other
buttons; a young version at most for the run, with the command that allows it for the project; German texts."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from sandbox_ai_app import i18n, pages  # noqa: E402

from test_approvals_buttons import answers_to, env, fake_run, page, question, select  # noqa: E402,F401

URL = "https://registry.npmjs.org/evil-pkg/-/evil-pkg-1.0.0.tgz"
MALICIOUS = {"kind": "http", "method": "GET", "url": URL, "rule": "malicious", "ruleset": "package-guard",
             "host": "registry.npmjs.org", "path": "/evil-pkg/-/evil-pkg-1.0.0.tgz",
             "package": {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "age_days": None,
                         "malicious": "MAL-2025-1"}}
YOUNG = {"kind": "http", "method": "GET", "url": "https://files.pythonhosted.org/packages/a/b/requests-2.32.0.whl",
         "rule": "min-age", "ruleset": "package-guard",
         "package": {"ecosystem": "pypi", "name": "requests", "version": "2.32.0", "age_days": 5, "new_name_days": 5,
                     "allow": "sandbox-ai packages allow pypi:requests@2.32.0"}}


def test_the_question_names_the_package_its_age_and_the_advisory(qapp):
    text = pages.describe(MALICIOUS)
    assert text.startswith("download the npm package evil-pkg 1.0.0: KNOWN MALICIOUS (advisory MAL-2025-1), "
                           "release age unknown")
    assert URL in text
    text = pages.describe(YOUNG)
    assert "download the pypi package requests 2.32.0: released 5 days ago" in text
    assert "first release 5 days ago: possibly a hallucinated or slopsquatted name" in text
    assert "released 1 day ago" in pages.describe({**YOUNG, "package": {**YOUNG["package"], "age_days": 1}})
    # what the agent's request chose is escaped; a question without a package is a request's as before
    hostile = {**YOUNG, "package": {**YOUNG["package"], "name": "x\x1b[31m\ny", "version": "1‮0"}}
    assert "\x1b" not in pages.describe(hostile) and "\n" not in pages.describe(hostile)
    assert "‮" not in pages.describe(hostile)
    plain = {key: value for key, value in YOUNG.items() if key != "package"}
    assert pages.describe(plain).startswith("GET https://files.pythonhosted.org") and "min-age" in pages.describe(plain)
    assert pages.package_of({**YOUNG, "package": "npm"}) is None and pages.package_of({"kind": "connect"}) is None
    # why it is asked, with the command that allows the package for the project
    assert "list of known malicious packages (advisory MAL-2025-1" in pages.why_asked(MALICIOUS)
    assert "this one download only" in pages.why_asked(MALICIOUS)
    assert "sandbox-ai packages allow pypi:requests@2.32.0" in pages.why_asked(YOUNG)
    # the widest answer: a malicious version once, a young one for the run
    assert pages.local_widest("always", {}, MALICIOUS) == "once" and pages.local_widest("always", {}, YOUNG) == "run"


def test_german(qapp):
    i18n.set_language("de")
    try:
        text = pages.describe(MALICIOUS)
        assert text.startswith("das npm-Paket evil-pkg 1.0.0 herunterladen: BEKANNT BÖSARTIG (Meldung MAL-2025-1), "
                               "Alter der Version unbekannt")
        assert "vor 5 Tagen veröffentlicht" in pages.describe(YOUNG)
        assert "möglicherweise ein halluzinierter oder per Slopsquatting besetzter Name" in pages.describe(YOUNG)
        assert pages.why_asked(MALICIOUS).startswith("Der Paketwächter fragt: Diese Version steht auf der Liste")
        assert "Um das Paket in jedem Lauf des Projekts zu erlauben: sandbox-ai packages allow" in pages.why_asked(YOUNG)
    finally:
        i18n.set_language("en")


def test_a_malicious_version_is_allowed_once_at_most_on_the_page(env, qapp, page, wait, monkeypatch):  # noqa: F811
    directory = fake_run(env)
    question(directory, "1763500000-7-1", **MALICIOUS)
    question(directory, "1763500000-7-2", **YOUNG)
    page.poll()
    shown = [page.open.item(row, column).text() for row in range(page.open.rowCount())
             for column in range(page.open.columnCount()) if page.open.item(row, column) is not None]
    assert any("evil-pkg 1.0.0" in cell and "KNOWN MALICIOUS (advisory MAL-2025-1)" in cell for cell in shown), shown
    assert any("requests 2.32.0" in cell and "released 5 days ago" in cell for cell in shown), shown
    select(page, "1763500000-7-1")
    assert page.allow_button.isEnabled()
    assert not page.allow_run_button.isEnabled() and not page.allow_always_button.isEnabled()
    for button in (page.allow_run_button, page.allow_always_button):
        assert "known malicious package version is allowed for this one download at most" in button.toolTip()
    calls = answers_to(page, monkeypatch)
    page.answer("allow", "run")  # by its key or the API: a scope the cap forbids is not sent
    assert calls == [] and "one download at most" in page.message.text()
    select(page, "1763500000-7-2")
    assert page.allow_button.isEnabled() and page.allow_run_button.isEnabled()
    assert not page.allow_always_button.isEnabled()
    assert "sandbox-ai packages allow pypi:requests@2.32.0" in page.allow_always_button.toolTip()
    page.allow_run_button.click()
    assert wait(lambda: calls == [("1763500000-7-2", "allow", "run")] and not page.answers)
