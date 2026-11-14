"""0.1.95, from what the user hit: a Qwen start from the app started its MCP servers, then failed with "Podman failed
(exit status 125) ...: localhost/qwen-sandbox:latest: image not known". The launcher asks for the images before
anything is made; the Board's check shows "The image of qwen ... is not built on this host" on the agent's part with
Build now, which runs `sandbox-ai build qwen` in a terminal tab with its progress and checks the board again."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, board as B, widgets  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
QWEN = ("agent-sandbox: The image of qwen (localhost/qwen-sandbox:latest) is not built on this host (nothing is "
        "pulled at a start): sandbox-ai build qwen")
SEVERAL = ("agent-sandbox: 3 problems with this start (nothing was started or made; fix them, then start again):\n"
           "  - MCP server whatweb-tor needs the variable A: set it\n"
           "  - The image of qwen (localhost/qwen-sandbox:latest) is not built on this host (nothing is pulled at a "
           "start): sandbox-ai build qwen\n"
           "  - The image of the browser (localhost/sandbox-ai-browser:latest) is not built on this host (nothing is "
           "pulled at a start): sandbox-ai build browser")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime"),
                      ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    return tmp_path


@pytest.fixture
def page(qapp, homes):
    from sandbox_ai_app.board_view import BoardPage
    view = BoardPage(backend.SandboxAI(), lambda *a, **kw: None, lambda: {"board_grid": 20})
    yield view
    view.deleteLater()


def test_build_offers():
    assert B.build_offers(QWEN) == [["build", "qwen"]]
    assert B.build_offers(SEVERAL) == [["build", "qwen"], ["build", "browser"]]
    assert B.build_offers(QWEN + "\n" + QWEN) == [["build", "qwen"]], "each once"
    variant = "The image of claude (x) is not built on this host (nothing is pulled at a start): sandbox-ai build " \
              "--variant web"
    assert B.build_offers(variant) == [["build", "--variant", "web"]]
    # Only sandbox-ai's own builds, word for word: nothing else of a message is ever run.
    assert B.build_offers(QWEN.replace("build qwen", "build qwen;rm -rf ~")) == []
    assert B.build_offers("is not built on this host (nothing is pulled at a start): podman pull x") == []
    assert B.build_offers(None) == []


def test_the_image_problem_is_on_the_agents_part(page):
    agent = page.board.first("agent")
    found = B.launcher_problems(page.board, QWEN)
    assert [(p.level, p.node) for p in found] == [("error", agent.id)]
    assert found[0].text.startswith("The image of qwen")
    several = B.launcher_problems(page.board, SEVERAL)
    assert len(several) == 3 and several[1].node == agent.id


def test_the_checks_build_now_builds_in_a_tab_then_checks_again(page, monkeypatch):
    page.checking_state = page.board.state()
    page.check_token += 1
    page.checked(page.check_token, (None, SEVERAL), None, "local")
    assert not page.build_button.isHidden() and page.build_commands == [["build", "qwen"], ["build", "browser"]]
    assert any("The image of qwen" in page.findings.item(i).text() for i in range(page.findings.count()))
    tabs, checks = [], []
    monkeypatch.setattr(page, "prerequisite_terminal",
                        lambda: lambda argv, title, ended: tabs.append((argv, title, ended)) or True)
    monkeypatch.setattr(page, "check", lambda: checks.append(True))
    page.build_button.click()
    (argv, title, ended), = tabs
    assert title == "Building the image" and not page.build_button.isEnabled()
    steps = json.loads(argv[-1])
    assert [step[0] for step in steps] == ["build qwen", "build browser"]
    assert steps[0][1][-2:] == ["build", "qwen"], "sandbox-ai build qwen, on the board's host"
    ended(0)
    assert checks == [True] and page.build_button.isEnabled(), "checked again at its end"
    # The launcher accepts the board now: no Build now any more.
    page.checked(page.check_token, ({"agent": "qwen"}, ""), None, "local")
    assert page.build_button.isHidden() and page.build_commands == []
