"""The learning video's storyboard (docs/video/storyboard.py): every text in English and German, every command
it shows exists in this sandbox-ai, and the chapters cover the course. Runs no renderer (no Pillow needed)."""
import functools
import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "docs" / "video"


def load(name):
    spec = importlib.util.spec_from_file_location(f"video_{name}", VIDEO / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SB = load("storyboard")


@pytest.fixture(scope="module")
def helper_env(tmp_path_factory):
    home = tmp_path_factory.mktemp("video-home")
    (home / "run").mkdir(mode=0o700)
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "XDG_RUNTIME_DIR": str(home / "run"),
            "LANG": "C.UTF-8", "NO_COLOR": "1", "COLUMNS": "200", "SANDBOX_AI_SECRETS": "file",
            "SANDBOX_AI_NO_DEPRECATION": "1"}


@pytest.fixture(scope="module")
def helptext(helper_env):
    @functools.lru_cache(maxsize=None)
    def run(*words):
        done = subprocess.run([str(ROOT / "sandbox-ai"), *words, "--help"], capture_output=True, text=True,
                              env=helper_env, timeout=60)
        if done.returncode != 0 and len(words) == 1:
            # a command whose --help needs an installation (update): its line in sandbox-ai --help
            lines = [line for line in run().splitlines() if line.strip().startswith(f"sandbox-ai {words[0]}")]
            assert lines, f"sandbox-ai {words[0]} --help: {done.stderr}"
            return "\n".join(lines)
        assert done.returncode == 0, f"sandbox-ai {' '.join(words)} --help: {done.stderr}"
        return done.stdout
    return run


def every_text():
    for chapter, index, scene in SB.iter_scenes():
        for value in SB.texts_of(scene):
            yield SB.scene_id(chapter, index), value
    for chapter in SB.CHAPTERS:
        yield chapter["id"], chapter["title"]
        for topic in chapter["topics"]:
            yield chapter["id"], topic
    for key in ("title", "subtitle", "contents", "chapter", "of", "in_chapter"):
        yield "course", SB.COURSE[key]
    for value in SB.COURSE["badges"].values():
        yield "badges", value
    for value in SB.COURSE["intro_captions"] + SB.COURSE["contents_captions"]:
        yield "intro", value
    outro = SB.COURSE["outro"]
    for value in [outro["heading"], *outro["bullets"], *outro["captions"]]:
        yield "outro", value
    for section, pages in SB.APP_NAV:
        yield "app", section
        for _, title in pages:
            yield "app", title


def test_every_text_has_english_and_german():
    for where, value in every_text():
        if isinstance(value, str):  # the same in every language: a command, a path, an option
            assert value.strip(), where
            continue
        assert set(value) == {"en", "de"}, (where, value)
        assert value["en"].strip() and value["de"].strip(), (where, value)
        assert value["en"].count("`") % 2 == 0 and value["de"].count("`") % 2 == 0, (where, value)


def test_every_scene_has_narration_in_both_languages_and_it_is_translated():
    for chapter, index, scene in SB.iter_scenes():
        where = SB.scene_id(chapter, index)
        assert scene["kind"] in ("bullets", "table", "compare", "terminal", "diagram", "app"), where
        assert scene.get("heading"), where
        assert scene.get("captions"), f"{where}: no narration"
        for caption in scene["captions"]:
            assert caption["en"] != caption["de"], f"{where}: the German narration is not translated: {caption}"
            assert len(caption["en"]) <= 240 and len(caption["de"]) <= 240, f"{where}: a cue too long to read"


def test_scenes_refer_to_what_exists():
    for chapter, index, scene in SB.iter_scenes():
        where = SB.scene_id(chapter, index)
        if scene["kind"] == "diagram":
            diagram = SB.DIAGRAMS[scene["diagram"]]
            ids = {box["id"] for box in diagram["boxes"]}
            for box in diagram["boxes"]:
                assert 0 <= box["x"] and box["x"] + box["w"] <= 100 and 0 <= box["y"] and box["y"] + box["h"] <= 40, \
                    (where, box["id"])
            for arrow in diagram["arrows"]:
                assert arrow["from"] in ids and arrow["to"] in ids, (where, arrow)
        if scene["kind"] == "app":
            assert scene["page"] in SB.APP_PAGES, where
        if scene["kind"] == "table":
            for row in scene["rows"]:
                assert len(row) == len(scene["columns"]), where
            if "widths" in scene:
                assert len(scene["widths"]) == len(scene["columns"]) and abs(sum(scene["widths"]) - 1) < 0.01
        for step in scene.get("steps", []):
            assert not (step.get("live") and step.get("example")), f"{where}: live output is not an example"
            if step.get("live"):
                assert step["cmd"].startswith("sandbox-ai "), f"{where}: only sandbox-ai commands run live"
    for ident in SB.STILLS:
        chapter_id, _, index = ident.partition("/")
        chapter = next(c for c in SB.CHAPTERS if c["id"] == chapter_id)
        assert int(index) < len(chapter["scenes"]), ident


def subcommand(help_text, word):
    """WORD is a subcommand in HELP_TEXT: listed in {a,b,c} or as an indented entry of a command list."""
    return bool(re.search(r"\{[^}]*\b" + re.escape(word) + r"\b[^}]*\}", help_text)
                or re.search(r"^\s{2,}" + re.escape(word) + r"\s{2,}", help_text, re.M)
                or re.search(r"^\s{2,}" + re.escape(word) + r"$", help_text, re.M))


