"""The app's checks before any window: a missing system library of Qt's network module is said clearly (Debian 12
without libgssapi-krb5-2 crashed with a traceback in the AppImage's e2e test of 0.1.96)."""
import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from sandbox_ai_app import main  # noqa: E402


def test_a_missing_gssapi_library_is_named_with_its_package():
    def importer():
        raise ImportError("libgssapi_krb5.so.2: cannot open shared object file: No such file or directory")
    text = main.missing_qt_network(importer)
    assert "libgssapi_krb5.so.2" in text and "libgssapi-krb5-2" in text and "Traceback" not in text


def test_a_loading_network_module_says_nothing():
    assert main.missing_qt_network(lambda: None) is None
