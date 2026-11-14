"""Image variants per agent (user report from live use, 2026-11-14: a dsh start wanted the variant tor, "why must a dsh
tor variant exist, and it is not in the app's image list"): no preset adds --image-variant (Tor networking needs no
variant: the Tor container beside the agent carries everything; there is no recipe tor any more); the Images page
lists every variant, built or not, with its agent - the same name of several agents as NAME@AGENT - and builds a
variant as one (--variant: `build tor` is the Tor VPN's image, a variant of yours called tor is built as one); a start
of an agent whose variant of a recipe's name is missing makes it from the recipe for that agent and builds
NAME@AGENT."""
import json
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import prerequisites, terminal, widgets  # noqa: E402
from sandbox_ai_app import presets as PR  # noqa: E402
from test_prepare_progress import FakeSai, Page  # noqa: E402
from test_ui import env, window  # noqa: E402,F401 - the isolated home and window of the UI tests

ROOT = Path(__file__).resolve().parents[3]


def test_no_preset_adds_a_fixed_image_variant():
    """A preset may offer a variant to fill in (gpu: optional), never name one: a variant belongs to an agent, and
    Tor networking needs none."""
    for preset in PR.PRESETS:
        options = list(preset.options) + [word for _, server in preset.servers for word in
                                          (server if isinstance(server, (list, tuple)) else [])]
        for index, word in enumerate(options):
            if str(word) in ("--image-variant", "-iv"):
                value = str(options[index + 1])
                assert value.startswith("<") and value.endswith(">"), (preset.id, value)
    for path in sorted((ROOT / "sandbox" / "presets").glob("*.json")):
        document = json.loads(path.read_text())
        if not isinstance(document, dict) or "options" not in document:
            continue
        words = [str(word) for word in document.get("options", [])]
        for index, word in enumerate(words):
            if word.partition("=")[0] in ("--image-variant", "-iv"):
                value = word.partition("=")[2] or words[index + 1]
                assert value.startswith("{"), (path.name, value)


def test_the_images_page_lists_each_agents_variant_and_builds_it_as_one(window):
    page = window.images
    page.show_images([
        {"agent": "claude", "target": "claude", "present": True, "image": "localhost/claude-sandbox:latest"},
        {"agent": "claude", "target": "tor@claude", "variant": True, "present": True, "status": "current",
         "image": "localhost/claude-sandbox-tor:latest", "extended": True},
        {"agent": "dsh", "target": "tor@dsh", "variant": True, "present": False, "status": "not-built",
         "image": "localhost/dsh-sandbox-tor:latest", "extended": True},
        {"agent": "codex", "target": "media", "variant": True, "present": False, "status": "not-built",
         "image": "localhost/codex-sandbox-media:latest", "extended": True}])
    rows = [[page.images.item(r, c).text() for c in (0, 1, 2)] for r in range(page.images.rowCount())]
    assert ["tor@dsh", "dsh", "not built"] in rows and ["tor@claude", "claude", "yes"] in rows
    assert ["media", "codex", "not built"] in rows
    page.package_command.start = lambda argv: True
    page.show_command.start = lambda argv: True
    page.images.selectRow(widgets.find_row(page.images, "tor@dsh"))
    assert page.build_button.isEnabled()
    assert "Build" in [text for text, enabled in widgets.menu_entries(widgets.menu_of(page.images)) if enabled]
    page.build()
    assert window.started[-1][-3:] == ["build", "--variant", "tor@dsh"]
    page.images.selectRow(widgets.find_row(page.images, "claude"))
    page.build()
    assert window.started[-1][-2:] == ["build", "claude"]
    window.sai.host_terminal = lambda host, *args: ["ssh", host, "sandbox-ai", *args]
    assert window.sai.image_build_argv("box", "tor@dsh", variant=True)[-3:] == ["build", "--variant", "tor@dsh"]
    with pytest.raises(ValueError):
        window.sai.image_build_argv("box", "tor@DSH; rm")


class VariantSai(FakeSai):
    def __init__(self, script, listing):
        super().__init__(script, terminal_setting="window")
        self.listing = listing

    def host_image_variants(self, host):
        return []

    def module(self, name):
        return type("Images", (), {"RECIPES": {"media": {}}})

    def host_json(self, host, *args, timeout=None):
        return self.listing

    def host_run(self, host, *args, timeout=None):
        if args[:1] == ("build",):  # (made and built: the listing has it now)
            self.listing = self.listing + [{"agent": "dsh", "target": "media@dsh", "variant": True, "present": True}]
        return super().host_run(host, *args, timeout=timeout)


class DshPage(Page):
    def prerequisite_agent(self):
        return "dsh"

    def prerequisite_argv(self):
        return ["--image-variant", "media"]


@pytest.fixture
def at_once(qapp, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(terminal, "available", lambda web=None: (True, ""))

    def run(function, *args, done, **kw):
        try:
            result = function(*args)
        except Exception as exc:  # noqa: BLE001
            return done(None, exc)
        return done(result, None)
    monkeypatch.setattr(prerequisites, "background", run)


def test_a_start_of_dsh_makes_its_own_media_variant_beside_claudes(at_once, tmp_path):
    script = tmp_path / "sandbox-ai"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    page = DshPage(VariantSai(script, [{"agent": "claude", "target": "media", "variant": True, "present": True}]))
    page.check_prerequisites(then=lambda: None)
    ran = [args for _, args in page.sai.ran]
    assert ("images", "recipe", "media", "--from", "dsh") in ran, ran
    assert ("build", "--variant", "media@dsh") in ran, "claude has a media too: dsh's is named"
    page.deleteLater()


def test_the_agents_own_built_variant_is_enough(at_once, tmp_path):
    script = tmp_path / "sandbox-ai"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    page = DshPage(VariantSai(script, [{"agent": "claude", "target": "media@claude", "variant": True, "present": True},
                                       {"agent": "dsh", "target": "media@dsh", "variant": True, "present": True}]))
    started = []
    assert page.check_prerequisites(then=lambda: started.append(True)) is True
    assert started == [True] and page.sai.ran == []
    page.deleteLater()
