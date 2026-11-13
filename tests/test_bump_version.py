"""scripts/bump_version.py: patch steps, the rollovers (patch below 250, minor below 50), explicit parts and bad input."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts/bump_version.py")
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)


@pytest.mark.parametrize("current, part, expected", [
    ("0.1.0", "patch", "0.1.1"),
    ("0.1.17", "patch", "0.1.18"),
    ("0.1.248", "patch", "0.1.249"),
    ("0.1.249", "patch", "0.2.0"),
    ("0.1.250", "patch", "0.2.0"),
    ("0.49.248", "patch", "0.49.249"),
    ("0.49.249", "patch", "1.0.0"),
    ("0.49.3", "minor", "1.0.0"),
    ("0.48.3", "minor", "0.49.0"),
    ("0.1.17", "minor", "0.2.0"),
    ("0.4.3", "major", "1.0.0"),
])
def test_next_version(current, part, expected):
    assert bump.next_version(current, part) == expected


def write(tmp_path, version):
    path = tmp_path / "agent_sandbox.py"
    path.write_text(f'import os\n\n__version__ = "{version}"\n\nX = "__version__ = \\"9.9.9\\""\n')
    return path


def test_main_rewrites_only_the_version_line(tmp_path, capsys):
    path = write(tmp_path, "0.1.250")
    assert bump.main(["--file", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "0.2.0"
    assert path.read_text() == 'import os\n\n__version__ = "0.2.0"\n\nX = "__version__ = \\"9.9.9\\""\n'


def test_set_and_show(tmp_path, capsys):
    path = write(tmp_path, "0.1.0")
    bump.main(["--file", str(path), "--set", "0.1.17"])
    bump.main(["--file", str(path), "--show"])
    assert capsys.readouterr().out.split() == ["0.1.17", "0.1.17"]


@pytest.mark.parametrize("bad", [["--set", "1.2"], ["--set", "v1.2.3"], ["--set", "1.2.3-rc1"]])
def test_rejects_bad_versions(tmp_path, bad):
    path = write(tmp_path, "0.1.0")
    with pytest.raises(SystemExit):
        bump.main(["--file", str(path), *bad])
    assert '__version__ = "0.1.0"' in path.read_text()


def test_missing_version_line(tmp_path):
    path = tmp_path / "agent_sandbox.py"
    path.write_text("nothing here\n")
    with pytest.raises(SystemExit):
        bump.main(["--file", str(path)])


def test_repository_version_is_plain_semver():
    bump.parse(bump.LINE.search((ROOT / "sandbox/agent_sandbox.py").read_text())[1])


def test_the_app_version_follows(tmp_path, monkeypatch):
    """Regression (0.1.94 was tagged with the app still saying 0.1.93): the bump raises the app's __version__ too."""
    source = tmp_path / "agent_sandbox.py"
    source.write_text('x = 1\n__version__ = "0.1.93"\n')
    app = tmp_path / "__init__.py"
    app.write_text('"""app"""\n__version__ = "0.1.93"  # the version of sandbox-ai it is released with\n')
    monkeypatch.setattr(bump, "SOURCE", source)
    monkeypatch.setattr(bump, "APP", app)
    assert bump.main(["--file", str(source)]) == 0
    assert '__version__ = "0.1.94"' in source.read_text()
    assert '__version__ = "0.1.94"  # the version of sandbox-ai it is released with' in app.read_text()
