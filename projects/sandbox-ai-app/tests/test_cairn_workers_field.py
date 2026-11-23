"""The Board's field of Cairn's workers (--cairn-workers): every harness of sandbox-ai's in its list, each with its
traffic light as the model lists show theirs (an icon of the light's colour and shape, the words and the evidence in
the tooltip and the accessible text, both languages); picking one adds it to the value, which reaches the command
line; nothing says "not live-tested".

The user (2026-11-22): "why workers only for claude and not all harnesses?" and "shouldnt all harnesses generally be
support? and if not the harnesses supported shown by ampel or desc?" """
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sandbox_ai_app import backend, widgets  # noqa: E402
from sandbox_ai_app import cairn_workers_view as CW  # noqa: E402
from sandbox_ai_app.i18n import set_language  # noqa: E402
from sandbox_ai_app.model_profile_view import LIGHT_ROLE  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def sai():
    return backend.SandboxAI()


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language("en")


def entries(field):
    combo = field.add
    return {combo.itemData(i, CW.AGENT_ROLE): i for i in range(1, combo.count())}


def test_every_harness_is_listed_with_its_light(qapp, sai):
    field = CW.CairnWorkersField(sai)
    table = sai.module("cairn_workers")
    listed = entries(field)
    assert list(listed) == list(table.relay().HARNESSES), "every harness of sandbox-ai's, in its order"
    for agent, index in listed.items():
        row = table.row(agent)
        assert field.add.itemData(index, LIGHT_ROLE) == row["light"], agent
        assert not field.add.itemIcon(index).isNull(), "an icon of its light"
        tip = field.add.itemData(index, Qt.ItemDataRole.ToolTipRole)
        assert f"as Cairn's worker: {row['words']}" in tip and "A task runs:" in tip, tip
        assert field.add.itemData(index, Qt.ItemDataRole.AccessibleTextRole).endswith(row["words"])
    claude = field.add.itemData(listed["claude"], Qt.ItemDataRole.ToolTipRole)
    assert "agents-live (cairn)" in claude, "the evidence it comes from"
    goose = field.add.itemData(listed["goose"], Qt.ItemDataRole.ToolTipRole)
    assert "Evidence: no data yet" in goose and "conclusion" in goose, "grey: no data yet; its limits said"


def test_in_german(qapp, sai):
    set_language("de")
    field = CW.CairnWorkersField(sai)
    tip = field.add.itemData(entries(field)["goose"], Qt.ItemDataRole.ToolTipRole)
    assert "Goose als Worker von Cairn: noch keine Daten" in tip and "Beleg: noch keine Daten" in tip
    assert field.add.itemText(0) == "Worker hinzufügen…"


def test_picking_a_harness_adds_it_once(qapp, sai):
    field = CW.CairnWorkersField(sai, "claude=opus")
    seen = []
    field.changed.connect(seen.append)
    field.picked(entries(field)["pi"])
    assert field.text() == "claude=opus,pi" and seen == ["claude=opus,pi"]
    field.picked(entries(field)["pi"])
    assert field.text() == "claude=opus,pi" and len(seen) == 1, "once"
    assert field.add.currentIndex() == 0
    assert "Pi as Cairn's worker" in field.edit.toolTip() and "Claude Code as Cairn's worker" in field.edit.toolTip()


def test_no_word_of_live_tested(qapp, sai):
    for language in ("en", "de"):
        set_language(language)
        field = CW.CairnWorkersField(sai)
        texts = [str(field.add.itemData(i, Qt.ItemDataRole.ToolTipRole) or "") for i in range(field.add.count())]
        assert not any("live-tested" in text or "live getestet" in text for text in texts)


def test_an_older_sandbox_ai_shows_the_plain_field(qapp):
    class Old:
        def module(self, name):
            raise FileNotFoundError(name)
    field = CW.CairnWorkersField(Old(), "claude")
    assert field.text() == "claude" and field.add.isHidden() and field.add.count() == 1


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **k: None, lambda: {"board_grid": 20})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    yield view
    view.check_timer.stop()
    view.close()
    widgets.ThreadCall.wait_all(30)
    view.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def field_of(page, option):
    import time
    end = time.monotonic() + 15
    while time.monotonic() < end:
        QApplication.processEvents()
        widget = page.option_fields.get(option)
        if widget is not None and not page.inspect_timer.isActive():
            return widget
        time.sleep(0.02)
    return page.option_fields[option]


def test_the_board_s_cairn_part_has_the_field_and_a_pick_reaches_the_command_line(page):
    page.add_node("cairn")
    node = page.board.first("cairn")
    page.scene.clearSelection()
    page.items[node.id].setSelected(True)
    page.show_inspector()
    field = field_of(page, "--cairn-workers")
    assert isinstance(field, CW.CairnWorkersField)
    field.picked(entries(field)["claude"])
    assert page.board.first("cairn").props["--cairn-workers"] == "claude"
    assert "--cairn-workers claude" in page.preview.toPlainText()
