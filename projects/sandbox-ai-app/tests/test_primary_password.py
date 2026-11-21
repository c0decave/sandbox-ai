"""The primary password of the secrets kept in private files (docs/SECRETS_DESIGN.md), in the app: the backend's
calls go through the real `sandbox-ai secrets` with a HOME of their own (the password on standard input, in no
argument, no log); the Secrets tab says what protects the secrets and has one button for the step that is due -
Set primary password…, Unlock… or Lock - and asks for the password before a secret is kept in a locked store."""
import os
import re
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import applog, backend, widgets  # noqa: E402

# (made at run time: no command line that ran these tests can hold them)
PASSWORD = "PWCANARY-app-" + os.urandom(6).hex()
VALUE = "CANARYVALUE-app-" + os.urandom(6).hex()


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    import shutil
    import tempfile
    short = tempfile.mkdtemp(prefix="sai-pp-", dir="/tmp")  # (a socket's path is at most 104 bytes)
    monkeypatch.setenv("XDG_RUNTIME_DIR", short)
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_SECRETS_PLAIN", raising=False)
    sai = backend.SandboxAI()
    yield tmp_path, sai
    sai.secrets_lock()
    shutil.rmtree(short, ignore_errors=True)


def tree_bytes(root):
    return b"".join(path.read_bytes() for path in sorted(Path(root).rglob("*")) if path.is_file())


def test_the_backend_sets_unlocks_and_locks_through_the_command_with_the_password_on_standard_input(home, monkeypatch):
    tmp_path, sai = home
    logged = []
    monkeypatch.setattr(applog, "log", lambda *args, **kwargs: logged.append(repr((args, kwargs))))
    info = sai.secrets_protection()
    assert info == {"backend": "file", "password": False, "locked": None, "left": None, "plain": 0, "encrypted": 0,
                    "plain_choice": False}
    status, out, err = sai.set_secret("GH_PAT", VALUE)
    assert status == 1 and "sandbox-ai secrets password set" in err, "said, never a plain file by default"
    status, out, err = sai.secrets_new_password(PASSWORD)
    assert status == 0 and "primary password set" in out, err
    assert sai.secrets_protection()["locked"] is False and sai.secrets_backend() == "file"
    assert sai.set_secret("GH_PAT", VALUE)[0] == 0
    assert sai.secrets() == ("file", [("GH_PAT", "file")])
    assert sai.secrets_lock()[0] == 0 and sai.secrets_protection()["locked"] is True
    status, out, err = sai.set_secret("OTHER", VALUE)
    assert status == 1 and "sandbox-ai secrets unlock" in err
    status, out, err = sai.secrets_unlock("not-" + PASSWORD)
    assert status == 1 and "wrong primary password" in err
    assert sai.secrets_unlock(PASSWORD)[0] == 0
    assert sai.secrets_protection() == {"backend": "file", "password": True, "locked": False,
                                        "left": sai.secrets_protection()["left"], "plain": 0, "encrypted": 1,
                                        "plain_choice": False}
    assert sai.secrets_unlock("two\nlines")[0] == 2 and sai.secrets_new_password("")[0] == 2
    everything = tree_bytes(tmp_path) + "".join(logged).encode()
    assert PASSWORD.encode() not in everything and VALUE.encode() not in everything
    for entry in Path("/proc").iterdir() if Path("/proc").exists() else []:
        if entry.name.isdigit():
            try:
                assert PASSWORD.encode() not in (entry / "cmdline").read_bytes()
            except OSError:
                continue


def test_an_older_sandbox_ai_knows_no_primary_password(home, monkeypatch):
    _, sai = home
    monkeypatch.setattr(sai, "host_run", lambda host, *args, **kwargs: (
        2, "", "sandbox-ai secrets: error: argument command: invalid choice: 'status'"))
    assert sai.secrets_protection("ct1") is None
    monkeypatch.setattr(sai, "host_run", lambda host, *args, **kwargs: (0, "file\n", ""))
    assert sai.secrets_backend("ct1") == "file"  # (before 0.1.98: the word alone)
    monkeypatch.setattr(sai, "host_run", lambda host, *args, **kwargs: (
        0, "file (encrypted with the primary password; locked: sandbox-ai secrets unlock)\n", ""))
    assert sai.secrets_backend("ct1") == "file"
    monkeypatch.setattr(sai, "host_run", lambda host, *args, **kwargs: (255, "", "ssh: no route"))
    with pytest.raises(ValueError, match="no route"):
        sai.secrets_protection("ct1")


