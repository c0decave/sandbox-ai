#!/usr/bin/env python3
"""The text of a release page: what is new, which file is for whom, how to install, start and update.

    python3 scripts/release_notes.py [--version X.Y.Z] [--source URL] > notes.md

The highlights come from docs/releases/vX.Y.Z.md when it exists (written with the release); everything else is the
same for every release and names the files make_dist.py and release.yml build. release.yml passes the text to
`gh release create --notes-file`, publish_release.py to Gitea/Forgejo. Standard library only.
"""
import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = "https://github.com/c0decave/sandbox-ai"
GITHUB_LIMIT = 125_000  # characters of a release's body: GitHub refuses more (and Linux an argument over 128 KiB)

def release_assets():
    """sandbox/release_assets.py: the release's files, in one place."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_release_assets_for_notes",
                                                  ROOT / "sandbox" / "release_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def files(version):
    """The release files, in the order of the table: (file name, who it is for) - every one of release_assets.py."""
    assets = release_assets()
    return [(entry["name"], entry["for"]) for entry in assets.artifacts(version)] + [
        (assets.SUMS, "the checksums of every file above"),
        (assets.SIGNATURE, "the signature of SHA256SUMS by a release key: `sandbox-ai update`, install.sh and the app "
                           "accept a release only with it")]


def version_of(root=ROOT):
    text = (root / "sandbox" / "agent_sandbox.py").read_text()
    return re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE).group(1)


def highlights(version, root=ROOT):
    """The release's own notes (docs/releases/vX.Y.Z.md without its first heading), or None."""
    path = root / "docs" / "releases" / f"v{version}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip() or None


