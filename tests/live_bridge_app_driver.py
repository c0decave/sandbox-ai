"""The app's New bridge… between two real sessions, driven as a human does it - for the live check bridge-live
(tests/test_live_gaps.py::test_live_a_bridge_between_two_real_sessions), with a Python that has PySide6:

    python3 -I live_bridge_app_driver.py CHECKOUT SESSION_A SESSION_B

The app's real window (offscreen: QT_QPA_PLATFORM), against the sandbox-ai of CHECKOUT (SANDBOX_AI_HOME) and the
environment it is given (the live check's own folders, where both sessions run): Sandboxes & sessions, New bridge…
clicked; in its dialog A and B chosen and "Send messages" ticked, Check and create… clicked; the launcher's real dry
run, its real question answered with Create bridge; the app's message that the bridge is active read. Each step is a
JSON line ({"step", ...}); the last line is APP_RESULT {"opened": the dialog opened, "id": the bridge made or None,
"messages": [what the app said], "problem": text or None}.

drive() is the whole of it on a window given; the app's tests run it on their window with stand-in sessions
(projects/sandbox-ai-app/tests/test_bridge_clicks_app.py), so what the live check runs is tested here too.
"""
import json
import re
import sys
import time


def drive(qt, window, first, second, say=lambda **fields: None, seconds=180):
    """New bridge… on WINDOW's Sandboxes page between FIRST and SECOND, through the real dialog and question (its
    widgets.AUTO_ANSWER None from the click on). The result (APP_RESULT's)."""
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from sandbox_ai_app import bridges_view, widgets

    result = {"opened": False, "id": None, "messages": [], "problem": None}

    def wait(condition, limit):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            qt.processEvents()
            if condition():
                return True
            time.sleep(0.05)
        qt.processEvents()
        return bool(condition())

    def shown():
        return [w for w in QApplication.topLevelWidgets() if w.isVisible() and w is not window]

    window.show()
    window.show_page("sandboxes")
    page = window.sandboxes
    box = page.bridges_box

    def both_listed():
        names = {entry["name"] for entry in page.bridge_sessions()}
        if not {first, second} <= names:
            page.refresh_soon(again=True)
        return {first, second} <= names
    if not wait(both_listed, seconds):
        result["problem"] = ("the Sessions page did not list both sessions as running: "
                             f"{sorted(entry['name'] for entry in page.bridge_sessions())}")
        return result
    say(step="listed", sessions=[entry["name"] for entry in page.bridge_sessions()])
    widgets.AUTO_ANSWER = None

    def refused():
        return any(said.startswith(("Not bridged:", "New bridge: A bridge joins", "New bridge: This did not work"))
                   for said in result["messages"])

    def fill():
        dialog = next((w for w in shown() if isinstance(w, bridges_view.BridgeDialog)), None)
        if dialog is None:
            QTimer.singleShot(100, fill)
            return
        result["opened"] = True
        dialog.a.setCurrentIndex(dialog.a.findData(first))
        dialog.b.setCurrentIndex(dialog.b.findData(second))
        dialog.message.setChecked(True)
        say(step="dialog", a=dialog.a.currentData(), b=dialog.b.currentData())
        QTest.mouseClick(dialog.ok, Qt.MouseButton.LeftButton)

    def answer():
        for message in [w for w in shown() if isinstance(w, QMessageBox)]:
            text = message.text()
            if text.startswith("Create this bridge?"):
                say(step="question", text=text)
                create = next(button for button in message.buttons() if button.text() == "Create bridge")
                QTest.mouseClick(create, Qt.MouseButton.LeftButton)
                continue
            said = f"{message.windowTitle()}: {text}"
            if said not in result["messages"]:
                result["messages"].append(said)
            found = re.search(r"Bridge ([0-9a-f]{8}) is active", text)
            if found:
                result["id"] = found.group(1)
            message.close()
        if result["id"] is None and not refused():
            QTimer.singleShot(100, answer)

    QTimer.singleShot(100, fill)
    QTimer.singleShot(100, answer)
    QTest.mouseClick(box.buttons["new"], Qt.MouseButton.LeftButton)
    if not wait(lambda: result["id"] is not None or refused(), 300):
        result["problem"] = "no bridge was made within 300 s" + (
            f"; the app said: {result['messages']}" if result["messages"] else "")
    elif result["id"] is None:
        result["problem"] = f"not bridged: {result['messages']}"
    widgets.AUTO_ANSWER = True
    return result


def main(argv):
    checkout, first, second = argv[:3]
    sys.path[0:0] = [f"{checkout}/projects/sandbox-ai-app"]
    from PySide6.QtWidgets import QApplication
    qt = QApplication(["sandbox-ai-app"])
    from sandbox_ai_app import backend, terminal, widgets
    widgets.collect_on_gui_thread()  # as the app does (main.py)
    widgets.nonblocking_dialogs()
    terminal.available = lambda web=None: (False, "not in the live check")  # (no terminal tab is opened)
    widgets.AUTO_ANSWER = True  # the window's own start asks nothing; from the click on, the real dialogs
    from sandbox_ai_app.ui import MainWindow

    def say(**fields):
        print(json.dumps(fields, default=str), flush=True)
    window = MainWindow(backend.SandboxAI(), check_updates=False, approvals_interval=0)
    if window.tray is not None:
        window.tray.hide()
    try:
        result = drive(qt, window, first, second, say)
    except Exception as exc:  # noqa: BLE001 - said in the result
        result = {"opened": False, "id": None, "messages": [], "problem": f"{type(exc).__name__}: {exc}"}
    print("APP_RESULT " + json.dumps(result), flush=True)
    widgets.AUTO_ANSWER = True
    window.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
