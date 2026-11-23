"""Helpful, not blocking - round 2 in the app (docs/HELPFUL_FIXES.md): the prerequisites' one question also holds the
agent's own image of a plain start (looked at without a launcher run) and a Tor profile for a VPN profile that is not
there; what is chosen is made, and the start goes on. The Board on this machine, its host's sandbox-ai answered by a
stand-in (sai.host_run); the question answered by widgets' test hooks."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, prerequisites, widgets  # noqa: E402
from test_remote_mcp_folder import at_once  # noqa: E402
from test_remote_paths import sai  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")

TITLE = "What the start lacks"


@pytest.fixture
def view(qapp, sai, monkeypatch):
    """The Board of this machine; sandbox-ai's answers recorded (view.ran): its plan empty, everything it is asked
    to make made; nothing really starts (view.started)."""
    from sandbox_ai_app import board as B
    from sandbox_ai_app.board_view import BoardPage
    page = BoardPage(sai, lambda *a, **k: None, lambda: {"board_grid": 20})
    page.ran, page.started, page.profiles = [], [], []

    def run(name, *args, **kw):
        page.ran.append((name, *args))
        if args and args[-1] == "--fix-plan":
            return 0, json.dumps({"fixes": [], "questions": "all", "mode": "medium"}), ""
        if args[:2] == ("vpn", "add"):
            page.profiles.append(args[2])
        return 0, "done", ""
    monkeypatch.setattr(sai, "host_run", run)
    monkeypatch.setattr(sai, "host_vpn_profiles", lambda host: [{"profile": name} for name in page.profiles])
    monkeypatch.setattr(sai, "fix_questions", lambda: "all")
    monkeypatch.setattr(prerequisites, "background", at_once)
    monkeypatch.setattr(page, "prerequisite_terminal", lambda: None)  # (made here: no terminal tab)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    widgets.SHOWN.clear()

    def shown(options):
        page.set_board(B.from_argv(sai, "claude", list(options), []), None)
        return page
    page.shown = shown
    yield page
    page.close()
    page.deleteLater()


def asked():
    return [text for title, text, _ in widgets.SHOWN if title == TITLE]


def test_a_plain_starts_image_not_built_is_in_the_question_and_built_first(view, monkeypatch):
    """Round 1 left it to the start's terminal tab; now this machine's Podman is asked (no launcher run) and the
    image is a line of the one question, built before the start goes on."""
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", raising=False)
    monkeypatch.setattr(prerequisites.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    built = {"claude": False}
    monkeypatch.setattr(view, "agent_images_built", lambda host, agents: {
        a: built[a] or ("local", "build", a) in view.ran for a in agents})
    view.shown([])
    view.check_prerequisites(then=lambda: view.started.append(1), why="start")
    (text,) = asked()
    assert "the image of claude is not built on this machine → built (about 5 minutes" in text
    assert ("local", "build", "claude") in view.ran and view.started == [1], widgets.SHOWN
    widgets.SHOWN.clear()
    built["claude"] = True
    view.started.clear()
    view.ran.clear()
    assert view.check_prerequisites(then=lambda: view.started.append(1), why="start") is False  # (at once)
    assert asked() == [] and view.ran == []  # (built: no question, nothing read)


def test_declined_the_image_is_not_built_and_the_start_waits_with_a_button(view, monkeypatch):
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", raising=False)
    monkeypatch.setattr(prerequisites.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    monkeypatch.setattr(view, "agent_images_built", lambda host, agents: {a: False for a in agents})
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)  # (Cancel)
    view.shown([])
    view.check_prerequisites(then=lambda: view.started.append(1), why="start")
    assert view.started == [] and not [args for args in view.ran if args[1:2] == ("build",)]
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "The image of claude: not built (sandbox-ai build NAME)." in said, widgets.SHOWN


def test_another_image_or_the_tests_images_are_not_looked_at(view, monkeypatch):
    monkeypatch.setattr(prerequisites.shutil, "which", lambda name: "/usr/bin/podman")
    assert view.shown(["--image-variant", "media"]).agent_image_need("local") == []
    assert view.shown(["--backend", "bwrap"]).agent_image_need("local") == []
    monkeypatch.setenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", "1")
    assert view.shown([]).agent_image_need("local") == []
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK")
    monkeypatch.setattr(view, "agent_images_built", lambda host, agents: {a: False for a in agents})
    assert view.shown([]).agent_image_need("local") == ["claude"]
    assert view.agent_image_need("lab") == []  # (no list of lab read: the start asks itself)
    view.agent_images_answered("local", {"claude": True})  # (the next look: built)
    assert view.agent_image_need("local") == []  # (built: the start is not held up for it)


def test_a_missing_vpn_profile_is_a_tor_line_of_the_same_question(view, monkeypatch):
    view.shown(["--vpn", "anon"])
    view.check_prerequisites(then=lambda: view.started.append(1), why="start")
    (text,) = asked()
    assert "the VPN profile anon is not on this machine → made a Tor profile" in text
    assert "security: the sandbox's connections go through Tor" in text
    assert not [title for title, _text, _ in widgets.SHOWN if title.startswith("VPN profile")], "no question of its own"
    assert view.profiles == ["anon"] and view.started == [1], widgets.SHOWN


def test_declined_the_tor_line_leaves_the_vpn_page_and_the_button(view, monkeypatch):
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    view.shown(["--vpn", "anon"])
    view.check_prerequisites(then=lambda: view.started.append(1), why="start")
    assert view.profiles == [] and view.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "VPN profile anon: make it on the VPN page" in said


def test_a_way_to_start_changes_only_the_options_it_names():
    assert prerequisites.changed_options(["--cairn", "--cairn-workers", "claude", "--mcp=x"], ["--cairn-workers",
                                                                                               "codex"],
                                         ["--cairn-workers"]) == ["--cairn", "--mcp=x", "--cairn-workers", "codex"]
    assert prerequisites.changed_options(["--hermes-enable=a", "--vpn", "t"], [], ["--hermes-enable"]) == ["--vpn", "t"]


def test_the_update_of_an_older_host_is_a_fix_run_on_this_machine():
    class TooOld(ValueError):
        missing = ["--fix-plan"]
    TooOld.__name__ = "HostTooOld"
    item = prerequisites.host_update_fix("lab", TooOld())
    assert item["commands"] == [["sandbox-ai", "hosts", "update", "lab"]] and item["here"] is True
    assert item["verdict"] == "offered" and "does not know --fix-plan" in item["missing"]
    plan = backend.checked_fix_plan(json.dumps({"fixes": [item]}))
    assert plan["fixes"][0]["verdict"] == "offered"  # (one of sandbox-ai's own commands)
    assert backend.checked_fix_plan(json.dumps({"fixes": [{**item, "commands": [["sandbox-ai", "publish-logins", "add",
                                                                                 "npm", "--secret", "T"]]}]}))[
        "fixes"][0]["verdict"] == "offered"


def test_an_image_still_missing_after_its_build_is_said_not_built_again(view, monkeypatch):
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", raising=False)
    monkeypatch.setattr(prerequisites.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    monkeypatch.setattr(view, "agent_images_built", lambda host, agents: {a: False for a in agents})
    view.shown([])
    view.check_prerequisites(then=lambda: view.started.append(1), why="start")
    assert [args for args in view.ran if args[1:2] == ("build",)] == [("local", "build", "claude")]
    assert view.started == []
    said = "\n".join(text for title, text, _ in widgets.SHOWN if title == "Not started")
    assert "the image of claude is still not built after its build ran" in said, widgets.SHOWN


def test_what_asks_for_a_value_in_a_terminal_is_left_to_the_starts_own_terminal(view, monkeypatch):
    """A fix of the launcher's plan that asks for a token (a publish login): not a reason to stop in the app - the
    start goes on to its terminal tab, where the launcher asks it (said in the notes)."""
    publish = {"id": "publish:npm", "kind": "publish", "name": "npm", "missing": "there is no publish login for npm",
               "title": "add it", "commands": [["sandbox-ai", "secrets", "set", "NPM_TOKEN"],
                                               ["sandbox-ai", "publish-logins", "add", "npm", "--secret", "NPM_TOKEN"]],
               "duration": "", "unsigned": False, "security": "the agent can publish as you", "terminal": True,
               "needed": True, "verdict": "offered", "why": ""}
    real = view.sai.host_run

    def run(name, *args, **kw):
        if args and args[-1] == "--fix-plan":
            view.ran.append((name, *args))
            return 0, json.dumps({"fixes": [publish], "questions": "all", "mode": "medium"}), ""
        return real(name, *args, **kw)
    monkeypatch.setattr(view.sai, "host_run", run)
    notes = []
    monkeypatch.setattr(view, "prerequisite_notice", lambda text, *a, **k: notes.append(text))
    view.shown(["--toolchain", "go", "--publish", "npm"])
    view.check_prerequisites(then=lambda: view.started.append(1), why="start")
    assert view.started == [1] and asked() == [], widgets.SHOWN
    assert any("the start asks for it in its terminal" in note for note in notes)
    assert not [title for title, _t, _c in widgets.SHOWN if title == "Not started"]


# -- round 2 (2026-11-22): the image is looked at in the background while the start is shown, never at Start --

def podman_stand_in(monkeypatch, view, built):
    """agent_images_built (this machine's `podman image exists`) answered from BUILT, each call noted with whether it
    ran on the GUI thread (view.looks: [(agents, on the GUI thread)])."""
    import threading
    monkeypatch.delenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", raising=False)
    monkeypatch.setattr(prerequisites.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    view.looks = []

    def looked(host, agents):
        view.looks.append((tuple(agents), threading.current_thread() is threading.main_thread()))
        return {agent: built.get(agent, True) for agent in agents}
    monkeypatch.setattr(view, "agent_images_built", looked)


def test_the_boards_agent_image_is_looked_at_in_the_background_and_start_asks_no_podman(view, monkeypatch, wait):
    """Round 1 asked Podman on the GUI thread when Start was pressed (up to 5 s): now the Board looks at its agent's
    image in the background as soon as it shows the agent, keeps the answer, and Start only reads it."""
    monkeypatch.setattr(prerequisites, "background", widgets.background)  # (a real thread)
    podman_stand_in(monkeypatch, view, {"claude": False, "codex": True})
    view.shown([])
    assert wait(lambda: view.known_agent_image(prerequisites.LOCAL, "claude") is False)
    assert view.looks and not any(on_gui for _agents, on_gui in view.looks), view.looks
    assert view.agent_image_need(prerequisites.LOCAL) == ["claude"]  # (from the answer kept)
    from sandbox_ai_app import board as B
    count = len(view.looks)
    view.set_board(B.from_argv(view.sai, "codex", [], []), None)  # another agent: looked at too
    assert wait(lambda: view.known_agent_image(prerequisites.LOCAL, "codex") is True)
    assert len(view.looks) == count + 1 and not any(on_gui for _agents, on_gui in view.looks)
    looks = len(view.looks)
    assert view.agent_image_need(prerequisites.LOCAL) == [] and len(view.looks) == looks  # (Start: no Podman)
    view.set_board(B.from_argv(view.sai, "codex", ["--vpn", "x"], []), None)  # the same agent: not looked at again
    assert len(view.looks) == looks


def test_a_start_pressed_before_the_answer_is_not_held_up(view, monkeypatch, wait):
    """Start before the look has answered: the image counts as there (the start says it in its terminal when it is
    not, as before) - nothing waits for Podman on the GUI thread."""
    import threading
    gate = threading.Event()
    monkeypatch.setattr(prerequisites, "background", widgets.background)
    podman_stand_in(monkeypatch, view, {"claude": False})
    inner = view.agent_images_built

    def slow(host, agents):
        gate.wait(10)
        return inner(host, agents)
    monkeypatch.setattr(view, "agent_images_built", slow)
    view.shown([])
    assert view.known_agent_image(prerequisites.LOCAL, "claude") is None
    assert view.agent_image_need(prerequisites.LOCAL) == []
    gate.set()
    assert wait(lambda: view.known_agent_image(prerequisites.LOCAL, "claude") is False)
    assert not any(on_gui for _agents, on_gui in view.looks)


def test_the_expert_view_looks_at_its_agents_image_when_the_agent_changes(qapp, sai, monkeypatch, wait):
    from sandbox_ai_app import ui
    page = ui.LaunchPage(sai, lambda *a, **k: None)
    monkeypatch.setattr(prerequisites, "background", widgets.background)
    podman_stand_in(monkeypatch, page, {"codex": False})
    page.agent.setCurrentText("claude")
    page.agent.setCurrentText("codex")
    assert wait(lambda: page.known_agent_image(prerequisites.LOCAL, "codex") is False)
    assert not any(on_gui for _agents, on_gui in page.looks)
    assert page.agent_image_need(prerequisites.LOCAL) == ["codex"]
    page.close()
    page.deleteLater()
