"""A model server of the local network behind Tor (0.1.94): the board's refusal line names the fix and offers it -
exactly that address and port go into the VPN part after one question; the refusal can be copied and read whole."""
import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import backend, widgets  # noqa: E402

from test_board_view import page  # noqa: E402,F401
from test_projects import env, make_window, project, window  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
REFUSAL = ("agent-sandbox: --llm macbook: http://192.168.1.20:1234 lies in your local network, which a Tor start "
           "cannot reach through Tor (--vpn tor-test: ...). Reach exactly this server past Tor with --vpn-bypass-tcp "
           "192.168.1.20:1234 (the rest of your network stays closed; ...), use a server on this host's loopback")


def refuse(page):
    page.check_token = getattr(page, "check_token", 0)
    page.check_running = True
    page.check_pending = False
    page.checking_state = page.board.state()  # (what check() keeps: the board its verdict is about)
    page.checked(page.check_token, None, REFUSAL)


def test_the_refusal_offers_exactly_the_named_bypass(page, qapp, monkeypatch):
    if page.board.first("vpn") is None:
        page.board.add("vpn")
    refuse(page)
    assert not page.bypass_button.isHidden()
    assert "192.168.1.20:1234" in page.bypass_button.text()
    monkeypatch.setattr(widgets, "AUTO_ANSWER", False)
    page.allow_past_tor()
    assert not page.board.first("vpn").props.get("--vpn-bypass-tcp"), "declined: nothing changes"
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.allow_past_tor()
    page.allow_past_tor()
    assert page.board.first("vpn").props["--vpn-bypass-tcp"] == ["192.168.1.20:1234"]
    page.checked(page.check_token, ({"plan": True}, None), None)
    assert page.bypass_button.isHidden(), "accepted: no offer left"


def test_an_offer_of_another_board_is_never_applied(page, qapp, monkeypatch, tmp_path):
    """Review: the offer stayed until the next verdict came - after another project's board was shown, or an edit,
    a click added the earlier board's address to the board shown now."""
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    refuse(page)
    page.change("an edit", lambda: page.board.add("limits"))  # edited meanwhile: its verdict is not there yet
    page.allow_past_tor()
    assert not any(n.props.get("--vpn-bypass-tcp") for n in page.board.of_kind("vpn"))
    other = tmp_path / "other"
    other.mkdir()
    page.open_project(str(other), quiet=True)
    assert page.bypass_button.isHidden() and page.bypass_target is None


def test_a_refusal_line_is_copied_whole(page, qapp):
    refuse(page)
    page.verdict.copy_text()
    assert "--vpn-bypass-tcp 192.168.1.20:1234" in QtWidgets.QApplication.clipboard().text()
    window = widgets.show_text_window(page, "Whole text", page.verdict.text())
    view = window.findChild(QtWidgets.QPlainTextEdit)
    assert view.isReadOnly() and REFUSAL in view.toPlainText()
    window.close()


def test_messages_and_statuses_can_be_marked():
    label = widgets.plain_label("refused", selectable=True)
    from PySide6.QtCore import Qt
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse


def test_without_a_vpn_part_the_offer_makes_one(page, qapp, monkeypatch):
    for node in list(page.board.of_kind("vpn")):
        page.board.remove(node.id)
    refuse(page)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    page.allow_past_tor()
    assert page.board.first("vpn").props["--vpn-bypass-tcp"] == ["192.168.1.20:1234"]


def test_the_refusal_line_copies_with_ctrl_c_and_opens_whole_on_a_double_click(page, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    refuse(page)
    QtWidgets.QApplication.clipboard().setText("")
    QTest.keyClick(page.verdict, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert "--vpn-bypass-tcp 192.168.1.20:1234" in QtWidgets.QApplication.clipboard().text()
    shown = []
    monkeypatch.setattr(widgets, "show_text_window", lambda parent, title, text: shown.append(text))
    QTest.mouseDClick(page.verdict, Qt.MouseButton.LeftButton)
    assert shown and REFUSAL in shown[0]
