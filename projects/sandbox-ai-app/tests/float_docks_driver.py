"""The app's main window with every panel floated out the way a drag leaves it (floating, with Qt's
X11BypassWindowManagerHint), then left to the app (MainWindow.settle_floating): what test_wm_live.py runs under a real
window manager. Run with the app's Python: python tests/float_docks_driver.py (SANDBOX_AI_HOME: the checkout)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def main():
    app = QApplication(["sandbox-ai-app"])
    from sandbox_ai_app import backend
    from sandbox_ai_app.ui import MainWindow
    window = MainWindow(backend.SandboxAI(), risk=None, lazy=False)
    window.show()

    def float_all():
        for dock in window.docks.values():
            dock.show()
            dock.setFloating(True)
            # What a drag that ended outside Qt's view leaves behind (spectrwm, 0.1.93): the bypass hint stays.
            dock.setWindowFlag(Qt.WindowType.X11BypassWindowManagerHint, True)
            dock.show()
            window.settle_floating(dock)
    QTimer.singleShot(1500, float_all)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
