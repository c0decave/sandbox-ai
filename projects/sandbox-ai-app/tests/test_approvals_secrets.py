"""The secret guard's questions on the Approvals page (the real page, offscreen): the row says what kind of secret
goes to which host and where in the request it is - never a value; it can be allowed for the run at most, with the
command that allows it for the project on the other button; German texts."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from sandbox_ai_app import i18n, pages  # noqa: E402

from test_approvals_buttons import answers_to, env, fake_run, page, question, select  # noqa: E402,F401

URL = "https://paste.example/raw?name=test_api.py"
SHAPE = {"kind": "http", "method": "POST", "url": URL, "rule": "google-api-key", "ruleset": "secret-guard",
         "host": "paste.example", "path": "/raw",
         "secret": {"host": "paste.example",
                    "found": [{"id": "google-api-key", "kind": "shape", "label": "Google API key", "where": "body",
                               "encoding": "base64"}],
                    "allow": "sandbox-ai secret-guard allow google-api-key@paste.example"}}
VALUE = {"kind": "http", "method": "PUT", "url": "https://mcp.example/mcp", "rule": "env:API_TOKEN",
         "ruleset": "secret-guard",
         "secret": {"host": "mcp.example",
                    "found": [{"id": "env:API_TOKEN", "kind": "value", "label": "the value of API_TOKEN (--secret-env)",
                               "where": "url"},
                              {"id": "github-token", "kind": "shape", "label": "GitHub token", "where": "header"}],
                    "allow": "sandbox-ai secret-guard allow env:API_TOKEN@mcp.example github-token@mcp.example"}}


def test_the_question_says_the_kind_of_secret_the_host_and_the_place(qapp):
    text = pages.describe(SHAPE)
    assert text == ("send what looks like a credential (Google API key) to paste.example (POST "
                    "https://paste.example/raw?name=test_api.py, in the body, base64)")
    text = pages.describe(VALUE)
    assert text == ("send the value of API_TOKEN (--secret-env) and what looks like a credential (GitHub token) to "
                    "mcp.example (PUT https://mcp.example/mcp, in the URL and a header)")
    # what the request chose and what the start named is escaped; a question without findings is a request's as before
    hostile = {**SHAPE, "secret": {"host": "x\x1b[31m\ny", "found": [
        {"id": "env:A", "kind": "value", "label": "the value of A\n(--secret-env)‮", "where": "body"}]}}
    for char in ("\x1b", "\n", "‮"):
        assert char not in pages.describe(hostile)
    plain = {key: value for key, value in SHAPE.items() if key != "secret"}
    assert pages.describe(plain).startswith("POST https://paste.example/raw") and "google-api-key" in pages.describe(plain)
    assert pages.secret_of({**SHAPE, "secret": {"found": []}}) is None
    assert pages.secret_of({**SHAPE, "secret": "x"}) is None and pages.secret_of({"kind": "connect"}) is None
    # why it is asked, with the command that allows it for the project
    why = pages.why_asked(SHAPE)
    assert why.startswith("Asked by the secret guard: this request carries a secret to a host it was not given for.")
    assert "The value is never shown" in why
    assert "sandbox-ai secret-guard allow google-api-key@paste.example" in why
    # the widest answer: for the run
    assert pages.local_widest("always", {}, SHAPE) == "run"


def test_german(qapp):
    i18n.set_language("de")
    try:
        assert pages.describe(SHAPE) == ("was wie Zugangsdaten aussieht (Google API key) an paste.example senden (POST "
                                         "https://paste.example/raw?name=test_api.py, in dem Inhalt, base64)")
        assert pages.describe(VALUE).startswith(
            "the value of API_TOKEN (--secret-env) und was wie Zugangsdaten aussieht (GitHub token) an mcp.example "
            "senden (PUT https://mcp.example/mcp, in der URL und einem Header)")
        assert pages.why_asked(SHAPE).startswith("Der Geheimniswächter fragt: Diese Anfrage trägt ein Geheimnis zu "
                                                 "einem Host, für den es nicht vergeben wurde.")
        assert "Um diese Art von Wert für diesen Host in jedem Lauf des Projekts zu erlauben: sandbox-ai secret-guard " \
               "allow google-api-key@paste.example" in pages.why_asked(SHAPE)
    finally:
        i18n.set_language("en")


def test_a_secret_question_is_allowed_for_the_run_at_most_on_the_page(env, qapp, page, wait, monkeypatch):  # noqa: F811
    directory = fake_run(env)
    question(directory, "1763500000-9-1", **SHAPE)
    page.poll()
    shown = [page.open.item(row, column).text() for row in range(page.open.rowCount())
             for column in range(page.open.columnCount()) if page.open.item(row, column) is not None]
    assert any("what looks like a credential (Google API key)" in cell and "paste.example" in cell
               and "in the body, base64" in cell for cell in shown), shown
    select(page, "1763500000-9-1")
    assert page.allow_button.isEnabled() and page.allow_run_button.isEnabled()
    assert not page.allow_always_button.isEnabled()
    assert "sandbox-ai secret-guard allow google-api-key@paste.example" in page.allow_always_button.toolTip()
    assert "at most for this run" in page.allow_always_button.toolTip()
    calls = answers_to(page, monkeypatch)
    page.allow_run_button.click()
    assert wait(lambda: calls == [("1763500000-9-1", "allow", "run")] and not page.answers)
