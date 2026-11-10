"""The start: a PySide6 release known to crash this Python is refused with a way out."""
import pytest

pytest.importorskip("PySide6", reason="PySide6 is not installed")

from sandbox_ai_app import main  # noqa: E402


def test_pyside_6_12_0_is_refused_below_python_3_12():
    message = main.broken_pyside("6.12.0", (3, 11, 2))
    assert "deallocating None" in message and "!=6.12.0" in message and "--app-deps" in message
    assert main.broken_pyside("6.12.0", (3, 12, 0)) is None
    assert main.broken_pyside("6.11.3", (3, 11, 2)) is None
    assert main.broken_pyside("6.12.1", (3, 9, 0)) is None


def test_main_stops_before_qt_with_a_broken_release(monkeypatch, capsys):
    monkeypatch.setattr(main, "broken_pyside", lambda: "sandbox-ai-app: PySide6 6.12.0 crashes")
    assert main.main(["sandbox-ai-app"]) == 1
    assert "crashes" in capsys.readouterr().err


def test_the_terminal_tabs_run_without_the_gpu_unless_asked():
    """A machine without usable acceleration filled app.log with Chromium's Vulkan, GLX and command-buffer errors;
    xterm.js needs no GPU."""
    from sandbox_ai_app.main import chromium_flags
    no_gpu = "--disable-gpu --disable-gpu-compositing --disable-features=Vulkan,DefaultANGLEVulkan,VulkanFromANGLE"
    assert chromium_flags({}) == no_gpu + " --log-level=2"
    assert chromium_flags({"SANDBOX_AI_APP_GPU": "1"}) == "--log-level=2"
    # --debug keeps Chromium quiet (its chatter flooded the terminal); --debug=all lets it talk
    assert chromium_flags({"SANDBOX_AI_APP_DEBUG": "1"}) == no_gpu + " --log-level=2"
    assert chromium_flags({"SANDBOX_AI_APP_DEBUG": "all"}) == no_gpu
    assert chromium_flags({"SANDBOX_AI_APP_GPU": "1", "SANDBOX_AI_APP_DEBUG": "all"}) is None
    assert chromium_flags({"QTWEBENGINE_CHROMIUM_FLAGS": "--enable-gpu-rasterization"}) is None  # yours wins
    assert chromium_flags({"QTWEBENGINE_CHROMIUM_FLAGS": ""}) is None


def test_self_test_builds_the_window_and_quits(monkeypatch, capsys, tmp_path):
    """--self-test (the AppImage's, the macOS app's and CI's check): the main window is built against the
    sandbox-ai found, its launcher loads, and the start ends with 0 without a dialog, intro or tour."""
    import os
    home = os.environ.get("SANDBOX_AI_HOME")
    if not home:
        pytest.skip("set SANDBOX_AI_HOME to a sandbox-ai checkout")
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    monkeypatch.setenv("SANDBOX_AI_HOME", home)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    assert main.main(["sandbox-ai-app", "--self-test"]) == 0
    out = capsys.readouterr().out
    assert "sandbox-ai-app self-test: ok" in out and "sandbox-ai " in out


def test_self_test_reports_a_missing_sandbox_ai_without_a_dialog(monkeypatch, capsys, tmp_path):
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: pytest.fail("a dialog in --self-test"))
    monkeypatch.setenv("SANDBOX_AI_HOME", str(tmp_path))
    assert main.main(["sandbox-ai-app", "--self-test"]) == 1
    assert "self-test: sandbox-ai not found" in capsys.readouterr().err


def test_a_start_at_login_runs_the_appimage_or_the_macos_app(monkeypatch, tmp_path):
    """Started from the AppImage (or sandbox-ai.app), the copy runs with PySide6 from inside the package: the
    autostart entry must run the package (SANDBOX_AI_APP_PACKAGE_LAUNCHER, set by its start), but only for the
    copy that package runs."""
    from sandbox_ai_app import backend
    package = tmp_path / "sandbox-ai-x86_64.AppImage"
    package.write_text("#!/bin/sh\n")
    package.chmod(0o755)
    copy = tmp_path / "cache" / "sandbox-ai" / "appimage" / "1.0-abc" / "sandbox-ai" / "projects" / "app" / "sandbox_ai_app"
    copy.mkdir(parents=True)
    real = backend.packaged_copy
    monkeypatch.setattr(backend, "packaged_copy", lambda launcher: real(launcher, copy))  # as if it ran there
    monkeypatch.setenv("SANDBOX_AI_APP_PACKAGE_LAUNCHER", str(package))
    assert backend.autostart_command() == [str(package), "--hidden"]
    assert str(package) in backend.autostart_paths()
    package.chmod(0o644)  # not executable, relative or missing: never named
    assert backend.installed_launcher() is None
    monkeypatch.setenv("SANDBOX_AI_APP_PACKAGE_LAUNCHER", "sandbox-ai.AppImage")
    assert backend.installed_launcher() is None


def test_the_package_launcher_counts_only_for_the_copy_it_runs(tmp_path):
    """Inherited by an app from install.sh or pip (a terminal opened by the AppImage's app), it is ignored."""
    from sandbox_ai_app import backend
    appimage = tmp_path / "sandbox-ai-x86_64.AppImage"
    appimage.write_text("")
    appimage.chmod(0o755)
    macos = tmp_path / "sandbox-ai.app" / "Contents" / "MacOS" / "sandbox-ai-app"
    macos.parent.mkdir(parents=True)
    macos.write_text("")
    macos.chmod(0o755)
    inside = tmp_path / "sandbox-ai.app/Contents/Resources/sandbox-ai/projects/sandbox-ai-app/sandbox_ai_app"
    cached = tmp_path / ".cache/sandbox-ai/appimage/0.1.56-abc/sandbox-ai/projects/sandbox-ai-app/sandbox_ai_app"
    installed = tmp_path / ".local/share/sandbox-ai/versions/0.1.56/projects/sandbox-ai-app/sandbox_ai_app"
    for folder in (inside, cached, installed):
        folder.mkdir(parents=True)
    assert backend.packaged_copy(str(macos), inside) and backend.packaged_copy(str(appimage), cached)
    assert not backend.packaged_copy(str(appimage), installed) and not backend.packaged_copy(str(macos), installed)


def test_main_takes_the_package_launcher_out_of_the_environment(monkeypatch):
    from sandbox_ai_app import backend
    monkeypatch.setattr(backend, "PACKAGE_LAUNCHER", None)
    monkeypatch.setenv("SANDBOX_AI_APP_PACKAGE_LAUNCHER", "/apps/sandbox-ai.AppImage")
    monkeypatch.setattr(main, "broken_pyside", lambda: "stop here")
    assert main.main(["sandbox-ai-app"]) == 1
    import os
    assert "SANDBOX_AI_APP_PACKAGE_LAUNCHER" not in os.environ
    assert backend.PACKAGE_LAUNCHER == "/apps/sandbox-ai.AppImage"


def test_no_test_opens_a_real_browser():
    """In a sandbox $BROWSER and xdg-open hand a URL to the human's desktop: the tests never reach them."""
    import os
    import shutil
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    import conftest
    assert "no-browser-bin" in os.environ["BROWSER"] and "no-browser-bin" in shutil.which("xdg-open")
    QDesktopServices.openUrl(QUrl("https://login.example/never"))
    assert conftest.OPENED_URLS[-1].toString() == "https://login.example/never"
