"""Hotfix round 1 of 0.1.95 (systematic coverage): every preset x every agent x backend x with/without its optional
values through the real entry point, every command's --help against what it does, every app page in English and
German. One test per bug found, named after it; the e2e ones drive the real ./sandbox-ai as a subprocess
(live_fakes.py)."""
import importlib.util
import json

import pytest

from live_fakes import Host, ROOT


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def preset_folder(host, document):
    folder = host.directory / "team-presets"
    folder.mkdir()
    (folder / f"{document['id']}.json").write_text(json.dumps(document))
    assert host.run("presets", "dirs", "add", str(folder)).returncode == 0
    return folder


TEAM_PRESET = {"format": "sandbox-ai preset", "version": 1, "id": "team-dev", "title": "Team development",
               "description": "The team's checkout.", "category": "development", "agents": ["claude"],
               "options": ["--workspace", "/srv/checkout", "--allow-pypi"]}


def test_a_start_with_a_preset_that_was_skipped_says_why(host):
    """A preset of an extra preset directory that is refused there (here: --workspace, allowed only in your own
    presets; the user's private preset sandbox-ai-dev is one) is skipped. `presets list` said why on standard error,
    but a start with it and `presets show` said only "no preset 'team-dev'; see: sandbox-ai presets list" - as if the
    file were not there, and the list does not show it either."""
    preset_folder(host, TEAM_PRESET)
    for args in (["claude", "--preset", "team-dev", "--dry-run"], ["presets", "show", "team-dev"],
                 ["presets", "check", "team-dev"]):
        result = host.run(*args)
        assert result.returncode != 0, args
        assert "--workspace is not allowed in a preset" in result.stderr, (args, result.stderr)
        assert "team-dev.json" in result.stderr, (args, result.stderr)


def test_a_preset_name_nobody_has_still_says_there_is_none(host):
    """(The other side of the fix above: a name no file has keeps its plain answer.)"""
    preset_folder(host, TEAM_PRESET)
    result = host.run("claude", "--preset", "no-such-preset", "--dry-run")
    assert result.returncode != 0
    assert "no preset 'no-such-preset'" in result.stderr
    assert "team-dev" not in result.stderr


def test_version_help_shows_the_usage_and_an_unknown_option_is_an_error(host):
    """`sandbox-ai version --help` printed the version (exit 0) instead of what `version [--verbose]` does, and
    `sandbox-ai version --verbos` (a typo) printed the short version as if nothing were wrong."""
    result = host.run("version", "--help")
    assert result.returncode == 0
    assert "--verbose" in result.stdout and "usage" in result.stdout.lower(), result.stdout
    typo = host.run("version", "--verbos")
    assert typo.returncode == 2
    assert "--verbos" in typo.stderr
    assert host.run("version").stdout.startswith("sandbox-ai ")
    assert "installed in:" in host.run("version", "--verbose").stdout
    assert host.run("--version").returncode == 0


def test_the_web_ui_offers_only_recipes_sandbox_ai_has():
    """The web UI's list of image recipes still named tor, a recipe removed in 0.1.95 (the Tor container carries
    Tor now): a request for it passed the web UI's check and failed in `images recipe`. Every recipe it accepts must
    be one of images.RECIPES."""
    webui = load("webui_ht1", "sandbox/webui.py")
    images = load("images_ht1", "sandbox/images.py")
    assert set(webui.IMAGE_RECIPES) <= set(images.RECIPES), set(webui.IMAGE_RECIPES) - set(images.RECIPES)
    # The Android ones too (user 2026-11-21: the web interface knows the recipe of android-development); app.js and
    # the desktop app's dialog offer the same ones.
    assert {"media", "android", "android-dev"} <= set(webui.IMAGE_RECIPES)
    page = (ROOT / "sandbox" / "web" / "app.js").read_text()
    for recipe in webui.IMAGE_RECIPES:
        assert f'["{recipe}", N_("{recipe}: ' in page, recipe
