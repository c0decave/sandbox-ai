"""The `sandbox-ai-app` command of the PyPI package (launch.py): without the extra [gui] it says how to add PySide6."""
import importlib.util

from sandbox_ai_app import launch


def test_without_pyside6_it_names_the_extra(capsys):
    message = launch.missing_pyside(find_spec=lambda name: None)
    assert "pip install 'sandbox-ai-app[gui]'" in message and "pipx install --force 'sandbox-ai-app[gui]'" in message
    assert all(line.startswith("sandbox-ai-app: ") for line in message.splitlines())


def test_with_pyside6_nothing_is_said():
    assert launch.missing_pyside(find_spec=lambda name: object()) is None
    assert launch.missing_pyside() is (None if importlib.util.find_spec("PySide6") else launch.missing_pyside())


def test_main_ends_with_1_before_anything_of_qt(monkeypatch, capsys):
    monkeypatch.setattr(launch, "missing_pyside", lambda: "sandbox-ai-app: needs PySide6")
    assert launch.main([]) == 1
    assert "needs PySide6" in capsys.readouterr().err
