"""ARTEX on the Board (--artex): a part of its own beside the agent, its fields (its data, its console's port), its
badge, the guide's questions, the help in both languages and the preset's card in the gallery.

The user: "check if we can integrate artex ... but check for backdoors of it. if none we add it." and "they claim they
have removed the backdoors spreaded with other versions, we still have to be careful"."""
import os

import pytest

from sandbox_ai_app import backend, board as B, guide, help_content, help_texts, labels
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


def test_artex_is_a_part_of_its_own_beside_the_agent(sai):
    kind = B.KINDS["artex"]
    assert kind.single and not kind.required and kind.column == B.KINDS["cairn"].column
    assert set(kind.options) == {"--artex", "--artex-data", "--artex-port"}
    assert ("agent", "artex") in B.EDGES and ("artex", "proxy") in B.EDGES, "beside the agent, behind its proxy"
    assert {o.long for o in sai.options()} >= set(kind.options), "the launcher's own options"
    assert backend.GROUPS["ARTEX"] == ["--artex", "--artex-data", "--artex-port"]
    board = B.from_argv(sai, "claude", ["--artex", "--artex-data", "session", "--artex-port", "9000",
                                        "--allow-tcp", "10.10.10.5:80"])
    node = board.first("artex")
    assert node is not None and node.props["--artex"] is True
    assert node.props["--artex-data"] == "session" and node.props["--artex-port"] == "9000"
    argv = B.to_argv(sai, board)
    assert "--artex" in argv and argv[argv.index("--artex-port") + 1] == "9000"
    assert B.to_argv(sai, B.from_argv(sai, "claude", argv)) == argv, "a second trip changes nothing"


def test_its_badge_says_its_agents_act_on_their_own_within_the_allow_list(sai):
    board = B.from_argv(sai, "claude", ["--artex"])
    score, found = B.badges(board)
    found = [b for b in found if b.node == board.first("artex").id]
    assert found and found[0].level == "warn" and "without asking" in found[0].text
    assert "allow-list" in found[0].text
    assert score == B.badges(B.from_argv(sai, "claude", []))[0] - found[0].cost


def test_the_guide_asks_for_its_data_and_its_port_in_both_languages():
    asked = {q.key: q for q in guide.questions("artex")}
    assert list(asked) == ["--artex", "--artex-data", "--artex-port"]
    assert asked["--artex"].needed and "sandbox-ai artex console" in asked["--artex"].help
    assert [v for v, _ in asked["--artex-data"].choices] == ["", "session"]
    set_language("de")
    assert tr(asked["--artex-data"].label) == "ARTEX' Daten" and tr(asked["--artex-port"].label) == "Konsolen-Port"
    assert tr(B.KINDS["artex"].help).startswith("ARTEX, eine KI-Plattform")
    assert "Erlaubnisliste" in tr(asked["--artex"].help) or "Konsole" in tr(asked["--artex"].help)


def test_its_options_have_labels_in_both_languages():
    for option in B.KINDS["artex"].options:
        en, de = labels.OPTION_LABELS[option]
        assert en and de and en != de, option


def test_the_help_says_how_the_part_works_in_both_languages():
    section = help_content.BY_ID["artex"]
    for text in (section.body.en, section.body.de):
        for said in ("--artex", "--artex-data", "--artex-port", "artex-pentest", "127.0.0.1", "AGPL", "paranoid",
                     "sandbox-ai build artex", "sandbox-ai artex console", "--llm", "docs/ARTEX.md"):
            assert said in text, said
    assert "sandbox-ai artex console [RUN]" in section.cli and "cairn" in section.see
    assert help_texts.KIND_SECTIONS["artex"] == "artex"


def test_the_preset_shows_the_part_in_the_gallery(sai):
    preset = next(p for p in PR.sandbox_ai_presets(sai)[0] if p.id == "sai-artex-pentest")
    board = PR.build(sai, preset)
    assert board.first("artex") is not None and board.first("artex").props["--artex"] is True
    assert preset.group == "security" and "ARTEX" in str(preset.title)
    assert not hasattr(preset, "experimental"), "no badge"
