"""board.sensitive() called every folder under /root "a system directory" (a warning that lowers a board's score):
also root's own projects, whose HOME /root is - tests/test_preset_gallery.py failed whenever the temporary folder
was under /root ("score 92") and passed under /tmp. The same for a HOME under /var (/var/home/USER: Fedora's
Silverblue and Kinoite), and a prefix matched names it does not name (/rootfs, /etcetera, /variants). Now a
folder inside your HOME is never a system directory, and the system directories are whole path components; /root
of another user, /etc, /var/lib, ... stay warned about."""
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import board as B  # noqa: E402

SYSTEM = ("warn", "a system directory")


def test_a_project_in_roots_own_home_is_no_system_directory(monkeypatch):
    monkeypatch.setenv("HOME", "/root")
    assert B.sensitive("/root/projects/app") is None
    assert B.sensitive("/root")[0] == "bad", "HOME itself stays refused"


def test_a_home_under_var_is_a_home(monkeypatch):
    monkeypatch.setenv("HOME", "/var/home/ana")
    assert B.sensitive("/var/home/ana/code/site") is None
    assert B.sensitive("/var/lib/containers") == SYSTEM


def test_the_system_directories_stay_warned_about(monkeypatch):
    monkeypatch.setenv("HOME", "/home/ana")
    for path in ("/root/projects/app", "/etc/nginx", "/usr/local/src", "/var/lib/x", "/boot", "/proc/1"):
        assert B.sensitive(path) == SYSTEM, path


def test_a_name_that_only_starts_like_a_system_directory_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", "/home/ana")
    for path in ("/rootfs/build", "/etcetera/notes", "/variants/a", "/usrdata/x"):
        assert B.sensitive(path) is None, path


def test_the_temporary_folder_does_not_change_the_verdict(monkeypatch, tmp_path):
    """Wherever the machine keeps its temporary files (TMPDIR under /root, /tmp, /var/tmp): a project inside HOME is
    an ordinary folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    os.chmod(tmp_path, 0o700)
    assert B.sensitive(str(project)) is None
    assert Path.home() == tmp_path