def notes(version, source=DEFAULT_SOURCE, root=ROOT):
    download = f"{source}/releases/download/v{version}"
    latest = f"{source}/releases/latest/download"
    parts = [f"# sandbox-ai {version}", "",
             "Run **Claude Code, Codex and OpenCode** in two verified sandbox layers (rootless Podman or Bubblewrap, "
             "then Firejail) with a default-deny network, an approval for everything else, secrets that never enter "
             "the sandbox and a privacy check of every push; on this machine or on every host you manage, from the "
             "command line, the desktop app or the browser.", ""]
    new = highlights(version, root)
    if new:
        parts += ["## What is new", "", new, ""]
    parts += ["## Which file do I need?", "", "| File | For |", "|---|---|"]
    for name, purpose in files(version):
        parts.append(f"| [`{name}`]({download}/{name}) | {purpose} |")
    parts += ["", "The PyPI package: `pipx install sandbox-ai-app` (or `pip install sandbox-ai-app`): the command "
                  "line `sandbox-ai` and the web interface, Linux and macOS, Python 3.9+; "
                  "`pip install 'sandbox-ai-app[gui]'` adds the desktop app (`sandbox-ai-app`, with PySide6).", "",
              "## Install and start", "",
              "Every way needs the same backend: on Linux rootless **Podman**, or **Bubblewrap with Firejail**; on "
              "macOS **Podman** with a rootless `podman machine` that shares `/Users`. `sandbox-ai doctor` checks the "
              "host and prints the commands that fix what is missing.", "",
              "**Linux or macOS, recommended** (the command line and the app, signed updates):", "", "```bash",
              f"curl -fsSL {latest}/install.sh | bash",
              "sandbox-ai doctor                      # checks Podman or Bubblewrap, Firejail, nftables",
              "sandbox-ai build claude                # the agent's image: claude, codex or opencode",
              "sandbox-ai claude -w ~/src/myproject   # the agent in its sandbox; Ctrl-b d detaches, it works on",
              "sandbox-ai-app                         # the desktop app", "```", "",
              "**Linux, the AppImage** (the app with the command line inside; `aarch64` on ARM):", "", "```bash",
              "mkdir -p ~/.local/bin && cd ~/.local/bin   # not into a project: an agent could rewrite it there",
              f"curl -fLO {latest}/sandbox-ai-x86_64.AppImage",
              "chmod +x sandbox-ai-x86_64.AppImage",
              "./sandbox-ai-x86_64.AppImage                  # the app",
              "./sandbox-ai-x86_64.AppImage cli doctor       # the command line: cli ARGS...",
              "ln -s sandbox-ai-x86_64.AppImage sandbox-ai   # called as sandbox-ai it is the command line", "```",
              "", "Without FUSE: `TMPDIR=$(mktemp -d) ./sandbox-ai-x86_64.AppImage --appimage-extract-and-run`.", "",
              "**macOS, the app** (`arm64` for Apple silicon, `x86_64` for Intel): open the `.dmg` and drag "
              "`sandbox-ai.app` to Applications. It is not notarised: on macOS 14 right-click > **Open** > **Open**; "
              "on macOS 15 open it once, then System Settings > Privacy & Security > **Open Anyway**. Then "
              "`brew install podman bash` and let `sandbox-ai doctor` set up `podman machine`.", "",
              "**From PyPI** (the command line and the web interface; with `[gui]` the app too; Linux and macOS):",
              "", "```bash",
              "pipx install sandbox-ai-app                   # or: python3 -m pip install --user sandbox-ai-app",
              "pipx install 'sandbox-ai-app[gui]'            # with the desktop app (PySide6)",
              "python3 -m pip install 'sandbox-ai-app[gui]'  # the same with pip, in a virtual environment",
              "sandbox-ai doctor",
              "sandbox-ai-app                                # the desktop app (installed with [gui])", "```", "",
              "**One file, nothing installed** (any Python 3.9+):", "", "```bash",
              "mkdir -p ~/.local/bin && cd ~/.local/bin",
              f"curl -fLO {latest}/sandbox-ai.pyz && chmod +x sandbox-ai.pyz",
              "./sandbox-ai.pyz doctor", "```", "",
              "## First steps", "",
              "1. `sandbox-ai doctor`: fix what it names.",
              "2. `sandbox-ai build claude` (or `codex`, `opencode`): the agent's image, once.",
              "3. `sandbox-ai login claude`: log in once; the login is kept for every sandbox.",
              "4. `sandbox-ai claude -w ~/src/myproject`: the agent works on that folder only; what it asks "
              "for appears under `sandbox-ai approvals` and in the app.",
              "5. In the app: **Help → Tours** walks through the first start, your own preset, approvals and more.", "",
              "## Update", "",
              "Every kind of installation updates itself from the release's files, signed (`SHA256SUMS.sig`, checked "
              "with the release keys the installed copy trusts): `sandbox-ai update` - in the app: **Settings › "
              "Updates › Install now…**. `--check` only looks, `--rollback` goes back to the version before.", "",
              "- install.sh installations: `sandbox-ai update` (a system one: `sudo sandbox-ai update`).",
              "- AppImage: `./sandbox-ai-x86_64.AppImage cli update` replaces the file; the `.pyz`: "
              "`./sandbox-ai.pyz update`; the macOS app: `/Applications/sandbox-ai.app/Contents/MacOS/sandbox-ai "
              "update` replaces `sandbox-ai.app`.",
              "- pip and pipx installations: `sandbox-ai update` installs this release's wheel with the "
              "environment's own pip (from PyPI instead: `pipx upgrade sandbox-ai-app`, `pip install -U "
              "sandbox-ai-app`).",
              "- A package or a pip installation of an earlier release (0.1.98 or older) does not replace itself "
              "yet: download the new file once (a pip installation of 0.1.94 or older is the package `sandbox-ai`: "
              "`pip uninstall sandbox-ai`, then `pip install sandbox-ai-app`).",
              "- Other hosts: `sandbox-ai hosts update NAME` (or `--all`) where that command exists.", "",
              "## Check the files", "", "```bash",
              f"curl -fLO {download}/SHA256SUMS",
              "sha256sum --ignore-missing -c SHA256SUMS     # macOS: shasum -a 256 --ignore-missing -c SHA256SUMS",
              "```", "",
              f"More: [README]({source}#readme) · [Quickstart]({source}/blob/v{version}/QUICKSTART.md) · "
              f"[Learning video]({source}/blob/v{version}/docs/video/README.md) · "
              f"[Releasing]({source}/blob/v{version}/RELEASING.md)", ""]
    return "\n".join(parts)


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", help="default: __version__ of sandbox/agent_sandbox.py")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help=f"the repository's URL (default {DEFAULT_SOURCE})")
    args = parser.parse_args(argv)
    version = args.version or version_of(root)
    text = notes(version, args.source.rstrip("/"), root=root)
    if len(text) > GITHUB_LIMIT:
        print(f"release_notes: the page has {len(text):,} characters, a release page has at most {GITHUB_LIMIT:,} "
              f"(GitHub refuses more): shorten docs/releases/v{version}.md", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
