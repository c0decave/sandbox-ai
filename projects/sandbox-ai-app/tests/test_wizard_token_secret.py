"""The wizard's Git page keeps a typed push token as a secret of sandbox-ai (since 0.1.98), never in a file: a locked
store asks for the primary password first (hidden), a store without one asks to set it; an older project file that
names a token file moved into the secrets shows that secret."""
import json
import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from conftest import wait_until  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

PASSWORD = "correct horse battery"
TOKEN = "ghp_" + os.urandom(12).hex()


@pytest.fixture
def wizard(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    import tempfile
    runtime = tempfile.mkdtemp(prefix="sai-wt-", dir="/tmp")  # (short: a socket's path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_SECRETS_PLAIN", raising=False)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.wizard import SandboxWizard
    w = SandboxWizard(backend.SandboxAI(), lambda: {"default_agent": "codex", "default_template": "development"})
    yield w
    w.sai.secrets_lock()
    w.deleteLater()
    import shutil
    shutil.rmtree(runtime, ignore_errors=True)


def tree_bytes(root):
    return b"".join(p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file() and not p.is_symlink())


def keep(qapp, page):
    """Validate the page: a typed token is kept in the background (the GUI never waits on sandbox-ai; a password asked
    on the way); the page is valid once the row names the secret."""
    if page.validatePage():
        return True
    wait_until(qapp, lambda: not getattr(page, "keeping", False), timeout=60)
    if page.token_edit(0).text() == TOKEN:
        return False  # not kept: the note says why
    return page.validatePage()


def typed_row(wizard):
    page = wizard.git_page
    page.initializePage()
    page.add_row("https://github.com", "typed", TOKEN)
    return page


def test_a_locked_store_asks_for_the_password_then_keeps_the_token_encrypted(qapp, wizard, tmp_path, monkeypatch):
    assert wizard.sai.secrets_new_password(PASSWORD)[0] == 0
    assert wizard.sai.secrets_lock()[0] == 0
    monkeypatch.setattr(widgets, "AUTO_SECRET", None)  # the password dialog cancelled: nothing kept, said why
    page = typed_row(wizard)
    assert not keep(qapp, page)
    assert "sandbox-ai secrets unlock" in page.note.text()
    monkeypatch.setattr(widgets, "AUTO_SECRET", PASSWORD)
    assert keep(qapp, page), page.note.text()
    assert any(shown[0] == "Primary password" for shown in widgets.SHOWN)
    assert page.node("git").props["--git-token-secret"] == ["https://github.com=GIT_TOKEN_GITHUB_COM"]
    assert TOKEN.encode() not in tree_bytes(tmp_path), "kept encrypted, in no file"
    assert page.token_edit(0).text() == "GIT_TOKEN_GITHUB_COM"


def test_without_a_primary_password_it_is_set_first(qapp, wizard, tmp_path, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_SECRET", PASSWORD)  # typed twice, the same
    page = typed_row(wizard)
    assert keep(qapp, page), page.note.text()
    assert len([shown for shown in widgets.SHOWN if shown[0] == "Primary password"]) == 2
    assert TOKEN.encode() not in tree_bytes(tmp_path)
    assert (tmp_path / "data/sandbox-ai/secrets/.primary.json").is_file()


def test_an_older_project_files_moved_token_file_shows_its_secret(wizard, tmp_path):
    folder = tmp_path / "config/sandbox-ai/tokens"
    folder.mkdir(parents=True, mode=0o700)
    moved = folder / "github.com"
    moved.write_text("sandbox-ai-token-moved-to-secret GIT_TOKEN_GITHUB_COM\n")
    moved.chmod(0o600)
    page = wizard.git_page
    page.node("git").props["--git-token-file"] = [f"https://github.com={moved}"]
    page.initializePage()
    assert page.table.cellWidget(0, 1).currentData() == "secret" and page.token_edit(0).text() == \
        "GIT_TOKEN_GITHUB_COM"
    assert page.validatePage()
    props = page.node("git").props
    assert props.get("--git-token-secret") == ["https://github.com=GIT_TOKEN_GITHUB_COM"]
    assert not props.get("--git-token-file")
    assert json.dumps(props, default=str).count("GIT_TOKEN_GITHUB_COM") == 1
