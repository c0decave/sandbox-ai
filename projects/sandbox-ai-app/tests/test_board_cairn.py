"""Cairn on the Board (--cairn): a part of its own beside the agent, its fields (workers and their models, the
project's origin, goal and first hint, its board's database), its badge, the guide's questions, the help in both
languages and the presets' cards in the gallery.

The user: "Cairn als eigener Teil im Board, wie ein Service-Container neben dem Agent; sein Board öffnet sich
automatisch im Browser-Tab, die Agents der Session sind seine Worker." - and "Cairn natürlich auch mit anderen
Modellen / lokalem Preset supporten." """
import os

import pytest

from sandbox_ai_app import backend, board as B, guide, help_content, labels
from sandbox_ai_app import presets as PR
from sandbox_ai_app.i18n import set_language, tr

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture(scope="module")
def sai():
    return backend.SandboxAI()


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language("en")


def test_cairn_is_a_part_of_its_own_beside_the_agent(sai):
    """"Cairn als eigener Teil im Board, wie ein Service-Container neben dem Agent"."""
    kind = B.KINDS["cairn"]
    assert kind.single and not kind.required and kind.column == B.KINDS["service"].column
    assert set(kind.options) == {"--cairn", "--cairn-workers", "--cairn-origin", "--cairn-goal", "--cairn-hint",
                                 "--cairn-board"}
    assert ("agent", "cairn") in B.EDGES and ("cairn", "proxy") in B.EDGES, "beside the agent, behind its proxy"
    assert {o.long for o in sai.options()} >= set(kind.options), "the launcher's own options"
    board = B.from_argv(sai, "claude", ["--with", "codex", "--cairn", "--cairn-workers", "claude=opus,codex",
                                        "--cairn-origin", "The machine 10.0.0.5", "--cairn-goal", "Its flaws",
                                        "--cairn-hint", "Port 80 first", "--cairn-board", "session"])
    node = board.first("cairn")
    assert node is not None and node.props["--cairn"] is True
    assert node.props["--cairn-workers"] == "claude=opus,codex" and node.props["--cairn-board"] == "session"
    argv = B.to_argv(sai, board)
    assert argv[argv.index("--cairn-workers") + 1] == "claude=opus,codex" and "--cairn" in argv
    assert B.to_argv(sai, B.from_argv(sai, "claude", argv)) == argv, "a second trip changes nothing"


def test_its_badge_says_that_it_starts_the_agents_on_its_own(sai):
    board = B.from_argv(sai, "claude", ["--cairn"])
    score, found = B.badges(board)
    found = [b for b in found if b.node == board.first("cairn").id]
    assert found and found[0].level == "warn" and "without their approval prompts" in found[0].text
    assert score == B.badges(B.from_argv(sai, "claude", []))[0] - found[0].cost
    assert not any("Cairn" in b.text for b in B.badges(B.from_argv(sai, "claude", []))[1])


def test_the_guide_asks_for_the_workers_the_models_and_the_project():
    asked = {q.key: q for q in guide.questions("cairn")}
    assert list(asked) == ["--cairn", "--cairn-workers", "--cairn-origin", "--cairn-goal", "--cairn-hint",
                           "--cairn-board"]
    assert asked["--cairn"].needed and "model" in asked["--cairn-workers"].help
    assert [v for v, _ in asked["--cairn-board"].choices] == ["", "session"]
    set_language("de")
    assert tr(asked["--cairn-workers"].label) == "Worker" and tr(asked["--cairn-goal"].label) == "Ziel"
    assert tr(B.KINDS["cairn"].help).startswith("Cairns Brett")


def test_its_options_have_labels_in_both_languages():
    for option in B.KINDS["cairn"].options:
        en, de = labels.OPTION_LABELS[option]
        assert en and de and en != de, option


def test_the_help_says_how_the_part_works_in_both_languages():
    section = help_content.BY_ID["cairn"]
    for text in (section.body.en, section.body.de):
        for said in ("--cairn", "--cairn-workers", "cairn-local-llm", "cairn-pentest-board", "127.0.0.1:8000",
                     "Relay" if text is section.body.de else "relay", "AGPL", "paranoid", "sandbox-ai build cairn"):
            assert said in text, said
    assert "--cairn-board fresh|session" in section.cli and "browser" in section.see


@pytest.mark.parametrize("ident", ["cairn-pentest-board", "cairn-local-llm"])
def test_the_presets_show_the_part_in_the_gallery(sai, ident):
    """"Cairn natürlich auch mit anderen Modellen / lokalem Preset supporten": both presets' cards show Cairn."""
    preset = next(p for p in PR.sandbox_ai_presets(sai)[0] if p.id == f"sai-{ident}")
    board = PR.build(sai, preset)
    assert board.first("cairn") is not None and board.first("cairn").props["--cairn"] is True
    assert board.first("agent").props["agent"] == "claude"
    if ident == "cairn-local-llm":
        assert board.first("llm") is not None and board.first("cairn").props["--cairn-workers"] == "claude"
        assert preset.group == "llm"
    else:
        assert preset.group == "security" and board.first("cairn").props["--cairn-goal"] == "<text:goal>"
    assert not hasattr(preset, "experimental"), "no badge (the user, 2026-11-22)"
