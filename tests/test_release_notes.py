"""The release page's text (scripts/release_notes.py): every file the release builds, install and start for each way."""
import importlib.util
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts/release_notes.py")
rn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rn)


def test_every_release_file_is_named_and_linked():
    text = rn.notes("9.8.7")
    for name, _ in rn.FILES:
        name = name.format(version="9.8.7")
        assert f"[`{name}`](https://github.com/c0decave/sandbox-ai/releases/download/v9.8.7/{name})" in text
    # what release.yml uploads: the packages it builds and what make_dist.py writes
    release = (ROOT / ".github/workflows/release.yml").read_text()
    for name in ("sandbox-ai-x86_64.AppImage", "sandbox-ai-aarch64.AppImage", "sandbox-ai-arm64.dmg",
                 "sandbox-ai-x86_64.dmg", "sandbox-ai.pyz"):
        assert name in release or name.replace("x86_64", "${{ matrix.arch }}") in release or "AppImage" in release
        assert name in text


def test_every_way_to_install_and_the_first_steps():
    text = rn.notes("1.2.3", source="https://git.example.org/me/sandbox-ai")
    for needle in ("install.sh | bash", "pipx install sandbox-ai", "chmod +x sandbox-ai-x86_64.AppImage",
                   "--appimage-extract-and-run", "sandbox-ai.app", "Open Anyway", "sandbox-ai.pyz doctor",
                   "sandbox-ai doctor", "sandbox-ai build claude", "sandbox-ai login claude", "sandbox-ai update",
                   "pipx upgrade sandbox-ai", "sha256sum --ignore-missing -c SHA256SUMS", "Help → Tours"):
        assert needle in text, needle
    assert "https://git.example.org/me/sandbox-ai/releases/latest/download/install.sh" in text
    assert "github.com" not in text  # another source is used everywhere


def test_the_releases_own_highlights(tmp_path):
    (tmp_path / "sandbox").mkdir()
    (tmp_path / "sandbox/agent_sandbox.py").write_text('__version__ = "0.4.2"\n')
    assert rn.version_of(tmp_path) == "0.4.2"
    assert "## What is new" not in rn.notes("0.4.2", root=tmp_path)  # none written: no empty section
    (tmp_path / "docs/releases").mkdir(parents=True)
    (tmp_path / "docs/releases/v0.4.2.md").write_text("# sandbox-ai 0.4.2\n\n- **Faster.** Much.\n")
    text = rn.notes("0.4.2", root=tmp_path)
    assert "## What is new\n\n- **Faster.** Much.\n" in text and text.count("# sandbox-ai 0.4.2") == 1


def test_the_commands_it_names_exist():
    """Every `sandbox-ai SUBCOMMAND` in the text is one the command line has (hosts update may come later)."""
    help_text = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/main.py"), "--help"], capture_output=True,
                               text=True).stdout + (ROOT / "sandbox/main.py").read_text()
    for command in set(re.findall(r"(?:^|`)sandbox-ai ([a-z][a-z-]+)", rn.notes("1.0.0"), re.MULTILINE)):
        if command in ("claude", "codex", "opencode", "app", "bundle", "x86_64", "aarch64", "arm64"):
            continue
        assert f"sandbox-ai {command}" in help_text, command


def test_the_command_line(capsys):
    assert rn.main(["--version", "3.2.1", "--source", "https://example.org/x/"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# sandbox-ai 3.2.1\n") and "https://example.org/x/releases/download/v3.2.1/" in out
