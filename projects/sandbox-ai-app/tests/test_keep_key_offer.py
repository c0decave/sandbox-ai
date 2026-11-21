"""A start whose key is not kept yet (a basic preset: default-goose needs ANTHROPIC_API_KEY, default-dsh
DEEPSEEK_API_KEY): the Board's refusal line only said the launcher's CLI hint (`sandbox-ai secrets set NAME`). Now
the Board offers to keep the key: a password field, then the launcher's own `secrets set NAME` (the value on its
standard input), then the board is checked again. The value is never logged, shown or put on a command line."""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from sandbox_ai_app import applog, backend, board as B, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
ROOT = Path(os.environ.get("SANDBOX_AI_HOME") or Path(__file__).resolve().parents[3])
KEY = "typed-value-" + "q7Zk" * 6  # (any text: no key of any service)
REFUSAL = ("agent-sandbox: --api-key api.anthropic.com=ANTHROPIC_API_KEY: no secret ANTHROPIC_API_KEY; keep one "
           "with: sandbox-ai secrets set ANTHROPIC_API_KEY")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return tmp_path


@pytest.fixture
def page(qapp, homes):
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    yield view
    view.deleteLater()


@pytest.fixture
def logged():
    entries = []
    applog.add_listener(entries.append)
    yield entries
    applog.remove_listener(entries.append)


def refuse(page, message=REFUSAL):
    page.checking_state = page.board.state()
    page.check_token += 1
    page.checked(page.check_token, (None, message), None, "local")


def wait_for(condition, seconds=60):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    return condition()


def test_secret_offers_name_only_what_the_launcher_says_word_for_word():
    assert B.secret_offers(REFUSAL) == ["ANTHROPIC_API_KEY"]
    two = REFUSAL + "\n  - --api-key api.deepseek.com=DEEPSEEK_API_KEY: no secret DEEPSEEK_API_KEY; keep one with: " \
                    "sandbox-ai secrets set DEEPSEEK_API_KEY\n" + REFUSAL
    assert B.secret_offers(two) == ["ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"], "each once"
    assert B.secret_offers(REFUSAL.replace("set ANTHROPIC_API_KEY", "set OTHER")) == [], "the same name twice"
    assert B.secret_offers("no secret A B; keep one with: sandbox-ai secrets set A B") == []
    assert B.secret_offers(None) == [] and B.secret_offers("The image of qwen is not built") == []


@pytest.mark.parametrize("agent, host, secret", [("goose", "api.anthropic.com", "ANTHROPIC_API_KEY"),
                                                 ("dsh", "api.deepseek.com", "DEEPSEEK_API_KEY")])
def test_the_real_launcher_refuses_a_missing_key_in_those_words(homes, agent, host, secret):
    """The words B.secret_offers reads are the ones the real launcher says (no key kept in this fresh home)."""
    done = subprocess.run([str(ROOT / "sandbox-ai"), agent, "--api-key", f"{host}={secret}", "--dry-run"],
                          capture_output=True, text=True, timeout=120, env=os.environ)
    assert done.returncode != 0
    assert B.secret_offers(done.stderr) == [secret], done.stderr


def test_the_refusal_offers_to_keep_the_key_and_a_check_without_it_takes_it_away(page):
    assert page.secret_button.isHidden()
    refuse(page)
    assert not page.secret_button.isHidden() and "ANTHROPIC_API_KEY" in page.secret_button.text()
    page.checking_state = page.board.state()
    page.check_token += 1
    page.checked(page.check_token, ({"plan": True}, None), None, "local")
    assert page.secret_button.isHidden() and page.offered_keys == []


def test_the_dialog_is_a_password_field_and_gives_the_value_only_on_ok(qapp, homes, monkeypatch):
    dialog = widgets.SecretValueDialog(None, "Keep the key", "ANTHROPIC_API_KEY: the value", "Keep")
    assert dialog.edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
    assert dialog.value() is None, "empty: nothing"
    dialog.edit.setText(KEY)
    assert dialog.value() == KEY
    dialog.deleteLater()
    monkeypatch.setattr(widgets, "AUTO_SECRET", None)
    assert widgets.ask_secret_value(None, "t", "x", "Keep") is None, "declined"
    monkeypatch.setattr(widgets, "AUTO_SECRET", KEY)
    assert widgets.ask_secret_value(None, "t", "x", "Keep") == KEY


def test_keeping_the_key_runs_the_launchers_secrets_set_then_checks_again(page, homes, monkeypatch, logged, capfd):
    refuse(page)
    checks = []
    monkeypatch.setattr(page, "check", lambda: checks.append(True))
    monkeypatch.setattr(widgets, "AUTO_SECRET", KEY)
    seen = []
    original = page.sai.set_secret
    monkeypatch.setattr(page.sai, "set_secret",
                        lambda *a, **k: seen.append((a[0], k.get("host"))) or original(*a, **k))
    page.secret_button.click()
    assert wait_for(lambda: checks), "the board is checked again once the key is kept"
    assert seen == [("ANTHROPIC_API_KEY", "local")]
    # kept by the launcher: its secrets list names it, and the dry run no longer refuses for the key
    listed = subprocess.run([str(ROOT / "sandbox-ai"), "secrets", "list"], capture_output=True, text=True, timeout=60,
                            env=os.environ).stdout
    assert "ANTHROPIC_API_KEY" in listed and KEY not in listed
    done = subprocess.run([str(ROOT / "sandbox-ai"), "goose", "--api-key", "api.anthropic.com=ANTHROPIC_API_KEY",
                           "--dry-run"], capture_output=True, text=True, timeout=120, env=os.environ)
    assert "no secret ANTHROPIC_API_KEY" not in done.stderr
    # the value is nowhere: not in the app's log, not on the page, not in a message shown
    everything = json.dumps(logged, default=str) + json.dumps(widgets.SHOWN, default=str) + "".join(capfd.readouterr())
    assert KEY not in everything and KEY not in page.verdict.text()
    assert KEY not in page.secret_button.toolTip() and KEY not in page.secret_button.text()


def test_a_failed_keep_says_why_and_never_the_value(page, homes, monkeypatch, logged):
    refuse(page)
    monkeypatch.setattr(widgets, "AUTO_SECRET", KEY)
    monkeypatch.setattr(page.sai, "set_secret", lambda *a, **k: (1, "", "the store is locked"))
    page.secret_button.click()
    assert wait_for(lambda: any("locked" in str(s) for s in widgets.SHOWN), 30)
    assert KEY not in json.dumps(widgets.SHOWN, default=str) + json.dumps(logged, default=str)


def test_a_declined_dialog_keeps_nothing(page, homes, monkeypatch):
    refuse(page)
    calls = []
    monkeypatch.setattr(page.sai, "set_secret", lambda *a, **k: calls.append(a) or (0, "", ""))
    monkeypatch.setattr(widgets, "AUTO_SECRET", None)
    page.secret_button.click()
    assert calls == []


def test_an_offer_of_another_board_is_never_kept(page, homes, monkeypatch):
    refuse(page)
    page.change("an edit", lambda: page.board.add("limits"))  # edited meanwhile: the verdict is not there yet
    calls = []
    monkeypatch.setattr(page.sai, "set_secret", lambda *a, **k: calls.append(a) or (0, "", ""))
    monkeypatch.setattr(widgets, "AUTO_SECRET", KEY)
    page.keep_secret()
    assert calls == []
