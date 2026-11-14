"""Deep test round 1 (features of 0.1.93, 0.1.94 and 0.1.95) in the app: what a hostile, careless or impatient user
ran into, each bug a test named after it, through the app's real code paths offscreen."""
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from conftest import open_tab  # noqa: E402
from sandbox_ai_app import backend, images_page  # noqa: E402
from test_terminal_reopen import sai, stub_web, terminal  # noqa: E402,F401
from test_terminal_tab_session import page  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("name", ["x-", "a..b", "a___b", "trail.", "_x", "a" * 31])
def test_an_image_name_podman_cannot_tag_is_flagged_in_the_dialog(name):
    """The Images page's dialogs took x- or a..b as the name of an image of yours; sandbox-ai then tagged
    localhost/sandbox-ai-image-x-:latest, which Podman refuses ("invalid reference format")."""
    assert not images_page.IMAGE_NAME.fullmatch(name)


@pytest.mark.parametrize("name", ["redis", "a.b", "a_b", "a__b", "a-b", "a--b", "pg16", "a" * 30])
def test_image_names_podman_takes_are_still_taken(name):
    assert images_page.IMAGE_NAME.fullmatch(name)


@pytest.mark.parametrize("answer", ["close", "stop"])
def test_a_tab_closed_by_itself_while_its_close_question_is_open(page, sai, monkeypatch, answer):
    """The question of a running session's tab (Close tab / Stop session / Cancel) blocks no window: the session ended
    meanwhile, its tab closed by itself (AUTO_CLOSE_MS) and was deleted inside the question's event loop - the answer
    then went to a tab that no longer was: RuntimeError "Internal C++ object already deleted"."""
    from PySide6.QtCore import QCoreApplication, QEvent
    view = open_tab(page, sai.command("attach", "web"), "web")

    def ended_meanwhile(*_args, **_kwargs):
        # (close_ended's timer fired while the question was open: its command had ended)
        page.close_tab(page.tabs.indexOf(view), ask=False)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return answer
    monkeypatch.setattr(terminal_module(), "choose", ended_meanwhile)
    monkeypatch.setattr(backend, "needs_confirm", lambda *_a, **_k: True)
    page.close_tab(page.tabs.indexOf(view))  # (no exception)
    assert page.tabs.count() == 0


def test_close_other_tabs_when_one_of_them_closed_by_itself_under_the_first_question(page, sai, monkeypatch):
    """Close other tabs asks for each running session's tab; one that closed by itself (its session ended) while the
    first question was open was gone when its turn came: RuntimeError instead of the next question."""
    from PySide6.QtCore import QCoreApplication, QEvent
    keep = open_tab(page, sai.command("attach", "web"), "web")
    first = open_tab(page, sai.command("attach", "one"), "one")
    second = open_tab(page, sai.command("attach", "two"), "two")
    assert page.tabs.count() == 3
    asked = []

    def answer(*_args, **_kwargs):
        asked.append(1)
        if len(asked) == 1:  # under the first question the second tab's session ended and its tab went
            page.close_tab(page.tabs.indexOf(second), ask=False)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return "close"
    monkeypatch.setattr(terminal_module(), "choose", answer)
    monkeypatch.setattr(backend, "needs_confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(page, "ended", lambda _view: False)  # (the stand-in attaches may end on their own)
    page.close_others(keep)  # (no exception)
    assert page.tabs.count() == 1 and page.tabs.widget(0) is keep and asked == [1]
    assert first is not None


def test_a_restart_that_answers_after_its_tab_went_does_nothing(page, sai, monkeypatch):
    """Restart runs `resume --restart` in the background (up to 5 minutes): a tab closed meanwhile was asked for its
    place when the answer came - RuntimeError in the callback."""
    from PySide6.QtCore import QCoreApplication, QEvent
    view = open_tab(page, sai.command("attach", "web"), "web")
    answers = []
    monkeypatch.setattr(terminal_module(), "background",
                        lambda *_args, done=None, **_kwargs: answers.append(done))
    monkeypatch.setattr(backend, "needs_confirm", lambda *_a, **_k: None)
    assert page.restart(view)
    page.close_tab(page.tabs.indexOf(view), ask=False)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    answers[0]((0, "", ""), None)  # (no exception)
    assert page.tabs.count() == 0


@pytest.mark.parametrize("path", ["proj", "projects/new", "./x"])
def test_a_relative_workspace_on_the_board_is_an_error_never_created_under_the_apps_folder(tmp_path, monkeypatch,
                                                                                          path):
    """A board's workspace typed as `projects/new` was offered for Create now and made below the folder the app was
    started in; its start then opened the terminal in that relative folder and passed --workspace projects/new on
    top of it (projects/new/projects/new). It is an error: a workspace is given in full."""
    from sandbox_ai_app import board as B
    monkeypatch.chdir(tmp_path)
    board = B.Board()
    board.add("agent", agent="claude")
    node = board.add("workspace", path=path)
    assert B.missing_workspaces(board) == [], "never offered for Create now"
    errors = [p for p in B.problems(backend.SandboxAI(), board) if p.level == "error" and p.node == node.id]
    assert errors and "relative path" in errors[0].text, [p.text for p in B.problems(backend.SandboxAI(), board)]
    node.props["path"] = str(tmp_path / "abs")
    assert not [p for p in B.problems(backend.SandboxAI(), board) if p.level == "error" and p.node == node.id]
    node.props["path"] = "~/somewhere-new"
    assert not [p for p in B.problems(backend.SandboxAI(), board) if p.level == "error" and p.node == node.id]


def terminal_module():
    from sandbox_ai_app import terminal
    return terminal


def test_the_default_name_of_a_long_reference_is_one_podman_can_tag():
    name = images_page.default_image_name("ghcr.io/org/abcdefghijklmnopqrstuvwxyz012-x:1")
    assert name and images_page.IMAGE_NAME.fullmatch(name), name