class FakeSai:
    agents = ("claude", "codex", "opencode")

    def __init__(self, protection):
        self.calls, self.protection = [], dict(protection or {})
        self.password = "the right password"

    def login_status(self): return []
    def secrets(self): return "file", [("GH_PUSH", "file")]
    def git_logins(self): return []
    def publish_logins(self): return []
    def has_identities(self): return False
    def vpn_profiles(self): return []
    def remote_hosts(self): return []
    def command(self, *args): return ["sandbox-ai", *args]

    def valid_secret(self, name, value=None):
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name or ""))

    def secrets_protection(self, host="local"):
        return dict(self.protection) if self.protection else None

    def secrets_backend(self, host="local"):
        return "file"

    def secrets_unlock(self, password, host="local"):
        self.calls.append(("unlock", password, host))
        if password != self.password:
            return 1, "", "sandbox-ai secrets: wrong primary password"
        self.protection.update(locked=False, left=28800)
        return 0, "unlocked for 8 hours (sandbox-ai secrets lock ends it)", ""

    def secrets_lock(self, host="local"):
        self.calls.append(("lock", host))
        self.protection.update(locked=True, left=None)
        return 0, "locked", ""

    def secrets_new_password(self, password, host="local"):
        self.calls.append(("new", password, host))
        self.protection.update(password=True, locked=False, left=28800, plain=0)
        return 0, "primary password set: 1 secret encrypted", ""

    def set_secret(self, name, value, host="local"):
        self.calls.append(("set", name, value, host))
        return 0, f"{name} kept (file, encrypted)", ""


LOCKED = {"backend": "file", "password": True, "locked": True, "left": None, "plain": 0, "encrypted": 2}
PLAIN = {"backend": "file", "password": False, "locked": None, "left": None, "plain": 2, "encrypted": 0}
KEYRING = {"backend": "keyring", "password": False, "locked": None, "left": None, "plain": 0, "encrypted": 0}


@pytest.fixture
def make(qapp, monkeypatch, wait):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    pages = []

    def build(protection):
        from sandbox_ai_app.credentials_page import CredentialsPage
        page = CredentialsPage(FakeSai(protection), lambda *a, **k: None)
        pages.append(page)
        page.refresh()
        assert wait(lambda: "asking" not in page.store_label.text() and page.store_label.text() != "")
        return page
    yield build
    for page in pages:
        page.deleteLater()


def test_a_locked_store_is_unlocked_with_the_password_typed_once_then_locked_again(make, wait, monkeypatch):
    page = make(LOCKED)
    assert "encrypted with the primary password, locked" in page.store_label.text()
    assert page.protect_button.isEnabled() and page.protect_button.text() == "Unlock…"
    assert page.protect_button in page.tabs.widget(page.tab_of("secrets")).button_entries(), "in the list's menu too"
    monkeypatch.setattr(widgets, "AUTO_SECRET", None)  # Cancel: nothing is asked of sandbox-ai
    page.protect_button.click()
    assert page.sai.calls == []
    monkeypatch.setattr(widgets, "AUTO_SECRET", "a wrong one")
    page.protect_button.click()
    assert wait(lambda: "wrong primary password" in page.message.text())
    assert page.protect_button.text() == "Unlock…"
    monkeypatch.setattr(widgets, "AUTO_SECRET", "the right password")
    page.protect_button.click()
    assert "sandbox-ai secrets unlock" in page.protect_button.toolTip()
    assert wait(lambda: page.protect_button.text() == "Lock" and "unlocked" in page.store_label.text())
    assert "sandbox-ai secrets lock" in page.protect_button.toolTip(), "the tooltip follows the step"
    assert page.sai.calls[-1] == ("unlock", "the right password", "local") and "unlocked for 8 hours" in page.message.text()
    assert "Primary password" in widgets.SHOWN[-1][0] and "the right password" not in repr(widgets.SHOWN)
    page.protect_button.click()
    assert wait(lambda: page.protect_button.text() == "Unlock…")
    assert page.sai.calls[-1] == ("lock", "local")


