"""Hotfix round 1 of 0.1.95 (systematic coverage), the app's part: one test per bug found, named after it."""
import json
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from test_projects import env, window  # noqa: E402,F401 - shared fixtures

from sandbox_ai_app import presets as P

ROOT = Path(__file__).resolve().parents[3]


def hosts_of(options):
    return {options[i + 1] for i, option in enumerate(options) if option == "--allow-host" and i + 1 < len(options)}


def test_the_gpu_preset_opens_hugging_faces_file_hosts():
    """The app's preset "GPU workstation" promises "PyPI and Hugging Face for models" but opened only huggingface.co:
    Hugging Face serves the model files themselves from cdn-lfs.hf.co (LFS) and cas-bridge.xethub.hf.co (Xet), so
    every download was refused. sandbox-ai's own preset data-ml opens them (0.1.95's CI fix); the app's must too."""
    data_ml = json.loads((ROOT / "sandbox/presets/data-ml.json").read_text())
    hugging_face = {host for host in hosts_of(data_ml["options"]) if "huggingface" in host or host.endswith("hf.co")}
    assert {"huggingface.co", "cdn-lfs.hf.co", "cas-bridge.xethub.hf.co"} <= hugging_face
    assert hugging_face <= hosts_of(list(P.BUILTIN["gpu"].options))


def test_the_kits_tables_context_menu_leaves_the_add_menu_alive(qapp, window):
    """Found by the sweep of every page (each button and context-menu entry once): after the Kits table's context
    menu had been open, "Install the kits' plugins" raised RuntimeError ("Internal C++ object (QAction) already
    deleted") - nothing was installed. The table's menu took the "Add to the kit" menu in as a submenu
    (QMenu.addMenu); when the context menu was dropped after use it took that menu and its actions (Skill from a
    folder…, Plugin by its name… - buttons of the page the busy bar disables) with it."""
    import shiboken6
    from PySide6 import QtCore
    from conftest import wait_until
    page = window.kits
    window.show()
    window.show_page("kits")
    wait_until(qapp, lambda: page.table.rowCount() > 0, 20)
    assert page.table.rowCount()

    def answer():
        popup = QtWidgets.QApplication.activePopupWidget()
        if popup is not None:
            popup.close()
    timer = QtCore.QTimer()
    timer.setInterval(20)
    timer.timeout.connect(answer)
    timer.start()
    try:
        page.table.customContextMenuRequested.emit(page.table.visualRect(page.table.model().index(0, 0)).center())
        for _ in range(5):
            qapp.processEvents()
            QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    finally:
        timer.stop()
    assert shiboken6.isValid(page.add_menu)
    assert all(shiboken6.isValid(action) for action in page.add_buttons), "the Add menu's entries were deleted"
    page.change_sources()  # (what the busy bar disables while a change runs)
    for widget in page.change_sources():
        widget.setEnabled(widget.isEnabled())
