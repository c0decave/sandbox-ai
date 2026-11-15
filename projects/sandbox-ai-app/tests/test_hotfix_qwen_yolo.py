"""Hotfix 0.1.96, from live use: Qwen Code started from the Board with "Agent without approval prompts" ticked.
The Board (offscreen) as the user works it, its Start running the real sandbox-ai of SANDBOX_AI_HOME as a
subprocess with only Podman, tmux, npm and ssh faked (sandbox-ai's tests/live_fakes.py)."""
import json
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import widgets  # noqa: E402

from test_hotfix_round_2 import as_user  # noqa: E402
from test_live_regressions import board_page, run_tab, user  # noqa: E402,F401 - the fixtures

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME"), reason="set SANDBOX_AI_HOME to a sandbox-ai "
                                                                             "checkout")


def started_qwen(page, user, tmp_path, tick):
    """The Board with Qwen Code, the agent part's box "Agent without approval prompts" ticked (TICK) or not, Start
    pressed; what the sandbox got: (its command, its /etc/qwen-code/settings.json)."""
    user.add_image("localhost/qwen-sandbox:latest")
    page.show_board("qwen")
    page.inspect(page.board.first("agent"))
    box = next(b for b in page.inspector_holder.findChildren(QtWidgets.QCheckBox)
               if b.text().startswith("Agent without approval prompts"))
    box.setChecked(tick)
    assert page.board.first("agent").props.get("--dangerously-skip-permissions", False) is tick
    page.check()
    widgets.SHOWN.clear()
    page.start()
    assert page.started, widgets.SHOWN
    argv = page.started[-1]
    assert ("--dangerously-skip-permissions" in argv or "-D" in argv) is tick, argv
    kept = tmp_path / ("kept-D" if tick else "kept")
    result = run_tab(as_user([*argv, *([] if "--no-tmux" in argv else ["--no-tmux"])], user),
                     env={**user.env, "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(kept)})
    session = kept / "session"
    assert (session / "config.json").is_file(), result.stderr
    return (json.loads((session / "config.json").read_text())["command"],
            json.loads((session / "qwen/settings.json").read_text()))


def test_qwen_started_without_approval_prompts_is_in_yolo_mode(board_page, user, tmp_path):
    """User: "Qwen, wenn 'ohne Rückfragen' angeklickt ist, im YOLO-Modus starten." Ticked on the Board, Qwen Code
    gets --yolo and YOLO in its system settings with /work trusted (Qwen Code 0.26.4 turned --yolo alone into
    "default" where folder trust was on); not ticked, it gets neither and still asks."""
    command, settings = started_qwen(board_page, user, tmp_path, True)
    assert command[0].endswith("qwen") and "--yolo" in command
    assert settings["tools"]["approvalMode"] == "yolo" and settings["security"]["folderTrust"] == {"enabled": False}
    assert settings["telemetry"]["enabled"] is False and settings["model"]["maxSessionTurns"] == 120
    board_page.started.clear()
    command, settings = started_qwen(board_page, user, tmp_path, False)
    assert "--yolo" not in command and "approvalMode" not in settings["tools"] and "security" not in settings
