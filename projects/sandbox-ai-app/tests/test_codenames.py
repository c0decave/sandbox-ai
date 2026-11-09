"""Codenames (sandbox-ai's codenames.py): a start without a name gets one such as brisk-otter-lantern from the
launcher, which the app shows as the start's name (read back from `sandbox-ai list`). The start form's name field
says so, in English and German; --name-style has its words and its place in the form."""
import os

import pytest

from sandbox_ai_app import backend

needs_sai = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    return tmp_path


@needs_sai
def test_the_name_field_says_what_an_empty_one_gets(qapp, isolated):
    from sandbox_ai_app import i18n, labels, ui
    options = backend.SandboxAI().options()
    assert "--name-style" in [o.long for o in options] and "--name-style" in backend.GROUPS["Workspace"]
    assert labels.OPTION_LABELS["--name-style"] == ("How starts without a name are named",
                                                    "Wie Starts ohne Namen heißen")
    for language, text in (("en", "empty: a codename like brisk-otter-lantern"),
                           ("de", "leer: ein Codename wie brisk-otter-lantern")):
        i18n.set_language(language)
        try:
            form = ui.OptionsForm(options)
            assert form.widgets["--name"].placeholderText() == text
            assert form.widgets["--project-name"].placeholderText() == "NAME", "the other fields keep theirs"
            style = form.widgets["--name-style"]
            assert [style.itemText(i) for i in range(style.count())] == ["", "codename", "project"]
            form.deleteLater()
        finally:
            i18n.set_language("en")
