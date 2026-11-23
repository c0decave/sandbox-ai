"""The user 2026-11-22: "the local model ampel sort also in the dropdown menu green first, then orange..." - every
model drop-down (model_profile_view.mark_combo) lists its models by their traffic light: green (verified), yellow
(partly working), red (does not work), grey (not tested), then models without a light; each group in its order.
An entry without a model at the top (the agent's provider, "none") stays first, and the chosen entry stays chosen."""
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import model_profile, model_profile_view as MPV  # noqa: E402

LIGHTS = {"m-red": "red", "m-green": "green", "m-grey": "grey", "m-yellow": "yellow", "m-green2": "green"}


def test_the_drop_down_lists_green_then_yellow_then_red_then_grey(qapp, monkeypatch):
    monkeypatch.setattr(model_profile, "table", lambda sai: {"present": True})
    monkeypatch.setattr(model_profile, "support_of", lambda sai, model: {"light": LIGHTS[model], "harnesses": {}}
                        if model in LIGHTS else None)
    combo = QtWidgets.QComboBox()
    combo.addItem("the agent's provider", None)
    for model in ("m-red", "m-unknown", "m-green", "m-grey", "m-yellow", "m-green2"):
        combo.addItem(model, model)
    combo.setCurrentIndex(combo.findData("m-grey"))
    MPV.mark_combo(combo, sai=None, model_of=lambda index: combo.itemData(index))
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "the agent's provider", "m-green", "m-green2", "m-yellow", "m-red", "m-grey", "m-unknown"]
    assert combo.currentData() == "m-grey", "the chosen one stays chosen"
    assert not combo.itemIcon(1).isNull(), "each keeps its light"
    MPV.mark_combo(combo, sai=None, model_of=lambda index: combo.itemData(index))
    assert [combo.itemText(i) for i in range(combo.count())][1:3] == ["m-green", "m-green2"], "again: the same order"


def test_with_the_agent_known_the_light_is_that_agents(qapp, monkeypatch):
    """The user 2026-11-22 saw "verifiziert" while their start said OpenCode was only partly working with that model:
    a model's light is the best of its harnesses. Where the agent is chosen, the drop-down shows THAT agent's light,
    its tooltip names the agent first and the others after."""
    found = {"light": "green", "model": "m", "status": None, "untested": ["letta"],
             "by_light": {"green": ["claude"], "yellow": ["opencode"], "red": []},
             "harnesses": [{"agent": "claude", "light": "green"}, {"agent": "opencode", "light": "yellow"}]}
    monkeypatch.setattr(model_profile, "table", lambda sai: {"present": True})
    monkeypatch.setattr(model_profile, "support_of", lambda sai, model: dict(found) if model == "m" else None)
    for agent, light, first in (("opencode", "yellow", "OpenCode"), ("claude", "green", "Claude Code"),
                                ("letta", "grey", "Letta Code")):
        combo = QtWidgets.QComboBox()
        combo.addItem("m", "m")
        MPV.mark_combo(combo, sai=None, model_of=lambda index: combo.itemData(index), agent=agent)
        tip = combo.itemData(0, 3)  # Qt.ItemDataRole.ToolTipRole
        assert combo.itemData(0, MPV.LIGHT_ROLE) == light, agent
        assert f"In {first}:" in tip, tip
        assert "Claude Code" in tip and "OpenCode" in tip, "the others still named"
