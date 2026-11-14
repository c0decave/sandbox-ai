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
