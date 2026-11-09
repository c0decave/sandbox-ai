"""The setting show_flags (Settings > Appearance, off by default): off, the options' fields show their names alone
(the Start page's form, the board's inspector, the wizard), no command-line option (--…) on them or in their tooltips;
on, the option beside its name and in its tooltip, as before. It applies at once."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, i18n, labels  # noqa: E402

from test_ui import env, window  # noqa: E402,F401 - the isolated home and the window of the UI tests

needs_sai = pytest.mark.skipif(not backend.os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                               reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
KINDS = (QtWidgets.QLabel, QtWidgets.QCheckBox, QtWidgets.QRadioButton, QtWidgets.QPushButton)


def flags_seen(root):
    """The visible names below ROOT (check boxes, buttons, one-line labels: not the explanations) that show a
    command-line option: starting with "--", or with one in parentheses."""
    found = []
    for kind in KINDS:
        for widget in root.findChildren(kind):
            text = widget.text().strip()
            if isinstance(widget, QtWidgets.QLabel) and widget.wordWrap():
                continue
            if widget.isVisibleTo(root) and (text.startswith("--") or "(--" in text or ": --" in text):
                found.append(text)
    return found


def test_the_names_and_their_fallback():
    assert labels.readable("--allow-host") == "Allow host" and labels.readable("--no-tmux") == "No tmux"
    assert labels.label("--some-new-option") == "Some new option", "an option not listed yet: no bare flag"
    labels.set_flags_shown(False)
    assert labels.shown("Kits (--kit)") == "Kits"
    assert labels.shown("on (every connection passes it; off: --no-proxy)") == "on (every connection passes it)"
    assert labels.shown("nur dieser Server (keine Cloud: --no-provider-defaults)") == "nur dieser Server (keine Cloud)"
    assert labels.tip("--offline", "No network.") == "No network."
    labels.set_flags_shown(True)
    try:
        assert labels.shown("Kits (--kit)") == "Kits (--kit)"
        assert labels.tip("--offline", "No network.") == "--offline\nNo network."
    finally:
        labels.set_flags_shown(False)


@needs_sai
@pytest.mark.parametrize("language", ["en", "de"])
def test_off_by_default_on_shows_them_and_it_applies_at_once(window, qapp, language):  # noqa: F811
    i18n.set_language(language)
    try:
        assert window.sai.settings().get("show_flags", backend.SETTINGS["show_flags"]) is False
        from sandbox_ai_app.ui import OptionsForm
        form = OptionsForm(window.sai.options())  # made in the language chosen (the window's was made in English)
        board = window.board
        network = board.board.first("network")

        def look():
            board.inspected = network.id  # (selected: the inspector made again shows it)
            board.inspect(network)
            qapp.processEvents()
            seen = []
            for index in range(form.count()):
                form.setCurrentIndex(index)
                seen += flags_seen(form.widget(index).widget())
            return seen, flags_seen(board.inspector_holder)

        start, inspector = look()
        assert start == [] and inspector == [], "off: the fields' names alone"
        names = [w.text() for w in board.inspector_holder.findChildren(QtWidgets.QLabel)]
        assert labels.label("--allow-host") in names, (network, board.inspector.rowCount(), names)
        assert "--allow-host" not in form.widgets["--allow-host"].toolTip()
        from sandbox_ai_app.settings import SettingsDialog
        dialog = SettingsDialog(window.sai, window)
        dialog.changed.connect(window.apply_setting)
        dialog.fields["show_flags"].setChecked(True)  # on, live
        qapp.processEvents()
        assert window.sai.settings()["show_flags"] is True and labels.flags_shown()
        form.show_flags()  # (this form is not the window's: the window's own follows by itself, below)
        start, inspector = look()
        assert not form.labels["--allow-host"].flag.isHidden() and any("--allow-host" in t for t in start)
        own = window.launch.form  # the Start page's own form follows the setting at once
        assert not own.labels["--allow-host"].flag.isHidden()
        assert "--allow-host" in own.widgets["--allow-host"].toolTip()
        assert any(text.startswith("--allow-host") for text in inspector), inspector
        assert "--allow-host" in form.widgets["--allow-host"].toolTip()
        dialog.fields["show_flags"].setChecked(False)  # off again, live
        qapp.processEvents()
        form.show_flags()
        start, inspector = look()
        assert start == [] and inspector == []
        assert own.labels["--allow-host"].flag.isHidden()
        dialog.close()
        form.deleteLater()
    finally:
        labels.set_flags_shown(False)
        i18n.set_language("en")


@needs_sai
def test_the_wizard_names_its_fields_without_options(window, qapp):  # noqa: F811
    from sandbox_ai_app import wizard
    flow = wizard.SandboxWizard(window.sai, lambda: {}, window)
    flow.show()
    seen = []
    for page in flow.pageIds():
        flow.setStartId(page)
        flow.restart()
        qapp.processEvents()
        seen += flags_seen(flow.currentPage())
    flow.close()
    assert seen == []
