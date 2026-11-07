"""SANDBOX_AI_APP_REQUIRE_QT=1 (set in CI) turns a skip for missing Qt into a failure (tests/conftest.py)."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_ui_tests(require):
    environment = {**os.environ, "SANDBOX_AI_APP_REQUIRE_QT": "1" if require else ""}
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_ui.py",
                           "-k", "test_pages_and_preview"], cwd=ROOT, env=environment, capture_output=True, text=True,
                          timeout=300)


def test_a_qt_skip_fails_when_qt_is_required():
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("Qt is installed here: nothing is skipped for it")  # not a Qt skip: allowed
    quiet, strict = run_ui_tests(False), run_ui_tests(True)
    assert quiet.returncode in (0, 5) and "skipped" in quiet.stdout, quiet.stdout + quiet.stderr  # 5: none ran
    assert strict.returncode != 0 and "SANDBOX_AI_APP_REQUIRE_QT=1" in strict.stdout, strict.stdout + strict.stderr


def test_ci_requires_qt_and_runs_offscreen():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert 'SANDBOX_AI_APP_REQUIRE_QT: "1"' in workflow and "QT_QPA_PLATFORM: offscreen" in workflow
    assert "runs-on: ubuntu-24.04" in workflow and "ref: ${{ env.SANDBOX_AI_REF }}" in workflow
    assert "libegl1" in workflow and "libxkbcommon0" in workflow and "PySide6" in workflow