def test_plain_files_get_a_primary_password_typed_twice(make, wait, monkeypatch):
    page = make(PLAIN)
    assert "not encrypted (2 in plain files)" in page.store_label.text()
    assert page.protect_button.text() == "Set password…"
    monkeypatch.setattr(widgets, "AUTO_SECRET", "short")
    page.protect_button.click()
    assert page.sai.calls == [] and "8 characters" in widgets.SHOWN[-1][1]
    answers = iter(["a new password", "another one"])
    monkeypatch.setattr(widgets, "ask_password", lambda *args: next(answers))
    page.protect_button.click()
    assert page.sai.calls == [] and "not the same" in widgets.SHOWN[-1][1]
    asked = []
    monkeypatch.setattr(widgets, "ask_password", lambda parent, title, text, accept: asked.append(text) or "a new password")
    page.protect_button.click()
    assert wait(lambda: page.protect_button.text() == "Lock")
    assert page.sai.calls == [("new", "a new password", "local")] and len(asked) == 2
    assert "no way to recover" in asked[0] and "again" in asked[1]


def test_a_secret_for_a_locked_store_asks_for_the_password_first(make, wait, monkeypatch):
    page = make(LOCKED)
    assert wait(lambda: page.remote_secrets.get("local") is not None)
    page.name.setText("NEW_ONE")
    page.value.setText(VALUE)
    monkeypatch.setattr(widgets, "AUTO_SECRET", None)  # the password declined: the secret is not sent
    page.set_secret()
    wait(lambda: False, 0.2)
    assert page.sai.calls == []
    page.value.setText(VALUE)
    monkeypatch.setattr(widgets, "AUTO_SECRET", "the right password")
    page.set_secret()
    assert wait(lambda: len(page.sai.calls) == 2)
    assert page.sai.calls == [("unlock", "the right password", "local"), ("set", "NEW_ONE", VALUE, "local")]
    assert page.value.text() == ""
    page.sai.calls.clear()  # unlocked now: kept at once
    page.name.setText("SECOND")
    page.value.setText(VALUE)
    page.set_secret()
    assert wait(lambda: page.sai.calls == [("set", "SECOND", VALUE, "local")])


def test_the_keyring_and_an_older_sandbox_ai_have_no_such_button(make):
    page = make(KEYRING)
    assert not page.protect_button.isEnabled() and "encrypted by the system" in page.store_label.text()
    assert "no primary password" in page.protect_button.toolTip()
    page = make(None)  # `secrets status` not known there: the backend's word alone, as before
    assert not page.protect_button.isEnabled() and "private files" in page.store_label.text()
    assert page.protection_step("local") is None


def test_every_text_is_german_too():
    from sandbox_ai_app import i18n
    for text in ("Unlock…", "Lock", "Set password…", "Primary password",
                 "encrypted with the primary password, locked", "The same primary password again:"):
        assert i18n.TEXTS[text]["de"] != text and "Hauptpasswort" in " ".join(
            i18n.TEXTS[key]["de"] for key in i18n.TEXTS if "primary password" in key.lower())


def test_plain_files_chosen_for_the_host_keep_a_secret_without_asking(make, wait, monkeypatch):
    """SANDBOX_AI_SECRETS_PLAIN=1 on that host (a machine with nobody to ask): the button still offers a primary
    password, and a secret is kept as before - no password dialog in its way."""
    page = make({**PLAIN, "plain_choice": True})
    assert wait(lambda: page.remote_secrets.get("local") is not None)
    assert page.protect_button.text() == "Set password…" and page.protect_button.isEnabled()
    monkeypatch.setattr(widgets, "ask_password", lambda *args: pytest.fail("asked for a password"))
    page.name.setText("NEW_ONE")
    page.value.setText(VALUE)
    page.set_secret()
    assert wait(lambda: page.sai.calls == [("set", "NEW_ONE", VALUE, "local")])
