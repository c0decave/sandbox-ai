"""scripts/publish_release.py checks what it uploads against SHA256SUMS before the release changes: a file that
SHA256SUMS does not list (users and installs could not check it), or files without any SHA256SUMS while
sandbox/release-signers lists keys, are refused unless --allow-unsigned says so (review round 2)."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("publish_release_for_checks", ROOT / "scripts" / "publish_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dist(tmp_path):
    out = tmp_path / "dist"
    out.mkdir()
    (out / "sandbox-ai.pyz").write_bytes(b"pyz")
    (out / "sandbox-ai-x86_64.AppImage").write_bytes(b"appimage")
    (out / "SHA256SUMS").write_text(f"{hashlib.sha256(b'pyz').hexdigest()}  sandbox-ai.pyz\n")
    return out


def test_a_file_sha256sums_does_not_list_is_refused(dist):
    publish = load()
    with pytest.raises(SystemExit, match="SHA256SUMS does not list sandbox-ai-x86_64.AppImage"):
        publish.check_files([dist / "SHA256SUMS", dist / "sandbox-ai.pyz", dist / "sandbox-ai-x86_64.AppImage"],
                            None, True)
    files = [dist / "SHA256SUMS", dist / "sandbox-ai.pyz"]
    assert publish.check_files(files, None, True) == files


def test_files_without_sha256sums_need_allow_unsigned_when_keys_are_listed(dist, monkeypatch):
    publish = load()
    keys = publish.release_keys()
    monkeypatch.setattr(publish, "release_keys", lambda: keys)
    monkeypatch.setattr(keys, "load", lambda *args, **kwargs: ["ssh-ed25519 AAAA... release"])
    with pytest.raises(SystemExit, match="no SHA256SUMS among the files"):
        publish.check_files([dist / "sandbox-ai.pyz"], None, False)
    assert publish.check_files([dist / "sandbox-ai.pyz"], None, True) == [dist / "sandbox-ai.pyz"]
    monkeypatch.setattr(keys, "load", lambda *args, **kwargs: [])
    assert publish.check_files([dist / "sandbox-ai.pyz"], None, False) == [dist / "sandbox-ai.pyz"]


def test_the_notes_come_from_a_file_and_a_body_github_refuses_is_refused_first(tmp_path, monkeypatch):
    """--notes-file: the page is no command-line argument (Linux takes at most 128 KiB); over 125,000 characters
    GitHub refuses the release, so nothing is asked of it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("publish_release_notes", ROOT / "scripts/publish_release.py")
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)
    asked = []
    monkeypatch.setattr(pr, "Client", lambda token: asked.append(token) or (_ for _ in ()).throw(AssertionError))
    monkeypatch.setenv("SANDBOX_AI_TOKEN", "t0ken-for-a-test")
    page = tmp_path / "notes.md"
    page.write_text("x" * 125_001)
    asset = tmp_path / "install.sh"
    asset.write_text("#!/bin/sh\n")
    with pytest.raises(SystemExit) as refused:
        pr.main(["--source", "https://github.com/c0decave/sandbox-ai", "--notes-file", str(page), str(asset)])
    assert "125,001 characters, GitHub takes at most 125,000" in str(refused.value.code) and asked == []
    with pytest.raises(SystemExit) as refused:
        pr.main(["--source", "https://github.com/c0decave/sandbox-ai", "--notes", "a", "--notes-file", str(page),
                 str(asset)])
    assert "not both" in str(refused.value.code) and asked == []
