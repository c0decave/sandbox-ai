"""Final review 2 (user journeys, docs/reviews/final-review-2-journeys.md), the app's part.

Resume with…: its folders were one text box to type "SOURCE:DESTINATION[:ro|rw]" into (German: "QUELLE:ZIEL[:ro|rw]"
- the cryptic kind of field the user asked to be rid of, as "ZIEL=HOST_ORDNER" was), split at every ':', so a folder
of another machine (sshfs://bob@lab1:2222/srv) - which `sandbox-ai resume --mount` takes - could not be given at all.
Now each folder is a row: the folder (chosen from the start's host, or a folder of another machine typed), where the
sandbox sees it, and whether the agent may write there."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, i18n, remote_chooser as RC, widgets  # noqa: E402


@pytest.fixture
def chooser(qapp, monkeypatch):
    asked = []

    def choose(parent, host, title, mode="folder", key="folder", start="", sai=None, name_filter=""):
        asked.append({"host": host, "mode": mode})
        return "/remote/chosen/data"
    monkeypatch.setattr(RC, "choose", choose)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    return asked


def test_resume_with_has_a_row_per_folder_chosen_from_the_starts_host(qapp, chooser):
    from sandbox_ai_app import ui
    dialog = ui.ResumeDialog(None, "api", host="lab")
    try:
        texts = " ".join(w.placeholderText() for w in dialog.findChildren(QtWidgets.QLineEdit)) + " ".join(
            w.placeholderText() for w in dialog.findChildren(QtWidgets.QPlainTextEdit))
        assert "SOURCE:DESTINATION" not in texts
        assert dialog.mounts.value() == []
        row = dialog.mounts.add_row()
        row.source.browse_button.click()
        assert chooser[-1]["host"] == "lab", "the start's host's folders"
        assert row.source.value() == "/remote/chosen/data"
        assert row.destination.text() == "/mnt/data", "where the sandbox sees it: offered from the folder's name"
        assert not row.writable.isChecked(), "read-only unless asked"
        remote = dialog.mounts.add_row()
        remote.source.set_value("sshfs://bob@lab1:2222/srv/code")
        remote.destination.setText("/mnt/code")
        remote.writable.setChecked(True)
        workspaces, mounts, servers, restart = dialog.values()
        assert mounts == [("/remote/chosen/data", "/mnt/data", "ro"), ("sshfs://bob@lab1:2222/srv/code", "/mnt/code",
                                                                       "rw")]
        assert backend.resume_args("api", mounts=mounts) == [
            "resume", "--mount=/remote/chosen/data:/mnt/data:ro", "--mount=sshfs://bob@lab1:2222/srv/code:/mnt/code:rw",
            "api"]
        remote.remove_button.click()
        assert [m[0] for m in dialog.values()[1]] == ["/remote/chosen/data"]
        half = dialog.mounts.add_row()
        half.source.set_value("/srv/other")
        assert half.destination.text() == "/mnt/other"
        half.destination.clear()
        with pytest.raises(ValueError, match="where the sandbox sees it"):
            dialog.values()
    finally:
        dialog.deleteLater()


def test_resume_with_takes_a_folder_of_another_machine_as_a_workspace_too():
    assert backend.resume_args("api", workspaces=["sshfs://bob@lab1/srv/code"]) == [
        "resume", "--workspace=sshfs://bob@lab1/srv/code", "api"]
    with pytest.raises(ValueError):
        backend.resume_args("api", mounts=[("/srv/a:b", "/mnt/x", "ro")])
    with pytest.raises(ValueError):
        backend.resume_args("api", mounts=[("relative", "/mnt/x", "ro")])


def test_the_resume_dialogs_words_are_german_too(qapp):
    for text in ("Folder", "In the sandbox at", "The agent may change it", "Add a folder…", "Remove this folder",
                 "Folder {number}: where the sandbox sees it (an absolute path such as /mnt/data)"):
        assert i18n.TEXTS[text]["de"] and i18n.TEXTS[text]["de"] != text, text
