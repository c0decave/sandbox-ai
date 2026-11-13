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


# -- the series: one video per page of the desktop app, spoken in German --------------------------------------

NARRATE = load("narrate")
CAPTURE = load("capture_app")
PRON = NARRATE.pronunciation


def series_texts():
    series = SB.SERIES["pages"]
    for key in ("title", "part", "next", "last"):
        yield "series", series[key]
    for value in series["outro_bullets"] + series["outro_captions"]:
        yield "series", value
    for video in SB.series_videos():
        for value in video["topics"] + video["intro"]:
            yield video["id"], value
        for index, scene in enumerate(video["scenes"]):
            for value in SB.texts_of(scene):
                yield f"{video['id']}/{index}", value


def english_texts():
    return [SB.text(v, "en") for _, v in every_text()] + [SB.text(v, "en") for _, v in series_texts()]


def app_sections():
    """The page keys of the app's navigation, read from ui.py's SECTIONS (no Qt needed)."""
    source = (ROOT / "projects" / "sandbox-ai-app" / "sandbox_ai_app" / "ui.py").read_text()
    block = source[source.index("SECTIONS = ("):source.index("PAGES = tuple(")]
    return re.findall(r'\("([a-z]+)", N_\(', block)


def test_every_page_of_the_app_has_a_video_of_its_own():
    pages = app_sections()
    assert pages and list(SB.APP_PAGES) == pages, "APP_NAV follows the app's navigation (ui.py SECTIONS)"
    assert CAPTURE.PAGES == pages, "capture_app.py takes a picture of every page"
    videos = SB.series_videos()
    assert [v["page"] for v in videos] == pages, "one video per page, in the app's order"
    assert len({v["id"] for v in videos}) == len(videos)
    for video in videos:
        assert len(video["topics"]) == 3 and video["intro"] and 2 <= len(video["scenes"]) <= 6, video["id"]
        assert any(s["kind"] == "app" and s["page"] == video["page"] for s in video["scenes"]), \
            f"{video['id']}: the video shows its page"


def test_the_series_has_every_text_in_english_and_german():
    for where, value in series_texts():
        if isinstance(value, str):
            assert value.strip(), where
            continue
        assert set(value) == {"en", "de"}, (where, value)
        assert value["en"].strip() and value["de"].strip(), (where, value)
        assert value["en"].count("`") % 2 == 0 and value["de"].count("`") % 2 == 0, (where, value)
    for where, text in SB.narration("de"):
        assert len(text) <= 240, f"{where}: a cue too long to read"
    english, german = SB.narration("en"), SB.narration("de")
    assert len(english) == len(german)
    for (where, en), (_, de) in zip(english, german):
        assert en != de, f"{where}: the German narration is not translated"


def test_series_scenes_refer_to_what_exists():
    for video, index, scene in SB.iter_series_scenes():
        where = f"{video['id']}/{index}"
        assert scene["kind"] in ("bullets", "table", "compare", "terminal", "diagram", "app"), where
        assert scene.get("heading") and scene.get("captions"), where
        if scene["kind"] == "diagram":
            assert scene["diagram"] in SB.DIAGRAMS, where
        if scene["kind"] == "app":
            assert scene["page"] in SB.APP_PAGES, where
            if "shot" in scene:
                assert CAPTURE.STATES.get(scene["shot"]) == scene["page"], f"{where}: capture_app.py takes no {scene['shot']}"
            for key in ("zoom", "focus"):
                if key in scene:
                    x0, y0, x1, y1 = scene[key]
                    assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1, (where, key)
            assert not ("zoom" in scene and "focus" in scene), where
        assert not any(step.get("live") for step in scene.get("steps", [])), f"{where}: no live commands in a series"
    for state, page in CAPTURE.STATES.items():
        assert page in SB.APP_PAGES and state.startswith(page + "-"), state


def test_the_board_is_called_board_in_german_too():
    source = (VIDEO / "storyboard.py").read_text()
    assert "Tafel" not in source, "the German app says Board, not Tafel"
    assert SB.APP_PAGES["board"]["de"] == "Board"


def test_the_spoken_narration_holds_nothing_unspeakable():
    for where, text in SB.narration("de"):
        for mark in ("`", "/", "--", "~", "_", "(", "[", "{", "<", "=", "$"):
            assert mark not in text, f"{where}: {mark!r} in the spoken narration: {text}"


def test_every_english_it_term_of_the_german_narration_is_said_in_english():
    problems = NARRATE.coverage_problems(SB.narration("de"), NARRATE.english_words(english_texts()))
    assert not problems, "\n".join(f"{where}: {word}: {why}" for where, word, why in problems)