def option_in(option, help_text):
    return re.search(r"(?<![\w-])" + re.escape(option) + r"(?![\w-])", help_text) is not None


def check_command(line, helptext):
    """Every word of LINE that names a sandbox-ai command, subcommand or option exists in its --help."""
    words = shlex.split(line)
    if words[0] == "./sandbox-ai.pyz":
        words[0] = "sandbox-ai"
    if words[0] == "./install.sh":
        source = (ROOT / "install.sh").read_text()
        for word in words[1:]:
            if word.startswith("-"):
                assert word in source, f"{line}: install.sh has no {word}"
        return
    if words[0] != "sandbox-ai":
        return  # curl, cat: not ours
    words = words[1:]
    rest = []
    if "--" in words:
        cut = words.index("--")
        words, rest = words[:cut], words[cut + 1:]
    top = words[0]
    assert option_in(top, helptext()), f"{line}: sandbox-ai --help does not list {top}"
    if top in ("claude", "codex", "opencode"):
        text = helptext("claude")
        for word in words[1:]:
            if word.startswith("-"):
                assert option_in(word.split("=")[0], text), f"{line}: {word} is not an option of sandbox-ai {top}"
        return
    path = [top]
    for word in words[1:]:
        if word.startswith("-"):
            break
        if subcommand(helptext(*path), word):
            path.append(word)
        else:
            break
    text = helptext(*path)
    for word in words[1:]:
        if word.startswith("-"):
            assert option_in(word.split("=")[0], text), f"{line}: {word} is not an option of sandbox-ai {' '.join(path)}"
    if path == ["team", "start"]:
        for word in rest:
            if word.startswith("-"):
                assert option_in(word.split("=")[0], helptext("claude")), f"{line}: {word} is no launcher option"


def test_every_command_shown_exists(helptext):
    found = SB.commands()
    assert len(found) > 80, "the storyboard shows the commands"
    for where, line in found:
        try:
            check_command(line, helptext)
        except AssertionError as error:
            raise AssertionError(f"{where}: {error}") from None


def test_the_checker_finds_a_wrong_command(helptext):
    with pytest.raises(AssertionError):
        check_command("sandbox-ai net allow --allow-everything", helptext)
    with pytest.raises(AssertionError):
        check_command("sandbox-ai claude --no-such-option", helptext)
    with pytest.raises(AssertionError):
        check_command("sandbox-ai teleport now", helptext)


def chapter_text(chapter):
    parts = [chapter["title"]["en"], *(t["en"] for t in chapter["topics"])]
    for scene in chapter["scenes"]:
        parts += [SB.text(v, "en") for v in SB.texts_of(scene)]
        parts += [step["cmd"] for step in scene.get("steps", [])]
    return "\n".join(parts)


COVERAGE = {
    "what-why": ["Podman", "Bubblewrap", "Firejail", "/work", "shared"],
    "install": ["install.sh", ".pyz", "AppImage", "sandbox-ai.app", "macOS", "sandbox-ai doctor"],
    "first-start": ["sandbox-ai claude -w", "tmux", "Ctrl-b d", "attach", "sandbox-ai list", "resume", "stop", "--name",
                    "HOME", "rename"],
    "network": ["--allow-github", "--allow-pypi", "proxy", "sandbox-ai net allow", "--dns", "once", "run", "always",
                "sandbox-ai approvals"],
    "security": ["lax", "paranoid", "taint", "canar", "sandbox-ai privacy-check", "git-logins", "publish-logins",
                 "--mode high"],
    "files": ["workspace", "-m ", "resume shop --restart --workspace", "memory", "85 %", "limits"],
    "hardware": ["--device", "--gpu", "--gui", "--audio virtual"],
    "kits": ["kit", "skill", "plugin", "hook", "ruleset", "--rules"],
    "mcp": ["--mcp", "sandbox-ai mcp list", "sandbox"],
    "llm-vpn": ["--llm", "--vpn", "Tor", "WireGuard"],
    "presets-wizard": ["--preset", "sandbox-ai wizard", "trust", "defaults"],
    "app-web": ["sandbox-ai-app", "board", "Terminal", "approvals", "sandbox-ai web", "sandbox-ai hosts"],
    "teams": ["sandbox-ai team start", "board", "lead"],
    "logs": ["sandbox-ai logs", "sandbox-ai activity", "sandbox-ai doctor", "connections"],
}


def test_the_chapters_cover_the_course():
    assert [c["id"] for c in SB.CHAPTERS] == SB.REQUIRED_CHAPTERS
    assert set(COVERAGE) == set(SB.REQUIRED_CHAPTERS)
    for chapter in SB.CHAPTERS:
        text = chapter_text(chapter)
        for needle in COVERAGE[chapter["id"]]:
            assert needle.lower() in text.lower(), f"chapter {chapter['id']} does not mention {needle!r}"
        assert 3 <= len(chapter["scenes"]) <= 9, chapter["id"]


def test_the_timing_fits_a_course_of_twelve_to_twenty_minutes():
    make = load("make_video")
    scenes = make.build(make._PlanCanvas(), "en", {}, None)
    total = scenes[-1].start + scenes[-1].duration
    assert 12 * 60 <= total <= 20 * 60, total / 60
    german = make.build(make._PlanCanvas(), "de", {}, None)
    assert [round(s.duration, 3) for s in german] == [round(s.duration, 3) for s in scenes], \
        "both languages share one timing, so either subtitle file fits either video"
