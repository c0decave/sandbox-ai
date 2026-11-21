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
    for name, _ in rn.files("9.8.7"):
        assert f"[`{name}`](https://github.com/c0decave/sandbox-ai/releases/download/v9.8.7/{name})" in text
    # what release.yml uploads: the packages it builds and what make_dist.py writes
    release = (ROOT / ".github/workflows/release.yml").read_text()
    for name in ("sandbox-ai-x86_64.AppImage", "sandbox-ai-aarch64.AppImage", "sandbox-ai-arm64.dmg",
                 "sandbox-ai-x86_64.dmg", "sandbox-ai.pyz"):
        assert name in release or name.replace("x86_64", "${{ matrix.arch }}") in release or "AppImage" in release
        assert name in text


def test_every_way_to_install_and_the_first_steps():
    text = rn.notes("1.2.3", source="https://git.example.org/me/sandbox-ai")
    for needle in ("install.sh | bash", "pipx install sandbox-ai-app", "pip install 'sandbox-ai-app[gui]'", "chmod +x sandbox-ai-x86_64.AppImage",
                   "--appimage-extract-and-run", "sandbox-ai.app", "Open Anyway", "sandbox-ai.pyz doctor",
                   "sandbox-ai doctor", "sandbox-ai build claude", "sandbox-ai login claude", "sandbox-ai update",
                   "pipx upgrade sandbox-ai-app", "sha256sum --ignore-missing -c SHA256SUMS", "Help → Tours"):
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
                               text=True).stdout
    listed = {word for line in help_text.splitlines() if line.startswith("  ") and not line.startswith("   ")
              for word in re.split(r"\s{2,}", line.strip())[0].split(", ")}
    for command in set(re.findall(r"(?:^|`)sandbox-ai ([a-z][a-z-]+)", rn.notes("1.0.0"), re.MULTILINE)):
        if command in ("app", "bundle", "x86_64", "aarch64", "arm64"):
            continue
        assert command in listed, command


def test_the_command_line(capsys):
    assert rn.main(["--version", "3.2.1", "--source", "https://example.org/x/"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# sandbox-ai 3.2.1\n") and "https://example.org/x/releases/download/v3.2.1/" in out


def test_a_page_github_would_refuse_is_refused_here(tmp_path, capsys):
    """GitHub takes a release body of at most 125,000 characters, and Linux a single argument of at most 128 KiB:
    0.1.99's notes alone were 243 KB. The page is checked before anything is created; the message names the file."""
    (tmp_path / "docs/releases").mkdir(parents=True)
    (tmp_path / "docs/releases/v0.4.2.md").write_text("# sandbox-ai 0.4.2\n\n" + "- **A change.** Words.\n" * 6000)
    assert len(rn.notes("0.4.2", root=tmp_path)) > rn.GITHUB_LIMIT == 125_000
    assert rn.main(["--version", "0.4.2"], root=tmp_path) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "at most 125,000" in captured.err and "docs/releases/v0.4.2.md" in captured.err


def test_the_current_versions_page_fits_on_github():
    version = rn.version_of()
    assert len(rn.notes(version)) <= rn.GITHUB_LIMIT, f"shorten docs/releases/v{version}.md"