def test_the_coverage_check_finds_a_missing_term():
    english = NARRATE.english_words(["the sandbox and the gateway", "SSO"])
    problems = NARRATE.coverage_problems([("x", "Der Gateway der Sandbox, mit SSO und Sandboxwelt.")], english)
    assert {word for _, word, _ in problems} == {"Gateway", "SSO", "Sandboxwelt"}


def test_the_lexicon_is_well_formed():
    assert PRON.LEXICON and not set(PRON.LEXICON) & PRON.GERMAN
    for term, value in PRON.LEXICON.items():
        assert term == term.strip() and value == value.strip() and value, term
        assert "/" not in value or (value.startswith("/") and value.endswith("/") and value.count("/") == 2), \
            f"{term}: either an English spelling or /IPA/"
    for word in ("Sandbox", "Sandboxen", "Workspace", "Proxy", "Session", "Board", "Preset", "Plugin", "Hook",
                 "Container", "Token", "Prompt", "Approval", "Kill-Switch", "Tor", "VPN", "MCP", "Podman",
                 "Firejail", "Bubblewrap", "Secrets", "Host", "Image", "Update", "Server"):
        assert word in PRON.LEXICON, f"{word} is said in English"


def fake_native(text):
    """espeak-ng's answer, made up: each clause upper-cased, with its terminator."""
    found = []
    for clause, mark in re.findall(r"([^,.;:?!]*)([,.;:?!]?)", text):
        if clause.strip() or mark:
            found.append((clause.strip().upper(), mark, mark in ".?!" and bool(mark)))
    return found


def fake_english(text):
    return "<" + text.replace(" ", "_") + ">"


def test_english_terms_are_spliced_into_the_german_phonemes():
    def say(text):
        return NARRATE.phoneme_sentences(NARRATE.spoken(text), fake_native, fake_english)

    assert say("Die Sandbox startet, und der Proxy prüft. Fertig!") == \
        ["DIE <sandbox> STARTET, UND DER <proxy> PRÜFT.", "FERTIG!"]
    assert say("Der MCP-Server und das Tor-Profil.") == ["DER <M_C_P> <server> UND DAS <tor> PROFIL."]
    assert say("Kits & Plugins: der Kill-Switch, sofort.") == ["<kits> UND <plugins>: DER <kill_switch>, SOFORT."]
    assert say("Ein Workspace") == ["EIN <workspace>"]
    assert say("Mit Podman.") == ["MIT pˈɒdmən."]  # the lexicon's own phonemes go in as they are
    assert say("Die Sandbox.") == say("Die Sandbox.")  # the same text, the same phonemes: the audio cache relies on it


def test_scenes_last_as_long_as_their_speech(tmp_path):
    import wave
    make = load("make_video")
    voice = tmp_path / "cue.wav"
    with wave.open(str(voice), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(1000)
        f.writeframes(b"\x01\x00" * 1000)

    class Speaker:
        def say(self, text):
            return voice, len(text) / 14.0

    video = SB.series_videos()[0]
    scenes = make.build_series(make._PlanCanvas(), "de", None, video, speaker=Speaker())
    for scene in scenes:
        spoken = [len(SB.text(c, "de")) / 14.0 for c in scene.captions]
        assert len(scene.cues) == len(spoken) == len(scene.sounds)
        for (a, b, _), (at, _), seconds in zip(scene.cues, scene.sounds, spoken):
            assert at == a and b - a >= seconds, scene.ident  # a cue is shown while it is said
        speech = make.VOICE_LEAD + sum(s + make.VOICE_PAUSE for s in spoken) + make.VOICE_TAIL
        assert abs(scene.duration - max(scene.content_seconds, speech)) < 1e-6, scene.ident
    for one, two in zip(scenes, scenes[1:]):
        assert abs(one.start + one.duration - two.start) < 1e-6
    track = make.soundtrack(scenes, tmp_path / "all.wav")
    with wave.open(str(track), "rb") as f:
        total = scenes[-1].start + scenes[-1].duration
        assert abs(f.getnframes() / f.getframerate() - total) < 0.01
        data = f.readframes(f.getnframes())
    first = scenes[0].start + scenes[0].sounds[0][0]
    offset = int(first * 1000) * 2
    assert data[offset - 2:offset] == b"\x00\x00" and data[offset:offset + 2] == b"\x01\x00"


def test_series_videos_last_one_to_three_minutes():
    make = load("make_video")
    for video in SB.series_videos():
        for lang in SB.LANGUAGES:
            scenes = make.build_series(make._PlanCanvas(), lang, None, video)
            total = scenes[-1].start + scenes[-1].duration
            assert 55 <= total <= 180, (video["id"], lang, total)
        assert make.series_file(video, "de").startswith("sandbox-ai-app-")
